import json
import subprocess

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


def test_receipt_binds_the_report_and_refuses_a_moved_head(tmp_path):
    base, candidate = repository(tmp_path)
    report = tmp_path / "workbench" / "raw" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text(f"Reviewed candidate {candidate}\nNo findings.\n")
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
    report.write_text(f"Reviewed {candidate[:8]}\n")
    try:
        receipt(tmp_path, candidate, base, "CodeRabbit", report)
    except ValueError as error:
        assert "full candidate SHA" in str(error)
    else:
        raise AssertionError("a report naming only a short SHA was accepted")


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
    report.write_text(f"Reviewed candidate {candidate} from base {base}\n")

    try:
        receipt(tmp_path, candidate, unrelated, "Independent Sol", report)
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("a receipt accepted a base outside candidate history")
