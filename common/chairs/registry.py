"""Resolution and verification for named model chairs, with no substitution path.

Every stored reading carries the resolved identity and revision of the model that produced it, at the moment it was produced.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import huggingface_hub

from common.contracts.canonical import canonical_bytes

from .config import load_models_toml
from .errors import (
    CacheRevisionRefusal,
    ChairRefusal,
    ConfigurationRefusal,
    DigestMismatchRefusal,
    DiskSpaceRefusal,
    LocalPathRefusal,
    ServingRecipeRefusal,
    UnresolvedChairRefusal,
)
from .filesystem import apfs_alias
from .manifests import inspect_snapshot_for_repair, read_manifest, verify_snapshot
from .models import (
    AbsentChair,
    ChairIdentity,
    DigestManifest,
    ModelsConfig,
    ServingDetails,
    ServingReceipt,
    VerifiedSnapshot,
    is_sha256,
)
from .receipts import build_receipt

CACHE_DESCRIPTOR = ".chair-identity.json"
# Caches are keyed by manifest digest, so chairs pinned to the same bytes share
# one copy: `cache_root/by-digest/<digest_manifest>`.
DIGEST_CACHE_DIRECTORY = "by-digest"
# The real roster must be parseable before materialization, but no verification,
# receipt, or serving path may treat this placeholder as a pin.
PRE_MATERIALIZATION_SENTINEL = "0" * 64


class SnapshotFetcher(Protocol):
    """The one deliberately small seam for network fetches; tests provide a fake."""

    def fetch(self, identity: ChairIdentity, destination: Path, paths: tuple[str, ...]) -> None:
        """Materialize exactly `paths` beneath `destination`, or raise."""


class HuggingFaceClient(Protocol):
    """The subset of `huggingface_hub` used by the adapter, kept mockable."""

    def snapshot_download(self, **kwargs: object) -> object:
        """Download an explicitly pinned snapshot subset."""

    def metadata_load(self, local_path: Path) -> dict[str, object] | None:
        """Read one local model card's front-matter metadata.

        `load_model_card_metadata` calls this, so a fake client must implement it
        to satisfy the seam.
        """


class HuggingFaceFetcher:
    """Adapter over an injected Hugging Face client."""

    def __init__(self, client: HuggingFaceClient):
        self.client = client

    @classmethod
    def from_huggingface_hub(cls) -> "HuggingFaceFetcher":
        """Construct the production adapter from the declared client dependency."""
        return cls(huggingface_hub)

    def fetch(self, identity: ChairIdentity, destination: Path, paths: tuple[str, ...]) -> None:
        if identity.source != "huggingface" or not identity.repo or not identity.revision:
            raise UnresolvedChairRefusal(
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
                raise UnresolvedChairRefusal(
                    identity.role,
                    f"Hugging Face fetch returned no requested file {relative!r} for the pinned revision",
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)


class HuggingFaceMaterializationFetcher:
    """Fetch a whole pinned repository for the pod's evidence materializer."""

    def __init__(self, client: HuggingFaceClient):
        self.client = client

    @classmethod
    def from_huggingface_hub(cls) -> "HuggingFaceMaterializationFetcher":
        return cls(HuggingFaceFetcher.from_huggingface_hub().client)

    def fetch(self, repo: str, revision: str, destination: Path) -> None:
        """Leave the exact repository revision below `destination`, and nothing else.

        ``snapshot_download(local_dir=...)`` writes client bookkeeping under the
        directory it fills, including a wall-clock timestamp.  Deleting the whole
        ``.cache`` afterward is not safe either: a repository may itself track
        ``.cache/*`` bytes, which would then disappear before the manifest claimed
        to measure the exact revision. Downloading through a per-call client cache
        beside staging and copying the returned snapshot separates those namespaces:
        the returned directory is repository content, while client state stays
        outside the evidence tree and inside the volume's reserved promotion space.
        """

        if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
            raise DigestMismatchRefusal(
                repo, "materialization destination must be an existing empty regular directory"
            )
        client_cache = destination.with_name(f"{destination.name}.huggingface-cache")
        if client_cache.exists() or client_cache.is_symlink():
            raise DigestMismatchRefusal(
                repo, f"per-call Hugging Face cache path already exists: {client_cache}"
            )
        try:
            downloaded = self.client.snapshot_download(
                repo_id=repo, revision=revision, cache_dir=client_cache
            )
            source = Path(str(downloaded))
            if not _same_directory_anchor(source, client_cache):
                raise DigestMismatchRefusal(
                    repo,
                    "Hugging Face returned a snapshot outside its per-call cache; only the "
                    "requested revision below that isolated cache can enter staging",
                )
            if client_cache.is_symlink() or not client_cache.is_dir():
                raise DigestMismatchRefusal(
                    repo,
                    "Hugging Face replaced its per-call cache root; an isolated cache "
                    "must remain one regular directory",
                )
            if source.is_symlink() or not source.is_dir():
                raise DigestMismatchRefusal(
                    repo, "Hugging Face returned no regular snapshot directory"
                )
            files = _validated_materialization_files(source, client_cache, repo)
            for relative, origin, identity in files:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_verified_file(origin, target, identity, repo, relative)
        except OSError as error:
            raise DigestMismatchRefusal(
                repo, f"cannot copy the pinned Hugging Face snapshot into staging: {error}"
            ) from error
        finally:
            _cleanup_huggingface_cache(client_cache)


def _same_directory_anchor(path: Path, root: Path) -> bool:
    """Prove lexical containment reaches the configured root's actual inode."""

    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(resolved_root)
        anchor = resolved
        for _ in relative.parts:
            anchor = anchor.parent
        return anchor.samefile(root)
    except (OSError, ValueError):
        return False


def _validated_materialization_files(
    source: Path, client_cache: Path, repo: str
) -> list[tuple[str, Path, tuple[int, int]]]:
    """Inventory repository files without following a link outside the cache."""

    try:
        cache_device = client_cache.stat(follow_symlinks=False).st_dev
    except OSError as error:
        raise DigestMismatchRefusal(repo, f"cannot inspect the per-call cache: {error}") from error
    identities: dict[str, str] = {}
    files: list[tuple[str, Path, tuple[int, int]]] = []

    def refuse_walk(error: OSError) -> None:
        raise DigestMismatchRefusal(
            repo, f"cannot inspect the returned Hugging Face snapshot: {error}"
        ) from error

    for directory, directories, filenames in os.walk(
        source, followlinks=False, onerror=refuse_walk
    ):
        parent = Path(directory)
        for name in [*directories, *filenames]:
            candidate = parent / name
            relative = candidate.relative_to(source).as_posix()
            previous = apfs_alias(identities, relative)
            if previous is not None:
                raise DigestMismatchRefusal(
                    repo,
                    "the pinned repository carries paths that collide on default APFS: "
                    f"{previous!r} and {relative!r}",
                )
        for name in directories:
            candidate = parent / name
            relative = candidate.relative_to(source).as_posix()
            if candidate.is_symlink():
                raise DigestMismatchRefusal(
                    repo,
                    f"the Hugging Face cache represents directory {relative!r} as a symlink; "
                    "directory links are never followed into repository evidence",
                )
            try:
                status = candidate.stat(follow_symlinks=False)
            except OSError as error:
                raise DigestMismatchRefusal(
                    repo, f"cannot inspect returned snapshot directory {relative!r}: {error}"
                ) from error
            if status.st_dev != cache_device:
                raise DigestMismatchRefusal(
                    repo,
                    f"returned snapshot directory {relative!r} crosses the isolated "
                    "cache's device boundary",
                )
        for name in filenames:
            candidate = parent / name
            relative = candidate.relative_to(source).as_posix()
            try:
                origin = candidate.resolve(strict=True) if candidate.is_symlink() else candidate
                status = origin.stat(follow_symlinks=False)
            except OSError as error:
                raise DigestMismatchRefusal(
                    repo, f"cannot resolve returned snapshot file {relative!r}: {error}"
                ) from error
            if not stat.S_ISREG(status.st_mode) or status.st_dev != cache_device:
                raise DigestMismatchRefusal(
                    repo,
                    f"returned snapshot file {relative!r} is not a regular file on the "
                    "isolated cache device",
                )
            if not _same_directory_anchor(origin, client_cache):
                raise DigestMismatchRefusal(
                    repo,
                    f"returned snapshot file {relative!r} resolves outside the per-call "
                    "cache; external link targets are never read",
                )
            files.append((relative, origin, (status.st_dev, status.st_ino)))
    return sorted(files, key=lambda item: item[0])


def _copy_verified_file(
    origin: Path,
    target: Path,
    expected_identity: tuple[int, int],
    repo: str,
    relative: str,
) -> None:
    """Copy the inode that validation observed, refusing a check/use swap."""

    # `O_NONBLOCK`: a name swapped for a FIFO since validation would block this
    # open forever on a billing pod before `fstat` could reject it.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(origin, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise DigestMismatchRefusal(
                repo,
                f"returned snapshot file {relative!r} is no longer a regular file",
            )
        if (status.st_dev, status.st_ino) != expected_identity:
            raise DigestMismatchRefusal(
                repo,
                f"returned snapshot file {relative!r} changed between validation and copy",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
            with target.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
    finally:
        os.close(descriptor)


def _cleanup_huggingface_cache(client_cache: Path) -> None:
    """Best-effort removal of the per-call cache."""
    try:
        if client_cache.is_symlink():
            client_cache.unlink(missing_ok=True)
        else:
            shutil.rmtree(client_cache, ignore_errors=True)
    except OSError:
        pass


def load_model_card_metadata(path: Path) -> dict[str, object] | None:
    """Load card metadata with the declared Hugging Face dependency."""

    client = HuggingFaceFetcher.from_huggingface_hub().client
    return client.metadata_load(path)  # type: ignore[no-any-return]


class ChairRegistry:
    """Resolve only the requested role, then verify only its pinned artifact.

    The resolved identity and revision travel with every value this returns,
    and with what its consumers do with them.
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
    ) -> "ChairRegistry":
        return cls(load_models_toml(path), cache_root=cache_root, fetcher=fetcher)

    def resolve(self, role: str) -> ChairIdentity | AbsentChair:
        """Return that role's exact identity or explicit absence, never another role."""

        value = self.config.chairs.get(role)
        if value is None:
            raise UnresolvedChairRefusal(role, "role is not present in models.toml")
        return value

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        """Fetch missing pinned files only, then verify the complete exact snapshot."""

        self._require_current_identity(identity)
        manifest = self._manifest(identity)
        if identity.source == "local-repository":
            return verify_snapshot(identity, self._resolve_local_path(identity), manifest)
        return self._ensure_huggingface(identity, manifest)

    def manifest(self, identity: ChairIdentity) -> DigestManifest:
        """The configured identity's pinned digest manifest, checked against its pin."""

        self._require_current_identity(identity)
        return self._manifest(identity)

    def receipt(self, identity: ChairIdentity, serving: ServingDetails) -> ServingReceipt:
        """Validate a run-receipt value; writing it belongs to the run receipt writer.

        The identity must still be the configured one.
        """

        self._require_current_identity(identity)
        return build_receipt(identity, serving)

    def refuse_recipe_start(self, identity: ChairIdentity, difference: str) -> None:
        """Represent a serving-manager start failure without offering another recipe.

        The serving manager (`operations/serving/manager.py`) uses this for
        ordinary start failures it observed that have not already crossed the
        chair boundary. A prior chair refusal is normally re-raised without this
        call; unverified cleanup is the exception, and operator interrupts never
        pass through this method.
        """

        self._require_current_identity(identity)
        raise ServingRecipeRefusal(identity.role, difference)

    def _require_current_identity(self, identity: ChairIdentity) -> None:
        configured = self.resolve(identity.role)
        if isinstance(configured, AbsentChair):
            raise UnresolvedChairRefusal(
                identity.role, f"chair is explicitly absent: {configured.reason}"
            )
        if configured != identity:
            raise UnresolvedChairRefusal(
                identity.role,
                "identity differs from the configured pin; ensure and receipt never accept a neighbouring revision",
            )
        # After the identity check, so a mismatch wins over the sentinel.
        if identity.digest_manifest == PRE_MATERIALIZATION_SENTINEL:
            raise ConfigurationRefusal(
                identity.role,
                "digest_manifest is the all-zero pre-materialization sentinel, not a "
                f"pin: {identity.repo or identity.path}@{identity.revision} has not been "
                "fetched and measured. Materialize the model store, then record the "
                "measured manifest digest on this row through a reviewed config edit; "
                "nothing serves from a sentinel",
            )

    def _manifest(self, identity: ChairIdentity) -> DigestManifest:
        if self.manifest_root is None:
            raise UnresolvedChairRefusal(identity.role, "no manifest root was supplied")
        path = _under_root(self.manifest_root, identity.manifest, identity.role, "manifest")
        return read_manifest(path, expected_digest=identity.digest_manifest, chair=identity.role)

    def _resolve_local_path(self, identity: ChairIdentity) -> Path:
        if self.config.model_root is None:
            raise LocalPathRefusal(
                identity.role, "no model_root is configured for local-repository chair"
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
        self, identity: ChairIdentity, manifest: DigestManifest
    ) -> VerifiedSnapshot:
        if self.cache_root is None:
            raise UnresolvedChairRefusal(
                identity.role, "no cache_root was supplied for Hugging Face chair"
            )
        digest = identity.digest_manifest
        if not is_sha256(digest):
            raise CacheRevisionRefusal(identity.role, "digest_manifest is unsafe as a cache path")
        # The cache writes its descriptor inside the snapshot root, and would
        # overwrite a pinned file of that name after verification passed.
        if any(row.path == CACHE_DESCRIPTOR for row in manifest.rows):
            raise CacheRevisionRefusal(
                identity.role,
                f"the pinned manifest names {CACHE_DESCRIPTOR!r}, which is the cache's own "
                "identity descriptor; a snapshot cannot hold both under one name",
            )
        digests_root = self.cache_root / DIGEST_CACHE_DIRECTORY
        with _cache_write(identity.role, f"cache root {digests_root} cannot be created"):
            digests_root.mkdir(parents=True, exist_ok=True)
        with _digest_lock(digests_root, digest, identity.role):
            return self._ensure_digest_locked(identity, manifest, digests_root)

    def _ensure_digest_locked(
        self, identity: ChairIdentity, manifest: DigestManifest, digests_root: Path
    ) -> VerifiedSnapshot:
        digest = identity.digest_manifest
        target = digests_root / digest
        descriptor = digest_cache_descriptor(identity)
        missing: tuple[str, ...]
        if target.exists():
            _verify_cache_descriptor(target, identity.role, descriptor)
            inspection = inspect_snapshot_for_repair(
                identity, target, manifest, ignored_paths=(CACHE_DESCRIPTOR,)
            )
            if inspection.verified is not None:
                self._mark_used(target, identity.role)
                return inspection.verified
            missing = inspection.missing
        else:
            missing = tuple(row.path for row in manifest.rows)

        if self.fetcher is None:
            raise UnresolvedChairRefusal(
                identity.role, "no fetcher is configured for a missing pinned snapshot"
            )
        self._make_room(identity, manifest, digests_root)
        with _cache_write(identity.role, "no candidate cache directory could be created"):
            candidate = Path(tempfile.mkdtemp(prefix=f".{digest}.candidate-", dir=digests_root))
        try:
            if target.exists():
                with _cache_write(identity.role, "the existing cache could not be carried over"):
                    _copy_existing_files(target, candidate, manifest)
            try:
                self.fetcher.fetch(identity, candidate, missing)
            except ChairRefusal:
                raise
            except Exception as error:
                raise UnresolvedChairRefusal(
                    identity.role, f"pinned fetch failed: {error}"
                ) from error
            verified = verify_snapshot(identity, candidate, manifest)
            with _cache_write(identity.role, "the verified snapshot could not be promoted"):
                _write_cache_descriptor(candidate, descriptor)
                _promote(candidate, target)
            self._mark_used(target, identity.role)
            return VerifiedSnapshot(
                identity=verified.identity,
                root=target.resolve(),
                manifest_digest=verified.manifest_digest,
            )
        except Exception:
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            raise

    @staticmethod
    def _mark_used(target: Path, role: str) -> None:
        with _cache_write(role, "cache use time could not be recorded"):
            os.utime(target, None)

    def _configured_digests(self) -> set[str]:
        return {
            configured.digest_manifest
            for configured in self.config.chairs.values()
            if isinstance(configured, ChairIdentity)
            and configured.source == "huggingface"
            and is_sha256(configured.digest_manifest)
        }

    def _make_room(
        self, identity: ChairIdentity, manifest: DigestManifest, digests_root: Path
    ) -> None:
        """Clear abandoned work and evict least recently used caches until the pin fits.

        Only caches of configured digests are touched, and only while no other
        ensure holds that digest's lock; the caller holds the incoming digest's.
        """

        own = identity.digest_manifest
        configured = self._configured_digests()
        with _cache_write(identity.role, "abandoned chair work directories could not be removed"):
            for other in sorted(digests_root.iterdir()):
                owner = _work_directory_digest(other.name, configured)
                if owner is None:
                    continue
                if owner == own:
                    _remove(other)
                    continue
                with _try_digest_lock(digests_root, owner, identity.role) as held:
                    if held:
                        _remove(other)
        required = sum(row.size for row in manifest.rows)
        with _cache_write(identity.role, "container-local free space could not be measured"):
            free = shutil.disk_usage(digests_root).free
        if free < required:
            with _cache_write(identity.role, "other chair caches could not be evicted"):
                candidates = sorted(
                    (
                        other
                        for other in digests_root.iterdir()
                        if other.name in configured
                        and other.name != own
                        and (other.is_symlink() or other.is_dir())
                    ),
                    key=lambda other: (other.stat(follow_symlinks=False).st_mtime_ns, other.name),
                )
                for other in candidates:
                    if free >= required:
                        break
                    with _try_digest_lock(digests_root, other.name, identity.role) as held:
                        if held:
                            _remove(other)
                    free = shutil.disk_usage(digests_root).free
        if free < required:
            raise DiskSpaceRefusal(
                identity.role,
                f"container disk too small for chair {identity.role}: {free} bytes free "
                f"under {digests_root}, need at least {required} bytes for its pinned "
                "snapshot; increase container_disk_gb",
            )


def digest_cache_descriptor(identity: ChairIdentity) -> dict[str, object]:
    """What a digest-keyed cache records about itself: only the bytes it holds.

    Several roles, and even several repositories, can pin one manifest; the role
    that asked travels in the returned `VerifiedSnapshot` and its receipts.
    """

    return {"digest_manifest": identity.digest_manifest}


def _work_directory_digest(name: str, digests: set[str]) -> str | None:
    """The configured digest a `.<digest>.candidate-*` or `.<digest>.prior-*` belongs to."""

    for marker in (".candidate-", ".prior-"):
        head, found, _ = name.partition(marker)
        if found and head.startswith(".") and head[1:] in digests:
            return head[1:]
    return None


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _lock_path(digests_root: Path, digest: str) -> Path:
    return digests_root / f".{digest}.lock"


@contextmanager
def _digest_lock(digests_root: Path, digest: str, chair: str):
    """Serialise every fill, repair and eviction of one digest's cache, across processes."""

    with _cache_write(chair, "the cache lock could not be opened"):
        handle = _lock_path(digests_root, digest).open("a+b")
    with handle:
        with _cache_write(chair, "the cache lock could not be taken"):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _try_digest_lock(digests_root: Path, digest: str, chair: str):
    """Yield whether another digest's lock was free, holding it while it was."""

    with _cache_write(chair, "the cache lock could not be opened"):
        handle = _lock_path(digests_root, digest).open("a+b")
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _cache_write(chair: str, what: str):
    """Turn a filesystem failure during a cache write into a refusal naming the chair.

    A stage catches `ChairRefusal` to record a refusal, so a bare `OSError` from a
    mkdir, copy or promote would crash it instead.
    """
    try:
        yield
    except OSError as error:
        raise CacheRevisionRefusal(chair, f"{what}: {error}") from error


def resolve_local_path(identity: ChairIdentity, model_root: str | Path) -> Path:
    """Resolve a local chair under model_root and refuse traversal or symlink escape."""

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


def _under_root(root: Path, relative: str, chair: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise LocalPathRefusal(chair, f"{label} {relative!r} escapes its configured root")
    return candidate


def _verify_cache_descriptor(target: Path, chair: str, expected: dict[str, object]) -> None:
    descriptor = target / CACHE_DESCRIPTOR
    try:
        actual = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CacheRevisionRefusal(
            chair, f"cache has no readable identity descriptor: {error}"
        ) from error
    if actual != expected:
        raise CacheRevisionRefusal(
            chair,
            "cache descriptor differs from the configured pin; a cache never supplies a revision",
        )


def _write_cache_descriptor(target: Path, descriptor: dict[str, object]) -> None:
    (target / CACHE_DESCRIPTOR).write_bytes(canonical_bytes(descriptor))


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
