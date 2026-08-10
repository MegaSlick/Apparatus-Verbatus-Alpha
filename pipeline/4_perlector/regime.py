"""The witness regime: named or blinded, and the pseudonym scheme blinding uses.

Tyrel's 2026-07-30 ruling (courtroom_doctrine.md, formalized in spec_08):
`witness_context = named | blinded` is run-level configuration. Named is the
default; blinded exists so that if training ever shows the named regime
breeding bias toward a particular witness, the switch flips without a rebuild.
`common/stage.py::validate_serving_provenance` already refuses a Perlectio whose
`witness_regime` is not one of these two -- this module is what actually
produces a blinded dossier rather than only naming the field.

Blinding must be reversible (spec_08) without becoming a second copy of the
roster that can drift from `run.json["witness_chairs"]`. Rather than store a
map, a pseudonym is a deterministic digest of the run's own sealed facts plus
the chair name, so reversal is: recompute the same digest for each configured
chair and match. The roster the map would have carried already lives in
`run.json`; storing a second copy of it here would be one more thing to keep
in sync with the one place it is supposed to live.
"""

from __future__ import annotations

from typing import Final

from common.contracts.canonical import digest_of

NAMED: Final = "named"
BLINDED: Final = "blinded"
REGIMES: Final = frozenset({NAMED, BLINDED})

_PSEUDONYM_DIGITS: Final = 12


def pseudonym_for(chair: str, *, run_id: str, config_digest: str) -> str:
    """A stable, run-scoped pseudonym for one chair. Deterministic, not random."""
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
    """The label a dossier may show for one witness, under the active regime."""
    if regime not in REGIMES:
        raise ValueError(f"witness regime {regime!r} is not one of {sorted(REGIMES)}")
    if regime == NAMED:
        return chair
    return pseudonym_for(chair, run_id=run_id, config_digest=config_digest)
