"""The geometry chain from a source image to a prepared page, and back.

Coordinates are continuous: pixel (i, j) covers [i, i+1) x [j, j+1), so its centre is
(i + 0.5, j + 0.5); x runs right and y runs down. Pillow's affine transform uses the
same convention, which the tests pin.

The chain for one page has these parts, applied in this order:

0. **orientation tag** (only when a source's tag is applied): the transform
   the file's orientation tag names, exact (no resampling), giving the tagged frame. The
   eight values of the Exif orientation tag are: 1 as stored; 2 mirrored left to right,
   (x, y) to (W - x, y); 3 a half turn, (W - x, H - y); 4 mirrored top to bottom,
   (x, H - y); 5 transposed, (y, x); 6 a quarter turn clockwise, (H - y, x); 7
   transversed, (H - y, W - x); 8 a quarter turn counterclockwise, (y, W - x).
1. **quarter turn**: q quarter turns clockwise of the W x H source. For q = 1 a point
   (x, y) goes to (H - y, x); for q = 2 to (W - x, H - y); for q = 3 to (y, W - x).
   This gives the upright frame.
2. **page polygon**: the page's side of the cut in the upright frame, kept past the cut
   by the overlap, as a polygon. The page's frame is the polygon's bounding box,
   rounded outward to whole pixels; points are moved by minus its top-left corner.
3. **rotation**: the page turned counterclockwise by the skew angle about its frame's
   centre, into a levelled grid just large enough to hold the turned frame, with the
   frame's centre at the grid's centre. A point p relative to the centre goes to
   (px cos a + py sin a, -px sin a + py cos a), y down.
4. **crop**: the margin box in the levelled grid; points are moved by minus its
   top-left corner.
5. **scale**: multiplied by sx and sy, each at most 1, which are the output size over
   the margin box size.
6. **padding** (only when set): the page moved right and down by the left
   and top padding, on a canvas larger by the padding on each side, filled with the
   paper colour; the scale is unchanged.

All are affine, so the whole chain is one affine map, stored both ways in the
manifest. The prepared image is made from the original source through that one map in
a single resampling: an exact crop when there is no rotation, Pillow's bicubic
interpolation when there is, and area averaging when the page is shrunk.

The page polygon is cut from the frame with one step of Sutherland and Hodgman's
clipping method (I. E. Sutherland and G. W. Hodgman, "Reentrant Polygon Clipping",
Communications of the ACM 17(1):32-42, 1974).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from PIL import Image, ImageDraw

from pagekit.check import otsu_threshold

Point = tuple[float, float]
Affine = tuple[float, float, float, float, float, float]  # x' = a x + b y + c; y' = d x + e y + f

CONVENTION = (
    "Continuous pixel coordinates: pixel (i, j) covers [i, i+1) x [j, j+1), x right, "
    "y down. An affine [a, b, c, d, e, f] maps (x, y) to (a x + b y + c, d x + e y + f)."
)
_TRANSPOSE = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180}
_TRANSPOSE[3] = Image.Transpose.ROTATE_90
# The orientation tag's transforms, as Pillow transposes and in words.
TAG_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
TAG_WORDS = {
    1: "none (as stored)",
    2: "mirrored left to right",
    3: "a half turn",
    4: "mirrored top to bottom",
    5: "transposed (mirrored across the main diagonal)",
    6: "a quarter turn clockwise",
    7: "transversed (mirrored across the other diagonal)",
    8: "a quarter turn counterclockwise",
}


class GeometryError(ValueError):
    """A geometry that cannot be built from the values given."""


def _compose(outer: Affine, inner: Affine) -> Affine:
    """The affine map that applies `inner`, then `outer`."""
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
        raise GeometryError("the geometry chain cannot be inverted")
    return (e / det, -b / det, (b * f - c * e) / det, -d / det, a / det, (c * d - a * f) / det)


def apply(m: Affine, points: Iterable[Sequence[float]]) -> list[Point]:
    """`points` mapped through the affine `m`."""
    a, b, c, d, e, f = m
    return [(a * x + b * y + c, d * x + e * y + f) for x, y in points]


def _translate(dx: float, dy: float) -> Affine:
    return (1.0, 0.0, dx, 0.0, 1.0, dy)


def tagged_size(size: tuple[int, int], tag: int) -> tuple[int, int]:
    """The size of the frame after the orientation tag's transform."""
    width, height = size
    return (height, width) if tag in (5, 6, 7, 8) else (width, height)


def tag_affine(size: tuple[int, int], tag: int) -> Affine:
    """Stored pixels to the tagged frame for orientation tag `tag` (1 to 8)."""
    width, height = (float(side) for side in size)
    maps = {
        1: (1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        2: (-1.0, 0.0, width, 0.0, 1.0, 0.0),
        3: (-1.0, 0.0, width, 0.0, -1.0, height),
        4: (1.0, 0.0, 0.0, 0.0, -1.0, height),
        5: (0.0, 1.0, 0.0, 1.0, 0.0, 0.0),
        6: (0.0, -1.0, height, 1.0, 0.0, 0.0),
        7: (0.0, -1.0, height, -1.0, 0.0, width),
        8: (0.0, 1.0, 0.0, -1.0, 0.0, width),
    }
    if tag not in maps:
        raise GeometryError(f"an orientation tag is 1 to 8, not {tag}")
    return maps[tag]


def apply_tag(image: Image.Image, tag: int) -> Image.Image:
    """The stored image in its tagged frame: an exact transpose, no resampling."""
    return image if tag == 1 else image.transpose(TAG_TRANSPOSE[tag])


def upright_image(source: Image.Image, tag: int, turns: int) -> Image.Image:
    """The stored image after its tag and `turns` quarter turns clockwise, exactly."""
    frame = apply_tag(source, tag)
    return frame.transpose(_TRANSPOSE[turns]) if turns else frame


def upright_size(size: tuple[int, int], turns: int) -> tuple[int, int]:
    width, height = size
    return (height, width) if turns % 2 else (width, height)


def quarter_turn(size: tuple[int, int], turns: int) -> Affine:
    """Source to upright frame for `turns` quarter turns clockwise."""
    width, height = size
    if turns == 0:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    if turns == 1:
        return (0.0, -1.0, float(height), 1.0, 0.0, 0.0)
    if turns == 2:
        return (-1.0, 0.0, float(width), 0.0, -1.0, float(height))
    if turns == 3:
        return (0.0, 1.0, 0.0, -1.0, 0.0, float(width))
    raise GeometryError(f"orientation must be 0 to 3 quarter turns, not {turns}")


def _clip(polygon: list[Point], inside, cross) -> list[Point]:
    """One Sutherland-Hodgman step: the part of `polygon` where inside(point) holds."""
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


def clip_to_box(polygon: Sequence[Point], box: Sequence[float]) -> list[Point]:
    """The part of a convex `polygon` inside `box` (left, top, right, bottom), by four
    steps of Sutherland and Hodgman's method."""
    left, top, right, bottom = box
    edges = [
        (lambda p: p[0] >= left, 0, left),
        (lambda p: p[0] <= right, 0, right),
        (lambda p: p[1] >= top, 1, top),
        (lambda p: p[1] <= bottom, 1, bottom),
    ]
    kept = [tuple(point) for point in polygon]
    for inside, axis, limit in edges:
        if not kept:
            break

        def cross(start: Point, end: Point, axis=axis, limit=limit) -> Point:
            t = (limit - start[axis]) / (end[axis] - start[axis])
            return (start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1]))

        kept = _clip(kept, inside, cross)
    return kept


def _area(polygon: list[Point]) -> float:
    total = 0.0
    for index, (x1, y1) in enumerate(polygon):
        x0, y0 = polygon[index - 1]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2


def page_polygon(
    frame: tuple[int, int], split: dict[str, Any], page: int, overlap_px: float
) -> list[Point]:
    """The polygon of page `page` (0 left, 1 right) in the upright frame.

    A two-page split's cut is the straight line through its two points, extended across
    the frame. It must be closer to upright than to level, and cross the frame with
    paper on both sides. Each page keeps `overlap_px` past the cut.
    """
    width, height = frame
    rectangle: list[Point] = [(0.0, 0.0), (float(width), 0.0), (float(width), float(height))]
    rectangle.append((0.0, float(height)))
    if split["pages"] == 1:
        if page != 0:
            raise GeometryError(f"a one-page frame has no page {page + 1}")
        return rectangle
    if page not in (0, 1):
        raise GeometryError(f"a two-page frame has no page {page + 1}")
    (x0, y0), (x1, y1) = split["cut"]
    dx, dy = x1 - x0, y1 - y0
    if abs(dy) <= abs(dx):
        raise GeometryError(
            f"the cut from {[x0, y0]} to {[x1, y1]} leans 45 degrees or more; "
            "a cut runs top to bottom"
        )
    length = math.hypot(dx, dy)
    nx, ny = dy / length, -dx / length  # unit normal; flipped below to point right
    if nx < 0:
        nx, ny = -nx, -ny

    def signed(point: Point) -> float:
        return (point[0] - x0) * nx + (point[1] - y0) * ny

    sides = [signed(corner) for corner in rectangle]
    if min(sides) >= 0 or max(sides) <= 0:
        raise GeometryError(f"the cut from {[x0, y0]} to {[x1, y1]} does not cross the frame")
    direction = -1.0 if page == 0 else 1.0
    limit = -overlap_px

    def inside(point: Point) -> bool:
        return direction * signed(point) >= limit

    def cross(start: Point, end: Point) -> Point:
        s0 = direction * signed(start) - limit
        s1 = direction * signed(end) - limit
        t = s0 / (s0 - s1)
        return (start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1]))

    polygon = _clip(rectangle, inside, cross)
    if len(polygon) < 3 or _area(polygon) < 1:
        raise GeometryError(f"page {page + 1} of the cut holds no paper")
    return polygon


def frame_box(polygon: list[Point], frame: tuple[int, int]) -> tuple[int, int, int, int]:
    """The polygon's bounding box rounded outward to whole pixels, inside the frame."""
    xs = [x for x, _ in polygon]
    ys = [y for _, y in polygon]
    left = max(0, math.floor(min(xs) + 1e-9))
    top = max(0, math.floor(min(ys) + 1e-9))
    right = min(frame[0], math.ceil(max(xs) - 1e-9))
    bottom = min(frame[1], math.ceil(max(ys) - 1e-9))
    return left, top, right, bottom


def levelled_size(size: tuple[int, int], angle: float) -> tuple[int, int]:
    """The grid that holds a `size` frame turned by `angle` degrees, whole pixels."""
    width, height = size
    if angle == 0:
        return width, height
    radians = math.radians(angle)
    cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
    return (
        math.ceil(width * cos + height * sin - 1e-9),
        math.ceil(width * sin + height * cos - 1e-9),
    )


def margin_box(
    page_box: Sequence[int],
    content_box: Sequence[int] | None,
    margin_px: tuple[float, float],
    allowance_px: tuple[float, float],
) -> tuple[int, int, int, int] | None:
    """The content box grown by the margin and held to the page box plus the
    allowance, rounded outward; the whole page box for a blank page (no content box);
    None if the content box lies wholly outside that limit."""
    if content_box is None:
        return tuple(page_box)
    mx, my = margin_px
    ax, ay = allowance_px
    left = max(content_box[0] - mx, page_box[0] - ax)
    top = max(content_box[1] - my, page_box[1] - ay)
    right = min(content_box[2] + mx, page_box[2] + ax)
    bottom = min(content_box[3] + my, page_box[3] + ay)
    box = (math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


@dataclass(frozen=True)
class Chain:
    """The geometry of one prepared page, from source to output."""

    source_size: tuple[int, int]
    turns: int
    polygon: tuple[Point, ...]  # in the upright frame
    frame_box: tuple[int, int, int, int]  # in the upright frame
    angle: float  # degrees counterclockwise
    levelled_size: tuple[int, int]
    crop_box: tuple[int, int, int, int]  # in the levelled grid
    scale: tuple[float, float]
    output_size: tuple[int, int]
    tag: int = 1  # the orientation tag applied first; 1 is none
    padding: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom, pixels

    @property
    def canvas_size(self) -> tuple[int, int]:
        """The size of the written page: the scaled margin box plus the padding."""
        left, top, right, bottom = self.padding
        return (self.output_size[0] + left + right, self.output_size[1] + top + bottom)

    @property
    def content_box(self) -> tuple[int, int, int, int]:
        """Where the scaled margin box lies on the canvas."""
        left, top = self.padding[0], self.padding[1]
        return (left, top, left + self.output_size[0], top + self.output_size[1])

    def regions(self) -> dict[str, Any]:
        """Which parts of the written page are photographed source and which are fill,
        and the margin box in the levelled grid and in stored source pixels."""
        left, top, right, bottom = self.padding
        width, height = self.canvas_size
        content = self.content_box
        photographed = clip_to_box(apply(self.upright_to_output(), self.polygon), content)
        crop = self.crop_box
        corners = [(crop[0], crop[1]), (crop[2], crop[1]), (crop[2], crop[3]), (crop[0], crop[3])]
        source_corners = self.inverse(apply(self.levelled_to_output(), corners))
        return {
            "regions": {
                "canvas": [0, 0, width, height],
                "padding": {"left": left, "top": top, "right": right, "bottom": bottom},
                "content": list(content),
                "photographed": [list(point) for point in photographed],
                "fill": (
                    "every other pixel of the canvas: the padding, and any part of the "
                    "content area outside the photographed polygon (past the source's "
                    "edge or the cut), in the page's paper colour"
                ),
            },
            "margin_box": {
                "levelled": list(crop),
                "source": [list(point) for point in source_corners],
            },
        }

    def levelled_to_output(self) -> Affine:
        """The levelled grid to the written page: crop, scale and padding."""
        return (
            self.scale[0],
            0.0,
            self.padding[0] - self.crop_box[0] * self.scale[0],
            0.0,
            self.scale[1],
            self.padding[1] - self.crop_box[1] * self.scale[1],
        )

    @property
    def frame_size(self) -> tuple[int, int]:
        """The source's size after the orientation tag."""
        return tagged_size(self.source_size, self.tag)

    @property
    def upright_size(self) -> tuple[int, int]:
        return upright_size(self.frame_size, self.turns)

    def source_to_upright(self) -> Affine:
        """Stored source pixels to the upright frame: the tag, then the quarter turns."""
        return _compose(
            quarter_turn(self.frame_size, self.turns), tag_affine(self.source_size, self.tag)
        )

    @classmethod
    def build(
        cls,
        source_size: tuple[int, int],
        turns: int,
        split: dict[str, Any],
        page: int,
        overlap_px: float,
        angle: float,
        crop_box: Sequence[int] | None = None,
        scale: float = 1.0,
        tag: int = 1,
        padding: Sequence[int] = (0, 0, 0, 0),
    ) -> Chain:
        """The chain for page `page`. With no crop box, the whole levelled grid. `tag`
        is the orientation tag applied before everything else (1: none).

        `scale` above 1 is refused: pagekit never upsamples.
        """
        if not 0 < scale <= 1:
            raise GeometryError(f"scale {scale} is refused: outputs are never upsampled")
        tag_affine(source_size, tag)  # refuses a tag outside 1 to 8
        if len(padding) != 4 or any(int(side) != side or side < 0 for side in padding):
            raise GeometryError(f"padding {list(padding)} must be four whole numbers of pixels")
        upright = upright_size(tagged_size(source_size, tag), turns)
        polygon = page_polygon(upright, split, page, overlap_px)
        box = frame_box(polygon, upright)
        grid = levelled_size((box[2] - box[0], box[3] - box[1]), angle)
        crop = tuple(crop_box) if crop_box is not None else (0, 0, grid[0], grid[1])
        if crop[2] <= crop[0] or crop[3] <= crop[1]:
            raise GeometryError(f"crop box {list(crop)} is empty")
        crop_width, crop_height = crop[2] - crop[0], crop[3] - crop[1]
        output = (max(1, round(crop_width * scale)), max(1, round(crop_height * scale)))
        return cls(
            source_size=tuple(source_size),
            turns=turns,
            polygon=tuple(polygon),
            frame_box=box,
            angle=angle,
            levelled_size=grid,
            crop_box=crop,
            scale=(output[0] / crop_width, output[1] / crop_height),
            output_size=output,
            tag=tag,
            padding=tuple(int(side) for side in padding),
        )

    def upright_to_output(self) -> Affine:
        fx, fy = self.frame_box[0], self.frame_box[1]
        width, height = self.frame_box[2] - fx, self.frame_box[3] - fy
        radians = math.radians(self.angle)
        cos, sin = math.cos(radians), math.sin(radians)
        if self.angle == 0:
            cos, sin = 1.0, 0.0
        # Relative to the frame's centre, turned, then relative to the grid's top left.
        centred = _translate(-fx - width / 2, -fy - height / 2)
        turned = (cos, sin, 0.0, -sin, cos, 0.0)
        placed = _translate(
            self.levelled_size[0] / 2 - self.crop_box[0],
            self.levelled_size[1] / 2 - self.crop_box[1],
        )
        scaled = (
            self.scale[0],
            0.0,
            float(self.padding[0]),
            0.0,
            self.scale[1],
            float(self.padding[1]),
        )
        return _compose(scaled, _compose(placed, _compose(turned, centred)))

    def source_to_output(self) -> Affine:
        return _compose(self.upright_to_output(), self.source_to_upright())

    def output_to_source(self) -> Affine:
        return _invert(self.source_to_output())

    def forward(self, points: Iterable[Sequence[float]]) -> list[Point]:
        """Source points (or a polygon's vertices) to output points."""
        return apply(self.source_to_output(), points)

    def inverse(self, points: Iterable[Sequence[float]]) -> list[Point]:
        """Output points (or a polygon's vertices) to source points."""
        return apply(self.output_to_source(), points)

    def to_dict(self) -> dict[str, Any]:
        """The chain as plain parameters, readable without pagekit."""
        frame = self.frame_box
        steps: list[dict[str, Any]] = []
        if self.tag != 1:  # only when a tag is applied, so a chain without one is unchanged
            steps.append(
                {
                    "op": "orientation_tag",
                    "tag": self.tag,
                    "transform": TAG_WORDS[self.tag],
                    "affine": list(tag_affine(self.source_size, self.tag)),
                    "size_after": list(self.frame_size),
                }
            )
        return {
            "convention": CONVENTION,
            "source_size": list(self.source_size),
            "steps": steps
            + [
                {
                    "op": "quarter_turn",
                    "turns_clockwise": self.turns,
                    "size_after": list(self.upright_size),
                },
                {
                    "op": "page_polygon",
                    "polygon": [list(point) for point in self.polygon],
                    "frame_box": list(frame),
                    "size_after": [frame[2] - frame[0], frame[3] - frame[1]],
                },
                {
                    "op": "rotate",
                    "degrees_counterclockwise": self.angle,
                    "centre": [(frame[2] - frame[0]) / 2, (frame[3] - frame[1]) / 2],
                    "size_after": list(self.levelled_size),
                },
                {
                    "op": "crop",
                    "box": list(self.crop_box),
                    "size_after": [
                        self.crop_box[2] - self.crop_box[0],
                        self.crop_box[3] - self.crop_box[1],
                    ],
                },
                {
                    "op": "scale",
                    "factor": list(self.scale),
                    "size_after": list(self.output_size),
                },
            ]
            + (
                [
                    {
                        "op": "pad",
                        "left_top_right_bottom": list(self.padding),
                        "size_after": list(self.canvas_size),
                    }
                ]
                if any(self.padding)  # only when set, so a chain without it is unchanged
                else []
            ),
            "affine_source_to_output": list(self.source_to_output()),
            "affine_output_to_source": list(self.output_to_source()),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chain:
        """The chain back from `to_dict`, for mapping points from a manifest."""
        steps = {step["op"]: step for step in data["steps"]}
        turn, polygon, rotate = steps["quarter_turn"], steps["page_polygon"], steps["rotate"]
        crop, scale = steps["crop"], steps["scale"]
        return cls(
            source_size=tuple(data["source_size"]),
            turns=turn["turns_clockwise"],
            polygon=tuple(tuple(point) for point in polygon["polygon"]),
            frame_box=tuple(polygon["frame_box"]),
            angle=rotate["degrees_counterclockwise"],
            levelled_size=tuple(rotate["size_after"]),
            crop_box=tuple(crop["box"]),
            scale=tuple(scale["factor"]),
            output_size=tuple(scale["size_after"]),
            tag=steps.get("orientation_tag", {"tag": 1})["tag"],
            padding=tuple(
                steps.get("pad", {"left_top_right_bottom": (0, 0, 0, 0)})["left_top_right_bottom"]
            ),
        )


def _polygon_mask(size: tuple[int, int], polygon: list[Point]) -> Image.Image:
    """255 on every pixel whose centre lies inside the convex `polygon`, else 0.

    In the continuous convention pixel (i, j) has its centre at (i + 0.5, j + 0.5). A
    centre on the polygon's left or top edge is inside and one on its right or bottom
    edge is outside, so a polygon with an edge at x = W covers columns up to W - 1 and
    no further, and two pages sharing a cut share no pixel. Every page polygon is
    convex (a rectangle cut by a straight line, then mapped by an affine map), so each
    row is one span.
    """
    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    ys = [y for _, y in polygon]
    first = max(0, math.ceil(min(ys) - 0.5))
    last = min(height, math.ceil(max(ys) - 0.5))
    edges = [(polygon[index - 1], point) for index, point in enumerate(polygon)]
    for row in range(first, last):
        y = row + 0.5
        crossings = [
            x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            for (x0, y0), (x1, y1) in edges
            if (y0 <= y < y1) or (y1 <= y < y0)
        ]
        if len(crossings) < 2:
            continue
        start = max(0, math.ceil(min(crossings) - 0.5))
        stop = min(width, math.ceil(max(crossings) - 0.5))
        if stop > start:
            draw.rectangle((start, row, stop - 1, row), fill=255)
    return mask


def polygon_mask(size: tuple[int, int], polygon: list[Point]) -> Image.Image:
    """255 on every pixel whose centre lies inside the convex `polygon` (half-open, as
    the chain's own masks are), else 0."""
    return _polygon_mask(size, polygon)


def render(source: Image.Image, chain: Chain, fill: int | tuple[int, ...]) -> Image.Image:
    """The prepared page made from `source` (the original, decoded) in one resampling.

    Everything outside the page polygon, which includes everything outside the source,
    is `fill`. Without rotation the page is an exact crop of the quarter-turned
    source, area-averaged if shrunk. With rotation it is sampled with Pillow's bicubic
    kernel; when also shrunk by up to 1/k, it is sampled on a k-times finer grid and
    each k x k block averaged, which is one area-averaging filter, not a second pass
    over a finished image.
    """
    if source.size != chain.source_size:
        raise GeometryError("the source is not the size the chain was built for")
    if any(chain.padding):
        # The page as without padding, then placed on a canvas of the paper colour.
        page = render(source, replace(chain, padding=(0, 0, 0, 0)), fill)
        canvas = Image.new(page.mode, chain.canvas_size, fill)
        canvas.paste(page, chain.padding[:2])
        return canvas
    shrunk = chain.scale != (1.0, 1.0)
    if chain.angle == 0:
        upright = upright_image(source, chain.tag, chain.turns)
        fx, fy = chain.frame_box[0], chain.frame_box[1]
        region = (
            fx + chain.crop_box[0],
            fy + chain.crop_box[1],
            fx + chain.crop_box[2],
            fy + chain.crop_box[3],
        )
        # The part of the region inside the source, on a canvas of paper colour: a crop
        # past the source's edge would otherwise bring in black.
        page = Image.new(upright.mode, (region[2] - region[0], region[3] - region[1]), fill)
        inside = (
            max(region[0], 0),
            max(region[1], 0),
            min(region[2], upright.size[0]),
            min(region[3], upright.size[1]),
        )
        if inside[2] > inside[0] and inside[3] > inside[1]:
            page.paste(upright.crop(inside), (inside[0] - region[0], inside[1] - region[1]))
        polygon = [(x - region[0], y - region[1]) for x, y in chain.polygon]
        page = Image.composite(
            page, Image.new(page.mode, page.size, fill), _polygon_mask(page.size, polygon)
        )
        if shrunk:
            page = page.resize(chain.output_size, Image.Resampling.BOX)
        return page
    factor = math.ceil(max(1 / chain.scale[0], 1 / chain.scale[1]) - 1e-9) if shrunk else 1
    size = (chain.output_size[0] * factor, chain.output_size[1] * factor)
    fine_to_output = (1 / factor, 0.0, 0.0, 0.0, 1 / factor, 0.0)
    fine_to_source = _compose(chain.output_to_source(), fine_to_output)
    page = source.transform(
        size, Image.Transform.AFFINE, fine_to_source, Image.Resampling.BICUBIC, fillcolor=fill
    )
    upright_to_fine = _compose(_invert(fine_to_output), chain.upright_to_output())
    polygon = apply(upright_to_fine, chain.polygon)
    page = Image.composite(
        page, Image.new(page.mode, page.size, fill), _polygon_mask(page.size, polygon)
    )
    if factor > 1:
        page = page.reduce(factor)
    return page


def _median(histogram: list[int]) -> int | None:
    count = sum(histogram)
    if count == 0:
        return None
    seen = 0
    for level, number in enumerate(histogram):
        seen += number
        if seen * 2 >= count:
            return level
    return None


def paper_colour(
    source: Image.Image, chain: Chain, long_side: int
) -> tuple[int | tuple[int, ...], str]:
    """The page's paper colour and how it was found.

    Measured on a reduced working copy of the page's frame: the grey levels inside the
    page polygon are split with Otsu's threshold (pagekit.check.otsu_threshold), and
    the median of each band over the light class is the paper colour, so ink and
    stains do not pull it. A page with a single grey level is all paper.
    """
    width, height = source.size
    reduce_by = max(1.0, max(width, height) / max(1, long_side))
    small_size = (max(1, round(width / reduce_by)), max(1, round(height / reduce_by)))
    small = upright_image(source.resize(small_size, Image.Resampling.BOX), chain.tag, chain.turns)
    sx = small.size[0] / chain.upright_size[0]
    sy = small.size[1] / chain.upright_size[1]
    mask = _polygon_mask(small.size, [(x * sx, y * sy) for x, y in chain.polygon])
    grey = small.convert("L") if small.mode != "L" else small
    histogram = grey.histogram(mask)
    threshold = otsu_threshold(histogram)
    light = grey.point(lambda level: 255 if level > threshold else 0)
    light = Image.composite(light, Image.new("L", light.size, 0), mask)
    method = "median of the light class (above Otsu's threshold) inside the page"
    if light.getbbox() is None:
        light, method = mask, "median of every pixel inside the page (one grey level)"
    bands = [_median(band.histogram(light)) for band in small.split()]
    bands = [0 if level is None else level for level in bands]
    if len(bands) == 1:
        return bands[0], method
    return tuple(bands), method
