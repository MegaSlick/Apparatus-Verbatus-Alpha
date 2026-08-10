"""The data-handling gate as machinery: the policy load, and storage-root location.

**Cut 2026-08-09, per Tyrel's ruling that session.** This file used to also cover
spec 03's test 7 — a per-run data-gate approval-record requirement for real input,
its policy-hash currency check, and the digest-checked reference an approval
travelled as. All three are gone: real material never reaches git regardless of any
per-run sign-off (it runs on a GPU host, `workbench/` is gitignored, and an ingress
check plus a pre-push payload scan already cover that mechanically), so the
requirement bought nothing. What remains, and is still real mechanical safety, is
the policy load's shape checks and the storage-root enforcement that keeps real
material inside the locations the policy names.

The shipped policy at `config/data_handling_policy.json` is the one this machinery
enforces. Harvest #14 applies as it does everywhere else — every refusal here has an
acceptance beside it, because a gate that stopped refusing bad things in order to
stop refusing good ones would not be a fix.
"""

import json

import pytest

from operations.submit import gate

# --- The policy is a file with every clause the spec named ------------------------


@pytest.fixture
def policy():
    return gate.load_policy()


def test_the_shipped_policy_carries_every_clause_the_spec_requires(policy):
    """A policy missing a clause is not a shorter policy; it is one Tyrel never
    approved, and reading it as valid would be the gate approving itself."""
    assert set(policy) == gate._POLICY_FIELDS
    assert policy["alpha_shortcuts_ledger"] == "workbench/standing/ALPHA_SHORTCUTS.md"


def test_the_shipped_policy_keeps_filename_links_and_states_settled_whole_run_retention(policy):
    assert "citation links" in policy["logging_rule"]
    assert "private refusal report" in policy["logging_rule"]
    assert "dead and broken or complete and exported" in policy["retention_and_deletion"]
    assert "whole run volume" in policy["retention_and_deletion"]


def test_the_alpha_shortcuts_ledger_is_a_clause_the_loader_enforces(tmp_path, policy):
    """It was named in the spec, asserted of the shipped file by the test above, and
    absent from what `load_policy` actually required — so a policy stripped of it
    loaded clean and the mismatch was invisible to the suite."""
    stripped = {key: value for key, value in policy.items() if key != "alpha_shortcuts_ledger"}
    path = tmp_path / "no-ledger.json"
    path.write_text(json.dumps(stripped), encoding="utf-8")
    with pytest.raises(gate.GateRefusal, match="alpha_shortcuts_ledger"):
        gate.load_policy(path)


@pytest.mark.parametrize("value", [True, 1, {"x": 1}, "x", ["a rule"]])
def test_a_clause_that_states_no_rule_is_refused(tmp_path, policy, value):
    """`if not record.get(field)` was pure truthiness, so every prose clause but
    `storage_roots` could be replaced by `true` and the policy still loaded — a
    policy that says nothing passing as one that does."""
    mutated = dict(policy)
    mutated["logging_rule"] = value
    path = tmp_path / "boolean-clause.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")
    with pytest.raises(gate.GateRefusal, match="not a stated rule"):
        gate.load_policy(path)


def test_a_clause_this_gate_does_not_enforce_is_refused(tmp_path, policy):
    """The other direction, and the reason the field set is exact: a clause Tyrel
    approved that nothing here checks is a rule with no machinery behind it."""
    extended = dict(policy)
    extended["a_clause_nothing_enforces"] = "some rule nobody checks"
    path = tmp_path / "extra-clause.json"
    path.write_text(json.dumps(extended), encoding="utf-8")
    with pytest.raises(gate.GateRefusal, match="Unknown"):
        gate.load_policy(path)


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


def test_a_policy_missing_its_version_label_is_refused(tmp_path, policy):
    stripped = dict(policy)
    stripped["policy_version"] = "   "
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(stripped), encoding="utf-8")
    with pytest.raises(gate.GateRefusal, match="no policy version"):
        gate.load_policy(path)


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
