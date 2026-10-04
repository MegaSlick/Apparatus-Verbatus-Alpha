"""Page count and split detector (spec 0003) on synthetic frames drawn here; no real
register material."""

from __future__ import annotations

import math
import random

import pytest
from PIL import Image, ImageChops, ImageDraw

from pagekit._orient_ink import ANSWER_KEYS, TOO_LITTLE_INK, DetectorError
from pagekit._orient_testpages import PAPER, page, spread, turned, write_block
from pagekit.split import detect_split

DPI = (300, 300)
VALUE_KEYS = {"pages", "cut", "method", "part", "neighbour"}


def _check(image: Image.Image, **kwargs) -> dict:
    kwargs.setdefault("dpi", DPI)
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
    result = _check(spread(gutter=(920, 1080)))
    cut = _two_pages(result, "gap")
    assert result["value"]["part"] is None
    for y in (0, 700, 1399):
        assert 870 < _x_at(cut, y) < 1080
    assert result["flags"] == []


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


# --- Review findings (brief 0021) -------------------------------------------------------


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
