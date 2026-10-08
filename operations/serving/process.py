"""Owned subprocess lifecycle for one vLLM server.

The protocol is small on purpose: a test launcher can implement it without a
GPU or a vLLM installation, which is what keeps the manager's whole lifecycle
drillable offline.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from .errors import ProcessLaunchError


class ServerProcess(Protocol):
    """A process handle owned by this manager, not a PID guessed from a pattern."""

    pid: int

    def poll(self) -> int | None:
        """Return an exit code once this exact child has exited."""

    def terminate(self) -> None:
        """Request graceful shutdown of this launch's process group."""

    def kill(self) -> None:
        """Force shutdown of this launch's process group."""

    def wait(self, timeout_seconds: float) -> int:
        """Wait until this launch's process group has no running member.

        Return the direct child's exit code. A member that outlives the direct
        child (vLLM's engine process holds the GPU memory) keeps the wait open.
        """

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        """Return only this launch's bounded diagnostic tail."""

    @property
    def start_marker(self) -> str | None:
        """What tells this process apart from a later one given the same pid.

        ``None`` where it cannot be read; such a process cannot be handed over.
        """


class ProcessLauncher(Protocol):
    """The process effects the manager requires: create one, or attach to one handed over."""

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> ServerProcess:
        """Launch an owned process group with only declared inherited FDs."""

    def attach(self, pid: int, start_marker: str, log_path: Path) -> ServerProcess:
        """The process group another manager launched and handed over.

        Returns a handle while ``pid`` is still the very process ``start_marker``
        names, or while its group still has a running member (an engine process
        can outlive its leader and keep the card); its ``poll`` then says whether
        the leader itself still runs. Refuses only when the whole group is gone.
        """


@dataclass(slots=True)
class PopenServerProcess:
    """A :class:`subprocess.Popen` wrapped in exact process-group operations."""

    process: subprocess.Popen[bytes]
    log_path: Path
    _log_handle: object
    # Once the leader is reaped and the group has no running member, the
    # kernel may hand this id to an unrelated group; nothing is signalled then.
    _group_gone: bool = False
    _start_marker: str | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def start_marker(self) -> str | None:
        return self._start_marker

    def poll(self) -> int | None:
        exit_code = self.process.poll()
        if exit_code is not None:
            self._close_log()
        return exit_code

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL)

    def wait(self, timeout_seconds: float) -> int:
        deadline = time.monotonic() + timeout_seconds
        try:
            exit_code = self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"owned process pid={self.pid} did not exit") from error
        finally:
            if self.process.poll() is not None:
                self._close_log()
        while _group_has_running_member(self.pid):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"process group of owned process pid={self.pid} still has a running member"
                )
            time.sleep(_GROUP_POLL_SECONDS)
        self._group_gone = True
        return exit_code

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - maximum_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except OSError as error:
            return f"VLLM_LOG_UNREADABLE: could not read launch log {self.log_path}: {error}"

    def _signal_group(self, signal_number: int) -> None:
        # start_new_session=True makes the child's pid the id of a group created
        # for this exact Popen instance, so no model name/PID-pattern search can
        # reach an unrelated service. The group is signalled whether or not the
        # direct child is still running: its other members may outlive it.
        # Only once the leader is reaped can the id be reused, so a group seen
        # empty after that is never signalled again.
        if self._group_gone:
            return
        leader_reaped = self.process.poll() is not None
        try:
            os.killpg(self.pid, signal_number)
        except ProcessLookupError:
            if leader_reaped:
                self._group_gone = True
        if leader_reaped:
            self._close_log()

    def _close_log(self) -> None:
        handle = self._log_handle
        close = getattr(handle, "close", None)
        if callable(close):
            close()
        self._log_handle = None


class SubprocessLauncher:
    """Production argv launcher with a fresh owner-private log per process."""

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> ServerProcess:
        _create_owner_only_directories(log_path.parent)
        if any(not isinstance(fd, int) or isinstance(fd, bool) or fd < 0 for fd in inheritable_fds):
            raise ProcessLaunchError("inherited file descriptors must be non-negative integers")
        handle: IO[bytes] | None = None
        try:
            # Request the owner-only mode at creation rather than open-then-chmod:
            # the latter leaves the file briefly at the umask-derived default mode
            # (commonly world/group-readable) before narrowing it.
            handle = os.fdopen(
                os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            )
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=inheritable_fds,
            )
        except (OSError, ValueError) as error:
            if handle is not None:
                with suppress(OSError):
                    handle.close()
            raise ProcessLaunchError(f"could not launch vLLM argv: {error}") from error
        return PopenServerProcess(
            process, log_path, handle, _start_marker=process_start_marker(process.pid)
        )

    def attach(self, pid: int, start_marker: str, log_path: Path) -> ServerProcess:
        observed = process_start_marker(pid)
        if observed == start_marker:
            return AttachedServerProcess(pid, start_marker, log_path)
        if observed is not None:
            # The pid now names a later process: the service is gone, and that
            # process is never signalled.
            raise ProcessLaunchError(
                f"process {pid} is no longer the service that was handed over "
                f"(start marker {start_marker!r})"
            )
        if not _group_has_running_member(pid):
            raise ProcessLaunchError(
                f"the service handed over as process {pid} has exited, and its group "
                "has no running member"
            )
        # The leader exited; a member of its group (the engine) still runs. While
        # it does, the kernel keeps the group's id from being reused.
        return AttachedServerProcess(pid, start_marker, log_path)


@dataclass(slots=True)
class AttachedServerProcess:
    """A process group launched by another manager, possibly in an exited process.

    It is not this process's child, so its exit status cannot be collected: once
    it is gone, ``poll`` answers ``-1``. Every signal first checks that ``pid``
    is still the process ``start_marker`` names, or that its group still has a
    running member, so a reused pid is never signalled.
    """

    pid: int
    _start_marker: str
    log_path: Path

    @property
    def start_marker(self) -> str | None:
        return self._start_marker

    def poll(self) -> int | None:
        return None if process_start_marker(self.pid) == self._start_marker else -1

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL)

    def wait(self, timeout_seconds: float) -> int:
        deadline = time.monotonic() + timeout_seconds
        while self.poll() is None or _group_has_running_member(self.pid):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"handed-over process group {self.pid} did not exit")
            time.sleep(_GROUP_POLL_SECONDS)
        return -1

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - maximum_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except OSError as error:
            return f"VLLM_LOG_UNREADABLE: could not read launch log {self.log_path}: {error}"

    def _signal_group(self, signal_number: int) -> None:
        # The leader may already be gone while its engine process still holds
        # the card; the group id stays reserved while any member lives.
        if self.poll() is not None and not _group_has_running_member(self.pid):
            return
        with suppress(ProcessLookupError):
            os.killpg(self.pid, signal_number)


def process_start_marker(pid: int) -> str | None:
    """The kernel's start time of ``pid`` (``/proc/<pid>/stat`` field 22), or ``None``.

    A pid can be reused once its process is reaped; the start time cannot, so
    the pair names one process. A zombie, or a host without ``/proc``, gives
    ``None``.
    """

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    # Fields after the parenthesised command name start at field 3 (state), so
    # field 22 (starttime) is index 19.
    if len(fields) < 20 or fields[0] in {"Z", "X"}:
        return None
    return fields[19]


_GROUP_POLL_SECONDS: float = 0.02


def _group_has_running_member(group_id: int) -> bool:
    """Whether any process in this group is still running.

    A zombie holds no memory and cannot be signalled away; only its parent can
    reap it, and an orphan's new parent may never do so. Where ``/proc`` exists
    zombies are therefore not counted; elsewhere any member counts.
    """

    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Fields after the parenthesised command name: state, ppid, pgrp, ...
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) >= 3 and fields[2] == str(group_id) and fields[0] not in {"Z", "X"}:
            return True
    return False


def _create_owner_only_directories(directory: Path) -> None:
    """Create only the missing path segments at 0700; leave existing ones unchanged."""

    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not path.is_dir():
                raise
            continue
        path.chmod(0o700)
