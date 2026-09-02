"""`notify_hooks` offline: a fake runner, no shell, no network, no phone.

Every test injects `runner` in place of `default_runner` -- exactly the
pattern `operations/pod/notify_bridge.py`'s own tests use -- so nothing here
ever spawns `sh` or touches `operations/notify/notify.sh` for real.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from .notify_hooks import (
    NOTIFY_SCRIPT,
    NotifyOutcome,
    notify_balance,
    notify_close,
    notify_launch,
)


@dataclass
class FakeRunner:
    """Records every argv it was called with; answers green unless told otherwise."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    raise_error: Exception | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        if self.raise_error is not None:
            raise self.raise_error
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, self.stderr)


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


def test_notify_close_sends_lease_state_and_elapsed() -> None:
    runner = FakeRunner()

    outcome = notify_close(
        lease_id="lease-abc123", verified_state="verified", elapsed_seconds=612, runner=runner
    )

    assert outcome.delivered
    message = runner.calls[0][3]
    assert "lease-abc123" in message
    assert "verified" in message
    assert "612" in message


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
        # that shape: `_looks_like_credential_word`'s prefix check does not
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
        elapsed_seconds=10,
        runner=runner,
    )

    assert not outcome.attempted
    assert "URL" in outcome.detail
    assert runner.calls == []


def test_a_lowercase_hex_identifier_is_not_mistaken_for_a_credential() -> None:
    """A git commit or a manifest digest is exactly this shape and is legitimate."""

    runner = FakeRunner()

    outcome = notify_close(
        lease_id="a" * 40, verified_state="verified", elapsed_seconds=10, runner=runner
    )

    assert outcome.delivered


def test_a_multi_line_message_is_refused_before_sending() -> None:
    runner = FakeRunner()

    outcome = notify_close(
        lease_id="lease\nabc", verified_state="verified", elapsed_seconds=10, runner=runner
    )

    assert not outcome.attempted
    assert runner.calls == []


# --- a failed ping cannot prevent a close -------------------------------------


def test_a_nonzero_exit_is_reported_not_raised() -> None:
    runner = FakeRunner(returncode=1, stderr="no topic configured\n")

    outcome = notify_close(
        lease_id="lease-abc123", verified_state="verified", elapsed_seconds=10, runner=runner
    )

    assert outcome.attempted
    assert not outcome.delivered
    assert "no topic configured" in outcome.detail


def test_a_transport_failure_is_reported_not_raised() -> None:
    runner = FakeRunner(raise_error=OSError("no such file or directory: sh"))

    outcome = notify_close(
        lease_id="lease-abc123", verified_state="verified", elapsed_seconds=10, runner=runner
    )

    assert outcome.attempted
    assert not outcome.delivered
    assert "could not run" in outcome.detail


def test_a_timeout_is_reported_not_raised() -> None:
    runner = FakeRunner(raise_error=subprocess.TimeoutExpired(cmd=["sh"], timeout=10.0))

    outcome = notify_close(
        lease_id="lease-abc123", verified_state="verified", elapsed_seconds=10, runner=runner
    )

    assert outcome.attempted
    assert not outcome.delivered
    assert "did not answer" in outcome.detail


def test_no_outcome_from_this_module_ever_raises() -> None:
    """The whole point: a caller closing a pod never has to catch anything here."""

    for runner in (
        FakeRunner(returncode=1),
        FakeRunner(raise_error=OSError("boom")),
        FakeRunner(raise_error=subprocess.TimeoutExpired(cmd=["sh"], timeout=1.0)),
    ):
        outcome = notify_close(
            lease_id="lease-abc123", verified_state="verified", elapsed_seconds=1, runner=runner
        )
        assert isinstance(outcome, NotifyOutcome)


def test_a_long_stderr_reason_is_bounded() -> None:
    runner = FakeRunner(returncode=1, stderr="x" * 500)

    outcome = notify_close(
        lease_id="lease-abc123", verified_state="verified", elapsed_seconds=1, runner=runner
    )

    assert len(outcome.detail) < 200
    assert "truncated" in outcome.detail
