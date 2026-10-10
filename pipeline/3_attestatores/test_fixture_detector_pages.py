"""DAI read record by record on the fixture pass, as a served DAI is read.

The runs use the committed roster: DAI (`attestator_2`) page-scoped and its
record detector on a fixture row, which
answers from the fixture's `[[detector_record]]` rows. DAI's answer to each
record is the fixture's `[[dai_record_response]]` row for it; everything else
-- the crop it is shown, the closed model view, the retained capture -- is
built as for a served record.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.envelope import read_verified
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR
from common.runtree.store import RunTree
from conftest import (
    file_bytes_snapshot,
    load_stage,
    programs_through,
    run_stage,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r"
DAI = "attestator_2"
ATTESTATORES_PROGRAM = "pipeline/3_attestatores/run.py"
attestatores = load_stage("3_attestatores")
FIXTURE = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text(encoding="utf-8"))


def _records(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def _dai_pages(tree: RunTree) -> dict[int, dict]:
    return {
        record["payload"]["page_ordinal"]: record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI
    }


def _through_designator(base: Path) -> tuple[Path, dict]:
    options: dict[str, object] = {}
    root = base / "runs"
    for program in programs_through("designator"):
        result = run_stage(root, RUN_ID, "happy", program, **options)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return root, options


@pytest.fixture(scope="module")
def witnessed(tmp_path_factory) -> tuple[Path, dict]:
    root, options = _through_designator(tmp_path_factory.mktemp("fixture-dai"))
    result = run_stage(root, RUN_ID, "happy", ATTESTATORES_PROGRAM, **options)
    assert result.returncode == 0, result.stderr
    return root, options


def test_dai_reads_each_detector_record_on_its_page_with_its_declared_answer(witnessed):
    root, _options = witnessed
    tree = RunTree(root, RUN_ID)
    census = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _records(tree, DESIGNATOR, "detector-page")
    }
    regions = {
        record["subject_id"]: record["payload"]
        for record in _records(tree, DESIGNATOR, "detector-region")
    }
    answers = {
        (row["page_ordinal"], row["detector_ordinal"]): row["text"]
        for row in FIXTURE["dai_record_response"]
        if "scenario" not in row
    }
    pages = _dai_pages(tree)
    assert set(pages) == {1, 2}
    for ordinal, record in pages.items():
        payload = record["payload"]
        assert record["outcome"] == "read"
        crops = [regions[subject] for subject in census[ordinal]["record_subjects"]]
        # One image per record, each an adapter crop of that record's own bounds.
        assert [shown["transform"]["bounds"] for shown in payload["presentations"]] == [
            crop["transform"]["bounds"] for crop in crops
        ]
        assert [item["bounds"] for item in payload["observed"]] == [
            crop["transform"]["bounds"] for crop in crops
        ]
        assert {item["bounds_source"] for item in payload["observed"]} == {"presented"}
        # Each unit's retained capture is DAI's own view under its `text` parser,
        # retained under the fixture's declared stop word, and its bytes are the
        # declared answer.
        texts = []
        for capture in payload["unit_captures"]:
            assert capture["adapter"] == "dai.v1"
            assert capture["view"]["adapter"] == "dai-atr.v2"
            assert capture["transport_stop_reason"] == "fixture-complete"
            assert capture["parse"]["parser"] == "text"
            raw = read_verified(tree.read_bytes, capture["raw_response_ref"], "a DAI response")
            texts.append(raw.decode("utf-8"))
        assert texts == [answers[(ordinal, n)] for n in range(len(crops))]
        receipt = tree.read_run_receipt(payload["provenance"]["receipt_ref"])
        assert receipt["endpoint"].startswith("fixture://")


def test_each_sealed_page_holds_one_dai_record_and_no_act_view(witnessed):
    root, _options = witnessed
    tree = RunTree(root, RUN_ID)
    kinds = {entry["kind"] for entry in tree.build_manifest(ATTESTATORES)["artifacts"]}
    assert "testimonium" not in kinds and "act-attachment" not in kinds
    pages = [
        record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI
    ]
    assert sorted(record["payload"]["page_ordinal"] for record in pages) == [1, 2]


def test_a_second_pass_over_a_sealed_page_repeats_nothing(witnessed, tmp_path):
    root, options = witnessed
    copy = tmp_path / "runs"
    shutil.copytree(root, copy)
    before = file_bytes_snapshot(copy / RUN_ID / "3_attestatores" / "artifacts")
    result = run_stage(copy, RUN_ID, "happy", ATTESTATORES_PROGRAM, **options)
    assert result.returncode == 0, result.stderr
    assert file_bytes_snapshot(copy / RUN_ID / "3_attestatores" / "artifacts") == before


def _declared(rows: list[dict], scenario: str = "happy") -> SimpleNamespace:
    return SimpleNamespace(fixture={"dai_record_response": rows}, scenario=scenario)


_ROW = {"page_ordinal": 1, "detector_ordinal": 0, "chair": DAI, "text": "SYNTHETIC"}


def test_a_record_needs_exactly_one_declared_answer_and_a_scenario_answer_replaces_it():
    read = attestatores.declared_dai_record_text
    assert read(_declared([_ROW]), DAI, 1, 0) == "SYNTHETIC"
    scoped = {**_ROW, "scenario": "happy", "text": "SCOPED"}
    assert read(_declared([_ROW, scoped]), DAI, 1, 0) == "SCOPED"
    assert read(_declared([_ROW, scoped], "review"), DAI, 1, 0) == "SYNTHETIC"
    for rows, where in (([], (1, 0)), ([_ROW, dict(_ROW)], (1, 0)), ([_ROW], (2, 0))):
        with pytest.raises(SchemaRefusal, match="a record DAI is shown needs exactly one"):
            read(_declared(rows), DAI, *where)
    with pytest.raises(SchemaRefusal, match="declares fields"):
        read(_declared([{**_ROW, "stop": "length"}]), DAI, 1, 0)
    with pytest.raises(SchemaRefusal, match="text is not text"):
        read(_declared([{**_ROW, "text": 7}]), DAI, 1, 0)
