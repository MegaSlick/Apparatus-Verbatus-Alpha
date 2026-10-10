"""A replay of a saved run's Perlector from its recorded replies, as a new run, offline.

A live fixture run is read once against the serving fakes (`test_page_reading`); a
replay of it then reads every page again with this checkout's code, answering each
call from the reply the source retained (`common/replay.py`,
`operations/serving/replay.py`, `operations/replay/replay.py`). Nothing here starts
a model or reaches a network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import page_run
import pytest
from test_live_perlector import TIER
from test_page_reading import (
    MODELS,
    PERLECTOR_PROGRAM,
    ROOT,
    _live_chain,
    _read_pages,
    perlector,
)
from test_page_reask import _first_act_only, _page_two, _recovered

from common import page_path, page_reask
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.replay import NOT_REPLAYED, SOURCE_ENV, replay_block
from common.runtree.store import RunTree
from conftest import file_bytes_snapshot
from operations.replay import compare
from operations.replay import replay as replay_program

COMMIT = "0" * 40
REPLAY_ID = "r-replay"


def _source(base: Path, *answers, plan_reasks: bool = True):
    """A live fixture run read through its Perlector with `answers`, sealed."""
    live = _live_chain(base)
    with pytest.MonkeyPatch.context() as patch:
        if not plan_reasks:
            # As code that planned no re-ask would have read it.
            patch.setattr(page_reask, "reask_plan", lambda *args, **kwargs: [])
        _endpoint, exit_code = _read_pages(live, base / "serving", patch, *answers)
    assert exit_code == 0
    return live


@pytest.fixture(scope="module")
def reasked(tmp_path_factory):
    """Page 1 read with an act left out and re-asked; page 2 read once."""
    return _source(
        tmp_path_factory.mktemp("replay-source"), _first_act_only(), _recovered(), _page_two()
    )


@pytest.fixture(scope="module")
def never_reasked(tmp_path_factory):
    """The same first answers read by code that planned no re-ask."""
    return _source(
        tmp_path_factory.mktemp("replay-old-code"),
        _first_act_only(),
        _page_two(),
        plan_reasks=False,
    )


def _prepared(live, run_root: Path) -> RunTree:
    source = replay_program.source_tree(live.root / "r")
    return replay_program.prepare(source, run_root, REPLAY_ID, COMMIT)


def _replay_perlector(live, tree: RunTree, monkeypatch) -> int:
    """The Perlector over the replay run, in this process, answered from the source."""
    monkeypatch.setenv(SOURCE_ENV, str(live.root / "r"))
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / PERLECTOR_PROGRAM),
            "--run-root",
            str(tree.root.parent),
            "--run-id",
            tree.run_id,
            "--scenario",
            live.scenario,
            "--serving-recipes-config",
            str(live.catalogue),
            "--placement-tier",
            TIER,
            "--perlector-protocol-config",
            str(live.protocol),
            "--recovery-config",
            str(live.recovery),
        ],
    )
    return perlector.main()


def _orchestrator_arguments(live) -> list[str]:
    return [
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        live.scenario,
        "--models-config",
        str(MODELS),
        "--serving-recipes-config",
        str(live.catalogue),
        "--placement-tier",
        TIER,
        "--perlector-protocol-config",
        str(live.protocol),
        "--recovery-config",
        str(live.recovery),
    ]


def _reading(tree: RunTree, ordinal: int, attempt: int) -> dict | None:
    for entry in tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] != "page-reading":
            continue
        record = tree.read_artifact(PERLECTOR, "page-reading", entry["artifact_id"])
        payload = record["payload"]
        if (payload["page_ordinal"], payload["attempt_ordinal"]) == (ordinal, attempt):
            return record
    return None


def test_a_replay_with_unchanged_code_derives_every_perlector_record_exactly(
    reasked, tmp_path, monkeypatch
):
    before = file_bytes_snapshot(reasked.root)
    tree = _prepared(reasked, tmp_path / "replays")
    assert _replay_perlector(reasked, tree, monkeypatch) == 0

    run = tree.read_run()
    source_run = RunTree(reasked.root, "r").read_run()
    assert run["replay"] == replay_block(source_run)
    assert run["repository_commit"] == COMMIT
    assert run["config_digest"] == source_run["config_digest"]
    source = RunTree(reasked.root, "r")
    found = compare.compare_records(source, tree)[PERLECTOR]
    assert found["differing"] == []
    assert {kind: set(counts) for kind, counts in found["kinds"].items()} == {
        kind: {"equal"} for kind in found["kinds"]
    }
    # Both readings of page 1, the re-ask among them, were answered from the record.
    assert _reading(tree, 1, 2)["payload"]["disposition"] == "read"
    # Every reply, call record, crop and the serving evidence are the source's own bytes.
    assert found["blobs"]["source-only"] == found["blobs"]["replay-only"] == 0
    assert file_bytes_snapshot(reasked.root) == before


def test_a_re_ask_its_source_never_sent_is_recorded_not_replayed_and_the_recensor_holds_it(
    never_reasked, tmp_path
):
    """The replay plans a re-ask the source's code did not; no recorded reply answers it."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "operations" / "replay" / "replay.py"),
            "--source",
            str(never_reasked.root / "r"),
            "--run-root",
            str(tmp_path / "replays"),
            "--run-id",
            REPLAY_ID,
            "--to",
            "recensor",
            "--repository-commit",
            COMMIT,
            "--",
            *_orchestrator_arguments(never_reasked),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    tree = RunTree(tmp_path / "replays", REPLAY_ID)
    second = _reading(tree, 1, 2)["payload"]
    assert (second["parse_state"], second["disposition"]) == (page_path.NOT_RUN, "held")
    assert [problem["code"] for problem in second["problems"]] == [NOT_REPLAYED]
    assert second["request_digest"] is None and second["engine_call"] is None
    assert second["provenance"]["receipt_ref"] is None
    holds = compare.holds(tree)
    assert holds["held_pages"] >= 1
    assert 1 not in holds["clean_pages"]


def test_a_replay_refuses_a_first_reading_its_source_never_sent(reasked, tmp_path, monkeypatch):
    tree = _prepared(reasked, tmp_path / "replays")
    original = page_path.request_digest
    # As code that would ask the page in other words.
    monkeypatch.setattr(
        page_path, "request_digest", lambda text, images: original(text + "\nchanged", images)
    )
    with pytest.raises(ContractError, match="asks the Perlector something new"):
        _replay_perlector(reasked, tree, monkeypatch)
    assert _reading(tree, 1, 1) is None


def test_a_replay_run_reads_nothing_without_its_named_source(reasked, tmp_path, monkeypatch):
    tree = _prepared(reasked, tmp_path / "replays")
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    with pytest.raises(ContractError, match=SOURCE_ENV):
        page_run.replay.open_source(tree.read_run())
    monkeypatch.setenv(SOURCE_ENV, str(tree.root))
    with pytest.raises(ContractError, match="not run r's directory"):
        page_run.replay.open_source(tree.read_run())
    # Another run under the same name: sealed with no re-ask budget, so another authority.
    other = _live_chain(tmp_path / "other", reask=0)
    monkeypatch.setenv(SOURCE_ENV, str(other.root / "r"))
    with pytest.raises(ContractError, match="not the run this replay names"):
        page_run.replay.open_source(tree.read_run())


def test_a_replay_never_runs_a_stage_it_imported(reasked, tmp_path):
    tree = _prepared(reasked, tmp_path / "replays")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "pipeline" / "3_attestatores" / "run.py"),
            "--run-root",
            str(tree.root.parent),
            "--run-id",
            tree.run_id,
            "--serving-recipes-config",
            str(reasked.catalogue),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "imported as sealed; the stage is never run here" in result.stderr


def test_a_replay_run_holds_its_source_s_records_for_the_imported_stages_alone(reasked, tmp_path):
    tree = _prepared(reasked, tmp_path / "replays")
    assert tree.holds_run_id("r", "attestatores") and tree.holds_run_id("r", "door")
    assert tree.holds_run_id(REPLAY_ID, PERLECTOR)
    assert not tree.holds_run_id("r", PERLECTOR)
    assert not tree.holds_run_id("elsewhere", "attestatores")
    assert not RunTree(reasked.root, "r").holds_run_id(REPLAY_ID, "attestatores")
    # A record of a replayed stage carrying the source's id does not belong here.
    source = RunTree(reasked.root, "r")
    entry = next(
        entry
        for entry in source.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]
        if entry["kind"] == "page-feed"
    )
    planted = tree.root / entry["relative_path"]
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes((source.root / entry["relative_path"]).read_bytes())
    with pytest.raises(SchemaRefusal, match="artifact belongs to run 'r'"):
        tree.build_manifest(PERLECTOR, verify_inputs=False)


def test_a_replay_is_always_a_new_run(reasked, tmp_path):
    source = RunTree(reasked.root, "r")
    authority = source.read_run()
    block = replay_block(authority)
    with pytest.raises(SchemaRefusal, match="run id of its own"):
        RunTree.create_replay(
            tmp_path, "r", source=authority, replay=block, repository_commit=COMMIT
        )
    tree = _prepared(reasked, tmp_path / "replays")
    with pytest.raises(IncompatibleReuse, match="never into one"):
        RunTree.create_replay(
            tree.root.parent, REPLAY_ID, source=authority, replay=block, repository_commit=COMMIT
        )
    with pytest.raises(SchemaRefusal, match="itself a replay"):
        replayed = tree.read_run()
        RunTree.create_replay(
            tmp_path / "again",
            "r-twice",
            source=replayed,
            replay={
                **block,
                "source_run_id": REPLAY_ID,
                "source_run_sha256": replayed["self_hash"],
            },
            repository_commit=COMMIT,
        )
    with pytest.raises(ContractError, match="chosen by the replay itself"):
        replay_program.orchestrate(source, tree, "recensor", ["--run-id", "x"])


def test_the_comparison_leaves_out_only_the_digests_of_the_run_s_own_records():
    record = {
        "run_id": "a",
        "self_hash": "x",
        "inputs": [
            {"relative_path": "4_perlector/artifacts/page-feed/art_1.json", "sha256": "1"},
            {"relative_path": "1_exemplar/artifacts/page/art_2.json", "sha256": "2"},
            {"relative_path": "4_perlector/blobs/sha256/3", "sha256": "3"},
        ],
        "payload": {"text": "same"},
    }
    other = json.loads(json.dumps(record))
    other["run_id"], other["self_hash"] = "b", "y"
    other["inputs"][0]["sha256"] = "changed"
    assert compare._comparable(record) == compare._comparable(other)
    for index in (1, 2):
        changed = json.loads(json.dumps(other))
        changed["inputs"][index]["sha256"] = "changed"
        assert compare._comparable(record) != compare._comparable(changed)
