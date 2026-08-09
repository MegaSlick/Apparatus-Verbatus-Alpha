"""Controller-arming contract kept separate from provider API details.

The laptop supervisor and pod dead-man are not optional conveniences.  A paid
create/adopt is green only after an implementation has both started a durable
laptop controller and observed a pod-side timer acknowledgement.  This module
contains no provider endpoint, credential, or provider-name knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol

from .lease import LeaseStore, PodLease
from .models import PodCreateRequest, PodRecord, require_utc
from .spend import SpendPolicy


@dataclass(frozen=True, slots=True)
class ControllerReadiness:
    """Pre-create proof that a real two-controller handshake can be attempted."""

    ready: bool
    observed_at: datetime
    detail: str
    receipt: Mapping[str, object]

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "controller readiness observed_at")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("controller readiness detail must be non-blank")
        if not isinstance(self.receipt, Mapping):
            raise ValueError("controller readiness receipt must be a mapping")
        _assert_nonsecret_receipt(self.receipt)

    def to_record(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "detail": self.detail,
            "receipt": dict(self.receipt),
        }


@dataclass(frozen=True, slots=True)
class ControllerArming:
    """Post-create evidence that the two independent spend controllers are armed."""

    laptop_supervisor_started: bool
    pod_timer_acknowledged: bool
    observed_at: datetime
    detail: str
    receipt: Mapping[str, object]

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "controller arming observed_at")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("controller arming detail must be non-blank")
        if not isinstance(self.receipt, Mapping):
            raise ValueError("controller arming receipt must be a mapping")
        _assert_nonsecret_receipt(self.receipt)

    @property
    def armed(self) -> bool:
        return self.laptop_supervisor_started and self.pod_timer_acknowledged

    def to_record(self) -> dict[str, object]:
        return {
            "laptop_supervisor_started": self.laptop_supervisor_started,
            "pod_timer_acknowledged": self.pod_timer_acknowledged,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "detail": self.detail,
            "receipt": dict(self.receipt),
        }


class ControllerArmer(Protocol):
    """Effect seam for the two-controller handshake, not a provider seam.

    ``preflight`` runs before a paid request and must fail closed when the
    implementation cannot start a durable supervisor or obtain a timer
    acknowledgement after creation. ``arm`` starts/proves both controllers
    after a record exists; callers immediately use verified shutdown if it
    returns non-armed.
    """

    def preflight(
        self, *, action: str, request: PodCreateRequest, policy: SpendPolicy
    ) -> ControllerReadiness:
        """Prove this action has an available two-controller arming procedure."""

    def arm(
        self,
        *,
        action: str,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
        store: LeaseStore,
        owner_token: str,
        policy: SpendPolicy,
    ) -> ControllerArming:
        """Start/prove controllers and return their independently observed receipt."""


class FailClosedControllerArmer:
    """Default launcher posture until an approved runtime handshake is supplied."""

    def __init__(self, *, now: Callable[[], datetime], detail: str | None = None) -> None:
        self.now = now
        self.detail = detail or (
            "no durable laptop-supervisor and pod-timer acknowledgement implementation was supplied"
        )

    def preflight(
        self, *, action: str, request: PodCreateRequest, policy: SpendPolicy
    ) -> ControllerReadiness:
        return ControllerReadiness(False, self.now(), self.detail, {"action": action})

    def arm(
        self,
        *,
        action: str,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
        store: LeaseStore,
        owner_token: str,
        policy: SpendPolicy,
    ) -> ControllerArming:
        return ControllerArming(False, False, self.now(), self.detail, {"action": action})


class CallbackControllerArmer:
    """Explicit integration point for an approved external controller harness.

    The callbacks are intentionally injected rather than silently reading an
    environment default.  A live operator must supply one that starts the
    laptop process and checks a durable acknowledgement from the pod.  Tests
    use this seam to drill death of each controller after the launch handshake.
    """

    def __init__(
        self,
        *,
        preflight_callback: Callable[[str, PodCreateRequest, SpendPolicy], ControllerReadiness],
        arm_callback: Callable[
            [str, PodCreateRequest, PodRecord, PodLease, LeaseStore, str, SpendPolicy],
            ControllerArming,
        ],
    ) -> None:
        self.preflight_callback = preflight_callback
        self.arm_callback = arm_callback

    def preflight(
        self, *, action: str, request: PodCreateRequest, policy: SpendPolicy
    ) -> ControllerReadiness:
        return self.preflight_callback(action, request, policy)

    def arm(
        self,
        *,
        action: str,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
        store: LeaseStore,
        owner_token: str,
        policy: SpendPolicy,
    ) -> ControllerArming:
        return self.arm_callback(action, request, record, lease, store, owner_token, policy)


def _assert_nonsecret_receipt(value: Mapping[str, object]) -> None:
    """Controller receipts live in leases, so capability material is forbidden."""

    forbidden = ("key", "secret", "password", "credential", "bearer", "token")
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise ValueError("controller receipt keys must be strings")
        normalized = item_key.lower().replace("-", "_")
        if any(marker in normalized for marker in forbidden):
            raise ValueError("controller receipt must not retain credential or token material")
        if isinstance(item_value, Mapping):
            _assert_nonsecret_receipt(item_value)
