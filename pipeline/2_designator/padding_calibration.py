"""A ready-to-run calibration harness for capture padding — not yet run.

`config/designator_padding.toml`'s four fractions are carried forward from a
third-party corpus (see that file's `[padding.provenance]`), and nothing in
this repository has re-derived them against this project's own pages. This
module is what "re-derive them" means concretely: given a gold set of
(detected structural rectangle, true content rectangle) pairs on this
project's own material, compute fresh per-edge padding fractions the same way
the old pipeline described doing it — a percentile of how far true content
falls outside the detected box, per edge, expressed as a fraction of the
detected box's own dimension.

**Nothing here invents a number.** `calibrate_padding` refuses an empty sample
set outright, and `sample_size_caveat` names — rather than silently accepts —
a sample too small for the requested percentile to be a defensible estimate
rather than noise dressed as a statistic. Nonparametric percentile estimation
for a reference range needs on the order of 60 samples at a minimum, more for
a percentile near the tails (CLSI EP28-A3c's guidance for exactly this kind of
estimate, and independent work on percentile confidence intervals for skewed
data agrees on the same order of magnitude) — this module's threshold follows
that guidance rather than a number invented for this project.

This harness produces the SAME four fractions the shipped config carries in
shape (basis points of the detected box's own width/height, asymmetric per
edge) but never in the config file itself: writing a freshly-calibrated
`designator_padding.toml` is a decision for whoever holds the gold set this
project does not yet have, not something this module does on import.
"""

from typing import Any, Final, TypedDict

from geometry import BP_DENOMINATOR, Bounds

from common.contracts.errors import ContractError

# Below this many gold samples, a percentile estimate is named provisional
# rather than refused outright — GOALS 1 and 2 want capture padding sooner
# rather than a perfect one later, and a provisional number that says so is
# safer than none at all. The number itself follows CLSI EP28-A3c's
# nonparametric reference-range guidance (minimum ~60, 120+ preferred), not an
# invented threshold: see the module docstring.
MINIMUM_DEFENSIBLE_SAMPLES: Final = 60
PREFERRED_SAMPLE_COUNT: Final = 120

_EDGES: Final = ("top", "bottom", "left", "right")


class GoldSample(TypedDict):
    detected: Bounds
    true_content: Bounds


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
    return round((shortfall_px * BP_DENOMINATOR) / dimension)


def _nearest_rank_percentile(values: list[int], percentile: int) -> int:
    """The nearest-rank percentile of a small integer sample.

    Nearest-rank rather than a linear-interpolation percentile: with a sample
    in the tens rather than the thousands, interpolating between two observed
    values claims a precision the data does not have, and nearest-rank always
    returns a value that was actually observed. `ceil` rather than `floor` or
    round-to-nearest so the 75th percentile of an even split still reports the
    higher group, matching "how bad must the padding be to cover this
    fraction of cases" rather than rounding the requirement away.
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
    samples: list[GoldSample], *, percentile: int = 75, corpus: str, sample_unit: str
) -> dict[str, Any]:
    """Fresh per-edge padding fractions from real (detected, true) rectangle pairs.

    Returns a payload shaped like `config/designator_padding.toml`'s own
    `[padding]` plus `[padding.provenance]` tables, ready to be written out by
    a caller that has decided to adopt it — this function only computes the
    numbers and states plainly what they rest on; it does not write a file
    and is not called by any run-path code.
    """
    if not samples:
        raise ContractError(
            "cannot calibrate padding from zero gold samples; a percentile of nothing is "
            "not a number, it is an absence wearing a number's shape"
        )
    per_edge_bp = {
        edge: _nearest_rank_percentile(
            [
                _edge_shortfall_bp(sample["detected"], sample["true_content"], edge)
                for sample in samples
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
            "sample_count": len(samples),
            "statistic": f"p{percentile} per-edge shortfall, as a fraction of the detected "
            "box's own dimension for that edge, nearest-rank",
            "calibrated_for_this_corpus": True,
            "caveat": sample_size_caveat(len(samples)),
        },
    }
