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
from pathlib import Path

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


def test_the_shipped_policy_names_a_local_storage_root_that_exists(policy):
    """The local half of the shipped list resolves on every checkout, host or pod.

    Checked directly against the repository rather than through
    ``gate.approved_storage_roots`` on the unfiltered shipped list, so this
    assertion does not depend on whichever roots that call happens to resolve
    on the machine running the suite.
    """
    local_roots = [root for root in policy["storage_roots"] if not root.startswith("/")]
    assert local_roots
    resolved = gate.approved_storage_roots(dict(policy, storage_roots=local_roots))
    assert resolved and all(root.is_dir() for root in resolved)
    assert any(root.name == "private" for root in resolved), (
        "the shipped policy's local storage root is private/, which is gitignored "
        "so that real material can live there without ever entering history"
    )


def test_the_shipped_policy_names_the_pod_volume_mount_as_a_root(policy):
    """The pod volume root is listed, spelled exactly as the launch seals it.

    ``operations/pod/boot_a_request.py``'s ``BOOT_A_VOLUME_MOUNT_PATH`` is the
    one concrete ``volume_mount_path`` a real launch request in this tree
    seals -- this is the path Tyrel's ruling names as accepted storage for the
    run's duration (workbench/standing/TYREL_RULINGS_2026-09-01_BUILD_SESSION.md).
    """
    assert "/workspace/private" in policy["storage_roots"]


def test_the_full_shipped_policy_still_yields_a_usable_gate_on_any_machine(policy):
    """The unmodified two-root shipped policy resolves wherever the suite runs.

    ``gate.resolve_storage_roots`` resolves each listed root independently, so
    a plain checkout gets the local ``private/`` root and a pod with the volume
    mounted gets both. The assertion is written against *this* machine's own
    facts rather than against the absence of ``/workspace/private``: this suite
    is meant to run on the pod too -- the branch adds a pod entrypoint and a
    pod dependency group -- and pinning the laptop's answer would turn the one
    machine the money is spent on into a red test for correct behaviour. The
    skip itself is proven synthetically below, where both halves are ours.
    """
    resolved = gate.resolve_storage_roots(policy)
    assert resolved.roots
    assert all(root.is_dir() for root in resolved.roots)
    assert any(root.name == "private" for root in resolved.roots)
    pod_root = Path("/workspace/private")
    admitted = any(root == pod_root for root in resolved.roots)
    assert admitted == pod_root.is_dir(), (
        "the pod volume root is admitted exactly when this machine has it mounted"
    )
    if not admitted:
        assert any("/workspace/private" in entry for entry in resolved.skipped)


def test_a_skipped_root_comes_back_beside_the_resolved_ones_not_only_in_a_refusal(tmp_path, policy):
    """GOVERNANCE 2: the narrowing is a fact about the run, on every path.

    Naming a skipped root only when *every* root fails left the ordinary case
    -- a partially resolving policy, which is nearly every machine -- returning
    a quietly shorter approved list. ``pod_run`` writes both lists into its run
    report from here.
    """
    present = tmp_path / "present"
    present.mkdir()
    absent = tmp_path / "absent"

    resolved = gate.resolve_storage_roots(dict(policy, storage_roots=[str(absent), str(present)]))

    assert resolved.roots == (present.resolve(),)
    [skipped] = resolved.skipped
    assert str(absent) in skipped and "does not exist" in skipped


def test_a_not_a_directory_root_is_skipped_and_named(tmp_path, policy):
    present = tmp_path / "present"
    present.mkdir()
    file_root = tmp_path / "a-file"
    file_root.write_text("not a directory", encoding="utf-8")

    resolved = gate.resolve_storage_roots(
        dict(policy, storage_roots=[str(file_root), str(present)])
    )

    assert resolved.roots == (present.resolve(),)
    [skipped] = resolved.skipped
    assert str(file_root) in skipped and "not a directory" in skipped


def test_a_policy_naming_no_storage_roots_is_refused(policy):
    with pytest.raises(gate.GateRefusal, match="names no approved storage roots"):
        gate.approved_storage_roots(dict(policy, storage_roots=[]))


def test_an_unresolvable_storage_root_is_a_failed_check_not_a_free_pass(tmp_path, policy):
    with pytest.raises(gate.GateRefusal, match="does not exist"):
        gate.approved_storage_roots(dict(policy, storage_roots=[str(tmp_path / "absent")]))


def test_one_absent_root_beside_one_present_root_yields_the_present_one(tmp_path, policy):
    present = tmp_path / "present"
    present.mkdir()
    absent = tmp_path / "absent"
    resolved = gate.approved_storage_roots(dict(policy, storage_roots=[str(absent), str(present)]))
    assert resolved == (present.resolve(),)


def test_all_roots_absent_still_refuses(tmp_path, policy):
    with pytest.raises(gate.GateRefusal, match="none of the data-handling policy"):
        gate.approved_storage_roots(
            dict(policy, storage_roots=[str(tmp_path / "a"), str(tmp_path / "b")])
        )


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


def test_an_intermediate_symlink_below_the_approved_root_is_also_refused(tmp_path, policy):
    approved = tmp_path / "approved"
    actual = approved / "actual"
    (actual / "batch").mkdir(parents=True)
    (approved / "redirect").symlink_to(actual, target_is_directory=True)
    roots = gate.approved_storage_roots(dict(policy, storage_roots=[str(approved)]))

    with pytest.raises(gate.GateRefusal, match="crosses a symlink"):
        gate.require_approved_storage_location(approved / "redirect" / "batch", roots, "folder")


def test_an_unapproved_location_is_named_as_unapproved_not_as_a_redirect(tmp_path, policy):
    """A refusal must name the problem the operator actually has.

    The redirect walk stops when it meets an approved root. A location under no
    approved root therefore walked to the filesystem root and reported the first
    ordinary platform alias it met -- `/tmp` is a symlink on macOS -- as
    "crosses a symlink; an approved storage root cannot be entered by redirect".
    The material was refused either way, so nothing was written anywhere it
    should not be; the cost was that the operator was sent to hunt for a planted
    redirect when the true fact was that the folder is not approved at all.

    The alias is built here rather than borrowed from the platform, so the case
    holds on a runner whose `/tmp` is a real directory.
    """
    approved = tmp_path / "approved"
    approved.mkdir()
    roots = gate.approved_storage_roots(dict(policy, storage_roots=[str(approved)]))

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "batch").mkdir(parents=True)
    alias = tmp_path / "platform-alias"
    alias.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(gate.GateRefusal) as refusal:
        gate.require_approved_storage_location(alias / "batch", roots, "submitted folder")

    assert "outside every approved storage root" in str(refusal.value)
    assert "crosses a symlink" not in str(refusal.value)


def test_containment_is_judged_by_filesystem_identity_not_spelling(tmp_path):
    """Case variants must remain contained when text comparison disagrees."""
    source = tmp_path / "masters"
    source.mkdir()
    sibling = tmp_path / "ready"
    sibling.mkdir()
    assert gate.same_or_inside(source, source)
    assert gate.same_or_inside(source, source / "inside")
    assert not gate.same_or_inside(source, sibling)
    assert not gate.same_or_inside(source, sibling / "not-yet-written.json")
    assert not gate.same_or_inside(tmp_path / "never-made", source)
    variant = tmp_path / "Masters"
    if variant.is_dir():  # Only case-insensitive filesystems make these names identical.
        assert gate.same_or_inside(source, variant / "ready")
        assert not (variant / "ready").is_relative_to(source)

    # The identity case that runs everywhere. Every assertion above this line
    # also holds for a plain `is_relative_to` implementation, and the block just
    # above is skipped on a case-sensitive filesystem, so on the Linux CI legs
    # nothing here could fail if `same_or_inside` regressed to comparing
    # spellings. A symlinked alias is the same directory under a different name
    # on every platform, which is precisely the distinction being claimed.
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    assert gate.same_or_inside(source, alias / "inside")
    assert not (alias / "inside").is_relative_to(source)
