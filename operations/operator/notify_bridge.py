"""The two notification event classes this surface is allowed to send.

`run`/`export` completion and a spend-threshold crossing are both a
`milestone`; a decision-needed hold is a `decision`. `start` and `done`
belong to a working session's own hooks, never a shipped tool's to send, so
the allowed event set here stays exactly two, enforced in code.

A failed ping never fails the verb that triggered it: the operator is
already looking at the terminal that printed the real result, so this
returns a verdict rather than raising, and the caller says on stdout
whether the phone got it.
"""

from __future__ import annotations

from typing import Final, Protocol

from operations.notify import client
from operations.notify.client import NotifyOutcome

ALLOWED_EVENTS: Final = frozenset({"milestone", "decision"})


class Notifier(Protocol):
    """The seam every verb uses, so no test can reach the real script."""

    def __call__(self, event: str, message: str) -> NotifyOutcome:
        """Send one standing moment and report honestly whether it arrived."""


def silent(event: str, message: str) -> NotifyOutcome:
    """The default: attempt nothing, and say that plainly rather than implying success."""

    del event, message
    return NotifyOutcome(False, False, "notifications are switched off for this command")


def shell_notifier(*, runner: client.Runner = client.run) -> Notifier:
    def notify(event: str, message: str) -> NotifyOutcome:
        if event not in ALLOWED_EVENTS:
            return NotifyOutcome(False, False, f"{event!r} is not a moment this tool may send")
        return client.send(event, message, runner=runner)

    return notify


__all__ = ["ALLOWED_EVENTS", "Notifier", "NotifyOutcome", "shell_notifier", "silent"]
