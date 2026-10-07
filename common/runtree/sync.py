"""Copy a run tree to another disk without changing or removing stored evidence."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path


class RunTreeSyncError(Exception):
    """A run tree could not be copied and checked."""


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _directory(path: Path) -> None:
    if path == path.parent:
        return
    _directory(path.parent)
    if path.is_symlink():
        raise RunTreeSyncError(f"run tree directory is a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise RunTreeSyncError(f"run tree directory is not a directory: {path}")
        return
    path.mkdir()
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RunTreeSync:
    """Verify each new local file on the target; leave all target files in place."""

    def __init__(self, source: Path, target: Path):
        self.source = source
        self.target = target
        self._verified: dict[Path, tuple[int, int]] = {}

    def sync(self) -> int:
        if not self.source.is_dir() or self.source.is_symlink():
            raise RunTreeSyncError(f"run tree is missing or linked: {self.source}")
        _directory(self.target)
        copied = 0
        for parent, directories, files in os.walk(self.source, followlinks=False):
            source_directory = Path(parent)
            relative_directory = source_directory.relative_to(self.source)
            target_directory = self.target / relative_directory
            _directory(target_directory)
            for name in directories:
                if (source_directory / name).is_symlink():
                    raise RunTreeSyncError(f"run tree contains a linked directory: {name}")
            for name in files:
                if name.startswith(".verbatus-sync-"):
                    continue
                source = source_directory / name
                relative = source.relative_to(self.source)
                mode = source.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise RunTreeSyncError(f"run tree contains a non-file: {source}")
                before = source.stat()
                identity = (before.st_size, before.st_mtime_ns)
                if self._verified.get(relative) == identity:
                    continue
                target = target_directory / name
                if target.is_symlink():
                    raise RunTreeSyncError(f"stored run file is a symlink: {target}")
                source_digest = _digest(source)
                if target.exists():
                    if not target.is_file() or _digest(target) != source_digest:
                        raise RunTreeSyncError(
                            f"stored run file differs from local evidence: {target}"
                        )
                else:
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=".verbatus-sync-", dir=target_directory
                    )
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                            for block in iter(lambda: input_file.read(1024 * 1024), b""):
                                output.write(block)
                            output.flush()
                            os.fsync(output.fileno())
                        if target.exists():
                            raise RunTreeSyncError(
                                f"stored run file appeared during copy: {target}"
                            )
                        os.link(temporary, target)
                        _fsync_directory(target_directory)
                        if _digest(target) != source_digest:
                            raise RunTreeSyncError(f"stored run file failed verification: {target}")
                        copied += 1
                    finally:
                        temporary.unlink(missing_ok=True)
                after = source.stat()
                if (after.st_size, after.st_mtime_ns) != identity or _digest(
                    source
                ) != source_digest:
                    raise RunTreeSyncError(f"local run file changed during copy: {source}")
                self._verified[relative] = identity
        return copied
