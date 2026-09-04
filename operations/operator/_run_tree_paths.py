from __future__ import annotations

from pathlib import PurePosixPath


def is_publication_temporary(relative: str, scope: tuple[str, ...]) -> bool:
    """RunTree's same-directory `.<target>.tmp-*` residue, and nothing else."""

    path = PurePosixPath(relative)
    if not path.name.startswith("."):
        return False
    target_name, separator, unique = path.name[1:].partition(".tmp-")
    if not separator or not target_name or not unique:
        return False
    target = path.with_name(target_name).as_posix()
    return any(target.startswith(item) if item.endswith("/") else target == item for item in scope)
