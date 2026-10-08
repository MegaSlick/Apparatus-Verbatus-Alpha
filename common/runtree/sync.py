"""Copy a run tree to another disk without changing or removing stored evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Every name with this prefix is the sync's own: a copy in flight on the
# target, or the ledger in the source. None is ever copied.
SYNC_PREFIX: Final = ".verbatus-sync-"
# One JSON object per line: first the target it records, then one line per
# file verified on that target. Lines are appended as files are verified, so a
# sync that stops part-way keeps what it proved.
LEDGER_NAME: Final = f"{SYNC_PREFIX}ledger.jsonl"
COPY_WORKERS: Final = 16


class RunTreeSyncError(Exception):
    """A run tree could not be copied and checked."""


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SyncPlan:
    """The run tree as it stood at a stage boundary: directories, and files with
    their size and mtime, relative to the run tree."""

    directories: tuple[Path, ...]
    files: tuple[tuple[Path, tuple[int, int]], ...]


class RunTreeSync:
    """Verify each new local file on the target; leave all target files in place.

    A file is copied and verified once: its source size, mtime and sha256 go to
    a ledger in the source tree, so a later sync, in this process or a new one,
    passes over a file whose source is unchanged and whose target is still
    there at that size. Anything else is hashed and checked again.
    """

    def __init__(self, source: Path, target: Path):
        self.source = source
        self.target = target
        self._verified: dict[Path, tuple[int, int]] | None = None
        self._directories: set[Path] = set()

    def sync(self) -> int:
        """Copy and check every new file now; the number of files copied."""
        return self.copy(self.plan())

    def plan(self) -> "SyncPlan":
        """Freeze what to sync: every run file and its size and mtime, read locally.

        Only the local tree is read, so this is quick; `copy` does the rest and may
        run on another thread while the next stage writes files this plan does not
        name.
        """
        if not self.source.is_dir() or self.source.is_symlink():
            raise RunTreeSyncError(f"run tree is missing or linked: {self.source}")
        directories: list[Path] = []
        files: list[tuple[Path, tuple[int, int]]] = []
        for parent, children, names in os.walk(self.source, followlinks=False):
            source_directory = Path(parent)
            directories.append(source_directory.relative_to(self.source))
            for name in children:
                if (source_directory / name).is_symlink():
                    raise RunTreeSyncError(f"run tree contains a linked directory: {name}")
            for name in names:
                if name.startswith(SYNC_PREFIX):
                    continue
                source = source_directory / name
                mode = source.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise RunTreeSyncError(f"run tree contains a non-file: {source}")
                before = source.stat()
                files.append(
                    (source.relative_to(self.source), (before.st_size, before.st_mtime_ns))
                )
        return SyncPlan(tuple(directories), tuple(files))

    def copy(self, plan: "SyncPlan") -> int:
        """Copy and check every planned file the ledger does not already hold.

        A planned file that changed since the plan is refused (`_copy_one`): run
        files are written once.
        """
        if self._verified is None:
            self._verified = self._read_ledger()
        self._directory(self.target)
        for relative_directory in plan.directories:
            self._directory(self.target / relative_directory)
        pending = [
            (relative, self.source / relative, self.target / relative, identity)
            for relative, identity in plan.files
            if self._verified.get(relative) != identity
        ]
        copied = 0
        linked: set[Path] = set()
        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as pool:
            results = pool.map(lambda job: self._copy_one(*job[1:]), pending)
            # In walk order, so the first failure is the first file that failed.
            for (relative, _source, target, identity), (digest, was_copied) in zip(
                pending, results, strict=True
            ):
                self._verified[relative] = identity
                self._record(relative, identity, digest)
                if was_copied:
                    copied += 1
                    linked.add(target.parent)
        for directory in sorted(linked):
            _fsync_directory(directory)
        return copied

    def _copy_one(self, source: Path, target: Path, identity: tuple[int, int]) -> tuple[str, bool]:
        """Copy one file unless the target already holds it; `(sha256, copied)`."""
        if target.is_symlink():
            raise RunTreeSyncError(f"stored run file is a symlink: {target}")
        source_digest = _digest(source)
        copied = False
        if target.exists():
            if not target.is_file() or _digest(target) != source_digest:
                raise RunTreeSyncError(f"stored run file differs from local evidence: {target}")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=SYNC_PREFIX, dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    for block in iter(lambda: input_file.read(1024 * 1024), b""):
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if target.exists():
                    raise RunTreeSyncError(f"stored run file appeared during copy: {target}")
                # The directory entry is made durable once per directory, after
                # all its files are linked, before `sync` returns.
                os.link(temporary, target)
                if _digest(target) != source_digest:
                    raise RunTreeSyncError(f"stored run file failed verification: {target}")
                copied = True
            finally:
                temporary.unlink(missing_ok=True)
        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != identity or _digest(source) != source_digest:
            raise RunTreeSyncError(f"local run file changed during copy: {source}")
        return source_digest, copied

    def _directory(self, path: Path) -> None:
        """`path` and its ancestors as directories, each checked once per sync object."""
        if path in self._directories or path == path.parent:
            return
        self._directory(path.parent)
        if path.is_symlink():
            raise RunTreeSyncError(f"run tree directory is a symlink: {path}")
        if path.exists():
            if not path.is_dir():
                raise RunTreeSyncError(f"run tree directory is not a directory: {path}")
        else:
            path.mkdir()
            _fsync_directory(path.parent)
        self._directories.add(path)

    # --- the ledger -------------------------------------------------------------

    def _read_ledger(self) -> dict[Path, tuple[int, int]]:
        """The files an earlier sync verified on this same target that are still there.

        A ledger for another target, or one that cannot be read, is started
        again; a line that cannot be read, or whose target file is gone or no
        longer that size, is left out, so that file is hashed and checked again.
        """
        path = self.source / LEDGER_NAME
        verified: dict[Path, tuple[int, int]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0]) if lines else None
        except (OSError, UnicodeDecodeError, ValueError):
            header = None
            lines = []
        if not isinstance(header, dict) or header.get("target") != str(self.target):
            path.unlink(missing_ok=True)
            with path.open("x", encoding="utf-8") as ledger:
                ledger.write(json.dumps({"target": str(self.target)}) + "\n")
            return verified
        for line in lines[1:]:
            try:
                row = json.loads(line)
                relative = Path(row["path"])
                identity = (int(row["size"]), int(row["mtime_ns"]))
            except (ValueError, KeyError, TypeError):
                continue
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = self.target / relative
            try:
                held = target.lstat()
            except OSError:
                continue
            if stat.S_ISREG(held.st_mode) and held.st_size == identity[0]:
                verified[relative] = identity
        return verified

    def _record(self, relative: Path, identity: tuple[int, int], digest: str) -> None:
        row = {
            "path": relative.as_posix(),
            "size": identity[0],
            "mtime_ns": identity[1],
            "sha256": digest,
        }
        with (self.source / LEDGER_NAME).open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(row, sort_keys=True) + "\n")
