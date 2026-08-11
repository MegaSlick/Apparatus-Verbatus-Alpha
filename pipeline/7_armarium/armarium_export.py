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
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Final
from zipfile import ZIP_STORED, BadZipFile, LargeZipFile, ZipFile, ZipInfo

from display import DISPLAY_CONVENTION, render_display, strip_display
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import ArmariumFormats, armarium_formats_from_record
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
    ArmariumCategory,
    require_approval,
    run_aggregate,
)
from common.contracts.stages import ARMARIUM
from common.imaging import dimensions

EXPORT_MANIFEST_NAME: Final = "EXPORT_MANIFEST.json"
# The one name for the published archive, shared by run.py (which records it in the
# export payload) and bundle.py (which writes the file under it), so the two cannot
# drift into naming two different files the same product.
ARMARIUM_ARCHIVE_NAME: Final = "armarium-export.zip"
EXPORT_MANIFEST_SCHEMA: Final = "armarium-export-manifest.v1"
ACT_RECORD_SCHEMA: Final = "armarium-act.v1"
SOURCES_SCHEMA: Final = "armarium-sources.v1"
SALVAGE_RECORD_SCHEMA: Final = "armarium-salvage-item.v1"
CANONICAL_TEXT_FIELD: Final = "canonical_clean_text"
CANONICAL_TEXT_ENCODING: Final = "utf-8"
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_SOURCE_ACCESS_REQUIRED: Final = "requires-source-access"
_RUN_ACCESS_REQUIRED: Final = "requires-retained-run-access"
_EMBEDDED: Final = "embedded"
_UNAVAILABLE_UNCERTAINTY: Final = "not-available-in-archetypus-contract"
_ANNOTATION_NOT_PRODUCED: Final = "not-produced-pending-architecture-approval"
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


@dataclass(frozen=True)
class ArmariumBundle:
    """The bytes and manifest facts that the stage seals as one blob."""

    data: bytes
    manifest: dict[str, Any]


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
        verify_export_bundle(data, clean_root)
        literal_formats = set(_LITERAL_TEXT_FORMATS) & set(formats.formats)
        if len(literal_formats) >= 2:
            verify_projection_identity(data, clean_root / "identity")
    return ArmariumBundle(data=data, manifest=manifest)


def verify_export_bundle(data: bytes, clean_root) -> dict[str, Any]:
    """Extract and independently verify a package without its source run tree.

    With pixels embedded this opens their packaged bytes.  With embedding off it
    verifies only the declared run-relative reference shape and explicitly does
    not claim that pixels can resolve on the clean machine.
    """
    root = clean_root
    root.mkdir(parents=True, exist_ok=True)
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
        if len(set(names)) != len(names):
            raise SchemaRefusal("an Armarium package repeats a member name")
        for name in names:
            _validate_member_name(name)
        # Names are safe one at a time and still unextractable together: `a` beside
        # `a/b` surfaces only as a `NotADirectoryError`, *after* part of the package
        # has been written to the clean machine.
        ancestors = {
            parent.as_posix()
            for name in names
            for parent in PurePosixPath(name).parents
            if parent.as_posix() != "."
        }
        shadowed = sorted(set(names) & ancestors)
        if shadowed:
            raise SchemaRefusal(
                f"package member(s) {shadowed} are named as both a file and a directory"
            )
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
        archive.extractall(root)

    try:
        manifest = json.loads((root / EXPORT_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal("EXPORT_MANIFEST.json is not readable canonical JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != EXPORT_MANIFEST_SCHEMA:
        raise SchemaRefusal("the package has no recognized EXPORT_MANIFEST schema")
    if manifest.get("self_hash") != self_hash(manifest):
        raise SchemaRefusal("EXPORT_MANIFEST.json fails its self-hash")
    run = manifest.get("run")
    if not isinstance(run, dict):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no run binding")
    _require_sha256(run.get("config_digest"), "EXPORT_MANIFEST.json run configuration digest")

    listed = manifest.get("members")
    if not isinstance(listed, list) or not listed:
        raise SchemaRefusal("EXPORT_MANIFEST.json has no member digest inventory")
    actual_names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    expected_names = {EXPORT_MANIFEST_NAME}
    listed_names: set[str] = set()
    for item in listed:
        if not isinstance(item, dict):
            raise SchemaRefusal("a manifest member inventory row is not an object")
        name, sha256, byte_count = item.get("path"), item.get("sha256"), item.get("bytes")
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
    _verify_annotations_claim(manifest)
    _verify_exact_product_members(formats, sources, actual_names)
    _verify_product_accounting(root, manifest, formats, sources)
    return manifest


def verify_projection_identity(data: bytes, clean_root) -> dict[str, str]:
    """Prove every selected literal-text format carries identical clean text.

    This is intentionally distinct from package-digest verification.  A package
    can be internally self-consistent while one product writer has transformed
    the literal differently; this compares the values and their UTF-8 hashes
    across the text bundle, SQLite literal column, and JSONL hand-off.
    """
    root = clean_root
    manifest = verify_export_bundle(data, root)
    formats = _manifest_formats(manifest)
    projections: dict[str, dict[str, tuple[str, str]]] = {}
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
                f"canonical clean-text projection differs between {baseline_name} and {name}"
            )
    return {act_id: text for act_id, (text, _digest) in baseline.items()}


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
        elif literal is not None:
            raise SchemaRefusal("a non-delivered act may not carry purported clean text")
        if category == ArmariumCategory.EXCLUDED_WITH_APPROVAL.value:
            require_approval(ARMARIUM, category, act.get("approval_ref"))
        _reject_act_salvage_namespace(act)
    expected_aggregate = _aggregate_from_basis(
        {act["act_key"]: act["category"] for act in projection.acts},
        projection.pages,
        projection.aggregate_basis,
    )
    if canonical_text(projection.aggregate) != canonical_text(expected_aggregate):
        raise SchemaRefusal("an Armarium projection aggregate does not match its measured basis")


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
) -> dict[str, Any]:
    """Recompute an Armarium aggregate from its retained, non-text inputs."""
    if not isinstance(basis, dict) or set(basis) != {
        "coverage_records",
        "unaddressed_chairs",
        "act_pages",
    }:
        raise SchemaRefusal("an Armarium aggregate has no recognized accounting basis")
    coverage, chairs, act_pages = (
        basis.get("coverage_records"),
        basis.get("unaddressed_chairs"),
        basis.get("act_pages"),
    )
    if (
        not isinstance(coverage, dict)
        or not isinstance(chairs, list)
        or not all(isinstance(chair, str) and chair for chair in chairs)
        or not isinstance(act_pages, dict)
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
    if isinstance(value, dict):
        forbidden = sorted(set(value) & _SALVAGE_RESERVED_FIELDS)
        if forbidden:
            raise SchemaRefusal(
                f"a salvage-tier {subject} reaches into the acts namespace through "
                f"reserved field(s) {forbidden}"
            )
        for item in value.values():
            _reject_salvage_act_namespace(item, subject=subject)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_salvage_act_namespace(item, subject=subject)


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
        # validated `declared_path`, so the folder is read rather than re-proved.
        folder = _source_folder_for_declared_path(act["source_regions"][0]["declared_path"])
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
            connection.execute("PRAGMA user_version=1")
            connection.executescript(
                """
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
                    uncertainty_spans_json TEXT,
                    uncertainty_status TEXT NOT NULL,
                    annotations_json TEXT NOT NULL,
                    annotation_status TEXT NOT NULL,
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
            )
            metadata = {
                "canonical_text_encoding": CANONICAL_TEXT_ENCODING,
                "canonical_text_field": CANONICAL_TEXT_FIELD,
                "normalizer_revision": TEXTNORM_REVISION,
                "schema": "armarium-acts-sqlite.v1",
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
                        uncertainty_spans_json, uncertainty_status, annotations_json,
                        annotation_status, evidence_json, approval_ref, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        act["act_id"],
                        act["act_key"],
                        act["category"],
                        literal,
                        text_hash,
                        canonical_text(act["provenance"]) if literal is not None else None,
                        canonical_text(act["source_regions"]) if literal is not None else None,
                        None,
                        _UNAVAILABLE_UNCERTAINTY,
                        "[]",
                        _ANNOTATION_NOT_PRODUCED,
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
                "uncertainty_spans": None,
                "uncertainty_status": _UNAVAILABLE_UNCERTAINTY,
                "annotations": [],
                "annotation_status": _ANNOTATION_NOT_PRODUCED,
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
    cut its own record in half in every line-oriented member. ``newline=""`` is the
    same principle: no universal-newline translation, because the writers join on
    ``\n`` and nothing else.
    """
    try:
        return path.read_text(encoding="utf-8", newline="").split("\n")
    except (OSError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"the {subject} cannot be read") from error


def _text_bundle_records(
    root, source_pages: list[dict[str, Any]] | None = None
) -> dict[str, tuple[str, str, tuple[tuple[str, str], ...]]]:
    """Parse literal records and their page/hash citations from the readable bundle."""
    known_pages: set[tuple[str, str]] | None = None
    if source_pages is not None:
        known_pages = set()
        for page in source_pages:
            if not isinstance(page, dict):
                raise SchemaRefusal("a text-bundle source citation has no page object")
            path, digest = page.get("declared_path"), page.get("declared_sha256")
            if not isinstance(path, str):
                raise SchemaRefusal("a text-bundle source citation has no declared path")
            _require_sha256(digest, "a text-bundle source citation digest")
            known_pages.add((path, digest))

    records: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {}
    for path in sorted((root / "text").rglob("readings.txt")) if (root / "text").exists() else []:
        lines = _package_lines(path, "text bundle")
        current_id: str | None = None
        pending: tuple[str, str, tuple[tuple[str, str], ...]] | None = None
        citations: list[tuple[str, str]] = []
        for index, line in enumerate(lines):
            if line.startswith("act-id: "):
                if current_id is not None:
                    raise SchemaRefusal("a text-bundle section has no completed literal record")
                current_id = line.removeprefix("act-id: ")
                if not current_id:
                    raise SchemaRefusal("a text-bundle section has an empty act identity")
                citations = []
                pending = None
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
                if (
                    not citations
                    or digest != canonical_text_sha256(literal)
                    or current_id in records
                ):
                    raise SchemaRefusal("a text-bundle literal identity or hash is invalid")
                pending = (literal, digest, tuple(citations))
            elif line == "display:":
                # Spec 11 test 2's second half, checked on the written product:
                # render -> strip -> hash. The rendered display is a reading aid and
                # stripping it must return the canonical field exactly, so a display
                # convention can never become characters in the hashed text.
                if current_id is None or pending is None or index + 1 >= len(lines):
                    raise SchemaRefusal("a text-bundle display has no literal to render")
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
                records[current_id] = pending
                current_id, pending = None, None
        if current_id is not None:
            raise SchemaRefusal("a text-bundle section has no completed literal record")
    return records


def _text_bundle_literals(root) -> dict[str, tuple[str, str]]:
    return {
        act_id: (literal, digest)
        for act_id, (literal, digest, _citations) in _text_bundle_records(root).items()
    }


_STORED_ACTS_TABLES: Final = ("acts", "act_search")


def _open_acts_database(path) -> sqlite3.Connection:
    """Open a package's acts database read-only, as stored rows and not as a program.

    **A table is not a view.**  Every read below names ``acts`` or ``act_search``, and
    SQLite is perfectly happy for either to be a *view* -- which is a program.  A view
    over a recursive CTE turns a few kilobytes of package member into an unbounded
    result set: built one, and watched this function's caller allocate until the kernel
    killed the process.  Same amplification the ``ZIP_STORED`` check refuses in the
    archive reader, and closed the same way -- a stored table's row count is bounded by
    the member's own physical bytes, a view's by nothing.

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
        kinds = dict(
            connection.execute(
                "SELECT name, type FROM sqlite_master WHERE name IN (?, ?)",
                _STORED_ACTS_TABLES,
            ).fetchall()
        )
    except sqlite3.DatabaseError as error:
        connection.close()
        raise SchemaRefusal("the acts database has no readable schema") from error
    if any(kinds.get(name) != "table" for name in _STORED_ACTS_TABLES):
        connection.close()
        raise SchemaRefusal("the acts database does not carry acts and act_search as stored tables")
    return connection


def _database_literals(path) -> dict[str, tuple[str, str]]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        rows = connection.execute(
            """
            SELECT act_id, canonical_clean_text, canonical_text_sha256
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
    records: dict[str, tuple[str, str]] = {}
    for act_id, literal, digest in rows:
        if (
            not isinstance(act_id, str)
            or not isinstance(literal, str)
            or not isinstance(digest, str)
        ):
            raise SchemaRefusal("the acts database has an untyped literal row")
        if digest != canonical_text_sha256(literal) or act_id in records:
            raise SchemaRefusal("the acts database literal identity or hash is invalid")
        records[act_id] = (literal, digest)
    return records


def _jsonl_literals(path) -> dict[str, tuple[str, str]]:
    records: dict[str, tuple[str, str]] = {}
    for line in _package_lines(path, "acts JSONL"):
        if not line:
            continue
        record = json.loads(line)
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
        records[act_id] = (literal, digest)
    return records


def _page_ledger_category(ordinal: int, act_categories: list[str]) -> tuple[str, str | None]:
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
            category, reason = _page_ledger_category(ordinal, acts_on_page.get(ordinal, []))
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
            "promotion": "recorded approval then pipeline re-entry; never export-time act promotion",
        }
        if projection.salvage_items is not None
        else {
            "namespace": "salvage",
            "status": "not-produced-no-sealed-salvage-inventory",
            "count": None,
            "reason": "this run has no sealed salvage inventory to account for",
            "promotion": "recorded approval then pipeline re-entry; never export-time act promotion",
        }
    )
    ledger = _terminal_ledger(
        _act_outcomes(projection.acts),
        list(projection.pages),
        projection.aggregate_basis.get("act_pages")
        if isinstance(projection.aggregate_basis, dict)
        else None,
        projection.aggregate,
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
            "act_partition": {
                "denominator": "proposal-seal expected acts",
                "expected_count": projection.expected_acts,
                "counted": len(projection.acts),
                "reconciles": len(projection.acts) == projection.expected_acts,
                "categories": categories,
                "act_keys": {
                    act["act_id"]: act["act_key"]
                    for act in sorted(projection.acts, key=lambda item: item["act_id"])
                },
            },
            "submission_inventory": {
                "status": "reconciled-at-source-page-ordinal-granularity",
                "granularity": _SOURCE_GRANULARITY,
                "limit": _CONTAINER_GRANULARITY_LIMIT,
                "observed_source_page_rows": len(projection.source_manifest),
                "observed_distinct_declared_paths": len(submission_paths),
            },
            "page_census": {
                "denominator": "run.json source-page/frame rows",
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
            "annotations": {
                "status": "not-produced-pending-architecture-approval",
                "text_writable": False,
            },
            # Labelled a proposal because it is one: spec 11 leaves the choice of
            # convention to Tyrel at this gate, and nothing hashed depends on it.
            "display": {
                "convention": DISPLAY_CONVENTION,
                "status": "proposed-pending-tyrels-choice",
                "alters_stored_text": False,
                "exercised_against_real_spans": False,
                "reason": (
                    "the Archetypus record carries no uncertainty or gap layer yet, so "
                    "every rendering in this build is the established text unchanged"
                ),
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
        for name in names:
            _validate_member_name(name)
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
        }:
            raise SchemaRefusal("a source act-outcome record has an unrecognized field set")
        act_id, act_key, category, reason = (
            record.get("act_id"),
            record.get("act_key"),
            record.get("category"),
            record.get("reason"),
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
        _verify_retained_references(record)
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
        records[act_id] = {
            "act_key": act_key,
            "category": category,
            "evidence": _act_evidence(record),
            "provenance": record.get("provenance"),
            "source_regions": record.get("source_regions"),
            "reason": reason,
        }
    return records


def _database_act_records(
    path: Path, source_graph_regions: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Validate the SQLite one-record-per-act projection and return categories."""
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        rows = connection.execute(
            "SELECT act_id, act_key, category, canonical_clean_text, canonical_text_sha256, "
            "provenance_json, source_regions_json, evidence_json, reason FROM acts"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal("the acts database cannot be read for product accounting") from error
    finally:
        if connection is not None:
            connection.close()
    records: dict[str, dict[str, Any]] = {}
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
        if category == ArmariumCategory.DELIVERED.value:
            _verify_delivered_product_provenance(
                decoded[0], decoded[1], source_graph_regions, subject="acts database"
            )
        records[act_id] = {
            "act_key": act_key,
            "category": category,
            "evidence": decoded[2],
            "provenance": decoded[0],
            "source_regions": decoded[1],
            "reason": reason,
        }
    return records


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
        if (
            record.get("schema") != "armarium-review-item.v1"
            or not isinstance(act_id, str)
            or not act_id
            or not isinstance(act_key, str)
            or not act_key
            or category not in allowed
            or not isinstance(reason, str)
            or not reason
            or act_id in records
        ):
            raise SchemaRefusal("a review-items JSONL row has an invalid act identity or category")
        records[act_id] = {"act_key": act_key, "category": category, "reason": reason}
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
    if status == "accounted":
        if not isinstance(count, int) or isinstance(count, bool) or count != len(records):
            raise SchemaRefusal("the salvage-tier count does not reconcile to its records")
    elif status == "not-produced-no-sealed-salvage-inventory":
        if count is not None or records or sources["salvage_regions"]:
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


def _verify_search_fold_claim(path: Path) -> None:
    """Recompute the derived search column from its own act's literal.

    A digest-checked SQLite member proves the package was not edited after
    sealing; it proves nothing about whether ``act_search.derived_search_text``
    was ever actually a fold of its own act's canonical clean text -- a build
    defect or a package rebuilt around a tampered column would pass every
    other check in this file with the search projection carrying unrelated
    text. Unlike ``claims.canonical_text``/``claims.annotations`` above, this
    one *does* have a source graph to recompute against: the literal each
    row's own ``act_id`` already carries in ``acts``.
    """
    literals = _database_literals(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        rows = connection.execute(
            "SELECT act_id, derived_search_text, derived_text_sha256, "
            "derived_from_canonical_sha256, normalizer_revision, derived_kind FROM act_search"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal(
            "the acts database search projection cannot be read for verification"
        ) from error
    finally:
        if connection is not None:
            connection.close()
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
        if (
            derived != search_fold(literal)
            or derived_hash != canonical_text_sha256(derived)
            or source_hash != literal_hash
        ):
            raise SchemaRefusal(
                "the acts database search projection is not a fold of its act's literal"
            )
    if seen != set(literals):
        raise SchemaRefusal(
            "the acts database search projection does not cover exactly the delivered literals"
        )


def _verify_product_accounting(
    root: Path,
    manifest: dict[str, Any],
    formats: ArmariumFormats,
    sources: dict[str, list[dict[str, Any]]],
) -> None:
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
        for act_id, (_literal, _digest, text_citations) in text_records.items():
            expected_citations = tuple(
                (region["declared_path"], region["declared_sha256"])
                for region in citations[act_id]["source_regions"]
            )
            if text_citations != expected_citations:
                raise SchemaRefusal(
                    "the text bundle does not retain every delivered source citation"
                )
    if "acts-database" in formats.formats:
        database_records = _database_act_records(root / "acts.sqlite", sources["regions"])
        if _product_categories(database_records) != expected:
            raise SchemaRefusal(
                "the acts database does not reconcile to the manifest act partition"
            )
        _verify_exact_product_outcomes(database_records, outcomes, subject="acts database")
        _verify_exact_delivered_citations(
            database_records, citations, act_keys, subject="acts database"
        )
        _verify_search_fold_claim(root / "acts.sqlite")
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
        or display.get("exercised_against_real_spans") is not False
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

    Unlike every other ``claims.*`` section, this one and ``claims.annotations``
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


def _verify_annotations_claim(manifest: dict[str, Any]) -> None:
    """No build in this repository can produce a text-writable annotation layer.

    The annotator is unbuilt and unwired (HANDOFF.md), so this claim is a fixed
    constant today, not a measurement -- but a tampered manifest claiming
    ``text_writable: true`` must still be refused here rather than accepted
    because nothing yet exists to disprove it.
    """
    claims = manifest.get("claims")
    annotations = claims.get("annotations") if isinstance(claims, dict) else None
    if annotations != {"status": _ANNOTATION_NOT_PRODUCED, "text_writable": False}:
        raise SchemaRefusal("the package annotations claim is not this build's fixed claim")


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
        if page.get("outcome") not in {"sealed", "refused"}:
            raise SchemaRefusal("a package source row has no recognized Exemplar outcome")
        declared_path, declared_sha256 = page.get("declared_path"), page.get("declared_sha256")
        if not isinstance(declared_path, str):
            raise SchemaRefusal("a package source row has no declared path")
        _source_folder_for_declared_path(declared_path)
        _require_sha256(declared_sha256, "a package source declared digest")
        if "ledger_sha256" in page:
            _require_sha256(page["ledger_sha256"], "a package source ledger digest")
        reference = page.get("page_image")
        if page.get("outcome") == "sealed" and reference is None:
            raise SchemaRefusal("a sealed package source page has no pixel reference")
        if page.get("outcome") != "sealed" and reference is not None:
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
    """Keep the non-text terminal reason that review-items must reproduce exactly."""
    return [
        {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "category": act["category"],
            "reason": _export_reason(act),
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


def _validate_member_name(name: str) -> None:
    subject = f"package member path {name!r}" if isinstance(name, str) else "a package member path"
    if isinstance(name, str) and name.endswith("/"):
        raise SchemaRefusal(f"{subject} is unsafe")
    _reject_unsafe_relative_path(name, subject=subject)


def _validate_run_relative_path(path: object) -> None:
    _reject_unsafe_relative_path(path, subject="a source reference into the retained run tree")
