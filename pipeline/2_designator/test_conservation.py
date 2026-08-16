"""Tests for independent ink-vs-crop conservation accounting.

Every claim here is checked against pixels this test paints itself, never
against what a structure/grouping pass says it claimed -- that is the whole
point of this module, and a test that trusted the same claimed list the
reconciliation is supposed to be checking would not be testing anything.
"""

import random

import pytest
from conservation import reconcile
from structure import PRIMARY_MARGIN, SECONDARY_MARGIN, ink_pixels, label_components

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


def _legacy_reference(width, height, rows, claimed_bounds, gap_tolerance_px):
    """The old pixel-set algorithm, kept here solely as a U13 equivalence oracle."""
    pixels = ink_pixels(width, height, rows, background=BACKGROUND, margin=SECONDARY_MARGIN)
    claimed = {
        pixel
        for pixel in pixels
        if any(
            bounds["x"] <= pixel[0] < bounds["x"] + bounds["w"]
            and bounds["y"] <= pixel[1] < bounds["y"] + bounds["h"]
            for bounds in claimed_bounds
        )
    }
    components = label_components(pixels - claimed, gap_tolerance_px=gap_tolerance_px)
    return {
        "total_ink_pixel_count": len(pixels),
        "claimed_pixel_count": len(claimed),
        "residual_pixel_count": len(pixels - claimed),
        "residual_components": [
            {
                **component,
                "review_priority": "high"
                if max(component["bounds"]["w"], component["bounds"]["h"]) >= 6
                else "low",
            }
            for component in components
        ],
    }


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


@pytest.mark.parametrize(
    "claimed_bounds",
    [
        {"x": -1, "y": 0, "w": 2, "h": 2},
        {"x": 0, "y": 0, "w": 61, "h": 2},
        {"x": 0.5, "y": 0, "w": 2, "h": 2},
        {"x": 0, "y": 0, "w": 2, "h": 2, "extra": 1},
    ],
)
def test_claimed_bounds_must_be_a_closed_integer_rectangle_inside_the_page(claimed_bounds):
    width, height = 60, 40
    with pytest.raises(ContractError, match="claimed bounds"):
        reconcile(
            width,
            height,
            blank_rows(width, height),
            background=BACKGROUND,
            claimed_bounds=[claimed_bounds],
        )


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
    assert result["total_ink_pixel_count"] == 100 + 36 + 1
    assert result["claimed_pixel_count"] == 100


def test_row_oriented_u13_reimplementation_is_equivalent_to_the_retired_pixel_set_oracle():
    """Fixed randomized small pages cover gaps, overlaps, and residual topology."""
    generator = random.Random(20260816)
    width, height = 31, 23
    for gap_tolerance_px in (0, 1, 3):
        for _case in range(15):
            rows = blank_rows(width, height)
            for y in range(height):
                for x in range(width):
                    if generator.randrange(7) == 0:
                        rows[y][x] = INK
            claims = [
                {
                    "x": generator.randrange(width - 1),
                    "y": generator.randrange(height - 1),
                    "w": generator.randrange(1, 8),
                    "h": generator.randrange(1, 8),
                }
                for _ in range(4)
            ]
            for bounds in claims:
                bounds["w"] = min(bounds["w"], width - bounds["x"])
                bounds["h"] = min(bounds["h"], height - bounds["y"])
            assert reconcile(
                width,
                height,
                rows,
                background=BACKGROUND,
                claimed_bounds=claims,
                gap_tolerance_px=gap_tolerance_px,
            ) == _legacy_reference(width, height, rows, claims, gap_tolerance_px)


def test_row_oriented_reconciliation_matches_the_oracle_on_a_fully_inked_densely_claimed_page():
    """S1 breaker case: a fully-inked page with many heavily overlapping claims is
    the pathological input for _subtract_claims' run-splitting arithmetic -- every
    row is one giant ink run split by many overlapping intervals at once."""
    generator = random.Random(20260816)
    width, height = 25, 25
    rows = [bytearray([INK] * width) for _ in range(height)]
    for gap_tolerance_px in (0, 2):
        claims = [
            {
                "x": generator.randrange(width - 1),
                "y": generator.randrange(height - 1),
                "w": generator.randrange(1, 12),
                "h": generator.randrange(1, 12),
            }
            for _ in range(10)  # far more overlap than the 4-claim randomized suite
        ]
        for bounds in claims:
            bounds["w"] = min(bounds["w"], width - bounds["x"])
            bounds["h"] = min(bounds["h"], height - bounds["y"])
        assert reconcile(
            width,
            height,
            rows,
            background=BACKGROUND,
            claimed_bounds=claims,
            gap_tolerance_px=gap_tolerance_px,
        ) == _legacy_reference(width, height, rows, claims, gap_tolerance_px)


def test_claim_boundaries_inside_tolerated_ink_gaps_match_the_pixel_oracle():
    """V3: exhaust claim edges inside and across every gap the topology may merge."""
    width, height = 12, 1
    for gap_tolerance_px in (1, 2, 3):
        rows = blank_rows(width, height)
        left_x = 2
        right_x = left_x + gap_tolerance_px + 1
        paint_pixel(rows, left_x, 0)
        paint_pixel(rows, right_x, 0)
        for claim_x0 in range(width):
            for claim_x1 in range(claim_x0 + 1, width + 1):
                claims = [{"x": claim_x0, "y": 0, "w": claim_x1 - claim_x0, "h": 1}]
                assert reconcile(
                    width,
                    height,
                    rows,
                    background=BACKGROUND,
                    claimed_bounds=claims,
                    gap_tolerance_px=gap_tolerance_px,
                ) == _legacy_reference(width, height, rows, claims, gap_tolerance_px)


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


def test_conservation_defaults_to_the_faintest_structural_sensitivity():
    width, height = 20, 20
    rows = blank_rows(width, height)
    faint = BACKGROUND - (PRIMARY_MARGIN - 1)  # just inside background at primary sensitivity
    paint_rect(rows, 5, 5, 3, 3, faint)
    primary = reconcile(
        width,
        height,
        rows,
        background=BACKGROUND,
        claimed_bounds=[],
        margin=PRIMARY_MARGIN,
    )
    sensitive = reconcile(width, height, rows, background=BACKGROUND, claimed_bounds=[])
    assert primary["total_ink_pixel_count"] == 0
    assert sensitive["total_ink_pixel_count"] == 9
    assert SECONDARY_MARGIN < PRIMARY_MARGIN


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
    with pytest.raises(ContractError, match=r"claimed bounds .* falls outside"):
        reconcile(
            10,
            10,
            blank_rows(10, 10),
            background=BACKGROUND,
            claimed_bounds=[{"x": 0, "y": 0, "w": 0, "h": 5}],
        )
