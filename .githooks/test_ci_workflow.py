"""Executable tests for the CI workflow and the full gate, check-all.sh."""

import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHECK_ALL = ROOT / ".githooks" / "check-all.sh"
BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]

# Read, not repeated, so the fabricated `uv --version` below follows a version bump.
REQUIRED_UV_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"][
    "required-version"
].removeprefix("==")


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


def workflow():
    return yaml.safe_load(workflow_text())


def all_steps():
    return [step for job in workflow()["jobs"].values() for step in job.get("steps", [])]


def test_every_action_is_pinned_to_a_commit_and_no_checkout_keeps_credentials():
    actions = [step["uses"] for step in all_steps() if "uses" in step]
    assert actions
    assert all(re.search(r"@[0-9a-f]{40}$", action) for action in actions), actions
    checkouts = [
        step for step in all_steps() if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts
    for checkout in checkouts:
        assert checkout["with"]["persist-credentials"] is False
        # The ingress scan needs the full history to find a change's base commit.
        assert checkout["with"]["fetch-depth"] == 0


def test_ci_runs_the_gate_in_ci_mode_after_its_own_history_scan():
    """`--ci` skips the gate's full local history scan; the workflow's own scan covers it."""
    names = [step.get("name") for step in workflow()["jobs"]["test"]["steps"]]
    assert names.index("Repository ingress") < names.index("Repository checks")
    assert step_run("Repository checks") == "sh .githooks/check-all.sh --ci --parallel\n"


def test_the_required_check_job_fails_unless_every_test_leg_succeeded():
    """Branch protection requires `check`; it must gate on the whole matrix, even when a
    leg is skipped or cancelled."""
    check = workflow()["jobs"]["check"]
    assert check["needs"] in ("test", ["test"])
    assert check["if"] == "${{ always() }}"
    run = check["steps"][-1]["run"]
    for result, status in (("success", 0), ("failure", 1), ("cancelled", 1), ("skipped", 1)):
        script = run.replace("${{ needs.test.result }}", result)
        assert run_shell(script, ROOT).returncode == status, result


@pytest.fixture
def install_stubs(tmp_path):
    """A `python` that records `-m` calls and runs everything else, and a recording `uv`."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "calls"
    (stubs / "python").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = -m ]; then echo "python $*" >> {log}; exit 0; fi\n'
        f'exec {sys.executable} "$@"\n'
    )
    (stubs / "uv").write_text(
        f'#!/bin/sh\necho "uv $*" >> {log}\n[ "$1 ${{2:-}}" != "lock --check" ] || exit "$LOCK_STATUS"\n'
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = "==9.9.9"\n')
    return tmp_path, {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}, log


def test_ci_installs_pyprojects_uv_and_syncs_only_a_current_lock(install_stubs):
    directory, environment, log = install_stubs
    script = step_run("Install the frozen dependency environment")

    result = run_shell(script, directory, {**environment, "LOCK_STATUS": "0"})
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "python -m pip install uv==9.9.9",
        "uv lock --check",
        "uv sync --frozen --group test --group audit",
    ]

    log.unlink()
    stale = run_shell(script, directory, {**environment, "LOCK_STATUS": "1"})
    assert stale.returncode != 0
    assert "uv sync --frozen --group test --group audit" not in log.read_text()


def gate_repo(tmp_path):
    """Stop after the early environment checks instead of entering the real suite."""

    repo = new_repo(tmp_path / "gate")
    (repo / ".githooks").mkdir()
    shutil.copy(ROOT / ".githooks" / "check-all.sh", repo / ".githooks" / "check-all.sh")
    # check-all.sh reads its uv version from pyproject.toml, with the real pin.
    (repo / "pyproject.toml").write_text(
        f'[tool.uv]\nrequired-version = "=={REQUIRED_UV_VERSION}"\n'
    )
    return repo


def frozen_venv(repo):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
        check=True,
        timeout=60,
    )


def run_gate(repo, *, env=None, args=()):
    return subprocess.run(
        ["sh", ".githooks/check-all.sh", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "args",
    [(), ("--ci",), ("--parallel",), ("--ci", "--parallel"), ("--parallel", "--ci")],
)
def test_the_gate_accepts_its_two_flags_in_either_order(tmp_path, args):
    """Both flags, once each, in any order. Accepted arguments reach the checks."""

    result = run_gate(gate_repo(tmp_path), args=args)

    # Exit 1 with this message is the first real check refusing a repo with no
    # `.venv` -- that is, the arguments parsed and the gate proceeded.
    assert result.returncode == 1
    assert "frozen interpreter is missing" in result.stderr
    assert "usage:" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--parallel=4",),
        ("-n", "4"),
        ("--Parallel",),
        ("--ci", "--ci"),
        ("--parallel", "--parallel"),
        ("--ci", "--parallel", "extra"),
    ],
)
def test_the_gate_refuses_anything_but_those_two_flags(tmp_path, args):
    """A misspelled or repeated flag is a usage error, never a silently different run."""

    result = run_gate(gate_repo(tmp_path), args=args)

    assert result.returncode == 2
    assert "usage: sh .githooks/check-all.sh [--ci] [--parallel]" in result.stderr


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

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "${1:-}" = --version ]; then echo \'uv '
        f"{REQUIRED_UV_VERSION}'; exit 0; fi\nexit 0\n"
    )
    uv.chmod(0o755)
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert "does not import from the frozen environment" in result.stderr


def test_the_gate_refuses_when_uv_cannot_verify_the_venv_against_the_lock(tmp_path):
    """A correctly located but stale environment cannot reach the check tools."""

    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "uv-calls"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = --version ]; then echo '
        f"'uv {REQUIRED_UV_VERSION} (fixture-platform)'; exit 0; fi\n"
        'calls="${0%/*}/../uv-calls"\n'
        'printf \'%s|%s|%s\\n\' "$UV_PROJECT_ENVIRONMENT" "${UV_INEXACT-unset}" '
        '"${UV_NO_GROUP-unset}" > "$calls"\n'
        'printf \'%s\\n\' "$*" >> "$calls"\n'
        "exit 1\n"
    )
    uv.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "UV_INEXACT": "1",
        "UV_NO_GROUP": "audit",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "wrong-environment"),
    }

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    # `unset|unset`: the sync runs under `env -i`, so no caller UV_* variable reaches it.
    assert calls.read_text().splitlines() == [
        f"{repo / '.venv'}|unset|unset",
        "sync --frozen --offline --group test --group audit --no-config",
    ]
    assert "could not reconcile" in result.stderr
    assert "with network access, then retry" in result.stderr
    assert "check-static.sh" not in result.stderr


def test_the_gate_does_not_import_from_an_inherited_pythonpath(tmp_path):
    """A caller cannot add packages to the environment the gate claims is frozen."""

    repo = gate_repo(tmp_path)
    frozen_venv(repo)
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
        '#!/bin/sh\nif [ "${1:-}" = --version ]; then echo \'uv '
        f"{REQUIRED_UV_VERSION}'; exit 0; fi\nexit 0\n"
    )
    uv.chmod(0o755)
    # Proves the static check was reached: the gate exits 1 for earlier reasons too.
    reached = tmp_path / "static-check-ran"
    (repo / ".githooks" / "check-static.sh").write_text(f"#!/bin/sh\n: > {reached}\nexit 1\n")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(injected),
        "ATTACK_MARKER": str(marker),
    }

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert reached.exists(), "the gate stopped before the static check"
    assert not marker.exists()


def test_the_gate_refuses_a_uv_other_than_the_pinned_version(tmp_path):
    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text("#!/bin/sh\necho 'uv 0.0.1'\n")
    uv.chmod(0o755)
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert f"requires uv {REQUIRED_UV_VERSION}" in result.stderr


def full_gate_repo(tmp_path, *, audit_status=0, topic="verbatus-test-sink"):
    """A gate repo whose every check is a recorder, to run check-all.sh end to end.

    The fake `.venv` is a real virtual environment, so the sys.prefix check passes, with
    stub `pytest` and `pip_audit` packages in its site-packages.
    """
    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    log = tmp_path / "log"
    purelib = subprocess.run(
        [
            str(repo / ".venv" / "bin" / "python"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    record = f"import os, sys\nlog = open({str(log)!r}, 'a')\n"
    for package, body in (
        (
            "pytest",
            "log.write(' '.join(['pytest', *sys.argv[1:]]) + '\\n')\n"
            "log.write('topic ' + os.environ['NTFY_TOPIC'] + '\\n')\n",
        ),
        (
            "pip_audit",
            "inventory = sys.argv[sys.argv.index('--requirement') + 1]\n"
            "log.write(' '.join(['audit', *sys.argv[1:-1]]) + '\\n')\n"
            "log.write('inventory ' + open(inventory).read())\n"
            "log.write('directory ' + os.path.dirname(inventory) + '\\n')\n"
            f"raise SystemExit({audit_status})\n",
        ),
    ):
        (Path(purelib) / package).mkdir()
        (Path(purelib) / package / "__init__.py").write_text("")
        (Path(purelib) / package / "__main__.py").write_text(record + body)
    (repo / ".githooks" / "check-static.sh").write_text(f"#!/bin/sh\necho static >> {log}\n")
    (repo / ".githooks" / "check_ingress.py").write_text(
        f"import sys\nopen({str(log)!r}, 'a').write(' '.join(['ingress', *sys.argv[1:]]) + '\\n')\n"
    )
    (repo / "conftest.py").write_text(f'NOTIFY_TEST_SINK_TOPIC = "{topic}"\n')
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = --version ]; then echo "uv {REQUIRED_UV_VERSION}"; exit 0; fi\n'
        f'echo "uv $*" >> {log}\n'
        '[ "$1" != export ] || echo "example==1.0"\n'
    )
    uv.chmod(0o755)
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    return repo, environment, log


SYNC = "uv sync --frozen --offline --group test --group audit --no-config"
EXPORT = (
    "uv export --frozen --offline --no-config --no-emit-project --no-hashes "
    "--group test --group audit"
)
AUDIT = "audit --strict --no-deps --disable-pip --requirement"


def test_the_local_gate_runs_every_check_and_audits_the_locked_inventory(tmp_path):
    repo, environment, log = full_gate_repo(tmp_path)

    result = run_gate(repo, env=environment)

    assert result.returncode == 0, result.stderr
    recorded = log.read_text().splitlines()
    directory = recorded.pop()
    assert recorded == [
        SYNC,
        "static",
        "ingress --history HEAD",
        "ingress --staged",
        "ingress --worktree",
        "pytest",
        "topic verbatus-test-sink",
        EXPORT,
        AUDIT,
        "inventory example==1.0",
    ]
    assert not Path(directory.removeprefix("directory ")).exists()


def test_the_ci_gate_leaves_history_to_the_workflow_and_runs_the_suite_in_parallel(tmp_path):
    repo, environment, log = full_gate_repo(tmp_path)

    result = run_gate(repo, env=environment, args=("--ci", "--parallel"))

    assert result.returncode == 0, result.stderr
    recorded = log.read_text().splitlines()
    assert not [line for line in recorded if line.startswith("ingress")]
    assert "pytest -p xdist -n 4 --dist loadfile" in recorded


def test_a_failed_or_unrunnable_audit_fails_the_gate(tmp_path):
    repo, environment, log = full_gate_repo(tmp_path, audit_status=1)

    result = run_gate(repo, env=environment)

    assert result.returncode != 0
    assert AUDIT in log.read_text()


def test_the_gate_refuses_to_run_the_suites_without_the_test_sink_topic(tmp_path):
    repo, environment, log = full_gate_repo(tmp_path, topic="")

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert "NOTIFY_TEST_SINK_TOPIC" in result.stderr
    assert "pytest" not in log.read_text()


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


def test_ingress_step_scans_only_new_commits_when_the_start_commit_is_known(recorded_ingress):
    git = ["git", "-C", str(recorded_ingress)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    base = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    result = run_shell(
        step_run("Repository ingress"),
        recorded_ingress,
        {"GITHUB_REF": "refs/pull/1/merge", "GITHUB_HEAD_REF": "work/topic", "SCAN_BASE": base},
    )
    assert result.returncode == 0, result.stderr
    assert calls(recorded_ingress) == ["--ref-fields", f"--history {base}..HEAD"]


def test_ingress_step_scans_everything_when_the_start_commit_is_unknown(recorded_ingress):
    for base in ("", "0" * 40, "f" * 40):
        (recorded_ingress / "calls").unlink(missing_ok=True)
        result = run_shell(
            step_run("Repository ingress"),
            recorded_ingress,
            {"GITHUB_REF": "refs/heads/main", "GITHUB_HEAD_REF": "", "SCAN_BASE": base},
        )
        assert result.returncode == 0, result.stderr
        assert calls(recorded_ingress) == ["--ref-fields", "--history HEAD"]
