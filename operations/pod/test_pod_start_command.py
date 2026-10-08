"""The half of the start command that runs on the laptop, Mac or Linux: it reads the
budget switch and, with the budget on, the hard maximum, and prints the container command,
or refuses and prints nothing.

The printed command and pod_guard.sh run only on the Linux pod; test_pod_guard.py tests
those, on Linux only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
GUARD = HERE / "pod_guard.sh"
START_COMMAND = HERE / "pod_start_command.sh"
SHA = "0" * 40


@pytest.fixture
def env():
    dropped = {"VERBATUS_HARD_MAX_SECONDS", "VERBATUS_POD_BUDGET"}
    return {key: value for key, value in os.environ.items() if key not in dropped}


def start(env, hours, script=START_COMMAND):
    return subprocess.run(["sh", str(script), hours, SHA], env=env, capture_output=True, text=True)


def checkout_with_policy(tmp_path, policy):
    """A copy of the start command beside `policy` as its config/spend.toml (None: no file)."""
    checkout = tmp_path / "checkout"
    (checkout / "operations" / "pod").mkdir(parents=True)
    script = checkout / "operations" / "pod" / "pod_start_command.sh"
    shutil.copy(START_COMMAND, script)
    if policy is not None:
        (checkout / "config").mkdir()
        (checkout / "config" / "spend.toml").write_text(policy)
    return script


def test_the_start_command_refuses_a_malformed_hours_value(env):
    result = subprocess.run(
        ["sh", str(START_COMMAND), "1.2.3", "0" * 40], env=env, capture_output=True
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    ("sealed", "hours"),
    [(None, "3.01"), (None, "4"), ("3600", "1.5")],
    ids=["checkout-just-over", "checkout-4h", "sealed"],
)
def test_the_start_command_refuses_hours_past_the_hard_maximum(env, sealed, hours):
    env["VERBATUS_POD_BUDGET"] = "on"
    if sealed is not None:
        env["VERBATUS_HARD_MAX_SECONDS"] = sealed
    result = subprocess.run(
        ["sh", str(START_COMMAND), hours, "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "hard maximum" in result.stderr
    assert result.stdout == ""


def test_the_start_command_accepts_the_hard_maximum_itself(env):
    env["VERBATUS_POD_BUDGET"] = "on"
    result = subprocess.run(
        ["sh", str(START_COMMAND), "3", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("policy", [None, 'hard_max_seconds = "10800"\n', "state = 1\n"])
def test_the_start_command_refuses_when_the_hard_maximum_cannot_be_read(env, tmp_path, policy):
    env["VERBATUS_POD_BUDGET"] = "on"
    script = checkout_with_policy(tmp_path, policy)
    result = subprocess.run(
        ["sh", str(script), "1", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "hard maximum" in result.stderr and result.stdout == ""


@pytest.mark.parametrize("sealed", ["", "abc", "0", " 10800", "1_0800"])
def test_the_start_command_refuses_an_unusable_sealed_hard_maximum(env, sealed):
    env["VERBATUS_POD_BUDGET"] = "on"
    env["VERBATUS_HARD_MAX_SECONDS"] = sealed
    result = subprocess.run(
        ["sh", str(START_COMMAND), "1", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "VERBATUS_HARD_MAX_SECONDS" in result.stderr and result.stdout == ""


def test_the_guard_keeps_its_records_under_the_volume_mount_the_bootstrap_requires():
    from operations.pod.models import POD_VOLUME_MOUNT_PATH

    expected = f"{POD_VOLUME_MOUNT_PATH}/.pod_guard"
    env = {key: value for key, value in os.environ.items() if key != "POD_GUARD_DIR"}
    printed = subprocess.run(
        ["sh", str(START_COMMAND), "1", "0" * 40], env=env, capture_output=True, text=True
    ).stdout
    assert f"${{POD_GUARD_DIR:-{expected}}}; export POD_GUARD_DIR=$d;" in printed
    assert f"dir=${{POD_GUARD_DIR:-{expected}}}" in GUARD.read_text()
    policy = json.loads((HERE.parents[1] / "config" / "data_handling_policy.json").read_text())
    assert POD_VOLUME_MOUNT_PATH in policy["storage_roots"]
    from operations.pod import pod_run

    assert expected == f"{POD_VOLUME_MOUNT_PATH}/{pod_run.POD_GUARD_DIRECTORY}"


@pytest.mark.parametrize("budget", ["on", "off"])
@pytest.mark.parametrize("hours", ["0", "0.0", ".0", "00"])
def test_the_start_command_refuses_no_time_at_all(env, budget, hours):
    """Zero hours would otherwise arm a one-second deadline."""
    env["VERBATUS_POD_BUDGET"] = budget
    result = start(env, hours)
    assert result.returncode == 2
    assert "more than 0" in result.stderr and result.stdout == ""


def test_with_the_budget_on_off_is_refused(env):
    env["VERBATUS_POD_BUDGET"] = "on"
    result = start(env, "off")
    assert result.returncode == 2
    assert "budget is on" in result.stderr and result.stdout == ""


def test_the_shipped_policy_has_the_budget_off_and_off_arms_no_deadline_or_backstop(env):
    result = start(env, "off")
    assert result.returncode == 0, result.stderr
    printed = result.stdout
    assert "runpodctl" not in printed
    assert 'rm -f "$d/deadline-$RUNPOD_POD_ID"' in printed and "echo $first" not in printed
    assert "created-$RUNPOD_POD_ID" in printed
    assert "POD_GUARD_DELETE=off sh /tmp/pod_guard.sh off)" in printed
    assert "POD_GUARD_FETCH_TRIES:-10" in printed


def test_with_the_budget_off_hours_need_no_hard_maximum(env, tmp_path):
    """The hours are the lead's deadline; the hard maximum is not read and does not cut it."""
    env["VERBATUS_HARD_MAX_SECONDS"] = "3600"
    result = start(env, "5")
    assert result.returncode == 0, result.stderr
    assert "first=$((start + 18000))" in result.stdout
    assert "runpodctl pod delete" in result.stdout
    script = checkout_with_policy(tmp_path, 'pod_budget = "off"\n')
    assert start({**env, "VERBATUS_HARD_MAX_SECONDS": "x"}, "2", script).returncode == 0


@pytest.mark.parametrize("policy", [None, "", 'pod_budget = "maybe"\n', "pod_budget = off\n"])
def test_the_start_command_refuses_when_the_budget_switch_cannot_be_read(env, tmp_path, policy):
    result = start(env, "off", checkout_with_policy(tmp_path, policy))
    assert result.returncode == 2
    assert "cannot read pod_budget" in result.stderr and result.stdout == ""


@pytest.mark.parametrize("value", ["", "yes", "OFF"])
def test_the_start_command_refuses_an_unusable_budget_override(env, value):
    env["VERBATUS_POD_BUDGET"] = value
    result = start(env, "off")
    assert result.returncode == 2
    assert "VERBATUS_POD_BUDGET" in result.stderr and result.stdout == ""


def test_the_budget_override_wins_over_the_policy(env, tmp_path):
    script = checkout_with_policy(tmp_path, 'pod_budget = "on"\nhard_max_seconds = 10800\n')
    assert start(env, "off", script).returncode == 2
    assert start({**env, "VERBATUS_POD_BUDGET": "off"}, "off", script).returncode == 0


@pytest.mark.parametrize(("line", "switch"), [('"on"', "on"), ('"off"', "off"), ('"On"', "off")])
def test_ladder_delete_in_the_policy_reaches_the_guard(env, tmp_path, line, switch):
    script = checkout_with_policy(tmp_path, f'pod_budget = "off"\nladder_delete = {line}\n')
    result = start(env, "off", script)
    assert result.returncode == 0, result.stderr
    assert f"POD_GUARD_DELETE={switch} sh /tmp/pod_guard.sh off)" in result.stdout
