"""The orientation tag, a grey main page, padding apart from the margin, and
the nominal density. Synthetic pages only; no real register material."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main
from pagekit.answer import Answer
from pagekit.geometry import TAG_TRANSPOSE, Chain
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


def save_as(image: Image.Image, path: Path, tag: int | None, carrier: str, dpi=DPI) -> Path:
    """`image` saved as PNG, TIFF or JPEG, with orientation tag `tag` if given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    options: dict = {} if dpi is None else {"dpi": dpi}
    if carrier == "tiff":
        if tag is not None:
            options["tiffinfo"] = {ORIENTATION: tag}
        image.save(path, "TIFF", **options)
    else:
        if tag is not None:
            exif = Image.Exif()
            exif[ORIENTATION] = tag
            options["exif"] = exif
        kind = {"png": "PNG", "jpeg": "JPEG"}[carrier]
        image.save(path, kind, **({"quality": 95} if carrier == "jpeg" else {}), **options)
    return path


def opened(path: Path) -> tuple[Image.Image, bool]:
    """The image as Pillow opens and loads it from the file's bytes, as pagekit reads
    it, and whether Pillow applied its tag. (Pillow 12.3 opening an uncompressed TIFF
    by its path, with a tag of 5 to 8, gives an image sized as stored with turned
    rows; pagekit always opens the bytes it hashed.)"""
    with Image.open(io.BytesIO(path.read_bytes())) as image:
        before = image.getexif().get(ORIENTATION)
        image.load()
        after = image.getexif().get(ORIENTATION)
        return image.copy(), before not in (None, 1) and after in (None, 1)


SUFFIX = {"png": ".png", "tiff": ".tif", "jpeg": ".jpg"}


@pytest.mark.parametrize("carrier", ["png", "tiff", "jpeg"])
@pytest.mark.parametrize("tag", range(1, 9))
def test_each_tag_gives_the_upright_page_once_whoever_applies_it(tmp_path, tag, carrier):
    tagged = save_as(stored(tag), tmp_path / "a" / "src" / f"page{SUFFIX[carrier]}", tag, carrier)
    grid, turned_on_open = opened(tagged)
    # The same page stored upright: for a lossy carrier, the decoded pixels turned by
    # the tag, so both runs start from the same samples.
    upright = grid if turned_on_open else (grid if tag == 1 else grid.transpose(TAG_TRANSPOSE[tag]))
    save(upright, tmp_path / "b" / "src" / "page.png", None)
    frames = []

    def spy(context):
        frames.append(context.frame().tobytes())
        return Answer(0, 1.0, "Spy.", ())

    detectors = {"orientation": Detector("spy/1", spy)}
    a = execute(plan([tagged.parent], tmp_path / "a" / "out", detectors=detectors))
    assert frames == [upright.tobytes()]  # the detector sees the upright frame
    b = execute(plan([tmp_path / "b" / "src"], tmp_path / "b" / "out"))
    (page_a,), (page_b,) = a["pages"], b["pages"]
    out_a = tmp_path / "a" / "out" / page_a["output"]["name"]
    out_b = tmp_path / "b" / "out" / page_b["output"]["name"]
    assert pixels(out_a) == pixels(out_b)  # one application, whoever made it
    record = page_a["orientation_tag"]
    assert (record["found"], record["trusted"], record["applied"]) == (tag, True, tag != 1)
    who = None if tag == 1 else ("image library on open" if turned_on_open else "chain")
    assert record["applied_by"] == who
    chain = Chain.from_dict(page_a["geometry"])
    steps = [step["op"] for step in page_a["geometry"]["steps"]]
    assert ("orientation_tag" in steps) == (who == "chain")
    # source_size is the grid the chain starts from: what Pillow opens.
    assert tuple(page_a["geometry"]["source_size"]) == grid.size
    with Image.open(out_a) as written:
        found = dark_centre(written)
    assert math.dist(chain.inverse([found])[0], dark_centre(grid)) < 1.0
    with Image.open(out_a) as written:
        assert written.getexif().get(ORIENTATION) in (None, 1)


@pytest.mark.parametrize("tag", [3, 6, 2])
def test_an_untrusted_tiff_tag_is_taken_as_stored(tmp_path, tag):
    save_as(stored(tag), tmp_path / "a" / "src" / "page.tif", tag, "tiff")
    save(stored(tag), tmp_path / "b" / "src" / "page.png", None)
    distrust = [{"source": "src/page.tif", "step": "tag_trust", "value": False}]
    a, out_a = prepared(tmp_path / "a", "--overrides", str(overrides(tmp_path / "a", distrust)))
    b, out_b = prepared(tmp_path / "b")
    page = a["pages"][0]
    assert pixels(out_a / page["output"]["name"]) == pixels(out_b / b["pages"][0]["output"]["name"])
    assert tuple(page["geometry"]["source_size"]) == stored(tag).size
    record = page["orientation_tag"]
    assert (record["trusted"], record["applied"], record["grid"]) == (False, False, "stored pixels")
    assert "not trusted" in page["steps"]["orientation"]["evidence"]


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


# --- 2. A grey main page -----------------------------------------------------------


def writing_page(seed: int = 14) -> Image.Image:
    return pages.page(size=(360, 480), seed=seed, margin=(30, 40, 30, 40))


def as_colour(grey: Image.Image) -> Image.Image:
    return Image.merge("RGB", (grey, grey, grey))


def noisy_colour(grey: Image.Image, amount: int, seed: int = 3) -> Image.Image:
    """Colour with sensor-like noise: each channel moved by up to `amount` levels."""
    import random

    rng = random.Random(seed)
    bands = []
    for _ in range(3):
        noise = Image.new("L", grey.size)
        noise.putdata([128 + rng.randint(-amount, amount) for _ in range(grey.width * grey.height)])
        bands.append(ImageChops.add(grey, noise, 1.0, -128))
    return Image.merge("RGB", bands)


def with_colour_marks(kind: str) -> Image.Image:
    page = as_colour(writing_page())
    draw = ImageDraw.Draw(page)
    if kind == "red stamp":
        draw.ellipse((240, 330, 320, 410), outline=(200, 30, 35), width=6)
        draw.line((250, 370, 310, 370), fill=(200, 30, 35), width=5)
    else:  # blue-black annotation: dark strokes with a clear blue cast
        for row in range(3):
            y = 340 + 25 * row
            draw.line((60, y, 200, y + 6), fill=(35, 40, 105), width=4)
    return page


def manifest_page(folder: Path, *extra: str) -> tuple[dict, Path]:
    manifest, out = prepared(folder, *extra)
    (page,) = manifest["pages"]
    return page, out


def test_equal_channels_made_grey_keep_every_intensity_and_say_exact(tmp_path):
    colour = as_colour(writing_page())
    save(colour, tmp_path / "a" / "src" / "page.png", None)
    save(colour, tmp_path / "b" / "src" / "page.png", None)
    grey, out_a = manifest_page(tmp_path / "a", "--output-mode", "grey")
    source, out_b = manifest_page(tmp_path / "b")
    mode = grey["output_mode"]
    assert (mode["mode"], mode["chosen"], mode["set_by"], mode["exact"]) == (
        "grey",
        "grey",
        "run",
        True,
    )
    assert grey["output"]["mode"] == "L" and source["output"]["mode"] == "RGB"
    with (
        Image.open(out_a / grey["output"]["name"]) as a,
        Image.open(out_b / source["output"]["name"]) as b,
    ):
        assert a.tobytes() == b.getchannel(0).tobytes()  # the same geometry, every level kept
    assert not [flag for flag in grey["flags"] if flag["step"] == "output_mode"]


def test_a_near_equal_source_made_grey_says_reviewed(tmp_path):
    save(noisy_colour(writing_page(), 1), tmp_path / "src" / "page.png", None)
    page, out = manifest_page(tmp_path, "--output-mode", "grey")
    mode = page["output_mode"]
    assert (mode["mode"], mode["exact"], mode["rule"]) == ("grey", False, "luminance")
    assert not [flag for flag in page["flags"] if flag["step"] == "output_mode"]
    with Image.open(out / page["output"]["name"]) as written:
        assert written.mode == "L"


def test_sensor_noise_alone_is_made_grey_with_no_flag(tmp_path):
    save(noisy_colour(writing_page(), 4), tmp_path / "src" / "page.png", None)
    page, _ = manifest_page(tmp_path, "--output-mode", "grey")
    assert page["output_mode"]["mode"] == "grey"
    assert page["output_mode"]["colour"]["coloured_mm2"] < 2
    assert not [flag for flag in page["flags"] if flag["step"] == "output_mode"]


@pytest.mark.parametrize("kind", ["red stamp", "blue-black annotation"])
def test_real_colour_is_flagged_and_kept_unless_grey_is_set_by_hand(tmp_path, kind):
    save(with_colour_marks(kind), tmp_path / "a" / "src" / "page.png", None)
    page, out = manifest_page(tmp_path / "a", "--output-mode", "grey")
    assert (page["output_mode"]["mode"], page["output_mode"]["chosen"]) == ("source", "grey")
    assert page["output"]["mode"] == "RGB"
    (reason,) = [flag["reason"] for flag in page["flags"] if flag["step"] == "output_mode"]
    assert "colour that grey would remove" in reason and "kept in colour" in reason
    left, top, right, bottom = page["output_mode"]["colour"]["where"]
    stamp = (240, 330, 320, 410) if kind == "red stamp" else (60, 340, 200, 396)
    chain = Chain.from_dict(page["geometry"])
    (x0, y0), (x1, y1) = chain.forward([stamp[:2], stamp[2:]])
    assert left <= x0 + 6 and top <= y0 + 6 and right >= x1 - 6 and bottom >= y1 - 6
    assert right - left < 220 and bottom - top < 140  # it names where, not the page

    # Set by hand: grey, and the flag stays visible.
    save(with_colour_marks(kind), tmp_path / "b" / "src" / "page.png", None)
    hand = [{"source": "src/page.png", "step": "output_mode", "page": 1, "value": "grey"}]
    page, out = manifest_page(tmp_path / "b", "--overrides", str(overrides(tmp_path / "b", hand)))
    assert (page["output_mode"]["mode"], page["output_mode"]["set_by"]) == ("grey", "manual")
    assert page["output"]["mode"] == "L"
    (reason,) = [flag["reason"] for flag in page["flags"] if flag["step"] == "output_mode"]
    assert "set by hand" in reason


def test_a_batch_choice_covers_only_the_sources_of_that_run(tmp_path):
    folder = tmp_path / "src"
    save(as_colour(writing_page(14)), folder / "first.png", None)
    out = tmp_path / "out"
    assert main(["prepare", str(folder), "--output", str(out), "--output-mode", "grey"]) == 1
    save(as_colour(writing_page(15)), folder / "later.png", None)
    assert main(["prepare", str(folder), "--output", str(out)]) == 1
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    modes = {page["source"]["name"]: page["output_mode"] for page in manifest["pages"]}
    assert (modes["first.png"]["mode"], modes["first.png"]["set_by"]) == ("grey", "run")
    assert (modes["later.png"]["mode"], modes["later.png"]["set_by"]) == ("source", "default")
    project = json.loads((out / "pagekit-project.json").read_text())
    kept = {s["path"]: s["pages"][0].get("output_mode") for s in project["sources"]}
    assert kept["../src/first.png"]["value"] == "grey" and kept["../src/later.png"] is None


def test_the_review_sheet_shows_the_rule_exactness_flag_and_override_lines(tmp_path):
    folder = tmp_path / "src"
    save(as_colour(writing_page(14)), folder / "plain.png", None)
    save(with_colour_marks("red stamp"), folder / "stamp.png", None)
    out = tmp_path / "out"
    main(
        [
            "prepare",
            str(folder),
            "--output",
            str(out),
            "--output-mode",
            "grey",
            "--grey-rule",
            "green",
        ]
    )
    text = (out / REVIEW_NAME).read_text(encoding="utf-8")
    assert "Output mode" in text and "green channel" in text
    assert "exact: every pixel had equal channels" in text
    assert "colour that grey would remove" in text
    for value in ("source", "grey"):
        assert (
            f"&quot;step&quot;: &quot;output_mode&quot;, &quot;page&quot;: 1, &quot;value&quot;: &quot;{value}&quot;"
            in text
        )


# --- 3. Padding apart from the margin ---------------------------------------------------


def _tilted_source(folder: Path, dpi=DPI) -> Path:
    page = upright_page().rotate(-1.5, Image.BICUBIC, fillcolor=pages.PAPER)
    path = folder / "src" / "page.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if dpi is None:
        page.save(path)
    else:
        page.save(path, dpi=dpi)
    return path


SKEW = [{"source": "src/page.png", "step": "skew", "page": 1, "value": 1.5}]


@pytest.mark.parametrize(("padding", "pixels_each"), [("4mm", round(4 * 150 / 25.4)), ("10px", 10)])
def test_padding_enlarges_the_canvas_exactly_and_keeps_the_content(tmp_path, padding, pixels_each):
    for name in ("plain", "padded"):
        _tilted_source(tmp_path / name)
    plain, out_plain = manifest_page(
        tmp_path / "plain", "--overrides", str(overrides(tmp_path / "plain", SKEW))
    )
    padded, out_padded = manifest_page(
        tmp_path / "padded",
        "--overrides",
        str(overrides(tmp_path / "padded", SKEW)),
        "--padding",
        padding,
    )
    p = pixels_each
    with (
        Image.open(out_plain / plain["output"]["name"]) as a,
        Image.open(out_padded / padded["output"]["name"]) as b,
    ):
        assert b.size == (a.size[0] + 2 * p, a.size[1] + 2 * p)
        inner = b.crop((p, p, p + a.size[0], p + a.size[1]))
        assert inner.tobytes() == a.tobytes()  # same content, same scale, same place
        fill = padded["geometry"]["fill"]["colour"]
        bands = [(0, 0, b.size[0], p), (0, b.size[1] - p, b.size[0], b.size[1])]
        bands += [(0, 0, p, b.size[1]), (b.size[0] - p, 0, b.size[0], b.size[1])]
        for box in bands:
            colours = b.crop(box).getcolors()
            assert [colour for _, colour in colours] == [fill if b.mode == "L" else tuple(fill)]
    plain_chain = Chain.from_dict(plain["geometry"])
    chain = Chain.from_dict(padded["geometry"])
    assert chain.scale == plain_chain.scale
    for point in [(40.0, 60.0), (200.5, 300.25), MARK]:
        x, y = plain_chain.forward([point])[0]
        assert chain.forward([point])[0] == pytest.approx((x + p, y + p))
        assert chain.inverse(chain.forward([point]))[0] == pytest.approx(point, abs=1e-6)
    a = padded["geometry"]["affine_output_to_source"]
    x, y = chain.forward([MARK])[0]
    assert (a[0] * x + a[1] * y + a[2], a[3] * x + a[4] * y + a[5]) == pytest.approx(MARK)


def test_the_manifest_records_margin_box_padding_and_regions(tmp_path):
    _tilted_source(tmp_path)
    page, _ = manifest_page(
        tmp_path, "--overrides", str(overrides(tmp_path, SKEW)), "--padding", "10px"
    )
    geometry = page["geometry"]
    chain = Chain.from_dict(geometry)
    regions = geometry["regions"]
    width, height = page["output"]["size"]
    assert regions["canvas"] == [0, 0, width, height]
    assert regions["padding"] == {"left": 10, "top": 10, "right": 10, "bottom": 10}
    assert regions["content"] == [10, 10, width - 10, height - 10]
    photographed = regions["photographed"]
    assert len(photographed) >= 4
    for x, y in photographed:  # inside the content area, and back inside the source
        assert 10 - 1e-6 <= x <= width - 10 + 1e-6 and 10 - 1e-6 <= y <= height - 10 + 1e-6
        sx, sy = chain.inverse([(x, y)])[0]
        assert -1e-6 <= sx <= 300 + 1e-6 and -1e-6 <= sy <= 420 + 1e-6
    assert "fill" in regions and "paper colour" in regions["fill"]
    crop = geometry["margin_box"]["levelled"]
    corners = geometry["margin_box"]["source"]
    levelled = [(crop[0], crop[1]), (crop[2], crop[1]), (crop[2], crop[3]), (crop[0], crop[3])]
    expected = chain.inverse(
        [
            ((x - crop[0]) * chain.scale[0] + 10, (y - crop[1]) * chain.scale[1] + 10)
            for x, y in levelled
        ]
    )
    assert corners == [pytest.approx(list(point)) for point in expected]


def test_padding_in_millimetres_needs_a_resolution(tmp_path):
    _tilted_source(tmp_path, dpi=None)
    page, out = manifest_page(tmp_path, "--padding", "4mm")
    reasons = [flag["reason"] for flag in page["flags"] if flag["step"] == "padding"]
    assert len(reasons) == 1 and "resolution" in reasons[0]
    assert page["geometry"]["regions"]["padding"] == {"left": 0, "top": 0, "right": 0, "bottom": 0}


# --- 4. Density on output ----------------------------------------------------------------


def _density(folder: Path, value, dpi=DPI) -> tuple[int, str]:
    path = folder / "src" / "page.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if dpi is None:
        upright_page().save(path)
    else:
        upright_page().save(path, dpi=dpi)
    entry = [{"source": "src/page.png", "step": "density", "page": 1, "value": value}]
    status = main(
        [
            "prepare",
            str(folder / "src"),
            "--output",
            str(folder / "out"),
            "--overrides",
            str(overrides(folder, entry)),
        ]
    )
    return status


def test_a_nominal_density_keeping_the_axis_ratio_is_written_and_pixels_unchanged(tmp_path):
    assert _density(tmp_path / "a", [600, 600]) == 1  # the neutral defaults are flagged
    save(upright_page(), tmp_path / "b" / "src" / "page.png", None)
    plain, out_b = prepared(tmp_path / "b")
    manifest = json.loads((tmp_path / "a" / "out" / MANIFEST_NAME).read_text())
    (page,) = manifest["pages"]
    assert page["output"]["resolution"] == [600.0, 600.0]
    assert page["density"]["nominal"] == [600.0, 600.0]
    assert page["density"]["accepted"] == pytest.approx([150.0, 150.0], abs=0.05)
    with Image.open(tmp_path / "a" / "out" / page["output"]["name"]) as written:
        assert written.info["dpi"] == pytest.approx((600, 600))
        assert pixels(tmp_path / "a" / "out" / page["output"]["name"]) == pixels(
            out_b / plain["pages"][0]["output"]["name"]
        )
    # Unequal axes keep their own ratio.
    assert _density(tmp_path / "c", [600, 300], dpi=(300, 150)) == 1


@pytest.mark.parametrize(
    ("value", "dpi", "words"),
    [
        ([600, 300], DPI, "ratio"),
        ([600, 600], (300, 150), "ratio"),
        ([600, 600], None, "resolution"),
    ],
)
def test_a_nominal_density_that_changes_the_axis_ratio_is_refused(
    tmp_path, capsys, value, dpi, words
):
    assert _density(tmp_path, value, dpi) == 2
    error = capsys.readouterr().err
    assert "density" in error and words in error
    assert not (tmp_path / "out").exists()


def test_the_same_input_gives_byte_identical_outputs_manifest_and_review(tmp_path):
    def run(folder: Path) -> dict[str, bytes]:
        save(stored(6), folder / "src" / "tagged.png", 6)
        save(with_colour_marks("red stamp"), folder / "src" / "stamp.png", None)
        save(noisy_colour(writing_page(16), 3), folder / "src" / "noisy.png", None)
        out = folder / "out"
        command = ["prepare", str(folder / "src"), "--output", str(out)]
        main([*command, "--output-mode", "grey", "--padding", "3mm"])
        return {
            path.name: path.read_bytes().replace(bytes(out), b"OUT")
            for path in sorted(out.iterdir())
        }

    first = run(tmp_path / "a")
    assert first == run(tmp_path / "b")
    again = run(tmp_path / "a")  # a re-run on its own project
    assert again == first


@pytest.mark.parametrize("carrier", ["tiff", "png"])
def test_the_crop_check_and_the_tone_view_see_the_grid_prepare_starts_from(tmp_path, carrier):
    from pagekit.check import check
    from pagekit.tone import tone_file

    path = save_as(stored(6), tmp_path / "src" / f"page{SUFFIX[carrier]}", 6, carrier)
    manifest, _ = prepared(tmp_path)
    size = manifest["pages"][0]["geometry"]["source_size"]
    width, height = size
    report = check(path, [(0, 0, width, height)])
    assert [
        report["checks"]["resolution"]["width_px"],
        report["checks"]["resolution"]["height_px"],
    ] == size
    view, _ = tone_file(path)
    assert list(view.size) == size


# --- The colour check measures the written page only -------------------


def _grey_flags(page: dict) -> list[str]:
    return [flag["reason"] for flag in page["flags"] if flag["step"] == "output_mode"]


def _on_backdrop(stamp: bool) -> Image.Image:
    """A neutral page on a blue backdrop, with a colour target below the page."""
    frame = Image.new("RGB", (700, 900), (60, 90, 160))
    page = as_colour(writing_page())
    if stamp:
        ImageDraw.Draw(page).ellipse((240, 330, 320, 410), outline=(200, 30, 35), width=6)
    frame.paste(page, (120, 100))
    draw = ImageDraw.Draw(frame)
    for index, colour in enumerate([(220, 40, 40), (40, 180, 60), (40, 60, 210), (230, 210, 40)]):
        draw.rectangle((140 + 100 * index, 640, 220 + 100 * index, 720), fill=colour)
    return frame


BOXES = [
    {"source": "src/page.png", "step": "page_box", "page": 1, "value": [120, 100, 480, 580]},
    {"source": "src/page.png", "step": "content_box", "page": 1, "value": [150, 140, 450, 540]},
]


def test_a_coloured_backdrop_and_target_outside_the_page_are_not_page_colour(tmp_path):
    save(_on_backdrop(False), tmp_path / "src" / "page.png", None)
    page, out = manifest_page(
        tmp_path, "--output-mode", "grey", "--overrides", str(overrides(tmp_path, BOXES))
    )
    assert page["output_mode"]["mode"] == "grey" and _grey_flags(page) == []
    assert page["output_mode"]["colour"]["coloured_mm2"] < 2


def test_a_stamp_on_the_page_is_still_flagged_and_located_on_the_written_page(tmp_path):
    save(_on_backdrop(True), tmp_path / "src" / "page.png", None)
    page, out = manifest_page(
        tmp_path, "--output-mode", "grey", "--overrides", str(overrides(tmp_path, BOXES))
    )
    assert page["output_mode"]["mode"] == "source" and len(_grey_flags(page)) == 1
    left, top, right, bottom = page["output_mode"]["colour"]["where"]
    chain = Chain.from_dict(page["geometry"])
    (x0, y0), (x1, y1) = chain.forward([(360, 430), (440, 510)])  # the stamp in the frame
    assert left <= x0 + 6 and top <= y0 + 6 and right >= x1 - 6 and bottom >= y1 - 6
    assert right - left < 120 and bottom - top < 120


def test_thin_pale_blue_ruling_is_found(tmp_path):
    grey = pages.page(size=(1000, 1300), seed=17, margin=(90, 120, 90, 120))
    page = as_colour(grey)
    draw = ImageDraw.Draw(page)
    for y in range(150, 1200, 95):  # 0.3 mm lines (3.5 px at 300 dpi), 8 mm apart
        draw.line((60, y, 940, y), fill=(185, 205, 235), width=3)
        draw.line((60, y + 3, 940, y + 3), fill=(205, 218, 238), width=1)
    save(page, tmp_path / "src" / "page.png", None)
    (tmp_path / "src" / "page.png").unlink()
    page.save(tmp_path / "src" / "page.png", dpi=(300, 300))
    result, _ = manifest_page(tmp_path, "--output-mode", "grey")
    assert result["output_mode"]["mode"] == "source" and len(_grey_flags(result)) == 1


def test_a_pale_wash_over_part_of_the_paper_does_not_hide_itself(tmp_path):
    page = as_colour(writing_page())
    ImageDraw.Draw(page).rectangle((0, 0, 359, 70), fill=(242, 222, 226))  # 15% of the page
    save(page, tmp_path / "src" / "page.png", None)
    result, _ = manifest_page(tmp_path, "--output-mode", "grey")
    assert result["output_mode"]["mode"] == "source" and len(_grey_flags(result)) == 1


def _specks(grey: Image.Image) -> Image.Image:
    """Isolated coloured specks (dust, hot pixels) on 0.3% of the pixels."""
    import random

    rng = random.Random(5)
    page = as_colour(grey)
    for _ in range(grey.width * grey.height * 3 // 1000):
        x, y = rng.randrange(1, grey.width - 1), rng.randrange(1, grey.height - 1)
        page.putpixel((x, y), (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    return page


def _blotchy(grey: Image.Image, sigma: float = 3.0) -> Image.Image:
    """Chroma noise in blotches three pixels across, as a compressed colour scan has."""
    import random

    rng = random.Random(9)
    small = (grey.width // 3 + 1, grey.height // 3 + 1)
    bands = []
    for _ in range(3):
        noise = Image.new("L", small)
        noise.putdata(
            [max(0, min(255, round(128 + rng.gauss(0, sigma)))) for _ in range(small[0] * small[1])]
        )
        noise = noise.resize((small[0] * 3, small[1] * 3), Image.NEAREST).crop((0, 0, *grey.size))
        bands.append(ImageChops.add(grey, noise, 1.0, -128))
    return Image.merge("RGB", bands)


@pytest.mark.parametrize("noise", ["specks", "blotchy"])
def test_scattered_specks_and_blotchy_chroma_noise_are_not_colour(tmp_path, noise):
    grey = writing_page()
    page = _specks(grey) if noise == "specks" else _blotchy(grey)
    save(page, tmp_path / "src" / "page.png", None)
    result, _ = manifest_page(tmp_path, "--output-mode", "grey")
    assert result["output_mode"]["mode"] == "grey" and _grey_flags(result) == []
