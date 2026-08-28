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
import secrets
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Iterator

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.contracts.identities import validate_run_id
from common.runtree.store import RunTree

SCHEMA = "mac-run-backup.v2"
CHUNK_BYTES = 1024 * 1024
MAX_BACKUP_FILES = 1_000_000
MAX_BACKUP_ENTRIES = 1_000_000
MAX_DIRECTORY_DEPTH = 128
MAX_RELATIVE_PATH_BYTES = 16 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
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
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                or count > MAX_BACKUP_FILES
            ):
                raise BackupRefusal(
                    f"backup worker report field {field!r} must be a non-negative integer "
                    f"no larger than {MAX_BACKUP_FILES}"
                )
            counts[field] = count
        if counts["copied"] + counts["reused"] == 0:
            raise BackupRefusal("backup worker report claims a successful snapshot of no files")
        if counts["copied"] + counts["reused"] > MAX_BACKUP_FILES:
            raise BackupRefusal(f"backup worker report claims more than {MAX_BACKUP_FILES} files")
        return cls(snapshot_sha256, counts["copied"], counts["reused"])


def sync_run_tree(
    run_root: Path,
    run_id: str,
    mac_directory: Path,
    *,
    expected_source_identity: tuple[int, int] | None = None,
    expected_destination_identities: tuple[tuple[int, int], ...] | None = None,
) -> BackupReport:
    """Copy one run's regular files into a verified, append-only local store.

    A run may be resumed while this command is running.  We therefore scan it
    before and after the copy and refuse to publish a snapshot if either view
    differs: a snapshot must name one coherent tree, never a silent mixture of
    two stage boundaries.
    """

    source, root = resolve_backup_paths(run_root, run_id, mac_directory)
    prepare_backup_layout(source, root)
    # Keep the requested id: an in-root symlink may resolve to a differently
    # named directory, but the snapshot must record the run the operator named.
    try:
        managed_paths = RunTree(run_root, run_id).inventory_scope()
    except ContractError as error:
        raise BackupRefusal(f"the selected run tree could not be bound: {error}") from error
    with _open_directory(source, what="source run tree") as source_descriptor:
        _require_descriptor_identity(
            source_descriptor, expected_source_identity, what="source run tree"
        )
        with _opened_destination(
            root, expected_identities=expected_destination_identities
        ) as destination:
            before, before_temporaries = _inventory_descriptor(source_descriptor, managed_paths)
            copied = reused = 0
            for relative, digest in before.items():
                outcome = _copy_verified(
                    source_descriptor,
                    relative,
                    destination.objects,
                    root / "objects" / "sha256" / digest,
                    digest,
                )
                copied += outcome == "copied"
                reused += outcome == "reused"
            after, after_temporaries = _inventory_descriptor(source_descriptor, managed_paths)
            if (after, after_temporaries) != (before, before_temporaries):
                raise BackupRefusal(
                    "the run tree changed while it was being copied; no current backup "
                    "snapshot was published"
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
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise BackupRefusal(
                    f"backup snapshot is larger than {MAX_SNAPSHOT_BYTES} bytes; it was not "
                    "published"
                )
            snapshot_sha256 = digest_bytes(data)
            _publish_bytes(
                destination.snapshots,
                f"{snapshot_sha256}.json",
                root / "snapshots" / "sha256" / f"{snapshot_sha256}.json",
                data,
            )
            report = BackupReport(snapshot_sha256, copied, reused)
            _verify_backup_snapshot(destination, run_id, report)
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
    try:
        requested_root = Path(run_root).resolve()
        source = (requested_root / checked_run_id).resolve()
    except (OSError, RuntimeError) as error:
        raise BackupRefusal(f"the selected run tree could not be resolved: {error}") from error
    if not source.is_relative_to(requested_root):
        raise BackupRefusal("run id resolves outside the selected run root")
    if not source.is_dir():
        raise BackupRefusal(f"run {run_id!r} does not exist below the selected run root")
    requested_destination = Path(mac_directory).absolute()
    if requested_destination.is_symlink():
        raise BackupRefusal(
            f"backup layout path {requested_destination} is a symbolic link; no backup path "
            "may redirect"
        )
    try:
        root = requested_destination.resolve()
    except (OSError, RuntimeError) as error:
        raise BackupRefusal(f"the Mac backup directory could not be resolved: {error}") from error
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


def prepare_backup_layout(source: Path, root: Path) -> None:
    """Create each child through its already-open, no-follow parent descriptor.

    Public because the trusted parent must run it too: custody grants the
    confined child publication into the destination but not directory
    creation, so `cli._backup_in_custody` builds the closed layout before the
    worker starts. A step another module is required to call is part of this
    module's surface, not a private detail a rename could quietly break.
    """

    _validate_backup_layout(source, root)
    try:
        root.mkdir(exist_ok=True)
    except OSError as error:
        raise BackupRefusal(f"backup layout path {root} could not be created: {error}") from error
    descriptors: list[int] = []
    try:
        root_descriptor = _open_directory_descriptor(root, what=f"backup layout path {root}")
        descriptors.append(root_descriptor)
        parent = root_descriptor
        parent_display = root
        for name in ("objects", "sha256"):
            display = parent_display / name
            try:
                os.mkdir(name, dir_fd=parent)
            except FileExistsError:
                pass
            except OSError as error:
                raise BackupRefusal(
                    f"backup layout path {display} could not be created: {error}"
                ) from error
            opened = _open_directory_descriptor(
                name, parent_descriptor=parent, what=f"backup layout path {display}"
            )
            descriptors.append(opened)
            parent = opened
            parent_display = display
        parent = root_descriptor
        parent_display = root
        for name in ("snapshots", "sha256"):
            display = parent_display / name
            try:
                os.mkdir(name, dir_fd=parent)
            except FileExistsError:
                pass
            except OSError as error:
                raise BackupRefusal(
                    f"backup layout path {display} could not be created: {error}"
                ) from error
            opened = _open_directory_descriptor(
                name, parent_descriptor=parent, what=f"backup layout path {display}"
            )
            descriptors.append(opened)
            parent = opened
            parent_display = display
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    _validate_backup_layout(source, root)


def _open_directory_descriptor(
    path: Path | str, *, what: str, parent_descriptor: int | None = None
) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise BackupRefusal(
            "this platform cannot open backup directories without following symbolic links"
        )
    flags = os.O_RDONLY | os.O_NONBLOCK | no_follow | directory
    try:
        descriptor = (
            os.open(path, flags)
            if parent_descriptor is None
            else os.open(path, flags, dir_fd=parent_descriptor)
        )
    except OSError as error:
        raise BackupRefusal(
            f"{what} could not be opened without following a redirect: {error}"
        ) from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise BackupRefusal(f"{what} is not a directory when opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _open_directory(path: Path, *, what: str) -> Iterator[int]:
    descriptor = _open_directory_descriptor(path, what=what)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _DestinationDescriptors:
    root: int
    objects_parent: int
    objects: int
    snapshots_parent: int
    snapshots: int


@contextmanager
def _opened_destination(
    root: Path, *, expected_identities: tuple[tuple[int, int], ...] | None = None
) -> Iterator[_DestinationDescriptors]:
    descriptors: list[int] = []
    try:
        root_descriptor = _open_directory_descriptor(root, what=f"backup layout path {root}")
        descriptors.append(root_descriptor)
        objects_parent = _open_directory_descriptor(
            "objects",
            parent_descriptor=root_descriptor,
            what=f"backup layout path {root / 'objects'}",
        )
        descriptors.append(objects_parent)
        objects = _open_directory_descriptor(
            "sha256",
            parent_descriptor=objects_parent,
            what=f"backup layout path {root / 'objects' / 'sha256'}",
        )
        descriptors.append(objects)
        snapshots_parent = _open_directory_descriptor(
            "snapshots",
            parent_descriptor=root_descriptor,
            what=f"backup layout path {root / 'snapshots'}",
        )
        descriptors.append(snapshots_parent)
        snapshots = _open_directory_descriptor(
            "sha256",
            parent_descriptor=snapshots_parent,
            what=f"backup layout path {root / 'snapshots' / 'sha256'}",
        )
        descriptors.append(snapshots)
        opened = _DestinationDescriptors(
            root_descriptor, objects_parent, objects, snapshots_parent, snapshots
        )
        observed = tuple(_descriptor_identity(descriptor) for descriptor in descriptors)
        if expected_identities is not None and observed != expected_identities:
            raise BackupRefusal(
                "the Mac backup layout changed filesystem identity before it could be used"
            )
        yield opened
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    details = os.fstat(descriptor)
    return (details.st_dev, details.st_ino)


def required_identity(path: Path, *, what: str) -> tuple[int, int]:
    """Return the directory identity that a custody child must see again."""

    with _open_directory(path, what=what) as descriptor:
        return _descriptor_identity(descriptor)


def destination_identities(root: Path) -> tuple[tuple[int, int], ...]:
    """Bind every fixed layout directory across the parent/child boundary."""

    with _opened_destination(root) as destination:
        return (
            _descriptor_identity(destination.root),
            _descriptor_identity(destination.objects_parent),
            _descriptor_identity(destination.objects),
            _descriptor_identity(destination.snapshots_parent),
            _descriptor_identity(destination.snapshots),
        )


def _require_descriptor_identity(
    descriptor: int, expected: tuple[int, int] | None, *, what: str
) -> None:
    if expected is not None and _descriptor_identity(descriptor) != expected:
        raise BackupRefusal(f"the {what} changed filesystem identity before it could be used")


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


def _inventory_descriptor(
    source_descriptor: int, managed_paths: tuple[str, ...]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Inventory one anchored tree without following a component or reopening a leaf."""

    inventory: dict[str, str] = {}
    publication_temporaries: list[str] = []
    mac_spellings: dict[str, str] = {}
    encountered = 0
    # A fresh open file description, not `os.dup`. `sync_run_tree` scans the
    # same anchored root twice and compares the two views, and `dup` shares the
    # directory offset with the caller's descriptor: on Linux `getdents64`
    # advances that shared offset, so the second pass would start at end of
    # directory, see an empty tree, and refuse a backup that had in fact just
    # been copied. macOS does not advance it, which is why this was invisible
    # here. `openat` on "." re-anchors the same directory the caller already
    # inspected -- "." cannot be a symlink -- at offset zero, so neither pass
    # depends on unspecified `fdopendir` positioning.
    root_descriptor = _open_directory_descriptor(
        ".", parent_descriptor=source_descriptor, what="source run tree"
    )
    if _descriptor_identity(root_descriptor) != _descriptor_identity(source_descriptor):
        os.close(root_descriptor)
        raise BackupRefusal("the source run tree changed filesystem identity before it was scanned")
    try:
        root_entries = os.scandir(root_descriptor)
    except OSError as error:
        os.close(root_descriptor)
        raise BackupRefusal("run tree directory '.' could not be listed") from error
    stack = [(root_descriptor, root_entries, "")]
    try:
        while stack:
            directory_descriptor, entries, prefix = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                os.close(directory_descriptor)
                stack.pop()
                continue
            except OSError as error:
                raise BackupRefusal(
                    f"run tree directory {prefix or '.'!r} could not be listed: {error}"
                ) from error
            encountered += 1
            if encountered > MAX_BACKUP_ENTRIES:
                raise BackupRefusal(
                    f"the selected run tree contains more than {MAX_BACKUP_ENTRIES} entries"
                )
            name = entry.name
            relative = f"{prefix}/{name}" if prefix else name
            _validate_relative_path(relative)
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise BackupRefusal(
                    f"run tree member {relative!r} could not be inspected: {error}"
                ) from error
            if stat.S_ISLNK(details.st_mode):
                raise BackupRefusal(f"run tree member {relative!r} is a symbolic link")
            if stat.S_ISDIR(details.st_mode):
                if len(stack) >= MAX_DIRECTORY_DEPTH:
                    raise BackupRefusal(
                        f"the selected run tree nests deeper than {MAX_DIRECTORY_DEPTH} directories"
                    )
                child = _open_directory_descriptor(
                    name,
                    parent_descriptor=directory_descriptor,
                    what=f"run tree member {relative!r}",
                )
                try:
                    child_entries = os.scandir(child)
                except OSError as error:
                    os.close(child)
                    raise BackupRefusal(
                        f"run tree directory {relative!r} could not be listed: {error}"
                    ) from error
                stack.append((child, child_entries, relative))
                continue
            if not stat.S_ISREG(details.st_mode):
                raise BackupRefusal(f"run tree member {relative!r} is not a regular file")
            _record_mac_spelling(relative, mac_spellings)
            if _is_publication_temporary(relative, managed_paths):
                publication_temporaries.append(relative)
                continue
            descriptor = _open_regular_descriptor(
                name,
                directory_descriptor,
                what=f"run tree member {relative!r}",
            )
            inventory[relative] = _sha256_descriptor(
                descriptor, what=f"run tree member {relative!r}"
            )
    finally:
        for directory_descriptor, entries, _prefix in reversed(stack):
            entries.close()
            os.close(directory_descriptor)
    if not inventory:
        raise BackupRefusal("the selected run tree has no files to back up")
    return inventory, tuple(sorted(publication_temporaries))


def _validate_relative_path(relative: str) -> tuple[str, ...]:
    try:
        encoded = relative.encode("utf-8")
    except UnicodeEncodeError as error:
        raise BackupRefusal(
            "a run tree member name is not valid UTF-8 and cannot enter canonical JSON"
        ) from error
    components = tuple(relative.split("/"))
    if (
        not relative
        or len(encoded) > MAX_RELATIVE_PATH_BYTES
        or relative.startswith("/")
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise BackupRefusal(
            f"run tree relative path is not a safe path of at most {MAX_RELATIVE_PATH_BYTES} "
            "UTF-8 bytes"
        )
    return components


def _mac_path_key(relative: str) -> str:
    return unicodedata.normalize("NFD", relative).casefold()


def _record_mac_spelling(relative: str, spellings: dict[str, str]) -> None:
    """Refuse any prefix whose spelling collapses on default APFS."""

    components = _validate_relative_path(relative)
    for length in range(1, len(components) + 1):
        spelling = "/".join(components[:length])
        key = _mac_path_key(spelling)
        previous = spellings.setdefault(key, spelling)
        if previous != spelling:
            raise BackupRefusal(
                f"run tree paths {previous!r} and {spelling!r} collide on default APFS"
            )


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


def _open_regular_descriptor(name: str, parent_descriptor: int, *, what: str) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise BackupRefusal(
            "this platform cannot open backup files without following symbolic links"
        )
    flags = os.O_RDONLY | os.O_NONBLOCK | no_follow
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise BackupRefusal(
            f"{what} is not a regular file or could not be opened without following a "
            f"redirect: {error}"
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupRefusal(f"{what} is not a regular file when opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_relative_regular(source_descriptor: int, relative: str, *, what: str) -> int:
    components = _validate_relative_path(relative)
    parent = os.dup(source_descriptor)
    try:
        for component in components[:-1]:
            child = _open_directory_descriptor(component, parent_descriptor=parent, what=what)
            os.close(parent)
            parent = child
        return _open_regular_descriptor(components[-1], parent, what=what)
    finally:
        os.close(parent)


def _stable_file_metadata(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _sha256_descriptor(descriptor: int, *, what: str) -> str:
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = _stable_file_metadata(os.fstat(handle.fileno()))
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                digest.update(chunk)
            after = _stable_file_metadata(os.fstat(handle.fileno()))
    except OSError as error:
        raise BackupRefusal(f"{what} could not be read: {error}") from error
    if before != after:
        raise BackupRefusal(f"{what} changed while it was being read")
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _existing_digest(directory_descriptor: int, name: str, *, what: str) -> str | None:
    try:
        descriptor = _open_regular_descriptor(name, directory_descriptor, what=what)
    except BackupRefusal as refusal:
        if isinstance(refusal.__cause__, FileNotFoundError):
            return None
        raise
    return _sha256_descriptor(descriptor, what=what)


def _temporary_regular(directory_descriptor: int, *, prefix: str) -> tuple[int, str]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise BackupRefusal(
            "this platform cannot create backup temporaries without following links"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
    for _attempt in range(128):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_descriptor), name
        except FileExistsError:
            continue
        except OSError as error:
            raise BackupRefusal(f"a backup temporary could not be created: {error}") from error
    raise BackupRefusal("a unique backup temporary name could not be created")


def _copy_verified(
    source_descriptor: int,
    relative: str,
    objects_descriptor: int,
    target: Path,
    expected_sha256: str,
) -> str:
    what = f"backup object {target.name!r}"
    existing = _existing_digest(objects_descriptor, target.name, what=what)
    if existing is not None:
        if existing != expected_sha256:
            raise BackupRefusal(
                f"{what} already exists but does not verify; it was not overwritten"
            )
        return "reused"
    source = _open_relative_regular(
        source_descriptor, relative, what=f"run tree member {relative!r}"
    )
    try:
        temporary_descriptor, temporary_name = _temporary_regular(
            objects_descriptor, prefix=".backup-"
        )
    except BaseException:
        os.close(source)
        raise
    try:
        digest = hashlib.sha256()
        with (
            os.fdopen(temporary_descriptor, "wb") as destination,
            os.fdopen(source, "rb") as origin,
        ):
            before = _stable_file_metadata(os.fstat(origin.fileno()))
            for chunk in iter(lambda: origin.read(CHUNK_BYTES), b""):
                digest.update(chunk)
                destination.write(chunk)
            after = _stable_file_metadata(os.fstat(origin.fileno()))
            destination.flush()
            os.fsync(destination.fileno())
        if before != after:
            raise BackupRefusal(f"source {relative!r} changed while it was being copied")
        if digest.hexdigest() != expected_sha256:
            raise BackupRefusal(f"source {relative!r} changed while it was being copied")
        try:
            _link_or_refuse(temporary_name, target.name, objects_descriptor, target_display=target)
        except FileExistsError:
            appeared = _existing_digest(objects_descriptor, target.name, what=what)
            if appeared != expected_sha256:
                raise BackupRefusal(
                    f"{what} appeared but does not verify; it was not overwritten"
                ) from None
            return "reused"
        if _existing_digest(objects_descriptor, target.name, what=what) != expected_sha256:
            raise BackupRefusal(f"{what} did not verify after copy")
        return "copied"
    finally:
        try:
            os.unlink(temporary_name, dir_fd=objects_descriptor)
        except FileNotFoundError:
            pass


def _link_or_refuse(
    temporary: str, target: str, directory_descriptor: int, *, target_display: Path
) -> None:
    """Publish atomically while translating unsupported hard links for the operator.

    `FileExistsError` must reach the caller so it can verify the bytes that won
    the name. A direct exclusive write would expose the final name before its
    content was complete.
    """
    try:
        os.link(
            temporary,
            target,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno in _NO_HARD_LINKS:
            raise BackupRefusal(
                f"a backup member could not take its final name under {target_display.parent}: that "
                f"filesystem refuses hard links ({error.strerror}). A member is linked into "
                "place so that a partly written file can never wear a digest's name, so "
                "--mac-directory has to name a directory on a filesystem that supports "
                "links -- an exFAT or FAT32 volume, an SMB or AFP share, and some "
                "sync-provider folders do not"
            ) from error
        raise


def _read_regular_bytes(
    directory_descriptor: int, name: str, *, what: str, limit: int
) -> bytes | None:
    try:
        descriptor = _open_regular_descriptor(name, directory_descriptor, what=what)
    except BackupRefusal as refusal:
        if isinstance(refusal.__cause__, FileNotFoundError):
            return None
        raise
    try:
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        raise BackupRefusal(f"{what} could not be read: {error}") from error
    if len(data) > limit:
        raise BackupRefusal(f"{what} is larger than {limit} bytes and was not read")
    return data


def _refuse_a_different_snapshot(snapshots_descriptor: int, name: str, data: bytes) -> bool:
    """Refuse a taken snapshot name unless it is already exactly these bytes.

    A symlink or a non-regular file is refused rather than followed, matching
    `_copy_verified`.  A published snapshot is an immutable index into the
    store; a link whose destination can change afterwards is not that, even on
    the pass where the bytes it points at happen to agree.
    """
    existing = _read_regular_bytes(
        snapshots_descriptor,
        name,
        what="backup snapshot",
        limit=len(data),
    )
    if existing is None:
        return False
    if existing != data:
        raise BackupRefusal(
            "backup snapshot path already names different bytes; it was not overwritten"
        )
    return True


def _publish_bytes(snapshots_descriptor: int, name: str, target: Path, data: bytes) -> None:
    """Never expose a final snapshot name before its content is written and synced.

    A direct `O_CREAT | O_EXCL` open makes the final name exist before the
    bytes are in it; a kill between that open and the write leaves a
    permanently unpublishable snapshot path, since every retry finds the
    empty file already there and refuses rather than completing it. Writing
    a same-directory temporary first and linking it into place only after it
    is flushed and synced means a crash can only ever leave a stray
    temporary behind, never a partial file at the final name.

    Every operation is relative to the already-open snapshots directory. The
    final-name read uses ``O_NOFOLLOW`` and the publication link uses dir-fds,
    so neither a leaf nor a swapped parent can redirect the operation.
    """
    if _refuse_a_different_snapshot(snapshots_descriptor, name, data):
        return
    descriptor, temporary = _temporary_regular(snapshots_descriptor, prefix=".snapshot-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _link_or_refuse(temporary, name, snapshots_descriptor, target_display=target)
        except FileExistsError:
            if not _refuse_a_different_snapshot(snapshots_descriptor, name, data):
                raise BackupRefusal("backup snapshot disappeared during publication") from None
        else:
            published = _read_regular_bytes(
                snapshots_descriptor,
                name,
                what="backup snapshot",
                limit=len(data),
            )
            if published != data:
                raise BackupRefusal("backup snapshot did not verify after publication")
    finally:
        try:
            os.unlink(temporary, dir_fd=snapshots_descriptor)
        except FileNotFoundError:
            pass


def verify_backup_snapshot(
    root: Path,
    run_id: str,
    report: BackupReport,
    *,
    expected_destination_identities: tuple[tuple[int, int], ...] | None = None,
) -> dict[str, object]:
    """Read back the snapshot and every object before reporting backup success."""

    with _opened_destination(
        root, expected_identities=expected_destination_identities
    ) as destination:
        return _verify_backup_snapshot(destination, run_id, report)


def _verify_backup_snapshot(
    destination: _DestinationDescriptors, run_id: str, report: BackupReport
) -> dict[str, object]:
    name = f"{report.snapshot_sha256}.json"
    data = _read_regular_bytes(
        destination.snapshots,
        name,
        what="backup snapshot",
        limit=MAX_SNAPSHOT_BYTES,
    )
    if data is None:
        raise BackupRefusal("backup worker reported a snapshot that does not exist")
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
    if not isinstance(files, list) or not files or len(files) > MAX_BACKUP_FILES:
        raise BackupRefusal("backup snapshot has no file inventory")
    checked_rows: list[tuple[str, str]] = []
    mac_spellings: dict[str, str] = {}
    for row in files:
        if not isinstance(row, dict) or set(row) != {"relative_path", "sha256"}:
            raise BackupRefusal("backup snapshot has a malformed file row")
        relative, digest = row["relative_path"], row["sha256"]
        if not isinstance(relative, str) or not _is_sha256(digest):
            raise BackupRefusal("backup snapshot has a malformed relative path or sha256")
        try:
            _record_mac_spelling(relative, mac_spellings)
        except BackupRefusal as error:
            raise BackupRefusal(
                f"backup snapshot has an unsafe or APFS-colliding path: {error}"
            ) from error
        checked_rows.append((relative, digest))
    if checked_rows != sorted(set(checked_rows)):
        raise BackupRefusal("backup snapshot file inventory is not sorted and unique")
    temporaries = value["excluded_publication_temporaries"]
    if (
        not isinstance(temporaries, list)
        or len(temporaries) > MAX_BACKUP_FILES
        or len(checked_rows) + len(temporaries) > MAX_BACKUP_FILES
        or any(not isinstance(path, str) or not path for path in temporaries)
        or temporaries != sorted(set(temporaries))
    ):
        raise BackupRefusal("backup snapshot has a malformed publication-temporary inventory")
    for relative in temporaries:
        try:
            _record_mac_spelling(relative, mac_spellings)
        except BackupRefusal as error:
            raise BackupRefusal(
                f"backup snapshot has an unsafe or APFS-colliding temporary path: {error}"
            ) from error
    if len(checked_rows) != report.copied + report.reused:
        raise BackupRefusal("backup worker report counts do not reconcile with its snapshot")
    for digest in sorted({digest for _relative, digest in checked_rows}):
        if (
            _existing_digest(destination.objects, digest, what=f"backup object {digest!r}")
            != digest
        ):
            raise BackupRefusal(f"backup object {digest!r} does not verify on read-back")
    return value
