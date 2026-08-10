"""Owned subprocess lifecycle for one vLLM server.

This module deliberately starts no process at import time.  The real launcher
uses an argv vector and a fresh log; test launchers can implement the small
protocol without a GPU or vLLM installation.
"""

from __future__ import annotations

import os
import signal
import subprocess
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
        """Wait for this exact child and return its exit code."""

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        """Return only this launch's bounded diagnostic tail."""


class ProcessLauncher(Protocol):
    """The one process-creation effect the manager requires."""

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> ServerProcess:
        """Launch an owned process group with only declared inherited FDs."""


@dataclass(slots=True)
class PopenServerProcess:
    """A :class:`subprocess.Popen` wrapped in exact process-group operations."""

    process: subprocess.Popen[bytes]
    log_path: Path
    _log_handle: object

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL)

    def wait(self, timeout_seconds: float) -> int:
        try:
            return self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"owned process pid={self.pid} did not exit") from error
        finally:
            if self.process.poll() is not None:
                self._close_log()

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - maximum_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _signal_group(self, signal_number: int) -> None:
        if self.process.poll() is not None:
            self._close_log()
            return
        try:
            # start_new_session=True makes this a group created for this exact
            # Popen instance.  No model name/PID-pattern search can reach an
            # unrelated service.
            os.killpg(os.getpgid(self.process.pid), signal_number)
        except ProcessLookupError:
            pass

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
        log_path.parent.mkdir(parents=True, exist_ok=True)
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
        return PopenServerProcess(process, log_path, handle)
