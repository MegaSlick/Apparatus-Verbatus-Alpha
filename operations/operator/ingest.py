"""`verbatus ingest`: a pre-Door folder's plan printed, then written in the same run.

The ingest reads the submitted folder through the data gate, builds the
submission ledger and the triage instrument's evidence, and plans every file
it will write. It prints that plan, then makes exactly those immutable files in
the one empty approved output folder the person selected, from the same
prepared result; the write first checks again that the folder is the one
prepared and still empty.

The submitted masters are untrusted images, so the work runs in a child Python
process (`main`) whose environment holds no credential
(`surface.credential_free_environment`). The parent only validates the
request, starts the child and relays a bounded amount of what it prints. The
child is credential-free, not sandboxed: it can write wherever the user can. On
Linux the parent first makes itself non-dumpable, so the child cannot read the
parent's environment through `/proc`; elsewhere a same-user process can still
read it.
"""

from __future__ import annotations

import ctypes
import json
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from common import corpus_register
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.contracts.errors import ContractError
from common.durability import is_temporary_name
from operations.submit import gate, inventory, submit
from operations.triage import instrument, producer

from .errors import ErrorCode, OperatorError, strip_control_bytes

# The instrument suite's explicit real-corpus-order case is 1,200 frames and
# 14,322 adjacency candidates.  These ceilings leave working margin above that
# case while refusing before the inventory's general 100,000-file allowance can
# become thousands of retained proxies or an unbounded full-comparison list.
MAX_INGEST_FRAMES: Final = 1_500
MAX_INGEST_CANDIDATE_PAIRS: Final = 20_000
# A corpus id is copied into every produced row.  This is far above an ordinary
# identifier and prevents one command-line value from multiplying across a full
# corpus manifest.
MAX_CORPUS_ID_CHARACTERS: Final = 256
# What a preparation or a write may refuse on.
_REFUSALS: Final = (ContractError, OSError, TypeError, ValueError, UnicodeError)
# The child's exit statuses: refused before anything was written, or a write
# that did not finish, which may have left records behind.
REFUSED_EXIT: Final = 2
UNRESOLVED_EXIT: Final = 3
# The child's first line of output, once preparation (which writes nothing) has
# succeeded and before the plan it then prints and writes. First, so the stdout
# bound can never drop it: a child that dies without printing it wrote nothing.
# The parent never relays it.
WRITING_MARKER: Final = "verbatus-ingest: writing"
# What the parent keeps of the child's output. The plan names every file, about
# a dozen per master, so 1,500 masters stay well inside the stdout bound.
MAX_CHILD_STDOUT_BYTES: Final = 8 * 1024 * 1024
MAX_CHILD_STDERR_BYTES: Final = 64 * 1024
_PR_SET_DUMPABLE: Final = 4
_CHECKOUT: Final = Path(__file__).resolve().parents[2]


def ingest(
    *,
    source: Path,
    output_dir: Path,
    policy_path: Path | None,
    corpus_id: str,
    mode: str,
    confirmation_file: Path | None,
    workspace: Path,
    printer: Callable[[str], None],
) -> None:
    """Prepare, print and write the ready folder in a credential-free child process."""

    from .surface import credential_free_environment

    request = {
        # Do not resolve operator-selected paths here. `resolve()` follows a
        # symlink before the data gate can reject that redirection, converting
        # the gate's deliberate no-symlink rule into an invisible bypass.
        "source": str(_absolute_path(source, workspace)),
        "output_dir": str(_absolute_path(output_dir, workspace)),
        "policy": str(
            _absolute_path(
                policy_path or workspace / "config" / "data_handling_policy.json", workspace
            )
        ),
        "corpus_id": corpus_id,
        "mode": mode,
        "confirmation_file": None
        if confirmation_file is None
        else str(_absolute_path(confirmation_file, workspace)),
    }
    try:
        _request(request)
    except ValueError as error:
        raise OperatorError(ErrorCode.INGEST_REFUSED, detail=str(error)) from error
    _deny_same_user_inspection()
    returncode, stdout, stderr = _run_child(json.dumps(request), credential_free_environment())
    lines = stdout.splitlines()
    writing = lines[:1] == [WRITING_MARKER]
    for line in lines[1:] if writing else lines:
        printer(line)
    detail = stderr.strip() or f"the ingest child exited {returncode}"
    if returncode == 0:
        return
    # The child's own statuses decide; a crash or kill is unresolved once the
    # child may have started writing.
    if returncode == REFUSED_EXIT or (returncode != UNRESOLVED_EXIT and not writing):
        raise OperatorError(ErrorCode.INGEST_REFUSED, detail=detail)
    raise OperatorError(ErrorCode.INGEST_UNRESOLVED, detail=detail)


def _deny_same_user_inspection(code: ErrorCode = ErrorCode.INGEST_REFUSED) -> None:
    """On Linux, stop a same-user process reading this one's environment or memory.

    A non-dumpable process's `/proc/<pid>` entries belong to root and it cannot
    be traced, so a compromised decoder in the child cannot read the parent's
    credentials while the parent waits. Other platforms have no such switch.
    """
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OperatorError(
            code,
            detail="this process could not be made non-dumpable before the child ran "
            f"(errno {ctypes.get_errno()}); nothing was written",
        )


def _run_child(request: str, environment: dict[str, str]) -> tuple[int, str, str]:
    """Run the child, keeping at most a bounded prefix of each output stream.

    Both streams are drained to the end so the child never blocks on a full
    pipe; bytes past the bound are dropped and the kept text says so.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "operations.operator.ingest"],
        cwd=_CHECKOUT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    kept: dict[str, str] = {}

    def drain(name: str, stream: Any, limit: int) -> None:
        data = bytearray()
        dropped = False
        while chunk := stream.read(65536):
            room = limit - len(data)
            data += chunk[:room]
            dropped = dropped or len(chunk) > room
        text = data.decode("utf-8", errors="replace")
        kept[name] = text + (f"\n[output past {limit} bytes was dropped]" if dropped else "")

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout, MAX_CHILD_STDOUT_BYTES)),
        threading.Thread(target=drain, args=("stderr", process.stderr, MAX_CHILD_STDERR_BYTES)),
    ]
    for reader in readers:
        reader.start()
    assert process.stdin is not None
    try:
        process.stdin.write(request.encode("utf-8"))
        process.stdin.close()
    except BrokenPipeError:
        pass
    returncode = process.wait()
    for reader in readers:
        reader.join()
    return returncode, kept["stdout"], kept["stderr"]


def main() -> int:
    """The child: prepare once, print the plan, write it, and say what the Door will see."""

    try:
        prepared = _prepare(_request(json.loads(sys.stdin.read())))
    except _REFUSALS as error:
        print(str(error), file=sys.stderr)
        return REFUSED_EXIT
    print(WRITING_MARKER, flush=True)
    _print_preview(_summary(prepared), print)
    sys.stdout.flush()
    try:
        _commit(prepared)
    except _REFUSALS as error:
        print(str(error), file=sys.stderr)
        return UNRESOLVED_EXIT
    summary = _summary(prepared)
    print(
        "Ready-to-submit folder: "
        f"{strip_control_bytes(str(prepared.output_dir))}\n"
        "What the Door will see: "
        f"{summary['submission_files']} submitted file(s), ledger self-hash "
        f"{summary['submission_ledger_self_hash']}, "
        f"triage mode {summary['mode']}, {summary['candidate_count']} candidate evidence record(s), "
        f"{summary['confirmed_cluster_count']} confirmed cluster(s).\n"
        "No pod was started, confirmed, or billed. The observation-based full-run exit remains the project lead's."
    )
    return 0


def _absolute_path(path: Path, workspace: Path) -> Path:
    """Anchor a UI path without resolving an operator-controlled symlink."""

    return path if path.is_absolute() else workspace / path


# How many undecodable frames a refusal lists before it summarises the rest. A
# folder of holiday photographs would otherwise produce a refusal longer than the
# 2000 characters `errors.sanitize_detail` keeps, and a truncated list reads as a
# complete one.
_MAX_LISTED_REFUSALS = 10


@dataclass(frozen=True)
class PreparedIngest:
    output_dir: Path
    output_identity: tuple[int, int]
    manifest: dict[str, Any]
    frames: tuple[producer.SubmittedFrame, ...]
    recipe: dict[str, Any]
    proxies: tuple[instrument.ProxySet, ...]
    evidence: tuple[dict[str, Any], ...]
    evidence_manifest: dict[str, Any]
    confirmation: dict[str, Any] | None
    produced: producer.ProducedTriage
    data_handling_policy_sha256: str


def _request(value: dict[str, Any]) -> dict[str, Any]:
    """Refuse an operator's paths, corpus id or mode before anything is read."""
    if not all(
        isinstance(value[name], str) and value[name].strip()
        for name in ("source", "output_dir", "policy", "corpus_id")
    ):
        raise ValueError("ingest request has a missing path or corpus id")
    if len(value["corpus_id"]) > MAX_CORPUS_ID_CHARACTERS:
        raise ValueError(f"ingest corpus id is longer than {MAX_CORPUS_ID_CHARACTERS} characters")
    if value["mode"] not in {"manual", "semi", "auto"}:
        raise ValueError("ingest request names an undeclared triage mode")
    return value


def _prepare(request: Mapping[str, Any]) -> PreparedIngest:
    policy_binding = gate.load_policy_binding(Path(request["policy"]))
    roots = gate.approved_storage_roots(policy_binding.policy)
    source = gate.require_approved_storage_location(
        Path(request["source"]), roots, "submitted folder"
    )
    output_dir = gate.require_approved_storage_location(
        Path(request["output_dir"]), roots, "ingest output folder"
    )
    output_identity = _directory_identity(output_dir)
    if gate.same_or_inside(source, output_dir):
        # Filesystem identity, not spelling: a case-variant path on default
        # (case-insensitive) APFS defeats a textual `is_relative_to` here.
        # The Door inventories the entire submitted folder, so an ingest output
        # inside it would become a submitted source and make the ready folder unusable.
        raise ValueError(
            "the ingest output folder cannot live inside the submitted folder; otherwise the "
            "next inventory includes these produced records as submitted sources and the Door "
            "refuses the whole submission. Choose an empty approved folder beside it."
        )
    if any(output_dir.iterdir()):
        raise ValueError(
            "the ingest output folder is not empty; choose a new empty approved folder"
        )
    manifest = submit.build_manifest(submit.walk_folder(source))
    if len(manifest["files"]) > MAX_INGEST_FRAMES:
        raise ValueError(
            f"this submission holds more than {MAX_INGEST_FRAMES} image masters, which is "
            "more than one bounded triage pass accepts; nothing was written. Prepare it as "
            "smaller submitted folders."
        )
    frames = _frames(source, manifest)
    config = instrument.load_config()
    proxies = _proxies(frames, manifest, config)
    evidence, evidence_manifest = _candidate_evidence(proxies, config)
    recipe = instrument.producer_recipe(config)
    confirmation = (
        None
        if request["confirmation_file"] is None
        else producer.load_confirmation(Path(request["confirmation_file"]))
    )
    produced = producer.produce(
        frames,
        corpus_id=request["corpus_id"],
        mode=request["mode"],
        confirmation=confirmation,
        instrument_recipe=recipe if confirmation is not None else None,
        evidence_manifest=evidence_manifest if confirmation is not None else None,
        evidence_records=evidence if confirmation is not None else None,
    )
    # A supplied confirmation authorizes a corpus-register append; an empty one
    # is refused before anything is written, with the commit seam's exact
    # refusal text.
    if confirmation is not None and not produced.clusters:
        raise producer.ProducerRefusal(
            "confirmation names no cluster; no manifest documents were written"
        )
    if _directory_identity(output_dir) != output_identity:
        raise ValueError(
            "the ingest output folder changed while the plan was being prepared; nothing "
            "was written. Choose a new empty approved folder and run ingest again."
        )
    return PreparedIngest(
        output_dir=output_dir,
        output_identity=output_identity,
        manifest=manifest,
        frames=frames,
        recipe=recipe,
        proxies=proxies,
        evidence=tuple(evidence),
        evidence_manifest=evidence_manifest,
        confirmation=confirmation,
        produced=produced,
        data_handling_policy_sha256=policy_binding.config_sha256,
    )


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ValueError("the ingest output location is not a readable directory") from error
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("the ingest output location is not a directory")
    return (status.st_dev, status.st_ino)


def _frames(source: Path, manifest: Mapping[str, Any]) -> tuple[producer.SubmittedFrame, ...]:
    """Reopen ledgered files through the bounded, anchored no-follow seam.

    The producer retains whole frames, so this uses the inventory module's one
    retained-byte ceiling; exceeding it must be a refusal, never an OOM with no result.
    """

    frames = []
    retained = 0
    for position, row in enumerate(manifest["files"], start=1):
        if row["bytes"] > inventory.MAX_SUBMITTED_BYTES:
            raise ValueError(_too_large(position, len(manifest["files"]), row["sha256"]))
        with inventory.open_submission_source(source, row["relative_path"]) as opened:
            # One byte past the ceiling: a file that grew between the ledger walk
            # and this reopen is caught by the length check rather than allocated.
            data = opened.handle.read(inventory.MAX_SUBMITTED_BYTES + 1)
            opened.assert_unchanged(expected_sha256=row["sha256"])
        if len(data) > inventory.MAX_SUBMITTED_BYTES:
            raise ValueError(_too_large(position, len(manifest["files"]), row["sha256"]))
        if digest_bytes(data) != row["sha256"]:
            raise ValueError("submitted source bytes changed after the ledger was read")
        retained += len(data)
        if retained > inventory.MAX_SUBMITTED_BYTES:
            raise ValueError(
                f"this submission holds more than {inventory.MAX_SUBMITTED_BYTES} bytes of "
                "master images, which is more than one triage pass may hold in memory at "
                "once; nothing was written. Prepare it as smaller submitted folders."
            )
        frames.append(producer.SubmittedFrame(path=row["relative_path"], data=data))
    return tuple(frames)


def _too_large(position: int, total: int, digest: str) -> str:
    return (
        f"submitted file {position} of {total} in the ledger's path order (digest "
        f"{digest[:12]}) is larger than the {inventory.MAX_SUBMITTED_BYTES}-byte ceiling on "
        "retained submitted bytes; nothing was written."
    )


def _proxies(
    frames: tuple[producer.SubmittedFrame, ...],
    manifest: Mapping[str, Any],
    config: instrument.InstrumentConfig,
) -> tuple[instrument.ProxySet, ...]:
    """Build every proxy, or identify every undecodable frame without paths.

    Candidate evidence covers the full frame set, so partial proxy production is
    invalid. Terminal policy permits ledger positions and digest prefixes, not names.
    """

    proxies: list[instrument.ProxySet] = []
    undecodable: list[str] = []
    total = len(manifest["files"])
    for position, (frame, row) in enumerate(zip(frames, manifest["files"], strict=True), start=1):
        try:
            proxy = instrument.build_proxies_from_bytes(frame.data, config)
        except instrument.InstrumentRefusal:
            undecodable.append(f"file {position} (digest {row['sha256'][:12]})")
            continue
        if (
            digest_bytes(proxy.signature_png) != proxy.signature_png_sha256
            or digest_bytes(proxy.review_png) != proxy.review_png_sha256
        ):
            raise instrument.InstrumentRefusal(
                "a triage proxy does not match the digest computed while it was prepared; "
                "nothing was written"
            )
        proxies.append(proxy)
    if undecodable:
        listed = ", ".join(undecodable[:_MAX_LISTED_REFUSALS])
        remainder = len(undecodable) - _MAX_LISTED_REFUSALS
        if remainder > 0:
            listed += f", and {remainder} more"
        raise instrument.InstrumentRefusal(
            f"the triage instrument prepares decodable image masters only, and "
            f"{len(undecodable)} of {total} submitted file(s) could not be decoded: {listed}. "
            "Positions count in the ledger's path order. Move them out of the submitted "
            "folder and run ingest again; a container such as a PDF reaches the Door "
            "through `verbatus upload` instead."
        )
    return tuple(proxies)


def _candidate_evidence(
    proxies: tuple[instrument.ProxySet, ...], config: instrument.InstrumentConfig
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bound both expensive comparisons and the refused-pair evidence list."""

    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    reached = len(selection.pairs) + len(selection.dimension_refused)
    if reached > MAX_INGEST_CANDIDATE_PAIRS:
        raise instrument.InstrumentRefusal(
            f"the triage instrument selected or explicitly refused {reached} candidate "
            f"pairs, above the {MAX_INGEST_CANDIDATE_PAIRS}-pair ceiling for one ingest; "
            "nothing was written. Prepare the submission as smaller folders."
        )
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    return evidence, instrument.evidence_manifest(proxies, selection, evidence, config)


def _paths(prepared: PreparedIngest) -> list[str]:
    """Every name commit creates; preview approval requires exact set equality."""

    values = [
        "submission-manifest.json",
        "triage-producer-recipe.json",
        "candidate-evidence-manifest.json",
    ]
    for proxy in prepared.proxies:
        values.extend(
            (
                f"signature-proxy-{proxy.source_frame_sha256}.png",
                f"review-proxy-{proxy.source_frame_sha256}.png",
            )
        )
    for record in prepared.evidence:
        left, right = record["both_digests"]
        values.append(f"candidate-evidence-{left}-{right}.json")
    values.extend(("triage-decision-manifest.json", "triage-clusters.json"))
    if prepared.confirmation is not None:
        # `.corpus-register.json.lock` is never removed after a crash, but the
        # commit really does create it, so it is listed rather than shown as
        # a plan that understates what gets written.
        values.extend(
            (
                "corpus-register.json",
                ".corpus-register.json.lock",
                "triage-confirmation.json",
            )
        )
    values.append("ingest-ready.json")
    return values


def _summary(prepared: PreparedIngest) -> dict[str, Any]:
    candidates = [
        " ".join(
            (
                record["both_digests"][0][:12],
                record["both_digests"][1][:12],
                record["verdict"],
                str(record["thresholds"].get("near_duplicate_reason", "")),
            )
        )
        for record in prepared.evidence
    ]
    return {
        "submission_files": len(prepared.manifest["files"]),
        # Commit pins the canonical ledger including its self-hash field.
        "submission_manifest_sha256": digest_of(prepared.manifest),
        # The Door binds the self-hash into every admitted page, so this is the
        # ledger identity shown to the operator and recorded in `ingest-ready.json`.
        "submission_ledger_self_hash": prepared.manifest["self_hash"],
        "confirmation_sha256": None
        if prepared.confirmation is None
        else digest_of(prepared.confirmation),
        "instrument_config_sha256": prepared.recipe["instrument_config_sha256"],
        "data_handling_policy_sha256": prepared.data_handling_policy_sha256,
        "mode": prepared.produced.manifest["records"][0]["mode"],
        "candidate_count": len(prepared.evidence),
        "confirmed_cluster_count": len(prepared.produced.clusters),
        "confirmed_clusters": _cluster_lines(prepared.confirmation),
        "planned_files": _paths(prepared),
        "candidates": candidates,
    }


def _cluster_lines(confirmation: Mapping[str, Any] | None) -> list[str]:
    # The preview's digest pins the confirmation's bytes, not what the
    # operator believes they say, so each cluster's page designations and
    # member digest prefixes are shown to put the membership itself in
    # front of them, not just a number.
    if confirmation is None:
        return []
    return [
        "; ".join(
            f"{page['volume_id']} {page['designation']}: "
            + " ".join(digest[:12] for digest in page["member_frame_sha256"])
            for page in cluster["pages"]
        )
        for cluster in confirmation["clusters"]
    ]


def _commit(prepared: PreparedIngest) -> None:
    _assert_output_identity(prepared)
    if any(prepared.output_dir.iterdir()):
        raise ValueError(
            "the ingest output folder changed after it was prepared; no existing entry was "
            "reused or overwritten"
        )
    # The already-built ledger goes through the submission's own immutable
    # atomic-create primitive, not its logging writer.
    _write(prepared.output_dir / "submission-manifest.json", prepared.manifest)
    _write(prepared.output_dir / "triage-producer-recipe.json", prepared.recipe)
    _write(prepared.output_dir / "candidate-evidence-manifest.json", prepared.evidence_manifest)
    for proxy in prepared.proxies:
        _write_bytes(
            prepared.output_dir / f"signature-proxy-{proxy.source_frame_sha256}.png",
            proxy.signature_png,
        )
        _write_bytes(
            prepared.output_dir / f"review-proxy-{proxy.source_frame_sha256}.png",
            proxy.review_png,
        )
    for record in prepared.evidence:
        left, right = record["both_digests"]
        _write(prepared.output_dir / f"candidate-evidence-{left}-{right}.json", record)
    if prepared.confirmation is None:
        _write(prepared.output_dir / "triage-decision-manifest.json", prepared.produced.manifest)
        _write(prepared.output_dir / "triage-clusters.json", prepared.produced.clusters)
    else:
        committed_production, register_sha256 = producer.commit_confirmed_production(
            prepared.frames,
            corpus_id=prepared.produced.manifest["corpus_id"],
            mode=prepared.produced.manifest["records"][0]["mode"],
            confirmation=prepared.confirmation,
            instrument_recipe=prepared.recipe,
            evidence_manifest=prepared.evidence_manifest,
            evidence_records=prepared.evidence,
            register_path=prepared.output_dir / "corpus-register.json",
            manifest_path=prepared.output_dir / "triage-decision-manifest.json",
            clusters_path=prepared.output_dir / "triage-clusters.json",
            authority_path=prepared.output_dir / "triage-confirmation.json",
        )
        if committed_production != prepared.produced:
            raise ValueError(
                "confirmed triage production changed between preparation and publication"
            )
        register_path = prepared.output_dir / "corpus-register.json"
        if corpus_register.register_digest(register_path.read_bytes()) != register_sha256:
            raise ValueError(
                "the published corpus register does not match the verified chain digest"
            )
    _assert_output_identity(prepared)
    _assert_pre_ready_entries(prepared)
    _write(prepared.output_dir / "ingest-ready.json", _ready_record(prepared))


def _assert_output_identity(prepared: PreparedIngest) -> None:
    if _directory_identity(prepared.output_dir) != prepared.output_identity:
        raise ValueError(
            "the ingest output folder changed after the preview was shown; the ready record "
            "was not published"
        )


def _assert_pre_ready_entries(prepared: PreparedIngest) -> None:
    expected = set(_paths(prepared))
    expected.remove("ingest-ready.json")
    entries = [
        entry
        for entry in prepared.output_dir.iterdir()
        if not (
            is_temporary_name(entry.name)
            and entry.name[1:].partition(".tmp-")[0] in expected
            and stat.S_ISREG(entry.lstat().st_mode)
        )
    ]
    if {entry.name for entry in entries} != expected or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise ValueError(
            "the ingest output folder does not contain exactly the regular files in the "
            "previewed plan; the ready record was not published"
        )


def _ready_record(prepared: PreparedIngest) -> dict[str, Any]:
    return {
        "schema": "operator-ingest-ready-v1",
        "submission_manifest_sha256": digest_of(prepared.manifest),
        # Both recorded: the canonical digest is what this commit pinned to,
        # and the self-hash is what a later reader reconciles a run against.
        "submission_ledger_self_hash": prepared.manifest["self_hash"],
        "data_handling_policy_sha256": prepared.data_handling_policy_sha256,
        "triage_manifest_sha256": digest_of(prepared.produced.manifest),
        "triage_recipe_sha256": digest_of(prepared.recipe),
        "candidate_evidence_manifest_sha256": digest_of(prepared.evidence_manifest),
        "confirmed_cluster_count": len(prepared.produced.clusters),
        "confirmation_file_retained": prepared.confirmation is not None,
    }


def _write(path: Path, record: Mapping[str, Any]) -> None:
    _write_bytes(path, canonical_bytes(record))


def _write_bytes(path: Path, data: bytes) -> None:
    # This flow requires a freshly empty folder.  An identical existing target is
    # therefore a concurrent change, not an idempotent retry: accepting it could
    # follow a planted symlink and call an output record written when it was not.
    if not submit.atomic_create(path, data):
        raise ValueError(
            "an ingest output entry appeared after the empty-folder check; no existing "
            "entry was accepted as this commit's evidence"
        )


def _print_preview(summary: dict[str, Any], printer: Callable[[str], None]) -> None:
    candidate_lines = [
        "  " + strip_control_bytes(str(candidate)) for candidate in summary["candidates"]
    ] or ["  none"]
    planned_lines = ["  " + strip_control_bytes(str(path)) for path in summary["planned_files"]]
    printer(
        "Ingest preview — no files have been written.\n"
        f"Submission ledger: {summary['submission_files']} file(s), self-hash "
        f"{summary['submission_ledger_self_hash']}.\n"
        f"Data gate: approved policy {summary['data_handling_policy_sha256']} checked. "
        f"Triage mode: {summary['mode']}.\n"
        "Instrument candidates (digest prefixes, verdict, reason):\n"
        + "\n".join(candidate_lines)
        + "\nConfirmation: "
        + (
            "the supplied confirmation will be validated and retained. "
            "Confirmed clusters (page designations, member digest prefixes):\n"
            + "\n".join(
                "  " + strip_control_bytes(str(line)) for line in summary["confirmed_clusters"]
            )
            if summary["confirmed_cluster_count"]
            else "no cluster confirmation file was supplied; every cluster field stays null."
        )
        + "\nThe following immutable files are written now, from exactly this plan:\n"
        + "\n".join(planned_lines)
        + "\nThe write first checks that the output folder is the one prepared and still "
        "empty, and refuses rather than write anywhere else."
    )


if __name__ == "__main__":
    raise SystemExit(main())
