"""Residual-ink page coverage: the deterministic core's cheapest instrument.

ARCHITECTURE's candidate list, spec 09's own words: "coverage vs the proposal-set
seal **plus a residual-ink check whose input is the page image itself, never the
proposal set** -- a denominator derived only from proposals cannot see an act
nobody proposed (GOALS 1)." This is that check.

It reads the sealed page's own pixels and asks whether any of the ink found in
them sits outside every region actually cut for it -- proposal or recovery, for
every act that touches that page. The ink itself is measured independently of
what any stage claimed to find; the coverage mask it is measured against is the
Designator's own declared `transform.bounds`, a stage claim this module trusts
rather than re-derives. That trust rests on `crop_png` refusing a rectangle that
reaches outside the page before a region is ever cut (`common/imaging.py`,
pinned against this module's own bound by
`test_this_modules_pixel_bound_matches_the_door_that_admits_the_pages`), so an
honest run cannot hand this check an over-declared rectangle that masks real
ink. A successful recovery crop that reaches the missed ink clears the finding
on the very next Recensor pass, with no code here that requests one: there is
no act to request a recrop for when the ink belongs to nobody's proposal at
all, and this module never invents one (that would be exactly the "hold at the
final boundary" GOVERNANCE 3 forbids in a stage that establishes no text).

The measured justification (window pass, 2026-08-05): in the old pipeline, 218
of 29,950 pages (0.73%) claimed success while producing nothing -- the silent
failure class outnumbering the loud one 218 to 1. That old ladder confirmed a
suspect page by rereading it independently with witness models; this check asks
a narrower, purely geometric question a page with at least one proposed region
can answer with no reading capability at all: does the ink on this page fit
inside what was cut for it?

A page with *zero* proposed acts has no evidence path here -- nothing ever
examined its pixels, so there is nothing to read a region's absence against.
That gap is named, not papered over, in `pipeline/5_recensor/HANDOFF.md`.
"""

from typing import Any

from common.imaging import Bounds, grayscale_rows

# PROPOSED, NOT YET MEASURED. There is no real corpus in this walking skeleton
# to calibrate against, so these three numbers are reasoned defaults, not
# alpha-tested ones -- the same epistemic status `config/recovery.toml`'s
# starting budget carries, and flagged the same way. They deliberately do not
# reuse a blank-page threshold (industry practice sits at roughly 0.5%-5%
# dark-pixel ratio) or the old pipeline's own unmeasured 0.005 density gate:
# both answer "is this page blank", which is a different question from "does ink
# exist that no currently-cut region covers". They are the order of magnitude a
# careful reader would expect routine scan noise (a fold shadow, a stray mark, a
# punch hole) to sit under. Tyrel raises or lowers them once alpha testing
# against a real corpus gives a real number.

#: Below this many outside-coverage ink pixels, a residual mark is treated as
#: noise regardless of what fraction of the page's (possibly tiny) total ink
#: count it represents.
MINIMUM_INK_PIXELS = 24

#: A pixel this many levels darker than the page's own inferred background is
#: "ink". Relative to the page's own background, never an absolute level: two
#: real scans can have different paper tone and lighting, and an absolute
#: threshold would read one page's ink as background on another.
MINIMUM_CONTRAST_BELOW_BACKGROUND = 40

#: The fraction of a page's own ink pixels that must fall outside every region
#: currently cut for it before the page is flagged.
MINIMUM_FRACTION_OUTSIDE_COVERAGE = 0.02


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
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    return histogram.index(max(histogram))


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
        # Filled by row slice, not pixel by pixel. The number of regions on a
        # page is the number of acts touching it and each one's declared width
        # and height are its own, so a per-pixel fill makes this check cost the
        # SUM of the declared areas -- which a page of overlapping full-page
        # rectangles multiplies by the act count.
        span = b"\x01" * (x1 - x0)
        for y in range(y0, y1):
            covered_mask[y * width + x0 : y * width + x1] = span

    total_ink = 0
    outside_ink = 0
    for y, row in enumerate(rows):
        row_offset = y * width
        for x, value in enumerate(row):
            if background - value < MINIMUM_CONTRAST_BELOW_BACKGROUND:
                continue
            total_ink += 1
            if not covered_mask[row_offset + x]:
                outside_ink += 1

    fraction_outside = (outside_ink / total_ink) if total_ink else 0.0
    flagged = (
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
    """`residual_ink`, decoding the sealed page bytes first."""
    width, height, rows = grayscale_rows(image_bytes)
    return residual_ink(width, height, rows, covered)
