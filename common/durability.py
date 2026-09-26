"""Publishing bytes under a name that survives both a crash and a power cut.

``fsync`` on a file persists its bytes, not the directory entry naming them; that
needs its own sync (``fsync(2)``). ``os.replace`` and ``os.link`` give atomic
visibility only. Whole-bytes path writers share these; dir_fd and streamed ones do not.
``common`` never imports the operational layer, which is why this lives here.
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from pathlib import Path

_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})


def sync_directory(path: Path, *, strict: bool = False) -> None:
    """Persist a directory entry, optionally refusing when durability is unproved.

    ``strict=False`` is best effort, for callers whose established behavior is to
    keep working on a filesystem that refuses to open or sync a directory — some
    network mounts and container bind mounts do.

    ``strict=True`` re-raises the ``OSError`` instead, for callers that must not
    report a durable success they cannot establish: money evidence, operator
    publication, and run-tree artifact publication.  The two failure points are
    kept separate on purpose — a directory that cannot be *opened* and one whose
    ``fsync`` fails are different facts, and the raised error names which.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        if strict:
            raise
        return
    try:
        os.fsync(descriptor)
    except OSError:
        if strict:
            raise
    finally:
        os.close(descriptor)


class HardLinkUnsupported(OSError):
    """The directory's filesystem refuses ``os.link``; the message names where it is."""


class PublishedUnsettled(OSError):
    """The final name exists, but a strict sync could not prove its directory entry.

    It holds this call's bytes or, when it already existed, another writer's
    uncompared bytes; either way a caller must not report it absent.
    """


def atomic_replace(path: Path, data: bytes, *, strict: bool = True) -> None:
    """Replace ``path`` with ``data`` whole, or leave what was there."""
    _publish(path, data, create=False, strict=strict)


def atomic_create(path: Path, data: bytes, *, strict: bool = True) -> None:
    """Create ``path`` holding ``data``, or raise ``FileExistsError`` and keep the winner.

    A hard link is the atomic create; ``O_EXCL`` would name the file before its bytes
    are in it. ``PublishedUnsettled`` replaces ``FileExistsError`` when the winner's
    directory entry cannot be proved. ``strict=False`` makes that sync best effort.
    """
    _publish(path, data, create=True, strict=strict)


def _publish(path: Path, data: bytes, *, create: bool, strict: bool) -> None:
    try:
        descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    except FileExistsError as error:
        # One argument, or OSError maps EEXIST back to FileExistsError: only the link's may.
        raise OSError(f"no temporary for {path}: {error}") from error
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not create:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                # The retry path: this call proved nothing yet about the winner's entry.
                _sync(path, strict, "already exists")
                raise
            except OSError as error:
                if error.errno in _NO_HARD_LINKS:
                    raise HardLinkUnsupported(
                        error.errno,
                        f"the directory at {path.parent} is on a filesystem that refuses hard "
                        f"links ({error.strerror}); this record is published by atomic link so "
                        "that a partly written file can never take its final name, and the "
                        "directory holding it has to be on a filesystem that supports it",
                    ) from error
                raise
    finally:
        # Best effort: readers ignore `.tmp-` names, so a leftover is harmless.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
    _sync(path, strict, "is published")


def _sync(path: Path, strict: bool, state: str) -> None:
    try:
        sync_directory(path.parent, strict=strict)
    except OSError as error:
        raise PublishedUnsettled(
            error.errno,
            f"{path} {state} but its directory entry is not proven durable: "
            f"{error.strerror or error}",
        ) from error
