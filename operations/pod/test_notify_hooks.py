"""`notify_hooks` offline: a fake runner, no shell, no network, no phone."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from common.test_credentials import FILES_AND_HOSTS, OPAQUE, PASSING_VALUES, SHAPED_VALUES
from operations.notify.client import NOTIFY_SCRIPT, NotifyOutcome

from .notify_hooks import notify_balance, notify_close, notify_launch


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
    # reported a measurement no instrument took (principle 8).
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
