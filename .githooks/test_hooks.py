"""Behavioral smoke tests for the repository's Git hooks.

The hooks are local alarms rather than a security boundary, but an alarm that
silently rejects normal Git objects or accepts a malformed document rule is not
useful. These tests execute the tracked hook files without pushing or committing
anything.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
ZERO = "0" * 40


def run_hook(name, *, input_text="", args=(), env=None):
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        [str(HOOKS / name), *args],
        cwd=ROOT,
        env=process_env,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "pipeline/README.md",
        "pipeline/1_exemplar/HANDOFF.md",
        "history/2026-07-26_audit.md",
        ".github/pull_request_template.md",
        ".claude/agents/auditor.md",
        ".claude/skills/session-start/SKILL.md",
    ],
)
def test_document_allowlist_accepts_declared_documents(path):
    assert run_hook("doc-allowlist.sh", input_text=f"{path}\n").returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "NOTES.md",
        "NOTES.txt",
        "plan.rst",
        "session.adoc",
        "history/undated-note.md",
        ".github/notes/session.md",
        ".claude/agents/drafts/session.md",
        ".claude/skills/drafts/nested/SKILL.md",
        ".claude/skills/session-start/NOTES.md",
    ],
)
def test_document_allowlist_rejects_stray_or_undated_documents(path):
    result = run_hook("doc-allowlist.sh", input_text=f"{path}\n")
    assert result.returncode == 1
    assert path in result.stdout


def make_document_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=5)
    hooks = path / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "check-documents.sh", hooks)
    shutil.copy2(HOOKS / "doc-allowlist.sh", hooks)
    for name in (
        "README.md",
        "GOALS.md",
        "GOVERNANCE.md",
        "ARCHITECTURE.md",
        "GLOSSARY.md",
        "CLAUDE.md",
    ):
        (path / name).write_text(f"# {name}\n", encoding="utf-8")


def run_document_check(repo):
    return subprocess.run(
        ["sh", ".githooks/check-documents.sh"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_repository_document_check_covers_untracked_notes_and_dated_state(tmp_path):
    make_document_repo(tmp_path)
    assert run_document_check(tmp_path).returncode == 0

    (tmp_path / "NOTES.txt").write_text("temporary status\n", encoding="utf-8")
    note_result = run_document_check(tmp_path)
    assert note_result.returncode == 1
    assert "NOTES.txt" in note_result.stderr

    (tmp_path / "NOTES.txt").unlink()
    (tmp_path / "GOALS.md").write_text("# Goals\n\n2026-07-26 status\n", encoding="utf-8")
    dated_result = run_document_check(tmp_path)
    assert dated_result.returncode == 1
    assert "dated state" in dated_result.stderr


def run_commit_message(message):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
        handle.write(message)
        handle.flush()
        return run_hook("commit-msg", args=(handle.name,))


def test_commit_message_requires_a_real_trailer():
    invalid = run_commit_message("Describe change\n")
    valid = run_commit_message(
        "Describe change\n\nCo-Authored-By: Codex (OpenAI) <noreply@openai.com>\n"
    )
    assert invalid.returncode == 1
    assert valid.returncode == 0


def test_typed_merge_subject_does_not_bypass_attribution():
    result = run_commit_message("Merge branch 'work/example'\n")
    assert result.returncode == 1


def test_fixup_subject_keeps_the_declared_exception():
    result = run_commit_message("fixup! attributed parent\n")
    assert result.returncode == 0


def head():
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()


def parent():
    return subprocess.run(
        ["git", "rev-parse", "HEAD^"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()


def push_line(remote_ref, *, local_sha=None, remote_sha=ZERO):
    local_sha = local_sha or head()
    local_ref = remote_ref
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def run_pre_push(remote_ref, *, local_sha=None, remote_sha=ZERO):
    return run_hook(
        "pre-push",
        input_text=push_line(remote_ref, local_sha=local_sha, remote_sha=remote_sha),
        env={"ALLOW_UNAUDITED_PUSH": "1"},
    )


def test_pre_push_accepts_declared_work_branch():
    assert run_pre_push("refs/heads/work/example").returncode == 0


def test_pre_push_rejects_direct_main_and_unknown_branch_kind():
    assert run_pre_push("refs/heads/main").returncode == 1
    assert run_pre_push("refs/heads/misc/example").returncode == 1


def test_pre_push_accepts_new_tag_but_refuses_to_move_published_tag():
    assert run_pre_push("refs/tags/v0.0.0").returncode == 0
    assert run_pre_push("refs/tags/v0.0.0", remote_sha=parent()).returncode == 1


def test_pre_push_rejects_ref_without_a_declared_policy():
    assert run_pre_push("refs/notes/example").returncode == 1
