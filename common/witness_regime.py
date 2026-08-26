"""Shared witness labels for producers and independent downstream verifiers.

The run-scoped pseudonym is part of the evidence contract, not a Perlector-only
implementation detail: a downstream stage must be able to re-derive which blinded
label belongs to which retained witness without importing an upstream pipeline stage.
"""

from __future__ import annotations

from typing import Final

from common.contracts.canonical import digest_of
from common.stage import WITNESS_CONTEXT_REGIMES

NAMED: Final = "named"
BLINDED: Final = "blinded"
REGIMES: Final = frozenset(WITNESS_CONTEXT_REGIMES)

_PSEUDONYM_DIGITS: Final = 12


def pseudonym_for(chair: str, *, run_id: str, config_digest: str) -> str:
    """Return the stable, run-scoped pseudonym for one chair."""
    if not chair:
        raise ValueError("a pseudonym cannot be derived for an unnamed chair")
    digest = digest_of(
        {
            "purpose": "witness-blinding",
            "run_id": run_id,
            "config_digest": config_digest,
            "chair": chair,
        }
    )
    return f"witness-{digest[:_PSEUDONYM_DIGITS]}"


def witness_label(chair: str, *, regime: str, run_id: str, config_digest: str) -> str:
    """Return the label a dossier may show for one witness under ``regime``."""
    if regime not in REGIMES:
        raise ValueError(f"witness regime {regime!r} is not one of {sorted(REGIMES)}")
    if regime == NAMED:
        return chair
    if regime == BLINDED:
        return pseudonym_for(chair, run_id=run_id, config_digest=config_digest)
    raise ValueError(
        f"witness regime {regime!r} is declared in common.stage but this module has no "
        "label rule for it; a new regime must be handled here, never blinded by default"
    )
