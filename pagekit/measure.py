"""`measure`: compare a prepared batch with a hand-checked answer file.

The answer file, schema `pagekit-gold.v1`, gives for each source it checks, all but the
source optional:

    {"schema": "pagekit-gold.v1", "sources": [
      {"source": "0012.tif", "orientation": 1, "pages": 2,
       "cut": [[1510, 0], [1532, 4480]],
       "skew": [0.4, -0.2],
       "content_box": [[120, 200, 1450, 4300], null]}
    ]}

`source` is the source's file name or its sha256. `orientation` is the quarter turns
clockwise that make it upright; `pages` 1 or 2; `cut` two points of the true cut in the
upright image's pixels; `skew` each page's angle in degrees counterclockwise; and
`content_box` each page's box of everything to keep, `[left, top, right, bottom]`, or
null for a blank page. The box is drawn on the upright image after turning it by the
page's true skew about the image's centre, keeping its size, as an image editor levels
a picture; on a page with no skew that is simply the upright image.

For each step pagekit's value is **right** when it matches within the step's
`measure_*` tolerance and the step carries no flag, **wrong** when it does not and the
step carries no flag (the case that matters most: a silent error), and **sent to review**
when the step carries a flag, whatever its value. A value set by hand is counted apart.
The size of each error is reported as well, in total and for each page (`errors`). A
detected content box, which lies in pagekit's levelled grid, is mapped back to the
upright image and turned by the true skew (the gold file's, or pagekit's own where the
gold file gives none), so both boxes are compared in one grid. The cut is compared
across the page, in the upright image's horizontal resolution. Nothing here changes any
setting: it is how the settings will be measured.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median
from typing import Any

from pagekit.geometry import Chain, apply
from pagekit.output import MANIFEST_NAME
from pagekit.project import SHA256, PrepareError, load_settings

GOLD_SCHEMA = "pagekit-gold.v1"
REPORT_SCHEMA = "pagekit-measure.v1"
STEPS = ("orientation", "pages", "cut", "skew", "content_box")
_STEP_WORDS = {
    "orientation": "orientation",
    "pages": "page count",
    "cut": "cut",
    "skew": "skew",
    "content_box": "content box",
}
_UNITS = {
    "orientation": "quarter turns",
    "pages": "pages",
    "cut": "mm",
    "skew": "degrees",
    "content_box": "mm",
}
_GOLD_KEYS = {"source", "orientation", "pages", "cut", "skew", "content_box"}
_MM_PER_INCH = 25.4


def _load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PrepareError(f"the {what} {path} does not exist") from error
    except (OSError, ValueError) as error:
        raise PrepareError(f"the {what} {path} cannot be read: {error}") from error


def _box(value: Any, where: str) -> list[float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
        or value[2] <= value[0]
        or value[3] <= value[1]
    ):
        raise PrepareError(f"{where}: a content box is [left, top, right, bottom] or null")
    return [float(v) for v in value]


def load_gold(path: Path) -> list[dict[str, Any]]:
    """The checked sources of a `pagekit-gold.v1` file, refused with a plain message if
    its shape is wrong."""
    data = _load_json(path, "answer file")
    if not isinstance(data, dict) or set(data) != {"schema", "sources"}:
        raise PrepareError(f"{path.name} must hold exactly 'schema' and 'sources'")
    if data["schema"] != GOLD_SCHEMA:
        raise PrepareError(f"{path.name}'s schema must be {GOLD_SCHEMA!r}")
    if not isinstance(data["sources"], list) or not data["sources"]:
        raise PrepareError(f"{path.name} names no sources")
    checked = []
    for index, entry in enumerate(data["sources"], start=1):
        where = f"source {index} in {path.name}"
        if not isinstance(entry, dict) or "source" not in entry:
            raise PrepareError(f"{where} must name its source")
        unknown = sorted(set(entry) - _GOLD_KEYS)
        if unknown:
            raise PrepareError(f"{where} has unknown keys {unknown}")
        if entry.get("orientation") not in (None, 0, 1, 2, 3):
            raise PrepareError(f"{where}: orientation is 0, 1, 2 or 3 quarter turns")
        if entry.get("pages") not in (None, 1, 2):
            raise PrepareError(f"{where}: pages is 1 or 2")
        cut = entry.get("cut")
        if cut is not None:
            if (
                not isinstance(cut, list)
                or len(cut) != 2
                or not all(isinstance(p, list) and len(p) == 2 for p in cut)
                or cut[0][1] == cut[1][1]
            ):
                raise PrepareError(
                    f"{where}: a cut is two points [[x, y], [x, y]], one above the other"
                )
        for key in ("skew", "content_box"):
            if key in entry and not isinstance(entry[key], list):
                raise PrepareError(f"{where}: {key} is a list with one entry per page")
        boxes = [_box(box, where) for box in entry.get("content_box", [])]
        checked.append({**entry, "content_box": boxes if "content_box" in entry else None})
    return checked


def _x_at(cut: list[list[float]], y: float) -> float:
    (x0, y0), (x1, y1) = cut
    return x0 + (x1 - x0) * (y - y0) / (y1 - y0)


def _levelled_by_truth(page: dict[str, Any], box: list[int], true_skew: float) -> list[float]:
    """A detected content box in the grid the hand-checked box is drawn in.

    The detected box lies in the page's levelled grid. Its corners are mapped back to
    the upright image, then turned by the true skew about the upright image's centre,
    as a person levelling the image in an editor (keeping its size) would; there the
    box of the corners is compared with the hand-drawn one. When the detected skew is
    the true one, the corners come out square and the box is exact.
    """
    chain = Chain.from_dict(page["geometry"])
    left, top, right, bottom = box
    corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
    output = apply(chain.levelled_to_output(), corners)
    upright = apply(chain.source_to_upright(), chain.inverse(output))
    width, height = chain.upright_size
    centre_x, centre_y = width / 2, height / 2
    radians = math.radians(true_skew)
    cos, sin = math.cos(radians), math.sin(radians)
    turned = []
    for x, y in upright:  # counterclockwise by the skew, y down, as pagekit levels
        dx, dy = x - centre_x, y - centre_y
        turned.append((centre_x + dx * cos + dy * sin, centre_y - dx * sin + dy * cos))
    xs = [x for x, _ in turned]
    ys = [y for _, y in turned]
    return [min(xs), min(ys), max(xs), max(ys)]


class _Tally:
    def __init__(self) -> None:
        self.counts = {
            step: {"right": 0, "wrong": 0, "review": 0, "by_hand": 0, "not_applied": 0}
            for step in STEPS
        }
        self.errors: dict[str, list[float]] = {step: [] for step in STEPS}
        self.by_item: dict[str, dict[str, float]] = {step: {} for step in STEPS}
        self.wrong: list[str] = []

    def add(self, step, where, step_entry, error, tolerance) -> None:
        if step_entry["origin"] != "detected":
            self.counts[step]["by_hand"] += 1
            return
        if error is not None:
            self.errors[step].append(error)
            self.by_item[step][where] = round(error, 3)
        if step_entry["flags"]:
            self.counts[step]["review"] += 1
        elif error is not None and error <= tolerance:
            self.counts[step]["right"] += 1
        else:
            self.counts[step]["wrong"] += 1
            self.wrong.append(f"{where}: {_STEP_WORDS[step]}")


def measure(prepared: Path, gold_path: Path) -> dict[str, Any]:
    """Compare the prepared batch in `prepared` with the answer file at `gold_path`."""
    manifest = _load_json(prepared / MANIFEST_NAME, "manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != "pagekit-prepare.v1":
        raise PrepareError(f"{prepared / MANIFEST_NAME} is not a pagekit-prepare.v1 manifest")
    gold = load_gold(gold_path)
    settings = {name: entry["value"] for name, entry in load_settings().items()}
    tolerance = {
        "orientation": 0,
        "pages": 0,
        "cut": settings["measure_cut_tolerance_mm"],
        "skew": settings["measure_skew_tolerance_deg"],
        "content_box": settings["measure_box_tolerance_mm"],
    }
    by_source: dict[str, list[dict[str, Any]]] = {}
    for page in manifest["pages"]:
        by_source.setdefault(page["source"]["sha256"], []).append(page)
    names: dict[str, list[str]] = {}
    for sha, pages in by_source.items():
        names.setdefault(pages[0]["source"]["name"], []).append(sha)

    tally = _Tally()
    notes: list[str] = []
    for entry in gold:
        wanted = entry["source"]
        if SHA256.fullmatch(wanted):
            matches = [wanted] if wanted in by_source else []
        else:
            matches = names.get(wanted, [])
        if len(matches) != 1:
            problem = "is not in" if not matches else "names more than one source in"
            raise PrepareError(f"the answer file's source {wanted!r} {problem} the prepared batch")
        pages = sorted(by_source[matches[0]], key=lambda page: page["page"])
        first = pages[0]
        turns = first["steps"]["orientation"]["value"]
        # The upright frame's resolution, as prepare recorded it: the tag (whoever
        # applied it) and the turns are already in its axes.
        upright = first.get("upright_resolution")
        if upright is None:
            dpi = (300.0, 300.0)
            notes.append(f"{wanted}: no resolution, so millimetres assume 300 dpi")
        else:
            dpi = tuple(upright)
        steps = first["steps"]
        if entry.get("orientation") is not None:
            error = abs(turns - entry["orientation"])
            tally.add("orientation", wanted, steps["orientation"], min(error, 4 - error), 0)
        split = steps["split"]["value"]
        if entry.get("pages") is not None:
            tally.add("pages", wanted, steps["split"], abs(split["pages"] - entry["pages"]), 0)
        if entry.get("cut") is not None and entry.get("pages", 2) == 2:
            if split["pages"] == 2 and turns == entry.get("orientation", turns):
                height = max(point[1] for point in split["cut"] + entry["cut"])
                apart = max(
                    abs(_x_at(split["cut"], y) - _x_at(entry["cut"], y)) for y in (0.0, height)
                )
                error = apart / dpi[0] * _MM_PER_INCH
            else:
                error = None  # no cut, or a cut in a differently turned frame
            tally.add("cut", wanted, steps["split"], error, tolerance["cut"])
        same_count = len(pages) == len(entry.get("skew") or pages) and len(pages) == len(
            entry.get("content_box") or pages
        )
        if not same_count:
            notes.append(f"{wanted}: the page count differs, so its pages were not compared")
            continue
        for index, page in enumerate(pages):
            where = f"{wanted} page {page['page']}"
            if entry.get("skew") is not None and entry["skew"][index] is not None:
                error = abs(page["steps"]["skew"]["value"] - float(entry["skew"][index]))
                tally.add("skew", where, page["steps"]["skew"], error, tolerance["skew"])
            applied = page.get("applied", {}).get("content_box", True)
            if entry.get("content_box") is not None and not applied:
                # Cropping was off: the content box is a stand-in for the whole side,
                # not a detection, so there is nothing to score.
                tally.counts["content_box"]["not_applied"] += 1
                notes.append(
                    f"{where}: cropping was off, so the content box was not applied and "
                    "is not scored"
                )
            elif entry.get("content_box") is not None:
                true_box = entry["content_box"][index]
                found = page["steps"]["content_box"]["value"]
                if true_box is None or found is None:
                    error = 0.0 if true_box is None and found is None else None
                else:
                    true_skew = page["steps"]["skew"]["value"]
                    if entry.get("skew") is not None and entry["skew"][index] is not None:
                        true_skew = float(entry["skew"][index])
                    box = _levelled_by_truth(page, found, true_skew)
                    per_mm = (dpi[0] / _MM_PER_INCH, dpi[1] / _MM_PER_INCH)
                    error = max(abs(box[i] - true_box[i]) / per_mm[i % 2] for i in range(4))
                tally.add(
                    "content_box",
                    where,
                    page["steps"]["content_box"],
                    error,
                    tolerance["content_box"],
                )
    steps = {}
    for step in STEPS:
        errors = tally.errors[step]
        steps[step] = {
            **tally.counts[step],
            "tolerance": tolerance[step],
            "unit": _UNITS[step],
            "error_median": round(median(errors), 3) if errors else None,
            "error_largest": round(max(errors), 3) if errors else None,
        }
    return {
        "schema": REPORT_SCHEMA,
        "gold": gold_path.name,
        "sources": len(gold),
        "steps": steps,
        "wrong_without_flag": tally.wrong,
        "errors": tally.by_item,
        "notes": notes,
        "thresholds_note": (
            "measure changes no setting; it reports how the current settings do on "
            "hand-checked pages."
        ),
    }


def report_text(report: dict[str, Any]) -> str:
    """The report in plain words, one step per line."""
    lines = [f"Compared {report['sources']} source image(s) with {report['gold']}."]
    for step, entry in report["steps"].items():
        counted = entry["right"] + entry["wrong"] + entry["review"] + entry["by_hand"]
        if entry["not_applied"]:
            lines.append(
                f"{_STEP_WORDS[step]}: {entry['not_applied']} not scored (cropping was off)"
            )
        if not counted:
            continue
        text = (
            f"{_STEP_WORDS[step]}: {entry['right']} right, {entry['wrong']} wrong, "
            f"{entry['review']} sent to review"
        )
        if entry["by_hand"]:
            text += f", {entry['by_hand']} set by hand"
        if entry["error_largest"] is not None and step not in ("orientation", "pages"):
            text += (
                f"; error median {entry['error_median']:g} {entry['unit']}, largest "
                f"{entry['error_largest']:g} {entry['unit']}"
            )
        lines.append(text)
    if report["wrong_without_flag"]:
        lines.append("Wrong with no flag (the errors that matter most):")
        lines += [f"  {item}" for item in report["wrong_without_flag"]]
    lines += [f"note: {note}" for note in report["notes"]]
    lines.append(f"note: {report['thresholds_note']}")
    return "\n".join(lines) + "\n"
