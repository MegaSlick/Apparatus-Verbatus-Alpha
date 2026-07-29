"""Behavioral smoke tests for the repository's Git hooks.

The hooks are local alarms rather than a security boundary, but an alarm that
silently rejects normal Git objects or accepts a malformed document rule is not
useful. These tests execute the tracked hook files without pushing or committing
anything.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
ZERO = "0" * 40
ZERO_SHA256 = "0" * 64


def clean_hook_env():
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("ALLOW_"):
            environment.pop(name)
    return environment


def run_hook(name, *, input_text="", args=(), env=None):
    process_env = clean_hook_env()
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
        # Not a workbench exception — this matches the older `*/HANDOFF.md` rule,
        # written for the pipeline stages. A session handoff merely shares the
        # filename, and .gitignore is what actually keeps it out of the tree.
        # Two artefacts under one name is the collision recorded as T18.
        "workbench/active/HANDOFF.md",
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
        # The whole drawer is refused again. active/ was tracked briefly so a
        # reviewer could read the run plan beside the code; it answered that
        # question and went back to being a drawer. These are task files that
        # session-end moves into archive/ when the work closes.
        #
        # Not listed here: any `*/HANDOFF.md`, which the older pipeline-stage
        # rule still matches wherever it sits. That is T18, and .gitignore is
        # what keeps those out.
        "workbench/active/RUN_PLAN.md",
        "workbench/active/AUDIT_LEDGER.md",
        "workbench/active/reviews-2026-07-27/DISPOSITION.md",
        "workbench/active/a/b/deep.md",
        "workbench/raw/run.md",
        "workbench/scratch/disposable.md",
        "workbench/design/iterative_reader.md",
    ],
)
def test_document_allowlist_rejects_stray_or_undated_documents(path):
    result = run_hook("doc-allowlist.sh", input_text=f"{path}\n")
    assert result.returncode == 1
    assert path in result.stdout


def test_document_allowlist_fails_closed_when_path_normalization_breaks(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tr = fake_bin / "tr"
    fake_tr.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    fake_tr.chmod(0o755)
    result = run_hook(
        "doc-allowlist.sh",
        input_text="NOTES.md\n",
        env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 2
    assert "could not normalize" in result.stderr


def make_document_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=5)
    hooks = path / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "check-documents.sh", hooks)
    shutil.copy2(HOOKS / "doc-allowlist.sh", hooks)
    shutil.copy2(HOOKS / "check_ingress.py", hooks)
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


def test_document_date_alarm_does_not_echo_the_source_line(tmp_path):
    make_document_repo(tmp_path)
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    (tmp_path / "GOALS.md").write_text(
        f"# Goals\n\n2026-07-26 accidental {secret}\n",
        encoding="utf-8",
    )

    result = run_document_check(tmp_path)

    assert result.returncode == 1
    assert "3:2026-07-26" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


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


def test_commit_message_refuses_a_recognized_secret_even_with_attribution():
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    result = run_commit_message(
        f"Do not record {secret}\n\nCo-Authored-By: Codex (OpenAI) <noreply@openai.com>\n"
    )
    assert result.returncode == 1
    assert secret not in result.stderr
    assert "credential pattern" in result.stderr


def test_typed_merge_subject_does_not_bypass_attribution():
    result = run_commit_message("Merge branch 'work/example'\n")
    assert result.returncode == 1


def test_fixup_subject_keeps_the_declared_exception():
    result = run_commit_message("fixup! attributed parent\n")
    assert result.returncode == 0


def test_hook_tests_do_not_inherit_policy_bypasses(monkeypatch):
    monkeypatch.setenv("ALLOW_UNATTRIBUTED", "1")
    assert run_commit_message("ordinary unattributed commit\n").returncode == 1


def push_line(remote_ref, *, local_sha, remote_sha=ZERO):
    local_ref = remote_ref
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def receipt_text(sha, reviewers=()):
    receipt = f"commit:  {sha}\nbranch:  work/example\naudited: {sha} (1 commit(s))\n"
    for reviewer in reviewers:
        receipt += f"\nauditor: {reviewer}\nwhen:    2026-07-28T12:00:00Z\nfinding: no findings\n"
    return receipt


def make_audit_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "work/example", str(path)], check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, timeout=5)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
        timeout=5,
    )
    (path / "safe.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=path, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, timeout=5)
    hooks = path / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "pre-push", hooks / "pre-push")
    shutil.copy2(HOOKS / "check_ingress.py", hooks / "check_ingress.py")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()


def real_git():
    found = shutil.which("git")
    assert found, "these tests need a real git on PATH"
    return found


def stub_git(path, body):
    """Install a fake `git` earlier on PATH and return an env that finds it.

    The hooks resolve the git directory through `git rev-parse`. Two of its
    failure modes cannot be reproduced with the git on this machine — an old
    release that does not understand an option, and a git that cannot answer at
    all — so they are modelled here. Everything the stub does not intercept is
    handed to the real binary, so the rest of the hook behaves normally.
    """
    directory = path / "stub-bin"
    directory.mkdir(exist_ok=True)
    (directory / "git").write_text(body, encoding="utf-8")
    (directory / "git").chmod(0o755)
    environment = clean_hook_env()
    environment["PATH"] = f"{directory}{os.pathsep}{environment.get('PATH', '')}"
    return environment


def old_git_stub(path):
    # An older git, before `--path-format`, does not answer with nothing. It
    # prints the unrecognised option back as a literal line, then the real
    # answer, and exits 0 — the behaviour verified on this machine in ledger
    # finding N6. A resolver that only tests for emptiness never notices.
    return stub_git(
        path,
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" --path-format=absolute "*)\n'
        "    printf '%s\\n' '--path-format=absolute'\n"
        f"    exec {real_git()} rev-parse --git-common-dir ;;\n"
        "esac\n"
        f'exec {real_git()} "$@"\n',
    )


def blind_git_stub(path):
    # A git that cannot say where the git directory is: a broken worktree link,
    # a hook run from somewhere unexpected, a permissions fault.
    return stub_git(
        path,
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" --git-common-dir "*) exit 128 ;;\n'
        "esac\n"
        f'exec {real_git()} "$@"\n',
    )


def run_isolated_pre_push(
    repo,
    sha,
    *,
    remote_ref="refs/heads/work/example",
    remote_sha=ZERO,
    env=None,
):
    return subprocess.run(
        ["sh", ".githooks/pre-push"],
        cwd=repo,
        env=env or clean_hook_env(),
        input=push_line(remote_ref, local_sha=sha, remote_sha=remote_sha),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_pre_commit_tests_do_not_inherit_policy_bypasses(tmp_path, monkeypatch):
    # The suite must fail on a machine where the operator has exported an escape
    # hatch, not quietly agree with it. This mirrors the existing commit-msg
    # test; the pre-commit helper was missing the same protection and would have
    # inherited the whole shell. Found by CodeRabbit, not by the eleven audits —
    # their test-integrity lens read the assertions and not the plumbing.
    monkeypatch.setenv("ALLOW_MAIN_COMMIT", "1")
    monkeypatch.setenv("ALLOW_DETACHED_COMMIT", "1")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, timeout=5)
    copy_commit_hooks(repo)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo)
    assert result.returncode == 1, "an exported bypass leaked into the test environment"
    assert "commit on main" in result.stderr


def test_pre_push_accepts_declared_work_branch(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0


def test_pre_push_rejects_direct_main_and_unknown_branch_kind(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    main = run_isolated_pre_push(repo, sha, remote_ref="refs/heads/main")
    unknown = run_isolated_pre_push(repo, sha, remote_ref="refs/heads/misc/example")
    assert main.returncode == 1
    assert unknown.returncode == 1


def test_pre_push_accepts_new_tag_but_refuses_to_move_published_tag(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    new = run_isolated_pre_push(repo, sha, remote_ref="refs/tags/v0.0.0")
    moved = run_isolated_pre_push(
        repo,
        sha,
        remote_ref="refs/tags/v0.0.0",
        remote_sha=sha,
    )
    assert new.returncode == 0
    assert moved.returncode == 1


def test_pre_push_refuses_to_delete_a_published_tag(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_isolated_pre_push(
        repo,
        ZERO,
        remote_ref="refs/tags/v0.0.0",
        remote_sha=sha,
    )
    assert result.returncode == 1
    assert "deleting published tag" in result.stderr


def test_pre_push_requires_deliberate_confirmation_before_deleting_a_branch(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    push_input = push_line(
        "refs/heads/work/example",
        local_sha=ZERO,
        remote_sha=sha,
    )

    refused = subprocess.run(
        ["sh", ".githooks/pre-push"],
        cwd=repo,
        env=clean_hook_env(),
        input=push_input,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    confirmed = subprocess.run(
        ["sh", ".githooks/pre-push"],
        cwd=repo,
        env={**clean_hook_env(), "ALLOW_BRANCH_DELETE": "work/example"},
        input=push_input,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert refused.returncode == 1
    assert "without confirming its work is retained" in refused.stderr
    assert "ALLOW_BRANCH_DELETE to this exact branch name" in refused.stderr
    assert confirmed.returncode == 0, confirmed.stderr

    wrong_branch = subprocess.run(
        ["sh", ".githooks/pre-push"],
        cwd=repo,
        env={**clean_hook_env(), "ALLOW_BRANCH_DELETE": "work/other"},
        input=push_input,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert wrong_branch.returncode == 1


def test_pre_push_recognizes_sha256_null_oids_for_new_refs_and_deletions(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    new_ref = run_isolated_pre_push(
        repo,
        sha,
        remote_sha=ZERO_SHA256,
    )
    deletion = run_isolated_pre_push(
        repo,
        ZERO_SHA256,
        remote_ref="refs/tags/v0.0.0",
        remote_sha=sha,
    )
    assert new_ref.returncode == 0
    assert deletion.returncode == 1
    assert "deleting published tag" in deletion.stderr


def test_pre_push_rejects_ref_without_a_declared_policy(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_isolated_pre_push(repo, sha, remote_ref="refs/notes/example")
    assert result.returncode == 1


def test_pre_push_scans_annotated_tag_messages(tmp_path):
    repo = tmp_path / "repo"
    make_audit_repo(repo)
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    subprocess.run(
        ["git", "tag", "-a", "unsafe", "-m", f"release {secret}"],
        cwd=repo,
        check=True,
        timeout=5,
    )
    tag_sha = subprocess.run(
        ["git", "rev-parse", "refs/tags/unsafe"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()

    result = run_isolated_pre_push(repo, tag_sha, remote_ref="refs/tags/unsafe")
    assert result.returncode == 1
    assert "annotated-tag" in result.stderr
    assert secret not in result.stderr


def test_pre_push_scans_secrets_deleted_from_the_outgoing_tip(tmp_path):
    repo = tmp_path / "repo"
    make_audit_repo(repo)
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    safe = repo / "safe.txt"
    safe.write_text(f"value = {secret}\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "unsafe ancestor"], cwd=repo, check=True, timeout=5)
    safe.write_text("safe again\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "clean tip"], cwd=repo, check=True, timeout=5)
    tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()

    result = run_isolated_pre_push(repo, tip)
    assert result.returncode == 1
    assert "outgoing-history" in result.stderr
    assert secret not in result.stderr


def test_pre_push_scans_credential_shaped_ref_names_without_echoing_them(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    result = run_isolated_pre_push(
        repo,
        sha,
        remote_ref=f"refs/heads/work/{secret}",
    )
    assert result.returncode == 1
    assert "<git-ref:" in result.stderr
    assert secret not in result.stderr


@pytest.mark.parametrize("reviewer_count", [0, 1, 2, 3])
def test_pre_push_review_coverage_is_a_checklist_and_never_blocks(tmp_path, reviewer_count):
    # Tyrel's ruling: the reviewer count is a CHECKLIST, not a gate. A receipt
    # records what the operator says happened — it cannot establish reviewer
    # identity, independence, or that anything was read — so refusing a push on
    # a number derived from self-asserted text bought ceremony, not safety, and
    # made the override a routine keystroke. It prints the coverage and pushes.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    names = ("Claude Opus 5", "Claude Fable 5", "GPT-5.6 Sol (OpenAI)")
    if reviewer_count:
        receipts = repo / ".git" / "audit-receipts"
        receipts.mkdir()
        (receipts / sha).write_text(
            receipt_text(sha, names[:reviewer_count]),
            encoding="utf-8",
        )

    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "review checklist" in result.stderr
    assert "Not a gate" in result.stderr
    # Every recorded reviewer is named, and every missing one is a visible gap.
    for who in names[:reviewer_count]:
        assert f"[x] {who}" in result.stderr
    assert result.stderr.count("[ ]") == 3 - reviewer_count
    if reviewer_count:
        assert f"{reviewer_count} distinct reviewer(s)" in result.stderr


def test_pre_push_checklist_needs_no_override_variable(tmp_path):
    # The old ALLOW_UNAUDITED_PUSH is gone. A push with no receipt at all still
    # succeeds, and nothing tells the operator to set a variable that no longer
    # does anything — a hook that names a dead escape hatch is lying to its user.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "ALLOW_UNAUDITED_PUSH" not in result.stderr
    assert "no receipt recorded" in result.stderr


def test_pre_push_audit_gate_rejects_receipt_for_a_different_commit(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    (receipts / sha).write_text(
        receipt_text(
            ZERO,
            ("Claude Opus 5", "Claude Fable 5", "GPT-5.6 Sol (OpenAI)"),
        ),
        encoding="utf-8",
    )
    result = run_isolated_pre_push(repo, sha)
    # Still does not block — but a receipt for a different commit must not be
    # silently counted as coverage for this one, or the checklist lies.
    assert result.returncode == 0, result.stderr
    assert "records a different commit" in result.stderr
    assert result.stderr.count("[ ]") == 3
    assert "[x]" not in result.stderr


def test_pre_push_counts_any_three_distinct_reviewers_not_three_fixed_names(tmp_path):
    # Tyrel's ruling: the receipt records the model that actually answered. Today's
    # product names baked into a safety hook go stale at the next release, and a
    # gate that then forces either a bypass or a mislabelled receipt is worse than
    # no gate. The releases below are deliberately not the current roster.
    #
    # Ledger L41. The body used to assert only `returncode == 0`, which this hook
    # returns for *every* input — no receipt, a receipt for another commit, a
    # malformed one. So the name promised a count and the body proved nothing
    # about counting: a hook that ignored off-roster names entirely would have
    # kept this test green. Assert what the name claims — each name ticked, the
    # count printed, and no unfilled box left over.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    names = ("Claude Opus 7", "Claude Fable 7", "GPT-6.0 Terra (OpenAI)")
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    (receipts / sha).write_text(receipt_text(sha, names), encoding="utf-8")
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    for who in names:
        assert f"[x] {who}" in result.stderr
    assert "3 distinct reviewer(s)" in result.stderr
    assert "[ ]" not in result.stderr


def test_pre_push_does_not_count_the_same_reviewer_three_times(tmp_path):
    # Three passes by one model is one review wearing three hats. The checklist
    # must show that honestly — one ticked, two gaps — rather than reporting
    # three. It still does not block; it just refuses to flatter the coverage.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    (receipts / sha).write_text(
        receipt_text(sha, ("Claude Opus 5", "Claude Opus 5", "Claude Opus 5")),
        encoding="utf-8",
    )
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "1 distinct reviewer(s)" in result.stderr
    assert result.stderr.count("[ ]") == 2


def test_pre_push_does_not_credit_partial_reviewer_records(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    partial = (
        receipt_text(
            sha,
            ("Claude Opus 5", "Claude Fable 5"),
        )
        + "\nauditor: GPT-5.6 Sol (OpenAI)\n"
    )
    (receipts / sha).write_text(partial, encoding="utf-8")

    result = run_isolated_pre_push(repo, sha)
    # A malformed receipt buys no credit. It does not block the push, but it must
    # never be read as three reviewers — a checklist that overstates coverage is
    # worse than no checklist, because it is believed.
    assert result.returncode == 0, result.stderr
    assert "incomplete or invalid" in result.stderr
    assert "[x]" not in result.stderr
    assert result.stderr.count("[ ]") == 3


def write_receipt(repo, sha, text):
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir(exist_ok=True)
    (receipts / sha).write_text(text, encoding="utf-8")
    return receipts / sha


def test_pre_push_finds_receipts_on_a_git_too_old_for_path_format(tmp_path):
    # Ledger N6. The compatibility fallback was guarded by an emptiness test,
    # and an old git does not answer with nothing — it echoes the option back
    # and then answers. So the fallback never fired, the receipt directory
    # became two lines of junk, and a fully reviewed push reported no receipt
    # at all. Dead compatibility code that looks like a safety net is worse
    # than an honest requirement; this proves the net actually catches.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    names = ("Claude Opus 5", "Claude Fable 5", "GPT-5.6 Sol (OpenAI)")
    write_receipt(repo, sha, receipt_text(sha, names))

    result = run_isolated_pre_push(repo, sha, env=old_git_stub(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "3 distinct reviewer(s)" in result.stderr
    assert "no receipt recorded" not in result.stderr


def test_record_audit_writes_to_the_git_directory_on_a_git_too_old_for_path_format(tmp_path):
    # The same defect in the writer, where the consequence is worse: the
    # receipt path became a relative one and the receipt would have been
    # written into the working tree, under a directory named after the option.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")

    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Opus 5", "no findings"],
        cwd=repo,
        env=old_git_stub(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (repo / ".git" / "audit-receipts" / sha).is_file()
    stray = [entry.name for entry in repo.iterdir() if "path-format" in entry.name]
    assert not stray, f"receipt written into the working tree: {stray}"


def test_pre_push_reports_unknown_coverage_when_it_cannot_look(tmp_path):
    # NR4 / GOVERNANCE 10. When the git directory cannot be located the hook
    # has not measured zero reviewers — it has measured nothing. Saying "no
    # receipt recorded" there is a claim about review coverage made by an
    # instrument that could not read, at the exact moment a human is deciding
    # whether the coverage is enough.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_isolated_pre_push(repo, sha, env=blind_git_stub(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "cannot locate the git directory" in result.stderr
    assert "UNKNOWN" in result.stderr
    assert "not a record of zero reviewers" in result.stderr
    assert "no receipt recorded" not in result.stderr
    # And it must not print the empty boxes, which read as a measured zero.
    assert "[ ]" not in result.stderr


def test_pre_push_matches_the_commit_line_despite_surrounding_whitespace(tmp_path):
    # NR8. `pre-push` matched `commit:  <sha>` on exactly two spaces, which is
    # how record-audit.sh happens to write it. The receipt validator is stubbed
    # out here on purpose: it holds its own copy of the format, and the point of
    # this test is the hook's *own* matcher — if the format is ever loosened
    # anywhere, pre-push must not silently report every reviewed push as
    # unreviewed. Stubbing the validator weakens nothing outside this test.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    (repo / ".githooks" / "check_ingress.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    names = ("Claude Opus 5", "Claude Fable 5", "GPT-5.6 Sol (OpenAI)")
    reformatted = receipt_text(sha, names).replace(f"commit:  {sha}", f"  commit: {sha} ", 1)
    write_receipt(repo, sha, reformatted)

    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "3 distinct reviewer(s)" in result.stderr
    assert "records a different commit" not in result.stderr


def test_pre_push_still_rejects_a_commit_line_naming_another_commit(tmp_path):
    # The tolerance above must be whitespace only. A receipt for a different
    # commit is still a receipt for a different commit, and the sha must match
    # in full — a prefix is not a match.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    (repo / ".githooks" / "check_ingress.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    other = "b" * 39 + "a"
    write_receipt(repo, sha, receipt_text(sha, ("Claude Opus 5",)).replace(sha, other, 1))
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "records a different commit" in result.stderr
    assert "[x]" not in result.stderr


def test_receipt_format_written_matches_what_pre_push_reads(tmp_path):
    # NR8's other half: pin the format so it cannot drift unnoticed. The writer,
    # the validator and the hook agree by contract, not by coincidence — this
    # asserts the exact header record-audit.sh writes and then reads it back
    # through the real hook and the real validator.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Opus 5", "no findings"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    receipt = (repo / ".git" / "audit-receipts" / sha).read_text(encoding="utf-8")
    assert receipt.splitlines()[0] == f"commit:  {sha}"
    pushed = run_isolated_pre_push(repo, sha)
    assert "1 distinct reviewer(s)" in pushed.stderr


def test_record_audit_binds_the_receipt_to_the_named_commit(tmp_path):
    # Ledger N7, and it misfired live: reviewers audited one commit, the session
    # committed again while they worked, and the receipt was stamped against a
    # commit nobody had opened. The receipt looked perfect and covered nothing.
    repo = tmp_path / "repo"
    reviewed = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "add", "later.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "later"], cwd=repo, check=True, timeout=5)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    assert head != reviewed

    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "--commit", reviewed, "Claude Opus 5", "no findings"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (repo / ".git" / "audit-receipts" / reviewed).is_file()
    assert not (repo / ".git" / "audit-receipts" / head).exists()
    body = (repo / ".git" / "audit-receipts" / reviewed).read_text(encoding="utf-8")
    assert body.splitlines()[0] == f"commit:  {reviewed}"
    assert "not HEAD" in body, "a receipt that is not for HEAD must say so"
    # And the hook reads it back: coverage lands on the reviewed commit only.
    assert "1 distinct reviewer(s)" in run_isolated_pre_push(repo, reviewed).stderr
    assert "no receipt recorded" in run_isolated_pre_push(repo, head).stderr


def test_record_audit_defaults_to_head_when_no_commit_is_named(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Opus 5", "no findings"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (repo / ".git" / "audit-receipts" / sha).is_file()


def test_record_audit_refuses_a_commit_it_cannot_resolve(tmp_path):
    # Fail closed: an unresolvable revision must not quietly fall back to HEAD,
    # which is the very substitution that produced the false receipt.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    for bad in ("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "no/such/ref"):
        result = subprocess.run(
            ["sh", ".githooks/record-audit.sh", "--commit", bad, "Claude Opus 5", "no findings"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 1, result.stdout
        assert "does not name a commit" in result.stderr
    assert not (repo / ".git" / "audit-receipts" / sha).exists()


def test_record_audit_rejects_an_unknown_option_instead_of_recording_it(tmp_path):
    # An option-shaped auditor name is a typo, not a reviewer. Recording it
    # would put a fabricated name on a receipt.
    repo = tmp_path / "repo"
    make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "--commmit", "Claude Opus 5", "no findings"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Unknown option" in result.stderr
    assert not (repo / ".git" / "audit-receipts").exists()


def test_record_audit_rejects_multiline_fields_before_writing():
    result = run_hook(
        "record-audit.sh",
        args=("one\nauditor: two", "no findings"),
    )
    assert result.returncode == 1
    assert "must each be one line" in result.stderr


def test_record_audit_rejects_secret_text_without_creating_a_receipt(tmp_path):
    repo = tmp_path / "repo"
    make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Opus 5", f"found {secret}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert secret not in result.stderr
    assert not (repo / ".git" / "audit-receipts").exists()


def test_record_audit_refuses_a_concurrent_writer_instead_of_losing_a_finding(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    lock = repo / ".git" / "audit-receipts" / f"{sha}.lock"
    lock.mkdir(parents=True)

    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Opus 5", "no findings"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "may be active" in result.stderr
    assert "stale lock" in result.stderr
    assert not (repo / ".git" / "audit-receipts" / sha).exists()


def test_record_audit_installs_complete_reviewer_records_atomically(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    for reviewer in ("Claude Opus 5", "Claude Fable 5"):
        result = subprocess.run(
            ["sh", ".githooks/record-audit.sh", reviewer, "no findings"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr

    receipt = repo / ".git" / "audit-receipts" / sha
    validation = subprocess.run(
        [sys.executable, str(HOOKS / "check_ingress.py"), "--audit-receipt", str(receipt)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert validation.returncode == 0, validation.stderr
    assert receipt.read_text(encoding="utf-8").count("\nauditor: ") == 2
    assert not list(receipt.parent.glob(f"{sha}.tmp.*"))


def test_record_audit_preserves_a_malformed_existing_receipt(tmp_path):
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    receipt = repo / ".git" / "audit-receipts" / sha
    receipt.parent.mkdir()
    partial = receipt_text(sha) + "\nauditor: Claude Opus 5\n"
    receipt.write_text(partial, encoding="utf-8")

    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Fable 5", "no findings"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "old receipt was not changed" in result.stderr
    assert receipt.read_text(encoding="utf-8") == partial


def stub_command(path, name, body):
    """Put one fake executable earlier on PATH and return an env that finds it.

    `stub_git` models a whole git; this models a single coreutils binary so the
    hooks' own failure handling can be exercised. A hook that only works when
    every tool on PATH works is a hook whose failure mode nobody has seen.
    """
    directory = path / "stub-bin"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
    (directory / name).chmod(0o755)
    environment = clean_hook_env()
    environment["PATH"] = f"{directory}{os.pathsep}{environment.get('PATH', '')}"
    return environment


def run_pre_push_raw(repo, input_text, env=None):
    return subprocess.run(
        ["sh", ".githooks/pre-push"],
        cwd=repo,
        env=env or clean_hook_env(),
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_pre_push_checks_a_final_record_with_no_trailing_newline(tmp_path):
    # Ledger L13. `while read -r a b c d` returns non-zero on an unterminated
    # last line *after* assigning the fields, so the loop body never ran for it.
    # The ref was pushed with no branch rule, no history scan and no checklist,
    # and the hook exited 0 — a gate that read nothing and looked like it agreed.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_pre_push_raw(
        repo,
        f"refs/heads/main {sha} refs/heads/main {ZERO}",
    )
    assert result.returncode == 1, result.stderr
    assert "direct push to main" in result.stderr


def test_pre_push_checks_the_last_record_when_earlier_ones_are_well_formed(tmp_path):
    # The same defect in the shape git actually produces it: several refs in one
    # push, only the last one truncated. The first record passing must not be
    # allowed to stand in for the last one never being read.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_pre_push_raw(
        repo,
        f"refs/heads/work/example {sha} refs/heads/work/example {ZERO}\n"
        f"refs/heads/main {sha} refs/heads/main {ZERO}",
    )
    assert result.returncode == 1, result.stderr
    assert "direct push to main" in result.stderr


def test_pre_push_says_so_when_it_receives_no_refs(tmp_path):
    # Measured on git 2.55: an up-to-date push runs this hook with an empty ref
    # list, so refusing here would refuse a no-op and teach the operator to reach
    # for --no-verify. It exits 0 — but silently exiting 0 after checking nothing
    # is indistinguishable from checking something and approving it, which
    # GOVERNANCE 2 does not allow. Say that nothing was examined.
    repo = tmp_path / "repo"
    make_audit_repo(repo)
    result = run_pre_push_raw(repo, "")
    assert result.returncode == 0, result.stderr
    assert "received no refs" in result.stderr
    assert "nothing was" in result.stderr
    # No per-ref checklist header, because no ref was examined.
    assert "── review checklist —" not in result.stderr


def test_pre_push_reports_unknown_when_the_reviewer_count_is_not_a_number(tmp_path):
    # Ledger L12. `reviewers=$(... | wc -l | tr -d ...)` holding text made
    # `[ "$reviewers" -lt 3 ]` *error* rather than evaluate true, so the whole
    # shortfall block was skipped: a push with a broken counter printed a number
    # nobody could read and no warning at all. Unknown is never zero, and it is
    # never silence either.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    write_receipt(repo, sha, receipt_text(sha, ("Claude Opus 5",)))
    env = stub_command(tmp_path, "wc", "#!/bin/sh\necho 'not a number'\n")
    result = run_isolated_pre_push(repo, sha, env=env)
    assert result.returncode == 0, result.stderr
    assert "UNKNOWN" in result.stderr
    assert "count could not be computed" in result.stderr
    assert "distinct reviewer(s)" not in result.stderr
    assert "not a number" not in result.stderr


def test_pre_push_folds_case_and_whitespace_before_counting_reviewers(tmp_path):
    # Ledger L11. `sort -u` over raw bytes made three spellings of one model into
    # three reviewers, and the checklist reported full coverage for a single
    # read. Overstating coverage is the one failure this checklist cannot have:
    # it is the number a human uses to decide whether to push.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    write_receipt(
        repo,
        sha,
        receipt_text(sha, ("Claude Opus 5", "claude  opus 5", "CLAUDE OPUS 5")),
    )
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "1 distinct reviewer(s)" in result.stderr
    assert result.stderr.count("[ ]") == 2
    # The first spelling recorded is what the operator sees; the folding decides
    # the count, not what gets displayed.
    assert "[x] Claude Opus 5" in result.stderr
    assert result.stderr.count("[x]") == 1


def test_pre_push_marks_a_reviewer_name_that_is_not_plain_ascii(tmp_path):
    # The other half of L11, and the honest half. A Cyrillic С in "Сlaude" is a
    # different reviewer to any fold this hook could carry, and a partial
    # confusable table would claim a coverage it does not have (GOVERNANCE 10).
    # So the name is counted as distinct — and flagged, beside the others, for
    # the human already reading the list.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    lookalike = "Сlaude Opus 5"
    write_receipt(repo, sha, receipt_text(sha, ("Claude Opus 5", lookalike)))
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, result.stderr
    assert "2 distinct reviewer(s)" in result.stderr
    assert "not plain ASCII" in result.stderr
    assert result.stderr.count("not plain ASCII") == 1


def test_pre_push_scans_the_real_object_not_a_git_replace_substitute(tmp_path):
    # Ledger L14. `git replace` makes every reader — this hook, the scanner it
    # calls, and a reviewer running `git show` — see a clean stand-in while the
    # genuine object is what leaves in the pack. One environment variable turns
    # the whole mechanism off; without it the receipt, the outgoing scan and the
    # ancestry check are all reading a different history from the one pushed.
    repo = tmp_path / "repo"
    base = make_audit_repo(repo)
    secret = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"
    (repo / "safe.txt").write_text(f"value = {secret}\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "unsafe"], cwd=repo, check=True, timeout=5)
    unsafe = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    innocent = subprocess.run(
        ["git", "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "innocent"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    subprocess.run(
        ["git", "replace", "-f", unsafe, innocent],
        cwd=repo,
        check=True,
        timeout=5,
        capture_output=True,
    )

    result = run_isolated_pre_push(repo, unsafe)
    assert result.returncode == 1, result.stderr
    assert "outgoing-history" in result.stderr
    assert secret not in result.stderr


def test_pre_push_allows_a_fast_forward_and_refuses_a_divergent_push(tmp_path):
    # Ledger L42. Every existing force-push test pushes a brand-new ref, where
    # the remote SHA is all zeroes and the ancestry test never runs. Reversing
    # the two arguments to `merge-base --is-ancestor` would refuse every ordinary
    # push and permit every rewrite, and the suite would not have noticed. Both
    # directions are asserted here against a real divergent history.
    repo = tmp_path / "repo"
    first = make_audit_repo(repo)
    (repo / "safe.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True, timeout=5)
    second = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    # A sibling of `first`, so neither tip is an ancestor of the other.
    divergent = subprocess.run(
        ["git", "commit-tree", f"{first}^{{tree}}", "-p", first, "-m", "divergent"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()

    forward = run_isolated_pre_push(repo, second, remote_sha=first)
    assert forward.returncode == 0, forward.stderr
    assert "force-push" not in forward.stderr

    rewrite = run_isolated_pre_push(repo, divergent, remote_sha=second)
    assert rewrite.returncode == 1, rewrite.stderr
    assert "force-push" in rewrite.stderr


def test_pre_push_does_not_label_an_unreadable_remote_tip_as_a_force_push(tmp_path):
    # `merge-base --is-ancestor` uses 1 for a proven non-ancestor and a larger
    # status when it cannot inspect the history at all. Conflating them reports
    # an unmeasured force-push verdict and hides the fetch or object failure that
    # tells the operator how to repair the check.
    repo = tmp_path / "repo"
    local = make_audit_repo(repo)
    missing_remote = "f" * 40

    result = run_isolated_pre_push(repo, local, remote_sha=missing_remote)

    assert result.returncode == 1, result.stderr
    assert "unable to verify ancestry" in result.stderr
    assert "force-push to" not in result.stderr
    assert "fetch or deepen" in result.stderr.lower()


def test_record_audit_fails_before_installing_the_receipt_not_after(tmp_path):
    # Ledger L19. The reviewer count ran *after* the receipt was moved into
    # place, so a failure there exited non-zero with the receipt permanently
    # installed. The operator reads a failure, runs the command again, and the
    # same reviewer is appended twice — the checklist then shows coverage that
    # one read produced. Anything that can fail belongs before the install.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    shutil.copy2(HOOKS / "record-audit.sh", repo / ".githooks" / "record-audit.sh")
    env = stub_command(tmp_path, "grep", "#!/bin/sh\nexit 2\n")
    result = subprocess.run(
        ["sh", ".githooks/record-audit.sh", "Claude Opus 5", "no findings"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (repo / ".git" / "audit-receipts" / sha).exists(), (
        "a failing step reported failure while leaving the receipt installed"
    )
    assert "No receipt was written" in result.stderr
    leftovers = list((repo / ".git" / "audit-receipts").glob("*.tmp.*"))
    assert not leftovers, f"temporary receipt left behind: {leftovers}"


def run_commit_message_in(repo, message, env=None):
    process_env = clean_hook_env()
    if env:
        process_env.update(env)
    path = repo / "COMMIT_EDITMSG_TEST"
    path.write_text(message, encoding="utf-8")
    return subprocess.run(
        ["sh", ".githooks/commit-msg", str(path)],
        cwd=repo,
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def make_commit_msg_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "work/example", str(path)], check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, timeout=5)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
        timeout=5,
    )
    (path / "safe.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=path, check=True, timeout=5)
    subprocess.run(
        ["git", "commit", "-qm", "base\n\nCo-Authored-By: Test <t@example.invalid>"],
        cwd=path,
        check=True,
        timeout=5,
    )
    hooks = path / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "commit-msg", hooks / "commit-msg")
    shutil.copy2(HOOKS / "check_ingress.py", hooks / "check_ingress.py")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()


@pytest.mark.parametrize(
    "leftover",
    ["directory", "empty", "garbage"],
)
def test_commit_message_merge_exemption_requires_a_real_state_file(tmp_path, leftover):
    # Ledger L27. The exemption tested `[ -e ]` only, so a directory, an empty
    # file or any leftover at .git/MERGE_HEAD handed away the attribution rule
    # for every commit made afterwards — and silently, because a check that
    # declines to fire prints nothing.
    repo = tmp_path / "repo"
    make_commit_msg_repo(repo)
    state = repo / ".git" / "MERGE_HEAD"
    if leftover == "directory":
        state.mkdir()
    elif leftover == "empty":
        state.write_text("", encoding="utf-8")
    else:
        state.write_text("not a commit id\n", encoding="utf-8")

    result = run_commit_message_in(repo, "unattributed commit\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not say which machine wrote it" in result.stderr


def test_commit_message_exempts_a_merge_git_is_actually_running(tmp_path):
    # Ledger L47: the exemption's own happy path had no test, so tightening it
    # could have refused every real merge and nothing would have said so.
    repo = tmp_path / "repo"
    head = make_commit_msg_repo(repo)
    (repo / ".git" / "MERGE_HEAD").write_text(f"{head}\n", encoding="utf-8")
    result = run_commit_message_in(repo, "Merge branch 'work/other'\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_commit_message_accepts_a_human_named_trailer(tmp_path):
    # Ledger L18, recorded rather than fixed. CLAUDE.md says this hook "enforces
    # authorship only", and it checks trailer *syntax*: any name plus an
    # email-shaped address passes, including a person's. That is the documented
    # behaviour, and this pins it so the gap is visible in the suite instead of
    # being rediscovered by the next audit.
    repo = tmp_path / "repo"
    make_commit_msg_repo(repo)
    result = run_commit_message_in(
        repo,
        "Describe change\n\nCo-Authored-By: Tyrel <tyrel@example.invalid>\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_pre_commit_hatches_open_only_for_the_exact_value_one(tmp_path, value):
    # Ledger L42/L47. Both hatches compare against the literal "1". Nothing
    # proved that, so a later `[ -n "$ALLOW_MAIN_COMMIT" ]` — the obvious
    # "simplification" — would have turned every near-miss value into a bypass
    # and kept the suite green.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, timeout=5)
    copy_commit_hooks(repo)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo, env={"ALLOW_MAIN_COMMIT": value})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "commit on main" in result.stderr


def test_pre_commit_main_hatch_opens_for_one(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, timeout=5)
    copy_commit_hooks(repo)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo, env={"ALLOW_MAIN_COMMIT": "1"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_hook_installer_does_not_report_success_when_chmod_fails(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=5)
    subprocess.run(
        ["git", "config", "core.hooksPath", "previous-hooks"],
        cwd=repo,
        check=True,
        timeout=5,
    )
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "install.sh", hooks / "install.sh")
    for name in (
        "pre-commit",
        "pre-merge-commit",
        "pre-applypatch",
        "applypatch-msg",
        "pre-push",
        "commit-msg",
        "check-all.sh",
        "check-documents.sh",
        "doc-allowlist.sh",
        "record-audit.sh",
    ):
        (hooks / name).write_text("#!/bin/sh\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_chmod = fake_bin / "chmod"
    fake_chmod.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_chmod.chmod(0o755)
    result = subprocess.run(
        ["sh", ".githooks/install.sh"],
        cwd=repo,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "Hooks installed" not in result.stdout
    assert "not usable" in result.stderr
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert configured.stdout.strip() == "previous-hooks", (
        "a failed install must not replace a previously working hook path"
    )


def test_hook_installer_configures_hooks_after_prerequisites_succeed(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=5)
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "install.sh", hooks / "install.sh")
    for name in (
        "pre-commit",
        "pre-merge-commit",
        "pre-applypatch",
        "applypatch-msg",
        "pre-push",
        "commit-msg",
        "check-all.sh",
        "check-documents.sh",
        "doc-allowlist.sh",
        "record-audit.sh",
    ):
        (hooks / name).write_text("#!/bin/sh\n", encoding="utf-8")

    result = subprocess.run(
        ["sh", ".githooks/install.sh"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "Hooks installed" in result.stdout
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert configured.stdout.strip() == ".githooks"
    for name in ("active", "archive", "scratch", "design", "tools", "raw"):
        assert (repo / "workbench" / name).is_dir()
    # Ledger L42: nothing asserted the executable bit the whole install exists
    # to set. A hooksPath pointing at files git cannot execute is a clone that
    # reports "Hooks installed" and runs no hook at all.
    # Ledger L15: the two integration-path hooks are as easy to leave
    # unexecutable as any other, and a hook git cannot run is a rule that is off
    # without saying so.
    for name in (
        "pre-commit",
        "pre-merge-commit",
        "pre-applypatch",
        "applypatch-msg",
        "pre-push",
        "commit-msg",
        "record-audit.sh",
    ):
        assert os.access(hooks / name, os.X_OK), f"{name} was not made executable"


def test_hook_installer_does_not_configure_when_mkdir_fails(tmp_path):
    # Ledger L47. `mkdir -p` runs before `git config` precisely so a filesystem
    # fault leaves a previously working hooksPath alone. Untested, that ordering
    # was one edit away from being lost.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=5)
    subprocess.run(
        ["git", "config", "core.hooksPath", "previous-hooks"],
        cwd=repo,
        check=True,
        timeout=5,
    )
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOKS / "install.sh", hooks / "install.sh")
    for name in (
        "pre-commit",
        "pre-merge-commit",
        "pre-applypatch",
        "applypatch-msg",
        "pre-push",
        "commit-msg",
        "check-all.sh",
        "check-documents.sh",
        "doc-allowlist.sh",
        "record-audit.sh",
    ):
        (hooks / name).write_text("#!/bin/sh\n", encoding="utf-8")

    env = stub_command(tmp_path, "mkdir", "#!/bin/sh\nexit 1\n")
    result = subprocess.run(
        ["sh", ".githooks/install.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert "Hooks installed" not in result.stdout
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert configured.stdout.strip() == "previous-hooks", (
        "a failed install must not replace a previously working hook path"
    )


@pytest.mark.parametrize("value", ["0", "true", ""])
def test_pre_commit_detached_hatch_opens_only_for_the_exact_value_one(tmp_path, value):
    # Ledger L42, named explicitly: a malformed ALLOW_DETACHED_COMMIT=0 must not
    # slip past the exact-"1" comparison.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "work/base", str(repo)], check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, timeout=5)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
        timeout=5,
    )
    (repo / "safe.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, timeout=5)
    copy_commit_hooks(repo)
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True, timeout=5)
    (repo / "safe.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo, env={"ALLOW_DETACHED_COMMIT": value})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "detached" in result.stderr


def copy_commit_hooks(repo):
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "check_ingress.py", "doc-allowlist.sh"):
        shutil.copy2(HOOKS / name, hooks / name)
    return hooks


def run_isolated_pre_commit(repo, env=None):
    # clean_hook_env() strips every ALLOW_* variable, and it is not optional.
    # Passing env=None would inherit the developer's shell wholesale: anyone who
    # had exported ALLOW_MAIN_COMMIT=1 for a legitimate one-off would then watch
    # the test that asserts main-commits are blocked pass for entirely the wrong
    # reason. The suite would be green and the guard dead, on that machine only.
    # The pre-push helper has always stripped them; this one did not.
    process_env = clean_hook_env()
    if env:
        process_env.update(env)
    return subprocess.run(
        ["sh", ".githooks/pre-commit"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=process_env,
    )


def test_pre_commit_blocks_the_first_commit_on_unborn_main(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, timeout=5)
    copy_commit_hooks(repo)
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo)
    assert result.returncode == 1
    assert "commit on main" in result.stderr


def test_pre_commit_blocks_detached_head_commits(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "work/base", str(repo)], check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, timeout=5)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
        timeout=5,
    )
    (repo / "safe.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, timeout=5)
    copy_commit_hooks(repo)
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True, timeout=5)
    (repo / "safe.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo)
    assert result.returncode == 1
    assert "detached" in result.stderr
    # The block has to say how to get past it, or the only route left is
    # --no-verify, which is denied to Claude and skips every other check too.
    assert "ALLOW_DETACHED_COMMIT=1" in result.stderr


def test_pre_commit_detached_head_block_has_a_named_escape_hatch(tmp_path):
    # An interactive rebase detaches HEAD legitimately. Without a hatch the only
    # way to amend mid-rebase is --no-verify, which disables the secret scan and
    # the stray-document check as collateral — a guard that forces a bigger
    # bypass than the thing it is guarding.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "work/base", str(repo)], check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, timeout=5)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
        timeout=5,
    )
    (repo / "safe.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, timeout=5)
    copy_commit_hooks(repo)
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True, timeout=5)
    (repo / "safe.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo, check=True, timeout=5)
    result = run_isolated_pre_commit(repo, env={"ALLOW_DETACHED_COMMIT": "1"})
    assert result.returncode == 0, result.stderr
    assert "detached" not in result.stderr


def test_document_check_rejects_control_character_paths(tmp_path):
    make_document_repo(tmp_path)
    shutil.copy2(HOOKS / "check_ingress.py", tmp_path / ".githooks" / "check_ingress.py")
    path = tmp_path / "evil\nREADME.md"
    path.write_text("not an allowed document\n", encoding="utf-8")
    result = run_document_check(tmp_path)
    assert result.returncode == 1
    assert "control-path" in result.stderr


def test_document_check_reflects_unstaged_deletion_but_keeps_staged_addition(tmp_path):
    make_document_repo(tmp_path)
    note = tmp_path / "NOTES.md"
    note.write_text("stray\n", encoding="utf-8")
    subprocess.run(["git", "add", "NOTES.md"], cwd=tmp_path, check=True, timeout=5)
    note.unlink()

    # The absent path is an index addition and would still be committed, so it
    # must not disappear from the document check merely because the worktree
    # copy was removed.
    staged_addition = run_document_check(tmp_path)
    assert staged_addition.returncode == 1
    assert "NOTES.md" in staged_addition.stderr

    subprocess.run(["git", "reset", "-q", "--", "NOTES.md"], cwd=tmp_path, check=True, timeout=5)
    note.write_text("stray\n", encoding="utf-8")
    subprocess.run(["git", "add", "NOTES.md"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
        timeout=5,
    )
    note.unlink()

    # This is now a tracked path deleted only in the worktree. The full local
    # check evaluates the tree being assembled and must allow the removal to be
    # checked before staging it.
    unstaged_deletion = run_document_check(tmp_path)
    assert unstaged_deletion.returncode == 0


def test_check_all_fails_when_common_scan_cannot_run(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=5)
    for name in (
        "README.md",
        "GOALS.md",
        "GOVERNANCE.md",
        "ARCHITECTURE.md",
        "GLOSSARY.md",
        "CLAUDE.md",
    ):
        (repo / name).write_text(f"# {name}\n", encoding="utf-8")

    hooks = repo / ".githooks"
    hooks.mkdir()
    # Copy every hook, derived rather than listed. A hardcoded list here goes
    # stale the moment a hook is added: three new integration hooks landed and
    # this fixture then failed with exit 127 and "No such file or directory",
    # which reads as a broken test rather than an incomplete one. That is the
    # same drift the entrypoint test in test_seat.py exists to catch, so this
    # fixture should not reintroduce it one directory away.
    for source in sorted(HOOKS.iterdir()):
        if source.is_file() and source.suffix not in (".py",):
            shutil.copy2(source, hooks / source.name)
    shutil.copy2(HOOKS / "check_ingress.py", hooks / "check_ingress.py")
    for relative in ("operations/notify/notify.sh", "operations/codex/seat.sh"):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (repo / "common").mkdir()
    subprocess.run(["git", "add", "."], cwd=repo, check=True, timeout=5)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("python3", "ruff", "shellcheck", "tach"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    fake_find = fake_bin / "find"
    fake_find.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    fake_find.chmod(0o755)

    result = subprocess.run(
        ["sh", ".githooks/check-all.sh"],
        cwd=repo,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "could not inspect common/" in result.stderr


def test_ci_actions_are_pinned_to_full_commit_shas():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*-\s+uses:\s+(\S+)\s*$", workflow, flags=re.MULTILINE)
    assert uses, "CI declares no external actions"
    unpinned = [value for value in uses if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", value)]
    assert not unpinned, f"CI actions are not immutable commit pins: {unpinned}"


def test_ci_checkouts_do_not_persist_credentials_for_repository_code():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    checkout_steps = re.findall(
        r"(?ms)^\s*-\s+uses:\s+actions/checkout@[0-9a-f]{40}\s*$"
        r"(?P<body>.*?)(?=^\s*-\s+(?:uses:|name:)|^\s{2}\S|\Z)",
        workflow,
    )
    assert checkout_steps, "CI declares no checkout steps"
    assert all(
        re.search(r"^\s+persist-credentials:\s+false\s*$", body, flags=re.MULTILINE)
        for body in checkout_steps
    ), "a checkout leaves GITHUB_TOKEN in local Git config while repository code runs"


def test_ci_autoclave_gate_uses_untrusted_paths_only_for_comparison():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gate = workflow.split("- name: The autoclave is empty", 1)[1]
    assert "stray_count=$((stray_count + 1))" in gate
    entry_lines = [
        line.strip()
        for line in gate.splitlines()
        if "$entry" in line and not line.lstrip().startswith("#")
    ]
    assert entry_lines == [
        '[ -z "$entry" ] && continue',
        '[ "$entry" = "autoclave/README.md" ] && continue',
    ]
    assert re.search(r"(?m)^\s*set\s+-[^#\n]*x", gate) is None, (
        "shell tracing would print expanded autoclave paths"
    )


def test_ci_scans_annotated_tag_objects_on_tag_pushes():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "tags: ['**']" in workflow
    assert '--ref-object "$GITHUB_REF"' in workflow
    assert "--ref-fields" in workflow


SAMPLE_SECRET = "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"

INTEGRATION_HOOKS = (
    "pre-commit",
    "pre-merge-commit",
    "pre-applypatch",
    "applypatch-msg",
    "commit-msg",
    "check_ingress.py",
    "doc-allowlist.sh",
)


def git(repo, *args, check=True, env=None):
    process_env = clean_hook_env()
    if env:
        process_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=process_env,
        capture_output=True,
        text=True,
        check=check,
        timeout=15,
    )


def install_integration_hooks(repo):
    """Install the real commit-path hooks the way install.sh does.

    Nothing here disables a hook to build its fixture: the adversarial history
    is created *before* core.hooksPath is set, so no test needs `--no-verify`
    or `-c core.hooksPath=` to reach the state it is testing.
    """
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    for name in INTEGRATION_HOOKS:
        source = HOOKS / name
        assert source.is_file(), f"{name} does not exist, so the hook cannot be installed"
        shutil.copy2(source, hooks / name)
        (hooks / name).chmod(0o755)
    git(repo, "config", "core.hooksPath", ".githooks")
    return hooks


def make_integration_repo(path, *, secret=SAMPLE_SECRET):
    """A repo with a diverged branch and a patch, both carrying a credential.

    Returns the directory holding two patches: one poisoned, one clean.
    """
    subprocess.run(["git", "init", "-q", "-b", "work/base", str(path)], check=True, timeout=15)
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    (path / "safe.txt").write_text("safe\n", encoding="utf-8")
    git(path, "add", "safe.txt")
    git(path, "commit", "-qm", "base\n\nCo-Authored-By: Test <t@example.invalid>")

    git(path, "switch", "-qc", "work/poisoned")
    (path / "leaked.txt").write_text(f"token = {secret}\n", encoding="utf-8")
    git(path, "add", "leaked.txt")
    git(path, "commit", "-qm", "add credential\n\nCo-Authored-By: Test <t@example.invalid>")

    git(path, "switch", "-q", "work/base")
    git(path, "switch", "-qc", "work/clean")
    (path / "harmless.txt").write_text("harmless\n", encoding="utf-8")
    git(path, "add", "harmless.txt")
    git(path, "commit", "-qm", "add harmless\n\nCo-Authored-By: Test <t@example.invalid>")

    # Diverge, so a merge is a real merge commit rather than a fast-forward.
    git(path, "switch", "-q", "work/base")
    (path / "other.txt").write_text("other\n", encoding="utf-8")
    git(path, "add", "other.txt")
    git(path, "commit", "-qm", "diverge\n\nCo-Authored-By: Test <t@example.invalid>")

    # A patch arrives as a file from outside this repository: its message is
    # prose somebody else wrote, and its author header is text somebody else
    # chose. Both are scanned by different hooks from the patch's content, so
    # each gets its own fixture branch.
    git(path, "switch", "-q", "work/base")
    git(path, "switch", "-qc", "work/poisoned-message")
    (path / "note-a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", "note-a.txt")
    git(
        path,
        "commit",
        "-qm",
        f"add note a\n\npasted from the bug report: {secret}"
        "\n\nCo-Authored-By: Test <t@example.invalid>",
    )

    git(path, "switch", "-q", "work/base")
    git(path, "switch", "-qc", "work/poisoned-author")
    (path / "note-b.txt").write_text("b\n", encoding="utf-8")
    git(path, "add", "note-b.txt")
    git(
        path,
        "commit",
        "-qm",
        "add note b\n\nCo-Authored-By: Test <t@example.invalid>",
        env={
            "GIT_AUTHOR_NAME": f"Pasted {secret}",
            "GIT_AUTHOR_EMAIL": "pasted@example.invalid",
        },
    )

    git(path, "switch", "-q", "work/base")
    git(path, "switch", "-qc", "work/unattributed")
    (path / "note-c.txt").write_text("c\n", encoding="utf-8")
    git(path, "add", "note-c.txt")
    git(path, "commit", "-qm", "add note c with no attribution at all")

    git(path, "switch", "-q", "work/base")
    patches = path.parent / "patches"
    for branch in (
        "poisoned",
        "clean",
        "poisoned-message",
        "poisoned-author",
        "unattributed",
    ):
        git(path, "format-patch", "-q", "-1", f"work/{branch}", "-o", str(patches / branch))
    install_integration_hooks(path)
    return patches


def apply_patch(repo, patches, name, env=None):
    files = sorted((patches / name).glob("*.patch"))
    assert files, f"no patch was produced for {name}"
    return git(repo, "am", *[str(p) for p in files], check=False, env=env)


def head_of(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_merge_commit_is_checked_like_any_other_commit(tmp_path):
    # Ledger L15. Git runs `pre-merge-commit`, never `pre-commit`, for a merge
    # that resolves cleanly. With no such hook the credential scan, the stray
    # document rule and the branch rule were all absent on the path a session is
    # most likely to use when integrating someone else's work.
    repo = tmp_path / "repo"
    make_integration_repo(repo)
    before = head_of(repo)
    result = git(repo, "merge", "--no-edit", "work/poisoned", check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ingress check" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert head_of(repo) == before, "the merge commit was created despite the credential"


def test_clean_merge_still_completes(tmp_path):
    # The guard above must not cost the ordinary case. A merge hook that refuses
    # every merge would pass the test above and be useless.
    repo = tmp_path / "repo"
    make_integration_repo(repo)
    before = head_of(repo)
    result = git(repo, "merge", "--no-edit", "work/clean", check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert head_of(repo) != before
    parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, "expected a two-parent merge commit"


def test_merge_on_main_is_refused_like_a_commit_on_main(tmp_path):
    # Hard rule 3. A local merge into main *is* a commit on main, and the branch
    # rule has to reach it or the rule is enforced everywhere except the one
    # command that most looks like integration.
    repo = tmp_path / "repo"
    make_integration_repo(repo)
    git(repo, "branch", "-m", "work/base", "main")
    before = head_of(repo)
    result = git(repo, "merge", "--no-edit", "work/clean", check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "commit on main" in result.stderr
    assert head_of(repo) == before


def test_git_am_is_checked_like_any_other_commit(tmp_path):
    # Ledger L15, the other half: `git am` runs `pre-applypatch`, not
    # `pre-commit`. The patch is already in the working tree and the index by
    # then, which is exactly what the ingress check reads.
    repo = tmp_path / "repo"
    patches = make_integration_repo(repo)
    git(repo, "switch", "-qc", "work/apply")
    before = head_of(repo)
    poisoned = sorted((patches / "poisoned").glob("*.patch"))
    assert poisoned, "no patch was produced for the fixture"
    result = git(repo, "am", *[str(p) for p in poisoned], check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ingress check" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert head_of(repo) == before, "git am committed the patch despite the credential"
    git(repo, "am", "--abort", check=False)


def test_clean_git_am_still_applies(tmp_path):
    repo = tmp_path / "repo"
    patches = make_integration_repo(repo)
    git(repo, "switch", "-qc", "work/apply")
    before = head_of(repo)
    clean = sorted((patches / "clean").glob("*.patch"))
    result = git(repo, "am", *[str(p) for p in clean], check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert head_of(repo) != before
    assert (repo / "harmless.txt").is_file()


def test_git_am_scans_the_patch_message(tmp_path):
    # The likelier leak of the two. A patch file's commit message is prose
    # written outside this repository — a mailing list post, a colleague's
    # export, a bug report — and `git am` runs `applypatch-msg` for it, never
    # `commit-msg`. Nothing read that text at all.
    repo = tmp_path / "repo"
    patches = make_integration_repo(repo)
    git(repo, "switch", "-qc", "work/apply")
    before = head_of(repo)
    result = apply_patch(repo, patches, "poisoned-message")
    assert result.returncode != 0, result.stdout + result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert "credential" in result.stderr
    assert head_of(repo) == before
    assert not (repo / "note-a.txt").exists(), (
        "applypatch-msg runs before the patch is applied; nothing should have landed"
    )
    git(repo, "am", "--abort", check=False)


def test_git_am_scans_the_patch_author(tmp_path):
    # The patch's author is not the local identity: at applypatch-msg time
    # GIT_AUTHOR_NAME is unset and `git var` answers with the committer, so the
    # incoming author has to be read from git's own author-script or it is not
    # read at all.
    repo = tmp_path / "repo"
    patches = make_integration_repo(repo)
    git(repo, "switch", "-qc", "work/apply")
    before = head_of(repo)
    result = apply_patch(repo, patches, "poisoned-author")
    assert result.returncode != 0, result.stdout + result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert "author" in result.stderr.lower()
    assert head_of(repo) == before
    git(repo, "am", "--abort", check=False)


def test_git_am_requires_attribution_like_any_other_commit(tmp_path):
    # `git am` never ran commit-msg, so a patch could land with no statement of
    # which machine wrote it — the one thing every other commit here must carry.
    repo = tmp_path / "repo"
    patches = make_integration_repo(repo)
    git(repo, "switch", "-qc", "work/apply")
    before = head_of(repo)
    result = apply_patch(repo, patches, "unattributed")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "does not say which machine wrote it" in result.stderr
    assert head_of(repo) == before
    git(repo, "am", "--abort", check=False)


def test_git_am_attribution_has_the_declared_escape_hatch(tmp_path):
    # A patch from a human with no machine involved is exactly the case
    # ALLOW_UNATTRIBUTED=1 exists for, and it must still work through `git am`.
    repo = tmp_path / "repo"
    patches = make_integration_repo(repo)
    git(repo, "switch", "-qc", "work/apply")
    before = head_of(repo)
    result = apply_patch(repo, patches, "unattributed", env={"ALLOW_UNATTRIBUTED": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert head_of(repo) != before


def test_commit_message_hook_scans_the_author_header(tmp_path):
    # Ledger L16. `git commit --author=...` writes operator-supplied text into
    # the commit object, and nothing scanned it: the message check reads the
    # message only. A pasted token lives in a name field as happily as in prose.
    repo = tmp_path / "repo"
    make_integration_repo(repo)
    (repo / "note.txt").write_text("note\n", encoding="utf-8")
    git(repo, "add", "note.txt")
    result = git(
        repo,
        "commit",
        "--author",
        f"Pasted {SAMPLE_SECRET} <pasted@example.invalid>",
        "-m",
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>",
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert "author" in result.stderr.lower()


def test_commit_message_hook_scans_the_committer_header(tmp_path):
    # The committer identity is separate from the author and equally unscanned.
    repo = tmp_path / "repo"
    make_integration_repo(repo)
    (repo / "note.txt").write_text("note\n", encoding="utf-8")
    git(repo, "add", "note.txt")
    result = git(
        repo,
        "commit",
        "-m",
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>",
        check=False,
        env={"GIT_COMMITTER_NAME": f"Pasted {SAMPLE_SECRET}"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert "committer" in result.stderr.lower()


def test_ordinary_commit_still_lands_with_the_identity_scan_in_place(tmp_path):
    repo = tmp_path / "repo"
    make_integration_repo(repo)
    (repo / "note.txt").write_text("note\n", encoding="utf-8")
    git(repo, "add", "note.txt")
    result = git(
        repo,
        "commit",
        "-m",
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_commit_message_reports_a_check_that_could_not_run_as_such(tmp_path):
    # Ledger L26 and GOVERNANCE 10. check_ingress.py exits 2 when it could not
    # read what it was pointed at — a FIFO, a socket, a device, an unreadable
    # path. The hook treated every non-zero exit as "a credential was found",
    # which asserts a measurement that never happened and misdirects whoever is
    # trying to work out why their commit was refused.
    repo = tmp_path / "repo"
    make_commit_msg_repo(repo)
    (repo / ".githooks" / "check_ingress.py").write_text(
        "import sys\nprint('could not open the file', file=sys.stderr)\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    result = run_commit_message_in(
        repo,
        "Describe change\n\nCo-Authored-By: Test <t@example.invalid>\n",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not run" in result.stderr
    assert "matched a recognized credential" not in result.stderr


def test_commit_message_still_names_a_real_credential_match(tmp_path):
    # The counterpart: exit 1 must keep saying a credential was found. Splitting
    # the two exits must not blur them into one vague message.
    repo = tmp_path / "repo"
    make_commit_msg_repo(repo)
    result = run_commit_message_in(
        repo,
        f"Do not record {SAMPLE_SECRET}\n\nCo-Authored-By: Test <t@example.invalid>\n",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "credential" in result.stderr
    assert SAMPLE_SECRET not in result.stderr


def test_attribution_exemptions_do_not_exempt_a_poisoned_identity(tmp_path):
    # The message scan sits before every exemption for a stated reason:
    # attribution exemptions never exempt secrets. The identity scan has to sit
    # there too, or ALLOW_UNATTRIBUTED=1 — a variable an operator reaches for to
    # skip an *attribution* rule — silently skips a credential check as well.
    repo = tmp_path / "repo"
    make_commit_msg_repo(repo)
    result = run_commit_message_in(
        repo,
        "Describe change\n",
        env={
            "ALLOW_UNATTRIBUTED": "1",
            "GIT_AUTHOR_NAME": f"Pasted {SAMPLE_SECRET}",
            "GIT_AUTHOR_EMAIL": "pasted@example.invalid",
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert "credential" in result.stderr


def test_pre_push_reports_unknown_for_a_receipt_it_cannot_read(tmp_path):
    # Ledger L22's remaining half. A path that is not a regular file — a FIFO
    # planted or left by a crashed process, a socket, a directory — was reported
    # as "no receipt recorded", which is a measurement of zero coverage taken by
    # an instrument that read nothing (GOVERNANCE 10).
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    receipts = repo / ".git" / "audit-receipts"
    receipts.mkdir()
    os.mkfifo(receipts / sha)
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0, "the checklist must never refuse a push"
    assert "UNKNOWN" in result.stderr
    assert "no receipt recorded" not in result.stderr


def test_pre_push_still_reports_a_missing_receipt_as_missing(tmp_path):
    # Nothing at the path is a different state from something unreadable at it,
    # and both must keep their own words.
    repo = tmp_path / "repo"
    sha = make_audit_repo(repo)
    result = run_isolated_pre_push(repo, sha)
    assert result.returncode == 0
    assert "no reviewer recorded" in result.stderr
    assert "UNKNOWN" not in result.stderr


def test_reviewer_pass_scans_gpt_output_before_first_write():
    skill = (ROOT / ".claude" / "skills" / "reviewer-pass" / "SKILL.md").read_text(encoding="utf-8")
    capture = 'gpt_output=$(sh operations/codex/seat.sh judge - < "$prompt_path" 2>&1)'
    scan = "python3 .githooks/check_ingress.py --stdin-file"
    persist = 'printf \'%s\\n\' "$gpt_output" > "$gpt_temporary"'

    assert capture in skill
    assert scan in skill
    assert persist in skill
    assert skill.index(capture) < skill.index(scan) < skill.index(persist)
    assert '> "$gpt_report" 2>&1' not in skill
