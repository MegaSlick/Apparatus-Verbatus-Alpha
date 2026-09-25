"""A ready-to-run calibration harness for capture padding — not yet run.

`config/designator_padding.toml`'s four fractions are carried forward from a
third-party corpus, not yet re-derived against this project's own pages. Given
a gold set of (detected structural rectangle, true content rectangle) pairs,
this module computes fresh per-edge padding fractions: a percentile of how far
true content falls outside the detected box, per edge, as a fraction of the
detected box's own dimension for that edge.

`calibrate_padding` refuses an empty sample set, and `sample_size_caveat` names
a sample too small for the percentile to be a defensible estimate rather than
silently accepting it (see `PREFERRED_SAMPLE_COUNT`/`MINIMUM_DEFENSIBLE_SAMPLES`
below for where those floors come from).

The output is shaped like the shipped config's `[padding]` and
`[padding.provenance]` tables but is never written to the config file itself:
adopting it is a decision for whoever holds the gold set, not this module.
"""

from collections.abc import Mapping
from typing import Any, Final, TypedDict, cast

from geometry import BP_DENOMINATOR, Bounds

from common.contracts.errors import ContractError

# PREFERRED_SAMPLE_COUNT is CLSI EP28-A3c's minimum for a nonparametric
# reference interval (a stricter statistic than the p75 estimated here);
# MINIMUM_DEFENSIBLE_SAMPLES is this project's own smaller floor below it.
# Below the floor, sample_size_caveat names the result provisional rather
# than refusing it outright.
MINIMUM_DEFENSIBLE_SAMPLES: Final = 60
PREFERRED_SAMPLE_COUNT: Final = 120

_EDGES: Final = ("top", "bottom", "left", "right")


class GoldSample(TypedDict):
    detected: Bounds
    true_content: Bounds


def _validated_bounds(value: object, *, sample_index: int, name: str) -> Bounds:
    """One complete integer rectangle with positive dimensions, or a named refusal."""
    fields = {"x", "y", "w", "h"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(
            f"gold sample {sample_index} {name} rectangle has fields outside {sorted(fields)}"
        )
    if any(not isinstance(value[field], int) or isinstance(value[field], bool) for field in fields):
        raise ContractError(f"gold sample {sample_index} {name} rectangle is not integer-valued")
    if value["w"] <= 0 or value["h"] <= 0:
        raise ContractError(
            f"gold sample {sample_index} {name} rectangle has non-positive dimensions"
        )
    return cast(Bounds, dict(value))


def _validated_sample(value: object, sample_index: int) -> GoldSample:
    """Validate the runtime shape the ``GoldSample`` type hint cannot enforce."""
    if not isinstance(value, Mapping) or set(value) != {"detected", "true_content"}:
        raise ContractError(
            f"gold sample {sample_index} has fields outside ['detected', 'true_content']"
        )
    return {
        "detected": _validated_bounds(
            value["detected"], sample_index=sample_index, name="detected"
        ),
        "true_content": _validated_bounds(
            value["true_content"], sample_index=sample_index, name="true_content"
        ),
    }


def _edge_shortfall_bp(detected: Bounds, true_content: Bounds, edge: str) -> int:
    """How far `true_content` extends past `detected` on one edge, in basis
    points of `detected`'s own dimension for that edge.

    Zero, never negative: a detected box that already fully contains the true
    content on this edge has no shortfall to report, and a true content box
    entirely inside the detected one contributes zero to every edge's
    percentile rather than a negative value that would pull it down.
    """
    if edge == "top":
        shortfall_px = max(0, detected["y"] - true_content["y"])
        dimension = detected["h"]
    elif edge == "bottom":
        shortfall_px = max(
            0, (true_content["y"] + true_content["h"]) - (detected["y"] + detected["h"])
        )
        dimension = detected["h"]
    elif edge == "left":
        shortfall_px = max(0, detected["x"] - true_content["x"])
        dimension = detected["w"]
    elif edge == "right":
        shortfall_px = max(
            0, (true_content["x"] + true_content["w"]) - (detected["x"] + detected["w"])
        )
        dimension = detected["w"]
    else:  # pragma: no cover - closed set, guarded by the caller
        raise ContractError(f"edge {edge!r} is not one of {_EDGES}")
    if dimension <= 0:
        raise ContractError(f"a detected rectangle {detected} has no positive area to divide by")
    # Round-half-up, integer-only, matching geometry._pad_amount's discipline
    # so the result is deterministic rather than float-rounding-dependent.
    return (shortfall_px * BP_DENOMINATOR + dimension // 2) // dimension


def _nearest_rank_percentile(values: list[int], percentile: int) -> int:
    """The nearest-rank percentile of a small integer sample.

    Nearest-rank, not interpolated: a sample in the tens shouldn't claim
    precision between two observed values. Ranks round up, so an even split's
    75th percentile reports the higher group rather than rounding it away.
    """
    if not values:
        raise ContractError("cannot take a percentile of zero samples")
    if not (0 < percentile <= 100):
        raise ContractError(f"percentile {percentile} is not in (0, 100]")
    ordered = sorted(values)
    rank = -(-len(ordered) * percentile // 100)  # ceil division, integers only
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def sample_size_caveat(sample_count: int) -> str:
    """The honest, sample-size-dependent caveat for one calibration run."""
    if sample_count < MINIMUM_DEFENSIBLE_SAMPLES:
        return (
            f"only {sample_count} gold sample(s); below the ~{MINIMUM_DEFENSIBLE_SAMPLES}-sample "
            "floor a nonparametric percentile needs to be more than noise. Treat this result as "
            "provisional and re-run once more gold pages exist"
        )
    if sample_count < PREFERRED_SAMPLE_COUNT:
        return (
            f"{sample_count} gold sample(s), above the ~{MINIMUM_DEFENSIBLE_SAMPLES}-sample "
            f"floor but below the ~{PREFERRED_SAMPLE_COUNT} preferred for a percentile this "
            "far from the median. Usable, not yet the target sample size"
        )
    return f"{sample_count} gold sample(s), at or above the preferred sample size"


def calibrate_padding(
    samples: list[GoldSample],
    *,
    percentile: int = 75,
    corpus: str,
    sample_unit: str,
    calibrated_for_this_corpus: bool,
) -> dict[str, Any]:
    """Fresh per-edge padding fractions from real (detected, true) rectangle pairs.

    Shaped like `config/designator_padding.toml`'s own `[padding]` plus
    `[padding.provenance]` tables. Computes the numbers only; does not write a
    file and is not called by any run-path code.
    """
    if not isinstance(samples, list):
        raise ContractError("gold samples must be supplied as a list for deterministic calibration")
    if not isinstance(calibrated_for_this_corpus, bool):
        raise ContractError("calibrated_for_this_corpus must be a boolean caller decision")
    if not samples:
        raise ContractError(
            "cannot calibrate padding from zero gold samples; a percentile of nothing is "
            "not a number, it is an absence wearing a number's shape"
        )
    validated_samples = [_validated_sample(sample, index) for index, sample in enumerate(samples)]
    per_edge_bp = {
        edge: _nearest_rank_percentile(
            [
                _edge_shortfall_bp(sample["detected"], sample["true_content"], edge)
                for sample in validated_samples
            ],
            percentile,
        )
        for edge in _EDGES
    }
    return {
        "top_bp": per_edge_bp["top"],
        "bottom_bp": per_edge_bp["bottom"],
        "left_bp": per_edge_bp["left"],
        "right_bp": per_edge_bp["right"],
        "provenance": {
            "source": "pipeline/2_designator/padding_calibration.py, run against real gold samples",
            "corpus": corpus,
            "sample_unit": sample_unit,
            "sample_count": len(validated_samples),
            "statistic": f"p{percentile} per-edge shortfall, as a fraction of the detected "
            "box's own dimension for that edge, nearest-rank",
            # Caller-supplied: rectangle coordinates alone can't say whether
            # a sample belongs to this project's corpus.
            "calibrated_for_this_corpus": calibrated_for_this_corpus,
            "caveat": sample_size_caveat(len(validated_samples)),
        },
    }
