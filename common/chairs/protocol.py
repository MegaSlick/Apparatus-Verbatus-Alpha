"""Executable contract mechanics for independent chair implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .errors import ProtocolClauseRefusal
from .models import AbsentChair, ChairIdentity, ServingDetails, ServingReceipt, VerifiedSnapshot


@runtime_checkable
class ChairProtocol(Protocol):
    """The caller-visible chair interface; lifecycle is intentionally absent."""

    def resolve(self, role: str) -> ChairIdentity | AbsentChair:
        """Resolve only the role asked for."""

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        """Verify only that exact resolved identity."""

    def receipt(self, identity: ChairIdentity, serving: ServingDetails) -> ServingReceipt:
        """Return a receipt for that identity and serving observation."""


def exercise_contract(
    implementation: ChairProtocol,
    *,
    role: str,
    expected_identity: ChairIdentity,
    serving: ServingDetails,
) -> VerifiedSnapshot:
    """Exercise all three clauses and name the clause an incompatible implementation breaks."""

    if not isinstance(implementation, ChairProtocol):
        raise ProtocolClauseRefusal(
            role, "protocol shape: implementation lacks resolve, ensure, or receipt"
        )
    resolved = implementation.resolve(role)
    if not isinstance(resolved, ChairIdentity):
        raise ProtocolClauseRefusal(
            role, "resolve clause: configured role did not return its identity"
        )
    if resolved.role != role or resolved != expected_identity:
        raise ProtocolClauseRefusal(
            role, "resolve clause: returned an identity other than the requested pin"
        )
    snapshot = implementation.ensure(resolved)
    if not isinstance(snapshot, VerifiedSnapshot) or snapshot.identity != expected_identity:
        raise ProtocolClauseRefusal(
            role, "ensure clause: did not return a verified snapshot of the resolved pin"
        )
    receipt = implementation.receipt(resolved, serving)
    if not isinstance(receipt, ServingReceipt) or receipt.identity != expected_identity:
        raise ProtocolClauseRefusal(
            role, "receipt clause: receipt does not name the resolved identity"
        )
    return snapshot
