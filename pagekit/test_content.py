"""Content-box detector (spec 0004) on synthetic pages drawn here; no real register material."""

from __future__ import annotations

import random
from functools import cache

import pytest
from PIL import Image, ImageChops, ImageDraw

from pagekit import _box_common as common
from pagekit import _box_synthetic as synth
from pagekit.content import detect_content_box, ink_map

DPI = 150
SLACK = 3  # source pixels: about two working pixels at the default 100 dpi


def mm(value: float) -> int:
    return synth.mm(value, DPI)


@cache
def text_page() -> Image.Image:
    """Writing between 30 mm margins; its true ink box is measured from the drawing."""
    return synth.page(seed=3, dpi=DPI, margins_mm=(30, 30, 30, 30))


def ink_box(image: Image.Image) -> tuple[int, int, int, int]:
    return image.point(lambda level: 255 if level < 150 else 0).getbbox()


def contains(outer: list[int], inner: tuple[int, int, int, int]) -> bool:
    return (
        outer[0] <= inner[0] + SLACK
        and outer[1] <= inner[1] + SLACK
        and outer[2] >= inner[2] - SLACK
        and outer[3] >= inner[3] - SLACK
    )


def close(found: list[int], wanted: tuple[int, int, int, int]) -> bool:
    return all(abs(a - b) <= SLACK for a, b in zip(found, wanted, strict=True))


def test_plain_page_gives_the_writings_box():
    image = text_page()
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert set(answer) == {"value", "confidence", "evidence", "flags"}
    assert close(answer["value"], ink_box(image)), (answer["value"], ink_box(image))
    assert answer["flags"] == []


def test_marginal_note_signature_and_small_cross_near_the_edge_are_kept():
    rng = random.Random(5)
    size = text_page().size
    note = Image.new("L", size, synth.PAPER)
    synth.writing(ImageDraw.Draw(note), rng, (mm(4), mm(60), mm(26), mm(80)), DPI, xh_mm=1.8)
    signature = Image.new("L", size, synth.PAPER)
    synth.signature(ImageDraw.Draw(signature), rng, mm(110), mm(205), DPI)
    cross = Image.new("L", size, synth.PAPER)
    synth.cross(ImageDraw.Draw(cross), mm(153), mm(7), DPI)
    image = synth.darker(text_page(), note, signature, cross)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    for part in (note, signature, cross):
        assert contains(answer["value"], ink_box(part)), (answer["value"], ink_box(part))
    assert "near the page edge" in answer["evidence"]


def test_speck_smaller_than_the_speck_size_is_not_counted():
    image = text_page().copy()
    # A 0.17 mm speck well away from the writing: below the 0.4 mm speck size.
    ImageDraw.Draw(image).point((mm(12), mm(200)), fill=30)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert close(answer["value"], ink_box(text_page()))


def test_dot_above_the_speck_size_is_counted():
    image = text_page().copy()
    ImageDraw.Draw(image).ellipse((mm(12), mm(200), mm(12) + 4, mm(200) + 4), fill=30)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert answer["value"][0] <= mm(12) + SLACK
    assert answer["value"][3] >= mm(200) + 4 - SLACK


def test_dark_border_shadow_is_not_content():
    image = text_page().copy()
    ImageDraw.Draw(image).rectangle((image.width - mm(12), 0, image.width, image.height), fill=60)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert close(answer["value"], ink_box(text_page()))
    assert "connected to the edge" in answer["evidence"]


def test_narrow_border_shadow_is_not_content():
    image = text_page().copy()
    ImageDraw.Draw(image).rectangle((0, 0, mm(4), image.height), fill=50)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert close(answer["value"], ink_box(text_page()))


def test_blank_page_gives_none_and_blank():
    answer = detect_content_box(
        synth.noisy(Image.new("L", text_page().size, synth.PAPER)), (DPI, DPI)
    )
    assert answer["value"] is None
    assert answer["evidence"].startswith("Blank page")
    assert answer["flags"] == []


def test_stained_blank_page_is_still_blank():
    blank = Image.new("L", text_page().size, synth.PAPER)
    stain = Image.radial_gradient("L").resize(blank.size).point(lambda v: 170 + v * 85 // 255)
    answer = detect_content_box(synth.noisy(ImageChops.multiply(blank, stain)), (DPI, DPI))
    assert answer["value"] is None


def test_page_with_one_small_cross_is_not_blank():
    image = Image.new("L", text_page().size, synth.PAPER)
    synth.cross(ImageDraw.Draw(image), mm(80), mm(110), DPI)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert answer["value"] is not None
    assert close(answer["value"], ink_box(image))


def test_page_judged_blank_with_ink_above_speck_size_is_flagged():
    image = Image.new("L", text_page().size, synth.PAPER)
    ImageDraw.Draw(image).ellipse((mm(80), mm(110), mm(80) + 4, mm(110) + 4), fill=30)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert answer["value"] is None
    assert any("Judged blank" in flag for flag in answer["flags"])


def test_colour_target_grid_is_excluded_and_reported():
    image = synth.noisy(text_page()).convert("RGB")
    draw = ImageDraw.Draw(image)
    colours = [
        (200, 30, 30),
        (30, 160, 40),
        (30, 40, 190),
        (220, 200, 20),
        (150, 30, 150),
        (20, 170, 170),
    ]
    for column in range(6):
        for row in range(2):
            x, y = mm(20) + column * mm(12), image.height - mm(26) + row * mm(12)
            draw.rectangle((x, y, x + mm(10), y + mm(10)), fill=colours[(column + row) % 6])
    answer = detect_content_box(image, (DPI, DPI))
    assert close(answer["value"], ink_box(text_page()))
    assert "scanning target" in answer["evidence"]
    assert "excluded" in answer["evidence"]


def test_grey_target_grid_is_flagged_not_excluded():
    image = synth.noisy(text_page()).copy()
    draw = ImageDraw.Draw(image)
    for column in range(6):
        for row in range(2):
            x, y = mm(20) + column * mm(12), image.height - mm(26) + row * mm(12)
            draw.rectangle((x, y, x + mm(10), y + mm(10)), fill=40 + 25 * ((column + row) % 4))
    answer = detect_content_box(image, (DPI, DPI))
    assert any("Suspected scanning target" in flag for flag in answer["flags"])
    assert answer["value"][3] >= image.height - mm(26) + mm(22) - SLACK  # kept in the content


def test_ruler_on_a_colour_page_is_excluded_and_reported():
    image = synth.noisy(text_page()).convert("RGB")
    draw = ImageDraw.Draw(image)
    y = image.height - mm(15)
    draw.rectangle((mm(30), y, mm(130), y + mm(1.5)), fill=(20, 20, 20))
    for x in range(mm(30), mm(131), mm(5)):
        draw.rectangle((x, y - mm(3), x + 1, y), fill=(20, 20, 20))
    answer = detect_content_box(image, (DPI, DPI))
    assert answer["value"][3] < y - mm(3)
    assert "ruler" in answer["evidence"]


def test_ruled_lines_reach_past_the_writing_only_by_the_overhang():
    image = text_page().copy()
    draw = ImageDraw.Draw(image)
    for y in range(mm(35), image.height - mm(30), mm(9)):
        draw.line((mm(5), y, image.width - mm(5), y), fill=70, width=2)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    writing = ink_box(text_page())
    overhang = mm(5)
    assert answer["value"][0] >= writing[0] - overhang - SLACK
    assert answer["value"][2] <= writing[2] + overhang + SLACK
    assert answer["value"][0] < writing[0]  # the rules are kept, up to the overhang
    assert "Ruled lines reach past the writing" in answer["evidence"]


def test_gutter_shadow_does_not_pull_the_content_box_into_it():
    image = text_page()
    gradient = Image.linear_gradient("L").rotate(90, expand=True).resize((mm(25), image.height))
    light = Image.new("L", image.size, 255)
    light.paste(gradient, (0, 0))  # darkest at the left edge, fading out over 25 mm
    shaded = ImageChops.multiply(image, light.point(lambda v: 80 + v * 175 // 255))
    answer = detect_content_box(synth.noisy(shaded), (DPI, DPI))
    assert close(answer["value"], ink_box(image))


def test_flattening_keeps_a_faint_gutter_shadow_out_of_the_ink():
    image = Image.new("L", text_page().size, synth.PAPER)
    gradient = Image.linear_gradient("L").rotate(90, expand=True).resize((mm(40), image.height))
    light = Image.new("L", image.size, 255)
    light.paste(gradient, (0, 0))
    shaded = synth.noisy(ImageChops.multiply(image, light.point(lambda v: 140 + v * 115 // 255)))
    settings = common.values(common.load_thresholds())
    work = common.working_copy(
        common.page_input(shaded, (DPI, DPI)), settings["content_working_dpi"]
    )
    ink, _ = ink_map(work, (0, 0) + work.grey.size, settings)
    assert ink.getbbox() is None


def test_page_box_limits_the_content():
    image = text_page().copy()
    synth.cross(ImageDraw.Draw(image), mm(5), mm(5), DPI)  # outside the page box below
    box = [mm(20), mm(20), image.width - mm(20), image.height - mm(20)]
    answer = detect_content_box(synth.noisy(image), (DPI, DPI), page_box=box)
    assert close(answer["value"], ink_box(text_page()))


def test_ink_outside_the_page_polygon_does_not_count():
    image = text_page().copy()
    synth.cross(ImageDraw.Draw(image), image.width - mm(8), mm(100), DPI)
    cut = image.width - mm(20)
    polygon = [(0, 0), (cut, 0), (cut, image.height), (0, image.height)]
    answer = detect_content_box(synth.noisy(image), (DPI, DPI), polygon=polygon)
    assert close(answer["value"], ink_box(text_page()))


def test_discarded_ink_is_reported_and_flagged_at_the_crop_check_thresholds():
    image = text_page().copy()
    # Writing that runs off the paper edge is debris here, so it is discarded and shown.
    ImageDraw.Draw(image).rectangle((0, mm(100), mm(15), mm(100) + 4), fill=30)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert "edge debris" in answer["evidence"]
    assert any("outside the content box" in flag for flag in answer["flags"])


def test_large_scan_is_measured_on_a_reduced_copy():
    image = synth.page(size_mm=(190, 285), dpi=400, seed=11)
    answer = detect_content_box(image, (400, 400))
    wanted = ink_box(image)
    assert all(abs(a - b) <= 8 for a, b in zip(answer["value"], wanted, strict=True))


def test_same_input_gives_the_same_answer_every_time():
    image = synth.noisy(text_page())
    assert detect_content_box(image, (DPI, DPI)) == detect_content_box(image.copy(), (DPI, DPI))


def test_bad_page_box_is_refused():
    with pytest.raises(common.DetectorInputError):
        detect_content_box(text_page(), (DPI, DPI), page_box=[10, 10, 5, 50])
