"""Keep a bounded window of calls in flight and finish their jobs in input order."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def in_order_window(width: int, jobs) -> list[Any]:
    """Run jobs' calls with at most `width` jobs unfinished; finish every job in order here.

    A job is `(call, finish)`, and `call` is `None` for a job that only publishes. Jobs
    are drawn lazily, so the next is prepared, and its deadline checked, only once fewer
    than `width` are unfinished. `finish` runs on this thread, strictly in job order,
    with what `call` returned (`None` without a call), so records are published in the
    order a serial pass publishes them. Width 1 calls inline, with no thread.

    Every job drawn is finished: if drawing a job, a call or a finish raises, the jobs
    already drawn are still finished in order, so no reply is left without its record,
    and then the first error re-raises with any later ones attached as notes. An
    interrupt stops waiting at once, so the chair can be stopped: it first finishes
    every job whose reply has already arrived, and leaves the calls still out
    unfinished. A reply that arrived is recorded even when an earlier call is still
    out; a record's bytes do not depend on the order it was written in.
    """
    finished: list[Any] = []
    if width == 1:
        for call, finish in jobs:
            finished.append(finish(call() if call is not None else None))
        return finished
    pool = ThreadPoolExecutor(max_workers=width)
    window: deque = deque()

    def finish_first() -> None:
        future, finish = window.popleft()
        finished.append(finish(future.result() if future is not None else None))

    error: Exception | None = None
    try:
        try:
            for call, finish in jobs:
                window.append((pool.submit(call) if call is not None else None, finish))
                while window and (len(window) == width or window[0][0] is None):
                    finish_first()
        except Exception as raised:
            error = raised
        while window:
            try:
                finish_first()
            except Exception as raised:
                if error is None:
                    error = raised
                else:
                    error.add_note(f"a later job also failed: {type(raised).__name__}: {raised}")
    except BaseException as interrupt:
        for future, finish in window:
            if future is not None and (
                not future.done() or future.cancelled() or future.exception() is not None
            ):
                continue
            try:
                finish(future.result() if future is not None else None)
            except Exception as raised:
                interrupt.add_note(f"an arrived reply was not recorded: {raised}")
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if error is not None:
        raise error
    return finished
