"""Canonical per-file digest manifests and complete snapshot verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, TypeVar

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.cpus import IoWorkers, io_workers

from .errors import ConfigurationRefusal, DigestMismatchRefusal
from .filesystem import read_limited_bytes
from .models import ChairIdentity, DigestManifest, ManifestRow, VerifiedSnapshot, is_sha256

# A manifest is a small control artifact, bounded like `model_store`'s shard index.
MAX_MANIFEST_BYTES = 16_777_216
HASH_CHUNK_BYTES = 8 * 1024 * 1024
HASH_WORKERS_MAX = 16

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class CopyLedger:
    """The SHA-256 of each file as it was written by a verifying copy.

    Every digest here already matched its manifest row when the copy wrote it;
    a verifier can then check the copied tree's structure without reading those
    bytes again.
    """

    digests: Mapping[str, str]
    workers: IoWorkers

    def to_record(self) -> dict[str, object]:
        return {"copied_files": len(self.digests), "io_workers": self.workers.to_record()}


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

    def measure_file(item: tuple[str, Path]) -> ManifestRow:
        relative, path = item
        return ManifestRow(
            path=relative,
            sha256=file_digest(path, "manifest", relative),
            size=file_size(path, "manifest", relative),
        )

    files = _regular_files(root, chair="manifest")
    return DigestManifest(rows=tuple(_map_files_in_order(files, measure_file)))


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
    copied: Mapping[str, str] | None = None,
) -> VerifiedSnapshot:
    """Verify every expected file and refuse the lexical first difference or extra.

    `copied` holds digests a verifying copy measured as it wrote those files
    (`CopyLedger.digests`); they are compared with the manifest instead of
    reading the bytes again. Every other check still runs on every file.
    """

    _inspect_snapshot(
        identity,
        snapshot_root,
        manifest,
        ignored_paths=ignored_paths,
        allow_missing=False,
        copied=copied or {},
    )
    return _verified(identity, snapshot_root, manifest)


def verify_snapshot_structure(
    identity: ChairIdentity,
    snapshot_root: str | Path,
    manifest: DigestManifest,
    *,
    ignored_paths: Iterable[str] = (),
) -> None:
    """Every check of `verify_snapshot` except reading the bytes.

    Missing and extra files, links, non-regular entries and sizes are refused as
    there. For a store snapshot whose bytes are hashed against this same manifest
    when they are copied to where they are used; it proves no byte's content.
    """

    _inspect_snapshot(
        identity,
        snapshot_root,
        manifest,
        ignored_paths=ignored_paths,
        allow_missing=False,
        hash_bytes=False,
    )


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
    copied: Mapping[str, str] | None = None,
    hash_bytes: bool = True,
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

    def inspect_file(item: tuple[str, ManifestRow | None, Path | None]) -> str | None:
        relative, row, path = item
        if row is None:
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: extra file"
            )
        if path is None:
            if allow_missing:
                return relative
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
        if copied and relative in copied:
            actual_sha = copied[relative]
        elif not hash_bytes:
            return None
        else:
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
        return None

    files = [
        (relative, expected.get(relative), actual.get(relative))
        for relative in sorted(set(expected) | set(actual))
    ]
    return tuple(
        relative for relative in _map_files_in_order(files, inspect_file) if relative is not None
    )


FileStat = tuple[str, int, int, int, int, int]


def snapshot_stat_identity(
    snapshot_root: str | Path, *, ignored_paths: Iterable[str] = ()
) -> tuple[FileStat, ...] | None:
    """Each regular file's path, device, inode, size, mtime and ctime, in path order.

    Any rewrite, replacement, addition or removal of a file changes this value.
    None when the tree cannot be walked or holds anything but regular files, so a
    caller falls back to full verification, which names the problem.
    """

    root = Path(snapshot_root)
    ignored = set(ignored_paths)
    try:
        files = _regular_files(root, chair="snapshot")
        stats: list[FileStat] = []
        for relative, path in files:
            if relative in ignored:
                continue
            status = path.stat(follow_symlinks=False)
            stats.append(
                (
                    relative,
                    status.st_dev,
                    status.st_ino,
                    status.st_size,
                    status.st_mtime_ns,
                    status.st_ctime_ns,
                )
            )
    except (OSError, DigestMismatchRefusal):
        return None
    return tuple(stats)


def _map_files_in_order(items: list[_Item], work: Callable[[_Item], _Result]) -> list[_Result]:
    """Hash independent files concurrently, then observe results in lexical order."""

    # A container sees its host's CPU count, often far above its own share, and
    # past a few readers the disk, not the hashing, is the limit.
    workers = min(len(items), os.cpu_count() or 1, HASH_WORKERS_MAX)
    if workers <= 1:
        return [work(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, items))


def copy_and_digest(source: Path, target: Path, row: ManifestRow, *, chair: str) -> str:
    """Copy one pinned file, hashing the bytes as they are written; refuse a mismatch.

    The source is opened without following a link, its size is checked against
    the row before any byte is read, and the digest of what was written must
    equal the row's. Returns that digest.
    """

    at = f"snapshot differs at {row.path}"
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    hasher = hashlib.sha256()
    written = 0
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise DigestMismatchRefusal(chair, f"{at}: cannot be read: {error}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise DigestMismatchRefusal(chair, f"{at}: not a regular file")
        if status.st_size != row.size:
            raise DigestMismatchRefusal(chair, f"{at}: size {status.st_size}, expected {row.size}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(descriptor, "rb", closefd=False) as reader, target.open("xb") as writer:
            while chunk := reader.read(HASH_CHUNK_BYTES):
                writer.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
    except OSError as error:
        raise DigestMismatchRefusal(chair, f"{at}: cannot be copied: {error}") from error
    finally:
        os.close(descriptor)
    if written != row.size:
        raise DigestMismatchRefusal(chair, f"{at}: size {written} copied, expected {row.size}")
    actual = hasher.hexdigest()
    if actual != row.sha256:
        raise DigestMismatchRefusal(chair, f"{at}: sha256 {actual}, expected {row.sha256}")
    return actual


def copy_and_digest_files(
    items: Iterable[tuple[Path, Path, ManifestRow]], *, chair: str
) -> CopyLedger:
    """Copy many pinned files in one pool, largest first, and return their ledger.

    Every copy runs to its end, then the lexically first refusal is raised, so
    the file a refusal names does not depend on which worker finished first.
    """

    work = sorted(items, key=lambda item: (-item[2].size, item[2].path))
    try:
        workers = io_workers()
    except ValueError as error:
        raise ConfigurationRefusal(chair, str(error)) from error

    def copy(
        item: tuple[Path, Path, ManifestRow],
    ) -> tuple[str, str | None, DigestMismatchRefusal | None]:
        source, target, row = item
        try:
            return row.path, copy_and_digest(source, target, row, chair=chair), None
        except DigestMismatchRefusal as refusal:
            return row.path, None, refusal

    if not work:
        return CopyLedger(digests={}, workers=workers)
    with ThreadPoolExecutor(max_workers=min(workers.count, len(work))) as pool:
        results = sorted(pool.map(copy, work), key=lambda result: result[0])
    for _, _, refusal in results:
        if refusal is not None:
            raise DigestMismatchRefusal(refusal.chair, refusal.difference) from refusal
    return CopyLedger(
        digests={path: digest for path, digest, _ in results if digest is not None},
        workers=workers,
    )


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

    Model snapshots can contain multi-gigabyte weights, so each read is bounded.
    Large ``hashlib`` updates release the GIL while other files are hashed.
    """

    def digest() -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(HASH_CHUNK_BYTES):
                hasher.update(chunk)
        return hasher.hexdigest()

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
