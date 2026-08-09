"""Small immutable records owned by the operator surface.

These records join backend receipts that currently have no common index.  The
surface writes only during a mutating verb.  ``status`` uses the read methods
only and never creates a directory, a marker, or an observation of its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

UTC = timezone.utc
SCHEMA = "operator-receipt.v1"
DESCRIPTOR_SCHEMA = "operator-surface.v2"
LEGACY_DESCRIPTOR_SCHEMA = "operator-surface.v1"


class RecordError(RuntimeError):
    """A record cannot be safely read or written."""


def canonical_bytes(value: object) -> bytes:
    """The stable bytes used for an immutable operator receipt."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise RecordError("operator receipt time must be UTC")
    return value.isoformat().replace("+00:00", "Z")


class ReceiptStore:
    """Content-addressed receipts, with no mutable current-state pointer.

    Callers retain the returned path in the next receipt when they need a chain.
    This intentionally avoids a "latest" pointer that status could accidentally
    rewrite or silently treat as authority.
    """

    def __init__(self, root: str | Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.root = Path(root)
        self.now = now or (lambda: datetime.now(UTC))

    @property
    def receipts(self) -> Path:
        return self.root / "receipts"

    def write(self, kind: str, payload: dict[str, Any]) -> Path:
        """Write a single immutable fact and return its content-addressed path."""

        if (
            not isinstance(kind, str)
            or not kind
            or any(char not in "abcdefghijklmnopqrstuvwxyz-" for char in kind)
        ):
            raise RecordError("operator receipt kind must use lowercase letters and hyphens")
        if not isinstance(payload, dict):
            raise RecordError("operator receipt payload must be an object")
        record = {
            "schema": SCHEMA,
            "kind": kind,
            "recorded_at": utc_stamp(self.now()),
            "payload": payload,
        }
        encoded = canonical_bytes(record)
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.receipts / f"{kind}-{digest}.json"
        self.receipts.mkdir(parents=True, exist_ok=True)
        _atomic_create_or_reuse(target, encoded)
        return target

    def read(self, path: str | Path) -> dict[str, Any]:
        """Read one receipt after validating its closed shape and its filename digest."""

        candidate = Path(path)
        try:
            resolved_root = self.receipts.resolve()
            resolved = candidate.resolve()
        except OSError as error:
            raise RecordError("operator receipt path cannot be resolved") from error
        if not resolved.is_relative_to(resolved_root):
            raise RecordError("operator receipt path is outside the receipt directory")
        try:
            data = resolved.read_bytes()
            record = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecordError(f"operator receipt cannot be read: {resolved.name}") from error
        if canonical_bytes(record) != data:
            raise RecordError(f"operator receipt is not canonical: {resolved.name}")
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "kind",
            "recorded_at",
            "payload",
        }:
            raise RecordError(f"operator receipt has an invalid shape: {resolved.name}")
        if (
            record["schema"] != SCHEMA
            or not isinstance(record["kind"], str)
            or not isinstance(record["payload"], dict)
        ):
            raise RecordError(f"operator receipt has invalid fields: {resolved.name}")
        expected_digest = resolved.stem.rsplit("-", 1)[-1]
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise RecordError(
                f"operator receipt digest does not match its filename: {resolved.name}"
            )
        return record

    def list(self) -> list[tuple[Path, dict[str, Any]]]:
        """Read existing receipts only; absent storage is an empty, not a created, state."""

        if not self.receipts.exists():
            return []
        if not self.receipts.is_dir() or self.receipts.is_symlink():
            raise RecordError("operator receipt directory is not a safe directory")
        loaded: list[tuple[Path, dict[str, Any]]] = []
        for candidate in sorted(self.receipts.glob("*.json")):
            loaded.append((candidate, self.read(candidate)))
        return loaded

    def records_of_kind(self, kind: str) -> list[tuple[Path, dict[str, Any]]]:
        return [(path, record) for path, record in self.list() if record["kind"] == kind]


class DescriptorStore:
    """A small, self-hashed index of explicitly chosen receipt paths.

    It is a navigation aid, not evidence: every fact it names lives in an
    immutable receipt.  Keeping one named descriptor lets ``status`` read what
    this operator session declared without scanning a directory and choosing a
    record by timestamp or filename.
    """

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "operator-surface.json"

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecordError("operator descriptor cannot be read") from error
        if not isinstance(raw, dict):
            raise RecordError("operator descriptor has an invalid shape")
        if raw.get("schema") == LEGACY_DESCRIPTOR_SCHEMA:
            if set(raw) != {"schema", "actions", "self_hash"}:
                raise RecordError("operator descriptor has an invalid shape")
            actions = raw["actions"]
            if not _valid_actions(actions):
                raise RecordError("operator descriptor action list is invalid")
            expected = dict(raw)
            expected.pop("self_hash")
            if raw["self_hash"] != hashlib.sha256(canonical_bytes(expected)).hexdigest():
                raise RecordError("operator descriptor fails its own integrity check")
            return {
                "schema": DESCRIPTOR_SCHEMA,
                "actions": actions,
                "history": {action: [receipt] for action, receipt in actions.items()},
            }
        if set(raw) != {"schema", "actions", "history", "self_hash"}:
            raise RecordError("operator descriptor has an invalid shape")
        expected = dict(raw)
        expected.pop("self_hash")
        digest = hashlib.sha256(canonical_bytes(expected)).hexdigest()
        if raw["schema"] != DESCRIPTOR_SCHEMA or raw["self_hash"] != digest:
            raise RecordError("operator descriptor fails its own integrity check")
        if not _valid_actions(raw["actions"]) or not _valid_history(raw["history"], raw["actions"]):
            raise RecordError("operator descriptor action list is invalid")
        return raw

    def record(self, action: str, receipt: Path) -> dict[str, Any]:
        if not isinstance(action, str) or not action:
            raise RecordError("operator descriptor action must be non-blank")
        current = self.load()
        actions = {} if current is None else dict(current["actions"])
        history = (
            {}
            if current is None
            else {name: list(paths) for name, paths in current["history"].items()}
        )
        receipt_text = str(receipt.resolve())
        actions[action] = receipt_text
        entries = history.setdefault(action, [])
        if receipt_text not in entries:
            entries.append(receipt_text)
        record: dict[str, Any] = {
            "schema": DESCRIPTOR_SCHEMA,
            "actions": actions,
            "history": history,
        }
        record["self_hash"] = hashlib.sha256(canonical_bytes(record)).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(self.path, canonical_bytes(record))
        return record


def _valid_actions(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(receipt, str) for key, receipt in value.items()
    )


def _valid_history(value: object, actions: object) -> bool:
    if not isinstance(value, dict) or not isinstance(actions, dict) or set(value) != set(actions):
        return False
    return all(
        isinstance(action, str)
        and isinstance(receipts, list)
        and bool(receipts)
        and all(isinstance(receipt, str) for receipt in receipts)
        and receipts[-1] == actions[action]
        for action, receipts in value.items()
    )


def _atomic_create_or_reuse(target: Path, payload: bytes) -> None:
    """Create a receipt once; identical bytes are a true no-op, never an overwrite."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            try:
                existing = target.read_bytes()
            except OSError as error:
                raise RecordError("existing operator receipt cannot be read") from error
            if existing != payload:
                raise RecordError(
                    "an operator receipt path already holds different evidence"
                ) from None
    except OSError as error:
        raise RecordError("operator receipt could not be written") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as error:
            raise RecordError("operator receipt temporary file could not be removed") from error


def _atomic_replace(target: Path, payload: bytes) -> None:
    """Replace the non-evidentiary descriptor atomically after all facts are stored."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        raise RecordError("operator descriptor could not be written") from error
    finally:
        temporary.unlink(missing_ok=True)
