"""The pod guard deletes its own pod when idle or out of time, with stand-in runpodctl and GPU."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

GUARD = Path(__file__).with_name("pod_guard.sh")


@pytest.fixture
def guard(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    for name, body in {
        "runpodctl": (
            f'printf "%s\\n" "$*" >> "{calls}"\n'
            'case "$*" in "$FAKE_RUNPODCTL_FAIL"*) exit 1 ;; esac\n'
        ),
        "nvidia-smi": 'echo "${FAKE_GPU_UTIL:-0}"\n',
    }.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    state = tmp_path / "guard"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNPOD_POD_ID": "testpod",
        "POD_GUARD_DIR": str(state),
        "POD_GUARD_INTERVAL": "1",
        "POD_GUARD_IDLE_SECONDS": "2",
        "FAKE_RUNPODCTL_FAIL": "never",
    }
    return env, calls, state


def run_guard(env, *args, seconds=6):
    """Runs the guard for a few seconds; it never exits on its own once it starts deleting."""
    process = subprocess.Popen(["sh", str(GUARD), *args], env=env)
    time.sleep(seconds)
    process.kill()
    process.wait()


def test_a_pod_with_an_idle_gpu_is_deleted(guard):
    env, calls, state = guard
    run_guard(env, "5", "30")
    assert "pod delete testpod" in calls.read_text()
    assert "GPU idle for" in (state / "guard.log").read_text()


def test_a_busy_gpu_is_deleted_only_when_its_time_is_up(guard):
    env, calls, state = guard
    env["FAKE_GPU_UTIL"] = "80"
    run_guard(env, "0.001", "30")
    log = (state / "guard.log").read_text()
    assert "approved time is up" in log
    assert "GPU idle" not in log
    assert "pod delete testpod" in calls.read_text()


def test_a_fresh_keepalive_holds_off_the_idle_delete(guard):
    env, _, state = guard
    state.mkdir()
    (state / "keepalive-testpod").touch()
    run_guard(env, "0.001", "30")
    log = (state / "guard.log").read_text()
    assert "approved time is up" in log
    assert "GPU idle" not in log


def test_a_garbled_deadline_file_is_replaced_not_trusted(guard):
    env, calls, state = guard
    state.mkdir()
    (state / "deadline-testpod").write_text("2026-09-29\n")
    run_guard(env, "5", "30")
    assert (state / "deadline-testpod").read_text().strip().isdigit()
    assert "pod delete testpod" in calls.read_text()


def test_a_delete_that_reports_success_is_repeated_and_then_stopped(guard):
    env, calls, _ = guard
    run_guard(env, "5", "30", seconds=7)
    lines = calls.read_text().splitlines()
    assert lines.count("pod delete testpod") >= 3
    assert "pod stop testpod" in lines


def test_the_older_runpodctl_form_is_tried_when_the_newer_one_fails(guard):
    env, calls, _ = guard
    env["FAKE_RUNPODCTL_FAIL"] = "pod delete"
    run_guard(env, "5", "30", seconds=4)
    assert calls.read_text().splitlines()[:2] == ["pod delete testpod", "remove pod testpod"]


def test_an_unwritable_state_directory_exits_so_the_backstop_takes_over(guard, tmp_path):
    env, _, _ = guard
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    env["POD_GUARD_DIR"] = str(blocker / "guard")
    result = subprocess.run(["sh", str(GUARD), "5"], env=env, timeout=10)
    assert result.returncode == 3
