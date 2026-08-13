"""Independent laptop supervisor and pod-resident hard-lifetime timer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable

from .lease import LeaseStore, PodLease
from .models import PodEstimate, PodRecord, utc_now
from .shutdown import CloseReport, VerifiedShutdown


class ControllerState(StrEnum):
    """Each controller's named result; a failed close never becomes healthy."""

    NO_LEASE = "no-lease"
    ACTIVE = "active"
    CLOSED_VERIFIED = "closed-verified"
    CLOSE_UNVERIFIED = "close-unverified"
    BUSY = "owner-heartbeat-fresh"
    PENDING_CREATE_REVIEW = "pending-create-review"
    PENDING_CREATE_RECOVERED = "pending-create-recovered"
    CONTROLLER_UNARMED = "controller-unarmed"
    LIFETIME_EXPIRED = "lifetime-expired"
    HEARTBEAT_LOST = "heartbeat-lost"
    ORPHAN_RECONCILED = "orphan-reconciled"
    TIMER_WAITING = "timer-waiting"
    TIMER_EXPIRED = "timer-expired"
    LEASE_RECORD_FAILURE = "lease-record-failure"


MAX_PENDING_RECOVERY_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ControllerResult:
    """A controller action and any verified-shutdown report it produced."""

    state: ControllerState
    detail: str
    close_report: CloseReport | None = None
    lease: PodLease | None = None

    @property
    def green(self) -> bool:
        if self.state is ControllerState.CLOSED_VERIFIED:
            return True
        if self.state is ControllerState.ACTIVE:
            return self.lease is not None and self.lease.controller_record is not None
        if self.state is ControllerState.LEASE_RECORD_FAILURE:
            # A verified shutdown whose result never reached the durable lease
            # is not safe to report green: a restart has nothing on disk
            # telling it the pod is gone (GOVERNANCE 2, nothing lost silently).
            return False
        return self.close_report is not None and self.close_report.verified


def record_from_lease(lease: PodLease) -> PodRecord:
    """Build the exact provider observation needed for a close from durable facts."""

    if lease.pod_id is None or lease.started_at is None:
        raise ValueError("pending-create lease has no exact pod id to terminate")
    return PodRecord(
        pod_id=lease.pod_id,
        name=f"leased-{lease.lease_id}",
        estimate=PodEstimate(
            pod_hourly_usd=lease.pod_hourly_usd,
            volume_hourly_usd=lease.volume_hourly_usd,
            source="durable lease estimate",
            observed_at=lease.started_at,
        ),
        volume_id=lease.volume_id,
        created_at=lease.started_at,
    )


class LaptopSupervisor:
    """Laptop-side controller that kills expired, stale, or orphaned active leases."""

    def __init__(
        self,
        store: LeaseStore,
        shutdown: VerifiedShutdown,
        *,
        owner_token: str,
        heartbeat_timeout: timedelta,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if heartbeat_timeout.total_seconds() <= 0:
            raise ValueError("heartbeat timeout must be positive")
        self.store = store
        self.shutdown = shutdown
        self.owner_token = owner_token
        self.heartbeat_timeout = heartbeat_timeout
        self.now = now

    def heartbeat(self) -> ControllerResult:
        """Refresh a live owner heartbeat without making a provider request."""

        try:
            lease = self.store.load()
        except Exception as error:
            # An unreadable or altered lease is a named non-green fact, not a
            # supervisor crash: a crash-looping supervisor makes no close
            # attempts at all, which is the worse outcome on this path.
            return ControllerResult(
                ControllerState.LEASE_RECORD_FAILURE,
                f"durable lease cannot be read: {error}",
            )
        if lease is None:
            return ControllerResult(ControllerState.NO_LEASE, "no lease exists")
        if not lease.active:
            return _terminal_result(lease)
        if lease.owner_token != self.owner_token:
            return ControllerResult(
                ControllerState.BUSY, "another controller heartbeat is fresh", lease=lease
            )
        try:
            refreshed = self.store.heartbeat(owner_token=self.owner_token, now=self.now())
        except Exception as error:
            return ControllerResult(ControllerState.LEASE_RECORD_FAILURE, str(error), lease=lease)
        return ControllerResult(ControllerState.ACTIVE, "heartbeat recorded", lease=refreshed)

    def run_once(self) -> ControllerResult:
        """Evaluate lifetime and heartbeat safety independently of a pod timer."""

        observed = self.now()
        try:
            lease = self.store.load()
        except Exception as error:
            # Same posture as `heartbeat`: name the lease as the fault rather
            # than crash the only controller that could still close a pod.
            return ControllerResult(
                ControllerState.LEASE_RECORD_FAILURE,
                f"durable lease cannot be read: {error}",
            )
        if lease is None:
            return ControllerResult(ControllerState.NO_LEASE, "no lease exists")
        if not lease.active:
            return _terminal_result(lease)
        if lease.pod_id is None:
            return self._recover_pending_create(lease, observed)
        stale = observed - lease.heartbeat_at >= self.heartbeat_timeout
        if lease.owner_token != self.owner_token:
            if not stale:
                return ControllerResult(
                    ControllerState.BUSY, "another controller heartbeat is fresh", lease=lease
                )
            try:
                lease = self.store.claim_if_orphan(
                    owner_token=self.owner_token,
                    now=observed,
                    stale_after=self.heartbeat_timeout,
                )
            except Exception as error:
                return ControllerResult(
                    ControllerState.LEASE_RECORD_FAILURE, str(error), lease=lease
                )
            return self._close(
                lease, ControllerState.ORPHAN_RECONCILED, "orphan lease heartbeat lost"
            )
        if observed >= lease.hard_deadline:
            return self._close(lease, ControllerState.LIFETIME_EXPIRED, "hard lifetime expired")
        if lease.controller_record is None:
            # A launch binds the pod, arms both controllers, then writes the
            # receipt — and the supervisor reading this is usually the one that
            # launch just started, so closing on the absent receipt alone kills
            # the pod it was started to guard. Waiting cannot leave an unarmed
            # pod running: this branch never refreshes the heartbeat, so an
            # abandoned launch goes stale and is closed below.
            if not stale:
                return ControllerResult(
                    ControllerState.CONTROLLER_UNARMED,
                    "active lease has no arming evidence yet and its launch owner is still "
                    "heartbeating; not green, and not closed while that launch may complete",
                    lease=lease,
                )
            return self._close(
                lease,
                ControllerState.CONTROLLER_UNARMED,
                "active lease lacks durable laptop-supervisor and pod-timer arming evidence",
            )
        if stale:
            return self._close(lease, ControllerState.HEARTBEAT_LOST, "laptop heartbeat lost")
        return self.heartbeat()

    def _recover_pending_create(self, lease: PodLease, observed: datetime) -> ControllerResult:
        """Recover one pre-armed launch token and close it before normal work.

        The seven-verb ``create`` contract accepts a recovery-only request. It
        may return exactly the existing token-matched pod, but it must never
        issue another paid POST.  A restart therefore has a durable route from
        a client crash to an exact, verified close rather than a manual
        name-based guess.
        """

        if lease.pending_create is None:
            return ControllerResult(
                ControllerState.PENDING_CREATE_REVIEW,
                "pending create has no durable recovery intent; do not guess a pod id",
                lease=lease,
            )
        if lease.pending_recovery_attempts >= MAX_PENDING_RECOVERY_ATTEMPTS:
            return ControllerResult(
                ControllerState.PENDING_CREATE_REVIEW,
                (
                    "exact launch-token recovery reached its bounded attempt limit; "
                    "preserve this lease and review the provider now"
                ),
                lease=lease,
            )
        stale = observed - lease.heartbeat_at >= self.heartbeat_timeout
        if lease.owner_token != self.owner_token and not stale:
            return ControllerResult(
                ControllerState.BUSY, "another controller heartbeat is fresh", lease=lease
            )
        try:
            claimed = self.store.claim_if_orphan(
                owner_token=self.owner_token,
                now=observed,
                stale_after=self.heartbeat_timeout,
            )
        except Exception as error:
            return ControllerResult(ControllerState.LEASE_RECORD_FAILURE, str(error), lease=lease)
        try:
            attempted = self.store.note_pending_recovery_attempt(
                owner_token=self.owner_token, now=observed
            )
        except Exception as error:
            return ControllerResult(ControllerState.LEASE_RECORD_FAILURE, str(error), lease=claimed)
        # Not an assert. `assert` disappears under `python -O`, and this one sat inside
        # the try below — so a missing recovery intent was reported as the *provider*
        # having failed to prove a pod, which is a different fact about a money path.
        # Found by CodeRabbit on this branch.
        if attempted.pending_create is None:
            return ControllerResult(
                ControllerState.PENDING_CREATE_REVIEW,
                "durable recovery intent disappeared while claiming the lease; "
                "do not guess a pod id",
                lease=attempted,
            )
        try:
            record = self.shutdown.provider.create(attempted.pending_create.recovery_request())
        except Exception as error:
            return ControllerResult(
                ControllerState.PENDING_CREATE_REVIEW,
                f"exact launch-token recovery could not prove a pod: {error}",
                lease=attempted,
            )
        try:
            bound = self.store.bind_pod(owner_token=self.owner_token, record=record, now=observed)
        except Exception as error:
            return ControllerResult(
                ControllerState.LEASE_RECORD_FAILURE,
                f"exact recovered pod {record.pod_id!r} could not bind to its lease: {error}",
                lease=attempted,
            )
        return self._close(
            bound,
            ControllerState.PENDING_CREATE_RECOVERED,
            "recovered exact pending-create launch token after controller restart",
        )

    def _close(self, lease: PodLease, state: ControllerState, reason: str) -> ControllerResult:
        try:
            record = record_from_lease(lease)
        except Exception as error:
            # Building the record reads only the durable lease, so its failure
            # names the lease -- reporting it as "shutdown controller raised"
            # sent the operator to the wrong subsystem on a money path.  The
            # close's own reason rides along so it is not lost with the close.
            return ControllerResult(
                ControllerState.LEASE_RECORD_FAILURE,
                f"durable lease cannot produce a closeable pod record for {reason!r}: {error}",
                lease=lease,
            )
        try:
            report = self.shutdown.close(record, reason=reason)
        except Exception as error:  # defensive: closure exception is itself non-green evidence
            return ControllerResult(state, f"shutdown controller raised: {error}", lease=lease)
        try:
            persisted = self.store.record_close(
                owner_token=self.owner_token,
                close_record=report.to_record(),
                verified=report.verified,
                now=self.now(),
            )
        except Exception as error:
            return ControllerResult(
                ControllerState.LEASE_RECORD_FAILURE,
                f"shutdown result was {report.state.value} but lease record failed: {error}",
                close_report=report,
                lease=lease,
            )
        return ControllerResult(state, reason, close_report=report, lease=persisted)


class PodDeadmanTimer:
    """Pod-side timer with no dependency on laptop heartbeats or processes."""

    def __init__(
        self,
        lease: PodLease,
        shutdown: VerifiedShutdown,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.lease = lease
        self.shutdown = shutdown
        self.now = now

    def run_once(self) -> ControllerResult:
        """Terminate at the immutable lease deadline even after laptop-controller death."""

        if self.lease.pod_id is None:
            return ControllerResult(
                ControllerState.PENDING_CREATE_REVIEW,
                "pod timer has no exact pod id because create did not bind its lease",
                lease=self.lease,
            )
        if self.now() < self.lease.hard_deadline:
            return ControllerResult(
                ControllerState.TIMER_WAITING, "hard lifetime has not expired", lease=self.lease
            )
        return self.close_now("pod dead-man hard lifetime expired")

    def close_now(self, reason: str) -> ControllerResult:
        """Close immediately for a failed mandatory bootstrap as well as expiry."""

        if self.lease.pod_id is None:
            return ControllerResult(
                ControllerState.PENDING_CREATE_REVIEW,
                "pod timer has no exact pod id because create did not bind its lease",
                lease=self.lease,
            )
        try:
            record = record_from_lease(self.lease)
        except Exception as error:
            return ControllerResult(
                ControllerState.LEASE_RECORD_FAILURE,
                f"durable lease cannot produce a closeable pod record for {reason!r}: {error}",
                lease=self.lease,
            )
        try:
            report = self.shutdown.close(record, reason=reason)
        except Exception as error:
            return ControllerResult(
                ControllerState.TIMER_EXPIRED,
                f"shutdown controller raised: {error}",
                lease=self.lease,
            )
        return ControllerResult(
            ControllerState.TIMER_EXPIRED,
            "pod-side dead-man attempted verified shutdown",
            close_report=report,
            lease=self.lease,
        )


def _terminal_result(lease: PodLease) -> ControllerResult:
    """Never re-label an unverified close as an active or green controller state."""

    if lease.phase == "closed-verified":
        return ControllerResult(
            ControllerState.CLOSED_VERIFIED, "lease was already verified closed", lease=lease
        )
    return ControllerResult(
        ControllerState.CLOSE_UNVERIFIED,
        f"lease close remains non-green in phase {lease.phase}; human reconciliation is required",
        lease=lease,
    )
