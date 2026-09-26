"""Non-blocking spend warnings delivered through ``operations/notify``.

The spend floor is enforced by the runtime, never by a phone delivery. This
bridge only carries warnings above that floor; its explicit seam keeps fake
provider tests offline and prevents a notification failure from changing a
paid-action result.
"""

from __future__ import annotations

from typing import Protocol

from operations.notify import client
from operations.notify.client import NotifyOutcome


class Notifier(Protocol):
    def __call__(self, message: str) -> NotifyOutcome:
        """Attempt one notification-only spend warning."""


def silent(message: str) -> NotifyOutcome:
    """The safe default: no process silently sends a phone notification."""

    del message
    return NotifyOutcome(False, False, "spend notifications are switched off")


def shell_notifier(*, runner: client.Runner = client.run) -> Notifier:
    """Use ``milestone`` because a spend warning requests no decision."""

    def notify(message: str) -> NotifyOutcome:
        return client.send("milestone", message, runner=runner)

    return notify
