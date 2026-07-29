"""Outcome tests for the repository's local Git alarms."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
ZERO = "0" * 40
SAMPLE_SECRET = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"


def clean_env(extra=None):
    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("ALLOW_"):
            env.pop(name)
    env.update(extra or {})
    return env


def command(args, *, cwd=ROOT, stdin="", env=None, check=False):
    return subprocess.run(
        args,
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        env=clean_env(env),
        timeout=30,
        check=check,
    )


def test_static_gate_names_every_repository_shell_entrypoint():
    gate = (HOOKS / "check-static.sh").read_text()
    roots = [HOOKS, ROOT / "operations"]
    scripts = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.read_bytes().startswith(b"#!/bin/sh\n"):
                scripts.append(path.relative_to(ROOT).as_posix())
    assert scripts
    assert not [path for path in scripts if path not in gate]


def git(repo, *args, check=True, env=None):
    return command(["git", *args], cwd=repo, check=check, env=env)


def init_repo(path, branch="work/example"):
    git(path.parent, "init", "-q", "-b", branch, str(path))
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    return path


def commit_file(repo, name, text, message="fixture", env=None):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", message, env=env)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def copy_hooks(repo, *names):
    target = repo / ".githooks"
    target.mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(HOOKS / name, target / name)
    return target


def run_hook(repo, name, *, stdin="", args=(), env=None):
    return command(
        ["sh", f".githooks/{name}", *args],
        cwd=repo,
        stdin=stdin,
        env=env,
    )


def push_line(sha, remote_ref="refs/heads/work/example", remote_sha=ZERO):
    return f"{remote_ref} {sha} {remote_ref} {remote_sha}\n"


def audit_repo(path):
    repo = init_repo(path)
    base = commit_file(repo, "safe.txt", "base\n")
    head = commit_file(repo, "safe.txt", "head\n")
    copy_hooks(repo, "pre-push", "check_ingress.py", "record-audit.sh")
    return repo, base, head


def receipt_text(sha, reviewers=()):
    body = f"commit:  {sha}\nbranch:  work/example\naudited: exact commit {sha}\n"
    for reviewer in reviewers:
        body += f"\nauditor: {reviewer}\nwhen:    2026-07-28T12:00:00Z\nfinding: no findings\n"
    return body


def test_document_allowlist_has_one_clear_boundary():
    accepted = [
        "README.md",
        "pipeline/README.md",
        "pipeline/1_exemplar/HANDOFF.md",
        "history/2026-07-26_audit.md",
        ".github/pull_request_template.md",
        ".claude/agents/auditor.md",
        ".claude/skills/session-start/SKILL.md",
    ]
    rejected = [
        "NOTES.md",
        "NOTES.txt",
        "history/undated.md",
        ".github/notes/session.md",
        ".claude/skills/session-start/NOTES.md",
        "workbench/active/RUN_PLAN.md",
    ]
    for path in accepted:
        result = command(["sh", str(HOOKS / "doc-allowlist.sh")], stdin=f"{path}\n")
        assert result.returncode == 0, path
    for path in rejected:
        result = command(["sh", str(HOOKS / "doc-allowlist.sh")], stdin=f"{path}\n")
        assert result.returncode == 1, path
        assert path in result.stdout


def make_document_repo(path):
    repo = init_repo(path)
    copy_hooks(repo, "check-documents.sh", "doc-allowlist.sh", "check_ingress.py")
    for name in (
        "README.md",
        "GOALS.md",
        "GOVERNANCE.md",
        "ARCHITECTURE.md",
        "GLOSSARY.md",
        "CLAUDE.md",
    ):
        (repo / name).write_text(f"# {name}\n")
    return repo


@pytest.mark.full
def test_document_check_rejects_untracked_notes_and_dated_canonical_state(tmp_path):
    repo = make_document_repo(tmp_path / "repo")
    assert run_hook(repo, "check-documents.sh").returncode == 0

    (repo / "NOTES.md").write_text("temporary\n")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "NOTES.md" in result.stderr

    (repo / "NOTES.md").unlink()
    (repo / "GOALS.md").write_text(f"# Goals\n\n2026-07-28 {SAMPLE_SECRET}\n")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "dated state" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr


def run_commit_message(message, env=None):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
        handle.write(message)
        handle.flush()
        return command(
            ["sh", str(HOOKS / "commit-msg"), handle.name],
            env=env,
        )


@pytest.mark.full
def test_commit_message_requires_real_attribution_but_keeps_explicit_exception():
    missing = run_commit_message("ordinary change\n")
    valid = run_commit_message(
        "ordinary change\n\nCo-Authored-By: GPT (OpenAI) <noreply@openai.com>\n"
    )
    explicit = run_commit_message("human-only change\n", {"ALLOW_UNATTRIBUTED": "1"})
    assert missing.returncode == 1
    assert valid.returncode == 0
    assert explicit.returncode == 0


def test_commit_message_exceptions_never_skip_secret_scanning():
    message = f"accident {SAMPLE_SECRET}\n\nCo-Authored-By: GPT (OpenAI) <noreply@openai.com>\n"
    result = run_commit_message(message, {"ALLOW_UNATTRIBUTED": "1"})
    assert result.returncode == 1
    assert "credential" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr


def make_precommit_repo(path, branch="work/example"):
    repo = init_repo(path, branch)
    copy_hooks(repo, "pre-commit", "check_ingress.py", "doc-allowlist.sh")
    return repo


def test_pre_commit_hard_blocks_main_even_if_old_bypass_is_set(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo", "main")
    (repo / "safe.txt").write_text("safe\n")
    git(repo, "add", "safe.txt")
    blocked = run_hook(repo, "pre-commit")
    old_bypass = run_hook(repo, "pre-commit", env={"ALLOW_MAIN_COMMIT": "1"})
    assert blocked.returncode == 1
    assert "commit on main" in blocked.stderr
    assert old_bypass.returncode == 1


def test_pre_commit_checks_staged_ingress_before_doc_exception(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    (repo / "NOTES.md").write_text(SAMPLE_SECRET)
    git(repo, "add", "NOTES.md")
    result = run_hook(repo, "pre-commit", env={"ALLOW_STRAY_DOC": "1"})
    assert result.returncode == 1
    assert "ingress" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr


def test_pre_commit_rejects_stray_document_on_work_branch(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    (repo / "NOTES.md").write_text("temporary\n")
    git(repo, "add", "NOTES.md")
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1
    assert "stray documentation" in result.stderr


def test_pre_push_allows_declared_branch_and_prints_missing_review(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    result = run_hook(repo, "pre-push", stdin=push_line(head))
    assert result.returncode == 0, result.stderr
    assert "no local review receipt" in result.stderr
    assert "Checklist only" in result.stderr


def test_pre_push_hard_blocks_main_even_if_old_bypass_is_set(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    result = run_hook(
        repo,
        "pre-push",
        stdin=push_line(head, "refs/heads/main"),
        env={"ALLOW_MAIN_PUSH": "1"},
    )
    assert result.returncode == 1
    assert "direct push to main" in result.stderr


@pytest.mark.full
def test_pre_push_nonstandard_branch_requires_exact_standard_exception(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    line = push_line(head, "refs/heads/feature/example")
    assert run_hook(repo, "pre-push", stdin=line).returncode == 1
    assert run_hook(repo, "pre-push", stdin=line, env={"ALLOW_ANY_BRANCH": "1"}).returncode == 0


@pytest.mark.full
def test_pre_push_branch_deletion_requires_exact_confirmation(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    line = push_line(ZERO, remote_sha=head)
    assert run_hook(repo, "pre-push", stdin=line).returncode == 1
    assert (
        run_hook(
            repo,
            "pre-push",
            stdin=line,
            env={"ALLOW_BRANCH_DELETE": "work/example"},
        ).returncode
        == 0
    )
    assert (
        run_hook(
            repo,
            "pre-push",
            stdin=line,
            env={"ALLOW_BRANCH_DELETE": "work/other"},
        ).returncode
        == 1
    )


def test_pre_push_history_rewrite_needs_the_exact_owned_branch_exception(tmp_path):
    repo, base, head = audit_repo(tmp_path / "repo")
    wrong = run_hook(
        repo,
        "pre-push",
        stdin=push_line(base, remote_sha=head),
        env={"ALLOW_FORCE_PUSH": "1"},
    )
    exact = run_hook(
        repo,
        "pre-push",
        stdin=push_line(base, remote_sha=head),
        env={"ALLOW_FORCE_PUSH": "work/example"},
    )
    assert wrong.returncode == 1
    assert "rewrites published history" in wrong.stderr
    assert exact.returncode == 0


@pytest.mark.full
def test_pre_push_tags_are_scanned_and_immutable(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    git(repo, "tag", "-a", "safe", "-m", "safe release")
    tag = git(repo, "rev-parse", "safe").stdout.strip()
    assert run_hook(repo, "pre-push", stdin=push_line(tag, "refs/tags/safe")).returncode == 0
    assert (
        run_hook(
            repo,
            "pre-push",
            stdin=push_line(tag, "refs/tags/safe", remote_sha=tag),
        ).returncode
        == 1
    )
    assert (
        run_hook(
            repo,
            "pre-push",
            stdin=push_line(ZERO, "refs/tags/safe", remote_sha=tag),
        ).returncode
        == 1
    )

    git(repo, "tag", "-a", "unsafe", "-m", f"release {SAMPLE_SECRET}", head)
    unsafe = git(repo, "rev-parse", "unsafe").stdout.strip()
    result = run_hook(repo, "pre-push", stdin=push_line(unsafe, "refs/tags/unsafe"))
    assert result.returncode == 1
    assert SAMPLE_SECRET not in result.stdout + result.stderr


@pytest.mark.full
def test_pre_push_scans_credentials_deleted_before_tip(tmp_path):
    repo, _base, _head = audit_repo(tmp_path / "repo")
    commit_file(repo, "safe.txt", SAMPLE_SECRET, "unsafe ancestor")
    tip = commit_file(repo, "safe.txt", "clean again\n", "clean tip")
    result = run_hook(repo, "pre-push", stdin=push_line(tip))
    assert result.returncode == 1
    assert "outgoing-history" in result.stderr.lower()
    assert SAMPLE_SECRET not in result.stdout + result.stderr


@pytest.mark.full
def test_pre_push_valid_receipt_reports_distinct_names_without_gating(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    (receipts / head).write_text(receipt_text(head, ["Claude Opus", "claude  opus", "GPT Sol"]))
    result = run_hook(repo, "pre-push", stdin=push_line(head))
    assert result.returncode == 0
    assert "2 distinct reviewer(s)" in result.stderr


@pytest.mark.full
def test_pre_push_unreadable_or_invalid_receipt_is_unknown_not_zero(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    (receipts / head).mkdir()
    result = run_hook(repo, "pre-push", stdin=push_line(head))
    assert result.returncode == 0
    assert "coverage unknown" in result.stderr


@pytest.mark.full
def test_record_audit_binds_and_appends_to_exact_commit(tmp_path):
    repo, base, head = audit_repo(tmp_path / "repo")
    first = run_hook(
        repo,
        "record-audit.sh",
        args=("--commit", base, "Claude Opus", "no findings"),
    )
    second = run_hook(
        repo,
        "record-audit.sh",
        args=("--commit", base, "GPT Sol", "one suggestion"),
    )
    assert first.returncode == second.returncode == 0
    receipt = repo / ".git" / "audit-receipts" / base
    body = receipt.read_text()
    assert body.startswith(f"commit:  {base}\n")
    assert body.count("\nauditor: ") == 2
    assert not (repo / ".git" / "audit-receipts" / head).exists()
    validation = command(
        [
            "python3",
            str(HOOKS / "check_ingress.py"),
            "--audit-receipt",
            str(receipt),
        ],
        cwd=repo,
    )
    assert validation.returncode == 0


@pytest.mark.full
def test_record_audit_refuses_unknown_commit_multiline_and_secret(tmp_path):
    repo, _base, _head = audit_repo(tmp_path / "repo")
    unknown = run_hook(
        repo,
        "record-audit.sh",
        args=("--commit", "no-such", "Reviewer", "none"),
    )
    multiline = run_hook(
        repo,
        "record-audit.sh",
        args=("Reviewer\nAuditor", "none"),
    )
    secret = run_hook(
        repo,
        "record-audit.sh",
        args=("Reviewer", SAMPLE_SECRET),
    )
    assert unknown.returncode == multiline.returncode == secret.returncode == 1
    receipts = repo / ".git" / "audit-receipts"
    assert not receipts.exists() or not list(receipts.iterdir())


@pytest.mark.full
def test_record_audit_preserves_invalid_existing_receipt(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    receipt = receipts / head
    receipt.write_text("broken\n")
    result = run_hook(repo, "record-audit.sh", args=("Reviewer", "none"))
    assert result.returncode == 1
    assert receipt.read_text() == "broken\n"


@pytest.mark.full
def test_record_audit_refuses_a_concurrent_writer_without_losing_state(tmp_path):
    repo, _base, head = audit_repo(tmp_path / "repo")
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    lock = receipts / f"{head}.lock"
    lock.mkdir()
    result = run_hook(repo, "record-audit.sh", args=("Reviewer", "none"))
    assert result.returncode == 1
    assert "another writer" in result.stderr
    assert lock.is_dir()
    assert not (receipts / head).exists()


def test_install_configures_local_hooks_after_prerequisites(tmp_path):
    repo = init_repo(tmp_path / "repo")
    shutil.copytree(HOOKS, repo / ".githooks")
    for folder in (
        "workbench/active",
        "workbench/archive",
        "workbench/scratch",
        "workbench/design",
        "workbench/tools",
        "workbench/raw",
    ):
        (repo / folder).mkdir(parents=True, exist_ok=True)
    result = run_hook(repo, "install.sh")
    assert result.returncode == 0, result.stderr
    assert git(repo, "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"


def install_integration_hooks(repo):
    copy_hooks(
        repo,
        "pre-commit",
        "pre-merge-commit",
        "pre-applypatch",
        "applypatch-msg",
        "commit-msg",
        "check_ingress.py",
        "doc-allowlist.sh",
    )
    git(repo, "config", "core.hooksPath", ".githooks")


@pytest.mark.full
def test_merge_path_runs_the_same_precommit_boundary(tmp_path):
    repo = init_repo(tmp_path / "repo", "work/base")
    commit_file(repo, "base.txt", "base\n")
    git(repo, "switch", "-qc", "work/feature")
    commit_file(repo, "feature.txt", "feature\n")
    git(repo, "switch", "-q", "work/base")
    install_integration_hooks(repo)
    clean = git(repo, "merge", "--no-edit", "--no-ff", "work/feature", check=False)
    assert clean.returncode == 0, clean.stdout + clean.stderr

    git(repo, "branch", "-m", "main")
    git(repo, "switch", "-qc", "work/second")
    commit_file(
        repo,
        "second.txt",
        "second\n",
        env={"ALLOW_UNATTRIBUTED": "1"},
    )
    git(repo, "switch", "-q", "main")
    blocked = git(repo, "merge", "--no-edit", "--no-ff", "work/second", check=False)
    assert blocked.returncode != 0
    assert "commit on main" in blocked.stderr


@pytest.mark.full
def test_git_am_path_scans_patch_content_before_commit(tmp_path):
    repo = init_repo(tmp_path / "repo", "work/base")
    commit_file(repo, "base.txt", "base\n")
    git(repo, "switch", "-qc", "work/source")
    commit_file(
        repo,
        "unsafe.txt",
        SAMPLE_SECRET,
        "unsafe patch\n\nCo-Authored-By: Test <test@example.invalid>",
    )
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    git(repo, "format-patch", "-q", "-1", "HEAD", "-o", str(patch_dir))
    patch = next(patch_dir.glob("*.patch"))
    git(repo, "switch", "-q", "work/base")
    git(repo, "switch", "-qc", "work/apply")
    install_integration_hooks(repo)
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    result = git(repo, "am", str(patch), check=False)
    assert result.returncode != 0
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == before
    git(repo, "am", "--abort", check=False)
