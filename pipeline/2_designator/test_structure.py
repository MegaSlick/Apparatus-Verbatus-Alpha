"""Tests for the ink connected-component structure pass.

Every page here is built directly, not through `proof/synthetic_pages.py`:
these tests own their own minimal, exact pixel layouts so a brace-linked-acts
or sub-threshold-mark case can be constructed at the single-pixel level
without disturbing the shared walking-skeleton fixture other stages depend on.
"""

import itertools

import pytest
from structure import (
    PRIMARY_MARGIN,
    SECONDARY_MARGIN,
    BackgroundInferenceRefusal,
    _ink_threshold,
    _label_components_reference,
    infer_background,
    ink_pixels,
    label_components,
    primary_scan,
    scan_ink_components,
    secondary_scan,
)

from common.contracts.errors import ContractError

BACKGROUND = 230
INK = 40
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
    assert (
        primary_scan(width, height, rows, background=BACKGROUND, gap_tolerance_px=GAP_TOLERANCE_PX)
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
        width, height, rows, background=BACKGROUND, gap_tolerance_px=GAP_TOLERANCE_PX
    ) == secondary_scan(
        width, height, rows, background=BACKGROUND, gap_tolerance_px=GAP_TOLERANCE_PX
    )


# --- infer_background ---------------------------------------------------------


def test_infer_background_is_the_most_common_pixel_value():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 2, 5, 5, INK)  # a minority of pixels
    assert infer_background(width, height, rows) == BACKGROUND


def test_infer_background_works_for_a_non_default_paper_colour():
    width, height = 20, 20
    paper = 200
    rows = [bytearray([paper] * width) for _ in range(height)]
    paint_rect(rows, 2, 2, 5, 5, INK)
    assert infer_background(width, height, rows) == paper


def test_infer_background_refuses_a_mismatched_scanline_shape():
    with pytest.raises(ContractError, match=r"expected 3 scanlines, got 2"):
        infer_background(10, 3, blank_rows(10, 2))


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
        infer_background(width, height, rows)


def test_an_inverted_scan_is_refused_rather_than_read_as_a_blank_page():
    """Light ink on dark paper is the same defect wearing a different cause."""

    width, height = 10, 10
    rows = [bytearray([30] * width) for _ in range(8)]
    rows += [bytearray([220] * width) for _ in range(2)]

    with pytest.raises(ContractError, match=r"the page is majority ink"):
        infer_background(width, height, rows)


def test_a_genuinely_blank_page_still_infers_its_paper_rather_than_being_refused():
    """The premise check must not fire on the case it exists to protect.

    A blank page's mode is its paper and its mean is that same value, so the
    comparison is an equality and passes. Zero ink here is honest: it is what the
    page has, proved from a background the page itself supplied.
    """

    width, height = 12, 12
    paper = 210
    rows = [bytearray([paper] * width) for _ in range(height)]

    assert infer_background(width, height, rows) == paper


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
        primary_scan(5, 5, blank_rows(5, 5), background=BACKGROUND)


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
        background = infer_background(width, height, rows)
        pixels = ink_pixels(width, height, rows, background=background, margin=margin)
        _both_labellers_agree(pixels, gap_tolerance_px)


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
        generator = random.Random((seed, density, gap_tolerance_px).__hash__())
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
