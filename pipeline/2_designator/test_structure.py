"""Tests for the ink connected-component structure pass.

Every page here is built directly, not through `proof/synthetic_pages.py`:
these tests own their own minimal, exact pixel layouts so a brace-linked-acts
or sub-threshold-mark case can be constructed at the single-pixel level
without disturbing the shared walking-skeleton fixture other stages depend on.
"""

import itertools
from pathlib import Path

import grouping_config
import pytest
from structure import (
    PRIMARY_MARGIN,
    SECONDARY_MARGIN,
    BackgroundInferenceRefusal,
    _derived_ink_margin,
    _ink_threshold,
    _label_components_reference,
    infer_background,
    infer_background_evidence,
    ink_pixels,
    label_components,
    primary_scan,
    scan_ink_components,
    secondary_scan,
)

from common.contracts.errors import ContractError

BACKGROUND = 230
INK = 40


# `infer_background` takes the sealed background-inference policy, resolved for the
# page in front of it. These tests drive the *shipped* policy rather than a
# convenient one, so a page here is inferred exactly as a run would infer it.
_SHIPPED_GROUPING_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "designator_grouping.toml"
)
_SHIPPED_POLICY = grouping_config.load_grouping_config(_SHIPPED_GROUPING_CONFIG)


def shipped_background_policy(width: int, height: int):
    return grouping_config.resolve_background_policy(_SHIPPED_POLICY, width, height)


GAP_TOLERANCE_PX = 3  # structure.py's retired DEFAULT_GAP_TOLERANCE_PX


def blank_rows(width: int, height: int, background: int = BACKGROUND) -> list[bytearray]:
    return [bytearray([background] * width) for _ in range(height)]


def paint_rect(rows: list[bytearray], x: int, y: int, w: int, h: int, value: int) -> None:
    for row_offset in range(h):
        row = rows[y + row_offset]
        for col_offset in range(w):
            row[x + col_offset] = value


def test_an_ink_threshold_below_every_8_bit_sample_is_refused():
    with pytest.raises(BackgroundInferenceRefusal, match="below every 8-bit sample"):
        _ink_threshold(10, 11)


def paint_pixel(rows: list[bytearray], x: int, y: int, value: int) -> None:
    rows[y][x] = value


# --- basic component detection ----------------------------------------------


def test_a_blank_page_has_no_components():
    width, height = 40, 30
    assert (
        scan_ink_components(
            width,
            height,
            blank_rows(width, height),
            background=BACKGROUND,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )
        == []
    )


def test_one_solid_rectangle_is_one_component_with_exact_geometry():
    width, height = 40, 30
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 6, INK)
    components = scan_ink_components(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=PRIMARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    )
    assert len(components) == 1
    assert components[0]["bounds"] == {"x": 5, "y": 5, "w": 10, "h": 6}
    assert components[0]["pixel_count"] == 60


def test_a_single_ink_pixel_is_its_own_one_by_one_component():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_pixel(rows, 7, 9, INK)
    components = scan_ink_components(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=PRIMARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    )
    assert len(components) == 1
    assert components[0]["bounds"] == {"x": 7, "y": 9, "w": 1, "h": 1}
    assert components[0]["pixel_count"] == 1


def test_two_well_separated_rectangles_are_two_components():
    width, height = 60, 40
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 2, 8, 6, INK)
    paint_rect(rows, 40, 20, 8, 6, INK)
    components = scan_ink_components(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=PRIMARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    )
    assert len(components) == 2
    assert {c["bounds"]["x"] for c in components} == {2, 40}


def test_components_are_returned_sorted_by_top_then_left():
    width, height = 60, 60
    rows = blank_rows(width, height)
    # Paint in an order deliberately different from the expected sort order.
    paint_rect(rows, 40, 40, 4, 4, INK)  # bottom-right
    paint_rect(rows, 2, 2, 4, 4, INK)  # top-left
    paint_rect(rows, 2, 40, 4, 4, INK)  # bottom-left
    components = scan_ink_components(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=PRIMARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    )
    origins = [(c["bounds"]["y"], c["bounds"]["x"]) for c in components]
    assert origins == sorted(origins)
    assert origins == [(2, 2), (40, 2), (40, 40)]


# --- gap-tolerant connectivity -----------------------------------------------


def test_a_gap_within_tolerance_merges_into_one_component():
    width, height = 40, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 5, 5, 5, INK)
    paint_rect(rows, 9, 5, 5, 5, INK)  # 2px gap: columns 7,8 unpainted
    components = scan_ink_components(
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN, gap_tolerance_px=3
    )
    assert len(components) == 1
    assert components[0]["bounds"] == {"x": 2, "y": 5, "w": 12, "h": 5}


def test_a_gap_beyond_tolerance_stays_two_components():
    width, height = 40, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 5, 5, 5, INK)
    paint_rect(rows, 20, 5, 5, 5, INK)  # 13px gap, far beyond tolerance
    components = scan_ink_components(
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN, gap_tolerance_px=3
    )
    assert len(components) == 2


def test_zero_gap_tolerance_still_requires_pixels_to_touch():
    """A tolerance of 0 bridges no gap at all: a 1px blank column keeps two
    blocks split. This does not test connectivity shape (4- vs 8-neighbour) --
    the blocks are row-aligned rectangles with no diagonal-only adjacency to
    tell the two apart. See `test_zero_gap_tolerance_still_connects_diagonal_neighbours`
    for that."""
    width, height = 40, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 5, 5, 5, INK)
    paint_rect(rows, 8, 5, 5, 5, INK)  # 1px gap: column 7 unpainted
    components = scan_ink_components(
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN, gap_tolerance_px=0
    )
    assert len(components) == 2


def test_zero_gap_tolerance_still_connects_diagonal_neighbours():
    """Connectivity here is 8-connected (Chebyshev radius), not 4-connected:
    two ink pixels touching only at a corner still union into one component
    even at zero gap tolerance. A pen stroke's own diagonal jitter must not
    scan as two separate marks."""
    width, height = 10, 10
    rows = blank_rows(width, height)
    paint_pixel(rows, 2, 2, INK)
    paint_pixel(rows, 3, 3, INK)  # touches (2, 2) only diagonally
    components = scan_ink_components(
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN, gap_tolerance_px=0
    )
    assert len(components) == 1


def test_two_components_sharing_a_top_left_origin_still_sort_deterministically():
    """`label_components` sorts by (top, left) only -- a component's own origin,
    not its full bounding box. Two disjoint components can share that origin
    while differing in every other respect: a lone pixel at (0, 0), and a
    four-pixel diagonal staircase from (3, 0) to (0, 3) whose own bounds also
    start at (0, 0). Neither touches the other (every cross-pixel Chebyshev
    distance is at least 2, above the zero-tolerance radius of 1), so both
    survive as separate components with a tied sort key. Without a tiebreak
    beyond (y, x), their relative order would fall back to `members.values()`'s
    dict-iteration order -- itself a function of Python's pixel-tuple hashing,
    not of the ink -- which is deterministic within one process but not a
    documented property a caller may rely on."""
    pixels = {(0, 0), (0, 3), (1, 2), (2, 1), (3, 0)}
    components = label_components(pixels, gap_tolerance_px=0)
    assert len(components) == 2
    assert [c["bounds"]["y"] for c in components] == [0, 0]
    assert [c["bounds"]["x"] for c in components] == [0, 0]  # both origins genuinely tie
    assert [c["bounds"] for c in components] == [
        {"x": 0, "y": 0, "w": 1, "h": 1},
        {"x": 0, "y": 0, "w": 4, "h": 4},
    ]
    assert [c["pixel_count"] for c in components] == [1, 4]

    # A set literal normalises insertion order away. Drive every order through
    # an ordered keys view so removal of the deterministic tiebreaker would make
    # at least one permutation disagree.
    for order in itertools.permutations(sorted(pixels)):
        reordered = dict.fromkeys(order).keys()
        assert label_components(reordered, gap_tolerance_px=0) == components


# --- primary vs secondary sensitivity ----------------------------------------


def test_secondary_scan_finds_a_faint_mark_primary_scan_misses():
    width, height = 20, 20
    rows = blank_rows(width, height)
    faint = BACKGROUND - (SECONDARY_MARGIN + 1)  # inside secondary's threshold, outside primary's
    assert faint > BACKGROUND - PRIMARY_MARGIN, (
        "the fixture must actually miss the primary threshold"
    )
    paint_rect(rows, 5, 5, 3, 3, faint)
    # `PRIMARY_MARGIN` is the floor under every margin a page can derive for
    # itself, so a mark this scan misses at the floor is missed at every margin
    # a run could hand it. That is what makes "the secondary adds recall" a
    # statement about the pair rather than about one calibration.
    assert (
        primary_scan(
            width,
            height,
            rows,
            background=BACKGROUND,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )
        == []
    )
    found = secondary_scan(
        width, height, rows, background=BACKGROUND, gap_tolerance_px=GAP_TOLERANCE_PX
    )
    assert len(found) == 1
    assert found[0]["bounds"] == {"x": 5, "y": 5, "w": 3, "h": 3}


def test_primary_and_secondary_agree_on_clearly_inked_marks():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 2, 6, 6, INK)
    assert primary_scan(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=PRIMARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    ) == secondary_scan(
        width, height, rows, background=BACKGROUND, gap_tolerance_px=GAP_TOLERANCE_PX
    )


# --- infer_background ---------------------------------------------------------


def test_infer_background_is_the_most_common_pixel_value():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 2, 5, 5, INK)  # a minority of pixels
    assert (
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
        == BACKGROUND
    )


def test_infer_background_works_for_a_non_default_paper_colour():
    width, height = 20, 20
    paper = 200
    rows = [bytearray([paper] * width) for _ in range(height)]
    paint_rect(rows, 2, 2, 5, 5, INK)
    assert (
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
        == paper
    )


def test_infer_background_refuses_a_mismatched_scanline_shape():
    with pytest.raises(ContractError, match=r"expected 3 scanlines, got 2"):
        infer_background(
            10, 3, blank_rows(10, 2), background_policy=shipped_background_policy(10, 3)
        )


def test_a_majority_ink_page_is_refused_rather_than_reconciling_to_zero_ink():
    """The one deferral that could lose a whole page in silence (06-3).

    `infer_background` takes the modal pixel as paper. On a page where ink is the
    numeric majority the mode *is* the ink, the threshold below it admits almost
    nothing, and the page reconciles to zero ink and exits `complete` having
    marked out no acts at all.

    Measured against the pre-fix implementation on exactly this page: background
    inferred as 30, and **0 ink pixels found out of the 60 that are there.**
    GOALS 1 -- a missed act is worse than a poorly read one -- and Tyrel's
    2026-08-04 ruling 15, that blank is proved and never inferred.
    """

    width, height = 10, 10
    rows = [bytearray([INK] * width) for _ in range(6)]
    rows += [bytearray([200] * width) for _ in range(4)]

    with pytest.raises(ContractError, match=r"the page is majority ink"):
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )


def test_an_inverted_scan_is_refused_rather_than_read_as_a_blank_page():
    """Light ink on dark paper is the same defect wearing a different cause."""

    width, height = 10, 10
    rows = [bytearray([30] * width) for _ in range(8)]
    rows += [bytearray([220] * width) for _ in range(2)]

    with pytest.raises(ContractError, match=r"the page is majority ink"):
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )


def test_a_genuinely_blank_page_still_infers_its_paper_rather_than_being_refused():
    """The premise check must not fire on the case it exists to protect.

    A blank page's mode is its paper and its mean is that same value, so the
    comparison is an equality and passes. Zero ink here is honest: it is what the
    page has, proved from a background the page itself supplied.
    """

    width, height = 12, 12
    paper = 210
    rows = [bytearray([paper] * width) for _ in range(height)]

    assert (
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
        == paper
    )


# --- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(0, 10), (10, 0), (-1, 10)])
def test_refuses_non_positive_dimensions(width, height):
    with pytest.raises(ContractError, match=r"a -?\d+x\d+ page has no pixels to scan"):
        scan_ink_components(
            width,
            height,
            [],
            background=BACKGROUND,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )


def test_refuses_a_scanline_count_that_does_not_match_height():
    with pytest.raises(ContractError, match=r"expected 5 scanlines, got 3"):
        scan_ink_components(
            10,
            5,
            blank_rows(10, 3),
            background=BACKGROUND,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )


def test_refuses_a_scanline_whose_width_does_not_match():
    rows = blank_rows(10, 3)
    rows[1] = bytearray([BACKGROUND] * 5)
    with pytest.raises(ContractError, match=r"scanline 1 has width 5, expected 10"):
        scan_ink_components(
            10,
            3,
            rows,
            background=BACKGROUND,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )


def test_refuses_a_background_outside_the_8_bit_range():
    with pytest.raises(ContractError, match=r"background value 300 is not an 8-bit sample"):
        scan_ink_components(
            5,
            5,
            blank_rows(5, 5),
            background=300,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )


def test_refuses_a_negative_margin():
    with pytest.raises(ContractError, match=r"sensitivity margin -1 is negative"):
        scan_ink_components(
            5,
            5,
            blank_rows(5, 5),
            background=BACKGROUND,
            margin=-1,
            gap_tolerance_px=GAP_TOLERANCE_PX,
        )


def test_refuses_a_negative_gap_tolerance():
    with pytest.raises(ContractError, match=r"gap tolerance -1 is negative"):
        scan_ink_components(
            5,
            5,
            blank_rows(5, 5),
            background=BACKGROUND,
            margin=PRIMARY_MARGIN,
            gap_tolerance_px=-1,
        )


def test_scan_ink_components_refuses_a_missing_gap_tolerance_keyword():
    """`gap_tolerance_px` lost its module default -- a caller that forgets it
    now fails loudly with `TypeError`, never runs under an unreviewed value."""
    with pytest.raises(TypeError):
        scan_ink_components(5, 5, blank_rows(5, 5), background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_label_components_refuses_a_missing_gap_tolerance_keyword():
    with pytest.raises(TypeError):
        label_components({(0, 0)})


def test_primary_scan_refuses_a_missing_gap_tolerance_keyword():
    with pytest.raises(TypeError):
        primary_scan(5, 5, blank_rows(5, 5), background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_primary_scan_refuses_a_missing_margin_keyword():
    """A caller that forgets the page's own margin fails loudly.

    The same rule `gap_tolerance_px` above is here under, and the reason is the
    same: `PRIMARY_MARGIN` used to be a default here, and a default would mean a
    page silently scanned at the floor while its record published the margin it
    derived. Two different thresholds, one of them written down.
    """
    with pytest.raises(TypeError):
        primary_scan(5, 5, blank_rows(5, 5), background=BACKGROUND, gap_tolerance_px=3)


def test_secondary_scan_refuses_a_missing_gap_tolerance_keyword():
    with pytest.raises(TypeError):
        secondary_scan(5, 5, blank_rows(5, 5), background=BACKGROUND)


# --- the row-run substitution: equality against the retired implementation ----
#
# `label_components` was replaced by a row-run union-find on measurement (383 s
# and 2.17 GB for one 8.7-megapixel photographed page at the sealed
# `gap_tolerance_px = 3`; `workbench/active/TIMING_REPORT_2026-09-05.md` §1b).
# The claim the substitution rests on is that the two implementations return the
# *same list*: same components, same bounds, same pixel counts, same order. That
# claim is proved here on every page these tests can build, not asserted once.


def _both_labellers_agree(pixels, gap_tolerance_px: int) -> list:
    produced = label_components(pixels, gap_tolerance_px=gap_tolerance_px)
    expected = _label_components_reference(pixels, gap_tolerance_px=gap_tolerance_px)
    assert produced == expected
    return produced


@pytest.mark.parametrize("gap_tolerance_px", [0, 1, 2, 3, 5, 8])
@pytest.mark.parametrize("margin", [PRIMARY_MARGIN, SECONDARY_MARGIN])
def test_the_row_run_labeller_matches_the_reference_on_every_fixture_page(margin, gap_tolerance_px):
    """Every walking-skeleton fixture page, at both declared sensitivities.

    These are the pages whose Designator evidence is pinned byte-for-byte
    downstream, so if the substitution moved a single component on any of them
    the acceptance pins would move with it.
    """
    from common.imaging import grayscale_rows
    from proof.synthetic_pages import ALL_PAGES, render_page

    for page in ALL_PAGES:
        width, height, rows = grayscale_rows(render_page(page))
        background = infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
        pixels = ink_pixels(width, height, rows, background=background, margin=margin)
        _both_labellers_agree(pixels, gap_tolerance_px)


def _a_large_page_of_ink(width: int, height: int) -> set:
    """A page-sized ink set: word-like strokes, solid blots, thousands of marks.

    Deliberately not a picture of a register -- `proof/synthetic_pages.py` is
    what this project builds pages with, and none of its pages is anywhere near
    this size. This is an abstract ink *set*, built to the one property the
    fixture pages cannot supply: enough runs, enough scanlines and enough
    separate components that the run-merge sweep, its forward pointer and the
    shared-origin tie-break are all exercised at scale rather than on twenty
    pixels. Strokes are 3-9 px wide with 5-9 px gaps, so the sealed
    `gap_tolerance_px = 3` leaves them apart and a wider tolerance welds them.
    """
    pixels = set()
    for row_top in range(20, height - 24, 24):
        x = 25
        seed = row_top
        while x < width - 25:
            seed = (seed * 1103515245 + 12345) % 2147483647
            run = 3 + seed % 7
            gap = 5 + (seed >> 5) % 5
            tall = 4 + (seed >> 9) % 5
            for dx in range(run):
                for dy in range(tall):
                    pixels.add((x + dx, row_top + dy))
            x += run + gap
    for blot_y in range(40, height - 40, 300):
        for blot_x in range(60, width - 60, 380):
            for dy in range(20):
                for dx in range(26):
                    pixels.add((blot_x + dx, blot_y + dy))
    return pixels


@pytest.mark.parametrize("gap_tolerance_px", [3, 8])
def test_the_row_run_labeller_matches_the_reference_on_one_page_sized_ink_set(gap_tolerance_px):
    """The scale leg, in the tree.

    The branch report's second equality leg ran on a real 1484x1103 review proxy
    -- 1,079,519 ink pixels, 241 components -- out of tree, because no page in
    this repository is anything like that size and no real page may enter it.
    That leg proved the claim once, in a session, on material this suite cannot
    re-read; nothing in the tree re-proved it. This does: 1200x950, about
    126,000 ink pixels and 3,340 components at the sealed tolerance, which is
    more components than the proxy carried and enough runs per scanline to make
    the sweep's dropped-prefix and stop conditions load-bearing. Two tolerances,
    the sealed one and the widest comparison point, at about 2.5 s of reference
    labelling each.
    """
    pixels = _a_large_page_of_ink(1200, 950)
    assert len(pixels) > 100_000, "the point of this test is the scale"
    components = _both_labellers_agree(pixels, gap_tolerance_px)
    assert len(components) > 500, "and that it resolves to many components, not one blob"
    assert sum(c["pixel_count"] for c in components) == len(pixels), (
        "every ink pixel lands in exactly one component"
    )


def test_a_repeated_pixel_is_tolerated_rather_than_split_into_two_runs():
    """`_ink_runs_by_row`'s docstring says duplicates are tolerated, and this is
    what says so. `label_components` declares a set, but the run builder sorts a
    per-row list and splits on `x > previous + 1`, so a repeated x is absorbed
    rather than ending a run -- and a caller handing the same pixel twice gets
    the same answer, not a crash and not a split component."""
    pixels = [(0, 0), (1, 0), (1, 0), (2, 0), (2, 0), (2, 0), (5, 0)]
    assert label_components(pixels, gap_tolerance_px=0) == label_components(
        set(pixels), gap_tolerance_px=0
    )
    components = label_components(pixels, gap_tolerance_px=0)
    assert [c["bounds"] for c in components] == [
        {"x": 0, "y": 0, "w": 3, "h": 1},
        {"x": 5, "y": 0, "w": 1, "h": 1},
    ]
    assert [c["pixel_count"] for c in components] == [3, 1]


def test_the_row_run_labeller_matches_the_reference_on_known_components():
    """A page whose components are known by construction, asserted outright.

    Equality against the reference proves the substitution changed nothing; it
    does not prove either implementation is right. This page's answer is written
    out independently of both: three marks, one of which is two rectangles a
    3-pixel blank gap apart on the same rows (joined at tolerance 3, separate at
    tolerance 2), and one single pixel two rows below the first mark's bottom
    edge (joined at tolerance 1, separate at tolerance 0).
    """
    width, height = 60, 50
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 6, INK)  # mark A
    paint_pixel(rows, 5, 13, INK)  # two blank rows below A's last row (10)
    paint_rect(rows, 30, 5, 8, 6, INK)  # mark B, left half
    paint_rect(rows, 41, 5, 8, 6, INK)  # mark B, right half: blank x in 38..40
    paint_rect(rows, 20, 30, 12, 9, INK)  # mark C

    def bounds_at(gap: int) -> list:
        pixels = ink_pixels(width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN)
        return [component["bounds"] for component in _both_labellers_agree(pixels, gap)]

    assert bounds_at(0) == [
        {"x": 5, "y": 5, "w": 10, "h": 6},
        {"x": 30, "y": 5, "w": 8, "h": 6},
        {"x": 41, "y": 5, "w": 8, "h": 6},
        {"x": 5, "y": 13, "w": 1, "h": 1},
        {"x": 20, "y": 30, "w": 12, "h": 9},
    ]
    assert bounds_at(1) == [
        {"x": 5, "y": 5, "w": 10, "h": 6},
        {"x": 30, "y": 5, "w": 8, "h": 6},
        {"x": 41, "y": 5, "w": 8, "h": 6},
        {"x": 5, "y": 13, "w": 1, "h": 1},
        {"x": 20, "y": 30, "w": 12, "h": 9},
    ]
    assert bounds_at(2) == [
        {"x": 5, "y": 5, "w": 10, "h": 9},  # the lone pixel has joined mark A
        {"x": 30, "y": 5, "w": 8, "h": 6},
        {"x": 41, "y": 5, "w": 8, "h": 6},
        {"x": 20, "y": 30, "w": 12, "h": 9},
    ]
    assert bounds_at(3) == [
        {"x": 5, "y": 5, "w": 10, "h": 9},
        {"x": 30, "y": 5, "w": 19, "h": 6},  # mark B's two halves have joined
        {"x": 20, "y": 30, "w": 12, "h": 9},
    ]


@pytest.mark.parametrize("gap_tolerance_px", [0, 1, 3, 5])
@pytest.mark.parametrize("density", [1, 5, 20, 60])
def test_the_row_run_labeller_matches_the_reference_on_randomised_pages(density, gap_tolerance_px):
    """Scattered ink at four densities, which is where an ordering tie is likely.

    A sparse page produces many single-pixel components that share origins and
    exercise the tie-break; a dense one produces few large ones and exercises
    the run-merge sweep. The seed is fixed so a failure is reproducible.
    """
    import random

    width, height = 70, 55
    for seed in range(6):
        generator = random.Random(f"{seed}-{density}-{gap_tolerance_px}")
        pixels = {
            (x, y)
            for y in range(height)
            for x in range(width)
            if generator.randrange(100) < density
        }
        _both_labellers_agree(pixels, gap_tolerance_px)


def test_the_row_run_labeller_matches_the_reference_on_the_shared_origin_page():
    """The one page whose expected order depends on the tie-break, both ways."""
    pixels = {(0, 0), (0, 3), (1, 2), (2, 1), (3, 0)}
    for gap_tolerance_px in (0, 1, 2, 3):
        _both_labellers_agree(pixels, gap_tolerance_px)


def test_the_row_run_labeller_matches_the_reference_on_negative_coordinates():
    """`label_components` is documented over an *arbitrary* pixel set.

    Nothing on the live path passes a negative coordinate -- `ink_pixels` only
    ever emits pixels inside the page -- but the contract does not exclude one,
    and the row sweep's earlier-scanline window is the place a `max(0, ...)`
    would have quietly changed the answer for a caller that did.
    """
    pixels = {(-4, -3), (-3, -3), (-3, -2), (2, -3), (0, 1), (1, 1)}
    for gap_tolerance_px in (0, 1, 2, 4):
        _both_labellers_agree(pixels, gap_tolerance_px)


def test_the_reference_labeller_is_reachable_and_refuses_the_same_way():
    """The oracle is real code, held to the same refusals as what replaced it."""
    assert _label_components_reference(set(), gap_tolerance_px=3) == []
    with pytest.raises(ContractError, match="gap tolerance -1 is negative"):
        _label_components_reference({(0, 0)}, gap_tolerance_px=-1)
    with pytest.raises(TypeError):
        _label_components_reference({(0, 0)})


# --- the dark surround: a photographed page is not a dark page ------------------
#
# Measured 2026-09-05: `infer_background` refused 7 of 7 real photographed
# register pages on the majority-ink branch, because 18-26% of each frame is
# black bezel and pure black is therefore the modal pixel. The live path cut all
# seven into blind fallback slabs with `ink_measurable: false` and reconciled
# none of their ink (`workbench/active/TIMING_REPORT_2026-09-05.md` §1a). These
# tests build that shape and the shapes it must still refuse.


def photographed_page(
    width: int,
    height: int,
    *,
    frame_x: int = 30,
    frame_y: int = 23,
    paper: int = 205,
    surround: int = 0,
    ink: int = 40,
) -> list[bytearray]:
    """A dark frame around a lighter interior carrying some writing.

    Deliberately not a picture of a register: an abstract frame, abstract paper
    tones and an abstract scatter of ink, which is all a shape test needs and all
    `proof/synthetic_pages.py` allows this project to build.

    **The paper is many tones, not one, and that is the point.** On a real
    photographed page the surround is the modal pixel only because the paper is
    spread across dozens of tones in the 180-240 band while the bezel is a single
    flat value; a synthetic page with one flat paper tone has that tone as its
    mode and never reaches the branch these tests exist for. So the interior here
    carries a dominant tone at `paper` and seven neighbours a little darker, and
    the assertions below check that the mode really is the surround before
    testing what the surround does.
    """
    rows = [bytearray([surround] * width) for _ in range(height)]
    for y in range(frame_y, height - frame_y):
        row = rows[y]
        for x in range(frame_x, width - frame_x):
            slot = (x * 13 + y * 7) % 11
            if slot == 0:
                row[x] = ink
            elif slot <= 3:
                row[x] = paper
            else:
                row[x] = paper - 10 + (slot - 4)
    return rows


def assert_the_surround_is_the_modal_pixel(width, height, rows) -> None:
    """The premise every test below rests on, asserted rather than assumed.

    Without this a change to `photographed_page` could make its paper the mode
    again, and every test here would pass through the ordinary modal branch
    while claiming to exercise the dark-surround one.
    """
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    assert max(range(256), key=lambda value: histogram[value]) == 0
    assert sum(v * c for v, c in enumerate(histogram)) // (width * height) > 0


def test_a_photographed_page_infers_its_paper_instead_of_refusing():
    width, height = 400, 300
    rows = photographed_page(width, height)
    assert_the_surround_is_the_modal_pixel(width, height, rows)

    policy = shipped_background_policy(width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)
    assert evidence["background"] == 205
    assert evidence["source"] == "inferred-interior-mode"
    surround = evidence["surround"]
    # The level is the page's own, and it is a valley rather than a spike: the
    # midpoint between the dark population's mode (0, the frame) and the light
    # population's mode (205, the paper). Asserted as the arithmetic rather than
    # as 102, so a change to `photographed_page`'s tones cannot leave this test
    # passing against a level it no longer describes.
    assert surround["dark_mode"] == 0
    assert surround["dark_at_or_below"] == (surround["dark_mode"] + 205) // 2
    assert surround["interior_dark_bp"] <= 5000
    # The border figure is published and decides nothing. On this page every
    # border pixel is frame, so it is the whole band.
    assert surround["border_dark_bp"] == 10000
    # The surround is measured, never removed: every one of those pixels is
    # still on the page and still below the ink threshold, so the scan counts
    # them. That is the safe direction (GOALS 1) and this is the number that
    # keeps a reader from taking the resulting ink fraction for writing.
    # The two counts bracket the bezel rather than either one being it. The
    # band is 5% of each dimension and this page's frame is wider than that, so
    # the band count is a lower bound; the page-wide count at the same level
    # also catches the interior's ink at 40, so it is an upper bound. Both are
    # published because one number here is how a reader takes the wrong one.
    band_x, band_y = policy["band_px_x"], policy["band_px_y"]
    assert surround["border_dark_pixel_count"] == sum(
        1
        for y in range(height)
        for x in range(width)
        if (x < band_x or x >= width - band_x or y < band_y or y >= height - band_y)
        and rows[y][x] <= surround["dark_at_or_below"]
    )
    frame_pixels = sum(1 for row in rows for value in row if value == 0)
    assert surround["border_dark_pixel_count"] < frame_pixels < surround["dark_pixel_count"]
    assert surround["dark_pixel_count"] == sum(
        1 for row in rows for value in row if value <= surround["dark_at_or_below"]
    )
    ink = ink_pixels(width, height, rows, background=205, margin=PRIMARY_MARGIN)
    assert (0, 0) in ink, "a corner of the surround must still be counted as ink"
    assert (width - 1, height - 1) in ink
    # `>=`, not `>`: this page carries no tone between the surround level (102)
    # and the ink threshold (185), so its dark set and its ink set coincide
    # exactly. On a real page they do not, and the subset assertion below is
    # what actually holds in both cases.
    assert len(ink) >= surround["dark_pixel_count"]
    # Every surround pixel, not merely most of them: the count above and the
    # threshold together are what make that true, and an inequality alone would
    # not have caught a test that dropped a strip.
    assert all((x, y) in ink for y in range(height) for x in range(width) if rows[y][x] == 0)
    # And every pixel the block calls dark is a pixel the scan calls ink, which
    # is what makes "this much of the counted ink is bezel" a true sentence.
    # `_dark_surround` caps the level at the ink threshold so this holds by
    # construction rather than by luck.
    assert surround["dark_at_or_below"] <= 205 - PRIMARY_MARGIN
    assert all(
        (x, y) in ink
        for y in range(height)
        for x in range(width)
        if rows[y][x] <= surround["dark_at_or_below"]
    )


def test_the_thin_wrapper_returns_the_same_value_as_the_evidence_function():
    width, height = 400, 300
    rows = photographed_page(width, height)
    policy = shipped_background_policy(width, height)
    assert (
        infer_background(width, height, rows, background_policy=policy)
        == infer_background_evidence(width, height, rows, background_policy=policy)["background"]
    )


def test_an_ordinary_page_still_reports_the_modal_source_and_no_surround():
    """The fixture path, unchanged. Every walking-skeleton page takes it, which
    is why no acceptance pin moves for the branch above.

    The two derivation fields are asserted as arithmetic rather than as
    literals: this page's dark population peaks at `INK` and its light one at
    `BACKGROUND`, and the sealed fraction of that distance is the margin. What
    matters for the fixtures is the line below it -- a synthetic page's ink and
    paper are 190 grey levels apart, so the floor and the derived margin select
    the identical pixels and nothing downstream can tell them apart.
    """
    width, height = 200, 260
    rows = blank_rows(width, height)
    paint_rect(rows, 20, 20, 160, 80, INK)
    policy = shipped_background_policy(width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)
    expected_margin = (BACKGROUND - INK) * policy["ink_margin_bp"] // 10000
    assert evidence == {
        "background": BACKGROUND,
        "source": "inferred-modal",
        "surround": None,
        "dark_mode": INK,
        "ink_margin": expected_margin,
    }
    assert expected_margin > PRIMARY_MARGIN, "this page must exercise the derivation, not the floor"
    assert ink_pixels(
        width, height, rows, background=BACKGROUND, margin=expected_margin
    ) == ink_pixels(width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN)


# --- the ink margin the page derives for itself --------------------------------
#
# `PRIMARY_MARGIN` was the threshold the primary scan ran at until 2026-09-06.
# On 127 real pages a fixed 20 grey levels below the paper *mode* landed inside
# the paper *population* -- a photographed leaf's tones spread over dozens of
# levels -- so between 28% and 66% of every real page counted as ink and the
# number could not be read (`DESIGNATOR_SURVEY_2026-09-06.md` section 6). The
# margin is now a sealed fraction of the distance between the page's own two
# population modes, and `PRIMARY_MARGIN` is its floor.


def test_the_ink_margin_is_a_sealed_fraction_of_the_pages_own_two_modes():
    width, height = 400, 300
    rows = photographed_page(width, height)  # frame 0, paper 205, ink 40
    policy = shipped_background_policy(width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)
    assert evidence["background"] == 205
    assert evidence["dark_mode"] == 0
    # Not a literal: the sealed fraction of this page's own 205-level span.
    assert evidence["ink_margin"] == (205 - 0) * policy["ink_margin_bp"] // 10000
    assert evidence["ink_margin"] > PRIMARY_MARGIN


def test_the_ink_margin_is_floored_at_primary_margin_where_the_two_modes_are_close():
    """A page whose populations are barely apart has no separation to scale by.

    Ten of the 127 calibration pages are here. The floor holds them at exactly
    the threshold they had before this unit, and it makes the whole change
    one-directional: a derived margin is never smaller than 20, so no page can
    start counting as ink anything it did not count as ink before.
    """
    width, height = 200, 260
    rows = blank_rows(width, height, background=200)
    paint_rect(rows, 20, 20, 40, 40, 190)  # ten levels down: a page with no contrast
    policy = shipped_background_policy(width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)
    assert evidence["background"] == 200
    assert evidence["dark_mode"] == 190
    assert (200 - 190) * policy["ink_margin_bp"] // 10000 < PRIMARY_MARGIN
    assert evidence["ink_margin"] == PRIMARY_MARGIN


def test_the_paper_mode_and_the_modal_background_are_one_value_on_that_branch():
    """The derivation reads `paper`; the modal branch publishes `background`.

    They are the same integer there and the code relies on it: a background that
    survives `mode * counted >= total` is at or above the page's own mean, so
    the global mode is also the mode of the population at or above the mean.
    Asserted on a page rather than argued, so a change to either selection
    cannot leave the two silently different.
    """
    width, height = 200, 260
    rows = blank_rows(width, height)
    paint_rect(rows, 20, 20, 160, 80, INK)
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    mean = sum(v * c for v, c in enumerate(histogram)) // (width * height)
    assert max(range(256), key=lambda v: histogram[v]) == max(
        range(mean, 256), key=lambda v: histogram[v]
    )


def test_a_paper_mode_below_the_dark_mode_is_refused_by_name():
    """The derivation's own precondition, guarded rather than assumed.

    `infer_background_evidence` can never produce this pair, but a caller could:
    the two modes are ordinary integers and transposing them would produce a
    negative margin, which *raises* the threshold above the paper value instead
    of lowering it -- a scan that counts almost the whole page as ink, quietly.
    """
    with pytest.raises(ContractError, match="not a page's own two populations"):
        _derived_ink_margin(40, 205, 3333)


def test_the_derived_threshold_never_falls_below_the_surround_level():
    """`SurroundEvidence`'s two counts are published as fractions of the ink.

    That reading is only true while every pixel the surround block counts is a
    pixel the scan counts too, which holds exactly while the derived threshold
    stays at or above the midpoint of the two modes. The loader refuses any
    `ink_margin_bp` at or past 5000 for this reason; here is the property it
    buys, on a page that actually reaches the branch.
    """
    width, height = 400, 300
    rows = photographed_page(width, height)
    policy = shipped_background_policy(width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)
    surround = evidence["surround"]
    threshold = evidence["background"] - evidence["ink_margin"]
    assert surround["dark_at_or_below"] <= threshold
    ink = ink_pixels(
        width, height, rows, background=evidence["background"], margin=evidence["ink_margin"]
    )
    assert surround["dark_pixel_count"] <= len(ink)
    assert surround["border_dark_pixel_count"] <= len(ink)


def test_a_page_whose_paper_spreads_over_many_tones_counts_far_less_of_itself_as_ink():
    """The measurement this unit exists for, on a shape test.

    `photographed_page`'s interior carries paper at seven tones between 195 and
    205 -- a tenth of the spread a real photographed leaf has, and enough to
    reach the branch. Widen it to sixty levels, as lighting and page curl do on
    a real one, and the floor margin puts most of the paper below the ink
    threshold while the derived margin puts none of it there.
    """
    width, height = 400, 300
    rows = photographed_page(width, height, paper=205, ink=40)
    # Re-tone the interior so the paper population spans 145..205 with its peak
    # at 205, instead of the seven tones `photographed_page` paints. The `min`
    # is what gives the population a mode rather than a plateau: a near-uniform
    # spread would leave `paper` decided by whichever value the argmax reached
    # first, and this test would then be about tie-breaking.
    for y in range(23, height - 23):
        row = rows[y]
        for x in range(30, width - 30):
            if row[x] != 40:
                row[x] = min(205, 145 + ((x * 13 + y * 7) % 80))
    policy = shipped_background_policy(width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)
    assert evidence["background"] == 205, "the premise: the paper population's peak"
    counted = width * height
    at_floor = len(
        ink_pixels(width, height, rows, background=evidence["background"], margin=PRIMARY_MARGIN)
    )
    at_derived = len(
        ink_pixels(
            width, height, rows, background=evidence["background"], margin=evidence["ink_margin"]
        )
    )
    # The floor counts the paper: over half the page, on a page whose frame and
    # writing together are barely a third of it.
    assert at_floor == sum(1 for row in rows for value in row if value <= 205 - PRIMARY_MARGIN)
    assert at_floor > counted * 0.6
    # The derived margin counts the frame and the writing and nothing else.
    assert at_derived == sum(1 for row in rows for value in row if value <= 40)
    assert at_derived < counted * 0.4


def test_the_background_probe_is_measured_at_the_floor_not_at_the_derived_threshold():
    """The bound that catches a wrong paper value must not read that value twice.

    This is the shape 6 of the 127 calibration pages have: a saturated highlight
    is the single most common value on a page whose real paper is spread across
    dozens of tones, so the modal branch takes the highlight for paper. At the
    *floor* threshold that value leaves 80% of the page below the ink threshold
    and `max_ink_bp` refuses it by name. At the page's own *derived* threshold it
    leaves 68%, inside the bound -- because the derivation reads the same wrong
    paper value as its upper end and slides the threshold down with it. That is
    the failure the bound exists for, made invisible, and it is why the probe
    stays at the floor.
    """
    width, height = 200, 260
    rows = []
    for y in range(height):
        if y < 65:  # 25% of the page: a photographic surround
            rows.append(bytearray((x % 11) for x in range(width)))
        elif y < 117:  # 20%: a blown highlight, and the page's modal value
            rows.append(bytearray([255] * width))
        else:  # 55%: the real paper, spread over forty tones
            rows.append(bytearray(140 + ((x * 7 + y * 13) % 41) for x in range(width)))
    policy = shipped_background_policy(width, height)
    counted = width * height

    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    assert max(range(256), key=lambda v: histogram[v]) == 255, "the premise: the highlight wins"
    mean = sum(v * c for v, c in enumerate(histogram)) // counted
    assert 255 >= mean, "the premise: so the majority-ink question is never asked"
    dark_mode = max(range(0, mean + 1), key=lambda v: histogram[v])
    derived = _derived_ink_margin(255, dark_mode, policy["ink_margin_bp"])
    assert derived > PRIMARY_MARGIN

    at_floor = sum(histogram[: 255 - PRIMARY_MARGIN + 1]) * 10000 // counted
    at_derived = sum(histogram[: 255 - derived + 1]) * 10000 // counted
    assert at_floor > policy["max_ink_bp"], "the bound must fire where it is asked"
    assert at_derived <= policy["max_ink_bp"], "and not where the derivation would ask it"

    with pytest.raises(BackgroundInferenceRefusal, match="is not a measurement of paper"):
        infer_background(width, height, rows, background_policy=policy)


def test_an_inverted_scan_is_still_refused_although_it_is_majority_dark():
    """The dark is everywhere, not in the frame, and the INTERIOR bound is what
    says so: 8333 bp of the interior is at or below the level, against the 5000
    this policy admits.

    Its border band measures 6578 bp -- *darker* than the border band of 9 of the
    72 real pages in the 127-page calibration -- which is exactly why the border
    bound this branch removed could never have separated them, and why this test
    now asserts the bound that actually decides. This is the shape a
    histogram-only repair would wrongly admit.
    """
    width, height = 200, 260
    rows = [bytearray([30] * width) for _ in range(height)]
    for y in range(int(height * 0.8), height):
        rows[y] = bytearray([220] * width)
    policy = shipped_background_policy(width, height)
    with pytest.raises(BackgroundInferenceRefusal, match=r"the page is majority ink"):
        infer_background(width, height, rows, background_policy=policy)
    # Which bound fires, shown rather than asserted from the message: widen the
    # interior bound alone and this page is *still* refused, now by `max_ink_bp`
    # -- 220 as paper would leave 8000 bp of the page below the threshold. Two
    # independent bounds refuse an inverted scan, and neither is load-bearing
    # alone. (The dark-core page below has only the first, which is why the two
    # tests together settle what each bound is for.)
    with pytest.raises(BackgroundInferenceRefusal, match=r"is not a measurement of paper"):
        infer_background(
            width, height, rows, background_policy={**policy, "max_interior_dark_bp": 10000}
        )


def test_a_dark_core_inside_a_light_border_is_still_refused():
    """The inverse arrangement: the dark is in the middle, 6647 bp of the
    interior at or below the level against the 5000 this policy admits. Its
    border measures 0, so on this page the removed border bound and the interior
    bound agreed; on the inverted scan above they did not, which is the pair that
    settles which of the two was doing the work."""
    width, height = 200, 260
    rows = [bytearray([25] * width) for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if y < 30 or y >= height - 30 or x < 30 or x >= width - 30:
                rows[y][x] = 210
    policy = shipped_background_policy(width, height)
    with pytest.raises(BackgroundInferenceRefusal, match=r"the page is majority ink"):
        infer_background(width, height, rows, background_policy=policy)
    assert (
        infer_background(
            width, height, rows, background_policy={**policy, "max_interior_dark_bp": 10000}
        )
        == 210
    )


def test_a_uniformly_dark_page_never_reaches_the_surround_test_at_all():
    """Its mode equals its mean, so the majority-ink branch does not fire and it
    is refused one branch later by the `PRIMARY_MARGIN` guard -- exactly as
    before this change. Named because the surround test would otherwise be
    credited with a refusal it does not make."""
    width, height = 200, 260
    rows = [bytearray([0] * width) for _ in range(height)]
    with pytest.raises(BackgroundInferenceRefusal, match=r"darker than the 20-point ink margin"):
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )


def test_a_dark_surround_whose_interior_is_too_dark_to_threshold_still_refuses():
    """A framed page whose paper is darker than the ink margin. The shape test
    passes and the paper value still cannot express a threshold, so the refusal
    stands -- and it names the surround it found rather than pretending it saw
    none."""
    width, height = 400, 300
    rows = photographed_page(width, height, paper=15, ink=2)
    assert_the_surround_is_the_modal_pixel(width, height, rows)
    with pytest.raises(BackgroundInferenceRefusal, match=r"a dark surround was found"):
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )


def test_a_band_with_no_interior_to_compare_against_refuses_rather_than_guesses():
    """`_dark_surround` returns `None` when the band leaves no interior. The
    sealed loader refuses such a band outright, so this can only be reached by a
    caller passing its own policy -- and it must not silently infer."""
    width, height = 400, 300
    rows = photographed_page(width, height)
    degenerate = {
        **shipped_background_policy(width, height),
        "band_px_x": width // 2,
        "band_px_y": height // 2,
    }
    with pytest.raises(BackgroundInferenceRefusal, match=r"the page is majority ink"):
        infer_background(width, height, rows, background_policy=degenerate)


def test_the_surround_bound_actually_decides_the_outcome():
    """The interior bound is load-bearing: moving it past this page's own
    measurement flips the answer, so it is not a decoration.

    There is one bound here now, not two. `min_border_dark_bp` sat beside it
    until 2026-09-06 and is gone on measurement: over 127 real pages it refused
    52 of them, and it refused no control the interior bound does not
    (`pipeline/2_designator/HANDOFF.md`, the calibration tables).
    """
    width, height = 400, 300
    rows = photographed_page(width, height)
    policy = shipped_background_policy(width, height)
    assert "min_border_dark_bp" not in policy
    measured = infer_background_evidence(width, height, rows, background_policy=policy)["surround"]

    too_strict_interior = {**policy, "max_interior_dark_bp": measured["interior_dark_bp"] - 1}
    with pytest.raises(BackgroundInferenceRefusal, match=r"the page is majority ink"):
        infer_background(width, height, rows, background_policy=too_strict_interior)


def test_a_paper_value_that_leaves_the_page_mostly_ink_is_refused_by_name():
    """The quiet failure the seven-page calibration could not see.

    Measured on 6 of 127 real pages: the modal pixel is 255 -- a blown highlight
    or a saturated margin -- which is *lighter* than the mean, so the
    majority-ink question is never asked, 255 is taken as paper, and 71 to 85%
    of the page is counted as ink. `group_page` finds structure,
    `conservation.reconcile` balances exactly, residual is zero, and nothing in
    the record marks it (`DESIGNATOR_SURVEY_2026-09-06.md` §5).

    This page is that shape in miniature: a saturated frame at 255 around paper
    at 205, so the mode is 255, the modal branch takes it, and the threshold it
    implies puts the paper itself below the line.
    """
    width, height = 400, 300
    rows = photographed_page(width, height, surround=255)
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    assert max(range(256), key=lambda value: histogram[value]) == 255, (
        "the premise: the saturated frame is the modal pixel, so the plain modal "
        "branch is the one under test"
    )
    with pytest.raises(
        BackgroundInferenceRefusal, match=r"is not a measurement of paper"
    ) as refusal:
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
    assert "inferred-modal" in str(refusal.value), "the refusal names the branch that produced it"
    assert "7000" in str(refusal.value), "and the bound it passed"


def framed_page_with_a_faint_interior(
    width: int, height: int, *, frame_x: int = 30, frame_y: int = 23, paper: int = 205
) -> list[bytearray]:
    """A black frame around an interior whose paper mode is still 205 but most
    of whose tones fall below the threshold 205 implies.

    38% of the interior at `paper` -- fewer pixels than the frame, so the frame
    is still the page's mode and the surround branch is still the one under
    test -- and 62% spread across 120-184, thinly enough that no single tone
    beats the frame either. The surround test passes and the value it returns
    still leaves the page mostly ink.
    """
    rows = [bytearray([0] * width) for _ in range(height)]
    for y in range(frame_y, height - frame_y):
        row = rows[y]
        for x in range(frame_x, width - frame_x):
            slot = (x * 13 + y * 7) % 100
            row[x] = paper if slot < 38 else 120 + (slot % 65)
    return rows


def test_the_ink_bound_is_asked_of_the_surround_branch_too():
    """Not only of the modal branch. A framed page whose paper value still leaves
    most of the page below the threshold is refused for the same reason, and the
    refusal names the branch it came from."""
    width, height = 400, 300
    rows = framed_page_with_a_faint_interior(width, height)
    assert_the_surround_is_the_modal_pixel(width, height, rows)
    with pytest.raises(
        BackgroundInferenceRefusal, match=r"is not a measurement of paper"
    ) as refusal:
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
    assert "inferred-interior-mode" in str(refusal.value)


def test_a_page_photographed_against_a_light_surface_is_refused_by_name():
    """The shape the sealed caveat names and the sample contains no example of.

    Measured here rather than asserted there. A white bezel at 230 or above wins
    the mode, the paper below it falls under the threshold that value implies,
    and `max_ink_bp` refuses the page by name instead of publishing an ink
    fraction of 0.72 that reconciles exactly. Named as a SYNTHETIC measurement,
    because that is what it is.
    """
    width, height = 400, 300
    for frame in (255, 245, 230):
        rows = photographed_page(width, height, surround=frame)
        with pytest.raises(BackgroundInferenceRefusal, match=r"is not a measurement of paper"):
            infer_background(
                width, height, rows, background_policy=shipped_background_policy(width, height)
            )


def test_a_light_surround_close_to_the_paper_tone_is_not_caught_and_that_is_recorded():
    """The limit of the rule above, pinned so it cannot be discovered later.

    A surround only fifteen grey levels lighter than the paper is inferred *as*
    the paper: the mode is the frame, the frame clears the majority-ink test,
    and the ink fraction it implies is 0.46 -- inside `max_ink_bp`. Nothing in
    this design catches it. The consequence is a paper value 15 too high and an
    ink fraction correspondingly inflated, which is visible in the page's own
    record rather than silent, but it is not refused and the caveat says so.
    """
    width, height = 400, 300
    rows = photographed_page(width, height, surround=220)
    assert (
        infer_background(
            width, height, rows, background_policy=shipped_background_policy(width, height)
        )
        == 220
    ), "the surround, not the paper at 205 -- recorded as a known limit, not a pass"
