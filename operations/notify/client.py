"""Run `notify.sh` once and say what happened: delivered, suppressed by the test sink, or not.

A failed ping never fails the caller: every outcome comes back as a value. The topic is
`notify.sh`'s secret; nothing here reads or carries it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Sequence

NOTIFY_SCRIPT: Final = Path(__file__).with_name("notify.sh")
NOTIFY_TIMEOUT_SECONDS: Final = 10.0
SUPPRESSED_MARKER: Final = "NOTIFY_SUPPRESSED"
# `start` is notify.sh's rate-limited session-hook event; a caller's result must never be.
EVENTS: Final = frozenset({"milestone", "decision", "done"})
_DETAIL_LIMIT: Final = 160


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    attempted: bool
    delivered: bool
    detail: str
    suppressed: bool = False

    def line(self) -> str:
        if not self.attempted:
            return f"Phone notification: not sent ({self.detail})."
        if self.suppressed:
            return "Phone notification: suppressed (test sink)."
        if self.delivered:
            return "Phone notification: sent."
        return (
            f"Phone notification: NOT DELIVERED ({self.detail}). The recorded result is unchanged."
        )


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=NOTIFY_TIMEOUT_SECONDS
    )


def _bounded(detail: str) -> str:
    detail = " ".join(detail.split())
    if len(detail) > _DETAIL_LIMIT:
        return f"{detail[:_DETAIL_LIMIT]} (reason truncated at {_DETAIL_LIMIT} characters)"
    return detail


def send(event: str, message: str, *, runner: Runner = run) -> NotifyOutcome:
    if event not in EVENTS:
        return NotifyOutcome(False, False, f"{event!r} is not an event this client sends")
    if "\n" in message or "\x00" in message or not message.strip():
        return NotifyOutcome(False, False, "the message was not one non-empty line")
    try:
        return _read(runner(["sh", str(NOTIFY_SCRIPT), event, message]))
    except subprocess.TimeoutExpired:
        return NotifyOutcome(
            True,
            False,
            f"the notification command did not answer within {NOTIFY_TIMEOUT_SECONDS:g} seconds",
        )
    except OSError as error:
        return NotifyOutcome(True, False, f"the notification command could not run: {error}")
    except Exception as error:  # noqa: BLE001 -- e.g. UnicodeDecodeError from text=True
        return NotifyOutcome(
            True, False, _bounded(f"the notification command failed unexpectedly: {error!r}")
        )


def _read(result: subprocess.CompletedProcess) -> NotifyOutcome:
    if result.returncode != 0:
        return NotifyOutcome(
            True, False, _bounded(result.stderr or result.stdout or "no reason given")
        )
    # notify.sh exits 0 for the test sink too; its stdout marker is the only difference.
    for stdout_line in (result.stdout or "").splitlines():
        if stdout_line.strip().split(" ", 1)[0] == SUPPRESSED_MARKER:
            return NotifyOutcome(True, False, stdout_line.strip()[:_DETAIL_LIMIT], suppressed=True)
    return NotifyOutcome(True, True, "delivered")
