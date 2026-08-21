"""Non-blocking spend warnings delivered through ``operations/notify``.

The spend floor is enforced by the runtime, never by a phone delivery. This
bridge only carries warnings above that floor; its explicit seam keeps fake
provider tests offline and prevents a notification failure from changing a
paid-action result.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Protocol

ROOT: Final = Path(__file__).resolve().parents[2]
NOTIFY_SCRIPT: Final = ROOT / "operations" / "notify" / "notify.sh"
NOTIFY_TIMEOUT_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    attempted: bool
    delivered: bool
    detail: str

    def line(self) -> str:
        """One printable line, in the same words the operator surface uses.

        ``operations/notify/README.md``: "If a send fails, say so." A warning
        that never reached the phone must not read like one that did.
        """

        if not self.attempted:
            return f"Phone notification: not sent ({self.detail})."
        if self.delivered:
            return "Phone notification: sent."
        return f"Phone notification: NOT DELIVERED ({self.detail}). The result above still stands."


class Notifier(Protocol):
    def __call__(self, message: str) -> NotifyOutcome:
        """Attempt one notification-only spend warning."""


def silent(message: str) -> NotifyOutcome:
    """The safe default: no process silently sends a phone notification."""

    del message
    return NotifyOutcome(False, False, "spend notifications are switched off")


def shell_notifier(
    *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> Notifier:
    """Use the existing notification script with its ordinary milestone event."""

    def notify(message: str) -> NotifyOutcome:
        if "\n" in message or not message.strip():
            return NotifyOutcome(False, False, "the message was not one non-empty line")
        try:
            result = runner(
                ["sh", str(NOTIFY_SCRIPT), "milestone", message],
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
        # notify.sh prints its own `NOT DELIVERED (reason)`; keeping only "did not
        # confirm delivery" threw away the one line that says whether the topic is
        # missing, the message was malformed, or ntfy refused it.  Bounded, and one
        # line, because this is printed into a record.
        detail = " ".join((result.stderr or result.stdout or "no reason given").split())
        if len(detail) > 160:
            detail = f"{detail[:160]} (reason truncated at 160 characters)"
        return NotifyOutcome(True, False, detail)

    return notify
