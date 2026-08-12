from __future__ import annotations

import io
import json
import stat
import time
import urllib.error
import urllib.request

from operations.autoclave import refresh_claude_token as refresh


def write_credential(tmp_path, monkeypatch, *, expires_at: int):
    config = tmp_path / ".claude"
    config.mkdir()
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
    real_replace = refresh.os.replace

    def observe_replace(source, destination):
        observed["replace"] = (source, destination)
        # Until the single publication boundary, readers still see the complete old
        # credential and the temporary holds the complete new one.
        assert json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == (
            "old-access"
        )
        assert (
            json.loads(open(source, encoding="utf-8").read())["claudeAiOauth"]["accessToken"]
            == "new-access"
        )
        real_replace(source, destination)

    class Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def endpoint(request, *, timeout):
        observed["timeout"] = timeout
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
    monkeypatch.setattr(refresh.os, "replace", observe_replace)
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
    assert observed["timeout"] == 45
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert observed["replace"][1] == path
    output = capsys.readouterr().out
    assert "new-access" not in output and "new-refresh" not in output
    assert list(path.parent.glob(".credentials.tmp.*")) == []


def test_an_unrotated_refresh_token_is_preserved_and_expiries_are_updated(tmp_path, monkeypatch):
    now_seconds = 1_700_000_000
    path = write_credential(tmp_path, monkeypatch, expires_at=1)

    class Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

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


def test_the_deadline_is_disarmed_after_response_parsing_before_publication(tmp_path, monkeypatch):
    write_credential(tmp_path, monkeypatch, expires_at=1)
    events = []
    original_publish = refresh.publish

    class Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

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

    class Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

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
