"""A self-hashed, scoped partition receipt for the Recensor boundary.

This is deliberately not the pipeline's final export verdict.  It proves only
the act denominator and the configured-witness denominator at the point the
Recensor has reviewed them.  Page-level blank proof, residual ink, and final
Archetypus/Armarium categories require evidence this receipt does not pretend
to own.

v1 and v2 count the Designator's proposal acts (`proposal_seal_ref`,
`expected_act_count`). v3 counts a page-read run's units
(`common.stage.reading_acts`, `expected_unit_count`): it names every sealed
page's `page-reading` (`page_reading_refs`, in page order), each item carries
the unit's page disposition instead of a Designator outcome, and
`continuation_links` names every page break an answer flags, so a break only
one side says the text runs across keeps the run partial.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import is_plain_int, is_sha256, self_hash, verify_self_hash
from common.contracts.envelope import digest_ref
from common.contracts.errors import FatalAccounting, ReceiptVersionMismatch, SchemaRefusal
from common.contracts.outcomes import (
    INTERIM_GRANULARITY_BASIS,
    NATIVE_GRANULARITY_BASIS,
    WITNESS_READING_OUTCOMES,
    OutcomeClass,
    classify,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR

RECENSOR_PARTITION_RECEIPT_SCHEMA: Final = "recensor-partition-receipt.v1"
RECENSOR_PARTITION_RECEIPT_SCHEMA_V2: Final = "recensor-partition-receipt.v2"
RECENSOR_PARTITION_RECEIPT_SCOPE: Final = "proposal-acts-and-configured-witnesses"
RECENSOR_PARTITION_RECEIPT_SCHEMA_V3: Final = "recensor-partition-receipt.v3"
RECENSOR_READING_RECEIPT_SCOPE: Final = "reading-acts-and-configured-witnesses"
PAGE_DISPOSITIONS: Final = frozenset({"read", "held"})
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
_ACT_FIELDS: Final = frozenset({"proposal_seal_ref", "expected_act_count"})
_READING_FIELDS: Final = frozenset(
    {"page_reading_refs", "expected_unit_count", "continuation_links"}
)
CONTINUATION_LINK_OUTCOMES: Final = frozenset({"accepted", "held-for-review"})
_LINK_FIELDS: Final = frozenset({"subject_id", "link_ref", "outcome"})
_SCOPE_BY_SCHEMA: Final = {
    RECENSOR_PARTITION_RECEIPT_SCHEMA: RECENSOR_PARTITION_RECEIPT_SCOPE,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V2: RECENSOR_PARTITION_RECEIPT_SCOPE,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V3: RECENSOR_READING_RECEIPT_SCOPE,
}
_PARTITION_KEYS: Final = tuple(klass.value for klass in OutcomeClass)


def build_recensor_partition_receipt(
    *,
    run_id: str,
    config_digest: str,
    proposal_seal_ref: dict[str, str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a receipt whose summary is mechanically derived from its items."""

    checked_items = [dict(item) for item in items]
    for item in checked_items:
        _validate_item(item)
    checked_items.sort(key=lambda item: item["act_id"])
    reasons = _reasons(checked_items)
    record: dict[str, Any] = {
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_PARTITION_RECEIPT_SCOPE,
        "proposal_seal_ref": proposal_seal_ref,
        "expected_act_count": len(checked_items),
        "items": checked_items,
        "by_partition_class": _partition_counts(checked_items),
        "recensor_status": _status(reasons),
        "reasons": reasons,
    }
    record["self_hash"] = self_hash(record)
    return validate_recensor_partition_receipt(record)


def build_recensor_reading_receipt(
    *,
    run_id: str,
    config_digest: str,
    page_reading_refs: list[dict[str, str]],
    items: list[dict[str, Any]],
    continuation_links: list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a v3 receipt over a page-read run's units, its summary derived from its items.

    `page_reading_refs` names every sealed page's `page-reading`, in page order;
    each item is one `reading_acts` row as the Recensor reviewed it; each
    continuation link is `{subject_id, link_ref, outcome}` for one flagged page
    break.
    """

    checked_items = [dict(item) for item in items]
    for item in checked_items:
        _validate_item(item, schema=RECENSOR_PARTITION_RECEIPT_SCHEMA_V3)
    checked_items.sort(key=lambda item: item["act_id"])
    links = sorted((dict(link) for link in continuation_links), key=_link_order)
    for link in links:
        _validate_link(link)
    reasons = _reasons(checked_items, schema=RECENSOR_PARTITION_RECEIPT_SCHEMA_V3, links=links)
    record: dict[str, Any] = {
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA_V3,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_READING_RECEIPT_SCOPE,
        "page_reading_refs": [dict(reference) for reference in page_reading_refs],
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
    reading = schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    denominator_fields = _READING_FIELDS if reading else _ACT_FIELDS
    if not isinstance(record, dict) or set(record) != _COMMON_FIELDS | denominator_fields:
        raise SchemaRefusal("Recensor partition receipt has the wrong closed schema")
    if schema not in _SCOPE_BY_SCHEMA or not verify_self_hash(record):
        raise SchemaRefusal("Recensor partition receipt has an invalid schema or self-hash")
    count = expected_count(record)
    if (
        not isinstance(record["run_id"], str)
        or not record["run_id"]
        or not is_sha256(record["config_digest"])
        or record["scope"] != _SCOPE_BY_SCHEMA[schema]
        or not _is_count(count)
        or not isinstance(record["items"], list)
        or count != len(record["items"])
    ):
        raise SchemaRefusal("Recensor partition receipt has invalid run or denominator facts")
    if schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3:
        references = record["page_reading_refs"]
        if not isinstance(references, list) or not references:
            raise SchemaRefusal(
                "Recensor partition receipt v3 names no page reading; every sealed page's "
                "reading is part of its denominator"
            )
        for reference in references:
            _validate_reference(reference, "page-reading reference")
        paths = [reference["relative_path"] for reference in references]
        if len(set(paths)) != len(paths):
            raise SchemaRefusal("Recensor partition receipt v3 names one page reading twice")
        links = record["continuation_links"]
        if not isinstance(links, list):
            raise SchemaRefusal("Recensor partition receipt v3 continuation_links is not a list")
        for link in links:
            _validate_link(link)
        subjects = [link["subject_id"] for link in links]
        if subjects != sorted(
            set(subjects), key=lambda subject: _link_order({"subject_id": subject})
        ):
            raise SchemaRefusal(
                "Recensor partition receipt v3 continuation links are not one per page break, "
                "in page order"
            )
    else:
        _validate_reference(record["proposal_seal_ref"], "proposal-seal reference")
    previous_act_id = ""
    for item in record["items"]:
        _validate_item(item, schema=record["schema"])
        act_id = item["act_id"]
        if not act_id or act_id <= previous_act_id:
            raise SchemaRefusal(
                "Recensor partition receipt items must be strictly sorted by unique act identity"
            )
        previous_act_id = act_id
    if record["by_partition_class"] != _partition_counts(record["items"]):
        raise SchemaRefusal("Recensor partition receipt partition counts do not reconcile")
    reasons = _reasons(record["items"], schema=schema, links=record.get("continuation_links", []))
    if record["reasons"] != reasons or record["recensor_status"] != _status(reasons):
        raise SchemaRefusal("Recensor partition receipt status does not derive from its items")
    return record


def expected_count(record: dict[str, Any]) -> Any:
    """The receipt's sealed denominator count: acts for v1 and v2, units for v3."""
    if record.get("schema") == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3:
        return record["expected_unit_count"]
    return record["expected_act_count"]


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


def _is_count(value: Any) -> bool:
    return is_plain_int(value) and value >= 0


def _partition_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in _PARTITION_KEYS}
    for item in items:
        counts[item["partition_class"]] += 1
    return counts


def _status(reasons: list[str]) -> str:
    return "complete" if not reasons else "partial"


def _witnessed_count(coverage: dict[str, Any], *, page_read: bool = False) -> int:
    """The count an act's `under_witnessed` flag is judged from.

    With `page_granularity_only`: reading outcomes less page-only contributions,
    which must reproduce `witness_coverage`'s own count exactly. Reading
    outcomes, not the COMPLETED class, because that class also holds approval
    exclusions that never looked at the ink. On a page-read run (v3), where
    every witness reads the whole page: the reading outcomes less the
    truncated ones. Otherwise (v1): the COMPLETED class.
    """
    reading_chairs = sum(
        coverage["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
    )
    if page_read:
        return reading_chairs - coverage["shortfalls"]["truncated"]
    if "page_granularity_only" in coverage:
        return reading_chairs - coverage["page_granularity_only"]
    return coverage["by_class"][OutcomeClass.COMPLETED.value]


def _validate_item(item: Any, *, schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2) -> None:
    reading = schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    required = {
        "act_id",
        "act_key",
        "page_disposition" if reading else "designator_outcome",
        "review_ref",
        "review_outcome",
        "partition_class",
        "coverage",
    }
    if not isinstance(item, dict) or set(item) != required:
        raise SchemaRefusal("Recensor partition receipt item has the wrong closed schema")
    if not isinstance(item["act_id"], str) or not item["act_id"]:
        raise SchemaRefusal("Recensor partition receipt item has no act identity")
    if not isinstance(item["act_key"], str) or not item["act_key"]:
        raise SchemaRefusal("Recensor partition receipt item has no act key")
    if reading and item["page_disposition"] not in PAGE_DISPOSITIONS:
        raise SchemaRefusal(
            f"Recensor partition receipt item names page_disposition "
            f"{item['page_disposition']!r}, not one of {sorted(PAGE_DISPOSITIONS)}"
        )
    try:
        if not reading:
            classify(DESIGNATOR, item["designator_outcome"])
        expected_class = classify(RECENSOR, item["review_outcome"]).value
    except FatalAccounting as error:
        raise SchemaRefusal(
            "Recensor partition receipt item names an unknown Designator or Recensor outcome"
        ) from error
    if item["partition_class"] != expected_class:
        raise SchemaRefusal(
            "Recensor partition receipt item names partition_class "
            f"{item['partition_class']!r}, but review_outcome {item['review_outcome']!r} "
            f"derives {expected_class!r}"
        )
    _validate_reference(item["review_ref"], "review reference")
    _validate_coverage(
        item["coverage"],
        schema=schema,
        require_complete_granularity=schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
    )


def _validate_coverage(
    coverage: Any,
    *,
    schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
    require_complete_granularity: bool = False,
) -> None:
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
    present_granularity = set(coverage) & granularity_fields
    if schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3:
        # Every witness of a page-read run reads the whole page, so no count is
        # judged at act granularity: health and shortfalls, never attachment.
        if present_granularity != {"health_unrecorded", "shortfalls"}:
            raise SchemaRefusal(
                "Recensor partition receipt v3 coverage carries exactly health_unrecorded and "
                "shortfalls beside its counts; a page-read run attaches no witness to an act"
            )
    if schema == RECENSOR_PARTITION_RECEIPT_SCHEMA and present_granularity:
        raise ReceiptVersionMismatch(
            "receipt schema v1 cannot carry page-granularity coverage facts; use receipt version v2"
        )
    allowed = required | granularity_fields
    if set(coverage) - allowed or not required <= set(coverage):
        raise SchemaRefusal("Recensor partition receipt has malformed witness coverage")
    if require_complete_granularity and not granularity_fields <= set(coverage):
        raise SchemaRefusal(
            "Recensor partition receipt v2 omits one or more required granularity facts"
        )
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
    page_only = coverage.get("page_granularity_only", 0)
    # Typed before `_witnessed_count` subtracts it.
    if not _is_count(page_only):
        raise SchemaRefusal("Recensor partition receipt has invalid page_granularity_only count")
    reading_chairs = sum(by_outcome.get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES)
    if page_only > reading_chairs:
        raise SchemaRefusal(
            "Recensor partition receipt has more page-only contributions than chairs that read"
        )
    # Only `page_granularity_only` decides the formula; the check always runs.
    page_read = schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    if page_read:
        # Typed before `_witnessed_count` subtracts it.
        shortfalls = coverage["shortfalls"]
        truncated = shortfalls.get("truncated") if isinstance(shortfalls, dict) else None
        if not _is_count(truncated) or truncated > reading_chairs:
            raise SchemaRefusal(
                "Recensor partition receipt v3 counts truncated readings that are not a count "
                "of chairs that read the page"
            )
    witnessed = _witnessed_count(coverage, page_read=page_read)
    if coverage["under_witnessed"] != (witnessed < coverage["floor"]):
        compared_label = (
            "page read(s)"
            if page_read
            else "act-level completed read(s)"
            if "page_granularity_only" in coverage
            else "completed chair(s)"
        )
        raise SchemaRefusal(
            f"Recensor partition receipt claims under_witnessed="
            f"{coverage['under_witnessed']}, but {witnessed} {compared_label} "
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
    if schema in (RECENSOR_PARTITION_RECEIPT_SCHEMA_V2, RECENSOR_PARTITION_RECEIPT_SCHEMA_V3):
        # Permissive for partial v2 records; writers always emit all three.
        health_unrecorded = coverage.get("health_unrecorded", 0)
        shortfalls = coverage.get("shortfalls", {"failed": 0, "truncated": 0, "unaligned": 0})
        if not _is_count(health_unrecorded):
            raise SchemaRefusal("Recensor partition receipt has invalid health_unrecorded count")
        if (
            not isinstance(shortfalls, dict)
            or set(shortfalls) != {"failed", "truncated", "unaligned"}
            or not all(_is_count(value) for value in shortfalls.values())
        ):
            raise SchemaRefusal("Recensor partition receipt has malformed shortfalls")
        configured = coverage["configured"]
        if health_unrecorded > configured or any(
            value > configured for value in shortfalls.values()
        ):
            raise SchemaRefusal(
                "Recensor partition receipt counts more granularity facts than configured chairs"
            )
        if "granularity_basis" in coverage and coverage["granularity_basis"] not in {
            INTERIM_GRANULARITY_BASIS,
            NATIVE_GRANULARITY_BASIS,
        }:
            raise SchemaRefusal(
                "Recensor partition receipt v2 does not name an honest granularity measurement basis"
            )
        if shortfalls["failed"] != by_outcome.get("failed", 0):
            raise SchemaRefusal(
                "Recensor partition receipt's failed shortfall does not derive from "
                "its own failed witness outcomes"
            )


def _validate_reference(reference: Any, what: str) -> None:
    digest_ref(reference, f"Recensor partition receipt {what}")


EMPTY_DENOMINATOR_REASON: Final = (
    "the Designator proposed no acts at all, so this receipt has no denominator to "
    "reconcile; a run that marked nothing out on its pages cannot be complete "
    "(goal 2: a missed act is worse than a poorly read one)"
)
EMPTY_READING_DENOMINATOR_REASON: Final = (
    "the receipt counts no unit at all, so it has no denominator to reconcile; every "
    "sealed page of a page-read run is at least one unit, so an empty count is a lost page"
)


def _reasons(
    items: list[dict[str, Any]],
    *,
    schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
    links: list[dict[str, Any]] = (),
) -> list[str]:
    # An empty denominator is a reason, not a malformed receipt: refusing would
    # hide the silent failure this boundary exists to show.
    page_read = schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    if not items:
        return [EMPTY_READING_DENOMINATOR_REASON if page_read else EMPTY_DENOMINATOR_REASON]
    # A page-read run counts units: act entries, `other` entries and page rows.
    counted = "unit" if page_read else "act"
    reasons: list[str] = []
    for item in items:
        act_id = item["act_id"]
        if item["partition_class"] != OutcomeClass.COMPLETED.value:
            reasons.append(f"{counted} {act_id} is {item['partition_class']} at the Recensor")
        coverage = item["coverage"]
        if coverage["under_witnessed"]:
            measured = (
                "page reads"
                if page_read
                else "act-level reads"
                if "page_granularity_only" in coverage
                else "completed chairs"
            )
            witnessed = _witnessed_count(coverage, page_read=page_read)
            reasons.append(
                f"{counted} {act_id} is under-witnessed "
                f"({witnessed} {measured} of a floor of {coverage['floor']})"
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
