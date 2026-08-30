"""Deterministic, read-only Armarium product projections.

This is deliberately a projection module rather than a second pipeline stage.  Its
only text-bearing input is ``canonical_clean_text``, a field the Armarium obtains
from the established Archetypus record after it has checked that exact lineage.
Every writer below receives that same field from one projection object; no writer
reads a witness, Perlectio, or alternate text-shaped field.

The package has one required first member, ``EXPORT_MANIFEST.json``.  Companion
formats are selected by the run-sealed ``formats.toml`` configuration.  ZIP is
stored (not compressed) with fixed metadata, so the container adds no
nondeterminism of its own.

**That is not the same as identical bytes, and this docstring used to claim it
was.**  The bundle embeds a SQLite database, and bytes 96-99 of every SQLite file
are ``SQLITE_VERSION_NUMBER`` for the library that last wrote it
(https://www.sqlite.org/fileformat.html#the_database_header).  So an identical
sealed projection produces identical bytes *for a given SQLite build*, and
different bytes across builds: measured at 3.46.1 in a Linux chamber against
3.53.4 on the maintainer's machine, with the same rows, schema and page size.
Content-addressing it in the run tree therefore binds the toolchain as well as
the data, which is carried to Tyrel rather than settled here.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Final
from zipfile import ZIP_STORED, BadZipFile, LargeZipFile, ZipFile, ZipInfo

from display import DISPLAY_CONVENTION, render_display, strip_display
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import ArmariumFormats, armarium_formats_from_record
from common.contracts.annotations import validate_annotations
from common.contracts.canonical import (
    canonical_bytes,
    canonical_text,
    digest_bytes,
    is_sha256,
    self_hash,
)
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.outcomes import (
    SILENT_PAGE_REASON,
    TEXT_STATUSES,
    ArmariumCategory,
    derive_record_text_status,
    require_approval,
    run_aggregate,
)
from common.contracts.stages import ARMARIUM
from common.contracts.uncertainty import utf8_round_trip
from common.contracts.uncertainty import validate as validate_uncertainty
from common.imaging import dimensions
from common.residual_ink import coverage_flag

_atomic_replace = os.replace
_unlink_at = os.unlink

EXPORT_MANIFEST_NAME: Final = "EXPORT_MANIFEST.json"
# The one name for the published archive, shared by run.py (which records it in the
# export payload) and bundle.py (which writes the file under it), so the two cannot
# drift into naming two different files the same product.
ARMARIUM_ARCHIVE_NAME: Final = "armarium-export.zip"
# v2: `claims.annotations` was renamed apart into `claims.semantic_annotations`
# beside the new measured `claims.transcription_annotations` — a rename under
# one id is the versioning miss the act-row bump below refuses, so the
# manifest's id moves with its claims.
# v3 adds required `claims.ink_map`; v2 readers and writers cannot consume that
# closed shape without an explicit schema boundary.
EXPORT_MANIFEST_SCHEMA: Final = "armarium-export-manifest.v3"
# v2: the damage record. `text_status` and `transcription_annotations` joined
# the row, and the bare `annotations`/`annotation_status` pair was renamed
# apart into `semantic_annotations`/`semantic_annotation_status`. A consumer
# keying on the schema id must not read a v1 shape out of a v2 row — the
# silent-rename-under-one-id miss (CR W15, on the sqlite id below) is the
# defect a version bump exists to prevent, so both ids move with the shape.
ACT_RECORD_SCHEMA: Final = "armarium-act.v2"
_ACT_RECORD_FIELDS: Final = frozenset(
    {
        "schema",
        "act_id",
        "act_key",
        "category",
        "canonical_clean_text",
        "canonical_text_sha256",
        "provenance",
        "source_regions",
        "uncertainty",
        "uncertainty_status",
        "text_status",
        "transcription_annotations",
        "semantic_annotations",
        "semantic_annotation_status",
        "witnesses",
        "perlectio_ref",
        "recensor_ref",
        "dissent_ref",
        "approval_ref",
        "reason",
        "evidence_refs",
    }
)
_REVIEW_ITEM_FIELDS: Final = frozenset(
    {"schema", "act_id", "act_key", "category", "reason", "evidence_refs"}
)
_SQLITE_SCHEMA: Final = "armarium-acts-sqlite.v2"
_SQLITE_USER_VERSION: Final = 2
# v2: every act-outcome row now REQUIRES `text_status` (exact-field-set
# checked), so a v1 sources file and a v2 reader are mutually unreadable.
# v3 adds required `ink_map_pages`, which lets a verifier derive the hold from
# source evidence instead of trusting the manifest claim it is checking.
SOURCES_SCHEMA: Final = "armarium-sources.v3"
SALVAGE_RECORD_SCHEMA: Final = "armarium-salvage-item.v1"
CANONICAL_TEXT_FIELD: Final = "canonical_clean_text"
CANONICAL_TEXT_ENCODING: Final = "utf-8"
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_SOURCE_ACCESS_REQUIRED: Final = "requires-source-access"
_RUN_ACCESS_REQUIRED: Final = "requires-retained-run-access"
_EMBEDDED: Final = "embedded"
_UNCERTAINTY_AVAILABLE: Final = "canonical-unicode-codepoint-offsets"
# The other half of the same declaration. An act with no established text, or a
# package with no literal-text format, has no offsets for a layer to anchor to or
# carry, so it says so in the one vocabulary every reader can compare.
_UNCERTAINTY_NOT_APPLICABLE: Final = "not-applicable"
_SEMANTIC_ANNOTATION_NOT_PRODUCED: Final = "not-produced-pending-architecture-approval"
# The manifest-level claim, distinct from the per-row status above since R8:
# a row saying "annotations not produced" beside a carried uncertainty layer
# needed to say *which* annotations, and the package claim says it once.
_SEMANTIC_ANNOTATIONS_CLAIM: Final = "semantic-annotations-not-produced"
# **Two layers, two words, because they are two things** (GLOSSARY: one concept
# per word). The *semantic* layer is `annotation_boundary.py`'s unbuilt
# person/date/kinship apparatus, and `not-produced` is true of it. The
# *transcription* layer is the Archetypus record's own `annotations` -- the
# `uncertain`/`illegible` marks over the established text, sealed at stage 6 and
# real. Every row here carried the first one's `annotations: []` and
# `annotation_status: not-produced` and nothing at all of the second, so an act
# whose record sealed a genuine illegible gap left the pipeline carrying a
# positive claim that no annotations were produced for it. Naming them apart is
# what stops one word answering for both again; neither takes the bare name.
_TRANSCRIPTION_ANNOTATIONS_CARRIED: Final = "archetypus-sealed-uncertain-and-illegible-marks"
_TRANSCRIPTION_ANNOTATIONS_NOT_APPLICABLE: Final = "not-applicable"
_LITERAL_TEXT_FORMATS: Final = ("text-bundle", "acts-database", "jsonl")
_PIXEL_REFERENCE_CLAIM: Final = "reference validity only; pixel resolution requires source access"
_PIXEL_EMBEDDED_CLAIM: Final = (
    "embedded pixels are packaged and opened by clean-machine verification"
)
TERMINAL_LEDGER_SCHEMA: Final = "armarium-terminal-ledger.v1"
_LEDGER_DENOMINATOR: Final = (
    "every submitted source page or frame, every sealed page, and every proposed act"
)
_SOURCE_GRANULARITY: Final = (
    "one unit per source page or frame ordinal bound into run.json at admission, door "
    "refusals and duplicates included"
)
# Spec 11 asks the denominator to start at "every submitted file". It cannot: run.json
# binds one ordinal per page, so this is the gap, published on the bundle's own face
# rather than left for a reader to hit.
_CONTAINER_GRANULARITY_LIMIT: Final = (
    "a multi-page PDF/TIFF container is represented by one unit per page or frame rather "
    "than one unit for the submitted file; the file's own single terminal category is not "
    "represented and cannot be counted off this ledger"
)
_ACT_PARTITION_DENOMINATOR: Final = "proposal-seal expected acts"
# The clustered spelling of the same claim. A logical act over two captures is
# two proposal-seal rows and one terminal category, so a bundle that counted it
# under the name above would be reporting a number nobody measured. Named as its
# own denominator, with the seal's own row count carried beside it, exactly as
# `_SALVAGE_CLAIM_FIELDS` keeps its two shapes apart rather than unioning them.
_LOGICAL_ACT_PARTITION_DENOMINATOR: Final = "physical-act-partition logical acts"
_PAGE_CENSUS_DENOMINATOR: Final = "run.json source-page/frame rows"
_SALVAGE_PROMOTION_CLAIM: Final = (
    "recorded approval then pipeline re-entry; never export-time act promotion"
)
_SALVAGE_ABSENCE_REASON: Final = "this run has no sealed salvage inventory to account for"
_DISPLAY_REASON: Final = (
    "the rendering is not fed this package's canonical uncertainty layer, which travels "
    "beside each literal instead; marking spans inside a displayed reading would exercise "
    "a convention that remains Tyrel's choice at this gate"
)
_COMPLETED_CATEGORIES: Final = frozenset(
    {
        ArmariumCategory.DELIVERED.value,
        ArmariumCategory.EXCLUDED_WITH_APPROVAL.value,
        ArmariumCategory.CONFIRMED_BLANK.value,
    }
)
# Presence of any one of these marks a record as salvage-tier whatever else it
# carries. Checked on every act the projection accepts, so a salvage item cannot enter
# the acts namespace by resembling one.
_SALVAGE_DISCRIMINANT_FIELDS: Final = frozenset(
    {"salvage_id", "harvested_content", "harvest_kind", "content", "promotion"}
)
_SALVAGE_RESERVED_FIELDS: Final = frozenset(
    {
        "act_id",
        "act_key",
        "canonical_clean_text",
        "canonical_text_sha256",
        "category",
        "dissent_ref",
        "perlectio_ref",
        "recensor_ref",
        "approval_ref",
        "text",
    }
)


@dataclass(frozen=True)
class ArmariumProjection:
    """The one checked record every product writer is allowed to see.

    ``acts`` contains one record per expected act, each with a closed Armarium
    category.  Delivered records alone have a literal ``canonical_clean_text``;
    all other records explicitly carry ``None`` rather than an invented empty
    reading.  ``salvage_items`` is deliberately a separate collection, and the
    writer refuses any record that resembles an act.
    """

    fixture_id: str
    scenario: str
    config_digest: str
    aggregate: dict[str, Any]
    acts: tuple[dict[str, Any], ...]
    pages: tuple[dict[str, Any], ...]
    source_manifest: tuple[dict[str, Any], ...]
    expected_acts: int
    witness_chairs: tuple[str, ...]
    witness_floor: int
    # The non-text inputs from which ``run_aggregate`` measured the status and
    # reasons.  Carrying this basis lets the exported claim be recomputed rather
    # than merely checked for a plausible-looking nonempty reason list.
    aggregate_basis: dict[str, Any]
    # ``None`` means this run has no sealed salvage inventory at all.  That is
    # materially different from a sealed, empty inventory: the former must not
    # be exported as a reassuring count of zero.
    salvage_items: tuple[dict[str, Any], ...] | None = None
    # One row per sealed page retains the initial map outcome and any later
    # re-measurement. The held set is derived, never stored beside this evidence.
    ink_map_pages: tuple[dict[str, Any], ...] = ()
    # How many rows the Designator's proposal seal actually held, when this run's
    # acts are logical acts over a `physical-act-partition.v1` rather than the
    # image-local acts the seal names one-for-one.
    #
    # `expected_acts` is the act *terminal* denominator, and consult §5.2 makes
    # it `logical_expected_count` for a clustered run -- one terminal category
    # per logical act. But the manifest's fixed claim calls that denominator
    # "proposal-seal expected acts", and for a cluster the two numbers differ:
    # a two-capture physical act is two seal rows and one logical act. Exporting
    # the logical count under the local count's name is a claim about something
    # that was not measured (GOVERNANCE 10), and dropping the local count
    # entirely loses the evidence a reader needs to reconcile the bundle against
    # the seal (GOVERNANCE 2). So a clustered run carries both, and the ledger
    # says which is which -- consult §5.2's "the terminal ledger reports both
    # counts explicitly". `None` is the ordinary image-local run, where the two
    # numbers are the same number and the claim already says so.
    local_proposal_rows: int | None = None


@dataclass(frozen=True)
class ArmariumBundle:
    """The bytes and manifest facts that the stage seals as one blob."""

    data: bytes
    manifest: dict[str, Any]


def _uncertainty_claim(formats: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Measure which selected formats carry canonical uncertainty."""
    carried_by = sorted(set(formats) & set(_LITERAL_TEXT_FORMATS))
    return {
        "status": _UNCERTAINTY_AVAILABLE if carried_by else _UNCERTAINTY_NOT_APPLICABLE,
        "offset_unit": "unicode-code-point",
        "carried_by": carried_by,
    }


def _transcription_annotations_claim(formats: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Measure which selected formats carry the Archetypus's own annotation layer.

    A measurement rather than a constant, exactly like the uncertainty claim it
    sits beside and unlike the semantic-annotation claim below it: this layer is
    produced, it rides in the literal-text formats with the text it marks up, and
    a package that named a format it does not carry it in would be describing a
    different package. Published so that `claims` cannot be read as saying no
    annotations of any kind exist -- which is what it said while the only
    annotation claim in it was the semantic layer's `not-produced`.
    """
    carried_by = sorted(set(formats) & set(_LITERAL_TEXT_FORMATS))
    return {
        "status": (
            _TRANSCRIPTION_ANNOTATIONS_CARRIED
            if carried_by
            else _TRANSCRIPTION_ANNOTATIONS_NOT_APPLICABLE
        ),
        "authority": "archetypus",
        "text_writable": False,
        "carried_by": carried_by,
    }


def canonical_text_sha256(text: str) -> str:
    """Hash one literal clean-text value exactly as every format encodes it."""
    if not isinstance(text, str):
        raise SchemaRefusal("a canonical clean-text hash requires one literal string")
    return digest_bytes(text.encode(CANONICAL_TEXT_ENCODING))


def build_armarium_bundle(
    projection: ArmariumProjection,
    formats: ArmariumFormats,
    read_bytes: Callable[[str], bytes],
) -> ArmariumBundle:
    """Write the selected product formats from one validated projection.

    ``read_bytes`` is used only for image blobs already verified by Armarium's
    upstream boundary.  It is never used to discover or recover text.
    """
    _validate_projection(projection)
    if projection.salvage_items is not None:
        _validate_salvage_items(projection.salvage_items)
    if projection.salvage_items and "salvage-tier" not in formats.formats:
        raise SchemaRefusal("a non-empty sealed salvage inventory requires the salvage-tier format")
    _validate_projection_region_bindings(projection)

    members: dict[str, bytes] = {}
    source_rows, embedded = _source_rows(projection.pages, formats.embed_pixels, read_bytes)
    projected_acts, embedded_crops = _acts_with_source_references(
        projection.acts, formats.embed_pixels, read_bytes
    )
    projected_salvage, embedded_salvage = _salvage_with_source_references(
        projection.salvage_items, formats.embed_pixels, read_bytes
    )
    projection = replace(
        projection,
        acts=tuple(_mark_retained_references(record) for record in projected_acts),
        salvage_items=(
            tuple(_mark_retained_references(record) for record in projected_salvage)
            if projected_salvage is not None
            else None
        ),
    )
    members["sources.json"] = canonical_bytes(
        {
            "schema": SOURCES_SCHEMA,
            "pages": source_rows,
            "regions": _source_regions(projection.acts),
            "act_citations": _act_citations(projection.acts),
            "act_outcomes": _act_outcomes(projection.acts),
            "aggregate_basis": projection.aggregate_basis,
            "witness_chairs": list(projection.witness_chairs),
            "witness_floor": projection.witness_floor,
            "salvage_regions": _salvage_regions(projection.salvage_items),
            # Source evidence must travel with the claim so a clean-machine
            # verifier can derive the page-level hold independently.
            "ink_map_pages": list(projection.ink_map_pages),
        }
    )

    if "text-bundle" in formats.formats:
        members.update(_text_bundle_members(projection.acts, source_rows))
    if "acts-database" in formats.formats:
        members["acts.sqlite"] = _acts_database_bytes(projection.acts)
    if "jsonl" in formats.formats:
        members["acts.jsonl"] = _jsonl_bytes(_act_json_records(projection.acts))
    if "review-items" in formats.formats:
        members["review-items.jsonl"] = _jsonl_bytes(_review_records(projection.acts))
    if "salvage-tier" in formats.formats:
        members["salvage/items.jsonl"] = _jsonl_bytes(
            _salvage_records(projection.salvage_items or ())
        )
    members.update(embedded)
    members.update(embedded_crops)
    members.update(embedded_salvage)

    manifest = _export_manifest(projection, formats, members)
    archive_members = {EXPORT_MANIFEST_NAME: canonical_bytes(manifest), **members}
    data = _zip_bytes(archive_members)
    # Validate the fully assembled object before its caller can make it a
    # content-addressed run-tree blob.  A product whose own manifest, references,
    # or embedded pixels do not survive a clean extraction is not an export to
    # publish and must fail before the atomic store writer is reached.
    with tempfile.TemporaryDirectory(prefix="armarium-verify-") as directory:
        clean_root = Path(directory)
        manifest_report = verify_export_bundle(data, clean_root)
        literal_formats = set(_LITERAL_TEXT_FORMATS) & set(formats.formats)
        if len(literal_formats) >= 2:
            _compare_literal_projections(clean_root, _manifest_formats(manifest_report))
    return ArmariumBundle(data=data, manifest=manifest)


def verify_export_bundle(data: bytes, clean_root) -> dict[str, Any]:
    """Extract and independently verify a package without its source run tree.

    With pixels embedded this opens their packaged bytes.  With embedding off it
    verifies only the declared run-relative reference shape and explicitly does
    not claim that pixels can resolve on the clean machine. The returned copy of
    the manifest adds a verifier-local ``verification`` report; that report is not
    represented as though it were part of the sealed package manifest.
    """
    root = Path(clean_root)
    root_fd = _prepare_clean_root(root)
    try:
        # "These bytes are not an archive at all" is one of the refusals this function
        # exists to make, not an exception for its caller to work out.
        try:
            archive = ZipFile(BytesIO(data))
        except (BadZipFile, LargeZipFile, OSError) as error:
            raise SchemaRefusal("an Armarium package is not a readable ZIP archive") from error
        with archive:
            names = archive.namelist()
            if not names or names[0] != EXPORT_MANIFEST_NAME:
                raise SchemaRefusal("an Armarium package must begin with EXPORT_MANIFEST.json")
            _validate_archive_member_names(names)
            # A stored member's extracted size cannot exceed the archive's own physical
            # size, so refusing every other compression method -- before a byte is
            # decompressed -- is what bounds a decompression bomb by construction rather
            # than by an arbitrary cap. `_zip_bytes` never writes anything but stored.
            for info in archive.infolist():
                if info.compress_type != ZIP_STORED:
                    raise SchemaRefusal(
                        f"package member {info.filename!r} is compressed; an Armarium "
                        "package is only ever written stored, never compressed"
                    )
            _extract_archive_members(archive, root_fd, names)
        actual_names = _ordinary_member_names(root_fd)
        _require_root_identity(root, root_fd)
    finally:
        os.close(root_fd)

    try:
        manifest = json.loads((root / EXPORT_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal("EXPORT_MANIFEST.json is not readable canonical JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != EXPORT_MANIFEST_SCHEMA:
        raise SchemaRefusal("the package has no recognized EXPORT_MANIFEST schema")
    if manifest.get("self_hash") != self_hash(manifest):
        raise SchemaRefusal("EXPORT_MANIFEST.json fails its self-hash")
    # Before any claim is read: a self-hash proves the manifest was not edited after
    # it was written, never that what it says is a thing this build writes.
    _verify_manifest_field_closure(manifest)
    run = manifest["run"]
    _require_sha256(run.get("config_digest"), "EXPORT_MANIFEST.json run configuration digest")

    listed = manifest["members"]
    if not isinstance(listed, list) or not listed:
        raise SchemaRefusal("EXPORT_MANIFEST.json has no member digest inventory")
    expected_names = {EXPORT_MANIFEST_NAME}
    listed_names: set[str] = set()
    for item in listed:
        _require_exact_fields(
            item, _MANIFEST_MEMBER_FIELDS, subject="a manifest member inventory row"
        )
        name, sha256, byte_count = item["path"], item["sha256"], item["bytes"]
        if not isinstance(name, str) or not isinstance(sha256, str):
            raise SchemaRefusal("a manifest member inventory row lacks path or digest")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise SchemaRefusal("a manifest member inventory row lacks a non-negative byte count")
        _validate_member_name(name)
        if name == EXPORT_MANIFEST_NAME or name in listed_names:
            raise SchemaRefusal("EXPORT_MANIFEST.json repeats or inventories its own member")
        listed_names.add(name)
        _require_sha256(sha256, f"manifest member {name!r} digest")
        expected_names.add(name)
        member = root / name
        if not member.is_file():
            raise SchemaRefusal(f"package member {name!r} does not match its manifest digest")
        contents = member.read_bytes()
        if digest_bytes(contents) != sha256:
            raise SchemaRefusal(f"package member {name!r} does not match its manifest digest")
        if len(contents) != byte_count:
            raise SchemaRefusal(f"package member {name!r} does not match its manifest byte count")
    if actual_names != expected_names:
        raise SchemaRefusal("the extracted package members disagree with EXPORT_MANIFEST.json")

    formats = _manifest_formats(manifest)
    sources = _load_sources(root)
    _verify_source_references(sources["pages"], root)
    _verify_region_references(sources, root)
    _verify_salvage_region_references(sources, root)
    _act_citation_sources(sources)
    _act_outcome_sources(sources)
    _verify_retained_references(sources)
    _verify_manifest_source_counts(manifest, sources)
    _verify_pixel_claims(manifest, formats, sources)
    _verify_display_claim(manifest)
    _verify_retained_run_claim(manifest)
    _verify_canonical_text_claim(manifest)
    _verify_annotations_claims(manifest)
    _verify_uncertainty_claim(manifest)
    _verify_exact_product_members(formats, sources, actual_names)
    search_fold_verification = _verify_product_accounting(root, manifest, formats, sources)
    verification = {}
    if search_fold_verification is not None:
        verification["search_fold"] = search_fold_verification
    return {**manifest, "verification": verification}


def _prepare_clean_root(root: Path) -> int:
    """Permit a reusable extraction root, but no link or special-file branch.

    Ordinary files may remain after a refusal; extraction replaces them atomically
    so even a pre-existing hard link cannot redirect a write outside this tree. The
    returned directory descriptor pins the root inode across preflight and extraction;
    callers must close it.
    """
    try:
        if root.is_symlink():
            raise SchemaRefusal("the clean extraction root is a link, not a new package directory")
        root.mkdir(parents=True, exist_ok=True)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise SchemaRefusal("the clean extraction root cannot be prepared") from error
    try:
        _ordinary_member_names(root_fd)
        _require_root_identity(root, root_fd)
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_root_identity(root: Path, root_fd: int) -> None:
    """Prove the path still names the directory descriptor opened at preflight."""
    try:
        named = os.stat(root, follow_symlinks=False)
        opened = os.fstat(root_fd)
    except OSError as error:
        raise SchemaRefusal("the clean extraction root changed during verification") from error
    if not stat.S_ISDIR(named.st_mode) or not _same_inode(named, opened):
        raise SchemaRefusal("the clean extraction root changed during verification")


def _ordinary_member_names(root_fd: int) -> set[str]:
    """Inventory regular files through pinned directory descriptors only."""
    names: set[str] = set()
    root_device = os.fstat(root_fd).st_dev

    def walk(directory_fd: int, prefix: PurePosixPath) -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise SchemaRefusal("the clean extraction directory cannot be listed") from error
        for entry in entries:
            relative = (prefix / entry.name).as_posix()
            try:
                named = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(named.st_mode):
                    raise SchemaRefusal(
                        f"the clean extraction tree contains a link at {relative!r}"
                    )
                if stat.S_ISDIR(named.st_mode):
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    try:
                        opened = os.fstat(child_fd)
                        if opened.st_dev != root_device or not _same_inode(named, opened):
                            raise SchemaRefusal(
                                f"the clean extraction directory {relative!r} changed or "
                                "crosses onto another device"
                            )
                        walk(child_fd, prefix / entry.name)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(named.st_mode):
                    names.add(relative)
                else:
                    raise SchemaRefusal(
                        f"the clean extraction tree contains a non-regular entry at {relative!r}"
                    )
            except OSError as error:
                raise SchemaRefusal(
                    f"the clean extraction entry {relative!r} cannot be inspected"
                ) from error

    walk(root_fd, PurePosixPath())
    return names


def _open_member_parent(root_fd: int, parents: tuple[str, ...]) -> int:
    """Open a member's parent from the pinned root, refusing links and mount crossings."""
    directory_fd = os.dup(root_fd)
    root_device = os.fstat(root_fd).st_dev
    try:
        for component in parents:
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            named = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode):
                raise SchemaRefusal("a package member parent is not an ordinary directory")
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            opened = os.fstat(child_fd)
            if opened.st_dev != root_device or not _same_inode(named, opened):
                os.close(child_fd)
                raise SchemaRefusal(
                    "a package member parent changed during extraction or crosses onto "
                    "another device"
                )
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _temporary_member(parent_fd: int, target_name: str) -> tuple[int, str]:
    """Create one unpredictable no-follow temporary file relative to a pinned parent."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _attempt in range(100):
        temporary_name = f".{target_name}.extracting-{secrets.token_hex(16)}"
        try:
            return os.open(temporary_name, flags, 0o600, dir_fd=parent_fd), temporary_name
        except FileExistsError:
            continue
    raise SchemaRefusal("a collision-free extraction temporary file could not be reserved")


def _extract_archive_members(archive: ZipFile, root_fd: int, names: list[str]) -> None:
    """Replace validated members atomically, never through an existing file link.

    The preflight walk has refused symlinks and special entries in every existing
    directory. Every later traversal is descriptor-relative and no-follow, so swapping
    a checked directory path to a symlink cannot redirect the write. A new temporary
    regular file plus descriptor-relative replace also breaks a pre-existing hard link
    instead of modifying the inode it shares outside the tree. The caller has already
    bounded every member by requiring stored ZIP entries.
    """
    for name in names:
        parts = PurePosixPath(name).parts
        parent_fd: int | None = None
        temporary_name: str | None = None
        try:
            parent_fd = _open_member_parent(root_fd, parts[:-1])
            temporary_fd, temporary_name = _temporary_member(parent_fd, parts[-1])
            with os.fdopen(temporary_fd, "wb") as handle, archive.open(name, "r") as source:
                shutil.copyfileobj(source, handle)
            _atomic_replace(
                temporary_name,
                parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = None
        except (BadZipFile, OSError, RuntimeError) as error:
            raise SchemaRefusal(f"package member {name!r} could not be extracted safely") from error
        finally:
            try:
                if temporary_name is not None and parent_fd is not None:
                    try:
                        _unlink_at(temporary_name, dir_fd=parent_fd)
                    except OSError as cleanup_error:
                        raise SchemaRefusal(
                            f"package member {name!r} could not be extracted safely, and its "
                            f"temporary file {temporary_name} could not be removed; the clean "
                            "root is incomplete and must not be used"
                        ) from cleanup_error
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)


_MANIFEST_FIELDS: Final = frozenset(
    {
        "schema",
        "canonical_text",
        "run",
        "formats",
        "claims",
        "aggregate",
        "aggregate_basis",
        "witness_chairs",
        "witness_floor",
        "members",
        "self_hash",
    }
)
_MANIFEST_RUN_FIELDS: Final = frozenset({"fixture_id", "scenario", "config_digest"})
_MANIFEST_MEMBER_FIELDS: Final = frozenset({"path", "sha256", "bytes"})
_MANIFEST_CLAIM_FIELDS: Final = frozenset(
    {
        "status",
        "partial_reasons",
        "terminal_ledger",
        "act_partition",
        "submission_inventory",
        "page_census",
        "pixels",
        "retained_run_references",
        "semantic_annotations",
        "transcription_annotations",
        "uncertainty",
        "display",
        "salvage",
        "ink_map",
    }
)
_ACT_PARTITION_CLAIM_FIELDS: Final = {
    _ACT_PARTITION_DENOMINATOR: frozenset(
        {"denominator", "expected_count", "counted", "reconciles", "categories", "act_keys"}
    ),
    _LOGICAL_ACT_PARTITION_DENOMINATOR: frozenset(
        {
            "denominator",
            "expected_count",
            "counted",
            "reconciles",
            "categories",
            "act_keys",
            "local_proposal_rows",
            "logical_membership",
        }
    ),
}
_CLAIM_SUBFIELDS: Final = {
    "submission_inventory": frozenset(
        {
            "status",
            "granularity",
            "limit",
            "observed_source_page_rows",
            "observed_distinct_declared_paths",
        }
    ),
    "page_census": frozenset({"denominator", "counted", "status"}),
    "ink_map": frozenset({"denominator", "held_pages"}),
    "pixels": frozenset({"embedded", "resolution_claim"}),
    "display": frozenset(
        {
            "convention",
            "status",
            "alters_stored_text",
            "renders_canonical_uncertainty",
            "exercised_against_real_spans",
            "reason",
        }
    ),
}
# Two shapes, because an unproduced salvage tier says why and an accounted one has
# nothing to explain.  Named as two closed sets rather than one union: a package
# claiming `accounted` while carrying the absence reason is describing neither.
_SALVAGE_CLAIM_FIELDS: Final = {
    "accounted": frozenset({"namespace", "status", "count", "promotion"}),
    "not-produced-no-sealed-salvage-inventory": frozenset(
        {"namespace", "status", "count", "reason", "promotion"}
    ),
}


def _require_exact_fields(value: object, expected: frozenset[str], *, subject: str) -> dict:
    if not isinstance(value, dict):
        raise SchemaRefusal(f"{subject} is not an object")
    if set(value) != expected:
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value))
        raise SchemaRefusal(
            f"{subject} has an unrecognized field set (unexpected {unknown}, missing {missing})"
        )
    return value


def _verify_manifest_field_closure(manifest: dict[str, Any]) -> None:
    """Reject unmeasured claims hidden in otherwise self-consistent JSON.

    Blocks compared wholesale against recomputed values are already closed. Only
    blocks read field by field need explicit key sets here; duplicating the other
    shapes would create a second schema that could drift from their recomputation.
    """
    _require_exact_fields(manifest, _MANIFEST_FIELDS, subject="EXPORT_MANIFEST.json")
    run = _require_exact_fields(
        manifest["run"], _MANIFEST_RUN_FIELDS, subject="the manifest run binding"
    )
    if any(
        not isinstance(run.get(field), str) or not run[field].strip()
        for field in ("fixture_id", "scenario")
    ):
        raise SchemaRefusal(
            "the manifest run binding has no non-blank fixture and scenario identities"
        )
    claims = _require_exact_fields(
        manifest["claims"], _MANIFEST_CLAIM_FIELDS, subject="the manifest claims block"
    )
    for name, fields in _CLAIM_SUBFIELDS.items():
        _require_exact_fields(claims[name], fields, subject=f"the manifest {name} claim")
    act_partition = claims["act_partition"]
    if not isinstance(act_partition, dict):
        raise SchemaRefusal("the manifest act_partition claim is not an object")
    act_partition_fields = _ACT_PARTITION_CLAIM_FIELDS.get(act_partition.get("denominator"))
    if act_partition_fields is None:
        raise SchemaRefusal("the manifest act denominator is not this build's fixed claim")
    _require_exact_fields(
        act_partition, act_partition_fields, subject="the manifest act_partition claim"
    )
    salvage = claims["salvage"]
    if not isinstance(salvage, dict):
        raise SchemaRefusal("the manifest salvage claim is not an object")
    status = salvage.get("status")
    expected = _SALVAGE_CLAIM_FIELDS.get(status) if isinstance(status, str) else None
    if expected is None:
        raise SchemaRefusal("EXPORT_MANIFEST.json has an invalid salvage-tier status")
    _require_exact_fields(salvage, expected, subject="the manifest salvage claim")
    rows = claims["act_partition"]["categories"]
    if not isinstance(rows, list):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no category rows")
    for row in rows:
        _require_exact_fields(
            row,
            frozenset({"category", "count", "act_ids"}),
            subject="an act partition category row",
        )
    if claims["page_census"]["denominator"] != _PAGE_CENSUS_DENOMINATOR:
        raise SchemaRefusal("the manifest page denominator is not this build's fixed claim")


def verify_projection_identity(data: bytes, clean_root) -> dict[str, str]:
    """Prove every selected literal-text format carries identical clean text.

    This is intentionally distinct from package-digest verification.  A package
    can be internally self-consistent while one product writer has transformed
    the literal differently; this compares the values and their UTF-8 hashes
    across the text bundle, SQLite literal column, and JSONL hand-off.
    """
    manifest = verify_export_bundle(data, clean_root)
    formats = _manifest_formats(manifest)
    return _compare_literal_projections(clean_root, formats)


def verify_delivered_bundle(data: bytes, clean_root) -> dict[str, Any]:
    """Package integrity *and* GOVERNANCE 5's one text, in a single extraction.

    ``verify_export_bundle`` deliberately answers only "is this package internally
    whole", and a package can pass it with two literal formats carrying different
    readings of the same act -- ``verify_projection_identity`` is the separate
    question, and separating them is what lets each refusal name its own defect.
    But ``EXPORT_MANIFEST.json`` asserts ``canonical_text.identity_verified_across``
    as a *fact about the package*, and the publish path is the last gate before a
    recipient who has only these bytes. A gate that leaves the manifest's own
    one-text claim unchecked is asserting it rather than verifying it, so the two
    questions are asked together here and the answer to each is reported.

    The identity comparison reads the members already extracted by the integrity
    pass rather than unpacking the archive a second time.
    """
    manifest = verify_export_bundle(data, clean_root)
    formats = _manifest_formats(manifest)
    compared = sorted(set(_LITERAL_TEXT_FORMATS) & set(formats.formats))
    if len(compared) >= 2:
        _compare_literal_projections(clean_root, formats)
        identity = {"status": "verified", "compared_formats": compared}
    else:
        # Said rather than left as a silent absence: with fewer than two literal
        # formats there is nothing to compare, and a reader must be able to tell
        # that from a comparison that was made.
        identity = {
            "status": "not-applicable-fewer-than-two-literal-formats",
            "compared_formats": compared,
        }
    verification = {**manifest.get("verification", {}), "projection_identity": identity}
    return {**manifest, "verification": verification}


def _compare_literal_projections(root: Path, formats: ArmariumFormats) -> dict[str, str]:
    """Compare already-verified literal members without extracting the package again.

    Each format's rows carry ``(text, digest, uncertainty, text_status,
    transcription_annotations)``: every layer rides in the same equality check as
    the text it describes, so a format that silently diverged on one of them --
    present, well-formed, but *different* from what the other formats say -- fails
    identity exactly as a diverging literal would (U3: these are projected
    readings like the text they sit beside, and GOVERNANCE 5 does not stop at the
    characters). A `text_status` that read `partial` in one product and
    `established` in another would be two deliverables disagreeing about whether
    the same act is damaged.
    """
    projections: dict[str, dict[str, tuple]] = {}
    selected_literal_formats = [name for name in _LITERAL_TEXT_FORMATS if name in formats.formats]
    if len(selected_literal_formats) < 2:
        raise SchemaRefusal("projection identity needs at least two selected literal-text formats")

    for name in selected_literal_formats:
        if name == "text-bundle":
            projections[name] = _text_bundle_literals(root)
        elif name == "acts-database":
            projections[name] = _database_literals(root / "acts.sqlite")
        elif name == "jsonl":
            projections[name] = _jsonl_literals(root / "acts.jsonl")

    baseline_name, baseline = next(iter(projections.items()))
    for name, records in projections.items():
        if records != baseline:
            raise SchemaRefusal(
                f"canonical clean-text, uncertainty or damage-record projection differs "
                f"between {baseline_name} and {name}"
            )
    return {act_id: record[0] for act_id, record in baseline.items()}


INK_MAP_DENOMINATOR: Final = "Unit 9 ink-map sealed pages"
_INK_MAP_ROW_FIELDS: Final = frozenset({"ordinal", "initial_outcome", "remeasured"})
_INK_MAP_REMEASURE_FIELDS: Final = frozenset(
    {"total_ink_pixels", "outside_ink_pixels", "edge_band_pixels"}
)
_UNCLAIMED_EDGE_INK: Final = "unclaimed-edge-ink"
_INK_MAP_OUTCOMES: Final = frozenset({"mapped", _UNCLAIMED_EDGE_INK})


def _validate_ink_map_pages(rows: Any, subject: str) -> list[dict[str, Any]]:
    """Close the ink-map source rows before anything derives a hold from them.

    A `mapped` page carries `remeasured: None`, not a zeroed measurement: this
    stage re-measures only the pages Unit 9 actually flagged, and writing zeros
    for the rest would put a measurement nobody took into the record
    (GOVERNANCE 10). The absence is recorded as absence.
    """
    if not isinstance(rows, list | tuple):
        raise SchemaRefusal(
            f"{subject} does not carry its ink-map page rows as a list. Its page-level edge "
            "holds cannot be derived or checked. Rebuild the export from an Armarium v3 source "
            "graph."
        )
    ordinals: set[int] = set()
    validated: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != _INK_MAP_ROW_FIELDS:
            raise SchemaRefusal(
                f"{subject} has an ink-map row that is not its closed shape. The verifier cannot "
                "tell which page measurement the row states. Rebuild the export from an intact "
                "Armarium v3 source graph."
            )
        ordinal, outcome, remeasured = row["ordinal"], row["initial_outcome"], row["remeasured"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise SchemaRefusal(
                f"{subject} has an ink-map row without an integer page ordinal. The verifier "
                "cannot bind its measurement to a sealed page. Rebuild the export from the "
                "sealed page inventory."
            )
        if ordinal in ordinals:
            raise SchemaRefusal(
                f"{subject} repeats ink-map page ordinal {ordinal}. Continuing would select one "
                "of two page findings by row order. Rebuild the export from the sealed page "
                "inventory."
            )
        ordinals.add(ordinal)
        if outcome not in _INK_MAP_OUTCOMES:
            raise SchemaRefusal(
                f"{subject} has an unknown ink-map page outcome. The verifier cannot determine "
                "whether that page is held or released. Rebuild the export with this Armarium "
                "version before using it."
            )
        if outcome == _UNCLAIMED_EDGE_INK:
            if not isinstance(remeasured, dict) or set(remeasured) != _INK_MAP_REMEASURE_FIELDS:
                raise SchemaRefusal(
                    f"{subject} has a flagged ink-map page with no re-measurement to resolve it. "
                    "The page's terminal hold cannot be derived from an absent measurement. "
                    "Rebuild the export from the retained Ink Map and Designator evidence."
                )
            if any(
                not isinstance(remeasured[field], int)
                or isinstance(remeasured[field], bool)
                or remeasured[field] < 0
                for field in sorted(_INK_MAP_REMEASURE_FIELDS)
            ):
                raise SchemaRefusal(
                    f"{subject} has an invalid ink-map re-measurement. The shared coverage "
                    "gate cannot evaluate those counts. Rebuild the export from the retained Ink "
                    "Map evidence."
                )
            if remeasured["outside_ink_pixels"] > remeasured["total_ink_pixels"]:
                raise SchemaRefusal(
                    f"{subject} re-measures more outside ink than the page carries at all. The "
                    "counts are internally impossible and cannot decide a page hold. Rebuild the "
                    "export from the retained Ink Map evidence."
                )
        elif remeasured is not None:
            raise SchemaRefusal(
                f"{subject} re-measures an ink-map page its own map never flagged. That records a "
                "measurement the stage did not need or claim to take. Rebuild the export without "
                "inventing a re-measurement for a mapped page."
            )
        validated.append(row)
    return validated


def _edge_hold_pages_from_validated_rows(rows: list[dict[str, Any]]) -> tuple[int, ...]:
    return tuple(
        sorted(
            row["ordinal"]
            for row in rows
            if row["initial_outcome"] == _UNCLAIMED_EDGE_INK
            and coverage_flag(
                row["remeasured"]["total_ink_pixels"], row["remeasured"]["outside_ink_pixels"]
            )[1]
        )
    )


def edge_hold_pages_from_rows(rows: list[dict[str, Any]]) -> tuple[int, ...]:
    """The held set, recomputed from the recorded counts by the shared gate.

    The held state is not stored: using the measurement predicate prevents a
    row's counts from disagreeing with a separate asserted boolean.
    """
    return _edge_hold_pages_from_validated_rows(
        _validate_ink_map_pages(rows, "an ink-map hold derivation")
    )


def _act_partition_claim(
    projection: ArmariumProjection, categories: list[dict[str, Any]]
) -> dict[str, Any]:
    """The act denominator, under the name of the thing that was actually counted.

    An image-local run counts proposal-seal rows and says so. A clustered run
    counts logical acts, which is a different denominator with a different
    number, and the seal's own row count travels beside it with the membership
    that reconciles the two -- so a reader holding the bundle and the Designator
    seal can see why 2 became 1, instead of finding a bundle that claims to have
    counted seal rows and reports the wrong total for them (GOVERNANCE 10).
    """
    claim = {
        "denominator": _ACT_PARTITION_DENOMINATOR,
        "expected_count": projection.expected_acts,
        "counted": len(projection.acts),
        "reconciles": len(projection.acts) == projection.expected_acts,
        "categories": categories,
        "act_keys": {
            act["act_id"]: act["act_key"]
            for act in sorted(projection.acts, key=lambda item: item["act_id"])
        },
    }
    logical_rows = [act for act in projection.acts if "logical_membership" in act]
    if not logical_rows:
        return claim
    claim["denominator"] = _LOGICAL_ACT_PARTITION_DENOMINATOR
    claim["local_proposal_rows"] = projection.local_proposal_rows
    claim["logical_membership"] = {
        act["act_id"]: {
            "member_local_act_ids": list(act["logical_membership"]["member_local_act_ids"]),
            "member_act_keys": list(act["logical_membership"]["member_act_keys"]),
            "member_source_page_ordinals": list(
                act["logical_membership"]["member_source_page_ordinals"]
            ),
        }
        for act in sorted(logical_rows, key=lambda item: item["act_id"])
    }
    return claim


_LOGICAL_MEMBERSHIP_FIELDS: Final = frozenset(
    {
        "member_local_act_ids",
        "member_act_keys",
        "member_source_page_ordinals",
        "physical_page_components",
    }
)


def _validate_logical_act_conservation(
    projection: ArmariumProjection, act_ids: set[str], act_keys: set[str]
) -> None:
    """Every local proposal row is counted once: under a logical act, or alone.

    Two things a clustered export could otherwise do silently.

    **Double-count (consult §7.15).** A projection carrying a logical act *and*
    its own member local acts as separate rows passes every other check in this
    file -- distinct ids, distinct keys, one terminal category each -- and the
    same ink leaves the pipeline as three delivered acts. The check is
    exact-identity, not heuristic: a member act_id or act_key that also names a
    row of this projection is the duplicate.

    **Vanish (GOVERNANCE 2, invariant 8).** A clustered run's act denominator
    is `logical_expected_count`, which is smaller than the proposal seal's row
    count by construction. Without `local_proposal_rows` beside it, a member act
    that never reached any logical act is invisible -- the arithmetic still
    closes, because the number it closes against already shrank. So a clustered
    projection must declare how many seal rows it is accounting for, and the
    members it retains plus the acts it carries alone must be exactly that many.

    Neither check reads a member as text or identity; the logical row's own
    `act_id`/`act_key` stay derived from `logical_act_id` alone.
    """
    logical_rows = [act for act in projection.acts if "logical_membership" in act]
    if not logical_rows:
        if projection.local_proposal_rows is not None:
            raise SchemaRefusal(
                "an Armarium projection declares a proposal-seal row count but carries no "
                "logical act; the two denominators are one number in an image-local run"
            )
        return
    member_ids_seen: set[str] = set()
    member_keys_seen: set[str] = set()
    for act in logical_rows:
        membership = act["logical_membership"]
        if not isinstance(membership, dict) or set(membership) != _LOGICAL_MEMBERSHIP_FIELDS:
            raise SchemaRefusal("an Armarium projection logical act has no closed membership")
        ids = membership["member_local_act_ids"]
        keys = membership["member_act_keys"]
        ordinals = membership["member_source_page_ordinals"]
        components = membership["physical_page_components"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not _is_line_safe_identity(member_id) for member_id in ids)
            or ids != sorted(set(ids))
            or not isinstance(keys, list)
            or len(keys) != len(ids)
            or any(not _is_line_safe_identity(member_key) for member_key in keys)
            or keys != sorted(set(keys))
            or not isinstance(ordinals, list)
            or not ordinals
            or any(
                not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0
                for ordinal in ordinals
            )
            or ordinals != sorted(set(ordinals))
            or not isinstance(components, list)
            or not components
        ):
            raise SchemaRefusal(
                "an Armarium projection logical act has malformed member ids, keys, source "
                "page ordinals, or physical-page components; the projection is refused "
                "because its local proposal accounting cannot be reconstructed"
            )
        colliding = sorted((set(ids) & act_ids) | (set(keys) & act_keys))
        if colliding:
            raise SchemaRefusal(
                f"an Armarium projection exports member local act(s) {colliding} beside the "
                "logical act they belong to; one logical act leaves as one act row, never "
                "again as its own members"
            )
        repeated_members = sorted((set(ids) & member_ids_seen) | (set(keys) & member_keys_seen))
        if repeated_members:
            raise SchemaRefusal(
                f"an Armarium projection repeats local member id/key(s) {repeated_members} "
                "across logical acts; the projection is refused because a proposal row "
                "belongs to exactly one logical act"
            )
        member_ids_seen.update(ids)
        member_keys_seen.update(keys)
        # Consult §5.2: attribution becomes `logical_act_id -> all source page
        # ordinals`, and it is what keeps "a silent page cannot hide beside a
        # busy page" true once several captures answer for one act. The basis
        # supplies that attribution as a caller's assertion; the member rows are
        # where the pages were actually marked out. A logical act attributed to
        # fewer pages than its own members were cut on would leave the missing
        # page looking silent -- or, worse, looking accounted for by a busy
        # sibling. Superset, not equality: a continuation legitimately reaches a
        # page no member proposal was cut on.
        attributed = (projection.aggregate_basis.get("act_pages") or {}).get(act["act_key"])
        if attributed is None:
            continue
        uncovered = sorted(set(ordinals) - set(attributed))
        if uncovered:
            raise SchemaRefusal(
                f"logical act {act['act_id']} has member acts marked out on page(s) {uncovered} "
                "that its own page attribution does not name; a member capture's page may not "
                "drop out of the run's page coverage check"
            )
    declared = projection.local_proposal_rows
    if not isinstance(declared, int) or isinstance(declared, bool):
        raise SchemaRefusal(
            "an Armarium projection carries a logical act but does not say how many "
            "proposal-seal rows its act denominator stands for"
        )
    accounted = len(member_ids_seen) + (len(projection.acts) - len(logical_rows))
    if accounted != declared:
        raise SchemaRefusal(
            f"an Armarium projection accounts for {accounted} local proposal row(s) against a "
            f"declared {declared}; every seal row is carried under exactly one logical act or "
            "as an act of its own"
        )


def _validate_projection(projection: ArmariumProjection) -> None:
    if not isinstance(projection.fixture_id, str) or not projection.fixture_id:
        raise SchemaRefusal("an Armarium projection has no fixture identifier")
    if not isinstance(projection.scenario, str) or not projection.scenario:
        raise SchemaRefusal("an Armarium projection has no scenario")
    _require_sha256(projection.config_digest, "an Armarium projection sealed configuration digest")
    if not isinstance(projection.expected_acts, int) or isinstance(projection.expected_acts, bool):
        raise SchemaRefusal("an Armarium projection expected-act count is not an integer")
    if len(projection.acts) != projection.expected_acts:
        raise SchemaRefusal(
            "an Armarium projection does not contain one record for every expected act"
        )
    _validate_witness_accounting(
        projection.witness_chairs,
        projection.witness_floor,
        projection.aggregate_basis,
        projection.acts,
    )
    ink_map_rows = _validate_ink_map_pages(list(projection.ink_map_pages), "an Armarium projection")
    edge_hold_pages = _edge_hold_pages_from_validated_rows(ink_map_rows)
    sealed = {
        page["ordinal"]
        for page in projection.pages
        if isinstance(page, dict) and page.get("outcome") == "sealed"
    }
    if {row["ordinal"] for row in ink_map_rows} != sealed:
        raise SchemaRefusal(
            "an Armarium projection's ink-map denominator is not exactly its sealed page census. "
            "At least one sealed page finding would be lost or one unsealed page would be counted. "
            "Rebuild the projection from the reconciled stage inventories."
        )
    for source in projection.source_manifest:
        if not isinstance(source, dict):
            raise SchemaRefusal("an Armarium projection source-manifest row is not an object")
        path, digest = source.get("relative_path"), source.get("sha256")
        if not isinstance(path, str):
            raise SchemaRefusal("an Armarium projection source-manifest row has no path")
        _source_folder_for_declared_path(path)
        _require_sha256(digest, "an Armarium projection source-manifest digest")
        if "ledger_sha256" in source:
            _require_sha256(source["ledger_sha256"], "an Armarium projection ledger digest")
    known_categories = {category.value for category in ArmariumCategory}
    act_ids: set[str] = set()
    act_keys: set[str] = set()
    for act in projection.acts:
        if not isinstance(act, dict):
            raise SchemaRefusal("an Armarium projection act is not an object")
        act_id, act_key, category = act.get("act_id"), act.get("act_key"), act.get("category")
        if not _is_line_safe_identity(act_id) or not _is_line_safe_identity(act_key):
            raise SchemaRefusal("an Armarium projection act lacks a line-safe act identity")
        if act_id in act_ids or act_key in act_keys:
            raise SchemaRefusal("an Armarium projection repeats an act identity")
        act_ids.add(act_id)
        act_keys.add(act_key)
        if category not in known_categories:
            raise SchemaRefusal(f"an Armarium projection uses unknown category {category!r}")
        if CANONICAL_TEXT_FIELD not in act:
            raise SchemaRefusal("an Armarium projection act has no canonical-text field")
        literal = act[CANONICAL_TEXT_FIELD]
        regions = act.get("source_regions", [])
        if not isinstance(regions, list):
            raise SchemaRefusal("an Armarium projection act has malformed source-region provenance")
        if act.get("reason") is not None and not isinstance(act.get("reason"), str):
            raise SchemaRefusal("an Armarium projection act has an untyped reason")
        if category == ArmariumCategory.DELIVERED.value:
            if not isinstance(literal, str):
                raise SchemaRefusal("a delivered act has no literal Archetypus clean text")
            if not isinstance(act.get("provenance"), dict) or not act["provenance"]:
                raise SchemaRefusal("a delivered act has no provenance")
            if not regions:
                raise SchemaRefusal("a delivered act has no source-region provenance")
            # `utf8_round_trip` runs `validate_uncertainty` itself, on exactly
            # these arguments, before it asks its own question.
            utf8_round_trip(act.get("uncertainty"), literal)
            _require_damage_record(
                act.get("text_status"),
                act.get("transcription_annotations"),
                act.get("uncertainty"),
                literal,
                subject="Armarium projection act",
            )
        elif literal is not None:
            raise SchemaRefusal("a non-delivered act may not carry purported clean text")
        elif act.get("uncertainty") is not None:
            # The same rule as the line above, for the layer that anchors to that
            # text: offsets into a text this act does not have are not a reading
            # the export may carry, and no writer would have anywhere to put them.
            raise SchemaRefusal("a non-delivered act may not carry an uncertainty layer")
        elif act.get("text_status") is not None or act.get("transcription_annotations") is not None:
            # And the same rule again for what a record says *about* its text. An
            # act with no Archetypus record has no status and no annotation layer;
            # a projection carrying either would be describing a reading that does
            # not exist.
            raise SchemaRefusal(
                "a non-delivered act may not carry an established-text status or a "
                "transcription annotation layer"
            )
        if category == ArmariumCategory.EXCLUDED_WITH_APPROVAL.value:
            require_approval(ARMARIUM, category, act.get("approval_ref"))
        _reject_act_salvage_namespace(act)
    _validate_logical_act_conservation(projection, act_ids, act_keys)
    # The basis is the copy the run's verdict is computed from, and it was the
    # one copy of the damage record nothing compared to the acts it describes —
    # the repaired defect's own shape, one level up. Delivered acts and the
    # basis must state the damage identically, key for key.
    recorded_basis_status = projection.aggregate_basis.get("act_text_status")
    delivered_status = {
        act["act_key"]: act.get("text_status")
        for act in projection.acts
        if act.get("category") == ArmariumCategory.DELIVERED.value
    }
    if recorded_basis_status != delivered_status:
        raise SchemaRefusal(
            "an Armarium projection's aggregate basis does not carry exactly the delivered "
            "acts' own established-text statuses"
        )
    expected_aggregate = _aggregate_from_basis(
        {act["act_key"]: act["category"] for act in projection.acts},
        projection.pages,
        projection.aggregate_basis,
        edge_hold_pages,
    )
    if canonical_text(projection.aggregate) != canonical_text(expected_aggregate):
        raise SchemaRefusal("an Armarium projection aggregate does not match its measured basis")


def _require_damage_record(
    text_status: Any,
    annotations: Any,
    uncertainty: Any,
    literal: str,
    *,
    subject: str,
) -> None:
    """Recompute a delivered act's text status from the layers carried beside it.

    The status is never merely carried. `established | partial | no_readable_text`
    is the one field that says whether the reading leaving the pipeline is whole,
    and a value read out of a row and believed is an assertion, not a check: a
    package could say `established` over an act whose own gap list records ink the
    Perlector could not read, which is exactly the shape GOVERNANCE 2 refuses. The
    two damage layers travel in every literal format, so every reader of one --
    the projection boundary and each product verifier on a clean machine -- can
    derive the word for itself and refuse the row if it disagrees.

    An empty annotation layer is ordinary and is not an absent one: `[]` says the
    reader marked no damage, and `None` would say this act has no record at all.
    """
    # `isinstance` before membership: package-sourced JSON can put an
    # unhashable value here, and a TypeError out of the membership test would
    # be a crash where the contract owes a named refusal.
    if not isinstance(text_status, str) or text_status not in TEXT_STATUSES:
        raise SchemaRefusal(
            f"a delivered {subject} carries established-text status {text_status!r}, which is "
            f"not one of {sorted(TEXT_STATUSES)}"
        )
    if not isinstance(annotations, list):
        raise SchemaRefusal(f"a delivered {subject} carries no transcription annotation layer")
    # The carried layer is the one exported layer that legitimately holds free
    # text (a reader's alternatives, a witness's quoted variant), so it gets the
    # producer's own validator on the clean machine too — closed kinds, closed
    # field sets, offsets inside this row's own literal. `witnesses=None`: the
    # roster lives in the retained run, so attribution and quotation were checked
    # where the layer was sealed and cannot be re-checked from the package alone.
    try:
        validate_annotations(annotations, literal, None, f"{subject} transcription annotation")
    except SchemaRefusal as error:
        raise SchemaRefusal(
            f"a delivered {subject}'s transcription annotation layer is not the closed "
            f"layer this pipeline seals: {error}"
        ) from error
    try:
        expected = derive_record_text_status(literal, annotations, uncertainty)
    except SchemaRefusal as error:
        raise SchemaRefusal(
            f"a delivered {subject}'s damage layers cannot be read for the status of its own text"
        ) from error
    if text_status != expected:
        raise SchemaRefusal(
            f"a delivered {subject} claims established-text status {text_status!r} over damage "
            f"layers that say {expected!r}; a damaged act may not be projected as a whole one"
        )


def _validate_witness_accounting(
    witness_chairs: Any,
    witness_floor: Any,
    aggregate_basis: Any,
    acts: tuple[dict[str, Any], ...] | None = None,
) -> None:
    """Keep the exported roster, coverage counts, and per-act witnesses one fact."""
    if (
        not isinstance(witness_chairs, (list, tuple))
        or any(not isinstance(chair, str) or not chair for chair in witness_chairs)
        or len(set(witness_chairs)) != len(witness_chairs)
    ):
        raise SchemaRefusal("Armarium witness chairs are not a unique named roster")
    if (
        not isinstance(witness_floor, int)
        or isinstance(witness_floor, bool)
        or witness_floor < 0
        or witness_floor > len(witness_chairs)
    ):
        raise SchemaRefusal("Armarium witness floor does not fit its named roster")
    coverage = (
        aggregate_basis.get("coverage_records") if isinstance(aggregate_basis, dict) else None
    )
    if not isinstance(coverage, dict):
        raise SchemaRefusal("Armarium witness accounting has no coverage records")
    for act_key, record in coverage.items():
        if (
            not isinstance(record, dict)
            or record.get("configured") != len(witness_chairs)
            or record.get("floor") != witness_floor
        ):
            raise SchemaRefusal(
                f"Armarium witness coverage for {act_key!r} disagrees with the exported roster"
            )
    if acts is None:
        return
    expected = set(witness_chairs)
    for act in acts:
        if act.get("category") != ArmariumCategory.DELIVERED.value:
            continue
        witnesses = act.get("witnesses")
        if not isinstance(witnesses, list):
            raise SchemaRefusal("a delivered act has no witness provenance list")
        chairs = [item.get("chair") for item in witnesses if isinstance(item, dict)]
        if (
            len(chairs) != len(witnesses)
            or set(chairs) != expected
            or len(set(chairs)) != len(chairs)
        ):
            raise SchemaRefusal("a delivered act's witness provenance disagrees with the roster")


def _aggregate_from_basis(
    categories: dict[str, str],
    pages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    basis: Any,
    edge_hold_pages: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Recompute an Armarium aggregate from its retained, non-text inputs."""
    if not isinstance(basis, dict) or set(basis) != {
        "coverage_records",
        "unaddressed_chairs",
        "act_pages",
        "act_text_status",
    }:
        raise SchemaRefusal("an Armarium aggregate has no recognized accounting basis")
    coverage, chairs, act_pages, act_text_status = (
        basis.get("coverage_records"),
        basis.get("unaddressed_chairs"),
        basis.get("act_pages"),
        basis.get("act_text_status"),
    )
    if (
        not isinstance(coverage, dict)
        or not isinstance(chairs, list)
        or not all(isinstance(chair, str) and chair for chair in chairs)
        or not isinstance(act_pages, dict)
        or not isinstance(act_text_status, dict)
    ):
        raise SchemaRefusal("an Armarium aggregate basis is malformed")
    normalized_categories: dict[str, ArmariumCategory] = {}
    for act_key, category in categories.items():
        if not isinstance(act_key, str) or not act_key:
            raise SchemaRefusal("an Armarium aggregate basis has no act key")
        try:
            normalized_categories[act_key] = ArmariumCategory(category)
        except ValueError as error:
            raise SchemaRefusal("an Armarium aggregate basis has an unknown category") from error
    try:
        return run_aggregate(
            normalized_categories,
            coverage,
            _pages_by_ordinal(pages),
            unaddressed_chairs=chairs,
            act_pages=act_pages,
            act_text_status=act_text_status,
            edge_hold_pages=edge_hold_pages,
        )
    # `KeyError`/`TypeError` as well as the refusals: `run_aggregate` reaches inside a
    # coverage record for `by_class['completed']` and `floor`, which nothing above
    # proves are there, and this basis was read back out of a package somebody else
    # assembled. Every hole in it has the same one answer. The original is chained, so
    # a genuine defect inside `run_aggregate` stays visible in the traceback.
    except (ContractError, KeyError, TypeError) as error:
        raise SchemaRefusal("an Armarium aggregate basis cannot be reconciled") from error


def _validate_salvage_items(items: tuple[dict[str, Any], ...]) -> None:
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise SchemaRefusal("a salvage-tier item is not an object")
        _reject_salvage_act_namespace(item, subject="item")
        salvage_id = item.get("salvage_id")
        if not _is_safe_path_segment(salvage_id) or salvage_id in seen:
            raise SchemaRefusal("a salvage-tier item has no unique, safe salvage identity")
        if not isinstance(item.get("content"), str):
            raise SchemaRefusal("a salvage-tier item has no separately named content")
        regions = item.get("source_regions")
        if not isinstance(regions, list) or not regions:
            raise SchemaRefusal("a salvage-tier item has no source-region provenance")
        if not isinstance(item.get("provenance"), dict) or not item["provenance"]:
            raise SchemaRefusal("a salvage-tier item has no collection provenance")
        for region in regions:
            _validate_salvage_region(region)
        seen.add(salvage_id)


def _reject_act_salvage_namespace(act: dict[str, Any]) -> None:
    """The salvage firewall in the other direction: no salvage record becomes an act.

    `_reject_salvage_act_namespace` stops a salvage item reaching into the acts
    namespace. This stops the reverse -- a salvage-shaped record arriving where an act
    is expected, and its harvested scrap becoming established text through a writer
    that only ever asked whether a `canonical_clean_text` field was present. Spec 11
    test 4 is that "no code path from this stage writes act text under any
    circumstance": promotion is a pipeline re-entry Tyrel approves, never an export
    act, so a record carrying any of these discriminants is refused by name rather
    than left to fail on a missing key somewhere downstream.
    """
    reached = sorted(set(act) & _SALVAGE_DISCRIMINANT_FIELDS)
    if reached:
        raise SchemaRefusal(
            f"an Armarium projection act carries salvage-tier field(s) {reached}; "
            "salvage is promoted by re-entering the pipeline, never by export"
        )


def _reject_salvage_act_namespace(value: Any, *, subject: str) -> None:
    """The salvage firewall applies to nested provenance as well as record headers."""
    try:
        _reject_salvage_act_namespace_walk(value, subject=subject)
    except RecursionError as error:
        # Both call sites (`_validate_salvage_items`, `_validate_salvage_region`)
        # run this screen on harvested, caller-supplied content before any of
        # its own shape checks, so an unvalidated salvage item can nest past
        # Python's recursion limit. A record this machine cannot walk is
        # refused, never crashed on -- the same boundary `self_hash` already
        # holds for the digests this package seals.
        raise SchemaRefusal(
            f"a salvage-tier {subject} nests too deeply for this machine to walk, so its "
            "acts-namespace screen was never computable"
        ) from error


def _reject_salvage_act_namespace_walk(value: Any, *, subject: str) -> None:
    if isinstance(value, dict):
        forbidden = sorted(set(value) & _SALVAGE_RESERVED_FIELDS)
        if forbidden:
            raise SchemaRefusal(
                f"a salvage-tier {subject} reaches into the acts namespace through "
                f"reserved field(s) {forbidden}"
            )
        for item in value.values():
            _reject_salvage_act_namespace_walk(item, subject=subject)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_salvage_act_namespace_walk(item, subject=subject)


def _validate_cited_region(region: object, *, subject: str) -> None:
    """Validate a crop citation before it is attached to any export namespace."""
    if not isinstance(region, dict):
        raise SchemaRefusal(f"a {subject} source region is not an object")
    if not _is_safe_path_segment(region.get("region_id")):
        raise SchemaRefusal(f"a {subject} source region has no safe identity")
    image_path, image_sha256 = region.get("image_path"), region.get("image_sha256")
    if not isinstance(image_path, str):
        raise SchemaRefusal(f"a {subject} source region has no crop path")
    _validate_run_relative_path(image_path)
    _require_sha256(image_sha256, f"a {subject} source region crop digest")
    declared_path, declared_sha256 = region.get("declared_path"), region.get("declared_sha256")
    if not isinstance(declared_path, str):
        raise SchemaRefusal(f"a {subject} source region has no declared source path")
    _source_folder_for_declared_path(declared_path)
    _require_sha256(declared_sha256, f"a {subject} source region declared source digest")
    if "ledger_sha256" in region:
        _require_sha256(region["ledger_sha256"], f"a {subject} source region ledger digest")
    ordinal = region.get("source_page_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal(f"a {subject} source region has no source-page ordinal")
    if not isinstance(region.get("source_page_id"), str) or not region["source_page_id"]:
        raise SchemaRefusal(f"a {subject} source region has no source-page identity")
    transform = region.get("transform")
    if not isinstance(transform, dict) or transform.get("operation") != "crop":
        raise SchemaRefusal(f"a {subject} source region has no crop transform")


def _validate_salvage_region(region: object) -> None:
    """Keep tier-only material cited to ink without turning it into an act."""
    if isinstance(region, dict):
        _reject_salvage_act_namespace(region, subject="source region")
    _validate_cited_region(region, subject="salvage-tier")


def _pages_by_ordinal(
    pages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Index the measured page census before accepting a crop's claimed parent."""
    indexed: dict[int, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise SchemaRefusal("an export page census row is not an object")
        ordinal = page.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal in indexed:
            raise SchemaRefusal("an export page census has no unique integer ordinal")
        indexed[ordinal] = page
    return indexed


def _verify_region_page_binding(
    region: dict[str, Any], pages: dict[int, dict[str, Any]], *, subject: str
) -> None:
    """A crop may cite only the exact sealed page it says it came from."""
    page = pages.get(region["source_page_ordinal"])
    if page is None or page.get("outcome") != "sealed":
        raise SchemaRefusal(f"a {subject} source region names no sealed source page")
    if (
        region.get("source_page_id") != page.get("page_id")
        or region.get("declared_path") != page.get("declared_path")
        or region.get("declared_sha256") != page.get("declared_sha256")
    ):
        raise SchemaRefusal(f"a {subject} source region disagrees with its cited source page")


def _validate_projection_region_bindings(projection: ArmariumProjection) -> None:
    """Check both namespaces against one page census before packaging either."""
    pages = _pages_by_ordinal(projection.pages)
    for act in projection.acts:
        for region in act.get("source_regions", []):
            _validate_cited_region(region, subject="exported act")
            _verify_region_page_binding(region, pages, subject="exported act")
    for item in projection.salvage_items or ():
        for region in item["source_regions"]:
            _validate_salvage_region(region)
            _verify_region_page_binding(region, pages, subject="salvage-tier")


def _source_rows(
    pages: tuple[dict[str, Any], ...],
    embed_pixels: bool,
    read_bytes: Callable[[str], bytes],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    rows: list[dict[str, Any]] = []
    embedded: dict[str, bytes] = {}
    # `_validate_projection_region_bindings` has already put every row through
    # `_pages_by_ordinal`, so the ordinal is read here rather than re-proved.
    for page in sorted(pages, key=lambda item: item["ordinal"]):
        ordinal = page["ordinal"]
        declared_path, declared_sha256 = page.get("declared_path"), page.get("declared_sha256")
        if not isinstance(declared_path, str) or not isinstance(declared_sha256, str):
            raise SchemaRefusal("an export page census lacks its declared source citation")
        _source_folder_for_declared_path(declared_path)
        _require_sha256(declared_sha256, "an export page declared source digest")
        row = {
            "ordinal": ordinal,
            "outcome": page.get("outcome"),
            "reason": page.get("reason", ""),
            "declared_path": declared_path,
            "declared_sha256": declared_sha256,
            "page_id": page.get("page_id"),
        }
        for field in ("declared_bytes", "ledger_sha256", "container_page_index"):
            if field in page:
                row[field] = page[field]
        if "ledger_sha256" in row:
            _require_sha256(row["ledger_sha256"], "an export page ledger digest")
        image_path, image_sha256 = page.get("image_path"), page.get("image_sha256")
        if page.get("outcome") == "sealed":
            if not isinstance(image_path, str) or not isinstance(image_sha256, str):
                raise SchemaRefusal("a sealed export page lacks its verified image reference")
            _require_sha256(image_sha256, "a sealed export page image digest")
            if embed_pixels:
                pixels = read_bytes(image_path)
                if digest_bytes(pixels) != image_sha256:
                    raise SchemaRefusal("a sealed page changed while its export was being built")
                member = f"pixels/pages/{ordinal}.img"
                embedded[member] = pixels
                row["page_image"] = {
                    "availability": _EMBEDDED,
                    "member_path": member,
                    "sha256": image_sha256,
                }
            else:
                _validate_run_relative_path(image_path)
                row["page_image"] = {
                    "availability": _SOURCE_ACCESS_REQUIRED,
                    "run_relative_path": image_path,
                    "sha256": image_sha256,
                }
        rows.append(row)
    return rows, embedded


def _text_bundle_members(
    acts: tuple[dict[str, Any]], source_rows: list[dict[str, Any]]
) -> dict[str, bytes]:
    """Write one readable file for every cited source folder.

    A folder with only holds or refusals still gets a file.  It contains no
    invented empty reading, but it does not disappear from a format whose shape
    is promised by the manifest either.  The wrapper names below deliberately
    distinguish the source root from a real ``_source_root`` directory.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    folders: set[str] = set()
    for page in source_rows:
        declared_path = page.get("declared_path")
        if not isinstance(declared_path, str):
            raise SchemaRefusal("a text-bundle source row has no declared path")
        folders.add(_source_folder_for_declared_path(declared_path))
    for act in acts:
        if act["category"] != ArmariumCategory.DELIVERED.value:
            continue
        # A delivered act reached here with at least one region, each already carrying a
        # validated `declared_path`. A logical act may cite captures in several source
        # folders; copy its one unchanged text into every cited folder rather than allowing
        # region order to choose one capture's folder as its representative.
        source_folders = sorted(
            {
                _source_folder_for_declared_path(region["declared_path"])
                for region in act["source_regions"]
            }
        )
        for folder in source_folders:
            folders.add(folder)
            grouped[folder].append(act)
    members: dict[str, bytes] = {}
    for folder in sorted(folders):
        records = grouped[folder]
        lines = [f"# Armarium text bundle — source folder: {folder or '.'}", ""]
        for act in sorted(records, key=lambda item: item["act_key"]):
            regions = act["source_regions"]
            lines.extend([f"## {act['act_key']} ({act['act_id']})", f"act-id: {act['act_id']}"])
            for region in regions:
                lines.extend(
                    [
                        f"source-page: {region['declared_path']}",
                        f"source-sha256: {region['declared_sha256']}",
                    ]
                )
            lines.extend(
                [
                    f"canonical_text_sha256: {canonical_text_sha256(act[CANONICAL_TEXT_FIELD])}",
                    "canonical_clean_text:",
                    json.dumps(act[CANONICAL_TEXT_FIELD], ensure_ascii=False),
                    "uncertainty:",
                    json.dumps(act["uncertainty"], ensure_ascii=False, sort_keys=True),
                    # What the record says about its own text, and the older
                    # annotation layer the status is partly derived from. The
                    # readable bundle is the format a person actually reads, so a
                    # damaged act saying so in words belongs here first.
                    f"text_status: {act['text_status']}",
                    "transcription_annotations:",
                    json.dumps(
                        act["transcription_annotations"], ensure_ascii=False, sort_keys=True
                    ),
                    # Beside the canonical field, never instead of it: the clean
                    # verifier strips this back and requires the line above exactly.
                    f"display_convention: {DISPLAY_CONVENTION}",
                    "display:",
                    json.dumps(render_display(act[CANONICAL_TEXT_FIELD]), ensure_ascii=False),
                    "",
                ]
            )
        members[_text_member_path(folder)] = "\n".join(lines).encode("utf-8")
    return members


def _acts_with_source_references(
    acts: tuple[dict[str, Any], ...],
    embed_pixels: bool,
    read_bytes: Callable[[str], bytes],
) -> tuple[tuple[dict[str, Any], ...], dict[str, bytes]]:
    """Add explicit crop availability records without changing any literal text."""
    projected: list[dict[str, Any]] = []
    embedded: dict[str, bytes] = {}
    for act in acts:
        record = dict(act)
        regions: list[dict[str, Any]] = []
        for region in act.get("source_regions", []):
            # Every field the rest of this loop reads is checked here and nowhere else.
            _validate_cited_region(region, subject="exported act")
            copied = dict(region)
            image_path, image_sha256, region_id = (
                copied["image_path"],
                copied["image_sha256"],
                copied["region_id"],
            )
            if embed_pixels:
                pixels = read_bytes(image_path)
                if digest_bytes(pixels) != image_sha256:
                    raise SchemaRefusal("a source crop changed while its export was being built")
                member = f"pixels/crops/{region_id}.img"
                previous = embedded.get(member)
                if previous is not None and previous != pixels:
                    raise SchemaRefusal("two source crops claimed one package member")
                embedded[member] = pixels
                copied["crop_image"] = {
                    "availability": _EMBEDDED,
                    "member_path": member,
                    "sha256": image_sha256,
                }
            else:
                copied["crop_image"] = {
                    "availability": _SOURCE_ACCESS_REQUIRED,
                    "run_relative_path": image_path,
                    "sha256": image_sha256,
                }
            regions.append(copied)
        record["source_regions"] = regions
        projected.append(record)
    return tuple(projected), embedded


def _salvage_with_source_references(
    items: tuple[dict[str, Any], ...] | None,
    embed_pixels: bool,
    read_bytes: Callable[[str], bytes],
) -> tuple[tuple[dict[str, Any], ...] | None, dict[str, bytes]]:
    """Project cited salvage regions without ever constructing an act record."""
    if items is None:
        return None, {}
    projected: list[dict[str, Any]] = []
    embedded: dict[str, bytes] = {}
    for item in items:
        salvage_id = item["salvage_id"]
        copied_item = dict(item)
        regions: list[dict[str, Any]] = []
        seen_regions: set[str] = set()
        for region in item["source_regions"]:
            _validate_salvage_region(region)
            copied = dict(region)
            region_id = copied["region_id"]
            if region_id in seen_regions:
                raise SchemaRefusal("a salvage-tier item repeats a source-region identity")
            seen_regions.add(region_id)
            image_path, image_sha256 = copied["image_path"], copied["image_sha256"]
            if embed_pixels:
                pixels = read_bytes(image_path)
                if digest_bytes(pixels) != image_sha256:
                    raise SchemaRefusal("a salvage-tier source crop changed while export was built")
                member = f"pixels/salvage/{salvage_id}/{region_id}.img"
                previous = embedded.get(member)
                if previous is not None and previous != pixels:
                    raise SchemaRefusal("two salvage source crops claim one package member")
                embedded[member] = pixels
                copied["crop_image"] = {
                    "availability": _EMBEDDED,
                    "member_path": member,
                    "sha256": image_sha256,
                }
            else:
                copied["crop_image"] = {
                    "availability": _SOURCE_ACCESS_REQUIRED,
                    "run_relative_path": image_path,
                    "sha256": image_sha256,
                }
            regions.append(copied)
        copied_item["source_regions"] = regions
        projected.append(copied_item)
    return tuple(projected), embedded


_UNSAFE_PATH_CHARACTERS: Final = frozenset({"\\", "\x00"})


def _is_line_safe_identity(value: object) -> bool:
    """Whether an identity may be spliced unescaped into a line-oriented format.

    `act_id`/`act_key` are written raw into the text bundle's ``## key (id)`` header
    and ``act-id: id`` line, which the clean verifier then parses line by line. The
    canonical text beside them is JSON-escaped onto one line; these are not, so this
    is the boundary's own answer rather than relying on the downstream cross-checks
    that happen to catch a forged line today.
    """
    if not isinstance(value, str) or not value:
        return False
    return not any(ord(character) < 0x20 or character == "\x7f" for character in value)


def _is_safe_path_segment(value: object) -> bool:
    """Whether an identity may be spliced into a member path as one whole component.

    A region identity and a salvage identity each become exactly one path component of
    an embedded pixel member -- ``pixels/crops/<region_id>.img``,
    ``pixels/salvage/<salvage_id>/<region_id>.img``. Same question
    ``_reject_unsafe_relative_path`` answers for a whole path, narrowed to one
    component, so that the two identities cannot answer it differently.
    """
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        return False
    return not any(character in value for character in _UNSAFE_PATH_CHARACTERS)


def _reject_unsafe_relative_path(value: object, *, subject: str) -> PurePosixPath:
    """The one 'is this a safe POSIX-relative path' check every path-shaped field shares.

    The raw-character rejection is the part that looks removable and is not.
    ``PurePosixPath`` splits only on ``/``, so ``PurePosixPath("a/..\\..\\evil").parts``
    is one opaque component rather than three and ``PurePosixPath("C:\\evil")`` is not
    absolute: a backslash traversal passes an ``is_absolute()``/``".." in parts`` check
    completely untouched. POSIX tooling shrugs at that, but a bundle exists to be opened
    by whatever tool its recipient has, including Windows-native tooling that does treat
    a backslash in a ZIP entry name as a separator.
    """
    if not isinstance(value, str) or not value:
        raise SchemaRefusal(f"{subject} is unsafe")
    if any(character in value for character in _UNSAFE_PATH_CHARACTERS):
        raise SchemaRefusal(f"{subject} is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SchemaRefusal(f"{subject} is unsafe")
    return path


def _source_folder_for_declared_path(declared_path: str) -> str:
    """Return a safe logical source folder, preserving root as an empty key."""
    path = _reject_unsafe_relative_path(declared_path, subject="a source folder")
    parent = path.parent.as_posix()
    return "" if parent == "." else parent


def _require_sha256(value: object, label: str) -> str:
    if not is_sha256(value):
        raise SchemaRefusal(f"{label} is not a lowercase sha256")
    return value


def _retained_run_reference(reference: dict[str, Any]) -> dict[str, str]:
    """Label a reference that only the retained run tree can resolve.

    The product is not the separate evidence package: it carries provenance and
    digest citations, but it never silently pretends to include the Testimonium,
    receipt, or intermediate artifact those citations name.
    """
    path, digest = reference.get("relative_path"), reference.get("sha256")
    if not isinstance(path, str):
        raise SchemaRefusal("a retained-run reference has no relative path")
    _validate_run_relative_path(path)
    _require_sha256(digest, "a retained-run reference digest")
    return {
        "availability": _RUN_ACCESS_REQUIRED,
        "run_relative_path": path,
        "sha256": digest,
    }


def _mark_retained_references(value: Any) -> Any:
    """Recursively make opaque run-tree evidence honest in a product projection."""
    if isinstance(value, dict):
        if "relative_path" in value:
            # Artifact references sometimes carry useful non-text metadata beside
            # their path and digest.  Preserve it, but never let an added field
            # make the raw retained-run reference evade its availability label.
            marked = {
                key: _mark_retained_references(item)
                for key, item in value.items()
                if key not in {"relative_path", "sha256"}
            }
            marked.update(_retained_run_reference(value))
            return marked
        return {key: _mark_retained_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mark_retained_references(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_mark_retained_references(item) for item in value)
    return value


def _verify_retained_references(value: Any) -> None:
    """Reject an opaque run-tree citation unless its availability is explicit."""
    if isinstance(value, dict):
        if "relative_path" in value:
            raise SchemaRefusal("a product reference lacks its retained-run availability")
        availability = value.get("availability")
        if "run_relative_path" in value and availability not in {
            _RUN_ACCESS_REQUIRED,
            _SOURCE_ACCESS_REQUIRED,
        }:
            raise SchemaRefusal("a run-tree reference has no honest availability status")
        if availability == _RUN_ACCESS_REQUIRED:
            path, digest = value.get("run_relative_path"), value.get("sha256")
            if not isinstance(path, str):
                raise SchemaRefusal("a retained-run reference has no path")
            _validate_run_relative_path(path)
            _require_sha256(digest, "a retained-run reference digest")
        for item in value.values():
            _verify_retained_references(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _verify_retained_references(item)


def _verify_evidence_refs(evidence_refs: Any, *, subject: str) -> None:
    """Require each act to retain its Recensor review and only real citations.

    The generic retained-reference walk permits non-reference dictionaries. This
    field is narrower: every entry must name a retained-run path, and production
    guarantees at least the review reference even when no reading was established.
    """
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise SchemaRefusal(
            f"{subject} has no evidence citations; evidence_refs must retain at least the "
            "Recensor review reference, and an empty list is refused rather than treated "
            "as complete provenance"
        )
    for item in evidence_refs:
        if (
            not isinstance(item, dict)
            or item.get("availability") != _RUN_ACCESS_REQUIRED
            or not isinstance(item.get("run_relative_path"), str)
        ):
            raise SchemaRefusal(f"{subject} evidence_refs entry cites nothing")


def _text_member_path(folder: str) -> str:
    """Map a logical source folder injectively into a product member name."""
    if not folder:
        return "text/_source_root/readings.txt"
    _reject_unsafe_relative_path(folder, subject="a text-bundle source folder")
    return f"text/_source_folder/{folder}/readings.txt"


def _acts_database_bytes(acts: tuple[dict[str, Any], ...]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="armarium-sqlite-") as directory:
        path = f"{directory}/acts.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            # 2, with the schema id: the row shape changed (damage-record columns
            # in, the bare annotations pair renamed apart), and a version the
            # id moved without is the CR W15 miss wearing a different hat.
            connection.execute(f"PRAGMA user_version={_SQLITE_USER_VERSION}")
            connection.executescript(_ACTS_DATABASE_DDL)
            metadata = {
                "canonical_text_encoding": CANONICAL_TEXT_ENCODING,
                "canonical_text_field": CANONICAL_TEXT_FIELD,
                "normalizer_revision": TEXTNORM_REVISION,
                # v2 covers two accumulated shape changes under what was one id:
                # R8's `annotations_json` → `uncertainty_json` rename (CR W15, a
                # real versioning miss) and this change's damage-record columns
                # (text_status, transcription_annotations_json, semantic_* pair).
                "schema": _SQLITE_SCHEMA,
                "unidata_version": unicodedata.unidata_version,
            }
            connection.executemany(
                "INSERT INTO export_metadata(key, value) VALUES (?, ?)",
                sorted(metadata.items()),
            )
            for act in sorted(acts, key=lambda item: item["act_key"]):
                literal = act[CANONICAL_TEXT_FIELD]
                text_hash = canonical_text_sha256(literal) if literal is not None else None
                connection.execute(
                    """
                    INSERT INTO acts(
                        act_id, act_key, category, canonical_clean_text,
                        canonical_text_sha256, provenance_json, source_regions_json,
                        uncertainty_json, uncertainty_status, text_status,
                        transcription_annotations_json, semantic_annotations_json,
                        semantic_annotation_status, evidence_json, approval_ref, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        act["act_id"],
                        act["act_key"],
                        act["category"],
                        literal,
                        text_hash,
                        canonical_text(act["provenance"]) if literal is not None else None,
                        canonical_text(act["source_regions"]) if literal is not None else None,
                        canonical_text(act["uncertainty"]) if literal is not None else None,
                        _UNCERTAINTY_AVAILABLE
                        if literal is not None
                        else _UNCERTAINTY_NOT_APPLICABLE,
                        act["text_status"] if literal is not None else None,
                        canonical_text(act["transcription_annotations"])
                        if literal is not None
                        else None,
                        "[]",
                        _SEMANTIC_ANNOTATION_NOT_PRODUCED,
                        canonical_text(_act_evidence(act)),
                        act.get("approval_ref"),
                        _export_reason(act),
                    ),
                )
                if literal is not None:
                    derived = search_fold(literal)
                    cursor = connection.execute(
                        """
                        INSERT INTO act_search(
                            act_id, derived_search_text, derived_text_sha256,
                            derived_from_canonical_sha256, normalizer_revision, derived_kind
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            act["act_id"],
                            derived,
                            canonical_text_sha256(derived),
                            text_hash,
                            TEXTNORM_REVISION,
                            "search-fold",
                        ),
                    )
                    connection.execute(
                        "INSERT INTO acts_fts(rowid, derived_search_text) VALUES (?, ?)",
                        (cursor.lastrowid, derived),
                    )
            connection.commit()
            # SQLite refuses VACUUM while the inserts are still in its implicit
            # transaction.  Closing that transaction first also makes the page
            # layout a deterministic post-insert state before it becomes package
            # bytes.
            connection.execute("VACUUM")
            connection.commit()
        except sqlite3.DatabaseError as error:
            raise SchemaRefusal(
                "SQLite FTS5 could not build the requested acts database"
            ) from error
        finally:
            connection.close()
        return Path(path).read_bytes()


def _act_json_records(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for act in sorted(acts, key=lambda item: item["act_key"]):
        literal = act[CANONICAL_TEXT_FIELD]
        records.append(
            {
                "schema": ACT_RECORD_SCHEMA,
                "act_id": act["act_id"],
                "act_key": act["act_key"],
                "category": act["category"],
                CANONICAL_TEXT_FIELD: literal,
                "canonical_text_sha256": canonical_text_sha256(literal)
                if literal is not None
                else None,
                "provenance": act.get("provenance") if literal is not None else None,
                "source_regions": act.get("source_regions", []) if literal is not None else [],
                "uncertainty": act.get("uncertainty") if literal is not None else None,
                "uncertainty_status": _UNCERTAINTY_AVAILABLE
                if literal is not None
                else _UNCERTAINTY_NOT_APPLICABLE,
                "text_status": act.get("text_status") if literal is not None else None,
                "transcription_annotations": act.get("transcription_annotations")
                if literal is not None
                else None,
                # The *other* annotation layer, named apart from the one above so
                # that "not produced" can never again be read as a statement about
                # the transcription marks an Archetypus record really did seal.
                "semantic_annotations": [],
                "semantic_annotation_status": _SEMANTIC_ANNOTATION_NOT_PRODUCED,
                "witnesses": act.get("witnesses", []),
                "perlectio_ref": act.get("perlectio_ref"),
                "recensor_ref": act.get("recensor_ref"),
                "dissent_ref": act.get("dissent_ref"),
                "approval_ref": act.get("approval_ref"),
                "reason": _export_reason(act),
                "evidence_refs": act.get("evidence_refs", []),
            }
        )
    return records


def _act_evidence(act: dict[str, Any]) -> dict[str, Any]:
    """Non-text lineage that travels with an act without becoming evidence copy."""
    return {
        "witnesses": act.get("witnesses", []),
        "perlectio_ref": act.get("perlectio_ref"),
        "recensor_ref": act.get("recensor_ref"),
        "dissent_ref": act.get("dissent_ref"),
        "evidence_refs": act.get("evidence_refs", []),
    }


def _export_reason(act: dict[str, Any]) -> str | None:
    """Make a held/refused review reason explicit without inventing one for other outcomes.

    The fallback names the gap itself ("upstream recorded no reason") rather than
    describing the outcome ("no usable reading was exported"): the two are
    distinguishable only by this sentence, and a reader should be able to tell an
    upstream stage that recorded nothing meaningful from one that never ran.
    """
    if act["category"] in {
        ArmariumCategory.HELD_FOR_REVIEW.value,
        ArmariumCategory.REFUSED_WITH_REASON.value,
    }:
        return act.get("reason") or "upstream recorded no reason"
    return act.get("reason")


def _review_records(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    review_categories = {
        ArmariumCategory.HELD_FOR_REVIEW.value,
        ArmariumCategory.REFUSED_WITH_REASON.value,
    }
    return [
        {
            "schema": "armarium-review-item.v1",
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "category": act["category"],
            "reason": _export_reason(act),
            "evidence_refs": act.get("evidence_refs", []),
        }
        for act in sorted(acts, key=lambda item: item["act_key"])
        if act["category"] in review_categories
    ]


def _salvage_records(items: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {
            "schema": SALVAGE_RECORD_SCHEMA,
            "salvage_id": item["salvage_id"],
            "content": item["content"],
            "source_regions": item["source_regions"],
            "provenance": item.get("provenance"),
            "promotion": "requires-recorded-approval-and-pipeline-re-entry",
        }
        for item in sorted(items, key=lambda item: item["salvage_id"])
    ]


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(record) + b"\n" for record in records)


def _package_lines(path, subject: str) -> list[str]:
    r"""Split one package member on exactly the separator its writer used.

    Not ``str.splitlines``, which also breaks on U+0085, U+2028 and U+2029 -- and this
    project serializes with ``ensure_ascii=False`` on purpose (``canonical.py``: "the
    stored bytes should be the text itself"), so ``json.dumps`` emits those three raw
    inside a JSON string instead of escaping them. An established reading carrying one
    cut its own record in half in every line-oriented member. Decoding raw bytes is
    the same principle: no universal-newline translation, because the writers join
    on ``\n`` and nothing else. (Not ``read_text(newline="")``: that keyword reached
    ``pathlib`` in Python 3.13, and CI runs 3.12.)
    """
    try:
        return path.read_bytes().decode("utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"the {subject} cannot be read") from error


def _text_bundle_records(
    root, source_pages: list[dict[str, Any]] | None = None
) -> dict[
    str,
    tuple[str, str, tuple[tuple[str, str], ...], dict[str, Any], str, list[Any], str],
]:
    """Parse literal records and their page/hash citations from the readable bundle."""
    if source_pages is None:
        source_pages = _load_sources(root)["pages"]
    known_pages: set[tuple[str, str]] = set()
    source_folders: set[str] = set()
    for page in source_pages:
        if not isinstance(page, dict):
            raise SchemaRefusal("a text-bundle source citation has no page object")
        path, digest = page.get("declared_path"), page.get("declared_sha256")
        if not isinstance(path, str):
            raise SchemaRefusal("a text-bundle source citation has no declared path")
        _require_sha256(digest, "a text-bundle source citation digest")
        known_pages.add((path, digest))
        source_folders.add(_source_folder_for_declared_path(path))

    records: dict[
        str,
        tuple[str, str, tuple[tuple[str, str], ...], dict[str, Any], str, list, str],
    ] = {}
    record_locations: set[tuple[str, str]] = set()
    # The source graph authenticates the complete folder census, and the exact-member
    # check has already proved these are the package's only text files. Enumerating
    # those derived names is both directions of the promise; an `rglob` walk could
    # silently omit a linked or unreadable subtree and has no additional authority.
    for folder in sorted(source_folders):
        path = root / _text_member_path(folder)
        lines = _package_lines(path, "text bundle")
        current_id: str | None = None
        heading_key: str | None = None
        pending: tuple[str, str, tuple[tuple[str, str], ...]] | None = None
        pending_uncertainty: dict[str, Any] | None = None
        pending_text_status: str | None = None
        pending_annotations: list[Any] | None = None
        citations: list[tuple[str, str]] = []
        for index, line in enumerate(lines):
            if line.startswith("act-id: "):
                if current_id is not None:
                    raise SchemaRefusal("a text-bundle section has no completed literal record")
                current_id = line.removeprefix("act-id: ")
                if not current_id:
                    raise SchemaRefusal("a text-bundle section has an empty act identity")
                heading = lines[index - 1] if index else ""
                suffix = f" ({current_id})"
                if not heading.startswith("## ") or not heading.endswith(suffix):
                    raise SchemaRefusal(
                        "a text-bundle act-id is not authenticated by its human heading"
                    )
                heading_key = heading.removeprefix("## ").removesuffix(suffix)
                if not heading_key:
                    raise SchemaRefusal("a text-bundle human heading has no act key")
                citations = []
                pending = None
                pending_uncertainty = None
                pending_text_status = None
                pending_annotations = None
            elif line.startswith("source-page: "):
                if current_id is None or index + 1 >= len(lines):
                    raise SchemaRefusal(
                        "a text-bundle source citation has no act identity or digest"
                    )
                declared_path = line.removeprefix("source-page: ")
                digest_line = lines[index + 1]
                if not declared_path or not digest_line.startswith("source-sha256: "):
                    raise SchemaRefusal("a text-bundle source citation is malformed")
                digest = digest_line.removeprefix("source-sha256: ")
                _require_sha256(digest, "a text-bundle source citation digest")
                citation = (declared_path, digest)
                if known_pages is not None and citation not in known_pages:
                    raise SchemaRefusal("a text-bundle source citation names no packaged page")
                citations.append(citation)
            elif line.startswith("source-sha256: "):
                if index == 0 or not lines[index - 1].startswith("source-page: "):
                    raise SchemaRefusal("a text-bundle source digest has no page citation")
            elif line == "canonical_clean_text:":
                if current_id is None or index + 1 >= len(lines):
                    raise SchemaRefusal("a text-bundle section has no act identity or literal")
                # One literal per section, so the literal `uncertainty:` anchored
                # to below is the literal this section ends up recording. A second
                # `canonical_clean_text:` after the layer would replace `pending`
                # while `pending_uncertainty` kept the offsets checked against the
                # first -- an act recorded beside a layer that anchors to a text it
                # no longer carries. Two or more literal formats catch that drift
                # as a projection-identity mismatch; the text bundle is a legal
                # single literal format, where nothing else would.
                if pending is not None:
                    raise SchemaRefusal("a text-bundle section carries more than one literal")
                try:
                    literal = json.loads(lines[index + 1])
                except json.JSONDecodeError as error:
                    raise SchemaRefusal("a text-bundle canonical text is not JSON") from error
                if not isinstance(literal, str):
                    raise SchemaRefusal("a text-bundle canonical text is not a string")
                digest_line = lines[index - 1] if index else ""
                if not digest_line.startswith("canonical_text_sha256: "):
                    raise SchemaRefusal("a text-bundle literal has no declared hash")
                digest = digest_line.removeprefix("canonical_text_sha256: ")
                if not citations or digest != canonical_text_sha256(literal):
                    raise SchemaRefusal("a text-bundle literal identity or hash is invalid")
                pending = (literal, digest, tuple(citations))
            elif line == "uncertainty:":
                # Read back beside the literal it anchors to, exactly like
                # `canonical_clean_text:` above -- a text-bundle uncertainty
                # layer that cannot re-validate against its own act's literal
                # text is not a member `_compare_literal_projections` may treat
                # as this act's one uncertainty record.
                if current_id is None or pending is None or index + 1 >= len(lines):
                    raise SchemaRefusal(
                        "a text-bundle uncertainty layer has no literal to anchor to"
                    )
                if pending_uncertainty is not None:
                    raise SchemaRefusal(
                        "a text-bundle section carries more than one uncertainty layer"
                    )
                try:
                    uncertainty = json.loads(lines[index + 1])
                except json.JSONDecodeError as error:
                    raise SchemaRefusal("a text-bundle uncertainty layer is not JSON") from error
                try:
                    # The round trip, not merely the shape: the text bundle is the
                    # one format whose layer arrives as decoded UTF-8 text lines, so
                    # this is where an encoding that changed offset meaning would
                    # have to be caught.
                    utf8_round_trip(uncertainty, pending[0])
                    pending_uncertainty = uncertainty
                except SchemaRefusal as error:
                    raise SchemaRefusal(
                        "a text-bundle uncertainty layer does not anchor to its own act's literal"
                    ) from error
            elif line.startswith("text_status: "):
                # Anchored to a literal exactly as the layers around it are: a
                # status with no reading to describe is not this act's record of
                # its own damage, and a second one would leave the earlier value
                # unread beside the layer it was derived from.
                if current_id is None or pending is None:
                    raise SchemaRefusal(
                        "a text-bundle established-text status has no literal to describe"
                    )
                if pending_text_status is not None:
                    raise SchemaRefusal(
                        "a text-bundle section carries more than one established-text status"
                    )
                pending_text_status = line.removeprefix("text_status: ")
            elif line == "transcription_annotations:":
                if current_id is None or pending is None or index + 1 >= len(lines):
                    raise SchemaRefusal(
                        "a text-bundle transcription annotation layer has no literal to mark up"
                    )
                if pending_annotations is not None:
                    raise SchemaRefusal(
                        "a text-bundle section carries more than one transcription annotation layer"
                    )
                try:
                    pending_annotations = json.loads(lines[index + 1])
                except json.JSONDecodeError as error:
                    raise SchemaRefusal(
                        "a text-bundle transcription annotation layer is not JSON"
                    ) from error
            elif line == "display:":
                # Spec 11 test 2's second half, checked on the written product:
                # render -> strip -> hash. The rendered display is a reading aid and
                # stripping it must return the canonical field exactly, so a display
                # convention can never become characters in the hashed text.
                if current_id is None or pending is None or index + 1 >= len(lines):
                    raise SchemaRefusal("a text-bundle display has no literal to render")
                # Its own refusal rather than the one above: a section that reaches
                # its display with no `uncertainty:` line has a literal and is
                # missing the layer, which is the opposite defect and the one a
                # reader of the message has to act on.
                if pending_uncertainty is None:
                    raise SchemaRefusal(
                        "a text-bundle section carries a literal with no uncertainty layer"
                    )
                # And its own refusal again for the damage record, for the same
                # reason: a section that reaches its display with no status, or
                # with no annotation layer, is missing the fields that say whether
                # the reading about to be rendered is whole. Recomputed rather than
                # read: the readable bundle is a legal single literal format, where
                # cross-format identity would catch nothing.
                if pending_text_status is None or pending_annotations is None:
                    raise SchemaRefusal(
                        "a text-bundle section carries a literal with no established-text "
                        "status or no transcription annotation layer"
                    )
                _require_damage_record(
                    pending_text_status,
                    pending_annotations,
                    pending_uncertainty,
                    pending[0],
                    subject="text-bundle section",
                )
                convention_line = lines[index - 1] if index else ""
                if convention_line != f"display_convention: {DISPLAY_CONVENTION}":
                    raise SchemaRefusal("a text-bundle display names no known convention")
                try:
                    rendered = json.loads(lines[index + 1])
                except json.JSONDecodeError as error:
                    raise SchemaRefusal("a text-bundle display is not JSON") from error
                # `strip_display` raises `ValueError` on markup it cannot parse, and
                # every such rendering is something a package can carry.
                try:
                    stripped = strip_display(rendered) if isinstance(rendered, str) else None
                except ValueError as error:
                    raise SchemaRefusal(
                        "a text-bundle display is not a renderable display convention"
                    ) from error
                if stripped != pending[0]:
                    raise SchemaRefusal(
                        "a text-bundle display does not strip back to its canonical clean text"
                    )
                citation_folders = sorted(
                    {_source_folder_for_declared_path(path) for path, _digest in citations}
                )
                if folder not in citation_folders:
                    raise SchemaRefusal("a text-bundle act is enclosed by the wrong source folder")
                candidate = (
                    *pending,
                    pending_uncertainty,
                    pending_text_status,
                    pending_annotations,
                    heading_key,
                )
                location = (current_id, folder)
                if location in record_locations:
                    raise SchemaRefusal(
                        "a text-bundle repeats one act inside the same source folder; the "
                        "bundle is refused because duplicated sections cannot be collapsed "
                        "silently while cross-folder citations are reconciled"
                    )
                existing = records.get(current_id)
                if existing is not None and existing != candidate:
                    raise SchemaRefusal(
                        "a text-bundle repeats one act with different text, provenance, or "
                        "damage layers across source folders"
                    )
                record_locations.add(location)
                records[current_id] = candidate
                current_id, heading_key, pending, pending_uncertainty = None, None, None, None
                pending_text_status, pending_annotations = None, None
        if current_id is not None:
            raise SchemaRefusal("a text-bundle section has no completed literal record")
    return records


def _text_bundle_literals(root) -> dict[str, tuple]:
    return {
        act_id: (literal, digest, uncertainty, text_status, annotations)
        for act_id, (
            literal,
            digest,
            _citations,
            uncertainty,
            text_status,
            annotations,
            _heading_key,
        ) in _text_bundle_records(root).items()
    }


_STORED_ACTS_TABLES: Final = ("acts", "act_search", "export_metadata")
_SQLITE_PRODUCT_TABLES: Final = (*_STORED_ACTS_TABLES, "acts_fts")
# Writer and verifier share this DDL so the schema check includes FTS bindings,
# shadow tables, and implicit indexes without maintaining a second schema spelling.
_ACTS_DATABASE_DDL: Final = """
                CREATE TABLE export_metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE acts (
                    act_id TEXT PRIMARY KEY NOT NULL,
                    act_key TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    canonical_clean_text TEXT,
                    canonical_text_sha256 TEXT,
                    provenance_json TEXT,
                    source_regions_json TEXT,
                    uncertainty_json TEXT,
                    uncertainty_status TEXT NOT NULL,
                    text_status TEXT,
                    transcription_annotations_json TEXT,
                    semantic_annotations_json TEXT NOT NULL,
                    semantic_annotation_status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    approval_ref TEXT,
                    reason TEXT
                );
                CREATE TABLE act_search (
                    rowid INTEGER PRIMARY KEY,
                    act_id TEXT UNIQUE NOT NULL REFERENCES acts(act_id),
                    derived_search_text TEXT NOT NULL,
                    derived_text_sha256 TEXT NOT NULL,
                    derived_from_canonical_sha256 TEXT NOT NULL,
                    normalizer_revision TEXT NOT NULL,
                    derived_kind TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE acts_fts USING fts5(
                    derived_search_text,
                    content='act_search',
                    content_rowid='rowid',
                    tokenize='unicode61 remove_diacritics 2'
                );
                """


@lru_cache(maxsize=1)
def _expected_acts_schema() -> dict[str, tuple[str, str, str | None]]:
    """Derive this SQLite runtime's complete schema from the writer's DDL."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_ACTS_DATABASE_DDL)
        return {
            name: (kind, table, sql)
            for kind, name, table, sql in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            ).fetchall()
        }
    finally:
        connection.close()


def _verify_acts_schema(connection: sqlite3.Connection) -> None:
    """Require the exact object graph and FTS content binding the writer declares.

    FTS integrity alone cannot detect an index consistently repointed at a decoy
    content table. Exact declarations close that gap; the runtime-derived schema
    also closes shadow tables and implicit indexes without hard-coding them.
    """
    try:
        expected = _expected_acts_schema()
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal(
            "the verifier's SQLite runtime cannot construct the expected acts database "
            "schema; this package requires FTS5 support before its product identity can "
            "be checked"
        ) from error
    try:
        actual = {
            name: (kind, table, sql)
            for kind, name, table, sql in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            ).fetchall()
        }
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal("the acts database has no readable schema") from error
    for name in _SQLITE_PRODUCT_TABLES:
        if actual.get(name) != expected[name]:
            raise SchemaRefusal(
                f"the acts database declares {name!r} with a definition this build never "
                "wrote; a product table redefined after sealing is not the product"
            )
    unexpected = sorted(set(actual) - set(expected))
    if unexpected:
        raise SchemaRefusal(
            f"the acts database carries unaccounted schema object(s) {unexpected}; an acts "
            "database holds exactly what its export DDL creates"
        )


def _open_acts_database(path) -> sqlite3.Connection:
    """Open a package's acts database read-only, as stored rows and not as a program.

    **A table is not a view.** Every read below names ``acts``, ``act_search`` or
    ``export_metadata``, and SQLite is perfectly happy for any of them to be a
    *view* -- which is a program. A view over a recursive CTE turns a few
    kilobytes of package member into an unbounded result set: built one, and
    watched this function's caller allocate until the kernel killed the process.
    Same amplification the ``ZIP_STORED`` check refuses in the archive reader,
    and closed the same way -- a stored table's row count is bounded by the
    member's own physical bytes, a view's by nothing.

    **A path is not a URI.**  ``f"file:{path}?mode=ro"`` makes a directory named ``x?y``
    into a query string, and ``bundle.py`` derives its staging directory from the
    operator's own ``--out`` name, so a good package gets refused with a message
    blaming the package.  ``as_uri`` percent-encodes.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal("the acts database cannot be opened") from error
    try:
        placeholders = ", ".join("?" for _name in _SQLITE_PRODUCT_TABLES)
        kinds = dict(
            connection.execute(
                f"SELECT name, type FROM sqlite_master WHERE name IN ({placeholders})",
                _SQLITE_PRODUCT_TABLES,
            ).fetchall()
        )
        user_version = connection.execute("PRAGMA user_version").fetchone()
        schema = connection.execute(
            "SELECT value FROM export_metadata WHERE key = 'schema'"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        connection.close()
        raise SchemaRefusal("the acts database has no readable schema") from error
    if any(kinds.get(name) != "table" for name in _STORED_ACTS_TABLES):
        connection.close()
        raise SchemaRefusal("the acts database does not carry acts and act_search as stored tables")
    if (
        kinds.get("acts_fts") != "table"
        or user_version != (_SQLITE_USER_VERSION,)
        or schema != (_SQLITE_SCHEMA,)
    ):
        connection.close()
        raise SchemaRefusal("the acts database has no recognized SQLite product identity")
    try:
        _verify_acts_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _database_literals(path) -> dict[str, tuple]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        rows = connection.execute(
            """
            SELECT act_id, canonical_clean_text, canonical_text_sha256, uncertainty_json,
                   text_status, transcription_annotations_json
            FROM acts
            WHERE canonical_clean_text IS NOT NULL
            ORDER BY act_id
            """
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal("the acts database cannot be read for projection identity") from error
    finally:
        if connection is not None:
            connection.close()
    records: dict[str, tuple] = {}
    for act_id, literal, digest, uncertainty_json, text_status, annotations_json in rows:
        if (
            not isinstance(act_id, str)
            or not isinstance(literal, str)
            or not isinstance(digest, str)
            or not isinstance(uncertainty_json, str)
            or not isinstance(annotations_json, str)
        ):
            raise SchemaRefusal("the acts database has an untyped literal row")
        if digest != canonical_text_sha256(literal) or act_id in records:
            raise SchemaRefusal("the acts database literal identity or hash is invalid")
        try:
            uncertainty = json.loads(uncertainty_json)
        except json.JSONDecodeError as error:
            raise SchemaRefusal("the acts database uncertainty layer is not JSON") from error
        annotations = _database_json_layer(annotations_json, "transcription annotation")
        _require_damage_record(
            text_status, annotations, uncertainty, literal, subject="acts database row"
        )
        records[act_id] = (
            literal,
            digest,
            validate_uncertainty(uncertainty, literal),
            text_status,
            annotations,
        )
    return records


def _jsonl_literals(path) -> dict[str, tuple]:
    records: dict[str, tuple] = {}
    for line in _package_lines(path, "acts JSONL"):
        if not line:
            continue
        # This reader is independently callable, so malformed JSON must remain a
        # named package refusal regardless of validation order.
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SchemaRefusal("an acts JSONL row is not JSON") from error
        if not isinstance(record, dict):
            raise SchemaRefusal("an acts JSONL row is not an object")
        literal = record.get(CANONICAL_TEXT_FIELD)
        if literal is None:
            continue
        act_id, digest = record.get("act_id"), record.get("canonical_text_sha256")
        if (
            not isinstance(act_id, str)
            or not isinstance(literal, str)
            or not isinstance(digest, str)
        ):
            raise SchemaRefusal("an acts JSONL literal row is untyped")
        if digest != canonical_text_sha256(literal) or act_id in records:
            raise SchemaRefusal("an acts JSONL literal identity or hash is invalid")
        _require_damage_record(
            record.get("text_status"),
            record.get("transcription_annotations"),
            record.get("uncertainty"),
            literal,
            subject="acts JSONL row",
        )
        records[act_id] = (
            literal,
            digest,
            validate_uncertainty(record.get("uncertainty"), literal),
            record.get("text_status"),
            record.get("transcription_annotations"),
        )
    return records


def _page_ledger_category(
    ordinal: int, act_categories: list[str], *, edge_hold: bool = False
) -> tuple[str, str | None]:
    """One sealed page's terminal category, derived from the acts cut on it.

    Every rule here errs toward `held-for-review`, the category that means a human
    must look.  A sealed page nobody marked an act out on is held, never
    `confirmed-blank`: silence cannot tell a genuinely blank page from a detection
    failure, and `run_aggregate` already refuses to infer blank from silence. This
    function itself never *infers* blank -- a page whose acts are all themselves
    `confirmed-blank` simply inherits that proof from them, the same way it
    inherits `delivered` when any act on it is delivered; what artifact would let
    this stage *prove* a page blank on its own, with no acts to inherit the
    category from, is open (HANDOFF.md).
    """
    if edge_hold:
        return (
            ArmariumCategory.HELD_FOR_REVIEW.value,
            f"unclaimed-edge-ink: page {ordinal} carries unreleased ink-map evidence",
        )
    if not act_categories:
        return ArmariumCategory.HELD_FOR_REVIEW.value, SILENT_PAGE_REASON.format(ordinal=ordinal)
    distinct = sorted(set(act_categories))
    if ArmariumCategory.DELIVERED.value in distinct:
        return ArmariumCategory.DELIVERED.value, None
    if distinct == [ArmariumCategory.EXCLUDED_WITH_APPROVAL.value]:
        return ArmariumCategory.EXCLUDED_WITH_APPROVAL.value, None
    if distinct == [ArmariumCategory.CONFIRMED_BLANK.value]:
        return ArmariumCategory.CONFIRMED_BLANK.value, None
    return (
        ArmariumCategory.HELD_FOR_REVIEW.value,
        f"page {ordinal} delivered no act; its acts are {', '.join(distinct)}",
    )


def _terminal_ledger(
    act_outcomes: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    act_pages: dict[str, Any],
    aggregate: dict[str, Any],
    edge_hold_pages: tuple[int, ...] = (),
) -> dict[str, Any]:
    """The honesty ledger: one closed category for every unit the run accounted for.

    Spec 11 test 1 is a *total partition*: every submitted source, every sealed page and
    every proposed act lands in exactly one of the five categories, and a unit in no set
    is invariant #10's imbalance. So all three unit types are enumerated here, and a unit
    outside the five sets, a repeated unit identity, or a count that does not reconcile
    stops the export rather than being reported.

    A source unit inherits the category of the page it sealed into: they are two
    questions with one answer, and giving the source its own vocabulary would mean
    inventing a sixth meaning for `delivered`. `by_unit_type` is published beside
    `by_category` because the three populations overlap by design -- an act, the page it
    was cut from, and the source that sealed that page are three units describing one
    piece of material, so a reader adding the category counts up is counting units, not
    acts.
    """
    by_act_id: dict[str, dict[str, Any]] = {}
    categories_by_key: dict[str, str] = {}
    for record in act_outcomes:
        # Today's two callers both deduplicate before calling, so this cannot fire from
        # inside the module. It stays because the total-partition claim above is made by
        # this function about itself: a duplicate act id collapsing silently into the
        # dict below is the very "unit in no set at all" the claim forbids.
        if record["act_id"] in by_act_id:
            raise SchemaRefusal(
                f"terminal ledger act outcomes repeat act identity {record['act_id']!r}"
            )
        by_act_id[record["act_id"]] = record
        categories_by_key[record["act_key"]] = record["category"]

    if not isinstance(act_pages, dict):
        raise SchemaRefusal("an Armarium terminal ledger has no act page attribution")
    acts_on_page: dict[int, list[str]] = {}
    for act_key, ordinals in act_pages.items():
        category = categories_by_key.get(act_key)
        if category is None:
            raise SchemaRefusal(
                "an Armarium terminal ledger attributes pages to an act it does not account for"
            )
        if not isinstance(ordinals, (list, tuple)):
            raise SchemaRefusal("an Armarium terminal ledger act has no page ordinal list")
        for ordinal in ordinals:
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise SchemaRefusal("an Armarium terminal ledger act names a non-integer page")
            acts_on_page.setdefault(ordinal, []).append(category)

    page_units: list[dict[str, Any]] = []
    source_units: list[dict[str, Any]] = []
    for page in sorted(pages, key=lambda row: row["ordinal"]):
        ordinal = page["ordinal"]
        if page.get("outcome") == "sealed":
            category, reason = _page_ledger_category(
                ordinal, acts_on_page.get(ordinal, []), edge_hold=ordinal in edge_hold_pages
            )
            page_units.append(
                {
                    "unit_type": "page",
                    "unit_id": f"page:{ordinal}",
                    "category": category,
                    "reason": reason,
                    "declared_path": page.get("declared_path"),
                    "declared_sha256": page.get("declared_sha256"),
                }
            )
        else:
            category = ArmariumCategory.REFUSED_WITH_REASON.value
            reason = page.get("reason") or "no reason was recorded"
        source_units.append(
            {
                "unit_type": "source",
                "unit_id": f"source:{ordinal}",
                "category": category,
                "reason": reason,
                "declared_path": page.get("declared_path"),
                "declared_sha256": page.get("declared_sha256"),
            }
        )

    act_units = [
        {
            "unit_type": "act",
            "unit_id": f"act:{act_id}",
            "category": record["category"],
            "reason": record["reason"],
            "act_key": record["act_key"],
        }
        for act_id, record in sorted(by_act_id.items())
    ]

    units = source_units + page_units + act_units
    known = {category.value for category in ArmariumCategory}
    by_category = {category: 0 for category in sorted(known)}
    by_unit_type = {"source": 0, "page": 0, "act": 0}
    seen: set[str] = set()
    for unit in units:
        if unit["category"] not in known:
            raise SchemaRefusal(
                f"terminal ledger unit {unit['unit_id']} carries category "
                f"{unit['category']!r}, which is not one of the five closed categories"
            )
        if unit["unit_id"] in seen:
            raise SchemaRefusal(f"terminal ledger unit {unit['unit_id']} is accounted twice")
        seen.add(unit["unit_id"])
        by_category[unit["category"]] += 1
        by_unit_type[unit["unit_type"]] += 1
    if sum(by_category.values()) != len(units):
        raise SchemaRefusal("a terminal ledger unit landed in no category at all")

    unresolved = [
        f"{unit['unit_type']} {unit['unit_id'].split(':', 1)[1]} is {unit['category']}"
        + (f": {unit['reason']}" if unit["reason"] else "")
        for unit in units
        if unit["category"] not in _COMPLETED_CATEGORIES
    ]
    reasons = list(unresolved)
    if aggregate.get("status") != "complete":
        for reason in aggregate.get("reasons", []):
            if reason not in reasons:
                reasons.append(reason)
    return {
        "schema": TERMINAL_LEDGER_SCHEMA,
        "denominator": _LEDGER_DENOMINATOR,
        "source_granularity": _SOURCE_GRANULARITY,
        "granularity_limit": _CONTAINER_GRANULARITY_LIMIT,
        "unit_count": len(units),
        "by_unit_type": by_unit_type,
        "by_category": by_category,
        "units": units,
        "status": "complete" if not reasons else "partial",
        "unresolved_reasons": reasons,
    }


def _export_manifest(
    projection: ArmariumProjection,
    formats: ArmariumFormats,
    members: dict[str, bytes],
) -> dict[str, Any]:
    counts = Counter(act["category"] for act in projection.acts)
    categories = [
        {
            "category": category.value,
            "count": counts[category.value],
            "act_ids": sorted(
                act["act_id"] for act in projection.acts if act["category"] == category.value
            ),
        }
        for category in ArmariumCategory
    ]
    submission_paths = {
        row.get("relative_path")
        for row in projection.source_manifest
        if isinstance(row, dict) and isinstance(row.get("relative_path"), str)
    }
    salvage_claim = (
        {
            "namespace": "salvage",
            "status": "accounted",
            "count": len(projection.salvage_items),
            "promotion": _SALVAGE_PROMOTION_CLAIM,
        }
        if projection.salvage_items is not None
        else {
            "namespace": "salvage",
            "status": "not-produced-no-sealed-salvage-inventory",
            "count": None,
            "reason": _SALVAGE_ABSENCE_REASON,
            "promotion": _SALVAGE_PROMOTION_CLAIM,
        }
    )
    edge_hold_pages = edge_hold_pages_from_rows(list(projection.ink_map_pages))
    ledger = _terminal_ledger(
        _act_outcomes(projection.acts),
        list(projection.pages),
        projection.aggregate_basis.get("act_pages")
        if isinstance(projection.aggregate_basis, dict)
        else None,
        projection.aggregate,
        edge_hold_pages,
    )
    manifest: dict[str, Any] = {
        "schema": EXPORT_MANIFEST_SCHEMA,
        "canonical_text": {
            "authority": "archetypus",
            "field": CANONICAL_TEXT_FIELD,
            "hash": "sha256-utf-8",
            "derived_columns_are_marked": True,
            # `verify_projection_identity` only runs when two or more literal
            # formats are selected -- with one selected (or zero) there is
            # nothing to compare, and this says so on the bundle's own face
            # rather than leaving a reader to infer it from `formats`.
            "identity_verified_across": sorted(set(_LITERAL_TEXT_FORMATS) & set(formats.formats))
            if len(set(_LITERAL_TEXT_FORMATS) & set(formats.formats)) >= 2
            else [],
        },
        "run": {
            "fixture_id": projection.fixture_id,
            "scenario": projection.scenario,
            "config_digest": projection.config_digest,
        },
        "formats": formats.to_record(),
        "claims": {
            # Measured, not constant. A status that says `partial` on every run
            # whatever happened cannot distinguish the run that lost something from
            # the run that did not, which is the distinction GOVERNANCE 2 exists to
            # keep visible.
            "status": ledger["status"],
            "partial_reasons": ledger["unresolved_reasons"],
            "terminal_ledger": ledger,
            "ink_map": {
                "denominator": INK_MAP_DENOMINATOR,
                "held_pages": list(edge_hold_pages),
            },
            "act_partition": _act_partition_claim(projection, categories),
            "submission_inventory": {
                "status": "reconciled-at-source-page-ordinal-granularity",
                "granularity": _SOURCE_GRANULARITY,
                "limit": _CONTAINER_GRANULARITY_LIMIT,
                "observed_source_page_rows": len(projection.source_manifest),
                "observed_distinct_declared_paths": len(submission_paths),
            },
            "page_census": {
                "denominator": _PAGE_CENSUS_DENOMINATOR,
                "counted": len(projection.pages),
                "status": "accounted-in-the-terminal-ledger",
            },
            "pixels": {
                "embedded": formats.embed_pixels,
                "resolution_claim": (
                    _PIXEL_EMBEDDED_CLAIM if formats.embed_pixels else _PIXEL_REFERENCE_CLAIM
                ),
            },
            "retained_run_references": {
                "availability": _RUN_ACCESS_REQUIRED,
                "resolution_claim": "artifact and receipt citations require retained-run access",
            },
            "semantic_annotations": {
                "status": _SEMANTIC_ANNOTATIONS_CLAIM,
                "text_writable": False,
            },
            # Named beside it rather than folded into it: the package carries a
            # real annotation layer, and a `claims` block whose only annotation
            # entry said "not produced" was a true statement about one layer read
            # by every recipient as a statement about both.
            "transcription_annotations": _transcription_annotations_claim(formats.formats),
            "uncertainty": _uncertainty_claim(formats.formats),
            # Labelled a proposal because it is one: spec 11 leaves the choice of
            # convention to Tyrel at this gate, and nothing hashed depends on it.
            # `renders_canonical_uncertainty` is the declaration R8 owes: the
            # record DOES carry the layer now, the `uncertainty:` field beside each
            # literal carries it into the product, and this rendering deliberately
            # does not -- said here rather than left for a reader to infer from a
            # `display:` line that looks like a complete reading.
            "display": {
                "convention": DISPLAY_CONVENTION,
                "status": "proposed-pending-tyrels-choice",
                "alters_stored_text": False,
                "renders_canonical_uncertainty": False,
                "exercised_against_real_spans": False,
                "reason": _DISPLAY_REASON,
            },
            "salvage": salvage_claim,
        },
        "aggregate": projection.aggregate,
        "aggregate_basis": projection.aggregate_basis,
        "witness_chairs": list(projection.witness_chairs),
        "witness_floor": projection.witness_floor,
        "members": [
            {"path": name, "sha256": digest_bytes(content), "bytes": len(content)}
            for name, content in sorted(members.items())
        ],
    }
    manifest["self_hash"] = self_hash(manifest)
    return manifest


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    if EXPORT_MANIFEST_NAME not in members:
        raise SchemaRefusal("an Armarium package cannot omit EXPORT_MANIFEST.json")
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED, strict_timestamps=True) as archive:
        names = [EXPORT_MANIFEST_NAME] + sorted(
            name for name in members if name != EXPORT_MANIFEST_NAME
        )
        _validate_archive_member_names(names)
        for name in names:
            info = ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, members[name])
    return buffer.getvalue()


def _load_sources(root) -> dict[str, Any]:
    try:
        record = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal("the package sources citation is unreadable") from error
    if not isinstance(record, dict) or record.get("schema") != SOURCES_SCHEMA:
        raise SchemaRefusal("the package sources citation has no recognized schema")
    if set(record) != {
        "schema",
        "pages",
        "regions",
        "act_citations",
        "act_outcomes",
        "aggregate_basis",
        "ink_map_pages",
        "witness_chairs",
        "witness_floor",
        "salvage_regions",
    }:
        raise SchemaRefusal("the package sources citation has an unrecognized field set")
    (
        pages,
        regions,
        act_citations,
        act_outcomes,
        aggregate_basis,
        witness_chairs,
        witness_floor,
        salvage_regions,
    ) = (
        record.get("pages"),
        record.get("regions"),
        record.get("act_citations"),
        record.get("act_outcomes"),
        record.get("aggregate_basis"),
        record.get("witness_chairs"),
        record.get("witness_floor"),
        record.get("salvage_regions"),
    )
    ink_map_pages = _validate_ink_map_pages(
        record.get("ink_map_pages"), "the package sources citation"
    )
    if (
        not isinstance(pages, list)
        or not isinstance(regions, list)
        or not isinstance(act_citations, list)
        or not isinstance(act_outcomes, list)
        or not isinstance(aggregate_basis, dict)
        or not isinstance(salvage_regions, list)
    ):
        raise SchemaRefusal("the package sources citation has no page and region lists")
    return {
        "pages": pages,
        "regions": regions,
        "act_citations": act_citations,
        "act_outcomes": act_outcomes,
        "aggregate_basis": aggregate_basis,
        "ink_map_pages": ink_map_pages,
        "witness_chairs": witness_chairs,
        "witness_floor": witness_floor,
        "salvage_regions": salvage_regions,
    }


def _manifest_formats(manifest: dict[str, Any]) -> ArmariumFormats:
    """Refuse a self-hashed manifest that names an unrecognized product set."""
    try:
        return armarium_formats_from_record(
            manifest.get("formats"), source="EXPORT_MANIFEST.json formats"
        )
    except SchemaRefusal as error:
        raise SchemaRefusal("EXPORT_MANIFEST.json has invalid format selections") from error


def _required_format_members(
    formats: ArmariumFormats, pages: list[dict[str, Any]]
) -> dict[str, set[str]]:
    """The non-optional members each selected product must contribute."""
    required: dict[str, set[str]] = {}
    if "text-bundle" in formats.formats:
        folders: set[str] = set()
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("declared_path"), str):
                raise SchemaRefusal("a text-bundle source citation has no declared path")
            folders.add(_source_folder_for_declared_path(page["declared_path"]))
        required["text-bundle"] = {_text_member_path(folder) for folder in folders}
    if "acts-database" in formats.formats:
        required["acts-database"] = {"acts.sqlite"}
    if "jsonl" in formats.formats:
        required["jsonl"] = {"acts.jsonl"}
    if "review-items" in formats.formats:
        required["review-items"] = {"review-items.jsonl"}
    if "salvage-tier" in formats.formats:
        required["salvage-tier"] = {"salvage/items.jsonl"}
    return required


def _all_pixel_references(sources: dict[str, list[dict[str, Any]]]) -> list[Any]:
    """Every page and crop pixel reference in the source graph, one place to gather.

    Shared by every reader that needs "all of them regardless of namespace" --
    a future pixel-bearing collection, or a rename of one of these three keys,
    now only has to be added here to stay visible to both the embedded-member
    inventory and the pixel-claim verifier below.
    """
    references: list[Any] = [
        page.get("page_image") for page in sources["pages"] if isinstance(page, dict)
    ]
    references.extend(
        region.get("crop_image") for region in sources["regions"] if isinstance(region, dict)
    )
    references.extend(
        region.get("crop_image")
        for region in sources["salvage_regions"]
        if isinstance(region, dict)
    )
    return references


def _embedded_member_paths(sources: dict[str, list[dict[str, Any]]]) -> set[str]:
    """Return exactly the pixel members cited as embedded by the source graph."""
    members: set[str] = set()
    for reference in _all_pixel_references(sources):
        if not isinstance(reference, dict) or reference.get("availability") != _EMBEDDED:
            continue
        member = reference.get("member_path")
        if not isinstance(member, str):
            raise SchemaRefusal("an embedded source citation has no member path")
        _validate_member_name(member)
        if member in members:
            raise SchemaRefusal("two package source citations name one embedded member")
        members.add(member)
    return members


def _verify_exact_product_members(
    formats: ArmariumFormats, sources: dict[str, list[dict[str, Any]]], actual_names: set[str]
) -> None:
    """Make the manifest's format list a closed promise, in both directions."""
    selected = set().union(*_required_format_members(formats, sources["pages"]).values())
    expected = {EXPORT_MANIFEST_NAME, "sources.json", *selected}
    expected.update(_embedded_member_paths(sources))
    if actual_names != expected:
        missing = sorted(expected - actual_names)
        unexpected = sorted(actual_names - expected)
        raise SchemaRefusal(
            "package members disagree with its selected formats "
            f"(missing={missing}, unexpected={unexpected})"
        )


def _manifest_act_categories(manifest: dict[str, Any]) -> dict[str, str]:
    """Read the manifest's five-category act denominator without trusting it."""
    claims = manifest.get("claims")
    partition = claims.get("act_partition") if isinstance(claims, dict) else None
    if not isinstance(partition, dict):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no act partition claim")
    expected_count = partition.get("expected_count")
    counted = partition.get("counted")
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 0
        or not isinstance(counted, int)
        or isinstance(counted, bool)
        or counted < 0
        or partition.get("reconciles") is not True
    ):
        raise SchemaRefusal("EXPORT_MANIFEST.json has an unreconciled act partition claim")
    rows = partition.get("categories")
    if not isinstance(rows, list):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no category rows")

    expected_categories = {category.value for category in ArmariumCategory}
    seen_categories: set[str] = set()
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SchemaRefusal("an act partition category row is not an object")
        category, count, act_ids = row.get("category"), row.get("count"), row.get("act_ids")
        if (
            not isinstance(category, str)
            or category not in expected_categories
            or category in seen_categories
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(act_ids, list)
            or count != len(act_ids)
        ):
            raise SchemaRefusal("an act partition category row is malformed")
        seen_categories.add(category)
        for act_id in act_ids:
            if not isinstance(act_id, str) or not act_id or act_id in result:
                raise SchemaRefusal("an act partition repeats or omits an act identity")
            result[act_id] = category
    if (
        seen_categories != expected_categories
        or len(result) != expected_count
        or counted != expected_count
    ):
        raise SchemaRefusal(
            "EXPORT_MANIFEST.json act categories do not reconcile to its denominator"
        )
    aggregate = manifest.get("aggregate")
    expected_counts = Counter(result.values())
    nonzero_counts = {category: expected_counts[category] for category in sorted(expected_counts)}
    if not isinstance(aggregate, dict) or aggregate.get("by_category") != nonzero_counts:
        raise SchemaRefusal("the exported aggregate does not reconcile to the act partition")
    return result


def _manifest_act_keys(manifest: dict[str, Any], categories: dict[str, str]) -> dict[str, str]:
    """Read the key-to-category accounting link needed to recompute the aggregate."""
    claims = manifest.get("claims")
    partition = claims.get("act_partition") if isinstance(claims, dict) else None
    keys = partition.get("act_keys") if isinstance(partition, dict) else None
    if not isinstance(keys, dict) or set(keys) != set(categories):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no complete act-key partition")
    if any(not isinstance(act_key, str) or not act_key for act_key in keys.values()):
        raise SchemaRefusal("EXPORT_MANIFEST.json has an invalid act key")
    if len(set(keys.values())) != len(keys):
        raise SchemaRefusal("EXPORT_MANIFEST.json repeats an act key")
    return keys


def _verify_honest_status_claims(
    manifest: dict[str, Any], categories: dict[str, str], sources: dict[str, list[dict[str, Any]]]
) -> None:
    """Refuse a self-hashed package that changes a measured partial result to green.

    The top-level claim is the terminal ledger's own status, and the ledger is
    recomputed here from the package's source graph rather than read out of the
    manifest -- a self-hash proves the manifest was not edited after it was written,
    not that what it says was ever true. The internal run aggregate is a separate
    measurement and is recomputed the same way.
    """
    claims = manifest.get("claims")
    if not isinstance(claims, dict):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no export claims")
    if manifest.get("witness_chairs") != sources.get("witness_chairs") or manifest.get(
        "witness_floor"
    ) != sources.get("witness_floor"):
        raise SchemaRefusal("the exported witness roster disagrees with its source accounting")
    _validate_witness_accounting(
        sources.get("witness_chairs"),
        sources.get("witness_floor"),
        sources.get("aggregate_basis"),
    )
    submission = claims.get("submission_inventory")
    if (
        not isinstance(submission, dict)
        or submission.get("status") != "reconciled-at-source-page-ordinal-granularity"
        or submission.get("granularity") != _SOURCE_GRANULARITY
        or submission.get("limit") != _CONTAINER_GRANULARITY_LIMIT
    ):
        raise SchemaRefusal("the export misstates what its submission denominator covers")

    aggregate = manifest.get("aggregate")
    if not isinstance(aggregate, dict):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no run aggregate")
    status, reasons = aggregate.get("status"), aggregate.get("reasons")
    if (
        status not in {"complete", "partial"}
        or not isinstance(reasons, list)
        or not all(isinstance(reason, str) and reason for reason in reasons)
    ):
        raise SchemaRefusal("the exported aggregate has no valid measured status and reasons")
    must_be_partial = any(category not in _COMPLETED_CATEGORIES for category in categories.values())
    must_be_partial = must_be_partial or any(
        page.get("outcome") != "sealed" for page in sources["pages"] if isinstance(page, dict)
    )
    # Derive the page-level incomplete cause from source rows, never from the
    # manifest claim being verified; otherwise a false claim verifies itself.
    ink_map_rows = _validate_ink_map_pages(
        sources.get("ink_map_pages"), "the package sources citation"
    )
    derived_edge_holds = _edge_hold_pages_from_validated_rows(ink_map_rows)
    sealed_ordinals = {
        page["ordinal"]
        for page in sources["pages"]
        if isinstance(page, dict) and page.get("outcome") == "sealed"
    }
    if {row["ordinal"] for row in ink_map_rows} != sealed_ordinals:
        raise SchemaRefusal(
            "the package's ink-map denominator is not exactly its own sealed page census. At "
            "least one page finding is missing or extra, so its terminal ledger cannot balance. "
            "Discard this extraction and rebuild the package from the intact run tree."
        )
    ink_map_claim = claims.get("ink_map")
    if (
        not isinstance(ink_map_claim, dict)
        or set(ink_map_claim) != {"denominator", "held_pages"}
        or ink_map_claim["denominator"] != INK_MAP_DENOMINATOR
        or ink_map_claim["held_pages"] != list(derived_edge_holds)
    ):
        raise SchemaRefusal(
            "the exported ink-map hold claim does not match the pages its own re-measured "
            "evidence still flags. The manifest and source graph disagree about which pages need "
            "review. Discard this extraction and rebuild the package from the intact run tree."
        )
    must_be_partial = must_be_partial or bool(derived_edge_holds)
    # The third way a run can be incomplete, and the one a category-only reading
    # could never see: an act that reached `delivered` carrying a reading its own
    # Perlector recorded a gap in. The full recomputation below covers it too;
    # this states it directly, so the refusal a tampered green package meets names
    # the damaged act rather than only "the aggregate does not match its basis".
    basis = sources["aggregate_basis"]
    recorded_status = basis.get("act_text_status") if isinstance(basis, dict) else None
    # The basis is the copy the verdict is computed from, so it is held to the
    # rows before it is believed: editing ONLY `aggregate_basis.act_text_status`
    # to `established` while every row honestly says `partial` would otherwise
    # pass every check on a clean machine and report `complete` — the repaired
    # defect's own shape, one level up.
    expected_status = {
        outcome["act_key"]: outcome["text_status"]
        for outcome in sources.get("act_outcomes", [])
        if outcome.get("category") == ArmariumCategory.DELIVERED.value
    }
    if (recorded_status or {}) != expected_status:
        raise SchemaRefusal(
            "the package's aggregate basis does not carry exactly the delivered acts' own "
            "established-text statuses; a verdict computed from an edited basis is not a "
            "measurement"
        )
    must_be_partial = must_be_partial or any(
        recorded != "established" for recorded in (recorded_status or {}).values()
    )
    if must_be_partial and (status != "partial" or not reasons):
        raise SchemaRefusal(
            "the exported aggregate claims complete despite measured incompleteness"
        )
    if status == "complete" and reasons:
        raise SchemaRefusal("a complete exported aggregate carries unresolved reasons")
    act_keys = _manifest_act_keys(manifest, categories)
    manifest_basis = manifest.get("aggregate_basis")
    if canonical_text(manifest_basis) != canonical_text(sources["aggregate_basis"]):
        raise SchemaRefusal("the exported aggregate basis disagrees with its source accounting")
    expected_aggregate = _aggregate_from_basis(
        {act_keys[act_id]: category for act_id, category in categories.items()},
        sources["pages"],
        sources["aggregate_basis"],
        derived_edge_holds,
    )
    if canonical_text(aggregate) != canonical_text(expected_aggregate):
        raise SchemaRefusal("the exported aggregate does not match its measured accounting basis")

    expected_ledger = _terminal_ledger(
        list(_act_outcome_sources(sources).values()),
        sources["pages"],
        sources["aggregate_basis"].get("act_pages")
        if isinstance(sources["aggregate_basis"], dict)
        else None,
        aggregate,
        derived_edge_holds,
    )
    if canonical_text(claims.get("terminal_ledger")) != canonical_text(expected_ledger):
        raise SchemaRefusal("the exported terminal ledger does not match its measured accounting")
    if (
        claims.get("status") != expected_ledger["status"]
        or claims.get("partial_reasons") != expected_ledger["unresolved_reasons"]
    ):
        raise SchemaRefusal("the export status does not match its own terminal ledger")
    page_census = claims.get("page_census")
    if (
        not isinstance(page_census, dict)
        or page_census.get("status") != "accounted-in-the-terminal-ledger"
    ):
        raise SchemaRefusal("the export page census makes no terminal-ledger claim")


def _verify_delivered_product_provenance(
    provenance: Any,
    source_regions: Any,
    source_graph_regions: list[dict[str, Any]],
    *,
    subject: str,
) -> None:
    """A delivered product row cannot discard the provenance export refused to omit."""
    if not isinstance(provenance, dict) or not provenance:
        raise SchemaRefusal(f"a delivered {subject} row has no provenance")
    if not isinstance(source_regions, list) or not source_regions:
        raise SchemaRefusal(f"a delivered {subject} row has no source-region provenance")
    known_regions = {canonical_text(region) for region in source_graph_regions}
    for region in source_regions:
        _validate_cited_region(region, subject=f"delivered {subject}")
        if canonical_text(region) not in known_regions:
            raise SchemaRefusal(f"a delivered {subject} row cites no packaged source region")


def _act_outcome_sources(sources: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Read all terminal categories and their explicit review reasons from the source graph."""
    records: dict[str, dict[str, Any]] = {}
    known = {category.value for category in ArmariumCategory}
    for record in sources["act_outcomes"]:
        if not isinstance(record, dict) or set(record) != {
            "act_id",
            "act_key",
            "category",
            "reason",
            "text_status",
        }:
            raise SchemaRefusal("a source act-outcome record has an unrecognized field set")
        act_id, act_key, category, reason, text_status = (
            record.get("act_id"),
            record.get("act_key"),
            record.get("category"),
            record.get("reason"),
            record.get("text_status"),
        )
        if (
            not isinstance(act_id, str)
            or not act_id
            or not isinstance(act_key, str)
            or not act_key
            or category not in known
            or not isinstance(reason, str | None)
            or act_id in records
        ):
            raise SchemaRefusal("a source act-outcome record has no valid terminal identity")
        # Delivered means an Archetypus record exists, which means a status exists.
        # Any other category means there is no record, so a status would describe a
        # reading that is not there.
        # isinstance folded into the delivered-iff-status equivalence: a
        # package-supplied unhashable value must be a named refusal, not a
        # TypeError out of the membership test.
        has_status = isinstance(text_status, str) and text_status in TEXT_STATUSES
        if has_status is not (category == ArmariumCategory.DELIVERED.value):
            raise SchemaRefusal(
                "a source act-outcome record's established-text status does not match whether "
                "the act was delivered"
            )
        if (
            category
            in {
                ArmariumCategory.HELD_FOR_REVIEW.value,
                ArmariumCategory.REFUSED_WITH_REASON.value,
            }
            and not reason
        ):
            raise SchemaRefusal("a source review outcome has no explicit reason")
        records[act_id] = record
    return records


def _act_citation_sources(sources: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Read the source graph's exact delivered-act lineage without carrying text."""
    records: dict[str, dict[str, Any]] = {}
    for record in sources["act_citations"]:
        if not isinstance(record, dict) or set(record) != {
            "act_id",
            "act_key",
            "evidence",
            "provenance",
            "source_regions",
        }:
            raise SchemaRefusal("a source act-citation record has an unrecognized field set")
        act_id, act_key = record.get("act_id"), record.get("act_key")
        if (
            not isinstance(act_id, str)
            or not act_id
            or not isinstance(act_key, str)
            or not act_key
            or act_id in records
        ):
            raise SchemaRefusal("a source act-citation record has no unique act identity")
        _verify_delivered_product_provenance(
            record.get("provenance"),
            record.get("source_regions"),
            sources["regions"],
            subject="source act-citation",
        )
        if not isinstance(record.get("evidence"), dict):
            raise SchemaRefusal("a source act-citation has no witness evidence")
        _verify_retained_references(record["evidence"])
        # sources.json is the only evidence-citation carrier common to every format
        # selection, including a text-bundle-only package.
        _verify_evidence_refs(
            record["evidence"].get("evidence_refs"), subject="a source act-citation"
        )
        records[act_id] = record
    return records


def _jsonl_act_records(
    path: Path, source_graph_regions: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Validate JSONL's one-record-per-act projection and return its categories."""
    lines = _package_lines(path, "acts JSONL")
    records: dict[str, dict[str, Any]] = {}
    known = {category.value for category in ArmariumCategory}
    for line in lines:
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SchemaRefusal("an acts JSONL row is not JSON") from error
        if not isinstance(record, dict) or record.get("schema") != ACT_RECORD_SCHEMA:
            raise SchemaRefusal("an acts JSONL row has no recognized schema")
        if set(record) != _ACT_RECORD_FIELDS:
            raise SchemaRefusal("an acts JSONL row has an unrecognized field set")
        _verify_retained_references(record)
        _verify_evidence_refs(record.get("evidence_refs"), subject="an acts JSONL row")
        act_id, act_key, category = (
            record.get("act_id"),
            record.get("act_key"),
            record.get("category"),
        )
        if (
            not isinstance(act_id, str)
            or not act_id
            or not isinstance(act_key, str)
            or not act_key
            or category not in known
            or act_id in records
        ):
            raise SchemaRefusal("an acts JSONL row has an invalid act identity or category")
        literal, digest = record.get(CANONICAL_TEXT_FIELD), record.get("canonical_text_sha256")
        reason = record.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise SchemaRefusal("an acts JSONL row has an untyped reason")
        if category == ArmariumCategory.DELIVERED.value:
            if not isinstance(literal, str) or digest != canonical_text_sha256(literal):
                raise SchemaRefusal("a delivered acts JSONL row has no valid literal text hash")
            _verify_delivered_product_provenance(
                record.get("provenance"),
                record.get("source_regions"),
                source_graph_regions,
                subject="acts JSONL",
            )
        elif literal is not None or digest is not None:
            raise SchemaRefusal("a non-delivered acts JSONL row carries purported clean text")
        _verify_carried_uncertainty(
            record.get("uncertainty"),
            record.get("uncertainty_status"),
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts JSONL",
        )
        _verify_carried_damage(
            record.get("text_status"),
            record.get("transcription_annotations"),
            record.get("uncertainty"),
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts JSONL",
        )
        _verify_semantic_annotation_row(
            record.get("semantic_annotations"),
            record.get("semantic_annotation_status"),
            subject="acts JSONL",
        )
        records[act_id] = {
            "act_key": act_key,
            "category": category,
            "evidence": _act_evidence(record),
            "provenance": record.get("provenance"),
            "source_regions": record.get("source_regions"),
            "reason": reason,
            "text_status": record.get("text_status"),
        }
    return records


def _database_uncertainty(encoded: Any) -> Any:
    """Decode the acts database's one uncertainty column, or refuse its bytes."""
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise SchemaRefusal("the acts database has an untyped uncertainty column")
    try:
        return json.loads(encoded)
    except json.JSONDecodeError as error:
        raise SchemaRefusal("the acts database uncertainty layer is not JSON") from error


def _database_json_layer(encoded: Any, subject: str) -> Any:
    """Decode one further JSON-encoded acts-database layer column, or refuse it."""
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise SchemaRefusal(f"the acts database has an untyped {subject} column")
    try:
        return json.loads(encoded)
    except json.JSONDecodeError as error:
        raise SchemaRefusal(f"the acts database {subject} layer is not JSON") from error


def _verify_carried_uncertainty(
    layer: Any, status: Any, literal: str | None, *, subject: str
) -> None:
    """Read one act row's uncertainty declaration and its payload as one statement.

    Cross-format identity (``_compare_literal_projections``) only compares the
    layers of two or more selected literal formats against each other, so on its
    own it leaves two things unasked: a package that selects exactly ONE literal
    format never has its layer read back at all, and ``uncertainty_status`` --
    the field a recipient reads to learn whether the layer is there -- is
    compared against nothing in any package. A row may not say
    ``not-applicable`` while carrying a layer, or claim canonical offsets while
    carrying none: the declaration and the payload are the same claim said twice,
    and a verifier that checks only one of them is asserting the other.
    """
    if literal is None:
        if layer is not None:
            raise SchemaRefusal(f"a non-delivered {subject} row carries an uncertainty layer")
        if status != _UNCERTAINTY_NOT_APPLICABLE:
            raise SchemaRefusal(
                f"a non-delivered {subject} row does not declare uncertainty not-applicable"
            )
        return
    if status != _UNCERTAINTY_AVAILABLE:
        raise SchemaRefusal(
            f"a delivered {subject} row does not declare the canonical uncertainty carriage"
        )
    try:
        utf8_round_trip(layer, literal)
    except SchemaRefusal as error:
        raise SchemaRefusal(
            f"a delivered {subject} row's uncertainty layer does not anchor to its own "
            "act's literal"
        ) from error


def _verify_carried_damage(
    text_status: Any, annotations: Any, uncertainty: Any, literal: str | None, *, subject: str
) -> None:
    """Read one act row's established-text status and the layer it derives from.

    Sits beside `_verify_carried_uncertainty` and asks the question that one
    cannot: uncertainty is checked for *anchoring*, and a well-anchored gap list
    beside a row claiming `established` is exactly the dishonesty this repairs. So
    the status is recomputed here, on a clean machine, from the row's own two
    damage layers -- the transcription annotations carried beside it and the
    canonical uncertainty that `_verify_carried_uncertainty` has just validated.

    A non-delivered row has no Archetypus record and therefore neither field. `[]`
    on a delivered row is a real answer -- no damage marked -- and is not the same
    claim as `None`.
    """
    if literal is None:
        if text_status is not None or annotations is not None:
            raise SchemaRefusal(
                f"a non-delivered {subject} row carries an established-text status or a "
                "transcription annotation layer"
            )
        return
    _require_damage_record(text_status, annotations, uncertainty, literal, subject=f"{subject} row")


def _verify_semantic_annotation_row(layer: Any, status: Any, *, subject: str) -> None:
    """The other annotation layer, checked as the fixed claim it still is.

    Nothing in this repository produces a semantic annotation, so every row says
    so. That was already true; what was not true is that the row said it under the
    bare name `annotations`, beside no mention of the transcription layer at all,
    so a reader met one word answering for two things and the sealed one lost.
    Checked here rather than assumed, for the same reason the manifest claim is:
    a row claiming a produced semantic layer must be refused by the verifier that
    knows none exists, not accepted because nothing disproves it.
    """
    if layer != [] or status != _SEMANTIC_ANNOTATION_NOT_PRODUCED:
        raise SchemaRefusal(
            f"a {subject} row's semantic annotation claim is not this build's fixed claim"
        )


def _database_act_records(
    path: Path, source_graph_regions: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, str]]]:
    """Validate the SQLite one-record-per-act projection and return categories."""
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        rows = connection.execute(
            "SELECT act_id, act_key, category, canonical_clean_text, canonical_text_sha256, "
            "provenance_json, source_regions_json, evidence_json, reason, "
            "uncertainty_json, uncertainty_status, text_status, "
            "transcription_annotations_json, semantic_annotations_json, "
            "semantic_annotation_status FROM acts"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal("the acts database cannot be read for product accounting") from error
    finally:
        if connection is not None:
            connection.close()
    records: dict[str, dict[str, Any]] = {}
    literals: dict[str, tuple[str, str]] = {}
    known = {category.value for category in ArmariumCategory}
    for (
        act_id,
        act_key,
        category,
        literal,
        digest,
        provenance,
        source_regions,
        evidence,
        reason,
        uncertainty_json,
        uncertainty_status,
        text_status,
        transcription_annotations_json,
        semantic_annotations_json,
        semantic_annotation_status,
    ) in rows:
        if (
            not isinstance(act_id, str)
            or not act_id
            or not isinstance(act_key, str)
            or not act_key
            or category not in known
            or act_id in records
        ):
            raise SchemaRefusal("the acts database has an invalid act identity or category")
        if category == ArmariumCategory.DELIVERED.value:
            if not isinstance(literal, str) or digest != canonical_text_sha256(literal):
                raise SchemaRefusal("a delivered acts database row has no valid literal text hash")
            literals[act_id] = (literal, digest)
        elif literal is not None or digest is not None:
            raise SchemaRefusal("a non-delivered acts database row carries purported clean text")
        if reason is not None and not isinstance(reason, str):
            raise SchemaRefusal("the acts database has an untyped reason")
        decoded: list[Any] = []
        for encoded in (provenance, source_regions, evidence):
            if encoded is None:
                decoded.append(None)
                continue
            try:
                parsed = json.loads(encoded)
            except (TypeError, json.JSONDecodeError) as error:
                raise SchemaRefusal(
                    "the acts database has unreadable provenance evidence"
                ) from error
            _verify_retained_references(parsed)
            decoded.append(parsed)
        evidence_refs = decoded[2].get("evidence_refs") if isinstance(decoded[2], dict) else None
        _verify_evidence_refs(evidence_refs, subject="an acts database row")
        if category == ArmariumCategory.DELIVERED.value:
            _verify_delivered_product_provenance(
                decoded[0], decoded[1], source_graph_regions, subject="acts database"
            )
        _verify_carried_uncertainty(
            _database_uncertainty(uncertainty_json),
            uncertainty_status,
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts database",
        )
        _verify_carried_damage(
            text_status,
            _database_json_layer(transcription_annotations_json, "transcription annotation"),
            _database_uncertainty(uncertainty_json),
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts database",
        )
        _verify_semantic_annotation_row(
            _database_json_layer(semantic_annotations_json, "semantic annotation"),
            semantic_annotation_status,
            subject="acts database",
        )
        records[act_id] = {
            "act_key": act_key,
            "category": category,
            "evidence": decoded[2],
            "provenance": decoded[0],
            "source_regions": decoded[1],
            "reason": reason,
            "text_status": text_status,
        }
    return records, literals


def _review_item_records(path: Path) -> dict[str, dict[str, str]]:
    """Validate the selected review projection's exact terminal population."""
    lines = _package_lines(path, "review-items JSONL")
    records: dict[str, dict[str, str]] = {}
    allowed = {
        ArmariumCategory.HELD_FOR_REVIEW.value,
        ArmariumCategory.REFUSED_WITH_REASON.value,
    }
    for line in lines:
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SchemaRefusal("a review-items JSONL row is not JSON") from error
        if not isinstance(record, dict):
            raise SchemaRefusal("a review-items JSONL row is not an object")
        _verify_retained_references(record)
        act_id, act_key, category, reason = (
            record.get("act_id"),
            record.get("act_key"),
            record.get("category"),
            record.get("reason"),
        )
        if record.get("schema") != "armarium-review-item.v1" or set(record) != _REVIEW_ITEM_FIELDS:
            raise SchemaRefusal("a review-items JSONL row has an unrecognized field set")
        if (
            not isinstance(act_id, str)
            or not act_id
            or not isinstance(act_key, str)
            or not act_key
            or category not in allowed
            or not isinstance(reason, str)
            or not reason
            or act_id in records
        ):
            raise SchemaRefusal("a review-items JSONL row has an invalid act identity or category")
        evidence_refs = record.get("evidence_refs")
        _verify_evidence_refs(evidence_refs, subject="a review-items JSONL row")
        records[act_id] = {
            "act_key": act_key,
            "category": category,
            "reason": reason,
            "evidence_refs": evidence_refs,
        }
    return records


def _salvage_product_records(path: Path) -> tuple[dict[str, Any], ...]:
    """Read the tier-only JSONL and reapply its no-acts firewall."""
    lines = _package_lines(path, "salvage-tier JSONL")
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SchemaRefusal("a salvage-tier JSONL row is not JSON") from error
        if not isinstance(record, dict) or record.get("schema") != SALVAGE_RECORD_SCHEMA:
            raise SchemaRefusal("a salvage-tier JSONL row has no recognized schema")
        _verify_retained_references(record)
        if set(record) != {
            "schema",
            "salvage_id",
            "content",
            "source_regions",
            "provenance",
            "promotion",
        }:
            raise SchemaRefusal("a salvage-tier JSONL row has an unrecognized field set")
        records.append({key: value for key, value in record.items() if key != "schema"})
    _validate_salvage_items(tuple(records))
    return tuple(records)


def _verify_salvage_claim(
    manifest: dict[str, Any],
    records: tuple[dict[str, Any], ...],
    sources: dict[str, list[dict[str, Any]]],
) -> None:
    claims = manifest.get("claims")
    salvage = claims.get("salvage") if isinstance(claims, dict) else None
    if not isinstance(salvage, dict) or salvage.get("namespace") != "salvage":
        raise SchemaRefusal("EXPORT_MANIFEST.json has no salvage-tier claim")
    status, count = salvage.get("status"), salvage.get("count")
    if salvage.get("promotion") != _SALVAGE_PROMOTION_CLAIM:
        raise SchemaRefusal("the salvage-tier promotion claim is not this build's fixed claim")
    if status == "accounted":
        if not isinstance(count, int) or isinstance(count, bool) or count != len(records):
            raise SchemaRefusal("the salvage-tier count does not reconcile to its records")
    elif status == "not-produced-no-sealed-salvage-inventory":
        if (
            count is not None
            or records
            or sources["salvage_regions"]
            or salvage.get("reason") != _SALVAGE_ABSENCE_REASON
        ):
            raise SchemaRefusal("an unproduced salvage tier claims or carries material")
    else:
        raise SchemaRefusal("EXPORT_MANIFEST.json has an invalid salvage-tier status")

    flattened = _salvage_regions(records)
    if canonical_text(flattened) != canonical_text(sources["salvage_regions"]):
        raise SchemaRefusal("salvage-tier records do not reconcile to their source citations")


def _product_categories(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {act_id: record["category"] for act_id, record in records.items()}


def _verify_exact_product_outcomes(
    records: dict[str, dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    *,
    subject: str,
) -> None:
    """Preserve terminal categories and their recorded reasons, never just their count."""
    if set(records) != set(outcomes):
        raise SchemaRefusal(f"the {subject} does not reconcile to source act outcomes")
    for act_id, record in records.items():
        outcome = outcomes[act_id]
        if (
            record["act_key"] != outcome["act_key"]
            or record["category"] != outcome["category"]
            or record.get("reason") != outcome["reason"]
            or record.get("text_status") != outcome["text_status"]
        ):
            raise SchemaRefusal(f"the {subject} does not retain its exact terminal reason")


def _verify_exact_delivered_citations(
    records: dict[str, dict[str, Any]],
    citations: dict[str, dict[str, Any]],
    act_keys: dict[str, str],
    *,
    subject: str,
) -> None:
    """Make every selected act projection retain the source graph's whole lineage."""
    if set(records) != set(act_keys):
        raise SchemaRefusal(f"the {subject} does not reconcile to the manifest act identities")
    if any(record["act_key"] != act_keys[act_id] for act_id, record in records.items()):
        raise SchemaRefusal(f"the {subject} does not reconcile to the manifest act keys")
    delivered = {
        act_id
        for act_id, record in records.items()
        if record["category"] == ArmariumCategory.DELIVERED.value
    }
    if delivered != set(citations):
        raise SchemaRefusal(f"the {subject} does not reconcile to source act citations")
    for act_id in sorted(delivered):
        record, citation = records[act_id], citations[act_id]
        if (
            record["act_key"] != citation["act_key"]
            or canonical_text(record["provenance"]) != canonical_text(citation["provenance"])
            or canonical_text(record["source_regions"])
            != canonical_text(citation["source_regions"])
            or canonical_text(record["evidence"]) != canonical_text(citation["evidence"])
        ):
            raise SchemaRefusal(f"the {subject} does not retain exact delivered provenance")


def _verify_fts_index_integrity(path: Path) -> None:
    """Verify every FTS term in both directions against ``act_search``.

    External-content FTS5 does not constrain its index to the content table, and a
    per-row MATCH probe cannot detect extra terms, ghost rowids, or tokenless rows.
    SQLite's integrity command covers the whole index but writes through its handle,
    so it must run on a private copy rather than mutate the delivered member.
    """
    with tempfile.TemporaryDirectory(prefix="armarium-fts-") as directory:
        writable = Path(directory) / "acts.sqlite"
        try:
            shutil.copyfile(path, writable)
            connection = sqlite3.connect(writable)
        except (OSError, sqlite3.DatabaseError) as error:
            raise SchemaRefusal(
                "the acts database cannot be read for full-text index verification"
            ) from error
        try:
            connection.execute("INSERT INTO acts_fts(acts_fts, rank) VALUES ('integrity-check', 1)")
        except sqlite3.DatabaseError as error:
            raise SchemaRefusal(
                "the acts database full-text index does not carry exactly its verified search "
                "folds; the index a recipient searches and the rows this package accounts for "
                "are not the same text"
            ) from error
        finally:
            connection.close()


def _verify_search_fold_claim(path: Path, literals: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Recompute the derived search column when its Unicode database is ours.

    A digest-checked SQLite member proves the package was not edited after
    sealing; it proves nothing about whether ``act_search.derived_search_text``
    was ever actually a fold of its own act's canonical clean text -- a build
    defect or a package rebuilt around a tampered column would pass every
    other check in this file with the search projection carrying unrelated
    text. Unlike ``claims.canonical_text``/``claims.semantic_annotations`` above, this
    one *does* have a source graph to recompute against: the literal each row's
    own ``act_id`` already carries in ``acts``. That recomputation is meaningful
    only under the Unicode database version that created the fold. A different
    verifier version keeps checking row identity and digests, but records that
    the fold calculation itself was not run instead of accusing a good package
    of tampering.
    """
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        metadata = dict(
            connection.execute(
                "SELECT key, value FROM export_metadata "
                "WHERE key IN ('normalizer_revision', 'unidata_version')"
            ).fetchall()
        )
        rows = connection.execute(
            "SELECT act_id, derived_search_text, derived_text_sha256, "
            "derived_from_canonical_sha256, normalizer_revision, derived_kind FROM act_search"
        ).fetchall()
        recorded_version = metadata.get("unidata_version")
        if (
            metadata.get("normalizer_revision") != TEXTNORM_REVISION
            or not isinstance(recorded_version, str)
            or not recorded_version
        ):
            raise SchemaRefusal("the acts database has no recognized search normalizer metadata")
        verifier_version = unicodedata.unidata_version
        recompute = recorded_version == verifier_version
        seen: set[str] = set()
        for act_id, derived, derived_hash, source_hash, revision, kind in rows:
            if (
                not isinstance(act_id, str)
                or act_id not in literals
                or act_id in seen
                or not isinstance(derived, str)
                or revision != TEXTNORM_REVISION
                or kind != "search-fold"
            ):
                raise SchemaRefusal("the acts database search projection has an invalid row")
            seen.add(act_id)
            literal, literal_hash = literals[act_id]
            if derived_hash != canonical_text_sha256(derived) or source_hash != literal_hash:
                raise SchemaRefusal(
                    "the acts database search projection is not a fold of its act's literal"
                )
            if recompute and derived != search_fold(literal):
                raise SchemaRefusal(
                    "the acts database search projection is not a fold of its act's literal"
                )
        if seen != set(literals):
            raise SchemaRefusal(
                "the acts database search projection does not cover exactly the delivered literals"
            )
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal(
            "the acts database search projection cannot be read for verification"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    # After the read-only pass and outside it: the index check needs a writable
    # handle, and the rows it is checked against have to be the ones already
    # proved to be folds of their own literals.
    _verify_fts_index_integrity(path)
    if recompute:
        return {
            "status": "verified",
            "recorded_unidata_version": recorded_version,
            "verifier_unidata_version": verifier_version,
            "statement": "search folds recomputed with the recorded Unicode database version",
        }
    return {
        "status": "not-run-unicode-database-mismatch",
        "recorded_unidata_version": recorded_version,
        "verifier_unidata_version": verifier_version,
        "statement": (
            "search-fold recomputation was not run because the package and verifier "
            "use different Unicode database versions"
        ),
    }


def _verify_product_accounting(
    root: Path,
    manifest: dict[str, Any],
    formats: ArmariumFormats,
    sources: dict[str, list[dict[str, Any]]],
) -> dict[str, str] | None:
    """Require every selected act projection to match the manifest denominator."""
    expected = _manifest_act_categories(manifest)
    _verify_honest_status_claims(manifest, expected, sources)
    act_keys = _manifest_act_keys(manifest, expected)
    delivered = {
        act_id
        for act_id, category in expected.items()
        if category == ArmariumCategory.DELIVERED.value
    }
    outcomes = _act_outcome_sources(sources)
    if _product_categories(outcomes) != expected or any(
        outcomes[act_id]["act_key"] != act_keys[act_id] for act_id in outcomes
    ):
        raise SchemaRefusal("source act outcomes do not reconcile to the manifest act partition")
    citations = _act_citation_sources(sources)
    if set(citations) != delivered or any(
        citations[act_id]["act_key"] != act_keys[act_id] for act_id in citations
    ):
        raise SchemaRefusal("source act citations do not reconcile to the manifest delivered acts")
    if "text-bundle" in formats.formats:
        text_records = _text_bundle_records(root, sources["pages"])
        if set(text_records) != delivered:
            raise SchemaRefusal(
                "the text bundle does not contain exactly the manifest's delivered acts"
            )
        for act_id, (
            _literal,
            _digest,
            text_citations,
            _uncertainty,
            _text_status,
            _annotations,
            heading_key,
        ) in text_records.items():
            if heading_key != act_keys[act_id]:
                raise SchemaRefusal(
                    "a text-bundle human heading does not authenticate its machine act identity"
                )
            expected_citations = tuple(
                (region["declared_path"], region["declared_sha256"])
                for region in citations[act_id]["source_regions"]
            )
            if text_citations != expected_citations:
                raise SchemaRefusal(
                    "the text bundle does not retain every delivered source citation"
                )
    search_fold_verification = None
    if "acts-database" in formats.formats:
        database_records, database_literals = _database_act_records(
            root / "acts.sqlite", sources["regions"]
        )
        if _product_categories(database_records) != expected:
            raise SchemaRefusal(
                "the acts database does not reconcile to the manifest act partition"
            )
        _verify_exact_product_outcomes(database_records, outcomes, subject="acts database")
        _verify_exact_delivered_citations(
            database_records, citations, act_keys, subject="acts database"
        )
        search_fold_verification = _verify_search_fold_claim(
            root / "acts.sqlite", database_literals
        )
    if "jsonl" in formats.formats:
        jsonl_records = _jsonl_act_records(root / "acts.jsonl", sources["regions"])
        if _product_categories(jsonl_records) != expected:
            raise SchemaRefusal("the acts JSONL does not reconcile to the manifest act partition")
        _verify_exact_product_outcomes(jsonl_records, outcomes, subject="acts JSONL")
        _verify_exact_delivered_citations(jsonl_records, citations, act_keys, subject="acts JSONL")
    if "review-items" in formats.formats:
        expected_review = {
            act_id
            for act_id, category in expected.items()
            if category
            in {ArmariumCategory.HELD_FOR_REVIEW.value, ArmariumCategory.REFUSED_WITH_REASON.value}
        }
        review_records = _review_item_records(root / "review-items.jsonl")
        if set(review_records) != expected_review:
            raise SchemaRefusal("review-items JSONL does not reconcile to the manifest review acts")
        _verify_exact_product_outcomes(
            review_records,
            {act_id: outcomes[act_id] for act_id in expected_review},
            subject="review-items JSONL",
        )
    if "salvage-tier" in formats.formats:
        _verify_salvage_claim(
            manifest,
            _salvage_product_records(root / "salvage/items.jsonl"),
            sources,
        )
    else:
        _verify_salvage_claim(manifest, (), sources)
    return search_fold_verification


def _verify_pixel_claims(
    manifest: dict[str, Any], formats: ArmariumFormats, sources: dict[str, list[dict[str, Any]]]
) -> None:
    """Check that the manifest's clean-machine claim matches its citations."""
    claims = manifest.get("claims")
    pixels = claims.get("pixels") if isinstance(claims, dict) else None
    if not isinstance(pixels, dict) or pixels.get("embedded") is not formats.embed_pixels:
        raise SchemaRefusal("the package pixel claim disagrees with its selected format settings")
    expected_claim = _PIXEL_EMBEDDED_CLAIM if formats.embed_pixels else _PIXEL_REFERENCE_CLAIM
    if pixels.get("resolution_claim") != expected_claim:
        raise SchemaRefusal("the package pixel-resolution claim is not the verified claim")

    expected_availability = _EMBEDDED if formats.embed_pixels else _SOURCE_ACCESS_REQUIRED
    for reference in _all_pixel_references(sources):
        if reference is not None and (
            not isinstance(reference, dict)
            or reference.get("availability") != expected_availability
        ):
            raise SchemaRefusal(
                "a package source citation disagrees with its selected pixel-embedding setting"
            )


def _verify_display_claim(manifest: dict[str, Any]) -> None:
    """A rendering may be proposed; it may not be presented as settled or as text."""
    claims = manifest.get("claims")
    display = claims.get("display") if isinstance(claims, dict) else None
    if (
        not isinstance(display, dict)
        or display.get("convention") != DISPLAY_CONVENTION
        or display.get("status") != "proposed-pending-tyrels-choice"
        or display.get("alters_stored_text") is not False
        or display.get("renders_canonical_uncertainty") is not False
        or display.get("exercised_against_real_spans") is not False
        or display.get("reason") != _DISPLAY_REASON
    ):
        raise SchemaRefusal("the package display claim is not the verified claim")


def _verify_retained_run_claim(manifest: dict[str, Any]) -> None:
    claims = manifest.get("claims")
    retained = claims.get("retained_run_references") if isinstance(claims, dict) else None
    if retained != {
        "availability": _RUN_ACCESS_REQUIRED,
        "resolution_claim": "artifact and receipt citations require retained-run access",
    }:
        raise SchemaRefusal("the package retained-run reference claim is not the verified claim")


def _verify_canonical_text_claim(manifest: dict[str, Any]) -> None:
    """The one field name and hash convention every literal projection is built from.

    Unlike every other ``claims.*`` section, this one and ``claims.semantic_annotations``
    below describe a fixed contract of this build rather than something computed
    from the package's own source graph -- there is nothing in ``sources.json`` to
    recompute them against. That is not a reason to leave them unchecked: without
    this, a tampered manifest could claim a different authority field, or a
    text-writable annotation layer, and every other check in this function would
    still accept the package.

    ``identity_verified_across`` is the one field in this claim that does vary by
    build, so it is recomputed from the manifest's own ``formats`` selection
    rather than compared to a constant.
    """
    canonical_text = manifest.get("canonical_text")
    if not isinstance(canonical_text, dict):
        raise SchemaRefusal("the package canonical-text claim is not this build's fixed claim")
    selected = manifest.get("formats")
    selected_formats = selected.get("formats") if isinstance(selected, dict) else None
    if not isinstance(selected_formats, list):
        raise SchemaRefusal("the package canonical-text claim is not this build's fixed claim")
    literal_selected = set(_LITERAL_TEXT_FORMATS) & set(selected_formats)
    expected_identity = sorted(literal_selected) if len(literal_selected) >= 2 else []
    if canonical_text != {
        "authority": "archetypus",
        "field": CANONICAL_TEXT_FIELD,
        "hash": "sha256-utf-8",
        "derived_columns_are_marked": True,
        "identity_verified_across": expected_identity,
    }:
        raise SchemaRefusal("the package canonical-text claim is not this build's fixed claim")


def _verify_annotations_claims(manifest: dict[str, Any]) -> None:
    """Two annotation layers, two claims, neither allowed to answer for the other.

    The *semantic* annotator is unbuilt and unwired (HANDOFF.md), so its claim is
    a fixed constant today, not a measurement -- but a tampered manifest claiming
    ``text_writable: true`` must still be refused here rather than accepted
    because nothing yet exists to disprove it.

    The *transcription* claim beside it is a measurement, like the uncertainty
    claim: the Archetypus's own `uncertain`/`illegible` marks are produced, and
    they ride in exactly the literal-text formats this package selected. Checked
    against the recomputed carriage rather than a constant, so a manifest naming a
    format it does not carry them in is describing a different package.
    """
    claims = manifest.get("claims")
    semantic = claims.get("semantic_annotations") if isinstance(claims, dict) else None
    if semantic != {"status": _SEMANTIC_ANNOTATIONS_CLAIM, "text_writable": False}:
        raise SchemaRefusal(
            "the package semantic-annotations claim is not this build's fixed claim"
        )
    selected = manifest.get("formats")
    format_rows = selected.get("formats") if isinstance(selected, dict) else None
    transcription = claims.get("transcription_annotations") if isinstance(claims, dict) else None
    if transcription != _transcription_annotations_claim(format_rows or []):
        raise SchemaRefusal(
            "the package transcription-annotations claim is not the measured carriage claim"
        )


def _verify_uncertainty_claim(manifest: dict[str, Any]) -> None:
    """The carriage claim is a measurement, and is checked as one.

    Unlike the annotations claim beside it, ``carried_by`` is not a constant: it
    is exactly the literal-text formats this package selected, so a manifest that
    named a format it does not carry the layer in -- or omitted one it does --
    would be describing a different package.
    """
    claims = manifest.get("claims")
    selected = manifest.get("formats")
    format_rows = selected.get("formats") if isinstance(selected, dict) else None
    uncertainty = claims.get("uncertainty") if isinstance(claims, dict) else None
    if uncertainty != _uncertainty_claim(format_rows or []):
        raise SchemaRefusal("the package uncertainty claim is not the canonical carriage claim")


def _verify_manifest_source_counts(
    manifest: dict[str, Any], sources: dict[str, list[dict[str, Any]]]
) -> None:
    """A source-page census claim must be no larger or smaller than its citations.

    `claims.submission_inventory`'s fields are named "submission" but counted here
    from `sources["pages"]`, the page census. The two populations are always equal
    because `run.py::page_census` refuses any submitted source with no page outcome
    and any page outcome with no submitted source before either can diverge -- so
    this is, in practice, a *stronger* check that source and page counts agree, not
    a weaker one that happens to read the wrong field.
    """
    ordinals: set[int] = set()
    paths: set[str] = set()
    for page in sources["pages"]:
        if not isinstance(page, dict):
            raise SchemaRefusal("a package source row is not an object")
        ordinal, path = page.get("ordinal"), page.get("declared_path")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal in ordinals
            or not isinstance(path, str)
        ):
            raise SchemaRefusal(
                "the package source census has duplicate or invalid page identities"
            )
        ordinals.add(ordinal)
        paths.add(path)
    claims = manifest.get("claims")
    page_census = claims.get("page_census") if isinstance(claims, dict) else None
    submission = claims.get("submission_inventory") if isinstance(claims, dict) else None
    if (
        not isinstance(page_census, dict)
        or page_census.get("counted") != len(sources["pages"])
        or not isinstance(submission, dict)
        or submission.get("observed_source_page_rows") != len(sources["pages"])
        or submission.get("observed_distinct_declared_paths") != len(paths)
    ):
        raise SchemaRefusal(
            "EXPORT_MANIFEST.json source-count claims do not reconcile to citations"
        )


def _verify_source_references(sources: list[dict[str, Any]], root) -> None:
    for page in sources:
        if not isinstance(page, dict):
            raise SchemaRefusal("a package source row is not an object")
        outcome, reason = page.get("outcome"), page.get("reason")
        if outcome not in {"sealed", "refused"}:
            raise SchemaRefusal("a package source row has no recognized Exemplar outcome")
        if not isinstance(reason, str):
            raise SchemaRefusal("a package source row has an untyped terminal reason")
        if outcome == "sealed" and reason:
            raise SchemaRefusal("a sealed package source page carries a refusal reason")
        if outcome == "refused" and not reason.strip():
            raise SchemaRefusal("a refused package source page has no terminal reason")
        declared_path, declared_sha256 = page.get("declared_path"), page.get("declared_sha256")
        if not isinstance(declared_path, str):
            raise SchemaRefusal("a package source row has no declared path")
        _source_folder_for_declared_path(declared_path)
        _require_sha256(declared_sha256, "a package source declared digest")
        if "ledger_sha256" in page:
            _require_sha256(page["ledger_sha256"], "a package source ledger digest")
        reference = page.get("page_image")
        if outcome == "sealed" and reference is None:
            raise SchemaRefusal("a sealed package source page has no pixel reference")
        if outcome != "sealed" and reference is not None:
            raise SchemaRefusal("a non-sealed package source page carries a pixel reference")
        if reference is None:
            continue
        _verify_reference(reference, root)


def _verify_region_references(sources: dict[str, list[dict[str, Any]]], root) -> None:
    pages = _pages_by_ordinal(sources["pages"])
    for region in sources["regions"]:
        _validate_cited_region(region, subject="exported act")
        _verify_region_page_binding(region, pages, subject="exported act")
        page_reference = pages[region["source_page_ordinal"]].get("page_image")
        _verify_reference(page_reference, root)
        _verify_reference(region.get("crop_image"), root)


def _verify_salvage_region_references(sources: dict[str, list[dict[str, Any]]], root) -> None:
    """Verify cited salvage ink separately from, and never as, an act region."""
    pages = _pages_by_ordinal(sources["pages"])
    seen: set[tuple[str, str]] = set()
    for region in sources["salvage_regions"]:
        if not isinstance(region, dict) or not isinstance(region.get("salvage_id"), str):
            raise SchemaRefusal("a salvage-tier source region has no tier identity")
        _validate_salvage_region(region)
        key = (region["salvage_id"], region["region_id"])
        if key in seen:
            raise SchemaRefusal("a salvage-tier source region repeats its tier identity")
        seen.add(key)
        _verify_region_page_binding(region, pages, subject="salvage-tier")
        page_reference = pages[region["source_page_ordinal"]].get("page_image")
        _verify_reference(page_reference, root)
        _verify_reference(region.get("crop_image"), root)


def _act_outcomes(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Keep the non-text terminal reason that review-items must reproduce exactly.

    `text_status` rides here beside `reason` because it is the same kind of fact:
    a closed word about how the act ended, carrying no characters of the reading.
    Putting it in the text-free source graph is what lets `_verify_exact_product_
    outcomes` require every selected format to retain it, rather than each format
    being trusted to have written it.
    """
    return [
        {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "category": act["category"],
            "reason": _export_reason(act),
            "text_status": act.get("text_status"),
        }
        for act in sorted(acts, key=lambda item: item["act_id"])
    ]


def _act_citations(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Keep each delivered act's exact non-text lineage beside the shared source graph."""
    return [
        {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "evidence": _act_evidence(act),
            "provenance": act["provenance"],
            "source_regions": act["source_regions"],
        }
        for act in sorted(acts, key=lambda item: item["act_id"])
        if act["category"] == ArmariumCategory.DELIVERED.value
    ]


def _source_regions(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    regions: dict[str, dict[str, Any]] = {}
    for act in acts:
        for region in act.get("source_regions", []):
            if not isinstance(region, dict) or not isinstance(region.get("region_id"), str):
                raise SchemaRefusal("an exported source region has no identity")
            region_id = region["region_id"]
            previous = regions.get(region_id)
            if previous is not None and previous != region:
                raise SchemaRefusal("one source-region identity carries conflicting provenance")
            regions[region_id] = region
    return [regions[region_id] for region_id in sorted(regions)]


def _salvage_regions(items: tuple[dict[str, Any], ...] | None) -> list[dict[str, Any]]:
    """Flatten cited salvage regions with their tier identity, never an act id."""
    if items is None:
        return []
    regions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(items, key=lambda record: record["salvage_id"]):
        for region in item["source_regions"]:
            key = (item["salvage_id"], region["region_id"])
            if key in seen:
                raise SchemaRefusal("a salvage-tier source region repeats its tier identity")
            seen.add(key)
            regions.append({"salvage_id": item["salvage_id"], **region})
    return sorted(regions, key=lambda record: (record["salvage_id"], record["region_id"]))


def _verify_reference(reference: Any, root) -> None:
    if not isinstance(reference, dict):
        raise SchemaRefusal("a package source reference is not an object")
    availability, sha256 = reference.get("availability"), reference.get("sha256")
    _require_sha256(sha256, "a package source reference digest")
    if availability == _EMBEDDED:
        member = reference.get("member_path")
        if not isinstance(member, str):
            raise SchemaRefusal("an embedded package source reference has no member path")
        _validate_member_name(member)
        path = root / member
        if not path.is_file():
            raise SchemaRefusal("an embedded package source reference does not resolve")
        content = path.read_bytes()
        if digest_bytes(content) != sha256:
            raise SchemaRefusal("an embedded package source reference does not resolve")
        try:
            dimensions(content)
        except ValueError as error:
            raise SchemaRefusal("an embedded package source pixel does not open") from error
    elif availability == _SOURCE_ACCESS_REQUIRED:
        # The digest was already required above, for every availability. Only the
        # path is this branch's own question.
        _validate_run_relative_path(reference.get("run_relative_path"))
    else:
        raise SchemaRefusal("a package source reference has no honest availability status")


_MAXIMUM_MEMBER_DEPTH: Final = 32


def _validate_member_name(name: str) -> None:
    subject = f"package member path {name!r}" if isinstance(name, str) else "a package member path"
    if isinstance(name, str) and name.endswith("/"):
        raise SchemaRefusal(f"{subject} is unsafe")
    path = _reject_unsafe_relative_path(name, subject=subject)
    # Extraction builds parents iteratively, but `_ordinary_member_names` walks
    # the result with a recursive helper, so a deep enough member reached
    # `RecursionError` -- which nothing converts, making a traceback the answer
    # to a hostile archive instead of a named refusal. Real packages nest a few
    # levels; this is far above them and far below the interpreter's limit.
    if len(path.parts) > _MAXIMUM_MEMBER_DEPTH:
        raise SchemaRefusal(
            f"{subject} nests {len(path.parts)} levels deep, past the "
            f"{_MAXIMUM_MEMBER_DEPTH}-component package member bound"
        )
    if path.as_posix() != name:
        # `PurePosixPath` removes `.` components and repeated separators. Distinct
        # archive spellings that normalize to one filesystem path would otherwise
        # replace one another during extraction.
        raise SchemaRefusal(f"{subject} is not in canonical POSIX spelling")


def _portable_member_key(name: str) -> str:
    """Approximate the case/normalization identity used by default APFS."""
    return unicodedata.normalize("NFD", name).casefold()


def _validate_archive_member_names(names: list[str]) -> None:
    """Refuse names that alias or shadow one another on recipient filesystems."""
    if len(set(names)) != len(names):
        raise SchemaRefusal("an Armarium package repeats a member name")
    keyed: dict[str, str] = {}
    for name in names:
        _validate_member_name(name)
        key = _portable_member_key(name)
        previous = keyed.get(key)
        if previous is not None:
            raise SchemaRefusal(
                f"package members {previous!r} and {name!r} collide after filesystem "
                "case and Unicode normalization"
            )
        keyed[key] = name

    ancestor_keys = {
        _portable_member_key(parent.as_posix())
        for name in names
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    shadowed = sorted(name for name in names if _portable_member_key(name) in ancestor_keys)
    if shadowed:
        raise SchemaRefusal(
            f"package member(s) {shadowed} are named as both a file and a directory"
        )


def _validate_run_relative_path(path: object) -> None:
    _reject_unsafe_relative_path(path, subject="a source reference into the retained run tree")
