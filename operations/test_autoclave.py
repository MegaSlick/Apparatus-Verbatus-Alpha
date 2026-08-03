"""The autoclave launcher, tested where it can be tested without an engine.

This file sits here by convention rather than by necessity, and the difference used
to be the other way round. `pyproject.toml` once put `autoclave` in pytest's
`norecursedirs` — for the cleanroom tray, which was called `autoclave/` until
2026-08-01 — and the pattern matched a directory of that name anywhere, so a test
beside the script would have been skipped in silence. The tray is `cleanroom/` now
and the skip entry moved with it, so a test placed beside the script **would** be
collected today. Nothing prevents moving this file back; nothing requires it either.

What is covered here is argument handling, path resolution and honest reporting.
Beyond that, a **recording stand-in for the `docker` CLI** drives the launcher's
engine-facing branches against real, disposable git repositories: what argv reached
the engine, what the chamber's own shell was handed, which host refs and files
survived a failure. Those are outcome tests of the launcher, not claims that an image
built or that a mount behaved — the stub is not a daemon and does not pretend to be
one. What still needs a real engine is named in `operations/autoclave/README.md`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "operations" / "autoclave" / "autoclave.sh"
SAFE_FILE = ROOT / "operations" / "autoclave" / "safe_file.py"


def run(*args, cwd=None, env=None, script=SCRIPT):
    """Invoke the launcher and return the completed process.

    `sh` explicitly rather than relying on the executable bit: the script
    declares `#!/bin/sh` and the point is that it runs under a POSIX shell, not
    under whatever the caller's login shell happens to be.
    """
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        cwd=cwd or ROOT,
        env={**os.environ, **(env or {})},
    )


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def elsewhere(tmp_path):
    """A copy of the launcher in a throwaway repository of its own.

    The launcher resolves its repository from `$0`, so a copy at
    `<tmp>/operations/autoclave/autoclave.sh` treats `<tmp>` as the repository and
    `<tmp>/workbench/autoclave` as its output root. That is what lets the tests below
    exercise commands that *write* — a snapshot ref, an output drawer, a fetched
    branch — without touching this repository or this machine's chambers.
    """
    directory = tmp_path / "operations" / "autoclave"
    directory.mkdir(parents=True)
    copy = directory / SCRIPT.name
    shutil.copy2(SCRIPT, copy)
    if SAFE_FILE.is_file():
        shutil.copy2(SAFE_FILE, directory / SAFE_FILE.name)
    subprocess.run(["git", "init", "--quiet", "-b", "work/test", str(tmp_path)], check=True)
    # An identity in the repository's own config rather than on a command line: `new`
    # writes its snapshot with `git commit-tree`, which needs one and is not this
    # test's to pass flags to. Without it the snapshot fails for the wrong reason and
    # the test passes without proving anything.
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    # A base commit, so `HEAD` resolves and `new` can reach the work that follows it.
    # Staged by name rather than with `add -A`: CLAUDE.md's Branches section forbids
    # the bulk form, and a test is not exempt from a rule it would teach.
    #
    # No `--no-verify`: a fresh `git init` has no `core.hooksPath` so there is nothing
    # to skip, and the trailer is here so this still commits on a machine where one is
    # set globally. A test reaching for that flag to make itself work is a test
    # teaching the flag.
    git(tmp_path, "add", "operations")
    git(
        tmp_path,
        "commit",
        "--quiet",
        "-m",
        "base\n\nCo-Authored-By: autoclave <autoclave@localhost>",
    )
    return copy


# A stand-in for `docker`, recording every call as one JSON line and answering from
# the environment. It is not a daemon: it proves what the launcher *asked for* and
# what it did with the answer, which is the half of this script that no amount of
# reading the source can pin down.
#
# Two behaviours earn their keep beyond recording. `docker exec ... sh -c '<script>'`
# against a chamber runs that script for real, with `cd /work` rewritten to a real
# throwaway clone — so `collect` and `rm` are tested against actual git states rather
# than against a mock's opinion of one. And `docker exec ... sh -s` writes the script
# it was handed to a file, so a test can read exactly what crossed into the chamber.
DOCKER_STUB = """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")


def setting(name, default=""):
    return os.environ.get(name, default)


def vendor_status(command):
    return "auth status" in command or "login status" in command


if args[:1] == ["info"] or args[:2] == ["image", "inspect"]:
    raise SystemExit(0)
if args[:2] == ["volume", "inspect"]:
    raise SystemExit(0 if args[-1] in setting("FAKE_VOLUMES").split() else 1)
if args[:2] in (["volume", "create"], ["volume", "rm"]):
    raise SystemExit(0)
if args[:2] == ["container", "inspect"]:
    present = setting("FAKE_CONTAINER_EXISTS") == "1"
    if "-f" in args:
        # `running()`, which is a separate question from `exists()`: a stopped
        # chamber is there to be removed and cannot be read.
        print("true" if present and setting("FAKE_CONTAINER_RUNNING", "1") == "1" else "false")
        raise SystemExit(0)
    raise SystemExit(0 if present else 1)
if args[:1] == ["inspect"]:
    template = " ".join(args)
    if "verbatus.base" in template:
        print(setting("FAKE_CHAMBER_BASE"))
    elif "verbatus.vendor" in template:
        print(setting("FAKE_CHAMBER_VENDOR", "codex"))
    raise SystemExit(0)
if args[:1] == ["run"]:
    if "--detach" in args:
        raise SystemExit(int(setting("FAKE_RUN_STATUS", "0")))
    if vendor_status(args[-1]):
        raise SystemExit(0 if setting("FAKE_AUTH_VALID") == "1" else 1)
    raise SystemExit(int(setting("FAKE_LOGIN_STATUS", "0")))
if args[:1] == ["exec"]:
    if args[-2:] == ["sh", "-s"]:
        with open(setting("FAKE_SETUP_SCRIPT", os.devnull), "w") as recorded:
            recorded.write(sys.stdin.read())
        raise SystemExit(int(setting("FAKE_SETUP_STATUS", "1")))
    command = args[-1]
    if vendor_status(command):
        raise SystemExit(0 if setting("FAKE_AUTH_VALID") == "1" else 1)
    chamber = setting("FAKE_CHAMBER_WORK")
    if chamber and "cd /work" in command:
        command = command.replace("cd /work", "cd " + chamber)
        outdir = setting("FAKE_OUT_DIR")
        if outdir:
            command = command.replace("/out/", outdir + "/")
        raise SystemExit(subprocess.run(["sh", "-c", command]).returncode)
    raise SystemExit(int(setting("FAKE_EXEC_STATUS", "1")))
if args[:1] == ["rm"]:
    raise SystemExit(0)
raise SystemExit(0)
"""


def fake_docker(tmp_path):
    """Install the stub on PATH and return the environment that drives it.

    Failing stubs for `claude` and `codex` go on the same PATH. If any invocation
    drifts out of the container and onto the host, the dispatch fails and the
    attempt is recorded — which is a stronger statement than reading the source and
    concluding that it cannot.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    host_vendor_log = tmp_path / "host-vendor-was-called"
    for vendor in ("claude", "codex"):
        stub = bindir / vendor
        stub.write_text(f"#!/bin/sh\nprintf '%s\\n' {vendor} >> '{host_vendor_log}'\nexit 99\n")
        stub.chmod(0o755)
    log = tmp_path / "docker.jsonl"
    return {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_HOST_VENDOR_LOG": str(host_vendor_log),
        "FAKE_SETUP_SCRIPT": str(tmp_path / "setup.sh"),
    }, log


def docker_calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def code_lines():
    """The launcher's runnable lines, with comments and blanks dropped.

    Several tests below assert that a dangerous flag or a known-trap invocation
    appears exactly once. Matching raw source counts the explanatory comment
    beside the code as a second occurrence, which made two of these tests fail on
    the very comments written to justify them. Shell has no block comments, so
    dropping `#` lines is sufficient and honest here.
    """
    return [
        line
        for line in SCRIPT.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "launcher is not executable"


def test_help_stops_at_the_header():
    """The help text is the header comment, and must not bleed into shell code.

    It is generated by reading the file's own leading comment, so the failure
    this guards against is real: an earlier version used a fixed line range and
    printed `set -eu` and a variable assignment to the operator.
    """
    result = run("help")
    assert result.returncode == 0
    assert "autoclave.sh doctor" in result.stdout
    assert "set -eu" not in result.stdout
    assert "IMAGE_NAME=" not in result.stdout


def test_no_argument_prints_help_rather_than_failing():
    bare = run()
    assert bare.returncode == 0
    assert "Usage:" in bare.stdout


def test_unknown_command_is_refused_by_name():
    result = run("frobnicate")
    assert result.returncode != 0
    assert "frobnicate" in result.stderr


def test_doctor_reports_state_without_an_engine():
    """`doctor` must answer honestly on a machine with nothing installed.

    It is the first thing anyone runs, and a diagnostic that fails when the
    thing it diagnoses is absent tells you nothing.
    """
    result = run("doctor")
    assert result.returncode == 0
    for label in ("repository:", "output root:", "colima:", "docker CLI:", "engine:", "image:"):
        assert label in result.stdout, f"doctor did not report {label!r}"


def test_doctor_resolves_the_repository_from_the_script_not_the_caller(tmp_path):
    """Every command must work from anywhere, so the root is resolved from $0."""
    result = run("doctor", cwd=tmp_path)
    assert result.returncode == 0
    assert str(ROOT) in result.stdout


class TestTaskNames:
    """A task name becomes a container name, a directory and a git branch.

    It is validated in one place so the three cannot disagree, and the cases
    below are the ones that would otherwise produce an invalid branch ref or a
    surprising path.
    """

    def test_empty_is_refused(self):
        result = run("new", "")
        assert result.returncode != 0
        assert "required" in result.stderr

    def test_missing_is_refused(self):
        result = run("new")
        assert result.returncode != 0
        assert "required" in result.stderr

    def test_uppercase_and_underscores_are_refused(self):
        for name in ("Bad_Name", "UPPER", "has_underscore", "has space", "has/slash"):
            result = run("new", name)
            assert result.returncode != 0, f"{name!r} was accepted"
            assert "lowercase" in result.stderr

    def test_leading_or_trailing_hyphen_is_refused(self):
        for name in ("-leading", "trailing-"):
            result = run("new", name)
            assert result.returncode != 0, f"{name!r} was accepted"
            assert "hyphen" in result.stderr

    def test_a_good_name_gets_past_validation(self):
        """A well-formed name must fail for the *next* reason, not the name.

        Checked through `report`, which validates the name and then looks for a
        file — it creates nothing. An earlier version of this test called `new`,
        which was wrong twice over: a test of argument handling must not invoke a
        command that can create a container, and the assertion it made ("the run
        stops at the missing docker") silently depended on the machine having no
        container engine. It passed until an engine was installed.
        """
        result = run("report", "spec-01-contracts")
        assert result.returncode != 0
        assert "lowercase" not in result.stderr
        assert "hyphen" not in result.stderr
        # Past validation and into the real work: it is looking for the report.
        assert "report.md" in result.stderr

    def test_validation_happens_before_anything_is_created(self):
        """A rejected name must not reach the engine at all.

        `new` is the only command that creates something, so the guarantee is
        asserted where it matters: a bad name is refused by the argument check,
        never by something further down that might have had a side effect first.
        """
        result = run("new", "Bad_Name")
        assert result.returncode != 0
        assert "lowercase" in result.stderr
        assert "docker" not in result.stderr.lower()


class TestLogin:
    """Signing a vendor in is a one-time act, and it must stay one-time.

    The sign-in lands in a Docker named volume rather than the image or a bind
    mount, which is what makes it survive every container and every restart. It
    is also what keeps a live host credential out of a chamber that has network
    egress.
    """

    def test_a_missing_vendor_is_refused(self):
        result = run("login")
        assert result.returncode != 0
        assert "claude" in result.stderr and "codex" in result.stderr

    def test_an_unknown_vendor_is_refused(self):
        result = run("login", "gemini")
        assert result.returncode != 0
        assert "claude" in result.stderr and "codex" in result.stderr

    def test_the_script_names_no_credential_path_on_the_host(self):
        """The whole point is that no host credential is read or mounted.

        Asserted against the source, because the failure would be someone later
        'helpfully' bind-mounting `~/.codex` or reaching into the Keychain to
        save Tyrel a step. Both would put a live credential inside a chamber
        with network egress.
        """
        source = SCRIPT.read_text()
        for forbidden in (
            "~/.codex",
            "~/.claude",
            "$HOME/.codex",
            "$HOME/.claude",
            "security find-",
        ):
            assert forbidden not in source, f"launcher reaches for a host credential: {forbidden}"

    def test_auth_is_mounted_read_write(self):
        """Read-only would turn a one-time sign-in into a recurring one, because
        both CLIs refresh their own tokens and would have nowhere to write."""
        source = SCRIPT.read_text()
        for volume in ("AUTH_VOL_CLAUDE", "AUTH_VOL_CODEX"):
            assert f"${{{volume}}}:" in source
            assert f"${{{volume}}}:ro" not in source, f"{volume} is mounted read-only"

    def test_a_chamber_holds_one_vendor_credential_or_none(self):
        """It used to hold every sign-in that existed, whichever vendor was later
        dispatched — so a Codex agent held the Claude credential and the reverse,
        read-write, with open egress. The vendor is chosen at `new`, because a mount
        cannot be added to a running container, and `dispatch` refuses a mismatch.
        """
        source = SCRIPT.read_text()
        assert 'vendor="${3:-none}"' in source, "new no longer takes a vendor"
        assert "verbatus.vendor" in source, "the chamber does not record its vendor"
        assert "holds no ${vendor} credential" in source, "dispatch does not check the vendor"
        # Both volumes reachable from one code path is the shape of the old defect.
        both = f"{source.count('AUTH_VOL_CLAUDE:-')}{source.count('AUTH_VOL_CODEX:-')}"
        assert both == "00", "a single branch still mounts both vendors"

    def test_the_mountpoints_are_created_before_the_bind(self):
        """All three masked drawers are gitignored, so a fresh clone has none of
        them. Docker builds a missing mountpoint inside the bind, `/src` is
        read-only, and `docker run` exits 125 — `new` fails outright on any machine
        but this one. Verified before this line existed.
        """
        source = SCRIPT.read_text()
        created = source.index('mkdir -p "${REPO_ROOT}/private"')
        assert created < source.index("--tmpfs /src/private"), "mountpoints made too late"

    def test_the_private_drawer_is_masked_inside_a_chamber(self):
        """`/src` read-only stops an agent changing this machine, not reading it.

        `private/` holds the notification bearer topic, and a chamber has open
        network egress, so a readable secret is a sendable one. The mount must be
        an empty tmpfs over that one path — not an exclusion from the bind, which
        Docker cannot express, and not deletion of the directory, which `/src`
        being read-only forbids anyway.
        """
        source = SCRIPT.read_text()
        assert "--tmpfs /src/private" in source, "the private drawer is exposed to every chamber"
        assert "/src/private:ro" in source, "the masking tmpfs is writable"
        # `workbench/` is the other drawer the guard's own SECRET_DRAWERS names, and
        # it holds every handoff, note and reviewer transcript. Masking `private/`
        # alone left half the rule unenforced.
        for drawer in ("workbench", "scriptorium"):
            assert f"--tmpfs /src/{drawer}:ro" in source, f"{drawer} is exposed to every chamber"


class TestDispatch:
    """Running an agent inside a chamber, against a brief written to a file."""

    # `.claude/agents/README.md`, "Reachability, which is not negotiable": `minimal` is
    # rejected by every Codex model; `gpt-5.3-codex-spark` also rejects `none` and
    # `max`; Claude accepts `low`, `medium`, `high`, `xhigh`, `max` and nothing else.
    # The launcher used to accept the union of all three for every vendor, and the test
    # that covered it asserted only that a refusal string was *absent* — which it was
    # for all seven, including the four no vendor would run, and which stayed true when
    # the whole `case` block was deleted.
    REACHABLE = {
        ("claude", "opus"): ("low", "medium", "high", "xhigh", "max"),
        ("codex", "gpt-5.6-luna"): ("none", "low", "medium", "high", "xhigh", "max"),
        ("codex", "gpt-5.3-codex-spark"): ("low", "medium", "high", "xhigh"),
    }

    def test_a_reachable_effort_gets_past_effort_validation(self):
        """A level the vendor accepts must fail for the *next* reason, not the effort.

        Both halves are asserted: the effort complaint is absent, and the run still
        stopped further along. Written with the absence alone, this passed for every
        string that never reached the check at all, which is how it passed for
        `minimal` on Claude.
        """
        for (vendor, model), levels in self.REACHABLE.items():
            for effort in levels:
                result = run("dispatch", "task-x", vendor, str(ROOT / "README.md"), model, effort)
                assert "not an allowed effort" not in result.stderr, (
                    f"{vendor} {model} refused the reachable effort {effort!r}"
                )
                assert result.returncode != 0
                stopped_at = result.stderr.lower()
                assert "docker" in stopped_at or "chamber" in stopped_at, (
                    f"{vendor} {model} {effort} stopped for an unexpected reason: {stopped_at!r}"
                )

    def test_an_unreachable_effort_is_refused_before_the_engine(self):
        """A level the vendor is measured to reject must cost a line of output.

        It used to cost a chamber and a model process: the launcher accepted the union
        of both vendors' vocabularies, so a documented-as-unreachable value passed
        validation and failed inside the vendor CLI two layers down, in a message
        naming neither the argument nor this script.
        """
        unreachable = [
            ("claude", "opus", "none"),
            ("claude", "opus", "minimal"),
            ("codex", "gpt-5.6-luna", "minimal"),
            ("codex", "gpt-5.3-codex-spark", "none"),
            ("codex", "gpt-5.3-codex-spark", "max"),
            ("codex", "gpt-5.3-codex-spark", "minimal"),
        ]
        for vendor, model, effort in unreachable:
            result = run("dispatch", "task-x", vendor, str(ROOT / "README.md"), model, effort)
            assert result.returncode != 0, f"{vendor} {model} accepted {effort!r}"
            assert "not an allowed effort" in result.stderr
            assert vendor in result.stderr, "the refusal does not say which vendor"
            assert "docker" not in result.stderr.lower(), "the engine was reached first"

    def test_an_unmeasured_codex_model_gets_the_general_codex_range(self):
        """Not a refusal: a launcher that rejects every seat added after it was
        written is a launcher somebody edits under time pressure to get a dispatch
        out. `minimal`, which no Codex model accepts, is still refused."""
        allowed = run(
            "dispatch", "task-x", "codex", str(ROOT / "README.md"), "gpt-9-unmeasured", "max"
        )
        assert "not an allowed effort" not in allowed.stderr
        refused = run(
            "dispatch", "task-x", "codex", str(ROOT / "README.md"), "gpt-9-unmeasured", "minimal"
        )
        assert "not an allowed effort" in refused.stderr

    def test_a_missing_task_is_refused(self):
        result = run("dispatch")
        assert result.returncode != 0
        assert "required" in result.stderr

    def test_an_unknown_vendor_is_refused(self):
        # Arguments are now checked left to right and all of them before Docker is
        # touched, so this names the vendor rather than stopping at the chamber. The
        # previous ordering reported "chamber not running" for a command that was
        # never going to run whatever the chamber's state — true, and unhelpful.
        result = run("dispatch", "nosuchtask", "gemini", str(SCRIPT), "gpt-5.6-luna")
        assert result.returncode != 0
        assert "claude" in result.stderr and "codex" in result.stderr

    def test_the_brief_is_the_cli_s_standard_input_never_an_argument(self):
        """A brief is prose a session wrote, and it never becomes argv anywhere.

        It used to be `"$(cat /out/brief.md)"` inside the container. Quoted, so its
        punctuation was inert — which is what made that safe and is all it ever
        established. Three things were still true: a brief past `MAX_ARG_STRLEN`
        (128 KiB) fails the exec outright, command substitution eats every trailing
        newline, and the whole brief sits in the container's process list. Both CLIs
        document the file form and both were run to confirm it.
        """
        joined = " ".join(code_lines()).replace("\\ ", " ")
        assert joined.count("< /out/brief.md") == 2, "one vendor is not reading the file"
        assert "$(cat /out/brief.md)" not in joined, "the brief is expanded into an argument"

    def test_permission_skipping_is_confined_to_the_chamber(self):
        """`--dangerously-skip-permissions` is correct inside a container and
        nowhere else, because the container is the boundary and a prompt inside a
        detached one is a hang. This asserts it appears in exactly one runnable
        line, so it cannot drift into something that executes on the host.
        """
        lines = code_lines()
        carrying = [ln for ln in lines if "--dangerously-skip-permissions" in ln]
        assert len(carrying) == 1, f"expected one runnable use, found {len(carrying)}"
        index = lines.index(carrying[0])
        preceding = "\n".join(lines[:index])
        assert "docker exec" in preceding, "the flag is not inside a docker exec"

    def test_a_model_is_required(self):
        """An omitted model runs the vendor's default, and `codex doctor` reports that
        default is `gpt-5.6-sol` — the most expensive seat OpenAI sells. Every chamber
        would silently have been a flagship chamber and the tier table in
        `.claude/agents/README.md` would describe a choice nothing ever made."""
        result = run("dispatch", "task-x", "claude", str(ROOT / "README.md"))
        assert result.returncode != 0
        assert "needs a model" in result.stderr

    @pytest.mark.parametrize(
        ("model", "effort", "expected"),
        [
            ("-evil", "medium", "not a plain model name"),
            ("gpt-5.6-luna; touch PWNED", "medium", "not a plain model name"),
            ("opus", "bogus", "not an allowed effort"),
        ],
    )
    def test_the_model_and_effort_are_validated(self, model, effort, expected):
        """Validated rather than trusted, exactly as `operations/codex/seat.sh`
        validates the same fields. Both reach the container as environment variables
        and never as interpolated text, but a value beginning with `-` would still
        arrive at the CLI as a flag."""
        result = run("dispatch", "task-x", "claude", str(ROOT / "README.md"), model, effort)
        assert result.returncode != 0
        assert expected in result.stderr

    def test_validation_precedes_any_docker_call(self):
        """A typo in a model name should cost a line of output, not a container's
        startup and a confusing failure from a vendor CLI two layers down."""
        result = run("dispatch", "task-x", "claude", str(ROOT / "README.md"), "-evil")
        assert "docker" not in result.stderr.lower()

    def test_both_vendors_receive_model_and_effort_in_their_own_spelling(self):
        """The two CLIs spell effort differently and neither spelling is guessable.
        `claude` takes `--effort <level>`; `codex exec` has no effort flag at all and
        needs the config override `-c model_reasoning_effort=`, which is how
        `operations/codex/seat.sh` has always done it. Checked against `--help` on
        both rather than assumed."""
        joined = " ".join(code_lines())
        assert '--model "$AC_MODEL"' in joined and '--effort "$AC_EFFORT"' in joined
        assert '-m "$AC_MODEL"' in joined
        assert '-c "model_reasoning_effort=$AC_EFFORT"' in joined

    def test_the_model_travels_as_an_environment_variable(self):
        """Same reasoning as the brief travelling as a file: a value interpolated into
        a quoted command line brings its punctuation with it."""
        joined = " ".join(code_lines())
        assert joined.count('-e AC_MODEL="$model" -e AC_EFFORT="$effort"') == 2

    def test_the_codex_prompt_is_behind_an_end_of_options_marker(self):
        """`--` so a prompt beginning with a dash is a prompt, `-` because that is
        how `codex exec` spells "read the instructions from stdin"."""
        joined = " ".join(code_lines()).replace("\\ ", " ")
        assert "-- - < /out/brief.md" in joined

    def test_codex_gets_a_finite_stdin(self):
        """`codex exec` waits forever on an open stdin when nothing is attached,
        which inside a detached container is a dispatch that never returns and
        never says why. A known trap, recorded in this project's own notes. A
        regular file gives the prompt and then an immediate EOF, which is the
        property `< /dev/null` used to supply.

        Continuations are joined before looking, because the invariant is that the
        *invocation* bounds stdin and not that it fits on one physical line. Written
        the other way, this failed the moment a `--model` argument was added and the
        command wrapped — a test reporting a formatting change as a safety defect.
        """
        joined = " ".join(code_lines()).replace("\\ ", " ")
        carrying = [part for part in joined.split(";") if "codex exec" in part]
        assert len(carrying) == 1, f"expected one runnable use, found {len(carrying)}"
        assert "< /out/brief.md" in carrying[0], "codex is handed an unbounded stdin"


def test_doctor_reports_sign_in_state_for_both_vendors():
    result = run("doctor")
    assert result.returncode == 0
    assert "auth claude" in result.stdout
    assert "auth codex" in result.stdout


class TestWhatComesBackAndWhatIsDestroyed:
    """`collect` and `rm`, against a real clone standing in for the chamber's tree.

    The stub rewrites `cd /work` to that clone and runs the launcher's own shell for
    real, so these are outcome tests: what the refusal actually saw, which ref actually
    arrived, whether the container was actually removed.
    """

    def chamber(self, tmp_path, *, commits=1, attributed=True):
        script = elsewhere(tmp_path)
        (tmp_path / ".gitignore").write_text("workbench/\n")
        (tmp_path / "tracked.txt").write_text("base\n")
        git(tmp_path, "add", ".gitignore", "tracked.txt")
        git(tmp_path, "commit", "--quiet", "-m", "files\n\nCo-Authored-By: a <a@b>")
        base = git(tmp_path, "rev-parse", "HEAD")

        clone = tmp_path / "chamber"
        subprocess.run(["git", "clone", "--quiet", str(tmp_path), str(clone)], check=True)
        git(clone, "switch", "--quiet", "-c", "agent/task-x")
        git(clone, "config", "user.name", "autoclave")
        git(clone, "config", "user.email", "autoclave@localhost")
        for index in range(commits):
            (clone / "tracked.txt").write_text(f"changed {index}\n")
            git(clone, "add", "tracked.txt")
            message = f"work {index}"
            if attributed:
                message += "\n\nCo-Authored-By: Test Model <model@example.invalid>"
            git(clone, "commit", "--quiet", "-m", message)

        drawer = tmp_path / "workbench" / "autoclave" / "task-x"
        drawer.mkdir(parents=True)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_WORK": str(clone),
                "FAKE_CHAMBER_BASE": base,
                "FAKE_OUT_DIR": str(drawer),
            }
        )
        return script, clone, drawer, env, log

    def test_collect_refuses_work_that_was_never_added_and_names_it(self, tmp_path):
        """A bundle carries commits, so untracked work is left behind — and `rm` then
        destroys the only copy. `git diff` and `git diff --cached` between them see
        modified and staged files and say nothing at all about an untracked one, which
        is the shape most of a chamber's output takes."""
        script, clone, drawer, env, _log = self.chamber(tmp_path)
        (clone / "new-module.py").write_text("# work an agent did\n")

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "new-module.py" in result.stderr, "the refusal did not name the path at risk"
        assert not (drawer / "task-x.bundle").exists()
        arrived = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "agent/task-x"],
            capture_output=True,
        )
        assert arrived.returncode != 0, "a branch arrived from a tree that was not clean"

    def test_the_premise_that_git_diff_is_blind_to_untracked_work(self, tmp_path):
        """Pinned against real git, so the change above rests on a fact rather than on
        a comment. If git ever starts reporting untracked paths from `diff`, this says
        so instead of the fix quietly becoming pointless."""
        subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
        (tmp_path / "new-file.py").write_text("# work an agent did\n")
        diff = subprocess.run(["git", "-C", str(tmp_path), "diff", "--quiet"])
        cached = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--quiet"])
        porcelain = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain"], capture_output=True, text=True
        )
        assert diff.returncode == 0 and cached.returncode == 0
        assert "new-file.py" in porcelain.stdout

    def test_a_clean_chamber_is_collected(self, tmp_path):
        """The positive control. Without it the refusals above pass for a `collect`
        that can no longer collect anything."""
        script, clone, drawer, env, _log = self.chamber(tmp_path)

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert (drawer / "task-x.bundle").is_file()
        assert git(tmp_path, "rev-parse", "agent/task-x") == git(clone, "rev-parse", "agent/task-x")
        assert "1 commit(s)" in result.stdout
        assert "UNATTRIBUTED" not in result.stdout

    def test_an_empty_branch_collects_and_says_it_is_empty(self, tmp_path):
        """The report is worth keeping, so this succeeds — loudly. Reporting success
        on nothing is the one thing this tool must not do."""
        script, _clone, drawer, env, _log = self.chamber(tmp_path, commits=0)
        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "NO COMMITS" in result.stdout
        assert (drawer / "task-x.bundle").is_file()

    def test_collect_names_commits_that_carry_no_attribution(self, tmp_path):
        """`commit-msg` is switched on inside a chamber now, so ordinarily every
        returned commit already names its model. Ordinarily is not always — the hook
        can be skipped, and a chamber created before that change never had it — and
        `pre-push` looks at reviewers and credentials, never at authorship. This is
        the last place anything checks, so it checks rather than assuming.

        It names them and does not refuse: a branch is not made safer by being
        stranded inside a container, and `rm` already refuses to destroy work nobody
        collected.
        """
        script, _clone, _drawer, env, _log = self.chamber(tmp_path, attributed=False)

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert "UNATTRIBUTED" in result.stdout
        assert "work 0" in result.stdout, "the warning does not name the commits"

    def test_rm_refuses_a_chamber_holding_work_nobody_collected(self, tmp_path):
        script, _clone, _drawer, env, log = self.chamber(tmp_path)
        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "holds commits this repository does not have" in result.stderr
        assert "collect task-x" in result.stderr, "the way out is not named"
        assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log)), (
            "the container was destroyed with commits this repository does not have"
        )

    def test_rm_refuses_a_chamber_with_a_dirty_tree(self, tmp_path):
        script, clone, _drawer, env, log = self.chamber(tmp_path, commits=0)
        (clone / "not-collected.txt").write_text("an hour of work\n")
        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "not-collected.txt" in result.stderr
        assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log))

    def test_rm_refuses_a_stopped_chamber_rather_than_destroying_it_unread(self, tmp_path):
        """A stopped chamber cannot be read, so nothing here can call it safe. Warning
        and destroying it anyway is the same loss with a sentence in front of it."""
        script, _clone, _drawer, env, log = self.chamber(tmp_path, commits=0)
        env["FAKE_CONTAINER_RUNNING"] = "0"
        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "stopped" in result.stderr
        assert "rm task-x force" in result.stderr, "the way out is not named"
        assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log))

    def test_force_destroys_a_stopped_chamber_without_reading_it(self, tmp_path):
        """The escape hatch the refusal names. Without it a chamber that will not
        start could never be removed by this tool at all."""
        script, _clone, _drawer, env, log = self.chamber(tmp_path)
        env["FAKE_CONTAINER_RUNNING"] = "0"
        result = run("rm", "task-x", "force", env=env, script=script, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert ["rm", "--force", "verbatus-ac-task-x"] in docker_calls(log)

    def test_rm_destroys_a_chamber_whose_work_was_collected(self, tmp_path):
        script, clone, _drawer, env, log = self.chamber(tmp_path)
        git(tmp_path, "fetch", "--quiet", str(clone), "agent/task-x:agent/task-x")
        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert ["rm", "--force", "verbatus-ac-task-x"] in docker_calls(log)

    def test_rm_takes_only_the_word_force(self, tmp_path):
        """A typo that reaches `docker rm --force` because it was not the word we
        expected is exactly the failure this refuses."""
        script = elsewhere(tmp_path)
        result = run("rm", "some-task", "--force", script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "force" in result.stderr
        assert "docker" not in result.stderr.lower(), "the engine was reached first"


class TestTheUntrustedDrawer:
    """`/out` is the one host path a chamber agent can write, both ways.

    What the launcher reads back from it is untrusted input; what it writes into it
    goes to a name the agent controls. Testing a path and then acting on it leaves a
    window the agent is running inside, so the acting has to be what checks.
    """

    def drawer(self, tmp_path, task="some-task"):
        script = elsewhere(tmp_path)
        slot = tmp_path / "workbench" / "autoclave" / task
        slot.mkdir(parents=True)
        return script, slot

    def test_a_report_that_is_a_symlink_is_refused_and_never_printed(self, tmp_path):
        """Asserting the exit status alone would pass for a command that printed the
        file and then failed, which is the whole of the damage."""
        script, slot = self.drawer(tmp_path)
        secret = tmp_path / "not-for-you.txt"
        secret.write_text("SENTINEL-CONTENTS\n")
        (slot / "report.md").symlink_to(secret)

        result = run("report", "some-task", script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "SENTINEL-CONTENTS" not in result.stdout
        assert "SENTINEL-CONTENTS" not in result.stderr, "the refusal quoted what it withheld"

    @pytest.mark.parametrize("shape", ["directory", "fifo"])
    def test_a_report_that_is_not_a_regular_file_is_refused(self, tmp_path, shape):
        """A FIFO is the interesting one: opened for reading it blocks until somebody
        writes, so an agent could hang the session without pointing anywhere."""
        script, slot = self.drawer(tmp_path)
        if shape == "directory":
            (slot / "report.md").mkdir()
        else:
            os.mkfifo(slot / "report.md")

        result = subprocess.run(
            ["sh", str(script), "report", "some-task"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=20,
        )
        assert result.returncode != 0

    def test_an_ordinary_report_is_still_printed(self, tmp_path):
        """The refusals must not have cost the command its only job."""
        script, slot = self.drawer(tmp_path)
        (slot / "report.md").write_text("what the agent said\n")
        result = run("report", "some-task", script=script, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "what the agent said" in result.stdout

    def test_a_brief_is_not_written_through_a_link_left_in_the_slot(self, tmp_path):
        script, slot = self.drawer(tmp_path, task="task-x")
        victim = tmp_path / "keep-me.txt"
        victim.write_text("ORIGINAL\n")
        (slot / "brief.md").symlink_to(victim)
        brief = tmp_path / "brief-source.md"
        brief.write_text("bounded task\n")
        env, _log = fake_docker(tmp_path)
        env.update({"FAKE_CONTAINER_EXISTS": "1", "FAKE_AUTH_VALID": "1"})

        result = run(
            "dispatch",
            "task-x",
            "codex",
            str(brief),
            "gpt-5.6-luna",
            "low",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode != 0
        assert victim.read_text() == "ORIGINAL\n", "the brief was written through the link"

    def test_the_helper_refuses_a_link_and_leaves_its_target_alone(self, tmp_path):
        """Directly, because the launcher's own `[ -L ]` test would mask it.

        That test says what is wrong in a sentence and is worth keeping; it is not
        what makes this safe, and a test that only ever reaches it would not notice
        the helper being removed.
        """
        source = tmp_path / "source"
        source.write_text("new bytes")
        victim = tmp_path / "victim"
        victim.write_text("keep")
        slot = tmp_path / "slot"
        slot.symlink_to(victim)

        write = subprocess.run(
            ["python3", str(SAFE_FILE), "write", str(source), str(slot)],
            capture_output=True,
            text=True,
        )
        read = subprocess.run(
            ["python3", str(SAFE_FILE), "read", str(slot)], capture_output=True, text=True
        )

        assert write.returncode != 0 and read.returncode != 0
        assert read.stdout == ""
        assert victim.read_text() == "keep"
        assert "keep" not in write.stderr + read.stderr

    def test_the_helper_moves_ordinary_bytes(self, tmp_path):
        """The positive control for the two refusals above."""
        source = tmp_path / "source"
        source.write_text("bounded task\n")
        destination = tmp_path / "destination"
        write = subprocess.run(
            ["python3", str(SAFE_FILE), "write", str(source), str(destination)],
            capture_output=True,
            text=True,
        )
        assert write.returncode == 0, write.stderr
        assert destination.read_text() == "bounded task\n"


class TestMakingAChamber:
    """`new` writes to the host repository, and every write must be undoable."""

    def dirty_repo(self, tmp_path):
        """A repository with something in it that a snapshot would have to capture.

        A clean tree takes no snapshot at all, so a test run against one passes
        whatever the ordering and whatever the snapshot does.
        """
        script = elsewhere(tmp_path)
        (tmp_path / ".gitignore").write_text("ignored-*\n")
        for name in ("staged.txt", "unstaged.txt", "deleted.txt"):
            (tmp_path / name).write_text("base\n")
        git(tmp_path, "add", ".gitignore", "staged.txt", "unstaged.txt", "deleted.txt")
        git(tmp_path, "commit", "--quiet", "-m", "files\n\nCo-Authored-By: a <a@b>")
        (tmp_path / "staged.txt").write_text("staged\n")
        git(tmp_path, "add", "staged.txt")
        (tmp_path / "unstaged.txt").write_text("unstaged\n")
        (tmp_path / "deleted.txt").unlink()
        (tmp_path / "untracked\nname.txt").write_text("untracked\n")
        (tmp_path / "ignored-secret").write_text("ignored\n")
        return script

    def snapshot_of(self, tmp_path, log):
        """The commit the chamber was actually pinned to, read off the label."""
        created = [call for call in docker_calls(log) if "--detach" in call]
        assert len(created) == 1, "expected exactly one chamber to be created"
        label = next(arg for arg in created[0] if arg.startswith("verbatus.base="))
        return label.removeprefix("verbatus.base=")

    def test_a_failed_new_leaves_no_snapshot_ref_behind(self, tmp_path):
        """Nothing writes to the host repository until every prerequisite has passed.

        The snapshot is the first thing `new` writes, and it used to be written before
        the engine, the image and the chamber name had been checked. A `new` that
        failed for any of those three left `refs/heads/autoclave/snapshot-<task>` and
        its commit standing — and `rm` refuses a task with no container, so the one
        command that deletes that ref could not be reached to do it.
        """
        script = self.dirty_repo(tmp_path)
        result = run("new", "some-task", script=script, cwd=tmp_path)
        assert result.returncode != 0, "new succeeded on a machine with no engine"
        assert "docker" in result.stderr.lower(), result.stderr
        ref = subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/heads/autoclave/snapshot-some-task",
            ],
            capture_output=True,
            text=True,
        )
        assert ref.returncode != 0, f"a failed new left {ref.stdout.strip()} behind"

    def test_a_failed_docker_run_takes_the_snapshot_ref_with_it(self, tmp_path):
        """The one failure past the checks that can still leave a ref standing."""
        script = self.dirty_repo(tmp_path)
        env, _log = fake_docker(tmp_path)
        env["FAKE_RUN_STATUS"] = "1"
        result = run("new", "some-task", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "snapshot ref was removed" in result.stderr, result.stderr
        ref = subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/heads/autoclave/snapshot-some-task",
            ],
            capture_output=True,
        )
        assert ref.returncode != 0

    def test_the_snapshot_carries_the_whole_working_tree_and_touches_no_index(self, tmp_path):
        """Staged, unstaged, deleted and untracked, and nothing that is ignored.

        `git add -A` against a temporary index did this correctly, and CLAUDE.md's
        Branches section still says "Never `git add -A`" with no exception. Plumbing
        does the same job without a line that reads as a rule violation. The newline in
        one filename is the reason every step here is NUL-delimited.
        """
        script = self.dirty_repo(tmp_path)
        index_before = git(tmp_path, "write-tree")
        env, log = fake_docker(tmp_path)

        result = run("new", "snapshot-case", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0, "the stubbed setup should have stopped new"
        snapshot = self.snapshot_of(tmp_path, log)
        names = set(
            subprocess.check_output(
                ["git", "-C", str(tmp_path), "ls-tree", "-rz", "--name-only", snapshot]
            ).split(b"\0")
        )
        assert b"staged.txt" in names
        assert b"unstaged.txt" in names
        assert "untracked\nname.txt".encode() in names, "an untracked path was left behind"
        assert b"deleted.txt" not in names, "a deleted path survived into the snapshot"
        assert b"ignored-secret" not in names, "an ignored path reached the chamber"
        assert git(tmp_path, "show", f"{snapshot}:unstaged.txt") == "unstaged"
        assert git(tmp_path, "write-tree") == index_before, "the real index was disturbed"
        assert "Co-Authored-By: autoclave <autoclave@localhost>" in git(
            tmp_path, "show", "-s", "--format=%B", snapshot
        ), "commit-tree runs no hooks, so the trailer has to be written by hand"
        # Against the runnable lines, not the raw source: the comment beside the
        # plumbing quotes the rule it is obeying, and a substring search over the whole
        # file counts that as the violation.
        assert not any("git add -A" in line for line in code_lines()), (
            "the bulk staging command CLAUDE.md forbids is back in the launcher"
        )

    def test_the_setup_script_is_not_expanded_on_the_host(self, tmp_path):
        """It crosses as stdin under a quoted here-document, values as environment.

        As one interpolated argument, the host shell expanded the whole block first —
        comments included — and a backtick in one of those comments ran on this machine
        once already. The recorded script is read back here: the variable must still be
        a variable, and the commit it stands for must appear nowhere in the text.
        """
        script = self.dirty_repo(tmp_path)
        env, log = fake_docker(tmp_path)
        result = run("new", "setup-case", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "setup failed" in result.stderr, result.stderr

        crossed = Path(env["FAKE_SETUP_SCRIPT"]).read_text()
        snapshot = self.snapshot_of(tmp_path, log)
        assert '"$AC_BASE_SHA"' in crossed, "the base commit was interpolated on the host"
        assert snapshot not in crossed, "the host expanded the value into the script"
        assert '"agent/$AC_TASK"' in crossed
        # The first line proves the here-document was not opened before the command
        # was finished: written `<<'X' ||` with a `die` on the next line, that `die`
        # becomes line one of what the container runs.
        assert crossed.strip().splitlines()[0].strip() == "set -eu", crossed[:200]
        assert "git config core.hooksPath .githooks" in crossed, (
            "a fresh clone has every git-hook rule off, commit-msg among them"
        )
        assert subprocess.run(["sh", "-n"], input=crossed, text=True).returncode == 0, (
            "the script handed to the chamber is not valid POSIX sh"
        )

    def test_a_failed_setup_stops_and_says_where_to_look(self, tmp_path):
        """It must refuse, on its own account, and name the container to inspect.

        Asserted against stderr rather than against the run merely stopping: with the
        here-document written the other way the refusal is swallowed and what stops
        `new` is the *next* command failing, which is not the same thing and would not
        survive that command being fixed.
        """
        script = self.dirty_repo(tmp_path)
        env, _log = fake_docker(tmp_path)
        result = run("new", "setup-case", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "setup failed" in result.stderr, result.stderr
        assert "docker logs verbatus-ac-setup-case" in result.stderr
        assert "is up" not in result.stdout, result.stdout


class TestSignInIsAsked:
    """A volume is where a sign-in lands. It is not evidence that one finished.

    `login` creates the volume before the sign-in runs, so an abandoned or failed
    attempt left one behind — and everything downstream read that empty volume as a
    credential: `doctor` said "signed in", `new <task> <vendor>` labelled a chamber
    for a credential it never mounted, and the only symptom was an authentication
    failure from inside a vendor CLI two layers down.

    The vendor is asked instead. `claude auth status` and `codex login status` both
    exit 0 signed in and non-zero signed out — checked against `--help` on both CLIs,
    and against `claude auth status` run with and without a sign-in present.
    """

    def test_a_present_volume_is_not_called_a_sign_in(self, tmp_path):
        script = elsewhere(tmp_path)
        env, _log = fake_docker(tmp_path)
        env["FAKE_VOLUMES"] = "verbatus-ac-auth-claude verbatus-ac-auth-codex"
        result = run("doctor", env=env, script=script)
        assert result.returncode == 0
        assert "signed in (" not in result.stdout, "a volume alone was reported as a sign-in"
        assert result.stdout.count("not signed in") == 2

    def test_a_vendor_that_answers_yes_is_reported_signed_in(self, tmp_path):
        """The positive control: without it the test above passes for a `doctor`
        that can never say anything but no."""
        script = elsewhere(tmp_path)
        env, _log = fake_docker(tmp_path)
        env["FAKE_VOLUMES"] = "verbatus-ac-auth-claude verbatus-ac-auth-codex"
        env["FAKE_AUTH_VALID"] = "1"
        result = run("doctor", env=env, script=script)
        assert result.returncode == 0
        assert result.stdout.count("signed in (") == 2
        assert "not signed in" not in result.stdout

    def test_doctor_says_unknown_rather_than_guessing_without_an_engine(self):
        """`doctor` is the first thing anyone runs and must answer on a bare machine.

        With nothing to ask, the honest answer is neither yes nor no.
        """
        result = run("doctor")
        assert result.returncode == 0
        assert result.stdout.count("unknown") == 2, result.stdout

    def test_an_incomplete_login_is_reported_and_takes_its_volume_with_it(self, tmp_path):
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        result = run("login", "codex", env=env, script=script)
        assert result.returncode != 0
        assert "did not complete" in result.stderr
        assert ["volume", "rm", "verbatus-ac-auth-codex"] in docker_calls(log), (
            "an empty volume this call created was left behind to be read as a sign-in"
        )

    def test_a_login_leaves_a_volume_it_did_not_create_alone(self, tmp_path):
        """Whether this call created the volume decides whether it may remove it."""
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_VOLUMES"] = "verbatus-ac-auth-codex"
        result = run("login", "codex", env=env, script=script)
        assert result.returncode != 0
        assert not any(call[:2] == ["volume", "rm"] for call in docker_calls(log))

    def test_a_completed_login_keeps_its_volume(self, tmp_path):
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_AUTH_VALID"] = "1"
        env["FAKE_VOLUMES"] = "verbatus-ac-auth-codex"
        result = run("login", "codex", env=env, script=script)
        assert result.returncode == 0, result.stderr
        assert "reports itself signed in" in result.stdout
        assert not any(call[:2] == ["volume", "rm"] for call in docker_calls(log))

    def test_new_refuses_a_vendor_that_is_not_signed_in(self, tmp_path):
        """And refuses before it writes anything: the chamber label would otherwise
        claim a credential the container never received."""
        script = elsewhere(tmp_path)
        (tmp_path / "loose.txt").write_text("uncommitted work\n")
        env, log = fake_docker(tmp_path)
        env["FAKE_VOLUMES"] = "verbatus-ac-auth-claude"
        result = run("new", "some-task", "HEAD", "claude", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0
        assert "not signed in" in result.stderr
        # `authenticated` legitimately runs a throwaway container to ask the CLI, so
        # the assertion is about the *chamber*: nothing was detached, and nothing was
        # labelled for a vendor whose credential is not there.
        assert not any("--detach" in call for call in docker_calls(log)), (
            "a chamber was started for a vendor that is not signed in"
        )
        assert not any("verbatus.vendor=claude" in call for call in docker_calls(log))

    def test_dispatch_asks_inside_the_chamber_not_of_the_label(self, tmp_path):
        """A chamber outlives the check `new` made: a token expires, a vendor forces
        a re-auth, an earlier agent rewrites the shared configuration directory. The
        label says which credential was mounted, not whether it still works."""
        script = elsewhere(tmp_path)
        brief = tmp_path / "brief.md"
        brief.write_text("bounded task\n")
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_VENDOR": "codex",
                "FAKE_VOLUMES": "verbatus-ac-auth-codex",
            }
        )
        result = run(
            "dispatch",
            "task-x",
            "codex",
            str(brief),
            "gpt-5.6-luna",
            "low",
            env=env,
            script=script,
            cwd=tmp_path,
        )
        assert result.returncode != 0
        assert "not signed in inside chamber" in result.stderr
        asked = [
            call
            for call in docker_calls(log)
            if call[:1] == ["exec"] and "login status" in " ".join(call)
        ]
        assert asked, "dispatch never asked the CLI inside the chamber"


def test_report_names_the_path_it_looked_for():
    """A missing report says where it looked, so the operator can go and see."""
    result = run("report", "no-such-task")
    assert result.returncode != 0
    assert "no-such-task" in result.stderr
    assert "report.md" in result.stderr


def test_output_root_is_inside_the_gitignored_workbench():
    """Nothing an agent produces may show up in `git status`.

    The drawer is under `workbench/`, which is gitignored, and this asserts the
    launcher agrees with that rather than trusting the comment.
    """
    result = run("doctor")
    assert "workbench/autoclave" in result.stdout


def test_the_brief_is_not_named_claude_md_on_the_host():
    """A `CLAUDE.md` here would be read by a host session working in this tree
    and would tell it, falsely, that it is inside a container. The Dockerfile
    renames the file on the way into the image, which is the only place the
    brief's claims are true."""
    directory = ROOT / "operations" / "autoclave"
    assert (directory / "agent-brief.md").is_file()
    assert not (directory / "CLAUDE.md").exists()


def test_dockerignore_denies_everything_before_admitting_anything():
    """The deny-all form is load-bearing: `private/` holds the notification
    topic, and an exclusion list goes stale the day a new drawer is added."""
    lines = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines[0] == "*", "the first rule must deny everything"
    assert all(line.startswith("!") for line in lines[1:]), (
        "after the deny-all, every rule must be a re-admission"
    )
    admitted = {line[1:] for line in lines[1:]}
    assert "operations/autoclave/agent-brief.md" in admitted
    assert not any(entry.startswith("private") for entry in admitted)
