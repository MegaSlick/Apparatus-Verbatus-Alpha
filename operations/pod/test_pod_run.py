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

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from argparse import Namespace
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.runtree.store import RunTree
from common.sealed_config import read_sealed_toml
from common.stage import real_run_policy_digest
from operations.operator import cli as operator_cli
from operations.operator.errors import ErrorCode, OperatorError
from operations.operator.records import SCHEMA as RECEIPT_SCHEMA
from operations.operator.volume_s3 import VolumeSpec
from pipeline.orchestrator import run as orchestrator
from pipeline.orchestrator.run import STOP_RECORD_SCHEMA

from . import bootstrap_main, pod_run
from . import launch as launch_module
from .bootstrap import BootstrapStep
from .models import run_report_paths
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
    "schema": "serving-config-inputs.v2",
    "serving_recipes_sha256": "1" * 64,
    "pod_placement_sha256": "2" * 64,
}


@dataclass
class PreflightedActions(FakeActions):
    """Green everywhere, with the receipt a real PREFLIGHT leaves: a measured tier."""

    def run_preflight(self) -> dict[str, object]:
        return self._step(
            BootstrapStep.PREFLIGHT,
            {
                "color": "green",
                "placement_tier": TIER,
                "serving_config_inputs": SERVING_INPUTS,
                "smoke_receipts": [
                    {"chair": role, "valid": True}
                    for role in (
                        "attestator_1",
                        "attestator_2",
                        "attestator_3",
                        "perlector",
                        "reconstructor",
                    )
                ],
                # The record detector runs in-process: a verified cache, no smoke read.
                # Surya runs as a subprocess: a verified cache and its own golden-page run.
                "placements": [
                    {"chair": "secondary_proposer", "state": "in-process"},
                    {"chair": "designator_surya", "state": "subprocess"},
                ],
                "cache_receipts": [
                    {"chair": "secondary_proposer"},
                    {"chair": "designator_surya"},
                ],
                "subprocess_receipts": [{"chair": "designator_surya"}],
            },
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
    # never came home.
    write_transcript: bool = True
    transcript_text: bytes = b"orchestrator output\n"
    journal_run_id: str | None = "first-real-run"
    journal_entries: int = 1
    transcript_failure: str | None = None
    dropped_bytes: int = 0
    tick_liveness: bool = True
    # The stop record the orchestrator leaves at its `--stop-record`, as a real
    # one does on every return: a bool says whether this invocation reached its
    # export, text is written as it stands, and None leaves none.
    stop: bool | str | None = False
    # The systemic alarm line the stop record names, or None.
    systemic: str | None = None
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
            transcript.write_bytes(self.transcript_text)
        if self.journal_run_id is not None:
            journal = transcript.with_name(
                transcript.name.replace("-transcript.log", "-timings.json")
            )
            journal.write_text(
                "".join(
                    json.dumps(
                        {
                            "schema": "stage-timing-journal.v4",
                            "run_id": self.journal_run_id,
                            "run_root": argv[argv.index("--run-root") + 1],
                        }
                    )
                    + "\n"
                    for _ in range(self.journal_entries)
                ),
                encoding="utf-8",
            )
        if self.stop is not None:
            Path(argv[argv.index("--stop-record") + 1]).write_text(
                self.stop
                if isinstance(self.stop, str)
                else json.dumps(
                    {
                        "schema": STOP_RECORD_SCHEMA,
                        "run_id": argv[argv.index("--run-id") + 1],
                        "exit_code": self.returncode,
                        "exported": self.stop,
                        "systemic": self.systemic,
                    }
                ),
                encoding="utf-8",
            )
        if self.tick_liveness:
            for _ in range(self.ticks):
                liveness(self.pid, True)
            liveness(self.pid, False)
        return pod_run.RunnerResult(
            self.returncode,
            transcript_failure=self.transcript_failure,
            transcript_dropped_bytes=self.dropped_bytes,
        )


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
    ws.models_config.parent.mkdir(parents=True, exist_ok=True)
    ws.models_config.write_bytes((ROOT / "config" / "models.toml").read_bytes())
    (ws.repository / "config" / "models-real.toml").write_bytes(
        (ROOT / "config" / "models-real.toml").read_bytes()
    )
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
    if not ws.models_config.exists():
        ws.models_config.parent.mkdir(parents=True, exist_ok=True)
        ws.models_config.write_bytes((ROOT / "config" / "models.toml").read_bytes())
    reconstruction = ws.repository / "config" / "reconstruction.toml"
    if not reconstruction.exists():
        reconstruction.parent.mkdir(parents=True, exist_ok=True)
        reconstruction.write_bytes((ROOT / "config" / "reconstruction.toml").read_bytes())
    # Existing hand-route tests exercise the explicitly volume-hosted compatibility path.
    if "--no-hold" in extra and "--run-root" not in extra:
        extra = ("--run-root", str(ws.volume / "runs"), *extra)
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


@pytest.fixture(autouse=True)
def _no_container_pod_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The test machine's own first process is not a pod; tests write one when they need it."""

    monkeypatch.setattr(pod_run, "PID1_ENVIRON", tmp_path / "no-such-proc" / "environ")


# --- the green run: bootstrap, orchestrate over the volume, hold --------------


def test_a_complete_run_exits_zero_after_bootstrap_orchestrator_and_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    actions = PreflightedActions()
    monkeypatch.setattr(
        actions,
        "configure_cuda_compat",
        lambda: actions._step(
            BootstrapStep.CUDA_COMPAT,
            {
                "driver": "570.195.03",
                "gpus": ["NVIDIA RTX A6000"],
                "compat_path": "/usr/local/cuda-13.0/compat",
                "action": "installed",
            },
        ),
    )
    runner = RecordedRunner(returncode=0)
    real_recipes = ws.repository / "config" / "serving_recipes_real.toml"
    real_roster = ws.repository / "config" / "models-real.toml"
    argv = _run_argv(ws, bootstrap_extra=("--serving-recipes-config", str(real_recipes)))
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
    assert command[1:4] == [
        "-I",
        "-u",
        str(ws.repository / "pipeline" / "orchestrator" / "run.py"),
    ]
    stop = Path(command[command.index("--stop-record") + 1])
    assert stop.name == "stop.json" and stop.parent.name.startswith("pod-run-stop-")
    assert not stop.parent.exists()
    assert command[4:] == [
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
        "--stage-timing-journal",
        str(ws.volume / "pod-run-report-timings.json"),
        # A private path made for this invocation, removed once read.
        "--stop-record",
        str(stop),
        # The commit the bootstrap checked out and verified, not one the
        # orchestrator re-derives: REPOSITORY already read the checkout back
        # and refused a tip that was not this pin.
        "--repository-commit",
        "a" * 40,
        "--cache-root",
        str(Path("/var/tmp/verbatus-chair-cache").resolve()),
        "--store-root",
        str(ws.store_root),
        "--placement-tier",
        TIER,
    ]
    # The scrubbed environment is what the orchestrator sees: no transfer key.
    assert "RUNPOD_S3_ACCESS_KEY" not in env
    assert env["LD_LIBRARY_PATH"].split(":")[0] == "/usr/local/cuda-13.0/compat"
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
    assert "pod guard deletes an idle pod" in report["hold_detail"]
    assert clock.seconds == 4.0
    hold = _report(ws, "pod-run-report-hold.json")
    assert hold["state"] == "holding-after-complete"
    assert hold["tick"] == 4


def test_mechanics_qualification_reaches_orchestrator_and_report(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    code = main(
        _run_argv(ws, extra=("--mechanics-qualification",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert code == EXIT_COMPLETE
    assert "--mechanics-qualification" in runner.calls[0][0]
    report = _report(ws)
    assert report["plan"]["mechanics_qualification"] is True
    assert "--mechanics-qualification" in report["orchestrator_argv"]


def test_perlector_protocol_config_reaches_orchestrator_and_report(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    protocol = _alternative_protocol(ws.repository / "config" / "perlector_protocol_edge.toml")
    clock = Clock()
    runner = RecordedRunner()

    code = main(
        _run_argv(ws, extra=("--perlector-protocol-config", str(protocol))),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert code == EXIT_COMPLETE
    command = runner.calls[0][0]
    flag = command.index("--perlector-protocol-config")
    assert command[flag + 1] == str(protocol.resolve())
    assert _report(ws)["plan"]["perlector_protocol_config"] == str(protocol.resolve())


def test_without_a_protocol_flag_the_orchestrator_keeps_its_default(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert "--perlector-protocol-config" not in runner.calls[0][0]
    assert _report(ws)["plan"]["perlector_protocol_config"] is None


@pytest.mark.parametrize("where", ["outside", "missing"])
def test_a_protocol_outside_the_repository_or_missing_is_refused_before_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    ws = _prepared(tmp_path)
    if where == "outside":
        protocol = tmp_path / "protocol.toml"
        protocol.write_text("", encoding="utf-8")
    else:
        protocol = ws.repository / "config" / "absent.toml"
    exit_code, runner = _refused(
        ws, _run_argv(ws, extra=("--perlector-protocol-config", str(protocol)))
    )
    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert "--perlector-protocol-config" in capsys.readouterr().err


def _first_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pod_id: str) -> None:
    environ = tmp_path / "pid1-environ"
    environ.write_bytes(f"PATH=/usr/bin\0{pod_run.POD_ID_ENVIRONMENT}={pod_id}\0HOME=/\0".encode())
    monkeypatch.setattr(pod_run, "PID1_ENVIRON", environ)


def _armed(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock, value: int
) -> Path:
    """This pod as --no-hold needs it: its id on its first process, and a live guard."""

    _first_process(tmp_path, monkeypatch, "pod123")
    return _guard_deadline(ws, value, heartbeat=clock.now().timestamp() - 30)


def test_the_backup_list_names_only_trees_that_exist_and_is_cleared(tmp_path: Path) -> None:
    local, volume = tmp_path / "local" / "run", tmp_path / "volume" / "run"
    listed = tmp_path / "backup-pod123"
    backup = pod_run.BackupList(listed, (local, volume, local))
    backup.refresh()
    assert lines(listed) == []
    volume.mkdir(parents=True)
    backup.refresh()
    assert lines(listed) == [str(volume)]
    local.mkdir(parents=True)
    backup.refresh()
    assert lines(listed) == [str(local), str(volume)]
    backup.clear()
    assert not listed.exists()
    pod_run.BackupList(None, (local,)).refresh()


def test_the_bootstrap_and_the_final_sync_tell_the_guard_they_are_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither has a liveness tick; a thread writes the progress line through both, and
    the line is gone once the run ends."""
    ws = _prepared(tmp_path)
    local_root = tmp_path / "local-runs"
    monkeypatch.setattr(pod_run, "DEFAULT_LOCAL_RUNS_DIRECTORY", local_root)
    _policy(ws, roots=[str(ws.volume), str(local_root)])
    clock = Clock()
    deadline = _armed(ws, tmp_path, monkeypatch, clock, int(clock.now().timestamp()) + 3600)
    progress = deadline.with_name("progress-pod123")
    seen: dict[str, list[str]] = {}

    class Watched(PreflightedActions):
        def run_preflight(self) -> dict[str, object]:
            # The thread writes once a second here (--interval-seconds 1).
            give_up = time.monotonic() + 10
            while "preflight" not in progress.read_text(encoding="ascii"):
                assert time.monotonic() < give_up, progress.read_text(encoding="ascii")
                time.sleep(0.05)
            seen["bootstrap"] = progress.read_text(encoding="ascii").split(" ", 4)
            return super().run_preflight()

    real_sync = pod_run.RunTreeSync.sync

    def watched_sync(self):  # type: ignore[no-untyped-def]
        seen["sync"] = progress.read_text(encoding="ascii").split(" ", 4)
        return real_sync(self)

    monkeypatch.setattr(pod_run.RunTreeSync, "sync", watched_sync)
    run = local_root / "first-real-run"
    recorded = RecordedRunner(returncode=0)

    def runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        run.mkdir(parents=True, exist_ok=True)
        (run / "record.json").write_bytes(b"new")
        return recorded(*args, **kwargs)

    argv = _run_argv(ws, extra=("--no-hold",))
    index = argv.index("--run-root")
    del argv[index : index + 2]
    code = main(
        argv,
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: Watched(),
        runner=runner,
    )

    assert code == EXIT_COMPLETE
    assert seen["bootstrap"][2:4] == ["bootstrapping", "bootstrap"]
    assert seen["bootstrap"][4].startswith("preflight for ")
    assert seen["sync"][2:4] == ["ok", "final-sync"]
    assert not progress.exists()


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _guard_deadline(ws: Workspace, value: int, *, heartbeat: float | None = None) -> Path:
    guard = ws.volume / pod_run.POD_GUARD_DIRECTORY
    guard.mkdir()
    path = guard / "deadline-pod123"
    path.write_text(f"{value}\n", encoding="ascii")
    if heartbeat is not None:
        beat = guard / "heartbeat-pod123"
        beat.touch()
        os.utime(beat, (heartbeat, heartbeat))
    return path


@pytest.mark.parametrize("fail_final_sync", (False, True))
def test_hand_run_uses_local_disk_and_requires_the_final_volume_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_final_sync: bool
) -> None:
    ws = _prepared(tmp_path)
    local_root = tmp_path / "local-runs"
    monkeypatch.setattr(pod_run, "DEFAULT_LOCAL_RUNS_DIRECTORY", local_root)
    _policy(ws, roots=[str(ws.volume), str(local_root)])
    clock = Clock()
    deadline = _armed(ws, tmp_path, monkeypatch, clock, int(clock.now().timestamp()) + 3600)
    argv = _run_argv(ws, extra=("--no-hold",))
    index = argv.index("--run-root")
    del argv[index : index + 2]
    run = local_root / "first-real-run"
    if fail_final_sync:
        stored = ws.volume / "runs" / "first-real-run"
        stored.mkdir(parents=True)
        (stored / "record.json").write_bytes(b"older")
    recorded = RecordedRunner(returncode=0, ticks=1)
    backup = deadline.with_name("backup-pod123")
    listed_at_start: list[str] = []

    def runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        listed_at_start.extend(lines(backup))
        run.mkdir(parents=True, exist_ok=True)
        (run / "record.json").write_bytes(b"new")
        return recorded(*args, **kwargs)

    code = main(
        argv,
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    [(command, _cwd, _env)] = recorded.calls
    assert command[command.index("--run-root") + 1] == str(local_root)
    assert command[command.index("--stage-sync-root") + 1] == str(ws.volume / "runs")
    report = _report(ws)
    if fail_final_sync:
        assert code == EXIT_FAILED
        assert report["state"] == "failed"
        assert "final volume sync failed" in report["detail"]
        assert (ws.volume / "runs" / "first-real-run" / "record.json").read_bytes() == b"older"
        # The volume's run was copied to local disk first; the copy's ledger stays
        # beside the local tree, and the volume's tree gains nothing but evidence.
        assert not list(stored.rglob(f"{pod_run.SYNC_PREFIX}*"))
        assert (local_root / f"{pod_run.SYNC_PREFIX}hydrate-first-real-run.jsonl").is_file()
        assert deadline.read_text(encoding="ascii") != f"{int(clock.now().timestamp())}\n"
        assert not deadline.with_name("released-pod123").exists()
        # The guard's hour-idle backup keeps both trees, from the start (the volume's run
        # was copied to local disk before the bootstrap) to past the failed sync.
        assert listed_at_start == lines(backup) == [str(run), str(stored)]
    else:
        assert code == EXIT_COMPLETE
        assert report["state"] == "complete"
        assert (ws.volume / "runs" / "first-real-run" / "record.json").read_bytes() == b"new"
        assert deadline.with_name("released-pod123").exists()
        assert listed_at_start == [], "only trees that exist are listed"
        assert not backup.exists(), "a clean finish leaves nothing to back up"


@pytest.mark.parametrize(
    ("first_process", "armed", "touched"),
    [("pod123", True, True), ("pod123", False, False), (None, True, False)],
)
def test_a_running_orchestrator_touches_its_own_pod_s_guard_keepalive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_process: str | None,
    armed: bool,
    touched: bool,
) -> None:
    """The guard's resource counters are a backstop: a run in progress is work.

    Only the first process's pod id is trusted (a shell's could keep another pod on the
    shared volume alive), and nothing is created where no guard armed its directory.
    """
    ws = _prepared(tmp_path)
    clock = Clock()
    if first_process is None:
        monkeypatch.setattr(pod_run, "PID1_ENVIRON", tmp_path / "no-such-proc" / "environ")
    else:
        _first_process(tmp_path, monkeypatch, first_process)
    guard = ws.volume / pod_run.POD_GUARD_DIRECTORY
    if armed:
        guard.mkdir()
    seen: list[bool] = []
    keepalive = guard / "keepalive-pod123"

    class Watching(RecordedRunner):
        def __call__(self, argv, *, cwd, env, transcript, liveness, interval_seconds):  # type: ignore[no-untyped-def]
            def watched(pid: int, alive: bool) -> None:
                liveness(pid, alive)
                seen.append(keepalive.exists())

            return super().__call__(
                argv,
                cwd=cwd,
                env=env,
                transcript=transcript,
                liveness=watched,
                interval_seconds=interval_seconds,
            )

    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=Watching(ticks=2),
    )

    assert code == EXIT_COMPLETE
    assert seen and seen[0] is touched, "touched on the first live tick, while the child runs"
    assert guard.is_dir() is armed


@pytest.mark.parametrize(
    ("orchestrator_exit", "expected_exit"),
    [
        (0, EXIT_COMPLETE),
        (orchestrator.EXIT_HELD, EXIT_HELD),
        (orchestrator.EXIT_RUN_HALTED, EXIT_HALTED),
        (pod_run.ORCHESTRATOR_FATAL, EXIT_FAILED),
        (None, EXIT_FAILED),
    ],
)
def test_no_hold_returns_at_once_and_moves_the_guard_deadline_to_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator_exit: int, expected_exit: int
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    later = int(clock.now().timestamp()) + 3600
    deadline = _armed(ws, tmp_path, monkeypatch, clock, later)

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(
            returncode=orchestrator_exit or 0, raise_oserror=orchestrator_exit is None
        ),
    )

    assert code == expected_exit
    # No paid idle time, and no hold record.
    assert clock.seconds == 0.0
    assert not (ws.volume / "pod-run-report-hold.json").exists()
    now = int(clock.now().timestamp())
    assert deadline.read_text(encoding="ascii") == f"{now}\n"
    report = _report(ws)
    assert report["plan"]["no_hold"] is True
    assert report["held_to_hard_deadline"] is False
    assert "--no-hold" in report["hold_detail"]
    assert report["guard_release"] == {
        "path": str(deadline),
        "released": True,
        "deadline": now,
        "guard_heartbeat_age_seconds": 30,
        "guard_alive": True,
    }
    # Written before the deadline moved: the guard quotes it in its delete notice.
    notice = deadline.with_name("released-pod123").read_text(encoding="ascii")
    assert notice == f"run first-real-run ended {report['state']}\n"


def test_no_hold_is_refused_under_a_launch_token(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    ws.report_path = ws.volume / "bootstrap-report-launch-abc123.json"
    ws.journal = ws.volume / "bootstrap-journal-launch-abc123.json"
    clock = Clock()
    runner = RecordedRunner()

    code = main(
        _run_argv(
            ws, extra=("--no-hold",), report_path=ws.volume / "pod-run-report-launch-abc123.json"
        ),
        environ=_environ(clock, lifetime=1.0, extra={"VERBATUS_LAUNCH_TOKEN": "launch-abc123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_never_called,
        runner=runner,
    )

    assert code == EXIT_REFUSED
    assert runner.calls == []
    assert "--no-hold" in _report(ws, "pod-run-report-launch-abc123.json")["reason"]


def test_no_hold_never_moves_an_earlier_guard_deadline_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    earlier = int(clock.now().timestamp()) - 60
    deadline = _armed(ws, tmp_path, monkeypatch, clock, earlier)

    main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(),
    )

    assert deadline.read_text(encoding="ascii") == f"{earlier}\n"
    assert _report(ws)["guard_release"]["released"] is True


@pytest.mark.parametrize("case", ["no-deadline-file", "garbage-deadline", "unwritable"])
def test_no_hold_without_an_armed_guard_still_returns_and_says_so(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    extra = {pod_run.POD_ID_ENVIRONMENT: "pod123"}
    later = int(clock.now().timestamp()) + 3600
    deadline_path = _armed(ws, tmp_path, monkeypatch, clock, later)
    runner = RecordedRunner()
    if case == "no-deadline-file":
        # No deadline, and the guard dies during the run: nothing would read a new one.
        deadline_path.unlink()
        inner = runner
        stale = clock.now().timestamp() - pod_run.GUARD_HEARTBEAT_STALE_SECONDS - 1

        def guard_dies_mid_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            os.utime(deadline_path.with_name("heartbeat-pod123"), (stale, stale))
            return inner(*args, **kwargs)

        runner = guard_dies_mid_run  # type: ignore[assignment]
    elif case == "garbage-deadline":
        deadline_path.write_text("abc\n", encoding="ascii")
    elif case == "unwritable":
        # Root ignores a read-only directory, so the failed write is made directly.
        write = pod_run.atomic_write

        def refuse_deadline(path: Path, data: bytes) -> None:
            if Path(path).name.startswith("deadline-"):
                raise OSError("read-only file system")
            write(path, data)

        monkeypatch.setattr(pod_run, "atomic_write", refuse_deadline)

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0, extra=extra),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert code == EXIT_COMPLETE
    assert clock.seconds == 0.0
    release = _report(ws)["guard_release"]
    assert release["released"] is False
    assert release["detail"]
    deadline = ws.volume / pod_run.POD_GUARD_DIRECTORY / "deadline-pod123"
    if case == "garbage-deadline":
        assert deadline.read_text(encoding="ascii") == "abc\n"
    elif case == "unwritable":
        assert release["detail"].startswith("deadline write failed")
    else:
        assert not deadline.exists()


def test_no_hold_writes_the_release_deadline_for_a_guard_started_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the budget off a pod can start with no deadline; its live guard honours one
    written later, so the release writes it, and the run needs no hard deadline either."""
    ws = _prepared(tmp_path)
    clock = Clock()
    deadline = _armed(ws, tmp_path, monkeypatch, clock, 0)
    deadline.unlink()
    environment = {
        **_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        bootstrap_main.HARD_DEADLINE_ENV: bootstrap_main.NO_HARD_DEADLINE,
        "VERBATUS_POD_BUDGET": "off",
    }

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=environment,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(),
    )

    assert code == EXIT_COMPLETE
    now = int(clock.now().timestamp())
    assert deadline.read_text(encoding="ascii") == f"{now}\n"
    report = _report(ws)
    assert report["hard_deadline"] is None
    assert report["guard_release"]["released"] is True
    assert report["guard_release"]["guard_alive"] is True


@pytest.mark.parametrize("sealed", [True, False], ids=["sealed-off", "checkout-off"])
def test_a_budget_switched_off_is_reported_as_off_not_unknown(tmp_path: Path, sealed: bool) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "spend.toml").write_bytes((ROOT / "config" / "spend.toml").read_bytes())
    environment = {name: None for name in pod_run.POD_BUDGET_ENVIRONMENT.values()}
    environment["VERBATUS_POD_BUDGET"] = "off" if sealed else None
    plan = SimpleNamespace(repository=tmp_path)

    budget, problem, source = pod_run._pod_budget(plan, environment)  # type: ignore[arg-type]

    assert (budget, problem) == (None, "budget off (lead's choice)")
    assert source.startswith("sealed into the pod") is sealed


def test_a_run_that_may_hold_is_refused_when_there_is_no_hard_deadline(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()
    environment = {
        **_environ(clock, lifetime=4.0),
        bootstrap_main.HARD_DEADLINE_ENV: bootstrap_main.NO_HARD_DEADLINE,
    }

    code = main(
        _run_argv(ws),
        environ=environment,
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_never_called,
        runner=runner,
    )

    assert code == EXIT_REFUSED
    assert runner.calls == []
    assert "--no-hold" in _report(ws)["reason"]


def test_no_hold_leaves_the_guard_alone_after_a_red_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    later = int(clock.now().timestamp()) + 3600
    deadline = _armed(ws, tmp_path, monkeypatch, clock, later)
    red = FakeActions(fail_step=BootstrapStep.PREFLIGHT)

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: red,
        runner=RecordedRunner(),
    )

    assert code == EXIT_BOOTSTRAP_RED
    assert deadline.read_text(encoding="ascii") == f"{later}\n"


def test_no_hold_says_when_the_guard_heartbeat_went_stale_during_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    later = int(clock.now().timestamp()) + 3600
    deadline = _armed(ws, tmp_path, monkeypatch, clock, later)
    stale = clock.now().timestamp() - pod_run.GUARD_HEARTBEAT_STALE_SECONDS - 1
    inner = RecordedRunner()

    def guard_dies_mid_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        os.utime(deadline.with_name("heartbeat-pod123"), (stale, stale))
        return inner(*args, **kwargs)

    main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=guard_dies_mid_run,
    )

    release = _report(ws)["guard_release"]
    # The deadline moved, but nothing is known to be watching it.
    assert release["released"] is True
    assert release["guard_alive"] is False
    assert "delete the pod by hand" in release["detail"]


def test_no_hold_reads_the_pod_id_from_the_container_s_first_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SSH shell need not inherit the provider's pod id; the container's first process has it."""

    ws = _prepared(tmp_path)
    clock = Clock()
    deadline = _armed(ws, tmp_path, monkeypatch, clock, int(clock.now().timestamp()) + 3600)

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(),
    )

    assert code == EXIT_COMPLETE
    assert deadline.read_text(encoding="ascii") == f"{int(clock.now().timestamp())}\n"
    assert _report(ws)["guard_release"]["released"] is True


def test_no_hold_refuses_a_shell_pod_id_that_is_not_the_container_s_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pod's deadline is on the shared volume; a wrong id would end another pod."""

    ws = _prepared(tmp_path)
    _first_process(tmp_path, monkeypatch, "pod123")
    clock = Clock()
    later = int(clock.now().timestamp()) + 3600
    deadline = _guard_deadline(ws, later)
    actions = PreflightedActions()
    runner = RecordedRunner()

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "otherpod"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=runner,
    )

    assert code == EXIT_REFUSED
    assert actions.calls == []
    assert runner.calls == []
    assert "'otherpod'" in _report(ws)["reason"]
    assert deadline.read_text(encoding="ascii") == f"{later}\n"


@pytest.mark.parametrize(
    "case",
    [
        "shell-id-only",
        "no-pod-id",
        "bad-first-process-id",
        "no-guard-directory",
        "missing-heartbeat",
        "stale-heartbeat",
    ],
)
def test_no_hold_without_this_pod_s_live_guard_is_refused_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A release must reach this pod's own guard; a shell's id could name another live pod."""

    ws = _prepared(tmp_path)
    clock = Clock()
    later = int(clock.now().timestamp()) + 3600
    extra: dict[str, str] = {}
    if case == "shell-id-only":
        # /proc/1/environ unreadable; a hand-exported id names a pod whose guard is live.
        _guard_deadline(ws, later, heartbeat=clock.now().timestamp() - 30)
        extra = {pod_run.POD_ID_ENVIRONMENT: "pod123"}
    elif case == "bad-first-process-id":
        _first_process(tmp_path, monkeypatch, "pod١٢٣")
    elif case != "no-pod-id":
        _first_process(tmp_path, monkeypatch, "pod123")
        if case == "missing-heartbeat":
            _guard_deadline(ws, later)
        elif case == "stale-heartbeat":
            stale = clock.now().timestamp() - pod_run.GUARD_HEARTBEAT_STALE_SECONDS - 1
            _guard_deadline(ws, later, heartbeat=stale)
    actions = PreflightedActions()
    runner = RecordedRunner()

    code = main(
        _run_argv(ws, extra=("--no-hold",)),
        environ=_environ(clock, lifetime=4.0, extra=extra),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=runner,
    )

    assert code == EXIT_REFUSED
    assert actions.calls == []
    assert runner.calls == []
    reason = _report(ws)["reason"]
    assert "--no-hold" in reason
    if case in ("shell-id-only", "no-pod-id"):
        assert "Run without --no-hold" in reason
    if case in ("missing-heartbeat", "stale-heartbeat", "no-guard-directory"):
        assert "heartbeat" in reason
    deadline = ws.volume / pod_run.POD_GUARD_DIRECTORY / "deadline-pod123"
    if deadline.exists():
        assert deadline.read_text(encoding="ascii") == f"{later}\n"
    assert not deadline.with_name("released-pod123").exists()


def test_a_protocol_the_orchestrator_cannot_parse_is_refused_before_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    protocol = ws.repository / "config" / "broken_protocol.toml"
    protocol.write_text("reading_unit = [unclosed\n", encoding="utf-8")

    exit_code, runner = _refused(
        ws, _run_argv(ws, extra=("--perlector-protocol-config", str(protocol)))
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert "not a protocol the orchestrator can seal" in capsys.readouterr().err


def test_a_protocol_outside_the_perlectors_closed_schema_is_refused_before_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    protocol = ws.repository / "config" / "open_protocol.toml"
    committed = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    protocol.write_text('reading_unit = "page"\n' + committed, encoding="utf-8")

    exit_code, runner = _refused(
        ws, _run_argv(ws, extra=("--perlector-protocol-config", str(protocol)))
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert "not its closed schema" in capsys.readouterr().err


def _alternative_protocol(path: Path) -> Path:
    """The committed protocol with one legal value changed: a second valid seal."""

    text = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    assert "\nmaximum_edge = 2560\n" in text
    path.write_text(
        text.replace("\nmaximum_edge = 2560\n", "\nmaximum_edge = 2048\n"), encoding="utf-8"
    )
    return path


def _protocols(ws: Workspace) -> tuple[Path, Path]:
    """The checkout's default protocol and a second valid one with another seal."""

    config = ws.repository / "config"
    default = config / "perlector_protocol.toml"
    default.write_bytes((ROOT / "config" / "perlector_protocol.toml").read_bytes())
    return default, _alternative_protocol(config / "perlector_protocol_edge.toml")


def _sealed_run(ws: Workspace, digests: dict[str, str]) -> None:
    RunTree.create(
        ws.volume / "runs",
        "first-real-run",
        source_manifest=[],
        config_digest="0" * 64,
        adapter_recipes={},
        witness_chairs=[],
        sealed_config_digests=digests,
    )


def _resume(ws: Workspace, extra: tuple[str, ...]) -> tuple[int, PreflightedActions]:
    clock = Clock()
    actions = PreflightedActions()
    code = main(
        _run_argv(ws, extra=("--models", "big", *extra)),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=RecordedRunner(),
    )
    return code, actions


@pytest.mark.parametrize("sealed", ["default", "alternative"])
def test_a_resume_naming_another_protocol_than_its_seal_is_refused_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sealed: str
) -> None:
    """Leaving out, or changing, the protocol on a resume would fail only after a paid bootstrap."""

    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    default, alternative = _protocols(ws)
    sealed_path = default if sealed == "default" else alternative
    _sealed_run(ws, {"perlector-protocol": read_sealed_toml(sealed_path, "protocol")[1]})
    named = ("--perlector-protocol-config", str(alternative))
    matching = () if sealed == "default" else named
    other = named if sealed == "default" else ()

    code, actions = _resume(ws, other)

    assert code == EXIT_REFUSED
    assert actions.calls == []
    reason = _report(ws)["reason"]
    assert "sealed under different inputs" in reason
    assert "Perlector protocol" in reason

    code, actions = _resume(ws, matching)

    assert code == EXIT_COMPLETE
    assert actions.calls


@pytest.mark.parametrize("case", ["not-json", "read-oserror"])
def test_a_resume_whose_run_json_cannot_be_read_is_refused_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    _protocols(ws)
    run_json = ws.volume / "runs" / "first-real-run" / "run.json"
    run_json.parent.mkdir(parents=True)
    if case == "not-json":
        run_json.write_text("{not json", encoding="utf-8")
    else:
        run_json.write_text("{}", encoding="utf-8")

        def unreadable(self: RunTree) -> dict:
            raise PermissionError(13, "Permission denied", str(run_json))

        monkeypatch.setattr(RunTree, "read_run", unreadable)

    code, actions = _resume(ws, ())

    assert code == EXIT_REFUSED
    assert actions.calls == []
    assert "sealed inputs could not be checked" in _report(ws)["reason"]


def test_a_pod_id_is_ascii_letters_and_digits_only(tmp_path: Path) -> None:
    clock = Clock()
    guard = tmp_path / pod_run.POD_GUARD_DIRECTORY
    guard.mkdir()
    for pod_id in ("pod١٢٣", "pod/../x", ""):
        release = pod_run.release_pod_guard(
            tmp_path, pod_id, run_id="r", state="complete", now=clock.now
        )
        assert release["released"] is False
    assert list(guard.iterdir()) == []


def test_a_real_resume_without_its_sealed_mechanics_qualification_is_refused_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    default, _ = _protocols(ws)
    # Computed here from the stage library, not through pod_run, under the
    # values the orchestrator seals when pod_run forwards none of them.
    policy = real_run_policy_digest(
        witness_context="named",
        mechanics_qualification=True,
    )
    _sealed_run(
        ws,
        {
            "perlector-protocol": read_sealed_toml(default, "protocol")[1],
            "run-policy": policy,
        },
    )

    code, actions = _resume(ws, ())

    assert code == EXIT_REFUSED
    assert actions.calls == []
    assert "--mechanics-qualification" in _report(ws)["reason"]

    code, actions = _resume(ws, ("--mechanics-qualification",))

    assert code == EXIT_COMPLETE
    assert actions.calls


def test_the_run_policy_pod_run_assumes_is_the_orchestrator_s_own_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pod_run forwards none of these knobs; a changed orchestrator default must fail here."""

    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()
    main(
        _run_argv(ws, extra=("--mechanics-qualification",)),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    command = runner.calls[0][0]
    captured: dict[str, Namespace] = {}
    parse = argparse.ArgumentParser.parse_args

    class Parsed(Exception):
        pass

    def capture(self, args=None, namespace=None):  # type: ignore[no-untyped-def]
        captured["args"] = parse(self, ["--fixture", "f", "--run-id", "r", "--run-root", "/r"])
        raise Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(Parsed):
        orchestrator.main()
    for name, value in pod_run.ORCHESTRATOR_RUN_POLICY_DEFAULTS.items():
        assert getattr(captured["args"], name) == value
        assert "--" + name.replace("_", "-") not in command


def test_small_models_selects_cheap_stages_and_returns_after_selection(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()
    code = main(
        _run_argv(ws, extra=("--models", "small")),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    assert code == pod_run.EXIT_SELECTION_COMPLETE
    assert clock.seconds == 0
    command = runner.calls[0][0]
    assert command[command.index("--from") : command.index("--from") + 4] == [
        "--from",
        "door",
        "--to",
        "attestatores",
    ]
    report = _report(ws)
    assert report["state"] == "selection-complete"
    assert report["held_to_hard_deadline"] is False
    assert report["hold_detail"].startswith("the selected stages completed")
    assert "did not finish" not in report["hold_detail"]
    assert report["plan"]["selection"]["models"] == "small"
    assert report["plan"]["bootstrap"]["preflight_roles"] == [
        "attestator_1",
        "attestator_2",
        "attestator_3",
        "designator_surya",
        "secondary_proposer",
    ]


@pytest.mark.parametrize(
    "selection",
    [
        ("--models", "big"),
        ("--from", "perlector", "--to", "armarium"),
        ("--stage", "perlector"),
    ],
)
def test_starting_at_perlector_requires_the_attestatores_seal_before_bootstrap(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    actions = PreflightedActions()
    runner = RecordedRunner()
    code = main(
        _run_argv(ws, extra=selection),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=runner,
    )
    assert code == EXIT_REFUSED
    assert "sealed attestatores" in _report(ws)["reason"]
    assert actions.calls == []
    assert runner.calls == []


def test_big_models_maps_to_perlector_through_armarium(tmp_path: Path, monkeypatch) -> None:
    ws = _prepared(tmp_path)
    checked = []
    monkeypatch.setattr(
        pod_run, "verify_predecessor_seal", lambda tree, stage: checked.append((tree.run_id, stage))
    )
    clock = Clock()
    runner = RecordedRunner()
    code = main(
        _run_argv(ws, extra=("--models", "big")),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    assert code == EXIT_COMPLETE
    assert checked == [("first-real-run", "perlector")]
    command = runner.calls[0][0]
    assert command[command.index("--from") : command.index("--from") + 4] == [
        "--from",
        "perlector",
        "--to",
        "armarium",
    ]
    # The selection runs the Coniector, which asks its chair: preflight checks it too.
    assert _report(ws)["plan"]["bootstrap"]["preflight_roles"] == ["perlector", "reconstructor"]


def test_model_slice_ends_at_coniector_with_both_chairs_preflighted(
    tmp_path: Path, monkeypatch
) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    clock = Clock()
    runner = RecordedRunner()
    code = main(
        _run_argv(ws, extra=("--from", "perlector", "--to", "coniector")),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    assert code == pod_run.EXIT_SELECTION_COMPLETE
    command = runner.calls[0][0]
    assert command[command.index("--from") : command.index("--from") + 4] == [
        "--from",
        "perlector",
        "--to",
        "coniector",
    ]
    assert _report(ws)["plan"]["bootstrap"]["preflight_roles"] == [
        "perlector",
        "reconstructor",
    ]


@pytest.mark.parametrize("mode", ["on", "off"])
def test_a_selection_through_the_coniector_preflights_its_chair_only_when_it_asks(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    argv = _run_argv(ws, extra=("--stage", "coniector", "--dry-run"))
    reconstruction = ws.repository / "config" / "reconstruction.toml"
    text = reconstruction.read_text(encoding="utf-8")
    assert 'mode = "on"' in text
    reconstruction.write_text(text.replace('mode = "on"', f'mode = "{mode}"'), encoding="utf-8")
    clock = Clock()

    code = main(
        argv,
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=_never_called,
    )

    assert code == EXIT_DRY_RUN
    roles = json.loads(capsys.readouterr().out)["bootstrap"]["preflight_roles"]
    assert roles == (["reconstructor"] if mode == "on" else [])


def test_a_full_run_held_before_its_export_closes_without_paid_idle_time(tmp_path: Path) -> None:
    """A run stopped at a held Recensor waits for a person, so the pod must not bill.

    The fake orchestrator writes no run tree, so no Armarium export is sealed:
    exactly a full run that stopped before its export. It returns at once with
    its final report, the records it names, and no hold record, so the pod
    timer closes the card (`pod_timer.run_with_bootstrap` on exit 3).
    """
    ws = _prepared(tmp_path)
    clock = Clock()
    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD),
    )
    assert code == EXIT_HELD
    assert clock.seconds == 0
    report = _report(ws)
    assert report["state"] == "held"
    assert report["held_to_hard_deadline"] is False
    assert report["hold_detail"].startswith("the run held before its Armarium export")
    assert report["records_missing"] == []
    assert report["finished_at"] is not None
    assert not (ws.volume / "pod-run-report-hold.json").exists()


def _stop_reading(path: Path, **fields: object) -> dict | None:
    """This run's stop record as `read_stop_record` reads it, written with `fields`."""
    record = {
        "schema": STOP_RECORD_SCHEMA,
        "run_id": "r",
        "exit_code": 3,
        "exported": False,
        "systemic": None,
        **fields,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return pod_run.read_stop_record(path, "r", 3)[0]


def test_only_this_invocations_stop_record_saying_exported_reads_as_reached(
    tmp_path: Path,
) -> None:
    """The stop record decides, never an export the run tree already holds."""
    path = tmp_path / "stop.json"
    assert pod_run.read_stop_record(path, "r", 3)[0] is None  # no record
    assert _stop_reading(path, exported=True)["exported"] is True  # its sealed export
    # Held at the Recensor, whatever the tree holds.
    assert _stop_reading(path, exported=False)["exported"] is False
    assert _stop_reading(path, exported=True, run_id="another") is None
    assert _stop_reading(path, exported=True, schema="another.v1") is None
    for unreadable in ("[]", "{", "\udcff", "null"):
        path.write_text(unreadable, encoding="utf-8", errors="surrogateescape")
        assert pod_run.read_stop_record(path, "r", 3)[0] is None, unreadable


def test_only_this_invocations_stop_record_names_its_systemic_alarm(tmp_path: Path) -> None:
    path = tmp_path / "stop.json"
    line = "run r: systemic: 1 of 2 page(s) are held after the recensor"
    assert _stop_reading(path, systemic=line)["systemic"] == line
    assert _stop_reading(path, systemic=None)["systemic"] is None
    for unusable in ("  ", 3):
        assert _stop_reading(path, systemic=unusable) is None
    assert _stop_reading(path, systemic=line, run_id="another") is None
    assert _stop_reading(path, systemic=line, schema="orchestrator-stop.v1") is None


class NotifyRecorder:
    """A notify runner that records each argv and answers green; no shell, no phone.

    `factory` stands in for `notify_hooks.environment_runner`, recording the
    environment the notification command would run in.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def factory(self, environment):  # type: ignore[no-untyped-def]
        self.environments.append(dict(environment))
        return self

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        import subprocess

        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.mark.parametrize("exported", [False, True], ids=["held", "advanced-and-exported"])
def test_a_systemic_run_on_the_pod_sends_the_alarm_as_a_decision(
    tmp_path: Path, exported: bool
) -> None:
    """Held at the Recensor, or exported past it on an advance, the phone hears of it."""
    from common.review_policy import systemic_notice

    ws = _prepared(tmp_path)
    topic = ws.volume / pod_run.POD_GUARD_DIRECTORY / "ntfy_topic"
    topic.parent.mkdir(parents=True, exist_ok=True)
    topic.write_text("guard-topic-for-the-test\n", encoding="utf-8")
    clock = Clock()
    argv = _run_argv(ws, extra=("--notify",))
    run_id = argv[argv.index("--run-id") + 1]
    line = f"run {run_id}: systemic: 2 of 2 page(s) are held after the recensor"
    notify = NotifyRecorder()
    code = main(
        argv,
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD, stop=exported, systemic=line),
        notify_runner=notify.factory,
    )
    assert code == EXIT_HELD
    [argv] = notify.calls
    assert argv[2:] == ["decision", systemic_notice(run_id, line)]
    report = _report(ws)
    assert report["systemic"] == line
    assert report["systemic_notification"] == "Phone notification: sent."


def test_the_guard_topic_reaches_only_the_notification_command(tmp_path: Path) -> None:
    """The topic is read from the guard's file into the notify call's own environment:
    never an argument, the orchestrator's environment, or the run report."""
    ws = _prepared(tmp_path)
    topic = "guard-topic-for-the-test"
    path = ws.volume / pod_run.POD_GUARD_DIRECTORY / "ntfy_topic"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(topic + "\n", encoding="utf-8")
    clock = Clock()
    notify = NotifyRecorder()
    runner = RecordedRunner(
        returncode=orchestrator.EXIT_HELD, stop=False, systemic="run first-real-run: systemic: x"
    )
    code = main(
        _run_argv(ws, extra=("--notify",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
        notify_runner=notify.factory,
    )
    assert code == EXIT_HELD
    [environment] = notify.environments
    assert environment["NTFY_TOPIC"] == topic
    assert all(topic not in part for call in notify.calls for part in call)
    for argv, _cwd, env in runner.calls:
        assert all(topic not in part for part in argv)
        assert topic not in "".join(f"{k}={v}" for k, v in env.items())
    assert topic not in (ws.volume / "pod-run-report.json").read_text(encoding="utf-8")


def test_without_notify_the_pod_records_the_alarm_and_pages_no_phone(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    notify = NotifyRecorder()
    line = "run first-real-run: systemic: 2 of 2 page(s) are held after the recensor"
    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD, stop=False, systemic=line),
        notify_runner=notify.factory,
    )
    assert code == EXIT_HELD
    assert notify.calls == []
    report = _report(ws)
    assert report["systemic"] == line
    assert report["systemic_notification"] == "Phone notification: not sent (no --notify)."


def test_with_no_guard_topic_the_pod_sends_nothing_and_says_so(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    notify = NotifyRecorder()
    code = main(
        _run_argv(ws, extra=("--notify",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(
            returncode=orchestrator.EXIT_HELD, stop=False, systemic="run first-real-run: x"
        ),
        notify_runner=notify.factory,
    )
    assert code == EXIT_HELD
    assert (notify.environments, notify.calls) == ([], [])
    assert (
        _report(ws)["systemic_notification"]
        == "Phone notification: not sent (no usable guard topic)."
    )


def test_a_run_with_no_systemic_alarm_sends_no_decision(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    notify = NotifyRecorder()
    code = main(
        _run_argv(ws, extra=("--notify",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD, stop=False),
        notify_runner=notify.factory,
    )
    assert code == EXIT_HELD
    assert notify.calls == []
    assert "systemic" not in _report(ws)


@dataclass
class PacedRunner(RecordedRunner):
    """An orchestrator whose Perlector finishes five pages of a hundred every ten minutes."""

    clock: Clock = field(default_factory=Clock)

    def __call__(self, argv, *, cwd, env, transcript, liveness, interval_seconds):  # type: ignore[no-untyped-def]
        run = Path(argv[argv.index("--run-root") + 1]) / argv[argv.index("--run-id") + 1]
        records = run / "4_perlector" / "artifacts" / "page-accounting"
        records.mkdir(parents=True)
        (run / "run.json").write_text(
            json.dumps({"source_manifest": [{}] * 100, "witness_chairs": ["a"]}), "utf-8"
        )
        pages = 10

        def paced(pid: int, alive: bool) -> None:
            nonlocal pages
            for page in range(pages):
                (records / f"art_{page}.json").write_text(
                    json.dumps({"subject_id": f"pg_{page}", "outcome": "read"}), "utf-8"
                )
            liveness(pid, alive)
            pages += 5
            self.clock.sleep(600)

        return super().__call__(
            argv,
            cwd=cwd,
            env=env,
            transcript=transcript,
            liveness=paced,
            interval_seconds=interval_seconds,
        )


# The shipped spend policy with the lead's budget switched on.
SHIPPED_SPEND_BUDGET_ON = (
    (ROOT / "config" / "spend.toml")
    .read_bytes()
    .replace(b'pod_budget = "off"', b'pod_budget = "on"', 1)
)
assert b'pod_budget = "on"' in SHIPPED_SPEND_BUDGET_ON


@pytest.mark.parametrize(
    ("flags", "rates", "hourly", "source"),
    [
        (("--hourly-usd", "1.99"), {}, "1.99", "--hourly-usd"),
        (
            (),
            {"VERBATUS_POD_HOURLY_USD": "1.99", "VERBATUS_VOLUME_ONGOING_HOURLY_USD": "0.06"},
            "2.05",
            "the launch-time estimate before create "
            "(VERBATUS_POD_HOURLY_USD plus VERBATUS_VOLUME_ONGOING_HOURLY_USD)",
        ),
    ],
    ids=["flag", "pod-timer-rates"],
)
def test_a_run_that_will_outlast_its_guard_deadline_sends_one_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    rates: dict[str, str],
    hourly: str,
    source: str,
) -> None:
    ws = _prepared(tmp_path)
    (ws.repository / "config" / "spend.toml").write_bytes(SHIPPED_SPEND_BUDGET_ON)
    clock = Clock()
    _first_process(tmp_path, monkeypatch, "pod123")
    _guard_deadline(ws, int(clock.now().timestamp()) + 3600)
    (ws.volume / pod_run.POD_GUARD_DIRECTORY / "ntfy_topic").write_text("guard-topic\n", "utf-8")
    notify = NotifyRecorder()

    code = main(
        _run_argv(ws, extra=("--notify", *flags)),
        environ=_environ(clock, lifetime=4.0, extra=rates),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=PacedRunner(ticks=3, clock=clock),
        notify_runner=notify.factory,
    )

    assert code == EXIT_COMPLETE
    [call] = notify.calls
    assert call[2] == "decision" and "deadline at risk" in call[3]
    assert "soft max 2 h / $6.00" in call[3] and "deadline-pod123" in call[3]
    report = _report(ws)
    estimate = json.loads(Path(report["estimate_path"]).read_text(encoding="utf-8"))
    assert estimate["deadline_source"] == "the pod guard's deadline"
    assert estimate["estimate"]["stage"] == "perlector"
    [notice] = report["deadline_watch"]["notices"]
    assert notice["delivered"] is True
    assert (estimate["hourly_usd"], estimate["hourly_usd_source"]) == (hourly, source)
    # The rate is quoted with where it came from: a sealed rate is the price before
    # create, and the pod may bill more, up to the spend policy's hourly ceiling.
    assert f"more at ${hourly}/h ({source})" in call[3]
    assert "mv $G/deadline.new $G/deadline-pod123" in call[3]


def test_the_final_report_records_each_page_stage_s_rate(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()

    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=PacedRunner(ticks=3, clock=clock),
    )

    assert code == EXIT_COMPLETE
    report = _report(ws)
    [rate] = report["stage_rates"]
    assert rate["stage"] == "perlector"
    assert (rate["pages_total"], rate["pages_per_minute"]) == (100, 0.5)
    progress = json.loads(Path(report["progress_path"]).read_text(encoding="utf-8"))
    assert progress["stage"] == "perlector" and progress["status"] == "ok"
    assert progress["stage_rates"] == report["stage_rates"]


SEALED_BUDGET = {
    "VERBATUS_SOFT_MAX_SECONDS": "7200",
    "VERBATUS_HARD_MAX_SECONDS": "10800",
    "VERBATUS_SOFT_MAX_COST_USD": "1.00",
    "VERBATUS_HARD_MAX_COST_USD": "1.50",
}
SHIPPED_SPEND_SHA256 = hashlib.sha256(SHIPPED_SPEND_BUDGET_ON).hexdigest()


@pytest.mark.parametrize(
    ("sealed", "limits", "source"),
    [
        (
            SEALED_BUDGET,
            "soft max 2 h / $1.00, hard max 3 h / $1.50",
            "sealed into the pod at launch",
        ),
        (
            {},
            "soft max 2 h / $6.00, hard max 3 h / $9.00",
            f"the checked-out config/spend.toml (SHA-256 {SHIPPED_SPEND_SHA256})",
        ),
        (
            {"VERBATUS_SOFT_MAX_SECONDS": "7200"},
            "soft and hard max unknown",
            "sealed into the pod at launch",
        ),
    ],
    ids=["sealed", "checkout", "half-sealed"],
)
def test_the_deadline_notice_quotes_the_budget_that_armed_the_pod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sealed: dict[str, str],
    limits: str,
    source: str,
) -> None:
    """A launch seals its budget into the pod; the checkout's own spend policy, which the
    laptop may not have launched with, is used only when nothing was sealed, and then
    named with its digest. A budget sealed in part is never filled from the checkout."""
    ws = _prepared(tmp_path)
    (ws.repository / "config" / "spend.toml").write_bytes(SHIPPED_SPEND_BUDGET_ON)
    clock = Clock()
    _first_process(tmp_path, monkeypatch, "pod123")
    created = int(clock.now().timestamp())
    _guard_deadline(ws, created + 3600)
    (ws.volume / pod_run.POD_GUARD_DIRECTORY / "ntfy_topic").write_text("guard-topic\n", "utf-8")
    # The instant the start command records, from which the hard maximum counts.
    (ws.volume / pod_run.POD_GUARD_DIRECTORY / "created-pod123").write_text(f"{created}\n")
    notify = NotifyRecorder()

    code = main(
        _run_argv(ws, extra=("--notify", "--hourly-usd", "1.99")),
        environ=_environ(clock, lifetime=4.0, extra=sealed),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=PacedRunner(ticks=3, clock=clock),
        notify_runner=notify.factory,
    )

    assert code == EXIT_COMPLETE
    [call] = notify.calls
    assert limits in call[3]
    if sealed.keys() in (SEALED_BUDGET.keys(), set()):
        hard_end = datetime.fromtimestamp(created + 10_800, UTC).strftime("%Y-%m-%d %H:%M UTC")
        assert f"the hard maximum {hard_end}" in call[3]
    estimate = json.loads(Path(_report(ws)["estimate_path"]).read_text(encoding="utf-8"))
    assert source in estimate["budget_source"]
    if sealed == SEALED_BUDGET:
        assert estimate["budget"]["soft_max_seconds"] == 7200
    elif sealed:
        assert estimate["budget"] is None
        assert estimate["budget_problem"].endswith("VERBATUS_HARD_MAX_COST_USD missing")


def test_a_finish_estimate_that_fails_never_stops_the_run_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()

    def broken(self: object) -> None:
        raise RuntimeError("unreadable tree")

    monkeypatch.setattr(pod_run.finish_estimate.RunTreeProgress, "sample", broken)

    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(ticks=2),
    )

    assert code == EXIT_COMPLETE
    watch = _report(ws)["deadline_watch"]
    assert watch["tick_failures"] == 2
    assert "unreadable tree" in watch["last_tick_failure"]


@pytest.mark.parametrize(
    ("rates", "reason"),
    [
        ({"VERBATUS_POD_HOURLY_USD": "1.99"}, "VERBATUS_VOLUME_ONGOING_HOURLY_USD missing"),
        ({"VERBATUS_VOLUME_ONGOING_HOURLY_USD": "0.06"}, "VERBATUS_POD_HOURLY_USD missing"),
        ({}, None),
    ],
)
def test_a_half_set_pod_timer_rate_is_named_not_dropped(
    rates: dict[str, str], reason: str | None
) -> None:
    plan = SimpleNamespace(hourly_usd=None)

    assert pod_run._hourly_price(plan, rates) == (None, reason)  # type: ignore[arg-type]


@pytest.mark.parametrize("price", ["abc", "0", "-1.00", "NaN"])
def test_an_hourly_price_that_is_not_a_positive_decimal_is_refused(
    tmp_path: Path, price: str
) -> None:
    ws = _prepared(tmp_path)

    exit_code, runner = _refused(ws, _run_argv(ws, extra=("--hourly-usd", price)))

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    report = json.loads((ws.volume / "pod-run-report.json").read_text("utf-8"))
    assert "--hourly-usd" in report["reason"]


@pytest.mark.parametrize(
    ("stop", "holding"),
    [(True, True), (False, False), (None, False), ("[1]", False)],
    ids=["exported", "held-before-export", "no-record", "unreadable"],
)
def test_a_full_held_run_holds_only_on_its_own_stop_record(
    tmp_path: Path, stop: bool | str | None, holding: bool
) -> None:
    """Whatever the stop record says, or fails to, the final report is written."""
    ws = _prepared(tmp_path)
    clock = Clock()
    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD, stop=stop),
    )
    assert code == EXIT_HELD
    report = _report(ws)
    assert report["state"] == "held" and report["finished_at"] is not None
    assert report["held_to_hard_deadline"] is holding
    assert clock.seconds == (4.0 if holding else 0)
    assert ("stop_record_problem" in report) is (not isinstance(stop, bool))


@pytest.mark.parametrize(
    ("returncode", "code", "state", "opening"),
    [
        (orchestrator.EXIT_COMPLETE, EXIT_HELD, "held", "the orchestrator completed, but"),
        (pod_run.ORCHESTRATOR_FATAL, EXIT_FAILED, "failed", "the orchestrator exited EXIT_FATAL"),
    ],
    ids=["exit-complete", "exit-fatal"],
)
def test_a_run_whose_stop_record_was_not_written_is_never_complete_and_says_why(
    tmp_path: Path, returncode: int, code: int, state: str, opening: str
) -> None:
    """A systemic run whose stop record could not be written: the orchestrator ends
    fatally (`_record_stop`), and even an exit of complete with no record is not
    called complete, since the alarm it sounded is unknown. With a guard topic
    and --notify, nothing is sent: the alarm is read from the record alone, never
    from the transcript that printed it."""
    alarm = "run first-real-run: systemic: 1 of 2 page(s) are held after the recensor"
    ws = _prepared(tmp_path)
    topic = ws.volume / pod_run.POD_GUARD_DIRECTORY / "ntfy_topic"
    topic.parent.mkdir(parents=True, exist_ok=True)
    topic.write_text("a-topic\n", encoding="utf-8")
    clock = Clock()
    notify = NotifyRecorder()
    result = main(
        _run_argv(ws, extra=("--notify",)),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        # The alarm was printed, so it is in the transcript, but no record names it.
        runner=RecordedRunner(
            returncode=returncode, stop=None, transcript_text=f"{alarm}\n".encode("utf-8")
        ),
        notify_runner=notify.factory,
    )
    assert result == code
    report = _report(ws)
    assert report["state"] == state
    assert report["stop_record_problem"] == "the orchestrator left no stop record"
    assert report["detail"].startswith(opening)
    assert "whether this invocation sounded the systemic alarm" in report["detail"]
    assert report["held_to_hard_deadline"] is False
    assert clock.seconds == 0
    assert (notify.environments, notify.calls) == ([], [])
    assert "systemic" not in report and "systemic_notification" not in report
    if state == "held":
        assert report["hold_detail"].startswith("the run held, and with no usable stop record")


def test_a_stop_record_of_another_exit_never_holds_the_pod(tmp_path: Path) -> None:
    """`exported` decides the paid hold only when the record's exit is the one observed."""
    ws = _prepared(tmp_path)
    clock = Clock()
    record = {
        "schema": STOP_RECORD_SCHEMA,
        "run_id": "first-real-run",
        "exit_code": orchestrator.EXIT_COMPLETE,
        "exported": True,
        "systemic": None,
    }
    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD, stop=json.dumps(record)),
    )
    assert code == EXIT_HELD
    report = _report(ws)
    assert report["stop_record_problem"] == (
        "the orchestrator's stop record says exit 0, but it exited 3"
    )
    assert report["held_to_hard_deadline"] is False
    assert clock.seconds == 0


def test_a_stop_record_is_usable_only_as_this_runs_v2_record(tmp_path: Path) -> None:
    path = tmp_path / "stop.json"
    good = {"schema": STOP_RECORD_SCHEMA, "run_id": "r", "exit_code": 3, "exported": False}

    def read(text: str) -> tuple[dict | None, str | None]:
        path.write_text(text, encoding="utf-8", errors="surrogateescape")
        return pod_run.read_stop_record(path, "r", 3)

    assert read(json.dumps({**good, "systemic": None})) == ({**good, "systemic": None}, None)
    assert pod_run.read_stop_record(tmp_path / "none.json", "r", 3) == (
        None,
        "the orchestrator left no stop record",
    )
    for text, reason in (
        ("{", "could not be read"),
        ("\udcff", "could not be read"),
        ("null", "is not an orchestrator-stop.v2 record"),
        (json.dumps({**good, "systemic": None, "run_id": "x"}), "is not run r's"),
        (json.dumps(good), "no usable exported or systemic field"),
        (json.dumps({**good, "systemic": "  "}), "no usable exported or systemic field"),
        (json.dumps({**good, "systemic": None, "exported": 1}), "no usable"),
        (json.dumps({**good, "systemic": None, "exit_code": None}), "no integer exit_code"),
        (json.dumps({**good, "systemic": None, "exit_code": "3"}), "no integer exit_code"),
        (json.dumps({**good, "systemic": None, "exit_code": True}), "no integer exit_code"),
        (
            json.dumps({k: v for k, v in {**good, "systemic": None}.items() if k != "exit_code"}),
            "no integer exit_code",
        ),
        (json.dumps({**good, "systemic": None, "exit_code": 0}), "says exit 0, but it exited 3"),
    ):
        record, problem = read(text)
        assert record is None and reason in problem, text


def test_a_held_selection_closes_without_paid_idle_time(tmp_path: Path, monkeypatch) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    clock = Clock()
    code = main(
        _run_argv(ws, extra=("--stage", "attestatores")),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=RecordedRunner(returncode=orchestrator.EXIT_HELD),
    )
    assert code == EXIT_HELD
    assert clock.seconds == 0
    report = _report(ws)
    assert report["held_to_hard_deadline"] is False
    assert report["hold_detail"].startswith("the selection held")
    assert not (ws.volume / "pod-run-report-hold.json").exists()


@pytest.mark.parametrize(
    ("runner", "missing"),
    [
        (RecordedRunner(write_transcript=False), "transcript"),
        (RecordedRunner(transcript_failure="the transcript write failed"), "transcript"),
        (RecordedRunner(tick_liveness=False), "liveness"),
    ],
)
def test_a_selection_whose_records_did_not_come_home_is_held_not_complete(
    tmp_path: Path, runner: RecordedRunner, missing: str
) -> None:
    """A selection is downgraded exactly as a full run is, and still returns at once."""

    ws = _prepared(tmp_path)
    clock = Clock()
    code = main(
        _run_argv(ws, extra=("--models", "small")),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    assert code == EXIT_HELD
    assert clock.seconds == 0
    report = _report(ws)
    assert report["state"] == "held"
    assert report["records_missing"] == [missing]
    assert report["detail"].startswith("the orchestrator completed, but")
    assert report["held_to_hard_deadline"] is False
    assert report["hold_detail"].startswith("the selection held")
    assert not (ws.volume / "pod-run-report-hold.json").exists()


def test_a_selection_from_the_perlector_passes_through_to_the_orchestrator_on_the_pod(
    tmp_path: Path, monkeypatch
) -> None:
    """The route a person's page re-ask is read on: pod_run hands a selection from the
    Perlector to the orchestrator unchanged. It checks the pass-through only; the
    re-read itself is the Perlector's (`pipeline/test_operator_reread_e2e.py`). The
    provider and the runner are fakes.
    """
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    clock = Clock()
    runner = RecordedRunner(returncode=orchestrator.EXIT_HELD)
    code = main(
        _run_argv(ws, extra=("--from", "perlector", "--to", "armarium")),
        environ=_environ(clock, lifetime=4.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )
    assert code == EXIT_HELD
    [(argv, _cwd, _env)] = runner.calls
    start = argv.index("--from")
    assert argv[start : start + 4] == ["--from", "perlector", "--to", "armarium"]


@pytest.mark.parametrize(
    ("selection", "predecessor"),
    [
        (("--stage", "exemplar"), "door"),
        (("--from", "designator", "--to", "attestatores"), "ink-map"),
        (("--stage", "recensor"), "perlector"),
        (("--from", "archetypus", "--to", "armarium"), "recensor"),
    ],
)
def test_every_selection_after_the_door_requires_its_predecessor_seal_before_bootstrap(
    tmp_path: Path, selection: tuple[str, ...], predecessor: str
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    actions = PreflightedActions()
    runner = RecordedRunner()
    code = main(
        _run_argv(ws, extra=selection),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: actions,
        runner=runner,
    )
    assert code == EXIT_REFUSED
    assert f"sealed {predecessor} stage" in _report(ws)["reason"]
    assert actions.calls == []
    assert runner.calls == []


def test_auto_and_empty_selection_preflight_roles(tmp_path: Path, capsys) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    # A full run preflights exactly the chairs its stages use, not every configured row.
    full = [
        "attestator_1",
        "attestator_2",
        "attestator_3",
        "designator_surya",
        "perlector",
        "reconstructor",
        "secondary_proposer",
    ]
    for extra, expected in (((), full), (("--stage", "door"), [])):
        assert (
            main(
                _run_argv(ws, extra=(*extra, "--dry-run")),
                environ=_environ(clock),
                now=clock.now,
                sleeper=clock.sleep,
            )
            == EXIT_DRY_RUN
        )
        assert json.loads(capsys.readouterr().out)["bootstrap"]["preflight_roles"] == expected


def test_attestatores_preflight_roles_follow_the_configured_roster(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(
        pod_run,
        "load_models_toml",
        lambda path: SimpleNamespace(chairs={"attestator_7": object(), "perlector": object()}),
    )
    clock = Clock()
    assert (
        main(
            _run_argv(ws, extra=("--models", "small", "--dry-run")),
            environ=_environ(clock),
            now=clock.now,
            sleeper=clock.sleep,
        )
        == EXIT_DRY_RUN
    )
    roles = json.loads(capsys.readouterr().out)["bootstrap"]["preflight_roles"]
    assert roles == [
        "attestator_7",
        "designator_surya",
        "secondary_proposer",
    ]


def test_selection_refuses_missing_chair_smoke_after_preflight(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    class PartialSmoke(PreflightedActions):
        def run_preflight(self) -> dict[str, object]:
            return self._step(
                BootstrapStep.PREFLIGHT,
                {
                    "color": "green",
                    "placement_tier": TIER,
                    "serving_config_inputs": SERVING_INPUTS,
                    "smoke_receipts": [{"chair": "attestator_1"}],
                },
            )

    code = main(
        _run_argv(ws, extra=("--models", "small")),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PartialSmoke(),
        runner=runner,
    )
    assert code == EXIT_REFUSED
    assert runner.calls == []
    assert "attestator_2" in _report(ws)["reason"]


def test_a_coniector_selection_is_refused_without_reconstructor_preflight_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run, "verify_predecessor_seal", lambda tree, stage: None)
    argv = _run_argv(ws, extra=("--stage", "coniector"))
    reconstruction = ws.repository / "config" / "reconstruction.toml"
    assert 'mode = "on"' in reconstruction.read_text(encoding="utf-8")
    clock = Clock()
    runner = RecordedRunner()

    class NoReconstructorSmoke(PreflightedActions):
        def run_preflight(self) -> dict[str, object]:
            return self._step(
                BootstrapStep.PREFLIGHT,
                {
                    "color": "green",
                    "placement_tier": TIER,
                    "serving_config_inputs": SERVING_INPUTS,
                    "smoke_receipts": [{"chair": "perlector", "valid": True}],
                },
            )

    code = main(
        argv,
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: NoReconstructorSmoke(),
        runner=runner,
    )

    assert code == EXIT_REFUSED
    assert runner.calls == []
    assert "reconstructor" in _report(ws)["reason"]


@pytest.mark.parametrize(
    "drop",
    ["subprocess_receipts", "cache_receipts", "placements"],
)
def test_the_designator_needs_surya_s_measured_subprocess_run(tmp_path: Path, drop: str) -> None:
    """Surya is never smoke-read: a selection with the Designator needs its verified
    cache and its own golden-page run, and a Surya not placed as a subprocess would
    need a smoke receipt it can never have."""
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()

    class NoSuryaRun(PreflightedActions):
        def run_preflight(self) -> dict[str, object]:
            receipt = PreflightedActions().run_preflight()
            receipt[drop] = [row for row in receipt[drop] if row["chair"] != "designator_surya"]
            return self._step(BootstrapStep.PREFLIGHT, receipt)

    code = main(
        _run_argv(ws, extra=("--models", "small")),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: NoSuryaRun(),
        runner=runner,
    )
    assert code == EXIT_REFUSED
    assert runner.calls == []
    assert "['designator_surya']" in _report(ws)["reason"]


def test_forwards_bootstrap_cache_and_trial_triage_inputs_to_the_orchestrator(
    tmp_path: Path,
) -> None:
    """The normal run uses the cache bootstrap verified and preserves Door triage and register inputs."""

    ws = _prepared(tmp_path)
    triage = ws.volume / "triage"
    triage.mkdir()
    decision = triage / "decisions.json"
    clusters = triage / "clusters.json"
    recipe = triage / "recipe.json"
    register = triage / "register.json"
    birds = ws.volume / "birds"
    birds.mkdir()
    bird_ledger = ws.volume / "bird-ledger.json"
    bird_ledger.write_text("{}", encoding="utf-8")
    for path in (decision, clusters, recipe, register):
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
                    "--corpus-register",
                    str(register),
                    "--canary-folder",
                    str(birds),
                    "--canary-manifest",
                    str(bird_ledger),
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
    assert command[command.index("--cache-root") + 1] == str(
        Path("/var/tmp/verbatus-chair-cache").resolve()
    )
    assert command[command.index("--store-root") + 1] == str(ws.store_root)
    for flag, path in (
        ("--triage-decision-manifest", decision),
        ("--triage-clusters", clusters),
        ("--triage-producer-recipe", recipe),
        # Without the register a confirmed re-shoot is refused at the Door.
        ("--corpus-register", register),
        ("--canary-folder", birds),
        ("--canary-manifest", bird_ledger),
    ):
        assert command[command.index(flag) + 1] == str(path)


@pytest.mark.parametrize(
    ("orchestrator_exit", "expected_exit", "state"),
    [(3, EXIT_HELD, "held"), (4, EXIT_HALTED, "halted"), (1, EXIT_FAILED, "failed")],
)
def test_a_partial_run_never_exits_zero_and_the_report_names_its_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_exit: int,
    expected_exit: int,
    state: str,
) -> None:
    ws = _prepared(tmp_path)
    clock = Clock()
    # A held run that reached its sealed export: the terminal hold.
    runner = RecordedRunner(returncode=orchestrator_exit, stop=True)

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
        # A held or halted report is the one that most needs a reason: `None`
        # here would read as "nothing further to say" while the reason sat on
        # a container stderr that dies with the pod. Name the transcript the
        # reason is durable in instead.
        assert str(ws.volume / "pod-run-report-transcript.log") in report["detail"]
    # A run that finished -- held, like complete -- holds to the hard deadline,
    # because `pod_timer` reads an early child exit as `completed-early` and
    # closes the pod with a non-green timer report. A run that did *not* finish
    # returns at once instead: holding a rented card to the deadline for a
    # halted or failed run bills for nothing, so it is never held; this is the
    # same close the red-bootstrap branch already takes.
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
    """The narrowing is recorded, not only the approval.

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
        return pod_run.RunnerResult(0)

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
    ws.report_path.write_bytes(b"the bootstrap's own record")

    exit_code, _runner = _refused(ws, _run_argv(ws, report_path=ws.report_path))

    assert exit_code == EXIT_REFUSED
    assert "collides" in capsys.readouterr().err
    assert ws.report_path.read_bytes() == b"the bootstrap's own record"
    assert not (ws.volume / "pod-run-report.json").exists()


@pytest.mark.parametrize("bootstrap_record", ("report", "journal"))
@pytest.mark.parametrize("side", ("report", "hold", "liveness", "timings", "transcript.log"))
def test_refuses_a_bootstrap_record_colliding_with_any_run_report_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bootstrap_record: str, side: str
) -> None:
    ws = _prepared(tmp_path)
    run_report = ws.volume / "pod-run-report.json"
    collision = (
        run_report
        if side == "report"
        else run_report.with_name(
            f"{run_report.stem}-{side}{run_report.suffix if side != 'transcript.log' else ''}"
        )
    )
    if bootstrap_record == "report":
        ws.report_path = collision
    else:
        ws.journal = collision
    collision.write_bytes(b"bootstrap evidence")

    exit_code, _runner = _refused(ws, _run_argv(ws, report_path=run_report))

    assert exit_code == EXIT_REFUSED
    assert "collides" in capsys.readouterr().err
    assert collision.read_bytes() == b"bootstrap evidence"
    if collision != run_report:
        assert not run_report.exists()


def test_refuses_a_run_report_path_outside_the_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    outside = tmp_path / "outside" / "pod-run-report.json"

    exit_code, _runner = _refused(ws, _run_argv(ws, report_path=outside))

    assert exit_code == EXIT_REFUSED
    assert "--report-path" in capsys.readouterr().err
    assert not outside.exists()


@pytest.mark.hostile_local
def test_a_symlinked_transcript_cannot_truncate_the_bootstrap_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    evidence = b"bootstrap evidence\n"
    ws.report_path.write_bytes(evidence)
    (ws.volume / "pod-run-report-transcript.log").symlink_to(ws.report_path)

    exit_code, _ = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert "collides" in capsys.readouterr().err
    assert ws.report_path.read_bytes() == evidence


@pytest.mark.hostile_local
def test_the_transcript_writer_refuses_a_symlink(tmp_path: Path) -> None:
    evidence = tmp_path / "bootstrap.json"
    evidence.write_bytes(b"bootstrap evidence\n")
    transcript = tmp_path / "run-transcript.log"
    transcript.symlink_to(evidence)

    with pytest.raises(OSError):
        pod_run.BoundedTranscript(transcript, head_bytes=8, tail_bytes=8)

    assert evidence.read_bytes() == b"bootstrap evidence\n"


def test_refuses_a_run_report_path_missing_the_launch_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    ws.report_path = ws.volume / "bootstrap-report-launch-abc123.json"
    ws.journal = ws.volume / "bootstrap-journal-launch-abc123.json"
    clock = Clock()
    environment = _environ(clock, extra={"VERBATUS_LAUNCH_TOKEN": "launch-abc123"})
    ws.report_path.write_bytes(b"the bootstrap's own record")

    exit_code, _runner = _refused(ws, _run_argv(ws), environ=environment)

    assert exit_code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "--report-path" in err and "this launch's token" in err
    assert ws.report_path.read_bytes() == b"the bootstrap's own record"
    assert not (ws.volume / "pod-run-report.json").exists()


def test_an_unknown_run_argument_is_refused_by_name_only_on_the_run_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)
    ws.report_path.write_bytes(b"the bootstrap's own record")

    exit_code, _runner = _refused(ws, _run_argv(ws, extra=("-h", "--old-flag=hunter2")))

    assert exit_code == EXIT_REFUSED
    assert ws.report_path.read_bytes() == b"the bootstrap's own record"
    reason = _report(ws)["reason"]
    assert "--old-flag" in reason and "(value)" in reason
    assert "hunter2" not in reason + capsys.readouterr().err


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


@pytest.mark.parametrize(
    ("case", "keyword"),
    (
        ("run-root", "--run-root"),
        ("run-id", "--run-id refused"),
        ("submission-folder", "--submission-folder"),
        ("submission-manifest", "--submission-manifest"),
        ("submission-outside", "--submission-folder"),
        ("data-policy-missing", "--data-gate-policy"),
        ("data-policy-outside", "--data-gate-policy"),
    ),
)
def test_run_plan_refusals_name_the_bad_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, keyword: str
) -> None:
    ws = _prepared(tmp_path)
    argv = _run_argv(ws)
    if case == "run-root":
        argv = _run_argv(ws, extra=("--run-root", str(tmp_path / "elsewhere")))
    elif case == "run-id":
        argv = _run_argv(ws, run_id="My-Run")
    elif case == "submission-folder":
        argv[argv.index("--submission-folder") + 1] = str(ws.volume / "submission" / "absent")
    elif case == "submission-manifest":
        (ws.volume / "submission" / "manifest.json").unlink()
    elif case == "submission-outside":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        argv[argv.index("--submission-folder") + 1] = str(elsewhere)
    elif case == "data-policy-missing":
        (ws.repository / "config" / "data_handling_policy.json").unlink()
    else:
        outside = tmp_path / "elsewhere-policy.json"
        outside.write_text("{}", encoding="utf-8")
        argv = _run_argv(ws, extra=("--data-gate-policy", str(outside)))

    exit_code, runner = _refused(ws, argv)
    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert keyword in capsys.readouterr().err


def test_a_refusal_report_write_failure_is_named_not_swallowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that refuses *and* fails to leave its durable reason must keep
    the refusal exit code while naming the write failure on stderr. Principle
    2 binds the write failure too.
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


def test_refuses_before_bootstrap_when_the_policy_does_not_admit_the_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shipped policy names ``private/`` only; the volume is the project lead's to list.

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
    assert "reserved to the project lead" in err


def test_refuses_the_pod_mount_path_when_it_is_only_a_plain_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path a real launch seals must actually be mounted, not merely present."""

    ws = _prepared(tmp_path)
    monkeypatch.setattr(pod_run.bootstrap_main, "POD_VOLUME_MOUNT_PATH", str(ws.volume))

    exit_code, runner = _refused(ws, _run_argv(ws))

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    err = capsys.readouterr().err
    assert "expected network-volume mount point" in err
    assert "does not have anything mounted there" in err


def test_the_pre_bootstrap_refusal_names_a_root_this_machine_did_not_have(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The narrowing is recorded even on the path where nothing else gets to say it."""

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
    finished.
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
    assert report["reason"] == "bootstrap actions could not be built: the workspace has no uv"
    assert report["bootstrap"] is None


def test_a_bootstrap_whose_result_could_not_be_written_says_so_not_unbuildable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steps ran; only writing their result failed, and the run report names that."""

    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner(returncode=0)
    real_write = pod_run.bootstrap_main.atomic_write

    def refuse_the_bootstrap_result(path, data):  # type: ignore[no-untyped-def]
        if Path(path) == ws.report_path:
            raise OSError(28, "No space left on device")
        return real_write(path, data)

    monkeypatch.setattr(pod_run.bootstrap_main, "atomic_write", refuse_the_bootstrap_result)

    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=1.0),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=runner,
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    report = _report(ws)
    assert report["state"] == "refused"
    assert "the bootstrap ran (bootstrap-green)" in report["reason"]
    assert "No space left on device" in report["reason"]
    assert "could not be built" not in report["reason"]
    assert report["bootstrap"]["color"] == "green"


def test_refuses_a_credential_looking_value_in_either_half(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _prepared(tmp_path)

    exit_code, _runner = _refused(
        ws, _run_argv(ws, bootstrap_extra=("--transfer-prefix", "my-api-key-123"))
    )

    assert exit_code == EXIT_REFUSED
    assert "looks like a credential" in capsys.readouterr().err


# --- the transcript and the liveness tick ------------------------


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
    absence beside a `running` report is the signal.
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
    """`detail: null` on the two outcomes that most need a reason."""

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


@pytest.mark.parametrize(
    "close_error",
    [OSError(28, "No space left on device"), ValueError("I/O operation on closed file")],
    ids=["full-volume", "closed-under-the-reader"],
)
def test_a_transcript_close_failure_keeps_the_orchestrator_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_error: Exception
) -> None:
    """A close that fails after the child ran is a transcript failure, not a failed start."""

    real_close = pod_run.BoundedTranscript.close

    def close_then_fail(self: pod_run.BoundedTranscript) -> None:
        real_close(self)
        raise close_error

    monkeypatch.setattr(pod_run.BoundedTranscript, "close", close_then_fail)
    completed = pod_run._run(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        transcript=tmp_path / "report-transcript.log",
        liveness=lambda pid, alive: None,
        interval_seconds=0.01,
    )

    assert completed.returncode == 3
    assert completed.transcript_failure is not None
    assert "transcript close failed" in completed.transcript_failure
    assert type(close_error).__name__ in completed.transcript_failure


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


# --- naming the launch's records so they can be fetched ----------


def test_the_sibling_suffixes_launch_derives_are_the_ones_pod_run_actually_writes() -> None:
    """Run side-file names come from the one shared path derivation."""

    report = Path("/workspace/pod-run-report-abc.json")
    plan = object.__new__(pod_run.RunPlan)
    object.__setattr__(plan, "report_path", report)
    written = {
        plan.hold_path.name.removeprefix(report.stem),
        plan.liveness_path.name.removeprefix(report.stem),
        plan.timing_journal_path.name.removeprefix(report.stem),
        plan.transcript_path.name.removeprefix(report.stem),
        plan.estimate_path.name.removeprefix(report.stem),
        plan.progress_path.name.removeprefix(report.stem),
    }

    nested = json.dumps(["python", "-m", pod_run.__name__, "--report-path", str(report)])
    command = ("--report-path", "/workspace/timer.json", "--bootstrap-command-json", nested)
    siblings = dict(launch_module.bound_report_paths(command))[str(report)]
    assert written == set(siblings)
    assert run_report_paths(report)[1].name.removeprefix(report.stem) in written
    timer_report = Path("/v/pod-runtime-report.log")
    assert dict(launch_module.bound_report_paths(("--report-path", str(timer_report))))[
        str(timer_report)
    ] == (terminating_path(timer_report).name.removeprefix(timer_report.stem),)


def test_launch_uses_the_run_reports_actual_extension_for_fetch_keys() -> None:
    report = "/workspace/run-receipt.data"
    nested = json.dumps(["python", "-m", pod_run.__name__, "--report-path", report])
    command = ("--report-path", "/workspace/timer.json", "--bootstrap-command-json", nested)

    keys = launch_module.launch_evidence_keys(command, volume_mount_path="/workspace")

    assert "run-receipt-timings.data" in keys
    assert "run-receipt-transcript.log" in keys


def test_launch_uses_the_bootstrap_report_without_inventing_a_hold_key() -> None:
    report = "/workspace/bootstrap-receipt.data"
    nested = json.dumps(["python", "-m", "operations.pod.bootstrap_main", "--report-path", report])
    command = ("--report-path", "/workspace/timer.log", "--bootstrap-command-json", nested)

    keys = launch_module.launch_evidence_keys(command, volume_mount_path="/workspace")

    assert "timer-terminating.log" in keys
    assert "bootstrap-receipt.data" in keys
    assert "bootstrap-receipt-hold.data" not in keys


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
        f"pod-run-report-{token}-estimate.json",
        f"pod-run-report-{token}-progress.json",
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
    """Every real request (boot_a_request.py, boot_b_request.py)
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
    a traceback instead of in a key list. It is dropped
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

    The action's own data sits under `payload`, never at the top level: a
    fixture that put it at the top level would let the console's derivation
    read a key no real receipt carries while the suite stayed green over it;
    `test_the_console_derives_those_keys_from_a_real_
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
    flag exists to prevent, on the flag itself.
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
        f"pod-run-report-{token}-estimate.json",
        f"pod-run-report-{token}-progress.json",
    )


def test_a_hold_only_launch_receipt_is_refused_when_a_run_id_is_requested(
    tmp_path: Path,
) -> None:
    """`recorded_run_id is None` is not "nothing to compare" once a specific
    run is being fetched: a hold-only launch still has its own real,
    derivable evidence keys -- just none that belong to any run. Supplying
    it to `fetch-run --run-id` would store that boot's evidence beside a
    run it never proves started."""

    from operations.operator.records import ReceiptStore

    token = "h" * 32
    hold_only = json.dumps(
        [
            "python",
            "-m",
            "operations.pod.bootstrap_main",
            "--hold-only",
            "--report-path",
            f"/workspace/bootstrap-hold-only-report-{token}.json",
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
            hold_only,
        ],
    }
    receipt = ReceiptStore(tmp_path / "state").write(
        "launch", {"summary": "fixture launch", "request": request}
    )

    with pytest.raises(OperatorError) as refusal:
        operator_cli._derived_evidence_keys(
            receipt, VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol-1"), "r1"
        )

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "does not prove that run 'r1' started" in refusal.value.detail

    # The same receipt, asked for without naming a run, still derives its
    # own (hold-only) evidence keys normally.
    assert operator_cli._derived_evidence_keys(
        receipt, VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol-1")
    ) == (
        f"pod-runtime-report-{token}.json",
        f"pod-runtime-report-{token}-terminating.json",
        f"bootstrap-hold-only-report-{token}.json",
    )


@pytest.mark.hostile_local
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
        # A subprocess row runs in its own locked environment, not the pod group's.
        if profile["kind"] == "subprocess":
            continue
        for package, version in profile.get("required_packages", {}).items():
            assert pins.setdefault(package, version) == version, (
                f"the real catalogue pins {package} at two versions"
            )
        assert profile["kind"] != "vllm" or "vllm" in profile["required_packages"]
    return pins


def test_the_real_catalogue_pins_one_serving_stack() -> None:
    """Every vLLM row names the same versions; the group carries exactly these.

    The stack is the one researched for the four ruled chairs: vLLM 0.30.0 registers
    both architectures the roster declares — `Qwen3_5ForConditionalGeneration`
    (Chandra-2 and the Perlector) and `Qwen2_5_VLForConditionalGeneration` (the DAI
    fine-tune and Churro-3B) — and its `huggingface_hub>=1.31.0` floor is met by the
    project's `huggingface_hub==1.31.0`. No
    `flash-attn`: vLLM brings its own FlashAttention through its attention backend
    registry, and the PyPI package is sdist-only.
    """

    pins = _recipe_pins()

    assert set(pins) == {"vllm", "transformers", "qwen-vl-utils", "ultralytics", "torch"}
    assert pins["vllm"] == "0.30.0"
    assert pins["transformers"] == "5.14.1"
    # The record detector's in-process row: the Ultralytics release its
    # checkpoint was written by, over the torch vLLM already resolves.
    assert pins["ultralytics"] == "8.4.14"
    assert pins["torch"] == "2.13.0"


def test_the_pod_dependency_group_carries_exactly_the_recipe_pins() -> None:
    """The locked group and the catalogue's rows are the same bytes, both ways."""

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


# --- the records the report names are audited at close ----


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
        "unreadable_lines": 0,
        "foreign_lines": 0,
    }


def test_a_completed_run_whose_timing_journal_never_landed_stays_complete(
    tmp_path: Path,
) -> None:
    """A failed stopwatch is visible in the report but does not hold output."""

    ws = _prepared(tmp_path)
    report = _run_with(ws, RecordedRunner(returncode=0, ticks=1, journal_run_id=None))

    assert report["state"] == "complete"
    assert report["exit_code"] == EXIT_COMPLETE
    assert report["orchestrator_exit"] == 0
    assert report["held_to_hard_deadline"] is True
    assert report["records_missing"] == ["timing_journal"]
    assert report["records_at_close"]["timing_journal"]["present"] is False
    assert "records this report names" in report["detail"]
    assert "timing_journal" in report["detail"]
    assert str(ws.volume / "pod-run-report-transcript.log") in report["detail"]
    assert _report(ws, "pod-run-report-hold.json")["state"] == "holding-after-complete"


@pytest.mark.parametrize(
    ("runner", "failure_fragment"),
    [
        (RecordedRunner(returncode=0, journal_run_id="some-other-run"), "has no entries"),
        (RecordedRunner(returncode=0, journal_entries=0), "has no entries"),
    ],
)
def test_a_journal_that_is_not_this_runs_or_is_empty_counts_as_missing(
    tmp_path: Path, runner: RecordedRunner, failure_fragment: str
) -> None:
    ws = _prepared(tmp_path)
    report = _run_with(ws, runner)

    assert report["state"] == "complete"
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


def test_a_transcript_that_dropped_its_middle_says_so_in_the_report(tmp_path: Path) -> None:
    """A bounded transcript is a stated partial record: complete, never read as whole."""

    ws = _prepared(tmp_path)
    report = _run_with(ws, RecordedRunner(returncode=0, dropped_bytes=4096))

    assert report["state"] == "complete"
    assert report["records_missing"] == []
    assert report["records_at_close"]["transcript"]["dropped_bytes"] == 4096


def test_the_real_runner_reports_the_bytes_its_transcript_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pod_run, "TRANSCRIPT_HEAD_BYTES", 16)
    monkeypatch.setattr(pod_run, "TRANSCRIPT_TAIL_BYTES", 8)
    program = "import sys; sys.stdout.write('x' * 100); sys.stdout.flush()"

    completed = pod_run._run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        transcript=tmp_path / "report-transcript.log",
        liveness=lambda pid, alive: None,
        interval_seconds=0.01,
    )

    assert completed.transcript_failure is None
    assert completed.transcript_dropped_bytes == 100 - 16 - 8


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


@pytest.mark.parametrize("later_entry", (False, True))
def test_a_torn_timing_line_is_skipped(tmp_path: Path, later_entry: bool) -> None:
    ws = _prepared(tmp_path)
    report = _run_with(ws, RecordedRunner(returncode=0, journal_entries=2))
    journal = Path(report["timing_journal_path"])
    torn = b'{"run_id":"first-real-run"'
    later = b'\n{"run_id":"first-real-run"}\n' if later_entry else b""
    journal.write_bytes(journal.read_bytes() + torn + later)
    plan = object.__new__(pod_run.RunPlan)
    object.__setattr__(plan, "report_path", ws.volume / "pod-run-report.json")
    object.__setattr__(plan, "run_id", "first-real-run")
    object.__setattr__(plan, "run_root", ws.volume / "runs")
    audit, missing = pod_run._records_at_close(plan)
    assert missing == []
    assert audit["timing_journal"]["entries"] == 2
    assert audit["timing_journal"]["unreadable_lines"] == 1 + later_entry


def test_a_deep_timing_line_is_skipped_without_losing_other_entries(tmp_path: Path) -> None:
    ws = _prepared(tmp_path)
    report = _run_with(ws, RecordedRunner(returncode=0, journal_entries=2))
    journal = Path(report["timing_journal_path"])
    with journal.open("ab") as handle:
        handle.write(b"[" * 1500 + b"0" + b"]" * 1500 + b"\n")
    plan = object.__new__(pod_run.RunPlan)
    object.__setattr__(plan, "report_path", ws.volume / "pod-run-report.json")
    object.__setattr__(plan, "run_id", "first-real-run")
    object.__setattr__(plan, "run_root", ws.volume / "runs")

    audit, missing = pod_run._records_at_close(plan)

    assert missing == []
    assert audit["timing_journal"]["entries"] == 2
    assert audit["timing_journal"]["unreadable_lines"] == 1


def test_real_timing_writer_and_reader_audit_mixed_and_damaged_lines(tmp_path: Path) -> None:
    report = tmp_path / "run-report.json"
    journal = pod_run.run_report_paths(report)[3]
    run_root = tmp_path / "runs"
    args = Namespace(
        stage_timing_journal=journal,
        run_id="our-run",
        run_root=run_root,
        repository_commit="a" * 40,
    )

    def write() -> None:
        orchestrator._record_stage_timing(
            args,
            program="door",
            started_at="start",
            finished_at="finish",
            duration_ms=1,
            exit_code=0,
            gpu_utilization=(None, "test"),
        )

    write()
    with journal.open("ab") as handle:
        handle.write(b'{"schema":\n42\n')
    args.run_id = "other-run"
    write()
    args.run_id = "our-run"
    args.run_root = tmp_path / "other-runs"
    write()
    args.run_root = run_root
    with journal.open("ab") as handle:
        handle.write(b'{"schema":"stage-timing-journal.v4"')
    write()  # Repairs the torn tail before appending the next complete line.

    plan = object.__new__(pod_run.RunPlan)
    object.__setattr__(plan, "report_path", report)
    object.__setattr__(plan, "run_id", "our-run")
    object.__setattr__(plan, "run_root", run_root)
    audit, missing = pod_run._records_at_close(plan)

    assert missing == ["transcript", "liveness"]
    timing = audit["timing_journal"]
    assert timing["entries"] == 2
    assert timing["foreign_lines"] == 2
    assert timing["unreadable_lines"] == 3


@pytest.mark.parametrize(
    "first", ('{"schema":"run.v1"}\n', '{"schema":"bootstrap-report.v1"}\n', "42\n")
)
def test_timing_writer_refuses_an_existing_foreign_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], first: str
) -> None:
    journal = tmp_path / "timings.json"
    journal.write_text(first, encoding="utf-8")
    args = Namespace(
        stage_timing_journal=journal,
        run_id="r",
        run_root=tmp_path / "runs",
        repository_commit="a" * 40,
    )

    orchestrator._record_stage_timing(
        args,
        program="door",
        started_at="start",
        finished_at="finish",
        duration_ms=1,
        exit_code=0,
        gpu_utilization=(None, "test"),
    )

    assert journal.read_text(encoding="utf-8") == first
    assert "could not be journaled" in capsys.readouterr().err


@pytest.mark.hostile_local
def test_timing_writer_does_not_follow_a_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "bootstrap.json"
    evidence.write_bytes(b"bootstrap evidence\n")
    journal = tmp_path / "timings.json"
    journal.symlink_to(evidence)
    args = Namespace(
        stage_timing_journal=journal,
        run_id="r",
        run_root=tmp_path / "runs",
        repository_commit="a" * 40,
    )

    orchestrator._record_stage_timing(
        args,
        program="door",
        started_at="start",
        finished_at="finish",
        duration_ms=1,
        exit_code=0,
        gpu_utilization=(None, "test"),
    )

    assert evidence.read_bytes() == b"bootstrap evidence\n"
    assert "could not be journaled" in capsys.readouterr().err


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
    assert "the transcript write failed part-way" in completed.transcript_failure
    assert "No space left on device" in completed.transcript_failure


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
    assert "still attached" in completed.transcript_failure
    assert "parent" in transcript.read_text(encoding="utf-8")


# --- a run holds its pod only while it shows progress -------------------------------


def _burn_cpu() -> None:
    """Use CPU time in this process until its counter visibly moves."""

    start = os.times()
    while os.times().user + os.times().system - start.user - start.system < 0.02:
        sum(range(1000))


def _stalling_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticks: list[str]
) -> tuple[list[float], list[list[str]], list[list[str]], dict]:
    """Drive one run whose orchestrator ticks once per five minutes doing what `ticks` says.

    On every tick an idle model server in the orchestrator's own process tree burns CPU
    and appends to its engine log in the run tree; "transcript" also adds stage output,
    and "artifact" also publishes a file in the run tree. Returns the minute of each
    keep-alive touch, the guard's progress line after each tick split into its fields,
    every notice argv and the final run report.
    """
    ws = _prepared(tmp_path)
    clock = Clock()
    _first_process(tmp_path, monkeypatch, "pod123")
    guard = ws.volume / pod_run.POD_GUARD_DIRECTORY
    guard.mkdir(parents=True, exist_ok=True)
    (guard / "ntfy_topic").write_text("guard-topic-for-the-test\n", encoding="utf-8")
    start = clock.now()
    touches: list[float] = []
    monkeypatch.setattr(
        pod_run,
        "_guard_keepalive",
        lambda volume, pod_id: lambda: touches.append((clock.now() - start).total_seconds() / 60),
    )
    tree = ws.volume / pod_run.DEFAULT_RUNS_DIRECTORY / "first-real-run"
    engine_log = tree / "4_perlector" / pod_run.SERVING_LOGS_DIR / "vllm-perlector.log"
    notify = NotifyRecorder()
    lines: list[list[str]] = []

    class Ticking(RecordedRunner):
        def __call__(self, argv, *, cwd, env, transcript, liveness, interval_seconds):  # type: ignore[no-untyped-def]
            for number, tick in enumerate(ticks):
                clock.sleep(300)
                _burn_cpu()
                engine_log.parent.mkdir(parents=True, exist_ok=True)
                with engine_log.open("a", encoding="utf-8") as log:
                    log.write("Avg prompt throughput: 0.0 tokens/s\n")
                if tick == "transcript":
                    with Path(transcript).open("a", encoding="utf-8") as out:
                        out.write(f"page {number} read\n")
                elif tick == "artifact":
                    page = tree / "4_perlector" / f"page-{number}"
                    page.mkdir(parents=True)
                    (page / "record.json").write_text("{}", encoding="utf-8")
                else:
                    assert tick == "idle"
                liveness(os.getpid(), True)
                lines.append((guard / "progress-pod123").read_text(encoding="ascii").split(" ", 4))
            return super().__call__(
                argv,
                cwd=cwd,
                env=env,
                transcript=transcript,
                liveness=liveness,
                interval_seconds=interval_seconds,
            )

    code = main(
        _run_argv(ws),
        environ=_environ(clock, lifetime=4.0, extra={pod_run.POD_ID_ENVIRONMENT: "pod123"}),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: PreflightedActions(),
        runner=Ticking(),
        notify_runner=notify.factory,
    )
    assert code == EXIT_COMPLETE
    return touches, lines, notify.calls, _report(ws)


def test_a_progressing_run_holds_its_pod_on_every_tick(tmp_path, monkeypatch) -> None:
    touches, lines, notices, _ = _stalling_run(
        tmp_path, monkeypatch, ["transcript", "artifact", "transcript", "artifact", "transcript"]
    )
    assert touches == [5, 10, 15, 20, 25]
    assert [line[2] for line in lines] == ["ok"] * 5
    assert all(line[0] == line[1] for line in lines), "last ok is now on every ok tick"
    assert notices == []


def test_an_idle_server_burning_cpu_does_not_hold_the_pod_past_the_quiet_window(
    tmp_path, monkeypatch
) -> None:
    assert pod_run.progress_watch.OUTPUT_QUIET_SECONDS == 15 * 60
    touches, lines, notices, _ = _stalling_run(tmp_path, monkeypatch, ["idle"] * 7)
    # Only the first tick, which finds the run tree new, is progress: held through
    # minute 15, stalled from minute 20 on, though the server burned CPU every tick.
    assert touches == [5, 10, 15]
    assert [line[2] for line in lines] == ["ok"] * 3 + ["stalled"] * 4
    started = int(lines[0][0]) - 300
    # The last ok stays at minute 15, so the guard's ladder runs from there.
    assert {int(line[1]) - started for line in lines[3:]} == {15 * 60}
    assert lines[-1][3] == "output"
    assert lines[-1][4].startswith("no new output or record for 1800 s")
    # The guard owns every notice about a stalled run; pod_run sends none.
    assert notices == []


def test_progress_after_a_stall_holds_the_pod_again(tmp_path, monkeypatch) -> None:
    touches, lines, _, _ = _stalling_run(
        tmp_path, monkeypatch, ["idle"] * 5 + ["artifact", "transcript"]
    )
    assert touches == [5, 10, 15, 30, 35]
    assert [line[2] for line in lines] == ["ok"] * 3 + ["stalled"] * 2 + ["ok"] * 2


def test_the_final_report_carries_the_progress_record(tmp_path, monkeypatch) -> None:
    *_, report = _stalling_run(tmp_path, monkeypatch, ["idle"] * 5)
    progress = json.loads(Path(report["progress_path"]).read_text(encoding="utf-8"))
    assert progress["schema"] == "pod-run-progress.v1"
    assert progress["status"] == "stalled" and progress["measure"] == "output"
    assert report["progress"]["status"] == "stalled"
    assert report["stage_rates"] == []


def test_the_run_tree_mark_moves_on_stage_writes_and_not_on_engine_logs(tmp_path: Path) -> None:
    assert pod_run.run_tree_mark(tmp_path / "absent") is None
    stage = tmp_path / "4_perlector"
    engine_log = stage / pod_run.SERVING_LOGS_DIR / "vllm-perlector.log"
    engine_log.parent.mkdir(parents=True)
    engine_log.write_text("starting\n", encoding="utf-8")
    before = pod_run.run_tree_mark(tmp_path)
    assert before is not None

    future = before + 10**9
    os.utime(engine_log.parent, ns=(future, future))
    assert pod_run.run_tree_mark(tmp_path) == before

    # A record published into a stage's directory moves that directory's time.
    records = stage / "artifacts" / "page-accounting"
    records.mkdir(parents=True)
    os.utime(records, ns=(future, future))
    assert pod_run.run_tree_mark(tmp_path) == future
    later = future + 10**9
    (records / "page-1.json").write_text("{}", encoding="utf-8")
    os.utime(records, ns=(later, later))
    assert pod_run.run_tree_mark(tmp_path) == later

    # Files are never stat'ed one by one.
    os.utime(records / "page-1.json", ns=(later + 10**9, later + 10**9))
    assert pod_run.run_tree_mark(tmp_path) == later


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_usage_of_both_halves_and_runs_nothing(flag: str) -> None:
    # The pod runs this from the checkout, whose root is then on the path; the gate's
    # PYTHONSAFEPATH keeps the working directory off it, so the root is named here.
    result = subprocess.run(
        [sys.executable, "-m", "operations.pod.pod_run", flag],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: python -m operations.pod.pod_run")
    assert "--run-id" in result.stdout and " -- " in result.stdout
    assert "refused" not in result.stdout + result.stderr


def _capacity_plan(tier: str = TIER):  # type: ignore[no-untyped-def]
    from decimal import Decimal

    from operations.serving.capacity import CapacityPlan, ChairCapacity
    from operations.serving.config import ServingConfigInputs

    return CapacityPlan(
        vram_gib=Decimal("47.99"),
        gpu_count=1,
        compute_capability="8.6",
        tier=tier,
        serving_config_inputs=ServingConfigInputs.from_record(SERVING_INPUTS),
        chairs={
            "attestator_2": ChairCapacity(
                recipe="unproven-real-attestatores",
                row_max_num_seqs=2,
                max_num_seqs=38,
                weights_gib=Decimal("15.5"),
                kv_gib_per_seq=Decimal("0.47"),
                memory_fraction=Decimal("0.78"),
            )
        },
    )


@dataclass
class _PlannedActions(PreflightedActions):
    """Green, and PREFLIGHT measured the card and published its capacity plan."""

    plan_record: dict | None = None

    def run_preflight(self) -> dict[str, object]:
        record = super().run_preflight()
        record["capacity_plan"] = self.plan_record
        return record


def _run_with_plan(tmp_path: Path, plan_record: dict | None):  # type: ignore[no-untyped-def]
    ws = _prepared(tmp_path)
    clock = Clock()
    runner = RecordedRunner()
    exit_code = main(
        _run_argv(ws),
        environ=_environ(clock),
        now=clock.now,
        sleeper=clock.sleep,
        actions_factory=lambda plan: _PlannedActions(plan_record=plan_record),
        runner=runner,
    )
    return exit_code, runner, _report(ws)


def test_pod_run_forwards_preflight_s_capacity_plan_to_the_orchestrator(tmp_path: Path) -> None:
    plan = _capacity_plan()
    exit_code, runner, report = _run_with_plan(tmp_path, plan.to_record())

    assert exit_code == EXIT_COMPLETE
    [(command, _cwd, _env)] = runner.calls
    assert command[command.index("--placement-tier") + 1] == TIER
    assert command[command.index("--capacity-plan") + 1] == plan.to_argument()
    assert report["capacity_plan"] == plan.to_record()


def test_without_a_plan_pod_run_forwards_none(tmp_path: Path) -> None:
    exit_code, runner, report = _run_with_plan(tmp_path, None)

    assert exit_code == EXIT_COMPLETE
    [(command, _cwd, _env)] = runner.calls
    assert "--capacity-plan" not in command
    assert report["capacity_plan"] is None


def test_a_changed_capacity_plan_is_refused_not_dropped(tmp_path: Path) -> None:
    record = _capacity_plan().to_record()
    record["chairs"]["attestator_2"]["max_num_seqs"] = 64
    exit_code, runner, report = _run_with_plan(tmp_path, record)

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert "capacity plan" in report["reason"]


def test_a_capacity_plan_for_another_tier_is_refused(tmp_path: Path) -> None:
    exit_code, runner, report = _run_with_plan(
        tmp_path, _capacity_plan("generic-80gb-plus").to_record()
    )

    assert exit_code == EXIT_REFUSED
    assert runner.calls == []
    assert "generic-80gb-plus" in report["reason"]
