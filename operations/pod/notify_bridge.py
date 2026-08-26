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
        """Distinguish failed delivery from a warning that reached the phone."""

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
    """Use ``milestone`` because a spend warning requests no decision."""

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
        # Preserve notify.sh's bounded one-line reason so the recorded failure
        # distinguishes a missing topic, malformed message, and ntfy refusal.
        detail = " ".join((result.stderr or result.stdout or "no reason given").split())
        if len(detail) > 160:
            detail = f"{detail[:160]} (reason truncated at 160 characters)"
        return NotifyOutcome(True, False, detail)

    return notify
