"""Stage checkpoints copy local evidence to the volume without removing older files."""

import json
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.runtree import sync
from common.runtree.sync import RunTreeSync, RunTreeSyncError
from pipeline.orchestrator import run as orchestrator


def test_each_stage_syncs_beside_the_next_and_every_sync_ends_before_the_run_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage's sync is planned at its boundary and copied while the next stage runs;
    it is joined before the next sync starts and before the invocation returns. The
    volume never loses a file it held."""
    local = tmp_path / "local" / "run"
    volume = tmp_path / "volume" / "run"
    local.mkdir(parents=True)
    volume.mkdir(parents=True)
    (volume / "older-evidence.json").write_bytes(b"retained")
    seen: list[str] = []
    copying = threading.Event()
    release = threading.Event()
    copy = RunTreeSync.copy

    def held_copy(self, plan):
        copying.set()
        assert release.wait(timeout=10)
        return copy(self, plan)

    def invoke(program: str, args: Namespace, **_options) -> int:
        if seen:
            # The first stage's sync is still copying while this stage runs.
            assert copying.wait(timeout=10)
            assert not (volume / "first.json").exists()
            release.set()
        name = "first.json" if not seen else "second.json"
        (local / name).write_bytes(name.removesuffix(".json").encode())
        seen.append(program)
        return orchestrator.EXIT_COMPLETE

    monkeypatch.setattr(RunTreeSync, "copy", held_copy)
    monkeypatch.setattr(orchestrator, "invoke", invoke)
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *args: None)
    journal = tmp_path / "timings.jsonl"
    args = Namespace(
        run_id="run",
        run_root=local.parent,
        stage_sync=RunTreeSync(local, volume),
        stage_timing_journal=journal,
        repository_commit=None,
    )

    result, exported = orchestrator._drive(args, (orchestrator.INK_MAP, "designator"), "semi", {})

    assert result == orchestrator.EXIT_COMPLETE
    assert exported is False
    assert len(seen) == 2
    assert (volume / "first.json").read_bytes() == b"first"
    assert (volume / "second.json").read_bytes() == b"second"
    assert (volume / "older-evidence.json").read_bytes() == b"retained"
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [(line["stage"], line["kind"], line["exit_code"]) for line in lines] == [
        (f"volume sync after {orchestrator.INK_MAP}", "volume-sync", 0),
        ("volume sync after designator", "volume-sync", 0),
    ]
    assert [line["files_copied"] for line in lines] == [1, 1]


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


def test_a_failed_sync_stops_the_run_at_the_next_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync after the first stage fails while the second runs; the run stops when
    that stage ends, and no third stage starts."""
    local = tmp_path / "local" / "run"
    volume = tmp_path / "volume" / "run"
    local.mkdir(parents=True)
    volume.mkdir(parents=True)
    (volume / "record.json").write_bytes(b"older")
    invoked: list[str] = []

    def invoke(program: str, args: Namespace, **_options) -> int:
        invoked.append(program)
        if len(invoked) == 1:
            (local / "record.json").write_bytes(b"new")
        return orchestrator.EXIT_COMPLETE

    monkeypatch.setattr(orchestrator, "invoke", invoke)
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *args: None)
    args = Namespace(run_id="run", run_root=local.parent, stage_sync=RunTreeSync(local, volume))

    with pytest.raises(ContractError, match=f"{orchestrator.INK_MAP} finished, but its volume"):
        orchestrator._drive(args, (orchestrator.INK_MAP, "designator", "attestatores"), "semi", {})

    assert len(invoked) == 2
    assert (volume / "record.json").read_bytes() == b"older"


def test_a_failed_stage_still_waits_for_the_sync_running_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first stage's sync is held mid-copy while the second stage fails; `_drive`
    does not return until the copy is released, then raises the stage's failure."""
    local = tmp_path / "local" / "run"
    volume = tmp_path / "volume" / "run"
    local.mkdir(parents=True)
    volume.mkdir(parents=True)
    copying = threading.Event()
    release = threading.Event()
    stage_failed = threading.Event()
    copy = RunTreeSync.copy

    def held_copy(self, plan):
        copying.set()
        assert release.wait(timeout=10)
        return copy(self, plan)

    def invoke(program: str, args: Namespace, **_options) -> int:
        if (local / "first.json").exists():
            assert copying.wait(timeout=10)
            stage_failed.set()
            raise ContractError(f"{program} exited 1")
        (local / "first.json").write_bytes(b"first")
        return orchestrator.EXIT_COMPLETE

    monkeypatch.setattr(RunTreeSync, "copy", held_copy)
    monkeypatch.setattr(orchestrator, "invoke", invoke)
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *args: None)
    args = Namespace(run_id="run", run_root=local.parent, stage_sync=RunTreeSync(local, volume))
    raised: list[BaseException] = []

    def drive() -> None:
        try:
            orchestrator._drive(args, (orchestrator.INK_MAP, "designator"), "semi", {})
        except BaseException as error:  # noqa: BLE001 -- examined below
            raised.append(error)

    driver = threading.Thread(target=drive)
    driver.start()
    try:
        assert stage_failed.wait(timeout=10)
        driver.join(timeout=0.5)
        assert driver.is_alive(), "_drive returned while the sync was still copying"
        assert not (volume / "first.json").exists()
    finally:
        release.set()
        driver.join(timeout=10)
    assert not driver.is_alive()
    assert len(raised) == 1 and "exited 1" in str(raised[0])
    assert (volume / "first.json").read_bytes() == b"first"


def test_a_plan_names_only_the_files_present_at_the_boundary(tmp_path: Path) -> None:
    local, volume = _trees(tmp_path, {"a.json": b"a"})
    sync_object = RunTreeSync(local, volume)
    plan = sync_object.plan()
    (local / "b.json").write_bytes(b"b")

    assert sync_object.copy(plan) == 1
    assert not (volume / "b.json").exists()
    assert sync_object.sync() == 1
    assert (volume / "b.json").read_bytes() == b"b"


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


def test_an_unreadable_ledger_is_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local, volume = _trees(tmp_path, FILES)
    RunTreeSync(local, volume).sync()
    (local / sync.LEDGER_NAME).write_bytes(b"\xff not a ledger")

    assert RunTreeSync(local, volume).sync() == 0

    def no_hashing(path: Path) -> str:
        raise AssertionError(f"{path} was hashed again: the ledger was not rewritten")

    monkeypatch.setattr(sync, "_digest", no_hashing)
    assert RunTreeSync(local, volume).sync() == 0


@pytest.mark.parametrize("change", ["rewritten", "removed"])
def test_a_verified_file_changed_after_the_plan_is_refused_not_skipped(
    tmp_path: Path, change: str
) -> None:
    """A file the ledger already verified is skipped only while its source is unchanged:
    a stage that rewrites or removes it after the boundary fails the sync, rather than
    the volume keeping the old bytes under a sync that reported success."""
    local, volume = _trees(tmp_path, {"a.json": b"a"})
    sync_object = RunTreeSync(local, volume)
    assert sync_object.sync() == 1
    plan = sync_object.plan()
    if change == "rewritten":
        (local / "a.json").write_bytes(b"a changed")
    else:
        (local / "a.json").unlink()

    with pytest.raises(RunTreeSyncError, match="changed during copy|gone since the plan"):
        sync_object.copy(plan)
    assert (volume / "a.json").read_bytes() == b"a"
