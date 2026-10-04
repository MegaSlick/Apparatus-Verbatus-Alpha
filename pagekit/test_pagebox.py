"""Page-box detector (spec 0004) on synthetic frames drawn here; no real register material."""

from __future__ import annotations

from functools import cache

import pytest
from PIL import Image, ImageDraw

from pagekit import _box_synthetic as synth
from pagekit._box_common import DetectorInputError
from pagekit.pagebox import detect_page_box

DPI = 150
AT = (120, 90)
FRAME = (1200, 1500)
SLACK = 4  # source pixels: under one working pixel at the default 50 dpi


@cache
def paper() -> Image.Image:
    return synth.page(seed=3, dpi=DPI)


def on_backdrop(level: int) -> Image.Image:
    frame = Image.new("L", FRAME, level)
    frame.paste(paper(), AT)
    return synth.noisy(frame)


def expected() -> list[int]:
    return [AT[0], AT[1], AT[0] + paper().width, AT[1] + paper().height]


def assert_close(found: list[int], wanted: list[int]) -> None:
    assert len(found) == len(wanted)
    assert all(abs(a - b) <= SLACK for a, b in zip(found, wanted, strict=True)), (found, wanted)


def test_page_on_a_dark_backdrop_gives_the_papers_box():
    answer = detect_page_box(on_backdrop(25), (DPI, DPI))
    assert set(answer) == {"value", "confidence", "evidence", "flags"}
    assert_close(answer["value"], expected())
    assert answer["flags"] == []


def test_page_on_a_pale_backdrop_gives_the_papers_box():
    answer = detect_page_box(on_backdrop(250), (DPI, DPI))
    assert_close(answer["value"], expected())
    assert answer["flags"] == []


def test_page_cropped_to_the_paper_gives_the_frame_with_no_flag():
    image = synth.noisy(paper())
    answer = detect_page_box(image, (DPI, DPI))
    assert answer["value"] == [0, 0, image.width, image.height]
    assert answer["flags"] == []
    assert answer["evidence"].count("kept at the frame edge") == 4


def test_side_without_backdrop_keeps_the_frame_edge_without_a_flag():
    frame = Image.new("L", (paper().width + 150, paper().height), 25)
    frame.paste(paper(), (150, 0))
    answer = detect_page_box(synth.noisy(frame), (DPI, DPI))
    assert_close(answer["value"], [150, 0, frame.width, frame.height])
    assert answer["value"][1:] == [0, frame.width, frame.height]
    assert answer["flags"] == []
    assert "left" in answer["evidence"] and "kept at the frame edge" in answer["evidence"]


def test_label_and_dust_on_the_backdrop_do_not_stop_the_walk():
    frame = on_backdrop(25)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((20, 400, 60, 470), fill=240)  # a label narrower than the run setting
    draw.rectangle((5, 5, 9, 9), fill=255)  # dust
    answer = detect_page_box(frame, (DPI, DPI))
    assert_close(answer["value"], expected())


def test_page_much_smaller_than_the_frame_is_found():
    frame = Image.new("L", (2400, 2600), 25)
    frame.paste(paper(), (900, 700))
    answer = detect_page_box(synth.noisy(frame), (DPI, DPI))
    assert_close(answer["value"], [900, 700, 900 + paper().width, 700 + paper().height])


def test_partial_shadow_covering_most_of_a_side_is_cut_off():
    frame = on_backdrop(25)
    height = round(paper().height * 0.7)
    ImageDraw.Draw(frame).rectangle((AT[0], AT[1], AT[0] + 60, AT[1] + height), fill=70)
    answer = detect_page_box(frame, (DPI, DPI))
    assert answer["value"][0] >= AT[0] + 60 - SLACK
    assert_close(answer["value"][1:], expected()[1:])
    assert "shadow" in answer["evidence"]


def test_short_partial_shadow_is_flagged_and_left_in():
    frame = on_backdrop(25)
    height = round(paper().height * 0.3)
    ImageDraw.Draw(frame).rectangle((AT[0], AT[1], AT[0] + 60, AT[1] + height), fill=70)
    answer = detect_page_box(frame, (DPI, DPI))
    assert_close(answer["value"], expected())
    assert any("shadow along the left side" in flag for flag in answer["flags"])


def test_neighbour_strip_outside_the_page_polygon_is_not_page():
    spread = Image.new("L", (1400, paper().height), synth.PAPER)
    spread.paste(paper().crop((0, 0, 900, paper().height)), (0, 0))
    spread.paste(paper().crop((0, 0, 500, paper().height)), (900, 0))
    polygon = [(0, 0), (900, 0), (900, spread.height), (0, spread.height)]
    answer = detect_page_box(synth.noisy(spread), (DPI, DPI), polygon=polygon)
    assert answer["value"] == [0, 0, 900, spread.height]


def test_large_scan_is_measured_on_a_reduced_copy():
    frame = Image.new("L", (3000, 4500), 25)
    page = synth.page(size_mm=(170, 260), dpi=400, seed=4)
    frame.paste(page, (150, 100))
    answer = detect_page_box(frame, (400, 400))
    wanted = [150, 100, 150 + page.width, 100 + page.height]
    assert all(abs(a - b) <= 10 for a, b in zip(answer["value"], wanted, strict=True))


def test_same_input_gives_the_same_answer_every_time():
    image = on_backdrop(25)
    assert detect_page_box(image, (DPI, DPI)) == detect_page_box(image.copy(), (DPI, DPI))


def test_unsupported_image_mode_is_refused():
    with pytest.raises(DetectorInputError):
        detect_page_box(Image.new("I;16", (100, 100)), (DPI, DPI))
