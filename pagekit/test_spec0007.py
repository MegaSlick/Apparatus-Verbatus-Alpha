"""Spec 0007: the orientation tag, a grey main page, padding apart from the margin, and
the nominal density. Synthetic pages only; no real register material."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main
from pagekit.answer import Answer
from pagekit.geometry import Chain
from pagekit.output import MANIFEST_NAME, execute
from pagekit.prepare import Detector, plan
from pagekit.review import REVIEW_NAME

DPI = (150, 150)
MARK = (100.5, 150.5)  # the centre of a dark 9 x 9 square on the upright page
ORIENTATION = 0x0112
# The stored pixels that a tag value turns into the upright page: the inverse of the
# tag's transform, applied to the upright page.
TO_STORED = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


@pytest.fixture(autouse=True)
def _neutral(monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})


def upright_page() -> Image.Image:
    page = pages.page(size=(300, 420), seed=12, margin=(30, 40, 30, 40))
    x, y = MARK
    ImageDraw.Draw(page).rectangle((x - 4.5, y - 4.5, x + 3.5, y + 3.5), fill=5)
    return page


def stored(tag: int) -> Image.Image:
    page = upright_page()
    return page if TO_STORED[tag] is None else page.transpose(TO_STORED[tag])


def save(image: Image.Image, path: Path, tag: int | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if tag is None:
        image.save(path, dpi=DPI)
    else:
        exif = Image.Exif()
        exif[ORIENTATION] = tag
        image.save(path, dpi=DPI, exif=exif)
    return path


def overrides(folder: Path, entries: list[dict]) -> Path:
    path = folder / "fix.json"
    path.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    return path


def prepared(folder: Path, *extra: str) -> tuple[dict, Path]:
    out = folder / "out"
    main(["prepare", str(folder / "src"), "--output", str(out), *extra])
    return json.loads((out / MANIFEST_NAME).read_text()), out


def pixels(path: Path) -> tuple[str, tuple[int, int], bytes]:
    with Image.open(path) as image:
        return image.mode, image.size, image.tobytes()


def dark_centre(image: Image.Image) -> tuple[float, float]:
    grey = image.convert("L")
    width = grey.size[0]
    total = sx = sy = 0.0
    for index, level in enumerate(grey.tobytes()):
        if level < 30:
            total += 1
            sx += index % width + 0.5
            sy += index // width + 0.5
    return sx / total, sy / total


# --- 1. The orientation tag ------------------------------------------------------------


@pytest.mark.parametrize("tag", range(1, 9))
def test_each_tag_gives_the_upright_page_and_maps_the_mark_back(tmp_path, tag):
    tagged = save(stored(tag), tmp_path / "a" / "src" / "page.png", tag)
    save(upright_page(), tmp_path / "b" / "src" / "page.png", None)
    frames = []

    def spy(context):
        frames.append(context.frame().tobytes())
        return Answer(0, 1.0, "Spy.", ())

    detectors = {"orientation": Detector("spy/1", spy)}
    a = execute(plan([tagged.parent], tmp_path / "a" / "out", detectors=detectors))
    assert frames == [upright_page().tobytes()]  # the detector sees the upright frame
    b = execute(plan([tmp_path / "b" / "src"], tmp_path / "b" / "out"))
    (page_a,), (page_b,) = a["pages"], b["pages"]
    out_a = tmp_path / "a" / "out" / page_a["output"]["name"]
    out_b = tmp_path / "b" / "out" / page_b["output"]["name"]
    assert pixels(out_a) == pixels(out_b)
    record = page_a["orientation_tag"]
    assert (record["found"], record["trusted"], record["applied"]) == (tag, True, tag != 1)
    # The mark maps back to where it is stored.
    chain = Chain.from_dict(page_a["geometry"])
    with Image.open(out_a) as written:
        found = dark_centre(written)
    assert math.dist(chain.inverse([found])[0], dark_centre(stored(tag))) < 0.5
    assert math.dist(chain.forward([dark_centre(stored(tag))])[0], found) < 0.5
    # The output carries no tag that would turn it again.
    with Image.open(out_a) as written:
        assert written.getexif().get(ORIENTATION) in (None, 1)


def test_a_png_output_carries_no_tag_either(tmp_path):
    save(stored(6), tmp_path / "src" / "page.png", 6)
    manifest, out = prepared(tmp_path, "--format", "png")
    with Image.open(out / manifest["pages"][0]["output"]["name"]) as written:
        assert written.getexif().get(ORIENTATION) in (None, 1)
        assert "exif" not in written.info


def test_a_quarter_turn_set_by_hand_comes_after_the_tag(tmp_path):
    save(stored(6), tmp_path / "a" / "src" / "page.png", 6)
    save(upright_page(), tmp_path / "b" / "src" / "page.png", None)
    turn = [{"source": "src/page.png", "step": "orientation", "value": 1}]
    a, out_a = prepared(tmp_path / "a", "--overrides", str(overrides(tmp_path / "a", turn)))
    b, out_b = prepared(tmp_path / "b", "--overrides", str(overrides(tmp_path / "b", turn)))
    name = a["pages"][0]["output"]["name"]
    assert pixels(out_a / name) == pixels(out_b / name)
    with Image.open(out_b / name) as written:
        assert written.size[0] > written.size[1]  # upright, then turned a quarter


def test_an_invalid_tag_is_flagged_and_the_source_taken_as_stored(tmp_path):
    save(stored(6), tmp_path / "a" / "src" / "page.png", 9)
    save(stored(6), tmp_path / "b" / "src" / "page.png", None)
    a, out_a = prepared(tmp_path / "a")
    b, out_b = prepared(tmp_path / "b")
    page = a["pages"][0]
    assert pixels(out_a / page["output"]["name"]) == pixels(out_b / page["output"]["name"])
    reasons = [flag["reason"] for flag in page["flags"] if flag["step"] == "orientation_tag"]
    assert len(reasons) == 1 and "9" in reasons[0] and "as stored" in reasons[0]
    assert (page["orientation_tag"]["found"], page["orientation_tag"]["applied"]) == (9, False)


def test_an_untrusted_tag_is_ignored_and_the_evidence_says_so(tmp_path):
    save(stored(6), tmp_path / "a" / "src" / "page.png", 6)
    save(stored(6), tmp_path / "b" / "src" / "page.png", None)
    distrust = [{"source": "src/page.png", "step": "tag_trust", "value": False}]
    a, out_a = prepared(tmp_path / "a", "--overrides", str(overrides(tmp_path / "a", distrust)))
    b, out_b = prepared(tmp_path / "b")
    page = a["pages"][0]
    assert pixels(out_a / page["output"]["name"]) == pixels(out_b / page["output"]["name"])
    assert (page["orientation_tag"]["trusted"], page["orientation_tag"]["applied"]) == (
        False,
        False,
    )
    assert "not trusted" in page["steps"]["orientation"]["evidence"]
    assert not [flag for flag in page["flags"] if flag["step"] == "orientation_tag"]
    # Kept on the next run like any correction; the review sheet says it too.
    again, _ = prepared(tmp_path / "a")
    assert again["pages"][0]["orientation_tag"]["trusted"] is False
    assert "not trusted" in (tmp_path / "a" / "out" / REVIEW_NAME).read_text(encoding="utf-8")
