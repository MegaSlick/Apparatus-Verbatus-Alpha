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

A page with *zero* proposed acts has no finding on this late path: there are no
regions, so there is nothing to read a region's absence against. Its pixels are
no longer unexamined -- Unit 9 runs this same measure over every sealed page
before any proposal exists (`pipeline/1_ink_map/run.py`) -- but the early map
records evidence and holds nothing, and Unit 14 owns the hold outcome. What
remains of the gap is named, not papered over, in
`pipeline/5_recensor/HANDOFF.md`.
"""

from collections import Counter
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
#: That is a missed act reported as a clean page, which is GOALS 1's worst
#: failure ("a missed act is worse than a poorly read act") and Tyrel's own
#: 2026-08-11 ruling ("Missing text is the worst failure"). Set well above any
#: plausible non-text artifact — a heavy fold shadow or a punch hole runs to
#: hundreds of pixels, not thousands — and deliberately below one line of
#: 300-DPI text (roughly 8,000 ink pixels), because flagging costs a human
#: glance at a page and missing an act costs the act.
SUBSTANTIAL_INK_PIXELS = 2_000

# A bounded strip on every page edge.  This is an instrument boundary, not a
# claim that 64 pixels is a calibrated cross-page-act threshold: the existing
# ink thresholds above remain PROPOSED, NOT YET MEASURED.  It keeps the signal
# local to the page break while Unit 14 decides the explicit hold outcome.
EDGE_BAND_PIXELS = 64


def _ink_table(background: int) -> bytes:
    """A 256-entry translation table mapping every pixel value to 1 (ink) or 0.

    Precomputed once per page so the per-pixel ink test becomes a single
    C-level `bytes.translate` over each row rather than an interpreted
    comparison per pixel. The predicate is exactly the one the old loop
    applied: ink is a value at least `MINIMUM_CONTRAST_BELOW_BACKGROUND`
    levels below this page's own inferred background.
    """
    return bytes(
        1 if background - value >= MINIMUM_CONTRAST_BELOW_BACKGROUND else 0 for value in range(256)
    )


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
    # `Counter.update` over each row counts in C rather than one interpreted
    # loop iteration per pixel; on a 300-DPI page that is ~8.4 million
    # iterations saved per page, and this check runs on every page of every
    # run. `max(range(256), ...)` reproduces `list.index(max(...))`'s tie-break
    # exactly -- both scan upward from 0 and return the lowest value holding
    # the maximum count, which matters because a page with two equally common
    # tones must infer the same background as it always did.
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
        # Filled by row slice, not pixel by pixel. The number of regions on a
        # page is the number of acts touching it and each one's declared width
        # and height are its own, so a per-pixel fill makes this check cost the
        # SUM of the declared areas -- which a page of overlapping full-page
        # rectangles multiplies by the act count.
        span = b"\x01" * (x1 - x0)
        for y in range(y0, y1):
            covered_mask[y * width + x0 : y * width + x1] = span

    # Both counts per row in C rather than per pixel in interpreted Python.
    # `translate` maps each pixel to 1 (ink) or 0; `count` totals them. For the
    # outside count, every byte of both the ink row and the mask row is 0 or 1,
    # so `ink & ~covered` is a per-byte "ink here, covered nowhere" and
    # `bit_count` totals exactly those positions -- the same predicate the
    # per-pixel branch applied, without materialising an intermediate list.
    # Equivalence against the straightforward implementation is pinned by
    # `test_the_fast_counts_agree_with_a_straightforward_implementation`.
    ink_table = _ink_table(background)
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
    # Either gate flags on its own. The fraction gate catches a miss that is
    # large *relative to* what the page carries; the absolute gate catches one
    # that is large *full stop*, which on a dense page the fraction gate alone
    # would let through (see `SUBSTANTIAL_INK_PIXELS`). This can only ever add
    # a flag, never remove one — every page flagged before this second gate
    # existed is still flagged by the first.
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
    """`residual_ink`, decoding the sealed page bytes first."""
    width, height, rows = grayscale_rows(image_bytes)
    return residual_ink(width, height, rows, covered)


def page_edge_ink(image_bytes: bytes) -> dict[str, Any]:
    """Measure unclaimed ink in the bounded perimeter of one sealed page.

    The central rectangle is the only covered area, so this delegates the ink
    predicate and both existing flag gates to ``residual_ink`` rather than
    creating a second detector with slightly different arithmetic.  Its finding
    is evidence only: Unit 14 owns the hold outcome for a possible unproposed
    cross-page half act.
    """
    width, height, rows = grayscale_rows(image_bytes)
    band = min(EDGE_BAND_PIXELS, width // 2, height // 2)
    covered = (
        [] if band == 0 else [{"x": band, "y": band, "w": width - 2 * band, "h": height - 2 * band}]
    )
    finding = residual_ink(width, height, rows, covered)
    return {**finding, "edge_band_pixels": band, "named_finding": "unclaimed-edge-ink"}
