"""One durable write for every record this package keeps on disk.

The lease, the bootstrap journal, the transfer journal and the pod-side report
are each read back by a restarting controller deciding what is still billing, so
each needs the same three properties: no reader sees a half-written file, the
bytes survive a power cut and not merely a process exit, and the file is not
world-readable.

The power-cut guarantee covers the file's own bytes, which are always fsynced.
The directory entry that points at them is fsynced too on every filesystem that
allows a directory to be opened and synced; where the platform refuses either
(``common.durability.sync_directory``'s two documented exceptions), that entry's
durability degrades to best-effort rather than failing the write outright.

This is not `common/contracts/canonical.py`'s serialization and does not claim to
be: these are local operational records, not pipeline artifacts.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping

# Re-exported, not re-implemented. The primitive moved down to `common` so that
# `common/runtree/store.py` could publish artifacts durably without `common`
# importing the operational layer; every caller that already said
# `from operations.pod.durable import sync_directory` keeps working, and there
# is one implementation rather than the two this package used to carry.
from common.durability import sync_directory

__all__ = [
    "HardLinkUnsupported",
    "atomic_write",
    "canonical_json",
    "exclusive_write",
    "sync_directory",
]

# What a filesystem that will not hard-link answers with. The same three codes
# `common/runtree/store.py`, `gold/core.py`, `operations/corpus/cache.py` and
# `operations/operator/backup.py` each name at their own link sites.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})


class HardLinkUnsupported(OSError):
    """The target filesystem refuses ``os.link`` outright.

    An ``OSError`` subclass, so every existing caller that catches ``OSError``
    around a durable write keeps catching this; what changes is that the
    message names the setup fact -- where the directory was put -- instead of
    arriving as a bare errno about ``link``.

    This matters most on a pod. ``PodPreflightReceiptPublisher`` publishes
    every serving receipt, launch audit and evidence manifest through
    ``exclusive_write(..., strict=True)`` onto the attached network volume, and
    distributed or object-backed mounts are exactly the filesystems that answer
    ``EPERM``/``EOPNOTSUPP``. Unnamed, that surfaced through
    ``ReceiptPublicationError`` as a *serving* failure -- so a preflight
    reported a chair problem for what is a filesystem capability problem, and
    the pod closed without the operator learning which.
    """


def canonical_json(value: Mapping[str, object]) -> bytes:
    """Sorted, unpadded, newline-terminated JSON, so two equal records match."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` or leave the previous content intact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def exclusive_write(path: Path, payload: bytes, *, strict: bool = False) -> None:
    """Create ``path`` with ``payload`` durably, or raise if it already exists.

    ``atomic_write`` replaces whatever was there.  A record whose *existence* is
    the fact being kept -- a spent authorization, a boot that happened -- needs
    the opposite: the create itself must be the exclusion, so two processes that
    both believe they hold the same grant cannot both proceed.  ``O_EXCL`` is
    that exclusion, and the caller decides whether ``FileExistsError`` means a
    replay to refuse or identical evidence to accept.

    Durability matches ``atomic_write``: the bytes are always fsynced, and the
    directory entry too wherever ``sync_directory`` can open and sync it.
    ``strict=True`` is for money evidence and other callers that must refuse a
    paid action unless the directory entry itself is proved durable.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes the already-complete inode without replacing an
        # existing target. Unlike opening the target with O_EXCL and then
        # filling it, no reader can observe an empty or partially written final
        # record, and a process death before this line leaves no false claim.
        try:
            os.link(temporary, path)
        except FileExistsError:
            # The name is published, but *this* call has proved nothing about
            # its directory entry. That is exactly the retry path: a strict
            # sync failure leaves the record linked and unsynced, the caller
            # refuses the paid action, and the operator runs it again --
            # `StageCostStore._append` then sees FileExistsError, compares the
            # bytes, and reports success. Without a sync here that success is a
            # claim nothing established, and the record it covers is the one
            # that tells a human a pod may still be billing.
            sync_directory(path.parent, strict=strict)
            raise
        except OSError as error:
            # After `FileExistsError`, never before it: that is an `OSError`
            # too, and catching the wide class first would take the
            # already-published path's exception away from the clause that
            # syncs its directory entry. EEXIST is not in `_NO_HARD_LINKS`, so
            # the two clauses never contend for the same errno.
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
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    sync_directory(path.parent, strict=strict)
