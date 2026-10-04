"""Triage decision rows for pages prepared by pagekit, cut from the original scans.

pagekit makes each page through one affine chain: a quarter turn, the page's polygon
on its side of a cut (which may lean, and is kept an overlap past it), a rotation
about the page's centre, a crop, and an optional shrink, with everything outside the
page's polygon filled with the page's paper colour. A triage part of the second
operation order (`region-crop-rotate-crop`) is a rectangular region of the frame (the
parts' regions partition it), a crop inside the region, a clockwise rotation about
the crop's centre onto an expanded canvas, a crop of that canvas, and the level that
fills it beyond the scan (`common.imaging.triage_apply_recipe`).

Both rotations are rigid, so the quarter turn and the skew fold into one triage
rotation of ``90 * turns - skew`` degrees clockwise. The first crop is the smallest
box of the original holding every source pixel pagekit's page shows; the crop after
rotation is pagekit's own page, to the nearest whole pixel, and the fill is pagekit's
paper colour. A page with no skew is therefore cut exactly, and a skewed one to within
half a pixel on each axis: the crop after rotation starts on a whole pixel, and where
pagekit's page falls on the rotated canvas is fixed by its own sub-pixel position.

What triage still cannot say is a cut that is not straight along the frame, a pixel
given to two pages, and a shrink. For a split frame, each page's first crop holds
every pixel either page shows inside that page's region, so across the frame's pages
nothing pagekit kept is dropped; what lies past the straight split is on the facing
page. Each such difference is returned as a note in plain words.

Coordinates are continuous, as in `pagekit.geometry`: pixel (i, j) covers
[i, i+1) x [j, j+1). An affine (a, b, c, d, e, f) maps (x, y) to
(a x + b y + c, d x + e y + f).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from common.contracts import triage as triage_manifest
from common.imaging import check_triage_post_crop, triage_page_to_frame
from pagekit.geometry import Chain, apply, quarter_turn, upright_size

Point = tuple[float, float]
Affine = tuple[float, float, float, float, float, float]

PRODUCER_IDENTITY: Final = "pagekit"
# Pillow's bicubic kernel reads two pixels either side of a sample; a rotated page's
# crop keeps them where the region allows, so its edge is sampled from real pixels.
_BICUBIC_REACH: Final = 2
# Below this, a distance is rounding in the arithmetic, not a pixel.
_EPSILON: Final = 1e-6
# A difference of at most half a pixel is not worth telling a person about.
_NOTEWORTHY_PX: Final = 0.5

GUTTER: Final = "gutter"
SHRUNK: Final = "shrunk"
NOTE_SUMMARIES: Final = {
    GUTTER: (
        "the frame is split along a straight line, but pagekit's cut leans or keeps an "
        "overlap: what lies past the line is on the facing page's Door page"
    ),
    SHRUNK: "pagekit shrank the page; the Door's page keeps the scan's full resolution",
}


class MappingError(ValueError):
    """pagekit's geometry for a source cannot be turned into a triage row."""


@dataclass(frozen=True)
class PageNote:
    code: str  # GUTTER or SHRUNK
    text: str


@dataclass(frozen=True)
class MappedPage:
    """One page as the Door will cut it, and where pagekit's page lies inside it."""

    part: dict[str, Any]
    door_size: tuple[int, int]
    # pagekit's whole page in the Door page's coordinates, as (left, top, right, bottom).
    pagekit_box_in_door: tuple[float, float, float, float]
    notes: tuple[PageNote, ...]


def _compose(outer: Affine, inner: Affine) -> Affine:
    a, b, c, d, e, f = outer
    p, q, r, s, t, u = inner
    return (
        a * p + b * s,
        a * q + b * t,
        a * r + b * u + c,
        d * p + e * s,
        d * q + e * t,
        d * r + e * u + f,
    )


def _invert(m: Affine) -> Affine:
    a, b, c, d, e, f = m
    det = a * e - b * d
    if det == 0:
        raise MappingError("a geometry in the chain cannot be inverted")
    return (e / det, -b / det, (b * f - c * e) / det, -d / det, a / det, (c * d - a * f) / det)


def door_affine(part: dict[str, Any]) -> tuple[Affine, tuple[int, int]]:
    """The map from the original frame to the Door's page for one triage part, and the
    page's size, as `common.imaging.render_triage_derivative` renders it."""
    post = part["post_crop_box"]
    return _invert(triage_page_to_frame(part)), (post["w"], post["h"])


def _signed_area(polygon: Sequence[Point]) -> float:
    total = 0.0
    for index, (x1, y1) in enumerate(polygon):
        x0, y0 = polygon[index - 1]
        total += x0 * y1 - x1 * y0
    return total / 2


def _clip_half_plane(polygon: list[Point], inside, cross) -> list[Point]:
    """One Sutherland-Hodgman step: the part of a convex `polygon` where inside() holds."""
    kept: list[Point] = []
    for index, current in enumerate(polygon):
        previous = polygon[index - 1]
        if inside(current):
            if not inside(previous):
                kept.append(cross(previous, current))
            kept.append(current)
        elif inside(previous):
            kept.append(cross(previous, current))
    return kept


def _intersect(subject: list[Point], clipper: Sequence[Point]) -> list[Point]:
    """The intersection of two convex polygons (empty when they do not overlap)."""
    orientation = 1.0 if _signed_area(clipper) >= 0 else -1.0
    result = list(subject)
    for index, end in enumerate(clipper):
        if len(result) < 3:
            return []
        start = clipper[index - 1]
        ex, ey = end[0] - start[0], end[1] - start[1]

        def side(point: Point, start=start, ex=ex, ey=ey) -> float:
            return orientation * (ex * (point[1] - start[1]) - ey * (point[0] - start[0]))

        def cross(p: Point, q: Point, side=side) -> Point:
            sp, sq = side(p), side(q)
            t = sp / (sp - sq)
            return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))

        result = _clip_half_plane(result, lambda point, side=side: side(point) >= -_EPSILON, cross)
    return result if len(result) >= 3 and abs(_signed_area(result)) > _EPSILON else []


def _rectangle(x0: float, y0: float, x1: float, y1: float) -> list[Point]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _shown(chain: Chain) -> list[Point]:
    """The source area pagekit's page shows: its output rectangle mapped back to the
    source, inside the page's polygon (beyond it pagekit shows paper colour)."""
    width, height = chain.output_size
    on_source = apply(chain.output_to_source(), _rectangle(0, 0, width, height))
    from_upright = _invert(quarter_turn(chain.source_size, chain.turns))
    polygon = apply(from_upright, chain.polygon)
    return _intersect(on_source, polygon)


def _box_around(polygons: Sequence[list[Point]], pad: int, bounds: dict[str, int]):
    """The whole-pixel box holding `polygons` grown by `pad`, inside `bounds`, or None."""
    points = [point for polygon in polygons for point in polygon]
    if not points:
        return None
    left = max(bounds["x"], math.floor(min(x for x, _ in points) + _EPSILON) - pad)
    top = max(bounds["y"], math.floor(min(y for _, y in points) + _EPSILON) - pad)
    right = min(bounds["x"] + bounds["w"], math.ceil(max(x for x, _ in points) - _EPSILON) + pad)
    bottom = min(bounds["y"] + bounds["h"], math.ceil(max(y for _, y in points) - _EPSILON) + pad)
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}


def _clockwise_millidegrees(turns: int, skew: float) -> int:
    """The one triage rotation for a quarter turn then a counterclockwise skew."""
    millidegrees = round((90 * turns - skew) * 1000)
    millidegrees = (millidegrees + 180_000) % 360_000 - 180_000
    return 180_000 if millidegrees == -180_000 else millidegrees


def _regions(
    chains: Sequence[Chain], split: dict[str, Any]
) -> list[tuple[dict[str, int], int, float]]:
    """Each page's region of the frame, with the axis it is split on and the line."""
    width, height = chains[0].source_size
    whole = {"x": 0, "y": 0, "w": width, "h": height}
    if len(chains) == 1:
        return [(whole, 0, 0.0)]
    turns = chains[0].turns
    upright_width, upright_height = upright_size((width, height), turns)
    (x0, y0), (x1, y1) = split["cut"]
    # Where the cut crosses the middle of the upright frame, back on the source.
    middle_y = upright_height / 2
    middle_x = x0 + (middle_y - y0) * (x1 - x0) / (y1 - y0)
    from_upright = _invert(quarter_turn((width, height), turns))
    point = apply(from_upright, [(middle_x, middle_y)])[0]
    axis = turns % 2  # 0: split across x on the source, 1: across y
    extent = (width, height)[axis]
    line = min(max(round(point[axis]), 1), extent - 1)
    low = dict(whole)
    high = dict(whole)
    if axis == 0:
        low["w"], high["x"], high["w"] = line, line, width - line
    else:
        low["h"], high["y"], high["h"] = line, line, height - line
    # The upright frame's left page lies at the source's low side for 0 and 3 turns.
    first_low = turns in (0, 3)
    return [(low if first_low else high, axis, line), (high if first_low else low, axis, line)]


def _beyond(polygon: list[Point], region: dict[str, int], axis: int) -> float:
    """How far `polygon` reaches out of `region` along the split axis, in pixels."""
    start = region["x"] if axis == 0 else region["y"]
    end = start + (region["w"] if axis == 0 else region["h"])
    values = [point[axis] for point in polygon]
    return max(0.0, start - min(values), max(values) - end)


def map_pages(
    chains: Sequence[Chain], split: dict[str, Any], fills: Sequence[Sequence[int]]
) -> list[MappedPage]:
    """The triage part of each page of one source, from pagekit's chains, in page order.

    `split` is pagekit's split value for the source, and `fills` each page's paper
    colour as sample levels in the master's own mode (`fill_levels`). Every page's
    colour mode is `keep`: the Door stores pagekit's source modes losslessly, and no
    page is converted.
    """
    if not chains or len(chains) != split["pages"] or len(fills) != len(chains):
        raise MappingError("one chain and one fill are needed for each page of the split")
    if len({(chain.source_size, chain.turns) for chain in chains}) != 1:
        raise MappingError("the pages of one source disagree on its size or quarter turn")
    shown = [_shown(chain) for chain in chains]
    for number, polygon in enumerate(shown, start=1):
        if not polygon:
            raise MappingError(f"page {number} shows no part of its source")
    regions = _regions(chains, split)
    mapped = []
    for index, (chain, (region, axis, _line)) in enumerate(zip(chains, regions, strict=True)):
        region_polygon = _rectangle(
            region["x"], region["y"], region["x"] + region["w"], region["y"] + region["h"]
        )
        # Every source pixel any page shows that falls in this region, so the frame's
        # pages together drop nothing pagekit kept.
        held = [_intersect(polygon, region_polygon) for polygon in shown]
        rotation = _clockwise_millidegrees(chain.turns, chain.angle)
        pad = 0 if rotation % 90_000 == 0 else _BICUBIC_REACH
        crop = _box_around([polygon for polygon in held if polygon], pad, region)
        notes: list[PageNote] = []
        if crop is None:
            crop = dict(region)
        crop_box = {
            "x": crop["x"] - region["x"],
            "y": crop["y"] - region["y"],
            "w": crop["w"],
            "h": crop["h"],
        }
        unplaced = triage_manifest.make_part(
            {key: region[key] for key in ("x", "y", "w", "h")},
            crop_box,
            rotation,
            colour_mode="keep",
            post_crop_box={"x": 0, "y": 0, "w": 1, "h": 1},
            fill=list(fills[index]),
        )
        # pagekit's page on the rotated canvas, cut out to the nearest pixel, together
        # with whatever of the facing page this region holds, so none of it is dropped.
        to_canvas = _invert(triage_page_to_frame(unplaced))
        page_corners = apply(
            _compose(to_canvas, chain.output_to_source()),
            _rectangle(0, 0, *chain.output_size),
        )
        left = round(min(x for x, _ in page_corners))
        top = round(min(y for _, y in page_corners))
        if chain.scale == (1.0, 1.0):
            right, bottom = left + chain.output_size[0], top + chain.output_size[1]
        else:
            right = round(max(x for x, _ in page_corners))
            bottom = round(max(y for _, y in page_corners))
        facing = [
            point
            for other, polygon in enumerate(held)
            if other != index
            for point in apply(to_canvas, polygon)
        ]
        if facing:
            left = min(left, math.floor(min(x for x, _ in facing) + _EPSILON))
            top = min(top, math.floor(min(y for _, y in facing) + _EPSILON))
            right = max(right, math.ceil(max(x for x, _ in facing) - _EPSILON))
            bottom = max(bottom, math.ceil(max(y for _, y in facing) - _EPSILON))
        part = triage_manifest.make_part(
            unplaced["region"],
            unplaced["crop_box"],
            rotation,
            colour_mode="keep",
            post_crop_box={"x": left, "y": top, "w": right - left, "h": bottom - top},
            fill=list(fills[index]),
        )
        try:
            check_triage_post_crop(part)
        except ValueError as error:
            raise MappingError(
                f"page {index + 1}: the Door would refuse this page ({error}); pagekit's "
                "margin runs too far past so small a scan. Set a smaller margin for it"
            ) from error
        to_door, door_size = door_affine(part)
        corners = apply(
            _compose(to_door, chain.output_to_source()),
            _rectangle(0, 0, *chain.output_size),
        )
        box = (
            min(x for x, _ in corners),
            min(y for _, y in corners),
            max(x for x, _ in corners),
            max(y for _, y in corners),
        )
        if len(chains) > 1:
            past = _beyond(shown[index], region, axis)
            facing = max(
                (
                    _beyond(polygon, regions[other][0], axis)
                    for other, polygon in enumerate(held)
                    if other != index and polygon
                ),
                default=0.0,
            )
            if past > _NOTEWORTHY_PX or facing > _NOTEWORTHY_PX:
                notes.append(
                    PageNote(
                        GUTTER,
                        f"up to {past:.0f} px of this page past the straight split are on "
                        f"the facing page's Door page, and this Door page holds up to "
                        f"{facing:.0f} px of the facing page",
                    )
                )
        if chain.scale != (1.0, 1.0):
            notes.append(
                PageNote(
                    SHRUNK,
                    f"pagekit shrank this page by {chain.scale[0]:.3f} x {chain.scale[1]:.3f}; "
                    "the Door's page keeps the scan's resolution",
                )
            )
        mapped.append(MappedPage(part, door_size, box, tuple(notes)))
    return mapped


def fill_levels(paper: int | Sequence[int], master_mode: str, palette: Sequence[int] | None):
    """pagekit's paper colour (grey, or RGB for colour and palette masters) as a triage
    fill: sample levels in the master's own mode."""
    levels = [paper] if isinstance(paper, int) else list(paper)
    if master_mode == "1":
        return [255 if levels[0] >= 128 else 0]
    if master_mode == "P":
        if palette is None:
            raise MappingError("a palette master carries no palette")
        entries = [palette[index : index + 3] for index in range(0, len(palette) - 2, 3)]
        return [
            min(
                range(len(entries)),
                key=lambda index: sum(
                    (a - b) ** 2 for a, b in zip(entries[index], levels, strict=True)
                ),
            )
        ]
    return levels


def make_row(
    *,
    corpus_id: str,
    source_sha256: str,
    frame: tuple[int, int],
    pages: Sequence[MappedPage],
    revision: str,
    confidence: int,
    human_override: bool,
) -> dict[str, Any]:
    """The `triage-decision-manifest-v1` row for one source, actor pagekit at `revision`.

    A row a person corrected is declared `semi`; one pagekit settled alone is `auto`.
    The shipped triage modes route both to review.
    """
    return triage_manifest.make_row(
        corpus_id=corpus_id,
        source_frame_sha256=source_sha256,
        frame={"width": frame[0], "height": frame[1]},
        split=triage_manifest.make_split(
            [page.part for page in pages],
            operation_order=triage_manifest.SPLIT_OPERATION_ORDER_V2,
        ),
        re_shoot_cluster_id=None,
        confidence=confidence,
        mode="semi" if human_override else "auto",
        actor={"kind": "producer", "identity": PRODUCER_IDENTITY, "revision": revision},
        human_override=human_override,
    )


def make_manifest(corpus_id: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The closed decision manifest, rows in source-digest order, validated."""
    manifest = {
        "schema": triage_manifest.MANIFEST_SCHEMA,
        "corpus_id": corpus_id,
        "records": sorted(rows, key=lambda row: row["source_frame_sha256"]),
    }
    return triage_manifest.validate_manifest(manifest)
