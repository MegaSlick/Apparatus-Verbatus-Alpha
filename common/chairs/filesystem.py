"""Bounded control-file reads and filesystem name keys shared across the chair package."""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path

from .errors import DigestMismatchRefusal


def apfs_key(name: str) -> str:
    """The key under which default APFS treats two spellings as one name.

    Default APFS is case-insensitive and normalization-insensitive, so the key
    folds both: two spellings with one key would share one file or directory.
    """

    return unicodedata.normalize("NFD", name).casefold()


def apfs_alias(seen: dict[str, str], spelling: str) -> str | None:
    """Record `spelling` and return an earlier, different spelling it aliases, if any."""

    previous = seen.setdefault(apfs_key(spelling), spelling)
    return previous if previous != spelling else None


def read_limited_bytes(path: Path, limit: int, chair: str, label: str) -> bytes:
    """Read one small control artifact without allowing boundary amplification.

    A regular-file check plus `O_NOFOLLOW` refuses a symlink-redirected read,
    `O_NONBLOCK` keeps a FIFO from hanging the open, and reading `limit + 1` bytes
    detects an oversized file without loading all of it.
    """

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise DigestMismatchRefusal(chair, f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            payload = handle.read(limit + 1)
    except OSError as error:
        raise DigestMismatchRefusal(chair, f"cannot read {label}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > limit:
        raise DigestMismatchRefusal(
            chair,
            f"{label} exceeds the {limit}-byte control-artifact limit",
        )
    return payload
