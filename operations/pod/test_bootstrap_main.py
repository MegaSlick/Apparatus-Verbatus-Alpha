"""Fakes-only proof of bootstrap_main's hold loop, refusals, and env scrub.

No git, uv, Hugging Face, or GPU probe is ever invoked here: every test injects
``actions_factory`` in place of :func:`operations.pod.bootstrap_main.build_actions`,
matching the house style in ``test_pod_runtime.py`` of driving the supervisor,
armer, and timer with fakes and an injected clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from .bootstrap import BootstrapStep, BootstrapStepFailure
from .bootstrap_main import (
    HARD_DEADLINE_ENV,
    HOLD_SCHEMA,
    PlanRefusal,
    build_parser,
    hold,
    main,
    resolve_plan,
    scrub_environment,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Clock:
    seconds: float = 0.0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass
class FakeActions:
    """A minimal stand-in for every ``BootstrapActions`` method, all green by default."""

    fail_step: BootstrapStep | None = None
    calls: list[BootstrapStep] = field(default_factory=list)

    def _step(self, step: BootstrapStep, receipt: dict[str, object]) -> dict[str, object]:
        self.calls.append(step)
        if self.fail_step is step:
            raise BootstrapStepFailure(step, "injected failure", "repair the injected failure")
        return receipt

    def checkout_commit(self, commit: str) -> dict[str, object]:
        return self._step(BootstrapStep.REPOSITORY, {"commit": commit})

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        return self._step(BootstrapStep.UV_ENVIRONMENT, {"lockfile": str(lockfile)})

    def resume_transfer(self) -> dict[str, object]:
        return self._step(BootstrapStep.TRANSFER, {"state": "nothing-to-transfer"})

    def materialize_model_store(self) -> dict[str, object]:
        return self._step(BootstrapStep.MODEL_STORE, {"real_roster_complete": True})

    def verify_chair_cache(self) -> dict[str, object]:
        return self._step(BootstrapStep.CHAIR_CACHE, {"chairs": []})

    def run_preflight(self) -> dict[str, object]:
        return self._step(BootstrapStep.PREFLIGHT, {"color": "green"})


@dataclass
class Workspace:
    volume: Path
    repository: Path
    journal: Path
    report_path: Path
    store_root: Path
    models_config: Path
    placement_config: Path


def _workspace(tmp_path: Path) -> Workspace:
    volume = tmp_path / "volume"
    volume.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return Workspace(
        volume=volume,
        repository=repository,
        journal=volume / "bootstrap-journal.json",
        report_path=volume / "bootstrap-report.json",
        store_root=tmp_path / "store",
        models_config=tmp_path / "config" / "models.toml",
        placement_config=tmp_path / "config" / "pod_placement.toml",
    )


def _argv(ws: Workspace, *, commit: str = "a" * 40, extra: tuple[str, ...] = ()) -> list[str]:
    return [
        "--volume-mount-path",
        str(ws.volume),
        "--report-path",
        str(ws.report_path),
        "--interval-seconds",
        "1",
        "--repository",
        str(ws.repository),
        "--repository-commit",
        commit,
        "--lockfile",
        str(ws.repository / "uv.lock"),
        "--journal",
        str(ws.journal),
        "--store-root",
        str(ws.store_root),
        "--models-config",
        str(ws.models_config),
        "--placement-config",
        str(ws.placement_config),
        *extra,
    ]


def _environ(
    clock: Clock, *, lifetime: float = 3.0, extra: dict[str, str] | None = None
) -> dict[str, str]:
    environment = {HARD_DEADLINE_ENV: _stamp(clock.now() + timedelta(seconds=lifetime))}
    if extra:
        environment.update(extra)
    return environment


# --- hold survives a completed bootstrap without exiting -------------------


def test_hold_survives_a_completed_bootstrap_without_exiting(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    fake = FakeActions()

    exit_code = main(
        _argv(ws),
        environ=_environ(clock, lifetime=3.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: fake,
    )

    assert exit_code == 0
    assert fake.calls == list(BootstrapStep)
    # The loop did not return the instant bootstrap went green: it kept
    # ticking, and sleeping, until the shared hard deadline arrived.
    assert clock.seconds == 3.0
    record = json.loads(ws.report_path.read_text(encoding="utf-8"))
    assert record["schema"] == HOLD_SCHEMA
    assert record["state"] == "holding"
    assert record["bootstrap"]["color"] == "green"
    assert record["tick"] == 3


def test_hold_helper_reports_every_tick_before_the_deadline(tmp_path: Path) -> None:
    clock = Clock()
    report_path = tmp_path / "hold.json"

    record = hold(
        report_path=report_path,
        hard_deadline=clock.now() + timedelta(seconds=2),
        state="hold-only",
        bootstrap=None,
        now=clock.now,
        sleeper=clock.sleep,
        interval_seconds=1,
    )

    assert record["tick"] == 2
    assert clock.seconds == 2.0
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk == record


# --- a red step exits non-zero, and the hold is never entered --------------


def test_red_bootstrap_step_exits_nonzero_and_never_holds(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    fake = FakeActions(fail_step=BootstrapStep.TRANSFER)

    exit_code = main(
        _argv(ws),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: fake,
    )

    assert exit_code == 3
    assert BootstrapStep.PREFLIGHT not in fake.calls
    assert not ws.report_path.exists()
    assert clock.seconds == 0.0  # the hold loop never ran to sleep on anything


# --- each named refusal, before anything runs -------------------------------


def test_refuses_a_journal_path_outside_the_mounted_volume(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    ws.journal = tmp_path / "outside" / "journal.json"
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_refuses_a_report_path_outside_the_mounted_volume(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    argv = _argv(ws)
    outside = tmp_path / "outside" / "report.json"
    argv[argv.index(str(ws.report_path))] = str(outside)
    clock = Clock()

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_refuses_a_lockfile_that_is_not_the_checked_out_uv_lock(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    argv = _argv(ws)
    stray = tmp_path / "elsewhere" / "uv.lock"
    argv[argv.index(str(ws.repository / "uv.lock"))] = str(stray)
    clock = Clock()

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_refuses_when_the_volume_fails_a_write_probe(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    # A regular file where the mount should be a directory: the probe's own
    # write attempt fails for real, rather than a stat-only check that a
    # present-but-unwritable mount would pass.
    ws.volume.rmdir()
    ws.volume.write_text("not a directory\n", encoding="utf-8")
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_refuses_without_a_hard_deadline_in_the_environment(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    exit_code = main(_argv(ws), environ={}, actions_factory=_never_called)

    assert exit_code == 2


def test_refuses_a_credential_looking_argv_value(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws, extra=("--transfer-prefix", "my-api-key-123"))

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_hold_only_refuses_any_plan_argument(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = [
        "--volume-mount-path",
        str(ws.volume),
        "--report-path",
        str(ws.report_path),
        "--hold-only",
        "--store-root",
        str(ws.store_root),
    ]

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_hold_only_drills_to_the_deadline_with_no_plan_arguments(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = [
        "--volume-mount-path",
        str(ws.volume),
        "--report-path",
        str(ws.report_path),
        "--hold-only",
        "--interval-seconds",
        "1",
    ]

    exit_code = main(
        argv,
        environ=_environ(clock, lifetime=2.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_never_called,
    )

    assert exit_code == 0
    record = json.loads(ws.report_path.read_text(encoding="utf-8"))
    assert record["state"] == "hold-only"
    assert record["bootstrap"] is None


def test_refuses_missing_required_plan_arguments(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws)
    index = argv.index("--models-config")
    del argv[index : index + 2]

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2


def test_resolve_plan_refusal_names_are_distinct(tmp_path: Path) -> None:
    """The refusal reasons stay findable by name, not just by exit code."""

    ws = _workspace(tmp_path)
    parser = build_parser()

    outside_journal = parser.parse_args(
        [
            arg if arg != str(ws.journal) else str(tmp_path / "outside" / "j.json")
            for arg in _argv(ws)
        ]
    )
    with pytest.raises(PlanRefusal, match="must be inside the mounted volume"):
        resolve_plan(outside_journal)

    bad_lockfile = parser.parse_args(
        [
            arg if arg != str(ws.repository / "uv.lock") else str(tmp_path / "stray" / "uv.lock")
            for arg in _argv(ws)
        ]
    )
    with pytest.raises(PlanRefusal, match="is not the checked-out repository uv.lock"):
        resolve_plan(bad_lockfile)

    hold_only_with_plan_arg = parser.parse_args(
        [
            "--volume-mount-path",
            str(ws.volume),
            "--report-path",
            str(ws.report_path),
            "--hold-only",
            "--store-root",
            str(ws.store_root),
        ]
    )
    with pytest.raises(PlanRefusal, match="--hold-only refuses a plan argument"):
        resolve_plan(hold_only_with_plan_arg)


# --- environment scrubbing ---------------------------------------------------


def test_scrub_environment_keeps_the_allowlist_and_drops_the_rest() -> None:
    environment = {
        "MY_API_KEY": "shhh",
        "SOME_TOKEN": "also-shhh",
        "PLAIN_VAR": "kept regardless",
        HARD_DEADLINE_ENV: "2026-01-01T00:00:00Z",
    }

    scrubbed = scrub_environment(environment, keep=["MY_API_KEY"])

    assert scrubbed == {
        "MY_API_KEY": "shhh",
        "PLAIN_VAR": "kept regardless",
        HARD_DEADLINE_ENV: "2026-01-01T00:00:00Z",
    }


def test_main_scrubs_the_environment_in_place_before_holding(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    environment = _environ(
        clock,
        lifetime=1.0,
        extra={"MY_API_KEY": "shhh", "SOME_SECRET": "also-shhh", "PLAIN_VAR": "kept"},
    )
    argv = _argv(ws, extra=("--keep-env", "MY_API_KEY"))

    exit_code = main(
        argv,
        environ=environment,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: FakeActions(),
    )

    assert exit_code == 0
    assert environment["MY_API_KEY"] == "shhh"
    assert environment["PLAIN_VAR"] == "kept"
    assert "SOME_SECRET" not in environment
    assert HARD_DEADLINE_ENV in environment


# --- --dry-run runs no action -------------------------------------------------


def test_dry_run_runs_no_action(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws, extra=("--dry-run",))

    exit_code = main(
        argv,
        environ=_environ(clock),
        actions_factory=_never_called,
    )

    assert exit_code == 0
    assert not ws.journal.exists()
    assert not ws.report_path.exists()
    plan_record = json.loads(capsys.readouterr().out)
    assert plan_record["dry_run"] is True
    assert Path(plan_record["repository"]) == ws.repository.resolve()


def _never_called(plan: object) -> object:  # pragma: no cover - defensive
    raise AssertionError("actions_factory must not be called for this scenario")
