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

from common.contracts.canonical import canonical_bytes, digest_bytes

from . import CorpusRefusal
from . import cache as cache_module
from .holdout import refuse_held_out_page

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

# Declares the project and a contact, per `SPEC.md` §5.1's politeness list. A
# real run against a real server should pass a config carrying an operator's own
# contact; this default names the project so an operator forgetting to override
# it still identifies the traffic honestly.
DEFAULT_USER_AGENT = (
    "ApparatusVerbatus-RecordGold-Fetcher/1.0 "
    "(+https://github.com/apparatus-verbatus/verbatus_alpha; research use, non-commercial)"
)

# `pipeline/1_exemplar/image_formats.py` caps a source at 64 MiB; the fetcher
# refuses a body larger than that itself rather than accepting bytes the Door
# would only refuse two stages later.
MAX_BODY_BYTES = 64 * 1024 * 1024

_EXIF_ORIENTATION_TAG = 0x0112

_SIZE_ORDER = ("full", "max")
_FALLBACK_STATUSES = frozenset({400, 501})


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
    """`Retry-After` (seconds form) if present and sane, else bounded exponential backoff."""
    if retry_after_header:
        try:
            seconds = float(retry_after_header)
        except ValueError:
            pass  # The HTTP-date form is not parsed; fall through to backoff.
        else:
            if seconds >= 0:
                return min(seconds, max_backoff_seconds)
    return min(float(2**attempt), max_backoff_seconds)


def _read_bounded(response: Any, max_bytes: int, expected_length: int | None) -> bytes:
    """Read a response body in chunks, bounded above and verified below.

    Refuses a body over `max_bytes` rather than keep reading it, and — when the
    server declared a `Content-Length` — refuses a body that came up short: a
    server or connection that closes mid-transfer must not be mistaken for one
    that finished, or a truncated JPEG would be cached and recorded as a
    completed fetch (`SPEC.md` §5.1's "an interrupt loses at most one in-flight
    body" requires the interrupted one to leave no record at all).
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
    if expected_length is not None and total < expected_length:
        raise CorpusRefusal(
            f"http-error: response body truncated — received {total} of {expected_length} "
            "declared bytes"
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
        if (
            config.max_requests_per_run is not None
            and self.request_count >= config.max_requests_per_run
        ):
            raise RequestCeilingReached(
                f"per-run request ceiling {config.max_requests_per_run} reached before {url!r}"
            )
        attempt = 0
        while True:
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
    """Every response digest already claimed by an earlier image fetch, from disk.

    Maps digest -> the identifier that first claimed it, not just a set: a page
    re-fetched from cache on a later run must not be flagged as a duplicate of
    *itself*, only of some *other* identifier landing the same bytes. Makes
    `duplicate-page-bytes` detection survive across runs, not just within one
    process — both fetches' request records are read back here.
    """
    requests_dir = Path(cache_root) / "requests"
    digests: dict[str, str] = {}
    if not requests_dir.exists():
        return digests
    for path in sorted(requests_dir.glob("*.json")):
        record = json.loads(path.read_bytes())
        if record.get("kind") == "image" and record.get("response_sha256"):
            digests.setdefault(record["response_sha256"], record["identifier"])
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
    """`info.json`, once per identifier, retained under `info_root` and never re-asked."""
    identifier = page["identifier"]
    key = _info_request_key(identifier)
    record = cache_module.load_request_record(session.config.cache_root, key)
    if record is not None:
        info_path = _info_path(session.config.info_root, identifier)
        return json.loads(info_path.read_bytes())

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
    cache_module.write_new_file(info_path, canonical_bytes({**declared, "raw": info}))
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


def _fetch_image_bytes(session: FetchSession, page: dict[str, Any]) -> tuple[bytes, str]:
    """Full-resolution image: try `full`, fall back to `max` on 400/501. Returns (body, size_used)."""
    identifier = page["identifier"]
    candidates = page["image_url_candidates"]
    last_error: Exception | None = None
    for size_parameter in _SIZE_ORDER:
        key = _image_request_key(identifier, size_parameter)
        record = cache_module.load_request_record(session.config.cache_root, key)
        if record is not None:
            if record.get("status") == "fetched":
                body_path = cache_module.body_path(
                    session.config.cache_root, record["response_sha256"]
                )
                return body_path.read_bytes(), size_parameter
            # A previously recorded fallback (e.g. "full" recorded as unsupported)
            # means never ask that size again either — move straight to the next.
            last_error = _HttpStatusError(record.get("http_status", 0), candidates[size_parameter])
            continue
        try:
            body, status = session._request(candidates[size_parameter])
        except _HttpStatusError as error:
            if size_parameter == "full" and error.status in _FALLBACK_STATUSES:
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
                "fetched_at_utc": session.config.clock(),
            },
        )
        return body, size_parameter
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

        body, size_used = _fetch_image_bytes(session, page)
        response_sha256 = digest_bytes(body)

        owner = session.seen_response_digests.get(response_sha256)
        if owner is not None and owner != identifier:
            raise CorpusRefusal(
                f"duplicate-page-bytes: identifier {identifier!r} produced response digest "
                f"{response_sha256!r}, already claimed by identifier {owner!r}"
            )

        image = _decode_jpeg(body)
        _check_dimensions(image, width, height)
        _check_exif_orientation(image)
        _check_regions(page["records"], width, height)

        session.seen_response_digests.setdefault(response_sha256, identifier)
        entry.update(
            {
                "status": "fetched",
                "size_parameter_used": size_used,
                "response_sha256": response_sha256,
                "declared_width": width,
                "declared_height": height,
            }
        )
        return entry
    except CorpusRefusal as error:
        reason = str(error).split(":", 1)[0]
        entry.update({"status": "refused", "reason": reason, "detail": str(error)})
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
    """
    session = FetchSession(config)
    result = RunResult()
    for page in plan["pages"]:
        if split not in page["splits_present"]:
            continue
        try:
            entry = fetch_page(session, page, holdout=holdout if enforce_holdout else None)
        except FetchHalt as halt:
            result.halted = type(halt).__name__
            break
        result.entries.append(entry)
    return result
