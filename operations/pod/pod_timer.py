"""Provider-neutral pod-side dead-man entrypoint.

Provider-specific capability construction belongs in ``provider_<name>.py``.
This module only supervises a generic timer context, makes bootstrap mandatory,
and durably writes both bootstrap and close evidence to the attached volume.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .controllers import ControllerResult, PodDeadmanTimer
from .durable import atomic_write, canonical_json


@dataclass(frozen=True, slots=True)
class TimerContext:
    """A generic timer already supplied with its provider-neutral close path."""

    timer: PodDeadmanTimer


def load_timer_context(reference: str) -> TimerContext:
    """Load the explicitly named provider-owned timer factory without defaults."""

    if reference.count(":") != 1:
        raise RuntimeError("pod timer factory must be module:callable")
    module_name, callable_name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module_name), callable_name)
    context = factory()
    if not isinstance(context, TimerContext):
        raise RuntimeError("pod timer factory did not return a TimerContext")
    return context


def run_with_bootstrap(
    context: TimerContext,
    *,
    bootstrap_command_json: str,
    report_path: str | Path,
    sleeper: Callable[[float], None] = time.sleep,
    interval_seconds: float = 15.0,
    popen: Callable[[list[str]], subprocess.Popen[bytes]] = subprocess.Popen,
) -> ControllerResult:
    """Keep the timer primary while monitoring mandatory bootstrap and close evidence."""

    if interval_seconds <= 0:
        raise ValueError("pod timer interval must be positive")
    command = _bootstrap_argv(bootstrap_command_json)
    bootstrap_record: dict[str, object] = {"argv": command, "state": "running"}
    report = Path(report_path)
    try:
        child = popen(command)
    except Exception as error:
        bootstrap_record = {
            "argv": command,
            "state": "failed-to-start",
            "remediation": "Repair the mandatory bootstrap command before another authorized run.",
        }
        _durable_failure_close(
            context, report, bootstrap_record, error, "mandatory bootstrap process failed to start"
        )
    _persist_or_close(
        context, report, {"bootstrap": bootstrap_record, "close": None, "green": False}
    )
    while context.timer.now() < context.timer.lease.hard_deadline:
        exit_code = child.poll()
        if exit_code is not None:
            if exit_code != 0:
                bootstrap_record = {
                    "argv": command,
                    "state": "failed",
                    "exit_code": exit_code,
                    "remediation": "Inspect the bootstrap report and repair it before another authorized run.",
                }
                result = context.timer.close_now("mandatory bootstrap child failed")
                _persist_or_close(
                    context,
                    report,
                    {
                        "bootstrap": bootstrap_record,
                        "close": result.close_report.to_record() if result.close_report else None,
                        "green": False,
                    },
                )
                return result
            if bootstrap_record["state"] == "running":
                bootstrap_record = {
                    "argv": command,
                    "state": "completed-early",
                    "exit_code": 0,
                    "remediation": (
                        "Use a long-running bootstrap/service entrypoint; the pod was closed to avoid idle spend."
                    ),
                }
                result = context.timer.close_now(
                    "mandatory bootstrap child exited before hard deadline"
                )
                _persist_or_close(
                    context,
                    report,
                    {
                        "bootstrap": bootstrap_record,
                        "close": result.close_report.to_record() if result.close_report else None,
                        "green": False,
                    },
                )
                return result
        remaining = (context.timer.lease.hard_deadline - context.timer.now()).total_seconds()
        sleeper(min(interval_seconds, max(0.01, remaining)))
    result = context.timer.run_once()
    _persist_or_close(
        context,
        report,
        {
            "bootstrap": bootstrap_record,
            "close": result.close_report.to_record() if result.close_report else None,
            "green": bool(result.close_report and result.close_report.verified),
        },
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verbatus provider-neutral pod hard-lifetime dead-man"
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument(
        "--timer-factory", required=True, help="provider module:callable yielding TimerContext"
    )
    parser.add_argument(
        "--bootstrap-command-json", required=True, help="mandatory bootstrap JSON argv"
    )
    parser.add_argument(
        "--report-path", required=True, help="durable report path on the attached volume"
    )
    args = parser.parse_args(argv)
    result = run_with_bootstrap(
        load_timer_context(args.timer_factory),
        bootstrap_command_json=args.bootstrap_command_json,
        report_path=args.report_path,
        interval_seconds=args.interval_seconds,
    )
    # Verification rather than process exit decides whether the pod-side close is green.
    return 0 if result.close_report is not None and result.close_report.verified else 2


def _bootstrap_argv(value: str) -> list[str]:
    """Accept only structured argv; bootstrap never reaches a shell interpolator."""

    try:
        argv = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError("pod dead-man bootstrap command is not JSON argv") from error
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(part, str) and part for part in argv)
    ):
        raise RuntimeError("pod dead-man bootstrap command must be a non-empty string argv")
    return argv


def _write_report(path: Path, value: dict[str, object]) -> None:
    """Retain pod bootstrap/close evidence on the attached volume."""

    atomic_write(path, canonical_json(value))


def _persist_or_close(context: TimerContext, path: Path, value: dict[str, object]) -> None:
    """A missing durable report is an immediate-close condition, never green."""

    try:
        _write_report(path, value)
    except Exception as error:
        bootstrap = value.get("bootstrap")
        if not isinstance(bootstrap, dict):
            # Not an assert: `assert` disappears under `python -O`, and a
            # non-dict would then reach `{**bootstrap}` in the close below and
            # raise TypeError instead of filing the fallback receipt.  Every
            # call site in this module passes a dict, so this is a placeholder
            # rather than a reconstruction -- it says the payload was unusable,
            # it does not invent a bootstrap state that was never observed.
            bootstrap = {"state": "unrecorded", "detail": "report payload carried no bootstrap"}
        _durable_failure_close(context, path, bootstrap, error, "mandatory pod report write failed")


def _durable_failure_close(
    context: TimerContext,
    path: Path,
    bootstrap: dict[str, object],
    error: Exception,
    label: str,
) -> None:
    """Attempt verified close even when the retained-volume receipt is unavailable.

    ``label`` names the failure that triggered this close (bootstrap failed to
    start, or the durable report write itself failed) and is used both as the
    close reason and the fallback report's error key, so the record filed on
    the volume says what actually happened rather than always blaming the
    write.

    Whether the fallback receipt itself reached the volume is carried in the
    raised error too.  It used to be swallowed, so an operator finding no
    receipt could not tell a write that failed twice from one that never ran --
    GOVERNANCE 2, on the only durable evidence this pod leaves behind.
    """

    result = context.timer.close_now(label)
    fallback = {
        "bootstrap": {**bootstrap, "failure_detail": str(error)},
        "close": result.close_report.to_record() if result.close_report else None,
        "green": False,
    }
    receipt = "fallback receipt was written"
    try:
        _write_report(path, fallback)
    except Exception as write_error:  # reported below, never swallowed
        receipt = f"fallback receipt also failed: {write_error}"
    state = result.close_report.state.value if result.close_report else "shutdown-exception"
    raise RuntimeError(
        f"{label} ({error}); immediate close result is {state}, never green; {receipt}"
    ) from error


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
