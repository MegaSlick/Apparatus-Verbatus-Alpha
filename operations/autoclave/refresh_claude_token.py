#!/usr/bin/env python3
"""Refresh the Claude access token stored in the chamber credential volume.

Claude Code does not refresh an expired access token in the print-mode dispatch used by
the autoclave. This helper runs inside the container, locks the shared credential volume,
exchanges the still-valid refresh token, and atomically publishes both rotated tokens.
No token is printed or handled on the host.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import math
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Public client identifier shipped in Claude Code 2.1.220; it identifies the CLI, not a user.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SKEW_SECONDS = 600
DEADLINE_SECONDS = 60


class RefreshDeadline(Exception):
    pass


class ConcurrentCredential(Exception):
    pass


class RestoreConcurrentCredential(Exception):
    """A concurrent credential could not be restored after publication began."""

    def __init__(self, preserved: Path, cause: OSError):
        self.preserved = preserved
        self.cause = cause
        super().__init__(
            "credential changed concurrently and restoration failed; "
            f"the displaced credential was preserved at {preserved} ({type(cause).__name__})"
        )


def credential_path() -> Path:
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/agent/.claude"))
    return config_dir / ".credentials.json"


def exchange_paths(first: Path, second: Path) -> None:
    """Atomically exchange two existing paths on Linux or macOS."""
    library = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(first)
    second_bytes = os.fsencode(second)
    if sys.platform.startswith("linux"):
        renameat2 = library.renameat2
        result = renameat2(-100, first_bytes, -100, second_bytes, 2)
    elif sys.platform == "darwin":
        renamex_np = library.renamex_np
        result = renamex_np(first_bytes, second_bytes, 2)
    else:
        raise OSError(errno.ENOTSUP, "atomic path exchange is unsupported")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def publish(path: Path, document: dict[str, object], expected: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credentials.tmp.")
    preserve_temporary = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        exchange_paths(Path(temporary), path)
        displaced = Path(temporary).read_bytes()
        if displaced != expected:
            try:
                exchange_paths(Path(temporary), path)
            except OSError as error:
                # The first exchange already put the new credential at `path`; the
                # temporary therefore holds the concurrent writer's complete value.
                # It is the only recoverable copy of that value, so never clean it
                # up when the restorative exchange fails.
                preserve_temporary = True
                raise RestoreConcurrentCredential(Path(temporary), error) from error
            raise ConcurrentCredential
        os.unlink(temporary)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if preserve_temporary:
            raise
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def lifetime_milliseconds(value: object) -> int | None:
    """Return a positive, finite response lifetime as whole milliseconds.

    JSON booleans are Python integers, so they need an explicit refusal. Converting
    through float lets one rule reject NaN, infinities, and integers too large for a
    usable timestamp without treating an endpoint response as a programming error.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    milliseconds = seconds * 1000
    if not math.isfinite(milliseconds):
        return None
    whole_milliseconds = int(milliseconds)
    return whole_milliseconds if whole_milliseconds >= 1 else None


def refresh() -> int:
    path = credential_path()
    try:
        lock = open(path.parent / ".credentials.autoclave.lock", "a+", encoding="utf-8")
    except OSError as error:
        print(f"refresh: cannot open the credential lock ({type(error).__name__})", file=sys.stderr)
        return 2

    with lock:
        lock_deadline = time.monotonic() + DEADLINE_SECONDS
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= lock_deadline:
                    print("refresh: credential lock wait exceeded 60 seconds", file=sys.stderr)
                    return 1
                time.sleep(0.05)
        try:
            original = path.read_bytes()
            document = json.loads(original)
            oauth = document["claudeAiOauth"]
            if not isinstance(oauth, dict):
                raise TypeError("claudeAiOauth is not an object")
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(
                f"refresh: cannot read a Claude credential ({type(error).__name__})",
                file=sys.stderr,
            )
            return 2

        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, int) and expires_at > (time.time() + SKEW_SECONDS) * 1000:
            print("refresh: access token still valid")
            return 0

        refresh_token = oauth.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            print("refresh: no usable refresh token", file=sys.stderr)
            return 1

        request = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Required by the endpoint; omitting it produces Cloudflare error 1010.
                "User-Agent": "claude-cli (autoclave refresh)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            print(f"refresh: token endpoint refused HTTP {error.code}", file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f"refresh: token endpoint failed ({type(error).__name__})", file=sys.stderr)
            return 1

        # The deadline bounds waiting on the remote endpoint, not local publication.
        # Once the complete response is parsed, disarm it before touching the rotated
        # state: a late SIGALRM between validation and os.replace must not interrupt an
        # otherwise complete credential update.
        signal.setitimer(signal.ITIMER_REAL, 0)

        if not isinstance(payload, dict):
            print("refresh: token endpoint returned an invalid response", file=sys.stderr)
            return 1

        # Validate every response field that can affect the stored credential before
        # changing the in-memory document. A refusal must leave both the old access
        # token and the old refresh state intact, not merely avoid a later publish.
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            print("refresh: token endpoint returned no access token", file=sys.stderr)
            return 1
        rotated = payload.get("refresh_token")
        if rotated is not None and (not isinstance(rotated, str) or not rotated):
            print("refresh: token endpoint returned an invalid refresh token", file=sys.stderr)
            return 1
        access_lifetime = lifetime_milliseconds(payload.get("expires_in"))
        if access_lifetime is None:
            print("refresh: token endpoint returned an invalid lifetime", file=sys.stderr)
            return 1
        refresh_lifetime = payload.get("refresh_token_expires_in")
        refresh_lifetime_ms = (
            None if refresh_lifetime is None else lifetime_milliseconds(refresh_lifetime)
        )
        if refresh_lifetime is not None and refresh_lifetime_ms is None:
            print("refresh: token endpoint returned an invalid refresh lifetime", file=sys.stderr)
            return 1

        now_ms = int(time.time() * 1000)
        oauth["accessToken"] = access_token
        if rotated is not None:
            oauth["refreshToken"] = rotated
        oauth["expiresAt"] = now_ms + access_lifetime
        if refresh_lifetime_ms is not None:
            oauth["refreshTokenExpiresAt"] = now_ms + refresh_lifetime_ms

        try:
            publish(path, document, original)
        except RestoreConcurrentCredential as error:
            print(f"refresh: {error}", file=sys.stderr)
            return 2
        except ConcurrentCredential:
            print(
                "refresh: credential changed concurrently; rotated tokens were not published — "
                "retry, and sign in again if the refresh token was invalidated",
                file=sys.stderr,
            )
            return 1
        except OSError as error:
            print(
                f"refresh: cannot publish the credential ({type(error).__name__})", file=sys.stderr
            )
            return 2

        valid_to = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(oauth["expiresAt"] / 1000))
        print(f"refresh: new access token valid to {valid_to}")
        return 0


def main() -> int:
    def timed_out(_signum, _frame):
        raise RefreshDeadline

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        return refresh()
    except RefreshDeadline:
        print(
            f"refresh: credential refresh exceeded {DEADLINE_SECONDS} seconds",
            file=sys.stderr,
        )
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
