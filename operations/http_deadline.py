"""One monotonic deadline over a whole blocking HTTP call.

A socket timeout bounds *one* blocking receive, never the call.  A responder
that delivers a byte just inside it holds the request open for as long as it
likes, and both loops built on these transports — the readiness watchdog and
the shutdown absence poll — consult their own deadline only *between* requests.
Measured at 36acde636f against a 0.15 s configured timeout: a loopback provider
response dribbling its body one byte at a time returned HTTP 200 after 8.559 s,
and a response dribbling its *headers* returned 200 after 1.273 s.  The serving
transport bounded its body and so returned in 0.220 s, but its headers escaped
that bound and took 1.280 s.  On a card that bills by the hour, a bound the
whole design rests on was not a bound.

**The mechanism is a worker thread joined with the remaining budget, and that is
a decision rather than a preference.**  The alternative — re-arming the socket
deadline before each read — cannot work here, for two independent reasons.  It
bounds each individual read, which is the defect itself: a dribbler resets it
every read and the total is unbounded.  And the header phase happens inside
``opener.open()``, several frames below anything this repository can re-arm.  A
join with the remaining budget returns control to the caller at the deadline
whatever the I/O is doing, which is the property both loops need.

**Cancellation is what keeps that from leaking workers.**  Giving up on a thread
without stopping it would leave one abandoned socket per expired call, and the
readiness poll makes such a call every interval.  So the connection's socket is
recorded as it is made and, at the deadline, shut down from the controlling
thread: a blocked ``recv`` returns immediately and the worker unwinds through
its own error path.  The join after cancellation says whether it did, and a
worker that still did not unwind is *named in the refusal* rather than passed
over — a hung worker is a fact an operator reading a failed close needs.

The one case cancellation cannot reach is a TCP connect that has not completed,
because there is no socket to shut down yet.  That case is bounded by the socket
timeout itself, which every caller here sets to no more than the same budget.

This module holds no provider, model-host, or endpoint behavior.  It is the
shared primitive under ``operations/serving/http.py`` and
``operations/pod/provider_runpod.py``, which had this protection in different
strengths and in different places.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.request
from typing import Callable, TypeVar

_T = TypeVar("_T")

# How long a cancelled worker is given to unwind after its socket is shut down.
# Short deliberately: this is spent inside a call that has *already* overrun its
# budget, and the refusal reports honestly whether the worker was still running.
CANCEL_GRACE_SECONDS = 2.0


class DeadlineExceeded(Exception):
    """One HTTP call did not finish within its whole-call budget.

    Carries the measured elapsed time as well as the budget, because the two
    differing is itself the diagnostic: a call that gave up at its budget was
    cancelled cleanly, and one that took materially longer spent the difference
    waiting for a worker that would not unwind.  ``cancel_failures`` carries what
    breaking the sockets reported, if anything — a worker still running and a
    shutdown that failed are one story, and each half alone is half of it.
    """

    def __init__(
        self,
        label: str,
        *,
        budget_seconds: float,
        elapsed_seconds: float,
        worker_still_running: bool,
        cancel_failures: tuple[str, ...] = (),
        cancel_attempted: bool = True,
    ) -> None:
        self.label = label
        self.cancel_attempted = cancel_attempted
        self.budget_seconds = budget_seconds
        self.elapsed_seconds = elapsed_seconds
        self.worker_still_running = worker_still_running
        self.cancel_failures = cancel_failures
        detail = (
            f"{label} did not complete within its {budget_seconds:g}s deadline "
            f"(abandoned after {elapsed_seconds:.3f}s)"
        )
        if worker_still_running:
            # Only a call that had a socket to break can say one was shut down; a
            # caller that passed a no-op cancel (the shutdown controller bounding
            # an injected provider seam) merely abandoned the thread.
            detail += (
                "; its request thread had not unwound when the socket was shut down"
                if cancel_attempted
                else "; its request thread was abandoned still running"
            )
        if cancel_failures:
            detail += f"; cancelling it reported {'; '.join(cancel_failures)}"
        super().__init__(detail)


def recording_opener(
    *handlers: urllib.request.BaseHandler,
) -> tuple[urllib.request.OpenerDirector, Callable[[], None]]:
    """One opener whose in-flight socket can be broken from another thread.

    Built per call rather than once per transport: the returned ``cancel`` owns
    exactly the sockets this one call made, so an expiring call can never shut
    down a socket belonging to a concurrent one.  ``build_opener`` costs
    microseconds and these callers make a request every couple of seconds at
    most.
    """

    sockets: list[socket.socket] = []
    lock = threading.Lock()

    def record(sock: socket.socket | None) -> None:
        if sock is None:  # pragma: no cover - connect() always leaves a socket
            return
        with lock:
            sockets.append(sock)

    class _RecordingConnection:
        """Record every socket this connection owns, at each stage it owns one.

        ``connect()`` alone is not enough, and the gap is on the path an
        operator behind a proxy actually uses.  ``HTTPConnection.connect``
        opens the socket, then — for HTTPS through a proxy — blocks reading the
        ``CONNECT`` tunnel response, then wraps the result in TLS.  Recording
        only after it returns leaves the tunnel read uncancellable, and TLS
        wrapping *detaches* the original socket, so the pre-wrap object cannot
        break a blocked TLS read either.  Both are recorded: the raw socket the
        moment `_create_connection` produces it, and whatever `self.sock` is
        once `connect()` is done.  `cancel` tries each and tolerates the
        detached one failing.
        """

        def __init__(self, *arguments: object, **keywords: object) -> None:
            super().__init__(*arguments, **keywords)  # type: ignore[call-arg]
            # An instance attribute in `HTTPConnection.__init__`, not a method,
            # so it is wrapped here rather than overridden on the class.
            opening = self._create_connection  # type: ignore[attr-defined]

            def creating(*arguments: object, **keywords: object) -> socket.socket:
                sock = opening(*arguments, **keywords)
                record(sock)
                return sock

            self._create_connection = creating  # type: ignore[attr-defined]

        def connect(self) -> None:
            super().connect()  # type: ignore[misc]
            record(self.sock)  # type: ignore[attr-defined]

    class _RecordingHTTPConnection(_RecordingConnection, http.client.HTTPConnection):
        pass

    class _RecordingHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # type: ignore[no-untyped-def]  # noqa: D102
            return self.do_open(_RecordingHTTPConnection, req)

    built: list[urllib.request.BaseHandler] = [_RecordingHTTPHandler()]

    if hasattr(http.client, "HTTPSConnection"):

        class _RecordingHTTPSConnection(_RecordingConnection, http.client.HTTPSConnection):
            pass

        class _RecordingHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):  # type: ignore[no-untyped-def]  # noqa: D102
                # Forward the handler's own TLS context, exactly as the stdlib's
                # `https_open` does. `getattr` rather than an attribute: this
                # reaches into a private name, and if a future CPython renames
                # it the fallback is `HTTPSConnection`'s own default context —
                # verified, hostname-checked — never an unverified connection.
                # `test_http_deadline.py` fails loudly on the rename either way.
                context = getattr(self, "_context", None)
                keywords = {} if context is None else {"context": context}
                return self.do_open(_RecordingHTTPSConnection, req, **keywords)

        built.append(_RecordingHTTPSHandler())

    def cancel() -> list[str]:
        failures: list[str] = []
        broke_one = False
        with lock:
            current = list(sockets)
        for sock in current:
            try:
                # `shutdown`, not `close`: closing only drops this reference,
                # and a peer file object made by `makefile` keeps the descriptor
                # alive, so the blocked `recv` in the worker would not return.
                # `SHUT_RDWR` is what makes it return at once.
                sock.shutdown(socket.SHUT_RDWR)
                broke_one = True
            except OSError as error:
                # Ordinarily benign — already closed, never connected, detached
                # by TLS wrapping, or torn down by the worker between the join
                # expiring and here — so a failure is only reported when *no*
                # socket could be broken at all. A shutdown that failed for some
                # other reason is exactly why a worker would then be found still
                # running, and the refusal should be able to say so.
                failures.append(f"{type(error).__name__}: {error}")
        return [] if broke_one else failures

    return urllib.request.build_opener(*built, *handlers), cancel


def call_within_deadline(
    work: Callable[[], _T],
    *,
    budget_seconds: float,
    label: str,
    cancel: Callable[[], object],
    monotonic: Callable[[], float] = time.monotonic,
) -> _T:
    """Run ``work`` on one worker thread and refuse when its budget runs out.

    ``work``'s own exception is re-raised in the calling thread with its
    traceback intact, so every caller's existing classification still sees the
    exception it classified before.  Only the deadline itself is new, and it
    arrives as :class:`DeadlineExceeded`.
    """

    if budget_seconds <= 0:
        raise DeadlineExceeded(
            label, budget_seconds=budget_seconds, elapsed_seconds=0.0, worker_still_running=False
        )

    outcome: dict[str, object] = {}

    def runner() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - relayed verbatim below
            outcome["error"] = error

    thread = threading.Thread(target=runner, name=f"http-deadline-{label}"[:60], daemon=True)
    started = monotonic()
    thread.start()
    thread.join(budget_seconds)
    if thread.is_alive():
        # A cancel may report why it could not break its sockets; `None` from a
        # seam that has none is not a failure.
        cancel_result = cancel()
        cancel_failures = cancel_result or ()
        thread.join(CANCEL_GRACE_SECONDS)
        raise DeadlineExceeded(
            label,
            budget_seconds=budget_seconds,
            elapsed_seconds=monotonic() - started,
            worker_still_running=thread.is_alive(),
            cancel_failures=tuple(str(failure) for failure in cancel_failures),
            # A seam with no sockets returns None: nothing was shut down.
            cancel_attempted=cancel_result is not None,
        )
    error = outcome.get("error")
    if error is not None:
        assert isinstance(error, BaseException)
        raise error
    return outcome["value"]  # type: ignore[return-value]
