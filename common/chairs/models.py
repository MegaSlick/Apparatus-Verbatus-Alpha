"""Typed values exchanged by the model-chair protocol.

These are deliberately data-only values.  They do not discover a model, launch a
server, or choose an alternative: resolution and verification live in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from common.contracts.canonical import digest_of
from common.contracts.canonical import is_sha256 as is_sha256  # re-exported to this package


def is_hf_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def is_witness_role(role: object) -> bool:
    """Apply the single naming rule used to classify Attestator chairs."""

    return isinstance(role, str) and role.startswith("attestator_")


@dataclass(frozen=True, slots=True)
class ChairIdentity:
    """The exact configured artifact for one role, never an inferred replacement."""

    role: str
    source: str
    repo: str | None
    path: str | None
    revision: str | None
    digest_manifest: str
    manifest: str
    adapter_of: str | None
    serving_recipe: str
    license_note: str
    witness_adapter: str | None = None
    witness_scope: str | None = None

    @property
    def source_reference(self) -> str:
        """The configured repo or local path, with no resolution fallback."""

        return self.repo if self.source == "huggingface" else self.path or ""

    @property
    def receipt_revision(self) -> str:
        """A reproducibility revision for receipts.

        Hugging Face identities carry their exact Git commit. A local repository has
        no Git revision by contract, so its verified manifest hash is the immutable
        revision-equivalent. `receipt_revision_kind` keeps the distinction explicit.
        """

        return self.revision if self.source == "huggingface" else self.digest_manifest

    @property
    def receipt_revision_kind(self) -> str:
        return "git-commit" if self.source == "huggingface" else "digest-manifest"

    def cache_descriptor(self) -> dict[str, object]:
        """The immutable facts a cache must match before it can be used."""

        return {
            "role": self.role,
            "source": self.source,
            "repo": self.repo,
            "path": self.path,
            "revision": self.revision,
            "digest_manifest": self.digest_manifest,
            "manifest": self.manifest,
            "adapter_of": self.adapter_of,
        }

    def to_record(self) -> dict[str, object]:
        """A stable, serializable resolved identity for provenance."""

        return {
            **self.cache_descriptor(),
            "serving_recipe": self.serving_recipe,
            "license_note": self.license_note,
            "witness_adapter": self.witness_adapter,
            "witness_scope": self.witness_scope,
        }


@dataclass(frozen=True, slots=True)
class AbsentChair:
    """An explicit, roster-visible absence rather than an exception or skipped role."""

    role: str
    reason: str

    def to_record(self) -> dict[str, str]:
        return {"role": self.role, "state": "absent", "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ManifestRow:
    """One regular file covered by a digest-manifest artifact."""

    path: str
    sha256: str
    size: int

    def to_record(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class DigestManifest:
    """The canonical artifact whose digest is the configured pin."""

    rows: tuple[ManifestRow, ...]

    def to_record(self) -> list[dict[str, object]]:
        """The artifact is the bare, sorted row list the pin hashes."""

        return [row.to_record() for row in self.rows]


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    """A snapshot that has matched every row of its pinned manifest."""

    identity: ChairIdentity
    root: Path
    manifest_digest: str

    def to_record(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_record(),
            "root": str(self.root),
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class ServingDetails:
    """Facts observed from the serving manager when a model actually answered."""

    tokenizer_revision: str
    seed: int
    context_cap: int
    pixel_cap: int
    engine: str
    engine_version: str
    dtype: str
    adapter_identity: ChairIdentity | None
    endpoint: str
    started_at: str


@dataclass(frozen=True, slots=True)
class ServingReceipt:
    """A non-deterministic run receipt, never a stage artifact."""

    identity: ChairIdentity
    details: ServingDetails

    def to_record(self) -> dict[str, object]:
        adapter = self.details.adapter_identity
        return {
            "schema": "chair-serving-receipt.v1",
            "chair": self.identity.role,
            "source": self.identity.source,
            "resolved": self.identity.source_reference,
            "revision": self.identity.receipt_revision,
            "revision_kind": self.identity.receipt_revision_kind,
            "digest_manifest": self.identity.digest_manifest,
            "tokenizer_revision": self.details.tokenizer_revision,
            "seed": self.details.seed,
            "context_cap": self.details.context_cap,
            "pixel_cap": self.details.pixel_cap,
            "engine": self.details.engine,
            "engine_version": self.details.engine_version,
            "dtype": self.details.dtype,
            "adapter_identity": adapter.to_record() if adapter else None,
            "endpoint": self.details.endpoint,
            "started_at": self.details.started_at,
        }


@dataclass(frozen=True, slots=True)
class WitnessFloorStatus:
    """The count that makes explicit absences visible against the witness floor."""

    floor: int
    configured_roles: tuple[str, ...]
    absent_roles: tuple[str, ...]

    @property
    def configured_count(self) -> int:
        return len(self.configured_roles)

    @property
    def deficit(self) -> int:
        return max(0, self.floor - self.configured_count)

    @property
    def meets_floor(self) -> bool:
        return self.deficit == 0


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """Validated `models.toml`, including the run bindings it owns."""

    witness_floor: int
    chairs: Mapping[str, ChairIdentity | AbsentChair]
    adapter_recipes: Mapping[str, str] = field(default_factory=dict)
    model_root: str | None = None
    source_path: Path | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "chairs", MappingProxyType(dict(self.chairs)))
        object.__setattr__(self, "adapter_recipes", MappingProxyType(dict(self.adapter_recipes)))

    def to_record(self) -> dict[str, object]:
        chairs: dict[str, object] = {}
        for role, value in sorted(self.chairs.items()):
            chairs[role] = value.to_record()
        return {
            "witness_floor": self.witness_floor,
            "model_root": self.model_root,
            "adapter_recipes": dict(sorted(self.adapter_recipes.items())),
            "chairs": chairs,
        }

    @property
    def models_digest(self) -> str:
        """A canonical digest of the model configuration's run-shaping facts.

        A convenience digest no run binding consumes. `config_digest` covers the
        same ground by embedding `to_record()` itself, beside the fixture and the
        scenario, because a digest that dropped those would let the same run id be
        reopened under a different scenario; see
        `common/stage.py::run_config_bindings`.
        """

        return digest_of(self.to_record())

    @property
    def witness_chairs(self) -> tuple[str, ...]:
        """All Attestator roles, including explicit absences that stay in the roster."""

        return tuple(sorted(role for role in self.chairs if is_witness_role(role)))

    def witness_floor_status(self) -> WitnessFloorStatus:
        """Count configured Attestator chairs; explicit absences create a deficit."""

        configured: list[str] = []
        absent: list[str] = []
        for role, value in self.chairs.items():
            if not is_witness_role(role):
                continue
            if isinstance(value, AbsentChair):
                absent.append(role)
            else:
                configured.append(role)
        return WitnessFloorStatus(
            floor=self.witness_floor,
            configured_roles=tuple(sorted(configured)),
            absent_roles=tuple(sorted(absent)),
        )
