from __future__ import annotations

import errno
import io
import json
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from operations.autoclave import refresh_claude_token as refresh


class Response(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def write_credential(tmp_path, monkeypatch, *, expires_at: int):
    config = tmp_path / ".claude"
    config.mkdir(parents=True)
    path = config / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": expires_at,
                    "refreshTokenExpiresAt": expires_at + 30 * 24 * 60 * 60 * 1000,
                    "subscriptionType": "kept",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return path


def test_a_live_access_token_never_reaches_the_endpoint(tmp_path, monkeypatch, capsys):
    path = write_credential(
        tmp_path,
        monkeypatch,
        expires_at=int((time.time() + refresh.SKEW_SECONDS + 60) * 1000),
    )
    before = path.read_bytes()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("the endpoint was called for a live token")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_call)
    assert refresh.main() == 0
    assert path.read_bytes() == before
    assert capsys.readouterr().out == "refresh: access token still valid\n"


def test_an_expired_access_token_is_atomically_replaced_with_both_rotated_tokens(
    tmp_path, monkeypatch, capsys
):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    observed = {}
    real_exchange = refresh.exchange_paths

    def observe_exchange(source, destination):
        observed["exchange"] = (source, destination)
        # Until the single publication boundary, readers still see the complete old
        # credential and the temporary holds the complete new one.
        assert json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == (
            "old-access"
        )
        assert (
            json.loads(open(source, encoding="utf-8").read())["claudeAiOauth"]["accessToken"]
            == "new-access"
        )
        real_exchange(source, destination)

    def endpoint(request, *, timeout):
        observed["timeout"] = timeout
        observed["url"] = request.full_url
        observed["headers"] = dict(request.header_items())
        observed["request"] = json.loads(request.data)
        return Response(
            json.dumps(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 28_800,
                    "refresh_token_expires_in": 2_000_000,
                }
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    monkeypatch.setattr(refresh, "exchange_paths", observe_exchange)
    assert refresh.main() == 0

    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "new-access"
    assert oauth["refreshToken"] == "new-refresh"
    assert oauth["subscriptionType"] == "kept"
    assert oauth["expiresAt"] > int(time.time() * 1000)
    assert observed["request"] == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "client_id": refresh.CLIENT_ID,
    }
    assert observed["headers"]["User-agent"]
    assert observed["url"] == "https://platform.claude.com/v1/oauth/token"
    assert observed["timeout"] == 45
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert observed["exchange"][1] == path
    output = capsys.readouterr().out
    assert "new-access" not in output and "new-refresh" not in output
    assert list(path.parent.glob(".credentials.tmp.*")) == []


def test_an_unrotated_refresh_token_is_preserved_and_expiries_are_updated(tmp_path, monkeypatch):
    now_seconds = 1_700_000_000
    path = write_credential(tmp_path, monkeypatch, expires_at=1)

    monkeypatch.setattr(refresh.time, "time", lambda: now_seconds)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps(
                {
                    "access_token": "new-access",
                    "expires_in": 3_600,
                    "refresh_token_expires_in": 86_400,
                }
            )
        ),
    )

    assert refresh.main() == 0

    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "new-access"
    assert oauth["refreshToken"] == "old-refresh"
    assert oauth["expiresAt"] == (now_seconds + 3_600) * 1000
    assert oauth["refreshTokenExpiresAt"] == (now_seconds + 86_400) * 1000


def test_finite_float_lifetimes_are_accepted(tmp_path, monkeypatch):
    now_seconds = 1_700_000_000
    path = write_credential(tmp_path, monkeypatch, expires_at=1)

    monkeypatch.setattr(refresh.time, "time", lambda: now_seconds)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps(
                {
                    "access_token": "new-access",
                    "expires_in": 3_600.5,
                    "refresh_token_expires_in": 86_400.25,
                }
            )
        ),
    )

    assert refresh.main() == 0

    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["expiresAt"] == now_seconds * 1000 + 3_600_500
    assert oauth["refreshTokenExpiresAt"] == now_seconds * 1000 + 86_400_250


def test_invalid_response_lifetimes_do_not_mutate_or_publish_a_credential(tmp_path, monkeypatch):
    for index, invalid in enumerate((True, False, 0, -1, 0.0001, float("nan"), float("inf"))):
        path = write_credential(tmp_path / str(index), monkeypatch, expires_at=1)
        before = path.read_bytes()
        published = []
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *_args, value=invalid, **_kwargs: Response(
                json.dumps({"access_token": "new-access", "expires_in": value})
            ),
        )
        monkeypatch.setattr(
            refresh,
            "publish",
            lambda *_args, sink=published: sink.append(True),
        )

        assert refresh.main() == 1
        assert path.read_bytes() == before
        assert published == []


def test_invalid_optional_refresh_lifetime_preserves_the_existing_credential(tmp_path, monkeypatch):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    before = path.read_bytes()

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps(
                {
                    "access_token": "new-access",
                    "expires_in": 3_600,
                    "refresh_token_expires_in": True,
                }
            )
        ),
    )

    assert refresh.main() == 1
    assert path.read_bytes() == before


def test_a_concurrent_vendor_write_is_not_overwritten(tmp_path, monkeypatch):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)

    def endpoint(*_args, **_kwargs):
        document = json.loads(path.read_text())
        document["vendorWrite"] = "kept"
        path.write_text(json.dumps(document))
        return Response(json.dumps({"access_token": "new-access", "expires_in": 3_600}))

    monkeypatch.setattr(urllib.request, "urlopen", endpoint)

    assert refresh.main() == 1
    document = json.loads(path.read_text())
    assert document["vendorWrite"] == "kept"
    assert document["claudeAiOauth"]["accessToken"] == "old-access"


def test_a_vendor_write_in_the_publication_window_is_restored(tmp_path, monkeypatch):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    original_exchange = refresh.exchange_paths
    interleaved = False
    exchanges = []

    def exchange(first, second):
        nonlocal interleaved
        exchanges.append((first, second))
        if not interleaved:
            document = json.loads(path.read_text())
            document["vendorWrite"] = "kept"
            path.write_text(json.dumps(document))
            interleaved = True
        original_exchange(first, second)

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps({"access_token": "new-access", "expires_in": 3_600})
        ),
    )
    monkeypatch.setattr(refresh, "exchange_paths", exchange)

    assert refresh.main() == 1
    document = json.loads(path.read_text())
    assert document["vendorWrite"] == "kept"
    assert document["claudeAiOauth"]["accessToken"] == "old-access"
    assert len(exchanges) == 2, "the second exchange restored the concurrent credential"


def test_a_failed_restoring_exchange_preserves_the_displaced_concurrent_credential(
    tmp_path, monkeypatch
):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    original = path.read_bytes()
    replacement = json.loads(original)
    replacement["claudeAiOauth"]["accessToken"] = "new-access"
    original_exchange = refresh.exchange_paths
    exchanges = []

    def exchange(first, second):
        exchanges.append((first, second))
        if len(exchanges) == 1:
            concurrent = json.loads(path.read_text(encoding="utf-8"))
            concurrent["vendorWrite"] = "kept"
            path.write_text(json.dumps(concurrent), encoding="utf-8")
            original_exchange(first, second)
        else:
            raise OSError(errno.EIO, "restoring exchange failed")

    monkeypatch.setattr(refresh, "exchange_paths", exchange)

    with pytest.raises(refresh.RestoreConcurrentCredential) as raised:
        refresh.publish(path, replacement, original)

    assert len(exchanges) == 2, "the first exchange and failed restoration stayed distinct"
    preserved = raised.value.preserved
    assert str(preserved) in str(raised.value)
    assert path.exists(), "the published credential must remain a complete file"
    assert (
        json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "new-access"
    )
    assert preserved.exists(), "the concurrent credential was lost during cleanup"
    displaced = json.loads(preserved.read_text(encoding="utf-8"))
    assert displaced["vendorWrite"] == "kept"
    assert displaced["claudeAiOauth"]["accessToken"] == "old-access"


def test_a_failed_restoring_exchange_is_reported_to_the_launcher_as_a_failure(
    tmp_path, monkeypatch, capsys
):
    """The test above proves the displaced credential survives; this proves the exit code.

    `RestoreConcurrentCredential` is the worst state this helper reaches: the new
    credential is live, the other writer's credential is in a temporary file, and a human
    has to choose. The test above calls `publish` directly, so it never sees what `main`
    reports. If a later broad `except` or a reordered handler mapped this to 0, the
    launcher would read the refresh as successful and build a chamber against a credential
    state nobody reconciled -- with every other test in this file still green. Hard rule 7
    and GOVERNANCE 2 both forbid a success status over unreconciled evidence.
    """

    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    original_exchange = refresh.exchange_paths
    exchanges = []

    def exchange(first, second):
        exchanges.append((first, second))
        if len(exchanges) == 1:
            concurrent = json.loads(path.read_text(encoding="utf-8"))
            concurrent["vendorWrite"] = "kept"
            path.write_text(json.dumps(concurrent), encoding="utf-8")
            original_exchange(first, second)
        else:
            raise OSError(errno.EIO, "restoring exchange failed")

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps({"access_token": "new-access", "expires_in": 3_600})
        ),
    )
    monkeypatch.setattr(refresh, "exchange_paths", exchange)

    assert refresh.main() == 2, "a two-credential state was reported as a successful refresh"

    error = capsys.readouterr().err
    assert "new-access" not in error, "a token was printed"
    assert "credential changed concurrently" in error
    assert list(path.parent.glob(".credentials.tmp.*")), "the recoverable copy was not named"


def test_dockerfile_pins_both_oauth_constants_from_the_refresh_helper():
    dockerfile = Path(__file__).with_name("Dockerfile").read_text(encoding="utf-8")

    # The constants, not the shell spelling. The build checks them in a loop that names
    # which one moved; pinning `grep -a -q '<value>'` made improving that message a
    # test failure while both constants were still verified.
    assert refresh.CLIENT_ID in dockerfile
    assert refresh.TOKEN_URL in dockerfile
    assert "grep -a -q" in dockerfile, "the build no longer greps the launcher"


def test_the_deadline_is_disarmed_after_response_parsing_before_publication(tmp_path, monkeypatch):
    write_credential(tmp_path, monkeypatch, expires_at=1)
    events = []
    original_publish = refresh.publish

    def record_timer(_which, seconds):
        if seconds == 0:
            events.append("disarm")

    def record_publish(*args):
        events.append("publish")
        return original_publish(*args)

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            json.dumps({"access_token": "new-access", "expires_in": 3_600})
        ),
    )
    monkeypatch.setattr(refresh.signal, "setitimer", record_timer)
    monkeypatch.setattr(refresh, "publish", record_publish)

    assert refresh.main() == 0
    assert events.index("disarm") < events.index("publish")


def test_a_failed_refresh_preserves_the_existing_credential(tmp_path, monkeypatch, capsys):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    before = path.read_bytes()

    def offline(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    assert refresh.main() == 1
    assert path.read_bytes() == before
    assert "OSError" in capsys.readouterr().err


def test_a_refused_refresh_preserves_the_existing_credential(tmp_path, monkeypatch, capsys):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    before = path.read_bytes()

    def refused(request, *, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "refused", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert refresh.main() == 1
    assert path.read_bytes() == before
    error = capsys.readouterr().err
    assert "HTTP 401" in error
    assert "old-refresh" not in error


def test_a_missing_refresh_token_never_reaches_the_endpoint(tmp_path, monkeypatch, capsys):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["claudeAiOauth"]["refreshToken"] = ""
    path.write_text(json.dumps(document), encoding="utf-8")

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("the endpoint was called without a refresh token")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_call)
    assert refresh.main() == 1
    assert "no usable refresh token" in capsys.readouterr().err


def test_an_invalid_access_lifetime_is_not_published(tmp_path, monkeypatch, capsys):
    path = write_credential(tmp_path, monkeypatch, expires_at=1)
    before = path.read_bytes()

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(json.dumps({"access_token": "new-access"})),
    )
    assert refresh.main() == 1
    assert path.read_bytes() == before
    assert "invalid lifetime" in capsys.readouterr().err


def test_the_complete_refresh_has_a_hard_deadline(tmp_path, monkeypatch, capsys):
    write_credential(tmp_path, monkeypatch, expires_at=1)
    monkeypatch.setattr(refresh, "DEADLINE_SECONDS", 0.01)

    def never_returns(*_args, **_kwargs):
        time.sleep(1)

    monkeypatch.setattr(urllib.request, "urlopen", never_returns)
    started = time.monotonic()
    assert refresh.main() == 1
    assert time.monotonic() - started < 0.5
    assert "exceeded" in capsys.readouterr().err
