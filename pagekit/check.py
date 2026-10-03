"""Check a declared crop against the master it was cut from.

Every flag means "send this page to review"; nothing here changes a crop. The master
is opened read-only and measured in its stored pixel grid (no EXIF rotation), which is
the grid the crop boxes are given in.

Ink is separated from paper with Otsu's threshold (N. Otsu, "A Threshold Selection
Method from Gray-Level Histograms", IEEE Trans. Systems, Man, and Cybernetics,
SMC-9(1):62-66, 1979). All pixel work goes through Pillow's C operations; Python only
walks histograms and one-pixel-thick profiles.
"""

from __future__ import annotations

import hashlib
import io
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from pagekit import __version__

SCHEMA = "pagekit-crop-check.v1"
THRESHOLDS_PATH = Path(__file__).with_name("thresholds.toml")
SUPPORTED_MODES = frozenset({"1", "L", "LA", "P", "PA", "RGB", "RGBA", "RGBX", "CMYK", "YCbCr"})
INK = 255
EDGES = ("left", "top", "right", "bottom")

Box = tuple[int, int, int, int]
NO_INK_REASON = (
    "No ink detected (the page's dark and light levels are closer than min_ink_contrast); "
    "the crop checks could not run, so the page goes to review."
)


class CheckError(ValueError):
    """The inputs cannot be checked as given."""


def load_thresholds(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The threshold table, with any overrides applied and marked as such."""
    with THRESHOLDS_PATH.open("rb") as handle:
        table = tomllib.load(handle)
    thresholds = {
        name: {"value": entry["value"], "status": entry["status"], "source": "default"}
        for name, entry in sorted(table.items())
    }
    for name, value in sorted((overrides or {}).items()):
        if name not in thresholds:
            raise CheckError(f"unknown threshold {name!r}")
        default = thresholds[name]["value"]
        if isinstance(value, bool) or not isinstance(value, type(default) | int):
            raise CheckError(f"threshold {name!r} must be a number like {default!r}")
        thresholds[name] = {"value": value, "status": "UNMEASURED", "source": "override"}
    if thresholds["remove_isolated_ink"]["value"] not in (0, 1):
        raise CheckError("remove_isolated_ink must be 0 or 1")
    if thresholds["edge_band_px"]["value"] < 1:
        raise CheckError("edge_band_px must be at least 1")
    return thresholds


def parse_box(text: str) -> Box:
    """`x0,y0,x1,y1` as four integers."""
    parts = text.split(",")
    if len(parts) != 4:
        raise CheckError(f"crop {text!r} is not x0,y0,x1,y1")
    try:
        x0, y0, x1, y1 = (int(part.strip()) for part in parts)
    except ValueError as error:
        raise CheckError(f"crop {text!r} is not four integers") from error
    return x0, y0, x1, y1


def otsu_threshold(histogram: list[int]) -> int:
    """The grey level t that best separates levels <= t from levels > t (Otsu 1979)."""
    total = sum(histogram)
    weighted_total = sum(level * count for level, count in enumerate(histogram))
    best_level, best_spread = 0, -1.0
    below_count = below_weighted = 0
    for level in range(255):
        below_count += histogram[level]
        below_weighted += level * histogram[level]
        above_count = total - below_count
        if below_count == 0 or above_count == 0:
            continue
        below_mean = below_weighted / below_count
        above_mean = (weighted_total - below_weighted) / above_count
        spread = below_count * above_count * (below_mean - above_mean) ** 2
        if spread > best_spread:
            best_level, best_spread = level, spread
    return best_level


def _class_means(histogram: list[int], threshold: int) -> tuple[float | None, float | None]:
    def mean(levels: range) -> float | None:
        count = sum(histogram[level] for level in levels)
        if count == 0:
            return None
        return sum(level * histogram[level] for level in levels) / count

    return mean(range(threshold + 1)), mean(range(threshold + 1, 256))


def _median_level(histogram: list[int], levels: range) -> int | None:
    count = sum(histogram[level] for level in levels)
    if count == 0:
        return None
    seen = 0
    for level in levels:
        seen += histogram[level]
        if seen * 2 >= count:
            return level
    return None


def _ink_map(grey: Image.Image, threshold: int) -> Image.Image:
    return grey.point(lambda level: INK if level <= threshold else 0)


def _remove_isolated(ink: Image.Image) -> Image.Image:
    """Clear ink pixels with no ink among their eight neighbours; a stroke one pixel
    thick keeps its pixels, since each touches the next."""
    neighbours = ink.filter(ImageFilter.Kernel((3, 3), (1, 1, 1, 1, 0, 1, 1, 1, 1), scale=1))
    return ImageChops.multiply(ink, neighbours)


def _ink_count(ink: Image.Image, box: Box) -> int:
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return 0
    return ink.crop(box).histogram()[INK]


def _profile(ink: Image.Image, box: Box, along_x: bool) -> list[float]:
    """Ink share per column (along_x) or per row of `box`, quantised to 1/255."""
    x0, y0, x1, y1 = box
    band = ink.crop(box)
    size = (x1 - x0, 1) if along_x else (1, y1 - y0)
    return [value / 255 for value in band.resize(size, Image.BOX).tobytes()]


def _runs(flags: list[bool], origin: int) -> list[list[int]]:
    runs: list[list[int]] = []
    for offset, flag in enumerate(flags):
        if not flag:
            continue
        position = origin + offset
        if runs and runs[-1][1] == position:
            runs[-1][1] = position + 1
        else:
            runs.append([position, position + 1])
    return runs


def _intersect(a: Box, b: Box) -> Box:
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


def _share(part: int, whole: int) -> float:
    return round(part / whole, 6) if whole else 0.0


@dataclass(frozen=True)
class _Page:
    grey: Image.Image
    ink: Image.Image
    area: Box
    threshold: int
    has_ink: bool


def _page_area(grey: Image.Image, threshold: int, edge_share: float) -> Box:
    """Trim border rows and columns that are nearly all ink: scanner backdrop."""
    width, height = grey.size
    ink = _ink_map(grey, threshold)
    columns = _profile(ink, (0, 0, width, height), along_x=True)
    rows = _profile(ink, (0, 0, width, height), along_x=False)

    def first_paper(profile: list[float]) -> int:
        for index, share in enumerate(profile):
            if share < edge_share:
                return index
        return len(profile)

    x0 = first_paper(columns)
    x1 = len(columns) - first_paper(columns[::-1])
    y0 = first_paper(rows)
    y1 = len(rows) - first_paper(rows[::-1])
    if x1 <= x0 or y1 <= y0:
        return 0, 0, width, height
    return x0, y0, x1, y1


def _measure_page(grey: Image.Image, thresholds: dict[str, dict[str, Any]]) -> tuple[_Page, dict]:
    value = {name: entry["value"] for name, entry in thresholds.items()}
    first_threshold = otsu_threshold(grey.histogram())
    area = _page_area(grey, first_threshold, value["page_edge_ink_share"])
    histogram = grey.crop(area).histogram()
    threshold = otsu_threshold(histogram)
    dark_mean, light_mean = _class_means(histogram, threshold)
    contrast = None if dark_mean is None or light_mean is None else light_mean - dark_mean
    has_ink = contrast is not None and contrast >= value["min_ink_contrast"]
    ink = _ink_map(grey, threshold if has_ink else -1)
    if value["remove_isolated_ink"]:
        ink = _remove_isolated(ink)
    background = _median_level(histogram, range(threshold + 1, 256) if has_ink else range(256))
    measurements = {
        "page_area": list(area),
        "page_area_method": "border rows and columns at or above page_edge_ink_share trimmed",
        "ink_threshold_method": "otsu-1979",
        "ink_threshold": threshold,
        "background_grey": background,
        "ink_contrast": None if contrast is None else round(contrast, 3),
        "ink_detected": has_ink,
        "page_ink_pixels": _ink_count(ink, area),
    }
    return _Page(grey, ink, area, threshold, has_ink), measurements


def _check_discarded(page: _Page, crops: list[Box], value: dict[str, Any]) -> dict:
    kept = Image.new("L", page.ink.size, 0)
    for box in crops:
        kept.paste(INK, box)
    discarded = ImageChops.subtract(page.ink, kept).crop(page.area)
    discarded_pixels = discarded.histogram()[INK]
    total = _ink_count(page.ink, page.area)
    share = _share(discarded_pixels, total)
    bbox = discarded.getbbox()
    if bbox is not None:
        ax, ay = page.area[0], page.area[1]
        bbox = (bbox[0] + ax, bbox[1] + ay, bbox[2] + ax, bbox[3] + ay)
    reasons = []
    if discarded_pixels >= value["discarded_min_pixels"]:
        reasons.append(
            f"{discarded_pixels} ink pixels lie inside the page but outside every crop "
            f"(at least {value['discarded_min_pixels']} sends the page to review)."
        )
    if share >= value["discarded_min_share"] and discarded_pixels:
        reasons.append(
            f"{share:.2%} of the page's ink lies outside every crop "
            f"(at least {value['discarded_min_share']:.2%} sends the page to review)."
        )
    return {
        "discarded_ink_pixels": discarded_pixels,
        "discarded_ink_share": share,
        "discarded_ink_bbox": None if bbox is None else list(bbox),
        "flag": bool(reasons),
        "reasons": reasons,
    }


def _edge_bands(box: Box, edge: str, band: int) -> tuple[Box, Box, bool]:
    """Inside band, outside band, and whether the edge runs along x."""
    x0, y0, x1, y1 = box
    if edge == "left":
        return (x0, y0, min(x0 + band, x1), y1), (x0 - band, y0, x0, y1), False
    if edge == "right":
        return (max(x1 - band, x0), y0, x1, y1), (x1, y0, x1 + band, y1), False
    if edge == "top":
        return (x0, y0, x1, min(y0 + band, y1)), (x0, y0 - band, x1, y0), True
    return (x0, max(y1 - band, y0), x1, y1), (x0, y1, x1, y1 + band), True


def _check_edge(page: _Page, index: int, box: Box, edge: str, value: dict[str, Any]) -> dict:
    inside, outside, along_x = _edge_bands(box, edge, value["edge_band_px"])
    inside = _intersect(inside, page.area)
    outside = _intersect(outside, page.area)
    # Both bands must cover the same stretch of the edge to be compared position by position.
    if along_x:
        start, stop = max(inside[0], outside[0]), min(inside[2], outside[2])
        inside = (start, inside[1], stop, inside[3])
        outside = (start, outside[1], stop, outside[3])
    else:
        start, stop = max(inside[1], outside[1]), min(inside[3], outside[3])
        inside = (inside[0], start, inside[2], stop)
        outside = (outside[0], start, outside[2], stop)

    def thickness(band: Box) -> int:
        return max(0, (band[3] - band[1]) if along_x else (band[2] - band[0]))

    compared = thickness(inside) > 0 and thickness(outside) > 0 and stop > start
    result = {
        "crop": index,
        "edge": edge,
        "compared": compared,
        "inside_band_px": thickness(inside) if compared else 0,
        "outside_band_px": thickness(outside) if compared else 0,
        "inside_ink_share": 0.0,
        "outside_ink_share": 0.0,
        "crossing_positions": 0,
        "crossing_spans": [],
        "flag": False,
        "reasons": [],
    }
    # An edge on or past the page's border has nothing beyond it to cut.
    if not compared:
        return result
    inside_area = (inside[2] - inside[0]) * (inside[3] - inside[1])
    outside_area = (outside[2] - outside[0]) * (outside[3] - outside[1])
    result["inside_ink_share"] = _share(_ink_count(page.ink, inside), inside_area)
    result["outside_ink_share"] = _share(_ink_count(page.ink, outside), outside_area)
    inner = _profile(page.ink, inside, along_x)
    outer = _profile(page.ink, outside, along_x)
    crossing = [a > 0 and b > 0 for a, b in zip(inner, outer, strict=True)]
    result["crossing_positions"] = sum(crossing)
    result["crossing_spans"] = _runs(crossing, start)
    if result["crossing_positions"] >= value["edge_cross_min_positions"]:
        result["flag"] = True
        result["reasons"] = [
            f"Ink runs across the {edge} edge of crop {index} at "
            f"{result['crossing_positions']} positions: writing there is likely cut "
            f"(at least {value['edge_cross_min_positions']} sends the page to review)."
        ]
    return result


def _implied_split(crops: list[Box]) -> int | None:
    """The split two side-by-side crops imply: midway between the facing edges."""
    if len(crops) != 2:
        return None
    left, right = sorted(crops)
    shorter = min(left[3] - left[1], right[3] - right[1])
    shared_rows = min(left[3], right[3]) - max(left[1], right[1])
    if shared_rows * 2 < shorter or right[0] <= left[0] or right[2] <= left[2]:
        return None
    overlap = left[2] - right[0]
    if overlap * 2 > min(left[2] - left[0], right[2] - right[0]):
        return None
    return (left[2] + right[0]) // 2


def _check_split(page: _Page, split_x: int, source: str, value: dict[str, Any]) -> dict:
    x0, y0, x1, y1 = page.area
    width = x1 - x0
    profile = _profile(page.ink, page.area, along_x=True)
    middle = width // 2
    reach = max(1, round(value["gutter_search_share"] * width))
    lo, hi = max(0, middle - reach), min(width, middle + reach + 1)
    window = profile[lo:hi]
    lowest = min(window)
    runs = _runs([share <= lowest for share in window], lo)
    # The widest low run is the gutter; ties go to the run nearest the middle.
    run = min(runs, key=lambda r: (-(r[1] - r[0]), abs((r[0] + r[1]) / 2 - middle), r[0]))
    gutter = [run[0] + x0, run[1] + x0]
    centre = (gutter[0] + gutter[1]) / 2
    tolerance = round(value["split_gutter_tolerance_share"] * width)
    if split_x < gutter[0]:
        outside_by = gutter[0] - split_x
    elif split_x >= gutter[1]:
        outside_by = split_x - (gutter[1] - 1)
    else:
        outside_by = 0
    split_column = split_x - x0
    split_ink = profile[split_column] if 0 <= split_column < width else None
    reasons = []
    if outside_by > tolerance:
        reasons.append(
            f"The split at x={split_x} sits {outside_by} px outside the gutter "
            f"(lowest-ink run x={gutter[0]}..{gutter[1] - 1}); more than {tolerance} px "
            f"sends the page to review."
        )
    return {
        "split_x": split_x,
        "split_source": source,
        "search_window": [lo + x0, hi + x0],
        "gutter_run": gutter,
        "gutter_centre_x": centre,
        "gutter_ink_share": round(lowest, 6),
        "split_offset_from_gutter_centre_px": split_x - centre,
        "split_outside_gutter_px": outside_by,
        "split_column_ink_share": None if split_ink is None else round(split_ink, 6),
        "flag": bool(reasons),
        "reasons": reasons,
    }


def _dpi(image: Image.Image) -> list[float] | None:
    dpi = image.info.get("dpi")
    if not dpi or len(dpi) != 2:
        return None
    try:
        values = [round(float(component), 2) for component in dpi]
    except (TypeError, ValueError):
        return None
    if any(component <= 0 for component in values):
        return None
    return values


def _check_resolution(image: Image.Image, value: dict[str, Any]) -> dict:
    width, height = image.size
    dpi = _dpi(image)
    short_side = min(width, height)
    reasons = []
    if dpi is None:
        reasons.append("The master carries no DPI metadata, so its physical size is unknown.")
    if short_side < value["min_short_side_px"]:
        reasons.append(
            f"The master's short side is {short_side} px, below {value['min_short_side_px']} px."
        )
    return {
        "width_px": width,
        "height_px": height,
        "dpi": dpi,
        "short_side_px": short_side,
        "flag": bool(reasons),
        "reasons": reasons,
    }


def _validate_box(box: Box, size: tuple[int, int], what: str) -> None:
    x0, y0, x1, y1 = box
    width, height = size
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise CheckError(f"{what} {list(box)} is not a non-empty box inside {width}x{height}")


def check(
    master: str | Path,
    crops: list[Box],
    split_x: int | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict:
    """The `pagekit-crop-check.v1` report for `crops` cut from `master`."""
    if not crops:
        raise CheckError("at least one crop is required")
    thresholds = load_thresholds(overrides)
    value = {name: entry["value"] for name, entry in thresholds.items()}
    path = Path(master)
    data = path.read_bytes()
    try:
        # Decode the bytes that were hashed, so the digest names what was measured.
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise CheckError("the master has more than one frame; check one page at a time")
            if image.mode not in SUPPORTED_MODES:
                raise CheckError(f"image mode {image.mode!r} is not supported")
            image.load()
            resolution = _check_resolution(image, value)
            grey = image.convert("L")
    except CheckError:
        raise
    except Exception as error:  # any decoder failure means the page cannot be checked
        raise CheckError(f"the master cannot be read as an image: {error}") from error
    crops = [tuple(box) for box in crops]
    for index, box in enumerate(crops):
        _validate_box(box, grey.size, f"crop {index}")
    if split_x is not None and not 0 < split_x < grey.size[0]:
        raise CheckError(f"split_x {split_x} is not inside the master's width {grey.size[0]}")

    page, page_measurements = _measure_page(grey, thresholds)
    discarded = _check_discarded(page, crops, value)
    edges = [
        _check_edge(page, index, box, edge, value)
        for index, box in enumerate(crops)
        for edge in EDGES
    ]
    implied = _implied_split(crops)
    if split_x is not None:
        split = _check_split(page, split_x, "declared", value)
    elif implied is not None:
        split = _check_split(page, implied, "between two side-by-side crops", value)
    else:
        split = None

    flags = []
    if not page.has_ink:
        flags.append({"check": "ink_detection", "reason": NO_INK_REASON})
    flags += [{"check": "ink_discarded", "reason": reason} for reason in discarded["reasons"]]
    for edge in edges:
        flags += [{"check": "ink_cut_at_edge", "reason": reason} for reason in edge["reasons"]]
    if split is not None:
        flags += [{"check": "split_off_gutter", "reason": reason} for reason in split["reasons"]]
    flags += [{"check": "resolution", "reason": reason} for reason in resolution["reasons"]]

    measured = all(entry["status"] == "MEASURED" for entry in thresholds.values())
    return {
        "schema": SCHEMA,
        "tool": {"name": "pagekit", "version": __version__},
        "input": {
            "master": {
                "name": path.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            },
            "crops": [list(box) for box in crops],
            "split_x": split_x,
        },
        "page": page_measurements,
        "checks": {
            "ink_discarded": discarded,
            "ink_cut_at_edge": edges,
            "split_off_gutter": split,
            "resolution": resolution,
        },
        "flags": flags,
        "verdict": "review" if flags else "no_flags",
        "thresholds": thresholds,
        "thresholds_measured": measured,
        "thresholds_note": (
            "Every threshold is a starting guess not yet calibrated on real pages; a flag "
            "means look at this page, and no flag is not proof the crop is right."
            if not measured
            else "Every threshold has been measured on real pages."
        ),
    }


def report_json(report: dict) -> str:
    """The report as canonical JSON: sorted keys, fixed indentation, trailing newline."""
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
