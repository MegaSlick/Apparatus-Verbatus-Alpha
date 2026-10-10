"""Orientation detector on synthetic pages drawn here; no real register
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
    account_page,
    cursive_page,
    cursive_spread,
    figure_page,
    hand_account_page,
    notes_page,
    page,
    printed_page,
    printed_spread,
    turned,
    words_over_figures_page,
    write_block,
)
from pagekit.orient import (
    POSSIBLY_NEGATIVE,
    UNCERTAIN_COLUMNS,
    UNCERTAIN_CORE_BAND,
    UNCERTAIN_DIRECTION,
    UNCERTAIN_FIGURE_TILES,
    UNCERTAIN_UNIFORM,
    UNCERTAIN_UPDOWN,
    _guarded,
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
    # Boxes of one height are uniform marks; the flag names that reason.
    assert result["flags"] == [UNCERTAIN_UNIFORM]
    assert result["confidence"] < 0.2
    assert "run across" in result["evidence"]


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


# --- Dense old cursive: real pages are not all flagged ----------


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
    """Unjoined round letters of varying width have edges as sharp at the x-line as at
    the baseline, so only ascenders tell upright from upside down."""
    image = Image.new("L", (1000, 1400), PAPER)
    write_block(
        ImageDraw.Draw(image),
        (100, 120, 900, 1280),
        seed=2,
        joined=False,
        descenders=0.0,
        narrow=4,
    )
    upright = detect_orientation(image)
    assert upright["value"] == 0 and upright["flags"] == []
    assert re.search(r"baseline against x-line [+-]0\.0", upright["evidence"])
    assert detect_orientation(turned(image, 2))["value"] == 2


# --- Small printed type: no confident wrong half turns ----------


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


# --- Figures only: tables are not turned silently ----------


@pytest.mark.parametrize("style", ["mono", "plain", "round"])
@pytest.mark.parametrize(
    ("spacing", "aligned"), [(1.2, True), (1.6, True), (2.4, False)], ids=["tight", "mid", "loose"]
)
@pytest.mark.parametrize("dpi", [150, 300])
def test_page_of_figures_is_never_turned_wrong_without_a_flag(style, spacing, aligned, dpi):
    """Figures in columns stack exactly, so the columns look like lines, and lining
    figures have no ascenders or descenders to tell up from down: such a page must be
    flagged, never decided silently, in every turn."""
    image = figure_page(
        dpi, seed=dpi + round(10 * spacing), style=style, spacing=spacing, aligned=aligned
    )
    for quarter_turns in range(4):
        result = detect_orientation(turned(image, quarter_turns))
        assert _right_or_flagged(result, (4 - quarter_turns) % 4), (
            quarter_turns,
            result["evidence"],
        )


def test_marks_of_many_sizes_balanced_about_the_line_are_too_weak_to_turn():
    """Lines across of word boxes of varied heights, each centred on its line and the
    lines centred on the page: the marks are not uniform, but nothing tells upright
    from upside down."""
    image = Image.new("L", (1000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    for row, middle in enumerate(range(160, 1260, 48)):
        widths = [20 + (row * 7 + i * 13) % 50 for i in range(9)]
        heights = [8 + (row * 5 + i * 3) % 12 for i in range(9)]
        total = sum(widths) + 14 * (len(widths) - 1)
        x = (1000 - total) // 2
        for w, h in zip(widths, heights, strict=True):
            draw.rectangle((x, middle - h, x + w, middle + h), outline=45, width=3)
            x += w + 14
    result = detect_orientation(image)
    assert result["value"] == 0
    assert result["flags"] == [UNCERTAIN_UPDOWN]
    assert detect_orientation(turned(image, 2))["flags"] == [UNCERTAIN_UPDOWN]


@pytest.mark.parametrize(
    ("dissent", "flagged"), [(-1.25, True), (-1.0, True), (-0.75, False), (0.3, False)]
)
def test_sign_guard_trips_at_one_standard_error_of_dissent(dissent, flagged):
    """Six estimates whose median is clearly upright, with one dissenting estimate:
    a dissent of a standard error or more makes the answer uncertain, a smaller one
    does not. Loosening the guard past 1.25 or tightening it below 0.75 fails this."""
    estimates = [6.0, 5.5, 4.8, 4.2, 3.9, dissent]
    middle, disagree = _guarded(estimates, setting_values())
    assert middle > 0
    assert disagree is flagged


# --- Account pages: words beside columns of figures ----------


@pytest.mark.parametrize(
    ("style", "serif"), [("mono", True), ("plain", False), ("round", True)], ids=str
)
@pytest.mark.parametrize(
    ("dpi", "spacing", "words_first"),
    [(300, 1.3, True), (150, 1.6, False), (300, 2.0, True)],
    ids=["300-tight-words-left", "150-mid-words-between", "300-loose-words-left"],
)
def test_account_page_is_never_turned_wrong_without_a_flag(style, serif, dpi, spacing, words_first):
    """A column of words beside three columns of right-aligned amounts: the words make
    the page as a whole too varied for the figure rule, while tiles of figures, which
    stack as exactly down a column as along a line, can outvote the words on the line
    direction."""
    image = account_page(
        dpi,
        seed=dpi + round(10 * spacing),
        style=style,
        serif=serif,
        spacing=spacing,
        words_first=words_first,
    )
    for quarter_turns in range(4):
        result = detect_orientation(turned(image, quarter_turns))
        assert _right_or_flagged(result, (4 - quarter_turns) % 4), (
            quarter_turns,
            result["evidence"],
        )


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_margin_notes_turned_a_quarter_are_outvoted_and_the_page_decided(quarter_turns):
    """About a third of the tiles of writing vote the other way here (notes 300 px
    wide); the page is still decided. Fails if tile_dissent_share is set below 0.35."""
    result = detect_orientation(turned(notes_page(8, notes_width=300), quarter_turns))
    assert result["value"] == (4 - quarter_turns) % 4
    assert result["flags"] == []


@pytest.mark.parametrize("quarter_turns", [0, 1])
def test_block_turned_a_quarter_as_large_as_the_text_leaves_the_direction_open(quarter_turns):
    """With notes 500 px wide whose lines touch (so they form one wide block, not narrow
    columns of items), 46% of the tiles vote the other way: the line direction is
    flagged, not decided. Fails if tile_dissent_share is set at 0.46 or above."""
    result = detect_orientation(
        turned(notes_page(8, notes_width=500, notes_pitch=30), quarter_turns)
    )
    assert result["value"] == 0
    assert result["flags"] == [UNCERTAIN_DIRECTION]
    assert "vote against the median" in result["evidence"]


# --- Handwritten account pages ----------


def _score_or_none(result: dict):
    match = re.search(r"up-down score ([+-][0-9.]+)", result["evidence"])
    return float(match.group(1)) if match else None


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("dpi", [150, 300])
def test_one_size_figures_do_not_vote_on_up_and_down(seed, dpi):
    """Handwritten words that lean upright (more ascenders than descenders) beside six
    columns of one-size figures with tails below the line. Tiles of the figures are left
    out of the line direction; their marks must be left out of the up-down strips too,
    or the tails pull the vote toward upside down (to about -1.1 here before)."""
    image = hand_account_page(
        dpi, seed, words_share=0.5, columns=6, extenders=(0.2, 0.15), tails="2345679"
    )
    score = _score_or_none(detect_orientation(image))
    assert score is None or score > -0.3


@pytest.mark.parametrize("dpi", [150, 300])
@pytest.mark.parametrize("words_share", [0.25, 0.35, 0.5])
@pytest.mark.parametrize("columns", [4, 6])
def test_handwritten_account_page_is_never_turned_wrong_without_a_flag(dpi, words_share, columns):
    image = hand_account_page(
        dpi, 1, words_share=words_share, columns=columns, extenders=(0.2, 0.15), tails="2345679"
    )
    for quarter_turns in range(4):
        result = detect_orientation(turned(image, quarter_turns))
        assert _right_or_flagged(result, (4 - quarter_turns) % 4), (
            quarter_turns,
            result["evidence"],
        )


def test_page_mostly_of_one_size_figure_tiles_is_flagged_and_a_page_of_few_is_not():
    """Lines of writing over rows of separate figures across the full width (wide runs,
    so the column rule does not apply). With the figures over half the height, 52% of
    the tiles holding writing are one-size tiles: flagged. With them over 30% of it,
    31%: decided. Fails if uniform_tile_share is set at 0.31 or below, or above 0.52."""
    many = detect_orientation(words_over_figures_page(1, words_share=0.5))
    assert many["flags"] == [UNCERTAIN_FIGURE_TILES]
    few = detect_orientation(words_over_figures_page(1, words_share=0.7))
    assert few["value"] == 0 and few["flags"] == []


@pytest.mark.parametrize("dpi", [150, 300])
@pytest.mark.parametrize("spacing", [0.6, 0.8, 1.0])
@pytest.mark.parametrize("seed", [1, 2])
def test_account_page_of_joined_figures_is_never_turned_wrong_without_a_flag(dpi, spacing, seed):
    """Words across a fifth of the width beside six columns of handwritten amounts whose
    figures run together: the amounts are marks of many sizes, so few tiles look like
    one-size figures, and the columns, stacked closely, read as lines running down."""
    image = hand_account_page(dpi, seed, words_share=0.2, columns=6, joined=True, spacing=spacing)
    for quarter_turns in range(4):
        result = detect_orientation(turned(image, quarter_turns))
        assert _right_or_flagged(result, (4 - quarter_turns) % 4), (
            quarter_turns,
            result["evidence"],
        )


def test_page_mostly_of_narrow_columns_is_flagged_and_one_with_a_large_block_is_not():
    """Joined handwritten amounts in six columns beside words across a fifth of the
    width: about 62% of the ink is in the narrow columns, flagged. Beside words across
    35% of the width: about 47%, not flagged for that reason (only the block of words
    votes). Fails if columns_ink_share is set at 0.47 or below, or above 0.62."""
    many = detect_orientation(hand_account_page(150, 2, words_share=0.2, columns=6, joined=True))
    assert many["flags"] == [UNCERTAIN_COLUMNS]
    few = detect_orientation(hand_account_page(150, 2, words_share=0.35, columns=6, joined=True))
    assert UNCERTAIN_COLUMNS not in few["flags"]
    assert "narrow columns" not in few["evidence"]


def _seal_page() -> Image.Image:
    image = page()
    ImageDraw.Draw(image).ellipse((650, 1150, 900, 1380), fill=60)
    return image


@pytest.mark.parametrize("which", ["seal", "spread"])
@pytest.mark.parametrize("quarter_turns", [0, 1])
def test_ordinary_writing_is_not_read_as_narrow_columns(which, quarter_turns):
    """A page of writing with a wax seal (a broad mark that makes a wide run of its
    own), and a spread with lines of writing running across the gutter: their lines are
    narrow runs of words lying along the run, not columns of items, so the columns
    rule must not flag them."""
    from pagekit._split_testspreads import spread_case

    image = _seal_page() if which == "seal" else spread_case("writing_across", 150, 2).image
    result = detect_orientation(turned(image, quarter_turns))
    assert UNCERTAIN_COLUMNS not in result["flags"], result["evidence"]
