"""Making a published filename survive a power cut, in one place.

``fsync`` on a file persists that file's *bytes*.  It says nothing about the
directory entry pointing at them: ``fsync(2)`` states plainly that the
containing directory has to be synced separately, and until that happens a
crash can leave a newly published name — or a replacement — missing, with the
data intact and unreachable.  Atomic visibility to another process and
durability across a power loss are different guarantees, and only the first
comes free from ``os.replace`` or ``os.link``.

This module exists because the repository had that second guarantee in one
place and not another.  ``operations/pod/durable.py`` synced the directory for
every operational record, with a strict mode for money evidence;
``common/runtree/store.py`` published run-tree artifacts — the irreplaceable
ones — with no directory sync at all.  Both now call this.

It lives under ``common`` because that is the layer both may import: ``common``
must never import the operational layer, so the primitive moves down rather
than being reached upward for, and it is not duplicated a third time.  Nothing
here knows about run trees, pods, receipts, or money; it is a filesystem call
and its two documented failure modes.
"""

from __future__ import annotations

import os
from pathlib import Path


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
