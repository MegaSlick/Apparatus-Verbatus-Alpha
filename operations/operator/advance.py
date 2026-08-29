"""The one decision the operator console may append: advance a sealed boundary.

This module is intentionally separate from the console renderer.  The renderer
never imports the approval builder or ``RunTree.write_approval_record``; only
this custody-side module does.  Keeping that import boundary executable makes a
compromised renderer able to misrepresent evidence, but unable to mint an
approval for an unrelated action.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.contracts.approval import ApprovalRecordReference, build_approval_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ApprovalRefusal, ContractError
from common.contracts.stages import ARMARIUM, SEAL_PREDECESSORS, STAGES
from common.runtree.store import RunTree
from common.stage import latest_attempt, verify_final_seal, verify_predecessor_seal

from .custody import (
    python_module_command,
    run_confined,
)
from .errors import ErrorCode, OperatorError, sanitize_detail

ADVANCE_ACTION = "advance"
ADVANCE_SUBJECT_PREFIX = "stage-boundary:"
MAX_ADVANCE_REASON_CHARACTERS = 4_000
MAX_ADVANCE_REQUEST_CHARACTERS = 65_536
UTC = timezone.utc

# The worker exits with this when the advance record is on disk but its report
# could not be written back to the parent. It is neither success nor a refused
# advance, and the two must not be told to the operator as the same thing.
WORKER_REPORT_FAILED_EXIT = 3

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def advance_subject(stage: str) -> str:
    """Subjects are derived only from the closed stage list, never caller text."""

    if stage not in STAGES:
        raise ApprovalRefusal(f"advance names unknown stage {stage!r}; no boundary was advanced")
    return f"{ADVANCE_SUBJECT_PREFIX}{stage}"


def directory_identity(path: Path, label: str) -> tuple[int, int]:
    """Name one real directory by device and inode without following its leaf."""

    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ApprovalRefusal(f"{label} could not be inspected ({error})") from error
    if not stat.S_ISDIR(observed.st_mode):
        raise ApprovalRefusal(f"{label} is not a real directory; symlinks are refused")
    return observed.st_dev, observed.st_ino


def require_directory_identity(path: Path, expected: tuple[int, int], label: str) -> None:
    """Refuse when a checked directory was replaced before or during use."""

    if directory_identity(path, label) != expected:
        raise ApprovalRefusal(
            f"{label} changed device or inode after it was reviewed; "
            "an advance record may exist, so inspect review before retrying"
        )


def receipt_directory_identity(
    run_root: Path, run_identity: tuple[int, int], *, create: bool
) -> tuple[int, int]:
    """Open the receipt path beneath a pinned root, never through a symlink.

    Directory descriptors make creation relative to the run object the parent
    already inspected, rather than relative to a pathname an attacker can swap.
    The case-fold check applies the default-APFS rule on every filesystem so a
    tree prepared on Linux cannot acquire both ``receipts`` and ``Receipts``
    and become ambiguous only after it moves to a Mac.
    """

    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise ApprovalRefusal(
            "this platform cannot open the advance receipt directory with no-follow semantics"
        )
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(run_root, _DIRECTORY_OPEN_FLAGS)
        descriptors.append(root_descriptor)
        if _descriptor_identity(root_descriptor) != run_identity:
            raise ApprovalRefusal(
                "the reviewed run tree changed device or inode before its receipt path was opened"
            )
        parent_descriptor = _open_child_directory(
            root_descriptor, "receipts", create=create, label="run-tree receipts directory"
        )
        descriptors.append(parent_descriptor)
        receipt_descriptor = _open_child_directory(
            parent_descriptor,
            "sha256",
            create=create,
            label="advance receipt directory",
        )
        descriptors.append(receipt_descriptor)
        return _descriptor_identity(receipt_descriptor)
    except OSError as error:
        raise ApprovalRefusal(
            f"the advance receipt directory could not be opened ({error})"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_child_directory(parent: int, name: str, *, create: bool, label: str) -> int:
    _refuse_case_variant(parent, name, label)
    if create:
        try:
            os.mkdir(name, dir_fd=parent)
        except FileExistsError:
            pass
        _refuse_case_variant(parent, name, label)
    try:
        return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise ApprovalRefusal(f"the {label} does not exist") from None


def _refuse_case_variant(parent: int, name: str, label: str) -> None:
    variants = sorted(entry for entry in os.listdir(parent) if entry.casefold() == name.casefold())
    if any(entry != name for entry in variants):
        raise ApprovalRefusal(
            f"the {label} collides by case with {variants}; the path is ambiguous on default APFS"
        )


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    observed = os.fstat(descriptor)
    return observed.st_dev, observed.st_ino


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
    consumers = [consumer for consumer, producer in SEAL_PREDECESSORS.items() if producer == stage]
    if len(consumers) != 1:  # the closed stage graph must give every non-final seal one reader
        raise ApprovalRefusal(
            f"stage {stage!r} has no unique seal verifier; no boundary was advanced"
        )
    verify_predecessor_seal(tree, consumers[0])


def stored_boundary(tree: RunTree, stage: str) -> tuple[dict[str, Any], str]:
    """Read the current stored seal and digest without claiming it is still valid.

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
            f"advance could not read {stage}'s stored completion seal ({error}); "
            "no boundary was advanced"
        ) from error
    if not seals:
        raise ApprovalRefusal(
            f"advance refuses {stage}: it has no stored stage-seal, so there is no witnessed boundary to pass"
        )
    seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
    try:
        data = tree.read_bytes(tree.artifact_path(stage, "stage-seal", seal["artifact_id"]))
    except (KeyError, OSError, TypeError) as error:
        raise ApprovalRefusal(
            f"advance could not read {stage}'s stored completion seal bytes ({error}); "
            "no boundary was advanced"
        ) from error
    return seal, digest_bytes(data)


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
    timestamp: str | None = None,
    expected_digest: str | None = None,
) -> ApprovalRecordReference:
    """Append the one allowed decision record after proving its boundary exists.

    **The boundary's verification belongs to the launching parent, and this
    half of the worker boundary deliberately does not repeat it.**
    `trigger_advance` calls `sealed_boundary` — `stored_boundary` *plus*
    `verify_sealed_boundary` — unconfined, read-only, and before the receipt
    directory is created, so a boundary whose seal no longer verifies still
    refuses before any advance record exists. This function reads the same
    stored seal and binds the digest the parent verified.

    It does not re-verify, because inside the confined worker it cannot.
    `verify_predecessor_seal` and `verify_final_seal` end in
    `common.stage._decode_environment`, whose `import pypdfium2` pulls in
    `ctypes` — the one import this repository has already measured aborting a
    confined child under the macOS Seatbelt profile, recorded at
    `custody._launcher_environment_hidden`, which imports `ctypes` inside its
    Linux-only branch for exactly that reason. Widening the profile to admit
    it is refused on principle: the boundary does not widen to accommodate a
    checker.

    **Nothing that can refuse was lost by the move.** Every refusal
    `_verify_stage_seal` raises is reached by reading files, which the profile
    already permits; the single step that needs the decoders is explicitly an
    observation Unit 17 owns and never a refusal. In the worker that
    observation was written to a stderr pipe `trigger_advance` discards on
    success, so moving it to the parent is where it reaches a person at all
    (GOVERNANCE 2). Splitting a diagnostic-free variant out of
    `_verify_stage_seal` was the alternative and is declined: that function
    exists to be the single definition of what a seal means, and a second,
    weaker entry point beside it is the shape its own docstring warns against.

    **The read-then-write window is closed by detection, not by locking, and
    that is the decision rather than an omission.** A seal re-written between
    the `stored_boundary` read above and the write below — or between the
    parent's verification and this process starting at all — leaves a record
    binding a digest that is already stale. No number of re-reads closes that:
    there is no cross-process lock over a run tree, and GOVERNANCE 4 forbids
    retracting the record once it is written, so a post-write check could only
    report what the binding already reports. What the binding buys instead is that
    the staleness is permanent and visible — `verify_advance` refuses such a
    record, and `review._still_binds` names it stale on the one surface a
    person reads, every time they read it.
    """

    reason = validate_advance_reason(reason)
    _seal, seal_digest = stored_boundary(tree, stage)
    if expected_digest is None:
        raise ApprovalRefusal(
            "advance refuses because no reviewed stage-seal digest was supplied; "
            "no boundary was advanced"
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
        timestamp
        if timestamp is not None
        else datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    reference, _result = tree.write_approval_record(record)
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
    workspace: str | Path,
    expected_digest: str,
) -> ApprovalRecordReference:
    """Run the narrow decision worker outside the renderer process.

    It has no provider credential and its confinement admits mutations only
    below this run's receipt directory.  The worker cannot publish an artifact,
    revise an existing record, or reach a paid adapter; it executes precisely
    the durable advance-record operation.

    The CLI reaches this only after a typed confirmation naming the exact seal
    digest observed before launch. The worker rechecks that digest, so a seal
    changed between display and execution is refused rather than silently
    advancing a boundary the operator did not review.

    ``expected_digest`` is required, with no default, because the alternative
    is a caller that binds an advance to whatever boundary happens to be
    current — the exact substitution the typed confirmation exists to prevent.
    A default would make that the quiet path and the reviewed digest the
    opt-in. `record_advance` refuses a missing digest too: the same fact
    enforced on both sides of the worker boundary.

    The two sides are not symmetric about *verification*, and that asymmetry
    is deliberate rather than an omission: seal verification re-derives the
    local decode environment, which the Seatbelt profile denies inside the
    worker, so it is performed here — before the worker is launched and before
    its writable directory exists. `record_advance` carries the full reasoning.
    """

    # Absolute before it is split between the two processes. The parent builds
    # the Landlock/Seatbelt allowance from *its* resolution of the run root
    # while the child resolves the same string against `workspace`, so a
    # relative path sends the permitted directory and the tree the worker
    # opens to two different places — the boundary would then guard a tree
    # nobody wrote to.
    root = Path(run_root).resolve()
    tree = RunTree(root, run_id)
    # **This is the boundary's verification, and it happens here because the
    # confined worker cannot perform it.** `sealed_boundary` refuses a seal
    # that no longer witnesses the evidence on disk, and it runs unconfined,
    # read-only, and above the `receipt_dir.mkdir` below — so the review's
    # property holds exactly as stated: a boundary whose seal no longer
    # verifies refuses before any advance record exists, and before the one
    # path the worker is permitted to write into is even created. The worker's
    # own check is the digest equality it binds; `record_advance` says why.
    try:
        run_identity = directory_identity(tree.root, "the reviewed run tree")
        _seal, current_digest = sealed_boundary(tree, stage)
    except (ApprovalRefusal, OSError) as error:
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=str(error)) from error
    # Checked after the seal is read, so an unsealed boundary is still refused
    # as unsealed rather than reported as a malformed request.
    if not isinstance(expected_digest, str) or not expected_digest:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "no reviewed stage-seal digest was supplied; a caller may not bind an "
                "advance to whatever boundary happens to be current"
            ),
        )
    if expected_digest != current_digest:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "the stage seal changed after it was shown for confirmation; no advance "
                "record was written"
            ),
        )
    try:
        reason = validate_advance_reason(reason)
        require_directory_identity(tree.root, run_identity, "the reviewed run tree")
        receipt_dir = tree.root / "receipts" / "sha256"
        receipt_identity = receipt_directory_identity(tree.root, run_identity, create=True)
        require_directory_identity(tree.root, run_identity, "the reviewed run tree")
    except (ApprovalRefusal, OSError) as error:
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=str(error)) from error
    # The run identity travels in argv and the decision travels on stdin, and
    # the split is the authority boundary, not a style choice: argv is written
    # by this trusted parent and names *which tree* may be written, while stdin
    # is the channel a requester (eventually the renderer) may fill and names
    # only *which sealed boundary* inside that tree. A request that could also
    # name the tree could redirect the one permitted write at a run nobody
    # granted. `validate_run_id` has already refused anything but
    # `[a-z0-9._-]`, and `root` is absolute, so neither value can be read by
    # the child's parser as an option.
    request = json.dumps({"stage": stage, "reason": reason, "expected_digest": expected_digest})
    if len(request) > MAX_ADVANCE_REQUEST_CHARACTERS:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail="the bounded advance request could not be represented safely",
        )
    command = python_module_command(
        "operations.operator.advance_worker",
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--run-device",
        str(run_identity[0]),
        "--run-inode",
        str(run_identity[1]),
        "--receipt-device",
        str(receipt_identity[0]),
        "--receipt-inode",
        str(receipt_identity[1]),
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
        if completed.returncode == WORKER_REPORT_FAILED_EXIT:
            raise OperatorError(
                ErrorCode.ADVANCE_REFUSED,
                detail=(
                    "the advance record was written and the worker could not report it, so no "
                    "reference could be checked; read the advance records in review rather "
                    "than retrying: " + (completed.stderr.strip() or "no diagnostic")
                ),
            )
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=completed.stdout or completed.stderr)
    try:
        require_directory_identity(tree.root, run_identity, "the reviewed run tree")
        if receipt_directory_identity(tree.root, run_identity, create=False) != receipt_identity:
            raise ApprovalRefusal(
                "the advance receipt directory changed device or inode after worker use"
            )
        decoded = json.loads(completed.stdout)
        reference = ApprovalRecordReference(decoded["relative_path"], decoded["sha256"])
        record = tree.read_approval_record(reference)
        if (
            record["action"] != ADVANCE_ACTION
            or record["subject_ids"] != [advance_subject(stage)]
            or record["target_version_hash"] != expected_digest
            or record["reason"] != reason
        ):
            raise ApprovalRefusal("the advance worker returned a different decision record")
    except (ApprovalRefusal, ContractError, KeyError, OSError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "the advance worker returned no checked decision reference; it may have "
                "written an advance record, so inspect the review surface before retrying: "
                f"{error}" + _worker_stderr_clause(completed)
            ),
        ) from error
    # Read after the reference is checked, deliberately. Deciding on stderr
    # first made any byte on that pipe a refusal, and the worker runs under
    # `runpy.run_module(run_name='__main__')`, where an ordinary
    # `DeprecationWarning` prints by default -- so a completed, verifiable
    # advance was reported as refused with no fault anywhere. What the worker
    # wrote is still not discarded (GOVERNANCE 2): the record verified against
    # the exact request, so this is a note beside a real advance rather than a
    # verdict on it, and it is the operator who decides what to do about it.
    if completed.stderr.strip():
        print(
            "Note: the advance record was written and verified, and the advance worker also "
            f"wrote to its diagnostic channel: {sanitize_detail(completed.stderr)}",
            file=sys.stderr,
        )
    return reference


def _worker_stderr_clause(completed: subprocess.CompletedProcess) -> str:
    """Carry the worker's own diagnostic into a refusal that was decided elsewhere."""

    text = completed.stderr.strip()
    return f" (the worker also wrote: {sanitize_detail(text)})" if text else ""
