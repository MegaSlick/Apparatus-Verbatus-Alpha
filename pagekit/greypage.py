"""A grey main page (spec 0007): the plain conversion, its exactness, and the check for
colour that a grey page would remove.

- **Conversion.** The grey page is the source-mode page, made through the same chain,
  converted pixel by pixel: no flattening, no tone curve, no sharpening (the tone view
  of spec 0006 stays a separate view). When every decoded pixel of the source has
  equal channels, the grey page takes the common channel, which keeps every intensity,
  and the conversion is exact. Otherwise a named rule decides: `luminance` (Pillow's
  grey conversion, the ITU-R BT.601 weights 0.299, 0.587, 0.114), or one channel
  (`red`, `green`, `blue`); the conversion is then a reviewed one.
- **Colour evidence.** On a reduced copy of the page, the chroma of each pixel is the
  spread between its largest and smallest channel (its distance from the neutral axis,
  in the simple max-minus-min form). The page's own plain paper, the pixels above
  Otsu's threshold of the grey copy, gives the chroma noise: the `colour_paper_percentile`
  level of the paper's chroma. Pixels more than `colour_chroma_margin` above that noise
  are coloured; an opening with a 3 x 3 square drops isolated noise pixels. When what
  remains covers at least `colour_min_area_mm2`, the page holds colour that grey would
  remove, and the measure says where.
"""

from __future__ import annotations

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


def _level_at(histogram: list[int], share: float) -> int:
    total = sum(histogram)
    if total == 0:
        return 0
    seen = 0
    for level, count in enumerate(histogram):
        seen += count
        if seen >= share * total:
            return level
    return 255


def colour_evidence(page: Image.Image, mm_per_px: float, settings: dict[str, Any]) -> dict:
    """The colour measure of a reduced RGB copy of a page: the paper's chroma noise,
    the threshold, the coloured area in mm² and the coloured pixels' box in the copy
    (or None)."""
    grey = page.convert("L")
    spread = chroma(page)
    threshold = otsu_threshold(grey.histogram())
    paper = grey.point(lambda level: 255 if level > threshold else 0)
    noise = _level_at(spread.histogram(paper), settings["colour_paper_percentile"])
    limit = min(254, noise + settings["colour_chroma_margin"])
    coloured = spread.point(lambda level: 255 if level > limit else 0)
    coloured = coloured.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    count = coloured.histogram()[255]
    area = count * mm_per_px * mm_per_px
    return {
        "paper_chroma_noise": noise,
        "chroma_threshold": limit,
        "coloured_mm2": round(area, 2),
        "box": coloured.getbbox(),
        "holds_colour": area >= settings["colour_min_area_mm2"],
    }
