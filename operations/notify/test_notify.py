from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from operations.notify import client

SOURCE = Path(__file__).with_name("notify.sh")
GATE = SOURCE.parents[2] / ".githooks" / "check-all.sh"

# The real topic is a bearer secret and notify.sh reads these from the environment:
# the fixture strips the whole prefix, so a variable added later cannot start leaking.
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
            # The fake curl wins; the inherited PATH follows for python3, which a venv,
            # pyenv or Homebrew-only box does not keep under /usr/bin.
            "PATH": f"{binary}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
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


def test_the_checkouts_own_venv_python_is_preferred_over_paths(notify_repo, tmp_path):
    """A failing PATH `python3` is never reached while `.venv` holds an interpreter."""
    script, env = notify_repo

    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        f"""#!/bin/sh
touch "{tmp_path}/venv-python-used"
exec {shlex.quote(sys.executable)} "$@"
""",
        encoding="utf-8",
    )
    venv_python.chmod(0o755)

    path_python3 = tmp_path / "bin" / "python3"
    path_python3.write_text(
        f"""#!/bin/sh
touch "{tmp_path}/path-python-used"
exit 1
""",
        encoding="utf-8",
    )
    path_python3.chmod(0o755)

    result = run(script, env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "venv-python-used").exists()
    assert not (tmp_path / "path-python-used").exists()


@pytest.mark.parametrize(("status", "exit_code"), [("500", "0"), ("204", "7")])
@pytest.mark.full
def test_waiting_event_fails_when_delivery_is_not_confirmed(notify_repo, status, exit_code):
    script, env = notify_repo
    env.update({"FAKE_STATUS": status, "FAKE_EXIT": exit_code})
    result = run(script, env)
    assert result.returncode == 1
    assert "NOT DELIVERED" in result.stderr


@pytest.mark.full
@pytest.mark.parametrize("event", ["start", "milestone", "decision", "done"])
def test_every_event_reports_a_failed_delivery_honestly(notify_repo, event):
    """A session survives a lost ping through the async hook, not a false exit 0."""
    script, env = notify_repo
    env["FAKE_STATUS"] = "503"
    result = run(script, env, event)
    assert result.returncode == 1
    assert "NOT DELIVERED" in result.stderr


@pytest.mark.full
def test_the_session_start_hook_is_declared_async_so_a_failure_cannot_block(tmp_path):
    # If this stops being async, a failed start ping could fail the session.
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text(
            encoding="utf-8"
        )
    )
    entries = [
        hook
        for block in settings["hooks"]["SessionStart"]
        for hook in block["hooks"]
        if "notify.sh" in hook["command"]
    ]
    assert entries, "no SessionStart hook invokes notify.sh"
    for hook in entries:
        assert hook.get("async") is True, "a failed start ping could now block the session"


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


def test_the_test_sink_topic_echoes_the_message_and_spawns_no_curl(notify_repo):
    """Exit 0, so the guard does not change what the suites measure; the swallowed
    message stays visible on stderr."""
    script, env = notify_repo
    env["NTFY_TOPIC"] = "verbatus-test-sink"
    result = run(script, env, "milestone", "a message no phone should see")
    assert result.returncode == 0
    assert not Path(env["FAKE_ARGS"]).exists()
    assert not Path(env["FAKE_BODY"]).exists()
    assert "test sink" in result.stderr
    assert "milestone" in result.stderr
    assert "a message no phone should see" in result.stderr

    # The bridges read exit 0 as delivered; this stdout marker is what tells them apart.
    assert result.stdout == "NOTIFY_SUPPRESSED verbatus-test-sink\n"


def test_a_delivered_notification_writes_nothing_on_stdout(notify_repo):
    """The suppression marker is unambiguous only while nothing else writes stdout."""
    script, env = notify_repo
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.full
@pytest.mark.parametrize("event", ["start", "milestone", "decision", "done"])
def test_a_delivered_notification_prints_one_line_on_stderr(notify_repo, event):
    """Silence must never read as a lost ping: a session once resent three `done` pings."""
    script, env = notify_repo
    result = run(script, env, event)
    assert result.returncode == 0, result.stderr
    assert result.stderr == f"notify: delivered ({event})\n"
    assert result.stdout == ""


def test_a_closed_stderr_does_not_turn_a_delivery_into_a_failure(notify_repo):
    """Under `set -e` a failing diagnostic write would get an accepted post resent."""
    script, env = notify_repo
    result = subprocess.run(
        ["sh", "-c", 'exec "$1" "$2" "$3" 2>&-', "sh", str(script), "done", "finished"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""
    assert Path(env["FAKE_ARGS"]).exists()


def test_a_failed_delivery_prints_no_delivered_line(notify_repo):
    script, env = notify_repo
    env["FAKE_STATUS"] = "503"
    result = run(script, env)
    assert result.returncode == 1
    assert "NOT DELIVERED" in result.stderr
    assert "notify: delivered" not in result.stderr


def test_the_test_sink_prints_no_delivered_line(notify_repo):
    script, env = notify_repo
    env["NTFY_TOPIC"] = "verbatus-test-sink"
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    assert "notify: delivered" not in result.stderr


def test_a_suppressed_start_prints_no_delivered_line(notify_repo):
    script, env = notify_repo
    seed_stamp(script, seconds_ago=60)
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert "suppressed" in result.stderr
    # The suppression line says "already delivered"; only the delivery line is refused.
    assert "notify: delivered" not in result.stderr


def test_the_test_sink_is_a_literal_not_a_prefix(notify_repo):
    """A prefix rule would let one mistyped character silently stop every notification."""
    script, env = notify_repo
    env["NTFY_TOPIC"] = "verbatus-test-sink-2"
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    body = json.loads(Path(env["FAKE_BODY"]).read_text(encoding="utf-8"))
    assert body["topic"] == "verbatus-test-sink-2"
    # Nor the marker, or a bridge would report a delivered notification as swallowed.
    assert "NOTIFY_SUPPRESSED" not in result.stdout


def test_the_conftest_sink_matches_the_topic_the_script_recognises():
    """Two languages, neither importing the other: only this comparison holds them together."""
    import conftest

    source = SOURCE.read_text(encoding="utf-8")
    assert f'"$topic" = "{conftest.NOTIFY_TEST_SINK_TOPIC}"' in source


def _gate_sink_block() -> str:
    """check-all.sh's lines from reading the sink topic through running pytest.

    Lifted verbatim to be executed, not read: a text match would pass a wrong value, an
    assignment that never reaches the child, or a refusal that carries on.
    """
    lines = GATE.read_text(encoding="utf-8").splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("NTFY_TOPIC=$(sed")), None)
    assert first is not None, "check-all.sh no longer reads the sink topic from conftest.py"
    last = next((i for i in range(first, len(lines)) if "-m pytest" in lines[i]), None)
    assert last is not None, "check-all.sh no longer runs pytest after reading the sink topic"
    # The pytest line sits inside the `--parallel` if, so run through its closing `fi`.
    end = next((i for i in range(last, len(lines)) if lines[i].strip() == "fi"), last)
    return "\n".join(lines[first : end + 1])


def _run_gate_sink_block(
    tmp_path: Path, root: Path, *, parallel: str = "no"
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run that block with a sentinel as `$frozen_python`, recording the `NTFY_TOPIC` it
    inherited and its arguments, so the child's view is observed, not inferred."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "sentinel-record"
    sentinel = tmp_path / "sentinel-python"
    sentinel.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "${NTFY_TOPIC-<unset>}" "$@" > "$RECORD"\n',
        encoding="utf-8",
    )
    sentinel.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith(PREFIX)}
    env["RECORD"] = str(record)
    result = subprocess.run(
        [
            "sh",
            "-c",
            'set -eu\nroot="$1"\nfrozen_python="$2"\nparallel="$3"\n' + _gate_sink_block(),
            "sh",
            str(root),
            str(sentinel),
            parallel,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=30,
    )
    return result, record


def test_the_gate_hands_the_sink_topic_to_the_process_it_runs_the_suite_with(tmp_path):
    """Run against the real checkout: the value the child inherits is the constant."""

    import conftest

    result, record = _run_gate_sink_block(tmp_path, SOURCE.parents[2])

    assert result.returncode == 0, result.stderr
    assert record.exists(), "the gate never reached the process it runs the suite with"
    inherited, *arguments = record.read_text(encoding="utf-8").splitlines()
    assert inherited == conftest.NOTIFY_TEST_SINK_TOPIC
    assert arguments == ["-m", "pytest"]
    result, record = _run_gate_sink_block(tmp_path / "parallel", SOURCE.parents[2], parallel="yes")
    assert result.returncode == 0, result.stderr
    inherited, *arguments = record.read_text(encoding="utf-8").splitlines()
    assert inherited == conftest.NOTIFY_TEST_SINK_TOPIC
    assert arguments == ["-m", "pytest", "-p", "xdist", "-n", "4", "--dist", "loadfile"]


def test_the_gate_refuses_to_run_when_the_sink_topic_cannot_be_read(tmp_path):
    """An empty `NTFY_TOPIC` means the real topic, so the gate must stop before the suite;
    an unwritten sentinel record is a suite never reached."""

    synthetic = tmp_path / "repo"
    synthetic.mkdir()
    (synthetic / "conftest.py").write_text(
        'NOTIFY_TEST_SINK_TOPIC_RENAMED = "verbatus-test-sink"\n', encoding="utf-8"
    )

    result, record = _run_gate_sink_block(tmp_path, synthetic)

    assert result.returncode == 1
    assert not record.exists(), "the gate ran the suite after failing to read the sink topic"
    assert "could not read NOTIFY_TEST_SINK_TOPIC" in result.stderr


def test_server_override_is_refused(notify_repo):
    script, env = notify_repo
    env["NTFY_SERVER"] = "https://example.test"
    result = run(script, env)
    assert result.returncode == 2
    assert not Path(env["FAKE_ARGS"]).exists()


def test_ambient_topic_never_reaches_the_script_or_its_output(monkeypatch, request):
    # Set before the fixture is built, as on a machine that exported the live topic.
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


# The start stamp: suppression is the one path that can lose a notification, so every
# ambiguous stamp below must resolve to SENT.

STAMP = "private/.notify-start-stamp"
WINDOW_S = 900


def repo_root(script: Path) -> Path:
    """The tree notify.sh resolves as its root — the fixture's tmp_path, not this clone."""
    return script.parents[2]


def stamp_path(script: Path) -> Path:
    return repo_root(script) / STAMP


def seed_stamp(script: Path, *, seconds_ago: int) -> Path:
    """Write a stamp as the script does, aged by the recorded epoch second: never by
    sleeping, and never by shortening the shipped window."""
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
    # End to end, the burst the SessionStart hook produces.
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
    # A rate limit here could swallow a real result or a decision a session waits on.
    script, env = notify_repo
    seed_stamp(script, seconds_ago=1)
    result = run(script, env, event)
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), f"{event} was suppressed by a start stamp"


def test_a_symlinked_stamp_is_not_trusted_and_is_not_written_through(notify_repo):
    # Read, a link to a busy file suppresses every start; written, any link redirects
    # the write out of private/.
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
    # A blocking read from a hook is a session that never starts, and nothing says why.
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
    script, env = notify_repo
    stamp_path(script).write_text(contents, encoding="utf-8")
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    assert curl_ran(env), "an unreadable stamp swallowed the ping"
    assert "no readable timestamp" in result.stderr


def test_a_future_dated_stamp_does_not_suppress_a_start(notify_repo):
    # A negative age is still "less than fifteen minutes": skew would suppress every start.
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
    assert first.returncode == 1
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
    # In the field the stamp becomes unwritable through private/'s permissions.
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
    # The one file this script writes, beside the config: where the topic would leak.
    script, env = notify_repo
    env["NTFY_TOPIC"] = "stamp_leak_topic"
    result = run(script, env, "start")
    assert result.returncode == 0, result.stderr
    written = stamp_path(script).read_text(encoding="utf-8")
    assert written.strip().isdigit()
    for text in (written, result.stdout, result.stderr):
        assert "stamp_leak_topic" not in text


def test_the_client_reports_the_real_script_under_the_sink_as_suppressed(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "verbatus-test-sink")
    monkeypatch.delenv("NTFY_SERVER", raising=False)

    outcome = client.send("milestone", "a message no phone should see")

    assert (outcome.attempted, outcome.delivered, outcome.suppressed) == (True, False, True)
    assert outcome.line() == "Phone notification: suppressed (test sink)."


@pytest.mark.parametrize(
    ("status", "delivered", "detail"),
    [("204", True, "delivered"), ("503", False, "notify: NOT DELIVERED (milestone)")],
)
def test_the_client_reads_delivery_and_failure_from_the_script(
    notify_repo, status, delivered, detail
):
    script, env = notify_repo
    env["FAKE_STATUS"] = status

    def runner(argv):
        return subprocess.run(
            ["sh", str(script), *argv[2:]], capture_output=True, text=True, env=env, timeout=10
        )

    outcome = client.send("milestone", "finished", runner=runner)

    assert outcome.attempted and not outcome.suppressed
    assert outcome.delivered is delivered
    assert outcome.detail.startswith(detail)


def _raises(error: BaseException):
    def runner(argv):
        raise error

    return runner


@pytest.mark.parametrize(
    ("runner", "detail"),
    [
        (_raises(subprocess.TimeoutExpired(["sh"], 10.0)), "did not answer within 10 seconds"),
        (_raises(OSError("no shell here")), "could not run: no shell here"),
        (_raises(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")), "failed unexpectedly"),
        (lambda argv: subprocess.CompletedProcess(argv, 1, "", "x" * 500), "x" * 160),
    ],
)
def test_the_client_turns_every_failure_into_a_not_delivered_outcome(runner, detail):
    outcome = client.send("milestone", "finished", runner=runner)

    assert outcome.attempted and not outcome.delivered and not outcome.suppressed
    assert detail in outcome.detail
    assert len(outcome.detail) < 220
    assert outcome.line().endswith("The result above still stands.")


def test_the_client_lets_a_keyboard_interrupt_through():
    with pytest.raises(KeyboardInterrupt):
        client.send("milestone", "finished", runner=_raises(KeyboardInterrupt()))


@pytest.mark.parametrize("message", ["two\nlines", "", "   ", "nul\x00byte"])
def test_the_client_never_runs_the_script_for_a_malformed_message(message):
    outcome = client.send("milestone", message, runner=_raises(AssertionError("ran")))

    assert not outcome.attempted
    assert outcome.detail == "the message was not one non-empty line"


def test_the_client_bounds_the_script_with_its_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        client.subprocess, "run", lambda argv, **kwargs: seen.update(kwargs) or None
    )

    client.run(["sh"])

    assert seen["timeout"] == client.NOTIFY_TIMEOUT_SECONDS
