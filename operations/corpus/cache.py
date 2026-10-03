"""Write a new file atomically, never over an existing one."""

from pathlib import Path

from common.durability import HardLinkUnsupported, atomic_create

from . import CorpusRefusal


class Refusal(CorpusRefusal):
    reasons = frozenset({"no-hard-link-support"})


def write_new_file(path: Path, data: bytes) -> bool:
    """Write `data` to `path` only if `path` does not already exist, atomically.

    Returns `True` if this call created the file and `False` if it already existed,
    in which case `data` was not written. A filesystem that refuses hard links
    outright raises `Refusal` (`no-hard-link-support`); any other `OSError`
    propagates unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_create(path, data, strict=False)
    except FileExistsError:
        return False
    except HardLinkUnsupported as error:
        raise Refusal(f"no-hard-link-support: {error.strerror}") from error
    return True
