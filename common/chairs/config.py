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
from .models import (
    AbsentChair,
    ChairIdentity,
    ModelsConfig,
    is_hf_revision,
    is_sha256,
    is_witness_role,
)

_TOP_LEVEL = {"witness_floor", "chairs", "adapter_recipes", "model_root"}
_CONFIGURED_COMMON = {
    "state",
    "source",
    "digest_manifest",
    "manifest",
    "adapter_of",
    "serving_recipe",
    "license_note",
    "witness_adapter",
    "witness_scope",
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
    raw_chairs = raw.get("chairs")
    if not isinstance(raw_chairs, dict) or not raw_chairs:
        raise ConfigurationRefusal("models.toml", "chairs must be a non-empty table")

    chairs: dict[str, ChairIdentity | AbsentChair] = {}
    for role, values in raw_chairs.items():
        _role(role)
        chairs[role] = _parse_chair(role, values)

    if (
        any(
            isinstance(value, ChairIdentity) and value.source == "local-repository"
            for value in chairs.values()
        )
        and model_root is None
    ):
        raise ConfigurationRefusal(
            "models.toml", "model_root is required when a local-repository chair is configured"
        )

    for role, value in chairs.items():
        if not isinstance(value, ChairIdentity) or value.adapter_of is None:
            continue
        base = chairs.get(value.adapter_of)
        if base is None:
            raise ConfigurationRefusal(
                role, f"adapter_of names no configured base chair {value.adapter_of!r}"
            )
        if isinstance(base, AbsentChair):
            raise ConfigurationRefusal(
                role, f"adapter_of base {value.adapter_of!r} is explicitly absent"
            )
        if value.adapter_of == role:
            raise ConfigurationRefusal(role, "adapter_of cannot name the adapter chair itself")
        # A self-reference is the one-step case of a cycle, and refusing only that let the
        # two-step case through: two chairs each declaring the other as its base were both
        # accepted, and neither pair member named a base artifact that exists. An adapter
        # is an adapter *of* something, so a chain that never reaches a non-adapter chair
        # is a roster with no base in it at all. Found by CodeRabbit on pull request 16.
        seen = [role]
        walker = value.adapter_of
        while walker is not None:
            if walker in seen:
                raise ConfigurationRefusal(
                    role, f"adapter_of forms a cycle {' -> '.join([*seen, walker])}"
                )
            seen.append(walker)
            step = chairs.get(walker)
            walker = step.adapter_of if isinstance(step, ChairIdentity) else None

    return ModelsConfig(
        witness_floor=witness_floor,
        chairs=chairs,
        adapter_recipes=adapter_recipes,
        model_root=model_root,
        source_path=Path(source_path) if source_path is not None else None,
    )


def _parse_chair(role: str, values: Any) -> ChairIdentity | AbsentChair:
    if not isinstance(values, dict):
        raise ConfigurationRefusal(role, "chair declaration is not a table")
    state = values.get("state")
    if state == "absent":
        _only_keys(role, values, {"state", "reason"})
        return AbsentChair(role=role, reason=_text(role, "reason", values.get("reason")))
    if state != "configured":
        raise ConfigurationRefusal(role, "state must be exactly 'configured' or 'absent'")

    source = values.get("source")
    if source not in ("huggingface", "local-repository"):
        raise ConfigurationRefusal(
            role, "configured chair source must be 'huggingface' or 'local-repository'"
        )
    allowed = _CONFIGURED_COMMON | ({"repo", "revision"} if source == "huggingface" else {"path"})
    _only_keys(role, values, allowed)
    required = _CONFIGURED_COMMON - {"adapter_of", "witness_adapter", "witness_scope"}
    required |= {"repo", "revision"} if source == "huggingface" else {"path"}
    missing = sorted(field for field in required if field not in values)
    if missing:
        raise ConfigurationRefusal(role, f"configured chair is missing field(s) {missing}")

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

    witness_adapter = values.get("witness_adapter")
    witness_scope = values.get("witness_scope")
    # Non-witness adapter rows would enter provenance and config_digest despite
    # naming a boundary that role never crosses. Refuse them at their source.
    if not is_witness_role(role) and (witness_adapter is not None or witness_scope is not None):
        raise ConfigurationRefusal(
            role,
            "declares witness_adapter or witness_scope on a non-Attestator chair. This role "
            "never invokes a native witness boundary. Remove both fields or move them to the "
            "intended [chairs.attestator_*] table",
        )
    if witness_adapter is None and witness_scope is not None:
        raise ConfigurationRefusal(
            role,
            "declares witness_scope without witness_adapter. Scope alone names no runnable "
            "native boundary. Add the exact witness_adapter name or remove witness_scope",
        )
    if witness_adapter is not None:
        witness_adapter = _text(role, "witness_adapter", witness_adapter)
        if witness_scope not in ("page", "act"):
            raise ConfigurationRefusal(
                role,
                f"declares invalid witness_scope {witness_scope!r}. The adapter cannot determine "
                "whether it runs once per page or once per act. Set witness_scope to exactly "
                "'page' or 'act'",
            )

    return ChairIdentity(
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
        witness_adapter=witness_adapter,
        witness_scope=witness_scope,
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
            role, f"field(s) {unknown} are forbidden for this chair state/source"
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
