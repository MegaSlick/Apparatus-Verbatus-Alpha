"""Shared helpers of the skew and box detectors, on tiny synthetic maps drawn here."""

from __future__ import annotations

import tomllib

import pytest
from PIL import Image, ImageDraw

from pagekit import _box_common as common


def test_every_setting_is_unmeasured_and_explained():
    with common.THRESHOLDS_PATH.open("rb") as handle:
        table = tomllib.load(handle)
    for name, entry in table.items():
        assert set(entry) == {"value", "status", "meaning"}, name
        assert entry["status"] == "UNMEASURED", name
        assert entry["meaning"].strip(), name


def test_overrides_are_checked():
    with pytest.raises(common.DetectorInputError):
        common.load_thresholds({"no_such_setting": 1})
    with pytest.raises(common.DetectorInputError):
        common.load_thresholds({"speck_mm": "big"})
    with pytest.raises(common.DetectorInputError):
        common.load_thresholds({"target_min_patches": 2.5})
    table = common.load_thresholds({"speck_mm": 0.3})
    assert table["speck_mm"] == {"value": 0.3, "status": "UNMEASURED", "source": "override"}


def test_answer_has_exactly_the_four_keys_and_refuses_a_broken_one():
    assert common.answer(1.5, 0.5, "Because.", []) == {
        "value": 1.5,
        "confidence": 0.5,
        "evidence": "Because.",
        "flags": [],
    }
    with pytest.raises(ValueError):
        common.answer(0, 1.5, "Because.", [])
    with pytest.raises(ValueError):
        common.answer(0, 0.5, " ", [])
    with pytest.raises(ValueError):
        common.answer(0, 0.5, "Because.", "a flag")
    with pytest.raises(TypeError):
        common.answer(0, True, "Because.", [])


def test_components_are_eight_connected_and_in_raster_order():
    marks = Image.new("L", (10, 6), 0)
    for point in [(1, 1), (2, 2), (3, 3), (7, 1), (7, 2), (8, 4)]:
        marks.putpixel(point, 255)
    parts = common.components(marks)
    assert [p.box for p in parts] == [(1, 1, 4, 4), (7, 1, 8, 3), (8, 4, 9, 5)]
    assert [p.area for p in parts] == [3, 2, 1]
    assert common.paint(parts, marks.size).tobytes() == marks.tobytes()


def test_components_join_runs_that_branch_and_merge():
    marks = Image.new("L", (9, 4), 0)
    draw = ImageDraw.Draw(marks)
    draw.line((0, 0, 0, 3), fill=255)  # a U shape joined only along the bottom row
    draw.line((8, 0, 8, 3), fill=255)
    draw.line((0, 3, 8, 3), fill=255)
    parts = common.components(marks)
    assert len(parts) == 1
    assert parts[0].area == 4 + 4 + 7


def test_erosion_and_dilation_use_the_given_rectangle():
    marks = Image.new("L", (40, 20), 0)
    ImageDraw.Draw(marks).rectangle((10, 5, 29, 14), fill=255)  # 20 by 10
    assert common.erode(marks, 3, 1).getbbox() == (13, 6, 27, 14)
    assert common.dilate(marks, 3, 1).getbbox() == (7, 4, 33, 16)
    assert common.opening(marks, 3, 1).getbbox() == (10, 5, 30, 15)
    assert common.erode(marks, 11, 0).getbbox() is None


def test_long_lines_keep_rule_pixels_and_spare_crossing_writing():
    marks = Image.new("L", (300, 60), 0)
    draw = ImageDraw.Draw(marks)
    draw.line((5, 30, 295, 30), fill=255)  # a rule
    draw.line((100, 10, 100, 50), fill=255)  # a stroke crossing it
    rules = common.long_lines(marks, 150, 2, 3.0)
    left, top, right, bottom = rules.getbbox()
    assert (left, right) == (5, 296) and 29 <= top <= 30 and 31 <= bottom <= 32
    assert rules.getpixel((100, 15)) == 0 and rules.getpixel((100, 45)) == 0


def test_rule_shape_needs_long_thin_and_straight():
    long_thin = Image.new("L", (300, 20), 0)
    ImageDraw.Draw(long_thin).line((0, 5, 299, 12), fill=255, width=2)
    (part,) = common.components(long_thin)
    assert common.rule_shaped(part, 150, 3.0, 10.0)
    assert not common.rule_shaped(part, 400, 3.0, 10.0)
    blob = Image.new("L", (300, 20), 0)
    ImageDraw.Draw(blob).rectangle((0, 0, 299, 19), fill=255)
    (part,) = common.components(blob)
    assert not common.rule_shaped(part, 150, 3.0, 10.0)


def test_working_copy_has_square_pixels_and_maps_boxes_back():
    image = Image.new("L", (600, 1200), 200)
    work = common.working_copy(common.page_input(image, (300, 600)), 100)
    assert work.grey.size == (200, 200)
    assert work.to_source((10, 10, 20, 20), image.size) == [30, 60, 60, 120]
    assert work.from_source((31, 61, 59, 119)) == (11, 11, 19, 19)


def test_flatten_turns_a_slow_gradient_into_level_paper_and_keeps_ink():
    gradient = Image.linear_gradient("L").resize((400, 400)).point(lambda v: 120 + v * 110 // 255)
    ImageDraw.Draw(gradient).line((50, 50, 350, 50), fill=20, width=3)
    flat = common.flatten(gradient, 40)
    paper = flat.crop((0, 100, 400, 400))
    assert min(paper.getextrema()) >= 235
    assert flat.getpixel((200, 50)) < 200  # still well below the flattened paper


def test_input_modes_and_polygons_are_checked():
    with pytest.raises(common.DetectorInputError):
        common.page_input(Image.new("F", (50, 50)), (100, 100))
    with pytest.raises(common.DetectorInputError):
        common.page_input(Image.new("L", (50, 50)), (100, 100), polygon=[(0, 0), (1, 1)])
    page = common.page_input(Image.new("RGB", (50, 50)), (100, 100))
    assert page.grey.mode == "L" and page.colour is not None
