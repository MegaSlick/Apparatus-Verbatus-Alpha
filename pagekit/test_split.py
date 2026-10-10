"""Page count and split detector on synthetic frames drawn here; no real
register material."""

from __future__ import annotations

import math
import random

import pytest
from PIL import Image, ImageChops, ImageDraw

from pagekit._orient_ink import ANSWER_KEYS, TOO_LITTLE_INK, DetectorError
from pagekit._orient_testpages import PAPER, page, spread, turned, write_block
from pagekit._split_testspreads import (
    KINDS,
    SHEET_KINDS,
    crossing_case,
    sheet_case,
    single_page_case,
    spread_case,
)
from pagekit.split import detect_split

DPI = (300, 300)
VALUE_KEYS = {"pages", "cut", "method", "part", "neighbour"}


def _check(image: Image.Image, **kwargs) -> dict:
    kwargs.setdefault("dpi", DPI)
    # These tests were written against a 5 mm overlap; the core's own setting
    # (thresholds_prepare.toml) is used only when a test leaves this out on purpose.
    kwargs.setdefault("overlap_mm", 5.0)
    result = detect_split(image, **kwargs)
    assert tuple(result) == ANSWER_KEYS
    assert set(result["value"]) == VALUE_KEYS
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["evidence"]
    return result


def _x_at(cut: list[list[float]], y: float) -> float:
    (x0, y0), (x1, y1) = cut
    return x0 + (x1 - x0) * (y - y0) / (y1 - y0)


def _fold(image: Image.Image, top: float, bottom: float | None = None) -> Image.Image:
    draw = ImageDraw.Draw(image)
    draw.line((top, 0, top if bottom is None else bottom, image.height - 1), fill=50, width=4)
    return image


def _two_pages(result: dict, method: str) -> list[list[float]]:
    assert result["value"]["pages"] == 2
    assert result["value"]["method"] == method
    assert result["value"]["neighbour"] is None
    return result["value"]["cut"]


def test_spread_with_thin_fold_line_is_cut_on_the_line():
    result = _check(_fold(spread(), 1000))
    cut = _two_pages(result, "fold")
    assert result["value"]["part"] == "line"
    assert abs(_x_at(cut, 0) - 1000) <= 3 and abs(_x_at(cut, 1399) - 1000) <= 3
    assert result["flags"] == []
    assert "fold line" in result["evidence"]


def test_spread_with_soft_shadow_is_cut_in_the_valley():
    image = spread(gutter=(860, 1120))
    shade = Image.new("L", (2000, 1))
    shade.putdata(
        [round(255 * (1 - 0.35 * math.exp(-(((x - 990) / 45) ** 2) / 2))) for x in range(2000)]
    )
    image = ImageChops.multiply(image, shade.resize(image.size))
    result = _check(image)
    cut = _two_pages(result, "fold")
    assert result["value"]["part"] == "valley"
    assert abs(_x_at(cut, 700) - 990) <= 15
    assert result["flags"] == []


def test_spread_with_no_fold_is_cut_in_the_gap():
    """Two pages and a cut in the gap. The paper here runs unbroken across the gap (no
    line, shadow, tone step or edge), so the answer is also flagged for review: an empty band
    alone may be the middle of one sheet."""
    result = _check(spread(gutter=(920, 1080)))
    cut = _two_pages(result, "gap")
    assert result["value"]["part"] is None
    for y in (0, 700, 1399):
        assert 870 < _x_at(cut, y) < 1080
    assert len(result["flags"]) == 1 and EMPTY_BAND in result["flags"][0]


def test_leaning_fold_gives_a_leaning_cut_on_the_drawn_line():
    result = _check(_fold(spread(), 980, 1025))
    cut = _two_pages(result, "fold")
    assert abs(cut[0][0] - 980) <= 4
    assert abs(cut[1][0] - 1025) <= 4


def test_leaning_spread_without_fold_gives_a_cut_leaning_with_the_gap():
    # Turned 1.5 degrees counterclockwise: the gutter's top moves left of its bottom.
    image = spread().rotate(1.5, resample=Image.Resampling.BICUBIC, fillcolor=PAPER)
    result = _check(image)
    cut = _two_pages(result, "gap")
    drift = cut[1][0] - cut[0][0]
    expected = 1399 * math.tan(math.radians(1.5))
    assert abs(drift - expected) <= 12


def test_single_page_on_wide_pale_backdrop_is_one_page_without_flag():
    frame = Image.new("L", (2000, 1400), 200)
    frame.paste(page((900, 1300)), (550, 50))
    result = _check(frame)
    assert result["value"] == {
        "pages": 1,
        "cut": None,
        "method": "none",
        "part": None,
        "neighbour": None,
    }
    assert result["flags"] == []
    assert "suggest 2" in result["evidence"]


def test_single_page_is_one_page_without_flag():
    result = _check(page())
    assert result["value"]["pages"] == 1
    assert result["flags"] == []


def _book_edge_frame(neighbour_writing: bool) -> Image.Image:
    frame = Image.new("L", (1300, 1400), PAPER)
    draw = ImageDraw.Draw(frame)
    if neighbour_writing:
        write_block(draw, (-300, 120, 90, 1280), seed=7)
    else:
        draw.rectangle((0, 0, 89, 1399), fill=190)
    draw.rectangle((90, 0, 120, 1399), fill=40)
    write_block(draw, (230, 120, 1200, 1280), seed=8)
    return frame


def test_book_edge_with_writing_cut_off_is_one_page_with_neighbour_flag():
    result = _check(_book_edge_frame(neighbour_writing=True))
    value = result["value"]
    assert value["pages"] == 1 and value["cut"] is None
    assert value["method"] == "neighbour edge"
    assert value["neighbour"]["side"] == "left"
    assert abs(value["neighbour"]["line"][0][0] - 120) <= 4
    assert len(result["flags"]) == 1
    assert result["flags"][0].startswith("neighbour strip on the left")
    assert "cut off by the frame edge" in result["flags"][0]


def test_bare_book_edge_is_one_page_and_no_cut_at_the_edge():
    """Decision: a page-edge line with nothing beyond it is a book edge, not a neighbour
    strip, so it raises no flag."""
    result = _check(_book_edge_frame(neighbour_writing=False))
    assert result["value"]["pages"] == 1
    assert result["value"]["method"] == "none"
    assert result["flags"] == []


def test_writing_cut_off_at_one_side_without_an_edge_line_is_a_neighbour_strip():
    frame = Image.new("L", (1300, 1400), PAPER)
    draw = ImageDraw.Draw(frame)
    write_block(draw, (40, 120, 1100, 1280), seed=4)
    write_block(draw, (1220, 120, 1700, 1280), seed=5)
    result = _check(frame)
    assert result["value"]["pages"] == 1
    assert result["value"]["neighbour"]["side"] == "right"
    line = result["value"]["neighbour"]["line"]
    assert line is not None and 1100 < line[0][0] < 1220


def test_writing_cut_off_at_both_sides_is_flagged_without_a_side():
    frame = Image.new("L", (1300, 1400), PAPER)
    write_block(ImageDraw.Draw(frame), (-200, 120, 1600, 1280), seed=6, ragged=(1.0, 1.0))
    result = _check(frame)
    assert result["value"]["pages"] == 1
    assert result["value"]["neighbour"] is None
    assert result["value"]["method"] == "none"
    assert any("both sides" in flag for flag in result["flags"])


def test_spread_whose_cues_disagree_is_one_page_with_a_flag():
    image = Image.new("L", (2000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (100, 120, 1200, 1280), seed=3)
    write_block(draw, (1360, 120, 1920, 1280), seed=4)
    result = _check(_fold(image, 900))
    assert result["value"]["pages"] == 1
    assert result["value"]["cut"] is None
    assert len(result["flags"]) == 1 and "disagree" in result["flags"][0]


def test_gap_that_does_not_balance_the_halves_is_flagged_not_cut():
    image = Image.new("L", (2000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (100, 120, 920, 1280), seed=3)
    write_block(draw, (1080, 120, 1900, 300), seed=4)
    result = _check(image)
    assert result["value"]["pages"] == 1
    assert len(result["flags"]) == 1 and "unbalanced" in result["flags"][0]


def test_gap_alone_against_tall_proportions_goes_to_review():
    image = Image.new("L", (1000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (60, 120, 440, 1280), seed=3, letter=8, ragged=(0.8, 1.0))
    write_block(draw, (560, 120, 950, 1280), seed=4, letter=8, ragged=(0.8, 1.0))
    result = _check(image)
    assert result["value"]["pages"] == 1
    assert len(result["flags"]) == 1 and "proportions suggest one page" in result["flags"][0]


def _note(image: Image.Image, reach_past_fold: int) -> Image.Image:
    draw = ImageDraw.Draw(image)
    draw.line((880, 700, 1000 + reach_past_fold, 700), fill=45, width=4)
    draw.ellipse((880, 686, 900, 700), outline=45, width=3)
    return image


def test_marginal_note_crossing_the_fold_is_counted_in_the_evidence():
    result = _check(_note(_fold(spread(), 1000), 20))
    _two_pages(result, "fold")
    assert "1 ink mark crosses the cut (kept mostly on the left side)" in result["evidence"]
    assert result["flags"] == []


def test_marginal_note_overhanging_the_cut_beyond_the_overlap_is_flagged():
    result = _check(_note(_fold(spread(), 1000), 160))
    _two_pages(result, "fold")
    assert "1 ink mark crosses the cut" in result["evidence"]
    assert len(result["flags"]) == 1 and "more than the 5 mm overlap" in result["flags"][0]
    # A wider overlap keeps the note whole on its page.
    assert _check(_note(_fold(spread(), 1000), 160), overlap_mm=20.0)["flags"] == []


def test_crossing_note_without_resolution_cannot_be_checked_and_is_flagged():
    image = _note(_fold(spread(), 1000), 20)
    result = _check(image, dpi=None)
    assert any("resolution is unknown" in flag for flag in result["flags"])


def test_split_runs_on_the_upright_frame_after_the_turn():
    upright = _check(_fold(spread(), 1000))
    sideways = _check(turned(_fold(spread(), 1000), 1), turns=3)
    assert sideways["value"] == upright["value"]


def test_full_size_spread_is_reduced_and_cut_in_its_own_grid():
    image = _fold(spread(), 1000).resize((4500, 3150))
    cut = _two_pages(_check(image), "fold")
    assert abs(_x_at(cut, 1575) - 2250) <= 8
    assert cut[1][1] == 3149.0


def test_blank_frame_is_one_page_with_too_little_ink_flag():
    result = _check(Image.new("L", (2000, 1400), PAPER))
    assert result["value"]["pages"] == 1
    assert result["confidence"] == 0.0
    assert result["flags"] == [TOO_LITTLE_INK]


def test_same_input_gives_the_same_answer():
    image = _note(_fold(spread(seed=4), 990, 1010), 40)
    assert detect_split(image, dpi=DPI) == detect_split(image, dpi=DPI)


def test_bad_turns_are_refused():
    with pytest.raises(DetectorError):
        detect_split(page(), turns=4)


# --- Ruled lines and proportions ------------------------------------------------


def _ruled_page() -> Image.Image:
    """Case A: a portrait single page with a ruled line down its middle, writing on both
    sides of it running through it."""
    image = page()
    ImageDraw.Draw(image).line((500, 0, 500, 1399), fill=50, width=4)
    return image


def _ruled_landscape() -> Image.Image:
    """Case B: a landscape single page whose writing runs across a ruled line."""
    image = Image.new("L", (1800, 1300), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (100, 100, 1700, 1200), seed=5)
    draw.line((900, 0, 900, 1299), fill=50, width=4)
    return image


def test_rule_on_a_portrait_single_page_is_not_cut_as_a_fold():
    result = _check(_ruled_page())
    assert result["value"]["pages"] == 1
    assert result["value"]["cut"] is None
    assert len(result["flags"]) == 1


def test_rule_crossed_by_writing_on_a_landscape_page_is_not_cut_as_a_fold():
    result = _check(_ruled_landscape())
    assert result["value"]["pages"] == 1
    assert result["value"]["cut"] is None
    assert any("crossed by" in flag for flag in result["flags"])


def test_fold_line_alone_against_tall_proportions_goes_to_review():
    """A clean line with no writing crossing it and no gap around it, in a portrait
    frame: as with a gap alone, the proportions veto a cut on the line alone."""
    image = Image.new("L", (1000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (60, 120, 490, 1280), seed=3, letter=8, ragged=(0.97, 1.0))
    write_block(draw, (510, 120, 950, 1280), seed=4, letter=8, ragged=(0.97, 1.0))
    draw.line((500, 0, 500, 1399), fill=50, width=4)
    result = _check(image)
    assert result["value"]["pages"] == 1
    assert len(result["flags"]) == 1
    assert "proportions suggest one page" in result["flags"][0]


@pytest.mark.parametrize("seed", range(12))
def test_binary_noise_is_never_a_confident_fold(seed):
    rng = random.Random(seed)
    noise = Image.new("L", (2000, 1400))
    noise.putdata([PAPER if rng.random() < 0.5 else 40 for _ in range(2000 * 1400)])
    result = _check(noise)
    assert result["value"]["method"] != "fold"
    assert result["value"]["pages"] == 1


@pytest.mark.parametrize("seed", range(4))
def test_dense_writing_is_not_a_fold_line_or_a_weak_one(seed):
    image = Image.new("L", (2000, 1400), PAPER)
    write_block(
        ImageDraw.Draw(image),
        (60, 60, 1940, 1340),
        seed=seed,
        pitch=30,
        core=12,
        rise=8,
        letter=9,
        ragged=(0.95, 1.0),
    )
    result = _check(image)
    assert result["value"]["method"] != "fold"
    assert not any("thin line" in flag for flag in result["flags"])
    assert not any("neighbour" in flag for flag in result["flags"])


def test_spread_with_one_blank_page_and_no_fold_is_flagged():
    image = Image.new("L", (2000, 1400), PAPER)
    write_block(ImageDraw.Draw(image), (100, 120, 920, 1280), seed=3)
    result = _check(image)
    assert result["value"]["pages"] == 1
    assert len(result["flags"]) == 1
    assert "possible spread with a blank page" in result["flags"][0]
    mirrored = _check(image.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    assert "possible spread with a blank page" in mirrored["flags"][0]


# --- Tests that each fail under one mutation the review found unguarded ---------------


def test_fold_line_and_shadow_valley_in_different_places_disagree():
    image = _fold(spread(gutter=(920, 1080)), 1000)
    shade = Image.new("L", (2000, 1))
    shade.putdata(
        [round(255 * (1 - 0.35 * math.exp(-(((x - 1400) / 45) ** 2) / 2))) for x in range(2000)]
    )
    result = _check(ImageChops.multiply(image, shade.resize(image.size)))
    assert result["value"]["pages"] == 1
    assert len(result["flags"]) == 1
    assert "shadow valley" in result["flags"][0] and "disagree" in result["flags"][0]


def test_line_in_the_edge_band_is_never_a_fold():
    image = Image.new("L", (2000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (400, 120, 1900, 1280), seed=3)
    draw.line((200, 0, 200, 1399), fill=50, width=4)
    result = _check(image)
    assert result["value"]["pages"] == 1
    assert result["value"]["cut"] is None


def test_frame_of_rules_only_is_too_little_ink_after_masking():
    image = Image.new("L", (2000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    for y in range(100, 1300, 48):
        draw.line((60, y, 1940, y), fill=60, width=3)
    result = _check(image)
    assert result["value"]["pages"] == 1
    assert result["confidence"] == 0.0
    assert result["flags"] == [TOO_LITTLE_INK]


def test_rule_across_the_gutter_touching_the_writing_is_masked():
    """A rule running across both pages, with letters sitting on it, would join the two
    pages' writing into one mark and close the gap if it were not masked."""
    image = spread(gutter=(920, 1080))
    baseline = 120 + 13 + 14 + 48 * 10
    ImageDraw.Draw(image).line((40, baseline, 1960, baseline), fill=60, width=3)
    result = _check(image)
    cut = _two_pages(result, "gap")
    assert 870 < _x_at(cut, 700) < 1080


def test_note_crossing_a_leaning_fold_is_kept_and_counted():
    """A leaning fold is not caught by the axis-aligned mask; masking along the found
    line is what keeps a note crossing it as its own mark."""
    image = _fold(spread(), 980, 1025)
    draw = ImageDraw.Draw(image)
    draw.line((880, 700, 1020, 700), fill=45, width=4)
    draw.ellipse((880, 686, 900, 700), outline=45, width=3)
    result = _check(image)
    _two_pages(result, "fold")
    assert "1 ink mark crosses the cut (kept mostly on the left side)" in result["evidence"]


def test_widest_balanced_gap_is_preferred():
    image = Image.new("L", (2000, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    write_block(draw, (100, 120, 700, 1280), seed=3, ragged=(0.99, 1.0))
    write_block(draw, (780, 120, 900, 1280), seed=4, ragged=(0.99, 1.0))
    write_block(draw, (1100, 120, 1900, 1280), seed=5, ragged=(0.99, 1.0))
    result = _check(image)
    cut = _two_pages(result, "gap")
    assert 900 < _x_at(cut, 700) < 1100


def test_proportions_against_the_evidence_lower_the_confidence():
    image = _fold(spread(), 1000)
    wide = _check(image)
    tall = _check(image, dpi=(300, 150))
    assert tall["value"]["pages"] == 2
    assert "suggest 1" in tall["evidence"]
    assert tall["confidence"] == pytest.approx(wide["confidence"] * 0.8, abs=0.002)
    # A single page on a wide pale backdrop: its writing spans less than half the
    # frame, so the one-page answer stands (no flag) with the factor applied.
    frame = Image.new("L", (2000, 1400), 200)
    frame.paste(page((900, 1300)), (550, 50))
    wide_single = _check(frame)
    tall_single = _check(frame, dpi=(600, 300))
    assert "suggest 2" in wide_single["evidence"] and "suggest 1" in tall_single["evidence"]
    assert wide_single["flags"] == [] and tall_single["flags"] == []
    assert wide_single["confidence"] == pytest.approx(tall_single["confidence"] * 0.8, abs=0.002)


def test_note_overhanging_from_the_right_is_flagged_by_its_left_reach():
    image = _fold(spread(), 1000)
    draw = ImageDraw.Draw(image)
    draw.line((1000 - 160, 700, 1120, 700), fill=45, width=4)
    draw.ellipse((1100, 686, 1120, 700), outline=45, width=3)
    draw.line((1040, 690, 1120, 690), fill=45, width=4)
    result = _check(image)
    _two_pages(result, "fold")
    assert "kept mostly on the right side" in result["evidence"]
    assert len(result["flags"]) == 1 and "more than the 5 mm overlap" in result["flags"][0]


def test_backdrop_notch_at_the_top_of_the_gutter_is_not_writing_across_the_cut():
    """Where the two pages' top edges curve down into the binding, the dark backdrop
    reaches into the gutter as a wedge. It joins the backdrop band, so it is backdrop,
    not a flourish across the fold."""
    image = _fold(spread(gutter=(900, 1100)), 1000)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1999, 59), fill=30)
    draw.polygon([(900, 59), (1100, 59), (1000, 140)], fill=30)
    result = _check(image)
    _two_pages(result, "fold")
    assert result["flags"] == []
    assert "joined to a dark band" in result["evidence"]


def _band_and_flourish(gap: int, bottom: bool = False) -> Image.Image:
    """A fold at x=1000, a dark backdrop band at the top (or bottom), and a 5 px pen
    stroke from the left page across the fold, reaching up to (or `gap` px short of)
    the band."""
    image = _fold(spread(), 1000)
    draw = ImageDraw.Draw(image)
    if bottom:
        draw.rectangle((0, 1340, 1999, 1399), fill=30)
        edge, sign = 1339 - gap, -1
    else:
        draw.rectangle((0, 0, 1999, 59), fill=30)
        edge, sign = 60 + gap, 1
    points = [(880, edge + sign * 60), (950, edge + sign * 20), (1000, edge)]
    points += [(1060, edge + sign * 15), (1160, edge + sign * 55)]
    draw.line(points, fill=40, width=5, joint="curve")
    return image


@pytest.mark.parametrize(
    ("gap", "bottom"), [(0, False), (3, False), (0, True)], ids=["touching", "3px", "bottom"]
)
def test_pen_stroke_touching_the_backdrop_band_still_counts_as_writing_across_the_cut(gap, bottom):
    result = _check(_band_and_flourish(gap, bottom))
    _two_pages(result, "fold")
    assert len(result["flags"]) == 1
    assert "more than the 5 mm overlap" in result["flags"][0]


def test_pen_stroke_touching_a_small_backdrop_bump_still_counts_as_writing():
    """The backdrop band has a small bump where the flourish touches it: the bump is
    thick, the stroke is not, so only the bump is excused."""
    image = _band_and_flourish(gap=22)
    ImageDraw.Draw(image).ellipse((975, 30, 1025, 84), fill=30)
    result = _check(image)
    _two_pages(result, "fold")
    assert len(result["flags"]) == 1
    assert "more than the 5 mm overlap" in result["flags"][0]


def test_pen_stroke_ending_in_a_blot_and_touching_the_band_still_counts_as_writing():
    image = _band_and_flourish(gap=0)
    ImageDraw.Draw(image).ellipse((1150, 102, 1176, 128), fill=40)
    result = _check(image)
    _two_pages(result, "fold")
    assert len(result["flags"]) == 1
    assert "more than the 5 mm overlap" in result["flags"][0]


def _mm(dpi: int, value: float) -> int:
    return round(value * dpi / 25.4)


def _a4_spread_with_band_stroke(dpi: int, below_mm: float) -> Image.Image:
    """An A4 spread with a fold, a 5 mm dark backdrop band along the top, a 3 x 3 mm
    bump of backdrop 8 mm left of the fold, and a 0.6 mm pen stroke 33 mm long running
    `below_mm` under the band across the fold, joined to the bump."""
    width, height = 2 * _mm(dpi, 210), _mm(dpi, 297)
    image = Image.new("L", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    middle = width // 2
    band = _mm(dpi, 5)
    margin, inner = _mm(dpi, 25), _mm(dpi, 10)
    scale = dpi / 150
    for left, right, seed in ((margin, middle - inner, 3), (middle + inner, width - margin, 4)):
        write_block(
            draw,
            (left, band + margin, right, height - margin),
            seed,
            pitch=round(48 * scale),
            core=round(14 * scale),
            rise=round(13 * scale),
            letter=round(10 * scale),
            stroke=max(1, round(3 * scale)),
        )
    draw.line((middle, 0, middle, height - 1), fill=50, width=max(2, _mm(dpi, 0.4)))
    draw.rectangle((0, 0, width - 1, band - 1), fill=30)
    y = band + _mm(dpi, below_mm)
    bump = _mm(dpi, 3)
    at = middle - _mm(dpi, 8)
    draw.rectangle((at - bump // 2, band - 1, at + bump // 2, band + bump), fill=30)
    half = _mm(dpi, 33) // 2
    draw.line((middle - half, y, middle + half, y), fill=40, width=max(1, _mm(dpi, 0.6)))
    return image


@pytest.mark.parametrize("dpi", [150, 300, 600])
def test_stroke_running_just_under_the_band_and_joined_to_it_still_crosses_the_cut(dpi):
    """2 mm under the band the stroke is only a few working px from it, but it is a
    pen stroke across the fold, not the band's ragged edge: its overhang is checked."""
    result = detect_split(_a4_spread_with_band_stroke(dpi, 2.0), dpi=(dpi, dpi), overlap_mm=5.0)
    _two_pages(result, "fold")
    assert len(result["flags"]) == 1
    assert "more than the 5 mm overlap" in result["flags"][0]


def test_ragged_backdrop_edge_and_gutter_wedge_are_not_writing_across_the_cut():
    """The pages' top edges curve down toward the binding, so the backdrop's lower edge
    sinks gradually toward the gutter, and it is ragged (teeth a few px deep); at the
    gutter the backdrop reaches down as a wedge with a thin crease tail along the fold,
    as on a real register spread. None of it is writing across the cut."""
    image = _fold(spread(gutter=(900, 1100)), 1000)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1999, 45), fill=30)
    edge = [(x, 46 + round(12 * (1 - ((x - 1000) / 1000) ** 2))) for x in range(0, 2001, 8)]
    draw.polygon([(0, 45), *edge, (2000, 45)], fill=30)
    for x, y in edge:
        draw.rectangle((x, y, x + 3, y + (x * 7) % 5), fill=30)
    draw.polygon([(860, 50), (1140, 50), (1000, 100)], fill=30)
    draw.line((1000, 100, 1000, 140), fill=30, width=3)
    result = _check(image)
    _two_pages(result, "fold")
    assert result["flags"] == []


# --- Split hardening over many kinds of spread --------------------------

CUT_TOLERANCE_MM = 3.0


def _judge(case, result) -> tuple[str, float | None]:
    """'right' (count right, cut within tolerance, no flag), 'flagged', or 'wrong' (a
    confident wrong count or cut). Also the cut error in mm."""
    value = result["value"]
    error = None
    if value["pages"] == case.pages and case.pages == 2:
        height = case.image.height
        error = (
            max(
                abs(_x_at(value["cut"], y) - case.gutter_x(y))
                for y in (0.1 * height, 0.5 * height, 0.9 * height)
            )
            * 25.4
            / case.dpi
        )
    correct = value["pages"] == case.pages and (error is None or error <= CUT_TOLERANCE_MM)
    if result["flags"]:
        return "flagged", error
    return ("right" if correct else "wrong"), error


def _run_case(case) -> list[tuple[str, float | None]]:
    out = []
    for quarter_turns in range(4):
        result = detect_split(
            turned(case.image, quarter_turns),
            turns=(4 - quarter_turns) % 4,
            dpi=(case.dpi, case.dpi),
            overlap_mm=3.0,
        )
        out.append(_judge(case, result))
    return out


@pytest.mark.parametrize("seed", [1, 2], ids=["usual", "hard"])
@pytest.mark.parametrize("kind", KINDS)
def test_spread_is_cut_right_or_flagged_in_every_turn(kind, seed):
    """Every kind of spread, its usual and its harder variant, at 150 dpi in all four
    turns: the right count and a cut within 3 mm of the drawn gutter, or a flag; never
    a confident wrong cut and never one page silently."""
    outcomes = _run_case(spread_case(kind, 150, seed))
    assert all(outcome != "wrong" for outcome, _ in outcomes), outcomes


@pytest.mark.parametrize(
    "kind", ["plain", "deep_shadow", "one_side_shadow", "rotated", "microfilm"]
)
def test_spread_at_300_dpi_is_cut_right_or_flagged(kind):
    outcomes = _run_case(spread_case(kind, 300, 1))
    assert all(outcome != "wrong" for outcome, _ in outcomes), outcomes


@pytest.mark.parametrize("kind", ["plain", "one_side_shadow"])
def test_spread_at_600_dpi_is_cut_right_or_flagged(kind):
    outcomes = _run_case(spread_case(kind, 600, 1))
    assert all(outcome != "wrong" for outcome, _ in outcomes), outcomes


def test_single_page_on_a_wide_frame_is_never_cut_silently():
    outcomes = _run_case(single_page_case(150, 1))
    assert all(outcome != "wrong" for outcome, _ in outcomes), outcomes


# --- Writing across the cut: overlap and overhang -----------------------


def _stroke_case(reach_mm: float, both_sides: bool = False):
    """A spread at 300 dpi with a fold at x=1000 and a 5 px pen stroke crossing it, held
    mostly by the left page and reaching `reach_mm` past the fold (or reaching far on
    both sides). Returns the image and a mask of the stroke."""
    image = _fold(spread(gutter=(880, 1120)), 1000)
    mask = Image.new("L", image.size, 0)
    reach = round(reach_mm * 300 / 25.4)
    start = 1000 - (reach + 150 if both_sides else 300)
    for target in (ImageDraw.Draw(image), ImageDraw.Draw(mask)):
        target.line((start, 700, 1000 + reach, 700), fill=40, width=5)
    mask = mask.point(lambda v: 255 if v else 0)
    return image, mask


def _whole_on_some_page(result: dict, mask: Image.Image, overlap_mm: float) -> bool:
    from pagekit.geometry import page_polygon

    overlap_px = overlap_mm * 300 / 25.4
    for page_index in (0, 1):
        polygon = page_polygon(mask.size, result["value"], page_index, overlap_px)
        area = Image.new("L", mask.size, 0)
        ImageDraw.Draw(area).polygon(polygon, fill=255)
        if ImageChops.subtract(mask, area).getbbox() is None:
            return True
    return False


@pytest.mark.parametrize("reach_mm", [1.0, 2.0, 2.5])
def test_stroke_within_the_overlap_is_whole_on_its_page_and_not_flagged(reach_mm):
    image, mask = _stroke_case(reach_mm)
    result = _check(image, overlap_mm=3.0)
    _two_pages(result, "fold")
    assert result["flags"] == []
    assert _whole_on_some_page(result, mask, 3.0)


@pytest.mark.parametrize(("reach_mm", "both_sides"), [(3.5, False), (6.0, False), (8.0, True)])
def test_stroke_beyond_the_overlap_is_flagged_as_whole_on_neither_page(reach_mm, both_sides):
    image, mask = _stroke_case(reach_mm, both_sides)
    result = _check(image, overlap_mm=3.0)
    _two_pages(result, "fold")
    assert not _whole_on_some_page(result, mask, 3.0)
    assert len(result["flags"]) == 1
    assert "whole on neither page" in result["flags"][0]
    assert "more than the 3 mm overlap" in result["flags"][0]


def test_one_sided_shadow_edge_inside_the_writing_does_not_move_the_cut():
    """A shadow deepening toward the gutter and ending in a steep edge inside the right
    page's writing (as a curled page can cast): the steep edge is not the fold here,
    since cutting there would cross the writing; the cut stays in the gutter."""
    image = spread(gutter=(920, 1080))
    row = []
    for x in range(2000):
        if 800 <= x < 1000:
            dark = 50 * (x - 800) / 200
        elif 1000 <= x < 1110:
            dark = 50
        else:
            dark = 0
        row.append(round(255 * (1 - dark / 255)))
    shade = Image.new("L", (2000, 1))
    shade.putdata(row)
    result = _check(ImageChops.multiply(image, shade.resize(image.size)))
    cut = _two_pages(result, "fold")
    assert 920 <= _x_at(cut, 700) <= 1080
    assert result["flags"] == []


@pytest.mark.parametrize("kind", ["rotated", "two_tones"])
def test_hard_spread_at_600_dpi_is_cut_right_or_flagged(kind):
    """A spread turned 4.5 degrees in the frame (more than a gap fit may lean), and two
    pages of different paper tone with no fold line or shadow (the gap's middle is
    pulled by ragged line ends; the tone step is where the pages meet)."""
    outcomes = _run_case(spread_case(kind, 600, 2))
    assert all(outcome != "wrong" for outcome, _ in outcomes), outcomes


def test_faint_gutter_shadow_inside_the_gap_places_a_gap_cut_at_600_dpi():
    """A gutter shadow too faint to count as a fold on its own, no fold line: the gap
    decides two pages, and the faint shadow inside it places the cut, not the gap's
    middle (pulled by ragged line ends)."""
    outcomes = _run_case(spread_case("low_contrast_gutter", 600, 2))
    assert all(outcome != "wrong" for outcome, _ in outcomes), outcomes


# --- Two pages made one; slanted strokes across the cut --------------------------------


@pytest.mark.parametrize("dpi", [150, 300])
def test_flourish_across_a_wide_gap_does_not_make_two_pages_one_silently(dpi):
    """A 28 mm gutter with no fold line and no shadow, crossed by one flourish: the
    flourish's box bridges the gap. Two pages, or a flag; never one page silently."""
    case, _ = crossing_case(dpi, curved=True, fold_line=False)
    result = detect_split(case.image, dpi=(dpi, dpi), overlap_mm=3.0)
    assert result["value"]["pages"] == 2 or result["flags"], result["evidence"]


@pytest.mark.parametrize("dpi", [150, 300, 600])
@pytest.mark.parametrize(
    ("slope", "curved"),
    [(0.0, False), (0.3, False), (1.0, False), (0.0, True)],
    ids=["level", "slope-0.3", "slope-1", "curved"],
)
@pytest.mark.parametrize("rising", [True, False], ids=["rising", "falling"])
def test_stroke_across_a_fold_line_is_counted_and_flagged(dpi, slope, curved, rising):
    """A stroke from 30 mm left to 12 mm right of a thin fold line reaches past the
    3 mm overlap on both sides: it cannot be whole on either page, so it must be
    counted as crossing the cut and flagged, whatever its slope."""
    case, _ = crossing_case(dpi, slope=slope, rising=rising, curved=curved)
    result = detect_split(case.image, dpi=(dpi, dpi), overlap_mm=3.0)
    assert result["value"]["pages"] == 2
    assert "no ink mark crosses the cut" not in result["evidence"]
    assert any("whole on neither page" in flag for flag in result["flags"]), result["evidence"]


def test_overlap_comes_from_the_core_settings_when_not_given():
    """One source of truth: without an overlap the split uses the preparation core's
    overlap_mm from thresholds_prepare.toml, and its own settings hold none."""
    import tomllib
    from pathlib import Path

    from pagekit._orient_ink import load_settings

    here = Path(__file__).with_name("thresholds_prepare.toml")
    core = tomllib.loads(here.read_text())["overlap_mm"]["value"]
    assert "overlap_mm" not in load_settings()
    image, _ = _stroke_case(core + 1.0)
    result = detect_split(image, dpi=DPI)
    assert f"more than the {core:g} mm overlap" in result["flags"][0]


# --- One sheet with an empty middle -------------------------------------------------

EMPTY_BAND = "two pages decided from an empty band alone"


@pytest.mark.parametrize("dpi", [150, 300])
@pytest.mark.parametrize("kind", SHEET_KINDS)
def test_one_sheet_with_an_empty_middle_is_two_pages_flagged_in_every_turn(kind, dpi):
    """A landscape sheet of unbroken paper with two columns (handwritten or printed),
    or a map, and nothing in the middle: only an empty band suggests two pages. The
    answer stays two pages with the cut in the band, and is flagged for review."""
    case = sheet_case(kind, dpi)
    for quarter_turns in range(4):
        result = detect_split(
            turned(case.image, quarter_turns), turns=(4 - quarter_turns) % 4, dpi=(dpi, dpi)
        )
        assert result["value"]["pages"] == 2
        assert any(EMPTY_BAND in flag for flag in result["flags"]), result["evidence"]


@pytest.mark.parametrize("seed", [1, 2], ids=["usual", "hard"])
@pytest.mark.parametrize("kind", KINDS)
def test_spread_with_a_gutter_cue_is_not_flagged_as_an_empty_band(kind, seed):
    """Every spread kind whose gutter shows something (a line, a shadow, a faint shadow,
    a step in paper tone) is not sent to review by the empty-band rule."""
    result = detect_split(spread_case(kind, 150, seed).image, dpi=(150, 150))
    assert not any(EMPTY_BAND in flag for flag in result["flags"]), result["evidence"]
