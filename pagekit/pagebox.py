"""Page box: where the paper is inside the frame.

`detect_page_box(image, dpi)` returns the detector answer shape. The value is
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

A stack of thin lines running along a side, close together and reaching the found
edge (the edges of the pages under the top sheet, a board edge), puts the side at the
innermost line, the top sheet's edge, and the strip beyond it is not searched for ink.

A shadow along one side that does not fill the whole side is measured as the depth of
backdrop-like pixels on each scanline from that edge. A shadow of even depth covering
most of the side is cut off the box; a shorter one is flagged and left in.

The walk passes objects lying on the backdrop (a target, a ruler, a label) when
backdrop resumes beyond them and no paper comes first. A side that is not walked but
whose frame edge is much paler than the paper is flagged, not called free of backdrop.

Each strip a walked side cuts off is then searched for strokes that stand out from the
strip's own texture, are not lines running along the side (page edges, a board
edge), are the size of a mark, are joined to the paper edge (directly or through a chain of such
strokes) and are not part of a target or ruler. Where there are any (writing under a
gutter shadow, say), the side is moved back out to keep them and the page is flagged.

Page-frame detection after F. Shafait, J. van Beusekom, D. Keysers and T. M. Breuel,
"Document cleanup using page frame detection", International Journal on Document
Analysis and Recognition, 2008, and K.-C. Fan, Y.-K. Wang and T.-R. Lay,
"Marginal noise removal of document images", Pattern Recognition, 2002; the scanline
walk itself is general knowledge.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
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
    "backdrop_object_mm",
    "strip_noise_k",
    "strip_reach_mm",
    "edge_mark_mm2",
    "edge_line_min_mm",
    "edge_line_max_mm",
    "stack_window_mm",
    "stack_reference_mm",
    "stack_line_contrast",
    "stack_line_max_mm",
    "stack_gap_mm",
    "stack_min_lines",
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
    backdrop, paper, dark, kinds = backdrop_map(work, v)
    width, height = backdrop.size
    run = work.px(v["edge_run_mm"])
    # Outside the page's own area everything is dark backdrop.
    grey = Image.composite(work.grey, Image.new("L", work.grey.size, 0), work.mask)
    dark = max(dark, 0)

    box = (0, 0, width, height)
    paler = [False] * 4
    for attempt in range(8):
        found, side_paler = _walk(grey, box, dark, paper, v, work)
        if attempt == 0:
            paler = side_paler
        if found == box:
            break
        box = found
    flags: list[str] = list(page.flags)
    if box[2] - box[0] <= run or box[3] - box[1] <= run:
        box = (0, 0, width, height)
        flags.append("No paper found: the frame looks like backdrop throughout.")

    walked = [box[0] > 0, box[1] > 0, box[2] < width, box[3] < height]
    unsure = [paler[i] and not walked[i] for i in range(4)]
    for index, side in enumerate(SIDES):
        if unsure[index]:
            flags.append(
                f"The frame edge on the {side} side is much paler than the paper, but no "
                "step to the paper was found; it may be a pale backdrop left in the box."
            )
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
    value, stacks = _edge_stacks(page, value, v)
    for index in range(4):
        walked[index] = walked[index] or stacks[index] is not None
    value, kept_ink = _keep_ink_in_strips(page, value, walked, v, stacks)
    for side, ink_box in kept_ink:
        flags.append(
            f"Ink lies in the strip the walk would cut on the {side} side (at {ink_box}): "
            "perhaps writing under a shadow. The edge is moved out to keep it."
        )
    parts = []
    for index, side in enumerate(SIDES):
        if stacks[index] is not None:
            parts.append(
                f"{side} at the innermost of {stacks[index][2]} thin lines running along it "
                "(a stack of page edges or a board edge): the top sheet's edge"
            )
        elif any(side == kept for kept, _ in kept_ink):
            parts.append(f"{side} moved out to keep ink found in the cut strip")
        elif shadows[index] is not None:
            parts.append(f"{side} past a shadow {shadows[index]:.0f} mm deep")
        elif walked[index]:
            depth = (box[index] if index < 2 else (width, height)[index - 2] - box[index]) * work.mm
            parts.append(f"{side} {depth:.0f} mm in")
        elif unsure[index]:
            parts.append(f"{side} kept at the frame edge, which is much paler than the paper")
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


def backdrop_map(work: common.Work, v: dict[str, Any]) -> tuple[Image.Image, int, int, str]:
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
    return (
        marks,
        paper,
        dark_level,
        f"backdrop-like pixels ({described}) against paper at grey {paper}",
    )


def _line_stats(lines: list[bytes], dark: int, paper: int, v: dict[str, Any]):
    """Per line, from the side inward: (dark share, pale share, paper-like share,
    median); and whether the outer lines make a pale backdrop, and their level."""
    ordered = [sorted(line) for line in lines]
    length = len(lines[0]) if lines else 1
    medians = [line[length // 2] for line in ordered]
    plateau = sorted(medians[:3])[len(medians[:3]) // 2] if medians else 0
    delta = v["pale_backdrop_delta"]
    pale = math.ceil(plateau - delta / 2) if plateau >= paper + delta else 256
    stats = []
    for line, median in zip(ordered, medians, strict=True):
        dark_share = bisect_right(line, dark) / length
        pale_share = (length - bisect_left(line, pale)) / length if pale <= 255 else 0.0
        stats.append((dark_share, pale_share, 1.0 - dark_share - pale_share, median))
    return stats, plateau, pale <= 255


def _side_edge(
    stats: list, pale_side: bool, share: float, run: int, objects: int, step: int, delta: int
):
    """Lines of backdrop before the paper on one side, or None when every line is
    backdrop.

    A line is backdrop when at least `share` of it is dark backdrop or pale backdrop,
    paper when most of it looks like neither, and otherwise part of something lying on
    the backdrop (a target, a ruler, a label). Such an object is walked past when
    backdrop resumes beyond it within `objects` lines and no paper line comes first,
    so a minority of odd lines does not stop the walk (second review, R3). The edge is
    the first line after which non-backdrop persists for `run` lines.

    Leaving a pale backdrop, the level must fall by `delta` within `step` lines of the
    last backdrop line: paper that brightens slowly toward one side drifts rather than
    steps, and is never cut."""
    count = len(stats)
    kinds = []
    for dark_share, pale_share, paperlike, _ in stats:
        if dark_share >= share or pale_share >= share:
            kinds.append("B")
        elif paperlike >= 0.6:
            kinds.append("P")
        else:
            kinds.append("O")
    i = 0
    while i < count:
        if kinds[i] == "B":
            i += 1
            continue
        ahead = kinds[i : i + objects]
        if "B" in ahead and "P" not in ahead[: ahead.index("B")]:
            i += ahead.index("B")  # an object on the backdrop, or a speck of dust
            continue
        if "B" in kinds[i : i + run]:
            i += kinds[i : i + run].index("B")
            continue
        if i and pale_side and stats[i - 1][1] >= share:
            later = stats[min(count - 1, i + step)][3]
            if stats[i - 1][3] - later < delta:
                return 0  # a drift, not a step
        return i
    return None


def _walk(grey: Image.Image, box, dark: int, paper: int, v: dict[str, Any], work: common.Work):
    """One pass of the walk in from each side of `box`; also, per side, whether its
    outer lines are much paler than the paper."""
    x0, y0, x1, y1 = box
    region = grey.crop(box)
    width, height = region.size
    share = v["backdrop_line_share"]
    run = work.px(v["edge_run_mm"])
    objects = work.px(v["backdrop_object_mm"])
    step = work.px(v["pale_step_mm"])
    delta = v["pale_backdrop_delta"]
    columns = region.transpose(Image.Transpose.TRANSPOSE).tobytes()
    rows = region.tobytes()
    by_column = [columns[x * height : (x + 1) * height] for x in range(width)]
    by_row = [rows[y * width : (y + 1) * width] for y in range(height)]
    edges = []
    paler = []
    for lines in (by_column, by_row, by_column[::-1], by_row[::-1]):
        stats, plateau, pale_side = _line_stats(lines, dark, paper, v)
        edge = _side_edge(stats, pale_side, share, run, objects, step, delta)
        edges.append(edge or 0)
        paler.append(plateau >= paper + delta)
    left, top, right, bottom = edges
    found = (x0 + left, y0 + top, x1 - right, y1 - bottom)
    if found[2] - found[0] < 1 or found[3] - found[1] < 1:
        return box, paler
    return found, paler


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
    page: common.Page,
    value: list[int],
    walked: list[bool],
    v: dict[str, Any],
    stacks: list | None = None,
) -> tuple[list[int], list[tuple[str, list[int]]]]:
    """Move a walked side back out where the strip it cuts off holds ink.

    Each strip between the frame and a walked side is looked at on a sharper copy for
    dark strokes against their own surroundings (a black top-hat: a small closing minus
    the image, so a slow shadow or a uniform backdrop adds nothing, and the edge of a
    pale label on the backdrop adds no halo). Components above speck size that are not
    long straight lines (the rim of the paper) are ink. Writing under a gutter shadow
    is found this way."""
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
        if not walked[index] or (stacks and stacks[index] is not None):
            continue  # beyond a stack of page edges lies only book, not writing
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
        # The bar is set by the strip's own texture: backdrop cloth or grain is not ink.
        histogram = darker.histogram(work.mask.crop(area))
        middle = common.median_level(histogram) or 0
        spread_hist = [0] * 256
        for level, number in enumerate(histogram):
            spread_hist[abs(level - middle)] += number
        spread = 1.4826 * (common.median_level(spread_hist) or 0)
        bar = max(v["blank_contrast"], middle + v["strip_noise_k"] * spread)
        marks = darker.point(lambda d, bar=bar: 255 if d >= bar else 0)
        marks = ImageChops.multiply(marks, work.mask.crop(area))
        if common.busy(marks, work, v) is not None:
            continue
        parts = [
            p
            for p in common.components(marks)
            if p.area >= speck and not common.rule_shaped(p, rule_length, thickest, 10.0)
        ]
        targets = _targets_in(grey, work, v)
        parts = [p for p in parts if not any(_centre_inside(p, t) for t in targets)]
        # Long thin lines running along the side are page edges or a board edge, not
        # writing (real-register follow-up, S3).
        lines = common.edge_line_pieces(parts, index % 2 == 0, work, v)
        parts = [p for p in parts if id(p) not in lines]
        # Only marks the size of a mark move an edge: a speck-sized dash on a book edge
        # is not worth a review.
        parts = [p for p in parts if p.area * work.mm**2 >= v["edge_mark_mm2"]]
        parts = _joined_to_paper(parts, side, grey.size, work.px(v["strip_reach_mm"]))
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


def _centre_inside(part: common.Component, box) -> bool:
    cx, cy = (part.x0 + part.x1) / 2, (part.y0 + part.y1) / 2
    return box[0] - 2 <= cx < box[2] + 2 and box[1] - 2 <= cy < box[3] + 2


def _targets_in(grey: Image.Image, work: common.Work, v: dict[str, Any]) -> list:
    """Boxes of target grids and rulers lying in a strip of backdrop: objects that
    differ from the strip's backdrop by the blank contrast, either way, found by the
    content box's own target finders."""
    from pagekit.content import find_grids, find_rulers

    level = common.median_level(grey.histogram()) or 0
    contrast = v["blank_contrast"]
    boxes = []
    # Objects as a whole, and split into those a little and those far from the
    # backdrop's level, so dark ticks printed on a mid-grey bar show as notches.
    for low, high in ((contrast, 256), (contrast, 3 * contrast), (3 * contrast, 256)):
        objects = grey.point(
            lambda g, low=low, high=high: 255 if low <= abs(g - level) < high else 0
        )
        parts = common.components(objects)
        boxes += [box for _, box, _, _ in find_grids(parts, None, work, v)]
        boxes += [part.box for part in find_rulers(parts, work, v)]
    return boxes


def _joined_to_paper(parts: list, side: str, size: tuple[int, int], reach: int) -> list:
    """The components within `reach` of the paper edge of the strip, directly or through
    a chain of such components: writing joined to the paper, not marks on the backdrop
    (second review, R2)."""
    width, height = size

    def to_paper(part: common.Component) -> int:
        if side == "left":
            return width - part.x1
        if side == "right":
            return part.x0
        if side == "top":
            return height - part.y1
        return part.y0

    chosen = [p for p in parts if to_paper(p) <= reach]
    rest = [p for p in parts if to_paper(p) > reach]
    grew = True
    while grew and rest:
        grew = False
        for part in list(rest):
            near = any(
                max(part.x0 - c.x1, c.x0 - part.x1, part.y0 - c.y1, c.y0 - part.y1) <= reach
                for c in chosen
            )
            if near:
                chosen.append(part)
                rest.remove(part)
                grew = True
    return chosen


def _stack_lines(levels: list[float], px_per_mm: float, v: dict[str, Any]) -> list[tuple[int, int]]:
    """Thin lines in a profile of medians: runs at least `stack_line_contrast` lighter
    or darker than a running median over `stack_reference_mm`, no wider than
    `stack_line_max_mm`. Returns (start, end) index pairs, end exclusive."""
    half = max(1, round(v["stack_reference_mm"] * px_per_mm / 2))
    widest = max(1, round(v["stack_line_max_mm"] * px_per_mm))
    contrast = v["stack_line_contrast"]
    signs = []
    for i in range(len(levels)):
        window = sorted(levels[max(0, i - half) : i + half + 1])
        d = levels[i] - window[len(window) // 2]
        signs.append(1 if d >= contrast else -1 if d <= -contrast else 0)
    lines = []
    i = 0
    while i < len(signs):
        if signs[i] == 0:
            i += 1
            continue
        j = i
        while j < len(signs) and signs[j] == signs[i]:
            j += 1
        if j - i <= widest:
            lines.append((i, j))
        i = j
    return lines


def _edge_stacks(page: common.Page, value: list[int], v: dict[str, Any]):
    """Find, along each side, a stack of thin lines running most of its length: the
    edges of the pages under the top sheet, or a book's board edge. Such lines are not
    writing; the side belongs at the innermost one, the top sheet's edge.

    For each side, the median grey level of every line parallel to it, over the box's
    extent along it, is taken in a window either side of the found edge; a median means
    a line must run most of the side's length to show. Thin lines in that profile, at
    least `stack_min_lines` of them no more than `stack_gap_mm` apart and reaching the
    found edge, make a stack (real-register follow-up, S3).

    Returns the moved box and, per side, None or (low, high, count): the stack's extent
    across the side in the page's grid, and its number of lines."""
    grey = page.grey
    width, height = grey.size
    result = list(value)
    found: list = [None, None, None, None]
    for index in range(4):
        horizontal = index % 2 == 0  # left and right: lines run down the page
        dpi = page.dpi[0] if horizontal else page.dpi[1]
        px_per_mm = dpi / 25.4
        window = round(v["stack_window_mm"] * px_per_mm)
        edge = value[index]
        limit = width if horizontal else height
        outward = -1 if index < 2 else 1
        inner = edge - outward * window
        outer = edge + outward * window
        low, high = sorted((max(0, min(limit, inner)), max(0, min(limit, outer))))
        if high - low < 8:
            continue
        if horizontal:
            band = grey.crop((low, value[1], high, value[3]))
            band = band.resize((band.width, min(band.height, 400)), Image.BOX)
            band = band.transpose(Image.Transpose.TRANSPOSE)
        else:
            band = grey.crop((value[0], low, value[2], high))
            band = band.resize((min(band.width, 400), band.height), Image.BOX)
        length = band.width
        data = band.tobytes()
        medians = [
            sorted(data[i * length : (i + 1) * length])[length // 2] for i in range(band.height)
        ]
        if outward < 0:
            medians = medians[::-1]  # index grows outward

        def to_page(i: int, outward: int = outward, low: int = low, high: int = high) -> int:
            return high - 1 - i if outward < 0 else low + i

        lines = _stack_lines(medians, px_per_mm, v)
        if len(lines) < v["stack_min_lines"]:
            continue
        gap = v["stack_gap_mm"] * px_per_mm
        chains = [[lines[0]]]
        for line in lines[1:]:
            if line[0] - chains[-1][-1][1] <= gap:
                chains[-1].append(line)
            else:
                chains.append([line])
        edge_at = abs(edge - (high - 1 if outward < 0 else low))  # the edge, as an index
        chains = [c for c in chains if len(c) >= v["stack_min_lines"] and c[-1][1] >= edge_at - gap]
        if not chains:
            continue
        chain = chains[0]
        first, last = to_page(chain[0][0]), to_page(chain[-1][1] - 1)
        top_sheet = first + (1 if outward < 0 else 0)  # the innermost line is excluded
        if outward < 0:
            result[index] = max(result[index], top_sheet)
        else:
            result[index] = min(result[index], top_sheet)
        found[index] = (min(first, last), max(first, last) + 1, len(chain))
    if result[2] <= result[0] or result[3] <= result[1]:
        return list(value), [None, None, None, None]
    return result, found
