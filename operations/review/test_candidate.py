import json
import subprocess

import pytest

from operations.review.candidate import prepare, receipt


def git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(tmp_path):
    git(tmp_path, "init", "--quiet", "-b", "work/test")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text("workbench/\n")
    (tmp_path / "file.txt").write_text("base\n")
    git(tmp_path, "add", ".gitignore", "file.txt")
    git(tmp_path, "commit", "--quiet", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "file.txt").write_text("candidate\n")
    git(tmp_path, "add", "file.txt")
    git(tmp_path, "commit", "--quiet", "-m", "candidate")
    return base, git(tmp_path, "rev-parse", "HEAD")


def test_prepare_names_the_exact_clean_candidate(tmp_path):
    base, candidate = repository(tmp_path)
    manifest = prepare(tmp_path, base)
    assert manifest["candidate"] == candidate
    assert manifest["base"] == base
    assert len(manifest["tree"]) == 40


def test_prepare_refuses_a_moving_index(tmp_path):
    base, _candidate = repository(tmp_path)
    (tmp_path / "file.txt").write_text("moving\n")
    try:
        prepare(tmp_path, base)
    except ValueError as error:
        assert "dirty" in str(error)
    else:
        raise AssertionError("a dirty candidate was prepared")


def test_prepare_ignores_local_replace_refs(tmp_path):
    base, candidate = repository(tmp_path)
    canonical_tree = git(tmp_path, "rev-parse", f"{candidate}^{{tree}}")
    (tmp_path / "file.txt").write_text("replacement\n")
    git(tmp_path, "add", "file.txt")
    replacement_tree = git(tmp_path, "write-tree")
    replacement = subprocess.run(
        ["git", "-C", str(tmp_path), "commit-tree", replacement_tree, "-p", base],
        input="replacement view\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(tmp_path, "replace", candidate, replacement)
    subprocess.run(
        ["git", "-C", str(tmp_path), "reset", "--quiet", "--hard", candidate],
        check=True,
        env={**__import__("os").environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    )

    manifest = prepare(tmp_path, base)

    assert manifest["candidate"] == candidate
    assert manifest["tree"] == canonical_tree


def test_receipt_binds_the_report_and_refuses_a_moved_head(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Candidate: {candidate}\nBase: {base}\nNo findings.\n")
    output, record = receipt(tmp_path, candidate, base, "Independent Sol", report)
    assert json.loads(output.read_text()) == record
    assert record["candidate"] == candidate
    assert len(record["report_sha256"]) == 64

    git(tmp_path, "commit", "--quiet", "--allow-empty", "-m", "moved")
    try:
        receipt(tmp_path, candidate, base, "Independent Opus", report)
    except ValueError as error:
        assert "not candidate" in str(error)
    else:
        raise AssertionError("a receipt was written after HEAD moved")


def test_receipt_requires_the_full_candidate_sha_in_the_report(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Candidate: {candidate[:8]}\nBase: {base}\n")
    with pytest.raises(ValueError, match="exact full Candidate SHA"):
        receipt(tmp_path, candidate, base, "CodeRabbit", report)


def test_receipt_requires_the_exact_full_base_sha_in_the_report(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Candidate: {candidate}\nBase: {base[:8]}\n")

    with pytest.raises(ValueError, match="exact full Base SHA"):
        receipt(tmp_path, candidate, base, "CodeRabbit", report)


def test_receipt_refuses_a_symlinked_report(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    target = report.with_name("actual-review.md")
    target.write_text(f"Candidate: {candidate}\nBase: {base}\n")
    report.symlink_to(target)

    with pytest.raises(ValueError, match="cannot be opened safely"):
        receipt(tmp_path, candidate, base, "Independent Sol", report)


def test_receipt_refuses_an_empty_report(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_bytes(b" \n\t")

    with pytest.raises(ValueError, match="empty"):
        receipt(tmp_path, candidate, base, "Independent Sol", report)


def test_receipt_refuses_a_base_outside_the_candidate_history(tmp_path):
    base, candidate = repository(tmp_path)
    git(tmp_path, "switch", "--quiet", "--orphan", "unrelated")
    (tmp_path / "other.txt").write_text("other\n")
    git(tmp_path, "add", "other.txt")
    git(tmp_path, "commit", "--quiet", "-m", "other")
    unrelated = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "switch", "--quiet", "work/test")
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Candidate: {candidate}\nBase: {unrelated}\n")

    with pytest.raises(ValueError, match="is not an ancestor"):
        receipt(tmp_path, candidate, unrelated, "Independent Sol", report)


def test_receipt_distinguishes_an_unknown_base(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Candidate: {candidate}\nBase: {base}\n")

    with pytest.raises(ValueError, match="unknown or is not a commit"):
        receipt(tmp_path, candidate, "does-not-exist", "Independent Sol", report)


def test_receipt_is_idempotent_but_cannot_replace_existing_evidence(tmp_path):
    base, candidate = repository(tmp_path)
    first = tmp_path / "workbench" / "raw" / "first.md"
    second = first.with_name("second.md")
    first.parent.mkdir(parents=True)
    body = f"Candidate: {candidate}\nBase: {base}\nNo findings.\n"
    first.write_text(body)
    second.write_text(body)

    output, original = receipt(tmp_path, candidate, base, "Independent Sol", first)
    repeated, record = receipt(tmp_path, candidate, base, "Independent Sol", first)
    assert repeated == output and record == original

    with pytest.raises(ValueError, match="immutable receipt already exists"):
        receipt(tmp_path, candidate, base, "Independent Sol", second)
    assert json.loads(output.read_text()) == original


def test_receipt_refuses_a_preexisting_symlink_at_its_final_name(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Candidate: {candidate}\nBase: {base}\n")
    digest = __import__("hashlib").sha256(report.read_bytes()).hexdigest()
    output = (
        tmp_path / "workbench" / "raw" / "reviews" / candidate / f"independent-sol-{digest}.json"
    )
    output.parent.mkdir(parents=True)
    victim = output.with_name("victim")
    victim.write_text("keep\n")
    output.symlink_to(victim)

    with pytest.raises(ValueError, match="opened safely"):
        receipt(tmp_path, candidate, base, "Independent Sol", report)
    assert victim.read_text() == "keep\n"
