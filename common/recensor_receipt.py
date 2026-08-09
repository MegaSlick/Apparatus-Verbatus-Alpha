"""A self-hashed, scoped partition receipt for the Recensor boundary.

This is deliberately not the pipeline's final export verdict.  It proves only
the Designator proposal-act denominator and the configured-witness denominator
at the point the Recensor has reviewed them.  Page-level blank proof, residual
ink, and final Archetypus/Armarium categories require evidence this receipt does
not pretend to own.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import self_hash, verify_self_hash
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR

RECENSOR_PARTITION_RECEIPT_SCHEMA: Final = "recensor-partition-receipt.v1"
RECENSOR_PARTITION_RECEIPT_SCOPE: Final = "proposal-acts-and-configured-witnesses"
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
    by_partition = {key: 0 for key in _PARTITION_KEYS}
    for item in checked_items:
        by_partition[item["partition_class"]] += 1
    reasons = _reasons(checked_items)
    record: dict[str, Any] = {
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_PARTITION_RECEIPT_SCOPE,
        "proposal_seal_ref": proposal_seal_ref,
        "expected_act_count": len(checked_items),
        "items": checked_items,
        "by_partition_class": by_partition,
        "recensor_status": "complete" if not reasons else "partial",
        "reasons": reasons,
    }
    record["self_hash"] = self_hash(record)
    return validate_recensor_partition_receipt(record)


def validate_recensor_partition_receipt(record: Any) -> dict[str, Any]:
    """Validate the closed receipt schema and its derived summary."""

    required = {
        "schema",
        "run_id",
        "config_digest",
        "scope",
        "proposal_seal_ref",
        "expected_act_count",
        "items",
        "by_partition_class",
        "recensor_status",
        "reasons",
        "self_hash",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise SchemaRefusal("Recensor partition receipt has the wrong closed schema")
    if record["schema"] != RECENSOR_PARTITION_RECEIPT_SCHEMA or not verify_self_hash(record):
        raise SchemaRefusal("Recensor partition receipt has an invalid schema or self-hash")
    if (
        not isinstance(record["run_id"], str)
        or not record["run_id"]
        or not _is_sha256(record["config_digest"])
        or record["scope"] != RECENSOR_PARTITION_RECEIPT_SCOPE
        or not isinstance(record["expected_act_count"], int)
        or isinstance(record["expected_act_count"], bool)
        or record["expected_act_count"] < 0
        or not isinstance(record["items"], list)
        or record["expected_act_count"] != len(record["items"])
    ):
        raise SchemaRefusal("Recensor partition receipt has invalid run or denominator facts")
    _validate_reference(record["proposal_seal_ref"], "proposal-seal reference")
    previous_act_id = ""
    by_partition = {key: 0 for key in _PARTITION_KEYS}
    for item in record["items"]:
        _validate_item(item)
        act_id = item["act_id"]
        if not act_id or act_id <= previous_act_id:
            raise SchemaRefusal(
                "Recensor partition receipt items must be strictly sorted by unique act identity"
            )
        previous_act_id = act_id
        by_partition[item["partition_class"]] += 1
    if record["by_partition_class"] != by_partition:
        raise SchemaRefusal("Recensor partition receipt partition counts do not reconcile")
    reasons = _reasons(record["items"])
    if record["reasons"] != reasons or record["recensor_status"] != (
        "complete" if not reasons else "partial"
    ):
        raise SchemaRefusal("Recensor partition receipt status does not derive from its items")
    return record


def _validate_item(item: Any) -> None:
    required = {
        "act_id",
        "act_key",
        "designator_outcome",
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
    try:
        classify(DESIGNATOR, item["designator_outcome"])
        expected_class = classify(RECENSOR, item["review_outcome"]).value
    except FatalAccounting as error:
        raise SchemaRefusal(
            "Recensor partition receipt item names an unknown Designator or Recensor outcome"
        ) from error
    if item["partition_class"] != expected_class:
        raise SchemaRefusal(
            "Recensor partition receipt item duplicates a partition class that its review does not "
            "derive"
        )
    _validate_reference(item["review_ref"], "review reference")
    _validate_coverage(item["coverage"])


def _validate_coverage(coverage: Any) -> None:
    required = {
        "configured",
        "floor",
        "by_outcome",
        "by_class",
        "under_witnessed",
        "unresolved_chairs",
    }
    if not isinstance(coverage, dict) or set(coverage) != required:
        raise SchemaRefusal("Recensor partition receipt has malformed witness coverage")
    integers = ("configured", "floor", "unresolved_chairs")
    if any(
        not isinstance(coverage[field], int)
        or isinstance(coverage[field], bool)
        or coverage[field] < 0
        for field in integers
    ):
        raise SchemaRefusal("Recensor partition receipt has invalid witness coverage counts")
    by_outcome = coverage["by_outcome"]
    by_class = coverage["by_class"]
    derived_by_class = {key: 0 for key in _PARTITION_KEYS}
    if isinstance(by_outcome, dict):
        for outcome, count in by_outcome.items():
            if isinstance(outcome, str) and isinstance(count, int) and not isinstance(count, bool):
                try:
                    derived_by_class[classify(ATTESTATORES, outcome).value] += count
                except FatalAccounting as error:
                    raise SchemaRefusal(
                        "Recensor partition receipt has an unknown witness outcome"
                    ) from error
    if (
        not isinstance(by_outcome, dict)
        or not all(
            isinstance(outcome, str)
            and outcome
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for outcome, count in by_outcome.items()
        )
        or not isinstance(by_class, dict)
        or set(by_class) != set(_PARTITION_KEYS)
        or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in by_class.values()
        )
        or sum(by_outcome.values()) != coverage["configured"]
        or sum(by_class.values()) != coverage["configured"]
        or by_class != derived_by_class
        or coverage["unresolved_chairs"] != by_class[OutcomeClass.UNRESOLVED.value]
        or not isinstance(coverage["under_witnessed"], bool)
        or coverage["under_witnessed"]
        != (by_class[OutcomeClass.COMPLETED.value] < coverage["floor"])
    ):
        raise SchemaRefusal("Recensor partition receipt witness coverage does not reconcile")


def _validate_reference(reference: Any, what: str) -> None:
    if (
        not isinstance(reference, dict)
        or set(reference) != {"relative_path", "sha256"}
        or not isinstance(reference["relative_path"], str)
        or not reference["relative_path"]
        or reference["relative_path"].startswith("/")
        or ".." in reference["relative_path"].split("/")
        or not _is_sha256(reference["sha256"])
    ):
        raise SchemaRefusal(f"Recensor partition receipt has malformed {what}")


EMPTY_DENOMINATOR_REASON: Final = (
    "the Designator proposed no acts at all, so this receipt has no denominator to "
    "reconcile; a run that marked nothing out on its pages cannot be complete "
    "(GOALS 1: a missed act is worse than a poorly read one)"
)


def _reasons(items: list[dict[str, Any]]) -> list[str]:
    # An empty denominator is a fact about the run, not a malformed receipt. The
    # Designator proposing nothing at all is exactly the silent-failure shape this
    # pipeline exists to catch, and refusing to build the receipt would turn it
    # into a traceback at the one boundary whose job is to make it visible. The
    # Armarium's own aggregate already treats a page nobody marked out this way.
    if not items:
        return [EMPTY_DENOMINATOR_REASON]
    reasons: list[str] = []
    for item in items:
        act_id = item["act_id"]
        if item["partition_class"] != OutcomeClass.COMPLETED.value:
            reasons.append(f"act {act_id} is {item['partition_class']} at the Recensor")
        coverage = item["coverage"]
        if coverage["under_witnessed"]:
            reasons.append(
                f"act {act_id} is under-witnessed "
                f"({coverage['by_class']['completed']} of a floor of {coverage['floor']})"
            )
        if coverage["unresolved_chairs"]:
            reasons.append(
                f"act {act_id} has {coverage['unresolved_chairs']} chair(s) with no outcome yet"
            )
    return reasons


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
