from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).with_name("notify.sh")

# The real topic is a bearer secret. notify.sh reads the topic and the server
# from the ambient environment, and its encoder reads the four presentation
# variables, so a suite that inherits any of them can write the live topic into
# a temporary file. Every variable in this namespace is stripped, by prefix, so
# one added to notify.sh later cannot quietly start leaking.
PREFIX = "NTFY" + "_"
NOTIFICATION_VARIABLES = tuple(
    PREFIX + name for name in ("TOPIC", "SERVER", "TITLE", "PRIORITY", "TAG", "MESSAGE")
)


@pytest.fixture
def notify_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    script = tmp_path / "operations" / "notify" / "notify.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(SOURCE, script)
    script.chmod(0o755)
    private = tmp_path / "private"
    private.mkdir()
    topic_key = PREFIX + "TOPIC"
    (private / "ntfy.conf").write_text(f"{topic_key}=test_topic\n", encoding="utf-8")

    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "curl"
    fake.write_text(
        """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_ARGS"
cat > "$FAKE_BODY"
printf '%s' "${FAKE_STATUS:-204}"
exit "${FAKE_EXIT:-0}"
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith(PREFIX)}
    env.update(
        {
            "PATH": f"{binary}:/usr/local/bin:/usr/bin:/bin",
            "FAKE_ARGS": str(tmp_path / "args"),
            "FAKE_BODY": str(tmp_path / "body"),
        }
    )
    return script, env


def run(script: Path, env: dict[str, str], event: str = "done", message: str = "finished"):
    return subprocess.run(
        [str(script), event, message],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=10,
    )


def test_success_sends_json_without_topic_in_curl_arguments(notify_repo):
    script, env = notify_repo
    result = run(script, env)
    assert result.returncode == 0
    arguments = Path(env["FAKE_ARGS"]).read_text(encoding="utf-8")
    body = json.loads(Path(env["FAKE_BODY"]).read_text(encoding="utf-8"))
    assert "test_topic" not in arguments
    assert body["topic"] == "test_topic"
    assert body["message"] == "finished"


@pytest.mark.parametrize(("status", "exit_code"), [("500", "0"), ("204", "7")])
@pytest.mark.full
def test_waiting_event_fails_when_delivery_is_not_confirmed(notify_repo, status, exit_code):
    script, env = notify_repo
    env.update({"FAKE_STATUS": status, "FAKE_EXIT": exit_code})
    result = run(script, env)
    assert result.returncode == 1
    assert "NOT DELIVERED" in result.stderr


@pytest.mark.full
def test_nonwaiting_event_reports_failure_but_does_not_block_session(notify_repo):
    script, env = notify_repo
    env["FAKE_STATUS"] = "503"
    result = run(script, env, "start")
    assert result.returncode == 0
    assert "NOT DELIVERED" in result.stderr


@pytest.mark.full
def test_environment_topic_overrides_private_config(notify_repo):
    script, env = notify_repo
    env["NTFY_TOPIC"] = "environment_topic"
    assert run(script, env).returncode == 0
    body = json.loads(Path(env["FAKE_BODY"]).read_text(encoding="utf-8"))
    assert body["topic"] == "environment_topic"


def test_missing_topic_is_explicit(notify_repo):
    script, env = notify_repo
    script.parents[2].joinpath("private/ntfy.conf").unlink()
    result = run(script, env)
    assert result.returncode == 1
    assert "no topic configured" in result.stderr


@pytest.mark.parametrize(
    ("event", "message"),
    [("other", "hello"), ("done", ""), ("done", "one\ntwo"), ("done", "one\rtwo")],
)
@pytest.mark.full
def test_invalid_interface_never_contacts_server(notify_repo, event, message):
    script, env = notify_repo
    result = run(script, env, event, message)
    assert result.returncode == 2
    assert not Path(env["FAKE_ARGS"]).exists()


def test_invalid_topic_is_not_echoed(notify_repo):
    script, env = notify_repo
    secret = "bad/topic"
    env["NTFY_TOPIC"] = secret
    result = run(script, env)
    assert result.returncode == 1
    assert secret not in result.stderr
    assert not Path(env["FAKE_ARGS"]).exists()


def test_server_override_is_refused(notify_repo):
    script, env = notify_repo
    env["NTFY_SERVER"] = "https://example.test"
    result = run(script, env)
    assert result.returncode == 2
    assert not Path(env["FAKE_ARGS"]).exists()


def test_ambient_topic_never_reaches_the_script_or_its_output(monkeypatch, request):
    # The fixture is built after the ambient variable is set, which is the order
    # a real machine presents: the operator exported the live topic long before
    # pytest started.
    leaked = "ambient" + "_bearer_topic"
    monkeypatch.setenv(PREFIX + "TOPIC", leaked)
    script, env = request.getfixturevalue("notify_repo")
    assert [name for name in env if name.startswith(PREFIX)] == []
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    arguments = Path(env["FAKE_ARGS"]).read_text(encoding="utf-8")
    body = Path(env["FAKE_BODY"]).read_text(encoding="utf-8")
    assert json.loads(body)["topic"] == "test_topic"
    for text in (arguments, body, result.stdout, result.stderr):
        assert leaked not in text


@pytest.mark.parametrize("variable", NOTIFICATION_VARIABLES)
def test_no_ambient_notification_variable_changes_the_run(monkeypatch, request, variable):
    monkeypatch.setenv(variable, "ambient" + "_value")
    script, env = request.getfixturevalue("notify_repo")
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    body = json.loads(Path(env["FAKE_BODY"]).read_text(encoding="utf-8"))
    assert body["topic"] == "test_topic"
    assert body["title"] == "Session complete"
    assert body["message"] == "finished"
    assert "ambient" + "_value" not in Path(env["FAKE_ARGS"]).read_text(encoding="utf-8")


# --- the start stamp is evidence, and is checked like evidence ---------------
#
# `start` fires from the SessionStart hook, and the desktop app opens several
# sessions per launch, so an unsuppressed start is a burst of identical pings
# every morning. Suppression is the one path in this script that deliberately
# does not send, which makes it the one path that can lose a notification, so
# every ambiguous stamp below must resolve to SENT. The old check asked
# `find "$stamp" -mmin -15` — "did anything here change recently" — and each
# object below answered yes without being a stamp this script ever wrote.

STAMP = "private/.notify-start-stamp"
WINDOW_S = 900


def repo_root(script: Path) -> Path:
    """The tree notify.sh resolves as its root — the fixture's tmp_path, not this clone."""
    return script.parents[2]


def stamp_path(script: Path) -> Path:
    return repo_root(script) / STAMP


def seed_stamp(script: Path, *, seconds_ago: int) -> Path:
    """Write a stamp the way the script writes one, aged by the given offset.

    The window is driven by the recorded epoch second, never by sleeping: a
    suite that waits fifteen minutes to prove a fifteen-minute window would not
    be run, and one that shortens the window is not testing the shipped value.
    """
    path = stamp_path(script)
    written = int(time.time()) - seconds_ago
    path.write_text(f"{written}\n", encoding="utf-8")
    return path


def curl_ran(env: dict[str, str]) -> bool:
    return Path(env["FAKE_ARGS"]).exists()


def forget_curl(env: dict[str, str]) -> None:
    Path(env["FAKE_ARGS"]).unlink(missing_ok=True)
    Path(env["FAKE_BODY"]).unlink(missing_ok=True)


def test_a_start_inside_the_window_is_suppressed(notify_repo):
    script, env = notify_repo
    seed_stamp(script, seconds_ago=60)
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert "suppressed" in result.stderr
    assert not curl_ran(env), "a fresh stamp did not suppress the duplicate ping"


def test_a_second_start_after_a_delivered_one_is_suppressed(notify_repo):
    # End to end: the first ping writes the stamp, the second reads it. This is
    # the burst the SessionStart hook actually produces.
    script, env = notify_repo
    first = run(script, env, "start")
    assert first.returncode == 0, first.stderr
    assert curl_ran(env)
    assert stamp_path(script).exists(), "a delivered start left no stamp"
    forget_curl(env)

    second = run(script, env, "start")
    assert second.returncode == 0, second.stderr
    assert not curl_ran(env)
    assert "already delivered" in second.stderr
    assert "attempted" not in second.stderr
    assert "NOT DELIVERED" not in second.stderr


def test_a_start_outside_the_window_is_sent(notify_repo):
    script, env = notify_repo
    seed_stamp(script, seconds_ago=WINDOW_S + 1)
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "an expired stamp suppressed a start ping"


@pytest.mark.parametrize("event", ["milestone", "decision", "done"])
def test_a_fresh_stamp_never_suppresses_a_deliberate_event(notify_repo, event):
    # A rate limit on a deliberate event could swallow a real result, and a
    # decision ping is what a blocked session is waiting on.
    script, env = notify_repo
    seed_stamp(script, seconds_ago=1)
    result = run(script, env, event)
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), f"{event} was suppressed by a start stamp"


def test_a_symlinked_stamp_is_not_trusted_and_is_not_written_through(notify_repo):
    # The stamp is read AND written. A link aimed at a file the machine rewrites
    # often would suppress every start ping; a link aimed anywhere at all would
    # redirect this script's own write out of private/. There is no legitimate
    # use for a symlinked suppression stamp, so both are refused — the opposite
    # of the ruling on the config file above, on purpose.
    script, env = notify_repo
    target = repo_root(script) / "busy-file"
    target.write_text("", encoding="utf-8")
    stamp_path(script).symlink_to(target)

    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "a symlinked stamp swallowed the ping"
    assert "symlink" in result.stderr
    assert target.read_text(encoding="utf-8") == "", "the stamp write followed the symlink out"


def test_a_fifo_at_the_stamp_path_does_not_suppress_a_start(notify_repo):
    # Reading a FIFO blocks until something writes, and this runs from a hook:
    # a blocking read is a session that never starts, with nothing to explain it.
    script, env = notify_repo
    os.mkfifo(stamp_path(script))
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "a FIFO at the stamp path swallowed the ping"
    assert "not a regular file" in result.stderr


def test_a_directory_at_the_stamp_path_does_not_suppress_a_start(notify_repo):
    script, env = notify_repo
    stamp_path(script).mkdir()
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "a directory at the stamp path swallowed the ping"
    assert "not a regular file" in result.stderr


@pytest.mark.parametrize("contents", ["", "\n", "not-a-timestamp\n", "-60\n", "12 34\n"])
def test_a_stamp_without_a_readable_timestamp_does_not_suppress(notify_repo, contents):
    # Includes every stamp the older touch-based version left behind: empty.
    script, env = notify_repo
    stamp_path(script).write_text(contents, encoding="utf-8")
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "an unreadable stamp swallowed the ping"
    assert "no readable timestamp" in result.stderr


def test_a_future_dated_stamp_does_not_suppress_a_start(notify_repo):
    # A negative age is still "less than fifteen minutes". One clock skew, or one
    # stray `touch -t`, and every start is suppressed until the date passes.
    script, env = notify_repo
    seed_stamp(script, seconds_ago=-3600)
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "a future-dated stamp swallowed the ping"
    assert "future" in result.stderr


def test_a_failed_start_writes_no_stamp_and_does_not_suppress_the_retry(notify_repo):
    script, env = notify_repo
    env["FAKE_STATUS"] = "503"
    first = run(script, env, "start")
    assert first.returncode == 0
    assert "NOT DELIVERED" in first.stderr
    assert not stamp_path(script).exists(), "a failed post recorded itself as delivered"
    forget_curl(env)

    del env["FAKE_STATUS"]
    second = run(script, env, "start")
    assert second.returncode == 0, second.stderr
    assert curl_ran(env), "a failed start suppressed its own retry"
    assert "suppressed" not in second.stderr


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_a_start_that_cannot_record_its_stamp_still_delivers_and_says_so(notify_repo):
    # The stamp lives in private/, so the way it becomes unwritable in the field
    # is a permission on that directory. The ping matters more than the stamp.
    script, env = notify_repo
    private = repo_root(script) / "private"
    private.chmod(0o555)
    try:
        result = run(script, env, "start")
    finally:
        private.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "the ping itself was lost"
    assert "could not record its suppression stamp" in result.stderr


def test_the_stamp_never_carries_the_topic(notify_repo):
    # The stamp is a clock reading. It lives beside the config file and is the
    # one thing this script writes, so it is the obvious place for the bearer
    # topic to end up by accident.
    script, env = notify_repo
    env["NTFY_TOPIC"] = "stamp_leak_topic"
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    written = stamp_path(script).read_text(encoding="utf-8")
    assert written.strip().isdigit()
    for text in (written, result.stdout, result.stderr):
        assert "stamp_leak_topic" not in text
