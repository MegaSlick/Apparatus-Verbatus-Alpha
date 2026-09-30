"""A self-hashed, scoped partition receipt for the Recensor boundary.

This is deliberately not the pipeline's final export verdict.  It proves only
the act denominator and the configured-witness denominator at the point the
Recensor has reviewed them.  Page-level blank proof, residual ink, and final
Archetypus/Armarium categories require evidence this receipt does not pretend
to own.

v1 and v2 count the Designator's proposal acts (`proposal_seal_ref`). v3 counts
a page-read run's units (`common.stage.reading_acts`, the classes in
`COUNTED_READING_CLASSES`): it names every sealed page's `page-reading`
(`page_reading_refs`, in page order), and each item carries the unit's page
disposition instead of a Designator outcome. A unit its page reading held is
completed at the Recensor only with the reason its review released it
(`release_reason`), and a receipt holding any held unit is never `complete`.
"""

from __future__ import annotations

import re
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
# `p<page ordinal>:<entry n>`, or the page row of a page with no entry.
_READING_ACT_KEY: Final = re.compile(r"p[1-9][0-9]*:(?:[1-9][0-9]*|unread|blank)")
_COMMON_FIELDS: Final = frozenset(
    {
        "schema",
        "run_id",
        "config_digest",
        "scope",
        "expected_act_count",
        "items",
        "by_partition_class",
        "recensor_status",
        "reasons",
        "self_hash",
    }
)
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
) -> dict[str, Any]:
    """Build a v3 receipt over a page-read run's units, its summary derived from its items.

    `page_reading_refs` names every sealed page's `page-reading`, in page order;
    each item is one counted `reading_acts` row as the Recensor reviewed it:
    `{act_id, act_key, page_disposition, review_ref, review_outcome,
    partition_class, coverage, release_reason}`, `release_reason` being the
    reason the Recensor's review record gives for completing a unit its page
    reading held, and `None` otherwise.
    """

    checked_items = [dict(item) for item in items]
    for item in checked_items:
        _validate_item(item, schema=RECENSOR_PARTITION_RECEIPT_SCHEMA_V3)
    checked_items.sort(key=lambda item: item["act_id"])
    reasons = _reasons(checked_items, schema=RECENSOR_PARTITION_RECEIPT_SCHEMA_V3)
    record: dict[str, Any] = {
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA_V3,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_READING_RECEIPT_SCOPE,
        "page_reading_refs": [dict(reference) for reference in page_reading_refs],
        "expected_act_count": len(checked_items),
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
    denominator_field = (
        "page_reading_refs"
        if schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
        else "proposal_seal_ref"
    )
    if not isinstance(record, dict) or set(record) != _COMMON_FIELDS | {denominator_field}:
        raise SchemaRefusal("Recensor partition receipt has the wrong closed schema")
    if schema not in _SCOPE_BY_SCHEMA or not verify_self_hash(record):
        raise SchemaRefusal("Recensor partition receipt has an invalid schema or self-hash")
    if (
        not isinstance(record["run_id"], str)
        or not record["run_id"]
        or not is_sha256(record["config_digest"])
        or record["scope"] != _SCOPE_BY_SCHEMA[schema]
        or not _is_count(record["expected_act_count"])
        or not isinstance(record["items"], list)
        or record["expected_act_count"] != len(record["items"])
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
        if len(record["items"]) < len(references):
            raise SchemaRefusal(
                f"Recensor partition receipt v3 counts {len(record['items'])} unit(s) over "
                f"{len(references)} sealed page(s); every sealed page is at least one unit"
            )
        keys = [item.get("act_key") if isinstance(item, dict) else None for item in record["items"]]
        if len(set(map(str, keys))) != len(keys):
            raise SchemaRefusal("Recensor partition receipt v3 names one act key twice")
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
    reasons = _reasons(record["items"], schema=schema)
    if record["reasons"] != reasons or record["recensor_status"] != _status(reasons):
        raise SchemaRefusal("Recensor partition receipt status does not derive from its items")
    return record


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
    every witness reads the whole page: the reading outcomes. Otherwise (v1):
    the COMPLETED class.
    """
    reading_chairs = sum(
        coverage["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
    )
    if page_read:
        return reading_chairs
    if "page_granularity_only" in coverage:
        return reading_chairs - coverage["page_granularity_only"]
    return coverage["by_class"][OutcomeClass.COMPLETED.value]


def _validate_item(item: Any, *, schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2) -> None:
    reading = schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    required = {
        "act_id",
        "act_key",
        "review_ref",
        "review_outcome",
        "partition_class",
        "coverage",
    } | ({"page_disposition", "release_reason"} if reading else {"designator_outcome"})
    if not isinstance(item, dict) or set(item) != required:
        raise SchemaRefusal("Recensor partition receipt item has the wrong closed schema")
    if not isinstance(item["act_id"], str) or not item["act_id"]:
        raise SchemaRefusal("Recensor partition receipt item has no act identity")
    if not isinstance(item["act_key"], str) or not item["act_key"]:
        raise SchemaRefusal("Recensor partition receipt item has no act key")
    if reading and not _READING_ACT_KEY.fullmatch(item["act_key"]):
        raise SchemaRefusal(
            f"Recensor partition receipt v3 item names act key {item['act_key']!r}, not "
            "p<page>:<n>, p<page>:unread or p<page>:blank"
        )
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
    if reading:
        _validate_release(item)
    _validate_coverage(
        item["coverage"],
        schema=schema,
        require_complete_granularity=schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
    )


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


def _reasons(
    items: list[dict[str, Any]], *, schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2
) -> list[str]:
    # An empty denominator is a reason, not a malformed receipt: refusing would
    # hide the silent failure this boundary exists to show.
    page_read = schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    if not items:
        return [EMPTY_DENOMINATOR_REASON]
    reasons: list[str] = []
    for item in items:
        act_id = item["act_id"]
        if page_read and item["page_disposition"] == "held":
            released = item["release_reason"]
            reasons.append(
                f"act {act_id} was held by its page reading"
                + (f" and released at review: {released}" if released else "")
            )
        if item["partition_class"] != OutcomeClass.COMPLETED.value:
            reasons.append(f"act {act_id} is {item['partition_class']} at the Recensor")
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
                f"act {act_id} is under-witnessed "
                f"({witnessed} {measured} of a floor of {coverage['floor']})"
            )
        if coverage["unresolved_chairs"]:
            reasons.append(
                f"act {act_id} has {coverage['unresolved_chairs']} chair(s) with no outcome yet"
            )
    return reasons
