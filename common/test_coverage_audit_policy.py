"""The sealed `[coverage_audit]` policy: what it refuses, and what it resolves to.

The two gates in `common/residual_ink.py` used to be flat pixel counts
and are fractions of the page now. This module holds the loader and
the resolver to the same shape `common/background.py`'s policy is held to: a
closed field set, both ends of both bounds refused by name, and a resolution
that is measurably proportional rather than merely renamed.
"""

import tomllib
from pathlib import Path

import pytest

from common.background import BASIS_POINTS
from common.contracts.errors import ContractError
from common.residual_ink import (
    COVERAGE_AUDIT_BP_FIELDS,
    COVERAGE_NOISE_FLOOR_FIELDS,
    COVERAGE_NOISE_FLOOR_TABLE,
    DEFAULT_COVERAGE_AUDIT_CONFIG_PATH,
    load_coverage_audit_config,
    resolve_coverage_audit_policy,
    validate_coverage_audit_table,
    validate_coverage_noise_floor_table,
)

ROOT = Path(__file__).resolve().parents[1]

# The shipped noise floor and fraction gate, and a well-formed `[coverage_audit]`
# table to vary one field of at a time. Both values used to be module constants
# (`MINIMUM_INK_PIXELS = 24`, `MINIMUM_FRACTION_OUTSIDE_COVERAGE =
# 0.02`) and are sealed in their own sub-table now, with their own provenance,
# so the calibration claim over the two gates is not read as covering them.
NOISE_FLOOR = {"minimum_ink_pixels": 24, "minimum_fraction_outside_bp": 200}
GATES = {"substantial_ink_area_bp": 4, "edge_band_bp": 100}
SEALED_VALUES = {**GATES, **NOISE_FLOOR}


def _table(**changes):
    table = {**GATES, "noise_floor": dict(NOISE_FLOOR)}
    for name, value in changes.items():
        (table["noise_floor"] if name in NOISE_FLOOR else table)[name] = value
    return table


def _sealed() -> dict:
    return load_coverage_audit_config()


def test_the_shipped_block_loads_with_the_digest_of_its_own_bytes():
    config = _sealed()
    from common.contracts.canonical import digest_bytes

    assert config["config_sha256"] == digest_bytes(DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_bytes())
    assert config["coverage_audit"] == SEALED_VALUES
    # The two Designator fields this audit resolves beside its own, out of the
    # same file: the component it takes out of its counts must be the component
    # that stage withheld, which is true only while both read one number.
    assert config["page_spanning_area_bp"] == 5000
    assert config["gap_tolerance_px"] == 3


def test_the_block_is_read_from_the_file_the_designator_is_sealed_under():
    """One file, one digest, one seal -- not a second sealed name for two gates."""
    assert DEFAULT_COVERAGE_AUDIT_CONFIG_PATH == ROOT / "config" / "designator_grouping.toml"
    raw = tomllib.loads(DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_bytes().decode("utf-8"))
    assert set(raw["coverage_audit"]) == set(COVERAGE_AUDIT_BP_FIELDS) | {
        "provenance",
        COVERAGE_NOISE_FLOOR_TABLE,
    }
    assert set(raw["coverage_audit"][COVERAGE_NOISE_FLOOR_TABLE]) == set(
        COVERAGE_NOISE_FLOOR_FIELDS
    ) | {"provenance"}


def test_the_noise_floor_carries_its_own_unmeasured_provenance():
    """The two gates are measured and say so; the two noise-floor values are
    not, and must not sit under that claim. Each block speaks for itself."""
    raw = tomllib.loads(DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_bytes().decode("utf-8"))
    audit = raw["coverage_audit"]
    assert audit["provenance"]["calibrated_for_this_corpus"] is True
    floor = audit[COVERAGE_NOISE_FLOOR_TABLE]["provenance"]
    assert floor["calibrated_for_this_corpus"] is False
    assert floor["sample_count"] == 0
    assert "PROPOSED, NOT YET MEASURED" in floor["caveat"]


def test_the_gates_resolve_proportionally_and_the_flat_constants_did_not():
    """The whole point of the change, as two numbers on two page sizes.

    The retired constants asked the same question of a 52,000-pixel fixture page
    and a 12.6-megapixel leaf. These do not: the gate is 240 times larger on the
    leaf and the band is 18 times wider, which is the ratio of the pages.
    """
    config = _sealed()
    fixture = resolve_coverage_audit_policy(config, 200, 260)
    leaf = resolve_coverage_audit_policy(config, 3634, 3457)

    assert fixture["substantial_ink_pixels"] == fixture["minimum_ink_pixels"] == 24
    assert fixture["edge_band_px"] == 2
    assert leaf["substantial_ink_pixels"] == 5025
    assert leaf["edge_band_px"] == 35
    # The two fields that are not lengths pass through unresolved, exactly as
    # they do in `GroupingThresholds`.
    assert leaf["page_spanning_area_bp"] == fixture["page_spanning_area_bp"] == 5000
    assert leaf["gap_tolerance_px"] == fixture["gap_tolerance_px"] == 3
    # And so do the two noise-floor values: a speck is a speck at every
    # resolution, and the fraction is already the page's own ink.
    assert leaf["minimum_ink_pixels"] == fixture["minimum_ink_pixels"] == 24
    assert leaf["minimum_fraction_outside_bp"] == fixture["minimum_fraction_outside_bp"] == 200


def test_the_substantial_gate_is_floored_at_the_noise_floor():
    """And the floor binds only on pages far smaller than any this reads.

    `coverage_flag` reads the substantial gate before the fraction gate's own
    noise floor, so a resolved value under the sealed floor would flag a
    page on a speck. The floor stops that; where it binds, the two gates
    coincide and the fraction gate cannot decide, which is a property of pages
    about 245 pixels square and smaller.
    """
    config = _sealed()
    assert resolve_coverage_audit_policy(config, 20, 20)["substantial_ink_pixels"] == 24
    assert resolve_coverage_audit_policy(config, 245, 245)["substantial_ink_pixels"] == 24
    assert resolve_coverage_audit_policy(config, 400, 400)["substantial_ink_pixels"] == 64


def test_the_band_never_resolves_to_zero_on_the_smallest_legal_image():
    """A one-pixel-thin page has no centre and still has an edge.

    Without this floor the Ink Map would flag such a page and the Armarium
    re-measure it clean, and `ink_map_page_rows` refuses that disagreement over
    the whole export rather than over the page.
    """
    config = _sealed()
    for width, height in ((1, 100), (100, 1), (1, 1)):
        assert resolve_coverage_audit_policy(config, width, height)["edge_band_px"] == 1


@pytest.mark.parametrize("value", [0, -1, BASIS_POINTS + 1, 0.5, True, "4"])
def test_a_substantial_gate_outside_its_range_is_refused(value):
    with pytest.raises(ContractError, match="substantial_ink_area_bp"):
        validate_coverage_audit_table(_table(substantial_ink_area_bp=value))


@pytest.mark.parametrize("value", [0, -1, BASIS_POINTS // 2, BASIS_POINTS, 0.5, True, "100"])
def test_a_band_outside_its_range_is_refused(value):
    with pytest.raises(ContractError, match="edge_band_bp"):
        validate_coverage_audit_table(_table(edge_band_bp=value))


@pytest.mark.parametrize("value", [0, -1, 0.5, True, "24"])
def test_a_noise_floor_outside_its_range_is_refused(value):
    """Zero is the instrument switched off by a value: every stray pixel flags."""
    with pytest.raises(ContractError, match="minimum_ink_pixels"):
        validate_coverage_audit_table(_table(minimum_ink_pixels=value))


@pytest.mark.parametrize("value", [0, -1, BASIS_POINTS + 1, 0.02, True, "200"])
def test_a_fraction_gate_outside_its_range_is_refused(value):
    """An integer in basis points, never the float it used to be in source."""
    with pytest.raises(ContractError, match="minimum_fraction_outside_bp"):
        validate_coverage_audit_table(_table(minimum_fraction_outside_bp=value))


@pytest.mark.parametrize(("substantial", "band"), [(1, 1), (BASIS_POINTS, BASIS_POINTS // 2 - 1)])
def test_both_accepted_endpoints_stay_accepted(substantial, band):
    assert validate_coverage_audit_table(
        _table(substantial_ink_area_bp=substantial, edge_band_bp=band)
    ) == {**NOISE_FLOOR, "substantial_ink_area_bp": substantial, "edge_band_bp": band}


@pytest.mark.parametrize(("floor", "fraction"), [(1, 1), (10_000_000, BASIS_POINTS)])
def test_both_noise_floor_endpoints_stay_accepted(floor, fraction):
    assert validate_coverage_noise_floor_table(
        {"minimum_ink_pixels": floor, "minimum_fraction_outside_bp": fraction}
    ) == {"minimum_ink_pixels": floor, "minimum_fraction_outside_bp": fraction}


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(ContractError, match="unknown field"):
        validate_coverage_audit_table(_table(edge_band_pixels=64))
    with pytest.raises(ContractError, match=r"noise_floor\] carries unknown field"):
        validate_coverage_noise_floor_table({**NOISE_FLOOR, "minimum_fraction": 0.02})


def test_a_missing_field_is_refused_by_name():
    with pytest.raises(ContractError, match=r"missing field\(s\) \['edge_band_bp'\]"):
        validate_coverage_audit_table({"substantial_ink_area_bp": 4, "noise_floor": NOISE_FLOOR})
    with pytest.raises(ContractError, match=r"missing field\(s\) \['noise_floor'\]"):
        validate_coverage_audit_table(GATES)
    with pytest.raises(ContractError, match=r"missing field\(s\) \['minimum_ink_pixels'\]"):
        validate_coverage_noise_floor_table({"minimum_fraction_outside_bp": 200})


def test_a_missing_table_is_refused_rather_than_defaulted():
    with pytest.raises(ContractError, match=r"no \[coverage_audit\] table"):
        validate_coverage_audit_table(None)


def test_a_file_without_the_designator_bounds_is_refused_by_name(tmp_path):
    """This audit cannot resolve its own policy without the two it borrows."""
    path = tmp_path / "partial.toml"
    path.write_text(
        "[grouping]\n[coverage_audit]\nsubstantial_ink_area_bp = 4\nedge_band_bp = 100\n"
        "[coverage_audit.noise_floor]\nminimum_ink_pixels = 24\nminimum_fraction_outside_bp = 200\n"
    )
    with pytest.raises(ContractError, match=r"page_area_bp.*absolute|absolute.*page_area_bp"):
        load_coverage_audit_config(path)


def test_an_unreadable_file_is_refused_rather_than_defaulted(tmp_path):
    with pytest.raises(ContractError, match="could not be read"):
        load_coverage_audit_config(tmp_path / "absent.toml")


def test_a_page_without_positive_integer_dimensions_is_refused():
    config = _sealed()
    for width, height in ((0, 10), (10, 0), (-1, 10), (True, 10)):
        with pytest.raises(ContractError, match="does not have positive integer dimensions"):
            resolve_coverage_audit_policy(config, width, height)


def _sealed_toml_without(block: str) -> str:
    """The shipped grouping file with one provenance block dropped.

    Built from the real file rather than hand-written, so this test cannot
    quietly stop describing the configuration the pipeline actually loads.
    """
    text = DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.strip() == block)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
        len(lines),
    )
    return "".join(lines[:start] + lines[end:])


@pytest.mark.parametrize(
    ("block", "named"),
    [
        ("[coverage_audit.noise_floor.provenance]", r"noise_floor\.provenance"),
        ("[coverage_audit.provenance]", r"coverage_audit\.provenance"),
    ],
)
def test_a_shipped_policy_with_no_provenance_block_is_refused_by_the_common_loader(
    tmp_path, block, named
):
    """The Ink Map resolves this policy through the loader alone.

    `pipeline/1_ink_map/run.py` calls `load_coverage_audit_config` and publishes
    `ink-map` records under what it returns, without ever calling the
    Designator's own loader -- and it runs before the Designator does. So a file
    whose numbers are well-formed and whose provenance block is missing used to
    reach a published measurement with nothing having asked where those numbers
    came from.
    """
    path = tmp_path / "no-provenance.toml"
    path.write_text(_sealed_toml_without(block), encoding="utf-8")

    with pytest.raises(ContractError, match=named):
        load_coverage_audit_config(path)


def test_a_provenance_block_claiming_calibration_without_samples_is_refused(tmp_path):
    """The shared calibration rule, applied by the loader the Ink Map uses."""
    text = DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_text(encoding="utf-8")
    # The over-claim has to be made for this test to mean anything: a shipped
    # file that stopped carrying an uncalibrated block would otherwise leave it
    # passing over an unmodified file.
    assert "calibrated_for_this_corpus = false" in text
    path = tmp_path / "over-claimed.toml"
    path.write_text(
        text.replace("calibrated_for_this_corpus = false", "calibrated_for_this_corpus = true"),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="sample_count is zero"):
        load_coverage_audit_config(path)
