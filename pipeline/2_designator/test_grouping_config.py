"""Tests for the grouping/reconciliation policy loader and resolver.

No float appears in an assertion here on purpose, matching `test_geometry.py`:
every resolved quantity is an integer, and a test written with float
arithmetic could pass by accident where the implementation quietly
reintroduced one.

`grouping_config` and `geometry` are imported bare, not dotted --
`pipeline.2_designator` cannot be a Python package path (`2_designator`
starts with a digit). Pytest's default "prepend" import mode puts this
file's own directory on `sys.path` before collecting it.
"""

import dataclasses
import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from grouping_config import (
    _PROVENANCE_FIELDS,
    _STRING_PROVENANCE_FIELDS,
    _TYPED_PROVENANCE_FIELDS,
    DEFAULT_GROUPING_CONFIG_PATH,
    GroupingThresholds,
    load_grouping_config,
    resolve_background_policy,
    resolve_thresholds,
)

from common.contracts.errors import ContractError
from common.imaging import dimensions

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "proof" / "fixtures" / "synthetic-two-page-v0"

# The retired module-level constants this file's basis-point fields replace.
_RETIRED = {
    "margin_bp": ("width", 0.15),  # grouping.DEFAULT_MARGIN_FRACTION -- a fraction, not px
    "chain_gap_bp": ("height", 6),  # grouping.DEFAULT_CHAIN_GAP_PX
    "anchor_reach_bp": ("height", 2),  # grouping.DEFAULT_ANCHOR_REACH_PX
    "brace_min_height_bp": ("height", 30),  # grouping.DEFAULT_BRACE_MIN_HEIGHT_PX
    "page_edge_reach_bp": ("height", 4),  # grouping.DEFAULT_PAGE_EDGE_REACH_PX
    "review_priority_min_dimension_bp": (
        "height",
        6,
    ),  # conservation.DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX
    "fallback_overlap_bp": ("height", 8),  # grouping.DEFAULT_FALLBACK_OVERLAP_PX
}


def _measured_fixture_page_sizes() -> list[tuple[int, int]]:
    """The fixture PNGs' own actual dimensions, decoded, not assumed."""
    sizes = []
    for png in sorted(FIXTURE_DIR.glob("*.png")):
        width, height = dimensions(png.read_bytes())
        sizes.append((width, height))
    return sizes


MEASURED_FIXTURE_SIZES = _measured_fixture_page_sizes()


def test_fixture_pages_measure_200x260():
    """Pin what unit A's first act found: every fixture page is 200x260.

    If this ever fails, every bit-identity assertion below (and the config's
    own header comment and provenance) is computed against the wrong size and
    must be redone -- see SPEC_C.md's own instruction to report a mismatch
    before writing the config, not after.
    """
    assert MEASURED_FIXTURE_SIZES == [(200, 260), (200, 260), (200, 260)]


# --- load_grouping_config: happy path ---------------------------------------


def test_default_config_loads_and_carries_a_digest_of_its_own_bytes():
    config = load_grouping_config()
    raw = DEFAULT_GROUPING_CONFIG_PATH.read_bytes()
    from common.contracts.canonical import digest_bytes

    assert config["config_sha256"] == digest_bytes(raw)
    assert config["max_residual_components"] == 2000
    assert config["max_secondary_proposals"] == 2000
    assert config["fallback_bands"] == 4
    assert set(config["page_fraction_bp"]) == set(_RETIRED)
    assert config["absolute"] == {"gap_tolerance_px": 3}
    assert config["provenance"]["calibrated_for_this_corpus"] is False
    assert config["background"]["band_bp"] == 500
    assert config["background"]["max_interior_dark_bp"] == 5000
    assert config["background"]["max_ink_bp"] == 7000
    assert config["background"]["ink_margin_bp"] == 3333
    # The one measured block in this file, and the only place in it where the
    # flag is true. Pinned so a later edit cannot quietly widen the claim: 127
    # real pages from five sources, not zero synthetic ones.
    assert config["background"]["provenance"]["calibrated_for_this_corpus"] is True
    assert config["background"]["provenance"]["sample_count"] == 127


def test_default_config_is_valid_toml_matching_the_loaded_shape():
    raw = tomllib.loads(DEFAULT_GROUPING_CONFIG_PATH.read_bytes().decode("utf-8"))
    assert set(raw) == {"grouping"}
    assert set(raw["grouping"]) == {
        "max_residual_components",
        "max_secondary_proposals",
        "fallback_bands",
        "page_fraction_bp",
        "absolute",
        "background",
        "provenance",
    }


# --- resolve_thresholds: bit-identity to the retired constants -------------


@pytest.mark.parametrize("width,height", MEASURED_FIXTURE_SIZES)
def test_every_bp_value_resolves_to_the_retired_constant_at_each_fixture_size(width, height):
    """The load-bearing claim: at each measured fixture page size, every
    resolved threshold equals what the retired hardcoded constant was.
    """
    config = load_grouping_config()
    resolved = resolve_thresholds(config, width, height)

    assert resolved.margin_px == 30  # DEFAULT_MARGIN_FRACTION 0.15 * 200
    assert resolved.chain_gap_px == 6  # DEFAULT_CHAIN_GAP_PX
    assert resolved.anchor_reach_px == 2  # DEFAULT_ANCHOR_REACH_PX
    assert resolved.brace_min_height_px == 30  # DEFAULT_BRACE_MIN_HEIGHT_PX
    assert resolved.page_edge_reach_px == 4  # DEFAULT_PAGE_EDGE_REACH_PX
    assert (
        resolved.review_priority_min_dimension_px == 6
    )  # DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX
    assert resolved.fallback_overlap_px == 8  # DEFAULT_FALLBACK_OVERLAP_PX
    assert resolved.gap_tolerance_px == 3  # DEFAULT_GAP_TOLERANCE_PX, unconverted
    assert resolved.max_residual_components == 2000
    assert resolved.max_secondary_proposals == 2000
    assert resolved.fallback_bands == 4  # DEFAULT_FALLBACK_BANDS, unconverted


def test_resolve_thresholds_at_260_height_matches_the_spec_worked_arithmetic():
    """Pin the exact spec-worked numbers, independent of the fixture measurement above."""
    config = load_grouping_config()
    resolved = resolve_thresholds(config, width=200, height=260)
    assert resolved.margin_px == 30  # 200 * 1500 / 10000
    assert resolved.chain_gap_px == 6  # 260 * 231 / 10000 = 6.006 -> 6
    assert resolved.anchor_reach_px == 2  # 260 * 77 / 10000 = 2.002 -> 2
    assert resolved.brace_min_height_px == 30  # 260 * 1154 / 10000 = 30.004 -> 30
    assert resolved.page_edge_reach_px == 4  # 260 * 154 / 10000 = 4.004 -> 4
    assert resolved.review_priority_min_dimension_px == 6  # 260 * 231 / 10000 = 6.006 -> 6
    assert resolved.fallback_overlap_px == 8  # 260 * 308 / 10000 = 8.008 -> 8


def test_resolve_thresholds_returns_a_frozen_dataclass_of_plain_ints():
    config = load_grouping_config()
    resolved = resolve_thresholds(config, width=200, height=260)
    assert isinstance(resolved, GroupingThresholds)
    with pytest.raises(FrozenInstanceError):
        resolved.margin_px = 999  # frozen -- reassignment must refuse
    for value in (
        resolved.margin_px,
        resolved.chain_gap_px,
        resolved.anchor_reach_px,
        resolved.brace_min_height_px,
        resolved.page_edge_reach_px,
        resolved.review_priority_min_dimension_px,
        resolved.fallback_overlap_px,
        resolved.gap_tolerance_px,
        resolved.max_residual_components,
        resolved.max_secondary_proposals,
        resolved.fallback_bands,
    ):
        assert isinstance(value, int) and not isinstance(value, bool)


def test_resolve_thresholds_breaks_an_exact_half_up_not_by_truncation(tmp_path):
    """A tie case: 2 * 2500 / 10000 = 0.5, half-up 1, truncation 0.

    Every case in the two tests above lands just above its integer, so floor
    division and round-half-up agree on all of them -- a `resolve_thresholds`
    rewritten to use plain integer truncation (`dimension * bp //
    BP_DENOMINATOR`) instead of calling `geometry._pad_amount` would still
    pass every assertion above it in this file. This tie is the one case that
    tells the two rules apart, which is the whole point of routing every
    resolution through `_pad_amount` instead of writing a second rounding
    rule (see this module's own docstring).
    """
    body = _valid_toml().replace("chain_gap_bp = 231", "chain_gap_bp = 2500")
    path = _write(tmp_path, body)
    config = load_grouping_config(path)
    resolved = resolve_thresholds(config, width=200, height=2)
    assert resolved.chain_gap_px == 1  # half-up; truncation would give 0


def test_resolve_thresholds_refuses_non_positive_dimensions():
    config = load_grouping_config()
    with pytest.raises(ContractError):
        resolve_thresholds(config, width=0, height=260)
    with pytest.raises(ContractError):
        resolve_thresholds(config, width=200, height=-1)


# --- closed schema: unknown / missing / forbidden ---------------------------


def _write(tmp_path, body: str) -> Path:
    path = tmp_path / "grouping.toml"
    path.write_text(body)
    return path


_VALID_PAGE_FRACTION = """\
margin_bp = 1500
chain_gap_bp = 231
anchor_reach_bp = 77
brace_min_height_bp = 1154
page_edge_reach_bp = 154
review_priority_min_dimension_bp = 231
fallback_overlap_bp = 308
"""

_VALID_PROVENANCE = """\
source = "s"
corpus = "c"
sample_unit = "u"
sample_count = 0
statistic = "st"
calibrated_for_this_corpus = false
caveat = "cv"
"""


# `[grouping.background]`'s own provenance block, written with TOML *literal*
# (single-quoted) strings and values that differ from `_VALID_PROVENANCE`'s in
# every field. That is deliberate: the tests below mutate the file-level
# provenance by string replacement (`caveat = "cv"`, `sample_count = 0`,
# `source = "`), and with two provenance blocks in one file a shared spelling
# would make those replacements hit whichever came first. Distinct spellings
# keep each test aimed at the block it names.
_VALID_BACKGROUND = """\
band_bp = 500
max_interior_dark_bp = 5000
max_ink_bp = 7000
ink_margin_bp = 3333

[grouping.background.provenance]
source = 's'
corpus = 'c'
sample_unit = 'u'
sample_count = 7
statistic = 'st'
calibrated_for_this_corpus = true
caveat = 'scv'
"""


def _valid_toml() -> str:
    # `[grouping.provenance]` stays last: several tests below append a line to
    # the end of this document to put a field inside it.
    return (
        "[grouping]\n"
        "max_residual_components = 2000\n"
        "max_secondary_proposals = 2000\n"
        "fallback_bands = 4\n\n"
        "[grouping.page_fraction_bp]\n" + _VALID_PAGE_FRACTION + "\n"
        "[grouping.absolute]\n"
        "gap_tolerance_px = 3\n\n"
        "[grouping.background]\n" + _VALID_BACKGROUND + "\n"
        "[grouping.provenance]\n" + _VALID_PROVENANCE
    )


def test_valid_synthetic_toml_round_trips(tmp_path):
    path = _write(tmp_path, _valid_toml())
    config = load_grouping_config(path)
    assert config["max_residual_components"] == 2000
    assert config["absolute"] == {"gap_tolerance_px": 3}


def test_unknown_top_level_field_refused(tmp_path):
    path = _write(tmp_path, _valid_toml() + "\n[bogus]\nx = 1\n")
    with pytest.raises(ContractError, match="unknown top-level field"):
        load_grouping_config(path)


def test_unknown_grouping_field_refused(tmp_path):
    body = _valid_toml().replace(
        "max_residual_components = 2000", "max_residual_components = 2000\nbogus_field = 1"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_missing_grouping_field_refused(tmp_path):
    body = _valid_toml().replace("max_residual_components = 2000\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


@pytest.mark.parametrize("forbidden", ["primary_margin", "secondary_margin"])
def test_forbidden_margin_names_refused_at_grouping_top_level(tmp_path, forbidden):
    body = _valid_toml().replace(
        "max_residual_components = 2000",
        f"max_residual_components = 2000\n{forbidden} = 20",
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=forbidden):
        load_grouping_config(path)


@pytest.mark.parametrize("forbidden", ["primary_margin", "secondary_margin"])
def test_forbidden_margin_names_refused_inside_page_fraction_bp(tmp_path, forbidden):
    body = _valid_toml().replace(_VALID_PAGE_FRACTION, _VALID_PAGE_FRACTION + f"{forbidden} = 20\n")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=forbidden):
        load_grouping_config(path)


@pytest.mark.parametrize("forbidden", ["primary_margin", "secondary_margin"])
def test_forbidden_margin_names_refused_inside_absolute(tmp_path, forbidden):
    body = _valid_toml().replace("gap_tolerance_px = 3", f"gap_tolerance_px = 3\n{forbidden} = 20")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=forbidden):
        load_grouping_config(path)


def test_value_in_wrong_sub_table_refused(tmp_path):
    # gap_tolerance_px belongs in [grouping.absolute], not [grouping.page_fraction_bp].
    body = _valid_toml().replace(
        _VALID_PAGE_FRACTION, _VALID_PAGE_FRACTION + "gap_tolerance_px = 3\n"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_margin_bp_in_absolute_table_refused(tmp_path):
    body = _valid_toml().replace("gap_tolerance_px = 3", "gap_tolerance_px = 3\nmargin_bp = 1500")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_missing_page_fraction_bp_field_refused(tmp_path):
    body = _valid_toml().replace("margin_bp = 1500\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_page_fraction_bp_field_present_but_not_a_table_refused(tmp_path):
    # Field present (so the top-level missing-field check does not fire
    # first) but a scalar, not a table -- exercises the sub-loader's own
    # "no table" refusal.
    body = (
        _valid_toml()
        .replace("[grouping.page_fraction_bp]\n" + _VALID_PAGE_FRACTION, "")
        .replace(
            "max_residual_components = 2000", "max_residual_components = 2000\npage_fraction_bp = 1"
        )
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="no \\[grouping.page_fraction_bp\\] table"):
        load_grouping_config(path)


def test_absolute_field_present_but_not_a_table_refused(tmp_path):
    body = (
        _valid_toml()
        .replace("[grouping.absolute]\ngap_tolerance_px = 3\n\n", "")
        .replace("max_residual_components = 2000", "max_residual_components = 2000\nabsolute = 1")
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="no \\[grouping.absolute\\] table"):
        load_grouping_config(path)


def test_provenance_field_present_but_not_a_table_refused(tmp_path):
    body = (
        _valid_toml()
        .replace("[grouping.provenance]\n" + _VALID_PROVENANCE, "")
        .replace("max_residual_components = 2000", "max_residual_components = 2000\nprovenance = 1")
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="no \\[grouping.provenance\\] table"):
        load_grouping_config(path)


def test_missing_page_fraction_bp_table_refused_as_missing_field(tmp_path):
    body = _valid_toml().replace("[grouping.page_fraction_bp]\n" + _VALID_PAGE_FRACTION, "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_missing_absolute_table_refused_as_missing_field(tmp_path):
    body = _valid_toml().replace("[grouping.absolute]\ngap_tolerance_px = 3\n\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_missing_provenance_table_refused_as_missing_field(tmp_path):
    body = _valid_toml().replace("[grouping.provenance]\n" + _VALID_PROVENANCE, "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


# --- provenance schema -------------------------------------------------------


def test_provenance_unknown_field_refused(tmp_path):
    body = _valid_toml() + 'bogus = "x"\n'
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_provenance_missing_field_refused(tmp_path):
    body = _valid_toml().replace('caveat = "cv"\n', "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_provenance_non_bool_calibrated_flag_refused(tmp_path):
    body = _valid_toml().replace(
        "calibrated_for_this_corpus = false", 'calibrated_for_this_corpus = "false"'
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="calibrated_for_this_corpus"):
        load_grouping_config(path)


def test_provenance_empty_string_field_refused(tmp_path):
    body = _valid_toml().replace('caveat = "cv"', 'caveat = "   "')
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="caveat"):
        load_grouping_config(path)


def test_provenance_negative_sample_count_refused(tmp_path):
    body = _valid_toml().replace("sample_count = 0", "sample_count = -1")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="sample_count"):
        load_grouping_config(path)


def test_provenance_refuses_calibrated_claim_with_zero_samples(tmp_path):
    body = _valid_toml().replace(
        "calibrated_for_this_corpus = false", "calibrated_for_this_corpus = true"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="calibrated_for_this_corpus.*sample_count is zero"):
        load_grouping_config(path)


def test_background_provenance_refuses_calibrated_claim_with_zero_samples(tmp_path):
    body = _valid_toml().replace("sample_count = 7", "sample_count = 0")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="calibrated_for_this_corpus.*sample_count is zero"):
        load_grouping_config(path)


def test_string_provenance_fields_are_derived_from_the_whole_schema():
    """The two field sets partition `_PROVENANCE_FIELDS` exactly.

    A field added to `_PROVENANCE_FIELDS` without being added to
    `_TYPED_PROVENANCE_FIELDS` lands here automatically instead of being
    silently unchecked -- this is the module-import assertion, exercised
    again here so a failure shows as a named test rather than a collection
    error.
    """
    assert set(_STRING_PROVENANCE_FIELDS) | _TYPED_PROVENANCE_FIELDS == set(_PROVENANCE_FIELDS)
    assert set(_STRING_PROVENANCE_FIELDS).isdisjoint(_TYPED_PROVENANCE_FIELDS)


@pytest.mark.parametrize("field", ["source", "corpus", "sample_unit", "statistic", "caveat"])
def test_every_string_provenance_field_is_actually_type_checked(tmp_path, field):
    """Each field the schema calls a string is refused when it is not one.

    Regression for a loader that validates a hand-copied subset of
    `_PROVENANCE_FIELDS`: a field present in the schema but missing from
    that subset would pass through this loop unchecked while still being
    accepted as present.
    """
    body = _valid_toml().replace(f'{field} = "', f"{field} = 7 #", 1)
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=field):
        load_grouping_config(path)


# --- type/value validation ---------------------------------------------------


def test_float_in_absolute_refused(tmp_path):
    body = _valid_toml().replace("gap_tolerance_px = 3", "gap_tolerance_px = 3.0")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_float_in_page_fraction_bp_refused(tmp_path):
    body = _valid_toml().replace("margin_bp = 1500", "margin_bp = 1500.5")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_negative_max_residual_components_refused(tmp_path):
    body = _valid_toml().replace("max_residual_components = 2000", "max_residual_components = -1")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_bool_max_residual_components_refused(tmp_path):
    # bool is an int subclass in Python; _is_plain_int must reject it explicitly.
    body = _valid_toml().replace("max_residual_components = 2000", "max_residual_components = true")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


@pytest.mark.parametrize("value", ["0", "-1", "true", "4.0"])
def test_a_fallback_band_count_that_cuts_nothing_is_refused(tmp_path, value):
    """The one count with a floor of one, and the reason it has one.

    Zero bands is a page the structure pass found nothing on reaching the
    witnesses as no crop at all -- the loss Tyrel's predetermined-crop ruling
    exists to prevent -- so this field refuses at the loader rather than
    resolving to a grid that cuts nothing.
    """
    body = _valid_toml().replace("fallback_bands = 4", f"fallback_bands = {value}")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="fallback_bands is not a positive integer"):
        load_grouping_config(path)


def test_negative_max_secondary_proposals_refused(tmp_path):
    body = _valid_toml().replace("max_secondary_proposals = 2000", "max_secondary_proposals = -1")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_negative_page_fraction_bp_value_refused(tmp_path):
    body = _valid_toml().replace("anchor_reach_bp = 77", "anchor_reach_bp = -1")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_unreadable_path_refused(tmp_path):
    with pytest.raises(ContractError, match="could not be read"):
        load_grouping_config(tmp_path / "does-not-exist.toml")


def test_malformed_toml_refused(tmp_path):
    path = _write(tmp_path, "not [ valid toml")
    with pytest.raises(ContractError, match="could not be read"):
        load_grouping_config(path)


def test_non_table_top_level_refused(tmp_path):
    # A TOML document whose [grouping] value is a list, not a table.
    path = _write(tmp_path, "grouping = [1, 2, 3]\n")
    with pytest.raises(ContractError, match="no \\[grouping\\] table"):
        load_grouping_config(path)


# --- [grouping.background]: the one measured block, and its own closed schema ----


def test_background_resolves_both_bands_from_the_page_it_is_given():
    """`band_bp` is the one field in this file that resolves against *both*
    dimensions -- the band is a frame, so it is `band_bp` of the width on the
    left and right and `band_bp` of the height on top and bottom. That is why it
    cannot live in `page_fraction_bp`, whose contract is one declared basis per
    field, and why it is resolved by its own function.
    """
    config = load_grouping_config()
    assert resolve_background_policy(config, 200, 260) == {
        "band_px_x": 10,  # round-half-up 5% of 200
        "band_px_y": 13,  # round-half-up 5% of 260
        "max_interior_dark_bp": 5000,
        "max_ink_bp": 7000,
        # A fraction of the distance between the page's own two population
        # modes, so it resolves to itself: unlike `band_bp` it has no page
        # dimension to be a fraction of.
        "ink_margin_bp": 3333,
    }
    # A real photographed proxy's size, resolved the way a run would resolve it.
    assert resolve_background_policy(config, 1484, 1103)["band_px_x"] == 74
    assert resolve_background_policy(config, 1484, 1103)["band_px_y"] == 55


def test_background_is_not_a_field_of_the_published_resolved_thresholds():
    """Deliberate, and load-bearing for the fixture pins.

    `run.py` publishes the whole `GroupingThresholds` as a page's
    `resolved_thresholds`. The background policy answers a question asked strictly
    before that record exists -- the background inference runs before any
    threshold touches any geometry -- so folding it in would put a
    background-inference input into the structure pass's published geometry and
    move every existing page record's bytes for a value that pass never used.
    """
    resolved = resolve_thresholds(load_grouping_config(), 200, 260)
    assert not hasattr(resolved, "background_policy")
    assert not hasattr(resolved, "surround_policy")
    assert "surround" not in dataclasses.asdict(
        resolve_thresholds(load_grouping_config(), 200, 260)
    )


def test_missing_background_table_refused_as_missing_field(tmp_path):
    body = _valid_toml().replace("[grouping.background]\n" + _VALID_BACKGROUND + "\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_background_field_present_but_not_a_table_refused(tmp_path):
    body = (
        _valid_toml()
        .replace("[grouping.background]\n" + _VALID_BACKGROUND + "\n", "")
        .replace("max_residual_components = 2000", "max_residual_components = 2000\nbackground = 1")
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="no \\[grouping.background\\] table"):
        load_grouping_config(path)


def test_background_unknown_field_refused(tmp_path):
    body = _valid_toml().replace("band_bp = 500", "band_bp = 500\nbogus_bp = 1")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_background_missing_field_refused(tmp_path):
    body = _valid_toml().replace("max_ink_bp = 7000\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_background_missing_its_own_provenance_refused(tmp_path):
    """Two provenance blocks, both required. The file-level one describes
    unmeasured defaults with `sample_count = 0`; this one describes three values
    measured on 127 real pages. One block could not say both truthfully."""
    body = _valid_toml().replace(
        "[grouping.background.provenance]\nsource = 's'\n", "[grouping.background.provenance]\n"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


def test_background_provenance_is_held_to_the_same_closed_schema(tmp_path):
    body = _valid_toml().replace("sample_count = 7", "sample_count = -1")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="sample_count"):
        load_grouping_config(path)


@pytest.mark.parametrize("bad", ["0", "5000", "9000", "-1", "500.0", "true"])
def test_a_band_that_leaves_no_border_or_no_interior_is_refused(tmp_path, bad):
    """Both ends refused by the loader rather than silently disarming the test.

    `structure._dark_distribution` returns `None` for a band with no border to
    measure or no interior to compare it against, and a page would then refuse
    for a reason no config line stated. The refusal belongs here, where the
    number is written.
    """
    body = _valid_toml().replace("band_bp = 500", f"band_bp = {bad}")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="band_bp"):
        load_grouping_config(path)


@pytest.mark.parametrize("field", ["max_interior_dark_bp", "max_ink_bp"])
@pytest.mark.parametrize("bad", ["-1", "10001", "0.5", "true"])
def test_a_population_fraction_outside_zero_to_one_is_refused(tmp_path, field, bad):
    current = {"max_interior_dark_bp": "5000", "max_ink_bp": "7000"}[field]
    body = _valid_toml().replace(f"{field} = {current}", f"{field} = {bad}")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=field):
        load_grouping_config(path)


@pytest.mark.parametrize("bad", ["-1", "10001", "0.5", "true"])
def test_an_ink_margin_fraction_outside_zero_to_one_is_refused(tmp_path, bad):
    body = _valid_toml().replace("ink_margin_bp = 3333", f"ink_margin_bp = {bad}")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="ink_margin_bp"):
        load_grouping_config(path)


@pytest.mark.parametrize("bad", ["0", "5000", "5001", "9999"])
def test_an_ink_margin_fraction_at_or_past_half_its_range_is_refused(tmp_path, bad):
    """The bound on `ink_margin_bp` is structural, not a matter of taste.

    Zero derives no margin at all and leaves every page on
    `structure.PRIMARY_MARGIN`, which is the derivation switched off by a value.
    At 5000 the derived ink threshold coincides with the level
    `structure._dark_distribution` measures at, and past it the threshold falls
    below that level -- at which point the dark-distribution counts stop being
    subsets of the ink they are published beside.
    """
    body = _valid_toml().replace("ink_margin_bp = 3333", f"ink_margin_bp = {bad}")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="strictly between 0 and 5000"):
        load_grouping_config(path)


def test_the_sealed_ink_margin_fraction_is_under_half_its_range():
    """The shipped value satisfies the bound above, checked on the shipped file.

    The parametrized refusals prove the loader stops a bad value; this proves
    the value in `config/designator_grouping.toml` is not one, which is a
    different claim and the one a run depends on.
    """
    config = load_grouping_config()
    assert 0 < config["background"]["ink_margin_bp"] < 5000


@pytest.mark.parametrize(
    "field,current", [("max_interior_dark_bp", "5000"), ("max_ink_bp", "7000")]
)
def test_a_bound_at_the_top_of_its_range_is_refused_as_the_test_switched_off(
    tmp_path, field, current
):
    """Both bounds refuse, and a bound of 10000 basis points refuses nothing.

    `max_interior_dark_bp = 10000` admits an inverted scan and a page of solid
    dark alike; `max_ink_bp = 10000` admits a background that leaves the whole
    page as ink. Either is the test turned off by a value rather than by a
    decision, and this policy is sealed into a run as something that decides.
    """
    body = _valid_toml().replace(f"{field} = {current}", f"{field} = 10000")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="which refuses nothing"):
        load_grouping_config(path)
