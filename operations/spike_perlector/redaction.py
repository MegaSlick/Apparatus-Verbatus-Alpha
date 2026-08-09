"""Closed public-safe projection for dated reading-claim findings.

The public projection aggregates private evidence before it returns a JSON-like
record.  It has no path for an act ID, image reference, model identity, prompt,
candidate/witness name, or transcription to enter ``history/``.  The accompanying
validator is intentionally stricter than generic JSON Schema validation: values
which could carry prose are closed enumerations, not arbitrary strings.
"""

from __future__ import annotations

import math
from typing import Any

from .encoding import is_sha256
from .errors import PublicSafetyRefusal
from .models import Condition
from .runner import (
    AggregateMetrics,
    CandidateConditionDeltas,
    ConditionAggregate,
    MeasurementRun,
    WitnessAggregate,
)

SCHEMA = "reading-claim-public-finding.v1"
MEASURE_QUOTES = (
    "reference_length_cer_wer_v1",
    "missing_or_refused_is_empty_hypothesis_v1",
    "condition_matrix_fully_accounted_v1",
    "dissent_is_structural_not_quality_v1",
    "cost_and_wall_time_per_candidate_act_v1",
)

_ROOT_KEYS = {
    "schema",
    "protocol_sha256",
    "normalization_profile_id",
    "normalization_profile_sha256",
    "measure_quotes",
    "matrix",
    "input_baselines",
    "condition_deltas",
}
_MATRIX_KEYS = {
    "subject_index",
    "condition",
    "cell_count",
    "cer_errors",
    "cer_reference_units",
    "cer_matches",
    "cer",
    "wer_errors",
    "wer_reference_units",
    "wer_matches",
    "wer",
    "completeness",
    "complete_cells",
    "truncated_cells",
    "no_readable_text_cells",
    "refused_cells",
    "missing_cells",
    "unavailable_cells",
    "malformed_cells",
    "dissent_compared",
    "dissent_departed",
    "dissent_unavailable",
    "dissent_rate",
    "elapsed_observed_cells",
    "mean_elapsed_ms",
    "cost_observed_cells",
    "mean_cost_usd",
}
_BASELINE_KEYS = _MATRIX_KEYS - {"subject_index", "condition"} | {"source_index"}
_DELTA_KEYS = {
    "subject_index",
    "priming_cer_delta",
    "priming_wer_delta",
    "image_cer_delta",
    "image_wer_delta",
}


def _metrics_record(metrics: AggregateMetrics) -> dict[str, int | float | None]:
    return {
        "cell_count": metrics.cell_count,
        "cer_errors": metrics.cer_errors,
        "cer_reference_units": metrics.cer_reference_units,
        "cer_matches": metrics.cer_matches,
        "cer": metrics.cer,
        "wer_errors": metrics.wer_errors,
        "wer_reference_units": metrics.wer_reference_units,
        "wer_matches": metrics.wer_matches,
        "wer": metrics.wer,
        "completeness": metrics.completeness,
        "complete_cells": metrics.complete_count,
        "truncated_cells": metrics.truncated_count,
        "no_readable_text_cells": metrics.no_readable_text_count,
        "refused_cells": metrics.refused_count,
        "missing_cells": metrics.missing_count,
        "unavailable_cells": metrics.unavailable_count,
        "malformed_cells": metrics.malformed_count,
        "dissent_compared": metrics.dissent_compared,
        "dissent_departed": metrics.dissent_departed,
        "dissent_unavailable": metrics.dissent_unavailable,
        "dissent_rate": metrics.dissent_rate,
        "elapsed_observed_cells": metrics.elapsed_observed_count,
        "mean_elapsed_ms": metrics.mean_elapsed_ms,
        "cost_observed_cells": metrics.cost_observed_count,
        "mean_cost_usd": metrics.mean_cost_usd,
    }


def _condition_record(row: ConditionAggregate) -> dict[str, Any]:
    return {
        "subject_index": row.public_slot,
        "condition": row.condition.value,
        **_metrics_record(row.metrics),
    }


def _baseline_record(row: WitnessAggregate) -> dict[str, Any]:
    record = _metrics_record(row.metrics)
    return {"source_index": row.public_source_index, **record}


def _delta_record(row: CandidateConditionDeltas) -> dict[str, Any]:
    return {
        "subject_index": row.public_slot,
        "priming_cer_delta": row.priming_cer_delta,
        "priming_wer_delta": row.priming_wer_delta,
        "image_cer_delta": row.image_cer_delta,
        "image_wer_delta": row.image_wer_delta,
    }


def project_public_finding(run: MeasurementRun) -> dict[str, Any]:
    """Aggregate a complete private run into the only history-safe record shape."""

    try:
        run.require_publishable()
    except Exception as error:
        raise PublicSafetyRefusal(f"run is not eligible for public evidence: {error}") from error
    if run.manifest is None:  # pragma: no cover - protected by require_publishable
        raise PublicSafetyRefusal("public finding has no sealed manifest")
    finding: dict[str, Any] = {
        "schema": SCHEMA,
        "protocol_sha256": run.manifest.protocol_sha256,
        "normalization_profile_id": run.profile.profile_id,
        "normalization_profile_sha256": run.profile.digest,
        "measure_quotes": list(MEASURE_QUOTES),
        "matrix": [_condition_record(row) for row in run.condition_aggregates()],
        "input_baselines": [_baseline_record(row) for row in run.witness_aggregates()],
        "condition_deltas": [_delta_record(row) for row in run.condition_deltas()],
    }
    validate_public_finding(finding)
    return finding


def _require_exact_keys(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != allowed:
        found = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise PublicSafetyRefusal(f"public {label} has keys {found!r}, not its closed schema")
    return value


def _require_nonnegative_int(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PublicSafetyRefusal(f"public {label} must be a non-negative integer")


def _require_rate(value: Any, label: str, *, nullable: bool = False, signed: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise PublicSafetyRefusal(f"public {label} must be a finite number")
    if not signed and value < 0:
        raise PublicSafetyRefusal(f"public {label} may not be negative")


def _validate_metric_fields(record: dict[str, Any], *, baseline: bool) -> None:
    integer_fields = {
        "cell_count",
        "cer_errors",
        "cer_reference_units",
        "cer_matches",
        "wer_errors",
        "wer_reference_units",
        "wer_matches",
        "complete_cells",
        "truncated_cells",
        "no_readable_text_cells",
        "refused_cells",
        "missing_cells",
        "unavailable_cells",
        "malformed_cells",
        "dissent_compared",
        "dissent_departed",
        "dissent_unavailable",
        "elapsed_observed_cells",
        "cost_observed_cells",
    }
    for field in integer_fields:
        _require_nonnegative_int(record[field], field)
    if record["cer_reference_units"] <= 0 or record["wer_reference_units"] <= 0:
        raise PublicSafetyRefusal("public metric has no checked CER/WER denominator")
    if record["dissent_departed"] > record["dissent_compared"]:
        raise PublicSafetyRefusal("public dissent departures exceed comparisons")
    if (
        record["complete_cells"]
        + record["truncated_cells"]
        + record["no_readable_text_cells"]
        + record["refused_cells"]
        + record["missing_cells"]
        + record["unavailable_cells"]
        + record["malformed_cells"]
        != record["cell_count"]
    ):
        raise PublicSafetyRefusal("public response states do not cover planned cells")
    for field in ("cer", "wer", "completeness"):
        _require_rate(record[field], field)
    if not math.isclose(
        record["cer"],
        record["cer_errors"] / record["cer_reference_units"],
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise PublicSafetyRefusal(
            "public CER does not equal its retained numerator and denominator"
        )
    if not math.isclose(
        record["wer"],
        record["wer_errors"] / record["wer_reference_units"],
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise PublicSafetyRefusal(
            "public WER does not equal its retained numerator and denominator"
        )
    if (
        record["cer_matches"] > record["cer_reference_units"]
        or record["wer_matches"] > record["wer_reference_units"]
    ):
        raise PublicSafetyRefusal("public match count exceeds its checked denominator")
    if not math.isclose(
        record["completeness"],
        record["cer_matches"] / record["cer_reference_units"],
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise PublicSafetyRefusal("public completeness does not equal its retained match count")
    _require_rate(record["dissent_rate"], "dissent_rate", nullable=True)
    _require_rate(record["mean_elapsed_ms"], "mean_elapsed_ms", nullable=True)
    _require_rate(record["mean_cost_usd"], "mean_cost_usd", nullable=True)
    if (record["dissent_rate"] is None) != (record["dissent_compared"] == 0):
        raise PublicSafetyRefusal(
            "public dissent rate nullness does not match its comparison count"
        )
    if (record["mean_elapsed_ms"] is None) != (record["elapsed_observed_cells"] == 0):
        raise PublicSafetyRefusal(
            "public elapsed mean nullness does not match its observation count"
        )
    if (record["mean_cost_usd"] is None) != (record["cost_observed_cells"] == 0):
        raise PublicSafetyRefusal("public cost mean nullness does not match its observation count")
    if not baseline and (
        record["elapsed_observed_cells"] != record["cell_count"]
        or record["cost_observed_cells"] != record["cell_count"]
    ):
        raise PublicSafetyRefusal(
            "public candidate row does not measure wall time and cost for every act"
        )
    index = record["source_index"] if baseline else record["subject_index"]
    _require_nonnegative_int(index, "source_index" if baseline else "subject_index")
    if index < 1:
        raise PublicSafetyRefusal("public slots must be positive non-identifying integers")


def validate_public_finding(finding: Any) -> None:
    """Reject data that is structurally or lexically capable of carrying raw evidence."""

    root = _require_exact_keys(finding, _ROOT_KEYS, "finding")
    if root["schema"] != SCHEMA:
        raise PublicSafetyRefusal("public finding has an unknown schema")
    if not is_sha256(root["protocol_sha256"]) or not is_sha256(
        root["normalization_profile_sha256"]
    ):
        raise PublicSafetyRefusal("public finding has no valid protocol/profile digest")
    if root["normalization_profile_id"] not in {"graphemic-v1", "allographetic-v1"}:
        raise PublicSafetyRefusal("public finding names an unknown normalization profile")
    if root["measure_quotes"] != list(MEASURE_QUOTES):
        raise PublicSafetyRefusal("public measure quotes differ from the fixed predeclared list")
    if not isinstance(root["matrix"], list):
        raise PublicSafetyRefusal(
            "public finding aggregate candidate-condition matrix is not a list"
        )
    seen_matrix: set[tuple[int, str]] = set()
    for item in root["matrix"]:
        record = _require_exact_keys(item, _MATRIX_KEYS, "matrix row")
        if record["condition"] not in {condition.value for condition in Condition}:
            raise PublicSafetyRefusal("public matrix row has an unknown condition")
        _validate_metric_fields(record, baseline=False)
        marker = (record["subject_index"], record["condition"])
        if marker in seen_matrix:
            raise PublicSafetyRefusal("public matrix repeats a subject-condition row")
        seen_matrix.add(marker)
    expected_matrix = {(slot, condition.value) for slot in (1, 2, 3) for condition in Condition}
    if seen_matrix != expected_matrix:
        raise PublicSafetyRefusal(
            "public matrix is not the sealed three-subject by three-condition product"
        )
    matrix_by_key = {(item["subject_index"], item["condition"]): item for item in root["matrix"]}
    act_count = next(iter(matrix_by_key.values()))["cell_count"]
    if any(row["cell_count"] != act_count for row in matrix_by_key.values()):
        raise PublicSafetyRefusal("public candidate rows do not share one selected-act denominator")
    if not isinstance(root["input_baselines"], list) or not root["input_baselines"]:
        raise PublicSafetyRefusal("public finding has no direct Testimonium baselines")
    seen_sources: set[int] = set()
    for item in root["input_baselines"]:
        record = _require_exact_keys(item, _BASELINE_KEYS, "input baseline row")
        _validate_metric_fields(record, baseline=True)
        if record["source_index"] in seen_sources:
            raise PublicSafetyRefusal("public finding repeats a source baseline row")
        seen_sources.add(record["source_index"])
        if record["cell_count"] != act_count:
            raise PublicSafetyRefusal("public witness baseline does not cover every selected act")
    # Both refusals were written, but the second sat after the first `raise` and
    # could never run, so an empty `condition_deltas` list reached the loop below
    # and was caught only by the closing three-subject check. Stated once, in the
    # condition, where it says what it means.
    if not isinstance(root["condition_deltas"], list) or not root["condition_deltas"]:
        raise PublicSafetyRefusal(
            "public finding condition deltas are missing, empty, or not a list"
        )
    seen_delta_slots: set[int] = set()
    for item in root["condition_deltas"]:
        record = _require_exact_keys(item, _DELTA_KEYS, "condition delta row")
        _require_nonnegative_int(record["subject_index"], "condition delta subject_index")
        if record["subject_index"] < 1:
            raise PublicSafetyRefusal("condition delta subject index must be positive")
        for field in _DELTA_KEYS - {"subject_index"}:
            _require_rate(record[field], field, signed=True)
        if record["subject_index"] in seen_delta_slots:
            raise PublicSafetyRefusal("public finding repeats a condition-delta subject")
        seen_delta_slots.add(record["subject_index"])
        nuda = matrix_by_key[(record["subject_index"], Condition.LECTIO_NUDA.value)]
        primed = matrix_by_key[(record["subject_index"], Condition.WITNESS_PRIMED.value)]
        image_absent = matrix_by_key[
            (record["subject_index"], Condition.IMAGE_ABSENT_CONTROL.value)
        ]
        expected = {
            "priming_cer_delta": nuda["cer"] - primed["cer"],
            "priming_wer_delta": nuda["wer"] - primed["wer"],
            "image_cer_delta": image_absent["cer"] - primed["cer"],
            "image_wer_delta": image_absent["wer"] - primed["wer"],
        }
        for field, value in expected.items():
            if not math.isclose(record[field], value, rel_tol=0, abs_tol=1e-12):
                raise PublicSafetyRefusal(
                    "public condition delta does not match the retained matrix"
                )
    if seen_delta_slots != {1, 2, 3}:
        raise PublicSafetyRefusal("public finding has no full three-subject condition-delta set")
