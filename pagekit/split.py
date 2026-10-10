"""Page count and split: one page or two in the upright frame, and where to cut.

The detector works on a reduced working copy of the source image, turned upright by the
orientation answer, and writes no file. It returns the detector answer shape:
exactly ``value``, ``confidence``, ``evidence`` and ``flags``. The value is a dict:

- ``pages``: 1 or 2;
- ``cut``: for 2 pages, the cut as two points ``[[x, y], [x, y]]`` (top and bottom row)
  in the upright frame's full-resolution pixel grid, so it may lean; otherwise None;
- ``method``: what decided: ``"fold"``, ``"gap"``, ``"neighbour edge"`` or ``"none"``;
- ``part``: for a fold, which part found it, ``"line"`` or ``"valley"``; otherwise None;
- ``neighbour``: for one page with a strip of the neighbouring page, ``{"side": ...,
  "line": [[x, y], [x, y]] or None}``; otherwise None. The strip is reported, not cut.

The page count is decided from evidence; the frame's proportions are only a prior that
lowers the confidence when it disagrees, and that sends a gap-only answer to review.
When cues disagree, or a cue is present but weak, the answer is one
page with a flag.

Methods:

- Fold, thin-line part: a black top-hat (grey closing minus the image) with a square
  element wider than the line enhances thin dark structures (J. Serra, Image Analysis
  and Mathematical Morphology, Academic Press, 1982); a Hough-style search over lines
  restricted to a small lean from vertical (R. O. Duda and P. E. Hart, "Use of the
  Hough transformation to detect lines and curves in pictures", Communications of the
  ACM 15(1):11-15, 1972) scores each line by the share of the page height it covers.
  The search shears the image once per lean so every candidate line becomes a column.
- Fold, valley part: pen strokes are removed with a brightening filter, the column
  brightness profile is smoothed, and the deepest broad minimum between two brighter
  shoulders is the shadow.
- Gap: the writing is reduced to the boxes of its connected components and projected
  onto the horizontal axis; the gaps between runs of content are candidate cuts,
  preferring a gap that balances the ink on its two sides and, among those, the widest
  (after T. M. Breuel, "Two geometric algorithms for layout analysis", Document
  Analysis Systems, 2002). The cut is fitted through the gap's middle measured in
  horizontal bands over the content height, so it may lean.

Limits: a long vertical rule near the middle of a single page looks like a fold; a
single page laid out in two balanced columns looks like a spread when its proportions
say spread; the valley part sees only a shadow darker than its shoulders; a neighbour
strip is found only from a line near the side or from writing cut off by the frame.
All settings are unmeasured guesses (``thresholds_split.toml``).
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from pagekit._orient_ink import (
    INK,
    TOO_LITTLE_INK,
    DetectorError,
    Mark,
    answer,
    grey_of,
    ink_map,
    is_long_thin,
    levels_of,
    marks_of,
    mask_along_line,
    mask_straight_marks,
    profile,
    runs_of,
    setting_values,
    strength,
    working_copy,
)

_TURNS = {
    0: None,
    1: Image.Transpose.ROTATE_270,
    2: Image.Transpose.ROTATE_180,
    3: Image.Transpose.ROTATE_90,
}


@dataclass(frozen=True)
class _Line:
    """A near-vertical line x = x_mid + slope * (y - y_mid), in working px."""

    x_mid: float
    y_mid: float
    slope: float
    strength: float

    def x_at(self, y: float) -> float:
        return self.x_mid + self.slope * (y - self.y_mid)


@dataclass(frozen=True)
class _Gap:
    start: int
    end: int
    balance: float

    @property
    def middle(self) -> float:
        return (self.start + self.end) / 2

    @property
    def width(self) -> int:
        return self.end - self.start


def _core_overlap_mm() -> float:
    """The preparation core's overlap past a cut, from its settings file."""
    with (Path(__file__).with_name("thresholds_prepare.toml")).open("rb") as handle:
        return float(tomllib.load(handle)["overlap_mm"]["value"])


def _count(marks: int) -> str:
    return "1 ink mark crosses" if marks == 1 else f"{marks} ink marks cross"


def _resolution(image: Image.Image, dpi: tuple[float, float] | None, turns: int):
    raw = dpi if dpi is not None else image.info.get("dpi")
    try:
        x, y = (float(raw[0]), float(raw[1])) if raw is not None else (0.0, 0.0)
    except (TypeError, ValueError, IndexError):
        x, y = 0.0, 0.0
    if not (x > 0 and y > 0 and math.isfinite(x) and math.isfinite(y)):
        return None
    return (y, x) if turns % 2 else (x, y)


def _dark_bands(flags: list[bool], narrowest: int) -> list[tuple[int, int]]:
    """Runs of dark columns (or rows) at least `narrowest` wide; narrower ones are lines,
    which the line search handles, and writing that crosses them is kept."""
    return [(a, b) for a, b in runs_of(flags) if b - a >= narrowest]


def _shear_coverage(binary: Image.Image, slope: float, y_mid: float) -> bytes:
    """Coverage (0 to 255) of each column after shearing the lines of lean `slope` to
    vertical."""
    width, height = binary.size
    sheared = binary.transform(
        (width, height),
        Image.Transform.AFFINE,
        (1, slope, -slope * y_mid, 0, 1, 0),
        resample=Image.Resampling.NEAREST,
    )
    return sheared.resize((width, 1), Image.Resampling.BOX).tobytes()


def _line_candidates(work: Image.Image, rows: tuple[int, int], value: dict[str, Any]):
    """Best covering near-vertical thin line through each column, and its coverage.

    The search is coarse to fine: every column is scored at leans lean_coarse_step_deg
    apart, and the columns that stand out are scored again, in a window around them,
    at lean_step_deg apart within one coarse step of their best lean."""
    width, _ = work.size
    r0, r1 = rows
    height = r1 - r0
    size = value["fold_tophat_px"] | 1
    background = work.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
    tophat = ImageChops.subtract(background, work)
    contrast = value["fold_line_contrast"]
    binary = tophat.point(lambda level: INK if level >= contrast else 0).crop((0, r0, width, r1))
    y_mid = height / 2
    coarse = value["lean_coarse_step_deg"]
    steps = int(value["lean_max_deg"] / coarse + 1e-9)
    best = [0] * width
    best_angle = [0.0] * width
    for i in sorted(range(-steps, steps + 1), key=lambda i: (abs(i), i)):
        angle = i * coarse
        coverage = _shear_coverage(binary, math.tan(math.radians(angle)), y_mid)
        for x, level in enumerate(coverage):
            if level > best[x]:
                best[x], best_angle[x] = level, angle
    inner = size // 2 + 1
    excess = _excess([b / 255 for b in best], inner, max(inner + 2, value["fold_background_px"]))
    pad = math.ceil(y_mid * math.tan(math.radians(coarse))) + 2
    fine = value["lean_step_deg"]
    for start, end in runs_of([e >= 0.5 * value["fold_min_excess"] for e in excess]):
        centre = max(range(start, end), key=lambda x: best[x])
        a, b = max(0, start - pad), min(width, end + pad)
        window = binary.crop((a, 0, b, height))
        n = int(coarse / fine + 1e-9)
        for k in range(-n, n + 1):
            angle = best_angle[centre] + k * fine
            if abs(angle) > value["lean_max_deg"] + 1e-9:
                continue
            coverage = _shear_coverage(window, math.tan(math.radians(angle)), y_mid)
            for offset, level in enumerate(coverage):
                x = a + offset
                if level > best[x]:
                    best[x], best_angle[x] = level, angle
    slopes = [math.tan(math.radians(angle)) for angle in best_angle]
    return [b / 255 for b in best], slopes, r0 + y_mid


def _excess(coverage: list[float], inner: int, outer: int) -> list[float]:
    """Each column's coverage less the median coverage of its neighbours (from `inner`
    to `outer` px away on both sides): only a line that stands out from its
    surroundings scores, not writing or texture that covers every column alike."""
    width = len(coverage)
    out = []
    for x in range(width):
        around = coverage[max(0, x - outer) : max(0, x - inner + 1)]
        around += coverage[min(width, x + inner) : min(width, x + outer + 1)]
        out.append(coverage[x] - median(around) if around else 0.0)
    return out


def _lines(work: Image.Image, rows: tuple[int, int], value: dict[str, Any]):
    """Lines standing out from their neighbours by at least the fold threshold, and the
    best excess of any column outside the edge bands (for weak cues)."""
    coverage, slopes, y_mid = _line_candidates(work, rows, value)
    width = len(coverage)
    inner = (value["fold_tophat_px"] | 1) // 2 + 1
    excess = _excess(coverage, inner, max(inner + 2, value["fold_background_px"]))
    found: list[_Line] = []
    for start, end in runs_of([e >= value["fold_min_excess"] for e in excess]):
        x = max(range(start, end), key=lambda i: (excess[i], -abs(i - width / 2)))
        found.append(_Line(float(x), y_mid, slopes[x], excess[x]))
    band = value["edge_band_share"] * width
    middle = [e for x, e in enumerate(excess) if band <= x <= width - band]
    return found, max(middle, default=0.0)


def _smooth(values: list[float], window: int) -> list[float]:
    half = max(0, window // 2)
    sums = [0.0]
    for v in values:
        sums.append(sums[-1] + v)
    out = []
    for x in range(len(values)):
        a, b = max(0, x - half), min(len(values), x + half + 1)
        out.append((sums[b] - sums[a]) / (b - a))
    return out


def _valley_in(levels: list[float], lo: int, hi: int, reach: int):
    """Deepest minimum in [lo, hi) between brighter shoulders: (x, depth)."""
    best_x, best_depth = None, 0.0
    middle = len(levels) / 2
    for x in range(lo, hi):
        left = max(levels[max(0, x - reach) : x + 1])
        right = max(levels[x : x + reach + 1])
        depth = min(left, right) - levels[x]
        if best_x is None or (depth, -abs(x - middle)) > (best_depth, -abs(best_x - middle)):
            best_x, best_depth = x, depth
    return best_x, best_depth


def _bright(work: Image.Image, value: dict[str, Any]) -> Image.Image:
    """The working copy with pen strokes removed by a brightening filter."""
    return work.filter(ImageFilter.MaxFilter(value["valley_ink_filter_px"] | 1))


def _valley(
    work: Image.Image,
    rows: tuple[int, int],
    value: dict[str, Any],
    bright_full: Image.Image | None = None,
):
    """The fold shadow as a line, its depth in grey levels (line None if too weak), and
    the line at the shadow's steep edge when the shadow falls on one side only (else
    None)."""
    width, _ = work.size
    r0, r1 = rows
    bright = (bright_full if bright_full is not None else _bright(work, value)).crop(
        (0, r0, width, r1)
    )
    window = max(1, round(value["valley_smooth_share"] * width))
    reach = max(1, round(value["valley_reach_share"] * width))
    band = math.ceil(value["edge_band_share"] * width)
    lo, hi = band, max(band + 1, width - band)
    levels = _smooth([float(v) for v in profile(bright, along_x=True)], window)
    x, depth = _valley_in(levels, lo, hi, reach)
    if x is None or depth < value["valley_min_depth"]:
        return None, max(0.0, depth), None
    floor = levels[x] + depth / 2
    left = x
    while left > 0 and levels[left - 1] < floor:
        left -= 1
    right = x
    while right < width - 1 and levels[right + 1] < floor:
        right += 1
    if right - left + 1 < value["valley_min_width_share"] * width:
        return None, depth, None
    # A shadow on one side of the fold only ends at the fold with a steep edge: when
    # one flank of the valley is much steeper than the other, the fold is that edge,
    # not the darkest column.
    fine = _smooth([float(v) for v in profile(bright, along_x=True)], 3)
    span = right - left + 1
    falls = [(fine[i - 1] - fine[i], i) for i in range(max(1, left - span), min(width, x + 1))]
    rises = [(fine[i] - fine[i - 1], i) for i in range(max(1, x + 1), min(width, right + span + 1))]
    fall, fall_x = max(falls, default=(0.0, x))
    rise, rise_x = max(rises, default=(0.0, x))
    ratio = value["valley_edge_ratio"]
    edge = None
    if rise > 0 and rise >= ratio * max(fall, 1e-9):
        edge = rise_x
    elif fall > 0 and fall >= ratio * max(rise, 1e-9):
        edge = fall_x
    height = r1 - r0
    slope = 0.0
    halves = []
    for a, b in ((0, height // 2), (height // 2, height)):
        part = _smooth(
            [float(v) for v in profile(bright.crop((0, a, width, b)), along_x=True)], window
        )
        span = max(1, reach // 2)
        hx, _ = _valley_in(part, max(lo, x - span), min(hi, x + span + 1), reach)
        halves.append(hx)
    if None not in halves and height >= 4:
        slope = (halves[1] - halves[0]) / (height / 2)
        if abs(slope) > math.tan(math.radians(value["valley_lean_max_deg"])):
            slope = 0.0
    line = _Line(float(x), r0 + height / 2, slope, depth)
    edged = None if edge is None else _Line(float(edge), r0 + height / 2, slope, depth)
    return line, depth, edged


def _tone_step(
    work: Image.Image,
    rows: tuple[int, int],
    value: dict[str, Any],
    bright_full: Image.Image | None = None,
) -> float | None:
    """Where the paper's brightness steps from one level to another between the edge
    bands (two pages of different tone meeting), at the steepest column; None when no
    step reaches tone_step_min grey levels."""
    width, _ = work.size
    r0, r1 = rows
    bright = (bright_full if bright_full is not None else _bright(work, value)).crop(
        (0, r0, width, r1)
    )
    levels = _smooth([float(v) for v in profile(bright, along_x=True)], 3)
    reach = max(2, round(value["valley_reach_share"] * width / 2))
    band = math.ceil(value["edge_band_share"] * width)
    sums = [0.0]
    for v in levels:
        sums.append(sums[-1] + v)
    best = None
    for x in range(max(band, reach), min(width - band, width - reach)):
        step = (sums[x + reach] - sums[x]) / reach - (sums[x] - sums[x - reach]) / reach
        if best is None or abs(step) > abs(best[0]):
            best = (step, x)
    if best is None or abs(best[0]) < value["tone_step_min"]:
        return None
    _, x = best
    span = max(1, reach // 4)
    candidates = range(max(1, x - span), min(width, x + span + 1))
    return float(max(candidates, key=lambda i: abs(levels[i] - levels[i - 1])))


def _gaps(marks: list[Mark], width: int, value: dict[str, Any], bridging: int = 0):
    """Candidate gaps between runs of content (each with its balance). A column counts
    as content when more than `bridging` marks cover it, so a gap crossed by a few
    marks (a flourish whose box spans the gutter) can still be found."""
    depth = [0] * width
    for mark in marks:
        for x in range(mark.x0, mark.x1):
            depth[x] += 1
    covered = [d > bridging for d in depth]
    runs = runs_of(covered)
    debris = value["edge_debris_share"] * width
    while runs and runs[0][1] - runs[0][0] < debris:
        runs.pop(0)
    while runs and runs[-1][1] - runs[-1][0] < debris:
        runs.pop()
    band = value["edge_band_share"] * width
    gaps = []
    for (_, start), (end, _) in zip(runs, runs[1:], strict=False):
        middle = (start + end) / 2
        if end - start < value["min_gap_share"] * width or not band <= middle <= width - band:
            continue
        left = sum(m.count for m in marks if m.centre_x < middle)
        right = sum(m.count for m in marks if m.centre_x >= middle)
        balance = min(left, right) / max(left, right) if max(left, right) else 0.0
        gaps.append(_Gap(start, end, balance))
    return gaps, runs


def _fit_gap(
    gap: _Gap, marks: list[Mark], value: dict[str, Any], limit: list | None = None
) -> _Line:
    """The cut through the gap's middle over the content height. In each horizontal band
    the free interval between the two sides' writing is measured; among leans in the
    allowed range, the line that keeps the widest clearance in every band is chosen
    (the smallest lean when several do equally well). When the best lean is the largest
    allowed, the gap may lean further; its slope is then added to `limit`."""
    middle = gap.middle
    if not marks:
        return _Line(middle, 0.0, 0.0, gap.width)
    top = min(m.y0 for m in marks)
    bottom = max(m.y1 for m in marks)
    y_mid = (top + bottom) / 2
    bands = max(1, value["gap_fit_bands"])
    step = (bottom - top) / bands
    free = []
    for i in range(bands):
        a, b = top + i * step, top + (i + 1) * step
        inside = [m for m in marks if m.y1 > a and m.y0 < b]
        left = [m.x1 for m in inside if m.centre_x < middle]
        right = [m.x0 for m in inside if m.centre_x >= middle]
        if left and right:
            free.append(((a + b) / 2 - y_mid, max(left), min(right)))
    if len(free) < 2:
        return _Line(middle, y_mid, 0.0, gap.width)
    steps = int(value["lean_max_deg"] / value["lean_step_deg"] + 1e-9)
    best = None
    for i in sorted(range(-steps, steps + 1), key=lambda i: (abs(i), i)):
        slope = math.tan(math.radians(i * value["lean_step_deg"]))
        low = max(left - slope * y for y, left, _ in free)
        high = min(right - slope * y for y, _, right in free)
        clearance = (high - low) / 2
        if best is None or clearance > best[0] + 0.5:
            best = (clearance, slope, (low + high) / 2)
    clearance, slope, x_mid = best
    if clearance <= 0:
        return _Line(middle, y_mid, 0.0, gap.width)
    if limit is not None and abs(slope) >= math.tan(math.radians(value["lean_max_deg"])) - 1e-9:
        limit.append(slope)
    return _Line(x_mid, y_mid, slope, gap.width)


def _thin_parts(mark: Mark, size: int) -> list[Mark] | None:
    """The mark without its thick part. The thick part is what an opening by a `size`
    px square keeps (backdrop, shadow, a blot), grown by one px so its fringe goes
    with it; the rest, such as a pen stroke touching the backdrop or ending in a blot,
    stays writing, each connected piece as its own mark. Returns None when nothing is
    thick (the mark stands as it is)."""
    side = size | 1
    width, height = mark.width + 2 * side, mark.height + 2 * side
    canvas = bytearray(width * height)
    for y, start, end in mark.runs:
        row = (y - mark.y0 + side) * width
        canvas[row + start - mark.x0 + side : row + end - mark.x0 + side] = b"\xff" * (end - start)
    image = Image.frombytes("L", (width, height), bytes(canvas))
    opened = image.filter(ImageFilter.MinFilter(side)).filter(ImageFilter.MaxFilter(side))
    if opened.getbbox() is None:
        return None
    thin = ImageChops.subtract(image, opened.filter(ImageFilter.MaxFilter(3)))
    dx, dy = mark.x0 - side, mark.y0 - side
    return [
        Mark(
            m.x0 + dx,
            m.y0 + dy,
            m.x1 + dx,
            m.y1 + dy,
            m.count,
            [(y + dy, start + dx, end + dx) for y, start, end in m.runs],
        )
        for m in marks_of(thin)
    ]


def _band_fringe(
    mark: Mark,
    band_rows: list[tuple[int, int]],
    band_columns: list[tuple[int, int]],
    reach: int,
    touch: int,
    share: float,
) -> bool:
    """Whether the mark is a fringe of a dark band's own ragged edge: it lies wholly
    within `reach` px of the band's edge and touches that edge (within `touch` px) along
    at least `share` of its length. A pen stroke running just under the band, even a
    few px away and joined to the band at one point, does not touch it along its
    length."""
    columns: dict[int, int] = {}
    rows: dict[int, int] = {}
    for y, start, end in mark.runs:
        for x in range(start, end):
            columns[x] = min(columns.get(x, y), y)
        rows[y] = min(rows.get(y, start), start)
    bottoms: dict[int, int] = {}
    rights: dict[int, int] = {}
    for y, start, end in mark.runs:
        for x in range(start, end):
            bottoms[x] = max(bottoms.get(x, y), y)
        rights[y] = max(rights.get(y, end - 1), end - 1)
    for a, b in band_rows:
        if mark.y0 >= b and mark.y1 <= b + reach:
            near = sum(1 for top in columns.values() if top - b <= touch)
            if near >= share * len(columns):
                return True
        if mark.y1 <= a and mark.y0 >= a - reach:
            near = sum(1 for bottom in bottoms.values() if a - 1 - bottom <= touch)
            if near >= share * len(bottoms):
                return True
    for a, b in band_columns:
        if mark.x0 >= b and mark.x1 <= b + reach:
            near = sum(1 for left in rows.values() if left - b <= touch)
            if near >= share * len(rows):
                return True
        if mark.x1 <= a and mark.x0 >= a - reach:
            near = sum(1 for right in rights.values() if a - 1 - right <= touch)
            if near >= share * len(rights):
                return True
    return False


def _joins_band(
    mark: Mark,
    band_rows: list[tuple[int, int]],
    band_columns: list[tuple[int, int]],
    reach: int,
) -> bool:
    """Whether the mark reaches within `reach` px of a removed dark band. A mark that
    does and is also thick (see _is_thick) is a remnant of the backdrop or a shadow,
    such as the backdrop reaching into the top of the gutter where the pages curve into
    the binding, not writing; a pen stroke touching the band is still writing. The
    reach covers a ragged band edge that the straight-mark mask has already taken
    away."""
    near_rows = set()
    for a, b in band_rows:
        near_rows.update(range(b, b + reach + 1))
        near_rows.update(range(a - 1 - reach, a))
    near_columns = set()
    for a, b in band_columns:
        near_columns.update(range(b, b + reach + 1))
        near_columns.update(range(a - reach, a + 1))
    for y, start, end in mark.runs:
        if y in near_rows or start in near_columns or end in near_columns:
            return True
    return False


def _straddlers(marks: list[Mark], cut: _Line, both_px: int) -> list[tuple[str, float, bool]]:
    """For each mark crossing the cut: the side holding most of it, how far it reaches
    past the cut into the other side (working px), and whether it has at least `both_px`
    of ink on each side (writing running through the line rather than touching it)."""
    found = []
    for mark in marks:
        xs = (cut.x_at(mark.y0), cut.x_at(mark.y1))
        if mark.x1 <= min(xs) or mark.x0 >= max(xs) + 1:
            continue
        left = right = 0
        reach_right = reach_left = 0.0
        for y, start, end in mark.runs:
            x = cut.x_at(y + 0.5) + 0.5
            left += max(0, min(end, x) - start)
            right += max(0, end - max(start, x))
            reach_right = max(reach_right, end - x)
            reach_left = max(reach_left, x - start)
        if left > 0 and right > 0:
            both = min(left, right) >= both_px
            if left >= right:
                found.append(("left", reach_right, both))
            else:
                found.append(("right", reach_left, both))
    return found


def _neighbour(
    marks: list[Mark],
    runs: list[tuple[int, int]],
    lines: list[_Line],
    bands: list[tuple[int, int]],
    width: int,
    value: dict[str, Any],
):
    """Evidence of a neighbour strip at each side: (side, line x in working px or None)."""
    strip = value["neighbour_strip_share"] * width
    touch = value["touch_px"]
    left_edge = next((b for a, b in bands if a == 0), 0)
    right_edge = next((a for a, b in bands if b == width), width)
    found = {}
    for side in ("left", "right"):
        if side == "left":
            cut_off = [m for m in marks if m.x0 <= left_edge + touch]
            edges = [ln.x_mid for ln in lines if left_edge < ln.x_mid <= left_edge + strip]
            edges += [b for a, b in bands if a > 0 and b <= left_edge + strip]
            edges = [x for x in edges if any(m.centre_x < x for m in marks)]
            line = max(edges) if edges else None
            if line is None and cut_off and len(runs) > 1 and runs[0][1] <= left_edge + strip:
                line = (runs[0][1] + runs[1][0]) / 2
        else:
            cut_off = [m for m in marks if m.x1 >= right_edge - touch]
            edges = [ln.x_mid for ln in lines if right_edge - strip <= ln.x_mid < right_edge]
            edges += [a for a, b in bands if b < width and a >= right_edge - strip]
            edges = [x for x in edges if any(m.centre_x > x for m in marks)]
            line = min(edges) if edges else None
            if line is None and cut_off and len(runs) > 1 and runs[-1][0] >= right_edge - strip:
                line = (runs[-2][1] + runs[-1][0]) / 2
        if cut_off or line is not None:
            found[side] = (bool(cut_off), line)
    return found


def detect_split(
    image: Image.Image,
    turns: int = 0,
    *,
    dpi: tuple[float, float] | None = None,
    overlap_mm: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Page count and cut for `image` after `turns` quarter turns clockwise.

    `dpi` is the source's resolution (x, y) before turning; when absent it is read from
    the image. `overlap_mm` is the preparation core's overlap past a cut; when absent
    it is read from the core's own settings file, thresholds_prepare.toml, the one
    source of truth for it;
    the setting of the same name is used."""
    if turns not in _TURNS:
        raise DetectorError(f"turns {turns!r} is not 0, 1, 2 or 3")
    value = setting_values(settings)
    overlap = _core_overlap_mm() if overlap_mm is None else float(overlap_mm)
    grey = grey_of(image)
    full_w, full_h = grey.size[::-1] if turns % 2 else grey.size
    work = working_copy(grey, value["split_long_side_px"])
    if _TURNS[turns] is not None:
        work = work.transpose(_TURNS[turns])
    width, height = work.size
    sx, sy = full_w / width, full_h / height
    resolution = _resolution(image, dpi, turns)

    def to_full(line: _Line) -> list[list[float]]:
        points = []
        for y_full in (0, full_h - 1):
            y = (y_full + 0.5) / sy - 0.5
            points.append([round((line.x_at(y) + 0.5) * sx - 0.5, 1), float(y_full)])
        return points

    def full_x(x: float) -> float:
        return round((x + 0.5) * sx - 0.5, 1)

    one_page = {"pages": 1, "cut": None, "method": "none", "part": None, "neighbour": None}
    levels = levels_of(work)
    if levels.contrast < value["min_ink_contrast"]:
        return answer(
            one_page,
            0.0,
            f"The frame's dark and light levels differ by {levels.contrast:.0f} grey levels, "
            f"below {value['min_ink_contrast']}; there is no writing to split by.",
            [TOO_LITTLE_INK],
        )

    # Dark bands (backdrop, book edges, heavy shadow) are not content.
    ink = ink_map(work, levels.threshold)
    dark = value["dark_band_share"] * 255
    narrowest = value["fold_tophat_px"] | 1
    band_columns = _dark_bands([v >= dark for v in profile(ink, along_x=True)], narrowest)
    band_rows = _dark_bands([v >= dark for v in profile(ink, along_x=False)], narrowest)
    r0 = next((b for a, b in band_rows if a == 0), 0)
    r1 = next((a for a, b in band_rows if b == height), height)
    if r1 - r0 < 4:
        r0, r1 = 0, height
    content = ink.copy()
    for a, b in band_columns:
        content.paste(0, (a, 0, b, height))
    for a, b in band_rows:
        content.paste(0, (0, a, width, b))

    # Fold: thin-line part and valley part.
    lines, line_best = _lines(work, (r0, r1), value)
    edge_band = value["edge_band_share"] * width
    folds = [ln for ln in lines if edge_band <= ln.x_mid <= width - edge_band]
    edge_lines = [ln for ln in lines if ln not in folds]
    fold_line = min(folds, key=lambda ln: (abs(ln.x_mid - width / 2), -ln.strength), default=None)
    bright = _bright(work, value)
    valley, valley_depth, valley_edge = _valley(work, (r0, r1), value, bright)
    tone_step = _tone_step(work, (r0, r1), value, bright)

    # Content marks, with straight marks and every found line masked.
    content = mask_straight_marks(content, value["long_mark_share"])
    for ln in lines:
        content = mask_along_line(
            content, (ln.x_at(0), 0), (ln.x_at(height - 1), height - 1), value["fold_mask_half_px"]
        )
    marks = [
        m
        for m in marks_of(content)
        if m.count >= value["speck_px"] and not is_long_thin(m, (width, height), value)
    ]
    kept = sum(m.count for m in marks)
    if kept < value["min_ink_share"] * width * (r1 - r0):
        return answer(
            one_page,
            0.0,
            f"Only {kept} working px of writing remain after dark bands, specks and straight "
            "lines are removed; there is too little writing to split by.",
            [TOO_LITTLE_INK],
        )
    gaps, runs = _gaps(marks, width, value)
    balanced = [g for g in gaps if g.balance >= value["min_balance"]]
    clear = balanced  # gaps no mark crosses: the only ones that confirm a fold
    bridged = False
    if not balanced:
        # A few marks (up to the number a fold may have crossing it) may bridge the
        # gap between the pages: look again with their boxes discounted.
        wider, _ = _gaps(marks, width, value, value["fold_max_crossings"])
        balanced = [g for g in wider if g.balance >= value["min_balance"]]
        bridged = bool(balanced)
        if bridged:
            gaps = wider
    tolerance = value["agree_tolerance_share"] * width

    # The proportions prior.
    if resolution:
        aspect = (full_w / resolution[0]) / (full_h / resolution[1])
        prior_basis = "physical"
    else:
        aspect = full_w / full_h
        prior_basis = "pixel (resolution unknown)"
    prior_pages = 2 if aspect >= value["spread_aspect_min"] else 1
    prior_text = f"the frame's {prior_basis} proportions ({aspect:.2f}) suggest {prior_pages}"

    flags: list[str] = []
    evidence: list[str] = []

    def disagree(reason: str) -> dict[str, Any]:
        return answer(
            one_page,
            0.25,
            f"{reason}; left as one page for review ({prior_text}).",
            [f"page count uncertain: {reason}"],
        )

    if fold_line and valley and abs(fold_line.x_mid - valley.x_at(fold_line.y_mid)) > tolerance:
        return disagree(
            f"a fold line at x={full_x(fold_line.x_mid)} and a shadow valley at "
            f"x={full_x(valley.x_at(fold_line.y_mid))} disagree"
        )
    if valley and valley_edge:
        # A one-sided shadow's steep edge is the fold, unless cutting there would cross
        # more writing than cutting at the darkest column.
        both = value["speck_px"] * 2
        if len(_straddlers(marks, valley_edge, both)) <= len(_straddlers(marks, valley, both)):
            valley = valley_edge
    fold, part = (
        (fold_line, "line") if fold_line else (valley, "valley") if valley else (None, None)
    )
    cut: _Line | None = None
    method = "none"
    confidence = 0.0
    if fold is not None:
        fold_x = fold.x_at(fold.y_mid)
        agreeing = [g for g in clear if g.start - tolerance <= fold_x <= g.end + tolerance]
        if clear and not agreeing:
            gap = max(clear, key=lambda g: g.width)
            return disagree(
                f"a fold {part} at x={full_x(fold_x)} and a content gap at "
                f"x={full_x(gap.middle)} disagree"
            )
        if not agreeing and prior_pages == 1:
            reason = f"a fold {part} at x={full_x(fold_x)} with no content gap around it"
            return answer(
                one_page,
                0.25,
                f"Only {reason}, while {prior_text}; left as one page for review.",
                [f"page count uncertain: only {reason}, and the proportions suggest one page"],
            )
        if part == "line":
            confidence = strength(fold.strength, value["fold_min_excess"])
            evidence.append(
                f"a thin fold line standing out from its neighbours over {fold.strength:.0%} "
                f"of the page height, leaning "
                f"{math.degrees(math.atan(fold.slope)):+.1f} degrees"
            )
            if valley:
                evidence.append("a shadow valley in the same place")
        else:
            confidence = strength(fold.strength, value["valley_min_depth"])
            evidence.append(f"a fold shadow {fold.strength:.0f} grey levels deep")
        if agreeing:
            gap = max(agreeing, key=lambda g: g.width)
            evidence.append(
                f"a content gap {gap.width * sx:.0f} px wide around it (balance {gap.balance:.2f})"
            )
            confidence = 1 - (1 - confidence) * (
                1 - strength(gap.width, value["min_gap_share"] * width)
            )
        cut, method = fold, "fold"
    elif balanced:
        gap = max(balanced, key=lambda g: (g.width, -abs(g.middle - width / 2)))
        at_limit: list[float] = []
        fitting = marks
        if bridged:
            # The few marks that bridge the gap do not bound it: fit the cut to the two
            # pages' writing, and let the overhang check judge the bridging marks.
            fitting = [m for m in marks if m.x1 <= gap.start or m.x0 >= gap.end]
        line = _fit_gap(gap, fitting, value, at_limit)
        if at_limit:
            flags.append(
                f"the gap between the pages leans {value['lean_max_deg']:g} degrees, the "
                "most a cut may lean, or more; the cut may not follow it"
            )
        paper_break = False
        faint = None
        if valley is None and valley_depth >= value["faint_shadow_depth"]:
            faint, _, _ = _valley(
                work, (r0, r1), {**value, "valley_min_depth": value["faint_shadow_depth"]}, bright
            )
        if (
            faint is not None
            and gap.start - tolerance <= faint.x_at(faint.y_mid) <= gap.end + tolerance
        ):
            # A shadow too faint to be a fold by itself still marks where the gutter
            # is inside the gap.
            line = faint
            paper_break = True
            evidence.append(
                f"a faint shadow {valley_depth:.0f} grey levels deep inside the gap places the cut"
            )
        elif tone_step is not None and gap.start - tolerance <= tone_step <= gap.end + tolerance:
            # Where the two pages' paper differs in tone, the step between them is
            # where they meet; the gap's middle is pulled by ragged line ends.
            line = _Line(float(tone_step), line.y_mid, 0.0, gap.width)
            paper_break = True
            evidence.append(
                f"a step in paper tone at x={full_x(tone_step)} inside the gap places the cut"
            )
        reason = (
            f"a content gap {gap.width * sx:.0f} px wide at x={full_x(gap.middle)} "
            f"(balance {gap.balance:.2f}"
            + (", crossed by a few marks" if bridged else "")
            + ") and no fold"
        )
        if prior_pages == 1:
            return answer(
                one_page,
                0.25,
                f"Only {reason}, while {prior_text}; left as one page for review.",
                [f"page count uncertain: only {reason}, and the proportions suggest one page"],
            )
        evidence.append(reason)
        confidence = strength(gap.width, value["min_gap_share"] * width)
        cut, method, part = line, "gap", None
        # An edge in the paper inside the gap (a dark band or a thin line, as where two
        # pages or a page edge meet) also breaks the paper.
        if any(gap.start <= (a + b) / 2 <= gap.end for a, b in band_columns) or any(
            gap.start - tolerance <= ln.x_mid <= gap.end + tolerance for ln in lines
        ):
            paper_break = True
        if not paper_break:
            flags.append(
                "two pages decided from an empty band alone; the paper runs unbroken across "
                "it (no fold line, no shadow, no change in tone or edge); check whether this "
                "is one sheet"
            )

    if cut is not None:
        if prior_pages != 2:
            confidence *= value["prior_disagree_factor"]
        # Dark bands at the frame's edges are backdrop; a dark band inside the frame may
        # be a gutter shadow that writing runs into, so it does not excuse a mark.
        outer_columns = [(a, b) for a, b in band_columns if a == 0 or b == width]
        crossing_marks = []
        excused = []
        dropped: list[Mark] = []
        for m in marks:
            if not _joins_band(m, band_rows, outer_columns, value["touch_px"]):
                crossing_marks.append(m)
                continue
            pieces = _thin_parts(m, value["backdrop_min_thickness_px"])
            if pieces is None:
                crossing_marks.append(m)
            else:
                excused.append(m)
                # Short corner fringes, and fringes of a band's own ragged edge, are
                # dropped (and checked below); the long thin pieces (a pen stroke, even
                # when the thick part cut it in two) stay together as one mark.
                least = 3 * value["backdrop_min_thickness_px"]
                strokes = []
                for p in pieces:
                    if max(p.width, p.height) >= least and not _band_fringe(
                        p,
                        band_rows,
                        outer_columns,
                        value["touch_px"] + value["backdrop_min_thickness_px"],
                        value["touch_px"],
                        value["fringe_touch_share"],
                    ):
                        strokes.append(p)
                    else:
                        dropped.append(p)
                if strokes:
                    runs = sorted(r for p in strokes for r in p.runs)
                    crossing_marks.append(
                        Mark(
                            min(p.x0 for p in strokes),
                            min(p.y0 for p in strokes),
                            max(p.x1 for p in strokes),
                            max(p.y1 for p in strokes),
                            sum(p.count for p in strokes),
                            runs,
                        )
                    )
        joined = len(_straddlers(excused, cut, value["speck_px"] * 2))
        dropped_reach = (
            max((reach for _, reach, _ in _straddlers(dropped, cut, 1)), default=0.0) * sx
        )
        straddling = _straddlers(crossing_marks, cut, value["speck_px"] * 2)
        crossing = sum(1 for *_, both in straddling if both)
        if crossing > value["fold_max_crossings"]:
            reason = (
                f"the {method} cut at x={full_x(cut.x_at(cut.y_mid))} is crossed by "
                f"{crossing} marks of writing with ink on both sides, so it looks like a rule "
                "or a crease inside a page, not a fold between pages"
            )
            return answer(
                one_page,
                0.2,
                f"One page for review: {reason}; {prior_text}.",
                [f"page count uncertain: {reason}"],
            )
        widest = max((reach for _, reach, _ in straddling), default=0.0) * sx
        if straddling:
            sides = sorted({side for side, _, _ in straddling})
            if resolution:
                widest_mm = widest / resolution[0] * 25.4
                evidence.append(
                    f"{_count(len(straddling))} the cut (kept mostly on the "
                    f"{' and '.join(sides)} side), the widest overhanging it by "
                    f"{widest_mm:.1f} mm"
                )
                # The overhang is measured on the working copy, so one working px of
                # doubt is counted against it: a stroke flagged here may just fit, one
                # passed here does fit, whole, on the page holding most of it.
                doubt_mm = sx / resolution[0] * 25.4
                if widest_mm + doubt_mm > overlap:
                    beyond = "more than" if widest_mm > overlap else "too close to"
                    flags.append(
                        f"writing across the cut: a mark overhangs it by {widest_mm:.1f} mm, "
                        f"{beyond} the {overlap:g} mm overlap kept past the cut, so it may "
                        "be whole on neither page and part of it lost"
                    )
            else:
                evidence.append(
                    f"{_count(len(straddling))} the cut, the widest overhanging it by "
                    f"{widest:.0f} px"
                )
                flags.append(
                    "writing across the cut, and the resolution is unknown, so the overhang "
                    "cannot be compared with the overlap"
                )
        else:
            evidence.append("no ink mark crosses the cut")
        if joined:
            evidence.append(
                f"{joined} dark mark{'s' if joined != 1 else ''} crossing the cut "
                f"{'are' if joined != 1 else 'is'} joined to a dark band; the part too broad "
                "for a pen stroke (backdrop or shadow) is not counted as writing, any pen "
                "stroke in it is"
            )
        if dropped_reach and resolution:
            dropped_mm = dropped_reach / resolution[0] * 25.4
            if dropped_mm > overlap:
                flags.append(
                    f"a mark joined to the backdrop crosses the cut by {dropped_mm:.1f} mm, "
                    f"more than the {overlap:g} mm overlap; it was taken for the backdrop's "
                    "edge, so check it is not writing"
                )
        result = {"pages": 2, "cut": to_full(cut), "method": method, "part": part}
        result["neighbour"] = None
        return answer(
            result,
            min(1.0, confidence),
            f"Two pages: {'; '.join(evidence)}; {prior_text}.",
            flags,
        )

    # One page. Weak cues send it to review.
    weak = []
    share = value["weak_cue_share"]
    if line_best >= share * value["fold_min_excess"]:
        weak.append(
            f"a thin line standing out from its neighbours over {line_best:.0%} of the page height"
        )
    if valley_depth >= share * value["valley_min_depth"]:
        weak.append(f"a shadow {valley_depth:.0f} grey levels deep")
    if gaps:
        gap = max(gaps, key=lambda g: g.width)
        weak.append(
            f"a content gap at x={full_x(gap.middle)} whose sides are unbalanced "
            f"({gap.balance:.2f})"
        )
    ratio = max(line_best / value["fold_min_excess"], valley_depth / value["valley_min_depth"])
    confidence = max(0.0, min(1.0, 1 - 0.5 * ratio))
    if prior_pages != 1:
        confidence *= value["prior_disagree_factor"]
    if weak:
        confidence = min(confidence, 0.4)
        flags.append(f"page count uncertain: {'; '.join(weak)}, none strong enough to cut")
    middle = width / 2
    one_side = None
    if max(m.x1 for m in marks) <= middle + tolerance:
        one_side = "left"
    elif min(m.x0 for m in marks) >= middle - tolerance:
        one_side = "right"
    extent = (max(m.x1 for m in marks) - min(m.x0 for m in marks)) / width
    if prior_pages == 2 and not one_side and extent >= value["spread_content_share"] and not weak:
        confidence = min(confidence, 0.4)
        flags.append(
            f"page count uncertain: the proportions suggest two pages and the writing spans "
            f"{extent:.0%} of the width, but no fold, shadow or gap decides; it may be a "
            "spread whose gutter shows nothing"
        )
    if prior_pages == 2 and one_side:
        confidence = min(confidence, 0.4)
        flags.append(
            f"possible spread with a blank page: all the writing lies on the {one_side} of "
            "the middle and the proportions suggest two pages"
        )

    result = dict(one_page)
    neighbour = _neighbour(marks, runs, edge_lines, band_columns, width, value)
    if len(neighbour) == 2:
        flags.append(
            "writing or page-edge lines at both sides; whether a neighbour strip is in the "
            "frame is left to review"
        )
    elif neighbour:
        side, (cut_off, line_x) = next(iter(neighbour.items()))
        line = (
            None if line_x is None else [[full_x(line_x), 0.0], [full_x(line_x), float(full_h - 1)]]
        )
        what = []
        if cut_off:
            what.append("writing cut off by the frame edge")
        if line_x is not None:
            what.append(f"a page edge or gap at x={full_x(line_x)} with ink beyond it")
        flags.append(f"neighbour strip on the {side}: {' and '.join(what)}")
        result["method"] = "neighbour edge"
        result["neighbour"] = {"side": side, "line": line}
    if not weak and not neighbour:
        evidence.append("no fold line, no fold shadow and one mass of writing")
    elif not weak:
        evidence.append("no fold line or shadow near the middle")
    else:
        evidence.append("no cue strong enough to cut")
    return answer(result, confidence, f"One page: {'; '.join(evidence)}; {prior_text}.", flags)
