"""Durable, atomically-owned pod leases.

The lease is deliberately retained after a failed close.  Removing evidence on
the path that needs human reconciliation would turn an orphan into a silent loss.

Every record carries a ``self_hash`` computed by this repository's existing
``common/contracts/canonical.py``, and a record whose hash does not recompute is
refused rather than acted on.  A lease is what tells a restarting controller
which pod is still billing; a hand-edited or half-written one that still parsed
would be a money-path decision made on evidence nobody checked.

The seal catches an edit and a torn write, and claims nothing beyond that:
anyone who can write the file can recompute the hash over what they wrote.  It
is an accident detector, not an authenticator, and the lease directory's own
permissions are what stand between the two.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping

from common.contracts.canonical import self_hash, verify_self_hash

from .durable import atomic_write, canonical_json, sync_directory
from .models import (
    LeaseFormatError,
    LeaseOwnershipError,
    PendingCreateIntent,
    PodRecord,
    as_decimal,
    assert_nonsecret_receipt,
    require_utc,
    utc_now,
)

LEASE_SCHEMA = "pod-lease.v1"


def _stamp(value: datetime) -> str:
    return require_utc(value, "lease timestamp").isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise LeaseFormatError(f"{label} is not an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LeaseFormatError(f"{label} is not RFC3339 UTC") from error
    try:
        return require_utc(parsed, label)
    except ValueError as error:
        raise LeaseFormatError(str(error)) from error


@dataclass(frozen=True, slots=True)
class PodLease:
    """What exists, who owns reconciliation, and the immutable hard deadline."""

    lease_id: str
    launch_token: str
    provider_name: str
    pod_id: str | None
    volume_id: str
    pod_hourly_usd: Decimal
    volume_hourly_usd: Decimal
    created_at: datetime
    started_at: datetime | None
    hard_deadline: datetime
    owner_token: str
    heartbeat_at: datetime
    generation: int = 1
    phase: str = "pending-create"
    close_record: Mapping[str, object] | None = None
    pending_create: PendingCreateIntent | None = None
    controller_record: Mapping[str, object] | None = None
    pending_recovery_attempts: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("lease_id", self.lease_id),
            ("launch_token", self.launch_token),
            ("provider_name", self.provider_name),
            ("volume_id", self.volume_id),
            ("owner_token", self.owner_token),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"lease {label} must be non-blank")
        if not re.fullmatch(r"[0-9a-f]{32}", self.lease_id):
            raise ValueError("lease_id must be 32 lowercase random hexadecimal characters")
        if not re.fullmatch(r"[0-9a-f]{32}", self.launch_token):
            raise ValueError("launch_token must be 32 lowercase random hexadecimal characters")
        if self.pod_id is not None and (
            not isinstance(self.pod_id, str) or not self.pod_id.strip()
        ):
            raise ValueError("lease pod_id must be non-blank when present")
        object.__setattr__(
            self, "pod_hourly_usd", as_decimal(self.pod_hourly_usd, "lease pod rate")
        )
        object.__setattr__(
            self, "volume_hourly_usd", as_decimal(self.volume_hourly_usd, "lease volume rate")
        )
        for label, value in (
            ("created_at", self.created_at),
            ("hard_deadline", self.hard_deadline),
            ("heartbeat_at", self.heartbeat_at),
        ):
            require_utc(value, label)
        if self.started_at is not None:
            require_utc(self.started_at, "started_at")
        if self.hard_deadline <= self.created_at:
            raise ValueError("lease hard deadline must be after creation")
        if self.generation < 1:
            raise ValueError("lease generation must be positive")
        if (
            not isinstance(self.pending_recovery_attempts, int)
            or isinstance(self.pending_recovery_attempts, bool)
            or self.pending_recovery_attempts < 0
        ):
            raise ValueError("pending recovery attempts must be a non-negative integer")
        if self.phase == "pending-create" and self.pending_create is None:
            raise ValueError("pending-create lease must retain its non-secret recovery intent")
        if self.phase not in {
            "pending-create",
            "active",
            "close-unverified",
            "closed-verified",
        }:
            raise ValueError("lease phase is unsupported")
        if self.phase == "active" and self.pod_id is None:
            raise ValueError("non-pending active lease must name an exact pod id")
        if self.controller_record is not None:
            _validate_controller_record(
                self.controller_record,
                lease_id=self.lease_id,
                pod_id=self.pod_id,
                created_at=self.created_at,
                hard_deadline=self.hard_deadline,
            )
        if self.phase in {"close-unverified", "closed-verified"}:
            _validate_close_record(
                self.close_record,
                pod_id=self.pod_id,
                verified=self.phase == "closed-verified",
            )
        elif self.close_record is not None:
            raise ValueError("non-terminal lease cannot carry a close record")

    @property
    def active(self) -> bool:
        return self.phase in {"pending-create", "active"}

    def to_record(self) -> dict[str, object]:
        record = self._unsealed_record()
        try:
            record["self_hash"] = self_hash(record)
        except TypeError as error:  # canonical_bytes refuses floats outright
            raise LeaseFormatError(f"lease record cannot be sealed: {error}") from error
        return record

    def _unsealed_record(self) -> dict[str, object]:
        return {
            "schema": LEASE_SCHEMA,
            "lease_id": self.lease_id,
            "launch_token": self.launch_token,
            "provider_name": self.provider_name,
            "pod_id": self.pod_id,
            "volume_id": self.volume_id,
            "pod_hourly_usd": str(self.pod_hourly_usd),
            "volume_hourly_usd": str(self.volume_hourly_usd),
            "created_at": _stamp(self.created_at),
            "started_at": _stamp(self.started_at) if self.started_at else None,
            "hard_deadline": _stamp(self.hard_deadline),
            "owner_token": self.owner_token,
            "heartbeat_at": _stamp(self.heartbeat_at),
            "generation": self.generation,
            "phase": self.phase,
            "close_record": dict(self.close_record) if self.close_record is not None else None,
            "pending_create": self.pending_create.to_record()
            if self.pending_create is not None
            else None,
            "controller_record": (
                dict(self.controller_record) if self.controller_record is not None else None
            ),
            "pending_recovery_attempts": self.pending_recovery_attempts,
        }

    @classmethod
    def from_record(cls, value: object) -> "PodLease":
        if not isinstance(value, dict) or value.get("schema") != LEASE_SCHEMA:
            raise LeaseFormatError("lease schema is absent or unsupported")
        if "self_hash" not in value:
            raise LeaseFormatError("lease carries no self_hash; it cannot be trusted as evidence")
        try:
            sealed = verify_self_hash(value)
        except TypeError as error:
            raise LeaseFormatError(f"lease record cannot be verified: {error}") from error
        if not sealed:
            raise LeaseFormatError(
                "lease self_hash does not recompute; refusing to act on an altered lease"
            )
        required = {
            "lease_id",
            "launch_token",
            "provider_name",
            "pod_id",
            "volume_id",
            "pod_hourly_usd",
            "volume_hourly_usd",
            "created_at",
            "started_at",
            "hard_deadline",
            "owner_token",
            "heartbeat_at",
            "generation",
            "phase",
            "close_record",
            "pending_create",
            "controller_record",
            "pending_recovery_attempts",
        }
        unknown = set(value) - ({"schema", "self_hash"} | required)
        if unknown:
            raise LeaseFormatError(f"lease has unknown fields {sorted(unknown)}")
        missing = required - set(value)
        if missing:
            raise LeaseFormatError(f"lease is missing fields {sorted(missing)}")
        started = value["started_at"]
        close = value["close_record"]
        pending = value["pending_create"]
        controller = value["controller_record"]
        if started is not None and not isinstance(started, str):
            raise LeaseFormatError("lease started_at must be string or null")
        if close is not None and not isinstance(close, dict):
            raise LeaseFormatError("lease close_record must be object or null")
        if pending is not None and not isinstance(pending, dict):
            raise LeaseFormatError("lease pending_create must be object or null")
        if controller is not None and not isinstance(controller, dict):
            raise LeaseFormatError("lease controller_record must be object or null")
        if not isinstance(value["generation"], int) or isinstance(value["generation"], bool):
            raise LeaseFormatError("lease generation must be integer")
        if not isinstance(value["pending_recovery_attempts"], int) or isinstance(
            value["pending_recovery_attempts"], bool
        ):
            raise LeaseFormatError("lease pending_recovery_attempts must be integer")
        if value["pod_id"] is not None and not isinstance(value["pod_id"], str):
            raise LeaseFormatError("lease pod_id must be string or null")
        try:
            return cls(
                lease_id=str(value["lease_id"]),
                launch_token=str(value["launch_token"]),
                provider_name=str(value["provider_name"]),
                pod_id=value["pod_id"],
                volume_id=str(value["volume_id"]),
                pod_hourly_usd=Decimal(str(value["pod_hourly_usd"])),
                volume_hourly_usd=Decimal(str(value["volume_hourly_usd"])),
                created_at=_parse_stamp(value["created_at"], "created_at"),
                started_at=_parse_stamp(started, "started_at") if started else None,
                hard_deadline=_parse_stamp(value["hard_deadline"], "hard_deadline"),
                owner_token=str(value["owner_token"]),
                heartbeat_at=_parse_stamp(value["heartbeat_at"], "heartbeat_at"),
                generation=value["generation"],
                phase=str(value["phase"]),
                close_record=close,
                pending_create=PendingCreateIntent.from_record(pending)
                if pending is not None
                else None,
                controller_record=controller,
                pending_recovery_attempts=value["pending_recovery_attempts"],
            )
        except (ValueError, TypeError) as error:
            raise LeaseFormatError(f"lease fields are invalid: {error}") from error


class LeaseStore:
    """One lease file, updated under an advisory lock and atomic replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - production pod and tests are POSIX
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - production pod and tests are POSIX
                pass
            handle.close()

    def create(self, lease: PodLease) -> PodLease:
        """Create an intent before a paid create; never overwrite a sibling owner."""

        with self._lock():
            if self.path.exists():
                raise LeaseOwnershipError(f"lease already exists at {self.path}")
            payload = canonical_json(lease.to_record())
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                sync_directory(self.path.parent)
            except Exception:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        return lease

    def load(self) -> PodLease | None:
        with self._lock():
            return self._read_unlocked()

    def bind_pod(
        self, *, owner_token: str, record: PodRecord, now: datetime | None = None
    ) -> PodLease:
        """Bind a returned provider id only to the owner who armed the intent."""

        observed = now or utc_now()
        with self._lock():
            lease = self._require_owner_unlocked(owner_token)
            if lease.phase != "pending-create":
                raise LeaseOwnershipError(f"cannot bind pod while lease phase is {lease.phase!r}")
            bound = replace(
                lease,
                pod_id=record.pod_id,
                started_at=record.created_at,
                pod_hourly_usd=record.estimate.pod_hourly_usd,
                volume_hourly_usd=record.estimate.volume_hourly_usd,
                heartbeat_at=require_utc(observed, "bind time"),
                phase="active",
                pending_create=None,
            )
            self._write_unlocked(bound)
            return bound

    def record_controller_arming(
        self,
        *,
        owner_token: str,
        controller_record: Mapping[str, object],
        now: datetime | None = None,
    ) -> PodLease:
        """Persist both controller acknowledgements before a launch becomes green."""

        with self._lock():
            lease = self._require_owner_unlocked(owner_token)
            if lease.phase != "active" or lease.pod_id is None:
                raise LeaseOwnershipError("cannot arm controllers for a non-active exact-pod lease")
            armed = replace(
                lease,
                heartbeat_at=require_utc(now or utc_now(), "controller arming time"),
                controller_record=dict(controller_record),
            )
            self._write_unlocked(armed)
            return armed

    def note_pending_recovery_attempt(
        self, *, owner_token: str, now: datetime | None = None
    ) -> PodLease:
        """Durably cap restart recovery probes instead of retrying forever."""

        with self._lock():
            lease = self._require_owner_unlocked(owner_token)
            if lease.phase != "pending-create" or lease.pending_create is None:
                raise LeaseOwnershipError(
                    "only a pending-create lease can record recovery attempts"
                )
            attempted = replace(
                lease,
                heartbeat_at=require_utc(now or utc_now(), "pending recovery time"),
                pending_recovery_attempts=lease.pending_recovery_attempts + 1,
            )
            self._write_unlocked(attempted)
            return attempted

    def heartbeat(self, *, owner_token: str, now: datetime | None = None) -> PodLease:
        with self._lock():
            lease = self._require_owner_unlocked(owner_token)
            if not lease.active:
                raise LeaseOwnershipError(
                    f"cannot heartbeat non-active lease phase {lease.phase!r}"
                )
            refreshed = replace(lease, heartbeat_at=require_utc(now or utc_now(), "heartbeat time"))
            self._write_unlocked(refreshed)
            return refreshed

    def claim_if_orphan(
        self,
        *,
        owner_token: str,
        now: datetime,
        stale_after: timedelta,
    ) -> PodLease:
        """Atomically claim a stale lease on supervisor restart, or refuse a live owner."""

        if stale_after.total_seconds() <= 0:
            raise ValueError("stale_after must be positive")
        observed = require_utc(now, "orphan claim time")
        with self._lock():
            lease = self._read_unlocked()
            if lease is None:
                raise LeaseOwnershipError("no lease exists to reconcile")
            if not lease.active:
                return lease
            if lease.owner_token != owner_token and observed - lease.heartbeat_at < stale_after:
                raise LeaseOwnershipError("lease owner heartbeat is still fresh")
            claimed = replace(
                lease,
                owner_token=owner_token,
                heartbeat_at=observed,
                generation=lease.generation + (lease.owner_token != owner_token),
            )
            self._write_unlocked(claimed)
            return claimed

    def record_close(
        self,
        *,
        owner_token: str,
        close_record: Mapping[str, object],
        verified: bool,
        now: datetime | None = None,
    ) -> PodLease:
        """Persist a close result; unverified evidence remains present for review.

        A lease that never finished binding its pod (create succeeded at the
        provider but the local bind then failed) still closes against the
        exact pod the close record names.  The pod id is bound here, as part
        of the same terminal write, so the close-record binding check below
        cannot be skipped for this lease, and the now-stale pending-create
        intent does not linger on a lease that is actually closed.
        """

        # Shape-only, before the lock: this lease's own pod id is not read yet.
        _validate_close_record(close_record, pod_id=None, verified=verified, bind_pod=False)
        with self._lock():
            lease = self._require_owner_unlocked(owner_token)
            pod_id = lease.pod_id or _close_record_pod_id(close_record)
            _validate_close_record(close_record, pod_id=pod_id, verified=verified)
            next_phase = "closed-verified" if verified else "close-unverified"
            closed = replace(
                lease,
                pod_id=pod_id,
                heartbeat_at=require_utc(now or utc_now(), "close time"),
                phase=next_phase,
                close_record=dict(close_record),
                pending_create=None,
            )
            self._write_unlocked(closed)
            return closed

    def _require_owner_unlocked(self, owner_token: str) -> PodLease:
        lease = self._read_unlocked()
        if lease is None:
            raise LeaseOwnershipError("lease does not exist")
        if lease.owner_token != owner_token:
            raise LeaseOwnershipError("lease belongs to a different controller")
        return lease

    def _read_unlocked(self) -> PodLease | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseFormatError(f"cannot read lease {self.path}: {error}") from error
        return PodLease.from_record(raw)

    def _write_unlocked(self, lease: PodLease) -> None:
        atomic_write(self.path, canonical_json(lease.to_record()))


def _validate_controller_record(
    value: Mapping[str, object],
    *,
    lease_id: str,
    pod_id: str | None,
    created_at: datetime,
    hard_deadline: datetime,
) -> None:
    """Accept only a complete two-controller receipt before treating a lease as armed."""

    required = {
        "laptop_supervisor_started",
        "pod_timer_acknowledged",
        "observed_at",
        "detail",
        "receipt",
    }
    if set(value) != required:
        raise ValueError("controller receipt has missing or unknown fields")
    if (
        value["laptop_supervisor_started"] is not True
        or value["pod_timer_acknowledged"] is not True
    ):
        raise ValueError("controller receipt must prove both laptop supervisor and pod timer")
    observed_at = _parse_stamp(value["observed_at"], "controller receipt observed_at")
    if not isinstance(value["detail"], str) or not value["detail"].strip():
        raise ValueError("controller receipt detail must be non-blank")
    if not isinstance(value["receipt"], dict):
        raise ValueError("controller receipt payload must be an object")
    if pod_id is None:
        raise ValueError("controller receipt cannot prove a pod before exact pod binding")
    receipt = value["receipt"]
    assert_nonsecret_receipt(receipt)
    required_receipt = {
        "lease_id",
        "pod_id",
        "hard_deadline",
        "laptop_supervisor",
        "pod_timer",
    }
    if set(receipt) != required_receipt:
        raise ValueError(
            "controller receipt must contain only complete lease/pod/deadline bindings"
        )
    if receipt["lease_id"] != lease_id or receipt["pod_id"] != pod_id:
        raise ValueError("controller receipt is not bound to this exact lease and pod")
    if receipt["hard_deadline"] != _stamp(hard_deadline):
        raise ValueError("controller receipt is not bound to this immutable hard deadline")
    supervisor = receipt["laptop_supervisor"]
    timer = receipt["pod_timer"]
    if not isinstance(supervisor, dict) or set(supervisor) != {"identity", "started_at"}:
        raise ValueError("controller receipt lacks a bound laptop-supervisor observation")
    if not isinstance(timer, dict) or set(timer) != {"report_path", "acknowledged_at"}:
        raise ValueError("controller receipt lacks a bound pod-timer acknowledgement")
    if not isinstance(supervisor["identity"], str) or not supervisor["identity"].strip():
        raise ValueError("controller receipt laptop-supervisor identity must be non-blank")
    supervisor_started = _parse_stamp(
        supervisor["started_at"], "controller receipt laptop supervisor start"
    )
    if (
        not isinstance(timer["report_path"], str)
        or not PurePosixPath(timer["report_path"]).is_absolute()
    ):
        raise ValueError("controller receipt pod timer report path must be absolute")
    timer_acknowledged = _parse_stamp(
        timer["acknowledged_at"], "controller receipt pod timer acknowledgement"
    )
    for label, timestamp in (
        ("controller receipt observed_at", observed_at),
        ("controller receipt laptop supervisor start", supervisor_started),
        ("controller receipt pod timer acknowledgement", timer_acknowledged),
    ):
        if timestamp < created_at or timestamp > hard_deadline:
            raise ValueError(f"{label} lies outside this lease's lifetime")
    if supervisor_started > observed_at or timer_acknowledged > observed_at:
        raise ValueError("controller component observation cannot occur after its receipt")


def _close_record_pod_id(close_record: Mapping[str, object]) -> str | None:
    value = close_record.get("pod_id")
    return value if isinstance(value, str) and value else None


def _validate_close_record(
    value: Mapping[str, object] | None,
    *,
    pod_id: str | None,
    verified: bool,
    bind_pod: bool = True,
) -> None:
    """Bind a terminal lease phase to the close evidence it summarizes.

    ``bind_pod=False`` is the shape-only pre-check ``record_close`` runs before
    it takes the lock, when the lease's own pod id is not yet known.  Every
    other caller binds.

    A verified close asserts ``pod_get_absent`` and ``pod_list_absent`` about
    one exact pod, so a ``closed-verified`` lease that names no pod is evidence
    about nothing.  Before this check, the binding check below skipped itself in
    exactly that case and the phase was accepted.  Found by CodeRabbit on this
    branch.  The unverified path legitimately closes without a pod id -- a
    create that never bound one still has to close -- so only the verified
    phase requires it.
    """

    if not isinstance(value, Mapping):
        raise ValueError("terminal lease must carry a close record")
    if pod_id is None:
        if bind_pod and verified:
            raise ValueError("verified close evidence must name the exact pod it observed absent")
    elif value.get("pod_id") != pod_id:
        raise ValueError("close record is not bound to this exact pod")
    if verified:
        capture = value.get("cost_capture")
        if (
            value.get("state") != "verified"
            or value.get("pod_get_absent") is not True
            or value.get("pod_list_absent") is not True
            or not isinstance(capture, Mapping)
            or capture.get("state") != "captured"
            or not isinstance(capture.get("lines"), list)
            or not capture["lines"]
            or capture.get("cutoff_at") != value.get("cutoff_at")
            or value.get("manual_action") is not None
        ):
            raise ValueError("verified lease phase requires complete verified close evidence")
    elif value.get("state") == "verified":
        raise ValueError("unverified lease phase cannot carry verified close evidence")
