"""Explicit no-network adapter for the operator rehearsal.

It is an application fake, not a pretend provider integration.  Every value it
returns is labelled fixture-only by the caller, and it contains no credential,
HTTP client, or S3 client.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO

from operations.pod.durable import sync_directory
from operations.pod.fake_provider import FakeProvider
from operations.pod.models import PodCreateRequest, PodRecord
from operations.pod.transfer import RemoteObject, TransferTarget

from .records import BLOCK_BYTES


class OperatorFakeProvider(FakeProvider):
    """`operations.pod.fake_provider.FakeProvider`, plus rehearsal-only affordances.

    The pod package's own fake carries only what its own test suite needs.
    The operator surface additionally needs to reopen a local fake state for a
    later `close` rehearsal in a fresh process (`seed_existing`), and to reset
    a hardening drill's injected failure before its retry (`clear_failures`).
    Neither belongs in `operations/pod/fake_provider.py` itself: that module
    is the reviewed, merged pod runtime's own fixture, taken as-is, and these
    two methods exist only for this surface's own tests and rehearsals.
    """

    def seed_existing(self, record: PodRecord, request: PodCreateRequest) -> None:
        """Install an already-recorded fixture pod without simulating a provider action.

        Used only to reopen a local fake state for a later `close` rehearsal.
        In particular, it must not call `create`: rehydration before a close
        confirmation cannot look like a paid action.
        """

        if record.pod_id in self.pods:
            raise ValueError(f"fixture pod already exists: {record.pod_id!r}")
        if record.name != request.name or record.volume_id != request.volume_id:
            raise ValueError("fixture pod does not match its recorded request")
        self.pods[record.pod_id] = record
        self._requests_by_pod[record.pod_id] = request
        self._present[record.pod_id] = True
        if record.pod_id.startswith("fake-pod-"):
            suffix = record.pod_id.removeprefix("fake-pod-")
            if suffix.isdigit():
                self._next_id = max(self._next_id, int(suffix) + 1)

    def clear_failures(self, verb: str) -> None:
        """Discard a still-queued synthetic drill failure before its recovery retry."""

        self._failures[verb].clear()


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
        self._path(key)
        path = self.root.resolve() / key
        # `_path` resolves for containment, so inspect the unresolved object key
        # as well: a link at the key is not verified bytes under that name.
        if path.is_symlink():
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                return None
            raise
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            for block in iter(lambda: handle.read(BLOCK_BYTES), b""):
                digest.update(block)
                size += len(block)
        return RemoteObject(digest.hexdigest(), size)

    def put_file(self, key: str, source: BinaryIO) -> None:
        # The same rule inspect() applies, at the write: a link at the object
        # key must be refused, or a successful put would record a key that
        # inspect() then reports absent and nothing could verify or resume.
        target = self.root.resolve() / key
        if target.is_symlink():
            raise RuntimeError(f"fixture object key {key!r} is a symbolic link, not an object")
        if self.fail_once_for == key:
            self.fail_once_for = None
            raise RuntimeError("injected partial transfer")
        self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                shutil.copyfileobj(source, handle, BLOCK_BYTES)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Claiming the name and comparing has to be one step. Ask
                # `exists()` and replace afterwards and two racing writers each
                # see it absent and each replace the other: 90 times in 400,
                # with the refusal below never firing.
                os.link(temporary, target)
                sync_directory(target.parent, strict=True)
            except FileExistsError:
                try:
                    existing = os.open(
                        target,
                        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    )
                except OSError as error:
                    raise RuntimeError(
                        f"fixture object key {key!r} is not a readable object"
                    ) from error
                with os.fdopen(existing, "rb") as opened:
                    if not stat.S_ISREG(os.fstat(opened.fileno()).st_mode):
                        raise RuntimeError(
                            f"fixture object key {key!r} is not a readable object"
                        ) from None
                    if not _same_handle_bytes(temporary, opened):
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


def _same_handle_bytes(left: Path, right: BinaryIO) -> bool:
    """Compare a local temporary with an already-opened object-key handle."""

    with left.open("rb") as first:
        while True:
            block = first.read(BLOCK_BYTES)
            if block != right.read(BLOCK_BYTES):
                return False
            if not block:
                return True
