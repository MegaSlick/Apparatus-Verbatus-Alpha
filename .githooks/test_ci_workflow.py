"""Executable wiring tests for the small GitHub Actions workflow."""

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]


def workflow_text():
    return WORKFLOW.read_text()


def block_after(lines, start, indent):
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return body


def step_run(name):
    lines = workflow_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {name}":
            step = block_after(lines, index, len(line) - len(line.lstrip()))
            break
    else:
        raise AssertionError(f"missing CI step {name!r}")
    for index, line in enumerate(step):
        if re.fullmatch(r"\s*run:\s*\|\s*", line):
            body = block_after(step, index, len(line) - len(line.lstrip()))
            return textwrap.dedent("\n".join(body)) + "\n"
        if re.fullmatch(r"\s*run:\s+\S.*", line):
            return line.split("run:", 1)[1].strip() + "\n"
    raise AssertionError(f"CI step {name!r} has no run command")


def run_shell(script, cwd, env=None):
    runtime = dict(os.environ)
    runtime.update(env or {})
    return subprocess.run(
        BASH + [script],
        cwd=cwd,
        env=runtime,
        capture_output=True,
        text=True,
        timeout=30,
    )


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )


def new_repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def test_workflow_has_one_history_scan_and_immutable_dependencies():
    text = workflow_text()
    assert text.count("python3 .githooks/check_ingress.py --history HEAD") == 1
    assert "run: sh .githooks/check-all.sh --ci" in text
    assert "fetch-depth: 0" in text
    assert text.count("persist-credentials: false") == 2
    uses = re.findall(r"(?m)^\s*-\s+uses:\s*(\S+)\s*$", text)
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", value) for value in uses)


@pytest.fixture
def recorded_ingress(tmp_path):
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "check_ingress.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.stdin.buffer.read()\n"
        "Path('calls').open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    return tmp_path


def calls(repo):
    path = repo / "calls"
    return path.read_text().splitlines() if path.exists() else []


def test_ingress_step_scans_tag_ref_history_and_tag_object(recorded_ingress):
    result = run_shell(
        step_run("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/tags/v1.0.0", "GITHUB_HEAD_REF": ""},
    )
    assert result.returncode == 0, result.stderr
    assert calls(recorded_ingress) == [
        "--ref-fields",
        "--history HEAD",
        "--ref-object refs/tags/v1.0.0",
    ]


def test_ingress_step_on_branch_skips_tag_object_and_fails_closed(recorded_ingress):
    result = run_shell(
        step_run("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/heads/main", "GITHUB_HEAD_REF": "work/topic"},
    )
    assert result.returncode == 0
    assert calls(recorded_ingress) == ["--ref-fields", "--history HEAD"]

    (recorded_ingress / ".githooks" / "check_ingress.py").write_text(
        "import sys\nsys.stdin.buffer.read()\nraise SystemExit(1)\n"
    )
    failed = run_shell(
        step_run("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/heads/main", "GITHUB_HEAD_REF": ""},
    )
    assert failed.returncode != 0


def tray_repo(path, *tracked):
    repo = new_repo(path)
    for relative in tracked:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n")
        git(repo, "add", "--", relative)
    return repo


def gate_env(tmp_path):
    runtime = tmp_path / "runner"
    runtime.mkdir(exist_ok=True)
    return {"RUNNER_TEMP": str(runtime)}


def test_autoclave_gate_distinguishes_empty_and_loaded_tray(tmp_path):
    gate = step_run("The autoclave is empty")
    empty = tray_repo(tmp_path / "empty", "autoclave/README.md")
    loaded = tray_repo(
        tmp_path / "loaded",
        "autoclave/README.md",
        "autoclave/draft.py",
        "autoclave/nested/other.py",
    )
    assert run_shell(gate, empty, gate_env(tmp_path)).returncode == 0
    result = run_shell(gate, loaded, gate_env(tmp_path))
    assert result.returncode == 1
    assert "2 unsterilized draft" in result.stderr


def test_autoclave_gate_fails_when_git_cannot_list(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    result = run_shell(step_run("The autoclave is empty"), loose, gate_env(tmp_path))
    assert result.returncode == 2
    assert "git ls-files failed" in result.stderr
