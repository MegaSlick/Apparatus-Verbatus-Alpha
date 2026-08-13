"""Lectio nuda: the unprimed instrument reading, sampled by a predeclared design.

ARCHITECTURE: "Lectio nuda -- an unprimed Lectio. No witness shown. The
baseline." Spec_08: nuda "never establishes text: it is an instrument record
with no path to the Archetypus constructor," and it "runs on a predeclared,
Tyrel-approved sampling design (`nuda_fraction` plus selection rule, fixed
before the run; cost is recorded but never silently narrows the instrument --
GOVERNANCE 10)."

`common/contracts/canonical.py` refuses floats everywhere a value is hashed
into an artifact or a run's sealed configuration, so a literal `nuda_fraction`
float cannot be sealed the way `pdf_target_dpi` or `witness_context` are.
`nuda_per_mille` is the same fraction expressed as a non-negative integer in
[0, 1000], which the sampling rule below turns back into the requested share
without ever holding a float. The rule is a deterministic hash threshold, not
`random`: two runs of the identical command must sample the identical acts, or
"repeating the identical command leaves every byte unchanged" stops being true
the moment nuda is turned on.
"""

from __future__ import annotations

from typing import Final

from common.contracts.canonical import digest_of
from common.stage import MAX_NUDA_PER_MILLE

LECTIO_NUDA_KIND: Final = "lectio-nuda"

# The selection rule, named so the record says which design produced the
# sample rather than leaving it implicit in whichever revision of this file
# happened to run. Spec 08 asks for "`nuda_fraction` plus selection rule, fixed
# before the run"; the rate is sealed in `config_digest` and the rule is this.
SELECTION_RULE: Final = "digest-threshold-over-run-id-and-act-id.v1"


def validate_nuda_per_mille(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not (0 <= value <= MAX_NUDA_PER_MILLE)
    ):
        raise ValueError(
            f"nuda_per_mille must be an integer in [0, {MAX_NUDA_PER_MILLE}], got {value!r}"
        )
    return value


def is_nuda_sampled(act_id: str, *, run_id: str, nuda_per_mille: int) -> bool:
    """Whether this act draws a Lectio nuda under the sealed sampling design.

    A deterministic hash threshold: the same (act, run) always votes the same
    way, so the sample is fixed the moment the run's configuration is sealed --
    "fixed before the run" is a property of this function rather than a promise
    about when it is called. `nuda_per_mille == 0` samples nothing, which is
    every existing scenario's unchanged behaviour.
    """
    validate_nuda_per_mille(nuda_per_mille)
    if nuda_per_mille == 0:
        return False
    digest = digest_of({"purpose": "nuda-sample", "run_id": run_id, "act_id": act_id})
    threshold = int(digest[:8], 16) % MAX_NUDA_PER_MILLE
    return threshold < nuda_per_mille


def sampling_design(*, nuda_per_mille: int, approval_ref: str) -> dict[str, object]:
    """The design record every Lectio nuda carries.

    GOVERNANCE 10: "cost is recorded but never silently narrows the
    instrument". A sample of unknown design measures nothing, so the rate, the
    rule and the approval travel on every record drawn under them.
    """
    validate_nuda_per_mille(nuda_per_mille)
    if not isinstance(approval_ref, str) or not approval_ref.strip():
        raise ValueError("a Lectio nuda was drawn with no predeclared approval reference")
    return {
        "nuda_per_mille": nuda_per_mille,
        "selection_rule": SELECTION_RULE,
        "approval_ref": approval_ref,
    }
