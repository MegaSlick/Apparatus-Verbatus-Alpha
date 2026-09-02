"""Reference records: RecordGold expert truth, joined to a sealed page.

`SPEC.md` §5.3(a)/(b): RecordGold truth is a *reference* corpus, not a `gold/`
record. `gold/core.py` requires two independently-named human readings and derives
`outcome` from adjudicating them (`gold/core.py:922-1069`, `:795-811`,
`:1656-1664`); RecordGold supplies one expert reading, unnamed, from a third
party. Forcing it into `gold/` would mean inventing two transcriber names for one
text and fabricating the `agreed` custody chain `gold/` exists to prevent. So this
family lives here, carries its own provenance class plainly (`provenance:
"third-party-expert-annotation"`, `independent_readings: 1`, `adjudicated_by:
null`, `provenance_class: "cleared_public"`), and is never filed beside a gold
record.

`completeness: "records-only"` is load-bearing, not decoration. RecordGold
annotates *records* — index rows, marginalia, and notes are acts under
`GLOSSARY.md` and are outside Teklia's annotation scope. A page's `acts` list is
therefore a lower bound on what is on the page, never a claim that every act was
found: an unmatched pipeline act is not automatically a false positive, and
`compare.py` must never score it as one on that basis alone.

**No `act_*` identity is ever minted here.** `common/contracts/identities.py:180-
211` binds an act's *originally minted* bounds; the Designator's own structure
pass mints its own rectangle, which will never equal an expert box. Minting
`act_id` from a RecordGold box would be well-formed, would verify against its own
bindings, and would mean nothing — a forgery that passes silently forever. This
module instead mints the ladder built for exactly this join:
`physical_act_id(physical_page_id(corpus_id, "<source>/<volume>", designation),
record_id)` — a `pac_` identity, disjoint from `act_*` by prefix, stable across
re-fetch and re-shard, and never accepted where an `act_` identity is expected.

**Each act carries the expert `text`, and a `text_sha256` beside it.** The unit
brief's shorthand for an act -- `{record_id, physical_act_id, region}` -- names
the fields this module's identities section introduces; it is not a claim that
`compare.py` can score CER/WER against a checked reference with no text to check
against. `SPEC.md` Section 5.3(d) requires compare.py to score every matched act
with the existing `normalization.py`/`scoring.py` instruments, and a reference
record with no text cannot supply that reference string. Carrying `text_sha256`
alongside `text` mirrors `rows.py`'s own convention and lets a reader verify the
text travelled unmodified from the row snapshot to this record -- an engineering
decision recorded here under hard rule 13.
"""

from __future__ import annotations

from typing import Any

from common.contracts.canonical import digest_bytes, is_sha256, self_hash, verify_self_hash
from common.contracts.errors import IdentityRefusal
from common.contracts.identities import (
    is_well_formed,
    physical_act_id,
    physical_page_id,
)

from . import CorpusRefusal

SCHEMA = "reference-page-truth.v1"
CORPUS_ID = "recordgold"

COMPLETENESS = "records-only"
PROVENANCE = "third-party-expert-annotation"
INDEPENDENT_READINGS = 1
PROVENANCE_CLASS = "cleared_public"

# Closed — the only splits a reference page may ever declare, matching
# `rows.SPLITS`. Not imported from there: this module's contract is `record_id` +
# `split` strings handed to it by a caller, not the row snapshot's own shape, and
# a fourth split arriving here must refuse by the same name either way.
SPLITS = frozenset({"train", "val", "test"})

REFERENCE_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "wrong-schema",
        "wrong-corpus",
        "unknown-split",
        "split-not-present",
        "empty-acts",
        "duplicate-record-id",
        "duplicate-region",
        "act-count-mismatch",
        "region-outside-page",
        "empty-text",
        "text-sha256-mismatch",
        "wrong-identity-family",
        "unmintable-physical-act",
        "duplicate-physical-act-id",
        "self-hash-mismatch",
    }
)

_PAGE_FIELDS = frozenset({"sha256", "width", "height"})
_REGION_FIELDS = frozenset({"x", "y", "w", "h"})
_ACT_FIELDS = frozenset({"record_id", "physical_act_id", "region", "text", "text_sha256"})
_TOP_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "page",
        "source",
        "volume",
        "designation",
        "split",
        "splits_present",
        "expected_act_count",
        "acts",
        "completeness",
        "provenance",
        "independent_readings",
        "adjudicated_by",
        "provenance_class",
        "self_hash",
    }
)


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _non_empty_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusRefusal(f"malformed-record: {what} must be a non-empty string")
    return value


def _positive_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CorpusRefusal(f"malformed-record: {what} must be a positive integer")
    return value


def _region(value: Any, what: str) -> dict[str, int]:
    region = _closed(value, _REGION_FIELDS, what)
    if any(not isinstance(region[key], int) or isinstance(region[key], bool) for key in region):
        raise CorpusRefusal(f"malformed-record: {what} bounds must be plain integers")
    if region["x"] < 0 or region["y"] < 0 or region["w"] <= 0 or region["h"] <= 0:
        raise CorpusRefusal(f"malformed-record: {what} must have non-negative x/y and positive w/h")
    return region


def build_reference_page(
    *,
    page: dict[str, Any],
    source: str,
    volume: str,
    designation: str,
    split: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Mint one `reference-page-truth.v1` from a page's facts and its expert records.

    `records` is the caller's own per-act facts for this page: each entry is
    `{"record_id": str, "region": {x,y,w,h}, "split": str, "text": str,
    "text_sha256": str}`, in RecordGold's page
    pixel space (the sealed raster's own coordinate frame — `SPEC.md` §5.1 point
    3's dimension check is what makes that frame trustworthy). `physical_act_id`
    is minted here, from the physical page identity and each record's own
    `record_id`, never accepted as caller-supplied: a reference record cannot
    silently carry a forged join key.

    `split` is the split this reference set is built to serve — the caller's own
    declared role for this page (`SPEC.md` §5.4's `val`/`test` roles) — and must
    be a split actually present among `records`; `splits_present` is derived from
    the records themselves, not restated by the caller, so a mismatch between the
    two can never enter unnoticed.
    """
    page = _closed(page, _PAGE_FIELDS, "page")
    if not is_sha256(page["sha256"]):
        raise CorpusRefusal("malformed-record: page.sha256 must be a lowercase sha256 digest")
    width = _positive_int(page["width"], "page.width")
    height = _positive_int(page["height"], "page.height")
    for name, value in (("source", source), ("volume", volume), ("designation", designation)):
        _non_empty_str(value, name)
    if split not in SPLITS:
        raise CorpusRefusal(f"unknown-split: {split!r} is not one of {sorted(SPLITS)}")
    if not records:
        raise CorpusRefusal("empty-acts: a reference page must carry at least one record")

    try:
        physical_page = physical_page_id(CORPUS_ID, f"{source}/{volume}", designation)
    except IdentityRefusal as error:
        raise CorpusRefusal(f"unmintable-physical-act: {error}") from None

    splits_present: set[str] = set()
    seen_record_ids: set[str] = set()
    seen_regions: set[tuple[int, int, int, int]] = set()
    seen_physical_act_ids: set[str] = set()
    acts: list[dict[str, Any]] = []
    for entry in records:
        entry = _closed(
            entry,
            frozenset({"record_id", "region", "split", "text", "text_sha256"}),
            "record entry",
        )
        record_id = _non_empty_str(entry["record_id"], "record entry record_id")
        if record_id in seen_record_ids:
            raise CorpusRefusal(f"duplicate-record-id: {record_id!r} appears twice on this page")
        seen_record_ids.add(record_id)
        entry_split = entry["split"]
        if entry_split not in SPLITS:
            raise CorpusRefusal(
                f"unknown-split: record {record_id!r} has split {entry_split!r}, "
                f"not one of {sorted(SPLITS)}"
            )
        splits_present.add(entry_split)
        region = _region(entry["region"], f"record {record_id!r} region")
        if region["x"] + region["w"] > width or region["y"] + region["h"] > height:
            raise CorpusRefusal(
                f"region-outside-page: record {record_id!r} region {region} exceeds "
                f"the page's {width}x{height} bounds"
            )
        region_key = (region["x"], region["y"], region["w"], region["h"])
        if region_key in seen_regions:
            raise CorpusRefusal(
                f"duplicate-region: record {record_id!r} repeats a region already "
                "claimed by another record on this page"
            )
        seen_regions.add(region_key)
        text = entry["text"]
        if not isinstance(text, str) or not text:
            raise CorpusRefusal(f"empty-text: record {record_id!r} carries no text")
        expected_text_sha256 = digest_bytes(text.encode("utf-8"))
        if entry["text_sha256"] != expected_text_sha256:
            raise CorpusRefusal(
                f"text-sha256-mismatch: record {record_id!r} carries "
                f"{entry['text_sha256']!r}, recomputed {expected_text_sha256!r}"
            )
        try:
            act_identity = physical_act_id(physical_page, record_id)
        except IdentityRefusal as error:
            raise CorpusRefusal(f"unmintable-physical-act: {error}") from None
        if act_identity in seen_physical_act_ids:
            raise CorpusRefusal(
                f"duplicate-physical-act-id: record {record_id!r} mints {act_identity!r}, "
                "already minted by another record_id on this page -- two record_ids that "
                "fold to the same declared text (NFC/whitespace) are not distinguishable "
                "join keys"
            )
        seen_physical_act_ids.add(act_identity)
        acts.append(
            {
                "record_id": record_id,
                "physical_act_id": act_identity,
                "region": region,
                "text": text,
                "text_sha256": entry["text_sha256"],
            }
        )

    if split not in splits_present:
        raise CorpusRefusal(
            f"split-not-present: this reference page is declared for split {split!r}, "
            f"but its records only carry {sorted(splits_present)}"
        )

    acts.sort(key=lambda act: act["record_id"])
    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "page": page,
        "source": source,
        "volume": volume,
        "designation": designation,
        "split": split,
        "splits_present": sorted(splits_present),
        "expected_act_count": len(acts),
        "acts": acts,
        "completeness": COMPLETENESS,
        "provenance": PROVENANCE,
        "independent_readings": INDEPENDENT_READINGS,
        "adjudicated_by": None,
        "provenance_class": PROVENANCE_CLASS,
    }
    body["self_hash"] = self_hash(body)
    return validate_reference_page(body)


def validate_reference_page(reference: Any) -> dict[str, Any]:
    """Refuse a reference page that is not exactly `reference-page-truth.v1`."""
    reference = _closed(reference, _TOP_FIELDS, "reference page")
    if reference["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {reference['schema']!r}")
    if reference["corpus_id"] != CORPUS_ID:
        raise CorpusRefusal(f"wrong-corpus: expected {CORPUS_ID!r}, got {reference['corpus_id']!r}")

    page = _closed(reference["page"], _PAGE_FIELDS, "page")
    if not is_sha256(page["sha256"]):
        raise CorpusRefusal("malformed-record: page.sha256 must be a lowercase sha256 digest")
    width = _positive_int(page["width"], "page.width")
    height = _positive_int(page["height"], "page.height")

    for name in ("source", "volume", "designation"):
        _non_empty_str(reference[name], name)

    try:
        physical_page = physical_page_id(
            reference["corpus_id"],
            f"{reference['source']}/{reference['volume']}",
            reference["designation"],
        )
    except IdentityRefusal as error:
        raise CorpusRefusal(f"unmintable-physical-act: {error}") from None

    if reference["split"] not in SPLITS:
        raise CorpusRefusal(f"unknown-split: {reference['split']!r} is not one of {sorted(SPLITS)}")

    splits_present = reference["splits_present"]
    if not isinstance(splits_present, list) or not all(
        isinstance(split, str) for split in splits_present
    ):
        raise CorpusRefusal("malformed-record: splits_present must be a list of strings")
    if splits_present != sorted(set(splits_present)):
        raise CorpusRefusal("malformed-record: splits_present must be a sorted, deduplicated list")
    for split in splits_present:
        if split not in SPLITS:
            raise CorpusRefusal(f"unknown-split: splits_present names {split!r}")
    if reference["split"] not in splits_present:
        raise CorpusRefusal(
            f"split-not-present: split {reference['split']!r} is not in splits_present"
        )

    acts = reference["acts"]
    if not isinstance(acts, list) or not acts:
        raise CorpusRefusal("empty-acts: a reference page must carry at least one act")
    if reference["expected_act_count"] != len(acts):
        raise CorpusRefusal(
            f"act-count-mismatch: expected_act_count is {reference['expected_act_count']!r} "
            f"but acts carries {len(acts)}"
        )

    seen_record_ids: set[str] = set()
    seen_regions: set[tuple[int, int, int, int]] = set()
    seen_physical_act_ids: set[str] = set()
    for act in acts:
        act = _closed(act, _ACT_FIELDS, "reference act")
        record_id = _non_empty_str(act["record_id"], "reference act record_id")
        if record_id in seen_record_ids:
            raise CorpusRefusal(f"duplicate-record-id: {record_id!r} appears twice")
        seen_record_ids.add(record_id)
        if not is_well_formed(act["physical_act_id"]) or not act["physical_act_id"].startswith(
            "pac_"
        ):
            raise CorpusRefusal(
                f"wrong-identity-family: reference act {record_id!r} carries "
                f"{act['physical_act_id']!r}, which is not a well-formed pac_ identity"
            )
        try:
            expected_physical_act_id = physical_act_id(physical_page, record_id)
        except IdentityRefusal as error:
            raise CorpusRefusal(f"unmintable-physical-act: {error}") from None
        if act["physical_act_id"] != expected_physical_act_id:
            raise CorpusRefusal(
                f"wrong-identity-family: reference act {record_id!r} carries "
                f"{act['physical_act_id']!r}, which does not recompute from this page's "
                f"declared corpus_id/source/volume/designation and record_id (expected "
                f"{expected_physical_act_id!r}) -- a pac_ id minted for a different page "
                "or record must never verify against this one"
            )
        if expected_physical_act_id in seen_physical_act_ids:
            raise CorpusRefusal(
                f"duplicate-physical-act-id: record {record_id!r} mints "
                f"{expected_physical_act_id!r}, already minted by another record_id on "
                "this page"
            )
        seen_physical_act_ids.add(expected_physical_act_id)
        region = _region(act["region"], f"reference act {record_id!r} region")
        if region["x"] + region["w"] > width or region["y"] + region["h"] > height:
            raise CorpusRefusal(
                f"region-outside-page: act {record_id!r} region {region} exceeds "
                f"the page's {width}x{height} bounds"
            )
        region_key = (region["x"], region["y"], region["w"], region["h"])
        if region_key in seen_regions:
            raise CorpusRefusal(f"duplicate-region: act {record_id!r} repeats a claimed region")
        seen_regions.add(region_key)
        text = act["text"]
        if not isinstance(text, str) or not text:
            raise CorpusRefusal(f"empty-text: reference act {record_id!r} carries no text")
        expected_text_sha256 = digest_bytes(text.encode("utf-8"))
        if act["text_sha256"] != expected_text_sha256:
            raise CorpusRefusal(
                f"text-sha256-mismatch: reference act {record_id!r} carries "
                f"{act['text_sha256']!r}, recomputed {expected_text_sha256!r}"
            )

    if reference["completeness"] != COMPLETENESS:
        raise CorpusRefusal(
            f"malformed-record: completeness must be {COMPLETENESS!r}, "
            f"got {reference['completeness']!r}"
        )
    if reference["provenance"] != PROVENANCE:
        raise CorpusRefusal(
            f"malformed-record: provenance must be {PROVENANCE!r}, got {reference['provenance']!r}"
        )
    if reference["independent_readings"] != INDEPENDENT_READINGS:
        raise CorpusRefusal(
            "malformed-record: independent_readings must be "
            f"{INDEPENDENT_READINGS!r}, got {reference['independent_readings']!r}"
        )
    if reference["adjudicated_by"] is not None:
        raise CorpusRefusal(
            "malformed-record: adjudicated_by must be null — a reference page carries "
            "one unnamed expert reading, never an adjudication"
        )
    if reference["provenance_class"] != PROVENANCE_CLASS:
        raise CorpusRefusal(
            f"malformed-record: provenance_class must be {PROVENANCE_CLASS!r}, "
            f"got {reference['provenance_class']!r}"
        )

    if not verify_self_hash(reference):
        raise CorpusRefusal(
            "self-hash-mismatch: reference page self_hash does not verify against its own content"
        )
    return reference


__all__ = [
    "SCHEMA",
    "CORPUS_ID",
    "REFERENCE_REFUSAL_REASONS",
    "build_reference_page",
    "validate_reference_page",
]
