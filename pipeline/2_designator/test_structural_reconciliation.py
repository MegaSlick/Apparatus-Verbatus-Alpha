"""`_match_structural_group`: grouping's output is reconciled, never substituted.

Direct unit tests for the pure geometry helpers `run.py` uses to bind a
declared act to the structural group detection actually found, and to refuse
when detection found nothing worth calling a match.
"""

import importlib.util
from pathlib import Path

import pytest

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]


def _load_designator():
    path = ROOT / "pipeline" / "2_designator" / "run.py"
    spec = importlib.util.spec_from_file_location(
        "designator_structural_reconciliation_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_overlap_area_of_disjoint_rectangles_is_zero():
    designator = _load_designator()
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 20, "y": 20, "w": 10, "h": 10}
    assert designator._overlap_area(a, b) == 0


def test_overlap_area_of_identical_rectangles_is_their_area():
    designator = _load_designator()
    a = {"x": 5, "y": 5, "w": 10, "h": 8}
    assert designator._overlap_area(a, dict(a)) == 80


def test_match_picks_the_group_with_the_most_overlap_not_the_first():
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 10, "h": 10}
    small_overlap = {"bounds": {"x": 8, "y": 8, "w": 10, "h": 10}}
    large_overlap = {"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}
    groups = [small_overlap, large_overlap]
    assert designator._match_structural_group(groups, declared, "test act") is large_overlap


def test_match_refuses_when_no_detected_group_covers_at_least_half_the_declared_area():
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 100, "h": 100}
    barely_touching = {
        "bounds": {"x": 95, "y": 95, "w": 10, "h": 10}
    }  # 5x5 = 25px overlap of 10000
    with pytest.raises(ContractError, match="structural grouping found no detected region"):
        designator._match_structural_group([barely_touching], declared, "test act")


def test_match_refuses_when_no_group_exists_at_all():
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 10, "h": 10}
    with pytest.raises(ContractError, match="structural grouping found no detected region"):
        designator._match_structural_group([], declared, "test act")


def test_match_accepts_a_group_covering_exactly_half_the_declared_area():
    """The boundary itself: half is the accepted floor, not the refused ceiling."""
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 10, "h": 10}  # area 100
    half = {"bounds": {"x": 0, "y": 0, "w": 10, "h": 5}}  # area 50, overlap 50
    assert designator._match_structural_group([half], declared, "test act") is half


def test_match_breaks_a_tied_full_bounds_overlap_by_the_groups_own_body_members():
    """The brace-linked case: a shared tall anchor makes both groups' union
    bounds identical, so full-bounds overlap alone cannot tell them apart.
    Each group's own body text -- not the anchor both groups carry -- must
    decide which group actually corresponds to which declared act."""
    designator = _load_designator()
    shared_bounds = {"x": 0, "y": 0, "w": 20, "h": 20}  # both groups tie here
    group_a = {
        "bounds": shared_bounds,
        "body_members": [{"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}],
    }
    group_b = {
        "bounds": shared_bounds,
        "body_members": [{"bounds": {"x": 0, "y": 10, "w": 10, "h": 10}}],
    }
    declared_a = {"x": 0, "y": 0, "w": 10, "h": 10}
    declared_b = {"x": 0, "y": 10, "w": 10, "h": 10}
    assert designator._match_structural_group([group_a, group_b], declared_a, "act a") is group_a
    assert designator._match_structural_group([group_a, group_b], declared_b, "act b") is group_b
    # Order must not matter -- the tie-break is a property of the geometry, not
    # of which group happened to be checked first.
    assert designator._match_structural_group([group_b, group_a], declared_a, "act a") is group_a
    assert designator._match_structural_group([group_b, group_a], declared_b, "act b") is group_b


def test_match_falls_back_to_first_when_body_members_cannot_break_the_tie_either():
    """Neither group carries a `body_members` key (a bare-bounds test double, or
    an isolated marginal-note group with no body at all): the tie-break has
    nothing to compare, and the original first-wins behavior is preserved
    rather than raising or picking arbitrarily."""
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 10, "h": 10}
    first = {"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}
    second = {"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}
    assert designator._match_structural_group([first, second], declared, "test act") is first
