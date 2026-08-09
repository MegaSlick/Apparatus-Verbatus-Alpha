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
