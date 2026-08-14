"""The two phone moments this surface is allowed to send, and nothing else.

Spec 12: "the four standing moments only (`operations/notify` exists);
`run`/`export` completion is a `milestone`, a decision-needed hold is a
`decision`." `start` and `done` belong to a working session's own hooks and are
never a shipped tool's to send, so the allowed set here is exactly two and it is
enforced in code rather than left to a caller's habit.

**A failed ping never fails the verb that triggered it.**
`operations/notify/README.md` names the same principle for a session, and here
the operator is already looking at the terminal that printed the real result. So
this returns a verdict rather than raising, and the caller says on stdout whether
the phone got it — CLAUDE.md's "if a send fails, say so".

Nothing in this module reads or handles the ntfy topic. `notify.sh` owns that
secret and keeps it off the command line; this only chooses an event name and a
one-line message.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Protocol

ROOT: Final = Path(__file__).resolve().parents[2]
NOTIFY_SCRIPT: Final = ROOT / "operations" / "notify" / "notify.sh"

ALLOWED_EVENTS: Final = frozenset({"milestone", "decision"})
NOTIFY_TIMEOUT_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """Whether the phone got it, in words the surface can print unchanged."""

    attempted: bool
    delivered: bool
    detail: str

    def line(self) -> str:
        if not self.attempted:
            return f"Phone notification: not sent ({self.detail})."
        if self.delivered:
            return "Phone notification: sent."
        return f"Phone notification: NOT DELIVERED ({self.detail}). The result above still stands."


class Notifier(Protocol):
    """The seam every verb uses, so no test can reach the real script."""

    def __call__(self, event: str, message: str) -> NotifyOutcome:
        """Send one standing moment and report honestly whether it arrived."""


def silent(event: str, message: str) -> NotifyOutcome:
    """The default: attempt nothing, and say that plainly rather than implying success."""

    del event, message
    return NotifyOutcome(False, False, "notifications are switched off for this command")


def shell_notifier(
    *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> Notifier:
    """A `Notifier` backed by `operations/notify/notify.sh`."""

    def notify(event: str, message: str) -> NotifyOutcome:
        if event not in ALLOWED_EVENTS:
            # A caller bug, not a delivery failure — but it must not raise out of
            # a verb that has already done its real work.
            return NotifyOutcome(False, False, f"{event!r} is not a moment this tool may send")
        if "\n" in message or not message.strip():
            return NotifyOutcome(False, False, "the message was not one non-empty line")
        try:
            result = runner(
                ["sh", str(NOTIFY_SCRIPT), event, message],
                capture_output=True,
                text=True,
                check=False,
                timeout=NOTIFY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return NotifyOutcome(
                True,
                False,
                f"the notification command did not answer within {NOTIFY_TIMEOUT_SECONDS:g} seconds",
            )
        except OSError as error:
            return NotifyOutcome(True, False, f"the notification command could not run: {error}")
        if result.returncode == 0:
            return NotifyOutcome(True, True, "delivered")
        detail = " ".join((result.stderr or result.stdout or "no reason given").split())
        if len(detail) > 160:
            detail = f"{detail[:160]} (reason truncated at 160 characters)"
        return NotifyOutcome(True, False, detail)

    return notify


__all__ = [
    "ALLOWED_EVENTS",
    "NOTIFY_TIMEOUT_SECONDS",
    "Notifier",
    "NotifyOutcome",
    "shell_notifier",
    "silent",
]
