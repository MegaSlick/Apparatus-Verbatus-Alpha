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
    except OSError:  # pragma: no cover - unusual filesystems may refuse directory opens
        if strict:
            raise
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - the same filesystem caveat
        if strict:
            raise
    finally:
        os.close(descriptor)
