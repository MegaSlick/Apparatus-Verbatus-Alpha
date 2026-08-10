"""Direct coverage for the cross-process single-resident lease.

``test_manager.py`` exercises :class:`FileResidencyLease` only indirectly,
through a manager's full start/stop lifecycle. This file pins the one
property that lifecycle can't isolate: what ``release()`` reports, and what
state it leaves behind, when unlocking succeeds but closing the descriptor
afterward does not.
"""

from __future__ import annotations

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
