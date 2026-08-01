"""Outcome tests for the bounded, read-only Codex seat wrapper."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEAT = ROOT / "operations" / "codex" / "seat.sh"
SEATS = ROOT / "operations" / "codex" / "seats.conf"


def clean_env():
    env = dict(os.environ)
    for name in (
        "CODEX_SEATS_FILE",
        "CODEX_SEAT_TIMEOUT",
        "CODEX_SEAT_DRYRUN",
    ):
        env.pop(name, None)
    return env


def run(*args, seats=None, stdin=None, dryrun=True, env=None):
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
        input=stdin,
        env=env or clean_env(),
        timeout=15,
    )


def write_seats(tmp_path, body):
    path = tmp_path / "seats.conf"
    path.write_text(body)
    return path


def parse_seats():
    parsed = {}
    for raw in SEATS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, model, effort, timeout_s = line.split()
        assert name not in parsed
        parsed[name] = (model, effort, timeout_s)
    return parsed


def fake_executable(directory, name, body):
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)
    return path


def test_roster_is_small_read_only_evidence_surface():
    seats = parse_seats()
    assert set(seats) == {
        "scout",
        "judge",
        "audit-sol",
        "audit-terra",
        "design-sol",
        "design-terra",
        "bulk-luna",
    }
    assert "orchestrate" not in seats
    assert all(len(fields) == 3 for fields in seats.values())


def test_paired_design_seats_differ_only_by_model():
    # Same shape as the audit pair, and for the same reason: two vendors' models
    # answering an identical question is the evidence; a difference in effort or
    # deadline between them would make the comparison mean something else.
    sol_model, *sol_policy = parse_seats()["design-sol"]
    terra_model, *terra_policy = parse_seats()["design-terra"]
    assert sol_model != terra_model
    assert sol_policy == terra_policy


def test_paired_audits_differ_only_by_model():
    sol_model, *sol_policy = parse_seats()["audit-sol"]
    terra_model, *terra_policy = parse_seats()["audit-terra"]
    assert sol_model != terra_model
    assert sol_policy == terra_policy


@pytest.mark.parametrize("name", sorted(parse_seats()))
def test_each_seat_resolves_to_fixed_read_only_command(name):
    model, effort, timeout_s = parse_seats()[name]
    result = run(name, "inspect this")
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("-m") + 1] == model
    assert argv[argv.index("-c") + 1] == f"model_reasoning_effort={effort}"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert Path(argv[argv.index("-C") + 1]) == ROOT
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--strict-config" in argv
    assert argv.index("--") < len(argv) - 1
    assert argv[-1] == "inspect this"
    assert f"Time budget: {timeout_s}s." in result.stdout


@pytest.mark.parametrize("prompt", ["--help", "resume"])
def test_prompt_cannot_become_an_option_or_subcommand(prompt):
    result = run("judge", prompt)
    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == prompt


def test_stdin_prompt_is_drained_before_codex():
    result = run("judge", "-", stdin="from stdin\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "from stdin"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("a m high\n", "exactly four fields"),
        ("a m high 60 extra\n", "exactly four fields"),
        ("a --flag high 60\n", "plain model"),
        ("a model ultra 60\n", "allowed effort"),
        ("a model high 0\n", "between 1"),
        ("a model high 3601\n", "between 1"),
    ],
)
def test_malformed_seat_is_refused(tmp_path, body, message):
    seats = write_seats(tmp_path, body)
    result = run("a", "x", seats=seats)
    assert result.returncode == 2
    assert message in result.stderr


def test_duplicate_or_unknown_seat_is_refused(tmp_path):
    duplicate = write_seats(tmp_path, "a m low 60\na n high 60\n")
    result = run("a", "x", seats=duplicate)
    assert result.returncode == 2
    assert "declared 2 times" in result.stderr

    result = run("missing", "x")
    assert result.returncode == 2
    assert "no seat named" in result.stderr


def test_alternate_roster_cannot_be_used_live(tmp_path):
    seats = write_seats(tmp_path, "a m low 60\n")
    result = subprocess.run(
        ["sh", str(SEAT), "--seats", str(seats), "a", "x"],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=15,
    )
    assert result.returncode == 2
    assert "validation-only" in result.stderr


def test_ambient_override_variables_are_ignored():
    env = clean_env()
    env.update(
        {
            "CODEX_SEATS_FILE": "/tmp/untrusted",
            "CODEX_SEAT_TIMEOUT": "9999",
            "CODEX_SEAT_DRYRUN": "0",
        }
    )
    result = run("judge", "x", env=env)
    assert result.returncode == 0
    assert "gpt-5.6-sol" in result.stdout
    assert "timeout 600s" in result.stderr


@pytest.mark.full
def test_live_call_closes_stdin_and_passes_the_deadline(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    timeout_args = tmp_path / "timeout-args"
    fake_executable(
        bin_dir,
        "codex",
        "if IFS= read -r line; then exit 9; fi\nexit 0\n",
    )
    fake_executable(
        bin_dir,
        "timeout",
        f"printf '%s\\n' \"$@\" > '{timeout_args}'\nshift 2\nexec \"$@\"\n",
    )
    env = clean_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    # Feed stdin something that must not be read: seat.sh closes the child's
    # stdin with </dev/null, and under pytest an inherited stdin is empty
    # anyway, so without this line a regression that stopped closing it would
    # leave the fake codex's read failing for the wrong reason and stay green.
    result = run("judge", "x", stdin="must-not-be-read\n", dryrun=False, env=env)
    assert result.returncode == 0, result.stderr
    assert timeout_args.read_text().splitlines()[:4] == [
        "--kill-after=10",
        "600",
        "codex",
        "exec",
    ]


@pytest.mark.parametrize(
    ("status", "message"),
    [(124, "hit its 600s ceiling"), (137, "hard-killed")],
)
@pytest.mark.full
def test_cutoff_is_reported(tmp_path, status, message):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_executable(bin_dir, "codex", "exit 0\n")
    fake_executable(bin_dir, "timeout", f"exit {status}\n")
    env = clean_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    result = run("judge", "x", dryrun=False, env=env)
    assert result.returncode == status
    assert message in result.stderr
