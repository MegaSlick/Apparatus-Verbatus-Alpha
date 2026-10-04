"""With no orientation tag, no grey choice and no padding, prepare gives the same pages,
bytes and manifest values as the code before spec 0007 did (pinned in
testdata/defaults_before_0007.json, made by that code), apart from fields that only
record the new choices. Synthetic pages only."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main

PINNED = Path(__file__).with_name("testdata") / "defaults_before_0007.json"


def build(folder: Path) -> tuple[Path, Path]:
    """Three sources and an overrides file: a grey spread with hand-set values (a
    quarter turn, a leaning cut, a skew, boxes), a colour page with defaults, and a
    grey page with no resolution."""
    source = folder / "src"
    source.mkdir(parents=True)
    spread = pages.turned(pages.spread(seed=4), 3)  # stored a quarter turn off
    ImageDraw.Draw(spread).rectangle((300, 600, 311, 611), fill=10)
    spread.save(source / "spread.png", dpi=(150, 150))
    grey = pages.page(size=(400, 560), seed=6, margin=(40, 50, 40, 50))
    colour = Image.merge("RGB", (grey, grey.point(lambda v: round(v * 0.95)), grey))
    colour.save(source / "colour.png", dpi=(200, 200))
    pages.page(size=(300, 420), seed=7, margin=(30, 40, 30, 40)).save(source / "nodpi.png")
    entries = [
        {"source": "src/spread.png", "step": "orientation", "value": 1},
        {
            "source": "src/spread.png",
            "step": "split",
            "value": {"pages": 2, "cut": [[990, 0], [1012, 1399]]},
        },
        {"source": "src/spread.png", "step": "skew", "page": 1, "value": 1.5},
        {"source": "src/spread.png", "step": "page_box", "page": 2, "value": [5, 8, 990, 1390]},
        {
            "source": "src/spread.png",
            "step": "content_box",
            "page": 2,
            "value": [60, 90, 900, 1300],
        },
    ]
    overrides = folder / "fix.json"
    overrides.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    return source, overrides


def run(folder: Path) -> dict:
    source, overrides = build(folder)
    out = folder / "out"
    main(["prepare", str(source), "--output", str(out), "--overrides", str(overrides)])
    manifest = json.loads((out / "pagekit-prepare.json").read_text())
    return {"pages": manifest["pages"], "skipped": manifest["skipped"]}


def _within(pinned, now, where="manifest"):
    """Every value pinned is in `now`, equal; `now` may hold more keys (new records)."""
    if isinstance(pinned, dict):
        assert isinstance(now, dict), where
        for key, value in pinned.items():
            assert key in now, f"{where}.{key} is missing"
            _within(value, now[key], f"{where}.{key}")
    elif isinstance(pinned, list):
        assert isinstance(now, list) and len(now) == len(pinned), where
        for index, (old, new) in enumerate(zip(pinned, now, strict=True)):
            _within(old, new, f"{where}[{index}]")
    else:
        assert now == pinned, f"{where}: {now!r} != {pinned!r}"


def test_defaults_give_the_pages_and_manifest_values_of_before(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})  # neutral, so only geometry
    pinned = json.loads(PINNED.read_text())
    now = run(tmp_path)
    _within(pinned, now)
    # The page bytes themselves, not only their digests in the manifest.
    for page in pinned["pages"]:
        assert (
            page["output"]["sha256"]
            == now["pages"][pinned["pages"].index(page)]["output"]["sha256"]
        )
