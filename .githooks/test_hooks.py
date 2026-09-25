"""Outcome tests for the repository's local Git alarms."""

import os
import shutil
import subprocess
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
    return repo


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


def test_pre_commit_hard_blocks_main_even_if_old_bypass_is_set(tmp_path):
    repo = make_precommit_repo(tmp_path / "repo", "main")
    (repo / "safe.txt").write_text("safe\n")
    git(repo, "add", "safe.txt")
    blocked = run_hook(repo, "pre-commit")
    old_bypass = run_hook(repo, "pre-commit", env={"ALLOW_MAIN_COMMIT": "1"})
    assert blocked.returncode == 1
    assert "commit on main" in blocked.stderr
    assert old_bypass.returncode == 1


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
    """Nothing is pre-created: only the installer can make these drawers appear."""
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
        "quarantine",
    )
    missing = [name for name in declared if not (repo / "workbench" / name).is_dir()]
    assert not missing, f"install.sh did not create: {missing}"


def test_fixture_images_are_binary_at_any_depth(tmp_path):
    """Asked of git, not read from the pattern: `proof/fixtures/*` once looked right yet
    matched only one level, leaving nested fixtures `text=auto`."""
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
    # A hooksPath at files git cannot execute would report installed and run no hook.
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
    assert run_hook(repo, "pre-commit").returncode == 1
