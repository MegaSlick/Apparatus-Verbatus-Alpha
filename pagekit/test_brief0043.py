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


# --- Colour on coloured paper ------------------------------------------------------------

PAPERS = {"white": (246, 245, 242), "cream": (240, 230, 205), "yellow": (236, 222, 172)}


def _paper_page(paper: tuple[int, int, int], size=(1000, 1300)) -> Image.Image:
    """Writing in dark neutral ink on paper of the given colour, at 300 dpi."""
    grey = pages.page(size=size, seed=44, margin=(90, 120, 90, 120))
    bands = []
    for level in paper:
        # Paper (light) takes the paper's level; ink stays dark and neutral.
        bands.append(grey.point(lambda v, level=level: round(v / pages.PAPER * level)))
    return Image.merge("RGB", bands)


def _grey_choice(tmp_path: Path, image: Image.Image) -> dict:
    folder = tmp_path / "src"
    folder.mkdir(parents=True)
    image.save(folder / "page.png", dpi=(300, 300))
    out = tmp_path / "out"
    main(["prepare", str(folder), "--output", str(out), "--output-mode", "grey"])
    (page,) = json.loads((out / "pagekit-prepare.json").read_text())["pages"]
    return page


def _flagged(page: dict) -> bool:
    return any(flag["step"] == "output_mode" for flag in page["flags"])


@pytest.mark.parametrize("paper", sorted(PAPERS))
def test_a_pale_blue_wash_over_part_of_coloured_paper_is_found(tmp_path, paper):
    from PIL import ImageDraw

    page = _paper_page(PAPERS[paper])
    ImageDraw.Draw(page).rectangle((0, 0, 999, 390), fill=(205, 220, 240))  # 30%
    result = _grey_choice(tmp_path, page)
    assert result["output_mode"]["mode"] == "source" and _flagged(result)


@pytest.mark.parametrize("paper", sorted(PAPERS))
@pytest.mark.parametrize("width", [2, 3])  # 0.17 and 0.25 mm at 300 dpi
def test_fine_blue_ruling_on_coloured_paper_is_found(tmp_path, paper, width):
    from PIL import ImageDraw

    page = _paper_page(PAPERS[paper])
    draw = ImageDraw.Draw(page)
    for y in range(150, 1250, 95):
        draw.line((60, y, 940, y), fill=(150, 175, 225), width=width)
    result = _grey_choice(tmp_path, page)
    assert result["output_mode"]["mode"] == "source" and _flagged(result)


@pytest.mark.parametrize("paper", sorted(PAPERS))
def test_plain_coloured_paper_with_neutral_ink_is_made_grey(tmp_path, paper):
    result = _grey_choice(tmp_path, _paper_page(PAPERS[paper]))
    assert result["output_mode"]["mode"] == "grey" and not _flagged(result)


def test_faint_brown_ink_on_yellow_paper_is_made_grey_as_before(tmp_path):
    yellow = PAPERS["yellow"]
    grey = pages.page(size=(1000, 1300), seed=45, margin=(90, 120, 90, 120))
    # Faded brown: the paper's own hue, darker and a little warmer.
    ink = (152, 130, 92)
    span = pages.PAPER - pages.INK_LEVEL
    bands = []
    for paper_level, ink_level in zip(yellow, ink, strict=True):
        bands.append(
            grey.point(
                lambda v, p=paper_level, i=ink_level: round(
                    i + (p - i) * (v - pages.INK_LEVEL) / span
                )
            )
        )
    result = _grey_choice(tmp_path, Image.merge("RGB", bands))
    assert result["output_mode"]["mode"] == "grey" and not _flagged(result)
