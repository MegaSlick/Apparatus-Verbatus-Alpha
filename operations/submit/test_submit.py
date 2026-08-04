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
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.approval import build_approval_record
from common.contracts.canonical import canonical_bytes, digest_bytes, verify_self_hash
from common.contracts.errors import ApprovalRefusal
from operations.submit import cleanup, gate, inventory, submit

ROOT = Path(__file__).resolve().parents[2]


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

    report = submit.purge(
        submission["manifest_out"], gate.approved_storage_roots(submission["policy"])
    )

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
    assert result.volume_objects_seen is None, (
        "no volume applied to this drill, and reporting 0 would read identically to "
        "'a volume was checked and found empty'. Unknown is never zero (GOVERNANCE 10), "
        "which the CleanupReport handed in one line above already says"
    )


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


# --- Nothing the operator's shell captures may carry a submitted name ------------
#
# `log()` was airtight and it was not the only output. `main()` printed every
# ContractError to stderr verbatim, and three of those messages interpolated the
# submitted folder path or an entry's relative path — so an ordinary rejected
# invocation emitted exactly the values the data-handling policy excludes, into the
# channel a shell runner, a CI job or a service manager captures by default. Three
# reviewing seats found it independently; a fourth reproduced all three cases.


def run_cli(submission, source, *, manifest_out=None) -> subprocess.CompletedProcess:
    """The real CLI, the way an operator or a runner would invoke it."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "operations" / "submit" / "submit.py"),
            "--source",
            str(source),
            "--manifest-out",
            str(manifest_out or submission["manifest_out"]),
            "--approval-record",
            str(submission["approval"]),
            "--policy",
            str(submission["policy_path"]),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_a_rejected_submission_names_no_path_on_the_channel_a_runner_captures(submission):
    """Every refusal shape that knows a name: an empty folder, a symlink, and a
    non-regular entry. Each is an expected hostile input, which is what makes the
    leak reachable rather than theoretical."""
    approved = submission["approved"]

    empty = approved / "SECRET-CLIENT-ARCHIVE-2019-Q3"
    empty.mkdir()

    linked = approved / "batch-with-link"
    linked.mkdir()
    (linked / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    (linked / "CLIENT-NAME-Smith-vs-Jones.png").symlink_to("/etc/passwd")

    piped = approved / "batch-with-fifo"
    piped.mkdir()
    (piped / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    os.mkfifo(piped / "PRIVATE-CASE-42.pipe")

    secrets = [
        "SECRET-CLIENT-ARCHIVE-2019-Q3",
        "CLIENT-NAME-Smith-vs-Jones.png",
        "PRIVATE-CASE-42.pipe",
    ]
    for source in (empty, linked, piped):
        result = run_cli(submission, source, manifest_out=submission["approved"] / "m.json")
        assert result.returncode == 2, result.stderr
        assert result.stderr.strip(), "a refusal still has to say why"
        for secret in secrets:
            assert secret not in result.stderr, (
                f"{secret!r} reached stderr; the data-handling policy's logging rule "
                "excludes a declared filename or path from operational output"
            )
            assert secret not in result.stdout


def test_the_refusal_still_says_what_went_wrong(submission):
    """Redaction that removed the reason as well as the name would trade one silent
    loss for another (GOVERNANCE 2)."""
    linked = submission["approved"] / "with-a-link"
    linked.mkdir()
    (linked / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    (linked / "elsewhere.png").symlink_to("/etc/passwd")

    manifest_out = submission["approved"] / "m.json"
    result = run_cli(submission, linked, manifest_out=manifest_out)
    assert "symlink" in result.stderr
    assert "named in" in result.stderr


def test_the_refusal_report_names_the_entry_the_terminal_withholds(submission):
    """Ruling 2026-08-04, item 1: the terminal never repeats a submitted name, but
    the name is not lost — it is written to a private report inside the same
    approved storage root the manifest itself would have used, and the terminal
    points at that report rather than saying the name is merely withheld with no
    way to ever find it."""
    linked = submission["approved"] / "with-a-link-2"
    linked.mkdir()
    (linked / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    (linked / "elsewhere.png").symlink_to("/etc/passwd")

    manifest_out = submission["approved"] / "m2.json"
    result = run_cli(submission, linked, manifest_out=manifest_out)
    assert result.returncode == 2
    report_path = manifest_out.parent / f"{manifest_out.name}.refusal.json"
    assert str(report_path) in result.stderr
    assert "elsewhere.png" not in result.stderr

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["entries"][0]["entry"] == "elsewhere.png"
    assert "symlink" in report["entries"][0]["reason"]


def test_a_refusal_still_carries_the_name_for_a_caller_allowed_to_see_one(submission):
    """The name is not destroyed, only kept off the log channel."""
    linked = submission["approved"] / "carrying-a-link"
    linked.mkdir()
    (linked / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    (linked / "nested").mkdir()
    (linked / "nested" / "target.png").symlink_to("/etc/passwd")

    with pytest.raises(inventory.SubmissionInputError) as caught:
        inventory.read_submission(linked, max_bytes=0)
    assert caught.value.entry == "nested/target.png"
    assert "target.png" not in str(caught.value)


# --- Evidence is never overwritten (GOVERNANCE 4) --------------------------------


def test_resubmitting_changed_content_to_one_path_refuses_rather_than_replacing(submission):
    """`os.replace` clobbered unconditionally, so a changed folder replaced a valid,
    self-hashed record of what was previously sealed — no comparison, no warning,
    nothing on disk retaining what it superseded. "Sealed" then meant only
    "self-consistent now"."""
    first = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        approval_record=submission["approval"],
        policy_path=submission["policy_path"],
    )
    sealed = submission["manifest_out"].read_bytes()

    (submission["folder"] / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\nsomething else")
    with pytest.raises(submit.SubmitRefusal, match="Evidence is never overwritten"):
        submit.submit(
            submission["folder"],
            submission["manifest_out"],
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert submission["manifest_out"].read_bytes() == sealed, "the existing record was not touched"
    assert json.loads(sealed)["self_hash"] == first["self_hash"]


def test_resubmitting_identical_content_to_one_path_is_still_a_true_no_op(submission):
    """Immutability that refused an unchanged resubmission would have broken
    idempotence to fix an overwrite."""
    for _ in range(2):
        submit.submit(
            submission["folder"],
            submission["manifest_out"],
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert submission["manifest_out"].exists()


# --- The cleanup drill's two halves agree about what "gone" means ----------------


def test_purge_reports_a_dangling_symlink_as_present_because_it_is(submission, tmp_path):
    """`Path.exists()` follows the link, so a manifest that was a symlink to
    something already gone read as absent: `purge` skipped the unlink, reported
    `target_removed=True` and logged `status=target-absent` while the entry sat on
    disk — and `cleanup._is_absent`, verifying the same drill, called that a
    failure."""
    target = submission["approved"] / "submission.json"
    target.symlink_to(submission["approved"] / "never-existed.json")
    assert not target.exists() and target.is_symlink()

    report = submit.purge(target, gate.approved_storage_roots(submission["policy"]))
    assert report.target_removed is True
    assert not target.is_symlink(), "the directory entry itself was removed"
    cleanup.verify_synthetic_cleanup(
        target_paths=[target],
        temporary_paths=[tmp_path / "absent.tmp"],
        log_paths=[_scratch_log(tmp_path)],
        forbidden_markers=[b"page-1.png"],
        volume_objects=None,
    )


def test_purge_refuses_a_target_outside_the_approved_storage_roots(submission, tmp_path):
    """`submit()` refuses to *write* outside them; deleting outside them was never
    checked at all, and the check may not be one a caller can decline to pass."""
    stray = tmp_path / "somewhere-else.json"
    stray.write_text("{}", encoding="utf-8")
    roots = gate.approved_storage_roots(submission["policy"])
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        submit.purge(stray, roots)
    assert stray.exists(), "a refused cleanup removes nothing"


def test_purge_removing_a_link_is_not_entering_through_one(submission):
    """The containment check is on the parent directory, not the entry. Resolving
    the entry would refuse a symlink outright — and refuse to clean up the exact
    case this function was repaired for. Unlinking a name inside an approved
    directory removes that name and cannot reach what it pointed at."""
    victim = submission["approved"] / "keep-me.json"
    victim.write_text("{}", encoding="utf-8")
    link = submission["approved"] / "submission.json"
    link.symlink_to(victim)

    submit.purge(link, gate.approved_storage_roots(submission["policy"]))
    assert not link.is_symlink(), "the link entry was removed"
    assert victim.exists(), "and what it pointed at was not touched"


def _scratch_log(tmp_path):
    path = tmp_path / "drill.log"
    path.write_bytes(b"nothing sensitive\n")
    return path


# --- The walk is bounded in every direction an attacker shapes -------------------


def test_a_deeply_nested_submission_is_a_named_refusal_and_not_a_recursion_error(submission):
    """2,000 nested directories escaped as a RecursionError and CPython's exit 1,
    straight past the ContractError handler — so the tool failed loudly and told the
    caller nothing it could act on, with a traceback for a message."""
    deep = submission["approved"] / "deep"
    current = deep
    for index in range(inventory.MAX_DIRECTORY_DEPTH + 5):
        current = current / f"d{index}"
    current.mkdir(parents=True)
    (current / "page.png").write_bytes(b"\x89PNG\r\n\x1a\ndeep")

    with pytest.raises(inventory.SubmissionInputError, match="nests deeper than"):
        inventory.read_submission(deep, max_bytes=0)

    result = run_cli(submission, deep, manifest_out=submission["approved"] / "m.json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_the_aggregate_file_count_is_bounded_not_only_the_per_file_size(monkeypatch, submission):
    """`max_bytes` bounded one file and nothing else: no accumulator across files, no
    count, no per-directory entry cap. A folder of sub-limit files exhausted memory
    before any named refusal — 150 MB of 1 MiB files held 149 MB of RSS at once."""
    monkeypatch.setattr(inventory, "MAX_SUBMITTED_FILES", 3)
    many = submission["approved"] / "many"
    many.mkdir()
    for index in range(5):
        (many / f"page-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(inventory.SubmissionInputError, match="more than 3 files"):
        inventory.read_submission(many, max_bytes=0)


def test_the_aggregate_retained_bytes_are_bounded(monkeypatch, submission):
    monkeypatch.setattr(inventory, "MAX_SUBMITTED_BYTES", 8)
    many = submission["approved"] / "bytes"
    many.mkdir()
    for index in range(4):
        (many / f"page-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\n" * 2)
    with pytest.raises(inventory.SubmissionInputError, match="aggregate limit"):
        inventory.read_submission(many, max_bytes=1024)


def test_the_submit_tool_retains_no_file_content_at_all(submission):
    """It writes paths, digests and sizes and never looks at what a file holds — so
    the second hand-kept copy of the door's 64 MiB limit that used to live here is
    gone rather than merely renamed."""
    assert submit.RETAIN_NO_BYTES == 0
    sources = inventory.read_submission(submission["folder"], max_bytes=submit.RETAIN_NO_BYTES)
    assert sources and all(source.data is None for source in sources)
    assert all(len(source.sha256) == 64 and source.size > 0 for source in sources)


# --- what an adversarial pass over the repair itself found -----------------------


def test_the_manifest_is_not_written_through_a_symlink_planted_at_its_temp_path(submission):
    """The temp name is derived from the manifest name and the pid, so it is
    guessable. `open(temporary, "wb")` followed a link planted there: the manifest
    bytes went wherever it pointed — outside every approved storage root — with
    `os.link` then failing and the write already done."""
    victim = submission["approved"].parent / "victim-outside-the-approved-roots.json"
    target = submission["approved"] / "sealed.json"
    (submission["approved"] / f".{target.name}.tmp-{os.getpid()}").symlink_to(victim)

    with pytest.raises(submit.SubmitRefusal, match="could not be written"):
        submit.submit(
            submission["folder"],
            target,
            approval_record=submission["approval"],
            policy_path=submission["policy_path"],
        )
    assert not victim.exists(), "nothing was written through the link"
    assert not target.exists(), "and nothing was sealed"


def test_a_manifest_name_at_the_filesystem_limit_refuses_rather_than_crashing(submission):
    """The temp name adds twelve bytes, so a legal 250-byte manifest name overflows
    NAME_MAX. The `except OSError` caught that — and then the `finally` called
    `is_symlink()` on the same over-length name, which raises rather than returning
    False, and an exception from a `finally` supersedes the one in flight. The named
    refusal was demoted to `__context__` and a raw OSError escaped as a traceback."""
    result = run_cli(
        submission,
        submission["folder"],
        manifest_out=submission["approved"] / ("N" * 245 + ".json"),
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_purge_sweeps_the_temp_files_of_a_manifest_whose_name_holds_glob_syntax(submission):
    """`Ledger [1948].json` is an ordinary archival name and `[1948]` is a character
    class. The writer names the temp file literally and the sweeper globbed it, so
    they disagreed: a temp file holding real page bytes survived, and
    `remaining_temp_files` recomputed with the same broken pattern and reported
    `()` — a clean drill that never happened."""
    target = submission["approved"] / "Ledger [1948].json"
    target.write_text("{}", encoding="utf-8")
    stray = submission["approved"] / f".{target.name}.tmp-31337"
    stray.write_bytes(b"\x89PNG crashed write holding real page bytes")

    report = submit.purge(target, gate.approved_storage_roots(submission["policy"]))
    assert report.temp_files_removed == 1
    assert not stray.exists()
    assert report.remaining_temp_files == ()


def test_a_submitted_name_that_is_not_valid_utf8_is_a_named_refusal(submission):
    """`os.listdir` surrogate-escapes bytes that are not valid UTF-8, and a
    surrogate cannot be encoded again — so the name reached the manifest's canonical
    encoding and escaped as a UnicodeEncodeError traceback, exit 1, printing the
    path on the way out."""
    folder = submission["approved"] / "odd-encoding"
    folder.mkdir()
    (folder / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    # Not every filesystem will hold this name. Linux and ext4 — where the chambers
    # run — accept arbitrary bytes; macOS and APFS reject anything that is not valid
    # UTF-8 with `OSError: Illegal byte sequence`, so the test cannot construct its
    # own subject there and dies before reaching the behaviour it is checking.
    #
    # Skipping is honest and refusing to run is not a pass: the refusal path this
    # covers is real on the deployment target, and a machine that cannot even create
    # the name cannot be reached by it. Written as an attempt rather than a platform
    # check so it follows the filesystem rather than a guess about which one is
    # mounted.
    try:
        os.close(os.open(bytes(folder) + b"/LEAK\xff\xfeNAME.png", os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError as error:
        pytest.skip(
            f"this filesystem refuses a non-UTF-8 filename, so the case cannot exist here: {error}"
        )

    with pytest.raises(inventory.SubmissionInputError, match="not valid UTF-8"):
        inventory.read_submission(folder, max_bytes=0)

    result = run_cli(submission, folder, manifest_out=submission["approved"] / "m.json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
