"""Tests for the ink connected-component structure pass.

Every page here is built directly, not through `proof/synthetic_pages.py`:
these tests own their own minimal, exact pixel layouts so a brace-linked-acts
or sub-threshold-mark case can be constructed at the single-pixel level
without disturbing the shared walking-skeleton fixture other stages depend on.
"""

import pytest
from structure import (
    PRIMARY_MARGIN,
    SECONDARY_MARGIN,
    infer_background,
    label_components,
    primary_scan,
    scan_ink_components,
    secondary_scan,
)

from common.contracts.errors import ContractError

BACKGROUND = 230
INK = 40


def blank_rows(width: int, height: int, background: int = BACKGROUND) -> list[bytearray]:
    return [bytearray([background] * width) for _ in range(height)]


def paint_rect(rows: list[bytearray], x: int, y: int, w: int, h: int, value: int) -> None:
    for row_offset in range(h):
        row = rows[y + row_offset]
        for col_offset in range(w):
            row[x + col_offset] = value


def paint_pixel(rows: list[bytearray], x: int, y: int, value: int) -> None:
    rows[y][x] = value


# --- basic component detection ----------------------------------------------


def test_a_blank_page_has_no_components():
    width, height = 40, 30
    assert (
        scan_ink_components(
            width, height, blank_rows(width, height), background=BACKGROUND, margin=PRIMARY_MARGIN
        )
        == []
    )


def test_one_solid_rectangle_is_one_component_with_exact_geometry():
    width, height = 40, 30
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 6, INK)
    components = scan_ink_components(
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN
    )
    assert len(components) == 1
    assert components[0]["bounds"] == {"x": 5, "y": 5, "w": 10, "h": 6}
    assert components[0]["pixel_count"] == 60


def test_a_single_ink_pixel_is_its_own_one_by_one_component():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_pixel(rows, 7, 9, INK)
    components = scan_ink_components(
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN
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
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN
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
        width, height, rows, background=BACKGROUND, margin=PRIMARY_MARGIN
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

    # Rerun with the same pixels inserted in a different order (a fresh set
    # literal, same elements): the result must not depend on that order.
    reordered = {(3, 0), (2, 1), (0, 0), (1, 2), (0, 3)}
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
    assert primary_scan(width, height, rows, background=BACKGROUND) == []
    found = secondary_scan(width, height, rows, background=BACKGROUND)
    assert len(found) == 1
    assert found[0]["bounds"] == {"x": 5, "y": 5, "w": 3, "h": 3}


def test_primary_and_secondary_agree_on_clearly_inked_marks():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_rect(rows, 2, 2, 6, 6, INK)
    assert primary_scan(width, height, rows, background=BACKGROUND) == secondary_scan(
        width, height, rows, background=BACKGROUND
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


# --- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(0, 10), (10, 0), (-1, 10)])
def test_refuses_non_positive_dimensions(width, height):
    with pytest.raises(ContractError, match=r"a -?\d+x\d+ page has no pixels to scan"):
        scan_ink_components(width, height, [], background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_refuses_a_scanline_count_that_does_not_match_height():
    with pytest.raises(ContractError, match=r"expected 5 scanlines, got 3"):
        scan_ink_components(10, 5, blank_rows(10, 3), background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_refuses_a_scanline_whose_width_does_not_match():
    rows = blank_rows(10, 3)
    rows[1] = bytearray([BACKGROUND] * 5)
    with pytest.raises(ContractError, match=r"scanline 1 has width 5, expected 10"):
        scan_ink_components(10, 3, rows, background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_refuses_a_background_outside_the_8_bit_range():
    with pytest.raises(ContractError, match=r"background value 300 is not an 8-bit sample"):
        scan_ink_components(5, 5, blank_rows(5, 5), background=300, margin=PRIMARY_MARGIN)


def test_refuses_a_negative_margin():
    with pytest.raises(ContractError, match=r"sensitivity margin -1 is negative"):
        scan_ink_components(5, 5, blank_rows(5, 5), background=BACKGROUND, margin=-1)


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
