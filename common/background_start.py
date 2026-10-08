"""Start a stage's chair on a background thread while its main thread prepares work.

A chair's start (verifying the snapshot, launching the engine, waiting for it to
answer) takes minutes; a stage that prepares its requests meanwhile overlaps the two.
The start writes only its serving receipt and launch audit; every record that names
the receipt is published on the main thread after `join`.
"""

import sys
import threading
import time
from collections.abc import Callable


class BackgroundStart:
    """One chair start running on its own thread.

    `start` starts the chair; `stop` stops it. A failed start is raised by `join`,
    on the main thread. A stage that stops while the chair is still starting does
    not wait for it: `abandon` hands the chair to this thread, which calls `stop`
    as soon as the start returns. The thread is not a daemon, so the process does
    not exit, leaving an engine running, before that stop.
    """

    def __init__(self, start: Callable[[], None], stop: Callable[[], None], *, stage: str):
        self._start_chair = start
        self._stop_chair = stop
        self._stage = stage
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._finished = False
        self._abandoned = False
        self._began = time.monotonic()
        self._thread = threading.Thread(target=self._start, name="chair-start", daemon=False)
        self._thread.start()

    def _start(self) -> None:
        try:
            self._start_chair()
        except BaseException as error:  # noqa: BLE001 -- raised again by `join`
            self._error = error
        with self._lock:
            self._finished = True
            abandoned = self._abandoned
        if abandoned:
            try:
                self._stop_chair()
            except BaseException as error:  # noqa: BLE001 -- nobody is left to raise to
                print(
                    f"{self._stage}: the chair started for a stopped pass did not stop "
                    f"cleanly: {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self._began)

    def join(self) -> None:
        """Wait for the start; raise its failure here, once."""
        self._thread.join()
        error, self._error = self._error, None
        if error is not None:
            raise error

    def abandon(self) -> bool:
        """Whether the chair was still starting, and is now this thread's to stop.

        `False` once the start has returned: the caller owns the chair, as after `join`.
        """
        with self._lock:
            if self._finished:
                return False
            self._abandoned = True
            return True

    def note_failure(self, raised: BaseException) -> None:
        """Attach a start that already failed to `raised`, which propagates instead."""
        with self._lock:
            error = self._error if self._finished else None
        if error is not None:
            self._error = None
            raised.add_note(f"the chair's start also failed: {type(error).__name__}: {error}")
