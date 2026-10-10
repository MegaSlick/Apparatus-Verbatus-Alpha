"""``python -m operations.pod.pod_run`` -- bootstrap, run the orchestrator, hold.

The one tracked entrypoint that runs the pipeline on a pod.  It is composed
from ``bootstrap_main`` rather than beside it: the argv after ``--`` is a
complete ``bootstrap_main`` argv, prepared and run through that module's own
``prepare``/``run_bootstrap``, so every refusal, the write probe, the
credential scrub and the hard deadline are the same ones a plain bootstrap
gets.  Only after that bootstrap journal is green does this process start the
orchestrator (``pipeline/orchestrator/run.py``) as a subprocess of the pod's
own interpreter::

    hand-route run root /var/tmp/verbatus-runs  (synced to <volume>/runs)
    timer-route root    <volume>/runs
    submission          --submission-folder / --submission-manifest, inside the volume
    roster              the bootstrap plan's --models-config
    serving catalogue   the bootstrap plan's --serving-recipes-config
    data gate           --data-gate-policy, inside the repository

The roster and the serving catalogue are deliberately taken from the bootstrap
plan and not accepted again here: ``PREFLIGHT`` measured that roster against
that catalogue, and a run that named different files would serve chairs no
preflight had looked at.

**Exit codes never read "complete" for a partial run.**
``EXIT_COMPLETE`` (0) is returned only when the orchestrator itself returned
``EXIT_COMPLETE``; ``EXIT_HELD`` (3) and ``EXIT_HALTED`` (4) mirror the
orchestrator's own held and halted exits; ``EXIT_REFUSED`` (2) is a named
refusal before the orchestrator ran; ``EXIT_BOOTSTRAP_RED`` (5) is a red
bootstrap step, the orchestrator never started; ``EXIT_FAILED`` (6) is an
orchestrator that could not start, that refused structurally (its own
``EXIT_FATAL``), or that exited outside its own vocabulary; ``EXIT_DRY_RUN``
(7) is a drill -- both plans printed, nothing run, no report written -- and is
never 0, so a dry run launched as ``pod_timer``'s bootstrap child by mistake
cannot be mistaken for a completed run; ``EXIT_SELECTION_COMPLETE`` (8) is a
selected range ending before Armarium whose orchestrator completed and whose
transcript and liveness records came home.  Whatever the outcome, the report at
``--report-path`` says the same thing durably, under the launch-bound name,
before the exit code says it.  The exceptions: the dry run writes no report at
all; a credential-looking argv, a missing ``--`` and a refused
``--report-path`` itself (missing, outside the volume, without the launch
token, or colliding with bootstrap evidence) are refused on stderr only, so no other
record is overwritten; and a refused bootstrap argv is recorded in the
bootstrap report, not the run report.

**A full terminal run holds to the deadline.**
``pod_timer.run_with_bootstrap`` treats any child exit before the hard deadline
-- exit 0 included -- as ``completed-early`` and closes the pod with a non-green
timer report, so after a full ``complete`` run, or a ``held`` one whose export is
sealed, this process holds to the
shared hard deadline exactly as ``bootstrap_main`` does, re-journaling a
liveness line beside the run report.  That hold is paid idle time between a
finished run and the deadline, because ``pod_timer`` reads any earlier exit as
``completed-early``.  Nothing here touches the pod guard's keep-alive, so its
idle ladder, when switched to delete, ends the hold early:
``held_to_hard_deadline`` records the choice to hold, and the hold journal's
last tick records when the hold actually ended.

A selected range ending before Armarium records ``selection-complete`` when it
completes, and returns at once when it holds. ``--stop-after-coniector`` (off by
default) cuts any selection to end at the Coniector, the last stage that needs the
card, so the pod closes and Recensor onward runs from the fetched tree off the card. The pod timer closes the card;
the run tree remains on the volume for the next selection.

**A run that holds before its export returns at once too.**  A held
Attestatores, or a Recensor that holds anything, stops a full run before the
Armarium; the next step is a person's review, not more GPU work, so the pod is
not kept waiting for it. Only a held run whose orchestrator says, in the stop
record this invocation alone gave it (``--stop-record``), that it reached a
sealed Armarium export (``read_stop_record``) holds to the deadline; an
export an earlier pass left in the tree never counts.

**Nothing the run printed dies with the pod.**  The orchestrator's stdout and
stderr -- and, through inheritance, every stage's -- are teed into a bounded,
launch-token-named transcript beside the run report, and the report names it by
path.  A liveness line beside them carries the child's pid and the moment it
was last seen, re-journaled on the same interval the hold loop uses, so a
supervisor killed mid-run leaves a stale tick rather than a record that still
says ``running``.

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
only here.

**The measured placement tier is forwarded.**  A receipt without one is refused.

``--mechanics-qualification`` permits unproven rows for that run.

**``--no-hold`` is for a run started by hand, outside the pod timer.**  After
the final report of any run past a green bootstrap, it returns instead of
holding and moves this pod's guard deadline to now, so the guard deletes the
pod within about a minute rather than leaving it to the idle ladder.  It first leaves
the run id and outcome in ``released-<pod id>`` beside the deadline, which the
guard's delete notice carries.  The pod id is the container's own, read from
its first process when that is readable, and a shell exporting a different
one is refused before the bootstrap.  It is refused under a launch token: the
pod timer reads an early exit as ``completed-early``.

**A resume is checked against its seal before the bootstrap.**  When the run
tree already has its ``run.json``, the Perlector protocol this launch names
(or the default) and, on a real run, the run policy
(``--mechanics-qualification``) are compared with the digests the run sealed,
so a resume the stages would refuse is refused before a card is paid for.

**The data gate is checked before the bootstrap spends anything.**  The
orchestrator's Door refuses a submission folder outside the policy's approved
storage roots (``config/data_handling_policy.json``; README.md says which
roots it lists and why).  This process asks the gate the same question first,
so a launch whose submission folder is outside every listed root is refused
here, by name, before a model is fetched on a billing card rather than after.

**A selection's predecessor is checked before the bootstrap spends anything.**
A range that starts after the Door needs the run tree's sealed predecessor
stage; a missing or changed seal is refused here, as the orchestrator would
refuse it after a paid bootstrap.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Final, Mapping, MutableMapping, Sequence, TypeGuard

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity, is_witness_role
from common.contracts.errors import ContractError
from common.contracts.identities import validate_run_id
from common.contracts.stages import SEAL_PREDECESSORS
from common.reconstruction import DEFAULT_RECONSTRUCTION_CONFIG_PATH, load_reconstruction_policy
from common.runtree.store import SERVING_LOGS_DIR, RunTree
from common.runtree.sync import SYNC_PREFIX, RunTreeSync, RunTreeSyncError
from common.sealed_config import read_sealed_toml
from common.stage import (
    DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    real_run_policy_digest,
    run_sealed_config_digests,
    verify_predecessor_seal,
)
from common.stage import EXIT_COMPLETE as ORCHESTRATOR_COMPLETE
from common.stage import EXIT_FATAL as ORCHESTRATOR_FATAL
from common.stage import EXIT_HELD as ORCHESTRATOR_HELD
from common.stage import EXIT_RUN_HALTED as ORCHESTRATOR_HALTED
from operations.notify.client import NotifyOutcome
from operations.pod.notify_hooks import (
    RunnerFactory,
    environment_runner,
    notify_deadline_at_risk_from_guard,
    notify_systemic_from_guard,
)
from operations.serving.capacity import CapacityPlan
from operations.serving.config import ServingConfigInputs
from operations.serving.errors import ServingConfigurationError
from operations.submit import gate
from pipeline.orchestrator.run import (
    SEQUENCE_NAMES,
    STAGE_TIMING_JOURNAL_SCHEMA,
    STOP_RECORD_SCHEMA,
)

from . import bootstrap_main, finish_estimate, progress_watch
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
from .models import POD_GUARD_DIRECTORY, POD_ID_ENVIRONMENT, run_report_paths, utc_now
from .run_exits import (
    EXIT_BOOTSTRAP_RED,
    EXIT_COMPLETE,
    EXIT_DRY_RUN,
    EXIT_FAILED,
    EXIT_HALTED,
    EXIT_HELD,
    EXIT_REFUSED,
    EXIT_SELECTION_COMPLETE,
)
from .spend import POD_BUDGET_ENVIRONMENT, POD_BUDGET_SWITCH_ENVIRONMENT

RUN_REPORT_SCHEMA = "pod-run-report.v1"
RUN_REFUSAL_SCHEMA = "pod-run-refusal.v1"
RUN_LIVENESS_SCHEMA = "pod-run-liveness.v1"
DEFAULT_RUNS_DIRECTORY = "runs"
DEFAULT_LOCAL_RUNS_DIRECTORY = Path("/var/tmp/verbatus-runs")
# The container's first process, where the provider sets the pod id. A login
# shell need not inherit it, and a value exported there by hand can be another
# pod's: every pod's guard keeps its deadline on the same shared volume.
PID1_ENVIRON = Path("/proc/1/environ")
# The guard touches heartbeat-<pod id> once a tick (`pod_guard.sh`, one minute
# by default). Older than this, nothing is known to be watching the deadline.
GUARD_HEARTBEAT_STALE_SECONDS = 300
# The run-policy knobs pod_run never forwards, so the orchestrator's own argv
# defaults govern them; a resume's run-policy digest is recomputed under them.
ORCHESTRATOR_RUN_POLICY_DEFAULTS: Mapping[str, object] = {"witness_context": "named"}
PERLECTOR_PROTOCOL_WHAT = "Perlector protocol configuration"
PERLECTOR_PROTOCOL_MODULE = Path(__file__).resolve().parents[2] / "pipeline/4_perlector/protocol.py"

# The transcript's two bounds. The head is written to the volume as it arrives,
# so a process killed mid-run still leaves the beginning of the run durable; the
# tail is the last window kept in memory and appended at close, because the
# traceback or refusal that explains a stopped run is at the *end* of a stream
# whose middle nobody needs. Between them a transcript cannot grow without
# limit on a volume whose space the run tree also needs.
TRANSCRIPT_HEAD_BYTES = 8 * 1024 * 1024
TRANSCRIPT_TAIL_BYTES = 1 * 1024 * 1024
# How long the reader thread is waited for after the orchestrator itself has
# exited. The pipe reaches end of file only when every holder closes it, and a
# stray descendant of the orchestrator can hold it long after the orchestrator
# is gone; an unbounded join there would keep `_run` from returning and the
# final run report from ever being written. The child
# is dead by then, so nothing this waits for is the run's own output.
TRANSCRIPT_READER_JOIN_SECONDS = 30.0
_STATE_FOR_EXIT = {
    EXIT_COMPLETE: "complete",
    EXIT_REFUSED: "refused",
    EXIT_HELD: "held",
    EXIT_HALTED: "halted",
    EXIT_BOOTSTRAP_RED: "bootstrap-red",
    EXIT_FAILED: "failed",
    EXIT_DRY_RUN: "dry-run",
    EXIT_SELECTION_COMPLETE: "selection-complete",
}

_ORCHESTRATOR_EXITS = {
    ORCHESTRATOR_COMPLETE: EXIT_COMPLETE,
    ORCHESTRATOR_HELD: EXIT_HELD,
    ORCHESTRATOR_HALTED: EXIT_HALTED,
}

# Which outcomes of a full run (one ending at Armarium) hold to the hard
# deadline. `pod_timer.run_with_bootstrap` reads any child exit before the
# deadline as `completed-early` and closes the pod with a non-green timer
# report, so a full run that ended complete or held stays until the deadline
# to keep its timer record green. A selection ending before Armarium never
# holds: it is one step of a longer run, `selection-complete` is the exit the
# timer accepts as a normal early finish, and the next selection needs the
# card closed rather than idle. `halted`, `failed`, and "the orchestrator
# could not start" never hold either: they would bill a rented card, at the
# sealed hourly rate, until the deadline for nothing, as a red bootstrap would.
# Nothing is lost by returning: the run tree, the reports and the preflight
# evidence are on the *volume*, which outlives the pod and is read by
# `verbatus fetch-run` over S3 with no pod running at all.
_HOLD_AFTER_EXITS = frozenset({EXIT_COMPLETE, EXIT_HELD})


def read_stop_record(
    stop_record: Path, run_id: str, observed_exit: int
) -> tuple[dict | None, str | None]:
    """This invocation's stop record for this run, or None and why it cannot be used.

    A usable record is this run's `STOP_RECORD_SCHEMA` record, with the integer
    `exit_code` the orchestrator was seen to exit with (`observed_exit`),
    `exported` true or false and `systemic` an alarm line or null. Anything else
    -- no record, one that cannot be read, another run's, another exit's, or one
    of another shape -- leaves both the export and the alarm unknown, and the
    reason says which.
    """
    try:
        record = json.loads(stop_record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "the orchestrator left no stop record"
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return None, f"the orchestrator's stop record could not be read ({error})"
    if not isinstance(record, dict) or record.get("schema") != STOP_RECORD_SCHEMA:
        return None, f"the orchestrator's stop record is not an {STOP_RECORD_SCHEMA} record"
    if record.get("run_id") != run_id:
        return None, f"the orchestrator's stop record is not run {run_id}'s"
    exit_code = record.get("exit_code")
    if type(exit_code) is not int:
        return None, "the orchestrator's stop record has no integer exit_code"
    if exit_code != observed_exit:
        return None, (
            f"the orchestrator's stop record says exit {exit_code}, but it exited {observed_exit}"
        )
    systemic = record.get("systemic", "")
    if not isinstance(record.get("exported"), bool) or not (
        systemic is None or (isinstance(systemic, str) and systemic.strip())
    ):
        return None, "the orchestrator's stop record has no usable exported or systemic field"
    return record, None


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """The orchestrator's exit and what the runner's tee left in the transcript.

    ``transcript_failure`` names why the transcript is incomplete (``None``
    when every byte the child printed reached it or was counted in
    ``transcript_dropped_bytes``); ``transcript_dropped_bytes`` is how much of
    the middle the bounded transcript dropped by design.
    """

    returncode: int
    transcript_failure: str | None = None
    transcript_dropped_bytes: int = 0


# `argv, *, cwd, env, transcript, liveness, interval_seconds`. The last three are
# what makes the child's output durable and its aliveness visible; a runner that
# ignored them would put both back inside the container.
Runner = Callable[..., RunnerResult]


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
    mechanics_qualification: bool = False
    perlector_protocol_config: Path | None = None
    no_hold: bool = False
    stage: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    models: str | None = None
    canary_folder: Path | None = None
    canary_manifest: Path | None = None
    triage_decision_manifest: Path | None = None
    triage_clusters: Path | None = None
    triage_producer_recipe: Path | None = None
    corpus_register: Path | None = None
    hourly_usd: Decimal | None = None
    stop_after_coniector: bool = False

    @property
    def volume_run_root(self) -> Path:
        return self.bootstrap.volume_mount_path / DEFAULT_RUNS_DIRECTORY

    @property
    def local_run(self) -> bool:
        return not self.run_root.is_relative_to(self.bootstrap.volume_mount_path)

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
    def repository(self) -> Path:
        return _named(self.bootstrap.repository, "--repository")

    @property
    def hold_path(self) -> Path:
        """The liveness line after the run, beside the run report, never over it."""

        return Path(run_report_paths(self.report_path)[1])

    @property
    def liveness_path(self) -> Path:
        """The liveness line *during* the run, beside the run report, never over it.

        `hold_path` covers the paid idle time after a finished run; this covers
        the run itself, which is the longer and more dangerous window. Without
        it the only durable statement on the volume for the whole duration of a
        run is a `running` record with no heartbeat, and a pod_run killed by the
        OOM killer or by the container teardown leaves that record as its final
        word -- a partial result that does not look partial.
        """

        return Path(run_report_paths(self.report_path)[2])

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

        return Path(run_report_paths(self.report_path)[4])

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

        return Path(run_report_paths(self.report_path)[3])

    @property
    def estimate_path(self) -> Path:
        """The current stage's finish estimate and any deadline-at-risk notice, beside the
        run report, rewritten on each liveness tick."""

        return Path(run_report_paths(self.report_path)[5])

    @property
    def progress_path(self) -> Path:
        """Whether the current stage keeps its pace, and the last moment it did, beside the
        run report, rewritten on each liveness tick (`progress_watch`)."""

        return Path(run_report_paths(self.report_path)[6])

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

    def orchestrator_argv(self, stop_record: Path) -> list[str]:
        command = [
            sys.executable,
            # Ignore PYTHON* startup controls and the user site, as the
            # orchestrator does for its own stages: nothing unsealed runs first.
            # Unbuffered, so the transcript shows each line as it is printed, not
            # in blocks when a buffer fills or the process ends.
            "-I",
            "-u",
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
            "--stage-timing-journal",
            str(self.timing_journal_path),
            "--stop-record",
            str(stop_record),
            "--repository-commit",
            self.repository_commit,
        ]
        if self.local_run:
            command += ["--stage-sync-root", str(self.volume_run_root)]
        cache_root = _named(self.bootstrap.cache_root, "--cache-root")
        command += ["--cache-root", str(cache_root)]
        store_root = _named(self.bootstrap.store_root, "--store-root")
        command += ["--store-root", str(store_root)]
        if self.mechanics_qualification:
            command.append("--mechanics-qualification")
        if self.perlector_protocol_config is not None:
            command += ["--perlector-protocol-config", str(self.perlector_protocol_config)]
        if self.stage is not None:
            command += ["--stage", self.stage]
        if self.from_stage is not None and self.to_stage is not None:
            command += ["--from", self.from_stage, "--to", self.to_stage]
        for value, flag in (
            (self.triage_decision_manifest, "--triage-decision-manifest"),
            (self.triage_clusters, "--triage-clusters"),
            (self.triage_producer_recipe, "--triage-producer-recipe"),
            (self.corpus_register, "--corpus-register"),
            (self.canary_folder, "--canary-folder"),
            (self.canary_manifest, "--canary-manifest"),
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
            **(
                {
                    "canary_folder": str(self.canary_folder),
                    "canary_manifest": str(self.canary_manifest),
                }
                if self.canary_folder and self.canary_manifest
                else {}
            ),
            "data_gate_policy": str(self.data_gate_policy),
            "models_config": str(self.models_config),
            "serving_recipes_config": str(self.serving_recipes_config),
            "fixture": self.fixture,
            "interval_seconds": self.interval_seconds,
            "dry_run": self.dry_run,
            "mechanics_qualification": self.mechanics_qualification,
            # None: the orchestrator's own default protocol.
            "perlector_protocol_config": str(self.perlector_protocol_config)
            if self.perlector_protocol_config
            else None,
            "no_hold": self.no_hold,
            "selection": self.selection_record(),
            "triage_decision_manifest": str(self.triage_decision_manifest)
            if self.triage_decision_manifest
            else None,
            "triage_clusters": str(self.triage_clusters) if self.triage_clusters else None,
            "triage_producer_recipe": str(self.triage_producer_recipe)
            if self.triage_producer_recipe
            else None,
            "corpus_register": str(self.corpus_register) if self.corpus_register else None,
            "hourly_usd": None if self.hourly_usd is None else str(self.hourly_usd),
            "stop_after_coniector": self.stop_after_coniector,
            "bootstrap": self.bootstrap.to_record(),
        }

    @property
    def perlector_protocol_path(self) -> Path:
        """The protocol the orchestrator will seal: the named one, or its default in the checkout."""

        if self.perlector_protocol_config is not None:
            return self.perlector_protocol_config
        default = DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH
        return self.repository / default.relative_to(default.parents[1])

    def selected_stages(self) -> tuple[str, ...]:
        if self.stage is not None:
            return (self.stage,)
        if self.from_stage is not None and self.to_stage is not None:
            return SEQUENCE_NAMES[
                SEQUENCE_NAMES.index(self.from_stage) : SEQUENCE_NAMES.index(self.to_stage) + 1
            ]
        return SEQUENCE_NAMES

    def selection_record(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "from": self.from_stage,
            "to": self.to_stage,
            "models": self.models,
            "stages": list(self.selected_stages()),
        }

    @property
    def ends_before_armarium(self) -> bool:
        return self.selected_stages()[-1] != "armarium"

    def required_chairs(self, *, configured_only: bool = False) -> set[str]:
        selected = set(self.selected_stages())
        roles: set[str] = set()
        if "designator" in selected:
            roles.update(("secondary_proposer", "designator_surya"))
        if "perlector" in selected:
            roles.add("perlector")
        try:
            configured = load_models_toml(self.models_config).chairs
        except ContractError as error:
            raise RunRefusal(
                f"--models-config {self.models_config} cannot name selected chairs: {error}",
                report_path=self.report_path,
            ) from error
        if "coniector" in selected:
            # The orchestrator reads the checkout's own reconstruction setting.
            reconstruction = self.repository / DEFAULT_RECONSTRUCTION_CONFIG_PATH.relative_to(
                DEFAULT_RECONSTRUCTION_CONFIG_PATH.parents[1]
            )
            try:
                mode = load_reconstruction_policy(reconstruction).mode
            except ContractError as error:
                raise RunRefusal(
                    f"{reconstruction} cannot say whether the Coniector asks its chair: {error}",
                    report_path=self.report_path,
                ) from error
            if mode == "on":
                roles.add("reconstructor")
        if "attestatores" in selected:
            roles.update(role for role in configured if is_witness_role(role))
        if configured_only:
            roles = {role for role in roles if isinstance(configured.get(role), ChairIdentity)}
        return roles


def _require_selection_predecessor(plan: RunPlan) -> None:
    """Refuse a selection whose first stage's predecessor is not sealed in this run's tree.

    The orchestrator refuses the same thing when that stage opens; asking
    first keeps the refusal ahead of a paid bootstrap.
    """

    first = plan.selected_stages()[0]
    predecessor = SEAL_PREDECESSORS.get(first)
    if predecessor is None:
        return
    try:
        verify_predecessor_seal(RunTree(plan.run_root, plan.run_id), first)
    except ContractError as error:
        raise RunRefusal(
            f"starting at {first} requires this run's sealed {predecessor} stage: {error}",
            report_path=plan.report_path,
        ) from error


def _require_sealed_run_inputs(plan: RunPlan) -> None:
    """Refuse a resume whose protocol or run policy differs from what the run sealed.

    The orchestrator's stages refuse the same thing, but only after a paid
    bootstrap. Compared here, for a run tree that already has its authority:
    the ``perlector-protocol`` digest against the file this launch would hand
    the orchestrator (read by the same seal reader the run binding uses), and,
    on a real run, the ``run-policy`` digest recomputed from
    ``--mechanics-qualification`` and the
    orchestrator defaults pod_run leaves in place. A fixture run seals those
    knobs only inside its ``config_digest``, which this cannot recompute; its
    stages still refuse a mismatch.
    """

    try:
        tree = RunTree(plan.run_root, plan.run_id)
        if not tree.resolve("run.json").exists():
            return
        sealed = run_sealed_config_digests(tree.read_run())
        protocol = plan.perlector_protocol_path
        observed = read_sealed_toml(protocol, PERLECTOR_PROTOCOL_WHAT)[1]
        mismatches: list[str] = []
        if sealed.get("perlector-protocol") != observed:
            mismatches.append(
                f"its Perlector protocol (sealed {sealed.get('perlector-protocol')}, and "
                f"{protocol} reads {observed}); name the protocol file the run started with in "
                "--perlector-protocol-config, or leave it out if the run used the default"
            )
        if "run-policy" in sealed:
            policy = _recomputed_run_policy(plan)
            if sealed["run-policy"] != policy:
                mismatches.append(
                    "its run policy (sealed "
                    f"{sealed['run-policy']}, this launch {policy}); pass the "
                    "--mechanics-qualification the run started with, or the run was sealed by "
                    "an older version of this code; start a new run"
                )
    except (ContractError, OSError) as error:
        # `read_run` already turns an unreadable or non-JSON run.json into a
        # ContractError; an OSError from the tree around it must not escape
        # either, or the refusal would be a traceback with no report.
        raise RunRefusal(
            f"run {plan.run_id!r} already exists and its sealed inputs could not be checked "
            f"against this launch: {error}",
            report_path=plan.report_path,
        ) from error
    if mismatches:
        raise RunRefusal(
            f"run {plan.run_id!r} was sealed under different inputs than this launch names: "
            + "; and ".join(mismatches)
            + ". Nothing was fetched and no run was started",
            report_path=plan.report_path,
        )


def _recomputed_run_policy(plan: RunPlan) -> str:
    defaults = ORCHESTRATOR_RUN_POLICY_DEFAULTS
    return real_run_policy_digest(
        mechanics_qualification=plan.mechanics_qualification,
        **defaults,  # type: ignore[arg-type]
    )


def _receipt_chairs(receipt: object, field: str, *, state: str | None = None) -> set[str]:
    """The chairs one PREFLIGHT receipt list names, optionally in one placement state."""
    rows = receipt.get(field) if isinstance(receipt, dict) else None
    if not isinstance(rows, list):
        return set()
    return {
        row.get("chair")
        for row in rows
        if isinstance(row, dict) and (state is None or row.get("state") == state)
    }


def _named(value: Path | None, flag: str) -> Path:
    if value is None:
        raise RunRefusal(f"the bootstrap plan names no {flag}; a run cannot proceed without it")
    return value


def _run_report_path(path: Path, bootstrap: Plan, launch_token: str | None) -> Path:
    report_path = _require_contained(path, bootstrap.volume_mount_path, "--report-path")
    _require_launch_token_named(report_path, launch_token, "--report-path", report_path=None)
    for name, bootstrap_path in (
        ("report", bootstrap.report_path),
        ("journal", bootstrap.journal),
    ):
        if bootstrap_path is not None and bootstrap_path.resolve() in {
            candidate.resolve() for candidate in run_report_paths(report_path)
        }:
            raise RunRefusal(
                f"the bootstrap {name} collides with the run report or one of its side "
                "files; name separate records"
            )
    return report_path


def _perlector_protocol():
    """The Perlector's protocol loader, by path: its stage folder is not a package."""
    name = "verbatus_perlector_protocol"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, PERLECTOR_PROTOCOL_MODULE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def build_parser() -> bootstrap_main.RefusingParser:
    parser = bootstrap_main.RefusingParser()
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--submission-folder", type=Path, required=True)
    parser.add_argument("--submission-manifest", type=Path, required=True)
    parser.add_argument("--canary-folder", type=Path)
    parser.add_argument("--canary-manifest", type=Path)
    parser.add_argument("--triage-decision-manifest", type=Path)
    parser.add_argument("--triage-clusters", type=Path)
    parser.add_argument("--triage-producer-recipe", type=Path)
    parser.add_argument("--corpus-register", type=Path)
    parser.add_argument(
        "--data-gate-policy",
        type=Path,
    )
    parser.add_argument(
        "--fixture",
        default=DEFAULT_PROOF_FIXTURE,
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mechanics-qualification",
        action="store_true",
        help="run real mechanics with unproven profiles; does not mark them proven",
    )
    parser.add_argument(
        "--perlector-protocol-config",
        type=Path,
        help="the Perlector protocol the orchestrator seals, inside the repository; "
        "omitted, the orchestrator's default",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="send the systemic alarm and the deadline-at-risk notice, when the run raises "
        "them, to the phone through operations/notify as decisions; off by default so a pod "
        "never pages a phone on its own",
    )
    parser.add_argument(
        "--hourly-usd",
        help="the pod and volume price per hour this pod was rented at, as a decimal; "
        "names the cost of running past the deadline in the deadline-at-risk notice",
    )
    parser.add_argument(
        "--no-hold",
        action="store_true",
        help="for a run started by hand: after the final report, return and move the pod "
        "guard's deadline to now instead of holding",
    )
    parser.add_argument(
        "--stop-after-coniector",
        action="store_true",
        help="end the selection at the Coniector, the last stage that needs the card, so "
        "the GPU pod is released; Recensor through Armarium then run from the fetched tree "
        "off the card (--from recensor --to armarium). Off by default",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--stage", choices=SEQUENCE_NAMES)
    selection.add_argument("--from", dest="from_stage", choices=SEQUENCE_NAMES)
    selection.add_argument("--models", choices=("small", "big"))
    parser.add_argument("--to", dest="to_stage", choices=SEQUENCE_NAMES)
    return parser


HELP_FLAGS: Final = frozenset({"-h", "--help"})


def usage() -> str:
    """Both halves' flags: this module's before the first ``--``, ``bootstrap_main``'s after."""

    run = build_parser()
    run.prog = "pod_run"
    bootstrap = bootstrap_main.build_parser()
    bootstrap.prog = "bootstrap_main"
    return (
        "usage: python -m operations.pod.pod_run <run flags> -- <bootstrap_main flags>\n\n"
        "Run flags:\n"
        + run.format_usage()
        + "\nBootstrap flags (a complete bootstrap_main argv):\n"
        + bootstrap.format_usage()
        + "\nSee the module docstring and operations/pod/README.md for what each does.\n"
    )


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
    report_path = _run_report_path(args.report_path, bootstrap, launch_token)
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
    try:
        run_id = validate_run_id(args.run_id)
    except ContractError as error:
        raise RunRefusal(f"--run-id refused: {error}", report_path=report_path) from error
    requested_root = args.run_root or (
        DEFAULT_LOCAL_RUNS_DIRECTORY if args.no_hold else volume / DEFAULT_RUNS_DIRECTORY
    )
    if args.no_hold and not requested_root.resolve().is_relative_to(volume):
        if requested_root.resolve() == DEFAULT_LOCAL_RUNS_DIRECTORY.resolve():
            if requested_root.is_symlink():
                raise RunRefusal("--run-root is a symlink", report_path=report_path)
            try:
                requested_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as error:
                raise RunRefusal(
                    f"--run-root could not be created on local disk: {error}",
                    report_path=report_path,
                ) from error
        run_root = requested_root.resolve()
    else:
        run_root = _require_contained(requested_root, volume, "--run-root", report_path=report_path)
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
    if (args.canary_folder is None) != (args.canary_manifest is None):
        raise RunRefusal(
            "--canary-folder and --canary-manifest must be supplied together",
            report_path=report_path,
        )
    canary_folder = None
    canary_manifest = None
    if args.canary_folder is not None:
        canary_folder = _require_contained(
            args.canary_folder, volume, "--canary-folder", report_path=report_path
        )
        canary_manifest = _require_contained(
            args.canary_manifest, volume, "--canary-manifest", report_path=report_path
        )
        if not canary_folder.is_dir() or not canary_manifest.is_file():
            raise RunRefusal(
                "canary folder or ledger is missing on the volume", report_path=report_path
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
    perlector_protocol_config = None
    if args.perlector_protocol_config is not None:
        perlector_protocol_config = _require_contained(
            args.perlector_protocol_config,
            repository,
            "--perlector-protocol-config",
            base_label="the checked-out repository",
            report_path=report_path,
        )
        if not perlector_protocol_config.is_file():
            raise RunRefusal(
                f"--perlector-protocol-config {perlector_protocol_config} is not a file in the "
                "checked-out repository",
                report_path=report_path,
            )
        # Read as the Perlector reads it, closed schema included, so a protocol
        # the run would refuse is refused here, before the bootstrap is paid for.
        try:
            _perlector_protocol().load(perlector_protocol_config)
        except ContractError as error:
            raise RunRefusal(
                f"--perlector-protocol-config {perlector_protocol_config} is not a protocol the "
                f"orchestrator can seal: {error}",
                report_path=report_path,
            ) from error
    if not isinstance(args.fixture, str) or not args.fixture.strip():
        raise RunRefusal("--fixture must be a non-blank fixture name", report_path=report_path)
    interval = bootstrap_main._positive_interval(args.interval_seconds, report_path=report_path)
    if args.to_stage is not None and args.from_stage is None:
        raise RunRefusal("--to requires --from", report_path=report_path)
    if args.from_stage is not None and args.to_stage is None:
        raise RunRefusal("--from requires --to", report_path=report_path)
    if args.from_stage is not None and SEQUENCE_NAMES.index(args.from_stage) > SEQUENCE_NAMES.index(
        args.to_stage
    ):
        raise RunRefusal("--from comes after --to", report_path=report_path)
    hourly_usd = None
    if args.hourly_usd is not None:
        try:
            hourly_usd = Decimal(args.hourly_usd)
        except InvalidOperation:
            hourly_usd = None
        if hourly_usd is None or not hourly_usd.is_finite() or hourly_usd <= 0:
            raise RunRefusal(
                f"--hourly-usd {args.hourly_usd!r} is not a positive decimal price",
                report_path=report_path,
            )
    stage = args.stage
    from_stage, to_stage = args.from_stage, args.to_stage
    if args.models == "small":
        from_stage, to_stage = "door", "attestatores"
    elif args.models == "big":
        from_stage, to_stage = "perlector", "armarium"
    if args.stop_after_coniector:
        stage, from_stage, to_stage = _end_at_coniector(stage, from_stage, to_stage, report_path)
    return RunPlan(
        bootstrap=bootstrap,
        report_path=report_path,
        run_id=run_id,
        run_root=run_root,
        submission_folder=submission_folder,
        submission_manifest=submission_manifest,
        canary_folder=canary_folder,
        canary_manifest=canary_manifest,
        data_gate_policy=data_gate_policy,
        fixture=args.fixture,
        interval_seconds=interval,
        dry_run=args.dry_run or bootstrap.dry_run,
        mechanics_qualification=args.mechanics_qualification,
        perlector_protocol_config=perlector_protocol_config,
        no_hold=args.no_hold,
        stage=stage,
        from_stage=from_stage,
        to_stage=to_stage,
        models=args.models,
        triage_decision_manifest=triage_paths["--triage-decision-manifest"],
        triage_clusters=triage_paths["--triage-clusters"],
        triage_producer_recipe=triage_paths["--triage-producer-recipe"],
        corpus_register=triage_paths["--corpus-register"],
        hourly_usd=hourly_usd,
        stop_after_coniector=args.stop_after_coniector,
    )


def require_approved_submission_folder(plan: RunPlan) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Ask the data gate what the Door will ask, before the bootstrap spends anything.

    Returns the approved roots *and* the listed roots that did not resolve on
    this machine, both for the run report.  A pod has no local ``private/`` and
    a laptop has no mounted volume, so this gate almost always enforces a
    shorter list than the policy names; the run report says which one it was,
    rather than leaving the narrowing to be inferred from a
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
        gate.require_approved_storage_location(plan.run_root, resolved.roots, "run root")
        if plan.local_run:
            gate.require_approved_storage_location(
                plan.volume_run_root, resolved.roots, "volume run root"
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


def _hydrate_local_run(plan: RunPlan) -> None:
    """Copy the volume's run tree to local disk before the orchestrator continues it.

    The sync's ledger stays beside the local tree, never on the volume: the volume's
    run tree holds evidence only, and `fetch-run` sets aside anything else it finds.
    """
    if not plan.local_run:
        return
    stored = plan.volume_run_root / plan.run_id
    if not stored.exists():
        return
    ledger = plan.run_root / f"{SYNC_PREFIX}hydrate-{plan.run_id}.jsonl"
    try:
        RunTreeSync(stored, plan.run_root / plan.run_id, ledger=ledger).sync()
    except (OSError, RunTreeSyncError) as error:
        raise RunRefusal(
            f"the volume's existing run could not be verified on local disk: {error}",
            report_path=plan.report_path,
        ) from error


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


def _end_at_coniector(
    stage: str | None, from_stage: str | None, to_stage: str | None, report_path: Path
) -> tuple[str | None, str | None, str | None]:
    """The selection cut to end at the Coniector: the full run becomes Door through
    Coniector, a range past it ends there, and a selection starting after it is refused,
    since it would run nothing on the card."""
    last = SEQUENCE_NAMES.index("coniector")
    first = stage or from_stage
    if first is not None and SEQUENCE_NAMES.index(first) > last:
        raise RunRefusal(
            f"--stop-after-coniector with a selection starting at {first} runs nothing; "
            "run that selection without it, off the card",
            report_path=report_path,
        )
    if stage is not None:
        return stage, None, None
    if from_stage is None:
        return None, SEQUENCE_NAMES[0], "coniector"
    assert to_stage is not None
    return None, from_stage, SEQUENCE_NAMES[min(SEQUENCE_NAMES.index(to_stage), last)]


def _capacity_plan(report: BootstrapReport, placement_tier: str) -> CapacityPlan | None:
    """PREFLIGHT's capacity plan for the measured card, or None when it published none.

    None (an unmeasured card) leaves every row at its own width. A plan that does
    not parse, was changed after PREFLIGHT, or names another tier or other serving
    digests than the receipt's is refused rather than dropped, since dropping it
    would quietly run the card at the rows' widths.
    """

    receipt = report.receipts.get("preflight")
    raw = receipt.get("capacity_plan") if isinstance(receipt, dict) else None
    if raw is None:
        return None
    try:
        plan = CapacityPlan.from_record(raw)
        plan.require_inputs(ServingConfigInputs.from_record(receipt["serving_config_inputs"]))
    except (ServingConfigurationError, KeyError) as error:
        raise RunRefusal(
            f"the green bootstrap's PREFLIGHT receipt carries a capacity plan that was refused: "
            f"{error}"
        ) from error
    if plan.tier != placement_tier:
        raise RunRefusal(
            f"the PREFLIGHT capacity plan was derived at tier {plan.tier!r}, not the measured "
            f"placement tier {placement_tier!r}"
        )
    return plan


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
    that never reaches the volume is silent unless something
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
    plan: RunPlan, *, transcript_failure: str | None = None, transcript_dropped_bytes: int = 0
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """What each record the report names actually left on the volume at close.

    Audit the three best-effort records beside the run tree. Their absences
    are named in the report; the stopwatch never changes a completed run's
    state.

    ``transcript_failure`` is what the runner reports about its own tee: a
    transcript whose pump failed part-way, or whose reader was still attached
    when the wait for it ran out, is a file that exists and is incomplete,
    which ``is_file`` alone would call present. ``transcript_dropped_bytes``
    is the middle the bounded transcript dropped by design: recorded, so a
    truncated transcript never reads as whole, but not a missing record.
    """

    audit: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for name, path in (
        ("transcript", plan.transcript_path),
        ("liveness", plan.liveness_path),
        ("timing_journal", plan.timing_journal_path),
    ):
        entry: dict[str, object] = {"path": str(path), "present": path.is_file()}
        if name == "transcript":
            entry["dropped_bytes"] = transcript_dropped_bytes
        if not entry["present"]:
            missing.append(name)
        elif name == "transcript" and transcript_failure is not None:
            entry["failure"] = transcript_failure
            missing.append(name)
        elif name == "timing_journal":
            try:
                entries = 0
                unreadable_lines = 0
                foreign_lines = 0
                with path.open("rb") as handle:
                    for line in handle:
                        if not line.endswith(b"\n"):
                            unreadable_lines += 1
                            break  # A stopped writer may leave a torn final line.
                        try:
                            record = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                            unreadable_lines += 1
                            continue  # A later append can leave a torn line in the middle.
                        if (
                            not isinstance(record, dict)
                            or record.get("schema") != STAGE_TIMING_JOURNAL_SCHEMA
                        ):
                            unreadable_lines += 1
                            continue
                        if record.get("run_id") != plan.run_id or record.get("run_root") != str(
                            plan.run_root
                        ):
                            foreign_lines += 1
                            continue
                        entries += 1
                entry["entries"] = entries
                entry["unreadable_lines"] = unreadable_lines
                entry["foreign_lines"] = foreign_lines
                if not entries:
                    entry["failure"] = (
                        f"the journal at {path} has no entries for run {plan.run_id!r}"
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


def container_pod_id() -> str | None:
    """The pod id the provider set on the container's first process, when it can be read."""

    try:
        entries = PID1_ENVIRON.read_bytes().split(b"\0")
    except OSError:
        return None
    name = POD_ID_ENVIRONMENT.encode("ascii") + b"="
    for entry in entries:
        if entry.startswith(name):
            return entry[len(name) :].decode("ascii", "replace") or None
    return None


def _is_pod_id(value: str | None) -> TypeGuard[str]:
    # ASCII only: str.isalnum accepts other scripts' letters and digits, which
    # are no provider pod id and would only name an odd file on the volume.
    return value is not None and value.isascii() and value.isalnum()


def _guard_heartbeat_age(volume: Path, pod_id: str, instant: float) -> int | None:
    """Seconds since this pod's guard last touched its heartbeat, or None when it never did."""

    try:
        beat = (volume / POD_GUARD_DIRECTORY / f"heartbeat-{pod_id}").stat().st_mtime
    except OSError:
        return None
    return max(0, int(instant - beat))


def run_tree_mark(root: Path) -> int | None:
    """The newest modification time, in nanoseconds, of any directory under `root` that a
    stage writes in; None when `root` cannot be read.

    Directories only: the run tree publishes every record by linking or renaming it into
    place, which moves its directory's time, so the walk never stats the files one by
    one. The serving-logs directories do not count: an engine that sits idle still
    writes its log, and that is not the stage advancing.
    """

    newest: int | None = None
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            newest = max(newest or 0, directory.lstat().st_mtime_ns)
            with os.scandir(directory) as entries:
                pending.extend(
                    Path(entry.path)
                    for entry in entries
                    if entry.name != SERVING_LOGS_DIR and entry.is_dir(follow_symlinks=False)
                )
        except OSError:
            if directory == root:
                return None
    return newest


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _guard_file(volume: Path, pod_id: str | None, name: str) -> Path | None:
    """`<name>-<pod id>` in this pod's guard directory; None when no guard armed one here
    or the pod id is not the first process's own."""

    guard = volume / POD_GUARD_DIRECTORY
    if not _is_pod_id(pod_id) or not guard.is_dir():
        return None
    return guard / f"{name}-{pod_id}"


class BackupList:
    """`backup-<pod id>`: the run's trees, one absolute path per line, that the guard
    copies to the volume when the pod has been idle for an hour.

    Only trees that exist are listed, since the guard reads a listed path that is
    missing as a lost run and refuses to delete the pod. Best effort: a failed write
    says so and never stops the run.
    """

    def __init__(self, path: Path | None, trees: Sequence[Path]) -> None:
        self._path = path
        self._trees = tuple(dict.fromkeys(tree.absolute() for tree in trees))
        self._listed: tuple[Path, ...] | None = None

    def refresh(self) -> None:
        if self._path is None:
            return
        present = tuple(tree for tree in self._trees if tree.is_dir())
        if present == self._listed:
            return
        try:
            atomic_write(self._path, "".join(f"{tree}\n" for tree in present).encode())
            self._listed = present
        except OSError as error:
            print(f"pod_run could not write the guard's backup list: {error}", file=sys.stderr)

    def clear(self) -> None:
        """At a clean finish every record is on the volume, so nothing is left to back up."""

        _remove(self._path)
        self._listed = None


def _remove(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        print(f"pod_run could not remove {path}: {error}", file=sys.stderr)


def _guard_keepalive(volume: Path, pod_id: str | None) -> Callable[[], None]:
    """Touch this pod's guard keep-alive file, so a running orchestrator counts as work.

    The guard reads its resource counters as a backstop; a run in progress is
    work whatever they read. Only the first process's pod id is used: a shell's
    could name another pod on the shared volume and keep it alive. Nothing is
    touched when no guard armed its directory here. Best effort: a failed touch
    says so and never stops the run, and the deadline still ends the pod.
    """

    path = _guard_file(volume, pod_id, "keepalive")
    if path is None:
        return lambda: None

    def touch() -> None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
            try:
                os.utime(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            print(f"pod_run could not touch the guard keep-alive: {error}", file=sys.stderr)

    return touch


def _require_live_guard_for_release(
    volume: Path,
    first_process_pod_id: str | None,
    shell_pod_id: str | None,
    *,
    report_path: Path,
    now: Callable[[], datetime],
) -> None:
    """Refuse a --no-hold whose release could not be shown to reach this pod's own live guard.

    Only the container's first process names this pod: a shell's id can be
    exported by hand and name another live pod on the shared volume, whose
    guard would then delete it mid-stage. Without that id the guard never
    armed here, so there is nothing to release. A fresh heartbeat under the
    id proves, before anything is paid for, that a guard is watching it.
    """

    if first_process_pod_id is None:
        raise RunRefusal(
            f"--no-hold needs this pod's id from its first process ({PID1_ENVIRON} names no "
            f"{POD_ID_ENVIRONMENT}), and a shell's own value is never trusted for it: without "
            "that id no guard armed for this pod, so there is nothing to release. Run without "
            "--no-hold; the guard's idle ladder still applies",
            report_path=report_path,
        )
    if not _is_pod_id(first_process_pod_id):
        raise RunRefusal(
            f"--no-hold: the first process's {POD_ID_ENVIRONMENT} {first_process_pod_id!r} "
            "is not a pod id",
            report_path=report_path,
        )
    if shell_pod_id not in (None, first_process_pod_id):
        raise RunRefusal(
            f"--no-hold would move the guard deadline of pod {first_process_pod_id!r}, the "
            f"container's own (its first process's {POD_ID_ENVIRONMENT}), but this shell exports "
            f"{shell_pod_id!r}; the volume holds every pod's deadline, so one of the two is "
            f"wrong. Unset or correct {POD_ID_ENVIRONMENT} in this shell",
            report_path=report_path,
        )
    age = _guard_heartbeat_age(volume, first_process_pod_id, now().timestamp())
    if age is None or age > GUARD_HEARTBEAT_STALE_SECONDS:
        seen = "never" if age is None else f"{age} s ago"
        raise RunRefusal(
            f"--no-hold needs a live guard for pod {first_process_pod_id!r}, but its heartbeat "
            f"was last touched {seen} (fresh means within {GUARD_HEARTBEAT_STALE_SECONDS} s). "
            "Run without --no-hold, or check the guard first (operations/pod/README.md)",
            report_path=report_path,
        )


def release_pod_guard(
    volume: Path, pod_id: str | None, *, run_id: str, state: str, now: Callable[[], datetime]
) -> dict[str, object]:
    """Move the pod guard's deadline to now, so the guard deletes this pod on its next tick.

    Uses the guard's own deadline file, the one it and the start command's
    backstop both read; nothing here reaches the provider. First it leaves
    ``released-<pod id>`` beside it, naming the run and how it ended, which
    the guard adds to its delete notice: without it, a phone ping after a
    finished run reads exactly like one after a window that ran out mid-run.
    ``guard_alive`` says whether this pod's guard touched its heartbeat
    recently; ``released`` alone only says the deadline was written. A guard
    started with no deadline (the budget off) writes no file but honours one
    written later, so with no file and a live guard the deadline is written. Never
    raises: a failed release leaves the pod to the guard's idle ladder and the lead.
    """

    if not _is_pod_id(pod_id):
        return {"released": False, "detail": f"{POD_ID_ENVIRONMENT} is unset or not a pod id"}
    guard = volume / POD_GUARD_DIRECTORY
    path = guard / f"deadline-{pod_id}"
    record: dict[str, object] = {"path": str(path)}
    current: int | None
    try:
        current = int(path.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        current = None
    except (OSError, ValueError) as error:
        return {
            **record,
            "released": False,
            "detail": f"no readable guard deadline for this pod ({type(error).__name__}); "
            "delete the pod by hand",
        }
    instant = now().timestamp()
    stamp = int(instant)
    try:
        atomic_write(guard / f"released-{pod_id}", f"run {run_id} ended {state}\n".encode("ascii"))
    except OSError as error:
        # The notice is the ping's wording, not the delete: the deadline still moves.
        record["notice_failure"] = str(error)
    heartbeat_age = _guard_heartbeat_age(volume, pod_id, instant)
    record["guard_heartbeat_age_seconds"] = heartbeat_age
    record["guard_alive"] = (
        heartbeat_age is not None and heartbeat_age <= GUARD_HEARTBEAT_STALE_SECONDS
    )
    if not record["guard_alive"]:
        record["detail"] = (
            "this pod's guard has not touched its heartbeat in the last "
            f"{GUARD_HEARTBEAT_STALE_SECONDS} s, so nothing is known to act on the deadline; "
            "delete the pod by hand and confirm it is gone"
        )
    if current is None and not record["guard_alive"]:
        return {
            **record,
            "released": False,
            "detail": "no guard deadline for this pod and no live guard to read a new one; "
            "delete the pod by hand",
        }
    if current is not None and current <= stamp:
        return {**record, "released": True, "deadline": current}
    try:
        atomic_write(path, f"{stamp}\n".encode("ascii"))
    except OSError as error:
        return {**record, "released": False, "detail": f"deadline write failed: {error}"}
    return {**record, "released": True, "deadline": stamp}


# What a pod-timer launch seals into the pod's environment as its quoted rates.
HOURLY_RATE_ENVIRONMENT = ("VERBATUS_POD_HOURLY_USD", "VERBATUS_VOLUME_ONGOING_HOURLY_USD")


def _hourly_price(
    plan: RunPlan, rates: Mapping[str, str | None]
) -> tuple[Decimal | None, str | None]:
    """The pod and volume price per hour, and where it came from: ``--hourly-usd``, else a
    pod-timer launch's sealed rates, else none."""

    if plan.hourly_usd is not None:
        return plan.hourly_usd, "--hourly-usd"
    values = [rates.get(name) for name in HOURLY_RATE_ENVIRONMENT]
    missing = [
        name for name, value in zip(HOURLY_RATE_ENVIRONMENT, values, strict=True) if value is None
    ]
    if len(missing) == len(HOURLY_RATE_ENVIRONMENT):
        return None, None
    if missing:
        return None, f"{' and '.join(missing)} missing"
    try:
        total = sum((Decimal(value) for value in values if value is not None), Decimal(0))
    except InvalidOperation:
        return None, f"unusable {' and '.join(HOURLY_RATE_ENVIRONMENT)}"
    if not total.is_finite() or total <= 0:
        return None, f"unusable {' and '.join(HOURLY_RATE_ENVIRONMENT)}"
    # The launch seals the price it assessed before create. The provider's price after
    # create may be higher and still within the spend policy's hourly ceiling, and the
    # pod cannot read it, so the rate is named for what it is.
    return total, (
        f"the launch-time estimate before create ({' plus '.join(HOURLY_RATE_ENVIRONMENT)})"
    )


def _pod_budget(
    plan: RunPlan, sealed: Mapping[str, str | None]
) -> tuple[finish_estimate.Budget | None, str | None, str]:
    """The pod's budget, why it is unknown, and where it came from.

    A launch seals the budget of the spend policy it armed the pod with into the
    pod's environment; that is the budget, and a part of it missing leaves it
    unknown. A budget the lead switched off is sealed as VERBATUS_POD_BUDGET=off and
    reported as off, not unknown. Only a pod with none sealed (started by hand, or adopted) falls back
    to the checkout's own spend policy, which the launching laptop may not have
    used, so it is named with its digest.
    """

    if any(value is not None for value in sealed.values()):
        budget, problem = finish_estimate.sealed_budget(sealed)
        return budget, problem, "sealed into the pod at launch"
    path = plan.repository / "config" / "spend.toml"
    budget, problem, digest = finish_estimate.load_budget(path)
    named = "unreadable" if digest is None else f"SHA-256 {digest}"
    return (
        budget,
        problem,
        f"the checked-out config/spend.toml ({named}), as no budget was sealed into the "
        "pod at launch",
    )


def _deadline_watch(
    plan: RunPlan,
    *,
    pod_id: str | None,
    hard_deadline: datetime | None,
    launch_token: str | None,
    rates: Mapping[str, str | None],
    sealed_budget: Mapping[str, str | None],
    notify: bool,
    notify_runner: RunnerFactory,
    sample: Callable[[], finish_estimate.StageProgress | None],
    now: Callable[[], datetime],
) -> finish_estimate.DeadlineWatch:
    """The finish estimate and deadline-at-risk notice for this run.

    Under the pod timer (a launch token) its hard deadline ends the pod; otherwise
    this pod's guard deadline does, when it has one (`finish_estimate.PodDeadline`). The budget is
    the one the launch sealed into the pod (`_pod_budget`), and the page witnesses
    come from the run's models configuration.
    """

    volume = plan.bootstrap.volume_mount_path
    known_pod = pod_id if _is_pod_id(pod_id) else None
    deadline = finish_estimate.PodDeadline(
        guard=None
        if known_pod is None
        else finish_estimate.GuardDeadline(volume, known_pod, now=now),
        bootstrap=hard_deadline,
        pod_timer=launch_token is not None,
    )

    def send(message: str) -> NotifyOutcome:
        return notify_deadline_at_risk_from_guard(
            message=message, volume_mount=volume, runner_factory=notify_runner
        )

    budget, budget_problem, budget_source = _pod_budget(plan, sealed_budget)
    hourly_usd, hourly_source = _hourly_price(plan, rates)
    return finish_estimate.DeadlineWatch(
        run_id=plan.run_id,
        pod_id=known_pod,
        path=plan.estimate_path,
        sample=sample,
        budget=budget,
        budget_problem=budget_problem,
        hourly_usd=hourly_usd,
        hourly_source=hourly_source,
        budget_source=budget_source,
        deadline=deadline,
        ignored=lambda: deadline.ignored,
        created_at=(lambda: None)
        if known_pod is None
        else (lambda: finish_estimate.pod_created_at(volume, known_pod)),
        send=send if notify else None,
        now=now,
    )


def _run_tree_progress(plan: RunPlan) -> finish_estimate.RunTreeProgress:
    try:
        chairs = load_models_toml(plan.models_config).chairs
    except Exception as error:  # noqa: BLE001 -- the Attestatores total is then unknown
        print(
            f"pod_run {plan.run_id}: the roster could not be read for the page counts: {error}",
            file=sys.stderr,
        )
        chairs = None
    return finish_estimate.RunTreeProgress(plan.run_root / plan.run_id, chairs)


def _progress_watch(
    plan: RunPlan,
    *,
    pod_id: str | None,
    placement_tier: str,
    tree: finish_estimate.RunTreeProgress,
    now: Callable[[], datetime],
) -> progress_watch.ProgressWatch:
    """The progress check for this run, with each stage's planned pace at this tier."""

    expected, problems = progress_watch.expected_rates(
        models_config=plan.models_config,
        serving_recipes_config=plan.serving_recipes_config,
        decoding_config=plan.repository / "config" / "decoding.toml",
        tier=placement_tier,
    )
    run_directory = plan.run_root / plan.run_id
    return progress_watch.ProgressWatch(
        run_id=plan.run_id,
        path=plan.progress_path,
        guard_line=_guard_file(plan.bootstrap.volume_mount_path, pod_id, "progress"),
        expected=expected,
        expected_problems=problems,
        stage_hint=lambda: progress_watch.transcript_stage(plan.transcript_path),
        count=tree.count,
        change=lambda: (_file_size(plan.transcript_path), run_tree_mark(run_directory)),
        now=now,
    )


def _refuse(refusal: PlanRefusal, *, now: Callable[[], datetime]) -> int:
    print(f"pod_run refused: {refusal}", file=sys.stderr)
    failure = _write_refusal(refusal.report_path, str(refusal), now=now)
    if failure is not None:
        print(f"pod_run refusal report could not be written: {failure}", file=sys.stderr)
    return EXIT_REFUSED


class BoundedTranscript:
    """The child's merged output, head written live and tail kept for the close.

    Two bounds rather than one file that grows forever (the run tree needs the
    volume's space) and rather than one ring buffer (which would leave nothing
    durable until the process ended).
    The head reaches the volume as it arrives, so a pod_run that is SIGKILLed
    still leaves the start of the run readable; the tail is the last
    ``tail_bytes`` seen, appended after a truncation marker when the file is
    closed, because the traceback or refusal that says why a run stopped is at
    the end of the stream.

    ``dropped_bytes`` is the middle that was dropped; the run report records
    it under ``records_at_close.transcript``, so a truncated transcript never
    reads as whole.

    What is *not* durable is the tail of a transcript that has already passed
    the head bound, in the window before ``close``: making it durable costs a
    rewrite of the whole file on every tick, and the case it would cover -- a
    multi-megabyte transcript *and* a killed supervisor -- still leaves eight
    megabytes of durable head to read.
    """

    __slots__ = ("_handle", "_head_room", "_tail", "_tail_bytes", "_overflow")

    def __init__(self, path: Path, *, head_bytes: int, tail_bytes: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        self._handle = os.fdopen(descriptor, "wb")
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

    @property
    def dropped_bytes(self) -> int:
        return self._overflow - len(self._tail)

    def close(self) -> None:
        try:
            if self._overflow:
                dropped = self.dropped_bytes
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
    pod runs carries the child's whole output. The transcript is the copy that
    survives the pod.

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
) -> RunnerResult:
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
        # A close that fails (a full volume at the fsync, or a reader still
        # attached closing it first) is a transcript failure, not a start
        # failure: the orchestrator has already run, and its exit code must
        # still reach the report.
        try:
            writer.close()
        except Exception as error:  # noqa: BLE001 -- recorded; the report must still be written
            failure.append(
                f"the transcript close failed ({type(error).__name__}: {error}); its tail "
                "may not have reached the volume"
            )
    return RunnerResult(
        child.returncode,
        transcript_failure=failure[0] if failure else None,
        transcript_dropped_bytes=writer.dropped_bytes,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    now: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    actions_factory: Callable[[Plan], BootstrapActions] = build_actions,
    runner: Runner = _run,
    notify_runner: RunnerFactory = environment_runner,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Help only when asked for alone: inside a run's argv a stray -h is refused like any
    # other unknown flag, so a launch never ends with a help page and exit 0.
    if len(raw_argv) == 1 and raw_argv[0] in HELP_FLAGS:
        print(usage(), end="")
        return 0
    environment = os.environ if environ is None else environ
    try:
        refuse_credential_looking_argv(raw_argv)
        run_argv, bootstrap_argv = split_argv(raw_argv)
    except PlanRefusal as refusal:
        return _refuse(refusal, now=now)
    # The token is read before `prepare` scrubs the environment: its own name
    # is credential-shaped and would be gone afterwards.
    launch_token = environment.get("VERBATUS_LAUNCH_TOKEN") or None
    shell_pod_id = environment.get(POD_ID_ENVIRONMENT) or None
    rates = {name: environment.get(name) for name in HOURLY_RATE_ENVIRONMENT}
    sealed_budget = {
        name: environment.get(name)
        for name in (*POD_BUDGET_ENVIRONMENT.values(), POD_BUDGET_SWITCH_ENVIRONMENT)
    }
    # Only the container's first process names this pod (see PID1_ENVIRON).
    pod_id = container_pod_id()
    try:
        bootstrap_plan, hard_deadline = bootstrap_main.prepare(bootstrap_argv, environment, now=now)
    except PlanRefusal as refusal:
        return bootstrap_main.refuse(refusal, plan=None, now=now, label="pod_run (bootstrap argv)")
    try:
        head = bootstrap_main.RefusingParser()
        head.add_argument("--report-path", type=Path, required=True)
        run_report = _run_report_path(
            head.parse_known_args(run_argv)[0].report_path, bootstrap_plan, launch_token
        )
        args = build_parser().parse_flags(run_argv, run_report)
        plan = resolve_run_plan(args, bootstrap_plan, launch_token)
        if hard_deadline is None and not plan.no_hold:
            raise RunRefusal(
                f"{bootstrap_main.HARD_DEADLINE_ENV}={bootstrap_main.NO_HARD_DEADLINE} says "
                "there is no deadline, which only a run that will not hold can use: add "
                "--no-hold, or set a hard deadline",
                report_path=plan.report_path,
            )
        if plan.no_hold and launch_token:
            raise RunRefusal(
                "--no-hold is for a run started by hand; under a launch token the pod timer "
                "reads its early exit as completed-early",
                report_path=plan.report_path,
            )
        if plan.no_hold:
            _require_live_guard_for_release(
                bootstrap_plan.volume_mount_path,
                pod_id,
                shell_pod_id,
                report_path=plan.report_path,
                now=now,
            )
        bootstrap_plan = replace(
            bootstrap_plan, preflight_roles=tuple(sorted(plan.required_chairs()))
        )
        plan = replace(plan, bootstrap=bootstrap_plan)
        approved_roots, skipped_roots = require_approved_submission_folder(plan)
        _hydrate_local_run(plan)
        _require_selection_predecessor(plan)
        _require_sealed_run_inputs(plan)
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
        # to guess which.
        "skipped_storage_roots": list(skipped_roots),
        "hard_deadline": None if hard_deadline is None else _stamp(hard_deadline),
        "started_at": started_at,
    }
    _write_run_report(plan, {**base, "state": "bootstrapping", "exit_code": None})
    backup = BackupList(
        _guard_file(plan.bootstrap.volume_mount_path, pod_id, "backup"),
        (plan.run_root / plan.run_id, plan.volume_run_root / plan.run_id),
    )
    backup.refresh()
    progress_line = _guard_file(plan.bootstrap.volume_mount_path, pod_id, "progress")

    # The bootstrap has no liveness tick: a thread tells the guard which step runs, so a
    # long hash or model load at low CPU is not read as an idle pod.
    journal_path = bootstrap_plan.journal
    with progress_watch.ProgressTicker(
        progress_line,
        status="bootstrapping",
        late_status="bootstrapping",
        check="bootstrap",
        step=lambda: (
            "running" if journal_path is None else progress_watch.bootstrap_step(journal_path)
        ),
        now=now,
        interval_seconds=plan.interval_seconds,
    ):
        report = bootstrap_main.run_bootstrap(
            bootstrap_plan,
            now=now,
            actions_factory=actions_factory,
            environment=environment,
            # A journal another pod left on the shared volume is set aside, not resumed.
            pod_id=pod_id if _is_pod_id(pod_id) else None,
        )
    if isinstance(report, bootstrap_main.BootstrapRefused):
        _write_run_report(
            plan,
            {
                **base,
                "state": "refused",
                "exit_code": EXIT_REFUSED,
                "reason": report.reason,
                "bootstrap": report.report.to_record() if report.report is not None else None,
                "finished_at": _stamp(now()),
            },
        )
        backup.clear()
        _remove(progress_line)
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
        backup.clear()
        _remove(progress_line)
        return EXIT_BOOTSTRAP_RED
    try:
        placement_tier, serving_config_inputs = _placement_tier(report)
        capacity_plan = _capacity_plan(report, placement_tier)
        receipt = report.receipts.get("preflight")
        smokes = receipt.get("smoke_receipts") if isinstance(receipt, dict) else None
        smoked = (
            {item.get("chair") for item in smokes if isinstance(item, dict)}
            if isinstance(smokes, list)
            else set()
        )
        required = plan.required_chairs(configured_only=True)
        # A chair its own stage runs has no engine to smoke-read. An in-process
        # chair shows its verified cache; a subprocess chair (Surya) its verified
        # cache and the receipt of its own runner reading the golden page.
        in_process = required & _receipt_chairs(receipt, "placements", state="in-process")
        subprocess_run = required & _receipt_chairs(receipt, "placements", state="subprocess")
        cached = _receipt_chairs(receipt, "cache_receipts")
        measured = _receipt_chairs(receipt, "subprocess_receipts")
        missing = (
            (required - in_process - subprocess_run - smoked)
            | ((in_process | subprocess_run) - cached)
            | (subprocess_run - measured)
        )
        if missing:
            raise RunRefusal(
                "selection needs a chair without green PREFLIGHT evidence (a smoke receipt, "
                f"or a verified cache and, for a subprocess chair, its run): {sorted(missing)}"
            )
        # Private and new, so the only stop record in it is this invocation's.
        try:
            stop_directory = tempfile.TemporaryDirectory(
                prefix="pod-run-stop-", ignore_cleanup_errors=True
            )
        except OSError as error:
            raise RunRefusal(
                f"no private directory for the orchestrator's stop record: {error}"
            ) from error
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
        backup.clear()
        _remove(progress_line)
        return EXIT_REFUSED

    stop_record = Path(stop_directory.name) / "stop.json"
    command = plan.orchestrator_argv(stop_record)
    command += ["--placement-tier", placement_tier]
    if capacity_plan is not None:
        command += ["--capacity-plan", capacity_plan.to_argument()]
    running: dict[str, object] = {
        **base,
        "bootstrap": report.to_record(),
        "placement_tier": placement_tier,
        "capacity_plan": capacity_plan.to_record() if capacity_plan is not None else None,
        "serving_config_inputs": serving_config_inputs,
        "orchestrator_argv": command,
        # Named in the report, not only written beside it: a fetched report is
        # what a later session reads first, and a record it cannot name is a
        # record nobody asks the volume for.
        "transcript_path": str(plan.transcript_path),
        "liveness_path": str(plan.liveness_path),
        "hold_path": str(plan.hold_path),
        "timing_journal_path": str(plan.timing_journal_path),
        "estimate_path": str(plan.estimate_path),
        "progress_path": str(plan.progress_path),
    }
    _write_run_report(plan, {**running, "state": "running", "exit_code": None})
    journal = _liveness_journal(plan, base, now=now)
    keepalive = _guard_keepalive(plan.bootstrap.volume_mount_path, pod_id)
    tree = _run_tree_progress(plan)
    sample = progress_watch.TickSample(tree.sample)
    watch = _progress_watch(plan, pod_id=pod_id, placement_tier=placement_tier, tree=tree, now=now)
    deadline_watch = _deadline_watch(
        plan,
        pod_id=pod_id,
        hard_deadline=hard_deadline,
        launch_token=launch_token,
        rates=rates,
        sealed_budget=sealed_budget,
        notify=args.notify,
        notify_runner=notify_runner,
        sample=sample,
        now=now,
    )

    def liveness(pid: int, alive: bool) -> None:
        # Only a run that keeps its pace holds the pod. The guard reads the progress
        # line and owns every notice about a slow or stalled run.
        journal(pid, alive)
        if not alive:
            return
        backup.refresh()
        current = sample.refresh()
        try:
            deadline_watch.tick()
        except Exception as error:  # noqa: BLE001 -- an estimate never stops a running stage
            deadline_watch.note_failure(error)
        if watch.tick(current) == "ok":
            keepalive()

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
        transcript_failure = completed.transcript_failure
        transcript_dropped_bytes = completed.transcript_dropped_bytes
    except OSError as error:
        orchestrator_exit = None
        failure_detail = f"the orchestrator could not start: {error}"
        transcript_failure = None
        transcript_dropped_bytes = 0
    # Once its selection starts, the orchestrator writes its stop record on
    # every return. A refusal before that, or an orchestrator that never
    # started, leaves none.
    stop, stop_problem = (
        (None, None)
        if orchestrator_exit is None
        else read_stop_record(stop_record, plan.run_id, orchestrator_exit)
    )
    exported = stop is not None and stop["exported"]
    systemic = None if stop is None else stop["systemic"]
    stop_directory.cleanup()
    exit_code = _ORCHESTRATOR_EXITS.get(orchestrator_exit, EXIT_FAILED)
    if exit_code == EXIT_COMPLETE and plan.ends_before_armarium:
        exit_code = EXIT_SELECTION_COMPLETE
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
    sync_failure = None
    if plan.local_run:
        try:
            with progress_watch.ProgressTicker(
                progress_line,
                status="ok",
                # A copy running long is still copying: warned about, never deleted.
                late_status="slow",
                check="final-sync",
                step=lambda: "final volume sync",
                now=now,
                interval_seconds=plan.interval_seconds,
            ):
                RunTreeSync(plan.run_root / plan.run_id, plan.volume_run_root / plan.run_id).sync()
        except (OSError, RunTreeSyncError) as error:
            sync_failure = str(error)
            exit_code = EXIT_FAILED
            failure_detail = f"final volume sync failed: {error}; local evidence is at {plan.run_root / plan.run_id}"
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
    records_at_close, records_missing = _records_at_close(
        plan,
        transcript_failure=transcript_failure,
        transcript_dropped_bytes=transcript_dropped_bytes,
    )
    if records_missing:
        absence = (
            "records this report names were not on the volume at close, or were not this "
            f"run's: {', '.join(records_missing)}; the writer's own reason is in "
            f"{plan.transcript_path} if that survived"
        )
        # A success whose transcript or liveness record is partial is not
        # complete: its evidence is missing, so it is held. The timing journal
        # is a stopwatch and never changes a run's state.
        if exit_code in (EXIT_COMPLETE, EXIT_SELECTION_COMPLETE) and any(
            name != "timing_journal" for name in records_missing
        ):
            exit_code = EXIT_HELD
            failure_detail = f"the orchestrator completed, but {absence}"
        else:
            failure_detail = absence if failure_detail is None else f"{failure_detail}. {absence}"
    if stop_problem is not None:
        # Without its stop record the run cannot say whether it sounded the
        # systemic alarm or reached its export, so it is never complete.
        unknown = (
            f"{stop_problem}, so whether this invocation sounded the systemic alarm or "
            f"reached its export is unknown; read {plan.transcript_path}"
        )
        if exit_code in (EXIT_COMPLETE, EXIT_SELECTION_COMPLETE):
            exit_code = EXIT_HELD
            failure_detail = f"the orchestrator completed, but {unknown}"
        else:
            failure_detail = unknown if failure_detail is None else f"{failure_detail}. {unknown}"
    state = _STATE_FOR_EXIT[exit_code]
    held_before_export = exit_code == EXIT_HELD and not plan.ends_before_armarium and not exported
    holding = (
        exit_code in _HOLD_AFTER_EXITS
        and not plan.ends_before_armarium
        and not held_before_export
        and not plan.no_hold
    )
    if plan.no_hold:
        hold_detail = (
            f"the run ended {state}; --no-hold returns now and asks the pod guard to delete "
            "the pod (guard_release says whether it could). Every record is on the volume, "
            "which outlives the pod"
        )
    elif holding:
        hold_detail = (
            f"the run ended {state}; holding toward the hard deadline so the pod timer does "
            "not read this as completed-early. The pod guard deletes an idle pod, which ends "
            "the hold early; the hold journal's last tick says when it ended"
        )
    elif exit_code == EXIT_SELECTION_COMPLETE:
        hold_detail = (
            "the selected stages completed; returning at once so the pod timer closes the "
            "pod. The run tree is on the volume, which outlives the pod, for the next selection"
        )
    elif held_before_export and stop_problem is not None:
        hold_detail = (
            "the run held, and with no usable stop record whether it reached its Armarium "
            "export is unknown; returning at once so the pod timer closes the pod rather than "
            "billing idle time on a guess. The run tree and every record are on the volume, "
            "which outlives the pod, and `verbatus fetch-run` brings them home"
        )
    elif held_before_export:
        hold_detail = (
            "the run held before its Armarium export, waiting for a person's review; "
            "returning at once so the pod timer closes the pod rather than billing idle time "
            "while it waits. The run tree and every record are on the volume, which outlives "
            "the pod, and `verbatus fetch-run` brings them home"
        )
    elif exit_code == EXIT_HELD:
        hold_detail = (
            "the selection held; returning at once so the pod timer closes the pod rather "
            "than billing idle time. The run tree and every record are on the volume, which "
            "outlives the pod"
        )
    else:
        hold_detail = (
            "the run did not finish; returning at once so the pod timer closes the pod "
            "rather than billing the rest of the lease for nothing. Every record is on the "
            "volume, which outlives the pod"
        )
    final: dict[str, object] = {
        **running,
        "state": state,
        "exit_code": exit_code,
        "orchestrator_exit": orchestrator_exit,
        "detail": failure_detail,
        "records_at_close": records_at_close,
        "records_missing": records_missing,
        "sync_failure": sync_failure,
        "held_to_hard_deadline": holding,
        "hold_detail": hold_detail,
        "deadline_watch": deadline_watch.summary(),
        "progress": watch.summary(),
        "stage_rates": watch.stage_rates(),
        "finished_at": _stamp(now()),
    }
    if stop_problem is not None:
        final = {**final, "stop_record_problem": stop_problem}
    if systemic is not None:
        # The run stopped on, or exported past, more held pages than its sealed
        # review policy allows: a person must decide. With --notify the phone
        # hears of it whatever happens to the pod next; a failed ping changes nothing.
        notice = (
            notify_systemic_from_guard(
                run_id=plan.run_id,
                alarm_line=systemic,
                volume_mount=plan.bootstrap.volume_mount_path,
                runner_factory=notify_runner,
            ).line()
            if args.notify
            else "Phone notification: not sent (no --notify)."
        )
        final = {**final, "systemic": systemic, "systemic_notification": notice}
        print(f"pod_run {plan.run_id}: {systemic}; {notice}")
    _write_run_report(plan, final)
    # The run is over: the guard's counters decide again.
    _remove(progress_line)
    if sync_failure is None:
        backup.clear()
    if plan.no_hold:
        if sync_failure is not None:
            print(
                f"pod_run {plan.run_id}: volume sync failed; the guard was not released: "
                f"{sync_failure}",
                file=sys.stderr,
            )
            return EXIT_FAILED
        # After the final report, so a prompt delete cannot cost the run's record.
        release = release_pod_guard(
            plan.bootstrap.volume_mount_path, pod_id, run_id=plan.run_id, state=state, now=now
        )
        _write_run_report(plan, {**final, "guard_release": release})
        print(f"pod_run {plan.run_id}: {state} (exit {exit_code}); guard release: {release}")
        return exit_code
    if not holding:
        print(
            f"pod_run {plan.run_id}: {state} (exit {exit_code}); returning now so the pod "
            "timer closes the pod"
        )
        return exit_code
    print(f"pod_run {plan.run_id}: {state} (exit {exit_code}); holding to the hard deadline")
    # A run with no hard deadline is refused before the bootstrap unless it will not hold.
    assert hard_deadline is not None
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
