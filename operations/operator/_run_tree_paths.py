from __future__ import annotations

from pathlib import PurePosixPath

from common.durability import is_temporary_name


def is_publication_temporary(relative: str, scope: tuple[str, ...]) -> bool:
    """RunTree's same-directory `.<target>.tmp-*` residue, and nothing else."""

    path = PurePosixPath(relative)
    if not is_temporary_name(path.name):
        return False
    target = path.with_name(path.name[1:].partition(".tmp-")[0]).as_posix()
    return any(target.startswith(item) if item.endswith("/") else target == item for item in scope)
