"""Executable wiring tests for the small GitHub Actions workflow."""

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FROZEN_AUDIT_REQUIREMENTS = ROOT / ".githooks" / "frozen_audit_requirements.py"
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


def test_ci_installs_the_frozen_project_environment_before_running_the_gate():
    """The gate must execute the lockfile environment, not PATH's Python."""
    text = workflow_text()
    # The lock is checked for currency, not merely installed from.
    assert "uv lock --check" in text
    assert "uv sync --frozen --group test --group audit" in text
    assert "python -m pip install ." not in text


def test_every_runtime_dependency_is_inside_the_image_and_everyday_environment():
    """Every runtime dependency must reach the image and everyday gate."""
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    requirements = (ROOT / "requirements-dev.txt").read_text().splitlines()

    def name(requirement):
        return re.split(r"[=<>!~\[]", requirement.strip(), maxsplit=1)[0].strip().lower()

    missing = sorted(
        {name(item) for item in declared} - {name(item) for item in requirements if item}
    )
    assert not missing, (
        f"runtime dependencies {missing} are not in requirements-dev.txt, so "
        "the chamber image and everyday gate do not install them"
    )


def _pinned(requirements):
    """Map every `name==version` line to its normalized distribution name."""

    pins = {}
    for line in requirements:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        distribution, separator, version = entry.partition("==")
        assert separator and version, f"{entry!r} is not an exact pin"
        # PEP 503 normalization: `huggingface_hub` and `huggingface-hub` are the
        # same distribution, and the two files spell several of them differently.
        pins[re.sub(r"[-_.]+", "-", distribution.strip()).lower()] = version.strip()
    return pins


def test_the_image_requirements_match_the_projects_declared_direct_environment():
    """Both independently consumed declarations must pin the same direct environment."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    installed = _pinned(
        [
            *project["project"]["dependencies"],
            *(entry for group in project["dependency-groups"].values() for entry in group),
        ]
    )
    image_requirements = _pinned((ROOT / "requirements-dev.txt").read_text().splitlines())

    assert image_requirements == installed, (
        "requirements-dev.txt and pyproject.toml no longer describe the same "
        "direct environment: "
        f"image_requirements={image_requirements} declared={installed}"
    )


def test_the_audit_runs_in_the_gate_and_fails_closed():
    gate = (ROOT / ".githooks" / "check-all.sh").read_text()
    assert 'frozen_python="$root/.venv/bin/python"' in gate
    assert '"$frozen_python" -m pytest' in gate
    assert '"$frozen_python" .githooks/frozen_audit_requirements.py' in gate
    assert '"$frozen_python" -m pip_audit --strict --no-deps --disable-pip' in gate
    assert '--requirement "$audit_inventory"' in gate
    assert "import os, sys; print(os.path.realpath(sys.prefix))" in gate
    assert "uv sync --frozen --offline --group test --group audit --no-config" in gate
    # Not swallowed: `set -eu` is in force, and nothing rescues a non-zero exit.
    assert "set -eu" in gate
    for rescue in ("|| true", "|| :", "continue-on-error", "set +e"):
        assert rescue not in gate, f"the audit's failure is swallowed by {rescue!r}"


def test_the_frozen_audit_inventory_is_the_running_interpreters_exact_third_party_set():
    """The helper projects installed versions and excludes only this local project."""

    result = subprocess.run(
        [sys.executable, str(FROZEN_AUDIT_REQUIREMENTS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    audited = _pinned(result.stdout.splitlines())

    from importlib.metadata import distributions

    installed = {
        re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(): distribution.version
        for distribution in distributions()
        if re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower() != "verbatus"
    }
    assert audited == installed


def test_a_missing_frozen_interpreter_points_chambers_at_the_image_launcher_fix():
    gate = (ROOT / ".githooks" / "check-all.sh").read_text()

    assert "install pinned uv==0.12.1 in operations/autoclave/Dockerfile" in gate
    assert "in cmd_new, after checkout" in gate
    assert "operations/autoclave/autoclave.sh to operations/autoclave/fingerprint.py" in gate
    assert "do not link .venv to /opt/venv" in gate


def gate_repo(tmp_path):
    """Stop after the early environment checks instead of entering the real suite."""

    repo = new_repo(tmp_path / "gate")
    (repo / ".githooks").mkdir()
    shutil.copy(ROOT / ".githooks" / "check-all.sh", repo / ".githooks" / "check-all.sh")
    return repo


def run_gate(repo, *, env=None):
    return subprocess.run(
        ["sh", ".githooks/check-all.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_gate_refuses_to_run_without_the_frozen_interpreter(tmp_path):
    """No `.venv` is a stop with an instruction, never a fall back to PATH."""

    result = run_gate(gate_repo(tmp_path))

    assert result.returncode == 1
    assert "frozen interpreter is missing" in result.stderr
    assert "uv sync --frozen --group test --group audit" in result.stderr


def test_the_gate_refuses_a_venv_python_that_is_really_paths_python(tmp_path):
    """A `.venv/bin/python` symlink must not impersonate the frozen environment."""

    repo = gate_repo(tmp_path)
    venv = repo / ".venv"
    (venv / "bin").mkdir(parents=True)
    shim = venv / "bin" / "python"
    shim.symlink_to(sys.executable)

    reported = subprocess.run(
        [
            str(shim),
            "-c",
            "import os, sys; print(sys.executable); print(os.path.realpath(sys.prefix))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.splitlines()
    # Some interpreters resolve the shim in sys.executable themselves, so the
    # weaker identity check is not vulnerable on those builds.
    if reported[0] != str(shim):
        pytest.skip("this interpreter resolves the shim itself; the attack does not exist here")
    assert reported[1] != os.path.realpath(venv)

    result = run_gate(repo)

    assert result.returncode == 1
    assert "does not import from the frozen environment" in result.stderr


def test_the_gate_refuses_when_uv_cannot_verify_the_venv_against_the_lock(tmp_path):
    """A correctly located but stale environment cannot reach the check tools."""

    repo = gate_repo(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
        check=True,
        timeout=60,
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "uv-calls"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --version ]; then echo 'uv 0.12.1 (fixture-platform)'; exit 0; fi\n"
        'printf \'%s|%s\\n\' "$UV_PROJECT_ENVIRONMENT" "${UV_INEXACT-unset}" > "$UV_CALLS"\n'
        'printf \'%s\\n\' "$*" >> "$UV_CALLS"\n'
        "exit 1\n"
    )
    uv.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "UV_CALLS": str(calls),
        "UV_INEXACT": "1",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "wrong-environment"),
    }

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert calls.read_text().splitlines() == [
        f"{repo / '.venv'}|unset",
        "sync --frozen --offline --group test --group audit --no-config",
    ]
    assert "could not reconcile" in result.stderr
    assert "with network access, then retry" in result.stderr
    assert "check-static.sh" not in result.stderr


def test_the_gate_does_not_import_from_an_inherited_pythonpath(tmp_path):
    """A caller cannot add packages to the environment the gate claims is frozen."""

    repo = gate_repo(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
        check=True,
        timeout=60,
    )
    injected = tmp_path / "injected"
    injected.mkdir()
    marker = tmp_path / "sitecustomize-ran"
    (injected / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['ATTACK_MARKER']).touch()\n"
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\nif [ \"${1:-}\" = --version ]; then echo 'uv 0.12.1'; exit 0; fi\nexit 0\n"
    )
    uv.chmod(0o755)
    (repo / ".githooks" / "check-static.sh").write_text("#!/bin/sh\nexit 1\n")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(injected),
        "ATTACK_MARKER": str(marker),
    }

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert not marker.exists()


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


def test_every_third_party_import_in_the_gate_suite_is_declared_for_the_image():
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

    declared = {
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
        and distribution.get(root, root).lower() not in declared
    )
    assert not undeclared, (
        f"the gate's own suite imports {undeclared}, which requirements-dev.txt does not "
        "declare, so the chamber image and everyday gate reach them only as an "
        "unpinned transitive dependency"
    )
