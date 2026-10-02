"""The session-end check pings about pods that still exist, and about a listing it could not make.

runpodctl and notify.sh are stand-ins in a copied tree, so nothing here reaches RunPod or a phone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("session_end_pod_check.sh")

HEADER = "ID              NAME    GPU             IMAGE NAME      STATUS"


@pytest.fixture
def check(tmp_path):
    script = tmp_path / "operations" / "pod" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, script)
    notify = tmp_path / "operations" / "notify" / "notify.sh"
    notify.parent.mkdir(parents=True)
    sent = tmp_path / "sent.txt"
    notify.write_text(f'printf "%s\\n" "$*" >> "{sent}"\nexit "${{FAKE_NOTIFY_EXIT:-0}}"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    listing = tmp_path / "listing.txt"
    runpodctl = bin_dir / "runpodctl"
    runpodctl.write_text(
        '#!/bin/sh\n[ "$*" = "get pod" ] || exit 2\n'
        '[ "${FAKE_LIST_FAIL:-}" = yes ] && exit 1\n'
        f'cat "{listing}"\n'
    )
    runpodctl.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home)}

    def run(rows: list[str], **extra: str) -> list[str]:
        listing.write_text("\n".join([HEADER, *rows]) + "\n")
        before = sent.read_text().splitlines() if sent.exists() else []
        subprocess.run(["sh", str(script)], env={**env, **extra}, check=True, timeout=10)
        # The check runs in the background; wait for its subshell to finish writing.
        deadline = time.monotonic() + 5
        marker = home / ".cache" / "verbatus" / "pods-reported"
        while time.monotonic() < deadline:
            now = sent.read_text().splitlines() if sent.exists() else []
            if len(now) > len(before) and (marker.exists() or extra.get("FAKE_NOTIFY_EXIT")):
                return now[len(before) :]
            time.sleep(0.05)
        return []

    return run


def test_a_running_and_a_stopped_pod_are_both_reported(check):
    sent = check(
        [
            "abc123          proof   RTX A5000       runpod/x        RUNNING",
            "def456          old     RTX A5000       runpod/x        EXITED",
        ]
    )
    assert sent == [
        "decision RunPod pods still exist after a Claude session closed: abc123 def456 "
    ]


def test_no_pod_sends_nothing(check):
    assert check([]) == []


def test_a_failed_listing_is_reported_not_read_as_no_pods(check):
    sent = check([], FAKE_LIST_FAIL="yes")
    assert len(sent) == 1
    assert "Could not list RunPod pods" in sent[0]


def test_the_same_pods_are_not_reported_twice_within_two_hours(check):
    rows = ["abc123          proof   RTX A5000       runpod/x        RUNNING"]
    assert len(check(rows)) == 1
    assert check(rows) == []


def test_a_failed_ping_is_tried_again_at_the_next_session_end(check):
    rows = ["abc123          proof   RTX A5000       runpod/x        RUNNING"]
    assert len(check(rows, FAKE_NOTIFY_EXIT="1")) == 1
    assert len(check(rows)) == 1
