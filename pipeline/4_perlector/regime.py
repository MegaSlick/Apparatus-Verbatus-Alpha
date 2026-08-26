"""The witness regime: named or blinded, and the pseudonym scheme blinding uses.

Tyrel's 2026-07-30 ruling (courtroom_doctrine.md, formalized in spec_08):
`witness_context = named | blinded` is run-level configuration. Named is the
default; blinded exists so that if training ever shows the named regime
breeding bias toward a particular witness, the switch flips without a rebuild.

Blinding must be reversible (spec_08) without becoming a second copy of the
roster that can drift from `run.json["witness_chairs"]`. Rather than store a
map, a pseudonym is a deterministic digest of the run's own sealed facts plus
the chair name, so reversal is: recompute the same digest for each configured
chair and match. The roster the map would have carried already lives in
`run.json`; storing a second copy of it here would be one more thing to keep
in sync with the one place it is supposed to live.
"""

from __future__ import annotations

from common.witness_regime import (  # noqa: F401  (stage-local compatibility surface)
    BLINDED,
    NAMED,
    REGIMES,
    pseudonym_for,
    witness_label,
)
