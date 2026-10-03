"""The data-handling gate as machinery: the policy load, and storage-root location.

There is no per-run data-gate approval-record requirement for real input: real
material never reaches git regardless of any per-run sign-off (it runs on a GPU
host, `workbench/` is gitignored, and an ingress check plus CI's full-history
payload scan already cover that mechanically). What remains, and is still real
mechanical safety, is the policy load's shape checks and the storage-root
enforcement that keeps real material inside the locations the policy names.

The shipped policy at `config/data_handling_policy.json` is the one this
machinery enforces. Every refusal here has an acceptance beside it, because a
gate that stopped refusing bad things in order to stop refusing good ones
would not be a fix.
"""

import json
from pathlib import Path

import pytest

from operations.submit import gate

# --- The policy load ---------------------------------------------------------


@pytest.fixture
def policy():
    return gate.load_policy()


@pytest.mark.parametrize("roots", [None, [], [""], [1]])
def test_a_policy_that_names_no_usable_storage_root_is_refused_at_load(tmp_path, policy, roots):
    mutated = dict(policy)
    if roots is None:
        del mutated["storage_roots"]
    else:
        mutated["storage_roots"] = roots
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")
    with pytest.raises(gate.GateRefusal, match="storage root"):
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
    seals; the pod volume is accepted storage for the run's duration.
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
    """The narrowing is a fact about the run, on every path.

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


@pytest.mark.hostile_local
def test_a_symlink_cannot_walk_material_into_an_approved_root(tmp_path, policy):
    approved = tmp_path / "approved"
    approved.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (approved / "link").symlink_to(elsewhere, target_is_directory=True)
    roots = gate.approved_storage_roots(dict(policy, storage_roots=[str(approved)]))
    with pytest.raises(gate.GateRefusal, match="symlink"):
        gate.require_approved_storage_location(approved / "link", roots, "folder")


@pytest.mark.hostile_local
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

    A location under no approved root at all must be refused as unapproved,
    not misdiagnosed as a planted redirect merely because the walk to the
    filesystem root passed an ordinary platform alias (e.g. `/tmp` on macOS).
    The alias is built here rather than borrowed from the platform, so the
    case holds on a runner whose `/tmp` is a real directory.
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


@pytest.mark.hostile_local
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
