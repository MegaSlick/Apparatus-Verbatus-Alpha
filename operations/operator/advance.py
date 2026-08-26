"""The one decision the operator console may append: advance a sealed boundary.

This module is intentionally separate from the console renderer.  The renderer
never imports the approval builder or ``RunTree.write_approval_record``; only
this custody-side module does.  Keeping that import boundary executable makes a
compromised renderer able to misrepresent evidence, but unable to mint an
approval for an unrelated action.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.contracts.approval import ApprovalRecordReference, build_approval_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ApprovalRefusal, ContractError
from common.contracts.stages import STAGES
from common.runtree.store import RunTree
from common.stage import held_advance_boundaries, latest_attempt

from .custody import (
    python_module_command,
    run_confined,
)
from .errors import ErrorCode, OperatorError

ADVANCE_ACTION = "advance"
UTC = timezone.utc


class UnsealedBoundaryRefusal(ApprovalRefusal):
    """A known stage has no completion seal yet, rather than unreadable evidence."""


def advance_subject(stage: str) -> str:
    """Return the closed subject name for one stage completion boundary."""

    if stage not in STAGES:
        raise ApprovalRefusal(f"advance names unknown stage {stage!r}; no boundary was advanced")
    return f"stage-boundary:{stage}"


def sealed_boundary(tree: RunTree, stage: str) -> tuple[dict[str, Any], str]:
    """Read the current stored seal and its bytes' digest, refusing no seal.

    The digest is taken from the immutable artifact bytes, not reconstructed
    from its payload.  A later re-seal therefore changes the value an advance
    record binds even if the new payload happens to look similar on screen.
    """

    advance_subject(stage)
    try:
        manifest = tree.build_manifest(stage, verify_inputs=False)
        seals = [
            tree.read_artifact(stage, "stage-seal", entry["artifact_id"])
            for entry in manifest["artifacts"]
            if entry["kind"] == "stage-seal"
        ]
    except (ContractError, KeyError, TypeError) as error:
        raise ApprovalRefusal(
            f"advance could not read {stage}'s stored completion seal; no boundary was advanced"
        ) from error
    if not seals:
        raise UnsealedBoundaryRefusal(
            f"advance refuses {stage}: it has no stored stage-seal, so there is no witnessed boundary to pass"
        )
    seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
    data = tree.read_bytes(tree.artifact_path(stage, "stage-seal", seal["artifact_id"]))
    return seal, digest_bytes(data)


def boundary_summary(tree: RunTree, stage: str) -> dict[str, Any]:
    """Project sealed boundary facts for display, never a recommendation."""

    seal, digest = sealed_boundary(tree, stage)
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


def held_boundaries_for_mode(
    mode: str,
    *,
    stage: str,
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> frozenset[str]:
    """Refuse unless this declared selection can require an advance at ``stage``.

    The held set is the driver's, not this module's opinion of it: an auto run
    can still stop at the Attestatores' witnessed boundary, and so can a semi
    range that spans it (`common.stage.ALWAYS_HELD_BOUNDARIES`). This function
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


def record_advance(
    tree: RunTree,
    stage: str,
    *,
    reason: str,
    timestamp: str | None = None,
    expected_digest: str | None = None,
) -> ApprovalRecordReference:
    """Append the one allowed decision record after proving its boundary exists.

    **The read-then-write window is closed by detection, not by locking, and
    that is the decision rather than an omission.** A seal re-written between
    `sealed_boundary` above and the write below leaves a record binding a
    digest that is already stale. No number of re-reads closes that: there is
    no cross-process lock over a run tree, and GOVERNANCE 4 forbids retracting
    the record once it is written. What the binding buys instead is that the
    staleness is permanent and visible: `trigger_advance` checks again after
    the append and refuses to report an already-stale record as success,
    `verify_advance` refuses such a record, and `review._still_binds` names it
    stale on the one surface a person reads, every time they read it.

    ``expected_digest`` is required, not merely checked when given: this is
    the one function that can append an advance record, so it is the one
    place that can silently drop the confirmation an operator was shown. An
    ``expected_digest`` of ``None`` used to mean "compare against whatever is
    on disk right now", which is not a check at all — it always matches
    itself. A caller with no observed digest to bind has not shown anyone a
    boundary to confirm, and must be refused before it can write.
    """

    _seal, seal_digest = sealed_boundary(tree, stage)
    if expected_digest is None:
        raise ApprovalRefusal(
            "advance refuses to record without the exact seal digest that was shown for "
            "confirmation; a caller may not bind to whatever happens to be on disk right now"
        )
    if seal_digest != expected_digest:
        raise ApprovalRefusal(
            "advance refuses because the stage seal changed after the operator reviewed it; "
            "no boundary was advanced"
        )
    record = build_approval_record(
        [advance_subject(stage)],
        ADVANCE_ACTION,
        reason,
        seal_digest,
        timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    reference, _result = tree.write_approval_record(record)
    return reference


def verify_advance(tree: RunTree, stage: str, reference: ApprovalRecordReference) -> dict[str, Any]:
    """Read an advance only when it still names this exact sealed boundary."""

    record = tree.read_approval_record(reference)
    _seal, current_digest = sealed_boundary(tree, stage)
    if record["action"] != ADVANCE_ACTION or record["subject_ids"] != [advance_subject(stage)]:
        raise ApprovalRefusal("advance record does not name this stage boundary")
    if record["target_version_hash"] != current_digest:
        raise ApprovalRefusal(
            "advance record binds a different stage-seal digest; the boundary changed after it was advanced"
        )
    return record


def trigger_advance(
    run_root: str | Path,
    run_id: str,
    stage: str,
    *,
    reason: str,
    workspace: str | Path,
    expected_digest: str | None = None,
) -> ApprovalRecordReference:
    """Run the narrow decision worker outside the renderer process.

    It has no provider credential and its confinement admits mutations only
    below this run's receipt directory.  The worker cannot publish an artifact,
    revise an existing record, or reach a paid adapter; it executes precisely
    the durable advance-record operation.

    The CLI reaches this only after a typed confirmation naming the exact seal
    digest observed before launch. The worker rechecks that digest, so a seal
    changed between display and execution is refused rather than silently
    advancing a boundary the operator did not review. The parent checks the
    returned record against the current seal once more, so a change during the
    worker's append window is reported before this call can return success.

    ``expected_digest`` is required for the same reason it is required in
    `record_advance`: an omitted value used to fall back to whatever digest
    this call happened to read from disk, which trivially matches itself and
    checks nothing. A caller with no digest to bind is a caller that showed no
    one a boundary to confirm.
    """

    # Absolute before it is split between the two processes. The parent builds
    # the Landlock/Seatbelt allowance from *its* resolution of the run root
    # while the child resolves the same string against `workspace`, so a
    # relative path sends the permitted directory and the tree the worker
    # opens to two different places — the boundary would then guard a tree
    # nobody wrote to. (Same defect class the rebuild plan names for the
    # orchestrator's own `--run-root`.)
    root = Path(run_root).resolve()
    tree = RunTree(root, run_id)
    try:
        _seal, current_digest = sealed_boundary(tree, stage)
    except ApprovalRefusal as error:
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=str(error)) from error
    if expected_digest is None:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "no observed seal digest was given to bind; a caller may not trigger an "
                "advance without the exact digest it showed for confirmation"
            ),
        )
    checked_digest = expected_digest
    if checked_digest != current_digest:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "the stage seal changed after it was shown for confirmation; no advance "
                "record was written"
            ),
        )
    receipt_dir = tree.resolve("receipts/sha256")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    # The run identity travels in argv and the decision travels on stdin, and
    # the split is the authority boundary, not a style choice: argv is written
    # by this trusted parent and names *which tree* may be written, while stdin
    # is the channel a requester (eventually the renderer) may fill and names
    # only *which sealed boundary* inside that tree. A request that could also
    # name the tree could redirect the one permitted write at a run nobody
    # granted. `validate_run_id` has already refused anything but
    # `[a-z0-9._-]`, and `root` is absolute, so neither value can be read by
    # the child's parser as an option.
    request = json.dumps({"stage": stage, "reason": reason, "expected_digest": checked_digest})
    command = python_module_command(
        "operations.operator.advance_worker",
        Path(workspace),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
    )
    backend, completed = run_confined(
        command,
        writable=receipt_dir,
        cwd=Path(workspace),
        input_text=request,
    )
    if completed.returncode != 0:
        launcher = backend.launcher_failure(completed)
        if launcher is not None:
            # The worker never executed: no boundary was established for it to
            # write inside. This is a platform-enforcement refusal, not a
            # verdict on the advance request the worker never saw.
            raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=launcher)
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=completed.stdout or completed.stderr)
    try:
        decoded = json.loads(completed.stdout)
        reference = ApprovalRecordReference(decoded["relative_path"], decoded["sha256"])
        record = tree.read_approval_record(reference)
        if (
            record["action"] != ADVANCE_ACTION
            or record["subject_ids"] != [advance_subject(stage)]
            or record["target_version_hash"] != checked_digest
            or record["reason"] != reason
        ):
            raise ApprovalRefusal("the advance worker returned a different decision record")
        try:
            verify_advance(tree, stage, reference)
        except ApprovalRefusal as error:
            raise OperatorError(
                ErrorCode.ADVANCE_REFUSED,
                detail=(
                    f"the advance worker wrote checked decision record {reference.relative_path}, "
                    "but the stage seal changed before the result was verified; the immutable "
                    "record remains visible as stale, so inspect advance_records before retrying"
                ),
            ) from error
        return reference
    except (ApprovalRefusal, ContractError, KeyError, OSError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "the advance worker returned no checked decision reference; it may have "
                "written an advance record, so inspect the review surface before retrying"
            ),
        ) from error
