"""Regression coverage for the real HTTP transport, against a real socket.

``operations/serving/test_manager.py`` exercises every manager behavior
against ``HttpTransport`` fakes; nothing in this package previously drove
``UrllibHttpTransport`` itself against an actual listener.  Two real bugs
lived in that gap: the stdlib opener followed a redirect Location header to
any host with no same-origin check, and a response body was buffered whole
into memory with no size bound.  These tests pin both repairs against a real
local server, plus the loopback-absence classification the sequential lease
release depends on.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from .http import EndpointUnavailable, UrllibHttpTransport


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # pragma: no cover - silence test noise
        pass


@contextmanager
def _server(handler_factory) -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_transport_completes_a_real_get_and_post_round_trip() -> None:
    class Echo(_Handler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"echo:" + data)

    transport = UrllibHttpTransport()
    with _server(Echo) as base:
        get_response = transport.request("GET", f"{base}/v1/models", body=None, timeout_seconds=2.0)
        assert get_response.status == 200
        assert get_response.body == b'{"ok":true}'

        post_response = transport.request(
            "POST", f"{base}/v1/chat/completions", body=b'{"a":1}', timeout_seconds=2.0
        )
        assert post_response.status == 200
        assert post_response.body == b"echo:" + b'{"a":1}'


def test_transport_refuses_to_follow_a_redirect_to_another_host() -> None:
    """A loopback responder that redirects must not be silently followed elsewhere."""

    collected = threading.Event()

    class Collector(_Handler):
        def do_GET(self) -> None:  # noqa: N802
            collected.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"leaked":true}')

    transport = UrllibHttpTransport()
    with _server(Collector) as collector_base:

        class Redirector(_Handler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"{collector_base}/collected")
                self.end_headers()

        with _server(Redirector) as redirector_base:
            response = transport.request(
                "GET", f"{redirector_base}/v1/models", body=None, timeout_seconds=2.0
            )

    assert response.status == 302
    assert not collected.is_set(), "the redirect target must never be reached"


def test_transport_refuses_a_response_past_the_size_bound() -> None:
    class Oversized(_Handler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"x" * (8 * 1024 * 1024 + 1))

    transport = UrllibHttpTransport()
    with _server(Oversized) as base:
        with pytest.raises(EndpointUnavailable, match="exceeded"):
            transport.request("GET", f"{base}/v1/models", body=None, timeout_seconds=5.0)


@contextmanager
def _raw_server(respond) -> Iterator[str]:
    """One connection, answered by hand, so a malformed response can be sent."""

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:  # pragma: no cover - listener closed before a request arrived
            return
        with connection:
            connection.recv(65536)
            try:
                respond(connection)
            except OSError:  # pragma: no cover - the client hung up first
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()
        thread.join(timeout=5.0)


def test_transport_reports_a_truncated_chunked_body_as_an_unavailable_endpoint() -> None:
    """A body that stops mid-chunk must not escape as a bare `http.client` error.

    This is what a vLLM child killed mid-response looks like from the readiness
    poll, and the poll retries only `EndpointUnavailable`: anything else aborts
    a launch that was one interval from succeeding.
    """

    def truncated(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        connection.sendall(b"10\r\nabc")  # declares 16 bytes, sends 3, then closes

    with _raw_server(truncated) as base:
        with pytest.raises(EndpointUnavailable) as caught:
            UrllibHttpTransport().request(
                "GET", f"{base}/v1/models", body=None, timeout_seconds=3.0
            )

    assert "IncompleteRead" in str(caught.value)
    # A body that stopped short says nothing about whether a listener owns the
    # port, so it may never release the sequential residency lease.
    assert caught.value.definitively_absent is False


def test_transport_refuses_a_body_that_trickles_past_its_request_budget() -> None:
    """A responder cannot hold a request open by staying under the socket timeout.

    `timeout_seconds` bounds one blocking receive, and both loops that drive
    this transport check their own deadline only between requests — so one call
    that never returns defeats the readiness watchdog and the shutdown absence
    poll alike, with the card billing.
    """

    stop = threading.Event()

    def trickle(connection: socket.socket) -> None:
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n")
        while not stop.wait(0.05):
            connection.sendall(b"x")

    with _raw_server(trickle) as base:
        started = time.monotonic()
        try:
            with pytest.raises(EndpointUnavailable, match="did not complete within"):
                UrllibHttpTransport().request(
                    "GET", f"{base}/v1/models", body=None, timeout_seconds=0.5
                )
            elapsed = time.monotonic() - started
        finally:
            stop.set()

    # The declared budget, plus the slack of one in-flight receive.
    assert elapsed < 3.0, f"the request ran {elapsed:.1f}s against a 0.5s budget"


def test_transport_classifies_a_refused_connection_as_definitively_absent() -> None:
    transport = UrllibHttpTransport()
    with _server(_Handler) as base:
        pass  # server has been shut down; nothing is listening on this port now

    with pytest.raises(EndpointUnavailable) as caught:
        transport.request("GET", f"{base}/health", body=None, timeout_seconds=2.0)
    assert caught.value.definitively_absent is True


@pytest.mark.parametrize("proxy_variable", ["http_proxy", "HTTP_PROXY"])
def test_transport_ignores_an_ambient_proxy_and_reaches_the_loopback_model(
    proxy_variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured proxy must never stand between this transport and 127.0.0.1.

    The prompt and its embedded page image are in the request body, so a proxy
    that answered here would take them off the machine before any response
    validation ran — and could answer 200 for a model that was never reached.
    Both spellings are set with no ``no_proxy`` bypass, because an operator's
    correct ``NO_PROXY`` is not a control this boundary may depend on.
    """

    reached_proxy = threading.Event()

    class Proxy(_Handler):
        def do_POST(self) -> None:  # noqa: N802 - pragma: no cover - must never run
            reached_proxy.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"proxied":true}')

    class Model(_Handler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"model":"real"}')

    with _server(Proxy) as proxy_base, _server(Model) as model_base:
        for name in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(proxy_variable, proxy_base)
        # Both the construction and the request happen under the proxy
        # environment, which is what a process started that way looks like: the
        # opener has to refuse discovery whenever it is built.
        transport = UrllibHttpTransport()
        response = transport.request(
            "POST",
            f"{model_base}/v1/chat/completions",
            body=b'{"messages":[]}',
            timeout_seconds=5.0,
        )

    assert response.body == b'{"model":"real"}'
    assert not reached_proxy.is_set(), "the loopback request reached the proxy"


_BROKEN_BODIES = {
    # A chunk header promising 0x40 bytes, then three, then a close.
    "malformed-chunk": (
        b"Transfer-Encoding: chunked\r\n\r\n",
        b"40\r\nabc",
    ),
    # A complete Content-Length that the responder never finishes delivering.
    "interrupted-body": (
        b"Content-Length: 64\r\n\r\n",
        b'{"partial":true}',
    ),
}


@pytest.mark.parametrize("status", [b"200 OK", b"404 Not Found", b"503 Service Unavailable"])
@pytest.mark.parametrize("shape", sorted(_BROKEN_BODIES))
def test_a_broken_body_is_one_transport_refusal_whatever_the_status_was(
    status: bytes, shape: str
) -> None:
    """The status line must not decide which exception class a broken body becomes.

    The 4xx/5xx body used to be read inside a sibling `except urllib.error.HTTPError`
    clause, where nothing that failed *inside* it could reach the transport clause
    beside it.  The same truncated body therefore left here as `EndpointUnavailable`
    at 200 and as a bare `http.client.IncompleteRead` at 503 — and the readiness
    poll retries the first while the second aborts a start that was one interval
    from succeeding.
    """

    tail, partial = _BROKEN_BODIES[shape]

    def broken(connection: socket.socket) -> None:
        connection.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n" + tail)
        connection.sendall(partial)

    with _raw_server(broken) as base:
        with pytest.raises(EndpointUnavailable) as caught:
            UrllibHttpTransport().request(
                "GET", f"{base}/v1/models", body=None, timeout_seconds=3.0
            )

    # A body that stopped short says nothing about whether a listener owns the port.
    assert caught.value.definitively_absent is False


@pytest.mark.parametrize("status", [b"200 OK", b"404 Not Found", b"503 Service Unavailable"])
def test_a_body_that_never_arrives_is_one_transport_refusal_whatever_the_status_was(
    status: bytes,
) -> None:
    """A read that times out mid-body classifies the same way at every status."""

    stop = threading.Event()

    def stalled(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
            b"Content-Length: 64\r\n\r\n"
        )
        stop.wait(10.0)

    with _raw_server(stalled) as base:
        started = time.monotonic()
        try:
            with pytest.raises(EndpointUnavailable):
                UrllibHttpTransport().request(
                    "GET", f"{base}/v1/models", body=None, timeout_seconds=0.5
                )
            elapsed = time.monotonic() - started
        finally:
            stop.set()

    assert elapsed < 3.0, f"the request ran {elapsed:.1f}s against a 0.5s budget"


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_a_complete_error_response_keeps_its_status_and_body(status: int) -> None:
    """Normalising the read must not turn a complete 4xx/5xx into a refusal.

    vLLM answers a not-yet-loaded model with a real error response, and the
    readiness parsers name their own refusal from that status.  Losing it would
    replace a diagnosable `HTTP 503` with an unavailable-endpoint message.
    """

    class Refusing(_Handler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not loaded"}')

    with _server(Refusing) as base:
        response = UrllibHttpTransport().request(
            "GET", f"{base}/v1/models", body=None, timeout_seconds=5.0
        )

    assert response.status == status
    assert response.body == b'{"error":"not loaded"}'


def _dribble(status: bytes, *, headers_slowly: bool):
    """A responder that stays inside the socket timeout and never finishes."""

    stop = threading.Event()

    def respond(connection: socket.socket) -> None:
        if headers_slowly:
            connection.sendall(b"HTTP/1.1 " + status + b"\r\n")
            while not stop.wait(0.05):
                connection.sendall(b"X-Pad: pad\r\n")
        else:
            connection.sendall(
                b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                b"Content-Length: 4096\r\n\r\n"
            )
            while not stop.wait(0.05):
                connection.sendall(b"x")

    return respond, stop


@pytest.mark.parametrize("status", [b"200 OK", b"503 Service Unavailable"])
@pytest.mark.parametrize("headers_slowly", [False, True], ids=["slow-body", "slow-headers"])
def test_the_declared_timeout_bounds_the_whole_call_not_one_receive(
    status: bytes, headers_slowly: bool
) -> None:
    """Connect, headers and body come out of one monotonic deadline.

    Measured at 36acde636f against a declared 0.15s: a response whose *headers*
    dribbled in returned HTTP 200 after 1.280s, because the body deadline was
    created only once the opener had already returned them.  Both loops that
    drive this transport consult their own deadline between requests only, so
    one such call defeats the readiness watchdog and the shutdown absence poll
    alike, on a card that bills by the hour.
    """

    respond, stop = _dribble(status, headers_slowly=headers_slowly)
    with _raw_server(respond) as base:
        started = time.monotonic()
        try:
            with pytest.raises(EndpointUnavailable) as caught:
                UrllibHttpTransport().request(
                    "GET", f"{base}/v1/models", body=None, timeout_seconds=0.4
                )
            elapsed = time.monotonic() - started
        finally:
            stop.set()

    assert elapsed < 3.0, f"the call ran {elapsed:.2f}s against a 0.4s budget"
    # An overrun proves nothing about whether a listener owns the port, so it
    # may never release the sequential residency lease.
    assert caught.value.definitively_absent is False


def test_a_slow_but_finite_response_still_succeeds_inside_its_budget() -> None:
    """The counterfactual: the deadline must not refuse a merely slow answer."""

    body = b'{"data":[{"id":"reader-api"}]}'

    def slow_but_finite(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
        )
        for start in range(0, len(body), 8):
            time.sleep(0.02)
            connection.sendall(body[start : start + 8])

    with _raw_server(slow_but_finite) as base:
        response = UrllibHttpTransport().request(
            "GET", f"{base}/v1/models", body=None, timeout_seconds=5.0
        )

    assert response.status == 200
    assert response.body == body
