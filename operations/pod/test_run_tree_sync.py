"""Stage checkpoints copy local evidence to the volume without removing older files."""

from argparse import Namespace
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.runtree.sync import RunTreeSync, RunTreeSyncError
from pipeline.orchestrator import run as orchestrator


def test_fake_run_syncs_before_each_next_stage_and_never_deletes_volume_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local" / "run"
    volume = tmp_path / "volume" / "run"
    local.mkdir(parents=True)
    volume.mkdir(parents=True)
    (volume / "older-evidence.json").write_bytes(b"retained")
    seen: list[str] = []

    def invoke(program: str, args: Namespace) -> int:
        if seen:
            assert (volume / "first.json").read_bytes() == b"first"
        name = "first.json" if not seen else "second.json"
        (local / name).write_bytes(name.removesuffix(".json").encode())
        seen.append(program)
        return orchestrator.EXIT_COMPLETE

    monkeypatch.setattr(orchestrator, "invoke", invoke)
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *args: None)
    args = Namespace(run_id="run", run_root=local.parent, stage_sync=RunTreeSync(local, volume))

    result, exported = orchestrator._drive(args, (orchestrator.INK_MAP, "designator"), "semi", {})

    assert result == orchestrator.EXIT_COMPLETE
    assert exported is False
    assert len(seen) == 2
    assert (volume / "first.json").read_bytes() == b"first"
    assert (volume / "second.json").read_bytes() == b"second"
    assert (volume / "older-evidence.json").read_bytes() == b"retained"


def test_a_changed_volume_file_refuses_the_stage_checkpoint(tmp_path: Path) -> None:
    local = tmp_path / "local"
    volume = tmp_path / "volume"
    local.mkdir()
    volume.mkdir()
    (local / "record.json").write_bytes(b"new")
    (volume / "record.json").write_bytes(b"older")

    with pytest.raises(RunTreeSyncError, match="differs"):
        RunTreeSync(local, volume).sync()

    assert (volume / "record.json").read_bytes() == b"older"


def test_a_failed_stage_checkpoint_stops_before_the_next_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local" / "run"
    volume = tmp_path / "volume" / "run"
    local.mkdir(parents=True)
    volume.mkdir(parents=True)
    (volume / "record.json").write_bytes(b"older")
    invoked: list[str] = []

    def invoke(program: str, args: Namespace) -> int:
        invoked.append(program)
        (local / "record.json").write_bytes(b"new")
        return orchestrator.EXIT_COMPLETE

    monkeypatch.setattr(orchestrator, "invoke", invoke)
    args = Namespace(run_id="run", run_root=local.parent, stage_sync=RunTreeSync(local, volume))

    with pytest.raises(ContractError, match="volume sync failed"):
        orchestrator._drive(args, (orchestrator.INK_MAP, "designator"), "semi", {})

    assert len(invoked) == 1
    assert (volume / "record.json").read_bytes() == b"older"
