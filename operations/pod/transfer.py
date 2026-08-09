"""Checksummed, resumable transfer from Spec 03's sealed submission manifest.

The transport is deliberately provider-neutral.  A provider object-store adapter
can implement ``TransferTarget`` without leaking its API into bootstrap logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from operations.submit.submit import load_manifest

TRANSFER_SCHEMA = "pod-transfer.v1"


class TransferFailure(RuntimeError):
    """A partial transfer remains named and non-complete until every digest matches."""


@dataclass(frozen=True, slots=True)
class RemoteObject:
    """The target's independently observed content facts for one object key."""

    sha256: str
    size: int


class TransferTarget(Protocol):
    """Minimal storage seam; provider S3/API knowledge belongs in its adapter."""

    def inspect(self, key: str) -> RemoteObject | None:
        """Return the target's digest/size evidence, or None if the object is absent."""

    def put_file(self, key: str, source: BinaryIO) -> None:
        """Upload the exact bytes behind this already-opened, verified-regular handle.

        A handle, not a path: the caller opens and verifies the source once
        (``O_NOFOLLOW``, regular-file check) and this seam reads only from
        that open file descriptor, so no implementation re-resolves the name
        and reopens whatever happens to be there by the time it runs.
        """


@dataclass(frozen=True, slots=True)
class TransferReport:
    """Receipt for verified files only; it cannot call a partially sent set complete."""

    completed_keys: tuple[str, ...]
    skipped_keys: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "schema": TRANSFER_SCHEMA,
            "state": "complete",
            "completed_keys": list(self.completed_keys),
            "skipped_keys": list(self.skipped_keys),
        }


class ChecksummedTransfer:
    """Transfer every Spec-03 manifest row once, reusing only verified target bytes."""

    def __init__(
        self,
        *,
        source_root: str | Path,
        submission_manifest: str | Path,
        target: TransferTarget,
        prefix: str,
        journal_path: str | Path,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.submission_manifest = Path(submission_manifest)
        self.target = target
        self.prefix = _prefix(prefix)
        self.journal_path = Path(journal_path)

    def resume(self) -> TransferReport:
        """Verify source, target, and journal row by row; retry only unfinished rows.

        A submission manifest is Spec 03's output, not this pod's: a freshly
        launched pod that has processed nothing yet has none to transfer.  That
        is a vacuous success, not a failure — only a manifest that exists and
        fails to parse or verify is a named `TransferFailure`.
        """

        if not self.submission_manifest.is_file():
            return TransferReport((), ())
        manifest = load_manifest(self.submission_manifest)
        journal = self._load_or_create(manifest)
        completed = set(journal["completed"])
        uploaded: list[str] = []
        skipped: list[str] = []
        for row in manifest["files"]:
            relative = row["relative_path"]
            expected_sha = row["sha256"]
            expected_size = row["bytes"]
            source = _under(self.source_root, relative)
            key = f"{self.prefix}/{relative}"
            # Open once, by name, with the leaf pinned to a regular file; every
            # later read (hash, upload) happens against this one descriptor so
            # nothing between the check and the use can swap what gets read.
            with _open_verified_regular_file(source, relative=relative) as handle:
                size = os.fstat(handle.fileno()).st_size
                if size != expected_size or _sha256(handle) != expected_sha:
                    raise TransferFailure(
                        f"source {relative!r} no longer matches the sealed submission manifest"
                    )
                remote = self.target.inspect(key)
                if remote is not None and (
                    remote.sha256 != expected_sha or remote.size != expected_size
                ):
                    raise TransferFailure(
                        f"target {key!r} exists but differs from the sealed manifest; "
                        "it was not overwritten"
                    )
                if remote is None:
                    handle.seek(0)
                    try:
                        self.target.put_file(key, handle)
                    except Exception as error:
                        raise TransferFailure(
                            f"transfer of {relative!r} failed: {error}"
                        ) from error
                    remote = self.target.inspect(key)
            if remote is None or remote.sha256 != expected_sha or remote.size != expected_size:
                raise TransferFailure(f"target {key!r} did not verify after transfer")
            if key not in completed:
                completed.add(key)
                journal["completed"] = sorted(completed)
                self._write(journal)
                uploaded.append(key)
            else:
                skipped.append(key)
        return TransferReport(tuple(uploaded), tuple(skipped))

    def _load_or_create(self, manifest: dict[str, object]) -> dict[str, object]:
        manifest_hash = str(manifest["self_hash"])
        if not self.journal_path.exists():
            record = {
                "schema": TRANSFER_SCHEMA,
                "manifest_self_hash": manifest_hash,
                "completed": [],
            }
            self._write(record)
            return record
        try:
            record = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TransferFailure(f"transfer journal cannot be read: {error}") from error
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "manifest_self_hash",
            "completed",
        }:
            raise TransferFailure("transfer journal has missing or unknown fields")
        if record["schema"] != TRANSFER_SCHEMA or record["manifest_self_hash"] != manifest_hash:
            raise TransferFailure("transfer journal names another submission manifest")
        if not isinstance(record["completed"], list) or not all(
            isinstance(key, str) for key in record["completed"]
        ):
            raise TransferFailure("transfer journal completion list is invalid")
        return record

    def _write(self, record: dict[str, object]) -> None:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.journal_path.name}.", dir=self.journal_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.journal_path)
            _sync_directory(self.journal_path.parent)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _sync_directory(path: Path) -> None:
    """Make the atomic journal replacement durable across a machine crash.

    bootstrap.py, lease.py, and pod_timer.py each carry the same durable-write
    pattern and each calls this after ``os.replace``; this copy previously
    stopped short of it, which is the one gap among the four.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - unusual filesystems may refuse directory opens
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - same filesystem caveat
        pass
    finally:
        os.close(descriptor)


def _prefix(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError("transfer prefix must be a safe relative key prefix")
    return value.rstrip("/")


def _under(root: Path, relative: object) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or ".." in relative.split("/")
    ):
        raise TransferFailure("submission manifest contains an unsafe relative path")
    candidate = root
    for component in relative.split("/"):
        candidate /= component
        if candidate.is_symlink():
            raise TransferFailure("submission path traverses a symbolic link")
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise TransferFailure("submission path escapes its source root")
    return candidate


def _open_verified_regular_file(path: Path, *, relative: str) -> BinaryIO:
    """Open by name exactly once, refusing a symlink leaf, and never reopen it.

    ``_under`` already refuses a symlinked path component during its walk, but
    that check and every later read of ``path`` (size, hash, upload) are
    otherwise separated by a gap in which a concurrent writer with access to
    ``source_root`` could swap the leaf for a symlink. ``O_NOFOLLOW`` closes
    that gap for the final component: every subsequent read in this row goes
    through the one descriptor opened here, never through the path again.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TransferFailure(
            f"source {relative!r} is absent or not a regular file: {error}"
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TransferFailure(f"source {relative!r} is not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "rb")


def _sha256(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
