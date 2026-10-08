"""Stage checkpoints copy local evidence to the volume without removing older files."""

from argparse import Namespace
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.runtree import sync
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


# --- the verified ledger ------------------------------------------------------------


def _trees(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, Path]:
    local = tmp_path / "local" / "run"
    volume = tmp_path / "volume" / "run"
    for relative, data in files.items():
        (local / relative).parent.mkdir(parents=True, exist_ok=True)
        (local / relative).write_bytes(data)
    volume.mkdir(parents=True)
    return local, volume


FILES = {
    f"stage-{stage}/artifacts/kind/art_{index}.json": f"{stage}:{index}".encode()
    for stage in range(3)
    for index in range(12)
}


def test_a_new_sync_of_the_same_trees_hashes_nothing_already_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local, volume = _trees(tmp_path, FILES)
    assert RunTreeSync(local, volume).sync() == len(FILES)
    assert not (volume / sync.LEDGER_NAME).exists(), "the ledger was copied as evidence"

    def no_hashing(path: Path) -> str:
        raise AssertionError(f"{path} was hashed again")

    monkeypatch.setattr(sync, "_digest", no_hashing)
    assert RunTreeSync(local, volume).sync() == 0


def test_every_file_is_copied_whole_and_each_directory_made_durable_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local, volume = _trees(tmp_path, FILES)
    durable: list[Path] = []
    real = sync._fsync_directory
    monkeypatch.setattr(sync, "_fsync_directory", lambda path: (durable.append(path), real(path)))

    RunTreeSync(local, volume).sync()

    for relative, data in FILES.items():
        assert (volume / relative).read_bytes() == data
    linked = [path for path in durable if path.name == "kind"]
    assert sorted(linked) == sorted({(volume / name).parent for name in FILES})
    assert not list(volume.rglob(f"{sync.SYNC_PREFIX}*"))


def test_a_ledger_for_another_target_or_a_missing_target_file_is_checked_again(
    tmp_path: Path,
) -> None:
    local, volume = _trees(tmp_path, FILES)
    RunTreeSync(local, volume).sync()
    gone = next(iter(FILES))
    (volume / gone).unlink()

    assert RunTreeSync(local, volume).sync() == 1
    assert (volume / gone).read_bytes() == FILES[gone]

    other = tmp_path / "other" / "run"
    other.mkdir(parents=True)
    assert RunTreeSync(local, other).sync() == len(FILES)


def test_a_local_file_changed_after_its_ledger_line_is_checked_against_the_volume(
    tmp_path: Path,
) -> None:
    local, volume = _trees(tmp_path, FILES)
    RunTreeSync(local, volume).sync()
    changed = next(iter(FILES))
    (local / changed).write_bytes(b"rewritten after the copy")

    with pytest.raises(RunTreeSyncError, match="differs"):
        RunTreeSync(local, volume).sync()
    assert (volume / changed).read_bytes() == FILES[changed]


def test_an_unreadable_ledger_is_started_again(tmp_path: Path) -> None:
    local, volume = _trees(tmp_path, FILES)
    RunTreeSync(local, volume).sync()
    (local / sync.LEDGER_NAME).write_bytes(b"\xff not a ledger")

    assert RunTreeSync(local, volume).sync() == 0
    assert RunTreeSync(local, volume).sync() == 0
