"""The half of the start command that runs on the laptop, Mac or Linux: it reads the hard
maximum and prints the container command, or refuses and prints nothing.

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


@pytest.fixture
def env():
    return {key: value for key, value in os.environ.items() if key != "VERBATUS_HARD_MAX_SECONDS"}


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
    env.pop("VERBATUS_HARD_MAX_SECONDS", None)
    if sealed is not None:
        env["VERBATUS_HARD_MAX_SECONDS"] = sealed
    result = subprocess.run(
        ["sh", str(START_COMMAND), hours, "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "hard maximum" in result.stderr
    assert result.stdout == ""


def test_the_start_command_accepts_the_hard_maximum_itself(env):
    env.pop("VERBATUS_HARD_MAX_SECONDS", None)
    result = subprocess.run(
        ["sh", str(START_COMMAND), "3", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("policy", [None, 'hard_max_seconds = "10800"\n', "state = 1\n"])
def test_the_start_command_refuses_when_the_hard_maximum_cannot_be_read(env, tmp_path, policy):
    env.pop("VERBATUS_HARD_MAX_SECONDS", None)
    checkout = tmp_path / "checkout"
    (checkout / "operations" / "pod").mkdir(parents=True)
    script = checkout / "operations" / "pod" / "pod_start_command.sh"
    shutil.copy(START_COMMAND, script)
    if policy is not None:
        (checkout / "config").mkdir()
        (checkout / "config" / "spend.toml").write_text(policy)
    result = subprocess.run(
        ["sh", str(script), "1", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "hard maximum" in result.stderr and result.stdout == ""


@pytest.mark.parametrize("sealed", ["", "abc", "0", " 10800", "1_0800"])
def test_the_start_command_refuses_an_unusable_sealed_hard_maximum(env, sealed):
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
