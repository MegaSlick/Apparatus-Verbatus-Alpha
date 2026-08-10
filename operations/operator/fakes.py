"""Explicit no-network adapter for the operator rehearsal.

It is an application fake, not a pretend provider integration.  Every value it
returns is labelled fixture-only by the caller, and it contains no credential,
HTTP client, or S3 client.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from operations.pod.transfer import RemoteObject, TransferTarget

from .records import BLOCK_BYTES


class LocalFixtureObjectStore(TransferTarget):
    """A file-backed implementation of the transfer seam for offline rehearsals.

    This is the default target of `verbatus upload`, not test scaffolding, so
    it moves real submitted material. Nothing here holds a whole file in
    memory: a submission is sized by what a person photographed, and reading
    one whole was the difference between 21 MiB resident and 533 MiB for a
    single 512 MiB page set.
    """

    def __init__(self, root: str | Path, *, fail_once_for: str | None = None) -> None:
        self.root = Path(root)
        self.fail_once_for = fail_once_for
        self.puts: list[str] = []

    def inspect(self, key: str) -> RemoteObject | None:
        path = self._path(key)
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(BLOCK_BYTES), b""):
                digest.update(block)
                size += len(block)
        return RemoteObject(digest.hexdigest(), size)

    def put_file(self, key: str, source: Path) -> None:
        if self.fail_once_for == key:
            self.fail_once_for = None
            raise RuntimeError("injected partial transfer")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                with Path(source).open("rb") as reader:
                    shutil.copyfileobj(reader, handle, BLOCK_BYTES)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Claiming the name and comparing has to be one step. Ask
                # `exists()` and replace afterwards and two racing writers each
                # see it absent and each replace the other: 90 times in 400,
                # with the refusal below never firing.
                os.link(temporary, target)
            except FileExistsError:
                if not _same_bytes(temporary, target):
                    raise RuntimeError(
                        "fixture object already exists with different bytes"
                    ) from None
            self.puts.append(key)
        finally:
            temporary.unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("fixture object key is unsafe")
        resolved_root = self.root.resolve()
        candidate = (resolved_root / key).resolve()
        if not candidate.is_relative_to(resolved_root):
            raise ValueError("fixture object key escapes its store")
        return candidate


def _same_bytes(left: Path, right: Path) -> bool:
    with left.open("rb") as first, right.open("rb") as second:
        while True:
            block = first.read(BLOCK_BYTES)
            if block != second.read(BLOCK_BYTES):
                return False
            if not block:
                return True
