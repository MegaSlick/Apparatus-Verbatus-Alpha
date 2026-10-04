"""The answer shape and the geometry chain, on synthetic images drawn here."""

from __future__ import annotations

import math

import pytest
from PIL import Image, ImageDraw

from pagekit.answer import Answer, AnswerError, validate_answer, validate_value
from pagekit.geometry import Chain, GeometryError, margin_box, paper_colour, render

TWO_PAGES = {"pages": 2, "cut": [[300.0, 0.0], [330.0, 400.0]]}


def _mark_centre(image: Image.Image, below: int = 100) -> tuple[float, float] | None:
    """The centroid of pixels darker than `below`, in continuous coordinates."""
    grey = image.convert("L")
    width = grey.size[0]
    total = sx = sy = 0
    for index, level in enumerate(grey.tobytes()):
        if level < below:
            weight = below - level
            total += weight
            sx += weight * (index % width + 0.5)
            sy += weight * (index // width + 0.5)
    return None if total == 0 else (sx / total, sy / total)


def _source(size=(600, 400), mark=(150.0, 100.0), mode="L") -> Image.Image:
    paper = 220 if mode == "L" else (225, 215, 195)
    ink = 10 if mode == "L" else (10, 10, 40)
    image = Image.new(mode, size, paper)
    x, y = mark
    ImageDraw.Draw(image).rectangle((x - 5, y - 5, x + 4, y + 4), fill=ink)
    return image


# The answer shape


def test_a_well_formed_answer_is_accepted_and_normalised():
    answer = validate_answer(
        "page_box",
        {"value": (1, 2, 30, 40), "confidence": 1, "evidence": "Paper edge found.", "flags": []},
    )
    assert answer == Answer([1, 2, 30, 40], 1.0, "Paper edge found.", ())
    assert validate_answer("skew", Answer(1.5, 0.4, "Lines lean.", ("Faint.",))).flags == (
        "Faint.",
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"value": 0, "confidence": 0.5, "evidence": "x"},  # flags missing
        {"value": 0, "confidence": 0.5, "evidence": "x", "flags": [], "extra": 1},
        {"value": 0, "confidence": 1.5, "evidence": "x", "flags": []},
        {"value": 0, "confidence": True, "evidence": "x", "flags": []},
        {"value": 0, "confidence": None, "evidence": "x", "flags": []},
        {"value": 0, "confidence": 0.5, "evidence": "", "flags": []},
        {"value": 0, "confidence": 0.5, "evidence": "x", "flags": [""]},
        {"value": 0, "confidence": 0.5, "evidence": "x", "flags": "look"},
        {"value": 4, "confidence": 0.5, "evidence": "x", "flags": []},
        "not an answer",
    ],
)
def test_an_answer_that_breaks_the_shape_is_refused(answer):
    with pytest.raises(AnswerError):
        validate_answer("orientation", answer)


@pytest.mark.parametrize(
    ("step", "value"),
    [
        ("orientation", 1.0),
        ("orientation", True),
        ("split", {"pages": 3}),
        ("split", {"pages": 2}),
        ("split", {"pages": 2, "cut": [[1, 1], [1, 1]]}),
        ("split", {"pages": 1, "cut": [[1, 1], [1, 2]]}),
        ("skew", 45),
        ("skew", math.nan),
        ("page_box", [0, 0, 0, 10]),
        ("page_box", [0, 0, 10.5, 10]),
        ("content_box", [5, 5, 4, 10]),
        ("margin", -1),
        ("dewarp", 0),
    ],
)
def test_a_value_that_breaks_its_step_is_refused(step, value):
    with pytest.raises(AnswerError):
        validate_value(step, value)


def test_a_blank_page_has_no_content_box():
    assert validate_value("content_box", None) is None


# The chain


@pytest.mark.parametrize("turns", [0, 1, 2, 3])
@pytest.mark.parametrize("angle", [0.0, 2.5, -4.0])
@pytest.mark.parametrize("scale", [1.0, 0.5])
def test_a_mark_lands_where_the_forward_map_says_and_maps_back(turns, angle, scale):
    source = _source()
    chain = Chain.build(source.size, turns, {"pages": 1}, 0, 0.0, angle, None, scale)
    page = render(source, chain, 220)
    expected = chain.forward([(150.0, 100.0)])[0]
    found = _mark_centre(page)
    # A 10 px mark keeps its centre within a fraction of an output pixel.
    assert math.dist(found, expected) < 0.75
    back = chain.inverse([expected])[0]
    assert math.dist(back, (150.0, 100.0)) < 1e-6
    assert page.size == chain.output_size


def test_quarter_turns_are_exact_and_need_no_resampling():
    source = Image.effect_noise((60, 40), 60).convert("L")
    for turns, transpose in [
        (1, Image.Transpose.ROTATE_270),
        (2, Image.Transpose.ROTATE_180),
        (3, Image.Transpose.ROTATE_90),
    ]:
        chain = Chain.build(source.size, turns, {"pages": 1}, 0, 0.0, 0.0)
        assert render(source, chain, 0).tobytes() == source.transpose(transpose).tobytes()


@pytest.mark.parametrize(
    ("angle", "scale", "expected"),
    [
        (0.0, 1.0, {}),
        (0.0, 0.5, {"resize": 1}),
        (3.0, 1.0, {"transform": 1}),
        (3.0, 0.5, {"transform": 1, "reduce": 1}),
    ],
)
def test_the_page_is_made_from_the_source_in_one_resampling(monkeypatch, angle, scale, expected):
    calls: dict[str, int] = {}
    for name in ("transform", "resize", "reduce", "rotate"):
        original = getattr(Image.Image, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            calls[_name] = calls.get(_name, 0) + 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Image.Image, name, counted)
    source = _source()
    chain = Chain.build(source.size, 1, TWO_PAGES, 0, 5.0, angle, None, scale)
    render(source, chain, 220)
    # With rotation and shrinking, the page is sampled once on a finer grid and each
    # block averaged: one area-averaging filter applied to the original.
    assert calls == expected


def test_a_leaning_cut_keeps_the_neighbours_wedge_out():
    source = Image.new("L", (600, 400), 220)
    draw = ImageDraw.Draw(source)
    # The right-hand page is ink from 20 px past the cut onwards.
    draw.polygon([(320, 0), (600, 0), (600, 400), (350, 400)], fill=15)
    chain = Chain.build(source.size, 0, TWO_PAGES, 0, 10.0, 0.0)
    page = render(source, chain, 220)
    # The overlap is measured across the leaning cut, so it reaches a little further
    # along x; the frame ends at the cut's lowest point plus that.
    reach = 10 * math.hypot(30, 400) / 400
    assert page.size == (math.ceil(330 + reach), 400)
    assert min(page.tobytes()) == 220  # no ink from the neighbour
    # Near the top, the frame runs past the leaning cut: that wedge is the neighbour's
    # ink in the source and paper colour in the page.
    assert source.getpixel((335, 10)) == 15
    assert page.getpixel((335, 10)) == 220
    second = Chain.build(source.size, 0, TWO_PAGES, 1, 10.0, 0.0)
    assert second.frame_box[0] == math.floor(300 - reach)


@pytest.mark.parametrize(
    "split",
    [
        {"pages": 2, "cut": [[0.0, 10.0], [600.0, 30.0]]},  # leans 45 degrees or more
        {"pages": 2, "cut": [[700.0, 0.0], [720.0, 400.0]]},  # misses the frame
    ],
)
def test_a_cut_that_cannot_split_the_frame_is_refused(split):
    with pytest.raises(GeometryError):
        Chain.build((600, 400), 0, split, 0, 0.0, 0.0)


def test_a_one_page_frame_has_no_second_page():
    with pytest.raises(GeometryError):
        Chain.build((600, 400), 0, {"pages": 1}, 1, 0.0, 0.0)


def test_scale_above_one_is_refused():
    with pytest.raises(GeometryError, match="never upsampled"):
        Chain.build((600, 400), 0, {"pages": 1}, 0, 0.0, 0.0, None, 1.5)


@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_area_outside_the_source_is_paper_colour(turns):
    """Every pixel past the source's edges is fill: no black line on any side."""
    source = Image.new("L", (600, 400), 180)
    upright = (400, 600) if turns % 2 else (600, 400)
    box = (-20, -10, upright[0] + 30, upright[1] + 15)
    chain = Chain.build(source.size, turns, {"pages": 1}, 0, 0.0, 0.0, box)
    page = render(source, chain, 220)
    assert page.size == (upright[0] + 50, upright[1] + 25)
    inside = (20, 10, 20 + upright[0], 10 + upright[1])
    assert set(page.crop(inside).tobytes()) == {180}
    outside = Image.new("L", page.size, 220)
    outside.paste(page.crop(inside), inside[:2])
    assert page.tobytes() == outside.tobytes()
    rotated = render(source, Chain.build(source.size, 0, {"pages": 1}, 0, 0.0, 5.0), 220)
    assert rotated.getpixel((0, 0)) == 220  # the corner the turned frame does not cover
    assert rotated.getpixel(tuple(n - 1 for n in rotated.size)) == 220


def test_without_overlap_not_one_column_of_the_neighbour_comes_through():
    source = Image.new("L", (600, 400), 220)
    ImageDraw.Draw(source).rectangle((300, 0, 599, 399), fill=15)
    cut = {"pages": 2, "cut": [[300.0, 0.0], [300.0, 400.0]]}
    left = render(source, Chain.build(source.size, 0, cut, 0, 0.0, 0.0), 220)
    right = render(source, Chain.build(source.size, 0, cut, 1, 0.0, 0.0), 220)
    assert left.size == (300, 400) and set(left.tobytes()) == {220}
    assert right.size == (300, 400) and set(right.tobytes()) == {15}


def test_without_overlap_a_leaning_cut_gives_each_pixel_to_one_page():
    """Ink on every pixel whose centre lies right of the cut: the left page shows none
    of it and the right page all of it."""
    (x0, y0), (x1, y1) = TWO_PAGES["cut"]
    source = Image.new("L", (600, 400), 220)
    source.putdata(
        [
            15 if (i + 0.5 - x0) * (y1 - y0) - (j + 0.5 - y0) * (x1 - x0) > 0 else 220
            for j in range(400)
            for i in range(600)
        ]
    )
    left = render(source, Chain.build(source.size, 0, TWO_PAGES, 0, 0.0, 0.0), 220)
    right_chain = Chain.build(source.size, 0, TWO_PAGES, 1, 0.0, 0.0)
    right = render(source, right_chain, 99)
    assert set(left.tobytes()) == {220}
    assert set(right.tobytes()) == {15, 99}
    # Every source pixel right of the cut is on the right page.
    assert right.tobytes().count(15) == source.tobytes().count(15)


@pytest.mark.parametrize("angle", [2.0, -3.0])
def test_a_leaning_cut_keeps_the_wedge_out_of_a_levelled_page(angle):
    source = Image.new("L", (600, 400), 220)
    draw = ImageDraw.Draw(source)
    draw.polygon([(320, 0), (600, 0), (600, 400), (350, 400)], fill=15)
    draw.polygon([(0, 0), (280, 0), (310, 400), (0, 400)], fill=15)
    left = render(source, Chain.build(source.size, 0, TWO_PAGES, 0, 10.0, angle), 220)
    right = render(source, Chain.build(source.size, 0, TWO_PAGES, 1, 10.0, angle), 220)
    # Each page shows its own ink (bicubic may undershoot at its edges), but the wedge
    # past the cut is never shown.
    assert min(left.tobytes()) < 100 and min(right.tobytes()) < 100
    for page, chain_page, wedge in ((left, 0, (335.5, 10.5)), (right, 1, (291.5, 390.5))):
        chain = Chain.build(source.size, 0, TWO_PAGES, chain_page, 10.0, angle)
        x, y = chain.forward([wedge])[0]
        assert 0 <= x < page.size[0] and 0 <= y < page.size[1]
        assert source.getpixel((int(wedge[0]), int(wedge[1]))) == 15
        assert page.getpixel((int(x), int(y))) == 220


def test_margin_box_grows_the_content_and_holds_to_the_page_box():
    assert margin_box([0, 0, 100, 200], [10, 20, 90, 180], (5, 5), (2, 2)) == (5, 15, 95, 185)
    assert margin_box([0, 0, 100, 200], [10, 20, 90, 180], (30, 30), (2, 2)) == (-2, -2, 102, 202)
    assert margin_box([0, 0, 100, 200], None, (30, 30), (2, 2)) == (0, 0, 100, 200)
    assert margin_box([0, 0, 100, 200], [300, 300, 400, 400], (5, 5), (2, 2)) is None


def test_paper_colour_is_the_median_of_the_light_class_not_pure_white():
    source = Image.new("RGB", (400, 300), (228, 214, 190))
    draw = ImageDraw.Draw(source)
    for y in range(20, 280, 20):
        draw.rectangle((20, y, 380, y + 8), fill=(30, 25, 20))
    draw.ellipse((200, 100, 260, 160), fill=(180, 150, 110))  # a stain
    chain = Chain.build(source.size, 0, {"pages": 1}, 0, 0.0, 0.0)
    colour, method = paper_colour(source, chain, 200)
    assert colour == (228, 214, 190)
    assert "median" in method
    blank = Image.new("L", (300, 300), 231)
    assert paper_colour(blank, chain_for(blank), 100)[0] == 231


def chain_for(image: Image.Image) -> Chain:
    return Chain.build(image.size, 0, {"pages": 1}, 0, 0.0, 0.0)


def test_colour_stays_colour_and_the_chain_round_trips_through_plain_data():
    source = _source(mode="RGB")
    chain = Chain.build(source.size, 3, {"pages": 1}, 0, 0.0, 1.5, (10, 10, 300, 500), 0.8)
    page = render(source, chain, (225, 215, 195))
    assert page.mode == "RGB" and page.size == chain.output_size
    again = Chain.from_dict(chain.to_dict())
    assert again == chain
    point = (123.25, 77.5)
    assert math.dist(again.inverse(again.forward([point]))[0], point) < 1e-9
