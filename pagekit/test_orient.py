"""Orientation detector (spec 0003) on synthetic pages drawn here; no real register
material."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageOps

from pagekit._orient_ink import (
    ANSWER_KEYS,
    TOO_LITTLE_INK,
    DetectorError,
    load_settings,
    settings_measured,
)
from pagekit._orient_testpages import PAPER, page, turned, write_block
from pagekit.orient import (
    POSSIBLY_NEGATIVE,
    UNCERTAIN_DIRECTION,
    UNCERTAIN_UPDOWN,
    detect_orientation,
)

# At the margin settings the confidence of each step is exactly 0.5 (see
# pagekit._orient_ink.strength), so 0.5 is the confidence the settings demand.
SETTING_CONFIDENCE = 0.5


def _assert_shape(result: dict) -> None:
    assert tuple(result) == ANSWER_KEYS
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["evidence"], str) and result["evidence"]
    assert all(isinstance(flag, str) for flag in result["flags"])


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_turned_page_gives_the_turns_back_to_upright(quarter_turns):
    result = detect_orientation(turned(page(), quarter_turns))
    _assert_shape(result)
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []
    assert result["confidence"] > SETTING_CONFIDENCE


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_ruled_lines_running_the_other_way_do_not_turn_the_answer(quarter_turns):
    ruled = page()
    draw = ImageDraw.Draw(ruled)
    for x in range(60, 1000, 40):
        draw.line((x, 40, x, 1360), fill=60, width=4)
    for y in range(160, 1300, 48):
        draw.line((80, y + 12, 960, y + 12), fill=90, width=2)
    result = detect_orientation(turned(ruled, quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []


def test_ruled_lines_would_mislead_without_masking():
    """The masking is what saves the ruled page: switched off, the rules win."""
    ruled = page()
    draw = ImageDraw.Draw(ruled)
    for x in range(60, 1000, 40):
        draw.line((x, 40, x, 1360), fill=60, width=4)
    unmasked = detect_orientation(ruled, {"long_mark_share": 1.5})
    assert "Lines run down" in unmasked["evidence"]


def test_grid_of_dots_is_uncertain_and_left_at_zero_turns():
    grid = Image.new("L", (1000, 1400), PAPER)
    draw = ImageDraw.Draw(grid)
    for y in range(100, 1300, 30):
        for x in range(100, 900, 30):
            draw.ellipse((x, y, x + 7, y + 7), fill=40)
    result = detect_orientation(grid)
    _assert_shape(result)
    assert result["value"] == 0
    assert result["flags"] == [UNCERTAIN_DIRECTION]
    assert result["confidence"] < SETTING_CONFIDENCE


def test_symmetric_writing_is_uncertain_up_or_down_and_says_which_part():
    """Lines across, but no ascenders or descenders and centred lines: nothing tells
    upright from upside down."""
    image = Image.new("L", (1000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    for row, y in enumerate(range(150, 1250, 48)):
        words = 8 + (row * 5) % 6
        left = (1000 - (words * 60 - 16)) // 2
        for x in range(left, left + words * 60, 60):
            draw.rectangle((x, y, x + 43, y + 13), outline=45, width=3)
    result = detect_orientation(image)
    assert result["value"] == 0
    assert result["flags"] == [UNCERTAIN_UPDOWN]
    assert "Lines run across" in result["evidence"]


def test_blank_page_gives_zero_turns_zero_confidence_and_a_flag():
    result = detect_orientation(Image.new("L", (1000, 1400), PAPER))
    _assert_shape(result)
    assert result == {
        "value": 0,
        "confidence": 0.0,
        "evidence": result["evidence"],
        "flags": [TOO_LITTLE_INK],
    }


def test_page_with_a_few_marks_is_too_little_ink_to_orient():
    image = Image.new("L", (1000, 1400), PAPER)
    write_block(ImageDraw.Draw(image), (100, 600, 300, 660), seed=5)
    result = detect_orientation(image)
    assert result["value"] == 0
    assert result["confidence"] == 0.0
    assert result["flags"] == [TOO_LITTLE_INK]


def test_light_writing_on_dark_ground_is_flagged_as_possibly_negative():
    result = detect_orientation(ImageOps.invert(page()))
    _assert_shape(result)
    assert result["value"] == 0
    assert result["confidence"] == 0.0
    assert result["flags"] == [POSSIBLY_NEGATIVE]


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_page_on_a_dark_backdrop_is_not_a_negative(quarter_turns):
    frame = Image.new("L", (1300, 1700), 25)
    frame.paste(page(), (150, 150))
    result = detect_orientation(turned(frame, quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []


@pytest.mark.parametrize("quarter_turns", [1, 2])
def test_full_size_scan_is_reduced_and_oriented(quarter_turns):
    big = Image.new("L", (3000, 4500), PAPER)
    write_block(
        ImageDraw.Draw(big),
        (330, 380, 2750, 4150),
        seed=3,
        pitch=150,
        core=44,
        rise=40,
        letter=32,
        stroke=8,
    )
    result = detect_orientation(turned(big, quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []


def test_colour_page_is_read_as_grey():
    result = detect_orientation(turned(page(), 2).convert("RGB"))
    assert result["value"] == 2


def test_same_input_gives_the_same_answer():
    image = turned(page(seed=9), 3)
    assert detect_orientation(image) == detect_orientation(image)


def test_unhandled_mode_is_refused():
    with pytest.raises(DetectorError):
        detect_orientation(Image.new("I;16", (100, 100)))


def test_settings_are_all_unmeasured_and_unknown_overrides_are_refused():
    settings = load_settings()
    assert settings and all(e["status"] == "UNMEASURED" for e in settings.values())
    assert settings_measured() is False
    with pytest.raises(DetectorError):
        load_settings({"no_such_setting": 1})
    with pytest.raises(DetectorError):
        load_settings({"direction_margin": "high"})
