"""Deterministic, read-only Armarium product projections.

The only source of delivered act text is ``canonical_clean_text`` from the checked
Archetypus record; every writer receives it from one projection object and none reads
a witness, Perlectio or other text-shaped field.

``EXPORT_MANIFEST.json`` is always the first member. The ZIP is stored with fixed
metadata, so the container adds no nondeterminism. The bytes are still not
identical across SQLite builds: bytes 96-99 of a SQLite file hold the writing
library's ``SQLITE_VERSION_NUMBER``
(https://www.sqlite.org/fileformat.html#the_database_header), so the bundle's
content address binds the toolchain as well as the data.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from fractions import Fraction
from functools import lru_cache
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Final, NamedTuple
from zipfile import ZIP_STORED, BadZipFile, LargeZipFile, ZipFile, ZipInfo

from coniector_layer import (
    CONIECTOR_MEMBER,
    anchor_act,
    join_section,
    reconstruction_lines,
    text_bundle_placements,
    verify_row,
)
from flagged_layer import FLAGGED_MEMBER
from flagged_layer import SOURCES_FIELD as FLAGGED_SOURCES_FIELD
from flagged_layer import verify_rows as verify_flagged_rows
from operator_layer import LABEL_LINE as OPERATOR_LABEL_LINE
from operator_layer import (
    MODEL_READING_SCHEMA,
    MODEL_READINGS_FIELD,
    MODEL_READINGS_MEMBER,
    MODEL_ROW_FIELDS,
    OPERATOR_MEMBER,
    READING_HOLDS_FIELD,
    block_end,
    lines_for,
    model_reading_record,
    text_bundle_rows,
    verify_model_reading,
    verify_rows,
)
from operator_layer import SOURCES_FIELD as OPERATOR_SOURCES_FIELD
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import (
    ArmariumFormats,
    armarium_formats_from_record,
    require_within_export_archive_limit,
)
from common.calibration import calibrated_claim_has_sample_evidence
from common.contracts.canonical import (
    canonical_bytes,
    canonical_text,
    digest_bytes,
    is_plain_int,
    is_sha256,
    self_hash,
)
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import is_lot
from common.contracts.outcomes import (
    CONFIRMED_NO_ACT_PAGE_REASON,
    CONTINUATION_FLAGS,
    NO_ACT_PAGE_HELD_REASON,
    PAGE_READ_SILENT_PAGE_REASON,
    TEXT_STATUSES,
    ArmariumCategory,
    derive_record_text_status,
    edge_hold_reason,
    require_approval,
    run_aggregate,
    unresolved_act_reason,
    unsealed_page_reason,
)
from common.contracts.stages import ARMARIUM
from common.contracts.uncertainty import CORRECTED_LECTIO, corrected_layer, utf8_round_trip
from common.contracts.uncertainty import validate as validate_uncertainty
from common.correction import (
    CORRECTED_LABEL,
    edit_digests,
    is_correction,
)
from common.correction import (
    DECISION_FIELDS as CORRECTION_DECISION_FIELDS,
)
from common.correction import (
    PROVENANCE_FIELDS as CORRECTION_PROVENANCE_FIELDS,
)
from common.imaging import dimensions
from common.reading_annotations import diplomatic_display, doubt_count
from common.residual_ink import INK_NOT_MEASURABLE, coverage_flag
from common.review_policy import parse_share

_atomic_replace = os.replace
_unlink_at = os.unlink

EXPORT_MANIFEST_NAME: Final = "EXPORT_MANIFEST.json"
# Shared by run.py and bundle.py so the recorded name and the written file agree.
ARMARIUM_ARCHIVE_NAME: Final = "armarium-export.zip"
# Every change to the manifest's closed shape moves the id, so an older reader
# refuses a new field instead of silently presenting a bundle without it. The
# manifest counts the readings the Perlector established on each page it read
# whole (`common.stage.reading_acts`), and carries its `other` readings, a
# labelled layer beside the acts, each page's accounting, and its acts counted by
# the reading they came from (`reask`).
EXPORT_MANIFEST_SCHEMA: Final = "armarium-export-manifest.v13"
# The act row and SQLite ids move with the row shape, so a consumer keying on the
# id never reads an old shape out of a new row.
ACT_RECORD_SCHEMA: Final = "armarium-act.v7"
_ACT_RECORD_FIELDS: Final = frozenset(
    {
        "schema",
        "act_id",
        "act_key",
        "lot",
        "category",
        "canonical_clean_text",
        "canonical_text_sha256",
        "provenance",
        "source_regions",
        "uncertainty",
        "uncertainty_status",
        "text_status",
        "witnesses",
        "perlectio_ref",
        "recensor_ref",
        "dissent_ref",
        "approval_ref",
        "reason",
        "evidence_refs",
        # The reading the act came from (`FIRST_READING_LABEL`, `READ_ON_REASK_LABEL`,
        # `OPERATOR_REREAD_LABEL`).
        "reading",
    }
)
# Every held or refused reading, act or other, named by `kind`; the act partition
# is `acts.jsonl`'s row count, never this file's.
REVIEW_ITEM_SCHEMA: Final = "armarium-review-item.v3"
_REVIEW_ITEM_FIELDS: Final = frozenset(
    {"schema", "act_id", "act_key", "lot", "kind", "category", "reason", "evidence_refs"}
)
_SQLITE_SCHEMA: Final = "armarium-acts-sqlite.v7"
_SQLITE_USER_VERSION: Final = 7
# The reading an act came from: its page's first reading, the one
# re-ask of its page, or an operator re-read a person's page re-ask asked for,
# which superseded the page's earlier readings. A row standing for a page with
# no entry names none.
FIRST_READING_LABEL: Final = "first reading"
READ_ON_REASK_LABEL: Final = "read on re-ask"
OPERATOR_REREAD_LABEL: Final = "read on operator re-read"
_ACT_READINGS: Final = (FIRST_READING_LABEL, READ_ON_REASK_LABEL, OPERATOR_REREAD_LABEL)
_ACT_READING_FIELDS: Final = frozenset({"act_id", "act_key", "page_ordinal", "reading"})
# A counted page-read act's key, `p<page>:<entry n>`, or a page row with no entry.
_PAGE_ACT_KEY: Final = re.compile(r"p([1-9][0-9]*):(?:([1-9][0-9]*)|unread|blank)")
# Field sets are checked exactly, so each shape change needs a new id.
SOURCES_SCHEMA: Final = "armarium-sources.v6"
# The `other` readings of a page-read run travel in their own member, never in
# `acts.jsonl`: that file is one row per counted act, and its row count is the act
# partition a consumer reconciles against.
OTHER_READING_SCHEMA: Final = "armarium-other-reading.v3"
OTHER_READINGS_MEMBER: Final = "other.jsonl"
_OTHER_READING_FIELDS: Final = frozenset(
    {
        "schema",
        "act_id",
        "act_key",
        "lot",
        "kind",
        "page_ordinal",
        "category",
        "canonical_clean_text",
        "canonical_text_sha256",
        "text_status",
        "uncertainty",
        "provenance",
        "source_regions",
        "witnesses",
        "perlectio_ref",
        "recensor_ref",
        "dissent_ref",
        "evidence_refs",
        "reason",
    }
)
_OTHER_OUTCOME_FIELDS: Final = frozenset(
    {"act_id", "act_key", "page_ordinal", "category", "reason", "text_status"}
)
# The formats that carry the other layer. The acts database's tables are the act
# partition and its search layer; it carries no other reading.
_OTHER_READING_FORMATS: Final = ("jsonl", "text-bundle")
_OTHER_READINGS_LAYER: Final = "other readings, not acts"
_OTHER_CATEGORIES: Final = frozenset(
    {
        ArmariumCategory.DELIVERED.value,
        ArmariumCategory.HELD_FOR_REVIEW.value,
        ArmariumCategory.REFUSED_WITH_REASON.value,
    }
)
_PAGE_ACCOUNTING_DENOMINATOR: Final = "every real sealed page, read whole"
_PAGE_ACCOUNTING_ROW_FIELDS: Final = frozenset(
    {"ordinal", "page_id", "rules", "hold_codes", "policy_sha256", "accounting_ref"}
)
# Code never joins text across a page break: a join row records only that an
# act may cross it. The Coniector alone reconstructs across one, and only on a
# run sealed `pages_are_consecutive` (`coniector_layer`).
JOIN_RULE: Final = "verbatus-page-join.v3"
_JOIN_FIELDS: Final = frozenset(
    {
        "join_id",
        "candidate_ref",
        "head_page_ordinal",
        "tail_page_ordinal",
        "head_act_ids",
        "tail_act_ids",
        "status",
        "not_reconstructed_reason",
        "head_canonical_text_sha256",
        "tail_canonical_text_sha256",
        "join_rule",
        "authoritative",
    }
)
CANONICAL_TEXT_FIELD: Final = "canonical_clean_text"
CANONICAL_TEXT_ENCODING: Final = "utf-8"
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_SOURCE_ACCESS_REQUIRED: Final = "requires-source-access"
_RUN_ACCESS_REQUIRED: Final = "requires-retained-run-access"
_EMBEDDED: Final = "embedded"
_UNCERTAINTY_AVAILABLE: Final = "canonical-unicode-codepoint-offsets"
# An act with no established text, or a package with no literal-text format, has
# no offsets to anchor to.
_UNCERTAINTY_NOT_APPLICABLE: Final = "not-applicable"
_LITERAL_TEXT_FORMATS: Final = ("text-bundle", "acts-database", "jsonl", "csv")
# The flat projection: one row per act, UTF-8 with a byte-order mark so a
# spreadsheet reads the accents, CRLF rows as RFC 4180 writes them.
CSV_MEMBER: Final = "acts.csv"
_CSV_BOM: Final = b"\xef\xbb\xbf"
_CSV_COLUMNS: Final = (
    "act_key",
    "act_id",
    "lot",
    "category",
    "reason",
    "reading",
    "text_status",
    "canonical_clean_text",
    "diplomatic_text",
    "canonical_text_sha256",
    "uncertainty_json",
    "doubtful_or_unread",
    "out_of",
)
# A spreadsheet runs a cell starting with one of these as a formula. Such a cell,
# and one already starting with the escape, is written with one leading `'`,
# which the reader removes, so every other format keeps the reading unchanged.
_CSV_FORMULA_STARTS: Final = ("=", "+", "-", "@", "\t", "\r")
_CSV_ESCAPE: Final = "'"
# Reading keys are `p<page>:<n>` with unpadded ordinals, so a string sort puts
# page 10 before page 2. A page's row with no reading (`p<page>:blank`) follows
# its numbered readings; any other key sorts as a string after every page.
_READING_ACT_KEY_PATTERN: Final = re.compile(r"^p(\d+):(?:(\d+)|([a-z]+))$")


def act_key_sort_key(act_key: str) -> tuple:
    """Reading order for an act key: page ordinal, then reading ordinal."""
    match = _READING_ACT_KEY_PATTERN.match(act_key)
    if match is None:
        return (1, act_key)
    page, ordinal, word = match.groups()
    if ordinal is not None:
        return (0, int(page), 0, int(ordinal))
    return (0, int(page), 1, word)


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
# run.json binds one ordinal per page, not per file, so the ledger cannot count
# submitted files; the bundle states that gap.
_CONTAINER_GRANULARITY_LIMIT: Final = (
    "a multi-page PDF/TIFF container is represented by one unit per page or frame rather "
    "than one unit for the submitted file; the file's own single terminal category is not "
    "represented and cannot be counted off this ledger"
)
_ACT_PARTITION_DENOMINATOR: Final = "page-read reading acts"
_PAGE_CENSUS_DENOMINATOR: Final = "run.json source-page/frame rows"
_COMPLETED_CATEGORIES: Final = frozenset(
    {
        ArmariumCategory.DELIVERED.value,
        ArmariumCategory.EXCLUDED_WITH_APPROVAL.value,
        ArmariumCategory.CONFIRMED_BLANK.value,
    }
)
_KNOWN_CATEGORIES: Final = frozenset(category.value for category in ArmariumCategory)
_REVIEW_CATEGORIES: Final = frozenset(
    {ArmariumCategory.HELD_FOR_REVIEW.value, ArmariumCategory.REFUSED_WITH_REASON.value}
)


@dataclass(frozen=True)
class ArmariumProjection:
    """The one checked record every product writer is allowed to see.

    Only delivered acts have a literal ``canonical_clean_text``; every other act
    carries ``None`` rather than an invented empty reading.
    """

    # Exactly one of ``fixture_id``/``submission_id`` is set, so a real corpus
    # never travels under a field that reads as a fixture run.
    fixture_id: str | None
    scenario: str
    config_digest: str
    aggregate: dict[str, Any]
    acts: tuple[dict[str, Any], ...]
    pages: tuple[dict[str, Any], ...]
    source_manifest: tuple[dict[str, Any], ...]
    expected_acts: int
    witness_chairs: tuple[str, ...]
    witness_floor: int
    # Carried so a verifier can recompute ``aggregate`` rather than trust it.
    aggregate_basis: dict[str, Any]
    # The filename ledger's self-hash (``common.stage.submission_identity``).
    submission_id: str | None = None
    # The held set is derived from these rows, never stored beside them.
    ink_map_pages: tuple[dict[str, Any], ...] = ()
    # `None` means the basis is missing, not that everything was measured;
    # `_validate_projection` refuses it.
    not_measured_basis: dict[str, Any] | None = None
    continuation_joins: tuple[dict[str, Any], ...] = ()
    # The `other` readings are a separate layer, shaped as acts plus
    # `page_ordinal`, never in `acts`; the page accounting rows are text-free,
    # one per real sealed page.
    other_readings: tuple[dict[str, Any], ...] = ()
    page_accounting: tuple[dict[str, Any], ...] = ()
    # The Coniector's reconstructions beneath delivered acts
    # (`coniector_layer.export_rows`): labelled, unconfirmed, never acts.
    reconstructions: tuple[dict[str, Any], ...] = ()
    # The operator layer (`operator_layer.released_row`): each delivered
    # reading a person's decision released, labelled, with who, when and why.
    operator_actions: tuple[dict[str, Any], ...] = ()
    # Each delivered reading's own hold codes by id, empty for one that carried
    # none; `None` exactly when the run has no review decisions, since only a
    # decision can deliver a held reading.
    reading_hold_codes: dict[str, list[str]] | None = None
    # The model's reading of each delivered reading a person corrected
    # (`run.model_reading_row`), shown beside the person's text.
    model_readings: tuple[dict[str, Any], ...] = ()
    # The flagged layer (`flagged_layer.flagged_row`): every counted reading the
    # Recensor held or flagged, with its text labelled, beside the strict export.
    flagged_readings: tuple[dict[str, Any], ...] = ()
    # The run's lot (`identities.lot_id`), set exactly when the sealed formats say
    # `lot = true`; every row and the manifest's `run` block carry it.
    lot: str | None = None


@dataclass(frozen=True)
class ArmariumBundle:
    """The bytes and manifest facts that the stage seals as one blob."""

    data: bytes
    manifest: dict[str, Any]


def _literal_formats_in(formats: tuple[str, ...] | list[str]) -> list[str]:
    return sorted(set(formats) & set(_LITERAL_TEXT_FORMATS))


def _canonical_text_claim(formats: tuple[str, ...] | list[str]) -> dict[str, Any]:
    literal_formats = _literal_formats_in(formats)
    return {
        "authority": "archetypus",
        "field": CANONICAL_TEXT_FIELD,
        "hash": "sha256-utf-8",
        "derived_columns_are_marked": True,
        # Empty when fewer than two literal formats leave nothing to compare.
        "identity_verified_across": literal_formats if len(literal_formats) >= 2 else [],
    }


def _uncertainty_claim(formats: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Measure which selected formats carry canonical uncertainty."""
    carried_by = _literal_formats_in(formats)
    return {
        "status": _UNCERTAINTY_AVAILABLE if carried_by else _UNCERTAINTY_NOT_APPLICABLE,
        "offset_unit": "unicode-code-point",
        "carried_by": carried_by,
    }


def canonical_text_sha256(text: str) -> str:
    """Hash one literal clean-text value exactly as every format encodes it."""
    if not isinstance(text, str):
        raise SchemaRefusal("a canonical clean-text hash requires one literal string")
    return digest_bytes(text.encode(CANONICAL_TEXT_ENCODING))


_FLAGS_DISAGREE: Final = "flags-disagree"
NO_CODE_JOIN: Final = "no-code-join"


def continuation_join_row(
    *,
    join_id: str,
    candidate_ref: dict[str, Any],
    head_page_ordinal: int,
    tail_page_ordinal: int,
    head_act_ids: list[str],
    tail_act_ids: list[str],
    delivered_texts: dict[str, str],
    selected_formats: tuple[str, ...] | list[str],
    flags_disagree: bool = False,
) -> dict[str, Any]:
    """One continuation candidate as a text-free join row over the delivered literals.

    Every row is `not-reconstructed`: code never joins the two sides' text. Its
    reason names what the row found; `no-code-join` is a break whose sides are
    each one delivered act. `flags_disagree` marks a page-read break whose two
    readings do not both say an act crosses it.
    """
    literal = bool(_literal_formats_in(selected_formats))

    def side_sha256(act_ids: list[str]) -> str | None:
        if literal and len(act_ids) == 1 and act_ids[0] in delivered_texts:
            return canonical_text_sha256(delivered_texts[act_ids[0]])
        return None

    if not head_act_ids or not tail_act_ids:
        reason = "side-names-no-act"
    elif flags_disagree:
        reason = _FLAGS_DISAGREE
    elif len(set(head_act_ids)) < len(head_act_ids) or len(set(tail_act_ids)) < len(tail_act_ids):
        reason = "act-named-twice-on-one-side"
    elif len(head_act_ids) > 1 or len(tail_act_ids) > 1:
        reason = "several-acts-on-a-side"
    elif head_act_ids[0] not in delivered_texts:
        reason = "head-not-delivered"
    elif tail_act_ids[0] not in delivered_texts:
        reason = "tail-not-delivered"
    else:
        reason = NO_CODE_JOIN
    return {
        "join_id": join_id,
        "candidate_ref": candidate_ref,
        "head_page_ordinal": head_page_ordinal,
        "tail_page_ordinal": tail_page_ordinal,
        "head_act_ids": list(head_act_ids),
        "tail_act_ids": list(tail_act_ids),
        "status": "not-reconstructed",
        "not_reconstructed_reason": reason,
        "head_canonical_text_sha256": side_sha256(head_act_ids),
        "tail_canonical_text_sha256": side_sha256(tail_act_ids),
        "join_rule": JOIN_RULE,
        "authoritative": False,
    }


def _join_notes(joins, act_keys: dict[str, str]) -> dict[str, list[str]]:
    """The mirrored note each side of a join carries in its own act section."""
    notes: dict[str, list[str]] = defaultdict(list)
    for join in joins:
        for head in join["head_act_ids"]:
            for tail in join["tail_act_ids"]:
                notes[head].append(
                    f"possible-continuation-on: {act_keys[tail]} "
                    f"(page {join['tail_page_ordinal']}) [{join['join_id']}]"
                )
                notes[tail].append(
                    f"possible-continuation-from: {act_keys[head]} "
                    f"(page {join['head_page_ordinal']}) [{join['join_id']}]"
                )
    return notes


def build_armarium_bundle(
    projection: ArmariumProjection,
    formats: ArmariumFormats,
    read_bytes: Callable[[str], bytes],
) -> ArmariumBundle:
    """Write the selected product formats from one validated projection.

    ``read_bytes`` is used only for image blobs already verified by Armarium's
    upstream boundary.  It is never used to discover or recover text.
    """
    ink_map_rows = _validate_projection(projection)
    _validate_projection_region_bindings(projection)
    _require_lot(projection.lot, formats.lot, subject="the projection")
    lot = projection.lot
    # Derived once and handed to every writer that states them.
    edge_hold_pages = _edge_hold_pages_from_validated_rows(ink_map_rows)
    other_outcomes = _other_outcomes(projection.other_readings)
    ledger = _terminal_ledger(
        _act_outcomes(projection.acts),
        list(projection.pages),
        projection.aggregate_basis["act_pages"],
        projection.aggregate,
        edge_hold_pages,
        other_outcomes,
    )

    members: dict[str, bytes] = {}
    source_rows, embedded = _source_rows(projection.pages, formats.embed_pixels, read_bytes)
    projected_acts, embedded_crops = _acts_with_source_references(
        projection.acts, formats.embed_pixels, read_bytes
    )
    projected_others, embedded_other_crops = _acts_with_source_references(
        projection.other_readings, formats.embed_pixels, read_bytes
    )
    projection = replace(
        projection,
        acts=tuple(_mark_retained_references(record) for record in projected_acts),
        other_readings=tuple(_mark_retained_references(record) for record in projected_others),
        page_accounting=tuple(_mark_retained_references(list(projection.page_accounting))),
    )
    sources_record: dict[str, Any] = {
        "schema": SOURCES_SCHEMA,
        "pages": source_rows,
        "regions": _source_regions(projection.acts + projection.other_readings),
        "act_citations": _act_citations(projection.acts),
        "act_outcomes": _act_outcomes(projection.acts),
        "aggregate_basis": projection.aggregate_basis,
        "witness_chairs": list(projection.witness_chairs),
        "witness_floor": projection.witness_floor,
        # Lets a clean-machine verifier derive the page-level hold itself.
        "ink_map_pages": list(projection.ink_map_pages),
        "other_outcomes": other_outcomes,
        "other_citations": _act_citations(projection.other_readings),
        "page_accounting": list(projection.page_accounting),
        "act_readings": _act_readings(projection.acts),
    }
    if projection.continuation_joins:
        sources_record["continuation_joins"] = _mark_retained_references(
            list(projection.continuation_joins)
        )
    coniector_rows = tuple(_mark_retained_references(row) for row in projection.reconstructions)
    if coniector_rows:
        # Which reconstructions the package shows, so a verifier can tell one
        # dropped from a format from one never made.
        sources_record["reconstructions"] = [list(row["act_ids"]) for row in coniector_rows]
    operator_rows = tuple(_mark_retained_references(row) for row in projection.operator_actions)
    model_readings = {
        model["act_id"]: _mark_retained_references(model) for model in projection.model_readings
    }
    if operator_rows:
        # Every package carries the label, whatever formats it selects.
        sources_record[OPERATOR_SOURCES_FIELD] = list(operator_rows)
    if model_readings:
        # And the model's reading beside each person's correction.
        sources_record[MODEL_READINGS_FIELD] = [
            model_reading_record(model_readings[act_id]) for act_id in sorted(model_readings)
        ]
    flagged_rows = _flagged_rows(projection)
    if flagged_rows:
        # Every package carries the held and flagged readings with their text,
        # labelled; `flagged.jsonl` repeats them with the review-items format.
        sources_record[FLAGGED_SOURCES_FIELD] = list(flagged_rows)
    if projection.reading_hold_codes is not None:
        # Lets a verifier require the row of a reading released on its own holds,
        # which no page hold would otherwise show.
        sources_record[READING_HOLDS_FIELD] = {
            act_id: sorted(set(codes))
            for act_id, codes in sorted(projection.reading_hold_codes.items())
        }
    members["sources.json"] = canonical_bytes(sources_record)

    if "text-bundle" in formats.formats:
        members.update(
            _text_bundle_members(
                projection.acts,
                source_rows,
                ledger,
                projection.continuation_joins,
                projection.other_readings,
                coniector_rows,
                operator_rows,
                model_readings,
                lot,
            )
        )
    if "acts-database" in formats.formats:
        members["acts.sqlite"] = _acts_database_bytes(
            projection.acts,
            {row["act_id"]: row["label"] for row in operator_rows},
            _database_run_metadata(_manifest_run_binding(projection), ledger),
            lot,
        )
    if "csv" in formats.formats:
        members[CSV_MEMBER] = _acts_csv_bytes(projection.acts, lot)
    if "jsonl" in formats.formats:
        members["acts.jsonl"] = _jsonl_bytes(_act_json_records(projection.acts, lot))
        if coniector_rows:
            members[CONIECTOR_MEMBER] = _jsonl_bytes(list(coniector_rows))
        if operator_rows:
            members[OPERATOR_MEMBER] = _jsonl_bytes(list(operator_rows))
        if model_readings:
            members[MODEL_READINGS_MEMBER] = _jsonl_bytes(
                [model_reading_record(model_readings[act_id]) for act_id in sorted(model_readings)]
            )
        members[OTHER_READINGS_MEMBER] = _jsonl_bytes(
            _other_json_records(projection.other_readings, lot)
        )
    if "review-items" in formats.formats:
        members["review-items.jsonl"] = _jsonl_bytes(
            _review_records(projection.acts, projection.other_readings, lot)
        )
        if flagged_rows:
            members[FLAGGED_MEMBER] = _jsonl_bytes(list(flagged_rows))
    members.update(embedded)
    members.update(embedded_crops)
    members.update(embedded_other_crops)

    manifest = _export_manifest(
        projection, formats, members, ledger, ink_map_rows, edge_hold_pages, other_outcomes
    )
    archive_members = {EXPORT_MANIFEST_NAME: canonical_bytes(manifest), **members}
    # Members that alone pass the limit are refused before the archive is assembled.
    embed_pixels = formats.embed_pixels
    members_size = sum(map(len, archive_members.values()))
    what = "the export archive's members together"
    require_within_export_archive_limit(members_size, what=what, embed_pixels=embed_pixels)
    data = _zip_bytes(archive_members)
    what = "the export archive"
    require_within_export_archive_limit(len(data), what=what, embed_pixels=embed_pixels)
    # A package that does not survive a clean extraction must fail before it
    # becomes a run-tree blob.
    with tempfile.TemporaryDirectory(prefix="armarium-verify-") as directory:
        clean_root = Path(directory)
        manifest_report = verify_export_bundle(data, clean_root)
        if len(_literal_formats_in(formats.formats)) >= 2:
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
        try:
            archive = ZipFile(BytesIO(data))
        except (BadZipFile, LargeZipFile, OSError) as error:
            raise SchemaRefusal("an Armarium package is not a readable ZIP archive") from error
        with archive:
            names = archive.namelist()
            if not names or names[0] != EXPORT_MANIFEST_NAME:
                raise SchemaRefusal("an Armarium package must begin with EXPORT_MANIFEST.json")
            _validate_archive_member_names(names)
            # A stored member cannot be larger than the archive, so refusing every
            # other method before decompression rules out a decompression bomb.
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
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SchemaRefusal("EXPORT_MANIFEST.json is not readable canonical JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != EXPORT_MANIFEST_SCHEMA:
        raise SchemaRefusal("the package has no recognized EXPORT_MANIFEST schema")
    if manifest.get("self_hash") != self_hash(manifest):
        raise SchemaRefusal("EXPORT_MANIFEST.json fails its self-hash")
    # A self-hash proves the manifest is unedited, not that this build writes it.
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
        if not _is_count(byte_count):
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
    _require_lot(run.get("lot"), formats.lot, subject="the manifest run binding")
    sources = _load_sources(root)
    _verify_source_references(sources["pages"], root)
    _verify_region_references(sources, root)
    _act_citation_sources(sources)
    _act_outcome_sources(sources)
    _verify_retained_references_bounded(sources)
    _verify_manifest_source_counts(manifest, sources)
    _verify_pixel_claims(manifest, formats, sources)
    _verify_retained_run_claim(manifest)
    _verify_canonical_text_claim(manifest)
    _verify_uncertainty_claim(manifest)
    _verify_exact_product_members(formats, sources, actual_names)
    search_fold_verification, operator_labels = _verify_product_accounting(
        root, manifest, formats, sources
    )
    _verify_page_layers(root, manifest, formats, sources)
    _verify_doubt_share_claim(root, manifest, formats)
    _verify_continuation_joins(root, formats, sources)
    # The operator rows and the model readings beside corrected ones, read once
    # for both layers that hold readings to them.
    recorded = _operator_rows(sources, manifest)
    models = _model_readings_shown(root, formats, sources, actual_names, recorded)
    _verify_coniector_layer(root, formats, sources, actual_names, models)
    _verify_operator_layer(
        root, manifest, formats, sources, actual_names, recorded, operator_labels
    )
    _verify_flagged_layer(root, manifest, formats, sources, actual_names)
    # Last, so every input the writer is fed has been checked on its own.
    if "text-bundle" in formats.formats:
        _verify_text_bundle_rendering(root, manifest, sources)
    if "csv" in formats.formats:
        _verify_csv_rendering(root, manifest, sources)
    verification = {}
    if search_fold_verification is not None:
        verification["search_fold"] = search_fold_verification
    return {**manifest, "verification": verification}


def _prepare_clean_root(root: Path) -> int:
    """Open a reusable extraction root that holds no link or special file.

    Leftover ordinary files are replaced atomically, so a pre-existing hard link
    cannot redirect a write. The returned descriptor pins the root inode; callers
    must close it.
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

    Traversal is descriptor-relative and no-follow, so swapping a checked directory
    for a symlink cannot redirect the write, and writing a new file then replacing
    breaks any pre-existing hard link instead of writing through it. Members are
    copied with no size cap because the caller has already refused every
    non-stored ZIP entry.
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


# A run can be `DELIVERED` and `complete` over instruments that never measured.
# Every bundle names them, with each status derived from the run's
# own records so a run that measured reads differently from one that did not.
NOT_MEASURED_SCHEMA: Final = "armarium-not-measured.v2"
NOT_MEASURED_BASIS_SCHEMA: Final = "armarium-not-measured-basis.v2"
# Every instrument is emitted on every bundle, so an absent row never reads as a
# measured one.
_PERLECTOR_UNCERTAIN_SPANS: Final = "perlector-uncertain-spans"
_GEOMETRY_CALIBRATION: Final = "designator-geometry-calibration"
# The page accounting's own thresholds, and Pass C over each page reading.
_PAGE_ACCOUNTING_THRESHOLDS: Final = "page-accounting-thresholds"
_PASS_C: Final = "perlector-pass-c"
# Each delivered act's dissent against its witnesses, which a comparison past the
# sealed step budget or the character-pair bound records as `compared: "unknown"`.
_COMPARISON_BOUNDS: Final = "comparison-bounds"
NOT_MEASURED_INSTRUMENTS: Final = (
    _PERLECTOR_UNCERTAIN_SPANS,
    _GEOMETRY_CALIBRATION,
    _PAGE_ACCOUNTING_THRESHOLDS,
    _PASS_C,
    _COMPARISON_BOUNDS,
)


# `declared-unproduced` means this run recorded nothing for the instrument: no stage
# in this build publishes it, or the run had nothing to assess (no act or no page
# read), so nothing was attempted; `not-measured` would suggest an attempt that
# came back empty or partial.
_NOT_MEASURED_STATUSES: Final = frozenset({"measured", "not-measured", "declared-unproduced"})
_NOT_MEASURED_ENTRY_FIELDS: Final = frozenset({"instrument", "status", "detail", "recorded_in"})
_NOT_MEASURED_FIELDS: Final = frozenset({"schema", "count", "entries"})
_NOT_MEASURED_DETAIL_FIELDS: Final = {
    _PERLECTOR_UNCERTAIN_SPANS: frozenset(
        {
            "sealed_audit_round_cap",
            "acts_delivered",
            "acts_with_uncertain_spans",
            "acts_assessed",
            "acts_not_assessed",
        }
    ),
    _GEOMETRY_CALIBRATION: frozenset({"configurations"}),
    _PAGE_ACCOUNTING_THRESHOLDS: frozenset(
        {"policy_sha256", "thresholds", "calibrated_for_this_corpus", "sample_count"}
    ),
    _PASS_C: frozenset({"pages_read", "pages_audit_not_run", "sealed_audit_round_cap"}),
    _COMPARISON_BOUNDS: frozenset(
        {
            "sealed_max_comparison_steps",
            "max_comparison_character_pairs",
            "acts_delivered",
            "acts_with_unmeasured_comparison",
            "unmeasured_act_ids",
        }
    ),
}
_GEOMETRY_CALIBRATION_ROW_FIELDS: Final = frozenset(
    {"configuration", "calibrated_for_this_corpus", "sample_count"}
)
# Where a reader checks each row against the evidence.
_NOT_MEASURED_RECORDED_IN: Final = {
    _PERLECTOR_UNCERTAIN_SPANS: (
        "the sealed Perlector audit policy's `round_cap` and each act's uncertainty layer, "
        "carried beside the act record in this bundle"
    ),
    _GEOMETRY_CALIBRATION: (
        "the `provenance` blocks of the sealed Designator geometry configuration and of the "
        "Perlector protocol's `[truncation]` table, whose digests "
        "this run's `config_digest` binds"
    ),
    _PAGE_ACCOUNTING_THRESHOLDS: (
        "the sealed `config/page_accounting.toml`, whose digest this run's `config_digest` "
        "binds and each page's `page-accounting` record names as `policy_sha256`"
    ),
    _PASS_C: (
        "each page's `page-reading` record, field `audit`, and the sealed Perlector audit "
        "policy's `round_cap`, in the retained run"
    ),
    _COMPARISON_BOUNDS: (
        'each delivered act\'s `perlectio` record, field `dissent`, rows `compared: "unknown"`, '
        "and the sealed alignment configuration's `[dissent] max_comparison_steps`, in the "
        "retained run"
    ),
}
# In canonical order. `perlector-protocol` is not Designator geometry, but its
# truncation length floor is an uncalibrated threshold and this is where an
# export discloses those. The instrument name is already on every bundle, so
# extend the list rather than rename it.
_GEOMETRY_CONFIGURATION_NAMES: Final = (
    "designator-geometry",
    "perlector-protocol",
)


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
# Two closed shapes: a manifest naming both identities or neither is refused.
_MANIFEST_RUN_FIELDS_FIXTURE: Final = frozenset({"fixture_id", "scenario", "config_digest", "lot"})
_MANIFEST_RUN_FIELDS_REAL: Final = frozenset({"submission_id", "scenario", "config_digest", "lot"})
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
        "uncertainty",
        "ink_map",
        "not_measured",
        "other_readings",
        "page_accounting",
        "reask",
        "doubt_share",
    }
)
_ACT_PARTITION_CLAIM_FIELDS: Final = frozenset(
    {"denominator", "expected_count", "counted", "reconciles", "categories", "act_keys"}
)
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
    "ink_map": frozenset({"denominator", "held_pages", "unmeasurable_pages"}),
    "pixels": frozenset({"embedded", "resolution_claim"}),
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


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_count(value: object) -> bool:
    return is_plain_int(value) and value >= 0


def _require_non_negative_integer(value: object, *, subject: str) -> int:
    if not _is_count(value):
        raise SchemaRefusal(f"{subject} is not a non-negative integer")
    return value


def _require_distinct_strings(value: object, *, subject: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise SchemaRefusal(f"{subject} is not a list of distinct non-blank strings")
    return value


def _validate_not_measured_detail(
    instrument: str, value: object, *, subject: str
) -> dict[str, Any]:
    """Validate the detail before either deriving or verifying its status."""
    detail = _require_exact_fields(value, _NOT_MEASURED_DETAIL_FIELDS[instrument], subject=subject)
    if instrument == _PERLECTOR_UNCERTAIN_SPANS:
        for field in (
            "sealed_audit_round_cap",
            "acts_delivered",
            "acts_with_uncertain_spans",
            "acts_assessed",
            "acts_not_assessed",
        ):
            _require_non_negative_integer(detail[field], subject=f"{subject} {field}")
        if detail["acts_assessed"] + detail["acts_not_assessed"] != detail["acts_delivered"]:
            raise SchemaRefusal(f"{subject} assessment counts do not partition its delivered acts")
        # Only the reader's own doubt report mints a span, so every act carrying
        # one was assessed; assessed acts are a part of the delivered ones.
        if detail["acts_with_uncertain_spans"] > detail["acts_assessed"]:
            raise SchemaRefusal(
                f"{subject} names more acts with uncertain spans than assessed acts; only "
                "a reader's own doubt report mints a span"
            )
    elif instrument == _PAGE_ACCOUNTING_THRESHOLDS:
        _require_sha256(detail["policy_sha256"], f"{subject} policy_sha256")
        thresholds = detail["thresholds"]
        if (
            not isinstance(thresholds, list)
            or not thresholds
            or any(
                not isinstance(row, dict)
                or set(row) != {"name", "value"}
                or not _is_nonempty_str(row["name"])
                or not is_plain_int(row["value"])
                or row["value"] <= 0
                for row in thresholds
            )
            or [row["name"] for row in thresholds] != sorted({row["name"] for row in thresholds})
        ):
            raise SchemaRefusal(
                f"{subject} thresholds are not distinct named positive values in name order"
            )
        if not isinstance(detail["calibrated_for_this_corpus"], bool):
            raise SchemaRefusal(f"{subject} calibrated_for_this_corpus is not a boolean")
        if detail["sample_count"] is not None:
            _require_non_negative_integer(detail["sample_count"], subject=f"{subject} sample_count")
        if not calibrated_claim_has_sample_evidence(
            detail["calibrated_for_this_corpus"], detail["sample_count"]
        ):
            raise SchemaRefusal(f"{subject} says calibrated but names no sample")
    elif instrument == _PASS_C:
        for field in ("pages_read", "pages_audit_not_run", "sealed_audit_round_cap"):
            _require_non_negative_integer(detail[field], subject=f"{subject} {field}")
        if detail["pages_audit_not_run"] > detail["pages_read"]:
            raise SchemaRefusal(f"{subject} names more unaudited pages than pages read")
    elif instrument == _COMPARISON_BOUNDS:
        for field in (
            "sealed_max_comparison_steps",
            "max_comparison_character_pairs",
            "acts_delivered",
            "acts_with_unmeasured_comparison",
        ):
            _require_non_negative_integer(detail[field], subject=f"{subject} {field}")
        act_ids = _require_distinct_strings(
            detail["unmeasured_act_ids"], subject=f"{subject} unmeasured_act_ids"
        )
        if act_ids != sorted(act_ids) or len(act_ids) != detail["acts_with_unmeasured_comparison"]:
            raise SchemaRefusal(
                f"{subject} does not name, in order, exactly the acts it counts as unmeasured"
            )
        if detail["acts_with_unmeasured_comparison"] > detail["acts_delivered"]:
            raise SchemaRefusal(f"{subject} names more unmeasured acts than delivered acts")
    elif instrument == _GEOMETRY_CALIBRATION:
        configurations = detail["configurations"]
        if not isinstance(configurations, list) or len(configurations) != len(
            _GEOMETRY_CONFIGURATION_NAMES
        ):
            raise SchemaRefusal(
                f"{subject} must name {len(_GEOMETRY_CONFIGURATION_NAMES)} configurations "
                "in canonical order"
            )
        for expected_name, configuration in zip(
            _GEOMETRY_CONFIGURATION_NAMES, configurations, strict=True
        ):
            row = _require_exact_fields(
                configuration,
                _GEOMETRY_CALIBRATION_ROW_FIELDS,
                subject=f"a row in {subject}",
            )
            if row["configuration"] != expected_name:
                raise SchemaRefusal(
                    f"{subject} does not name the sealed configurations in canonical order"
                )
            if not isinstance(row["calibrated_for_this_corpus"], bool):
                raise SchemaRefusal(f"a row in {subject} has untyped values")
            sample_count = row["sample_count"]
            if sample_count is not None:
                _require_non_negative_integer(
                    sample_count, subject=f"a row in {subject} sample_count"
                )
            if not calibrated_claim_has_sample_evidence(
                row["calibrated_for_this_corpus"], sample_count
            ):
                raise SchemaRefusal(
                    f"a row in {subject} says calibrated_for_this_corpus but sample_count is zero"
                )
    return detail


def _verify_manifest_field_closure(manifest: dict[str, Any]) -> None:
    """Reject unmeasured claims hidden in otherwise self-consistent JSON.

    Only blocks read field by field are closed here; blocks compared wholesale
    against a recomputation are closed by that comparison.
    """
    _require_exact_fields(manifest, _MANIFEST_FIELDS, subject="EXPORT_MANIFEST.json")
    _verify_manifest_run_binding(manifest.get("run"))
    claims = _require_exact_fields(
        manifest["claims"], _MANIFEST_CLAIM_FIELDS, subject="the manifest claims block"
    )
    for name, fields in _CLAIM_SUBFIELDS.items():
        _require_exact_fields(claims[name], fields, subject=f"the manifest {name} claim")
    act_partition = claims["act_partition"]
    if not isinstance(act_partition, dict):
        raise SchemaRefusal("the manifest act_partition claim is not an object")
    _require_exact_fields(
        act_partition, _ACT_PARTITION_CLAIM_FIELDS, subject="the manifest act_partition claim"
    )
    if act_partition["denominator"] != _ACT_PARTITION_DENOMINATOR:
        raise SchemaRefusal("the manifest act denominator is not this build's fixed claim")
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
    _verify_not_measured_block(claims["not_measured"])


def _verify_manifest_run_binding(raw_run: object) -> None:
    """Exactly one of the two closed run-identity shapes, with non-blank values."""
    if not isinstance(raw_run, dict):
        raise SchemaRefusal("the manifest run binding is not an object")
    has_fixture = "fixture_id" in raw_run
    has_submission = "submission_id" in raw_run
    if has_fixture and has_submission:
        raise SchemaRefusal(
            "the manifest run binding names both a fixture identifier and a submission "
            "identifier; a run's export is identified by exactly one, never both"
        )
    if not has_fixture and not has_submission:
        raise SchemaRefusal(
            "the manifest run binding names neither a fixture identifier nor a submission "
            "identifier; a run's export must be identified by exactly one"
        )
    identity_field = "fixture_id" if has_fixture else "submission_id"
    run = _require_exact_fields(
        raw_run,
        _MANIFEST_RUN_FIELDS_FIXTURE if has_fixture else _MANIFEST_RUN_FIELDS_REAL,
        subject="the manifest run binding",
    )
    if any(
        not isinstance(run.get(field), str) or not run[field].strip()
        for field in (identity_field, "scenario")
    ):
        subject = "fixture" if has_fixture else "submission"
        raise SchemaRefusal(
            f"the manifest run binding has no non-blank {subject} and scenario identities"
        )
    if has_submission:
        # As strict as `_validate_projection`, so a resealed package cannot carry
        # a hand-typed label where a ledger hash belongs.
        _require_sha256(run["submission_id"], "the manifest run binding submission identity")


def _verify_not_measured_block(block: object) -> None:
    """Every instrument once, in order, each status re-derived from its detail."""
    not_measured = _require_exact_fields(
        block, _NOT_MEASURED_FIELDS, subject="the manifest not_measured block"
    )
    if not_measured["schema"] != NOT_MEASURED_SCHEMA:
        raise SchemaRefusal("the manifest not_measured block is not this build's schema")
    rows = not_measured["entries"]
    if not isinstance(rows, list):
        raise SchemaRefusal("the manifest not_measured block has no entry rows")
    named = []
    for row in rows:
        entry = _require_exact_fields(
            row, _NOT_MEASURED_ENTRY_FIELDS, subject="a manifest not_measured entry"
        )
        instrument = entry["instrument"]
        if not isinstance(instrument, str):
            raise SchemaRefusal("a manifest not_measured entry has a non-string instrument")
        if instrument not in _NOT_MEASURED_DETAIL_FIELDS:
            raise SchemaRefusal(
                "the manifest not_measured block names an instrument this build does not produce"
            )
        if not isinstance(entry["status"], str) or entry["status"] not in _NOT_MEASURED_STATUSES:
            raise SchemaRefusal("a manifest not_measured entry carries an unrecognized status")
        detail = _validate_not_measured_detail(
            instrument,
            entry["detail"],
            subject=f"the manifest not_measured detail for {instrument}",
        )
        expected_status = _not_measured_status(instrument, detail)
        if entry["status"] != expected_status:
            raise SchemaRefusal(
                f"the manifest not_measured status for {instrument} disagrees with its detail: "
                f"expected {expected_status!r}, got {entry['status']!r}"
            )
        if entry["recorded_in"] != _NOT_MEASURED_RECORDED_IN[instrument]:
            raise SchemaRefusal(
                f"the manifest not_measured entry for {instrument} does not name this build's "
                "canonical evidence location"
            )
        named.append(instrument)
    if named != list(NOT_MEASURED_INSTRUMENTS):
        raise SchemaRefusal(
            "the manifest not_measured block does not name this build's instruments exactly "
            f"once each, in order: expected {list(NOT_MEASURED_INSTRUMENTS)}, got {named}"
        )
    expected_count = sum(1 for row in rows if row["status"] != "measured")
    _require_non_negative_integer(not_measured["count"], subject="the manifest not_measured count")
    if not_measured["count"] != expected_count:
        raise SchemaRefusal(
            "the manifest not_measured count does not reconcile with its own entries"
        )


def verify_delivered_bundle(data: bytes, clean_root) -> dict[str, Any]:
    """Package integrity and one reading per act across formats, in a single extraction.

    The manifest asserts ``canonical_text.identity_verified_across``, and this is
    the last gate before a recipient, so the publish path checks that claim as
    well as the package's integrity.
    """
    manifest = verify_export_bundle(data, clean_root)
    formats = _manifest_formats(manifest)
    compared = _literal_formats_in(formats.formats)
    if len(compared) >= 2:
        _compare_literal_projections(clean_root, formats)
        identity = {"status": "verified", "compared_formats": compared}
    else:
        identity = {
            "status": "not-applicable-fewer-than-two-literal-formats",
            "compared_formats": compared,
        }
    verification = {**manifest.get("verification", {}), "projection_identity": identity}
    return {**manifest, "verification": verification}


def _compare_literal_projections(root: Path, formats: ArmariumFormats) -> dict[str, str]:
    """Compare already-verified literal members without extracting the package again.

    Uncertainty and text status are compared with the text, so formats that disagree about whether an act is damaged fail as a
    diverging literal would.
    """
    projections: dict[str, dict[str, tuple]] = {}
    selected_literal_formats = [name for name in _LITERAL_TEXT_FORMATS if name in formats.formats]
    if len(selected_literal_formats) < 2:
        raise SchemaRefusal("projection identity needs at least two selected literal-text formats")

    for name in selected_literal_formats:
        projections[name] = _literal_projection(root, name)

    baseline_name, baseline = next(iter(projections.items()))
    for name, records in projections.items():
        if records != baseline:
            raise SchemaRefusal(
                f"canonical clean-text, uncertainty or damage-record projection differs "
                f"between {baseline_name} and {name}"
            )
    return {act_id: record[0] for act_id, record in baseline.items()}


def _literal_projection(root: Path, name: str) -> dict[str, tuple]:
    if name == "text-bundle":
        return _text_bundle_literals(root)
    if name == "acts-database":
        return _database_literals(root / "acts.sqlite")
    if name == "jsonl":
        return _jsonl_literals(root / "acts.jsonl")
    if name == "csv":
        return _csv_literals(root / CSV_MEMBER)
    # A new literal format with no branch here would otherwise be skipped and
    # reported identical.
    raise SchemaRefusal(f"projection identity has no comparison built for literal format {name!r}")


def _verify_continuation_joins(root: Path, formats: ArmariumFormats, sources: dict) -> None:
    """Recompute every join row from the packaged literals."""
    joins = sources["continuation_joins"] or []
    outcomes = _act_outcome_sources(sources)
    act_keys = {act_id: outcome["act_key"] for act_id, outcome in outcomes.items()}
    act_pages = sources["aggregate_basis"].get("act_pages") or {}
    if joins and not (
        isinstance(act_pages, dict)
        and all(
            isinstance(pages, list) and all(is_plain_int(page) for page in pages)
            for pages in act_pages.values()
        )
    ):
        raise SchemaRefusal("the package's act page attribution is not lists of page ordinals")
    literal_formats = _literal_formats_in(formats.formats)
    delivered_texts = (
        {
            act_id: record[0]
            for act_id, record in _literal_projection(root, literal_formats[0]).items()
        }
        if literal_formats and joins
        else {
            act_id: ""
            for act_id, outcome in outcomes.items()
            if outcome["category"] == ArmariumCategory.DELIVERED.value
        }
    )
    join_ids: set[str] = set()
    for join in joins:
        _require_exact_fields(join, _JOIN_FIELDS, subject="a continuation-join row")
        sides = (join["head_act_ids"], join["tail_act_ids"])
        pages = (join["head_page_ordinal"], join["tail_page_ordinal"])
        reference = join["candidate_ref"]
        if (
            not _is_line_safe_identity(join["join_id"])
            or join["join_id"] in join_ids
            or not all(
                isinstance(side, list)
                and all(isinstance(act_id, str) for act_id in side)
                and set(side) <= set(outcomes)
                for side in sides
            )
            or not all(is_plain_int(page) for page in pages)
            or pages[1] != pages[0] + 1
            or any(
                page not in act_pages.get(act_keys[act_id], [])
                for side, page in zip(sides, pages, strict=True)
                for act_id in side
            )
            or not isinstance(reference, dict)
            or set(reference) != {"availability", "run_relative_path", "sha256"}
            or reference["availability"] != _RUN_ACCESS_REQUIRED
        ):
            raise SchemaRefusal(
                "a continuation-join row names no valid join, candidate, acts or adjacent pages"
            )
        join_ids.add(join["join_id"])
        expected = continuation_join_row(
            join_id=join["join_id"],
            candidate_ref=reference,
            head_page_ordinal=pages[0],
            tail_page_ordinal=pages[1],
            head_act_ids=sides[0],
            tail_act_ids=sides[1],
            delivered_texts=delivered_texts,
            selected_formats=formats.formats,
            flags_disagree=join["not_reconstructed_reason"] == _FLAGS_DISAGREE,
        )
        if expected != join:
            raise SchemaRefusal(
                f"continuation join {join['join_id']} does not recompute from its acts' literals"
            )


def _verify_coniector_layer(
    root: Path,
    formats: ArmariumFormats,
    sources: dict,
    actual_names: set[str],
    models: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Recompute every reconstruction the package shows, in each format that shows it.

    Each row must stand beneath delivered literals of its own format, name each
    act by that act's own key and, when made, be its own departures applied to
    its own diplomatic pieces. Beneath an act a person corrected, its pieces
    are held to the model reading that format shows beside the person's text,
    and the row must say it was made from it. In the text bundle each row must
    sit beneath its own act's section (a join in its own section) in every
    folder that sections the act. Every format that shows reconstructions shows
    exactly the rows `sources.json` records, so a row dropped from one is
    refused. `models` is `_model_readings_shown`'s, by format.
    """
    shown: list[list[dict[str, Any]]] = []
    if CONIECTOR_MEMBER in actual_names:
        literals = _jsonl_literals(root / "acts.jsonl")
        keys = {
            row["act_id"]: row["act_key"]
            for row in _jsonl_rows(root / "acts.jsonl", "acts JSONL", "an acts JSONL row")
            if isinstance(row, dict) and isinstance(row.get("act_id"), str)
        }
        corrected = {act_id: model["text"] for act_id, model in models.get("jsonl", {}).items()}
        shown.append(
            [
                verify_row(row, literals, keys, corrected)
                for row in _jsonl_rows(
                    root / CONIECTOR_MEMBER, CONIECTOR_MEMBER, "a reconstruction row"
                )
            ]
        )
    elif "jsonl" in formats.formats:
        shown.append([])
    if "text-bundle" in formats.formats:
        records = _text_bundle_records(root, sources["pages"])
        literals = {act_id: (record.literal,) for act_id, record in records.items()}
        keys = {act_id: record.heading_key for act_id, record in records.items()}
        corrected = {
            act_id: model["text"] for act_id, model in models.get("text-bundle", {}).items()
        }
        by_act_ids: dict[tuple[str, ...], dict[str, Any]] = {}
        sectioned: dict[str, set[str]] = {}
        placed: dict[str, Counter[tuple[str, ...]]] = {}
        for folder in sorted(
            {_source_folder_for_declared_path(page["declared_path"]) for page in sources["pages"]}
        ):
            lines = _package_lines(root / _text_member_path(folder), "text bundle")
            sectioned[folder], placements = text_bundle_placements(lines)
            placed[folder] = Counter()
            for _place, shown_row in placements:
                row = verify_row(shown_row, literals, keys, corrected)
                act_ids = tuple(row["act_ids"])
                if by_act_ids.setdefault(act_ids, row) != row:
                    raise SchemaRefusal(
                        f"the text bundle shows {row['act_keys']}'s reconstruction "
                        "differently in two places"
                    )
                placed[folder][act_ids] += 1
        for act_ids, row in by_act_ids.items():
            for folder, acts in sectioned.items():
                if anchor_act(row) in acts and placed[folder][act_ids] != 1:
                    raise SchemaRefusal(
                        f"the text bundle does not show {row['act_keys']}'s reconstruction "
                        "exactly once in every folder that shows the act"
                    )
        shown.append(list(by_act_ids.values()))
    recorded = sorted(tuple(act_ids) for act_ids in sources.get("reconstructions") or [])
    for rows in shown:
        if len({tuple(row["act_ids"]) for row in rows}) != len(rows):
            raise SchemaRefusal("a package shows one reconstruction twice")
        if sorted(tuple(row["act_ids"]) for row in rows) != recorded:
            raise SchemaRefusal(
                "a package format shows other reconstructions than its sources record"
            )
        for row in rows:
            _verify_retained_references(row)
    keyed = [sorted(rows, key=lambda row: row["act_ids"]) for rows in shown]
    if any(rows != keyed[0] for rows in keyed):
        raise SchemaRefusal(
            f"the text bundle and {CONIECTOR_MEMBER} show different reconstructions"
        )


def _delivered_readings(
    sources: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, tuple[str, str]]:
    """Each reading the package delivers, act or `other`, as `{act_id: (act_key, kind)}`."""
    categories = _manifest_act_categories(manifest)
    keys = _manifest_act_keys(manifest, categories)
    delivered = {
        act_id: (keys[act_id], "act")
        for act_id, category in categories.items()
        if category == ArmariumCategory.DELIVERED.value
    }
    for act_id, row in _other_outcome_sources(sources).items():
        if row["category"] == ArmariumCategory.DELIVERED.value:
            delivered[act_id] = (row["act_key"], "other")
    return delivered


def _operator_rows(sources: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The operator rows `sources.json` records, each about a reading the package delivers."""
    return verify_rows(
        sources.get(OPERATOR_SOURCES_FIELD) or [],
        _delivered_readings(sources, manifest),
        "sources.json",
    )


def _verify_reading_holds(
    sources: dict[str, Any], manifest: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    """Every delivered reading held on its own holds carries the operator row that released it.

    `sources.json` names each delivered reading's own hold codes whenever the
    run has review decisions, the only way a held reading is delivered; the
    aggregate is recomputed from those decisions, so they cannot be dropped
    without the aggregate showing it. A reading with codes needs a row that
    names exactly them, so a label dropped from every format is refused even
    when its page carries no hold.
    """
    codes = sources.get(READING_HOLDS_FIELD)
    decided = _REVIEW_DECISIONS_BASIS_FIELD in sources["aggregate_basis"]
    if codes is None:
        if decided:
            raise SchemaRefusal(
                "the package records review decisions but not its delivered readings' own holds"
            )
        if rows:
            raise SchemaRefusal("the package labels a release no review decision made")
        return
    if not decided:
        raise SchemaRefusal("the package names readings' own holds without any review decision")
    delivered = _delivered_readings(sources, manifest)
    if (
        not isinstance(codes, dict)
        or set(codes) != set(delivered)
        or not all(
            isinstance(held, list)
            and all(_is_nonempty_str(code) for code in held)
            and held == sorted(set(held))
            for held in codes.values()
        )
    ):
        raise SchemaRefusal(
            "sources.json does not name exactly the delivered readings' own hold codes"
        )
    by_id = {row["act_id"]: row for row in rows}
    for act_id, held in sorted(codes.items()):
        row = by_id.get(act_id)
        if (row["reading_hold_codes"] if row is not None else []) != held:
            raise SchemaRefusal(
                f"{delivered[act_id][0]} is delivered over its own holds {held} but its operator "
                "row does not name exactly them"
            )


def _verify_operator_layer(
    root: Path,
    manifest: dict[str, Any],
    formats: ArmariumFormats,
    sources: dict[str, Any],
    actual_names: set[str],
    recorded: list[dict[str, Any]],
    operator_labels: dict[str, str | None] | None,
) -> None:
    """Every format that carries the operator layer shows exactly the rows `sources.json` records.

    `operator.jsonl` carries them when the JSONL format is selected, the text
    bundle shows each once beneath its reading's section in every folder that
    sections the reading, and the acts database names each act's label in its
    `operator_label` column (`operator_labels`, by act id; None without the
    database), so a label dropped from a format is refused.
    """
    _verify_reading_holds(sources, manifest, recorded)
    _verify_corrections(root, formats, sources, recorded)
    if operator_labels is not None:
        expected = {row["act_id"]: row["label"] for row in recorded if row["kind"] == "act"}
        if operator_labels != {act_id: expected.get(act_id) for act_id in operator_labels}:
            raise SchemaRefusal(
                "the acts database does not label exactly the acts an operator released or "
                "corrected, as their operator rows do"
            )
    shown: list[tuple[str, list[dict[str, Any]]]] = []
    if "jsonl" in formats.formats:
        rows = (
            list(_jsonl_rows(root / OPERATOR_MEMBER, OPERATOR_MEMBER, "an operator row"))
            if OPERATOR_MEMBER in actual_names
            else []
        )
        shown.append((OPERATOR_MEMBER, sorted(rows, key=lambda row: str(row.get("act_id")))))
    if "text-bundle" in formats.formats:
        by_id: dict[str, dict[str, Any]] = {}
        for folder in sorted(
            {_source_folder_for_declared_path(page["declared_path"]) for page in sources["pages"]}
        ):
            lines = _package_lines(root / _text_member_path(folder), "text bundle")
            sectioned = {
                line.split(": ", 1)[1]
                for line in lines
                if line.startswith(("act-id: ", "other-id: "))
            }
            placed = Counter()
            for act_id, row, _model in text_bundle_rows(lines):
                if by_id.setdefault(act_id, row) != row:
                    raise SchemaRefusal(
                        f"the text bundle labels {row['act_key']} differently in two places"
                    )
                placed[act_id] += 1
            for row in recorded:
                if row["act_id"] in sectioned and placed[row["act_id"]] != 1:
                    raise SchemaRefusal(
                        f"the text bundle does not label {row['act_key']} as released by an "
                        "operator exactly once in every folder that shows it"
                    )
        shown.append(("the text bundle", sorted(by_id.values(), key=lambda row: row["act_id"])))
    for subject, rows in shown:
        if rows != recorded:
            raise SchemaRefusal(f"{subject} shows other operator rows than sources.json records")
        for row in rows:
            _verify_retained_references(row)


def _flagged_rows(projection: ArmariumProjection) -> tuple[dict[str, Any], ...]:
    """The projection's flagged layer, each row held to the reading it names, references marked.

    The producer's rows are checked here as the verifier checks a package's, so
    a row of no counted reading, or one that contradicts its reading's category
    or text, never leaves the run tree.
    """
    if not projection.flagged_readings:
        return ()
    readings = _readings_by_kind(projection.acts, projection.other_readings)
    outcomes = {
        reading["act_id"]: {**_row_head(reading), "kind": kind} for reading, kind in readings
    }
    rows = [
        {**row, "reason": outcomes[row["act_id"]]["reason"]}
        if row.get("act_id") in outcomes
        else row
        for row in projection.flagged_readings
    ]
    verify_flagged_rows(
        rows,
        outcomes=outcomes,
        kinds={act_id: outcome["kind"] for act_id, outcome in outcomes.items()},
        literals={reading["act_id"]: reading[CANONICAL_TEXT_FIELD] for reading, _kind in readings},
        lot=projection.lot,
        subject="an Armarium projection",
    )
    return tuple(_mark_retained_references(row) for row in rows)


def _verify_flagged_layer(
    root: Path,
    manifest: dict[str, Any],
    formats: ArmariumFormats,
    sources: dict[str, Any],
    actual_names: set[str],
) -> None:
    """The held and flagged readings, as `sources.json` records them and `flagged.jsonl` shows them.

    Each row is held to the package's own accounting (`flagged_layer.verify_rows`):
    a counted reading's identity and category, codes that hold or flag it, the
    priority those codes give, and, for an established reading when the package
    carries JSONL, the delivered literal. With the review-items format the member
    repeats the rows exactly, and is written exactly when there are any.
    """
    recorded = sources.get(FLAGGED_SOURCES_FIELD) or []
    acts = _act_outcome_sources(sources)
    others = _other_outcome_sources(sources)
    outcomes: dict[str, dict[str, Any]] = {**acts, **others}
    kinds = {act_id: "act" for act_id in acts} | {act_id: "other" for act_id in others}
    literals: dict[str, str | None] | None = None
    if "jsonl" in formats.formats:
        literals = {}
        for member in ("acts.jsonl", OTHER_READINGS_MEMBER):
            for row in _jsonl_rows(root / member, member, "a reading row"):
                if isinstance(row, dict) and isinstance(row.get("act_id"), str):
                    literals[row["act_id"]] = row.get(CANONICAL_TEXT_FIELD)
    lot = manifest["run"]["lot"]
    verified = verify_flagged_rows(
        recorded, outcomes=outcomes, kinds=kinds, literals=literals, lot=lot, subject="sources.json"
    )
    for row in verified.values():
        _verify_retained_references_bounded(row)
        _verify_evidence_refs(row["evidence_refs"], subject="a flagged-reading row")
    if "review-items" in formats.formats:
        shown = (
            list(_jsonl_rows(root / FLAGGED_MEMBER, FLAGGED_MEMBER, "a flagged-reading row"))
            if FLAGGED_MEMBER in actual_names
            else []
        )
        if shown != list(recorded):
            raise SchemaRefusal(
                f"{FLAGGED_MEMBER} shows other flagged readings than sources.json records"
            )


def _model_readings_shown(
    root: Path,
    formats: ArmariumFormats,
    sources: dict[str, Any],
    actual_names: set[str],
    recorded: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """The model's reading of each corrected reading, as each place that carries it shows it.

    `{place: {reading id: {text, uncertainty, text_status}}}` for `sources`
    (`sources.json`'s `model_readings`, in every package), `jsonl`
    (`model_readings.jsonl`) and `text-bundle` (beneath the reading's section,
    the same in every folder). Each must show one for exactly the readings a
    corrected row names, each held to its row
    (`operator_layer.verify_model_reading`), and the formats must show what
    `sources.json` records, so a dropped or altered original is refused.
    """
    corrected = {row["act_id"]: row for row in recorded if row["label"] == CORRECTED_LABEL}
    shown: dict[str, dict[str, dict[str, Any]]] = {
        "sources": _model_rows(sources.get(MODEL_READINGS_FIELD) or [], corrected, "sources.json")
    }
    if "jsonl" in formats.formats:
        rows = (
            _jsonl_rows(root / MODEL_READINGS_MEMBER, MODEL_READINGS_MEMBER, "a model reading row")
            if MODEL_READINGS_MEMBER in actual_names
            else []
        )
        shown["jsonl"] = _model_rows(rows, corrected, MODEL_READINGS_MEMBER)
    if "text-bundle" in formats.formats:
        models = {}
        for folder in sorted(
            {_source_folder_for_declared_path(page["declared_path"]) for page in sources["pages"]}
        ):
            lines = _package_lines(root / _text_member_path(folder), "text bundle")
            for act_id, _row, model in text_bundle_rows(lines):
                if model is not None and models.setdefault(act_id, model) != model:
                    raise SchemaRefusal("the text bundle shows one model reading differently")
        shown["text-bundle"] = models
    for name, models in shown.items():
        place = "sources.json" if name == "sources" else f"the {name} format"
        if set(models) != set(corrected):
            raise SchemaRefusal(
                f"{place} does not show the model reading (original) beside exactly the "
                "readings a person corrected"
            )
        if models != shown["sources"]:
            raise SchemaRefusal(
                f"{place} shows a model reading (original) other than sources.json records"
            )
    return shown


def _model_rows(
    rows: Any, corrected: dict[str, dict[str, Any]], subject: str
) -> dict[str, dict[str, Any]]:
    """`model_reading_record` rows as one place shows them, each held to its corrected row."""
    models: dict[str, dict[str, Any]] = {}
    for record in rows:
        if not isinstance(record, dict) or record.get("schema") != MODEL_READING_SCHEMA:
            raise SchemaRefusal(f"a model reading row in {subject} has no recognized schema")
        _require_exact_fields(record, MODEL_ROW_FIELDS, subject="a model reading row")
        row = corrected.get(record["act_id"])
        if (
            row is None
            or record["act_id"] in models
            or (record["act_key"], record["kind"]) != (row["act_key"], row["kind"])
            or record["label"] != row["model_reading"]["label"]
            or record["perlectio_ref"] != row["model_reading"]["perlectio_ref"]
        ):
            raise SchemaRefusal(
                f"{subject} shows a model reading no corrected row names, or names it differently"
            )
        _verify_retained_references_bounded(record)
        models[record["act_id"]] = verify_model_reading(row, record, subject)
    return models


def _correction_views(
    root: Path, formats: ArmariumFormats, sources: dict[str, Any]
) -> list[tuple[str, dict[str, tuple[str, Any]]]]:
    """Each selected literal format's delivered readings, acts and others, as `{id: (text, layer)}`."""
    views = []
    for name in _LITERAL_TEXT_FORMATS:
        if name not in formats.formats:
            continue
        view = {
            act_id: (record[0], record[2])
            for act_id, record in _literal_projection(root, name).items()
        }
        if name == "jsonl":
            for act_id, record in _other_jsonl_records(
                root / OTHER_READINGS_MEMBER, sources["regions"]
            ).items():
                if record["category"] == ArmariumCategory.DELIVERED.value:
                    view[act_id] = (record[CANONICAL_TEXT_FIELD], record["uncertainty"])
        elif name == "text-bundle":
            for act_id, record in _text_bundle_other_records(root, sources["pages"]).items():
                view[act_id] = (record[1], record[4])
        views.append((name, view))
    return views


def _verify_corrections(
    root: Path,
    formats: ArmariumFormats,
    sources: dict[str, Any],
    recorded: list[dict[str, Any]],
) -> None:
    """Every reading a person corrected is labelled, its decisions rebuilt from its text.

    A delivered reading's provenance is a correction's exactly when an operator
    row labels it "corrected by a person", and the aggregate basis names
    exactly those readings. The provenance names the row's note, model reading
    and edits; each literal format delivers the person's text with the fixed
    no-doubt layer, and each edit the provenance names is rebuilt from that
    text and note (`common.correction.edit_digests`) and must hash to the
    decision and stored approval it names, so a text no edit names is refused.
    No other delivered reading carries the correction's layer.
    """
    corrected = {row["act_id"]: row for row in recorded if row["label"] == CORRECTED_LABEL}
    basis = sources["aggregate_basis"].get(_REVIEW_DECISIONS_BASIS_FIELD)
    named = basis.get("corrections") if isinstance(basis, dict) else []
    if named != sorted(row["act_key"] for row in corrected.values()):
        raise SchemaRefusal(
            "the aggregate basis does not name exactly the readings the package labels "
            "corrected by a person"
        )
    citations = {
        **_act_citation_sources(sources),
        **_act_citation_sources(sources, "other_citations"),
    }
    for act_id, citation in citations.items():
        if is_correction(citation["provenance"]) != (act_id in corrected):
            raise SchemaRefusal(
                f"{citation['act_key']}'s provenance and its operator row disagree about whether "
                "a person corrected it"
            )
    for act_id, row in corrected.items():
        _require_correction_provenance(citations[act_id]["provenance"], row)
    for name, view in _correction_views(root, formats, sources):
        for act_id, (literal, layer) in view.items():
            if act_id not in corrected:
                if isinstance(layer, dict) and layer.get("lectio_kind") == CORRECTED_LECTIO:
                    raise SchemaRefusal(
                        f"the {name} format carries a person's correction layer on a reading "
                        "no operator row labels corrected"
                    )
                continue
            row = corrected[act_id]
            if layer != corrected_layer():
                raise SchemaRefusal(
                    f"the {name} format does not deliver {row['act_key']}, which a person "
                    "corrected, with the correction's no-doubt layer"
                )
            for decision in citations[act_id]["provenance"]["decisions"]:
                try:
                    digests = edit_digests(decision, act_id=act_id, text=literal, note=row["note"])
                except ContractError as error:
                    raise SchemaRefusal(
                        f"{row['act_key']}'s correction cannot be rebuilt as an edit"
                    ) from error
                if digests != (decision["decision_hash"], decision["approval_ref"]["sha256"]):
                    raise SchemaRefusal(
                        f"the {name} format delivers a text for {row['act_key']} that the edit "
                        "its provenance names does not record"
                    )


# What an operator row and a correction's provenance both say of each edit.
_EDIT_NAMED_FIELDS: Final = ("decision_hash", "approver", "timestamp", "reason", "approval_ref")


def _edit_named(decision: dict[str, Any]) -> dict[str, Any]:
    """The fields of one edit both an operator row and a correction's provenance name."""
    return {key: decision[key] for key in _EDIT_NAMED_FIELDS}


def _require_correction_provenance(provenance: Any, row: dict[str, Any]) -> None:
    """A corrected reading's provenance: the row's label, note, model reading and edits."""
    edits = sorted(
        (_edit_named(decision) for decision in row["decisions"] if decision["decision"] == "edit"),
        key=lambda decision: decision["decision_hash"],
    )
    decisions = provenance.get("decisions") if isinstance(provenance, dict) else None
    if (
        not isinstance(provenance, dict)
        or set(provenance) != CORRECTION_PROVENANCE_FIELDS
        or provenance["note"] != row["note"]
        or provenance["model_reading"] != row["model_reading"]
        or not isinstance(decisions, list)
        or not all(
            isinstance(decision, dict) and set(decision) == CORRECTION_DECISION_FIELDS
            for decision in decisions
        )
        or [_edit_named(decision) for decision in decisions] != edits
    ):
        raise SchemaRefusal(
            f"{row['act_key']}'s provenance does not name the correction its operator row labels"
        )


def _operator_released_pages(
    sources: dict[str, Any], manifest: dict[str, Any], held: dict[int, list[str]]
) -> set[int]:
    """The held pages whose every delivered reading an operator released over its page's holds.

    Each such reading's row must name every one of its page accounting's
    hold codes among the reading's own holds the decisions overrode.
    """
    rows = {row["act_id"]: row for row in _operator_rows(sources, manifest)}
    on_page: dict[int, list[str]] = {}
    for act_id, citation in _act_citation_sources(sources).items():
        for region in citation["source_regions"]:
            on_page.setdefault(region["source_page_ordinal"], []).append(act_id)
    for act_id, row in _other_outcome_sources(sources).items():
        if row["category"] == ArmariumCategory.DELIVERED.value:
            on_page.setdefault(row["page_ordinal"], []).append(act_id)
    return {
        ordinal
        for ordinal, codes in held.items()
        if all(
            act_id in rows and set(codes) <= set(rows[act_id]["reading_hold_codes"])
            for act_id in on_page.get(ordinal, [])
        )
    }


INK_MAP_DENOMINATOR: Final = "ink-map sealed pages"
_INK_MAP_ROW_FIELDS: Final = frozenset({"ordinal", "initial_outcome", "remeasured"})
# The gates travel with the counts because a clean-machine verifier recomputes
# the hold without the run's configuration.
_INK_MAP_REMEASURE_FIELDS: Final = frozenset(
    {
        "total_ink_pixels",
        "outside_ink_pixels",
        "edge_band_pixels",
        "substantial_ink_pixels",
        "minimum_ink_pixels",
        "minimum_fraction_outside_bp",
    }
)
# The recorded gates that are refused at zero: a zero gate holds every page.
_INK_MAP_REMEASURE_GATES: Final = frozenset(
    {"substantial_ink_pixels", "minimum_ink_pixels", "minimum_fraction_outside_bp"}
)
_UNCLAIMED_EDGE_INK: Final = "unclaimed-edge-ink"
# A new value in this closed vocabulary needs no schema bump: an older verifier
# refuses it by name, whereas a renamed field would be misread silently.
_INK_MAP_OUTCOMES: Final = frozenset({"mapped", _UNCLAIMED_EDGE_INK, INK_NOT_MEASURABLE})


def _validate_ink_map_pages(rows: Any, subject: str) -> list[dict[str, Any]]:
    """Close the ink-map source rows before anything derives a hold from them.

    Only pages the Ink Map flagged are re-measured; the rest carry
    `remeasured: None` rather than zeros nobody measured. An
    `ink-not-measurable` page stays in the rows because it is in the page census,
    and can never be held because it has no counts.
    """
    if not isinstance(rows, list | tuple):
        raise SchemaRefusal(
            f"{subject} does not carry its ink-map page rows as a list. Its page-level edge "
            "holds cannot be derived or checked. Rebuild the export from the intact run tree."
        )
    ordinals: set[int] = set()
    validated: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != _INK_MAP_ROW_FIELDS:
            raise SchemaRefusal(
                f"{subject} has an ink-map row that is not its closed shape. The verifier cannot "
                "tell which page measurement the row states. Rebuild the export from the intact "
                "run tree."
            )
        ordinal, outcome, remeasured = row["ordinal"], row["initial_outcome"], row["remeasured"]
        if not is_plain_int(ordinal) or ordinal <= 0:
            raise SchemaRefusal(
                f"{subject} has an ink-map row without a positive integer page ordinal. The "
                "verifier cannot bind its measurement to a sealed page. Rebuild the export "
                "from the sealed page inventory."
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
                    "Rebuild the export from the retained Ink Map and Perlector act-region evidence."
                )
            if any(
                not is_plain_int(remeasured[field])
                or remeasured[field] < (1 if field in _INK_MAP_REMEASURE_GATES else 0)
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
    # The noise floor is one sealed value for the whole run (never scaled per
    # page), so every flagged row must agree; otherwise one inflated row could
    # silently release that page's hold.
    noise_floors = {
        (row["remeasured"]["minimum_ink_pixels"], row["remeasured"]["minimum_fraction_outside_bp"])
        for row in validated
        if row["initial_outcome"] == _UNCLAIMED_EDGE_INK
    }
    if len(noise_floors) > 1:
        raise SchemaRefusal(
            f"{subject} carries more than one sealed noise floor across its flagged ink-map "
            f"pages ({sorted(noise_floors)}). minimum_ink_pixels and minimum_fraction_outside_bp "
            "are one run's single sealed noise floor, identical for every page; a bundle with "
            "more than one value cannot have come from one honest run. Rebuild the export from "
            "the retained Ink Map evidence."
        )
    return validated


def _edge_hold_pages_from_validated_rows(rows: list[dict[str, Any]]) -> tuple[int, ...]:
    return tuple(
        sorted(
            row["ordinal"]
            for row in rows
            if row["initial_outcome"] == _UNCLAIMED_EDGE_INK
            and coverage_flag(
                row["remeasured"]["total_ink_pixels"],
                row["remeasured"]["outside_ink_pixels"],
                substantial_ink_pixels=row["remeasured"]["substantial_ink_pixels"],
                minimum_ink_pixels=row["remeasured"]["minimum_ink_pixels"],
                minimum_fraction_outside_bp=row["remeasured"]["minimum_fraction_outside_bp"],
            )[1]
        )
    )


def _unmeasurable_ink_map_pages_from_validated_rows(
    rows: list[dict[str, Any]],
) -> tuple[int, ...]:
    """Every page whose Ink Map audit refused, derived from its closed rows."""
    return tuple(
        sorted(row["ordinal"] for row in rows if row["initial_outcome"] == INK_NOT_MEASURABLE)
    )


def edge_hold_pages_from_rows(rows: list[dict[str, Any]]) -> tuple[int, ...]:
    """The held set, recomputed from the recorded counts by the shared gate.

    Not stored, so a row's counts cannot disagree with an asserted boolean.
    """
    return _edge_hold_pages_from_validated_rows(
        _validate_ink_map_pages(rows, "an ink-map hold derivation")
    )


def _act_partition_claim(
    projection: ArmariumProjection, categories: list[dict[str, Any]]
) -> dict[str, Any]:
    """The act denominator, under the name of the thing that was actually counted."""
    return {
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


def _validate_not_measured_basis(basis: object) -> dict[str, Any]:
    """Refuse a not-measured basis that is not this build's closed shape.

    Checked before any product byte is written, against the claim's own detail
    field sets so the two cannot drift.
    """
    if not isinstance(basis, dict):
        raise SchemaRefusal(
            "an Armarium projection carries no not-measured basis; the export's "
            "`not_measured` block is derived from the run's own records and may not be "
            "built from nothing"
        )
    expected = frozenset({"schema", *NOT_MEASURED_INSTRUMENTS})
    record = _require_exact_fields(basis, expected, subject="an Armarium not-measured basis")
    if record["schema"] != NOT_MEASURED_BASIS_SCHEMA:
        raise SchemaRefusal("an Armarium not-measured basis is not this build's schema")
    for instrument in NOT_MEASURED_INSTRUMENTS:
        _validate_not_measured_detail(
            instrument,
            record[instrument],
            subject=f"the not-measured basis for {instrument}",
        )
    return record


def _not_measured_status(instrument: str, detail: dict[str, Any]) -> str:
    """One instrument's status, read off what this run actually recorded."""
    if instrument == _PERLECTOR_UNCERTAIN_SPANS:
        # An empty span list under `not-assessed` is an absence, not confidence.
        if detail["acts_assessed"] == detail["acts_delivered"] != 0:
            return "measured"
        if detail["acts_assessed"] == 0 and detail["acts_with_uncertain_spans"] == 0:
            return "declared-unproduced"
        # Partly measured: some readers assessed and some did not.
        return "not-measured"
    if instrument == _GEOMETRY_CALIBRATION:
        return (
            "measured"
            if detail["configurations"]
            and all(row["calibrated_for_this_corpus"] for row in detail["configurations"])
            else "not-measured"
        )
    if instrument == _PAGE_ACCOUNTING_THRESHOLDS:
        return "measured" if detail["calibrated_for_this_corpus"] else "not-measured"
    if instrument == _PASS_C:
        # No page-path stage audits a page reading, so a run whose every page
        # records `not-run` had no producer for this instrument at all.
        if detail["pages_audit_not_run"] == 0 and detail["pages_read"] > 0:
            return "measured"
        if detail["pages_audit_not_run"] == detail["pages_read"]:
            return "declared-unproduced"
        return "not-measured"
    if instrument == _COMPARISON_BOUNDS:
        return "measured" if detail["acts_with_unmeasured_comparison"] == 0 else "not-measured"
    raise SchemaRefusal(f"no not-measured status rule exists for {instrument!r}")


def _not_measured_claim(projection: ArmariumProjection) -> dict[str, Any]:
    """The export's own account of what this run did not measure."""
    basis = _validate_not_measured_basis(projection.not_measured_basis)
    entries = [
        {
            "instrument": instrument,
            "status": _not_measured_status(instrument, basis[instrument]),
            "detail": copy.deepcopy(basis[instrument]),
            "recorded_in": _NOT_MEASURED_RECORDED_IN[instrument],
        }
        for instrument in NOT_MEASURED_INSTRUMENTS
    ]
    return {
        "schema": NOT_MEASURED_SCHEMA,
        "count": sum(1 for entry in entries if entry["status"] != "measured"),
        "entries": entries,
    }


def _validate_projection(projection: ArmariumProjection) -> list[dict[str, Any]]:
    """Refuse a projection no run could produce; return its validated ink-map rows."""
    has_fixture = _is_nonempty_str(projection.fixture_id)
    has_submission = _is_nonempty_str(projection.submission_id)
    if not has_fixture and not has_submission:
        raise SchemaRefusal(
            "an Armarium projection has neither a fixture identifier nor a submission "
            "identifier; a run's export must be identified by exactly one"
        )
    if has_fixture and has_submission:
        raise SchemaRefusal(
            "an Armarium projection has both a fixture identifier and a submission "
            "identifier; a run's export must be identified by exactly one, never both"
        )
    if has_submission:
        # `common.stage.submission_identity` only produces a sha256; be as strict,
        # so a hand-typed corpus label cannot leave under this field.
        _require_sha256(projection.submission_id, "an Armarium projection submission identity")
    if not _is_nonempty_str(projection.scenario):
        raise SchemaRefusal("an Armarium projection has no scenario")
    _require_sha256(projection.config_digest, "an Armarium projection sealed configuration digest")
    if not is_plain_int(projection.expected_acts):
        raise SchemaRefusal("an Armarium projection expected-act count is not an integer")
    if len(projection.acts) != projection.expected_acts:
        raise SchemaRefusal(
            "an Armarium projection does not contain one record for every expected act"
        )
    _validate_witness_accounting(
        projection.witness_chairs,
        projection.witness_floor,
        projection.aggregate_basis,
        projection.acts + projection.other_readings,
    )
    not_measured_basis = _validate_not_measured_basis(projection.not_measured_basis)
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
    act_ids: set[str] = set()
    act_keys: set[str] = set()
    for act in projection.acts:
        if not isinstance(act, dict):
            raise SchemaRefusal("an Armarium projection act is not an object")
        act_id, act_key = act.get("act_id"), act.get("act_key")
        if not _is_line_safe_identity(act_id) or not _is_line_safe_identity(act_key):
            raise SchemaRefusal("an Armarium projection act lacks a line-safe act identity")
        if act_id in act_ids or act_key in act_keys:
            raise SchemaRefusal("an Armarium projection repeats an act identity")
        act_ids.add(act_id)
        act_keys.add(act_key)
        _validate_projection_act(act)
    _validate_page_projection(projection, act_ids | act_keys, sealed)
    if not_measured_basis[_PASS_C]["pages_read"] != len(sealed):
        raise SchemaRefusal(
            "an Armarium projection's Pass C basis does not count exactly its real sealed "
            "pages as read"
        )
    perlector_basis = not_measured_basis[_PERLECTOR_UNCERTAIN_SPANS]
    delivered_counts = _delivered_doubt_counts(projection.acts)
    if any(perlector_basis[field] != count for field, count in delivered_counts.items()):
        raise SchemaRefusal(
            "an Armarium projection's Perlector uncertainty basis does not exactly reconcile "
            "with its delivered act projection"
        )
    bounds = not_measured_basis[_COMPARISON_BOUNDS]
    delivered_ids = {
        act["act_id"]
        for act in projection.acts
        if act["category"] == ArmariumCategory.DELIVERED.value
    }
    if (
        bounds["acts_delivered"] != delivered_counts["acts_delivered"]
        or not set(bounds["unmeasured_act_ids"]) <= delivered_ids
    ):
        raise SchemaRefusal(
            "an Armarium projection's comparison-bounds basis does not count exactly its "
            "delivered acts, or names an unmeasured act it does not deliver"
        )
    # The run's verdict is computed from the basis, so its damage record must
    # match the delivered acts key for key.
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
        list(projection.continuation_joins),
        others=list(projection.other_readings),
        act_keys={act["act_id"]: act["act_key"] for act in projection.acts},
    )
    if canonical_text(projection.aggregate) != canonical_text(expected_aggregate):
        raise SchemaRefusal("an Armarium projection aggregate does not match its measured basis")
    return ink_map_rows


def _validate_page_projection(
    projection: ArmariumProjection, act_names: set[str], sealed: set[int]
) -> None:
    """The page path's other layer and page accounting, before any byte is written."""
    seen: set[str] = set()
    for other in projection.other_readings:
        if not isinstance(other, dict):
            raise SchemaRefusal("an Armarium projection other reading is not an object")
        act_id, act_key = other.get("act_id"), other.get("act_key")
        if not _is_line_safe_identity(act_id) or not _is_line_safe_identity(act_key):
            raise SchemaRefusal("an Armarium projection other reading lacks a line-safe identity")
        if {act_id, act_key} & (act_names | seen):
            raise SchemaRefusal(
                "an Armarium projection other reading shares an identity with an act or another "
                "other reading; an other reading is never counted as an act"
            )
        seen |= {act_id, act_key}
        if other.get("category") not in _OTHER_CATEGORIES:
            raise SchemaRefusal("an Armarium projection other reading has no reading category")
        if not is_plain_int(other.get("page_ordinal")) or other["page_ordinal"] not in sealed:
            raise SchemaRefusal("an Armarium projection other reading names no sealed page")
        _validate_projection_act(other)
    _page_accounting_claim(list(projection.page_accounting), sealed)
    _validate_act_readings(_act_readings(projection.acts), sealed, "an Armarium projection")


def _validate_page_accounting_rows(rows: Any, sealed: set[int], subject: str) -> list[dict]:
    """One text-free accounting row per real sealed page, closed and in page order."""
    if not isinstance(rows, list):
        raise SchemaRefusal(f"{subject} page accounting is not a list")
    for row in rows:
        _require_exact_fields(
            row, _PAGE_ACCOUNTING_ROW_FIELDS, subject=f"{subject} page accounting row"
        )
        rules, codes = row["rules"], row["hold_codes"]
        if (
            not is_plain_int(row["ordinal"])
            or not _is_nonempty_str(row["page_id"])
            or not isinstance(rules, dict)
            or not rules
            or any(
                not _is_nonempty_str(name) or not _is_nonempty_str(status)
                for name, status in rules.items()
            )
            or list(rules) != sorted(rules)
            or not isinstance(codes, list)
            or not all(_is_nonempty_str(code) for code in codes)
            or codes != sorted(set(codes))
        ):
            raise SchemaRefusal(f"{subject} page accounting row is malformed")
        _require_sha256(row["policy_sha256"], f"{subject} page accounting policy digest")
    ordinals = [row["ordinal"] for row in rows]
    if ordinals != sorted(sealed):
        raise SchemaRefusal(
            f"{subject} page accounting does not name every real sealed page once, in order"
        )
    return rows


def _page_accounting_claim(rows: list[dict[str, Any]], sealed: set[int]) -> dict[str, Any]:
    """Each page's rule statuses, hold codes and policy digest, derived from its rows."""
    rows = _validate_page_accounting_rows(rows, sealed, "an Armarium")
    return {
        "denominator": _PAGE_ACCOUNTING_DENOMINATOR,
        "pages": copy.deepcopy(rows),
        "held_pages": [row["ordinal"] for row in rows if row["hold_codes"]],
        "policy_sha256s": sorted({row["policy_sha256"] for row in rows}),
    }


def _other_outcomes(others: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Each other reading's text-free terminal record, beside the act outcomes."""
    return [
        {
            **_row_head(other),
            "page_ordinal": other["page_ordinal"],
            "text_status": other.get("text_status"),
        }
        for other in sorted(others, key=lambda item: item["act_id"])
    ]


def _other_readings_claim(
    outcomes: list[dict[str, Any]], formats: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    """The other layer's count and categories, derived from its outcome rows."""
    counts = Counter(row["category"] for row in outcomes)
    return {
        "layer": _OTHER_READINGS_LAYER,
        "counted_as_acts": False,
        "count": len(outcomes),
        "by_category": {category: counts[category] for category in sorted(counts)},
        "act_ids": sorted(row["act_id"] for row in outcomes),
        "carried_by": sorted(set(formats) & set(_OTHER_READING_FORMATS)),
    }


def _act_readings(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Each page-read act's page and the reading it came from, in act-id order."""
    return [
        {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "page_ordinal": _key_page(act["act_key"]),
            "reading": act["reading"],
        }
        for act in sorted(acts, key=lambda item: item["act_id"])
    ]


def _key_page(act_key: Any) -> int | None:
    """The page an act key names (`p<page>:...`), or `None` for a key of no page."""
    match = _PAGE_ACT_KEY.fullmatch(act_key) if isinstance(act_key, str) else None
    return None if match is None else int(match[1])


def _validate_act_readings(rows: Any, sealed: set[int], subject: str) -> dict[str, str | None]:
    """Each act's reading, by act id.

    An entry's row (`p<page>:<n>`) names a label of `_ACT_READINGS`, a row with no
    entry (`p<page>:unread` or `p<page>:blank`) names null, and each row's page is
    the sealed page its key names.
    """
    if not isinstance(rows, list):
        raise SchemaRefusal(f"{subject} act readings are not a list")
    readings: dict[str, str | None] = {}
    for row in rows:
        _require_exact_fields(row, _ACT_READING_FIELDS, subject=f"{subject} act reading")
        key = row["act_key"]
        match = _PAGE_ACT_KEY.fullmatch(key) if isinstance(key, str) else None
        if (
            not _is_nonempty_str(row["act_id"])
            or row["act_id"] in readings
            or match is None
            or not is_plain_int(row["page_ordinal"])
            or row["page_ordinal"] != int(match[1])
            or row["page_ordinal"] not in sealed
            or (row["reading"] in _ACT_READINGS) != (match[2] is not None)
        ):
            raise SchemaRefusal(
                f"{subject} act reading names no act on the sealed page its key names, or a "
                f"reading other than one of {list(_ACT_READINGS)} for an entry and null for a "
                "row with no entry"
            )
        readings[row["act_id"]] = row["reading"]
    if list(readings) != sorted(readings):
        raise SchemaRefusal(f"{subject} act readings are not in act-id order")
    return readings


def _reask_claim(rows: list[dict[str, Any]], ordinals: list[int]) -> dict[str, Any]:
    """The acts read on re-ask, counted apart from first-reading acts, per page and in total.

    A run with operator re-reads counts the acts they read apart too.
    """

    def count(reading: str, ordinal: int | None = None) -> int:
        return sum(
            row["reading"] == reading and (ordinal is None or row["page_ordinal"] == ordinal)
            for row in rows
        )

    reread = count(OPERATOR_REREAD_LABEL) > 0

    def rereads(ordinal: int | None = None) -> dict[str, int]:
        return (
            {"read_on_operator_reread_acts": count(OPERATOR_REREAD_LABEL, ordinal)}
            if reread
            else {}
        )

    return {
        "label": READ_ON_REASK_LABEL,
        "first_reading_acts": count(FIRST_READING_LABEL),
        "read_on_reask_acts": count(READ_ON_REASK_LABEL),
        **rereads(),
        "read_on_reask_act_ids": sorted(
            row["act_id"] for row in rows if row["reading"] == READ_ON_REASK_LABEL
        ),
        "pages": [
            {
                "ordinal": ordinal,
                "first_reading_acts": count(FIRST_READING_LABEL, ordinal),
                "read_on_reask_acts": count(READ_ON_REASK_LABEL, ordinal),
                **rereads(ordinal),
            }
            for ordinal in ordinals
        ],
    }


def _validate_projection_act(act: dict[str, Any]) -> None:
    """One act's text, provenance and approval, as its category requires."""
    category = act.get("category")
    if category not in _KNOWN_CATEGORIES:
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
        # `utf8_round_trip` also runs `validate_uncertainty`.
        utf8_round_trip(act.get("uncertainty"), literal)
        _require_damage_record(
            act.get("text_status"),
            act.get("uncertainty"),
            literal,
            subject="Armarium projection act",
        )
    elif literal is not None:
        raise SchemaRefusal("a non-delivered act may not carry purported clean text")
    elif act.get("uncertainty") is not None:
        # Offsets into a text the act does not have.
        raise SchemaRefusal("a non-delivered act may not carry an uncertainty layer")
    elif act.get("text_status") is not None:
        # An act with no Archetypus record has no status.
        raise SchemaRefusal("a non-delivered act may not carry an established-text status")
    if category == ArmariumCategory.EXCLUDED_WITH_APPROVAL.value:
        require_approval(ARMARIUM, category, act.get("approval_ref"))


def _delivered_doubt_counts(acts: tuple[dict[str, Any], ...]) -> dict[str, int]:
    """The Perlector uncertainty basis's four counts, taken from the delivered acts.

    Counted by state, never by subtraction, so a broken doubt report is not
    counted as a person's correction (`not-assessed`). Any other state (the Recensor's `malformed`,
    for one) is refused: the Recensor holds those, so a delivered one means the
    projection did not come from a run.
    """
    counts = dict.fromkeys(
        ("acts_delivered", "acts_with_uncertain_spans", "acts_assessed", "acts_not_assessed"), 0
    )
    for act in acts:
        if act["category"] != ArmariumCategory.DELIVERED.value:
            continue
        counts["acts_delivered"] += 1
        uncertainty = act.get("uncertainty")
        if isinstance(uncertainty, dict) and uncertainty.get("uncertain_spans"):
            counts["acts_with_uncertain_spans"] += 1
        assessment = uncertainty.get("assessment") if isinstance(uncertainty, dict) else None
        state = assessment.get("state") if isinstance(assessment, dict) else None
        if state == "assessed":
            counts["acts_assessed"] += 1
        elif state == "not-assessed":
            counts["acts_not_assessed"] += 1
        else:
            raise SchemaRefusal(
                f"an Armarium projection delivers act {act.get('act_key')!r} whose sealed doubt "
                f"assessment is {state!r}; only a reading that was assessed, or one a person "
                "corrected, is deliverable -- a doubt report that could not be anchored is "
                "held for review, never counted"
            )
    return counts


def _require_damage_record(
    text_status: Any,
    uncertainty: Any,
    literal: str,
    *,
    subject: str,
) -> None:
    """Recompute a delivered act's text status from the uncertainty layer beside it.

    A carried status is never believed: a package must not say `established` over
    an act whose own gap list records unread ink. A reading with no text and no
    gap is never delivered: an empty reading is held before the Archetypus.
    """
    # Type before membership, so an unhashable JSON value is refused, not raised.
    if not isinstance(text_status, str) or text_status not in TEXT_STATUSES:
        raise SchemaRefusal(
            f"a delivered {subject} carries established-text status {text_status!r}, which is "
            f"not one of {sorted(TEXT_STATUSES)}"
        )
    try:
        expected = derive_record_text_status(literal, uncertainty)
    except SchemaRefusal as error:
        raise SchemaRefusal(
            f"a delivered {subject}'s uncertainty layer cannot be read for the status of its text"
        ) from error
    if expected == "no_readable_text":
        raise SchemaRefusal(
            f"a delivered {subject} has no text and no gap; an empty reading is held for "
            "review, never delivered"
        )
    if text_status != expected:
        raise SchemaRefusal(
            f"a delivered {subject} claims established-text status {text_status!r} over an "
            f"uncertainty layer that says {expected!r}; a damaged act may not be projected as "
            "a whole one"
        )


def _validate_witness_accounting(
    witness_chairs: Any,
    witness_floor: Any,
    aggregate_basis: Any,
    acts: tuple[dict[str, Any], ...] | None = None,
) -> None:
    """Keep the exported roster, coverage counts, and per-act witnesses one fact.

    Only the page-scoped witnesses read a page: the basis names them
    (`page_witness_chairs`, part of the roster), each coverage record counts
    exactly them, and a reading's witnesses are a non-empty part of them.
    """
    if (
        not isinstance(witness_chairs, (list, tuple))
        or any(not _is_nonempty_str(chair) for chair in witness_chairs)
        or len(set(witness_chairs)) != len(witness_chairs)
    ):
        raise SchemaRefusal("Armarium witness chairs are not a unique named roster")
    if not _is_count(witness_floor) or witness_floor > len(witness_chairs):
        raise SchemaRefusal("Armarium witness floor does not fit its named roster")
    counted = (
        aggregate_basis.get("page_witness_chairs") if isinstance(aggregate_basis, dict) else None
    )
    if (
        not isinstance(counted, list)
        or not counted
        or any(not _is_nonempty_str(chair) for chair in counted)
        or counted != sorted(set(counted))
        or not set(counted) <= set(witness_chairs)
    ):
        raise SchemaRefusal(
            "Armarium page witness chairs are not a sorted, unique part of the roster"
        )
    basis = aggregate_basis if isinstance(aggregate_basis, dict) else {}
    routed = basis.get(_ROUTED_WITNESS_BASIS_FIELD, [])
    if (
        not isinstance(routed, list)
        or any(not _is_nonempty_str(chair) for chair in routed)
        or routed != sorted(set(routed))
        or not set(routed) < set(counted)
        or (_ROUTED_WITNESS_BASIS_FIELD in basis and not routed)
    ):
        raise SchemaRefusal(
            "Armarium routed page witness chairs are not a sorted, unique, proper part of the "
            "page witness chairs"
        )
    # A page a routed chair is not routed to counts the other page witnesses alone.
    page_rosters = {len(counted), len(counted) - len(routed)}
    coverage = (
        aggregate_basis.get("coverage_records") if isinstance(aggregate_basis, dict) else None
    )
    if not isinstance(coverage, dict):
        raise SchemaRefusal("Armarium witness accounting has no coverage records")
    for act_key, record in coverage.items():
        if (
            not isinstance(record, dict)
            or record.get("configured") not in page_rosters
            or record.get("floor") != witness_floor
        ):
            raise SchemaRefusal(
                f"Armarium witness coverage for {act_key!r} disagrees with the exported roster"
            )
    if acts is None:
        return
    expected = set(counted)
    for act in acts:
        if act.get("category") != ArmariumCategory.DELIVERED.value:
            continue
        witnesses = act.get("witnesses")
        if not isinstance(witnesses, list):
            raise SchemaRefusal("a delivered act has no witness provenance list")
        chairs = [item.get("chair") for item in witnesses if isinstance(item, dict)]
        if (
            len(chairs) != len(witnesses)
            or not chairs
            or not set(chairs) <= expected
            or len(set(chairs)) != len(chairs)
        ):
            raise SchemaRefusal("a delivered act's witness provenance disagrees with the roster")


_AGGREGATE_BASIS_FIELDS: Final = (
    "coverage_records",
    "unaddressed_chairs",
    "act_pages",
    "act_text_status",
    "continuation_flags",
    "page_witness_chairs",
)
# Present only on a run whose Recensor applied operator review decisions.
_REVIEW_DECISIONS_BASIS_FIELD: Final = "review_decisions"
# Present only when the run's held share after the Recensor was above its
# sealed limit and a person's advance passed it (`systemic_aggregate_argument`).
_SYSTEMIC_BASIS_FIELD: Final = "systemic_review"
# Present only when the run seats a witness on routed pages only
# (`common/witness_routing.py`): those chairs, a sorted part of
# `page_witness_chairs`. A page they are not routed to counts the rest alone.
_ROUTED_WITNESS_BASIS_FIELD: Final = "routed_page_witness_chairs"
_OPTIONAL_BASIS_FIELDS: Final = frozenset(
    {_REVIEW_DECISIONS_BASIS_FIELD, _SYSTEMIC_BASIS_FIELD, _ROUTED_WITNESS_BASIS_FIELD}
)


def review_aggregate_arguments(basis: Any) -> dict[str, Any]:
    """`run_aggregate`'s review arguments from a basis's `review_decisions`; none when absent.

    `{clearances, page_holds, corrections}`: the `review_clearances` rows,
    each page still held after review as `{page, codes}`, and the key of each
    reading a person corrected. `run_aggregate` checks the rows themselves; a
    clearance or page hold only adds a reason, so a package cannot read as
    complete by carrying one. A correction is no reason: the person's text is
    the truth (`_verify_corrections` holds each to its labelled reading).
    """
    if basis is None:
        return {}
    if (
        not isinstance(basis, dict)
        or set(basis) != {"clearances", "page_holds", "corrections"}
        or not isinstance(basis["clearances"], list)
        or not isinstance(basis["page_holds"], list)
        or not isinstance(basis["corrections"], list)
        or not all(_is_nonempty_str(key) for key in basis["corrections"])
        or basis["corrections"] != sorted(set(basis["corrections"]))
        or not all(
            isinstance(row, dict) and set(row) == {"page", "codes"} for row in basis["page_holds"]
        )
    ):
        raise SchemaRefusal("an Armarium aggregate's review decisions are malformed")
    holds = {row["page"]: row["codes"] for row in basis["page_holds"]}
    if len(holds) != len(basis["page_holds"]):
        raise SchemaRefusal("an Armarium aggregate's review decisions name a page twice")
    return {"review_clearances": basis["clearances"], "review_page_holds": holds}


def systemic_aggregate_argument(basis: Any) -> dict[str, Any]:
    """`run_aggregate`'s `systemic_review` from a basis's `systemic_review`; none when absent.

    The record names a held share above its limit, checked exactly here, so a
    package cannot carry a systemic reason its own counts do not support;
    `run_aggregate` checks the record's shape.
    """
    if basis is None:
        return {}
    try:
        limit = parse_share(basis["max_held_page_share"], "a systemic review's limit")
        exceeds = Fraction(len(basis["held_pages"]), basis["pages"]) > limit
    except (ContractError, KeyError, TypeError, ZeroDivisionError) as error:
        raise SchemaRefusal("an Armarium aggregate's systemic review is malformed") from error
    if not exceeds:
        raise SchemaRefusal(
            "an Armarium aggregate's systemic review names a held share within its limit"
        )
    return {"systemic_review": basis}


def _validated_continuation_flags(flags: Any, categories: dict[str, str]) -> dict[str, list[str]]:
    """The basis's continuation flags: delivered acts, each its raised flags in order."""
    if not isinstance(flags, dict) or any(
        not _is_nonempty_str(act_key)
        or categories.get(act_key) != ArmariumCategory.DELIVERED.value
        or not isinstance(raised, list)
        or not raised
        or not all(isinstance(flag, str) and flag in CONTINUATION_FLAGS for flag in raised)
        or raised != sorted(set(raised))
        for act_key, raised in flags.items()
    ):
        raise SchemaRefusal(
            "an Armarium aggregate basis's continuation flags are not the raised flags of "
            "delivered acts"
        )
    return flags


def unpaired_continuations(
    flags: dict[str, list[str]], joins: list[dict[str, Any]], act_keys: dict[str, str]
) -> list[tuple[str, str]]:
    """Each raised continuation flag of a delivered act that no join row has as a side.

    A join's head continues onto the next page and its tail from the previous
    one; a flag with no join (at the run's edge, say) is named, never dropped.
    """
    paired = {
        (act_keys.get(act_id), "continues_to_next_page")
        for join in joins
        for act_id in join["head_act_ids"]
    } | {
        (act_keys.get(act_id), "continues_from_previous_page")
        for join in joins
        for act_id in join["tail_act_ids"]
    }
    return [
        (act_key, flag)
        for act_key in sorted(flags)
        for flag in flags[act_key]
        if (act_key, flag) not in paired
    ]


def _aggregate_from_basis(
    categories: dict[str, str],
    pages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    basis: Any,
    edge_hold_pages: tuple[int, ...],
    continuation_joins: list[dict[str, Any]] | None,
    *,
    others: list[dict[str, Any]],
    act_keys: dict[str, str],
) -> dict[str, Any]:
    """Recompute an Armarium aggregate from its retained, non-text inputs.

    `others` is the other layer (`{page_ordinal, category}` rows). The basis's
    `continuation_flags` are read against the joins through `act_keys` (act id
    to act key); its `page_witness_chairs` are checked by
    `_validate_witness_accounting`.
    """
    if not isinstance(basis, dict) or set(basis) - _OPTIONAL_BASIS_FIELDS != set(
        _AGGREGATE_BASIS_FIELDS
    ):
        raise SchemaRefusal("an Armarium aggregate has no recognized accounting basis")
    review = review_aggregate_arguments(basis.get(_REVIEW_DECISIONS_BASIS_FIELD))
    review.update(systemic_aggregate_argument(basis.get(_SYSTEMIC_BASIS_FIELD)))
    by_page: dict[int, list[str]] = {}
    for other in others:
        by_page.setdefault(other["page_ordinal"], []).append(other["category"])
    flags = _validated_continuation_flags(basis["continuation_flags"], categories)
    try:
        unpaired = unpaired_continuations(flags, continuation_joins or [], act_keys)
    except (KeyError, TypeError) as error:
        raise SchemaRefusal("an Armarium aggregate's joins cannot be read for pairing") from error
    coverage, chairs, act_pages, act_text_status = (
        basis.get("coverage_records"),
        basis.get("unaddressed_chairs"),
        basis.get("act_pages"),
        basis.get("act_text_status"),
    )
    if (
        not isinstance(coverage, dict)
        or not isinstance(chairs, list)
        or not all(_is_nonempty_str(chair) for chair in chairs)
        or not isinstance(act_pages, dict)
        or not isinstance(act_text_status, dict)
    ):
        raise SchemaRefusal("an Armarium aggregate basis is malformed")
    normalized_categories: dict[str, ArmariumCategory] = {}
    for act_key, category in categories.items():
        if not _is_nonempty_str(act_key):
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
            continuation_joins=continuation_joins,
            other_categories_by_page=by_page,
            unpaired_continuations=unpaired,
            **review,
        )
    # The basis may come from an untrusted package and `run_aggregate` reads
    # coverage-record keys nothing above checks. The cause stays chained.
    except (ContractError, KeyError, TypeError) as error:
        raise SchemaRefusal("an Armarium aggregate basis cannot be reconciled") from error


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
    if not is_plain_int(ordinal):
        raise SchemaRefusal(f"a {subject} source region has no source-page ordinal")
    if not isinstance(region.get("source_page_id"), str) or not region["source_page_id"]:
        raise SchemaRefusal(f"a {subject} source region has no source-page identity")
    transform = region.get("transform")
    if not isinstance(transform, dict) or transform.get("operation") != "crop":
        raise SchemaRefusal(f"a {subject} source region has no crop transform")


def _pages_by_ordinal(
    pages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Index the measured page census before accepting a crop's claimed parent."""
    indexed: dict[int, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise SchemaRefusal("an export page census row is not an object")
        ordinal = page.get("ordinal")
        if not is_plain_int(ordinal) or ordinal in indexed:
            raise SchemaRefusal("an export page census has no unique integer ordinal")
        indexed[ordinal] = page
    return indexed


def _manifest_run_binding(projection: ArmariumProjection) -> dict[str, str]:
    """The manifest's `run` block, under whichever identity name this run carries.

    `_validate_projection` has already required exactly one identity, so the
    fallback needs no second check.
    """
    if _is_nonempty_str(projection.submission_id):
        identity = {"submission_id": projection.submission_id}
    else:
        identity = {"fixture_id": projection.fixture_id}
    return {
        **identity,
        "scenario": projection.scenario,
        "config_digest": projection.config_digest,
        "lot": projection.lot,
    }


def _require_lot(lot: object, enabled: bool, *, subject: str) -> None:
    """A lot exactly when the sealed formats turn it on, and then a well-formed one."""
    if enabled and not is_lot(lot):
        raise SchemaRefusal(
            f"{subject} carries no well-formed lot, and the sealed formats ask for one"
        )
    if not enabled and lot is not None:
        raise SchemaRefusal(f"{subject} carries a lot, and the sealed formats turn lots off")


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
    for act in projection.acts + projection.other_readings:
        for region in act.get("source_regions", []):
            _validate_cited_region(region, subject="exported act")
            _verify_region_page_binding(region, pages, subject="exported act")


def _source_rows(
    pages: tuple[dict[str, Any], ...],
    embed_pixels: bool,
    read_bytes: Callable[[str], bytes],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    rows: list[dict[str, Any]] = []
    embedded: dict[str, bytes] = {}
    # Ordinals were already checked by `_validate_projection_region_bindings`.
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
            if not embed_pixels:
                _validate_run_relative_path(image_path)
            row["page_image"] = _image_reference(
                image_path,
                image_sha256,
                f"pixels/pages/{ordinal}.img",
                embed_pixels,
                read_bytes,
                embedded,
                changed="a sealed page changed while its export was being built",
            )
        rows.append(row)
    return rows, embedded


def _image_reference(
    path: str,
    sha256: str,
    member: str,
    embed_pixels: bool,
    read_bytes: Callable[[str], bytes],
    embedded: dict[str, bytes],
    *,
    changed: str,
    collision: str | None = None,
) -> dict[str, str]:
    """Cite one image by its run path, or embed its re-verified bytes as ``member``."""
    if not embed_pixels:
        return {
            "availability": _SOURCE_ACCESS_REQUIRED,
            "run_relative_path": path,
            "sha256": sha256,
        }
    pixels = read_bytes(path)
    if digest_bytes(pixels) != sha256:
        raise SchemaRefusal(changed)
    if collision is not None and embedded.get(member, pixels) != pixels:
        raise SchemaRefusal(collision)
    embedded[member] = pixels
    return {"availability": _EMBEDDED, "member_path": member, "sha256": sha256}


def _text_bundle_members(
    acts: tuple[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    ledger: dict[str, Any],
    joins: tuple[dict[str, Any], ...] = (),
    others: tuple[dict[str, Any], ...] = (),
    coniector_rows: tuple[dict[str, Any], ...] = (),
    operator_rows: tuple[dict[str, Any], ...] = (),
    model_readings: dict[str, dict[str, Any]] | None = None,
    lot: str | None = None,
) -> dict[str, bytes]:
    """Write one readable file for every cited source folder.

    Each file opens with the run's status (the terminal `ledger`'s), its lot when
    the run has one, and how many of its readings were delivered, and ends with a text-free `## NOT DELIVERED`
    section for every unresolved page or unsealed source in the folder and every
    reading on its pages that was not delivered, so a partial run never reads as
    complete. The verifier renders every file again with this function and
    requires the same bytes.

    A reading an operator acted on carries its operator lines, and a corrected
    one the model's reading beside the person's (`model_readings`, by id).

    A folder with only holds or refusals still gets a file, with no invented
    reading in it. A delivered other reading follows the acts of its folder in
    its own `## OTHER <key> (not an act)` section, whose fields are named apart
    from an act's so no act parser reads one as an act.
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
        # An act may cite regions from several folders; its text goes into each
        # rather than letting region order pick one.
        source_folders = sorted(
            {
                _source_folder_for_declared_path(region["declared_path"])
                for region in act["source_regions"]
            }
        )
        for folder in source_folders:
            folders.add(folder)
            grouped[folder].append(act)
    other_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for other in others:
        if other["category"] != ArmariumCategory.DELIVERED.value:
            continue
        for folder in sorted(
            {
                _source_folder_for_declared_path(region["declared_path"])
                for region in other["source_regions"]
            }
        ):
            folders.add(folder)
            other_groups[folder].append(other)
    act_keys = {act["act_id"]: act["act_key"] for act in acts}
    notes = _join_notes(joins, act_keys)
    beneath = {anchor_act(row): row for row in coniector_rows if row["unit"] == "act"}
    released = {row["act_id"]: row for row in operator_rows}
    models = model_readings or {}
    members: dict[str, bytes] = {}
    not_delivered = _not_delivered_by_folder(
        [
            _not_delivered_row(reading, kind)
            for reading, kind in _readings_by_kind(acts, others)
            if reading["category"] != ArmariumCategory.DELIVERED.value
        ],
        source_rows,
    )
    unresolved_pages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _unresolved_page_rows(ledger["units"]):
        unresolved_pages[_source_folder_for_declared_path(row["declared_path"])].append(row)
    for folder in sorted(folders):
        records = grouped[folder]
        lines = [
            f"# Armarium text bundle — source folder: {folder or '.'}",
            *_folder_status_lines(
                ledger["status"],
                len(records) + len(other_groups[folder]),
                len(not_delivered[folder]),
                lot,
            ),
            "",
        ]
        for act in sorted(records, key=lambda item: act_key_sort_key(item["act_key"])):
            regions = act["source_regions"]
            lines.extend([f"## {act['act_key']} ({act['act_id']})", f"act-id: {act['act_id']}"])
            lines.append(f"{_READING_PREFIX}{act['reading']}")
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
                    "diplomatic:",
                    json.dumps(
                        diplomatic_display(act[CANONICAL_TEXT_FIELD], act["uncertainty"]),
                        ensure_ascii=False,
                    ),
                    "uncertainty:",
                    json.dumps(act["uncertainty"], ensure_ascii=False, sort_keys=True),
                    f"text_status: {act['text_status']}",
                    *notes.get(act["act_id"], []),
                    *(
                        lines_for(released[act["act_id"]], models.get(act["act_id"]))
                        if act["act_id"] in released
                        else []
                    ),
                    *(
                        reconstruction_lines(beneath[act["act_id"]])
                        if act["act_id"] in beneath
                        else []
                    ),
                    "",
                ]
            )
        in_folder = {act["act_id"] for act in records}
        for row in coniector_rows:
            if row["unit"] == "join" and anchor_act(row) in in_folder:
                lines.extend(join_section(row))
        for other in sorted(
            other_groups[folder], key=lambda item: act_key_sort_key(item["act_key"])
        ):
            lines.extend(
                _other_section(other, released.get(other["act_id"]), models.get(other["act_id"]))
            )
        for row in unresolved_pages[folder]:
            lines.extend(_unresolved_page_section(row))
        for row in not_delivered[folder]:
            lines.extend(_not_delivered_section(row))
        members[_text_member_path(folder)] = "\n".join(lines).encode("utf-8")
    return members


def _unresolved_page_rows(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every unresolved sealed page, and every unsealed source, from the ledger's units.

    A sealed page's source unit shares its page unit's category, so only the
    page is named.
    """
    rows = []
    for unit in units:
        unit_type, ordinal = unit["unit_id"].split(":", 1)
        if unit["category"] in _COMPLETED_CATEGORIES or not (
            unit_type == "page"
            or (
                unit_type == "source"
                and unit["category"] == ArmariumCategory.REFUSED_WITH_REASON.value
            )
        ):
            continue
        rows.append(
            {
                "kind": unit_type,
                "ordinal": int(ordinal),
                "category": unit["category"],
                "reason": unit["reason"],
                "declared_path": unit["declared_path"],
            }
        )
    return sorted(rows, key=lambda row: row["ordinal"])


def _unresolved_page_section(row: dict[str, Any]) -> list[str]:
    """One text-free section for a page or source the run did not resolve."""
    return [
        f"{_NOT_DELIVERED_PREFIX}{row['kind']} {row['ordinal']}",
        f"not-delivered: {row['kind']} {row['category']}",
        f"not-delivered-reason: {json.dumps(row['reason'], ensure_ascii=False)}",
        "",
    ]


_NOT_DELIVERED_PREFIX: Final = "## NOT DELIVERED "


def _folder_status_lines(
    status: str, delivered: int, not_delivered: int, lot: str | None
) -> list[str]:
    """A text-bundle file's opening lines: the run's status, its lot and this folder's count."""
    said = "" if status == "complete" else " (EXPORT_MANIFEST.json claims.partial_reasons says why)"
    return [
        f"run-status: {status}{said}",
        *([f"lot: {lot}"] if lot is not None else []),
        f"folder-readings: {delivered} delivered, {not_delivered} not delivered",
    ]


def _not_delivered_row(reading: dict[str, Any], kind: str) -> dict[str, Any]:
    """What a text bundle says of a reading it does not deliver: who, where, and why."""
    page = _key_page(reading["act_key"]) if kind == "act" else reading["page_ordinal"]
    return {
        **_row_head(reading),
        "kind": kind,
        "page_ordinal": page,
        "approval_ref": reading.get("approval_ref"),
    }


def _not_delivered_by_folder(
    rows: list[dict[str, Any]], pages: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Each not-delivered reading under the folder of the page it was read on, in reading order."""
    paths = {page["ordinal"]: page["declared_path"] for page in pages}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda item: act_key_sort_key(item["act_key"])):
        if row["page_ordinal"] not in paths:
            raise SchemaRefusal(f"the not-delivered reading {row['act_key']} names no source page")
        grouped[_source_folder_for_declared_path(paths[row["page_ordinal"]])].append(row)
    return grouped


def _not_delivered_section(row: dict[str, Any]) -> list[str]:
    """One text-free section for a reading the package does not deliver."""
    return [
        f"{_NOT_DELIVERED_PREFIX}{row['act_key']} ({row['act_id']})",
        f"not-delivered: {row['kind']} {row['category']}",
        f"not-delivered-reason: {json.dumps(row['reason'], ensure_ascii=False)}",
        *(
            [f"not-delivered-approval: {json.dumps(row['approval_ref'], ensure_ascii=False)}"]
            if row["approval_ref"] is not None
            else []
        ),
        "",
    ]


def _verify_text_bundle_rendering(
    root: Path, manifest: dict[str, Any], sources: dict[str, Any]
) -> None:
    """Every text-bundle file is exactly what this build's writer renders for the package.

    The writer is fed only what verification has already checked: the
    accounting, citations, readings, joins, operator rows and model readings of
    `sources.json`, the manifest's ledger, and each delivered reading's literal,
    uncertainty layer and status as the text bundle carries them (hash- and
    damage-checked by the parsers) with the reconstruction rows it shows (each
    recomputed). Any other line, a changed title, or a moved section is refused.
    """
    act_texts = _text_bundle_records(root, sources["pages"])
    other_texts = _text_bundle_other_records(root, sources["pages"])
    readings = {row["act_id"]: row["reading"] for row in sources["act_readings"]}

    def projected(outcome: dict[str, Any], citations: dict[str, Any], text: Any) -> dict[str, Any]:
        reading = {**outcome, CANONICAL_TEXT_FIELD: None}
        if outcome["category"] == ArmariumCategory.DELIVERED.value:
            literal, uncertainty, status = text
            reading.update(
                {
                    CANONICAL_TEXT_FIELD: literal,
                    "uncertainty": uncertainty,
                    "text_status": status,
                    "source_regions": citations[outcome["act_id"]]["source_regions"],
                }
            )
        return reading

    act_citations = _act_citation_sources(sources)
    acts = tuple(
        {
            **projected(
                outcome,
                act_citations,
                (record.literal, record.uncertainty, record.text_status)
                if (record := act_texts.get(act_id)) is not None
                else None,
            ),
            "reading": readings.get(act_id),
        }
        for act_id, outcome in _act_outcome_sources(sources).items()
    )
    other_citations = _act_citation_sources(sources, "other_citations")
    others = tuple(
        projected(
            outcome,
            other_citations,
            (record[1], record[4], record[5])
            if (record := other_texts.get(act_id)) is not None
            else None,
        )
        for act_id, outcome in _other_outcome_sources(sources).items()
    )
    folders = sorted(
        {_source_folder_for_declared_path(page["declared_path"]) for page in sources["pages"]}
    )
    files = {
        folder: _package_lines(root / _text_member_path(folder), "text bundle")
        for folder in folders
    }
    shown = {
        tuple(row["act_ids"]): row
        for lines in files.values()
        for _place, row in text_bundle_placements(lines)[1]
    }
    try:
        expected = _text_bundle_members(
            acts,
            sources["pages"],
            manifest["claims"]["terminal_ledger"],
            tuple(sources["continuation_joins"] or ()),
            others,
            tuple(shown[tuple(act_ids)] for act_ids in sources["reconstructions"] or ()),
            tuple(sources[OPERATOR_SOURCES_FIELD] or ()),
            {model["act_id"]: model for model in sources[MODEL_READINGS_FIELD] or ()},
            manifest["run"]["lot"],
        )
    except (KeyError, TypeError) as error:
        raise SchemaRefusal(
            "the text bundle cannot be rendered again from the package's own accounting"
        ) from error
    for folder in folders:
        path = _text_member_path(folder)
        if "\n".join(files[folder]).encode("utf-8") != expected.get(path):
            raise SchemaRefusal(
                f"the text bundle for folder {folder or '.'!r} is not exactly what this build "
                "writes for the package's own accounting"
            )


_OTHER_SECTION_PREFIX: Final = "## OTHER "
_OTHER_SECTION_SUFFIX: Final = " (not an act)"


def _other_section(
    other: dict[str, Any],
    operator_row: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
) -> list[str]:
    """One delivered other reading, labelled as not an act, with its own field names.

    A reading a person released or corrected carries its operator lines last.
    """
    literal = other[CANONICAL_TEXT_FIELD]
    lines = [
        f"{_OTHER_SECTION_PREFIX}{other['act_key']}{_OTHER_SECTION_SUFFIX}",
        f"other-id: {other['act_id']}",
    ]
    for region in other["source_regions"]:
        lines.extend(
            [
                f"other-source-page: {region['declared_path']}",
                f"other-source-sha256: {region['declared_sha256']}",
            ]
        )
    return [
        *lines,
        f"other_text_sha256: {canonical_text_sha256(literal)}",
        "other_text:",
        json.dumps(literal, ensure_ascii=False),
        "other_diplomatic:",
        json.dumps(diplomatic_display(literal, other["uncertainty"]), ensure_ascii=False),
        "other_uncertainty:",
        json.dumps(other["uncertainty"], ensure_ascii=False, sort_keys=True),
        f"other_text_status: {other['text_status']}",
        *(lines_for(operator_row, model) if operator_row is not None else []),
        "",
    ]


def _section_field(block: list[str], position: int, prefix: str) -> str:
    """One line of an OTHER section, which must carry the named field."""
    if position >= len(block) or not block[position].startswith(prefix):
        raise SchemaRefusal(f"a text-bundle OTHER section has no {prefix.strip()!r} line")
    return block[position].removeprefix(prefix)


def _text_bundle_other_records(
    root, source_pages: list[dict[str, Any]]
) -> dict[str, tuple[str, str, str, tuple[tuple[str, str], ...], Any, str]]:
    """Every OTHER section, as `{act_id: (key, text, digest, citations, uncertainty, status)}`.

    Each section is read field by field in the order its writer uses; one repeated
    across folders must repeat identically.
    """
    folders = {_source_folder_for_declared_path(page["declared_path"]) for page in source_pages}
    known = {(page["declared_path"], page["declared_sha256"]) for page in source_pages}
    records: dict[str, tuple] = {}
    for folder in sorted(folders):
        lines = _package_lines(root / _text_member_path(folder), "text bundle")
        in_folder: set[str] = set()
        index = 0
        while index < len(lines):
            heading = lines[index]
            index += 1
            if not heading.startswith(_OTHER_SECTION_PREFIX):
                continue
            if not heading.endswith(_OTHER_SECTION_SUFFIX):
                raise SchemaRefusal("a text-bundle OTHER heading does not say it is not an act")
            act_key = heading.removeprefix(_OTHER_SECTION_PREFIX).removesuffix(
                _OTHER_SECTION_SUFFIX
            )
            block = lines[index:]

            act_id = _section_field(block, 0, "other-id: ")
            position, citations = 1, []
            while position < len(block) and block[position].startswith("other-source-page: "):
                citation = (
                    _section_field(block, position, "other-source-page: "),
                    _section_field(block, position + 1, "other-source-sha256: "),
                )
                if citation not in known:
                    raise SchemaRefusal("a text-bundle OTHER citation names no packaged page")
                citations.append(citation)
                position += 2
            digest = _section_field(block, position, "other_text_sha256: ")
            _section_field(block, position + 1, "other_text:")
            literal = _decode_json(
                _section_field(block, position + 2, ""), "a text-bundle other text is not JSON"
            )
            # The diplomatic line is derived; rendering the file again checks it.
            _section_field(block, position + 3, "other_diplomatic:")
            _section_field(block, position + 5, "other_uncertainty:")
            uncertainty = _decode_json(
                _section_field(block, position + 6, ""),
                "a text-bundle other uncertainty layer is not JSON",
            )
            status = _section_field(block, position + 7, "other_text_status: ")
            end = position + 8
            if end < len(block) and block[end].startswith(OPERATOR_LABEL_LINE):
                # Its lines are the operator row's own (`_verify_operator_layer`).
                end = block_end(block, end)
            if _section_field(block, end, "") != "":
                raise SchemaRefusal("a text-bundle OTHER section does not end where its fields do")
            if (
                not _is_line_safe_identity(act_key)
                or not _is_line_safe_identity(act_id)
                or not citations
                or not isinstance(literal, str)
                or digest != canonical_text_sha256(literal)
                or act_id in in_folder
            ):
                raise SchemaRefusal(
                    "a text-bundle OTHER section has an invalid identity, hash or citation"
                )
            try:
                utf8_round_trip(uncertainty, literal)
            except SchemaRefusal as error:
                raise SchemaRefusal(
                    "a text-bundle other uncertainty layer does not anchor to its own literal"
                ) from error
            if folder not in {
                _source_folder_for_declared_path(path) for path, _digest in citations
            }:
                raise SchemaRefusal(
                    "a text-bundle OTHER section is enclosed by the wrong source folder"
                )
            record = (act_key, literal, digest, tuple(citations), uncertainty, status)
            if records.setdefault(act_id, record) != record:
                raise SchemaRefusal(
                    "a text-bundle repeats one other reading differently across folders"
                )
            in_folder.add(act_id)
            index += position + 7
    return records


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
            _validate_cited_region(region, subject="exported act")
            copied = dict(region)
            copied["crop_image"] = _image_reference(
                copied["image_path"],
                copied["image_sha256"],
                f"pixels/crops/{copied['region_id']}.img",
                embed_pixels,
                read_bytes,
                embedded,
                changed="a source crop changed while its export was being built",
                collision="two source crops claimed one package member",
            )
            regions.append(copied)
        record["source_regions"] = regions
        projected.append(record)
    return tuple(projected), embedded


_UNSAFE_PATH_CHARACTERS: Final = frozenset({"\\", "\x00"})


def _is_line_safe_identity(value: object) -> bool:
    """Whether an identity may be spliced unescaped into a line-oriented format.

    Act ids and keys are written raw into the text bundle's headers, which the
    verifier parses line by line.
    """
    if not _is_nonempty_str(value):
        return False
    return not any(ord(character) < 0x20 or character == "\x7f" for character in value)


def _is_safe_path_segment(value: object) -> bool:
    """Whether an identity may be spliced into a member path as one whole component."""
    if not _is_nonempty_str(value) or "/" in value or value in (".", ".."):
        return False
    return not any(character in value for character in _UNSAFE_PATH_CHARACTERS)


def _reject_unsafe_relative_path(value: object, *, subject: str) -> PurePosixPath:
    """The one 'is this a safe POSIX-relative path' check every path-shaped field shares.

    Backslashes are refused because ``PurePosixPath`` does not split on them, so
    ``a/..\\..\\evil`` passes the ``..`` check, yet Windows tools treat a backslash
    in a ZIP entry name as a separator.
    """
    if not _is_nonempty_str(value):
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

    The product cites evidence by path and digest but never includes it.
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
            # Keep any extra metadata, but always relabel the path and digest.
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


def _verify_retained_references_bounded(value: Any) -> None:
    """`_verify_retained_references`, refusing a citation nested past the recursion limit.

    Every external call site uses this wrapper, never the bare walker.
    """
    try:
        _verify_retained_references(value)
    except RecursionError as error:
        raise SchemaRefusal(
            "a product reference nests too deeply for its availability walk"
        ) from error


def _verify_evidence_refs(evidence_refs: Any, *, subject: str) -> None:
    """Require each act to retain its Recensor review and only real citations.

    Stricter than the generic walk: every entry must be a retained-run citation,
    and there is always at least the review.
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


def _database_run_metadata(run: dict[str, str], ledger: dict[str, Any]) -> dict[str, str]:
    """What the acts database says of its run: who, and whether it is complete and why not.

    The manifest's `run` binding and its ledger's status and reasons, so a reader
    of the database alone sees that a partial run is partial.
    """
    return {
        "run": canonical_text(run),
        "run_status": ledger["status"],
        "partial_reasons": canonical_text(ledger["unresolved_reasons"]),
    }


def _acts_database_bytes(
    acts: tuple[dict[str, Any], ...],
    operator_labels: dict[str, str],
    run_metadata: dict[str, str],
    lot: str | None,
) -> bytes:
    """The acts table, its search layer and its metadata.

    `operator_labels` are by act id, from the operator rows; `run_metadata` is
    `_database_run_metadata`'s.
    """
    with tempfile.TemporaryDirectory(prefix="armarium-sqlite-") as directory:
        path = f"{directory}/acts.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            # Moves with `_SQLITE_SCHEMA`.
            connection.execute(f"PRAGMA user_version={_SQLITE_USER_VERSION}")
            connection.executescript(_ACTS_DATABASE_DDL)
            metadata = {
                "canonical_text_encoding": CANONICAL_TEXT_ENCODING,
                "canonical_text_field": CANONICAL_TEXT_FIELD,
                "normalizer_revision": TEXTNORM_REVISION,
                "schema": _SQLITE_SCHEMA,
                "unidata_version": unicodedata.unidata_version,
                **run_metadata,
            }
            connection.executemany(
                "INSERT INTO export_metadata(key, value) VALUES (?, ?)",
                sorted(metadata.items()),
            )
            for act in sorted(acts, key=lambda item: act_key_sort_key(item["act_key"])):
                row = _database_row(act, operator_labels.get(act["act_id"]), lot)
                connection.execute(
                    f"INSERT INTO acts({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                    tuple(row.values()),
                )
                literal, text_hash = row[CANONICAL_TEXT_FIELD], row["canonical_text_sha256"]
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
            # SQLite refuses VACUUM inside the open transaction; VACUUM makes the
            # page layout deterministic.
            connection.execute("VACUUM")
            connection.commit()
        except sqlite3.DatabaseError as error:
            raise SchemaRefusal(
                "SQLite FTS5 could not build the requested acts database"
            ) from error
        finally:
            connection.close()
        return Path(path).read_bytes()


def _row_head(reading: dict[str, Any]) -> dict[str, Any]:
    """The fields every row of one reading carries, in every row-shaped member.

    `acts.jsonl`, `other.jsonl`, `review-items.jsonl` and the acts database each
    build their rows on this, so a field every row carries is added here once.
    """
    return {
        "act_id": reading["act_id"],
        "act_key": reading["act_key"],
        "category": reading["category"],
        "reason": _export_reason(reading),
    }


def _text_fields(reading: dict[str, Any]) -> dict[str, Any]:
    """A reading's text and the fields that describe it, all null (regions empty) without text."""
    literal = reading[CANONICAL_TEXT_FIELD]
    delivered = literal is not None
    return {
        CANONICAL_TEXT_FIELD: literal,
        "canonical_text_sha256": canonical_text_sha256(literal) if delivered else None,
        "provenance": reading.get("provenance") if delivered else None,
        "source_regions": reading.get("source_regions", []) if delivered else [],
        "uncertainty": reading.get("uncertainty") if delivered else None,
        "text_status": reading.get("text_status") if delivered else None,
    }


def _uncertainty_status(reading: dict[str, Any]) -> str:
    """Whether a row's uncertainty layer anchors to a text it carries."""
    return (
        _UNCERTAINTY_AVAILABLE
        if reading[CANONICAL_TEXT_FIELD] is not None
        else _UNCERTAINTY_NOT_APPLICABLE
    )


def _readings_by_kind(
    acts: tuple[dict[str, Any], ...], others: tuple[dict[str, Any], ...]
) -> list[tuple[dict[str, Any], str]]:
    """Every reading with its kind, acts and other readings together, in reading order."""
    paired = [(act, "act") for act in acts] + [(other, "other") for other in others]
    return sorted(paired, key=lambda item: act_key_sort_key(item[0]["act_key"]))


def _database_row(
    act: dict[str, Any], operator_label: str | None, lot: str | None
) -> dict[str, Any]:
    """One `acts` table row, by column; text-derived columns are null without text.

    `operator_label` is the act's operator row's label ("released by operator",
    "corrected by a person"), so a database-only reader sees that a person
    acted on the reading; null for an act no operator acted on.
    """
    fields = _text_fields(act)
    delivered = fields[CANONICAL_TEXT_FIELD] is not None
    return {
        **_row_head(act),
        "lot": lot,
        CANONICAL_TEXT_FIELD: fields[CANONICAL_TEXT_FIELD],
        "canonical_text_sha256": fields["canonical_text_sha256"],
        **{
            f"{name}_json": canonical_text(fields[name]) if delivered else None
            for name in ("provenance", "source_regions", "uncertainty")
        },
        "uncertainty_status": _uncertainty_status(act),
        "text_status": fields["text_status"],
        "evidence_json": canonical_text(_act_evidence(act)),
        "approval_ref": act.get("approval_ref"),
        "reading": act["reading"],
        "operator_label": operator_label,
    }


def _act_json_records(acts: tuple[dict[str, Any], ...], lot: str | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for act in sorted(acts, key=lambda item: act_key_sort_key(item["act_key"])):
        records.append(
            {
                "schema": ACT_RECORD_SCHEMA,
                **_row_head(act),
                "lot": lot,
                **_text_fields(act),
                "uncertainty_status": _uncertainty_status(act),
                **_act_evidence(act),
                "approval_ref": act.get("approval_ref"),
                "reading": act["reading"],
            }
        )
    return records


def _other_json_records(
    others: tuple[dict[str, Any], ...], lot: str | None
) -> list[dict[str, Any]]:
    """`other.jsonl`: one row per other reading, text only when it was delivered."""
    records: list[dict[str, Any]] = []
    for other in sorted(others, key=lambda item: act_key_sort_key(item["act_key"])):
        records.append(
            {
                "schema": OTHER_READING_SCHEMA,
                **_row_head(other),
                "lot": lot,
                "kind": "other",
                "page_ordinal": other["page_ordinal"],
                **_text_fields(other),
                **_act_evidence(other),
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

    The fallback names the gap ("upstream recorded no reason"), not the outcome.
    """
    if act["category"] in _REVIEW_CATEGORIES:
        return act.get("reason") or "upstream recorded no reason"
    return act.get("reason")


def _review_records(
    acts: tuple[dict[str, Any], ...], others: tuple[dict[str, Any], ...], lot: str | None
) -> list[dict[str, Any]]:
    """Every held or refused reading, in reading order, its `kind` act or other."""
    return [
        {
            "schema": REVIEW_ITEM_SCHEMA,
            **_row_head(reading),
            "lot": lot,
            "kind": kind,
            "evidence_refs": reading.get("evidence_refs", []),
        }
        for reading, kind in _readings_by_kind(acts, others)
        if reading["category"] in _REVIEW_CATEGORIES
    ]


def _csv_cell(value: str | None) -> str:
    """One CSV cell: empty for null, and a formula start neutralised by `_CSV_ESCAPE`."""
    if value is None:
        return ""
    if value.startswith((*_CSV_FORMULA_STARTS, _CSV_ESCAPE)):
        return _CSV_ESCAPE + value
    return value


def _csv_value(cell: str) -> str:
    """The value one CSV cell stands for: `_csv_cell` undone."""
    return cell.removeprefix(_CSV_ESCAPE)


def _acts_csv_bytes(acts: tuple[dict[str, Any], ...], lot: str | None) -> bytes:
    """`acts.csv`: one flat row per act in reading order, text columns empty without text.

    The verifier renders it again with this function from what it has already
    checked and requires the same bytes.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_CSV_COLUMNS)
    for act in sorted(acts, key=lambda item: act_key_sort_key(item["act_key"])):
        fields = _text_fields(act)
        delivered = fields[CANONICAL_TEXT_FIELD] is not None
        doubt = (
            doubt_count(fields[CANONICAL_TEXT_FIELD], fields["uncertainty"]) if delivered else None
        )
        row = {
            **_row_head(act),
            "lot": lot,
            "reading": act["reading"],
            "text_status": fields["text_status"],
            CANONICAL_TEXT_FIELD: fields[CANONICAL_TEXT_FIELD],
            "diplomatic_text": (
                diplomatic_display(fields[CANONICAL_TEXT_FIELD], fields["uncertainty"])
                if delivered
                else None
            ),
            "canonical_text_sha256": fields["canonical_text_sha256"],
            "uncertainty_json": canonical_text(fields["uncertainty"]) if delivered else None,
            "doubtful_or_unread": None if doubt is None else str(doubt[0]),
            "out_of": None if doubt is None else str(doubt[1]),
        }
        writer.writerow([_csv_cell(row[column]) for column in _CSV_COLUMNS])
    return _CSV_BOM + buffer.getvalue().encode("utf-8")


def _csv_records(path: Path) -> list[dict[str, str]]:
    """Every row of `acts.csv` by column, each cell's escape removed."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise SchemaRefusal("the acts CSV cannot be read") from error
    if not data.startswith(_CSV_BOM):
        raise SchemaRefusal("the acts CSV does not open with its UTF-8 byte-order mark")
    try:
        text = data[len(_CSV_BOM) :].decode("utf-8")
        # No cell is longer than the member, so the field limit never cuts one.
        csv.field_size_limit(max(csv.field_size_limit(), len(text) + 1))
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as error:
        raise SchemaRefusal("the acts CSV is not one readable UTF-8 CSV table") from error
    if not rows or tuple(rows[0]) != _CSV_COLUMNS:
        raise SchemaRefusal("the acts CSV does not carry exactly this build's columns")
    if any(len(row) != len(_CSV_COLUMNS) for row in rows[1:]):
        raise SchemaRefusal("an acts CSV row does not carry one cell per column")
    return [
        {column: _csv_value(cell) for column, cell in zip(_CSV_COLUMNS, row, strict=True)}
        for row in rows[1:]
    ]


def _csv_literals(path: Path) -> dict[str, tuple]:
    """Each delivered act's literal, hash, uncertainty layer and status, as `acts.csv` gives them."""
    records: dict[str, tuple] = {}
    for row in _csv_records(path):
        if row["category"] != ArmariumCategory.DELIVERED.value:
            continue
        act_id, literal, digest = (
            row["act_id"],
            row[CANONICAL_TEXT_FIELD],
            row["canonical_text_sha256"],
        )
        if not act_id or digest != canonical_text_sha256(literal) or act_id in records:
            raise SchemaRefusal("an acts CSV literal identity or hash is invalid")
        uncertainty = _decode_json(
            row["uncertainty_json"], "an acts CSV uncertainty layer is not JSON"
        )
        _require_damage_record(row["text_status"], uncertainty, literal, subject="acts CSV row")
        records[act_id] = (
            literal,
            digest,
            validate_uncertainty(uncertainty, literal),
            row["text_status"],
        )
    return records


def _verify_csv_rendering(root: Path, manifest: dict[str, Any], sources: dict[str, Any]) -> None:
    """`acts.csv` is exactly what this build writes for the package.

    The writer is fed the act outcomes and readings of `sources.json`, the
    manifest's lot and each delivered act's literal and layer as the CSV gives
    them (hash- and damage-checked), so no other row, cell or order passes.
    """
    literals = _csv_literals(root / CSV_MEMBER)
    readings = {row["act_id"]: row["reading"] for row in sources["act_readings"]}
    acts = []
    for act_id, outcome in _act_outcome_sources(sources).items():
        act = {**outcome, "reading": readings.get(act_id), CANONICAL_TEXT_FIELD: None}
        if outcome["category"] == ArmariumCategory.DELIVERED.value:
            if act_id not in literals:
                raise SchemaRefusal(f"the acts CSV does not deliver {act_id}")
            act[CANONICAL_TEXT_FIELD], _digest, act["uncertainty"], _status = literals[act_id]
        acts.append(act)
    if (root / CSV_MEMBER).read_bytes() != _acts_csv_bytes(tuple(acts), manifest["run"]["lot"]):
        raise SchemaRefusal(
            "the acts CSV is not exactly what this build writes for the package's own accounting"
        )


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(record) + b"\n" for record in records)


def _package_lines(path, subject: str) -> list[str]:
    r"""Split one package member on exactly the separator its writer used.

    Not ``str.splitlines``: with ``ensure_ascii=False`` JSON strings carry U+0085,
    U+2028 and U+2029 raw, and splitting on them would cut a record in half. Raw
    bytes avoid newline translation (``read_text(newline="")`` needs Python 3.13).
    """
    try:
        return path.read_bytes().decode("utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"the {subject} cannot be read") from error


def _decode_json(encoded: Any, refusal: str) -> Any:
    try:
        return json.loads(encoded)
    except (TypeError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SchemaRefusal(refusal) from error


def _jsonl_rows(path, subject: str, row: str):
    """Decode every non-blank line of one package JSONL member."""
    for line in _package_lines(path, subject):
        if line:
            yield _decode_json(line, f"{row} is not JSON")


class _TextBundleRecord(NamedTuple):
    literal: str
    digest: str
    citations: tuple[tuple[str, str], ...]
    uncertainty: dict[str, Any]
    text_status: str
    heading_key: str
    # An act's reading, on the line after its act-id; `None` without one.
    reading: str | None = None


_READING_PREFIX: Final = "reading: "


def _text_bundle_records(
    root, source_pages: list[dict[str, Any]] | None = None
) -> dict[str, _TextBundleRecord]:
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

    records: dict[str, _TextBundleRecord] = {}
    record_locations: set[tuple[str, str]] = set()
    # Enumerate the folders from the authenticated source graph, not an `rglob`
    # walk, which could silently skip a linked or unreadable subtree.
    for folder in sorted(source_folders):
        path = root / _text_member_path(folder)
        lines = _package_lines(path, "text bundle")
        current_id: str | None = None
        heading_key: str | None = None
        pending: tuple[str, str, tuple[tuple[str, str], ...]] | None = None
        pending_uncertainty: dict[str, Any] | None = None
        reading: str | None = None
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
                pending = pending_uncertainty = None
                reading = None
            elif line.startswith(_READING_PREFIX):
                if current_id is None or not lines[index - 1].startswith("act-id: "):
                    raise SchemaRefusal("a text-bundle reading line does not follow an act-id")
                reading = line.removeprefix(_READING_PREFIX)
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
                if citation not in known_pages:
                    raise SchemaRefusal("a text-bundle source citation names no packaged page")
                citations.append(citation)
            elif line.startswith("source-sha256: "):
                if index == 0 or not lines[index - 1].startswith("source-page: "):
                    raise SchemaRefusal("a text-bundle source digest has no page citation")
            elif line == "canonical_clean_text:":
                if current_id is None or index + 1 >= len(lines):
                    raise SchemaRefusal("a text-bundle section has no act identity or literal")
                # A second literal would keep offsets validated against the first.
                # When the text bundle is the only selected literal format, nothing
                # else would catch that.
                if pending is not None:
                    raise SchemaRefusal("a text-bundle section carries more than one literal")
                literal = _decode_json(lines[index + 1], "a text-bundle canonical text is not JSON")
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
                if current_id is None or pending is None or index + 1 >= len(lines):
                    raise SchemaRefusal(
                        "a text-bundle uncertainty layer has no literal to anchor to"
                    )
                if pending_uncertainty is not None:
                    raise SchemaRefusal(
                        "a text-bundle section carries more than one uncertainty layer"
                    )
                uncertainty = _decode_json(
                    lines[index + 1], "a text-bundle uncertainty layer is not JSON"
                )
                try:
                    # The round trip, not just the shape: this is the one format
                    # whose layer arrives as decoded text, where offsets could shift.
                    utf8_round_trip(uncertainty, pending[0])
                    pending_uncertainty = uncertainty
                except SchemaRefusal as error:
                    raise SchemaRefusal(
                        "a text-bundle uncertainty layer does not anchor to its own act's literal"
                    ) from error
            elif line.startswith("text_status: "):
                # The section's last field completes its record.
                if current_id is None or pending is None:
                    raise SchemaRefusal(
                        "a text-bundle established-text status has no literal to describe"
                    )
                if pending_uncertainty is None:
                    raise SchemaRefusal(
                        "a text-bundle section carries a literal with no uncertainty layer"
                    )
                text_status = line.removeprefix("text_status: ")
                # Recomputed here because the text bundle may be the only literal
                # format, where cross-format identity catches nothing.
                _require_damage_record(
                    text_status,
                    pending_uncertainty,
                    pending[0],
                    subject="text-bundle section",
                )
                citation_folders = sorted(
                    {_source_folder_for_declared_path(path) for path, _digest in citations}
                )
                if folder not in citation_folders:
                    raise SchemaRefusal("a text-bundle act is enclosed by the wrong source folder")
                candidate = _TextBundleRecord(
                    *pending,
                    pending_uncertainty,
                    text_status,
                    heading_key,
                    reading,
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
        if current_id is not None:
            raise SchemaRefusal("a text-bundle section has no completed literal record")
    return records


def _text_bundle_literals(root) -> dict[str, tuple]:
    return {
        act_id: (
            record.literal,
            record.digest,
            record.uncertainty,
            record.text_status,
        )
        for act_id, record in _text_bundle_records(root).items()
    }


_STORED_ACTS_TABLES: Final = ("acts", "act_search", "export_metadata")
_DATABASE_METADATA_KEYS: Final = frozenset(
    {
        "canonical_text_encoding",
        "canonical_text_field",
        "normalizer_revision",
        "schema",
        "unidata_version",
        "run",
        "run_status",
        "partial_reasons",
    }
)
_SQLITE_PRODUCT_TABLES: Final = (*_STORED_ACTS_TABLES, "acts_fts")
# Writer and verifier share this DDL, so the schema check covers FTS shadow
# tables and implicit indexes without a second spelling. The `acts` table
# names the reading each act came from.
_ACTS_DATABASE_DDL: Final = """
                CREATE TABLE export_metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE acts (
                    act_id TEXT PRIMARY KEY NOT NULL,
                    act_key TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    lot TEXT,
                    canonical_clean_text TEXT,
                    canonical_text_sha256 TEXT,
                    provenance_json TEXT,
                    source_regions_json TEXT,
                    uncertainty_json TEXT,
                    uncertainty_status TEXT NOT NULL,
                    text_status TEXT,
                    evidence_json TEXT NOT NULL,
                    approval_ref TEXT,
                    reason TEXT,
                    reading TEXT,
                    operator_label TEXT
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
    content table.
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

    The product names must be tables, not views: a view over a recursive CTE turns
    a few kilobytes into an unbounded result set, while a table is bounded by the
    member's size. ``as_uri`` percent-encodes the path, so a directory named
    ``x?y`` does not become a query string.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal("the acts database cannot be opened") from error
    try:
        _verify_acts_database_identity(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _verify_acts_database_identity(connection: sqlite3.Connection) -> None:
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
        raise SchemaRefusal("the acts database has no readable schema") from error
    if any(kinds.get(name) != "table" for name in _STORED_ACTS_TABLES):
        raise SchemaRefusal("the acts database does not carry acts and act_search as stored tables")
    if kinds.get("acts_fts") != "table" or (schema, user_version) != (
        (_SQLITE_SCHEMA,),
        (_SQLITE_USER_VERSION,),
    ):
        raise SchemaRefusal("the acts database has no recognized SQLite product identity")
    _verify_acts_schema(connection)


def _read_acts_database(path, query: str, refusal: str) -> list[tuple]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_acts_database(path)
        return connection.execute(query).fetchall()
    except sqlite3.DatabaseError as error:
        raise SchemaRefusal(refusal) from error
    finally:
        if connection is not None:
            connection.close()


def _database_literals(path) -> dict[str, tuple]:
    rows = _read_acts_database(
        path,
        """
        SELECT act_id, canonical_clean_text, canonical_text_sha256, uncertainty_json,
               text_status
        FROM acts
        WHERE canonical_clean_text IS NOT NULL
        ORDER BY act_id
        """,
        "the acts database cannot be read for projection identity",
    )
    records: dict[str, tuple] = {}
    for act_id, literal, digest, uncertainty_json, text_status in rows:
        if (
            not isinstance(act_id, str)
            or not isinstance(literal, str)
            or not isinstance(digest, str)
            or not isinstance(uncertainty_json, str)
        ):
            raise SchemaRefusal("the acts database has an untyped literal row")
        if digest != canonical_text_sha256(literal) or act_id in records:
            raise SchemaRefusal("the acts database literal identity or hash is invalid")
        uncertainty = _database_json_layer(uncertainty_json, "uncertainty")
        _require_damage_record(text_status, uncertainty, literal, subject="acts database row")
        records[act_id] = (
            literal,
            digest,
            validate_uncertainty(uncertainty, literal),
            text_status,
        )
    return records


def _jsonl_literals(path) -> dict[str, tuple]:
    records: dict[str, tuple] = {}
    # Independently callable, so it cannot rely on earlier validation.
    for record in _jsonl_rows(path, "acts JSONL", "an acts JSONL row"):
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
            record.get("uncertainty"),
            literal,
            subject="acts JSONL row",
        )
        records[act_id] = (
            literal,
            digest,
            validate_uncertainty(record.get("uncertainty"), literal),
            record.get("text_status"),
        )
    return records


def _page_ledger_category(
    ordinal: int,
    act_categories: list[str],
    *,
    edge_hold: bool = False,
    other_categories: list[str],
) -> tuple[str, str | None]:
    """One sealed page's terminal category, derived from the acts cut on it.

    Every rule errs toward `held-for-review`. A page with no acts is never
    `confirmed-blank`, because silence cannot tell a blank page from a detection
    failure; a page is blank only when all its acts are. A page whose readings
    are all `other` and all delivered is a delivered no-act page; one with no
    reading at all, or with an undelivered `other` reading, is held.
    """
    if edge_hold:
        return (
            ArmariumCategory.HELD_FOR_REVIEW.value,
            edge_hold_reason(ordinal),
        )
    if not act_categories:
        # Every page read has a row, so one with no act row holds only other
        # readings, and it is a confirmed no-act page once all are delivered.
        if not other_categories:
            return ArmariumCategory.HELD_FOR_REVIEW.value, PAGE_READ_SILENT_PAGE_REASON.format(
                ordinal=ordinal
            )
        if set(other_categories) == {ArmariumCategory.DELIVERED.value}:
            return ArmariumCategory.DELIVERED.value, CONFIRMED_NO_ACT_PAGE_REASON.format(
                ordinal=ordinal
            )
        return ArmariumCategory.HELD_FOR_REVIEW.value, NO_ACT_PAGE_HELD_REASON.format(
            ordinal=ordinal, categories=", ".join(sorted(set(other_categories)))
        )
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


def _page_ledger_unit(
    unit_type: str, page: dict[str, Any], category: str, reason: str | None
) -> dict[str, Any]:
    return {
        "unit_type": unit_type,
        "unit_id": f"{unit_type}:{page['ordinal']}",
        "category": category,
        "reason": reason,
        "declared_path": page.get("declared_path"),
        "declared_sha256": page.get("declared_sha256"),
    }


def _terminal_ledger(
    act_outcomes: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    act_pages: dict[str, Any],
    aggregate: dict[str, Any],
    edge_hold_pages: tuple[int, ...],
    other_outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """The honesty ledger: one closed category for every unit the run accounted for.

    Every source, sealed page and act lands in exactly one of the five categories
    (a total partition); anything else stops the export. A source inherits its
    page's category. The three unit types describe overlapping material, so
    `by_unit_type` shows that category totals count units, not acts. The other
    readings are a fourth unit type (`other`), never acts: a held one
    keeps the run partial, and they decide a page's category only on a page with
    no act, which is a confirmed no-act page once every one is delivered.
    """
    by_act_id: dict[str, dict[str, Any]] = {}
    categories_by_key: dict[str, str] = {}
    for record in act_outcomes:
        # Callers deduplicate, but the partition must not depend on that.
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
            if not is_plain_int(ordinal):
                raise SchemaRefusal("an Armarium terminal ledger act names a non-integer page")
            acts_on_page.setdefault(ordinal, []).append(category)

    others_on_page: dict[int, list[str]] = {}
    for record in other_outcomes:
        others_on_page.setdefault(record["page_ordinal"], []).append(record["category"])
    page_units: list[dict[str, Any]] = []
    source_units: list[dict[str, Any]] = []
    # Each unresolved page's one fact, in the words the aggregate uses for it.
    page_facts: list[str] = []
    for page in sorted(pages, key=lambda row: row["ordinal"]):
        ordinal = page["ordinal"]
        if page.get("outcome") == "sealed":
            category, reason = _page_ledger_category(
                ordinal,
                acts_on_page.get(ordinal, []),
                edge_hold=ordinal in edge_hold_pages,
                other_categories=others_on_page.get(ordinal, []),
            )
            page_units.append(_page_ledger_unit("page", page, category, reason))
            if category not in _COMPLETED_CATEGORIES:
                page_facts.append(reason)
        else:
            category = ArmariumCategory.REFUSED_WITH_REASON.value
            reason = page.get("reason") or "no reason was recorded"
            page_facts.append(
                unsealed_page_reason(ordinal, page.get("outcome"), page.get("reason"))
            )
        source_units.append(_page_ledger_unit("source", page, category, reason))

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
    other_units = [
        {
            "unit_type": "other",
            "unit_id": f"other:{record['act_id']}",
            "category": record["category"],
            "reason": record["reason"],
            "act_key": record["act_key"],
        }
        for record in sorted(other_outcomes, key=lambda item: item["act_id"])
    ]

    units = source_units + page_units + act_units + other_units
    by_category = {category: 0 for category in sorted(_KNOWN_CATEGORIES)}
    by_unit_type = {"source": 0, "page": 0, "act": 0, "other": 0}
    seen: set[str] = set()
    for unit in units:
        if unit["category"] not in _KNOWN_CATEGORIES:
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

    reasons = _unresolved_reasons(
        units,
        aggregate.get("reasons", []) if aggregate.get("status") != "complete" else [],
        page_facts,
    )
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


def _unresolved_reasons(
    units: list[dict[str, Any]], aggregate_reasons: list[str], page_facts: list[str]
) -> list[str]:
    """One line per unresolved fact: the aggregate's reasons, then what only the ledger knows.

    Both sides state a fact with the same `common.contracts.outcomes` sentence,
    so a fact is one string and is named once. The aggregate's line for an
    unresolved act gains the act's recorded reason; an act it does not name is
    still named, so no unit drops out. Every unresolved `other` reading is named
    by key with its reason, since the aggregate names only its page. Each
    unresolved page or unsealed source adds its own fact (`page_facts`).
    """

    def line(unit: dict[str, Any]) -> str:
        fact = (
            unresolved_act_reason(unit["act_key"], unit["category"])
            if unit["unit_type"] == "act"
            else f"other {unit['act_key']} is {unit['category']}"
        )
        return f"{fact}: {unit['reason']}" if unit["reason"] else fact

    unresolved = [unit for unit in units if unit["category"] not in _COMPLETED_CATEGORIES]
    acts = {
        unresolved_act_reason(unit["act_key"], unit["category"]): unit
        for unit in unresolved
        if unit["unit_type"] == "act"
    }
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    for reason in aggregate_reasons:
        unit = acts.pop(reason, None)
        add(line(unit) if unit is not None else reason)
    for unit in acts.values():
        add(line(unit))
    for unit in unresolved:
        if unit["unit_type"] == "other":
            add(line(unit))
    for fact in page_facts:
        add(fact)
    return reasons


def _export_manifest(
    projection: ArmariumProjection,
    formats: ArmariumFormats,
    members: dict[str, bytes],
    ledger: dict[str, Any],
    ink_map_rows: list[dict[str, Any]],
    edge_hold_pages: tuple[int, ...],
    other_outcomes: list[dict[str, Any]],
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
    manifest: dict[str, Any] = {
        "schema": EXPORT_MANIFEST_SCHEMA,
        "canonical_text": _canonical_text_claim(formats.formats),
        "run": _manifest_run_binding(projection),
        "formats": formats.to_record(),
        "claims": {
            "status": ledger["status"],
            "partial_reasons": ledger["unresolved_reasons"],
            "terminal_ledger": ledger,
            "ink_map": {
                "denominator": INK_MAP_DENOMINATOR,
                "held_pages": list(edge_hold_pages),
                "unmeasurable_pages": list(
                    _unmeasurable_ink_map_pages_from_validated_rows(ink_map_rows)
                ),
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
            "uncertainty": _uncertainty_claim(formats.formats),
            "not_measured": _not_measured_claim(projection),
            "other_readings": _other_readings_claim(other_outcomes, formats.formats),
            "page_accounting": _page_accounting_claim(
                list(projection.page_accounting),
                {row["ordinal"] for row in projection.page_accounting},
            ),
            "reask": _reask_claim(
                _act_readings(projection.acts),
                [row["ordinal"] for row in projection.page_accounting],
            ),
            "doubt_share": _doubt_share_claim(
                {
                    act["act_id"]: (act[CANONICAL_TEXT_FIELD], act["uncertainty"])
                    for act in projection.acts
                    if act["category"] == ArmariumCategory.DELIVERED.value
                },
                {act["act_id"]: act["act_key"] for act in projection.acts},
                measured=bool(_literal_formats_in(formats.formats)),
            ),
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


_DOUBT_SHARE_DENOMINATOR: Final = (
    "each delivered act's established text: its non-whitespace characters, each "
    "zero-width gap counted as one unread character, and a reading with nothing read "
    "as one unread character"
)
_DOUBT_SHARE_MEASURED: Final = "measured"
_DOUBT_SHARE_NOT_APPLICABLE: Final = "not-applicable-no-literal-format"


def _doubt_share_claim(
    texts: dict[str, tuple[str, Any]], act_keys: dict[str, str], *, measured: bool
) -> dict[str, Any]:
    """How much of each delivered act, and of each page's delivered acts, is doubtful or unread.

    `texts` is each delivered act's literal and layer. A package with no literal
    format carries no text to recount, so it states that rather than numbers.
    """
    acts: list[dict[str, Any]] = []
    pages: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    if measured:
        for act_id in sorted(texts, key=lambda item: act_key_sort_key(act_keys[item])):
            doubtful, out_of = doubt_count(*texts[act_id])
            page = _key_page(act_keys[act_id])
            acts.append(
                {
                    "act_id": act_id,
                    "act_key": act_keys[act_id],
                    "page_ordinal": page,
                    "doubtful_or_unread": doubtful,
                    "out_of": out_of,
                }
            )
            if page is not None:
                pages[page][0] += doubtful
                pages[page][1] += out_of
    return {
        "denominator": _DOUBT_SHARE_DENOMINATOR,
        "status": _DOUBT_SHARE_MEASURED if measured else _DOUBT_SHARE_NOT_APPLICABLE,
        "acts": acts,
        "pages": [
            {"ordinal": ordinal, "doubtful_or_unread": counts[0], "out_of": counts[1]}
            for ordinal, counts in sorted(pages.items())
        ],
    }


def _verify_doubt_share_claim(
    root: Path, manifest: dict[str, Any], formats: ArmariumFormats
) -> None:
    """The doubt-share claim, recounted from a literal format's own text and layers.

    Every literal format gives the same reading (`_compare_literal_projections`),
    so the first one selected stands for all.
    """
    literal_formats = [name for name in _LITERAL_TEXT_FORMATS if name in formats.formats]
    texts = (
        {
            act_id: (record[0], record[2])
            for act_id, record in _literal_projection(root, literal_formats[0]).items()
        }
        if literal_formats
        else {}
    )
    act_keys = _manifest_act_keys(manifest, _manifest_act_categories(manifest))
    if set(texts) - set(act_keys):
        raise SchemaRefusal("a literal format delivers an act the manifest does not partition")
    expected = _doubt_share_claim(texts, act_keys, measured=bool(literal_formats))
    if manifest["claims"]["doubt_share"] != expected:
        raise SchemaRefusal(
            "the manifest's doubt share is not what the package's own readings count"
        )


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


_SOURCES_LIST_FIELDS: Final = (
    "pages",
    "regions",
    "act_citations",
    "act_outcomes",
    "other_outcomes",
    "other_citations",
    "page_accounting",
    "act_readings",
)
_SOURCES_FIELDS: Final = (
    "pages",
    "regions",
    "act_citations",
    "act_outcomes",
    "aggregate_basis",
    "ink_map_pages",
    "witness_chairs",
    "witness_floor",
    "other_outcomes",
    "other_citations",
    "page_accounting",
    "act_readings",
)


def _load_sources(root) -> dict[str, Any]:
    try:
        record = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    except RecursionError as error:
        raise SchemaRefusal(
            "the package sources citation nests too deeply for this parser to read"
        ) from error
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise SchemaRefusal("the package sources citation is unreadable") from error
    if not isinstance(record, dict) or record.get("schema") != SOURCES_SCHEMA:
        raise SchemaRefusal("the package sources citation has no recognized schema")
    optional = {
        "continuation_joins",
        "reconstructions",
        OPERATOR_SOURCES_FIELD,
        READING_HOLDS_FIELD,
        MODEL_READINGS_FIELD,
        FLAGGED_SOURCES_FIELD,
    }
    if set(record) - optional != {"schema", *_SOURCES_FIELDS}:
        raise SchemaRefusal("the package sources citation has an unrecognized field set")
    sources = {field: record[field] for field in _SOURCES_FIELDS}
    sources["ink_map_pages"] = _validate_ink_map_pages(
        sources["ink_map_pages"], "the package sources citation"
    )
    if not isinstance(sources["aggregate_basis"], dict) or any(
        not isinstance(sources[field], list) for field in _SOURCES_LIST_FIELDS
    ):
        raise SchemaRefusal("the package sources citation has no page and region lists")
    sources["continuation_joins"] = record.get("continuation_joins")
    if "continuation_joins" in record and not (
        isinstance(sources["continuation_joins"], list) and sources["continuation_joins"]
    ):
        raise SchemaRefusal("the package sources citation carries an empty continuation-join list")
    sources["reconstructions"] = record.get("reconstructions")
    if "reconstructions" in record and not (
        isinstance(sources["reconstructions"], list)
        and sources["reconstructions"]
        and all(
            isinstance(act_ids, list) and act_ids and all(isinstance(a, str) for a in act_ids)
            for act_ids in sources["reconstructions"]
        )
    ):
        raise SchemaRefusal("the package sources citation names its reconstructions malformed")
    sources[OPERATOR_SOURCES_FIELD] = record.get(OPERATOR_SOURCES_FIELD)
    if OPERATOR_SOURCES_FIELD in record and not (
        isinstance(sources[OPERATOR_SOURCES_FIELD], list) and sources[OPERATOR_SOURCES_FIELD]
    ):
        raise SchemaRefusal("the package sources citation carries an empty operator layer")
    sources[READING_HOLDS_FIELD] = record.get(READING_HOLDS_FIELD)
    sources[FLAGGED_SOURCES_FIELD] = record.get(FLAGGED_SOURCES_FIELD)
    if FLAGGED_SOURCES_FIELD in record and not (
        isinstance(sources[FLAGGED_SOURCES_FIELD], list) and sources[FLAGGED_SOURCES_FIELD]
    ):
        raise SchemaRefusal("the package sources citation carries an empty flagged layer")
    sources[MODEL_READINGS_FIELD] = record.get(MODEL_READINGS_FIELD)
    if MODEL_READINGS_FIELD in record and not (
        isinstance(sources[MODEL_READINGS_FIELD], list) and sources[MODEL_READINGS_FIELD]
    ):
        raise SchemaRefusal("the package sources citation carries an empty model reading layer")
    return sources


def _manifest_claim(manifest: dict[str, Any], name: str) -> Any:
    claims = manifest.get("claims")
    return claims.get(name) if isinstance(claims, dict) else None


def _manifest_format_names(manifest: dict[str, Any]) -> Any:
    selected = manifest.get("formats")
    return selected.get("formats") if isinstance(selected, dict) else None


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
    if "csv" in formats.formats:
        required["csv"] = {CSV_MEMBER}
    if "review-items" in formats.formats:
        required["review-items"] = {"review-items.jsonl"}
    return required


def _all_pixel_references(sources: dict[str, list[dict[str, Any]]]) -> list[Any]:
    """Every page and crop pixel reference in the source graph.

    The one place both the member inventory and the pixel-claim check gather them.
    """
    references: list[Any] = [
        page.get("page_image") for page in sources["pages"] if isinstance(page, dict)
    ]
    references.extend(
        region.get("crop_image") for region in sources["regions"] if isinstance(region, dict)
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
    if "jsonl" in formats.formats:
        expected.add(OTHER_READINGS_MEMBER)
    # Written exactly when `sources.json` records a reconstruction; its rows are
    # verified whole (`_verify_coniector_layer`).
    if "jsonl" in formats.formats and sources.get("reconstructions"):
        expected.add(CONIECTOR_MEMBER)
    # Written exactly when a delivered reading carries an operator row
    # (`_verify_operator_layer`).
    if "jsonl" in formats.formats and sources.get(OPERATOR_SOURCES_FIELD):
        expected.add(OPERATOR_MEMBER)
        if any(
            isinstance(row, dict) and row.get("label") == CORRECTED_LABEL
            for row in sources[OPERATOR_SOURCES_FIELD]
        ):
            expected.add(MODEL_READINGS_MEMBER)
    # Written exactly when `sources.json` records a held or flagged reading
    # (`_verify_flagged_layer`).
    if "review-items" in formats.formats and sources.get(FLAGGED_SOURCES_FIELD):
        expected.add(FLAGGED_MEMBER)
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
    partition = _manifest_claim(manifest, "act_partition")
    if not isinstance(partition, dict):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no act partition claim")
    expected_count = partition.get("expected_count")
    counted = partition.get("counted")
    if (
        not _is_count(expected_count)
        or not _is_count(counted)
        or partition.get("reconciles") is not True
    ):
        raise SchemaRefusal("EXPORT_MANIFEST.json has an unreconciled act partition claim")
    rows = partition.get("categories")
    if not isinstance(rows, list):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no category rows")

    seen_categories: set[str] = set()
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SchemaRefusal("an act partition category row is not an object")
        category, count, act_ids = row.get("category"), row.get("count"), row.get("act_ids")
        if (
            not isinstance(category, str)
            or category not in _KNOWN_CATEGORIES
            or category in seen_categories
            or not _is_count(count)
            or not isinstance(act_ids, list)
            or count != len(act_ids)
        ):
            raise SchemaRefusal("an act partition category row is malformed")
        seen_categories.add(category)
        for act_id in act_ids:
            if not _is_nonempty_str(act_id) or act_id in result:
                raise SchemaRefusal("an act partition repeats or omits an act identity")
            result[act_id] = category
    if (
        seen_categories != _KNOWN_CATEGORIES
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
    partition = _manifest_claim(manifest, "act_partition")
    keys = partition.get("act_keys") if isinstance(partition, dict) else None
    if not isinstance(keys, dict) or set(keys) != set(categories):
        raise SchemaRefusal("EXPORT_MANIFEST.json has no complete act-key partition")
    if any(not _is_nonempty_str(act_key) for act_key in keys.values()):
        raise SchemaRefusal("EXPORT_MANIFEST.json has an invalid act key")
    if len(set(keys.values())) != len(keys):
        raise SchemaRefusal("EXPORT_MANIFEST.json repeats an act key")
    return keys


def _verify_honest_status_claims(
    manifest: dict[str, Any], categories: dict[str, str], sources: dict[str, list[dict[str, Any]]]
) -> None:
    """Refuse a self-hashed package that changes a measured partial result to green.

    The terminal ledger and the run aggregate are recomputed from the source graph,
    since a self-hash proves only that the manifest is unedited.
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
        or not all(_is_nonempty_str(reason) for reason in reasons)
    ):
        raise SchemaRefusal("the exported aggregate has no valid measured status and reasons")
    must_be_partial = any(category not in _COMPLETED_CATEGORIES for category in categories.values())
    must_be_partial = must_be_partial or any(
        page.get("outcome") != "sealed" for page in sources["pages"] if isinstance(page, dict)
    )
    derived_edge_holds = _verify_ink_map_claim(claims, sources)
    must_be_partial = must_be_partial or bool(derived_edge_holds)
    # A delivered act with a recorded gap also makes the run partial. The full
    # recomputation below covers it too; checking it here gives a specific refusal.
    basis = sources["aggregate_basis"]
    recorded_status = basis.get("act_text_status") if isinstance(basis, dict) else None
    # Hold the basis to the rows first, or editing only the basis to
    # `established` would verify as `complete`.
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
        sources["continuation_joins"],
        others=list(_other_outcome_sources(sources).values()),
        act_keys=act_keys,
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
        list(_other_outcome_sources(sources).values()),
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


def _verify_ink_map_claim(claims: dict[str, Any], sources: dict[str, Any]) -> tuple[int, ...]:
    """Require the ink-map claim to state the held and unmeasurable pages its rows give.

    Both sets are derived from source rows, never from the manifest claim being
    verified; otherwise a false claim verifies itself. Returns the held pages.
    """
    ink_map_rows = _validate_ink_map_pages(
        sources.get("ink_map_pages"), "the package sources citation"
    )
    derived_edge_holds = _edge_hold_pages_from_validated_rows(ink_map_rows)
    derived_unmeasurable_pages = _unmeasurable_ink_map_pages_from_validated_rows(ink_map_rows)
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
    declared_unmeasurable_pages = (
        ink_map_claim.get("unmeasurable_pages") if isinstance(ink_map_claim, dict) else None
    )
    if (
        not isinstance(ink_map_claim, dict)
        or set(ink_map_claim) != {"denominator", "held_pages", "unmeasurable_pages"}
        or ink_map_claim["denominator"] != INK_MAP_DENOMINATOR
        or ink_map_claim["held_pages"] != list(derived_edge_holds)
        or not isinstance(declared_unmeasurable_pages, list)
        or any(not is_plain_int(ordinal) or ordinal <= 0 for ordinal in declared_unmeasurable_pages)
        or declared_unmeasurable_pages != sorted(set(declared_unmeasurable_pages))
        or canonical_text(declared_unmeasurable_pages)
        != canonical_text(list(derived_unmeasurable_pages))
    ):
        raise SchemaRefusal(
            "the exported ink-map claim does not match the held and unmeasurable pages in its "
            "own source evidence. The manifest and source graph disagree about which pages need "
            "review or had no audit measurement. Discard this extraction and rebuild the package "
            "from the intact run tree."
        )
    return derived_edge_holds


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
    for record in sources["act_outcomes"]:
        if not isinstance(record, dict) or set(record) != {
            "act_id",
            "act_key",
            "category",
            "reason",
            "text_status",
            "approval_ref",
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
            not _is_nonempty_str(act_id)
            or not _is_nonempty_str(act_key)
            or category not in _KNOWN_CATEGORIES
            or not isinstance(reason, str | None)
            or act_id in records
        ):
            raise SchemaRefusal("a source act-outcome record has no valid terminal identity")
        # Only a delivered act has an Archetypus record, and so a status. The type
        # check comes first so an unhashable value is refused, not raised.
        has_status = isinstance(text_status, str) and text_status in TEXT_STATUSES
        if has_status is not (category == ArmariumCategory.DELIVERED.value):
            raise SchemaRefusal(
                "a source act-outcome record's established-text status does not match whether "
                "the act was delivered"
            )
        if category in _REVIEW_CATEGORIES and not reason:
            raise SchemaRefusal("a source review outcome has no explicit reason")
        # An exclusion names the approval it rests on, and nothing else names one.
        approval = record["approval_ref"]
        if (category == ArmariumCategory.EXCLUDED_WITH_APPROVAL.value) != (
            _is_nonempty_str(approval)
        ) or not isinstance(approval, str | None):
            raise SchemaRefusal(
                "a source act-outcome record names an approval exactly when it is not an "
                "exclusion, or an exclusion without one"
            )
        records[act_id] = record
    return records


def _act_citation_sources(
    sources: dict[str, list[dict[str, Any]]], field: str = "act_citations"
) -> dict[str, dict[str, Any]]:
    """Read the source graph's exact delivered lineage (acts, or other readings) without text."""
    records: dict[str, dict[str, Any]] = {}
    for record in sources[field]:
        if not isinstance(record, dict) or set(record) != {
            "act_id",
            "act_key",
            "evidence",
            "provenance",
            "source_regions",
        }:
            raise SchemaRefusal("a source act-citation record has an unrecognized field set")
        act_id, act_key = record.get("act_id"), record.get("act_key")
        if not _is_nonempty_str(act_id) or not _is_nonempty_str(act_key) or act_id in records:
            raise SchemaRefusal("a source act-citation record has no unique act identity")
        _verify_delivered_product_provenance(
            record.get("provenance"),
            record.get("source_regions"),
            sources["regions"],
            subject="source act-citation",
        )
        if not isinstance(record.get("evidence"), dict):
            raise SchemaRefusal("a source act-citation has no witness evidence")
        _verify_retained_references_bounded(record["evidence"])
        # sources.json is the one evidence carrier in every format selection.
        _verify_evidence_refs(
            record["evidence"].get("evidence_refs"), subject="a source act-citation"
        )
        records[act_id] = record
    return records


def _other_outcome_sources(sources: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The other layer's terminal records, validated like act outcomes."""
    records: dict[str, dict[str, Any]] = {}
    sealed = _sealed_source_ordinals(sources)
    for record in sources["other_outcomes"]:
        _require_exact_fields(
            record, _OTHER_OUTCOME_FIELDS, subject="a source other-outcome record"
        )
        category, text_status = record["category"], record["text_status"]
        if (
            not _is_nonempty_str(record["act_id"])
            or not _is_nonempty_str(record["act_key"])
            or record["act_id"] in records
            or not isinstance(category, str)
            or category not in _OTHER_CATEGORIES
            or record["page_ordinal"] not in sealed
            or not isinstance(record["reason"], str | None)
        ):
            raise SchemaRefusal("a source other-outcome record has no valid terminal identity")
        has_status = isinstance(text_status, str) and text_status in TEXT_STATUSES
        if has_status is not (category == ArmariumCategory.DELIVERED.value):
            raise SchemaRefusal(
                "a source other-outcome record's text status does not match whether it was delivered"
            )
        if category in _REVIEW_CATEGORIES and not record["reason"]:
            raise SchemaRefusal("a held other reading has no explicit reason")
        records[record["act_id"]] = record
    return records


def _other_jsonl_records(
    path: Path, source_graph_regions: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """`other.jsonl`, each row checked as its category requires."""
    records: dict[str, dict[str, Any]] = {}
    for record in _jsonl_rows(path, "other readings JSONL", "an other-reading JSONL row"):
        if not isinstance(record, dict) or record.get("schema") != OTHER_READING_SCHEMA:
            raise SchemaRefusal("an other-reading JSONL row has no recognized schema")
        _require_exact_fields(record, _OTHER_READING_FIELDS, subject="an other-reading JSONL row")
        _verify_retained_references_bounded(record)
        _verify_evidence_refs(record["evidence_refs"], subject="an other-reading JSONL row")
        act_id, literal = record["act_id"], record[CANONICAL_TEXT_FIELD]
        if record["kind"] != "other" or not _is_nonempty_str(act_id) or act_id in records:
            raise SchemaRefusal("an other-reading JSONL row has no unique other identity")
        if record["category"] == ArmariumCategory.DELIVERED.value:
            if not isinstance(literal, str) or record[
                "canonical_text_sha256"
            ] != canonical_text_sha256(literal):
                raise SchemaRefusal("a delivered other reading has no valid literal text hash")
            _verify_delivered_product_provenance(
                record["provenance"],
                record["source_regions"],
                source_graph_regions,
                subject="other-reading JSONL",
            )
            utf8_round_trip(record["uncertainty"], literal)
            _require_damage_record(
                record["text_status"],
                record["uncertainty"],
                literal,
                subject="other-reading JSONL row",
            )
        elif (
            any(
                record[field] is not None
                for field in (
                    CANONICAL_TEXT_FIELD,
                    "canonical_text_sha256",
                    "text_status",
                    "uncertainty",
                    "provenance",
                )
            )
            or record["source_regions"]
        ):
            raise SchemaRefusal("an undelivered other reading carries text or its lineage")
        records[act_id] = record
    return records


def _verify_page_layers(
    root: Path, manifest: dict[str, Any], formats: ArmariumFormats, sources: dict[str, Any]
) -> None:
    """Recompute the other layer and the page accounting claims from the source graph.

    Every other reading is apart from the act partition, its claim follows from
    its rows, every format carrying it carries the same reading, and no page the
    accounting holds delivered a reading.
    """
    claims = manifest["claims"]
    outcomes = _other_outcome_sources(sources)
    act_ids = set(_manifest_act_categories(manifest))
    act_keys = set(_manifest_act_keys(manifest, _manifest_act_categories(manifest)).values())
    if set(outcomes) & act_ids or {row["act_key"] for row in outcomes.values()} & act_keys:
        raise SchemaRefusal("an other reading is counted in the act partition")
    if canonical_text(claims["other_readings"]) != canonical_text(
        _other_readings_claim(list(outcomes.values()), formats.formats)
    ):
        raise SchemaRefusal("the exported other-readings claim does not follow from its rows")
    delivered = {
        act_id
        for act_id, row in outcomes.items()
        if row["category"] == ArmariumCategory.DELIVERED.value
    }
    citations = _act_citation_sources(sources, "other_citations")
    if set(citations) != delivered or any(
        citations[act_id]["act_key"] != outcomes[act_id]["act_key"] for act_id in citations
    ):
        raise SchemaRefusal(
            "the source other citations do not reconcile to the delivered other readings"
        )
    literals: dict[str, dict[str, tuple]] = {}
    if "jsonl" in formats.formats:
        rows = _other_jsonl_records(root / OTHER_READINGS_MEMBER, sources["regions"])
        if set(rows) != set(outcomes):
            raise SchemaRefusal("other.jsonl does not carry exactly the source other readings")
        for act_id, row in rows.items():
            outcome = outcomes[act_id]
            if any(
                row[field] != outcome[field]
                for field in ("act_key", "page_ordinal", "category", "reason", "text_status")
            ):
                raise SchemaRefusal("other.jsonl does not retain an other reading's exact outcome")
            if row["lot"] != manifest["run"]["lot"]:
                raise SchemaRefusal("other.jsonl does not carry the manifest's lot on every row")
            if act_id in delivered and (
                canonical_text(row["provenance"]) != canonical_text(citations[act_id]["provenance"])
                or canonical_text(row["source_regions"])
                != canonical_text(citations[act_id]["source_regions"])
                or canonical_text(_act_evidence(row))
                != canonical_text(citations[act_id]["evidence"])
            ):
                raise SchemaRefusal(
                    "other.jsonl does not retain a delivered other reading's lineage"
                )
        literals["jsonl"] = {
            act_id: (row[CANONICAL_TEXT_FIELD], row["uncertainty"], row["text_status"])
            for act_id, row in rows.items()
            if act_id in delivered
        }
    if "text-bundle" in formats.formats:
        sections = _text_bundle_other_records(root, sources["pages"])
        if set(sections) != delivered:
            raise SchemaRefusal(
                "the text bundle does not carry exactly the delivered other readings"
            )
        for act_id, (key, _text, _digest, cited, _uncertainty, status) in sections.items():
            expected = tuple(
                (region["declared_path"], region["declared_sha256"])
                for region in citations[act_id]["source_regions"]
            )
            if (
                key != outcomes[act_id]["act_key"]
                or cited != expected
                or status != outcomes[act_id]["text_status"]
            ):
                raise SchemaRefusal(
                    "a text-bundle OTHER section does not match its source other reading"
                )
        literals["text-bundle"] = {
            act_id: (record[1], record[4], record[5]) for act_id, record in sections.items()
        }
    if len({canonical_text(value) for value in literals.values()}) > 1:
        raise SchemaRefusal("the formats carrying the other layer disagree about its readings")

    sealed = _sealed_source_ordinals(sources)
    page_rows = _validate_page_accounting_rows(sources["page_accounting"], sealed, "the package")
    _verify_retained_references_bounded(page_rows)
    if canonical_text(claims["page_accounting"]) != canonical_text(
        _page_accounting_claim(page_rows, sealed)
    ):
        raise SchemaRefusal("the exported page-accounting claim does not follow from its rows")
    if canonical_text(claims["reask"]) != canonical_text(
        _reask_claim(sources["act_readings"], [row["ordinal"] for row in page_rows])
    ):
        raise SchemaRefusal("the exported re-ask claim does not follow from the act readings")
    held_codes = {row["ordinal"]: row["hold_codes"] for row in page_rows if row["hold_codes"]}
    held = set(held_codes) - _operator_released_pages(sources, manifest, held_codes)
    not_measured = {
        entry["instrument"]: entry["detail"] for entry in claims["not_measured"]["entries"]
    }
    if not_measured[_PASS_C]["pages_read"] != len(sealed):
        raise SchemaRefusal(
            "the Pass C claim does not count exactly the package's real sealed pages as read"
        )
    delivered_ids = {
        act_id
        for act_id, category in _manifest_act_categories(manifest).items()
        if category == ArmariumCategory.DELIVERED.value
    }
    bounds = not_measured[_COMPARISON_BOUNDS]
    if bounds["acts_delivered"] != len(delivered_ids):
        raise SchemaRefusal(
            "the comparison-bounds claim does not count exactly the package's delivered acts"
        )
    if not set(bounds["unmeasured_act_ids"]) <= delivered_ids:
        raise SchemaRefusal(
            "the comparison-bounds claim names an unmeasured act the package does not deliver"
        )
    # Each delivered act's pages are where its cited regions were cut, and the
    # aggregate's page attribution must name every one of them.
    act_pages = sources["aggregate_basis"].get("act_pages") or {}
    delivered_pages: set[int] = set()
    for citation in _act_citation_sources(sources).values():
        cut_on = {region["source_page_ordinal"] for region in citation["source_regions"]}
        attributed = act_pages.get(citation["act_key"])
        if not isinstance(attributed, list) or not cut_on <= set(attributed):
            raise SchemaRefusal(
                f"the aggregate's page attribution of {citation['act_key']} does not name every "
                "page its cited regions were cut from"
            )
        delivered_pages |= cut_on
    delivered_pages |= {outcomes[act_id]["page_ordinal"] for act_id in delivered}
    if held & delivered_pages:
        raise SchemaRefusal(
            f"page(s) {sorted(held & delivered_pages)} are held by their page accounting yet "
            "delivered a reading; every reading on a held page is held unless an operator "
            "released it over those holds"
        )


def _jsonl_act_records(
    path: Path, source_graph_regions: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Validate JSONL's one-record-per-act projection and return its categories."""
    records: dict[str, dict[str, Any]] = {}
    for record in _jsonl_rows(path, "acts JSONL", "an acts JSONL row"):
        if not isinstance(record, dict) or record.get("schema") != ACT_RECORD_SCHEMA:
            raise SchemaRefusal("an acts JSONL row has no recognized schema")
        if set(record) != _ACT_RECORD_FIELDS:
            raise SchemaRefusal("an acts JSONL row has an unrecognized field set")
        _verify_retained_references_bounded(record)
        _verify_evidence_refs(record.get("evidence_refs"), subject="an acts JSONL row")
        act_id, act_key, category = (
            record.get("act_id"),
            record.get("act_key"),
            record.get("category"),
        )
        if (
            not _is_nonempty_str(act_id)
            or not _is_nonempty_str(act_key)
            or category not in _KNOWN_CATEGORIES
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
            record.get("uncertainty"),
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts JSONL",
        )
        records[act_id] = {
            "act_key": act_key,
            "lot": record.get("lot"),
            "category": category,
            "evidence": _act_evidence(record),
            "provenance": record.get("provenance"),
            "source_regions": record.get("source_regions"),
            "reason": reason,
            "text_status": record.get("text_status"),
            "reading": record.get("reading"),
            "approval_ref": record.get("approval_ref"),
        }
    return records


def _database_json_layer(encoded: Any, subject: str) -> Any:
    """Decode one JSON-encoded acts-database layer column, or refuse it."""
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise SchemaRefusal(f"the acts database has an untyped {subject} column")
    return _decode_json(encoded, f"the acts database {subject} layer is not JSON")


def _verify_carried_uncertainty(
    layer: Any, status: Any, literal: str | None, *, subject: str
) -> None:
    """Read one act row's uncertainty declaration and its payload as one statement.

    Cross-format identity never reads a single-format package's layer, nor
    ``uncertainty_status`` at all, so both are checked here per row.
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
    text_status: Any, uncertainty: Any, literal: str | None, *, subject: str
) -> None:
    """Read one act row's established-text status and the layer it derives from.

    A well-anchored gap list can still sit beside a row claiming `established`, so
    the status is recomputed from the row's own uncertainty layer.
    """
    if literal is None:
        if text_status is not None:
            raise SchemaRefusal(f"a non-delivered {subject} row carries an established-text status")
        return
    _require_damage_record(text_status, uncertainty, literal, subject=f"{subject} row")


def _database_act_records(
    path: Path, source_graph_regions: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, str]]]:
    """Validate the SQLite one-record-per-act projection and return categories."""
    rows = _read_acts_database(
        path,
        "SELECT act_id, act_key, category, canonical_clean_text, canonical_text_sha256, "
        "provenance_json, source_regions_json, evidence_json, reason, "
        "uncertainty_json, uncertainty_status, text_status, reading, operator_label, "
        "approval_ref, lot FROM acts",
        "the acts database cannot be read for product accounting",
    )
    records: dict[str, dict[str, Any]] = {}
    literals: dict[str, tuple[str, str]] = {}
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
        reading,
        operator_label,
        approval_ref,
        lot,
    ) in rows:
        if (
            not _is_nonempty_str(act_id)
            or not _is_nonempty_str(act_key)
            or category not in _KNOWN_CATEGORIES
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
            parsed = _decode_json(encoded, "the acts database has unreadable provenance evidence")
            _verify_retained_references_bounded(parsed)
            decoded.append(parsed)
        evidence_refs = decoded[2].get("evidence_refs") if isinstance(decoded[2], dict) else None
        _verify_evidence_refs(evidence_refs, subject="an acts database row")
        if category == ArmariumCategory.DELIVERED.value:
            _verify_delivered_product_provenance(
                decoded[0], decoded[1], source_graph_regions, subject="acts database"
            )
        _verify_carried_uncertainty(
            _database_json_layer(uncertainty_json, "uncertainty"),
            uncertainty_status,
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts database",
        )
        _verify_carried_damage(
            text_status,
            _database_json_layer(uncertainty_json, "uncertainty"),
            literal if category == ArmariumCategory.DELIVERED.value else None,
            subject="acts database",
        )
        records[act_id] = {
            "act_key": act_key,
            "lot": lot,
            "category": category,
            "evidence": decoded[2],
            "provenance": decoded[0],
            "source_regions": decoded[1],
            "reason": reason,
            "text_status": text_status,
            "reading": reading,
            "operator_label": operator_label,
            "approval_ref": approval_ref,
        }
    return records, literals


def _review_item_records(path: Path) -> dict[str, dict[str, str]]:
    """Validate the selected review projection's exact terminal population."""
    records: dict[str, dict[str, str]] = {}
    for record in _jsonl_rows(path, "review-items JSONL", "a review-items JSONL row"):
        if not isinstance(record, dict):
            raise SchemaRefusal("a review-items JSONL row is not an object")
        _verify_retained_references_bounded(record)
        act_id, act_key, category, reason = (
            record.get("act_id"),
            record.get("act_key"),
            record.get("category"),
            record.get("reason"),
        )
        if record.get("schema") != REVIEW_ITEM_SCHEMA or set(record) != _REVIEW_ITEM_FIELDS:
            raise SchemaRefusal("a review-items JSONL row has an unrecognized field set")
        if (
            not _is_nonempty_str(act_id)
            or not _is_nonempty_str(act_key)
            or record["kind"] not in {"act", "other"}
            or category not in _REVIEW_CATEGORIES
            or not isinstance(reason, str)
            or not reason
            or act_id in records
        ):
            raise SchemaRefusal("a review-items JSONL row has an invalid act identity or category")
        evidence_refs = record.get("evidence_refs")
        _verify_evidence_refs(evidence_refs, subject="a review-items JSONL row")
        records[act_id] = {
            "act_key": act_key,
            "lot": record["lot"],
            "kind": record["kind"],
            "category": category,
            "reason": reason,
            "evidence_refs": evidence_refs,
        }
    return records


def _product_categories(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {act_id: record["category"] for act_id, record in records.items()}


def _verify_exact_product_outcomes(
    records: dict[str, dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    *,
    subject: str,
    lot: str | None,
) -> None:
    """Preserve terminal categories and their recorded reasons, never just their count.

    Every row carries the manifest's `lot`, so a row copied out of the package still
    names its run.
    """
    if set(records) != set(outcomes):
        raise SchemaRefusal(f"the {subject} does not reconcile to source act outcomes")
    if any(record["lot"] != lot for record in records.values()):
        raise SchemaRefusal(f"the {subject} does not carry the manifest's lot on every row")
    for act_id, record in records.items():
        outcome = outcomes[act_id]
        if (
            record["act_key"] != outcome["act_key"]
            or record["category"] != outcome["category"]
            or record.get("reason") != outcome["reason"]
            or record.get("text_status") != outcome["text_status"]
            or record.get("approval_ref") != outcome.get("approval_ref")
        ):
            raise SchemaRefusal(f"the {subject} does not retain its exact terminal reason")


def _sealed_source_ordinals(sources: dict[str, Any]) -> set[int]:
    """The ordinals of the package's sealed source pages."""
    return {
        page["ordinal"]
        for page in sources["pages"]
        if isinstance(page, dict) and page.get("outcome") == "sealed"
    }


def _act_reading_sources(sources: dict[str, Any], act_keys: dict[str, str]) -> dict[str, Any]:
    """Each act's reading from the source graph, by act id."""
    sealed = _sealed_source_ordinals(sources)
    readings = _validate_act_readings(sources["act_readings"], sealed, "the package")
    if readings.keys() != act_keys.keys() or any(
        row["act_key"] != act_keys[row["act_id"]] for row in sources["act_readings"]
    ):
        raise SchemaRefusal(
            "the source act readings do not reconcile to the manifest act partition"
        )
    return readings


def _verify_product_readings(
    records: dict[str, dict[str, Any]], readings: dict[str, Any], *, subject: str
) -> None:
    """Every act row names the reading its source act came from."""
    if any(record["reading"] != readings.get(act_id) for act_id, record in records.items()):
        raise SchemaRefusal(f"the {subject} does not name the reading each act came from")


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
    per-row MATCH probe misses extra terms and ghost rowids. The integrity command
    writes through its handle, so it runs on a private copy.
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

    Digests do not prove the search column is a fold of its act's literal. The
    fold depends on the Unicode database version, so under a different version
    the recomputation is reported as not run rather than as tampering.
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
    # After the read-only pass, so the index is checked against proven folds.
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
) -> tuple[dict[str, str] | None, dict[str, str | None] | None]:
    """Require every selected act projection to match the manifest denominator.

    Returns the search-fold verification and each act's operator label from the
    acts database, both None without it.
    """
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
    readings = _act_reading_sources(sources, act_keys)
    if "text-bundle" in formats.formats:
        text_records = _text_bundle_records(root, sources["pages"])
        if set(text_records) != delivered:
            raise SchemaRefusal(
                "the text bundle does not contain exactly the manifest's delivered acts"
            )
        for act_id, record in text_records.items():
            if record.heading_key != act_keys[act_id]:
                raise SchemaRefusal(
                    "a text-bundle human heading does not authenticate its machine act identity"
                )
            if record.reading != readings.get(act_id):
                raise SchemaRefusal("a text-bundle act does not name the reading it came from")
            expected_citations = tuple(
                (region["declared_path"], region["declared_sha256"])
                for region in citations[act_id]["source_regions"]
            )
            if record.citations != expected_citations:
                raise SchemaRefusal(
                    "the text bundle does not retain every delivered source citation"
                )
    search_fold_verification = operator_labels = None
    if "acts-database" in formats.formats:
        metadata = dict(
            _read_acts_database(
                root / "acts.sqlite",
                "SELECT key, value FROM export_metadata",
                "the acts database has no readable metadata",
            )
        )
        if metadata.get("schema") != _SQLITE_SCHEMA:
            raise SchemaRefusal("the acts database's schema is not the one this build writes")
        claims = manifest["claims"]
        expected_run = _database_run_metadata(
            manifest["run"],
            {"status": claims["status"], "unresolved_reasons": claims["partial_reasons"]},
        )
        if set(metadata) != _DATABASE_METADATA_KEYS or any(
            metadata[key] != value for key, value in expected_run.items()
        ):
            raise SchemaRefusal(
                "the acts database does not name the package's run, status and partial "
                "reasons as its manifest does"
            )
        database_records, database_literals = _database_act_records(
            root / "acts.sqlite", sources["regions"]
        )
        if _product_categories(database_records) != expected:
            raise SchemaRefusal(
                "the acts database does not reconcile to the manifest act partition"
            )
        _verify_exact_product_outcomes(
            database_records, outcomes, subject="acts database", lot=manifest["run"]["lot"]
        )
        _verify_product_readings(database_records, readings, subject="acts database")
        _verify_exact_delivered_citations(
            database_records, citations, act_keys, subject="acts database"
        )
        operator_labels = {
            act_id: record["operator_label"] for act_id, record in database_records.items()
        }
        search_fold_verification = _verify_search_fold_claim(
            root / "acts.sqlite", database_literals
        )
    if "jsonl" in formats.formats:
        jsonl_records = _jsonl_act_records(root / "acts.jsonl", sources["regions"])
        if _product_categories(jsonl_records) != expected:
            raise SchemaRefusal("the acts JSONL does not reconcile to the manifest act partition")
        _verify_exact_product_outcomes(
            jsonl_records, outcomes, subject="acts JSONL", lot=manifest["run"]["lot"]
        )
        _verify_product_readings(jsonl_records, readings, subject="acts JSONL")
        _verify_exact_delivered_citations(jsonl_records, citations, act_keys, subject="acts JSONL")
    if "review-items" in formats.formats:
        # Every held or refused reading: the acts, then the other readings.
        expected_review = {
            act_id: ("act", outcomes[act_id])
            for act_id, category in expected.items()
            if category in _REVIEW_CATEGORIES
        }
        for act_id, outcome in _other_outcome_sources(sources).items():
            if outcome["category"] in _REVIEW_CATEGORIES:
                expected_review[act_id] = ("other", outcome)
        review_records = _review_item_records(root / "review-items.jsonl")
        if set(review_records) != set(expected_review) or any(
            record["kind"] != expected_review[act_id][0]
            for act_id, record in review_records.items()
        ):
            raise SchemaRefusal(
                "review-items JSONL does not list exactly the package's held and refused "
                "readings, each under its kind"
            )
        _verify_exact_product_outcomes(
            review_records,
            {act_id: outcome for act_id, (_kind, outcome) in expected_review.items()},
            subject="review-items JSONL",
            lot=manifest["run"]["lot"],
        )
    return search_fold_verification, operator_labels


def _verify_pixel_claims(
    manifest: dict[str, Any], formats: ArmariumFormats, sources: dict[str, list[dict[str, Any]]]
) -> None:
    """Check that the manifest's clean-machine claim matches its citations."""
    pixels = _manifest_claim(manifest, "pixels")
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


def _verify_retained_run_claim(manifest: dict[str, Any]) -> None:
    retained = _manifest_claim(manifest, "retained_run_references")
    if retained != {
        "availability": _RUN_ACCESS_REQUIRED,
        "resolution_claim": "artifact and receipt citations require retained-run access",
    }:
        raise SchemaRefusal("the package retained-run reference claim is not the verified claim")


def _verify_canonical_text_claim(manifest: dict[str, Any]) -> None:
    """The one field name and hash convention every literal projection is built from.

    A fixed contract with nothing in ``sources.json`` to recompute it from, so it
    is compared to constants; ``identity_verified_across`` is recomputed from the
    selected formats.
    """
    canonical_text = manifest.get("canonical_text")
    if not isinstance(canonical_text, dict):
        raise SchemaRefusal("the package canonical-text claim is not this build's fixed claim")
    selected_formats = _manifest_format_names(manifest)
    if not isinstance(selected_formats, list):
        raise SchemaRefusal("the package canonical-text claim is not this build's fixed claim")
    if canonical_text != _canonical_text_claim(selected_formats):
        raise SchemaRefusal("the package canonical-text claim is not this build's fixed claim")


def _verify_uncertainty_claim(manifest: dict[str, Any]) -> None:
    """The carriage claim is recomputed from the selected literal-text formats."""
    uncertainty = _manifest_claim(manifest, "uncertainty")
    if uncertainty != _uncertainty_claim(_manifest_format_names(manifest) or []):
        raise SchemaRefusal("the package uncertainty claim is not the canonical carriage claim")


def _verify_manifest_source_counts(
    manifest: dict[str, Any], sources: dict[str, list[dict[str, Any]]]
) -> None:
    """A source-page census claim must be no larger or smaller than its citations.

    The submission counts are checked against the page census, which
    `run.py::page_census` keeps equal to the submitted sources.
    """
    ordinals: set[int] = set()
    paths: set[str] = set()
    for page in sources["pages"]:
        if not isinstance(page, dict):
            raise SchemaRefusal("a package source row is not an object")
        ordinal, path = page.get("ordinal"), page.get("declared_path")
        if not is_plain_int(ordinal) or ordinal in ordinals or not isinstance(path, str):
            raise SchemaRefusal(
                "the package source census has duplicate or invalid page identities"
            )
        ordinals.add(ordinal)
        paths.add(path)
    page_census = _manifest_claim(manifest, "page_census")
    submission = _manifest_claim(manifest, "submission_inventory")
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


def _act_outcomes(acts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Keep the non-text terminal reason that review-items must reproduce exactly.

    `text_status` carries no characters of the reading, so it travels here too and
    every format is checked against it.
    """
    return [
        {
            **_row_head(act),
            "text_status": act.get("text_status"),
            "approval_ref": act.get("approval_ref"),
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
        _validate_run_relative_path(reference.get("run_relative_path"))
    else:
        raise SchemaRefusal("a package source reference has no honest availability status")


_MAXIMUM_MEMBER_DEPTH: Final = 32


def _validate_member_name(name: str) -> None:
    subject = f"package member path {name!r}" if isinstance(name, str) else "a package member path"
    if isinstance(name, str) and name.endswith("/"):
        raise SchemaRefusal(f"{subject} is unsafe")
    path = _reject_unsafe_relative_path(name, subject=subject)
    # `_ordinary_member_names` walks recursively. Real packages nest a few levels;
    # this bound is far above them and far below the interpreter's limit.
    if len(path.parts) > _MAXIMUM_MEMBER_DEPTH:
        raise SchemaRefusal(
            f"{subject} nests {len(path.parts)} levels deep, past the "
            f"{_MAXIMUM_MEMBER_DEPTH}-component package member bound"
        )
    if path.as_posix() != name:
        # Two spellings of one path would overwrite each other on extraction.
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
