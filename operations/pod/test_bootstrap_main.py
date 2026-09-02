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
    REFUSAL_SCHEMA,
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
        # Inside the volume / the checked-out repository respectively: a
        # pinned real-roster store and a chair/placement config that resolve
        # outside their required container are refused (resolve_plan).
        store_root=volume / "store",
        models_config=repository / "config" / "models.toml",
        placement_config=repository / "config" / "pod_placement.toml",
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


def test_refuses_a_journal_path_outside_the_mounted_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    ws.journal = tmp_path / "outside" / "journal.json"
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "--journal" in capsys.readouterr().err


def test_refuses_a_report_path_outside_the_mounted_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    argv = _argv(ws)
    outside = tmp_path / "outside" / "report.json"
    argv[argv.index(str(ws.report_path))] = str(outside)
    clock = Clock()

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "--report-path" in capsys.readouterr().err
    assert not outside.exists()  # no report path outside the volume is ever written to


def test_refuses_a_lockfile_that_is_not_the_checked_out_uv_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    argv = _argv(ws)
    stray = tmp_path / "elsewhere" / "uv.lock"
    argv[argv.index(str(ws.repository / "uv.lock"))] = str(stray)
    clock = Clock()

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "is not the checked-out repository uv.lock" in capsys.readouterr().err


def test_refuses_when_the_volume_fails_a_write_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    # A regular file where the mount should be a directory: the probe's own
    # write attempt fails for real, rather than a stat-only check that a
    # present-but-unwritable mount would pass.
    ws.volume.rmdir()
    ws.volume.write_text("not a directory\n", encoding="utf-8")
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "volume write probe failed" in capsys.readouterr().err


def test_refuses_when_the_volume_mount_path_does_not_exist(tmp_path: Path) -> None:
    """The probe must not fail for a mount by *creating* the mount point.

    ``atomic_write`` creates its target's parent directories; routing the
    write probe through it would let an unmounted volume pass by creating the
    very mount point the probe exists to require, so every later evidence
    write would land on ephemeral container disk instead of the volume.
    """

    ws = _workspace(tmp_path)
    ws.volume.rmdir()  # simulate an unmounted volume: nothing was ever created here
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert not ws.volume.exists()  # the probe must never create the mount point it checks


def test_refuses_without_a_hard_deadline_in_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)

    exit_code = main(_argv(ws), environ={}, actions_factory=_never_called)

    assert exit_code == 2
    assert HARD_DEADLINE_ENV in capsys.readouterr().err


def test_refuses_a_credential_looking_argv_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws, extra=("--transfer-prefix", "my-api-key-123"))

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "looks like a credential" in capsys.readouterr().err


def test_refuses_an_argv_value_shaped_like_a_real_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value's own opaque shape is refused even when its flag name is not."""

    ws = _workspace(tmp_path)
    clock = Clock()
    # An opaque, separator-free, mixed alphanumeric run -- not a recognizable
    # provider token format, just the general shape one would have.
    argv = _argv(ws, extra=("--transfer-prefix", "zZ9mQ2xR7vT4kL8nP1wA6cE3sD5fG0h"))

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "looks like a credential" in capsys.readouterr().err


def test_credential_argv_refusal_does_not_catch_every_secret_shape() -> None:
    """Documents a known, accepted gap rather than letting it drift unnoticed.

    ``refuse_credential_looking_argv`` refuses a name-shaped marker word and an
    opaque, separator-free 20+ character run. A short or separator-bearing
    value with neither a marker word nor that shape is not caught -- this pins
    the boundary so a future change is a deliberate one, not a silent one.
    """

    from .bootstrap_main import refuse_credential_looking_argv

    refuse_credential_looking_argv(["--transfer-prefix", "a-plain-run-id"])


def test_hold_only_refuses_any_plan_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    assert "--hold-only refuses a plan argument" in capsys.readouterr().err


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
    # The drill actually held to the deadline, not just wrote one record and
    # returned: without this the control mutation on the hold loop would not
    # be caught here the way it is by its bootstrap-mode sibling above.
    assert clock.seconds == 2.0
    record = json.loads(ws.report_path.read_text(encoding="utf-8"))
    assert record["state"] == "hold-only"
    assert record["bootstrap"] is None
    assert record["tick"] == 2


def test_hold_only_refuses_a_non_finite_interval_with_a_durable_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = [
        "--volume-mount-path",
        str(ws.volume),
        "--report-path",
        str(ws.report_path),
        "--hold-only",
        "--interval-seconds",
        "nan",
    ]

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "--interval-seconds must be a positive finite number" in capsys.readouterr().err
    record = json.loads(ws.report_path.read_text(encoding="utf-8"))
    assert "positive finite number" in record["reason"]


def test_hold_only_refuses_a_zero_interval_with_a_durable_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = [
        "--volume-mount-path",
        str(ws.volume),
        "--report-path",
        str(ws.report_path),
        "--hold-only",
        "--interval-seconds",
        "0",
    ]

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "--interval-seconds must be a positive finite number" in capsys.readouterr().err
    record = json.loads(ws.report_path.read_text(encoding="utf-8"))
    assert record["schema"] == REFUSAL_SCHEMA
    assert "positive finite number" in record["reason"]


def test_refuses_missing_required_plan_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws)
    index = argv.index("--models-config")
    del argv[index : index + 2]

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "missing required plan argument(s)" in err
    assert "--models-config" in err


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


# --- the launch token binds report/journal evidence to this launch ----------


def test_refuses_a_report_path_missing_the_launch_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mirrors ``models._required_timer_arguments``'s guard on the timer side.

    A volume is retained across pods; an unbound report path would let a
    second launch's bootstrap/close evidence silently replace the first's
    (GOVERNANCE 4).
    """

    ws = _workspace(tmp_path)
    clock = Clock()
    environment = _environ(clock, extra={"VERBATUS_LAUNCH_TOKEN": "launch-abc123"})

    exit_code = main(_argv(ws), environ=environment, actions_factory=_never_called)

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--report-path" in err
    assert "this launch's token" in err


def test_holds_when_the_report_path_carries_the_launch_token(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    ws.report_path = ws.volume / "bootstrap-report-launch-abc123.json"
    ws.journal = ws.volume / "bootstrap-journal-launch-abc123.json"
    clock = Clock()
    environment = _environ(clock, extra={"VERBATUS_LAUNCH_TOKEN": "launch-abc123"})

    exit_code = main(
        _argv(ws),
        environ=environment,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: FakeActions(),
    )

    assert exit_code == 0


# --- store-root and chair config stay inside their required container -------


def test_refuses_a_store_root_outside_the_mounted_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    ws.store_root = tmp_path / "outside-store"
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert "--store-root" in capsys.readouterr().err


def test_refuses_a_models_config_outside_the_checked_out_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _workspace(tmp_path)
    ws.models_config = tmp_path / "outside-config" / "models.toml"
    clock = Clock()

    exit_code = main(_argv(ws), environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--models-config" in err
    assert "checked-out repository" in err


# --- the chair cache is built lazily, only when CHAIR_CACHE actually runs ---


def test_build_actions_does_not_read_models_config_before_chair_cache_runs(
    tmp_path: Path,
) -> None:
    """``build_actions`` runs before REPOSITORY checks out the pinned commit.

    Building the chair cache eagerly would read whatever ``models.toml``
    happened to be on disk at container start, not the commit the journal
    names -- the receipt would attest a provenance nothing measured
    (GOVERNANCE 6). ``--models-config`` is deliberately left absent here: were
    ``build_actions`` still eager, constructing the real actions would already
    raise trying to read it.
    """

    from .bootstrap_main import Plan, build_actions

    ws = _workspace(tmp_path)
    assert not ws.models_config.exists()
    plan = Plan(
        volume_mount_path=ws.volume,
        report_path=ws.report_path,
        interval_seconds=1.0,
        keep_env=(),
        dry_run=False,
        hold_only=False,
        repository=ws.repository,
        repository_commit="a" * 40,
        lockfile=ws.repository / "uv.lock",
        journal=ws.journal,
        store_root=ws.store_root,
        models_config=ws.models_config,
        placement_config=ws.placement_config,
        cache_root=ws.volume / "chair-cache",
        fixture=ws.repository / "proof" / "fixtures" / "synthetic-two-page-v0" / "page-1.png",
        submission_manifest=ws.volume / "submission" / "manifest.json",
        transfer_source_root=ws.volume,
    )

    from common.chairs.errors import ChairRefusal

    actions = build_actions(plan)  # must not touch ws.models_config at all

    assert not ws.models_config.exists()
    with pytest.raises(ChairRefusal):
        actions.verify_chair_cache()  # only *now* does it try to read the config


def test_build_actions_refuses_a_plan_with_no_submission_manifest() -> None:
    """A non-hold-only ``Plan`` with ``submission_manifest=None`` must be a named
    refusal at build time, not a bare ``AttributeError`` inside the TRANSFER
    step (or, under ``python -O``, a stripped ``assert`` that lets it through
    silently). ``resolve_plan`` always fills this in for a real launch; this
    drives the invariant guard directly the way a future caller of
    ``build_actions`` might trip it.
    """

    from .bootstrap_main import Plan, build_actions

    plan = Plan(
        volume_mount_path=Path("/volume"),
        report_path=Path("/volume/report.json"),
        interval_seconds=1.0,
        keep_env=(),
        dry_run=False,
        hold_only=False,
        repository=Path("/repo"),
        repository_commit="a" * 40,
        lockfile=Path("/repo/uv.lock"),
        journal=Path("/volume/journal.json"),
        store_root=Path("/volume/store"),
        models_config=Path("/repo/models.toml"),
        placement_config=Path("/repo/placement.toml"),
        cache_root=Path("/volume/chair-cache"),
        fixture=Path("/repo/proof/fixtures/synthetic-two-page-v0/page-1.png"),
        submission_manifest=None,
        transfer_source_root=Path("/volume"),
    )

    with pytest.raises(PlanRefusal, match="no submission manifest"):
        build_actions(plan)


# --- a refusal leaves a durable, readable reason on the volume --------------


def test_a_refusal_after_report_path_validation_writes_a_durable_reason(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws)
    index = argv.index("--models-config")
    del argv[index : index + 2]

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    record = json.loads(ws.report_path.read_text(encoding="utf-8"))
    assert record["schema"] == "pod-bootstrap-refusal.v1"
    assert "missing required plan argument(s)" in record["reason"]


def test_a_refusal_that_precedes_report_path_validation_writes_nothing(
    tmp_path: Path,
) -> None:
    """The credential-argv scan runs before argv is even parsed: no report path
    is known yet, so this refusal is necessarily stderr-only residue."""

    ws = _workspace(tmp_path)
    clock = Clock()
    argv = _argv(ws, extra=("--transfer-prefix", "my-api-key-123"))

    exit_code = main(argv, environ=_environ(clock), actions_factory=_never_called)

    assert exit_code == 2
    assert not ws.report_path.exists()


class _NotCalled(BaseException):
    """Raised by ``_never_called`` -- deliberately not ``Exception``.

    ``main``'s ``actions_factory`` call is wrapped in a narrow
    ``except Exception`` that turns any build failure into exit 2. An ordinary
    ``Exception`` raised here would be silently absorbed by that handler if
    the refusal actually under test were deleted, so a mutated guard would
    still return 2 and the test would stay green for the wrong reason. A
    ``BaseException`` subclass cannot be caught there, so a refusal test that
    reaches this function fails loudly instead of passing by accident.
    """


def _never_called(plan: object) -> object:  # pragma: no cover - defensive
    raise _NotCalled("actions_factory must not be called for this scenario")
