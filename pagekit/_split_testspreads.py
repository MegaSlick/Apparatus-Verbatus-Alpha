"""Synthetic two-page spreads of many kinds for the split tests; never real material.

`spread_case(kind, dpi, seed)` draws a spread of the named kind and returns it with
its truth: the number of pages and the drawn gutter line (two points, top and bottom
row, in the returned image's pixel grid). Pages are 140 by 200 mm of handwriting-like
lines (pagekit's own `write_block`), scaled with the resolution. Everything is drawn
from a seeded generator, so a case is the same every time.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw

from pagekit._orient_testpages import PAPER, write_block

KINDS = (
    "plain",
    "deep_shadow",
    "one_side_shadow",
    "unequal_widths",
    "off_centre",
    "rotated",
    "blank_page",
    "nearly_blank",
    "two_tones",
    "writing_across",
    "table_across",
    "signature_across",
    "slip_across",
    "edge_stack_board",
    "microfilm",
    "low_contrast_gutter",
    "faint_ink",
    "dense_cursive",
)
BACKDROP = 28


@dataclass
class SpreadCase:
    image: Image.Image
    pages: int
    gutter: tuple[tuple[float, float], tuple[float, float]] | None
    kind: str
    dpi: int

    def gutter_x(self, y: float) -> float:
        (x0, y0), (x1, y1) = self.gutter
        return x0 + (x1 - x0) * (y - y0) / ((y1 - y0) or 1.0)


def _mm(dpi: int, value: float) -> int:
    return round(value * dpi / 25.4)


def _write(draw, box, seed, dpi, *, ink=45, dense=False, lines=None):
    scale = dpi / 150
    x0, y0, x1, y1 = box
    if lines is not None:
        y1 = min(y1, y0 + round(lines * 48 * scale))
    if dense:
        write_block(
            draw,
            (x0, y0, x1, y1),
            seed,
            pitch=round(34 * scale),
            core=round(10 * scale),
            rise=round(16 * scale),
            letter=round(9 * scale),
            stroke=max(1, round(3 * scale)),
            ink=ink,
            ragged=(0.93, 1.0),
            ascenders=0.4,
            descenders=0.35,
        )
    else:
        write_block(
            draw,
            (x0, y0, x1, y1),
            seed,
            pitch=round(48 * scale),
            core=round(14 * scale),
            rise=round(13 * scale),
            letter=round(10 * scale),
            stroke=max(1, round(3 * scale)),
            ink=ink,
        )


def _shade(image: Image.Image, centre: float, half: float, depth: float, side: str = "both"):
    """Darken columns toward `centre`: a gutter shadow `half` px wide on each side (or
    one side), `depth` grey levels deep at the centre, falling off smoothly."""
    width, height = image.size
    row = []
    for x in range(width):
        d = (x - centre) / max(1.0, half)
        if side == "left" and x > centre:
            d = 9.0
        if side == "right" and x < centre:
            d = 9.0
        row.append(round(255 * (1 - depth / 255 * math.exp(-2.5 * d * d))))
    shade = Image.new("L", (width, 1))
    shade.putdata(row)
    return ImageChops.multiply(image, shade.resize((width, height)))


def spread_case(kind: str, dpi: int = 150, seed: int = 1) -> SpreadCase:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    rng = random.Random(f"{kind}-{dpi}-{seed}")
    hard = seed % 2 == 0  # even seeds draw a harder variant of the kind
    left_w, right_w = _mm(dpi, 140), _mm(dpi, 140)
    if kind == "unequal_widths":
        left_w = _mm(dpi, 90 if hard else 115)
    height = _mm(dpi, 200)
    width = left_w + right_w
    gutter = left_w
    tones = (PAPER, PAPER)
    if kind == "two_tones":
        tones = (226, 196)
    if kind == "faint_ink":
        tones = (220, 220)
    image = Image.new("L", (width, height), tones[0])
    image.paste(tones[1], (gutter, 0, width, height))
    draw = ImageDraw.Draw(image)
    margin, inner = _mm(dpi, 15), _mm(dpi, 10)
    if kind == "deep_shadow":
        inner = _mm(dpi, 4 if hard else 7)
    ink = (175 if hard else 150) if kind == "faint_ink" else 45
    dense = kind == "dense_cursive"
    left_box = (margin, margin, gutter - inner, height - margin)
    right_box = (gutter + inner, margin, width - margin, height - margin)
    _write(draw, left_box, seed, dpi, ink=ink, dense=dense)
    if kind == "blank_page":
        pass
    elif kind == "nearly_blank":
        _write(draw, right_box, seed + 1, dpi, ink=ink, lines=3)
    elif kind == "writing_across":
        _write(draw, right_box, seed + 1, dpi, ink=ink)
    else:
        _write(draw, right_box, seed + 1, dpi, ink=ink, dense=dense)

    # The gutter: a thin fold line and a light shadow, or a deep or one-sided shadow.
    # Harder variants drop the fold line where a real gutter may show none.
    line_width = max(1, _mm(dpi, 0.3))
    no_line = hard and kind in ("blank_page", "nearly_blank", "two_tones", "dense_cursive")
    if kind == "deep_shadow":
        image = _shade(image, gutter, _mm(dpi, 18 if hard else 14), 200 if hard else 150)
    elif kind == "one_side_shadow":
        image = _shade(
            image, gutter, _mm(dpi, 12), 150 if hard else 110, side="right" if hard else "left"
        )
    elif kind == "low_contrast_gutter":
        image = _shade(image, gutter, _mm(dpi, 8), 12 if hard else 22)
    elif kind == "two_tones" and hard:
        pass
    else:
        image = _shade(image, gutter, _mm(dpi, 5), 30)
        if not no_line:
            fold = (175 if hard else 150) if kind == "faint_ink" else 70
            ImageDraw.Draw(image).line((gutter, 0, gutter, height - 1), fill=fold, width=line_width)
    draw = ImageDraw.Draw(image)
    stroke = max(1, round(3 * dpi / 150))

    if kind == "writing_across":
        scale = dpi / 150
        for i in range(6 if hard else 3):
            y = margin + _mm(dpi, 60) + i * round(48 * scale)
            span = (gutter - _mm(dpi, 50), y - round(44 * scale), gutter + _mm(dpi, 45), y)
            draw.rectangle(span, fill=tones[0])
            write_block(
                draw,
                span,
                seed + 10 + i,
                pitch=round(48 * scale),
                core=round(14 * scale),
                rise=round(13 * scale),
                letter=round(10 * scale),
                stroke=stroke,
                ragged=(1.0, 1.0),
            )
    if kind == "table_across":
        top, rows = (margin, 22) if hard else (margin + _mm(dpi, 40), 8)
        left, right = gutter - _mm(dpi, 60), gutter + _mm(dpi, 60)
        pitch = _mm(dpi, 8)
        for r in range(rows + 1):
            draw.line((left, top + r * pitch, right, top + r * pitch), fill=60, width=stroke)
        for x in (left, gutter - _mm(dpi, 25), gutter + _mm(dpi, 20), right):
            draw.line((x, top, x, top + rows * pitch), fill=60, width=stroke)
    if kind == "signature_across":
        y = height - margin - _mm(dpi, 12)
        for i in range(4):
            draw.arc(
                (
                    gutter - _mm(dpi, 35) + i * 3,
                    y - _mm(dpi, 10) + i * 2,
                    gutter + _mm(dpi, 30),
                    y + _mm(dpi, 8),
                ),
                15,
                345,
                fill=40,
                width=stroke + 1,
            )
    if kind == "slip_across":
        sw, sh = _mm(dpi, 70), _mm(dpi, 50)
        sx, sy = gutter - _mm(dpi, 30), _mm(dpi, 70)
        draw.rectangle((sx, sy, sx + sw, sy + sh), fill=238, outline=150, width=stroke)
        scale = dpi / 150
        write_block(
            draw,
            (sx + _mm(dpi, 5), sy + _mm(dpi, 5), sx + sw - _mm(dpi, 5), sy + sh - _mm(dpi, 5)),
            seed + 20,
            pitch=round(40 * scale),
            core=round(12 * scale),
            rise=round(10 * scale),
            letter=round(9 * scale),
            stroke=stroke,
        )

    gutter_line = ((float(gutter), 0.0), (float(gutter), float(height - 1)))
    frame = image
    if kind == "edge_stack_board":
        board, stack = _mm(dpi, 12), _mm(dpi, 6)
        frame = Image.new("L", (width + 2 * (board + stack), height + 2 * stack), BACKDROP)
        fd = ImageDraw.Draw(frame)
        fd.rectangle((board, 0, frame.width - board - 1, frame.height - 1), fill=200)
        for i in range(0, stack, max(2, stack // 6)):
            fd.line((board + i, 0, board + i, frame.height), fill=90, width=1)
            fd.line(
                (frame.width - board - 1 - i, 0, frame.width - board - 1 - i, frame.height),
                fill=90,
                width=1,
            )
        frame.paste(image, (board + stack, stack))
        offset = board + stack
        gutter_line = ((gutter + offset, 0.0), (gutter + offset, float(frame.height - 1)))
    if kind == "microfilm":
        border = _mm(dpi, 25 if hard else 15)
        frame = Image.new("L", (width + 2 * border, height + 2 * border), 12)
        frame.paste(image, (border, border))
        gutter_line = ((gutter + border, 0.0), (gutter + border, float(frame.height - 1)))
    if kind == "off_centre":
        left_pad, right_pad, pad = _mm(dpi, 8), _mm(dpi, 70), _mm(dpi, 10)
        if hard:
            left_pad, right_pad, pad = _mm(dpi, 110), _mm(dpi, 15), _mm(dpi, 40)
        frame = Image.new(
            "L", (width + left_pad + right_pad, height + 2 * pad), BACKDROP if hard else 205
        )
        frame.paste(image, (left_pad, pad))
        gutter_line = ((gutter + left_pad, 0.0), (gutter + left_pad, float(frame.height - 1)))
    if kind == "rotated":
        angle = rng.choice((4.5, -4.0) if hard else (3.0, -2.5))
        pad = _mm(dpi, 15)
        canvas = Image.new("L", (width + 2 * pad, height + 2 * pad), BACKDROP)
        canvas.paste(image, (pad, pad))
        frame = canvas.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=BACKDROP)
        cx, cy = canvas.width / 2, canvas.height / 2
        a = math.radians(angle)

        def turn(x, y):
            # Image.rotate turns counterclockwise by `angle` about the centre.
            dx, dy = x - cx, y - cy
            return cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(
                a
            )

        top = turn(gutter + pad, pad)
        bottom = turn(gutter + pad, pad + height - 1)
        gutter_line = (top, bottom)
    return SpreadCase(frame, 2, gutter_line, kind, dpi)


def single_page_case(dpi: int = 150, seed: int = 1) -> SpreadCase:
    """One page of writing on a wide pale backdrop: a frame the proportions take for a
    spread, holding one page."""
    page_w, height = _mm(dpi, 140), _mm(dpi, 200)
    image = Image.new("L", (page_w, height), PAPER)
    _write(
        ImageDraw.Draw(image),
        (_mm(dpi, 15), _mm(dpi, 15), page_w - _mm(dpi, 15), height - _mm(dpi, 15)),
        seed,
        dpi,
    )
    frame = Image.new("L", (round(2.1 * page_w), height + _mm(dpi, 10)), 205)
    frame.paste(image, (round(0.55 * page_w), _mm(dpi, 5)))
    return SpreadCase(frame, 1, None, "single_page", dpi)
