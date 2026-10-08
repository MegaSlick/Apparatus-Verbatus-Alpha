"""The ``verbatus`` command: one plain word at a time, with no raw tracebacks."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import sys
import time
import traceback
from pathlib import Path
from typing import Final, Sequence

from common.checkout import missing_checkout_resources
from common.contracts.approval import FINDINGS, REVIEW_DECISIONS
from common.contracts.stages import STAGES
from common.stage import RUN_MODES
from operations.pod.launch import launch_evidence_keys, launch_evidence_prefixes, launch_run_id
from operations.pod.transfer import normalize_transfer_prefix

from . import notify_bridge, review_text
from . import spend as spend_view
from . import watch as watch_view
from .advance import (
    UnsealedBoundaryRefusal,
    boundary_summary,
    held_boundaries_for_mode,
    trigger_advance,
)
from .errors import ErrorCode, OperatorError, strip_control_bytes
from .ingest import ingest
from .prepare import prepare as prepare_pages
from .records import DescriptorStore, ReceiptStore
from .review import ReadOnlyRun
from .surface import DEFAULT_FIXTURE, RESUME_TO_STAGE, OperatorSurface, bounded_tail
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
    "ingest": ("config",),
    "triage": ("config",),
    "spend": ("config",),
}


def _checkout_resources_read(args: argparse.Namespace) -> tuple[str, ...]:
    """The checkout directories this exact invocation will read from its workspace."""

    needed = _CHECKOUT_RESOURCES_BY_VERB.get(args.verb, ())
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


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number above zero")
    return value


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
    """Name the fix for the one unrecognized-arguments cause this is.

    `message` is argparse's own wording, not this codebase's -- matched by
    prefix rather than parsed, so a wording this function does not
    recognize still reaches the operator unmodified instead of being misread.
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
        description="The Apparatus Verbatus operator, one plain word at a time.",
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
        # `None` tells `main` none was named, so it resolves the default against the
        # workspace; scanning raw argv would miss abbreviations like `--state-di`.
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

    upload = verbs.add_parser(
        "upload", help="seal or reuse a submission record, then transfer with zero GPU-hours"
    )
    upload.add_argument(
        "--source", type=Path, required=True, help="folder containing the submitted files"
    )

    reuse = upload.add_mutually_exclusive_group(required=True)
    reuse.add_argument("--sealed-manifest", type=Path, help="existing sealed submission record")
    reuse.add_argument(
        "--manifest-out",
        type=Path,
        help="where to write a new sealed submission record",
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

    upload.add_argument(
        "--triage-decision-manifest",
        type=Path,
        help="a triage decision manifest, such as `verbatus prepare` writes, to send beside "
        "the scans; it must hold a row for every sealed file",
    )
    upload.add_argument(
        "--triage-producer-recipe",
        type=Path,
        help="the producer recipe written beside that triage decision manifest",
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
        help="canonical cluster-confirmation file; omit when confirming no cluster",
    )

    prepare = verbs.add_parser(
        "prepare",
        help="prepare page images from a folder of scans, and the triage manifest that has "
        "the Door cut the same pages from the original scans",
    )
    prepare.add_argument("--scans", type=Path, required=True, help="folder of scans; only read")
    prepare.add_argument(
        "--out",
        type=Path,
        required=True,
        help="folder for the prepared pages and the triage manifest; a second run over it "
        "continues the same project and keeps every correction",
    )
    prepare.add_argument(
        "--overrides", type=Path, help="a pagekit-overrides.v1 file of corrections to apply"
    )
    prepare.add_argument(
        "--crop",
        choices=("none", "page", "content"),
        help="crop pages: none (pagekit's default, each page its whole levelled side of the "
        "cut), page (to the page box) or content (to the writing plus a margin)",
    )
    cache = prepare.add_mutually_exclusive_group()
    cache.add_argument(
        "--cache",
        type=Path,
        help="where pagekit's stage cache goes (default: pagekit-cache beside --out); never "
        "inside the scans folder",
    )
    cache.add_argument("--no-cache", action="store_true", help="write no stage cache")
    prepare.add_argument(
        "--corpus-id",
        help="corpus identity for the triage manifest (default: the scans folder's name)",
    )

    run = verbs.add_parser("run", help="run or resume a recorded fixture or real submission")
    run.add_argument("--run-id", required=True, help="a short name for this run")
    run.add_argument(
        "--from",
        dest="from_stage",
        choices=STAGES,
        help="resume only from this stage (with --to), e.g. recensor after a review decision",
    )
    run.add_argument(
        "--to",
        dest="to_stage",
        choices=(RESUME_TO_STAGE,),
        help="the last stage of the --from range: always armarium",
    )
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
        "--triage-decision-manifest",
        type=Path,
        help="triage decisions for the real folder, such as the one `verbatus prepare` writes: "
        "the Door cuts each page from its original scan as they say",
    )
    run.add_argument(
        "--triage-producer-recipe",
        type=Path,
        help="the producer recipe written beside that triage decision manifest",
    )
    run.add_argument(
        "--models-config",
        type=Path,
        help="the chair roster to seal into this run (config/models-real.toml for the real "
        "chairs); always supplied with --serving-recipes-config",
    )
    run.add_argument(
        "--serving-recipes-config",
        type=Path,
        help="the serving catalogue the roster's chairs are served under "
        "(config/serving_recipes_real.toml with the real roster); always supplied with "
        "--models-config",
    )

    fetch_run = verbs.add_parser(
        "fetch-run",
        help="bring one run tree back from the network volume, every object digest-checked",
    )
    fetch_run.add_argument("--run-id", required=True, help="the run pod_run wrote on the volume")
    fetch_run.add_argument(
        "--canary-root", type=Path, help="private canary references and verdicts"
    )
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

    verbs.add_parser("status", help="read saved receipts only; it never contacts a provider")
    watch = verbs.add_parser(
        "watch",
        help="follow a pod run from saved copies of its report files; it writes nothing and "
        "contacts no provider or volume",
    )
    watch.add_argument("--run-id", required=True, help="the run pod_run is running")
    where = watch.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--receipts",
        type=Path,
        metavar="FOLDER",
        help="the folder holding copies of pod-run-report-<run id>.json and its -liveness, "
        "-timings, -estimate and -progress siblings (the hand route's names)",
    )
    where.add_argument(
        "--report", type=Path, help="the copy of the pod-run report, when it has another name"
    )
    watch.add_argument(
        "--lease",
        type=Path,
        help="the pod's saved lease, for its creation time and hourly rates; without it spend "
        "is counted from pod_run's start and its --hourly-usd",
    )
    watch.add_argument(
        "--stale-minutes",
        type=_positive_int,
        default=watch_view.STALE_MINUTES_DEFAULT,
        help="call the copies stale when the newest pod record is older than this "
        f"(default {watch_view.STALE_MINUTES_DEFAULT})",
    )
    watch.add_argument(
        "--interval",
        type=_positive_int,
        metavar="SECONDS",
        help="read again every this many seconds and show each change, until the run ends; "
        "without it, show once",
    )
    watch.add_argument(
        "--timeout",
        type=_positive_int,
        metavar="SECONDS",
        help="with --interval, stop after this long even if the run is still going",
    )
    spend = verbs.add_parser(
        "spend", help="show the reviewed spending policy's ceilings, floor and alert threshold"
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
        "--review-page", type=int, default=1, help="review queue page (1-based, 500 items per page)"
    )
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
    decide = verbs.add_parser(
        "decide",
        help=(
            "record the project lead's confirmed review decision about one held unit or page; "
            "the Recensor applies it when the run resumes from it"
        ),
    )
    decide.add_argument(
        "--run-root", type=Path, required=True, help="folder containing the run tree"
    )
    decide.add_argument("--run-id", required=True, help="the run whose review is decided")
    decide.add_argument(
        "decision",
        choices=sorted({word for words in REVIEW_DECISIONS.values() for word in words}),
        help=(
            "release (send a held unit to export, overriding its reading's own holds), "
            "edit (correct a held unit's text, from --text-file), "
            "exclude (not an act), hold (with --finding), re-ask (send it through the "
            "Perlector again); for a page: no-missed-act (release its page holds), missed-act, "
            "re-ask, re-shoot, hold"
        ),
    )
    subject = decide.add_mutually_exclusive_group(required=True)
    subject.add_argument("--unit", help="the unit's key, as review shows it (for example p1:2)")
    subject.add_argument("--page", type=int, help="the page's ordinal, for a page decision")
    decide.add_argument("--finding", choices=FINDINGS, help="what a hold names")
    decide.add_argument(
        "--text-file",
        type=Path,
        help=(
            "for an edit: a UTF-8 file holding the corrected text exactly; one final line "
            "ending is not part of it"
        ),
    )
    decide.add_argument("--note", help="for an edit: an optional note that travels with it")
    decide.add_argument("--reason", required=True, help="why the project lead decided this")
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
    clear = verbs.add_parser(
        "clear-leftovers",
        help="list, or with --apply remove, what interrupted publications left under a folder",
    )
    clear.add_argument(
        "--root", type=Path, required=True, help="run tree, volume mount or export folder"
    )
    clear.add_argument("--apply", action="store_true", help="remove them instead of listing")
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
            _print("Verbatus works on this computer. It will not contact a cloud provider.")
        else:
            _print("Verbatus never starts, adopts or closes a pod.")
            if args.verb == "fetch-run":
                _print(f"You asked it to read a run tree from {volume.describe()}.")
            else:
                _print(f"You asked it to send files to {volume.describe()}.")
        if args.verb == "upload":
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
                    **_triage_upload(args),
                )
            else:
                surface.submit_and_upload(
                    args.source,
                    manifest_out=args.manifest_out,
                    policy_path=args.policy,
                    prefix=args.prefix,
                    volume=volume,
                    **_triage_upload(args),
                )
        elif args.verb == "ingest":
            ingest(
                source=args.source,
                output_dir=args.output_dir,
                policy_path=args.policy,
                corpus_id=args.corpus_id,
                mode=args.mode,
                confirmation_file=args.confirmation_file,
                workspace=workspace,
                printer=_print,
            )
        elif args.verb == "prepare":
            prepare_pages(
                scans=args.scans,
                out=args.out,
                overrides=args.overrides,
                corpus_id=args.corpus_id,
                workspace=workspace,
                printer=_print,
                state_dir=state,
                crop=args.crop,
                cache=args.cache,
                no_cache=args.no_cache,
            )
        elif args.verb == "run":
            surface.run(
                run_id=args.run_id,
                scenario=args.scenario,
                fixture=args.fixture,
                submission_folder=args.submission_folder,
                submission_manifest=args.submission_manifest,
                data_gate_policy=args.data_gate_policy,
                triage_decision_manifest=args.triage_decision_manifest,
                triage_producer_recipe=args.triage_producer_recipe,
                models_config=args.models_config,
                serving_recipes_config=args.serving_recipes_config,
                from_stage=args.from_stage,
                to_stage=args.to_stage,
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
                "canary_root": args.canary_root,
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
        elif args.verb == "status":
            surface.status()
        elif args.verb == "watch":
            if args.timeout is not None and args.interval is None:
                raise OperatorError(ErrorCode.INVALID_COMMAND, detail="--timeout needs --interval")
            report = args.report or watch_view.report_path_for(args.run_id, args.receipts)
            try:
                watch_view.watch(
                    args.run_id,
                    report,
                    lease_path=args.lease,
                    stale_minutes=args.stale_minutes,
                    interval=args.interval,
                    timeout=args.timeout,
                    printer=_print,
                )
            except KeyboardInterrupt:
                # Stopping a follow is the normal way out, not a failure.
                _print("Stopped watching; nothing was changed.")
        elif args.verb == "spend":
            policy = args.policy or workspace / "config" / "spend.toml"
            for line in spend_view.show(policy):
                _print(line)
        elif args.verb == "review":
            _review(args.run_root, args.run_id, raw=args.json, review_page=args.review_page)
        elif args.verb == "advance":
            _advance_with_confirmation(
                args.run_root,
                args.run_id,
                args.stage,
                reason=args.reason,
                surface=surface,
                mode=args.mode,
                from_stage=args.from_stage,
                to_stage=args.to_stage,
            )
        elif args.verb == "decide":
            _decide_with_confirmation(
                args.run_root,
                args.run_id,
                args.decision,
                unit=args.unit,
                page=args.page,
                finding=args.finding,
                reason=args.reason,
                text_file=args.text_file,
                note=args.note,
            )
        elif args.verb == "backup":
            _backup(args.run_root, args.run_id, args.mac_directory, surface)
        elif args.verb == "triage":
            _triage_queue(args, workspace)
        elif args.verb == "clear-leftovers":
            _clear_leftovers(args.root, apply=args.apply)
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


# Exact names from mkstemp/mkdtemp (8 chars), surface's token_hex(16), and a clear's quarantine.
_CLEARING = r"(?:\.clearing-[0-9a-f]{8})?"
_TEMPORARY = re.compile(rf"\..+\.tmp-(?:[a-z0-9_]{{8}}|[0-9a-f]{{32}}){_CLEARING}")
_STAGING = re.compile(rf"\..+\.publishing-[a-z0-9_]{{8}}{_CLEARING}")
# Nothing holds a writer lock, so a leftover this fresh may still be in use.
LEFTOVER_QUIET_SECONDS: Final = 3600


def _raise(error: OSError) -> None:
    raise error


def _is_fresh(name: str, descriptor: int, *, own_ctime: bool) -> bool:
    details = os.lstat(name, dir_fd=descriptor)
    newest = max(details.st_mtime, details.st_ctime if own_ctime else 0)
    if stat.S_ISDIR(details.st_mode):
        for _, subdirectories, files, inner in os.fwalk(name, dir_fd=descriptor, onerror=_raise):
            for entry in (*subdirectories, *files):
                below = os.lstat(entry, dir_fd=inner)
                newest = max(newest, below.st_mtime, below.st_ctime)
    return time.time() - newest < LEFTOVER_QUIET_SECONDS


def _move_no_clobber(source: str, target: str, descriptor: int, *, link: bool) -> bool:
    """Move within one folder, or return False and move nothing when ``target`` is taken."""
    try:
        if link:
            os.link(source, target, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            os.unlink(source, dir_fd=descriptor)
            return True
        with contextlib.suppress(FileNotFoundError):
            os.lstat(target, dir_fd=descriptor)
            return False
        os.rename(source, target, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        return True
    except OSError as error:
        if error.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR, errno.EISDIR):
            return False
        raise


def _clear_one(name: str, descriptor: int, path: Path, *, folder: bool, apply: bool) -> bool:
    checked = os.lstat(name, dir_fd=descriptor)
    if not (stat.S_ISDIR if folder else stat.S_ISREG)(checked.st_mode):
        return False
    if _is_fresh(name, descriptor, own_ctime=True):
        _print(f"Left alone, changed within the last hour: {path}")
        return False
    if apply:
        # Moved aside so a racing publish's os.replace fails; the move sets its own ctime.
        base = re.sub(_CLEARING + "$", "", name)
        quarantine = f"{base}.clearing-{secrets.token_hex(4)}"
        if not _move_no_clobber(name, quarantine, descriptor, link=False):
            _print(f"Skipped, {quarantine} already exists: {path}")
            return False
        moved = os.lstat(quarantine, dir_fd=descriptor)
        if os.path.samestat(moved, checked) and not _is_fresh(
            quarantine, descriptor, own_ctime=False
        ):
            if folder:
                shutil.rmtree(quarantine, dir_fd=descriptor)
            else:
                os.unlink(quarantine, dir_fd=descriptor)
        elif _move_no_clobber(quarantine, name, descriptor, link=not folder):
            _print(f"Skipped, changed during the check: {path}")
            return False
        else:
            _print(f"Left as {quarantine} because {path} now exists")
            return False
    _print(f"{'Removed' if apply else 'Would remove'}: {path}")
    return True


def _clear_leftovers(root: Path, *, apply: bool) -> None:
    try:
        root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise OperatorError(
            ErrorCode.INVALID_COMMAND, detail=f"{root} is not a real folder: {error.strerror}"
        ) from error
    found = 0
    try:
        walk = os.fwalk(".", dir_fd=root_descriptor, onerror=_raise)
        for directory, subdirectories, files, descriptor in walk:
            staging = [name for name in subdirectories if _STAGING.fullmatch(name)]
            subdirectories[:] = [name for name in subdirectories if name not in staging]
            candidates = [(name, True) for name in staging]
            candidates += [(name, False) for name in files if _TEMPORARY.fullmatch(name)]
            for name, folder in candidates:
                path = Path(root, directory, name)
                try:
                    found += _clear_one(name, descriptor, path, folder=folder, apply=apply)
                except FileNotFoundError:
                    _print(f"Skipped, changed during the check: {path}")
    except OSError as error:
        raise OperatorError(ErrorCode.CLEAR_LEFTOVERS_STOPPED, detail=f"{root}: {error}") from error
    finally:
        os.close(root_descriptor)
    _print(
        f"{found} leftover(s): publication temporaries (.<name>.tmp-<id>) and export staging "
        f"folders (.<name>.publishing-<id>){'' if apply else '; add --apply to remove them'}."
    )


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


def _review(run_root: Path, run_id: str, *, raw: bool = False, review_page: int = 1) -> None:
    """Read the run tree once, read-only, and show it in plain language or as raw JSON."""

    from common.runtree.store import RunTree

    _bound_run_tree(RunTree, run_root, run_id)
    projection = dataclasses.asdict(
        ReadOnlyRun(run_root, run_id).projection(review_page=review_page)
    )
    if raw:
        _print(json.dumps(projection, sort_keys=True))
        return
    try:
        lines = review_text.render(projection)
    except review_text.ProjectionShapeError as error:
        raise OperatorError(ErrorCode.CONSOLE_PROJECTION_UNREADABLE, detail=str(error)) from error
    for line in lines:
        _print(line)


def _backup(run_root: Path, run_id: str, mac_directory: Path, surface: OperatorSurface) -> None:
    """Copy one run tree into the Mac directory, read the snapshot back, and record the attempt.

    ``surface`` records the operator's own receipt of the attempt: which run
    root was copied where, with what snapshot, or why it was refused; without
    it, `status` could not say a backup had ever happened.
    """

    from .backup import BackupRefusal, BackupUnverified, sync_run_tree

    facts = {
        "run_id": run_id,
        "run_root": str(Path(run_root).absolute()),
        "mac_directory": str(Path(mac_directory).absolute()),
    }
    try:
        report = sync_run_tree(Path(run_root).resolve(), run_id, mac_directory)
    except BackupUnverified as error:
        surface.record_backup(state="unverified", facts=facts, detail=str(error))
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=str(error)) from error
    except (BackupRefusal, OSError, ValueError, TypeError, RecursionError) as refusal:
        surface.record_backup(state="refused", facts=facts, detail=str(refusal))
        raise OperatorError(ErrorCode.BACKUP_FAILED, detail=str(refusal)) from refusal
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
    surface: OperatorSurface | None = None,
    mode: str = "manual",
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> None:
    """Bind a human confirmation to one observed digest, then record the advance."""

    from common.contracts.errors import ApprovalRefusal
    from common.runtree.store import RunTree

    tree = _bound_run_tree(RunTree, run_root, run_id)
    try:
        # Stored boundary facts are gathered and printed before the declared
        # mode is validated; only the mode claims below wait on that check.
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


def _decide_with_confirmation(
    run_root: Path,
    run_id: str,
    decision: str,
    *,
    unit: str | None,
    page: int | None,
    finding: str | None,
    reason: str,
    text_file: Path | None = None,
    note: str | None = None,
) -> None:
    """Bind one review decision to the run's latest review, confirm it, then record it.

    An edit's text comes from `text_file`, and its confirmation names the
    text's digest, so the person confirms the exact text recorded.
    """

    from common.contracts.errors import ApprovalRefusal, ContractError
    from common.runtree.store import RunTree

    from . import decide as decide_module

    if decision == "edit" and text_file is None:
        raise OperatorError(
            ErrorCode.DECISION_REFUSED,
            detail="an edit names its corrected text with --text-file",
        )
    if decision != "edit" and text_file is not None:
        raise OperatorError(
            ErrorCode.DECISION_REFUSED,
            detail=f"only an edit takes --text-file; a {decision} names no text",
        )
    if decision != "edit" and note is not None:
        raise OperatorError(
            ErrorCode.DECISION_REFUSED,
            detail=f"only an edit takes --note; a {decision} carries no note",
        )
    text = None
    if text_file is not None:
        try:
            text = text_file.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise OperatorError(
                ErrorCode.DECISION_REFUSED,
                detail=f"the corrected text in {text_file} is not readable UTF-8: {error}",
            ) from error
        text = text.removesuffix("\n").removesuffix("\r") if text.endswith("\n") else text
    tree = _bound_run_tree(RunTree, run_root, run_id)
    try:
        prepared = decide_module.prepare_decision(
            tree,
            decision=decision,
            unit=unit,
            page=page,
            finding=finding,
            reason=reason,
            text=text,
            note=note,
        )
    except (ContractError, OSError) as error:
        raise OperatorError(ErrorCode.DECISION_REFUSED, detail=str(error)) from error
    _print(
        f"Current review of {prepared.subject}: held by {', '.join(prepared.held_codes) or 'nothing'}."
    )
    _print(f"Review basis digest: {prepared.basis_digest}")
    edited = ""
    if prepared.text_sha256 is not None:
        _print(f"Corrected text: {json.dumps(text, ensure_ascii=False)}")
        _print(f"Corrected text digest: {prepared.text_sha256}")
        if note is None:
            _print("Note: none")
            edited = f" with text {prepared.text_sha256} and no note"
        else:
            from common.contracts.canonical import text_sha256

            _print(f"Note: {json.dumps(note, ensure_ascii=False)}")
            _print(f"Note digest: {text_sha256(note)}")
            edited = f" with text {prepared.text_sha256} and note {text_sha256(note)}"
    phrase = (
        f"decide {decision} of {prepared.subject} in {run_id} at {prepared.basis_digest}"
        f"{edited} for reason {json.dumps(reason, ensure_ascii=True)}"
    )
    if _typed_decide_confirmation(phrase) != phrase:
        raise OperatorError(
            ErrorCode.DECISION_REFUSED,
            detail=(
                "the typed confirmation did not exactly name this decision, subject, run, "
                "review basis, corrected text and note, and recorded reason"
            ),
        )
    try:
        reference = decide_module.record_decision(tree, prepared)
    except (ApprovalRefusal, ContractError, OSError) as error:
        raise OperatorError(ErrorCode.DECISION_REFUSED, detail=str(error)) from error
    for line in decide_module.report(prepared, reference):
        _print(line)


def _triage_upload(args: argparse.Namespace) -> dict[str, Path]:
    """The triage documents to send beside the scans, only when named."""
    named = {
        "triage_decision_manifest": args.triage_decision_manifest,
        "triage_producer_recipe": args.triage_producer_recipe,
    }
    return {key: value for key, value in named.items() if value is not None}


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
        "Choose one word: prepare, ingest, triage, upload, run, fetch-run, watch, export, "
        "status, spend, review, decide, advance, backup, or clear-leftovers."
    )
    try:
        verb = input("What would you like to do? ").strip().lower()
    except EOFError:
        _print("No action was chosen. Nothing changed.")
        return []
    if not verb:
        _print("No action was chosen. Nothing changed.")
        return []
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
    if verb == "prepare":
        _print("You can drag a folder from Finder into this window instead of typing its path.")
        scans = _ask_path("Folder of scans to prepare")
        out = _ask_path("Folder for the prepared pages (a new one, or the one used last time)")
        if not scans or not out:
            _print(
                "Prepare needs the folder of scans and a folder for the prepared pages. "
                "One was left blank, so nothing changed."
            )
            return []
        arguments = ["prepare", "--scans", scans, "--out", out]
        overrides = _ask_path("Overrides file with corrections (leave blank for none)")
        if overrides:
            arguments.extend(("--overrides", overrides))
        return arguments
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
    if verb == "clear-leftovers":
        root = _ask("Folder to check for leftovers")
        if not root:
            _print(
                "Clear-leftovers needs a folder to check for leftovers. "
                "It was left blank, so nothing changed."
            )
            return []
        return ["clear-leftovers", "--root", root]
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
    if verb == "watch":
        run_id = _ask("The run ID pod_run is running")
        receipts = _ask("Folder holding the saved copies of that run's report files")
        if not run_id or not receipts:
            _print(
                "Watch needs a run ID and the folder holding the saved report copies. "
                "One was left blank, so nothing changed."
            )
            return []
        arguments = ["watch", "--run-id", run_id, "--receipts", receipts]
        lease = _ask("The pod's saved lease file (leave blank to count spend from the run's start)")
        if lease:
            arguments.extend(("--lease", lease))
        return arguments
    if verb == "export":
        return ["export"]
    if verb == "spend":
        policy = _ask("Reviewed spending-policy file (leave blank for config/spend.toml)")
        arguments = ["spend", "show"]
        if policy:
            arguments.extend(("--policy", policy))
        return arguments
    if verb in {"review", "decide", "advance", "backup"}:
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
        if verb == "decide":
            words = sorted({word for words in REVIEW_DECISIONS.values() for word in words})
            decision = _ask(f"The review decision — one of: {', '.join(words)}")
            subject = _ask(
                "The unit's key as review shows it (for example p2:1), or a page's ordinal "
                "for a page decision"
            )
            reason = _ask("Why the project lead decided this")
            finding = _ask(
                f"For a hold only, its finding — one of: {', '.join(FINDINGS)} (blank otherwise)"
            )
            if not decision or not subject or not reason:
                _print("Decide needs a decision, a unit or page, and a reason. Nothing changed.")
                return []
            arguments.extend(
                (decision, "--page" if subject.isdigit() else "--unit", subject, "--reason", reason)
            )
            if finding:
                arguments.extend(("--finding", finding))
            if decision == "edit":
                text_file = _ask("For an edit, the UTF-8 file holding the corrected text")
                if not text_file:
                    _print("An edit needs the file holding its corrected text. Nothing changed.")
                    return []
                arguments.extend(("--text-file", text_file))
                note = _ask("For an edit, an optional note to travel with it (blank for none)")
                if note:
                    arguments.extend(("--note", note))
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


def _ask_path(label: str) -> str:
    """A path typed or dragged in: a dragged path arrives quoted, or with each space
    escaped by a backslash, which the shell would have removed."""
    answer = _ask(label)
    if len(answer) >= 2 and answer[0] == answer[-1] and answer[0] in "'\"":
        return answer[1:-1]
    if "\\" in answer and not Path(answer).expanduser().exists():
        return re.sub(r"\\(.)", r"\1", answer)
    return answer


def _typed_decide_confirmation(phrase: str) -> str | None:
    try:
        return input(
            "This appends the project lead's review decision; it does not edit evidence or start "
            f"a stage.\nType this line exactly, with no quotation marks:\n{phrase}\n> "
        )
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
