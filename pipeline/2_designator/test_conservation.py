"""Tests for independent ink-vs-crop conservation accounting.

Every claim here is checked against pixels this test paints itself, never
against what a structure/grouping pass says it claimed -- that is the whole
point of this module, and a test that trusted the same claimed list the
reconciliation is supposed to be checking would not be testing anything.
"""

import pytest
from conservation import reconcile
from structure import PRIMARY_MARGIN

from common.contracts.errors import ContractError

BACKGROUND = 230
INK = 40


def blank_rows(width: int, height: int) -> list[bytearray]:
    return [bytearray([BACKGROUND] * width) for _ in range(height)]


def paint_rect(rows: list[bytearray], x: int, y: int, w: int, h: int, value: int = INK) -> None:
    for row_offset in range(h):
        row = rows[y + row_offset]
        for col_offset in range(w):
            row[x + col_offset] = value


def paint_pixel(rows: list[bytearray], x: int, y: int, value: int = INK) -> None:
    rows[y][x] = value


# --- exact reconciliation -----------------------------------------------------


def test_ink_fully_covered_by_claimed_bounds_reconciles_with_no_residual():
    width, height = 60, 40
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 10, INK)
    paint_rect(rows, 30, 20, 8, 8, INK)
    result = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[
            {"x": 0, "y": 0, "w": 20, "h": 20},
            {"x": 25, "y": 15, "w": 20, "h": 20},
        ],
    )
    assert result["residual_pixel_count"] == 0
    assert result["residual_components"] == []
    assert result["claimed_pixel_count"] == result["total_ink_pixel_count"] == 100 + 64


def test_claimed_plus_residual_always_equals_total():
    width, height = 80, 60
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 10, INK)
    paint_rect(rows, 40, 30, 6, 6, INK)
    paint_pixel(rows, 70, 55, INK)
    result = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[{"x": 0, "y": 0, "w": 20, "h": 20}],
    )
    assert (
        result["claimed_pixel_count"] + result["residual_pixel_count"]
        == result["total_ink_pixel_count"]
    )


def test_an_empty_page_reconciles_to_all_zeros():
    width, height = 30, 30
    result = reconcile(
        width, height, blank_rows(width, height), background=BACKGROUND, claimed_bounds=[]
    )
    assert result == {
        "total_ink_pixel_count": 0,
        "claimed_pixel_count": 0,
        "residual_pixel_count": 0,
        "residual_components": [],
    }


def test_overlapping_claimed_bounds_do_not_double_count():
    width, height = 40, 40
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 10, INK)
    result = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[
            {"x": 0, "y": 0, "w": 20, "h": 20},
            {"x": 3, "y": 3, "w": 20, "h": 20},  # heavily overlapping the first
        ],
    )
    assert result["claimed_pixel_count"] == 100
    assert result["residual_pixel_count"] == 0


def test_claimed_bounds_are_half_open_at_their_far_edge():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_pixel(rows, 10, 10, INK)  # exactly at x+w, y+h of the claimed box below
    result = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[{"x": 0, "y": 0, "w": 10, "h": 10}],
    )
    assert result["claimed_pixel_count"] == 0
    assert result["residual_pixel_count"] == 1


# --- uncovered ink is held, never dropped --------------------------------------


def test_an_uncovered_ink_band_produces_a_held_high_priority_residual():
    width, height = 60, 40
    rows = blank_rows(width, height)
    paint_rect(rows, 5, 5, 10, 10, INK)  # claimed
    paint_rect(rows, 40, 5, 10, 10, INK)  # never claimed: a real missed region
    result = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[{"x": 0, "y": 0, "w": 20, "h": 20}],
    )
    assert result["residual_pixel_count"] == 100
    assert len(result["residual_components"]) == 1
    residual = result["residual_components"][0]
    assert residual["bounds"] == {"x": 40, "y": 5, "w": 10, "h": 10}
    assert residual["review_priority"] == "high"


def test_a_one_pixel_mark_outside_every_claim_is_accounted_not_absent():
    width, height = 20, 20
    rows = blank_rows(width, height)
    paint_pixel(rows, 15, 15, INK)
    result = reconcile(width, height, rows, background=BACKGROUND, claimed_bounds=[])
    assert result["residual_pixel_count"] == 1
    assert len(result["residual_components"]) == 1
    only = result["residual_components"][0]
    assert only["bounds"] == {"x": 15, "y": 15, "w": 1, "h": 1}
    assert only["pixel_count"] == 1
    assert only["review_priority"] == "low"


def test_a_two_line_marginal_note_is_accounted_alongside_a_one_pixel_mark():
    """Two named sub-threshold fixtures, both surviving accounting at once."""
    width, height = 40, 40
    rows = blank_rows(width, height)
    paint_pixel(rows, 2, 2, INK)  # the one-character mark
    # A two-line note: two short painted rows close enough to merge into one
    # component under the default gap tolerance.
    paint_rect(rows, 20, 20, 4, 1, INK)
    paint_rect(rows, 20, 22, 4, 1, INK)
    result = reconcile(width, height, rows, background=BACKGROUND, claimed_bounds=[])
    assert result["residual_pixel_count"] == 1 + 8
    assert len(result["residual_components"]) == 2
    by_size = sorted(result["residual_components"], key=lambda c: c["pixel_count"])
    assert by_size[0]["pixel_count"] == 1
    assert by_size[1]["pixel_count"] == 8
    # Neither is missing: both appear regardless of the review-priority split.
    assert {c["bounds"]["x"] for c in result["residual_components"]} == {2, 20}


def test_review_priority_threshold_only_reorders_never_excludes():
    width, height = 30, 30
    rows = blank_rows(width, height)
    paint_pixel(rows, 5, 5, INK)
    lenient = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[],
        review_priority_min_dimension_px=1,
    )
    strict = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[],
        review_priority_min_dimension_px=1000,
    )
    assert len(lenient["residual_components"]) == len(strict["residual_components"]) == 1
    assert lenient["residual_components"][0]["review_priority"] == "high"
    assert strict["residual_components"][0]["review_priority"] == "low"
    # The pixel itself is identically accounted either way.
    assert lenient["residual_components"][0]["bounds"] == strict["residual_components"][0]["bounds"]


# --- secondary-sensitivity corroboration ---------------------------------------


def test_a_fainter_margin_finds_more_residual_than_the_primary_default():
    width, height = 20, 20
    rows = blank_rows(width, height)
    faint = BACKGROUND - (PRIMARY_MARGIN - 1)  # just inside background at primary sensitivity
    paint_rect(rows, 5, 5, 3, 3, faint)
    primary = reconcile(width, height, rows, background=BACKGROUND, claimed_bounds=[])
    sensitive = reconcile(width, height, rows, background=BACKGROUND, claimed_bounds=[], margin=1)
    assert primary["total_ink_pixel_count"] == 0
    assert sensitive["total_ink_pixel_count"] == 9


# --- refusals -------------------------------------------------------------------


def test_refuses_a_negative_review_priority_threshold():
    with pytest.raises(ContractError, match=r"review priority threshold -1 is negative"):
        reconcile(
            10,
            10,
            blank_rows(10, 10),
            background=BACKGROUND,
            claimed_bounds=[],
            review_priority_min_dimension_px=-1,
        )


def test_refuses_a_non_positive_claimed_rectangle():
    with pytest.raises(ContractError, match=r"claimed bounds .* are not a positive rectangle"):
        reconcile(
            10,
            10,
            blank_rows(10, 10),
            background=BACKGROUND,
            claimed_bounds=[{"x": 0, "y": 0, "w": 0, "h": 5}],
        )
