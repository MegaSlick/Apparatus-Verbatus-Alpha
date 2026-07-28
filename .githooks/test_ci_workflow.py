"""Behaviour tests for the CI workflow's own shell.

The workflow's gates were previously held only by substring assertions in
`test_hooks.py`: commenting out the annotated-tag scan, or neutering the
autoclave stray count, left every asserted substring in place. A lock that
survives the deletion of the thing it locks is not a guard.

These tests extract the `run:` blocks out of `ci.yml` and execute them, so a
disabled gate fails here. They deliberately avoid PyYAML: CI installs only
`requirements-dev.txt`, which does not carry it, and a test that cannot run
is a failure rather than a pass.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
INGRESS = ROOT / ".githooks" / "check_ingress.py"

# GitHub Actions runs `shell: bash` steps this way. Reproducing it matters:
# `pipefail` is what makes a failing scan inside a pipeline fail the step.
BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_after(lines: list[str], start: int, header_indent: int) -> list[str]:
    """Collect the lines belonging to the construct opened at `start`."""
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and _indent_of(line) <= header_indent:
            break
        body.append(line)
    return body


def step_run_block(step_name: str, workflow: str | None = None) -> str:
    """Return the shell of the step named `step_name`, dedented."""
    lines = (workflow if workflow is not None else workflow_text()).splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {step_name}":
            step_body = _block_after(lines, index, _indent_of(line))
            break
    else:
        raise AssertionError(f"CI declares no step named {step_name!r}")

    for index, line in enumerate(step_body):
        if re.fullmatch(r"\s*run:\s*\|\s*", line):
            run_body = _block_after(step_body, index, _indent_of(line))
            return textwrap.dedent("\n".join(run_body)) + "\n"
        if re.fullmatch(r"\s*run:\s+\S.*", line):
            return line.split("run:", 1)[1].strip() + "\n"
    raise AssertionError(f"step {step_name!r} runs no shell")


def job_block(job_name: str, workflow: str | None = None) -> str:
    lines = (workflow if workflow is not None else workflow_text()).splitlines()
    for index, line in enumerate(lines):
        if re.fullmatch(rf"\s*{re.escape(job_name)}:\s*", line):
            return "\n".join(_block_after(lines, index, _indent_of(line)))
    raise AssertionError(f"CI declares no job named {job_name!r}")


def run_shell(script: str, cwd: Path, env: dict[str, str] | None = None):
    environment = dict(os.environ)
    environment.pop("GITHUB_REF", None)
    environment.pop("GITHUB_HEAD_REF", None)
    environment.update(env or {})
    return subprocess.run(
        BASH + [script],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


RECORDER = """\
import sys
from pathlib import Path

sys.stdin.buffer.read()
Path(__file__).parent.parent.joinpath("argv.log").open("a", encoding="utf-8").write(
    " ".join(sys.argv[1:]) + "\\n"
)
"""


@pytest.fixture
def recorded_ingress(tmp_path):
    """A stand-in `check_ingress.py` that records how CI invoked it."""
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "check_ingress.py").write_text(RECORDER, encoding="utf-8")
    return tmp_path


def invocations(workspace: Path) -> list[str]:
    log = workspace / "argv.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --- L37 / L38: the ingress step actually issues every scan it claims to ---


def test_ci_ingress_step_scans_ref_fields_history_and_tag_object(recorded_ingress):
    result = run_shell(
        step_run_block("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/tags/v9.9.9", "GITHUB_HEAD_REF": ""},
    )
    assert result.returncode == 0, result.stderr
    assert invocations(recorded_ingress) == [
        "--ref-fields",
        "--history HEAD",
        "--ref-object refs/tags/v9.9.9",
    ]


def test_ci_ingress_step_scans_history_on_a_branch_push_too(recorded_ingress):
    result = run_shell(
        step_run_block("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/heads/main", "GITHUB_HEAD_REF": "work/topic"},
    )
    assert result.returncode == 0, result.stderr
    calls = invocations(recorded_ingress)
    assert "--history HEAD" in calls, "the full-history secret scan is not reached"
    assert not any(call.startswith("--ref-object") for call in calls), (
        "a branch ref was peeled as a tag object"
    )


def test_ci_ingress_step_fails_when_a_scan_reports_a_finding(recorded_ingress):
    refusing = "import sys; sys.stdin.buffer.read(); sys.exit(1)\n"
    (recorded_ingress / ".githooks" / "check_ingress.py").write_text(refusing, encoding="utf-8")
    result = run_shell(
        step_run_block("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/heads/main", "GITHUB_HEAD_REF": ""},
    )
    assert result.returncode != 0, "a refused ingress scan left the CI step green"


# --- L38: the history scan and the full clone it depends on ---


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "--initial-branch", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    return path


def scan(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INGRESS), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_history_scan_catches_a_secret_deleted_before_head(tmp_path):
    """`--history HEAD` is the only reason a later deletion cannot turn CI green."""
    repo = new_repo(tmp_path / "repo")
    secret = "ghp_" + "a" * 10 + "TEST" + "b" * 16
    (repo / "app.py").write_text(f'TOKEN = "{secret}"\n', encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "--quiet", "-m", "add app")
    (repo / "app.py").write_text('TOKEN = ""\n', encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "--quiet", "-m", "remove token")

    clean_tip = scan(repo, "--worktree")
    assert clean_tip.returncode == 0, clean_tip.stderr

    found = scan(repo, "--history", "HEAD")
    assert found.returncode == 1, found.stdout + found.stderr
    assert secret not in found.stderr


def test_history_scan_refuses_a_shallow_clone(tmp_path):
    """This is what `fetch-depth: 0` buys; without it the scan must fail, not pass."""
    repo = new_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "--quiet", "-m", "one")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "--quiet", "-m", "two")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", repo.as_uri(), str(shallow)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result = scan(shallow, "--history", "HEAD")
    assert result.returncode != 0, "a shallow clone reported a clean history"
    assert "complete clone" in result.stderr


def test_ci_checks_out_full_history_for_the_job_that_scans_it():
    for name in ("check", "autoclave-empty"):
        block = job_block(name)
        if "--history HEAD" not in block:
            continue
        assert re.search(r"(?m)^\s*fetch-depth:\s*0\s*$", block), (
            f"job {name!r} scans full history without checking it out"
        )
        return
    raise AssertionError("no CI job runs the full-history secret scan")


# --- L37: the autoclave gate ---


def autoclave_gate() -> str:
    return step_run_block("The autoclave is empty")


def prepared_tray(tmp_path: Path, *paths: str) -> Path:
    repo = new_repo(tmp_path / "repo")
    (repo / "autoclave").mkdir()
    for relative in paths:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("draft\n", encoding="utf-8")
        git(repo, "add", "--", relative)
    return repo


def gate_env(tmp_path: Path) -> dict[str, str]:
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    return {"RUNNER_TEMP": str(runner_temp)}


def test_autoclave_gate_fails_on_a_stray_draft(tmp_path):
    repo = prepared_tray(tmp_path, "autoclave/README.md", "autoclave/draft.py")
    result = run_shell(autoclave_gate(), repo, gate_env(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 unsterilized draft" in result.stderr


def test_autoclave_gate_counts_every_stray(tmp_path):
    repo = prepared_tray(tmp_path, "autoclave/README.md", "autoclave/a.py", "autoclave/nested/b.py")
    result = run_shell(autoclave_gate(), repo, gate_env(tmp_path))
    assert result.returncode == 1
    assert "2 unsterilized draft" in result.stderr


def test_autoclave_gate_is_not_fooled_by_a_newline_in_a_filename(tmp_path):
    repo = prepared_tray(tmp_path, "autoclave/README.md")
    tricky = "autoclave/x\nautoclave/README.md"
    (repo / "autoclave" / "x\nautoclave").mkdir(parents=True, exist_ok=True)
    (repo / tricky).write_text("draft\n", encoding="utf-8")
    git(repo, "add", "--", tricky)
    result = run_shell(autoclave_gate(), repo, gate_env(tmp_path))
    assert result.returncode == 1, "a crafted filename matched the README exemption"


def test_autoclave_gate_passes_on_an_empty_tray(tmp_path):
    repo = prepared_tray(tmp_path, "autoclave/README.md")
    result = run_shell(autoclave_gate(), repo, gate_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_autoclave_gate_fails_closed_when_git_cannot_list(tmp_path):
    not_a_repo = tmp_path / "loose"
    not_a_repo.mkdir()
    result = run_shell(autoclave_gate(), not_a_repo, gate_env(tmp_path))
    assert result.returncode == 2, "an unlistable tray read as empty"
    assert "git ls-files failed" in result.stderr
