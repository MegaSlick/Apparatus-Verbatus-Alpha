#!/usr/bin/env python3
"""Refresh the Claude access token stored in the chamber credential volume.

Claude Code does not refresh an expired access token in the print-mode dispatch used by
the autoclave. This helper runs inside the container, locks the shared credential volume,
exchanges the still-valid refresh token, and atomically publishes both rotated tokens.
No token is printed or handled on the host.
"""

from __future__ import annotations

import fcntl
import json
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


def credential_path() -> Path:
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/agent/.claude"))
    return config_dir / ".credentials.json"


def publish(path: Path, document: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credentials.tmp.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def refresh() -> int:
    path = credential_path()
    try:
        lock = open(path.parent / ".credentials.autoclave.lock", "a+", encoding="utf-8")
    except OSError as error:
        print(f"refresh: cannot open the credential lock ({type(error).__name__})", file=sys.stderr)
        return 2

    with lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
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

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            print("refresh: token endpoint returned no access token", file=sys.stderr)
            return 1

        now_ms = int(time.time() * 1000)
        oauth["accessToken"] = access_token
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            oauth["refreshToken"] = rotated
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, int) or expires_in <= 0:
            print("refresh: token endpoint returned an invalid lifetime", file=sys.stderr)
            return 1
        oauth["expiresAt"] = now_ms + expires_in * 1000
        refresh_lifetime = payload.get("refresh_token_expires_in")
        if isinstance(refresh_lifetime, int):
            oauth["refreshTokenExpiresAt"] = now_ms + refresh_lifetime * 1000

        try:
            publish(path, document)
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
