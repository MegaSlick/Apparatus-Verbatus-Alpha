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
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TextIO

from common.chairs.models import ChairIdentity

from .errors import ResidencyError, ServiceStopError

# Shared by every serving caller on one pod (preflight and each pipeline
# stage). Container-local, not on the network volume: an advisory lock there
# is not guaranteed honoured. The boundary is the GPU card, not a run tree:
# every caller on the pod must contend on this one container-local file, or
# two vLLM servers could share one GPU with no refusal. A caller wanting a different
# boundary passes its own path to `FileResidencyLease` instead.
POD_RESIDENCY_LOCK_PATH: Final = Path("/tmp/verbatus-pod-gpu.lock")
# Beside the lease: the record a manager leaves when it hands its running
# service to the next stage's process (`ServingManager.hand_off`). Like the
# lease it is about the card, not a run tree.
POD_HAND_OFF_PATH: Final = Path("/tmp/verbatus-pod-gpu.hand-off.json")


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

    def relinquish(self) -> None:
        """Close this manager's descriptor without unlocking.

        The owned child inherited the same open-file description, so the lock
        stays held for as long as that process lives. Used only when the
        running service is handed to another process, which stops the service
        and leaves the lease to the processes still holding it.
        """


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
            raise ResidencyError(f"serving residency lease {self.path} is already released")
        try:
            descriptor = handle.fileno()
        except OSError as error:
            raise ResidencyError(
                f"could not access serving residency lease {self.path}: {error}"
            ) from error
        if descriptor < 0:  # pragma: no cover - Python file objects do not expose this normally
            raise ResidencyError(f"serving residency lease {self.path} has no valid descriptor")
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
        # flock(2): the lock is gone the instant LOCK_UN succeeds, so a later
        # close failure must not report the lease as still held.
        self._handle = None
        try:
            handle.close()
        except OSError as error:
            raise ServiceStopError(
                f"serving residency lease {self.path} was released but its descriptor "
                f"could not be closed: {error}"
            ) from error

    def relinquish(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            # No LOCK_UN: that would unlock the description the child shares.
            _close_quietly(handle)


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
            # O_NOFOLLOW: the fixed lease name sits in a world-writable
            # directory, so on a shared (non-pod) machine another user's
            # symlink there must not be followed and locked; the OSError arm
            # below refuses it the same way it refuses a denied file.
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            try:
                handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            except BaseException:
                os.close(descriptor)
                raise
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            close_failure = _close_quietly(handle)
            holders = lease_holders(self.path)
            raise ResidencyError(
                f"another serving manager holds the single-resident lease {self.path}"
                + (f"; held by {'; '.join(holders)}" if holders else "")
                + close_failure
            ) from error
        except OSError as error:
            close_failure = _close_quietly(handle)
            raise ResidencyError(
                f"could not acquire serving residency lease {self.path}: {error}" + close_failure
            ) from error
        return _FileResidencyHandle(self.path, handle)


def lease_holders(path: Path) -> tuple[str, ...]:
    """The processes holding the lease's lock, named for a refusal; empty without ``/proc``.

    A process counts when one of its descriptors is the lease file and the
    kernel lists the lock on it (``/proc/<pid>/fdinfo``), so a process that only
    has the file open is left out. Each is named by its pid, the parent, group
    and session it is in, its state and its command name: enough to tell which
    process kept the card leased, and nothing it was working on.
    """

    try:
        lease = os.stat(path)
    except OSError:
        return ()
    proc = Path("/proc")
    if not proc.is_dir():
        return ()
    named: list[str] = []
    for entry in sorted(proc.iterdir(), key=lambda item: item.name.zfill(10)):
        if not entry.name.isdigit():
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                opened = os.stat(descriptor)
                if (opened.st_dev, opened.st_ino) != (lease.st_dev, lease.st_ino):
                    continue
                info = (entry / "fdinfo" / descriptor.name).read_text(encoding="utf-8")
            except OSError:
                continue
            if "FLOCK" not in info:
                continue
            named.append(_describe_process(entry))
            break
    return tuple(named)


def _describe_process(entry: Path) -> str:
    try:
        stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"pid {entry.name}"
    command = stat[stat.find("(") + 1 : stat.rfind(")")]
    # Fields after the parenthesised command name: state, ppid, pgrp, session.
    fields = stat.rsplit(")", 1)[-1].split()
    if len(fields) < 4:
        return f"pid {entry.name} ({command})"
    state, ppid, group, session = fields[:4]
    return (
        f"pid {entry.name} ({command}, state {state}, parent {ppid}, "
        f"group {group}, session {session})"
    )


def _close_quietly(handle: TextIO | None) -> str:
    """Close a partially acquired lease descriptor, reporting rather than hiding failure.

    The acquisition refusal is already on its way; a close failure may not
    replace it, but silently discarding it would hide a leaked descriptor.
    """

    if handle is None:
        return ""
    try:
        handle.close()
    except OSError as error:
        return f"; additionally its descriptor could not be closed: {error}"
    return ""
