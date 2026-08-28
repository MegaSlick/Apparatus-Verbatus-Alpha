"""The ``verbatus`` command: one plain word at a time, with no raw tracebacks."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from operations.pod.models import PodCreateRequest, require_utc

from . import notify_bridge
from .advance import sealed_boundary, trigger_advance
from .custody import python_module_command, run_confined
from .errors import ErrorCode, OperatorError, strip_control_bytes
from .review import ReadOnlyRun
from .surface import DEFAULT_FIXTURE, OperatorSurface
from .volume_s3 import VolumeSpec, VolumeTransferRefusal

MAX_REQUEST_BYTES = 1024 * 1024


def _is_within(path: Path, directory: Path) -> bool:
    """Judge containment by directory identity, including aliases and case variants."""

    try:
        directory_stat = os.stat(directory)
    except OSError:
        return False
    spellings = (Path(os.path.abspath(path)), path.resolve(strict=False))
    for spelling in spellings:
        for ancestor in (spelling, *spelling.parents):
            try:
                if os.path.samestat(os.stat(ancestor), directory_stat):
                    return True
            except OSError:
                continue
    return False


def _account_state_dir(workspace: Path | None = None) -> Path:
    # Path.home() honours HOME, including an absolute value inside the checkout.
    # The account database is the independent fallback for either that case or
    # a relative HOME.
    try:
        environment_home = Path.home()
    except RuntimeError:
        environment_home = Path()

    def homes():
        yield environment_home
        try:
            yield Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            # No passwd entry for this UID -- a container running as an unmapped
            # user, for instance. Building the tuple eagerly ran this lookup
            # before the loop had even looked at the environment home, so every
            # `verbatus` word ended in the unclassified message even when HOME
            # was absolute and perfectly usable. A missing fallback is not a
            # reason to fail a command that never needed it.
            return

    for home in homes():
        candidate = home / ".local" / "state" / "verbatus"
        if home.is_absolute() and (workspace is None or not _is_within(candidate, workspace)):
            return candidate
    raise RuntimeError("the operator account has no absolute home directory outside the checkout")


def _default_state_dir(workspace: Path | None = None) -> Path:
    """Return durable operator state outside the checked-out project.

    The Base Directory specification requires an absolute `XDG_STATE_HOME`;
    accepting a relative value would put records under the workspace.
    """

    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    candidate = (
        state_home / "verbatus" if state_home.is_absolute() else _account_state_dir(workspace)
    )
    if workspace is not None and _is_within(candidate, workspace):
        candidate = _account_state_dir(workspace)
    return candidate


def _warn_about_abandoned_state_dir(workspace: Path, *, using_default: bool) -> None:
    """Name an old in-checkout `.verbatus/` this version no longer reads by default.

    An old folder may hold real receipts; omitting it would make existing records
    appear not to exist.
    """

    old_state = workspace / ".verbatus"
    if not using_default or not old_state.is_dir():
        return
    _print(
        f"Note: {old_state} holds operator records from before this version moved "
        "the default state location outside the checkout. They are not read here "
        f"automatically — point at them explicitly with: --state-dir {old_state}"
    )


def _print(text: str = "") -> None:
    """Print through the same control-byte stripping the operator surface uses.

    Applied one line at a time, not to the whole string at once:
    `strip_control_bytes` treats a newline as a control byte like any other,
    and `OperatorError.render()`'s three-part message depends on its own
    newlines to stay three parts on screen.
    """

    print("\n".join(strip_control_bytes(line) for line in text.split("\n")))


class PlainParser(argparse.ArgumentParser):
    """Argparse must use the same recovery contract as every other failure."""

    def error(self, message: str) -> None:
        raise OperatorError(ErrorCode.INVALID_COMMAND, detail=message)


def build_parser() -> PlainParser:
    parser = PlainParser(
        prog="verbatus",
        description="A safe, offline rehearsal for the Apparatus Verbatus operator flow.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        # The current directory, not this module's own parents: under an
        # installed wheel that spelling is site-packages, where no project
        # config lives and where receipt writes do not belong. The wrapper cd's
        # into the checkout before running, so the default is right for both
        # the double-click route and a terminal opened at the project root.
        default=Path.cwd(),
        help="the checked-out Apparatus Verbatus folder (defaults to the current directory)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        # No computed default here. `main` resolves the durable default against
        # the *resolved* workspace, and `None` is how it knows the operator did
        # not name a path. Deciding that from a scan of raw argv could not work:
        # argparse accepts unambiguous abbreviations, so `--state-di /records`
        # set this value and the scan missed it -- the named folder was parsed
        # and then silently overwritten with the default, and `status` afterwards
        # read a different set of records than the operator had asked for.
        default=None,
        help="where local receipts are kept",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "also send a phone notification when a run or export finishes, or when a run is "
            "held for a decision. Off unless you ask for it; the terminal always tells you "
            "whether it arrived"
        ),
    )
    verbs = parser.add_subparsers(dest="verb", required=True, title="words you can use")

    launch = verbs.add_parser(
        "launch", help="show price and ceilings, then record a typed paid confirmation"
    )
    launch.add_argument("--request", type=Path, required=True, help="reviewed pod request JSON")
    launch.add_argument("--spend", type=Path, help="reviewed spending policy TOML")
    launch.add_argument(
        "--adopt-pod", help="adopt this already-recorded fixture pod through the same gate"
    )

    verbs.add_parser("boot", help="run bootstrap and finish with a green or red report")

    upload = verbs.add_parser(
        "upload", help="seal or reuse a submission record, then transfer with zero GPU-hours"
    )
    upload.add_argument(
        "--source", type=Path, required=True, help="folder containing the submitted files"
    )
    reuse = upload.add_mutually_exclusive_group(required=True)
    reuse.add_argument(
        "--sealed-manifest", type=Path, help="existing sealed Spec 03 submission record"
    )
    reuse.add_argument(
        "--manifest-out",
        type=Path,
        help="where Spec 03 should write a new sealed submission record",
    )
    upload.add_argument("--policy", type=Path, help="data-handling policy used with --manifest-out")
    upload.add_argument(
        "--network-volume",
        metavar="DATACENTER:VOLUME_ID",
        help=(
            "send to a real RunPod network volume instead of the local fixture volume, "
            "for example EU-CZ-1:abc123. This is the one thing this tool can do that "
            "leaves your computer, so you have to name it; it needs no pod and uses no "
            "GPU-hours. Credentials are read from RUNPOD_S3_ACCESS_KEY and "
            "RUNPOD_S3_SECRET_KEY in your environment, never from a file here"
        ),
    )

    run = verbs.add_parser("run", help="run or resume a recorded fixture or real submission")
    run.add_argument("--run-id", required=True, help="a short name for this run")
    run.add_argument("--scenario", default="happy", help="declared fixture scenario")
    run.add_argument("--fixture", default=DEFAULT_FIXTURE, help="declared fixture name")
    run.add_argument(
        "--submission-folder", type=Path, help="approved folder of real submitted files"
    )
    run.add_argument(
        "--submission-manifest", type=Path, help="self-hashed ledger for the real folder"
    )
    run.add_argument("--data-gate-policy", type=Path, help="approved-storage policy for real input")

    export = verbs.add_parser(
        "export", help="copy the recorded base Armarium evidence locally and print reconciliation"
    )
    export.add_argument("--run-id", help="the explicitly recorded run to export")

    close = verbs.add_parser(
        "close", help="record a typed confirmation, then verify close and captured cost"
    )
    close.add_argument(
        "--pod-id", help="the recorded fixture pod id, if you want to repeat it explicitly"
    )

    verbs.add_parser("status", help="read saved receipts only; it never contacts a provider")
    review = verbs.add_parser(
        "review",
        help="open one run tree read-only; it cannot contact a provider or change evidence",
    )
    review.add_argument(
        "--run-root", type=Path, required=True, help="folder containing the run tree"
    )
    review.add_argument("--run-id", required=True, help="the sealed run to inspect")
    advance = verbs.add_parser(
        "advance",
        help="append Tyrel's confirmed decision to pass one exact sealed stage boundary",
    )
    advance.add_argument(
        "--run-root", type=Path, required=True, help="folder containing the run tree"
    )
    advance.add_argument("--run-id", required=True, help="the sealed run to advance")
    advance.add_argument("--stage", required=True, help="the sealed stage boundary to pass")
    advance.add_argument("--reason", required=True, help="why Tyrel chose to advance this boundary")
    backup = verbs.add_parser(
        "backup",
        help="copy one run tree to a local Mac directory by digest, without a provider credential",
    )
    backup.add_argument(
        "--run-root", type=Path, required=True, help="folder containing the volume-hosted run"
    )
    backup.add_argument("--run-id", required=True, help="the sealed run to copy")
    backup.add_argument(
        "--mac-directory", type=Path, required=True, help="local synced backup directory"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        # Parser construction resolves the environment-derived state root and
        # belongs inside the same refusal boundary as parsing and execution.
        parser = build_parser()
        if not arguments:
            # Inside the try, not before it: a Ctrl+C at this prompt has to
            # reach the same three-part contract as every other failure.
            arguments = _interactive_arguments()
            if not arguments:
                return 0
        args = parser.parse_args(arguments)
        workspace = args.workspace.resolve()
        explicit_state = args.state_dir is not None
        if explicit_state:
            state = args.state_dir if args.state_dir.is_absolute() else workspace / args.state_dir
        else:
            state = _default_state_dir(workspace)
        _warn_about_abandoned_state_dir(workspace, using_default=not explicit_state)
        surface = OperatorSurface(
            workspace,
            state,
            notifier=notify_bridge.shell_notifier() if args.notify else notify_bridge.silent,
        )
        volume = _network_volume(getattr(args, "network_volume", None))
        if volume is None:
            _print("Verbatus is in offline rehearsal mode. It will not contact a cloud provider.")
        else:
            _print("Verbatus will not start, adopt or close any pod: that stays offline.")
            _print(f"You asked it to send files to {volume.describe()}.")
        if args.verb == "launch":
            request = load_request(args.request)
            spend = args.spend or workspace / "config" / "spend.toml"
            _print(f"Using reviewed spending policy: {spend}")
            prepared = surface.prepare_launch(
                request, policy_path=spend, adopt_pod_id=args.adopt_pod
            )
            confirmation = _typed_paid_confirmation()
            surface.launch(prepared, confirmation)
        elif args.verb == "boot":
            surface.boot()
        elif args.verb == "upload":
            if args.sealed_manifest is not None:
                if args.policy is not None:
                    raise OperatorError(
                        ErrorCode.INVALID_COMMAND,
                        detail=(
                            "--policy applies only when --manifest-out seals a new submission "
                            "record; an existing sealed record already carries the policy it "
                            "was sealed under"
                        ),
                    )
                surface.upload(args.source, sealed_manifest=args.sealed_manifest, volume=volume)
            else:
                surface.submit_and_upload(
                    args.source,
                    manifest_out=args.manifest_out,
                    policy_path=args.policy,
                    volume=volume,
                )
        elif args.verb == "run":
            surface.run(
                run_id=args.run_id,
                scenario=args.scenario,
                fixture=args.fixture,
                submission_folder=args.submission_folder,
                submission_manifest=args.submission_manifest,
                data_gate_policy=args.data_gate_policy,
            )
        elif args.verb == "export":
            surface.export(run_id=args.run_id)
        elif args.verb == "close":
            prepared_close = surface.prepare_close(pod_id=args.pod_id)
            confirmation = _typed_close_confirmation(prepared_close.phrase)
            surface.close(prepared_close, confirmation)
        elif args.verb == "status":
            surface.status()
        elif args.verb == "review":
            _review_in_custody(args.run_root, args.run_id, workspace)
        elif args.verb == "advance":
            _advance_with_confirmation(
                args.run_root,
                args.run_id,
                args.stage,
                reason=args.reason,
                workspace=workspace,
            )
        elif args.verb == "backup":
            _backup_in_custody(args.run_root, args.run_id, args.mac_directory, workspace)
        # Parser choices must never become a silent no-op if dispatch drifts.
        else:
            raise OperatorError(
                ErrorCode.INVALID_COMMAND, detail="the requested word has no action"
            )
    except OperatorError as error:
        _print(error.render())
        return 2
    except KeyboardInterrupt:
        _print(OperatorError(ErrorCode.INTERRUPTED).render())
        return 2
    # Raw implementation failures must reach the same three-part operator contract.
    except Exception as error:
        wrapped = OperatorError(ErrorCode.UNEXPECTED, detail=str(error))
        _print(wrapped.render())
        return 2
    return 0


def _bound_run_tree(run_tree_class, run_root: Path, run_id: str):
    """Bind one run before any verb acts on it, and name a bad id as a bad id.

    `RunTree.__init__` validates the run id and refuses one that resolves
    outside the run root; both arrive as `ContractError`. Constructed outside
    a guard, a typed `--run-id My-Run` reached the unclassified handler and
    told the operator to photograph the message and find a maintainer over one
    capital letter, and an attempted escape from the approved run root
    reported itself the same way — a tool that broke rather than a request
    that was refused.

    The code is `INVALID_COMMAND` rather than a verb-specific refusal because
    that is what happened: the instruction named no run this tool could bind,
    nothing was read, and nothing was changed. A tree-unreadable code would
    send the operator to preserve and investigate register evidence that was
    never opened.
    """

    from common.contracts.errors import ContractError

    try:
        return run_tree_class(Path(run_root).resolve(), run_id)
    except (ContractError, OSError) as error:
        raise OperatorError(ErrorCode.INVALID_COMMAND, detail=str(error)) from error


def _review_in_custody(run_root: Path, run_id: str, workspace: Path) -> None:
    """Exec the renderer with no credential and a kernel-enforced no-write policy."""

    # Read before crossing the boundary.  The UI child receives only this
    # immutable value stream, never a run-tree path or a ``RunTree`` object.
    # It can deceive its viewer about those bytes if compromised, but cannot
    # reopen the evidence to mutate it or reach any pipeline/provider module.
    from common.runtree.store import RunTree

    _bound_run_tree(RunTree, run_root, run_id)
    projection = dataclasses.asdict(ReadOnlyRun(run_root, run_id).projection())
    command = python_module_command("operations.operator.console")
    backend, completed = run_confined(
        command,
        writable=None,
        cwd=workspace,
        input_text=json.dumps(projection),
    )
    if completed.returncode != 0:
        launcher = backend.launcher_failure(completed)
        if launcher is not None:
            # The confinement launcher never exec'd the console, so this is a
            # platform-enforcement refusal rather than a claim that the run
            # tree itself is unreadable.
            raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=launcher)
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail=completed.stdout or completed.stderr
        )
    _print(completed.stdout.rstrip())


def _backup_in_custody(run_root: Path, run_id: str, mac_directory: Path, _workspace: Path) -> None:
    """Copy evidence only in the no-network, credential-free custody child."""

    from .backup import (
        BackupRefusal,
        BackupReport,
        destination_identities,
        prepare_backup_layout,
        required_identity,
        resolve_backup_paths,
        verify_backup_snapshot,
    )

    # The parent must reject overlap before creating the layout; otherwise its
    # setup can write `objects/` and `snapshots/` inside the sealed source.
    # Custody grants the child publication rights but deliberately withholds
    # directory creation, so the checked closed layout must exist first.
    try:
        source, destination = resolve_backup_paths(run_root, run_id, mac_directory)
        prepare_backup_layout(source, destination)
        source_identity = required_identity(source, what="source run tree")
        destination_identity = destination_identities(destination)
    except BackupRefusal as refusal:
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=str(refusal)) from refusal
    # `--workspace` selects project data for other verbs; it is not authority to
    # replace this custody worker's code: the surviving python_module_command
    # pins the import root to the checkout that loaded this module and accepts
    # no caller-nominated root at all. The same pinned root stays the cwd.
    worker_root = Path(__file__).resolve().parents[2]
    command = python_module_command("operations.operator.backup_worker")
    request = json.dumps(
        {
            "run_root": str(run_root.resolve()),
            "run_id": run_id,
            "mac_directory": str(destination),
            "source_identity": list(source_identity),
            "destination_identities": [list(identity) for identity in destination_identity],
        }
    )
    backend, completed = run_confined(
        command, writable=destination, cwd=worker_root, input_text=request
    )
    if completed.returncode != 0:
        launcher = backend.launcher_failure(completed)
        detail = launcher or completed.stderr.strip() or completed.stdout.strip()
        if not detail:
            detail = f"backup worker exited {completed.returncode} without a diagnostic"
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=detail)
    try:
        report = BackupReport.from_record(json.loads(completed.stdout))
        verify_backup_snapshot(
            destination,
            run_id,
            report,
            expected_destination_identities=destination_identity,
        )
    except (BackupRefusal, ValueError, RecursionError) as error:
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=str(error)) from error
    _print(
        "Mac backup complete: "
        f"{report.copied} copied, {report.reused} reused; snapshot {report.snapshot_sha256}."
    )


def _advance_with_confirmation(
    run_root: Path,
    run_id: str,
    stage: str,
    *,
    reason: str,
    workspace: Path,
) -> None:
    """Bind a human confirmation to one observed digest, then launch the worker."""

    from common.contracts.errors import ApprovalRefusal
    from common.runtree.store import RunTree

    tree = _bound_run_tree(RunTree, run_root, run_id)
    try:
        _seal, digest = sealed_boundary(tree, stage)
    except ApprovalRefusal as error:
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=str(error)) from error
    phrase = f"advance {run_id} past {stage} at {digest}"
    _print(f"The current {stage} boundary has seal digest {digest}.")
    confirmation = _typed_advance_confirmation(phrase)
    if confirmation != phrase:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail="the typed confirmation did not exactly name this run, stage, and seal digest",
        )
    reference = trigger_advance(
        run_root,
        run_id,
        stage,
        reason=reason,
        workspace=workspace,
        expected_digest=digest,
    )
    _print(f"Advance record: {reference.relative_path} ({reference.sha256})")


def load_request(path: str | Path) -> PodCreateRequest:
    """Read the strict request shape without showing a JSON/parser traceback."""

    source = Path(path)
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("the pod request is not a regular file")
            data = handle.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError(f"the pod request exceeds {MAX_REQUEST_BYTES} bytes")
        raw = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.INVALID_COMMAND, detail="the pod request JSON could not be read"
        ) from error
    if not isinstance(raw, dict):
        raise OperatorError(
            ErrorCode.INVALID_COMMAND, detail="the pod request must be a JSON object"
        )
    allowed = {
        "name",
        "gpu_type",
        "image",
        "volume_id",
        "volume_mount_path",
        "docker_start_cmd",
        "hard_deadline",
        "repository_commit",
        "template",
        "metadata",
        "interruptible",
        "recovery_only",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise OperatorError(
            ErrorCode.INVALID_COMMAND,
            detail=f"the pod request contains unknown fields: {', '.join(unknown)}",
        )
    try:
        command = raw["docker_start_cmd"]
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("docker_start_cmd must be a list of words")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise ValueError("metadata must map words to words")
        interruptible = raw.get("interruptible", False)
        recovery_only = raw.get("recovery_only", False)
        if not isinstance(interruptible, bool) or not isinstance(recovery_only, bool):
            raise ValueError("interruptible and recovery_only must be true or false")
        deadline = datetime.fromisoformat(str(raw["hard_deadline"]).replace("Z", "+00:00"))
        return PodCreateRequest(
            name=raw["name"],
            gpu_type=raw["gpu_type"],
            image=raw["image"],
            volume_id=raw["volume_id"],
            volume_mount_path=raw["volume_mount_path"],
            docker_start_cmd=tuple(command),
            hard_deadline=require_utc(deadline, "hard deadline"),
            repository_commit=raw["repository_commit"],
            template=raw.get("template"),
            metadata=metadata,
            interruptible=interruptible,
            recovery_only=recovery_only,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.INVALID_COMMAND,
            detail=f"the reviewed pod request is incomplete or invalid: {error}",
        ) from error


def _network_volume(value: str | None) -> VolumeSpec | None:
    """Read `DATACENTER:VOLUME_ID` without letting a typo become a raw traceback."""

    if value is None:
        return None
    datacenter, separator, volume_id = value.partition(":")
    if not separator or not datacenter or not volume_id:
        raise OperatorError(
            ErrorCode.UPLOAD_VOLUME_UNAVAILABLE,
            detail="a network volume is written as DATACENTER:VOLUME_ID, for example EU-CZ-1:abc123",
        )
    try:
        return VolumeSpec(datacenter_id=datacenter, volume_id=volume_id)
    except VolumeTransferRefusal as error:
        raise OperatorError(ErrorCode.UPLOAD_VOLUME_UNAVAILABLE, detail=str(error)) from error


def _interactive_arguments() -> list[str]:
    """The double-click route asks for a word and the smallest needed facts."""

    _print("Verbatus")
    _print(
        "Choose one word: launch, boot, upload, run, export, close, status, review, advance, or backup."
    )
    try:
        verb = input("What would you like to do? ").strip().lower()
    except EOFError:
        _print("No action was chosen. Nothing changed.")
        return []
    if not verb:
        _print("No action was chosen. Nothing changed.")
        return []
    if verb == "launch":
        request = _ask("Path to the reviewed pod request file")
        spend = _ask("Path to the reviewed spending-policy file")
        if not request or not spend:
            _print(
                "Launch needs both a reviewed pod request and a reviewed spending policy. "
                "One of them was left blank, so nothing changed or billed."
            )
            return []
        adoption_id = _ask(
            "Recorded fixture pod ID to adopt (leave blank to create a new fixture pod)"
        )
        arguments = ["launch", "--request", request, "--spend", spend]
        if adoption_id:
            arguments.extend(("--adopt-pod", adoption_id))
        return arguments
    if verb == "upload":
        source = _ask("Folder containing the submitted files")
        if not source:
            _print(
                "Upload needs a folder containing the submitted files. "
                "It was left blank, so nothing changed."
            )
            return []
        manifest = _ask(
            "Path to its sealed submission record (leave blank if this is the first upload)"
        )
        if manifest:
            return ["upload", "--source", source, "--sealed-manifest", manifest]
        manifest_out = _ask("Path where the new sealed submission record should be saved")
        if not manifest_out:
            _print(
                "A first upload needs a destination for its new sealed submission record. "
                "It was left blank, so nothing changed."
            )
            return []
        return ["upload", "--source", source, "--manifest-out", manifest_out]
    if verb == "run":
        run_id = _ask("A short name for this run", default="dry-run")
        return ["run", "--run-id", run_id]
    if verb == "export":
        return ["export"]
    if verb == "close":
        return ["close"]
    if verb in {"review", "advance", "backup"}:
        run_root = _ask("Folder containing the run tree")
        run_id = _ask("The sealed run ID")
        if not run_root or not run_id:
            _print(f"{verb.title()} needs both a run-tree folder and a run ID. Nothing changed.")
            return []
        arguments = [verb, "--run-root", run_root, "--run-id", run_id]
        if verb == "advance":
            stage = _ask("The sealed stage boundary to pass")
            reason = _ask("Why this boundary should be advanced")
            if not stage or not reason:
                _print("Advance needs both a stage and a reason. Nothing changed.")
                return []
            arguments.extend(("--stage", stage, "--reason", reason))
        if verb == "backup":
            mac_directory = _ask("Local synced Mac backup directory")
            if not mac_directory:
                _print("Backup needs a local synced destination directory. Nothing changed.")
                return []
            arguments.extend(("--mac-directory", mac_directory))
        return arguments
    return [verb]


def _ask(label: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        answer = input(f"{label}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def _typed_paid_confirmation() -> str | None:
    try:
        return input("Type the confirmation shown above to continue with this paid action: ")
    except EOFError:
        return None


def _typed_close_confirmation(phrase: str) -> str | None:
    try:
        return input(f"Type this line exactly, with no quotation marks:\n{phrase}\n> ")
    except EOFError:
        return None


def _typed_advance_confirmation(phrase: str) -> str | None:
    try:
        return input(
            "This appends Tyrel's decision record; it does not edit evidence or start a provider.\n"
            f"Type this line exactly, with no quotation marks:\n{phrase}\n> "
        )
    except EOFError:
        return None


if __name__ == "__main__":  # pragma: no cover - console wrapper
    from .entry import main as entry_main

    raise SystemExit(entry_main())
