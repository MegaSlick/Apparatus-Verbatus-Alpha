"""The ``verbatus`` command: one plain word at a time, with no raw tracebacks."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import stat
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Final, Sequence

from common.checkout import missing_checkout_resources
from common.contracts.stages import STAGES
from common.stage import RUN_MODES
from operations.pod.launch import launch_evidence_keys, launch_evidence_prefixes, launch_run_id
from operations.pod.models import (
    DEFAULT_CONTAINER_DISK_GB,
    PodCreateRequest,
    require_utc,
)
from operations.pod.transfer import normalize_transfer_prefix

from . import console, notify_bridge, review_text
from .advance import (
    UnsealedBoundaryRefusal,
    boundary_summary,
    held_boundaries_for_mode,
    trigger_advance,
)
from .custody import python_module_command, run_confined
from .errors import ErrorCode, OperatorError, strip_control_bytes
from .ingest import ingest_in_custody
from .records import DescriptorStore, ReceiptStore
from .review import ReadOnlyRun
from .spend import SpendSurface
from .surface import DEFAULT_FIXTURE, OperatorSurface, bounded_tail
from .volume_s3 import VolumeSpec, VolumeTransferRefusal

MAX_REQUEST_BYTES = 1024 * 1024


def _upload_prefix(value: str) -> str:
    try:
        return normalize_transfer_prefix(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


# What each word reads from the workspace by checkout-relative path. Verbs
# absent here have a workspace that is legitimately not the checkout (a
# folder of run trees, a backup drive), so they are never checked against it.
_CHECKOUT_RESOURCES_BY_VERB: Final[dict[str, tuple[str, ...]]] = {
    "run": ("pipeline", "config", "proof"),
    "boot": ("config", "proof"),
    "ingest": ("config",),
    "triage": ("config",),
    "launch": ("config",),
    "spend": ("config",),
}


def _checkout_resources_read(args: argparse.Namespace) -> tuple[str, ...]:
    """The checkout directories this exact invocation will read from its workspace."""

    needed = _CHECKOUT_RESOURCES_BY_VERB.get(args.verb, ())
    if args.verb == "launch" and args.spend is not None:
        return ()
    if args.verb == "spend" and args.policy is not None:
        return ()
    if args.verb == "ingest" and args.policy is not None:
        return ()
    return needed


def _require_workspace_checkout(workspace: Path, args: argparse.Namespace) -> None:
    """Refuse, before anything starts, a workspace missing what this word reads.

    Checks `--workspace` itself rather than the code's own import directory,
    for exactly the resources the chosen word reads, so a word whose
    workspace is legitimately not the checkout is never refused for lacking
    one.
    """

    needed = _checkout_resources_read(args)
    if not needed:
        return
    missing = tuple(name for name in missing_checkout_resources(workspace) if name in needed)
    if missing:
        raise OperatorError(
            ErrorCode.NOT_A_CHECKOUT,
            detail=(
                f"`verbatus {args.verb}` reads {', '.join(needed)} from its workspace, and "
                f"{workspace} has no {', '.join(missing)} directory; start from the checkout "
                "or name it with --workspace"
            ),
        )


def _current_directory() -> str:
    """Where this ran, or a stated unavailability -- never a second failure.

    `os.getcwd()` raises when the directory this process started in has been
    deleted or is no longer readable; a receipt that cannot say where it ran
    is still the record of what happened.
    """

    try:
        return os.getcwd()
    except OSError as error:
        return f"unavailable: {error.strerror or error}"


def record_unexpected(
    error: BaseException, arguments: Sequence[str], state: Path | None
) -> OperatorError:
    """Turn an unclassified failure into the operator message, with a receipt behind it.

    The receipt carries the exception, a bounded trace, the command and the
    working directory; the message names the receipt first, since
    `sanitize_detail` cuts a rendered detail at a traceback header and an
    exception message can contain one.
    """

    described = f"{type(error).__name__}: {error}"
    if state is None:
        # Before `--state-dir` is resolved, fall back to the default location
        # where the operator's other records already are.
        try:
            state = _default_state_dir()
        except Exception:  # noqa: BLE001 -- best effort; the message below says so
            state = None
    if state is None:
        return OperatorError(
            ErrorCode.UNEXPECTED,
            detail=f"No receipt could be saved (no state directory could be resolved). {described}",
        )
    # Bounded like a child's output: an exception message can carry a whole
    # document, past what a receipt can be read back at.
    message = bounded_tail(str(error))
    payload = {
        "summary": (
            "Verbatus met a problem it could not classify: "
            f"{type(error).__name__}: {_first_line(message)}"
        ),
        "state": "unexpected",
        "exception_type": type(error).__name__,
        "message": message,
        "traceback": bounded_tail(
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
        ),
        "argv": [str(word) for word in arguments],
        "cwd": _current_directory(),
    }
    try:
        receipt = ReceiptStore(state).write("unexpected", payload)
        DescriptorStore(state).record("unexpected", receipt)
    except Exception as record_error:  # noqa: BLE001 -- said aloud, never over the failure
        return OperatorError(
            ErrorCode.UNEXPECTED,
            detail=(
                f"No receipt could be saved under {state} ({record_error}); this message is "
                f"the only record. {described}"
            ),
        )
    return OperatorError(
        ErrorCode.UNEXPECTED, detail=f"Saved unexpected receipt: {receipt}. {described}"
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


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
    # Path.home() honours HOME, which may point inside the checkout or be
    # relative; the account database below is the independent fallback.
    try:
        environment_home = Path.home()
    except RuntimeError:
        environment_home = Path()

    def homes():
        yield environment_home
        try:
            yield Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            # No passwd entry for this UID (an unmapped container user, say);
            # a missing fallback is not a reason to fail a command that never
            # needed it.
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


_UNREADABLE_RECEIPT = (
    OSError,
    UnicodeDecodeError,
    json.JSONDecodeError,
    # A receipt inside the byte bound can still nest deeply enough for the
    # decoder to recurse out on 3.12; that is unreadable, not internal.
    RecursionError,
    KeyError,
    TypeError,
    ValueError,
)


def _read_launch_command(
    receipt: Path | None, volume: VolumeSpec | None = None, run_id: str | None = None
) -> tuple[list[str], str] | None:
    """The sealed ``docker_start_cmd`` and volume mount a saved launch receipt names.

    Shared by every deriver that reads launch-bound paths out of it
    (``_derived_evidence_keys``, ``_derived_evidence_prefixes``), so the read,
    shape checks and cross-volume refusal live in one place. Refuses loudly
    rather than silently on an unreadable receipt, one with no launch
    request, one for a different volume, or one whose command started a
    different run: any of these could otherwise store another launch's
    records under this run's evidence, misstating their provenance rather
    than merely failing to find them. Read through a bounded, no-follow open,
    since a record this verb did not write is not read whole on trust.
    Returns ``None`` when no receipt was named.
    """

    if receipt is None:
        return None
    try:
        descriptor = os.open(receipt, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("the launch receipt is not a regular file")
            data = handle.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError(f"the launch receipt exceeds {MAX_REQUEST_BYTES} bytes")
        # `ReceiptStore.write` stores every action under `payload`, and the
        # launch request with it. Read directly rather than through
        # `ReceiptStore.read`, since this receipt may sit outside the state
        # root that store resolves against.
        record = json.loads(data.decode("utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            raise ValueError("the receipt does not carry an operator receipt payload")
        request = record["payload"]["request"]
        command = request["docker_start_cmd"]
        mount = request["volume_mount_path"]
        recorded_volume = request["volume_id"]
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("docker_start_cmd is not a list of words")
        if not isinstance(mount, str) or not mount:
            raise ValueError("volume_mount_path is missing")
        # Decoded inside the guard: `launch_run_id` does its own JSON decode
        # of the nested `--bootstrap-command-json` value, which can recurse
        # out on the same pathologically-nested input `_UNREADABLE_RECEIPT`
        # already guards the outer read against.
        recorded_run_id = launch_run_id(command)
    except _UNREADABLE_RECEIPT as error:
        raise OperatorError(
            ErrorCode.FETCH_RUN_FAILED,
            detail=(
                f"the launch receipt {receipt} does not carry a readable launch request, so "
                f"no evidence key or prefix could be derived from it: {error}"
            ),
        ) from error
    if volume is not None and recorded_volume != volume.volume_id:
        raise OperatorError(
            ErrorCode.FETCH_RUN_FAILED,
            detail=(
                f"the launch receipt {receipt} is for network volume {recorded_volume!r}, and "
                f"this call is reading {volume.volume_id!r}. Deriving from it would name "
                "another launch's records on a volume that never held them; name the receipt "
                "for this run, or pass --evidence-key/--evidence-prefix explicitly"
            ),
        )
    if run_id is not None and recorded_run_id != run_id:
        # A hold-only launch, or a receipt whose command cannot be decoded,
        # still has derivable evidence keys, just none belonging to any run;
        # asked for a specific run, it is refused the same as a proven
        # mismatch.
        if recorded_run_id is None:
            detail = (
                f"the launch receipt {receipt} does not prove that run {run_id!r} started; "
                "name the receipt for this run, or pass --evidence-key/--evidence-prefix "
                "explicitly"
            )
        else:
            detail = (
                f"the launch receipt {receipt} started run {recorded_run_id!r}, and this call "
                f"is fetching {run_id!r}. Deriving from it would store another run's evidence "
                "beside this one and misstate its provenance; name the receipt for this run, "
                "or pass --evidence-key/--evidence-prefix explicitly"
            )
        raise OperatorError(ErrorCode.FETCH_RUN_FAILED, detail=detail)
    return command, mount


def _derived_evidence_keys(
    receipt: Path | None, volume: VolumeSpec | None = None, run_id: str | None = None
) -> tuple[str, ...]:
    """The launch-bound evidence keys a saved launch receipt already names."""

    read = _read_launch_command(receipt, volume, run_id)
    if read is None:
        return ()
    command, mount = read
    return launch_evidence_keys(command, volume_mount_path=mount)


def _derived_evidence_prefixes(
    receipt: Path | None, volume: VolumeSpec | None = None, run_id: str | None = None
) -> tuple[str, ...]:
    """The launch-scoped evidence prefixes a saved launch receipt already names."""

    read = _read_launch_command(receipt, volume, run_id)
    if read is None:
        return ()
    command, mount = read
    return launch_evidence_prefixes(command, volume_mount_path=mount)


def _print(text: str = "") -> None:
    """Print through the same control-byte stripping the operator surface uses.

    Applied one line at a time: `strip_control_bytes` treats a newline as a
    control byte like any other, and multi-line messages depend on theirs.
    """

    print("\n".join(strip_control_bytes(line) for line in text.split("\n")))


# argparse stops accepting parent-parser options once the verb token is
# consumed, so any of these typed after the verb is rejected as unrecognized
# with nothing pointing at the fix; `_annotate_unrecognized` adds that.
_TOP_LEVEL_ONLY_FLAGS: Final = ("--workspace", "--state-dir", "--notify")


class PlainParser(argparse.ArgumentParser):
    """Argparse must use the same recovery contract as every other failure."""

    def error(self, message: str) -> None:
        raise OperatorError(ErrorCode.INVALID_COMMAND, detail=_annotate_unrecognized(message))


def _annotate_unrecognized(message: str) -> str:
    """Name the fix when argparse's unrecognized-arguments message is our own.

    `message` is argparse's own wording, matched by prefix rather than
    parsed, so an unrecognized wording reaches the operator unmodified
    instead of being misread.
    """

    prefix = "unrecognized arguments: "
    if not message.startswith(prefix):
        return message
    tokens = {token.split("=", 1)[0] for token in message[len(prefix) :].split()}
    named = [flag for flag in _TOP_LEVEL_ONLY_FLAGS if flag in tokens]
    if not named:
        return message
    return (
        f"{message} ({', '.join(named)} {'is' if len(named) == 1 else 'are'} accepted only "
        "before the word, e.g. 'verbatus --state-dir DIR review ...', not after it)"
    )


def build_parser() -> PlainParser:
    parser = PlainParser(
        prog="verbatus",
        description="A safe, offline rehearsal for the Apparatus Verbatus operator flow.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        # The current directory, not this module's own parents, which under
        # an installed wheel would be site-packages; the wrapper cd's into
        # the checkout before running.
        default=Path.cwd(),
        help="the checked-out Apparatus Verbatus folder (defaults to the current directory)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        # No computed default: `main` resolves the durable default against
        # the resolved workspace, and `None` is how it knows none was named.
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
        "--prefix",
        type=_upload_prefix,
        default="submission",
        help=(
            "one safe relative object-key component for this immutable submission, with no "
            "'/': its ledger is written beside it as <prefix>-manifest.json, and a nested "
            "prefix would leave that ledger inside another prefix's inventory "
            "(default: submission)"
        ),
    )
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

    ingest = verbs.add_parser(
        "ingest",
        help="prepare one folder for the Door: ledger, data gate, triage evidence, and confirmation",
    )
    ingest.add_argument(
        "--source", type=Path, required=True, help="folder containing submitted masters"
    )
    ingest.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="existing empty approved folder for the ready-to-submit records",
    )
    ingest.add_argument(
        "--policy",
        type=Path,
        help="reviewed data-handling policy (defaults to config/data_handling_policy.json)",
    )
    ingest.add_argument("--corpus-id", required=True, help="non-empty corpus identity for triage")
    ingest.add_argument(
        "--mode",
        choices=("manual", "semi", "auto"),
        default="auto",
        help="declared triage mode; all current modes remain routed to review",
    )
    ingest.add_argument(
        "--confirmation-file",
        type=Path,
        help="canonical Unit 6B cluster-confirmation file; omit when confirming no cluster",
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
    run.add_argument(
        "--models-config",
        type=Path,
        help="the chair roster to seal into this run (config/models-real.toml for the real "
        "chairs); always supplied with --serving-recipes-config and "
        "--witness-context-config",
    )
    run.add_argument(
        "--serving-recipes-config",
        type=Path,
        help="the serving catalogue the roster's chairs are served under "
        "(config/serving_recipes_real.toml with the real roster); always supplied with "
        "--models-config and --witness-context-config",
    )
    run.add_argument(
        "--witness-context-config",
        type=Path,
        help="the factual witness-context declaration the Perlector is told about this "
        "roster's chairs (config/witness_context-real.toml with the real roster; the "
        "default describes every chair as a synthetic fixture); always supplied with "
        "--models-config and --serving-recipes-config",
    )

    fetch_run = verbs.add_parser(
        "fetch-run",
        help="bring one run tree back from the network volume, every object digest-checked",
    )
    fetch_run.add_argument("--run-id", required=True, help="the run pod_run wrote on the volume")
    fetch_run.add_argument(
        "--into",
        type=Path,
        required=True,
        help="local run root; the tree lands at <into>/<run-id> and an existing file there "
        "is compared, never replaced",
    )
    fetch_run.add_argument(
        "--network-volume",
        metavar="DATACENTER:VOLUME_ID",
        required=True,
        help=(
            "the RunPod network volume the run was written to, for example EU-CZ-1:abc123. "
            "Reading it needs no pod and uses no GPU-hours. Credentials are read from "
            "RUNPOD_S3_ACCESS_KEY and RUNPOD_S3_SECRET_KEY in your environment, never from a "
            "file here"
        ),
    )
    fetch_run.add_argument(
        "--evidence-key",
        action="append",
        metavar="KEY",
        help="one more volume key to bring home beside the run tree, repeatable. The "
        "launch's preflight/ tree comes home on its own; the bootstrap report, the pod-run "
        "report, that report's '-hold' liveness sibling and the bootstrap journal are named "
        "with the launch token at paths this verb cannot derive, and "
        "'pod-transfer-journal.json' sits at the volume root outside both prefixes, so name "
        "each here -- or let --launch-receipt derive the token-bound ones for you. A key is "
        "volume-root-relative -- the volume path with the mount prefix removed, never a "
        "leading '/'. "
        "operations/pod/README.md lists the complete set and how each key is derived. "
        "The receipt says which were fetched and which were not",
    )
    fetch_run.add_argument(
        "--evidence-prefix",
        action="append",
        metavar="PREFIX",
        help="a volume prefix to bring home into <into>/evidence/, repeatable; the default "
        "is the whole preflight/ tree. A volume is reused across launches, so preflight/ "
        "holds every launch's evidence and a later reader cannot say which measured the "
        "chairs for THIS run. Name this run's own stem -- preflight/<bootstrap report stem>, "
        "which carries the launch token -- to bring back exactly one launch's evidence",
    )
    fetch_run.add_argument(
        "--launch-receipt",
        type=Path,
        metavar="PATH",
        help="the saved launch receipt for this run. Its sealed docker_start_cmd already "
        "names the launch-token-bound report paths, so the exact --evidence-key values are "
        "derived from it and printed rather than retyped from a 32-hex token by hand. "
        "Derivation rule: each --report-path in that command, made relative to "
        "volume_mount_path, plus the siblings the program that writes it writes -- "
        "-terminating.json for the pod timer's report, and -hold.json, -liveness.json, "
        "-timings.json and -transcript.log for pod_run's. A receipt for another volume "
        "is refused rather than used",
    )

    export = verbs.add_parser(
        "export", help="copy the recorded base Armarium evidence locally and print reconciliation"
    )
    export.add_argument("--run-id", help="the explicitly recorded run to export")
    export.add_argument(
        "--run-root",
        type=Path,
        help=(
            "the folder containing the run tree, needed only when --run-id names more than "
            "one recorded run root (an ambiguous name Verbatus refuses to guess between)"
        ),
    )

    close = verbs.add_parser(
        "close", help="record a typed confirmation, then verify close and captured cost"
    )
    close.add_argument(
        "--pod-id", help="the recorded fixture pod id, if you want to repeat it explicitly"
    )

    verbs.add_parser("status", help="read saved receipts only; it never contacts a provider")
    spend = verbs.add_parser(
        "spend", help="show the reviewed spend floor, saved balance observations, and alert history"
    )
    spend.add_argument("view", choices=("show",), help="the read-only spend view")
    spend.add_argument(
        "--policy", type=Path, help="reviewed spend policy (defaults to config/spend.toml)"
    )
    review = verbs.add_parser(
        "review",
        help="open one run tree read-only; it cannot contact a provider or change evidence",
    )
    review.add_argument(
        "--run-root", type=Path, required=True, help="folder containing the run tree"
    )
    review.add_argument("--run-id", required=True, help="the sealed run to inspect")
    review.add_argument(
        "--json",
        action="store_true",
        help=(
            "print the whole projection as JSON instead of the plain-language view; the "
            "plain view is the same projection read out in words"
        ),
    )
    advance = verbs.add_parser(
        "advance",
        help="append the project lead's confirmed decision to pass one exact sealed stage boundary",
    )
    advance.add_argument(
        "--run-root", type=Path, required=True, help="folder containing the run tree"
    )
    advance.add_argument("--run-id", required=True, help="the sealed run to advance")
    advance.add_argument("--stage", required=True, help="the sealed stage boundary to pass")
    advance.add_argument(
        "--reason", required=True, help="why the project lead chose to advance this boundary"
    )
    advance.add_argument(
        "--mode",
        choices=RUN_MODES,
        default="manual",
        help=(
            "the run mode this confirmation is declared under, which decides whether "
            "--stage can require a person-held advance at all: 'manual' holds the one "
            "named stage, 'semi' the inclusive --from-stage/--to-stage range, and "
            "'auto' passes its selected boundaries without a person-held record except "
            "the boundaries where a run can stop in every mode"
        ),
    )
    advance.add_argument("--from-stage", help="first stage of the inclusive semi-mode range")
    advance.add_argument("--to-stage", help="last stage of the inclusive semi-mode range")
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
    triage = verbs.add_parser(
        "triage", help="declare a batch mode and walk its offline review queue"
    )
    triage.add_argument(
        "--manifest", type=Path, required=True, help="producer decision manifest JSON"
    )
    triage.add_argument(
        "--evidence", type=Path, required=True, help="candidate evidence JSON {records}"
    )
    triage.add_argument("--proxy-paths", type=Path, required=True, help="digest-to-proxy-path JSON")
    triage.add_argument("--mode", required=True, choices=("manual", "semi", "auto"))
    triage.add_argument("--batch-id", required=True)
    triage.add_argument("--operator", required=True)
    triage.add_argument("--mode-record", type=Path, required=True)
    triage.add_argument("--queue-state", type=Path, help="append-only decision journal")
    triage_decision = triage.add_mutually_exclusive_group()
    triage_decision.add_argument(
        "--decline", metavar="ITEM_SHA256", help="record a visible decline"
    )
    triage_decision.add_argument(
        "--accept", metavar="ITEM_SHA256", help="accept this cluster candidate"
    )
    triage.add_argument(
        "--draft", type=Path, help="canonical confirmation draft shown to the operator"
    )
    triage.add_argument("--confirmation-out", type=Path, help="producer confirmation-file target")
    triage.add_argument("--preview-sha256", help="digest of the draft shown before writing")
    scantailor = verbs.add_parser(
        "scantailor",
        help="name the separate ScanTailor desktop handoff, then import its saved geometry",
    )
    scantailor.add_argument(
        "--project", type=Path, required=True, help="saved ScanTailor Advanced project XML"
    )
    scantailor.add_argument(
        "--geometry-out",
        type=Path,
        help="existing folder to receive the immutable imported geometry document",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    state: Path | None = None
    try:
        parser = build_parser()
        if not arguments:
            # Inside the try, not before it: a Ctrl+C at this prompt still
            # reaches the same three-part contract as every other failure.
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
        _require_workspace_checkout(workspace, args)
        _warn_about_abandoned_state_dir(workspace, using_default=not explicit_state)
        surface = OperatorSurface(
            workspace,
            state,
            notifier=notify_bridge.shell_notifier() if args.notify else notify_bridge.silent,
        )
        volume = _network_volume(getattr(args, "network_volume", None), verb=args.verb)
        if volume is None:
            _print("Verbatus is in offline rehearsal mode. It will not contact a cloud provider.")
        else:
            _print("Verbatus will not start, adopt or close any pod: that stays offline.")
            if args.verb == "fetch-run":
                _print(f"You asked it to read a run tree from {volume.describe()}.")
            else:
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
                surface.upload(
                    args.source,
                    sealed_manifest=args.sealed_manifest,
                    prefix=args.prefix,
                    volume=volume,
                )
            else:
                surface.submit_and_upload(
                    args.source,
                    manifest_out=args.manifest_out,
                    policy_path=args.policy,
                    prefix=args.prefix,
                    volume=volume,
                )
        elif args.verb == "ingest":
            ingest_in_custody(
                source=args.source,
                output_dir=args.output_dir,
                policy_path=args.policy,
                corpus_id=args.corpus_id,
                mode=args.mode,
                confirmation_file=args.confirmation_file,
                workspace=workspace,
                printer=_print,
            )
        elif args.verb == "run":
            surface.run(
                run_id=args.run_id,
                scenario=args.scenario,
                fixture=args.fixture,
                submission_folder=args.submission_folder,
                submission_manifest=args.submission_manifest,
                data_gate_policy=args.data_gate_policy,
                models_config=args.models_config,
                serving_recipes_config=args.serving_recipes_config,
                witness_context_config=args.witness_context_config,
            )
        elif args.verb == "fetch-run":
            derived = _derived_evidence_keys(args.launch_receipt, volume, args.run_id)
            for key in derived:
                _print(f"Derived from the launch receipt: --evidence-key {key}")
            evidence_keys = tuple(dict.fromkeys((*(args.evidence_key or ()), *derived)))
            derived_prefixes = _derived_evidence_prefixes(args.launch_receipt, volume, args.run_id)
            for prefix in derived_prefixes:
                _print(f"Derived from the launch receipt: --evidence-prefix {prefix}")
            fetch_arguments: dict[str, object] = {
                "run_id": args.run_id,
                "into": args.into,
                "volume": volume,
                "evidence_keys": evidence_keys,
            }
            # Passed only when named or derived, so the surface's own default
            # (the whole preflight/ tree) applies when neither is.
            evidence_prefixes = tuple(
                dict.fromkeys((*(args.evidence_prefix or ()), *derived_prefixes))
            )
            if evidence_prefixes:
                fetch_arguments["evidence_prefixes"] = evidence_prefixes
            surface.fetch_run(**fetch_arguments)  # type: ignore[arg-type]
        elif args.verb == "export":
            surface.export(run_id=args.run_id, run_root=args.run_root)
        elif args.verb == "close":
            prepared_close = surface.prepare_close(pod_id=args.pod_id)
            confirmation = _typed_close_confirmation(prepared_close.phrase)
            surface.close(prepared_close, confirmation)
        elif args.verb == "status":
            surface.status()
        elif args.verb == "spend":
            policy = args.policy or workspace / "config" / "spend.toml"
            for line in SpendSurface(surface.receipts, surface.now()).show(policy):
                _print(line)
        elif args.verb == "review":
            _review_in_custody(args.run_root, args.run_id, workspace, raw=args.json)
        elif args.verb == "advance":
            _advance_with_confirmation(
                args.run_root,
                args.run_id,
                args.stage,
                reason=args.reason,
                workspace=workspace,
                surface=surface,
                mode=args.mode,
                from_stage=args.from_stage,
                to_stage=args.to_stage,
            )
        elif args.verb == "backup":
            _backup_in_custody(args.run_root, args.run_id, args.mac_directory, workspace, surface)
        elif args.verb == "triage":
            _triage_queue(args, workspace)
        elif args.verb == "scantailor":
            from .scantailor import import_in_custody, instruction

            if args.geometry_out is not None:
                _print(
                    "Importing the ScanTailor project as it is saved right now. "
                    "If you have not yet saved its page-split geometry, stop and do that first."
                )
                import_in_custody(
                    project=args.project,
                    output_dir=args.geometry_out,
                    workspace=workspace,
                    printer=_print,
                )
            else:
                _print(instruction(args.project, workspace=workspace))
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
    except Exception as error:
        _print(record_unexpected(error, arguments, state).render())
        return 2
    return 0


def _bound_run_tree(run_tree_class, run_root: Path, run_id: str):
    """Bind one run before any verb acts on it, and name a bad id as a bad id.

    `RunTree.__init__` validates the run id and refuses one that resolves
    outside the run root, both as `ContractError`, caught here as
    `INVALID_COMMAND` rather than a verb-specific refusal: the instruction
    named no run this tool could bind, so nothing was read and nothing
    changed, unlike a tree-unreadable refusal that would send the operator to
    preserve and investigate evidence that was never opened.
    """

    from common.contracts.errors import ContractError

    try:
        return run_tree_class(Path(run_root).resolve(), run_id)
    except (ContractError, OSError) as error:
        raise OperatorError(ErrorCode.INVALID_COMMAND, detail=str(error)) from error


def _review_in_custody(run_root: Path, run_id: str, workspace: Path, *, raw: bool = False) -> None:
    """Exec the renderer with no credential and a kernel-enforced no-write policy.

    The run tree is opened once, read-only, by the parent, and the child
    receives only the resulting immutable JSON value stream, never a
    run-tree path or object; a compromised child can deceive its viewer
    about those bytes but cannot reopen the evidence or reach any
    pipeline/provider module. The parent reads the child's JSON out in plain
    language (`review_text.render`), or prints it as-is when `raw` is asked
    for.
    """

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
            # A platform-enforcement refusal: the launcher never exec'd the
            # console, so this is not a claim the run tree is unreadable.
            raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=launcher)
        if completed.returncode == console.PROJECTION_UNREADABLE_EXIT:
            # The console never opened the run tree here either.
            raise OperatorError(
                ErrorCode.CONSOLE_PROJECTION_UNREADABLE,
                detail=completed.stderr or completed.stdout,
            )
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail=completed.stdout or completed.stderr
        )
    if raw:
        _print(completed.stdout.rstrip())
        return
    try:
        returned = json.loads(completed.stdout)
    except ValueError as error:
        # A fault of this tool's pipe, not a claim about the run tree.
        raise OperatorError(
            ErrorCode.CONSOLE_PROJECTION_UNREADABLE,
            detail=(
                f"the console returned text that is not the projection JSON "
                f"({type(error).__name__}); the run tree itself is not in question"
            ),
        ) from error
    if not isinstance(returned, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_PROJECTION_UNREADABLE,
            detail="the console returned JSON that is not a projection object",
        )
    try:
        lines = review_text.render(returned)
    except review_text.ProjectionShapeError as error:
        raise OperatorError(
            ErrorCode.CONSOLE_PROJECTION_UNREADABLE,
            detail=f"the console returned a projection this tool cannot read out: {error}",
        ) from error
    for line in lines:
        _print(line)


def _backup_in_custody(
    run_root: Path,
    run_id: str,
    mac_directory: Path,
    _workspace: Path,
    surface: OperatorSurface,
) -> None:
    """Copy evidence only in the no-network, credential-free custody child.

    ``surface`` records the operator's own receipt of the attempt: which run
    root was copied where, with what snapshot, or why it was refused; without
    it, `status` could not say a backup had ever happened.
    """

    from .backup import (
        BackupRefusal,
        BackupReport,
        destination_identities,
        prepare_backup_layout,
        required_identity,
        resolve_backup_paths,
        verify_backup_snapshot,
    )

    facts = {
        "run_id": run_id,
        "run_root": str(Path(run_root).absolute()),
        "mac_directory": str(Path(mac_directory).absolute()),
    }
    # The parent rejects overlap before creating the layout, since custody
    # grants the child publication rights but withholds directory creation:
    # unchecked, setup could write `objects/`/`snapshots/` inside the source.
    try:
        source, destination = resolve_backup_paths(run_root, run_id, mac_directory)
        prepare_backup_layout(source, destination)
        source_identity = required_identity(source, what="source run tree")
        destination_identity = destination_identities(destination)
    except BackupRefusal as refusal:
        surface.record_backup(state="refused", facts=facts, detail=str(refusal))
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=str(refusal)) from refusal
    # `--workspace` selects project data for other verbs; it is not authority
    # to replace this custody worker's code, so its root is pinned here
    # rather than taken from a caller-nominated path.
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
        surface.record_backup(state="worker-failed", facts=facts, detail=detail)
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
        surface.record_backup(state="unverified", facts=facts, detail=str(error))
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=str(error)) from error
    receipt = surface.record_backup(state="complete", facts=facts, report=report.to_record())
    _print(
        "Mac backup complete: "
        f"{report.copied} copied, {report.reused} reused; snapshot {report.snapshot_sha256}."
    )
    _print(f"Saved backup receipt: {receipt}")


def _triage_queue(args: argparse.Namespace, workspace: Path) -> None:
    """Render paths and evidence; the console never opens a master or chooses a link."""
    from . import triage

    try:
        # Completeness is checked before anything durable: `write_mode_declaration`
        # refuses to rewrite a batch's declared mode once it exists, so a
        # decision that wrote the mode record and then refused would claim
        # that batch's mode for a command that did nothing further.
        if args.decline is not None and args.queue_state is None:
            raise triage.TriageRefusal(
                "triage refusal queue-state-required: decline needs --queue-state"
            )
        # --draft, --confirmation-out and --preview-sha256 belong to --accept
        # alone; a decline row is never rewritten, so silently ignoring them
        # here would decide the item for good and strand an acceptance the
        # operator was plainly assembling.
        if args.decline is not None and any(
            (args.draft, args.confirmation_out, args.preview_sha256)
        ):
            raise triage.TriageRefusal(
                "triage refusal decline-with-acceptance-arguments: --draft, "
                "--confirmation-out and --preview-sha256 belong to --accept; a decline "
                "would ignore them and decide this row for good"
            )
        # The reverse of the check above: acceptance companions with no
        # decision word would otherwise print the queue and exit 0 as though
        # nothing needed deciding. `--queue-state` is absent from this list,
        # since reading the journal alongside the rendered queue is a
        # legitimate display run.
        if (
            args.accept is None
            and args.decline is None
            and any((args.draft, args.confirmation_out, args.preview_sha256))
        ):
            raise triage.TriageRefusal(
                "triage refusal decision-word-required: --draft, --confirmation-out and "
                "--preview-sha256 belong to an --accept or a --decline, and none was given"
            )
        if args.accept is not None:
            if args.queue_state is None or args.draft is None or args.confirmation_out is None:
                raise triage.TriageRefusal(
                    "triage refusal acceptance-incomplete: accept needs --queue-state, --draft, and --confirmation-out"
                )
            if args.preview_sha256 is None:
                raise triage.TriageRefusal(
                    "triage refusal preview-confirmation-required: accept needs the shown preview digest"
                )
        declaration = triage.declare_mode(args.mode, batch_id=args.batch_id, operator=args.operator)
        # The queue loads before the declaration is persisted: `load_queue`
        # refuses an unreadable or non-canonical manifest, evidence or proxy
        # map, and nothing durable should happen until the batch is known to
        # load.
        queue = triage.load_queue(
            args.manifest,
            args.evidence,
            args.proxy_paths,
            mode=args.mode,
            batch_id=args.batch_id,
            operator=args.operator,
            triage_modes_path=workspace / "config" / "triage_modes.toml",
        )
        triage.write_mode_declaration(args.mode_record, declaration)
        if args.decline is not None:
            triage.append_decision(
                args.queue_state, queue, item_digest=args.decline, decision="decline"
            )
        if args.accept is not None:
            draft = triage.load_confirmation_draft(args.draft)
            triage.accept_candidate(
                args.queue_state,
                queue,
                item_digest=args.accept,
                draft=draft,
                confirmation_path=args.confirmation_out,
                preview_sha256=args.preview_sha256,
            )
        _print(json.dumps(queue, sort_keys=True))
    except triage.TriageRefusal as error:
        raise OperatorError(ErrorCode.TRIAGE_REFUSED, detail=str(error)) from error


def _advance_with_confirmation(
    run_root: Path,
    run_id: str,
    stage: str,
    *,
    reason: str,
    workspace: Path,
    surface: OperatorSurface | None = None,
    mode: str = "manual",
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> None:
    """Bind a human confirmation to one observed digest, then launch the worker."""

    from common.contracts.errors import ApprovalRefusal
    from common.runtree.store import RunTree

    tree = _bound_run_tree(RunTree, run_root, run_id)
    try:
        # Stored boundary facts are gathered before the declared mode is
        # validated, so an invalid range is refused before anything is
        # presented as evidence.
        boundary_states: list[dict[str, object] | None] = []
        for candidate in STAGES:
            try:
                candidate_summary = boundary_summary(tree, candidate)
            except UnsealedBoundaryRefusal:
                boundary_states.append(None)
            else:
                boundary_states.append(candidate_summary)
        first_gap: str | None = None
        for candidate, state in zip(STAGES, boundary_states, strict=True):
            if state is None and first_gap is None:
                first_gap = candidate
            elif state is not None and first_gap is not None:
                raise ApprovalRefusal(
                    f"advance refuses the stored boundary chain: {first_gap} has no "
                    f"completion seal although later stage {candidate} is sealed; earlier evidence "
                    "is missing, not merely unfinished"
                )
        _print("Current boundary state (pipeline order; not a recommendation):")
        for candidate, candidate_summary in zip(STAGES, boundary_states, strict=True):
            if candidate_summary is None:
                _print(f"- {candidate}: no stored completion seal")
            else:
                _print(
                    f"- {candidate}: seal {candidate_summary['seal_digest']}; "
                    f"attempt {candidate_summary['attempt_ordinal']}; "
                    f"census {json.dumps(candidate_summary['census'], sort_keys=True)}"
                )
        held = held_boundaries_for_mode(mode, stage=stage, from_stage=from_stage, to_stage=to_stage)
        # Named as the operator's own declaration: nothing in the run tree
        # records how the pipeline was invoked, so this is not checked
        # against evidence and must not be presented as though it were.
        _print(f"Staged invocation mode, as you declared it: {mode}.")
        _print("The run tree records no invocation mode, so nothing here checks that claim.")
        if mode == "auto":
            _print("Auto mode runs every stage and passes every boundary it does not hold.")
        elif mode == "semi":
            _print(
                f"Semi mode runs the inclusive range {from_stage} through {to_stage} "
                "and passes every boundary in it that it does not hold."
            )
        else:
            _print(f"Manual mode runs {stage} alone and passes nothing.")
        _print(
            "This declared selection can require a person-held advance at: "
            f"{', '.join(sorted(held))}."
        )
        summary = boundary_summary(tree, stage)
        digest = summary["seal_digest"]
    except (ApprovalRefusal, OSError) as error:
        # `OSError` also covers a closed or broken stdout raised out of
        # `_print` while this block prints the boundary chain; uncaught,
        # either would reach the catch-all as an unexpected error rather than
        # a plain refusal to advance. `advance.trigger_advance` guards its
        # own printing the same way.
        raise OperatorError(ErrorCode.ADVANCE_REFUSED, detail=str(error)) from error
    _print("Sealed evidence summary:")
    _print(f"- seal digest: {summary['seal_digest']}")
    _print(f"- configuration digest: {summary['config_digest']}")
    _print(f"- artifact inventory digest: {summary['artifact_inventory']}")
    _print(f"- blob inventory digest: {summary['blob_inventory']}")
    _print(f"- outcome census: {json.dumps(summary['census'], sort_keys=True)}")
    phrase = (
        f"advance {run_id} past {stage} at {digest} "
        f"for reason {json.dumps(reason, ensure_ascii=True)}"
    )
    _print(f"The current {stage} boundary has seal digest {digest}.")
    confirmation = _typed_advance_confirmation(phrase)
    if confirmation != phrase:
        raise OperatorError(
            ErrorCode.ADVANCE_REFUSED,
            detail=(
                "the typed confirmation did not exactly name this run, stage, seal digest, "
                "and recorded reason"
            ),
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
    if surface is not None:
        # Lets `status` find the approval record above again; `surface` is
        # optional since most of this function's own tests exercise the
        # confirmation/boundary-selection logic without one.
        surface.record_advance(
            run_id=run_id,
            run_root=run_root,
            stage=stage,
            reason=reason,
            seal_digest=digest,
            reference=reference,
        )


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
        "container_disk_gb",
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
            # Absent falls back to the reviewed default, not the provider's.
            container_disk_gb=raw.get("container_disk_gb", DEFAULT_CONTAINER_DISK_GB),
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


def _network_volume(value: str | None, *, verb: str) -> VolumeSpec | None:
    """Read `DATACENTER:VOLUME_ID` without letting a typo become a raw traceback.

    The error code names the verb this parse is for: `UPLOAD_VOLUME_UNAVAILABLE`
    advises re-running `verbatus upload`, which is wrong for a `fetch-run`
    operator who asked to read a run tree home, not send one, so that verb
    gets `FETCH_RUN_FAILED` instead.
    """

    if value is None:
        return None
    code = (
        ErrorCode.FETCH_RUN_FAILED if verb == "fetch-run" else ErrorCode.UPLOAD_VOLUME_UNAVAILABLE
    )
    datacenter, separator, volume_id = value.partition(":")
    if not separator or not datacenter or not volume_id:
        raise OperatorError(
            code,
            detail="a network volume is written as DATACENTER:VOLUME_ID, for example EU-CZ-1:abc123",
        )
    try:
        return VolumeSpec(datacenter_id=datacenter, volume_id=volume_id)
    except VolumeTransferRefusal as error:
        raise OperatorError(code, detail=str(error)) from error


def _interactive_arguments() -> list[str]:
    """The double-click route asks for a word and the smallest needed facts."""

    _print("Verbatus")
    _print(
        "Choose one word: ingest, triage, scantailor, launch, boot, upload, run, fetch-run, export, close, status, spend, review, advance, or backup."
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
    if verb == "ingest":
        source = _ask("Folder containing the submitted master files")
        output_dir = _ask("Existing empty approved folder for the ready-to-submit records")
        policy = _ask("Reviewed data-handling policy (leave blank for the project default)")
        corpus_id = _ask("Corpus ID")
        # Names the three legal words, since a typo would otherwise reach
        # argparse's `choices` and answer with "invalid choice" alone.
        mode = _ask("Triage mode — manual, semi, or auto", default="auto")
        confirmation = _ask(
            "Canonical cluster confirmation file (leave blank when confirming no cluster)"
        )
        if not source or not output_dir or not corpus_id:
            _print(
                "Ingest needs a submitted folder, an empty approved output folder, and a corpus ID. "
                "One was left blank, so nothing changed."
            )
            return []
        arguments = [
            "ingest",
            "--source",
            source,
            "--output-dir",
            output_dir,
            "--corpus-id",
            corpus_id,
            "--mode",
            mode,
        ]
        if confirmation:
            arguments.extend(("--confirmation-file", confirmation))
        if policy:
            arguments.extend(("--policy", policy))
        return arguments
    if verb == "triage":
        manifest = _ask("Producer decision manifest JSON")
        evidence = _ask("Candidate evidence JSON")
        proxy_paths = _ask("Digest-to-proxy-path JSON")
        # No default: every triage invocation writes the batch's durable,
        # never-rewritten mode declaration, so defaulting to `semi` would let
        # someone who chose `triage` merely to look at the queue permanently
        # claim that batch's mode by accident.
        mode = _ask("Triage mode — manual, semi, or auto")
        batch_id = _ask("Batch ID")
        operator = _ask("Operator name for the mode record")
        mode_record = _ask("Path where the mode declaration should be recorded")
        if not all((manifest, evidence, proxy_paths, mode, batch_id, operator, mode_record)):
            _print(
                "Triage needs the manifest, evidence, proxy paths, mode, batch ID, operator, "
                "and mode-record path. One was left blank, so nothing changed."
            )
            return []
        # Display only, deliberately: acceptance is pinned to
        # `--preview-sha256`, the digest of a draft the operator has actually
        # seen, which a blind prompt chain cannot honestly produce. Decline
        # is withheld with it, and both are recorded at the command line
        # instead, where the digests are visible and checkable.
        return [
            "triage",
            "--manifest",
            manifest,
            "--evidence",
            evidence,
            "--proxy-paths",
            proxy_paths,
            "--mode",
            mode,
            "--batch-id",
            batch_id,
            "--operator",
            operator,
            "--mode-record",
            mode_record,
        ]
    if verb == "scantailor":
        project = _ask("Saved ScanTailor Advanced project XML")
        output = _ask(
            "Existing folder for the imported geometry document (leave blank for instructions only)"
        )
        if not project:
            _print(
                "ScanTailor needs its saved project file. It was left blank, so nothing changed."
            )
            return []
        arguments = ["scantailor", "--project", project]
        if output:
            arguments.extend(("--geometry-out", output))
        return arguments
    if verb == "run":
        run_id = _ask("A short name for this run", default="dry-run")
        return ["run", "--run-id", run_id]
    if verb == "fetch-run":
        run_id = _ask("The run ID pod_run wrote on the volume")
        into = _ask("Local folder to bring the run tree into")
        volume = _ask("Network volume, as DATACENTER:VOLUME_ID")
        if not run_id or not into or not volume:
            _print(
                "Fetch-run needs a run ID, a local folder and a network volume. Nothing changed."
            )
            return []
        arguments = ["fetch-run", "--run-id", run_id, "--into", into, "--network-volume", volume]
        # The launch receipt the operator's machine wrote names every
        # launch-token-bound report path, so asking for it first derives
        # every key and skips typing a 32-hex token by hand; the per-record
        # prompts below are the fallback when the receipt is not to hand.
        receipt = _ask("Saved launch receipt for this run (leave blank to name keys by hand)")
        if receipt:
            arguments.extend(("--launch-receipt", receipt))
        else:
            # Each stays "leave blank to skip": a key naming a record this
            # launch never wrote comes back as a per-object refusal, not a
            # command failure.
            for label in (
                "Volume key for the bootstrap report (leave blank to skip)",
                "Volume key for the pod-run report (leave blank to skip)",
                "Volume key for the pod-run '-hold' liveness report, the pod-run key with "
                "'-hold' before its suffix (leave blank to skip)",
                "Volume key for the pod-run '-liveness' tick, the pod-run key with "
                "'-liveness' before its suffix (leave blank to skip)",
                "Volume key for the pod-run '-timings' stage journal, the pod-run key with "
                "'-timings' before its suffix (leave blank to skip)",
                "Volume key for the pod-run '-transcript.log' orchestrator transcript, the "
                "pod-run key with '-transcript' before its suffix and a .log suffix "
                "(leave blank to skip)",
                "Volume key for the pod-timer runtime report (leave blank to skip)",
                "Volume key for the pod-timer '-terminating' breadcrumb, the pod-timer key "
                "with '-terminating' before its suffix (leave blank to skip)",
                "Volume key for the bootstrap journal (leave blank to skip)",
                "Volume key for the transfer journal, normally pod-transfer-journal.json at "
                "the volume root (leave blank to skip)",
            ):
                evidence_key = _ask(label)
                if evidence_key:
                    arguments.extend(("--evidence-key", evidence_key))
            # A volume is reused across launches, so preflight/ holds every
            # launch's tree with no way to say which measured this run's
            # chairs; asked here only in this no-receipt fallback, since
            # --launch-receipt derives the stem on its own otherwise.
            prefix = _ask(
                "This run's preflight stem, as preflight/<bootstrap report stem> "
                "(leave blank for every launch's preflight tree)"
            )
            if prefix:
                arguments.extend(("--evidence-prefix", prefix))
        return arguments
    if verb == "export":
        return ["export"]
    if verb == "close":
        return ["close"]
    if verb == "spend":
        policy = _ask("Reviewed spending-policy file (leave blank for config/spend.toml)")
        arguments = ["spend", "show"]
        if policy:
            arguments.extend(("--policy", policy))
        return arguments
    if verb in {"review", "advance", "backup"}:
        run_root = _ask("Folder containing the run tree")
        run_id = _ask("The sealed run ID")
        if not run_root or not run_id:
            _print(f"{verb.title()} needs both a run-tree folder and a run ID. Nothing changed.")
            return []
        arguments = [verb, "--run-root", run_root, "--run-id", run_id]
        if verb == "advance":
            # No `--help` on the double-click route, so every closed-value
            # prompt states the spellings its parser accepts.
            boundaries = ", ".join(STAGES)
            stage = _ask(f"The sealed stage boundary to pass — one of: {boundaries}")
            reason = _ask("Why this boundary should be advanced")
            mode = _ask(
                f"Staged invocation mode — one of: {', '.join(RUN_MODES)}", default="manual"
            )
            if not stage or not reason or not mode:
                _print("Advance needs a stage, reason, and staged mode. Nothing changed.")
                return []
            arguments.extend(("--stage", stage, "--reason", reason, "--mode", mode))
            if mode == "semi":
                from_stage = _ask(
                    f"First stage in the inclusive semi-mode range — one of: {boundaries}"
                )
                to_stage = _ask(
                    f"Last stage in the inclusive semi-mode range — one of: {boundaries}"
                )
                if not from_stage or not to_stage:
                    _print("Semi advance needs both range endpoints. Nothing changed.")
                    return []
                arguments.extend(("--from-stage", from_stage, "--to-stage", to_stage))
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
            "This appends the project lead's decision record; it does not edit evidence or start a provider.\n"
            f"Type this line exactly, with no quotation marks:\n{phrase}\n> "
        )
    except EOFError:
        return None


if __name__ == "__main__":  # pragma: no cover - console wrapper
    from .entry import main as entry_main

    raise SystemExit(entry_main())
