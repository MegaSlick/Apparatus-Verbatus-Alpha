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
        self.run = tree.read_run()
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


@pytest.fixture(scope="module")
def established_run(tmp_path_factory):
    """One happy run, shared by every test below that only reads it.

    `build_index` and `validate_index` write nothing, so orchestrating once is
    the same evidence as orchestrating ten times and several minutes cheaper.
    The tests that mutate a tree — deleting the index, forging a second record —
    still take their own run, because they change what the next reader sees.
    """
    root = tmp_path_factory.mktemp("established") / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    return _Context(RunTree(root, "r"))


def test_index_reconciles_1_to_1_with_both_established_acts_in_the_happy_scenario(established_run):
    tree = established_run.tree
    index = _index(tree)

    established = {
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert established == {row["act_id"] for row in index["rows"]}
    assert index["record_count"] == len(index["rows"]) == 2
    assert index["rows"] == sorted(index["rows"], key=lambda row: row["act_id"])
    assert index["stage"] == ARCHETYPUS


def test_every_index_row_carries_its_records_status_and_text_hash(established_run):
    tree = established_run.tree
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
    assert index["record_count"] == 1


def test_the_index_self_hash_verifies(established_run):
    assert verify_self_hash(_index(established_run.tree))


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


def test_validate_index_refuses_a_missing_row(established_run):
    short = archetypus.build_index(established_run)
    short["rows"] = short["rows"][:-1]
    short["record_count"] = len(short["rows"])
    short["self_hash"] = self_hash(short)
    with pytest.raises(FatalAccounting, match="do not reconcile 1:1"):
        archetypus.validate_index(established_run, short)


def test_validate_index_refuses_a_record_for_an_act_the_recensor_did_not_accept(established_run):
    """The mirror of the missing row, killing the reconciliation's OTHER conjunct.

    An Archetypus record exists on disk for an act nobody accepted; the index
    honestly does not carry it, so rows == accepted and the first conjunct is
    quiet. Only `set(on_disk) != accepted` notices. If someone deletes that
    comparison, a run can deliver an established reading for an act the
    Recensor never accepted while every other check verifies.
    """
    index = archetypus.build_index(established_run)
    intruder = dict(index["rows"][0], act_id="act_ffffffffffffffff")
    on_disk = {row["act_id"]: row for row in index["rows"]}
    on_disk[intruder["act_id"]] = intruder
    with pytest.raises(FatalAccounting, match="do not reconcile 1:1"):
        archetypus.validate_index(established_run, index, on_disk=on_disk)


def test_validate_index_refuses_a_duplicate_row(established_run):
    doubled = archetypus.build_index(established_run)
    doubled["rows"] = doubled["rows"] + [dict(doubled["rows"][0])]
    doubled["record_count"] = len(doubled["rows"])
    doubled["self_hash"] = self_hash(doubled)
    with pytest.raises(FatalAccounting, match="duplicate row"):
        archetypus.validate_index(established_run, doubled)


def test_validate_index_refuses_a_row_that_disagrees_with_its_record(established_run):
    edited = archetypus.build_index(established_run)
    edited["rows"][0]["text_status"] = "no_readable_text"
    edited["self_hash"] = self_hash(edited)
    with pytest.raises(FatalAccounting, match="does not match its immutable record"):
        archetypus.validate_index(established_run, edited)


def test_validate_index_refuses_an_index_whose_self_hash_was_not_recomputed(established_run):
    edited = archetypus.build_index(established_run)
    edited["record_count"] = 99
    with pytest.raises(FatalAccounting, match="self-hash"):
        archetypus.validate_index(established_run, edited)


# --- The rest of `validate_index`'s refusals, each exercised ------------------
#
# HANDOFF.md offers `validate_index` to any consumer wanting to prove the
# accounting before relying on it, so these refusals are load-bearing for
# someone other than this stage. Each case reseals a well-formed index around
# one defect, because a refusal no test can kill is a claim nobody has measured.


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda index: index.update(record_count=len(index["rows"]) + 1), "count disagrees"),
        (lambda index: index.update(record_count=-1), "non-negative integer"),
        (lambda index: index.update(record_count=True), "non-negative integer"),
        (lambda index: index.update(record_count="2"), "non-negative integer"),
        (lambda index: index.update(schema="skeleton.v0"), "different schema or run"),
        (lambda index: index.update(run_id="another-run"), "different schema or run"),
        (lambda index: index.update(stage="7_armarium"), "own stage label or self-hash"),
        (lambda index: index.update(rows={}), "rows are not a list"),
        (lambda index: index["rows"].__setitem__(0, {"act_id": "a"}), "malformed row"),
        (lambda index: index["rows"][0].update(act_key=""), "row with malformed values"),
        (lambda index: index["rows"][0].update(text_hash=7), "row with malformed values"),
        (lambda index: index["rows"][0].update(sha256="not-a-digest"), "row with malformed values"),
    ],
)
def test_validate_index_refuses_each_resealed_defect(established_run, mutate, expected):
    index = archetypus.build_index(established_run)
    mutate(index)
    # Resealed, so every refusal below is the check under test rather than the
    # self-hash catching an edit before anything else looks at it.
    index["self_hash"] = self_hash(index)
    with pytest.raises(FatalAccounting, match=expected):
        archetypus.validate_index(established_run, index)


def test_validate_index_refuses_an_index_that_is_not_the_closed_shape(established_run):
    index = archetypus.build_index(established_run)
    index["note"] = "an extra field the derived shape does not carry"
    index["self_hash"] = self_hash(index)
    with pytest.raises(FatalAccounting, match="closed derived-index shape"):
        archetypus.validate_index(established_run, index)


def test_the_index_the_stage_actually_wrote_passes_its_own_consumer_check(established_run):
    """The acceptance half: the refusals above must not refuse a real index."""
    assert archetypus.validate_index(
        established_run, established_run.tree.read_index(ARCHETYPUS)
    ) == archetypus.build_index(established_run)


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


def test_a_record_whose_payload_names_a_different_act_than_its_envelope_is_fatal(tmp_path):
    """The row's identity comes from the envelope; the text comes from the payload.

    A record resealed so the two disagree would put one act's established text
    under another act's identity in every consumer that reads the index — the
    one place a wrong answer would look perfectly well formed.
    """
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")

    entry = next(
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["act_id"] = "act_ffffffffffffffff"
    record["payload"]["self_hash"] = self_hash(record["payload"])
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))

    with pytest.raises(FatalAccounting, match="carries payload identity"):
        archetypus.build_index(_Context(tree))
