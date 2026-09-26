"""Direct coverage for the cross-process single-resident lease.

``test_manager.py`` constructs :class:`FileResidencyLease` directly as well as
through a manager's start/stop lifecycle, and its direct uses cover acquisition
and descriptor inheritance. This file pins what those do not isolate: what
``release()`` reports, and what state it leaves behind, when unlocking succeeds
but closing the descriptor afterward does not.
"""

from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path

import pytest

from .errors import ResidencyError, ServiceStopError
from .residency import POD_RESIDENCY_LOCK_PATH, FileResidencyLease

_REPOSITORY = Path(__file__).resolve().parents[2]

# Every production caller that serves a chair on the one card a pod rents. The
# pod preflight and the three pipeline stages had disjoint lock paths: the
# preflight on container-local disk, the stages inside their own run trees, so
# preflight and run never contended, and two stages resumed under different run
# ids each acquired their own lease and co-resided on one GPU.
_SERVING_CALLERS = (
    "operations/pod/bootstrap_main.py",
    "operations/serving/assembly.py",
    "pipeline/2_designator/structure_pass.py",
    "pipeline/3_attestatores/run.py",
    "pipeline/4_perlector/run.py",
)


def test_the_pod_lease_path_is_container_local_and_not_a_run_tree_path() -> None:
    """The boundary is the card, which belongs to the pod, not to any run tree.

    A lock under `<volume>/runs/<id>/` is both scoped to the wrong thing and
    asked of a network mount whose honouring of advisory locks is unknown -- and
    it put an object inside the run tree that `fetch-run` then refused the whole
    tree for.
    """

    assert POD_RESIDENCY_LOCK_PATH.is_absolute()
    assert POD_RESIDENCY_LOCK_PATH.parent == Path("/tmp")
    assert "runs" not in POD_RESIDENCY_LOCK_PATH.parts


def test_every_serving_caller_takes_the_one_pod_wide_lease_path() -> None:
    """Read from source, so a fifth caller added later with its own literal fails here.

    The lease is only a boundary if every manager on the card opens the same
    file; three call-site literals and a fourth default were four boundaries.
    """

    for relative in _SERVING_CALLERS:
        source = (_REPOSITORY / relative).read_text(encoding="utf-8")
        assert "POD_RESIDENCY_LOCK_PATH" in source or "stage_chair_client" in source, (
            f"{relative} serves a chair but does not name the one pod-wide lease path"
        )
        if relative.startswith("pipeline/"):
            assert "ServingManager(" not in source and "ChairClient(" not in source, (
                f"{relative} builds its own manager or client instead of stage_chair_client"
            )
        # `chosen.residency_lock` is `bootstrap_main`'s injection seam, whose
        # own default is the constant; every other spelling is a second
        # boundary, and a second boundary is no boundary.
        #
        # The regex reads one line and one positional argument, so exactly two
        # spellings pass it: `FileResidencyLease(POD_RESIDENCY_LOCK_PATH)` and
        # `FileResidencyLease(chosen.residency_lock)`. A correct call the
        # formatter has wrapped across lines, or a keyword spelling
        # (`FileResidencyLease(path=POD_RESIDENCY_LOCK_PATH)`), fails here and
        # the message below will be wrong about why. That is the trade taken
        # deliberately: a source read cannot tell a renamed variable holding the
        # constant from a second lock path, and the failure is a caller to
        # respell rather than a defect to hunt. If this fires on a call that
        # does name the constant, widen the regex -- do not widen the boundary.
        assert not re.search(
            r"FileResidencyLease\((?!POD_RESIDENCY_LOCK_PATH\)|chosen\.residency_lock\))",
            source,
        ), (
            f"{relative} builds a residency lease from something other than the one "
            "pod-wide lease path"
        )
        assert "RESIDENCY_LOCK_FILE" not in source, (
            f"{relative} still resolves a lock file name against its run tree; the lease "
            "is pod-wide and container-local"
        )


def test_a_symlink_at_the_lease_path_is_refused_rather_than_followed(tmp_path: Path) -> None:
    """The one pod-wide lease is a fixed name in a world-writable directory.

    Inside the single-tenant pod container that is exactly right. On a shared
    developer machine -- which is new, because three pipeline stages now take
    the same fixed path -- another user's symlink at that name would otherwise
    be followed, and the lock taken on whatever it pointed at while the lease
    reported itself held. `O_NOFOLLOW` makes that a named refusal instead. Two
    unrelated local runs still serialize on the shared path; that is the lease
    doing its job on a machine with one card, and it refuses loudly either way.
    """

    real = tmp_path / "somebody-elses-file"
    real.write_text("not a lease", encoding="utf-8")
    link = tmp_path / "pod-gpu.lock"
    link.symlink_to(real)

    with pytest.raises(ResidencyError, match="could not acquire serving residency lease"):
        FileResidencyLease(link).acquire(identity=None)  # type: ignore[arg-type]

    assert real.read_text(encoding="utf-8") == "not a lease"


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
    real_fdopen = os.fdopen

    class TrackingHandle:
        def __init__(self, real: object) -> None:
            self._real = real

        def fileno(self) -> int:
            return self._real.fileno()  # type: ignore[attr-defined]

        def close(self) -> None:
            closed.append(True)
            self._real.close()  # type: ignore[attr-defined]

    # The seam is `os.fdopen`, not `Path.open`: the lease opens its descriptor
    # with `os.open(..., O_NOFOLLOW)` so a symlink at the one fixed lease path
    # is refused rather than followed, and wraps that descriptor afterwards.
    def _fdopen(descriptor: int, *args: object, **kwargs: object) -> object:
        return TrackingHandle(real_fdopen(descriptor, *args, **kwargs))

    def _flock(fd: int, operation: int) -> None:
        raise OSError("simulated non-contention flock failure")

    monkeypatch.setattr(os, "fdopen", _fdopen)
    monkeypatch.setattr(fcntl, "flock", _flock)

    path = tmp_path / "pod-gpu.lock"
    with pytest.raises(ResidencyError, match="could not acquire serving residency lease"):
        FileResidencyLease(path).acquire(identity=None)  # type: ignore[arg-type]

    assert closed == [True]


def test_acquire_reports_a_close_failure_inside_the_refusal_it_was_already_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close() that raises during acquire's cleanup may not replace the refusal — or vanish.

    The contention refusal stays the primary error, and the leaked-descriptor
    detail rides inside its message instead of being silently discarded.
    """

    class ExplodingCloseHandle:
        def __init__(self, real: object) -> None:
            self._real = real

        def fileno(self) -> int:
            return self._real.fileno()  # type: ignore[attr-defined]

        def close(self) -> None:
            raise OSError("simulated close failure during acquire cleanup")

    real_fdopen = os.fdopen

    # As above, the seam is `os.fdopen`: the lease opens with `os.open` and
    # `O_NOFOLLOW`, then wraps the descriptor it got.
    def _fdopen(descriptor: int, *args: object, **kwargs: object) -> object:
        return ExplodingCloseHandle(real_fdopen(descriptor, *args, **kwargs))

    def _flock(fd: int, operation: int) -> None:
        raise BlockingIOError("simulated contention")

    monkeypatch.setattr(os, "fdopen", _fdopen)
    monkeypatch.setattr(fcntl, "flock", _flock)

    with pytest.raises(ResidencyError, match="another serving manager holds") as refused:
        FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(identity=None)  # type: ignore[arg-type]

    assert "additionally its descriptor could not be closed" in str(refused.value)
    assert "simulated close failure during acquire cleanup" in str(refused.value)
