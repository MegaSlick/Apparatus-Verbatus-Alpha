"""Lectio nuda: the unprimed instrument reading, sampled by a predeclared design.

An instrument record with no path to establishing text, drawn by a
deterministic hash threshold rather than `random`, so two runs of the
identical command sample the identical acts. `nuda_per_mille` holds the
sampling rate as a non-negative integer in [0, 1000] because
`common/contracts/canonical.py` refuses floats anywhere a value is sealed.
"""

from __future__ import annotations

from typing import Final

from common.contracts.approval import ApprovalRecordBinding
from common.contracts.canonical import digest_of
from common.stage import MAX_NUDA_PER_MILLE, NUDA_APPROVAL_SUBJECT

LECTIO_NUDA_KIND: Final = "lectio-nuda"

# Named so the record says which design produced the sample; the rate is
# sealed separately, in `config_digest`.
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
    way, fixed the moment the run's configuration is sealed. `nuda_per_mille
    == 0` samples nothing.
    """
    validate_nuda_per_mille(nuda_per_mille)
    if nuda_per_mille == 0:
        return False
    digest = digest_of({"purpose": "nuda-sample", "run_id": run_id, "act_id": act_id})
    threshold = int(digest[:8], 16) % MAX_NUDA_PER_MILLE
    return threshold < nuda_per_mille


def sampling_design(
    *, nuda_per_mille: int, approval_ref: ApprovalRecordBinding
) -> dict[str, object]:
    """The design record every Lectio nuda carries: rate, rule and approval,
    since a sample of unknown design measures nothing."""
    validate_nuda_per_mille(nuda_per_mille)
    if not isinstance(approval_ref, ApprovalRecordBinding):
        raise ValueError(
            "a Lectio nuda was drawn with an untyped approval reference; "
            "an arbitrary string is not an approval record"
        )
    if approval_ref.subject != NUDA_APPROVAL_SUBJECT:
        raise ValueError(
            f"a Lectio nuda executes design {NUDA_APPROVAL_SUBJECT!r}, but its approval "
            f"record names {approval_ref.subject!r}"
        )
    return {
        "nuda_per_mille": nuda_per_mille,
        "selection_rule": SELECTION_RULE,
        "approval_ref": approval_ref.reference.to_record(),
    }
