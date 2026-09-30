"""Canonical per-file digest manifests and complete snapshot verification."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of

from .errors import DigestMismatchRefusal
from .filesystem import read_limited_bytes
from .models import ChairIdentity, DigestManifest, ManifestRow, VerifiedSnapshot, is_sha256

# A manifest is a small control artifact, bounded like `model_store`'s shard index.
MAX_MANIFEST_BYTES = 16_777_216


@dataclass(frozen=True, slots=True)
class SnapshotInspection:
    """One strict cache inspection, either complete or repairably incomplete."""

    verified: VerifiedSnapshot | None
    missing: tuple[str, ...]


def manifest_digest(manifest: DigestManifest) -> str:
    """The configured pin: digest of the canonical bare sorted row artifact."""

    return digest_of(manifest.to_record())


def build_manifest(snapshot_root: str | Path) -> DigestManifest:
    """Build a sorted manifest from regular files beneath one snapshot root."""

    root = Path(snapshot_root)
    if not root.is_dir():
        raise DigestMismatchRefusal("manifest", f"snapshot root {root} is not a directory")
    rows = []
    for relative, path in _regular_files(root, chair="manifest"):
        rows.append(
            ManifestRow(
                path=relative,
                sha256=file_digest(path, "manifest", relative),
                size=file_size(path, "manifest", relative),
            )
        )
    return DigestManifest(rows=tuple(rows))


def write_manifest(manifest: DigestManifest, path: str | Path) -> str:
    """Write the canonical artifact and return its configured digest pin.

    It replaces an existing file, because it serves fixtures and authoring tools
    that regenerate a pin; the model store publishes real manifests once instead.
    """

    _validate_manifest(manifest, "manifest")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_bytes(manifest.to_record()))
    return manifest_digest(manifest)


def read_manifest(path: str | Path, *, expected_digest: str, chair: str) -> DigestManifest:
    """Read a manifest artifact and prove its canonical bytes match the pin.

    The pin names the artifact, not merely a JSON value that happens to parse to
    the same rows.  Accepting whitespace or another serialization here would let
    the file on disk differ from the artifact whose digest the configuration
    names.  Every writer emits `canonical_bytes` of the manifest record:
    `write_manifest` for fixtures and authoring tools, and the model store's
    publish-once promotion for real snapshots.
    """

    source = Path(path)
    try:
        data = read_limited_bytes(source, MAX_MANIFEST_BYTES, chair, f"manifest {source}")
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DigestMismatchRefusal(chair, f"cannot read manifest {source}: {error}") from error
    manifest = _manifest_from_record(raw, chair)
    canonical = canonical_bytes(manifest.to_record())
    if data != canonical:
        raise DigestMismatchRefusal(
            chair,
            f"manifest {source} is not serialized as the canonical artifact bytes",
        )
    actual = digest_bytes(data)
    if actual != expected_digest:
        raise DigestMismatchRefusal(
            chair,
            f"manifest differs: expected digest {expected_digest}, got {actual}",
        )
    return manifest


def verify_snapshot(
    identity: ChairIdentity,
    snapshot_root: str | Path,
    manifest: DigestManifest,
    *,
    ignored_paths: Iterable[str] = (),
) -> VerifiedSnapshot:
    """Verify every expected file and refuse the lexical first difference or extra."""

    _inspect_snapshot(
        identity,
        snapshot_root,
        manifest,
        ignored_paths=ignored_paths,
        allow_missing=False,
    )
    return _verified(identity, snapshot_root, manifest)


def inspect_snapshot_for_repair(
    identity: ChairIdentity,
    snapshot_root: str | Path,
    manifest: DigestManifest,
    *,
    ignored_paths: Iterable[str] = (),
) -> SnapshotInspection:
    """Strictly inspect a cache while returning only pinned paths that are absent.

    Present files are size- and digest-verified, extras and non-regular entries
    are refused, and a complete cache carries the verified result of this same
    pass.  Callers may therefore repair genuine gaps without weakening complete
    cache verification or hashing every present file a second time.
    """

    missing = _inspect_snapshot(
        identity,
        snapshot_root,
        manifest,
        ignored_paths=ignored_paths,
        allow_missing=True,
    )
    if missing:
        return SnapshotInspection(verified=None, missing=missing)
    return SnapshotInspection(verified=_verified(identity, snapshot_root, manifest), missing=())


def _inspect_snapshot(
    identity: ChairIdentity,
    snapshot_root: str | Path,
    manifest: DigestManifest,
    *,
    ignored_paths: Iterable[str],
    allow_missing: bool,
) -> tuple[str, ...]:
    """Inventory and verify a snapshot once, returning the pinned paths it lacks.

    Strict mode refuses the first missing file, so it only ever returns `()`.
    """

    root = Path(snapshot_root)
    if not root.is_dir():
        raise DigestMismatchRefusal(identity.role, f"snapshot root {root} is not a directory")
    _validate_manifest(manifest, identity.role)
    ignored = set(ignored_paths)
    expected = {row.path: row for row in manifest.rows}
    actual = {
        relative: path
        for relative, path in _regular_files(root, chair=identity.role)
        if relative not in ignored
    }
    missing: list[str] = []
    for relative in sorted(set(expected) | set(actual)):
        row = expected.get(relative)
        path = actual.get(relative)
        if row is None:
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: extra file"
            )
        if path is None:
            if allow_missing:
                missing.append(relative)
                continue
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: missing file"
            )
        size = file_size(path, identity.role, relative)
        if size != row.size:
            if allow_missing:
                raise DigestMismatchRefusal(
                    identity.role,
                    f"snapshot differs at {relative}: cached bytes do not match",
                )
            raise DigestMismatchRefusal(
                identity.role,
                f"snapshot differs at {relative}: size {size}, expected {row.size}",
            )
        actual_sha = file_digest(path, identity.role, relative)
        if actual_sha != row.sha256:
            if allow_missing:
                raise DigestMismatchRefusal(
                    identity.role,
                    f"snapshot differs at {relative}: cached bytes do not match",
                )
            raise DigestMismatchRefusal(
                identity.role,
                f"snapshot differs at {relative}: sha256 {actual_sha}, expected {row.sha256}",
            )
    return tuple(missing)


def _verified(
    identity: ChairIdentity, snapshot_root: str | Path, manifest: DigestManifest
) -> VerifiedSnapshot:
    return VerifiedSnapshot(
        identity=identity,
        root=Path(snapshot_root).resolve(),
        manifest_digest=manifest_digest(manifest),
    )


def _manifest_from_record(raw: Any, chair: str) -> DigestManifest:
    if not isinstance(raw, list):
        raise DigestMismatchRefusal(chair, "manifest artifact is not the required bare row list")
    rows: list[ManifestRow] = []
    for index, value in enumerate(raw):
        if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
            raise DigestMismatchRefusal(
                chair, f"manifest row {index} does not have exactly path, sha256, size"
            )
        path = value["path"]
        sha = value["sha256"]
        size = value["size"]
        if not _safe_relative(path):
            raise DigestMismatchRefusal(chair, f"manifest row {index} has unsafe path {path!r}")
        if not is_sha256(sha):
            raise DigestMismatchRefusal(chair, f"manifest row {index} has no lowercase sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DigestMismatchRefusal(chair, f"manifest row {index} has invalid size {size!r}")
        rows.append(ManifestRow(path=path, sha256=sha, size=size))
    manifest = DigestManifest(tuple(rows))
    _validate_manifest(manifest, chair)
    return manifest


def file_size(path: Path, chair: str, relative: str) -> int:
    """One file's size, with a filesystem failure kept inside the taxonomy.

    A caller catches `ChairRefusal` to record a refusal against a named chair, so
    a bare `PermissionError` naming no chair and no file would escape it. An
    unreadable pinned file is a snapshot that does not verify.

    Split from `file_digest` rather than returning both, so that a size that
    already disagrees with the pin refuses without reading the file. Model weights
    are the files this walks; hashing several gigabytes to then report a size
    mismatch is a long wait for an answer the `stat` already had.
    """
    return _guarded(chair, relative, lambda: path.stat().st_size)


def file_digest(path: Path, chair: str, relative: str) -> str:
    """Stream one file's SHA-256 under the same taxonomy guarantee as `file_size`.

    ``hashlib.file_digest`` (Python 3.11+, PSF license) is the standard-library
    file helper.  Model snapshots can contain multi-gigabyte weights, so reading
    a whole file before hashing unnecessarily duplicates it in process memory.
    """

    def digest() -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    return _guarded(chair, relative, digest)


def _guarded(chair: str, relative: str, read):
    try:
        return read()
    except OSError as error:
        raise DigestMismatchRefusal(
            chair, f"snapshot differs at {relative}: cannot be read: {error}"
        ) from error


def _validate_manifest(manifest: DigestManifest, chair: str) -> None:
    # An empty manifest is a pin no artifact can fail.
    if not manifest.rows:
        raise DigestMismatchRefusal(
            chair, "manifest has no rows; an empty manifest constrains no snapshot"
        )
    paths = [row.path for row in manifest.rows]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise DigestMismatchRefusal(chair, "manifest rows are not strictly sorted by unique path")
    for row in manifest.rows:
        if not _safe_relative(row.path):
            raise DigestMismatchRefusal(chair, f"manifest has unsafe path {row.path!r}")
        if not is_sha256(row.sha256):
            raise DigestMismatchRefusal(chair, f"manifest row {row.path} has no lowercase sha256")
        if not isinstance(row.size, int) or isinstance(row.size, bool) or row.size < 0:
            raise DigestMismatchRefusal(chair, f"manifest row {row.path} has invalid size")


def _regular_files(root: Path, *, chair: str) -> list[tuple[str, Path]]:
    """Return sorted regular files, refusing a symlink instead of following it."""

    root = root.resolve()
    found: list[tuple[str, Path]] = []

    def _refuse(error: OSError) -> None:
        """An unlistable directory is a snapshot that does not verify, not a crash."""
        raise DigestMismatchRefusal(
            chair, f"snapshot under {root} cannot be walked: {error}"
        ) from error

    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=_refuse):
        directory_path = Path(directory)
        directories.sort()
        filenames.sort()
        for name in list(directories):
            candidate = directory_path / name
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                raise DigestMismatchRefusal(
                    chair, f"snapshot differs at {relative}: symlink directory"
                )
        for name in filenames:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink() or not candidate.is_file():
                raise DigestMismatchRefusal(
                    chair, f"snapshot differs at {relative}: not a regular file"
                )
            found.append((relative, candidate))
    return sorted(found, key=lambda item: item[0])


def _safe_relative(value: Any) -> bool:
    # Judged by empty `.parts` ("./" too), agreeing with `model_store._safe`.
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and bool(path.parts)
