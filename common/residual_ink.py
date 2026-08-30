"""Shared residual-ink measurement for the early Ink Map and late Recensor.

Ink is derived from the sealed page independently of stage claims. Coverage is
the Designator's declared ``transform.bounds`` rather than geometry re-derived
here; ``crop_png`` has already refused out-of-page crop bounds before they can
become honest coverage. Bounds are still clipped here so a later geometry
refusal is not pre-empted by this independent measurement.

The early Ink Map passes empty coverage to record the pre-proposal denominator;
the Recensor passes every proposal and recovery region for the page. This module
must not invent an act identity, request recovery, or hold a run. Unit 14 owns
the explicit hold for unproposed edge ink.
"""

from collections import Counter
from typing import Any

from common.imaging import Bounds, grayscale_rows

# PROPOSED, NOT YET MEASURED. There is no real corpus in this walking skeleton
# to calibrate against, so these three numbers are reasoned defaults, not
# alpha-tested ones. A blank-page density threshold cannot substitute for them:
# "is this page blank" and "is any ink outside coverage" are different questions.
# Change them only when real-corpus calibration supplies a measured value.

#: Below this many outside-coverage ink pixels, a residual mark is treated as
#: noise regardless of what fraction of the page's (possibly tiny) total ink
#: count it represents.
MINIMUM_INK_PIXELS = 24

#: A pixel this many levels darker than the page's own inferred background is
#: "ink". Relative to the page's own background, never an absolute level: two
#: real scans can have different paper tone and lighting, and an absolute
#: threshold would read one page's ink as background on another.
#:
#: Deliberately stricter than the Designator conservation denominator's
#: `SECONDARY_MARGIN` (2, in
#: `pipeline/2_designator/structure.py`): this check is an independent audit
#: that flags whole missed regions with confidence, while the Designator's
#: conservation errs sensitive and holds faint residuals itself. Keeping this
#: value at or above the Designator's margin is what guarantees the audit,
#: on any page that stage measured, never calls a pixel ink that the
#: Designator's accounting dismissed — the one direction of disagreement that
#: could lose ink silently. Pinned by
#: `common/test_designator_recensor_ink_calibration.py`; see the Designator's
#: `conservation.py` module docstring for the whole decision.
MINIMUM_CONTRAST_BELOW_BACKGROUND = 40

#: The fraction of a page's own ink pixels that must fall outside every region
#: currently cut for it before the page is flagged.
MINIMUM_FRACTION_OUTSIDE_COVERAGE = 0.02

#: Enough outside-coverage ink to flag a page on its own, whatever fraction of
#: that page's total ink it is. The fraction gate alone has a hole at the dense
#: end: a page carrying 500,000 ink pixels can leave 9,000 of them — several
#: words, plainly real text — outside every cut region and still sit under 2%.
#: That would report a missed act as a clean page, contrary to GOALS 1. The
#: proposed value is above plausible isolated scan artifacts and below the
#: estimated ink in one line of 300-DPI text.
SUBSTANTIAL_INK_PIXELS = 2_000

# A bounded strip on every page edge.  This is an instrument boundary, not a
# claim that 64 pixels is a calibrated cross-page-act threshold: the existing
# ink thresholds above remain PROPOSED, NOT YET MEASURED.  It keeps the signal
# local to the page break while Unit 14 decides the explicit hold outcome.
EDGE_BAND_PIXELS = 64


def _background_level(rows: list[bytearray]) -> int:
    """The page's own most common pixel value: the inferred paper tone.

    A histogram mode, not a mean or a fixed constant, so each page self-
    calibrates to its own scan conditions rather than assuming one tone for an
    entire corpus. That rests on an explicit assumption and not a proven bound:
    a page whose ink genuinely covers more than half its pixels -- a dense
    full-bleed plate, not a parish register act -- would confuse the mode into
    calling ink the background. True of every register page this project has
    described so far; worth revisiting if a real corpus contradicts it.
    """
    # Counter avoids an interpreted iteration per pixel. Iterating the key range
    # upward also fixes ties at the lowest modal value, which is part of the
    # deterministic background inference.
    histogram: Counter[int] = Counter()
    for row in rows:
        histogram.update(row)
    return max(range(256), key=lambda value: histogram.get(value, 0))


def residual_ink(
    width: int, height: int, rows: list[bytearray], covered: list[Bounds]
) -> dict[str, Any]:
    """How much of this page's own ink sits outside every region cut for it.

    `covered` is every region actually cut for this page (proposal and recovery,
    from every act that touches it), in page-pixel coordinates, exactly as the
    Designator recorded them in `transform.bounds`. A bound that reaches
    outside the page is
    clipped to it rather than refused: geometry a later stage will itself
    refuse as a fatal accounting imbalance should not also crash this
    independent check before that refusal is ever reached.
    """
    background = _background_level(rows)
    covered_mask = bytearray(width * height)
    for bounds in covered:
        x0 = max(0, min(bounds["x"], width))
        y0 = max(0, min(bounds["y"], height))
        x1 = max(x0, min(bounds["x"] + bounds["w"], width))
        y1 = max(y0, min(bounds["y"] + bounds["h"], height))
        # Slice assignment keeps overlapping full-page regions from multiplying
        # Python-level work by every pixel in every act's declared area.
        span = b"\x01" * (x1 - x0)
        for y in range(y0, y1):
            covered_mask[y * width + x0 : y * width + x1] = span

    # Values in both translated rows and mask rows are restricted to 0 or 1, so
    # integer `ink & ~covered` preserves the per-pixel predicate. Equivalence is pinned by
    # `test_the_fast_counts_agree_with_a_straightforward_implementation`.
    ink_table = bytes(
        1 if background - value >= MINIMUM_CONTRAST_BELOW_BACKGROUND else 0 for value in range(256)
    )
    total_ink = 0
    outside_ink = 0
    for y, row in enumerate(rows):
        row_offset = y * width
        ink_row = row.translate(ink_table)
        total_ink += ink_row.count(1)
        covered_row = covered_mask[row_offset : row_offset + width]
        outside_ink += (
            int.from_bytes(ink_row, "big") & ~int.from_bytes(covered_row, "big")
        ).bit_count()

    fraction_outside = (outside_ink / total_ink) if total_ink else 0.0
    # The relative gate catches sparse pages; the absolute gate prevents dense
    # pages from hiding substantial ink below the fraction threshold.
    flagged = outside_ink >= SUBSTANTIAL_INK_PIXELS or (
        outside_ink >= MINIMUM_INK_PIXELS and fraction_outside >= MINIMUM_FRACTION_OUTSIDE_COVERAGE
    )
    return {
        "background_level": background,
        "total_ink_pixels": total_ink,
        "outside_ink_pixels": outside_ink,
        "fraction_outside": fraction_outside,
        "flagged": flagged,
    }


def page_residual_ink(image_bytes: bytes, covered: list[Bounds]) -> dict[str, Any]:
    width, height, rows = grayscale_rows(image_bytes)
    return residual_ink(width, height, rows, covered)


def page_edge_ink(image_bytes: bytes) -> dict[str, Any]:
    """Measure the bounded perimeter as evidence; Unit 14 owns any resulting hold."""
    width, height, rows = grayscale_rows(image_bytes)
    # A one-pixel-wide or one-pixel-high image has no centre, but it still has
    # an edge. ``max(1, ...)`` keeps the recorded band honest for that smallest
    # legal image; the explicit centre check prevents a negative rectangle.
    band = min(EDGE_BAND_PIXELS, max(1, width // 2), max(1, height // 2))
    covered = (
        []
        if 2 * band >= width or 2 * band >= height
        else [{"x": band, "y": band, "w": width - 2 * band, "h": height - 2 * band}]
    )
    finding = residual_ink(width, height, rows, covered)
    return {**finding, "edge_band_pixels": band, "named_finding": "unclaimed-edge-ink"}
