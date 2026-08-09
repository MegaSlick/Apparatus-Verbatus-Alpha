"""Fail-closed machinery for a rented GPU pod.

Nothing in this package starts a pod at import time.  The production provider is
an adapter behind :mod:`operations.pod.provider`; tests use the in-memory fake.
"""

from .models import (
    BillingState,
    CloseState,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    ProviderStatus,
)

__all__ = [
    "BillingState",
    "CloseState",
    "PodCreateRequest",
    "PodEstimate",
    "PodRecord",
    "ProviderStatus",
]
