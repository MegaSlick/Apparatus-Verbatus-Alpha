"""The two standing moments this surface may send, and the honesty around them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from . import notify_bridge
from .errors import OperatorError
from .fakes import OperatorFakeProvider
from .test_surface import START, _launch, _manifest, _spend_policy, _surface


class RecordingRunner:
    """Stands in for `notify.sh`; nothing in this file may reach the real script."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.mark.parametrize("event", ("start", "done", "note", ""))
def test_only_milestone_and_decision_may_ever_be_sent(event: str) -> None:
    """`start` and `done` belong to a session's own hooks, never to a shipped tool."""

    runner = RecordingRunner()
    outcome = notify_bridge.shell_notifier(runner=runner)(event, "a one line message")

    assert not outcome.attempted and not outcome.delivered
    assert runner.calls == []
    assert "may send" in outcome.detail


@pytest.mark.parametrize("event", sorted(notify_bridge.ALLOWED_EVENTS))
def test_an_allowed_moment_reaches_the_notify_script_with_one_line(event: str) -> None:
    runner = RecordingRunner()
    outcome = notify_bridge.shell_notifier(runner=runner)(event, "run tyrel-1 finished")

    assert outcome.attempted and outcome.delivered
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == "sh"
    assert argv[1].endswith("operations/notify/notify.sh")
    assert argv[2] == event
    assert argv[3] == "run tyrel-1 finished"
    assert outcome.line() == "Phone notification: sent."


@pytest.mark.parametrize("message", ("first line\nsecond line", "", "   "))
def test_a_message_that_is_not_one_non_empty_line_is_refused(message: str) -> None:
    runner = RecordingRunner()
    outcome = notify_bridge.shell_notifier(runner=runner)("milestone", message)

    assert runner.calls == []
    assert not outcome.delivered
    assert "one non-empty line" in outcome.detail


def test_a_failed_delivery_is_reported_as_not_delivered_and_never_as_success() -> None:
    runner = RecordingRunner(returncode=1, stderr="NOT DELIVERED: no topic configured")
    outcome = notify_bridge.shell_notifier(runner=runner)("milestone", "run finished")

    assert outcome.attempted and not outcome.delivered
    assert "NOT DELIVERED" in outcome.line()
    assert "The result above still stands." in outcome.line()


def test_a_cut_delivery_reason_says_where_it_was_cut() -> None:
    runner = RecordingRunner(returncode=1, stderr="x" * 200)

    outcome = notify_bridge.shell_notifier(runner=runner)("milestone", "run finished")

    assert outcome.detail.startswith("x" * 160)
    assert "reason truncated at 160 characters" in outcome.detail


def test_a_notification_timeout_is_a_named_non_delivery() -> None:
    observed: dict[str, object] = {}

    def hangs(argv, **kwargs):  # type: ignore[no-untyped-def]
        del argv
        observed.update(kwargs)
        raise subprocess.TimeoutExpired("notify", kwargs["timeout"])

    outcome = notify_bridge.shell_notifier(runner=hangs)("milestone", "run finished")

    assert observed["timeout"] == notify_bridge.NOTIFY_TIMEOUT_SECONDS
    assert outcome.attempted and not outcome.delivered
    assert "did not answer within 10 seconds" in outcome.detail


def test_a_notifier_that_cannot_run_at_all_is_still_not_an_exception() -> None:
    def refuses(argv, **kwargs):  # type: ignore[no-untyped-def]
        del argv, kwargs
        raise OSError("no shell here")

    outcome = notify_bridge.shell_notifier(runner=refuses)("milestone", "run finished")

    assert outcome.attempted and not outcome.delivered
    assert "could not run" in outcome.detail


def test_the_default_surface_notifier_sends_nothing_and_says_nothing(tmp_path: Path) -> None:
    """No test, and no first rehearsal, may put a ping on his phone unasked.

    And a feature nobody switched on does not narrate itself at them either: the
    silent default is the absence of a notification, not a notification about its
    own absence. Every *attempted* send is still reported, delivered or not.
    """

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)

    assert surface.notifier is notify_bridge.silent
    surface.run(run_id="silent-run")
    assert not any("Phone notification" in line for line in messages)
    assert notify_bridge.silent("milestone", "anything").attempted is False


def test_run_and_export_completion_are_milestones_and_a_hold_is_a_decision(
    tmp_path: Path,
) -> None:
    sent: list[tuple[str, str]] = []

    def notifier(event: str, message: str) -> notify_bridge.NotifyOutcome:
        sent.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface = _surface(tmp_path)
    surface.notifier = notifier
    surface.run(run_id="notified-run")
    surface.export(run_id="notified-run")

    assert [event for event, _ in sent] == ["milestone", "milestone"]
    assert "run notified-run finished" in sent[0][1]
    assert "export for run notified-run landed" in sent[1][1]


def test_a_held_run_sends_a_decision_when_it_stops_not_afterwards(tmp_path: Path) -> None:
    sent: list[tuple[str, str]] = []

    def notifier(event: str, message: str) -> notify_bridge.NotifyOutcome:
        sent.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface = _surface(tmp_path)
    surface.notifier = notifier

    with pytest.raises(OperatorError):
        surface.run(run_id="held-run", scenario="review")

    assert [event for event, _ in sent] == ["decision"]
    assert "needs a decision" in sent[0][1]


def test_a_raising_notifier_cannot_fail_the_verb_that_triggered_it(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, provider=OperatorFakeProvider(now=lambda: START), output=messages)

    def explodes(event: str, message: str) -> notify_bridge.NotifyOutcome:
        del event, message
        raise RuntimeError("the notifier itself is broken")

    surface.notifier = explodes
    outcome = surface.run(run_id="broken-notifier-run")

    assert outcome.state == "complete"
    assert any("NOT DELIVERED" in line for line in messages)


def test_notification_does_not_replace_the_terminal_result(tmp_path: Path) -> None:
    """The phone is an extra, never the only place a result appears.

    With an *active* notifier, deliberately: under the silent default no
    notification path runs at all, and this test would prove nothing beyond
    what the silent-default test already covers.
    """

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    sent: list[tuple[str, str]] = []

    def notifier(event: str, message: str) -> notify_bridge.NotifyOutcome:
        sent.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = notifier
    spend = _spend_policy(tmp_path)
    source, manifest = _manifest(tmp_path)
    _launch(surface, spend)
    surface.upload(source, sealed_manifest=manifest)
    surface.run(run_id="terminal-result-run")

    assert any("Run complete." in line for line in messages)
    assert [event for event, _ in sent] == ["milestone"]


def test_the_test_sink_marker_is_reported_as_suppressed_and_never_as_sent() -> None:
    """The operator surface prints this line to a human; it must not say "sent".

    `notify.sh` exits 0 under the reserved test topic so the guard cannot change
    what the suites it protects measure. Reading that 0 as delivery made the
    surface report a notification that never left the machine; the marker on
    stdout is what tells the two apart.
    """

    runner = RecordingRunner(stdout="NOTIFY_SUPPRESSED verbatus-test-sink\n")

    outcome = notify_bridge.shell_notifier(runner=runner)("milestone", "run tyrel-1 finished")

    assert outcome.attempted
    assert not outcome.delivered
    assert outcome.suppressed
    assert outcome.detail == "NOTIFY_SUPPRESSED verbatus-test-sink"
    assert outcome.line() == "Phone notification: suppressed (test sink)."
    assert "sent" not in outcome.line()


def test_a_real_success_is_still_delivered_and_carries_no_suppression() -> None:
    """The counterfactual: exit 0 without the marker must not become suppressed."""

    for stdout in ("", "\n", "some unrelated chatter\n"):
        outcome = notify_bridge.shell_notifier(runner=RecordingRunner(stdout=stdout))(
            "milestone", "run tyrel-1 finished"
        )

        assert outcome.attempted and outcome.delivered
        assert not outcome.suppressed
        assert outcome.line() == "Phone notification: sent."


def test_the_marker_word_this_module_reads_is_the_one_the_script_prints() -> None:
    """Two languages, neither able to import the other, one typo apart from a
    silent return to "sent." for a notification that never left the machine."""

    source = notify_bridge.NOTIFY_SCRIPT.read_text(encoding="utf-8")
    assert f"printf '{notify_bridge.NOTIFY_SUPPRESSED_MARKER} %s\\n' \"$topic\"" in source
