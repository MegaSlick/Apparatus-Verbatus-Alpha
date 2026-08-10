"""Checksummed, resumable transfer from Spec 03's sealed submission manifest.

The transport is deliberately provider-neutral.  A provider object-store adapter
can implement ``TransferTarget`` without leaking its API into bootstrap logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from operations.submit.submit import load_manifest

from .durable import atomic_write, canonical_json

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
    submission_manifest_present: bool = True

    def to_record(self) -> dict[str, object]:
        return {
            "schema": TRANSFER_SCHEMA,
            # An absent manifest is not a completed transfer -- there was
            # nothing to send -- and the word a reader scans for must say so
            # rather than require noticing "submission_manifest": "absent"
            # one field over (GOVERNANCE 2: a partial result is visibly partial).
            "state": "complete" if self.submission_manifest_present else "nothing-to-transfer",
            "submission_manifest": "present" if self.submission_manifest_present else "absent",
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
            return TransferReport((), (), submission_manifest_present=False)
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
            sent = False
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
                    sent = True
                    remote = self.target.inspect(key)
            if remote is None or remote.sha256 != expected_sha or remote.size != expected_size:
                raise TransferFailure(f"target {key!r} did not verify after transfer")
            if key not in completed:
                completed.add(key)
                journal["completed"] = sorted(completed)
                self._write(journal)
            # What this run put on the wire, not what the journal remembers: a
            # row the journal holds but the target has lost is sent again.
            (uploaded if sent else skipped).append(key)
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
        atomic_write(self.journal_path, canonical_json(record))


def _prefix(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError("transfer prefix must be a safe relative key prefix")
    return value.rstrip("/")


def _under(root: Path, relative: object) -> Path:
    # Spec 03's manifest check is non-empty, not absolute, no dot-dot, and it
    # stops there: an embedded NUL reaches os.lstat as a bare ValueError rather
    # than a named refusal, and "./x" and "a//b" resolve to a real file whose
    # object key keeps the un-normalized spelling. This is the last look before
    # the open, so every component is checked.
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\x00" in relative
        or any(component in {"", ".", ".."} for component in relative.split("/"))
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
