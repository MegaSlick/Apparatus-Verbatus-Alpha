"""Tests for geometric act grouping: brace-linking, interleaved margins,
page-break continuation candidates, and the no-picker permutation property.

Every fixture here is a small, hand-built list of component dicts -- no image
decoding, no pixels -- so a grouping scenario can be stated as exactly the
geometry it needs to exercise.
"""

import itertools
import random

import pytest
from grouping import (
    DEFAULT_BRACE_MIN_HEIGHT_PX,
    assign_columns,
    find_continuation_candidate,
    group_page,
)
from structure import infer_background, primary_scan

from common.contracts.errors import ContractError

PAGE_W, PAGE_H = 200, 300


def component(x: int, y: int, w: int, h: int) -> dict:
    return {"bounds": {"x": x, "y": y, "w": w, "h": h}, "pixel_count": w * h}


def margin_component(y: int, h: int = 8) -> dict:
    # Centre-x well inside the margin band (< 0.15 * 200 == 30).
    return component(2, y, 15, h)


def body_component(y: int, h: int) -> dict:
    # Centre-x well inside the body band.
    return component(40, y, 120, h)


def bound_key(group: dict) -> tuple[int, int, int, int]:
    bounds = group["bounds"]
    return bounds["x"], bounds["y"], bounds["w"], bounds["h"]


# --- assign_columns -----------------------------------------------------------


def test_assign_columns_splits_by_centre_x():
    margin_c = margin_component(10)
    body_c = body_component(10, 20)
    margin, body = assign_columns([margin_c, body_c], PAGE_W)
    assert margin == [margin_c]
    assert body == [body_c]


def test_assign_columns_refuses_a_non_positive_page_width():
    with pytest.raises(ContractError, match=r"page width 0 is not positive"):
        assign_columns([], 0)


def test_assign_columns_refuses_a_margin_fraction_outside_zero_one():
    with pytest.raises(ContractError, match=r"margin fraction 1\.0 is not between 0 and 1"):
        assign_columns([], PAGE_W, margin_fraction=1.0)


# --- basic grouping -------------------------------------------------------------


def test_no_components_groups_to_nothing():
    assert group_page([], PAGE_W, PAGE_H) == []


def test_one_anchored_body_run_is_one_act():
    anchor = margin_component(20)
    body = body_component(20, 60)
    groups = group_page([anchor, body], PAGE_W, PAGE_H)
    assert len(groups) == 1
    assert groups[0]["anchors"] == [anchor]
    assert groups[0]["body_members"] == [body]


def test_two_well_separated_anchored_acts_stay_two_groups():
    anchor_a = margin_component(20)
    body_a = body_component(20, 60)
    anchor_b = margin_component(140)
    body_b = body_component(140, 60)
    groups = group_page([anchor_a, body_a, anchor_b, body_b], PAGE_W, PAGE_H)
    assert len(groups) == 2
    assert groups[0]["anchors"] == [anchor_a]
    assert groups[1]["anchors"] == [anchor_b]


def test_a_body_run_with_no_preceding_anchor_is_a_leading_fragment():
    body = body_component(5, 40)
    groups = group_page([body], PAGE_W, PAGE_H)
    assert len(groups) == 1
    assert groups[0]["anchors"] == []
    assert "leading fragment" in groups[0]["rationale"]


def test_a_component_contained_in_a_taller_predecessor_does_not_shorten_the_active_run():
    """The active run reaches the greatest bottom edge seen, not merely the last one.

    The middle component is wholly contained by the first.  Replacing the run's
    bottom with that smaller component's bottom makes the third component appear
    seven pixels away and falsely splits it at the six-pixel chain-gap limit.
    """
    tall = body_component(10, 30)  # reaches row 40
    contained = body_component(20, 5)  # reaches only row 25
    following = body_component(32, 5)  # still overlaps the tall component

    groups = group_page([tall, contained, following], PAGE_W, PAGE_H)

    assert len(groups) == 1
    assert groups[0]["body_members"] == [tall, contained, following]


def test_a_blank_gap_still_splits_anchorless_entries_below_a_tall_earlier_component():
    """The gap rule measures from the active run, not an earlier run's bottom."""
    tall = body_component(10, 190)  # reaches row 200
    later_a = body_component(150, 20)  # reaches row 170
    later_b = body_component(185, 10)  # 15 blank rows after later_a
    boundary_anchor = margin_component(140)

    groups = group_page([tall, boundary_anchor, later_a, later_b], PAGE_W, PAGE_H)

    assert [group["body_members"] for group in groups] == [[tall], [later_a], [later_b]]


def test_the_cross_run_blank_gap_split_is_permutation_invariant():
    """Arrival order cannot hide the later blank gap behind the tall first run."""
    tall = body_component(10, 190)
    later_a = body_component(150, 20)
    later_b = body_component(185, 10)
    boundary_anchor = margin_component(140)
    components = [tall, boundary_anchor, later_a, later_b]

    for ordering in itertools.permutations(components):
        groups = group_page(list(ordering), PAGE_W, PAGE_H)
        assert [group["body_members"] for group in groups] == [[tall], [later_a], [later_b]]


def test_an_isolated_anchor_with_no_body_run_is_its_own_marginal_note_act():
    stray = margin_component(200)
    body = body_component(20, 40)  # far away, will not overlap the stray anchor's range
    groups = group_page([stray, body], PAGE_W, PAGE_H)
    assert len(groups) == 2
    marginal = next(g for g in groups if g["body_members"] == [])
    assert marginal["anchors"] == [stray]
    assert "isolated marginal note" in marginal["rationale"]


# --- brace-linked acts (the named fixture) ---------------------------------------


def test_a_tall_brace_anchor_links_two_acts_without_merging_their_body_text():
    """`B. 43 } ... S. 26 }`: one physical marginal glyph, two real acts.

    A single anchor tall enough to span both body runs must not fold them into
    one act (that would lose the second act's own identity) and must not be
    claimed by only one of them (that would leave the other with no evidence
    at all). Both groups carry the brace; their body text stays separate.
    """
    brace = margin_component(20, h=DEFAULT_BRACE_MIN_HEIGHT_PX + 10)
    body_a = body_component(20, 20)  # first entry: y in [20, 40)
    body_b = body_component(45, 20)  # second entry: y in [45, 65)
    groups = group_page([brace, body_a, body_b], PAGE_W, PAGE_H)
    assert len(groups) == 2
    assert groups[0]["body_members"] == [body_a]
    assert groups[1]["body_members"] == [body_b]
    assert groups[0]["anchors"] == [brace]
    assert groups[1]["anchors"] == [brace]
    assert "brace-linked" in groups[0]["rationale"]
    assert "brace-linked" in groups[1]["rationale"]


def test_a_real_decoded_brace_page_drives_primary_scan_into_group_page():
    """The brace fixture above, but through decoded pixels rather than hand-built
    component dicts.

    Every other test in this file states its geometry directly and never calls
    `structure.primary_scan` at all -- true of every grouping test in the diff,
    per spec 06 test 3's own "brace-linked acts fixture" requirement and
    `run.py::_analyze_page`'s real chain (`primary_scan` -> `group_page`). This
    is the one place that chain runs over real decoded pixels: a solid margin
    brace and two solid body blocks, painted onto a page, ink-scanned at
    `structure`'s own default sensitivity and gap tolerance, and handed to
    `group_page` with no overrides -- exactly as `run.py::_analyze_page` calls
    it. If `primary_scan` ever produced components `group_page` did not read as
    a brace (a scan that over-merges the anchor into a body run, or splits the
    brace itself into two pieces below `DEFAULT_BRACE_MIN_HEIGHT_PX`), this is
    the test that would catch it; hand-built component dicts cannot.
    """
    width, height = PAGE_W, 100
    background = 230
    ink = 40
    rows = [bytearray([background] * width) for _ in range(height)]

    def paint(x: int, y: int, w: int, h: int) -> None:
        for row_offset in range(h):
            row = rows[y + row_offset]
            for col_offset in range(w):
                row[x + col_offset] = ink

    # Same geometry as the hand-built brace test above: a margin anchor tall
    # enough to be a brace, and two body runs it links without merging.
    brace_bounds = {"x": 2, "y": 20, "w": 15, "h": DEFAULT_BRACE_MIN_HEIGHT_PX + 10}
    body_a_bounds = {"x": 40, "y": 20, "w": 120, "h": 20}
    body_b_bounds = {"x": 40, "y": 45, "w": 120, "h": 20}
    for bounds in (brace_bounds, body_a_bounds, body_b_bounds):
        paint(bounds["x"], bounds["y"], bounds["w"], bounds["h"])

    background_value = infer_background(width, height, rows)
    assert background_value == background, "ink must stay the numeric minority for this test"
    components = primary_scan(width, height, rows, background=background_value)
    # Three real, independently-scanned ink components -- not the three dicts
    # a hand-built test would have started from.
    assert len(components) == 3
    assert {
        (c["bounds"]["x"], c["bounds"]["y"], c["bounds"]["w"], c["bounds"]["h"]) for c in components
    } == {
        (brace_bounds["x"], brace_bounds["y"], brace_bounds["w"], brace_bounds["h"]),
        (body_a_bounds["x"], body_a_bounds["y"], body_a_bounds["w"], body_a_bounds["h"]),
        (body_b_bounds["x"], body_b_bounds["y"], body_b_bounds["w"], body_b_bounds["h"]),
    }

    groups = group_page(components, width, height)
    assert len(groups) == 2
    body_member_bounds = [[m["bounds"] for m in group["body_members"]] for group in groups]
    assert body_member_bounds == [[body_a_bounds], [body_b_bounds]]
    assert all(group["anchors"][0]["bounds"] == brace_bounds for group in groups)
    assert "brace-linked" in groups[0]["rationale"]
    assert "brace-linked" in groups[1]["rationale"]


def test_a_short_anchor_below_the_brace_threshold_seeds_only_one_act():
    short = margin_component(20, h=DEFAULT_BRACE_MIN_HEIGHT_PX - 1)
    body = body_component(20, 60)
    groups = group_page([short, body], PAGE_W, PAGE_H)
    assert len(groups) == 1
    assert groups[0]["anchors"] == [short]


# --- interleaved margins: no vertical gap between two acts' body text -----------


def test_two_acts_with_no_body_gap_still_split_at_the_margin_anchor():
    """Two acts whose body text touches with zero blank rows between them.

    A gap-only chainer would fuse this into one run; the margin anchor
    starting mid-run is what a real page would show, and grouping must use it
    even though there is no whitespace to find.
    """
    anchor_a = margin_component(20)
    anchor_b = margin_component(60)
    body_a = body_component(20, 40)  # y in [20, 60)
    body_b = body_component(60, 40)  # y in [60, 100), touches body_a exactly
    groups = group_page([anchor_a, body_a, anchor_b, body_b], PAGE_W, PAGE_H)
    assert len(groups) == 2
    assert groups[0]["body_members"] == [body_a]
    assert groups[1]["body_members"] == [body_b]


def test_a_body_run_starting_slightly_above_its_own_anchor_still_splits():
    """Ordinary detection jitter, not evidence the second act starts earlier.

    A second act's first body line can land a pixel or two above its own
    margin anchor's own top edge (scan noise, not an interleaved-margins
    zero-gap page). A strict, zero-tolerance boundary comparison would count
    that line as still belonging to the previous zone and silently merge the
    two acts into one -- the same `anchor_reach_px` slack the anchor
    attachment test already gives this geometry must also apply to the
    partition itself.
    """
    anchor_a = margin_component(0, h=8)
    anchor_b = margin_component(50, h=8)
    body_a = body_component(10, 35)  # y in [10, 45)
    body_b = body_component(49, 42)  # y in [49, 91): starts 1px above anchor_b
    groups = group_page([anchor_a, body_a, anchor_b, body_b], PAGE_W, PAGE_H)
    assert len(groups) == 2
    assert groups[0]["body_members"] == [body_a]
    assert groups[1]["body_members"] == [body_b]


# --- no picker: permutation invariance ------------------------------------------


def test_grouping_is_invariant_to_the_order_components_are_supplied_in():
    components = [
        margin_component(20),
        body_component(20, 40),
        margin_component(70),
        body_component(70, 30),
        margin_component(150, h=DEFAULT_BRACE_MIN_HEIGHT_PX + 5),
        body_component(150, 15),
        body_component(170, 15),
        margin_component(260),
    ]
    baseline = group_page(components, PAGE_W, PAGE_H)
    baseline_keys = sorted(
        (bound_key(group), tuple(sorted(bound_key(m) for m in group["body_members"])))
        for group in baseline
    )
    rng = random.Random(1234567)
    for _ in range(20):
        shuffled = list(components)
        rng.shuffle(shuffled)
        result = group_page(shuffled, PAGE_W, PAGE_H)
        result_keys = sorted(
            (bound_key(group), tuple(sorted(bound_key(m) for m in group["body_members"])))
            for group in result
        )
        assert result_keys == baseline_keys


def test_grouping_is_invariant_across_every_permutation_of_a_small_case():
    components = [
        margin_component(20),
        body_component(20, 30),
        margin_component(70),
        body_component(70, 30),
    ]
    baseline = group_page(components, PAGE_W, PAGE_H)
    baseline_keys = sorted(bound_key(group) for group in baseline)
    for permutation in itertools.permutations(components):
        result = group_page(list(permutation), PAGE_W, PAGE_H)
        assert sorted(bound_key(group) for group in result) == baseline_keys


# --- continuation candidates -----------------------------------------------------


def test_a_trailing_group_touching_the_bottom_pairs_with_an_unanchored_leading_group():
    page_a_h = 300
    trailing = body_component(page_a_h - 20, 20)  # bottom touches page edge
    page_a_groups = group_page([trailing], PAGE_W, page_a_h)

    leading = body_component(0, 30)  # top touches page edge, no anchor
    page_b_groups = group_page([leading], PAGE_W, page_a_h)

    candidate = find_continuation_candidate(page_a_groups, page_a_h, page_b_groups)
    assert candidate is not None
    assert candidate["page_a_group"]["body_members"] == [trailing]
    assert candidate["page_b_group"]["body_members"] == [leading]


def test_an_anchored_leading_group_is_a_new_act_not_a_continuation():
    page_a_h = 300
    trailing = body_component(page_a_h - 20, 20)
    page_a_groups = group_page([trailing], PAGE_W, page_a_h)

    anchor = margin_component(0)
    leading = body_component(0, 30)
    page_b_groups = group_page([anchor, leading], PAGE_W, page_a_h)

    assert find_continuation_candidate(page_a_groups, page_a_h, page_b_groups) is None


def test_a_trailing_group_far_from_the_bottom_edge_is_not_a_continuation():
    page_a_h = 300
    trailing = body_component(100, 20)  # nowhere near the bottom
    page_a_groups = group_page([trailing], PAGE_W, page_a_h)

    leading = body_component(0, 30)
    page_b_groups = group_page([leading], PAGE_W, page_a_h)

    assert find_continuation_candidate(page_a_groups, page_a_h, page_b_groups) is None


def test_columns_that_do_not_overlap_are_not_a_continuation():
    page_a_h = 300
    trailing = component(5, page_a_h - 20, 20, 20)  # far left, touches bottom
    page_a_groups = group_page([trailing], PAGE_W, page_a_h)

    leading = component(150, 0, 20, 20)  # far right, touches top
    page_b_groups = group_page([leading], PAGE_W, page_a_h)

    assert find_continuation_candidate(page_a_groups, page_a_h, page_b_groups) is None


def test_no_continuation_when_either_page_marked_out_nothing():
    assert find_continuation_candidate([], 300, [component(0, 0, 10, 10)]) is None
    assert find_continuation_candidate([component(0, 0, 10, 10)], 300, []) is None


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize("page_w,page_h", [(0, 100), (100, 0), (-1, 100)])
def test_group_page_refuses_a_non_positive_page(page_w, page_h):
    with pytest.raises(ContractError, match=r"a -?\d+x\d+ page has no area to group within"):
        group_page([component(0, 0, 5, 5)], page_w, page_h)
