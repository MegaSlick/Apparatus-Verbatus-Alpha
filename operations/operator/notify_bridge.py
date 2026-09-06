"""The two notification event classes this surface is allowed to send.

Spec 12: "the four standing moments only (`operations/notify` exists);
`run`/`export` completion is a `milestone`, a decision-needed hold is a
`decision`." A spend warning is also a `milestone`: it reports an observed
threshold crossing and never asks for a decision or changes a launch result.
`start` and `done` belong to a working session's own hooks and are never a
shipped tool's to send, so the allowed event set here remains exactly two and is
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


NOTIFY_SUPPRESSED_MARKER: Final = "NOTIFY_SUPPRESSED"
"""`notify.sh` prints this word, then the reserved topic, on stdout and exits 0
when the test sink swallowed the notification instead of posting it.

Mapping that exit 0 to `delivered` was the defect this constant closes: under the
sink the record said "Phone notification: sent." for a notification that never
left the machine. The exit code stays 0 on purpose -- suites assert on delivered
versus NOT DELIVERED outcomes and a guard must not change what its subject
measures -- so the marker is what separates the two, on the one stream this
script writes nothing else to. The word is matched, not the topic: the topic is
normally a bearer secret and no bridge carries it."""


def _suppression_marker(stdout: str | None) -> str | None:
    """The marker line if the sink swallowed this notification, else `None`."""

    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped == NOTIFY_SUPPRESSED_MARKER or stripped.startswith(
            f"{NOTIFY_SUPPRESSED_MARKER} "
        ):
            return stripped[:160]
    return None


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """Whether the phone got it, in words the surface can print unchanged."""

    attempted: bool
    delivered: bool
    detail: str
    suppressed: bool = False
    """The test sink swallowed it: attempted, not delivered, and not a failure.

    A third state rather than a reworded failure. `delivered=False` alone would
    report the sink as a delivery problem in a record an operator reads for real
    ones, and `delivered=True` is the lie this field exists to stop."""

    def line(self) -> str:
        if not self.attempted:
            return f"Phone notification: not sent ({self.detail})."
        if self.suppressed:
            return "Phone notification: suppressed (test sink)."
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
            marker = _suppression_marker(result.stdout)
            if marker is not None:
                return NotifyOutcome(True, False, marker, suppressed=True)
            return NotifyOutcome(True, True, "delivered")
        detail = " ".join((result.stderr or result.stdout or "no reason given").split())
        if len(detail) > 160:
            detail = f"{detail[:160]} (reason truncated at 160 characters)"
        return NotifyOutcome(True, False, detail)

    return notify


__all__ = [
    "ALLOWED_EVENTS",
    "NOTIFY_SUPPRESSED_MARKER",
    "NOTIFY_TIMEOUT_SECONDS",
    "Notifier",
    "NotifyOutcome",
    "shell_notifier",
    "silent",
]
