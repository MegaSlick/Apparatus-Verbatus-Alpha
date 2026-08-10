"""The submit door: sealed once, and silent about what it saw.

Spec 03 asks three things of this tool that are testable here. Upload completion is
explicit and sealed — a partial transfer can never look admitted. The storage-root
check is enforced before a byte is hashed, because this is the first place a real
folder is ever touched. And the logging rule is mechanical rather than a promise: a
log line may carry counts, digests and status words, and it may never carry a name,
a path, or image bytes.

**Cut 2026-08-09, per Tyrel's ruling that session.** A submission used to also need
a current data-gate approval-record artifact, verified before a byte was hashed and
named in the sealed manifest's `authorized_by` field. His ruling: none of this
material ever reaches git regardless of any such sign-off, so the requirement bought
nothing and is gone; the manifest no longer carries an authorization reference.

Spec 03's test 6 — the cleanup drill on synthetic material, with declared bounds —
runs against `purge` here and against `cleanup.verify_synthetic_cleanup` beside it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from operations.submit import cleanup, gate, inventory, submit

ROOT = Path(__file__).resolve().parents[2]


def approved_policy(tmp_path, roots):
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(root) for root in roots]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return policy, path


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
        "manifest_out": approved / "submission.json",
    }


# --- The sealed manifest ---------------------------------------------------------


def test_a_submission_seals_one_self_hashed_manifest_naming_every_file(submission):
    manifest = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )

    assert manifest["schema"] == submit.SCHEMA
    assert [entry["relative_path"] for entry in manifest["files"]] == [
        "nested/page-2.png",
        "page-1.png",
    ]
    assert verify_self_hash(manifest)
    assert json.loads(submission["manifest_out"].read_text(encoding="utf-8")) == manifest


def test_the_manifest_digests_are_the_real_digests_of_the_real_bytes(submission):
    manifest = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    for entry in manifest["files"]:
        data = (submission["folder"] / entry["relative_path"]).read_bytes()
        assert entry["sha256"] == digest_bytes(data)
        assert entry["bytes"] == len(data)


def test_a_manifest_loads_only_when_its_canonical_self_hashed_filename_ledger_is_sound(
    submission,
):
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )

    loaded = submit.load_manifest(submission["manifest_out"])

    assert loaded["files"][0]["relative_path"] == "nested/page-2.png"
    assert loaded["files"][0]["sha256"] == digest_bytes(
        (submission["folder"] / "nested" / "page-2.png").read_bytes()
    )


def test_a_manifest_with_unsorted_or_duplicate_filename_rows_is_refused_before_ingress(submission):
    manifest = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    manifest["files"] = [manifest["files"][1], manifest["files"][0]]
    manifest["self_hash"] = self_hash(manifest)

    with pytest.raises(submit.SubmitRefusal, match="sorted unique"):
        submit.validate_manifest(manifest)


def test_submitting_the_same_folder_twice_seals_identical_bytes(submission):
    first = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    second = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    assert canonical_bytes(first) == canonical_bytes(second)


def test_an_empty_folder_is_a_loud_failure_rather_than_an_empty_submission(submission, tmp_path):
    empty = submission["approved"] / "empty"
    empty.mkdir()
    with pytest.raises(submit.SubmissionRefusal, match="0 source refusal"):
        submit.submit(
            empty,
            submission["manifest_out"],
            policy_path=submission["policy_path"],
        )
    assert not submission["manifest_out"].exists()


# --- The storage-root gate is checked before a byte is read ----------------------


def test_a_folder_outside_the_approved_storage_roots_is_refused(submission, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "page.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        submit.submit(
            elsewhere,
            submission["manifest_out"],
            policy_path=submission["policy_path"],
        )
    assert not submission["manifest_out"].exists()


def test_a_manifest_outside_the_approved_storage_roots_is_refused(submission, tmp_path):
    outside = tmp_path / "outside" / "submission.json"
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        submit.submit(
            submission["folder"],
            outside,
            policy_path=submission["policy_path"],
        )
    assert not outside.exists()


def test_the_manifest_cannot_be_written_inside_the_folder_it_inventories(submission):
    inside = submission["folder"] / "submission.json"
    with pytest.raises(submit.SubmitRefusal, match="inside the submitted folder"):
        submit.submit(
            submission["folder"],
            inside,
            policy_path=submission["policy_path"],
        )
    assert not inside.exists()


def test_a_private_refusal_report_cannot_be_written_inside_the_folder_it_inventories(
    submission,
):
    report_inside = submission["folder"] / "refusals.json"
    with pytest.raises(submit.SubmitRefusal, match="private refusal report cannot be written"):
        submit.submit(
            submission["folder"],
            submission["manifest_out"],
            policy_path=submission["policy_path"],
            refusal_report_out=report_inside,
        )
    assert not report_inside.exists()


# --- The logging rule, made mechanical -------------------------------------------


def test_a_log_line_may_carry_counts_and_digests(capsys):
    submit.log("submission sealed", files=2, digest="a" * 64)
    assert capsys.readouterr().out.strip() == f"submission sealed: digest={'a' * 64} files=2"


def test_a_terminal_log_line_may_never_carry_image_bytes_or_unstructured_fields():
    for field in ("path", "filename", "relative_path", "image", "content"):
        with pytest.raises(submit.SubmitRefusal, match="sealed record"):
            submit.log("event", **{field: "page-1.png"})


def test_a_log_name_cannot_be_smuggled_through_the_event_or_an_allowed_field():
    with pytest.raises(submit.SubmitRefusal, match="event"):
        submit.log("page-1.png", files=1)
    with pytest.raises(submit.SubmitRefusal, match="status"):
        submit.log("submission sealed", status="page-1.png")
    with pytest.raises(submit.SubmitRefusal, match="digest"):
        submit.log("submission sealed", digest="page-1.png")


def test_no_log_line_the_tool_actually_emits_names_a_submitted_file(submission, capsys):
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    printed = capsys.readouterr().out
    assert printed.strip()
    for name in ("page-1.png", "page-2.png", "nested", str(submission["folder"])):
        assert name not in printed


# --- Retention and the synthetic cleanup drill -------------------------------------


def test_purge_refuses_to_delete_a_submission_before_a_sealed_run_terminal_condition(submission):
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    with pytest.raises(submit.SubmitRefusal, match="sealed dead/broken or complete/exported"):
        submit.purge(submission["manifest_out"], gate.approved_storage_roots(submission["policy"]))
    assert submission["manifest_out"].exists()


def test_the_cleanup_drill_fails_when_the_target_is_still_there(submission, tmp_path):
    """The other direction. A drill that could only pass would prove nothing."""
    submit.submit(
        submission["folder"],
        submission["manifest_out"],
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


# --- Refusal records keep filenames; terminals point at the private report --------


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
            "--policy",
            str(submission["policy_path"]),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_a_rejected_submission_writes_a_private_named_refusal_report(submission):
    """A filename stays in the record, while the terminal gives report location."""
    approved = submission["approved"]
    linked = approved / "batch-with-link"
    linked.mkdir()
    (linked / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    (linked / "CLIENT-NAME-Smith-vs-Jones.png").symlink_to("/etc/passwd")
    manifest_out = approved / "m.json"

    result = run_cli(submission, linked, manifest_out=manifest_out)

    report_path = manifest_out.with_suffix(".refusals.json")
    assert result.returncode == 2, result.stderr
    assert "1 source refusal(s)" in result.stderr
    assert str(report_path) in result.stderr
    assert "CLIENT-NAME-Smith-vs-Jones.png" not in result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert verify_self_hash(report)
    assert report["refusals"] == [
        {
            "relative_path": "CLIENT-NAME-Smith-vs-Jones.png",
            "reason": "the submission contains a symlink; only plain files and directories may be submitted",
        }
    ]


def test_the_refusal_still_says_what_went_wrong(submission):
    """The terminal names the report and the report carries the exact source/reason."""
    linked = submission["approved"] / "with-a-link"
    linked.mkdir()
    (linked / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nreal")
    (linked / "elsewhere.png").symlink_to("/etc/passwd")

    result = run_cli(submission, linked, manifest_out=submission["approved"] / "m.json")
    assert "private refusal report" in result.stderr
    report = json.loads((submission["approved"] / "m.refusals.json").read_text(encoding="utf-8"))
    assert "symlink" in report["refusals"][0]["reason"]


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


def test_distinct_refusals_at_one_manifest_location_keep_both_named_reports(submission):
    """A retry may not turn a second refused filename into an unrecorded error."""
    first_source = submission["approved"] / "first-refusal"
    second_source = submission["approved"] / "second-refusal"
    for folder, name in ((first_source, "first-link.png"), (second_source, "second-link.png")):
        folder.mkdir()
        (folder / name).symlink_to("/etc/passwd")

    with pytest.raises(submit.SubmissionRefusal) as first:
        submit.submit(
            first_source,
            submission["manifest_out"],
            policy_path=submission["policy_path"],
        )
    with pytest.raises(submit.SubmissionRefusal) as second:
        submit.submit(
            second_source,
            submission["manifest_out"],
            policy_path=submission["policy_path"],
        )

    assert first.value.report_path == submission["manifest_out"].with_suffix(".refusals.json")
    assert second.value.report_path != first.value.report_path
    assert second.value.report_path.name.startswith("submission.refusals.")
    first_report = json.loads(first.value.report_path.read_text(encoding="utf-8"))
    second_report = json.loads(second.value.report_path.read_text(encoding="utf-8"))
    assert verify_self_hash(first_report) and verify_self_hash(second_report)
    assert [row["relative_path"] for row in first_report["refusals"]] == ["first-link.png"]
    assert [row["relative_path"] for row in second_report["refusals"]] == ["second-link.png"]


def test_a_submit_budget_alarm_writes_the_source_name_to_its_private_report(
    monkeypatch, submission
):
    """Aggregate limits are alarms about a particular next source, not empty reports."""
    monkeypatch.setattr(inventory, "MAX_SUBMITTED_FILES", 1)
    source = submission["approved"] / "over-budget"
    source.mkdir()
    (source / "first.png").write_bytes(b"first")
    (source / "second.png").write_bytes(b"second")

    with pytest.raises(submit.SubmissionRefusal) as refused:
        submit.submit(
            source,
            submission["manifest_out"],
            policy_path=submission["policy_path"],
        )

    assert refused.value.refusal_count == 1
    report = json.loads(refused.value.report_path.read_text(encoding="utf-8"))
    assert report["refusals"][0]["relative_path"] == "second.png"
    assert "more than 1 files" in report["refusals"][0]["reason"]


# --- Evidence is never overwritten (GOVERNANCE 4) --------------------------------


def test_resubmitting_changed_content_to_one_path_refuses_rather_than_replacing(submission):
    """`os.replace` clobbered unconditionally, so a changed folder replaced a valid,
    self-hashed record of what was previously sealed — no comparison, no warning,
    nothing on disk retaining what it superseded. "Sealed" then meant only
    "self-consistent now"."""
    first = submit.submit(
        submission["folder"],
        submission["manifest_out"],
        policy_path=submission["policy_path"],
    )
    sealed = submission["manifest_out"].read_bytes()

    (submission["folder"] / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\nsomething else")
    with pytest.raises(submit.SubmitRefusal, match="Evidence is never overwritten"):
        submit.submit(
            submission["folder"],
            submission["manifest_out"],
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
            policy_path=submission["policy_path"],
        )
    assert submission["manifest_out"].exists()


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
    with pytest.raises(inventory.SubmissionInputError, match="more than 3 files") as caught:
        inventory.read_submission(many, max_bytes=0)
    assert caught.value.entry == "page-3.png"


def test_the_aggregate_retained_bytes_are_bounded(monkeypatch, submission):
    monkeypatch.setattr(inventory, "MAX_SUBMITTED_BYTES", 8)
    many = submission["approved"] / "bytes"
    many.mkdir()
    for index in range(4):
        (many / f"page-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\n" * 2)
    with pytest.raises(inventory.SubmissionInputError, match="aggregate limit") as caught:
        inventory.read_submission(many, max_bytes=1024)
    assert caught.value.entry == "page-0.png"


def test_the_submit_tool_retains_no_file_content_at_all(submission):
    """It writes paths, digests and sizes and never looks at what a file holds — so
    the second hand-kept copy of the door's 64 MiB limit that used to live here is
    gone rather than merely renamed."""
    assert submit.RETAIN_NO_BYTES == 0
    sources = inventory.read_submission(submission["folder"], max_bytes=submit.RETAIN_NO_BYTES)
    assert sources and all(source.data is None for source in sources)
    assert all(len(source.sha256) == 64 and source.size > 0 for source in sources)


# --- what an adversarial pass over the repair itself found -----------------------


def test_the_manifest_temp_name_is_unpredictable_and_never_reused(submission):
    """The temp name used to be `.{manifest}.tmp-{pid}` — guessable, so a link could
    be planted at it, and *reusable*, so a crash or a recycled pid left that exact
    name behind and wedged every later submission to the same manifest path behind
    the generic "could not be written". `mkstemp` picks an unpredictable name per
    attempt, so neither the plant nor the wedge has a name to aim at."""
    target = submission["approved"] / "sealed.json"
    stale = submission["approved"] / f".{target.name}.tmp-{os.getpid()}"
    stale.write_bytes(b"a temporary file a dead run left behind")

    assert submit.submit(
        submission["folder"],
        target,
        policy_path=submission["policy_path"],
    )
    assert target.exists(), "a stale temporary must not block a fresh submission"
    assert stale.read_bytes() == b"a temporary file a dead run left behind"
    leftovers = [
        path
        for path in submission["approved"].iterdir()
        if path.name.startswith(f".{target.name}.tmp-") and path != stale
    ]
    assert leftovers == [], f"the run's own temporary was not removed: {leftovers}"


def test_the_manifest_is_not_written_through_a_symlink_planted_at_its_temp_path(
    monkeypatch, submission
):
    """`open(temporary, "wb")` followed a link planted at the temp path: the manifest
    bytes went wherever it pointed — outside every approved storage root — with
    `os.link` then failing and the write already done. The name is unguessable now,
    so the plant is forced here by pinning it; what must still hold is that a link
    sitting at the chosen name takes no bytes and seals nothing."""
    victim = submission["approved"].parent / "victim-outside-the-approved-roots.json"
    target = submission["approved"] / "sealed.json"
    planted = submission["approved"] / f".{target.name}.tmp-planted"
    planted.symlink_to(victim)

    def _mkstemp_at_the_planted_name(prefix, dir):  # noqa: A002 - mkstemp's own name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        return os.open(planted, flags, 0o600), str(planted)

    monkeypatch.setattr(
        submit.tempfile, "mkstemp", lambda prefix, dir: _mkstemp_at_the_planted_name(prefix, dir)
    )

    with pytest.raises(submit.SubmitRefusal, match="could not be written"):
        submit.submit(
            submission["folder"],
            target,
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


def test_a_successful_manifest_with_an_unremoved_temp_is_not_reported_complete(
    monkeypatch, tmp_path
):
    """A successful link does not make a retained temporary file disappear."""
    target = tmp_path / "manifest.json"
    original_unlink = Path.unlink

    def fail_temporary_unlink(path, *, missing_ok=False):
        if path.name.startswith(".manifest.json.tmp-"):
            raise OSError("synthetic cleanup failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)

    with pytest.raises(submit.SubmitRefusal, match="temporary file could not be removed"):
        submit._atomic_create(target, b"synthetic manifest")
    assert target.read_bytes() == b"synthetic manifest"


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

    raw_name = b"LEAK\xff\xfeNAME.png"
    with pytest.raises(inventory.SubmissionInputError, match="not valid UTF-8") as caught:
        inventory.read_submission(folder, max_bytes=0)
    assert isinstance(caught.value.entry, inventory.RawPathReference)
    assert bytes.fromhex(caught.value.entry.raw_bytes_hex) == raw_name

    result = run_cli(submission, folder, manifest_out=submission["approved"] / "m.json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    report = json.loads((submission["approved"] / "m.refusals.json").read_text(encoding="utf-8"))
    assert report["refusals"] == [
        caught.value.entry.to_refusal_record(
            "a submitted name is not valid UTF-8; the manifest is canonical JSON and cannot record a name it cannot encode"
        )
    ]
    assert raw_name.hex() not in result.stderr
