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
    attempted: bool
    delivered: bool
    detail: str
    suppressed: bool = False
    """The test sink swallowed it: attempted, not delivered, and not a failure.

    A third state rather than a reworded failure. `delivered=False` alone would
    report the sink as a delivery problem in a record an operator reads for real
    ones, and `delivered=True` is the lie this field exists to stop."""

    def line(self) -> str:
        """Distinguish failed delivery from a warning that reached the phone."""

        if not self.attempted:
            return f"Phone notification: not sent ({self.detail})."
        if self.suppressed:
            return "Phone notification: suppressed (test sink)."
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
            marker = _suppression_marker(result.stdout)
            if marker is not None:
                return NotifyOutcome(True, False, marker, suppressed=True)
            return NotifyOutcome(True, True, "delivered")
        # Preserve notify.sh's bounded one-line reason so the recorded failure
        # distinguishes a missing topic, malformed message, and ntfy refusal.
        detail = " ".join((result.stderr or result.stdout or "no reason given").split())
        if len(detail) > 160:
            detail = f"{detail[:160]} (reason truncated at 160 characters)"
        return NotifyOutcome(True, False, detail)

    return notify
