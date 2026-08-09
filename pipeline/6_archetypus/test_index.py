"""Spec 10, test 6: `index.json` reconciles 1:1 with the acts the Recensor
accepted; a missing or duplicate row is FATAL.

`index.json` is a rebuildable summary derived from the immutable per-act
records — spec 01's artifact/manifest split, never the only evidence — exactly
as `manifest.json` is. End-to-end tests exercise the real CLI; direct tests call
`build_index` and `validate_index` themselves, because a single invocation of the
stage cannot actually produce a divergence for the consumer check to catch, and
"nothing can go wrong here today" is not the same claim as "this refuses it".

The reconciliation target is deliberately the *Recensor's* accepted set,
recomputed from the immutable review records, and not the list of acts this
invocation happened to establish. An index checked only against the writer's own
list would agree with itself about an act the writer had skipped.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ARCHETYPUS
from common.runtree.store import RunTree
from common.stage import load_fixture

ROOT = Path(__file__).resolve().parents[2]


def _load_archetypus():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("archetypus_run_under_test_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archetypus = _load_archetypus()


class _Context:
    """Just enough of a stage context to call the index functions directly.

    `build_index` and `validate_index` read the tree; `accepted_act_ids` goes
    through `expected_acts`, which binds the seal's act denominator back to the
    fixture the run was sealed with and re-derives its region references. Both
    are reproduced here rather than mocked away, so these tests reconcile against
    the same evidence the real stage does.
    """

    def __init__(self, tree: RunTree):
        self.tree = tree
        self.fixture = load_fixture(str(ROOT / "proof"))

    def input_ref(self, relative_path: str) -> dict[str, str]:
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }

    def artifact_ref(self, stage: str, kind: str, identity: str) -> dict[str, str]:
        return self.input_ref(self.tree.artifact_path(stage, kind, identity))


def orchestrate(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def invoke_archetypus(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/6_archetypus/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _index(tree: RunTree) -> dict:
    return json.loads(tree.resolve(tree.index_path(ARCHETYPUS)).read_text(encoding="utf-8"))


def test_index_reconciles_1_to_1_with_both_established_acts_in_the_happy_scenario(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    index = _index(tree)

    established = {
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert established == {row["act_id"] for row in index["rows"]}
    assert index["accepted_count"] == len(index["rows"]) == 2
    assert index["rows"] == sorted(index["rows"], key=lambda row: row["act_id"])
    assert index["stage"] == ARCHETYPUS


def test_every_index_row_carries_its_records_status_and_text_hash(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    for row in _index(tree)["rows"]:
        record = tree.read_artifact(ARCHETYPUS, "archetypus", row["artifact_id"])["payload"]
        assert row["text_status"] == record["text_status"]
        assert row["text_hash"] == record["text_hash"]
        assert row["act_key"] == record["act_key"]


def test_the_held_act_never_appears_in_the_index(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "review").returncode == 3
    tree = RunTree(root, "r")
    index = _index(tree)

    established = {
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert len(established) == 1
    assert {row["act_id"] for row in index["rows"]} == established
    assert index["accepted_count"] == 1


def test_the_index_self_hash_verifies(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    assert verify_self_hash(_index(RunTree(root, "r")))


def test_deleting_and_rerunning_rebuilds_the_index_identically(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    path = tree.resolve(tree.index_path(ARCHETYPUS))
    original = path.read_bytes()

    path.unlink()
    assert not path.exists()

    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == original


def test_validate_index_refuses_a_missing_row(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    context = _Context(tree)

    short = archetypus.build_index(context)
    short["rows"] = short["rows"][:-1]
    short["accepted_count"] = len(short["rows"])
    short["self_hash"] = self_hash(short)
    with pytest.raises(FatalAccounting, match="do not reconcile 1:1"):
        archetypus.validate_index(context, short)


def test_validate_index_refuses_a_duplicate_row(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    context = _Context(tree)

    doubled = archetypus.build_index(context)
    doubled["rows"] = doubled["rows"] + [dict(doubled["rows"][0])]
    doubled["accepted_count"] = len(doubled["rows"])
    doubled["self_hash"] = self_hash(doubled)
    with pytest.raises(FatalAccounting, match="duplicate row"):
        archetypus.validate_index(context, doubled)


def test_validate_index_refuses_a_row_that_disagrees_with_its_record(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    context = _Context(tree)

    edited = archetypus.build_index(context)
    edited["rows"][0]["text_status"] = "no_readable_text"
    edited["self_hash"] = self_hash(edited)
    with pytest.raises(FatalAccounting, match="does not match its immutable record"):
        archetypus.validate_index(context, edited)


def test_validate_index_refuses_an_index_whose_self_hash_was_not_recomputed(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    context = _Context(tree)

    edited = archetypus.build_index(context)
    edited["accepted_count"] = 99
    with pytest.raises(FatalAccounting, match="self-hash"):
        archetypus.validate_index(context, edited)


def test_a_duplicate_record_on_disk_is_fatal_before_an_index_can_paper_over_it(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")

    original_entry = next(
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    original = tree.read_artifact(ARCHETYPUS, "archetypus", original_entry["artifact_id"])
    act_id = original["subject_id"]

    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "establish", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(ARCHETYPUS, "archetypus", act_id, forged_attempt)
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(tree.artifact_path(ARCHETYPUS, "archetypus", forged["artifact_id"]))
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))

    with pytest.raises(FatalAccounting, match="more than one Archetypus record"):
        archetypus.build_index(_Context(tree))
