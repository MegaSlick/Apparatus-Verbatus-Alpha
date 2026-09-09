"""Dependency-light validation for witness declarations and roster identities.

This module is safe before the pod's uv environment exists: it reads TOML and
the chair model contract, and imports no image, serving, or stage dependency.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.chairs.config import load_models_toml
from common.chairs.errors import ConfigurationRefusal
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig

_TRAINING_DOMAIN_FIELDS = {"training_domain"}
_SHIPPED_PROFILES = (
    ("shipped-fixture", "witness_context.toml", "models.toml"),
    ("shipped-real", "witness_context-real.toml", "models-real.toml"),
)


@dataclass(frozen=True, slots=True)
class WitnessContextValidation:
    """The declaration bytes and any shipped identity profile they matched."""

    source_sha256: str
    profile: str
    declared_roles: tuple[str, ...]
    verified_present_roles: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "profile": self.profile,
            "declared_roles": list(self.declared_roles),
            "verified_present_roles": list(self.verified_present_roles),
        }


def _read_declaration(path: Path) -> tuple[bytes, dict[str, dict[str, str]]]:
    try:
        source = path.read_bytes()
    except OSError as error:
        raise ConfigurationRefusal(
            "witness-context", f"declaration {path} could not be read: {error}"
        ) from error
    try:
        parsed: Any = tomllib.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationRefusal(
            "witness-context", f"declaration {path} could not be parsed: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise ConfigurationRefusal("witness-context", f"declaration {path} is not an object")
    for role, entry in sorted(parsed.items()):
        if (
            not isinstance(role, str)
            or not isinstance(entry, dict)
            or set(entry) != _TRAINING_DOMAIN_FIELDS
            or not isinstance(entry.get("training_domain"), str)
            or not entry["training_domain"].strip()
        ):
            raise ConfigurationRefusal(
                "witness-context",
                f"entry for {role!r} in {path} is not a closed table with only a non-blank "
                "training_domain",
            )
    return source, parsed


def _identity_projection(identity: ChairIdentity) -> dict[str, str]:
    return {
        "source": identity.source,
        "source_reference": identity.source_reference,
        "receipt_revision": identity.receipt_revision,
        "receipt_revision_kind": identity.receipt_revision_kind,
    }


def validate_witness_context_configuration(
    models: ModelsConfig,
    witness_context_path: str | Path,
    *,
    shipped_config_root: str | Path,
) -> WitnessContextValidation:
    """Validate coverage and bind either shipped declaration to its shipped identities.

    Comments, whitespace, and location do not select a profile: parsed semantic
    content does. A declaration unlike either shipped profile is operator-authored
    and remains accepted under the closed shape and coverage rules; this code has
    no basis for inferring the truth of arbitrary training-domain prose.
    """

    selected_path = Path(witness_context_path)
    source, declaration = _read_declaration(selected_path)
    expected_roles = set(models.witness_chairs)
    missing = sorted(expected_roles - set(declaration))
    if missing:
        raise ConfigurationRefusal(
            "witness-context",
            f"chair {missing[0]!r} has no declared entry in {selected_path}; every configured "
            "witness, including an explicit absence, must retain declaration coverage",
        )
    unaddressed = sorted(set(declaration) - expected_roles)
    if unaddressed:
        raise ConfigurationRefusal(
            "witness-context",
            f"{selected_path} declares {unaddressed[0]!r}, which is not a configured witness chair",
        )

    config_root = Path(shipped_config_root)
    matches: list[tuple[str, ModelsConfig]] = []
    for profile, declaration_name, roster_name in _SHIPPED_PROFILES:
        _, shipped_declaration = _read_declaration(config_root / declaration_name)
        shipped_roster = load_models_toml(config_root / roster_name)
        if set(shipped_declaration) != set(shipped_roster.witness_chairs):
            raise ConfigurationRefusal(
                "witness-context",
                f"shipped profile {profile!r} no longer has exact declaration coverage for "
                "its roster",
            )
        if declaration == shipped_declaration:
            matches.append((profile, shipped_roster))
    if len(matches) > 1:
        raise ConfigurationRefusal(
            "witness-context",
            "the shipped fixture and real declarations have identical semantic content; their "
            "identity profiles cannot be distinguished",
        )

    verified: list[str] = []
    profile = "operator-authored"
    if matches:
        profile, shipped_roster = matches[0]
        mismatches: list[str] = []
        for role in models.witness_chairs:
            selected = models.chairs[role]
            if isinstance(selected, AbsentChair):
                continue
            expected = shipped_roster.chairs.get(role)
            if not isinstance(selected, ChairIdentity) or not isinstance(expected, ChairIdentity):
                mismatches.append(
                    f"present witness {role!r} cannot be matched to the {profile} identity profile"
                )
                continue
            observed_identity = _identity_projection(selected)
            expected_identity = _identity_projection(expected)
            if observed_identity != expected_identity:
                mismatches.append(
                    f"present witness {role!r} does not match the {profile} identity projection: "
                    f"expected {expected_identity!r}, got {observed_identity!r}"
                )
                continue
            verified.append(role)
        if mismatches:
            raise ConfigurationRefusal(
                "witness-context",
                "; ".join(mismatches) + ". A local "
                "repository location alone does not prove a fixture identity, and a legitimate "
                "local mirror cannot be inferred to be a Hugging Face identity. Supply an "
                "operator-authored declaration whose parsed content describes this custom roster",
            )

    return WitnessContextValidation(
        source_sha256=hashlib.sha256(source).hexdigest(),
        profile=profile,
        declared_roles=tuple(sorted(declaration)),
        verified_present_roles=tuple(verified),
    )
