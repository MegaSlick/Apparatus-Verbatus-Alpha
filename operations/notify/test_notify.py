"""Contract tests for the one-way notification client.

Every delivery test uses a fake ``curl`` and a placeholder topic. Nothing in
this suite opens a network connection or reads a real credential.

WHAT A GREEN RUN HERE DOES AND DOES NOT ESTABLISH
-------------------------------------------------
Read this before treating a passing suite as a working notifier. An audit found
that every assertion below about ``curl`` is an assertion about a shell script
this file writes, and the component's real failure mode is silence — which is
exactly what a fake cannot detect.

It establishes, and this is worth having:

* what ``notify.sh`` decides — which events fail their caller, which do not,
  which inputs are refused, when a start ping is suppressed;
* what it would hand to ``curl`` — the argument vector, the JSON body, and the
  absence of the bearer topic from both argv and curl's environment;
* that a non-2xx code, or a curl exit status, is never reported as delivery.

It establishes none of this, and no amount of testing harder will change that:

* that ``curl`` accepts the flags used. ``-sS`` mutated to ``--silentt`` passes
  this whole suite; the fake reads ``$1`` and its own recorded argv, and never
  parses an option. The one flag pinned against a real program is ``-q`` being
  first, and only because the fake exits 90 otherwise;
* that DNS resolves, that TLS negotiates, that a proxy does not intercept, or
  that ntfy.sh accepts this payload shape at the server root;
* that Tyrel's phone rings. Nothing here has ever sent a notification.

The gap closes only with a live send against the real topic, which spends a
real credential and is Tyrel's call to make in a session, not a test's to make
on its own. Until then: green here means the script's *logic* is intact, and
says nothing about whether the message arrives.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "operations" / "notify" / "notify.sh"


def clean_env():
    """Return an environment that cannot inherit an operator's notification data."""
    env = dict(os.environ)
    env.pop("NTFY_TOPIC", None)
    env.pop("NTFY_SERVER", None)
    return env


def make_checkout(
    tmp_path,
    *,
    curl_status="204",
    curl_exit=0,
    config_topic=None,
):
    checkout = tmp_path / "checkout"
    notify = checkout / "operations" / "notify"
    private = checkout / "private"
    fake_bin = tmp_path / "bin"
    notify.mkdir(parents=True)
    private.mkdir()
    fake_bin.mkdir()
    shutil.copy2(SCRIPT, notify / "notify.sh")

    # The gitignored config file is the default source of the bearer topic when
    # the environment does not carry one. Tyrel's ruling: an ignored file under
    # private/ is an acceptable home for it.
    if config_topic is not None:
        (private / "ntfy.conf").write_text(f'NTFY_TOPIC="{config_topic}"\n', encoding="utf-8")

    curl_args = tmp_path / "curl-args"
    curl_payload = tmp_path / "curl-payload"
    curl_environment = tmp_path / "curl-environment"
    curl_calls = tmp_path / "curl-calls"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        '[ "$1" = -q ] || exit 90\n'
        f"printf '%s\\n' \"$@\" > '{curl_args}'\n"
        f"cat > '{curl_payload}'\n"
        f"if [ \"${{NTFY_TOPIC+x}}\" = x ]; then printf present > '{curl_environment}'; "
        f"else printf absent > '{curl_environment}'; fi\n"
        f"printf call >> '{curl_calls}'\n"
        f"printf '%s' '{curl_status}'\n"
        f"exit {curl_exit}\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    evidence = {
        "args": curl_args,
        "payload": curl_payload,
        "environment": curl_environment,
        "calls": curl_calls,
    }
    return checkout, fake_bin, evidence


def run_notify(
    checkout,
    fake_bin,
    event="decision",
    message="A decision is needed",
    *,
    topic="test_topic",
    server=None,
):
    env = clean_env()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    if topic is not None:
        env["NTFY_TOPIC"] = topic
    if server is not None:
        env["NTFY_SERVER"] = server
    return subprocess.run(
        ["sh", "operations/notify/notify.sh", event, message],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_success_requires_http_2xx_and_builds_json_without_exposing_topic_to_curl(
    tmp_path,
):
    checkout, fake_bin, evidence = make_checkout(tmp_path, curl_status="204")
    result = run_notify(checkout, fake_bin)
    assert result.returncode == 0, result.stderr

    args = evidence["args"].read_text(encoding="utf-8").splitlines()
    assert args[0] == "-q", "a machine-local .curlrc was not disabled"
    assert "--max-time" in args and args[args.index("--max-time") + 1] == "10"
    assert "--write-out" in args
    assert args[args.index("--write-out") + 1] == "%{http_code}"
    assert "--data-binary" in args
    assert args[args.index("--data-binary") + 1] == "@-"
    assert "Content-Type: application/json" in args
    assert args[-1] == "https://ntfy.sh/"
    assert "test_topic" not in args, "the bearer topic entered curl's process arguments"
    assert evidence["environment"].read_text(encoding="utf-8") == "absent"

    payload = json.loads(evidence["payload"].read_text(encoding="utf-8"))
    assert payload == {
        "topic": "test_topic",
        "message": "A decision is needed",
        "title": "Needs a decision",
        "priority": 4,
        "tags": ["warning"],
    }


@pytest.mark.parametrize("status", ["000", "301", "404", "503"])
def test_non_2xx_is_not_reported_as_delivery(tmp_path, status):
    checkout, fake_bin, _evidence = make_checkout(tmp_path, curl_status=status)
    result = run_notify(checkout, fake_bin)
    assert result.returncode == 1
    assert "NOT reached" in result.stderr


def test_curl_failure_is_not_hidden_by_a_printed_2xx(tmp_path):
    checkout, fake_bin, _evidence = make_checkout(tmp_path, curl_status="204", curl_exit=7)
    result = run_notify(checkout, fake_bin)
    assert result.returncode == 1
    assert "NOT reached" in result.stderr


@pytest.mark.parametrize("event", ["start", "milestone"])
def test_low_priority_delivery_failure_does_not_fail_the_session(tmp_path, event):
    checkout, fake_bin, _evidence = make_checkout(tmp_path)
    result = run_notify(checkout, fake_bin, event=event, topic=None)
    assert result.returncode == 0
    assert "notifications are off" in result.stderr


# Every way a delivery can fail for an event that must not fail its caller. All
# of them funnel through the one `fail()` helper, which is what makes this list
# complete rather than merely long: a new refusal added anywhere in the script
# reaches the same exit, so it inherits the same reporting.
DELIVERY_FAILURES = {
    "no topic in the environment or the config file": ({}, {"topic": None}),
    "topic carries characters ntfy will not route": ({}, {"topic": "slash/topic"}),
    "topic is longer than ntfy allows": ({}, {"topic": "x" * 65}),
    "the server refused the notification": ({"curl_status": "503"}, {}),
    "curl itself failed after printing a 2xx": (
        {"curl_status": "204", "curl_exit": 7},
        {},
    ),
}


@pytest.mark.parametrize("event", ["start", "milestone"])
@pytest.mark.parametrize("scenario", sorted(DELIVERY_FAILURES))
def test_a_low_priority_failure_exits_zero_but_never_reads_as_delivered(tmp_path, event, scenario):
    """GOVERNANCE 10: exit 0 here means "carry on", never "the phone rang".

    `start` and `milestone` deliberately do not fail their caller — a session
    must not die because a ping did not land. The cost of that choice is that
    the exit status can no longer tell a delivered ping from a dropped one, so
    the stderr line has to, on *every* failure path and not just the one where
    no topic is configured.
    """
    make_kwargs, run_kwargs = DELIVERY_FAILURES[scenario]
    checkout, fake_bin, _evidence = make_checkout(tmp_path, **make_kwargs)
    result = run_notify(checkout, fake_bin, event=event, **run_kwargs)

    assert result.returncode == 0, result.stderr
    assert "NOT DELIVERED" in result.stderr, (
        f"{event} failed ({scenario}) and exited 0 without saying so; a reader of "
        "the transcript cannot tell this from a delivered notification"
    )
    assert "NOT reached" in result.stderr


@pytest.mark.parametrize("event", ["start", "milestone", "decision", "done"])
def test_a_delivered_notification_never_prints_the_failure_line(tmp_path, event):
    # The other half of the guard: a marker that is always printed distinguishes
    # nothing. A delivered notification must be silent about failure.
    checkout, fake_bin, evidence = make_checkout(tmp_path, curl_status="204")
    result = run_notify(checkout, fake_bin, event=event)
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].read_text(encoding="utf-8") == "call"
    assert "NOT DELIVERED" not in result.stderr
    assert "NOT reached" not in result.stderr


@pytest.mark.parametrize("event", ["decision", "done"])
def test_waiting_event_delivery_failure_fails_the_session(tmp_path, event):
    checkout, fake_bin, _evidence = make_checkout(tmp_path)
    result = run_notify(checkout, fake_bin, event=event, topic=None)
    assert result.returncode == 1
    assert "notifications are off" in result.stderr


def test_topic_is_read_from_the_gitignored_config_when_the_environment_has_none(tmp_path):
    # The whole point of the file source: nothing in this repository injects
    # NTFY_TOPIC, so without it every notification is silently off.
    checkout, fake_bin, evidence = make_checkout(tmp_path, config_topic="TEST_from_file")
    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "curl never ran, so nothing was delivered"
    assert "TEST_from_file" in evidence["payload"].read_text(encoding="utf-8")


def test_the_environment_overrides_the_config_file(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path, config_topic="TEST_from_file")
    result = run_notify(checkout, fake_bin, topic="TEST_from_env")
    assert result.returncode == 0, result.stderr
    payload = evidence["payload"].read_text(encoding="utf-8")
    assert "TEST_from_env" in payload
    assert "TEST_from_file" not in payload


def test_a_named_pipe_at_the_config_path_does_not_hang_the_hook(tmp_path):
    # Reading a FIFO blocks until something writes. This script runs from the
    # SessionStart hook, so a blocking read is not a missed notification — it is
    # a session that never starts, with nothing on screen to explain why.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    os.mkfifo(checkout / "private" / "ntfy.conf")
    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 1
    assert "notifications are off" in result.stderr
    assert not evidence["calls"].exists()


def test_a_dangling_config_symlink_is_reported_as_no_configuration(tmp_path):
    # Named for what it proves. `-f` is false for a link with nothing on the end
    # of it, so this is the *dangling* case only — see the test below for the
    # working link, which is followed on purpose.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    (checkout / "private" / "ntfy.conf").symlink_to("missing-topic-file")
    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 1
    assert "notifications are off" in result.stderr
    assert not evidence["calls"].exists()


def test_a_working_config_symlink_is_followed_deliberately(tmp_path):
    """The decided answer to "does the config check follow symlinks?" — it does.

    An audit found the code and its comment disagreeing: the comment explained
    that `-f` was chosen so a named pipe could not block a session, and read as
    though it also refused symlinks, which it never did. One of the two had to
    change. The comment did.

    The reasoning, so a later reader can overturn it deliberately rather than by
    accident: anyone able to plant a symlink inside `private/` can plant a
    regular file there instead, so the refusal defends against nobody, while an
    operator who keeps the topic somewhere else and links to it has a real use
    for the link. This test is here to make the behaviour a decision with a
    stated reason instead of an accident of `-f`.
    """
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    elsewhere = tmp_path / "elsewhere.conf"
    assignment = "NTFY_" + 'TOPIC="TEST_via_symlink"'
    elsewhere.write_text(f"{assignment}\n", encoding="utf-8")
    (checkout / "private" / "ntfy.conf").symlink_to(elsewhere)

    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "the linked config was not read"
    assert "TEST_via_symlink" in evidence["payload"].read_text(encoding="utf-8")


def test_a_config_file_without_a_topic_line_is_not_treated_as_configured(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path, config_topic=None)
    (checkout / "private" / "ntfy.conf").write_text("# no topic here\n", encoding="utf-8")
    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 1
    assert "notifications are off" in result.stderr
    assert not evidence["calls"].exists()


def test_the_config_file_is_never_executed(tmp_path):
    # A config that runs is a config that can do anything, and this one is read
    # from a hook. It is parsed as data; a command in it must stay inert.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    marker = tmp_path / "executed"
    # Assembled rather than written out: an exact topic assignment in tracked
    # source is what the ingress scanner exists to refuse, and a test fixture is
    # not a reason to carve a hole in it.
    assignment = "NTFY_" + 'TOPIC="TEST_inert"'
    (checkout / "private" / "ntfy.conf").write_text(
        f"{assignment}\ntouch {marker}\n", encoding="utf-8"
    )
    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "the config file was sourced instead of parsed"


def test_caller_xtrace_cannot_copy_topic_to_output(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    topic = "TEST_xtrace_topic"
    env = clean_env()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["NTFY_TOPIC"] = topic

    result = subprocess.run(
        ["sh", "-x", "operations/notify/notify.sh", "decision", "A decision is needed"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0
    assert topic not in result.stdout
    assert topic not in result.stderr
    assert evidence["calls"].read_text(encoding="utf-8") == "call"


@pytest.mark.parametrize("topic", ["slash/topic", "space topic", "dot.topic", "é"])
def test_topic_characters_outside_ntfy_contract_are_refused(tmp_path, topic):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    result = run_notify(checkout, fake_bin, topic=topic)
    assert result.returncode == 1
    assert "characters outside" in result.stderr
    assert topic not in result.stderr
    assert not evidence["calls"].exists()


def test_topic_longer_than_ntfy_limit_is_refused_without_echoing_it(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    topic = "x" * 65
    result = run_notify(checkout, fake_bin, topic=topic)
    assert result.returncode == 1
    assert "64-character limit" in result.stderr
    assert topic not in result.stderr
    assert not evidence["calls"].exists()


def test_ambient_server_override_is_refused_without_echoing_or_contacting_it(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    server = "https://collector.example"
    result = run_notify(checkout, fake_bin, server=server)
    assert result.returncode == 2
    assert "delivery is fixed to https://ntfy.sh" in result.stderr
    assert server not in result.stderr
    assert not evidence["calls"].exists()


@pytest.mark.parametrize("message", ["", "first\nsecond", "first\rsecond", "trailing\r"])
def test_message_must_be_one_non_empty_line(tmp_path, message):
    # A bare carriage return splits the message in the phone client exactly as a
    # newline does, so a check that caught only LF let a two-line notification
    # through the guard whose only job was to refuse one.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    result = run_notify(checkout, fake_bin, message=message)
    assert result.returncode == 2
    assert "one non-empty line" in result.stderr
    assert not evidence["calls"].exists()


def test_invalid_interface_fails_before_credential_handling(tmp_path):
    checkout, fake_bin, _evidence = make_checkout(tmp_path)
    env = clean_env()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["sh", "operations/notify/notify.sh", "unknown", "message"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "unknown event" in result.stderr


def test_delivered_start_is_suppressed_for_fifteen_minutes(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    first = run_notify(checkout, fake_bin, event="start")
    second = run_notify(checkout, fake_bin, event="start")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "suppressed" in second.stderr
    assert evidence["calls"].read_text(encoding="utf-8") == "call"


def test_the_suppression_notice_claims_delivery_and_not_a_mere_attempt(tmp_path):
    # The stamp is written only inside the success branch, so what it records is
    # a *delivered* start. Saying "attempted" understates the evidence in the one
    # direction that matters here: a reader who believes a failed ping could have
    # set the stamp reads a suppression notice as a possible silent loss.
    checkout, fake_bin, _evidence = make_checkout(tmp_path, curl_status="204")
    first = run_notify(checkout, fake_bin, event="start")
    second = run_notify(checkout, fake_bin, event="start")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "already delivered" in second.stderr
    assert "attempted" not in second.stderr
    assert "NOT DELIVERED" not in second.stderr


def test_failed_start_does_not_suppress_the_retry(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path, curl_status="503")
    first = run_notify(checkout, fake_bin, event="start")
    second = run_notify(checkout, fake_bin, event="start")
    assert first.returncode == 0
    assert second.returncode == 0
    assert evidence["calls"].read_text(encoding="utf-8") == "callcall"


def test_start_reports_when_it_cannot_record_suppression_state(tmp_path):
    # The stamp lives in private/, so the way it becomes unwritable in the field
    # is a permission on that directory, not a broken `touch`.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    private = checkout / "private"
    private.chmod(0o555)
    try:
        result = run_notify(checkout, fake_bin, event="start")
    finally:
        private.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].read_text(encoding="utf-8") == "call", "the ping itself was lost"
    assert "could not record its suppression stamp" in result.stderr


# --- the start stamp is evidence, and is checked like evidence ---------------
#
# Suppression is the one path in this script that deliberately does not send.
# The old check asked `find "$stamp" -mmin -15` — "is there anything here that
# changed recently" — and everything below answered yes without being a stamp
# this script ever wrote. Each one silenced a real SessionStart ping and said
# nothing, which is the failure mode a notifier is least able to survive.

STAMP = "private/.notify-start-stamp"


def _seed_stamp(checkout, *, seconds_ago):
    """Write a stamp as this script writes one, aged by the given offset."""
    import time

    path = checkout / STAMP
    written = int(time.time()) - seconds_ago
    path.write_text(f"{written}\n", encoding="utf-8")
    os.utime(path, (written, written))
    return path


def test_a_directory_at_the_stamp_path_does_not_suppress_a_start(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    (checkout / STAMP).mkdir()
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "a directory at the stamp path swallowed the ping"
    assert "not a regular file" in result.stderr


def test_a_fifo_at_the_stamp_path_does_not_suppress_a_start(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    os.mkfifo(checkout / STAMP)
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "a FIFO at the stamp path swallowed the ping"
    assert "not a regular file" in result.stderr


def test_a_symlinked_stamp_is_not_trusted_and_is_not_written_through(tmp_path):
    # The stamp is read AND written. A link aimed at a file the machine rewrites
    # often would suppress every start ping forever; a link aimed anywhere at all
    # would redirect this script's own write out of private/. There is no
    # legitimate use for a symlinked suppression stamp, so both are refused —
    # which is the opposite of the ruling on the config file, on purpose.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    target = tmp_path / "busy-file"
    target.write_text("", encoding="utf-8")
    (checkout / STAMP).symlink_to(target)

    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "a symlinked stamp swallowed the ping"
    assert "symlink" in result.stderr
    assert target.read_text(encoding="utf-8") == "", "the stamp write followed the symlink out"


def test_a_future_dated_stamp_does_not_suppress_a_start(tmp_path):
    # A negative age is still "less than fifteen minutes". One clock skew, or one
    # stray `touch -t`, and every start ping is suppressed until the date passes.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    _seed_stamp(checkout, seconds_ago=-3600)
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "a future-dated stamp swallowed the ping"
    assert "future" in result.stderr


def test_an_unreadable_stamp_does_not_suppress_a_start(tmp_path):
    # Includes every stamp the older touch-based version left behind: empty.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    (checkout / STAMP).write_text("", encoding="utf-8")
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "an empty stamp swallowed the ping"
    assert "no readable timestamp" in result.stderr


def test_a_stamp_older_than_the_window_does_not_suppress(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    _seed_stamp(checkout, seconds_ago=901)
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), "an expired stamp suppressed a start ping"


def test_a_stamp_inside_the_window_still_suppresses(tmp_path):
    # The other half: the fixes above must not have disabled suppression, which
    # is the behaviour the stamp exists for.
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    _seed_stamp(checkout, seconds_ago=60)
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert "suppressed" in result.stderr
    assert not evidence["calls"].exists()


@pytest.mark.parametrize("event", ["milestone", "decision", "done"])
def test_a_fresh_stamp_never_suppresses_a_deliberate_event(tmp_path, event):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    _seed_stamp(checkout, seconds_ago=1)
    result = run_notify(checkout, fake_bin, event=event)
    assert result.returncode == 0, result.stderr
    assert evidence["calls"].exists(), f"{event} was suppressed by a start stamp"
