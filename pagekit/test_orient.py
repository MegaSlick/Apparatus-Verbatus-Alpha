"""Orientation detector (spec 0003) on synthetic pages drawn here; no real register
material."""

from __future__ import annotations

import re

import pytest
from PIL import Image, ImageDraw, ImageOps

from pagekit._orient_ink import (
    ANSWER_KEYS,
    TOO_LITTLE_INK,
    DetectorError,
    load_settings,
    setting_values,
    settings_measured,
)
from pagekit._orient_testpages import (
    PAPER,
    cursive_page,
    cursive_spread,
    page,
    printed_page,
    printed_spread,
    turned,
    write_block,
)
from pagekit.orient import (
    POSSIBLY_NEGATIVE,
    UNCERTAIN_CORE_BAND,
    UNCERTAIN_DIRECTION,
    UNCERTAIN_UPDOWN,
    _updown,
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
    assert result["confidence"] < 0.2
    assert "default, not a finding" in result["evidence"]


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
    assert result["confidence"] < 0.2
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


def test_ascender_cue_alone_decides_centred_writing():
    """Centred lines leave the ragged-edge cue nothing; ascenders decide."""
    image = Image.new("L", (1000, 1400), PAPER)
    write_block(ImageDraw.Draw(image), (100, 120, 900, 1280), seed=2, centred=True)
    assert detect_orientation(image)["value"] == 0
    assert detect_orientation(turned(image, 2))["value"] == 2


def test_ragged_edge_cue_when_switched_on_outweighs_a_hand_with_more_descenders():
    """A hand with descenders and no ascenders turns the core-band cue against the
    truth. The ragged-edge cue is off by default (registers justify their lines); with
    its weight set, it carries the vote."""
    image = Image.new("L", (1000, 1400), PAPER)
    write_block(ImageDraw.Draw(image), (100, 120, 900, 1280), seed=2, ascenders=0.0, descenders=0.3)
    with_ragged = {"ragged_score_weight": 12.0}
    upright = detect_orientation(image, with_ragged)
    assert upright["value"] == 0
    assert "ascenders against descenders -" in upright["evidence"]
    assert detect_orientation(turned(image, 2), with_ragged)["value"] == 2


def test_page_on_a_backdrop_that_is_most_of_the_frame_is_not_a_negative():
    """The dark class is most of the frame here, so only the erosion test tells the
    broad light page from thin light strokes."""
    frame = Image.new("L", (1800, 2200), 25)
    frame.paste(page(), (400, 400))
    result = detect_orientation(frame)
    assert result["value"] == 0
    assert result["flags"] == []


def _textured_backdrop_frame() -> Image.Image:
    """A page between two dark backdrop bands with a pattern of small light holes: the
    holes break every long straight run, so only the dark-border trim removes the
    bands."""
    frame = Image.new("L", (1500, 1400), PAPER)
    frame.paste(page(), (250, 0))
    draw = ImageDraw.Draw(frame)
    for left in (0, 1250):
        draw.rectangle((left, 0, left + 249, 1399), fill=30)
        for y in range(0, 1400, 20):
            for x in range(left + (y // 20 % 2) * 7, left + 250, 20):
                draw.rectangle((x, y, x + 3, y + 3), fill=PAPER)
    return frame


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_textured_dark_border_is_trimmed_before_scoring(quarter_turns):
    result = detect_orientation(turned(_textured_backdrop_frame(), quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []


# --- Dense old cursive (follow-up to brief 0021: every real page was flagged) ----------


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_dense_cursive_spread_with_gutter_and_show_through_is_oriented(quarter_turns):
    """Two facing pages of close cursive whose ascenders and descenders reach the next
    line, with flourishes, a signature, show-through, a gutter shadow, page-edge stacks
    and a dark backdrop; their lines are out of step and lean apart. Whole-frame
    profiles see little difference between rows and columns here."""
    result = detect_orientation(turned(cursive_spread(), quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []
    assert result["confidence"] > SETTING_CONFIDENCE


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_dense_cursive_page_with_gutter_strip_is_oriented(quarter_turns):
    result = detect_orientation(turned(cursive_page(seed=12), quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []


def test_ascender_cue_decides_when_the_baseline_cue_is_silent():
    """Unjoined round letters have edges as sharp at the x-line as at the baseline, so
    only ascenders tell upright from upside down."""
    image = Image.new("L", (1000, 1400), PAPER)
    write_block(ImageDraw.Draw(image), (100, 120, 900, 1280), seed=2, joined=False, descenders=0.0)
    upright = detect_orientation(image)
    assert upright["value"] == 0 and upright["flags"] == []
    assert re.search(r"baseline against x-line [+-]0\.0", upright["evidence"])
    assert detect_orientation(turned(image, 2))["value"] == 2


# --- Small printed type (follow-up to brief 0028: confident wrong half turns) ----------


def _right_or_flagged(result: dict, expected: int) -> bool:
    return result["value"] == expected and not result["flags"] or bool(result["flags"])


@pytest.mark.parametrize(
    ("dpi", "points", "seed", "serif"),
    [(150, 9, s, True) for s in range(3)]
    + [(150, 8, 2, True), (150, 9, 0, False), (300, 7, 0, True), (300, 7, 1, False)],
)
@pytest.mark.parametrize("quarter_turns", [0, 2])
def test_small_printed_type_is_never_turned_wrong_without_a_flag(
    dpi, points, seed, serif, quarter_turns
):
    """At an x-height of 6 to 8 px in the working copy, the x-band of printed type has
    dense top and bottom rows and a dense e crossbar with sparser rows between, so a
    core band found with one share for the whole strip splits each line in pieces."""
    image = turned(printed_page(dpi, points, seed, serif=serif), quarter_turns)
    result = detect_orientation(image)
    assert _right_or_flagged(result, (4 - quarter_turns) % 4), result["evidence"]


@pytest.mark.parametrize(
    ("dpi", "points", "serif"), [(300, 7, True), (300, 7, False), (150, 9, True)]
)
@pytest.mark.parametrize("quarter_turns", [0, 2])
def test_small_printed_spread_is_never_turned_wrong_without_a_flag(
    dpi, points, serif, quarter_turns
):
    image = turned(printed_spread(dpi, points, seed=1, serif=serif), quarter_turns)
    result = detect_orientation(image)
    assert _right_or_flagged(result, (4 - quarter_turns) % 4), result["evidence"]


def _updown_score(result: dict) -> float:
    match = re.search(r"up-down score ([+-][0-9.]+)", result["evidence"])
    assert match, result["evidence"]
    return float(match.group(1))


@pytest.mark.parametrize("image", [page(), cursive_page(seed=12)], ids=["page", "cursive"])
def test_up_down_score_changes_sign_under_a_half_turn(image):
    """The score of a frame is exactly minus the score of its half turn; the turned
    input differs from the frame only by its reduction, so the two answers agree to
    within a few percent."""
    upright = _updown_score(detect_orientation(image))
    flipped = _updown_score(detect_orientation(turned(image, 2)))
    assert upright > 0 > flipped
    assert abs(upright + flipped) <= 0.05 * abs(upright)


def test_up_down_score_of_a_frame_is_antisymmetric_exactly():
    ink = Image.new("L", (1000, 1400), 0)
    sample = page().point(lambda level: 255 if level < 128 else 0)
    ink.paste(sample)
    value = setting_values()
    forward = _updown(ink, value)["score"]
    backward = _updown(ink.transpose(Image.Transpose.ROTATE_180), value)["score"]
    assert forward == pytest.approx(-backward, abs=1e-9)


@pytest.mark.parametrize("quarter_turns", [0, 2])
def test_up_down_vote_that_changes_sign_with_the_core_band_is_flagged(quarter_turns):
    """On this small sans page the estimates run from strongly upside down to strongly
    upright as the core band share and strip width vary: the vote measures where the
    core band is put more than the page, so the answer is uncertain even though the
    median happens to be right."""
    result = detect_orientation(turned(printed_page(150, 9, 0, serif=False), quarter_turns))
    assert result["value"] == 0
    assert result["flags"] == [UNCERTAIN_CORE_BAND]
    assert result["confidence"] < 0.2
