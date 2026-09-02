"""Deterministic fake provider used by the pod-runtime tests.

It has a price sheet, independent GET/list disappearance delays, and injectable
provider failures.  It intentionally does not imitate credentials or make HTTP
requests.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    AbsenceObservation,
    AccountBalanceObservation,
    BillingState,
    CostCapture,
    CostLine,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    PodRuntimeContract,
    Presence,
    ProviderFailure,
    ProviderStatus,
    as_decimal,
    parse_billing_cutoff_margin_seconds,
    utc_now,
)


class FakeProvider:
    """In-memory ``PodProvider`` implementation with observable call history."""

    def __init__(
        self,
        price_sheet: dict[str, tuple[Decimal | str, Decimal | str]] | None = None,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.price_sheet = price_sheet or {"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}
        self.now = now
        self.pods: dict[str, PodRecord] = {}
        self._present: dict[str, bool] = {}
        self._get_lag: dict[str, int] = defaultdict(int)
        self._list_lag: dict[str, int] = defaultdict(int)
        self._billing: dict[str, CostCapture] = {}
        self._failures: dict[str, deque[BaseException]] = defaultdict(deque)
        self._post_create_failures: deque[BaseException] = deque()
        self.calls: list[tuple[str, str | None]] = []
        self.create_requests: list[PodCreateRequest] = []
        self._requests_by_pod: dict[str, PodCreateRequest] = {}
        self.terminate_calls: list[str] = []
        self._next_id = 1
        self._account_balance_usd = Decimal("100.00")

    def inject_failure(self, verb: str, error: BaseException, *, times: int = 1) -> None:
        """Make a verb raise ``error`` the requested finite number of times."""

        if times < 1:
            raise ValueError("failure injection times must be positive")
        self._failures[verb].extend(error for _ in range(times))

    def inject_post_create_failure(self, error: BaseException) -> None:
        """Create one fake pod, then lose the client response like a crashed POST."""

        self._post_create_failures.append(error)

    def set_disappearance_lag(
        self, pod_id: str, *, get_polls: int = 0, list_polls: int = 0
    ) -> None:
        """Keep independent GET/list observations present after termination."""

        self._get_lag[pod_id] = get_polls
        self._list_lag[pod_id] = list_polls

    def set_billing(self, capture: CostCapture) -> None:
        self._billing[capture.pod_id] = capture

    def set_pod_state(self, pod_id: str, state: str) -> None:
        """Move an existing fake pod's observed lifecycle word, e.g. to EXITED.

        Presence and lifecycle are independent facts here as everywhere else:
        this never removes the pod from ``_present``, so a caller can put a
        pod in exactly the shape a real EXITED pod has -- PRESENT to `status`
        and still billing its attached volume.
        """

        record = self.pods.get(pod_id)
        if record is None:
            raise ProviderFailure(f"fake provider cannot set state on unknown pod {pod_id!r}")
        self.pods[pod_id] = replace(record, state=state)

    def set_account_balance(self, available_usd: Decimal | str) -> None:
        self._account_balance_usd = as_decimal(available_usd, "fake available account balance")

    def observe_account_balance(self) -> AccountBalanceObservation:
        self._call("observe_account_balance", None)
        return AccountBalanceObservation(
            self._account_balance_usd, self.now(), "fake provider account balance"
        )

    def estimate(self, request: PodCreateRequest) -> PodEstimate:
        self._call("estimate", request.name)
        try:
            pod_rate, volume_rate = self.price_sheet[request.gpu_type]
        except KeyError as error:
            raise ProviderFailure(f"fake price sheet has no GPU {request.gpu_type!r}") from error
        return PodEstimate(
            pod_hourly_usd=as_decimal(pod_rate, "fake pod rate"),
            volume_hourly_usd=as_decimal(volume_rate, "fake volume rate"),
            source="fake price sheet",
            observed_at=self.now(),
        )

    def create(self, request: PodCreateRequest) -> PodRecord:
        self._call("create", request.name)
        self.create_requests.append(request)
        launch_id = request.metadata.get("VERBATUS_LAUNCH_TOKEN")
        if isinstance(launch_id, str):
            matches = [
                pod_id
                for pod_id, prior in self._requests_by_pod.items()
                if prior.name == request.name
                and prior.metadata.get("VERBATUS_LAUNCH_TOKEN") == launch_id
                and self._present.get(pod_id, False)
            ]
            if len(matches) == 1:
                return self.pods[matches[0]]
            if len(matches) > 1:
                raise ProviderFailure("fake exact launch token matched multiple pods")
        if request.recovery_only:
            raise ProviderFailure("fake recovery found no exact existing launch-token pod")
        estimate = self.estimate(request)
        pod_id = f"fake-pod-{self._next_id}"
        self._next_id += 1
        record = PodRecord(
            pod_id=pod_id,
            name=request.name,
            estimate=estimate,
            volume_id=request.volume_id,
            created_at=self.now(),
            runtime_contract=PodRuntimeContract(
                interruptible=False,
                gpu_type=request.gpu_type,
                image=request.image,
                template=request.template,
                volume_id=request.volume_id,
                volume_mount_path=request.volume_mount_path,
                docker_start_cmd=request.docker_start_cmd,
                billing_cutoff_margin_seconds=parse_billing_cutoff_margin_seconds(
                    request.metadata.get(BILLING_CUTOFF_MARGIN_ENV),
                    "fake request billing cutoff margin",
                ),
            ),
        )
        self.pods[pod_id] = record
        self._requests_by_pod[pod_id] = request
        self._present[pod_id] = True
        if self._post_create_failures:
            raise self._post_create_failures.popleft()
        return record

    def adopt(self, pod_id: str) -> PodRecord:
        self._call("adopt", pod_id)
        record = self.pods.get(pod_id)
        if record is None or not self._present.get(pod_id, False):
            raise ProviderFailure(f"fake provider cannot adopt absent pod {pod_id!r}")
        request = self._requests_by_pod[pod_id]
        # Adoption intentionally re-observes the current fake price sheet rather
        # than reusing the launch-time estimate.
        return PodRecord(
            pod_id=record.pod_id,
            name=record.name,
            estimate=self.estimate(request),
            volume_id=record.volume_id,
            created_at=record.created_at,
            state=record.state,
            runtime_contract=record.runtime_contract,
        )

    def status(self, pod_id: str) -> ProviderStatus:
        self._call("status", pod_id)
        if self._present.get(pod_id, False):
            record = self.pods.get(pod_id)
            provider_state = record.state if record is not None else None
            return ProviderStatus(
                pod_id, Presence.PRESENT, self.now(), http_status=200, provider_state=provider_state
            )
        lag = self._get_lag[pod_id]
        if lag > 0:
            self._get_lag[pod_id] = lag - 1
            return ProviderStatus(
                pod_id, Presence.PRESENT, self.now(), "termination propagating", 200
            )
        return ProviderStatus(pod_id, Presence.ABSENT, self.now(), http_status=404)

    def terminate(self, pod_id: str) -> None:
        self._call("terminate", pod_id)
        self.terminate_calls.append(pod_id)
        if pod_id not in self.pods:
            return
        self._present[pod_id] = False

    def verify_absent(self, pod_id: str) -> AbsenceObservation:
        self._call("verify_absent", pod_id)
        if self._present.get(pod_id, False):
            return AbsenceObservation(pod_id, Presence.PRESENT, self.now(), "pod remains in list")
        lag = self._list_lag[pod_id]
        if lag > 0:
            self._list_lag[pod_id] = lag - 1
            return AbsenceObservation(
                pod_id, Presence.PRESENT, self.now(), "list propagation pending"
            )
        return AbsenceObservation(pod_id, Presence.ABSENT, self.now(), "pod absent from list")

    def capture_cost(self, pod_id: str, started_at: datetime, cutoff_at: datetime) -> CostCapture:
        self._call("capture_cost", pod_id)
        capture = self._billing.get(pod_id)
        if capture is not None:
            return capture
        record = self.pods.get(pod_id)
        if record is None:
            return CostCapture(
                pod_id=pod_id,
                state=BillingState.UNAVAILABLE,
                cutoff_at=cutoff_at,
                reason="fake billing has no observation for this pod",
            )
        # The shipped RunPod adapter's `_unavailable` always names its window
        # start; the fake matches that shape so a test reaches the same
        # empty-billing branch the real adapter would, not the missing-window
        # evidence error.  The start is clamped strictly below the cutoff: a
        # real close always takes time, but a deterministic test clock may not
        # have moved, and CostCapture refuses a zero-width window.
        return CostCapture(
            pod_id=pod_id,
            state=BillingState.UNAVAILABLE,
            cutoff_at=cutoff_at,
            reason="fake billing deliberately has no records; zero is not inferred",
            window_start_at=min(record.created_at, cutoff_at - timedelta(seconds=1)),
        )

    def bill(
        self, pod_id: str, amount_usd: Decimal | str, *, description: str = "pod runtime"
    ) -> None:
        """Install a provider-computed billing result for a successful close test."""

        record = self.pods.get(pod_id)
        if record is None:
            raise ProviderFailure(f"fake provider cannot bill unknown pod {pod_id!r}")
        self.set_billing(
            CostCapture(
                pod_id=pod_id,
                state=BillingState.CAPTURED,
                # The deterministic test clock commonly has not advanced
                # between create and fixture setup.  A provider-resolved later
                # cutoff keeps this a valid non-empty billing window and is
                # still sufficient evidence for every close before it.
                cutoff_at=self.now() + timedelta(hours=1),
                lines=(CostLine(as_decimal(amount_usd, "fake bill"), description, self.now()),),
                source="fake provider billing",
                window_start_at=record.created_at,
            )
        )

    def _call(self, verb: str, subject: str | None) -> None:
        self.calls.append((verb, subject))
        failures = self._failures[verb]
        if failures:
            error = failures.popleft()
            raise error
