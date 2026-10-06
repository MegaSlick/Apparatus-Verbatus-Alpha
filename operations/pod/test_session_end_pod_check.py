"""The session-end check pings about pods that still exist, and about any listing it could not
read.

runpodctl and notify.sh are stand-ins in a copied tree, so nothing here reaches RunPod or a phone.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("session_end_pod_check.sh")

HEADER = "ID              NAME    GPU             IMAGE NAME      STATUS"
RUNNING = "abc123          proof   RTX A5000       runpod/x        RUNNING"
TOOLS = ("sh", "awk", "sort", "tr", "cat", "find", "mkdir", "dirname", "sleep", "mktemp", "rm")


@pytest.fixture
def check(tmp_path):
    script = tmp_path / "operations" / "pod" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, script)
    notify = tmp_path / "operations" / "notify" / "notify.sh"
    notify.parent.mkdir(parents=True)
    sent = tmp_path / "sent.txt"
    notify.write_text(f'printf "%s\\n" "$*" >> "{sent}"\nexit "${{FAKE_NOTIFY_EXIT:-0}}"\n')
    # Only the tools the script needs, so "runpodctl is missing" is a fact of this PATH.
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in TOOLS:
        (tools / name).symlink_to(shutil.which(name))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    listing = tmp_path / "listing.txt"
    runpodctl = bin_dir / "runpodctl"
    runpodctl.write_text(
        '#!/bin/sh\n[ "$*" = "get pod" ] || exit 2\n'
        '[ "${FAKE_LIST_FAIL:-}" = yes ] && exit 1\n'
        '[ "${FAKE_LIST_HANG:-}" = yes ] && exec sleep 60\n'
        f'cat "{listing}"\n'
    )
    runpodctl.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".cache" / "verbatus" / "pods-reported"

    def run(
        output: str, *, installed: bool = True, with_timeout: bool | None = None, **extra: str
    ) -> list[str]:
        listing.write_text(output)
        # By default the machine's own: macOS ships no `timeout`.
        if with_timeout is None:
            with_timeout = shutil.which("timeout") is not None
        timeout = tools / "timeout"
        if with_timeout and not timeout.exists():
            timeout.symlink_to(shutil.which("timeout"))
        if not with_timeout and timeout.exists():
            timeout.unlink()
        path = f"{bin_dir}:{tools}" if installed else str(tools)
        before = sent.read_text().splitlines() if sent.exists() else []
        marked = marker.read_text() if marker.exists() else None
        env = {"PATH": path, "HOME": str(home), **extra}
        subprocess.run([str(tools / "sh"), str(script)], env=env, check=True, timeout=10)
        # The check runs in the background: wait until its ping is sent and, unless the
        # ping fails, recorded.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            now = sent.read_text().splitlines() if sent.exists() else []
            recorded = marker.exists() and marker.read_text() != marked
            if len(now) > len(before) and (recorded or extra.get("FAKE_NOTIFY_EXIT")):
                return now[len(before) :]
            time.sleep(0.05)
        return []

    run.marker = marker
    return run


def table(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def test_every_listed_pod_is_reported_with_its_state(check):
    sent = check(
        table(
            RUNNING,
            "def456          old     RTX A5000       runpod/x        EXITED",
            "ghi789          new     RTX A5000       runpod/x        CREATED",
        )
    )
    assert sent == [
        "decision RunPod pods still exist after a Claude session closed: "
        "abc123:RUNNING def456:EXITED ghi789:CREATED "
    ]


def test_a_header_with_no_rows_sends_nothing(check):
    assert check(HEADER + "\n") == []


def test_an_empty_listing_is_reported_not_read_as_no_pods(check):
    sent = check("")
    assert len(sent) == 1
    assert "came back empty" in sent[0]
    assert check.marker.read_text() == "unlisted"


@pytest.mark.parametrize(
    "with_timeout",
    [
        pytest.param(
            True,
            id="timeout",
            marks=pytest.mark.skipif(
                shutil.which("timeout") is None, reason="this machine has no timeout"
            ),
        ),
        pytest.param(False, id="background-kill"),
    ],
)
def test_a_hung_listing_is_stopped_and_reported(check, with_timeout):
    started = time.monotonic()
    sent = check(
        table(RUNNING),
        with_timeout=with_timeout,
        FAKE_LIST_HANG="yes",
        SESSION_END_LIST_SECONDS="1",
    )
    assert len(sent) == 1
    assert "Could not list RunPod pods" in sent[0]
    assert time.monotonic() - started < 5


def test_a_failed_listing_is_reported_not_read_as_no_pods(check):
    sent = check("", FAKE_LIST_FAIL="yes")
    assert len(sent) == 1
    assert "Could not list RunPod pods" in sent[0]
    assert check.marker.read_text() == "unlisted"


def test_a_table_it_does_not_recognise_is_reported_not_read_as_no_pods(check):
    sent = check('[{"id": "abc123", "desiredStatus": "RUNNING"}]\n')
    assert len(sent) == 1
    assert "unrecognised table" in sent[0]
    assert check.marker.read_text() == "unlisted"


def test_a_missing_runpodctl_is_reported_by_name(check):
    sent = check(table(RUNNING), installed=False)
    assert len(sent) == 1
    assert "runpodctl is not installed" in sent[0]
    assert check.marker.read_text() == "runpodctl-missing"
    assert check(table(RUNNING), installed=False) == [], "reported once per two hours"


def test_the_same_pods_in_the_same_states_are_not_reported_twice_within_two_hours(check):
    assert len(check(table(RUNNING))) == 1
    assert check(table(RUNNING)) == []


def test_a_pod_that_changed_state_is_reported_again(check):
    assert len(check(table(RUNNING))) == 1
    stopped = RUNNING.replace("RUNNING", "EXITED")
    assert check(table(stopped)) == [
        "decision RunPod pods still exist after a Claude session closed: abc123:EXITED "
    ]


def test_a_failed_ping_is_tried_again_at_the_next_session_end(check):
    assert len(check(table(RUNNING), FAKE_NOTIFY_EXIT="1")) == 1
    assert not check.marker.exists()
    assert len(check(table(RUNNING))) == 1


def test_a_machine_without_timeout_still_runs_the_check(check, monkeypatch):
    """macOS ships no `timeout`; the script falls back to a background kill, and the
    fixture must not need one either."""
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "timeout" else which(name))
    sent = check(table(RUNNING))
    assert sent and "abc123:RUNNING" in sent[0]
