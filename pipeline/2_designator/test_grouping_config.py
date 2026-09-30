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
from pathlib import Path

import grouping_config
import pytest
from grouping_config import (
    _PROVENANCE_FIELDS,
    _STRING_PROVENANCE_FIELDS,
    _TYPED_PROVENANCE_FIELDS,
    DEFAULT_GROUPING_CONFIG_PATH,
    resolve_background_policy,
    resolve_thresholds,
)

from common.contracts.errors import ContractError
from common.imaging import dimensions
from common.sealed_config import read_sealed_toml

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "proof" / "fixtures" / "synthetic-two-page-v0"

# The retired module-level constants this file's basis-point fields replace.
_RETIRED = {
    "margin_bp": ("width", 0.15),  # grouping.DEFAULT_MARGIN_FRACTION -- a fraction, not px
    "chain_gap_bp": ("height", 6),  # grouping.DEFAULT_CHAIN_GAP_PX
    "anchor_reach_bp": ("height", 2),  # grouping.DEFAULT_ANCHOR_REACH_PX
    "brace_min_height_bp": ("height", 30),  # grouping.DEFAULT_BRACE_MIN_HEIGHT_PX
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
    """Every fixture page is 200x260; if this ever fails, every bit-identity
    assertion below is computed against the wrong size and must be redone.
    """
    assert MEASURED_FIXTURE_SIZES == [(200, 260), (200, 260), (200, 260)]


# --- load_grouping_config: happy path ---------------------------------------


def test_default_config_loads_and_carries_the_seal_of_its_own_file():
    config = load_grouping_config()

    assert config["config_sha256"] == read_sealed_toml(DEFAULT_GROUPING_CONFIG_PATH, "x")[1]
    assert config["max_secondary_proposals"] == 2000
    assert config["fallback_bands"] == 4
    assert set(config["page_fraction_bp"]) == set(_RETIRED)
    assert config["connectivity"]["gap_tolerance_px"] == 3
    assert config["connectivity"]["provenance"]["calibrated_for_this_corpus"] is False
    assert config["provenance"]["calibrated_for_this_corpus"] is False
    assert config["background"]["band_bp"] == 500
    assert config["background"]["max_interior_dark_bp"] == 5000
    assert config["background"]["max_ink_bp"] == 7000
    assert config["background"]["ink_margin_bp"] == 3333
    # Pinned so a later edit cannot quietly widen the calibration claim.
    assert config["background"]["provenance"]["calibrated_for_this_corpus"] is True
    assert config["background"]["provenance"]["sample_count"] == 127
    assert config["page_spanning"]["page_spanning_area_bp"] == 5000
    assert config["page_spanning"]["provenance"]["calibrated_for_this_corpus"] is True
    assert config["page_spanning"]["provenance"]["sample_count"] == 17
    assert config["continuation"]["page_edge_reach_bp"] == 280
    assert config["continuation"]["provenance"]["calibrated_for_this_corpus"] is True
    assert config["continuation"]["provenance"]["sample_count"] == 44
    # [coverage_audit] isn't this stage's policy -- it's the outside-coverage
    # audit's, validated here so a malformed one is refused before any stage runs.
    assert config["coverage_audit"]["substantial_ink_area_bp"] == 4
    assert config["coverage_audit"]["edge_band_bp"] == 100
    assert config["coverage_audit"]["provenance"]["calibrated_for_this_corpus"] is True
    assert config["coverage_audit"]["provenance"]["sample_count"] == 44
    # The noise floor's own, truthfully unmeasured, provenance.
    assert config["coverage_audit"]["minimum_ink_pixels"] == 24
    assert config["coverage_audit"]["minimum_fraction_outside_bp"] == 200
    assert config["coverage_audit"]["noise_floor_provenance"]["calibrated_for_this_corpus"] is False
    assert config["coverage_audit"]["noise_floor_provenance"]["sample_count"] == 0


# --- resolve_thresholds: bit-identity to the retired constants -------------


@pytest.mark.parametrize("width,height", MEASURED_FIXTURE_SIZES)
def test_every_bp_value_resolves_to_the_retired_constant_at_each_fixture_size(width, height):
    """At each measured fixture page size, every resolved threshold equals
    what the retired hardcoded constant was -- except `page_edge_reach_px`,
    re-measured on 44 real pages and asserted separately below.
    """
    config = load_grouping_config()
    resolved = resolve_thresholds(config, width, height)

    assert resolved.margin_px == 30  # DEFAULT_MARGIN_FRACTION 0.15 * 200
    assert resolved.chain_gap_px == 6  # DEFAULT_CHAIN_GAP_PX
    assert resolved.anchor_reach_px == 2  # DEFAULT_ANCHOR_REACH_PX
    assert resolved.brace_min_height_px == 30  # DEFAULT_BRACE_MIN_HEIGHT_PX
    assert resolved.page_edge_reach_px == 7  # 260 * 280 / 10000 = 7.28 -> 7
    assert (
        resolved.review_priority_min_dimension_px == 6
    )  # DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX
    assert resolved.fallback_overlap_px == 8  # DEFAULT_FALLBACK_OVERLAP_PX
    assert resolved.gap_tolerance_px == 3  # DEFAULT_GAP_TOLERANCE_PX, unconverted
    assert resolved.max_secondary_proposals == 2000
    assert resolved.fallback_bands == 4  # DEFAULT_FALLBACK_BANDS, unconverted
    # A fraction of the page's AREA, so it passes through unresolved: the
    # same 5000 at every fixture size.
    assert resolved.page_spanning_area_bp == 5000


def test_resolve_thresholds_breaks_an_exact_half_up_not_by_truncation(tmp_path):
    """A tie case: 2 * 2500 / 10000 = 0.5, half-up 1, truncation 0.

    Every case above lands just above its integer, so floor division and
    round-half-up would agree on all of them; only this tie tells the two
    rules apart.
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


def load_grouping_config(path: Path | None = None) -> dict:
    """The loader over a written grouping file and the ink-map file written beside it."""
    if path is None:
        return grouping_config.load_grouping_config()
    return grouping_config.load_grouping_config(path, path.with_name("ink_map.toml"))


_INK_MAP_HEADS = ("[background", "[coverage_audit", "[page_spanning", "[connectivity")


def _write(tmp_path, body: str) -> Path:
    """Write a synthetic document as the two files the stage reads.

    The `[background]`, `[coverage_audit]`, `[page_spanning]` and
    `[connectivity]` tables go to an ink-map file, everything else to the
    grouping file.
    """
    grouping, ink_map, target = [], [], None
    for line in body.split("\n"):
        if line.startswith("["):
            target = ink_map if line.startswith(_INK_MAP_HEADS) else grouping
        (target if target is not None else grouping).append(line)
    (tmp_path / "ink_map.toml").write_text("\n".join(ink_map))
    path = tmp_path / "grouping.toml"
    path.write_text("\n".join(grouping))
    return path


_VALID_PAGE_FRACTION = """\
margin_bp = 1500
chain_gap_bp = 231
anchor_reach_bp = 77
brace_min_height_bp = 1154
review_priority_min_dimension_bp = 231
fallback_overlap_bp = 308
"""

# `[grouping.continuation]`'s own provenance block, a distinct spelling for
# the reason `_VALID_BACKGROUND`'s comment gives.
_VALID_CONTINUATION = """\
page_edge_reach_bp = 280

[grouping.continuation.provenance]
source = 'ks'
corpus = 'kc'
sample_unit = 'ku'
sample_count = 44
statistic = 'kst'
calibrated_for_this_corpus = true
caveat = 'kcv'
"""

# [coverage_audit] is a TOP-LEVEL table, not one of [grouping]'s: it's the
# outside-coverage audit's sealed policy, validated here and applied nowhere
# in this stage. Written between [grouping]'s bare keys and its sub-tables
# because several tests below append a line to the END of the document to
# put a field inside [grouping.provenance], which must therefore stay last.
_VALID_COVERAGE_AUDIT = """\
substantial_ink_area_bp = 4
edge_band_bp = 100

[coverage_audit.provenance]
source = 'vs'
corpus = 'vc'
sample_unit = 'vu'
sample_count = 44
statistic = 'vst'
calibrated_for_this_corpus = true
caveat = 'vcv'

[coverage_audit.noise_floor]
minimum_ink_pixels = 24
minimum_fraction_outside_bp = 200

[coverage_audit.noise_floor.provenance]
source = 'ns'
corpus = 'nc'
sample_unit = 'nu'
sample_count = 0
statistic = 'nst'
calibrated_for_this_corpus = false
caveat = 'ncv'
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


# [background]'s own provenance block, deliberately spelled with
# different literal values than _VALID_PROVENANCE's in every field: the tests
# below mutate one block by string replacement, and a shared spelling would
# make that replacement hit whichever block came first.
_VALID_BACKGROUND = """\
band_bp = 500
max_interior_dark_bp = 5000
max_ink_bp = 7000
ink_margin_bp = 3333

[background.provenance]
source = 's'
corpus = 'c'
sample_unit = 'u'
sample_count = 7
statistic = 'st'
calibrated_for_this_corpus = true
caveat = 'scv'
"""


# The ink map's [page_spanning] and [connectivity], each with its own
# provenance block, distinctly spelled for the reason _VALID_BACKGROUND's
# comment gives.
_VALID_CONNECTIVITY = """\
gap_tolerance_px = 3

[connectivity.provenance]
source = 'gs'
corpus = 'gc'
sample_unit = 'gu'
sample_count = 0
statistic = 'gst'
calibrated_for_this_corpus = false
caveat = 'gcv'
"""

_VALID_PAGE_SPANNING = """\
page_spanning_area_bp = 5000

[page_spanning.provenance]
source = 'as'
corpus = 'ac'
sample_unit = 'au'
sample_count = 17
statistic = 'ast'
calibrated_for_this_corpus = true
caveat = 'acv'
"""


def _valid_toml() -> str:
    # `[grouping.provenance]` stays last: several tests below append a line to
    # the end of this document to put a field inside it.
    return (
        "[grouping]\n"
        "max_secondary_proposals = 2000\n"
        "fallback_bands = 4\n\n"
        "[grouping.residual_presentation]\n"
        "residual_aggregate_max_pixel_count = 500\n"
        "residual_aggregate_max_area_px = 2000\n\n"
        "[grouping.residual_presentation.provenance]\n"
        "source = 'operational'\ncorpus = 'none'\nsample_unit = 'page'\n"
        "sample_count = 0\nstatistic = 'none'\n"
        "calibrated_for_this_corpus = false\ncaveat = 'unmeasured'\n\n"
        "[coverage_audit]\n" + _VALID_COVERAGE_AUDIT + "\n"
        "[grouping.page_fraction_bp]\n" + _VALID_PAGE_FRACTION + "\n"
        "[grouping.continuation]\n" + _VALID_CONTINUATION + "\n"
        "[page_spanning]\n" + _VALID_PAGE_SPANNING + "\n"
        "[connectivity]\n" + _VALID_CONNECTIVITY + "\n"
        "[background]\n" + _VALID_BACKGROUND + "\n"
        "[grouping.provenance]\n" + _VALID_PROVENANCE
    )


def test_valid_synthetic_toml_round_trips(tmp_path):
    path = _write(tmp_path, _valid_toml())
    config = load_grouping_config(path)
    assert config["max_secondary_proposals"] == 2000
    assert config["connectivity"]["gap_tolerance_px"] == 3


def test_unknown_top_level_field_refused(tmp_path):
    path = _write(tmp_path, _valid_toml() + "\n[bogus]\nx = 1\n")
    with pytest.raises(ContractError, match="unknown top-level field"):
        load_grouping_config(path)


def test_unknown_grouping_field_refused(tmp_path):
    body = _valid_toml().replace(
        "max_secondary_proposals = 2000", "max_secondary_proposals = 2000\nbogus_field = 1"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_the_retired_residual_component_count_is_refused_as_unknown(tmp_path):
    body = _valid_toml().replace(
        "max_secondary_proposals = 2000",
        "max_secondary_proposals = 2000\nmax_residual_components = 2000",
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_missing_grouping_field_refused(tmp_path):
    body = _valid_toml().replace("max_secondary_proposals = 2000\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="missing field"):
        load_grouping_config(path)


@pytest.mark.parametrize("forbidden", ["primary_margin", "secondary_margin"])
def test_forbidden_margin_names_refused_at_grouping_top_level(tmp_path, forbidden):
    body = _valid_toml().replace(
        "max_secondary_proposals = 2000",
        f"max_secondary_proposals = 2000\n{forbidden} = 20",
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


@pytest.mark.parametrize(
    "table",
    [
        "[grouping.absolute]\ngap_tolerance_px = 3",
        "[grouping.page_area_bp]\npage_spanning_area_bp = 5000",
    ],
)
def test_a_grouping_copy_of_an_ink_map_bound_is_refused(tmp_path, table):
    """The page-spanning bound and the connectivity radius live only in the ink map."""
    body = _valid_toml().replace(
        "[grouping.provenance]\n", table + "\n\n[grouping.provenance]\n", 1
    )
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(_write(tmp_path, body))


def test_value_in_wrong_sub_table_refused(tmp_path):
    # gap_tolerance_px is the ink map's [connectivity], never a page fraction.
    body = _valid_toml().replace(
        _VALID_PAGE_FRACTION, _VALID_PAGE_FRACTION + "gap_tolerance_px = 3\n"
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
        load_grouping_config(path)


def test_margin_bp_in_the_connectivity_table_refused(tmp_path):
    body = _valid_toml().replace("gap_tolerance_px = 3", "gap_tolerance_px = 3\nmargin_bp = 1500")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="not exactly gap_tolerance_px"):
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
            "max_secondary_proposals = 2000", "max_secondary_proposals = 2000\npage_fraction_bp = 1"
        )
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="no \\[grouping.page_fraction_bp\\] table"):
        load_grouping_config(path)


def test_provenance_field_present_but_not_a_table_refused(tmp_path):
    body = (
        _valid_toml()
        .replace("[grouping.provenance]\n" + _VALID_PROVENANCE, "")
        .replace("max_secondary_proposals = 2000", "max_secondary_proposals = 2000\nprovenance = 1")
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="no \\[grouping.provenance\\] table"):
        load_grouping_config(path)


def test_missing_page_fraction_bp_table_refused_as_missing_field(tmp_path):
    body = _valid_toml().replace("[grouping.page_fraction_bp]\n" + _VALID_PAGE_FRACTION, "")
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
    # Scoped to `[grouping.provenance]` by its preceding line: the noise-floor
    # block below it truthfully says `false` with zero samples as well, and a
    # document-wide replacement would trip that block first.
    body = _valid_toml().replace(
        'statistic = "st"\ncalibrated_for_this_corpus = false',
        'statistic = "st"\ncalibrated_for_this_corpus = true',
    )
    assert body != _valid_toml()
    path = _write(tmp_path, body)
    with pytest.raises(
        ContractError,
        match=r"\[grouping\.provenance\].*calibrated_for_this_corpus.*sample_count is zero",
    ):
        load_grouping_config(path)


def test_background_provenance_refuses_calibrated_claim_with_zero_samples(tmp_path):
    body = _valid_toml().replace("sample_count = 7", "sample_count = 0")
    path = _write(tmp_path, body)
    with pytest.raises(
        ContractError,
        match=r"\[background\.provenance\].*calibrated_for_this_corpus.*sample_count is zero",
    ):
        load_grouping_config(path)


def test_string_provenance_fields_are_derived_from_the_whole_schema():
    """The two field sets partition `_PROVENANCE_FIELDS` exactly. The
    module-import assertion, exercised again here so a failure shows as a
    named test rather than a collection error.
    """
    assert set(_STRING_PROVENANCE_FIELDS) | _TYPED_PROVENANCE_FIELDS == set(_PROVENANCE_FIELDS)
    assert set(_STRING_PROVENANCE_FIELDS).isdisjoint(_TYPED_PROVENANCE_FIELDS)


@pytest.mark.parametrize("field", ["source", "corpus", "sample_unit", "statistic", "caveat"])
def test_every_string_provenance_field_is_actually_type_checked(tmp_path, field):
    """Each field the schema calls a string is refused when it is not one:
    a regression check for a loader that validated a hand-copied subset of
    `_PROVENANCE_FIELDS` instead of the whole schema.
    """
    body = _valid_toml().replace(f'{field} = "', f"{field} = 7 #", 1)
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=field):
        load_grouping_config(path)


# --- type/value validation ---------------------------------------------------


def test_a_float_connectivity_radius_refused(tmp_path):
    body = _valid_toml().replace("gap_tolerance_px = 3", "gap_tolerance_px = 3.0")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_float_in_page_fraction_bp_refused(tmp_path):
    body = _valid_toml().replace("margin_bp = 1500", "margin_bp = 1500.5")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


def test_bool_max_secondary_proposals_refused(tmp_path):
    # bool is an int subclass in Python; is_plain_int must reject it explicitly.
    body = _valid_toml().replace("max_secondary_proposals = 2000", "max_secondary_proposals = true")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="non-negative integer"):
        load_grouping_config(path)


@pytest.mark.parametrize("value", ["0", "-1", "true", "4.0"])
def test_a_fallback_band_count_that_cuts_nothing_is_refused(tmp_path, value):
    """The one count with a floor of one: zero bands would let a page reach
    the witnesses as no crop at all, so this field refuses at the loader
    rather than resolving to a grid that cuts nothing.
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
    with pytest.raises(ContractError, match="not valid TOML"):
        load_grouping_config(path)


def test_non_table_top_level_refused(tmp_path):
    # A TOML document whose [grouping] value is a list, not a table.
    path = _write(tmp_path, "grouping = [1, 2, 3]\n")
    with pytest.raises(ContractError, match="no \\[grouping\\] table"):
        load_grouping_config(path)


# --- [background]: the one measured block, and its own closed schema ----


def test_background_resolves_both_bands_from_the_page_it_is_given():
    """`band_bp` is the one field here that resolves against *both* dimensions
    (the band is a frame: `band_bp` of width on left/right, of height on
    top/bottom), so it can't live in `page_fraction_bp`.
    """
    config = load_grouping_config()
    assert resolve_background_policy(config, 200, 260) == {
        "band_px_x": 10,  # round-half-up 5% of 200
        "band_px_y": 13,  # round-half-up 5% of 260
        "max_interior_dark_bp": 5000,
        "max_ink_bp": 7000,
        # A fraction of a population distance, not a page dimension, so it
        # resolves to itself unlike band_bp.
        "ink_margin_bp": 3333,
    }
    assert resolve_background_policy(config, 1484, 1103)["band_px_x"] == 74
    assert resolve_background_policy(config, 1484, 1103)["band_px_y"] == 55


def test_background_is_not_a_field_of_the_published_resolved_thresholds():
    """Deliberate, and load-bearing for the fixture pins: the background
    policy answers a question asked strictly before `GroupingThresholds`
    exists, so folding it in would move every existing page record's bytes
    for a value the structure pass never used.
    """
    resolved = resolve_thresholds(load_grouping_config(), 200, 260)
    assert not hasattr(resolved, "background_policy")
    assert not hasattr(resolved, "surround_policy")
    assert "dark_distribution" not in dataclasses.asdict(
        resolve_thresholds(load_grouping_config(), 200, 260)
    )


def test_missing_background_table_refused_by_name(tmp_path):
    body = _valid_toml().replace("[background]\n" + _VALID_BACKGROUND + "\n", "")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=r"no \[background\] table"):
        load_grouping_config(path)


def test_background_field_present_but_not_a_table_refused(tmp_path):
    body = (
        _valid_toml()
        .replace("[background]\n" + _VALID_BACKGROUND + "\n", "")
        .replace("max_secondary_proposals = 2000", "max_secondary_proposals = 2000\nbackground = 1")
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="unknown field"):
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
    """Two provenance blocks, both required: the file-level one describes
    unmeasured defaults, this one describes measured real-page values, and
    one block could not say both truthfully."""
    body = _valid_toml().replace(
        "[background.provenance]\nsource = 's'\n", "[background.provenance]\n"
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
    """Both ends refused by the loader: `structure._dark_distribution` returns
    `None` for a band with no border or no interior to measure, and a page
    would then refuse for a reason no config line stated.
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
    """The bound on `ink_margin_bp` is structural, not a matter of taste: zero
    leaves every page on `structure.PRIMARY_MARGIN` (the derivation switched
    off), and past 5000 the derived threshold falls below the level
    `structure._dark_distribution` measures at, so its counts stop being
    subsets of the ink they're published beside.
    """
    body = _valid_toml().replace("ink_margin_bp = 3333", f"ink_margin_bp = {bad}")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="strictly between 0 and 5000"):
        load_grouping_config(path)


def test_the_sealed_ink_margin_fraction_is_under_half_its_range():
    """The shipped value satisfies the bound above, checked on the shipped
    file -- a different claim than the loader refusing a bad value, and the
    one a run actually depends on.
    """
    config = load_grouping_config()
    assert 0 < config["background"]["ink_margin_bp"] < 5000


@pytest.mark.parametrize(
    "field,current", [("max_interior_dark_bp", "5000"), ("max_ink_bp", "7000")]
)
def test_a_bound_at_the_top_of_its_range_is_refused_as_the_test_switched_off(
    tmp_path, field, current
):
    """A bound of 10000 basis points refuses nothing: `max_interior_dark_bp`
    would admit an inverted scan, `max_ink_bp` a background that leaves the
    whole page as ink -- the test turned off by a value, not a decision.
    """
    body = _valid_toml().replace(f"{field} = {current}", f"{field} = 10000")
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="which refuses nothing"):
        load_grouping_config(path)


# --- the ink map's [page_spanning]: the closed table and its bound -------------
#
# The loader's refusals are policy, not plumbing: a value this file admits
# decides which components the grouping pass may use as connective tissue.


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("page_spanning_area_bp = 5000", "page_spanning_area_bp = 5000\nbogus = 1"),
        ("page_spanning_area_bp = 5000\n", ""),
        ("page_spanning_area_bp = 5000", "page_spanning_area_bp = 5000\nprimary_margin = 20"),
        # Its fields left loose inside [page_spanning], which the closed set refuses.
        ("[page_spanning.provenance]\n", ""),
    ],
    ids=["unknown-field", "missing-bound", "forbidden-name", "no-provenance"],
)
def test_the_page_spanning_table_is_closed(tmp_path, old, new):
    body = _valid_toml().replace(old, new, 1)
    with pytest.raises(ContractError, match=r"\[page_spanning\] is not exactly"):
        load_grouping_config(_write(tmp_path, body))


@pytest.mark.parametrize("bad_value", ["0", "-1", "10001", "0.5", "true", "'5000'"])
def test_a_page_spanning_bound_outside_one_to_ten_thousand_is_refused(tmp_path, bad_value):
    """Closed at both ends: at or below zero every component on every page
    spans the bound and the pass withholds the whole page; past a whole page
    nothing can ever reach it.
    """
    body = _valid_toml().replace(
        "page_spanning_area_bp = 5000", f"page_spanning_area_bp = {bad_value}", 1
    )
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match="basis-point integer in 1..10000"):
        load_grouping_config(path)


def test_a_bound_of_a_whole_page_is_legal_and_is_the_top_of_the_range(tmp_path):
    """10000 means "only a component whose bounding box IS the page": legal
    rather than refused, since `>=` still withholds a whole-page component at
    10000, so the top of the range is the tightest the policy goes.
    """
    body = _valid_toml().replace("page_spanning_area_bp = 5000", "page_spanning_area_bp = 10000", 1)
    config = load_grouping_config(_write(tmp_path, body))
    assert config["page_spanning"]["page_spanning_area_bp"] == 10000
    assert resolve_thresholds(config, 200, 260).page_spanning_area_bp == 10000


def test_the_page_spanning_provenance_is_held_to_the_same_closed_schema(tmp_path):
    body = _valid_toml().replace("sample_count = 17\n", "", 1)
    path = _write(tmp_path, body)
    with pytest.raises(ContractError, match=r"page_spanning.provenance"):
        load_grouping_config(path)


def test_the_noise_floor_sub_table_must_carry_its_own_provenance(tmp_path):
    """The two noise-floor values do not inherit the gates' provenance block
    above them; a number with no declared source may not ship as a default.
    """
    block = "[coverage_audit.noise_floor.provenance]\n"
    body = _valid_toml()
    head, tail = body.split(block)
    without = head + tail.split("\n\n", 1)[1]
    assert block not in without and "[coverage_audit.noise_floor]" in without
    with pytest.raises(ContractError, match=r"no \[coverage_audit.noise_floor.provenance\] table"):
        load_grouping_config(_write(tmp_path, without))
    claimed = body.replace(
        "statistic = 'nst'\ncalibrated_for_this_corpus = false",
        "statistic = 'nst'\ncalibrated_for_this_corpus = true",
    )
    assert claimed != body
    with pytest.raises(ContractError, match=r"noise_floor.provenance\] says calibrated"):
        load_grouping_config(_write(tmp_path, claimed))


def test_the_page_spanning_bound_and_radius_are_the_ink_map_s(tmp_path):
    """The component this pass withholds is the one the coverage audit takes out."""
    body = (
        _valid_toml()
        .replace("page_spanning_area_bp = 5000", "page_spanning_area_bp = 6000", 1)
        .replace("gap_tolerance_px = 3", "gap_tolerance_px = 4", 1)
    )
    resolved = resolve_thresholds(load_grouping_config(_write(tmp_path, body)), 200, 260)
    assert (resolved.page_spanning_area_bp, resolved.gap_tolerance_px) == (6000, 4)
