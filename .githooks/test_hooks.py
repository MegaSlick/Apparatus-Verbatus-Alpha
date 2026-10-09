"""Outcome tests for the repository's local Git alarms."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
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
    """A repo stubbing every named script, ruff and shellcheck; a fake `sh` logs each
    `sh -n` operand, the one file a real `sh -n` reads."""
    repo = init_repo(path)
    listed = static_gate_scripts()
    for relative in listed:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\nexit 0\n")
    if broken is not None:
        # Unterminated `if`: a syntax error for dash and bash alike.
        (repo / broken).write_text("#!/bin/sh\nif true; then\n")
    shutil.copy2(HOOKS / "check-static.sh", repo / ".githooks" / "check-static.sh")

    stubs = repo / "stub-bin"
    stubs.mkdir()
    for name in ("ruff", "shellcheck"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    # Both recorded: the gate prefers dash where it exists, sh otherwise.
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
    git(repo, "commit", "-qm", "fixture")
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


@pytest.mark.full
def test_static_gate_refuses_committed_trailing_whitespace(tmp_path):
    """A clean checkout has no working-tree diff, so the committed content is checked."""
    repo, _listed, _log, environment = make_static_gate_repo(tmp_path / "repo")
    commit_file(repo, "notes.txt", "trailing space \n")
    assert git(repo, "status", "--porcelain").stdout == ""
    result = run_hook(repo, "check-static.sh", env=environment)
    assert result.returncode != 0
    assert "trailing whitespace" in result.stdout + result.stderr


# The gate testing itself: these prove the list is walked, which rarely changes, so
# they are reserved for the full gate.
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
    # Every hook finds its interpreter through this one.
    for name in (*names, "find-python.sh"):
        shutil.copy2(HOOKS / name, target / name)
    return target


def run_hook(repo, name, *, stdin="", args=(), env=None):
    return command(
        ["sh", f".githooks/{name}", *args],
        cwd=repo,
        stdin=stdin,
        env=env,
    )


def make_document_repo(path):
    repo = init_repo(path)
    copy_hooks(repo, "check-documents.sh", "check_ingress.py")
    for name in (
        "README.md",
        "PRINCIPLES.md",
        "ARCHITECTURE.md",
        "GLOSSARY.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "CLAUDE.md",
    ):
        (repo / name).write_text(f"# {name}\n")
    write_separation(repo)
    return repo


def write_separation(repo, *rows):
    """A separation inventory whose table holds `rows` of (path, class)."""
    (repo / "docs").mkdir(exist_ok=True)
    table = "".join(f"| `{path}` | {kind} | |\n" for path, kind in rows)
    (repo / "docs" / "SEPARATION.md").write_text(
        f"# Separation\n\n| Path | Class | Note |\n|---|---|---|\n{table}"
    )


def track(repo, *names):
    for name in names:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("tracked\n")
    git(repo, "add", *names)


@pytest.mark.full
def test_the_root_readme_is_scanned_for_dates_like_every_other_document(tmp_path):
    """A dated line in a core document is refused, and the secret-safe output holds."""
    repo = make_document_repo(tmp_path / "repo")
    (repo / "README.md").write_text(
        f"# Apparatus Verbatus\n\n**Status — 2026-08-03:** alpha. {SAMPLE_SECRET}\n"
    )
    result = run_hook(repo, "check-documents.sh")
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    assert result.returncode == 1, "a dated status line in README.md was accepted"
    assert "dated state" in result.stderr
    assert "README.md" in result.stderr
    # And the positive control: undated, the same claim passes.
    (repo / "README.md").write_text("# Apparatus Verbatus\n\n**Status:** alpha.\n")
    assert run_hook(repo, "check-documents.sh").returncode == 0


def test_document_check_reports_a_missing_core_document(tmp_path):
    repo = make_document_repo(tmp_path / "repo")
    (repo / "PRINCIPLES.md").unlink()
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "missing core document: PRINCIPLES.md" in result.stderr


def test_document_check_rejects_control_character_paths(tmp_path):
    # A newline in a filename could split one record into two for later checks.
    repo = make_document_repo(tmp_path / "repo")
    (repo / "evil\nREADME.md").write_text("not an allowed document\n")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "control-path" in result.stderr


def test_separation_check_refuses_an_unclassified_path(tmp_path):
    repo = make_document_repo(tmp_path / "repo")
    write_separation(
        repo,
        ("LICENSE", "PRODUCT"),
        ("ops/pod/", "PRODUCT"),
        ("ops/pod/HANDOFF.md", "HISTORY"),
        ("ops/bench/", "HARNESS"),
    )
    track(repo, "LICENSE", "ops/pod/run.py", "ops/pod/HANDOFF.md", "ops/bench/scale.py")
    assert run_hook(repo, "check-documents.sh").returncode == 0

    # A new top-level path, and a new path in a folder classified below its top level.
    track(repo, "NOTES.md", "ops/spike/a.py", "ops/spike/b.py")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "unclassified path: NOTES.md\n" in result.stderr
    assert result.stderr.count("unclassified path: ops/spike/\n") == 1
    # A new file under a classified folder is covered by the folder's row.
    assert "ops/pod" not in result.stderr


def test_separation_check_refuses_a_row_with_no_tracked_path(tmp_path):
    repo = make_document_repo(tmp_path / "repo")
    write_separation(repo, ("LICENSE", "PRODUCT"), ("gone/", "HISTORY"))
    track(repo, "LICENSE")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "separation row matches no tracked path: gone/" in result.stderr


def test_separation_check_refuses_a_row_without_a_known_class(tmp_path):
    # The folder row covers the file, so only the bad class itself can fail the check.
    repo = make_document_repo(tmp_path / "repo")
    write_separation(repo, ("lib/", "PRODUCT"), ("lib/a.py", "MAYBE"))
    track(repo, "lib/a.py")
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "separation row has no known class: | `lib/a.py` | MAYBE |" in result.stderr


def test_separation_check_reports_a_missing_inventory(tmp_path):
    repo = make_document_repo(tmp_path / "repo")
    (repo / "docs" / "SEPARATION.md").unlink()
    result = run_hook(repo, "check-documents.sh")
    assert result.returncode == 1
    assert "missing separation inventory: docs/SEPARATION.md" in result.stderr


def run_commit_message(message, env=None):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
        handle.write(message)
        handle.flush()
        return command(
            ["sh", str(HOOKS / "commit-msg"), handle.name],
            env=env,
        )


def test_commit_message_with_a_credential_is_refused():
    message = f"accident {SAMPLE_SECRET}\n\nCo-Authored-By: GPT (OpenAI) <noreply@openai.com>\n"
    result = run_commit_message(message)
    assert result.returncode == 1
    assert "credential" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr


def make_commit_message_repo(path):
    repo = init_repo(path)
    copy_hooks(repo, "commit-msg", "check_ingress.py")
    commit_file(repo, "safe.txt", "base\n")
    return repo


def run_commit_message_in(repo, message, env=None):
    """Run commit-msg in a throwaway repo: `git var` answers from the repo the hook runs in."""
    (repo / "message.txt").write_text(message)
    return run_hook(repo, "commit-msg", args=("message.txt",), env=env)


POISONED_NAME = f"Pasted {SAMPLE_SECRET}"


def test_commit_message_hook_scans_the_author_header(tmp_path):
    # Only the author is poisoned: a message-only scan would let this commit land.
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
    # The committer is poisoned alone, for the same reason.
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
    # Positive control: a hook that blocked everything would pass the tests above.
    repo = make_commit_message_repo(tmp_path / "repo")
    result = run_commit_message_in(
        repo,
        "add a note\n\nCo-Authored-By: Test <t@example.invalid>\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def make_precommit_repo(path, branch="work/example"):
    repo = init_repo(path, branch)
    copy_hooks(repo, "pre-commit", "check_ingress.py")
    return repo


def test_pre_commit_refuses_a_staged_credential_without_echoing_it(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    (repo / "config.txt").write_text(f"token = {SAMPLE_SECRET}\n")
    git(repo, "add", "config.txt")
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1
    assert "ingress" in result.stderr
    assert SAMPLE_SECRET not in result.stdout + result.stderr
    # Positive control: the same hook accepts clean staged content.
    (repo / "config.txt").write_text("token = placeholder\n")
    git(repo, "add", "config.txt")
    assert run_hook(repo, "pre-commit").returncode == 0


def test_pre_commit_refuses_a_detached_head_unless_asked(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    commit_file(repo, "base.txt", "base\n")
    git(repo, "switch", "-q", "--detach")
    (repo / "safe.txt").write_text("safe\n")
    git(repo, "add", "safe.txt")
    blocked = run_hook(repo, "pre-commit")
    assert blocked.returncode == 1
    assert "detached" in blocked.stderr
    allowed = run_hook(repo, "pre-commit", env={"ALLOW_DETACHED_COMMIT": "1"})
    assert allowed.returncode == 0, allowed.stderr


def test_pre_commit_blocks_a_commit_on_main(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo", "main")
    (repo / "safe.txt").write_text("safe\n")
    git(repo, "add", "safe.txt")
    blocked = run_hook(repo, "pre-commit")
    assert blocked.returncode == 1
    assert "commit on main" in blocked.stderr


def test_install_configures_local_hooks(tmp_path):
    repo = init_repo(tmp_path / "repo")
    shutil.copytree(HOOKS, repo / ".githooks")
    result = run_hook(repo, "install.sh")
    assert result.returncode == 0, result.stderr
    assert git(repo, "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"


def test_fixture_images_are_binary_at_any_depth(tmp_path):
    """Asked of git, not read from the pattern: a one-level pattern such as
    `proof/fixtures/*` would leave nested fixtures `text=auto`."""
    repo = init_repo(tmp_path / "repo")
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    nested = repo / "proof" / "fixtures" / "synthetic-two-page-v0"
    nested.mkdir(parents=True)
    (nested / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    reported = git(
        repo, "check-attr", "text", "--", "proof/fixtures/synthetic-two-page-v0/page-1.png"
    )
    assert reported.stdout.strip().endswith("text: unset"), reported.stdout


def test_install_does_not_configure_hooks_when_chmod_fails(tmp_path):
    # A hooksPath at files git cannot execute would report installed and run no hook.
    repo = init_repo(tmp_path / "repo")
    shutil.copytree(HOOKS, repo / ".githooks")
    git(repo, "config", "core.hooksPath", "previous-hooks")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "chmod").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "chmod").chmod(0o755)
    result = run_hook(repo, "install.sh", env={"PATH": f"{stubs}:{os.environ['PATH']}"})
    assert result.returncode != 0
    assert "Hooks installed" not in result.stdout
    assert "not usable" in result.stderr
    assert git(repo, "config", "--get", "core.hooksPath").stdout.strip() == "previous-hooks"


def install_integration_hooks(repo):
    copy_hooks(
        repo,
        "pre-commit",
        "pre-merge-commit",
        "commit-msg",
        "check_ingress.py",
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
    )
    git(repo, "switch", "-q", "main")
    blocked = git(repo, "merge", "--no-edit", "--no-ff", "work/second", check=False)
    assert blocked.returncode != 0
    assert "commit on main" in blocked.stderr


def make_push_repo(path):
    """A repo with one clean commit and an empty bare remote. Its commits are made
    before the hooks are installed, as a commit made elsewhere or with hooks off is."""
    repo = init_repo(path)
    commit_file(repo, "base.txt", "base\n")
    remote = path.parent / "remote.git"
    git(path.parent, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    return repo


def push_with_hooks(repo):
    install_integration_hooks(repo)
    copy_hooks(repo, "pre-push")
    return git(repo, "push", "-q", "origin", "work/example", check=False)


@pytest.mark.full
def test_pre_push_allows_a_clean_new_branch_and_a_clean_update(tmp_path):
    repo = make_push_repo(tmp_path / "repo")
    first = push_with_hooks(repo)
    assert first.returncode == 0, first.stdout + first.stderr
    commit_file(repo, "more.txt", "more\n")
    second = git(repo, "push", "-q", "origin", "work/example", check=False)
    assert second.returncode == 0, second.stdout + second.stderr


@pytest.mark.full
@pytest.mark.parametrize("area", ["scriptorium", "workbench"])
def test_pre_push_refuses_a_new_branch_carrying_a_local_only_path(tmp_path, area):
    repo = make_push_repo(tmp_path / "repo")
    (repo / area).mkdir()
    commit_file(repo, f"{area}/x", "local page notes\n")
    result = push_with_hooks(repo)
    assert result.returncode != 0
    assert "[private-path]" in result.stderr
    assert "failed its ingress check" in result.stderr
    remote = tmp_path / "remote.git"
    assert git(remote, "branch", "--list").stdout.strip() == ""


@pytest.mark.full
def test_pre_push_scans_the_commits_an_update_adds(tmp_path):
    repo = make_push_repo(tmp_path / "repo")
    assert push_with_hooks(repo).returncode == 0
    # Committed with hooks off, as the pre-commit check would refuse it.
    git(repo, "config", "--unset", "core.hooksPath")
    (repo / "workbench").mkdir()
    commit_file(repo, "workbench/x", "local notes\n")
    git(repo, "rm", "-q", "workbench/x")
    git(repo, "commit", "-qm", "remove it again")
    git(repo, "config", "core.hooksPath", ".githooks")
    result = git(repo, "push", "-q", "origin", "work/example", check=False)
    assert result.returncode != 0
    assert "[private-path]" in result.stderr


@pytest.mark.full
def test_pre_push_scans_a_new_branch_from_where_it_left_main(tmp_path):
    repo = make_push_repo(tmp_path / "repo")
    install_integration_hooks(repo)
    copy_hooks(repo, "pre-push")
    assert git(repo, "push", "-q", "origin", "work/example:main", check=False).returncode == 0
    git(repo, "fetch", "-q", "origin")
    git(repo, "switch", "-qc", "work/clean")
    commit_file(repo, "clean.txt", "clean\n")
    clean = git(repo, "push", "-q", "origin", "work/clean", check=False)
    assert clean.returncode == 0, clean.stdout + clean.stderr

    git(repo, "switch", "-qc", "work/leak", "origin/main")
    git(repo, "config", "--unset", "core.hooksPath")
    (repo / "scriptorium").mkdir()
    commit_file(repo, "scriptorium/x", "local page notes\n")
    git(repo, "config", "core.hooksPath", ".githooks")
    blocked = git(repo, "push", "-q", "origin", "work/leak", check=False)
    assert blocked.returncode != 0
    assert "[private-path]" in blocked.stderr


def test_pre_push_refuses_when_the_ingress_check_is_missing_or_cannot_run(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    copy_hooks(repo, "pre-push")
    commit_file(repo, "base.txt", "base\n")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    line = f"refs/heads/work/example {head} refs/heads/work/example {'0' * 40}\n"
    stub_ingress(repo, ref_fields=2)
    result = run_hook(repo, "pre-push", stdin=line)
    assert result.returncode == 1
    assert "could not be checked" in result.stderr
    (repo / ".githooks" / "check_ingress.py").unlink()
    result = run_hook(repo, "pre-push", stdin=line)
    assert result.returncode == 1
    assert "is missing" in result.stderr


def stub_ingress(repo, *, message=0, ref_fields=0, staged=0):
    """A check_ingress.py that exits with the given status for each mode it is run in."""
    (repo / ".githooks" / "check_ingress.py").write_text(
        "import sys\n"
        "sys.stdin.buffer.read() if '--ref-fields' in sys.argv else None\n"
        f"codes = {{'--message-file': {message}, '--ref-fields': {ref_fields}, "
        f"'--staged': {staged}}}\n"
        "raise SystemExit(next(codes[a] for a in sys.argv if a in codes))\n"
    )


@pytest.mark.parametrize(
    ("codes", "refusal"),
    [
        ({"message": 1}, "matched a recognized credential pattern"),
        ({"message": 2}, "exited 2, meaning it could not run"),
        ({"ref_fields": 1}, "author or committer header matched"),
        ({"ref_fields": 2}, "headers could not be checked"),
    ],
)
def test_commit_message_hook_refuses_on_every_nonzero_ingress_status(tmp_path, codes, refusal):
    repo = make_commit_message_repo(tmp_path / "repo")
    stub_ingress(repo, **codes)
    result = run_commit_message_in(repo, "add a note\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert refusal in result.stderr


def test_commit_message_hook_refuses_when_the_ingress_check_is_missing(tmp_path):
    repo = make_commit_message_repo(tmp_path / "repo")
    (repo / ".githooks" / "check_ingress.py").unlink()
    result = run_commit_message_in(repo, "add a note\n")
    assert result.returncode == 1
    assert "is missing" in result.stderr


def test_pre_commit_refuses_when_the_ingress_check_cannot_run(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    stub_ingress(repo, staged=2)
    assert run_hook(repo, "pre-commit").returncode == 1
    (repo / ".githooks" / "check_ingress.py").unlink()
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1
    assert "is missing" in result.stderr


def test_pre_merge_commit_refuses_when_pre_commit_fails_or_is_missing(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    copy_hooks(repo, "pre-merge-commit")
    stub_ingress(repo, staged=2)
    assert run_hook(repo, "pre-merge-commit").returncode == 1
    stub_ingress(repo)
    assert run_hook(repo, "pre-merge-commit").returncode == 0
    (repo / ".githooks" / "pre-commit").unlink()
    result = run_hook(repo, "pre-merge-commit")
    assert result.returncode == 1
    assert "is missing" in result.stderr


def test_pre_commit_scans_the_index_not_the_working_copy(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    (repo / "config.txt").write_text(f"token = {SAMPLE_SECRET}\n")
    git(repo, "add", "config.txt")
    (repo / "config.txt").write_text("clean\n")
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1
    assert "[runpod-api-key]" in result.stdout + result.stderr
    git(repo, "add", "config.txt")
    assert run_hook(repo, "pre-commit").returncode == 0


PAGEKIT_GATE = (
    "pagekit/__init__.py",
    "pagekit/cleanroom/__init__.py",
    "pagekit/cleanroom/scan.py",
    "pagekit/cleanroom/gate.py",
    "pagekit/cleanroom/deny-hashes.txt",
)


def make_pagekit_repo(path):
    """A repo with the pre-commit hook, pagekit's gate and one committed pagekit file."""
    repo = make_precommit_repo(path)
    for relative in PAGEKIT_GATE:
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, repo / relative)
    (repo / "pagekit" / "check.py").write_text("VALUE = 1\n")
    git(repo, "add", ".githooks", "pagekit")
    git(repo, "commit", "-qm", "fixture")
    return repo


def stage(repo, relative, text):
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    (repo / relative).write_text(text)
    git(repo, "add", relative)


def test_pre_commit_holds_pagekit_until_the_lead_decides(tmp_path):
    repo = make_pagekit_repo(tmp_path / "repo")
    hold = "pagekit/cleanroom/HOLD"
    note = "pagekit/cleanroom/incidents/0001.md"
    # An uncommitted HOLD in the working copy already holds pagekit.
    (repo / hold).write_text("Suspected leak in the crop check.\n")
    stage(repo, "pagekit/check.py", "VALUE = 2\n")
    held = run_hook(repo, "pre-commit")
    assert held.returncode == 1, held.stdout + held.stderr
    assert "pagekit/check.py: pagekit is on HOLD" in held.stderr
    git(repo, "reset", "-q", "pagekit/check.py")
    git(repo, "checkout", "-q", "pagekit/check.py")

    # The pause itself: HOLD and an undecided incident note may be committed.
    git(repo, "add", hold)
    stage(repo, note, "# Incident 0001\n\nDecision:\n")
    assert run_hook(repo, "pre-commit").returncode == 0
    git(repo, "commit", "-qm", "hold")

    # While held, pagekit changes are refused; changes outside pagekit are not.
    stage(repo, "pagekit/check.py", "VALUE = 3\n")
    assert run_hook(repo, "pre-commit").returncode == 1
    git(repo, "reset", "-q", "pagekit/check.py")
    stage(repo, "notes.txt", "unrelated\n")
    assert run_hook(repo, "pre-commit").returncode == 0, "a change outside pagekit was held"

    # Removing HOLD needs the lead's decision in the same commit.
    git(repo, "rm", "-q", hold)
    undecided = run_hook(repo, "pre-commit")
    assert undecided.returncode == 1
    assert "Decision" in undecided.stderr
    stage(repo, note, "# Incident 0001\n\nDecision: false flag, a common idiom.\n")
    decided = run_hook(repo, "pre-commit")
    assert decided.returncode == 0, decided.stdout + decided.stderr
    git(repo, "commit", "-qm", "lift hold")

    stage(repo, "pagekit/check.py", "VALUE = 4\n")
    assert run_hook(repo, "pre-commit").returncode == 0


def test_pre_commit_scans_staged_pagekit_files_without_echoing_the_hit(tmp_path):
    repo = make_pagekit_repo(tmp_path / "repo")
    header = "under the GNU General " + "Public License, version 3"
    stage(repo, "pagekit/deskew.py", f"# {header}\nVALUE = 1\n")
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1
    assert "pagekit/deskew.py:1: gpl_licence_header" in result.stderr
    assert header not in result.stdout + result.stderr
    stage(repo, "pagekit/deskew.py", "VALUE = 1\n")
    assert run_hook(repo, "pre-commit").returncode == 0


def test_pre_commit_refuses_when_the_clean_room_has_no_gate(tmp_path):
    repo = make_pagekit_repo(tmp_path / "repo")
    stage(repo, "pagekit/check.py", "VALUE = 2\n")
    assert run_hook(repo, "pre-commit").returncode == 0
    git(repo, "rm", "-q", "pagekit/cleanroom/gate.py")
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pagekit/cleanroom/gate.py is missing" in result.stderr


def test_pre_commit_refuses_when_the_gate_is_unstaged_but_still_on_disk(tmp_path):
    repo = make_pagekit_repo(tmp_path / "repo")
    git(repo, "rm", "-q", "--cached", "pagekit/cleanroom/gate.py")
    assert (repo / "pagekit/cleanroom/gate.py").is_file()
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pagekit/cleanroom/gate.py is missing" in result.stderr


def test_pre_commit_runs_without_the_gate_where_there_is_no_clean_room(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo")
    stage(repo, "pagekit/check.py", "VALUE = 1\n")
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 0, result.stdout + result.stderr


def old_python_first(tmp_path):
    """A PATH whose first python3 is too old for the checks, as macOS's own 3.9 is."""
    fake = tmp_path / "old-python"
    fake.mkdir()
    python3 = fake / "python3"
    python3.write_text(
        "#!/bin/sh\n"
        'case "$*" in *version_info*) exit 1 ;; esac\n'
        "echo \"ModuleNotFoundError: No module named 'tomllib'\" >&2\n"
        "exit 1\n"
    )
    python3.chmod(0o755)
    return {"PATH": f"{fake}:{os.environ['PATH']}"}


@pytest.mark.parametrize("hook", ["commit-msg", "pre-commit"])
def test_a_hook_with_only_an_old_python_says_so_not_credential(tmp_path, hook):
    repo = make_commit_message_repo(tmp_path / "repo")
    copy_hooks(repo, "pre-commit")
    (repo / "staged.txt").write_text("fine\n")
    git(repo, "add", "staged.txt")
    args = ("message.txt",) if hook == "commit-msg" else ()
    (repo / "message.txt").write_text("add a note\n")
    result = run_hook(repo, hook, args=args, env=old_python_first(tmp_path))
    assert result.returncode == 1
    assert "Python 3.11 or later" in result.stderr
    assert "uv sync" in result.stderr
    assert "credential" not in result.stderr


@pytest.mark.parametrize("hook", ["commit-msg", "pre-commit"])
def test_a_hook_prefers_the_checkout_s_venv_over_an_old_python3(tmp_path, hook):
    repo = make_commit_message_repo(tmp_path / "repo")
    copy_hooks(repo, "pre-commit")
    venv = repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    (repo / "staged.txt").write_text("fine\n")
    git(repo, "add", "staged.txt")
    args = ("message.txt",) if hook == "commit-msg" else ()
    (repo / "message.txt").write_text("add a note\n")
    result = run_hook(repo, hook, args=args, env=old_python_first(tmp_path))
    assert result.returncode == 0, result.stderr
