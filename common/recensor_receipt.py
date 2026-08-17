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
from common.contracts.errors import FatalAccounting, ReceiptVersionMismatch, SchemaRefusal
from common.contracts.outcomes import (
    INTERIM_GRANULARITY_BASIS,
    WITNESS_READING_OUTCOMES,
    OutcomeClass,
    classify,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR

RECENSOR_PARTITION_RECEIPT_SCHEMA: Final = "recensor-partition-receipt.v1"
RECENSOR_PARTITION_RECEIPT_SCHEMA_V2: Final = "recensor-partition-receipt.v2"
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
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
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
    if record["schema"] not in {
        RECENSOR_PARTITION_RECEIPT_SCHEMA,
        RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
    } or not verify_self_hash(record):
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
        _validate_item(item, schema=record["schema"])
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


def _validate_item(item: Any, *, schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2) -> None:
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
    # One fault, one message. These were a single `or` chain of about a dozen
    # independent checks all raising the same sentence, so a refusal on a real
    # run told an operator only that "some number in the witness coverage is
    # wrong" and left them to find which by reading this function and comparing
    # counts by hand. The receipt exists so that "complete" is a *refutable*
    # claim; a refusal that cannot name what it refused makes the refutation
    # harder than the claim. Each branch below now names its own disagreement
    # and quotes the numbers that disagree. Order is preserved from the old
    # chain, so a receipt that is malformed in several ways at once still
    # refuses on the same one it always did. Found by CodeRabbit.
    for field in ("configured", "floor", "unresolved_chairs"):
        value = coverage[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SchemaRefusal(
                f"Recensor partition receipt has invalid witness coverage counts: {field!r} "
                f"is {value!r}, not a non-negative integer"
            )
    by_outcome = coverage["by_outcome"]
    by_class = coverage["by_class"]
    if not isinstance(by_outcome, dict) or not all(
        isinstance(outcome, str)
        and outcome
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
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
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in by_class.values()
    ):
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
    # Typed before it is subtracted, not after. This count is the one granularity
    # fact the under_witnessed rederivation below depends on, and the v2 type
    # checks that were its only guard run further down: a record carrying
    # `"page_granularity_only": "1"` reached the subtraction first and left this
    # validator through a raw TypeError, where every other malformed field in
    # this file is a named refusal a caller can catch as a ContractError. Found
    # in audit; F-O4.
    if not isinstance(page_only, int) or isinstance(page_only, bool) or page_only < 0:
        raise SchemaRefusal("Recensor partition receipt has invalid page_granularity_only count")
    # Rederived from the reading outcomes, not from the COMPLETED class, because
    # that class is wider: it also holds `excluded`, an approval-bound exclusion
    # that never looked at the ink (`blank_corroboration` in the Recensor names
    # the same trap -- "trusting it here would let an excluded chair stand in for
    # a witness that never looked"). `witness_coverage` counts only chairs whose
    # outcome IS a reading, so an act with one excluded chair produced a coverage
    # record this rederivation then refused as self-contradictory: writer and
    # validator disagreed, and the Recensor validates every item it builds, so
    # the receipt could not be written at all for such an act. Reading outcomes
    # minus page-granularity-only contributions is exactly what the writer
    # counted. Found in audit; F-O3.
    reading_chairs = sum(by_outcome.get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES)
    # `reading_chairs - page_only` must reproduce witness_coverage's own
    # `len(attached_chairs)` exactly. The two definitions are one contract:
    # a change to what `page_granularity_only` counts must land in both
    # files in the same commit, or this receipt refuses its own writer.
    act_completed = reading_chairs - page_only
    if page_only > reading_chairs:
        raise SchemaRefusal(
            "Recensor partition receipt has more page-only contributions than chairs that read"
        )
    # Standalone schema callers may be validating one newly introduced field at a
    # time (page_granularity_only/health_unrecorded/shortfalls need not all land
    # together in one unit-level call), so a record naming none, some, or all of
    # them is not itself malformed. But the rederivation below must not become
    # skippable by naming only a *subset* that omits `page_granularity_only`: that
    # was this check's own gate before this fix (`has_complete_granularity`
    # required all three), which let a coverage record supply an unrelated
    # granularity field (or none) while asserting an arbitrary `under_witnessed`
    # for an actually-under-witnessed act -- `health_unrecorded` and `shortfalls`
    # play no part in this derivation, so gating on their presence too was never
    # load-bearing for it, only an accidental door. `page_granularity_only` is the
    # one field this derivation actually depends on, and its presence alone now
    # decides which formula applies; the check itself always runs. Found in
    # audit (S4, "can a dishonest record thread the needle" -- yes); F-S4.
    has_page_only_fact = "page_granularity_only" in coverage
    expected_under_witnessed = (
        act_completed < coverage["floor"]
        if has_page_only_fact
        else by_class[OutcomeClass.COMPLETED.value] < coverage["floor"]
    )
    if coverage["under_witnessed"] != expected_under_witnessed:
        raise SchemaRefusal(
            f"Recensor partition receipt claims under_witnessed="
            f"{coverage['under_witnessed']}, but {act_completed} act-level completed "
            f"read(s) against a floor of {coverage['floor']} says otherwise"
        )
    # Rederived from the outcome counts rather than compared field by field: the
    # per-class summary is the receipt's own arithmetic, and a receipt whose
    # summary does not fall out of its own numbers is not evidence of anything.
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
    if schema == RECENSOR_PARTITION_RECEIPT_SCHEMA_V2:
        # The unit validator remains permissive for partial records so a caller
        # can record one new fact at a time; writers always emit all three.
        # `page_granularity_only` is typed above rather than here, because the
        # under_witnessed rederivation subtracts it before this block runs.
        health_unrecorded = coverage.get("health_unrecorded", 0)
        shortfalls = coverage.get("shortfalls", {"failed": 0, "truncated": 0, "unaligned": 0})
        if (
            not isinstance(health_unrecorded, int)
            or isinstance(health_unrecorded, bool)
            or health_unrecorded < 0
        ):
            raise SchemaRefusal("Recensor partition receipt has invalid health_unrecorded count")
        if (
            not isinstance(shortfalls, dict)
            or set(shortfalls) != {"failed", "truncated", "unaligned"}
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in shortfalls.values()
            )
        ):
            raise SchemaRefusal("Recensor partition receipt has malformed shortfalls")
        # Every granularity count describes configured chairs, so none may exceed
        # the configured count — a shortfall tally larger than the roster is not a
        # measurement (CodeRabbit chain-end review; host disposition: fixed).
        configured = coverage["configured"]
        if health_unrecorded > configured or any(
            value > configured for value in shortfalls.values()
        ):
            raise SchemaRefusal(
                "Recensor partition receipt counts more granularity facts than configured chairs"
            )
        if "granularity_basis" in coverage and (
            coverage["granularity_basis"] != INTERIM_GRANULARITY_BASIS
        ):
            raise SchemaRefusal(
                "Recensor partition receipt v2 does not name R0's honest interim "
                "granularity measurement basis"
            )
        if shortfalls["failed"] != by_outcome.get("failed", 0):
            raise SchemaRefusal(
                "Recensor partition receipt's failed shortfall does not derive from "
                "its own failed witness outcomes"
            )


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
            if "page_granularity_only" in coverage:
                reading_chairs = sum(
                    coverage["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
                )
                counted = reading_chairs - coverage["page_granularity_only"]
            else:
                # A v1 receipt derived its flag from the completed class, so the
                # reason must quote that same number or it argues with the flag.
                counted = coverage["by_class"][OutcomeClass.COMPLETED.value]
            reasons.append(
                f"act {act_id} is under-witnessed "
                f"({counted} act-level reads of a floor of {coverage['floor']})"
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
