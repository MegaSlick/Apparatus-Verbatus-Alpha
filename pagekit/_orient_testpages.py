"""Synthetic pages for the orientation and split tests; never real register material.

Lines of word-like ink shapes: letters are loops of the core height joined at the
baseline, some with an ascender above or (less often) a descender below, words of
varied length, lines left-aligned with ragged right ends. Everything is drawn from a
seeded generator, so a page is the same every time.
"""

from __future__ import annotations

import random

from PIL import Image, ImageDraw

PAPER = 228
INK_LEVEL = 45


def write_block(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    seed: int,
    *,
    pitch: int = 48,
    core: int = 14,
    rise: int = 13,
    letter: int = 10,
    stroke: int = 3,
    ink: int = INK_LEVEL,
    ragged: tuple[float, float] = (0.55, 1.0),
) -> None:
    """Lines of writing inside `box`, left-aligned at its left edge."""
    rng = random.Random(seed)
    x0, y0, x1, y1 = box
    top = y0
    while top + rise + core + rise <= y1:
        baseline = top + rise + core
        end = x0 + (x1 - x0) * rng.uniform(*ragged)
        x = x0
        while True:
            letters = rng.randint(2, 8)
            if x + letters * letter > end:
                break
            for i in range(letters):
                lx = x + i * letter
                draw.ellipse(
                    (lx, baseline - core, lx + letter - 2, baseline), outline=ink, width=stroke
                )
                if i:
                    draw.line((lx - 3, baseline - 1, lx + 2, baseline - 1), fill=ink, width=stroke)
                roll = rng.random()
                if roll < 0.3:
                    draw.line(
                        (lx + letter - 3, baseline - core - rise, lx + letter - 3, baseline - 2),
                        fill=ink,
                        width=stroke,
                    )
                elif roll < 0.4:
                    draw.line(
                        (lx + 1, baseline - 2, lx + 1, baseline + rise),
                        fill=ink,
                        width=stroke,
                    )
            x += letters * letter + rng.randint(12, 22)
        top += pitch


def page(
    size: tuple[int, int] = (1000, 1400),
    seed: int = 1,
    *,
    margin: tuple[int, int, int, int] = (110, 120, 90, 120),
    paper: int = PAPER,
) -> Image.Image:
    """A single upright page of writing."""
    width, height = size
    image = Image.new("L", size, paper)
    left, top, right, bottom = margin
    write_block(ImageDraw.Draw(image), (left, top, width - right, height - bottom), seed)
    return image


def spread(
    size: tuple[int, int] = (2000, 1400),
    seed: int = 1,
    *,
    gutter: tuple[int, int] = (920, 1080),
    paper: int = PAPER,
) -> Image.Image:
    """Two facing pages of writing with an empty channel `gutter` between them."""
    width, height = size
    image = Image.new("L", size, paper)
    draw = ImageDraw.Draw(image)
    write_block(draw, (100, 120, gutter[0], height - 120), seed)
    write_block(draw, (gutter[1], 120, width - 80, height - 120), seed + 1)
    return image


def turned(image: Image.Image, quarter_turns_clockwise: int) -> Image.Image:
    """`image` turned clockwise by the given number of quarter turns."""
    steps = {
        0: None,
        1: Image.Transpose.ROTATE_270,
        2: Image.Transpose.ROTATE_180,
        3: Image.Transpose.ROTATE_90,
    }
    step = steps[quarter_turns_clockwise % 4]
    return image.copy() if step is None else image.transpose(step)
