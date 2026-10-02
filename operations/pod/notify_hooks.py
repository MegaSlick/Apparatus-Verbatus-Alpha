"""A vendor-neutral phone-notification seam for the pod-lease moments and a systemic run.

Spend machinery is tracking plus notifications only -- no new enforcement,
RunPod's own limits enforce. This module is the notification half of that: it
never refuses a launch, never blocks a close, and never changes what any of
this package's spend gates decide. It only tells `operations/notify/notify.sh`
one short line, three times in a lease's life --

- launch: the lease id, the card, and the hourly ceiling that governs it
- close: the lease id, the verified close state, and the billed window
- each balance observation: the balance and the spend rate the observer
  reported

-- through `operations/notify/client.py`. A fourth, `notify_systemic`, is the
one question among them: a run on the pod that stopped with more of its pages
held than its sealed review policy allows, or exported past that stop on a
person's advance, sends the systemic alarm as a `decision`, the line
`verbatus run --notify` sends for the same run on this computer.

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
receipt, since nothing is lost silently, and moves on.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Final

from common.credentials import notification_carries_credential
from common.review_policy import systemic_notice
from operations.notify import client
from operations.notify.client import NotifyOutcome, Runner
from operations.pod.models import POD_GUARD_DIRECTORY

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


def _send(message: str, *, runner: Runner, event: str = NOTIFY_EVENT) -> NotifyOutcome:
    unsafe = _unsafe_reason(message)
    if unsafe is not None:
        return NotifyOutcome(False, False, unsafe)
    return client.send(event, message, runner=runner)


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
    measured, never as the nearer-sounding one.
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


def notify_systemic(*, run_id: str, alarm_line: str, runner: Runner = client.run) -> NotifyOutcome:
    """One `decision` line for a run whose systemic alarm sounded (`common.review_policy`)."""

    return _send(systemic_notice(run_id, alarm_line), runner=runner, event="decision")


# The file under the pod guard's directory on the volume holding the topic it
# pings (`operations/pod/README.md`, "Arming the ping").
GUARD_TOPIC_NAME: Final = "ntfy_topic"
_TOPIC: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Removed from anywhere in the file before the format check, as the pod guard
# removes them before it pings.
_TOPIC_IGNORED_BYTES: Final = b" \r\n"
# A topic is at most 64 characters; this leaves room for a line ending and stray
# spaces. A file larger than this is not a topic, and nothing more is read.
_TOPIC_READ_BYTES: Final = 256
# What the notification command needs from the pod's environment to reach the
# service; nothing else of it is passed on.
_PASSED_ENVIRONMENT: Final = (
    "PATH",
    "HTTPS_PROXY",
    "https_proxy",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
)
NO_GUARD_TOPIC: Final = "no usable guard topic"


def guard_topic_path(volume_mount: Path) -> Path:
    """Where the pod guard keeps its topic: its directory on the volume mount."""
    return Path(volume_mount) / POD_GUARD_DIRECTORY / GUARD_TOPIC_NAME


def guard_topic(volume_mount: Path) -> str | None:
    """The topic the pod guard pings, read from its file on the volume; None when absent or bad.

    Spaces, carriage returns and newlines are removed wherever they stand, as
    the pod guard removes them, and what is left must be a topic. Only a
    regular file, never followed through a link, and at most
    `_TOPIC_READ_BYTES` of it, so a FIFO, a device or a huge file cannot hold
    the run.
    """
    path = guard_topic_path(volume_mount)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            data = os.read(descriptor, _TOPIC_READ_BYTES + 1)
        finally:
            os.close(descriptor)
        topic = (
            data.translate(None, _TOPIC_IGNORED_BYTES).decode("utf-8")
            if len(data) <= _TOPIC_READ_BYTES
            else ""
        )
    except (OSError, UnicodeDecodeError):
        return None
    return topic if _TOPIC.fullmatch(topic) else None


def notify_environment(topic: str) -> dict[str, str]:
    """The one environment the notification command runs in: the topic and what it needs."""
    environment = {name: os.environ[name] for name in _PASSED_ENVIRONMENT if name in os.environ}
    environment["NTFY_TOPIC"] = topic
    return environment


def environment_runner(environment: Mapping[str, str]) -> Runner:
    """`client.run` bound to `environment`: the notification command's, and only its."""
    return partial(client.run, env=dict(environment))


RunnerFactory = Callable[[Mapping[str, str]], Runner]


def notify_systemic_from_guard(
    *, run_id: str, alarm_line: str, volume_mount: Path, runner_factory: RunnerFactory
) -> NotifyOutcome:
    """The systemic alarm sent with the pod guard's topic, in the notification command's own
    environment; with no usable guard topic nothing runs, so the command never falls back to a
    topic of the checkout's."""
    topic = guard_topic(volume_mount)
    if topic is None:
        return NotifyOutcome(False, False, NO_GUARD_TOPIC)
    return notify_systemic(
        run_id=run_id, alarm_line=alarm_line, runner=runner_factory(notify_environment(topic))
    )
