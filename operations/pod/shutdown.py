"""Verified shutdown: terminate, prove absence twice, then capture billing."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from .models import (
    BILLING_BUCKET_WIDTH,
    AbsenceObservation,
    BillingState,
    CloseState,
    CostCapture,
    PodRecord,
    Presence,
    ProviderStatus,
    require_billing_cutoff_margin_seconds,
    require_utc,
    utc_now,
)
from .provider import PodProvider


@dataclass(frozen=True, slots=True)
class ShutdownReadiness:
    """Proof that the local closure machinery is wired before a paid launch."""

    ready: bool
    missing_verbs: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class CloseReport:
    """A bounded observation report, not a claim that future billing is impossible."""

    pod_id: str
    state: CloseState
    reason: str
    cutoff_at: datetime
    terminate_attempts: int
    billing_attempts: int
    pod_get_absent: bool
    pod_list_absent: bool
    cost_capture: CostCapture | None
    volume_id: str
    volume_ongoing_hourly_usd: Decimal
    last_detail: str
    manual_action: str | None

    def __post_init__(self) -> None:
        require_utc(self.cutoff_at, "close report cutoff")
        if not isinstance(self.pod_id, str) or not self.pod_id.strip():
            raise ValueError("close report pod_id must be non-blank")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("close report reason must be non-blank")
        if not isinstance(self.terminate_attempts, int) or self.terminate_attempts < 1:
            raise ValueError("close report must record at least one terminate attempt")
        if not isinstance(self.billing_attempts, int) or self.billing_attempts < 0:
            raise ValueError("close report billing attempts must be a non-negative integer")
        if self.state is CloseState.VERIFIED:
            capture = self.cost_capture
            if not self.pod_get_absent or not self.pod_list_absent:
                raise ValueError("verified close requires both exact-pod and pod-list absence")
            if (
                capture is None
                or capture.state is not BillingState.CAPTURED
                or capture.pod_id != self.pod_id
                or not capture.lines
            ):
                raise ValueError("verified close requires captured billing for this exact pod")
            if capture.cutoff_at != self.cutoff_at:
                raise ValueError("verified close cutoff must equal its billing capture cutoff")
            if self.manual_action is not None:
                raise ValueError("verified close cannot also require manual action")

    @property
    def verified(self) -> bool:
        return self.state is CloseState.VERIFIED

    @property
    def captured_cost_usd(self) -> Decimal | None:
        return self.cost_capture.total_usd if self.cost_capture else None

    def to_record(self) -> dict[str, object]:
        cost = self.cost_capture
        return {
            "pod_id": self.pod_id,
            "state": self.state.value,
            "reason": self.reason,
            "cutoff_at": self.cutoff_at.isoformat().replace("+00:00", "Z"),
            "terminate_attempts": self.terminate_attempts,
            "billing_attempts": self.billing_attempts,
            "pod_get_absent": self.pod_get_absent,
            "pod_list_absent": self.pod_list_absent,
            "cost_capture": None
            if cost is None
            else {
                "state": cost.state.value,
                "window_start_at": (
                    cost.window_start_at.isoformat().replace("+00:00", "Z")
                    if cost.window_start_at is not None
                    else None
                ),
                "cutoff_at": cost.cutoff_at.isoformat().replace("+00:00", "Z"),
                "total_usd": str(cost.total_usd) if cost.total_usd is not None else None,
                "records": len(cost.lines),
                "lines": [
                    {
                        "amount_usd": str(line.amount_usd),
                        "description": line.description,
                        "recorded_at": (
                            line.recorded_at.isoformat().replace("+00:00", "Z")
                            if line.recorded_at is not None
                            else None
                        ),
                    }
                    for line in cost.lines
                ],
                "reason": cost.reason,
                "source": cost.source,
            },
            "volume": {
                "id": self.volume_id,
                "ongoing_hourly_usd": str(self.volume_ongoing_hourly_usd),
                "decision": (
                    "unchanged by pod close; retention or deletion requires separate authorization"
                ),
            },
            "last_detail": self.last_detail,
            "manual_action": self.manual_action,
        }


BILLING_RECONCILIATION_ATTEMPTS = 3
BILLING_RECONCILIATION_RETRY_SECONDS = 15.0
"""The fixed-in-code billing reconciliation schedule (config/spend.toml names
it so a configured shutdown deadline is not silently short by its tail).
`SpendPolicy` computes its deadline-versus-lifetime bound from these same
symbols, so changing the schedule cannot silently unhinge that bound."""


class VerifiedShutdown:
    """Closure controller whose only green state has three independent observations."""

    _VERBS = (
        "estimate",
        "create",
        "adopt",
        "status",
        "terminate",
        "verify_absent",
        "capture_cost",
    )

    def __init__(
        self,
        provider: PodProvider,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 2.0,
        billing_attempts: int = BILLING_RECONCILIATION_ATTEMPTS,
        billing_retry_seconds: float = BILLING_RECONCILIATION_RETRY_SECONDS,
        billing_cutoff_margin_seconds: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("shutdown timeout and poll interval must be positive")
        if billing_attempts < 1 or billing_retry_seconds < 0:
            raise ValueError("billing reconciliation must allow at least one attempt")
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        # Billing lags termination, and an adapter that cannot tell "not posted
        # yet" from "nothing to post" has no pending state to return. A bounded,
        # recorded retry absorbs the lag before an empty response becomes the
        # terminal UNVERIFIED_BILLING that needs a human at the console — bounded
        # because GOVERNANCE 11 does not allow an unbounded reconsideration loop.
        self.billing_attempts = billing_attempts
        self.billing_retry_seconds = billing_retry_seconds
        self.billing_cutoff_margin_seconds = require_billing_cutoff_margin_seconds(
            billing_cutoff_margin_seconds, "billing cutoff margin"
        )
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.now = now

    def prove_ready(self) -> ShutdownReadiness:
        """Check the local closure wiring before a launch asks a provider to bill."""

        missing = tuple(
            name for name in self._VERBS if not callable(getattr(self.provider, name, None))
        )
        if missing:
            return ShutdownReadiness(False, missing, "provider seam lacks shutdown verbs")
        return ShutdownReadiness(True, (), "local terminate/absence/billing path is wired")

    def close(self, record: PodRecord, *, reason: str) -> CloseReport:
        """Terminate until GET-404 and list absence agree, then capture actual billing."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("shutdown reason must be non-blank")
        deadline = self.monotonic() + self.timeout_seconds
        attempts = 0
        last_detail = "no provider observation yet"
        last_terminate_failure: str | None = None
        get_absent = False
        list_absent = False

        # The first operation is an idempotent terminate, never an optimistic status check.
        attempts, terminate_failure = self._terminate(record.pod_id, attempts)
        if terminate_failure is not None:
            last_terminate_failure = terminate_failure
        while True:
            status = self._status(record.pod_id)
            listed = self._list_absence(record.pod_id)
            get_absent = status.presence is Presence.ABSENT
            list_absent = listed.presence is Presence.ABSENT
            last_detail = _join_details(status.detail, listed.detail)

            if get_absent and list_absent:
                cutoff = require_utc(self.now(), "billing cutoff")
                return self._capture_billing(
                    record,
                    reason=reason,
                    cutoff=cutoff,
                    attempts=attempts,
                    last_detail=_join_details(last_detail, last_terminate_failure or ""),
                )

            if self.monotonic() >= deadline:
                return self._failed_shutdown(
                    record,
                    reason,
                    attempts,
                    get_absent,
                    list_absent,
                    _join_details(last_detail, last_terminate_failure or ""),
                )

            # 202/204 or a caught timeout is not a close.  Re-issue termination
            # while either independent observation is still anything but absent.
            attempts, terminate_failure = self._terminate(record.pod_id, attempts)
            if terminate_failure is not None:
                last_terminate_failure = terminate_failure
            remaining = max(0.0, deadline - self.monotonic())
            self.sleeper(min(self.poll_seconds, remaining))

    def _terminate(self, pod_id: str, attempts: int) -> tuple[int, str | None]:
        try:
            self.provider.terminate(pod_id)
            return attempts + 1, None
        except Exception as error:
            return attempts + 1, f"terminate failed: {error}"

    def _status(self, pod_id: str) -> ProviderStatus:
        try:
            observation = self.provider.status(pod_id)
        except Exception as error:
            return ProviderStatus(
                pod_id, Presence.UNKNOWN, require_utc(self.now(), "status error time"), str(error)
            )
        if not isinstance(observation, ProviderStatus):
            return ProviderStatus(
                pod_id,
                Presence.UNKNOWN,
                require_utc(self.now(), "invalid status observation time"),
                "provider returned an invalid exact-pod status observation",
            )
        if observation.pod_id != pod_id:
            return ProviderStatus(
                pod_id,
                Presence.UNKNOWN,
                observation.observed_at,
                "provider status response names a different pod; exact-pod absence is unproven",
                observation.http_status,
            )
        if observation.presence is Presence.ABSENT and observation.http_status != 404:
            return ProviderStatus(
                pod_id,
                Presence.UNKNOWN,
                observation.observed_at,
                "provider exact-pod status reports absence without the required GET-404 proof",
                observation.http_status,
            )
        return observation

    def _list_absence(self, pod_id: str) -> AbsenceObservation:
        try:
            observation = self.provider.verify_absent(pod_id)
        except Exception as error:
            return AbsenceObservation(
                pod_id,
                Presence.UNKNOWN,
                require_utc(self.now(), "list error time"),
                str(error),
            )
        if not isinstance(observation, AbsenceObservation):
            return AbsenceObservation(
                pod_id,
                Presence.UNKNOWN,
                require_utc(self.now(), "invalid list observation time"),
                "provider returned an invalid pod-list absence observation",
            )
        if observation.pod_id != pod_id:
            return AbsenceObservation(
                pod_id,
                Presence.UNKNOWN,
                observation.observed_at,
                "provider pod-list observation names a different pod; list absence is unproven",
            )
        return observation

    def _capture_billing(
        self,
        record: PodRecord,
        *,
        reason: str,
        cutoff: datetime,
        attempts: int,
        last_detail: str,
    ) -> CloseReport:
        capture: object
        for attempt in range(1, self.billing_attempts + 1):
            try:
                capture = self.provider.capture_cost(record.pod_id, record.created_at, cutoff)
            except Exception as error:
                capture = error
            else:
                if not isinstance(capture, CostCapture) or capture.state is BillingState.CAPTURED:
                    break
            if attempt < self.billing_attempts:
                self.sleeper(self.billing_retry_seconds)

        def report(
            state: CloseState,
            report_cutoff: datetime,
            evidence: CostCapture | None,
            detail: str,
            manual_action: str | None,
        ) -> CloseReport:
            # Reached only where both absence observations already agreed, which
            # is why the two absence flags below are unconditional.
            return CloseReport(
                record.pod_id,
                state,
                reason,
                report_cutoff,
                attempts,
                attempt,
                True,
                True,
                evidence,
                record.volume_id,
                record.estimate.volume_hourly_usd,
                detail,
                manual_action,
            )

        console = "Review the provider billing console for this pod; no zero cost was inferred."
        if isinstance(capture, BaseException):
            return report(
                CloseState.UNVERIFIED_BILLING,
                cutoff,
                None,
                _join_details(last_detail, f"billing capture failed: {capture}"),
                console,
            )
        evidence_error = _billing_evidence_error(
            record,
            capture,
            requested_cutoff=cutoff,
            billing_cutoff_margin_seconds=self.billing_cutoff_margin_seconds,
        )
        if evidence_error is not None:
            return report(
                CloseState.UNVERIFIED_BILLING,
                cutoff,
                capture if isinstance(capture, CostCapture) else None,
                _join_details(last_detail, evidence_error),
                "Review the provider billing console for this pod; returned billing evidence did not prove the requested pod and time window.",
            )
        assert isinstance(capture, CostCapture)
        if capture.state is BillingState.CAPTURED:
            # CostCapture refuses to construct a CAPTURED capture with empty
            # lines, so the state alone already implies non-empty lines here.
            return report(CloseState.VERIFIED, capture.cutoff_at, capture, last_detail, None)
        if capture.state is BillingState.PENDING_RECONCILIATION:
            return report(
                CloseState.PENDING_RECONCILIATION,
                capture.cutoff_at,
                capture,
                _join_details(last_detail, capture.reason),
                "Re-run billing reconciliation after the provider posts this pod's charges.",
            )
        # An empty billing result for a pod that ran is specifically non-green.
        return report(
            CloseState.UNVERIFIED_BILLING,
            capture.cutoff_at,
            capture,
            _join_details(last_detail, capture.reason or "billing returned no verifiable records"),
            console,
        )

    def _failed_shutdown(
        self,
        record: PodRecord,
        reason: str,
        attempts: int,
        get_absent: bool,
        list_absent: bool,
        last_detail: str,
    ) -> CloseReport:
        # Presence was never proven, so billing capture is not attempted here:
        # the window this pod actually ran is not closed, and a captured
        # number would read as the total while really being a partial one.
        # The report's own manual_action sends the operator to the console
        # instead of showing a figure that could be mistaken for final.
        return CloseReport(
            record.pod_id,
            CloseState.FAILED_SHUTDOWN,
            reason,
            require_utc(self.now(), "failed shutdown cutoff"),
            attempts,
            0,
            get_absent,
            list_absent,
            None,
            record.volume_id,
            record.estimate.volume_hourly_usd,
            last_detail,
            "Provider absence was not proven by both exact-pod GET and pod list; review now.",
        )


def _join_details(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _billing_evidence_error(
    record: PodRecord,
    capture: object,
    *,
    requested_cutoff: datetime,
    billing_cutoff_margin_seconds: int,
) -> str | None:
    """Return why a provider billing response cannot prove this close.

    Provider adapters are independently testable, but the generic verifier is
    the final money-path boundary.  It binds returned evidence to the exact pod
    and requested time window so a future adapter cannot turn another pod's
    records (or a narrowed/older query) into a green close.
    """

    if not isinstance(capture, CostCapture):
        return "provider returned an invalid billing capture object"
    if capture.pod_id != record.pod_id:
        return "billing capture names a different pod; this pod's charges are unverified"
    if capture.cutoff_at < requested_cutoff:
        return "billing capture cutoff precedes the requested shutdown cutoff"
    margin = timedelta(
        seconds=require_billing_cutoff_margin_seconds(
            billing_cutoff_margin_seconds, "billing cutoff margin"
        )
    )
    if capture.cutoff_at > requested_cutoff + margin:
        return "billing capture cutoff is too far beyond the requested window to be verified"
    if capture.window_start_at is None:
        return "billing capture omits its window start; coverage from pod creation is unproven"
    if capture.window_start_at > record.created_at:
        return "billing capture starts after pod creation; the requested charge window is narrowed"
    # The generic seam is the final money-path boundary, so it bounds every
    # dated record to the capture's own declared window -- the same one-bucket
    # slack the RunPod adapter applies, from the shared symbol, so a future
    # adapter cannot total another window's records into a green close.
    # Undated records pass: not every provider stamps its lines, and absence
    # of a stamp is already visible on the receipt.
    bucket_slack = BILLING_BUCKET_WIDTH
    for line in capture.lines:
        if line.recorded_at is None:
            continue
        if (
            line.recorded_at < capture.window_start_at - bucket_slack
            or line.recorded_at > capture.cutoff_at
        ):
            return (
                "billing capture contains a record dated outside its own declared "
                "window; cost attribution is unverifiable"
            )
    return None
