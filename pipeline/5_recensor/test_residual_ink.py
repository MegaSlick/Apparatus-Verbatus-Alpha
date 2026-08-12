"""Residual-ink page coverage: proof at the pixel level.

Each test builds its own small grayscale canvas directly (never the shared
`proof/skeleton_fixture.toml`, whose fixture pages are engineered so every
declared act's bounds exactly equal its painted ink -- by construction there is
no coverage gap anywhere in that fixture to detect). The point of this module is
exactly the geometry a hand-built canvas can prove without touching pipeline
wiring at all; `test_confirmed_blank.py`-style end-to-end proof belongs to a
scenario the Designator can genuinely miss something on, which the walking
skeleton's synthetic proposer does not yet support (see HANDOFF.md).
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from residual_ink import (  # noqa: E402
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    MINIMUM_FRACTION_OUTSIDE_COVERAGE,
    MINIMUM_INK_PIXELS,
    SUBSTANTIAL_INK_PIXELS,
    page_residual_ink,
    residual_ink,
)

from common.imaging import encode_grayscale_png  # noqa: E402

BACKGROUND = 230
INK = BACKGROUND - MINIMUM_CONTRAST_BELOW_BACKGROUND - 10  # comfortably past the contrast floor


def canvas(width: int, height: int) -> list[bytearray]:
    return [bytearray([BACKGROUND] * width) for _ in range(height)]


def paint(rows: list[bytearray], x: int, y: int, w: int, h: int, value: int = INK) -> None:
    for row_offset in range(h):
        row = rows[y + row_offset]
        for col_offset in range(w):
            row[x + col_offset] = value


def _straightforward_counts(
    width: int, height: int, rows: list[bytearray], covered: list[dict]
) -> tuple[int, int, int]:
    """`(background, total_ink, outside_ink)` the obvious, per-pixel way.

    The reference the module's C-level implementation is checked against. This
    is deliberately the slow, plainly-correct version -- one interpreted
    comparison per pixel, a full 256-bucket histogram list, `list.index(max())`
    for the mode -- exactly what `residual_ink` did before `bytes.translate`,
    `Counter` and `int.bit_count` replaced those loops for real-page speed.
    """
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    background = histogram.index(max(histogram))

    mask = bytearray(width * height)
    for bounds in covered:
        x0 = max(0, min(bounds["x"], width))
        y0 = max(0, min(bounds["y"], height))
        x1 = max(x0, min(bounds["x"] + bounds["w"], width))
        y1 = max(y0, min(bounds["y"] + bounds["h"], height))
        for y in range(y0, y1):
            for x in range(x0, x1):
                mask[y * width + x] = 1

    total_ink = outside_ink = 0
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            if background - value < MINIMUM_CONTRAST_BELOW_BACKGROUND:
                continue
            total_ink += 1
            if not mask[y * width + x]:
                outside_ink += 1
    return background, total_ink, outside_ink


def test_the_fast_counts_agree_with_a_straightforward_implementation():
    """The optimized counters must be a pure speed change, never a measurement one.

    `residual_ink` feeds a completeness guard: if these counts drift, a page
    with real uncovered ink can report clean, which is GOALS 1's worst failure
    arriving through an optimization nobody re-measured. Driven over pseudo-
    random pages -- fixed seed, so a failure reproduces exactly -- including
    overlapping and out-of-bounds covered regions, ink at the exact contrast
    boundary, and pages with no ink at all.
    """
    rng = random.Random(20260811)
    for case in range(60):
        width = rng.randint(1, 40)
        height = rng.randint(1, 40)
        # A background-dominant page most of the time (the real shape), and
        # occasionally an arbitrary one, so the mode's tie-breaking is exercised.
        if case % 5:
            rows = [
                bytearray(
                    rng.choice((BACKGROUND, BACKGROUND, BACKGROUND, INK, BACKGROUND - 40, 0, 255))
                    for _ in range(width)
                )
                for _ in range(height)
            ]
        else:
            rows = [bytearray(rng.randrange(256) for _ in range(width)) for _ in range(height)]

        covered = [
            {
                "x": rng.randint(-5, width),
                "y": rng.randint(-5, height),
                "w": rng.randint(0, width + 5),
                "h": rng.randint(0, height + 5),
            }
            for _ in range(rng.randint(0, 4))
        ]

        expected_background, expected_total, expected_outside = _straightforward_counts(
            width, height, [bytearray(row) for row in rows], covered
        )
        result = residual_ink(width, height, [bytearray(row) for row in rows], covered)

        context = f"case {case}: {width}x{height}, covered={covered}"
        assert result["background_level"] == expected_background, context
        assert result["total_ink_pixels"] == expected_total, context
        assert result["outside_ink_pixels"] == expected_outside, context


def test_ink_fully_inside_the_covered_region_is_not_flagged():
    rows = canvas(20, 20)
    paint(rows, 2, 2, 6, 6)
    result = residual_ink(20, 20, rows, covered=[{"x": 2, "y": 2, "w": 6, "h": 6}])
    assert result["outside_ink_pixels"] == 0
    assert result["flagged"] is False


def test_ink_entirely_outside_any_covered_region_is_flagged():
    rows = canvas(20, 20)
    paint(rows, 2, 2, 6, 6)  # 36 ink pixels, well past the pixel floor
    result = residual_ink(20, 20, rows, covered=[])
    assert result["total_ink_pixels"] == 36
    assert result["outside_ink_pixels"] == 36
    assert result["fraction_outside"] == 1.0
    assert result["flagged"] is True


def test_a_mark_below_the_minimum_pixel_count_is_not_flagged():
    """A few stray dark pixels -- scanner dust, a fold shadow -- read as noise,
    never as a missed act, however large a fraction of the (tiny) total ink
    on this otherwise-empty page they happen to be."""
    rows = canvas(40, 20)
    mark = MINIMUM_INK_PIXELS - 1
    paint(rows, 1, 1, mark, 1)
    result = residual_ink(40, 20, rows, covered=[])
    assert result["outside_ink_pixels"] == mark
    assert result["fraction_outside"] == 1.0
    assert result["flagged"] is False


def test_a_mark_at_exactly_the_minimum_pixel_count_is_flagged():
    rows = canvas(40, 20)
    paint(rows, 1, 1, MINIMUM_INK_PIXELS, 1)
    result = residual_ink(40, 20, rows, covered=[])
    assert result["outside_ink_pixels"] == MINIMUM_INK_PIXELS
    assert result["flagged"] is True


def test_a_small_fraction_of_heavily_covered_ink_is_not_flagged():
    """A real page's own residual mark can comfortably clear the pixel-count
    floor and still be an ordinary trace beside abundant, fully-covered ink --
    the fraction gate is what separates that from a genuinely missed region."""
    # Wide enough that ink -- covered and uncovered together -- stays a
    # minority of the page, so the background inference (a histogram mode)
    # is not itself confused by the covered block.
    rows = canvas(200, 200)
    paint(rows, 0, 0, 50, 40)  # 2000 covered ink pixels
    outside = MINIMUM_INK_PIXELS + 6  # 30: above the pixel floor
    paint(rows, 55, 0, outside, 1)
    result = residual_ink(200, 200, rows, covered=[{"x": 0, "y": 0, "w": 50, "h": 40}])
    assert result["outside_ink_pixels"] == outside
    assert result["fraction_outside"] < MINIMUM_FRACTION_OUTSIDE_COVERAGE
    assert result["flagged"] is False


def test_a_substantial_absolute_miss_is_flagged_even_where_the_fraction_gate_would_not():
    """The dense-page hole the fraction gate alone leaves open.

    A page carrying abundant, correctly-covered ink can leave plainly-real text
    outside every cut region and still sit under the 2% fraction threshold --
    a missed act reported as a clean page, GOALS 1's worst failure. The
    absolute gate is what catches it.
    """
    # Ink stays a minority of the page (202,000 of 960,000), so the background
    # inference is not itself confused -- this test is about the fraction gate's
    # dense-page hole, not about `_background_level`'s own majority-ink limit.
    rows = canvas(1200, 800)
    paint(rows, 0, 0, 400, 500)  # 200,000 covered ink pixels
    outside = SUBSTANTIAL_INK_PIXELS  # plainly real text, but only ~1% of the total
    paint(rows, 0, 600, outside // 4, 4)
    result = residual_ink(1200, 800, rows, covered=[{"x": 0, "y": 0, "w": 400, "h": 500}])

    assert result["outside_ink_pixels"] == outside
    # Under the fraction gate, which alone would have called this page clean.
    assert result["fraction_outside"] < MINIMUM_FRACTION_OUTSIDE_COVERAGE
    assert result["flagged"] is True


def test_a_large_enough_fraction_outside_coverage_is_flagged_even_with_other_ink_covered():
    rows = canvas(20, 20)
    paint(rows, 0, 0, 10, 10)  # 100 covered ink pixels
    paint(rows, 10, 10, 6, 6)  # 36 uncovered -- 36 / 136 > 2%
    result = residual_ink(20, 20, rows, covered=[{"x": 0, "y": 0, "w": 10, "h": 10}])
    assert result["fraction_outside"] > MINIMUM_FRACTION_OUTSIDE_COVERAGE
    assert result["flagged"] is True


def test_a_page_with_no_ink_at_all_is_never_flagged():
    rows = canvas(20, 20)
    result = residual_ink(20, 20, rows, covered=[])
    assert result["total_ink_pixels"] == 0
    assert result["fraction_outside"] == 0.0
    assert result["flagged"] is False


def test_background_is_inferred_per_page_not_assumed():
    """A darker paper tone (say, an aged or toned scan) is still the inferred
    background, and ink is judged relative to it -- not against a hard-coded
    light value that would misread the whole page as ink."""
    rows = [bytearray([80] * 20) for _ in range(20)]
    paint(rows, 5, 5, 5, 5, value=80 - MINIMUM_CONTRAST_BELOW_BACKGROUND - 5)
    result = residual_ink(20, 20, rows, covered=[])
    assert result["background_level"] == 80
    assert result["total_ink_pixels"] == 25
    assert result["flagged"] is True


def test_a_covered_bound_reaching_outside_the_page_is_clipped_not_refused():
    """A later stage's own accounting refuses geometry like this as a fatal
    imbalance; this independent check must not crash before that refusal is
    ever reached, so it clips rather than raising."""
    rows = canvas(20, 20)
    paint(rows, 15, 15, 5, 5)
    result = residual_ink(20, 20, rows, covered=[{"x": 15, "y": 15, "w": 50, "h": 50}])
    assert result["outside_ink_pixels"] == 0
    assert result["flagged"] is False


def test_a_negative_covered_origin_is_clipped_not_refused():
    rows = canvas(20, 20)
    paint(rows, 0, 0, 5, 5)
    result = residual_ink(20, 20, rows, covered=[{"x": -3, "y": -3, "w": 8, "h": 8}])
    assert result["outside_ink_pixels"] == 0
    assert result["flagged"] is False


def test_overlapping_and_clipped_bounds_cover_exactly_their_union():
    """The mask is filled by row slice rather than pixel by pixel, so the cost of
    this check is the covered AREA and not the sum of the declared rectangles.
    Overlap, partial overlap and clipping are where a row-slice fill could
    plausibly differ from a per-pixel one, so each is asserted against a
    hand-counted union rather than against the implementation."""
    rows = canvas(20, 20)
    ink = set()
    for x, y in ((1, 1), (7, 7), (12, 12), (19, 19), (0, 19), (19, 0)):
        paint(rows, x, y, 1, 1)
        ink.add((x, y))
    covered = [
        {"x": 0, "y": 0, "w": 10, "h": 10},  # top-left quarter
        {"x": 5, "y": 5, "w": 10, "h": 10},  # overlaps it, and reaches past it
        {"x": 18, "y": 18, "w": 50, "h": 50},  # clipped to the page's corner
        {"x": -5, "y": -5, "w": 7, "h": 7},  # clipped at the origin
        {"x": 0, "y": 0, "w": 0, "h": 0},  # empty: covers nothing
    ]
    union = set()
    for bounds in covered:
        for y in range(max(0, bounds["y"]), min(20, bounds["y"] + bounds["h"])):
            for x in range(max(0, bounds["x"]), min(20, bounds["x"] + bounds["w"])):
                union.add((x, y))
    result = residual_ink(20, 20, rows, covered=covered)
    assert result["total_ink_pixels"] == len(ink)
    assert result["outside_ink_pixels"] == len(ink - union)
    assert ink - union  # the case would prove nothing if everything were covered


def test_page_residual_ink_decodes_before_measuring():
    rows = canvas(10, 10)
    paint(rows, 0, 0, 6, 6)
    encoded = encode_grayscale_png(10, 10, rows)
    result = page_residual_ink(encoded, covered=[])
    assert result["outside_ink_pixels"] == 36
    assert result["flagged"] is True


def test_page_residual_ink_refuses_undecodable_bytes():
    with pytest.raises(ValueError):
        page_residual_ink(b"not a page", covered=[])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
