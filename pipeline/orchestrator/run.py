"""The orchestrator: sequencing and resume. It is not a stage.

Its home is decided here, once: `pipeline/orchestrator/`, a peer of the numbered
stage directories rather than one of them. It is stage-neutral, imports only
`common/`, and invokes stages **as programs** — real subprocesses, real argv, real
exit codes. That last part is meta-invariant #90's requirement made concrete: one
harness runs the real orchestration end to end offline, so a green Python suite can
never stand in for a pipeline that was never actually executed.

It establishes nothing and reads nothing except the outcome bookkeeping it needs to
sequence and to checkpoint. Its three jobs:

  Sequence.   Door, Exemplar, Ink Map, Designator, Attestatores, Perlector,
              Recensor, Archetypus, Coniector, Armarium, in that order. The
              Coniector reads the Perlector's readings; only the Armarium reads
              what it writes.
  Checkpoint. After every stage invocation, the run-level
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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.alignment import DEFAULT_ALIGNMENT_CONFIG_PATH  # noqa: E402
from common.armarium_formats import DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH  # noqa: E402
from common.background import DEFAULT_INK_MAP_CONFIG_PATH  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.outcomes import ArmariumCategory, check_algebra_is_total  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    CONIECTOR,
    INK_MAP,
    RECENSOR,
)
from common.credentials import looks_like_credential_env  # noqa: E402
from common.durability import atomic_create  # noqa: E402
from common.hard_failure import (  # noqa: E402
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.page_accounting import DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH  # noqa: E402
from common.page_review import held_by_recensor, held_share  # noqa: E402
from common.reconstruction import DEFAULT_RECONSTRUCTION_CONFIG_PATH  # noqa: E402
from common.recovery import DEFAULT_RECOVERY_CONFIG_PATH  # noqa: E402
from common.review_policy import DEFAULT_REVIEW_CONFIG_PATH, alarm_line  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    DEFAULT_DECODING_CONFIG_PATH,
    DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH,
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
    boundary_advanced,
    current_stage_seal,
    load_fixture,
    require_sealed_config,
    run_sealed_config_digests,
    scenario_for,
    verify_final_seal,
)

DESCRIPTION = "The orchestrator: sequencing and resume. It is not a stage."

ROOT = Path(__file__).resolve().parents[2]
_TRIAGE_PATHS = ("triage_decision_manifest", "triage_clusters", "triage_producer_recipe")

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
    ("archetypus", "pipeline/6_archetypus/run.py"),
    (CONIECTOR, "pipeline/4b_coniector/run.py"),
    ("armarium", "pipeline/7_armarium/run.py"),
)

STAGE_PROGRAMS = dict(SEQUENCE)
_PROGRAM_NAMES = {program: name for name, program in STAGE_PROGRAMS.items()}
SEQUENCE_NAMES = tuple(STAGE_PROGRAMS)
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
# although `stage_environment` does not loop over it directly (below): the
# reconciliation test still pins this exact set against the transfer's own.
_TRANSFER_CREDENTIAL_ENV = frozenset({"RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"})
# The wall clock a timing receipt is stamped with, and the monotonic one its
# duration is measured against. Kept separate deliberately: a duration taken
# from wall-clock differences is wrong across a clock adjustment, and a
# monotonic reading names no instant a reader could compare across records.
_clock = time.monotonic
STAGE_TIMING_JOURNAL_SCHEMA = "stage-timing-journal.v4"
# How one invocation ended, for the caller that named `--stop-record`.
STOP_RECORD_SCHEMA = "orchestrator-stop.v1"

GPU_QUERY = (
    "nvidia-smi",
    "--query-gpu=utilization.gpu,memory.used",
    "--format=csv,noheader,nounits",
)
GPU_SAMPLE_INTERVAL_SECONDS = 5.0
GPU_SAMPLES_KEPT = 720
GPU_BUSY_PERCENT = 95
GPU_SAMPLER_JOIN_SECONDS = 30


class GpuSampler:
    """Polls GPU utilisation while a stage runs, so the journal can say how busy it kept the card.

    Each read is parsed per card and per field: a field the driver reports as
    `[N/A]` is `None` and costs nothing else. A sample's utilisation is its
    busiest card's, and the busy measure counts samples above 95%. Mean, max
    and busy fraction cover every read; only the stored list is capped. A read
    with no utilisation is counted in `failed_reads` and never becomes a zero:
    with no successful read the result is `None` plus the reason. The sampler
    never raises into the stage. A thread still alive after the join timeout is
    abandoned and its partial statistics are never published. `run`, `interval`
    and `join_timeout` are injectable for tests.
    """

    def __init__(
        self,
        run=subprocess.run,
        interval: float = GPU_SAMPLE_INTERVAL_SECONDS,
        join_timeout: float = GPU_SAMPLER_JOIN_SECONDS,
    ):
        self._run = run
        self._interval = interval
        self._join_timeout = join_timeout
        self._abandoned = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="gpu-sampler", daemon=True)
        self._started = False
        self._samples: list[dict[str, object]] = []
        self._count = 0
        self._sum = 0
        self._max = 0
        self._busy = 0
        self._failed = 0
        self._reason: str | None = None

    def __enter__(self) -> "GpuSampler":
        try:
            self._thread.start()
            self._started = True
        except Exception as error:  # noqa: BLE001 -- the stage runs unmeasured
            self._reason = f"sampling could not start: {type(error).__name__}: {error}"
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=self._join_timeout)
            self._abandoned = self._thread.is_alive()

    def _loop(self) -> None:
        while True:
            try:
                self._read()
            except Exception as error:  # noqa: BLE001 -- a gauge never fails a stage
                self._fail(f"{type(error).__name__}: {error}")
            if self._stop.wait(self._interval):
                return

    def _fail(self, reason: str) -> None:
        self._failed += 1
        self._reason = self._reason or reason

    @staticmethod
    def _field(text: str) -> int | None:
        try:
            return int(text.strip())
        except ValueError:
            return None

    def _read(self) -> None:
        result = self._run(list(GPU_QUERY), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            self._fail(f"nvidia-smi exited {result.returncode}: {result.stderr.strip()[:200]}")
            return
        cards = []
        for line in result.stdout.strip().splitlines():
            fields = line.split(",")
            cards.append(
                {
                    "utilization_percent": self._field(fields[0]),
                    "memory_used_mib": self._field(fields[1]) if len(fields) > 1 else None,
                }
            )
        utilizations = [
            c["utilization_percent"] for c in cards if c["utilization_percent"] is not None
        ]
        if not utilizations:
            self._fail(f"nvidia-smi gave no utilisation reading: {result.stdout.strip()[:200]!r}")
            return
        utilization = max(utilizations)
        memories = [c["memory_used_mib"] for c in cards if c["memory_used_mib"] is not None]
        self._count += 1
        self._sum += utilization
        self._max = max(self._max, utilization)
        self._busy += utilization > GPU_BUSY_PERCENT
        self._samples.append(
            {
                "utilization_percent": utilization,
                "memory_used_mib": max(memories) if memories else None,
                "cards": cards,
            }
        )
        del self._samples[:-GPU_SAMPLES_KEPT]

    def result(self) -> tuple[dict[str, object] | None, str | None]:
        """The journal's `gpu_utilization` and, when it is `None`, why."""

        if self._abandoned:
            return None, f"the GPU sampler did not stop within {self._join_timeout:g} s"
        if not self._count:
            return None, self._reason or "the stage ended before the first read"
        return {
            "interval_seconds": self._interval,
            "samples": list(self._samples),
            "sample_count": self._count,
            "failed_reads": self._failed,
            "first_failure_reason": self._reason,
            "mean": self._sum / self._count,
            "max": self._max,
            "busy_fraction_over_95": self._busy / self._count,
        }, None


def require_coherent_ingress_options(args: argparse.Namespace) -> None:
    if (getattr(args, "canary_folder", None) is None) != (
        getattr(args, "canary_manifest", None) is None
    ):
        raise ContractError("--canary-folder and --canary-manifest must be supplied together")
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
    if getattr(args, "canary_folder", None) is not None:
        raise ContractError("canary input requires a real submission folder")
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
        "canary_folder",
        "canary_manifest",
        *_TRIAGE_PATHS,
        "cache_root",
        "store_root",
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
        name: value for name, value in os.environ.items() if not looks_like_credential_env(name)
    }


def _argv(pairs, *, omit_unset: bool = False) -> list[str]:
    return [
        part
        for flag, value in pairs
        if not (omit_unset and value is None)
        for part in (flag, str(value))
    ]


def _require_absolute_caller_paths(args: argparse.Namespace) -> None:
    for attribute in (
        "run_root",
        "submission_folder",
        "submission_manifest",
        "canary_folder",
        "canary_manifest",
        "data_gate_policy",
        *_TRIAGE_PATHS,
        "cache_root",
        "store_root",
    ):
        value = getattr(args, attribute, None)
        if value is not None and not Path(value).is_absolute():
            flag = "--" + attribute.replace("_", "-")
            raise ContractError(
                f"{flag} is still the caller-relative path {str(value)!r}. Stages run from "
                f"{ROOT} while the caller may be anywhere, so this must be resolved at the "
                "orchestration boundary (`resolve_caller_paths`) before any child sees it"
            )


def invoke(program: str, args: argparse.Namespace) -> int:
    """Run one stage as a program and return its exit code."""
    require_coherent_ingress_options(args)
    _require_absolute_caller_paths(args)
    command = [
        sys.executable,
        # Ignore PYTHON* startup controls and the user site for child stages.
        # The stage scripts add the repository root themselves; accepting an
        # operator's PYTHONPATH/sitecustomize here would execute unsealed code
        # before the stage reached its first refusal boundary.
        "-I",
        str(ROOT / program),
        *_argv(
            (
                ("--run-root", args.run_root),
                ("--run-id", args.run_id),
                ("--scenario", args.scenario),
                ("--fixture-root", args.fixture_root),
                ("--models-config", args.models_config),
                ("--decoding-config", args.decoding_config),
                ("--serving-recipes-config", args.serving_recipes_config),
                ("--pdf-render-config", args.pdf_render_config),
                ("--designator-geometry-config", args.designator_geometry_config),
                ("--alignment-config", args.alignment_config),
                ("--page-accounting-config", args.page_accounting_config),
                ("--reconstruction-config", args.reconstruction_config),
                ("--ink-map-config", args.ink_map_config),
                ("--formats-config", args.formats_config),
                ("--recovery-config", args.recovery_config),
                ("--hard-failure-config", args.hard_failure_config),
                ("--review-config", args.review_config),
            )
        ),
    ]
    command += _argv((("--cache-root", getattr(args, "cache_root", None)),), omit_unset=True)
    command += _argv((("--store-root", getattr(args, "store_root", None)),), omit_unset=True)
    # Later stages may read only the run tree the Door sealed, never source paths.
    if program == STAGE_PROGRAMS["door"]:
        # Only the Door creates the run authority, so only it can seal the commit.
        # An unread commit is omitted, never a placeholder.
        commit, _detail = repository_commit(args)
        command += _argv(
            (
                ("--repository-commit", commit),
                ("--submission-folder", args.submission_folder),
                ("--submission-manifest", args.submission_manifest),
                ("--canary-folder", getattr(args, "canary_folder", None)),
                ("--canary-manifest", getattr(args, "canary_manifest", None)),
                ("--data-gate-policy", args.data_gate_policy),
                ("--triage-decision-manifest", getattr(args, "triage_decision_manifest", None)),
                ("--triage-clusters", getattr(args, "triage_clusters", None)),
                ("--triage-producer-recipe", getattr(args, "triage_producer_recipe", None)),
            ),
            omit_unset=True,
        )
    # The placement tier is a measured runtime fact of the card, not run
    # configuration, so an unset one is omitted and stage_parser's
    # own default (None) governs.
    command += _argv(
        (("--pdf-target-dpi", args.pdf_target_dpi), ("--placement-tier", args.placement_tier)),
        omit_unset=True,
    )
    if getattr(args, "mechanics_qualification", False):
        command.append("--mechanics-qualification")
    # Forwarded to every stage, not only to the door that snapshots it: the
    # drift refusal exists to catch a register appended *between* two stages of
    # one run, which is precisely the case an unforwarded flag cannot see.
    command += _argv((("--corpus-register", args.corpus_register),), omit_unset=True)
    command += _argv(
        (
            ("--witness-context", args.witness_context),
            ("--witness-context-config", args.witness_context_config),
            ("--perlector-protocol-config", args.perlector_protocol_config),
            ("--perlector-audit-config", args.perlector_audit_config),
        )
    )
    if program == STAGE_PROGRAMS["perlector"]:
        # A scheduling choice, not run configuration: unsealed, so a resume may change it.
        command += _argv(
            (("--perlector-concurrency", getattr(args, "perlector_concurrency", None)),),
            omit_unset=True,
        )

    # Streams are inherited, not buffered: stage output is unbounded, and a
    # partial Door's private refusal report must reach the operator's terminal.
    #
    # `stage_environment()` is not optional and is the reason this call is not a
    # bare subprocess.run: it drops the transfer credentials from every stage's
    # environment, so only the upload-only verb can ever see them.
    started = _clock()
    started_at = _stamp()
    # Bound before the try: set inside it, an interrupted stage would leave it
    # unbound and `finally` would raise a NameError that hides the real error.
    exit_code: int | None = None
    finished: tuple[str, float] | None = None
    sampler = GpuSampler()
    try:
        with sampler:
            try:
                completed = subprocess.run(command, cwd=ROOT, env=stage_environment())
            finally:
                # The stage's end, taken before the sampler's shutdown can add to it.
                finished = (_stamp(), _clock())
        exit_code = completed.returncode
    finally:
        finished_at, ended = finished or (_stamp(), _clock())
        # The invocations a reader most wants timed are the ones that went wrong.
        _record_stage_timing(
            args,
            program=program,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, round((ended - started) * 1000)),
            exit_code=exit_code,
            gpu_utilization=sampler.result(),
        )
    if completed.returncode not in (EXIT_COMPLETE, EXIT_HELD, EXIT_RUN_HALTED):
        raise ContractError(f"{program} exited {completed.returncode}")
    return completed.returncode


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError(f"{value!r} is not a positive count")
    return number


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_commit(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """The commit this run's code is at, as its caller named it -- or why it has none.

    Read from argv, not measured: on a pod the bootstrap already checked out and
    verified the pinned commit (`operations/pod/bootstrap.py`). Absence is
    `None` with a reason, never a refusal, since a source export has no version
    control. A short or decorated revision is refused: it names a
    commit only against the repository that resolved it.
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
    started_at: str,
    finished_at: str,
    duration_ms: int,
    exit_code: int | None,
    gpu_utilization: tuple[dict[str, object] | None, str | None],
) -> None:
    """Append one stage's clock to the timing journal, best effort.

    Outside the run tree, because the tree is pinned byte-identical across
    reruns and resumes and a clock is not. Best effort, because refusing a
    completed stage over its stopwatch would destroy work to protect a record
    of it; a failure is said on stderr.
    """

    journal = getattr(args, "stage_timing_journal", None)
    if journal is None:
        return
    path = Path(journal)
    entry: dict[str, object] = {
        "schema": STAGE_TIMING_JOURNAL_SCHEMA,
        "run_id": args.run_id,
        "run_root": str(args.run_root),
        # The Door and the Exemplar share `1_exemplar/`, so name the member.
        "stage": _PROGRAM_NAMES.get(program, program),
        "program": program,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        # `None` plus a reason when no read succeeded: not taken, which is not
        # the same as zero or as a pass.
        "gpu_utilization": gpu_utilization[0],
        "gpu_utilization_reason": gpu_utilization[1],
        # What the Perlector was asked to keep in flight; `None` is its served row's
        # `max_num_seqs`. The width it used follows from this, the sealed row and the
        # sealed draft setting, and the stage prints it. A scheduling choice, so
        # journaled rather than sealed; each record holds the reply its call received.
        "perlector_concurrency": (
            getattr(args, "perlector_concurrency", None)
            if program == STAGE_PROGRAMS["perlector"]
            else None
        ),
    }
    try:
        # Inside the try: this runs from a `finally`, and a refusal here would
        # replace the stage failure already propagating. A resume at another
        # commit is visible in each line; run.json is never rewritten.
        commit, commit_detail = repository_commit(args)
        entry["repository_commit"] = commit
        entry["repository_commit_detail"] = commit_detail
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(0)
                first_line = handle.readline()
                try:
                    first = json.loads(first_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("existing timing journal has no valid first line") from error
                if (
                    not isinstance(first, dict)
                    or first.get("schema") != STAGE_TIMING_JOURNAL_SCHEMA
                ):
                    raise ValueError(
                        f"existing timing journal is not {STAGE_TIMING_JOURNAL_SCHEMA}"
                    )
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write(json.dumps(entry, sort_keys=True).encode("utf-8") + b"\n")
    except Exception as error:  # noqa: BLE001 -- a stopwatch never fails a stage
        print(
            f"run {args.run_id}: the {program} timing entry could not be journaled "
            f"to {path}: {error}",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--submission-folder")
    parser.add_argument("--submission-manifest")
    parser.add_argument("--canary-folder")
    parser.add_argument("--canary-manifest")
    parser.add_argument("--triage-decision-manifest", default=None)
    parser.add_argument("--triage-clusters", default=None)
    parser.add_argument("--triage-producer-recipe", default=None)
    # No default: a relative one would bind beside the caller (see resolve_caller_paths).
    parser.add_argument("--data-gate-policy", default=None)
    # No choices: the fixture declares its scenarios and `scenario_for` refuses others.
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
        "--stop-record",
        default=None,
        help="a new file outside the run tree that this invocation writes as it ends, "
        "saying whether it reached a sealed Armarium export; `pod_run` names a fresh one "
        "for each invocation and keeps the pod toward its deadline only on its word. "
        "Absent means none is written",
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
    parser.add_argument("--store-root", default=None)
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
        help="the sealed decoding posture of every reading chair",
    )
    # The roster's other half, forwarded with `--models-config`: without it the
    # real roster would resolve against the fixture-only catalogue. Declared here
    # because this is the only program that invokes the stages.
    parser.add_argument(
        "--serving-recipes-config",
        default=str(DEFAULT_SERVING_RECIPES_CONFIG_PATH),
        help="the sealed serving-profile catalogue for this run; the default is the "
        "fixture-only catalogue",
    )
    parser.add_argument(
        "--perlector-protocol-config",
        default=str(DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH),
        help="the sealed Perlector protocol (page feed, page render, truncation); its "
        "exact bytes enter every run's config digest",
    )
    parser.add_argument(
        "--perlector-audit-config", default=str(DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH)
    )
    parser.add_argument(
        "--perlector-concurrency",
        type=_positive_int,
        default=None,
        help="Perlector reader calls kept in flight at once on a live chair; absent means "
        "the served row's max_num_seqs, and 1 reads one page at a time",
    )
    parser.add_argument(
        "--pdf-render-config",
        default=str(DEFAULT_PDF_RENDER_CONFIG_PATH),
        help="the default whole-page PDF rasterisation target for this run",
    )
    parser.add_argument(
        "--designator-geometry-config",
        default=str(DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH),
        help="the sealed Surya/YOLO geometry and crop-policy declaration for this run",
    )
    parser.add_argument(
        "--alignment-config",
        default=str(DEFAULT_ALIGNMENT_CONFIG_PATH),
        help="the sealed step budget of the Perlector's dissent comparisons",
    )
    parser.add_argument(
        "--ink-map-config",
        default=str(DEFAULT_INK_MAP_CONFIG_PATH),
        help="the sealed ink-measurement policy: background, page-spanning bound, coverage audit",
    )
    parser.add_argument(
        "--page-accounting-config",
        default=str(DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH),
        help="the sealed thresholds of the check that a page reading missed nothing",
    )
    parser.add_argument(
        "--reconstruction-config",
        default=str(DEFAULT_RECONSTRUCTION_CONFIG_PATH),
        help="the sealed Coniector switches: whether its chair runs, and whether the pages "
        "are consecutive leaves of one register",
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
        help="the re-ask budget sealed into this run",
    )
    parser.add_argument(
        "--hard-failure-config",
        default=str(DEFAULT_HARD_FAILURE_CONFIG_PATH),
        help="the run-level hard-failure cap this orchestrator checkpoints against",
    )
    parser.add_argument(
        "--review-config",
        default=str(DEFAULT_REVIEW_CONFIG_PATH),
        help=(
            "the share of the run's pages that may stay held after the recensor before the "
            "run itself is called systemic, sealed into this run"
        ),
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
    # Proved up front, not lazily from `_record_stage_timing`: a manual or semi
    # run that starts past the Door, or one with no journal, would otherwise
    # carry a malformed revision through every stage and record it nowhere.
    repository_commit(args)
    _require_journal_outside_run_tree(args)
    _require_fresh_stop_record(args)
    # A stage added later without a class or a terminal decision should fail at
    # the first run, not at the first unusual page.
    check_algebra_is_total()
    # Real run authority seals neither fixture identity nor fixture scenario.
    if args.submission_folder is None:
        _require_declared_fixture(args)
    names, mode = selected_sequence(args)

    tree = _run_tree(args)
    # Every checkpoint shares this object so the cap cannot move mid-run. A
    # resume proves it before entry; a new run cannot prove it until Door creates
    # the run authority, so run_sequence proves that first boundary instead.
    hard_failure_policy = load_hard_failure_policy(args.hard_failure_config)
    if tree.resolve("run.json").exists():
        _require_sealed_hard_failure_policy(tree.read_run(), hard_failure_policy)
        halted = checkpoint(args, "resume-preflight", hard_failure_policy)
        if halted is not None:
            return _halt(args, halted)
    return run_sequence(args, names, mode, hard_failure_policy)


def _require_journal_outside_run_tree(args: argparse.Namespace) -> None:
    """The journal is mutable; inside the immutable run tree it would change the tree's bytes."""
    journal = getattr(args, "stage_timing_journal", None)
    if journal is None:
        return
    journal_path = Path(journal).resolve()
    run_directory = (Path(args.run_root) / args.run_id).resolve()
    if journal_path == run_directory or journal_path.is_relative_to(run_directory):
        raise ContractError(
            f"--stage-timing-journal {journal_path} is inside this run's own tree at "
            f"{run_directory}; the journal is mutable and the tree is not, so it is "
            "written outside the tree or not at all"
        )


def _require_fresh_stop_record(args: argparse.Namespace) -> None:
    """The stop record is this invocation's alone: new, and outside the immutable run tree."""
    record = getattr(args, "stop_record", None)
    if record is None:
        return
    record_path = Path(record).resolve()
    run_directory = (Path(args.run_root) / args.run_id).resolve()
    if record_path == run_directory or record_path.is_relative_to(run_directory):
        raise ContractError(
            f"--stop-record {record_path} is inside this run's own tree at {run_directory}; "
            "it is written outside the tree"
        )
    if record_path.exists() or record_path.is_symlink():
        raise ContractError(
            f"--stop-record {record_path} already exists; it must be new, so no earlier "
            "invocation's stop can be read as this one's"
        )


def _record_stop(args: argparse.Namespace, exit_code: int, *, exported: bool) -> None:
    """Write `--stop-record`, when one was named, saying how this invocation ended.

    `exported` is true only when this invocation ran the Armarium and proved its
    sealed export; a stop before it is false whatever export the tree already
    holds. `systemic` is the systemic alarm line this invocation printed, at a
    held Recensor or at an advance past it, or null. A record that cannot be written is said on stderr and leaves the run
    as it is: its caller reads no record as no export.
    """
    record = getattr(args, "stop_record", None)
    if record is None:
        return
    payload = {
        "schema": STOP_RECORD_SCHEMA,
        "run_id": args.run_id,
        "exit_code": exit_code,
        "exported": exported,
        # The systemic alarm line this invocation printed (`report_systemic_share`),
        # so a caller that reads no transcript can still say it.
        "systemic": getattr(args, "systemic_line", None),
    }
    try:
        atomic_create(Path(record), json.dumps(payload, sort_keys=True).encode("utf-8"))
    except OSError as error:
        print(f"run {args.run_id}: the stop record could not be written: {error}", file=sys.stderr)


def _require_declared_fixture(args: argparse.Namespace) -> None:
    fixture = load_fixture(args.fixture_root)
    if fixture["fixture_id"] != args.fixture:
        raise ContractError(
            f"asked for fixture {args.fixture!r} but {args.fixture_root} declares "
            f"{fixture['fixture_id']!r}"
        )
    scenario_for(fixture, args.scenario)


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
    """Run one contiguous selection (`_drive`), and record how it ended (`_record_stop`)."""
    exit_code, exported = _drive(args, names, mode, hard_failure_policy)
    _record_stop(args, exit_code, exported=exported)
    return exit_code


def _drive(
    args: argparse.Namespace,
    names: tuple[str, ...],
    mode: str,
    hard_failure_policy: dict,
) -> tuple[int, bool]:
    """Run one contiguous selection without persisting its driver mode.

    Returns the exit and whether this invocation reached a sealed Armarium export.

    In every mode, a Recensor that holds anything stops the run before the
    first of the Archetypus and the Armarium it selects, so nothing is
    established or exported over a hold no person has looked at. Both stages
    refuse such a Recensor at their own entry too
    (`common.page_review.require_recensor_passed`); the check here names
    every hold and how to go on. The Coniector, which reads only the
    Perlector's readings, still runs when the selection includes it, so what is
    left needs no model. The run continues past the Recensor once nothing is
    held, or once an advance record passes its current seal.
    """
    held_recensor: list[dict] | None = None
    first_after_recensor: str | None = None
    ran_after_hold: list[str] = []
    for name in names:
        if held_recensor is not None and name != CONIECTOR:
            continue
        if name in (ARCHETYPUS, ARMARIUM) and first_after_recensor is None:
            first_after_recensor = name
            held = recensor_holds(args)
            if held:
                held_recensor = held
                continue
        result = invoke(STAGE_PROGRAMS[name], args)
        if held_recensor is not None:
            ran_after_hold.append(name)
        if result == EXIT_RUN_HALTED:
            return _halt(args, _entry_halt(args, name, hard_failure_policy)), False
        if name == "door" and result in (EXIT_COMPLETE, EXIT_HELD):
            _require_sealed_hard_failure_policy(_run_tree(args).read_run(), hard_failure_policy)
        # The cap and its exact-threshold warning take precedence over every
        # held exit, including an Attestatores hold whose outcome is not counted.
        halted = checkpoint(args, name, hard_failure_policy)
        if halted is not None:
            return _halt(args, halted), False
        # An Attestatores hold means its attempt tally is unestablished, so no
        # later member may advance even when the stage already sealed evidence.
        if name == ATTESTATORES and result == EXIT_HELD:
            print(f"run {args.run_id}: held; its reason is on stderr above")
            return EXIT_HELD, False
        # A range that ends at the Armarium runs through held boundaries as auto mode
        # does, so its export names every hold; the Armarium is terminal either way.
        if mode in ("semi", "manual") and result == EXIT_HELD and names[-1] != "armarium":
            print(f"run {args.run_id}: {mode} mode stopped at held {name}")
            if name == RECENSOR:
                report_held_recensor(args, held_by_recensor(_run_tree(args)), ran_after_hold)
            return EXIT_HELD, False

    if held_recensor is not None:
        print(f"run {args.run_id}: stopped at a held recensor, before the {first_after_recensor}")
        report_held_recensor(args, held_recensor, ran_after_hold)
        return EXIT_HELD, False
    if names[-1] != "armarium":
        return EXIT_COMPLETE, False
    # Armarium has no successor, so its own seal is proved here. The export comes
    # from the one manifest snapshot `verify_final_seal` checked: reopening it by
    # path afterwards would leave a check/use window at the last boundary.
    export = verify_final_seal(_run_tree(args))
    status, lines = terminal_report(export)
    print(f"run {args.run_id}: {status}")
    for line in lines:
        print(f"  - {line}")
    return (EXIT_COMPLETE if status == "complete" else EXIT_HELD), True


def recensor_holds(args) -> list[dict]:
    """What the Recensor holds that stops this run before the Archetypus; empty to continue.

    Its current holds (`common.page_review.held_by_recensor`), unless a
    person's advance record passes the Recensor's current seal
    (`common.stage.boundary_advanced`), which the run then says it relied on.
    """
    tree = _run_tree(args)
    # An unsealed Recensor is the Archetypus's to refuse, by name, at its entry.
    if current_stage_seal(tree, RECENSOR) is None:
        return []
    held = held_by_recensor(tree)
    if held and boundary_advanced(tree, RECENSOR):
        # A person's advance passes even a systemic share; the alarm still sounds,
        # and the export carries it as a reason.
        report_systemic_share(args)
        print(
            f"run {args.run_id}: the recensor holds {len(held)} item(s); an advance record "
            "passes its current seal, so the run continues and the export names every hold"
        )
        return []
    return held


def report_systemic_share(args) -> None:
    """Print the systemic alarm line when the run's held share is above its sealed limit.

    The line (`common.review_policy.alarm_line`) is what the operator's
    notification carries, and the stop record names it (`_record_stop`). A run
    that sealed no review policy says the share was not checked.
    """
    tree = _run_tree(args)
    share = held_share(tree, run_sealed_config_digests(tree.read_run()), args.review_config)
    if share is None:
        print(
            f"run {args.run_id}: this run sealed no review policy, so whether its held share "
            "is systemic was not checked"
        )
    elif share["systemic"]:
        args.systemic_line = alarm_line(args.run_id, share["held_pages"], share["pages"], share)
        print(args.systemic_line)


def report_held_recensor(args, held: list[dict], ran_after_hold: list[str]) -> None:
    """Say what a held Recensor holds and how the run goes on from it.

    When more of the run's pages are held than its sealed review policy allows
    (`common.review_policy`), the report opens with the systemic alarm line,
    which the operator's notification carries.
    """
    report_systemic_share(args)
    print(
        f"  the recensor holds {len(held)} item(s), and nothing is exported until they are decided:"
    )
    for item in held:
        codes = ", ".join(item["hold_codes"])
        print(f"  - {item['what']} ({item['subject_id']})" + (f": {codes}" if codes else ""))
    if ran_after_hold:
        print(
            f"  {', '.join(ran_after_hold)} ran after the hold: it reads only the perlector's "
            "readings, so what is left needs no model"
        )
    print(
        "  next: record operator review decisions in this run, then resume it from the "
        "recensor (--from recensor --to armarium), which applies them; it continues past the "
        "recensor once nothing is held, or once `verbatus advance --stage recensor` passes "
        "its current seal"
    )


def terminal_report(export: dict) -> tuple[str, list[str]]:
    """The run's verdict, taken from the Armarium's own terminal outcome.

    The terminal ledger's status subsumes the aggregate's and is partial in one
    case the aggregate is not (7_armarium/CONTRACT.md). The reasons stay the
    aggregate's, which are what an operator acts on; when it has none, this
    says where the ledger names its unresolved units.
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
    is the project lead's named "early warning" and stops nothing; more than two halts the
    run at this exact boundary.
    """
    tree = _run_tree(args)
    tally = tally_hard_failures(tree, hard_failure_policy)
    if tally["count"] == tally["threshold"] and tally["count"] > 0:
        print(
            f"run {args.run_id}: {tally['count']} hard failure(s) so far — the project lead's ruling "
            f"treats this as an early warning; one more halts the run at the next checkpoint"
        )
    return dict(tally, checkpoint=checkpoint_name) if tally["breached"] else None


def report_halt(args, tally: dict) -> None:
    """The one place this halt is said out loud. Not lost silently."""
    print(
        f"run {args.run_id}: halted at the {tally['checkpoint']} checkpoint — {tally['count']} "
        f"hard failure(s) exceed the run-level cap of {tally['threshold']} (the project lead's ruling: "
        f"more than {tally['threshold']} needs fixing, not another automatic stage). The "
        "section already in flight finished; nothing further was invoked"
    )
    for kind, subjects in tally["by_kind"].items():
        if subjects:
            print(f"  - {kind}: {subjects}")


def _run_tree(args) -> RunTree:
    return RunTree(Path(args.run_root), args.run_id)


def _halt(args, tally: dict) -> int:
    report_halt(args, tally)
    return EXIT_RUN_HALTED


def _require_sealed_hard_failure_policy(run: dict, hard_failure_policy: dict) -> None:
    require_sealed_config(
        run_sealed_config_digests(run), "hard-failure", hard_failure_policy["config_sha256"]
    )


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
