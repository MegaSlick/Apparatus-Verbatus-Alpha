"""Verbatim provider exchanges, secrets scrubbed, as a replayable drill fixture.

`operations/pod/README.md`'s boot plan asks one thing of the first authorized
boot beyond its measurements: that it leave behind the provider's *actual*
answers, so the next offline suite can replay them instead of the documented
shapes every test in this package is built from today (deferral 04-6). This
module is that recorder. It knows no vendor: it sits between an adapter and
its HTTP transport, sees the method, the path, the request body, the status
and the response body, and appends each as one JSON line to the evidence file
`--record-fixture` names.

**What "verbatim" means here, stated exactly.** A response body that carries
nothing credential-shaped is stored as the bytes the provider sent, decoded as
UTF-8 with replacement. A body that does carry a credential-shaped key -- by
`models.looks_like_credential_field`, the one shared definition -- is parsed,
the offending values replaced with `SCRUBBED`, and re-serialized; the record
then names every scrubbed path and says `verbatim: false`, so a reader never
mistakes a scrubbed body for the provider's own bytes. Money survives the
round trip as numbers: JSON floats are parsed as `Decimal` and written back
as the same digits, never through a binary float.

**The launch token is scrubbed too.** `VERBATUS_LAUNCH_TOKEN` is not a
capability, and `models.PodCreateRequest` exempts it from the credential
scan of pod metadata for that reason. This recorder does not: the brief for
the fixture is "secrets scrubbed by the predicate", and one predicate with no
exemptions is a rule a reader can check without reading this file. Replaying
the exact-token recovery path from a fixture therefore needs the token
re-substituted from the lease, which the record's `scrubbed` list makes
possible.

A raised transport failure is recorded too, as a line with `error` set and
no status: "every provider response the launch sees" includes the ones it
never got.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .models import looks_like_credential_field, utc_now

FIXTURE_SCHEMA = "provider-exchange.v1"
SCRUBBED = "SCRUBBED"

# NUL cannot appear unescaped in JSON text, and ``ensure_ascii`` writes it as
# ``\u0000``, so a marked Decimal is a string no provider body can collide with.
_DECIMAL_MARK = "\x00decimal:"
_DECIMAL_END = "\x00"
_DECIMAL_PATTERN = re.compile(r'"\\u0000decimal:([-+0-9.Ee]+)\\u0000"')


class _Response(Protocol):
    status: int
    body: bytes


class FixtureRecorder:
    """Append-only JSON-lines evidence of every exchange, 0600, fsynced per line."""

    def __init__(self, path: str | Path, *, now: Callable[[], datetime] = utc_now) -> None:
        self.path = Path(path)
        self.now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Append, never truncate: a second launch that names the same file adds
        # to the evidence rather than erasing the first's (GOVERNANCE 4).
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._handle = os.fdopen(descriptor, "ab")
        self.sequence = 0

    def record(
        self,
        method: str,
        path: str,
        request_body: Mapping[str, object] | None,
        *,
        status: int | None,
        response_body: bytes | None,
        error: str | None = None,
    ) -> dict[str, object]:
        """Write one exchange and return the record that was written."""

        self.sequence += 1
        scrubbed: list[str] = []
        recorded_path = _scrub_query(path, scrubbed)
        recorded_request = (
            None if request_body is None else _scrub(request_body, "request_body", scrubbed)
        )
        recorded_response, verbatim = _scrub_body(response_body, scrubbed)
        record: dict[str, object] = {
            "schema": FIXTURE_SCHEMA,
            "sequence": self.sequence,
            "observed_at": self.now().isoformat(),
            "method": method,
            "path": recorded_path,
            "request_body": recorded_request,
            "status": status,
            "response_body": recorded_response,
            "verbatim": verbatim,
            "scrubbed": scrubbed,
            "error": error,
        }
        line = _dumps(record) + "\n"
        self._handle.write(line.encode("utf-8"))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return record

    def close(self) -> None:
        self._handle.close()


class RecordingTransport:
    """Wrap any ``request(method, path, body)`` transport and record what it saw."""

    def __init__(self, inner: object, recorder: FixtureRecorder) -> None:
        if not callable(getattr(inner, "request", None)):
            raise TypeError("a recording transport wraps an object with a request method")
        self.inner = inner
        self.recorder = recorder

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None):  # type: ignore[no-untyped-def]
        try:
            response: _Response = self.inner.request(method, path, body)  # type: ignore[attr-defined]
        except Exception as error:
            self.recorder.record(
                method, path, body, status=None, response_body=None, error=repr(error)
            )
            raise
        self.recorder.record(
            method, path, body, status=int(response.status), response_body=bytes(response.body)
        )
        return response


def read_fixture(path: str | Path) -> list[dict[str, object]]:
    """Read every record back, money as ``Decimal``; the replay side of the seam."""

    records: list[dict[str, object]] = []
    with Path(path).open("rb") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            record = json.loads(line, parse_float=Decimal)
            if not isinstance(record, dict) or record.get("schema") != FIXTURE_SCHEMA:
                raise ValueError(f"{path} is not a {FIXTURE_SCHEMA} fixture")
            records.append(record)
    return records


def _scrub(value: object, where: str, scrubbed: list[str]) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if looks_like_credential_field(name):
                scrubbed.append(f"{where}.{name}")
                result[name] = SCRUBBED
            else:
                result[name] = _scrub(item, f"{where}.{name}", scrubbed)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub(item, f"{where}[{index}]", scrubbed) for index, item in enumerate(value)]
    return value


def _scrub_query(path: str, scrubbed: list[str]) -> str:
    """Replace credential-shaped query values; a key in a URL is still a key."""

    if "?" not in path:
        return path
    base, query = path.split("?", 1)
    parts: list[str] = []
    for index, (key, value) in enumerate(urllib.parse.parse_qsl(query, keep_blank_values=True)):
        if looks_like_credential_field(key):
            scrubbed.append(f"path.query[{index}].{key}")
            value = SCRUBBED
        parts.append(urllib.parse.urlencode({key: value}))
    return f"{base}?{'&'.join(parts)}"


def _scrub_body(body: bytes | None, scrubbed: list[str]) -> tuple[object, bool]:
    if body is None:
        return None, True
    text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError:
        return text, True
    before = len(scrubbed)
    cleaned = _scrub(parsed, "response_body", scrubbed)
    if len(scrubbed) == before:
        return text, True
    return cleaned, False


def _dumps(value: object) -> str:
    """JSON with ``Decimal`` written as the number it was, not a float or a string."""

    def mark(item: object) -> object:
        if isinstance(item, Decimal):
            return f"{_DECIMAL_MARK}{item}{_DECIMAL_END}"
        raise TypeError(f"fixture record cannot serialize {type(item).__name__}")

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=mark)
    return _DECIMAL_PATTERN.sub(r"\1", text)
