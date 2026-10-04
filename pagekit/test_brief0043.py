"""Brief 0043: tag trust re-runs what it changes, measure skips steps not applied, the
colour check on coloured paper, the correction command, the stage cache, and the tag
detection's version guard. Synthetic pages only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main
from pagekit.output import execute
from pagekit.pipeline import DETECTORS
from pagekit.prepare import plan

ORIENTATION = 0x0112
DPI = (150, 150)


def _overrides(folder: Path, entries: list[dict]) -> Path:
    path = folder / "fix.json"
    path.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    return path


def _pixels(path: Path) -> bytes:
    with Image.open(path) as image:
        return image.tobytes()


# --- C1. Changing tag trust re-runs what depends on it ----------------------------------


@pytest.mark.parametrize("carrier", ["tiff", "png"])
def test_changing_tag_trust_re_detects_on_the_new_grid(tmp_path, carrier):
    upright = pages.page(seed=41)
    folder = tmp_path / "src"
    folder.mkdir()
    name = "page.tif" if carrier == "tiff" else "page.png"
    if carrier == "tiff":
        upright.save(folder / name, "TIFF", dpi=DPI, tiffinfo={ORIENTATION: 3})  # a wrong tag
    else:
        exif = Image.Exif()
        exif[ORIENTATION] = 3
        upright.save(folder / name, "PNG", dpi=DPI, exif=exif)
    reference = tmp_path / "ref"
    reference.mkdir()
    upright.save(reference / name.replace(".tif", ".png"), dpi=DPI)
    detectors = {"orientation": DETECTORS["orientation"]}
    out = tmp_path / "out"
    first = execute(plan([folder], out, detectors=detectors))
    assert first["pages"][0]["steps"]["orientation"]["value"] == 2  # the tag's half turn undone
    distrust = _overrides(
        tmp_path, [{"source": f"src/{name}", "step": "tag_trust", "value": False}]
    )
    second = execute(plan([folder], out, overrides_path=distrust, detectors=detectors))
    (page,) = second["pages"]
    assert page["steps"]["orientation"]["value"] == 0  # re-detected on the stored grid
    expected = execute(plan([reference], tmp_path / "ref-out", detectors=detectors))
    assert _pixels(out / page["output"]["name"]) == _pixels(
        tmp_path / "ref-out" / expected["pages"][0]["output"]["name"]
    )


def test_the_opened_cache_entry_follows_the_tag_trust(tmp_path):
    upright = pages.page(size=(300, 420), seed=42, margin=(30, 40, 30, 40))
    folder = tmp_path / "src"
    folder.mkdir()
    upright.save(folder / "page.tif", "TIFF", dpi=DPI, tiffinfo={ORIENTATION: 6})
    out = tmp_path / "out"
    main(["prepare", str(folder), "--output", str(out)])
    cache = next(path for path in tmp_path.iterdir() if path.name.startswith("pagekit-cache"))
    (sha,) = [path.name for path in cache.iterdir() if path.is_dir()]
    index = json.loads((cache / sha / "index.json").read_text())
    opened = next(e for e in index["entries"] if e["stage"] == "opened")
    with Image.open(cache / sha / opened["files"]["full"]) as image:
        assert image.size == (420, 300)  # turned on open
    distrust = _overrides(
        tmp_path, [{"source": "src/page.tif", "step": "tag_trust", "value": False}]
    )
    main(["prepare", str(folder), "--output", str(out), "--overrides", str(distrust)])
    index = json.loads((cache / sha / "index.json").read_text())
    opened = next(e for e in index["entries"] if e["stage"] == "opened")
    with Image.open(cache / sha / opened["files"]["full"]) as image:
        assert image.size == (300, 420)  # the stored pixels


# --- C2. measure skips boxes that were not applied ------------------------------------------


def test_measure_skips_the_content_box_when_cropping_was_off(tmp_path):
    from pagekit.measure import measure

    page = pages.page(size=(600, 840), seed=43, margin=(60, 80, 50, 80))
    folder = tmp_path / "src"
    folder.mkdir()
    page.save(folder / "page.png", dpi=DPI)
    out = tmp_path / "out"
    execute(plan([folder], out))
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "schema": "pagekit-gold.v1",
                "sources": [{"source": "page.png", "content_box": [[60, 80, 550, 760]]}],
            }
        )
    )
    report = measure(out, gold)
    steps = report["steps"]["content_box"]
    assert (steps["right"], steps["wrong"], steps["review"]) == (0, 0, 0)
    assert steps.get("not_applied") == 1
    assert report["wrong_without_flag"] == []
    assert any("cropping was off" in note for note in report["notes"])
