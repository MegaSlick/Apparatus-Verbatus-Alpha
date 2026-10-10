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
from .filesystem import apfs_key
from .models import (
    AbsentChair,
    ChairIdentity,
    ModelsConfig,
    is_hf_revision,
    is_plain_role,
    is_sha256,
    is_witness_role,
)

_TOP_LEVEL = {
    "witness_floor",
    "chairs",
    "adapter_recipes",
    "witness_framings",
    "witness_routing",
    "model_root",
}
#: The page-routing rules a witness chair may be seated under
#: (`common/witness_routing.py` derives each page's decision). `index-and-table.v1`:
#: the chair reads a page when Surya's layout tags a `Table` block on it or the
#: record detector found no record on it, and no other page.
WITNESS_ROUTING_RULES = frozenset({"index-and-table.v1"})
_CONFIGURED_COMMON = {
    "state",
    "source",
    "digest_manifest",
    "manifest",
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
    witness_framings = _parse_witness_framings(raw.get("witness_framings", {}))
    witness_routing = _parse_witness_routing(raw.get("witness_routing", {}))
    raw_chairs = raw.get("chairs")
    if not isinstance(raw_chairs, dict) or not raw_chairs:
        raise ConfigurationRefusal("models.toml", "chairs must be a non-empty table")

    chairs: dict[str, ChairIdentity | AbsentChair] = {}
    for role, values in raw_chairs.items():
        _role(role)
        chairs[role] = _parse_chair(role, values)

    _refuse_case_variant_collisions(chairs)

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

    for role in witness_framings:
        chair = chairs.get(role)
        if not isinstance(chair, ChairIdentity):
            raise ConfigurationRefusal(
                "witness_framings",
                f"{role!r} names no configured chair, so nothing would ever be asked in the "
                "framing it declares",
            )
        if chair.witness_adapter is None:
            raise ConfigurationRefusal(
                "witness_framings",
                f"{role!r} is not a witness chair and has no adapter to be framed",
            )

    for role in witness_routing:
        chair = chairs.get(role)
        if not isinstance(chair, ChairIdentity) or chair.witness_adapter is None:
            raise ConfigurationRefusal(
                "witness_routing",
                f"{role!r} names no configured witness chair; only a configured witness can "
                "be seated on some pages and not others",
            )
    if witness_routing:
        unrouted = [
            role
            for role in chairs
            if is_witness_role(role)
            and isinstance(chairs[role], ChairIdentity)
            and role not in witness_routing
        ]
        if not unrouted:
            raise ConfigurationRefusal(
                "witness_routing",
                "routes every configured witness chair, so a page no rule routes would have "
                "no witness at all; leave at least one witness reading every page",
            )
        for needed in ("designator_surya", "secondary_proposer"):
            if not isinstance(chairs.get(needed), ChairIdentity):
                raise ConfigurationRefusal(
                    "witness_routing",
                    f"routes a witness by Surya's Table blocks and the record detector's count, "
                    f"but the {needed!r} chair that measures one of them is not configured",
                )

    return ModelsConfig(
        witness_floor=witness_floor,
        chairs=chairs,
        adapter_recipes=adapter_recipes,
        witness_framings=witness_framings,
        witness_routing=witness_routing,
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
    if "adapter_of" in values:
        raise ConfigurationRefusal(
            role,
            "adapter_of is not supported: every chair is served as a full checkpoint, "
            "and nothing serves or qualifies an adapter over a base",
        )

    source = values.get("source")
    if source not in ("huggingface", "local-repository"):
        raise ConfigurationRefusal(
            role, "configured chair source must be 'huggingface' or 'local-repository'"
        )
    allowed = _CONFIGURED_COMMON | ({"repo", "revision"} if source == "huggingface" else {"path"})
    _only_keys(role, values, allowed)
    required = _CONFIGURED_COMMON - {"witness_adapter", "witness_scope"}
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
        if witness_scope != "page":
            raise ConfigurationRefusal(
                role,
                f"declares invalid witness_scope {witness_scope!r}. Every witness reads whole "
                "pages. Set witness_scope to exactly 'page'",
            )

    return ChairIdentity(
        role=role,
        source=source,
        repo=repo,
        path=path,
        revision=revision,
        digest_manifest=digest,
        manifest=manifest,
        adapter_of=None,
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


def _parse_witness_framings(value: Any) -> dict[str, str]:
    """Which framing each witness chair is asked in, by role.

    The name is checked against the chair's own adapter where the adapters are
    known (`pipeline/3_attestatores/witness_adapters.py::
    validate_runnable_adapter_bindings`), not here: `common/` may not import a
    stage, and a spelling this file accepted but no adapter declares would then
    be refused before a run opens rather than at the first request.
    """

    if not isinstance(value, dict):
        raise ConfigurationRefusal("models.toml", "witness_framings must be a table of strings")
    parsed: dict[str, str] = {}
    for name, framing in value.items():
        parsed[_role(name)] = _text("witness_framings", str(name), framing)
    return parsed


def _parse_witness_routing(value: Any) -> dict[str, str]:
    """Which witness chairs read only the pages a rule routes to them, by role.

    Absent or empty, every configured witness reads every sealed page, as
    before; nothing about such a run changes.
    """

    if not isinstance(value, dict):
        raise ConfigurationRefusal("models.toml", "witness_routing must be a table of strings")
    parsed: dict[str, str] = {}
    for name, rule in value.items():
        text = _text("witness_routing", str(name), rule)
        if text not in WITNESS_ROUTING_RULES:
            raise ConfigurationRefusal(
                "witness_routing",
                f"{name!r} names rule {text!r}; the rules are {sorted(WITNESS_ROUTING_RULES)}",
            )
        parsed[_role(name)] = text
    return parsed


def _refuse_case_variant_collisions(
    chairs: Mapping[str, ChairIdentity | AbsentChair],
) -> None:
    """Refuse distinct spellings that alias on default APFS.

    Chair roles become cache directory names, manifests become files below the
    configuration root, and local paths become snapshot directories.  Exact
    sharing is deliberate and remains legal; two different spellings that differ
    only in case or Unicode normalization are ambiguous across supported
    filesystems.
    """

    _refuse_case_variants(((role, role) for role in chairs), "chair roles")
    configured = [
        (role, identity) for role, identity in chairs.items() if isinstance(identity, ChairIdentity)
    ]
    _refuse_case_variants(
        ((role, identity.manifest) for role, identity in configured), "manifest paths"
    )
    _refuse_case_variants(
        (
            (role, identity.path)
            for role, identity in configured
            if identity.source == "local-repository" and identity.path is not None
        ),
        "local-repository paths",
    )


def _refuse_case_variants(rows: Any, label: str) -> None:
    first_by_folded: dict[str, tuple[str, str]] = {}
    for role, spelling in rows:
        first = first_by_folded.setdefault(apfs_key(spelling), (role, spelling))
        if first[1] != spelling:
            raise ConfigurationRefusal(
                "models.toml",
                f"{label} alias on default APFS (case or Unicode normalization): "
                f"{first[0]!r} names {first[1]!r}, while {role!r} names {spelling!r}",
            )


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
    if not is_plain_role(value):
        raise ConfigurationRefusal(
            "models.toml",
            f"role {value!r} is not a plain configuration key: it holds a path "
            "separator or starts with '.'",
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
