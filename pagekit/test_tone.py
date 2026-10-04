"""The grey tone view on synthetic pages drawn here; no real register material.

Each page is a paper reflectance (with grain) carrying strokes of known levels, lit by a
strong horizontal ramp (a gutter shadow on the left) and marked by a stain with a sharp
edge: observed = reflectance * illumination. The truth that the measures use (where
paper, strokes, hairlines and the stain lie) is kept beside the image.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import PIL
import pytest
from PIL import Image, ImageChops, ImageDraw

from pagekit.__main__ import main
from pagekit.tone import (
    FINE,
    HEADROOM,
    SCHEMA,
    SETTINGS_PATH,
    TONE_SHA256,
    ToneError,
    closing,
    lift_curve,
    lift_lut,
    load_settings,
    record_json,
    tiff_bytes,
    to_grey,
    tone,
    tone_file,
    window_max,
    window_min,
    write_view,
)

SIZE = (1200, 1600)
Box = tuple[int, int, int, int]

# Reflectance levels per channel. Grey uses one channel; colour is brown ink on yellow
# paper, where the faint ink is close to the paper in luminance but far in blue.
GREY_LEVELS = {"paper": 225, "dark": 30, "faint": 217, "hairline": 150}
COLOUR_LEVELS = (
    {"paper": 230, "dark": 110, "faint": 222, "hairline": 190},
    {"paper": 215, "dark": 70, "faint": 206, "hairline": 150},
    {"paper": 170, "dark": 30, "faint": 128, "hairline": 90},
)
STAIN: Box = (700, 1150, 1000, 1450)  # inside paper-only ground; sharp edges
STAIRCASE_STEP = 4


@dataclass
class Truth:
    paper: list[Box] = field(default_factory=list)  # pure paper, spread over the lighting
    dark: list[Box] = field(default_factory=list)
    faint: list[Box] = field(default_factory=list)
    hairlines: list[tuple[int, int, int]] = field(default_factory=list)  # x0, x1, y
    staircase: list[list[Box]] = field(default_factory=list)  # runs of falling levels
    dip: tuple[Box, tuple[int, int]] | None = None  # a stroke and its unique darkest pixel


def _grain(size: tuple[int, int], level: int, seed: int) -> Image.Image:
    """Paper at `level` with uniform grain of -4..4, deterministic."""
    table = bytes(max(0, min(255, level + (b % 9) - 4)) for b in range(256))
    return Image.frombytes(
        "L", size, random.Random(seed).randbytes(size[0] * size[1]).translate(table)
    )


def _ramp(size: tuple[int, int]) -> Image.Image:
    """Illumination from 0.55 on the left to 1.0 on the right, as 140..255."""
    gradient = Image.linear_gradient("L").rotate(90).resize(size, Image.BILINEAR)
    return gradient.point(lambda level: 140 + round(level * 115 / 255))


def _channel(
    levels: dict[str, int], seed: int, truth: Truth, record_truth: bool, lit: bool, stain: bool
) -> Image.Image:
    width, height = SIZE
    paper = levels["paper"]
    image = _grain(SIZE, paper, seed)
    draw = ImageDraw.Draw(image)
    # Dark strokes in rows across the whole lighting range; paper boxes between the rows.
    for y in range(200, 560, 60):
        for x in range(100, 1100, 150):
            box = (x, y, x + 60, y + 10)
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=levels["dark"])
            if record_truth:
                truth.dark.append(box)
    # Faint strokes, a few levels below the paper.
    for y in range(600, 760, 40):
        for x in range(100, 1100, 150):
            box = (x, y, x + 60, y + 8)
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=levels["faint"])
            if record_truth:
                truth.faint.append(box)
    # Hairlines one pixel wide.
    for y in range(800, 900, 20):
        for x in range(100, 1100, 150):
            draw.line((x, y, x + 79, y), fill=levels["hairline"], width=1)
            if record_truth:
                truth.hairlines.append((x, x + 80, y))
    # A stroke with a unique darkest pixel at its centre.
    dip_box = (560, 950, 620, 960)
    draw.rectangle((560, 950, 619, 959), fill=40)
    draw.point((590, 955), fill=25)
    # Staircases of falling levels, spaced STAIRCASE_STEP apart: short strokes with
    # paper between them, one run from the shadow to the middle and one from the middle
    # to the light, drawn flat so only the view's own merging can join two steps.
    for x0, y in ((60, 1000), (500, 1040)):
        boxes = []
        level = paper - STAIRCASE_STEP
        x = x0
        while level >= 20 and x + 40 <= 1180:
            box = (x, y, x + 40, y + 8)
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=level)
            boxes.append(box)
            level -= STAIRCASE_STEP
            x += 60
        if record_truth:
            truth.staircase.append(boxes)
    if record_truth:
        truth.dip = (dip_box, (590, 955))
        truth.paper = [
            (20, 20, 140, 180),
            (560, 20, 680, 180),
            (1060, 20, 1180, 180),
            (20, 1480, 140, 1580),
            (560, 1480, 680, 1580),
            (1060, 1480, 1180, 1580),
            (760, 1200, 940, 1400),  # inside the stain
            (300, 1200, 600, 1400),  # beside the stain
        ]
    if stain:
        stained = image.crop(STAIN).point(lambda level: round(level * 0.85))
        image.paste(stained, STAIN)
    if lit:
        image = ImageChops.multiply(image, _ramp(SIZE))
    return image


def synthetic(
    colour: bool = False, lit: bool = True, stain: bool = True
) -> tuple[Image.Image, Truth]:
    truth = Truth()
    if not colour:
        return _channel(GREY_LEVELS, 0, truth, True, lit, stain), truth
    channels = [
        _channel(levels, seed, truth, seed == 0, lit, stain)
        for seed, levels in enumerate(COLOUR_LEVELS)
    ]
    return Image.merge("RGB", channels), truth


# -- measures -------------------------------------------------------------------------


def _mean(image: Image.Image, box: Box) -> float:
    histogram = image.crop(box).histogram()
    total = sum(histogram)
    return sum(level * count for level, count in enumerate(histogram)) / total


def _std(image: Image.Image, box: Box) -> float:
    return statistics.pstdev(image.crop(box).get_flattened_data())


def _above(box: Box, gap: int = 4, height: int = 10) -> Box:
    """A paper box just above a stroke, the same width."""
    return box[0], box[1] - gap - height, box[2], box[1] - gap


def _paper_spread(image: Image.Image, truth: Truth) -> float:
    means = [_mean(image, box) for box in truth.paper]
    return max(means) - min(means)


def _faint_measures(image: Image.Image, truth: Truth) -> tuple[float, float]:
    """Mean faint-stroke contrast against the paper above it, and that paper's grain."""
    contrasts, noises = [], []
    for box in truth.faint:
        paper = _above(box)
        contrasts.append(_mean(image, paper) - _mean(image, box))
        noises.append(_std(image, paper))
    return statistics.mean(contrasts), statistics.mean(noises)


def _darkest(image: Image.Image, box: Box) -> tuple[int, int]:
    crop = image.crop(box)
    pixels = crop.load()
    positions = [
        (x, y)
        for y in range(crop.size[1])
        for x in range(crop.size[0])
        if pixels[x, y] == crop.getextrema()[0]
    ]
    assert len(positions) == 1, "the darkest pixel is not unique"
    return positions[0][0] + box[0], positions[0][1] + box[1]


def _grey_of(image: Image.Image) -> Image.Image:
    return image if image.mode == "L" else image.convert("L")


# -- the measures the spec lists --------------------------------------------------------


@pytest.mark.parametrize("colour", [False, True], ids=["grey", "colour"])
def test_paper_evenness_improves_sharply(colour):
    page, truth = synthetic(colour)
    view, record = tone(page)
    before = _paper_spread(_grey_of(page), truth)
    after = _paper_spread(view, truth)
    assert before > 80  # the ramp and the stain make the paper very uneven
    assert after <= before / 8
    assert all(abs(_mean(view, box) - 235) <= 5 for box in truth.paper)
    assert record["flatten"]["applied"] is True
    assert record["flatten"]["paper_spread_after"] <= record["flatten"]["paper_spread_before"] / 8
    assert abs(record["flatten"]["paper_level_after"] - 235) <= 2


@pytest.mark.parametrize("colour", [False, True], ids=["grey", "colour"])
def test_faint_ink_contrast_and_contrast_to_noise_rise(colour):
    page, truth = synthetic(colour)
    view, _ = tone(page)
    contrast_before, noise_before = _faint_measures(_grey_of(page), truth)
    contrast_after, noise_after = _faint_measures(view, truth)
    assert contrast_before > 0
    assert contrast_after >= 1.4 * contrast_before
    assert contrast_after / noise_after >= 1.05 * contrast_before / noise_before


@pytest.mark.parametrize("colour", [False, True], ids=["grey", "colour"])
def test_no_stroke_is_lost_and_no_levels_merge(colour):
    page, truth = synthetic(colour)
    grey = _grey_of(page)
    view, _ = tone(page)
    for box in truth.dark + truth.faint:
        assert _mean(grey, box) < _mean(grey, _above(box))
        assert _mean(view, box) < _mean(view, _above(box)) - 4
    for x0, x1, y in truth.hairlines:
        line = view.crop((x0, y, x1, y + 1))
        above = _mean(view, (x0, y - 4, x1, y - 3))
        below = _mean(view, (x0, y + 3, x1, y + 4))
        assert line.getextrema()[1] < min(above, below) - 10, (
            "a hairline pixel faded into the paper"
        )
    for run in truth.staircase:
        levels = [round(_mean(view, box)) for box in run]
        assert levels == sorted(levels, reverse=True)
        assert len(set(levels)) == len(levels), "two input levels below the paper merged"


@pytest.mark.parametrize("colour", [False, True], ids=["grey", "colour"])
def test_nothing_moves(colour):
    page, truth = synthetic(colour)
    view, _ = tone(page)
    assert view.size == page.size and view.mode == "L"
    box, position = truth.dip
    assert _darkest(_grey_of(page), box) == position
    assert _darkest(view, box) == position
    # Every dark stroke keeps its footprint exactly.
    for stroke in truth.dark:
        around = (stroke[0] - 5, stroke[1] - 5, stroke[2] + 5, stroke[3] + 5)
        ink = view.crop(around).point(lambda level: 255 if level < 120 else 0)
        x0, y0, x1, y1 = ink.getbbox()
        assert (x0 + around[0], y0 + around[1], x1 + around[0], y1 + around[1]) == stroke


def test_an_even_page_with_dark_ink_changes_only_a_little():
    page, _ = synthetic(lit=False, stain=False)
    view, record = tone(page, {"paper_level": 225})
    difference = ImageChops.difference(page, view).histogram()
    mean_change = sum(level * count for level, count in enumerate(difference)) / sum(difference)
    assert mean_change <= 3
    assert record["flatten"]["applied"] is True


def test_a_page_almost_covered_in_ink_skips_flattening_and_says_so():
    page = _grain(SIZE, 225, 0)
    ImageDraw.Draw(page).rectangle((0, 0, 1199, 1399), fill=35)  # 87.5% ink
    page = ImageChops.multiply(page, _ramp(SIZE))
    view, record = tone(page)
    assert view.size == page.size
    flatten = record["flatten"]
    assert flatten["applied"] is False
    assert flatten["paper_share"] < 0.25
    assert "background estimate" in flatten["reason"]
    assert "flatten:skipped" in record["steps"]
    assert flatten["paper_level_after"] is None
    # Without flattening the lift is anchored at the paper the page actually has.
    assert record["lift"]["anchor_source"] == "measured paper class"
    assert 100 <= record["lift"]["anchor_level"] <= 230
    # The ink is still ink and the paper still paper.
    assert _mean(view, (0, 0, 1200, 1400)) < _mean(view, (0, 1420, 1200, 1600))


def test_a_sharp_stain_edge_leaves_no_wide_halo_and_no_white():
    page, _ = synthetic()
    view, record = tone(page)
    # Bright paper leaks half the stroke window (40 px at the default 81) into the stain
    # at its edge; on a lighting gradient the leaked plateau tails off over the second
    # half of the window. So the paper is within 8 levels of the set level beyond 40 px
    # and within 5 beyond 60 px, on every row and column of the stain and of the 100 px
    # of bare paper around it, and nothing is driven to white. The distances are fixed,
    # not read from the settings, so a wider window or a smeared estimate fails here.
    x0, y0, x1, y1 = STAIN

    def within(level: float, distance: int) -> bool:
        return abs(level - 235) <= (8 if distance <= 60 else 5)

    for x in range(x0 - 100, x1 + 100, 2):
        distance = min(abs(x - x0), abs(x - x1))
        if distance <= 40:
            continue
        for y_a, y_b in ((y0 - 100, y0 - 60), (y0 + 60, y1 - 60), (y1 + 60, y1 + 100)):
            column = _mean(view, (x, y_a, x + 1, y_b))
            assert within(column, distance), f"column {x} rows {y_a}..{y_b} sits at {column:.1f}"
    for y in range(y0 - 100, y1 + 100, 2):
        distance = min(abs(y - y0), abs(y - y1))
        if distance <= 40:
            continue
        for x_a, x_b in ((x0 - 100, x0 - 60), (x0 + 60, x1 - 60), (x1 + 60, x1 + 100)):
            row = _mean(view, (x_a, y, x_b, y + 1))
            assert within(row, distance), f"row {y} columns {x_a}..{x_b} sits at {row:.1f}"
    assert view.histogram()[255] == 0, "something was driven to white"
    edges = record["flatten"]["background_edges"]
    assert edges["edge_share"] > 0 and edges["band_share"] > edges["edge_share"]


def test_the_minimum_channel_separates_brown_ink_from_yellow_paper_better():
    page, truth = synthetic(colour=True)
    luminance, record_l = tone(page, {"grey_rule": "luminance"})
    minimum, record_m = tone(page, {"grey_rule": "min"})
    blue, _ = tone(page, {"grey_rule": "blue"})
    contrast_l, _ = _faint_measures(luminance, truth)
    contrast_m, _ = _faint_measures(minimum, truth)
    contrast_b, _ = _faint_measures(blue, truth)
    assert contrast_m >= 2 * contrast_l
    assert contrast_b >= 2 * contrast_l
    assert record_l["grey"]["rule"] == "luminance" and record_l["grey"]["applied"] == "luminance"
    assert record_l["grey"]["source_mode"] == "RGB"
    assert record_m["grey"]["applied"] == "min" and record_m["steps"][0] == "grey:min"
    assert record_l["settings_sha256"] != record_m["settings_sha256"]


def test_the_view_is_deterministic():
    page, _ = synthetic(colour=True)
    first, record_a = tone(page)
    second, record_b = tone(page)
    assert first.tobytes() == second.tobytes()
    assert record_json(record_a) == record_json(record_b)


def test_a_full_size_page_takes_a_few_seconds():
    page = Image.new("RGB", (3000, 4500), (230, 215, 170))
    draw = ImageDraw.Draw(page)
    for y in range(300, 4200, 90):
        for x in range(300, 2700, 200):
            draw.rectangle((x, y, x + 150, y + 40), fill=(110, 70, 30))
    page = ImageChops.multiply(page, _ramp(page.size).convert("RGB"))
    started = time.perf_counter()
    view, record = tone(page)
    elapsed = time.perf_counter() - started
    assert view.size == (3000, 4500)
    assert record["flatten"]["applied"] is True and record["flatten"]["reduce_factor"] == 6
    assert elapsed < 8, f"{elapsed:.1f} s"


# -- the curve and the filters ----------------------------------------------------------


@pytest.mark.parametrize(
    "anchor_level, strength, shape", [(235, 0.2, 3), (200, 0.4, 1), (254, 0.4, 8), (235, 0.1, 5)]
)
@pytest.mark.parametrize("top", [1.0, HEADROOM])
def test_the_lift_curve_is_strictly_increasing_and_anchored(anchor_level, strength, shape, top):
    anchor = anchor_level / 255
    points = [top * index / 8000 for index in range(8001)]
    values = [lift_curve(x, anchor, strength, shape, top) for x in points]
    assert all(b > a for a, b in zip(values, values[1:], strict=False))
    assert values[0] == 0 and abs(values[-1] - 1) < 1e-9
    assert abs(lift_curve(anchor, anchor, strength, shape, top) - anchor) < 1e-12
    # Below the paper the curve darkens; the darkening peaks nearer the paper than black.
    below = [(x, x - f) for x, f in zip(points, values, strict=True) if 0 < x < anchor]
    assert all(darkening > 0 for _, darkening in below)
    peak = max(below, key=lambda pair: pair[1])[0]
    assert peak >= anchor / 2 - 0.01  # shape 1 peaks in the middle, higher shapes nearer the paper
    # The two halves meet with the same slope.
    step = 1e-6
    slope_below = (anchor - lift_curve(anchor - step, anchor, strength, shape, top)) / step
    slope_above = (lift_curve(anchor + step, anchor, strength, shape, top) - anchor) / step
    assert abs(slope_below - slope_above) <= 0.01 * slope_below
    lut = lift_lut(anchor_level, strength, shape, top)
    assert len(lut) == FINE * 256 * HEADROOM and lut[0] == 0 and lut[-1] == 255
    assert all(b >= a for a, b in zip(lut, lut[1:], strict=False))
    assert lut[anchor_level * FINE] == anchor_level


def test_values_above_the_paper_map_strictly_upward_without_clipping():
    # With the headroom of the flattened path, a value at white maps below white and
    # everything up to twice white keeps its order; white is reached only at the top.
    lut = lift_lut(235, 0.2, 3, HEADROOM)
    white = lut[255 * FINE]
    assert 235 < white < 255
    assert lut[int(1.5 * 255 * FINE)] > white
    assert lut[-1] == 255
    above = [lift_curve(1 + k / 100, 235 / 255, 0.2, 3, HEADROOM) for k in range(101)]
    assert all(b > a for a, b in zip(above, above[1:], strict=False))
    # On a page: pixels brighter than their paper end brighter than the paper, not white,
    # and two patches whose bright pixels both pass white once flattened keep their
    # order instead of meeting at one clipped level. (A bright patch wider than a few
    # pixels is paper to the estimate, so the patches are fine checkerboards, which the
    # reduced copy averages.)
    page = _grain(SIZE, 200, 0)
    for x0, light, dark in ((300, 255, 150), (500, 230, 170)):
        for y in range(1480, 1520):
            for x in range(x0, x0 + 40):
                page.putpixel((x, y), light if (x + y) % 2 else dark)
    view, _ = tone(page)
    bright = view.crop((300, 1480, 340, 1520)).getextrema()[1]
    less = view.crop((500, 1480, 540, 1520)).getextrema()[1]
    assert 237 <= less < bright <= 254
    assert view.histogram()[255] == 0


def test_a_lift_of_zero_is_the_identity():
    lut = lift_lut(235, 0, 3, 1.0)
    assert all(lut[level * FINE] == level for level in range(256))
    assert all(lut[fine] in (fine // FINE, -(-fine // FINE)) for fine in range(256 * FINE))
    _, record = tone(synthetic(lit=False, stain=False)[0], {"lift_strength": 0})
    assert record["lift"]["applied"] is False


def test_window_filters_match_a_brute_force_neighbourhood():
    width, height = 13, 9
    generator = random.Random(1)
    source = Image.frombytes(
        "L", (width, height), bytes(generator.randrange(256) for _ in range(width * height))
    )
    pixels = source.load()
    for window in (3, 5, 7):
        reach = window // 2
        largest, smallest = window_max(source, window), window_min(source, window)
        for y in range(height):
            for x in range(width):
                around = [
                    pixels[xx, yy]
                    for xx in range(max(0, x - reach), min(width, x + reach + 1))
                    for yy in range(max(0, y - reach), min(height, y + reach + 1))
                ]
                assert largest.getpixel((x, y)) == max(around)
                assert smallest.getpixel((x, y)) == min(around)


def test_closing_removes_strokes_and_keeps_the_paper_level():
    page = Image.new("L", (80, 80), 200)
    draw = ImageDraw.Draw(page)
    draw.rectangle((30, 20, 34, 60), fill=20)
    draw.rectangle((0, 0, 79, 2), fill=90)  # a dark band at the border
    assert closing(page, 11).getextrema() == (200, 200)


def test_grey_rules_and_source_modes():
    colour = Image.new("RGB", (4, 4), (200, 100, 50))
    assert to_grey(colour, "min")[0].getpixel((0, 0)) == 50
    assert to_grey(colour, "red")[0].getpixel((0, 0)) == 200
    assert to_grey(colour, "green")[0].getpixel((0, 0)) == 100
    assert to_grey(colour, "blue")[0].getpixel((0, 0)) == 50
    assert to_grey(colour, "luminance")[1] == "luminance"
    for mode in ("1", "L", "LA"):
        grey, applied = to_grey(Image.new(mode, (4, 4)), "min")
        assert grey.mode == "L" and applied == "source-grey"
    for mode in ("P", "RGBA", "CMYK", "YCbCr"):
        view, record = tone(Image.new(mode, (64, 64)))
        assert view.mode == "L" and record["grey"]["source_mode"] == mode


def test_settings_are_validated():
    assert load_settings()["grey_rule"]["value"] == "luminance"
    assert load_settings({"lift_strength": 0.3})["lift_strength"]["source"] == "override"
    for bad in (
        {"nonsense": 1},
        {"grey_rule": "cyan"},
        {"grey_rule": 3},
        {"lift_strength": 0.9},
        {"lift_strength": True},
        {"lift_shape": 0},
        {"paper_level": 255},
        {"min_paper_share": 0},
        {"sharpen": 2},
    ):
        with pytest.raises(ToneError):
            load_settings(bad)


def test_sharpening_is_off_by_default_and_recorded_when_on():
    page, _ = synthetic()
    _, plain = tone(page)
    sharpened, record = tone(page, {"sharpen": 1})
    assert plain["sharpen"]["applied"] is False and "sharpen" not in plain["steps"]
    assert record["sharpen"]["applied"] is True and record["steps"][-1] == "sharpen"
    assert sharpened.size == page.size


def test_the_record_is_closed_and_canonical():
    page, _ = synthetic()
    _, record = tone(page)
    assert record["schema"] == SCHEMA
    assert set(record) == {
        "schema", "tool", "input", "view", "steps", "grey", "flatten", "lift", "sharpen",
        "settings", "settings_sha256", "settings_measured", "settings_note",
    }  # fmt: skip
    assert record["tool"]["pillow"] == PIL.__version__
    assert record["tool"]["tone_sha256"] == TONE_SHA256
    assert (
        TONE_SHA256 == hashlib.sha256((Path(__file__).parent / "tone.py").read_bytes()).hexdigest()
    )
    assert record["view"]["pixels_sha256"] == hashlib.sha256(tone(page)[0].tobytes()).hexdigest()
    assert record["settings_measured"] is False
    assert json.loads(record_json(record)) == record


# -- files and the command ---------------------------------------------------------------


def test_the_command_writes_a_lossless_tiff_and_prints_the_record(tmp_path, capsys):
    page, _ = synthetic(colour=True)
    source = tmp_path / "page.png"
    page.save(source, dpi=(300, 300))
    out = tmp_path / "view.tif"
    assert main(["tone", "--in", str(source), "--out", str(out), "--grey-rule", "min"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["input"]["name"] == "page.png" and record["input"]["dpi"] == [300.0, 300.0]
    assert record["grey"]["applied"] == "min"
    assert (
        record["output"]["name"] == "view.tif" and record["output"]["bytes"] == out.stat().st_size
    )
    expected, _ = tone(page, {"grey_rule": "min"})
    with Image.open(out) as written:
        assert written.mode == "L" and written.size == page.size
        assert written.info["compression"] == "tiff_adobe_deflate"
        assert written.info["dpi"] == (300.0, 300.0)
        assert written.tobytes() == expected.tobytes()
    # The same page and settings give the same bytes on disk.
    again = tmp_path / "again.tif"
    assert main(["tone", "--in", str(source), "--out", str(again), "--grey-rule", "min"]) == 0
    assert again.read_bytes() == out.read_bytes()


def test_the_command_refuses_what_it_cannot_do(tmp_path, capsys):
    page, _ = synthetic()
    source = tmp_path / "page.png"
    page.save(source)
    assert main(["tone", "--in", str(source), "--out", str(tmp_path / "view.png")]) == 2
    assert ".tif" in capsys.readouterr().err
    deep = tmp_path / "deep.png"
    Image.new("I;16", (100, 100), 40000).save(deep)
    assert main(["tone", "--in", str(deep), "--out", str(tmp_path / "view.tif")]) == 2
    assert "not supported" in capsys.readouterr().err
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert main(["tone", "--in", str(broken), "--out", str(tmp_path / "view.tif")]) == 2
    assert "cannot be read" in capsys.readouterr().err
    assert (
        main(["tone", "--in", str(tmp_path / "missing.png"), "--out", str(tmp_path / "v.tif")]) == 2
    )
    assert (
        main(
            [
                "tone",
                "--in",
                str(source),
                "--out",
                str(tmp_path / "v.tif"),
                "--lift-strength",
                "0.9",
            ]
        )
        == 2
    )
    assert "lift_strength" in capsys.readouterr().err


def test_tone_file_names_the_bytes_it_read(tmp_path):
    page, _ = synthetic()
    source = tmp_path / "page.png"
    page.save(source)
    view, record = tone_file(source, {"lift_strength": 0})
    assert record["input"]["bytes"] == source.stat().st_size
    assert len(record["input"]["sha256"]) == 64 and record["input"]["dpi"] is None
    assert record["lift"]["applied"] is False and "lift:off" in record["steps"]
    with pytest.raises(ToneError):
        write_view(view, tmp_path / "view.jpg")
    info = write_view(view, tmp_path / "view.tiff")
    assert info["bytes"] == (tmp_path / "view.tiff").stat().st_size
    assert isinstance(Path(info["name"]), Path)


# -- the review's cases ---------------------------------------------------------------------


def _lit_page(seed: int = 0) -> Image.Image:
    """Grainy paper under the lighting ramp, nothing drawn yet (draw before lighting)."""
    return _grain(SIZE, 225, seed)


def _light(page: Image.Image) -> Image.Image:
    return ImageChops.multiply(page, _ramp(SIZE))


def test_faint_strokes_just_outside_a_sharp_stain_edge_survive():
    # A 0.6 stain on the lit page; 5-level strokes 8 px outside its shadow-side edge.
    page = _lit_page()
    draw = ImageDraw.Draw(page)
    x0, y0, x1, y1 = STAIN
    strokes = [(x0 - 68, y, x0 - 8, y + 8) for y in range(y0 + 30, y1 - 30, 30)]
    for box in strokes:
        draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=220)
    page.paste(page.crop(STAIN).point(lambda level: round(level * 0.6)), STAIN)
    page = _light(page)
    view, _ = tone(page)

    def contrast(image: Image.Image) -> float:
        return statistics.mean(_mean(image, _above(box)) - _mean(image, box) for box in strokes)

    assert contrast(page) > 2
    assert contrast(view) >= 1.5 * contrast(page)
    for box in strokes:
        stroke = view.crop(box)
        paper = _mean(view, _above(box))
        darker = sum(1 for level in stroke.get_flattened_data() if level < paper)
        assert darker == stroke.size[0] * stroke.size[1], (
            "a stroke pixel ended no darker than its paper"
        )
    # The paper right outside the edge is at the set level, not driven to white.
    for x in range(x0 - 30, x0 - 2, 2):
        assert abs(_mean(view, (x, y0 + 50, x + 1, y1 - 50)) - 235) <= 6
    assert view.histogram()[255] == 0


def test_a_dense_ink_page_keeps_its_paper_gaps_and_the_faint_strokes_in_them():
    # 80 % ink in bands with 20 px paper gaps carrying faint strokes; flattening is
    # forced on by a low paper-share guard so the estimate itself is what is tested.
    page = _lit_page(1)
    draw = ImageDraw.Draw(page)
    gaps, strokes = [], []
    for y in range(0, 1600, 100):
        draw.rectangle((0, y, 1199, y + 79), fill=35)
        gaps.append((0, y + 80, 1200, y + 100))
        for x in range(100, 1100, 150):
            box = (x, y + 86, x + 60, y + 93)
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=217)
            strokes.append(box)
    page = _light(page)
    view, record = tone(page, {"min_paper_share": 0.1})
    assert record["flatten"]["applied"] is True
    histogram = view.histogram()
    assert histogram[255] / sum(histogram) < 0.001
    for box in strokes:
        beside = (box[0] - 30, box[1], box[0] - 5, box[3])
        assert _mean(view, box) <= _mean(view, beside) - 3
        assert abs(_mean(view, beside) - 235) <= 8
    assert _mean(view, (0, 0, 1200, 80)) < 100


def test_a_seal_and_a_blot_keep_their_darkness_and_their_marks():
    page = _lit_page(2)
    draw = ImageDraw.Draw(page)
    draw.ellipse((400, 600, 799, 999), fill=60)  # a 400 px seal
    marks = [(500 + 70 * k, 790, 540 + 70 * k, 800) for k in range(4)]  # letters inside it
    for box in marks:
        draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=20)
    draw.ellipse((225, 1225, 374, 1374), fill=40)  # a 150 px blot
    page = _light(page)
    view, record = tone(page)
    seal_inside = (450, 650, 750, 760)
    blot_inside = (260, 1260, 340, 1340)
    assert _mean(page, seal_inside) < 60 and _mean(page, blot_inside) < 40
    assert _mean(view, seal_inside) <= 90, "the seal was lifted toward paper"
    assert _mean(view, blot_inside) <= 60, "the blot was lifted toward paper"
    # Marks inside the seal keep roughly their contrast against the seal.
    before = statistics.mean(
        _mean(page, (b[0], b[1] - 20, b[2], b[1] - 5)) - _mean(page, b) for b in marks
    )
    after = statistics.mean(
        _mean(view, (b[0], b[1] - 20, b[2], b[1] - 5)) - _mean(view, b) for b in marks
    )
    assert 0.8 * before <= after <= 1.6 * before
    # No ring: the paper around both is at the set level.
    for box in ((120, 780, 170, 820), (830, 780, 880, 820), (400, 1280, 440, 1320)):
        assert abs(_mean(view, box) - 235) <= 6
    assert record["flatten"]["floor_applied_share"] > 0.02


def test_a_thick_faded_stroke_is_not_taken_for_paper():
    # A 60 px square of faded ink: the stroke window must not fit inside it.
    page = _lit_page(3)
    ImageDraw.Draw(page).rectangle((900, 1200, 959, 1259), fill=120)
    page = _light(page)
    view, _ = tone(page)
    assert _mean(view, (905, 1205, 955, 1255)) <= 120  # a halved window lifts it to about 134


def test_the_background_follows_a_steep_lighting_ramp_exactly():
    # A linear ramp is its own closing; an estimator that only takes maxima is offset by
    # half a window and leaves the paper low on the dark side.
    page = Image.new("L", SIZE, 225)
    ramp = Image.linear_gradient("L").rotate(90).resize(SIZE, Image.BILINEAR)
    page = ImageChops.multiply(page, ramp.point(lambda level: 100 + round(level * 155 / 255)))
    view, record = tone(page)
    assert record["flatten"]["applied"] is True
    for x in range(100, 1100, 100):
        assert abs(_mean(view, (x, 100, x + 50, 1500)) - 235) <= 3, f"column {x}"


def test_window_sizes_follow_the_dpi_when_known(tmp_path):
    page, _ = synthetic()
    _, plain = tone(page)
    assert plain["input"]["dpi"] is None
    assert plain["flatten"]["window_px"] == 81 and plain["flatten"]["floor_window_px"] == 405
    assert "no dpi" in plain["flatten"]["scale_note"]
    source = tmp_path / "page.png"
    page.save(source, dpi=(600, 600))
    _, scaled = tone_file(source)
    assert scaled["input"]["dpi"] == [600.0, 600.0]
    assert scaled["flatten"]["scale_from_dpi"] == 2.0
    assert scaled["flatten"]["window_px"] == 163 and scaled["flatten"]["floor_window_px"] == 811
    assert scaled["flatten"]["window_reduced_px"] > plain["flatten"]["window_reduced_px"]
    page.info["dpi"] = (150, 150)
    _, halved = tone(page)
    assert halved["flatten"]["window_px"] == 41


def test_grey_rule_notes_warn_about_blue_black_ink():
    page, _ = synthetic(colour=True)
    for rule in ("min", "blue"):
        _, record = tone(page, {"grey_rule": rule})
        assert "blue-black" in record["grey"]["note"]
    _, record = tone(page)
    assert record["grey"]["rule"] == "luminance" and "default" in record["grey"]["note"]
    meaning = SETTINGS_PATH.read_text().split("[grey_rule]")[1].split("[", 1)[0]
    assert "blue-black" in meaning and "luminance (the default)" in meaning


def test_background_edges_are_counted_only_where_the_estimate_steps():
    _, even = tone(synthetic(stain=False)[0])
    assert even["flatten"]["background_edges"]["edge_share"] == 0
    assert even["flatten"]["background_edges"]["band_share"] == 0
    _, stained = tone(synthetic()[0])
    edges = stained["flatten"]["background_edges"]
    assert 0 < edges["edge_share"] < edges["band_share"] < 0.5


def test_the_written_tiff_is_byte_identical_on_repeat_even_with_an_odd_strip(tmp_path):
    # Find a view whose compressed strip ends on an odd byte, where a pad byte precedes
    # the directory, then write it several times.
    for seed in range(40):
        view, record = tone(_grain((301, 203), 200, seed))
        data = tiff_bytes(view, [300.0, 300.0])
        with Image.open(io.BytesIO(data)) as written:
            counts = written.tag_v2[279]
        if any(count & 1 for count in counts):
            break
    else:
        pytest.fail("no page gave an odd strip")
    directory_offset = int.from_bytes(data[4:8], "little")
    assert directory_offset % 2 == 0 and data[directory_offset - 1] == 0
    out = tmp_path / "view.tif"
    digests = set()
    for repeat in range(5):
        info = write_view(view, out, [300.0, 300.0], force=repeat > 0)
        digests.add(hashlib.sha256(out.read_bytes()).hexdigest())
        assert (
            info["sha256"] in digests and info["pixels_sha256"] == record["view"]["pixels_sha256"]
        )
    assert len(digests) == 1
    with Image.open(out) as written:
        assert written.mode == "L" and written.size == view.size
        assert written.info["compression"] == "tiff_adobe_deflate"
        assert written.info["dpi"] == (300.0, 300.0)
        assert written.tobytes() == view.tobytes()
    # Without dpi the file carries none, and a tall page is written in several strips.
    tall, _ = tone(_grain((40, 700), 200, 0))
    with Image.open(io.BytesIO(tiff_bytes(tall))) as written:
        assert "dpi" not in written.info and len(written.tag_v2[273]) == 3
        assert written.tobytes() == tall.tobytes()


def test_the_command_never_writes_over_its_input_or_an_existing_file(tmp_path, capsys):
    page, _ = synthetic()
    source = tmp_path / "page.tif"
    page.save(source, format="TIFF")
    original = source.read_bytes()
    assert main(["tone", "--in", str(source), "--out", str(source)]) == 2
    assert "never written over" in capsys.readouterr().err
    assert (
        main(["tone", "--in", str(source), "--out", str(tmp_path / "sub" / ".." / "page.tif")]) == 2
    )
    assert source.read_bytes() == original
    out = tmp_path / "view.tif"
    assert main(["tone", "--in", str(source), "--out", str(out)]) == 0
    first = out.read_bytes()
    assert main(["tone", "--in", str(source), "--out", str(out)]) == 2
    assert "exists" in capsys.readouterr().err and out.read_bytes() == first
    assert main(["tone", "--in", str(source), "--out", str(out), "--force"]) == 0
    assert out.read_bytes() == first
    # --force never allows the input itself.
    assert main(["tone", "--in", str(source), "--out", str(source), "--force"]) == 2
    assert source.read_bytes() == original
