"""The detectors' working copies at reduction factors above 1 (600 dpi, 333 dpi and
unequal axes), on tilted, quarter-turned and odd-sized synthetic pages; no real
register material.

Each test here fails under one of these changes to pagekit/pipeline.py: rounding the
reduced page box inward; not dividing the affine translation by the factor; dropping
the reduction; not swapping the x and y resolution after a quarter turn; not clipping
the content box to the page box.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit import _box_synthetic as boxes
from pagekit import pipeline
from pagekit.answer import Answer
from pagekit.pipeline import DETECTORS, _page_copy
from pagekit.prepare import Detector, Source, StepContext, plan
from pagekit.project import load_settings

SIZE = (1001, 1503)  # odd on both axes
MARK = (401.5, 702.5)  # the centre of a 9 x 9 dark square in the source


def _source_image() -> Image.Image:
    image = Image.new("L", SIZE, 220)
    x, y = MARK
    ImageDraw.Draw(image).rectangle((x - 4.5, y - 4.5, x + 3.5, y + 3.5), fill=10)
    return image


def _context(image, resolution, step, values) -> StepContext:
    data = image.tobytes()
    source = Source(
        path=Path("/nowhere/synthetic.png"),
        relative="synthetic.png",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        size=image.size,
        mode=image.mode,
        file_dpi=resolution,
    )
    settings = {name: entry["value"] for name, entry in load_settings().items()}
    return StepContext(step, 1, source, resolution, dict(values), settings, lambda: image, {})


def _dark_centre(image: Image.Image) -> tuple[float, float]:
    width = image.size[0]
    total = sx = sy = 0.0
    for index, level in enumerate(image.convert("L").tobytes()):
        if level < 150:
            weight = 150 - level
            total += weight
            sx += weight * (index % width + 0.5)
            sy += weight * (index // width + 0.5)
    return sx / total, sy / total


CASES = [
    # resolution (x, y) of the source, quarter turns, skew
    ((600.0, 600.0), 0, 3.0),
    ((600.0, 600.0), 1, -2.5),
    ((333.0, 333.0), 3, 1.75),
    ((333.0, 333.0), 2, 0.0),
    ((600.0, 333.0), 1, 2.0),
]


@pytest.mark.parametrize(("resolution", "turns", "angle"), CASES)
def test_the_working_copy_is_reduced_and_lands_where_the_chain_says(resolution, turns, angle):
    image = _source_image()
    values = {"orientation": turns, "split": {"pages": 1}, "skew": angle}
    context = _context(image, resolution, "page_box", values)
    page, area, factor = _page_copy(context)
    upright = (resolution[1], resolution[0]) if turns % 2 else resolution
    assert factor == math.floor(min(upright) / 150) and factor >= 2
    width, height = context.chain().output_size
    assert page.size == (math.ceil(width / factor), math.ceil(height / factor))
    assert area is None
    expected = context.chain().forward([MARK])[0]
    found = _dark_centre(page)
    assert math.dist(found, (expected[0] / factor, expected[1] / factor)) < 0.75
    # The skew step's copy (no rotation yet) is reduced the same way.
    skew_context = _context(image, resolution, "skew", values)
    flat, _, flat_factor = _page_copy(skew_context)
    assert flat_factor == factor
    expected = skew_context.chain().forward([MARK])[0]
    assert math.dist(_dark_centre(flat), (expected[0] / factor, expected[1] / factor)) < 0.75


def _spy(monkeypatch, name, answer):
    calls = []

    def spy(image, dpi, *args):
        calls.append({"size": image.size, "dpi": tuple(dpi), "args": args})
        return answer(image, args)

    monkeypatch.setattr(pipeline, name, spy)
    return calls


@pytest.mark.parametrize(("resolution", "turns", "angle"), CASES)
def test_each_detector_gets_the_upright_resolution_of_its_reduced_copy(
    monkeypatch, resolution, turns, angle
):
    def plain(value):
        return lambda image, args: {
            "value": value(image),
            "confidence": 0.9,
            "evidence": "Spy.",
            "flags": [],
        }

    skew = _spy(monkeypatch, "detect_skew", plain(lambda image: 0.0))
    box = _spy(monkeypatch, "detect_page_box", plain(lambda image: [0, 0, *image.size]))
    content = _spy(monkeypatch, "detect_content_box", plain(lambda image: [1, 1, 5, 5]))
    image = _source_image()
    values = {"orientation": turns, "split": {"pages": 1}, "skew": angle}
    upright = (resolution[1], resolution[0]) if turns % 2 else resolution
    factor = math.floor(min(upright) / 150)
    wanted = (upright[0] / factor, upright[1] / factor)
    DETECTORS["skew"].run(_context(image, resolution, "skew", values))
    DETECTORS["page_box"].run(_context(image, resolution, "page_box", values))
    with_box = {**values, "page_box": [0, 0, 40, 40]}
    DETECTORS["content_box"].run(_context(image, resolution, "content_box", with_box))
    for calls in (skew, box, content):
        (call,) = calls
        assert call["dpi"] == pytest.approx(wanted)


@pytest.mark.parametrize(("resolution", "turns", "angle"), CASES)
def test_boxes_are_reduced_outward_scaled_back_outward_and_clipped(
    monkeypatch, resolution, turns, angle
):
    image = _source_image()
    values = {"orientation": turns, "split": {"pages": 1}, "skew": angle}
    context = _context(image, resolution, "page_box", values)
    width, height = context.chain().output_size
    _, _, factor = _page_copy(context)

    # The page box detector's working box comes back multiplied out, inside the grid.
    _spy(
        monkeypatch,
        "detect_page_box",
        lambda small, args: {
            "value": [3, 5, small.size[0] - 1, small.size[1] - 2],
            "confidence": 0.9,
            "evidence": "Spy.",
            "flags": [],
        },
    )
    found = DETECTORS["page_box"].run(context)
    small = (math.ceil(width / factor), math.ceil(height / factor))
    assert found.value == [
        3 * factor,
        5 * factor,
        min(width, (small[0] - 1) * factor),
        min(height, (small[1] - 2) * factor),
    ]

    # A page box off the reduced grid's lines is reduced outward, never inward; a
    # content box the detector draws past it is clipped back to it.
    page_box = [13, 27, width - 7, height - 11]
    content = _spy(
        monkeypatch,
        "detect_content_box",
        lambda small, args: {
            "value": [0, 0, *small.size],
            "confidence": 0.9,
            "evidence": "Spy.",
            "flags": [],
        },
    )
    with_box = {**values, "page_box": page_box}
    answer = DETECTORS["content_box"].run(_context(image, resolution, "content_box", with_box))
    (call,) = content
    assert list(call["args"][0]) == [
        13 // factor,
        27 // factor,
        math.ceil((width - 7) / factor),
        math.ceil((height - 11) / factor),
    ]
    assert answer.value == page_box


@pytest.mark.parametrize(("dpi", "turns", "angle"), [(600, 1, 2.0), (333, 3, -1.5)])
def test_a_tilted_quarter_turned_page_at_high_and_odd_dpi_is_prepared_right(
    tmp_path, dpi, turns, angle
):
    level = boxes.page(size_mm=(90, 120), dpi=dpi, seed=3, margins_mm=(20, 20, 20, 20))
    tilted = boxes.turned(level, angle)
    steps = {1: Image.Transpose.ROTATE_90, 3: Image.Transpose.ROTATE_270}
    scanned = tilted.transpose(steps[turns])  # needs `turns` quarter turns to be upright
    scanned = scanned.crop((0, 0, scanned.width - 1, scanned.height))  # an odd frame
    folder = tmp_path / "src"
    folder.mkdir()
    scanned.save(folder / "page.png", dpi=(dpi, dpi))
    detectors = {step: DETECTORS[step] for step in ("skew", "page_box", "content_box")}
    detectors["orientation"] = Detector(
        "test.orientation/1", lambda context: Answer(turns, 1.0, "Given.", ())
    )
    (page,) = plan(
        [folder], tmp_path / "out", detectors=detectors, settings_overrides={"crop": "content"}
    ).pages
    assert page.steps["skew"]["value"] == pytest.approx(angle, abs=0.25)
    found = page.steps["content_box"]["value"]
    grid = page.chain.levelled_size
    per_mm = dpi / 25.4
    # The writing's box on the level page, moved to the levelled grid: the tilt and the
    # levelling both turn about the centre, so only the centres differ.
    ink = level.point(lambda value: 255 if value < 128 else 0).getbbox()
    dx, dy = (grid[0] - level.width) / 2, (grid[1] - level.height) / 2
    expected = [ink[0] + dx, ink[1] + dy, ink[2] + dx, ink[3] + dy]
    for side in range(4):
        assert abs(found[side] - expected[side]) / per_mm < 1.5, (found, expected)
