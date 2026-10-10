"""The detectors, connected to the preparation core.

`DETECTORS` maps each detected step to a `pagekit.prepare.Detector` that adapts one
detector's plain answer (value, confidence, evidence, flags) to the core's interface
without changing what the detector decides:

- **orientation** runs `pagekit.orient.detect_orientation` on the source as stored;
- **split** runs `pagekit.split.detect_split` on the source with the orientation's
  turns. Its value carries more than the core keeps (the method that decided, which
  part found a fold, a neighbour strip); the core keeps the page count and the cut, and
  the method is added to the evidence. A cut that cannot make two pages is an error, so
  that page takes the neutral default with a flag;
- **skew** runs `pagekit.skew.detect_skew` on the page's side of the cut, before
  levelling, with the page's side of the cut as its own area;
- **page box** and **content box** run `pagekit.pagebox.detect_page_box` and
  `pagekit.content.detect_content_box` on the levelled page, with the same area.

Every detector reduces its input to its own working resolution. The skew page is an
exact crop of the original, reduced by a whole factor with area averaging. The levelled
page for the two box steps is a reduced copy made from the original in one bicubic
resampling, at no less than the `detector_working_dpi` setting (which is above both box
detectors' own working resolutions, so they see what they would on the full page); its
boxes are scaled back by that whole factor, which rounds them outward, never inward. A
full-size page is then measured in a few seconds with Pillow alone. A source with no
usable resolution is measured as if it had `unknown_dpi_assumed`, and the evidence says
so (the page carries the core's resolution flag either way).

Each detector names the pagekit files it reads, its settings files and its own code;
their sha256 enter every value's inputs hash, so changing a threshold or the detector's
code recomputes every value it decided, and nothing else. Each one can also compare a
value set by hand with what it finds; only a confident answer (no flags) that differs
by more than the step's `compare_*` setting is reported.

The grey tone view is reached only through `make_tone_view`, the hook
`prepare --tone-view` calls, which uses `pagekit/tone.py`'s `tone` and `tiff_bytes`.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import sys
from typing import Any

from PIL import Image, ImageDraw

from pagekit.answer import Answer
from pagekit.content import detect_content_box
from pagekit.geometry import Chain, GeometryError, apply, page_polygon, paper_colour, render
from pagekit.orient import detect_orientation
from pagekit.pagebox import detect_page_box
from pagekit.prepare import Detector, StepContext, _overlap_px
from pagekit.skew import detect_skew
from pagekit.split import detect_split

TONE_MODULE = "pagekit.tone"
_MM_PER_INCH = 25.4
# pagekit's files each detector reads: its settings files and the code that decides,
# down to the shared helpers and this adapter. Their digests enter the inputs hash.
_ORIENT_FILES = ("thresholds_split.toml", "_orient_ink.py", "check.py", "pipeline.py")
_BOX_FILES = ("thresholds_skew.toml", "_box_common.py", "check.py", "geometry.py", "pipeline.py")
_FILES = {
    "orientation": ("orient.py", *_ORIENT_FILES),
    "split": ("split.py", *_ORIENT_FILES),
    "skew": ("skew.py", *_BOX_FILES),
    "page_box": ("pagebox.py", *_BOX_FILES),
    # The content box also measures discarded ink with the crop check's thresholds.
    "content_box": ("content.py", "thresholds.toml", *_BOX_FILES),
}


def _answer(found: dict[str, Any], value: Any = None, extra: str = "") -> Answer:
    """A detector's plain answer as the core's Answer, with `value` in place if given."""
    evidence = found["evidence"] + extra
    return Answer(
        found["value"] if value is None else value,
        found["confidence"],
        evidence,
        tuple(found["flags"]),
    )


# --- Resolution --------------------------------------------------------------------


def _upright_dpi(context: StepContext) -> tuple[tuple[float, float], bool]:
    """The page's resolution (x, y) in the upright frame, and whether it is known."""
    if context.resolution is None:
        assumed = float(context.settings["unknown_dpi_assumed"])
        return (assumed, assumed), False
    x, y = context.resolution
    turns = context.values.get("orientation", 0)
    return ((y, x) if turns % 2 else (x, y)), True


def _assumed_note(known: bool, dpi: tuple[float, float]) -> str:
    if known:
        return ""
    return f" The source has no usable resolution, so it was measured as if it were {dpi[0]:g} dpi."


# --- Working copies ------------------------------------------------------------------


def _page_copy(context: StepContext) -> tuple[Image.Image, list[tuple[float, float]] | None, int]:
    """A reduced copy of this step's grid, the page's own area in it, and the whole
    factor it is reduced by. Made from the original source, kept for the other steps of
    the same page.

    The page's own area is its side of the cut (with the overlap),
    or None for a page with no cut. It is not the turned outline of the frame: the
    corners a rotation brings into the levelled grid lie outside the scan and are
    filled with the paper colour, and marking them as outside the page would make the
    page-box detector read them as shadows along every side."""
    chain = context.chain()
    key = ("page copy", context.page, chain.angle)
    if key in context.cache:
        return context.cache[key]
    (dpi_x, dpi_y), _ = _upright_dpi(context)
    factor = max(1, math.floor(min(dpi_x, dpi_y) / context.settings["detector_working_dpi"]))
    source = context.image()
    fill, _ = paper_colour(source, chain, context.settings["paper_estimate_long_side_px"])
    polygon = [(x / factor, y / factor) for x, y in apply(chain.upright_to_output(), chain.polygon)]
    area = None
    split = context.values["split"]
    if split["pages"] == 2:
        cut = apply(chain.upright_to_output(), split["cut"])
        overlap = _overlap_px(context.settings, context.resolution, context.values["orientation"])
        side = page_polygon(chain.output_size, {"pages": 2, "cut": cut}, context.page - 1, overlap)
        area = [(x / factor, y / factor) for x, y in side]
    if chain.angle == 0:
        page = render(source, chain, fill)  # an exact crop of the original
        if factor > 1:
            page = page.reduce(factor)
    else:
        width, height = chain.output_size
        size = (math.ceil(width / factor), math.ceil(height / factor))
        small = source.reduce(factor) if factor > 1 else source
        # A copy point q is the grid point q * factor, which maps to a source point, and
        # that source point is at 1/factor of it in the reduced source.
        a, b, c, d, e, f = chain.output_to_source()
        page = small.transform(
            size,
            Image.Transform.AFFINE,
            (a, b, c / factor, d, e, f / factor),
            Image.Resampling.BICUBIC,
            fillcolor=fill,
        )
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon(polygon, fill=255)
        page = Image.composite(page, Image.new(page.mode, size, fill), mask)
    result = (page, area, factor)
    context.cache[key] = result
    return result


def _grid_size(context: StepContext) -> tuple[int, int]:
    return context.chain().output_size


def _scale_up(box: list[int], factor: int, limit: tuple[int, int]) -> list[int]:
    """A box in a copy reduced by `factor`, in the full grid: outward, inside `limit`."""
    left, top, right, bottom = box
    return [
        min(limit[0] - 1, left * factor),
        min(limit[1] - 1, top * factor),
        min(limit[0], right * factor),
        min(limit[1], bottom * factor),
    ]


# --- The five detectors ----------------------------------------------------------------


def _orientation(context: StepContext) -> Answer:
    return _answer(detect_orientation(context.frame()))


def _split(context: StepContext) -> Answer:
    image = context.frame()
    turns = context.values["orientation"]
    if context.resolution is None:
        # The core found the file's resolution missing or implausible: the detector must
        # not read it from the image either.
        image = image.copy()
        image.info.pop("dpi", None)
    found = detect_split(
        image, turns, dpi=context.resolution, overlap_mm=context.settings["overlap_mm"]
    )
    detail = found["value"]
    if detail["pages"] == 2:
        value = {"pages": 2, "cut": [list(point) for point in detail["cut"]]}
        for page in (0, 1):  # a cut that cannot make two pages is an error, not a value
            Chain.build(context.source.size, turns, value, page, 0.0, 0.0, tag=context.tag)
    else:
        value = {"pages": 1}
    extra = ""
    if detail["method"] != "none":
        part = f" ({detail['part']})" if detail.get("part") else ""
        extra = f" Decided by: {detail['method']}{part}."
    return _answer(found, value, extra)


def _skew(context: StepContext) -> Answer:
    page, polygon, factor = _page_copy(context)
    dpi, known = _upright_dpi(context)
    found = detect_skew(page, (dpi[0] / factor, dpi[1] / factor), polygon)
    return _answer(found, extra=_assumed_note(known, dpi))


def _page_box(context: StepContext) -> Answer:
    page, polygon, factor = _page_copy(context)
    dpi, known = _upright_dpi(context)
    found = detect_page_box(page, (dpi[0] / factor, dpi[1] / factor), polygon)
    value = _scale_up(found["value"], factor, _grid_size(context))
    return _answer(found, value, _assumed_note(known, dpi))


def _content_box(context: StepContext) -> Answer:
    page, polygon, factor = _page_copy(context)
    dpi, known = _upright_dpi(context)
    left, top, right, bottom = context.values["page_box"]
    width, height = page.size
    reduced = [
        min(width - 1, max(0, left // factor)),
        min(height - 1, max(0, top // factor)),
        min(width, max(1, math.ceil(right / factor))),
        min(height, max(1, math.ceil(bottom / factor))),
    ]
    found = detect_content_box(page, (dpi[0] / factor, dpi[1] / factor), reduced, polygon)
    note = _assumed_note(known, dpi)
    if found["value"] is None:
        return _answer(found, extra=note) if note else _answer(found)
    box = _scale_up(found["value"], factor, _grid_size(context))
    box = [max(box[0], left), max(box[1], top), min(box[2], right), min(box[3], bottom)]
    if box[2] <= box[0] or box[3] <= box[1]:
        raise GeometryError(f"the content box {found['value']} lies outside the page box")
    return _answer(found, box, note)


# --- Comparing with values set by hand ---------------------------------------------------

_TURN_WORDS = {
    0: "no quarter turn",
    1: "one quarter turn clockwise",
    2: "a half turn",
    3: "three quarter turns clockwise",
}
_RUN_TO_COMPARE = "The detector, run only to compare, finds"


def _compare_orientation(manual: int, found: Answer, context: StepContext) -> str | None:
    if found.flags or found.value == manual:
        return None
    return (
        f"{_RUN_TO_COMPARE} {_TURN_WORDS[found.value]} (confidence {found.confidence:.2f}), "
        f"not the {_TURN_WORDS[manual]} set by hand."
    )


def _x_at(cut: list[list[float]], y: float) -> float:
    (x0, y0), (x1, y1) = cut
    return x0 + (x1 - x0) * (y - y0) / (y1 - y0)


def _compare_split(manual: dict, found: Answer, context: StepContext) -> str | None:
    if found.flags:
        return None
    if found.value["pages"] != manual["pages"]:
        return (
            f"{_RUN_TO_COMPARE} {found.value['pages']} page(s) (confidence "
            f"{found.confidence:.2f}), not the {manual['pages']} set by hand."
        )
    if manual["pages"] == 1:
        return None
    turns = context.values["orientation"]
    height = context.frame_size[0 if turns % 2 else 1]
    (dpi_x, _), _ = _upright_dpi(context)
    apart = max(
        abs(_x_at(found.value["cut"], y) - _x_at(manual["cut"], y)) for y in (0.0, float(height))
    )
    apart_mm = apart / dpi_x * _MM_PER_INCH
    if apart_mm <= context.settings["compare_cut_mm"]:
        return None
    return (
        f"{_RUN_TO_COMPARE} a cut up to {apart_mm:.1f} mm away from the one set by hand "
        f"(confidence {found.confidence:.2f})."
    )


def _compare_skew(manual: float, found: Answer, context: StepContext) -> str | None:
    if found.flags or abs(found.value - manual) <= context.settings["compare_skew_deg"]:
        return None
    return (
        f"{_RUN_TO_COMPARE} {found.value:+.2f} degrees (confidence {found.confidence:.2f}), "
        f"{abs(found.value - manual):.2f} degrees from the {manual:+.2f} set by hand."
    )


def _compare_box(name: str):
    def compare(manual: list[int] | None, found: Answer, context: StepContext) -> str | None:
        if found.flags:
            return None
        if (manual is None) != (found.value is None):
            what = "a blank page" if found.value is None else f"content at {found.value}"
            set_by_hand = "a blank page" if manual is None else f"{manual}"
            return (
                f"{_RUN_TO_COMPARE} {what} (confidence {found.confidence:.2f}), not "
                f"{set_by_hand} as set by hand."
            )
        if manual is None:
            return None
        (dpi_x, dpi_y), _ = _upright_dpi(context)
        per_mm = (dpi_x / _MM_PER_INCH, dpi_y / _MM_PER_INCH)
        apart = max(
            abs(found.value[index] - manual[index]) / per_mm[index % 2] for index in range(4)
        )
        if apart <= context.settings["compare_box_mm"]:
            return None
        return (
            f"{_RUN_TO_COMPARE} a {name} of {found.value}, a side up to {apart:.1f} mm from "
            f"the {manual} set by hand (confidence {found.confidence:.2f})."
        )

    return compare


# The core settings each detector reads, besides its own thresholds file (named in its
# method) and the overlap and resolution the core adds: they enter every value's inputs
# hash. The compare_* settings only change what is reported, so they are not inputs.
_PAGE_READS = ("detector_working_dpi", "unknown_dpi_assumed", "paper_estimate_long_side_px")

DETECTORS: dict[str, Detector] = {
    "orientation": Detector(
        "pagekit.orientation/1",
        _orientation,
        (),
        _compare_orientation,
        _FILES["orientation"],
    ),
    "split": Detector(
        "pagekit.split/1",
        _split,
        (),
        _compare_split,
        _FILES["split"],
    ),
    "skew": Detector(
        "pagekit.skew/1",
        _skew,
        _PAGE_READS,
        _compare_skew,
        _FILES["skew"],
    ),
    "page_box": Detector(
        "pagekit.page-box/1",
        _page_box,
        _PAGE_READS,
        _compare_box("page box"),
        _FILES["page_box"],
    ),
    "content_box": Detector(
        "pagekit.content-box/1",
        _content_box,
        _PAGE_READS,
        _compare_box("content box"),
        _FILES["content_box"],
    ),
}


# --- The grey tone view --------------------------------------------------


def tone_view_available() -> bool:
    """Whether `pagekit/tone.py` is in this copy of pagekit."""
    if TONE_MODULE in sys.modules:
        return True
    return importlib.util.find_spec(TONE_MODULE) is not None


def make_tone_view(
    page: Image.Image, dpi: tuple[float, float] | None
) -> tuple[Image.Image, dict[str, Any], bytes]:
    """THE TONE-VIEW HOOK: the grey tone view of a prepared page, its record, and its
    file bytes.

    It calls `tone(image)` in `pagekit/tone.py`, with the page's resolution
    set on the image so the view's millimetre settings hold, and makes the file with
    tone.py's own deterministic writer, `tiff_bytes`.
    """
    module = importlib.import_module(TONE_MODULE)
    image = page.copy()
    image.info.pop("dpi", None)
    if dpi is not None:
        image.info["dpi"] = tuple(dpi)
    view, record = module.tone(image)
    if view.size != page.size or view.mode != "L":
        raise ValueError("the tone view must be a grey image the size of the page")
    return view, record, module.tiff_bytes(view, None if dpi is None else list(dpi))
