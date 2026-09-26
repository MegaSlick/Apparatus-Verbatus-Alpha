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

import json
from pathlib import Path
from typing import Mapping

from common.durability import atomic_create, atomic_replace

__all__ = [
    "atomic_write",
    "canonical_json",
    "exclusive_write",
]


def canonical_json(value: Mapping[str, object]) -> bytes:
    """Sorted, unpadded, newline-terminated JSON, so two equal records match."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` or leave the previous content intact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, payload, strict=False)


def exclusive_write(path: Path, payload: bytes, *, strict: bool = False) -> None:
    """Create ``path`` with ``payload`` durably, or raise ``FileExistsError``.

    ``PublishedUnsettled`` (an ``OSError``) instead means the name exists but its
    directory entry is unproved; callers stay fail-closed on it.

    A record whose *existence* is the fact kept -- a spent authorization, a boot
    that happened -- needs the create itself to be the exclusion. ``strict=True``
    is for money evidence, which must refuse unless the directory entry is proved.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_create(path, payload, strict=strict)
