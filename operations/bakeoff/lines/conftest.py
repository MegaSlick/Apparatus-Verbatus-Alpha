"""Synthetic pages, Surya page documents and ALTO for the line-arm tests. No real text."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest
from PIL import Image, ImageDraw

from operations.serving import surya_detector

RECORD_KEYS = {
    "schema", "model", "arm", "repo", "revision", "weights", "server", "page", "source_file",
    "source_sha256", "units", "text", "empty", "finish_reason", "loop", "loop_reasons",
    "seconds", "error", "written",
}  # fmt: skip
PAGE_SIZE = (600, 400)
# Three "written lines" per page, as dark bars, given top to bottom.
LINE_BOXES = [(40, 40, 560, 80), (40, 160, 300, 200), (320, 160, 560, 200)]


def draw_page(path: Path) -> None:
    image = Image.new("L", PAGE_SIZE, 255)
    draw = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in LINE_BOXES:
        draw.rectangle((x0 + 5, y0 + 10, x1 - 5, y1 - 10), fill=0)
    image.save(path, compression="tiff_lzw")


@pytest.fixture
def pages(tmp_path: Path) -> Path:
    folder = tmp_path / "pages"
    folder.mkdir()
    for index in range(2):
        draw_page(folder / f"p{index:03d}.tif")
    return folder


def _quad(box: tuple[int, int, int, int]) -> list[list[int]]:
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def surya_documents(count: int) -> dict[int, bytes]:
    """The runner's page documents, built by the repository's own fixture builder.

    Lines are declared out of reading order (the detector gives no order); one block
    holds the top line, a second block (read first) holds the lower row.
    """
    lines, blocks = [], []
    for ordinal in range(1, count + 1):
        for box in (LINE_BOXES[2], LINE_BOXES[0], LINE_BOXES[1]):
            lines.append({"page_ordinal": ordinal, "polygon": _quad(box), "confidence_bp": 9000})
        for position, box in enumerate(((30, 150, 570, 210), (30, 30, 570, 90))):
            blocks.append(
                {
                    "page_ordinal": ordinal,
                    "polygon": _quad(box),
                    "label": "Text",
                    "raw_label": "Text",
                    "position": position,
                    "confidence_bp": 9000,
                }
            )
    sizes = {ordinal: PAGE_SIZE for ordinal in range(1, count + 1)}
    facts = {"engine": surya_detector.FIXTURE_ENGINE, "declared_by": "test"}
    return surya_detector.declared_page_documents(lines, blocks, sizes, facts)


@pytest.fixture
def surya_dir(tmp_path: Path, pages: Path) -> Path:
    folder = tmp_path / "surya"
    folder.mkdir()
    stems = sorted(p.stem for p in pages.iterdir())
    for ordinal, raw in surya_documents(len(stems)).items():
        (folder / f"page-{ordinal}.json").write_bytes(raw)
    index = {"schema": "bakeoff-surya-pages.v1", "pages": stems}
    (folder / "pages.json").write_text(json.dumps(index))
    return folder


def alto(image: str, lines: list[tuple[tuple[int, int, int, int], list[str]]], order=None) -> str:
    """A kraken-style ALTO page: each line a polygon, a baseline and String/SP children.

    `order` lists line indexes for a ReadingOrder group; None writes no ReadingOrder.
    """
    body = []
    for index, (box, words) in enumerate(lines):
        x0, y0, x1, y1 = box
        children = []
        for n, word in enumerate(words):
            if n:
                children.append('<SP HPOS="0" VPOS="0" WIDTH="1" HEIGHT="1"/>')
            children.append(
                f"<String CONTENT={quoteattr(word)}><Glyph CONTENT={quoteattr(word[:1])}/></String>"
            )
        body.append(
            f'<TextLine ID="line_{index}" HPOS="{x0}" VPOS="{y0}" WIDTH="{x1 - x0}" '
            f'HEIGHT="{y1 - y0}" BASELINE="{x0} {y1 - 8} {x1} {y1 - 8}"><Shape><Polygon '
            f'POINTS="{x0} {y0} {x1} {y0} {x1} {y1} {x0} {y1}"/></Shape>{"".join(children)}</TextLine>'
        )
    reading = ""
    if order is not None:
        refs = "".join(f'<ElementRef ID="o_{n}" REF="line_{i}"/>' for n, i in enumerate(order))
        reading = f'<ReadingOrder><OrderedGroup ID="ro_0">{refs}</OrderedGroup></ReadingOrder>'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#"><Description>'
        f"<sourceImageInformation><fileName>{image}</fileName></sourceImageInformation>"
        f"</Description>{reading}<Layout><Page><PrintSpace>"
        f'<TextBlock ID="block_0">{"".join(body)}</TextBlock>'
        "</PrintSpace></Page></Layout></alto>"
    )


class FakeRunner:
    """Stands in for `subprocess.run`: records each argv and calls `respond(argv)`,
    which writes whatever files the vendor command would and returns (code, stdout)."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        code, stdout = self.respond(list(argv))
        return subprocess.CompletedProcess(argv, code, stdout, "")


def weights_dir(root: Path, artifact: str, files: list[str]) -> Path:
    folder = root / "hf" / artifact
    folder.mkdir(parents=True)
    for name in files:
        (folder / name).write_bytes(b"stand-in")
    return folder


def fake_venv(root: Path, *commands: str) -> Path:
    folder = root / "venv"
    (folder / "bin").mkdir(parents=True)
    for name in ("python", *commands):
        (folder / "bin" / name).write_text("#!/bin/sh\nexit 99\n")
    return folder
