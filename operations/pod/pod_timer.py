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
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .controllers import ControllerResult, ControllerState, PodDeadmanTimer
from .durable import atomic_write, canonical_json
from .models import POD_REPORT_SCHEMA, require_utc

_CLOSE_ATTEMPTS = 3
"""Bounded re-attempts of a non-green close before the timer exits.

GOVERNANCE 11 forbids an unbounded reconsideration loop, and staying alive to
retry bills the full running-pod rate against the cheaper EXITED state the exit
falls back to -- so the bound is small, the wait is the monitoring interval
capped at `_MAX_CLOSE_RETRY_WAIT_SECONDS`, and every attempt count reaches the
durable report."""


class PodTimerFailureClosed(RuntimeError):
    """Raised after a startup failure has already spent an immediate close."""


def _stamp(value: datetime) -> str:
    return require_utc(value, "pod timer timestamp").isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class TimerContext:
    """A generic timer already supplied with its provider-neutral close path.

    ``identity`` and ``acknowledged_at`` are captured once, in ``__post_init__``
    -- the moment this timer's provider-backed close capability is wrapped and
    first exists -- so every report a run writes afterward carries the same
    lease/pod/deadline binding and the same acknowledgement instant, rather
    than a fresh guess recomputed at each write.  ``acknowledged_at`` is
    stamped from ``timer.now()``, the same injected clock the timer itself
    uses to decide expiry, never from wall-clock time: a report claiming an
    acknowledgement moment nobody's clock measured is exactly what
    GOVERNANCE 10 forbids.
    """

    timer: PodDeadmanTimer
    identity: Mapping[str, object] = field(init=False)
    acknowledged_at: str = field(init=False)

    def __post_init__(self) -> None:
        lease = self.timer.lease
        object.__setattr__(
            self,
            "identity",
            {
                "lease_id": lease.lease_id,
                "pod_id": lease.pod_id,
                "hard_deadline": _stamp(lease.hard_deadline),
            },
        )
        object.__setattr__(self, "acknowledged_at", _stamp(self.timer.now()))


_MAX_CLOSE_RETRY_WAIT_SECONDS = 60.0
"""The retry wait is the monitoring interval, capped: the interval is an
operator-supplied command-line value with no upper bound, and a pod at this
point bills the full running rate for every second the timer sleeps."""


def _close_with_retries(
    context: TimerContext,
    reason: str,
    sleeper: Callable[[float], None],
    wait_seconds: float,
    attempts: int = _CLOSE_ATTEMPTS,
) -> tuple[ControllerResult, int]:
    """Re-attempt a non-green close a fixed, recorded number of times.

    One close already re-issues termination for its whole polling window; this
    re-enters after that window has closed red, so a transient provider fault
    at the exact deadline is not the last word before the timer exits.  A
    result no retry can improve -- a lease that never bound a pod id -- exits
    the loop at once instead of sleeping on it.
    """

    unimprovable = {
        ControllerState.PENDING_CREATE_REVIEW,
        ControllerState.LEASE_RECORD_FAILURE,
    }
    result = context.timer.close_now(reason)
    tried = 1
    while tried < attempts and not (
        result.close_report is not None and result.close_report.verified
    ):
        if result.state in unimprovable:
            break
        sleeper(max(0.01, min(wait_seconds, _MAX_CLOSE_RETRY_WAIT_SECONDS)))
        result = context.timer.close_now(reason)
        tried += 1
    return result, tried


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
                result, attempts = _close_with_retries(
                    context, "mandatory bootstrap child failed", sleeper, interval_seconds
                )
                _persist_or_close(
                    context,
                    report,
                    {
                        "bootstrap": bootstrap_record,
                        "close": result.close_report.to_record() if result.close_report else None,
                        "close_attempts": attempts,
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
                result, attempts = _close_with_retries(
                    context,
                    "mandatory bootstrap child exited before hard deadline",
                    sleeper,
                    interval_seconds,
                )
                _persist_or_close(
                    context,
                    report,
                    {
                        "bootstrap": bootstrap_record,
                        "close": result.close_report.to_record() if result.close_report else None,
                        "close_attempts": attempts,
                        "green": False,
                    },
                )
                return result
        remaining = (context.timer.lease.hard_deadline - context.timer.now()).total_seconds()
        sleeper(min(interval_seconds, max(0.01, remaining)))
    # The deliberately still-running bootstrap child is left to the pod's
    # destruction: the timer is the container's primary process and the pod is
    # being terminated either way.
    result, attempts = _close_with_retries(
        context, "pod dead-man hard lifetime expired", sleeper, interval_seconds
    )
    _persist_or_close(
        context,
        report,
        {
            "bootstrap": bootstrap_record,
            "close": result.close_report.to_record() if result.close_report else None,
            "close_attempts": attempts,
            "green": bool(result.close_report and result.close_report.verified),
        },
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verbatus provider-neutral pod hard-lifetime dead-man",
        # No abbreviated long options: the pre-create validator in models.py
        # checks the exact flag spellings, and an abbreviation would bypass it.
        allow_abbrev=False,
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
    try:
        context = load_timer_context(args.timer_factory)
    except Exception as error:
        # No timer means no close capability was ever constructed (deferral
        # 04-4's territory) -- but a durable statement of that fact can still
        # reach the volume, so the restart machinery finds evidence rather
        # than silence.
        try:
            _write_report(
                Path(args.report_path),
                {
                    "schema": POD_REPORT_SCHEMA,
                    "identity": _identity_from_report_path(args.report_path),
                    "acknowledged_at": None,
                    "bootstrap": {
                        "state": "unstarted",
                        "detail": "no close capability was constructed; the timer factory failed",
                    },
                    "close": None,
                    "close_attempts": 0,
                    "green": False,
                    "error": str(error),
                },
            )
        except Exception as write_error:
            print(
                f"timer-factory failure report could not be written: {write_error}", file=sys.stderr
            )
        print(f"pod timer factory failed; nothing can close this pod: {error}", file=sys.stderr)
        return 2
    try:
        result = run_with_bootstrap(
            context,
            bootstrap_command_json=args.bootstrap_command_json,
            report_path=args.report_path,
            interval_seconds=args.interval_seconds,
        )
    except PodTimerFailureClosed as outcome:
        # The failure already spent an immediate close and filed its receipt.
        print(outcome, file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt) as error:
        # Any failure escaping after the provider-backed timer exists must
        # spend that capability on an immediate close rather than exit with
        # the pod running -- the timer is the pod's last disciplined act.
        # The receipt claims nothing about when the failure happened: this
        # handler cannot tell a startup refusal from a fault that escaped
        # after hours of monitoring, and inventing a state nobody observed is
        # exactly what `_persist_or_close` refuses to do.
        try:
            _durable_failure_close(
                context,
                Path(args.report_path),
                {
                    "state": "unrecorded",
                    "detail": (
                        "a failure escaped the monitored bootstrap/close paths; "
                        "this receipt replaced whatever report preceded it"
                    ),
                },
                error if isinstance(error, Exception) else RuntimeError(repr(error)),
                "pod timer failed outside its monitored paths",
            )
        except PodTimerFailureClosed as outcome:
            print(outcome, file=sys.stderr)
        return 2
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


def _identity_from_report_path(report_path: str) -> dict[str, object]:
    """Best-effort identity for a report filed before any lease was ever loaded.

    The timer factory failed, so no lease exists to name this launch by
    lease_id/pod_id/hard_deadline -- the only launch-identifying fact argv
    carries at this point is the bound report path itself, which
    ``models._required_timer_arguments`` already requires to include this
    launch's token.  This deliberately does not match the closed
    ``{lease_id, pod_id, hard_deadline}`` shape ``validate_pod_report_identity``
    accepts: a reader comparing this report against a real lease refuses it,
    the same way an actual mismatch would be refused.
    """

    return {"report_path": report_path}


def _acknowledged_report(context: TimerContext, value: dict[str, object]) -> dict[str, object]:
    """Tag a report with this run's schema, lease identity, and acknowledgement.

    Every write this module makes after the timer object exists carries the
    same three facts, captured once at ``TimerContext`` construction, so a
    reader can refuse one that does not name this exact lease
    (``models.validate_pod_report_identity``) without ever having watched the
    pod run.
    """

    return {
        "schema": POD_REPORT_SCHEMA,
        "identity": dict(context.identity),
        "acknowledged_at": context.acknowledged_at,
        **value,
    }


def _persist_or_close(context: TimerContext, path: Path, value: dict[str, object]) -> None:
    """A missing durable report is an immediate-close condition, never green."""

    try:
        _write_report(path, _acknowledged_report(context, value))
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
        prior = value.get("close_attempts")
        _durable_failure_close(
            context,
            path,
            bootstrap,
            error,
            "mandatory pod report write failed",
            prior_attempts=prior if isinstance(prior, int) and prior >= 0 else 0,
        )


def _durable_failure_close(
    context: TimerContext,
    path: Path,
    bootstrap: dict[str, object],
    error: Exception,
    label: str,
    *,
    prior_attempts: int = 0,
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
        # Close attempts already made before the report write failed, plus
        # this one -- a fallback claiming one attempt after three would hide
        # the three (GOVERNANCE 2).
        "close_attempts": prior_attempts + 1,
        "green": False,
    }
    receipt = "fallback receipt was written"
    try:
        _write_report(path, _acknowledged_report(context, fallback))
    except Exception as write_error:  # reported below, never swallowed
        receipt = f"fallback receipt also failed: {write_error}"
    state = result.close_report.state.value if result.close_report else "shutdown-exception"
    raise PodTimerFailureClosed(
        f"{label} ({error}); immediate close result is {state}, never green; {receipt}"
    ) from error


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
