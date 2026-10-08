from __future__ import annotations

from pathlib import PurePosixPath

from common.durability import is_temporary_name
from common.runtree.sync import SYNC_PREFIX


def is_publication_temporary(relative: str, scope: tuple[str, ...]) -> bool:
    """RunTree's same-directory `.<target>.tmp-*` residue, and nothing else."""

    path = PurePosixPath(relative)
    if not is_temporary_name(path.name):
        return False
    target = path.with_name(path.name[1:].partition(".tmp-")[0]).as_posix()
    return any(target.startswith(item) if item.endswith("/") else target == item for item in scope)


def is_sync_residue(relative: str) -> bool:
    """A run-tree sync's own file: its ledger, or a copy in flight a killed sync left."""

    return PurePosixPath(relative).name.startswith(SYNC_PREFIX)
