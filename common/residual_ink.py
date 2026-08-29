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
# claim that 64 pixels is a calibrated cross-page-act threshold: it only
# localizes the signal, and the ink thresholds above remain PROPOSED, NOT YET
# MEASURED until they are read against a real corpus.
EDGE_BAND_PIXELS = 64


def coverage_flag(total_ink_pixels: int, outside_ink_pixels: int) -> tuple[float, bool]:
    """The one outside-coverage gate, and the ratio it is read against.

    Either gate flags on its own. The fraction gate catches a miss that is
    large *relative to* what the page carries; the absolute gate catches one
    that is large *full stop*, which on a dense page the fraction gate alone
    would let through (see `SUBSTANTIAL_INK_PIXELS`). This can only ever add
    a flag, never remove one — every page flagged before this second gate
    existed is still flagged by the first.

    One function rather than a copy per measure, because the Armarium's export
    verifier recomputes this predicate over recorded counts on a clean machine
    (`pipeline/7_armarium/armarium_export.py`). A verifier applying its own
    fourth copy of the arithmetic would be checking a different instrument
    than the one that measured, which is not a check.
    """
    fraction_outside = (outside_ink_pixels / total_ink_pixels) if total_ink_pixels else 0.0
    flagged = outside_ink_pixels >= SUBSTANTIAL_INK_PIXELS or (
        outside_ink_pixels >= MINIMUM_INK_PIXELS
        and fraction_outside >= MINIMUM_FRACTION_OUTSIDE_COVERAGE
    )
    return fraction_outside, flagged


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
    # One spelling of the ink predicate, shared with `ink_runs`: a second copy
    # would let the retained runs and this count drift apart silently.
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

    fraction_outside, flagged = coverage_flag(total_ink, outside_ink)
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


def ink_runs(image_bytes: bytes) -> dict[str, Any]:
    """The ink map's reusable, lossless page-space evidence.

    Later consumers must count these retained runs rather than decode the page
    again under a potentially different pixel measurement.
    """
    width, height, rows = grayscale_rows(image_bytes)
    table = _ink_table(_background_level(rows))
    encoded: list[list[list[int]]] = []
    for row in rows:
        bits = row.translate(table)
        runs: list[list[int]] = []
        start = 0
        while start < width:
            start = bits.find(1, start)
            if start < 0:
                break
            end = bits.find(0, start)
            if end < 0:
                end = width
            runs.append([start, end - start])
            start = end
        encoded.append(runs)
    return {"schema": "ink-runs.v1", "width": width, "height": height, "rows": encoded}


def edge_ink_from_runs(evidence: dict[str, Any], covered: list[Bounds]) -> dict[str, Any]:
    """Re-measure the edge finding against later Designator cuts.

    The initial measure precedes all proposals, so it is a candidate finding.
    A later crop may release it only by applying the same edge band and flag
    gates to these retained runs; only the coverage mask may change.
    """
    if not isinstance(evidence, dict) or evidence.get("schema") != "ink-runs.v1":
        raise ValueError("ink-run evidence has the wrong schema")
    width, height, rows = evidence.get("width"), evidence.get("height"), evidence.get("rows")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
        or not isinstance(rows, list)
        or len(rows) != height
    ):
        raise ValueError("ink-run evidence has invalid dimensions")

    # The same band `page_edge_ink` records, including its smallest-legal-image
    # floor. Without `max(1, ...)` a one-pixel-wide or one-pixel-high page gets
    # band 0 here and band 1 there, so the Ink Map flags the page and the
    # Armarium re-measures it clean; `ink_map_page_rows` then refuses the whole
    # export over two detectors disagreeing rather than over the page.
    band = min(EDGE_BAND_PIXELS, max(1, width // 2), max(1, height // 2))
    total_ink = 0
    outside_ink = 0
    for y, row in enumerate(rows):
        if not isinstance(row, list):
            raise ValueError("ink-run evidence has a malformed row")
        runs: list[tuple[int, int]] = []
        previous_end = 0
        for run in row:
            if (
                not isinstance(run, list)
                or len(run) != 2
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in run)
            ):
                raise ValueError("ink-run evidence has a malformed run")
            start, length = run
            end = start + length
            if start < previous_end or length <= 0 or end > width:
                raise ValueError("ink-run evidence has unordered or out-of-bounds runs")
            runs.append((start, end))
            previous_end = end

        total_ink += sum(end - start for start, end in runs)
        if band == 0:
            continue
        edge_intervals = (
            [(0, width)] if y < band or y >= height - band else [(0, band), (width - band, width)]
        )
        covered_intervals = sorted(
            (
                max(0, min(bounds["x"], width)),
                max(0, min(bounds["x"] + bounds["w"], width)),
            )
            for bounds in covered
            if bounds["y"] <= y < bounds["y"] + bounds["h"]
        )
        merged_coverage: list[tuple[int, int]] = []
        for start, end in covered_intervals:
            if start >= end:
                continue
            if merged_coverage and start <= merged_coverage[-1][1]:
                merged_coverage[-1] = (merged_coverage[-1][0], max(end, merged_coverage[-1][1]))
            else:
                merged_coverage.append((start, end))
        for run_start, run_end in runs:
            for edge_start, edge_end in edge_intervals:
                start, end = max(run_start, edge_start), min(run_end, edge_end)
                if start >= end:
                    continue
                cursor = start
                for covered_start, covered_end in merged_coverage:
                    if covered_end <= cursor:
                        continue
                    if covered_start >= end:
                        break
                    outside_ink += max(0, min(covered_start, end) - cursor)
                    cursor = max(cursor, covered_end)
                    if cursor >= end:
                        break
                outside_ink += max(0, end - cursor)

    fraction_outside, flagged = coverage_flag(total_ink, outside_ink)
    return {
        "total_ink_pixels": total_ink,
        "outside_ink_pixels": outside_ink,
        "fraction_outside": fraction_outside,
        "flagged": flagged,
        "edge_band_pixels": band,
        "named_finding": "unclaimed-edge-ink",
    }


def page_edge_ink(image_bytes: bytes) -> dict[str, Any]:
    """Measure unclaimed ink in the bounded perimeter of one sealed page.

    The central rectangle is the only covered area, so this delegates the ink
    predicate and both existing flag gates to ``residual_ink`` rather than
    creating a second detector with slightly different arithmetic. Its finding
    is evidence only; this measurement does not assign the ink to an act.
    """
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
