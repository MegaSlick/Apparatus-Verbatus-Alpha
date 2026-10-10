"""Skew detector on synthetic pages drawn here; no real register material."""

from __future__ import annotations

import random
from functools import cache

import pytest
from PIL import Image, ImageDraw

from pagekit import _box_synthetic as synth
from pagekit.skew import detect_skew

DPI = 150
TOLERANCE = 0.15  # degrees


def mm(value: float) -> int:
    return synth.mm(value, DPI)


@cache
def level_page(seed: int = 2) -> Image.Image:
    return synth.page(seed=seed, dpi=DPI)


def assert_answer_shape(answer: dict) -> None:
    assert set(answer) == {"value", "confidence", "evidence", "flags"}
    assert 0.0 <= answer["confidence"] <= 1.0
    assert isinstance(answer["evidence"], str) and answer["evidence"]
    assert isinstance(answer["flags"], list)


@pytest.mark.parametrize("angle", [1.3, -2.1, 3.7, -4.4, 0.6])
def test_known_angle_is_found_for_positive_and_negative_turns(angle):
    answer = detect_skew(synth.turned(level_page(), angle), (DPI, DPI))
    assert_answer_shape(answer)
    assert answer["value"] == pytest.approx(angle, abs=TOLERANCE)
    assert answer["flags"] == []
    assert answer["confidence"] > 0.5


def test_positive_value_means_counterclockwise_turn_levels_the_lines():
    # Lines that fall to the right (y grows with x) need a counterclockwise turn.
    image = Image.new("L", (mm(160), mm(220)), synth.PAPER)
    draw = ImageDraw.Draw(image)
    rng = random.Random(9)
    for row in range(18):
        y = mm(25) + row * mm(9)
        for x in range(mm(15), mm(140), mm(18)):
            drop = (x - mm(15)) * 0.035  # about 2 degrees, falling to the right
            synth.word(draw, rng, x, y + drop, DPI)
    answer = detect_skew(image, (DPI, DPI))
    assert answer["value"] > 1.5


def test_level_rules_do_not_pull_the_angle_off_sloping_writing():
    image = synth.turned(synth.page(seed=4, dpi=DPI), 2.0)
    draw = ImageDraw.Draw(image)
    for y in range(mm(25), image.height - mm(15), mm(9)):
        draw.line((mm(8), y, image.width - mm(8), y), fill=60, width=2)
    answer = detect_skew(image, (DPI, DPI))
    assert answer["value"] == pytest.approx(2.0, abs=0.25)
    assert "ruled lines" in answer["evidence"]


def test_wide_dark_band_does_not_pull_the_angle():
    image = synth.turned(synth.page(seed=5, dpi=DPI), -1.5)
    ImageDraw.Draw(image).rectangle((0, 0, image.width, mm(14)), fill=20)
    answer = detect_skew(image, (DPI, DPI))
    assert answer["value"] == pytest.approx(-1.5, abs=TOLERANCE)
    assert "dark band" in answer["evidence"]


def test_slanted_dark_band_at_one_side_does_not_pull_the_angle():
    image = synth.turned(synth.page(seed=5, dpi=DPI), 2.5)
    ImageDraw.Draw(image).polygon(
        [(0, 0), (mm(18), 0), (mm(25), image.height), (0, image.height)], fill=25
    )
    answer = detect_skew(image, (DPI, DPI))
    assert answer["value"] == pytest.approx(2.5, abs=TOLERANCE)


def test_too_little_writing_gives_zero_and_a_too_little_content_flag():
    image = Image.new("L", (mm(160), mm(220)), synth.PAPER)
    draw = ImageDraw.Draw(image)
    rng = random.Random(1)
    synth.word(draw, rng, mm(50), mm(70), DPI)
    synth.word(draw, rng, mm(70), mm(70), DPI)
    answer = detect_skew(image, (DPI, DPI))
    assert_answer_shape(answer)
    assert answer["value"] == 0
    assert answer["confidence"] == 0
    assert any("too little content" in flag for flag in answer["flags"])


def test_blank_page_gives_zero_and_a_too_little_content_flag():
    answer = detect_skew(synth.noisy(Image.new("L", (mm(160), mm(220)), synth.PAPER)), (DPI, DPI))
    assert answer["value"] == 0
    assert answer["confidence"] == 0
    assert any("too little content" in flag for flag in answer["flags"])


def test_two_regions_sloping_differently_are_flagged():
    top = synth.page(seed=6, dpi=DPI, margins_mm=(20, 20, 20, 120))
    bottom = synth.page(seed=7, dpi=DPI, margins_mm=(20, 110, 20, 20))
    image = synth.darker(synth.turned(top, 3.0), synth.turned(bottom, -2.0))
    answer = detect_skew(image, (DPI, DPI))
    assert any("lean differently" in flag for flag in answer["flags"])
    # Neither angle covers most of the writing, so nothing is applied.
    assert answer["value"] == 0


def test_two_columns_sloping_differently_are_flagged():
    left = synth.page(seed=8, dpi=DPI, margins_mm=(15, 20, 85, 20))
    right = synth.page(seed=9, dpi=DPI, margins_mm=(85, 20, 15, 20))
    image = synth.darker(synth.turned(left, 3.0), synth.turned(right, -1.0))
    answer = detect_skew(image, (DPI, DPI))
    assert any("lean differently" in flag for flag in answer["flags"])


def test_tiny_angle_is_snapped_to_zero_and_the_evidence_says_so():
    answer = detect_skew(
        synth.turned(level_page(), 0.3), (DPI, DPI), overrides={"skew_snap_deg": 0.5}
    )
    assert answer["value"] == 0
    assert "snapped to 0" in answer["evidence"]
    assert answer["flags"] == []


def test_angle_past_the_range_is_found_by_widening_once():
    answer = detect_skew(synth.turned(level_page(), 7.0), (DPI, DPI))
    assert answer["value"] == pytest.approx(7.0, abs=TOLERANCE)
    assert answer["flags"] == []


def test_angle_past_the_widened_range_gives_zero_and_a_flag():
    answer = detect_skew(synth.turned(level_page(), -12.0), (DPI, DPI))
    assert answer["value"] == 0
    assert any("edge of the widened search range" in flag for flag in answer["flags"])


def test_untrusted_peak_gives_zero_and_a_flag():
    answer = detect_skew(
        synth.turned(level_page(), 2.0), (DPI, DPI), overrides={"skew_score_margin": 1000.0}
    )
    assert answer["value"] == 0
    assert any("No clear skew" in flag for flag in answer["flags"])


def test_ink_outside_the_page_polygon_does_not_count():
    page = synth.turned(synth.page(seed=10, dpi=DPI, margins_mm=(15, 20, 85, 20)), 1.5)
    neighbour = synth.turned(synth.page(seed=11, dpi=DPI, margins_mm=(85, 20, 15, 20)), -3.0)
    image = synth.darker(page, neighbour)
    cut = mm(80)
    polygon = [(0, 0), (cut, 0), (cut, image.height), (0, image.height)]
    answer = detect_skew(image, (DPI, DPI), polygon=polygon)
    assert answer["value"] == pytest.approx(1.5, abs=TOLERANCE)


def test_unequal_axis_resolution_is_measured_on_square_working_pixels():
    tilted = synth.turned(level_page(), 2.0)
    stretched = tilted.resize((tilted.width, tilted.height * 2), Image.BICUBIC)
    answer = detect_skew(stretched, (DPI, 2 * DPI))
    assert answer["value"] == pytest.approx(2.0, abs=0.2)


def test_same_input_gives_the_same_answer_every_time():
    image = synth.turned(level_page(), -1.7)
    assert detect_skew(image, (DPI, DPI)) == detect_skew(image.copy(), (DPI, DPI))


def test_bad_resolution_is_refused():
    from pagekit._box_common import DetectorInputError

    with pytest.raises(DetectorInputError):
        detect_skew(level_page(), (0, DPI))
    with pytest.raises(DetectorInputError):
        detect_skew(level_page(), None)


# --- Noise, disagreeing estimates and long tilted lines -----------------------------


@pytest.mark.parametrize("kind", ["specks", "grain"])
def test_pure_noise_is_not_trusted(kind):
    size = (mm(160), mm(220))
    if kind == "specks":
        rng = random.Random(1)
        noise = Image.frombytes("L", size, rng.randbytes(size[0] * size[1]))
        noise = noise.point(lambda v: 30 if v > 225 else 228)
    else:
        noise = Image.effect_noise(size, 60)
    answer = detect_skew(noise, (DPI, DPI))
    assert answer["value"] == 0
    assert answer["flags"]
    assert answer["confidence"] <= 0.5


def test_disagreeing_estimates_give_zero_and_a_flag(monkeypatch):
    from pagekit import skew

    monkeypatch.setattr(skew, "line_fit_estimate", lambda *args: 3.0)
    answer = detect_skew(synth.turned(level_page(), 1.0), (DPI, DPI))
    assert answer["value"] == 0
    assert any("disagree" in flag for flag in answer["flags"])


def test_long_tilted_lines_apart_from_the_writing_are_removed_by_shape():
    # Fails if the shape test for rules is removed: these lines are too steep for the
    # run test and would pull the profile to their own angle.
    image = synth.page(seed=13, dpi=DPI, margins_mm=(20, 60, 20, 60))
    draw = ImageDraw.Draw(image)
    for y in list(range(mm(8), mm(50), mm(6))) + list(range(mm(170), mm(215), mm(6))):
        draw.line((mm(10), y, mm(150), y + mm(140) * 0.07), fill=30, width=3)
    answer = detect_skew(image, (DPI, DPI))
    assert answer["value"] == pytest.approx(0.0, abs=TOLERANCE)


def test_implausible_resolution_is_flagged():
    # Measured at the fallback resolution, never silently.
    answer = detect_skew(synth.turned(level_page(), 1.0), (1e6, 1e6))
    assert any("resolution" in flag for flag in answer["flags"])
    assert answer["confidence"] <= 0.5


def test_noise_page_finishes_quickly():
    import time

    noise = Image.effect_noise((mm(160), mm(220)), 90)
    start = time.monotonic()
    answer = detect_skew(noise, (DPI, DPI))
    assert time.monotonic() - start < 5
    assert answer["value"] == 0 and answer["flags"]


def test_ends_of_an_inked_block_do_not_make_noise_peak_at_zero():
    # Fails without the taper: a block of random pixels (scored directly, so the noise
    # guard does not stop it first) otherwise scores highest at 0 degrees because of its
    # straight top and bottom.
    from pagekit import _box_common as common
    from pagekit import skew

    rng = random.Random(3)
    size = (900, 400)
    block = Image.frombytes("L", size, rng.randbytes(size[0] * size[1]))
    ink = block.point(lambda v: 1 if v > 127 else 0).convert("F")
    settings = common.values(common.load_thresholds())
    found = skew.profile_search(ink, settings, lag=3, widen=False)
    assert found["ratio"] < settings["skew_score_margin"]


# --- Agreeing and opposing estimates ----------------------------------------------


def test_estimates_that_agree_in_sign_and_roughly_in_size_level_the_page():
    # Waving baselines and flourishes put the line fit about 0.7 degrees below the
    # profile; a page visibly tilted 2.5 degrees must still be levelled.
    image = synth.turned(synth.flourished_page(3), 2.5)
    answer = detect_skew(image, (DPI, DPI))
    assert answer["value"] == pytest.approx(2.5, abs=0.2)
    assert "line fit" in answer["evidence"] and "profile" in answer["evidence"]
    assert answer["confidence"] < 1.0


def test_estimates_of_opposite_sign_still_give_zero(monkeypatch):
    from pagekit import skew

    monkeypatch.setattr(skew, "line_fit_estimate", lambda *args: -1.0)
    answer = detect_skew(synth.turned(level_page(), 1.0), (DPI, DPI))
    assert answer["value"] == 0
    assert any("disagree" in flag for flag in answer["flags"])
