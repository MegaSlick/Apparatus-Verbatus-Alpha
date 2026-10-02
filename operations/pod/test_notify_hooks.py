"""`notify_hooks` offline: a fake runner, no shell, no network, no phone."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from common.test_credentials import FILES_AND_HOSTS, OPAQUE, PASSING_VALUES, SHAPED_VALUES
from operations.notify.client import NOTIFY_SCRIPT, NotifyOutcome

from .notify_hooks import notify_balance, notify_close, notify_launch, notify_systemic


@dataclass
class FakeRunner:
    """Records every argv it was called with and answers green."""

    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


# --- the three messages ------------------------------------------------------


def test_notify_launch_sends_lease_card_and_ceiling() -> None:
    runner = FakeRunner()

    outcome = notify_launch(
        lease_id="lease-abc123", card="RTX PRO 6000", max_hourly_usd="1.99", runner=runner
    )

    assert outcome == NotifyOutcome(True, True, "delivered")
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:3] == ["sh", str(NOTIFY_SCRIPT), "milestone"]
    message = argv[3]
    assert "lease-abc123" in message
    assert "RTX PRO 6000" in message
    assert "1.99" in message
    assert "\n" not in message


def test_notify_close_sends_lease_state_and_the_billed_window() -> None:
    runner = FakeRunner()

    outcome = notify_close(
        lease_id="lease-abc123", verified_state="verified", billed_seconds=612, runner=runner
    )

    assert outcome.delivered
    message = runner.calls[0][3]
    assert "lease-abc123" in message
    assert "verified" in message
    assert "612" in message
    # The number is pod creation to the verified billing cutoff, and nothing on
    # this path observes a stop time. It is named for what it is: "ran 612s"
    # reported a measurement no instrument took.
    assert "billed" in message
    assert "ran" not in message


def test_notify_balance_sends_balance_and_spend_rate() -> None:
    runner = FakeRunner()

    outcome = notify_balance(balance_usd="76.50", spend_rate_usd_per_hr="1.99", runner=runner)

    assert outcome.delivered
    message = runner.calls[0][3]
    assert "76.50" in message
    assert "1.99" in message


def test_notify_balance_names_the_lease_when_given_one() -> None:
    runner = FakeRunner()

    notify_balance(
        balance_usd="76.50", spend_rate_usd_per_hr="1.99", lease_id="lease-xyz", runner=runner
    )

    assert "lease-xyz" in runner.calls[0][3]


def test_notify_balance_reads_as_account_scoped_with_no_lease() -> None:
    """A preview can observe the balance before any lease exists."""

    runner = FakeRunner()

    notify_balance(balance_usd="76.50", spend_rate_usd_per_hr="1.99", runner=runner)

    assert "account" in runner.calls[0][3]


def test_notify_systemic_sends_the_alarm_as_a_decision() -> None:
    """The same line `verbatus run --notify` sends for a systemic run on this computer."""
    from common.review_policy import systemic_notice

    runner = FakeRunner()
    line = "run r1: systemic: 2 of 3 page(s) are held after the recensor"

    outcome = notify_systemic(run_id="r1", alarm_line=line, runner=runner)

    assert outcome == NotifyOutcome(True, True, "delivered")
    [argv] = runner.calls
    assert argv[:3] == ["sh", str(NOTIFY_SCRIPT), "decision"]
    assert argv[3] == systemic_notice("r1", line)
    assert argv[3] == (
        "Verbatus run r1 has a systemic problem and needs a decision: "
        "2 of 3 page(s) are held after the recensor"
    )


# --- the no-secret rule -------------------------------------------------------


@pytest.mark.parametrize(
    "card",
    [
        # Vendor-prefix cases below are deliberately shorter than a real key of
        # that shape: the shared prefix check does not
        # care about length, but the repository's own ingress scanner
        # (`.githooks/check_ingress.py`) pattern-matches a *real-length* key
        # and would refuse to let this file be committed at all otherwise.
        "sk-not-a-real-key",
        "hf_not-a-real-token",
        "AKIAnotarealawskeyid",
        "aB3fG9kL2mN7pQ5rS8tU1v",  # opaque 20+ char mixed alphanumeric run
        "the-api-secret-is-here",
        "bearer-token-value",
        "abcdefghij.klmnopqrst.uvwxyz1234",  # JWT-shaped, dot-joined opaque segments
        "QUJDREVGR0hJSktMTU5PUFFS+/=",  # base64-shaped, slash and padding included
    ],
)
def test_a_credential_shaped_value_is_refused_before_sending(card: str) -> None:
    runner = FakeRunner()

    outcome = notify_launch(
        lease_id="lease-abc123", card=card, max_hourly_usd="1.99", runner=runner
    )

    assert not outcome.attempted
    assert not outcome.delivered
    assert "credential" in outcome.detail
    assert runner.calls == [], "a refused message must never reach the shell"


@pytest.mark.parametrize("value", SHAPED_VALUES)
def test_every_boundary_refuses_or_removes_a_credential_shape(value: str) -> None:
    from operations.serving.manager import _redacted

    from .fixture import SCRUBBED, _scrub

    runner = FakeRunner()
    assert not notify_launch(lease_id="l", card=value, max_hourly_usd="1", runner=runner).attempted
    assert _scrub({"message": f"started {value}"}, "body", []) == {"message": SCRUBBED}
    redacted = _redacted(f"INFO started {value} ok")
    assert value not in redacted
    assert "K7MDENG" not in redacted and "3xK9pLm2Qz" not in redacted


def test_the_fixture_scrubs_a_secret_named_inside_a_string() -> None:
    from .fixture import SCRUBBED, _scrub

    scrubbed: list[str] = []
    assert _scrub({"args": "run password=Abc123xyz98"}, "body", scrubbed) == {"args": SCRUBBED}
    assert scrubbed == ["body.args"]


@pytest.mark.parametrize("value", PASSING_VALUES)
def test_every_boundary_passes_an_identifier(value: str) -> None:
    from operations.serving.manager import _redacted

    from .bootstrap_main import refuse_credential_looking_argv
    from .fixture import _scrub

    assert notify_close(
        lease_id=value, verified_state="ok", billed_seconds=1, runner=FakeRunner()
    ).delivered
    assert _scrub({"message": value}, "body", []) == {"message": value}
    refuse_credential_looking_argv(["--run-id", value])
    assert _redacted(f"INFO {value}") == f"INFO {value}"


@pytest.mark.parametrize("value", FILES_AND_HOSTS)
def test_a_file_or_host_passes_argv_and_logs_but_not_notifications_or_fixtures(value: str) -> None:
    from operations.serving.manager import _redacted

    from .bootstrap_main import refuse_credential_looking_argv
    from .fixture import SCRUBBED, _scrub

    assert not notify_launch(
        lease_id="l", card=value, max_hourly_usd="1", runner=FakeRunner()
    ).attempted
    assert _scrub({"message": value}, "body", []) == {"message": SCRUBBED}
    refuse_credential_looking_argv(["--run-id", value])
    assert _redacted(f"INFO {value}") == f"INFO {value}"


@pytest.mark.parametrize(
    "value",
    [
        "sk-not-a-real-key",
        "aB3fG9kL2mN7pQ5rS8tU1v",
        "abcdefghij.klmnopqrst.uvwxyz1234",
        "/workspace/abcdefghij.klmnopqrst.uvwxyz1234/report.json",
        "/workspace/sk-not-a-real-key/report.json",
        "https://user" + ":" + OPAQUE + "@example.invalid/x",
        f"https://example.invalid/x?key={OPAQUE}",
    ],
)
def test_the_argv_refusal_refuses_a_bare_secret_or_a_prefixed_or_dotted_segment(value: str) -> None:
    from .bootstrap_main import PlanRefusal, refuse_credential_looking_argv

    with pytest.raises(PlanRefusal, match="looks like a credential"):
        refuse_credential_looking_argv(["--run-id", value])


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/pytest-of-runner/pytest-341/test_hold_survives_a_completed0/volume",
        "/var/folders/wv/yt31hyzs7cgf7xs7mn8xnlpw0000gn/T/volume",
        "/workspace/runs/recordgold-pilot-2026-09-25/",
    ],
)
def test_the_argv_refusal_passes_run_folders_and_temp_directories(value: str) -> None:
    from .bootstrap_main import refuse_credential_looking_argv

    refuse_credential_looking_argv(["--volume-mount-path", value])


@pytest.mark.parametrize(
    "verified_state",
    [
        "https://console.runpod.io/pod/abc",
        "console.runpod.io/pod/xyz",  # scheme-less console link
        "notify.example/topic/abcdefghijklmnop",  # scheme-less notification-service link
    ],
)
def test_a_message_naming_a_url_is_refused_before_sending(verified_state: str) -> None:
    runner = FakeRunner()

    outcome = notify_close(
        lease_id="lease-abc123",
        verified_state=verified_state,
        billed_seconds=10,
        runner=runner,
    )

    assert not outcome.attempted
    assert "URL" in outcome.detail
    assert runner.calls == []


def test_a_lowercase_hex_identifier_is_not_mistaken_for_a_credential() -> None:
    """A git commit or a manifest digest is exactly this shape and is legitimate."""

    runner = FakeRunner()

    outcome = notify_close(
        lease_id="a" * 40, verified_state="verified", billed_seconds=10, runner=runner
    )

    assert outcome.delivered


def test_the_topic_is_read_where_the_pod_guard_keeps_it() -> None:
    """The guard keeps its records in `<volume mount>/.pod_guard` (`pod_start_command.sh`,
    pinned by test_pod_guard); its topic is `ntfy_topic` there."""
    from operations.pod.models import POD_VOLUME_MOUNT_PATH

    from .notify_hooks import guard_topic_path

    guard = (Path(__file__).parent / "pod_guard.sh").read_text(encoding="utf-8")
    assert "topic=$(tr -d ' \\r\\n' <\"$dir/ntfy_topic\"" in guard
    assert guard_topic_path(Path(POD_VOLUME_MOUNT_PATH)) == Path(
        f"{POD_VOLUME_MOUNT_PATH}/.pod_guard/ntfy_topic"
    )


def test_the_guard_topic_is_read_from_its_file_and_refused_when_malformed(tmp_path) -> None:
    from .models import POD_GUARD_DIRECTORY
    from .notify_hooks import guard_topic, notify_environment

    assert guard_topic(tmp_path) is None
    path = tmp_path / POD_GUARD_DIRECTORY / "ntfy_topic"
    path.parent.mkdir(parents=True)
    path.write_text("a-topic_1\n", encoding="utf-8")
    assert guard_topic(tmp_path) == "a-topic_1"
    assert notify_environment("a-topic_1")["NTFY_TOPIC"] == "a-topic_1"
    for bad in ("x" * 65, "a/slash", "tab\there", "x" * 4096):
        path.write_text(bad, encoding="utf-8")
        assert guard_topic(tmp_path) is None


def test_the_guard_topic_is_normalised_as_the_pod_guard_normalises_it(tmp_path) -> None:
    """Spaces, CRs and newlines go wherever they stand, as `tr -d ' \\r\\n'` removes them."""
    from .models import POD_GUARD_DIRECTORY
    from .notify_hooks import guard_topic

    path = tmp_path / POD_GUARD_DIRECTORY / "ntfy_topic"
    path.parent.mkdir(parents=True)
    longest = "x" * 64
    for written, read in (
        (longest + "\r\n", longest),
        (" a-topic_1 \r\n", "a-topic_1"),
        ("two words\n", "twowords"),
        ("split\r\nline\n", "splitline"),
    ):
        path.write_bytes(written.encode("utf-8"))
        assert guard_topic(tmp_path) == read


def test_the_guard_topic_is_read_only_from_a_regular_file_never_a_link_or_fifo(tmp_path) -> None:
    """A link is not followed, and a FIFO is never opened for a read that could block."""
    import os

    from .models import POD_GUARD_DIRECTORY
    from .notify_hooks import guard_topic

    guard = tmp_path / POD_GUARD_DIRECTORY
    guard.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("a-topic_1", encoding="utf-8")
    (guard / "ntfy_topic").symlink_to(elsewhere)
    assert guard_topic(tmp_path) is None
    (guard / "ntfy_topic").unlink()
    os.mkfifo(guard / "ntfy_topic")
    assert guard_topic(tmp_path) is None


def test_with_no_guard_topic_nothing_is_run(tmp_path) -> None:
    """notify.sh never runs, so it never falls back to a topic of the checkout's."""
    from .notify_hooks import NO_GUARD_TOPIC, notify_systemic_from_guard

    factories = []
    outcome = notify_systemic_from_guard(
        run_id="r1",
        alarm_line="run r1: systemic: 2 of 3 page(s) are held after the recensor",
        volume_mount=tmp_path,
        runner_factory=lambda environment: factories.append(environment) or FakeRunner(),
    )
    assert outcome == NotifyOutcome(False, False, NO_GUARD_TOPIC)
    assert factories == []
