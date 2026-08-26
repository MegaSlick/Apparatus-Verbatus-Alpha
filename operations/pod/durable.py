"""One durable write for every record this package keeps on disk.

The lease, the bootstrap journal, the transfer journal and the pod-side report
are each read back by a restarting controller deciding what is still billing, so
each needs the same three properties: no reader sees a half-written file, the
bytes survive a power cut and not merely a process exit, and the file is not
world-readable.

The power-cut guarantee covers the file's own bytes, which are always fsynced.
The directory entry that points at them is fsynced too on every filesystem that
allows a directory to be opened and synced; where the platform refuses either
(``sync_directory``'s two documented exceptions), that entry's durability
degrades to best-effort rather than failing the write outright.

This is not `common/contracts/canonical.py`'s serialization and does not claim to
be: these are local operational records, not pipeline artifacts.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


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


def sync_directory(path: Path, *, strict: bool = False) -> None:
    """Persist a directory entry, optionally refusing when durability cannot be proved.

    Pod-side callers retain the established best-effort behavior on filesystems
    that refuse directory opens or syncs. Operator publication passes
    ``strict=True`` because it must not print that a receipt or submitted object
    was saved after the directory entry itself failed to become durable.
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


def exclusive_write(
    path: Path, payload: bytes, *, strict: bool = False, create_parent: bool = True
) -> None:
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

    ``create_parent=False`` serves operator flows whose contract requires an
    existing approved folder. If that folder disappears before the open, the
    write refuses instead of silently recreating a directory the operator did
    not approve.
    """

    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    sync_directory(path.parent, strict=strict)
