"""Tests for the Codex seat wrapper.

Every test runs with CODEX_SEAT_DRYRUN=1, so the suite never calls Codex and
never spends a token. What is being tested is the resolution — that a named
seat produces exactly the command line its line in seats.conf describes, and
that the refusals refuse.

The two behaviours worth naming, because both were observed before this
wrapper existed: a codex call handed an open stdin blocks forever while
looking like deep reasoning, and a call with no ceiling outlives the session
that started it.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEAT = ROOT / "operations" / "codex" / "seat.sh"
SEATS = ROOT / "operations" / "codex" / "seats.conf"

# The efforts the API accepts. `ultra` is deliberately absent: the CLI forwards
# it, the API enum does not list it, and no seat should spend it.
API_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
SANDBOXES = {"read-only", "workspace-write"}


def run(*args, seats=None, stdin=None, dryrun=True, timeout=30):
    env = dict(os.environ)
    if dryrun:
        env["CODEX_SEAT_DRYRUN"] = "1"
    else:
        env.pop("CODEX_SEAT_DRYRUN", None)
    if seats is not None:
        env["CODEX_SEATS_FILE"] = str(seats)
    return subprocess.run(
        ["sh", str(SEAT), *args],
        capture_output=True,
        text=True,
        env=env,
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
        assert len(fields) == 5, f"seat line is not five fields: {line!r}"
        name, model, effort, sandbox, workroot = fields
        assert name not in out, f"seat {name!r} is declared twice"
        out[name] = (model, effort, sandbox, workroot)
    return out


def write_seats(tmp_path, body):
    p = tmp_path / "seats.conf"
    p.write_text(body)
    return p


# --- the seat file itself is a fact worth asserting -------------------------


def test_seats_file_parses_and_is_not_empty():
    seats = parse_seats()
    assert seats, "seats.conf declares no seats"


def test_every_declared_effort_is_one_the_api_accepts():
    for name, (_model, effort, _sandbox, _root) in parse_seats().items():
        assert effort in API_EFFORTS, f"seat {name} runs at unaccepted effort {effort!r}"


def test_no_seat_spends_ultra():
    # Not an API value, and delegation does not require it: the collaboration
    # tools are present at every effort.
    for name, (_m, effort, _s, _r) in parse_seats().items():
        assert effort != "ultra", f"seat {name} is pinned to ultra"


def test_every_declared_sandbox_is_known_and_never_full_access():
    for name, (_m, _e, sandbox, _r) in parse_seats().items():
        assert sandbox in SANDBOXES, f"seat {name} has sandbox {sandbox!r}"


def test_no_writing_seat_runs_inside_the_repository():
    """Measured: `-C` does not bound a workspace-write sandbox.

    The boundary resolves to an ancestor of `-C` — the enclosing git
    repository when there is one — so a seat rooted anywhere inside this tree
    can write all of it. A seat rooted outside was refused an absolute-path
    write in ("operation not permitted"), so outside is the boundary that
    holds.
    """
    for name, (_m, _e, sandbox, workroot) in parse_seats().items():
        if sandbox == "workspace-write":
            assert workroot == "TMPTRAY", (
                f"seat {name} writes from inside the repository ({workroot!r}); "
                "it could write the whole tree"
            )


def test_the_drafting_seat_still_exists_and_writes_outside():
    seats = parse_seats()
    assert "build" in seats, "the drafting seat is gone — was it renamed?"
    _model, _effort, sandbox, workroot = seats["build"]
    assert sandbox == "workspace-write"
    assert workroot == "TMPTRAY"


def test_tmptray_resolves_to_a_fresh_directory_outside_the_repository():
    r = run("build", "x")
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    workdir = Path(argv[argv.index("-C") + 1])
    assert workdir.is_dir(), "the tray was not created"
    assert ROOT not in workdir.resolve().parents and workdir.resolve() != ROOT, (
        f"the tray {workdir} is inside the repository"
    )

    second = run("build", "x")
    other = second.stdout.splitlines()
    other_dir = other[other.index("-C") + 1]
    assert str(workdir) != other_dir, "two calls shared a tray; drafts would collide"


# --- resolution -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(parse_seats()))
def test_each_seat_resolves_to_its_declared_line(name):
    model, effort, sandbox, workroot = parse_seats()[name]
    r = run(name, "prompt text")
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()

    assert argv[0] == "codex" and argv[1] == "exec"
    assert "--ignore-user-config" in argv, "the call would inherit ~/.codex/config.toml"
    assert argv[argv.index("-m") + 1] == model
    assert argv[argv.index("-c") + 1] == f"model_reasoning_effort={effort}"
    assert argv[argv.index("-s") + 1] == sandbox
    assert argv[-1] == "prompt text", "the prompt is not the final argument"

    resolved = Path(argv[argv.index("-C") + 1]).resolve()
    if workroot == "TMPTRAY":
        assert resolved != ROOT and ROOT not in resolved.parents
    else:
        assert resolved == (ROOT / workroot).resolve()


def test_the_prompt_is_passed_as_an_argument_not_left_on_stdin():
    # The failure this prevents: codex prints "Reading additional input from
    # stdin..." and waits forever, spending nothing and looking like thought.
    r = run("judge", "-", stdin="from stdin\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "from stdin"


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


def test_timeout_is_reported_in_the_announcement():
    env_r = subprocess.run(
        ["sh", str(SEAT), "judge", "x"],
        capture_output=True,
        text=True,
        env={**os.environ, "CODEX_SEAT_DRYRUN": "1", "CODEX_SEAT_TIMEOUT": "42"},
        timeout=30,
    )
    assert "timeout 42s" in env_r.stderr


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
    seats = write_seats(tmp_path, "a m1 low read-only .\na m2 high read-only .\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "declared 2 times" in r.stderr


def test_effort_outside_the_api_enum_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol ultra read-only .\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "ultra" in r.stderr


def test_danger_full_access_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high danger-full-access .\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "danger-full-access" in r.stderr


def test_unknown_sandbox_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high wide-open .\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2


@pytest.mark.parametrize("workroot", [".", "autoclave", "operations", "operations/codex"])
def test_workspace_write_anywhere_inside_the_repository_is_refused(tmp_path, workroot):
    # `autoclave` is the important case: it is where drafts belong, and it was
    # this wrapper's first (wrong) answer to confinement.
    seats = write_seats(tmp_path, f"a gpt-5.6-terra medium workspace-write {workroot}\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "may not run inside the repository" in r.stderr


def test_incomplete_seat_line_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2


def test_missing_workroot_directory_is_refused(tmp_path):
    seats = write_seats(tmp_path, "a gpt-5.6-sol high read-only no/such/dir\n")
    r = run("a", "x", seats=seats)
    assert r.returncode == 2
    assert "does not exist" in r.stderr


def test_missing_seat_file_is_refused(tmp_path):
    r = run("judge", "x", seats=tmp_path / "absent.conf")
    assert r.returncode == 2


def test_every_shell_script_is_named_in_check_all():
    """A new script must not escape the static checks the way this one did.

    `check-all.sh` lists its scripts by hand so that each is checked
    deliberately. The cost of a hand-written list is that additions are
    forgotten: seat.sh spent money through an external API for a whole session
    before anything linted it. This test is the guard against a repeat.
    """
    check_all = (ROOT / ".githooks" / "check-all.sh").read_text()
    searched = [ROOT / ".githooks", ROOT / "operations"]
    scripts = sorted(
        path.relative_to(ROOT).as_posix()
        for directory in searched
        for path in directory.rglob("*.sh")
    )
    assert scripts, "found no shell scripts at all — the search paths are wrong"
    missing = [s for s in scripts if s not in check_all]
    assert not missing, f"shell scripts not checked by check-all.sh: {missing}"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    seats = write_seats(
        tmp_path,
        "# a comment\n\n   # indented comment\nsolo gpt-5.6-luna low read-only .\n",
    )
    r = run("solo", "x", seats=seats)
    assert r.returncode == 0, r.stderr
    assert "gpt-5.6-luna" in r.stdout
