"""The submit door: gated first, sealed once, and silent about what it saw.

Spec 03 asks three things of this tool that are testable here. Upload completion is
explicit and sealed — a partial transfer can never look admitted. The gate is
enforced before a byte is hashed, because this is the first place a real folder is
ever touched. And the logging rule is mechanical rather than a promise: a log line
may carry counts, digests and status words, and it may never carry a name, a path,
or image bytes.

Spec 03's test 6 — the cleanup drill on synthetic material, with declared bounds —
runs against `purge` here and against `cleanup.verify_synthetic_cleanup` beside it.
"""

import json

import pytest

from common.contracts.approval import build_approval_record
from common.contracts.canonical import canonical_bytes, digest_bytes, verify_self_hash
from common.contracts.errors import ApprovalRefusal
from operations.submit import cleanup, gate, submit


def approved_policy(tmp_path, roots):
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(root) for root in roots]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return policy, path


def approval_file(tmp_path, policy, *, target=None):
    path = tmp_path / "approval.json"
    path.write_text(
        json.dumps(
            build_approval_record(
                subject_ids=["data-handling-policy"],
                action="data-gate",
                reason="synthetic proving record; approves nothing",
                target_version_hash=target or gate.policy_hash(policy),
                timestamp="2026-08-04T12:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def submission(tmp_path):
    """An approved storage root with two synthetic files in it."""
    approved = tmp_path / "approved"
    folder = approved / "batch"
    folder.mkdir(parents=True)
    (folder / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    (folder / "nested").mkdir()
    (folder / "nested" / "page-2.png").write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    policy, policy_path = approved_policy(tmp_path, [approved])
    return {
        "approved": approved,
        "folder": folder,
        "policy": policy,
        "policy_path": policy_path,
        "approval": approval_file(tmp_path, policy),
        "manifest_out": approved / "submission.json",
    }


# --- The sealed manifest ---------------------------------------------------------


def test_a_submission_seals_one_self_hashed_manifest_naming_every_file(submission):
    manifest = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )

    assert manifest["schema"] == submit.SCHEMA
    assert [entry["relative_path"] for entry in manifest["files"]] == [
        "nested/page-2.png",
        "page-1.png",
    ]
    assert verify_self_hash(manifest)
    assert json.loads(submission["manifest_out"].read_text(encoding="utf-8")) == manifest


def test_the_manifest_names_the_approval_that_admitted_the_corpus(submission):
    manifest = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    _record, reference = gate.read_external_approval(submission["approval"], submission["policy"])
    assert manifest["authorized_by"] == reference.to_record()


def test_the_manifest_digests_are_the_real_digests_of_the_real_bytes(submission):
    manifest = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    for entry in manifest["files"]:
        data = (submission["folder"] / entry["relative_path"]).read_bytes()
        assert entry["sha256"] == digest_bytes(data)
        assert entry["bytes"] == len(data)


def test_submitting_the_same_folder_twice_seals_identical_bytes(submission):
    first = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    second = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    assert canonical_bytes(first) == canonical_bytes(second)


def test_an_empty_folder_is_a_loud_failure_rather_than_an_empty_submission(submission, tmp_path):
    empty = submission["approved"] / "empty"
    empty.mkdir()
    with pytest.raises(submit.SubmitRefusal, match="no files to submit"):
        submit.submit(
            empty,
            submission["manifest_out"],
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert not submission["manifest_out"].exists()


# --- The gate is checked before a byte is read -----------------------------------


def test_a_submission_with_no_approval_touches_nothing_and_writes_nothing(submission):
    with pytest.raises(gate.GateRefusal, match="none was supplied"):
        submit.submit(
            submission["folder"],
            submission["manifest_out"],
            approval_record=None,
            policy_path=submission["policy_path"],
        )
    assert not submission["manifest_out"].exists()


def test_a_submission_with_a_stale_approval_is_refused_and_writes_nothing(submission, tmp_path):
    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    stale_path = approval_file(stale_root, submission["policy"], target="b" * 64)
    with pytest.raises(ApprovalRefusal, match="stale"):
        submit.submit(
            submission["folder"],
            submission["manifest_out"],
            approval_record=stale_path,
            policy_path=submission["policy_path"],
        )
    assert not submission["manifest_out"].exists()


def test_a_folder_outside_the_approved_storage_roots_is_refused(submission, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "page.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        submit.submit(
            elsewhere,
            submission["manifest_out"],
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert not submission["manifest_out"].exists()


def test_a_manifest_outside_the_approved_storage_roots_is_refused(submission, tmp_path):
    outside = tmp_path / "outside" / "submission.json"
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        submit.submit(
            submission["folder"],
            outside,
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert not outside.exists()


def test_the_manifest_cannot_be_written_inside_the_folder_it_inventories(submission):
    inside = submission["folder"] / "submission.json"
    with pytest.raises(submit.SubmitRefusal, match="inside the submitted folder"):
        submit.submit(
            submission["folder"],
            inside,
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert not inside.exists()


# --- The logging rule, made mechanical -------------------------------------------


def test_a_log_line_may_carry_counts_and_digests(capsys):
    submit.log("submission sealed", files=2, digest="a" * 64)
    assert capsys.readouterr().out.strip() == f"submission sealed: digest={'a' * 64} files=2"


def test_a_log_line_may_never_carry_a_name_a_path_or_bytes():
    for field in ("path", "filename", "relative_path", "image", "content"):
        with pytest.raises(submit.SubmitRefusal, match="may never carry a name"):
            submit.log("event", **{field: "page-1.png"})


def test_a_log_name_cannot_be_smuggled_through_the_event_or_an_allowed_field():
    with pytest.raises(submit.SubmitRefusal, match="event"):
        submit.log("page-1.png", files=1)
    with pytest.raises(submit.SubmitRefusal, match="status"):
        submit.log("cleanup", status="page-1.png")
    with pytest.raises(submit.SubmitRefusal, match="digest"):
        submit.log("submission sealed", digest="page-1.png")


def test_no_log_line_the_tool_actually_emits_names_a_submitted_file(submission, capsys):
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    printed = capsys.readouterr().out
    assert printed.strip()
    for name in ("page-1.png", "page-2.png", "nested", str(submission["folder"])):
        assert name not in printed


# --- Test 6: the cleanup drill, on synthetic material, with declared bounds -------


def test_the_cleanup_drill_removes_its_target_and_verifies_declared_bounds(submission, tmp_path):
    """The threat model is stated and narrow: a declared target path, temp path, log
    marker, or listed volume object left behind. It is never a claim of forensic
    unrecoverability from storage media, snapshots, or provider backups, which no
    filesystem check can establish (GOVERNANCE 10)."""
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    stray = submission["manifest_out"].with_name(f".{submission['manifest_out'].name}.tmp-999")
    stray.write_bytes(b"a crashed write")
    log_path = tmp_path / "run.log"
    log_path.write_bytes(b"submission sealed: digest=abc files=2\n")

    report = submit.purge(submission["manifest_out"])

    assert report.target_removed is True
    assert report.temp_files_removed == 1
    assert report.remaining_temp_files == ()
    assert report.volume_listing is None, "no volume exists yet; an empty tuple would claim a check"

    result = cleanup.verify_synthetic_cleanup(
        target_paths=[submission["manifest_out"]],
        temporary_paths=[stray],
        log_paths=[log_path],
        forbidden_markers=[b"page-1.png", b"nested"],
        volume_objects=None,
    )
    assert result.target_paths_checked == 1
    assert result.temporary_paths_checked == 1
    assert result.logs_scanned == 1
    assert result.volume_objects_seen == 0


def test_the_cleanup_drill_fails_when_the_target_is_still_there(submission, tmp_path):
    """The other direction. A drill that could only pass would prove nothing."""
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    log_path = tmp_path / "run.log"
    log_path.write_bytes(b"nothing sensitive\n")
    with pytest.raises(cleanup.CleanupDrillRefusal, match="target path remains"):
        cleanup.verify_synthetic_cleanup(
            target_paths=[submission["manifest_out"]],
            temporary_paths=[tmp_path / "absent.tmp"],
            log_paths=[log_path],
            forbidden_markers=[b"page-1.png"],
            volume_objects=None,
        )
