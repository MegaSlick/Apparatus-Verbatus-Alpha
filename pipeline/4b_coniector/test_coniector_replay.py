"""The Coniector of a replay run: each call answered from its source run's recorded reply.

The source is `test_coniector`'s fixture run with its reconstructor served live by the
serving fakes. Its replay (`operations/replay/replay.py`) reads the pages again and
asks the Coniector again, and every call the source sent in the same bytes gets the
source's reply; a call it never sent is recorded not asked, `not-replayed`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_coniector import (
    CONIECTOR_PROGRAM,
    RUN_ID,
    TIER,
    _declared_answers,
    _live_tree,
    _run_live,
)

from common.contracts.errors import FatalAccounting
from common.contracts.stages import CONIECTOR
from common.reconstruction_records import (
    CALL_KIND,
    NOT_ASKED,
    NOT_REPLAYED,
    _require_not_asked_evidence,
    verified_reconstructions,
)
from common.replay import SOURCE_ENV
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, reading_acts
from conftest import load_stage, page_context, run_stage
from operations.replay import compare
from operations.replay import replay as replay_program
from operations.serving.assembly import SERVING_READER

REPLAY_ID = "r-replay"


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    from operations.serving.fakes import FakeEndpoint, ScriptedAnswer

    base = tmp_path_factory.mktemp("coniector-replay-source")
    root, options = _live_tree(base)
    endpoint = FakeEndpoint(served_model_id="served-reconstructor")
    endpoint.script(
        *(ScriptedAnswer(content=answer, finish_reason="stop") for answer in _declared_answers())
    )
    assert _run_live(base, root, options, endpoint) == EXIT_COMPLETE
    return root, options


def _replayed_through_the_perlector(source, tmp_path: Path) -> RunTree:
    root, options = source
    tree = replay_program.prepare(
        replay_program.source_tree(root / RUN_ID), tmp_path / "replays", REPLAY_ID, "0" * 40
    )
    result = run_stage(
        tree.root.parent, REPLAY_ID, "happy", "pipeline/4_perlector/run.py", **options
    )
    assert result.returncode == 0, result.stderr
    return tree


def _coniector(source, tree: RunTree, monkeypatch, stage=None) -> int:
    root, options = source
    monkeypatch.setenv(SOURCE_ENV, str(root / RUN_ID))
    stage = stage or load_stage("4b_coniector", "run")
    argv = [CONIECTOR_PROGRAM, "--run-root", str(tree.root.parent), "--run-id", REPLAY_ID]
    argv += ["--scenario", "happy"]
    for name, value in {**options, "placement_tier": TIER}.items():
        argv += [f"--{name.replace('_', '-')}", str(value)]
    monkeypatch.setattr(sys, "argv", argv)
    return stage.main()


def _calls(tree: RunTree) -> list[dict]:
    return [
        tree.read_artifact(CONIECTOR, CALL_KIND, entry["artifact_id"])["payload"]
        for entry in tree.build_manifest(CONIECTOR, verify_inputs=False)["artifacts"]
        if entry["kind"] == CALL_KIND
    ]


def test_every_recorded_call_is_answered_with_the_source_s_reply(source, tmp_path, monkeypatch):
    tree = _replayed_through_the_perlector(source, tmp_path)
    assert _coniector(source, tree, monkeypatch) == EXIT_COMPLETE
    root, options = source
    found = compare.compare_records(RunTree(root, RUN_ID), tree)[CONIECTOR]
    assert found["differing"] == []
    assert all(set(counts) == {"equal"} for counts in found["kinds"].values())
    assert found["blobs"]["source-only"] == found["blobs"]["replay-only"] == 0
    context = page_context(
        tree.root.parent,
        REPLAY_ID,
        "happy",
        {**options, "placement_tier": TIER},
        stage=CONIECTOR,
        serving_reader=SERVING_READER,
    )
    verified = verified_reconstructions(context, reading_acts(context))
    assert all(record["made"] for record in verified["acts"].values())


def test_a_call_its_source_never_sent_is_recorded_not_replayed(source, tmp_path, monkeypatch):
    tree = _replayed_through_the_perlector(source, tmp_path)
    stage = load_stage("4b_coniector", "run")
    original = stage.call_prompt
    # As code that would ask the Coniector in other words.
    monkeypatch.setattr(stage, "call_prompt", lambda *args: original(*args) + "\nchanged")
    assert _coniector(source, tree, monkeypatch, stage) == EXIT_COMPLETE
    calls = _calls(tree)
    assert calls and all(call["parse_state"] == NOT_ASKED for call in calls)
    assert {call["problems"][0]["code"] for call in calls} == {NOT_REPLAYED}
    assert all(call["maker"]["receipt_ref"] is None for call in calls)
    _root, options = source
    context = page_context(
        tree.root.parent, REPLAY_ID, "happy", {**options, "placement_tier": TIER}, stage=CONIECTOR
    )
    for call in calls:
        _require_not_asked_evidence(context, call, "the call")
    root, _options = source
    elsewhere = page_context(root, RUN_ID, "happy", options, stage=CONIECTOR)
    with pytest.raises(FatalAccounting, match="this run replays none"):
        _require_not_asked_evidence(elsewhere, calls[0], "the call")
