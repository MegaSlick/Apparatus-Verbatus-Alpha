"""The pod guard deletes its own pod when idle or out of time, with a stand-in runpodctl."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

GUARD = Path(__file__).with_name("pod_guard.sh")


@pytest.fixture
def guard_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    fake = bin_dir / "runpodctl"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'case "$*" in "$FAKE_RUNPODCTL_FAIL"*) exit 1 ;; esac\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    marker = f"pod-guard-busy-{uuid.uuid4().hex}"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNPOD_POD_ID": "testpod",
        "POD_GUARD_DIR": str(tmp_path / "guard"),
        "POD_GUARD_INTERVAL": "1",
        "POD_GUARD_IDLE_SECONDS": "2",
        "POD_GUARD_BUSY": marker,
        "FAKE_RUNPODCTL_FAIL": "never",
    }
    return env, calls, marker, tmp_path / "guard" / "guard.log"


def run_guard(env, *args):
    return subprocess.run(
        ["sh", str(GUARD), *args], env=env, capture_output=True, text=True, timeout=30
    )


def test_an_idle_pod_is_deleted(guard_env):
    env, calls, _, log = guard_env
    result = run_guard(env, "5", "30")
    assert result.returncode == 0
    assert "pod delete testpod" in calls.read_text()
    assert "idle for" in log.read_text()


def test_a_busy_pod_is_deleted_when_its_time_is_up(guard_env):
    env, calls, marker, log = guard_env
    busy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])
    try:
        result = run_guard(env, "0.001", "30")
    finally:
        busy.kill()
    assert result.returncode == 0
    assert "approved time is up" in log.read_text()
    assert "idle for" not in log.read_text()
    assert "pod delete testpod" in calls.read_text()


def test_the_older_runpodctl_form_is_tried_when_the_newer_one_fails(guard_env):
    env, calls, _, _ = guard_env
    env["FAKE_RUNPODCTL_FAIL"] = "pod delete"
    result = run_guard(env, "5", "30")
    assert result.returncode == 0
    assert calls.read_text().splitlines()[-2:] == ["pod delete testpod", "remove pod testpod"]
