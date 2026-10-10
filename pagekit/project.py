"""The project file (`pagekit-project.v1`), the overrides file (`pagekit-overrides.v1`)
and the settings of `prepare`.

The project file holds, for every source image and every page cut from it, each step's
value, where it came from (origin), how sure the detector was (confidence), what
decided it (evidence), why to look at it (flags) and what it was computed from (inputs
and their sha256, the inputs hash). It is plain JSON with sorted keys, so the same
inputs and values give the same bytes, and it is written atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pagekit.answer import ORIGINS, PAGE_STEPS, SOURCE_STEPS, STEPS, AnswerError, validate_value

PROJECT_SCHEMA = "pagekit-project.v1"
OVERRIDES_SCHEMA = "pagekit-overrides.v1"
SETTINGS_PATH = Path(__file__).with_name("thresholds_prepare.toml")
OUTPUT_FORMATS = ("png", "tiff")
RESOLUTION_ORIGINS = ("file", "override", "missing")
SHA256 = re.compile(r"[0-9a-f]{64}")

_TOP_KEYS = {"schema", "tool", "settings", "sources"}
_SOURCE_KEYS = {
    "path",
    "sha256",
    "bytes",
    "size",
    "mode",
    "resolution",
    "flags",
    "steps",
    "pages",
    "dropped_pages",
}
_RESOLUTION_KEYS = {"value", "origin", "file_value"}
_PAGE_KEYS = {"page", "output", "steps"}
# Keys a project may leave out.
_SOURCE_OPTIONAL = frozenset({"orientation_tag"})
_TAG_KEYS = {"found", "trusted", "trust_origin", "applied", "transform"}
_TAG_OPTIONAL = frozenset({"applied_by", "grid"})
# Corrections that are not steps: for the whole source, and per page.
SOURCE_SETTINGS = ("resolution", "tag_trust")
PAGE_SETTINGS = ("output_mode", "density", "crop")
_PAGE_OPTIONAL = frozenset({"output_mode", "density", "crop"})
_MODE_KEYS = {"value", "set_by", "evidence"}
_RECORD_KEYS = {
    "value",
    "origin",
    "confidence",
    "evidence",
    "flags",
    "method",
    "inputs",
    "inputs_hash",
}
_INPUTS_KEYS = {"source_sha256", "page", "earlier_steps", "settings"}
_OVERRIDE_KEYS = {"source", "step", "page", "value", "lock", "evidence"}


class PrepareError(ValueError):
    """The inputs cannot be used as given; nothing is written."""


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
            raise PrepareError(f"unknown setting {name!r}")
        default = settings[name]["value"]
        if isinstance(default, str):
            if not isinstance(value, str):
                raise PrepareError(f"setting {name!r} must be text like {default!r}")
        elif isinstance(value, bool) or not isinstance(value, int | float):
            raise PrepareError(f"setting {name!r} must be a number like {default!r}")
        settings[name] = {"value": value, "status": "UNMEASURED", "source": "override"}
    value = {name: entry["value"] for name, entry in settings.items()}
    if value["output_format"] not in OUTPUT_FORMATS:
        raise PrepareError(f"output_format must be one of {', '.join(OUTPUT_FORMATS)}")
    for name in ("overlap_mm", "margin_mm", "margin_allowance_mm", "max_output_dpi"):
        if value[name] < 0:
            raise PrepareError(f"setting {name!r} must not be negative")
    if not 0 < value["min_plausible_dpi"] <= value["max_plausible_dpi"]:
        raise PrepareError("the plausible resolution range is empty")
    if value["paper_estimate_long_side_px"] < 16:
        raise PrepareError("paper_estimate_long_side_px must be at least 16")
    for name in _POSITIVE:
        if not value[name] > 0:
            raise PrepareError(f"setting {name!r} must be more than 0")
    if value["crop"] not in ("none", "page", "content"):
        raise PrepareError("crop must be none, page or content")
    for name in ("crop_detectors_when_off", "stage_cache", "stage_cache_full"):
        if value[name] not in (0, 1):
            raise PrepareError(f"{name} is 1 (on) or 0 (off)")
    if value["cache_preview_long_side_px"] < 32:
        raise PrepareError("cache_preview_long_side_px must be at least 32")
    if value["padding_mm"] < 0 or value["padding_px"] < 0:
        raise PrepareError("padding must not be negative")
    if value["padding_mm"] > 0 and value["padding_px"] > 0:
        raise PrepareError("give padding in millimetres or in pixels, not both")
    if int(value["padding_px"]) != value["padding_px"]:
        raise PrepareError("padding_px must be a whole number of pixels")
    if value["grey_rule"] not in ("luminance", "red", "green", "blue"):
        raise PrepareError("grey_rule must be luminance, red, green or blue")
    for name in (
        "colour_working_dpi",
        "colour_chroma_margin",
        "colour_min_area_mm2",
        "colour_noise_spread",
        "colour_speck_mm2",
    ):
        if not value[name] > 0:
            raise PrepareError(f"setting {name!r} must be more than 0")
    if value["trust_orientation_tag"] not in (0, 1):
        raise PrepareError("trust_orientation_tag must be 1 (trust) or 0 (do not)")
    if value["volume_min_pages"] < 3:
        raise PrepareError("volume_min_pages must be at least 3")
    if value["preview_long_side_px"] < 32:
        raise PrepareError("preview_long_side_px must be at least 32")
    return settings


# Settings that must be more than zero.
_POSITIVE = (
    "detector_working_dpi",
    "unknown_dpi_assumed",
    "compare_skew_deg",
    "compare_cut_mm",
    "compare_box_mm",
    "volume_outlier_distance",
    "volume_skew_floor_deg",
    "volume_size_floor_mm",
    "volume_margin_floor_mm",
    "measure_skew_tolerance_deg",
    "measure_cut_tolerance_mm",
    "measure_box_tolerance_mm",
)


def canonical_json(data: Any) -> str:
    """Sorted keys, fixed indentation, trailing newline: one text per value."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def digest(data: Any) -> str:
    """The sha256 of `data` as compact canonical JSON."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def step_inputs(
    source_sha256: str,
    page: int | None,
    earlier: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """What a step value is computed from, and its sha256 (the inputs hash).

    `page` is the page number counted from 1, or None for a step of the whole source;
    `earlier` the values of the earlier steps it depends on; `settings` everything else
    it reads.
    """
    inputs = {
        "source_sha256": source_sha256,
        "page": page,
        "earlier_steps": {step: digest(value) for step, value in sorted(earlier.items())},
        "settings": dict(sorted(settings.items())),
    }
    return inputs, digest(inputs)


def input_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Plain words for what differs between two step inputs."""
    changes = []
    if old["source_sha256"] != new["source_sha256"]:
        changes.append("the source file changed")
    if old["page"] != new["page"]:
        changes.append("the page number changed")
    steps = sorted(
        step
        for step in set(old["earlier_steps"]) | set(new["earlier_steps"])
        if old["earlier_steps"].get(step) != new["earlier_steps"].get(step)
    )
    if steps:
        changes.append("the earlier step " + ", ".join(steps) + " changed")
    settings = sorted(
        name
        for name in set(old["settings"]) | set(new["settings"])
        if old["settings"].get(name) != new["settings"].get(name)
    )
    if settings:
        changes.append("the setting " + ", ".join(settings) + " changed")
    return changes


def record(
    value: Any,
    origin: str,
    confidence: float | None,
    evidence: str,
    flags: list[str],
    method: str | None,
    inputs: dict[str, Any],
    inputs_hash: str,
) -> dict[str, Any]:
    """One step value in its stored form."""
    return {
        "value": value,
        "origin": origin,
        "confidence": confidence,
        "evidence": evidence,
        "flags": list(flags),
        "method": method,
        "inputs": inputs,
        "inputs_hash": inputs_hash,
    }


def write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` so a crash leaves either the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _closed(data: Any, keys: set[str], where: str, optional: frozenset = frozenset()) -> None:
    """`data` must hold exactly `keys`, plus any of `optional` (keys added later, which a
    project written before them does not hold)."""
    if not isinstance(data, dict):
        raise PrepareError(f"{where} must be an object")
    if not keys <= set(data) <= keys | optional:
        missing = sorted(keys - set(data))
        extra = sorted(set(data) - keys)
        raise PrepareError(f"{where} has missing keys {missing} and unknown keys {extra}")


def _check_tag(data: Any, where: str) -> None:
    _closed(data, _TAG_KEYS, where, _TAG_OPTIONAL)
    found = data["found"]
    if found is not None and (isinstance(found, bool) or not isinstance(found, int)):
        raise PrepareError(f"{where}: found is the tag's whole-number value or null")
    if not isinstance(data["trusted"], bool) or not isinstance(data["applied"], bool):
        raise PrepareError(f"{where}: trusted and applied are true or false")
    if data["trust_origin"] not in ("setting", "override"):
        raise PrepareError(f"{where}: trust_origin is setting or override")


def _check_mode(data: Any, where: str) -> None:
    _closed(data, _MODE_KEYS, where)
    if data["value"] not in ("source", "grey"):
        raise PrepareError(f"{where}: the output mode is source or grey")
    if data["set_by"] not in ("run", "manual", "locked"):
        raise PrepareError(f"{where}: set_by is run, manual or locked")
    if not isinstance(data["evidence"], str) or not data["evidence"]:
        raise PrepareError(f"{where}: evidence must be a sentence")


def _sentences(data: Any, where: str) -> None:
    if not isinstance(data, list) or not all(isinstance(item, str) and item for item in data):
        raise PrepareError(f"{where} must be a list of sentences")


def _check_record(data: Any, step: str, where: str) -> None:
    _closed(data, _RECORD_KEYS, where)
    try:
        if validate_value(step, data["value"]) != data["value"]:
            raise AnswerError("the value is not in its stored form")
    except AnswerError as error:
        raise PrepareError(f"{where}: {error}") from error
    if data["origin"] not in ORIGINS:
        raise PrepareError(f"{where}: origin must be one of {', '.join(ORIGINS)}")
    detected = data["origin"] == "detected"
    confidence = data["confidence"]
    if detected:
        is_number = isinstance(confidence, int | float) and not isinstance(confidence, bool)
        valid = is_number and 0 <= confidence <= 1
    else:
        valid = confidence is None
    if not valid:
        raise PrepareError(f"{where}: a detected value has a confidence from 0 to 1, others none")
    if detected != isinstance(data["method"], str):
        raise PrepareError(f"{where}: a detected value names its method, others none")
    if not isinstance(data["evidence"], str) or not data["evidence"]:
        raise PrepareError(f"{where}: evidence must be a sentence")
    _sentences(data["flags"], f"{where} flags")
    _closed(data["inputs"], _INPUTS_KEYS, f"{where} inputs")
    if digest(data["inputs"]) != data["inputs_hash"]:
        raise PrepareError(f"{where}: the inputs hash does not match the inputs")


def validate_project(data: Any) -> dict[str, Any]:
    """`data` if it is a well-formed `pagekit-project.v1`, else PrepareError."""
    _closed(data, _TOP_KEYS, "the project")
    if data["schema"] != PROJECT_SCHEMA:
        raise PrepareError(f"the project's schema is {data['schema']!r}, not {PROJECT_SCHEMA!r}")
    _closed(data["tool"], {"name", "version"}, "the project's tool")
    if not isinstance(data["settings"], dict):
        raise PrepareError("the project's settings must be an object")
    for name, entry in data["settings"].items():
        _closed(entry, {"value", "status", "source"}, f"setting {name!r}")
    if not isinstance(data["sources"], list):
        raise PrepareError("the project's sources must be a list")
    for index, source in enumerate(data["sources"]):
        where = f"project source {index + 1}"
        _closed(source, _SOURCE_KEYS, where, _SOURCE_OPTIONAL)
        if "orientation_tag" in source:
            _check_tag(source["orientation_tag"], f"{where} orientation_tag")
        if not isinstance(source["path"], str) or not SHA256.fullmatch(str(source["sha256"])):
            raise PrepareError(f"{where}: path and sha256 are required")
        _check_resolution(source["resolution"], f"{where} resolution")
        _sentences(source["flags"], f"{where} flags")
        _closed(source["steps"], set(SOURCE_STEPS), f"{where} steps")
        for step in SOURCE_STEPS:
            _check_record(source["steps"][step], step, f"{where} {step}")
        if not isinstance(source["pages"], list):
            raise PrepareError(f"{where}: pages must be a list")
        for number, page in enumerate(source["pages"], start=1):
            _closed(page, _PAGE_KEYS, f"{where} page {number}", _PAGE_OPTIONAL)
            if "output_mode" in page:
                _check_mode(page["output_mode"], f"{where} page {number} output_mode")
            if "crop" in page:
                _closed(page["crop"], _MODE_KEYS, f"{where} page {number} crop")
                if page["crop"]["value"] not in ("none", "page", "content") or page["crop"][
                    "set_by"
                ] not in ("manual", "locked"):
                    raise PrepareError(
                        f"{where} page {number} crop: none, page or content, by hand"
                    )
            if "density" in page:
                try:
                    _resolution(page["density"])
                except AnswerError as error:
                    raise PrepareError(f"{where} page {number} density: {error}") from error
            if page["page"] != number:
                raise PrepareError(f"{where}: pages must be numbered 1, 2 in order")
            _closed(page["steps"], set(PAGE_STEPS), f"{where} page {number} steps")
            for step in PAGE_STEPS:
                _check_record(page["steps"][step], step, f"{where} page {number} {step}")
        if not isinstance(source["dropped_pages"], list):
            raise PrepareError(f"{where}: dropped_pages must be a list")
        for page in source["dropped_pages"]:
            _closed(page, _PAGE_KEYS, f"{where} dropped page", _PAGE_OPTIONAL)
            number = page["page"]
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number <= len(source["pages"])
            ):
                raise PrepareError(f"{where}: a dropped page must be past the last page")
            if not isinstance(page["output"], str) or not isinstance(page["steps"], dict):
                raise PrepareError(f"{where} dropped page {number}: output and steps required")
            for step, entry in page["steps"].items():
                if step not in PAGE_STEPS:
                    raise PrepareError(f"{where} dropped page {number}: unknown step {step!r}")
                _check_record(entry, step, f"{where} dropped page {number} {step}")
                if entry["origin"] == "detected":
                    raise PrepareError(f"{where} dropped page {number}: only hand-set values")
    return data


def _check_resolution(data: Any, where: str) -> None:
    _closed(data, _RESOLUTION_KEYS, where)
    if data["origin"] not in RESOLUTION_ORIGINS:
        raise PrepareError(f"{where}: unknown origin {data['origin']!r}")
    if (data["value"] is None) != (data["origin"] == "missing"):
        raise PrepareError(f"{where}: a value is missing exactly when its origin is missing")
    for name in ("value", "file_value"):
        if data[name] is not None:
            try:
                _resolution(data[name])
            except AnswerError as error:
                raise PrepareError(f"{where} {name}: {error}") from error


def load_project(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PrepareError(f"the project file {path} does not exist") from error
    except (OSError, ValueError) as error:
        raise PrepareError(f"the project file {path} cannot be read: {error}") from error
    return validate_project(data)


@dataclass(frozen=True)
class Override:
    """One correction from an overrides file."""

    source: str  # a path (relative to the overrides file) or a sha256
    step: str  # one of STEPS, or "resolution"
    page: int | None  # counted from 1, for a per-page step
    value: Any
    lock: bool
    evidence: str
    where: str  # "entry N of FILE", for messages


def load_overrides(path: Path) -> list[Override]:
    """The corrections in a `pagekit-overrides.v1` file, checked for shape only."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PrepareError(f"the overrides file {path} does not exist") from error
    except (OSError, ValueError) as error:
        raise PrepareError(f"the overrides file {path} cannot be read: {error}") from error
    _closed(data, {"schema", "overrides"}, "the overrides file")
    if data["schema"] != OVERRIDES_SCHEMA:
        raise PrepareError(f"the overrides file's schema must be {OVERRIDES_SCHEMA!r}")
    if not isinstance(data["overrides"], list):
        raise PrepareError("the overrides file's overrides must be a list")
    overrides = []
    seen = set()
    for index, entry in enumerate(data["overrides"], start=1):
        where = f"override {index} in {path.name}"
        if not isinstance(entry, dict) or not {"source", "step", "value"} <= set(entry):
            raise PrepareError(f"{where} must name a source, a step and a value")
        unknown = sorted(set(entry) - _OVERRIDE_KEYS)
        if unknown:
            raise PrepareError(f"{where} has unknown keys {unknown}")
        source, step = entry["source"], entry["step"]
        if not isinstance(source, str) or not source:
            raise PrepareError(f"{where}: source must be a path or a sha256")
        if step not in STEPS + SOURCE_SETTINGS + PAGE_SETTINGS:
            others = ", ".join(SOURCE_SETTINGS + PAGE_SETTINGS)
            raise PrepareError(
                f"{where}: there is no step {step!r}; the steps are {', '.join(STEPS)}, "
                f"and the other corrections are {others}"
            )
        page = entry.get("page")
        if step in PAGE_STEPS + PAGE_SETTINGS:
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise PrepareError(f"{where}: the {step} step is per page, so name a page (1, 2)")
        elif page is not None:
            raise PrepareError(f"{where}: the {step} step is for the whole source; drop 'page'")
        lock = entry.get("lock", False)
        if not isinstance(lock, bool) or (lock and step in (*SOURCE_SETTINGS, "density")):
            raise PrepareError(
                f"{where}: lock must be true or false, and is not for {', '.join(SOURCE_SETTINGS)}"
            )
        evidence = entry.get("evidence", f"Set by hand in {path.name}.")
        if not isinstance(evidence, str) or not evidence.strip():
            raise PrepareError(f"{where}: evidence must be a sentence")
        value = entry["value"]
        try:
            value = _setting_value(step, value)
        except AnswerError as error:
            raise PrepareError(f"{where}: {error}") from error
        key = (source, step, page)
        if key in seen:
            raise PrepareError(f"{where} repeats an earlier override of the same step")
        seen.add(key)
        overrides.append(Override(source, step, page, value, lock, evidence, where))
    return overrides


def _setting_value(step: str, value: Any) -> Any:
    """An override's value in its stored form, or AnswerError."""
    if step == "resolution":
        return _resolution(value)
    if step == "density":
        return _resolution(value)
    if step == "crop":
        if value not in ("none", "page", "content"):
            raise AnswerError("crop is none (keep the whole side), page or content")
        return value
    if step == "output_mode":
        if value not in ("source", "grey"):
            raise AnswerError("output_mode is source (as scanned) or grey")
        return value
    if step == "tag_trust":
        if not isinstance(value, bool):
            raise AnswerError("tag_trust is true (apply the file's orientation tag) or false")
        return value
    return validate_value(step, value)


def _resolution(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise AnswerError("a resolution is [x_dpi, y_dpi]")
    for part in value:
        if isinstance(part, bool) or not isinstance(part, int | float) or not part > 0:
            raise AnswerError("a resolution is two positive numbers of dots per inch")
    return [float(value[0]), float(value[1])]
