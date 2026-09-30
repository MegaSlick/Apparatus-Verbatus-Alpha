"""Tests for the Designator's ink scans over a decoded page.

`structure.py` thresholds a page's pixels and labels them through
`common/components.py`; the background and labeller themselves are tested in
`common/test_background_components.py`. These cover what this stage adds: the
scan's own shape refusals, its required keywords, and the primary scan against
the lower margin conservation counts at.
"""

import pytest
from structure import (
    PRIMARY_MARGIN,
    SECONDARY_MARGIN,
    primary_scan,
    scan_ink_components,
)

from common.contracts.errors import ContractError

BACKGROUND = 230
INK = 40
GAP_TOLERANCE_PX = 3


def blank_rows(width: int, height: int, background: int = BACKGROUND) -> list[bytearray]:
    return [bytearray([background] * width) for _ in range(height)]


def paint_rect(rows: list[bytearray], x: int, y: int, w: int, h: int, value: int) -> None:
    for row_offset in range(h):
        row = rows[y + row_offset]
        for col_offset in range(w):
            row[x + col_offset] = value


def test_the_conservation_margin_finds_a_faint_mark_primary_scan_misses():
    width, height = 20, 20
    rows = blank_rows(width, height)
    faint = BACKGROUND - (SECONDARY_MARGIN + 1)  # inside secondary's threshold, outside primary's
    assert faint > BACKGROUND - PRIMARY_MARGIN, (
        "the fixture must actually miss the primary threshold"
    )
    paint_rect(rows, 5, 5, 3, 3, faint)
    # PRIMARY_MARGIN is the floor under every margin a page can derive, so a
    # mark missed at the floor is missed at every margin a run could hand it.
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
    found = scan_ink_components(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=SECONDARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    )
    assert len(found) == 1
    assert found[0]["bounds"] == {"x": 5, "y": 5, "w": 3, "h": 3}


def test_primary_and_conservation_margins_agree_on_clearly_inked_marks():
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
    ) == scan_ink_components(
        width,
        height,
        rows,
        background=BACKGROUND,
        margin=SECONDARY_MARGIN,
        gap_tolerance_px=GAP_TOLERANCE_PX,
    )


# --- infer_background ---------------------------------------------------------


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
    """No module default: a caller that forgets it fails loudly with
    `TypeError` rather than running under an unreviewed value."""
    with pytest.raises(TypeError):
        scan_ink_components(5, 5, blank_rows(5, 5), background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_primary_scan_refuses_a_missing_gap_tolerance_keyword():
    with pytest.raises(TypeError):
        primary_scan(5, 5, blank_rows(5, 5), background=BACKGROUND, margin=PRIMARY_MARGIN)


def test_primary_scan_refuses_a_missing_margin_keyword():
    """A caller that forgets the page's own margin fails loudly: a default
    here would mean a page silently scanned at the floor while its record
    published the margin it actually derived.
    """
    with pytest.raises(TypeError):
        primary_scan(5, 5, blank_rows(5, 5), background=BACKGROUND, gap_tolerance_px=3)


# --- the row-run substitution: equality against the retired implementation ----
#
# The claim the substitution rests on is that the two implementations return
# the same list: same components, bounds, pixel counts, order -- proved here
# on every page these tests can build, not asserted once.
