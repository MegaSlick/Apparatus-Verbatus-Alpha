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
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000000\r\n\r\n")
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
