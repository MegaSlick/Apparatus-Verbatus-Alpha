"""Page box: where the paper is inside the frame (spec 0004, finding 0015).

`detect_page_box(image, dpi)` returns spec 0002's answer shape. The value is
[left, top, right, bottom] of the paper in the page's own pixel grid, right and bottom
not included. It works on a reduced working copy and writes no file.

Backdrops vary, so two tests find them. A dark backdrop is darker than Otsu's threshold
of the whole frame. A pale backdrop is found by its change in brightness from the
paper: a uniform margin, paler than the paper by a set amount, that steps down to the
paper within a few millimetres; paper that brightens slowly toward one side drifts
rather than steps, and is never cut. From each side the walk moves
inward while the scanline is almost entirely backdrop; the paper edge is where that run
of backdrop lines ends and stays ended for a set distance, so dust, tears and labels at
the edge do not stop it. The walk repeats on the box it found until nothing moves, so a
page much smaller than the frame is found too. A side with no backdrop keeps the frame
edge, and the evidence says so; that alone is not a flag. Outside the page's own area
(the polygon from the split) everything counts as backdrop.

A shadow along one side that does not fill the whole side is measured as the depth of
backdrop-like pixels on each scanline from that edge. A shadow of even depth covering
most of the side is cut off the box; a shorter one is flagged and left in.

Each strip a walked side cuts off is then searched for ink above speck size against its
own surroundings; where there is any (writing under a gutter shadow, say), the side is
moved back out to keep it and the page is flagged.

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

from PIL import Image, ImageChops, ImageFilter

from pagekit import _box_common as common
from pagekit.check import otsu_threshold

_READS = (
    "plausible_page_min_mm",
    "plausible_page_max_mm",
    "fallback_dpi",
    "busy_runs_per_cm2",
    "busy_max_runs",
    "pale_step_mm",
    "content_working_dpi",
    "speck_mm",
    "blank_contrast",
    "rule_min_length_mm",
    "rule_max_thickness_mm",
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
    page = common.page_input(image, dpi, polygon, v)
    work = common.working_copy(page, v["pagebox_working_dpi"])
    backdrop, paper, kinds = backdrop_map(work, v)
    width, height = backdrop.size
    run = work.px(v["edge_run_mm"])
    share = v["backdrop_line_share"]
    pale = (paper, v["pale_backdrop_delta"], work.px(v["pale_step_mm"]), run)

    box = (0, 0, width, height)
    for _ in range(8):
        found = _walk(backdrop, work.grey, box, share, run, pale)
        if found == box:
            break
        box = found
    flags: list[str] = list(page.flags)
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
    value, kept_ink = _keep_ink_in_strips(page, value, walked, v)
    for side, ink_box in kept_ink:
        flags.append(
            f"Ink lies in the strip the walk would cut on the {side} side (at {ink_box}): "
            "perhaps writing under a shadow. The edge is moved out to keep it."
        )
    parts = []
    for index, side in enumerate(SIDES):
        if any(side == kept for kept, _ in kept_ink):
            parts.append(f"{side} moved out to keep ink found in the cut strip")
        elif shadows[index] is not None:
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


def backdrop_map(work: common.Work, v: dict[str, Any]) -> tuple[Image.Image, int, str]:
    """255 where a pixel looks like dark backdrop, the paper's grey level, and a
    description. A pale backdrop is found by its step down to the paper, in `_walk`."""
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
    if paper + v["pale_backdrop_delta"] <= 255:
        kinds.append(
            f"a uniform margin of grey {paper + v['pale_backdrop_delta']} or paler that "
            "steps down to the paper"
        )
    marks = grey.point(lambda level: 255 if level <= dark_level else 0)
    outside = ImageChops.invert(work.mask)
    if outside.getbbox() is not None:
        kinds.append("anything outside the page's own area")
    marks = ImageChops.lighter(marks, outside)
    described = ", ".join(kinds) if kinds else "nothing distinct from the paper"
    return marks, paper, f"backdrop-like pixels ({described}) against paper at grey {paper}"


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


def _quartiles(region: Image.Image) -> tuple[list[int], list[int]]:
    """The darker-quartile grey level of each column and of each row."""
    width, height = region.size
    rows = region.tobytes()
    columns = region.transpose(Image.Transpose.TRANSPOSE).tobytes()
    by_column = [sorted(columns[x * height : (x + 1) * height])[height // 4] for x in range(width)]
    by_row = [sorted(rows[y * width : (y + 1) * width])[width // 4] for y in range(height)]
    return by_column, by_row


def _pale_step(levels: list[int], paper: int, delta: int, step: int, run: int) -> int:
    """Lines of pale backdrop before the paper, or 0.

    A pale backdrop is a uniform margin at least `delta` paler than the paper that
    steps down to the paper within `step` lines and stays down for `run` lines. Paper
    that brightens slowly toward one side drifts instead of stepping, so it is never
    cut (brief 0022, B2)."""
    if len(levels) < 4:
        return 0
    plateau = sorted(levels[:3])[1]
    if plateau < paper + delta:
        return 0
    for j in range(1, len(levels)):
        if levels[j] > plateau - delta / 2:
            continue
        if any(level < plateau - delta / 3 for level in levels[: max(1, j - step + 1)]):
            return 0  # the margin drifted down before the step: a gradient
        after = levels[j : j + run]
        if all(level <= plateau - delta for level in after[1:]) and after:
            return j
        return 0
    return 0


def _walk(
    backdrop: Image.Image,
    grey: Image.Image,
    box: tuple[int, int, int, int],
    share: float,
    run: int,
    pale: tuple[int, int, int, int],
):
    x0, y0, x1, y1 = box
    region = backdrop.crop(box)
    columns = common.profile(region, along_x=True)
    rows = common.profile(region, along_x=False)
    left = _edge(columns, share, run) or 0
    right = _edge(columns[::-1], share, run) or 0
    top = _edge(rows, share, run) or 0
    bottom = _edge(rows[::-1], share, run) or 0
    by_column, by_row = _quartiles(grey.crop(box))
    left = max(left, _pale_step(by_column, *pale))
    right = max(right, _pale_step(by_column[::-1], *pale))
    top = max(top, _pale_step(by_row, *pale))
    bottom = max(bottom, _pale_step(by_row[::-1], *pale))
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


def _keep_ink_in_strips(
    page: common.Page, value: list[int], walked: list[bool], v: dict[str, Any]
) -> tuple[list[int], list[tuple[str, list[int]]]]:
    """Move a walked side back out where the strip it cuts off holds ink.

    Each strip between the frame and a walked side is looked at on a sharper copy for
    dark strokes against their own surroundings (a black top-hat: a small closing minus
    the image, so a slow shadow or a uniform backdrop adds nothing, and the edge of a
    pale label on the backdrop adds no halo). Components above speck size that are not
    long straight lines (the rim of the paper) are ink. Writing under a gutter shadow
    is found this way (brief 0022, B2)."""
    work = common.working_copy(page, v["content_working_dpi"])
    width, height = page.grey.size
    margin = work.px(1.0)
    speck = (v["speck_mm"] / work.mm) ** 2
    rule_length = work.px(v["rule_min_length_mm"])
    thickest = v["rule_max_thickness_mm"] / work.mm
    size = max(3, 2 * work.px(1.0) + 1)
    x0, y0, x1, y1 = value
    strips = {
        "left": (0, y0, x0, y1),
        "top": (x0, 0, x1, y0),
        "right": (x1, y0, width, y1),
        "bottom": (x0, y1, x1, height),
    }
    result = list(value)
    kept = []
    for index, side in enumerate(SIDES):
        if not walked[index]:
            continue
        sx0, sy0, sx1, sy1 = work.from_source(strips[side])
        # Leave out the last millimetre before the paper edge, where the edge itself is.
        if side == "left":
            sx1 -= margin
        elif side == "right":
            sx0 += margin
        elif side == "top":
            sy1 -= margin
        else:
            sy0 += margin
        if sx1 - sx0 < 3 or sy1 - sy0 < 3:
            continue
        area = (sx0, sy0, sx1, sy1)
        grey = work.grey.crop(area)
        closed = grey.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
        darker = ImageChops.subtract(closed, grey)
        marks = darker.point(lambda d: 255 if d >= v["blank_contrast"] else 0)
        marks = ImageChops.multiply(marks, work.mask.crop(area))
        if common.busy(marks, work, v) is not None:
            continue
        parts = [
            p
            for p in common.components(marks)
            if p.area >= speck and not common.rule_shaped(p, rule_length, thickest, 10.0)
        ]
        found = common.union_box([p.box for p in parts])
        if found is None:
            continue
        ink = work.to_source(
            (found[0] + sx0, found[1] + sy0, found[2] + sx0, found[3] + sy0), (width, height)
        )
        pad = math.ceil(1.0 / 25.4 * page.dpi[0 if index % 2 == 0 else 1])
        if side == "left":
            result[0] = max(0, min(result[0], ink[0] - pad))
        elif side == "top":
            result[1] = max(0, min(result[1], ink[1] - pad))
        elif side == "right":
            result[2] = min(width, max(result[2], ink[2] + pad))
        else:
            result[3] = min(height, max(result[3], ink[3] + pad))
        kept.append((side, ink))
    return result, kept
