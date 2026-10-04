"""The grey tone view: a page in grey with its lighting flattened and faint ink lifted.

The view has the input's size, pixel for pixel in the same place; it never binarises
and never moves a pixel. It is made on request for readers that see the page in grey,
and is never applied to the master or to the prepared page. Deliberately gentle:
published work on vision-language readers of historical handwriting found that
binarisation, denoising and strong local contrast equalisation raised the error rate,
and that only mild sharpening helped (Farazi et al., 2026, arXiv:2608.22366).

Steps, in order:

1. Grey. A grey source is used as it is. A colour source becomes grey by a rule named in
   the settings: luminance, the darkest of the three channels, or one channel. The
   minimum and the blue channel often separate brown iron-gall ink from yellowed paper
   better than luminance; which is right is to be measured, so the record names the
   rule.
2. Flat-field correction. The paper background is estimated without the ink on a
   reduced copy by a grey-scale morphological closing (a maximum filter, which removes
   dark strokes narrower than its window, followed by a minimum filter of the same
   window, which restores the paper's slow shape), smoothed with a small box blur and
   brought back to full size bilinearly. The grey image is divided by that estimate and
   scaled so the paper lands at a set level just below white. Background estimation and
   division follow the flat-field idea in B. Gatos, I. Pratikakis and S. J. Perantonis,
   "Adaptive degraded document image binarization", Pattern Recognition 39(3), 2006,
   and S. Lu, B. Su and C. L. Tan, "Document image binarization using background
   estimation and stroke edges", IJDAR 13(4), 2010; the closing as a local background
   estimator is the standard grey-scale morphology of J. Serra, "Image Analysis and
   Mathematical Morphology", Academic Press, 1982. Guards: when too little of the page
   is paper (a page almost covered in writing or in shadow), no correction is made and
   the record says so; the estimate is never taken below half the page's paper level,
   so a solid dark area wider than the window (a blot, a seal) is not lifted to paper.
   Limits: at a sharp edge between bright paper and a dark stain the maximum filter
   leaks bright paper half a window into the stain, which leaves a darker rim of that
   width inside the stain (plus the blur radius on both sides); and any dark area the
   window fits inside is treated as background up to the floor above.
3. Faint-ink lift. One smooth, strictly increasing tone curve anchored at the paper
   level: below it, f(u) = u - s * u * (1 - u^n) in units of the paper level, which
   darkens mid-tones near the paper more than ink that is already dark (slope 1 + s * n
   at the paper, 1 - s at black) so faint strokes gain contrast against the paper;
   above it, a short soft knee whose slope decays from 1 + s * n to just below 1, so
   white stays white. It is strictly increasing, so no two input levels merge by
   clipping; see `lift_lut`.
4. Mild sharpening, off by default: Pillow's unsharp mask with a radius below the
   stroke width and a small amount.

Everything pixel-wide goes through Pillow's C operations; the flattened level is kept
at 16-bit precision until the tone curve quantises it once. Python only walks
histograms and builds a lookup table. Pillow is the only dependency.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageMath

from pagekit import __version__
from pagekit.check import otsu_threshold

SCHEMA = "pagekit-tone-view.v1"
SETTINGS_PATH = Path(__file__).with_name("thresholds_tone.toml")
SUPPORTED_MODES = frozenset({"1", "L", "LA", "P", "PA", "RGB", "RGBA", "RGBX", "CMYK", "YCbCr"})
GREY_SOURCE_MODES = frozenset({"1", "L", "LA"})
GREY_RULES = ("luminance", "min", "red", "green", "blue")
CHANNEL = {"red": 0, "green": 1, "blue": 2}
# Flattened levels are kept at 1/256 of a grey level until the tone curve maps them.
FINE = 256
# Width of the soft knee above the paper level, as a share of the range up to white.
KNEE = 0.1
# Reduced-copy pixels at or above this share (of 255) of their background estimate are
# paper-like and calibrate the estimate; ink and leaked stain edges lie below it.
PAPER_RATIO_FLOOR = 200
TIFF_SUFFIXES = frozenset({".tif", ".tiff"})


class ToneError(ValueError):
    """The input cannot be turned into a tone view as given."""


def load_settings(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The settings table, with any overrides applied and marked as such."""
    with SETTINGS_PATH.open("rb") as handle:
        table = tomllib.load(handle)
    settings = {
        name: {"value": entry["value"], "status": entry["status"], "source": "default"}
        for name, entry in sorted(table.items())
    }
    for name, value in sorted((overrides or {}).items()):
        if name not in settings:
            raise ToneError(f"unknown setting {name!r}")
        default = settings[name]["value"]
        if isinstance(default, str):
            if not isinstance(value, str):
                raise ToneError(f"setting {name!r} must be a word like {default!r}")
        elif isinstance(value, bool) or not isinstance(value, type(default) | int):
            raise ToneError(f"setting {name!r} must be a number like {default!r}")
        settings[name] = {"value": value, "status": "UNMEASURED", "source": "override"}
    value = {name: entry["value"] for name, entry in settings.items()}
    if value["grey_rule"] not in GREY_RULES:
        raise ToneError(f"grey_rule must be one of {', '.join(GREY_RULES)}")
    if not 128 <= value["paper_level"] <= 254:
        raise ToneError("paper_level must be between 128 and 254: just below white, never white")
    if value["work_long_side_px"] < 64:
        raise ToneError("work_long_side_px must be at least 64")
    if value["background_window_px"] < 3:
        raise ToneError("background_window_px must be at least 3")
    if value["background_blur_px"] < 0:
        raise ToneError("background_blur_px must not be negative")
    if not 0 < value["min_paper_share"] <= 1:
        raise ToneError("min_paper_share must be above 0 and at most 1")
    if not 0 <= value["min_background_share"] < 1:
        raise ToneError("min_background_share must be at least 0 and below 1")
    if value["min_ink_contrast"] < 0:
        raise ToneError("min_ink_contrast must not be negative")
    if not 0 <= value["lift_strength"] <= 0.4:
        raise ToneError("lift_strength must be between 0 and 0.4")
    if not 1 <= value["lift_shape"] <= 8:
        raise ToneError("lift_shape must be between 1 and 8")
    if value["sharpen"] not in (0, 1):
        raise ToneError("sharpen must be 0 or 1")
    if value["sharpen_radius_px"] <= 0 or value["sharpen_percent"] < 0:
        raise ToneError("sharpen_radius_px must be positive and sharpen_percent not negative")
    return settings


def _settings_digest(settings: dict[str, dict[str, Any]]) -> str:
    values = {name: entry["value"] for name, entry in settings.items()}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


# -- step 1: grey -------------------------------------------------------------------


def to_grey(image: Image.Image, rule: str) -> tuple[Image.Image, str]:
    """The source as an "L" image, and the name of the rule that was applied."""
    if image.mode in GREY_SOURCE_MODES:
        return image.convert("L"), "source-grey"
    colour = image.convert("RGB")
    if rule == "luminance":
        return colour.convert("L"), rule
    red, green, blue = colour.split()
    if rule == "min":
        return ImageChops.darker(ImageChops.darker(red, green), blue), rule
    return (red, green, blue)[CHANNEL[rule]], rule


# -- step 2: background and flattening ----------------------------------------------


def _shift(image: Image.Image, dx: int, dy: int) -> Image.Image:
    """The image moved so that pixel (x, y) shows the source's (x + dx, y + dy); what
    comes from outside the source is 0."""
    width, height = image.size
    return image.crop((dx, dy, dx + width, dy + height))


def _running_max(image: Image.Image, length: int, along_x: bool) -> Image.Image:
    """At each pixel, the maximum of the `length` pixels starting there along one axis,
    built by doubling spans: a logarithmic number of whole-image operations."""
    result = image
    span = 1
    while span * 2 <= length:
        result = ImageChops.lighter(
            result, _shift(result, span if along_x else 0, 0 if along_x else span)
        )
        span *= 2
    if span < length:
        rest = length - span
        result = ImageChops.lighter(
            result, _shift(result, rest if along_x else 0, 0 if along_x else rest)
        )
    return result


def window_max(image: Image.Image, window: int) -> Image.Image:
    """Maximum over a centred square window of odd side `window` (grey-scale dilation).
    The area outside the image never wins."""
    reach = (window - 1) // 2
    width, height = image.size
    # A zero border of `reach` on every side, so the forward running max of `window`
    # pixels at (x, y) covers the source's x - reach .. x + reach, truncated at the edges.
    padded = image.crop((-reach, -reach, width + reach, height + reach))
    result = _running_max(padded, window, along_x=True)
    result = _running_max(result, window, along_x=False)
    return result.crop((0, 0, width, height))


def window_min(image: Image.Image, window: int) -> Image.Image:
    """Minimum over a centred square window (grey-scale erosion), as the dilation of
    the inverted image; the area outside the image never wins."""
    return ImageChops.invert(window_max(ImageChops.invert(image), window))


def closing(image: Image.Image, window: int) -> Image.Image:
    """Grey-scale closing: dark features narrower than the window are removed and the
    slowly varying bright paper is kept. The result is nowhere darker than the input."""
    return window_min(window_max(image, window), window)


def _level_at(histogram: list[int], share: float) -> int:
    """The grey level at which the running count first reaches `share` of the total."""
    total = sum(histogram)
    target = share * total
    seen = 0
    for level, count in enumerate(histogram):
        seen += count
        if seen >= target and count:
            return level
    return 255


def _class_mean(histogram: list[int], levels: range) -> float | None:
    count = sum(histogram[level] for level in levels)
    if count == 0:
        return None
    return sum(level * histogram[level] for level in levels) / count


@dataclass(frozen=True)
class _Background:
    reduced: Image.Image
    estimate: Image.Image
    window: int
    blur: int
    floor: int
    paper_ratio: float

    def summary(self) -> dict[str, Any]:
        """The typical paper level (the estimate's median, brought down to the paper's
        own level by `paper_ratio`) and its spread (5th to 95th percentile)."""
        histogram = self.estimate.histogram()
        return {
            "level": round(_level_at(histogram, 0.5) * self.paper_ratio),
            "spread": _level_at(histogram, 0.95) - _level_at(histogram, 0.05),
        }


def _reduce(grey: Image.Image, long_side: int) -> tuple[Image.Image, int]:
    """The grey image reduced by an integer factor so its long side is at most `long_side`."""
    factor = max(1, math.ceil(max(grey.size) / long_side))
    return (grey.reduce(factor) if factor > 1 else grey), factor


def _background(
    reduced: Image.Image, factor: int, floor: int, value: dict[str, Any]
) -> _Background:
    """The paper background of a reduced copy: its closing, smoothed, never below `floor`.

    The closing follows the brightest grain, so the estimate sits a little above the
    paper's typical level; `paper_ratio` is the median of paper / estimate over the
    paper-like pixels (ratio at least PAPER_RATIO_FLOOR), and dividing by it as well
    lands the typical paper exactly on the set level."""
    window = max(3, round(value["background_window_px"] / factor) | 1)
    blur = round(value["background_blur_px"] / factor)
    estimate = closing(reduced, window)
    if blur > 0:
        estimate = estimate.filter(ImageFilter.BoxBlur(blur))
    if floor > 0:
        estimate = ImageChops.lighter(estimate, Image.new("L", estimate.size, floor))
    ratio = ImageMath.lambda_eval(
        lambda d: d["min"](d["a"] * 255.0 / d["max"](d["b"], 1.0) + 0.5, 255.0),
        a=reduced.convert("F"),
        b=estimate.convert("F"),
    ).convert("L")
    histogram = ratio.histogram()
    paper_like = [0] * PAPER_RATIO_FLOOR + histogram[PAPER_RATIO_FLOOR:]
    paper_ratio = _level_at(paper_like, 0.5) / 255 if sum(paper_like) else 1.0
    return _Background(reduced, estimate, window, blur, floor, paper_ratio)


def _paper_samples(reduced: Image.Image, value: dict[str, Any]) -> dict[str, Any]:
    """How much of the reduced page is paper, by Otsu's split (Otsu 1979) when the page
    carries ink contrast; a page without it is all paper."""
    histogram = reduced.histogram()
    threshold = otsu_threshold(histogram)
    dark = _class_mean(histogram, range(threshold + 1))
    light = _class_mean(histogram, range(threshold + 1, 256))
    contrast = None if dark is None or light is None else round(light - dark, 3)
    has_ink = contrast is not None and contrast >= value["min_ink_contrast"]
    total = sum(histogram)
    if has_ink:
        paper = sum(histogram[threshold + 1 :])
        paper_histogram = [0] * (threshold + 1) + histogram[threshold + 1 :]
    else:
        paper = total
        paper_histogram = histogram
    return {
        "ink_threshold": threshold if has_ink else None,
        "ink_contrast": contrast,
        "paper_share": round(paper / total, 6) if total else 0.0,
        "paper_class_level": _level_at(paper_histogram, 0.5),
    }


def _fine_levels(
    grey: Image.Image, background: _Background | None, paper_level: int
) -> Image.Image:
    """The grey image in 1/256 grey levels ("I" mode): divided by the background and
    scaled so the paper lands at `paper_level` when a background is given, else as it
    is. Values are rounded, never truncated, and clamped to the 16-bit range."""
    if background is None:
        return ImageMath.lambda_eval(
            lambda d: d["a"] * float(FINE) + 0.5, a=grey.convert("F")
        ).convert("I")
    gain = ImageMath.lambda_eval(
        lambda d: float(paper_level * FINE / background.paper_ratio) / d["max"](d["b"], 1.0),
        b=background.estimate.convert("F"),
    ).resize(grey.size, Image.BILINEAR)
    fine = ImageMath.lambda_eval(
        lambda d: d["min"](d["a"] * d["g"] + 0.5, float(FINE * 256 - 1)),
        a=grey.convert("F"),
        g=gain,
    )
    return fine.convert("I")


# -- step 3: the lift ---------------------------------------------------------------


def lift_curve(x: float, anchor: float, strength: float, shape: int) -> float:
    """The tone curve on [0, 1] with the paper at `anchor` (also in [0, 1]).

    Below the anchor, in units u = x / anchor: f = u - s * u * (1 - u**n); its slope is
    1 - s at black and 1 + s * n at the paper, so it is strictly increasing for s < 1
    and leaves the anchor where it is. Above the anchor, in units t of the range up to
    white: a knee whose slope starts at 1 + s * n and decays exponentially (scale KNEE)
    to just below 1, with white mapped to white. The two halves meet with the same
    value and slope.
    """
    if strength == 0 or anchor <= 0 or anchor >= 1:
        return x
    if x <= anchor:
        u = x / anchor
        return anchor * (u - strength * u * (1 - u**shape))
    slope = 1 + strength * shape
    t = (x - anchor) / (1 - anchor)
    decay = math.exp(-1 / KNEE)
    rise = (slope - 1) / (1 / KNEE - 1 + decay)
    fall = rise * (1 - decay)
    h = t + rise * (1 - math.exp(-t / KNEE)) - fall * t
    return anchor + (1 - anchor) * h


def lift_lut(anchor_level: int, strength: float, shape: int) -> list[int]:
    """The 8-bit output for every 1/256-level input, through `lift_curve`. Rounded once
    and non-decreasing; strictly increasing wherever the curve's slope allows."""
    anchor = anchor_level / 255
    table = []
    for fine in range(FINE * 256):
        x = min(1.0, fine / (FINE * 255))
        table.append(min(255, round(255 * lift_curve(x, anchor, strength, shape))))
    return table


# -- the view -----------------------------------------------------------------------


def _open_image(image: Image.Image) -> None:
    if getattr(image, "n_frames", 1) != 1:
        raise ToneError("the page has more than one frame; make one view at a time")
    if image.mode not in SUPPORTED_MODES:
        raise ToneError(f"image mode {image.mode!r} is not supported")


def tone(image: Image.Image, overrides: dict[str, Any] | None = None) -> tuple[Image.Image, dict]:
    """The grey tone view of `image` and its `pagekit-tone-view.v1` record."""
    settings = load_settings(overrides)
    value = {name: entry["value"] for name, entry in settings.items()}
    _open_image(image)
    image.load()

    grey, rule_applied = to_grey(image, value["grey_rule"])
    reduced, factor = _reduce(grey, value["work_long_side_px"])
    samples = _paper_samples(reduced, value)
    floor = round(value["min_background_share"] * samples["paper_class_level"])
    background = _background(reduced, factor, floor, value)
    before = background.summary()
    flatten = samples["paper_share"] >= value["min_paper_share"]
    if flatten:
        reason = None
        anchor = value["paper_level"]
    else:
        reason = (
            f"only {samples['paper_share']:.1%} of the reduced page is paper (above the ink "
            f"threshold); at least {value['min_paper_share']:.0%} is needed to trust a "
            "background estimate, so the lighting was left as it is"
        )
        anchor = samples["paper_class_level"]
    fine = _fine_levels(grey, background if flatten else None, value["paper_level"])
    strength, shape = value["lift_strength"], value["lift_shape"]
    view = fine.point(lift_lut(anchor, strength, shape), "L")
    sharpen = value["sharpen"] == 1
    if sharpen:
        view = view.filter(
            ImageFilter.UnsharpMask(value["sharpen_radius_px"], value["sharpen_percent"], 0)
        )
    after = None
    if flatten:
        after_floor = round(value["min_background_share"] * value["paper_level"])
        after = _background(
            _reduce(view, value["work_long_side_px"])[0], factor, after_floor, value
        ).summary()

    steps = [f"grey:{rule_applied}"]
    steps.append("flatten" if flatten else "flatten:skipped")
    steps.append(f"lift:{strength}" if strength else "lift:off")
    if sharpen:
        steps.append("sharpen")
    measured = all(entry["status"] == "MEASURED" for entry in settings.values())
    record = {
        "schema": SCHEMA,
        "tool": {"name": "pagekit", "version": __version__},
        "input": {"mode": image.mode, "width_px": image.size[0], "height_px": image.size[1]},
        "steps": steps,
        "grey": {"rule": value["grey_rule"], "applied": rule_applied, "source_mode": image.mode},
        "flatten": {
            "applied": flatten,
            "reason": reason,
            "method": (
                "grey-scale closing on a reduced copy, box blur, bilinear return to full "
                "size, division so the paper lands at paper_level"
            ),
            "reduce_factor": factor,
            "reduced_size": list(reduced.size),
            "window_reduced_px": background.window,
            "blur_reduced_px": background.blur,
            "background_floor_level": background.floor,
            "paper_ratio_median": round(background.paper_ratio, 6),
            "ink_threshold": samples["ink_threshold"],
            "ink_contrast": samples["ink_contrast"],
            "paper_share": samples["paper_share"],
            "paper_class_level": samples["paper_class_level"],
            "paper_level_before": before["level"],
            "paper_spread_before": before["spread"],
            "paper_level_after": None if after is None else after["level"],
            "paper_spread_after": None if after is None else after["spread"],
        },
        "lift": {
            "applied": strength > 0,
            "strength": strength,
            "shape": shape,
            "anchor_level": anchor,
            "anchor_source": "paper_level" if flatten else "measured paper class",
            "slope_at_anchor": round(1 + strength * shape, 4),
            "slope_at_black": round(1 - strength, 4),
        },
        "sharpen": {
            "applied": sharpen,
            "radius_px": value["sharpen_radius_px"],
            "percent": value["sharpen_percent"],
        },
        "settings": settings,
        "settings_sha256": _settings_digest(settings),
        "settings_measured": measured,
        "settings_note": (
            "Every setting is a starting guess not yet calibrated on real pages."
            if not measured
            else "Every setting has been measured on real pages."
        ),
    }
    return view, record


def tone_file(
    path: str | Path, overrides: dict[str, Any] | None = None
) -> tuple[Image.Image, dict]:
    """The view of the page at `path`; the record names the bytes that were read."""
    path = Path(path)
    data = path.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as image:
            _open_image(image)
            image.load()
            dpi = image.info.get("dpi")
            view, record = tone(image, overrides)
    except ToneError:
        raise
    except Exception as error:  # any decoder failure means the page cannot be viewed
        raise ToneError(f"the page cannot be read as an image: {error}") from error
    record["input"] = {
        "name": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "dpi": None if not dpi else [round(float(component), 2) for component in dpi],
        **record["input"],
    }
    return view, record


def write_view(view: Image.Image, path: str | Path, dpi: list[float] | None = None) -> dict:
    """Write the view as a lossless (deflate) TIFF and describe the bytes written."""
    path = Path(path)
    if path.suffix.lower() not in TIFF_SUFFIXES:
        raise ToneError(f"the view is written as TIFF; {path.name!r} must end in .tif or .tiff")
    options: dict[str, Any] = {"compression": "tiff_adobe_deflate"}
    if dpi:
        options["dpi"] = tuple(dpi)
    buffer = io.BytesIO()
    view.save(buffer, format="TIFF", **options)
    data = buffer.getvalue()
    path.write_bytes(data)
    return {"name": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def record_json(record: dict) -> str:
    """The record as canonical JSON: sorted keys, fixed indentation, trailing newline."""
    return json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
