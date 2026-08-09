"""Checksummed, resumable transfer from Spec 03's sealed submission manifest.

The transport is deliberately provider-neutral.  A provider object-store adapter
can implement ``TransferTarget`` without leaking its API into bootstrap logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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

    def put_file(self, key: str, source: Path) -> None:
        """Upload a regular local file under the exact safe key."""


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
            if not source.is_file() or source.is_symlink():
                raise TransferFailure(f"source {relative!r} is absent or not a regular file")
            if source.stat().st_size != expected_size or _sha256(source) != expected_sha:
                raise TransferFailure(
                    f"source {relative!r} no longer matches the sealed submission manifest"
                )
            remote = self.target.inspect(key)
            if remote is not None and (
                remote.sha256 != expected_sha or remote.size != expected_size
            ):
                raise TransferFailure(
                    f"target {key!r} exists but differs from the sealed manifest; it was not overwritten"
                )
            if remote is None:
                try:
                    self.target.put_file(key, source)
                except Exception as error:
                    raise TransferFailure(f"transfer of {relative!r} failed: {error}") from error
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
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
