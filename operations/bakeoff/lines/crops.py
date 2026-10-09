"""Line geometry shared by both line sources: reading order within a block, and crops.

A line is a dict with an integer `bbox` [x0, y0, x1, y1] (x1, y1 exclusive) and a
`polygon` of (x, y) points in page pixels. A crop is the line's bounding box cut from
the page with everything outside the polygon painted white: neighbouring lines'
ascenders and descenders that reach into a slanted box are removed, and white is the
page ground the CTC models were trained on. For an axis-aligned polygon the crop is the
plain box.

Crops for a page live at `<out>/_lines/<source>/<stem>/NNNN.png` (1-based, in reading
order) with `<out>/_lines/<source>/<stem>.json` listing each line's bounds and order.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

INDEX_SCHEMA = "bakeoff-line-crops.v1"


def polygon_bbox(polygon: list[list[float]], width: int, height: int) -> list[int] | None:
    """The integer box holding every polygon point, clipped to the page; None if empty."""
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    x0, y0 = max(0, math.floor(min(xs))), max(0, math.floor(min(ys)))
    x1, y1 = min(width, math.ceil(max(xs))), min(height, math.ceil(max(ys)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return [x0, y0, x1, y1]


def row_order(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top to bottom; lines whose vertical centre falls inside the previous line's
    vertical extent share its row and are read left to right (table rows, two-column
    entries on one baseline)."""
    by_centre = sorted(lines, key=lambda line: (line["bbox"][1] + line["bbox"][3]) / 2)
    rows: list[list[dict[str, Any]]] = []
    for line in by_centre:
        centre = (line["bbox"][1] + line["bbox"][3]) / 2
        if rows:
            anchor = rows[-1][0]["bbox"]
            if anchor[1] <= centre < anchor[3]:
                rows[-1].append(line)
                continue
        rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda item: item["bbox"][0])]


def crop_line(page: Image.Image, polygon: list[list[float]], bbox: list[int]) -> Image.Image:
    """The line's box from the page, outside the polygon painted white."""
    x0, y0, x1, y1 = bbox
    box = page.crop((x0, y0, x1, y1))
    mask = Image.new("L", box.size, 0)
    ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for x, y in polygon], fill=255)
    white = Image.new(box.mode, box.size, 255 if box.mode == "L" else (255, 255, 255))
    return Image.composite(box, white, mask)


def open_page(path: Path) -> Image.Image:
    """The page as Pillow reads it, in L or RGB (bilevel and palette pages widened)."""
    image = Image.open(path)
    image.load()
    if image.mode in ("1", "L", "I;16", "I"):
        return image.convert("L")
    return image.convert("RGB")


def index_path(out: Path, source: str, stem: str) -> Path:
    return out / "_lines" / source / f"{stem}.json"


def load_index(out: Path, source: str, stem: str) -> dict[str, Any] | None:
    """The page's crop index if every crop it lists is on disk, else None."""
    path = index_path(out, source, stem)
    try:
        index = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    folder = path.with_suffix("")
    if all((folder / line["file"]).is_file() for line in index.get("lines", [])):
        return index
    return None


def write_crops(
    out: Path,
    source: str,
    page_path: Path,
    lines: list[dict[str, Any]],
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Cut each line (already in reading order) and write the crops and their index."""
    from operations.bakeoff.witness_run import write_json

    folder = out / "_lines" / source / page_path.stem
    folder.mkdir(parents=True, exist_ok=True)
    page = open_page(page_path)
    rows = []
    for order, line in enumerate(lines, start=1):
        name = f"{order:04d}.png"
        crop_line(page, line["polygon"], line["bbox"]).save(folder / name, format="PNG")
        rows.append({"order": order, "file": name, **line})
    index = {
        "schema": INDEX_SCHEMA,
        "source": source,
        "page": page_path.stem,
        "page_size": list(page.size),
        "crop": "polygon mask on white, cut to the polygon's bounding box",
        **facts,
        "lines": rows,
    }
    write_json(index_path(out, source, page_path.stem), index)
    return index
