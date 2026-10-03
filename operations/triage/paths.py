"""Path identity checks for the triage producer's input and output files."""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path
from typing import Sequence


class PathCheckFailure(Exception):
    def __init__(self, reason: str, role: str, prior: str = "") -> None:
        self.reason = reason
        self.role = role
        self.prior = prior
        super().__init__(reason)


def canonical_distinct_paths(labelled: Sequence[tuple[str, Path]]) -> list[Path]:
    """Resolve parent names and reject spelling, link, type, or inode aliases."""
    canonical = []
    spellings: dict[str, str] = {}
    identities: dict[tuple[int, int], str] = {}
    for role, path in labelled:
        try:
            target = path.parent.resolve(strict=False) / path.name
        except (OSError, RuntimeError) as error:
            raise PathCheckFailure("resolve", role) from error
        key = unicodedata.normalize("NFC", os.fspath(target)).casefold()
        if prior := spellings.get(key):
            raise PathCheckFailure("spelling", role, prior)
        spellings[key] = role
        try:
            status = os.lstat(target)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise PathCheckFailure("inspect", role) from error
        else:
            if stat.S_ISLNK(status.st_mode):
                raise PathCheckFailure("symlink", role)
            if not stat.S_ISREG(status.st_mode):
                raise PathCheckFailure("type", role)
            identity = (status.st_dev, status.st_ino)
            if prior := identities.get(identity):
                raise PathCheckFailure("inode", role, prior)
            identities[identity] = role
        canonical.append(target)
    return canonical
