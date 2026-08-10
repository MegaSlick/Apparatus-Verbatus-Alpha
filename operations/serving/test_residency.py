"""Direct coverage for the cross-process single-resident lease.

``test_manager.py`` exercises :class:`FileResidencyLease` only indirectly,
through a manager's full start/stop lifecycle. This file pins the one
property that lifecycle can't isolate: what ``release()`` reports, and what
state it leaves behind, when unlocking succeeds but closing the descriptor
afterward does not.
"""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from .errors import ResidencyError, ServiceStopError
from .residency import FileResidencyLease


def test_acquire_is_non_blocking_and_refuses_a_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "pod-gpu.lock"
    lease_a = FileResidencyLease(path)
    lease_b = FileResidencyLease(path)

    held = lease_a.acquire(identity=None)  # type: ignore[arg-type]
    try:
        with pytest.raises(ResidencyError, match="another serving manager holds"):
            lease_b.acquire(identity=None)  # type: ignore[arg-type]
    finally:
        held.release()

    # Released: a second acquire now succeeds and is itself releasable.
    lease_b.acquire(identity=None).release()  # type: ignore[arg-type]


def test_release_clears_held_state_even_when_closing_the_descriptor_fails(
    tmp_path: Path,
) -> None:
    """A successful unlock must not be reported as a still-retained lease.

    ``flock(LOCK_UN)`` releases the OS-level lock the instant it succeeds,
    independent of whether closing the descriptor afterward also succeeds. If
    ``release()`` reported the lease as retained anyway, this manager would
    refuse its own next start while a different process could legitimately
    (and correctly) already treat the lease as free.
    """

    handle = FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(identity=None)  # type: ignore[arg-type]
    real_file = handle._handle  # type: ignore[union-attr]
    original_close = real_file.close

    def _explode() -> None:
        raise OSError("simulated close failure after a successful unlock")

    real_file.close = _explode  # type: ignore[method-assign]

    with pytest.raises(ServiceStopError, match="released but its descriptor could not be closed"):
        handle.release()

    # The lease itself is gone even though closing the fd raised: a fresh
    # acquire on the same path must now succeed rather than see it held.
    second = FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(identity=None)  # type: ignore[arg-type]
    second._handle.close()  # type: ignore[union-attr]

    # release() is also safe to call again on the original handle: its
    # internal state is already cleared, so a caller retrying cleanup after
    # the raised error does not raise a second time.
    handle.release()

    # Close the real descriptor through the original method, bypassing the
    # monkeypatch, so nothing is left open at the end of the test.
    original_close()


def test_release_reports_the_unlock_itself_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``LOCK_UN`` must raise, and must not clear the held state.

    Unlike a close failure *after* a successful unlock, the OS-level lock is
    not known to be gone here, so the conservative behaviour is to leave
    ``_handle`` set: a caller retrying ``release()`` will try to unlock again
    rather than silently treating the lease as already free.
    """

    handle = FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(identity=None)  # type: ignore[arg-type]
    real_flock = fcntl.flock

    def _flock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("simulated unlock failure")
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", _flock)

    with pytest.raises(ServiceStopError, match="could not release serving residency lease"):
        handle.release()

    assert handle._handle is not None  # type: ignore[union-attr]

    monkeypatch.undo()
    handle.release()


def test_acquire_closes_the_handle_and_refuses_on_a_plain_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-contention OS failure during acquire is a named refusal too, not just contention.

    The handle opened before the failing ``flock`` call must be closed as part
    of that same failure, not leaked.
    """

    closed: list[bool] = []
    real_open = Path.open

    class TrackingHandle:
        def __init__(self, real: object) -> None:
            self._real = real

        def fileno(self) -> int:
            return self._real.fileno()  # type: ignore[attr-defined]

        def close(self) -> None:
            closed.append(True)
            self._real.close()  # type: ignore[attr-defined]

    def _open(self: Path, *args: object, **kwargs: object) -> object:
        return TrackingHandle(real_open(self, *args, **kwargs))

    def _flock(fd: int, operation: int) -> None:
        raise OSError("simulated non-contention flock failure")

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(fcntl, "flock", _flock)

    path = tmp_path / "pod-gpu.lock"
    with pytest.raises(ResidencyError, match="could not acquire serving residency lease"):
        FileResidencyLease(path).acquire(identity=None)  # type: ignore[arg-type]

    assert closed == [True]


def test_acquire_swallows_a_close_failure_during_its_own_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close() that itself raises during acquire's cleanup must not replace the real refusal."""

    class ExplodingCloseHandle:
        def __init__(self, real: object) -> None:
            self._real = real

        def fileno(self) -> int:
            return self._real.fileno()  # type: ignore[attr-defined]

        def close(self) -> None:
            raise OSError("simulated close failure during acquire cleanup")

    real_open = Path.open

    def _open(self: Path, *args: object, **kwargs: object) -> object:
        return ExplodingCloseHandle(real_open(self, *args, **kwargs))

    def _flock(fd: int, operation: int) -> None:
        raise BlockingIOError("simulated contention")

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(fcntl, "flock", _flock)

    with pytest.raises(ResidencyError, match="another serving manager holds"):
        FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(identity=None)  # type: ignore[arg-type]
