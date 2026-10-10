"""Page-box detector on synthetic frames drawn here; no real register material."""

from __future__ import annotations

import random
from functools import cache

import pytest
from PIL import Image, ImageChops, ImageDraw

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


# --- Uneven light and shadows over writing ----------------------------------------


def lit_page(low: int, high: int) -> Image.Image:
    """Paper brightening from `low` at the left to `high` at the right, with writing
    from 10 mm of each edge; cropped to the paper."""
    size = (synth.mm(160, DPI), synth.mm(220, DPI))
    ramp = (
        Image.linear_gradient("L")
        .rotate(90, expand=True)
        .transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    )
    ramp = ramp.resize(size).point(lambda v: low + v * (high - low) // 255)
    ink = Image.new("L", size, 255)
    synth.writing(
        ImageDraw.Draw(ink),
        random.Random(31),
        (
            synth.mm(10, DPI),
            synth.mm(20, DPI),
            size[0] - synth.mm(10, DPI),
            size[1] - synth.mm(20, DPI),
        ),
        DPI,
    )
    return synth.noisy(ImageChops.darker(ramp, ink))


@pytest.mark.parametrize(("low", "high"), [(185, 225), (180, 250)])
def test_paper_brightening_across_the_page_is_not_cut_as_pale_backdrop(low, high):
    # The bright side must not be cut in, through the writing.
    image = lit_page(low, high)
    answer = detect_page_box(image, (DPI, DPI))
    assert answer["value"] == [0, 0, image.width, image.height]


def test_gutter_shadow_over_writing_is_not_cut_off_silently():
    # A 40 mm shadow over writing must not move the left edge into the writing.
    frame = Image.new("L", (paper().width + 2 * AT[0], paper().height + 2 * AT[1]), 25)
    shade = (
        Image.linear_gradient("L")
        .rotate(90, expand=True)
        .resize((synth.mm(40, DPI), paper().height))
    )
    light = Image.new("L", paper().size, 255)
    light.paste(shade.point(lambda v: 40 + v * 215 // 255), (0, 0))
    page = synth.page(seed=3, dpi=DPI, margins_mm=(8, 20, 20, 20))
    frame.paste(ImageChops.multiply(page, light), AT)
    writing_left = AT[0] + page.point(lambda v: 255 if v < 100 else 0).getbbox()[0]
    answer = detect_page_box(synth.noisy(frame), (DPI, DPI))
    assert answer["value"][0] <= writing_left
    assert answer["flags"]


def test_pale_backdrop_is_found_by_its_step_to_the_paper():
    # Fails if pale-backdrop detection is turned off.
    frame = Image.new("L", FRAME, 250)
    frame.paste(paper(), AT)
    answer = detect_page_box(frame, (DPI, DPI))
    assert_close(answer["value"], expected())


def test_tall_label_on_the_backdrop_does_not_stop_the_walk():
    # Fails if the run that ends the walk is set to one line.
    frame = on_backdrop(25)
    # Paper-like over two thirds of its columns, but narrower than the run setting.
    ImageDraw.Draw(frame).rectangle((30, 200, 60, 1250), fill=232)
    answer = detect_page_box(frame, (DPI, DPI))
    assert_close(answer["value"], expected())


def test_implausible_resolution_is_flagged():
    answer = detect_page_box(on_backdrop(25), (1e6, 1e6))
    assert any("resolution" in flag for flag in answer["flags"])


# --- Second review (texture is not ink) ---------------------------------------------


@pytest.mark.parametrize("sd", [8, 16])
def test_textured_dark_backdrop_gives_the_papers_box_without_a_flag(sd):
    # Backdrop cloth: grain about half a millimetre across.
    frame = synth.mottled(Image.new("L", FRAME, 30), sd, 0.5, DPI, seed=4)
    frame.paste(synth.noisy(paper()), AT)
    answer = detect_page_box(frame, (DPI, DPI))
    assert_close(answer["value"], expected())
    assert answer["flags"] == []


def grey_target(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    for column in range(6):
        for row in range(2):
            left, top = x + column * synth.mm(12, DPI), y + row * synth.mm(12, DPI)
            level = 90 + 30 * ((column + row) % 6)
            draw.rectangle(
                (left, top, left + synth.mm(10, DPI), top + synth.mm(10, DPI)), fill=level
            )


def ruler(draw: ImageDraw.ImageDraw, x: int, y: int, bar: int = 200) -> None:
    length, height = synth.mm(100, DPI), synth.mm(10, DPI)
    draw.rectangle((x, y, x + length, y + height), fill=bar)
    for tick in range(x + 4, x + length, synth.mm(5, DPI)):
        draw.rectangle((tick, y, tick + 1, y + synth.mm(4, DPI)), fill=20)


def on_tall_frame(level: int) -> Image.Image:
    """The paper with 40 mm of backdrop below it, room for a target or ruler."""
    frame = Image.new("L", (FRAME[0], AT[1] + paper().height + synth.mm(45, DPI)), level)
    frame.paste(paper(), AT)
    return frame


@pytest.mark.parametrize("gap_mm", [12, 3])
@pytest.mark.parametrize("thing", ["target", "ruler"])
@pytest.mark.parametrize("backdrop", [25, 250])
def test_target_or_ruler_on_the_backdrop_stays_out_of_the_box(thing, backdrop, gap_mm):
    # At 3 mm the target is within reach of the paper edge, so only the target and
    # ruler finders keep it out of the cut strip's ink.
    frame = on_tall_frame(backdrop)
    draw = ImageDraw.Draw(frame)
    y = AT[1] + paper().height + synth.mm(gap_mm, DPI)
    if thing == "target":
        grey_target(draw, AT[0] + synth.mm(10, DPI), y)
    else:
        ruler(draw, AT[0] + synth.mm(10, DPI), y)
    answer = detect_page_box(synth.noisy(frame), (DPI, DPI))
    assert_close(answer["value"], expected())
    assert answer["flags"] == []


def test_unwalked_side_much_paler_than_the_paper_is_flagged():
    # A soft-edged pale margin is not cut (no sharp step), but not passed as no backdrop.
    frame = Image.new("L", (paper().width + 150, paper().height), 250)
    frame.paste(paper(), (150, 0))
    from PIL import ImageFilter

    soft = frame.crop((0, 0, 300, frame.height)).filter(ImageFilter.BoxBlur(60))
    frame.paste(soft.crop((0, 0, 210, frame.height)), (0, 0))
    answer = detect_page_box(synth.noisy(frame), (DPI, DPI))
    assert answer["value"][0] < 150
    assert any("paler than the paper" in flag for flag in answer["flags"])
    assert "no backdrop there" not in answer["evidence"].split("left")[1].split(";")[0]


# --- Page-edge stacks -----------------------------------------------------------


@pytest.mark.parametrize("board_mm", [0.0, 8.0])
@pytest.mark.parametrize("lines", [6, 12])
def test_stack_of_page_edges_gives_the_top_sheets_edge(lines, board_mm):
    frame = Image.new("L", (FRAME[0] + 100, FRAME[1]), 20)
    frame.paste(paper(), AT)
    edge = AT[0] + paper().width
    frame = synth.edge_stack(frame, edge, DPI, lines=lines, board_mm=board_mm)
    # The stack runs only beside the sheet, with backdrop above and below it.
    top = Image.new("L", (frame.width - edge, AT[1]), 20)
    frame.paste(top, (edge, 0))
    frame.paste(top, (edge, AT[1] + paper().height))
    answer = detect_page_box(synth.noisy(frame), (DPI, DPI))
    assert abs(answer["value"][2] - edge) <= 6, answer["value"]
    assert_close(answer["value"][:2] + answer["value"][3:], expected()[:2] + expected()[3:])
    assert answer["flags"] == []
    assert "page edges" in answer["evidence"]


def test_dashed_board_edge_beside_the_sheet_is_not_writing():
    # A board edge or loose page edge 2 mm outside the sheet, broken into dashes and
    # seen over part of the side, is not ink and does not pull the edge out.
    frame = on_backdrop(25)
    draw = ImageDraw.Draw(frame)
    # A grey band of book edge in shadow beside the sheet, with a dark dashed line in it.
    draw.rectangle((AT[0] - synth.mm(4, DPI), AT[1], AT[0] - 1, AT[1] + paper().height), fill=105)
    x = AT[0] - synth.mm(2, DPI)
    y = AT[1] + synth.mm(120, DPI)
    while y < AT[1] + paper().height - synth.mm(5, DPI):
        draw.line((x, y, x, y + synth.mm(6, DPI)), fill=30, width=2)
        y += synth.mm(8, DPI)
    answer = detect_page_box(frame, (DPI, DPI))
    assert_close(answer["value"], expected())
    assert answer["flags"] == []
