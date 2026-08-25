"""A local, content-addressed copy of one sealed run tree.

The destination is intended to be a synced Mac directory.  It is not a second
mutable run tree: every member is stored by its SHA-256 and a snapshot names
the exact relative-path-to-digest inventory.  That gives a later invocation
enough evidence to reuse a verified copy, while making a different byte at an
existing digest path a loud refusal rather than an overwrite.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.contracts.identities import validate_run_id
from common.runtree.store import RunTree

SCHEMA = "mac-run-backup.v2"
CHUNK_BYTES = 1024 * 1024
# Backup targets commonly include exFAT, network shares, and sync folders; their
# hard-link errors must name that filesystem constraint without weakening the
# temp-then-link publication guarantee.
_NO_HARD_LINKS: Final = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})
_LAYOUT_DIRECTORIES: Final = (
    PurePosixPath("objects"),
    PurePosixPath("objects/sha256"),
    PurePosixPath("snapshots"),
    PurePosixPath("snapshots/sha256"),
)


class BackupRefusal(RuntimeError):
    """The backup is incomplete or cannot be verified; it is never called current."""


@dataclass(frozen=True, slots=True)
class BackupReport:
    snapshot_sha256: str
    copied: int
    reused: int

    def to_record(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "snapshot_sha256": self.snapshot_sha256,
            "copied": self.copied,
            "reused": self.reused,
        }

    @classmethod
    def from_record(cls, value: object) -> BackupReport:
        """The parent must verify every worker-reported type before declaring success."""

        fields = {"schema", "snapshot_sha256", "copied", "reused"}
        if not isinstance(value, dict) or set(value) != fields:
            raise BackupRefusal(f"backup worker report must contain exactly {sorted(fields)}")
        if value["schema"] != SCHEMA:
            raise BackupRefusal(
                f"backup worker report declares schema {value['schema']!r}, not {SCHEMA!r}"
            )
        snapshot_sha256 = value["snapshot_sha256"]
        if not _is_sha256(snapshot_sha256):
            raise BackupRefusal("backup worker report has no lowercase snapshot sha256")
        counts: dict[str, int] = {}
        for field in ("copied", "reused"):
            count = value[field]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise BackupRefusal(
                    f"backup worker report field {field!r} must be a non-negative integer"
                )
            counts[field] = count
        if counts["copied"] + counts["reused"] == 0:
            raise BackupRefusal("backup worker report claims a successful snapshot of no files")
        return cls(snapshot_sha256, counts["copied"], counts["reused"])


def sync_run_tree(run_root: Path, run_id: str, mac_directory: Path) -> BackupReport:
    """Copy one run's regular files into a verified, append-only local store.

    A run may be resumed while this command is running.  We therefore scan it
    before and after the copy and refuse to publish a snapshot if either view
    differs: a snapshot must name one coherent tree, never a silent mixture of
    two stage boundaries.
    """

    source, root = resolve_backup_paths(run_root, run_id, mac_directory)
    _prepare_backup_layout(source, root)
    # Keep the requested id: an in-root symlink may resolve to a differently
    # named directory, but the snapshot must record the run the operator named.
    managed_paths = RunTree(run_root, run_id).inventory_scope()
    before, before_temporaries = _inventory(source, managed_paths)
    copied = reused = 0
    for relative, digest in before.items():
        outcome = _copy_verified(source / relative, root / "objects" / "sha256" / digest, digest)
        copied += outcome == "copied"
        reused += outcome == "reused"
    after, after_temporaries = _inventory(source, managed_paths)
    if (after, after_temporaries) != (before, before_temporaries):
        raise BackupRefusal(
            "the run tree changed while it was being copied; no current backup snapshot was published"
        )
    record = {
        "schema": SCHEMA,
        "run_id": run_id,
        "files": [
            {"relative_path": relative, "sha256": digest}
            for relative, digest in sorted(before.items())
        ],
        # RunTree publishes through same-directory `.<target>.tmp-*` names.
        # They are in-flight or crash residue, never published evidence. Their
        # names remain in the snapshot so excluding them cannot become silent.
        "excluded_publication_temporaries": list(before_temporaries),
    }
    data = canonical_bytes(record)
    snapshot_sha256 = digest_bytes(data)
    _publish_bytes(root / "snapshots" / "sha256" / f"{snapshot_sha256}.json", data)
    report = BackupReport(snapshot_sha256, copied, reused)
    verify_backup_snapshot(root, run_id, report)
    return report


def resolve_backup_paths(run_root: Path, run_id: str, mac_directory: Path) -> tuple[Path, Path]:
    """Check source and destination before any destination component is created.

    The parent creates the layout before confinement because custody grants the
    child publication, not directory creation. Delaying overlap checks to the
    child could therefore create the layout inside the sealed source first.
    """

    try:
        checked_run_id = validate_run_id(run_id)
    except ContractError as error:
        raise BackupRefusal(f"backup run id is invalid: {error}") from error
    requested_root = Path(run_root).resolve()
    source = (requested_root / checked_run_id).resolve()
    if not source.is_relative_to(requested_root):
        raise BackupRefusal("run id resolves outside the selected run root")
    if not source.is_dir():
        raise BackupRefusal(f"run {run_id!r} does not exist below the selected run root")
    root = Path(mac_directory).resolve()
    if _contains(root, source):
        raise BackupRefusal("the Mac backup directory must not contain the source run tree")
    if _contains(source, root):
        raise BackupRefusal("the Mac backup directory must not sit inside the source run tree")
    _validate_backup_layout(source, root)
    return source, root


def _validate_backup_layout(source: Path, root: Path) -> None:
    """Every existing layout component must be a real, non-source directory."""

    for directory in (root, *(root / part for part in _LAYOUT_DIRECTORIES)):
        if directory.is_symlink():
            raise BackupRefusal(
                f"backup layout path {directory} is a symbolic link; no backup path may redirect"
            )
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise BackupRefusal(f"backup layout path {directory} exists but is not a directory")
        if _contains(source, directory) or _contains(directory, source):
            raise BackupRefusal(
                f"backup layout path {directory} overlaps the source run tree by filesystem identity"
            )


def _prepare_backup_layout(source: Path, root: Path) -> None:
    """Revalidate after each mkdir because later paths depend on earlier ones."""

    _validate_backup_layout(source, root)
    for directory in (root, *(root / part for part in _LAYOUT_DIRECTORIES)):
        try:
            directory.mkdir(exist_ok=True)
        except OSError as error:
            raise BackupRefusal(
                f"backup layout path {directory} could not be created: {error}"
            ) from error
        _validate_backup_layout(source, root)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def _contains(ancestor: Path, descendant: Path) -> bool:
    """Whether one path is the other, or holds it, by filesystem identity.

    Not `is_relative_to`, which compares spellings.  The Mac target is a
    case-insensitive filesystem -- APFS is case-insensitive by default -- so
    `/Volumes/Vol/runs` and `/Volumes/vol/runs` are one directory that compares
    unequal as text, and `Path.resolve` does not correct case on macOS.  Device
    and inode are what decide whether two names are the same directory, and they
    also settle aliases such as bind mounts.
    """
    target = _identity(ancestor)
    if target is None:
        return False
    return any(_identity(candidate) == target for candidate in (descendant, *descendant.parents))


def _inventory(
    source: Path, managed_paths: tuple[str, ...]
) -> tuple[dict[str, str], tuple[str, ...]]:
    inventory: dict[str, str] = {}
    publication_temporaries: list[str] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise BackupRefusal(f"run tree member {relative!r} is a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BackupRefusal(f"run tree member {relative!r} is not a regular file")
        if _is_publication_temporary(relative, managed_paths):
            publication_temporaries.append(relative)
            continue
        inventory[relative] = _sha256(path, what=f"run tree member {relative!r}")
    if not inventory:
        raise BackupRefusal("the selected run tree has no files to back up")
    return inventory, tuple(publication_temporaries)


def _is_publication_temporary(relative: str, managed_paths: tuple[str, ...]) -> bool:
    path = PurePosixPath(relative)
    if not path.name.startswith("."):
        return False
    target_name, separator, unique = path.name[1:].partition(".tmp-")
    if not separator or not target_name or not unique:
        return False
    target = path.with_name(target_name).as_posix()
    return any(
        target.startswith(scope) if scope.endswith("/") else target == scope
        for scope in managed_paths
    )


def _sha256(path: Path, *, what: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupRefusal(f"{what} could not be read: {error}") from error
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _copy_verified(source: Path, target: Path, expected_sha256: str) -> str:
    if target.exists():
        if (
            not target.is_file()
            or target.is_symlink()
            or _sha256(target, what=f"backup object {target.name!r}") != expected_sha256
        ):
            raise BackupRefusal(
                f"backup object {target.name!r} already exists but does not verify; it was not overwritten"
            )
        return "reused"
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as destination, source.open("rb") as origin:
            for chunk in iter(lambda: origin.read(CHUNK_BYTES), b""):
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if digest.hexdigest() != expected_sha256:
            raise BackupRefusal(f"source {source.name!r} changed while it was being copied")
        try:
            _link_or_refuse(temporary_path, target)
        except FileExistsError:
            if (
                not target.is_file()
                or target.is_symlink()
                or _sha256(target, what=f"backup object {target.name!r}") != expected_sha256
            ):
                raise BackupRefusal(
                    f"backup object {target.name!r} appeared but does not verify; it was not overwritten"
                ) from None
            return "reused"
        if _sha256(target, what=f"backup object {target.name!r}") != expected_sha256:
            raise BackupRefusal(f"backup object {target.name!r} did not verify after copy")
        return "copied"
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _link_or_refuse(temporary: Path, target: Path) -> None:
    """Publish atomically while translating unsupported hard links for the operator.

    `FileExistsError` must reach the caller so it can verify the bytes that won
    the name. A direct exclusive write would expose the final name before its
    content was complete.
    """
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno in _NO_HARD_LINKS:
            raise BackupRefusal(
                f"a backup member could not take its final name under {target.parent}: that "
                f"filesystem refuses hard links ({error.strerror}). A member is linked into "
                "place so that a partly written file can never wear a digest's name, so "
                "--mac-directory has to name a directory on a filesystem that supports "
                "links -- an exFAT or FAT32 volume, an SMB or AFP share, and some "
                "sync-provider folders do not"
            ) from error
        raise


def _refuse_a_different_snapshot(target: Path, data: bytes) -> None:
    """Refuse a taken snapshot name unless it is already exactly these bytes.

    A symlink or a non-regular file is refused rather than followed, matching
    `_copy_verified`.  A published snapshot is an immutable index into the
    store; a link whose destination can change afterwards is not that, even on
    the pass where the bytes it points at happen to agree.
    """
    if target.is_symlink() or not target.is_file():
        raise BackupRefusal(
            "backup snapshot path is taken by something that is not a regular file; "
            "it was not overwritten"
        )
    try:
        existing = target.read_bytes()
    except OSError as error:
        raise BackupRefusal(f"backup snapshot cannot be read: {error}") from error
    if existing != data:
        raise BackupRefusal(
            "backup snapshot path already names different bytes; it was not overwritten"
        )


def _publish_bytes(target: Path, data: bytes) -> None:
    """Never expose a final snapshot name before its content is written and synced.

    A direct `O_CREAT | O_EXCL` open makes the final name exist before the
    bytes are in it; a kill between that open and the write leaves a
    permanently unpublishable snapshot path, since every retry finds the
    empty file already there and refuses rather than completing it. Writing
    a same-directory temporary first and linking it into place only after it
    is flushed and synced means a crash can only ever leave a stray
    temporary behind, never a partial file at the final name.

    `is_symlink() or exists()`, not `exists()` alone: `exists()` follows the
    link and answers False for a dangling one, which let a dangling symlink at
    a snapshot's name reach `os.link` and escape as a bare `FileNotFoundError`
    from the read that was meant to refuse it.
    """
    if target.is_symlink() or target.exists():
        _refuse_a_different_snapshot(target, data)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _link_or_refuse(temporary_path, target)
        except FileExistsError:
            _refuse_a_different_snapshot(target, data)
        else:
            try:
                published = target.read_bytes()
            except OSError as error:
                raise BackupRefusal(
                    f"backup snapshot could not be read back after publication: {error}"
                ) from error
            if published != data:
                raise BackupRefusal("backup snapshot did not verify after publication")
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def verify_backup_snapshot(root: Path, run_id: str, report: BackupReport) -> dict[str, object]:
    """Read back the snapshot and every object before reporting backup success."""

    target = root / "snapshots" / "sha256" / f"{report.snapshot_sha256}.json"
    if target.is_symlink() or not target.is_file():
        raise BackupRefusal("backup worker reported a snapshot that is not a regular file")
    try:
        data = target.read_bytes()
    except OSError as error:
        raise BackupRefusal(f"backup snapshot could not be read back: {error}") from error
    if digest_bytes(data) != report.snapshot_sha256:
        raise BackupRefusal("backup worker reported a snapshot whose bytes do not match its sha256")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BackupRefusal(f"backup snapshot is not readable JSON: {error}") from error
    fields = {"schema", "run_id", "files", "excluded_publication_temporaries"}
    if not isinstance(value, dict) or set(value) != fields:
        raise BackupRefusal(f"backup snapshot must contain exactly {sorted(fields)}")
    if value["schema"] != SCHEMA or value["run_id"] != run_id:
        raise BackupRefusal("backup snapshot does not name this schema and requested run id")
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise BackupRefusal("backup snapshot has no file inventory")
    checked_rows: list[tuple[str, str]] = []
    for row in files:
        if not isinstance(row, dict) or set(row) != {"relative_path", "sha256"}:
            raise BackupRefusal("backup snapshot has a malformed file row")
        relative, digest = row["relative_path"], row["sha256"]
        if (
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or not _is_sha256(digest)
        ):
            raise BackupRefusal("backup snapshot has a malformed relative path or sha256")
        checked_rows.append((relative, digest))
    if checked_rows != sorted(set(checked_rows)):
        raise BackupRefusal("backup snapshot file inventory is not sorted and unique")
    temporaries = value["excluded_publication_temporaries"]
    if (
        not isinstance(temporaries, list)
        or any(not isinstance(path, str) or not path for path in temporaries)
        or temporaries != sorted(set(temporaries))
    ):
        raise BackupRefusal("backup snapshot has a malformed publication-temporary inventory")
    if len(checked_rows) != report.copied + report.reused:
        raise BackupRefusal("backup worker report counts do not reconcile with its snapshot")
    for digest in sorted({digest for _relative, digest in checked_rows}):
        object_path = root / "objects" / "sha256" / digest
        if (
            object_path.is_symlink()
            or not object_path.is_file()
            or _sha256(object_path, what=f"backup object {digest!r}") != digest
        ):
            raise BackupRefusal(f"backup object {digest!r} does not verify on read-back")
    return value
