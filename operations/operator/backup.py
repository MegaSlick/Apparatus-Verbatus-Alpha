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
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.identities import validate_run_id

SCHEMA = "mac-run-backup.v1"
CHUNK_BYTES = 1024 * 1024
# What a filesystem that will not hard-link answers with.  Named here for the
# same reason `common/runtree/store.py::_atomic_create` names it, and with more
# cause: that one guards the run root, while this one guards a *Mac* directory,
# so an exFAT USB drive, an SMB or AFP share, and some sync-provider folders are
# all ordinary choices for it and all of them refuse `os.link`.  Unnamed, the
# condition reaches the operator as an errno about a temporary file nobody asked
# for, inside a message promising to name the backup-directory problem.
_NO_HARD_LINKS: Final = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})


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


def sync_run_tree(run_root: Path, run_id: str, mac_directory: Path) -> BackupReport:
    """Copy one run's regular files into a verified, append-only local store.

    A run may be resumed while this command is running.  We therefore scan it
    before and after the copy and refuse to publish a snapshot if either view
    differs: a snapshot must name one coherent tree, never a silent mixture of
    two stage boundaries.
    """

    source, root = resolve_backup_paths(run_root, run_id, mac_directory)
    # The requested id, not `source.name`: a run-id symlink that stays inside the
    # run root resolves to a directory with a different name, and the snapshot
    # records which run was asked for.
    checked_run_id = validate_run_id(run_id)
    root.mkdir(parents=True, exist_ok=True)
    before = _inventory(source)
    copied = reused = 0
    for relative, digest in before.items():
        outcome = _copy_verified(source / relative, root / "objects" / "sha256" / digest, digest)
        copied += outcome == "copied"
        reused += outcome == "reused"
    after = _inventory(source)
    if after != before:
        raise BackupRefusal(
            "the run tree changed while it was being copied; no current backup snapshot was published"
        )
    record = {
        "schema": SCHEMA,
        "run_id": checked_run_id,
        "files": [
            {"relative_path": relative, "sha256": digest}
            for relative, digest in sorted(before.items())
        ],
    }
    data = canonical_bytes(record)
    snapshot_sha256 = digest_bytes(data)
    _publish_bytes(root / "snapshots" / "sha256" / f"{snapshot_sha256}.json", data)
    return BackupReport(snapshot_sha256, copied, reused)


def resolve_backup_paths(run_root: Path, run_id: str, mac_directory: Path) -> tuple[Path, Path]:
    """The source run tree and the backup root, checked before either is written.

    Separate from `sync_run_tree` so the operator command can apply the same
    rules *before* it prepares the destination layout.  It creates those
    directories itself, because the custody allowance deliberately grants
    publication and not directory creation -- and a destination that fails these
    checks would have had them created inside the run tree first.
    """

    checked_run_id = validate_run_id(run_id)
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
    return source, root


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
    settle a bind mount and a hard-linked directory with the same reading.

    Checked in both directions.  A backup directory *inside* the run tree was
    caught only by the after-the-fact inventory recheck, which is to say only
    after the tool had copied the whole sealed tree into the sealed tree, and
    only after custody had granted that path -- inside the run tree -- as the
    one place the credential-free child was allowed to write.
    """
    target = _identity(ancestor)
    if target is None:
        return False
    return any(_identity(candidate) == target for candidate in (descendant, *descendant.parents))


def _inventory(source: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise BackupRefusal(f"run tree member {relative!r} is a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BackupRefusal(f"run tree member {relative!r} is not a regular file")
        inventory[relative] = _sha256(path)
    if not inventory:
        raise BackupRefusal("the selected run tree has no files to back up")
    return inventory


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupRefusal(f"run tree member {path.name!r} could not be read: {error}") from error
    return digest.hexdigest()


def _copy_verified(source: Path, target: Path, expected_sha256: str) -> str:
    if target.exists():
        if not target.is_file() or target.is_symlink() or _sha256(target) != expected_sha256:
            raise BackupRefusal(
                f"backup object {target.name!r} already exists but does not verify; it was not overwritten"
            )
        return "reused"
    target.parent.mkdir(parents=True, exist_ok=True)
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
            if not target.is_file() or target.is_symlink() or _sha256(target) != expected_sha256:
                raise BackupRefusal(
                    f"backup object {target.name!r} appeared but does not verify; it was not overwritten"
                ) from None
            return "reused"
        if _sha256(target) != expected_sha256:
            raise BackupRefusal(f"backup object {target.name!r} did not verify after copy")
        return "copied"
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _link_or_refuse(temporary: Path, target: Path) -> None:
    """`os.link`, with the one setup fact an operator can act on named.

    `FileExistsError` is deliberately let through: both callers verify the bytes
    that won the name.  A plain `O_CREAT | O_EXCL` write is deliberately not
    substituted for the link, for the reason `_atomic_create` gives -- it would
    publish the final name before the bytes were in it, which is the failure
    both call sites exist to prevent.
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
    """Publish by the same write-temp-then-link idiom `_copy_verified` uses.

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
    target.parent.mkdir(parents=True, exist_ok=True)
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
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
