from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).with_name("notify.sh")
GATE = SOURCE.parents[2] / ".githooks" / "check-all.sh"

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
            # The fake bin comes first so its curl always wins; the inherited
            # PATH stays behind it because notify.sh also needs python3, which
            # a venv, pyenv or Homebrew-only box does not keep under /usr/bin.
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
    """`start` and `milestone` used to exit 0 after printing NOT DELIVERED.

    Two reviewers found it independently. The reason it was 0 — a session must not
    die because a ping did not land — is provided by `"async": true` on the
    SessionStart hook, not by the exit status, so the status is free to be true.
    """
    script, env = notify_repo
    env["FAKE_STATUS"] = "503"
    result = run(script, env, event)
    assert result.returncode == 1
    assert "NOT DELIVERED" in result.stderr


@pytest.mark.full
def test_the_session_start_hook_is_declared_async_so_a_failure_cannot_block(tmp_path):
    # The property that replaced the false success. If this ever stops being async,
    # a failed start ping could fail the session, and the reasoning above lapses.
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
    """The reserved topic that stops a test session from paging his phone.

    Its whole job is that the fake `curl` -- which stands where the real one
    would be -- is never reached, so that is what is asserted: no arguments
    file and no body file, the two things the fake writes the instant it runs.
    Exit 0 and not a refusal, because the guard must not change what the suite
    it is protecting measures; the swallowed message goes to stderr instead, so
    a leak stays visible to anyone reading it.
    """

    script, env = notify_repo
    env["NTFY_TOPIC"] = "verbatus-test-sink"
    result = run(script, env, "milestone", "a message no phone should see")
    assert result.returncode == 0
    assert not Path(env["FAKE_ARGS"]).exists()
    assert not Path(env["FAKE_BODY"]).exists()
    assert "test sink" in result.stderr
    assert "milestone" in result.stderr
    assert "a message no phone should see" in result.stderr

    # Exit 0 alone was read by every Python bridge over this script as delivery,
    # so the record said "Phone notification: sent." for a notification that
    # never left the machine. The marker on stdout is what tells them apart, and
    # the swallowed message never joins it there: stdout is machine-readable,
    # the human reason stays on stderr.
    assert result.stdout == "NOTIFY_SUPPRESSED verbatus-test-sink\n"


def test_a_delivered_notification_writes_nothing_on_stdout(notify_repo):
    """What makes the suppression marker unambiguous: stdout is otherwise unused.

    A bridge reads a marker line out of stdout and calls that outcome
    suppressed. That is only safe while nothing else in this script writes
    there -- every reason, refusal and suppression note goes to stderr -- so
    the emptiness is asserted rather than assumed.
    """

    script, env = notify_repo
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.full
@pytest.mark.parametrize("event", ["start", "milestone", "decision", "done"])
def test_a_delivered_notification_prints_one_line_on_stderr(notify_repo, event):
    """The 2026-09-06 fix: silence on success let a session read a stalled prior
    command as a lost ping and resend it -- three `done` pings for one close.
    Every event that actually reaches the server now says so, on stderr, once."""

    script, env = notify_repo
    result = run(script, env, event)
    assert result.returncode == 0, result.stderr
    # The whole stream, exactly: one event-specific line and nothing around it.
    assert result.stderr == f"notify: delivered ({event})\n"
    assert result.stdout == ""


def test_a_closed_stderr_does_not_turn_a_delivery_into_a_failure(notify_repo):
    """The script runs under `set -e`; if the diagnostic write could fail the
    run, an accepted post would come back non-zero and be resent -- the very
    duplicate the line exists to prevent."""

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
    # The suppression line itself says a start "was already delivered"; the
    # claim under test is the delivery line, not the word.
    assert "notify: delivered" not in result.stderr


def test_the_test_sink_is_a_literal_not_a_prefix(notify_repo):
    """A near-miss must still notify. A prefix or substring rule would make one
    mistyped character in the real topic silently stop every notification, which
    is the failure this whole file exists to prevent."""

    script, env = notify_repo
    env["NTFY_TOPIC"] = "verbatus-test-sink-2"
    result = run(script, env)
    assert result.returncode == 0, result.stderr
    body = json.loads(Path(env["FAKE_BODY"]).read_text(encoding="utf-8"))
    assert body["topic"] == "verbatus-test-sink-2"
    # A near miss must not carry the suppression marker either, or a bridge
    # would report a delivered notification as swallowed.
    assert "NOTIFY_SUPPRESSED" not in result.stdout


def test_the_conftest_sink_matches_the_topic_the_script_recognises():
    """One typo apart, the guard is off and nothing says so.

    The root `conftest.py` sets the value and `notify.sh` recognises it; they
    are in different languages and neither can import the other, so the only
    thing holding them together is this comparison.
    """

    import conftest

    source = SOURCE.read_text(encoding="utf-8")
    assert f'"$topic" = "{conftest.NOTIFY_TEST_SINK_TOPIC}"' in source


def _gate_sink_block() -> str:
    """`.githooks/check-all.sh`'s own lines, from reading the sink topic to running pytest.

    The gate is the one run that happens in the checkout holding the real
    `private/ntfy.conf`, so it is the run that most needs the sink, and it
    deliberately controls its own environment rather than inheriting one.

    It *reads* the constant out of `conftest.py` instead of restating it, for two
    reasons that point the same way: a fourth copy of a value whose whole job is
    to be identical everywhere, and `.githooks/check_ingress.py`, which refuses a
    literal ``NTFY_TOPIC=<topic-shaped value>`` anywhere in the tree under a
    ruling that exempts no exact topic.

    So the extraction has to be *run*, not read. Asserting on the script's text
    said only that some line matching a pattern exists; it would have passed over
    an extraction that yields the wrong value, an assignment that never reaches a
    child, and a guard that prints its refusal and carries on into the suite. The
    block below is lifted verbatim and executed, with `$root` and
    `$frozen_python` supplied -- the two variables the gate has already set by
    the time control reaches here.
    """

    lines = GATE.read_text(encoding="utf-8").splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("NTFY_TOPIC=$(sed")), None)
    assert first is not None, "check-all.sh no longer reads the sink topic from conftest.py"
    last = next((i for i in range(first, len(lines)) if "-m pytest" in lines[i]), None)
    assert last is not None, "check-all.sh no longer runs pytest after reading the sink topic"
    # The pytest line sits inside the `--parallel` switch (`if ... else ... fi`), so
    # the lifted block must close that `if`: run through the `fi` that follows,
    # and the child supplies `$parallel` beside `$root` and `$frozen_python`.
    end = next((i for i in range(last, len(lines)) if lines[i].strip() == "fi"), last)
    return "\n".join(lines[first : end + 1])


def _run_gate_sink_block(
    tmp_path: Path, root: Path, *, parallel: str = "no"
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run that block with a sentinel standing where the frozen interpreter stands.

    The sentinel records the `NTFY_TOPIC` it inherited and the arguments it was
    invoked with, so "the child receives the topic" is observed in the child
    rather than inferred from an `export` line. Nothing here runs pytest: the
    gate reaches the suite only through `"$frozen_python"`, and that is exactly
    what the sentinel replaces.
    """

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
    # The sentinel stands where pytest is invoked, not somewhere earlier.
    assert arguments == ["-m", "pytest"]
    # And under `--parallel` the same block hands the same topic to the four-worker
    # line, with the plugin, count and distribution on the command line.
    result, record = _run_gate_sink_block(tmp_path / "parallel", SOURCE.parents[2], parallel="yes")
    assert result.returncode == 0, result.stderr
    inherited, *arguments = record.read_text(encoding="utf-8").splitlines()
    assert inherited == conftest.NOTIFY_TEST_SINK_TOPIC
    assert arguments == ["-m", "pytest", "-p", "xdist", "-n", "4", "--dist", "loadfile"]


def test_the_gate_refuses_to_run_when_the_sink_topic_cannot_be_read(tmp_path):
    """An empty `NTFY_TOPIC` is not "no sink" -- it is `private/ntfy.conf`, the
    real topic, which is the precise failure the sink exists to prevent. So a
    renamed or reshaped constant must stop the gate *before* the suite runs, not
    print a complaint and run it unsinked. The sentinel proves the difference:
    a file that was never written is a suite that was never reached."""

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
    # 1, not 0: a failed delivery now says so for every event. The subject of this
    # test is the stamp and the retry, and neither depends on the exit status.
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
