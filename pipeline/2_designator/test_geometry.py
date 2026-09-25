"""Property-style tests for padding, rescale, and transform digests.

No float appears in an assertion here on purpose, since every quantity this
module produces is an integer.
"""

import itertools

import pytest
from geometry import (
    BP_DENOMINATOR,
    apply_padding,
    from_model_space,
    load_padding_config,
    to_model_space,
    transform_digest,
    verify_isotropic,
)

from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError

PADDING = {"top_bp": 600, "bottom_bp": 1800, "left_bp": 500, "right_bp": 2200}


# --- apply_padding ---------------------------------------------------------


def test_padding_expands_by_the_configured_fraction_of_its_own_dimension():
    result = apply_padding({"x": 100, "y": 100, "w": 100, "h": 50}, 1000, 1000, PADDING)
    assert result["bounds"] == {"x": 95, "y": 97, "w": 100 + 5 + 22, "h": 50 + 3 + 9}
    assert result["applied_px"] == {"top": 3, "bottom": 9, "left": 5, "right": 22}


def test_padding_rounds_half_up_deterministically():
    result = apply_padding({"x": 10, "y": 10, "w": 10, "h": 5}, 1000, 1000, PADDING)
    # w=10, left_bp=500 is exactly 0.5px; half-up gives 1, unlike truncation
    # or round-half-even.
    assert result["applied_px"]["left"] == 1
    assert result["applied_px"]["top"] == 0
    assert result["applied_px"]["bottom"] == 1


def test_padding_clamps_at_every_page_edge_and_records_the_shaved_amount():
    result = apply_padding({"x": 0, "y": 0, "w": 20, "h": 20}, 20, 20, PADDING)
    assert result["bounds"] == {"x": 0, "y": 0, "w": 20, "h": 20}
    assert result["applied_px"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}


def test_padding_clamps_on_one_edge_only_when_only_one_edge_is_tight():
    page_w, page_h = 200, 200
    bounds = {"x": 40, "y": 40, "w": 160, "h": 40}  # x+w == page_w
    nominal = apply_padding(bounds, 10_000, 10_000, PADDING)["applied_px"]
    result = apply_padding(bounds, page_w, page_h, PADDING)
    assert result["applied_px"]["right"] == 0
    assert result["applied_px"]["left"] == nominal["left"]
    assert result["applied_px"]["top"] == nominal["top"]
    assert result["applied_px"]["bottom"] == nominal["bottom"]
    assert result["bounds"]["x"] + result["bounds"]["w"] == page_w


@pytest.mark.parametrize(
    "bounds,page_w,page_h",
    [
        ({"x": 0, "y": 0, "w": 0, "h": 10}, 100, 100),
        ({"x": 0, "y": 0, "w": 10, "h": 0}, 100, 100),
        ({"x": -1, "y": 0, "w": 10, "h": 10}, 100, 100),
        ({"x": 95, "y": 0, "w": 10, "h": 10}, 100, 100),
    ],
)
def test_padding_refuses_degenerate_or_out_of_page_bounds(bounds, page_w, page_h):
    with pytest.raises(
        ContractError, match=r"structural bounds .* falls outside its 100x100 pixel space"
    ):
        apply_padding(bounds, page_w, page_h, PADDING)


def test_padding_refuses_a_non_positive_page():
    with pytest.raises(
        ContractError, match=r"page 0x100 does not have positive integer dimensions"
    ):
        apply_padding({"x": 0, "y": 0, "w": 10, "h": 10}, 0, 100, PADDING)


def test_padding_refuses_a_bool_coordinate_rather_than_reading_it_as_zero_or_one():
    with pytest.raises(ContractError, match="non-integer"):
        apply_padding({"x": 0, "y": 0, "w": 200, "h": True}, 1000, 1000, PADDING)


def test_padding_refuses_a_float_coordinate():
    with pytest.raises(ContractError, match="non-integer"):
        apply_padding({"x": 10.5, "y": 10, "w": 20, "h": 20}, 100, 100, PADDING)


# --- to_model_space / from_model_space round trip ---------------------------


# A fixed grid rather than `random`, so a failure reproduces without a seed.
_ROUND_TRIP_CASES = list(
    itertools.product(
        [(200, 260), (1000, 1000), (837, 1201)],  # page sizes
        [(1024, 1024), (512, 667), (2048, 2048)],  # model-space targets
    )
)


@pytest.mark.parametrize("page,model", _ROUND_TRIP_CASES)
def test_model_space_round_trips_the_page_rectangle_exactly(page, model):
    page_w, page_h = page
    model_w, model_h = model
    whole_page = {"x": 0, "y": 0, "w": page_w, "h": page_h}
    projected = to_model_space(whole_page, page_w, page_h, model_w, model_h)
    recovered = from_model_space(projected["bounds"], projected["scale"], page_w, page_h)
    # No rounding slack: both edges are anchored at 0 and at the page's own size.
    assert recovered == whole_page


@pytest.mark.parametrize("page,model", _ROUND_TRIP_CASES)
def test_a_round_trip_never_loses_a_pixel_of_the_original_rectangle(page, model):
    """The property that matters is containment, not closeness: a lossy round
    trip is fine as long as it's one-sided and the recovered rectangle always
    contains the original, never clips it.
    """
    page_w, page_h = page
    model_w, model_h = model
    bounds = {"x": page_w // 5, "y": page_h // 7, "w": page_w // 3, "h": page_h // 4}
    projected = to_model_space(bounds, page_w, page_h, model_w, model_h)
    recovered = from_model_space(projected["bounds"], projected["scale"], page_w, page_h)
    assert recovered["x"] <= bounds["x"], (recovered, bounds)
    assert recovered["y"] <= bounds["y"], (recovered, bounds)
    assert recovered["x"] + recovered["w"] >= bounds["x"] + bounds["w"], (recovered, bounds)
    assert recovered["y"] + recovered["h"] >= bounds["y"] + bounds["h"], (recovered, bounds)


@pytest.mark.parametrize("page,model", _ROUND_TRIP_CASES)
def test_a_round_trip_grows_a_rectangle_by_at_most_a_pixel_per_edge(page, model):
    """Outward rounding is a bounded correction, not a licence to widen."""
    page_w, page_h = page
    model_w, model_h = model
    bounds = {"x": page_w // 5, "y": page_h // 7, "w": page_w // 3, "h": page_h // 4}
    projected = to_model_space(bounds, page_w, page_h, model_w, model_h)
    recovered = from_model_space(projected["bounds"], projected["scale"], page_w, page_h)
    # One model-space pixel of outward rounding each way, per edge, converted
    # to source pixels; two edges per axis.
    slack_x = 2 * (-(-page_w // model_w) + 1)
    slack_y = 2 * (-(-page_h // model_h) + 1)
    assert recovered["w"] - bounds["w"] <= slack_x, (recovered, bounds, slack_x)
    assert recovered["h"] - bounds["h"] <= slack_y, (recovered, bounds, slack_y)


def test_model_space_scale_is_an_exact_integer_ratio_not_a_float():
    projected = to_model_space({"x": 0, "y": 0, "w": 200, "h": 260}, 200, 260, 1024, 1024)
    for axis in ("x", "y"):
        ratio = projected["scale"][axis]
        assert isinstance(ratio["numerator"], int)
        assert isinstance(ratio["denominator"], int)
    # Canonicalizing must not raise: nothing in the returned structure is a float.
    digest_of(projected)


def test_from_model_space_refuses_a_scale_that_does_not_belong_to_this_page():
    projected = to_model_space({"x": 0, "y": 0, "w": 200, "h": 260}, 200, 260, 1024, 1024)
    with pytest.raises(ContractError, match="recorded for"):
        from_model_space(projected["bounds"], projected["scale"], 50, 50)


def test_from_model_space_refuses_another_pages_scale_even_when_the_rectangle_would_fit():
    projected = to_model_space({"x": 10, "y": 10, "w": 20, "h": 20}, 200, 260, 100, 130)
    with pytest.raises(ContractError, match="recorded for"):
        from_model_space(projected["bounds"], projected["scale"], 100, 130)


@pytest.mark.parametrize(
    ("bounds", "refusal"),
    [
        # The fourth case is a different refusal (type, not geometry); the
        # message is matched so each case proves the check it's named for.
        (
            {"x": -1, "y": 0, "w": 2, "h": 2},
            r"source bounds .* falls outside its 100x100 pixel space",
        ),
        (
            {"x": 0, "y": 0, "w": 0, "h": 2},
            r"source bounds .* falls outside its 100x100 pixel space",
        ),
        (
            {"x": 99, "y": 0, "w": 2, "h": 2},
            r"source bounds .* falls outside its 100x100 pixel space",
        ),
        ({"x": 0, "y": 0, "w": True, "h": 2}, r"source bounds has a non-integer coordinate"),
    ],
)
def test_to_model_space_refuses_invalid_source_rectangles(bounds, refusal):
    with pytest.raises(ContractError, match=refusal):
        to_model_space(bounds, 100, 100, 50, 50)


def test_from_model_space_refuses_a_rectangle_outside_the_recorded_model_space():
    scale = {
        "x": {"numerator": 50, "denominator": 100},
        "y": {"numerator": 50, "denominator": 100},
    }
    with pytest.raises(ContractError, match="model-space bounds"):
        from_model_space({"x": 49, "y": 0, "w": 2, "h": 2}, scale, 100, 100)


def test_from_model_space_refuses_a_malformed_scale():
    with pytest.raises(ContractError, match=r"scale\.x .* is not a positive integer ratio"):
        from_model_space({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": {}, "y": {}}, 100, 100)


@pytest.mark.parametrize(
    ("page", "refusal"),
    [
        # ("100", 100) renders as "page 100x100 ..." -- indistinguishable from a
        # valid page by eye, which is why each case pins its own expectation.
        ((True, 100), r"page Truex100 does not have positive integer dimensions"),
        ((100, 1.5), r"page 100x1\.5 does not have positive integer dimensions"),
        (("100", 100), r"page 100x100 does not have positive integer dimensions"),
    ],
)
def test_model_space_conversion_refuses_non_integer_dimensions(page, refusal):
    with pytest.raises(ContractError, match=refusal):
        to_model_space({"x": 0, "y": 0, "w": 1, "h": 1}, page[0], page[1], 50, 50)


def test_from_model_space_refuses_a_non_object_scale():
    with pytest.raises(ContractError, match="ratio object"):
        from_model_space({"x": 0, "y": 0, "w": 1, "h": 1}, None, 100, 100)


# --- verify_isotropic --------------------------------------------------------


def test_verify_isotropic_accepts_equal_axis_scales():
    scale = {
        "x": {"numerator": 1024, "denominator": 1000},
        "y": {"numerator": 1024, "denominator": 1000},
    }
    verify_isotropic(scale)  # must not raise


def test_verify_isotropic_accepts_rounding_noise_from_different_page_dimensions():
    # Same real-world scale, computed on a non-square page: the two ratios are
    # different fractions representing nearly the same physical scale.
    projected = to_model_space({"x": 0, "y": 0, "w": 200, "h": 199}, 200, 199, 1024, 1020)
    verify_isotropic(projected["scale"], tolerance_bp=100)


def test_verify_isotropic_refuses_a_distorted_rescale():
    # Roughly 1x width, 2x height: a squished resize, not a letterboxed one.
    scale = {
        "x": {"numerator": 1000, "denominator": 1000},
        "y": {"numerator": 2000, "denominator": 1000},
    }
    with pytest.raises(ContractError, match="anisotropic"):
        verify_isotropic(scale)


@pytest.mark.parametrize(
    "ratio",
    [
        {"numerator": -1, "denominator": 1},
        {"numerator": 1, "denominator": -1},
        {"numerator": True, "denominator": 1},
        {"numerator": 1, "denominator": False},
        {"numerator": 1.0, "denominator": 1},
        {"numerator": 1, "denominator": 1.0},
    ],
)
def test_verify_isotropic_refuses_non_positive_or_boolean_ratio_components(ratio):
    scale = {"x": ratio, "y": ratio}
    with pytest.raises(ContractError, match="positive integer ratio"):
        verify_isotropic(scale)


@pytest.mark.parametrize("tolerance", [-1, True, 1.5])
def test_verify_isotropic_refuses_an_invalid_tolerance(tolerance):
    scale = {
        "x": {"numerator": 1, "denominator": 1},
        "y": {"numerator": 1, "denominator": 1},
    }
    with pytest.raises(ContractError, match="non-negative plain integer"):
        verify_isotropic(scale, tolerance_bp=tolerance)


# --- transform_digest ---------------------------------------------------------


def test_transform_digest_is_stable_for_identical_transforms():
    transform = {"operation": "crop", "bounds": {"x": 1, "y": 2, "w": 3, "h": 4}}
    assert transform_digest(transform) == transform_digest(dict(transform))


def test_transform_digest_changes_when_a_bound_changes():
    base = {"operation": "crop", "bounds": {"x": 1, "y": 2, "w": 3, "h": 4}}
    changed = {"operation": "crop", "bounds": {"x": 1, "y": 2, "w": 3, "h": 5}}
    assert transform_digest(base) != transform_digest(changed)


def test_transform_digest_matches_the_shared_canonical_digest():
    transform = {"operation": "crop", "bounds": {"x": 1, "y": 2, "w": 3, "h": 4}}
    assert transform_digest(transform) == digest_of(transform)


# --- load_padding_config -----------------------------------------------------

PROVENANCE = {
    "source": "test fixture",
    "corpus": "test corpus",
    "sample_unit": "test-record",
    "sample_count": 1,
    "statistic": "test statistic",
    "calibrated_for_this_corpus": False,
    "caveat": "test caveat",
}


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_padding_toml(path, *, fields=None, provenance=None, omit_provenance_table=False) -> None:
    """Write a `[padding]` (+ `[padding.provenance]`) TOML file for a test.

    `provenance=None` writes the default valid block; `omit_provenance_table`
    skips the table entirely; either argument may be a dict missing or adding
    fields, so a test can construct exactly the malformed shape it needs.
    """
    lines = ["[padding]"] + [
        f"{name} = {_toml_value(value)}" for name, value in (fields or PADDING).items()
    ]
    if not omit_provenance_table:
        lines.append("[padding.provenance]")
        for name, value in (PROVENANCE if provenance is None else provenance).items():
            lines.append(f"{name} = {_toml_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_padding_config_reads_the_shipped_defaults():
    config = load_padding_config()
    assert config["top_bp"] == 600
    assert config["bottom_bp"] == 1800
    assert config["left_bp"] == 500
    assert config["right_bp"] == 2200
    assert len(config["config_sha256"]) == 64
    provenance = config["provenance"]
    # The shipped default is honest that it's carried forward from a
    # third-party corpus, not calibrated for this project's own.
    assert provenance["calibrated_for_this_corpus"] is False
    assert provenance["sample_count"] == 4572
    assert "Teklia" in provenance["corpus"]


def test_load_padding_config_refuses_a_missing_file(tmp_path):
    with pytest.raises(ContractError, match=r"the padding configuration at .* could not be read"):
        load_padding_config(tmp_path / "does-not-exist.toml")


def test_load_padding_config_refuses_a_missing_table(tmp_path):
    path = tmp_path / "padding.toml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ContractError, match=r"the padding configuration has no \[padding\] table"):
        load_padding_config(path)


@pytest.mark.parametrize("missing_field", ["top_bp", "bottom_bp", "left_bp", "right_bp"])
def test_load_padding_config_refuses_a_missing_field(tmp_path, missing_field):
    """Refused for *this* missing field, not for the absent provenance table
    a hand-written `[padding]` table would also be refused for.
    """
    fields = dict(PADDING)
    del fields[missing_field]
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, fields=fields)
    with pytest.raises(ContractError, match=missing_field):
        load_padding_config(path)


def test_load_padding_config_refuses_a_negative_bp_field(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, fields={**PADDING, "top_bp": -1})
    with pytest.raises(ContractError, match="invalid non-negative integer"):
        load_padding_config(path)


def test_load_padding_config_refuses_a_string_bp_field(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, fields={**PADDING, "left_bp": "500"})
    with pytest.raises(ContractError, match="invalid non-negative integer"):
        load_padding_config(path)


def test_load_padding_config_refuses_a_bool_bp_field(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, fields={**PADDING, "right_bp": True})
    with pytest.raises(ContractError, match="invalid non-negative integer"):
        load_padding_config(path)


def test_load_padding_config_refuses_an_unknown_padding_field(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, fields={**PADDING, "right_bps": 9999})
    with pytest.raises(ContractError, match=r"unknown field.*right_bps"):
        load_padding_config(path)


def test_load_padding_config_refuses_an_unknown_top_level_table(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("[paddding]\ntop_bp = 9999\n")
    with pytest.raises(ContractError, match=r"unknown top-level field.*paddding"):
        load_padding_config(path)


def test_load_padding_config_refuses_malformed_toml_syntax(tmp_path):
    path = tmp_path / "padding.toml"
    path.write_text("[padding]\ntop_bp = 600\nbottom_bp = [unterminated\n", encoding="utf-8")
    with pytest.raises(ContractError, match="could not be read"):
        load_padding_config(path)


def test_bp_denominator_is_ten_thousand():
    assert BP_DENOMINATOR == 10_000


# --- [padding.provenance]: a padding fraction with no declared source may not
# be shipped as a default. Each test below proves one way that refusal fires.


def test_load_padding_config_accepts_a_well_formed_custom_provenance(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path)
    config = load_padding_config(path)
    assert config["provenance"] == PROVENANCE


def test_load_padding_config_refuses_no_provenance_table_at_all(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, omit_provenance_table=True)
    with pytest.raises(ContractError, match=r"\[padding\.provenance\]"):
        load_padding_config(path)


def test_load_padding_config_refuses_an_unknown_provenance_field(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, provenance={**PROVENANCE, "extra_field": "surprise"})
    with pytest.raises(ContractError, match="unknown field"):
        load_padding_config(path)


@pytest.mark.parametrize("missing_field", list(PROVENANCE))
def test_load_padding_config_refuses_a_missing_provenance_field(tmp_path, missing_field):
    provenance = dict(PROVENANCE)
    del provenance[missing_field]
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, provenance=provenance)
    with pytest.raises(ContractError, match="missing field"):
        load_padding_config(path)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("source", ""),
        ("corpus", "   "),
        ("sample_unit", ""),
        ("statistic", ""),
        ("caveat", ""),
    ],
)
def test_load_padding_config_refuses_a_blank_provenance_string_field(tmp_path, field, bad_value):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, provenance={**PROVENANCE, field: bad_value})
    with pytest.raises(ContractError, match="non-empty string"):
        load_padding_config(path)


def test_load_padding_config_refuses_a_negative_sample_count(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, provenance={**PROVENANCE, "sample_count": -1})
    with pytest.raises(ContractError, match="sample_count"):
        load_padding_config(path)


def test_load_padding_config_refuses_a_non_boolean_calibrated_flag(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(path, provenance={**PROVENANCE, "calibrated_for_this_corpus": "true"})
    with pytest.raises(ContractError, match="calibrated_for_this_corpus"):
        load_padding_config(path)


def test_this_project_has_exactly_one_basis_point_rounding_rule():
    """`geometry._pad_amount` delegates to `common.background.round_half_up_bp`
    (shared with the Ink Map and Recensor); this pins both halves agree,
    including at the exact half-pixel tie the half-up rule exists for.
    """
    import geometry

    from common.background import BASIS_POINTS, round_half_up_bp

    assert geometry.BP_DENOMINATOR == BASIS_POINTS
    assert geometry._pad_amount(10, 500) == 1  # exactly 0.5 px: half-up, not half-even
    for dimension in (1, 5, 10, 200, 260, 2480, 3508):
        for bp in (0, 1, 250, 499, 500, 501, 1500, 3333, 9999, 10000):
            assert geometry._pad_amount(dimension, bp) == round_half_up_bp(dimension, bp)


def test_load_padding_config_refuses_calibrated_provenance_with_no_samples(tmp_path):
    path = tmp_path / "padding.toml"
    _write_padding_toml(
        path,
        provenance={**PROVENANCE, "calibrated_for_this_corpus": True, "sample_count": 0},
    )
    with pytest.raises(ContractError, match="calibrated_for_this_corpus.*sample_count is zero"):
        load_padding_config(path)
