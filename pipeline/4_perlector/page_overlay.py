"""The page overlay: a labelled copy of the page render, the page request's second image.

Under `[feed] page_overlay = "boxes"` the page request carries two images: the
clean page render first, then this overlay, a copy of the same render with
every boxed candidate the feed shows outlined and labelled with its id -- each
witness unit with a shown box_1000 and each shown Surya line and block. It is
drawn on the render, never on the Exemplar, and never replaces the render, so
the ink is always also seen unmarked.

The drawing is deterministic: boxes are mapped from sealed-page pixels to
render pixels in exact rationals rounded half to even, outlines are axis-aligned
rectangles, and labels use Pillow's built-in bitmap font scaled by an integer
factor with nearest-neighbour resampling, so no anti-aliasing enters the bytes.
Every colour is fixed per source (`COLOURS`). The same render bytes and the
same feed give the same PNG bytes.

    plan = overlay_plan(feed_body)          # what is drawn, from the feed alone
    png = draw_page_overlay(render_bytes, plan)

The page feed records the plan with `renderer_sha256` and the PNG's
`image_sha256` (`page_feed.assemble_page_feed`); `overlay_image(feed,
read_bytes)` redraws it for the request and refuses bytes that differ from
the recorded digest.
"""

from __future__ import annotations

import io
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Final

from PIL import Image, ImageDraw, ImageFont

from common.contracts.canonical import code_digest, digest_bytes
from common.contracts.envelope import read_verified
from common.contracts.errors import SchemaRefusal

RENDERER_SHA256: Final[str] = code_digest(Path(__file__).resolve().read_text(encoding="utf-8"))

# One colour per witness letter, in letter order, then one for Surya's lines and
# one for its blocks. A letter is a sorted-label position, so its colour names
# no chair.
WITNESS_COLOURS: Final = ("#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#e377c2")
SURYA_LINE_COLOUR: Final = "#ff7f0e"
SURYA_BLOCK_COLOUR: Final = "#17becf"
LABEL_TEXT_COLOUR: Final = "#ffffff"
# The label scale: the bitmap font is about 11 px tall, so it is enlarged by one
# step per this many pixels of the render's longer edge (3x on a 2,560 render).
LABEL_SCALE_EDGE: Final = 800


def _source_colour(source: str) -> str:
    if source == "surya-line":
        return SURYA_LINE_COLOUR
    if source == "surya-block":
        return SURYA_BLOCK_COLOUR
    letter = source.removeprefix("witness-")
    return WITNESS_COLOURS[(ord(letter) - ord("A")) % len(WITNESS_COLOURS)]


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
    """The id in white on its source's colour, bitmap font, enlarged without smoothing."""
    font = ImageFont.load_default_imagefont()
    left, top, right, bottom = font.getbbox(text)
    label = Image.new("RGB", (right - left + 2, bottom - top + 2), colour)
    ImageDraw.Draw(label).text((1 - left, 1 - top), text, font=font, fill=LABEL_TEXT_COLOUR)
    return label.resize((label.width * scale, label.height * scale), Image.Resampling.NEAREST)


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
    draw = ImageDraw.Draw(image)
    for item in plan["drawn"]:
        draw.rectangle(item["box"], outline=plan["colours"][item["source"]], width=scale)
    for item in plan["drawn"]:
        label = _label_image(item["id"], plan["colours"][item["source"]], scale)
        x0, y0, x1, _y1 = item["box"]
        # Witness labels sit at the box's top-left, Surya's at its top-right, so
        # a witness unit and the detection under it do not cover each other's id.
        x = x0 if item["source"].startswith("witness-") else max(0, x1 - label.width)
        image.paste(label, (min(x, image.width - label.width), y0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


def overlay_record(render_bytes: bytes, plan: dict[str, Any]) -> dict[str, Any]:
    """The page feed's `overlay` field: the plan, the renderer and the PNG's digest."""
    if digest_bytes(render_bytes) != plan["source_image_sha256"]:
        raise SchemaRefusal("the page render's bytes are not the render the feed shows")
    png = draw_page_overlay(render_bytes, plan)
    return {**plan, "renderer_sha256": RENDERER_SHA256, "image_sha256": digest_bytes(png)}


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
    render = feed["page_render"]
    render_bytes = read_verified(
        read_bytes,
        {"relative_path": render["image_path"], "sha256": render["image_sha256"]},
        "the page render",
    )
    plan = {key: value for key, value in overlay.items() if key not in _RECORD_ONLY}
    png = draw_page_overlay(render_bytes, plan)
    if digest_bytes(png) != overlay["image_sha256"]:
        raise SchemaRefusal("the redrawn page overlay's bytes differ from the recorded digest")
    return png


_RECORD_ONLY: Final = frozenset({"renderer_sha256", "image_sha256"})
