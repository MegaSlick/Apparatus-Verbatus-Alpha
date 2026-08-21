"""The provider seam.

Every provider-specific protocol detail belongs in one ``provider_<name>.py``
adapter.  Callers know only these seven verbs and can therefore run entirely
against a fake provider.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import (
    AbsenceObservation,
    AccountBalanceObservation,
    CostCapture,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    ProviderStatus,
)


@runtime_checkable
class AccountBalanceProvider(Protocol):
    """The separate observed-balance seam used by the spend floor."""

    def observe_account_balance(self) -> AccountBalanceObservation:
        """Return the currently available account balance from a named source."""


@runtime_checkable
class PodProvider(Protocol):
    """Minimal paid-infrastructure contract; no volume deletion verb exists."""

    def estimate(self, request: PodCreateRequest) -> PodEstimate:
        """Return the current provider price for this exact requested pod."""

    def create(self, request: PodCreateRequest) -> PodRecord:
        """Create one non-interruptible pod with its volume attached at creation.

        ``VERBATUS_LAUNCH_TOKEN`` makes this operation idempotent for the
        runtime's durable recovery path.  An adapter first correlates an exact
        existing name-plus-token record.  When ``request.recovery_only`` is
        true it must *only* perform that lookup: no POST is permitted.  An
        ambiguous post-request failure therefore remains recoverable after the
        original client dies, without an eighth provider verb.
        """

    def adopt(self, pod_id: str) -> PodRecord:
        """Read the named existing pod for a separately gated adoption."""

    def status(self, pod_id: str) -> ProviderStatus:
        """Observe that exact pod, including an explicit 404-as-absent result."""

    def terminate(self, pod_id: str) -> None:
        """Request idempotent termination; an acknowledgement is not proof."""

    def verify_absent(self, pod_id: str) -> AbsenceObservation:
        """Independently observe that the pod is absent from the provider pod list."""

    def capture_cost(self, pod_id: str, started_at: datetime, cutoff_at: datetime) -> CostCapture:
        """Capture provider billing records through ``cutoff_at`` for this pod."""
