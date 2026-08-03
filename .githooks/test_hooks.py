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


def static_gate_scripts():
    """The exact list of shell entrypoints check-static.sh promises to check."""
    body = (HOOKS / "check-static.sh").read_text().split('scripts="', 1)[1].split('"', 1)[0]
    listed = [line.strip() for line in body.splitlines() if line.strip()]
    assert len(listed) > 1, listed
    return listed


def make_static_gate_repo(path, broken=None):
    """A throwaway repo holding a stub for every script the gate names.

    ruff, shellcheck and the document check are stubbed out so the only live
    step is the syntax check under test. A fake `sh` records the operand of
    every `sh -n` call, which is exactly the one file a real `sh -n` reads.
    """
    repo = init_repo(path)
    listed = static_gate_scripts()
    for relative in listed:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\nexit 0\n")
    if broken is not None:
        # Unterminated `if`: a syntax error for dash and for bash alike, so the
        # fixture does not depend on which shell provides /bin/sh.
        (repo / broken).write_text("#!/bin/sh\nif true; then\n")
    shutil.copy2(HOOKS / "check-static.sh", repo / ".githooks" / "check-static.sh")

    stubs = repo / "stub-bin"
    stubs.mkdir()
    for name in ("ruff", "shellcheck"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    # The gate picks its syntax shell — dash where it exists, sh otherwise — so
    # both names are recorded. Stubbing only one meant the test broke the moment
    # the gate's preference changed, rather than when its coverage did.
    for name in ("sh", "dash"):
        recorder = stubs / name
        recorder.write_text(
            '#!/bin/sh\nif [ "${1:-}" = "-n" ]; then\n'
            '  printf \'%s\\n\' "${2:-}" >> "$SH_N_LOG"\nfi\nexec /bin/sh "$@"\n'
        )
        recorder.chmod(0o755)

    git(
        repo,
        "add",
        *listed,
        "stub-bin/ruff",
        "stub-bin/shellcheck",
        "stub-bin/sh",
        "stub-bin/dash",
    )
    git(repo, "commit", "-qm", "fixture", env={"ALLOW_UNATTRIBUTED": "1"})
    log = repo / "sh-n.log"
    environment = {"PATH": f"{stubs}:{os.environ['PATH']}", "SH_N_LOG": str(log)}
    return repo, listed, log, environment


@pytest.mark.full
def test_static_gate_syntax_checks_every_script_it_names(tmp_path):
    repo, listed, log, environment = make_static_gate_repo(tmp_path / "repo")
    result = run_hook(repo, "check-static.sh", env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    checked = [line for line in log.read_text().splitlines() if line]
    assert checked == listed


# These three run the whole static gate inside a fixture repo — the gate testing
# itself, on every commit. A genuinely broken script is already caught by the real
# `check-static.sh` run in the same gate; what these add is proof that the *list*
# is walked, which changes about as often as the list does. Reserved for the full
# gate so the everyday one stays worth running.
@pytest.mark.full
@pytest.mark.parametrize("position", [1, -1])
def test_static_gate_fails_on_a_broken_script_that_is_not_first(tmp_path, position):
    broken = static_gate_scripts()[position]
    repo, _listed, _log, environment = make_static_gate_repo(tmp_path / "repo", broken=broken)
    result = run_hook(repo, "check-static.sh", env=environment)
    assert result.returncode != 0, result.stdout + result.stderr
    assert Path(broken).name in result.stderr
    assert "syntax" in result.stderr.lower()


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
    copy_hooks(repo, "pre-push", "check_ingress.py")
    return repo, base, head


def reviewed_message(subject, reviewers=()):
    """A commit message carrying one `Reviewed-by:` trailer per reviewer."""
    body = f"{subject}\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    for reviewer in reviewers:
        body += f"Reviewed-by: {reviewer} <noreply@example.invalid>\n"
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


def test_a_bounded_drawer_is_not_reopened_by_the_generic_readme_rule():
    """`case` takes the first match, and the generic rule used to be first.

    `HANDOFF.md|*/README.md|*/HANDOFF.md` accepts those two names at *any* depth — `*`
    matches `/` in a shell case pattern — so it admitted
    `operations/autoclave/briefs/nested/README.md` before the one-level-deep rules
    further down could refuse it. Three drawers are bounded on purpose, and under those
    two filenames all three were not. Found by CodeRabbit on pull request 15.

    Both filenames are tested because only `README.md` was reported, and `HANDOFF.md`
    sits in the same alternation with the same defect.
    """
    reopened = [
        "operations/autoclave/briefs/nested/README.md",
        "operations/autoclave/briefs/nested/HANDOFF.md",
        ".claude/agents/nested/README.md",
        ".claude/agents/nested/HANDOFF.md",
        ".github/nested/README.md",
        ".github/nested/HANDOFF.md",
    ]
    for path in reopened:
        result = command(["sh", str(HOOKS / "doc-allowlist.sh")], stdin=f"{path}\n")
        assert result.returncode == 1, f"{path} was admitted into a bounded drawer"
        assert path in result.stdout, path
    # The positive control. Tightening the order must not refuse the one-level files
    # those drawers exist to hold, nor an ordinary README anywhere else in the tree.
    for path in (
        "operations/autoclave/briefs/README.md",
        "operations/autoclave/README.md",
        "workbench/README.md",
        ".claude/agents/README.md",
        "HANDOFF.md",
        "README.md",
    ):
        result = command(["sh", str(HOOKS / "doc-allowlist.sh")], stdin=f"{path}\n")
        assert result.returncode == 0, f"{path} was refused: {result.stdout}"


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


def test_document_check_rejects_control_character_paths(tmp_path):
    # A newline in a filename splits one record into two innocent-looking ones on
    # the way to the newline-delimited allowlist, so the paths are refused before
    # the allowlist is ever handed them.
    repo = make_document_repo(tmp_path / "repo")
    (repo / "evil\nREADME.md").write_text("not an allowed document\n")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "control-path" in result.stderr
    assert "unsafe to pass to the documentation allowlist" in result.stderr


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


def make_commit_message_repo(path):
    repo = init_repo(path)
    copy_hooks(repo, "commit-msg", "check_ingress.py")
    commit_file(repo, "safe.txt", "base\n", env={"ALLOW_UNATTRIBUTED": "1"})
    return repo


def run_commit_message_in(repo, message, env=None):
    """Run commit-msg inside a throwaway repo, so the identity it reads is ours.

    `git var` answers from the repository the hook runs in, so the author and
    committer scans cannot be exercised against a message file alone.
    """
    (repo / "message.txt").write_text(message)
    return run_hook(repo, "commit-msg", args=("message.txt",), env=env)


POISONED_NAME = f"Pasted {SAMPLE_SECRET}"


def test_commit_message_hook_scans_the_author_header(tmp_path):
    # `git commit --author=` and a pasted user.name write operator-supplied text
    # into the commit object; the message scan reads the message only. Only the
    # author is poisoned here, so dropping it from the scan would leave a clean
    # committer and an attributed message, and the commit would land.
    repo = make_commit_message_repo(tmp_path / "repo")
    result = run_commit_message_in(
        repo,
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>\n",
        env={
            "GIT_AUTHOR_NAME": POISONED_NAME,
            "GIT_AUTHOR_EMAIL": "pasted@example.invalid",
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "author" in result.stderr.lower()
    assert SAMPLE_SECRET not in result.stdout + result.stderr


def test_commit_message_hook_scans_the_committer_header(tmp_path):
    # The committer identity is separate from the author and equally carried into
    # the object. Poisoned alone for the same reason as above.
    repo = make_commit_message_repo(tmp_path / "repo")
    result = run_commit_message_in(
        repo,
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>\n",
        env={
            "GIT_COMMITTER_NAME": POISONED_NAME,
            "GIT_COMMITTER_EMAIL": "pasted@example.invalid",
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "committer" in result.stderr.lower()
    assert SAMPLE_SECRET not in result.stdout + result.stderr


def test_ordinary_commit_message_still_passes_with_the_identity_scan(tmp_path):
    # The other half: the scan must not refuse an ordinary configured identity,
    # or the tests above would pass for a hook that blocks everything.
    repo = make_commit_message_repo(tmp_path / "repo")
    result = run_commit_message_in(
        repo,
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("exemption", ["allow-unattributed", "fixup", "merge"])
def test_attribution_exemptions_do_not_exempt_a_poisoned_identity(tmp_path, exemption):
    # The identity scan sits before every attribution exemption for the same
    # reason the message scan does. Otherwise ALLOW_UNATTRIBUTED=1 — reached for
    # to skip an *attribution* rule — silently skips a credential check too, and
    # so does any commit git is merging or a `fixup!` subject.
    #
    # None of these messages carry a trailer, so an exemption that failed to fire
    # would be refused for attribution instead; asserting the credential wording
    # is what proves the scan ran ahead of an exemption that did fire.
    repo = make_commit_message_repo(tmp_path / "repo")
    message = "describe change\n"
    env = {
        "GIT_AUTHOR_NAME": POISONED_NAME,
        "GIT_AUTHOR_EMAIL": "pasted@example.invalid",
    }
    if exemption == "allow-unattributed":
        env["ALLOW_UNATTRIBUTED"] = "1"
    elif exemption == "fixup":
        message = "fixup! describe change\n"
    else:
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / ".git" / "MERGE_HEAD").write_text(f"{head}\n")
    result = run_commit_message_in(repo, message, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "credential pattern" in result.stderr
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
    # Checklist, not gate: an unreviewed push is reported and then allowed,
    # because nothing here turns on anything but Tyrel's word.
    assert "no commit in this push names a reviewer" in result.stderr
    assert "Checklist only" in result.stderr
    assert "Tyrel decides whether the coverage is enough" in result.stderr


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
def test_pre_push_scans_the_real_object_not_a_git_replace_substitute(tmp_path):
    # `git replace` makes every reader — this hook, the scanner it calls, and a
    # reviewer running `git show` — see a clean stand-in while the genuine object
    # is what leaves in the pack. GIT_NO_REPLACE_OBJECTS is what turns the whole
    # mechanism off; without it the outgoing scan reads a different history from
    # the one being pushed and reports it clean.
    repo, base, _head = audit_repo(tmp_path / "repo")
    unsafe = commit_file(repo, "safe.txt", SAMPLE_SECRET, "unsafe tip")
    innocent = git(
        repo, "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "innocent"
    ).stdout.strip()
    git(repo, "replace", "-f", unsafe, innocent)
    result = run_hook(repo, "pre-push", stdin=push_line(unsafe))
    assert result.returncode == 1, result.stderr
    assert "outgoing-history" in result.stderr.lower()
    assert SAMPLE_SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("with_earlier_record", [False, True])
def test_pre_push_checks_a_final_record_with_no_trailing_newline(tmp_path, with_earlier_record):
    # `while read -r a b c d` returns non-zero on an unterminated last line
    # *after* assigning the fields, so the loop body never ran for it: the ref
    # went out with no branch rule, no history scan and no checklist, and the
    # hook exited 0 — a gate that read nothing and looked exactly like one that
    # agreed. The second case is the shape git actually produces it in, several
    # refs at once with only the last truncated.
    repo, _base, head = audit_repo(tmp_path / "repo")
    stdin = push_line(head) if with_earlier_record else ""
    stdin += push_line(head, "refs/heads/main").rstrip("\n")
    result = run_hook(repo, "pre-push", stdin=stdin)
    assert result.returncode == 1, result.stderr
    assert "direct push to main" in result.stderr


def reviewed_repo(path, subjects_and_reviewers):
    """A repo whose outgoing commits carry the given `Reviewed-by:` trailers."""
    repo = init_repo(path)
    head = commit_file(repo, "safe.txt", "base\n")
    for index, (subject, reviewers) in enumerate(subjects_and_reviewers):
        head = commit_file(
            repo, "safe.txt", f"revision {index}\n", reviewed_message(subject, reviewers)
        )
    copy_hooks(repo, "pre-push", "check_ingress.py")
    return repo, head


def test_pre_push_lists_the_reviewers_the_outgoing_commits_name(tmp_path):
    repo, head = reviewed_repo(
        tmp_path / "repo",
        [
            ("first", ["Claude Opus 5", "GPT-5.6 Sol (OpenAI)"]),
            ("second", ["Claude Opus 5"]),
        ],
    )
    result = run_hook(repo, "pre-push", stdin=push_line(head))
    assert result.returncode == 0, result.stderr
    assert "[x] Claude Opus 5" in result.stderr
    assert "[x] GPT-5.6 Sol (OpenAI)" in result.stderr


def test_pre_push_counts_one_reviewer_once_across_commits_and_spellings(tmp_path):
    # Two commits naming the same seat is one reviewer, and case or run-together
    # whitespace must not turn it into two — a checklist that inflates its own
    # coverage is worse than none.
    repo, head = reviewed_repo(
        tmp_path / "repo",
        [("first", ["Claude Opus 5"]), ("second", ["claude  opus 5"])],
    )
    result = run_hook(repo, "pre-push", stdin=push_line(head))
    assert result.returncode == 0, result.stderr
    assert result.stderr.lower().count("[x] claude") == 1


def test_pre_push_reports_outgoing_commits_that_name_nobody(tmp_path):
    # Partial coverage is the case worth surfacing: some commits reviewed, some
    # slipped in behind them. Naming a reviewer at all must not imply the whole
    # push was read.
    repo, head = reviewed_repo(
        tmp_path / "repo",
        [("reviewed", ["Claude Opus 5"]), ("slipped in", [])],
    )
    result = run_hook(repo, "pre-push", stdin=push_line(head))
    assert result.returncode == 0, result.stderr
    assert "[x] Claude Opus 5" in result.stderr
    assert "outgoing commit(s) name no reviewer" in result.stderr


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


def test_install_creates_every_drawer_the_contract_declares(tmp_path):
    """The test above pre-creates six drawers and asserts only `core.hooksPath`, so
    it cannot see the installer dropping one. `quarantine/` was added on 2026-08-02
    without the installer following, and a fresh clone silently lacked the one-way
    staging drawer while `tidy.py` read its absence as empty. Nothing here is
    pre-created: the installer is the only thing that can make these appear.
    """
    repo = init_repo(tmp_path / "repo")
    shutil.copytree(HOOKS, repo / ".githooks")
    result = run_hook(repo, "install.sh")
    assert result.returncode == 0, result.stderr
    declared = (
        "active",
        "standing",
        "archive",
        "scratch",
        "design",
        "tools",
        "raw",
        "autoclave",
        "quarantine",
    )
    missing = [name for name in declared if not (repo / "workbench" / name).is_dir()]
    assert not missing, f"install.sh did not create: {missing}"


def test_tidy_names_the_chamber_drawers_no_installer_fills(tmp_path):
    """`autoclave.sh` writes `workbench/autoclave/<task>/` and `rm` keeps it.

    Nothing ages it, nothing empties it, and until now nothing counted it: a session
    read a clean workbench while chamber bundles accumulated beside it. The `scratch/`
    line is the positive control, because an assertion that `autoclave/` was reported
    is worth nothing beside a run that never looked at the workbench at all.
    """
    repo = init_repo(tmp_path / "repo")
    copy_hooks(repo, "tidy.py")
    chamber = repo / "workbench" / "autoclave" / "refactor-designator"
    chamber.mkdir(parents=True)
    (chamber / "report.md").write_text("what the chamber did\n")
    scratch = repo / "workbench" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "grep.txt").write_text("a dump\n")

    result = command(["python3", ".githooks/tidy.py"], cwd=repo)

    assert "scratch/" in result.stdout, "the report never ran"
    assert "autoclave/ 1 chamber drawers" in result.stdout, result.stdout
    assert "refactor-designator" in result.stdout, "the report must name the drawers it found"
    assert (chamber / "report.md").is_file(), "the report changes nothing"


def test_fixture_images_are_binary_at_any_depth(tmp_path):
    """`proof/fixtures/*` matched one path level while the fixtures live a directory
    deeper, so `git check-attr` reported `text=auto` on them and the explicit binary
    policy was not the thing applying. Asserted by asking git, not by reading the
    pattern — the pattern looked right before, too.
    """
    repo = init_repo(tmp_path / "repo")
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    nested = repo / "proof" / "fixtures" / "synthetic-two-page-v0"
    nested.mkdir(parents=True)
    (nested / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    reported = git(
        repo, "check-attr", "text", "--", "proof/fixtures/synthetic-two-page-v0/page-1.png"
    )
    assert reported.stdout.strip().endswith("text: unset"), reported.stdout


def failing_command_env(path, name):
    """A PATH whose first entry is one command that only ever fails."""
    stubs = path / f"stub-{name}"
    stubs.mkdir()
    stub = stubs / name
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    return {"PATH": f"{stubs}:{os.environ['PATH']}"}


@pytest.mark.parametrize("failing", ["chmod", "mkdir"])
def test_install_does_not_configure_hooks_when_a_prerequisite_fails(tmp_path, failing):
    # Both filesystem steps run before `git config` so that a fault leaves a
    # previously working hooksPath alone. A hooksPath pointed at files git cannot
    # execute is a clone reporting "Hooks installed" and running no hook at all.
    repo = init_repo(tmp_path / "repo")
    shutil.copytree(HOOKS, repo / ".githooks")
    git(repo, "config", "core.hooksPath", "previous-hooks")
    result = run_hook(repo, "install.sh", env=failing_command_env(tmp_path, failing))
    assert result.returncode != 0
    assert "Hooks installed" not in result.stdout
    if failing == "chmod":
        assert "not usable" in result.stderr
    assert git(repo, "config", "--get", "core.hooksPath").stdout.strip() == "previous-hooks"


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
