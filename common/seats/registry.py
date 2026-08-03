"""Resolution and verification for named model seats, with no substitution path.

Every stored reading carries the resolved identity and revision of the model that produced it, at the moment it was produced.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from common.contracts.canonical import canonical_bytes, digest_bytes

from .config import load_models_toml
from .errors import (
    AdapterFetchRefusal,
    CacheRevisionRefusal,
    DigestMismatchRefusal,
    LocalPathRefusal,
    SeatRefusal,
    ServingRecipeRefusal,
    UnresolvedSeatRefusal,
)
from .manifests import read_manifest, verify_snapshot
from .models import (
    AbsentSeat,
    DigestManifest,
    ModelsConfig,
    SeatIdentity,
    ServingDetails,
    ServingReceipt,
    VerifiedSnapshot,
)
from .receipts import build_receipt

CACHE_DESCRIPTOR = ".seat-identity.json"


class SnapshotFetcher(Protocol):
    """The one deliberately small seam for network fetches; tests provide a fake."""

    def fetch(self, identity: SeatIdentity, destination: Path, paths: tuple[str, ...]) -> None:
        """Materialize exactly `paths` beneath `destination`, or raise."""


class HuggingFaceClient(Protocol):
    """The subset of `huggingface_hub` used by the adapter, kept mockable."""

    def snapshot_download(self, **kwargs: object) -> object:
        """Download an explicitly pinned snapshot subset."""


class HuggingFaceFetcher:
    """Adapter over an injected Hugging Face client; no import-time network dependency."""

    def __init__(self, client: HuggingFaceClient):
        self.client = client

    @classmethod
    def from_huggingface_hub(cls) -> "HuggingFaceFetcher":
        """Construct the production adapter from the installed official client.

        Importing lazily keeps offline config/receipt tests independent of the
        optional runtime package, while production deliberately uses the declared
        `huggingface_hub.snapshot_download` seam.
        """

        try:
            from importlib import import_module

            client = import_module("huggingface_hub")
        except ImportError as error:
            raise UnresolvedSeatRefusal(
                "huggingface", "huggingface_hub is not installed for the production fetcher"
            ) from error
        return cls(client)  # type: ignore[arg-type]

    def fetch(self, identity: SeatIdentity, destination: Path, paths: tuple[str, ...]) -> None:
        if identity.source != "huggingface" or not identity.repo or not identity.revision:
            raise UnresolvedSeatRefusal(
                identity.role, "Hugging Face fetch requested for a non-Hugging Face pin"
            )
        downloaded = self.client.snapshot_download(
            repo_id=identity.repo,
            revision=identity.revision,
            allow_patterns=list(paths),
        )
        source = Path(str(downloaded))
        for relative in paths:
            origin = source / relative
            if not origin.is_file():
                raise UnresolvedSeatRefusal(
                    identity.role,
                    f"Hugging Face fetch returned no requested file {relative!r} for the pinned revision",
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)


class SeatRegistry:
    """Resolve only the requested role, then verify only its pinned artifact.

    GOVERNANCE 6 applies to the values returned here as well as their consumers.
    """

    def __init__(
        self,
        config: ModelsConfig,
        *,
        manifest_root: str | Path | None = None,
        cache_root: str | Path | None = None,
        fetcher: SnapshotFetcher | None = None,
    ):
        self.config = config
        if manifest_root is None and config.source_path is not None:
            manifest_root = config.source_path.parent
        self.manifest_root = Path(manifest_root).resolve() if manifest_root is not None else None
        self.cache_root = Path(cache_root).resolve() if cache_root is not None else None
        self.fetcher = fetcher

    @classmethod
    def from_toml(
        cls,
        path: str | Path,
        *,
        cache_root: str | Path | None = None,
        fetcher: SnapshotFetcher | None = None,
    ) -> "SeatRegistry":
        return cls(load_models_toml(path), cache_root=cache_root, fetcher=fetcher)

    def resolve(self, role: str) -> SeatIdentity | AbsentSeat:
        """Return that role's exact identity or explicit absence, never another role."""

        value = self.config.seats.get(role)
        if value is None:
            raise UnresolvedSeatRefusal(role, "role is not present in models.toml")
        return value

    def ensure(self, identity: SeatIdentity) -> VerifiedSnapshot:
        """Fetch missing pinned files only, then verify the complete exact snapshot."""

        self._require_current_identity(identity)
        manifest = self._manifest(identity)
        if identity.source == "local-repository":
            return verify_snapshot(identity, self._resolve_local_path(identity), manifest)
        return self._ensure_huggingface(identity, manifest)

    def receipt(self, identity: SeatIdentity, serving: ServingDetails) -> ServingReceipt:
        """Validate a run-receipt value; writing it belongs to the run receipt writer."""

        self._require_current_identity(identity)
        return build_receipt(identity, serving)

    def refuse_recipe_start(self, identity: SeatIdentity, difference: str) -> None:
        """Represent a serving-manager start failure without offering another recipe."""

        self._require_current_identity(identity)
        raise ServingRecipeRefusal(identity.role, difference)

    def _require_current_identity(self, identity: SeatIdentity) -> None:
        configured = self.resolve(identity.role)
        if isinstance(configured, AbsentSeat):
            raise UnresolvedSeatRefusal(
                identity.role, f"seat is explicitly absent: {configured.reason}"
            )
        if configured != identity:
            raise UnresolvedSeatRefusal(
                identity.role,
                "identity differs from the configured pin; ensure and receipt never accept a neighbouring revision",
            )

    def _manifest(self, identity: SeatIdentity) -> DigestManifest:
        if self.manifest_root is None:
            raise UnresolvedSeatRefusal(identity.role, "no manifest root was supplied")
        path = _under_root(self.manifest_root, identity.manifest, identity.role, "manifest")
        return read_manifest(path, expected_digest=identity.digest_manifest, seat=identity.role)

    def _resolve_local_path(self, identity: SeatIdentity) -> Path:
        if self.config.model_root is None:
            raise LocalPathRefusal(
                identity.role, "no model_root is configured for local-repository seat"
            )
        base = (
            self.config.source_path.parent
            if self.config.source_path is not None
            else self.manifest_root
        )
        if base is None:
            raise LocalPathRefusal(
                identity.role, "no models.toml parent is available for model_root"
            )
        model_root = _under_root(base, self.config.model_root, identity.role, "model_root")
        return resolve_local_path(identity, model_root)

    def _ensure_huggingface(
        self, identity: SeatIdentity, manifest: DigestManifest
    ) -> VerifiedSnapshot:
        if self.cache_root is None:
            raise UnresolvedSeatRefusal(
                identity.role, "no cache_root was supplied for Hugging Face seat"
            )
        if "/" in identity.role or "\\" in identity.role or identity.role in ("", ".", ".."):
            raise CacheRevisionRefusal(identity.role, "role is unsafe as a cache path")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        target = self.cache_root / identity.role
        descriptor = self._cache_descriptor(identity)
        missing: tuple[str, ...]
        if target.exists():
            _verify_cache_descriptor(target, identity.role, descriptor)
            missing = _missing_files(identity, target, manifest)
            if not missing:
                return verify_snapshot(
                    identity, target, manifest, ignored_paths=(CACHE_DESCRIPTOR,)
                )
        else:
            missing = tuple(row.path for row in manifest.rows)

        if self.fetcher is None:
            raise UnresolvedSeatRefusal(
                identity.role, "no fetcher is configured for a missing pinned snapshot"
            )
        candidate = Path(
            tempfile.mkdtemp(prefix=f".{identity.role}.candidate-", dir=self.cache_root)
        )
        try:
            if target.exists():
                _copy_existing_files(target, candidate, manifest)
            try:
                self.fetcher.fetch(identity, candidate, missing)
            except SeatRefusal:
                raise
            except Exception as error:
                refusal = AdapterFetchRefusal if identity.adapter_of else UnresolvedSeatRefusal
                raise refusal(identity.role, f"pinned fetch failed: {error}") from error
            verified = verify_snapshot(identity, candidate, manifest)
            _write_cache_descriptor(candidate, descriptor)
            _promote(candidate, target)
            return VerifiedSnapshot(
                identity=verified.identity,
                root=target.resolve(),
                manifest_digest=verified.manifest_digest,
            )
        except Exception:
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            raise

    def _cache_descriptor(self, identity: SeatIdentity) -> dict[str, object]:
        """Bind an adapter cache to its configured base identity as well.

        An adapter's own pin is insufficient when its ``adapter_of`` role is
        repinned.  The role alone must not let an old adapter cache masquerade as
        compatible with a new base.  Resolving the named base is configuration
        lookup only; it never fetches, serves, ranks, or substitutes that base.
        """
        descriptor = identity.cache_descriptor()
        if identity.adapter_of is None:
            return descriptor
        base = self.resolve(identity.adapter_of)
        if isinstance(base, AbsentSeat):
            raise CacheRevisionRefusal(
                identity.role,
                f"adapter base {identity.adapter_of!r} is explicitly absent",
            )
        descriptor["adapter_base_identity"] = base.to_record()
        return descriptor


def resolve_local_path(identity: SeatIdentity, model_root: str | Path) -> Path:
    """Resolve a local seat under model_root and refuse traversal or symlink escape."""

    if identity.source != "local-repository" or not identity.path:
        raise LocalPathRefusal(identity.role, "local path requested for a non-local identity")
    root = Path(model_root).resolve()
    if not root.is_dir():
        raise LocalPathRefusal(identity.role, f"model_root {root} is not a directory")
    candidate = root / identity.path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise LocalPathRefusal(
            identity.role, f"local path {identity.path!r} cannot resolve: {error}"
        ) from error
    if not resolved.is_relative_to(root):
        raise LocalPathRefusal(identity.role, f"local path {identity.path!r} escapes model_root")
    if not resolved.is_dir():
        raise LocalPathRefusal(
            identity.role, f"local path {identity.path!r} is not a snapshot directory"
        )
    return resolved


def _under_root(root: Path, relative: str, seat: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise LocalPathRefusal(seat, f"{label} {relative!r} escapes its configured root")
    return candidate


def _verify_cache_descriptor(target: Path, seat: str, expected: dict[str, object]) -> None:
    descriptor = target / CACHE_DESCRIPTOR
    try:
        actual = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CacheRevisionRefusal(
            seat, f"cache has no readable identity descriptor: {error}"
        ) from error
    if actual != expected:
        raise CacheRevisionRefusal(
            seat,
            "cache descriptor differs from the configured pin; a cache never supplies a revision",
        )


def _write_cache_descriptor(target: Path, descriptor: dict[str, object]) -> None:
    (target / CACHE_DESCRIPTOR).write_bytes(canonical_bytes(descriptor))


def _missing_files(
    identity: SeatIdentity, target: Path, manifest: DigestManifest
) -> tuple[str, ...]:
    expected = {row.path: row for row in manifest.rows}
    actual: dict[str, Path] = {}
    for path in sorted(target.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(target).as_posix()
        if relative == CACHE_DESCRIPTOR:
            continue
        if path.is_symlink() or not path.is_file():
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: not a regular file"
            )
        actual[relative] = path
    missing: list[str] = []
    for relative in sorted(set(expected) | set(actual)):
        row = expected.get(relative)
        path = actual.get(relative)
        if row is None:
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: extra file"
            )
        if path is None:
            missing.append(relative)
            continue
        if path.stat().st_size != row.size or digest_bytes(path.read_bytes()) != row.sha256:
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: cached bytes do not match"
            )
    return tuple(missing)


def _copy_existing_files(target: Path, candidate: Path, manifest: DigestManifest) -> None:
    for row in manifest.rows:
        source = target / row.path
        if not source.exists():
            continue
        destination = candidate / row.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _promote(candidate: Path, target: Path) -> None:
    """Swap only a fully verified candidate, keeping a prior cache on failure."""

    backup = target.parent / f".{target.name}.prior-{os.getpid()}"
    had_target = target.exists()
    if had_target:
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(target, backup)
    try:
        os.replace(candidate, target)
    except Exception:
        if had_target and backup.exists():
            os.replace(backup, target)
        raise
    if had_target and backup.exists():
        shutil.rmtree(backup)
