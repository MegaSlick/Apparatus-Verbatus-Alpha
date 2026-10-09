"""Line crops from the repository's cached Surya detections, shared by every `-surya` arm.

    python -m operations.bakeoff.lines.surya_lines command --pages DIR --lines-dir DIR \
        --weights BUNDLE [--threads 8]
    python -m operations.bakeoff.lines.surya_lines prepare --pages DIR --lines-dir DIR --out CACHE

`command` writes `<lines-dir>/pages.json` and prints the exact command that runs the
repository's Surya runner (`operations/serving/surya/runner.py`, in its own environment)
over the pages; `prepare` reads the page documents it wrote and cuts the line crops.

Matching a page document to a page: the runner numbers documents by input order
(`page-<n>.json` is the n-th page it was given). When `<lines-dir>/pages.json` exists
(`{"schema": "bakeoff-surya-pages.v1", "pages": [<stem>, ...]}`, in the order the runner
was given them), `page-<n>.json` is its n-th stem. Otherwise a document named
`<stem>.json` belongs to that page. A page with neither is refused by name. A document is
also refused when its recorded `input_ordinal` is not the `<n>` of its name or its
`image_size` is not the page's size, so a stale `pages.json` (the runner given the pages
in another order, or another page set) cannot hand one page another page's lines.

Reading order: Surya's text lines carry no order of their own. Each line joins the layout
block that holds most of it (at least half its area); blocks are read in Surya's own
block order (`position`, from its reading-order head or its raster fallback), lines in a
block top to bottom and, on one row, left to right. Lines in no block follow, in the same
row order.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from operations.bakeoff.lines import crops

SOURCE = "surya"
PAGES_SCHEMA = "bakeoff-surya-pages.v1"
ROOT = Path(__file__).resolve().parents[3]
SURYA_ENV = ROOT / "operations" / "serving" / "surya"
BLOCK_SHARE = 0.5


class LinesRefusal(RuntimeError):
    """The cached Surya documents do not cover the pages asked for."""


def match_documents(lines_dir: Path, pages: list[Path]) -> dict[str, Path]:
    index = lines_dir / "pages.json"
    found: dict[str, Path] = {}
    if index.is_file():
        listed = json.loads(index.read_text("utf-8"))
        if listed.get("schema") != PAGES_SCHEMA:
            raise LinesRefusal(f"{index} is not a {PAGES_SCHEMA} index")
        for ordinal, stem in enumerate(listed["pages"], start=1):
            found[stem] = lines_dir / f"page-{ordinal}.json"
    else:
        for page in pages:
            found[page.stem] = lines_dir / f"{page.stem}.json"
    missing = [p.stem for p in pages if not found.get(p.stem, Path()).is_file()]
    if missing:
        raise LinesRefusal(f"no Surya page document in {lines_dir} for pages {missing}")
    return {p.stem: found[p.stem] for p in pages}


def read_document(path: Path, page: Path) -> dict[str, Any]:
    """The runner's document for `page`, refused when it was written for another page."""
    from PIL import Image

    document = json.loads(path.read_text("utf-8"))
    named = path.stem.removeprefix("page-")
    recorded = document.get("input_ordinal")
    if path.stem.startswith("page-") and named.isdigit() and recorded not in (None, int(named)):
        raise LinesRefusal(f"{path.name} records input ordinal {recorded}, not {named}")
    with Image.open(page) as image:
        size = list(image.size)
    if document.get("image_size") != size:
        raise LinesRefusal(
            f"{path.name} was written for a {document.get('image_size')} image and page "
            f"{page.stem} is {size}; is {path.parent / 'pages.json'} stale?"
        )
    return document


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _overlap(a: list[float], b: list[float]) -> float:
    return _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def ordered_lines(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Surya's text lines in reading order, each with its block's position (or None)."""
    width, height = document["image_size"]
    blocks = sorted(document["layout"]["bboxes"], key=lambda block: block["position"])
    groups: dict[int | None, list[dict[str, Any]]] = {}
    for detected, line in enumerate(document["text_detection"]["bboxes"]):
        bbox = crops.polygon_bbox(line["polygon"], width, height)
        if bbox is None:
            continue
        area = _area(line["bbox"]) or 1.0
        shares = [(_overlap(line["bbox"], block["bbox"]) / area, block) for block in blocks]
        best = max(shares, key=lambda pair: pair[0], default=(0.0, None))
        position = best[1]["position"] if best[0] >= BLOCK_SHARE else None
        row = {
            "detected": detected,
            "block": position,
            "polygon": line["polygon"],
            "bbox": bbox,
            "confidence": line["confidence"],
        }
        groups.setdefault(position, []).append(row)
    ordered = []
    for block in blocks:
        ordered.extend(crops.row_order(groups.get(block["position"], [])))
    ordered.extend(crops.row_order(groups.get(None, [])))
    return ordered


def prepare(pages: list[Path], lines_dir: Path, out: Path) -> dict[str, dict[str, Any]]:
    """Each page's crop index, cutting the crops a page does not have yet."""
    documents = match_documents(lines_dir, pages)
    indexes = {}
    for page in pages:
        index = crops.load_index(out, SOURCE, page.stem)
        if index is None:
            document = read_document(documents[page.stem], page)
            facts = {
                "document": documents[page.stem].name,
                "reading_order": document.get("reading_order"),
                "surya_run": {
                    k: document.get("run", {}).get(k) for k in ("surya_ocr", "threads", "engine")
                },
                "order": "layout block position, then rows top to bottom, left to right",
            }
            index = crops.write_crops(out, SOURCE, page, ordered_lines(document), facts)
        indexes[page.stem] = index
    return indexes


def runner_command(pages: list[Path], lines_dir: Path, weights: Path, threads: int) -> list[str]:
    return [
        str(SURYA_ENV / ".venv" / "bin" / "python"),
        str(SURYA_ENV / "runner.py"),
        "--weights",
        str(weights),
        "--threads",
        str(threads),
        "--output-dir",
        str(lines_dir),
        *(str(p) for p in pages),
    ]


def write_pages_index(pages: list[Path], lines_dir: Path) -> None:
    from operations.bakeoff.witness_run import write_json

    lines_dir.mkdir(parents=True, exist_ok=True)
    write_json(lines_dir / "pages.json", {"schema": PAGES_SCHEMA, "pages": [p.stem for p in pages]})


def main(argv: list[str] | None = None) -> int:
    from operations.bakeoff.witness_run import list_pages

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("command", help="write pages.json and print the Surya runner command")
    c.add_argument("--weights", type=Path, required=True, help="the locked Surya bundle")
    c.add_argument("--threads", type=int, default=8)
    p = sub.add_parser("prepare", help="cut line crops from the cached Surya documents")
    p.add_argument("--out", type=Path, required=True)
    for each in (c, p):
        each.add_argument("--pages", type=Path, required=True)
        each.add_argument("--lines-dir", type=Path, required=True)
        each.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    pages = list_pages(args.pages)[: args.limit or None]
    if args.command == "command":
        write_pages_index(pages, args.lines_dir)
        print(shlex.join(runner_command(pages, args.lines_dir, args.weights, args.threads)))
        return 0
    try:
        indexes = prepare(pages, args.lines_dir, args.out)
    except LinesRefusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    print(f"{sum(len(i['lines']) for i in indexes.values())} lines on {len(indexes)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
