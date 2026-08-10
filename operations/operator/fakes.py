"""Explicit no-network adapter for the operator rehearsal.

It is an application fake, not a pretend provider integration.  Every value it
returns is labelled fixture-only by the caller, and it contains no credential,
HTTP client, or S3 client.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from operations.pod.transfer import RemoteObject, TransferTarget


class LocalFixtureObjectStore(TransferTarget):
    """A file-backed implementation of the transfer seam for offline rehearsals."""

    def __init__(self, root: str | Path, *, fail_once_for: str | None = None) -> None:
        self.root = Path(root)
        self.fail_once_for = fail_once_for
        self.puts: list[str] = []

    def inspect(self, key: str) -> RemoteObject | None:
        path = self._path(key)
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()
        return RemoteObject(hashlib.sha256(data).hexdigest(), len(data))

    def put_file(self, key: str, source: Path) -> None:
        if self.fail_once_for == key:
            self.fail_once_for = None
            raise RuntimeError("injected partial transfer")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                existing = target.read_bytes()
                if existing != payload:
                    raise RuntimeError("fixture object already exists with different bytes")
            else:
                os.replace(temporary, target)
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
