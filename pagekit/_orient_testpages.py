"""Synthetic pages for the orientation and split tests; never real register material.

Lines of word-like ink shapes: letters are loops of the core height joined at the
baseline, some with an ascender above or (less often) a descender below, words of
varied length, lines left-aligned with ragged right ends. Everything is drawn from a
seeded generator, so a page is the same every time.
"""

from __future__ import annotations

import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter

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
    ascenders: float = 0.3,
    descenders: float = 0.1,
    centred: bool = False,
    joined: bool = True,
) -> None:
    """Lines of writing inside `box`, left-aligned at its left edge (or centred in it).
    `ascenders` and `descenders` are the shares of letters carrying one; `joined`
    links the letters of a word at the baseline."""
    rng = random.Random(seed)
    x0, y0, x1, y1 = box
    top = y0
    while top + rise + core + rise <= y1:
        baseline = top + rise + core
        end = x0 + (x1 - x0) * rng.uniform(*ragged)
        x = x0
        words = []
        while True:
            letters = rng.randint(2, 8)
            if x + letters * letter > end:
                break
            words.append((x, letters, [rng.random() for _ in range(letters)]))
            x += letters * letter + rng.randint(12, 22)
        shift = 0
        if centred and words:
            last_x, last_letters, _ = words[-1]
            shift = (x0 + x1 - words[0][0] - (last_x + last_letters * letter)) // 2
        for wx, _, rolls in words:
            for i, roll in enumerate(rolls):
                lx = wx + shift + i * letter
                draw.ellipse(
                    (lx, baseline - core, lx + letter - 2, baseline), outline=ink, width=stroke
                )
                if i and joined:
                    draw.line((lx - 3, baseline - 1, lx + 2, baseline - 1), fill=ink, width=stroke)
                if roll < ascenders:
                    draw.line(
                        (lx + letter - 3, baseline - core - rise, lx + letter - 3, baseline - 2),
                        fill=ink,
                        width=stroke,
                    )
                elif roll < ascenders + descenders:
                    draw.line(
                        (lx + 1, baseline - 2, lx + 1, baseline + rise),
                        fill=ink,
                        width=stroke,
                    )
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


def cursive_page(
    size: tuple[int, int] = (1800, 2600),
    seed: int = 11,
    *,
    gutter: str = "right",
) -> Image.Image:
    """A dense page of cursive in the manner of an old register: close lines with long
    ascenders and descenders that reach the neighbouring lines, looping flourishes
    under some lines and a flourished signature, faint show-through of mirrored
    writing from the other side, a dark gutter strip shading into the page on one side
    and a stack of page edges beyond it."""
    rng = random.Random(seed)
    width, height = size
    image = Image.new("L", size, 196)
    # Show-through: writing from the other side, mirrored and faint.
    back = Image.new("L", size, 255)
    write_block(
        ImageDraw.Draw(back),
        (150, 170, width - 150, height - 200),
        seed + 100,
        pitch=44,
        core=11,
        rise=22,
        letter=9,
        ink=150,
        ragged=(0.9, 1.0),
        ascenders=0.3,
        descenders=0.25,
    )
    image = ImageChops.darker(image, back.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    draw = ImageDraw.Draw(image)
    left, right = (190, width - 230) if gutter == "right" else (230, width - 190)
    write_block(
        draw,
        (left, 150, right, height - 330),
        seed,
        pitch=40,
        core=10,
        rise=22,
        letter=9,
        stroke=3,
        ink=40,
        ragged=(0.93, 1.0),
        ascenders=0.4,
        descenders=0.35,
    )
    # Line-end fillers drawn out to the right margin (so the right edge is straight),
    # and entry starts pushed into the left margin (so the left edge is not).
    for y in range(150 + 13 + 22 + 10, height - 330 - 22, 40):
        draw.line((right - rng.randint(20, 120), y - 3, right + 10, y - 5), fill=40, width=3)
        if rng.random() < 0.3:
            x = left - rng.randint(40, 140)
            draw.ellipse((x, y - 22, x + 40, y), outline=40, width=3)
    # Flourishes looping down from the end of some lines into the next.
    for y in range(150 + 32, height - 380, 40):
        if rng.random() < 0.25:
            x = rng.randint(left + 300, right - 200)
            draw.arc((x, y - 10, x + 220, y + 60), 0, 180, fill=40, width=3)
    # A flourished signature.
    sy = height - 260
    for i in range(5):
        draw.arc(
            (width // 2 - 300 + 25 * i, sy - 60 + 8 * i, width // 2 + 200, sy + 80),
            20,
            340,
            fill=40,
            width=4,
        )
    draw.line((width // 2 - 350, sy + 70, width // 2 + 320, sy + 40), fill=40, width=4)
    # Dark gutter strip shading into the page, and a stack of page edges beyond it.
    strip = 120
    for i in range(strip):
        level = round(196 - 150 * (i / strip) ** 1.5)
        x = width - strip + i if gutter == "right" else strip - 1 - i
        draw.line((x, 0, x, height - 1), fill=level)
    for i in range(10):
        x = 20 + 9 * i if gutter == "right" else width - 20 - 9 * i
        draw.line((x, 0, x + rng.randint(-6, 6), height - 1), fill=60, width=4)
    return image


def cursive_spread(seed: int = 11, *, lean: float = 0.4) -> Image.Image:
    """Two facing cursive pages on a dark backdrop: their lines out of step and leaning
    slightly apart, a gutter shadow between them, page-edge stacks at the outer sides,
    and a backdrop margin that is dark but not uniformly so."""
    width, height = 1800, 2600
    left = cursive_page((width, height), seed, gutter="right")
    right = cursive_page((width, height), seed + 1, gutter="left")
    right = right.transform(right.size, Image.Transform.AFFINE, (1, 0, 0, 0, 1, -20), fillcolor=196)
    left = left.rotate(lean, resample=Image.Resampling.BICUBIC, fillcolor=196)
    right = right.rotate(-lean, resample=Image.Resampling.BICUBIC, fillcolor=196)
    border = 70
    frame = Image.new("L", (2 * width + 2 * border, height + 2 * border), 30)
    rng = random.Random(seed)
    draw = ImageDraw.Draw(frame)
    for _ in range(4000):
        x, y = rng.randrange(frame.width), rng.randrange(frame.height)
        draw.rectangle((x, y, x + 3, y + 3), fill=120)
    frame.paste(left, (border, border))
    frame.paste(right, (border + width, border))
    return frame


_PRINT_WORDS = (
    "le dit jour par devant nous notaire royal au bailliage et en presence des temoins "
    "soussignes fut present messire jean baptiste de la fontaine seigneur du lieu lequel a "
    "reconnu avoir vendu cede quitte et transporte une piece de terre labourable situee au "
    "terroir de saint pierre contenant environ deux arpents tenant au chemin royal"
).split()


def _type_glyph(draw, ch, x, base, xh, stroke, ink, serif):
    """One letter of drawn type: bowls, stems, an e with its crossbar, ascenders to 1.55
    x-heights and descenders to 0.55 below; serifs are short bars at a stem's ends,
    wider at the foot. Returns the advance."""
    width = round(0.62 * xh) if ch not in "mw" else round(1.0 * xh)
    top = base - xh

    def stem(x0, y0, y1):
        draw.rectangle((x0, y0, x0 + stroke - 1, y1), fill=ink)
        if serif:
            draw.rectangle((x0 - 2 * stroke, y1 - stroke + 1, x0 + 3 * stroke - 1, y1), fill=ink)
            draw.rectangle((x0 - 2 * stroke, y0, x0 + stroke - 1, y0 + stroke - 1), fill=ink)

    if ch in "cosgpqdbe":
        draw.ellipse((x, top, x + width, base), outline=ink, width=stroke)
    if ch == "e":
        mid = top + xh // 2
        draw.rectangle((x, mid - stroke // 2 - 1, x + width, mid + stroke // 2 - 1), fill=ink)
    if ch == "a":
        draw.arc((x, top, x + width, top + xh), 200, 340, fill=ink, width=stroke)
        draw.ellipse((x, top + xh // 2 - 1, x + width, base), outline=ink, width=stroke)
        stem(x + width, top + xh // 4, base)
    if ch in "bdfhklt":
        stem(x + (width if ch == "d" else 0), base - round(1.55 * xh), base)
    if ch in "gjpqy":
        stem(x + (0 if ch == "p" else width), top, base + round(0.55 * xh))
    if ch in "nmhru":
        stem(x, top, base)
        draw.arc((x, top, x + width, top + xh), 180, 360, fill=ink, width=stroke)
        if ch != "r":
            stem(x + width, top + xh // 3, base)
        if ch == "m":
            stem(x + width // 2, top + xh // 3, base)
    if ch in "ivwxz":
        stem(x + width // 2, top, base)
        if ch == "i":
            dot = top - xh // 2
            draw.rectangle(
                (x + width // 2, dot - stroke, x + width // 2 + stroke - 1, dot), fill=ink
            )
    return width + max(1, round(0.18 * xh))


def printed_page(
    dpi: int = 150, points: float = 9, seed: int = 1, *, serif: bool = True
) -> Image.Image:
    """An A4 page of small justified-width printed type drawn letter by letter (no font
    files): at 150 dpi and 9 pt the x-height is about 8 px, and the x-band's top and
    bottom rows, the e crossbars and the serifs are its densest rows."""
    rng = random.Random(seed)
    width, height = round(8.27 * dpi), round(11.69 * dpi)
    image = Image.new("L", (width, height), 230)
    draw = ImageDraw.Draw(image)
    size = points / 72 * dpi
    xh = max(3, round(0.47 * size))
    stroke = max(1, round(size / 15))
    margin = round(20 / 25.4 * dpi)
    base = margin + round(size)
    while base + round(0.3 * size) <= height - margin:
        x = margin
        while True:
            word = rng.choice(_PRINT_WORDS)
            if x + len(word) * 0.8 * xh > width - margin:
                break
            for ch in word:
                x += _type_glyph(draw, ch, x, base, xh, stroke, 30, serif)
            x += round(0.6 * xh)
        base += round(1.25 * size)
    return image.filter(ImageFilter.GaussianBlur(dpi / 500))


def printed_spread(dpi: int = 300, points: float = 9, seed: int = 1, *, serif: bool = False):
    """Two printed pages side by side on a dark backdrop, the right one a little lower."""
    left = printed_page(dpi, points, seed, serif=serif)
    right = printed_page(dpi, points, seed + 1, serif=serif)
    frame = Image.new("L", (2 * left.width + 120, left.height + 140), 25)
    frame.paste(left, (60, 60))
    frame.paste(right, (60 + left.width, 80))
    return frame
