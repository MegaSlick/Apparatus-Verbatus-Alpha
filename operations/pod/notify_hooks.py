"""A vendor-neutral phone-notification seam for the three pod-lease moments.

Ruling (b) in `workbench/standing/TYREL_RULINGS_2026-09-01_BUILD_SESSION.md`:
spend machinery is tracking plus notifications only -- no new enforcement,
RunPod's own limits enforce. This module is the notification half of that: it
never refuses a launch, never blocks a close, and never changes what any of
this package's spend gates decide. It only tells `operations/notify/notify.sh`
one short line, three times in a lease's life --

- launch: the lease id, the card, and the hourly ceiling that governs it
- close: the lease id, the verified close state, and the billed window
- each balance observation: the balance and the spend rate the observer
  reported

-- exactly as `operations/pod/notify_bridge.py` already does for spend-floor
warnings, and reusing that module's `NotifyOutcome` shape so a caller reading
either seam's result reads the same fields. `notify_bridge.py` is left
untouched: it is a narrower seam (one warning kind, gated behind `--notify`
and opt-in `silent` by default) and this module does not change its contract.

**Never a secret, never a URL.** Every message is checked, before the shell
call, against the same two independent markers this codebase already uses
elsewhere to flag a value as credential-shaped -- a marker word in the text
(`models.looks_like_credential_field`'s word list) and an opaque,
separator-free run of 20+ mixed alphanumeric characters
(`models.looks_like_credential_value`'s shape test) -- plus a bare
`http://`/`https://` substring, because a URL in a phone notification is a
disclosure channel `operations/pod/README.md` never asks for. The check is
local to this module rather than importing either private helper: neither
`models.py` nor `bootstrap_main.py` are owned by this seam, and a notification
line is free-form prose, not an argv token or an environment name, so the two
shape tests are re-expressed here against whitespace-split words instead.
A message that fails the check is never sent, and that refusal is itself
never raised -- it comes back as an ordinary `NotifyOutcome(attempted=False,
...)`, so a bug that would have leaked a secret cannot also take down the
close or launch path that was about to report it.

**A failed ping cannot prevent a close.** Every function here returns a
`NotifyOutcome` rather than raising for a transport failure, a timeout, or a
non-zero exit from `notify.sh` -- the caller logs `.detail` in the durable
receipt (GOVERNANCE 2: nothing is lost silently) and moves on. Only a
malformed message (not one non-empty line) or a message this module refuses
on sight is reported the same way, never as an exception.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Sequence

ROOT: Final = Path(__file__).resolve().parents[2]
NOTIFY_SCRIPT: Final = ROOT / "operations" / "notify" / "notify.sh"
NOTIFY_TIMEOUT_SECONDS: Final = 10.0
NOTIFY_EVENT: Final = "milestone"
"""Every hook here reports a fact, not a question -- `operations/notify/README.md`'s
table reserves `decision` for something that needs an answer, and none of launch,
close, or a balance reading does."""


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
    """Same shape as `notify_bridge.NotifyOutcome`, defined again rather than
    imported: that module is a distinct, narrower seam (spend-floor warnings
    only) and this one must not gain a dependency on it to change its own
    behavior."""

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
        return (
            f"Phone notification: NOT DELIVERED ({self.detail}). Nothing else was changed by this."
        )


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """The real script: `sh operations/notify/notify.sh <event> "<message>"`."""

    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=NOTIFY_TIMEOUT_SECONDS
    )


_CREDENTIAL_WORD_MARKERS: Final = (
    "key",
    "secret",
    "password",
    "credential",
    "bearer",
    "token",
    "apikey",
)
_CREDENTIAL_VALUE_PREFIXES: Final = ("sk-", "hf_", "ghp_", "gho_", "github_pat_", "AKIA", "xox")
# `.`, `/`, `:` and `@` are deliberately NOT here: those are exactly the
# separators a credential-shaped opaque run (a JWT's dot-joined segments, a
# base64 blob's `/` and `=` padding) or a console link uses to look like
# ordinary punctuated prose instead of one long unbroken run. Treating them
# as "safe" let a value built from a few such separators slip past the
# 20+-character opaque test untouched; only whitespace, backslash and the
# grouping/quoting marks a caller strips a *word* down to at its edges
# (see `_unsafe_reason`) stay safe here.
_CREDENTIAL_VALUE_SAFE_CHARACTERS: Final = frozenset(" \t\\,;()[]{}'\"")
# A scheme-less host+path -- a console link, or a bare notification-service
# link, pasted without `http(s)://` -- is exactly the shape a bare scheme
# check misses. One or more dot-joined labels, an alphabetic TLD-shaped label
# of 2+ characters, then a `/` and something after it: `console.runpod.io/
# pod/abc` matches; a dotted version number followed by a path-like suffix
# (`v1.2.3/notes`) does not, because `3` is not an alphabetic TLD.
_HOST_PATH_PATTERN: Final = re.compile(r"(?:[\w-]+\.)+[a-zA-Z]{2,}/\S", re.ASCII)


def _looks_like_credential_word(word: str) -> bool:
    normalized = word.lower().replace("-", "_")
    if any(marker in normalized for marker in _CREDENTIAL_WORD_MARKERS):
        return True
    if word.startswith(_CREDENTIAL_VALUE_PREFIXES):
        return True
    if len(word) < 20 or any(character in _CREDENTIAL_VALUE_SAFE_CHARACTERS for character in word):
        return False
    if all(character in "0123456789abcdef" for character in word):
        return False
    return any(character.isalpha() for character in word) and any(
        character.isdigit() for character in word
    )


def _unsafe_reason(message: str) -> str | None:
    lowered = message.lower()
    if "http://" in lowered or "https://" in lowered:
        return "the message names a URL, which this seam never sends"
    if _HOST_PATH_PATTERN.search(message):
        return "the message names a URL, which this seam never sends"
    for word in message.split():
        stripped = word.strip("\"'(),;:")
        if stripped and _looks_like_credential_word(stripped):
            return f"the message looks like it carries a credential ({word!r}) and was refused"
    return None


def _send(message: str, *, runner: Runner) -> NotifyOutcome:
    if "\n" in message or "\x00" in message or not message.strip():
        return NotifyOutcome(False, False, "the message was not one non-empty line")
    unsafe = _unsafe_reason(message)
    if unsafe is not None:
        return NotifyOutcome(False, False, unsafe)
    try:
        result = runner(["sh", str(NOTIFY_SCRIPT), NOTIFY_EVENT, message])
    except subprocess.TimeoutExpired:
        return NotifyOutcome(
            True,
            False,
            f"the notification command did not answer within {NOTIFY_TIMEOUT_SECONDS:g} seconds",
        )
    except OSError as error:
        return NotifyOutcome(True, False, f"the notification command could not run: {error}")
    except Exception as error:  # noqa: BLE001 -- "never raised" is the promise; contain, don't propagate
        # `subprocess.run(..., text=True)` decodes strictly and can raise
        # `UnicodeDecodeError` on the child's own stderr/stdout, whose `repr`
        # embeds the offending bytes -- unbounded, that can run to tens of
        # thousands of characters. Bound it the same way the returncode path
        # below bounds the child's own reason, so a broken pipe never blows
        # up the printed record.
        detail = f"the notification command failed unexpectedly: {error!r}"
        if len(detail) > 160:
            detail = f"{detail[:160]} (reason truncated at 160 characters)"
        return NotifyOutcome(True, False, detail)
    if result.returncode == 0:
        marker = _suppression_marker(result.stdout)
        if marker is not None:
            return NotifyOutcome(True, False, marker, suppressed=True)
        return NotifyOutcome(True, True, "delivered")
    # Preserve notify.sh's bounded one-line reason so the recorded failure
    # distinguishes a missing topic, malformed message, and ntfy refusal --
    # matching notify_bridge.shell_notifier's own truncation bound.
    detail = " ".join((result.stderr or result.stdout or "no reason given").split())
    if len(detail) > 160:
        detail = f"{detail[:160]} (reason truncated at 160 characters)"
    return NotifyOutcome(True, False, detail)


def notify_launch(
    *, lease_id: str, card: str, max_hourly_usd: object, runner: Runner = default_runner
) -> NotifyOutcome:
    """One line at launch: which lease, which card, what ceiling governs it."""

    message = f"pod launch: lease {lease_id}, card {card}, ceiling ${max_hourly_usd}/h"
    return _send(message, runner=runner)


def notify_close(
    *,
    lease_id: str,
    verified_state: str,
    billed_seconds: object,
    runner: Runner = default_runner,
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
    measured (GOVERNANCE 10), never as the nearer-sounding one.
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
    runner: Runner = default_runner,
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
