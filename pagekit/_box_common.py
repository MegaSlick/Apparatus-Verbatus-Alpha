"""Shared helpers for the skew, page-box and content-box detectors.

Nothing here writes a file. Every image here is a reduced working copy made with
Pillow's area-averaging reduction; binary maps hold 255 for the marked class and 0
elsewhere. Pixel work goes through Pillow's C operations; Python walks runs of pixels
(one regular-expression pass per row) and small profiles.

Morphology (erosion, dilation, opening, closing and reconstruction by connected
components) follows the usual definitions; reconstruction from markers is done by
keeping each connected component that holds a marker, which is what reconstruction
by dilation yields on a binary image (L. Vincent, "Morphological grayscale
reconstruction in image analysis: applications and efficient algorithms", IEEE
Transactions on Image Processing, 1993).

The ink threshold is Otsu's (N. Otsu, "A Threshold Selection Method from Gray-Level
Histograms", IEEE Trans. Systems, Man, and Cybernetics, SMC-9(1):62-66, 1979), taken
from pagekit's crop check.
"""

from __future__ import annotations

import math
import re
import tomllib
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

THRESHOLDS_PATH = Path(__file__).with_name("thresholds_skew.toml")
ANSWER_KEYS = ("value", "confidence", "evidence", "flags")
MARK = 255
_RUN = re.compile(rb"\xff+")
_MAX_BOX_RADIUS = 127  # keeps a one-pixel difference visible in an 8-bit box mean
_COLOUR_MODES = frozenset({"RGB", "RGBA", "RGBX", "CMYK", "YCbCr", "P", "PA"})
_GREY_MODES = frozenset({"1", "L", "LA"})

Box = tuple[int, int, int, int]
Point = tuple[float, float]


class DetectorInputError(ValueError):
    """The page cannot be measured as given (bad image mode, resolution or polygon)."""


# --- Settings and answers ------------------------------------------------------------


def load_thresholds(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The settings table of this slice, with any overrides applied and marked as such."""
    with THRESHOLDS_PATH.open("rb") as handle:
        table = tomllib.load(handle)
    thresholds = {
        name: {"value": entry["value"], "status": entry["status"], "source": "default"}
        for name, entry in sorted(table.items())
    }
    for name, value in sorted((overrides or {}).items()):
        if name not in thresholds:
            raise DetectorInputError(f"unknown setting {name!r}")
        default = thresholds[name]["value"]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise DetectorInputError(f"setting {name!r} must be a number like {default!r}")
        if isinstance(default, int) and not isinstance(value, int):
            raise DetectorInputError(f"setting {name!r} must be a whole number")
        thresholds[name] = {"value": value, "status": "UNMEASURED", "source": "override"}
    return thresholds


def values(thresholds: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {name: entry["value"] for name, entry in thresholds.items()}


def unmeasured_note(thresholds: dict[str, dict[str, Any]], names: tuple[str, ...]) -> str:
    """A short clause for the evidence when any setting the detector read is unmeasured."""
    if all(thresholds[name]["status"] == "MEASURED" for name in names):
        return ""
    return " Settings are unmeasured starting guesses."


def answer(value: Any, confidence: float, evidence: str, flags: list[str]) -> dict:
    """A detector answer: exactly value, confidence, evidence, flags."""
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise TypeError("confidence must be a number")
    if not 0.0 <= confidence <= 1.0 or math.isnan(confidence):
        raise ValueError(f"confidence {confidence!r} is not between 0 and 1")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence must be a non-empty sentence")
    if not isinstance(flags, list) or not all(isinstance(f, str) and f for f in flags):
        raise ValueError("flags must be a list of non-empty strings")
    return {
        "value": value,
        "confidence": round(float(confidence), 3),
        "evidence": evidence.strip(),
        "flags": list(flags),
    }


# --- Input and working copies -----------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One upright page as given: grey image, optional colour, resolution and area."""

    grey: Image.Image
    colour: Image.Image | None
    dpi: tuple[float, float]
    polygon: tuple[Point, ...] | None
    flags: tuple[str, ...] = ()


def page_input(
    image: Image.Image, dpi: Any, polygon: Any = None, v: dict[str, Any] | None = None
) -> Page:
    """The page as the detectors measure it.

    With settings `v`, a resolution that implies a sheet outside the plausible size
    range is replaced by the fallback resolution, and a flag says so."""
    if not isinstance(image, Image.Image):
        raise DetectorInputError("the page must be a Pillow image")
    if getattr(image, "n_frames", 1) != 1:
        raise DetectorInputError("the page has more than one frame")
    if image.width < 8 or image.height < 8:
        raise DetectorInputError(f"the page is too small to measure ({image.width}x{image.height})")
    if image.mode in _GREY_MODES:
        grey, colour = image.convert("L"), None
    elif image.mode in _COLOUR_MODES:
        colour = image.convert("RGB")
        grey = colour.convert("L")
    else:
        raise DetectorInputError(f"image mode {image.mode!r} is not supported")
    try:
        dx, dy = (float(component) for component in dpi)
    except (TypeError, ValueError) as error:
        raise DetectorInputError("the resolution must be two numbers, x and y") from error
    if not all(math.isfinite(d) and d > 0 for d in (dx, dy)):
        raise DetectorInputError(f"the resolution {dpi!r} is not two positive numbers")
    points = None
    if polygon is not None:
        try:
            points = tuple((float(x), float(y)) for x, y in polygon)
        except (TypeError, ValueError) as error:
            raise DetectorInputError("the polygon must be a list of (x, y) points") from error
        if len(points) < 3 or not all(math.isfinite(c) for p in points for c in p):
            raise DetectorInputError("the polygon needs at least three finite points")
    flags: tuple[str, ...] = ()
    if v is not None:
        sides = (grey.width / dx * 25.4, grey.height / dy * 25.4)
        low, high = v["plausible_page_min_mm"], v["plausible_page_max_mm"]
        if not all(low <= side <= high for side in sides):
            fallback = float(v["fallback_dpi"])
            flags = (
                f"The resolution {dx:g} x {dy:g} dpi implies a sheet of {sides[0]:.3g} by "
                f"{sides[1]:.3g} mm, outside {low:g} to {high:g} mm; the page is measured as "
                f"if it were {fallback:g} dpi.",
            )
            dx = dy = fallback
    return Page(grey, colour, (dx, dy), points, flags)


@dataclass(frozen=True)
class Work:
    """A reduced working copy with square pixels at `dpi`, and its mask of the page area."""

    grey: Image.Image
    mask: Image.Image  # 255 inside the page's own area
    dpi: float
    sx: float  # working pixels per source pixel, x
    sy: float
    colour: Image.Image | None = None

    @property
    def mm(self) -> float:
        """Millimetres per working pixel."""
        return 25.4 / self.dpi

    def px(self, millimetres: float) -> int:
        return max(1, round(millimetres / self.mm))

    def to_source(self, box: Box, limit: tuple[int, int]) -> list[int]:
        """A working-grid box as a source-grid box, rounded outward and clamped."""
        x0, y0, x1, y1 = box
        width, height = limit
        return [
            max(0, math.floor(x0 / self.sx)),
            max(0, math.floor(y0 / self.sy)),
            min(width, math.ceil(x1 / self.sx)),
            min(height, math.ceil(y1 / self.sy)),
        ]

    def from_source(self, box: Box) -> Box:
        """A source-grid box as a working-grid box, rounded inward and clamped."""
        x0, y0, x1, y1 = box
        width, height = self.grey.size
        left = min(width, max(0, math.ceil(x0 * self.sx - 1e-6)))
        top = min(height, max(0, math.ceil(y0 * self.sy - 1e-6)))
        right = max(left, min(width, math.floor(x1 * self.sx + 1e-6)))
        bottom = max(top, min(height, math.floor(y1 * self.sy + 1e-6)))
        return left, top, right, bottom


def working_copy(page: Page, target_dpi: float) -> Work:
    """The page reduced by area averaging to `target_dpi` on both axes (never enlarged)."""
    dx, dy = page.dpi
    dpi = min(float(target_dpi), dx, dy)
    width, height = page.grey.size
    size = (max(8, round(width * dpi / dx)), max(8, round(height * dpi / dy)))
    sx, sy = size[0] / width, size[1] / height
    grey = page.grey if size == page.grey.size else page.grey.resize(size, Image.BOX)
    colour = None
    if page.colour is not None:
        colour = page.colour if size == page.colour.size else page.colour.resize(size, Image.BOX)
    mask = Image.new("L", size, MARK)
    if page.polygon is not None:
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon([(x * sx, y * sy) for x, y in page.polygon], fill=MARK)
    return Work(grey, mask, dpi, sx, sy, colour)


# --- Grey levels -------------------------------------------------------------------


def class_means(histogram: list[int], threshold: int) -> tuple[float | None, float | None]:
    def mean(levels: range) -> float | None:
        count = sum(histogram[level] for level in levels)
        if count == 0:
            return None
        return sum(level * histogram[level] for level in levels) / count

    return mean(range(threshold + 1)), mean(range(threshold + 1, 256))


def median_level(histogram: list[int], levels: range = range(256)) -> int | None:
    count = sum(histogram[level] for level in levels)
    if count == 0:
        return None
    seen = 0
    for level in levels:
        seen += histogram[level]
        if seen * 2 >= count:
            return level
    return None


def threshold_map(
    grey: Image.Image, threshold: int, mask: Image.Image | None = None
) -> Image.Image:
    """255 where the grey level is at or below `threshold` (inside `mask`)."""
    marked = grey.point(lambda level: MARK if level <= threshold else 0)
    if mask is not None:
        marked = ImageChops.multiply(marked, mask)
    return marked


def flatten(grey: Image.Image, smoothing_px: int) -> Image.Image:
    """Divide out a slow brightness gradient.

    The paper background is estimated on a copy reduced to cells a quarter of the
    smoothing size: a closing (brightest, then darkest, over the smoothing size) removes
    the writing, a blur softens it, and it is enlarged back. The result is the page minus
    that background, shifted so bare paper sits at 255; ink keeps its darkness relative
    to the paper around it. Flat-field correction by background estimation is the
    general technique; the grey-level closing used to estimate the
    background is general image-processing knowledge.
    """
    width, height = grey.size
    cell = max(1, smoothing_px // 4)
    small = grey.resize(
        (max(1, math.ceil(width / cell)), max(1, math.ceil(height / cell))), Image.BOX
    )
    size = 5  # cells: about the smoothing size
    # The closing runs on a copy extended by repeating its edge cells, so a gradient that
    # darkens toward the edge is not overestimated there (which reads as faint ink).
    pad = size
    padded = _extend(small, pad)
    background = padded.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
    background = background.filter(ImageFilter.GaussianBlur(1))
    background = background.crop((pad, pad, pad + small.width, pad + small.height))
    background = background.resize((width, height), Image.BILINEAR)
    return ImageChops.subtract(grey, background, 1.0, 255)


def _extend(image: Image.Image, pad: int) -> Image.Image:
    """The image with `pad` pixels added on every side, each a copy of the nearest edge."""
    width, height = image.size
    out = Image.new(image.mode, (width + 2 * pad, height + 2 * pad))
    out.paste(image, (pad, pad))
    out.paste(image.crop((0, 0, 1, height)).resize((pad, height)), (0, pad))
    out.paste(image.crop((width - 1, 0, width, height)).resize((pad, height)), (pad + width, pad))
    top = out.crop((0, pad, width + 2 * pad, pad + 1))
    bottom = out.crop((0, pad + height - 1, width + 2 * pad, pad + height))
    out.paste(top.resize((width + 2 * pad, pad)), (0, 0))
    out.paste(bottom.resize((width + 2 * pad, pad)), (0, pad + height))
    return out


# --- Binary morphology -----------------------------------------------------------------


def _radii(radius: int) -> list[int]:
    chunks = []
    while radius > 0:
        step = min(radius, _MAX_BOX_RADIUS)
        chunks.append(step)
        radius -= step
    return chunks


def erode(marks: Image.Image, rx: int, ry: int) -> Image.Image:
    """Erosion by a (2rx+1) by (2ry+1) rectangle, as separable box means."""
    for r in _radii(rx):
        marks = marks.filter(ImageFilter.BoxBlur((r, 0))).point(lambda v: MARK if v == MARK else 0)
    for r in _radii(ry):
        marks = marks.filter(ImageFilter.BoxBlur((0, r))).point(lambda v: MARK if v == MARK else 0)
    return marks


def dilate(marks: Image.Image, rx: int, ry: int) -> Image.Image:
    """Dilation by a (2rx+1) by (2ry+1) rectangle, as separable box means."""
    for r in _radii(rx):
        marks = marks.filter(ImageFilter.BoxBlur((r, 0))).point(lambda v: MARK if v else 0)
    for r in _radii(ry):
        marks = marks.filter(ImageFilter.BoxBlur((0, r))).point(lambda v: MARK if v else 0)
    return marks


def opening(marks: Image.Image, rx: int, ry: int) -> Image.Image:
    return dilate(erode(marks, rx, ry), rx, ry)


def closing(marks: Image.Image, rx: int, ry: int) -> Image.Image:
    return erode(dilate(marks, rx, ry), rx, ry)


def long_lines(ink: Image.Image, length_px: int, fatten_px: int, thickest_px: float) -> Image.Image:
    """Ink pixels on long straight horizontal or vertical lines: ruled lines.

    Ink is thickened across the run direction by `fatten_px` so that a slightly tilted
    rule still makes one unbroken run, and an opening with a line element `length_px`
    long keeps only such runs; writing is cut by word gaps long before that. A straight
    line is fitted by least squares to each surviving run and the rule's own thickness
    is measured along it (ink near the line per unit of length). Only ink within that
    thickness of the line is returned, so writing that touches or crosses a rule keeps
    everything but the rule's own pixels. A run thicker than `thickest_px` is not a rule.
    """
    radius = max(1, length_px // 2)
    found = Image.new("L", ink.size, 0)
    for horizontal in (True, False):
        marks = ink if horizontal else ink.transpose(Image.Transpose.TRANSPOSE)
        survivors = opening(dilate(marks, 0, fatten_px), radius, 0)
        lines = Image.new("L", marks.size, 0)
        draw = ImageDraw.Draw(lines)
        for part in components(survivors):
            if part.width < length_px:
                continue
            slope, intercept = fit_line(part)
            ends = [(x, intercept + slope * x) for x in (part.x0, part.x1 - 1)]
            band = Image.new("L", marks.size, 0)
            ImageDraw.Draw(band).line(ends, fill=MARK, width=2 * fatten_px + 1)
            thickness = count(ImageChops.multiply(marks, band)) / part.width
            if thickness > thickest_px:
                continue
            draw.line(ends, fill=MARK, width=max(1, round(thickness)) + 1)
        if not horizontal:
            lines = lines.transpose(Image.Transpose.TRANSPOSE)
        found = ImageChops.lighter(found, lines)
    return ImageChops.multiply(ink, found)


def rule_shaped(part: Component, length_px: int, thickest_px: float, tilt_deg: float) -> bool:
    """Long, thin and nearly straight: a ruled line by its shape.

    Long: at least `length_px` along its longer side. Thin: its mean thickness (pixels
    per unit of length) at most `thickest_px`. Nearly straight: its shorter side no more
    than a line of that length tilted by `tilt_deg` would span, plus its thickness.
    """
    length = max(part.width, part.height)
    across = min(part.width, part.height)
    if length < length_px or part.area / length > thickest_px:
        return False
    return across <= length * math.tan(math.radians(tilt_deg)) + 2 * thickest_px + 1


def edge_line(part: Component, upright: bool, work: Work, v: dict[str, Any]) -> bool:
    """A long thin line running along a side (vertical for the left and right sides,
    `upright`; horizontal for the top and bottom): an edge of the sheet or of the pages
    under it, a board edge or a thin shadow, not writing. Long: at least
    `edge_line_min_mm`; thin: its mean thickness (area over length) at most
    `edge_line_max_mm`; along the side: tilted from it by no more than 5 degrees, and at
    least eight times as long as it is wide."""
    along, across = (part.height, part.width) if upright else (part.width, part.height)
    thickness = part.area / along
    return (
        along * work.mm >= v["edge_line_min_mm"]
        and thickness * work.mm <= v["edge_line_max_mm"]
        and across <= along * math.tan(math.radians(5)) + v["edge_line_max_mm"] / work.mm
        and along >= 8 * across
    )


def edge_line_pieces(
    parts: list[Component], upright: bool, work: Work, v: dict[str, Any]
) -> set[int]:
    """The ids of the components that are lines, or pieces of lines, running along a
    side (vertical for left and right, `upright`).

    A page edge or board edge is often broken into dashes. At each position across the
    side (give or take a pixel, as the line wobbles), the thin pieces lying there are
    gathered; when three or more of them reach from end to end over an edge line's
    length, that position is a line band, and a thin piece lying mostly in a band is part
    of that line (real-register follow-up, S3 and S4)."""
    longest = v["edge_line_min_mm"] / work.mm
    thickest = v["edge_line_max_mm"] / work.mm

    def across(part: Component) -> tuple[int, int]:
        return (part.x0, part.x1) if upright else (part.y0, part.y1)

    def along(part: Component) -> tuple[int, int]:
        return (part.y0, part.y1) if upright else (part.x0, part.x1)

    thin = [p for p in parts if across(p)[1] - across(p)[0] <= thickest]
    reach: dict[int, list[tuple[int, int]]] = {}
    for part in thin:
        low, high = across(part)
        for position in range(low - 1, high + 1):
            reach.setdefault(position, []).append(along(part))
    band = {
        position
        for position, spans in reach.items()
        if len(spans) >= 3 and max(b for _, b in spans) - min(a for a, _ in spans) >= longest
    }
    found = set()
    for part in parts:
        low, high = across(part)
        inside = sum(1 for position in range(low, high) if position in band)
        if edge_line(part, upright, work, v) or (
            high - low <= thickest and inside * 2 >= high - low
        ):
            found.add(id(part))
    return found


def fit_line(part: Component) -> tuple[float, float]:
    """Least-squares line y = intercept + slope * x through a component's pixels."""
    n = sx = sy = sxx = sxy = 0.0
    for y, x0, x1 in part.runs:
        length = x1 - x0
        total_x = (x0 + x1 - 1) * length / 2
        n += length
        sx += total_x
        sy += y * length
        sxx += (x1 - 1) * x1 * (2 * x1 - 1) / 6 - (x0 - 1) * x0 * (2 * x0 - 1) / 6
        sxy += y * total_x
    variance = sxx - sx * sx / n
    slope = 0.0 if variance <= 0 else (sxy - sx * sy / n) / variance
    return slope, (sy - slope * sx) / n


# --- Connected components ----------------------------------------------------------


class Component:
    """An 8-connected set of marked pixels, kept as row runs (y, x0, x1), x1 exclusive."""

    __slots__ = ("runs", "area", "x0", "y0", "x1", "y1")

    def __init__(self, runs: list[tuple[int, int, int]]) -> None:
        self.runs = runs
        self.area = sum(x1 - x0 for _, x0, x1 in runs)
        self.x0 = min(r[1] for r in runs)
        self.x1 = max(r[2] for r in runs)
        self.y0 = runs[0][0]
        self.y1 = runs[-1][0] + 1

    @property
    def box(self) -> Box:
        return self.x0, self.y0, self.x1, self.y1

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def fill(self) -> float:
        return self.area / (self.width * self.height)

    def touches(self, marks: bytes, width: int) -> bool:
        """Whether any of this component's pixels is marked in `marks` (a map's bytes)."""
        return any(b"\xff" in marks[y * width + x0 : y * width + x1] for y, x0, x1 in self.runs)


def components(marks: Image.Image) -> list[Component]:
    """The 8-connected components of a binary map, in raster order of their first pixel."""
    width, height = marks.size
    data = marks.tobytes()
    runs: list[tuple[int, int, int]] = []
    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    previous: list[int] = []  # run indices of the row above
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        current = []
        for match in _RUN.finditer(row):
            index = len(runs)
            runs.append((y, match.start(), match.end()))
            parent.append(index)
            current.append(index)
        # Two runs touch (8-connected) when they overlap after widening by one pixel.
        i = j = 0
        while i < len(current) and j < len(previous):
            _, a0, a1 = runs[current[i]]
            _, b0, b1 = runs[previous[j]]
            if b0 <= a1 and a0 <= b1:
                ra, rb = find(current[i]), find(previous[j])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            if a1 < b1:
                i += 1
            else:
                j += 1
        previous = current
    groups: dict[int, list[tuple[int, int, int]]] = {}
    for index, run in enumerate(runs):
        groups.setdefault(find(index), []).append(run)
    return [Component(groups[root]) for root in sorted(groups)]


def paint(parts: list[Component], size: tuple[int, int]) -> Image.Image:
    """A binary map holding exactly the given components."""
    width, height = size
    buffer = bytearray(width * height)
    for part in parts:
        for y, x0, x1 in part.runs:
            buffer[y * width + x0 : y * width + x1] = b"\xff" * (x1 - x0)
    return Image.frombytes("L", size, bytes(buffer))


def union_box(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def profile(marks: Image.Image, along_x: bool) -> list[float]:
    """Share of marked pixels per column (along_x) or per row, as floats."""
    width, height = marks.size
    size = (width, 1) if along_x else (1, height)
    reduced = marks.convert("F").resize(size, Image.BOX)
    return [value / MARK for value in array("f", reduced.tobytes())]


def run_count(marks: Image.Image) -> int:
    """Number of horizontal runs of marked pixels: a cheap measure of how busy a map is."""
    return count(ImageChops.subtract(marks, ImageChops.offset(marks, 1, 0)))


def busy(marks: Image.Image, work: Work, v: dict[str, Any]) -> str | None:
    """A reason when a binary map is too busy to be writing (noise, grain, a halftone),
    measured before any component is labelled so such a page cannot take long."""
    runs = run_count(marks)
    area_cm2 = marks.width * marks.height * (work.mm / 10) ** 2
    density = runs / area_cm2 * 100 / work.dpi if area_cm2 else 0.0
    if density > v["busy_runs_per_cm2"] or runs > v["busy_max_runs"]:
        return (
            f"The page looks like noise: {runs} runs of dark pixels, {density:.0f} per cm\u00b2 "
            f"at 100 dpi (more than {v['busy_runs_per_cm2']:g}), far more than writing makes."
        )
    return None


def count(marks: Image.Image) -> int:
    return marks.histogram()[MARK]
