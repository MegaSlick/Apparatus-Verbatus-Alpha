"""Closed refusals for the model-seat boundary.

GOVERNANCE 6: Every stored reading carries the resolved identity and revision of the
model that produced it, at the moment it was produced.  The registry therefore
refuses a mismatch rather than changing a role, revision, cache, adapter, or recipe
to make a request succeed.
"""

from __future__ import annotations

from common.contracts.errors import ContractError


class ChairRefusal(ContractError):
    """Base refusal with the requested seat and the concrete mismatch."""

    code = "seat-refusal"

    def __init__(self, chair: str, difference: str):
        self.chair = chair
        self.difference = difference
        super().__init__(f"seat {chair!r}: {difference}")


class ConfigurationRefusal(ChairRefusal):
    """The named seat does not conform to the models.toml schema."""

    code = "configuration"


class UnresolvedChairRefusal(ChairRefusal):
    """The requested role cannot resolve to its own configured identity."""

    code = "unresolved-seat"


class DigestMismatchRefusal(ChairRefusal):
    """A manifest or snapshot differs from the identity's pinned digest."""

    code = "digest-mismatch"


class CacheRevisionRefusal(ChairRefusal):
    """A cache describes a different pin than the requested identity."""

    code = "cache-revision"


class AdapterFetchRefusal(ChairRefusal):
    """An adapter could not be fetched; its base is never substituted."""

    code = "adapter-fetch"


class ServingRecipeRefusal(ChairRefusal):
    """The named serving recipe did not start; no second route is considered."""

    code = "serving-recipe"


class LocalPathRefusal(ChairRefusal):
    """A local-repository path is unsafe, absent, or resolves outside model_root."""

    code = "local-path"


class ReceiptRefusal(ChairRefusal):
    """A serving receipt is incomplete or disagrees with its resolved identity."""

    code = "receipt"


class ProtocolClauseRefusal(ChairRefusal):
    """An implementation breaks one named clause of the seat protocol."""

    code = "protocol-clause"


ALL_REFUSAL_TYPES = (
    ConfigurationRefusal,
    UnresolvedChairRefusal,
    DigestMismatchRefusal,
    CacheRevisionRefusal,
    AdapterFetchRefusal,
    ServingRecipeRefusal,
    LocalPathRefusal,
    ReceiptRefusal,
    ProtocolClauseRefusal,
)
"""The complete public taxonomy. New behaviour must use an existing refusal."""


def is_closed_refusal(error: BaseException) -> bool:
    """True only for an exact member of this boundary's public taxonomy."""

    return type(error) in ALL_REFUSAL_TYPES
