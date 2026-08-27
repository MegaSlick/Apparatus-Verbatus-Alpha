"""The Recensor names recovery-history gaps before a later stage sees them.

These are real stage invocations because the defects all lived at joins between
append-only artifact histories.  Unit-testing a handcrafted list would not show
whether a normal restart can publish a new ordinal over a broken history.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.envelope import validate_envelope
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import DESIGNATOR, PERLECTOR, RECENSOR
from common.perlector_audit import (
    audit_digest,
    audit_request,
    neutral_prompt,
    validate_chain,
    validate_draft,
    validate_finding,
    validate_perlectio_audit,
)
from common.recovery import FALLBACK_RECROP
from common.runtree.store import RunTree
from conftest import rebind_stage_seal_artifact as rebind_stage_seal

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
        "pipeline/1_ink_map/run.py",
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
    rebind_stage_seal(tree, PERLECTOR)

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

    # Blanking the reading alone would leave its Pass-C audit draft still
    # naming the original non-empty semi-final text: audit_state's own
    # accounting boundary ("changed after its audit draft without a change
    # record") would refuse the forgery before the test ever reaches the
    # held-not-accepted boundary it means to exercise. The draft -- and the
    # finding and references that bind it -- must be forged blank right
    # alongside the reading, exactly as a genuinely-blank Pass-B reading
    # would have produced them.
    audit_record = reading["payload"]["audit"]
    draft_relative = audit_record["draft_ref"]["relative_path"]
    draft_path = tree.resolve(draft_relative)
    draft = json.loads(draft_path.read_text())
    draft["payload"]["semi_final_text"] = ""
    blank_flags = [
        {"class": flag["class"], "location": {"start": 0, "end": 0}}
        for flag in draft["payload"]["flags"]
    ]
    draft["payload"]["flags"] = blank_flags
    validate_draft(draft["payload"])
    draft["self_hash"] = self_hash(draft)
    validate_envelope(draft)
    draft_bytes = canonical_bytes(draft)
    draft_path.write_bytes(draft_bytes)
    draft_digest = digest_bytes(draft_bytes)
    tree.read_artifact(PERLECTOR, "audit-draft", draft["artifact_id"])

    finding_relative = audit_record["finding_ref"]["relative_path"]
    finding_path = tree.resolve(finding_relative)
    finding = json.loads(finding_path.read_text())
    finding["inputs"] = [{"relative_path": draft_relative, "sha256": draft_digest}]
    finding["payload"]["flags"] = blank_flags
    finding["payload"]["change_record"] = []
    finding["payload"]["uncertain_spans"] = []
    finding["payload"]["unresolved"] = False
    validate_finding(finding["payload"], text="")
    finding["self_hash"] = self_hash(finding)
    validate_envelope(finding)
    finding_bytes = canonical_bytes(finding)
    finding_path.write_bytes(finding_bytes)
    tree.read_artifact(PERLECTOR, "audit-finding", finding["artifact_id"])

    finding_digest = digest_bytes(finding_bytes)
    changed = copy.deepcopy(reading)
    changed["payload"]["text"] = ""
    changed["payload"]["audit"]["finding_digest"] = audit_digest(finding["payload"])
    changed["payload"]["audit"]["unresolved"] = False
    changed["payload"]["audit"]["reproofs"] = [
        {
            "class": flag["class"],
            "location": flag["location"],
            "prompt": neutral_prompt(start=0, end=0, text_length=0),
        }
        for flag in blank_flags
    ]
    changed["payload"]["audit"]["draft_ref"]["sha256"] = draft_digest
    changed["payload"]["audit"]["finding_ref"]["sha256"] = finding_digest
    # Pass C's re-proof plan is delivered to the reader as a closed request and
    # the Perlectio names that request's digest, so the forgery has to restate
    # the instrument as well as the plan: a blanked draft renders a different
    # request, and `validate_chain` rebuilds it from the draft it reads back.
    changed["payload"]["audit"]["request_digest"] = audit_digest(
        audit_request(
            act_key=draft["payload"]["act_key"],
            attempt_ordinal=draft["payload"]["attempt_ordinal"],
            draft_ref=changed["payload"]["audit"]["draft_ref"],
            semi_final_text="",
            flags=blank_flags,
        )
    )
    # The reading's own envelope-level `inputs` also names the draft and
    # finding it bound (`row["inputs"] + [draft_ref, finding_ref]` in
    # pipeline/4_perlector/run.py), independent of the payload's copies.
    for reference in changed["inputs"]:
        if reference["relative_path"] == draft_relative:
            reference["sha256"] = draft_digest
        elif reference["relative_path"] == finding_relative:
            reference["sha256"] = finding_digest
    validate_perlectio_audit(changed["payload"]["audit"], text_length=0)
    changed["self_hash"] = self_hash(changed)
    validate_envelope(changed)
    path.write_bytes(canonical_bytes(changed))
    rebind_stage_seal(tree, PERLECTOR)
    validate_chain(
        tree,
        tree.read_artifact(PERLECTOR, "perlectio", reading["artifact_id"]),
        reading["subject_id"],
    )

    result = invoke(root, "empty-reading", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 3, result.stderr
    reviews = records(tree, RECENSOR, "review", changed["subject_id"])
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "silence is not blank proof" in reviews[0]["payload"]["reason"]
