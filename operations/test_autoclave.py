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
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from operations.autoclave.fingerprint import INPUTS as FINGERPRINT_INPUTS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "operations" / "autoclave" / "autoclave.sh"
SAFE_FILE = ROOT / "operations" / "autoclave" / "safe_file.py"
BRIEF = ROOT / "operations" / "autoclave" / "agent-brief.md"
FINGERPRINT = ROOT / "operations" / "autoclave" / "fingerprint.py"
# The single declaration of what the image bakes in. `elsewhere` stages exactly these so
# `fingerprint.py` can run in a throwaway repository; keeping a second list here is what
# `test_the_declared_inputs_cover_every_baked_file` exists to make unnecessary.
IMAGE_INPUTS = FINGERPRINT_INPUTS


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


def assert_no_incoming_ref(repo):
    """No ref anywhere under the incoming namespace, whatever the launcher named it.

    Asking for one guessed name proves nothing: the launcher writes
    `refs/autoclave/incoming/<task>-<nonce>`, so `rev-parse --verify` on a fixed
    `.../task-x` returns non-zero no matter what happened. A stray ref keeps a fetched
    chamber commit reachable, which is what lets `rm` believe the work is safely in this
    repository and destroy the container -- the output then survives only under a ref
    nobody reads. The namespace is the thing to ask about.
    """

    leaked = git(repo, "for-each-ref", "--format=%(refname)", "refs/autoclave/incoming/")
    assert leaked == "", f"a refused collection left an incoming ref behind: {leaked}"


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
    assert SAFE_FILE.is_file(), f"required launcher input is missing: {SAFE_FILE}"
    shutil.copy2(SAFE_FILE, directory / SAFE_FILE.name)
    # `new` compares this file against what the image carries, so a throwaway
    # repository without it would fail every chamber creation for the wrong reason.
    assert BRIEF.is_file(), f"required launcher input is missing: {BRIEF}"
    shutil.copy2(BRIEF, directory / BRIEF.name)
    # Derived from the one declaration rather than listed again. `fake_docker` runs
    # `fingerprint.py` inside this throwaway repository with `check=True`, so every path
    # in `INPUTS` has to exist here. A second hand-maintained copy drifts the moment
    # somebody bakes in a new file, and the whole suite then fails at once inside a
    # subprocess traceback that sends the reader debugging the harness, not their change.
    for relative in IMAGE_INPUTS:
        source = ROOT / relative
        assert source.is_file(), f"required launcher input is missing: {source}"
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source != destination:
            shutil.copy2(source, destination)
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
    git(tmp_path, "add", "operations", "requirements-dev.txt", ".dockerignore")
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
import time

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")


def setting(name, default=""):
    return os.environ.get(name, default)


def vendor_status(command):
    return "auth status" in command or "login status" in command


hold_on = setting("FAKE_HOLD_ON")
if hold_on and hold_on in " ".join(args):
    ready = setting("FAKE_HOLD_READY")
    if ready:
        open(ready, "w").close()
    release = setting("FAKE_HOLD_RELEASE")
    if not release:
        # A hold with no release file used to fall straight through: the stub returned at
        # once, the second dispatch found no lock held, and the tests proving two agents
        # cannot occupy one chamber passed on a race they never staged.
        sys.stderr.write("fake docker: a hold was requested with no release file\\n")
        raise SystemExit(126)
    deadline = time.monotonic() + 60
    while not os.path.exists(release):
        if time.monotonic() >= deadline:
            sys.stderr.write(f"fake docker: hold was never released: {release}\\n")
            raise SystemExit(126)
        time.sleep(0.01)


def answer_vendor_status():
    # `authenticated()` asks through a stdout sentinel so that "the vendor says no"
    # and "the container never ran" are different answers. FAKE_AUTH_ASKABLE=0 is
    # the second one: nothing printed, non-zero exit.
    if setting("FAKE_AUTH_ASKABLE", "1") != "1":
        raise SystemExit(125)
    print("ac-auth:" + ("0" if setting("FAKE_AUTH_VALID") == "1" else "1"), end="")
    raise SystemExit(0)


if args[:1] == ["info"]:
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    if setting("FAKE_IMAGE_EXISTS", "1") != "1":
        raise SystemExit(1)
    if "{{.Id}}" in " ".join(args):
        print(setting("FAKE_IMAGE_ID", "sha256:" + "1" * 64))
    elif "verbatus.harness-fingerprint" in " ".join(args):
        print(setting("FAKE_IMAGE_FINGERPRINT"))
    raise SystemExit(0)
if args[:2] == ["buildx", "version"]:
    raise SystemExit(int(setting("FAKE_BUILDX_STATUS", "0")))
def volumes():
    # Preset volumes plus any this run has created and not removed. Without the
    # second half, `login`'s first-time path could never be reached: it creates the
    # volume and every later `has_volume` said it was still absent.
    live = set(setting("FAKE_VOLUMES").split())
    ledger = setting("FAKE_VOLUME_LEDGER")
    if ledger and os.path.exists(ledger):
        for line in open(ledger):
            action, _, name = line.strip().partition(" ")
            live.add(name) if action == "create" else live.discard(name)
    return live


if args[:2] == ["volume", "inspect"]:
    raise SystemExit(0 if args[-1] in volumes() else 1)
if args[:2] in (["volume", "create"], ["volume", "rm"]):
    ledger = setting("FAKE_VOLUME_LEDGER")
    if ledger:
        with open(ledger, "a") as book:
            book.write(args[1] + " " + args[-1] + "\\n")
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
    if "refresh_claude_token.py" in " ".join(args):
        raise SystemExit(int(setting("FAKE_REFRESH_STATUS", "0")))
    if vendor_status(args[-1]):
        answer_vendor_status()
    raise SystemExit(int(setting("FAKE_LOGIN_STATUS", "0")))
if args[:1] == ["exec"]:
    if args[-2:] == ["sh", "-s"]:
        with open(setting("FAKE_SETUP_SCRIPT", os.devnull), "w") as recorded:
            recorded.write(sys.stdin.read())
        raise SystemExit(int(setting("FAKE_SETUP_STATUS", "1")))
    command = args[-1]
    if vendor_status(command):
        raise SystemExit(0 if setting("FAKE_AUTH_VALID") == "1" else 1)
    if "refresh_claude_token.py" in command:
        raise SystemExit(int(setting("FAKE_REFRESH_STATUS", "0")))
    if "git config user.name" in command:
        # The author stamp, answered on its own so it does not consume
        # `FAKE_EXEC_STATUS` — the fallback below is how tests stage a failing
        # *CLI* invocation, and a stamp indistinguishable from that would fail
        # dispatch before the CLI it exists to attribute ever ran.
        raise SystemExit(int(setting("FAKE_STAMP_STATUS", "0")))
    chamber = setting("FAKE_CHAMBER_WORK")
    if chamber and "cd /work" in command:
        command = command.replace("cd /work", "cd " + chamber)
        command = command.replace(
            "/src/operations/autoclave/safe_file.py", setting("FAKE_SAFE_FILE")
        )
        outdir = setting("FAKE_OUT_DIR")
        if outdir:
            command = command.replace("/out/", outdir + "/")
        status = subprocess.run(["sh", "-c", command]).returncode
        # A background process left running in the container, replacing a slot the
        # host is about to read. That is the race `collect` has to survive, and it
        # cannot be staged from outside the container any other way.
        planted = setting("FAKE_PLANT_AFTER_EXEC")
        if planted and ("bundle create" in command or "safe_file.py bundle" in command):
            target, _, content = planted.partition("=")
            with open(target, "w") as slot:
                slot.write(content)
        raise SystemExit(status)
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
    fingerprint_root = tmp_path if (tmp_path / ".dockerignore").is_file() else ROOT
    fingerprint = subprocess.run(
        ["python3", str(fingerprint_root / "operations" / "autoclave" / "fingerprint.py")],
        check=True,
        capture_output=True,
        text=True,
        cwd=fingerprint_root,
    ).stdout.strip()
    return {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_IMAGE_FINGERPRINT": fingerprint,
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_HOST_VENDOR_LOG": str(host_vendor_log),
        "FAKE_SETUP_SCRIPT": str(tmp_path / "setup.sh"),
        "FAKE_SAFE_FILE": str(tmp_path / "operations" / "autoclave" / "safe_file.py"),
        "FAKE_VOLUME_LEDGER": str(tmp_path / "volumes.log"),
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


def _mount_operands(runnable):
    """Every `(source, destination, options)` the launcher passes as a bind mount.

    Read out of the text rather than matched against it: `--volume` and `-v` are the
    same flag, the operand may be quoted or not, and the options field may be absent
    entirely. A test that asserts one exact spelling is absent proves nothing about
    the three spellings it did not name.
    """
    operands = []
    for match in re.finditer(r"(?:--volume|--mount|-v)[= ]+(\"?)([^\s\"]+)\1", runnable):
        fields = match.group(2).split(":")
        if len(fields) < 2:
            continue
        source, destination = fields[0], fields[1]
        options = fields[2].split(",") if len(fields) > 2 else []
        operands.append((source, destination, options))
    assert operands, "no bind mounts were parsed out of the launcher at all"
    return operands


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

    def test_a_drawer_holding_a_report_refuses_a_new_chamber(self, tmp_path):
        """Reusing a task name would overwrite the only evidence a dispatch ran.

        `rm` keeps the output drawer deliberately — it is the sole surviving record
        of a chamber. So a second `new` under the same task name silently destroys
        the first one's report the moment the new seat finishes, and `workbench/` is
        gitignored, so there is no history to recover it from.

        This happened on 2026-08-04: a four-seat review was dispatched into
        `rev-terra`, `rev-sonnet`, `rev-opus` and `rev-sol`, the names a different
        review had used the night before, and GPT-5.6 Terra's System 02 review report
        was destroyed. The other three survived only because their seats had not
        finished writing when it was noticed. See
        `workbench/archive/2026-08-03_reviews-preserved/LOST.md`.

        It refuses rather than warns, per CLAUDE.md hard rule 7: a warning printed at
        the top of a dispatch that then runs for two hours is a warning nobody reads.
        Asserted to happen before the engine is touched, so the refusal cannot leave
        a half-made chamber behind.
        """
        script = elsewhere(tmp_path)
        drawer = tmp_path / "workbench" / "autoclave" / "a-finished-review"
        drawer.mkdir(parents=True)
        (drawer / "report.md").write_text("an earlier seat's findings\n")
        # A *working* engine, deliberately. Reading the refusal text cannot tell a
        # launcher that never called Docker apart from one that called it and then
        # refused for its own reasons — and on a host with the image already built,
        # the second is what used to happen. The stub records every invocation, so
        # "the engine was not touched" is asserted against what was called rather
        # than against what the error happened to say.
        env, log = fake_docker(tmp_path)

        result = run("new", "a-finished-review", script=script, env=env)

        assert result.returncode != 0
        assert "report.md" in result.stderr
        assert "a-finished-review" in result.stderr
        # The old report is still there, untouched.
        assert (drawer / "report.md").read_text() == "an earlier seat's findings\n"
        # And nothing reached the engine — no Docker call of any kind was made.
        assert docker_calls(log) == []

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
        # Both volumes reachable from one code path is the shape of the old defect,
        # and this is what says so. It used to count the substring `AUTH_VOL_CLAUDE:-`
        # and require zero of them — a spelling that appears nowhere in this script and
        # appeared nowhere in the defective version either, so the assertion was
        # `"00" == "00"` for every implementation there has ever been, including one
        # that mounts both vendors from a single branch. The structure is what matters:
        # the mount is built in exactly two places, one per vendor, and neither names
        # both.
        mounting = [ln for ln in code_lines() if 'auth_mounts="--volume' in ln]
        assert len(mounting) == 2, f"expected one mount line per vendor, found {len(mounting)}"
        for line in mounting:
            assert not ("CLAUDE" in line and "CODEX" in line), (
                f"a single line mounts both vendors: {line.strip()}"
            )
        assert sum("CLAUDE" in line for line in mounting) == 1
        assert sum("CODEX" in line for line in mounting) == 1

    def test_the_mountpoints_are_created_before_the_bind(self):
        """All three masked drawers are gitignored, so a fresh clone has none of
        them. Docker builds a missing mountpoint inside the bind, `/src` is
        read-only, and `docker run` exits 125 — `new` fails outright on any machine
        but this one. Verified before this line existed.
        """
        runnable = "\n".join(code_lines())
        created = runnable.index('mkdir -p "${REPO_ROOT}/private"')
        assert created < runnable.index("--tmpfs /src/private"), "mountpoints made too late"

    def test_the_private_drawer_is_masked_inside_a_chamber(self):
        """`/src` read-only stops an agent changing this machine, not reading it.

        `private/` holds the notification bearer topic, and a chamber has open
        network egress, so a readable secret is a sendable one. The mount must be
        an empty tmpfs over that one path — not an exclusion from the bind, which
        Docker cannot express, and not deletion of the directory, which `/src`
        being read-only forbids anyway.

        Read against the runnable lines and not the raw source, because the old
        assertion searched the whole file: a commented-out `--tmpfs` line still
        contains `--tmpfs /src/private:ro`, so the mask could be switched off in the
        one spelling anybody would actually use and this stayed green. (Deleting the
        three lines outright did fail it. Commenting them out did not, and that is the
        edit a person makes.)
        """
        runnable = " ".join(code_lines())
        # `workbench/` is the other drawer the guard's own SECRET_DRAWERS names, and
        # it holds every handoff, note and reviewer transcript. Masking `private/`
        # alone left half the rule unenforced.
        for drawer in ("private", "workbench", "scriptorium"):
            assert f"--tmpfs /src/{drawer}:ro" in runnable, f"{drawer} is exposed to every chamber"

    def test_the_specs_reach_a_chamber_and_the_chamber_may_write_them(self):
        """`workbench/design/` is gitignored *and* inside the masked drawer, so the
        specs were absent from a chamber twice over and a brief had to carry a whole
        spec as prose. They arrive as their own mount.

        Writable, and the asymmetry with `/src` and `/window` is the point: those two
        are really this machine's tree and really the old repository, so writing them
        would change the host with no diff. This is a per-chamber copy `rm` deletes.
        It was read-only for one sitting and that cost a dispatch — a builder told by
        its spec to record resolved revisions in the bench memo could not.
        """
        runnable = " ".join(code_lines())
        assert '--volume "${specs_stage}:/specs"' in runnable, "specs are not mounted"
        assert "${specs_stage}:/specs:ro" not in runnable, "the specs mount is read-only again"

    def test_the_staged_specs_are_a_frozen_copy_not_a_live_bind(self):
        """Same reason `/src` is a snapshot: a session repairing a spec while a
        chamber reads it would change the tree underneath the agent. The stage is
        copied at `new`, so `/specs` cannot move while the chamber lives.
        """
        runnable = "\n".join(code_lines())
        assert 'cp -R "${REPO_ROOT}/workbench/design/."' in runnable, "specs are not copied"
        copied = runnable.index('cp -R "${REPO_ROOT}/workbench/design/."')
        assert copied < runnable.index("${specs_stage}:/specs"), "staged after the bind"

    def test_the_window_onto_the_old_code_is_open_by_default_and_read_only(self):
        """**Tyrel ruled 2026-08-04 that the window is on by default**, reversing the
        opt-in rule this test used to assert. His evidence: four seats built System 03
        with no window and one decided to refuse PDFs outright, while the old pipeline
        had taken PDFs at stage one all along. An opt-in window is one every session
        must remember, and the session that forgot cost a night.

        The copying risk the old rule guarded against is unchanged and is still
        controlled the same way — the operator reads every line of the diff. What is
        mechanical here is that neither mount can ever be writable.
        """
        runnable = "\n".join(code_lines())
        assert "window.conf" in runnable, "the window is not configured from window.conf"
        assert ":/window:ro" in runnable, "the old-code window is not mounted read-only"
        assert ":/stage:ro" in runnable, "the audit-notes window is not mounted read-only"
        assert ":/window:rw" not in runnable, "the window is writable"
        assert ":/stage:rw" not in runnable, "the stage window is writable"

    def test_the_old_repository_is_admitted_by_named_directory_never_whole(self):
        """The old repository is 5.5 GB and holds roughly 31,500 real register images
        plus months of dated notes, and a chamber has open network egress — a readable
        image is a sendable one.

        **The control is that the root is never mounted.** `window.conf` names the
        directories that may cross and the launcher mounts each one individually, so a
        drawer that appears in the old repository tomorrow is absent from the airlock
        until somebody names it. An exclusion list would have the opposite default and
        would admit it silently — the same reasoning that makes the repository's own
        `.dockerignore` deny everything and re-admit two files by name.

        A control that is merely configured is not a control that holds; what proves it
        is scanning the mounts from inside a chamber, which no static test can do. The
        chamber-side scan is recorded in the commit that added this.
        """
        runnable = "\n".join(code_lines())
        assert "${WINDOW_OCR_ROOT}/${wdir}:/window/${wdir}:ro" in runnable, (
            "the old repository is not mounted directory by directory"
        )
        # Parsed, not string-matched. The negative assertion used to be one exact
        # substring, so `-v ${WINDOW_OCR_ROOT}:/window`, a quoted operand, or one
        # written without `:ro` all passed it while mounting the whole 5.5 GB root.
        # Every mount operand naming WINDOW_OCR_ROOT or WINDOW_STAGE is read out and
        # judged on its source, destination and options instead.
        for source, destination, options in _mount_operands(runnable):
            if source not in {"${WINDOW_OCR_ROOT}", "${WINDOW_STAGE}"}:
                continue
            assert destination != "/window", (
                f"the old repository root is mounted whole at /window ({source})"
            )
            assert "ro" in options, (
                f"the window mount {source}:{destination} is not read-only ({options})"
            )
        # The override remains supported and is the one place a whole root may be
        # named, because it is an operator's deliberate one-off rather than a default.
        assert "${AUTOCLAVE_WINDOW}:/window:ro" in runnable, (
            "the AUTOCLAVE_WINDOW override no longer mounts read-only"
        )
        assert "--tmpfs /stage/${masked}:ro" in runnable, "the stage probes are not masked"
        assert "$window_masks" in runnable, "the mask flags never reach docker run"

    def test_the_stage_mount_does_not_clobber_the_window_mount(self):
        """Both flag sets share one variable, and `/stage` was assigned into it rather
        than appended — so every `/window` flag built above was silently discarded and
        the chamber came up with `/stage` present, `/window` absent, and a log line
        still saying the airlock was open.

        Caught by inspecting a real container's mounts rather than by reading the
        script, which is the only way this class of bug shows itself.
        """
        runnable = "\n".join(code_lines())
        assert 'window_mount="--volume ${WINDOW_STAGE}:/stage:ro"' not in runnable, (
            "the stage mount overwrites the window mount instead of appending"
        )
        assert 'window_mount="${window_mount} --volume ${WINDOW_STAGE}:/stage:ro"' in runnable

    def test_the_window_path_cannot_forge_a_volume_spec(self):
        """`window_mount` is word-split into the flag list, and Docker's short volume
        form is `src:dst:opts`. So the two characters that could make one path read as
        several fields — whitespace and a colon — are refused before the flag is built.
        A path with a colon in it produced an invalid-volume-specification error naming
        a mount the operator never wrote.
        """
        runnable = "\n".join(code_lines())
        assert "*[[:space:]]*) die " in runnable, "whitespace is not refused"
        assert "*:*) die " in runnable, "a colon is not refused"

    def test_an_absent_window_mounts_nothing(self):
        """The flags are empty when the configured paths are not on this machine, and
        they word-split into the `docker run` line beside the auth mounts. An empty
        string must contribute no argument at all — a stray quote here would pass Docker
        an empty operand.

        This is what keeps `window.conf`'s machine-specific absolute paths harmless in a
        clone somewhere else: the directory is missing, the flag stays empty, and the
        chamber starts without a window rather than failing to start at all.
        """
        runnable = " ".join(code_lines())
        assert "$auth_mounts $window_mount $window_masks \\" in runnable, (
            "a window flag is quoted or absent"
        )
        assert 'window_mount=""' in runnable, "the window flag has no empty default"
        assert 'window_masks=""' in runnable, "the mask flag list has no empty default"
        assert "is not on this machine" in runnable, "a missing window path is not reported"


class TestDispatch:
    """Running an agent inside a chamber, against a brief written to a file."""

    # `.claude/agents/README.md`'s dispatch table records that `minimal` is
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
        """Both bypass flags are correct inside a container and nowhere else, because
        the container is the boundary and a prompt inside a detached one is a hang.
        Each must appear in exactly one runnable line, and the docker call that line
        belongs to must be an `exec`.

        The *nearest preceding* docker call is what decides, not whether one appears
        anywhere above. Written the loose way, `cmd_shell` guarantees a `docker exec` a
        hundred lines up, so a host invocation added later carried the flag and still
        passed.

        **This still does not close it, and the next test is what does.** Moving both
        invocations onto the host was tried against this assertion and it stayed green,
        because the auth check immediately above them is itself a `docker exec` and
        becomes the nearest one. What this pins is that each flag is written once; that
        it reaches a container is an outcome, and is tested as one below.
        """
        lines = code_lines()
        for flag in (
            "--dangerously-skip-permissions",
            "--dangerously-bypass-approvals-and-sandbox",
        ):
            carrying = [ln for ln in lines if flag in ln]
            assert len(carrying) == 1, f"expected one runnable use of {flag}, found {len(carrying)}"
            index = lines.index(carrying[0])
            before = [ln for ln in lines[:index] if "docker " in ln]
            assert before, f"{flag} is not inside any docker call"
            assert "docker exec" in before[-1], (
                f"the docker call {flag} belongs to is not an exec: {before[-1].strip()}"
            )

    def test_the_bypass_flags_never_reach_a_host_binary(self, tmp_path):
        """And the same thing as an outcome, because the reading above is a reading.

        Failing `claude` and `codex` stubs sit on the dispatch's PATH. If either
        invocation drifted onto the host it would run one of them, and the attempt
        would be recorded. The recorded docker argv is what carries the flag.
        """
        script = elsewhere(tmp_path)
        brief = tmp_path / "brief.md"
        brief.write_text("bounded task\n")
        drawer = tmp_path / "workbench" / "autoclave" / "task-x"
        drawer.mkdir(parents=True)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_VENDOR": "codex",
                "FAKE_AUTH_VALID": "1",
                "FAKE_EXEC_STATUS": "0",
            }
        )

        result = run(
            "dispatch",
            "task-x",
            "codex",
            str(brief),
            "gpt-5.6-luna",
            "high",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode == 0, result.stderr
        carrying = [
            call
            for call in docker_calls(log)
            if "--dangerously-bypass-approvals-and-sandbox" in " ".join(call)
        ]
        assert len(carrying) == 1 and carrying[0][0] == "exec"
        assert not Path(env["FAKE_HOST_VENDOR_LOG"]).exists(), "a vendor CLI ran on the host"

    def test_a_vendor_failure_is_the_dispatch_exit_status(self, tmp_path):
        script = elsewhere(tmp_path)
        brief = tmp_path / "brief.md"
        brief.write_text("bounded task\n")
        (tmp_path / "workbench" / "autoclave" / "task-x").mkdir(parents=True)
        env, _log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_VENDOR": "codex",
                "FAKE_AUTH_VALID": "1",
                "FAKE_EXEC_STATUS": "7",
            }
        )

        result = run(
            "dispatch",
            "task-x",
            "codex",
            str(brief),
            "gpt-5.6-luna",
            "medium",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode == 7
        assert "the CLI exited 7" in result.stdout

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
        """Both reach the container as environment variables, but a value beginning
        with `-` would still arrive at the CLI as a flag."""
        result = run("dispatch", "task-x", "claude", str(ROOT / "README.md"), model, effort)
        assert result.returncode != 0
        assert expected in result.stderr

    def test_validation_precedes_any_docker_call(self, tmp_path):
        """A typo in a model name should cost a line of output, not a container's
        startup and a confusing failure from a vendor CLI two layers down."""
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        result = run(
            "dispatch",
            "task-x",
            "claude",
            str(ROOT / "README.md"),
            "-evil",
            env=env,
            script=script,
            cwd=tmp_path,
        )
        assert result.returncode != 0
        assert "not a plain model name" in result.stderr
        assert docker_calls(log) == []

    def test_both_vendors_receive_model_and_effort_in_their_own_spelling(self):
        """The two CLIs spell effort differently and neither spelling is guessable.
        `claude` takes `--effort <level>`; `codex exec` has no effort flag at all and
        needs the config override `-c model_reasoning_effort=`. Checked against
        `--help` on both rather than assumed."""
        joined = " ".join(code_lines())
        assert '--model "$AC_MODEL"' in joined and '--effort "$AC_EFFORT"' in joined
        assert '-m "$AC_MODEL"' in joined
        assert '-c "model_reasoning_effort=$AC_EFFORT"' in joined

    def test_the_model_travels_as_an_environment_variable(self):
        """Same reasoning as the brief travelling as a file: a value interpolated into
        a quoted command line brings its punctuation with it."""
        joined = " ".join(code_lines())
        assert joined.count('-e AC_MODEL="$model" -e AC_EFFORT="$effort"') == 2

    def test_every_dispatch_path_stamps_the_model_as_author_first(self):
        """`new` sets a placeholder git identity — `autoclave <autoclave@localhost>`
        — because the chamber is created before any model is chosen. Left alone
        that placeholder becomes the author of everything the agent commits. Tyrel
        ruled on 2026-08-11 that autoclave itself should never be the author of any
        code, so the model has to be stamped as the identity at the first moment it
        is known: dispatch, before either vendor's CLI runs.

        Checked structurally, against every `docker exec -e AC_MODEL="$model"`
        dispatch line found in the source, rather than against the two arms named
        today — so a vendor arm added later without the call is caught the same
        way. The two behavioural tests below prove the stamp actually *executes*
        before the CLI and carries the right address; this one exists so a third
        arm cannot be added without one.
        """
        lines = SCRIPT.read_text().splitlines()
        dispatch_lines = [
            i for i, line in enumerate(lines) if 'docker exec -e AC_MODEL="$model"' in line
        ]
        assert dispatch_lines, "no dispatch invocation found to check"
        for i in dispatch_lines:
            preceding = lines[max(0, i - 16) : i]
            assert any('stamp_author "$task" "$vendor" "$model"' in line for line in preceding), (
                f"line {i + 1} dispatches the CLI with no preceding stamp_author call"
            )

    def _stamped_dispatch_calls(self, tmp_path, vendor, model, cli_marker):
        """Run one successful dispatch and return (stamp call, CLI call, all calls)."""
        script = elsewhere(tmp_path)
        brief = tmp_path / "brief.md"
        brief.write_text("bounded task\n")
        (tmp_path / "workbench" / "autoclave" / "task-x").mkdir(parents=True)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_VENDOR": vendor,
                "FAKE_AUTH_VALID": "1",
                "FAKE_REFRESH_STATUS": "0",
                "FAKE_EXEC_STATUS": "0",
            }
        )

        result = run(
            "dispatch",
            "task-x",
            vendor,
            str(brief),
            model,
            "medium",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode == 0, result.stderr
        calls = [call for call in docker_calls(log) if call[:1] == ["exec"]]
        stamp = next(call for call in calls if "git config user.name" in " ".join(call))
        cli = next(call for call in calls if cli_marker in " ".join(call))
        return stamp, cli, calls

    def test_a_claude_dispatch_stamps_the_model_and_anthropic_address_before_the_cli(
        self, tmp_path
    ):
        """The stamp is asserted as an executed command, not as source text: it names
        the dispatched model, carries the Anthropic no-reply address `.githooks/
        commit-msg` documents, and runs before the CLI it attributes."""
        stamp, cli, calls = self._stamped_dispatch_calls(
            tmp_path, "claude", "sonnet", "--append-system-prompt-file"
        )
        joined = " ".join(stamp)
        assert "user.name 'sonnet'" in joined
        assert "user.email 'noreply@anthropic.com'" in joined
        assert calls.index(stamp) < calls.index(cli), "the CLI ran before the author stamp"

    def test_a_codex_dispatch_stamps_the_model_and_openai_address_before_the_cli(self, tmp_path):
        """The vendor argument, not the model's spelling, chooses the address — a
        name pattern would misattribute the first model whose spelling crosses
        vendors."""
        stamp, cli, calls = self._stamped_dispatch_calls(
            tmp_path, "codex", "gpt-5.6-luna", "--dangerously-bypass-approvals-and-sandbox"
        )
        joined = " ".join(stamp)
        assert "user.name 'gpt-5.6-luna'" in joined
        assert "user.email 'noreply@openai.com'" in joined
        assert calls.index(stamp) < calls.index(cli), "the CLI ran before the author stamp"

    def test_a_claude_chamber_has_no_background_wait_ceiling(self):
        """The default ceiling is a real kill, and it silently cost a lane.

        In `-p` mode the Claude CLI waits a bounded time for its own background
        tasks and then terminates them and exits. An orchestrating seat fans out —
        that is the point of `ultracode` — so it reaches the ceiling as a matter of
        course. A round-two lane spawned a 28-agent audit, hit the 600-second
        default, killed its own workflow and committed nothing; the dispatch exited
        0 and `collect` said NO COMMITS, so a harness kill looked like a model that
        did no work.

        `.claude/agents/README.md` names this exact shape: a real kill is not a
        deadline, and a killed seat loses its report entirely rather than handing
        back a shorter one. Zero means wait indefinitely. Asserted rather than
        trusted, because nothing else in the dispatch would notice its removal —
        the failure it prevents is silent by construction.
        """
        joined = " ".join(code_lines())
        assert "-e CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0" in joined

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


def test_build_requires_buildx_and_loads_a_fingerprinted_image(tmp_path):
    script = elsewhere(tmp_path)
    env, log = fake_docker(tmp_path)

    result = run("build", env=env, script=script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    calls = docker_calls(log)
    assert ["buildx", "version"] in calls
    build = next(call for call in calls if call[:2] == ["buildx", "build"])
    assert "--load" in build
    argument = next(
        build[index + 1]
        for index, value in enumerate(build[:-1])
        if value == "--build-arg" and build[index + 1].startswith("HARNESS_FINGERPRINT=")
    )
    expected = subprocess.run(
        ["python3", str(tmp_path / "operations" / "autoclave" / "fingerprint.py")],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ).stdout.strip()
    assert argument.partition("=")[2] == expected


def test_build_refuses_when_buildx_is_unavailable(tmp_path):
    script = elsewhere(tmp_path)
    env, log = fake_docker(tmp_path)
    env["FAKE_BUILDX_STATUS"] = "1"

    result = run("build", env=env, script=script, cwd=tmp_path)

    assert result.returncode != 0
    assert "Buildx is required" in result.stderr
    assert not any(call[:2] == ["buildx", "build"] for call in docker_calls(log))


def test_fingerprint_names_a_missing_image_input(tmp_path):
    elsewhere(tmp_path)
    (tmp_path / ".dockerignore").unlink()

    result = subprocess.run(
        ["python3", str(tmp_path / "operations" / "autoclave" / "fingerprint.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    # The name of this test is the assertion: the old message said only that *an* input
    # could not be read, leaving the operator to check six files. Naming the file is the
    # behaviour under test, so the file name is what is asserted.
    assert ".dockerignore" in result.stderr, "the failure did not name the missing input"
    assert "cannot read the image input" in result.stderr


def test_a_second_dispatch_for_the_same_task_is_refused_while_the_first_runs(tmp_path):
    script = elsewhere(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("bounded task\n")
    (tmp_path / "workbench" / "autoclave" / "task-x").mkdir(parents=True)
    env, _log = fake_docker(tmp_path)
    ready = tmp_path / "dispatch-is-holding"
    release = tmp_path / "release-dispatch"
    env.update(
        {
            "FAKE_CONTAINER_EXISTS": "1",
            "FAKE_CHAMBER_VENDOR": "codex",
            "FAKE_AUTH_VALID": "1",
            "FAKE_EXEC_STATUS": "0",
            "FAKE_HOLD_ON": "codex exec",
            "FAKE_HOLD_READY": str(ready),
            "FAKE_HOLD_RELEASE": str(release),
        }
    )
    first = subprocess.Popen(
        ["sh", str(script), "dispatch", "task-x", "codex", str(brief), "gpt-5.6-luna", "low"],
        cwd=tmp_path,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "the first dispatch never reached the CLI"
            assert first.poll() is None
            time.sleep(0.01)
        second = run(
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
        assert second.returncode != 0
        assert "task-x.lock" in second.stderr
    finally:
        release.touch()
        first.wait(timeout=30)


@pytest.mark.parametrize(
    ("command", "arguments"),
    (("shell", ("task-x",)), ("exec", ("task-x", "true"))),
)
def test_manual_access_still_works_while_a_dispatch_holds_the_lock(tmp_path, command, arguments):
    """Watching a running agent is the whole point of these two commands.

    They used to take the exclusive lifecycle lock. `dispatch` holds that lock for the
    entire agent run, and a Claude dispatch buffers its output until it exits, so for the
    hours an agent worked the only two commands that could show what it was doing answered
    "task is already active" -- and the refusal suggested removing the lock, which is the
    one action that breaks the serialisation the lock provides.

    Neither command mutates lifecycle state: they attach to a container that is already
    running, and the agent inside it is already executing arbitrary code. `collect`
    defends against concurrent drawer writes with its own single snapshot rather than by
    excluding the operator. So these sit beside `report`, outside the lock.
    """

    script = elsewhere(tmp_path)
    env, log = fake_docker(tmp_path)
    # `FAKE_EXEC_STATUS` is 0 because what the operator's command returns is not the
    # subject here; whether the lock let them reach the container is.
    env.update(
        {
            "FAKE_CONTAINER_EXISTS": "1",
            "FAKE_CONTAINER_RUNNING": "1",
            "FAKE_EXEC_STATUS": "0",
        }
    )
    lock = tmp_path / "workbench" / "autoclave" / ".locks" / "task-x.lock"
    lock.mkdir(parents=True)

    result = run(command, *arguments, env=env, script=script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert str(lock) not in result.stderr
    assert any(call[:1] == ["exec"] for call in docker_calls(log)), "the container was not reached"


def test_a_signal_releases_the_task_lock(tmp_path):
    script = elsewhere(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("bounded task\n")
    (tmp_path / "workbench" / "autoclave" / "task-x").mkdir(parents=True)
    env, _log = fake_docker(tmp_path)
    ready = tmp_path / "signal-ready"
    release = tmp_path / "signal-release"
    env.update(
        {
            "FAKE_CONTAINER_EXISTS": "1",
            "FAKE_CHAMBER_VENDOR": "codex",
            "FAKE_AUTH_VALID": "1",
            "FAKE_HOLD_ON": "codex exec",
            "FAKE_HOLD_READY": str(ready),
            "FAKE_HOLD_RELEASE": str(release),
        }
    )
    process = subprocess.Popen(
        ["sh", str(script), "dispatch", "task-x", "codex", str(brief), "gpt-5.6-luna", "low"],
        cwd=tmp_path,
        env={**os.environ, **env},
    )
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < deadline
        assert process.poll() is None
        time.sleep(0.01)
    try:
        process.terminate()
        release.touch()
        assert process.wait(timeout=30) != 0
    finally:
        release.touch()
    assert not (tmp_path / "workbench" / "autoclave" / ".locks" / "task-x.lock").exists()


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

    def test_collect_refuses_a_branch_that_rewrote_its_requested_base(self, tmp_path):
        script, clone, _drawer, env, _log = self.chamber(tmp_path)
        git(clone, "switch", "--orphan", "rewritten")
        (clone / "tracked.txt").write_text("rewritten history\n")
        git(clone, "add", "tracked.txt")
        git(
            clone,
            "commit",
            "--quiet",
            "-m",
            "rewritten\n\nCo-Authored-By: Test Model <model@example.invalid>",
        )
        git(clone, "branch", "-D", "agent/task-x")
        git(clone, "branch", "-m", "agent/task-x")

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "rewrote its chamber base" in result.stderr
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "rev-parse", "--verify", "agent/task-x"],
                capture_output=True,
            ).returncode
            != 0
        )
        assert_no_incoming_ref(tmp_path)

    def test_collect_does_not_replace_divergent_local_agent_work(self, tmp_path):
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        git(tmp_path, "switch", "--quiet", "-c", "agent/task-x")
        (tmp_path / "host-only.txt").write_text("keep this branch\n")
        git(tmp_path, "add", "host-only.txt")
        git(
            tmp_path,
            "commit",
            "--quiet",
            "-m",
            "host work\n\nCo-Authored-By: Test Model <model@example.invalid>",
        )
        local_tip = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "switch", "--quiet", "work/test")

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "contains work" in result.stderr
        assert git(tmp_path, "rev-parse", "agent/task-x") == local_tip
        assert_no_incoming_ref(tmp_path)

    def test_collect_does_not_move_an_agent_branch_checked_out_elsewhere(self, tmp_path):
        """A checked-out branch is another worktree, not merely a ref.

        This is deliberately a fast-forward: divergence must not be the thing that
        protects a worktree whose branch collection is about to update.
        """
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "branch", "agent/task-x", base)
        linked = tmp_path / "other-worktree"
        git(tmp_path, "worktree", "add", "--quiet", str(linked), "agent/task-x")
        try:
            result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

            assert result.returncode != 0
            assert "checked out in a worktree" in result.stderr
            assert git(tmp_path, "rev-parse", "agent/task-x") == base
            assert_no_incoming_ref(tmp_path)
        finally:
            git(tmp_path, "worktree", "remove", "--force", str(linked))

    def test_collect_refuses_a_symbolic_agent_branch_without_moving_its_target(self, tmp_path):
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        target = git(tmp_path, "rev-parse", "refs/heads/work/test")
        git(tmp_path, "symbolic-ref", "refs/heads/agent/task-x", "refs/heads/work/test")

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "symbolic ref" in result.stderr
        assert git(tmp_path, "rev-parse", "refs/heads/work/test") == target
        assert git(tmp_path, "symbolic-ref", "refs/heads/agent/task-x") == "refs/heads/work/test"

    def test_collect_serializes_the_same_task(self, tmp_path):
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        lock = tmp_path / "workbench" / "autoclave" / ".locks" / "task-x.lock"
        lock.mkdir(parents=True)

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert str(lock) in result.stderr
        assert f"rmdir {lock}" in result.stderr

    def test_collect_refuses_a_worktree_that_appears_after_the_occupancy_check(self, tmp_path):
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "branch", "agent/task-x", base)
        linked = tmp_path / "racing-worktree"
        real_git = shutil.which("git")
        assert real_git is not None
        wrapper = tmp_path / "bin" / "git"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "result = subprocess.run([os.environ['REAL_GIT'], *args])\n"
            "if result.returncode == 0 and 'worktree' in args and 'list' in args and not os.path.exists(os.environ['RACE_LINKED']):\n"
            "    subprocess.run([os.environ['REAL_GIT'], '-C', os.environ['RACE_ROOT'], "
            "'worktree', 'add', '--quiet', os.environ['RACE_LINKED'], 'agent/task-x'], check=True)\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        env.update({"REAL_GIT": real_git, "RACE_ROOT": str(tmp_path), "RACE_LINKED": str(linked)})
        try:
            result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

            assert result.returncode != 0
            assert "occupied" in result.stderr
            assert git(tmp_path, "rev-parse", "agent/task-x") == base
            assert git(linked, "status", "--porcelain") == ""
        finally:
            if linked.exists():
                subprocess.run(
                    [real_git, "-C", str(tmp_path), "worktree", "remove", "--force", str(linked)],
                    check=True,
                )

    def test_collect_refuses_a_ref_move_between_its_read_and_cas(self, tmp_path):
        """The final update must compare the value it inspected, not force it."""
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "branch", "agent/task-x", base)
        real_git = shutil.which("git")
        assert real_git is not None
        wrapper = tmp_path / "bin" / "git"
        moved = tmp_path / "ref-moved"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "result = subprocess.run([os.environ['REAL_GIT'], *args])\n"
            "marker = pathlib.Path(os.environ['RACE_MOVED'])\n"
            "if result.returncode == 0 and 'worktree' in args and 'list' in args:\n"
            "    count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
            "    marker.write_text(str(count))\n"
            "    if count != 2:\n"
            "        raise SystemExit(result.returncode)\n"
            "    root = os.environ['RACE_ROOT']\n"
            "    pathlib.Path(root, 'host-race.txt').write_text('new host work\\n')\n"
            "    subprocess.run([os.environ['REAL_GIT'], '-C', root, 'add', 'host-race.txt'], check=True)\n"
            "    subprocess.run([os.environ['REAL_GIT'], '-C', root, 'commit', '--quiet', '-m', 'host race\\n\\nCo-Authored-By: Test <test@example.invalid>'], check=True)\n"
            "    subprocess.run([os.environ['REAL_GIT'], '-C', root, 'update-ref', 'refs/heads/agent/task-x', 'HEAD'], check=True)\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        env.update({"REAL_GIT": real_git, "RACE_ROOT": str(tmp_path), "RACE_MOVED": str(moved)})

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "moved during collection" in result.stderr
        assert moved.exists()
        assert git(tmp_path, "rev-parse", "agent/task-x") == git(tmp_path, "rev-parse", "HEAD")

    def test_collect_rolls_back_if_a_worktree_started_before_the_cas(self, tmp_path):
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "branch", "agent/task-x", base)
        linked = tmp_path / "post-cas-worktree"
        real_git = shutil.which("git")
        assert real_git is not None
        wrapper = tmp_path / "bin" / "git"
        count = tmp_path / "worktree-list-count"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "result = subprocess.run([os.environ['REAL_GIT'], *args])\n"
            "counter = pathlib.Path(os.environ['RACE_COUNT'])\n"
            "if result.returncode == 0 and 'worktree' in args and 'list' in args:\n"
            "    n = int(counter.read_text()) + 1 if counter.exists() else 1\n"
            "    counter.write_text(str(n))\n"
            "    if n == 2:\n"
            "        subprocess.run([os.environ['REAL_GIT'], '-C', os.environ['RACE_ROOT'], 'worktree', 'add', '--quiet', os.environ['RACE_LINKED'], 'agent/task-x'], check=True)\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        env.update(
            {
                "REAL_GIT": real_git,
                "RACE_ROOT": str(tmp_path),
                "RACE_LINKED": str(linked),
                "RACE_COUNT": str(count),
            }
        )
        try:
            result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

            assert result.returncode != 0
            assert "previous value was restored" in result.stderr
            assert git(tmp_path, "rev-parse", "agent/task-x") == base
            assert git(linked, "status", "--porcelain") == ""
        finally:
            if linked.exists():
                subprocess.run(
                    [real_git, "-C", str(tmp_path), "worktree", "remove", "--force", str(linked)],
                    check=True,
                )

    def test_collect_rolls_back_a_newline_named_worktree_started_before_the_cas(self, tmp_path):
        script, _clone, _drawer, env, _log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "branch", "agent/task-x", base)
        linked = tmp_path / "pre-cas\nworktree"
        real_git = shutil.which("git")
        assert real_git is not None
        wrapper = tmp_path / "bin" / "git"
        count = tmp_path / "newline-worktree-list-count"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "result = subprocess.run([os.environ['REAL_GIT'], *args])\n"
            "counter = pathlib.Path(os.environ['RACE_COUNT'])\n"
            "if result.returncode == 0 and 'worktree' in args and 'list' in args:\n"
            "    n = int(counter.read_text()) + 1 if counter.exists() else 1\n"
            "    counter.write_text(str(n))\n"
            "    if n == 2:\n"
            "        subprocess.run([os.environ['REAL_GIT'], '-C', os.environ['RACE_ROOT'], 'worktree', 'add', '--quiet', os.environ['RACE_LINKED'], 'agent/task-x'], check=True)\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        env.update(
            {
                "REAL_GIT": real_git,
                "RACE_ROOT": str(tmp_path),
                "RACE_LINKED": str(linked),
                "RACE_COUNT": str(count),
            }
        )
        try:
            result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

            assert result.returncode != 0
            assert "previous value was restored" in result.stderr
            assert git(tmp_path, "rev-parse", "agent/task-x") == base
            assert git(linked, "status", "--porcelain") == ""
        finally:
            if linked.exists():
                subprocess.run(
                    [real_git, "-C", str(tmp_path), "worktree", "remove", "--force", str(linked)],
                    check=True,
                )

    def test_collect_keeps_a_clean_worktree_created_after_the_cas(self, tmp_path):
        script, clone, _drawer, env, _log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        incoming = git(clone, "rev-parse", "agent/task-x")
        git(tmp_path, "branch", "agent/task-x", base)
        linked = tmp_path / "after-cas-worktree"
        real_git = shutil.which("git")
        assert real_git is not None
        wrapper = tmp_path / "bin" / "git"
        count = tmp_path / "worktree-list-count"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "counter = pathlib.Path(os.environ['RACE_COUNT'])\n"
            "if 'worktree' in args and 'list' in args:\n"
            "    n = int(counter.read_text()) + 1 if counter.exists() else 1\n"
            "    counter.write_text(str(n))\n"
            "    if n == 3:\n"
            "        subprocess.run([os.environ['REAL_GIT'], '-C', os.environ['RACE_ROOT'], 'worktree', 'add', '--quiet', os.environ['RACE_LINKED'], 'agent/task-x'], check=True)\n"
            "result = subprocess.run([os.environ['REAL_GIT'], *args])\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        env.update(
            {
                "REAL_GIT": real_git,
                "RACE_ROOT": str(tmp_path),
                "RACE_LINKED": str(linked),
                "RACE_COUNT": str(count),
            }
        )
        try:
            result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

            assert result.returncode == 0, result.stderr
            assert git(tmp_path, "rev-parse", "agent/task-x") == incoming
            assert git(linked, "status", "--porcelain") == ""
        finally:
            if linked.exists():
                subprocess.run(
                    [real_git, "-C", str(tmp_path), "worktree", "remove", "--force", str(linked)],
                    check=True,
                )

    def test_failed_collect_does_not_make_an_unreachable_object_safe_to_remove(self, tmp_path):
        script, _clone, drawer, env, log = self.chamber(tmp_path)
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "branch", "agent/task-x", base)
        linked = tmp_path / "occupied-worktree"
        real_git = shutil.which("git")
        assert real_git is not None
        subprocess.run(
            [
                real_git,
                "-C",
                str(tmp_path),
                "worktree",
                "add",
                "--quiet",
                str(linked),
                "agent/task-x",
            ],
            check=True,
        )
        try:
            collected = run("collect", "task-x", env=env, script=script, cwd=tmp_path)
            assert collected.returncode != 0
            (drawer / "task-x.bundle").unlink()

            removed = run("rm", "task-x", env=env, script=script, cwd=tmp_path)

            assert removed.returncode != 0
            assert "not have a durably collected copy" in removed.stderr
            assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log))
        finally:
            subprocess.run(
                [real_git, "-C", str(tmp_path), "worktree", "remove", "--force", str(linked)],
                check=True,
            )

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
        assert "holds a commit this repository does not have" in result.stderr
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

    def test_rm_sees_work_committed_on_any_branch_not_only_agent_task(self, tmp_path):
        """Asking only about `agent/<task>` reads the wrong chamber as empty.

        The tree is clean once an agent has committed, and `agent/<task>` still stands
        at the base — so a chamber whose agent worked on `work/refactor`, or on a
        detached HEAD, presented as holding nothing and went straight to `docker rm
        --force`. `collect` bundles only `agent/<task>` too, so that work was never
        collectable and nothing ever said so.
        """
        script, clone, _drawer, env, log = self.chamber(tmp_path, commits=0)
        git(clone, "switch", "--quiet", "-c", "work/spike")
        (clone / "RESULT.txt").write_text("the whole point of the dispatch\n")
        git(clone, "add", "RESULT.txt")
        git(clone, "commit", "--quiet", "-m", "spike\n\nCo-Authored-By: m <m@x>")
        git(clone, "switch", "--quiet", "agent/task-x")

        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0, "a chamber holding an hour of work was destroyed"
        assert "does not have" in result.stderr
        assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log))

    def test_rm_asks_whether_the_object_is_here_not_whether_the_tips_match(self, tmp_path):
        """Equality deadlocked the moment the host branch moved — which is the very
        next thing `collect` tells the operator to do, amend the trailers in. `rm`
        then refused for ever, `collect` refused to re-fetch what is now a rewind, and
        the only spelling left was the destructive one. Presence is the question that
        was always meant."""
        script, clone, _drawer, env, log = self.chamber(tmp_path)
        git(tmp_path, "fetch", "--quiet", str(clone), "agent/task-x:agent/task-x")
        # What collect recommends: re-author, which moves the host branch off the tip.
        git(tmp_path, "switch", "--quiet", "agent/task-x")
        git(
            tmp_path, "commit", "--quiet", "--allow-empty", "-m", "after\n\nCo-Authored-By: m <m@x>"
        )
        git(tmp_path, "switch", "--quiet", "work/test")

        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert ["rm", "--force", "verbatus-ac-task-x"] in docker_calls(log)

    def test_rm_keeps_a_recoverable_bundle_after_the_collected_ref_is_pruned(self, tmp_path):
        script, clone, drawer, env, log = self.chamber(tmp_path)
        tip = git(clone, "rev-parse", "agent/task-x")
        collected = run("collect", "task-x", env=env, script=script, cwd=tmp_path)
        assert collected.returncode == 0, collected.stderr
        retained = tmp_path / "workbench" / "autoclave" / ".collected" / "task-x" / tip
        assert retained.is_file()

        git(tmp_path, "branch", "-D", "agent/task-x")
        (drawer / "task-x.bundle").unlink()
        git(tmp_path, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
        git(tmp_path, "gc", "--prune=now")
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "cat-file", "-e", f"{tip}^{{commit}}"]
            ).returncode
            != 0
        )

        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert ["rm", "--force", "verbatus-ac-task-x"] in docker_calls(log)

    def test_rm_refuses_a_retained_bundle_whose_pack_was_truncated(self, tmp_path):
        script, clone, drawer, env, log = self.chamber(tmp_path)
        tip = git(clone, "rev-parse", "agent/task-x")
        collected = run("collect", "task-x", env=env, script=script, cwd=tmp_path)
        assert collected.returncode == 0, collected.stderr
        retained = tmp_path / "workbench" / "autoclave" / ".collected" / "task-x" / tip

        git(tmp_path, "branch", "-D", "agent/task-x")
        (drawer / "task-x.bundle").unlink()
        git(tmp_path, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
        git(tmp_path, "gc", "--prune=now")
        data = retained.read_bytes()
        retained.write_bytes(data[: data.index(b"PACK") + 20])

        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "durably collected copy" in result.stderr
        assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log))

    def test_rm_refuses_a_chamber_it_cannot_read(self, tmp_path):
        """ "A check that cannot run is a failure, not a pass" — `.githooks/commit-msg`
        says it in those words, and the arm that says it here had no test. Swallowing
        the exit status reads a broken clone or a docker error as a clean tree."""
        script, _clone, _drawer, env, log = self.chamber(tmp_path, commits=0)
        env["FAKE_CHAMBER_WORK"] = str(tmp_path / "no-such-clone")

        result = run("rm", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "cannot read" in result.stderr
        assert not any(call[:2] == ["rm", "--force"] for call in docker_calls(log))

    def test_collect_refuses_a_bundle_slot_that_is_not_a_bundle(self, tmp_path):
        """`/out` is the agent's, and `[ -f ]` is not enough.

        `git fetch` only reads — and git reads a *gitfile*: a one-line `gitdir:
        /some/other/repo/.git` is an ordinary regular file, and fetching it fetches
        from that repository. Left in this slot it turns `collect` into "import any
        repository on this machine and report it as the work I did". This builds a real
        private repository and proves nothing from it arrives.
        """
        script, clone, drawer, env, _log = self.chamber(tmp_path)
        private = tmp_path / "somebody-elses-repo"
        subprocess.run(["git", "init", "--quiet", str(private)], check=True)
        git(private, "config", "user.name", "v")
        git(private, "config", "user.email", "v@v")
        (private / "PRIVATE.txt").write_text("SENTINEL-PRIVATE\n")
        git(private, "add", "PRIVATE.txt")
        git(private, "commit", "--quiet", "-m", "private\n\nCo-Authored-By: m <m@x>")
        git(private, "branch", "--move", "agent/task-x")

        # A process the agent left running replaces the slot after `git bundle create`
        # wrote it and before the host reads it.
        env["FAKE_PLANT_AFTER_EXEC"] = f"{drawer / 'task-x.bundle'}=gitdir: {private}/.git\n"

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0, "collect fetched from a repository the agent chose"
        assert "not a git bundle" in result.stderr
        arrived = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "agent/task-x"],
            capture_output=True,
        )
        assert arrived.returncode != 0, "a ref arrived from somebody else's repository"

    def test_collect_fetches_snapshot_when_slot_changes_after_verify(self, tmp_path):
        """A post-verify slot swap cannot change the bundle that fetches.

        The git wrapper replaces the agent-controlled `/out` slot immediately after
        the host verifies its snapshot. Fetching the original slot would import the
        private gitfile; fetching the snapshot returns the chamber branch.
        """
        script, clone, drawer, env, _log = self.chamber(tmp_path)
        private = tmp_path / "somebody-elses-repo"
        subprocess.run(["git", "init", "--quiet", str(private)], check=True)
        git(private, "config", "user.name", "v")
        git(private, "config", "user.email", "v@v")
        (private / "PRIVATE.txt").write_text("SENTINEL-PRIVATE\n")
        git(private, "add", "PRIVATE.txt")
        git(private, "commit", "--quiet", "-m", "private\n\nCo-Authored-By: m <m@x>")
        git(private, "branch", "--move", "agent/task-x")

        real_git = shutil.which("git")
        assert real_git is not None
        wrapper = tmp_path / "bin" / "git"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "from pathlib import Path\n"
            "result = subprocess.run([os.environ['REAL_GIT'], *sys.argv[1:]])\n"
            "if result.returncode == 0 and 'bundle' in sys.argv and 'verify' in sys.argv:\n"
            "    Path(os.environ['SWAP_AFTER_VERIFY']).write_text(os.environ['SWAP_CONTENT'])\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        env.update(
            {
                "REAL_GIT": real_git,
                "SWAP_AFTER_VERIFY": str(drawer / "task-x.bundle"),
                "SWAP_CONTENT": f"gitdir: {private}/.git\n",
            }
        )

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert git(tmp_path, "rev-parse", "agent/task-x") == git(clone, "rev-parse", "agent/task-x")
        assert (drawer / "task-x.bundle").read_text() == f"gitdir: {private}/.git\n"

    def test_collect_judges_attribution_the_way_the_hook_does(self, tmp_path):
        """A substring search disagreed with `commit-msg` in the direction that
        matters. `Co-Authored-By: nobody` in the middle of a body matches a grep, is
        refused by the hook, and registers no co-author with git or GitHub — so the
        loose test called such a commit attributed and said nothing."""
        script, clone, _drawer, env, _log = self.chamber(tmp_path, commits=0)
        (clone / "tracked.txt").write_text("changed\n")
        git(clone, "add", "tracked.txt")
        git(clone, "commit", "--quiet", "-m", "work\nCo-Authored-By: nobody\n\nreal body here")

        result = run("collect", "task-x", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert "UNATTRIBUTED" in result.stdout, (
            "a trailer git itself does not register was counted as attribution"
        )

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

    @pytest.mark.parametrize("shape", ["directory", "fifo"])
    def test_the_brief_slot_is_refused_by_the_helper_alone(self, tmp_path, shape):
        """The cases the shell layer does not cover, so the helper is load-bearing.

        `[ -L ]` catches a link and nothing else, so a directory or a FIFO left in the
        brief slot reaches `safe_file` and only its `S_ISREG` test refuses them. The
        FIFO is the one with teeth: without `O_NONBLOCK` the open would wait for a
        writer that never comes, and an agent could hang the launcher without pointing
        anywhere at all. A timeout here is a failure, not a hang.
        """
        script, slot = self.drawer(tmp_path, task="task-x")
        if shape == "directory":
            (slot / "brief.md").mkdir()
        else:
            os.mkfifo(slot / "brief.md")
        brief = tmp_path / "brief-source.md"
        brief.write_text("bounded task\n")
        env, _log = fake_docker(tmp_path)
        env.update({"FAKE_CONTAINER_EXISTS": "1", "FAKE_AUTH_VALID": "1"})

        result = subprocess.run(
            ["sh", str(script), "dispatch", "task-x", "codex", str(brief), "gpt-5.6-luna", "low"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={**os.environ, **env},
            timeout=20,
        )
        assert result.returncode != 0
        assert "not a safe regular file" in result.stderr

    def test_a_brief_reached_through_a_symlink_still_works(self, tmp_path):
        """The source is a path the *session* chose, not the agent's, so it is not
        the untrusted end. Opening it with `O_NOFOLLOW` too would break the ordinary
        way of pointing at a standing brief — and would refuse it with a message
        naming the destination, which is a perfectly good empty slot."""
        script, _slot = self.drawer(tmp_path, task="task-x")
        real = tmp_path / "standing-brief.md"
        real.write_text("bounded task\n")
        link = tmp_path / "brief.md"
        link.symlink_to(real)
        env, _log = fake_docker(tmp_path)
        env.update({"FAKE_CONTAINER_EXISTS": "1", "FAKE_AUTH_VALID": "1", "FAKE_EXEC_STATUS": "0"})

        result = run(
            "dispatch",
            "task-x",
            "codex",
            str(link),
            "gpt-5.6-luna",
            "low",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode == 0, result.stderr
        assert (tmp_path / "workbench" / "autoclave" / "task-x" / "brief.md").read_text() == (
            "bounded task\n"
        )

    @pytest.mark.parametrize("shape", ["directory", "fifo"])
    def test_the_helper_refuses_anything_that_is_not_a_regular_file(self, tmp_path, shape):
        """Directly, because `S_ISREG` and `O_NONBLOCK` are each removable on their
        own. Without `S_ISREG` a FIFO opened non-blocking reads as an *empty file* and
        the helper returns success; without `O_NONBLOCK` the same open never returns.
        Both are failures and neither is caught by the link test."""
        target = tmp_path / shape
        if shape == "directory":
            target.mkdir()
        else:
            os.mkfifo(target)
        source = tmp_path / "source"
        source.write_text("bounded task\n")

        read = subprocess.run(
            ["python3", str(SAFE_FILE), "read", str(target)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        write = subprocess.run(
            ["python3", str(SAFE_FILE), "write", str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert read.returncode != 0, f"reading a {shape} succeeded"
        assert read.stdout == ""
        assert write.returncode != 0, f"writing to a {shape} succeeded"

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

    def test_bundle_creation_does_not_follow_an_output_symlink(self, tmp_path):
        repository = tmp_path / "repository"
        repository.mkdir()
        git(repository, "init", "--quiet", "-b", "work/test")
        git(repository, "config", "user.name", "Test")
        git(repository, "config", "user.email", "test@example.invalid")
        (repository / "file").write_text("content\n")
        git(repository, "add", "file")
        git(repository, "commit", "--quiet", "-m", "base")
        victim = tmp_path / "victim"
        victim.write_text("keep\n")
        slot = tmp_path / "bundle"
        slot.symlink_to(victim)

        result = subprocess.run(
            ["python3", str(SAFE_FILE), "bundle", str(slot), "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert victim.read_text() == "keep\n"

    def test_writing_a_slot_from_itself_preserves_the_brief(self, tmp_path):
        slot = tmp_path / "brief.md"
        slot.write_text("bounded task\n")

        result = subprocess.run(
            ["python3", str(SAFE_FILE), "write", str(slot), str(slot)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert slot.read_text() == "bounded task\n"


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
        # The dead engine is staged rather than inherited. Written without this, the
        # test asserted that the host running it had no Docker: it passed in CI and in
        # a chamber and failed on the machine the autoclave was built for.
        bindir = tmp_path / "no-engine"
        bindir.mkdir()
        dead = bindir / "docker"
        dead.write_text("#!/bin/sh\nexit 1\n")
        dead.chmod(0o755)
        env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        result = run("new", "some-task", env=env, script=script, cwd=tmp_path)
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

    def test_new_refuses_before_a_snapshot_when_the_image_is_missing(self, tmp_path):
        script = self.dirty_repo(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_IMAGE_EXISTS"] = "0"

        result = run("new", "no-image", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "image not built" in result.stderr
        assert not any("--detach" in call for call in docker_calls(log))
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tmp_path),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/heads/autoclave/snapshot-no-image",
                ],
                capture_output=True,
            ).returncode
            != 0
        )

    def test_a_stale_image_fingerprint_refuses_the_chamber(self, tmp_path):
        """An edit to any baked input does nothing at all until the image is rebuilt.

        It happened on 2026-08-02: the brief was edited and a chamber created in the
        same session ran the previous one, and the only symptom was an agent behaving
        as though the new paragraph had never been written — indistinguishable from an
        agent that read it and ignored it. Nothing can correct it at run time either,
        because all three copies sit at root-owned paths and the container runs as a
        non-root user.
        """
        script = self.dirty_repo(tmp_path)
        env, _log = fake_docker(tmp_path)
        env["FAKE_IMAGE_FINGERPRINT"] = "0" * 64
        result = run("new", "some-task", env=env, script=script, cwd=tmp_path)
        assert result.returncode != 0, "a chamber was built from stale harness inputs"
        assert "harness inputs" in result.stderr, result.stderr
        assert "build" in result.stderr, result.stderr

    def test_a_matching_baked_brief_lets_the_chamber_through(self, tmp_path):
        """The positive control. Without it the test above passes on any failure."""
        script = self.dirty_repo(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_SETUP_STATUS"] = "0"
        env["FAKE_EXEC_STATUS"] = "0"
        result = run("new", "some-task", env=env, script=script, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        creation = next(call for call in docker_calls(log) if "--detach" in call)
        assert "sha256:" + "1" * 64 in creation
        assert "verbatus-autoclave:dev" not in creation

    def test_new_skips_the_claude_trust_initializer_for_codex(self, tmp_path):
        script = self.dirty_repo(tmp_path)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_VOLUMES": "verbatus-ac-auth-codex",
                "FAKE_AUTH_VALID": "1",
                "FAKE_SETUP_STATUS": "0",
                "FAKE_EXEC_STATUS": "0",
            }
        )

        result = run("new", "codex-seat", "HEAD", "codex", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        config = next(call for call in docker_calls(log) if "AC_VENDOR=codex" in call)
        assert "AC_VENDOR=codex" in config
        assert '[ "$AC_VENDOR" = claude ] || exit 0' in script.read_text()

    def test_a_second_new_for_the_same_task_is_refused_while_the_first_is_live(self, tmp_path):
        script = self.dirty_repo(tmp_path)
        env, _log = fake_docker(tmp_path)
        ready = tmp_path / "new-is-holding"
        release = tmp_path / "release-new"
        env.update(
            {
                "FAKE_HOLD_ON": "--detach",
                "FAKE_HOLD_READY": str(ready),
                "FAKE_HOLD_RELEASE": str(release),
                "FAKE_SETUP_STATUS": "0",
            }
        )
        first = subprocess.Popen(
            ["sh", str(script), "new", "same-task"],
            cwd=tmp_path,
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert time.monotonic() < deadline, "the first new never reached docker run"
                assert first.poll() is None
                time.sleep(0.01)
            second = run("new", "same-task", env=env, script=script, cwd=tmp_path)
            assert second.returncode != 0
            assert "same-task.lock" in second.stderr
            assert "rmdir" in second.stderr
        finally:
            release.touch()
            first.wait(timeout=30)

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

    def test_the_captured_path_count_is_the_one_the_operator_reads(self, tmp_path):
        """It said `0 path(s)` directly under "the working tree is not clean".

        The count was taken after `base_sha` had already been moved onto the snapshot,
        so it diffed the working tree against a commit of that same working tree and
        found nothing every time. A number that is always zero beside a line saying
        something was captured is worse than no number at all.

        A floor rather than an exact figure: the stub's own scratch files land in this
        throwaway repository untracked and are legitimately part of the count, so
        pinning an exact number would pin the test to the stub's file list.
        """
        script = self.dirty_repo(tmp_path)
        env, _log = fake_docker(tmp_path)

        result = run("new", "counted-case", env=env, script=script, cwd=tmp_path)

        assert "working tree is not clean" in result.stdout, result.stdout
        counted = int(result.stdout.split("path(s) beyond")[0].rsplit(None, 1)[-1])
        # Three tracked — staged, unstaged, deleted — and one untracked, at least.
        assert counted >= 4, f"counted {counted} of the four paths this repository has"
        assert result.returncode != 0, "the stubbed setup should have stopped new"

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
        # **The canary, and it is what actually pins this.** The assertions above pin
        # the two *values*; unquoting the delimiter and backslash-escaping those two
        # variables leaves every one of them true while the host resumes expanding
        # everything else in the block, comments included — which is verbatim the
        # defect this test is named for. These two survive only under a quoted
        # delimiter.
        assert "$(printf host-expansion-canary)" in crossed, (
            "a command substitution in the block was evaluated on the host"
        )
        assert "`printf host-expansion-canary`" in crossed, (
            "a backtick in the block was evaluated on the host"
        )
        assert "<<'AUTOCLAVE_SETUP'" in SCRIPT.read_text(), (
            "the here-document delimiter is unquoted"
        )
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

    def test_doctor_says_unknown_rather_than_guessing_without_an_engine(self, tmp_path):
        """`doctor` is the first thing anyone runs and must answer on a bare machine.

        With nothing to ask, the honest answer is neither yes nor no.

        The absent engine is **staged**, not inherited from whatever machine this runs
        on. Written as a bare `run("doctor")` it asserted that *this* host had no
        engine, so it passed in CI and in a chamber and failed on the one machine the
        autoclave exists for — the machine where Colima is up.
        """
        bindir = tmp_path / "bin"
        bindir.mkdir()
        dead = bindir / "docker"
        dead.write_text("#!/bin/sh\nexit 1\n")
        dead.chmod(0o755)
        env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        result = run("doctor", env=env)
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

    def test_login_uses_its_inspected_image_id_for_the_booth_and_status_check(self, tmp_path):
        """A tag may move while an operator completes a login, so pin this operation."""
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        image_id = "sha256:" + "a" * 64
        env.update({"FAKE_AUTH_VALID": "1", "FAKE_IMAGE_ID": image_id})

        result = run("login", "codex", env=env, script=script)

        assert result.returncode == 0, result.stderr
        login = next(call for call in docker_calls(log) if "codex login" in " ".join(call))
        status = next(call for call in docker_calls(log) if "login status" in " ".join(call))
        assert image_id in login
        assert image_id in status
        assert "verbatus-autoclave:dev" not in login
        assert "verbatus-autoclave:dev" not in status

    def test_a_tty_login_uses_the_same_inspected_image_id(self, tmp_path):
        """The interactive branch must not fall back to the mutable tag either."""
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        image_id = "sha256:" + "b" * 64
        env.update({"FAKE_AUTH_VALID": "1", "FAKE_IMAGE_ID": image_id})
        master, slave = os.openpty()
        try:
            process = subprocess.Popen(
                ["sh", str(script), "login", "codex"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=ROOT,
                env={**os.environ, **env},
            )
            assert process.wait(timeout=15) == 0
        finally:
            os.close(slave)
            os.close(master)

        login = next(call for call in docker_calls(log) if "codex login" in " ".join(call))
        assert "--tty" in login
        assert image_id in login
        assert "verbatus-autoclave:dev" not in login

    def test_a_first_time_login_keeps_the_volume_it_created(self, tmp_path):
        """The path that matters, and the one nothing covered.

        Every other login test presets the volume, so `created_now` is `no`, the trap
        is never armed and the disarm is never reached. Deleting the disarm left the
        whole suite green while `login` reported success and then removed the
        credential volume it had just created — which is every genuine first sign-in.
        """
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_AUTH_VALID"] = "1"

        result = run("login", "codex", env=env, script=script)

        assert result.returncode == 0, result.stderr
        assert "reports itself signed in" in result.stdout
        assert ["volume", "create", "verbatus-ac-auth-codex"] in docker_calls(log), (
            "the fixture did not exercise the first-time path"
        )
        assert not any(call[:2] == ["volume", "rm"] for call in docker_calls(log)), (
            "a completed first sign-in was deleted on the way out"
        )

    def test_a_first_time_claude_login_keeps_the_empty_volume_it_created(self, tmp_path):
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_AUTH_VALID"] = "1"

        result = run("login", "claude", env=env, script=script)

        assert result.returncode == 0, result.stderr
        assert ["volume", "create", "verbatus-ac-auth-claude"] in docker_calls(log)
        assert not any(call[:2] == ["volume", "rm"] for call in docker_calls(log))

    def test_a_check_that_could_not_run_never_costs_a_sign_in(self, tmp_path):
        """ "The vendor says no" and "the container never started" are different
        answers, and only one of them may be destructive.

        Collapsed into one, a transient engine error during the one-second
        verification run deleted the volume the interactive sign-in had just landed
        in — after the vendor CLI itself exited 0.
        """
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_AUTH_ASKABLE"] = "0"

        result = run("login", "codex", env=env, script=script)

        assert result.returncode != 0
        assert "could not be verified" in result.stderr
        assert "left as it is" in result.stderr
        assert not any(call[:2] == ["volume", "rm"] for call in docker_calls(log)), (
            "a sign-in was destroyed because the check could not run"
        )

    def test_doctor_says_unknown_when_the_vendor_cannot_be_asked(self, tmp_path):
        script = elsewhere(tmp_path)
        env, _log = fake_docker(tmp_path)
        env["FAKE_VOLUMES"] = "verbatus-ac-auth-claude verbatus-ac-auth-codex"
        env["FAKE_AUTH_ASKABLE"] = "0"
        result = run("doctor", env=env, script=script)
        assert result.returncode == 0
        assert result.stdout.count("could not be asked") == 2, result.stdout

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

    def test_new_refreshes_claude_before_its_authoritative_status_check(self, tmp_path):
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_AUTH_VALID": "1",
                "FAKE_REFRESH_STATUS": "0",
                "FAKE_VOLUMES": "verbatus-ac-auth-claude",
                "FAKE_SETUP_STATUS": "0",
                "FAKE_EXEC_STATUS": "0",
            }
        )

        result = run("new", "refresh-first", "HEAD", "claude", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        calls = docker_calls(log)
        refresh_call = next(call for call in calls if "refresh_claude_token.py" in " ".join(call))
        status_call = next(call for call in calls if "auth status" in " ".join(call))
        assert calls.index(refresh_call) < calls.index(status_call)

    def test_new_does_not_create_an_empty_claude_volume_while_refusing(self, tmp_path):
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_AUTH_VALID"] = "1"

        result = run("new", "no-volume", "HEAD", "claude", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert "not signed in" in result.stderr
        assert not any("refresh_claude_token.py" in " ".join(call) for call in docker_calls(log))
        assert ["volume", "create", "verbatus-ac-auth-claude"] not in docker_calls(log)

    @pytest.mark.parametrize(
        ("status", "expected", "forbidden"),
        (
            ("1", "stored credential is unchanged", "could not be read or republished"),
            ("2", "could not be read or republished", "stored credential is unchanged"),
        ),
    )
    def test_new_blocks_when_refresh_fails_and_says_which_kind_of_failure(
        self, tmp_path, status, expected, forbidden
    ):
        """Exit 1 is retryable; exit 2 is a local credential fault needing a human.

        Collapsing both into "run login" sent the operator to an interactive sign-in that
        rotates a refresh token which is usually still valid -- turning a transient
        endpoint failure into a real re-authentication.
        """

        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_AUTH_VALID": "1",
                "FAKE_REFRESH_STATUS": status,
                "FAKE_VOLUMES": "verbatus-ac-auth-claude",
            }
        )

        result = run("new", "dead-token", "HEAD", "claude", env=env, script=script, cwd=tmp_path)

        assert result.returncode != 0
        assert expected in result.stderr
        assert forbidden not in result.stderr
        assert not any("auth status" in " ".join(call) for call in docker_calls(log))

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


class TestClaudeConfigurationUsesTheMountedVolume:
    """Claude keeps all refresh state inside the mounted credential directory."""

    def test_the_login_booth_points_configuration_at_the_volume(self, tmp_path):
        script = elsewhere(tmp_path)
        env, log = fake_docker(tmp_path)
        env["FAKE_AUTH_VALID"] = "1"

        result = run("login", "claude", env=env, script=script, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        login = next(call for call in docker_calls(log) if "claude auth login" in " ".join(call))
        assert "CLAUDE_CONFIG_DIR=/home/agent/.claude" in login
        assert ".credentials.autoclave.lock" in " ".join(login)

    def test_dispatch_points_configuration_at_the_volume(self, tmp_path):
        script = elsewhere(tmp_path)
        brief = tmp_path / "brief.md"
        brief.write_text("bounded task\n")
        (tmp_path / "workbench" / "autoclave" / "task-x").mkdir(parents=True)
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_VENDOR": "claude",
                "FAKE_AUTH_VALID": "1",
                "FAKE_REFRESH_STATUS": "0",
                "FAKE_EXEC_STATUS": "0",
            }
        )

        result = run(
            "dispatch",
            "task-x",
            "claude",
            str(brief),
            "sonnet",
            "medium",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode == 0, result.stderr
        calls = [call for call in docker_calls(log) if call[:1] == ["exec"]]
        cli = next(call for call in calls if "--append-system-prompt-file" in " ".join(call))
        refresh_call = next(call for call in calls if "refresh_claude_token.py" in " ".join(call))
        assert "CLAUDE_CONFIG_DIR=/home/agent/.claude" in cli
        assert ".credentials.autoclave.lock" not in " ".join(cli)
        assert "CLAUDE_CONFIG_DIR=/home/agent/.claude" in refresh_call
        status_call = next(call for call in calls if "auth status" in " ".join(call))
        assert ".credentials.autoclave.lock" not in " ".join(status_call)
        assert calls.index(refresh_call) < calls.index(status_call)
        assert calls.index(refresh_call) < calls.index(cli)

    @pytest.mark.parametrize(
        ("status", "expected"),
        (
            ("1", "stored credential is unchanged"),
            ("2", "could not be read or republished"),
        ),
    )
    def test_dispatch_blocks_when_refresh_fails_and_says_which_kind_of_failure(
        self, tmp_path, status, expected
    ):
        """The same three-way split as `new`: a transient failure mid-dispatch is not a
        lost sign-in, and must not be reported as one."""

        script = elsewhere(tmp_path)
        brief = tmp_path / "brief.md"
        brief.write_text("bounded task\n")
        env, log = fake_docker(tmp_path)
        env.update(
            {
                "FAKE_CONTAINER_EXISTS": "1",
                "FAKE_CHAMBER_VENDOR": "claude",
                "FAKE_AUTH_VALID": "1",
                "FAKE_REFRESH_STATUS": status,
            }
        )

        result = run(
            "dispatch",
            "task-x",
            "claude",
            str(brief),
            "sonnet",
            "medium",
            env=env,
            script=script,
            cwd=tmp_path,
        )

        assert result.returncode != 0
        assert expected in result.stderr
        assert not any("auth status" in " ".join(call) for call in docker_calls(log))

    def test_the_refresh_helper_reaches_the_image(self):
        helper = ROOT / "operations" / "autoclave" / "refresh_claude_token.py"
        assert helper.is_file()
        assert (
            "refresh_claude_token.py"
            in (ROOT / "operations" / "autoclave" / "Dockerfile").read_text()
        )
        assert (
            "!operations/autoclave/refresh_claude_token.py" in (ROOT / ".dockerignore").read_text()
        )

    def test_the_image_and_refresh_helper_name_the_same_claude_client_id(self):
        helper = (ROOT / "operations" / "autoclave" / "refresh_claude_token.py").read_text()
        dockerfile = (ROOT / "operations" / "autoclave" / "Dockerfile").read_text()
        client_id = re.search(r'^CLIENT_ID = "([^"]+)"$', helper, re.MULTILINE)
        token_url = re.search(r'^TOKEN_URL = "([^"]+)"$', helper, re.MULTILINE)

        assert client_id, "refresh helper defines no public client id"
        assert token_url, "refresh helper defines no token endpoint"
        # The values, not the shell spelling that checks them. Asserting on `grep -a -q`
        # pinned an implementation detail: rewriting the check to name *which* constant
        # moved broke this test without any constant having changed.
        assert client_id.group(1) in dockerfile, "image does not verify the helper client id"
        assert token_url.group(1) in dockerfile, "image does not verify the helper token endpoint"
        assert "grep -a -q" in dockerfile, "image no longer greps the launcher at all"


class TestTheTrustFlagAgainstARealFilesystem:
    """The trust update, extracted from the launcher and executed for real.

    Temp-then-rename stopped a reader seeing a torn file. It did nothing about a
    *lost update*, which was the half that remained: a writer landing between the
    read and the rename has its change discarded, on the one file the mounted
    sign-in now depends on. These tests run the launcher's own bytes against a
    temporary directory, so a regression shows up as an outcome rather than as a
    missing string.
    """

    def _block(self, tmp_path):
        """Extract the real python block from the script, retargeted into tmp_path."""
        source = SCRIPT.read_text()
        start = source.index('python3 - <<"PY"')
        end = source.index("\nPY\n", start) + len("\nPY\n")
        directory = tmp_path / ".claude"
        directory.mkdir(exist_ok=True)
        return source[start:end].replace("/home/agent/.claude", str(directory)), directory

    def _run(self, block):
        return subprocess.run(["sh", "-c", block], capture_output=True, text=True)

    def test_the_enclosing_configuration_body_succeeds_as_shell(self, tmp_path):
        source = SCRIPT.read_text()
        marker = 'docker exec -e AC_VENDOR="$vendor" "$(container_of "$task")" sh -c \'\n'
        start = source.index(marker) + len(marker)
        body = source[start : source.index("\n    ' || die", start)]
        assert "set -eu" in {line.strip() for line in body.splitlines()}
        work_config = tmp_path / "work" / ".claude"
        shared_config = tmp_path / "shared" / ".claude"
        work_config.mkdir(parents=True)
        shared_config.mkdir(parents=True)
        body = (
            body.replace('[ "$AC_VENDOR" = claude ] || exit 0', ":")
            .replace("/work/.claude", str(work_config))
            .replace("/home/agent/.claude", str(shared_config))
        )

        result = self._run(body)

        assert result.returncode == 0, result.stderr
        settings = json.loads((work_config / "settings.local.json").read_text())
        assert settings["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "64"
        trust = json.loads((shared_config / ".claude.json").read_text())
        assert trust["projects"]["/work"]["hasTrustDialogAccepted"] is True

    def test_the_enclosing_body_stops_when_local_settings_cannot_be_written(self, tmp_path):
        source = SCRIPT.read_text()
        marker = 'docker exec -e AC_VENDOR="$vendor" "$(container_of "$task")" sh -c \'\n'
        start = source.index(marker) + len(marker)
        body = source[start : source.index("\n    ' || die", start)]
        assert "set -eu" in {line.strip() for line in body.splitlines()}
        work = tmp_path / "work"
        work.mkdir()
        blocked_config = work / ".claude"
        blocked_config.write_text("not a directory")
        shared_config = tmp_path / "shared" / ".claude"
        shared_config.mkdir(parents=True)
        body = (
            body.replace('[ "$AC_VENDOR" = claude ] || exit 0', ":")
            .replace("/work/.claude", str(blocked_config))
            .replace("/home/agent/.claude", str(shared_config))
        )

        result = self._run(body)

        assert result.returncode != 0
        assert not (shared_config / ".claude.json").exists()

    def test_the_flag_is_written_once_and_not_rewritten_afterwards(self, tmp_path):
        """Once the flag is on the volume it stays there, so no later chamber has any
        reason to write at all. That is what shrinks the window against the writer no
        lock here can reach — the CLI itself, refreshing this same file."""
        block, directory = self._block(tmp_path)
        config = directory / ".claude.json"

        assert self._run(block).returncode == 0
        first = config.read_text()
        assert json.loads(first)["projects"]["/work"]["hasTrustDialogAccepted"] is True
        inode = config.stat().st_ino

        assert self._run(block).returncode == 0
        assert config.read_text() == first
        assert config.stat().st_ino == inode, "a chamber republished a file it already agreed with"

    def test_the_flag_keeps_what_the_cli_had_already_written(self, tmp_path):
        """The whole hazard is this file carrying the sign-in the CLI refreshes."""
        block, directory = self._block(tmp_path)
        config = directory / ".claude.json"
        config.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "kept"}, "projects": {"/other": {"x": 1}}})
        )

        assert self._run(block).returncode == 0

        merged = json.loads(config.read_text())
        assert merged["oauthAccount"] == {"emailAddress": "kept"}
        assert merged["projects"]["/other"] == {"x": 1}
        assert merged["projects"]["/work"]["hasTrustDialogAccepted"] is True

    def test_an_invalid_shared_config_is_not_replaced_with_an_empty_one(self, tmp_path):
        block, directory = self._block(tmp_path)
        config = directory / ".claude.json"
        config.write_text("not json")

        result = self._run(block)

        assert result.returncode != 0
        assert config.read_text() == "not json"

    def test_an_empty_shared_config_is_initialized(self, tmp_path):
        block, directory = self._block(tmp_path)
        config = directory / ".claude.json"
        config.write_text("")

        result = self._run(block)

        assert result.returncode == 0, result.stderr
        initialized = json.loads(config.read_text())
        assert initialized["projects"]["/work"]["hasTrustDialogAccepted"] is True

    def test_an_interleaved_update_under_the_same_lock_is_not_lost(self, tmp_path):
        """The interleaving regression CodeRabbit asked for.

        A competitor takes the same lock, holds it across a read and a write, and
        adds a key of its own. Unserialized, the launcher reads the file before that
        key exists and republishes it without — a lost update. Under the lock it
        waits, reads what the competitor wrote, and both survive.

        The login booth and refresh helper take this volume lock; dispatch and status do
        not. This pins the lock shared by the trust initializer and refresh helper.
        """
        block, directory = self._block(tmp_path)
        config = directory / ".claude.json"
        config.write_text(json.dumps({"projects": {}}))
        holding = tmp_path / "competitor-holds-the-lock"
        competitor = tmp_path / "competitor.py"
        competitor.write_text(
            "import fcntl, json, os, pathlib, tempfile, time\n"
            f"d = pathlib.Path({str(directory)!r})\n"
            'guard = open(d / ".credentials.autoclave.lock", "a+")\n'
            "fcntl.flock(guard.fileno(), fcntl.LOCK_EX)\n"
            'p = d / ".claude.json"\n'
            # Read first, publish last, and only then let the launcher start: that is
            # the interleaving a lost update needs. Reading after the launcher wrote
            # would preserve both by accident and prove nothing.
            "config = json.loads(p.read_text())\n"
            f"pathlib.Path({str(holding)!r}).write_text('held')\n"
            "time.sleep(1.0)\n"
            'config["refreshed"] = True\n'
            'fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".competitor.")\n'
            'with os.fdopen(fd, "w") as handle:\n'
            "    handle.write(json.dumps(config))\n"
            "os.replace(tmp, p)\n"
            "guard.close()\n"
        )

        process = subprocess.Popen(["python3", str(competitor)])
        try:
            deadline = time.monotonic() + 10
            while not holding.exists():
                assert time.monotonic() < deadline, "the competitor never took the lock"
                assert process.poll() is None, "the competitor exited before taking the lock"
                time.sleep(0.01)
            result = self._run(block)
        finally:
            assert process.wait(timeout=30) == 0, "the competitor failed"

        assert result.returncode == 0, result.stderr
        merged = json.loads(config.read_text())
        assert merged.get("refreshed") is True, "the competitor's update was lost"
        assert merged["projects"]["/work"]["hasTrustDialogAccepted"] is True

    def test_the_wait_for_the_lock_is_bounded_rather_than_indefinite(self):
        """Nobody is in a chamber to notice a creation that hangs forever."""
        source = SCRIPT.read_text()
        start = source.index('python3 - <<"PY"')
        block = source[start : source.index("\nPY\n", start)]
        assert "LOCK_NB" in block and "time.monotonic()" in block, (
            "the trust update waits on the shared lock without a deadline"
        )


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
