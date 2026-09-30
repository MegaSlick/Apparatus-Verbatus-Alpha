"""The page overlay: a labelled copy of the page render, the page request's second image.

Under `[feed] page_overlay = "boxes"` the page request carries two images: the
clean page render first, then this overlay, a copy of the same render with
every boxed candidate the feed shows outlined and labelled with its id -- each
witness unit with a shown box_1000 and each shown Surya line and block. It is
drawn on the render, never on the Exemplar, and never replaces the render, so
the ink is always also seen unmarked.

The drawing is deterministic and owes nothing to a library's choices: boxes
are mapped from sealed-page pixels to render pixels in exact rationals rounded
half to even; outlines are solid axis-aligned bands; labels are drawn from this
module's own 5x7 bitmap glyphs (`GLYPHS`), each glyph pixel a solid square of
the label scale, so no font file, font version or anti-aliasing enters the
pixels; and the PNG is written by the project's own encoder
(`common.imaging.encode_image_deterministic`), every byte fixed by the PNG and
DEFLATE specifications. The same render bytes and the same feed give the same
PNG bytes on any host.

Each source has its own colour: one per witness letter (`WITNESS_COLOURS`),
never repeated, and one each for Surya's lines and blocks. A label is placed at the
first spot no earlier label covers, scanning left to right across its box's
width, row by row from the box's top edge down to `LABEL_REACH` label heights
below its bottom edge, so labels of a witness unit and of the Surya detections
on the same box sit side by side and every label stays by its box. When no
such spot is free the label is drawn at its box's top-left corner, over
whatever label is already there.

    plan = overlay_plan(feed_body)          # what is drawn, from the feed alone
    png = draw_page_overlay(render_bytes, plan)

The page feed records the plan with `renderer_sha256` (this module's code and
the encoder's, `common/imaging.py`) and the PNG's
`image_sha256` (`page_feed.assemble_page_feed`); `overlay_image(feed,
read_bytes)` redraws it for the request and refuses bytes that differ from
the recorded digest.
"""

from __future__ import annotations

import io
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Final

from PIL import Image, ImageColor

from common import imaging
from common.contracts.canonical import code_digest, digest_bytes, digest_of
from common.contracts.envelope import read_verified
from common.contracts.errors import SchemaRefusal
from common.imaging import encode_image_deterministic

# The drawing and the PNG encoder both fix the overlay's bytes, so both are named.
RENDERER_SHA256: Final[str] = digest_of(
    {
        "page_overlay": code_digest(Path(__file__).resolve().read_text(encoding="utf-8")),
        "imaging": code_digest(Path(imaging.__file__).resolve().read_text(encoding="utf-8")),
    }
)

# One colour per witness letter, in letter order, then one for Surya's lines and
# one for its blocks, all distinct. A letter is a sorted-label position, so its
# colour names no chair. A feed with more letters than colours draws no overlay.
WITNESS_COLOURS: Final = (
    "#d62728",
    "#1f77b4",
    "#2ca02c",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
)
SURYA_LINE_COLOUR: Final = "#ff7f0e"
SURYA_BLOCK_COLOUR: Final = "#17becf"
LABEL_TEXT_COLOUR: Final = "#ffffff"
# The label scale: a glyph is 7 px tall, so it is enlarged by one step per this
# many pixels of the render's longer edge (3x on a 2,560 render).
LABEL_SCALE_EDGE: Final = 800
# How many label heights below its box a label may be placed.
LABEL_REACH: Final = 2

# 5x7 glyphs for every character an id can hold: a letter and digits.
GLYPHS: Final[dict[str, tuple[str, str, str, str, str, str, str]]] = {
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
}
_GLYPH_W: Final = 5
_GLYPH_H: Final = 7


def _source_colour(source: str) -> str:
    if source == "surya-line":
        return SURYA_LINE_COLOUR
    if source == "surya-block":
        return SURYA_BLOCK_COLOUR
    index = ord(source.removeprefix("witness-")) - ord("A")
    if not 0 <= index < len(WITNESS_COLOURS):
        raise SchemaRefusal(
            f"the overlay has {len(WITNESS_COLOURS)} distinct witness colours, and witness "
            f"{source.removeprefix('witness-')} would repeat one; switch the overlay off or "
            "show fewer witnesses"
        )
    return WITNESS_COLOURS[index]


def _render_box(
    box_px: dict[str, int], page_size: tuple[int, int], render_size: tuple[int, int]
) -> list[int]:
    """A sealed-page `{x, y, w, h}` as render-pixel `[x0, y0, x1, y1]`, at least 1 px wide."""
    (width, height), (render_w, render_h) = page_size, render_size
    x0 = round(Fraction(box_px["x"] * render_w, width))
    y0 = round(Fraction(box_px["y"] * render_h, height))
    x1 = round(Fraction((box_px["x"] + box_px["w"]) * render_w, width))
    y1 = round(Fraction((box_px["y"] + box_px["h"]) * render_h, height))
    x0, y0 = min(x0, render_w - 1), min(y0, render_h - 1)
    return [x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)]


def overlay_plan(feed: dict[str, Any]) -> dict[str, Any]:
    """What the overlay draws for one feed: every shown boxed id, in render pixels.

    `feed` needs `page_render` (not `None`), `page_size`, `witnesses` and
    `surya` as the page feed builds them. Witness units are drawn only where
    their box_1000 is shown, so switching coordinates off removes them here too.
    """
    render = feed["page_render"]
    if render is None:
        raise SchemaRefusal("the page overlay is drawn on the page render, and none is shown")
    dimensions = render["transform"]["target_dimensions"]
    render_size = (dimensions["w"], dimensions["h"])
    page_size = (feed["page_size"]["w"], feed["page_size"]["h"])
    drawn = [
        {
            "id": unit["id"],
            "source": f"witness-{row['letter']}",
            "box": _render_box(unit["box_px"], page_size, render_size),
        }
        for row in feed["witnesses"]
        for unit in row["units"]
        if unit["box_1000"] is not None
    ]
    surya = feed["surya"]
    if surya is not None:
        drawn += [
            {
                "id": line["id"],
                "source": "surya-line",
                "box": _render_box(line["box_px"], page_size, render_size),
            }
            for line in surya["lines"]
        ]
        drawn += [
            {
                "id": block["id"],
                "source": "surya-block",
                "box": _render_box(block["box_px"], page_size, render_size),
            }
            for block in surya["blocks"]
        ]
    sources = sorted({item["source"] for item in drawn})
    return {
        "source_image_sha256": render["image_sha256"],
        "dimensions": {"w": render_size[0], "h": render_size[1]},
        "label_scale": max(1, max(render_size) // LABEL_SCALE_EDGE),
        "colours": {source: _source_colour(source) for source in sources},
        "drawn": drawn,
    }


def _label_image(text: str, colour: str, scale: int) -> Image.Image:
    """The id in white on its source's colour, from `GLYPHS`, each glyph pixel `scale` square."""
    label = Image.new("RGB", label_size(text, scale), ImageColor.getrgb(colour))
    ink = ImageColor.getrgb(LABEL_TEXT_COLOUR)
    for position, character in enumerate(text):
        glyph = GLYPHS.get(character)
        if glyph is None:
            raise SchemaRefusal(f"the overlay has no glyph for {character!r} in id {text!r}")
        left = 1 + position * (_GLYPH_W + 1)
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "#":
                    x, y = (left + column) * scale, (1 + row) * scale
                    label.paste(ink, (x, y, x + scale, y + scale))
    return label


def _outline(image: Image.Image, box: list[int], colour: tuple[int, int, int], width: int) -> None:
    """A solid band `width` px wide just inside `[x0, y0, x1, y1]`, clipped to the image."""
    x0, y0, x1, y1 = box
    for band in (
        (x0, y0, x1, min(y1, y0 + width)),
        (x0, max(y0, y1 - width), x1, y1),
        (x0, y0, min(x1, x0 + width), y1),
        (max(x0, x1 - width), y0, x1, y1),
    ):
        image.paste(colour, band)


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _label_place(
    box: list[int], size: tuple[int, int], image_size: tuple[int, int], placed: list
) -> tuple[int, int]:
    """The first free spot for a label by its box, else the box's top-left corner.

    Spots run left to right across the box's width, row by row from its top
    edge to `LABEL_REACH` label heights below its bottom edge, kept inside the
    render.
    """
    (width, height), (image_w, image_h) = size, image_size
    x0, y0, x1, y1 = box
    left = max(0, min(x0, image_w - width))
    right = max(left, min(x1, image_w) - width)
    top = max(0, min(y0, image_h - height))
    bottom = max(top, min(y1 + LABEL_REACH * height, image_h) - height)
    for y in range(top, bottom + 1, height):
        for x in range(left, right + 1, width):
            spot = (x, y, x + width, y + height)
            if not any(_overlaps(spot, other) for other in placed):
                return x, y
    return left, top


def draw_page_overlay(render_bytes: bytes, plan: dict[str, Any]) -> bytes:
    """The overlay PNG: the render with each planned box outlined and labelled."""
    try:
        with Image.open(io.BytesIO(render_bytes)) as opened:
            image = opened.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise SchemaRefusal("the page render could not be read to draw its overlay") from error
    if image.size != (plan["dimensions"]["w"], plan["dimensions"]["h"]):
        raise SchemaRefusal(
            f"the page render is {image.size[0]}x{image.size[1]}, not the planned "
            f"{plan['dimensions']['w']}x{plan['dimensions']['h']}"
        )
    scale = plan["label_scale"]
    colours = {source: ImageColor.getrgb(colour) for source, colour in plan["colours"].items()}
    for item in plan["drawn"]:
        _outline(image, item["box"], colours[item["source"]], scale)
    for item, (x, y, _x1, _y1) in zip(plan["drawn"], label_boxes(plan), strict=True):
        image.paste(_label_image(item["id"], plan["colours"][item["source"]], scale), (x, y))
    return encode_image_deterministic(image)


def label_size(text: str, scale: int) -> tuple[int, int]:
    """The `(width, height)` of one label, in render pixels."""
    return (1 + len(text) * (_GLYPH_W + 1)) * scale, (_GLYPH_H + 2) * scale


def label_boxes(plan: dict[str, Any]) -> list[tuple[int, int, int, int]]:
    """Where each planned id's label is drawn, `(x0, y0, x1, y1)`, in plan order."""
    image_size = (plan["dimensions"]["w"], plan["dimensions"]["h"])
    placed: list[tuple[int, int, int, int]] = []
    for item in plan["drawn"]:
        width, height = label_size(item["id"], plan["label_scale"])
        x, y = _label_place(item["box"], (width, height), image_size, placed)
        placed.append((x, y, x + width, y + height))
    return placed


def overlay_record(render_bytes: bytes, plan: dict[str, Any]) -> dict[str, Any]:
    """The page feed's `overlay` field: the plan, the renderer and the PNG's digest."""
    if digest_bytes(render_bytes) != plan["source_image_sha256"]:
        raise SchemaRefusal("the page render's bytes are not the render the feed shows")
    png = draw_page_overlay(render_bytes, plan)
    return {**plan, "renderer_sha256": RENDERER_SHA256, "image_sha256": digest_bytes(png)}


def render_ref(render: Any) -> dict[str, str]:
    """The `{relative_path, sha256}` of a page render's retained image, or a refusal."""
    if (
        not isinstance(render, dict)
        or not isinstance(render.get("image_path"), str)
        or not isinstance(render.get("image_sha256"), str)
    ):
        raise SchemaRefusal(
            "the page render names no retained image (image_path and image_sha256), so its "
            "bytes cannot be read to draw the overlay"
        )
    return {"relative_path": render["image_path"], "sha256": render["image_sha256"]}


def overlay_image(feed: dict[str, Any], read_bytes: Callable[[str], bytes]) -> bytes:
    """The overlay PNG a feed records, redrawn from its page render and checked."""
    overlay = feed.get("overlay")
    if overlay is None:
        raise SchemaRefusal("the page feed shows no overlay")
    if overlay["renderer_sha256"] != RENDERER_SHA256:
        raise SchemaRefusal(
            "the page feed's overlay was drawn by another overlay renderer; it cannot be "
            "redrawn to the recorded bytes"
        )
    render_bytes = read_verified(read_bytes, render_ref(feed["page_render"]), "the page render")
    plan = {key: value for key, value in overlay.items() if key not in _RECORD_ONLY}
    png = draw_page_overlay(render_bytes, plan)
    if digest_bytes(png) != overlay["image_sha256"]:
        raise SchemaRefusal("the redrawn page overlay's bytes differ from the recorded digest")
    return png


_RECORD_ONLY: Final = frozenset({"renderer_sha256", "image_sha256"})
