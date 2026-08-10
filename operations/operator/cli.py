"""The ``verbatus`` command: one plain word at a time, with no raw tracebacks."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from operations.pod.models import PodCreateRequest, require_utc

from . import notify_bridge
from .errors import ErrorCode, OperatorError
from .errors import sanitize_detail as _safe_detail
from .surface import DEFAULT_FIXTURE, OperatorSurface
from .volume_s3 import VolumeSpec, VolumeTransferRefusal

ROOT = Path(__file__).resolve().parents[2]


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
        "--workspace", type=Path, default=ROOT, help="the checked-out Apparatus Verbatus folder"
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path(".verbatus"), help="where local receipts are kept"
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
    upload.add_argument(
        "--approval-record",
        type=Path,
        help="current data-handling approval, required with --manifest-out",
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

    run = verbs.add_parser("run", help="run or resume the fixture run tree, naming pages and acts")
    run.add_argument("--run-id", required=True, help="a short name for this run")
    run.add_argument("--scenario", default="happy", help="declared fixture scenario")
    run.add_argument("--fixture", default=DEFAULT_FIXTURE, help="declared fixture name")

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

    verbs.add_parser(
        "status", help="read saved receipts and manifests only; it never contacts a provider"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = _interactive_arguments()
        if not arguments:
            return 0
    parser = build_parser()
    try:
        args = parser.parse_args(arguments)
        workspace = args.workspace.resolve()
        state = args.state_dir if args.state_dir.is_absolute() else workspace / args.state_dir
        surface = OperatorSurface(
            workspace,
            state,
            notifier=notify_bridge.shell_notifier() if args.notify else notify_bridge.silent,
        )
        volume = _network_volume(getattr(args, "network_volume", None))
        if volume is None:
            print("Verbatus is in offline rehearsal mode. It will not contact a cloud provider.")
        else:
            print("Verbatus will not start, adopt or close any pod: that stays offline.")
            print(f"You asked it to send files to {volume.describe()}.")
        if args.verb == "launch":
            request = load_request(args.request)
            spend = args.spend or workspace / "config" / "spend.toml"
            print(f"Using reviewed spending policy: {spend}")
            prepared = surface.prepare_launch(
                request, policy_path=spend, adopt_pod_id=args.adopt_pod
            )
            confirmation = _typed_paid_confirmation()
            surface.launch(prepared, confirmation)
        elif args.verb == "boot":
            surface.boot()
        elif args.verb == "upload":
            if args.sealed_manifest is not None:
                surface.upload(args.source, sealed_manifest=args.sealed_manifest, volume=volume)
            else:
                if args.approval_record is None:
                    raise OperatorError(
                        ErrorCode.INVALID_COMMAND,
                        detail="--approval-record is required when --manifest-out creates a submission record",
                    )
                surface.submit_and_upload(
                    args.source,
                    manifest_out=args.manifest_out,
                    approval_record=args.approval_record,
                    policy_path=args.policy,
                    volume=volume,
                )
        elif args.verb == "run":
            surface.run(run_id=args.run_id, scenario=args.scenario, fixture=args.fixture)
        elif args.verb == "export":
            surface.export(run_id=args.run_id)
        elif args.verb == "close":
            prepared_close = surface.prepare_close(pod_id=args.pod_id)
            confirmation = _typed_close_confirmation(prepared_close.phrase)
            surface.close(prepared_close, confirmation)
        elif args.verb == "status":
            surface.status()
        else:  # argparse owns this list, but an explicit branch prevents a silent no-op.
            raise OperatorError(
                ErrorCode.INVALID_COMMAND, detail="the requested word has no action"
            )
    except OperatorError as error:
        print(error.render())
        return 2
    except KeyboardInterrupt:
        print(OperatorError(ErrorCode.INTERRUPTED).render())
        return 2
    except Exception as error:  # the only route raw implementation failures take to the operator
        wrapped = OperatorError(ErrorCode.UNEXPECTED, detail=_safe_detail(str(error)))
        print(wrapped.render())
        return 2
    return 0


def load_request(path: str | Path) -> PodCreateRequest:
    """Read the strict request shape without showing a JSON/parser traceback."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
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
            ErrorCode.INVALID_COMMAND, detail="the pod request contains an unknown field"
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
            ErrorCode.INVALID_COMMAND, detail="the reviewed pod request is incomplete or invalid"
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
        raise OperatorError(
            ErrorCode.UPLOAD_VOLUME_UNAVAILABLE, detail=_safe_detail(str(error))
        ) from error


def _interactive_arguments() -> list[str]:
    """The double-click route asks for a word and the smallest needed facts."""

    print("Verbatus")
    print("Choose one word: launch, boot, upload, run, export, close, or status.")
    try:
        verb = input("What would you like to do? ").strip().lower()
    except EOFError:
        print("No action was chosen. Nothing changed.")
        return []
    if not verb:
        print("No action was chosen. Nothing changed.")
        return []
    if verb == "launch":
        request = _ask("Path to the reviewed pod request file")
        spend = _ask("Path to the reviewed spending-policy file")
        if not request or not spend:
            print(
                "No reviewed request and spending policy were both chosen. Nothing changed or billed."
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
        manifest = _ask("Path to its sealed submission record")
        return ["upload", "--source", source, "--sealed-manifest", manifest]
    if verb == "run":
        run_id = _ask("A short name for this run", default="dry-run")
        return ["run", "--run-id", run_id]
    if verb == "export":
        return ["export"]
    if verb == "close":
        return ["close"]
    if verb in {"boot", "status"}:
        return [verb]
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
        return input(f"Type exactly {phrase!r} to continue with close: ")
    except EOFError:
        return None


if __name__ == "__main__":  # pragma: no cover - console wrapper
    from .entry import main as entry_main

    raise SystemExit(entry_main())
