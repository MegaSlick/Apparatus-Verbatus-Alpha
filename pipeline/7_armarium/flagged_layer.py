"""The flagged layer of an Armarium package: every reading the Recensor held or flagged, with its text.

The established export stays as strict as it is: `acts.jsonl`, the text bundle,
the database and the CSV deliver established text only. Beside them this layer
carries, for a person's review and for training, one row per counted reading
that the Recensor held or that carries a review flag (`common.page_accounting`,
`[flags]`): the reading's text as the model read it, its hold codes, its flag
codes, its review priority (`common.page_review.REVIEW_PRIORITY`) and its
category, so a held reading keeps its text and a flagged one keeps its reasons.

Every row says what its text is. A delivered reading's row is
`established-with-flags` and its text is the established text; any other row is
`not-established`, its text labelled "model reading, not established", or null
for a row standing for a page with no reading (unread or blank). No row is an
act count: `acts.jsonl` is, and `review-items.jsonl` stays the text-free queue.

`sources.json` carries every row (`flagged_readings`) so the label travels in
every package; `flagged.jsonl` carries the same rows when the `review-items`
format is selected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import SchemaRefusal
from common.contracts.outcomes import ArmariumCategory
from common.page_review import (
    REVIEW_PRIORITY_INFORMATIONAL,
    REVIEW_PRIORITY_LOOK_FIRST,
    review_priority,
)

FLAGGED_MEMBER: Final = "flagged.jsonl"
SOURCES_FIELD: Final = "flagged_readings"
ROW_SCHEMA: Final = "armarium-flagged-reading.v1"
ESTABLISHED_STATUS: Final = "established-with-flags"
NOT_ESTABLISHED_STATUS: Final = "not-established"
STATUSES: Final = frozenset({ESTABLISHED_STATUS, NOT_ESTABLISHED_STATUS})
ESTABLISHED_LABEL: Final = "established"
NOT_ESTABLISHED_LABEL: Final = "model reading, not established"
NO_READING_LABEL: Final = "no reading: a page row"
LABELS: Final = frozenset({ESTABLISHED_LABEL, NOT_ESTABLISHED_LABEL, NO_READING_LABEL})
ROW_FIELDS: Final = frozenset(
    {
        "schema",
        "act_id",
        "act_key",
        "lot",
        "kind",
        "page_ordinal",
        "status",
        "category",
        "review_priority",
        "hold_codes",
        "flag_codes",
        "text",
        "text_label",
        "reason",
        "perlectio_ref",
        "page_reading_ref",
        "recensor_ref",
        "evidence_refs",
    }
)


def flagged_row(
    row: Mapping[str, Any],
    *,
    category: str,
    hold_codes: Sequence[str],
    flag_codes: Sequence[str],
    priority: int | None,
    text: str | None,
    reason: str | None,
    recensor_ref: Mapping[str, str],
    evidence_refs: Sequence[Mapping[str, str]],
    lot: str | None,
) -> dict[str, Any] | None:
    """One flagged-layer row for a counted reading, or None when nothing holds or flags it.

    `row` is the denominator's row; `text` is the established text of a
    delivered reading, else the model's reading from its Perlectio, or None
    for a page row with no reading.
    """
    if not hold_codes and not flag_codes:
        return None
    delivered = category == ArmariumCategory.DELIVERED.value
    if row["perlectio_ref"] is None:
        label = NO_READING_LABEL
    else:
        label = ESTABLISHED_LABEL if delivered else NOT_ESTABLISHED_LABEL
    return {
        "schema": ROW_SCHEMA,
        "act_id": row["act_id"],
        "act_key": row["act_key"],
        "lot": lot,
        "kind": row["kind"],
        "page_ordinal": row["page_ordinal"],
        "status": ESTABLISHED_STATUS if delivered else NOT_ESTABLISHED_STATUS,
        "category": category,
        "review_priority": priority,
        "hold_codes": sorted(set(hold_codes)),
        "flag_codes": sorted(set(flag_codes)),
        "text": text,
        "text_label": label,
        "reason": reason,
        "perlectio_ref": row["perlectio_ref"],
        "page_reading_ref": row["reading_ref"],
        "recensor_ref": dict(recensor_ref),
        "evidence_refs": [dict(reference) for reference in evidence_refs],
    }


def _codes(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(code, str) and code for code in value)


def verify_rows(
    rows: Any,
    *,
    outcomes: Mapping[str, Mapping[str, Any]],
    kinds: Mapping[str, str],
    literals: Mapping[str, str | None] | None,
    lot: str | None,
    subject: str,
) -> dict[str, dict[str, Any]]:
    """The flagged rows one place shows, each held to the package's own accounting.

    `outcomes` is every counted reading's terminal record by id (acts and other
    readings), `kinds` each id's kind, `literals` each delivered reading's
    established text by id when the package carries a literal format (None
    when it does not). Refused: a row of no counted reading, a row whose
    category, kind or key disagrees with the reading's, a row that holds and
    flags nothing, a status or label that does not follow the category, a
    priority that does not follow the codes, and an established row whose text
    is not the delivered literal.
    """
    if not isinstance(rows, list):
        raise SchemaRefusal(f"{subject} carries flagged readings that are not a list")
    verified: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("schema") != ROW_SCHEMA:
            raise SchemaRefusal(f"a flagged-reading row in {subject} has no recognized schema")
        if set(row) != ROW_FIELDS:
            raise SchemaRefusal(f"a flagged-reading row in {subject} has an unrecognized field set")
        act_id = row["act_id"]
        outcome = outcomes.get(act_id)
        if outcome is None or act_id in verified:
            raise SchemaRefusal(
                f"{subject} shows a flagged reading no counted reading has, or shows one twice"
            )
        if (
            row["act_key"] != outcome["act_key"]
            or row["category"] != outcome["category"]
            or row["kind"] != kinds[act_id]
            or row["lot"] != lot
            or row["reason"] != outcome["reason"]
        ):
            raise SchemaRefusal(
                f"{subject} shows flagged reading {act_id!r} with another key, kind, lot, "
                "category or reason than the package's accounting"
            )
        if not _codes(row["hold_codes"]) or not _codes(row["flag_codes"]):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} names codes that are not codes"
            )
        codes = set(row["hold_codes"]) | set(row["flag_codes"])
        if not codes:
            raise SchemaRefusal(f"a flagged-reading row in {subject} is neither held nor flagged")
        if row["review_priority"] != review_priority(codes) or not (
            REVIEW_PRIORITY_LOOK_FIRST <= row["review_priority"] <= REVIEW_PRIORITY_INFORMATIONAL
        ):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} carries a review priority its codes do "
                "not give"
            )
        delivered = row["category"] == ArmariumCategory.DELIVERED.value
        if row["status"] != (ESTABLISHED_STATUS if delivered else NOT_ESTABLISHED_STATUS):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} says {row['status']!r} of a "
                f"{row['category']} reading"
            )
        if delivered and row["hold_codes"]:
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} says a delivered reading is held"
            )
        label, text = row["text_label"], row["text"]
        if label not in LABELS or not (text is None or isinstance(text, str)):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} labels its text unrecognizably"
            )
        if (label == NO_READING_LABEL) != (row["perlectio_ref"] is None) or (
            label == NO_READING_LABEL and text is not None
        ):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} carries text for a page row, or labels a "
                "reading as having none"
            )
        if label != NO_READING_LABEL and (
            (label == ESTABLISHED_LABEL) != delivered or not isinstance(text, str)
        ):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} labels its text against its category, or "
                "carries none for a reading"
            )
        if delivered and literals is not None and text != literals.get(act_id):
            raise SchemaRefusal(
                f"a flagged-reading row in {subject} shows an established reading with text "
                "other than the delivered literal"
            )
        verified[act_id] = row
    return verified
