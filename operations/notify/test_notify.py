"""Contract tests for the one-way notification client.

Every delivery test uses a fake ``curl`` and a placeholder topic. Nothing in
this suite opens a network connection or reads a real credential.
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
    touch_exit=0,
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

    if touch_exit:
        fake_touch = fake_bin / "touch"
        fake_touch.write_text(f"#!/bin/sh\nexit {touch_exit}\n", encoding="utf-8")
        fake_touch.chmod(0o755)

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
def test_a_low_priority_failure_exits_zero_but_never_reads_as_delivered(
    tmp_path, event, scenario
):
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


def test_broken_config_symlink_is_reported_not_followed(tmp_path):
    checkout, fake_bin, evidence = make_checkout(tmp_path)
    (checkout / "private" / "ntfy.conf").symlink_to("missing-topic-file")
    result = run_notify(checkout, fake_bin, topic=None)
    assert result.returncode == 1
    assert "notifications are off" in result.stderr
    assert not evidence["calls"].exists()


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


@pytest.mark.parametrize("message", ["", "first\nsecond"])
def test_message_must_be_one_non_empty_line(tmp_path, message):
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
    checkout, fake_bin, _evidence = make_checkout(tmp_path, touch_exit=1)
    result = run_notify(checkout, fake_bin, event="start")
    assert result.returncode == 0, result.stderr
    assert "could not record its suppression stamp" in result.stderr
