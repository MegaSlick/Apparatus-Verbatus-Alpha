"""`prepare`: run the steps for each source image, keep corrections, plan the outputs.

The steps run in order: orientation and split once per source, then skew, page box,
content box and margin once per page. Each step's value comes from, in this order of
precedence: an override in the overrides file being applied; a value already set by
hand (manual or locked) in the project; a detected value in the project whose inputs
and method are unchanged; or the step's detector, run now. A step with no detector
connected takes a neutral default, recorded as detected with confidence 0 and a flag.
The margin is a setting, not a detection: unless set by hand it is `margin_mm`, recorded
as detected with confidence 1 and no flag.

Detectors are built in other slices. A detector is a `Detector`: a method name with a
version, the settings it reads (part of every value's inputs hash) and a function that
takes a `StepContext` and returns an `Answer`. Pass them to `plan` by step name.

Nothing is written until the whole batch has been read and every value settled, so an
input that cannot be used stops the run with nothing written (PrepareError).
"""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from pagekit import __version__
from pagekit.answer import PAGE_STEPS, SOURCE_STEPS, STEPS, Answer, AnswerError, validate_answer
from pagekit.geometry import Chain, GeometryError, margin_box, paper_colour, render
from pagekit.project import (
    PROJECT_SCHEMA,
    SHA256,
    Override,
    PrepareError,
    input_changes,
    load_overrides,
    load_project,
    load_settings,
    record,
    step_inputs,
)

PROJECT_NAME = "pagekit-project.json"
IMAGE_SUFFIXES = (".png", ".tif", ".tiff", ".jpg", ".jpeg")
# Modes kept as they are, and modes turned into one of them exactly (no resampling).
_KEPT_MODES = {"L": "L", "RGB": "RGB", "1": "L", "P": "RGB"}
_MM_PER_INCH = 25.4


@dataclass(frozen=True)
class Source:
    """A source image as read: never changed, only read."""

    path: Path  # resolved
    relative: str  # relative to the project file's folder, with forward slashes
    sha256: str
    bytes: int
    size: tuple[int, int]
    mode: str  # the stored mode
    file_dpi: tuple[float, float] | None

    def open(self) -> Image.Image:
        """The decoded original in L or RGB, checked against the sha256 read first."""
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise PrepareError(f"{self.path} changed while pagekit was running")
        return _decode(data, self.path)


def _decode(data: bytes, path: Path) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise PrepareError(f"{path} has more than one frame; give one page per file")
            if image.mode not in _KEPT_MODES:
                raise PrepareError(f"{path}: image mode {image.mode!r} is not supported")
            image.load()
            return (
                image.convert(_KEPT_MODES[image.mode]) if image.mode in ("1", "P") else image.copy()
            )
    except PrepareError:
        raise
    except Exception as error:  # any decoder failure means the source cannot be used
        raise PrepareError(f"{path} cannot be read as an image: {error}") from error


def _file_dpi(image: Image.Image) -> tuple[float, float] | None:
    dpi = image.info.get("dpi")
    if not dpi or len(dpi) != 2:
        return None
    try:
        values = (round(float(dpi[0]), 2), round(float(dpi[1]), 2))
    except (TypeError, ValueError):
        return None
    return values if all(value > 0 for value in values) else None


def read_source(path: Path, project_folder: Path) -> Source:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PrepareError(f"{path} cannot be read: {error}") from error
    image = _decode(data, path)
    with Image.open(io.BytesIO(data)) as stored:
        mode, dpi = stored.mode, _file_dpi(stored)
    return Source(
        path=path,
        relative=Path(os.path.relpath(path, project_folder)).as_posix(),
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        size=image.size,
        mode=mode,
        file_dpi=dpi,
    )


@dataclass
class StepContext:
    """What a detector is given for one step of one source or page."""

    step: str
    page: int | None  # counted from 1; None for orientation and split
    source: Source
    resolution: tuple[float, float] | None  # dots per inch, x and y, if usable
    values: dict[str, Any]  # the earlier steps' values for this page
    settings: dict[str, Any]  # every setting's value
    _image: Callable[[], Image.Image] = field(repr=False, default=None)

    def image(self) -> Image.Image:
        """The decoded original source. Never change it and never write it out."""
        return self._image()

    def chain(self, scale: float = 1.0) -> Chain:
        """The chain to the grid this step works in, whole, scaled by `scale` (<= 1).

        Orientation works on the source as stored, split on the upright frame, skew on
        the page before levelling, and the box steps on the levelled page.
        """
        size = self.source.size
        if self.step == "orientation":
            return Chain.build(size, 0, {"pages": 1}, 0, 0.0, 0.0, None, scale)
        turns = self.values["orientation"]
        if self.step == "split":
            return Chain.build(size, turns, {"pages": 1}, 0, 0.0, 0.0, None, scale)
        angle = 0.0 if self.step == "skew" else self.values["skew"]
        overlap = _overlap_px(self.settings, self.resolution, turns)
        return Chain.build(
            size, turns, self.values["split"], self.page - 1, overlap, angle, None, scale
        )

    def working_copy(self, long_side: int) -> tuple[Image.Image, Chain]:
        """A reduced copy of this step's grid, made from the original, and its chain.

        A point in the copy divided by the chain's scale is a point in the step's grid.
        """
        whole = self.chain()
        width, height = whole.output_size
        chain = self.chain(min(1.0, long_side / max(width, height)))
        image = self.image()
        fill, _ = paper_colour(image, chain, self.settings["paper_estimate_long_side_px"])
        return render(image, chain, fill), chain


@dataclass(frozen=True)
class Detector:
    """A detector for one step: its method name with version, the settings it reads,
    and the function that answers."""

    method: str
    run: Callable[[StepContext], Answer | dict[str, Any]]
    settings: tuple[str, ...] = ()


def _neutral(step: str, description: str, value: Callable[[StepContext], Any], reads=()):
    flag = (
        f"The {step} step was not run: no detector for it is connected yet, so the neutral "
        f"default ({description}) was used."
    )

    def run(context: StepContext) -> Answer:
        return Answer(value(context), 0.0, f"Neutral default: {description}.", (flag,))

    return Detector(f"pagekit.neutral-default.{step}/1", run, tuple(reads))


def _whole_grid(context: StepContext) -> list[int]:
    width, height = context.chain().output_size
    return [0, 0, width, height]


NEUTRAL_DETECTORS: dict[str, Detector] = {
    "orientation": _neutral("orientation", "no quarter turn", lambda context: 0),
    "split": _neutral("split", "one page", lambda context: {"pages": 1}),
    "skew": _neutral("skew", "no skew", lambda context: 0.0),
    "page_box": _neutral("page_box", "the whole levelled page", _whole_grid),
    "content_box": _neutral(
        "content_box", "the whole page box", lambda context: context.values["page_box"]
    ),
    "margin": Detector(
        "pagekit.setting.margin_mm/1",
        lambda context: Answer(
            context.settings["margin_mm"],
            1.0,
            f"From the margin_mm setting ({context.settings['margin_mm']:g} mm).",
            (),
        ),
        ("margin_mm",),
    ),
}


def _overlap_px(settings: dict[str, Any], resolution, turns: int) -> float:
    if resolution is None:
        return 0.0
    dpi_x = resolution[1] if turns % 2 else resolution[0]
    return settings["overlap_mm"] * dpi_x / _MM_PER_INCH


def _upright_dpi(resolution, turns: int) -> tuple[float, float] | None:
    if resolution is None:
        return None
    return (resolution[1], resolution[0]) if turns % 2 else tuple(resolution)


@dataclass
class PagePlan:
    """One prepared page: where it comes from, its values and how to make it."""

    source: Source
    number: int
    output_name: str
    chain: Chain
    steps: dict[str, dict[str, Any]]
    flags: list[dict[str, str]]
    output_dpi: tuple[float, float] | None
    resolution: dict[str, Any]


@dataclass
class Plan:
    """Everything `prepare` will write, settled before anything is written."""

    output_dir: Path
    project_path: Path
    project: dict[str, Any]
    pages: list[PagePlan]
    settings: dict[str, dict[str, Any]]
    stale: list[dict[str, Any]]
    stale_outputs: list[str]  # outputs of pages that no longer exist, left in place


def _gather_inputs(inputs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        if item.is_dir():
            found = sorted(
                path
                for path in item.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
            if not found:
                raise PrepareError(f"{item} holds no source images ({', '.join(IMAGE_SUFFIXES)})")
            paths += found
        elif item.is_file():
            paths.append(item)
        else:
            raise PrepareError(f"{item} is neither a file nor a folder")
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise PrepareError("a source image is named more than once")
    if not resolved:
        raise PrepareError("no source images were given")
    return resolved


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _output_names(sources: list[Source], extension: str) -> dict[str, str]:
    """The base of each source's output names: its stem, or stem and digest if shared."""
    stems: dict[str, int] = {}
    for source in sources:
        stems[source.path.stem] = stems.get(source.path.stem, 0) + 1
    bases = {}
    for source in sources:
        stem = source.path.stem
        bases[source.relative] = stem if stems[stem] == 1 else f"{stem}_{source.sha256[:12]}"
    if len(set(bases.values())) != len(bases):
        raise PrepareError("two sources would give the same output names (identical files?)")
    return bases


class _Runner:
    """Settles every step value of one source, or (dry) says which are stale."""

    def __init__(self, settings, detectors, overrides, dry, image_loader):
        self.settings = settings
        self.values = {name: entry["value"] for name, entry in settings.items()}
        self.detectors = detectors
        self.overrides = overrides  # {(step, page): Override}
        self.dry = dry
        self.image_loader = image_loader
        self.stale: list[dict[str, Any]] = []

    def _reads(self, step: str, resolution) -> dict[str, Any]:
        reads = {name: self.values[name] for name in self.detectors[step].settings}
        if step in PAGE_STEPS:
            reads["overlap_mm"] = self.values["overlap_mm"]
            reads["resolution"] = None if resolution is None else list(resolution)
        return reads

    def _note(self, source: Source, step: str, page: int | None, reason: str) -> None:
        self.stale.append({"source": source.relative, "page": page, "step": step, "why": reason})

    def settle(self, source, step, page, earlier, old, resolution, uncertain):
        """The record for one step. `uncertain` names earlier steps that a dry run
        cannot settle, so this one may change too."""
        inputs, inputs_hash = step_inputs(
            source.sha256, page, earlier, self._reads(step, resolution)
        )
        override = self.overrides.get((step, page))
        if (
            override is not None
            and old is not None
            and old["origin"] in ("manual", "locked")
            and old["value"] == override.value
            and (old["origin"] == "locked") == override.lock
        ):
            # The same correction again: keep the inputs it was first set on, so it is
            # still flagged if what it was set on has moved. Changing the value or the
            # lock is what sets it afresh.
            override = None
        if override is not None:
            origin = "locked" if override.lock else "manual"
            new = record(
                override.value, origin, None, override.evidence, [], None, inputs, inputs_hash
            )
            if self.dry and (old is None or _without_flags(old) != _without_flags(new)):
                self._note(source, step, page, f"{override.where} sets it ({origin})")
            return new, False
        if old is not None and old["origin"] in ("manual", "locked"):
            changes = input_changes(old["inputs"], inputs)
            kept = dict(old)
            if old["origin"] == "locked" or not changes:
                kept["flags"] = []
            else:
                kept["flags"] = [
                    f"This value was set by hand, but what it was set on has changed "
                    f"({'; '.join(changes)}); check that it still holds."
                ]
            if self.dry and changes:
                action = "kept, not flagged" if old["origin"] == "locked" else "kept and flagged"
                self._note(
                    source,
                    step,
                    page,
                    f"set by hand ({old['origin']}); {'; '.join(changes)}; {action}",
                )
            elif self.dry and uncertain:
                self._note(
                    source,
                    step,
                    page,
                    f"set by hand ({old['origin']}); may be flagged once "
                    f"{', '.join(sorted(uncertain))} is recomputed",
                )
            return kept, False
        detector = self.detectors[step]
        if (
            old is not None
            and old["inputs_hash"] == inputs_hash
            and old["method"] == detector.method
        ):
            if self.dry and uncertain:
                self._note(
                    source,
                    step,
                    page,
                    f"may change: it depends on {', '.join(sorted(uncertain))}, "
                    "which will be recomputed",
                )
                return old, True
            return old, False
        if self.dry:
            if old is None:
                reason = "not computed yet"
            else:
                changes = input_changes(old["inputs"], inputs)
                if old["method"] != detector.method:
                    changes.append(f"the method changed to {detector.method}")
                reason = "will be recomputed: " + "; ".join(changes)
            self._note(source, step, page, reason)
            return old, True
        context = StepContext(
            step, page, source, resolution, dict(earlier), self.values, self.image_loader
        )
        try:
            answer = validate_answer(step, detector.run(context))
        except AnswerError as error:
            raise PrepareError(
                f"{source.relative}: the {step} detector {detector.method} gave an answer "
                f"pagekit refuses: {error}"
            ) from error
        new = record(
            answer.value,
            "detected",
            answer.confidence,
            answer.evidence,
            list(answer.flags),
            detector.method,
            inputs,
            inputs_hash,
        )
        return new, False


def _without_flags(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "flags"}


def _resolution(source: Source, old: dict | None, override: Override | None, values):
    """The stored resolution, the resolution the millimetre settings use, and flags."""
    if override is not None:
        stored = {"value": override.value, "origin": "override"}
    elif old is not None and old["resolution"]["origin"] == "override":
        stored = {"value": old["resolution"]["value"], "origin": "override"}
    elif source.file_dpi is not None:
        stored = {"value": list(source.file_dpi), "origin": "file"}
    else:
        stored = {"value": None, "origin": "missing"}
    stored["file_value"] = None if source.file_dpi is None else list(source.file_dpi)
    low, high = values["min_plausible_dpi"], values["max_plausible_dpi"]
    flags = []
    usable = None
    if stored["origin"] == "missing":
        flags.append(
            "The source carries no resolution and the project has no override for it, so "
            "the millimetre settings (overlap, margin, allowance) were applied as 0 px."
        )
    elif stored["origin"] == "file" and not all(low <= v <= high for v in stored["value"]):
        flags.append(
            f"The source's resolution {stored['value'][0]:g} x {stored['value'][1]:g} dpi is "
            f"outside the plausible range {low:g} to {high:g} dpi and the project has no "
            "override for it, so the millimetre settings were applied as 0 px."
        )
    else:
        usable = tuple(stored["value"])
    if usable is not None and usable[0] != usable[1]:
        flags.append(
            f"The resolution differs between the axes ({usable[0]:g} x {usable[1]:g} dpi); "
            "millimetres are converted on each axis separately and the page is not resampled "
            "to equal axes."
        )
    return stored, usable, flags


def _check_split(source: Source, split_record, turns: int) -> None:
    for page in range(split_record["value"]["pages"]):
        try:
            Chain.build(source.size, turns, split_record["value"], page, 0.0, 0.0)
        except GeometryError as error:
            raise PrepareError(
                f"{source.relative}: the {split_record['origin']} split cannot be used: {error}"
            ) from error


def _run_source(source, old, overrides, runner: _Runner, base, extension, output_dir):
    values = runner.values
    resolution_override = overrides.get(("resolution", None))
    stored, usable, source_flags = _resolution(source, old, resolution_override, values)
    if runner.dry and old is not None and old["resolution"] != stored:
        runner._note(source, "resolution", None, "the resolution changes")
    old_steps = old["steps"] if old else {}
    steps: dict[str, dict[str, Any]] = {}
    earlier: dict[str, Any] = {}
    uncertain: set[str] = set()
    for step in SOURCE_STEPS:
        entry, unsure = runner.settle(
            source, step, None, earlier, old_steps.get(step), usable, uncertain
        )
        if entry is None:  # dry run, never computed: the later steps are not either
            for later in STEPS[STEPS.index(step) + 1 :]:
                runner._note(source, later, None, "not computed yet")
            return None
        if unsure:
            uncertain.add(step)
        steps[step] = entry
        earlier[step] = entry["value"]
    _check_split(source, steps["split"], earlier["orientation"])
    count = earlier["split"]["pages"]
    old_pages = {page["page"]: page for page in (old["dropped_pages"] if old else [])}
    old_pages.update({page["page"]: page for page in (old["pages"] if old else [])})
    for (_step, page), override in sorted(overrides.items(), key=lambda item: item[1].where):
        if page is not None and page > count:
            raise PrepareError(
                f"{override.where}: {source.relative} has {count} page(s); there is no page {page}"
            )
    dropped_pages, split_flags = [], []
    for number in sorted(old_pages):
        if number <= count:
            continue  # a page that exists, or comes back with its values restored
        output_name = f"{base}_p{number}.{extension}"
        kept = {
            step: entry
            for step, entry in old_pages[number]["steps"].items()
            if entry["origin"] != "detected"
        }
        # A page with nothing set by hand is remembered only while its old output is
        # still in the output folder, so that file can be reported as stale.
        if not kept and not (output_dir / output_name).is_file():
            continue
        dropped_pages.append({"page": number, "output": output_name, "steps": kept})
        if not kept:
            continue
        message = (
            f"Page {number} no longer exists (the split gives {count} page(s)), but it had "
            f"values set by hand ({', '.join(sorted(kept))}); they are kept in the project's "
            "dropped_pages and come back if the page does. To discard them, delete that "
            "entry from dropped_pages in the project file."
        )
        split_flags.append(message)
        if runner.dry:
            runner._note(source, "pages", number, message)
    pages = []
    plans = []
    for number in range(1, count + 1):
        page_earlier = dict(earlier)
        page_steps = {}
        page_uncertain = set(uncertain)
        old_page = old_pages.get(number, {"steps": {}})
        for step in PAGE_STEPS:
            entry, unsure = runner.settle(
                source,
                step,
                number,
                page_earlier,
                old_page["steps"].get(step),
                usable,
                page_uncertain,
            )
            if entry is None:  # dry run, never computed: the later steps are not either
                for later in PAGE_STEPS[PAGE_STEPS.index(step) + 1 :]:
                    runner._note(source, later, number, "not computed yet")
                break
            if unsure:
                page_uncertain.add(step)
            page_steps[step] = entry
            page_earlier[step] = entry["value"]
        output_name = f"{base}_p{number}.{extension}"
        pages.append({"page": number, "output": output_name, "steps": page_steps})
        if runner.dry:
            continue
        chain, chain_flags, output_dpi = _page_chain(
            source, number, page_earlier, usable, stored, values
        )
        all_steps = {**steps, **page_steps}
        flags = [{"step": "resolution", "reason": reason} for reason in source_flags]
        flags += [{"step": "split", "reason": reason} for reason in split_flags]
        for step in STEPS:
            flags += [{"step": step, "reason": reason} for reason in all_steps[step]["flags"]]
        flags += [{"step": "margin", "reason": reason} for reason in chain_flags]
        plans.append(
            PagePlan(source, number, output_name, chain, all_steps, flags, output_dpi, stored)
        )
    entry = {
        "path": source.relative,
        "sha256": source.sha256,
        "bytes": source.bytes,
        "size": list(source.size),
        "mode": source.mode,
        "resolution": stored,
        "flags": source_flags + split_flags,
        "steps": steps,
        "pages": pages,
        "dropped_pages": dropped_pages,
    }
    return entry, plans


def _page_chain(source, number, values, usable, stored, settings):
    """The chain of page `number` (from 1), any margin flags, and its output dpi."""
    turns = values["orientation"]
    overlap = _overlap_px(settings, usable, turns)
    upright_dpi = _upright_dpi(usable, turns)
    if upright_dpi is None:
        per_mm = (0.0, 0.0)
    else:
        per_mm = (upright_dpi[0] / _MM_PER_INCH, upright_dpi[1] / _MM_PER_INCH)
    margin, allowance = values["margin"], settings["margin_allowance_mm"]
    flags = []
    box = margin_box(
        values["page_box"],
        values["content_box"],
        (margin * per_mm[0], margin * per_mm[1]),
        (allowance * per_mm[0], allowance * per_mm[1]),
    )
    if box is None:
        box = tuple(values["page_box"])
        flags.append(
            "The content box lies wholly outside the page box and its allowance, so the "
            "whole page box was kept."
        )
    scale = 1.0
    if upright_dpi is not None and settings["max_output_dpi"] > 0:
        scale = min(1.0, settings["max_output_dpi"] / max(upright_dpi))
    try:
        chain = Chain.build(
            source.size, turns, values["split"], number - 1, overlap, values["skew"], box, scale
        )
    except GeometryError as error:
        raise PrepareError(f"{source.relative} page {number}: {error}") from error
    declared = _upright_dpi(stored["value"], turns)
    output_dpi = None
    if declared is not None:
        output_dpi = (declared[0] * chain.scale[0], declared[1] * chain.scale[1])
    return chain, flags, output_dpi


def _match_override(override: Override, sources: list[Source], folder: Path) -> Source:
    if SHA256.fullmatch(override.source):
        matches = [source for source in sources if source.sha256 == override.source]
        if len(matches) > 1:
            raise PrepareError(
                f"{override.where}: more than one source has sha256 {override.source}; "
                "name it by path"
            )
    else:
        wanted = (folder / override.source).resolve()
        matches = [source for source in sources if source.path == wanted]
    if not matches:
        raise PrepareError(
            f"{override.where}: there is no source image {override.source!r} in this run; "
            "nothing was changed"
        )
    return matches[0]


def plan(
    inputs: list[str | Path] | None,
    output_dir: str | Path,
    project_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
    detectors: Mapping[str, Detector] | None = None,
    settings_overrides: dict[str, Any] | None = None,
    dry: bool = False,
) -> Plan:
    """Read every source, settle every step value and plan every output, writing nothing.

    With no project file named, `output_dir/pagekit-project.json` is continued from if
    it exists, so a re-run never loses a correction. With `dry`, no detector runs and
    the plan holds only the list of stale steps and why.
    """
    settings = load_settings(settings_overrides)
    values = {name: entry["value"] for name, entry in settings.items()}
    unknown = sorted(set(detectors or {}) - set(STEPS))
    if unknown:
        raise PrepareError(f"detectors given for unknown steps {unknown}")
    detectors = {**NEUTRAL_DETECTORS, **(detectors or {})}
    output_dir = Path(output_dir).resolve()
    if project_path is not None:
        project_path = Path(project_path).resolve()
        old_project = load_project(project_path)
    else:
        project_path = output_dir / PROJECT_NAME
        old_project = load_project(project_path) if project_path.exists() else None
    project_folder = project_path.parent
    if inputs:
        paths = _gather_inputs([Path(item) for item in inputs])
    elif old_project is not None:
        paths = [(project_folder / entry["path"]).resolve() for entry in old_project["sources"]]
        for path in paths:
            if not path.is_file():
                raise PrepareError(f"the project's source {path} does not exist")
    else:
        raise PrepareError("no source images were given")
    for path in paths:
        if _inside(output_dir, path.parent) or _inside(project_folder, path.parent):
            raise PrepareError(
                f"pagekit never writes inside a source folder, and {path.parent} holds "
                f"{path.name}; choose an output folder and project file elsewhere"
            )
    sources = [read_source(path, project_folder) for path in paths]

    old_entries: dict[str, dict[str, Any]] = {}
    if old_project is not None:
        by_path = {entry["path"]: entry for entry in old_project["sources"]}
        by_sha: dict[str, list[dict[str, Any]]] = {}
        for entry in old_project["sources"]:
            by_sha.setdefault(entry["sha256"], []).append(entry)
        used: set[str] = set()
        for source in sources:
            entry = by_path.get(source.relative)
            if entry is None and len(by_sha.get(source.sha256, [])) == 1:
                entry = by_sha[source.sha256][0]  # the same file, moved
            if entry is not None and entry["path"] not in used:
                used.add(entry["path"])
                old_entries[source.relative] = entry
        missing = sorted(set(by_path) - used)
        if missing:
            raise PrepareError(
                f"the project holds sources not given in this run ({', '.join(missing)}); "
                "give them too, or start a new project"
            )

    overrides: dict[str, dict[tuple[str, int | None], Override]] = {
        source.relative: {} for source in sources
    }
    if overrides_path is not None:
        overrides_path = Path(overrides_path).resolve()
        for override in load_overrides(overrides_path):
            source = _match_override(override, sources, overrides_path.parent)
            key = (override.step, override.page)
            if key in overrides[source.relative]:
                raise PrepareError(f"{override.where} repeats an override of the same step")
            overrides[source.relative][key] = override

    extension = "png" if values["output_format"] == "png" else "tif"
    bases = _output_names(sources, extension)
    runner = _Runner(settings, detectors, {}, dry, None)
    entries, pages = [], []
    for source in sources:
        cache: dict[str, Image.Image] = {}

        def load(source=source, cache=cache) -> Image.Image:
            if "image" not in cache:
                cache["image"] = source.open()
            return cache["image"]

        runner.overrides = overrides[source.relative]
        runner.image_loader = load
        result = _run_source(
            source,
            old_entries.get(source.relative),
            overrides[source.relative],
            runner,
            bases[source.relative],
            extension,
            output_dir,
        )
        if result is not None:
            entry, source_pages = result
            entries.append(entry)
            pages += source_pages
    project = {
        "schema": PROJECT_SCHEMA,
        "tool": {"name": "pagekit", "version": __version__},
        "settings": settings,
        "sources": sorted(entries, key=lambda entry: entry["path"]),
    }
    stale_outputs = sorted(
        dropped["output"]
        for entry in entries
        for dropped in entry["dropped_pages"]
        if (output_dir / dropped["output"]).is_file()
    )
    return Plan(output_dir, project_path, project, pages, settings, runner.stale, stale_outputs)
