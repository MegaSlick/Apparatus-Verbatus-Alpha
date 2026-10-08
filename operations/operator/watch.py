"""`verbatus watch`: a pod run's progress, finish estimate and spend, from saved copies.

It reads the copies of `pod_run`'s report and its `-liveness`, `-timings`,
`-estimate` and `-progress` siblings that are already on this computer, and optionally a pod
lease. It writes nothing and contacts no provider or volume. Every time it
shows is by this computer's clock; a record older than the stale limit is
said to be stale, and its numbers are labelled with the time they were true.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Final

from operations.pod.lease import PodLease
from operations.pod.models import run_report_paths

from .errors import ErrorCode, OperatorError, strip_control_bytes
from .records import RecordError, bounded_bytes

STALE_MINUTES_DEFAULT: Final = 2
"""The design's liveness limit: a pod run ticks every 15 seconds."""
RETRY_SECONDS: Final = 2
"""The pause before one more read of a copy that did not parse while following."""
ACTIVE_REPORT_STATES: Final = frozenset({"bootstrapping", "running"})
"""`pod_run` report states written before the run's outcome is known."""


@dataclass(frozen=True, slots=True)
class Receipts:
    """One reading of the saved copies; a missing sibling is None."""

    report_path: Path
    report: dict[str, Any]
    liveness: dict[str, Any] | None
    estimate: dict[str, Any] | None
    timings: tuple[dict[str, Any], ...]
    timings_unreadable: int
    lease: PodLease | None
    problems: tuple[str, ...]
    fingerprint: bytes
    """The bytes read, so a follower prints only when something changed."""
    progress: dict[str, Any] | None = None


def report_path_for(run_id: str, receipts: Path) -> Path:
    """The hand route's report name (`operations/pod/README.md`) inside ``receipts``."""

    return receipts / f"pod-run-report-{run_id}.json"


def read_receipts(run_id: str, report_path: Path, lease_path: Path | None) -> Receipts:
    """Read the report (required) and whatever siblings are there, refusing another run's."""

    seen: list[bytes] = []
    problems: list[str] = []
    report = _object(_read(report_path, "the pod-run report", seen, required=True))
    if report is None or report.get("schema") != "pod-run-report.v1":
        raise OperatorError(
            ErrorCode.WATCH_UNREADABLE,
            detail=f"{report_path} is not a pod-run-report.v1 record",
        )
    if report.get("run_id") != run_id:
        raise OperatorError(
            ErrorCode.WATCH_UNREADABLE,
            detail=f"{report_path} is the report of run {report.get('run_id')!r}, not {run_id!r}",
        )
    _, _, liveness_path, timings_path, _, estimate_path, progress_path = (
        Path(path) for path in run_report_paths(PurePosixPath(report_path))
    )
    sides: dict[str, dict[str, Any] | None] = {}
    for name, path, schema in (
        ("liveness", liveness_path, "pod-run-liveness.v1"),
        ("estimate", estimate_path, "pod-run-estimate.v1"),
        ("progress", progress_path, "pod-run-progress.v1"),
    ):
        record = _object(_read(path, f"the {name} copy", seen, problems=problems))
        if record is not None and (
            record.get("schema") != schema or record.get("run_id") != run_id
        ):
            problems.append(f"{path.name} is not this run's {schema} record; it was not used")
            record = None
        sides[name] = record
    timings: list[dict[str, Any]] = []
    timings_unreadable = 0
    raw = _read(timings_path, "the timings copy", seen, problems=problems)
    for line in (raw or b"").splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            entry = None
        if not isinstance(entry, dict):
            timings_unreadable += 1
        elif entry.get("run_id") == run_id:
            timings.append(entry)
    lease = None
    if lease_path is not None:
        record = _object(_read(lease_path, "the lease", seen, required=True))
        try:
            lease = PodLease.from_record(record)
        except Exception as error:  # noqa: BLE001 -- a lease that does not verify is named, not used
            problems.append(f"the lease {lease_path.name} was not used: {error}")
    return Receipts(
        report_path=report_path,
        report=report,
        liveness=sides["liveness"],
        estimate=sides["estimate"],
        timings=tuple(timings),
        timings_unreadable=timings_unreadable,
        lease=lease,
        problems=tuple(problems),
        fingerprint=b"\0".join(seen),
        progress=sides["progress"],
    )


def _read(
    path: Path,
    subject: str,
    seen: list[bytes],
    *,
    required: bool = False,
    problems: list[str] | None = None,
) -> bytes | None:
    try:
        data = bounded_bytes(path, subject)
    except FileNotFoundError:
        if required:
            raise OperatorError(
                ErrorCode.WATCH_UNREADABLE, detail=f"{path} does not exist"
            ) from None
        seen.append(b"")
        return None
    except (OSError, RecordError) as error:
        if required or problems is None:
            raise OperatorError(ErrorCode.WATCH_UNREADABLE, detail=f"{path}: {error}") from error
        problems.append(f"{path.name} could not be read: {error}")
        seen.append(b"")
        return None
    seen.append(data)
    return data


def _progress_lines(receipts: Receipts, active: bool) -> list[str]:
    """Whether the stage keeps its pace, as the pod guard is told it."""

    progress = receipts.progress
    if not active or progress is None:
        return []
    status = _text(progress.get("status")) or "unknown"
    last_ok = _instant(progress.get("last_ok"))
    line = f"Progress: {status}"
    if status != "ok" and last_ok is not None:
        line += f", last on pace {_clock(last_ok)}"
    findings = progress.get("findings")
    if isinstance(findings, list) and findings and _text(findings[0]):
        line += f" ({findings[0]})"
    return [line + "."]


def _object(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def render(
    receipts: Receipts, now: datetime, *, stale_minutes: int = STALE_MINUTES_DEFAULT
) -> list[str]:
    """Short lines for a phone, the loudest first."""

    report = receipts.report
    liveness = receipts.liveness
    estimate_file = receipts.estimate
    state = _text(report.get("state")) or "unknown"
    active = state in ACTIVE_REPORT_STATES
    lines: list[str] = []

    holding = report.get("held_to_hard_deadline") is True
    limit = stale_minutes * 60
    seen = _instant(liveness.get("last_seen")) if liveness else None
    written = _instant(estimate_file.get("updated_at")) if estimate_file else None
    # Each record is judged on its own age, so a fresh liveness never vouches for an old estimate.
    estimate_note = ""
    if active:
        if seen is None and written is None:
            lines.append(
                "STALE OR UNKNOWN: no liveness or estimate copy with a time is here, so "
                "nothing below can be called current."
            )
        if seen is not None and (now - seen).total_seconds() > limit:
            lines.append(
                f"STALE liveness: last written {_ago(seen, now)} (limit {stale_minutes} min). "
                "Copy fresh files from the volume, or check the pod."
            )
        if written is not None and (now - written).total_seconds() > limit:
            lines.append(
                f"STALE estimate: written {_ago(written, now)} (limit {stale_minutes} min); "
                "its stage, finish and deadline are as of then, not now."
            )
            estimate_note = (
                f" (as of {_clock(written)}, {_span((now - written).total_seconds())} old)"
            )
    run_line = f"Run {report.get('run_id')}: pod_run report says {state}"
    if not active:
        exit_code = report.get("exit_code")
        finished = _instant(report.get("finished_at"))
        run_line += f" (exit {exit_code}" + (f", {_clock(finished)}" if finished else "") + ")"
    lines.append(run_line + ".")
    if holding:
        hard = _instant(report.get("hard_deadline"))
        lines.append(
            f"Pod still billing until {_clock(hard) if hard else 'an unknown hard deadline'}: "
            "pod_run keeps it up to the hard deadline after the run ended."
        )
    for label, key in (("Hold", "hold_detail"), ("Detail", "detail")):
        if not active and _text(report.get(key)):
            lines.append(f"{label}: {report[key]}")

    failures = estimate_file.get("tick_failures") if estimate_file else None
    failing = (isinstance(failures, int) and failures > 0) or bool(
        estimate_file and _text(estimate_file.get("last_tick_failure"))
    )
    failure = (
        f"the estimate is failing ({failures if isinstance(failures, int) else '?'} ticks): "
        f"{estimate_file.get('last_tick_failure') if estimate_file else None}"
    )
    estimate = estimate_file.get("estimate") if estimate_file else None
    if not active:
        pass  # A finished run has no stage in progress to estimate.
    elif isinstance(estimate, dict):
        total = estimate.get("pages_total")
        lines.append(
            f"Stage {estimate.get('stage')}: {estimate.get('pages_done')}/"
            f"{'?' if total is None else total} pages{estimate_note}."
        )
        finishes = _instant(estimate.get("finishes_at"))
        if finishes is not None:
            lines.append(
                f"This stage finishes about {_when(finishes, now)}{estimate_note}; later "
                "stages are not counted."
            )
        else:
            lines.append(
                f"Estimated finish: unknown ({estimate.get('reason') or 'no reason given'})."
            )
        if failing:
            lines.append(f"Note: {failure}.")
    elif estimate_file is None:
        lines.append("Estimated finish: unknown (no estimate copy here).")
    elif failing:
        lines.append(f"Estimated finish: unknown; {failure}.")
    else:
        lines.append("Estimated finish: unknown (the stage running is not counted in pages).")
    write_failures = estimate_file.get("write_failures") if estimate_file else None
    if isinstance(write_failures, int) and write_failures > 0:
        lines.append(
            f"The estimate could not be written {write_failures} times; last: "
            f"{estimate_file.get('last_write_failure') if estimate_file else None}."
        )

    lines.extend(_progress_lines(receipts, active))
    lines.extend(_deadline_lines(receipts, now, estimate_note))
    lines.extend(_spend_lines(receipts, now))

    notices = _notices(receipts)
    if notices:
        last = notices[-1]
        sent = "delivered" if last.get("delivered") else "NOT delivered"
        at = _instant(last.get("at"))
        lines.append(
            f"Last notice: {last.get('type')} at {_clock(at) if at else '?'}, {sent}"
            f" ({last.get('outcome')})."
        )
    else:
        lines.append("Last notice: none recorded.")

    finished_stages = [
        f"{entry.get('stage')} {_span(entry['duration_ms'] / 1000)}"
        + ("" if entry.get("exit_code") == 0 else f" exit {entry.get('exit_code')}")
        for entry in receipts.timings
        if isinstance(entry.get("duration_ms"), int)
    ]
    if finished_stages:
        lines.append("Stage runs: " + ", ".join(finished_stages) + ".")
    if seen is not None:
        alive = (
            "orchestrator running" if liveness and liveness.get("alive") else "orchestrator exited"
        )
        lines.append(f"Liveness: {alive}, last seen {_ago(seen, now)} (by this computer's clock).")
    elif active:
        lines.append("Liveness: no copy here.")
    if receipts.timings_unreadable:
        lines.append(f"Note: {receipts.timings_unreadable} timings line(s) could not be read.")
    lines.extend(f"Note: {problem}" for problem in receipts.problems)
    return [strip_control_bytes(line) for line in lines]


def _deadline_lines(receipts: Receipts, now: datetime, note: str) -> list[str]:
    estimate_file = receipts.estimate
    deadline = _instant(estimate_file.get("deadline")) if estimate_file else None
    lines: list[str] = []
    if deadline is not None and estimate_file is not None:
        source = _text(estimate_file.get("deadline_source")) or "source not named"
        extendable = estimate_file.get("deadline_extendable_by_hand")
        by_hand = "yes" if extendable is True else "no" if extendable is False else "unknown"
        line = f"Deadline {_when(deadline, now)}{note}, {source}; extendable by hand: {by_hand}."
        if estimate_file.get("at_risk") is True:
            line += " AT RISK: this stage plus time to bring results home passes it."
        lines.append(line)
    else:
        why = (
            "there is no estimate copy"
            if estimate_file is None
            else "the estimate names no deadline"
        )
        hard = _instant(receipts.report.get("hard_deadline"))
        if hard is None:
            lines.append(f"Deadline: unknown ({why}, and the report names no hard deadline).")
        else:
            lines.append(
                f"Deadline {_when(hard, now)}: the bootstrap's hard deadline from the report, "
                f"because {why}; the guard's own deadline may be earlier."
            )
    lines.append(
        "Guard: watch cannot tell whether the guard is armed; it shows only the deadline "
        "pod_run read from the guard's file."
    )
    ignored = estimate_file.get("ignored_deadline_file_values") if estimate_file else None
    if isinstance(ignored, list) and ignored:
        lines.append(
            "Ignored deadline file values: " + ", ".join(repr(value) for value in ignored) + "."
        )
    return lines


def _spend_lines(receipts: Receipts, now: datetime) -> list[str]:
    estimate_file = receipts.estimate or {}
    lines: list[str] = []
    budget = estimate_file.get("budget")
    if isinstance(budget, dict):
        lines.append(
            f"Budget: soft max {_hours(budget.get('soft_max_seconds'))} / "
            f"${budget.get('soft_max_cost_usd')}, hard max "
            f"{_hours(budget.get('hard_max_seconds'))} / ${budget.get('hard_max_cost_usd')}."
        )
    else:
        problem = _text(estimate_file.get("budget_problem"))
        lines.append(f"Budget: unknown ({problem or 'not in these copies'}).")

    finished = _instant(receipts.report.get("finished_at"))
    # A pod kept up after the run still bills, so its spend runs to now.
    holding = receipts.report.get("held_to_hard_deadline") is True
    end = finished if finished is not None and finished < now and not holding else now
    lease = receipts.lease
    if lease is not None:
        rate = lease.pod_hourly_usd + lease.volume_hourly_usd
        start = lease.created_at
        basis = "pod and volume, since the pod was created (lease)"
        floor = "" if end == now else "at least "
    else:
        rate = _decimal(estimate_file.get("hourly_usd"))
        start = _instant(receipts.report.get("started_at"))
        basis = "since pod_run started; the pod was created earlier"
        floor = "at least "
    if rate is None or start is None:
        lines.append("Spend: unknown (no hourly rate or start time in these copies).")
        return lines
    seconds = max(0.0, (end - start).total_seconds())
    cost = (rate * Decimal(str(seconds)) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    against = (
        f" of soft ${budget.get('soft_max_cost_usd')} / hard ${budget.get('hard_max_cost_usd')}"
        if isinstance(budget, dict)
        else ""
    )
    lines.append(
        f"Spend to {'now' if end == now else 'the end of pod_run'}: {floor}${cost}{against} "
        f"({_span(seconds)} at ${rate}/h {basis})."
    )
    if lease is not None and lease.phase == "closed-verified":
        lines.append("The lease is closed-verified: billing has ended, so this overcounts.")
    elif lease is not None and lease.phase == "close-unverified":
        lines.append("The lease's close is UNVERIFIED: check billing in the RunPod console.")
    return lines


def _notices(receipts: Receipts) -> list[Mapping[str, Any]]:
    for source in (receipts.estimate, receipts.report.get("deadline_watch")):
        if isinstance(source, dict) and isinstance(source.get("notices"), list):
            notices = [notice for notice in source["notices"] if isinstance(notice, dict)]
            if notices:
                return notices
    return []


def watch(
    run_id: str,
    report_path: Path,
    *,
    lease_path: Path | None,
    stale_minutes: int,
    interval: float | None,
    timeout: float | None,
    printer: Callable[[str], None],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Show the run once, or with ``interval`` again on every change until it ends."""

    started = monotonic()
    last: tuple[bytes, tuple[str, ...]] | None = None
    while True:
        try:
            receipts = read_receipts(run_id, report_path, lease_path)
        except OperatorError:
            if interval is None or last is None:
                raise
            # A copy caught mid-write reads as broken; one more read tells it from a bad one.
            sleep(RETRY_SECONDS)
            receipts = read_receipts(run_id, report_path, lease_path)
        lines = render(receipts, now(), stale_minutes=stale_minutes)
        key = (receipts.fingerprint, tuple(line for line in lines if line.startswith("STALE")))
        if key != last:
            if last is not None:
                printer("")
            printer(f"As of {_clock(now())} (this computer's clock):")
            for line in lines:
                printer(line)
            last = key
        if interval is None:
            return
        if receipts.report.get("state") not in ACTIVE_REPORT_STATES:
            printer("The run has ended; watch stops.")
            return
        if timeout is not None and monotonic() - started + interval > timeout:
            printer("Timed out with the run still going; nothing was changed.")
            return
        sleep(interval)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _clock(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _span(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{int(seconds // 60)} min"


def _hours(value: object) -> str:
    return f"{value / 3600:g} h" if isinstance(value, int) else "? h"


def _ago(value: datetime, now: datetime) -> str:
    return f"{_span(max(0.0, (now - value).total_seconds()))} ago"


def _when(value: datetime, now: datetime) -> str:
    seconds = (value - now).total_seconds()
    relative = f"in {_span(seconds)}" if seconds >= 0 else f"{_span(-seconds)} ago"
    return f"{_clock(value)} ({relative})"
