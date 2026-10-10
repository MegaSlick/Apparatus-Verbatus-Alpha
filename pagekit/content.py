"""Content box and blank pages.

`detect_content_box(image, dpi, page_box)` returns the detector answer shape. The value
is [left, top, right, bottom] of everything to keep, in the page's own pixel grid, right
and bottom not included, or None for a blank page: None means blank, and the evidence
says why. It works on a reduced working copy and writes no file.

1. Uneven light: inside the page box, a slow brightness gradient (gutter
   shadow, staining) is flattened before anything is called ink.
2. Ink is measured from the median of the local, flattened paper, at levels set from
   the page's own paper noise. A mark that reaches the dark level is ink whatever its
   shape. A paler mark (down to the faint level) is kept only if it is shaped like a pen
   stroke, thin and elongated, or is a small piece beside one; a faint stroke that never
   reaches the strict level is flagged. Paler blotches (mottling, foxing, stains) never
   widen the box; those the size of a mark are flagged. Dark main writing does not
   raise the bar for a faint note beside it, and very faint strokes outside the box are
   flagged on every page.
3. Blank first: a page with less ink above speck size than the blank amount is blank. A blank page that still has
   some ink above speck size is flagged, and a blank page with faint structure below
   even the lenient level gets a low confidence.
4. From ink, not text lines: specks below the speck size are dropped; the solid part of dark material at the page edge (shadow, backdrop, book edge, a
   stain), as an opening by the border-seed square finds it (Vincent 1993), is taken
   out, but writing joined to it is not; ink inside that dark part is measured against
   the dark part's own level, counted as discarded and flagged. Components touching the
   paper edge, or shaped like a strip of tape near it, are debris: not discarded
   writing, so not counted as discarded ink, but debris as large as a small mark is
   flagged unless it is a line running along the edge (the sheet's own edge, the pages
   under it, a thin shadow). Every other component is kept, however far out: marginal
   notes, signatures and crosses.
8. A page that looks like noise (far more runs of dark pixels than writing makes) is
   not labelled further: the whole page box is kept, with a flag.
5. Ruled lines are kept, but a rule alone extends the box past the
   writing by at most the rule overhang setting.
6. Scanning targets: solid rectangular patches of like size in a grid,
   or a straight bar with evenly spaced ticks. On a colour page, a grid with strongly
   coloured patches, and any ruler, is excluded and reported with its box. On a
   greyscale page there is no colour cue, so a suspected target is flagged and kept.
7. Check against the crop check: the ink above speck size inside the page box but
   outside the content box is reported, in source pixels and as a share, and flagged at
   the discarded-ink thresholds (pagekit/thresholds.toml).
"""

from __future__ import annotations

import statistics
from typing import Any

from PIL import Image, ImageChops

from pagekit import _box_common as common
from pagekit.check import load_thresholds as crop_check_thresholds
from pagekit.check import otsu_threshold

_READS = (
    "plausible_page_min_mm",
    "plausible_page_max_mm",
    "fallback_dpi",
    "busy_runs_per_cm2",
    "busy_max_runs",
    "faint_contrast",
    "faint_noise_k",
    "doubt_noise_k",
    "dark_contrast",
    "stroke_max_mm",
    "stroke_min_length_mm",
    "edge_mark_mm2",
    "content_working_dpi",
    "speck_mm",
    "border_seed_mm",
    "debris_distance_mm",
    "debris_zone_mm",
    "tape_min_thickness_mm",
    "blank_contrast",
    "blank_ink_mm2",
    "background_smoothing_mm",
    "rule_min_length_mm",
    "rule_fatten_mm",
    "rule_max_thickness_mm",
    "rule_overhang_mm",
    "target_patch_min_mm",
    "target_patch_max_mm",
    "target_min_patches",
    "target_saturation",
    "ruler_min_ticks",
    "ruler_min_length_mm",
)

Box = tuple[int, int, int, int]


def detect_content_box(
    image: Image.Image,
    dpi: tuple[float, float],
    page_box: list[int] | tuple[int, int, int, int] | None = None,
    polygon: list[tuple[float, float]] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict:
    """The content-box answer for one upright page: value, confidence, evidence, flags."""
    thresholds = common.load_thresholds(overrides)
    v = common.values(thresholds)
    note = common.unmeasured_note(thresholds, _READS)
    page = common.page_input(image, dpi, polygon, v)
    size = page.grey.size
    source_box = _page_box(page_box, size)
    work = common.working_copy(page, v["content_working_dpi"])
    area = work.from_source(source_box)
    if area[2] - area[0] < 4 or area[3] - area[1] < 4:
        raise common.DetectorInputError(f"the page box {list(source_box)} is too small to measure")
    found = measure(work, area, v)

    flags: list[str] = list(page.flags)
    if found["noise"] is not None:
        flags.append(found["noise"] + " The content box is the whole page box.")
        return common.answer(
            list(source_box),
            0.1,
            f"Content kept as the whole page box {list(source_box)}: {found['noise']}{note}",
            flags,
        )
    to_source = 1 / (work.sx * work.sy)
    flags += _review_flags(found, work, area, size, to_source, v)
    kept_mm2 = found["kept_pixels"] * work.mm**2
    removed = _removed_text(found)
    targets_text, target_flags = _targets_text(found, work, area, size)
    flags += target_flags

    if found["box"] is None or kept_mm2 < v["blank_ink_mm2"]:
        if found["kept_pixels"]:
            flags.append(
                f"Judged blank, but {kept_mm2:.1f} mm² of ink above speck size remains "
                f"(less than {v['blank_ink_mm2']:g} mm²); check that nothing is lost."
            )
        why = (
            f"no mark stands {v['blank_contrast']} grey levels below the flattened paper"
            if not found["kept_pixels"]
            else f"only {kept_mm2:.1f} mm² of ink above speck size"
        )
        discarded = _discarded_text(found, None, to_source, flags)
        doubt = ""
        if found["structure"]:
            doubt = " Faint structure lies below even the faint-ink level, so blank is uncertain."
        evidence = f"Blank page: {why}{removed}.{targets_text}{discarded}{doubt}{note}"
        confidence = 0.4 if flags else 0.9
        if found["structure"]:
            confidence = min(confidence, 0.3)
        return common.answer(None, confidence, evidence, flags)

    box = found["box"]
    value = work.to_source(
        (box[0] + area[0], box[1] + area[1], box[2] + area[0], box[3] + area[1]), size
    )
    value = [
        max(value[0], source_box[0]),
        max(value[1], source_box[1]),
        min(value[2], source_box[2]),
        min(value[3], source_box[3]),
    ]
    if found["only_rules"]:
        flags.append("Only ruled lines were found, no writing; the content box is the rules' box.")
    discarded = _discarded_text(found, box, to_source, flags)
    near = found["near_edge"]
    rules = ""
    if found["rules_clipped"]:
        rules = f" Ruled lines reach past the writing; they extend the box by at most {v['rule_overhang_mm']:g} mm."
    evidence = (
        f"Content at {value}: {_plural(found['kept_count'], 'mark')} above speck size kept"
        f"{f', {near} of them near the page edge' if near else ''}{removed}.{rules}"
        f"{targets_text}{discarded}{note}"
    )
    confidence = 0.5 if flags else 0.9
    return common.answer(value, confidence, evidence, flags)


def _review_flags(
    found: dict, work: common.Work, area: Box, size, to_source: float, v
) -> list[str]:
    """Flags for ink that is kept on weak evidence or left out."""

    def source(box: Box) -> list[int]:
        return work.to_source(
            (box[0] + area[0], box[1] + area[1], box[2] + area[0], box[3] + area[1]), size
        )

    flags = []
    if found["faint"]:
        flags.append(
            f"{_plural(found['faint'], 'faint mark')} kept: lighter than "
            f"{v['blank_contrast']} grey levels below the paper but at least "
            f"{v['faint_contrast']}; check them."
        )
    if found["under"]:
        flags.append(
            f"{round(found['under'] * to_source)} source pixels of ink lie inside a dark area "
            "at the page edge (a shadow or stain) and are left out of the content box."
        )
    for kind in sorted({k for _, _, k in found["edge_marks"]}):
        marks = [(b, a) for b, a, k in found["edge_marks"] if k == kind]
        largest_box, largest = max(marks, key=lambda item: item[1])
        flags.append(
            f"{_plural(len(marks), 'mark')} {kind} left out as debris (the largest "
            f"{largest:.1f} mm\u00b2 at {source(largest_box)}); check them."
        )
    if found["large_blotches"]:
        flags.append(
            f"{_plural(found['large_blotches'], 'pale blotch', 'pale blotches')} the size of a "
            "mark (shaped like stains or foxing, not strokes) left out of the content box; "
            "check them."
        )
    if found["doubtful"] and found["box"] is not None:
        flags.append(
            f"{_plural(found['doubtful'], 'very faint stroke')} outside the content box, "
            "fainter than the faint-ink level; check them."
        )
    return flags


def _page_box(page_box: Any, size: tuple[int, int]) -> Box:
    width, height = size
    if page_box is None:
        return 0, 0, width, height
    try:
        x0, y0, x1, y1 = (int(c) for c in page_box)
    except (TypeError, ValueError) as error:
        raise common.DetectorInputError("the page box must be four integers") from error
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise common.DetectorInputError(
            f"the page box {[x0, y0, x1, y1]} is not a non-empty box inside {width}x{height}"
        )
    return x0, y0, x1, y1


def _flat(work: common.Work, area: Box, v: dict[str, Any]) -> Image.Image:
    """The page inside `area`, flattened so the local paper sits at 255."""
    grey = work.grey.crop(area)
    mask = work.mask.crop(area)
    flat = common.flatten(grey, work.px(v["background_smoothing_mm"]))
    return ImageChops.lighter(flat, ImageChops.invert(mask))  # outside the page's area: paper


def _ink_under(grey: Image.Image, region: Image.Image, work: common.Work, v: dict[str, Any]):
    """Ink inside a dark `region` at the edge, measured against the region's own level.

    Outside the region the page is replaced by the region's median level, so the
    background estimate inside it never sees the bright paper around it."""
    if region.getbbox() is None:
        return Image.new("L", grey.size, 0)
    level = common.median_level(grey.histogram(region)) or 0
    alone = Image.composite(grey, Image.new("L", grey.size, level), region)
    flat = common.flatten(alone, work.px(v["background_smoothing_mm"]))
    marks = common.threshold_map(flat, 255 - v["blank_contrast"], region)
    speck = (v["speck_mm"] / work.mm) ** 2
    parts = [p for p in common.components(marks) if p.area >= speck]
    # Lines along the sides inside the dark area are page edges or a board's edge.
    lines = common.edge_line_pieces(parts, True, work, v)
    lines |= common.edge_line_pieces(parts, False, work, v)
    return common.paint([p for p in parts if id(p) not in lines], grey.size)


def ink_levels(work: common.Work, area: Box, v: dict[str, Any]) -> dict[str, Any]:
    """The flattened page inside `area`, its paper level and noise, and the ink levels.

    Flattening puts the brightest local paper at 255, so ordinary paper sits a little
    below it. Contrast is therefore measured from the median of the paper (the pixels
    within `blank_contrast` of 255), and the faint level is set from the page's own
    paper noise: at least `faint_contrast`, and at least `faint_noise_k` robust
    standard deviations, below the paper (second review, R1)."""
    mask = work.mask.crop(area)
    flat = _flat(work, area, v)
    histogram = flat.histogram(mask)
    levels = range(255 - v["blank_contrast"], 256)
    paper = common.median_level(histogram, levels)
    paper = 255 if paper is None else paper
    deviations = [0] * 256
    for level in levels:
        deviations[abs(level - paper)] += histogram[level]
    spread = 1.4826 * (common.median_level(deviations) or 0)
    faint = max(v["faint_contrast"], v["faint_noise_k"] * spread)
    doubt = max(v["faint_contrast"] / 2, v["doubt_noise_k"] * spread)
    return {
        "flat": flat,
        "mask": mask,
        "paper": paper,
        "spread": spread,
        "lenient": common.threshold_map(flat, round(paper - faint), mask),
        "strict": common.threshold_map(flat, round(paper - v["blank_contrast"]), mask),
        "dark": common.threshold_map(flat, round(paper - v["dark_contrast"]), mask),
        "doubt": common.threshold_map(flat, round(paper - doubt), mask),
    }


def ink_map(work: common.Work, area: Box, v: dict[str, Any]) -> tuple[Image.Image, Image.Image]:
    """Ink inside `area` at the faint (lenient) level and at the strict level."""
    found = ink_levels(work, area, v)
    return found["lenient"], found["strict"]


def _stroke_shaped(part: common.Component, thick: bytes, width: int, longest: int) -> bool:
    """Thin (gone after an erosion by the thickest stroke) and elongated (at least
    `longest` pixels along its longer side): a pen stroke, not a stain or a mottle."""
    return max(part.width, part.height) >= longest and not part.touches(thick, width)


def _near(part: common.Component, size: tuple[int, int], distance: int) -> bool:
    width, height = size
    return (
        part.x0 < distance
        or part.y0 < distance
        or part.x1 > width - distance
        or part.y1 > height - distance
    )


def measure(work: common.Work, area: Box, v: dict[str, Any]) -> dict:
    """Everything the content box is decided from, in the working grid of `area`."""
    grey = work.grey.crop(area)
    mask = work.mask.crop(area)
    size = grey.size
    touch = work.px(v["debris_distance_mm"])
    zone = work.px(v["debris_zone_mm"])

    # Border-connected dark material, found on the page as scanned (before flattening,
    # which would turn a wide shadow into paper with a dark rim): the solid part (an
    # opening by the border-seed square) of dark areas at the edge. Only that solid part
    # is taken out, so writing joined to a shadow or stain is not lost with it; ink inside
    # it is measured against the dark area's own level and reported.
    histogram = grey.histogram(mask)
    split = otsu_threshold(histogram)
    dark, light = common.class_means(histogram, split)
    if dark is not None and light is not None and light - dark >= v["blank_contrast"]:
        raw = common.threshold_map(grey, split, mask)
    else:
        raw = Image.new("L", size, 0)
    noise = common.busy(raw, work, v)
    if noise is not None:
        return {"noise": noise}
    seed_r = max(1, work.px(v["border_seed_mm"]) // 2)
    solid = common.opening(raw, seed_r, seed_r)
    border = [p for p in common.components(solid) if _near(p, size, touch)]
    core = common.paint(border, size)
    border_mask = common.dilate(core, 1, 1)
    under = _ink_under(grey, border_mask, work, v)

    levels = ink_levels(work, area, v)
    lenient, strict = levels["lenient"], levels["strict"]
    noise = common.busy(lenient, work, v)
    if noise is not None:
        return {"noise": noise}
    raw_parts = common.components(ImageChops.subtract(raw, border_mask))
    ink = ImageChops.subtract(lenient, border_mask)
    speck = (v["speck_mm"] / work.mm) ** 2
    parts = common.components(ink)
    marks = [p for p in parts if p.area >= speck]
    specks = len(parts) - len(marks)
    strict_marks = strict.tobytes()
    dark_marks = levels["dark"].tobytes()
    stroke_r = max(1, work.px(v["stroke_max_mm"]) // 2)
    thick = common.erode(ink, stroke_r, stroke_r).tobytes()
    longest = work.px(v["stroke_min_length_mm"])
    # A mark that reaches the dark level is ink whatever its shape. A paler one counts
    # only if it is shaped like a pen stroke; a paler blotch (mottling, foxing, a stain)
    # is counted and reported but never widens the box (second review, R1).
    blotches = [
        p
        for p in marks
        if not p.touches(dark_marks, size[0])
        and not _stroke_shaped(p, thick, size[0], longest)
        and not _near(p, size, touch)
    ]
    # A small thin piece close to a stroke is a broken-off bit of that stroke.
    strokes = [p.box for p in marks if id(p) not in {id(b) for b in blotches}]
    blotches = [
        p
        for p in blotches
        if p.touches(thick, size[0])
        or not any(_overlaps(_grow(p.box, longest), other) for other in strokes)
    ]
    blotch_ids = {id(p) for p in blotches}
    marks = [p for p in marks if id(p) not in blotch_ids]
    # Faint marks worth a look: stroke-shaped ones that never reach the strict level. A
    # small piece kept only for lying beside a stroke is part of that stroke, not news.
    faint_ids = {
        id(p)
        for p in marks
        if not p.touches(strict_marks, size[0])
        and (p.touches(dark_marks, size[0]) or _stroke_shaped(p, thick, size[0], longest))
    }

    tape = v["tape_min_thickness_mm"] / work.mm
    rule_length = work.px(v["rule_min_length_mm"])
    thickest = v["rule_max_thickness_mm"] / work.mm
    debris, kept, debris_kind = [], [], {}
    for part in marks:
        if _near(part, size, touch):
            debris.append(part)
            debris_kind[id(part)] = "touching the paper edge"
        elif _near(part, size, zone) and _tape_shaped(part, tape):
            debris.append(part)
            debris_kind[id(part)] = "shaped like tape or a tear near the paper edge"
        else:
            kept.append(part)

    # Scanning targets. Solid patches are looked for on the page as scanned too, since
    # flattening absorbs a patch wider than the smoothing size into the background.
    colour = work.colour.crop(area) if work.colour is not None else None
    candidates = kept + [p for p in raw_parts if not _near(p, size, touch)]
    grids = find_grids(candidates, colour, work, v)
    rulers = find_rulers(kept, work, v)
    excluded_ids: set[int] = set()
    excluded: list[tuple[str, Box]] = []
    suspected: list[tuple[str, Box]] = []
    for kind, box, coloured in [(k, b, c) for k, b, _, c in grids] + [
        ("ruler", r.box, False) for r in rulers
    ]:
        if colour is not None and (kind == "ruler" or coloured):
            excluded.append((kind, box))
            grown = _grow(box, 2)
            excluded_ids.update(id(p) for p in kept if _centre_in(p, grown))
        else:
            suspected.append((kind, box))
    kept = [p for p in kept if id(p) not in excluded_ids]

    # Rules, then writing: a rule alone may reach only a little past the writing.
    kept_map = common.paint(kept, size)
    rule_map = common.long_lines(kept_map, rule_length, work.px(v["rule_fatten_mm"]), thickest)
    pieces = [
        p for p in common.components(ImageChops.subtract(kept_map, rule_map)) if p.area >= speck
    ]
    shaped = [p for p in pieces if common.rule_shaped(p, rule_length, thickest, 10.0)]
    if shaped:
        rule_map = ImageChops.lighter(rule_map, common.paint(shaped, size))
    shaped_ids = {id(p) for p in shaped}
    writing = [p.box for p in pieces if id(p) not in shaped_ids] + [b for _, b in suspected]

    writing_box = common.union_box(writing)
    rules_box = rule_map.getbbox()
    clipped = only_rules = False
    if writing_box is not None:
        box = writing_box
        if rules_box is not None:
            allowed = _grow(writing_box, work.px(v["rule_overhang_mm"]))
            limited = (
                max(rules_box[0], allowed[0]),
                max(rules_box[1], allowed[1]),
                min(rules_box[2], allowed[2]),
                min(rules_box[3], allowed[3]),
            )
            clipped = limited != rules_box
            if limited[0] < limited[2] and limited[1] < limited[3]:
                box = common.union_box([box, limited])
    elif rules_box is not None:
        box, only_rules = rules_box, True
    else:
        box = None

    # Lines along the paper edge (the sheet's own edge, the pages under it, a thin
    # shadow) are debris but not writing: they are neither counted as discarded ink nor
    # reported as marks (real-register follow-up, S4).
    width, height = size
    edge_ids: set[int] = set()
    for upright, touching in (
        (True, lambda p: p.x0 < touch),
        (True, lambda p: p.x1 > width - touch),
        (False, lambda p: p.y0 < touch),
        (False, lambda p: p.y1 > height - touch),
    ):
        pieces = [p for p in debris if touching(p)]
        edge_ids |= common.edge_line_pieces(pieces, upright, work, v)
    edge_lines = [p for p in debris if id(p) in edge_ids]

    # Ink above speck size for the crop-check comparison: every mark but excluded
    # targets, border material and edge debris, and with ruled lines counted apart. Edge
    # debris is not discarded writing: a mark-sized piece is reported on its own below,
    # and edge lines not at all (real-register follow-up, S4).
    debris_ids = {id(p) for p in debris}
    counted = common.paint(
        [p for p in marks if id(p) not in excluded_ids and id(p) not in debris_ids], size
    )
    rules_counted = ImageChops.multiply(counted, rule_map)
    counted = ImageChops.lighter(ImageChops.subtract(counted, rule_map), under)
    # Marks touching the paper edge left out as debris, large enough to be a mark
    # (a small cross), are reported, since at a low resolution they can be too few
    # pixels for the crop check's thresholds.
    edge_marks = [
        p
        for p in debris
        if id(p) not in edge_ids
        and p.area * work.mm**2 >= v["edge_mark_mm2"]
        and not common.rule_shaped(p, rule_length, thickest, 10.0)
    ]
    # Strokes fainter than the faint level, outside the box: checked on every page,
    # since a pale pen beside ordinary writing would otherwise vanish unflagged.
    doubt_map = ImageChops.subtract(levels["doubt"], ImageChops.lighter(lenient, border_mask))
    doubt_thick = common.erode(levels["doubt"], stroke_r, stroke_r).tobytes()
    doubtful = [
        p
        for p in common.components(doubt_map)
        if p.area >= speck
        and not _near(p, size, touch)
        and _stroke_shaped(p, doubt_thick, size[0], 2 * longest)
        and (box is None or not _overlaps(p.box, box))
    ]
    return {
        "noise": None,
        "under": common.count(under),
        "faint": sum(1 for p in kept if id(p) in faint_ids),
        "edge_marks": [(p.box, p.area * work.mm**2, debris_kind[id(p)]) for p in edge_marks],
        "structure": bool(doubtful),
        "doubtful": len(doubtful),
        "blotches": len(blotches),
        "large_blotches": sum(1 for p in blotches if p.area * work.mm**2 >= v["edge_mark_mm2"]),
        "box": box,
        "kept_pixels": sum(p.area for p in kept)
        + sum((b[2] - b[0]) * (b[3] - b[1]) for _, b in suspected),
        "kept_count": len(kept),
        "near_edge": sum(1 for p in kept if _near(p, size, zone)),
        "specks": specks,
        "border": len(border),
        "debris": len(debris),
        "excluded": excluded,
        "suspected": suspected,
        "rules_clipped": clipped,
        "only_rules": only_rules,
        "counted": counted,
        "edge_lines": len(edge_lines),
        "rules": rules_counted,
    }


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _grow(box: Box, by: int) -> Box:
    return box[0] - by, box[1] - by, box[2] + by, box[3] + by


def _centre_in(part: common.Component, box: Box) -> bool:
    cx, cy = (part.x0 + part.x1) / 2, (part.y0 + part.y1) / 2
    return box[0] <= cx < box[2] and box[1] <= cy < box[3]


def _tape_shaped(part: common.Component, thinnest: float) -> bool:
    """A solid strip at least `thinnest` thick and four times as long: tape or a tear."""
    across = min(part.width, part.height)
    return across >= thinnest and part.fill >= 0.6 and max(part.width, part.height) >= 4 * across


def _solid_patch(part: common.Component, low: float, high: float) -> bool:
    return (
        low <= part.width <= high
        and low <= part.height <= high
        and part.fill >= 0.8
        and max(part.width, part.height) <= 2.5 * min(part.width, part.height)
    )


def find_grids(kept, colour, work: common.Work, v: dict[str, Any]):
    """Grids of solid patches of like size: (kind, box, members, coloured)."""
    low, high = v["target_patch_min_mm"] / work.mm, v["target_patch_max_mm"] / work.mm
    patches = [(p, False) for p in kept if _solid_patch(p, low, high)]
    if colour is not None:
        saturation = colour.convert("HSV").getchannel("S")
        strong = saturation.point(lambda s: 255 if s >= v["target_saturation"] else 0)
        patches += [(p, True) for p in common.components(strong) if _solid_patch(p, low, high)]
    # Group patches of like size that sit within a patch's size of each other.
    groups: list[list[int]] = []
    seen: set[int] = set()
    for start in range(len(patches)):
        if start in seen:
            continue
        group, queue = [], [start]
        seen.add(start)
        while queue:
            i = queue.pop()
            group.append(i)
            a = patches[i][0]
            for j in range(len(patches)):
                if j in seen:
                    continue
                b = patches[j][0]
                like = abs(a.width - b.width) <= 0.35 * max(a.width, b.width) and abs(
                    a.height - b.height
                ) <= 0.35 * max(a.height, b.height)
                reach = max(a.width, a.height, b.width, b.height)
                gap_x = max(b.x0 - a.x1, a.x0 - b.x1, 0)
                gap_y = max(b.y0 - a.y1, a.y0 - b.y1, 0)
                if like and gap_x <= reach and gap_y <= reach:
                    seen.add(j)
                    queue.append(j)
        groups.append(sorted(group))
    found = []
    for group in groups:
        members = [patches[i][0] for i in group]
        # The same patch can be found in both maps; count places, not detections.
        places = {(round((p.x0 + p.x1) / 2 / 3), round((p.y0 + p.y1) / 2 / 3)) for p in members}
        if len(places) < v["target_min_patches"]:
            continue
        side = statistics.median(min(p.width, p.height) for p in members)
        rows = {round(((p.y0 + p.y1) / 2) / side) for p in members}
        columns = {round(((p.x0 + p.x1) / 2) / side) for p in members}
        if len(rows) < 2 or len(columns) < 2:
            continue
        coloured = sum(1 for i in group if patches[i][1]) * 2 >= len(places)
        box = common.union_box([p.box for p in members])
        found.append(("colour target" if coloured else "patch grid", box, members, coloured))
    return found


def find_rulers(kept, work: common.Work, v: dict[str, Any]) -> list[common.Component]:
    """Straight bars with evenly spaced ticks along them."""
    rulers = []
    shortest = work.px(v["ruler_min_length_mm"])
    for part in kept:
        horizontal = part.width >= part.height
        length = part.width if horizontal else part.height
        if length < shortest or length < 6 * min(part.width, part.height):
            continue
        counts = [0] * length
        for y, x0, x1 in part.runs:
            if horizontal:
                for x in range(x0, x1):
                    counts[x - part.x0] += 1
            else:
                counts[y - part.y0] += x1 - x0
        bar = statistics.median(counts)
        # Ticks stand out from the bar, or are cut into it (dark ticks on a pale bar).
        tick = [c >= 1.8 * bar + 1 or (bar >= 4 and c <= 0.7 * bar) for c in counts]
        centres = []
        start = None
        for i, on in enumerate(tick + [False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                centres.append((start + i - 1) / 2)
                start = None
        if len(centres) < v["ruler_min_ticks"]:
            continue
        gaps = [b - a for a, b in zip(centres, centres[1:], strict=False)]
        mean = sum(gaps) / len(gaps)
        if mean > 0 and statistics.pstdev(gaps) <= 0.2 * mean:
            rulers.append(part)
    return rulers


def _plural(number: int, noun: str, many: str | None = None) -> str:
    return f"{number} {noun if number == 1 else (many or noun + 's')}"


def _removed_text(found: dict) -> str:
    parts = []
    if found["border"]:
        parts.append(f"{_plural(found['border'], 'dark area')} connected to the edge")
    if found["debris"]:
        lines = found.get("edge_lines", 0)
        detail = (
            f" ({lines} of them lines along the paper's edge: its own edge, pages under it "
            "or a thin shadow)"
            if lines
            else ""
        )
        parts.append(
            f"{_plural(found['debris'], 'piece')} of edge debris{detail}, not counted as "
            "discarded ink"
        )
    if found["specks"]:
        parts.append(_plural(found["specks"], "speck"))
    return f"; removed {', '.join(parts)}" if parts else ""


def _targets_text(found: dict, work: common.Work, area: Box, size) -> tuple[str, list[str]]:
    def source(box: Box) -> list[int]:
        return work.to_source(
            (box[0] + area[0], box[1] + area[1], box[2] + area[0], box[3] + area[1]), size
        )

    text = ""
    flags = []
    for kind, box in found["excluded"]:
        text += f" A scanning target ({kind}) at {source(box)} is excluded from the content."
    for kind, box in found["suspected"]:
        flags.append(
            f"Suspected scanning target ({kind}) at {source(box)}; with no colour cue it is "
            "kept in the content."
        )
    return text, flags


def _outside(marks: Image.Image, box: Box | None) -> int:
    total = common.count(marks)
    return total if box is None else total - common.count(marks.crop(box))


def _discarded_text(found: dict, box: Box | None, to_source: float, flags: list[str]) -> str:
    """Ink above speck size inside the page box but outside the content box.

    Ruled lines cut at the rule overhang are reported apart and not flagged: the
    detector cuts them there on purpose."""
    total = common.count(found["counted"])
    outside = _outside(found["counted"], box)
    pixels = round(outside * to_source)
    share = round(outside / total, 6) if total else 0.0
    limits = {name: entry["value"] for name, entry in crop_check_thresholds().items()}
    if pixels and (
        pixels >= limits["discarded_min_pixels"] or share >= limits["discarded_min_share"]
    ):
        flags.append(
            f"{pixels} source pixels of ink ({share:.2%} of the ink above speck size) lie inside "
            "the page box but outside the content box, past the crop check's discarded-ink "
            "thresholds."
        )
    text = (
        " No ink above speck size lies outside it."
        if not pixels
        else f" {pixels} source pixels of ink ({share:.2%}) above speck size lie outside it."
    )
    rules = round(_outside(found["rules"], box) * to_source)
    if rules and box is not None:
        text += f" {rules} source pixels of ruled lines lie outside it, cut at the overhang."
    return text
