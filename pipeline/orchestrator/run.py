"""The orchestrator: sequencing, resume, and recovery dispatch. It is not a stage.

Its home is decided here, once: `pipeline/orchestrator/`, a peer of the numbered
stage directories rather than one of them. It is stage-neutral, imports only
`common/`, and invokes stages **as programs** — real subprocesses, real argv, real
exit codes. That last part is meta-invariant #90's requirement made concrete: one
harness runs the real orchestration end to end offline, so a green Python suite can
never stand in for a pipeline that was never actually executed.

It establishes nothing and reads nothing except the outcome bookkeeping it needs to
sequence and to checkpoint. Its four jobs:

  Sequence.   Door, Exemplar, Ink Map, Designator, Attestatores, Perlector,
              Recensor, recovery, Archetypus, Armarium, in that order.
  Recover.    The Recensor appends a request; the orchestrator invokes the owning
              stage — the Designator — for a replacement region, then re-reads and
              re-reviews. The Recensor never cuts a crop, so recovery does not grow
              a second author for regions.
  Checkpoint. After every stage invocation and every recovery round, the run-level
              hard-failure cap (`common/hard_failure.py`, which owns the tally and
              the reasoning behind it) is recomputed. Two hard failures is an early
              warning and the run keeps going; more than two halts it at the stage
              boundary just finished — never mid-stage — with whatever completed
              intact.
  Resume.     Nothing here tracks progress in a file of its own. Every stage
              republishes what it already published, and the run tree reuses
              identical bytes and refuses different ones. Resume is therefore a
              property of the artifacts rather than of a checkpoint that could
              disagree with them.

    python pipeline/orchestrator/run.py --fixture synthetic-two-page-v0 \\
      --scenario <happy|review> --run-id <id> --run-root <dir>
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.alignment import DEFAULT_ALIGNMENT_CONFIG_PATH  # noqa: E402
from common.armarium_formats import DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.outcomes import ArmariumCategory, check_algebra_is_total  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, INK_MAP, RECENSOR  # noqa: E402
from common.durability import sync_directory  # noqa: E402
from common.hard_failure import (  # noqa: E402
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.recovery import (  # noqa: E402
    DEFAULT_RECOVERY_CONFIG_PATH,
    FALLBACK_RECROP,
    load_recovery_policy,
)
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    DEFAULT_DECODING_CONFIG_PATH,
    DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH,
    DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH,
    DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH,
    DEFAULT_PDF_RENDER_CONFIG_PATH,
    DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    EXIT_COMPLETE,
    EXIT_HELD,
    EXIT_RUN_HALTED,
    RUN_MODES,
    WITNESS_CONTEXT_REGIMES,
    current_recovery_request,
    is_real_ingress,
    latest_attempt,
    load_fixture,
    require_sealed_config,
    run_sealed_config_digests,
    scenario_for,
    verify_final_seal,
    verify_predecessor_seal,
)

ROOT = Path(__file__).resolve().parents[2]

# The pipeline in flow order. The door is a program of the Exemplar's directory
# because it owns no directory of its own.
SEQUENCE = (
    ("door", "pipeline/1_exemplar/door.py"),
    ("exemplar", "pipeline/1_exemplar/run.py"),
    (INK_MAP, "pipeline/1_ink_map/run.py"),
    ("designator", "pipeline/2_designator/run.py"),
    (ATTESTATORES, "pipeline/3_attestatores/run.py"),
    ("perlector", "pipeline/4_perlector/run.py"),
    ("recensor", "pipeline/5_recensor/run.py"),
    ("recovery", None),
    ("archetypus", "pipeline/6_archetypus/run.py"),
    ("armarium", "pipeline/7_armarium/run.py"),
)

STAGE_PROGRAMS = {name: program for name, program in SEQUENCE if program is not None}
_PROGRAM_NAMES = {program: name for name, program in STAGE_PROGRAMS.items()}
SEQUENCE_NAMES = tuple(name for name, _program in SEQUENCE)
# Named here rather than imported from `operations.submit.gate`, because this
# module imports only `common/` (see the module docstring) and the Door is the
# one place the gate itself is loaded. The two spellings are held together by
# `test_orchestrator_default_data_gate_policy_is_the_gates_own`, so the pair
# cannot drift apart in silence.
DEFAULT_DATA_GATE_POLICY_PATH = ROOT / "config" / "data_handling_policy.json"
# Duplicated for the same reason and closed the same way: importing
# `operations.operator.volume_s3`, which owns these names, would cross that
# boundary too, and
# `test_orchestrator_upload_credentials_are_the_transfers_own` reconciles this
# copy with it. A credential added to one list alone would otherwise leave this
# route carrying it into a stage that decodes caller-supplied material. Kept
# even though `stage_environment` no longer loops over it directly (below): the
# reconciliation test still pins this exact set against the transfer's own.
_TRANSFER_CREDENTIAL_ENV = frozenset({"RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"})
# The wall clock a timing receipt is stamped with, and the monotonic one its
# duration is measured against. Kept separate deliberately: a duration taken
# from wall-clock differences is wrong across a clock adjustment, and a
# monotonic reading names no instant a reader could compare across records.
_clock = time.monotonic
STAGE_TIMING_JOURNAL_SCHEMA = "stage-timing-journal.v1"
# Duplicated from `operations.operator.custody.PROVIDER_ENV_PREFIXES` and
# `operations.pod.models.looks_like_credential_field`'s marker scan, for the
# identical reason and closed the identical way (reconciled by the same test
# named above, widened to cover this). `stage_environment` used to pop only
# the two names above -- the transfer verb's own upload-only S3 keys -- and
# pass every *other* provider credential (RUNPOD_API_KEY: pod creation, i.e.
# money; HF_TOKEN; AWS_*; ...) straight into a subprocess that decodes
# attacker-supplied PDFs, TIFFs, HEICs and PNGs and talks to the serving
# endpoint. That subprocess is at least as hostile a boundary as the operator's
# confined console/backup/advance/ScanTailor children, which already run under
# `operations.operator.custody.credential_free_environment` -- this is that
# same predicate, held to it by the widened test rather than imported, because
# this module imports only `common/`.
_PROVIDER_ENV_PREFIXES = ("RUNPOD_", "AWS_", "HF_", "HUGGINGFACE_")
_CREDENTIAL_NAME_MARKERS = ("key", "secret", "password", "credential", "bearer", "token")


def _looks_like_provider_credential(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(name.startswith(prefix) for prefix in _PROVIDER_ENV_PREFIXES) or any(
        marker in normalized for marker in _CREDENTIAL_NAME_MARKERS
    )


def require_coherent_ingress_options(args: argparse.Namespace) -> None:
    if args.submission_folder is not None:
        if (
            getattr(args, "triage_clusters", None) is not None
            or getattr(args, "triage_producer_recipe", None) is not None
        ) and getattr(args, "triage_decision_manifest", None) is None:
            raise ContractError(
                "--triage-clusters and --triage-producer-recipe require --triage-decision-manifest"
            )
        return
    if args.submission_manifest is not None:
        raise ContractError(
            "a submission filename ledger is meaningful only with a real submission folder; "
            "the walking skeleton's declared synthetic pages are not gated input "
            "(--submission-manifest was supplied without --submission-folder)"
        )
    if args.data_gate_policy is not None:
        raise ContractError(
            "--data-gate-policy is meaningful only with --submission-folder; the synthetic "
            "fixture route does not evaluate the real-input storage policy"
        )
    if (
        getattr(args, "triage_decision_manifest", None) is not None
        or getattr(args, "triage_clusters", None) is not None
        or getattr(args, "triage_producer_recipe", None) is not None
    ):
        raise ContractError("triage geometry is meaningful only with --submission-folder")


def resolve_caller_paths(args: argparse.Namespace) -> argparse.Namespace:
    """Bind paths to the caller's cwd without hiding symlinks from the Door."""
    args.run_root = Path(args.run_root).absolute()
    for attribute in (
        "submission_folder",
        "submission_manifest",
        "triage_decision_manifest",
        "triage_clusters",
        "triage_producer_recipe",
        # Resolved here too, or an ordinary CLI run with a relative
        # `--cache-root` would be refused by the boundary guard above.
        "cache_root",
    ):
        value = getattr(args, attribute, None)
        if value is not None:
            setattr(args, attribute, Path(value).absolute())
    # A real run's absent policy means the repository default; fixture runs must
    # not forward a real-only control that the Door would ignore.
    if args.data_gate_policy is None and args.submission_folder is not None:
        args.data_gate_policy = DEFAULT_DATA_GATE_POLICY_PATH
    elif args.data_gate_policy is not None:
        args.data_gate_policy = Path(args.data_gate_policy).absolute()
    return args


def stage_environment() -> dict[str, str]:
    """Keep stage runtime settings, but drop every provider credential (F016)."""
    return {
        name: value
        for name, value in os.environ.items()
        if not _looks_like_provider_credential(name)
    }


def invoke(program: str, args: argparse.Namespace, **extra) -> int:
    """Run one stage as a program and return its exit code."""
    require_coherent_ingress_options(args)
    # Direct invocation entry points must not reinterpret caller paths under the
    # child's repository-root cwd.
    for attribute, flag in (
        ("run_root", "--run-root"),
        ("submission_folder", "--submission-folder"),
        ("submission_manifest", "--submission-manifest"),
        ("data_gate_policy", "--data-gate-policy"),
        # The three triage paths and the model cache were forwarded to children
        # unchecked, so a direct caller could make the Door read triage data, or
        # a stage read a model cache, relative to the repository rather than to
        # the caller (CodeRabbit). They belong behind the same boundary as every
        # other caller path.
        ("triage_decision_manifest", "--triage-decision-manifest"),
        ("triage_clusters", "--triage-clusters"),
        ("triage_producer_recipe", "--triage-producer-recipe"),
        ("cache_root", "--cache-root"),
    ):
        value = getattr(args, attribute, None)
        if value is not None and not Path(value).is_absolute():
            raise ContractError(
                f"{flag} is still the caller-relative path {str(value)!r}. Stages run from "
                f"{ROOT} while the caller may be anywhere, so this must be resolved at the "
                "orchestration boundary (`resolve_caller_paths`) before any child sees it"
            )
    command = [
        sys.executable,
        # Ignore PYTHON* startup controls and the user site for child stages.
        # The stage scripts add the repository root themselves; accepting an
        # operator's PYTHONPATH/sitecustomize here would execute unsealed code
        # before the stage reached its first refusal boundary.
        "-I",
        str(ROOT / program),
        "--run-root",
        str(args.run_root),
        "--run-id",
        args.run_id,
        "--scenario",
        args.scenario,
        "--fixture-root",
        str(args.fixture_root),
        "--models-config",
        str(args.models_config),
        "--decoding-config",
        str(args.decoding_config),
        "--serving-recipes-config",
        str(args.serving_recipes_config),
        "--pdf-render-config",
        str(args.pdf_render_config),
        "--designator-padding-config",
        str(args.designator_padding_config),
        "--designator-geometry-config",
        str(args.designator_geometry_config),
        "--designator-grouping-config",
        str(args.designator_grouping_config),
        "--alignment-config",
        str(args.alignment_config),
        "--formats-config",
        str(args.formats_config),
        "--recovery-config",
        str(args.recovery_config),
        "--hard-failure-config",
        str(args.hard_failure_config),
    ]
    cache_root = getattr(args, "cache_root", None)
    if cache_root is not None:
        command += ["--cache-root", str(cache_root)]
    # Later stages may read only the run tree the Door sealed, never source paths.
    if program == STAGE_PROGRAMS["door"]:
        # The Door is the one stage that creates the run authority, so it is the
        # only one that can seal the commit into it. Forwarded only when it was
        # actually read: a tree with no version control records no commit rather
        # than a placeholder that looks like one (GOVERNANCE 10).
        commit, _detail = repository_commit(args)
        if commit is not None:
            command += ["--repository-commit", commit]
        if args.submission_folder is not None:
            command += ["--submission-folder", str(args.submission_folder)]
        if args.submission_manifest is not None:
            command += ["--submission-manifest", str(args.submission_manifest)]
        if args.data_gate_policy is not None:
            command += ["--data-gate-policy", str(args.data_gate_policy)]
        for attribute, flag in (
            ("triage_decision_manifest", "--triage-decision-manifest"),
            ("triage_clusters", "--triage-clusters"),
            ("triage_producer_recipe", "--triage-producer-recipe"),
        ):
            value = getattr(args, attribute, None)
            if value is not None:
                command += [flag, str(value)]
    if args.pdf_target_dpi is not None:
        command += ["--pdf-target-dpi", str(args.pdf_target_dpi)]
    # A measured runtime fact of the card, not run configuration (GOVERNANCE 6);
    # forwarded only when set, so a fixture run's argv carries no
    # "--placement-tier None" and stage_parser's own default (None) governs.
    if args.placement_tier is not None:
        command += ["--placement-tier", str(args.placement_tier)]
    if getattr(args, "mechanics_qualification", False):
        command.append("--mechanics-qualification")
    # Forwarded to every stage, not only to the door that snapshots it: the
    # drift refusal exists to catch a register appended *between* two stages of
    # one run, which is precisely the case an unforwarded flag cannot see.
    if args.corpus_register is not None:
        command += ["--corpus-register", str(args.corpus_register)]
    command += [
        "--witness-context",
        args.witness_context,
        "--witness-context-config",
        str(args.witness_context_config),
        "--nuda-per-mille",
        str(args.nuda_per_mille),
        "--nuda-approval-ref",
        str(args.nuda_approval_ref),
        "--perlector-instrument-per-mille",
        str(args.perlector_instrument_per_mille),
        "--perlector-instrument-approval-ref",
        str(args.perlector_instrument_approval_ref),
        "--perlector-protocol-config",
        str(args.perlector_protocol_config),
        "--perlector-audit-config",
        str(args.perlector_audit_config),
    ]
    command.append("--draft-fed" if args.draft_fed else "--no-draft-fed")
    for key, value in extra.items():
        command += [f"--{key.replace('_', '-')}", str(value)]

    # Inherit the operator's streams instead of buffering a stage's unbounded
    # stdout/stderr in the orchestrator. Each stage owns its diagnostic text,
    # and an unexpected exit is named after that text has already been relayed.
    # This is also how a completed-but-partial Door's private refusal report
    # reaches the human who ran the pipeline: it is already on the terminal by
    # the time any exit is judged, rather than being captured and re-printed.
    #
    # `stage_environment()` is not optional and is the reason this call is not a
    # bare subprocess.run: it drops the transfer credentials from every stage's
    # environment, so only the upload-only verb can ever see them.
    started = _clock()
    started_at = _stamp()
    # Bound before the call, not inside it: the `finally` below reads this, and
    # an interruption that is not an `OSError` -- a `KeyboardInterrupt` while a
    # stage runs is the ordinary one -- used to leave the name unbound and
    # replace the interruption with an `UnboundLocalError` from the stopwatch
    # (CodeRabbit on PR #117). A stage that could not start is timed with no
    # exit code, which is the same record the OSError path produced.
    exit_code: int | None = None
    try:
        completed = subprocess.run(command, cwd=ROOT, env=stage_environment())
        exit_code = completed.returncode
    finally:
        # In `finally` so a stage that could not start, and one about to be
        # turned into a ContractError below, are both timed: the invocations a
        # later reader most wants a clock on are the ones that went wrong.
        _record_stage_timing(
            args,
            program=program,
            extra=extra,
            started_at=started_at,
            finished_at=_stamp(),
            duration_ms=max(0, round((_clock() - started) * 1000)),
            exit_code=exit_code,
        )
    if completed.returncode not in (EXIT_COMPLETE, EXIT_HELD, EXIT_RUN_HALTED):
        raise ContractError(f"{program} exited {completed.returncode}")
    return completed.returncode


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_commit(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """The commit this run's code is at, as its caller named it -- or why it has none.

    Read from argv rather than measured here, deliberately. On a pod the
    bootstrap has already checked out the pinned commit and *verified* the
    checkout against it (`operations/pod/bootstrap.py`, REPOSITORY), so the
    plan's value is a proven fact about the running code; re-deriving it here
    would be a second, weaker measurement of something already established, and
    it would make the orchestrator spend a subprocess per process on an answer
    its caller was already holding.

    Returned as a pair, never raising for absence: a run must not be refused
    because the tree it runs from is a source export with no version control,
    and it must equally not record a commit nobody measured (GOVERNANCE 10). An
    absent commit is `None` *with* a reason, so a reader can tell "not
    measured" from "not looked for". A malformed one is a refusal, because a
    short or decorated revision names a commit only against the repository that
    resolved it -- which a fetched run tree no longer has.
    """

    commit = getattr(args, "repository_commit", None)
    if commit is None:
        return None, (
            "no --repository-commit was named for this run; on a pod `pod_run` passes the "
            "commit the bootstrap checked out and verified, and a run driven by hand records "
            "one only when its caller names it"
        )
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ContractError(f"--repository-commit {commit!r} is not a full lowercase Git SHA-1")
    return commit, None


def _record_stage_timing(
    args: argparse.Namespace,
    *,
    program: str,
    extra: dict,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    exit_code: int | None,
) -> None:
    """Append one stage's clock to the timing journal, best effort.

    **Outside the run tree, deliberately.** A run tree is pinned byte-identical
    across a rerun, a resume, a restored backup and every driver mode by a dozen
    acceptance tests, and a clock is by definition not that: putting timings
    under `receipts/` would have made "the same run" mean something weaker for
    every one of those checks. So the journal is a sibling of the pod-run report
    on the volume, where the transcript and the liveness tick already live, and
    `pod_run` is what names it (`--stage-timing-journal`). A local run that names
    no journal writes none, and the run tree is bit-for-bit what it was before.

    Best effort because a stopwatch is a diagnostic, not evidence the run
    depends on: refusing a completed stage because its timing could not be
    written would destroy work to protect a record of it. A failure says so on
    stderr -- which on a pod reaches the durable transcript -- rather than
    passing in silence (hard rule 7).

    Rewritten whole on each append rather than appended to: the file is bounded
    by the number of stage invocations in a run, and a torn append is a journal
    a later reader cannot parse at all. `_atomic_json` replaces it in one step.
    """

    journal = getattr(args, "stage_timing_journal", None)
    if journal is None:
        return
    path = Path(journal)
    subject = extra.get("act")
    entry: dict[str, object] = {
        # The sequence member's own name, not the program's directory: the Door
        # and the Exemplar are two members that share `1_exemplar/`, and an
        # entry calling both of them "1_exemplar" would make a resumed Door
        # unreadable as one.
        "stage": _PROGRAM_NAMES.get(program, program),
        "program": program,
        "operation": str(extra.get("operation", "run")),
        "subject": None if subject is None else str(subject),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
    }
    try:
        # Inside the try with the write, not above it: this runs from a
        # `finally`, and a refusal raised here would replace the stage failure
        # the caller is already propagating.
        commit, commit_detail = repository_commit(args)
        # Recorded per entry, not once at the top: a run resumed at another
        # commit is exactly the case the run authority cannot record (run.json
        # is created once and never rewritten), and two entries naming two
        # commits is what makes that resume visible instead of silent.
        entry["repository_commit"] = commit
        entry["repository_commit_detail"] = commit_detail
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        entries: list = []
        if isinstance(existing, dict):
            # Whose journal this is, before its entries are carried forward. Two
            # runs pointed at one path used to keep the first run's entries and
            # replace the identity above them, so the file then attributed one
            # run's stage timings to another (CodeRabbit on PR #117). A journal
            # that names a different run, root or schema is left exactly as it
            # is and the conflict is reported; the stopwatch never edits a
            # record it cannot account for.
            identity = (
                existing.get("schema"),
                existing.get("run_id"),
                existing.get("run_root"),
            )
            expected = (STAGE_TIMING_JOURNAL_SCHEMA, args.run_id, str(args.run_root))
            if identity != expected:
                raise ContractError(
                    f"the timing journal at {path} already belongs to {identity!r}, and this "
                    f"run is {expected!r}; it was left unchanged rather than merged"
                )
            if isinstance(existing.get("entries"), list):
                entries = list(existing["entries"])
        entries.append(entry)
        _atomic_json(
            path,
            {
                "schema": STAGE_TIMING_JOURNAL_SCHEMA,
                "run_id": args.run_id,
                "run_root": str(args.run_root),
                "entries": entries,
            },
        )
    except Exception as error:  # noqa: BLE001 -- a stopwatch never fails a stage
        print(
            f"run {args.run_id}: the {program} timing entry could not be journaled "
            f"to {path}: {error}",
            file=sys.stderr,
        )


def _atomic_json(path: Path, record: dict) -> None:
    """Replace `path` with `record` or leave what was there, then sync the name.

    The same shape `operations/pod/durable.py` uses, spelled here because this
    module imports only `common/` (see the module docstring) and a half-written
    journal on a volume is exactly the record a later session cannot use.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(record, sort_keys=True, indent=2).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def pending_recoveries(tree: RunTree, recovery_policy: dict) -> list[tuple[str, str, str]]:
    """Checked `(act_id, request_id, recovery_kind)` triples the latest review asks for.

    The kind travels alongside the request id because dispatch depends on it:
    a Designator recrop and a Perlector page-level/continuation-aware reread
    are two distinct operations (ARCHITECTURE, spec 09), and which one a
    request means is not this function's business to decide, only to report.
    """
    by_subject: dict[str, list[dict]] = {}
    for entry in tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "review":
            continue
        record = tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        by_subject.setdefault(record["subject_id"], []).append(record)
    outstanding: list[tuple[str, str, str]] = []
    for subject, records in by_subject.items():
        review = latest_attempt(records, f"Recensor review of {subject}", operation="recense")
        if review["outcome"] != "recovery-requested":
            continue
        # Indexed without a check: `current_recovery_request` refuses a request
        # whose `recovery_kind` is outside `RECOVERY_KINDS` before returning one.
        request = current_recovery_request(tree, subject, recovery_policy)
        recovery_kind = request["payload"]["recovery_kind"]
        outstanding.append((subject, request["artifact_id"], recovery_kind))
    return sorted(outstanding)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--submission-folder")
    parser.add_argument("--submission-manifest")
    parser.add_argument("--triage-decision-manifest", default=None)
    parser.add_argument("--triage-clusters", default=None)
    parser.add_argument("--triage-producer-recipe", default=None)
    # A relative default would bind beside the caller, not inside the repository.
    # `resolve_caller_paths` fills the repository default only for real ingress.
    parser.add_argument("--data-gate-policy", default=None)
    # The fixture declares which scenarios exist; `scenario_for` refuses an
    # undeclared name once the fixture is loaded, so there is no second list here
    # to drift from the declaration.
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--fixture-root", default="proof")
    parser.add_argument(
        "--repository-commit",
        default=None,
        help="the commit this run's code is at, as a full lowercase revision. `pod_run` "
        "passes the one its bootstrap checked out and verified; the Door seals it into the "
        "run authority and every timing entry names it. Absent records no commit",
    )
    parser.add_argument(
        "--stage-timing-journal",
        default=None,
        help="a file outside the run tree to journal each stage invocation's clock and "
        "repository commit into; `pod_run` names one beside the run report on the volume. "
        "Absent means no journal is written, and the run tree is unchanged either way",
    )
    parser.add_argument(
        "--corpus-register",
        default=None,
        help="the append-only corpus register this run is snapshotted against",
    )
    parser.add_argument(
        "--models-config",
        default="config/models.toml",
        help="the sealed model-chair roster and recipes for this run",
    )
    parser.add_argument("--cache-root", default=None)
    parser.add_argument(
        "--mechanics-qualification",
        action="store_true",
        help=(
            "run the full real mechanics with optically unproven profiles; "
            "does not mark any profile proven"
        ),
    )
    parser.add_argument(
        "--decoding-config",
        default=str(DEFAULT_DECODING_CONFIG_PATH),
        help="the sealed decoding posture for record readings and variance experiments",
    )
    # The roster's other half. `--models-config` selects which chairs exist and
    # this selects the vLLM profile each one is served under; both are sealed
    # into `config_digest`, so a run that forwarded one and not the other would
    # let the real roster resolve against the fixture-only catalogue. Unit 17
    # added the flag to `stage_parser` alone, which made the real catalogue
    # unreachable through the only program that invokes the stages.
    parser.add_argument(
        "--serving-recipes-config",
        default=str(DEFAULT_SERVING_RECIPES_CONFIG_PATH),
        help="the sealed serving-profile catalogue for this run; the default is the "
        "fixture-only catalogue",
    )
    parser.add_argument(
        "--perlector-instrument-per-mille",
        type=int,
        default=0,
        help="per-mille rate at which the protocol's selection rule samples acts into "
        "the primed-without-prior control arm (Lectio nuda has its own "
        "--nuda-per-mille); raising it above 0 is Tyrel's, with "
        "--perlector-instrument-approval-ref (config/README.md, R5a toggle register)",
    )
    parser.add_argument(
        "--perlector-instrument-approval-ref",
        default="",
        help="Tyrel's recorded approval reference for a nonzero instrument rate",
    )
    parser.add_argument(
        "--perlector-protocol-config",
        default=str(DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH),
        help="the sealed Perlector prior-draft protocol; its exact bytes enter every "
        "run's config digest",
    )
    parser.add_argument(
        "--draft-fed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="feed the Pass-A draft to Pass B (fed) or withhold it (--no-draft-fed); "
        "changing the default is Tyrel's through B5a (config/README.md, R5a toggle "
        "register)",
    )
    parser.add_argument(
        "--perlector-audit-config", default=str(DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH)
    )
    parser.add_argument(
        "--pdf-render-config",
        default=str(DEFAULT_PDF_RENDER_CONFIG_PATH),
        help="the default whole-page PDF rasterisation target for this run",
    )
    parser.add_argument(
        "--designator-padding-config",
        default=str(DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH),
        help="the capture padding applied to every act crop, sealed into this run",
    )
    parser.add_argument(
        "--designator-geometry-config",
        default=str(DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH),
        help="the sealed Surya/YOLO geometry and crop-policy declaration for this run",
    )
    parser.add_argument(
        "--designator-grouping-config",
        default=str(DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH),
        help=(
            "the sealed grouping, structure and conservation thresholds the Designator "
            "resolves against each page's own dimensions"
        ),
    )
    parser.add_argument(
        "--alignment-config",
        default=str(DEFAULT_ALIGNMENT_CONFIG_PATH),
        help="the sealed limits for page-witness alignment",
    )
    parser.add_argument(
        "--formats-config",
        default=str(DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH),
        help="the sealed Armarium product projections for this run",
    )
    parser.add_argument(
        "--pdf-target-dpi",
        type=int,
        default=None,
        help="override the configured PDF target for this run only",
    )
    parser.add_argument(
        "--recovery-config",
        default=str(DEFAULT_RECOVERY_CONFIG_PATH),
        help="the bounded recovery policy sealed into this run",
    )
    parser.add_argument(
        "--hard-failure-config",
        default=str(DEFAULT_HARD_FAILURE_CONFIG_PATH),
        help="the run-level hard-failure cap this orchestrator checkpoints against",
    )
    parser.add_argument(
        "--placement-tier",
        default=None,
        help=(
            "the measured placement tier of the card serving this run; forwarded "
            "to every stage when set, omitted (not sealed) otherwise — see "
            "stage_parser's own flag for the full rationale"
        ),
    )
    parser.add_argument(
        "--witness-context",
        default="named",
        choices=WITNESS_CONTEXT_REGIMES,
        help="the run-level named/blinded toggle the Perlector's dossier is built under",
    )
    parser.add_argument(
        "--witness-context-config",
        default=str(DEFAULT_WITNESS_CONTEXT_CONFIG_PATH),
        help="the Perlector-owned factual witness-context declaration this run seals",
    )
    parser.add_argument(
        "--nuda-per-mille",
        type=int,
        default=0,
        help="the sealed Lectio nuda sampling rate, in thousandths (0 disables it)",
    )
    parser.add_argument(
        "--nuda-approval-ref",
        default="",
        help="Tyrel's reference for the predeclared Lectio nuda sampling design",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--all",
        action="store_true",
        help="run the complete automatic sequence (the default)",
    )
    selection.add_argument(
        "--stage",
        choices=SEQUENCE_NAMES,
        help="run exactly one boundary operation (manual mode)",
    )
    selection.add_argument(
        "--from",
        dest="from_stage",
        choices=SEQUENCE_NAMES,
        help="first boundary operation in an inclusive semi-mode range",
    )
    parser.add_argument(
        "--to",
        dest="to_stage",
        choices=SEQUENCE_NAMES,
        help="last boundary operation in an inclusive semi-mode range",
    )
    parser.add_argument(
        "--mode",
        choices=RUN_MODES,
        default=None,
        help="driver vocabulary: manual (one stage), semi (a range), or auto (all)",
    )
    args = parser.parse_args()

    require_coherent_ingress_options(args)
    resolve_caller_paths(args)
    # Both argv facts the journal rests on, proved here rather than at the
    # first entry that happens to need them (CodeRabbit on PR #117).
    #
    # `repository_commit` refuses a short or decorated revision, and it used to
    # be reached only from `_record_stage_timing` -- so a manual or semi run
    # that started past the Door, or any run with no journal configured, could
    # carry a malformed value through every stage it selected and record it
    # nowhere.
    repository_commit(args)
    # And the journal is outside the run tree, as its own help text says. A
    # journal at `<run-root>/<run-id>/timings.json` would add mutable,
    # untracked bytes to an immutable tree once per stage invocation and change
    # its byte identity; nothing refused it before.
    journal = getattr(args, "stage_timing_journal", None)
    if journal is not None:
        journal_path = Path(journal).resolve()
        run_directory = (Path(args.run_root) / args.run_id).resolve()
        if journal_path == run_directory or journal_path.is_relative_to(run_directory):
            raise ContractError(
                f"--stage-timing-journal {journal_path} is inside this run's own tree at "
                f"{run_directory}; the journal is mutable and the tree is not, so it is "
                "written outside the tree or not at all"
            )

    # Prove the algebra total before anything runs. A stage added later without a
    # class or a terminal decision should fail at the first run, not at the first
    # unusual page.
    check_algebra_is_total()

    # Real run authority seals neither fixture identity nor fixture scenario.
    if args.submission_folder is None:
        fixture = load_fixture(args.fixture_root)
        if fixture["fixture_id"] != args.fixture:
            raise ContractError(
                f"asked for fixture {args.fixture!r} but {args.fixture_root} declares "
                f"{fixture['fixture_id']!r}"
            )
        scenario_for(fixture, args.scenario)

    names, mode = selected_sequence(args)

    tree = RunTree(Path(args.run_root), args.run_id)
    # Every checkpoint shares this object so the cap cannot move mid-run. A
    # resume proves it before entry; a new run cannot prove it until Door creates
    # the run authority, so run_sequence proves that first boundary instead.
    hard_failure_policy = load_hard_failure_policy(args.hard_failure_config)
    if tree.resolve("run.json").exists():
        require_sealed_config(
            run_sealed_config_digests(tree.read_run()),
            "hard-failure",
            hard_failure_policy["config_sha256"],
        )
        halted = checkpoint(args, "resume-preflight", hard_failure_policy)
        if halted is not None:
            report_halt(args, halted)
            return EXIT_RUN_HALTED
    return run_sequence(args, names, mode, hard_failure_policy)


def selected_sequence(args: argparse.Namespace) -> tuple[tuple[str, ...], str]:
    """Resolve one contiguous selection into the ruled driver vocabulary.

    A range is deliberately inclusive and contiguous.  A non-contiguous set would
    look exactly like an unrecorded skipped boundary, which a staged run must never
    turn into an operator convenience.
    """
    if args.to_stage is not None and args.from_stage is None:
        raise ContractError("--to requires --from; a semi run is an inclusive stage range")
    if args.from_stage is not None:
        if args.to_stage is None:
            raise ContractError("--from requires --to; a semi run must name both boundaries")
        first = SEQUENCE_NAMES.index(args.from_stage)
        last = SEQUENCE_NAMES.index(args.to_stage)
        if first > last:
            raise ContractError(
                f"--from {args.from_stage!r} comes after --to {args.to_stage!r}; "
                "a semi run cannot run a boundary backwards"
            )
        names, inferred_mode = SEQUENCE_NAMES[first : last + 1], "semi"
    elif args.stage is not None:
        names, inferred_mode = (args.stage,), "manual"
    else:
        names, inferred_mode = SEQUENCE_NAMES, "auto"
    if args.mode is not None and args.mode != inferred_mode:
        raise ContractError(
            f"--mode {args.mode!r} conflicts with this selection's {inferred_mode!r} mode"
        )
    return names, inferred_mode


def run_sequence(
    args: argparse.Namespace,
    names: tuple[str, ...],
    mode: str,
    hard_failure_policy: dict,
) -> int:
    """Run one contiguous selection without persisting its driver mode."""
    for name in names:
        if name == "recovery":
            # Recovery has no program whose open_context can verify Recensor;
            # Archetypus's predecessor mapping names that required boundary.
            recovery_tree = RunTree(Path(args.run_root), args.run_id)
            # Isolated sequencing tests mock every stage and intentionally have
            # no run tree; real recovery invocations always have run.json.
            if recovery_tree.resolve("run.json").exists():
                verify_predecessor_seal(recovery_tree, "archetypus")
            halted = drive_recovery(args, hard_failure_policy)
            if halted is not None:
                report_halt(args, halted)
                return EXIT_RUN_HALTED
            halted = checkpoint(args, name, hard_failure_policy)
            if halted is not None:
                report_halt(args, halted)
                return EXIT_RUN_HALTED
            continue

        result = invoke(STAGE_PROGRAMS[name], args)
        if result == EXIT_RUN_HALTED:
            halted = _entry_halt(args, name, hard_failure_policy)
            report_halt(args, halted)
            return EXIT_RUN_HALTED
        if name == "door" and result in (EXIT_COMPLETE, EXIT_HELD):
            require_sealed_config(
                run_sealed_config_digests(RunTree(Path(args.run_root), args.run_id).read_run()),
                "hard-failure",
                hard_failure_policy["config_sha256"],
            )
        # The cap and its exact-threshold warning take precedence over every
        # held exit, including an Attestatores hold whose outcome is not counted.
        halted = checkpoint(args, name, hard_failure_policy)
        if halted is not None:
            report_halt(args, halted)
            return EXIT_RUN_HALTED
        # An Attestatores hold means its attempt tally is unestablished, so no
        # later member may advance even when the stage already sealed evidence.
        if name == ATTESTATORES and result == EXIT_HELD:
            print(f"run {args.run_id}: held; its reason is on stderr above")
            return EXIT_HELD
        # Armarium is terminal; returning here would discard its named partial reasons.
        if mode in ("semi", "manual") and result == EXIT_HELD and name != "armarium":
            print(f"run {args.run_id}: {mode} mode stopped at held {name}")
            return EXIT_HELD

    # A staged selection that stops before the Armarium has produced no export to
    # report on; every halt in the loop above has already reported itself and
    # returned EXIT_RUN_HALTED, so reaching here means the selection ran out.
    if names[-1] != "armarium":
        return EXIT_COMPLETE
    tree = RunTree(Path(args.run_root), args.run_id)
    # Armarium has no stage successor, so the orchestrator consumes and proves its
    # final boundary before it reads the export inside that boundary. The
    # consumer-keyed predecessor helper would re-read Archetypus rather than
    # Armarium's own seal, and `verify_final_seal` additionally returns the exact
    # export bytes represented by the one manifest snapshot it checked -- reopening
    # by path after verification would leave a check/use window at the last
    # reporting boundary in the run.
    export = verify_final_seal(tree)
    status, lines = terminal_report(export)
    print(f"run {args.run_id}: {status}")
    for line in lines:
        print(f"  - {line}")
    return EXIT_COMPLETE if status == "complete" else EXIT_HELD


def terminal_report(export: dict) -> tuple[str, list[str]]:
    """The run's verdict, taken from the Armarium's own terminal outcome.

    Deriving it again from `payload["aggregate"]` was a second, weaker derivation of
    a question the last stage had already answered: the Armarium reports its terminal
    ledger's status, which subsumes the aggregate's and is partial in one case the
    aggregate is not (7_armarium/HANDOFF.md). A run whose bundle said `partial` on its
    own face would have printed `complete` and exited 0 here.

    The reasons stay the aggregate's, because they are the ones an operator acts on
    and every reachable run's two statuses agree. When they do not, the ledger's own
    unresolved units are on the bundle's face and this says where to read them rather
    than reporting a partial run with nothing named.
    """
    payload = export.get("payload")
    aggregate = payload.get("aggregate") if isinstance(payload, dict) else None
    if not isinstance(aggregate, dict):
        raise ContractError("the Armarium export has no aggregate object for its terminal report")
    aggregate_status = aggregate.get("status")
    reasons_value = aggregate.get("reasons")
    if aggregate_status not in {"complete", "partial"} or not isinstance(reasons_value, list):
        raise ContractError("the Armarium export has a malformed aggregate status or reasons list")
    if any(not isinstance(reason, str) or not reason for reason in reasons_value):
        raise ContractError("the Armarium export carries a blank or non-string terminal reason")
    outcome = export.get("outcome")
    if outcome not in {
        ArmariumCategory.DELIVERED.value,
        ArmariumCategory.HELD_FOR_REVIEW.value,
    }:
        raise ContractError(f"the Armarium export has unsupported terminal outcome {outcome!r}")
    complete = outcome == ArmariumCategory.DELIVERED.value
    if complete and (aggregate_status != "complete" or reasons_value):
        raise ContractError(
            "the Armarium export claims delivered while its aggregate remains partial or "
            "names unresolved reasons; the orchestrator refuses to report complete over a conflict"
        )
    reasons = list(reasons_value)
    if not complete and not reasons:
        reasons.append(
            "the export bundle's terminal ledger is partial while the run aggregate "
            "reconciled; its unresolved units are named in EXPORT_MANIFEST.json's "
            "claims.partial_reasons"
        )
    return ("complete" if complete else "partial"), reasons


def checkpoint(args, checkpoint_name: str, hard_failure_policy: dict) -> dict | None:
    """Recompute the run-level hard-failure tally from disk; the tally if breached.

    The boundary's own name travels back inside the tally, so the halt below can
    say which section finished rather than only that one did. Two hard failures
    is Tyrel's named "early warning" and stops nothing; more than two halts the
    run at this exact boundary.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    tally = tally_hard_failures(tree, hard_failure_policy)
    if tally["instrument_count"]:
        print(
            f"run {args.run_id}: {tally['instrument_count']} Perlector instrument failure(s) "
            "retained separately; they do not consume Tyrel's production hard-failure cap"
        )
    if tally["count"] == tally["threshold"] and tally["count"] > 0:
        print(
            f"run {args.run_id}: {tally['count']} hard failure(s) so far — Tyrel's ruling "
            f"treats this as an early warning; one more halts the run at the next checkpoint"
        )
    return dict(tally, checkpoint=checkpoint_name) if tally["breached"] else None


def report_halt(args, tally: dict) -> None:
    """The one place this halt is said out loud. GOVERNANCE 2: not lost silently."""
    print(
        f"run {args.run_id}: halted at the {tally['checkpoint']} checkpoint — {tally['count']} "
        f"hard failure(s) exceed the run-level cap of {tally['threshold']} (Tyrel's ruling: "
        f"more than {tally['threshold']} needs fixing, not another automatic stage). The "
        "section already in flight finished; nothing further was invoked"
    )
    for kind, subjects in tally["by_kind"].items():
        if subjects:
            print(f"  - {kind}: {subjects}")


def undispatchable_recovery_reason(recovery_kind: str, *, real_route: bool) -> str | None:
    """Why this orchestrator cannot answer one outstanding request, or `None`.

    Only the recrop operation has a real implementation today, and on a real
    submission not even that: `pipeline/2_designator/run.py` refuses
    `--operation recover` by name, because a recovery still reads the fixture's
    declared rectangle. Refusing any other kind loudly is what naming the kind
    exists to stop — a silent conflation with a substitute crop — and naming the
    real route here is what stops the same refusal reaching an operator as a bare
    `pipeline/2_designator/run.py exited 2` with the cause a stage away
    (findings F068/F083).

    The Recensor no longer publishes a real-ingress request, so this branch is a
    backstop over trees written before that gate landed. It is still checked,
    because a bound nobody checks is not a bound.
    """
    if recovery_kind != FALLBACK_RECROP:
        return (
            f"names recovery_kind {recovery_kind!r}, which this orchestrator has no dispatch "
            f"for; only {FALLBACK_RECROP!r} (a Designator recrop) is implemented today, and "
            "the page-level reread belongs to the Perlector, which has not built it"
        )
    if real_route:
        return (
            "is a fallback recrop on a real submission, which the Designator refuses by name: "
            "a recovery still reads the fixture's declared rectangle, which a real submission "
            "does not carry. This request predates the gate that now withholds it. Nothing "
            "supersedes it in this run tree: the Designator will not cut the recrop, and the "
            "Recensor holds the act without republishing while the request is outstanding, so "
            "no sequence of stage invocations reaches an export here and this run ends with "
            "none. Its coverage evidence stays readable in the request artifact and the "
            "Recensor review beside it; a fresh run of the same submission from the Door does "
            "not reach this state, because the Recensor now holds such an act for review "
            "instead of publishing a request"
        )
    return None


def report_undispatchable_recoveries(args, refused: list[tuple[str, str, str, str]]) -> None:
    """Say every refused dispatch out loud, by act, before the run stops.

    GOVERNANCE 2, and the one place this refusal is recorded. The orchestrator
    keeps no file of its own (the module docstring says why: resume is a property
    of the artifacts, never of a checkpoint that could disagree with them), so its
    record of a dispatch it would not make is the run's own output — and it names
    every affected act, not only the one the raised exception happens to carry.
    The durable evidence stays where it was published: each request artifact and
    its `recovery-requested` Recensor review are immutable in the run tree, and
    nothing here writes to or changes them.

    Written to stderr, which is the half of this program's output the operator
    surface keeps: it records a failed run's detail as
    `completed.stderr or completed.stdout` (`operations/operator/surface.py`),
    and the `ContractError` raised immediately after this is printed to stderr
    by the entry point below — so stderr is never empty on this path and a
    per-act listing on stdout would be dropped from the receipt and never seen.
    A record the one consumer discards is GOVERNANCE 2 claimed, not met.
    """
    print(
        f"run {args.run_id}: recovery cannot be dispatched for {len(refused)} outstanding "
        "request(s); no stage was invoked and nothing in the run tree was changed",
        file=sys.stderr,
    )
    for act_id, request_id, recovery_kind, reason in refused:
        print(
            f"  - act {act_id} (request {request_id}, kind {recovery_kind}): {reason}",
            file=sys.stderr,
        )


def drive_recovery(args, hard_failure_policy: dict) -> dict | None:
    """Dispatch every outstanding recovery request, then re-read and re-review.

    The Recensor decides an act needs a wider crop; the Designator is the only
    stage that cuts one. Keeping that ownership is why recovery lives here and not
    inside the Recensor, where it would be one short step from a stage recropping
    its own evidence until it liked it.

    Every round screens the whole outstanding batch before dispatching any of
    it (`undispatchable_recovery_reason`), so a request nothing here can answer
    refuses by its own cause and leaves no half-finished round behind it, and
    every refused act is recorded before the refusal is raised.

    Returns the hard-failure tally if the run-level cap trips partway through.
    A recovery round is one completed Designator section followed by one
    completed Perlector section followed by one Recensor pass, and the cap is
    checked at each of those three boundaries — never between two acts of the
    same batch. That is Tyrel's own shape for the cap ("if errors happened in
    chandra stage it finishes that section but pauses"): a section already in
    flight finishes, and a second act whose recrop was already approved is not
    left without its owning stage's answer.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    recovery_policy = load_recovery_policy(args.recovery_config)
    # One read of the run authority, used for both the sealed-policy proof below
    # and the ingress route the dispatch screen consults. Read here rather than
    # before the policy load, so the order in which those two can refuse is the
    # order it always was.
    run = tree.read_run()
    # The orchestrator is not a stage and holds no `StageContext`, so it proves the
    # policy it dispatches under against the digests the run authority recorded for
    # itself. Without this, the dispatcher bounded the whole recovery loop — the
    # round ceiling and every request it checked — on whatever `config/recovery.toml`
    # said at this moment, which need not be what the run sealed (audit S3 names
    # this the third point of use). Checked before the first round, so a swapped
    # policy stops the loop rather than being discovered by the stage it dispatched.
    require_sealed_config(
        run_sealed_config_digests(run), "recovery", recovery_policy["config_sha256"]
    )
    # Read after the sealed-policy proof, not before it. `is_real_ingress` parses
    # the run's ingress record and can refuse on a malformed one, so reading it
    # first would let a run carrying both a bad ingress record and a swapped
    # recovery policy report the former while the policy this loop is bounded by
    # is still unproven. The proof that bounds the dispatch comes first; the
    # route the dispatch screen consults comes after it.
    real_route = is_real_ingress(run)
    maximum_rounds = recovery_policy["absolute_cap"]

    for round_number in range(maximum_rounds + 1):
        outstanding = pending_recoveries(tree, recovery_policy)
        if not outstanding:
            return None
        if round_number == maximum_rounds:
            raise ContractError(
                f"recovery is still outstanding for {outstanding} after "
                f"{maximum_rounds} rounds. The run-bound policy stops the loop"
            )
        # Checked for the whole batch before any of it is dispatched, so an
        # unanswerable request does not leave half a round behind it, and
        # recorded act by act before the refusal is raised so the run says which
        # requests it could not answer rather than only that one existed.
        refused = []
        for act_id, request_id, recovery_kind in outstanding:
            reason = undispatchable_recovery_reason(recovery_kind, real_route=real_route)
            if reason is not None:
                refused.append((act_id, request_id, recovery_kind, reason))
        if refused:
            # A refusal, not a per-act hold, and that is a decision rather than
            # an omission. Holding the refused acts and dispatching the rest
            # would not give this run an export: `recovery-requested` maps to no
            # terminal Armarium category (`common/contracts/outcomes.py`), so the
            # Armarium refuses the act fatally whatever this function does, and
            # skipping here would only move the same dead end a stage later while
            # losing the named cause at the boundary that knows it. Turning it
            # into an export instead would mean making `recovery-requested`
            # terminal, which would also let a fixture run whose recovery was
            # simply never driven deliver as a partial — a genuinely half-driven
            # run reported as a finished one. So this run stops here and says so;
            # what the tree keeps is the immutable request and its review.
            report_undispatchable_recoveries(args, refused)
            first_act, _first_request, _first_kind, first_reason = refused[0]
            raise ContractError(f"act {first_act}'s outstanding recovery request {first_reason}")
        for act_id, request_id, _recovery_kind in outstanding:
            result = invoke(
                STAGE_PROGRAMS[DESIGNATOR],
                args,
                operation="recover",
                act=act_id,
                recovery_request=request_id,
            )
            if result == EXIT_RUN_HALTED:
                return _entry_halt(args, DESIGNATOR, hard_failure_policy)
        tally = checkpoint(args, DESIGNATOR, hard_failure_policy)
        if tally is not None:
            return tally
        for act_id, _request_id, _recovery_kind in outstanding:
            result = invoke(STAGE_PROGRAMS["perlector"], args, act=act_id)
            if result == EXIT_RUN_HALTED:
                return _entry_halt(args, "perlector", hard_failure_policy)
        tally = checkpoint(args, "perlector", hard_failure_policy)
        if tally is not None:
            return tally
        result = invoke(STAGE_PROGRAMS[RECENSOR], args)
        if result == EXIT_RUN_HALTED:
            return _entry_halt(args, RECENSOR, hard_failure_policy)
        tally = checkpoint(args, RECENSOR, hard_failure_policy)
        if tally is not None:
            return tally
    return None


def _entry_halt(args, stage: str, hard_failure_policy: dict) -> dict:
    """Return the named tally that a direct stage-entry refusal already proved."""
    tally = checkpoint(args, f"{stage}-entry", hard_failure_policy)
    if tally is None:
        raise ContractError(
            f"{stage} returned EXIT_RUN_HALTED but its hard-failure tally is not breached"
        )
    return tally


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
