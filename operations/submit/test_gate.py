"""The data-handling gate as machinery: spec 03's test 7, both directions.

"Real input refused without a current approval artifact; stale (policy hash
mismatch) and missing approvals each refuse with the reason named; fixture input
passes without one." Harvest #14 applies as it does everywhere else — every refusal
here has an acceptance beside it, because a gate that stopped refusing bad things in
order to stop refusing good ones would not be a fix.

The shipped policy at `config/data_handling_policy.json` is the one Tyrel's approval
would name. Nothing here approves anything: an approval record built in a test is a
record of nobody's decision, and it is used only to prove that the machinery around
one behaves.
"""

import inspect
import json

import pytest

from common.contracts.approval import build_approval_record
from common.contracts.errors import ApprovalRefusal
from operations.submit import gate


def approval(policy, *, action="data-gate", target=None, timestamp="2026-08-04T12:00:00Z"):
    return build_approval_record(
        subject_ids=["data-handling-policy"],
        action=action,
        reason="synthetic proving record; approves nothing",
        target_version_hash=target if target is not None else gate.policy_hash(policy),
        timestamp=timestamp,
    )


@pytest.fixture
def policy():
    return gate.load_policy()


# --- The policy is a file with a hash, and every clause the spec named -----------


def test_the_shipped_policy_carries_every_clause_the_spec_requires(policy):
    """A policy missing a clause is not a shorter policy; it is one Tyrel never
    approved, and reading it as valid would be the gate approving itself."""
    for field in gate._REQUIRED_POLICY_FIELDS:
        assert policy.get(field), f"the shipped policy has no {field}"
    assert policy["alpha_shortcuts_ledger"] == "workbench/standing/ALPHA_SHORTCUTS.md"


def test_a_policy_with_a_clause_removed_is_refused(tmp_path, policy):
    stripped = dict(policy)
    del stripped["retention_and_deletion"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(stripped), encoding="utf-8")
    with pytest.raises(gate.GateRefusal, match="retention_and_deletion"):
        gate.load_policy(path)


def test_an_absent_policy_is_a_failed_check_not_an_empty_one(tmp_path):
    with pytest.raises(gate.GateRefusal, match="could not be read"):
        gate.load_policy(tmp_path / "nothing.json")


def test_the_policy_hash_is_the_canonical_digest_of_its_own_content(policy):
    """Ruling 2026-08-04, item 2: the canonical policy bytes are the policy file,
    hashed the same way the rest of the tree hashes."""
    from common.contracts.canonical import canonical_bytes, digest_bytes

    assert gate.policy_hash(policy) == digest_bytes(canonical_bytes(policy))


def test_editing_one_character_of_the_policy_changes_its_hash(policy):
    changed = dict(policy, routing_rule=policy["routing_rule"] + ".")
    assert gate.policy_hash(changed) != gate.policy_hash(policy)


# --- Test 7: real input, three ways it refuses and one way it passes -------------


def test_real_input_with_no_approval_at_all_is_refused(policy):
    with pytest.raises(gate.GateRefusal, match="none was supplied"):
        gate.enforce(approval=None, policy=policy)


def test_real_input_with_a_stale_approval_is_refused_and_names_the_mismatch(policy):
    stale = approval(policy, target="b" * 64)
    with pytest.raises(gate.GateRefusal, match="stale"):
        gate.enforce(approval=stale, policy=policy)


def test_an_approval_for_a_different_action_does_not_authorize_this_door(policy):
    """`exclusion` and `salvage-promotion` are real approvals of real things. They
    are not this one, whatever target hash they happen to carry."""
    wrong = approval(policy, action="exclusion")
    with pytest.raises(gate.GateRefusal, match="does not authorize real input"):
        gate.enforce(approval=wrong, policy=policy)


def test_real_input_with_a_current_approval_passes(policy):
    gate.enforce(approval=approval(policy), policy=policy)


def test_the_gate_has_no_caller_controlled_fixture_override():
    """Fixture status comes from the loaded fixture path, never a gate argument."""
    assert "is_fixture" not in inspect.signature(gate.enforce).parameters


def test_a_missing_policy_on_the_real_path_refuses_rather_than_passing():
    """Unknown is never zero: a check that cannot run is a failure."""
    with pytest.raises(gate.GateRefusal, match="missing policy is a failed check"):
        gate.enforce(approval={"action": "data-gate"}, policy=None)


# --- The approval travels as a digest-checked reference --------------------------


def test_a_valid_approval_file_yields_its_content_addressed_reference(tmp_path, policy):
    path = tmp_path / "approval.json"
    record = approval(policy)
    path.write_text(json.dumps(record), encoding="utf-8")

    checked, reference = gate.read_external_approval(path, policy)

    assert checked == record
    assert reference.relative_path == f"receipts/sha256/{reference.sha256}.json"


def test_an_approval_edited_after_sealing_is_refused(tmp_path, policy):
    record = approval(policy)
    record["reason"] = "quietly widened after the fact"
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ApprovalRefusal, match="self-hash"):
        gate.read_external_approval(path, policy)


def test_an_approval_naming_someone_other_than_tyrel_is_refused(tmp_path, policy):
    record = approval(policy)
    record["approver"] = "an agent"
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ApprovalRefusal, match="only Tyrel approves"):
        gate.read_external_approval(path, policy)


def test_an_approval_file_that_is_not_json_is_refused(tmp_path, policy):
    path = tmp_path / "approval.json"
    path.write_text("this is not an approval, it is a sentence", encoding="utf-8")
    with pytest.raises(ApprovalRefusal, match="canonical JSON"):
        gate.read_external_approval(path, policy)


def test_a_stale_approval_file_is_refused_at_the_reference_boundary(tmp_path, policy):
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(approval(policy, target="c" * 64)), encoding="utf-8")
    with pytest.raises(ApprovalRefusal, match="stale"):
        gate.read_external_approval(path, policy)


def test_an_approval_read_from_a_run_tree_cannot_escape_its_root(tmp_path, policy):
    from common.contracts.approval import ApprovalRecordReference

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(approval(policy)), encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    escaping = ApprovalRecordReference("../outside.json", "d" * 64)
    with pytest.raises(ApprovalRefusal):
        gate.load_approval(escaping, root=root, policy=policy)


# --- Storage roots: the policy decides where real material may live --------------


def test_the_shipped_policy_names_a_storage_root_that_exists(policy):
    roots = gate.approved_storage_roots(policy)
    assert roots and all(root.is_dir() for root in roots)
    assert any(root.name == "private" for root in roots), (
        "the shipped policy's local storage root is private/, which is gitignored "
        "so that real material can live there without ever entering history"
    )


def test_a_policy_naming_no_storage_roots_is_refused(policy):
    with pytest.raises(gate.GateRefusal, match="names no approved storage roots"):
        gate.approved_storage_roots(dict(policy, storage_roots=[]))


def test_an_unresolvable_storage_root_is_a_failed_check_not_a_free_pass(tmp_path, policy):
    with pytest.raises(gate.GateRefusal, match="does not exist"):
        gate.approved_storage_roots(dict(policy, storage_roots=[str(tmp_path / "absent")]))


def test_a_location_inside_an_approved_root_is_allowed(tmp_path, policy):
    approved = tmp_path / "approved"
    (approved / "batch").mkdir(parents=True)
    roots = gate.approved_storage_roots(dict(policy, storage_roots=[str(approved)]))
    assert gate.require_approved_storage_location(approved / "batch", roots, "folder")


def test_a_location_outside_every_approved_root_is_refused(tmp_path, policy):
    approved = tmp_path / "approved"
    approved.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    roots = gate.approved_storage_roots(dict(policy, storage_roots=[str(approved)]))
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        gate.require_approved_storage_location(elsewhere, roots, "folder")


def test_a_symlink_cannot_walk_material_into_an_approved_root(tmp_path, policy):
    approved = tmp_path / "approved"
    approved.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (approved / "link").symlink_to(elsewhere, target_is_directory=True)
    roots = gate.approved_storage_roots(dict(policy, storage_roots=[str(approved)]))
    with pytest.raises(gate.GateRefusal, match="symlink"):
        gate.require_approved_storage_location(approved / "link", roots, "folder")
