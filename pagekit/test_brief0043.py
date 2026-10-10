"""Tag trust re-runs what it changes, measure skips steps not applied, the
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
    main(["prepare", str(folder), "--output", str(out), "--cache-full"])
    cache = next(path for path in tmp_path.iterdir() if path.name.endswith("pagekit-cache"))
    (sha,) = [path.name for path in cache.iterdir() if path.is_dir()]
    index = json.loads((cache / sha / "index.json").read_text())
    opened = next(e for e in index["entries"] if e["stage"] == "opened")
    with Image.open(cache / sha / opened["files"]["full"]) as image:
        assert image.size == (420, 300)  # turned on open
    distrust = _overrides(
        tmp_path, [{"source": "src/page.tif", "step": "tag_trust", "value": False}]
    )
    main(
        ["prepare", str(folder), "--output", str(out), "--overrides", str(distrust), "--cache-full"]
    )
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


# --- The correction command ---------------------------------------------------------------


def test_the_correction_command_repeats_the_tone_view_and_padding(tmp_path):
    import html
    import os
    import re
    import subprocess

    page = pages.page(size=(400, 560), seed=46, margin=(40, 50, 40, 50))
    folder = tmp_path / "src"
    folder.mkdir()
    page.save(folder / "page.png", dpi=DPI)
    out = tmp_path / "out"
    main(["prepare", str(folder), "--output", str(out), "--tone-view", "--padding", "3mm"])
    review = (out / "review.html").read_text(encoding="utf-8")
    (command,) = re.findall(r'<pre class="command">([^<]+)</pre>', review)
    command = html.unescape(command)
    assert "--tone-view" in command and "--padding 3mm" in command
    (out / "overrides.json").write_text('{"schema": "pagekit-overrides.v1", "overrides": []}')
    before = json.loads((out / "pagekit-prepare.json").read_text())["pages"][0]
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONSAFEPATH"] = "1"
    subprocess.run(command, shell=True, cwd=tmp_path, env=environment, check=False)
    after = json.loads((out / "pagekit-prepare.json").read_text())["pages"][0]
    assert after["tone_view"]["sha256"] == before["tone_view"]["sha256"]
    assert after["output"]["size"] == before["output"]["size"]


# --- The stage cache --------------------------------------------------------------------


def _two_sources(folder: Path) -> None:
    folder.mkdir(parents=True)
    for index in (1, 2):
        pages.page(size=(300, 420), seed=50 + index, margin=(30, 40, 30, 40)).save(
            folder / f"p{index}.png", dpi=DPI
        )


def _cache_files(cache: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(cache)): p.read_bytes() for p in sorted(cache.rglob("*")) if p.is_file()
    }


def test_each_output_folder_has_its_own_cache_and_the_cache_size_is_printed(tmp_path, capsys):
    _two_sources(tmp_path / "src")
    for name in ("first", "second"):
        main(["prepare", str(tmp_path / "src"), "--output", str(tmp_path / name)])
        printed = capsys.readouterr().out
        assert f"stage cache: {tmp_path / (name + '.pagekit-cache')}" in printed
        assert " MB" in printed or " kB" in printed
    assert (tmp_path / "first.pagekit-cache").is_dir() and (
        tmp_path / "second.pagekit-cache"
    ).is_dir()
    assert not (tmp_path / "pagekit-cache").exists()


def test_a_source_that_leaves_the_batch_leaves_the_cache(tmp_path):
    _two_sources(tmp_path / "src")
    out = tmp_path / "out"
    main(["prepare", str(tmp_path / "src"), "--output", str(out)])
    cache = tmp_path / "out.pagekit-cache"
    folders = sorted(p.name for p in cache.iterdir() if p.is_dir())
    assert len(folders) == 2
    (tmp_path / "src" / "p2.png").write_bytes(b"no longer an image")  # skipped now
    main(["prepare", str(tmp_path / "src"), "--output", str(out)])
    assert len([p for p in cache.iterdir() if p.is_dir()]) == 1


def test_a_corrupted_cache_file_is_found_by_its_hash_and_rewritten(tmp_path):
    _two_sources(tmp_path / "src")
    out = tmp_path / "out"
    main(["prepare", str(tmp_path / "src"), "--output", str(out)])
    cache = tmp_path / "out.pagekit-cache"
    good = _cache_files(cache)
    victim = next(cache / name for name in good if name.endswith(".png"))
    data = bytearray(victim.read_bytes())
    data[-10] ^= 0xFF  # one byte flipped
    victim.write_bytes(bytes(data))
    main(["prepare", str(tmp_path / "src"), "--output", str(out)])
    assert _cache_files(cache) == good


def test_a_changed_page_leaves_no_stale_cache_files(tmp_path):
    _two_sources(tmp_path / "src")
    out = tmp_path / "out"
    main(["prepare", str(tmp_path / "src"), "--output", str(out)])
    cache = tmp_path / "out.pagekit-cache"
    before = set(_cache_files(cache))
    fix = _overrides(tmp_path, [{"source": "src/p1.png", "step": "skew", "page": 1, "value": 1.0}])
    main(["prepare", str(tmp_path / "src"), "--output", str(out), "--overrides", str(fix)])
    after = set(_cache_files(cache))
    gone = before - after
    assert gone and all("levelled_p1" in name for name in gone)
    for folder in (p for p in cache.iterdir() if p.is_dir()):
        index = json.loads((folder / "index.json").read_text())
        named = {name for entry in index["entries"] for name in entry["files"].values()}
        assert {p.name for p in folder.iterdir()} == named | {"index.json"}


def test_the_cache_may_not_lie_inside_the_output_folder(tmp_path, capsys):
    _two_sources(tmp_path / "src")
    out = tmp_path / "out"
    status = main(
        ["prepare", str(tmp_path / "src"), "--output", str(out), "--cache", str(out / "c")]
    )
    assert status == 2 and "output folder" in capsys.readouterr().err
    assert not out.exists()


def test_low_disk_space_is_warned_before_the_run(tmp_path, monkeypatch, capsys):
    import shutil as shutil_module
    from collections import namedtuple

    _two_sources(tmp_path / "src")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil_module, "disk_usage", lambda path: usage(10**9, 10**9 - 1000, 1000))
    main(["prepare", str(tmp_path / "src"), "--output", str(tmp_path / "out")])
    error = capsys.readouterr().err
    assert "free" in error and "may need" in error


# --- The tag detection's version guard ----------------------------------------------------


def _simulate(monkeypatch, behaviour: str) -> None:
    """Make Pillow's TIFF reader behave as a later version might."""
    from PIL import ImageOps, TiffImagePlugin

    original = TiffImagePlugin.TiffImageFile.load_end

    if behaviour == "applies, keeps the tag":

        def load_end(self):
            tag = self.tag_v2.get(ORIENTATION)
            original(self)
            if tag is not None:
                self.tag_v2[ORIENTATION] = tag
                self.getexif()[ORIENTATION] = tag

    elif behaviour == "does not apply":

        def load_end(self):
            real = ImageOps.exif_transpose
            monkeypatch.setattr(ImageOps, "exif_transpose", lambda image, **kw: image)
            try:
                original(self)
            finally:
                monkeypatch.setattr(ImageOps, "exif_transpose", real)

    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "load_end", load_end)


@pytest.mark.parametrize(
    ("behaviour", "tag"),
    [("applies, keeps the tag", tag) for tag in range(2, 9)]
    + [("does not apply", tag) for tag in (2, 3, 4)],
)
def test_one_application_whatever_pillow_does_with_a_tiff_tag(
    tmp_path, monkeypatch, behaviour, tag
):
    from pagekit.geometry import TAG_TRANSPOSE

    upright = pages.page(size=(300, 420), seed=47, margin=(30, 40, 30, 40))
    undo = {2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6}[tag]
    stored = upright.transpose(TAG_TRANSPOSE[undo])
    folder = tmp_path / "src"
    folder.mkdir()
    stored.save(folder / "page.tif", "TIFF", dpi=DPI, tiffinfo={ORIENTATION: tag})
    _simulate(monkeypatch, behaviour)
    manifest = execute(plan([folder], tmp_path / "out"))
    (page,) = manifest["pages"]
    expected = "image library on open" if behaviour.startswith("applies") else "chain"
    assert page["orientation_tag"]["applied_by"] == expected
    reference = tmp_path / "ref"
    reference.mkdir()
    upright.save(reference / "page.png", dpi=DPI)
    plain = execute(plan([reference], tmp_path / "ref-out"))
    assert _pixels(tmp_path / "out" / page["output"]["name"]) == _pixels(
        tmp_path / "ref-out" / plain["pages"][0]["output"]["name"]
    )


# --- Tests that catch the surviving mutations ------------------------------------------


def test_strong_blotchy_chroma_noise_on_the_paper_is_not_colour(tmp_path):
    """Catches: dropping or scaling down the MAD term of the paper's noise."""
    import random

    from PIL import ImageChops

    grey = pages.page(size=(600, 800), seed=48, margin=(60, 80, 60, 80))
    rng = random.Random(4)
    small = (grey.width // 3 + 1, grey.height // 3 + 1)
    bands = []
    for _ in range(3):
        noise = Image.new("L", small)
        noise.putdata(
            [max(0, min(255, round(128 + rng.gauss(0, 7)))) for _ in range(small[0] * small[1])]
        )
        noise = noise.resize((small[0] * 3, small[1] * 3), Image.NEAREST).crop((0, 0, *grey.size))
        bands.append(ImageChops.add(grey, noise, 1.0, -128))
    result = _grey_choice(tmp_path, Image.merge("RGB", bands))
    assert result["output_mode"]["mode"] == "grey" and not _flagged(result)


def test_the_batch_checks_leave_out_content_boxes_not_applied(tmp_path):
    """Catches: the batch checks measuring content boxes with cropping off."""
    from pagekit.volume import measure_page

    folder = tmp_path / "src"
    folder.mkdir()
    pages.page(size=(300, 420), seed=49, margin=(30, 40, 30, 40)).save(folder / "p.png", dpi=DPI)
    (default,) = plan([folder], tmp_path / "a").pages
    assert set(measure_page(default)) == {"skew"}
    (cropped,) = plan([folder], tmp_path / "b", settings_overrides={"crop": "content"}).pages
    assert "content_width" in measure_page(cropped)


def test_the_cache_fills_outside_the_side_with_the_pages_paper_colour(tmp_path):
    """Catches: a wrong fill colour in the cache's side and levelled images."""
    upright = Image.new("L", (800, 600), 180)
    upright.paste(235, (400, 0, 800, 600))  # the right page's paper is lighter
    folder = tmp_path / "src"
    folder.mkdir()
    upright.save(folder / "s.png", dpi=DPI)
    fix = _overrides(
        tmp_path,
        [
            {
                "source": "src/s.png",
                "step": "split",
                "value": {"pages": 2, "cut": [[380, 0], [420, 600]]},
            },
            {"source": "src/s.png", "step": "skew", "page": 2, "value": 3.0},
        ],
    )
    out = tmp_path / "out"
    main(["prepare", str(folder), "--output", str(out), "--overrides", str(fix), "--cache-full"])
    manifest = json.loads((out / "pagekit-prepare.json").read_text())
    right = manifest["pages"][1]
    sha = right["source"]["sha256"]
    cache = tmp_path / "out.pagekit-cache" / sha
    index = json.loads((cache / "index.json").read_text())
    for stage in ("side", "levelled"):
        entry = next(e for e in index["entries"] if e["stage"] == stage and e["page"] == 2)
        with Image.open(cache / entry["files"]["full"]) as image:
            # The corner left of the leaning cut is outside the page: paper colour.
            assert (
                image.getpixel((0, image.height - 1)) == right["geometry"]["fill"]["colour"] == 235
            )


def test_a_page_box_set_by_hand_turns_cropping_on_for_its_page(tmp_path):
    """Catches: a hand-set page box not turning cropping on."""
    folder = tmp_path / "src"
    folder.mkdir()
    pages.page(size=(300, 420), seed=50, margin=(30, 40, 30, 40)).save(folder / "p.png", dpi=DPI)
    fix = _overrides(
        tmp_path,
        [{"source": "src/p.png", "step": "page_box", "page": 1, "value": [10, 10, 290, 400]}],
    )
    (page,) = plan([folder], tmp_path / "out", overrides_path=fix).pages
    assert page.applied["crop"] == "page" and page.chain.crop_box == (10, 10, 290, 400)


def test_the_margins_allowance_past_the_page_box_is_not_page_colour(tmp_path):
    """Catches: the colour area not held to the page box (the margin may run past it
    into the backdrop by the allowance)."""
    from pagekit.test_spec0007 import _on_backdrop

    folder = tmp_path / "src"
    folder.mkdir()
    _on_backdrop(False).save(folder / "page.png", dpi=DPI)
    boxes = [
        {"source": "src/page.png", "step": "page_box", "page": 1, "value": [120, 100, 480, 580]},
        {"source": "src/page.png", "step": "content_box", "page": 1, "value": [125, 105, 475, 575]},
    ]
    out = tmp_path / "out"
    main(
        [
            "prepare",
            str(folder),
            "--output",
            str(out),
            "--output-mode",
            "grey",
            "--overrides",
            str(_overrides(tmp_path, boxes)),
        ]
    )
    (page,) = json.loads((out / "pagekit-prepare.json").read_text())["pages"]
    crop = next(step for step in page["geometry"]["steps"] if step["op"] == "crop")["box"]
    assert crop[0] < 120 and crop[2] > 480  # the margin runs into the blue backdrop
    assert page["output_mode"]["mode"] == "grey" and not _flagged(page)
