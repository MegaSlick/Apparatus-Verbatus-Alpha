"""``pod_run`` offline: fakes-only bootstrap, a recorded orchestrator, no model.

Every test drives ``pod_run.main`` the way ``test_bootstrap_main.py`` drives
``bootstrap_main.main``: an injected ``actions_factory`` in place of git, uv,
Hugging Face and the GPU probe, an injected ``runner`` in place of the
orchestrator subprocess, and an injected clock for every sleep. The fixture
roster (``config/models.toml``) and the fixture serving catalogue are what the
plan names; no chair is ever served and no provider is ever reached.

The last two tests are the ``surface.py``/``bootstrap_main`` evidence-prefix
spelling held together, and the reconciliation the ``pod`` dependency group carries with
``config/serving_recipes_real.toml``. The group is locked now -- what could
not share one environment before was ``transformers==4.57.1`` wanting
``huggingface-hub<1.0``, and that is resolved -- so the reconciliation is
live: the test fails on drift between the group and the catalogue's pins, or
on a requirement missing the Linux/x86_64 marker that keeps a laptop
``uv sync`` from resolving torch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from operations.operator import cli as operator_cli
from operations.operator.errors import ErrorCode, OperatorError
from operations.operator.records import SCHEMA as RECEIPT_SCHEMA
from operations.operator.volume_s3 import VolumeSpec

from . import launch as launch_module
from . import pod_run
from .bootstrap import BootstrapStep
from .pod_run import (
    EXIT_BOOTSTRAP_RED,
    EXIT_COMPLETE,
    EXIT_DRY_RUN,
    EXIT_FAILED,
    EXIT_HALTED,
    EXIT_HELD,
    EXIT_REFUSED,
    RUN_REFUSAL_SCHEMA,
    RUN_REPORT_SCHEMA,
    main,
)
from .pod_timer import terminating_path
from .test_bootstrap_main import (
    Clock,
    FakeActions,
    Workspace,
    _argv,
    _environ,
    _never_called,
    _workspace,
)

ROOT = Path(__file__).resolve().parents[2]
TIER = "generic-48gb"
SERVING_INPUTS = {
    "schema": "serving-config-inputs.v1",
    "serving_recipes_sha256": "1" * 64,
    "pod_placement_sha256": "2" * 64,
}


@dataclass
class PreflightedActions(FakeActions):
    """Green everywhere, with the receipt a real PREFLIGHT leaves: a measured tier."""

    def run_preflight(self) -> dict[str, object]:
        return self._step(
            BootstrapStep.PREFLIGHT,
            {"color": "green", "placement_tier": TIER, "serving_config_inputs": SERVING_INPUTS},
        )


@dataclass
class RecordedRunner:
    """Stands in for the orchestrator subprocess; returns the exit it is told to.

    ``ticks`` is how many liveness journals a fake child claims to have lived
    through: the real runner ticks once per poll while the child is alive and
    once more when it is gone, and a double that never called back would leave
    the liveness record untested at this level.
    """

    returncode: int = 0
    raise_oserror: bool = False
    ticks: int = 0
    pid: int = 4242
    # The records a real orchestrator run leaves beside the report: the runner
    # tees the transcript and the orchestrator journals its stage timings. A
    # fake that left neither would make every run read as one whose records
    # never came home, which pod_run now holds for review.
    write_transcript: bool = True
    journal_run_id: str | None = "first-real-run"
    journal_entries: int = 1
    transcript_failure: str | None = None
    calls: list[tuple[list[str], Path, dict[str, str]]] = field(default_factory=list)
    supervision: list[dict[str, object]] = field(default_factory=list)

    def __call__(  # type: ignore[no-untyped-def]
        self, argv, *, cwd, env, transcript, liveness, interval_seconds
    ):
        self.calls.append((list(argv), Path(cwd), dict(env)))
        self.supervision.append(
            {"transcript": Path(transcript), "interval_seconds": interval_seconds}
        )
        if self.raise_oserror:
            raise OSError("no such interpreter")
        transcript = Path(transcript)
        if self.write_transcript:
            transcript.write_bytes(b"orchestrator output\n")
        if self.journal_run_id is not None:
            journal = transcript.with_name(
                transcript.name.replace("-transcript.log", "-timings.json")
            )
            journal.write_text(
                json.dumps(
                    {
                        "schema": "stage-timing-journal.v1",
                        "run_id": self.journal_run_id,
                        "entries": [{}] * self.journal_entries,
                    }
                ),
                encoding="utf-8",
            )
        for _ in range(self.ticks):
            liveness(self.pid, True)
        liveness(self.pid, False)
        return subprocess.CompletedProcess(argv, self.returncode, stderr=self.transcript_failure)


def _policy(ws: Workspace, *, roots: list[str] | None = None) -> Path:
    """The reviewed policy, with the volume listed as an approved root unless told otherwise."""

    record = json.loads((ROOT / "config" / "data_handling_policy.json").read_text("utf-8"))
    record["storage_roots"] = [str(ws.volume)] if roots is None else roots
    target = ws.repository / "config" / "data_handling_policy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record), encoding="utf-8")
    return target


def _submission(ws: Workspace) -> tuple[Path, Path]:
    folder = ws.volume / "submission" / "pages"
    folder.mkdir(parents=True)
    (folder / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    manifest = ws.volume / "submission" / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    return folder, manifest


def _prepared(tmp_path: Path) -> Workspace:
    ws = _workspace(tmp_path)
    _policy(ws)
    _submission(ws)
    return ws


def _run_argv(
    ws: Workspace,
    *,
    run_id: str = "first-real-run",
    report_path: Path | None = None,
    extra: tuple[str, ...] = (),
    bootstrap_extra: tuple[str, ...] = (),
) -> list[str]:
    return [
        "--report-path",
        str(report_path or ws.volume / "pod-run-report.json"),
        "--run-id",
        run_id,
        "--submission-folder",
        str(ws.volume / "submission" / "pages"),
        "--submission-manifest",
        str(ws.volume / "submission" / "manifest.json"),
        "--interval-seconds",
        "1",
        *extra,
        "--",
        *_argv(ws, extra=bootstrap_extra),
    ]


def _report(ws: Workspace, name: str = "pod-run-report.json") -> dict:
    return json.loads((ws.volume / name).read_text(encoding="utf-8"))


# --- the green run: bootstrap, orchestrate over the volume, hold --------------


def test_a_complete_run_exits_zero_after_bootstrap_orchestrator_and_hold(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    actions = PreflightedActions()
    runner = RecordedRunner(returncode=0)
    real_recipes = ws.repository / "config" / "serving_recipes_real.toml"
    real_roster = ws.repository / "config" / "models-real.toml"
    real_context = ws.repository / "config" / "witness_context-real.toml"
    argv = _run_argv(
        ws,
        bootstrap_extra=(
            "--serving-recipes-config",
            str(real_recipes),
            "--witness-context-config",
            str(real_context),
        ),
    )
    argv[argv.index("--models-config") + 1] = str(real_roster)
    environment = _environ(clock, lifetime=4.0, extra={"RUNPOD_S3_ACCESS_KEY": "user_abc"})

    exit_code = main(
        argv,
        environ=environment,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=runner,
    )

    assert exit_code == EXIT_COMPLETE
    assert actions.calls == list(BootstrapStep)
    [(command, cwd, env)] = runner.calls
    assert cwd == ws.repository
    assert command[1:3] == ["-I", str(ws.repository / "pipeline" / "orchestrator" / "run.py")]
    assert command[3:] == [
        "--fixture",
        "synthetic-two-page-v0",
        "--run-id",
        "first-real-run",
        "--run-root",
        str(ws.volume / "runs"),
        "--submission-folder",
        str(ws.volume / "submission" / "pages"),
        "--submission-manifest",
        str(ws.volume / "submission" / "manifest.json"),
        "--data-gate-policy",
        str(ws.repository / "config" / "data_handling_policy.json"),
        "--models-config",
        str(real_roster),
        "--serving-recipes-config",
        str(real_recipes),
        "--witness-context-config",
        str(real_context),
        "--stage-timing-journal",
        str(ws.volume / "pod-run-report-timings.json"),
        # The commit the bootstrap checked out and verified, not one the
        # orchestrator re-derives: REPOSITORY already read the checkout back
        # and refused a tip that was not this pin.
        "--repository-commit",
        "a" * 40,
        "--cache-root",
        str(ws.volume / "chair-cache"),
        "--placement-tier",
        TIER,
    ]
    # The scrubbed environment is what the orchestrator sees: no transfer key.
    assert "RUNPOD_S3_ACCESS_KEY" not in env
    report = _report(ws)
    assert report["schema"] == RUN_REPORT_SCHEMA
    assert report["state"] == "complete"
    assert report["exit_code"] == EXIT_COMPLETE
    assert report["orchestrator_exit"] == 0
    assert report["placement_tier"] == TIER
    assert report["serving_config_inputs"] == SERVING_INPUTS
    assert report["bootstrap"]["color"] == "green"
    assert report["approved_storage_roots"] == [str(ws.volume.resolve())]
    assert report["skipped_storage_roots"] == []
    assert report["orchestrator_argv"] == command
    # Then it held: the run finished at once, and the process still ticked to
    # the shared hard deadline rather than exiting into `completed-early`.
    assert report["held_to_hard_deadline"] is True
    assert clock.seconds == 4.0
    hold = _report(ws, "pod-run-report-hold.json")
    assert hold["state"] == "holding-after-complete"
    assert hold["tick"] == 4


def test_forwards_bootstrap_cache_and_trial_triage_inputs_to_the_orchestrator(
    tmp_path: Path,
) -> None:
    """The normal run uses the cache bootstrap verified and preserves Door triage inputs."""

    ws = _prepared(tmp_path)
    triage = ws.volume / "triage"
    triage.mkdir()
    decision = triage / "decisions.json"
    clusters = triage / "clusters.json"
    recipe = triage / "recipe.json"
    for path in (decision, clusters, recipe):
        path.write_text("{}", encoding="utf-8")
    runner = RecordedRunner()
    clock = Clock()

    assert (
        main(
            _run_argv(
                ws,
                extra=(
                    "--triage-decision-manifest",
                    str(decision),
                    "--triage-clusters",
                    str(clusters),
                    "--triage-producer-recipe",
                    str(recipe),
                ),
            ),
            environ=_environ(clock, lifetime=1.0),
            now=clock.now,
            sleeper=clock.sleep,
            actions_factory=lambda plan: PreflightedActions(),
            runner=runner,
        )
        == EXIT_COMPLETE
    )
    command = runner.calls[0][0]
    assert command[command.index("--cache-root") + 1] == str(ws.volume / "chair-cache")
    for flag, path in (
        ("--triage-decision-manifest", decision),
        ("--triage-clusters", clusters),
        ("--triage-producer-recipe", recipe),
    ):
        assert command[command.index(flag) + 1] == str(path)


@pytest.mark.parametrize(
    ("orchestrator_exit", "expected_exit", "state"),
    [(3, EXIT_HELD, "held"), (4, EXIT_HALTED, "halted"), (1, EXIT_FAILED, "failed")],
)
def test_a_partial_run_never_exits_zero_and_the_report_names_its_state(
    tmp_path: Path, orchestrator_exit: int, expected_exit: int, state: str
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner(returncode=orchestrator_exit)

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=2.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert exit_code == expected_exit
    report = _report(ws)
    assert report["state"] == state
    assert report["exit_code"] == expected_exit
    assert report["orchestrator_exit"] == orchestrator_exit
    if state == "failed":
        assert "outside its own complete/held/halted/fatal vocabulary" in report["detail"]
    else:
        # Was `detail is None`. A held or halted report is the one that most
        # needs a reason, and a null there read as "there is nothing further to
        # say" while the reason was on a container stderr that dies with the
        # pod. It now names the transcript the reason is durable in (F094).
        assert str(ws.volume / "pod-run-report-transcript.log") in report["detail"]
    # A run that finished -- held, like complete -- holds to the hard deadline,
    # because `pod_timer` reads an early child exit as `completed-early` and
    # closes the pod with a non-green timer report. A run that did *not* finish
    # returns at once instead: holding a rented card to the deadline for a
    # halted or failed run bills for nothing (GOVERNANCE 8, hard rule 2), which
    # is the same close the red-bootstrap branch already takes.
    holding = state == "held"
    assert report["held_to_hard_deadline"] is holding
    hold_path = ws.volume / "pod-run-report-hold.json"
    if holding:
        assert _report(ws, "pod-run-report-hold.json")["state"] == f"holding-after-{state}"
        assert clock.seconds == 2.0
    else:
        assert not hold_path.exists()
        assert clock.seconds == 0.0


def test_a_root_the_policy_names_and_this_machine_lacks_is_in_the_run_report(
    tmp_path: Path,
) -> None:
    """GOVERNANCE 2: the narrowing is recorded, not only the approval.

    The shipped policy names two roots and no machine has both -- a pod has no
    local ``private/``, a laptop has no mounted volume -- so the gate almost
    always enforces a shorter list than the policy approved. Naming the skipped
    root only in the all-absent refusal left the ordinary case silent.
    """

    ws = _workspace(tmp_path)
    absent = tmp_path / "never-mounted"
    _policy(ws, roots=[str(ws.volume), str(absent)])
    _submission(ws)
    clock = Clock()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=0),
    )

    assert exit_code == EXIT_COMPLETE
    report = _report(ws)
    assert report["approved_storage_roots"] == [str(ws.volume.resolve())]
    [skipped] = report["skipped_storage_roots"]
    assert str(absent) in skipped and "does not exist" in skipped


def test_a_structural_orchestrator_refusal_is_named_not_called_unrecognised(
    tmp_path: Path,
) -> None:
    """``EXIT_FATAL`` (2) is a named orchestrator exit (`common/stage.py`).

    Reporting it as a code outside the vocabulary sent a reader hunting a
    transcript for a problem the exit code had already named.
    """

    ws = _prepared(tmp_path)
    clock = Clock()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=2),
    )

    assert exit_code == EXIT_FAILED
    report = _report(ws)
    assert "EXIT_FATAL (2)" in report["detail"]
    assert "refused structurally" in report["detail"]
    assert "outside its own" not in report["detail"]


def test_an_orchestrator_that_cannot_start_is_a_failed_run_not_a_traceback(
    tmp_path: Path,
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(raise_oserror=True),
    )

    assert exit_code == EXIT_FAILED
    report = _report(ws)
    assert report["state"] == "failed"
    assert report["orchestrator_exit"] is None
    assert "could not start" in report["detail"]
    # An orchestrator that never started has nothing to hold the pod open for.
    assert report["held_to_hard_deadline"] is False
    assert clock.seconds == 0.0
    assert not (ws.volume / "pod-run-report-hold.json").exists()


def test_a_red_bootstrap_step_never_starts_the_orchestrator(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    actions = PreflightedActions(fail_step=BootstrapStep.CHAIR_CACHE)
    runner = RecordedRunner()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=runner,
    )

    assert exit_code == EXIT_BOOTSTRAP_RED
    assert runner.calls == []
    assert BootstrapStep.PREFLIGHT not in actions.calls
    report = _report(ws)
    assert report["state"] == "bootstrap-red"
    assert report["bootstrap"]["failure_step"] == "chair-cache"
    assert clock.seconds == 0.0  # no hold after a red bootstrap: exit is the close


def test_a_green_bootstrap_without_a_measured_tier_is_refused_by_name(tmp_path: Path) -> None:
    """A preflight receipt with no ``placement_tier`` cannot say what it measured."""

    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: FakeActions(),  # green, but its receipt names no tier
        runner=runner,
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    report = _report(ws)
    assert report["state"] == "refused"
    assert "placement_tier" in report["reason"]


@dataclass
class _TierWithoutServingInputsActions(FakeActions):
    """Green, with a measured tier but no ``serving_config_inputs`` at all."""

    def run_preflight(self) -> dict[str, object]:
        return self._step(
            BootstrapStep.PREFLIGHT,
            {"color": "green", "placement_tier": TIER},
        )


def test_a_green_bootstrap_with_no_serving_config_inputs_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """A preflight receipt with a tier but no digests cannot say what those chairs

    were actually measured against -- the run must not reach "complete" believing
    a serving recipe and pod-placement pair that PREFLIGHT never checked.
    """

    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: _TierWithoutServingInputsActions(),
        runner=runner,
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    report = _report(ws)
    assert report["state"] == "refused"
    assert "serving_config_inputs" in report["reason"]


@dataclass
class _MalformedServingInputsActions(FakeActions):
    """Green, with a tier and a ``serving_config_inputs`` that fails validation."""

    def run_preflight(self) -> dict[str, object]:
        return self._step(
            BootstrapStep.PREFLIGHT,
            {
                "color": "green",
                "placement_tier": TIER,
                # Missing `schema` and carrying a non-hex digest: neither
                # `ServingConfigInputs.from_record`'s field check nor its
                # `is_sha256` check can accept this.
                "serving_config_inputs": {
                    "serving_recipes_sha256": "not-a-digest",
                    "pod_placement_sha256": "2" * 64,
                },
            },
        )


def test_a_green_bootstrap_with_a_malformed_serving_config_inputs_is_refused_by_name(
    tmp_path: Path,
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: _MalformedServingInputsActions(),
        runner=runner,
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    report = _report(ws)
    assert report["state"] == "refused"
    assert "malformed serving_config_inputs" in report["reason"]


def test_the_run_report_is_written_before_the_orchestrator_starts(tmp_path: Path) -> None:
    """A crash mid-run leaves a durable ``running`` record, never silence."""

    ws = _prepared(tmp_path)
    clock = Clock()
    seen: list[str] = []

    def runner(argv, *, cwd, env, transcript, liveness, interval_seconds):  # type: ignore[no-untyped-def]
        seen.append(_report(ws)["state"])
        return subprocess.CompletedProcess(argv, 0)

    main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert seen == ["running"]


def test_dry_run_prints_both_plans_and_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    exit_code = main(
        _run_argv(ws, extra=("--dry-run",)),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_never_called,
        runner=runner,
    )

    assert exit_code == EXIT_DRY_RUN
    assert exit_code != EXIT_COMPLETE
    assert runner.calls == []
    printed = json.loads(capsys.readouterr().out)
    assert printed["run_id"] == "first-real-run"
    assert printed["bootstrap"]["repository"] == str(ws.repository)
    assert not (ws.volume / "pod-run-report.json").exists()


# --- every refusal, by name, before anything runs ----------------------------


def _refused(
    ws: Workspace,
    argv: list[str],
    *,
    environ: dict[str, str] | None = None,
) -> tuple[int, RecordedRunner]:
    clock = Clock()
    runner = RecordedRunner()
    exit_code = main(
        argv,
        environ=_environ(clock) if environ is None else environ,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_never_called,
        runner=runner,
    )
    return exit_code, runner


def test_refuses_without_a_bootstrap_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    argv = _run_argv(ws)
    argv = argv[: argv.index("--")]

    exit_code, runner = _refused(ws, argv)

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert "after a literal --" in capsys.readouterr().err


def test_refuses_a_hold_only_bootstrap_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    argv = _run_argv(ws)
    argv[argv.index("--") + 1 :] = [
        "--volume-mount-path",
        str(ws.volume),
        "--report-path",
        str(ws.report_path),
        "--hold-only",
    ]

    exit_code, _runner = _refused(ws, argv)

    assert exit_code == EXIT_REFUSED
    assert "--hold-only is the drill" in capsys.readouterr().err
    assert _report(ws)["schema"] == RUN_REFUSAL_SCHEMA


def test_a_bootstrap_argv_refusal_is_the_bootstrap_refusal_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    argv = _run_argv(ws)
    index = argv.index("--journal")
    del argv[index : index + 2]

    exit_code, _runner = _refused(ws, argv)

    assert exit_code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "pod_run (bootstrap argv) refused" in err
    assert "missing required plan argument(s): --journal" in err


def test_refuses_a_run_report_path_that_is_the_bootstrap_report_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)

    exit_code, _runner = _refused(ws, _run_argv(ws, report_path=ws.report_path))

    assert exit_code == EXIT_REFUSED
    assert "two records" in capsys.readouterr().err


def test_refuses_a_run_report_path_outside_the_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    outside = tmp_path / "outside" / "pod-run-report.json"

    exit_code, _runner = _refused(ws, _run_argv(ws, report_path=outside))

    assert exit_code == EXIT_REFUSED
    assert "--report-path" in capsys.readouterr().err
    assert not outside.exists()


def test_refuses_a_run_report_path_missing_the_launch_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    ws.report_path = ws.volume / "bootstrap-report-launch-abc123.json"
    ws.journal = ws.volume / "bootstrap-journal-launch-abc123.json"
    clock = Clock()
    environment = _environ(clock, extra={"VERBATUS_LAUNCH_TOKEN": "launch-abc123"})

    exit_code, _runner = _refused(ws, _run_argv(ws), environ=environment)

    assert exit_code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "--report-path" in err and "this launch's token" in err


def test_a_launch_bound_run_report_path_is_accepted(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    ws.report_path = ws.volume / "bootstrap-report-launch-abc123.json"
    ws.journal = ws.volume / "bootstrap-journal-launch-abc123.json"
    clock = Clock()
    environment = _environ(clock, lifetime=1.0, extra={"VERBATUS_LAUNCH_TOKEN": "launch-abc123"})

    exit_code = main(
        _run_argv(ws, report_path=ws.volume / "pod-run-report-launch-abc123.json"),
        environ=environment,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(),
    )

    assert exit_code == EXIT_COMPLETE
    assert _report(ws, "pod-run-report-launch-abc123.json")["state"] == "complete"


def test_refuses_a_run_root_outside_the_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)

    exit_code, _runner = _refused(
        ws, _run_argv(ws, extra=("--run-root", str(tmp_path / "elsewhere")))
    )

    assert exit_code == EXIT_REFUSED
    assert "--run-root" in capsys.readouterr().err


def test_refuses_a_bad_run_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = _prepared(tmp_path)

    exit_code, _runner = _refused(ws, _run_argv(ws, run_id="My-Run"))

    assert exit_code == EXIT_REFUSED
    assert "--run-id refused" in capsys.readouterr().err


def test_a_refusal_report_write_failure_is_named_not_swallowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_write_refusal`` used to return ``None`` whether it wrote the report or
    hit an ``OSError`` -- ``_refuse`` could not tell, so a run that refused
    *and* failed to leave its durable reason exited exactly like a run that
    refused cleanly. GOVERNANCE 2 binds the write failure too: it must be
    named on stderr, and the refusal exit code stays exactly what it was.
    """

    ws = _prepared(tmp_path)

    def broken_atomic_write(path, payload):  # type: ignore[no-untyped-def]
        raise OSError("no space left on device")

    monkeypatch.setattr(pod_run, "atomic_write", broken_atomic_write)

    exit_code, _runner = _refused(ws, _run_argv(ws, run_id="My-Run"))

    assert exit_code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "--run-id refused" in err
    assert "pod_run refusal report could not be written" in err
    assert "no space left on device" in err


def test_refuses_a_missing_submission_folder_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    argv = _run_argv(ws)
    argv[argv.index("--submission-folder") + 1] = str(ws.volume / "submission" / "absent")

    exit_code, _runner = _refused(ws, argv)

    assert exit_code == EXIT_REFUSED
    assert "--submission-folder" in capsys.readouterr().err


def test_refuses_a_missing_submission_manifest_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    (ws.volume / "submission" / "manifest.json").unlink()

    exit_code, _runner = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert "--submission-manifest" in capsys.readouterr().err


def test_refuses_a_submission_outside_the_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    argv = _run_argv(ws)
    argv[argv.index("--submission-folder") + 1] = str(elsewhere)

    exit_code, _runner = _refused(ws, argv)

    assert exit_code == EXIT_REFUSED
    assert "--submission-folder" in capsys.readouterr().err


def test_refuses_a_missing_data_gate_policy_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    (ws.repository / "config" / "data_handling_policy.json").unlink()

    exit_code, _runner = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert "--data-gate-policy" in capsys.readouterr().err


def test_refuses_a_data_gate_policy_outside_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    elsewhere = tmp_path / "elsewhere-policy.json"
    elsewhere.write_text("{}", encoding="utf-8")

    exit_code, _runner = _refused(ws, _run_argv(ws, extra=("--data-gate-policy", str(elsewhere))))

    assert exit_code == EXIT_REFUSED
    assert "--data-gate-policy" in capsys.readouterr().err


def test_refuses_before_bootstrap_when_the_policy_does_not_admit_the_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shipped policy names ``private/`` only; the volume is Tyrel's to list.

    Refused here, before a single model is fetched on a billing card, and the
    refusal says whose decision the missing root is.
    """

    ws = _prepared(tmp_path)
    _policy(ws, roots=["private/"])

    exit_code, runner = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    err = capsys.readouterr().err
    assert "does not admit the submission folder" in err
    assert "reserved to Tyrel" in err


def test_refuses_the_pod_mount_path_when_it_is_only_a_plain_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path a real launch seals must actually be mounted, not merely present.

    ``boot_a_request.py`` seals ``BOOT_A_VOLUME_MOUNT_PATH`` into every real
    launch request. Neither ``bootstrap_main.write_probe`` (a writable
    directory) nor ``gate.resolve_storage_roots`` (an existing directory)
    proves that path is the attached network volume rather than an unmounted
    local substitute on the pod's own ephemeral disk. This test stands a
    plain temporary directory in for that path -- ``tmp_path`` is never
    itself a mount point -- and expects the refusal named in
    ``resolve_run_plan``, before the orchestrator or even the bootstrap runs.
    """

    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run.boot_a_request, "BOOT_A_VOLUME_MOUNT_PATH", str(ws.volume))

    exit_code, runner = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    err = capsys.readouterr().err
    assert "expected network-volume mount point" in err
    assert "does not have anything mounted there" in err


def test_the_pre_bootstrap_refusal_names_a_root_this_machine_did_not_have(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """GOVERNANCE 2, on the path where nothing else gets to say it.

    This gate runs before the bootstrap's own mount diagnostic, so on a pod
    with the volume unmounted it is the only thing an operator reads. Naming
    only the roots that resolved made "the policy does not admit it" look like
    a policy that never listed the folder, when the truth is that the root
    listing it was not there. The skipped root is named, never admitted.
    """

    ws = _prepared(tmp_path)
    absent = tmp_path / "never-mounted"
    _policy(ws, roots=["private/", str(absent)])

    exit_code, runner = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    err = capsys.readouterr().err
    assert "does not admit the submission folder" in err
    assert str(absent) in err and "did not resolve on this machine" in err
    assert str(absent) in _report(ws)["reason"]


def test_actions_that_cannot_be_built_are_a_refusal_not_a_started_run(
    tmp_path: Path,
) -> None:
    """``run_bootstrap`` returns ``EXIT_REFUSED`` when the factory raises.

    Nothing about that reaches the orchestrator, and the run report has to say
    refused: a run tree that never started must never be readable as one that
    finished (GOVERNANCE 2).
    """

    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner(returncode=0)

    def _unbuildable(plan):  # type: ignore[no-untyped-def]
        raise RuntimeError("the workspace has no uv")

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_unbuildable,
        runner=runner,
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    report = _report(ws)
    assert report["state"] == "refused"
    assert report["exit_code"] == EXIT_REFUSED
    assert "actions could not be built" in report["reason"]


def test_refuses_a_credential_looking_value_in_either_half(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)

    exit_code, _runner = _refused(
        ws, _run_argv(ws, bootstrap_extra=("--transfer-prefix", "my-api-key-123"))
    )

    assert exit_code == EXIT_REFUSED
    assert "looks like a credential" in capsys.readouterr().err


# --- the transcript and the liveness tick (F094, F059) ------------------------


def test_the_run_report_names_the_transcript_and_the_liveness_record(tmp_path: Path) -> None:
    """A fetched report is what a later session reads first; it names both files."""

    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner(returncode=0, ticks=2)

    main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    report = _report(ws)
    transcript = ws.volume / "pod-run-report-transcript.log"
    liveness = ws.volume / "pod-run-report-liveness.json"
    assert report["transcript_path"] == str(transcript)
    assert report["liveness_path"] == str(liveness)
    assert report["hold_path"] == str(ws.volume / "pod-run-report-hold.json")
    assert report["timing_journal_path"] == str(ws.volume / "pod-run-report-timings.json")
    assert runner.supervision == [{"transcript": transcript, "interval_seconds": 1.0}]


def test_a_liveness_tick_carries_the_child_pid_and_the_moment_it_was_last_seen(
    tmp_path: Path,
) -> None:
    """A stale tick reading `alive: true` is how a SIGKILLed pod_run looks.

    The last write of a run that ended normally says `alive: false`, so its
    absence beside a `running` report is the signal (GOVERNANCE 2, F059).
    """

    ws = _prepared(tmp_path)
    clock = Clock()

    main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=0, ticks=3, pid=31337),
    )

    tick = _report(ws, "pod-run-report-liveness.json")
    assert tick["schema"] == pod_run.RUN_LIVENESS_SCHEMA
    assert tick["run_id"] == "first-real-run"
    assert tick["pid"] == 31337
    assert tick["alive"] is False
    assert tick["state"] == "orchestrator-exited"
    # Three live ticks then the final one: the counter is not reset by the exit.
    assert tick["tick"] == 3
    assert tick["last_seen"].endswith("Z")
    assert tick["report_path"] == str(ws.volume / "pod-run-report.json")
    assert tick["transcript_path"] == str(ws.volume / "pod-run-report-transcript.log")


@pytest.mark.parametrize(("orchestrator_exit", "expected_exit"), [(3, EXIT_HELD), (4, EXIT_HALTED)])
def test_a_held_or_halted_report_names_where_the_reason_is_instead_of_null_detail(
    tmp_path: Path, orchestrator_exit: int, expected_exit: int
) -> None:
    """`detail: null` on the two outcomes that most need a reason (F094)."""

    ws = _prepared(tmp_path)
    clock = Clock()

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator_exit),
    )

    assert exit_code == expected_exit
    detail = _report(ws)["detail"]
    assert detail is not None
    assert str(ws.volume / "pod-run-report-transcript.log") in detail


def test_the_real_runner_tees_the_child_output_into_the_transcript(tmp_path: Path) -> None:
    """The one test that runs a real child: inheritance is what lost the text.

    A stage's refusal reaches this file only because the orchestrator inherits
    pod_run's streams and each stage inherits the orchestrator's, so one pipe
    at the top captures the whole tree. The child here writes to both streams
    and exits non-zero, which is the shape of the case the transcript exists
    for.
    """

    transcript = tmp_path / "report-transcript.log"
    seen: list[tuple[int, bool]] = []
    program = (
        r"import sys; sys.stdout.write('stage begins\n'); sys.stdout.flush(); "
        r"sys.stderr.write('ContractError: the Perlector refused\n'); sys.exit(2)"
    )

    completed = pod_run._run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        transcript=transcript,
        liveness=lambda pid, alive: seen.append((pid, alive)),
        interval_seconds=0.01,
    )

    assert completed.returncode == 2
    text = transcript.read_text(encoding="utf-8")
    assert "stage begins" in text
    assert "ContractError: the Perlector refused" in text
    # Whatever the scheduling, the last call says the child is gone.
    assert seen[-1][1] is False
    assert seen[-1][0] > 0


def test_a_transcript_past_its_head_bound_keeps_the_tail_and_says_what_it_dropped(
    tmp_path: Path,
) -> None:
    """The end of the stream is the traceback; the middle is what may go."""

    path = tmp_path / "bounded.log"
    writer = pod_run.BoundedTranscript(path, head_bytes=16, tail_bytes=8)
    writer.write(b"A" * 16)
    writer.write(b"B" * 40)
    writer.write(b"C" * 8)
    writer.close()

    text = path.read_text(encoding="utf-8")
    assert text.startswith("A" * 16)
    assert text.endswith("C" * 8)
    assert "40 byte(s) of this transcript were dropped" in text


def test_the_head_of_a_transcript_is_on_disk_before_the_writer_is_closed(
    tmp_path: Path,
) -> None:
    """A pod_run killed mid-run still leaves the start of the run readable."""

    path = tmp_path / "live.log"
    writer = pod_run.BoundedTranscript(path, head_bytes=64, tail_bytes=8)
    writer.write(b"the door opened\n")

    assert path.read_bytes() == b"the door opened\n"
    writer.close()


def test_the_operator_evidence_prefix_names_the_same_directory_as_preflight() -> None:
    """``surface.FETCH_EVIDENCE_PREFIX`` is a second, deliberate spelling of
    ``bootstrap_main.PREFLIGHT_DIRECTORY`` -- ``surface.py`` says so rather than
    import it, to avoid loading the whole serving stack for one directory name.
    ``surface.py``'s own docstring says this file is where the two spellings are
    held together; this is that test.
    """

    from operations.operator.surface import FETCH_EVIDENCE_PREFIX

    assert FETCH_EVIDENCE_PREFIX == pod_run.bootstrap_main.PREFLIGHT_DIRECTORY


# --- naming the launch's records so they can be fetched (F101, F110) ----------


def test_the_sibling_suffixes_launch_derives_are_the_ones_pod_run_actually_writes() -> None:
    """`launch.py` spells these rather than importing pod_run, which would pull the
    whole serving stack in for four strings. This is the reconciliation that keeps
    the copy from drifting."""

    report = Path("/workspace/pod-run-report-abc.json")
    plan = object.__new__(pod_run.RunPlan)
    object.__setattr__(plan, "report_path", report)
    written = {
        plan.hold_path.name.removeprefix(report.stem),
        plan.liveness_path.name.removeprefix(report.stem),
        plan.timing_journal_path.name.removeprefix(report.stem),
        plan.transcript_path.name.removeprefix(report.stem),
    }

    assert written == set(launch_module.RUN_REPORT_SIBLINGS)
    assert set(launch_module.HOLD_REPORT_SIBLINGS) <= written
    assert launch_module.TIMER_REPORT_SIBLINGS == (
        terminating_path(Path("/v/pod-runtime-report.json")).name.removeprefix(
            "pod-runtime-report"
        ),
    )
    assert launch_module._POD_RUN_MODULE == pod_run.__name__


def test_every_launch_bound_record_is_derived_from_the_sealed_start_command() -> None:
    """The operator is not asked to retype a 32-hex token out of a JSON receipt."""

    token = "a" * 32
    nested = json.dumps(
        [
            "python",
            "-m",
            "operations.pod.pod_run",
            f"--report-path=/workspace/pod-run-report-{token}.json",
        ]
    )
    command = (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--report-path",
        f"/workspace/pod-runtime-report-{token}.json",
        "--bootstrap-command-json",
        nested,
    )

    keys = launch_module.launch_evidence_keys(command, volume_mount_path="/workspace")

    assert keys == (
        f"pod-runtime-report-{token}.json",
        f"pod-runtime-report-{token}-terminating.json",
        f"pod-run-report-{token}.json",
        f"pod-run-report-{token}-hold.json",
        f"pod-run-report-{token}-liveness.json",
        f"pod-run-report-{token}-timings.json",
        f"pod-run-report-{token}-transcript.log",
    )


def test_launch_run_id_reads_pod_runs_own_run_id_flag() -> None:
    """`--run-id` is `pod_run`'s own flag, sealed inside the nested argv --
    not a top-level request field the way `volume_id` is, so it can only be
    read out of the sealed command.

    The bootstrap half below carries its own, different `--run-id` so this
    proves `launch_run_id` reads `pod_run`'s half specifically
    (`_nested_argv_halves(nested)[0]`) rather than the whole nested argv
    flatly -- a flat read would find `bootstrap_half`'s `--run-id` first or
    last depending on scan order and could return either value, not
    reliably `pod_run`'s own.
    """

    run_half = [
        "python",
        "-m",
        "operations.pod.pod_run",
        "--report-path",
        "/workspace/pod-run-report.json",
        "--run-id",
        "r1",
    ]
    bootstrap_half = ["--volume-mount-path", "/workspace", "--run-id", "r2"]
    nested = json.dumps([*run_half, "--", *bootstrap_half])
    command = (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--bootstrap-command-json",
        nested,
    )

    assert launch_module.launch_run_id(command) == "r1"


def test_launch_run_id_is_none_for_a_hold_only_launch() -> None:
    """A hold-only boot starts no run and has no `--run-id` to name."""

    hold_only = json.dumps(
        [
            "python",
            "-m",
            "operations.pod.bootstrap_main",
            "--hold-only",
            "--volume-mount-path",
            "/workspace",
        ]
    )
    command = (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--bootstrap-command-json",
        hold_only,
    )

    assert launch_module.launch_run_id(command) is None


def test_launch_run_id_is_none_with_no_bootstrap_command_at_all() -> None:
    command = ("python", "--report-path", "/workspace/pod-runtime-report.json")

    assert launch_module.launch_run_id(command) is None


def test_evidence_prefix_derives_bootstrap_mains_own_preflight_directory() -> None:
    """F110/G11: every real request (boot_a_request.py, boot_b_request.py)
    writes its report paths at the *volume root*, never under `preflight/` --
    only `bootstrap_main.Plan.preflight_root` computes a `preflight/` path,
    from `<mount>/preflight/<bootstrap_main's own --report-path stem>`. A
    full run launch's nested argv is pod_run's own argv with bootstrap_main's
    appended after the first literal `--` (`pod_run.split_argv`); the derived
    prefix must come from bootstrap_main's own report path, not pod_run's and
    not the pod timer's outer one -- reusing `bound_report_paths` here would
    derive a prefix matching nothing on the volume, since it deliberately
    does not distinguish pod_run's nested report from bootstrap_main's."""

    token = "a" * 32
    run_half = [
        "python",
        "-m",
        "operations.pod.pod_run",
        "--report-path",
        f"/workspace/pod-run-report-{token}.json",
        "--run-id",
        "r1",
    ]
    bootstrap_half = [
        "--volume-mount-path",
        "/workspace",
        "--report-path",
        f"/workspace/bootstrap-report-{token}.json",
        "--repository",
        "https://example/repo",
    ]
    nested = json.dumps([*run_half, "--", *bootstrap_half])
    command = (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--bootstrap-command-json",
        nested,
        "--report-path",
        f"/workspace/pod-runtime-report-{token}.json",
    )

    prefixes = launch_module.launch_evidence_prefixes(command, volume_mount_path="/workspace")

    assert prefixes == (f"preflight/bootstrap-report-{token}",)
    # Matches the real formula exactly, not just a plausible-looking string.
    from operations.pod.bootstrap_main import PREFLIGHT_DIRECTORY

    assert prefixes[0] == f"{PREFLIGHT_DIRECTORY}/bootstrap-report-{token}"


def test_evidence_prefix_for_a_hold_only_launch_has_no_nested_dash_dash_split() -> None:
    """Boot A's nested argv is bootstrap_main's own directly -- no pod_run
    wrapper, no literal `--` to split on -- and `_nested_argv_halves` returns
    the whole thing as its own last (and only) half in that case."""

    token = "b" * 32
    hold_only = [
        "python",
        "-m",
        "operations.pod.bootstrap_main",
        "--hold-only",
        "--volume-mount-path",
        "/workspace",
        "--report-path",
        f"/workspace/bootstrap-hold-only-report-{token}.json",
    ]
    command = (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--bootstrap-command-json",
        json.dumps(hold_only),
        "--report-path",
        f"/workspace/pod-runtime-report-{token}.json",
    )

    prefixes = launch_module.launch_evidence_prefixes(command, volume_mount_path="/workspace")

    assert prefixes == (f"preflight/bootstrap-hold-only-report-{token}",)


def test_evidence_prefix_drops_a_bootstrap_report_path_outside_the_volume() -> None:
    """The same volume-boundary rule `launch_evidence_keys` applies."""

    nested = json.dumps(["python", "--report-path", "/elsewhere/bootstrap-report.json"])
    command = (
        "python",
        "--bootstrap-command-json",
        nested,
        "--report-path",
        "/workspace/pod-runtime-report.json",
    )

    assert launch_module.launch_evidence_prefixes(command, volume_mount_path="/workspace") == ()


def test_evidence_prefix_is_empty_with_no_bootstrap_command_at_all() -> None:
    command = ("python", "--report-path", "/workspace/pod-runtime-report.json")

    assert launch_module.launch_evidence_prefixes(command, volume_mount_path="/workspace") == ()


def test_a_hold_only_launch_derives_no_record_pod_run_alone_writes() -> None:
    """A receipt full of refusals for records nothing ever wrote is a worse record
    than none, so which siblings apply is decided by which program writes them."""

    token = "b" * 32
    nested = json.dumps(
        [
            "python",
            "-m",
            "operations.pod.bootstrap_main",
            "--hold-only",
            "--report-path",
            f"/workspace/bootstrap-hold-only-report-{token}.json",
        ]
    )
    command = (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--report-path",
        f"/workspace/pod-runtime-report-{token}.json",
        "--bootstrap-command-json",
        nested,
    )

    keys = launch_module.launch_evidence_keys(command, volume_mount_path="/workspace")

    assert keys == (
        f"pod-runtime-report-{token}.json",
        f"pod-runtime-report-{token}-terminating.json",
        f"bootstrap-hold-only-report-{token}.json",
        f"bootstrap-hold-only-report-{token}-hold.json",
    )


def test_a_report_path_outside_the_volume_is_dropped_not_returned() -> None:
    """A key fetch-run could not fetch anyway; returning it would put a misleading
    name in the receipt."""

    command = (
        "python",
        "--report-path",
        "/elsewhere/pod-runtime-report.json",
        "--bootstrap-command-json",
        "not json at all",
    )

    assert launch_module.launch_evidence_keys(command, volume_mount_path="/workspace") == ()


def test_a_report_path_that_is_the_mount_itself_is_dropped_not_a_traceback() -> None:
    """`relative_to(mount)` answers `.` for the mount, and `.` has no name.

    A receipt carrying such a path made the sibling derivation raise
    `ValueError: PurePosixPath('.') has an empty name`, so `fetch-run` ended in
    a traceback instead of in a key list (CodeRabbit on PR #117). It is dropped
    like any other path this verb could not fetch.
    """

    command = (
        "python",
        "--report-path",
        "/workspace",
        "--bootstrap-command-json",
        "not json at all",
    )

    assert launch_module.launch_evidence_keys(command, volume_mount_path="/workspace") == ()


def _launch_request(token: str, volume_id: str) -> dict:
    return {
        "volume_id": volume_id,
        "volume_mount_path": "/workspace",
        "docker_start_cmd": [
            "python",
            "--report-path",
            f"/workspace/pod-runtime-report-{token}.json",
        ],
    }


def _launch_receipt(path: Path, token: str, *, volume_id: str = "vol-1") -> Path:
    """A launch receipt in the shape `ReceiptStore.write` actually writes one.

    The action's own data sits under `payload`, never at the top level. This
    fixture said otherwise until 2026-09-15, so the console's derivation could
    read a key no real receipt carries and the suite stayed green over it
    (CodeRabbit on PR #117); `test_the_console_derives_those_keys_from_a_real_
    stored_receipt` below builds one through the store itself.
    """

    path.write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "kind": "launch",
                "recorded_at": "2026-09-15T00:00:00Z",
                "payload": {"request": _launch_request(token, volume_id)},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_console_derives_those_keys_from_a_saved_launch_receipt(tmp_path: Path) -> None:
    token = "c" * 32
    receipt = _launch_receipt(tmp_path / "launch.json", token)

    assert operator_cli._derived_evidence_keys(receipt) == (
        f"pod-runtime-report-{token}.json",
        f"pod-runtime-report-{token}-terminating.json",
    )
    assert operator_cli._derived_evidence_keys(None) == ()


def test_the_console_derives_those_keys_from_a_real_stored_receipt(tmp_path: Path) -> None:
    """The receipt written by the store the launch actually uses, not a hand-built one.

    `ReceiptStore.write` wraps every action under `payload`, so a derivation
    reading a top-level `request` refused every genuine launch receipt with
    "does not carry a readable launch request" -- the one failure shape this
    flag exists to prevent, on the flag itself (CodeRabbit on PR #117).
    """

    from operations.operator.records import ReceiptStore

    token = "f" * 32
    receipt = ReceiptStore(tmp_path / "state").write(
        "launch", {"summary": "fixture launch", "request": _launch_request(token, "vol-1")}
    )

    assert operator_cli._derived_evidence_keys(receipt) == (
        f"pod-runtime-report-{token}.json",
        f"pod-runtime-report-{token}-terminating.json",
    )


def test_a_receipt_with_no_payload_wrapper_is_refused_rather_than_read(tmp_path: Path) -> None:
    """A JSON file that is not an operator receipt is not a launch receipt."""

    receipt = tmp_path / "launch.json"
    receipt.write_text(
        json.dumps({"request": _launch_request("a" * 32, "vol-1")}), encoding="utf-8"
    )

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(receipt)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED


def test_a_pathologically_nested_launch_receipt_is_unreadable_not_an_internal_failure(
    tmp_path: Path,
) -> None:
    """Deep nesting inside the byte bound refuses as an unreadable receipt on every
    interpreter: 3.12's decoder recurses out, 3.14's walks it and refuses the integer
    at its own digit limit; neither may reach the console's catch-all."""

    receipt = tmp_path / "launch.json"
    receipt.write_bytes(b"[" * 10_000 + b"9" * 4301 + b"]" * 10_000)

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(receipt)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED


def test_a_launch_receipt_for_another_volume_is_refused_rather_than_used(
    tmp_path: Path,
) -> None:
    """The quiet failure: real names of another launch's records, asked for
    against a volume that never held them."""

    receipt = _launch_receipt(tmp_path / "launch.json", "d" * 32, volume_id="vol-other")

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(
            receipt, VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol-1")
        )

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "vol-other" in refusal.value.detail


def test_a_launch_receipt_for_another_run_is_refused_rather_than_used(tmp_path: Path) -> None:
    """The quieter failure on the *same* volume: every derived key and prefix
    would still resolve to real objects, just the wrong launch's -- stored
    beside the fetched run and misstating its provenance rather than merely
    failing to find them."""

    from operations.operator.records import ReceiptStore

    token = "g" * 32
    nested = json.dumps(
        [
            "python",
            "-m",
            "operations.pod.pod_run",
            "--report-path",
            f"/workspace/pod-run-report-{token}.json",
            "--run-id",
            "r1",
        ]
    )
    request = {
        "volume_id": "vol-1",
        "volume_mount_path": "/workspace",
        "docker_start_cmd": [
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--report-path",
            f"/workspace/pod-runtime-report-{token}.json",
            "--bootstrap-command-json",
            nested,
        ],
    }
    receipt = ReceiptStore(tmp_path / "state").write(
        "launch", {"summary": "fixture launch", "request": request}
    )

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(
            receipt, VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol-1"), "r2"
        )

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "r1" in refusal.value.detail
    assert "r2" in refusal.value.detail

    # Fetching the run the receipt actually started still derives normally.
    assert operator_cli._derived_evidence_keys(
        receipt, VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol-1"), "r1"
    ) == (
        f"pod-runtime-report-{token}.json",
        f"pod-runtime-report-{token}-terminating.json",
        f"pod-run-report-{token}.json",
        f"pod-run-report-{token}-hold.json",
        f"pod-run-report-{token}-liveness.json",
        f"pod-run-report-{token}-timings.json",
        f"pod-run-report-{token}-transcript.log",
    )


def test_a_launch_receipt_read_through_a_link_is_refused(tmp_path: Path) -> None:
    """A record this verb did not write is not read whole on trust."""

    real = _launch_receipt(tmp_path / "launch.json", "e" * 32)
    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(link)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED


def test_an_unreadable_launch_receipt_refuses_rather_than_deriving_nothing(
    tmp_path: Path,
) -> None:
    """An operator who named a receipt and silently got no keys would believe the
    reports came home."""

    receipt = tmp_path / "launch.json"
    receipt.write_text("{}", encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(receipt)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED


def test_the_console_offers_a_prefix_for_this_runs_own_preflight_tree() -> None:
    """A volume is reused across launches, so `preflight/` holds every launch's
    tree; without a prefix a later reader cannot say which measured this run."""

    parsed = operator_cli.build_parser().parse_args(
        [
            "fetch-run",
            "--run-id",
            "r1",
            "--into",
            "/tmp/into",
            "--network-volume",
            "EU-CZ-1:vol",
            "--evidence-prefix",
            "preflight/bootstrap-report-abc",
        ]
    )

    assert parsed.evidence_prefix == ["preflight/bootstrap-report-abc"]
    assert parsed.launch_receipt is None


# --- the pod dependency group and the recipe's pins ---------------------------


def _recipe_pins() -> dict[str, str]:
    catalogue = tomllib.loads(
        (ROOT / "config" / "serving_recipes_real.toml").read_text(encoding="utf-8")
    )
    pins: dict[str, str] = {}
    for profile in catalogue["profiles"]:
        for package, version in profile.get("required_packages", {}).items():
            assert pins.setdefault(package, version) == version, (
                f"the real catalogue pins {package} at two versions"
            )
        assert profile["kind"] != "vllm" or "vllm" in profile["required_packages"]
    return pins


def test_the_real_catalogue_pins_one_serving_stack() -> None:
    """Every vLLM row names the same versions; the group carries exactly these.

    The stack is the one researched for the four ruled chairs: vLLM 0.27.1 registers
    both architectures the roster declares — `Qwen3_5ForConditionalGeneration`
    (Chandra-2 and the Perlector) and `Qwen2_5_VLForConditionalGeneration` (the DAI
    fine-tune and Churro-3B) — and, unlike 0.28.0, states no direct
    `huggingface_hub` floor, so the project's `huggingface_hub==1.26.0` stands. No
    `flash-attn`: vLLM brings its own FlashAttention through its attention backend
    registry, and the PyPI package is sdist-only.
    """

    pins = _recipe_pins()

    assert set(pins) == {"vllm", "transformers", "qwen-vl-utils"}
    assert pins["vllm"] == "0.27.1"
    assert pins["transformers"] == "5.14.1"


def test_the_pod_dependency_group_carries_exactly_the_recipe_pins() -> None:
    """The locked group and the catalogue's rows are the same bytes, both ways.

    This was a strict expected failure while no `pod` group could be locked at all
    (`transformers==4.57.1` wanted `huggingface-hub<1.0`). The group exists now, so
    the reconciliation is live: `ServingManager` checks each `required_packages` pin
    through `importlib.metadata` before it launches, and a group that drifted from
    the catalogue would mean a pod that installs the stack and is then refused.
    Every requirement must also carry the Linux/x86_64 marker, which is what keeps a
    laptop `uv sync` from resolving torch.
    """

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = pyproject["dependency-groups"]["pod"]
    marker = "sys_platform == 'linux' and platform_machine == 'x86_64'"
    observed: dict[str, str] = {}
    for requirement in group:
        assert isinstance(requirement, str)
        spec, _, condition = requirement.partition(";")
        assert condition.strip() == marker, requirement
        name, _, version = spec.strip().partition("==")
        observed[name.strip().lower()] = version.strip()

    assert observed == _recipe_pins()


# --- the records the report names are audited at close (CodeRabbit, PR #117) ----


def _run_with(ws: Workspace, runner: RecordedRunner) -> dict:
    clock = Clock()
    main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    return _report(ws)


def test_a_complete_run_whose_named_records_all_came_home_reports_nothing_missing(
    tmp_path: Path,
) -> None:
    ws = _prepared(tmp_path)
    report = _run_with(ws, RecordedRunner(returncode=0, ticks=1, journal_entries=2))

    assert report["state"] == "complete"
    assert report["exit_code"] == EXIT_COMPLETE
    assert report["records_missing"] == []
    assert report["detail"] is None
    audit = report["records_at_close"]
    assert audit["transcript"]["present"] is True
    assert audit["liveness"]["present"] is True
    assert audit["timing_journal"] == {
        "path": str(ws.volume / "pod-run-report-timings.json"),
        "present": True,
        "entries": 2,
        "run_id_matches": True,
    }


def test_a_completed_run_whose_timing_journal_never_landed_is_held_not_complete(
    tmp_path: Path,
) -> None:
    """The orchestrator journals best-effort and says so on stderr; the report that
    names the journal must not read `complete` over its absence (nothing is lost
    silently). The run is held for review, and holds to the deadline as a complete
    run would, so the meter does not change."""

    ws = _prepared(tmp_path)
    report = _run_with(ws, RecordedRunner(returncode=0, ticks=1, journal_run_id=None))

    assert report["state"] == "held"
    assert report["exit_code"] == EXIT_HELD
    assert report["orchestrator_exit"] == 0
    assert report["held_to_hard_deadline"] is True
    assert report["records_missing"] == ["timing_journal"]
    assert report["records_at_close"]["timing_journal"]["present"] is False
    assert report["detail"].startswith("the orchestrator completed, but")
    assert "timing_journal" in report["detail"]
    assert str(ws.volume / "pod-run-report-transcript.log") in report["detail"]
    assert _report(ws, "pod-run-report-hold.json")["state"] == "holding-after-held"


@pytest.mark.parametrize(
    ("runner", "failure_fragment"),
    [
        (RecordedRunner(returncode=0, journal_run_id="some-other-run"), "some-other-run"),
        (RecordedRunner(returncode=0, journal_entries=0), "has no entries"),
    ],
)
def test_a_journal_that_is_not_this_runs_or_is_empty_counts_as_missing(
    tmp_path: Path, runner: RecordedRunner, failure_fragment: str
) -> None:
    ws = _prepared(tmp_path)
    report = _run_with(ws, runner)

    assert report["state"] == "held"
    assert report["records_missing"] == ["timing_journal"]
    entry = report["records_at_close"]["timing_journal"]
    assert entry["present"] is True
    assert failure_fragment in entry["failure"]


def test_a_held_run_keeps_its_own_reason_and_appends_the_missing_records(
    tmp_path: Path,
) -> None:
    ws = _prepared(tmp_path)
    report = _run_with(
        ws, RecordedRunner(returncode=3, ticks=1, write_transcript=False, journal_run_id=None)
    )

    assert report["state"] == "held"
    assert report["records_missing"] == ["transcript", "timing_journal"]
    assert "the stage's own reason is the last text in" in report["detail"]
    assert "transcript, timing_journal" in report["detail"]


def test_a_transcript_the_runner_reports_incomplete_holds_the_run(tmp_path: Path) -> None:
    """A transcript file that exists but lost text part-way is not a record that
    came home; the runner says so and the run is held rather than complete."""

    ws = _prepared(tmp_path)
    report = _run_with(
        ws, RecordedRunner(returncode=0, transcript_failure="the transcript write failed")
    )

    assert report["state"] == "held"
    assert report["records_missing"] == ["transcript"]
    entry = report["records_at_close"]["transcript"]
    assert entry["present"] is True
    assert entry["failure"] == "the transcript write failed"


def test_an_oversized_or_pathological_journal_is_unreadable_not_an_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit runs before the final report is written; a journal the decoder
    cannot take must be a named failure in that report, never an exception that
    leaves the report saying `running`."""

    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "TIMING_JOURNAL_READ_BYTES", 64)
    runner = RecordedRunner(returncode=0, journal_entries=50)
    report = _run_with(ws, runner)
    assert report["records_missing"] == ["timing_journal"]
    assert "larger than 64 bytes" in report["records_at_close"]["timing_journal"]["failure"]

    monkeypatch.setattr(pod_run, "TIMING_JOURNAL_READ_BYTES", 4 * 1024 * 1024)
    plan = object.__new__(pod_run.RunPlan)
    object.__setattr__(plan, "report_path", tmp_path / "audit.json")
    object.__setattr__(plan, "run_id", "first-real-run")
    plan.transcript_path.write_bytes(b"x")
    plan.liveness_path.write_bytes(b"{}")
    # The nesting wraps a 4,301-digit integer, as the branch's other deep-nesting
    # regressions do: 3.12 recurses out of the decoder and 3.14 walks the nesting
    # and refuses the integer at its own digit limit, so the audit lands in its
    # unreadable path on either interpreter rather than on a shape check that
    # happens to agree.
    plan.timing_journal_path.write_bytes(b"[" * 10_000 + b"9" * 4301 + b"]" * 10_000)
    audit, missing = pod_run._records_at_close(plan)
    assert missing == ["timing_journal"]
    assert audit["timing_journal"]["failure"].startswith("unreadable: ")


def test_a_transcript_write_that_fails_part_way_is_reported_and_the_pipe_still_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pump keeps reading after its writer fails, so the child is never blocked
    on a full pipe, and the failure reaches the runner's return rather than dying
    in the thread."""

    transcript = tmp_path / "report-transcript.log"

    def failing_write(self: pod_run.BoundedTranscript, chunk: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pod_run.BoundedTranscript, "write", failing_write)
    # More than one pipe buffer's worth, so a reader that stopped would block the child.
    program = "import sys; sys.stdout.write('x' * 300_000); sys.stdout.flush()"

    completed = pod_run._run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        transcript=transcript,
        liveness=lambda pid, alive: None,
        interval_seconds=0.01,
    )

    assert completed.returncode == 0
    assert "the transcript write failed part-way" in completed.stderr
    assert "No space left on device" in completed.stderr


def test_a_descendant_holding_the_pipe_cannot_stop_the_runner_from_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child exits at once but leaves a grandchild holding its stdout; the
    reader's join is bounded so `_run` returns, and the transcript is reported
    incomplete rather than the final report never being written."""

    monkeypatch.setattr(pod_run, "TRANSCRIPT_READER_JOIN_SECONDS", 0.2)
    transcript = tmp_path / "report-transcript.log"
    program = (
        "import subprocess, sys; sys.stdout.write('parent\\n'); sys.stdout.flush(); "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])"
    )

    completed = pod_run._run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        transcript=transcript,
        liveness=lambda pid, alive: None,
        interval_seconds=0.01,
    )

    assert completed.returncode == 0
    assert "still attached" in completed.stderr
    assert "parent" in transcript.read_text(encoding="utf-8")
