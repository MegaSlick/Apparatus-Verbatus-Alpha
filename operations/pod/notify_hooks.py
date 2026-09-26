"""A vendor-neutral phone-notification seam for the three pod-lease moments.

Spend machinery is tracking plus notifications only -- no new enforcement,
RunPod's own limits enforce. This module is the notification half of that: it
never refuses a launch, never blocks a close, and never changes what any of
this package's spend gates decide. It only tells `operations/notify/notify.sh`
one short line, three times in a lease's life --

- launch: the lease id, the card, and the hourly ceiling that governs it
- close: the lease id, the verified close state, and the billed window
- each balance observation: the balance and the spend rate the observer
  reported

-- through `operations/notify/client.py`.

**Never a secret, never a URL.** Every message is checked before the shell
call: a word naming a secret, any piece `common.credentials` reads as
credential-shaped, or a URL, because a URL in a phone notification is a
disclosure channel `operations/pod/README.md` never asks for.
A message that fails the check is never sent, and that refusal is itself
never raised -- it comes back as an ordinary `NotifyOutcome(attempted=False,
...)`, so a bug that would have leaked a secret cannot also take down the
close or launch path that was about to report it.

**A failed ping cannot prevent a close.** Every function here returns a
`NotifyOutcome` and never raises; the caller logs `.detail` in the durable
receipt (principle 2: nothing is lost silently) and moves on.
"""

from __future__ import annotations

import re
from typing import Final

from common.credentials import notification_carries_credential
from operations.notify import client
from operations.notify.client import NotifyOutcome, Runner

NOTIFY_EVENT: Final = "milestone"
"""Every hook here reports a fact, not a question -- `operations/notify/README.md`'s
table reserves `decision` for something that needs an answer, and none of launch,
close, or a balance reading does."""


# A scheme-less host+path -- a console link, or a bare notification-service
# link, pasted without `http(s)://` -- is exactly the shape a bare scheme
# check misses. One or more dot-joined labels, an alphabetic TLD-shaped label
# of 2+ characters, then a `/` and something after it: `console.runpod.io/
# pod/abc` matches; a dotted version number followed by a path-like suffix
# (`v1.2.3/notes`) does not, because `3` is not an alphabetic TLD.
_HOST_PATH_PATTERN: Final = re.compile(r"(?:[\w-]+\.)+[a-zA-Z]{2,}/\S", re.ASCII)


def _unsafe_reason(message: str) -> str | None:
    lowered = message.lower()
    if "http://" in lowered or "https://" in lowered:
        return "the message names a URL, which this seam never sends"
    if _HOST_PATH_PATTERN.search(message):
        return "the message names a URL, which this seam never sends"
    if notification_carries_credential(message):
        return "the message looks like it carries a credential and was refused"
    return None


def _send(message: str, *, runner: Runner) -> NotifyOutcome:
    unsafe = _unsafe_reason(message)
    if unsafe is not None:
        return NotifyOutcome(False, False, unsafe)
    return client.send(NOTIFY_EVENT, message, runner=runner)


def notify_launch(
    *, lease_id: str, card: str, max_hourly_usd: object, runner: Runner = client.run
) -> NotifyOutcome:
    """One line at launch: which lease, which card, what ceiling governs it."""

    message = f"pod launch: lease {lease_id}, card {card}, ceiling ${max_hourly_usd}/h"
    return _send(message, runner=runner)


def notify_close(
    *,
    lease_id: str,
    verified_state: str,
    billed_seconds: object,
    runner: Runner = client.run,
) -> NotifyOutcome:
    """One line at close: which lease, the verified state, the billed window.

    ``verified_state`` names the outcome exactly as the close report does
    (``verified``, ``unverified``, ``pending-reconciliation``, ...) -- never
    reworded into a friendlier phrase that could read as more certain than
    `operations/pod/shutdown.py`'s own verification actually established.

    ``billed_seconds`` is pod creation to the close's *billing cutoff*, which
    is what `CloseReport` carries; no stop time is observed anywhere on this
    path, and the cutoff can stand up to `billing_cutoff_margin_seconds` past
    the moment the pod was seen gone. The message says "billed" rather than
    "ran" for that reason: a number is reported as the thing that was actually
    measured (principle 8), never as the nearer-sounding one.
    """

    message = (
        f"pod close: lease {lease_id}, {verified_state}, billed {billed_seconds}s from creation"
    )
    return _send(message, runner=runner)


def notify_balance(
    *,
    balance_usd: object,
    spend_rate_usd_per_hr: object,
    lease_id: str | None = None,
    runner: Runner = client.run,
) -> NotifyOutcome:
    """One line per observation: the balance and spend rate the observer reported.

    ``lease_id`` is optional because the account balance is not lease-scoped
    -- a preview can observe it before any lease exists -- but a caller
    observing it for an open lease may still name which one.
    """

    subject = f"lease {lease_id}" if lease_id else "account"
    message = (
        f"pod balance: {subject}, ${balance_usd} available, ${spend_rate_usd_per_hr}/h spend rate"
    )
    return _send(message, runner=runner)
