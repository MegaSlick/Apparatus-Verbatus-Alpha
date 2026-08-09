"""Tests for the padding calibration harness — proven against synthetic gold
pairs only, never against invented "real" numbers. See the module docstring:
this harness is ready to run against a real gold set, and none exists yet.
"""

import pytest
from geometry import BP_DENOMINATOR
from padding_calibration import (
    MINIMUM_DEFENSIBLE_SAMPLES,
    PREFERRED_SAMPLE_COUNT,
    _edge_shortfall_bp,
    _nearest_rank_percentile,
    calibrate_padding,
    sample_size_caveat,
)

from common.contracts.errors import ContractError

# --- _edge_shortfall_bp -------------------------------------------------------


def test_true_content_fully_inside_detected_box_has_zero_shortfall_every_edge():
    detected = {"x": 0, "y": 0, "w": 100, "h": 100}
    true_content = {"x": 10, "y": 10, "w": 50, "h": 50}
    assert all(
        _edge_shortfall_bp(detected, true_content, edge) == 0
        for edge in ("top", "bottom", "left", "right")
    )


def test_shortfall_is_a_fraction_of_the_detected_boxs_own_dimension():
    # True content extends 10px above a 100px-tall detected box: 10% = 1000bp.
    detected = {"x": 0, "y": 20, "w": 100, "h": 100}
    true_content = {"x": 0, "y": 10, "w": 100, "h": 110}
    assert _edge_shortfall_bp(detected, true_content, "top") == 1000


def test_shortfall_on_the_bottom_and_right_edges():
    detected = {"x": 10, "y": 10, "w": 100, "h": 100}
    # Extends 25px past the bottom (25% of h) and 5px past the right (5% of w).
    true_content = {"x": 10, "y": 10, "w": 105, "h": 125}
    assert _edge_shortfall_bp(detected, true_content, "bottom") == 2500
    assert _edge_shortfall_bp(detected, true_content, "right") == 500


def test_a_non_positive_detected_dimension_is_refused():
    with pytest.raises(ContractError):
        _edge_shortfall_bp(
            {"x": 0, "y": 0, "w": 0, "h": 10}, {"x": 0, "y": 0, "w": 1, "h": 1}, "left"
        )


# --- _nearest_rank_percentile --------------------------------------------------


def test_nearest_rank_percentile_returns_an_observed_value():
    values = [10, 20, 30, 40]
    result = _nearest_rank_percentile(values, 75)
    assert result in values


def test_p75_of_four_ascending_values_is_the_third():
    # ceil(4 * 0.75) = 3 -> the 3rd smallest, 1-indexed.
    assert _nearest_rank_percentile([10, 20, 30, 40], 75) == 30


def test_p50_of_a_single_value_is_that_value():
    assert _nearest_rank_percentile([42], 50) == 42


def test_percentile_of_empty_sample_is_refused():
    with pytest.raises(ContractError):
        _nearest_rank_percentile([], 75)


@pytest.mark.parametrize("percentile", [0, 101, -5])
def test_percentile_outside_valid_range_is_refused(percentile):
    with pytest.raises(ContractError):
        _nearest_rank_percentile([1, 2, 3], percentile)


# --- sample_size_caveat --------------------------------------------------------


def test_below_the_minimum_is_named_provisional():
    caveat = sample_size_caveat(MINIMUM_DEFENSIBLE_SAMPLES - 1)
    assert "provisional" in caveat


def test_between_minimum_and_preferred_is_named_usable_but_short():
    caveat = sample_size_caveat(MINIMUM_DEFENSIBLE_SAMPLES)
    assert "Usable" in caveat


def test_at_or_above_preferred_names_no_shortfall():
    caveat = sample_size_caveat(PREFERRED_SAMPLE_COUNT)
    assert "provisional" not in caveat
    assert "preferred sample size" in caveat


# --- calibrate_padding ---------------------------------------------------------


def test_calibrate_padding_refuses_zero_samples():
    with pytest.raises(ContractError):
        calibrate_padding([], corpus="test", sample_unit="test-record")


def test_calibrate_padding_produces_the_shipped_config_shape():
    samples = [
        {
            "detected": {"x": 0, "y": 20, "w": 100, "h": 100},
            "true_content": {"x": 0, "y": 10, "w": 100, "h": 110},
        }
    ]
    result = calibrate_padding(
        samples, corpus="synthetic-test-only", sample_unit="synthetic-record"
    )
    assert set(result) == {"top_bp", "bottom_bp", "left_bp", "right_bp", "provenance"}
    assert result["top_bp"] == 1000
    assert result["bottom_bp"] == 0
    assert result["left_bp"] == 0
    assert result["right_bp"] == 0
    for value in (result["top_bp"], result["bottom_bp"], result["left_bp"], result["right_bp"]):
        assert isinstance(value, int) and not isinstance(value, bool)
    provenance = result["provenance"]
    assert provenance["calibrated_for_this_corpus"] is True
    assert provenance["sample_count"] == 1
    assert "provisional" in provenance["caveat"]
    assert provenance["corpus"] == "synthetic-test-only"


def test_calibrate_padding_result_loads_through_load_padding_config_shape(tmp_path):
    """The harness's output is drop-in compatible with the shipped config's own
    reader — proof that adopting a fresh calibration is a file write, not a
    schema migration."""
    import geometry

    samples = [
        {
            "detected": {"x": 0, "y": 0, "w": 100, "h": 100},
            "true_content": {"x": 0, "y": 0, "w": 100, "h": 100},
        }
        for _ in range(MINIMUM_DEFENSIBLE_SAMPLES)
    ]
    result = calibrate_padding(
        samples, corpus="synthetic-test-only", sample_unit="synthetic-record"
    )
    lines = ["[padding]"]
    for field in ("top_bp", "bottom_bp", "left_bp", "right_bp"):
        lines.append(f"{field} = {result[field]}")
    lines.append("[padding.provenance]")
    for field, value in result["provenance"].items():
        if isinstance(value, bool):
            lines.append(f"{field} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{field} = {value}")
        else:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{field} = "{escaped}"')
    path = tmp_path / "calibrated.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = geometry.load_padding_config(path)
    assert loaded["top_bp"] == result["top_bp"]
    assert loaded["provenance"]["calibrated_for_this_corpus"] is True


def test_basis_points_stay_within_denominator_scale_for_a_realistic_shortfall():
    # A pathological case: true content twice as tall as detected, entirely
    # below it. The shortfall exceeds 100% and basis points reflect that
    # honestly rather than clamping -- a real percentile output should look
    # exactly this alarming when the structural detector is this wrong.
    detected = {"x": 0, "y": 0, "w": 10, "h": 10}
    true_content = {"x": 0, "y": 10, "w": 10, "h": 20}
    assert _edge_shortfall_bp(detected, true_content, "bottom") == 2 * BP_DENOMINATOR
