"""A grey main page: the plain conversion, its exactness, and the check for
colour that a grey page would remove.

- **Conversion.** The grey page is the source-mode page, made through the same chain,
  converted pixel by pixel: no flattening, no tone curve, no sharpening (the tone view
  stays a separate view). When every decoded pixel of the source has
  equal channels, the grey page takes the common channel, which keeps every intensity,
  and the conversion is exact. Otherwise a named rule decides: `luminance` (Pillow's
  grey conversion, the ITU-R BT.601 weights 0.299, 0.587, 0.114), or one channel
  (`red`, `green`, `blue`); the conversion is then a reviewed one.
- **Colour evidence.** On a reduced copy of the written page only (the margin box,
  within the page box), each pixel's colour difference from the page's own paper
  colour is measured on two opponent axes (red against green, yellow against blue):
  across the paper's hue in full, along it only beyond the paper's saturation or past
  neutral (see colour_difference). The page's own plain paper, the pixels above Otsu's
  threshold of the grey copy, gives the noise, from its most neutral part (median plus
  a multiple of the median absolute deviation). Pixels more than
  `colour_chroma_margin` above that noise are coloured; connected pieces smaller than
  `colour_speck_mm2` are specks and dropped. When what remains covers at least
  `colour_min_area_mm2`, the page holds colour that grey would remove, and the measure
  says where.
- **What it does not see.** Colour fainter than the margin above the paper's noise;
  specks smaller than `colour_speck_mm2`; colour covering more than half the paper
  (which then reads as the paper's own colour); and inks between neutral and the
  paper's own hue, such as black and brown on yellowed paper: they are not compared
  with each other, so two such inks may come out as the same grey.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageChops, ImageMath

from pagekit._box_common import components
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


def _opponent(page: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Two opponent colour axes of each pixel, as float images: red against green,
    and yellow (red and green) against blue. Neutral greys are 0 on both."""
    red, green, blue = (band.convert("F") for band in page.split())
    across = ImageMath.lambda_eval(lambda e: e["r"] - e["g"], r=red, g=green)
    down = ImageMath.lambda_eval(lambda e: (e["r"] + e["g"]) / 2 - e["b"], r=red, g=green, b=blue)
    return across, down


def _median_float(image: Image.Image, mask: Image.Image) -> float:
    """The median of a float image's values under `mask`, to half a level."""
    shifted = ImageMath.lambda_eval(lambda e: (e["v"] + 256.0) / 2.0, v=image).convert("L")
    centre, _ = _median_and_spread(shifted.histogram(mask))
    return centre * 2.0 - 256.0


def colour_difference(page: Image.Image, paper: Image.Image) -> tuple[Image.Image, tuple]:
    """How far each pixel's colour lies from the paper's own colour, beyond what a
    darker or lighter, less or equally saturated ink of the paper's hue would show
    (an 8-bit image), and the paper's colour on the two opponent axes.

    Each pixel's colour is a point on two opponent axes. The paper's colour p is the
    median over the paper. A pixel's difference v from p is split into the part along
    p and the part across it. Across counts in full. Along counts only beyond the paper
    (more saturated in the paper's hue) or beyond neutral (towards the opposite hue):
    an ink that only loses the paper's tint, such as black or faded brown on yellowed
    paper, does not count."""
    across, down = _opponent(page)
    pa, pb = _median_float(across, paper), _median_float(down, paper)
    length = math.hypot(pa, pb)
    if length < 1.0:  # neutral paper: plain distance from neutral
        found = ImageMath.lambda_eval(
            lambda e: (e["a"] * e["a"] + e["b"] * e["b"]) ** 0.5, a=across, b=down
        )
        return found.convert("L"), (pa, pb)
    ux, uy = pa / length, pb / length

    def measure(e):
        va, vb = e["a"] - pa, e["b"] - pb
        along = va * ux + vb * uy
        side = abs(va * uy - vb * ux)
        beyond = e["max"](along, 0.0) + e["max"](-length - along, 0.0)
        return (side * side + beyond * beyond) ** 0.5

    found = ImageMath.lambda_eval(measure, a=across, b=down)
    return found.convert("L"), (pa, pb)


def colour_evidence(
    page: Image.Image, mm_per_px: float, settings: dict[str, Any], area: Image.Image | None = None
) -> dict:
    """The colour measure of a reduced RGB copy of a page, inside `area` (a 0/255 map
    of the written page; the whole copy when None): the paper's colour noise, the
    threshold, the coloured area in mm² and the coloured pixels' box in the copy (or
    None).

    Each pixel's colour difference from the page's own paper colour is measured (see
    colour_difference), so pale colour on cream or yellow paper is seen as clearly as on
    white. The paper's noise is taken from its most neutral part: the median difference
    over the paper plus `colour_noise_spread` times its median absolute deviation
    (scaled to a standard deviation by 1.4826), so a pale colour over part of the paper
    does not raise the level it is measured against. Coloured pixels are kept by
    connected piece: a piece smaller than `colour_speck_mm2` is a speck and dropped, so
    stray dust goes and lines of any width, such as fine ruling, stay."""
    if area is None:
        area = Image.new("L", page.size, 255)
    grey = page.convert("L")
    threshold = otsu_threshold(grey.histogram(area))
    paper = ImageChops.darker(grey.point(lambda level: 255 if level > threshold else 0), area)
    difference, paper_colour = colour_difference(page, paper)
    centre, deviation = _median_and_spread(difference.histogram(paper))
    noise = round(centre + settings["colour_noise_spread"] * 1.4826 * deviation)
    limit = min(254, noise + settings["colour_chroma_margin"])
    coloured = ImageChops.darker(difference.point(lambda level: 255 if level > limit else 0), area)
    smallest = max(2, math.ceil(settings["colour_speck_mm2"] / (mm_per_px * mm_per_px) - 1e-9))
    pieces = [part for part in components(coloured) if part.area >= smallest]
    count = sum(part.area for part in pieces)
    box = None
    if pieces:
        box = (
            min(part.x0 for part in pieces),
            min(part.y0 for part in pieces),
            max(part.x1 for part in pieces),
            max(part.y1 for part in pieces),
        )
    measured = count * mm_per_px * mm_per_px
    return {
        "paper_colour": [round(paper_colour[0], 1), round(paper_colour[1], 1)],
        "paper_chroma_noise": noise,
        "chroma_threshold": limit,
        "coloured_mm2": round(measured, 2),
        "box": box,
        "holds_colour": measured >= settings["colour_min_area_mm2"],
    }
