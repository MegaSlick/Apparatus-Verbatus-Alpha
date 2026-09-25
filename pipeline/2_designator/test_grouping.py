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
    fallback_tiles,
    find_continuation_candidate,
    group_page,
    partition_page_spanning,
)
from structure import infer_background_evidence, primary_scan

from common.contracts.errors import ContractError

PAGE_W, PAGE_H = 200, 300

# Passed explicitly by every test below, since group_page no longer carries
# these as module defaults.
MARGIN_PX = 30  # 0.15 * PAGE_W
CHAIN_GAP_PX = 6
ANCHOR_REACH_PX = 2
BRACE_MIN_HEIGHT_PX = 30
PAGE_EDGE_REACH_PX = 4
# The sealed [grouping.page_area_bp] value, spelled here rather than loaded so
# these tests stay a test of the module, not the config. test_grouping_config.py
# is what holds the two to the same number.
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

    Every other fixture in this file puts components far enough from the
    boundary that MARGIN_PX could silently drift and still pass; this pins it
    directly, one component just inside the 30px boundary, one just outside.
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
    """The margin threshold is validated whether or not there is ink to sort,
    so a bad sealed margin cannot pass silently on an ink-free page.
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
    """The active run reaches the greatest bottom edge seen, not merely the
    last one: replacing it with the wholly-contained middle component's
    bottom would falsely split the third component at the chain-gap limit.
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
    """One physical marginal glyph, two real acts.

    A single anchor tall enough to span both body runs must not fold them
    into one act, nor be claimed by only one of them, leaving the other with
    no evidence. Both groups carry the brace; their body text stays separate.
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
    """The brace fixture above, but through decoded pixels rather than
    hand-built component dicts.

    Every other test in this file states its geometry directly and never
    calls `structure.primary_scan`. This is the one place `run.py`'s real
    chain (`primary_scan` -> `group_page`) runs over real decoded pixels, so
    it is the one test that would catch `primary_scan` merging the anchor
    into a body run or splitting the brace below `BRACE_MIN_HEIGHT_PX`.
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
    # Taken from the same evidence object run.py::_analyze_page takes it
    # from, not a hand-copied literal, so this stays the same scan as the real one.
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

    A gap-only chainer would fuse this into one run; grouping must use the
    margin anchor starting mid-run even though there is no whitespace to find.
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

    A strict, zero-tolerance boundary comparison would count a body line
    starting a pixel above its own anchor as belonging to the previous zone
    and silently merge the two acts; `anchor_reach_px` slack must apply here too.
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

    (candidate,) = continuation(page_a_groups, page_a_h, page_b_groups)
    assert candidate["page_a_group"]["body_members"] == [trailing]
    assert candidate["page_b_group"]["body_members"] == [leading]


def test_an_anchored_leading_group_is_a_new_act_not_a_continuation():
    page_a_h = 300
    trailing = body_component(page_a_h - 20, 20)
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    anchor = margin_component(0)
    leading = body_component(0, 30)
    page_b_groups = group([anchor, leading], PAGE_W, page_a_h)

    assert continuation(page_a_groups, page_a_h, page_b_groups) == []


def test_a_trailing_group_far_from_the_bottom_edge_is_not_a_continuation():
    page_a_h = 300
    trailing = body_component(100, 20)  # nowhere near the bottom
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    leading = body_component(0, 30)
    page_b_groups = group([leading], PAGE_W, page_a_h)

    assert continuation(page_a_groups, page_a_h, page_b_groups) == []


def test_columns_that_do_not_overlap_are_not_a_continuation():
    page_a_h = 300
    trailing = component(5, page_a_h - 20, 20, 20)  # far left, touches bottom
    page_a_groups = group([trailing], PAGE_W, page_a_h)

    leading = component(150, 0, 20, 20)  # far right, touches top
    page_b_groups = group([leading], PAGE_W, page_a_h)

    assert continuation(page_a_groups, page_a_h, page_b_groups) == []


def test_no_continuation_when_either_page_marked_out_nothing():
    assert continuation([], 300, [component(0, 0, 10, 10)]) == []
    assert continuation([component(0, 0, 10, 10)], 300, []) == []


def _edge_group(x: int, y: int, w: int, h: int, *, anchored: bool = False) -> dict:
    """A group as `group_page` returns it, built directly: the scan merges
    side-by-side components into one row group, so separate columns and a
    detached folio box are stated here rather than grown from pixels."""
    member = component(x, y, w, h)
    return {
        "bounds": member["bounds"],
        "body_members": [member],
        "anchors": [member] if anchored else [],
        "rationale": "test group",
    }


def test_two_columns_crossing_one_page_break_are_two_candidates():
    page_a = [_edge_group(20, 200, 70, 100), _edge_group(110, 220, 70, 80)]
    page_b = [_edge_group(20, 0, 70, 40), _edge_group(110, 2, 70, 60)]
    pairs = continuation(page_a, PAGE_H, page_b)
    assert [
        (pair["page_a_group"]["bounds"]["x"], pair["page_b_group"]["bounds"]["x"]) for pair in pairs
    ] == [
        (20, 20),
        (110, 110),
    ]


def test_a_folio_number_at_the_bottom_edge_does_not_hide_the_crossing():
    """The folio box is the lowest group on the page and shares no column with
    the next page's opening; comparing only the lowest group would miss the act."""
    act = _edge_group(20, 150, 140, 148)
    folio = _edge_group(175, 290, 15, 10)
    leading = _edge_group(20, 0, 140, 40)
    (pair,) = continuation([act, folio], PAGE_H, [leading])
    assert pair["page_a_group"] is act
    assert pair["page_b_group"] is leading


def test_an_anchored_group_at_the_top_edge_does_not_hide_an_unanchored_one():
    trailing = _edge_group(20, 250, 160, 50)
    heading = _edge_group(20, 0, 60, 20, anchored=True)
    tail = _edge_group(100, 1, 80, 30)
    (pair,) = continuation([trailing], PAGE_H, [heading, tail])
    assert pair["page_b_group"] is tail


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
    """No module default: a caller that forgets one fails loudly with
    `TypeError` rather than silently running under an unreviewed value.
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
    """The one geometric decision here whose failure is an act cut in half: a
    float or negative reach would silently change whether an act runs on
    across a page break.
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
    """A real corpus's pages need not share a height, and thus not an edge
    reach: a single shared `edge_reach_px` couldn't distinguish page A's 3px
    gap from page B's 5px one, so the per-page values must each be consulted.
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
        == []
    )
    (candidate,) = find_continuation_candidate(
        page_a_groups,
        page_a_h,
        page_b_groups,
        edge_reach_a_px=3,
        edge_reach_b_px=5,
    )
    assert candidate["page_a_group"]["body_members"] == [trailing]
    assert candidate["page_b_group"]["body_members"] == [leading]

    # Page A's 3px gap exceeds its own 2px reach even though page B's 5px gap
    # passes; a mutant using edge_reach_b_px for both pages would wrongly
    # return a candidate here.
    assert (
        find_continuation_candidate(
            page_a_groups,
            page_a_h,
            page_b_groups,
            edge_reach_a_px=2,
            edge_reach_b_px=5,
        )
        == []
    )


def test_find_continuation_candidate_shares_a_column_with_no_slack_at_all():
    """The column-share test is plain intersection: no tolerance, in either
    direction. `column_overlap_px` no longer exists as a keyword at all
    (rather than defaulting to zero), which this test asserts by passing it
    and expecting `TypeError`.

    `_x_range` reports the half-open pixel span `[x, x+w)`, so two ranges that
    only touch (one ends exactly where the other starts) share no pixel
    column and must not corroborate a continuation; a one-pixel real overlap
    must.
    """
    trailing = component(40, 280, 60, 20)  # x-range [40, 100): pixel columns 40..99
    page_a_groups = group([trailing], PAGE_W, PAGE_H)

    touching = component(100, 0, 60, 30)  # x-range [100, 160): first column 100, no shared pixel
    assert continuation(page_a_groups, PAGE_H, group([touching], PAGE_W, PAGE_H)) == []

    overlapping = component(99, 0, 60, 30)  # x-range [99, 159): shares column 99 with trailing
    assert continuation(page_a_groups, PAGE_H, group([overlapping], PAGE_W, PAGE_H)) != []

    apart = component(101, 0, 60, 30)  # x-range [101, 161]: one pixel clear of it
    assert continuation(page_a_groups, PAGE_H, group([apart], PAGE_W, PAGE_H)) == []

    with pytest.raises(TypeError):
        continuation(page_a_groups, PAGE_H, group([apart], PAGE_W, PAGE_H), column_overlap_px=1)


# --- the page-spanning bound ------------------------------------------------
#
# What these pin: a component covering the sealed fraction of the page is
# withheld from column assignment and body chaining, so it cannot weld two
# acts together -- withheld from grouping only, never removed from ink counts.


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

    The area is floor-divided and the comparison is `>=`, so rounding only
    ever moves a box down, toward being grouped, and the bound still holds
    inclusively at its own stated value.
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

    A wide band across two anchored acts lands in the body column and overlaps
    both anchors. Just under the bound (4966bp) it comes back as its own group
    swallowing both acts and claiming both anchors; at the bound (exactly
    5000bp) it is withheld and the two acts are all that come back.
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
    """10000 is the largest legal value and it still withholds a whole-page
    box: the comparison is `>=`, so no config edit that looks like widening
    the bound can ever disarm it.
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
    """Withheld is not excluded: both halves come back, and their union is the
    input. A component set aside here is still ink: still inside
    `conservation.reconcile`'s rescan, still mintable as a held act.
    """
    parts = [_bezel(), body_component(20, 30), margin_component(20)]
    grouped, withheld = partition_page_spanning(
        parts, PAGE_W, PAGE_H, page_spanning_area_bp=PAGE_SPANNING_AREA_BP
    )
    assert sorted(map(id, grouped + withheld)) == sorted(map(id, parts))


def test_a_page_of_nothing_but_bezel_groups_to_nothing_so_the_fallback_grid_fires():
    """The guaranteed fallback grid is re-armed by this bound.

    Before it, `group_page` returned bezel-welded groups, so the page read as
    `detected` and the fallback grid never ran. A page whose only component
    spans it now returns no groups, which `run.py` reads as `fallback-tiles`;
    both halves (the empty result and `fallback_tiles` covering the page) are
    asserted since either alone wouldn't prove the point. What this cannot
    check from here is `run.py` making that call; `test_structure_failure.py`
    is where the live path's `structure_evidence` is pinned.
    """
    assert group([_bezel()]) == []

    tiles = fallback_tiles(PAGE_W, PAGE_H, bands=4, overlap_px=8)
    assert len(tiles) == 4
    covered = set()
    for tile in tiles:
        bounds = tile["bounds"]
        assert bounds["x"] == 0 and bounds["w"] == PAGE_W
        covered.update(range(bounds["y"], bounds["y"] + bounds["h"]))
        assert "fallback tile" in tile["rationale"]
    assert covered == set(range(PAGE_H))


def test_the_partition_is_invariant_under_input_order():
    """Principle 1: a partition, never an election.

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
    """Idempotent, which is what lets `run.py` call it beside `group_page`
    (which takes the partition again internally) without the two disagreeing.
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
    """Refused at both ends, and refused on an empty page too: a sealed
    policy nobody validated must not pass merely because the page had no ink.
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
    """The same component shape is withheld on a fixture page and on a scan,
    because the bound is a basis point of the page's own area, not a pixel
    count -- a pixel count would withhold every act on a large page and
    nothing on a small one.
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
