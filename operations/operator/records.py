"""Small immutable records owned by the operator surface.

These records join backend receipts that currently have no common index.  The
surface writes only during a mutating verb.  ``status`` uses the read methods
only and never creates a directory, a marker, or an observation of its own.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Final, Iterator

from common.contracts.canonical import canonical_bytes as _pipeline_canonical_bytes
from common.durability import sync_directory

UTC = timezone.utc
SCHEMA = "operator-receipt.v1"
DESCRIPTOR_SCHEMA = "operator-surface.v2"

RECEIPTS_DIRECTORY: Final = "receipts"
"""The one directory under a state root that holds receipts.

Named once because two things resolve it: the store that writes receipts and
the descriptor that indexes them by name. A descriptor entry is a receipt's
basename, never a path, since a receipt is content-addressed and its
directory is wherever the state root is now; an absolute path would bind
every receipt to the machine it was written on.
"""

BLOCK_BYTES: Final = 1024 * 1024

MAX_RECORD_BYTES: Final = 4 * 1024 * 1024
"""How large one of these files may be before reading it is itself the failure.

Both readers below load a whole file before they can check anything about
it: measured, a 600 MiB file costs 1.8 GiB resident, and a larger one ends
as an OOM kill that prints nothing. The largest receipt written here is a
few kilobytes, so only a file this tool did not write can reach four
mebibytes.
"""


class RecordError(RuntimeError):
    """A record cannot be safely read or written."""


def canonical_bytes(value: object) -> bytes:
    """The stable bytes used for an immutable operator receipt.

    The pipeline's one canonical serialization, not a second
    reimplementation: same key order, same UTF-8 text, and the same refusal
    of a raw float. A trailing newline is added so a receipt reads as an
    ordinary text file when opened directly.
    """

    return _pipeline_canonical_bytes(value) + b"\n"


def utc_stamp(value: datetime) -> str:
    """The one spelling of an instant this surface writes or shows."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise RecordError("operator receipt time must be UTC")
    return value.isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    """The one spelling of a file digest, read in blocks rather than whole.

    Opened non-blocking, no-follow, and refused unless the open descriptor
    says it is a regular file, not the name, which can change between the
    check and the open. A FIFO left at a recorded path would otherwise
    block the open, and `status` would hang having printed nothing; a
    planted symlink would be read through to bytes this store never wrote
    and cannot vouch for (`operations/pod/transfer.py` closes the same gap
    the same way).
    """

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(errno.EINVAL, "a digest needs a regular file", str(path))
        for block in iter(lambda: handle.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def bounded_bytes(path: str | Path, subject: str, *, dir_fd: int | None = None) -> bytes:
    """Read a whole record, or refuse a file too large to be one of ours.

    Opened the same way `sha256_file` opens one, and for the same reason.
    ``dir_fd`` resolves ``path`` as a single entry relative to an already
    open directory, so the directory it applies to cannot be swapped
    underneath it.
    """

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(errno.EINVAL, "a record needs a regular file", str(path))
        data = handle.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise RecordError(f"{subject} is larger than {MAX_RECORD_BYTES} bytes and was not read")
    return data


class ReceiptStore:
    """Content-addressed receipts, with no mutable current-state pointer.

    Callers retain the returned path in the next receipt when they need a chain.
    This intentionally avoids a "latest" pointer that status could accidentally
    rewrite or silently treat as authority.
    """

    def __init__(self, root: str | Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.root = Path(root)
        self.now = now or (lambda: datetime.now(UTC))

    @property
    def receipts(self) -> Path:
        return self.root / RECEIPTS_DIRECTORY

    def write(self, kind: str, payload: dict[str, Any]) -> Path:
        """Write a single immutable fact and return its content-addressed path."""

        if (
            not isinstance(kind, str)
            or not kind
            or any(char not in "abcdefghijklmnopqrstuvwxyz-" for char in kind)
        ):
            raise RecordError("operator receipt kind must use lowercase letters and hyphens")
        if not isinstance(payload, dict):
            raise RecordError("operator receipt payload must be an object")
        record = {
            "schema": SCHEMA,
            "kind": kind,
            "recorded_at": utc_stamp(self.now()),
            "payload": payload,
        }
        try:
            encoded = canonical_bytes(record)
        except TypeError as error:
            raise RecordError(f"operator receipt payload is not serializable: {error}") from error
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.receipts / f"{kind}-{digest}.json"
        self.receipts.mkdir(parents=True, exist_ok=True)
        if not self.receipts.is_dir() or self.receipts.is_symlink():
            # mkdir(exist_ok=True) treats an existing directory-symlink as
            # already satisfied and leaves it in place — the same condition
            # list() already refuses, checked here before anything is written
            # through it.
            raise RecordError("operator receipt directory is not a safe directory")
        _atomic_create_or_reuse(target, encoded)
        return target

    def read(self, path: str | Path) -> dict[str, Any]:
        """Read one receipt after validating its closed shape and its filename digest."""

        candidate = Path(path)
        try:
            resolved_root = self.receipts.resolve()
            resolved = candidate.resolve()
        except OSError as error:
            raise RecordError("operator receipt path cannot be resolved") from error
        if not resolved.is_relative_to(resolved_root):
            raise RecordError("operator receipt path is outside the receipt directory")
        try:
            data = bounded_bytes(resolved, "operator receipt")
        except OSError as error:
            raise RecordError(f"operator receipt cannot be read: {resolved.name}") from error
        return _validated(resolved.name, data)

    def _read_at(self, directory: int, name: str) -> dict[str, Any]:
        """Read one receipt as an entry of the directory already open as ``directory``.

        The same reader as `read`, minus the part that resolves a name a
        second time: containment is established by the descriptor, so there
        is no window in which the entry's directory can change underneath it.
        """

        try:
            data = bounded_bytes(name, "operator receipt", dir_fd=directory)
        except OSError as error:
            raise RecordError(f"operator receipt cannot be read: {name}") from error
        return _validated(name, data)

    @contextmanager
    def _bound_receipts(self) -> Iterator[int | None]:
        """Open the receipt directory once and hold it open for the whole read.

        The descriptor is the directory, not a name resolved to it once, so
        every entry below is enumerated relative to the object that passed
        the check, and a process replacing the directory in between cannot
        substitute its own files for this store's history.

        ``None`` means there is nothing recorded yet, which stays an empty
        history rather than a failure; every other refusal to open it is
        the unsafe location the caller must hear about instead.
        """

        try:
            descriptor = os.open(self.receipts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            # A dangling link answers `O_NOFOLLOW` with ELOOP or ENOTDIR
            # rather than ENOENT, so this branch is the truly absent
            # directory; the lstat confirms that.
            try:
                os.lstat(self.receipts)
            except OSError:
                yield None
                return
            raise RecordError("operator receipt directory is not a safe directory") from error
        except OSError as error:
            raise RecordError("operator receipt directory is not a safe directory") from error
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def list(self) -> list[tuple[Path, dict[str, Any]]]:
        """Read existing receipts only; absent storage is an empty, not a created, state."""

        with self._bound_receipts() as directory:
            if directory is None:
                return []
            return [
                (self.receipts / name, self._read_at(directory, name))
                for name in _entries(directory, "")
            ]

    def records_of_kind(self, kind: str) -> list[tuple[Path, dict[str, Any]]]:
        return [(path, record) for path, record in self.list() if record["kind"] == kind]

    def readable_records_of_kind(
        self, kind: str
    ) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
        """Read one kind while naming its failures beside the records that survive.

        The filename prefix avoids unrelated failures, but hyphenated kinds can
        make the prefix overmatch; only the validated record's exact kind decides.
        """

        loaded: list[tuple[Path, dict[str, Any]]] = []
        unreadable: list[str] = []
        with self._bound_receipts() as directory:
            if directory is None:
                return [], []
            for name in _entries(directory, f"{kind}-"):
                try:
                    linked = stat.S_ISLNK(
                        os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
                    )
                except OSError:
                    # The entry was listed and is now gone or unstattable;
                    # named rather than silently dropped.
                    unreadable.append(f"{name}: it could not be examined")
                    continue
                if linked:
                    # `read` validates the resolved name against the bytes
                    # it hashed, so a link may carry any name at all. A
                    # receipt is a file this store created, not a name
                    # pointing at one.
                    unreadable.append(
                        f"{name}: it is a link rather than a receipt this store wrote"
                    )
                    continue
                try:
                    record = self._read_at(directory, name)
                except RecordError as error:
                    unreadable.append(f"{name}: {error}")
                    continue
                if record["kind"] == kind:
                    loaded.append((self.receipts / name, record))
        return loaded, unreadable


def _entries(directory: int, prefix: str) -> list[str]:
    """The receipt filenames of an open directory, sorted, dot names excluded.

    A dot name here is one of `_sealed_temporary`'s partial files.
    """

    try:
        names = os.listdir(directory)
    except OSError as error:
        raise RecordError("operator receipt directory could not be read") from error
    return sorted(
        name
        for name in names
        if not name.startswith(".") and name.startswith(prefix) and name.endswith(".json")
    )


def _validated(name: str, data: bytes) -> dict[str, Any]:
    """Every check a receipt's bytes must pass, wherever the descriptor came from."""

    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecordError(f"operator receipt cannot be read: {name}") from error
    try:
        # A saved file can contain a raw float or a non-string key, which the
        # shared serializer refuses; this must arrive as RecordError, since
        # `status` only reports an unreadable record beside intact ones for
        # that type.
        canonical = canonical_bytes(record)
    except TypeError as error:
        raise RecordError(f"operator receipt is not canonical: {name}") from error
    if canonical != data:
        raise RecordError(f"operator receipt is not canonical: {name}")
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "kind",
        "recorded_at",
        "payload",
    }:
        raise RecordError(f"operator receipt has an invalid shape: {name}")
    if (
        record["schema"] != SCHEMA
        or not isinstance(record["kind"], str)
        or not isinstance(record["recorded_at"], str)
        or not isinstance(record["payload"], dict)
    ):
        raise RecordError(f"operator receipt has invalid fields: {name}")
    try:
        recorded_at = datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00"))
        if utc_stamp(recorded_at) != record["recorded_at"]:
            raise RecordError("operator receipt time is not canonical UTC")
    except (ValueError, RecordError) as error:
        raise RecordError(f"operator receipt has an invalid time: {name}") from error
    digest = hashlib.sha256(data).hexdigest()
    if name != f"{record['kind']}-{digest}.json":
        raise RecordError(f"operator receipt kind or digest does not match its filename: {name}")
    return record


class DescriptorStore:
    """A small, self-hashed index of explicitly chosen receipt paths.

    It is a navigation aid, not evidence: every fact it names lives in an
    immutable receipt.  Keeping one named descriptor lets ``status`` read what
    this operator session declared without scanning a directory and choosing a
    record by timestamp or filename.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "operator-surface.json"
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.receipts = self.root / RECEIPTS_DIRECTORY

    def receipt_path(self, entry: str) -> Path:
        """Where a recorded receipt lives now, whatever the entry was written as.

        Only the basename is taken (see `RECEIPTS_DIRECTORY`), even from an
        older descriptor that recorded absolute paths, so a state directory
        that has since been copied or moved still reads through this index;
        the reader still checks the bytes against the digest in that name.
        """

        if not isinstance(entry, str) or not entry:
            raise RecordError("operator descriptor names a blank receipt")
        return self.receipts / PurePosixPath(entry).name

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Serialize the descriptor's read-modify-write across operator processes."""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+b")
        except OSError as error:
            raise RecordError("the operator descriptor lock could not be opened") from error
        try:
            import fcntl
        except ImportError:  # pragma: no cover - production operator and tests are POSIX
            fcntl = None  # type: ignore[assignment]
        locked = False
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError as error:
                    raise RecordError("the operator descriptor lock could not be taken") from error
                locked = True
            yield
        finally:
            if fcntl is not None and locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # Closing releases the POSIX lock; an unlock failure must
                    # not replace the protected body's more useful exception.
                    pass
            handle.close()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(bounded_bytes(self.path, "operator descriptor").decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecordError("operator descriptor cannot be read") from error
        if not isinstance(raw, dict):
            raise RecordError("operator descriptor has an invalid shape")
        if set(raw) != {"schema", "actions", "history", "self_hash"}:
            raise RecordError("operator descriptor has an invalid shape")
        expected = dict(raw)
        expected.pop("self_hash")
        try:
            digest = hashlib.sha256(canonical_bytes(expected)).hexdigest()
        except TypeError as error:
            raise RecordError("operator descriptor is not canonical") from error
        if raw["schema"] != DESCRIPTOR_SCHEMA or raw["self_hash"] != digest:
            raise RecordError("operator descriptor fails its own integrity check")
        if not _valid_actions(raw["actions"]) or not _valid_history(raw["history"], raw["actions"]):
            raise RecordError("operator descriptor action list is invalid")
        return raw

    def record(self, action: str, receipt: Path) -> dict[str, Any]:
        if not isinstance(action, str) or not action:
            raise RecordError("operator descriptor action must be non-blank")
        with self._lock():
            return self._record_unlocked(action, receipt)

    def _record_unlocked(self, action: str, receipt: Path) -> dict[str, Any]:
        current = self.load()
        actions = {} if current is None else dict(current["actions"])
        history = (
            {}
            if current is None
            else {name: list(paths) for name, paths in current["history"].items()}
        )
        try:
            resolved = receipt.resolve()
            inside = resolved.parent == self.receipts.resolve()
        except OSError as error:
            raise RecordError("operator receipt path cannot be resolved") from error
        if not inside:
            raise RecordError("operator descriptor indexes only receipts in its receipt directory")
        receipt_text = resolved.name
        actions[action] = receipt_text
        entries = history.setdefault(action, [])
        # Move-to-end, never skip-if-present: an idempotent retry reproduces
        # an earlier path exactly, and skipping it would leave history[-1]
        # naming something else.
        if receipt_text in entries:
            entries.remove(receipt_text)
        entries.append(receipt_text)
        record: dict[str, Any] = {
            "schema": DESCRIPTOR_SCHEMA,
            "actions": actions,
            "history": history,
        }
        record["self_hash"] = hashlib.sha256(canonical_bytes(record)).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(self.path, canonical_bytes(record))
        return record


def _valid_actions(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(receipt, str) for key, receipt in value.items()
    )


def _valid_history(value: object, actions: object) -> bool:
    if not isinstance(value, dict) or not isinstance(actions, dict) or set(value) != set(actions):
        return False
    return all(
        isinstance(action, str)
        and isinstance(receipts, list)
        and bool(receipts)
        and all(isinstance(receipt, str) for receipt in receipts)
        and receipts[-1] == actions[action]
        for action, receipts in value.items()
    )


def _sealed_temporary(target: Path, payload: bytes) -> Path:
    """Write the payload beside its target, owner-only and already on the disk."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return Path(temporary_name)


def _atomic_create_or_reuse(target: Path, payload: bytes) -> None:
    """Create a receipt once; identical bytes are a true no-op, never an overwrite."""

    try:
        temporary = _sealed_temporary(target, payload)
    except OSError as error:
        raise RecordError("operator receipt could not be written") from error
    try:
        try:
            os.link(temporary, target)
            try:
                sync_directory(target.parent, strict=True)
            except OSError as error:
                raise RecordError(
                    "the operator receipt was written but its directory entry could not be "
                    "made durable"
                ) from error
        except FileExistsError:
            try:
                existing = bounded_bytes(target, "existing operator receipt")
            except OSError as error:
                raise RecordError("existing operator receipt cannot be read") from error
            if existing != payload:
                raise RecordError(
                    "an operator receipt path already holds different evidence"
                ) from None
            try:
                sync_directory(target.parent, strict=True)
            except OSError as error:
                raise RecordError(
                    "the operator receipt exists but its directory entry could not be made durable"
                ) from error
    except OSError as error:
        raise RecordError("operator receipt could not be written") from error
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace(target: Path, payload: bytes) -> None:
    """Replace the non-evidentiary descriptor atomically after all facts are stored."""

    try:
        temporary = _sealed_temporary(target, payload)
    except OSError as error:
        raise RecordError("operator descriptor could not be written") from error
    try:
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RecordError("operator descriptor could not be written") from error
    try:
        sync_directory(target.parent, strict=True)
    except OSError as error:
        # The replace already succeeded, so "not written" here would
        # contradict what status then shows.
        raise RecordError(
            "the operator descriptor was written but its directory entry could not be made durable"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)
