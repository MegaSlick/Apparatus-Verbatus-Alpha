"""Tests for the Codex seat wrapper.

Resolution tests use the explicit --dry-run interface, so they never call
Codex or spend a token. Focused execution tests put a fake ``codex`` on PATH to
exercise closed stdin and timeout wiring without network access.

The two behaviours worth naming, because both were observed before this
wrapper existed: a codex call handed an open stdin blocks forever while
looking like deep reasoning, and a call with no ceiling outlives the session
that started it.
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEAT = ROOT / "operations" / "codex" / "seat.sh"
SEATS = ROOT / "operations" / "codex" / "seats.conf"

# The efforts this project's tracked seats permit. `ultra` is present, but it is
# the only one that is not permitted on its own terms: it was excluded because
# automatic delegation is not externally evidenced, and it was readmitted on the
# condition that such a seat writes into a working copy that can be diffed
# against a frozen snapshot. Membership in this set is therefore necessary and
# not sufficient — test_an_ultra_seat_must_write_into_a_working_copy_it_can_be_
# diffed_against carries the rest, and seats.conf's header states the rule.
SEAT_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
SANDBOXES = {"read-only", "workspace-write"}

# The wrapper prepends the seat's deadline to every prompt. The first line is
# fixed so the tests can find the boundary between the options and the single
# prompt argument, which is otherwise several printed lines long.
BUDGET_FIRST_LINE = "Your time budget for this run."


SEAT_ENV_NAMES = {
    "CODEX_SEATS_FILE",
    "CODEX_SEAT_TIMEOUT",
    "CODEX_SEAT_DRYRUN",
    "VERBATUS_CODEX_SEATS_FILE",
    "VERBATUS_CODEX_TIMEOUT",
    "VERBATUS_CODEX_DRYRUN",
}


def clean_env():
    env = dict(os.environ)
    for name in SEAT_ENV_NAMES:
        env.pop(name, None)
    return env


def run(*args, seats=None, stdin=None, dryrun=True, timeout=30, env=None):
    command = ["sh", str(SEAT)]
    if dryrun:
        command.append("--dry-run")
    if seats is not None:
        command.extend(["--seats", str(seats)])
    command.extend(args)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env or clean_env(),
        input=stdin,
        timeout=timeout,
    )


def parse_seats(path=SEATS):
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        assert len(fields) == 6, f"seat line is not six fields: {line!r}"
        name, model, effort, sandbox, workroot, timeout_s = fields
        assert name not in out, f"seat {name!r} is declared twice"
        out[name] = (model, effort, sandbox, workroot, timeout_s)
    return out


def write_seats(tmp_path, body):
    p = tmp_path / "seats.conf"
    p.write_text(body)
    return p


# --- the seat file itself is a fact worth asserting -------------------------


def test_seats_file_parses_and_is_not_empty():
    seats = parse_seats()
    assert seats, "seats.conf declares no seats"


def test_every_declared_effort_is_allowed_by_the_seat_policy():
    for name, (_model, effort, _sandbox, _root, _timeout) in parse_seats().items():
        assert effort in SEAT_EFFORTS, f"seat {name} runs at disallowed effort {effort!r}"


def test_an_ultra_seat_must_write_into_a_working_copy_it_can_be_diffed_against():
    # Ultra was excluded because it couples maximum reasoning to automatic
    # delegation, and a model can narrate delegation it did not perform. The
    # exclusion carried its own release condition — "until the mechanism leaves
    # external evidence" — and Tyrel admitted it on that condition: an ultra seat
    # writes into a working copy taken from a frozen snapshot, so what it did is
    # read from the diff rather than from anything it says about itself.
    #
    # This test is that condition. An ultra seat that writes nowhere diffable, or
    # reads the live tree, is the case the original exclusion was written for.
    for name, (_m, effort, sandbox, workroot, _timeout) in parse_seats().items():
        if effort == "ultra":
            assert sandbox == "workspace-write", (
                f"seat {name} spends ultra read-only; the extra reasoning buys "
                "nothing that a cheaper effort does not, and leaves no artefact"
            )
            assert workroot == "WORKCOPY", (
                f"seat {name} runs ultra without a durable working copy "
                f"({workroot!r}); a temporary tray is gone before it can be "
                "diffed, so the narration would be the only record"
            )


def test_the_seats_file_header_does_not_contradict_its_own_seat_lines():
    """A header that denies what the lines below it declare is a stale claim.

    The comment block is the only thing a reader consults before adding a seat.
    When it says `ultra` is unused and line 59 runs a seat at `ultra`, the reader
    is told something that stopped being true — so the header must carry the
    condition the effort was admitted under, not a flat denial.
    """
    comments = "\n".join(
        line for line in SEATS.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("#")
    )
    efforts = {effort for (_m, effort, _s, _r, _t) in parse_seats().values()}
    if "ultra" not in efforts:
        return
    for stale in ("no tracked seat uses", "deliberately absent", "is not allowed"):
        assert stale not in comments, (
            f"a seat runs at ultra, but the header still says {stale!r}"
        )
    assert "WORKCOPY" in comments, "the header must name the condition ultra was admitted under"
    assert "diff" in comments, "the header must say the seat is read from a diff, not its report"


def test_every_declared_sandbox_is_known_and_never_full_access():
    for name, (_m, _e, sandbox, _r, _timeout) in parse_seats().items():
        assert sandbox in SANDBOXES, f"seat {name} has sandbox {sandbox!r}"


def test_no_writing_seat_runs_inside_the_repository():
    """Keep the conservative boundary while the sandbox evidence conflicts."""
    # Two homes are allowed for a writing seat, and both are outside the tree:
    # TMPTRAY, a temporary directory that vanishes with the run, and WORKCOPY, a
    # durable working copy whose absolute path is declared in a gitignored file
    # rather than tracked here. Anything else is a path inside the repository,
    # where a workspace-write sandbox could reach the whole live tree.
    for name, (_m, _e, sandbox, workroot, _timeout) in parse_seats().items():
        if sandbox == "workspace-write":
            assert workroot in ("TMPTRAY", "WORKCOPY"), (
                f"seat {name} writes from inside the repository ({workroot!r}); "
                "it could write the whole tree"
            )


def test_no_seat_line_carries_a_machine_specific_path():
    # A tracked absolute path is correct on exactly one laptop and wrong in every
    # clone, sandbox and pod. WORKCOPY exists so the tracked line names the
    # concept and the machine says where.
    for name, (_m, _e, _s, workroot, _timeout) in parse_seats().items():
        assert not workroot.startswith("/"), (
            f"seat {name} hard-codes the absolute path {workroot!r}"
        )
        assert ".." not in workroot, f"seat {name} escapes upward via {workroot!r}"


def test_the_drafting_seat_still_exists_and_writes_outside():
    seats = parse_seats()
    assert "build" in seats, "the drafting seat is gone — was it renamed?"
    _model, _effort, sandbox, workroot, _timeout = seats["build"]
    assert sandbox == "workspace-write"
    assert workroot == "TMPTRAY"


def test_tmptray_dry_run_resolves_outside_without_creating_a_directory():
    r = run("build", "x")
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    workdir = Path(argv[argv.index("-C") + 1])
    assert not workdir.exists(), "a dry run must not create its disposable tray"
    assert ROOT not in workdir.resolve().parents and workdir.resolve() != ROOT, (
        f"the tray {workdir} is inside the repository"
    )

    second = run("build", "x")
    other = second.stdout.splitlines()
    other_dir = other[other.index("-C") + 1]
    assert str(workdir) == other_dir, "dry-run output should be stable and side-effect free"


def test_live_tmptray_is_created_fresh_outside_the_repository(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trays = tmp_path / "trays"
    trays.mkdir()
    _fake_executable(fake_bin, "codex", "exit 0\n")
    _fake_executable(fake_bin, "timeout", 'shift 2\nexec "$@"\n')
    env = clean_env()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["TMPDIR"] = str(trays)

    first = run("build", "x", dryrun=False, env=env)
    second = run("build", "x", dryrun=False, env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    announced = []
    for result in (first, second):
        lines = [
            line.removeprefix("seat: workdir ")
            for line in result.stderr.splitlines()
            if line.startswith("seat: workdir ")
        ]
        assert len(lines) == 1
        workdir = Path(lines[0])
        assert workdir.is_dir(), "a live drafting call did not create its announced tray"
        assert workdir.parent == trays.resolve()
        assert ROOT != workdir and ROOT not in workdir.parents
        announced.append(workdir)
    assert announced[0] != announced[1], "two live calls reused one disposable drafting tray"


def test_direct_prompt_dry_run_does_not_require_gnu_timeout(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("awk", "dirname", "grep", "sed", "sh"):
        executable = shutil.which(name)
        assert executable is not None
        (fake_bin / name).symlink_to(executable)

    env = clean_env()
    env["PATH"] = str(fake_bin)
    result = run("judge", "x", env=env)

    assert result.returncode == 0, result.stderr
    assert "codex" in result.stdout


def test_repository_root_is_physical_when_wrapper_is_reached_through_a_symlink(tmp_path):
    checkout = tmp_path / "checkout"
    codex_dir = checkout / "operations" / "codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "seat.sh").write_bytes(SEAT.read_bytes())
    (codex_dir / "seats.conf").write_text(
        "reader gpt-5.6-sol high read-only . 60\n",
        encoding="utf-8",
    )
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(checkout, target_is_directory=True)

    result = subprocess.run(
        [
            "sh",
            str(alias / "operations" / "codex" / "seat.sh"),
            "--dry-run",
            "reader",
            "x",
        ],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert Path(argv[argv.index("-C") + 1]) == checkout.resolve()
    assert f"seat: workdir {checkout.resolve()}" in result.stderr


# --- resolution -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(parse_seats()))
def test_each_seat_resolves_to_its_declared_line(name):
    model, effort, sandbox, workroot, timeout_s = parse_seats()[name]
    r = run(name, "prompt text")

    # A WORKCOPY seat points at a working copy outside the repository, and that
    # path is declared in a gitignored file so a machine-specific path is never
    # committed. The consequence is that such a seat resolves only where the
    # operator has set one up: on a CI runner, a fresh clone or a pod, it cannot.
    #
    # That is the design working, not a failure — so assert the refusal instead.
    # It must be a clean, explanatory exit rather than a crash or a fallback to
    # somewhere plausible, because a writing seat that guesses its own root is
    # how the live tree gets edited. This test failed in CI exactly once, for
    # exactly this reason, which is the argument for keeping the assertion.
    if workroot == "WORKCOPY" and not (ROOT / "private" / "workcopy.conf").exists():
        assert r.returncode == 2, r.stderr
        assert "WORKCOPY" in r.stderr
        assert "workcopy.conf" in r.stderr
        assert "WORKCOPY_PATH=" in r.stderr, "the refusal must say how to fix it"
        return

    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()

    assert argv[0] == "codex" and argv[1] == "exec"
    assert argv.count("--ephemeral") == 1, "ephemeral mode must be declared exactly once"
    assert argv.index("--ephemeral") < argv.index("--"), (
        "ephemeral mode appears after the option terminator and would be prompt data"
    )
    assert "--ignore-user-config" in argv, "the call would inherit ~/.codex/config.toml"
    assert "--strict-config" in argv, "a stale config key could be ignored silently"
    assert argv[argv.index("-m") + 1] == model
    assert argv[argv.index("-c") + 1] == f"model_reasoning_effort={effort}"
    assert argv[argv.index("-s") + 1] == sandbox
    # The prompt is one argument that now opens with the injected time budget, so
    # it spans several printed lines. What must hold is unchanged: `--` is the
    # last option, and everything after it is that single prompt argument.
    assert argv[argv.index("--") + 1] == BUDGET_FIRST_LINE, (
        "the prompt is not protected from Codex option/subcommand parsing"
    )
    assert argv[-1] == "prompt text", "the caller's prompt is not the final argument"
    assert f"killed without warning after {timeout_s} seconds" in r.stdout, (
        "the seat was not told the deadline it will actually be killed at"
    )
    assert f"timeout {timeout_s}s" in r.stderr

    resolved = Path(argv[argv.index("-C") + 1]).resolve()
    if workroot in ("TMPTRAY", "WORKCOPY"):
        # Both resolve outside the tree. That is the property worth asserting:
        # where exactly is machine-local and deliberately not tracked here.
        assert resolved != ROOT and ROOT not in resolved.parents
    else:
        assert resolved == (ROOT / workroot).resolve()


def test_the_prompt_is_passed_as_an_argument_not_left_on_stdin():
    # The failure this prevents: codex prints "Reading additional input from
    # stdin..." and waits forever, spending nothing and looking like thought.
    r = run("judge", "-", stdin="from stdin\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "from stdin"


@pytest.mark.parametrize("prompt", ["--help", "resume"])
def test_prompt_cannot_be_parsed_as_a_codex_option_or_subcommand(prompt):
    r = run("judge", prompt)
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    assert argv[argv.index("--") + 1] == BUDGET_FIRST_LINE
    assert argv[-1] == prompt


def test_empty_stdin_prompt_is_refused():
    r = run("judge", "-", stdin="")
    assert r.returncode == 2
    assert "empty prompt" in r.stderr


def test_the_resolved_seat_is_announced_on_stderr():
    # A seat nobody can read back from the transcript is a seat nobody can check.
    r = run("scout", "x")
    assert "seat: scout ->" in r.stderr
    assert "gpt-5.3-codex-spark" in r.stderr
    assert "timeout" in r.stderr


def test_the_resolved_workdir_is_announced():
    """A tray nobody can name is a draft nobody can carry in.

    TMPTRAY resolves to a freshly created directory with a random name. The
    seat announcement says only "root TMPTRAY", so without the resolved path on
    stderr a writing seat's output is findable only by globbing $TMPDIR and
    guessing which tray belonged to which call.
    """
    r = run("build", "x")
    assert r.returncode == 0, r.stderr
    announced = [
        line.split("seat: workdir ", 1)[1]
        for line in r.stderr.splitlines()
        if line.startswith("seat: workdir ")
    ]
    assert announced, "the resolved workdir was not announced"
    workdir = Path(announced[0])
    assert not workdir.exists(), "a dry run created the announced workdir"

    argv = r.stdout.splitlines()
    given = argv[argv.index("-C") + 1]
    assert str(workdir) == given, "the announced workdir is not the one codex is given"
    assert "//" not in given, (
        f"the tray path carries a doubled separator ({given!r}); $TMPDIR ends in "
        "a slash on macOS and every path comparison downstream has to normalise "
        "around it"
    )


def test_timeout_is_reported_in_the_announcement(tmp_path):
    seats = write_seats(
        tmp_path,
        "a gpt-5.6-sol high read-only . 42\n",
    )
    env_r = run("a", "x", seats=seats)
    assert "timeout 42s" in env_r.stderr


def test_the_seat_is_told_the_deadline_it_will_be_killed_at(tmp_path):
    # A seat blind to its deadline cannot triage against it, and because the
    # report is written last, the kill deletes the answer rather than shortening
    # it. The wrapper holds the number, so the wrapper states it: a prompt author
    # cannot forget, and the stated deadline cannot drift from the real ceiling.
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only . 42\n")
    r = run("a", "the actual task", seats=seats)
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()

    assert argv[argv.index("--") + 1] == BUDGET_FIRST_LINE
    assert "killed without warning after 42 seconds" in r.stdout
    # The target sits below the hard ceiling, leaving margin to write the report.
    assert "reported by 37 seconds" in r.stdout
    assert "stop working and write the report anyway" in r.stdout
    # The caller's own prompt survives intact, and comes last.
    assert argv[-1] == "the actual task"


def test_a_long_deadline_is_also_stated_in_minutes(tmp_path):
    # Seconds alone stop being readable at the length these seats actually run.
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only . 3600\n")
    r = run("a", "x", seats=seats)
    assert "after 3600 seconds (about 60 minutes)" in r.stdout
    assert "by 3240 seconds (about 54 minutes)" in r.stdout


@pytest.mark.parametrize("value", ["", "0", "-1", "ten", "1.5"])
def test_timeout_must_be_a_positive_whole_number(tmp_path, value):
    seats = write_seats(tmp_path, f"a gpt-5.6-sol high read-only . {value}\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "timeout" in r.stderr


# --- refusals ---------------------------------------------------------------


def test_no_arguments_is_refused_with_the_roster():
    r = run()
    assert r.returncode == 2
    assert "usage" in r.stderr
    for name in parse_seats():
        assert name in r.stderr, "the usage message should name the seats"


def test_unknown_seat_is_refused():
    r = run("nosuch", "x")
    assert r.returncode == 2
    assert "nosuch" in r.stderr


def test_a_seat_cannot_be_run_without_naming_one():
    r = run("prompt only")
    assert r.returncode == 2


def test_duplicate_seat_name_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a m1 low read-only . 60\na m2 high read-only . 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "declared 2 times" in r.stderr


def test_effort_outside_the_api_enum_is_refused(tmp_path):
    # `ultra` used to be the example here; it is now an allowed effort, so this
    # needs a value that is genuinely not in the enum. The point of the test is
    # unchanged: the CLI does not check an effort against the model receiving
    # it, so a typo would be accepted silently and spend at the wrong depth.
    seats = write_seats(tmp_path, "a gpt-5.6-sol maximum read-only . 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "maximum" in r.stderr


def test_a_writing_seat_is_refused_when_its_working_copy_is_not_declared(tmp_path):
    # WORKCOPY names a concept; the machine says where. If nothing says where,
    # the seat must refuse rather than fall back to somewhere plausible — a
    # writing seat that guesses its own root is how the live tree gets edited.
    seats = write_seats(tmp_path, "a gpt-5.6-sol high workspace-write WORKCOPY 60\n")
    env = clean_env()
    env["HOME"] = str(tmp_path)
    r = run("a", "x", seats=seats, env=env)
    if r.returncode == 0:
        # The real private/workcopy.conf exists on this machine, so the seat
        # resolves. Assert the property that matters instead: it landed outside.
        argv = r.stdout.splitlines()
        resolved = Path(argv[argv.index("-C") + 1]).resolve()
        assert ROOT not in resolved.parents and resolved != ROOT
    else:
        assert r.returncode == 2
        assert "WORKCOPY" in r.stderr or "workcopy" in r.stderr


def test_danger_full_access_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high danger-full-access . 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "danger-full-access" in r.stderr


def test_unknown_sandbox_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high wide-open . 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2


@pytest.mark.parametrize("workroot", [".", "autoclave", "operations", "operations/codex"])
def test_workspace_write_anywhere_inside_the_repository_is_refused(tmp_path, workroot):
    # `autoclave` is the important case: it is where drafts belong, and it was
    # this wrapper's first (wrong) answer to confinement.
    seats = write_seats(tmp_path, f"a gpt-5.6-terra medium workspace-write {workroot} 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "may not run inside the repository" in r.stderr


def test_incomplete_seat_line_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2


def test_extra_seat_fields_are_refused_at_runtime(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only . 60 extra\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "exactly six fields" in r.stderr


@pytest.mark.parametrize("workroot", ["../outside", "operations/../../outside"])
def test_non_tmp_workroot_cannot_escape_the_repository(tmp_path, workroot):
    seats = write_seats(tmp_path, f"a gpt-5.6-sol high read-only {workroot} 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "inside the repository" in r.stderr


def test_missing_workroot_directory_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only no/such/dir 60\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "does not exist" in r.stderr


def test_missing_seat_file_is_refused(tmp_path):
    r = run("judge", "x", seats=tmp_path / "absent.conf")
    assert r.returncode == 2


def test_alternate_seat_file_cannot_execute(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only . 1\n")
    result = subprocess.run(
        ["sh", str(SEAT), "--seats", str(seats), "a", "x"],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=30,
    )
    assert result.returncode == 2
    assert "validation-only" in result.stderr


def test_ambient_seat_variables_from_another_clone_are_ignored(tmp_path):
    stale = write_seats(tmp_path, "wrong stale-model max danger-full-access .. 9999\n")
    env = clean_env()
    env.update(
        {
            "CODEX_SEATS_FILE": str(stale),
            "CODEX_SEAT_TIMEOUT": "2700",
            "CODEX_SEAT_DRYRUN": "1",
        }
    )
    result = run("judge", "x", env=env)
    assert result.returncode == 0, result.stderr
    assert "gpt-5.6-sol" in result.stdout
    assert "stale-model" not in result.stdout
    assert "timeout 600s" in result.stderr


def _fake_executable(directory, name, body):
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_live_wrapper_gives_codex_closed_stdin_without_network(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_executable(
        fake_bin,
        "codex",
        "if IFS= read -r line; then echo 'stdin was open' >&2; exit 9; fi\nexit 0\n",
    )
    env = clean_env()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = run("judge", "x", dryrun=False, env=env)
    assert result.returncode == 0, result.stderr
    assert "stdin was open" not in result.stderr


def test_live_wrapper_passes_tracked_ceiling_and_hard_kill_to_timeout(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "timeout-args"
    _fake_executable(fake_bin, "codex", "exit 0\n")
    _fake_executable(
        fake_bin,
        "timeout",
        f"printf '%s\\n' \"$@\" > '{marker}'\nshift 2\nexec \"$@\"\n",
    )
    env = clean_env()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = run("judge", "x", dryrun=False, env=env)
    assert result.returncode == 0, result.stderr
    args = marker.read_text(encoding="utf-8").splitlines()
    assert args[:2] == ["--kill-after=10", "600"]
    assert args[2:4] == ["codex", "exec"]


def _continued_command(script, prefix):
    lines = script.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            parts = [line]
            while parts[-1].rstrip().endswith("\\"):
                index += 1
                parts.append(lines[index])
            return " ".join(part.rstrip().removesuffix("\\") for part in parts)
    raise AssertionError(f"check-all.sh has no {prefix!r} command")


def test_every_shell_entrypoint_is_in_both_check_all_commands():
    """A new script must not escape the static checks the way this one did.

    `check-all.sh` lists its scripts by hand so that each is checked
    deliberately. The cost of a hand-written list is that additions are
    forgotten: seat.sh spent money through an external API for a whole session
    before anything linted it. This test is the guard against a repeat.
    """
    check_all = (ROOT / ".githooks" / "check-all.sh").read_text()
    searched = [ROOT / ".githooks", ROOT / "operations"]
    scripts = []
    for directory in searched:
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            first = path.read_bytes().splitlines()[:1]
            shell_shebang = first and re.search(
                rb"^#!.*(?:^|[/ ])(?:ba|da|k|z)?sh(?:[ \r\n]|$)", first[0]
            )
            if path.suffix == ".sh" or shell_shebang:
                scripts.append(path.relative_to(ROOT).as_posix())
    scripts.sort()
    assert scripts, "found no shell scripts at all — the search paths are wrong"
    shellcheck_command = shlex.split(_continued_command(check_all, "shellcheck "))[1:]
    syntax_command = shlex.split(_continued_command(check_all, "sh -n "))[2:]
    missing_shellcheck = [s for s in scripts if s not in shellcheck_command]
    missing_syntax = [s for s in scripts if s not in syntax_command]
    assert not missing_shellcheck, f"shell scripts not checked by shellcheck: {missing_shellcheck}"
    assert not missing_syntax, f"shell scripts not checked by sh -n: {missing_syntax}"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    seats = write_seats(
        tmp_path,
        "# a comment\n\n   # indented comment\nsolo gpt-5.6-luna low read-only . 60\n",
    )
    r = run("solo", "x", seats=seats)
    assert r.returncode == 0, r.stderr
    assert "gpt-5.6-luna" in r.stdout
