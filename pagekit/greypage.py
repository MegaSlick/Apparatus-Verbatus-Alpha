"""A grey main page (spec 0007): the plain conversion, its exactness, and the check for
colour that a grey page would remove.

- **Conversion.** The grey page is the source-mode page, made through the same chain,
  converted pixel by pixel: no flattening, no tone curve, no sharpening (the tone view
  of spec 0006 stays a separate view). When every decoded pixel of the source has
  equal channels, the grey page takes the common channel, which keeps every intensity,
  and the conversion is exact. Otherwise a named rule decides: `luminance` (Pillow's
  grey conversion, the ITU-R BT.601 weights 0.299, 0.587, 0.114), or one channel
  (`red`, `green`, `blue`); the conversion is then a reviewed one.
- **Colour evidence.** On a reduced copy of the written page only (the margin box,
  within the page box), the chroma of each pixel is the spread between its largest and
  smallest channel (its distance from the neutral axis, in the simple max-minus-min
  form). The page's own plain paper, the pixels above Otsu's threshold of the grey copy,
  gives the chroma noise, from its most neutral part (median plus a multiple of the
  median absolute deviation). Pixels more than `colour_chroma_margin` above that noise
  are coloured; an opening with a square as wide as `colour_thinnest_mm` drops isolated
  specks and keeps lines that wide. When what remains covers at least
  `colour_min_area_mm2`, the page holds colour that grey would remove, and the measure
  says where.
- **What it does not see.** Colour fainter than the margin above the paper's noise;
  lines thinner than `colour_thinnest_mm`; colour covering more than half the paper
  (which then reads as the paper's own tint); and inks that are less saturated than the
  paper itself, such as black and brown on yellowed paper: they are not compared with
  each other, so two such inks may come out as the same grey.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from pagekit.check import otsu_threshold

GREY_RULES = ("luminance", "red", "green", "blue")
RULE_WORDS = {
    "luminance": "luminance (0.299 red + 0.587 green + 0.114 blue)",
    "red": "the red channel",
    "green": "the green channel",
    "blue": "the blue channel",
}
_CHANNEL = {"red": 0, "green": 1, "blue": 2}


def equal_channels(image: Image.Image) -> bool:
    """Whether every pixel of `image` has equal channels (always true for grey)."""
    if image.mode == "L":
        return True
    red, green, blue = image.split()
    return (
        ImageChops.difference(red, green).getbbox() is None
        and ImageChops.difference(green, blue).getbbox() is None
    )


def to_grey(page: Image.Image, exact: bool, rule: str) -> Image.Image:
    """The page in 8-bit grey: the common channel when exact, else by `rule`."""
    if page.mode == "L":
        return page
    if exact:
        return page.getchannel(0)
    if rule == "luminance":
        return page.convert("L")
    return page.getchannel(_CHANNEL[rule])


def chroma(image: Image.Image) -> Image.Image:
    """Max minus min channel of each pixel, as a grey image."""
    red, green, blue = image.split()
    high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    low = ImageChops.darker(ImageChops.darker(red, green), blue)
    return ImageChops.difference(high, low)


def _median_and_spread(histogram: list[int]) -> tuple[float, float]:
    """The median of a histogram's levels and their median absolute deviation."""
    total = sum(histogram)
    if total == 0:
        return 0.0, 0.0

    def median_of(counts: dict[int, int]) -> float:
        seen = 0
        for level in sorted(counts):
            seen += counts[level]
            if 2 * seen >= total:
                return float(level)
        return 0.0

    centre = median_of({level: count for level, count in enumerate(histogram) if count})
    deviations: dict[int, int] = {}
    for level, count in enumerate(histogram):
        if count:
            deviation = round(abs(level - centre))
            deviations[deviation] = deviations.get(deviation, 0) + count
    return centre, median_of(deviations)


def _shifted(image: Image.Image, dx: int, dy: int, fill: int) -> Image.Image:
    moved = Image.new("L", image.size, fill)
    moved.paste(image, (dx, dy))
    return moved


def _opening(marks: Image.Image, side: int) -> Image.Image:
    """An opening of a 0/255 map by a `side` x `side` square: what is narrower than the
    square on either axis goes; lines at least that wide stay."""
    if side <= 1:
        return marks
    if side % 2:
        return marks.filter(ImageFilter.MinFilter(side)).filter(ImageFilter.MaxFilter(side))
    eroded = marks
    for dx in range(side):
        for dy in range(side):
            if dx or dy:
                eroded = ImageChops.darker(eroded, _shifted(marks, -dx, -dy, 0))
    opened = eroded
    for dx in range(side):
        for dy in range(side):
            if dx or dy:
                opened = ImageChops.lighter(opened, _shifted(eroded, dx, dy, 0))
    return opened


def colour_evidence(
    page: Image.Image, mm_per_px: float, settings: dict[str, Any], area: Image.Image | None = None
) -> dict:
    """The colour measure of a reduced RGB copy of a page, inside `area` (a 0/255 map
    of the written page; the whole copy when None): the paper's chroma noise, the
    threshold, the coloured area in mm² and the coloured pixels' box in the copy (or
    None).

    The paper's noise is taken from its most neutral part: the median chroma of the
    paper plus `colour_noise_spread` times its median absolute deviation (scaled to a
    standard deviation by 1.4826), so a pale colour over part of the paper does not
    raise the level it is measured against. Coloured pixels are opened by a square as
    wide as `colour_thinnest_mm`, which drops isolated specks and keeps lines at least
    that wide, such as ruling."""
    if area is None:
        area = Image.new("L", page.size, 255)
    grey = page.convert("L")
    spread = chroma(page)
    threshold = otsu_threshold(grey.histogram(area))
    paper = ImageChops.darker(grey.point(lambda level: 255 if level > threshold else 0), area)
    centre, deviation = _median_and_spread(spread.histogram(paper))
    noise = round(centre + settings["colour_noise_spread"] * 1.4826 * deviation)
    limit = min(254, noise + settings["colour_chroma_margin"])
    coloured = ImageChops.darker(spread.point(lambda level: 255 if level > limit else 0), area)
    # At least two pixels, so a single stray pixel never counts.
    side = max(2, math.ceil(settings["colour_thinnest_mm"] / mm_per_px - 1e-9))
    coloured = _opening(coloured, side)
    count = coloured.histogram()[255]
    measured = count * mm_per_px * mm_per_px
    return {
        "paper_chroma_noise": noise,
        "chroma_threshold": limit,
        "coloured_mm2": round(measured, 2),
        "box": coloured.getbbox(),
        "holds_colour": measured >= settings["colour_min_area_mm2"],
    }
