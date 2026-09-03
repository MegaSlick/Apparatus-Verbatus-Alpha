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
never got. That text is free-form, not a mapping the shared predicate can
walk field by field, so it gets its own pass: any `key=value`-shaped or
`Bearer <token>`-shaped substring that looks credential-shaped is redacted
before the line is written, and the redaction is named in `scrubbed` like
every other field's.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import urllib.parse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol

from .models import looks_like_credential_field, looks_like_credential_value, utc_now

FIXTURE_SCHEMA = "provider-exchange.v1"
SCRUBBED = "SCRUBBED"

# The mark that survives `json.dumps` and is rewritten back into a bare
# number.  NUL cannot appear unescaped in JSON text and `ensure_ascii`
# escapes it, but that alone is not a value a provider body cannot produce:
# a recorded string is decoded with errors="replace" and keeps whatever NUL
# bytes it carried, so a body echoing NUL + `decimal:` + number-shaped
# characters + NUL would be rewritten from a JSON string into a bare number
# -- evidence altered with no record of it (GOVERNANCE 4).  A fresh nonce
# per line closes that: the mark a body would have to wear is chosen after
# the body was read, and is never reused.
_DECIMAL_MARK = "\x00decimal:"
_DECIMAL_END = "\x00"

BodyKind = Literal["absent", "text", "json-text", "json-object"]
"""What shape ``response_body`` actually holds, apart from ``verbatim``.

``verbatim`` alone collapses four cases into one boolean: no response body,
a non-JSON body, a JSON body that needed no scrubbing, and a scrubbed
JSON body -- the first three all read ``verbatim: true`` but a caller has
to know by convention, not by the record's own shape, whether to treat
``response_body`` as ``None``, a string, or a parsed object. A reader
branches on ``body_kind`` instead: ``"absent"`` (``None``), ``"text"`` (a
non-JSON string), ``"json-text"`` (JSON that needed no scrubbing, still a
string so the original bytes are preserved verbatim), or ``"json-object"``
(scrubbed, so ``response_body`` is the cleaned object, never the original
string)."""


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
        # `record_exchanges` wraps both the provider's own transport and its
        # balance observer's around one recorder, and the observer runs on a
        # daemon thread that can still be mid-call when the main thread
        # records a close exchange (its socket timeout matches the join
        # deadline it is abandoned at). Held across the whole method, not
        # just the counter increment, so a record's `sequence` and its
        # position in the file never disagree -- narrowing the lock to the
        # increment alone would make sequence values unique but let two
        # records land in the opposite order from their own numbers.
        self._lock = threading.Lock()

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

        with self._lock:
            self.sequence += 1
            sequence = self.sequence
            scrubbed: list[str] = []
            recorded_path = _scrub_query(path, scrubbed)
            recorded_request = (
                None if request_body is None else _scrub(request_body, "request_body", scrubbed)
            )
            recorded_response, verbatim, body_kind = _scrub_body(response_body, scrubbed)
            recorded_error = _scrub_error(error, scrubbed)
            record: dict[str, object] = {
                "schema": FIXTURE_SCHEMA,
                "sequence": sequence,
                "observed_at": self.now().isoformat(),
                "method": method,
                "path": recorded_path,
                "request_body": recorded_request,
                "status": status,
                "response_body": recorded_response,
                "body_kind": body_kind,
                "verbatim": verbatim,
                "scrubbed": scrubbed,
                "error": recorded_error,
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
    if isinstance(value, str) and _carries_a_credential_shaped_word(value):
        # The name check above asks whether a *key* names itself a secret. A
        # real leaked value carries no such name: a provider answer echoing a
        # key inside a `dockerArgs`, `message` or env-value string would land
        # in the drill fixture verbatim with an empty `scrubbed` list. This is
        # the same shape test `bootstrap_main` applies to argv, so a
        # credential-shaped value is replaced and named whatever innocuous key
        # it sat under. It is deliberately narrow -- 20+ opaque mixed
        # alphanumeric characters, no path or URL punctuation, never a plain
        # hex digest -- so ordinary provider ids, digests and paths still
        # record verbatim and the fixture stays replayable.
        scrubbed.append(where)
        return SCRUBBED
    return value


def _carries_a_credential_shaped_word(value: str) -> bool:
    """The shared shape test, applied to the leaf and to each word inside it.

    A provider answer rarely hands back a bare key: it hands back
    ``"started with sk-..."`` or a `dockerArgs` line with one in it. Testing
    only the whole leaf would miss exactly the case this exists for, so the
    leaf is split on whitespace and each word stripped of the quoting and
    grouping marks a value picks up at its edges -- the same reading
    `notify_hooks` applies to a notification line.
    """

    if looks_like_credential_value(value):
        return True
    return any(
        stripped and looks_like_credential_value(stripped)
        for stripped in (word.strip("\"'(),;:") for word in value.split())
    )


_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+\S+")
_KEY_VALUE_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_.\-]*)=([^&\s'\")]+)")


def _scrub_error(error: str | None, scrubbed: list[str]) -> str | None:
    """Redact credential-shaped substrings from a free-text error message.

    Every other field a record carries is scrubbed through the shared
    predicate on a parsed structure; `error` is free text taken from a
    caught exception's own message, which the predicate cannot walk as a
    mapping. Nothing reaches here credential-shaped today -- the adapters
    that raise into this recorder name only `reason`, never a URL or a
    header, and that is pinned by their own tests -- so this is
    belt-and-braces containment at the recorder itself, not a fix for a
    reproduced leak.
    """

    if error is None:
        return None

    def redact_pair(match: re.Match[str]) -> str:
        key = match.group(1)
        if looks_like_credential_field(key):
            scrubbed.append(f"error.{key}")
            return f"{key}={SCRUBBED}"
        return match.group(0)

    redacted = _KEY_VALUE_PATTERN.sub(redact_pair, error)

    def redact_bearer(_match: re.Match[str]) -> str:
        scrubbed.append("error.bearer")
        return "Bearer SCRUBBED"

    return _BEARER_PATTERN.sub(redact_bearer, redacted)


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


def _scrub_body(body: bytes | None, scrubbed: list[str]) -> tuple[object, bool, BodyKind]:
    if body is None:
        return None, True, "absent"
    text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError:
        return text, True, "text"
    before = len(scrubbed)
    cleaned = _scrub(parsed, "response_body", scrubbed)
    if len(scrubbed) == before:
        return text, True, "json-text"
    return cleaned, False, "json-object"


def _dumps(value: object) -> str:
    """JSON with ``Decimal`` written as the number it was, not a float or a string.

    The mark carries a nonce chosen for this line alone, so no recorded string
    -- hostile or merely odd -- can wear the mark and be rewritten into a bare
    number on its way to disk.  A non-finite ``Decimal`` is refused rather than
    marked: ``NaN`` and ``Infinity`` are not JSON numbers, and a money value
    that is one of them is a named failure here, never a fixture line the
    replay side reads back as a leftover marker string.
    """

    nonce = secrets.token_hex(8)
    prefix = f"{_DECIMAL_MARK}{nonce}:"

    def mark(item: object) -> object:
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError(f"fixture record cannot serialize the non-finite Decimal {item}")
            return f"{prefix}{item}{_DECIMAL_END}"
        raise TypeError(f"fixture record cannot serialize {type(item).__name__}")

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=mark)
    pattern = re.compile('"' + _escaped_mark(nonce) + r"([-+0-9.Ee]+)" + _ESCAPED_NUL + '"')
    return pattern.sub(r"\1", text)


# What `json.dumps(..., ensure_ascii=True)` writes a NUL byte as, and therefore
# what the mark looks like in the serialized text the pattern below rewrites.
_ESCAPED_NUL = "\\\\u0000"


def _escaped_mark(nonce: str) -> str:
    return _ESCAPED_NUL + "decimal:" + re.escape(nonce) + ":"
