"""A person's correction of a held reading, as the stages after the Recensor carry it.

A current `edit` decision (`common.contracts.approval`) the Recensor applied
(`common.page_review.operator_correction`) makes the person's text the
reading. Nothing changes the model's output: the model's reading stays in the
run tree as read, and the correction is a separate, labelled human layer.

- The Archetypus establishes the edited text, with the fixed no-doubt
  uncertainty layer (`common.contracts.uncertainty.corrected_layer`) and the
  provenance `correction_provenance` builds: labelled "corrected by a person",
  naming who decided, when, why and on which stored approval, the person's
  optional note, and the model's reading it corrects (its Perlectio reference,
  text digest, text status and serving provenance).
- The Armarium builds the same provenance again from the stored decisions and
  the model's reading, refuses a record that does not carry it exactly, and
  exports the edited text as the reading with the model's reading beside it,
  labelled "model reading (original)".

`stored_edits` resolves each applied edit to the approval it was stored as, so
every stage reads the person's text and note from the record itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.approval import (
    EDIT_DECISION,
    UNIT_SCOPE,
    ApprovalRecordReference,
    review_decision_digests,
)
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import FatalAccounting
from common.review_decisions import CORRECTION_FIELD, correction_digest

CORRECTED_LABEL: Final = "corrected by a person"
ORIGINAL_LABEL: Final = "model reading (original)"
PROVENANCE_FIELDS: Final = frozenset({"label", "note", "decisions", "model_reading"})
# Enough of each edit to rebuild its approval record from the delivered text and
# the note (`rebuilt_edit`), so a clean machine can check the decision is the one
# that names that text.
DECISION_FIELDS: Final = frozenset(
    {
        "decision_hash",
        "approver",
        "timestamp",
        "reason",
        "approval_ref",
        "run_id",
        "page_id",
        "basis_digest",
    }
)
MODEL_READING_FIELDS: Final = frozenset(
    {"label", "perlectio_ref", "text_sha256", "text_status", "provenance"}
)


def text_sha256(text: str) -> str:
    """The digest a correction names a text by: its UTF-8 bytes."""
    return digest_bytes(text.encode("utf-8"))


def stored_edits(
    summaries: Sequence[Mapping[str, Any]],
    approvals: Mapping[str, tuple[ApprovalRecordReference, dict[str, Any]]],
    what: str,
) -> list[tuple[ApprovalRecordReference, dict[str, Any]]]:
    """Each applied edit summary's stored approval, refused unless it records that edit.

    `approvals` is the run's stored decisions by digest
    (`RunTree.review_decision_records`). Each must be in the run and be the
    edit the summary names: same hash, subject and text and note.
    """
    found = []
    for summary in summaries:
        stored = approvals.get(summary["record_sha256"])
        if stored is None:
            raise FatalAccounting(
                f"{what} was corrected by decision {summary['decision_hash']}, whose stored "
                f"approval {summary['record_sha256']} is not in the run"
            )
        reference, record = stored
        review = record.get("review") if isinstance(record, dict) else None
        if (
            not isinstance(review, dict)
            or review.get("decision") != EDIT_DECISION
            or record.get("self_hash") != summary["decision_hash"]
            or record.get("subject_ids") != [summary["subject_id"]]
            or correction_digest(review) != summary.get(CORRECTION_FIELD)
        ):
            raise FatalAccounting(
                f"{what}'s review names an edit its stored approval {reference.relative_path} "
                "does not record"
            )
        found.append((reference, record))
    if not found:
        raise FatalAccounting(f"{what} names no edit to correct it")
    return found


def edited_text(stored: Sequence[tuple[ApprovalRecordReference, dict[str, Any]]]) -> str:
    """The person's text the stored edits name; they agree, or the Recensor held the unit."""
    texts = {record["review"]["text"] for _reference, record in stored}
    notes = {record["review"]["note"] for _reference, record in stored}
    if len(texts) != 1 or len(notes) != 1:
        raise FatalAccounting("the edits of one reading name different texts or notes")
    return next(iter(texts))


def correction_provenance(
    stored: Sequence[tuple[ApprovalRecordReference, dict[str, Any]]],
    *,
    perlectio_ref: Mapping[str, str],
    model_text: str,
    model_text_status: str,
    model_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """The provenance of a corrected reading: the person's decisions and the model's reading."""
    edited_text(stored)
    note = stored[0][1]["review"]["note"]
    return {
        "label": CORRECTED_LABEL,
        "note": note,
        "decisions": sorted(
            (
                {
                    "decision_hash": record["self_hash"],
                    "approver": record["approver"],
                    "timestamp": record["timestamp"],
                    "reason": record["reason"],
                    "approval_ref": reference.to_record(),
                    "run_id": record["review"]["run_id"],
                    "page_id": record["review"]["page_id"],
                    "basis_digest": record["target_version_hash"],
                }
                for reference, record in stored
            ),
            key=lambda decision: decision["decision_hash"],
        ),
        "model_reading": {
            "label": ORIGINAL_LABEL,
            "perlectio_ref": dict(perlectio_ref),
            "text_sha256": text_sha256(model_text),
            "text_status": model_text_status,
            "provenance": dict(model_provenance),
        },
    }


def is_correction(provenance: Any) -> bool:
    """Whether a reading's provenance is a person's correction rather than a model's."""
    return isinstance(provenance, Mapping) and provenance.get("label") == CORRECTED_LABEL


def model_provenance(provenance: Mapping[str, Any]) -> Any:
    """The serving provenance of the model reading behind a reading, corrected or not."""
    if is_correction(provenance):
        return provenance["model_reading"]["provenance"]
    return provenance


def record_digest(record: Mapping[str, Any]) -> str:
    """The digest a stored approval is named by in the run tree: its canonical bytes."""
    return digest_bytes(canonical_bytes(record))


def edit_digests(
    decision: Mapping[str, Any], *, act_id: str, text: str, note: str | None
) -> tuple[str, str]:
    """What the edit one provenance decision names must hash to, given the text it delivers.

    `(self_hash, stored digest)`, refused (`ApprovalRefusal`) when the fields
    cannot make a sound edit; the caller compares them with the decision's
    `decision_hash` and `approval_ref` digest.
    """
    return review_decision_digests(
        run_id=decision["run_id"],
        scope=UNIT_SCOPE,
        subject_id=act_id,
        page_id=decision["page_id"],
        decision=EDIT_DECISION,
        finding=None,
        basis_digest=decision["basis_digest"],
        reason=decision["reason"],
        timestamp=decision["timestamp"],
        text=text,
        note=note,
    )
