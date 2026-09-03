"""Executable wiring tests for the small GitHub Actions workflow."""

import importlib.util
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

_audit_spec = importlib.util.spec_from_file_location(
    "verbatus_frozen_audit_requirements", FROZEN_AUDIT_REQUIREMENTS
)
assert _audit_spec is not None and _audit_spec.loader is not None
frozen_audit = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(frozen_audit)


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
        return _normalized(re.split(r"[=<>!~\[]", requirement.strip(), maxsplit=1)[0])

    missing = sorted(
        {name(item) for item in declared} - {name(item) for item in requirements if item}
    )
    assert not missing, (
        f"runtime dependencies {missing} are not in requirements-dev.txt, so "
        "the chamber image and everyday gate do not install them"
    )


def _normalized(raw: str) -> str:
    """PEP 503 name folding, the same rule `_pinned` applies.

    The two files spell several distributions differently -- `huggingface_hub`
    against `huggingface-hub` -- so comparing lower-cased text alone reported a
    present dependency as absent and turned the build red over a spelling.
    """

    return re.sub(r"[-_.]+", "-", raw.strip()).lower()


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
        pins[_normalized(distribution)] = version.strip()
    return pins


# The chamber image and the everyday non-frozen gate install only these two
# groups (`.githooks/check-all.sh` and the Dockerfile both name them). `pod` is
# installed only by `operations/pod/bootstrap.py`, on the pod itself, never on
# a laptop or in the chamber image -- putting a ~10 GB CUDA stack into the
# image it is not is not a gap requirements-dev.txt should close. A future
# laptop-side group has to earn its way into IMAGE_GROUPS explicitly; the
# marker assertion below is the tripwire that catches one that tries to ride
# along silently instead.
IMAGE_GROUPS = ("test", "audit")


def test_the_image_requirements_match_the_projects_declared_direct_environment():
    """Both independently consumed declarations must pin the same direct environment."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependency_groups = project["dependency-groups"]
    excluded_entries = [
        entry
        for name, group in dependency_groups.items()
        if name not in IMAGE_GROUPS
        for entry in group
    ]
    # Two different problems, reported separately. A PEP 735
    # `{include-group = "..."}` table here is a shape this test does not
    # understand, not an entry missing a marker -- the comment further down
    # explains that those tables are a real thing to expect -- and folding both
    # into one assertion made a future group that used one fail with a message
    # naming the wrong problem.
    excluded_tables = [entry for entry in excluded_entries if not isinstance(entry, str)]
    assert all(
        isinstance(entry, dict) and set(entry) == {"include-group"} for entry in excluded_tables
    ), (
        f"unrecognised non-string dependency-group entries outside {IMAGE_GROUPS}: "
        f"{excluded_tables}; this test compares environment markers and does not know "
        "what these declare"
    )
    unmarked = [entry for entry in excluded_entries if isinstance(entry, str) and ";" not in entry]
    assert not unmarked, (
        f"dependency-group entries outside {IMAGE_GROUPS} without an environment "
        f"marker: {unmarked}; an unmarked entry would install everywhere "
        "and this test would silently stop comparing it"
    )
    group_entries = [
        entry
        for name, group in dependency_groups.items()
        if name in IMAGE_GROUPS
        for entry in group
    ]
    # PEP 735 lets a group hold `{include-group = "..."}` tables. `_pinned` would
    # be handed the table and fail on `entry.strip()` with an AttributeError,
    # reporting a type error where this test is meant to report environment
    # drift. Those tables name no distribution, so they are not pins to compare.
    included = [entry for entry in group_entries if not isinstance(entry, str)]
    assert all(set(entry) == {"include-group"} for entry in included), (
        f"unrecognised non-string dependency-group entries {included}; this test "
        "compares pins and does not know what these declare"
    )
    installed = _pinned(
        [
            *project["project"]["dependencies"],
            *(entry for entry in group_entries if isinstance(entry, str)),
        ]
    )
    image_requirements = _pinned((ROOT / "requirements-dev.txt").read_text().splitlines())

    assert image_requirements == installed, (
        "requirements-dev.txt and pyproject.toml no longer describe the same "
        "direct environment: "
        f"image_requirements={image_requirements} declared={installed}"
    )


def test_the_audit_is_invoked_from_the_frozen_interpreter_and_nothing_rescues_a_failure():
    """What this proves, and what it does not.

    It reads the script as text, so it establishes that the audit is written
    with the frozen interpreter and its own inventory, and -- through the sweep
    at the end, which is a real property over the whole file -- that `set -eu` is
    in force and no rescue construct exists anywhere to swallow a non-zero exit.

    It does not execute the audit. The executable gate tests below stop before
    the suite on purpose, and the audit runs after `pytest`, so reaching it here
    would mean running the whole suite inside one of its own tests. The previous
    name claimed "fails closed" as tested behaviour; this one claims only what
    the assertions below actually check.
    """

    gate = (ROOT / ".githooks" / "check-all.sh").read_text()
    assert 'frozen_python="$root/.venv/bin/python"' in gate
    assert '"$frozen_python" -m pytest' in gate
    assert '"$frozen_python" .githooks/frozen_audit_requirements.py' in gate
    assert '"$frozen_python" -m pip_audit --strict --no-deps --disable-pip' in gate
    assert '--requirement "$audit_inventory"' in gate
    assert "import os, sys; print(os.path.realpath(sys.prefix))" in gate
    assert '"$uv_binary" sync --frozen --offline --group test --group audit --no-config' in gate
    assert '/usr/bin/env -i HOME="$uv_home" PATH=/usr/bin:/bin' in gate
    assert 'mktemp -d "/tmp/verbatus-frozen-audit.XXXXXX"' in gate
    assert 'PATH="$root/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"' in gate
    assert '"$frozen_python" .githooks/check_ingress.py --history HEAD' in gate
    assert "root=$(/usr/bin/git rev-parse --show-toplevel" in gate
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

    # Read from the helper rather than repeating the literal: a rename that
    # updates only one copy would otherwise fail the full gate with pip-audit's
    # "unresolvable requirement" instead of anything about the rename.
    excluded = frozen_audit.PROJECT_DISTRIBUTION
    import tomllib

    declared_name = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["name"]
    assert _normalized(declared_name) == excluded, (
        f"pyproject declares {declared_name!r} but the audit helper excludes {excluded!r}; "
        "the local project would be sent to pip-audit as a third-party pin"
    )
    installed = {
        _normalized(distribution.metadata["Name"]): distribution.version
        for distribution in distributions()
        if _normalized(distribution.metadata["Name"]) != excluded
    }
    assert audited == installed


@pytest.mark.parametrize(
    ("name", "version"),
    (("safe\nother", "1.0"), ("safe", "1.0\nother==2"), ("safe; marker", "1.0")),
)
def test_the_frozen_audit_inventory_refuses_requirement_injection(
    monkeypatch: pytest.MonkeyPatch, name: str, version: str
) -> None:
    class Distribution:
        metadata = {"Name": name}

        def __init__(self) -> None:
            self.version = version

    monkeypatch.setattr(frozen_audit, "distributions", lambda: [Distribution()])

    with pytest.raises(ValueError, match="unsafe"):
        frozen_audit.installed_pins()


def test_a_missing_frozen_interpreter_prints_the_image_launcher_recovery_steps(tmp_path):
    """The advice a chamber is given is read back out of the gate that prints it.

    Asserting these strings against the script said only that the words exist
    somewhere in the file: advice naming the wrong Dockerfile, the wrong command
    or the wrong directory passed, and so would advice written into a branch
    nothing reaches.

    `chamber_environment_followup` returns early unless
    `/opt/autoclave/CLAUDE.md` exists, which is true only inside a chamber image
    and cannot be staged on a host. That one absolute marker path -- and nothing
    else -- is rewritten to a file in `tmp_path`, so the branch runs here exactly
    as it does in the image.
    """

    repo = gate_repo(tmp_path)
    marker = tmp_path / "chamber-marker"
    marker.write_text("stand-in for the chamber image's own marker\n")
    script = repo / ".githooks" / "check-all.sh"
    source = script.read_text()
    assert source.count("/opt/autoclave/CLAUDE.md") == 1
    script.write_text(source.replace("/opt/autoclave/CLAUDE.md", str(marker)))

    result = run_gate(repo)

    assert result.returncode == 1
    assert "frozen interpreter is missing" in result.stderr
    assert "this chamber image cannot construct the required checkout-local .venv" in result.stderr
    assert "install pinned uv==0.12.1 in operations/autoclave/Dockerfile" in result.stderr
    assert "in cmd_new, after checkout" in result.stderr
    assert (
        "operations/autoclave/autoclave.sh to operations/autoclave/fingerprint.py" in result.stderr
    )
    assert "do not link .venv to /opt/venv" in result.stderr


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

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\nif [ \"${1:-}\" = --version ]; then echo 'uv 0.12.1'; exit 0; fi\nexit 0\n"
    )
    uv.chmod(0o755)
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert "does not import from the frozen environment" in result.stderr


def test_the_gate_refuses_a_repository_controlled_uv_binary(tmp_path):
    repo = gate_repo(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
        check=True,
        timeout=60,
    )
    uv = repo / "uv"
    uv.write_text("#!/bin/sh\necho 'uv 0.12.1'\n")
    uv.chmod(0o755)
    environment = {**os.environ, "PATH": f"{repo}{os.pathsep}{os.environ['PATH']}"}

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert "repository-controlled verifier" in result.stderr


def test_the_gate_refuses_an_outside_uv_symlink_to_repository_code(tmp_path):
    repo = gate_repo(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
        check=True,
        timeout=60,
    )
    owned = repo / "owned-uv"
    owned.write_text("#!/bin/sh\necho 'uv 0.12.1'\n")
    owned.chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "uv").symlink_to(owned)
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = run_gate(repo, env=environment)

    assert result.returncode == 1
    assert "repository-controlled verifier" in result.stderr


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
    # What the `unset|unset` here establishes: the sync runs under `/usr/bin/env
    # -i`, so uv sees an emptied environment plus only the variables named on
    # that line. It is *not* a check on the gate's own `unset UV_CONFIG_FILE
    # UV_INEXACT UV_PYTHON`; deleting that line leaves this passing, because
    # `env -i` already removed them. That line still matters for the later
    # `frozen_python` steps, which do not run under `env -i`, so it is asserted
    # as text below and its PYTHONPATH half is exercised by the next test.
    assert calls.read_text().splitlines() == [
        f"{repo / '.venv'}|unset|unset",
        "sync --frozen --offline --group test --group audit --no-config",
    ]
    gate = (ROOT / ".githooks" / "check-all.sh").read_text()
    assert "unset PYTHONHOME PYTHONOPTIMIZE PYTHONPATH PYTEST_ADDOPTS PYTEST_PLUGINS" in gate
    assert "unset UV_CONFIG_FILE UV_INEXACT UV_PYTHON" in gate
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
    # The stub records that it ran. Asserting only `returncode == 1` proved
    # nothing about contamination: the gate exits 1 for several earlier reasons,
    # so a change that stopped before the static check would still pass here.
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
        _normalized(re.split(r"[=<>!~\[]", line.strip(), maxsplit=1)[0])
        for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # Import name to distribution name, for the few that differ.
    distribution = {"yaml": "pyyaml"}
    undeclared = sorted(
        root
        for root in roots
        if root not in sys.stdlib_module_names
        and _normalized(distribution.get(root, root)) not in declared
    )
    assert not undeclared, (
        f"the gate's own suite imports {undeclared}, which requirements-dev.txt does not "
        "declare, so the chamber image and everyday gate reach them only as an "
        "unpinned transitive dependency"
    )
