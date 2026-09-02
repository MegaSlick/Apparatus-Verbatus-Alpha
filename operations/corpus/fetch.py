"""The fetcher: one polite `urllib.request` client over a `recordgold-fetch-plan.v1`.

`SPEC.md` §5.1, per identifier: `info.json` once, then the full-resolution image
(`full/full` first, `max` on 400/501, size used recorded per page), decoded
dimensions verified against `info.json`, EXIF orientation refused if present and
not `1`, every record's region verified inside the page, and the response bytes
landed content-addressed via `cache.py` so a page already answered is never asked
again.

**Politeness is one object, not scattered state**: `FetchSession` owns the
opener, the rate limiter, and the running request count, so every call this
module makes to the network goes through `FetchSession._request`, which is the
single place the delay, the retry, the 403 stop, and the per-run ceiling are
enforced. Nothing downstream can accidentally bypass it by calling `urlopen`
directly — there is no module-level opener to reach for.

**Two kinds of failure, two exception families.** `CorpusRefusal` (this
package's base) is a per-page outcome: `fetch_page` catches it, writes a
"refused" log entry by name, and moves on to the next page — rule 7, nothing
lost silently, but nothing stops the run either. `FetchHalt` and its two
subclasses (`Http403Stop`, `RequestCeilingReached`) are run-level: they escape
`fetch_page` and `run_fetch` on purpose, because "stop on first 403" and "bounded
per-run request ceiling" mean the *run*, not the page.
"""

import argparse
import email.utils
import http.client
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash

from . import CorpusRefusal
from . import cache as cache_module
from . import plan as plan_module
from .cache import CacheUnusable
from .holdout import HELD_SPLIT, load_holdout, refuse_held_out_page, validate_holdout

# `SPEC.md` §5.1's closed refusal vocabulary, verbatim. A caller that wants to
# dispatch on the reason reads `str(error).split(":", 1)[0]`, same convention as
# every other refusal in this package (see `__init__.py`).
FETCH_REFUSAL_REASONS = frozenset(
    {
        "http-error",
        "non-image-body",
        "dimension-mismatch",
        "exif-orientation",
        "region-outside-page",
        "duplicate-page-bytes",
        "unexpected-host",
        "unsupported-size-parameter",
        "holdout-page",
        "cross-split-page",
    }
)

# Refusals that stop a *run* before it fetches a single page — a misconfigured
# `run_fetch`/`main` call, not a per-page outcome. These never reach a
# fetch-log entry (both raise sites below run before `FetchSession` exists),
# so they are declared in their own closed set rather than widening
# `FETCH_REFUSAL_REASONS`, which is `SPEC.md` §5.1's per-page log vocabulary,
# verbatim.
FETCH_RUN_REFUSAL_REASONS = frozenset(
    {
        "holdout-ledger-required",
    }
)

# Declares the project and a contact, per `SPEC.md` §5.1's politeness list. A
# real run against a real server should pass a config carrying an operator's own
# contact; this default names the project so an operator forgetting to override
# it still identifies the traffic honestly.
DEFAULT_USER_AGENT = (
    "ApparatusVerbatus-RecordGold-Fetcher/1.0 "
    "(+https://github.com/MegaSlick/Apparatus-Verbatus-Alpha; research use, non-commercial)"
)

# `pipeline/1_exemplar/image_formats.py` caps a source at 64 MiB; the fetcher
# refuses a body larger than that itself rather than accepting bytes the Door
# would only refuse two stages later.
MAX_BODY_BYTES = 64 * 1024 * 1024

_EXIF_ORIENTATION_TAG = 0x0112

_SIZE_ORDER = ("full", "max")
_FALLBACK_STATUSES = frozenset({400, 501})

# The closed shapes this module itself writes to `cache/requests/<key>.json`
# (image request records) and to the retained `info.json` (`info_root`). A
# request record loaded from disk is checked against the shape its own
# `status` claims before any field is read by name — a record damaged outside
# this module's own writes (a partial cache-root copy, a restored backup, an
# operator editing rather than deleting a record) must be refused by name, not
# read as a raw `KeyError`. `cache.load_request_record` already catches an
# unreadable *file*; these catch a readable file with the wrong *fields*.
_IMAGE_FETCHED_RECORD_FIELDS = frozenset(
    {
        "kind",
        "identifier",
        "size_parameter",
        "status",
        "response_sha256",
        "http_status",
        "bytes",
        "fetched_at_utc",
    }
)
_IMAGE_UNSUPPORTED_RECORD_FIELDS = frozenset(
    {"kind", "identifier", "size_parameter", "status", "http_status", "fetched_at_utc"}
)
_RETAINED_INFO_FIELDS = frozenset({"declared_width", "declared_height", "raw"})


def _require_closed_record(
    record: Any, fields: frozenset[str], *, identifier: str, key: str
) -> None:
    if not isinstance(record, dict) or set(record) != fields:
        raise CorpusRefusal(
            f"http-error: stale request record for {identifier!r} — "
            f"cache/requests/{key}.json was written by a different version of this cache "
            "or is damaged; delete it to force a re-fetch"
        )


class FetchHalt(Exception):
    """A run-level stop. Escapes `fetch_page`; the caller must not continue the run."""


class Http403Stop(FetchHalt):
    """The server returned 403. `SPEC.md` §5.1: stop the whole run on first 403."""


class RequestCeilingReached(FetchHalt):
    """The per-run request ceiling was reached before this request was issued."""


class _HttpStatusError(Exception):
    """Internal: a non-retryable, non-403 HTTP status, carrying the code for the caller."""

    def __init__(self, status: int, url: str):
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} fetching {url!r}")


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses a redirect whose target host differs from the request's own host.

    `SPEC.md` §5.1: "no cross-host redirects." Raising here — rather than letting
    the handler silently follow — is what makes this a refusal instead of a quiet
    hop to a server this fetcher never declared it would talk to.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 (stdlib override)
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise CorpusRefusal(
                f"unexpected-host: refused a redirect from {req.full_url!r} to {newurl!r} "
                "— cross-host redirects are not followed"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener() -> urllib.request.OpenerDirector:
    """The one explicit opener this module uses — never `urllib.request.urlopen`."""
    return urllib.request.build_opener(_NoCrossHostRedirect)


class RateLimiter:
    """Enforces a minimum interval between the starts of two requests."""

    def __init__(self, min_interval_seconds: float, monotonic: Callable[[], float]):
        self._min_interval = min_interval_seconds
        self._monotonic = monotonic
        self._last: float | None = None

    def wait(self, sleep: Callable[[float], None]) -> None:
        now = self._monotonic()
        if self._last is not None:
            remaining = self._min_interval - (now - self._last)
            if remaining > 0:
                sleep(remaining)
        self._last = self._monotonic()


def _retry_delay(retry_after_header: str | None, attempt: int, max_backoff_seconds: float) -> float:
    """`Retry-After`, either the seconds form or the HTTP-date form (RFC 7231), else backoff."""
    if retry_after_header:
        try:
            seconds = float(retry_after_header)
        except ValueError:
            try:
                target = email.utils.parsedate_to_datetime(retry_after_header)
            except (TypeError, ValueError):
                target = None
            if target is not None:
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                remaining = (target - datetime.now(UTC)).total_seconds()
                return min(max(0.0, remaining), max_backoff_seconds)
        else:
            if seconds >= 0:
                return min(seconds, max_backoff_seconds)
    return min(float(2**attempt), max_backoff_seconds)


def _read_bounded(response: Any, max_bytes: int, expected_length: int | None) -> bytes:
    """Read a response body in chunks, bounded above and verified below.

    Refuses a body over `max_bytes` rather than keep reading it, and — when the
    server declared a `Content-Length` — refuses a body whose length disagrees
    with it in either direction. Short: a server or connection that closes
    mid-transfer must not be mistaken for one that finished, or a truncated
    JPEG would be cached and recorded as a completed fetch (`SPEC.md` §5.1's
    "an interrupt loses at most one in-flight body" requires the interrupted
    one to leave no record at all). Long: a proxy or misbehaving origin that
    concatenates bytes past the declared length must not have the overrun
    silently folded into the cached body — a decoder that tolerates trailing
    bytes would hash and store the wrong page without complaint.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise CorpusRefusal(
                f"http-error: response body exceeded the {max_bytes}-byte cap; refused "
                "rather than kept reading"
            )
        chunks.append(chunk)
    if expected_length is not None and total != expected_length:
        raise CorpusRefusal(
            f"http-error: response body length disagrees with Content-Length — received "
            f"{total} of {expected_length} declared bytes"
        )
    return b"".join(chunks)


@dataclass
class FetchConfig:
    """Every knob `SPEC.md` §5.1's politeness list names, in one place."""

    cache_root: Path
    info_root: Path
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 30.0
    min_interval_seconds: float = 1.0
    max_retries: int = 5
    max_backoff_seconds: float = 60.0
    max_body_bytes: int = MAX_BODY_BYTES
    max_requests_per_run: int | None = None
    opener_factory: Callable[[], urllib.request.OpenerDirector] = build_opener
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    clock: Callable[[], str] = _default_clock


class FetchSession:
    """One run's opener, rate limiter, request count, and duplicate-bytes memory."""

    def __init__(self, config: FetchConfig):
        self.config = config
        self.opener = config.opener_factory()
        self.rate_limiter = RateLimiter(config.min_interval_seconds, config.monotonic)
        self.request_count = 0
        self.seen_response_digests: dict[str, str] = _known_response_digests(config.cache_root)

    def _request(self, url: str) -> tuple[bytes, int]:
        """One polite GET: rate-limited, retried on 429/503, halted on 403 or ceiling."""
        config = self.config
        attempt = 0
        while True:
            if (
                config.max_requests_per_run is not None
                and self.request_count >= config.max_requests_per_run
            ):
                raise RequestCeilingReached(
                    f"per-run request ceiling {config.max_requests_per_run} reached before {url!r}"
                )
            self.rate_limiter.wait(config.sleep)
            self.request_count += 1
            request = urllib.request.Request(url, headers={"User-Agent": config.user_agent})
            try:
                with self.opener.open(request, timeout=config.timeout_seconds) as response:
                    length_header = (
                        response.headers.get("Content-Length") if response.headers else None
                    )
                    expected_length = (
                        int(length_header) if length_header and length_header.isdigit() else None
                    )
                    body = _read_bounded(response, config.max_body_bytes, expected_length)
                    return body, response.status
            except urllib.error.HTTPError as error:
                status = error.code
                error.read()  # drain so the connection can be reused/closed cleanly
                if status == 403:
                    raise Http403Stop(f"stopped on first 403 fetching {url!r}") from error
                if status in (429, 503) and attempt < config.max_retries:
                    delay = _retry_delay(
                        error.headers.get("Retry-After") if error.headers else None,
                        attempt,
                        config.max_backoff_seconds,
                    )
                    config.sleep(delay)
                    attempt += 1
                    continue
                raise _HttpStatusError(status, url) from error
            except (http.client.IncompleteRead, ConnectionError, TimeoutError, OSError) as error:
                raise CorpusRefusal(
                    f"http-error: transport failure fetching {url!r}: {error}"
                ) from error


def _known_response_digests(cache_root: Path) -> dict[str, str]:
    """Every response digest already claimed by an earlier, *verified* image fetch.

    Maps digest -> the identifier that first claimed it, not just a set: a page
    re-fetched from cache on a later run must not be flagged as a duplicate of
    *itself*, only of some *other* identifier landing the same bytes. Makes
    `duplicate-page-bytes` detection survive across runs, not just within one
    process.

    Reads `cache/owners/`, not `cache/requests/`: a request record is written as
    soon as the bytes are down, before `fetch_page` has decoded them, checked
    dimensions, EXIF, or regions — reading it back here would let which of two
    duplicate pages is "first" flip between runs depending on filesystem glob
    order (`sorted(requests_dir.glob(...))`), independent of fetch order. An
    owner record is written only after a page passes every check, so the
    identifier on disk is the one that actually earned the claim, permanently.
    """
    owners_dir = Path(cache_root) / "owners"
    digests: dict[str, str] = {}
    if not owners_dir.exists():
        return digests
    for path in owners_dir.glob("*.json"):
        record = json.loads(path.read_bytes())
        digests[path.stem] = record["identifier"]
    return digests


def _decode_jpeg(body: bytes):
    """Decode `body` as JPEG, refusing anything else. Import deferred: heavy, optional at import time."""
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(body))
        image.load()
    except Exception as error:
        raise CorpusRefusal(f"non-image-body: failed to decode as an image: {error}") from error
    if image.format != "JPEG":
        raise CorpusRefusal(f"non-image-body: decoded format {image.format!r}, expected JPEG")
    return image


def _check_dimensions(image: Any, declared_width: int, declared_height: int) -> None:
    width, height = image.size
    if width != declared_width or height != declared_height:
        raise CorpusRefusal(
            f"dimension-mismatch: decoded image is {width}x{height}, info.json declared "
            f"{declared_width}x{declared_height} — the region boxes are in a frame that "
            "does not match these pixels"
        )


def _check_exif_orientation(image: Any) -> None:
    exif = image.getexif()
    orientation = exif.get(_EXIF_ORIENTATION_TAG)
    if orientation is not None and orientation != 1:
        raise CorpusRefusal(
            f"exif-orientation: decoded image declares EXIF orientation {orientation}, "
            "only absent or 1 is accepted — a display-rotation tag would put the region "
            "boxes in a different frame from the stored pixels"
        )


def _check_regions(records: list[dict[str, Any]], width: int, height: int) -> None:
    for record in records:
        region = record["region"]
        x, y, w, h = region["x"], region["y"], region["w"], region["h"]
        if x + w > width or y + h > height:
            raise CorpusRefusal(
                f"region-outside-page: record {record['record_id']!r} region "
                f"x={x} y={y} w={w} h={h} exceeds the page's {width}x{height}"
            )


def _info_request_key(identifier: str) -> str:
    return cache_module.compute_request_key(
        kind="info",
        identifier=identifier,
        region=cache_module.INFO_SENTINEL,
        size=cache_module.INFO_SENTINEL,
        rotation=cache_module.INFO_SENTINEL,
        quality=cache_module.INFO_SENTINEL,
        format="json",
    )


def _image_request_key(identifier: str, size_parameter: str) -> str:
    return cache_module.compute_request_key(
        kind="image",
        identifier=identifier,
        region="full",
        size=size_parameter,
        rotation="0",
        quality="default",
        format="jpg",
    )


def _fetch_info(session: FetchSession, page: dict[str, Any]) -> dict[str, Any]:
    """`info.json`, once per identifier, retained under `info_root` and never re-asked.

    A retained copy is never overwritten (`write_new_file` never overwrites);
    if a second fetch's bytes disagree with what is already retained, the page
    is refused by name rather than the disagreement being resolved silently in
    either direction — this is not "keep the old answer."
    """
    identifier = page["identifier"]
    key = _info_request_key(identifier)
    record = cache_module.load_request_record(session.config.cache_root, key)
    if record is not None:
        info_path = _info_path(session.config.info_root, identifier)
        if not info_path.exists():
            raise CorpusRefusal(
                f"http-error: retained info.json missing for {identifier!r} — its request "
                f"record ({key}) says this was already fetched but the retained copy is gone; "
                "delete that request record to force a re-fetch"
            )
        try:
            retained = json.loads(info_path.read_bytes())
        except ValueError as error:
            raise CorpusRefusal(
                f"http-error: retained info.json for {identifier!r} at {info_path} is not "
                f"readable JSON ({error}); delete that request record to force a re-fetch"
            ) from error
        if not isinstance(retained, dict) or set(retained) != _RETAINED_INFO_FIELDS:
            raise CorpusRefusal(
                f"http-error: retained info.json for {identifier!r} at {info_path} is not "
                f"the closed record {sorted(_RETAINED_INFO_FIELDS)}; delete that request "
                "record to force a re-fetch"
            )
        return retained

    body, status = session._request(page["info_url"])
    try:
        info = json.loads(body)
        width = info["width"]
        height = info["height"]
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise ValueError(f"width/height must be positive integers, got {width!r}/{height!r}")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
        raise CorpusRefusal(
            f"http-error: unusable info.json body from {page['info_url']!r}: {error}"
        ) from error

    declared = {"declared_width": width, "declared_height": height}
    info_path = _info_path(session.config.info_root, identifier)
    body_to_retain = canonical_bytes({**declared, "raw": info})
    if not cache_module.write_new_file(info_path, body_to_retain) and (
        info_path.read_bytes() != body_to_retain
    ):
        raise CorpusRefusal(
            f"http-error: retained info.json for {identifier!r} at {info_path} disagrees "
            "with the copy the server just returned; nothing was overwritten and no "
            "request record was written — delete that file and re-run so the page is "
            "checked against the dimensions actually fetched"
        )
    cache_module.write_request_record(
        session.config.cache_root,
        key,
        {
            "kind": "info",
            "identifier": identifier,
            "http_status": status,
            "bytes": len(body),
            "fetched_at_utc": session.config.clock(),
        },
    )
    return declared


def _info_path(info_root: Path, identifier: str) -> Path:
    return Path(info_root) / f"{digest_bytes(identifier.encode('utf-8'))}.json"


def _fetch_image_bytes(
    session: FetchSession, page: dict[str, Any]
) -> tuple[bytes, str, int, int, str]:
    """Full-resolution image: try `full`, fall back to `max` on 400/501.

    Returns `(body, size_parameter_used, http_status, byte_count, fetched_at_utc)`.
    The last three are always the facts of the completed fetch that actually
    talked to the server: on a cache hit they come back from that request's own
    recorded `request_record`, never re-measured "now" — a page answered from
    cache on this run still carries the status and timestamp of the run that
    earned it, which is what U3's `FetchedPage` (`submission.py`) needs to build
    an honest sidecar `iiif` block regardless of which run fetched the bytes.
    """
    identifier = page["identifier"]
    candidates = page["image_url_candidates"]
    last_error: Exception | None = None
    for size_parameter in _SIZE_ORDER:
        key = _image_request_key(identifier, size_parameter)
        record = cache_module.load_request_record(session.config.cache_root, key)
        if record is not None:
            status_value = record.get("status") if isinstance(record, dict) else None
            if status_value == "fetched":
                _require_closed_record(
                    record, _IMAGE_FETCHED_RECORD_FIELDS, identifier=identifier, key=key
                )
                body_path = cache_module.body_path(
                    session.config.cache_root, record["response_sha256"]
                )
                if not body_path.exists():
                    raise CorpusRefusal(
                        f"http-error: cached body missing for {identifier!r} — request "
                        f"record ({key}) says this size was already fetched but {body_path} "
                        f"is gone; delete cache/requests/{key}.json to force a re-fetch"
                    )
                return (
                    body_path.read_bytes(),
                    size_parameter,
                    record["http_status"],
                    record["bytes"],
                    record["fetched_at_utc"],
                )
            if status_value == "unsupported":
                _require_closed_record(
                    record, _IMAGE_UNSUPPORTED_RECORD_FIELDS, identifier=identifier, key=key
                )
                # A previously recorded fallback (e.g. "full" recorded as
                # unsupported) means never ask that size again either — move
                # straight to the next.
                last_error = _HttpStatusError(
                    record.get("http_status", 0), candidates[size_parameter]
                )
                continue
            raise CorpusRefusal(
                f"http-error: stale request record for {identifier!r} — "
                f"cache/requests/{key}.json was written by a different version of this "
                "cache or is damaged; delete it to force a re-fetch"
            )
        try:
            body, status = session._request(candidates[size_parameter])
        except _HttpStatusError as error:
            if error.status in _FALLBACK_STATUSES:
                # A 400/501 here means only "this size parameter is unsupported" —
                # for `full` that is expected fallback behaviour, for `max` it means
                # the server accepted neither, which the loop's trailing raise below
                # names as `unsupported-size-parameter` rather than a bare http-error.
                cache_module.write_request_record(
                    session.config.cache_root,
                    key,
                    {
                        "kind": "image",
                        "identifier": identifier,
                        "size_parameter": size_parameter,
                        "status": "unsupported",
                        "http_status": error.status,
                        "fetched_at_utc": session.config.clock(),
                    },
                )
                last_error = error
                continue
            raise CorpusRefusal(f"http-error: {error}") from error
        response_sha256 = cache_module.store_response_body(session.config.cache_root, body)
        fetched_at_utc = session.config.clock()
        cache_module.write_request_record(
            session.config.cache_root,
            key,
            {
                "kind": "image",
                "identifier": identifier,
                "size_parameter": size_parameter,
                "status": "fetched",
                "response_sha256": response_sha256,
                "http_status": status,
                "bytes": len(body),
                "fetched_at_utc": fetched_at_utc,
            },
        )
        return body, size_parameter, status, len(body), fetched_at_utc
    if isinstance(last_error, _HttpStatusError) and last_error.status in _FALLBACK_STATUSES:
        raise CorpusRefusal(
            f"unsupported-size-parameter: server accepted neither 'full' nor 'max' for "
            f"{identifier!r} ({last_error})"
        )
    raise CorpusRefusal(
        f"http-error: both 'full' and 'max' size requests failed for {identifier!r} ({last_error})"
    )


def fetch_page(
    session: FetchSession, page: dict[str, Any], holdout: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Fetch and verify one fetch-plan page end to end.

    Returns a log entry — `{"status": "fetched", ...}` or `{"status": "refused",
    "reason": ..., "detail": ...}` — for every ordinary outcome (rule 7: nothing
    lost silently, every refusal named). `FetchHalt` (403, request ceiling) is the
    one thing this function does not catch: those are run-level and must stop
    `run_fetch`, not be logged as one more page's refusal.
    """
    identifier = page["identifier"]
    entry: dict[str, Any] = {
        "identifier": identifier,
        "physical_page_id": page["physical_page_id"],
    }
    try:
        if holdout is not None:
            refuse_held_out_page(holdout, identifier, page["splits_present"])

        info = _fetch_info(session, page)
        width, height = info["declared_width"], info["declared_height"]

        body, size_used, http_status, byte_count, fetched_at_utc = _fetch_image_bytes(session, page)
        response_sha256 = digest_bytes(body)

        owner = session.seen_response_digests.get(response_sha256)
        if owner is not None and owner != identifier:
            raise CorpusRefusal(
                f"duplicate-page-bytes: identifier {identifier!r} produced response digest "
                f"{response_sha256!r}, already claimed by identifier {owner!r}"
            )

        image_key = _image_request_key(identifier, size_used)
        try:
            image = _decode_jpeg(body)
            _check_dimensions(image, width, height)
            _check_exif_orientation(image)
            _check_regions(page["records"], width, height)
            decoded_width, decoded_height = image.size
        except CorpusRefusal as error:
            # The request record for `image_key` was already written as "fetched"
            # once the body landed (`_fetch_image_bytes`), before any of these
            # checks ran, so a bad answer would otherwise be cached forever with
            # no way back short of hand-editing the cache. Name the recovery path.
            raise CorpusRefusal(
                f"{error} — cached under request key {image_key!r}; delete "
                f"cache/requests/{image_key}.json to force a re-fetch on the next run"
            ) from error

        # Claimed only now, after every check has passed: an owner record written
        # from the raw request record (before verification) would let which of two
        # duplicate pages "wins" flip between runs depending on filesystem order.
        cache_module.write_new_file(
            cache_module.owner_path(session.config.cache_root, response_sha256),
            canonical_bytes({"identifier": identifier}),
        )
        session.seen_response_digests.setdefault(response_sha256, identifier)
        entry.update(
            {
                "status": "fetched",
                # U3's `FetchedPage` (`submission.py`) needs a page's IIIF facts and
                # this module is the only place that knows which candidate URL was
                # actually used — carried here rather than re-derived downstream so
                # `fetched_pages_from_log` never needs the fetch plan back.
                "info_url": page["info_url"],
                "image_url": page["image_url_candidates"][size_used],
                "size_parameter_used": size_used,
                "response_sha256": response_sha256,
                "bytes": byte_count,
                "http_status": http_status,
                "fetched_at_utc": fetched_at_utc,
                "declared_width": width,
                "declared_height": height,
                # Decoded, not merely declared: `_check_dimensions` already refused
                # any page where these would differ, but the log entry carries both
                # explicitly so a reader downstream never has to trust that a
                # decode-time check ran rather than re-deriving the same fact.
                "width": decoded_width,
                "height": decoded_height,
            }
        )
        return entry
    except CacheUnusable:
        # A cache-root filesystem constraint (e.g. no hard-link support) is not
        # one page's problem — let it escape and halt the run rather than
        # relabel it `http-error` and log it as a per-page refusal, which would
        # repeat identically on every remaining page.
        raise
    except CorpusRefusal as error:
        detail = str(error)
        reason = detail.split(":", 1)[0]
        if reason not in FETCH_REFUSAL_REASONS:
            # `cache.py` and other collaborators raise their own, differently
            # closed refusal vocabularies (e.g. `malformed-digest`); a log entry's
            # `reason` must stay inside this module's own closed set, so anything
            # else is relabelled — the original text survives in `detail`.
            reason = "http-error"
        entry.update({"status": "refused", "reason": reason, "detail": detail})
        return entry


@dataclass
class RunResult:
    entries: list[dict[str, Any]] = field(default_factory=list)
    halted: str | None = None
    """The reason `run_fetch` stopped early (a `FetchHalt` subclass name), or None."""


def run_fetch(
    plan: dict[str, Any],
    config: FetchConfig,
    *,
    split: str = "val",
    holdout: dict[str, Any] | None = None,
    enforce_holdout: bool = True,
) -> RunResult:
    """Fetch every plan page carrying `split`, sequentially, halting the run on `FetchHalt`.

    `SPEC.md` §5.4: the fetcher defaults to `val`; a caller that wants `test`
    passes `split="test"` explicitly (and, per §5.4, should also pass
    `enforce_holdout=False` — deliberately fetching the held split is not the
    same mistake as a `val` build accidentally including a held page).

    Rule 6, nothing enters uninspected: `plan` and `holdout` are revalidated here
    against their own `self_hash`, not trusted merely because a caller already
    ran them through `plan.load_plan`/`holdout.load_holdout` once — a tampered
    plan (a swapped host in `image_url_candidates`, a dropped page) fails its own
    self-hash check and is refused before a single request is made, rather than
    fetched from silently.
    """
    plan = plan_module.validate_plan(plan)
    if holdout is not None:
        holdout = validate_holdout(holdout)
    if enforce_holdout and holdout is None:
        raise CorpusRefusal(
            f"holdout-ledger-required: split={split!r} requires a hold-out ledger — pass "
            "`holdout=...` or, to deliberately skip the defence, `enforce_holdout=False`"
        )

    session = FetchSession(config)
    result = RunResult()
    for page in plan["pages"]:
        if split not in page["splits_present"]:
            continue
        try:
            entry = fetch_page(session, page, holdout=holdout if enforce_holdout else None)
        except FetchHalt as halt:
            result.halted = type(halt).__name__
            result.entries.append(
                {
                    "identifier": page["identifier"],
                    "physical_page_id": page["physical_page_id"],
                    "status": "halted",
                    "reason": type(halt).__name__,
                    "detail": str(halt),
                }
            )
            break
        result.entries.append(entry)
    return result


FETCH_LOG_SCHEMA = "recordgold-fetch-log.v1"
FETCH_REFUSALS_SCHEMA = "recordgold-fetch-refusals.v1"
_FETCH_LOG_FIELDS = frozenset(
    {"schema", "split", "plan_self_hash", "holdout_self_hash", "entries", "halted", "self_hash"}
)

# A "fetched" entry's closed shape — everything `submission.FetchedPage` needs,
# named on the wire so `submission.fetched_pages_from_log` can build a
# `FetchedPage` from the log alone, with no fetch plan to consult back. Every
# field here is one `fetch_page` actually writes; a log missing one is refused
# `malformed-record` rather than read with a `KeyError` three modules away.
_FETCHED_ENTRY_FIELDS = frozenset(
    {
        "identifier",
        "physical_page_id",
        "status",
        "info_url",
        "image_url",
        "size_parameter_used",
        "response_sha256",
        "bytes",
        "http_status",
        "fetched_at_utc",
        "declared_width",
        "declared_height",
        "width",
        "height",
    }
)
# "refused" and "halted" entries share one shape (`fetch_page`'s except-branch and
# `run_fetch`'s halt branch write exactly these fields, no more).
_REFUSED_OR_HALTED_ENTRY_FIELDS = frozenset(
    {"identifier", "physical_page_id", "status", "reason", "detail"}
)
_ENTRY_STATUSES = frozenset({"fetched", "refused", "halted"})


def _closed_entry(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _seal_fetch_log(
    result: RunResult, *, split: str, plan: dict[str, Any], holdout: dict[str, Any] | None
) -> dict[str, Any]:
    """Close `result` into a canonical, self-hashed `recordgold-fetch-log.v1` record."""
    body: dict[str, Any] = {
        "schema": FETCH_LOG_SCHEMA,
        "split": split,
        "plan_self_hash": plan["self_hash"],
        "holdout_self_hash": holdout["self_hash"] if holdout is not None else None,
        "entries": result.entries,
        "halted": result.halted,
    }
    body["self_hash"] = self_hash(body)
    return body


def validate_fetch_log(record: Any) -> dict[str, Any]:
    """Refuse a fetch log that is not exactly `recordgold-fetch-log.v1`, closed and self-consistent.

    Every entry is checked against its own closed shape too, keyed by `status`:
    rule 6 ("nothing enters uninspected") covers what a caller reads out of an
    individual entry just as much as the log's own top-level fields, and
    `submission.fetched_pages_from_log` reads `"fetched"` entries by field name —
    a log entry silently missing one must be refused here, not three modules
    downstream as a `KeyError`.
    """
    if not isinstance(record, dict) or set(record) != _FETCH_LOG_FIELDS:
        raise CorpusRefusal(
            f"malformed-record: fetch log must be the closed record {sorted(_FETCH_LOG_FIELDS)}"
        )
    if record["schema"] != FETCH_LOG_SCHEMA:
        raise CorpusRefusal(
            f"wrong-schema: expected {FETCH_LOG_SCHEMA!r}, got {record['schema']!r}"
        )
    entries = record["entries"]
    if not isinstance(entries, list):
        raise CorpusRefusal("malformed-record: fetch log entries must be a list")
    seen_identifiers: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "status" not in entry:
            raise CorpusRefusal(
                f"malformed-record: entries[{index}] must be a dict carrying a status"
            )
        identifier = entry.get("identifier")
        if identifier in seen_identifiers:
            raise CorpusRefusal(
                f"malformed-record: entries[{index}] names identifier {identifier!r}, "
                f"already named by entries[{seen_identifiers[identifier]}] — a fetch log "
                "carries one entry per page"
            )
        seen_identifiers[identifier] = index
        status = entry["status"]
        if status not in _ENTRY_STATUSES:
            raise CorpusRefusal(
                f"malformed-record: entries[{index}] status {status!r} is not one of "
                f"{sorted(_ENTRY_STATUSES)}"
            )
        if status == "fetched":
            _closed_entry(entry, _FETCHED_ENTRY_FIELDS, f"entries[{index}] (status=fetched)")
        else:
            _closed_entry(
                entry, _REFUSED_OR_HALTED_ENTRY_FIELDS, f"entries[{index}] (status={status})"
            )
            if status == "refused" and entry["reason"] not in FETCH_REFUSAL_REASONS:
                raise CorpusRefusal(
                    f"malformed-record: entries[{index}] reason {entry['reason']!r} is not in "
                    "the closed FETCH_REFUSAL_REASONS vocabulary"
                )
    if not verify_self_hash(record):
        raise CorpusRefusal(
            "self-hash-mismatch: fetch log self_hash does not verify against its own content"
        )
    return record


def main(argv: list[str] | None = None) -> RunResult:
    """Run one fetch pass from the CLI, writing a sealed `ledger/fetch-log.json` and
    `ledger/refusals.json`.

    `--split test` additionally requires `--release-test-split`, and every other
    `--split` refuses it: deliberately fetching the held-out split is not the
    same mistake as a `val` or `train` build silently including a held page
    (`SPEC.md` §5.4 point 2), so releasing it needs a second, explicit flag
    rather than falling out of `--split` alone, and hold-out enforcement is on
    for every split but the one the flag deliberately releases. The distinct
    root §5.4 asks for is simply whichever `--cache-root`/`--info-root`/
    `--output-dir` the operator passes for that run — this module keeps no
    default of its own that would let a `val` and a `test` run collide on one
    directory by accident.

    A run that halted (403, or the per-run request ceiling) before covering the
    whole split exits nonzero, once both ledger files are sealed to disk: a
    wrapper script, cron job, or Makefile target that only checks the exit
    status must not be told a partial fetch completed. `run_fetch` itself still
    returns a `RunResult` with `halted` set — it is the library entry point,
    and this halt-to-`SystemExit` step is `main`'s alone.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="Path to a recordgold-fetch-plan.v1 file.")
    parser.add_argument("--holdout", help="Path to a recordgold-holdout.v1 file.")
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--info-root", required=True)
    parser.add_argument(
        "--output-dir", required=True, help="Where fetch-log.json/refusals.json land."
    )
    parser.add_argument("--split", default="val", choices=("train", "val", "test"))
    parser.add_argument(
        "--release-test-split",
        action="store_true",
        help=(
            f"Required to fetch --split {HELD_SPLIT}, and refused with any other --split — "
            "the flag releases the held split only, it does not disable hold-out "
            "enforcement for val or train."
        ),
    )
    args = parser.parse_args(argv)

    if (args.split == HELD_SPLIT) != args.release_test_split:
        raise CorpusRefusal(
            f"holdout-ledger-required: --release-test-split and --split {HELD_SPLIT} go "
            "together — fetching the held-out split must be a deliberate, separate act, "
            "and the flag releases no other split"
        )

    plan = plan_module.load_plan(args.plan)
    holdout = load_holdout(args.holdout) if args.holdout else None
    enforce_holdout = args.split != HELD_SPLIT
    config = FetchConfig(cache_root=Path(args.cache_root), info_root=Path(args.info_root))

    result = run_fetch(
        plan, config, split=args.split, holdout=holdout, enforce_holdout=enforce_holdout
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log = _seal_fetch_log(result, split=args.split, plan=plan, holdout=holdout)
    (output_dir / "fetch-log.json").write_bytes(canonical_bytes(log))

    refusals = [entry for entry in result.entries if entry.get("status") in ("refused", "halted")]
    refusals_body: dict[str, Any] = {
        "schema": FETCH_REFUSALS_SCHEMA,
        "split": args.split,
        "refusals": refusals,
    }
    refusals_body["self_hash"] = self_hash(refusals_body)
    (output_dir / "refusals.json").write_bytes(canonical_bytes(refusals_body))

    if result.halted is not None:
        raise SystemExit(
            f"halted: {result.halted} — the {args.split!r} split was not fetched in full; "
            f"see {output_dir / 'fetch-log.json'}"
        )

    return result


if __name__ == "__main__":
    main()
