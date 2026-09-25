"""``python -m operations.pod.pod_run`` -- bootstrap, run the orchestrator, hold.

The one tracked entrypoint that runs the pipeline on a pod.  It is composed
from ``bootstrap_main`` rather than beside it: the argv after ``--`` is a
complete ``bootstrap_main`` argv, prepared and run through that module's own
``prepare``/``run_bootstrap``, so every refusal, the write probe, the
credential scrub and the hard deadline are the same ones a plain bootstrap
gets.  Only after that bootstrap journal is green does this process start the
orchestrator (``pipeline/orchestrator/run.py``) as a subprocess of the pod's
own interpreter, over the volume::

    run root            <volume>/runs           (or --run-root, inside the volume)
    submission          --submission-folder / --submission-manifest, inside the volume
    roster              the bootstrap plan's --models-config
    serving catalogue   the bootstrap plan's --serving-recipes-config
    witness context     the bootstrap plan's --witness-context-config
    data gate           --data-gate-policy, inside the repository

The roster and the serving catalogue are deliberately taken from the bootstrap
plan and not accepted again here: ``PREFLIGHT`` measured that roster against
that catalogue, and a run that named different files would serve chairs no
preflight had looked at.

**Exit codes never read "complete" for a partial run (principle 2).**
``EXIT_COMPLETE`` (0) is returned only when the orchestrator itself returned
``EXIT_COMPLETE``; ``EXIT_HELD`` (3) and ``EXIT_HALTED`` (4) mirror the
orchestrator's own held and halted exits; ``EXIT_REFUSED`` (2) is a named
refusal before anything ran; ``EXIT_BOOTSTRAP_RED`` (5) is a red bootstrap
step, the orchestrator never started; ``EXIT_FAILED`` (6) is an orchestrator
that could not start or exited outside its own vocabulary; ``EXIT_DRY_RUN``
(7) is a drill -- both plans printed, nothing run, no report written -- and is
never 0, so a dry run launched as ``pod_timer``'s bootstrap child by mistake
cannot be mistaken for a completed run.  Whatever the outcome, the report at
``--report-path`` says the same thing durably, under the launch-bound name,
before the exit code says it -- except the dry run, which writes no report at
all (see below).

**The bootstrap-and-hold contract is unchanged for a run that finished.**
``pod_timer.run_with_bootstrap`` treats any child exit before the hard deadline
-- exit 0 included -- as ``completed-early`` and closes the pod with a non-green
timer report, so after a ``complete`` or ``held`` run this process holds to the
shared hard deadline exactly as ``bootstrap_main`` does, re-journaling a
liveness line beside the run report.  That hold is paid idle time between a
finished run and the deadline; closing early on a complete run would be a
``pod_timer`` contract change and is not made here.

**Nothing the run printed dies with the pod.**  The orchestrator's stdout and
stderr -- and, through inheritance, every stage's -- are teed into a bounded,
launch-token-named transcript beside the run report, and the report names it by
path.  A liveness line beside them carries the child's pid and the moment it
was last seen, re-journaled on the same interval the hold loop uses, so a
supervisor killed mid-run leaves a stale tick rather than a record that still
says ``running`` (principle 2).

**A run that did not finish returns instead, and the pod closes.**  ``halted``,
``failed``, and "the orchestrator could not start" get no hold: holding one of
those bills a rented card at the sealed hourly rate, to the hard deadline, for
a run that produced nothing further -- exactly what the red-bootstrap branch
already refuses to pay.  There is no path that holds such a pod, with or
without anyone's permission.  Nothing is lost by leaving: the run
tree, both reports, the journal and the preflight evidence are on the
*volume*, which outlives the pod, and ``verbatus fetch-run`` reads it over S3
with no pod running.  The run report records which way it went in
``held_to_hard_deadline``, so the choice is in the durable record and not
only here (principle 2).

**No placement-tier flag.**  The consult that asked for this entrypoint named
``--placement-tier``; neither the orchestrator nor any stage parser accepts one
as the code stands, and no stage reads a tier.  The tier the sealed launch
measured is the one thing this process can honestly carry: it is read from the
green bootstrap's ``PREFLIGHT`` receipt and recorded in the run report, and a
green bootstrap whose receipt carries no tier is refused by name.

**The data gate is checked before the bootstrap spends anything.**  The
orchestrator's Door refuses a submission folder outside the policy's approved
storage roots.  ``config/data_handling_policy.json`` now names the pod volume
mount path (``operations/pod/boot_a_request.py``'s sealed
``volume_mount_path``) beside the local ``private/`` root -- that listing was
a disclosure decision the project lead made once rather than
per-launch.  This process asks the gate the same question first, so a launch
whose submission folder is outside every listed root is refused here, by
name, before a model is fetched on a billing card rather than after.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Sequence

from common.contracts.errors import ContractError
from common.contracts.identities import validate_run_id
from common.stage import EXIT_COMPLETE as ORCHESTRATOR_COMPLETE
from common.stage import EXIT_FATAL as ORCHESTRATOR_FATAL
from common.stage import EXIT_HELD as ORCHESTRATOR_HELD
from common.stage import EXIT_RUN_HALTED as ORCHESTRATOR_HALTED
from operations.serving.config import ServingConfigInputs
from operations.serving.errors import ServingConfigurationError
from operations.submit import gate

from . import boot_a_request, bootstrap_main
from .bootstrap import BootstrapActions, BootstrapReport
from .bootstrap_main import (
    DEFAULT_PROOF_FIXTURE,
    Plan,
    PlanRefusal,
    _require_contained,
    _require_launch_token_named,
    build_actions,
    hold,
    refuse_credential_looking_argv,
)
from .durable import atomic_write, canonical_json
from .models import utc_now

RUN_REPORT_SCHEMA = "pod-run-report.v1"
RUN_REFUSAL_SCHEMA = "pod-run-refusal.v1"
RUN_LIVENESS_SCHEMA = "pod-run-liveness.v1"
DEFAULT_RUNS_DIRECTORY = "runs"

# The transcript's two bounds. The head is written to the volume as it arrives,
# so a process killed mid-run still leaves the beginning of the run durable; the
# tail is the last window kept in memory and appended at close, because the
# traceback or refusal that explains a stopped run is at the *end* of a stream
# whose middle nobody needs. Between them a transcript cannot grow without
# limit on a volume whose space the run tree also needs -- which is the one
# objection the inherited-streams comment this replaces actually had.
TRANSCRIPT_HEAD_BYTES = 8 * 1024 * 1024
TRANSCRIPT_TAIL_BYTES = 1 * 1024 * 1024
# How long the reader thread is waited for after the orchestrator itself has
# exited. The pipe reaches end of file only when every holder closes it, and a
# stray descendant of the orchestrator can hold it long after the orchestrator
# is gone; an unbounded join there would keep `_run` from returning and the
# final run report from ever being written. The child
# is dead by then, so nothing this waits for is the run's own output.
TRANSCRIPT_READER_JOIN_SECONDS = 30.0
# The stage-timing journal is read back at close to audit it; a file past this
# bound is not a journal the orchestrator wrote and is not read whole.
TIMING_JOURNAL_READ_BYTES = 4 * 1024 * 1024

EXIT_COMPLETE = 0
EXIT_REFUSED = 2
EXIT_HELD = 3
EXIT_HALTED = 4
EXIT_BOOTSTRAP_RED = 5
EXIT_FAILED = 6
EXIT_DRY_RUN = 7

_STATE_FOR_EXIT = {
    EXIT_COMPLETE: "complete",
    EXIT_REFUSED: "refused",
    EXIT_HELD: "held",
    EXIT_HALTED: "halted",
    EXIT_BOOTSTRAP_RED: "bootstrap-red",
    EXIT_FAILED: "failed",
    EXIT_DRY_RUN: "dry-run",
}

_ORCHESTRATOR_EXITS = {
    ORCHESTRATOR_COMPLETE: EXIT_COMPLETE,
    ORCHESTRATOR_HELD: EXIT_HELD,
    ORCHESTRATOR_HALTED: EXIT_HALTED,
}

# Which outcomes are worth paying the rest of the lease for. `pod_timer.
# run_with_bootstrap` reads any child exit before the hard deadline as
# `completed-early` and closes the pod with a non-green timer report, so a run
# that finished its work has to hold: closing early on a complete run would
# turn a good run into a non-green timer record, and that is a `pod_timer`
# contract change this unit does not make. A run that did *not* finish has no
# such claim on the meter. `halted`, `failed`, and "the orchestrator could not
# start" hold a rented card, at the sealed hourly rate, until the deadline for
# nothing -- the same waste the bootstrap-red branch above already refuses to
# pay; no permission opens a hold for such a run. The
# evidence argument does not save the hold either: the run tree, the reports
# and the preflight evidence are all on the *volume*, which outlives the pod
# and is read by `verbatus fetch-run` over S3 with no pod running at all.
_HOLD_AFTER_EXITS = frozenset({EXIT_COMPLETE, EXIT_HELD})

# `argv, *, cwd, env, transcript, liveness, interval_seconds`. The last three are
# what makes the child's output durable and its aliveness visible; a runner that
# ignored them would put both back inside the container. The returned
# `CompletedProcess.stderr` is the runner's report about its own tee: `None`
# when the transcript is whole, a string naming why it is not.
Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class RunRefusal(PlanRefusal):
    """A named pre-run refusal; the orchestrator never started."""


@dataclass(frozen=True, slots=True)
class RunPlan:
    """Every explicit, tracked input of the run half, resolved against the bootstrap plan."""

    bootstrap: Plan
    report_path: Path
    run_id: str
    run_root: Path
    submission_folder: Path
    submission_manifest: Path
    data_gate_policy: Path
    fixture: str
    interval_seconds: float
    dry_run: bool
    triage_decision_manifest: Path | None = None
    triage_clusters: Path | None = None
    triage_producer_recipe: Path | None = None
    corpus_register: Path | None = None

    # Not asserts: `assert` disappears under `python -O`, and `resolve_run_plan`
    # already refused a bootstrap plan missing any of these. Stated as raises so
    # a hand-built RunPlan fails by name rather than with an AttributeError.
    @property
    def models_config(self) -> Path:
        return _named(self.bootstrap.models_config, "--models-config")

    @property
    def serving_recipes_config(self) -> Path:
        return _named(self.bootstrap.serving_recipes_config, "--serving-recipes-config")

    @property
    def witness_context_config(self) -> Path:
        """The factual witness-context declaration this run seals.

        Named on the plan beside the roster, never defaulted here:
        `bootstrap_main.resolve_plan` supplies the default when none is named.
        After checkout, the journaled CONFIGURATION step checks known shipped
        sentences against their present witness identities before environment
        or model work. This property receives that resolved path selection;
        it does not choose another declaration for the orchestrator.
        """
        return _named(self.bootstrap.witness_context_config, "--witness-context-config")

    @property
    def repository(self) -> Path:
        return _named(self.bootstrap.repository, "--repository")

    @property
    def hold_path(self) -> Path:
        """The liveness line after the run, beside the run report, never over it."""

        return self.report_path.with_name(f"{self.report_path.stem}-hold{self.report_path.suffix}")

    @property
    def liveness_path(self) -> Path:
        """The liveness line *during* the run, beside the run report, never over it.

        `hold_path` covers the paid idle time after a finished run; this covers
        the run itself, which is the longer and more dangerous window. Without
        it the only durable statement on the volume for the whole duration of a
        run is a `running` record with no heartbeat, and a pod_run killed by the
        OOM killer or by the container teardown leaves that record as its final
        word -- a partial result that does not look partial (principle 2).
        """

        return self.report_path.with_name(
            f"{self.report_path.stem}-liveness{self.report_path.suffix}"
        )

    @property
    def transcript_path(self) -> Path:
        """The orchestrator's merged stdout/stderr, beside the run report.

        Without it, a refusal, traceback or hold reason for a stage that
        inherits the orchestrator's streams goes only to a container log that
        RunPod destroys with the pod, unreachable from the volume the run
        report points a reader to. Teeing here, once, captures the whole
        inherited stream tree with one pipe.

        Launch-token-named like every other record beside it: `report_path`
        has already been refused unless its own name carries the token
        (`_require_launch_token_named`), and this is derived from that name.
        """

        return self.report_path.with_name(f"{self.report_path.stem}-transcript.log")

    @property
    def timing_journal_path(self) -> Path:
        """Each stage's clock and commit, beside the run report, outside the run tree.

        Outside deliberately: a run tree is pinned byte-identical across a
        rerun, a resume, a restored backup and every driver mode by a dozen
        acceptance tests, and a clock is by definition not that. So the one
        record that says how long the Perlector took, and which commit ran each
        stage, is a sibling of this report -- fetched by the same derived key
        set as the transcript and the liveness tick, and destroyed with the
        volume like the rest of them if nobody fetches it.
        """

        return self.report_path.with_name(
            f"{self.report_path.stem}-timings{self.report_path.suffix}"
        )

    @property
    def repository_commit(self) -> str:
        """The commit the bootstrap checked out and *verified*, forwarded to the run.

        Not re-derived by the orchestrator: `REPOSITORY` already read the
        checkout back and refused a tip that was not the pinned commit
        (`bootstrap.py`), so this is a proven fact about the code that is about
        to run, and a second measurement of it could only be weaker.
        """

        commit = self.bootstrap.repository_commit
        if commit is None:
            raise RunRefusal(
                "the bootstrap plan names no --repository-commit; a run cannot say which "
                "code produced its tree without one"
            )
        return commit

    def orchestrator_argv(self) -> list[str]:
        command = [
            sys.executable,
            # Ignore PYTHON* startup controls and the user site, as the
            # orchestrator does for its own stages: nothing unsealed runs first.
            "-I",
            str(self.repository / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            self.fixture,
            "--run-id",
            self.run_id,
            "--run-root",
            str(self.run_root),
            "--submission-folder",
            str(self.submission_folder),
            "--submission-manifest",
            str(self.submission_manifest),
            "--data-gate-policy",
            str(self.data_gate_policy),
            "--models-config",
            str(self.models_config),
            "--serving-recipes-config",
            str(self.serving_recipes_config),
            "--witness-context-config",
            str(self.witness_context_config),
            "--stage-timing-journal",
            str(self.timing_journal_path),
            "--repository-commit",
            self.repository_commit,
        ]
        cache_root = _named(self.bootstrap.cache_root, "--cache-root")
        command += ["--cache-root", str(cache_root)]
        for value, flag in (
            (self.triage_decision_manifest, "--triage-decision-manifest"),
            (self.triage_clusters, "--triage-clusters"),
            (self.triage_producer_recipe, "--triage-producer-recipe"),
            (self.corpus_register, "--corpus-register"),
        ):
            if value is not None:
                command += [flag, str(value)]
        return command

    def to_record(self) -> dict[str, object]:
        return {
            "report_path": str(self.report_path),
            "run_id": self.run_id,
            "run_root": str(self.run_root),
            "submission_folder": str(self.submission_folder),
            "submission_manifest": str(self.submission_manifest),
            "data_gate_policy": str(self.data_gate_policy),
            "models_config": str(self.models_config),
            "serving_recipes_config": str(self.serving_recipes_config),
            "witness_context_config": str(self.witness_context_config),
            "fixture": self.fixture,
            "interval_seconds": self.interval_seconds,
            "dry_run": self.dry_run,
            "triage_decision_manifest": str(self.triage_decision_manifest)
            if self.triage_decision_manifest
            else None,
            "triage_clusters": str(self.triage_clusters) if self.triage_clusters else None,
            "triage_producer_recipe": str(self.triage_producer_recipe)
            if self.triage_producer_recipe
            else None,
            "corpus_register": str(self.corpus_register) if self.corpus_register else None,
            "bootstrap": self.bootstrap.to_record(),
        }


def _named(value: Path | None, flag: str) -> Path:
    if value is None:
        raise RunRefusal(f"the bootstrap plan names no {flag}; a run cannot proceed without it")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verbatus pod-side run: bootstrap, orchestrate over the volume, hold",
        allow_abbrev=False,
        epilog="the bootstrap_main argv follows a literal -- and is required",
    )
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path, help="defaults to <volume-mount-path>/runs")
    parser.add_argument("--submission-folder", type=Path, required=True)
    parser.add_argument("--submission-manifest", type=Path, required=True)
    parser.add_argument("--triage-decision-manifest", type=Path)
    parser.add_argument("--triage-clusters", type=Path)
    parser.add_argument("--triage-producer-recipe", type=Path)
    parser.add_argument("--corpus-register", type=Path)
    parser.add_argument(
        "--data-gate-policy",
        type=Path,
        help="defaults to <repository>/config/data_handling_policy.json",
    )
    parser.add_argument(
        "--fixture",
        default=DEFAULT_PROOF_FIXTURE,
        help="the orchestrator requires a fixture name even for a real submission; "
        "a real run seals neither its identity nor its scenario",
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """The run half and the bootstrap half, at the first literal ``--``."""

    if "--" not in argv:
        raise RunRefusal(
            "pod_run needs the bootstrap_main argv after a literal --; a run with no "
            "bootstrap plan has nothing green to run after"
        )
    index = list(argv).index("--")
    return list(argv[:index]), list(argv[index + 1 :])


def resolve_run_plan(
    args: argparse.Namespace, bootstrap: Plan, launch_token: str | None
) -> RunPlan:
    """Validate the run half against the already-resolved bootstrap plan."""

    # The report path first, so every later refusal has somewhere durable to go.
    volume = bootstrap.volume_mount_path
    report_path = _require_contained(args.report_path, volume, "--report-path")
    _require_launch_token_named(report_path, launch_token, "--report-path", report_path=report_path)
    # boot_a_request.py seals BOOT_A_VOLUME_MOUNT_PATH into every real launch
    # request; a directory at exactly that path that is not actually mounted
    # is an unmounted local substitute on the pod's own ephemeral disk, not
    # the approved network volume -- write_probe (bootstrap_main.py) only
    # proves the path is a writable directory, and gate.resolve_storage_roots
    # only proves it exists, so neither catches this on its own. This check
    # is scoped to the one path a real launch actually seals, not to every
    # --volume-mount-path a drill or test may name, so a plain temporary
    # directory used as a stand-in volume elsewhere is unaffected.
    if str(volume) == boot_a_request.BOOT_A_VOLUME_MOUNT_PATH and not os.path.ismount(volume):
        raise RunRefusal(
            f"--volume-mount-path {volume} is the pod's expected network-volume mount "
            "point, but this machine does not have anything mounted there; an unmounted "
            "local directory at that path is not the approved storage root, whatever "
            "gate.resolve_storage_roots would otherwise admit for it existing and being "
            "a directory",
            report_path=report_path,
        )
    if bootstrap.hold_only:
        raise RunRefusal(
            "pod_run needs a full bootstrap plan; --hold-only is the drill and runs nothing",
            report_path=report_path,
        )
    if bootstrap.repository is None or bootstrap.models_config is None:
        raise RunRefusal(
            "pod_run needs a bootstrap plan that names its repository and roster",
            report_path=report_path,
        )
    if report_path == bootstrap.report_path:
        raise RunRefusal(
            "--report-path is the bootstrap's own report path; the run report and the "
            "bootstrap report are two records and may not overwrite each other",
            report_path=report_path,
        )
    try:
        run_id = validate_run_id(args.run_id)
    except ContractError as error:
        raise RunRefusal(f"--run-id refused: {error}", report_path=report_path) from error
    run_root = _require_contained(
        args.run_root or (volume / DEFAULT_RUNS_DIRECTORY),
        volume,
        "--run-root",
        report_path=report_path,
    )
    submission_folder = _require_contained(
        args.submission_folder, volume, "--submission-folder", report_path=report_path
    )
    if not submission_folder.is_dir():
        raise RunRefusal(
            f"--submission-folder {submission_folder} is not a directory on the volume; "
            "nothing was uploaded there, or the transfer prefix differs",
            report_path=report_path,
        )
    submission_manifest = _require_contained(
        args.submission_manifest, volume, "--submission-manifest", report_path=report_path
    )
    if not submission_manifest.is_file():
        raise RunRefusal(
            f"--submission-manifest {submission_manifest} is not a file on the volume",
            report_path=report_path,
        )
    triage_paths: dict[str, Path | None] = {}
    for value, flag in (
        (args.triage_decision_manifest, "--triage-decision-manifest"),
        (args.triage_clusters, "--triage-clusters"),
        (args.triage_producer_recipe, "--triage-producer-recipe"),
        (args.corpus_register, "--corpus-register"),
    ):
        if value is None:
            triage_paths[flag] = None
            continue
        path = _require_contained(value, volume, flag, report_path=report_path)
        if not path.is_file():
            raise RunRefusal(f"{flag} {path} is not a file on the volume", report_path=report_path)
        triage_paths[flag] = path
    if (
        triage_paths["--triage-clusters"] is not None
        or triage_paths["--triage-producer-recipe"] is not None
    ) and triage_paths["--triage-decision-manifest"] is None:
        raise RunRefusal(
            "--triage-clusters and --triage-producer-recipe require --triage-decision-manifest",
            report_path=report_path,
        )
    repository = bootstrap.repository
    data_gate_policy = _require_contained(
        args.data_gate_policy or (repository / "config" / "data_handling_policy.json"),
        repository,
        "--data-gate-policy",
        base_label="the checked-out repository",
        report_path=report_path,
    )
    if not data_gate_policy.is_file():
        raise RunRefusal(
            f"--data-gate-policy {data_gate_policy} is not a file in the checked-out repository",
            report_path=report_path,
        )
    if not isinstance(args.fixture, str) or not args.fixture.strip():
        raise RunRefusal("--fixture must be a non-blank fixture name", report_path=report_path)
    interval = bootstrap_main._positive_interval(args.interval_seconds, report_path=report_path)
    return RunPlan(
        bootstrap=bootstrap,
        report_path=report_path,
        run_id=run_id,
        run_root=run_root,
        submission_folder=submission_folder,
        submission_manifest=submission_manifest,
        data_gate_policy=data_gate_policy,
        fixture=args.fixture,
        interval_seconds=interval,
        dry_run=args.dry_run or bootstrap.dry_run,
        triage_decision_manifest=triage_paths["--triage-decision-manifest"],
        triage_clusters=triage_paths["--triage-clusters"],
        triage_producer_recipe=triage_paths["--triage-producer-recipe"],
        corpus_register=triage_paths["--corpus-register"],
    )


def require_approved_submission_folder(plan: RunPlan) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Ask the data gate what the Door will ask, before the bootstrap spends anything.

    Returns the approved roots *and* the listed roots that did not resolve on
    this machine, both for the run report.  A pod has no local ``private/`` and
    a laptop has no mounted volume, so this gate almost always enforces a
    shorter list than the policy names; the run report says which one it was
    (principle 2), rather than leaving the narrowing to be inferred from a
    refusal that did not happen.  A refusal names the policy file and says
    whose decision the missing root is.
    """

    resolved: gate.ResolvedStorageRoots | None = None
    try:
        policy = gate.load_policy(plan.data_gate_policy)
        resolved = gate.resolve_storage_roots(policy)
        gate.require_approved_storage_location(
            plan.submission_folder, resolved.roots, "submission folder on the volume"
        )
    except gate.GateRefusal as error:
        # The skipped roots belong in this refusal, not only in the report a
        # refusal never writes. This check runs before the bootstrap's own
        # mount diagnostic, so it is often the only thing an operator sees --
        # and "the policy does not admit it" reads as a policy that never
        # listed the folder, when what happened is that the root listing it
        # was not mounted here. Skipped roots are still not admitted; they are
        # named.
        narrowing = ""
        if resolved is not None and resolved.skipped:
            narrowing = (
                f" The policy also lists {list(resolved.skipped)}, which did not resolve on "
                "this machine and was therefore not enforced as an approved root."
            )
        raise RunRefusal(
            f"the data-handling policy {plan.data_gate_policy} does not admit the submission "
            f"folder: {error}.{narrowing} Listing the volume root as an approved storage root "
            "is a disclosure decision reserved to the project lead; nothing was fetched and "
            "no run was started",
            report_path=plan.report_path,
        ) from error
    return tuple(str(root) for root in resolved.roots), resolved.skipped


def _placement_tier(report: BootstrapReport) -> tuple[str, dict[str, object]]:
    """The measured tier, and the exact serving-recipe/placement digests it was measured against.

    An absent, malformed or stray non-dict ``serving_config_inputs`` must not
    read as "no digests": the run report would then record "complete" with no
    proof the serving recipe and pod-placement bytes the orchestrator is
    about to use are the ones ``PREFLIGHT`` actually measured.
    ``ServingConfigInputs.from_record`` is the same validation
    ``bootstrap_main`` applies when it seals this value onto the receipt
    (``ServingConfigInputs.to_record``); reapplying it here closes the gap
    between "the receipt carries something under this key" and "the receipt
    carries a run-sealed configuration projection this run can trust".
    """

    receipt = report.receipts.get("preflight")
    tier = receipt.get("placement_tier") if isinstance(receipt, dict) else None
    if not isinstance(tier, str) or not tier:
        raise RunRefusal(
            "the green bootstrap's PREFLIGHT receipt carries no placement_tier; the run cannot "
            "record which measured tier its chairs were preflighted for"
        )
    inputs = receipt.get("serving_config_inputs") if isinstance(receipt, dict) else None
    if not isinstance(inputs, Mapping):
        raise RunRefusal(
            "the green bootstrap's PREFLIGHT receipt carries no serving_config_inputs; the run "
            "cannot record which measured serving recipe and pod-placement digests its chairs "
            "were preflighted against"
        )
    try:
        validated = ServingConfigInputs.from_record(inputs)
    except ServingConfigurationError as error:
        raise RunRefusal(
            "the green bootstrap's PREFLIGHT receipt carries a malformed serving_config_inputs: "
            f"{error}"
        ) from error
    return tier, validated.to_record()


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _write_run_report(plan: RunPlan, record: Mapping[str, object]) -> None:
    atomic_write(plan.report_path, canonical_json({"schema": RUN_REPORT_SCHEMA, **record}))


def _write_refusal(
    report_path: Path | None, reason: str, *, now: Callable[[], datetime]
) -> str | None:
    """Best-effort durable reason, with ``bootstrap_main``'s own rule about parents.

    Returns ``None`` when the reason was written, and also when there was
    legitimately nowhere yet to write it (``report_path`` is ``None`` for a
    refusal raised before a plan exists) -- neither is a failure worth a
    caller's attention. Any other case returns a description of why the
    durable record could not be written, so the caller can say so: a refusal
    that never reaches the volume is silent (principle 2) unless something
    names that it happened.
    """

    if report_path is None:
        return None
    if not report_path.parent.is_dir():
        return f"{report_path.parent} does not exist"
    try:
        atomic_write(
            report_path,
            canonical_json({"schema": RUN_REFUSAL_SCHEMA, "reason": reason, "at": _stamp(now())}),
        )
    except OSError as error:
        return str(error)
    return None


def _liveness_journal(
    plan: RunPlan, base: Mapping[str, object], *, now: Callable[[], datetime]
) -> Callable[[int, bool], None]:
    """A callback that re-journals one liveness line per tick beside the run report.

    Carries the child's pid and the moment it was last seen, so a record found
    on a fetched volume reads as *stale* rather than as current: a tick stamped
    hours before the hard deadline, with `alive: true`, says the supervisor
    stopped ticking while its child was still running -- which is what a
    SIGKILLed pod_run looks like, and is a different fact from a run that
    finished. The last write of a normal run is `alive: false`, so the absence
    of that line is itself the signal.

    Best effort on the write: the volume that would hold this may be the thing
    that failed, and a liveness tick must never be the reason a running
    orchestrator is abandoned. A failed tick says so on stderr (and therefore
    in the transcript) instead of raising.
    """

    counter = {"tick": 0}

    def journal(pid: int, alive: bool) -> None:
        record = {
            "schema": RUN_LIVENESS_SCHEMA,
            "run_id": plan.run_id,
            "state": "orchestrator-running" if alive else "orchestrator-exited",
            "alive": alive,
            "pid": pid,
            "tick": counter["tick"],
            "last_seen": _stamp(now()),
            "started_at": base.get("started_at"),
            "hard_deadline": base.get("hard_deadline"),
            "report_path": str(plan.report_path),
            "transcript_path": str(plan.transcript_path),
        }
        counter["tick"] += 1
        try:
            atomic_write(plan.liveness_path, canonical_json(record))
        except OSError as error:
            print(f"pod_run liveness tick could not be written: {error}", file=sys.stderr)

    return journal


def _records_at_close(
    plan: RunPlan, *, transcript_failure: str | None = None
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """What each record the report names actually left on the volume at close.

    The orchestrator's stage-timing journal, the liveness record and the
    transcript are all written best-effort by design: a stopwatch or a tick
    must never be the reason a running orchestrator is abandoned, so each
    writer says so on stderr and carries on. That stderr line lives in the
    transcript, which is bounded, and nothing else said whether the file the
    report *names* was actually there -- a fetched report could read
    ``complete`` over an absent journal. This audits the three at close and names each missing, unreadable
    or foreign one in the report itself, and a run whose named records did
    not all come home is held rather than complete, so the absence is a
    durable fact in the state rather than a line a reader has to grep for.

    ``transcript_failure`` is what the runner reports about its own tee: a
    transcript whose pump failed part-way, or whose reader was still attached
    when the wait for it ran out, is a file that exists and is incomplete,
    which ``is_file`` alone would call present.
    """

    audit: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for name, path in (
        ("transcript", plan.transcript_path),
        ("liveness", plan.liveness_path),
        ("timing_journal", plan.timing_journal_path),
    ):
        entry: dict[str, object] = {"path": str(path), "present": path.is_file()}
        if not entry["present"]:
            missing.append(name)
        elif name == "transcript" and transcript_failure is not None:
            entry["failure"] = transcript_failure
            missing.append(name)
        elif name == "timing_journal":
            try:
                with path.open("rb") as handle:
                    data = handle.read(TIMING_JOURNAL_READ_BYTES + 1)
                if len(data) > TIMING_JOURNAL_READ_BYTES:
                    raise ValueError(
                        f"larger than {TIMING_JOURNAL_READ_BYTES} bytes, which no stage-timing "
                        "journal the orchestrator writes is"
                    )
                journal = json.loads(data.decode("utf-8"))
                entries = journal.get("entries") if isinstance(journal, dict) else None
                owner = journal.get("run_id") if isinstance(journal, dict) else None
                entry["entries"] = len(entries) if isinstance(entries, list) else None
                entry["run_id_matches"] = owner == plan.run_id
                if not entry["entries"] or not entry["run_id_matches"]:
                    # No entries is as missing as no file: at least one stage
                    # ran, so an empty journal is a journal every write failed.
                    entry["failure"] = (
                        f"the journal at {path} belongs to run {owner!r} or has no entries; "
                        "the orchestrator refuses to merge into a foreign journal and says "
                        "so in the transcript"
                    )
                    missing.append(name)
            except (OSError, ValueError, RecursionError, MemoryError) as error:
                # `ValueError` covers the decode and the JSON errors; the last
                # two are what a hostile or corrupt journal can raise from the
                # decoder, and an audit that escapes here would leave the run
                # report in its `running` state with no final outcome at all.
                entry["failure"] = f"unreadable: {type(error).__name__}: {error}"
                missing.append(name)
        audit[name] = entry
    return audit, missing


def _refuse(refusal: PlanRefusal, *, now: Callable[[], datetime]) -> int:
    print(f"pod_run refused: {refusal}", file=sys.stderr)
    failure = _write_refusal(refusal.report_path, str(refusal), now=now)
    if failure is not None:
        print(f"pod_run refusal report could not be written: {failure}", file=sys.stderr)
    return EXIT_REFUSED


class BoundedTranscript:
    """The child's merged output, head written live and tail kept for the close.

    Two bounds rather than one file that grows forever (the objection the
    inherited-streams comment this replaces actually had) and rather than one
    ring buffer (which would leave nothing durable until the process ended).
    The head reaches the volume as it arrives, so a pod_run that is SIGKILLed
    still leaves the start of the run readable; the tail is the last
    ``tail_bytes`` seen, appended after a truncation marker when the file is
    closed, because the traceback or refusal that says why a run stopped is at
    the end of the stream.

    What is *not* durable is the tail of a transcript that has already passed
    the head bound, in the window before ``close``. That is stated here and in
    the run report rather than hidden: it costs a rewrite of the whole file on
    every tick to fix, and the case it would cover -- a multi-megabyte
    transcript *and* a killed supervisor -- still leaves eight megabytes of
    durable head to read.
    """

    __slots__ = ("_handle", "_head_room", "_tail", "_tail_bytes", "_overflow")

    def __init__(self, path: Path, *, head_bytes: int, tail_bytes: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("wb")
        self._head_room = head_bytes
        self._tail_bytes = tail_bytes
        self._tail = bytearray()
        self._overflow = 0

    def write(self, chunk: bytes) -> None:
        if self._head_room > 0:
            head, chunk = chunk[: self._head_room], chunk[self._head_room :]
            self._handle.write(head)
            # Flushed per chunk, not per close: an unflushed transcript is
            # exactly the record that is missing when the process is killed.
            self._handle.flush()
            self._head_room -= len(head)
        if not chunk:
            return
        self._overflow += len(chunk)
        self._tail.extend(chunk)
        if len(self._tail) > self._tail_bytes:
            del self._tail[: len(self._tail) - self._tail_bytes]

    def close(self) -> None:
        try:
            if self._overflow:
                dropped = self._overflow - len(self._tail)
                self._handle.write(
                    f"\n[pod_run: {dropped} byte(s) of this transcript were dropped between "
                    f"the head above and the final {len(self._tail)} byte(s) below]\n".encode()
                )
                self._handle.write(bytes(self._tail))
            self._handle.flush()
            os.fsync(self._handle.fileno())
        finally:
            self._handle.close()


def _pump(stream, transcript: BoundedTranscript, mirror, failure: list[str]) -> None:
    """Copy the child's merged output to the transcript and to our own stderr.

    Mirrored, not diverted: the container log an operator watches live while a
    pod runs is the same text as before this teeing existed. The transcript is
    the copy that survives the pod.

    A transcript write that fails (the volume is the usual suspect) is
    recorded in ``failure`` and the pipe is still drained to the mirror: a
    reader that stopped would block the child on a full pipe, and a failure
    that stayed in this thread would leave a truncated file the close-time
    audit could only call present.
    """

    try:
        while True:
            # `read1`, not `read`: `read` on a pipe blocks until it has the
            # whole 64 KiB or the child exits, which would hold every short
            # run's output in memory until the end and defeat the live head
            # this transcript exists to leave behind.
            chunk = stream.read1(65536)
            if not chunk:
                return
            if not failure:
                try:
                    transcript.write(chunk)
                except Exception as error:  # noqa: BLE001 -- recorded, not raised, in a thread
                    failure.append(
                        f"the transcript write failed part-way ({type(error).__name__}: "
                        f"{error}); the file is incomplete from that point"
                    )
            if mirror is None:
                continue
            try:
                mirror.write(chunk)
                mirror.flush()
            except (OSError, ValueError):
                # A closed or broken mirror must not cost the durable copy.
                pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    transcript: Path,
    liveness: Callable[[int, bool], None],
    interval_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run the orchestrator with its output teed to the volume, ticking while it lives.

    Without this, an inherited stream leaves a stage's refusal text reachable
    only from the container's log, which dies with the pod, and a killed
    supervisor leaves the run report claiming a run still in progress. Both
    are fixed by the same call: the child's stdout and stderr are merged into
    one pipe that a reader thread tees into ``transcript``, and the poll loop
    that waits for the child re-journals a liveness tick through ``liveness``
    every ``interval_seconds`` while it is alive.

    ``stderr=STDOUT`` deliberately: two separately bounded files would let a
    reader interleave them wrongly, and the one question the transcript exists
    to answer -- what was printed just before this stopped -- needs the two
    streams in the order they were actually written.
    """

    writer = BoundedTranscript(
        transcript, head_bytes=TRANSCRIPT_HEAD_BYTES, tail_bytes=TRANSCRIPT_TAIL_BYTES
    )
    try:
        child = subprocess.Popen(
            argv, cwd=cwd, env=dict(env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    except BaseException:
        # An orchestrator that could not start still opened this file; leaving
        # the handle behind would leak it and leave a zero-length transcript
        # with no explanation. `main` turns the OSError into a `failed` run
        # report whose detail names the start failure.
        writer.close()
        raise
    # `getattr`: a captured or replaced stderr need not expose a binary buffer,
    # and the mirror is the disposable half of this tee -- the transcript is not.
    mirror = getattr(sys.stderr, "buffer", None)
    failure: list[str] = []
    reader = threading.Thread(
        target=_pump, args=(child.stdout, writer, mirror, failure), daemon=True
    )
    reader.start()
    try:
        while True:
            liveness(child.pid, True)
            try:
                # Waited on with a timeout rather than polled and slept: a child
                # that exits a moment after a tick must not cost the run a whole
                # idle interval on a card that is billing.
                child.wait(timeout=max(0.01, interval_seconds))
                break
            except subprocess.TimeoutExpired:
                continue
        liveness(child.pid, False)
    finally:
        # Joined before the transcript is closed: the pump owns the writer
        # until the pipe is at end of file, and closing under it would lose
        # the tail this whole mechanism exists to keep. Bounded, because the
        # pipe reaches end of file only when every holder closes it and a
        # descendant the orchestrator left behind can hold it indefinitely;
        # the child itself is already gone, so what is waited for past this
        # point is not the run's output, and the final report must be written.
        reader.join(timeout=TRANSCRIPT_READER_JOIN_SECONDS)
        if reader.is_alive():
            failure.append(
                f"the transcript reader was still attached {TRANSCRIPT_READER_JOIN_SECONDS:g}s "
                "after the orchestrator exited: a descendant it left behind still holds its "
                "output pipe, and the transcript was closed without that text"
            )
        writer.close()
    # The runner's own report about its tee rides on `stderr`, which the tee
    # leaves unused (the child's streams are merged into the transcript): a
    # string names the failure, `None` says the transcript is whole.
    return subprocess.CompletedProcess(
        argv, child.returncode, stderr=failure[0] if failure else None
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    now: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    actions_factory: Callable[[Plan], BootstrapActions] = build_actions,
    runner: Runner = _run,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    try:
        refuse_credential_looking_argv(raw_argv)
        run_argv, bootstrap_argv = split_argv(raw_argv)
    except PlanRefusal as refusal:
        return _refuse(refusal, now=now)
    # The token is read before `prepare` scrubs the environment: its own name
    # is credential-shaped and would be gone afterwards.
    launch_token = environment.get("VERBATUS_LAUNCH_TOKEN") or None
    try:
        bootstrap_plan, hard_deadline = bootstrap_main.prepare(bootstrap_argv, environment, now=now)
    except PlanRefusal as refusal:
        return bootstrap_main.refuse(refusal, plan=None, now=now, label="pod_run (bootstrap argv)")
    try:
        args = build_parser().parse_args(run_argv)
        plan = resolve_run_plan(args, bootstrap_plan, launch_token)
        approved_roots, skipped_roots = require_approved_submission_folder(plan)
    except PlanRefusal as refusal:
        return _refuse(refusal, now=now)

    if plan.dry_run:
        print(json.dumps(plan.to_record(), sort_keys=True, indent=2))
        return EXIT_DRY_RUN

    started_at = _stamp(now())
    base: dict[str, object] = {
        "run_id": plan.run_id,
        "run_root": str(plan.run_root),
        "plan": plan.to_record(),
        "approved_storage_roots": list(approved_roots),
        # Named on every run, not only when every root is missing: the roots
        # this machine did not have are what makes the enforced list shorter
        # than the approved policy, and a reader of this report should not have
        # to guess which (principle 2).
        "skipped_storage_roots": list(skipped_roots),
        "hard_deadline": _stamp(hard_deadline),
        "started_at": started_at,
    }
    _write_run_report(plan, {**base, "state": "bootstrapping", "exit_code": None})

    report = bootstrap_main.run_bootstrap(bootstrap_plan, now=now, actions_factory=actions_factory)
    if isinstance(report, int):
        _write_run_report(
            plan,
            {
                **base,
                "state": "refused",
                "exit_code": EXIT_REFUSED,
                "reason": "bootstrap actions could not be built; see the bootstrap report",
                "finished_at": _stamp(now()),
            },
        )
        return EXIT_REFUSED
    if not report.green:
        _write_run_report(
            plan,
            {
                **base,
                "state": "bootstrap-red",
                "exit_code": EXIT_BOOTSTRAP_RED,
                "bootstrap": report.to_record(),
                "finished_at": _stamp(now()),
            },
        )
        return EXIT_BOOTSTRAP_RED
    try:
        placement_tier, serving_config_inputs = _placement_tier(report)
    except RunRefusal as refusal:
        refusal.report_path = plan.report_path
        _write_run_report(
            plan,
            {
                **base,
                "state": "refused",
                "exit_code": EXIT_REFUSED,
                "reason": str(refusal),
                "bootstrap": report.to_record(),
                "finished_at": _stamp(now()),
            },
        )
        print(f"pod_run refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED

    command = plan.orchestrator_argv()
    command += ["--placement-tier", placement_tier]
    running: dict[str, object] = {
        **base,
        "bootstrap": report.to_record(),
        "placement_tier": placement_tier,
        "serving_config_inputs": serving_config_inputs,
        "orchestrator_argv": command,
        # Named in the report, not only written beside it: a fetched report is
        # what a later session reads first, and a record it cannot name is a
        # record nobody asks the volume for (principle 2).
        "transcript_path": str(plan.transcript_path),
        "liveness_path": str(plan.liveness_path),
        "hold_path": str(plan.hold_path),
        "timing_journal_path": str(plan.timing_journal_path),
    }
    _write_run_report(plan, {**running, "state": "running", "exit_code": None})
    liveness = _liveness_journal(plan, base, now=now)
    try:
        completed = runner(
            command,
            cwd=plan.repository,
            env=dict(environment),
            transcript=plan.transcript_path,
            liveness=liveness,
            interval_seconds=plan.interval_seconds,
        )
        orchestrator_exit: int | None = completed.returncode
        failure_detail: str | None = None
        transcript_failure = completed.stderr if isinstance(completed.stderr, str) else None
    except OSError as error:
        orchestrator_exit = None
        failure_detail = f"the orchestrator could not start: {error}"
        transcript_failure = None
    exit_code = _ORCHESTRATOR_EXITS.get(orchestrator_exit, EXIT_FAILED)
    if exit_code == EXIT_FAILED and failure_detail is None:
        # `EXIT_FATAL` is a *named* orchestrator exit (`common/stage.py`:
        # structural or fatal), it simply has no run state of its own here. It
        # is reported as the refusal it is: telling a reader the code was
        # unrecognised would send them hunting a transcript for a problem the
        # exit already named.
        failure_detail = (
            "the orchestrator exited EXIT_FATAL (2): it refused structurally rather than "
            f"completing, holding, or halting. Read {plan.transcript_path} and the run tree "
            "before calling this run anything"
            if orchestrator_exit == ORCHESTRATOR_FATAL
            else f"the orchestrator exited {orchestrator_exit}, outside its own "
            f"complete/held/halted/fatal vocabulary; read {plan.transcript_path} and the "
            "run tree before calling this run anything"
        )
    if failure_detail is None and exit_code in (EXIT_HELD, EXIT_HALTED):
        # `detail: null` here would read as "nothing further to say" about
        # the two outcomes that most need a reason. The stage's own stderr is
        # the reason, and it has a durable home in the transcript; name it
        # rather than restating it badly.
        failure_detail = (
            f"the orchestrator exited {orchestrator_exit} ({_STATE_FOR_EXIT[exit_code]}); the "
            f"stage's own reason is the last text in {plan.transcript_path}, and the run tree "
            "holds the evidence it was decided on"
        )
    state = _STATE_FOR_EXIT[exit_code]
    holding = exit_code in _HOLD_AFTER_EXITS
    records_at_close, records_missing = _records_at_close(
        plan, transcript_failure=transcript_failure
    )
    if records_missing:
        absence = (
            "records this report names were not on the volume at close, or were not this "
            f"run's: {', '.join(records_missing)}; the writer's own reason is in "
            f"{plan.transcript_path} if that survived"
        )
        if exit_code == EXIT_COMPLETE:
            # A run is not complete while a record its own report names is
            # missing: the timings are what the first live run exists to
            # measure, and a transcript or liveness record that never landed
            # is the diagnosis a later session would go looking for. It is
            # held for review rather than failed -- the run tree is intact and
            # the orchestrator finished -- and `held` holds to the hard
            # deadline exactly as `complete` does, so the meter is unchanged.
            exit_code = EXIT_HELD
            state = _STATE_FOR_EXIT[exit_code]
            holding = True
            failure_detail = f"the orchestrator completed, but {absence}"
        else:
            failure_detail = absence if failure_detail is None else f"{failure_detail}. {absence}"
    final: dict[str, object] = {
        **running,
        "state": state,
        "exit_code": exit_code,
        "orchestrator_exit": orchestrator_exit,
        "detail": failure_detail,
        "records_at_close": records_at_close,
        "records_missing": records_missing,
        "held_to_hard_deadline": holding,
        "hold_detail": (
            "the run finished; holding to the hard deadline so the pod timer does not read "
            "this as completed-early"
            if holding
            else "the run did not finish; returning at once so the pod timer closes the pod "
            "rather than billing the rest of the lease for nothing. Every record is on the "
            "volume, which outlives the pod"
        ),
        "finished_at": _stamp(now()),
    }
    _write_run_report(plan, final)
    if not holding:
        print(
            f"pod_run {plan.run_id}: {state} (exit {exit_code}); returning now so the pod "
            "timer closes the pod"
        )
        return exit_code
    print(f"pod_run {plan.run_id}: {state} (exit {exit_code}); holding to the hard deadline")
    hold(
        report_path=plan.hold_path,
        hard_deadline=hard_deadline,
        state=f"holding-after-{state}",
        bootstrap=report.to_record(),
        now=now,
        sleeper=sleeper,
        interval_seconds=plan.interval_seconds,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
