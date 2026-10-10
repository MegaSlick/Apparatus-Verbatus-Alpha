"""Content-box detector on synthetic pages drawn here; no real register material."""

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
    speck = (settings["speck_mm"] / work.mm) ** 2
    touch = work.px(settings["debris_distance_mm"])
    for ink in ink_map(work, (0, 0) + work.grey.size, settings):  # lenient, then strict
        inside = [p for p in common.components(ink) if p.area >= speck and p.x0 >= touch]
        assert not inside
    answer = detect_content_box(shaded, (DPI, DPI))
    assert answer["value"] is None and answer["flags"] == []


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
    # Edge debris is not counted as discarded writing,
    # but a piece the size of a mark is still reported.
    assert "edge debris" in answer["evidence"]
    assert any("touching the paper edge" in flag for flag in answer["flags"])


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


# --- Faint ink, stains and edge debris ------------------------------------------


def faint_page(dpi: int, paper: int, main: int | None, note: int, signature: int):
    """Writing at `main` (or none), a marginal note at `note` and a signature at
    `signature` grey, on paper at `paper`; also the boxes of the note and signature."""
    size = (synth.mm(150, dpi), synth.mm(200, dpi))
    rng = random.Random(21)
    image = Image.new("L", size, paper)
    if main is not None:
        synth.writing(
            ImageDraw.Draw(image),
            rng,
            (synth.mm(35, dpi), synth.mm(30, dpi), synth.mm(120, dpi), synth.mm(150, dpi)),
            dpi,
            ink=main,
        )
    note_layer = Image.new("L", size, paper)
    synth.writing(
        ImageDraw.Draw(note_layer),
        rng,
        (synth.mm(5, dpi), synth.mm(60, dpi), synth.mm(30, dpi), synth.mm(85, dpi)),
        dpi,
        xh_mm=1.8,
        ink=note,
    )
    sign_layer = Image.new("L", size, paper)
    draw = ImageDraw.Draw(sign_layer)
    for i in range(60):  # a flourish drawn at the signature's grey
        t = i / 59
        x0 = synth.mm(95, dpi) + t * synth.mm(40, dpi)
        draw.line(
            (x0, synth.mm(178, dpi), x0 + synth.mm(1, dpi), synth.mm(172 + 6 * (i % 2), dpi)),
            fill=signature,
            width=max(1, synth.mm(0.4, dpi)),
        )
    boxes = (
        note_layer.point(lambda v: 255 if v < paper - 10 else 0).getbbox(),
        sign_layer.point(lambda v: 255 if v < paper - 10 else 0).getbbox(),
    )
    return synth.darker(image, note_layer, sign_layer), boxes


def test_faint_note_and_signature_beside_dark_writing_are_kept():
    # With dark main text, Otsu splits halfway to the paper; both faint marks are kept.
    image, boxes = faint_page(300, 228, 35, 150, 140)
    answer = detect_content_box(synth.noisy(image), (300, 300))
    for box in boxes:
        assert contains(answer["value"], box), (answer["value"], box)


def test_page_written_only_in_faint_ink_is_not_blank_and_is_flagged():
    # Paper 210 and ink 180 is not a confident blank page.
    image, boxes = faint_page(150, 210, 180, 180, 180)
    answer = detect_content_box(synth.noisy(image, sigma=3), (150, 150))
    assert answer["value"] is not None
    for box in boxes:
        assert contains(answer["value"], box), (answer["value"], box)
    assert any("faint" in flag for flag in answer["flags"])


def test_blank_page_with_low_contrast_structure_gets_low_confidence():
    # Very faint marks, below even the lenient level, leave the page blank but doubtful.
    image = Image.new("L", text_page().size, synth.PAPER)
    synth.writing(
        ImageDraw.Draw(image),
        random.Random(4),
        (mm(30), mm(30), mm(130), mm(180)),
        DPI,
        ink=synth.PAPER - 12,
    )
    answer = detect_content_box(image, (DPI, DPI))
    assert answer["value"] is None
    assert answer["confidence"] <= 0.5


def test_writing_inside_a_dark_area_at_the_border_is_kept_or_reported():
    # A dark stain touching the border with writing running into it must not be
    # removed whole, writing and all, without a report.
    image = text_page().copy()
    stain = Image.new("L", image.size, 255)
    ImageDraw.Draw(stain).rectangle((0, 0, mm(40), image.height), fill=110)
    image = ImageChops.multiply(image, stain)
    writing = ink_box(text_page())
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    # The writing outside the stain is kept, up to the stain's edge...
    assert answer["value"][0] <= mm(40) + SLACK
    assert close(answer["value"][1:], writing[1:])
    # ...and the writing under the stain is reported and flagged.
    assert any("dark area" in flag for flag in answer["flags"])


def test_small_mark_touching_the_paper_edge_is_flagged():
    # A 3.5 mm cross about 0.5 mm from the edge is debris, but must not vanish silently.
    image = text_page().copy()
    half = 3.5 / 2
    synth.cross(ImageDraw.Draw(image), mm(0.5 + half), mm(100), DPI)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert any("touching the paper edge" in flag for flag in answer["flags"])


def test_implausible_resolution_is_flagged_and_the_page_is_not_blank():
    answer = detect_content_box(synth.noisy(text_page()), (1e6, 1e6))
    assert answer["value"] is not None
    assert any("resolution" in flag for flag in answer["flags"])


def test_noise_page_finishes_quickly_and_is_flagged():
    import time

    noise = Image.effect_noise((mm(160), mm(220)), 90)
    start = time.monotonic()
    answer = detect_content_box(noise, (DPI, DPI))
    assert time.monotonic() - start < 5
    assert any("noise" in flag for flag in answer["flags"])
    assert answer["value"] is not None  # when in doubt, keep everything


# --- Second review (texture is not ink) ---------------------------------------------


@cache
def clean_300() -> tuple[Image.Image, list[int]]:
    image = synth.page(seed=3, dpi=300, size_mm=(150, 200), margins_mm=(30, 30, 30, 30))
    return image, detect_content_box(synth.noisy(image, sigma=2), (300, 300))["value"]


def near(found: list[int], wanted: list[int], slack: int = 6) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(found, wanted, strict=True))


def test_mottled_text_page_gives_the_clean_box_and_no_flag():
    image, clean = clean_300()
    answer = detect_content_box(synth.mottled(image, 5, 1.5, 300), (300, 300))
    assert answer["flags"] == []
    assert near(answer["value"], clean), (answer["value"], clean)


@pytest.mark.parametrize("sd", [4, 5])
def test_mottled_text_page_at_400_dpi_gives_no_flag(sd):
    image = synth.page(seed=5, dpi=400, size_mm=(150, 200), margins_mm=(30, 30, 30, 30))
    clean = detect_content_box(synth.noisy(image, sigma=2), (400, 400))["value"]
    answer = detect_content_box(synth.mottled(image, sd, 1.5, 400, seed=5), (400, 400))
    assert answer["flags"] == []
    assert near(answer["value"], clean, 8), (answer["value"], clean)


def test_strongly_mottled_text_page_does_not_spread_the_box():
    image, clean = clean_300()
    answer = detect_content_box(synth.mottled(image, 6.5, 2.0, 300, seed=8), (300, 300))
    assert near(answer["value"], clean), (answer["value"], clean)


def test_blank_mottled_page_is_blank():
    blank = Image.new("L", clean_300()[0].size, synth.PAPER)
    answer = detect_content_box(synth.mottled(blank, 5, 1.5, 300), (300, 300))
    assert answer["value"] is None


def test_foxed_page_keeps_its_box():
    image, clean = clean_300()
    answer = detect_content_box(synth.foxed(image, 300), (300, 300))
    assert near(answer["value"], clean), (answer["value"], clean)


def test_foxed_blank_page_is_blank():
    blank = Image.new("L", clean_300()[0].size, synth.PAPER)
    answer = detect_content_box(synth.foxed(blank, 300), (300, 300))
    assert answer["value"] is None


def test_very_faint_writing_outside_the_box_is_flagged():
    # A 0.4 mm pen at contrast 15 beside ordinary writing is flagged, not lost.
    image = text_page().copy()
    note = Image.new("L", image.size, synth.PAPER)
    synth.writing(
        ImageDraw.Draw(note),
        random.Random(7),
        (mm(3), mm(60), mm(27), mm(100)),
        DPI,
        ink=synth.PAPER - 15,
    )
    answer = detect_content_box(synth.noisy(synth.darker(image, note), sigma=1.5), (DPI, DPI))
    assert any("faint" in flag for flag in answer["flags"])


def test_tape_near_the_edge_is_described_as_tape():
    image = text_page().copy()
    ImageDraw.Draw(image).rectangle((mm(8), mm(100), mm(11), mm(125)), fill=60)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    flags = [flag for flag in answer["flags"] if "tape" in flag]
    assert flags and not any("touching" in flag for flag in flags)


# --- Edge lines are not discarded writing ----------------------------------------


def test_thin_shadows_along_the_paper_edges_are_not_discarded_writing():
    image = text_page().copy()
    draw = ImageDraw.Draw(image)
    draw.rectangle((mm(30), 0, mm(120), mm(1.2)), fill=90)  # along the top edge
    draw.rectangle((mm(40), image.height - mm(1.5), mm(110), image.height), fill=80)
    draw.rectangle((image.width - mm(1.0), mm(40), image.width, mm(160)), fill=70)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert close(answer["value"], ink_box(text_page()))
    assert answer["flags"] == []
    assert "lines along the paper's edge" in answer["evidence"]


def test_slanted_edge_line_along_the_side_is_not_discarded_writing():
    # The sheet's edge leaning a degree inside the page box: one long thin line.
    image = text_page().copy()
    ImageDraw.Draw(image).line((mm(0.3), mm(20), mm(2.6), mm(200)), fill=70, width=3)
    answer = detect_content_box(synth.noisy(image), (DPI, DPI))
    assert close(answer["value"], ink_box(text_page()))
    assert answer["flags"] == []
