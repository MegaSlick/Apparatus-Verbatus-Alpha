"""Canonical per-file digest manifests and complete snapshot verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of

from .errors import DigestMismatchRefusal
from .models import ChairIdentity, DigestManifest, ManifestRow, VerifiedSnapshot, is_sha256


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
    """Write the canonical artifact and return its configured digest pin."""

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
    names.  `write_manifest` is the one writer, so exact canonical bytes are a
    reasonable and useful contract.
    """

    source = Path(path)
    try:
        data = source.read_bytes()
        raw = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
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
    for relative in sorted(set(expected) | set(actual)):
        row = expected.get(relative)
        path = actual.get(relative)
        if row is None:
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: extra file"
            )
        if path is None:
            raise DigestMismatchRefusal(
                identity.role, f"snapshot differs at {relative}: missing file"
            )
        size = file_size(path, identity.role, relative)
        if size != row.size:
            raise DigestMismatchRefusal(
                identity.role,
                f"snapshot differs at {relative}: size {size}, expected {row.size}",
            )
        actual_sha = file_digest(path, identity.role, relative)
        if actual_sha != row.sha256:
            raise DigestMismatchRefusal(
                identity.role,
                f"snapshot differs at {relative}: sha256 {actual_sha}, expected {row.sha256}",
            )
    return VerifiedSnapshot(
        identity=identity, root=root.resolve(), manifest_digest=manifest_digest(manifest)
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

    `errors.py` calls its list "the complete public taxonomy", and a caller that
    catches `ChairRefusal` to record a refusal against a named chair got a bare
    `PermissionError` instead — an error outside the taxonomy that names no chair
    and no file. An unreadable pinned file is a snapshot that does not verify.

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
    # A manifest with no rows is satisfied by any empty directory, so a chair
    # pinned to one resolves, verifies and receipts exactly as a real one while
    # constraining nothing. A pin is a constant the artifact must match (#43);
    # a pin that no artifact can fail is not one.
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
    # Empty `.parts` is the rule, not the literal ".": "./" also names the
    # directory itself and has no parts, and `model_store._safe` — the same
    # rule spelled as a refusal — already judges by parts. The two must agree.
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and bool(path.parts)
