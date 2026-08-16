"""Sealed definitions for the R7b bench cells.

Definitions are deliberately data, not a scorecard: a future pod execution may
fill observations only against the measures sealed here.  A missing execution is
an explicit ``not-run`` record, never a passing result.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.errors import SchemaRefusal

SCHEMA: Final = "bench-cell-definition.v1"
RESULT_SCHEMA: Final = "bench-cell-result.v1"
VERSION: Final = "r7b-v1"
REAL_CELLS: Final = frozenset({"B0", "B0.5"})


def _measure(name: str, numerator: str, denominator: str, unknown: str) -> dict[str, str]:
    return {"name": name, "numerator": numerator, "denominator": denominator, "unknown": unknown}


# The matrix is intentionally explicit about denominator and unknown treatment.
# No threshold appears here: the bench must report what happened before anyone
# decides whether a candidate merits a gate.
_MEASURES: Final = {
    "B0": [
        _measure(
            "correct_feeding_rate",
            "attempted calls whose retained request-view digest matches the sealed chair feeding recipe",
            "all attempted calls for that chair and corpus member",
            "missing request or retained-view evidence is a visible unknown and not a match",
        )
    ],
    "B0.5": [
        _measure(
            "model_load_seconds",
            "seconds from process start to ready receipt",
            "each chair start",
            "missing receipt is unknown",
        ),
        _measure(
            "prefill_tokens_per_second",
            "input tokens divided by prefill seconds",
            "calls with separately observed prefill timing",
            "unseparated timing is unknown",
        ),
        _measure(
            "decode_tokens_per_second",
            "output tokens divided by decode seconds",
            "calls with separately observed decode timing",
            "unseparated timing is unknown",
        ),
        _measure(
            "wall_clock_seconds_per_act",
            "elapsed wall seconds",
            "all attempted act reads",
            "failed or incomplete reads remain visible and are not omitted",
        ),
        _measure(
            "artifact_bytes_per_act",
            "published artifact bytes",
            "all attempted act reads",
            "unpublished or unreadable inventory is unknown",
        ),
        _measure(
            "cpu_detector_busy_gpu_utilization",
            "GPU utilization samples during CPU detector work",
            "all valid samples while that work is active",
            "no samples is unknown, never idle or saturated",
        ),
    ],
    "B2": [
        _measure(
            "witness_cer",
            "grapheme edit distance against adjudicated gold",
            "legible adjudicated graphemes",
            "ILLEGIBLE and unaligned material are counted separately, not scored as correct",
        ),
        _measure(
            "witness_wer",
            "word edit distance against adjudicated gold",
            "legible adjudicated words",
            "ILLEGIBLE and unaligned material are counted separately, not scored as correct",
        ),
        _measure(
            "visible_partial_rate",
            "failed, truncated, or unaligned witness records",
            "all expected witness/page records",
            "missing record is a visible shortfall",
        ),
    ],
    "B3": [
        _measure(
            "absolute_act_recall",
            "gold act regions reached by any resolved proposal",
            "all page-layout gold act regions",
            "unscorable geometry is unknown and remains in review-load",
        ),
        _measure(
            "topology_review_load",
            "proposals, ambiguities, occlusions, and uncovered regions requiring review",
            "all page-layout gold pages",
            "missing topology record is unknown",
        ),
    ],
    "B4": [
        _measure(
            "image_delta",
            "production-versus-image-absent error difference",
            "paired legible locked-acceptance acts",
            "unpaired or unknown output is reported outside the paired denominator",
        ),
        _measure(
            "witness_only_advantage",
            "primed/image-absent advantage over lectio nuda",
            "paired legible locked-acceptance acts",
            "unknown or missing condition is not imputed",
        ),
        _measure(
            "abstention_calibration",
            "correctly calibrated uncertain outcomes",
            "adjudicated uncertain/legible decisions",
            "unannotated uncertainty is unknown",
        ),
    ],
    "B5": [
        _measure(
            "reproof_delta",
            "correct spans fixed minus correct spans corrupted by Pass C",
            "adjudicated flagged spans",
            "unadjudicated or unchanged spans remain separately counted",
        ),
        _measure(
            "audit_kill_signal",
            "correct spans corrupted by audit",
            "adjudicated flagged spans",
            "unknown spans cannot satisfy a kill decision",
        ),
    ],
    "B5a": [
        _measure(
            "pass_a_to_b_change_rate",
            "graphemes changed from prior draft to production reading",
            "graphemes in paired Pass A and Pass B outputs",
            "missing prior or final is unknown",
        ),
        _measure(
            "anchor_ablation_delta",
            "error difference across prior/dossier and prompt-framing conditions",
            "paired legible locked-acceptance acts",
            "missing condition is not imputed",
        ),
    ],
    "B6": [
        _measure(
            "corrupt_witness_follow_rate",
            "corrupted witness spans copied into final output",
            "injected corrupt-witness spans",
            "unlocatable or unresolved injection is unknown",
        ),
        _measure(
            "corrupt_prior_follow_rate",
            "corrupted prior spans copied into final output",
            "injected corrupt-prior spans",
            "unlocatable or unresolved injection is unknown",
        ),
    ],
}


def definition(cell: str) -> dict[str, Any]:
    """Return one closed, self-hashed, versioned bench-cell definition."""
    if cell not in _MEASURES:
        raise SchemaRefusal(f"unknown R7b bench cell {cell!r}")
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "cell": cell,
        "measures": _MEASURES[cell],
        "execution_requirement": "authorized-pod-session"
        if cell in REAL_CELLS
        else "runner-and-gold-inputs",
    }
    record["definition_digest"] = digest_bytes(canonical_bytes(record))
    record["self_hash"] = self_hash(record)
    return record


def all_definitions() -> list[dict[str, Any]]:
    return [definition(cell) for cell in ("B0", "B0.5", "B2", "B3", "B4", "B5", "B5a", "B6")]


def validate_definition(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "version",
        "cell",
        "measures",
        "execution_requirement",
        "definition_digest",
        "self_hash",
    }:
        raise SchemaRefusal("bench-cell definition has the wrong closed schema")
    expected = definition(record["cell"])
    if record != expected or not verify_self_hash(record):
        raise SchemaRefusal("bench-cell definition does not match its sealed R7b measures")
    return record


def not_run(cell: str, *, fixture_verified: bool) -> dict[str, Any]:
    """Make a non-green result for an unexecuted real bench cell."""
    sealed = definition(cell)
    record: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "definition_digest": sealed["definition_digest"],
        "cell": cell,
        "state": "not-run",
        "fixture_verified": fixture_verified,
        "reason": "no authorized pod session and no real-model execution",
        "observations": [],
    }
    record["self_hash"] = self_hash(record)
    return record


def validate_result(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "definition_digest",
        "cell",
        "state",
        "fixture_verified",
        "reason",
        "observations",
        "self_hash",
    }:
        raise SchemaRefusal("bench-cell result has the wrong closed schema")
    if record["schema"] != RESULT_SCHEMA or record["state"] != "not-run":
        raise SchemaRefusal("R7b fixture runner may publish only the visible not-run state")
    if record["definition_digest"] != definition(record["cell"])["definition_digest"]:
        raise SchemaRefusal("bench-cell result is not bound to its frozen definition")
    if record["observations"] != [] or not isinstance(record["fixture_verified"], bool):
        raise SchemaRefusal(
            "a not-run bench result has no observations and a boolean fixture marker"
        )
    if not verify_self_hash(record):
        raise SchemaRefusal("bench-cell result fails its self-hash")
    return record
