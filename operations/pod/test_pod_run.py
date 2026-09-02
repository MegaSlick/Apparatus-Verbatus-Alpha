"""``pod_run`` offline: fakes-only bootstrap, a recorded orchestrator, no model.

Every test drives ``pod_run.main`` the way ``test_bootstrap_main.py`` drives
``bootstrap_main.main``: an injected ``actions_factory`` in place of git, uv,
Hugging Face and the GPU probe, an injected ``runner`` in place of the
orchestrator subprocess, and an injected clock for every sleep. The fixture
roster (``config/models.toml``) and the fixture serving catalogue are what the
plan names; no chair is ever served and no provider is ever reached.

The last test is the reconciliation the ``pod`` dependency group was to carry
with ``config/serving_recipes_real.toml``. It is a strict expected failure
today because that group cannot exist yet -- the recipe's ``transformers``
pin and the project's ``huggingface_hub`` pin cannot share one environment
(``operations/pod/README.md`` carries the resolver's own words) -- and it
flips red the moment the group lands so the reconciliation goes live rather
than lapsing.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

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
    """Stands in for the orchestrator subprocess; returns the exit it is told to."""

    returncode: int = 0
    raise_oserror: bool = False
    calls: list[tuple[list[str], Path, dict[str, str]]] = field(default_factory=list)

    def __call__(self, argv, *, cwd, env):  # type: ignore[no-untyped-def]
        self.calls.append((list(argv), Path(cwd), dict(env)))
        if self.raise_oserror:
            raise OSError("no such interpreter")
        return subprocess.CompletedProcess(argv, self.returncode)


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
    argv = _run_argv(
        ws,
        bootstrap_extra=("--serving-recipes-config", str(real_recipes)),
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
    assert report["orchestrator_argv"] == command
    # Then it held: the run finished at once, and the process still ticked to
    # the shared hard deadline rather than exiting into `completed-early`.
    assert clock.seconds == 4.0
    hold = _report(ws, "pod-run-report-hold.json")
    assert hold["state"] == "holding-after-complete"
    assert hold["tick"] == 4


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
        assert "outside its own complete/held/halted vocabulary" in report["detail"]
    else:
        assert report["detail"] is None
    assert _report(ws, "pod-run-report-hold.json")["state"] == f"holding-after-{state}"
    assert clock.seconds == 2.0


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


def test_the_run_report_is_written_before_the_orchestrator_starts(tmp_path: Path) -> None:
    """A crash mid-run leaves a durable ``running`` record, never silence."""

    ws = _prepared(tmp_path)
    clock = Clock()
    seen: list[str] = []

    def runner(argv, *, cwd, env):  # type: ignore[no-untyped-def]
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
    report = _report(ws)
    assert report["schema"] == RUN_REFUSAL_SCHEMA
    assert "reserved to Tyrel" in report["reason"]


def test_refuses_a_credential_looking_value_in_either_half(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)

    exit_code, _runner = _refused(
        ws, _run_argv(ws, bootstrap_extra=("--transfer-prefix", "my-api-key-123"))
    )

    assert exit_code == EXIT_REFUSED
    assert "looks like a credential" in capsys.readouterr().err


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
    """Every vLLM row names the same versions; the group would carry exactly these."""

    pins = _recipe_pins()

    assert set(pins) == {"vllm", "transformers", "qwen-vl-utils", "flash-attn"}
    assert pins["vllm"] == "0.10.1"


@pytest.mark.xfail(
    strict=True,
    raises=KeyError,
    reason=(
        "no `pod` dependency group can be locked today: transformers==4.57.1 requires "
        "huggingface-hub<1.0 while the project pins huggingface_hub==1.26.0 (uv's own "
        "resolution, recorded in operations/pod/README.md). Strict, so the day the group "
        "lands this reconciliation goes live instead of lapsing. `raises=KeyError` narrows "
        "the expected failure to the group's absence: once a `pod` group exists with the "
        "wrong pins the assertion below fails loudly instead of being absorbed, and once it "
        "carries exactly the recipe's pins the marker XPASSes strictly, forcing removal."
    ),
)
def test_the_pod_dependency_group_carries_exactly_the_recipe_pins() -> None:
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
