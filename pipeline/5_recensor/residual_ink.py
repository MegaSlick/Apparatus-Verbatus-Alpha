"""Residual-ink page coverage: the deterministic core's cheapest instrument.

ARCHITECTURE's candidate list, spec 09's own words: "coverage vs the proposal-set
seal **plus a residual-ink check whose input is the page image itself, never the
proposal set** -- a denominator derived only from proposals cannot see an act
nobody proposed (GOALS 1)." This is that check.

It reads the sealed page's own pixels and asks whether any of its ink sits
outside every region actually cut for it -- proposal or recovery, for every act
that touches that page -- entirely independent of what any stage claimed to
find. A successful recovery crop that reaches the missed ink clears the finding
on the very next Recensor pass, with no code here that requests one: there is
no act to request a recrop for when the ink belongs to nobody's proposal at
all, and this module never invents one (that would be exactly the "hold at the
final boundary" GOVERNANCE 3 forbids in a stage that establishes no text).

Window pass, 2026-08-05 (`/out/report.md`): the old pipeline's measured
justification for this whole stage was a page that claimed success while
producing nothing -- 218 of 29,950 pages (0.73%), the silent failure class
outnumbering the loud one 218 to 1. That old ladder confirmed a suspect page
by rereading it independently with witness models; this check asks a narrower,
purely geometric question a page that already has at least one proposed region
can answer without any new reading capability at all: does the ink on this page
fit inside what was cut for it? A page with *zero* proposed acts still has no
evidence path here -- nothing ever examined its pixels, so there is nothing
this module can read a region's absence against. That gap is named, not
papered over, in `pipeline/5_recensor/HANDOFF.md` and `/out/report.md`.
"""

from typing import Any

from common.imaging import Bounds, grayscale_rows

# PROPOSED, NOT YET MEASURED. There is no real corpus in this walking skeleton
# to calibrate against (`pipeline/1_exemplar/door.py`: "No real image has been
# touched"), so these three numbers are reasoned defaults, not alpha-tested
# ones -- the same epistemic status `config/recovery.toml`'s starting budget and
# `config/hard_failure.toml`'s proposed kind list already carry, and flagged the
# same way. They do not answer "is this page blank" (industry practice for that
# question sits at roughly 0.5%-5% dark-pixel ratio) or reuse the old pipeline's
# own unmeasured 0.005 density threshold, which its own review named as an
# unmeasured gate (G10) -- both answer a different question. These answer "does
# ink exist that no currently-cut region covers", and are chosen in the same
# order of magnitude a careful reader would expect routine scan noise (a fold
# shadow, a stray mark, a punch hole) to sit under. Tyrel raises or lowers them
# once alpha testing against a real corpus gives a real number.

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
    calibrates to its own scan conditions rather than assuming one tone for
    an entire corpus. Background occupies the large majority of any real or
    synthetic page, so the most frequent value is a robust estimate of it --
    this is an explicit assumption, not a proven bound: a page whose ink
    genuinely covers more than half its pixels (a dense full-bleed plate, not
    a parish register act) would confuse a mode into calling ink the
    background. True of every register page this project has described so
    far; worth revisiting if a real corpus ever contradicts it.
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

    Pure geometry over pixels the page itself carries -- no witness, no
    reading, no stage's claim about what it found. `covered` is every region
    actually cut for this page (proposal and recovery, from every act that
    touches it), in page-pixel coordinates, exactly as the Designator recorded
    them in `transform.bounds`. A bound that reaches outside the page is
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
