"""Skew: the small angle that levels a page's lines of writing.

`detect_skew(image, dpi)` returns the detector answer shape: the value is the angle in
degrees by which the page is turned counterclockwise to level its lines (positive is
counterclockwise; 0 means no rotation). It works on a reduced working copy, writes no
file, and flags rather than guesses.

1. Only writing is measured. Inside the page's area the copy is split
   into ink and paper (Otsu 1979). Wide dark bands and large blobs are found by an
   opening with an element much longer than any word and thicker than any stroke;
   every component holding a survivor is removed whole (reconstruction, Vincent 1993).
   Ruled lines are removed as long straight runs and, when tilted past
   that test, by their shape: long, thin components.
2. Projection profiles: for each candidate angle the writing is sheared
   so that lines at that angle become level, and summed along rows; the score is the
   energy of the differences between row sums about a stroke's width apart, which peaks
   when lines and gaps line up with the rows. A coarse search over the range on the
   working copy is refined near the best angle on a sharper copy. (W. Postl, "Detection
   of linear oblique structures and skew scan in digitized documents", Proc.
   International Conference on Pattern Recognition, 1986; H. S. Baird, "The skew angle
   of printed documents", Proc. SPSE Symposium on Hybrid Imaging Systems, 1987.)
3. A second, independent estimate as a cross-check (after
   D. S. Le, G. R. Thoma and H. Wechsler, "Automated page orientation and skew angle
   detection for binary document images", Pattern Recognition, 1994): each line is
   smeared into one band by a horizontal closing, a least-squares line is fitted to each
   band's pixels, and the length-weighted median of the bands' angles is taken. The
   smeared-band fit itself is general knowledge.
4. Trust: the best score must stand clearly above the median score
   across the range; the top and bottom of the inked area are faded so their edges do
   not make every page, noise included, peak at 0 degrees. Too little writing, a page
   that looks like noise, a best angle at the edge of the widened range, estimates that
   disagree, or regions of the page that lean differently are each reported with their
   reason; all but leaning regions with a dominant angle give 0.
5. An angle below the snap setting is reported as 0, and the evidence says so.
"""

from __future__ import annotations

import math
from array import array
from typing import Any

from PIL import Image, ImageChops, ImageMath

from pagekit import _box_common as common
from pagekit.check import otsu_threshold

_READS = (
    "skew_working_dpi",
    "skew_range_deg",
    "skew_widen_range_deg",
    "skew_coarse_step_deg",
    "skew_refine_step_deg",
    "skew_refine_dpi",
    "skew_profile_lag_mm",
    "skew_score_margin",
    "skew_disagree_deg",
    "skew_disagree_share",
    "skew_region_disagree_deg",
    "skew_region_fit",
    "skew_region_grid",
    "skew_region_min_ink_mm2",
    "skew_dominant_share",
    "skew_snap_deg",
    "skew_min_ink_mm2",
    "skew_band_length_mm",
    "skew_band_thickness_mm",
    "skew_smear_mm",
    "skew_line_min_mm",
    "rule_min_length_mm",
    "rule_fatten_mm",
    "rule_max_thickness_mm",
    "blank_contrast",
)
TOO_LITTLE = "too little content"


def detect_skew(
    image: Image.Image,
    dpi: tuple[float, float],
    polygon: list[tuple[float, float]] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict:
    """The skew answer for one upright page: value, confidence, evidence, flags."""
    thresholds = common.load_thresholds(overrides)
    v = common.values(thresholds)
    note = common.unmeasured_note(thresholds, _READS)
    page = common.page_input(image, dpi, polygon, v)
    flags: list[str] = list(page.flags)
    work = common.working_copy(page, v["skew_working_dpi"])
    writing, removed = writing_map(work, v)
    if writing is None:
        if removed.startswith("The page looks like noise"):
            return common.answer(
                0.0, 0.0, removed + " No rotation applied." + note, flags + [removed]
            )
        return _too_little(
            "The page shows no ink: its dark and light levels are too close." + note, flags
        )
    ink_mm2 = common.count(writing) * work.mm**2
    if ink_mm2 < v["skew_min_ink_mm2"]:
        return _too_little(
            f"Only {ink_mm2:.0f} mm² of writing after removing {removed}; at least "
            f"{v['skew_min_ink_mm2']:g} mm² is needed for a profile." + note,
            flags,
        )

    ink = writing.point(lambda level: 1 if level else 0).convert("F")
    lag = work.px(v["skew_profile_lag_mm"])
    search = profile_search(ink, v, lag, fine=sharp_writing(page, writing, v))
    angle, ratio = search["angle"], search["ratio"]
    if search["at_edge"]:
        return common.answer(
            0.0,
            0.1,
            f"The best profile angle stayed at the edge of the widened search range "
            f"(±{v['skew_widen_range_deg']:g}°); no rotation applied." + note,
            flags
            + [
                f"Skew may exceed ±{v['skew_widen_range_deg']:g}°: the best "
                "angle lies at the edge of the widened search range."
            ],
        )
    if ratio < v["skew_score_margin"]:
        return common.answer(
            0.0,
            round(max(0.0, min(0.3, (ratio - 1.0) / 2)), 3),
            f"The best profile score is only {ratio:.2f} times the median across the range "
            f"(at least {v['skew_score_margin']:g} is needed); no rotation applied." + note,
            flags + ["No clear skew: the profile peak does not stand above the range."],
        )

    second = line_fit_estimate(writing, work, v)
    # The estimates agree when they have the same sign and differ by no more than the
    # larger of a fixed tolerance and a share of the angle: on running hands with
    # flourishes a line fit strays by a fraction of a degree, which must not leave a
    # visibly tilted page unlevelled (real-register follow-up, S1).
    spread = 0.0 if second is None else abs(second - angle)
    tolerance = v["skew_disagree_deg"]
    opposite = False
    if second is not None:
        tolerance = max(tolerance, v["skew_disagree_share"] * max(abs(angle), abs(second)))
        opposite = angle * second < 0 and min(abs(angle), abs(second)) >= v["skew_snap_deg"]
    disagree = second is not None and (opposite or spread > tolerance)
    if second is None:
        flags.append("The line-fit cross-check found no line long enough to measure.")
    elif disagree:
        why = "opposite signs" if opposite else f"more than {tolerance:.2f}° apart"
        flags.append(
            f"The two skew estimates disagree: profile {angle:+.2f}°, line fit "
            f"{second:+.2f}° ({why}); no rotation applied."
        )

    regions = region_estimates(ink, writing, work, v, search["reach"], angle)
    measured = sum(weight for _, weight, _ in regions)

    def leans(a: float, fit: float) -> bool:
        # A region leans differently only if its own angle is well apart from the
        # page's and its lines are clearly less level at the page's angle: with running
        # hands and flourishes a small region's peak wanders on a flat top (S2).
        return abs(a - angle) > v["skew_region_disagree_deg"] and fit < v["skew_region_fit"]

    agreeing = sum(w for a, w, fit in regions if not leans(a, fit))
    disagreeing = [a for a, _, fit in regions if leans(a, fit)]
    applied = 0.0 if disagree else angle
    region_text = ""
    if disagreeing:
        share = agreeing / measured if measured else 0.0
        listed = ", ".join(f"{a:+.2f}°" for a in sorted(set(round(a, 2) for a in disagreeing)))
        if share >= v["skew_dominant_share"]:
            flags.append(
                f"Regions of the page lean differently ({listed} against {angle:+.2f}°); "
                f"the page's angle covers {share:.0%} of the writing"
                f"{' but the estimates disagree' if disagree else ' and is applied'}."
            )
        else:
            applied = 0.0
            flags.append(
                f"Regions of the page lean differently ({listed} against {angle:+.2f}°); "
                f"no angle covers most of the writing ({share:.0%}), so no rotation applied."
            )
        region_text = f" {len(disagreeing)} of {len(regions)} regions disagree."
    elif regions:
        region_text = f" {len(regions)} regions agree."

    snapped = ""
    if applied != 0.0 and abs(applied) < v["skew_snap_deg"]:
        snapped = (
            f" The measured {applied:+.2f}° is below {v['skew_snap_deg']:g}° and "
            "is snapped to 0 to avoid a needless resample."
        )
        applied = 0.0
    applied = round(applied, 2) + 0.0  # no negative zero
    cross = "" if second is None else f", line fit {second:+.2f}°"
    if second is not None and not disagree and spread > v["skew_disagree_deg"]:
        cross += (
            f" (they differ by {spread:.2f}°, within {tolerance:.2f}° for an angle this size, "
            "so the profile angle is applied)"
        )
    evidence = (
        f"Projection profile on {ink_mm2:.0f} mm² of writing (after removing "
        f"{removed}) peaks at {angle:+.2f}°, {ratio:.2f} times the median score"
        f"{cross}.{region_text}{snapped}{note}"
    )
    confidence = min(1.0, 0.5 + 0.5 * (ratio - v["skew_score_margin"]) / v["skew_score_margin"])
    if second is not None and not disagree:
        confidence *= 1.0 - 0.5 * spread / tolerance  # a wider spread, less confidence
    if flags:
        confidence *= 0.5
    return common.answer(applied, max(0.0, confidence), evidence, flags)


def _too_little(evidence: str, flags: list[str]) -> dict:
    return common.answer(
        0.0,
        0.0,
        evidence,
        flags + [f"No rotation applied: {TOO_LITTLE} for a skew estimate."],
    )


def writing_map(work: common.Work, v: dict[str, Any]) -> tuple[Image.Image | None, str]:
    """The ink that is writing: bands, blobs and rules removed.

    Returns None when the page shows no ink at all or looks like noise (with the
    reason in place of the description), and otherwise a plain description of what
    was removed.
    """
    histogram = work.grey.histogram(work.mask)
    threshold = otsu_threshold(histogram)
    dark, light = common.class_means(histogram, threshold)
    if dark is None or light is None or light - dark < v["blank_contrast"]:
        return None, "nothing"
    ink = common.threshold_map(work.grey, threshold, work.mask)
    noise = common.busy(ink, work, v)
    if noise is not None:
        return None, noise

    # Wide bands and large blobs: an opening with a long, thick element in each direction
    # keeps only them; every ink component holding a survivor goes, whole.
    long_r = max(1, work.px(v["skew_band_length_mm"]) // 2)
    thick_r = max(1, work.px(v["skew_band_thickness_mm"]) // 2)
    survivors = ImageChops.lighter(
        common.opening(ink, long_r, thick_r), common.opening(ink, thick_r, long_r)
    )
    parts = common.components(ink)
    marks = survivors.tobytes()
    width = ink.width
    bands = [p for p in parts if p.touches(marks, width)]

    # Rules: long straight runs first, so writing touching a rule loses only the rule's
    # own pixels; then any long, thin, nearly straight component left (a tilted rule).
    band_ids = {id(p) for p in bands}
    writing = common.paint([p for p in parts if id(p) not in band_ids], ink.size)
    rule_length = work.px(v["rule_min_length_mm"])
    thickest = v["rule_max_thickness_mm"] / work.mm
    runs = common.long_lines(writing, rule_length, work.px(v["rule_fatten_mm"]), thickest)
    rule_pixels = common.count(runs)
    writing = ImageChops.subtract(writing, runs)
    # Strokes that crossed a rule are bridged again across the rule's own pixels, so
    # removing a level rule does not leave level slits in sloping writing.
    fatten = work.px(v["rule_fatten_mm"])
    writing = ImageChops.lighter(
        writing, ImageChops.multiply(runs, common.closing(writing, 0, fatten))
    )
    tilt = v["skew_widen_range_deg"]
    pieces = common.components(writing)
    shaped = [p for p in pieces if common.rule_shaped(p, rule_length, thickest, tilt)]
    if shaped:
        shaped_ids = {id(p) for p in shaped}
        writing = common.paint([p for p in pieces if id(p) not in shaped_ids], ink.size)

    removed = []
    if bands:
        removed.append(f"{len(bands)} dark band{'s' if len(bands) != 1 else ''} or blob")
    if shaped or rule_pixels:
        removed.append("ruled lines")
    return writing, " and ".join(removed) or "nothing"


def _score(ink: Image.Image, angle: float, lag: int) -> float:
    """Energy of the differences between row sums `lag` rows apart after shearing the
    writing so that lines at `angle` become level.

    The shear moves each column up or down by a whole number of rows (nearest
    neighbour), so no ink is blurred or lost and no angle is favoured by interpolation;
    a shear stands in for a rotation at these small angles. A lag of about a stroke's width,
    rather than one row, keeps a one-row slit, such as a removed rule leaves, from
    outscoring the gaps between lines.

    The caller tapers the top and bottom of the inked area (see `_tapered`), so the
    ends of the area add the same at every angle.
    """
    width, height = ink.size
    slope = math.tan(math.radians(angle))
    rise = math.ceil(width * abs(slope)) + 1
    offset = rise if slope > 0 else 0
    sheared = ink.transform(
        (width, height + rise),
        Image.AFFINE,
        (1, 0, 0, slope, 1, -offset),
        resample=Image.NEAREST,
    )
    rows = sheared.resize((1, sheared.height), Image.BOX)
    sums = [value * width for value in array("f", rows.tobytes())]
    return sum((b - a) ** 2 for a, b in zip(sums, sums[lag:], strict=False))


def _angles(low: float, high: float, step: float) -> list[float]:
    steps = round((high - low) / step)
    return [round(low + i * step, 4) for i in range(steps + 1)]


def _best(scores: list[tuple[float, float]]) -> tuple[float, float]:
    # Highest score; ties go to the smaller turn, then the lower angle, so runs repeat.
    return max(scores, key=lambda pair: (pair[1], -abs(pair[0]), -pair[0]))


def profile_search(
    ink: Image.Image,
    v: dict[str, Any],
    lag: int,
    widen: bool = True,
    fine: tuple[Image.Image, int] | None = None,
    reach: float | None = None,
) -> dict:
    """Coarse search over the range (widened once at the edge), then refinement near the
    best angle, on `fine` (a sharper copy of the same writing and its lag) when given.

    The trust ratio compares the best coarse score with the median coarse score."""
    step = v["skew_coarse_step_deg"]
    reach = v["skew_range_deg"] if reach is None else reach
    ink = _tapered(_inked(ink), reach, lag)
    scores = [(a, _score(ink, a, lag)) for a in _angles(-reach, reach, step)]
    best, top = _best(scores)
    at_edge = abs(best) >= reach - 1e-9
    if at_edge and widen:
        reach = v["skew_widen_range_deg"]
        scores = [(a, _score(ink, a, lag)) for a in _angles(-reach, reach, step)]
        best, top = _best(scores)
        at_edge = abs(best) >= reach - 1e-9
    ordered = sorted(score for _, score in scores)
    median = ordered[len(ordered) // 2]
    ratio = top / median if median > 0 else (math.inf if top > 0 else 1.0)
    if fine is not None:
        sharp, sharp_lag = _tapered(_inked(fine[0]), reach, fine[1]), fine[1]
    else:
        sharp, sharp_lag = ink, lag
    refined = [
        (a, _score(sharp, a, sharp_lag))
        for a in _angles(best - step, best + step, v["skew_refine_step_deg"])
        if abs(a) <= reach + 1e-9
    ]
    angle = _peak(refined)
    return {"angle": angle, "ratio": ratio, "at_edge": at_edge, "reach": reach}


def _inked(ink: Image.Image) -> Image.Image:
    """The ink map cropped to its inked area."""
    box = ink.convert("L").getbbox()  # ink is 1.0 on 0.0, so 1 on 0 once converted
    return ink if box is None else ink.crop(box)


def _peak(scores: list[tuple[float, float]]) -> float:
    """The angle of the refined scores' peak: the vertex of a least-squares parabola
    through them, which steadies a flat, jagged top; the best single angle when the
    parabola does not open downward or its vertex leaves the refined interval."""
    best, _ = _best(scores)
    if len(scores) < 5:
        return best
    xs = [a - best for a, _ in scores]
    top = max(score for _, score in scores) or 1.0
    ys = [score / top for _, score in scores]
    # Normal equations for y = c0 + c1 x + c2 x^2.
    sums = [sum(x**k for x in xs) for k in range(5)]
    rhs = [sum(y * x**k for x, y in zip(xs, ys, strict=True)) for k in range(3)]
    matrix = [[sums[i + j] for j in range(3)] for i in range(3)]
    try:
        c0, c1, c2 = _solve3(matrix, rhs)
    except ZeroDivisionError:
        return best
    if c2 >= 0:
        return best
    vertex = -c1 / (2 * c2)
    if not min(xs) <= vertex <= max(xs):
        return best
    return round(best + vertex, 2)


def _solve3(m: list[list[float]], r: list[float]) -> tuple[float, float, float]:
    """Solve a 3 by 3 linear system by Cramer's rule."""

    def det(a: list[list[float]]) -> float:
        return (
            a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
        )

    d = det(m)
    if abs(d) < 1e-15:
        raise ZeroDivisionError
    result = []
    for column in range(3):
        swapped = [row[:column] + [r[i]] + row[column + 1 :] for i, row in enumerate(m)]
        result.append(det(swapped) / d)
    return result[0], result[1], result[2]


def _tapered(ink: Image.Image, reach: float, lag: int) -> Image.Image:
    """The ink with its top and bottom rows faded in and out (a raised cosine).

    Where the inked area starts and ends, a profile steps from empty to full. Unfaded,
    that step is sharpest when the shear is 0 and makes any page, noise included, peak
    at 0 degrees. The fade is as tall as the most a row can move across the area at the
    widest angle searched, so the ends add the same at every angle."""
    width, height = ink.size
    fade = max(lag, math.ceil((width - 1) * math.tan(math.radians(reach))))
    if 2 * fade >= height:
        return ink
    weights = array("f", [1.0] * height)
    for i in range(fade):
        w = 0.5 - 0.5 * math.cos(math.pi * (i + 0.5) / fade)
        weights[i] = weights[height - 1 - i] = w
    column = Image.frombytes("F", (1, height), weights.tobytes())
    return ImageMath.lambda_eval(
        lambda args: args["a"] * args["b"], a=ink, b=column.resize(ink.size, Image.NEAREST)
    )


def sharp_writing(
    page: common.Page, writing: Image.Image, v: dict[str, Any]
) -> tuple[Image.Image, int]:
    """The writing at the refinement resolution: the sharper copy's ink, kept only where
    the working copy found writing (so bands and rules stay removed), and its lag."""
    fine = common.working_copy(page, v["skew_refine_dpi"])
    histogram = fine.grey.histogram(fine.mask)
    ink = common.threshold_map(fine.grey, otsu_threshold(histogram), fine.mask)
    keep = writing.resize(fine.grey.size, Image.BILINEAR).point(
        lambda level: 255 if level > 64 else 0
    )
    sharp = ImageChops.multiply(ink, keep)
    return sharp.point(lambda level: 1 if level else 0).convert("F"), fine.px(
        v["skew_profile_lag_mm"]
    )


def line_fit_estimate(writing: Image.Image, work: common.Work, v: dict[str, Any]) -> float | None:
    """Weighted median angle of least-squares lines fitted to smeared lines of writing."""
    smear = common.closing(writing, max(1, work.px(v["skew_smear_mm"]) // 2), 0)
    shortest = work.px(v["skew_line_min_mm"])
    limit = math.tan(math.radians(v["skew_widen_range_deg"]))
    fits = []
    for part in common.components(smear):
        if part.width < shortest:
            continue
        slope, _ = common.fit_line(part)
        if abs(slope) > limit:
            continue
        fits.append((math.degrees(math.atan(slope)), part.width))
    if not fits:
        return None
    fits.sort()
    half = sum(weight for _, weight in fits) / 2
    seen = 0.0
    for angle, weight in fits:
        seen += weight
        if seen >= half:
            return round(angle, 2)
    return round(fits[-1][0], 2)


def region_estimates(
    ink: Image.Image,
    writing: Image.Image,
    work: common.Work,
    v: dict[str, Any],
    reach: float,
    page_angle: float = 0.0,
) -> list[tuple[float, float, float]]:
    """(angle, ink in mm², fit) for each region of the writing with enough ink to
    measure. The fit is the region's own profile score at the page's angle as a share of
    its score at its best angle: near 1, the region's lines are as level at the page's
    angle as at its own, and its own peak is only noise on a flat top."""
    box = writing.getbbox()
    if box is None:
        return []
    cells = max(1, int(v["skew_region_grid"]))
    x0, y0, x1, y1 = box
    estimates = []
    for row in range(cells):
        for column in range(cells):
            cell = (
                x0 + (x1 - x0) * column // cells,
                y0 + (y1 - y0) * row // cells,
                x0 + (x1 - x0) * (column + 1) // cells,
                y0 + (y1 - y0) * (row + 1) // cells,
            )
            amount = common.count(writing.crop(cell)) * work.mm**2
            if amount < v["skew_region_min_ink_mm2"]:
                continue
            lag = work.px(v["skew_profile_lag_mm"])
            found = profile_search(ink.crop(cell), v, lag, widen=False, reach=reach)
            if found["ratio"] < v["skew_score_margin"]:
                continue
            tapered = _tapered(_inked(ink.crop(cell)), reach, lag)
            own = _score(tapered, found["angle"], lag)
            fit = _score(tapered, page_angle, lag) / own if own > 0 else 1.0
            estimates.append((found["angle"], amount, min(1.0, fit)))
    return estimates
