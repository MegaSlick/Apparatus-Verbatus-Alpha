"""`prepare`: run the steps for each source image, keep corrections, plan the outputs.

The steps run in order: orientation and split once per source, then skew, page box,
content box and margin once per page. Each step's value comes from, in this order of
precedence: an override in the overrides file being applied; a value already set by
hand (manual or locked) in the project; a detected value in the project whose inputs
and method are unchanged; or the step's detector, run now. A step with no detector
connected takes a neutral default, recorded as detected with confidence 0 and a flag.
The margin is a setting, not a detection: unless set by hand it is `margin_mm`, recorded
as detected with confidence 1 and no flag.

A detector is a `Detector`: a method name with a version, the settings it reads (part
of every value's inputs hash), a function that takes a `StepContext` and returns an
`Answer`, and optionally a function that compares a value set by hand with what the
detector finds. Pass them to `plan` by step name; `pagekit.pipeline.DETECTORS` connects
the detectors of specs 0003 and 0004.

A detector that raises an error, or gives an answer pagekit refuses, fails for that page
alone: the step takes its neutral default with confidence 0 and a flag naming
the step and the error, and the batch carries on. A value set by hand is never detected
again, but when its detector can compare, it runs, and a confident answer far from the
hand-set value is reported in the evidence the manifest and review sheet show (never in
the project file, never as a change and never as a flag).

Nothing is written until the whole batch has been read and every value settled, so an
input that cannot be used stops the run with nothing written (PrepareError).
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from pagekit import __version__
from pagekit.answer import PAGE_STEPS, SOURCE_STEPS, STEPS, Answer, AnswerError, validate_answer
from pagekit.geometry import (
    TAG_TRANSPOSE,
    TAG_WORDS,
    Chain,
    GeometryError,
    apply,
    apply_tag,
    margin_box,
    paper_colour,
    polygon_mask,
    render,
    tagged_size,
)
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
CACHE_NAME = "pagekit-cache"  # the stage cache's default folder, beside the output folder
IMAGE_SUFFIXES = (".png", ".tif", ".tiff", ".jpg", ".jpeg")
# Modes read: grey and colour as they are; bilevel to grey; palette to grey or colour.
_KEPT_MODES = frozenset({"L", "RGB", "1", "P"})
_MM_PER_INCH = 25.4
_ORIENTATION_TAG = 0x0112  # Exif and TIFF tag 274


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
    tag_found: int | None = None  # the file's orientation tag as read, if it has one
    tag_on_open: bool = False  # the image library applied that tag as it opened the file
    undo_tag: int | None = None  # undo the library's turn: the tag is not trusted

    def open(self) -> Image.Image:
        """The decoded original in L or RGB, checked against the sha256 read first: the
        grid the chain starts from."""
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise PrepareError(f"{self.path} changed while pagekit was running")
        return _decode(data, self.path, self.undo_tag)


def _grey_palette(image: Image.Image) -> bool:
    """Whether every colour a palette image uses is a grey (red = green = blue)."""
    palette = image.getpalette("RGB") or []
    used = image.getcolors(256) or []
    for _, index in used:
        red, green, blue = palette[3 * index : 3 * index + 3] or (0, 0, 0)
        if not red == green == blue:
            return False
    return True


class SourceError(PrepareError):
    """One source file cannot be used; `reason` says why in plain words. The run skips
    it and carries on with the others."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path.name}: {reason}")
        self.path = path
        self.reason = reason


# Plain names for the modes pagekit does not read.
_MODE_WORDS = {
    "I;16": "16-bit grey",
    "I;16B": "16-bit grey",
    "I;16L": "16-bit grey",
    "I;16N": "16-bit grey",
    "I": "32-bit grey",
    "F": "floating-point grey",
    "CMYK": "CMYK (printing) colour",
    "LA": "grey with transparency",
    "RGBA": "colour with transparency",
    "PA": "palette with transparency",
    "YCbCr": "YCbCr colour",
    "LAB": "Lab colour",
    "HSV": "HSV colour",
}


# The transpose that undoes each orientation tag's transform.
_UNDO_TAG = {2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6}


def _decode(data: bytes, path: Path, undo_tag: int | None = None) -> Image.Image:
    """The decoded source in L or RGB, as the image library opens it (pagekit's source
    grid). Bilevel and grey-palette images become grey, colour-palette images colour;
    nothing is resampled. With `undo_tag`, the turn the library applied on open for that
    tag is undone, giving the stored pixels. SourceError when it cannot be used."""
    image = _decode_opened(data, path)[0]
    if undo_tag is not None:
        image = image.transpose(TAG_TRANSPOSE[_UNDO_TAG[undo_tag]])
    return image


def _decode_opened(data: bytes, path: Path) -> tuple[Image.Image, int | None, bool]:
    """The decoded image, the orientation tag read before loading, and whether the
    image library applied that tag while loading.

    Pillow turns some carriers upright by their tag as it loads them (TIFF, today) and
    drops the tag; others (PNG, JPEG) it leaves as stored. Which it does is found, not
    assumed: a tag of 2 to 8 that is gone after loading was applied; so was a tag of 5
    to 8 when the loaded size is the transposed size the file's own header gives; and
    for a TIFF, the loaded pixels are compared with the same file read with its tag set
    to 1, which settles it for every tag whatever a later Pillow does."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            before = _file_tag(image)
            header = _header_size(image)
            if getattr(image, "n_frames", 1) != 1:
                raise SourceError(path, "it holds more than one page; give one page per file")
            if image.mode not in _KEPT_MODES:
                words = _MODE_WORDS.get(image.mode, f"mode {image.mode}")
                raise SourceError(
                    path,
                    f"its image is {words}, which pagekit does not read (it reads 8-bit grey, "
                    "colour, bilevel and palette images); save it as 8-bit grey or colour",
                )
            image.load()
            after = _file_tag(image)
            on_open = before in _TAG_TRANSFORMS and (
                after in (None, 1)
                or (
                    before in (5, 6, 7, 8)
                    and header is not None
                    and header[0] != header[1]
                    and image.size == (header[1], header[0])
                )
            )
            if before in _TAG_TRANSFORMS and header is not None:
                # A TIFF: compare with the same file read with its tag set to 1, which
                # is the stored pixels whatever the library does with tags.
                found = _applied_by_comparison(data, image, before)
                if found is not None:
                    on_open = found
            if image.mode == "1":
                decoded = image.convert("L")
            elif image.mode == "P":
                decoded = image.convert("L" if _grey_palette(image) else "RGB")
            else:
                decoded = image.copy()
            return decoded, before, on_open
    except SourceError:
        raise
    except UnidentifiedImageError as error:
        raise SourceError(
            path, "not an image pagekit can read (it may be damaged, empty or of another kind)"
        ) from error
    except Exception as error:  # any decoder failure means the source cannot be used
        if "truncated" in str(error).lower():
            reason = "the image is cut short (the file is truncated); copy or scan it again"
        else:
            reason = (
                f"the image cannot be decoded (the file may be damaged; {type(error).__name__})"
            )
        raise SourceError(path, reason) from error


_TAG_TRANSFORMS = (2, 3, 4, 5, 6, 7, 8)


def _tiff_without_tag(data: bytes) -> bytes | None:
    """The TIFF's bytes with its orientation tag set to 1 (first directory), or None
    when the tag cannot be found where a plain TIFF keeps it."""
    if data[:2] == b"II":
        order = "<"
    elif data[:2] == b"MM":
        order = ">"
    else:
        return None
    try:
        (offset,) = struct.unpack(order + "I", data[4:8])
        (count,) = struct.unpack(order + "H", data[offset : offset + 2])
        patched = bytearray(data)
        for index in range(count):
            at = offset + 2 + 12 * index
            tag, kind, number = struct.unpack(order + "HHI", data[at : at + 8])
            if tag == _ORIENTATION_TAG and kind == 3 and number == 1:
                patched[at + 8 : at + 10] = struct.pack(order + "H", 1)
                return bytes(patched)
    except struct.error:
        return None
    return None


def _applied_by_comparison(data: bytes, loaded: Image.Image, tag: int) -> bool | None:
    """Whether the library applied `tag` while loading this TIFF: the loaded pixels
    equal the stored pixels turned by the tag (True), or the stored pixels as they are
    (False). None when that cannot be told."""
    plain = _tiff_without_tag(data)
    if plain is None:
        return None
    try:
        with Image.open(io.BytesIO(plain)) as stored:
            stored.load()
            stored = stored.copy()
    except Exception:
        return None
    turned = stored.transpose(TAG_TRANSPOSE[tag])
    if loaded.size == turned.size and loaded.tobytes() == turned.tobytes():
        return True
    if loaded.size == stored.size and loaded.tobytes() == stored.tobytes():
        return False
    return None


def _header_size(image: Image.Image) -> tuple[int, int] | None:
    """The stored width and height the file's own header gives, where pagekit can read
    it (TIFF image width and length), else None."""
    tags = getattr(image, "tag_v2", None)
    if tags is None:
        return None
    try:
        return int(tags[256]), int(tags[257])
    except (KeyError, TypeError, ValueError):
        return None


def _file_tag(image: Image.Image) -> int | None:
    """The file's orientation tag (Exif tag 274) as a whole number, if it has one; a
    value that is not a whole number reads as 0, which is not a valid tag."""
    try:
        value = image.getexif().get(_ORIENTATION_TAG)
    except Exception:  # unreadable metadata is no tag
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
        reason = error.strerror or type(error).__name__
        raise SourceError(path, f"the file cannot be read ({reason})") from error
    image, tag, on_open = _decode_opened(data, path)
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
        tag_found=tag,
        tag_on_open=on_open,
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
    cache: dict[Any, Any] = field(repr=False, default_factory=dict)  # shared per source
    tag: int = 1  # the orientation tag applied before everything else (1: none)

    def image(self) -> Image.Image:
        """The decoded original source. Never change it and never write it out."""
        return self._image()

    def frame(self) -> Image.Image:
        """The source after its orientation tag (exact): what orientation and the split
        look at."""
        return apply_tag(self.image(), self.tag)

    @property
    def frame_size(self) -> tuple[int, int]:
        return tagged_size(self.source.size, self.tag)

    def chain(self, scale: float = 1.0) -> Chain:
        """The chain to the grid this step works in, whole, scaled by `scale` (<= 1).

        Orientation works on the tagged frame (the source after its orientation tag),
        split on the upright frame, skew on the page before levelling, and the box steps
        on the levelled page.
        """
        size, tag = self.source.size, self.tag
        if self.step == "orientation":
            return Chain.build(size, 0, {"pages": 1}, 0, 0.0, 0.0, None, scale, tag)
        turns = self.values["orientation"]
        if self.step == "split":
            return Chain.build(size, turns, {"pages": 1}, 0, 0.0, 0.0, None, scale, tag)
        angle = 0.0 if self.step == "skew" else self.values["skew"]
        overlap = _overlap_px(self.settings, self.resolution, turns)
        return Chain.build(
            size, turns, self.values["split"], self.page - 1, overlap, angle, None, scale, tag
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
    # (value set by hand, the detector's answer, context) -> a sentence when they differ
    # by more than the step's comparison setting, else None.
    compare: Callable[[Any, Answer, StepContext], str | None] | None = None
    # pagekit's files the detector reads: its settings files and its own code. Their
    # sha256 enter every value's inputs hash, so editing one recomputes what it decided.
    files: tuple[str, ...] = ()


def file_digest(name: str) -> str:
    """The sha256 of pagekit's own file `name` (a settings file or a module)."""
    return hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()


def _neutral(step: str, description: str, value: Callable[[StepContext], Any], reads=()):
    flag = (
        f"The {step} step was not run: no detector for it is connected yet, so the neutral "
        f"default ({description}) was used."
    )

    def run(context: StepContext) -> Answer:
        return Answer(value(context), 0.0, f"Neutral default: {description}.", (flag,))

    return Detector(f"pagekit.neutral-default.{step}/1", run, tuple(reads))


_NEUTRAL_WORDS = {
    "orientation": "no quarter turn",
    "split": "one page",
    "skew": "no skew",
    "page_box": "the whole levelled page",
    "content_box": "the whole page box",
    "margin": "the margin setting",
}


CROP_MODES = ("none", "page", "content")
CROP_WORDS = {
    "none": "off (the whole levelled side of the cut is kept)",
    "page": "to the page box",
    "content": "to the content box, with the margin",
}


def _crop_off_detector(step: str, real: Detector, values: dict[str, Any]) -> Detector:
    """A box step with cropping off: the page box is the whole levelled side and the
    content box the page box, so nothing of the side is cut away. With the
    crop_detectors_when_off setting, the real detector still runs and its answer is
    reported in the evidence, never applied."""
    report = bool(values["crop_detectors_when_off"])
    words = "page box" if step == "page_box" else "content box"

    def run(context: StepContext) -> Answer:
        value = _whole_grid(context) if step == "page_box" else context.values["page_box"]
        evidence = f"Cropping is off for this page, so the {words} is the whole levelled side."
        if report:
            try:
                found = validate_answer(step, real.run(context))
                evidence += (
                    f" The {words} detector, run only to report, would have cut to "
                    f"{found.value} (confidence {found.confidence:.2f})."
                )
            except Exception as error:  # reporting only: a failure changes nothing
                evidence += f" The {words} detector, run only to report, failed ({error})."
        return Answer(value, 1.0, evidence, ())

    method = f"pagekit.crop-off.{step}/1" + (f" reporting {real.method}" if report else "")
    reads = ("crop_detectors_when_off",) + (real.settings if report else ())
    return Detector(method, run, reads, None, real.files if report else ())


def _crop_mode(override, old_page: dict, overrides: dict, number: int, values) -> dict:
    """A page's crop mode and who set it: an override this run; one set by hand before;
    a box set by hand (cropping is then on for that page, to that box); the crop setting."""
    if override is not None:
        set_by = "locked" if override.lock else "manual"
        return {"value": override.value, "set_by": set_by, "evidence": override.evidence}
    old = old_page.get("crop")
    if old is not None:
        return dict(old)
    steps = old_page.get("steps", {})

    def by_hand(step: str) -> bool:
        if overrides.get((step, number)) is not None:
            return True
        return steps.get(step, {}).get("origin") in ("manual", "locked")

    if by_hand("content_box"):
        return {"value": "content", "set_by": "box", "evidence": "A content box was set by hand."}
    if by_hand("page_box") and values["crop"] != "content":
        return {"value": "page", "set_by": "box", "evidence": "A page box was set by hand."}
    return {"value": values["crop"], "set_by": "setting", "evidence": "From the crop setting."}


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
    tag: dict[str, Any] = field(default_factory=dict)  # the orientation tag's record
    mode: dict[str, Any] = field(default_factory=dict)  # the output mode
    density: dict[str, Any] | None = None  # a nominal density set by hand, if any
    # The usable resolution (x, y) of the upright frame and the levelled page, before
    # any shrinking, in the axes the tag and the turns give; None when there is none.
    upright_resolution: list[float] | None = None
    applied: dict[str, Any] = field(default_factory=dict)  # which steps were applied
    crop: dict[str, Any] = field(default_factory=dict)  # the crop mode and who set it


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
    batch: dict[str, Any] = field(default_factory=dict)  # the volume-wide checks
    tone_view: bool = False  # also write the grey tone view beside each page
    cache_dir: Path | None = None  # the stage cache, or None when off
    # Source files that cannot be used: name, path, sha256 (None if unreadable), reason.
    skipped: list[dict[str, Any]] = field(default_factory=list)


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
        self.cache: dict[Any, Any] = {}
        self.source_dpi: float | None = None  # --dpi, for sources that carry none
        self.tag = 1  # the orientation tag the chain applies to the source being settled
        # The tag applied (whoever applied it) and the grid the chain starts from, for a
        # source with a tag; part of every step's inputs. None for a source without one.
        self.grid: dict[str, Any] | None = None
        self.run_mode: str | None = None  # this run's output mode choice, if one was made
        # {(source, step, page): sentence} added to that step's evidence in the manifest.
        self.notes: dict[tuple[str, str, int | None], str] = {}
        self.stale: list[dict[str, Any]] = []
        # {(source relative path, step, page): sentence} for hand-set values a confident
        # detection disagrees with.
        self.comparisons: dict[tuple[str, str, int | None], str] = {}
        self.crop_off: frozenset[str] = frozenset()  # box steps off for the current page

    def _detector(self, step: str) -> Detector:
        """The step's detector, or, for a box step with cropping off, the whole side."""
        if step in self.crop_off:
            return _crop_off_detector(step, self.detectors[step], self.values)
        return self.detectors[step]

    def _reads(self, step: str, resolution) -> dict[str, Any]:
        detector = self._detector(step)
        reads = {name: self.values[name] for name in detector.settings}
        for name in detector.files:
            reads[f"file {name}"] = file_digest(name)
        if self.grid is not None:  # only for a tagged source, so others read as before
            reads["orientation_tag"] = self.grid
        if step in PAGE_STEPS or step == "split":
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
            self._compare(source, step, page, earlier, resolution, new["value"])
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
            self._compare(source, step, page, earlier, resolution, kept["value"])
            return kept, False
        detector = self._detector(step)
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
        context = self._context(source, step, page, earlier, resolution)
        method = detector.method
        try:
            answer = validate_answer(step, detector.run(context))
        except PrepareError:
            raise  # the source itself cannot be used (changed while running): stop
        except Exception as error:  # one page's failure is a flag on that page, never a stop
            answer, method = _failed(step, detector, error, context)
        new = record(
            answer.value,
            "detected",
            answer.confidence,
            answer.evidence,
            list(answer.flags),
            method,
            inputs,
            inputs_hash,
        )
        return new, False

    def _context(self, source, step, page, earlier, resolution) -> StepContext:
        return StepContext(
            step,
            page,
            source,
            resolution,
            dict(earlier),
            self.values,
            self.image_loader,
            self.cache,
            self.tag,
        )

    def _compare(self, source, step, page, earlier, resolution, value) -> None:
        """Run the step's detector beside a hand-set value and keep any large difference."""
        detector = self.detectors[step]
        if self.dry or detector.compare is None:
            return
        context = self._context(source, step, page, earlier, resolution)
        try:
            answer = validate_answer(step, detector.run(context))
            sentence = detector.compare(value, answer, context)
        except Exception:  # comparing is advice; a failure to compare changes nothing
            return
        if sentence:
            self.comparisons[(source.relative, step, page)] = sentence


def _failed(step: str, detector: Detector, error: Exception, context: StepContext):
    """The neutral default for a step whose detector failed, with a flag saying so."""
    neutral = NEUTRAL_DETECTORS[step]
    value = neutral.run(context).value
    if isinstance(error, AnswerError):
        what = f"gave an answer pagekit refuses ({error})"
    else:
        what = f"stopped with an error ({type(error).__name__}: {error})"
    description = _NEUTRAL_WORDS[step]
    flag = (
        f"The {step.replace('_', ' ')} step could not be measured on this page: its detector "
        f"{what}. The neutral default ({description}) was used instead; look at this page."
    )
    evidence = f"Neutral default ({description}) after the detector {detector.method} failed."
    method = f"{neutral.method} after {detector.method} failed"
    return Answer(value, 0.0, evidence, (flag,)), method


def _without_flags(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "flags"}


def _resolution(source: Source, old: dict | None, override: Override | None, values, given=None):
    """The stored resolution, the resolution the millimetre settings use, and flags.

    `given` is the resolution `--dpi` gives to a source that carries none."""
    if override is not None:
        stored = {"value": override.value, "origin": "override"}
    elif source.file_dpi is None and given is not None:
        stored = {"value": [float(given), float(given)], "origin": "override"}
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
            "the millimetre settings (overlap, margin, allowance) were applied as 0 px. "
            "Give the scan's resolution with --dpi (for example --dpi 300), or with a "
            "resolution line in the overrides file."
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


def _check_split(source: Source, split_record, turns: int, tag: int = 1) -> None:
    for page in range(split_record["value"]["pages"]):
        try:
            Chain.build(source.size, turns, split_record["value"], page, 0.0, 0.0, tag=tag)
        except GeometryError as error:
            raise PrepareError(
                f"{source.relative}: the {split_record['origin']} split cannot be used: {error}"
            ) from error


def _run_source(source, old, overrides, runner: _Runner, base, extension, output_dir):
    values = runner.values
    resolution_override = overrides.get(("resolution", None))
    stored, usable, source_flags = _resolution(
        source, old, resolution_override, values, runner.source_dpi
    )
    if runner.dry and old is not None and old["resolution"] != stored:
        runner._note(source, "resolution", None, "the resolution changes")
    tag_record, tag_flags, tag_note, tag = _orientation_tag(
        source, old, overrides.get(("tag_trust", None)), values
    )
    runner.tag = tag
    runner.grid = grid_key(tag_record)
    if tag_note:
        runner.notes[(source.relative, "orientation", None)] = tag_note
    # The millimetre settings work in the tagged frame. The file's resolution is for its
    # stored axes, which a tag of 5 to 8 swaps, whoever applies it.
    axes = tag_record["found"] if tag_record["applied"] else 1
    usable = _tagged_dpi(usable, axes)
    stored_frame = dict(stored, value=_tagged_dpi(stored["value"], axes))
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
    _check_split(source, steps["split"], earlier["orientation"], tag)
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
        crop = _crop_mode(overrides.get(("crop", number)), old_page, overrides, number, values)
        off = {"none": {"page_box", "content_box"}, "page": {"content_box"}, "content": set()}
        runner.crop_off = frozenset(off[crop["value"]])
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
        mode_record = _mode_record(
            overrides.get(("output_mode", number)), old_page.get("output_mode"), runner.run_mode
        )
        runner.crop_off = frozenset()
        page_entry = {"page": number, "output": output_name, "steps": page_steps}
        if crop["set_by"] in ("manual", "locked"):
            page_entry["crop"] = crop
        if mode_record is not None:
            page_entry["output_mode"] = mode_record
        density_override = overrides.get(("density", number))
        nominal = density_override.value if density_override else old_page.get("density")
        if nominal is not None:
            page_entry["density"] = list(nominal)
        pages.append(page_entry)
        if runner.dry:
            continue
        chain, chain_flags, output_dpi, upright_resolution = _page_chain(
            source, number, page_earlier, usable, stored_frame, values, tag, crop["value"]
        )
        applied = {
            "orientation_tag": tag_record["applied"],
            "orientation": True,
            "split": True,
            "skew": True,
            "page_box": crop["value"] != "none",
            "content_box": crop["value"] == "content",
            "margin": crop["value"] == "content",
            "crop": crop["value"],
        }
        if crop["value"] != "content":
            runner.notes[(source.relative, "margin", number)] = (
                f"The margin is not applied: cropping is {CROP_WORDS[crop['value']]}."
            )
        all_steps = {}
        for step, entry in {**steps, **page_steps}.items():
            key = (source.relative, step, None if step in SOURCE_STEPS else number)
            said = [runner.comparisons.get(key), runner.notes.get(key)]
            sentence = " ".join(part for part in said if part) or None
            # The project keeps the person's own evidence; the manifest and the review
            # sheet also say where a confident detection disagrees, or a tag is not
            # trusted.
            all_steps[step] = (
                entry
                if sentence is None
                else {**entry, "evidence": f"{entry['evidence']} {sentence}"}
            )
        flags = [{"step": "resolution", "reason": reason} for reason in source_flags]
        flags += [{"step": "orientation_tag", "reason": reason} for reason in tag_flags]
        flags += [{"step": "split", "reason": reason} for reason in split_flags]
        for step in STEPS:
            flags += [{"step": step, "reason": reason} for reason in all_steps[step]["flags"]]
        flags += chain_flags
        flags += _outside_flags(chain, page_steps)
        density = None
        if nominal is not None:
            density = _nominal_density(source, number, nominal, output_dpi)
            output_dpi = tuple(density["nominal"])
        mode, mode_flags = _output_mode(
            source, number, page_earlier, usable, mode_record, chain, runner, tag
        )
        flags += [{"step": "output_mode", "reason": reason} for reason in mode_flags]
        plans.append(
            PagePlan(
                source,
                number,
                output_name,
                chain,
                all_steps,
                flags,
                output_dpi,
                stored,
                tag_record,
                mode,
                density,
                None if upright_resolution is None else list(upright_resolution),
                applied,
                crop,
            )
        )
    entry = {
        "path": source.relative,
        "sha256": source.sha256,
        "bytes": source.bytes,
        "size": list(source.size),
        "mode": source.mode,
        "resolution": stored,
        "orientation_tag": tag_record,
        "flags": source_flags + tag_flags + split_flags,
        "steps": steps,
        "pages": pages,
        "dropped_pages": dropped_pages,
    }
    return entry, plans


_BOX_WORDS = {"page_box": "page box", "content_box": "content box"}


def _outside_flags(chain: Chain, steps: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Flags for a page or content box that lies partly or wholly outside the levelled
    page, where there is nothing but the paper colour pagekit fills in."""
    width, height = chain.levelled_size
    flags = []
    for step, words in _BOX_WORDS.items():
        entry = steps[step]
        box = entry["value"]
        if box is None:
            continue
        left, top, right, bottom = box
        if left >= 0 and top >= 0 and right <= width and bottom <= height:
            continue
        wholly = right <= 0 or bottom <= 0 or left >= width or top >= height
        who = "set by hand" if entry["origin"] != "detected" else "found"
        flags.append(
            {
                "step": step,
                "reason": (
                    f"The {words} {who}, {box}, lies {'wholly' if wholly else 'partly'} "
                    f"outside the levelled page ({width} by {height} pixels), where there "
                    "is only filled-in paper colour; check the box."
                ),
            }
        )
    return flags


OUTPUT_MODES = ("source", "grey")
_SET_BY_WORDS = {
    "default": "the default",
    "run": "this batch's choice",
    "manual": "set by hand",
    "locked": "set by hand and locked",
}


def _mode_record(override: Override | None, old: dict | None, run_mode: str | None):
    """A page's output mode choice as the project keeps it, or None for the default.

    Precedence: an override in this run; one set by hand before; this run's choice
    (which covers only the sources of this run); an earlier run's choice."""
    if override is not None:
        set_by = "locked" if override.lock else "manual"
        return {"value": override.value, "set_by": set_by, "evidence": override.evidence}
    if old is not None and old["set_by"] in ("manual", "locked"):
        return dict(old)
    if run_mode is not None:
        return {
            "value": run_mode,
            "set_by": "run",
            "evidence": f"Chosen for the sources of a run (--output-mode {run_mode}).",
        }
    return None if old is None else dict(old)


def _output_mode(source, number, values, usable, record, chain, runner, tag):
    """The page's output mode as the manifest records it, and any flags.

    A grey choice on a page whose colour is more than its paper's noise is flagged,
    and the page is kept in source mode unless grey was set by hand or locked."""
    from pagekit.greypage import RULE_WORDS, colour_evidence, equal_channels

    settings = runner.values
    chosen = "source" if record is None else record["value"]
    set_by = "default" if record is None else record["set_by"]
    mode = {
        "mode": chosen,
        "chosen": chosen,
        "set_by": set_by,
        "rule": None,
        "rule_words": None,
        "exact": None,
        "colour": None,
    }
    if chosen != "grey":
        return mode, []
    image = runner.image_loader()
    if "equal channels" not in runner.cache:
        runner.cache["equal channels"] = equal_channels(image)
    exact = runner.cache["equal channels"]
    rule = settings["grey_rule"]
    mode.update({"rule": rule, "rule_words": RULE_WORDS[rule], "exact": exact})
    if exact:
        return mode, []
    # The colour measure, on a reduced copy of the page's side of the frame.
    dpi = _upright_dpi(usable, values["orientation"])
    dpi = dpi or (settings["unknown_dpi_assumed"],) * 2
    scale = min(1.0, settings["colour_working_dpi"] / max(dpi))
    overlap = _overlap_px(settings, usable, values["orientation"])
    frame = Chain.build(
        source.size,
        values["orientation"],
        values["split"],
        number - 1,
        overlap,
        0.0,
        None,
        scale,
        tag,
    )
    fill, _ = paper_colour(image, frame, settings["paper_estimate_long_side_px"])
    small = render(image, frame, fill)
    # Only the written page counts: the margin box, within the page box. A backdrop or
    # a colour target beside the paper is not colour on the page.
    margin = chain.crop_box
    page_box = values["page_box"]
    inner = (
        max(margin[0], page_box[0]),
        max(margin[1], page_box[1]),
        min(margin[2], page_box[2]),
        min(margin[3], page_box[3]),
    )
    if inner[2] <= inner[0] or inner[3] <= inner[1]:
        inner = margin
    corners = [
        (inner[0], inner[1]),
        (inner[2], inner[1]),
        (inner[2], inner[3]),
        (inner[0], inner[3]),
    ]
    outline = frame.forward(chain.inverse(apply(chain.levelled_to_output(), corners)))
    written = polygon_mask(small.size, outline)
    found = colour_evidence(small, _MM_PER_INCH / (max(dpi) * scale), settings, written)
    where = None
    if found["box"] is not None:
        x0, y0, x1, y1 = found["box"]
        corners = chain.forward(frame.inverse([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
        width, height = chain.output_size
        xs = [min(max(x, 0.0), width) for x, _ in corners]
        ys = [min(max(y, 0.0), height) for _, y in corners]
        where = [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]
    mode["colour"] = {
        "paper_chroma_noise": found["paper_chroma_noise"],
        "chroma_threshold": found["chroma_threshold"],
        "coloured_mm2": found["coloured_mm2"],
        "where": where,
    }
    if not found["holds_colour"]:
        return mode, []
    place = f"around x {where[0]} to {where[2]}, y {where[1]} to {where[3]} of the page"
    said = (
        f"This page holds colour that grey would remove: {found['coloured_mm2']:g} mm² of "
        f"marks stand clearly above the paper's own colour noise, {place}."
    )
    if set_by in ("manual", "locked"):
        return mode, [f"{said} It is made grey as {_SET_BY_WORDS[set_by]}; the colour is lost."]
    mode["mode"] = "source"
    return mode, [
        f"{said} It is kept in colour; to make it grey anyway, set output_mode grey for "
        "this page by hand."
    ]


def _tagged_dpi(resolution, tag: int):
    """A resolution (x, y) of the stored pixels, in the tagged frame's axes."""
    if resolution is None or tag not in (5, 6, 7, 8):
        return resolution
    swapped = (resolution[1], resolution[0])
    return list(swapped) if isinstance(resolution, list) else swapped


def grid_key(record: dict[str, Any]) -> dict[str, Any] | None:
    """What a source's tag did to the grid every step works on: the tag applied and by
    whom, and whether the grid is the stored pixels. None when the source has no tag."""
    if record["found"] in (None, 1):
        return None
    return {
        "tag": record["found"],
        "applied": record["applied"],
        "applied_by": record["applied_by"],
        "grid": record["grid"],
    }


def _tag_trust(old, override: Override | None, values) -> tuple[bool, str]:
    """Whether a source's orientation tag is trusted, and what says so."""
    if override is not None:
        return bool(override.value), "override"
    if old is not None and old.get("orientation_tag", {}).get("trust_origin") == "override":
        return old["orientation_tag"]["trusted"], "override"
    return bool(values["trust_orientation_tag"]), "setting"


def _orientation_tag(source: Source, old, override: Override | None, values):
    """The orientation tag's record for a source, flags, a note for the evidence, and
    the tag the chain applies (1: none).

    The source grid is the image as the image library opens it. A trusted tag of 2 to 8
    is applied once: by the library on open (then the chain adds nothing), or else as
    the chain's first link. An untrusted tag is not applied; where the library applied
    it on open, its turn is undone, so the grid is the stored pixels. A value outside 1
    to 8 is flagged and the source taken as stored."""
    trusted, origin = _tag_trust(old, override, values)
    found = source.tag_found
    flags, note, applied, by = [], None, 1, None
    grid = "stored pixels" if source.undo_tag else "as the image library opens it"
    if found is not None and found not in TAG_WORDS:
        flags.append(
            f"The file's orientation tag has the value {found}, which is not one of the "
            "eight the Exif standard defines (1 to 8); the source is taken as stored."
        )
    elif found not in (None, 1) and not trusted:
        undone = " (the turn the image library made on opening it was undone)"
        note = (
            f"The file's orientation tag ({found}: {TAG_WORDS[found]}) is not trusted for "
            f"this source, so the stored pixels were used as they are"
            f"{undone if source.undo_tag else ''}."
        )
    elif found not in (None, 1):
        applied = found
        by = "image library on open" if source.tag_on_open else "chain"
    record = {
        "found": found,
        "trusted": trusted,
        "trust_origin": origin,
        "applied": applied != 1,
        "applied_by": by,
        "grid": grid,
        "transform": TAG_WORDS[applied],
    }
    chain_tag = applied if by == "chain" else 1
    return record, flags, note, chain_tag


_RATIO_TOLERANCE = 0.002


def _nominal_density(source, number, nominal, accepted) -> dict[str, Any]:
    """A nominal output density set by hand, checked against the accepted resolution:
    the ratio of its axes must be the accepted one, since pagekit never stretches
    pixels to change it. PrepareError otherwise."""
    where = f"{source.relative} page {number}"
    if accepted is None:
        raise PrepareError(
            f"{where}: a nominal density needs the scan's resolution, which this source "
            "does not have; give it first (--dpi, or a resolution line)"
        )
    want = nominal[0] / nominal[1]
    have = accepted[0] / accepted[1]
    # Within the precision a file stores a resolution in (PNG keeps dots per metre).
    if abs(want - have) > _RATIO_TOLERANCE * have:
        raise PrepareError(
            f"{where}: the nominal density {nominal[0]:g} x {nominal[1]:g} dpi changes the "
            f"ratio between the axes (accepted {accepted[0]:g} x {accepted[1]:g} dpi); "
            "pagekit never stretches pixels to change it, so give a density with the same "
            "ratio"
        )
    return {"nominal": [float(v) for v in nominal], "accepted": [float(v) for v in accepted]}


def _padding(settings, upright_dpi, scale) -> tuple[tuple[int, int, int, int], list[dict]]:
    """The padding on each side in output pixels, and any flag: padding_px
    as it is, or padding_mm converted per axis with the output resolution."""
    if settings["padding_px"] > 0:
        side = int(settings["padding_px"])
        return (side, side, side, side), []
    if settings["padding_mm"] <= 0:
        return (0, 0, 0, 0), []
    if upright_dpi is None:
        return (0, 0, 0, 0), [
            {
                "step": "padding",
                "reason": (
                    f"Padding of {settings['padding_mm']:g} mm needs the scan's resolution, "
                    "which this source does not have, so none was added; give it with "
                    "--dpi, or set the padding in pixels."
                ),
            }
        ]
    across = round(settings["padding_mm"] * upright_dpi[0] * scale[0] / _MM_PER_INCH)
    down = round(settings["padding_mm"] * upright_dpi[1] * scale[1] / _MM_PER_INCH)
    return (across, down, across, down), []


def _page_chain(source, number, values, usable, stored, settings, tag: int = 1, crop="content"):
    """The chain of page `number` (from 1), any margin flags, and its output dpi.

    With cropping off (`crop` "none") the page is the whole levelled side of the cut;
    with "page" it is cut to the page box; with "content" to the content box plus the
    margin, held to the page box and its allowance (specs 0002, 0005)."""
    turns = values["orientation"]
    overlap = _overlap_px(settings, usable, turns)
    upright_dpi = _upright_dpi(usable, turns)
    if upright_dpi is None:
        per_mm = (0.0, 0.0)
    else:
        per_mm = (upright_dpi[0] / _MM_PER_INCH, upright_dpi[1] / _MM_PER_INCH)
    margin, allowance = values["margin"], settings["margin_allowance_mm"]
    flags = []
    if crop == "none":
        box = None
    elif crop == "page":
        box = tuple(values["page_box"])
    else:
        box = margin_box(
            values["page_box"],
            values["content_box"],
            (margin * per_mm[0], margin * per_mm[1]),
            (allowance * per_mm[0], allowance * per_mm[1]),
        )
    if box is None and crop == "content":
        box = tuple(values["page_box"])
        flags.append(
            {
                "step": "margin",
                "reason": (
                    "The content box lies wholly outside the page box and its allowance, "
                    "so the whole page box was kept."
                ),
            }
        )
    scale = 1.0
    if upright_dpi is not None and settings["max_output_dpi"] > 0:
        scale = min(1.0, settings["max_output_dpi"] / max(upright_dpi))
    try:
        chain = Chain.build(
            source.size,
            turns,
            values["split"],
            number - 1,
            overlap,
            values["skew"],
            box,
            scale,
            tag,
        )
    except GeometryError as error:
        raise PrepareError(f"{source.relative} page {number}: {error}") from error
    padding, padding_flags = _padding(settings, upright_dpi, chain.scale)
    flags += padding_flags
    if any(padding):
        chain = replace(chain, padding=padding)
    declared = _upright_dpi(stored["value"], turns)
    output_dpi = None
    if declared is not None:
        output_dpi = (declared[0] * chain.scale[0], declared[1] * chain.scale[1])
    return chain, flags, output_dpi, upright_dpi


def _skipped(path: Path, project_folder: Path, reason: str) -> dict[str, Any]:
    try:
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        sha256 = None
    return {
        "name": path.name,
        "path": Path(os.path.relpath(path, project_folder)).as_posix(),
        "sha256": sha256,
        "reason": reason,
    }


def _names_skipped(override: Override, skipped, folder: Path, project_folder: Path) -> bool:
    """Whether `override` names a source skipped in this run."""
    if SHA256.fullmatch(override.source):
        return any(entry["sha256"] == override.source for entry in skipped)
    wanted = (folder / override.source).resolve()
    return any((project_folder / entry["path"]).resolve() == wanted for entry in skipped)


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
    tone_view: bool = False,
    source_dpi: float | None = None,
    output_mode: str | None = None,
) -> Plan:
    """Read every source, settle every step value and plan every output, writing nothing.

    With no project file named, `output_dir/pagekit-project.json` is continued from if
    it exists, so a re-run never loses a correction. With `dry`, no detector runs and
    the plan holds only the list of stale steps and why. Once every page has its values,
    the volume-wide checks (pagekit.volume) compare each page with the rest of the batch.
    With `tone_view`, the grey tone view is written beside each page.
    `source_dpi` is the resolution given to every source that carries none, stored as
    an override.
    """
    if tone_view and not dry:
        from pagekit.pipeline import tone_view_available

        if not tone_view_available():
            raise PrepareError(
                "the grey tone view is not built into this copy of pagekit yet "
                "(pagekit/tone.py is missing), so it cannot be written; run without it"
            )
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
    cache_dir = None
    if values["stage_cache"]:
        folder = values["stage_cache_folder"]
        if folder:
            cache_dir = Path(folder).resolve()
        else:  # one cache per output folder, named after it
            cache_dir = output_dir.parent / f"{output_dir.name}.{CACHE_NAME}"
        if _inside(cache_dir, output_dir):
            raise PrepareError(
                f"the stage cache {cache_dir} would lie inside the output folder; choose a "
                "folder elsewhere (--cache)"
            )
        from pagekit.cache import owner

        belongs = owner(cache_dir)
        if belongs is not None and belongs != str(output_dir):
            raise PrepareError(
                f"the stage cache {cache_dir} belongs to the output folder {belongs}; give "
                "this output folder its own cache (--cache) so neither removes the other's"
            )
    for path in paths:
        if _inside(output_dir, path.parent) or _inside(project_folder, path.parent):
            raise PrepareError(
                f"pagekit never writes inside a source folder, and {path.parent} holds "
                f"{path.name}; choose an output folder and project file elsewhere"
            )
        if cache_dir is not None and _inside(cache_dir, path.parent):
            raise PrepareError(
                f"pagekit never writes inside a source folder, and {path.parent} holds "
                f"{path.name}; choose a stage cache folder elsewhere (--cache)"
            )
    # A source that cannot be used is skipped, not fatal: it gets no page and no new
    # project entry, so a later run tries it again. Only a run with none usable stops.
    sources, skipped = [], []
    for path in paths:
        try:
            sources.append(read_source(path, project_folder))
        except SourceError as error:
            skipped.append(_skipped(path, project_folder, error.reason))
    if not sources:
        raise PrepareError(
            "no source image can be used, so nothing was written:\n  "
            + "\n  ".join(f"{entry['name']}: {entry['reason']}" for entry in skipped)
        )
    skipped_paths = {(project_folder / entry["path"]).resolve() for entry in skipped}

    old_entries: dict[str, dict[str, Any]] = {}
    kept_entries: list[dict[str, Any]] = []
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
        # A skipped source keeps what the project held for it, untouched, so its
        # corrections are not lost while it cannot be read.
        kept_entries = [
            entry
            for path, entry in by_path.items()
            if path not in used and (project_folder / path).resolve() in skipped_paths
        ]
        used |= {entry["path"] for entry in kept_entries}
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
            if _names_skipped(override, skipped, overrides_path.parent, project_folder):
                continue  # kept in the file, applied once the source can be read
            source = _match_override(override, sources, overrides_path.parent)
            key = (override.step, override.page)
            if key in overrides[source.relative]:
                raise PrepareError(f"{override.where} repeats an override of the same step")
            overrides[source.relative][key] = override

    extension = "png" if values["output_format"] == "png" else "tif"
    bases = _output_names(sources, extension)
    runner = _Runner(settings, detectors, {}, dry, None)
    if source_dpi is not None:
        low, high = values["min_plausible_dpi"], values["max_plausible_dpi"]
        if not low <= source_dpi <= high:
            raise PrepareError(
                f"--dpi {source_dpi:g} is not a plausible scan resolution "
                f"({low:g} to {high:g} dots per inch)"
            )
        runner.source_dpi = float(source_dpi)
    if output_mode is not None:
        if output_mode not in OUTPUT_MODES:
            raise PrepareError(f"the output mode is source or grey, not {output_mode!r}")
        runner.run_mode = output_mode
    entries, pages = [], []
    for source in sources:
        trusted, _ = _tag_trust(
            old_entries.get(source.relative),
            overrides[source.relative].get(("tag_trust", None)),
            values,
        )
        if source.tag_on_open and not trusted:
            # The library turned it on open; an untrusted tag means the stored pixels.
            source = replace(
                source,
                undo_tag=source.tag_found,
                size=tagged_size(source.size, _UNDO_TAG[source.tag_found]),
            )
        cache: dict[str, Image.Image] = {}

        def load(source=source, cache=cache) -> Image.Image:
            if "image" not in cache:
                cache["image"] = source.open()
            return cache["image"]

        runner.overrides = overrides[source.relative]
        runner.image_loader = load
        runner.cache = cache
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
        "sources": sorted(entries + kept_entries, key=lambda entry: entry["path"]),
    }
    stale_outputs = sorted(
        dropped["output"]
        for entry in entries
        for dropped in entry["dropped_pages"]
        if (output_dir / dropped["output"]).is_file()
    )
    batch: dict[str, Any] = {}
    if not dry:
        from pagekit.volume import check_batch

        batch = check_batch(pages, values)
    return Plan(
        output_dir,
        project_path,
        project,
        pages,
        settings,
        runner.stale,
        stale_outputs,
        batch,
        tone_view,
        cache_dir=cache_dir,
        skipped=skipped,
    )
