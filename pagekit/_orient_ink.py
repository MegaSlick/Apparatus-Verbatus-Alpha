"""Shared ground for the orientation and split detectors.

Settings, the answer shape, reduced working copies, the ink map, masking of long
straight marks and connected components. Everything here works on a reduced working
copy and writes no file.

Ink is separated from paper with Otsu's threshold (N. Otsu, "A Threshold Selection
Method from Gray-Level Histograms", IEEE Trans. Systems, Man, and Cybernetics,
SMC-9(1):62-66, 1979), reused from pagekit's crop check. Connected components are
labelled from runs of ink joined across rows with a union-find, a form of the
sequential labelling of A. Rosenfeld and J. L. Pfaltz, "Sequential Operations in
Digital Picture Processing", Journal of the ACM 13(4):471-494, 1966.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from pagekit.check import SUPPORTED_MODES, otsu_threshold

SETTINGS_PATH = Path(__file__).with_name("thresholds_split.toml")
INK = 255
ANSWER_KEYS = ("value", "confidence", "evidence", "flags")
TOO_LITTLE_INK = "too little ink to decide (the page is blank or nearly blank)"

_INK_RUN = re.compile(b"\xff+")
_MASK_RUN = re.compile(b"\x01+")


class DetectorError(ValueError):
    """The image or the settings cannot be used as given."""


# --- Settings and the answer shape --------------------------------------------------------


def load_settings(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The settings table, each entry with its value, status and source."""
    with SETTINGS_PATH.open("rb") as handle:
        table = tomllib.load(handle)
    settings = {
        name: {"value": entry["value"], "status": entry["status"], "source": "default"}
        for name, entry in sorted(table.items())
    }
    for name, value in sorted((overrides or {}).items()):
        if name not in settings:
            raise DetectorError(f"unknown setting {name!r}")
        default = settings[name]["value"]
        if isinstance(value, bool) or not isinstance(value, type(default) | int):
            raise DetectorError(f"setting {name!r} must be a number like {default!r}")
        settings[name] = {"value": value, "status": "UNMEASURED", "source": "override"}
    return settings


def setting_values(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return {name: entry["value"] for name, entry in load_settings(overrides).items()}


def settings_measured(overrides: dict[str, Any] | None = None) -> bool:
    """True only when every setting has been measured; today none has."""
    return all(entry["status"] == "MEASURED" for entry in load_settings(overrides).values())


def answer(value: Any, confidence: float, evidence: str, flags: list[str]) -> dict[str, Any]:
    """A detector's answer: exactly value, confidence, evidence and flags."""
    if not 0.0 <= confidence <= 1.0 or math.isnan(confidence):
        raise DetectorError(f"confidence {confidence!r} is not between 0 and 1")
    if not isinstance(evidence, str) or not evidence:
        raise DetectorError("evidence must be a non-empty sentence")
    if not all(isinstance(flag, str) and flag for flag in flags):
        raise DetectorError("flags must be non-empty strings")
    return {
        "value": value,
        "confidence": round(float(confidence), 3),
        "evidence": evidence,
        "flags": list(flags),
    }


def strength(measure: float, threshold: float) -> float:
    """0.5 at the threshold, rising toward 1 above it and falling toward 0 below it."""
    if threshold <= 0:
        return 1.0
    measure = max(0.0, measure)
    return measure / (measure + threshold)


# --- Working copies and the ink map ----------------------------------------------------------


def grey_of(image: Image.Image) -> Image.Image:
    """The image as 8-bit grey; modes pagekit does not handle are refused loudly."""
    if image.mode not in SUPPORTED_MODES:
        raise DetectorError(f"image mode {image.mode!r} is not handled")
    if image.mode == "L":
        return image
    if image.mode in ("P", "PA"):
        image = image.convert("RGBA" if image.mode == "PA" else "RGB")
    return image.convert("L")


def working_copy(grey: Image.Image, long_side: int) -> Image.Image:
    """A reduced copy whose long side is at most `long_side`; never enlarged."""
    width, height = grey.size
    scale = long_side / max(width, height)
    if scale >= 1.0:
        return grey.copy()
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return grey.resize(size, Image.Resampling.BOX)


@dataclass(frozen=True)
class Levels:
    threshold: int
    dark_mean: float | None
    light_mean: float | None
    dark_share: float

    @property
    def contrast(self) -> float:
        if self.dark_mean is None or self.light_mean is None:
            return 0.0
        return self.light_mean - self.dark_mean


def levels_of(grey: Image.Image) -> Levels:
    histogram = grey.histogram()
    threshold = otsu_threshold(histogram)
    total = sum(histogram) or 1
    dark = histogram[: threshold + 1]
    light = histogram[threshold + 1 :]
    dark_count, light_count = sum(dark), sum(light)
    dark_mean = sum(i * c for i, c in enumerate(dark)) / dark_count if dark_count else None
    light_mean = (
        sum((threshold + 1 + i) * c for i, c in enumerate(light)) / light_count
        if light_count
        else None
    )
    return Levels(threshold, dark_mean, light_mean, dark_count / total)


def ink_map(grey: Image.Image, threshold: int) -> Image.Image:
    """255 where the grey level is at or below the threshold, else 0."""
    return grey.point(lambda level: INK if level <= threshold else 0)


def profile(image: Image.Image, along_x: bool) -> list[float]:
    """Mean level per column (along_x) or per row, 0 to 255."""
    width, height = image.size
    size = (width, 1) if along_x else (1, height)
    return list(image.resize(size, Image.Resampling.BOX).tobytes())


def runs_of(flags: list[bool]) -> list[tuple[int, int]]:
    """Half-open runs of True."""
    runs: list[tuple[int, int]] = []
    start = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


def looks_negative(grey: Image.Image, levels: Levels, value: dict[str, Any]) -> tuple[bool, float]:
    """Light writing on a dark ground: the dark class is most of the frame, and the light
    class does not survive an erosion wider than a stroke. A page on a
    dark backdrop keeps a broad light area and is not taken for a negative.
    Returns the decision and the surviving share of the light class."""
    if levels.dark_share < value["negative_dark_share"] or levels.contrast <= 0:
        return False, 1.0
    light = grey.point(lambda level: INK if level > levels.threshold else 0)
    light_count = light.histogram()[INK]
    if not light_count:
        return False, 1.0
    size = value["negative_stroke_px"] | 1
    eroded = light.filter(ImageFilter.MinFilter(size))
    survival = eroded.histogram()[INK] / light_count
    return survival < value["negative_light_survival"], survival


# --- Long straight marks --------------------------------------------------------------------


def _transpose(buffer: bytes | bytearray, width: int, height: int) -> bytes:
    image = Image.frombytes("L", (width, height), bytes(buffer))
    return image.transpose(Image.Transpose.TRANSPOSE).tobytes()


def _long_runs(buffer: bytes, width: int, height: int, min_length: int, gap: int) -> bytearray:
    """Mask (1) of ink runs along rows at least `min_length` long, allowing gaps of up
    to `gap` px inside a run."""
    pattern = re.compile(b"\xff(?:\xff|\x00{1,%d}\xff)*" % max(1, gap))
    mask = bytearray(width * height)
    for y in range(height):
        row = buffer[y * width : (y + 1) * width]
        for match in pattern.finditer(row):
            start, end = match.span()
            if end - start >= min_length:
                base = y * width
                mask[base + start : base + end] = b"\x01" * (end - start)
    return mask


def _ink_near(ink, mask, width: int, height: int, y: int, columns: range, rows: int) -> bool:
    """Whether unmasked ink lies in `columns` within `rows` rows of row `y`."""
    for row in range(max(0, y - rows), min(height, y + rows + 1)):
        base = row * width
        for x in columns:
            if ink[base + x] == INK and not (mask is not None and mask[base + x]):
                return True
    return False


def _bridge(ink: bytes, mask: bytearray, width: int, height: int, reach: int) -> None:
    """Unmask spans of a row where ink outside the mask lies just before and just after
    the span, in the same row or up to two rows away (a slanting stroke meets the two
    sides of a line at different rows): writing that crosses a line keeps its crossing
    stroke."""
    crossing = []
    for y in range(height):
        base = y * width
        row = mask[base : base + width]
        for match in _MASK_RUN.finditer(row):
            start, end = match.span()
            before = _ink_near(ink, mask, width, height, y, range(max(0, start - reach), start), 2)
            after = _ink_near(ink, mask, width, height, y, range(end, min(width, end + reach)), 2)
            if before and after:
                crossing.append((base + start, base + end))
    for start, end in crossing:
        mask[start:end] = bytes(end - start)


def mask_straight_marks(ink: Image.Image, share: float, gap: int = 1) -> Image.Image:
    """Remove horizontal and vertical straight marks (rules, page edges, a straight fold)
    at least `share` of the frame's width or height long. Writing that crosses such a
    line keeps its crossing pixels."""
    width, height = ink.size
    buffer = ink.tobytes()
    transposed = _transpose(buffer, width, height)
    # Vertical marks: long runs down columns, bridged along rows.
    vertical = bytearray(
        _transpose(
            _long_runs(transposed, height, width, max(2, round(share * height)), gap), height, width
        )
    )
    _bridge(buffer, vertical, width, height, reach=2)
    # Horizontal marks: long runs along rows, bridged down columns.
    horizontal = _long_runs(buffer, width, height, max(2, round(share * width)), gap)
    horizontal_t = bytearray(_transpose(horizontal, width, height))
    _bridge(transposed, horizontal_t, height, width, reach=2)
    horizontal = bytearray(_transpose(horizontal_t, height, width))
    size = (width, height)
    mask = ImageChops.lighter(
        Image.frombytes("L", size, bytes(vertical)), Image.frombytes("L", size, bytes(horizontal))
    ).point(lambda v: INK if v else 0)
    return ImageChops.subtract(ink, mask)


def mask_along_line(
    ink: Image.Image, top: tuple[float, float], bottom: tuple[float, float], half: int
) -> Image.Image:
    """Remove the ink of a near-vertical line through `top` and `bottom` (working px).
    In each row only the line's own run of ink is removed: the run, at most `half` px
    from the line and at most 2 * half + 1 px wide, that holds the line there. A wider
    run is writing crossing the line and is kept, and so is a narrow run with ink just
    beyond it on both sides within two rows (a slanting stroke crossing the line)."""
    width, height = ink.size
    buffer = ink.tobytes()
    out = bytearray(buffer)
    (x0, y0), (x1, y1) = top, bottom
    span_y = (y1 - y0) or 1.0
    widest = 2 * half + 1
    for y in range(height):
        x = round(x0 + (x1 - x0) * (y - y0) / span_y)
        base = y * width
        near = [
            c for c in range(max(0, x - half), min(width, x + half + 1)) if buffer[base + c] == INK
        ]
        if not near:
            continue
        seed = min(near, key=lambda c: abs(c - x))
        a = seed
        while a > 0 and buffer[base + a - 1] == INK:
            a -= 1
        b = seed + 1
        while b < width and buffer[base + b] == INK:
            b += 1
        if b - a > widest:
            continue
        before = _ink_near(buffer, None, width, height, y, range(max(0, a - 2), a), 2)
        after = _ink_near(buffer, None, width, height, y, range(b, min(width, b + 2)), 2)
        if before and after:
            continue
        out[base + a : base + b] = bytes(b - a)
    return Image.frombytes("L", (width, height), bytes(out))


# --- Connected components --------------------------------------------------------------------


@dataclass
class Mark:
    """One connected ink component: its box (right and bottom exclusive), pixel count
    and its runs as (row, start, end)."""

    x0: int
    y0: int
    x1: int
    y1: int
    count: int
    runs: list[tuple[int, int, int]]

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def centre_x(self) -> float:
        return (self.x0 + self.x1) / 2


def marks_of(ink: Image.Image) -> list[Mark]:
    """8-connected components of the ink map, in a fixed order (top, then left)."""
    width, height = ink.size
    buffer = ink.tobytes()
    parent: list[int] = []
    runs: list[tuple[int, int, int]] = []

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    previous: list[tuple[int, int, int]] = []
    for y in range(height):
        current: list[tuple[int, int, int]] = []
        row = buffer[y * width : (y + 1) * width]
        first = 0
        for match in _INK_RUN.finditer(row):
            start, end = match.span()
            index = len(runs)
            runs.append((y, start, end))
            parent.append(index)
            while first < len(previous) and previous[first][1] < start:
                first += 1
            other = first
            while other < len(previous) and previous[other][0] <= end:
                a, b = find(index), find(previous[other][2])
                if a != b:
                    parent[max(a, b)] = min(a, b)
                other += 1
            current.append((start, end, index))
        previous = current
    groups: dict[int, Mark] = {}
    for index, (y, start, end) in enumerate(runs):
        root = find(index)
        mark = groups.get(root)
        if mark is None:
            groups[root] = Mark(start, y, end, y + 1, end - start, [(y, start, end)])
        else:
            mark.x0 = min(mark.x0, start)
            mark.x1 = max(mark.x1, end)
            mark.y1 = y + 1
            mark.count += end - start
            mark.runs.append((y, start, end))
    return [groups[root] for root in sorted(groups)]


def is_long_thin(mark: Mark, frame: tuple[int, int], value: dict[str, Any]) -> bool:
    """A long mark of line-like thickness: a rule or edge that is not axis-aligned."""
    width, height = frame
    long_side = max(mark.width, mark.height)
    along = height if mark.height >= mark.width else width
    if long_side < value["long_mark_share"] * along:
        return False
    return mark.count / long_side <= value["long_mark_max_thickness_px"]


def paint(marks: list[Mark], size: tuple[int, int]) -> Image.Image:
    """An ink map holding only the given marks."""
    width, height = size
    out = bytearray(width * height)
    for mark in marks:
        for y, start, end in mark.runs:
            out[y * width + start : y * width + end] = b"\xff" * (end - start)
    return Image.frombytes("L", size, bytes(out))
