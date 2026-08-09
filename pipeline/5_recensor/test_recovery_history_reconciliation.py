"""The Recensor names recovery-history gaps before a later stage sees them.

These are real stage invocations because the defects all lived at joins between
append-only artifact histories.  Unit-testing a handcrafted list would not show
whether a normal restart can publish a new ordinal over a broken history.
"""

import copy
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import DESIGNATOR, PERLECTOR, RECENSOR
from common.recovery import FALLBACK_RECROP
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def invoke(
    root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def through_perlector(root: Path, run_id: str, scenario: str) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def records(tree: RunTree, stage: str, kind: str, act_id: str | None = None) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind and (act_id is None or entry["subject_id"] == act_id)
    ]


def test_an_unrequested_second_perlectio_is_refused_before_recensor_publishes(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "unrequested", "happy")
    tree = RunTree(root, "unrequested")
    original = records(tree, PERLECTOR, "perlectio")[0]
    act_id = original["subject_id"]
    forged = copy.deepcopy(original)
    forged_attempt = attempt_id(act_id, "perlegere", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(PERLECTOR, "perlectio", act_id, forged_attempt)
    forged["payload"]["attempt_ordinal"] = 2
    forged["payload"]["text"] = "a reread nobody requested"
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", forged["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(forged))
    tree.write_manifest(PERLECTOR)

    result = invoke(root, "unrequested", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "no reading may appear unrequested" in result.stderr
    assert records(tree, RECENSOR, "review") == []


def test_an_orphaned_recovery_request_is_named_before_recensor_writes_again(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "orphan", "review")
    first = invoke(root, "orphan", "review", "pipeline/5_recensor/run.py")
    assert first.returncode == 3, first.stderr
    tree = RunTree(root, "orphan")
    request = records(tree, RECENSOR, "recovery-request")[0]
    assert request["payload"]["recovery_kind"] == FALLBACK_RECROP
    assert request["payload"]["kind_budget_allowed"] == 1
    assert request["payload"]["kind_budget_used"] == 0
    review = next(
        record
        for record in records(tree, RECENSOR, "review", request["subject_id"])
        if record["outcome"] == "recovery-requested"
    )
    review_path = tree.resolve(tree.artifact_path(RECENSOR, "review", review["artifact_id"]))
    review_path.unlink()
    tree.write_manifest(RECENSOR)
    before = [entry["artifact_id"] for entry in tree.build_manifest(RECENSOR)["artifacts"]]

    result = invoke(root, "orphan", "review", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "without exactly one matching recovery-requested review" in result.stderr
    assert [entry["artifact_id"] for entry in tree.build_manifest(RECENSOR)["artifacts"]] == before


def test_a_pending_recovery_request_remains_held_on_a_direct_recensor_retry(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "pending", "review")
    first = invoke(root, "pending", "review", "pipeline/5_recensor/run.py")
    assert first.returncode == 3, first.stderr
    tree = RunTree(root, "pending")
    before = [entry["artifact_id"] for entry in tree.build_manifest(RECENSOR)["artifacts"]]

    retry = invoke(root, "pending", "review", "pipeline/5_recensor/run.py")
    assert retry.returncode == 3, retry.stderr
    assert [entry["artifact_id"] for entry in tree.build_manifest(RECENSOR)["artifacts"]] == before


def test_a_resealed_per_kind_counter_is_refused_before_recensor_treats_it_as_history(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "counter", "review")
    first = invoke(root, "counter", "review", "pipeline/5_recensor/run.py")
    assert first.returncode == 3, first.stderr
    tree = RunTree(root, "counter")
    request = records(tree, RECENSOR, "recovery-request")[0]
    review = next(
        record
        for record in records(tree, RECENSOR, "review", request["subject_id"])
        if record["outcome"] == "recovery-requested"
    )

    changed_request = copy.deepcopy(request)
    changed_request["payload"]["kind_budget_used"] = 1
    changed_request["self_hash"] = self_hash(changed_request)
    request_path = tree.resolve(
        tree.artifact_path(RECENSOR, "recovery-request", changed_request["artifact_id"])
    )
    request_bytes = canonical_bytes(changed_request)
    request_path.write_bytes(request_bytes)
    changed_reference = {
        "relative_path": tree.artifact_path(
            RECENSOR, "recovery-request", changed_request["artifact_id"]
        ),
        "sha256": digest_bytes(request_bytes),
    }
    changed_review = copy.deepcopy(review)
    original_reference = changed_review["payload"]["recovery_request_ref"]
    changed_review["payload"]["recovery_request_ref"] = changed_reference
    changed_review["inputs"] = [
        changed_reference if reference == original_reference else reference
        for reference in changed_review["inputs"]
    ]
    changed_review["self_hash"] = self_hash(changed_review)
    tree.resolve(tree.artifact_path(RECENSOR, "review", changed_review["artifact_id"])).write_bytes(
        canonical_bytes(changed_review)
    )
    tree.write_manifest(RECENSOR)
    before = [entry["artifact_id"] for entry in tree.build_manifest(RECENSOR)["artifacts"]]

    result = invoke(root, "counter", "review", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "recorded total or kind budget" in result.stderr
    assert [entry["artifact_id"] for entry in tree.build_manifest(RECENSOR)["artifacts"]] == before


def test_a_recovery_crop_without_its_reread_is_refused_before_acceptance(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "unread-recrop", "review")
    first = invoke(root, "unread-recrop", "review", "pipeline/5_recensor/run.py")
    assert first.returncode == 3, first.stderr
    tree = RunTree(root, "unread-recrop")
    request = records(tree, RECENSOR, "recovery-request")[0]
    act_id = request["subject_id"]

    recrop = invoke(
        root,
        "unread-recrop",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=act_id,
        recovery_request=request["artifact_id"],
    )
    assert recrop.returncode == 0, recrop.stderr
    assert len(records(tree, DESIGNATOR, "region", act_id)) == 2

    result = invoke(root, "unread-recrop", "review", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "recovery crop(s)" in result.stderr
    assert "Perlectio attempt(s)" in result.stderr


def test_an_empty_completed_reading_is_held_not_accepted(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "empty-reading", "happy")
    tree = RunTree(root, "empty-reading")
    reading = records(tree, PERLECTOR, "perlectio")[0]
    path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", reading["artifact_id"]))
    changed = copy.deepcopy(reading)
    changed["payload"]["text"] = ""
    changed["self_hash"] = self_hash(changed)
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(PERLECTOR)

    result = invoke(root, "empty-reading", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 3, result.stderr
    reviews = records(tree, RECENSOR, "review", changed["subject_id"])
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "silence is not blank proof" in reviews[0]["payload"]["reason"]
