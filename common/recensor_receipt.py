"""A self-hashed, scoped partition receipt for the Recensor boundary.

This is deliberately not the pipeline's final export verdict.  It proves only
the act denominator and the configured-witness denominator at the point the
Recensor has reviewed them.  Page-level blank proof, residual ink, and final
Archetypus/Armarium categories require evidence this receipt does not pretend
to own.

It counts a page-read run's units (`common.stage.reading_acts`, the classes in
`COUNTED_READING_CLASSES`; `expected_unit_count`): it names every sealed page's `page-reading` by its
page ordinal (`page_reading_refs`, `[{page_ordinal, reading_ref}]` in page
order), every sealed page has at least one unit and every unit's page is one of
them, and each item carries the unit's page disposition instead of a Designator
outcome. A unit its page reading held is completed at the Recensor only with the
reason its review released it (`release_reason`); so released, it is resolved.
A held unit with no completed review keeps the receipt `partial`.
`continuation_links` names every page break an answer flags, so a break only
one side says the text runs across keeps the run partial.
"""

from __future__ import annotations

import re
from typing import Any, Final

from common.contracts.canonical import is_plain_int, is_sha256, self_hash, verify_self_hash
from common.contracts.envelope import digest_ref
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.outcomes import (
    WITNESS_READING_OUTCOMES,
    OutcomeClass,
    classify,
    witnessed_count,
)
from common.contracts.stages import ATTESTATORES, RECENSOR

RECENSOR_PARTITION_RECEIPT_SCHEMA_V3: Final = "recensor-partition-receipt.v3"
# Receipts over the Designator's proposal acts, which no run counts any more.
RETIRED_RECENSOR_PARTITION_RECEIPT_SCHEMAS: Final = frozenset(
    {"recensor-partition-receipt.v1", "recensor-partition-receipt.v2"}
)
RECENSOR_READING_RECEIPT_SCOPE: Final = "reading-acts-and-configured-witnesses"
PAGE_DISPOSITIONS: Final = frozenset({"read", "held"})
# `p<page ordinal>:<entry n>`, or the page row of a page with no entry.
_READING_ACT_KEY: Final = re.compile(r"p[1-9][0-9]*:(?:[1-9][0-9]*|unread|blank)")
_READING_ACT_PAGE: Final = re.compile(r"p([1-9][0-9]*):")
_COMMON_FIELDS: Final = frozenset(
    {
        "schema",
        "run_id",
        "config_digest",
        "scope",
        "items",
        "by_partition_class",
        "recensor_status",
        "reasons",
        "self_hash",
    }
)
_READING_FIELDS: Final = frozenset(
    {"page_reading_refs", "expected_unit_count", "continuation_links"}
)
CONTINUATION_LINK_OUTCOMES: Final = frozenset({"accepted", "held-for-review"})
_LINK_FIELDS: Final = frozenset({"subject_id", "link_ref", "outcome"})
_PARTITION_KEYS: Final = tuple(klass.value for klass in OutcomeClass)


def build_recensor_reading_receipt(
    *,
    run_id: str,
    config_digest: str,
    page_reading_refs: list[dict[str, Any]],
    items: list[dict[str, Any]],
    continuation_links: list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a v3 receipt over a page-read run's units, its summary derived from its items.

    `page_reading_refs` names every sealed page's `page-reading` by its page
    ordinal, `[{page_ordinal, reading_ref}]` in page order;
    each item is one counted `reading_acts` row as the Recensor reviewed it:
    `{act_id, act_key, page_disposition, review_ref, review_outcome,
    partition_class, coverage, release_reason}`, `release_reason` being the
    reason the Recensor's review record gives for completing a unit its page
    reading held, and `None` otherwise. Each continuation link is
    `{subject_id, link_ref, outcome}` for one flagged page break.
    """

    checked_items = [dict(item) for item in items]
    for item in checked_items:
        _validate_item(item)
    checked_items.sort(key=lambda item: item["act_id"])
    links = sorted((dict(link) for link in continuation_links), key=_link_order)
    for link in links:
        _validate_link(link)
    reasons = _reasons(checked_items, links=links)
    record: dict[str, Any] = {
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA_V3,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_READING_RECEIPT_SCOPE,
        "page_reading_refs": [
            {"page_ordinal": row["page_ordinal"], "reading_ref": dict(row["reading_ref"])}
            for row in page_reading_refs
        ],
        "expected_unit_count": len(checked_items),
        "continuation_links": links,
        "items": checked_items,
        "by_partition_class": _partition_counts(checked_items),
        "recensor_status": _status(reasons),
        "reasons": reasons,
    }
    record["self_hash"] = self_hash(record)
    return validate_recensor_partition_receipt(record)


def validate_recensor_partition_receipt(record: Any) -> dict[str, Any]:
    """Validate the closed receipt schema and its derived summary."""

    schema = record.get("schema") if isinstance(record, dict) else None
    if isinstance(schema, str) and schema in RETIRED_RECENSOR_PARTITION_RECEIPT_SCHEMAS:
        raise SchemaRefusal(
            f"Recensor partition receipt was written as {schema}, which counts the Designator's "
            "proposal acts this build no longer has; re-run the Recensor"
        )
    if not isinstance(record, dict) or set(record) != _COMMON_FIELDS | _READING_FIELDS:
        raise SchemaRefusal("Recensor partition receipt has the wrong closed schema")
    if schema != RECENSOR_PARTITION_RECEIPT_SCHEMA_V3 or not verify_self_hash(record):
        raise SchemaRefusal("Recensor partition receipt has an invalid schema or self-hash")
    count = expected_count(record)
    if (
        not isinstance(record["run_id"], str)
        or not record["run_id"]
        or not is_sha256(record["config_digest"])
        or record["scope"] != RECENSOR_READING_RECEIPT_SCOPE
        or not _is_count(count)
        or not isinstance(record["items"], list)
        or count != len(record["items"])
    ):
        raise SchemaRefusal("Recensor partition receipt has invalid run or denominator facts")
    links = record["continuation_links"]
    if not isinstance(links, list):
        raise SchemaRefusal("Recensor partition receipt v3 continuation_links is not a list")
    for link in links:
        _validate_link(link)
    subjects = [link["subject_id"] for link in links]
    if subjects != sorted(set(subjects), key=lambda subject: _link_order({"subject_id": subject})):
        raise SchemaRefusal(
            "Recensor partition receipt v3 continuation links are not one per page break, "
            "in page order"
        )
    previous_act_id = ""
    for item in record["items"]:
        _validate_item(item)
        act_id = item["act_id"]
        if not act_id or act_id <= previous_act_id:
            raise SchemaRefusal(
                "Recensor partition receipt items must be strictly sorted by unique act identity"
            )
        previous_act_id = act_id
    _validate_page_readings(record)
    if record["by_partition_class"] != _partition_counts(record["items"]):
        raise SchemaRefusal("Recensor partition receipt partition counts do not reconcile")
    reasons = _reasons(record["items"], links=links)
    if record["reasons"] != reasons or record["recensor_status"] != _status(reasons):
        raise SchemaRefusal("Recensor partition receipt status does not derive from its items")
    return record


def expected_count(record: dict[str, Any]) -> Any:
    """The receipt's sealed denominator count: the units it reviewed."""
    return record["expected_unit_count"]


def _link_order(link: dict[str, Any]) -> tuple[int, ...]:
    subject = link["subject_id"]
    return tuple(int(part) for part in subject.split(":")[1:])


def _validate_link(link: Any) -> None:
    if not isinstance(link, dict) or set(link) != _LINK_FIELDS:
        raise SchemaRefusal("Recensor partition receipt continuation link has the wrong shape")
    subject = link["subject_id"]
    parts = subject.split(":") if isinstance(subject, str) else []
    if (
        len(parts) != 3
        or parts[0] != "page-break"
        or not all(part.isdigit() and str(int(part)) == part for part in parts[1:])
        or int(parts[2]) != int(parts[1]) + 1
    ):
        raise SchemaRefusal(
            f"Recensor partition receipt names continuation link {subject!r}, not "
            "page-break:<p>:<p+1>"
        )
    if link["outcome"] not in CONTINUATION_LINK_OUTCOMES:
        raise SchemaRefusal(
            f"Recensor partition receipt continuation link {subject} is {link['outcome']!r}, "
            f"not one of {sorted(CONTINUATION_LINK_OUTCOMES)}"
        )
    _validate_reference(link["link_ref"], "continuation-link reference")


def _validate_page_readings(record: dict[str, Any]) -> None:
    """Every sealed page's reading once, by ordinal, each page with a unit and no unit elsewhere."""
    rows = record["page_reading_refs"]
    if not isinstance(rows, list) or not rows:
        raise SchemaRefusal(
            "Recensor partition receipt v3 names no page reading; every sealed page's "
            "reading is part of its denominator"
        )
    ordinals = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"page_ordinal", "reading_ref"}
            or not is_plain_int(row["page_ordinal"])
            or row["page_ordinal"] < 1
        ):
            raise SchemaRefusal(
                "Recensor partition receipt v3 page reading is not {page_ordinal, reading_ref} "
                "with a positive page ordinal"
            )
        _validate_reference(row["reading_ref"], "page-reading reference")
        ordinals.append(row["page_ordinal"])
    if ordinals != sorted(set(ordinals)):
        raise SchemaRefusal(
            "Recensor partition receipt v3 page readings are not keyed by strictly increasing "
            "page ordinals; each sealed page is named once, in page order"
        )
    paths = [row["reading_ref"]["relative_path"] for row in rows]
    if len(set(paths)) != len(paths):
        raise SchemaRefusal("Recensor partition receipt v3 names one page reading twice")
    keys = [item.get("act_key") if isinstance(item, dict) else None for item in record["items"]]
    if len(set(map(str, keys))) != len(keys):
        raise SchemaRefusal("Recensor partition receipt v3 names one act key twice")
    pages = {
        int(match[1])
        for key in keys
        if isinstance(key, str) and (match := _READING_ACT_PAGE.match(key)) is not None
    }
    unread = sorted(set(ordinals) - pages)
    if unread:
        raise SchemaRefusal(
            f"Recensor partition receipt v3 counts no unit on sealed page(s) {unread}; every "
            "sealed page is at least one unit"
        )
    stray = sorted(pages - set(ordinals))
    if stray:
        raise SchemaRefusal(
            f"Recensor partition receipt v3 counts units on page(s) {stray}, which name no "
            "sealed page reading"
        )


def _is_count(value: Any) -> bool:
    return is_plain_int(value) and value >= 0


def _partition_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in _PARTITION_KEYS}
    for item in items:
        counts[item["partition_class"]] += 1
    return counts


def _status(reasons: list[str]) -> str:
    return "complete" if not reasons else "partial"


def _validate_item(item: Any) -> None:
    required = {
        "act_id",
        "act_key",
        "review_ref",
        "review_outcome",
        "partition_class",
        "coverage",
        "page_disposition",
        "release_reason",
    }
    if not isinstance(item, dict) or set(item) != required:
        raise SchemaRefusal("Recensor partition receipt item has the wrong closed schema")
    if not isinstance(item["act_id"], str) or not item["act_id"]:
        raise SchemaRefusal("Recensor partition receipt item has no act identity")
    if not isinstance(item["act_key"], str) or not item["act_key"]:
        raise SchemaRefusal("Recensor partition receipt item has no act key")
    if not _READING_ACT_KEY.fullmatch(item["act_key"]):
        raise SchemaRefusal(
            f"Recensor partition receipt v3 item names act key {item['act_key']!r}, not "
            "p<page>:<n>, p<page>:unread or p<page>:blank"
        )
    if item["page_disposition"] not in PAGE_DISPOSITIONS:
        raise SchemaRefusal(
            f"Recensor partition receipt item names page_disposition "
            f"{item['page_disposition']!r}, not one of {sorted(PAGE_DISPOSITIONS)}"
        )
    try:
        expected_class = classify(RECENSOR, item["review_outcome"]).value
    except FatalAccounting as error:
        raise SchemaRefusal(
            "Recensor partition receipt item names an unknown Recensor outcome"
        ) from error
    if item["partition_class"] != expected_class:
        raise SchemaRefusal(
            "Recensor partition receipt item names partition_class "
            f"{item['partition_class']!r}, but review_outcome {item['review_outcome']!r} "
            f"derives {expected_class!r}"
        )
    _validate_reference(item["review_ref"], "review reference")
    _validate_release(item)
    _validate_coverage(item["coverage"])


def _validate_release(item: dict[str, Any]) -> None:
    """A held unit completed at review names why it was released; no other unit names one."""
    released = (
        item["page_disposition"] == "held"
        and item["partition_class"] == OutcomeClass.COMPLETED.value
    )
    reason = item["release_reason"]
    if released and not (isinstance(reason, str) and reason.strip()):
        raise SchemaRefusal(
            f"Recensor partition receipt v3 item {item['act_key']} was held by its page reading "
            f"and is {item['review_outcome']} at the Recensor, but names no reason its review "
            "released it"
        )
    if not released and reason is not None:
        raise SchemaRefusal(
            f"Recensor partition receipt v3 item {item['act_key']} names a release reason, but "
            "only a held unit completed at review is released"
        )


def _validate_coverage(coverage: Any) -> None:
    required = {
        "configured",
        "floor",
        "by_outcome",
        "by_class",
        "under_witnessed",
        "unresolved_chairs",
    }
    granularity_fields = {
        "page_granularity_only",
        "health_unrecorded",
        "shortfalls",
        "granularity_basis",
    }
    if not isinstance(coverage, dict):
        raise SchemaRefusal("Recensor partition receipt has malformed witness coverage")
    # Every witness of a page-read run reads the whole page, so no count is
    # judged at act granularity: health and shortfalls, never attachment.
    if set(coverage) & granularity_fields != {"health_unrecorded", "shortfalls"}:
        raise SchemaRefusal(
            "Recensor partition receipt v3 coverage carries exactly health_unrecorded and "
            "shortfalls beside its counts; a page-read run attaches no witness to an act"
        )
    if set(coverage) - (required | granularity_fields) or not required <= set(coverage):
        raise SchemaRefusal("Recensor partition receipt has malformed witness coverage")
    for field in ("configured", "floor", "unresolved_chairs"):
        value = coverage[field]
        if not _is_count(value):
            raise SchemaRefusal(
                f"Recensor partition receipt has invalid witness coverage counts: {field!r} "
                f"is {value!r}, not a non-negative integer"
            )
    by_outcome = coverage["by_outcome"]
    by_class = coverage["by_class"]
    if not isinstance(by_outcome, dict) or not all(
        isinstance(outcome, str) and outcome and _is_count(count)
        for outcome, count in by_outcome.items()
    ):
        raise SchemaRefusal(
            "Recensor partition receipt's by_outcome is not a mapping of non-empty witness "
            "outcome names to non-negative integer counts"
        )
    if not isinstance(by_class, dict) or set(by_class) != set(_PARTITION_KEYS):
        raise SchemaRefusal(
            "Recensor partition receipt's by_class does not name exactly the partition "
            f"classes {sorted(_PARTITION_KEYS)}"
        )
    if not all(_is_count(count) for count in by_class.values()):
        raise SchemaRefusal(
            f"Recensor partition receipt's by_class {by_class} holds a count that is not a "
            "non-negative integer"
        )
    if sum(by_outcome.values()) != coverage["configured"]:
        raise SchemaRefusal(
            f"Recensor partition receipt's by_outcome totals {sum(by_outcome.values())} "
            f"against {coverage['configured']} configured chair(s); every configured chair "
            "gets exactly one outcome"
        )
    if sum(by_class.values()) != coverage["configured"]:
        raise SchemaRefusal(
            f"Recensor partition receipt's by_class totals {sum(by_class.values())} against "
            f"{coverage['configured']} configured chair(s)"
        )
    if coverage["unresolved_chairs"] != by_class[OutcomeClass.UNRESOLVED.value]:
        raise SchemaRefusal(
            f"Recensor partition receipt names {coverage['unresolved_chairs']} unresolved "
            f"chair(s) while its own by_class counts "
            f"{by_class[OutcomeClass.UNRESOLVED.value]}"
        )
    if not isinstance(coverage["under_witnessed"], bool):
        raise SchemaRefusal(
            f"Recensor partition receipt's under_witnessed is {coverage['under_witnessed']!r}, "
            "not a boolean"
        )
    reading_chairs = sum(by_outcome.get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES)
    # Typed before `witnessed_count` subtracts it.
    shortfalls = coverage["shortfalls"]
    truncated = shortfalls.get("truncated") if isinstance(shortfalls, dict) else None
    if not _is_count(truncated) or truncated > reading_chairs:
        raise SchemaRefusal(
            "Recensor partition receipt v3 counts truncated readings that are not a count "
            "of chairs that read the page"
        )
    witnessed = witnessed_count(coverage)
    if coverage["under_witnessed"] != (witnessed < coverage["floor"]):
        raise SchemaRefusal(
            f"Recensor partition receipt claims under_witnessed="
            f"{coverage['under_witnessed']}, but {witnessed} page read(s) "
            f"against a floor of {coverage['floor']} says otherwise"
        )
    derived_by_class = {key: 0 for key in _PARTITION_KEYS}
    for outcome, count in by_outcome.items():
        try:
            derived_by_class[classify(ATTESTATORES, outcome).value] += count
        except FatalAccounting as error:
            raise SchemaRefusal(
                "Recensor partition receipt has an unknown witness outcome"
            ) from error
    if by_class != derived_by_class:
        raise SchemaRefusal(
            f"Recensor partition receipt's by_class {by_class} does not fall out of its own "
            f"per-outcome counts, which classify as {derived_by_class}"
        )
    health_unrecorded = coverage["health_unrecorded"]
    if not _is_count(health_unrecorded):
        raise SchemaRefusal("Recensor partition receipt has invalid health_unrecorded count")
    if set(shortfalls) != {"failed", "truncated", "unaligned"} or not all(
        _is_count(value) for value in shortfalls.values()
    ):
        raise SchemaRefusal("Recensor partition receipt has malformed shortfalls")
    configured = coverage["configured"]
    if health_unrecorded > configured or any(value > configured for value in shortfalls.values()):
        raise SchemaRefusal(
            "Recensor partition receipt counts more granularity facts than configured chairs"
        )
    if shortfalls["failed"] != by_outcome.get("failed", 0):
        raise SchemaRefusal(
            "Recensor partition receipt's failed shortfall does not derive from "
            "its own failed witness outcomes"
        )


def _validate_reference(reference: Any, what: str) -> None:
    digest_ref(reference, f"Recensor partition receipt {what}")


def _reasons(items: list[dict[str, Any]], *, links: list[dict[str, Any]] = ()) -> list[str]:
    # A page-read run counts units: act entries, `other` entries and page rows.
    counted = "unit"
    reasons: list[str] = []
    for item in items:
        act_id = item["act_id"]
        if (
            item["page_disposition"] == "held"
            and item["partition_class"] != OutcomeClass.COMPLETED.value
        ):
            reasons.append(f"unit {act_id} was held by its page reading and is not released")
        if item["partition_class"] != OutcomeClass.COMPLETED.value:
            reasons.append(f"{counted} {act_id} is {item['partition_class']} at the Recensor")
        coverage = item["coverage"]
        if coverage["under_witnessed"]:
            reasons.append(
                f"{counted} {act_id} is under-witnessed "
                f"({witnessed_count(coverage)} page reads of a floor of {coverage['floor']})"
            )
        if coverage["unresolved_chairs"]:
            reasons.append(
                f"{counted} {act_id} has {coverage['unresolved_chairs']} chair(s) with no "
                "outcome yet"
            )
    for link in links:
        if link["outcome"] != "accepted":
            reasons.append(
                f"{link['subject_id']} is {link['outcome']}: only one side says the text runs "
                "across the page break"
            )
    return reasons
