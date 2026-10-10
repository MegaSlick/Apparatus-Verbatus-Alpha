"""Volume-wide checks: a page unlike the rest of its batch.

Within one batch most pages have a similar skew, a similar content size and similar
margins; a page far from the rest is often a detection error. Once every page has its
values, each measurement is collected over the batch: the skew in degrees, the content
box's width and height, and the four margins between the page box and the content box,
in millimetres. For each, the centre is the median and the spread is 1.4826 times the
median absolute deviation (which matches the standard deviation for normally
distributed values while hardly moving for the outliers being sought), held at least at
a floor so that a very even batch does not flag tiny differences. A page whose
measurement lies more than `volume_outlier_distance` spreads from the centre is flagged,
naming the measurement and how far off it is. A measurement is compared only when at
least `volume_min_pages` pages have it.

Robust centre and spread after P. J. Rousseeuw and C. Croux, "Alternatives to the
median absolute deviation", Journal of the American Statistical Association 88(424),
1993.

Blank pages (no content box) are left out, since they have no writing to measure. The
millimetre measurements of a page whose resolution is missing or implausible are left
out too. A page is not flagged on a measurement whose values were all set by hand.
"""

from __future__ import annotations

from statistics import median
from typing import Any

_MM_PER_INCH = 25.4
_MAD_TO_SPREAD = 1.4826

# name: (words, unit, floor setting, steps it comes from)
MEASUREMENTS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "skew": ("skew", "degrees", "volume_skew_floor_deg", ("skew",)),
    "content_width": ("content width", "mm", "volume_size_floor_mm", ("content_box",)),
    "content_height": ("content height", "mm", "volume_size_floor_mm", ("content_box",)),
    "margin_left": ("left margin", "mm", "volume_margin_floor_mm", ("page_box", "content_box")),
    "margin_top": ("top margin", "mm", "volume_margin_floor_mm", ("page_box", "content_box")),
    "margin_right": ("right margin", "mm", "volume_margin_floor_mm", ("page_box", "content_box")),
    "margin_bottom": (
        "bottom margin",
        "mm",
        "volume_margin_floor_mm",
        ("page_box", "content_box"),
    ),
}


def measure_page(page: Any) -> dict[str, float]:
    """The batch measurements of one planned page (pagekit.prepare.PagePlan)."""
    steps = page.steps
    content = steps["content_box"]["value"]
    if content is None:
        return {}
    found = {"skew": float(steps["skew"]["value"])}
    if not page.applied.get("content_box", True):  # cropping off: no content box
        return found
    if page.upright_resolution is None:  # missing or implausible
        return found
    # The levelled page's axes: the tag (whoever applied it) and the turns are in it.
    dpi_x, dpi_y = page.upright_resolution
    mm_x, mm_y = _MM_PER_INCH / dpi_x, _MM_PER_INCH / dpi_y
    box = steps["page_box"]["value"]
    found["content_width"] = (content[2] - content[0]) * mm_x
    found["content_height"] = (content[3] - content[1]) * mm_y
    found["margin_left"] = (content[0] - box[0]) * mm_x
    found["margin_top"] = (content[1] - box[1]) * mm_y
    found["margin_right"] = (box[2] - content[2]) * mm_x
    found["margin_bottom"] = (box[3] - content[3]) * mm_y
    return found


def _amount(value: float, unit: str) -> str:
    return f"{value:+.2f} degrees" if unit == "degrees" else f"{value:.1f} mm"


def check_batch(pages: list[Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Flag pages far from the rest of the batch; a summary of each measurement.

    Each flag is added to the page's flags with step "batch".
    """
    measured = [measure_page(page) for page in pages]
    summary: dict[str, Any] = {}
    for name, (words, unit, floor_name, steps) in MEASUREMENTS.items():
        values = [(index, found[name]) for index, found in enumerate(measured) if name in found]
        entry: dict[str, Any] = {"pages": len(values), "compared": False}
        summary[name] = entry
        if len(values) < settings["volume_min_pages"]:
            continue
        centre = median(value for _, value in values)
        deviation = median(abs(value - centre) for _, value in values)
        spread = max(_MAD_TO_SPREAD * deviation, settings[floor_name])
        limit = settings["volume_outlier_distance"]
        flagged = 0
        for index, value in values:
            distance = abs(value - centre) / spread
            page = pages[index]
            by_hand = all(page.steps[step]["origin"] != "detected" for step in steps)
            if distance <= limit or by_hand:
                continue
            flagged += 1
            page.flags.append(
                {
                    "step": "batch",
                    "reason": (
                        f"This page's {words} ({_amount(value, unit)}) is far from the rest of "
                        f"the batch (median {_amount(centre, unit)}): {distance:.1f} times the "
                        f"usual spread, and pages beyond {limit:g} are flagged."
                    ),
                }
            )
        entry.update(
            {
                "compared": True,
                "median": round(centre, 3),
                "spread": round(spread, 3),
                "flagged": flagged,
            }
        )
    return {
        "min_pages": settings["volume_min_pages"],
        "distance": settings["volume_outlier_distance"],
        "measurements": summary,
    }
