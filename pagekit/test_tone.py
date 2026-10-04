"""The grey tone view on synthetic pages drawn here; no real register material.

Each page is a paper reflectance (with grain) carrying strokes of known levels, lit by a
strong horizontal ramp (a gutter shadow on the left) and marked by a stain with a sharp
edge: observed = reflectance * illumination. The truth that the measures use (where
paper, strokes, hairlines and the stain lie) is kept beside the image.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from pagekit.tone import (
    SCHEMA,
    ToneError,
    closing,
    lift_curve,
    lift_lut,
    load_settings,
    record_json,
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


def test_a_sharp_stain_edge_leaves_no_wide_halo():
    page, _ = synthetic()
    view, record = tone(page)
    settings = {name: entry["value"] for name, entry in record["settings"].items()}
    # Bright paper leaks half a window into the stain at its edge, and the blur smears
    # the edge both ways; beyond that the paper, stained or not, is at the set level.
    halo = settings["background_window_px"] // 2 + settings["background_blur_px"] + 8
    x0, y0, x1, y1 = STAIN
    band = (y0 + 60, y1 - 60)  # rows well inside the stain's height, paper only
    for x in range(x0 - 200, x1 + 150, 4):
        if min(abs(x - x0), abs(x - x1)) <= halo:
            continue
        column = _mean(view, (x, band[0], x + 1, band[1]))
        assert abs(column - 235) <= 5, (
            f"column {x} sits at {column:.1f}, a halo or an uncorrected stain"
        )


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
    assert record_l["grey"] == {"rule": "luminance", "applied": "luminance", "source_mode": "RGB"}
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
def test_the_lift_curve_is_strictly_increasing_and_anchored(anchor_level, strength, shape):
    anchor = anchor_level / 255
    points = [index / 4000 for index in range(4001)]
    values = [lift_curve(x, anchor, strength, shape) for x in points]
    assert all(b > a for a, b in zip(values, values[1:], strict=False))
    assert values[0] == 0 and abs(values[-1] - 1) < 1e-9
    assert abs(lift_curve(anchor, anchor, strength, shape) - anchor) < 1e-12
    # Below the paper the curve darkens; the darkening peaks nearer the paper than black.
    below = [(x, x - f) for x, f in zip(points, values, strict=True) if 0 < x < anchor]
    assert all(darkening > 0 for _, darkening in below)
    peak = max(below, key=lambda pair: pair[1])[0]
    assert peak > anchor / 2
    lut = lift_lut(anchor_level, strength, shape)
    assert len(lut) == 65536 and lut[0] == 0 and lut[-1] == 255
    assert all(b >= a for a, b in zip(lut, lut[1:], strict=False))
    assert lut[anchor_level * 256] == anchor_level


def test_a_lift_of_zero_is_the_identity():
    assert lift_lut(235, 0, 3) == [min(255, round(fine / 256)) for fine in range(65536)]


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
        "schema", "tool", "input", "steps", "grey", "flatten", "lift", "sharpen",
        "settings", "settings_sha256", "settings_measured", "settings_note",
    }  # fmt: skip
    assert record["settings_measured"] is False
    assert json.loads(record_json(record)) == record


# -- files ----------------------------------------------------------------------------------
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
