"""The Archetypus on a page-read run: every accepted reading, act or other, established once.

The trees are the fixture's `happy` and `page-review` scenarios read with
`reading_unit = "page"`. The Recensor's page path is stood in for by
`conftest.publish_stand_in_page_reviews`, which publishes the review shape the
downstream readers (`common/page_review.py`) read.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from common.contracts.canonical import self_hash
from common.contracts.stages import ARCHETYPUS
from common.exemplar_boundary import verify_reading_region_lineage
from common.runtree.store import RunTree
from conftest import (
    build_page_tree,
    load_stage,
    publish_stand_in_page_reviews,
    rewrite_page_answer_entry,
    run_stage,
)

RUN_ID = "r"
archetypus = load_stage("6_archetypus")


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> tuple[Path, Path]:
    return build_page_tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def page_review(tmp_path_factory) -> tuple[Path, Path]:
    return build_page_tree(tmp_path_factory.mktemp("page-review"), "page-review")


def _copy(tree: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    root, protocol = tree
    shutil.copytree(root, tmp_path / "runs")
    return tmp_path / "runs", protocol


def _establish(root: Path, protocol: Path, scenario: str, **outcomes: str):
    publish_stand_in_page_reviews(root, RUN_ID, scenario, protocol, outcomes=outcomes)
    return run_stage(
        root, RUN_ID, scenario, "pipeline/6_archetypus/run.py", perlector_protocol_config=protocol
    )


def _records(root: Path) -> dict[str, dict]:
    tree = RunTree(root, RUN_ID)
    return {
        record["payload"]["act_key"]: record
        for record in (
            tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
            for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
            if entry["kind"] == "archetypus"
        )
    }


def test_every_accepted_page_reading_is_established_from_its_own_act_region(happy, tmp_path):
    root, protocol = _copy(happy, tmp_path)
    rewrite_page_answer_entry(root, RUN_ID, 1, 1, kind="other")
    result = _establish(root, protocol, "happy")
    assert result.returncode == 0, result.stderr
    records = _records(root)
    assert sorted(records) == ["p1:1", "p1:2", "p2:1"]
    tree = RunTree(root, RUN_ID)
    for key, record in records.items():
        payload = archetypus.validate_record(record["payload"])
        assert payload["kind"] == ("other" if key == "p1:1" else "act")
        reading = tree.read_artifact_reference(
            payload["perlectio_ref"], stage="perlector", kind="perlectio"
        )
        assert payload["text"] == reading["payload"]["text"]
        region = tree.read_artifact_reference(
            reading["payload"]["act_region_ref"], stage="perlector", kind="act-region"
        )
        assert payload["regions"] == [verify_reading_region_lineage(tree, tree.read_run(), region)]
        assert reading["payload"]["act_region_ref"] in record["inputs"]
        # A page reader saw no Pass-A draft: its self-revisions were not measured.
        assert payload["uncertainty"]["self_revisions"] is None
    # The doubt mark in p1:1's answer travels as its uncertain span.
    assert records["p1:1"]["payload"]["uncertainty"]["uncertain_spans"]
    index = json.loads(tree.resolve(tree.index_path(ARCHETYPUS)).read_text(encoding="utf-8"))
    assert {row["act_key"] for row in index["rows"]} == {"p1:1", "p1:2", "p2:1"}


def test_a_held_reading_and_its_page_reach_no_record(page_review, tmp_path):
    root, protocol = _copy(page_review, tmp_path)
    result = _establish(root, protocol, "page-review")
    assert result.returncode == 0, result.stderr
    assert sorted(_records(root)) == ["p1:1", "p1:2"]


def test_the_recensor_accepting_a_held_reading_is_refused(page_review, tmp_path):
    root, protocol = _copy(page_review, tmp_path)
    result = _establish(root, protocol, "page-review", **{"p2:1": "accepted"})
    assert result.returncode == 2
    assert "may not resurrect a held reading" in result.stderr
    assert _records(root) == {}


def test_the_index_reconciles_with_the_recensors_accepted_readings_only(happy, tmp_path):
    root, protocol = _copy(happy, tmp_path)
    result = _establish(root, protocol, "happy", **{"p1:2": "held-for-review"})
    assert result.returncode == 0, result.stderr
    assert sorted(_records(root)) == ["p1:1", "p2:1"]
    tree = RunTree(root, RUN_ID)
    index = json.loads(tree.resolve(tree.index_path(ARCHETYPUS)).read_text(encoding="utf-8"))
    assert [row["act_key"] for row in sorted(index["rows"], key=lambda row: row["act_key"])] == [
        "p1:1",
        "p2:1",
    ]


def test_a_page_record_is_closed_on_its_own_region_fields(happy, tmp_path):
    root, protocol = _copy(happy, tmp_path)
    assert _establish(root, protocol, "happy").returncode == 0
    payload = _records(root)["p1:2"]["payload"]
    region = {**payload["regions"][0], "witness_covered": True}
    with pytest.raises(archetypus.SchemaRefusal, match="closed region schema"):
        archetypus.validate_record(_resealed(payload, regions=[region]))
    with pytest.raises(archetypus.SchemaRefusal, match="neither act nor other"):
        archetypus.validate_record(_resealed(payload, kind="stamp"))


def _resealed(payload: dict, **changes) -> dict:
    record = {key: value for key, value in payload.items() if key != "self_hash"}
    record.update(changes)
    record["self_hash"] = self_hash(record)
    return record
