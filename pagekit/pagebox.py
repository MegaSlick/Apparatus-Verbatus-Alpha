"""Page box: where the paper is inside the frame (spec 0004, finding 0015).

`detect_page_box(image, dpi)` returns spec 0002's answer shape. The value is
[left, top, right, bottom] of the paper in the page's own pixel grid, right and bottom
not included. It works on a reduced working copy and writes no file.

The frame is split into backdrop-like and paper-like pixels by two thresholds, since
backdrops vary: darker than Otsu's threshold of the whole frame (a dark backdrop), or
paler than the paper by a set amount (a pale backdrop, found by its change in
brightness from the paper rather than by darkness). From each side the walk moves
inward while the scanline is almost entirely backdrop; the paper edge is where that run
of backdrop lines ends and stays ended for a set distance, so dust, tears and labels at
the edge do not stop it. The walk repeats on the box it found until nothing moves, so a
page much smaller than the frame is found too. A side with no backdrop keeps the frame
edge, and the evidence says so; that alone is not a flag. Outside the page's own area
(the polygon from the split) everything counts as backdrop.

A shadow along one side that does not fill the whole side is measured as the depth of
backdrop-like pixels on each scanline from that edge. A shadow of even depth covering
most of the side is cut off the box; a shorter one is flagged and left in.

Page-frame detection after F. Shafait, J. van Beusekom, D. Keysers and T. M. Breuel,
"Document cleanup using page frame detection", International Journal on Document
Analysis and Recognition, 2008, and K.-C. Fan, Y.-K. Wang and T.-R. Lay,
"Marginal noise removal of document images", Pattern Recognition, 2002; the scanline
walk itself is general knowledge (finding 0015).
"""

from __future__ import annotations

import math
import re
from typing import Any

from PIL import Image, ImageChops

from pagekit import _box_common as common
from pagekit.check import otsu_threshold

_READS = (
    "pagebox_working_dpi",
    "backdrop_line_share",
    "backdrop_min_contrast",
    "pale_backdrop_delta",
    "edge_run_mm",
    "shadow_min_depth_mm",
    "shadow_min_share",
    "shadow_exclude_share",
)
SIDES = ("left", "top", "right", "bottom")
_LEADING = re.compile(rb"\xff+")


def detect_page_box(
    image: Image.Image,
    dpi: tuple[float, float],
    polygon: list[tuple[float, float]] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict:
    """The page-box answer for one upright page: value, confidence, evidence, flags."""
    thresholds = common.load_thresholds(overrides)
    v = common.values(thresholds)
    note = common.unmeasured_note(thresholds, _READS)
    page = common.page_input(image, dpi, polygon)
    work = common.working_copy(page, v["pagebox_working_dpi"])
    backdrop, kinds = backdrop_map(work, v)
    width, height = backdrop.size
    run = work.px(v["edge_run_mm"])
    share = v["backdrop_line_share"]

    box = (0, 0, width, height)
    for _ in range(8):
        found = _walk(backdrop, box, share, run)
        if found == box:
            break
        box = found
    flags: list[str] = []
    if box[2] - box[0] <= run or box[3] - box[1] <= run:
        box = (0, 0, width, height)
        flags.append("No paper found: the frame looks like backdrop throughout.")

    walked = [box[0] > 0, box[1] > 0, box[2] < width, box[3] < height]
    box, shadows, shadow_flags = _shadows(backdrop, box, work, v)
    flags += shadow_flags
    for index, cut in enumerate(shadows):
        walked[index] = walked[index] or cut is not None

    value = _to_source(work, box, walked, page.grey.size)
    if page.polygon is not None:
        # Nothing outside the page's own area counts, so the box never passes its bounds.
        xs = [x for x, _ in page.polygon]
        ys = [y for _, y in page.polygon]
        bounds = (math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys)))
        clamped = [
            max(value[0], bounds[0]),
            max(value[1], bounds[1]),
            min(value[2], bounds[2]),
            min(value[3], bounds[3]),
        ]
        if clamped[0] < clamped[2] and clamped[1] < clamped[3]:
            value = clamped
    parts = []
    for index, side in enumerate(SIDES):
        if shadows[index] is not None:
            parts.append(f"{side} past a shadow {shadows[index]:.0f} mm deep")
        elif walked[index]:
            depth = (box[index] if index < 2 else (width, height)[index - 2] - box[index]) * work.mm
            parts.append(f"{side} {depth:.0f} mm in")
        else:
            parts.append(f"{side} kept at the frame edge (no backdrop there)")
    evidence = (
        f"Paper at {value} found by walking in from each side over {kinds}: "
        + "; ".join(parts)
        + "."
        + note
    )
    confidence = 0.9 if any(walked) else 0.8
    if flags:
        confidence = 0.4
    return common.answer(value, confidence, evidence, flags)


def backdrop_map(work: common.Work, v: dict[str, Any]) -> tuple[Image.Image, str]:
    """255 where a pixel looks like backdrop (much darker or paler than the paper)."""
    grey = work.grey
    width, height = grey.size
    centre = (width // 4, height // 4, width - width // 4, height - height // 4)
    histogram = grey.crop(centre).histogram(work.mask.crop(centre))
    if sum(histogram) == 0:
        histogram = grey.histogram(work.mask)
    split = otsu_threshold(histogram)
    dark, light = common.class_means(histogram, split)
    if dark is not None and light is not None and light - dark >= v["backdrop_min_contrast"]:
        paper = common.median_level(histogram, range(split + 1, 256))
    else:
        paper = common.median_level(histogram)
    paper = 255 if paper is None else paper

    frame = grey.histogram()
    dark_level = otsu_threshold(frame)
    dark_mean, _ = common.class_means(frame, dark_level)
    kinds = []
    if dark_mean is None or paper - dark_mean < v["backdrop_min_contrast"]:
        dark_level = -1
    else:
        kinds.append(f"grey {dark_level} or darker")
    pale_level = paper + v["pale_backdrop_delta"]
    if pale_level <= 255:
        kinds.append(f"grey {pale_level} or paler")
    marks = grey.point(lambda level: 255 if level <= dark_level or level >= pale_level else 0)
    outside = ImageChops.invert(work.mask)
    if outside.getbbox() is not None:
        kinds.append("anything outside the page's own area")
    marks = ImageChops.lighter(marks, outside)
    described = ", ".join(kinds) if kinds else "nothing distinct from the paper"
    return marks, f"backdrop-like pixels ({described}) against paper at grey {paper}"


def _edge(shares: list[float], share: float, run: int) -> int | None:
    """Index where the run of backdrop lines ends and stays ended for `run` lines."""
    paper = [s < share for s in shares]
    streak = 0
    for index in range(len(paper) - 1, -1, -1):
        streak = streak + 1 if paper[index] else 0
        paper[index] = streak  # number of paper lines from here inward
    for index, streak in enumerate(paper):
        if streak >= min(run, len(paper) - index):
            return index
    return None


def _walk(backdrop: Image.Image, box: tuple[int, int, int, int], share: float, run: int):
    x0, y0, x1, y1 = box
    region = backdrop.crop(box)
    columns = common.profile(region, along_x=True)
    rows = common.profile(region, along_x=False)
    left = _edge(columns, share, run)
    right = _edge(columns[::-1], share, run)
    top = _edge(rows, share, run)
    bottom = _edge(rows[::-1], share, run)
    found = (
        x0 + (left or 0),
        y0 + (top or 0),
        x1 - (right or 0),
        y1 - (bottom or 0),
    )
    if found[2] - found[0] < 1 or found[3] - found[1] < 1:
        return box
    return found


def _depths(region: Image.Image) -> list[int]:
    """Backdrop-like pixels from the left end of each row, until the first paper pixel."""
    width, height = region.size
    data = region.tobytes()
    depths = []
    for y in range(height):
        match = _LEADING.match(data, y * width, (y + 1) * width)
        depths.append(0 if match is None else match.end() - match.start())
    return depths


def _shadows(backdrop: Image.Image, box, work: common.Work, v: dict[str, Any]):
    """Cut off, or flag, shadows along a side that do not fill the whole side."""
    flags: list[str] = []
    cuts: list[float | None] = [None, None, None, None]
    least = work.px(v["shadow_min_depth_mm"])
    x0, y0, x1, y1 = box
    box = list(box)
    region = backdrop.crop((x0, y0, x1, y1))
    views = {
        "left": region,
        "top": region.transpose(Image.Transpose.TRANSPOSE),
        "right": region.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
        "bottom": region.transpose(Image.Transpose.TRANSVERSE),
    }
    for index, side in enumerate(SIDES):
        depths = _depths(views[side])
        deep = sorted(d for d in depths if d >= least)
        if not depths or len(deep) / len(depths) < v["shadow_min_share"]:
            continue
        shallow, deepest = deep[len(deep) // 10], deep[(len(deep) * 9) // 10]
        if shallow * 2 < deepest:
            continue  # depths spread from shallow to deep: a slanted or torn edge, not a shadow
        covered = len(deep) / len(depths)
        across = views[side].width
        if covered >= v["shadow_exclude_share"] and deepest < across // 2:
            cuts[index] = deepest * work.mm
            box[index] = box[index] + deepest if index < 2 else box[index] - deepest
        else:
            flags.append(
                f"A shadow along the {side} side covers {covered:.0%} of it, about "
                f"{deepest * work.mm:.0f} mm deep; it is counted as page."
            )
    return tuple(box), cuts, flags


def _to_source(work: common.Work, box, walked: list[bool], size: tuple[int, int]) -> list[int]:
    """Working box to source grid: walked sides rounded inward, kept sides exact."""
    width, height = size
    x0, y0, x1, y1 = box
    result = [
        min(width, math.ceil(x0 / work.sx - 1e-9)) if walked[0] else 0,
        min(height, math.ceil(y0 / work.sy - 1e-9)) if walked[1] else 0,
        max(0, math.floor(x1 / work.sx + 1e-9)) if walked[2] else width,
        max(0, math.floor(y1 / work.sy + 1e-9)) if walked[3] else height,
    ]
    if result[2] <= result[0] or result[3] <= result[1]:
        return work.to_source(box, size)
    return result
