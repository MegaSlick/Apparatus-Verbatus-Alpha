"""`_match_structural_group`: grouping's output is reconciled, never substituted.

Direct unit tests for the pure geometry helpers `run.py` uses to bind a
declared act to the structural group detection actually found, and to refuse
when detection found nothing worth calling a match.
"""

import pytest
from _test_support import load_designator

from common.contracts.errors import ContractError


def _load_designator():
    return load_designator("designator_structural_reconciliation_under_test")


# `grouping.group_page` no longer carries module defaults for these -- `run.py`
# resolves them per page from the sealed grouping policy and hands them in.
# These four are the retired constants at the 200-wide page every test below
# builds on (margin 0.15 * 200 = 30px, and the three pixel counts unchanged),
# spelled out here so these tests keep exercising the geometry they always
# exercised rather than a policy that could be edited out from under them: what
# is under test is `_match_structural_group`, not the thresholds.
_THRESHOLDS = {
    "margin_px": 30,
    "chain_gap_px": 6,
    "anchor_reach_px": 2,
    "brace_min_height_px": 30,
}


def _group(bounds: dict, body_members=()) -> dict:
    return {
        "bounds": bounds,
        "body_members": list(body_members),
        "anchors": [],
        "rationale": "complete structural reconciliation test group",
    }


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
    small_overlap = _group({"x": 8, "y": 8, "w": 10, "h": 10})
    large_overlap = _group({"x": 0, "y": 0, "w": 10, "h": 10})
    groups = [small_overlap, large_overlap]
    assert designator._match_structural_group(groups, declared, "test act") is large_overlap


def test_match_refuses_when_no_detected_group_covers_at_least_half_the_declared_area():
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 100, "h": 100}
    barely_touching = _group({"x": 95, "y": 95, "w": 10, "h": 10})  # 5x5 = 25px overlap of 10000
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
    half = _group({"x": 0, "y": 0, "w": 10, "h": 5})  # area 50, overlap 50
    assert designator._match_structural_group([half], declared, "test act") is half


def test_match_breaks_a_tied_full_bounds_overlap_by_the_groups_own_body_members():
    """The brace-linked case: a shared tall anchor makes both groups' union
    bounds identical, so full-bounds overlap alone cannot tell them apart.
    Each group's own body text -- not the anchor both groups carry -- must
    decide which group actually corresponds to which declared act."""
    designator = _load_designator()
    shared_bounds = {"x": 0, "y": 0, "w": 20, "h": 20}  # both groups tie here
    group_a = _group(shared_bounds, [{"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}])
    group_b = _group(shared_bounds, [{"bounds": {"x": 0, "y": 10, "w": 10, "h": 10}}])
    declared_a = {"x": 0, "y": 0, "w": 10, "h": 10}
    declared_b = {"x": 0, "y": 10, "w": 10, "h": 10}
    assert designator._match_structural_group([group_a, group_b], declared_a, "act a") is group_a
    assert designator._match_structural_group([group_a, group_b], declared_b, "act b") is group_b
    # Order must not matter -- the tie-break is a property of the geometry, not
    # of which group happened to be checked first.
    assert designator._match_structural_group([group_b, group_a], declared_a, "act a") is group_a
    assert designator._match_structural_group([group_b, group_a], declared_b, "act b") is group_b


def test_two_declared_acts_cannot_both_claim_one_detected_group():
    """Detection merged a boundary it did not find; it corroborates neither act.

    Built on real `grouping.group_page` output rather than a hand-written group:
    two register entries with no margin anchor and three blank rows between them
    (under the resolved chain gap of 6px) are one detected run, and each declared
    act's own rectangle lies wholly inside it, so `_match_structural_group`
    returns the same group for both. Recording it as each act's own
    `detected_bounds` would claim a corroboration that was never measured.
    """
    import grouping

    designator = _load_designator()

    def component(x, y, w, h):
        return {"bounds": {"x": x, "y": y, "w": w, "h": h}, "pixel_count": w * h}

    groups = grouping.group_page(
        [component(40, 20, 120, 40), component(40, 63, 120, 37)], 200, 300, **_THRESHOLDS
    )
    assert len(groups) == 1, "the premise of this test is that detection merged the two"
    declared_a = {"x": 40, "y": 20, "w": 120, "h": 40}
    declared_b = {"x": 40, "y": 63, "w": 120, "h": 37}
    analysis = {"groups": groups}

    first = designator._match_structural_group(groups, declared_a, "act a")
    designator._claim_structural_group(analysis, first, "a", "act a")
    second = designator._match_structural_group(groups, declared_b, "act b")
    with pytest.raises(ContractError, match="already corresponds to act 'a'"):
        designator._claim_structural_group(
            analysis,
            {
                **second,
                "bounds": dict(second["bounds"]),
                "body_members": [dict(member) for member in second["body_members"]],
                "anchors": [dict(anchor) for anchor in second["anchors"]],
            },
            "b",
            "act b",
        )


def test_one_act_may_claim_its_own_group_twice_without_refusing_itself():
    """A same-act second claim is not a second claimant."""
    designator = _load_designator()
    group = _group({"x": 0, "y": 0, "w": 10, "h": 10})
    analysis: dict = {}
    designator._claim_structural_group(analysis, group, "a1", "act a1")
    designator._claim_structural_group(analysis, group, "a1", "act a1 continuation")


def test_brace_linked_acts_each_claim_their_own_group():
    """The named brace fixture stays legal: two groups sharing one anchor."""
    import grouping

    designator = _load_designator()

    def component(x, y, w, h):
        return {"bounds": {"x": x, "y": y, "w": w, "h": h}, "pixel_count": w * h}

    brace = component(2, 20, 15, _THRESHOLDS["brace_min_height_px"] + 10)
    groups = grouping.group_page(
        [brace, component(40, 20, 120, 20), component(40, 45, 120, 20)], 200, 300, **_THRESHOLDS
    )
    assert len(groups) == 2
    analysis = {"groups": groups}
    for key, declared in (
        ("a", {"x": 40, "y": 20, "w": 120, "h": 20}),
        ("b", {"x": 40, "y": 45, "w": 120, "h": 20}),
    ):
        group = designator._match_structural_group(groups, declared, f"act {key}")
        designator._claim_structural_group(analysis, group, key, f"act {key}")


def test_match_refuses_when_body_members_cannot_break_the_tie_either():
    """Equal evidence has no measured basis for selecting either group."""
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 10, "h": 10}
    first = _group({"x": 0, "y": 0, "w": 10, "h": 10})
    second = _group({"x": 0, "y": 0, "w": 10, "h": 10})
    with pytest.raises(ContractError, match="unresolved structural tie"):
        designator._match_structural_group([first, second], declared, "test act")


def test_match_refuses_a_group_missing_its_body_evidence():
    designator = _load_designator()
    declared = {"x": 0, "y": 0, "w": 10, "h": 10}
    incomplete = {
        "bounds": dict(declared),
        "anchors": [],
        "rationale": "test fixture missing body evidence",
    }

    with pytest.raises(ContractError, match="carries no body_members evidence"):
        designator._match_structural_group([incomplete], declared, "test act")
