"""`verbatus watch`: a pod run's progress, finish estimate and spend, from saved copies.

It reads the copies of `pod_run`'s report and its `-liveness`, `-timings` and
`-estimate` siblings that are already on this computer, and optionally a pod
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
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Final

from operations.pod.lease import PodLease
from operations.pod.models import run_report_paths

from .errors import ErrorCode, OperatorError, strip_control_bytes
from .records import RecordError, bounded_bytes

STALE_MINUTES_DEFAULT: Final = 2
"""The design's liveness limit: a pod run ticks every 15 seconds."""
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
    lease: PodLease | None
    problems: tuple[str, ...]
    fingerprint: bytes
    """The bytes read, so a follower prints only when something changed."""


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
    _, _, liveness_path, timings_path, _, estimate_path = (
        Path(path) for path in run_report_paths(PurePosixPath(report_path))
    )
    sides: dict[str, dict[str, Any] | None] = {}
    for name, path, schema in (
        ("liveness", liveness_path, "pod-run-liveness.v1"),
        ("estimate", estimate_path, "pod-run-estimate.v1"),
    ):
        record = _object(_read(path, f"the {name} copy", seen, problems=problems))
        if record is not None and (
            record.get("schema") != schema or record.get("run_id") != run_id
        ):
            problems.append(f"{path.name} is not this run's {schema} record; it was not used")
            record = None
        sides[name] = record
    timings: list[dict[str, Any]] = []
    raw = _read(timings_path, "the timings copy", seen, problems=problems)
    for line in (raw or b"").splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and entry.get("run_id") == run_id:
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
        lease=lease,
        problems=tuple(problems),
        fingerprint=b"\0".join(seen),
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

    # The freshest stamp the pod wrote is how current every number below is.
    stamps = {
        "liveness": _instant(liveness.get("last_seen")) if liveness else None,
        "estimate": _instant(estimate_file.get("updated_at")) if estimate_file else None,
    }
    if active:
        known = [stamp for stamp in stamps.values() if stamp is not None]
        if not known:
            lines.append(
                "STALE OR UNKNOWN: no liveness or estimate copy with a time is here, so "
                "nothing below can be called current."
            )
        else:
            newest = max(known)
            age = (now - newest).total_seconds()
            if age > stale_minutes * 60:
                lines.append(
                    f"STALE: the newest pod record is {_span(age)} old (limit {stale_minutes} "
                    f"min). The pod's progress below is as of {_clock(newest)}, not now. Copy fresh "
                    "files from the volume, or check the pod."
                )
    run_line = f"Run {report.get('run_id')}: pod_run report says {state}"
    if not active:
        exit_code = report.get("exit_code")
        finished = _instant(report.get("finished_at"))
        run_line += f" (exit {exit_code}" + (f", {_clock(finished)}" if finished else "") + ")"
    lines.append(run_line + ".")

    estimate = estimate_file.get("estimate") if estimate_file else None
    if isinstance(estimate, dict):
        total = estimate.get("pages_total")
        lines.append(
            f"Stage {estimate.get('stage')}: {estimate.get('pages_done')}/"
            f"{'?' if total is None else total} pages."
        )
        finishes = _instant(estimate.get("finishes_at"))
        if finishes is not None:
            lines.append(
                f"This stage finishes about {_when(finishes, now)}; later stages are not counted."
            )
        else:
            lines.append(
                f"Estimated finish: unknown ({estimate.get('reason') or 'no reason given'})."
            )
    elif estimate_file is None:
        lines.append("Estimated finish: unknown (no estimate copy here).")
    else:
        lines.append("Estimated finish: unknown (the stage running is not counted in pages).")

    lines.extend(_deadline_lines(receipts, now))
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
    if stamps["liveness"] is not None:
        alive = (
            "orchestrator running" if liveness and liveness.get("alive") else "orchestrator exited"
        )
        lines.append(
            f"Liveness: {alive}, last seen {_ago(stamps['liveness'], now)} "
            "(by this computer's clock)."
        )
    elif active:
        lines.append("Liveness: no copy here.")
    lines.extend(f"Note: {problem}" for problem in receipts.problems)
    return [strip_control_bytes(line) for line in lines]


def _deadline_lines(receipts: Receipts, now: datetime) -> list[str]:
    estimate_file = receipts.estimate or {}
    deadline = _instant(estimate_file.get("deadline"))
    source = _text(estimate_file.get("deadline_source"))
    if deadline is None:
        deadline = _instant(receipts.report.get("hard_deadline"))
        source = "the bootstrap's hard deadline in the report"
    if deadline is None:
        return ["Deadline: unknown (not in these copies)."]
    line = f"Deadline {_when(deadline, now)}, {source}."
    if estimate_file.get("at_risk") is True:
        line += " AT RISK: this stage plus time to bring results home passes it."
    return [line]


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
    # After pod_run ends the pod may still bill (a hold), so this is then a floor too.
    end = finished if finished is not None and finished < now else now
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
    cost = (rate * Decimal(str(seconds)) / Decimal(3600)).quantize(Decimal("0.01"))
    lines.append(
        f"Spend to {'now' if end == now else 'the end of pod_run'}: {floor}${cost} "
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
    last: tuple[bytes, bool] | None = None
    while True:
        receipts = read_receipts(run_id, report_path, lease_path)
        lines = render(receipts, now(), stale_minutes=stale_minutes)
        key = (receipts.fingerprint, lines[0].startswith("STALE"))
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
