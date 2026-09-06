"""Tests for geometric act grouping: brace-linking, interleaved margins,
page-break continuation candidates, and the no-picker permutation property.

Every fixture here is a small, hand-built list of component dicts -- no image
decoding, no pixels -- so a grouping scenario can be stated as exactly the
geometry it needs to exercise.
"""

import itertools
import random
from pathlib import Path

import grouping_config
import pytest
from grouping import (
    assign_columns,
    find_continuation_candidate,
    group_page,
    partition_page_spanning,
)
from structure import infer_background_evidence, primary_scan

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
# The sealed `[grouping.page_area_bp]` value, spelled here rather than loaded so
# these tests stay a test of the module and not of the config. `test_grouping_
# config.py` is what holds the two to the same number.
PAGE_SPANNING_AREA_BP = 5000
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
        page_spanning_area_bp=PAGE_SPANNING_AREA_BP,
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


def test_assign_columns_refuses_a_non_integer_margin_px():
    with pytest.raises(
        ContractError, match=rf"margin 1\.5px is not between 0 and page width {PAGE_W}"
    ):
        assign_columns([], PAGE_W, margin_px=1.5)


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


@pytest.mark.parametrize("margin_px", [0, PAGE_W, -5, 1.5])
def test_group_page_refuses_a_bad_margin_even_with_no_components(margin_px):
    """The margin threshold is validated whether or not there is ink to sort.

    `group_page` short-circuits to `[]` on an empty page, and before this fix
    that short-circuit sat ahead of the margin check -- a sealed policy that
    resolved to an out-of-range or non-integer margin_px produced no refusal
    at all on an ink-free page, only a resolved_thresholds record naming a
    margin nothing had actually validated.
    """
    with pytest.raises(
        ContractError,
        match=rf"margin {margin_px}px is not between 0 and page width {PAGE_W}",
    ):
        group_page(
            [],
            PAGE_W,
            PAGE_H,
            margin_px=margin_px,
            chain_gap_px=CHAIN_GAP_PX,
            anchor_reach_px=ANCHOR_REACH_PX,
            brace_min_height_px=BRACE_MIN_HEIGHT_PX,
            page_spanning_area_bp=PAGE_SPANNING_AREA_BP,
        )


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

    evidence = infer_background_evidence(
        width,
        height,
        rows,
        background_policy=grouping_config.resolve_background_policy(
            grouping_config.load_grouping_config(
                Path(__file__).resolve().parents[2] / "config" / "designator_grouping.toml"
            ),
            width,
            height,
        ),
    )
    background_value = evidence["background"]
    # The margin the run would scan this page at, taken from the same evidence
    # object `run.py::_analyze_page` takes it from rather than written out as a
    # literal -- a hand-copied threshold is how this chain and the real one
    # would quietly stop being the same scan.
    ink_margin = evidence["ink_margin"]
    assert background_value == background, "ink must stay the numeric minority for this test"
    components = primary_scan(
        width,
        height,
        rows,
        background=background_value,
        margin=ink_margin,
        gap_tolerance_px=GAP_TOLERANCE_PX,
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
    [
        "margin_px",
        "chain_gap_px",
        "anchor_reach_px",
        "brace_min_height_px",
        "page_spanning_area_bp",
    ],
)
def test_group_page_refuses_a_missing_required_keyword(missing):
    """Every geometric parameter is required now that the module default is gone.

    A caller that forgets one of the five resolved ints fails loudly with
    `TypeError` at the call, rather than silently running under a value
    nobody reviewed for this page.
    """
    kwargs = {
        "margin_px": MARGIN_PX,
        "chain_gap_px": CHAIN_GAP_PX,
        "anchor_reach_px": ANCHOR_REACH_PX,
        "brace_min_height_px": BRACE_MIN_HEIGHT_PX,
        "page_spanning_area_bp": PAGE_SPANNING_AREA_BP,
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
        "page_spanning_area_bp": PAGE_SPANNING_AREA_BP,
    }
    kwargs[name] = bad_value
    with pytest.raises(ContractError, match="is not a non-negative integer"):
        group_page([component(0, 0, 5, 5)], PAGE_W, PAGE_H, **kwargs)


@pytest.mark.parametrize(
    "name,bad_value",
    [
        ("edge_reach_a_px", -1),
        ("edge_reach_a_px", 1.5),
        ("edge_reach_a_px", True),
        ("edge_reach_b_px", -1),
        ("edge_reach_b_px", 1.5),
        ("edge_reach_b_px", True),
    ],
)
def test_find_continuation_candidate_refuses_a_negative_or_non_integer_reach(name, bad_value):
    """The one geometric decision here whose failure is an act cut in half.

    `group_page` refuses its four thresholds by name; these two decide
    whether an act is judged to run on across a page break, and a float or a
    negative reach silently changes that answer rather than failing.
    """
    trailing = body_component(280, 20)
    leading = body_component(0, 30)
    page_a_groups = group([trailing], PAGE_W, PAGE_H)
    page_b_groups = group([leading], PAGE_W, PAGE_H)
    with pytest.raises(ContractError, match="is not a non-negative integer"):
        continuation(page_a_groups, PAGE_H, page_b_groups, **{name: bad_value})


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


def test_find_continuation_candidate_shares_a_column_with_no_slack_at_all():
    """The column-share test is plain intersection: no tolerance, in either direction.

    `find_continuation_candidate` used to take a `column_overlap_px` slack
    defaulting to zero that no caller ever passed -- a geometric policy in
    force on every page of every real run, sealed in no config and named in no
    inventory, under a module comment claiming no default was left in the file.
    It is gone rather than sealed, so what is asserted here is that zero means
    *no length in the test at all* rather than a length set to zero: the
    keyword itself is no longer accepted.

    `_x_range` reports the half-open pixel span `[x, x+w)`, so two ranges that
    only touch (one ends exactly where the other starts) share no pixel
    column and must not corroborate a continuation; a one-pixel real overlap
    must. Both are stated in physical pixel columns below, not endpoint
    arithmetic, so the fixture reads the same way the bug did: at pixel 99,
    the trailing group's last column, does the leading group also start
    there or one column later?
    """
    trailing = component(40, 280, 60, 20)  # x-range [40, 100): pixel columns 40..99
    page_a_groups = group([trailing], PAGE_W, PAGE_H)

    touching = component(100, 0, 60, 30)  # x-range [100, 160): first column 100, no shared pixel
    assert continuation(page_a_groups, PAGE_H, group([touching], PAGE_W, PAGE_H)) is None

    overlapping = component(99, 0, 60, 30)  # x-range [99, 159): shares column 99 with trailing
    assert continuation(page_a_groups, PAGE_H, group([overlapping], PAGE_W, PAGE_H)) is not None

    apart = component(101, 0, 60, 30)  # x-range [101, 161]: one pixel clear of it
    assert continuation(page_a_groups, PAGE_H, group([apart], PAGE_W, PAGE_H)) is None

    with pytest.raises(TypeError):
        continuation(page_a_groups, PAGE_H, group([apart], PAGE_W, PAGE_H), column_overlap_px=1)


# --- the page-spanning bound ------------------------------------------------
#
# What these pin is the property the module docstring states: a component whose
# bounding box covers the sealed fraction of the page is withheld from column
# assignment and body chaining, so it cannot weld two acts together -- and it is
# withheld from *grouping* only, never removed from anything that counts ink.
# The measurement behind the bound is in `config/designator_grouping.toml`'s
# `[grouping.page_area_bp.provenance]`: on all fourteen real photographed pages
# this project has measured, exactly one component's bounding box was the whole
# leaf and it held 69.5 to 95.2 percent of every ink pixel the scan counted.


def _bezel(page_w: int = PAGE_W, page_h: int = PAGE_H) -> dict:
    """A component with the page's own bounds: what a photographed bezel labels as."""
    return component(0, 0, page_w, page_h)


def test_a_page_spanning_component_is_withheld_and_a_small_one_is_not():
    bezel = _bezel()
    mark = body_component(20, 30)
    grouped, withheld = partition_page_spanning(
        [bezel, mark], PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    assert grouped == [mark]
    assert withheld == [bezel]


def test_the_bound_is_inclusive_at_its_own_value_and_exclusive_just_below():
    """A box exactly on the line is withheld; one basis point under it is not.

    The area is floor-divided, so the box just below the bound reads as under it
    -- the safe direction for a rule that withholds, because a component that is
    grouped is still reconciled while one that is withheld is not grouped at all.
    """
    # PAGE_W x PAGE_H is 200x300 == 60,000 px. Half of it is 30,000: a
    # 200x150 box is exactly 5000bp, a 200x149 box is 4966bp.
    on_the_line = component(0, 0, 200, 150)
    just_under = component(0, 0, 200, 149)
    grouped, withheld = partition_page_spanning(
        [on_the_line, just_under], PAGE_W, PAGE_H, page_spanning_area_bp=5000
    )
    assert withheld == [on_the_line]
    assert grouped == [just_under]


def test_a_page_spanning_component_stops_claiming_two_acts_at_the_bound():
    """The measured failure, reproduced at fixture scale on both sides of the bound.

    A wide band across two anchored acts is what a photographed bezel labels as:
    it lands in the body column, its y-range covers both acts, and every anchor
    on the page overlaps it. Just under the bound (200x149 on a 200x300 page,
    4966bp) it comes back as a group of its own whose bounds swallow both acts
    and which claims BOTH anchors -- the shape the real pages produced at scale,
    where one group held 440 body components and 14 anchors and its bounds were
    the leaf. At the bound (200x150, exactly 5000bp) it is withheld and the two
    acts are all that come back, identical to grouping them with no band there
    at all.
    """
    acts = [
        margin_component(20),
        body_component(20, 30),
        margin_component(100),
        body_component(100, 30),
    ]
    welding = group([component(0, 10, 200, 149)] + acts)
    assert len(welding) == 3
    swallower = next(g for g in welding if len(g["anchors"]) == 2)
    assert bound_key(swallower) == (0, 10, 200, 149)
    assert swallower["bounds"]["y"] <= 20
    assert swallower["bounds"]["y"] + swallower["bounds"]["h"] >= 130

    withheld = group([component(0, 10, 200, 150)] + acts)
    assert list(map(bound_key, withheld)) == list(map(bound_key, group(acts)))
    assert len(withheld) == 2
    assert [g["bounds"]["y"] for g in withheld] == [20, 100]


def test_the_bound_cannot_be_switched_off_by_setting_it_to_a_whole_page():
    """10000 is the largest legal value and it still withholds a whole-page box.

    The comparison is `>=`, so a component whose bounding box IS the page
    reaches even the top of the range. There is therefore no value of this
    policy under which a page-spanning component is connective tissue again --
    which is deliberate: the bound may be tightened or loosened, never disarmed
    by a config edit that looks like widening it.
    """
    assert (
        group_page(
            [_bezel()],
            PAGE_W,
            PAGE_H,
            margin_px=MARGIN_PX,
            chain_gap_px=CHAIN_GAP_PX,
            anchor_reach_px=ANCHOR_REACH_PX,
            brace_min_height_px=BRACE_MIN_HEIGHT_PX,
            page_spanning_area_bp=10000,
        )
        == []
    )


def test_a_withheld_component_is_returned_rather_than_dropped():
    """Withheld is not excluded: both halves come back, and their union is the input.

    This is the property `run.py` publishes the record from, and the reason the
    word in this module is *withheld*. Nothing here may quietly become a filter
    -- a component the grouping pass sets aside is still ink, still inside
    `conservation.reconcile`'s own rescan of the page, and still minted as a
    held act when no group claims it.
    """
    parts = [_bezel(), body_component(20, 30), margin_component(20)]
    grouped, withheld = partition_page_spanning(
        parts, PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    assert sorted(map(id, grouped + withheld)) == sorted(map(id, parts))


def test_a_page_of_nothing_but_bezel_groups_to_nothing_so_the_fallback_grid_fires():
    """The guaranteed fallback grid is re-armed by this bound.

    `SPEC_FINDINGS.md` 2026-09-06 item 4 recorded that on real pages
    `group_page` returned bezel-welded groups, so the page read as `detected`
    and the four-band grid Tyrel ruled for on 2026-08-11 never ran. A page whose
    only component spans it now returns no groups at all, which is what
    `run.py` reads as `fallback-tiles`.
    """
    assert group([_bezel()]) == []


def test_the_partition_is_invariant_under_input_order():
    """GOVERNANCE 3: a partition, never an election.

    Each component is measured against the page it sits on, independently of
    every other, so no presentation order can change which side it lands on.
    """
    parts = [_bezel(), body_component(20, 30), margin_component(20), body_component(150, 30)]
    first = partition_page_spanning(
        parts, PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    second = partition_page_spanning(
        list(reversed(parts)), PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    assert sorted(map(bound_key, first[0])) == sorted(map(bound_key, second[0]))
    assert sorted(map(bound_key, first[1])) == sorted(map(bound_key, second[1]))


def test_partitioning_an_already_partitioned_page_withholds_nothing_further():
    """Idempotent, which is what lets `run.py` call it beside `group_page`.

    `run.py` takes the partition itself to publish the withheld half on the
    page's conservation record, and `group_page` takes it again internally. The
    two cannot disagree only because a second application is a no-op.
    """
    parts = [_bezel(), body_component(20, 30)]
    grouped, withheld = partition_page_spanning(
        parts, PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    again, none = partition_page_spanning(
        grouped, PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    assert again == grouped
    assert none == []
    assert withheld  # the premise: something was withheld the first time


@pytest.mark.parametrize("bad_value", [0, -1, 10001, 1.5, True, "5000", None])
def test_a_page_spanning_bound_outside_one_to_ten_thousand_is_refused(bad_value):
    """Refused at both ends, and refused on an empty page too.

    At or below zero every component on every page spans the bound and the pass
    would withhold the whole page while returning a well-formed empty result;
    past a whole page nothing can reach it, so the policy would read as being in
    force while doing nothing. The empty-page call is the same reason
    `_check_margin` runs before the empty short-circuit: a sealed policy nobody
    validated must not pass merely because the page had no ink on it.
    """
    kwargs = {
        "margin_px": MARGIN_PX,
        "chain_gap_px": CHAIN_GAP_PX,
        "anchor_reach_px": ANCHOR_REACH_PX,
        "brace_min_height_px": BRACE_MIN_HEIGHT_PX,
        "page_spanning_area_bp": bad_value,
    }
    for components in ([], [component(0, 0, 5, 5)]):
        with pytest.raises(ContractError, match="basis points"):
            group_page(components, PAGE_W, PAGE_H, **kwargs)
    with pytest.raises(ContractError, match="basis points"):
        partition_page_spanning([], PAGE_W, PAGE_H, page_spanning_area_bp=bad_value)


def test_the_bound_is_a_fraction_of_area_so_it_means_the_same_thing_at_two_scales():
    """The same component shape is withheld on a fixture page and on a scan.

    A box covering half of a 200x300 page and the same box scaled to a
    2000x3000 one are one decision, which is the whole reason the value is a
    basis point of the page's own area rather than a pixel count. A pixel count
    here would withhold every act on a large page and nothing at all on a small
    one.
    """
    for scale in (1, 10):
        w, h = PAGE_W * scale, PAGE_H * scale
        half = component(0, 0, w, h // 2)
        quarter = component(0, 0, w // 2, h // 2)
        grouped, withheld = partition_page_spanning(
            [half, quarter], w, h, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
        )
        assert [bound_key({"bounds": c["bounds"]}) for c in withheld] == [(0, 0, w, h // 2)]
        assert [bound_key({"bounds": c["bounds"]}) for c in grouped] == [(0, 0, w // 2, h // 2)]
