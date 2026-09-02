"""Tests for `fetch.py` and `cache.py`, offline over a loopback `ThreadingHTTPServer`.

No network: every server this suite talks to is bound to `127.0.0.1` on an
ephemeral port, started and stopped per test. `parse_record_url` (`plan.py`)
pins the real IIIF host, so these tests build fetch-plan-shaped page dicts by
hand rather than through `build_fetch_plan` — `fetch.py`'s contract is a page
dict carrying `identifier`, `info_url`, `image_url_candidates`, `splits_present`,
`records`, `physical_page_id`; where that dict comes from is `plan.py`'s
business, not this module's.

JPEG bytes are produced with Pillow rather than `proof/synthetic_pages.py`:
that module's only encoder (`common.imaging.encode_grayscale_png`) writes PNG,
which is the pipeline's own raster codec, not the JPEG a IIIF server actually
returns. Page geometry (200x260) is still borrowed from
`proof/synthetic_pages.PAGES[0]` so the fixture's *shape* traces to the
project's one synthetic-page convention even though its bytes do not.
"""

import http.server
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from proof.synthetic_pages import PAGES

from . import cache as cache_module
from . import fetch as fetch_module
from .fetch import (
    FetchConfig,
    FetchSession,
    Http403Stop,
    fetch_page,
    run_fetch,
)
from .holdout import build_holdout

PAGE_WIDTH = PAGES[0]["width"]
PAGE_HEIGHT = PAGES[0]["height"]


def _jpeg_bytes(width: int, height: int, exif_orientation: int | None = None) -> bytes:
    import io

    image = Image.new("RGB", (width, height), color=(96, 96, 96))
    buffer = io.BytesIO()
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        image.save(buffer, format="JPEG", exif=exif)
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


GOOD_JPEG = _jpeg_bytes(PAGE_WIDTH, PAGE_HEIGHT)
WRONG_SIZE_JPEG = _jpeg_bytes(PAGE_WIDTH - 10, PAGE_HEIGHT)
ROTATED_JPEG = _jpeg_bytes(PAGE_WIDTH, PAGE_HEIGHT, exif_orientation=6)
INFO_JSON = json.dumps({"width": PAGE_WIDTH, "height": PAGE_HEIGHT}).encode("utf-8")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.script: dict[str, list[dict[str, Any]]] = {}
        self.hit_counts: dict[str, int] = {}
        self.lock = threading.Lock()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib signature)
        pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib override)
        server: _Server = self.server  # type: ignore[assignment]
        with server.lock:
            server.hit_counts[self.path] = server.hit_counts.get(self.path, 0) + 1
        responses = server.script.get(self.path)
        if not responses:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        spec = responses.pop(0) if len(responses) > 1 else responses[0]
        status = spec.get("status", 200)
        body: bytes = spec.get("body", b"")
        truncate = spec.get("truncate", False)
        self.send_response(status)
        self.send_header("Content-Type", spec.get("content_type", "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        for name, value in spec.get("headers", {}).items():
            self.send_header(name, value)
        self.end_headers()
        if truncate:
            self.wfile.write(body[: len(body) // 2])
            self.close_connection = True
        else:
            self.wfile.write(body)


@pytest.fixture
def server():
    httpd = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _base_url(httpd: _Server) -> str:
    host, port = httpd.server_address[:2]
    return f"http://{host}:{port}"


def _page(
    httpd: _Server,
    identifier: str,
    *,
    records: list[dict[str, Any]] | None = None,
    splits_present: list[str] | None = None,
) -> dict[str, Any]:
    base = f"{_base_url(httpd)}/iiif/2/{identifier}"
    if records is None:
        records = [{"record_id": f"{identifier}-r0", "region": {"x": 5, "y": 5, "w": 10, "h": 10}}]
    return {
        "identifier": identifier,
        "info_url": f"{base}/info.json",
        "image_url_candidates": {
            "full": f"{base}/full/full/0/default.jpg",
            "max": f"{base}/full/max/0/default.jpg",
        },
        "physical_page_id": f"pac-{identifier}",
        "splits_present": splits_present or ["val"],
        "records": records,
    }


def _script(
    httpd: _Server,
    identifier: str,
    *,
    info: list[dict[str, Any]],
    full: list[dict[str, Any]] | None = None,
    max_: list[dict[str, Any]] | None = None,
) -> None:
    httpd.script[f"/iiif/2/{identifier}/info.json"] = info
    if full is not None:
        httpd.script[f"/iiif/2/{identifier}/full/full/0/default.jpg"] = full
    if max_ is not None:
        httpd.script[f"/iiif/2/{identifier}/full/max/0/default.jpg"] = max_


def _config(tmp_path: Path, **overrides: Any) -> FetchConfig:
    defaults = dict(
        cache_root=tmp_path / "cache",
        info_root=tmp_path / "info",
        min_interval_seconds=0.0,
        max_backoff_seconds=0.05,
        sleep=lambda seconds: None,
        clock=lambda: "2026-09-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return FetchConfig(**defaults)


# --------------------------------------------------------------------------- #
# Happy path


def test_fetch_page_full_succeeds(tmp_path, server):
    _script(server, "vol/001", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    page = _page(server, "vol/001")
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "fetched"
    assert entry["size_parameter_used"] == "full"
    assert entry["response_sha256"] == digest_bytes(GOOD_JPEG)
    assert entry["declared_width"] == PAGE_WIDTH
    assert entry["declared_height"] == PAGE_HEIGHT
    cached = cache_module.body_path(tmp_path / "cache", entry["response_sha256"])
    assert cached.read_bytes() == GOOD_JPEG


def test_full_falls_back_to_max_on_400(tmp_path, server):
    _script(
        server,
        "vol/002",
        info=[{"body": INFO_JSON}],
        full=[{"status": 400, "body": b""}],
        max_=[{"body": GOOD_JPEG}],
    )
    page = _page(server, "vol/002")
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "fetched"
    assert entry["size_parameter_used"] == "max"


def test_full_falls_back_to_max_on_501(tmp_path, server):
    _script(
        server,
        "vol/003",
        info=[{"body": INFO_JSON}],
        full=[{"status": 501, "body": b""}],
        max_=[{"body": GOOD_JPEG}],
    )
    page = _page(server, "vol/003")
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "fetched"
    assert entry["size_parameter_used"] == "max"


# --------------------------------------------------------------------------- #
# Refusals by name


def test_dimension_mismatch_refused(tmp_path, server):
    _script(server, "vol/004", info=[{"body": INFO_JSON}], full=[{"body": WRONG_SIZE_JPEG}])
    page = _page(server, "vol/004")
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "refused"
    assert entry["reason"] == "dimension-mismatch"


def test_exif_orientation_refused(tmp_path, server):
    _script(server, "vol/005", info=[{"body": INFO_JSON}], full=[{"body": ROTATED_JPEG}])
    page = _page(server, "vol/005")
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "refused"
    assert entry["reason"] == "exif-orientation"


def test_region_outside_page_refused(tmp_path, server):
    _script(server, "vol/006", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    page = _page(
        server,
        "vol/006",
        records=[
            {"record_id": "vol/006-r0", "region": {"x": PAGE_WIDTH - 5, "y": 0, "w": 20, "h": 20}}
        ],
    )
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "refused"
    assert entry["reason"] == "region-outside-page"


def test_404_refused_as_http_error(tmp_path, server):
    _script(server, "vol/007", info=[{"body": INFO_JSON}], full=[{"status": 404, "body": b""}])
    page = _page(server, "vol/007")
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page)

    assert entry["status"] == "refused"
    assert entry["reason"] == "http-error"


def test_duplicate_page_bytes_refused(tmp_path, server):
    _script(server, "vol/008a", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    _script(server, "vol/008b", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    session = FetchSession(_config(tmp_path))

    first = fetch_page(session, _page(server, "vol/008a"))
    second = fetch_page(session, _page(server, "vol/008b"))

    assert first["status"] == "fetched"
    assert second["status"] == "refused"
    assert second["reason"] == "duplicate-page-bytes"


def test_duplicate_page_bytes_refused_across_sessions(tmp_path, server):
    """The digest memory is loaded from disk, so a later run still catches it."""
    _script(server, "vol/008c", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    _script(server, "vol/008d", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    config = _config(tmp_path)

    fetch_page(FetchSession(config), _page(server, "vol/008c"))
    second = fetch_page(FetchSession(config), _page(server, "vol/008d"))

    assert second["status"] == "refused"
    assert second["reason"] == "duplicate-page-bytes"


# --------------------------------------------------------------------------- #
# Politeness: retries, backoff, and the stop-on-403 run halt


def test_429_with_retry_after_then_success(tmp_path, server):
    sleeps: list[float] = []
    _script(
        server,
        "vol/009",
        info=[{"body": INFO_JSON}],
        full=[
            {"status": 429, "body": b"", "headers": {"Retry-After": "0"}},
            {"body": GOOD_JPEG},
        ],
    )
    page = _page(server, "vol/009")
    session = FetchSession(_config(tmp_path, sleep=sleeps.append))

    entry = fetch_page(session, page)

    assert entry["status"] == "fetched"
    assert len(sleeps) >= 1


def test_503_exhausts_retries_then_refused(tmp_path, server):
    _script(
        server,
        "vol/010",
        info=[{"body": INFO_JSON}],
        full=[{"status": 503, "body": b""}],
    )
    page = _page(server, "vol/010")
    session = FetchSession(_config(tmp_path, max_retries=2, max_backoff_seconds=0.01))

    entry = fetch_page(session, page)

    assert entry["status"] == "refused"
    assert entry["reason"] == "http-error"
    # 1 initial + 2 retries against the *same* URL.
    assert server.hit_counts["/iiif/2/vol/010/full/full/0/default.jpg"] == 3


def test_403_halts_the_whole_run(tmp_path, server):
    _script(server, "vol/011a", info=[{"body": INFO_JSON}], full=[{"status": 403, "body": b""}])
    _script(server, "vol/011b", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    plan = {
        "pages": [
            _page(server, "vol/011a"),
            _page(server, "vol/011b"),
        ]
    }
    result = run_fetch(plan, _config(tmp_path), split="val", enforce_holdout=False)

    assert result.halted == "Http403Stop"
    assert result.entries == []  # never even reached the second page
    assert server.hit_counts.get("/iiif/2/vol/011b/info.json") is None


def test_fetch_page_raises_http403stop_directly(tmp_path, server):
    _script(server, "vol/011c", info=[{"body": INFO_JSON}], full=[{"status": 403, "body": b""}])
    page = _page(server, "vol/011c")
    session = FetchSession(_config(tmp_path))

    with pytest.raises(Http403Stop):
        fetch_page(session, page)


def test_request_ceiling_halts_the_run(tmp_path, server):
    _script(server, "vol/012a", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    _script(server, "vol/012b", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    plan = {"pages": [_page(server, "vol/012a"), _page(server, "vol/012b")]}
    # One page's happy path costs two requests (info + image); a ceiling of 2
    # must halt before the second page is touched at all.
    result = run_fetch(
        plan, _config(tmp_path, max_requests_per_run=2), split="val", enforce_holdout=False
    )

    assert result.halted == "RequestCeilingReached"
    assert len(result.entries) == 1
    assert server.hit_counts.get("/iiif/2/vol/012b/info.json") is None


# --------------------------------------------------------------------------- #
# Never re-fetch, and the interrupt-leaves-no-record guarantee


def test_cached_request_key_is_never_re_requested(tmp_path, server):
    _script(server, "vol/013", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    config = _config(tmp_path)

    first = fetch_page(FetchSession(config), _page(server, "vol/013"))
    info_hits_after_first = server.hit_counts["/iiif/2/vol/013/info.json"]
    image_hits_after_first = server.hit_counts["/iiif/2/vol/013/full/full/0/default.jpg"]

    second = fetch_page(FetchSession(config), _page(server, "vol/013"))

    assert first["status"] == second["status"] == "fetched"
    assert first["response_sha256"] == second["response_sha256"]
    assert server.hit_counts["/iiif/2/vol/013/info.json"] == info_hits_after_first
    assert server.hit_counts["/iiif/2/vol/013/full/full/0/default.jpg"] == image_hits_after_first


def test_truncated_body_leaves_no_request_record(tmp_path, server):
    _script(
        server,
        "vol/014",
        info=[{"body": INFO_JSON}],
        full=[{"body": GOOD_JPEG, "truncate": True}],
    )
    page = _page(server, "vol/014")
    config = _config(tmp_path)
    session = FetchSession(config)

    entry = fetch_page(session, page)

    assert entry["status"] == "refused"
    assert entry["reason"] == "http-error"
    key = fetch_module._image_request_key("vol/014", "full")
    assert cache_module.load_request_record(config.cache_root, key) is None
    # Nothing was cached under any digest either — an interrupted body was never stored.
    assert not any((config.cache_root).glob("*.jpg"))


def test_truncated_body_is_retried_on_the_next_run(tmp_path, server):
    """No request record means the next attempt asks the server again, not skips it."""
    server.script["/iiif/2/vol/015/info.json"] = [{"body": INFO_JSON}]
    server.script["/iiif/2/vol/015/full/full/0/default.jpg"] = [
        {"body": GOOD_JPEG, "truncate": True},
        {"body": GOOD_JPEG},
    ]
    page = _page(server, "vol/015")
    config = _config(tmp_path)

    first = fetch_page(FetchSession(config), page)
    second = fetch_page(FetchSession(config), page)

    assert first["status"] == "refused"
    assert second["status"] == "fetched"
    assert server.hit_counts["/iiif/2/vol/015/full/full/0/default.jpg"] == 2


# --------------------------------------------------------------------------- #
# Hold-out enforcement (defensive, at fetch time — SPEC.md §5.4 point 2)


def _sample_rows() -> list[dict[str, Any]]:
    def _row(record_id: str, split: str, path: str) -> dict[str, Any]:
        text = f"text for {record_id}"
        return {
            "split": split,
            "source": "geneanet",
            "record_id": record_id,
            "record_url": f"https://europe.iiif.teklia.com/iiif/2/{path}/1,1,10,10/full/0/default.jpg",
            "start_date": None,
            "end_date": None,
            "parish": None,
            "text": text,
            "text_sha256": digest_bytes(text.encode("utf-8")),
        }

    return [_row("r-held", "test", "held/001.jpg")]


def test_holdout_page_refused_by_fetcher(tmp_path, server):
    holdout = build_holdout(_sample_rows(), digest_bytes(b"snapshot"))
    _script(server, "held/001.jpg", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    page = _page(server, "held/001.jpg", splits_present=["test"])
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page, holdout=holdout)

    assert entry["status"] == "refused"
    assert entry["reason"] == "holdout-page"
    assert "/iiif/2/held/001.jpg/info.json" not in server.hit_counts


def test_cross_split_page_refused_by_fetcher(tmp_path, server):
    holdout = build_holdout(_sample_rows(), digest_bytes(b"snapshot"))
    page = _page(server, "held/001.jpg", splits_present=["val", "test"])
    session = FetchSession(_config(tmp_path))

    entry = fetch_page(session, page, holdout=holdout)

    assert entry["status"] == "refused"
    assert entry["reason"] == "cross-split-page"


def test_run_fetch_with_split_test_explicit_skips_holdout_enforcement(tmp_path, server):
    holdout = build_holdout(_sample_rows(), digest_bytes(b"snapshot"))
    _script(server, "held/001.jpg", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    plan = {"pages": [_page(server, "held/001.jpg", splits_present=["test"])]}

    result = run_fetch(
        plan, _config(tmp_path), split="test", holdout=holdout, enforce_holdout=False
    )

    assert result.halted is None
    assert result.entries[0]["status"] == "fetched"


def test_run_fetch_filters_pages_to_the_requested_split(tmp_path, server):
    _script(server, "vol/016", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}])
    plan = {
        "pages": [
            _page(server, "vol/016", splits_present=["val"]),
            _page(server, "vol/017", splits_present=["train"]),
        ]
    }

    result = run_fetch(plan, _config(tmp_path), split="val", enforce_holdout=False)

    assert len(result.entries) == 1
    assert result.entries[0]["identifier"] == "vol/016"


# --------------------------------------------------------------------------- #
# cache.py directly


def test_cache_write_new_file_refuses_overwrite(tmp_path):
    path = tmp_path / "a.json"
    assert cache_module.write_new_file(path, b"{}")
    assert not cache_module.write_new_file(path, b'{"different": true}')
    assert path.read_bytes() == b"{}"


def test_cache_write_request_record_refuses_duplicate(tmp_path):
    cache_root = tmp_path / "cache"
    key = cache_module.compute_request_key(
        kind="image",
        identifier="x",
        region="full",
        size="full",
        rotation="0",
        quality="default",
        format="jpg",
    )
    cache_module.write_request_record(cache_root, key, {"kind": "image"})
    with pytest.raises(fetch_module.CorpusRefusal):
        cache_module.write_request_record(cache_root, key, {"kind": "image"})


def test_cache_request_key_is_stable_and_distinguishes_size(tmp_path):
    common = dict(
        kind="image", identifier="x", region="full", rotation="0", quality="default", format="jpg"
    )
    key_full = cache_module.compute_request_key(size="full", **common)
    key_max = cache_module.compute_request_key(size="max", **common)
    key_full_again = cache_module.compute_request_key(size="full", **common)

    assert key_full == key_full_again
    assert key_full != key_max
