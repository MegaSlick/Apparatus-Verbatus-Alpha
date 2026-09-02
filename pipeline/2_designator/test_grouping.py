"""Tests for geometric act grouping: brace-linking, interleaved margins,
page-break continuation candidates, and the no-picker permutation property.

Every fixture here is a small, hand-built list of component dicts -- no image
decoding, no pixels -- so a grouping scenario can be stated as exactly the
geometry it needs to exercise.
"""

import itertools
import random

import pytest
from grouping import assign_columns, find_continuation_candidate, group_page
from structure import infer_background, primary_scan

from common.contracts.errors import ContractError

PAGE_W, PAGE_H = 200, 300

# Explicit ints equal to the retired module defaults (grouping.py used to
# declare these as DEFAULT_MARGIN_FRACTION == 0.15 (0.15 * PAGE_W == 30px),
# DEFAULT_CHAIN_GAP_PX == 6, DEFAULT_ANCHOR_REACH_PX == 2,
# DEFAULT_BRACE_MIN_HEIGHT_PX == 30, DEFAULT_PAGE_EDGE_REACH_PX == 4;
# structure.py's DEFAULT_GAP_TOLERANCE_PX == 3). Every test below passes
# these explicitly so the behaviour the old defaults produced is proven
# identical, never merely assumed.
MARGIN_PX = 30  # 0.15 * PAGE_W
CHAIN_GAP_PX = 6
ANCHOR_REACH_PX = 2
BRACE_MIN_HEIGHT_PX = 30
PAGE_EDGE_REACH_PX = 4
GAP_TOLERANCE_PX = 3


def component(x: int, y: int, w: int, h: int) -> dict:
    return {"bounds": {"x": x, "y": y, "w": w, "h": h}, "pixel_count": w * h}


def margin_component(y: int, h: int = 8) -> dict:
    # Centre-x well inside the margin band (< MARGIN_PX == 30).
    return component(2, y, 15, h)


def body_component(y: int, h: int) -> dict:
    # Centre-x well inside the body band.
    return component(40, y, 120, h)


def bound_key(group: dict) -> tuple[int, int, int, int]:
    bounds = group["bounds"]
    return bounds["x"], bounds["y"], bounds["w"], bounds["h"]


def group(components: list[dict], page_w: int = PAGE_W, page_h: int = PAGE_H) -> list[dict]:
    """`group_page`, wired with the explicit ints equal to the retired defaults."""
    return group_page(
        components,
        page_w,
        page_h,
        margin_px=MARGIN_PX,
        chain_gap_px=CHAIN_GAP_PX,
        anchor_reach_px=ANCHOR_REACH_PX,
        brace_min_height_px=BRACE_MIN_HEIGHT_PX,
    )


def continuation(page_a_groups, page_a_h, page_b_groups, **overrides):
    kwargs = {
        "edge_reach_a_px": PAGE_EDGE_REACH_PX,
        "edge_reach_b_px": PAGE_EDGE_REACH_PX,
        **overrides,
    }
    return find_continuation_candidate(page_a_groups, page_a_h, page_b_groups, **kwargs)


# --- assign_columns -----------------------------------------------------------


def test_assign_columns_splits_by_centre_x():
    margin_c = margin_component(10)
    body_c = body_component(10, 20)
    margin, body = assign_columns([margin_c, body_c], PAGE_W, margin_px=MARGIN_PX)
    assert margin == [margin_c]
    assert body == [body_c]


def test_assign_columns_refuses_a_non_positive_page_width():
    with pytest.raises(ContractError, match=r"page width 0 is not positive"):
        assign_columns([], 0, margin_px=1)


@pytest.mark.parametrize("margin_px", [0, PAGE_W, -5, PAGE_W + 1])
def test_assign_columns_refuses_a_margin_px_outside_the_page(margin_px):
    with pytest.raises(
        ContractError,
        match=rf"margin {margin_px}px is not between 0 and page width {PAGE_W}",
    ):
        assign_columns([], PAGE_W, margin_px=margin_px)


def test_assign_columns_refuses_a_missing_margin_px():
    with pytest.raises(TypeError):
        assign_columns([], PAGE_W)


def test_assign_columns_pins_margin_px_at_its_own_boundary():
    """MARGIN_PX must actually decide the partition, not just describe it.

    Every other fixture in this file puts margin components at centre-x 9.5
    and body components at centre-x 100, so any boundary in (10, 100] gives
    the identical split -- MARGIN_PX == 30 could silently drift and every
    other test would still pass. This pins it directly: one component sits
    just inside the 30px boundary, one just outside.
    """
    inside = component(MARGIN_PX - 11, 0, 20, 5)  # centre 29, < MARGIN_PX
    outside = component(MARGIN_PX - 9, 0, 20, 5)  # centre 31, >= MARGIN_PX
    margin, body = assign_columns([inside, outside], PAGE_W, margin_px=MARGIN_PX)
    assert margin == [inside]
    assert body == [outside]


# --- basic grouping -------------------------------------------------------------


def test_no_components_groups_to_nothing():
    assert group([], PAGE_W, PAGE_H) == []


def test_one_anchored_body_run_is_one_act():
    anchor = margin_component(20)
    body = body_component(20, 60)
    groups = group([anchor, body], PAGE_W, PAGE_H)
    assert len(groups) == 1
    assert groups[0]["anchors"] == [anchor]
    assert groups[0]["body_members"] == [body]


def test_two_well_separated_anchored_acts_stay_two_groups():
    anchor_a = margin_component(20)
    body_a = body_component(20, 60)
    anchor_b = margin_component(140)
    body_b = body_component(140, 60)
    groups = group([anchor_a, body_a, anchor_b, body_b], PAGE_W, PAGE_H)
    assert len(groups) == 2
    assert groups[0]["anchors"] == [anchor_a]
    assert groups[1]["anchors"] == [anchor_b]


def test_a_body_run_with_no_preceding_anchor_is_a_leading_fragment():
    body = body_component(5, 40)
    groups = group([body], PAGE_W, PAGE_H)
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

    groups = group([tall, contained, following], PAGE_W, PAGE_H)

    assert len(groups) == 1
    assert groups[0]["body_members"] == [tall, contained, following]


def test_a_blank_gap_still_splits_anchorless_entries_below_a_tall_earlier_component():
    """The gap rule measures from the active run, not an earlier run's bottom."""
    tall = body_component(10, 190)  # reaches row 200
    later_a = body_component(150, 20)  # reaches row 170
    later_b = body_component(185, 10)  # 15 blank rows after later_a
    boundary_anchor = margin_component(140)

    groups = group([tall, boundary_anchor, later_a, later_b], PAGE_W, PAGE_H)

    assert [group["body_members"] for group in groups] == [[tall], [later_a], [later_b]]


def test_the_cross_run_blank_gap_split_is_permutation_invariant():
    """Arrival order cannot hide the later blank gap behind the tall first run."""
    tall = body_component(10, 190)
    later_a = body_component(150, 20)
    later_b = body_component(185, 10)
    boundary_anchor = margin_component(140)
    components = [tall, boundary_anchor, later_a, later_b]

    for ordering in itertools.permutations(components):
        groups = group(list(ordering), PAGE_W, PAGE_H)
        assert [group["body_members"] for group in groups] == [[tall], [later_a], [later_b]]


def test_an_isolated_anchor_with_no_body_run_is_its_own_marginal_note_act():
    stray = margin_component(200)
    body = body_component(20, 40)  # far away, will not overlap the stray anchor's range
    groups = group([stray, body], PAGE_W, PAGE_H)
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
    brace = margin_component(20, h=BRACE_MIN_HEIGHT_PX + 10)
    body_a = body_component(20, 20)  # first entry: y in [20, 40)
    body_b = body_component(45, 20)  # second entry: y in [45, 65)
    groups = group([brace, body_a, body_b], PAGE_W, PAGE_H)
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
    PRIMARY_MARGIN sensitivity with the explicit gap tolerance and grouping
    thresholds equal to the retired defaults -- the same values
    run.py::_analyze_page will resolve per page from
    config/designator_grouping.toml. If `primary_scan` ever produced components `group_page` did not read as
    a brace (a scan that over-merges the anchor into a body run, or splits the
    brace itself into two pieces below `BRACE_MIN_HEIGHT_PX`), this is
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
    brace_bounds = {"x": 2, "y": 20, "w": 15, "h": BRACE_MIN_HEIGHT_PX + 10}
    body_a_bounds = {"x": 40, "y": 20, "w": 120, "h": 20}
    body_b_bounds = {"x": 40, "y": 45, "w": 120, "h": 20}
    for bounds in (brace_bounds, body_a_bounds, body_b_bounds):
        paint(bounds["x"], bounds["y"], bounds["w"], bounds["h"])

    background_value = infer_background(width, height, rows)
    assert background_value == background, "ink must stay the numeric minority for this test"
    components = primary_scan(
        width, height, rows, background=background_value, gap_tolerance_px=GAP_TOLERANCE_PX
    )
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

    groups = group(components, width, height)
    assert len(groups) == 2
    body_member_bounds = [[m["bounds"] for m in group["body_members"]] for group in groups]
    assert body_member_bounds == [[body_a_bounds], [body_b_bounds]]
    assert all(group["anchors"][0]["bounds"] == brace_bounds for group in groups)
    assert "brace-linked" in groups[0]["rationale"]
    assert "brace-linked" in groups[1]["rationale"]


def test_a_short_anchor_below_the_brace_threshold_seeds_only_one_act():
    short = margin_component(20, h=BRACE_MIN_HEIGHT_PX - 1)
    body = body_component(20, 60)
    groups = group([short, body], PAGE_W, PAGE_H)
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
    groups = group([anchor_a, body_a, anchor_b, body_b], PAGE_W, PAGE_H)
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
    groups = group([anchor_a, body_a, anchor_b, body_b], PAGE_W, PAGE_H)
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
        margin_component(150, h=BRACE_MIN_HEIGHT_PX + 5),
        body_component(150, 15),
        body_component(170, 15),
        margin_component(260),
    ]
    baseline = group(components, PAGE_W, PAGE_H)
    baseline_keys = sorted(
        (bound_key(group), tuple(sorted(bound_key(m) for m in group["body_members"])))
        for group in baseline
    )
    rng = random.Random(1234567)
    for _ in range(20):
        shuffled = list(components)
        rng.shuffle(shuffled)
        result = group(shuffled, PAGE_W, PAGE_H)
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
    baseline = group(components, PAGE_W, PAGE_H)
    baseline_keys = sorted(bound_key(group) for group in baseline)
    for permutation in itertools.permutations(components):
        result = group(list(permutation), PAGE_W, PAGE_H)
        assert sorted(bound_key(group) for group in result) == baseline_keys


# --- continuation candidates -----------------------------------------------------


def test_a_trailing_group_touching_the_bottom_pairs_with_an_unanchored_leading_group():
    page_a_h = 300
    trailing = body_component(page_a_h - 20, 20)  # bottom touches page edge
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    leading = body_component(0, 30)  # top touches page edge, no anchor
    page_b_groups = group([leading], PAGE_W, page_a_h)

    candidate = continuation(page_a_groups, page_a_h, page_b_groups)
    assert candidate is not None
    assert candidate["page_a_group"]["body_members"] == [trailing]
    assert candidate["page_b_group"]["body_members"] == [leading]


def test_an_anchored_leading_group_is_a_new_act_not_a_continuation():
    page_a_h = 300
    trailing = body_component(page_a_h - 20, 20)
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    anchor = margin_component(0)
    leading = body_component(0, 30)
    page_b_groups = group([anchor, leading], PAGE_W, page_a_h)

    assert continuation(page_a_groups, page_a_h, page_b_groups) is None


def test_a_trailing_group_far_from_the_bottom_edge_is_not_a_continuation():
    page_a_h = 300
    trailing = body_component(100, 20)  # nowhere near the bottom
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    leading = body_component(0, 30)
    page_b_groups = group([leading], PAGE_W, page_a_h)

    assert continuation(page_a_groups, page_a_h, page_b_groups) is None


def test_columns_that_do_not_overlap_are_not_a_continuation():
    page_a_h = 300
    trailing = component(5, page_a_h - 20, 20, 20)  # far left, touches bottom
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    leading = component(150, 0, 20, 20)  # far right, touches top
    page_b_groups = group([leading], PAGE_W, page_a_h)

    assert continuation(page_a_groups, page_a_h, page_b_groups) is None


def test_no_continuation_when_either_page_marked_out_nothing():
    assert continuation([], 300, [component(0, 0, 10, 10)]) is None
    assert continuation([component(0, 0, 10, 10)], 300, []) is None


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize("page_w,page_h", [(0, 100), (100, 0), (-1, 100)])
def test_group_page_refuses_a_non_positive_page(page_w, page_h):
    with pytest.raises(ContractError, match=r"a -?\d+x\d+ page has no area to group within"):
        group([component(0, 0, 5, 5)], page_w, page_h)


@pytest.mark.parametrize(
    "missing",
    ["margin_px", "chain_gap_px", "anchor_reach_px", "brace_min_height_px"],
)
def test_group_page_refuses_a_missing_required_keyword(missing):
    """Every geometric parameter is required now that the module default is gone.

    A caller that forgets one of the four resolved ints fails loudly with
    `TypeError` at the call, rather than silently running under a value
    nobody reviewed for this page.
    """
    kwargs = {
        "margin_px": MARGIN_PX,
        "chain_gap_px": CHAIN_GAP_PX,
        "anchor_reach_px": ANCHOR_REACH_PX,
        "brace_min_height_px": BRACE_MIN_HEIGHT_PX,
    }
    del kwargs[missing]
    with pytest.raises(TypeError):
        group_page([component(0, 0, 5, 5)], PAGE_W, PAGE_H, **kwargs)


@pytest.mark.parametrize(
    "name,bad_value",
    [
        ("chain_gap_px", -1),
        ("chain_gap_px", 1.5),
        ("anchor_reach_px", -1),
        ("anchor_reach_px", 1.5),
        ("brace_min_height_px", -1),
        ("brace_min_height_px", 1.5),
    ],
)
def test_group_page_refuses_a_negative_or_non_integer_threshold(name, bad_value):
    kwargs = {
        "margin_px": MARGIN_PX,
        "chain_gap_px": CHAIN_GAP_PX,
        "anchor_reach_px": ANCHOR_REACH_PX,
        "brace_min_height_px": BRACE_MIN_HEIGHT_PX,
    }
    kwargs[name] = bad_value
    with pytest.raises(ContractError, match="is not a non-negative integer"):
        group_page([component(0, 0, 5, 5)], PAGE_W, PAGE_H, **kwargs)


def test_find_continuation_candidate_refuses_a_missing_edge_reach_keyword():
    trailing = body_component(280, 20)
    leading = body_component(0, 30)
    page_a_groups = group([trailing], PAGE_W, PAGE_H)
    page_b_groups = group([leading], PAGE_W, PAGE_H)
    with pytest.raises(TypeError):
        find_continuation_candidate(
            page_a_groups, PAGE_H, page_b_groups, edge_reach_a_px=PAGE_EDGE_REACH_PX
        )
    with pytest.raises(TypeError):
        find_continuation_candidate(
            page_a_groups, PAGE_H, page_b_groups, edge_reach_b_px=PAGE_EDGE_REACH_PX
        )


def test_find_continuation_candidate_uses_each_page_s_own_edge_reach():
    """A real corpus's pages need not share a height -- and thus not an edge reach.

    Page A's trailing group sits 3px from its bottom; page B's leading group
    sits 5px from its top. A single shared `edge_reach_px` of 4 would have
    admitted the first and refused the second identically on both sides; the
    per-page values below prove each page's own reach is what is actually
    consulted.
    """
    page_a_h = 300
    trailing = body_component(page_a_h - 3 - 20, 20)  # bottom sits 3px above the edge
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    leading = body_component(5, 30)  # top sits 5px below the edge
    page_b_groups = group([leading], PAGE_W, page_a_h)

    assert (
        find_continuation_candidate(
            page_a_groups,
            page_a_h,
            page_b_groups,
            edge_reach_a_px=3,
            edge_reach_b_px=4,
        )
        is None
    )
    candidate = find_continuation_candidate(
        page_a_groups,
        page_a_h,
        page_b_groups,
        edge_reach_a_px=3,
        edge_reach_b_px=5,
    )
    assert candidate is not None
    assert candidate["page_a_group"]["body_members"] == [trailing]
    assert candidate["page_b_group"]["body_members"] == [leading]

    # Page A must be checked against its *own* reach, not page B's. Page A's
    # 3px gap exceeds a 2px reach of its own even though page B's 5px gap
    # passes at edge_reach_b_px=5 -- a mutant that used edge_reach_b_px for
    # both pages would wrongly return a candidate here.
    assert (
        find_continuation_candidate(
            page_a_groups,
            page_a_h,
            page_b_groups,
            edge_reach_a_px=2,
            edge_reach_b_px=5,
        )
        is None
    )
