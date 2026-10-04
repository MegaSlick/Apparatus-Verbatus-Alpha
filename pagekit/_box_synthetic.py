"""Synthetic pages for the tests of the skew and box detectors.

Everything here is drawn from a seeded random generator: lines of word-like ink shapes
with ascenders and descenders, signatures, crosses, rules, bands, backdrops, shadows and
target-like grids. No real register material is used or imitated.
"""

from __future__ import annotations

import math
import random

from PIL import Image, ImageChops, ImageDraw

PAPER = 228
INK = 35


def mm(value: float, dpi: float) -> int:
    return max(1, round(value * dpi / 25.4))


def word(
    draw: ImageDraw.ImageDraw,
    rng: random.Random,
    x: float,
    baseline: float,
    dpi: float,
    xh_mm: float = 2.4,
    pen_mm: float = 0.4,
    ink: int = INK,
) -> float:
    """Draw one cursive-like word starting at x on `baseline`; return its right end."""
    xh = xh_mm * dpi / 25.4
    pen = max(1, round(pen_mm * dpi / 25.4))
    letters = rng.randint(2, 7)
    for _ in range(letters):
        width = xh * rng.uniform(0.7, 1.2)
        kind = rng.random()
        top = baseline - xh
        if kind < 0.45:  # a bowl
            draw.ellipse((x, top, x + width, baseline), outline=ink, width=pen)
        elif kind < 0.65:  # an ascender with a bowl
            draw.line((x, baseline, x, baseline - 2.2 * xh), fill=ink, width=pen)
            draw.ellipse((x, top, x + width, baseline), outline=ink, width=pen)
        elif kind < 0.8:  # a descender
            draw.line((x + width / 2, top, x + width / 2, baseline + 1.1 * xh), fill=ink, width=pen)
            draw.arc((x, top, x + width, baseline), 180, 360, fill=ink, width=pen)
        else:  # an arch
            draw.arc((x, top, x + width, baseline + xh), 180, 360, fill=ink, width=pen)
            draw.line((x, baseline, x, top + xh / 2), fill=ink, width=pen)
            draw.line((x + width, baseline, x + width, top + xh / 2), fill=ink, width=pen)
        # The joining stroke along the baseline.
        draw.line((x, baseline, x + width * 1.25, baseline), fill=ink, width=pen)
        x += width * 1.25
    return x


def writing(
    draw: ImageDraw.ImageDraw,
    rng: random.Random,
    box: tuple[float, float, float, float],
    dpi: float,
    line_mm: float = 9.0,
    xh_mm: float = 2.4,
    ink: int = INK,
) -> None:
    """Lines of words filling `box` (left, top, right, bottom) with level baselines."""
    left, top, right, bottom = box
    step = line_mm * dpi / 25.4
    baseline = top + 2.5 * xh_mm * dpi / 25.4
    while baseline < bottom - 1.2 * xh_mm * dpi / 25.4:
        x = left + rng.uniform(0, 3) * dpi / 25.4
        while True:
            # Words never run past the right edge of the box.
            if x > right - 8 * xh_mm * dpi / 25.4:
                break
            wobble = rng.uniform(-0.4, 0.4) * dpi / 25.4  # baselines wander a little
            x = word(draw, rng, x, baseline + wobble, dpi, xh_mm, ink=ink)
            x += rng.uniform(1.2, 2.2) * xh_mm * dpi / 25.4
        baseline += step


def signature(
    draw: ImageDraw.ImageDraw, rng: random.Random, x: float, y: float, dpi: float
) -> None:
    """A looping flourish about 35 mm wide and 10 mm tall, starting at (x, y)."""
    points = []
    span = 35 * dpi / 25.4
    height = 5 * dpi / 25.4
    for i in range(60):
        t = i / 59
        points.append(
            (x + t * span, y + height * math.sin(t * 9.5 + rng.uniform(-0.2, 0.2)) * (1 - t / 2))
        )
    draw.line(points, fill=INK, width=max(1, round(0.4 * dpi / 25.4)), joint="curve")
    draw.line(
        (x, y + height * 1.2, x + span * 0.9, y + height * 0.8),
        fill=INK,
        width=max(1, round(0.4 * dpi / 25.4)),
    )


def cross(draw: ImageDraw.ImageDraw, x: float, y: float, dpi: float, size_mm: float = 3.5) -> None:
    """A small cross for a mark, centred on (x, y)."""
    half = size_mm * dpi / 25.4 / 2
    pen = max(1, round(0.4 * dpi / 25.4))
    draw.line((x - half, y - half, x + half, y + half), fill=INK, width=pen)
    draw.line((x - half, y + half, x + half, y - half), fill=INK, width=pen)


def page(size_mm=(160, 220), dpi=150, seed=1, margins_mm=(20, 20, 20, 20), paper=PAPER):
    """A level page with writing inside the margins (left, top, right, bottom in mm)."""
    rng = random.Random(seed)
    size = (mm(size_mm[0], dpi), mm(size_mm[1], dpi))
    image = Image.new("L", size, paper)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = margins_mm
    writing(
        draw,
        rng,
        (mm(left, dpi), mm(top, dpi), size[0] - mm(right, dpi), size[1] - mm(bottom, dpi)),
        dpi,
    )
    return image


def turned(image: Image.Image, degrees_ccw_to_level: float, paper: int = PAPER) -> Image.Image:
    """The image tilted so that turning it `degrees_ccw_to_level` counterclockwise levels it."""
    return image.rotate(-degrees_ccw_to_level, Image.BICUBIC, fillcolor=paper)


def darker(*images: Image.Image) -> Image.Image:
    result = images[0]
    for image in images[1:]:
        result = ImageChops.darker(result, image)
    return result


def noisy(image: Image.Image, sigma: float = 6, seed: int = 3) -> Image.Image:
    """Paper grain: deterministic Gaussian noise added around the image's levels."""
    rng = random.Random(seed)
    width, height = image.size
    small = Image.new("L", (max(1, width // 2), max(1, height // 2)))
    small.putdata(
        [
            max(0, min(255, round(128 + rng.gauss(0, sigma))))
            for _ in range(small.width * small.height)
        ]
    )
    grain = small.resize(image.size, Image.NEAREST)
    return ImageChops.add(image, grain, 1.0, -128)


def mottled(
    image: Image.Image, sd: float, patch_mm: float, dpi: float, seed: int = 5
) -> Image.Image:
    """Paper mottling: smooth patches about `patch_mm` across, standard deviation `sd`."""
    rng = random.Random(seed)
    cell = max(1, round(patch_mm * dpi / 25.4))
    grid = (max(2, image.width // cell + 2), max(2, image.height // cell + 2))
    values = [rng.gauss(0, 1) for _ in range(grid[0] * grid[1])]
    low = min(values)
    span = (max(values) - low) or 1.0
    small = Image.frombytes("L", grid, bytes(round((x - low) / span * 255) for x in values))
    smooth = small.resize((grid[0] * cell, grid[1] * cell), Image.BICUBIC).crop((0, 0) + image.size)
    from PIL import ImageStat

    stat = ImageStat.Stat(smooth)
    mean, spread = stat.mean[0], stat.stddev[0] or 1.0
    offset = smooth.point(lambda v: max(0, min(255, round(128 + (v - mean) * sd / spread))))
    return ImageChops.add(image, offset, 1.0, -128)


def foxed(image: Image.Image, dpi: float, seed: int = 6, count: int = 12) -> Image.Image:
    """Foxing: soft brown spots 2 to 5 mm across, 25 to 40 grey levels deep."""
    from PIL import ImageFilter

    rng = random.Random(seed)
    spots = Image.new("L", image.size, 255)
    draw = ImageDraw.Draw(spots)
    for _ in range(count):
        r = rng.uniform(1, 2.5) * dpi / 25.4
        x, y = rng.uniform(r, image.width - r), rng.uniform(r, image.height - r)
        depth = rng.randint(25, 40)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=255 - depth)
    spots = spots.filter(ImageFilter.GaussianBlur(dpi / 25.4 * 0.4))
    return ImageChops.subtract(image, ImageChops.invert(spots))


def flourished_page(seed: int, amp_mm: float = 1.5, loop_mm: float = 2.0, dpi: float = 150):
    """Running writing whose baselines wave by `amp_mm` along each line, with a looping
    flourish under about half the words: the kind of hand where a line fit and a
    projection profile disagree by a fraction of a degree."""
    rng = random.Random(seed)
    image = Image.new("L", (mm(160, dpi), mm(220, dpi)), PAPER)
    draw = ImageDraw.Draw(image)
    px = dpi / 25.4
    y = 25 * px
    while y < 200 * px:
        x = 20 * px + rng.uniform(0, 3) * px
        while x < 135 * px:
            wave = amp_mm * px * math.sin(2 * math.pi * x / (40 * px) + y)
            start = x
            x = word(draw, rng, x, y + wave, dpi)
            if rng.random() < 0.5:
                points = [
                    (
                        start + t * (x - start + 12 * px),
                        y + wave + loop_mm * px * math.sin(math.pi * t),
                    )
                    for t in (i / 20 for i in range(21))
                ]
                draw.line(points, fill=INK, width=max(1, round(0.4 * px)))
            x += rng.uniform(1.2, 2.2) * 2.4 * px
        y += 9 * px
    return image


def edge_stack(
    image: Image.Image,
    side_x: int,
    dpi: float,
    lines: int = 8,
    board_mm: float = 0.0,
    backdrop: int = 20,
    seed: int = 9,
) -> Image.Image:
    """A stack of page edges to the right of `side_x`: thin vertical lines, alternately
    light and dark, about 0.6 mm apart, darkening outward as in the book's shadow,
    running most of the height with small breaks; then an optional dark board edge and
    the backdrop. Everything right of the stack is replaced."""
    rng = random.Random(seed)
    out = image.copy()
    draw = ImageDraw.Draw(out)
    px = dpi / 25.4
    width, height = out.size
    draw.rectangle((side_x, 0, width, height), fill=backdrop)
    spacing = 0.6 * px
    for i in range(lines):
        base = round(200 - 140 * i / max(1, lines - 1))  # the shadow deepens outward
        x0 = side_x + round(i * spacing)
        x1 = side_x + round((i + 1) * spacing)
        draw.rectangle((x0, 0, x1 - 1, height), fill=base)
        level = base - 45 if i % 2 else min(255, base + 25)
        y = 0
        while y < height:  # each edge line, broken here and there
            length = rng.uniform(30, 90) * px
            draw.line((x0, y, x0, y + length), fill=level, width=max(1, round(0.2 * px)))
            y += length + rng.uniform(0, 4) * px
    edge = side_x + round(lines * spacing)
    if board_mm:
        draw.rectangle((edge, 0, edge + round(board_mm * px), height), fill=55)
    return out
