"""Dependency-light validation for witness declarations and roster identities.

This module is safe before the pod's uv environment exists: it reads TOML and
the chair model contract, and imports no image, serving, or stage dependency.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.chairs.config import load_models_toml
from common.chairs.errors import ConfigurationRefusal
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig
from common.contracts.canonical import digest_bytes

_TRAINING_DOMAIN_FIELDS = {"training_domain"}
_SHIPPED_PROFILES = (
    ("shipped-fixture", "witness_context.toml", "models.toml"),
    ("shipped-real", "witness_context-real.toml", "models-real.toml"),
)


def _comparable_sentence(value: str) -> str:
    """Normalize whitespace and casing only for recognized-sentence comparison."""

    return " ".join(value.split()).casefold()


@dataclass(frozen=True, slots=True)
class WitnessContextValidation:
    """The declaration bytes and role-level shipped identity profiles they matched."""

    source_sha256: str
    profile: str
    declared_roles: tuple[str, ...]
    verified_present_roles: tuple[str, ...]
    role_profiles: tuple[tuple[str, str], ...]

    def to_record(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "profile": self.profile,
            "declared_roles": list(self.declared_roles),
            "verified_present_roles": list(self.verified_present_roles),
            "role_profiles": dict(self.role_profiles),
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
    """Validate coverage and bind each known shipped sentence to its shipped identity.

    Comments, location, TOML formatting, and whitespace runs inside the value do
    not disguise a known sentence. Each genuinely different role entry remains
    operator-authored under the closed shape and coverage rules; this code has no
    basis for inferring the truth of arbitrary training-domain prose.
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
    shipped_profiles: list[tuple[str, dict[str, dict[str, str]], ModelsConfig]] = []
    for profile, declaration_name, roster_name in _SHIPPED_PROFILES:
        _, shipped_declaration = _read_declaration(config_root / declaration_name)
        shipped_roster = load_models_toml(config_root / roster_name)
        if set(shipped_declaration) != set(shipped_roster.witness_chairs):
            raise ConfigurationRefusal(
                "witness-context",
                f"shipped profile {profile!r} no longer has exact declaration coverage for "
                "its roster",
            )
        shipped_profiles.append((profile, shipped_declaration, shipped_roster))

    verified: list[str] = []
    role_profiles: list[tuple[str, str]] = []
    mismatches: list[str] = []
    for role in models.witness_chairs:
        selected_sentence = _comparable_sentence(declaration[role]["training_domain"])
        known_matches = [
            (profile, shipped_roster)
            for profile, shipped_declaration, shipped_roster in shipped_profiles
            if role in shipped_declaration
            and selected_sentence
            == _comparable_sentence(shipped_declaration[role]["training_domain"])
        ]
        if len(known_matches) > 1:
            raise ConfigurationRefusal(
                "witness-context",
                f"the known shipped sentence for {role!r} names more than one identity profile; "
                "the profiles cannot be distinguished",
            )
        if not known_matches:
            role_profiles.append((role, "operator-authored"))
            continue

        role_profile, shipped_roster = known_matches[0]
        role_profiles.append((role, role_profile))
        selected = models.chairs[role]
        if isinstance(selected, AbsentChair):
            continue
        expected = shipped_roster.chairs.get(role)
        if not isinstance(selected, ChairIdentity) or not isinstance(expected, ChairIdentity):
            mismatches.append(
                f"present witness {role!r} cannot be matched to the {role_profile} identity profile"
            )
            continue
        observed_identity = _identity_projection(selected)
        expected_identity = _identity_projection(expected)
        if observed_identity != expected_identity:
            mismatches.append(
                f"present witness {role!r} does not match the {role_profile} identity projection: "
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

    distinct_profiles = {role_profile for _, role_profile in role_profiles}
    if not distinct_profiles:
        # The shared roster contract permits non-witness roles alone. This
        # records that absence without inventing authorship or a new roster rule;
        # real ingress separately requires a served witness.
        profile = "no-witness-roles"
    elif len(distinct_profiles) == 1:
        profile = next(iter(distinct_profiles))
    else:
        profile = "mixed"

    return WitnessContextValidation(
        source_sha256=digest_bytes(source),
        profile=profile,
        declared_roles=tuple(sorted(declaration)),
        verified_present_roles=tuple(verified),
        role_profiles=tuple(role_profiles),
    )
