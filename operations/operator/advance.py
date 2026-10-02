"""Advance a sealed boundary: the one approval besides a review decision the operator writes.

`trigger_advance` verifies the stage's stored seal against the run tree, checks
it is the one the operator confirmed, and appends an `approval-record.v1`
binding that seal's digest (`record_advance`), then reads the record back and
verifies it still names this boundary (`verify_advance`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.contracts.approval import (
    ADVANCE_ACTION,
    ADVANCE_SUBJECT_PREFIX,
    ApprovalRecordReference,
    build_approval_record,
)
from common.contracts.errors import ApprovalRefusal, ContractError
from common.contracts.stages import ARMARIUM, STAGES, seal_readers
from common.runtree.store import RunTree
from common.stage import (
    current_stage_seal,
    held_advance_boundaries,
    verify_final_seal,
    verify_stage_seal,
)

from .errors import ErrorCode, OperatorError

MAX_ADVANCE_REASON_CHARACTERS = 4_000
UTC = timezone.utc


class UnsealedBoundaryRefusal(ApprovalRefusal):
    """A known stage has no completion seal yet, rather than unreadable evidence."""


def advance_subject(stage: str) -> str:
    """Subjects are derived only from the closed stage list, never caller text."""

    if stage not in STAGES:
        raise ApprovalRefusal(f"advance names unknown stage {stage!r}; no boundary was advanced")
    return f"{ADVANCE_SUBJECT_PREFIX}{stage}"


def validate_advance_reason(reason: Any) -> str:
    """Bound the only requester-controlled text that reaches an approval record."""

    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalRefusal("advance reason is blank or not a string")
    if len(reason) > MAX_ADVANCE_REASON_CHARACTERS:
        raise ApprovalRefusal(f"advance reason exceeds {MAX_ADVANCE_REASON_CHARACTERS} characters")
    return reason


def verify_sealed_boundary(tree: RunTree, stage: str) -> None:
    """Verify that one stored seal still witnesses the evidence now on disk."""

    # Unknown stages must refuse before a caller can observe any run-tree state.
    advance_subject(stage)
    if stage == ARMARIUM:
        verify_final_seal(tree)
        return
    # Every reader verifies one seal the same way, so any of them proves it.
    readers = seal_readers(stage)
    if not readers:  # the closed stage graph must give every non-final seal a reader
        raise ApprovalRefusal(f"stage {stage!r} has no seal verifier; no boundary was advanced")
    verify_stage_seal(tree, stage, readers[0])


def stored_boundary(tree: RunTree, stage: str) -> tuple[dict[str, Any], str]:
    """Read the current stored seal and digest without claiming it is still valid.

    The digest is taken from the immutable artifact bytes, not reconstructed
    from its payload.  A later re-seal therefore changes the value an advance
    record binds even if the new payload happens to look similar on screen.
    """

    advance_subject(stage)
    try:
        current = current_stage_seal(tree, stage)
    except (ContractError, KeyError, OSError, TypeError) as error:
        raise ApprovalRefusal(
            f"advance could not read {stage}'s stored completion seal ({error}); "
            "no boundary was advanced"
        ) from error
    if current is None:
        raise UnsealedBoundaryRefusal(
            f"advance refuses {stage}: it has no stored stage-seal, so there is no "
            "witnessed boundary to pass"
        )
    return current


def boundary_summary(tree: RunTree, stage: str) -> dict[str, Any]:
    """Project stored boundary facts for display, never a recommendation.

    Reads via `stored_boundary`, not the verifying `sealed_boundary`: a chain
    display row states what is on disk, and verifying every stage to draw a
    table would make display cost scale with the whole run. The one digest an
    operator confirms an advance against is sourced from the verifying path
    at the advance itself (`trigger_advance` via `sealed_boundary`).
    """

    seal, digest = stored_boundary(tree, stage)
    # Inside the guard: `stored_boundary` proves only `attempt_ordinal`, and a
    # payload missing its census or a digest would otherwise raise a bare
    # `KeyError` that reaches the unclassified handler instead of a named
    # `ApprovalRefusal`.
    try:
        payload = seal["payload"]
        return {
            "stage": stage,
            "seal_digest": digest,
            "attempt_ordinal": payload["attempt_ordinal"],
            "config_digest": payload["config_digest"],
            "artifact_inventory": payload["artifact_inventory"],
            "blob_inventory": payload["blob_inventory"],
            "census": payload["census"],
        }
    except (KeyError, TypeError) as error:
        raise ApprovalRefusal(
            f"advance could not read {stage}'s stored completion seal ({error}); "
            "no boundary was advanced"
        ) from error


def held_boundaries_for_mode(
    mode: str,
    *,
    stage: str,
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> frozenset[str]:
    """Refuse unless this declared selection can require an advance at ``stage``.

    The held set is the driver's, not this module's opinion of it: an auto run
    can still stop at the Attestatores' witnessed boundary or at a Recensor that
    holds anything, and so can a semi range that spans either
    (`common.stage.ALWAYS_HELD_BOUNDARIES`). An advance at the Recensor is what
    lets a later run pass its current seal while units stay held. This function
    validates invocation shape; the run tree carries no invocation mode or
    exit-status evidence from which it could claim that one particular run did
    stop there.
    """

    try:
        held = held_advance_boundaries(mode, stage=stage, from_stage=from_stage, to_stage=to_stage)
    except ContractError as error:
        raise ApprovalRefusal(f"advance refuses this mode selection: {error}") from error
    if stage not in held:
        waits = ", ".join(sorted(held))
        if mode == "auto":
            raise ApprovalRefusal(
                f"advance refuses {stage} in auto mode: auto passes every selected boundary "
                f"without a person-held record except {waits}, where a run can stop in every mode"
            )
        raise ApprovalRefusal(
            f"advance refuses {stage} in {mode} mode: that declared selection can require "
            f"a person-held advance at {waits}, not {stage}"
        )
    return held


def sealed_boundary(tree: RunTree, stage: str) -> tuple[dict[str, Any], str]:
    """Read a stored seal only when it still witnesses the evidence on disk."""

    seal, digest = stored_boundary(tree, stage)
    try:
        verify_sealed_boundary(tree, stage)
    except ContractError as error:
        raise ApprovalRefusal(
            f"advance refuses {stage}: its stored completion seal no longer verifies against "
            f"the run tree ({error}); no boundary was advanced"
        ) from error
    return seal, digest


def record_advance(
    tree: RunTree,
    stage: str,
    *,
    reason: str,
    expected_digest: str,
    timestamp: str | None = None,
) -> ApprovalRecordReference:
    """Append the advance record binding the stored seal the operator reviewed.

    Verifying the seal against the run tree is `trigger_advance`'s, before
    this is called; here, the one place the digest is compared, the stored
    seal's digest must still be the one the operator confirmed. A caller
    handing over no digest would bind the advance to whatever seal is current,
    the substitution the typed confirmation exists to prevent. A seal rewritten between that read and the write
    leaves a record binding a digest that is already stale: `verify_advance`
    refuses such a record, and `review` names it stale every time it is read.
    """

    reason = validate_advance_reason(reason)
    _seal, seal_digest = stored_boundary(tree, stage)
    if not isinstance(expected_digest, str) or not expected_digest:
        raise ApprovalRefusal(
            "advance refuses because no reviewed stage-seal digest was supplied; "
            "no boundary was advanced"
        )
    if seal_digest != expected_digest:
        raise ApprovalRefusal(
            "advance refuses because the stage seal changed after it was shown for "
            "confirmation; no boundary was advanced"
        )
    record = build_approval_record(
        [advance_subject(stage)],
        ADVANCE_ACTION,
        reason,
        seal_digest,
        timestamp
        if timestamp is not None
        else datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    reference, _ = tree.write_approval_record(record)
    return reference


def verify_advance(tree: RunTree, stage: str, reference: ApprovalRecordReference) -> dict[str, Any]:
    """Read an advance only when it still names this exact sealed boundary."""

    record = tree.read_approval_record(reference)
    _seal, current_digest = stored_boundary(tree, stage)
    if record["action"] != ADVANCE_ACTION or record["subject_ids"] != [advance_subject(stage)]:
        raise ApprovalRefusal("advance record does not name this stage boundary")
    if record["target_version_hash"] != current_digest:
        raise ApprovalRefusal(
            "advance record binds a different stage-seal digest; the boundary changed after it was advanced"
        )
    try:
        verify_sealed_boundary(tree, stage)
    except ContractError as error:
        raise ApprovalRefusal(
            f"advance record names {stage}, but that stage-seal no longer verifies against "
            f"the run tree ({error})"
        ) from error
    return record


def trigger_advance(
    run_root: str | Path,
    run_id: str,
    stage: str,
    *,
    reason: str,
    expected_digest: str,
) -> ApprovalRecordReference:
    """Verify the boundary, record the advance, and verify the record names it.

    The CLI reaches this only after a typed confirmation naming the exact seal
    digest observed before it. ``expected_digest`` is required, with no
    default: a caller that bound an advance to whatever boundary happens to be
    current is the exact substitution the typed confirmation exists to
    prevent, so a seal changed between display and execution is refused.
    """

    tree = RunTree(Path(run_root).resolve(), run_id)
    try:
        sealed_boundary(tree, stage)
        reference = record_advance(tree, stage, reason=reason, expected_digest=expected_digest)
    except (ContractError, OSError) as error:
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=str(error)) from error
    try:
        verify_advance(tree, stage, reference)
    except (ContractError, OSError) as error:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                f"advance record {reference.relative_path} was written, but the stage seal "
                f"changed before it was verified ({error}); the record stays visible as "
                "stale, so read the run's review before retrying"
            ),
        ) from error
    return reference
