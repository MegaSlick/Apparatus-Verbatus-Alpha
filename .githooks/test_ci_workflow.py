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
    # The property, not the tally: every checkout step must decline to persist
    # credentials, however many jobs the workflow grows.
    checkouts = re.findall(r"(?ms)^\s*-\s+uses:\s*actions/checkout@\S+\n(.*?)(?=^\s*-\s|\Z)", text)
    assert checkouts, "no actions/checkout step found"
    assert all("persist-credentials: false" in block for block in checkouts)
    uses = re.findall(r"(?m)^\s*-\s+uses:\s*(\S+)\s*$", text)
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", value) for value in uses)


def test_ci_installs_the_project_before_running_a_gate_that_imports_it():
    """The first runtime dependency turns the gate red unless CI installs the
    project — or worse, leaves it green over tests that mocked the import away."""
    text = workflow_text()
    assert "python -m pip install ." in text
    assert "python -m pip install -r requirements-dev.txt" in text
    # The lock is checked for currency, not merely installed from.
    assert "uv lock --check" in text
    assert "uv sync --frozen" in text


def test_every_runtime_dependency_is_inside_what_the_audit_reads():
    """The audit reads requirements-dev.txt. A runtime dependency declared only
    in pyproject.toml would therefore never be audited, and nothing else would
    notice — the gate would stay green over an unexamined package.

    Names only, deliberately: the version each file pins is checked by the lock,
    and duplicating that comparison here would make one legitimate bump fail in
    two places with two different messages.
    """
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    audited = (ROOT / "requirements-dev.txt").read_text().splitlines()

    def name(requirement):
        return re.split(r"[=<>!~\[]", requirement.strip(), maxsplit=1)[0].strip().lower()

    missing = sorted({name(item) for item in declared} - {name(item) for item in audited if item})
    assert not missing, (
        f"runtime dependencies {missing} are not in requirements-dev.txt, so "
        "`pip_audit --requirement requirements-dev.txt` never looks at them"
    )


def test_the_audit_runs_in_the_gate_and_fails_closed():
    gate = (ROOT / ".githooks" / "check-all.sh").read_text()
    assert "python3 -m pip_audit --strict --requirement requirements-dev.txt" in gate
    # Not swallowed: `set -eu` is in force, and nothing rescues a non-zero exit.
    assert "set -eu" in gate
    for rescue in ("|| true", "|| :", "continue-on-error", "set +e"):
        assert rescue not in gate, f"the audit's failure is swallowed by {rescue!r}"


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


def test_cleanroom_gate_distinguishes_empty_and_loaded_tray(tmp_path):
    gate = step_run("The cleanroom is empty")
    empty = tray_repo(tmp_path / "empty", "cleanroom/README.md")
    loaded = tray_repo(
        tmp_path / "loaded",
        "cleanroom/README.md",
        "cleanroom/draft.py",
        "cleanroom/nested/other.py",
    )
    assert run_shell(gate, empty, gate_env(tmp_path)).returncode == 0
    result = run_shell(gate, loaded, gate_env(tmp_path))
    assert result.returncode == 1
    assert "2 unsterilized draft" in result.stderr


def test_cleanroom_gate_fails_when_git_cannot_list(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    result = run_shell(step_run("The cleanroom is empty"), loose, gate_env(tmp_path))
    assert result.returncode == 2
    assert "git ls-files failed" in result.stderr


def test_every_third_party_import_in_the_gate_suite_is_inside_what_the_audit_reads():
    """The sibling above covers the project's runtime dependencies. This covers
    the gate's own: a package these hook tests import, but nothing declares,
    reaches the gate only as some other dependency's transitive -- unpinned,
    unrecorded, and one upstream trim away from turning collection red with a
    message about the wrong thing.

    Found in audit (R0): `.githooks/test_r0_contract_ci_matrix.py` imports
    `yaml`, which appeared in no requirements file, no pyproject entry and no
    workflow step; it was present only because `huggingface_hub` requires
    PyYAML.
    """
    import ast
    import sys

    roots: set[str] = set()
    for path in sorted((ROOT / ".githooks").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])

    audited = {
        re.split(r"[=<>!~\[]", line.strip(), maxsplit=1)[0].strip().lower()
        for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # Import name to distribution name, for the few that differ.
    distribution = {"yaml": "pyyaml"}
    undeclared = sorted(
        root
        for root in roots
        if root not in sys.stdlib_module_names
        and distribution.get(root, root).lower() not in audited
    )
    assert not undeclared, (
        f"the gate's own suite imports {undeclared}, which requirements-dev.txt does not "
        "declare, so `pip_audit --requirement requirements-dev.txt` never looks at them "
        "and nothing pins the version the gate actually runs"
    )
