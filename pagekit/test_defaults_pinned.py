"""Two pins, synthetic pages only.

1. With `--crop content`, no orientation tag, no grey choice and no padding, prepare
   gives the pages and manifest values pinned in testdata/defaults_before_0007.json,
   apart from fields that only record those options.
2. The current default (cropping off, each page its whole levelled side of the
   cut) is pinned in testdata/defaults_0008.json.

The pin holds on every platform. The sources are written here as uncompressed TIFF,
byte for byte the same wherever the test runs, so their sha256 is pinned. The prepared
pages are compared by the sha256 of their decoded pixels: their files are compressed
with zlib, whose output may differ between zlib builds, so the output file's sha256 and
byte size are not pinned."""

from __future__ import annotations

import hashlib
import json
import struct
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main

PINNED = Path(__file__).with_name("testdata") / "defaults_before_0007.json"
PINNED_0008 = Path(__file__).with_name("testdata") / "defaults_0008.json"
# Values that name the bytes of a compressed file, which may differ between builds of
# the compressor; the decoded pixels' sha256 stands for them.
_COMPRESSED = {("output", "sha256"), ("output", "bytes")}


def uncompressed_tiff(image: Image.Image, path: Path, dpi=None) -> None:
    """`image` (L or RGB) as an uncompressed little-endian TIFF, one strip, written
    field by field so its bytes depend on nothing but the pixels and `dpi`."""
    bands = 1 if image.mode == "L" else 3
    width, height = image.size
    raw = image.tobytes()
    fields = [
        (256, 4, [width]),
        (257, 4, [height]),
        (258, 3, [8] * bands),
        (259, 3, [1]),
        (262, 3, [1 if bands == 1 else 2]),
        (273, 4, [8]),
        (277, 3, [bands]),
        (278, 4, [height]),
        (279, 4, [len(raw)]),
    ]
    if dpi:
        for tag, value in ((282, dpi[0]), (283, dpi[1])):
            ratio = Fraction(value).limit_denominator(10000)
            fields.append((tag, 5, [ratio.numerator, ratio.denominator]))
        fields += [(284, 3, [1]), (296, 3, [2])]
    else:
        fields += [(284, 3, [1]), (296, 3, [1])]
    directory = 8 + len(raw) + (len(raw) & 1)
    after = directory + 2 + 12 * len(fields) + 4
    entries, extra = [], b""
    for tag, kind, values in fields:
        count = len(values) // 2 if kind == 5 else len(values)
        packed = struct.pack(f"<{len(values)}{'H' if kind == 3 else 'I'}", *values)
        if len(packed) <= 4:
            entries.append(struct.pack("<HHI", tag, kind, count) + packed.ljust(4, b"\0"))
        else:
            entries.append(struct.pack("<HHII", tag, kind, count, after + len(extra)))
            extra += packed + (b"\0" if len(packed) & 1 else b"")
    data = (
        struct.pack("<2sHI", b"II", 42, directory)
        + raw
        + (b"\0" if len(raw) & 1 else b"")
        + struct.pack("<H", len(fields))
        + b"".join(entries)
        + struct.pack("<I", 0)
        + extra
    )
    path.write_bytes(data)


def build(folder: Path) -> tuple[Path, Path]:
    """Three sources and an overrides file: a grey spread with hand-set values (a
    quarter turn, a leaning cut, a skew, boxes), a colour page with defaults, and a
    grey page with no resolution."""
    source = folder / "src"
    source.mkdir(parents=True)
    spread = pages.turned(pages.spread(seed=4), 3)  # stored a quarter turn off
    ImageDraw.Draw(spread).rectangle((300, 600, 311, 611), fill=10)
    uncompressed_tiff(spread, source / "spread.tif", (150, 150))
    grey = pages.page(size=(400, 560), seed=6, margin=(40, 50, 40, 50))
    colour = Image.merge("RGB", (grey, grey.point(lambda v: round(v * 0.95)), grey))
    uncompressed_tiff(colour, source / "colour.tif", (200, 200))
    nodpi = pages.page(size=(300, 420), seed=7, margin=(30, 40, 30, 40))
    uncompressed_tiff(nodpi, source / "nodpi.tif")
    entries = [
        {"source": "src/spread.tif", "step": "orientation", "value": 1},
        {
            "source": "src/spread.tif",
            "step": "split",
            "value": {"pages": 2, "cut": [[990, 0], [1012, 1399]]},
        },
        {"source": "src/spread.tif", "step": "skew", "page": 1, "value": 1.5},
        {"source": "src/spread.tif", "step": "page_box", "page": 2, "value": [5, 8, 990, 1390]},
        {
            "source": "src/spread.tif",
            "step": "content_box",
            "page": 2,
            "value": [60, 90, 900, 1300],
        },
    ]
    overrides = folder / "fix.json"
    overrides.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    return source, overrides


def run(folder: Path, *extra: str) -> dict:
    source, overrides = build(folder)
    out = folder / "out"
    main(["prepare", str(source), "--output", str(out), "--overrides", str(overrides), *extra])
    manifest = json.loads((out / "pagekit-prepare.json").read_text())
    return {"pages": manifest["pages"], "skipped": manifest["skipped"]}


def _within(pinned, now, where="manifest", parent=None):
    """Every value pinned is in `now`, equal; `now` may hold more keys (new records).
    A compressed file's sha256 and size are skipped: its pixels' sha256 is compared."""
    if isinstance(pinned, dict):
        assert isinstance(now, dict), where
        for key, value in pinned.items():
            if (parent, key) in _COMPRESSED:
                continue
            assert key in now, f"{where}.{key} is missing"
            _within(value, now[key], f"{where}.{key}", key)
    elif isinstance(pinned, list):
        assert isinstance(now, list) and len(now) == len(pinned), where
        for index, (old, new) in enumerate(zip(pinned, now, strict=True)):
            _within(old, new, f"{where}[{index}]", parent)
    elif isinstance(pinned, float) and not isinstance(now, bool):
        # Geometry passes through sin and cos, whose last bit may differ between
        # platforms' maths libraries; anything a reader sees differs far more than this.
        assert now == pytest.approx(pinned, rel=1e-12, abs=1e-9), f"{where}: {now!r} != {pinned!r}"
    else:
        assert now == pinned, f"{where}: {now!r} != {pinned!r}"


def _same(pinned: dict, now: dict, folder: Path) -> None:
    _within(pinned, now)
    # The pages' decoded pixels, read back from the files written.
    out = folder / "out"
    for old, new in zip(pinned["pages"], now["pages"], strict=True):
        assert old["output"]["pixels_sha256"] == new["output"]["pixels_sha256"]
        with Image.open(out / new["output"]["name"]) as page:
            assert hashlib.sha256(page.tobytes()).hexdigest() == old["output"]["pixels_sha256"]
    # The sources' bytes depend on nothing but their pixels, so they are pinned too.
    for page in pinned["pages"]:
        path = folder / "src" / page["source"]["name"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == page["source"]["sha256"]


def test_crop_content_gives_the_pages_and_manifest_values_of_before(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})  # neutral, so only geometry
    _same(json.loads(PINNED.read_text()), run(tmp_path, "--crop", "content"), tmp_path)


def test_the_default_without_cropping_is_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    pinned = json.loads(PINNED_0008.read_text())
    now = run(tmp_path)
    _same(pinned, now, tmp_path)
    # The spread's page 2 has its boxes set by hand, which turns cropping on for it.
    crops = {(p["source"]["name"], p["page"]): p["applied"]["crop"] for p in now["pages"]}
    assert crops == {
        ("colour.tif", 1): "none",
        ("nodpi.tif", 1): "none",
        ("spread.tif", 1): "none",
        ("spread.tif", 2): "content",
    }
