"""Strict loading for the one configuration file that names models.

The schema accepts new role names without a code change. It does not accept a new
state, source, or omitted pin silently: those would turn a spelling error into a
different model answering under a familiar role.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .errors import ConfigurationRefusal
from .models import AbsentSeat, ModelsConfig, SeatIdentity, is_hf_revision, is_sha256

_TOP_LEVEL = {"witness_floor", "seats", "adapter_recipes", "model_root"}
_CONFIGURED_COMMON = {
    "state",
    "source",
    "digest_manifest",
    "manifest",
    "adapter_of",
    "serving_recipe",
    "license_note",
}


def load_models_toml(path: str | Path) -> ModelsConfig:
    """Read and validate a `models.toml` without resolving or fetching a model."""

    source_path = Path(path)
    try:
        with source_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationRefusal("models.toml", f"cannot read configuration: {error}") from error
    return parse_models_config(raw, source_path=source_path)


def parse_models_config(raw: Any, *, source_path: str | Path | None = None) -> ModelsConfig:
    """Validate parsed TOML. Exposed for fully offline tests and callers."""

    if not isinstance(raw, dict):
        raise ConfigurationRefusal("models.toml", "top level is not an object")
    unknown = sorted(set(raw) - _TOP_LEVEL)
    if unknown:
        raise ConfigurationRefusal("models.toml", f"unknown top-level field(s) {unknown}")

    witness_floor = raw.get("witness_floor")
    if not isinstance(witness_floor, int) or isinstance(witness_floor, bool) or witness_floor < 0:
        raise ConfigurationRefusal(
            "models.toml", "witness_floor must be a non-negative integer owned by this config"
        )

    raw_model_root = raw.get("model_root")
    model_root = None
    if raw_model_root is not None:
        model_root = _relative_posix("models.toml", "model_root", raw_model_root)

    adapter_recipes = _parse_adapter_recipes(raw.get("adapter_recipes", {}))
    raw_seats = raw.get("seats")
    if not isinstance(raw_seats, dict) or not raw_seats:
        raise ConfigurationRefusal("models.toml", "seats must be a non-empty table")

    seats: dict[str, SeatIdentity | AbsentSeat] = {}
    for role, values in raw_seats.items():
        _role(role)
        seats[role] = _parse_seat(role, values)

    if (
        any(
            isinstance(value, SeatIdentity) and value.source == "local-repository"
            for value in seats.values()
        )
        and model_root is None
    ):
        raise ConfigurationRefusal(
            "models.toml", "model_root is required when a local-repository seat is configured"
        )

    for role, value in seats.items():
        if not isinstance(value, SeatIdentity) or value.adapter_of is None:
            continue
        base = seats.get(value.adapter_of)
        if base is None:
            raise ConfigurationRefusal(
                role, f"adapter_of names no configured base seat {value.adapter_of!r}"
            )
        if isinstance(base, AbsentSeat):
            raise ConfigurationRefusal(
                role, f"adapter_of base {value.adapter_of!r} is explicitly absent"
            )
        if value.adapter_of == role:
            raise ConfigurationRefusal(role, "adapter_of cannot name the adapter seat itself")

    return ModelsConfig(
        witness_floor=witness_floor,
        seats=seats,
        adapter_recipes=adapter_recipes,
        model_root=model_root,
        source_path=Path(source_path) if source_path is not None else None,
    )


def _parse_seat(role: str, values: Any) -> SeatIdentity | AbsentSeat:
    if not isinstance(values, dict):
        raise ConfigurationRefusal(role, "seat declaration is not a table")
    state = values.get("state")
    if state == "absent":
        _only_keys(role, values, {"state", "reason"})
        return AbsentSeat(role=role, reason=_text(role, "reason", values.get("reason")))
    if state != "configured":
        raise ConfigurationRefusal(role, "state must be exactly 'configured' or 'absent'")

    source = values.get("source")
    if source not in ("huggingface", "local-repository"):
        raise ConfigurationRefusal(
            role, "configured seat source must be 'huggingface' or 'local-repository'"
        )
    allowed = _CONFIGURED_COMMON | ({"repo", "revision"} if source == "huggingface" else {"path"})
    _only_keys(role, values, allowed)
    required = _CONFIGURED_COMMON - {"adapter_of"}
    required |= {"repo", "revision"} if source == "huggingface" else {"path"}
    missing = sorted(field for field in required if field not in values)
    if missing:
        raise ConfigurationRefusal(role, f"configured seat is missing field(s) {missing}")

    digest = values["digest_manifest"]
    if not is_sha256(digest):
        raise ConfigurationRefusal(
            role, "digest_manifest must be exactly 64 lowercase hexadecimal characters"
        )
    manifest = _relative_posix(role, "manifest", values["manifest"])
    adapter_of = values.get("adapter_of")
    if adapter_of is not None:
        adapter_of = _role(adapter_of)

    if source == "huggingface":
        repo = _text(role, "repo", values["repo"])
        revision = values["revision"]
        if not is_hf_revision(revision):
            raise ConfigurationRefusal(
                role,
                "huggingface revision must be exactly 40 lowercase hexadecimal characters; "
                "a branch name is not a pin",
            )
        path = None
    else:
        repo = None
        path = _relative_posix(role, "path", values["path"])
        revision = None

    return SeatIdentity(
        role=role,
        source=source,
        repo=repo,
        path=path,
        revision=revision,
        digest_manifest=digest,
        manifest=manifest,
        adapter_of=adapter_of,
        serving_recipe=_text(role, "serving_recipe", values["serving_recipe"]),
        license_note=_text(role, "license_note", values["license_note"]),
    )


def _parse_adapter_recipes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigurationRefusal("models.toml", "adapter_recipes must be a table of strings")
    parsed: dict[str, str] = {}
    for name, recipe in value.items():
        parsed[_role(name)] = _text("adapter_recipes", str(name), recipe)
    return parsed


def _only_keys(role: str, values: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigurationRefusal(
            role, f"field(s) {unknown} are forbidden for this seat state/source"
        )


def _text(role: str, field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationRefusal(role, f"field {field!r} must be a non-blank string")
    return value


def _role(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ConfigurationRefusal(
            "models.toml", f"role {value!r} is blank or has surrounding whitespace"
        )
    if "/" in value or "\\" in value or ".." in value.split("_"):
        raise ConfigurationRefusal(
            "models.toml", f"role {value!r} is not a plain configuration key"
        )
    return value


def _relative_posix(role: str, field: str, value: Any) -> str:
    """A path under a configured root: relative, POSIX, and no way out of it.

    The `~` case is not an escape and is refused anyway. Nothing here calls
    `expanduser`, so `~/models` would resolve to a literal directory named `~`
    under the model root — a pin that plainly means the home directory, silently
    read as something else. A pin that cannot be read the way it was written is
    refused rather than reinterpreted.
    """
    text = _text(role, field, value)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text or text in (".", ""):
        raise ConfigurationRefusal(role, f"field {field!r} must be a safe relative POSIX path")
    if path.parts[0].startswith("~"):
        raise ConfigurationRefusal(
            role,
            f"field {field!r} starts with {path.parts[0]!r}; nothing here expands a home "
            "directory, so this pin would name a literal directory of that name",
        )
    return path.as_posix()
