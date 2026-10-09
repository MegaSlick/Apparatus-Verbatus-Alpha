"""Line crops from kraken's blla baseline segmenter, shared by every `-blla` arm.

    python -m operations.bakeoff.lines.blla prepare --pages DIR --out CACHE \
        [--venv-dir DIR] [--device cpu|cuda] [--threads N]

For each page, kraken's own command in kraken's environment (`venvs/kraken`):

    kraken -d cpu|cuda:0 -a -i <page> <out>/_lines/blla/<stem>.xml segment -bl

with kraken's default segmentation model (`blla.mlmodel`, shipped in the kraken 7.1.1
wheel; SHA-256 `BLLA_SHA256`), then the same crop layout as the Surya source, cut from
each ALTO TextLine's boundary polygon in the ALTO reading order. A page whose XML is
already there is not segmented again.

`read_alto` is also the plain-line rule for every arm whose vendor answers in ALTO
(kraken, Party): lines in the order of the first `ReadingOrder` group when the file has
one, else document order; a line's text is its `String` contents with each `SP` as one
space, whitespace collapsed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from operations.bakeoff.lines import crops

SOURCE = "blla"
KRAKEN_VERSION = "7.1.1"
BLLA_SHA256 = "77a638a83c9e535620827a09e410ed36391e9e8e8126d5796a0f15b978186056"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VENV = ROOT / "operations" / "bakeoff" / "lines" / "venvs" / "kraken" / ".venv"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _points(text: str | None) -> list[list[float]]:
    values = [float(v) for v in (text or "").replace(",", " ").split()]
    return [[values[i], values[i + 1]] for i in range(0, len(values) - 1, 2)]


def _line_text(line: ET.Element) -> str:
    parts = []
    for child in line:
        name = _local(child.tag)
        if name == "String":
            parts.append(child.get("CONTENT") or "")
        elif name == "SP":
            parts.append(" ")
    return " ".join("".join(parts).split())


def read_alto(raw: bytes) -> list[dict[str, Any]]:
    """Each TextLine in reading order: id, boundary polygon, baseline, text."""
    root = ET.fromstring(raw)
    elements = {el.get("ID"): el for el in root.iter() if el.get("ID")}
    lines_in_document = [el for el in root.iter() if _local(el.tag) == "TextLine"]
    order: list[ET.Element] = []
    group = next((el for el in root.iter() if _local(el.tag) == "OrderedGroup"), None)
    if group is not None:
        seen: set[int] = set()
        for ref in group:
            target = elements.get(ref.get("REF"))
            if target is None:
                continue
            members = (
                [target]
                if _local(target.tag) == "TextLine"
                else [el for el in target.iter() if _local(el.tag) == "TextLine"]
            )
            for member in members:
                if id(member) not in seen:
                    seen.add(id(member))
                    order.append(member)
        order.extend(el for el in lines_in_document if id(el) not in seen)
    else:
        order = lines_in_document
    rows = []
    for line in order:
        polygon = next(
            (_points(el.get("POINTS")) for el in line.iter() if _local(el.tag) == "Polygon"),
            [],
        )
        if not polygon and line.get("HPOS") is not None:
            x, y = float(line.get("HPOS")), float(line.get("VPOS"))
            w, h = float(line.get("WIDTH")), float(line.get("HEIGHT"))
            polygon = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        rows.append(
            {
                "id": line.get("ID"),
                "polygon": polygon,
                "baseline": _points(line.get("BASELINE")),
                "text": _line_text(line),
            }
        )
    return rows


def alto_text(raw: bytes) -> str:
    """The plain text of an ALTO answer, one line per TextLine, empty lines dropped."""
    return "\n".join(row["text"] for row in read_alto(raw) if row["text"])


def alto_units(
    page: Path, answer: Path, base: dict[str, Any], seconds: float
) -> list[dict[str, Any]]:
    """One unit per TextLine of a vendor's ALTO answer, in its reading order: the line's
    bounds (its polygon's box on the page), baseline and text. The whole answer stays in
    `answer`, named with its digest in each unit's request."""
    from PIL import Image

    from operations.bakeoff.lines import harness

    raw = answer.read_bytes()
    rows = read_alto(raw)
    with Image.open(page) as image:
        width, height = image.size
    share = seconds / max(1, len(rows))
    units = []
    for order, row in enumerate(rows, start=1):
        bbox = crops.polygon_bbox(row["polygon"], width, height) if row["polygon"] else None
        request = {
            **base,
            "unit": f"line-{order:04d}",
            "order": order,
            "alto_id": row["id"],
            "bbox": bbox,
            "baseline": row["baseline"],
            "answer": {"file": str(answer), "sha256": harness.sha256_file(answer)},
        }
        units.append(harness.unit(request, row["text"], row["text"], share))
    return units


def device_argv(device: str) -> list[str]:
    """kraken's and Party's `-d`: always given, because their default is `auto`, which
    takes a GPU whenever one is visible."""
    return ["-d", "cuda:0" if device == "cuda" else device]


def segment_argv(
    kraken: Path, page: Path, xml: Path, device: str, threads: int | None = None
) -> list[str]:
    """kraken's own segment command; `threads` is kraken's `--threads`, else its default."""
    argv = [str(kraken), *device_argv(device)]
    argv += ["--threads", str(threads)] if threads else []
    argv += ["-a", "-i", str(page.resolve()), str(xml.resolve())]
    return [*argv, "segment", "-bl"]


def crop_rows(page: Path, raw: bytes) -> list[dict[str, Any]]:
    width, height = crops.open_page(page).size
    rows = []
    for line in read_alto(raw):
        bbox = crops.polygon_bbox(line["polygon"], width, height) if line["polygon"] else None
        if bbox is not None:
            rows.append(
                {
                    "id": line["id"],
                    "polygon": line["polygon"],
                    "baseline": line["baseline"],
                    "bbox": bbox,
                }
            )
    return rows


def prepare(
    pages: list[Path],
    out: Path,
    venv_dir: Path = DEFAULT_VENV,
    device: str = "cpu",
    runner: Any = subprocess.run,
    threads: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Segment each page that has no ALTO yet, then cut its crops; returns the indexes."""
    from operations.bakeoff.witness_run import OFFLINE_ENV, event

    folder = out / "_lines" / SOURCE
    folder.mkdir(parents=True, exist_ok=True)
    kraken = venv_dir / "bin" / "kraken"
    indexes = {}
    event(out, "prepare-start", model=f"_lines/{SOURCE}")
    for page in pages:
        xml = folder / f"{page.stem}.xml"
        argv = segment_argv(kraken, page, xml, device, threads)
        seconds = None
        if not xml.is_file():
            started = time.monotonic()
            env = {**os.environ, **OFFLINE_ENV}
            done = runner(argv, capture_output=True, text=True, check=False, env=env)
            seconds = round(time.monotonic() - started, 3)
            (folder / f"{page.stem}.log").write_text(done.stdout + done.stderr, "utf-8")
            if done.returncode != 0 or not xml.is_file():
                raise RuntimeError(f"kraken blla failed on {page.name}; see {page.stem}.log")
        index = crops.load_index(out, SOURCE, page.stem)
        if index is None:
            facts = {
                "segmenter": {
                    "kraken": KRAKEN_VERSION,
                    "model": "blla.mlmodel",
                    "sha256": BLLA_SHA256,
                    "argv": argv,
                    "seconds": seconds,
                },
                "order": "ALTO ReadingOrder (kraken's own line order)",
            }
            rows = crop_rows(page, xml.read_bytes())
            index = crops.write_crops(out, SOURCE, page, rows, facts)
        indexes[page.stem] = index
    event(out, "prepare-done", model=f"_lines/{SOURCE}", pages=len(indexes))
    return indexes


def main(argv: list[str] | None = None) -> int:
    from operations.bakeoff.witness_run import list_pages

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="segment pages with blla and cut line crops")
    p.add_argument("--pages", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--venv-dir", type=Path, default=DEFAULT_VENV)
    p.add_argument("--device", default="cpu")
    p.add_argument("--limit", type=int)
    p.add_argument("--threads", type=int, help="kraken's --threads (its default when absent)")
    args = parser.parse_args(argv)
    if not (args.venv_dir / "bin" / "kraken").is_file():
        print(f"refused: no kraken at {args.venv_dir}; run kraken_ppocr install", file=sys.stderr)
        return 2
    pages = list_pages(args.pages)[: args.limit or None]
    try:
        indexes = prepare(pages, args.out, args.venv_dir, args.device, threads=args.threads)
    except RuntimeError as failure:
        print(f"failed: {failure}", file=sys.stderr)
        return 1
    print(f"{sum(len(i['lines']) for i in indexes.values())} lines on {len(indexes)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
