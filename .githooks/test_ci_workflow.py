"""Executable tests for the CI workflow and the full gate, check-all.sh."""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import packaging
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
    return yaml.safe_load(WORKFLOW.read_text())


def all_steps():
    return [step for job in workflow()["jobs"].values() for step in job.get("steps", [])]


def step_run(name):
    (step,) = [step for step in all_steps() if step.get("name") == name]
    return step["run"]


WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
BRANCH_INGRESS = ROOT / ".github" / "workflows" / "ingress.yml"


def steps_of(path):
    document = yaml.safe_load(path.read_text())
    return [step for job in document["jobs"].values() for step in job.get("steps", [])]


def test_every_action_is_pinned_to_a_commit_and_no_checkout_keeps_credentials():
    every_step = [step for path in WORKFLOWS for step in steps_of(path)]
    actions = [step["uses"] for step in every_step if "uses" in step]
    assert actions
    assert all(re.search(r"@[0-9a-f]{40}$", action) for action in actions), actions
    checkouts = [
        step for step in every_step if step.get("uses", "").startswith("actions/checkout@")
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
    assert step_run("Repository checks") == "sh .githooks/check-all.sh --ci --parallel"


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
    log = tmp_path / "calls"
    environment = stub_uv(
        tmp_path,
        f'echo "uv $*" >> {log}\n[ "$1 ${{2:-}}" != "lock --check" ] || exit "$LOCK_STATUS"\n',
    )
    python = tmp_path / "fake-bin" / "python"
    python.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = -m ]; then echo "python $*" >> {log}; exit 0; fi\n'
        f'exec {sys.executable} "$@"\n'
    )
    python.chmod(0o755)
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = "==9.9.9"\n')
    return tmp_path, environment, log


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


def test_the_pinned_python_is_one_ci_runs_and_never_replaces_a_leg_s_own():
    """`.python-version` picks the interpreter uv creates `.venv` with on a Mac. CI tests
    it, and every leg that syncs names its own matrix version, or uv would build the 3.14
    leg's environment on the pinned 3.12."""
    pinned = (ROOT / ".python-version").read_text(encoding="ascii").strip()
    jobs = workflow()["jobs"]
    assert pinned in jobs["test"]["strategy"]["matrix"]["python-version"]
    syncing = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if "uv sync" in step.get("run", "")
    ]
    assert syncing
    for step in syncing:
        assert step.get("env", {}).get("UV_PYTHON") == "${{ matrix.python-version }}", step


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


def stub_uv(directory, body="exit 0\n", version=REQUIRED_UV_VERSION):
    """A `fake-bin/uv` under `directory` that reports `version` and runs `body` for
    every other call; returns an environment with it first on PATH."""
    fake_bin = directory / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    uv = fake_bin / "uv"
    uv.write_text(
        f'#!/bin/sh\nif [ "${{1:-}}" = --version ]; then echo "uv {version}"; exit 0; fi\n{body}'
    )
    uv.chmod(0o755)
    return {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}


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
def test_the_gate_accepts_its_two_flags_and_needs_the_frozen_interpreter(tmp_path, args):
    """Accepted arguments reach the checks, and no `.venv` is a stop with an
    instruction, never a fall back to PATH."""

    result = run_gate(gate_repo(tmp_path), args=args)

    assert result.returncode == 1
    assert "frozen interpreter is missing" in result.stderr
    assert "uv sync --frozen --group test --group audit" in result.stderr
    assert "usage:" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--parallel=4",),
        ("-n", "4"),
        ("--Parallel",),
        ("--ci", "--parallel", "extra"),
    ],
)
def test_the_gate_refuses_anything_but_those_two_flags(tmp_path, args):
    """A misspelled flag or extra argument is a usage error, never a silently different run."""

    result = run_gate(gate_repo(tmp_path), args=args)

    assert result.returncode == 2
    assert "usage: sh .githooks/check-all.sh [--ci] [--parallel]" in result.stderr


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

    environment = stub_uv(tmp_path)

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert "does not import from the frozen environment" in result.stderr


def test_the_gate_refuses_when_uv_cannot_verify_the_venv_against_the_lock(tmp_path):
    """A correctly located but stale environment cannot reach the check tools."""

    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    calls = tmp_path / "uv-calls"
    record = (
        'printf \'%s|%s|%s\\n\' "$UV_PROJECT_ENVIRONMENT" "${UV_INEXACT-unset}" '
        f'"${{UV_NO_GROUP-unset}}" > {calls}\n'
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        "exit 1\n"
    )
    environment = {
        **stub_uv(tmp_path, record, version=f"{REQUIRED_UV_VERSION} (fixture-platform)"),
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
    # Proves the static check was reached: the gate exits 1 for earlier reasons too.
    reached = tmp_path / "static-check-ran"
    (repo / ".githooks" / "check-static.sh").write_text(f"#!/bin/sh\n: > {reached}\nexit 1\n")
    environment = {
        **stub_uv(tmp_path),
        "PYTHONPATH": str(injected),
        "ATTACK_MARKER": str(marker),
    }

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert reached.exists(), "the gate stopped before the static check"
    assert not marker.exists()


def test_the_gate_takes_the_required_uv_version_from_pyproject(tmp_path):
    """The fixture declares a version other than the real pin, so a literal in the
    script, of the real pin or anything else, fails one of the two runs."""
    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    (repo / "pyproject.toml").write_text('[tool.uv]\nrequired-version = "==9.9.9"\n')
    assert REQUIRED_UV_VERSION != "9.9.9"

    refused = run_gate(repo, env=stub_uv(tmp_path / "pinned", "exit 1\n"))
    assert refused.returncode == 1
    assert "requires uv 9.9.9" in refused.stderr

    # The declared version passes the check and reaches the sync, which the stub fails.
    accepted = run_gate(repo, env=stub_uv(tmp_path / "declared", "exit 1\n", version="9.9.9"))
    assert accepted.returncode == 1
    assert "requires uv" not in accepted.stderr
    assert "could not reconcile" in accepted.stderr


# macOS /bin/sh is bash in POSIX mode, whose `command -v` makes a relative PATH match
# absolute; dash prints it as found. The gate must refuse the same way under both.
GATE_SHELLS = [
    pytest.param(["sh"], id="sh"),
    pytest.param(["bash", "--posix"], id="bash-posix"),
]


def run_gate_with(shell, repo, env):
    command = [shutil.which(shell[0]), *shell[1:], ".githooks/check-all.sh"]
    return subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=60)


def recording_uv(directory, marker):
    """A uv under `directory` that records every call, version checks included."""
    directory.mkdir(parents=True, exist_ok=True)
    uv = directory / "uv"
    uv.write_text(f'#!/bin/sh\necho "$*" >> {marker}\nexit 1\n')
    uv.chmod(0o755)


@pytest.mark.parametrize("shell", GATE_SHELLS)
@pytest.mark.parametrize(
    ("entry", "directory"),
    [("fake-bin", "fake-bin"), ("", "."), (".", "."), ("./fake-bin", "fake-bin")],
    ids=["relative", "empty", "dot", "dot-relative"],
)
def test_the_gate_refuses_a_uv_found_through_a_relative_path_entry(
    tmp_path, shell, entry, directory
):
    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    marker = tmp_path / "uv-ran"
    recording_uv(repo / directory, marker)
    environment = {**os.environ, "PATH": f"{entry}{os.pathsep}{os.environ['PATH']}"}

    result = run_gate_with(shell, repo, environment)

    assert result.returncode == 1, result.stderr
    assert "from the PATH entry" in result.stderr
    assert not marker.exists(), "the checkout's uv ran before the gate refused it"


@pytest.mark.parametrize("shell", GATE_SHELLS)
def test_the_gate_refuses_a_trailing_empty_path_entry_that_selects_uv(tmp_path, shell):
    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    marker = tmp_path / "uv-ran"
    recording_uv(repo, marker)
    environment = {**os.environ, "PATH": f"/usr/bin{os.pathsep}/bin{os.pathsep}"}

    result = run_gate_with(shell, repo, environment)

    assert result.returncode == 1, result.stderr
    assert "from the PATH entry" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("shell", GATE_SHELLS)
def test_the_gate_runs_a_uv_from_an_absolute_path_entry_after_a_relative_one(tmp_path, shell):
    """A relative entry that holds no uv does not matter; the selected uv does."""
    repo = gate_repo(tmp_path)
    frozen_venv(repo)
    marker = tmp_path / "uv-ran"
    recording_uv(tmp_path / "trusted", marker)
    environment = {
        **os.environ,
        "PATH": os.pathsep.join(["empty-dir", str(tmp_path / "trusted"), os.environ["PATH"]]),
    }

    result = run_gate_with(shell, repo, environment)

    assert result.returncode == 1
    assert "from the PATH entry" not in result.stderr
    assert marker.read_text().splitlines() == ["--version"]
    assert "requires uv" in result.stderr


def full_gate_repo(tmp_path, *, audit_status=0, serving_audit_status=0, topic="verbatus-test-sink"):
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
            "serving = os.path.basename(inventory).startswith('serving-')\n"
            f"raise SystemExit({serving_audit_status} if serving else {audit_status})\n",
        ),
    ):
        (Path(purelib) / package).mkdir()
        (Path(purelib) / package / "__init__.py").write_text("")
        (Path(purelib) / package / "__main__.py").write_text(record + body)
    # The serving audit evaluates the lock's markers with the real `packaging`.
    shutil.copytree(Path(packaging.__file__).parent, Path(purelib) / "packaging")
    shutil.copy(ROOT / ".githooks" / "serving_audit.py", repo / ".githooks" / "serving_audit.py")
    with (repo / "pyproject.toml").open("a") as pyproject:
        pyproject.write(SERVING_PYPROJECT)
    (repo / ".githooks" / "check-static.sh").write_text(f"#!/bin/sh\necho static >> {log}\n")
    (repo / ".githooks" / "check_ingress.py").write_text(
        f"import sys\nopen({str(log)!r}, 'a').write(' '.join(['ingress', *sys.argv[1:]]) + '\\n')\n"
    )
    (repo / "conftest.py").write_text(f'NOTIFY_TEST_SINK_TOPIC = "{topic}"\n')
    (tmp_path / "serving-export").write_text(SERVING_EXPORT_OUTPUT)
    environment = stub_uv(
        tmp_path,
        f'echo "uv $*" >> {log}\n'
        'if [ "$1" = export ]; then\n'
        '  case " $* " in\n'
        f'    *" --group pod "*) cat {tmp_path / "serving-export"} ;;\n'
        '    *) echo "example==1.0" ;;\n'
        "  esac\n"
        "fi\n",
    )
    return repo, environment, log


# A serving group as the lock exports it: Linux-only markers, one package locked at a
# version per Python, and a line for another platform that the pod never installs.
SERVING_PYPROJECT = """
[project]
requires-python = ">=3.12"

[dependency-groups]
pod = ["served==2.0; sys_platform == 'linux' and platform_machine == 'x86_64'"]
"""
SERVING_EXPORT_OUTPUT = """\
numpy==1.0 ; python_full_version < '3.13' and sys_platform == 'linux'
numpy==2.0 ; python_full_version >= '3.13' and sys_platform == 'linux'
    # via served
served==2.0 ; platform_machine == 'x86_64' and sys_platform == 'linux'
windows-only==1.0 ; sys_platform == 'win32'
"""


SYNC = "uv sync --frozen --offline --group test --group audit --no-config"
EXPORT = (
    "uv export --frozen --offline --no-config --no-emit-project --no-hashes "
    "--group test --group audit"
)
AUDIT = "audit --strict --no-deps --disable-pip --requirement"
SERVING_EXPORT = (
    "uv export --frozen --offline --no-config --no-emit-project --no-hashes --group pod"
)


def test_the_local_gate_runs_every_check_and_audits_the_locked_inventory(tmp_path):
    repo, environment, log = full_gate_repo(tmp_path)

    result = run_gate(repo, env=environment)

    assert result.returncode == 0, result.stderr
    recorded = log.read_text().splitlines()
    directories = {line for line in recorded if line.startswith("directory ")}
    assert [line for line in recorded if not line.startswith("directory ")] == [
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
        # The serving stack as the Linux x86_64 pod installs it, whatever the host.
        SERVING_EXPORT,
        AUDIT,
        "inventory numpy==1.0",
        "served==2.0",
        AUDIT,
        "inventory numpy==2.0",
    ]
    assert len(directories) == 1
    assert not Path(directories.pop().removeprefix("directory ")).exists()


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
    recorded = log.read_text().splitlines()
    assert AUDIT in recorded
    assert not Path(recorded[-1].removeprefix("directory ")).exists()


def test_a_failed_serving_audit_fails_the_gate(tmp_path):
    repo, environment, log = full_gate_repo(tmp_path, serving_audit_status=1)

    result = run_gate(repo, env=environment)

    assert result.returncode != 0
    assert SERVING_EXPORT in log.read_text().splitlines()


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
    git(recorded_ingress, "init", "-q")
    git(
        recorded_ingress,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    base = git(recorded_ingress, "rev-parse", "HEAD").stdout.strip()
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


def test_every_workflow_reads_the_repository_and_nothing_else():
    for path in WORKFLOWS:
        assert yaml.safe_load(path.read_text())["permissions"] == {"contents": "read"}, path


def test_pushes_to_other_branches_get_the_same_ingress_scan_as_ci():
    document = yaml.safe_load(BRANCH_INGRESS.read_text())
    # PyYAML reads the bare key `on` as True.
    assert document[True] == {"push": {"branches-ignore": ["main"]}}
    (scan,) = [
        step for step in steps_of(BRANCH_INGRESS) if step.get("name") == "Repository ingress"
    ]
    assert scan["run"] == step_run("Repository ingress")
    assert "concurrency" not in document, "a cancelled run would leave a push unscanned"
