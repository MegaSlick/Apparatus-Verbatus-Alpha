"""The stage cache (spec 0008): what each step did, as images a person can open.

For each source, in `<cache>/<source sha256>/`:

- `opened`: the source as opened (after any orientation tag the chain applies), full
  resolution and a preview;
- `upright`: the upright frame, with the cut drawn, as a preview;
- per page, `side`: the page's side of the cut (with the overlap), full resolution and a
  preview; `levelled`: the side levelled by the skew, full resolution and a preview;
- per page with cropping on, `boxes`: the levelled page with its page box (blue) and
  content box (green) drawn, as a preview.

Every entry is keyed by the source's sha256 and the inputs hash and value of the step
that produced it; a re-run whose keys are unchanged writes nothing, and only entries
whose keys changed are rewritten. `index.json` lists the entries. Full-resolution
images are lossless TIFF (pagekit._tiff); previews are small PNGs. Cache images are
for looking only: no prepared page is ever made from one (each is made from the
original source, spec 0002), so the cache may be deleted at any time; the next run
rebuilds it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from pagekit._tiff import tiff_bytes
from pagekit.geometry import apply_tag, paper_colour, render, upright_image
from pagekit.project import digest, write_atomic

INDEX_NAME = "index.json"
CUT_COLOUR = (213, 94, 0)
PAGE_BOX_COLOUR = (0, 114, 178)
CONTENT_BOX_COLOUR = (0, 158, 115)


def _key(*parts: Any) -> str:
    return digest(list(parts))


def _entry(stage, page, step, inputs_hash, sha, key, full: bool) -> dict[str, Any]:
    stem = stage if page is None else f"{stage}_p{page}"
    files = {"preview": f"{stem}_{key[:16]}_preview.png"}
    if full:
        files["full"] = f"{stem}_{key[:16]}.tif"
    return {
        "stage": stage,
        "page": page,
        "step": step,
        "inputs_hash": inputs_hash,
        "source_sha256": sha,
        "key": key,
        "files": files,
    }


def plan_entries(pages: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """{source sha256: entries} for the planned pages, without writing anything."""
    by_source: dict[str, list[Any]] = {}
    for page in pages:
        by_source.setdefault(page.source.sha256, []).append(page)
    found = {}
    for sha, source_pages in by_source.items():
        first = source_pages[0]
        tag = first.chain.tag
        steps = first.steps
        entries = [
            _entry("opened", None, None, None, sha, _key(sha, "opened", tag), True),
            _entry(
                "upright",
                None,
                "split",
                steps["split"]["inputs_hash"],
                sha,
                _key(sha, "upright", tag, steps["split"]["inputs_hash"], steps["split"]["value"]),
                False,
            ),
        ]
        for page in source_pages:
            split, skew, content = (page.steps[name] for name in ("split", "skew", "content_box"))
            side = _key(sha, "side", page.number, split["inputs_hash"], split["value"], tag)
            entries.append(
                _entry("side", page.number, "split", split["inputs_hash"], sha, side, True)
            )
            levelled = _key(sha, "levelled", page.number, side, skew["inputs_hash"], skew["value"])
            entries.append(
                _entry("levelled", page.number, "skew", skew["inputs_hash"], sha, levelled, True)
            )
            if page.applied.get("page_box"):
                boxes = _key(
                    sha,
                    "boxes",
                    page.number,
                    levelled,
                    content["inputs_hash"],
                    page.steps["page_box"]["value"],
                    content["value"],
                    page.applied["crop"],
                )
                entries.append(
                    _entry(
                        "boxes",
                        page.number,
                        "content_box",
                        content["inputs_hash"],
                        sha,
                        boxes,
                        False,
                    )
                )
        found[sha] = entries
    return found


def _preview(image: Image.Image, long_side: int) -> bytes:
    import io

    scale = min(1.0, long_side / max(image.size))
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    small = image if size == image.size else image.resize(size, Image.Resampling.BOX)
    buffer = io.BytesIO()
    small.save(buffer, "PNG")
    return buffer.getvalue()


def _box_outline(draw, box, colour, scale, width):
    left, top, right, bottom = box
    corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
    points = [(x * scale, y * scale) for x, y in corners]
    draw.line([*points, points[0]], fill=colour, width=width)


def write(cache_dir: Path, pages: list[Any], settings: dict[str, Any]) -> None:
    """Write every entry whose files are missing, remove the source's files no entry
    names any more, and write each source's index when it changed."""
    long_side = settings["cache_preview_long_side_px"]
    planned = plan_entries(pages)
    for sha, entries in planned.items():
        folder = cache_dir / sha
        folder.mkdir(parents=True, exist_ok=True)
        source_pages = [page for page in pages if page.source.sha256 == sha]
        missing = [
            entry
            for entry in entries
            if not all((folder / name).is_file() for name in entry["files"].values())
        ]
        if missing:
            _write_entries(folder, missing, source_pages, settings, long_side)
        named = {name for entry in entries for name in entry["files"].values()}
        for path in folder.iterdir():
            if path.is_file() and path.name not in named and path.name != INDEX_NAME:
                path.unlink()
        index = (
            json.dumps(
                {"schema": "pagekit-stage-cache.v1", "entries": entries}, indent=2, sort_keys=True
            )
            + "\n"
        ).encode("utf-8")
        path = folder / INDEX_NAME
        if not path.is_file() or path.read_bytes() != index:
            write_atomic(path, index)


def _write_entries(folder, entries, pages, settings, long_side) -> None:
    first = pages[0]
    source = first.source.open()
    by_page = {page.number: page for page in pages}
    paper_long = settings["paper_estimate_long_side_px"]
    for entry in entries:
        stage, number = entry["stage"], entry["page"]
        files = entry["files"]
        if stage == "opened":
            image = apply_tag(source, first.chain.tag)
        elif stage == "upright":
            image = upright_image(source, first.chain.tag, first.chain.turns).convert("RGB")
            split = first.steps["split"]["value"]
            if split["pages"] == 2:
                (x0, y0), (x1, y1) = split["cut"]
                height = image.height
                top_x = x0 + (x1 - x0) * (0 - y0) / (y1 - y0)
                bottom_x = x0 + (x1 - x0) * (height - y0) / (y1 - y0)
                ImageDraw.Draw(image).line(
                    [(top_x, 0), (bottom_x, height)],
                    fill=CUT_COLOUR,
                    width=max(2, round(max(image.size) / 300)),
                )
        else:
            page = by_page[number]
            chain = page.chain
            if stage == "side":
                width = chain.frame_box[2] - chain.frame_box[0]
                height = chain.frame_box[3] - chain.frame_box[1]
                grid = (width, height)
                whole = replace(chain, angle=0.0, levelled_size=grid)
            else:
                grid = chain.levelled_size
                whole = chain
            whole = replace(
                whole,
                crop_box=(0, 0, *grid),
                scale=(1.0, 1.0),
                output_size=grid,
                padding=(0, 0, 0, 0),
            )
            fill, _ = paper_colour(source, whole, paper_long)
            image = render(source, whole, fill)
            if stage == "boxes":
                image = image.convert("RGB")
                draw = ImageDraw.Draw(image)
                line = max(2, round(max(image.size) / 300))
                _box_outline(draw, page.steps["page_box"]["value"], PAGE_BOX_COLOUR, 1.0, line)
                if page.applied.get("content_box") and page.steps["content_box"]["value"]:
                    _box_outline(
                        draw, page.steps["content_box"]["value"], CONTENT_BOX_COLOUR, 1.0, line
                    )
        if "full" in files:
            write_atomic(folder / files["full"], tiff_bytes(image, None))
        write_atomic(folder / files["preview"], _preview(image, long_side))


def links(cache_dir: Path | None, output_dir: Path, pages: list[Any]) -> dict[str, list[dict]]:
    """{source sha256: [{stage, page, href}]} for the review sheet, relative to the
    output folder."""
    if cache_dir is None:
        return {}
    import os

    found = {}
    for sha, entries in plan_entries(pages).items():
        found[sha] = [
            {
                "stage": entry["stage"],
                "page": entry["page"],
                "href": Path(
                    os.path.relpath(cache_dir / sha / entry["files"]["preview"], output_dir)
                ).as_posix(),
            }
            for entry in entries
        ]
    return found
