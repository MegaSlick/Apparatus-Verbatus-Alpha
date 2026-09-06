"""The sealed `[coverage_audit]` policy: what it refuses, and what it resolves to.

The two gates in `common/residual_ink.py` were flat pixel counts until
2026-09-06 and are fractions of the page now. This module holds the loader and
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
    DEFAULT_COVERAGE_AUDIT_CONFIG_PATH,
    MINIMUM_INK_PIXELS,
    load_coverage_audit_config,
    resolve_coverage_audit_policy,
    validate_coverage_audit_table,
)

ROOT = Path(__file__).resolve().parents[1]


def _sealed() -> dict:
    return load_coverage_audit_config()


def test_the_shipped_block_loads_with_the_digest_of_its_own_bytes():
    config = _sealed()
    from common.contracts.canonical import digest_bytes

    assert config["config_sha256"] == digest_bytes(DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_bytes())
    assert config["coverage_audit"] == {"substantial_ink_area_bp": 4, "edge_band_bp": 100}
    # The two Designator fields this audit resolves beside its own, out of the
    # same file: the component it takes out of its counts must be the component
    # that stage withheld, which is true only while both read one number.
    assert config["page_spanning_area_bp"] == 5000
    assert config["gap_tolerance_px"] == 3


def test_the_block_is_read_from_the_file_the_designator_is_sealed_under():
    """One file, one digest, one seal -- not a second sealed name for two gates."""
    assert DEFAULT_COVERAGE_AUDIT_CONFIG_PATH == ROOT / "config" / "designator_grouping.toml"
    raw = tomllib.loads(DEFAULT_COVERAGE_AUDIT_CONFIG_PATH.read_bytes().decode("utf-8"))
    assert set(raw["coverage_audit"]) == set(COVERAGE_AUDIT_BP_FIELDS) | {"provenance"}


def test_the_gates_resolve_proportionally_and_the_flat_constants_did_not():
    """The whole point of the change, as two numbers on two page sizes.

    The retired constants asked the same question of a 52,000-pixel fixture page
    and a 12.6-megapixel leaf. These do not: the gate is 240 times larger on the
    leaf and the band is 18 times wider, which is the ratio of the pages.
    """
    config = _sealed()
    fixture = resolve_coverage_audit_policy(config, 200, 260)
    leaf = resolve_coverage_audit_policy(config, 3634, 3457)

    assert fixture["substantial_ink_pixels"] == MINIMUM_INK_PIXELS == 24
    assert fixture["edge_band_px"] == 2
    assert leaf["substantial_ink_pixels"] == 5025
    assert leaf["edge_band_px"] == 35
    # The two fields that are not lengths pass through unresolved, exactly as
    # they do in `GroupingThresholds`.
    assert leaf["page_spanning_area_bp"] == fixture["page_spanning_area_bp"] == 5000
    assert leaf["gap_tolerance_px"] == fixture["gap_tolerance_px"] == 3


def test_the_substantial_gate_is_floored_at_the_noise_floor():
    """And the floor binds only on pages far smaller than any this reads.

    `coverage_flag` reads the substantial gate before the fraction gate's own
    noise floor, so a resolved value under `MINIMUM_INK_PIXELS` would flag a
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
        validate_coverage_audit_table({"substantial_ink_area_bp": value, "edge_band_bp": 100})


@pytest.mark.parametrize("value", [0, -1, BASIS_POINTS // 2, BASIS_POINTS, 0.5, True, "100"])
def test_a_band_outside_its_range_is_refused(value):
    with pytest.raises(ContractError, match="edge_band_bp"):
        validate_coverage_audit_table({"substantial_ink_area_bp": 4, "edge_band_bp": value})


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(ContractError, match="unknown field"):
        validate_coverage_audit_table(
            {"substantial_ink_area_bp": 4, "edge_band_bp": 100, "edge_band_pixels": 64}
        )


def test_a_missing_field_is_refused_by_name():
    with pytest.raises(ContractError, match=r"missing field\(s\) \['edge_band_bp'\]"):
        validate_coverage_audit_table({"substantial_ink_area_bp": 4})


def test_a_missing_table_is_refused_rather_than_defaulted():
    with pytest.raises(ContractError, match=r"no \[coverage_audit\] table"):
        validate_coverage_audit_table(None)


def test_a_file_without_the_designator_bounds_is_refused_by_name(tmp_path):
    """This audit cannot resolve its own policy without the two it borrows."""
    path = tmp_path / "partial.toml"
    path.write_text(
        "[grouping]\n[coverage_audit]\nsubstantial_ink_area_bp = 4\nedge_band_bp = 100\n"
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
