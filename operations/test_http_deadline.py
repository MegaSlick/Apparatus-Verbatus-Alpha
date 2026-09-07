"""The whole-call deadline primitive, against real threads and a real socket."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from .http_deadline import DeadlineExceeded, call_within_deadline, recording_opener


def test_a_work_result_is_returned_unchanged() -> None:
    assert (
        call_within_deadline(
            lambda: "answer", budget_seconds=5.0, label="work", cancel=lambda: None
        )
        == "answer"
    )


def test_the_worker_s_own_exception_is_re_raised_with_its_traceback() -> None:
    """Every caller classifies transport exceptions; the deadline must not hide one."""

    def failing() -> None:
        raise ValueError("the worker's own failure")

    with pytest.raises(ValueError, match="the worker's own failure") as caught:
        call_within_deadline(failing, budget_seconds=5.0, label="work", cancel=lambda: None)

    frames = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "failing" in frames, "the worker frame must survive the relay"


def test_an_overrunning_call_is_refused_at_its_budget_and_cancelled() -> None:
    release = threading.Event()
    cancelled = threading.Event()

    def blocked() -> None:
        release.wait(10.0)

    started = time.monotonic()
    try:
        with pytest.raises(DeadlineExceeded) as caught:
            call_within_deadline(
                blocked, budget_seconds=0.2, label="blocked work", cancel=cancelled.set
            )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert cancelled.is_set(), "the deadline must cancel the work it abandoned"
    assert caught.value.budget_seconds == 0.2
    # The budget, plus the grace the cancelled worker is given to unwind.
    assert elapsed < 0.2 + 3.0, f"the call ran {elapsed:.2f}s against a 0.2s budget"
    assert "did not complete within its 0.2s deadline" in str(caught.value)


def test_a_worker_that_will_not_unwind_is_named_rather_than_passed_over() -> None:
    """A hung worker is a fact the refusal carries; silence would be a lie."""

    release = threading.Event()

    def unstoppable() -> None:
        release.wait(10.0)

    try:
        with pytest.raises(DeadlineExceeded) as caught:
            call_within_deadline(
                unstoppable,
                budget_seconds=0.05,
                label="unstoppable work",
                # A cancel that cancels nothing, which is exactly what a seam
                # exposing no socket gives this primitive.
                cancel=lambda: None,
            )
    finally:
        release.set()

    assert caught.value.worker_still_running is True
    # No socket was shut down here -- the cancel was a no-op -- so the refusal
    # must not say one was (the independent read of this branch).
    assert caught.value.cancel_attempted is False
    assert "was abandoned still running" in str(caught.value)
    assert "socket was shut down" not in str(caught.value)


def test_a_worker_that_survives_a_real_cancel_is_named_with_the_socket_shut_down() -> None:
    """The stronger clause is earned only when a cancel actually ran."""

    release = threading.Event()

    def unstoppable() -> None:
        release.wait(10.0)

    try:
        with pytest.raises(DeadlineExceeded) as caught:
            call_within_deadline(
                unstoppable,
                budget_seconds=0.05,
                label="unstoppable work",
                cancel=lambda: [],  # a cancel that ran and had nothing to report
            )
    finally:
        release.set()

    assert caught.value.cancel_attempted is True
    assert "had not unwound when the socket was shut down" in str(caught.value)


def test_a_non_positive_budget_is_refused_without_starting_work() -> None:
    started = threading.Event()

    with pytest.raises(DeadlineExceeded):
        call_within_deadline(started.set, budget_seconds=0.0, label="work", cancel=lambda: None)

    assert not started.is_set()


@contextmanager
def _stalled_server() -> Iterator[str]:
    """Accept one connection, send nothing, and never answer."""

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held: list[socket.socket] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:  # pragma: no cover - closed before a request arrived
            return
        held.append(connection)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/"
    finally:
        listener.close()
        thread.join(timeout=5.0)
        for connection in held:
            connection.close()


def test_the_recording_opener_s_cancel_unblocks_a_real_stalled_read() -> None:
    """This is what keeps an expired call from leaking one socket per interval.

    A join that merely gave up would leave the worker blocked in `recv` for as
    long as the responder liked — once per readiness poll interval.
    """

    with _stalled_server() as url:
        opener, cancel = recording_opener()
        request = urllib.request.Request(url, method="GET")

        def exchange() -> int:
            with opener.open(request, timeout=30.0) as response:
                return int(response.status)

        started = time.monotonic()
        with pytest.raises(DeadlineExceeded) as caught:
            call_within_deadline(exchange, budget_seconds=0.3, label="GET stalled", cancel=cancel)
        elapsed = time.monotonic() - started

    # A socket timeout of 30s was configured, so only cancellation can end this
    # inside the budget — and the worker really unwound rather than being left
    # blocked, which is the whole point of breaking the socket.
    assert elapsed < 3.0, f"the call ran {elapsed:.2f}s against a 0.3s budget"
    assert caught.value.worker_still_running is False
    assert caught.value.cancel_failures == ()


def test_the_recording_opener_still_carries_the_handlers_it_is_given() -> None:
    """The recording handlers are added to a caller's handlers, never instead of them."""

    class _Refuse(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    opener, _ = recording_opener(urllib.request.ProxyHandler({}), _Refuse)
    installed = {type(handler).__name__ for handler in opener.handlers}
    assert "_Refuse" in installed
    assert "_RecordingHTTPHandler" in installed
    # `ProxyHandler({})` is deliberately absent from `opener.handlers`: an empty
    # proxy map defines no `<scheme>_open` method, so `add_handler` never
    # installs it — which is precisely the discovery being off.  Passing it is
    # what stops `build_opener` adding the *discovering* default in its place;
    # `operations/serving/test_http.py` pins that behaviour against a real proxy.
    assert "ProxyHandler" not in installed
    # And the recording handler must not have displaced the ordinary defaults.
    assert "HTTPDefaultErrorHandler" in installed
    assert "HTTPHandler" not in installed, "the recording handler replaces the default one"


def test_an_ordinary_request_still_completes_through_the_recording_opener() -> None:
    body = b'{"ok":true}'

    def serve(listener: socket.socket) -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                + body
            )

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    thread = threading.Thread(target=serve, args=(listener,), daemon=True)
    thread.start()
    try:
        opener, cancel = recording_opener()
        request = urllib.request.Request(
            f"http://127.0.0.1:{listener.getsockname()[1]}/", method="GET"
        )

        def exchange() -> tuple[int, bytes]:
            try:
                with opener.open(request, timeout=5.0) as response:
                    return int(response.status), response.read()
            except urllib.error.HTTPError as error:  # pragma: no cover - a 200 is sent
                return int(error.code), error.read()

        assert call_within_deadline(
            exchange, budget_seconds=5.0, label="GET ok", cancel=cancel
        ) == (200, body)
    finally:
        listener.close()
        thread.join(timeout=5.0)


def test_the_https_handler_keeps_the_stdlib_s_verified_tls_context() -> None:
    """The RunPod transport is HTTPS, and this handler must not weaken it.

    `https_open` is overridden to install the recording connection class, which
    means reaching into `HTTPSHandler._context`. If a future CPython renames it,
    this fails here rather than silently downgrading a money-path connection.
    """

    import ssl

    opener, _ = recording_opener()
    handler = next(h for h in opener.handlers if type(h).__name__ == "_RecordingHTTPSHandler")
    assert isinstance(handler._context, ssl.SSLContext)
    assert handler._context.check_hostname is True
    assert handler._context.verify_mode is ssl.CERT_REQUIRED

    captured: dict[str, object] = {}

    def capture(http_class, req, **keywords):  # type: ignore[no-untyped-def]
        captured["class"] = http_class
        captured["keywords"] = keywords

    handler.do_open = capture  # type: ignore[method-assign]
    handler.https_open(urllib.request.Request("https://example.invalid/", method="GET"))

    assert captured["class"].__name__ == "_RecordingHTTPSConnection"
    assert captured["keywords"] == {"context": handler._context}


def test_a_socket_is_cancellable_from_the_moment_it_exists() -> None:
    """Recording only after `connect()` returns leaves a CONNECT tunnel unbreakable.

    `HTTPConnection.connect` opens the socket, then — for HTTPS through a proxy —
    blocks reading the tunnel response, then wraps the result in TLS. The socket
    therefore has to be recorded as `_create_connection` produces it, not after
    the whole of `connect()`, or a call blocked mid-tunnel cannot be cancelled.
    That is the path an operator behind a proxy takes to the provider API.
    """

    captured: list[type] = []
    opener, cancel = recording_opener()
    handler = next(h for h in opener.handlers if type(h).__name__ == "_RecordingHTTPHandler")
    handler.do_open = lambda klass, req, **_: captured.append(klass)  # type: ignore[method-assign]
    handler.http_open(urllib.request.Request("http://127.0.0.1:1/", method="GET"))
    connection_class = captured[0]

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        connection = connection_class(f"127.0.0.1:{port}", timeout=5.0)
        # The socket exists; `connect()` has never returned, which is where a
        # tunnelling connection would be blocked.
        sock = None
        sock = connection._create_connection(("127.0.0.1", port), 5.0)
        assert cancel() == []
        # Proof it was really shut down: a read on a shut-down socket returns
        # immediately with EOF instead of blocking on a server that sends nothing.
        assert sock.recv(1) == b""
    finally:
        if sock is not None:
            sock.close()
        listener.close()
