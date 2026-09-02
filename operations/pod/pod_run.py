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
    data gate           --data-gate-policy, inside the repository

The roster and the serving catalogue are deliberately taken from the bootstrap
plan and not accepted again here: ``PREFLIGHT`` measured that roster against
that catalogue, and a run that named different files would serve chairs no
preflight had looked at.

**Exit codes never read "complete" for a partial run (GOVERNANCE 2).**
``EXIT_COMPLETE`` (0) is returned only when the orchestrator itself returned
``EXIT_COMPLETE``; ``EXIT_HELD`` (3) and ``EXIT_HALTED`` (4) mirror the
orchestrator's own held and halted exits; ``EXIT_REFUSED`` (2) is a named
refusal before anything ran; ``EXIT_BOOTSTRAP_RED`` (5) is a red bootstrap
step, the orchestrator never started; ``EXIT_FAILED`` (6) is an orchestrator
that could not start or exited outside its own vocabulary.  Whatever the
outcome, the report at ``--report-path`` says the same thing durably, under
the launch-bound name, before the exit code says it.

**The bootstrap-and-hold contract is unchanged.**  ``pod_timer.run_with_bootstrap``
treats any child exit before the hard deadline -- exit 0 included -- as
``completed-early`` and closes the pod with a non-green timer report, so after
the run this process holds to the shared hard deadline exactly as
``bootstrap_main`` does, re-journaling a liveness line beside the run report.
That hold is paid idle time between a finished run and the deadline; closing
early on a complete run would be a ``pod_timer`` contract change and is not
made here.

**No placement-tier flag.**  The consult that asked for this entrypoint named
``--placement-tier``; neither the orchestrator nor any stage parser accepts one
as the code stands, and no stage reads a tier.  The tier the sealed launch
measured is the one thing this process can honestly carry: it is read from the
green bootstrap's ``PREFLIGHT`` receipt and recorded in the run report, and a
green bootstrap whose receipt carries no tier is refused by name.

**The data gate is checked before the bootstrap spends anything.**  The
orchestrator's Door refuses a submission folder outside the policy's approved
storage roots, and ``config/data_handling_policy.json`` names none on a volume
-- that listing is a disclosure decision, Tyrel's under hard rule 1.  This
process asks the gate the same question first, so a launch whose volume is not
yet an approved root is refused here, by name, before a model is fetched on a
billing card rather than after.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Sequence

from common.contracts.errors import ContractError
from common.contracts.identities import validate_run_id
from common.stage import EXIT_COMPLETE as ORCHESTRATOR_COMPLETE
from common.stage import EXIT_HELD as ORCHESTRATOR_HELD
from common.stage import EXIT_RUN_HALTED as ORCHESTRATOR_HALTED
from operations.submit import gate

from . import bootstrap_main
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
DEFAULT_RUNS_DIRECTORY = "runs"

EXIT_COMPLETE = 0
EXIT_REFUSED = 2
EXIT_HELD = 3
EXIT_HALTED = 4
EXIT_BOOTSTRAP_RED = 5
EXIT_FAILED = 6

_STATE_FOR_EXIT = {
    EXIT_COMPLETE: "complete",
    EXIT_REFUSED: "refused",
    EXIT_HELD: "held",
    EXIT_HALTED: "halted",
    EXIT_BOOTSTRAP_RED: "bootstrap-red",
    EXIT_FAILED: "failed",
}

_ORCHESTRATOR_EXITS = {
    ORCHESTRATOR_COMPLETE: EXIT_COMPLETE,
    ORCHESTRATOR_HELD: EXIT_HELD,
    ORCHESTRATOR_HALTED: EXIT_HALTED,
}

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

        return self.report_path.with_name(f"{self.report_path.stem}-hold{self.report_path.suffix}")

    def orchestrator_argv(self) -> list[str]:
        return [
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
        ]

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
            "fixture": self.fixture,
            "interval_seconds": self.interval_seconds,
            "dry_run": self.dry_run,
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
    )


def require_approved_submission_folder(plan: RunPlan) -> tuple[str, ...]:
    """Ask the data gate what the Door will ask, before the bootstrap spends anything.

    Returns the approved roots for the run report.  A refusal names the policy
    file and says whose decision the missing root is.
    """

    try:
        policy = gate.load_policy(plan.data_gate_policy)
        roots = gate.approved_storage_roots(policy)
        gate.require_approved_storage_location(
            plan.submission_folder, roots, "submission folder on the volume"
        )
    except gate.GateRefusal as error:
        raise RunRefusal(
            f"the data-handling policy {plan.data_gate_policy} does not admit the submission "
            f"folder: {error}. Listing the volume root as an approved storage root is a "
            "disclosure decision reserved to Tyrel (hard rule 1); nothing was fetched and no "
            "run was started",
            report_path=plan.report_path,
        ) from error
    return tuple(str(root) for root in roots)


def _placement_tier(report: BootstrapReport) -> tuple[str, dict[str, object]]:
    receipt = report.receipts.get("preflight")
    tier = receipt.get("placement_tier") if isinstance(receipt, dict) else None
    if not isinstance(tier, str) or not tier:
        raise RunRefusal(
            "the green bootstrap's PREFLIGHT receipt carries no placement_tier; the run cannot "
            "record which measured tier its chairs were preflighted for"
        )
    inputs = receipt.get("serving_config_inputs") if isinstance(receipt, dict) else None
    return tier, dict(inputs) if isinstance(inputs, dict) else {}


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _write_run_report(plan: RunPlan, record: Mapping[str, object]) -> None:
    atomic_write(plan.report_path, canonical_json({"schema": RUN_REPORT_SCHEMA, **record}))


def _write_refusal(report_path: Path | None, reason: str, *, now: Callable[[], datetime]) -> None:
    """Best-effort durable reason, with ``bootstrap_main``'s own rule about parents."""

    if report_path is None or not report_path.parent.is_dir():
        return
    try:
        atomic_write(
            report_path,
            canonical_json({"schema": RUN_REFUSAL_SCHEMA, "reason": reason, "at": _stamp(now())}),
        )
    except OSError:
        pass


def _refuse(refusal: PlanRefusal, *, now: Callable[[], datetime]) -> int:
    print(f"pod_run refused: {refusal}", file=sys.stderr)
    _write_refusal(refusal.report_path, str(refusal), now=now)
    return EXIT_REFUSED


def _run(argv: list[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[bytes]:
    # Streams are inherited, as the orchestrator inherits its stages': the
    # run tree is the evidence, and buffering an unbounded stage transcript in
    # this process would be a second, weaker copy of it.
    return subprocess.run(argv, cwd=cwd, env=dict(env), check=False)


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
        approved_roots = require_approved_submission_folder(plan)
    except PlanRefusal as refusal:
        return _refuse(refusal, now=now)

    if plan.dry_run:
        print(json.dumps(plan.to_record(), sort_keys=True, indent=2))
        return EXIT_COMPLETE

    started_at = _stamp(now())
    base: dict[str, object] = {
        "run_id": plan.run_id,
        "run_root": str(plan.run_root),
        "plan": plan.to_record(),
        "approved_storage_roots": list(approved_roots),
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
    running: dict[str, object] = {
        **base,
        "bootstrap": report.to_record(),
        "placement_tier": placement_tier,
        "serving_config_inputs": serving_config_inputs,
        "orchestrator_argv": command,
    }
    _write_run_report(plan, {**running, "state": "running", "exit_code": None})
    try:
        completed = runner(command, cwd=plan.repository, env=dict(environment))
        orchestrator_exit: int | None = completed.returncode
        failure_detail: str | None = None
    except OSError as error:
        orchestrator_exit = None
        failure_detail = f"the orchestrator could not start: {error}"
    exit_code = _ORCHESTRATOR_EXITS.get(orchestrator_exit, EXIT_FAILED)
    if exit_code == EXIT_FAILED and failure_detail is None:
        failure_detail = (
            f"the orchestrator exited {orchestrator_exit}, outside its own complete/held/halted "
            "vocabulary; read its transcript and the run tree before calling this run anything"
        )
    state = _STATE_FOR_EXIT[exit_code]
    final: dict[str, object] = {
        **running,
        "state": state,
        "exit_code": exit_code,
        "orchestrator_exit": orchestrator_exit,
        "detail": failure_detail,
        "finished_at": _stamp(now()),
    }
    _write_run_report(plan, final)
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
