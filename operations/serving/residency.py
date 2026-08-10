"""One cross-process lease for the single-resident serving rule.

The placement table's ``residency = "single"`` is not merely a property of one
``ServingManager`` object.  A pod can construct more than one manager, so this
small OS-backed lease is held from before endpoint probing until the owned
process is both stopped and its endpoint is absent.  A failed stop deliberately
retains the lease: starting another server while the first may still own GPU
memory would recreate co-residency under a different object name.
"""

from __future__ import annotations

import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from common.chairs.models import ChairIdentity

from .errors import ResidencyError, ServiceStopError


class ResidencyHandle(Protocol):
    """The held single-resident lease, released only after verified shutdown."""

    def inheritable_fd(self) -> int:
        """Return the held lock descriptor for the owned vLLM child to inherit.

        The child retaining this open-file description means that a manager
        crash cannot silently drop the lease while its process still owns GPU
        memory.  The launcher passes this descriptor with ``pass_fds``.
        """

    def release(self) -> None:
        """Release this exact lease."""


class ResidencyLease(Protocol):
    """Acquire the pod-wide serving residency boundary for one named chair."""

    def acquire(self, identity: ChairIdentity) -> ResidencyHandle:
        """Return a held lease or refuse before any vLLM process starts."""


@dataclass(slots=True)
class _FileResidencyHandle:
    """One file descriptor whose advisory lock is this manager's ownership."""

    path: Path
    _handle: TextIO | None

    def inheritable_fd(self) -> int:
        handle = self._handle
        if handle is None:
            raise ServiceStopError(f"serving residency lease {self.path} is already released")
        try:
            descriptor = handle.fileno()
        except OSError as error:
            raise ServiceStopError(
                f"could not access serving residency lease {self.path}: {error}"
            ) from error
        if descriptor < 0:  # pragma: no cover - Python file objects do not expose this normally
            raise ServiceStopError(f"serving residency lease {self.path} has no valid descriptor")
        return descriptor

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise ServiceStopError(
                f"could not release serving residency lease {self.path}: {error}"
            ) from error
        # The OS-level lock is gone the instant LOCK_UN succeeds, independent of
        # whether closing the descriptor afterward also succeeds (flock(2)).  A
        # failed close here must not report the lease as still retained: a
        # different process is already free to acquire it.
        self._handle = None
        try:
            handle.close()
        except OSError as error:
            raise ServiceStopError(
                f"serving residency lease {self.path} was released but its descriptor "
                f"could not be closed: {error}"
            ) from error


class FileResidencyLease:
    """A non-blocking, advisory single-resident lease shared by pod managers.

    Callers must choose a path scoped to one pod/GPU, not a manager log
    directory.  No process name, PID lookup, or GPU process search is involved.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def acquire(self, identity: ChairIdentity) -> ResidencyHandle:
        del identity  # The file lock itself, not a role string, is the boundary.
        handle: TextIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            raise ResidencyError(
                f"another serving manager holds the single-resident lease {self.path}"
            ) from error
        except OSError as error:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            raise ResidencyError(
                f"could not acquire serving residency lease {self.path}: {error}"
            ) from error
        return _FileResidencyHandle(self.path, handle)
