"""The pod side's per-stage finish estimate and its deadline-at-risk notice.

``pod_run`` calls ``DeadlineWatch.tick`` on each liveness tick while the
orchestrator lives. A tick reads the run tree for the stage in progress (pages
done of pages total), keeps a finish estimate for that stage, and writes it
beside the run report so a later reader (``verbatus watch``) can show it.

**The estimate is for the current stage only.** It is the stage's pace since
``pod_run`` first saw it, times the pages it has left. Later stages are not
counted, so the run itself ends later still: a stage whose own finish passes
the deadline is a run that certainly will. Stages that are not counted in pages
(Recensor, Archetypus, Coniector, Armarium) have no estimate.

**Deadline at risk.** When the stage's finish, plus time to bring results home,
passes the deadline that actually ends the pod, one ``decision`` notice names
the soft and hard maximums, the finish, the extra time and its cost, and the
manual extension route. It is sent once per deadline: a deadline moved by hand
re-arms it. A send that did not arrive is recorded and tried again on later
ticks, a bounded number of times. Nothing here moves the deadline; extending is
the lead's own act over SSH.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

from common.contracts.stages import (
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    WRITING_DIRECTORIES,
)
from operations.notify.client import NotifyOutcome

from .durable import atomic_write, canonical_json
from .models import POD_GUARD_DIRECTORY, POD_VOLUME_MOUNT_PATH, SpendRefusal
from .spend import SpendPolicy, load_spend_policy

ESTIMATE_SCHEMA: Final = "pod-run-estimate.v1"
RESULTS_HOME_MARGIN_SECONDS: Final = 20 * 60
"""Time after the run to bring results home before the deadline (the design's margin)."""
MIN_PAGES_FOR_RATE: Final = 2
"""Pages that must finish after the first sight of a stage before it has a pace."""
NOTICE_ATTEMPTS: Final = 3
"""Sends tried for one crossing when the notification command reports it did not arrive."""


@dataclass(frozen=True, slots=True)
class PageRecord:
    """The record a stage writes once per page; per witness for the Attestatores."""

    kind: str
    per_witness: bool = False


PAGE_RECORDS: Final[Mapping[str, PageRecord]] = {
    DOOR: PageRecord("admission"),
    EXEMPLAR: PageRecord("page"),
    INK_MAP: PageRecord("ink-map"),
    DESIGNATOR: PageRecord("detector-page"),
    ATTESTATORES: PageRecord("page-testimonium", per_witness=True),
    PERLECTOR: PageRecord("page-accounting"),
}
"""The page-counted stages, in run order."""

_STAGE_SEAL_KIND: Final = "stage-seal"


@dataclass(frozen=True, slots=True)
class StageProgress:
    stage: str
    done: int
    total: int | None
    """None when the run tree does not say how many pages this stage has."""


class RunTreeProgress:
    """Pages done and pages total for the page-counted stage in progress.

    Records are immutable once published, so each file is read once and kept;
    a tick reads only what appeared since the last. A file that cannot be read
    is not kept and is tried again next tick.
    """

    def __init__(self, run_directory: Path) -> None:
        self._root = Path(run_directory)
        self._seen: dict[Path, tuple[str, str | None] | None] = {}
        self._run: dict[str, Any] | None = None

    def _records(self, stage: str, kind: str) -> list[tuple[str, str | None]]:
        directory = self._root / WRITING_DIRECTORIES[stage] / "artifacts" / kind
        try:
            names = sorted(entry for entry in directory.iterdir() if entry.suffix == ".json")
        except OSError:
            return []
        found = []
        for path in names:
            if path not in self._seen:
                try:
                    record = json.loads(path.read_bytes())
                    self._seen[path] = _unit(record, per_witness=kind == "page-testimonium")
                except (OSError, ValueError, RecursionError):
                    continue
            unit = self._seen[path]
            if unit is not None:
                found.append(unit)
        return found

    def _sealed(self, stage: str) -> bool:
        return any(unit == stage for unit, _ in self._records(stage, _STAGE_SEAL_KIND))

    def _run_record(self) -> dict[str, Any] | None:
        if self._run is None:
            try:
                record = json.loads((self._root / "run.json").read_bytes())
            except (OSError, ValueError, RecursionError):
                return None
            self._run = record if isinstance(record, dict) else None
        return self._run

    def _total(self, stage: str) -> int | None:
        run = self._run_record()
        sources = None if run is None else run.get("source_manifest")
        if not isinstance(sources, list):
            return None
        if stage in (DOOR, EXEMPLAR) or not self._sealed(EXEMPLAR):
            pages = len(sources)
        else:
            pages = sum(outcome == "sealed" for _, outcome in set(self._records(EXEMPLAR, "page")))
        if PAGE_RECORDS[stage].per_witness:
            witnesses = None if run is None else run.get("witness_chairs")
            if not isinstance(witnesses, list) or not witnesses:
                return None
            pages *= len(witnesses)
        return pages

    def count(self, stage: str) -> StageProgress:
        units = {unit for unit, _ in self._records(stage, PAGE_RECORDS[stage].kind)}
        return StageProgress(stage, done=len(units), total=self._total(stage))

    def sample(self) -> StageProgress | None:
        """The first page-counted stage with records and no seal, or None."""

        for stage, page in PAGE_RECORDS.items():
            if self._records(stage, page.kind) and not self._sealed(stage):
                return self.count(stage)
        return None


def _unit(record: object, *, per_witness: bool) -> tuple[str, str | None] | None:
    """What one record counts as: its page (and witness), with its outcome."""

    if not isinstance(record, dict) or not isinstance(record.get("subject_id"), str):
        return None
    subject = record["subject_id"]
    if per_witness:
        payload = record.get("payload")
        chair = payload.get("chair") if isinstance(payload, dict) else None
        if not isinstance(chair, str):
            return None
        subject = f"{subject}/{chair}"
    outcome = record.get("outcome")
    return subject, outcome if isinstance(outcome, str) else None


@dataclass(frozen=True, slots=True)
class StageEstimate:
    stage: str
    done: int
    total: int | None
    observed_since: datetime
    observed_pages: int
    seconds_per_page: float | None
    finishes_at: datetime | None
    reason: str | None
    """Why there is no finish time; None when there is one."""

    def to_record(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "pages_done": self.done,
            "pages_total": self.total,
            "observed_since": _stamp(self.observed_since),
            "observed_pages": self.observed_pages,
            "seconds_per_page": self.seconds_per_page,
            "finishes_at": None if self.finishes_at is None else _stamp(self.finishes_at),
            "label": "this stage finishes about",
            "reason": self.reason,
        }


class FinishEstimator:
    """The current stage's finish, from its pace since ``pod_run`` first saw it.

    The anchor is the first sight of the stage, so pages a previous invocation
    finished, and the model load before the first page, never set the pace.
    """

    def __init__(self) -> None:
        self._anchors: dict[str, tuple[datetime, int]] = {}

    def update(self, progress: StageProgress | None, now: datetime) -> StageEstimate | None:
        if progress is None:
            return None
        since, base = self._anchors.setdefault(progress.stage, (now, progress.done))
        if progress.done < base:
            since, base = self._anchors[progress.stage] = (now, progress.done)
        observed = progress.done - base
        elapsed = (now - since).total_seconds()
        pace = elapsed / observed if observed > 0 else None
        finishes_at = None
        reason = None
        if progress.total is None:
            reason = "the run tree does not say how many pages this stage has"
        elif progress.done > progress.total:
            reason = (
                f"{progress.done} pages done of {progress.total} expected; the total is not known"
            )
        elif progress.done == progress.total:
            finishes_at = now
        elif observed < MIN_PAGES_FOR_RATE or pace is None:
            reason = (
                f"{observed} page(s) finished since this stage was first seen; a pace needs "
                f"{MIN_PAGES_FOR_RATE}"
            )
        else:
            finishes_at = now + timedelta(seconds=pace * (progress.total - progress.done))
        return StageEstimate(
            stage=progress.stage,
            done=progress.done,
            total=progress.total,
            observed_since=since,
            observed_pages=observed,
            seconds_per_page=pace,
            finishes_at=finishes_at,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class Budget:
    """A pod's soft and hard maximums, in time from creation and in metered cost."""

    soft_max_seconds: int
    hard_max_seconds: int
    soft_max_cost_usd: Decimal
    hard_max_cost_usd: Decimal

    @classmethod
    def from_policy(cls, policy: SpendPolicy) -> Budget:
        if (
            not policy.configured
            or policy.soft_max_seconds is None
            or policy.hard_max_seconds is None
            or policy.soft_max_cost_usd is None
            or policy.hard_max_cost_usd is None
        ):
            raise SpendRefusal("spend policy is unconfigured; it names no budget")
        return cls(
            soft_max_seconds=policy.soft_max_seconds,
            hard_max_seconds=policy.hard_max_seconds,
            soft_max_cost_usd=policy.soft_max_cost_usd,
            hard_max_cost_usd=policy.hard_max_cost_usd,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "soft_max_seconds": self.soft_max_seconds,
            "hard_max_seconds": self.hard_max_seconds,
            "soft_max_cost_usd": str(self.soft_max_cost_usd),
            "hard_max_cost_usd": str(self.hard_max_cost_usd),
        }


def load_budget(path: Path) -> tuple[Budget | None, str | None]:
    """The budget the spend policy names, or None and why not."""

    try:
        return Budget.from_policy(load_spend_policy(path)), None
    except SpendRefusal as refusal:
        return None, str(refusal)


def guard_deadline(volume_mount: Path, pod_id: str | None) -> datetime | None:
    """This pod's guard deadline, from its file on the volume; None when there is none."""

    if not pod_id or not (pod_id.isascii() and pod_id.isalnum()):
        return None
    try:
        text = (Path(volume_mount) / POD_GUARD_DIRECTORY / f"deadline-{pod_id}").read_text(
            encoding="ascii"
        )
    except (OSError, UnicodeDecodeError):
        return None
    text = text.strip()
    if not text.isdigit() or len(text) > 12:
        return None
    return datetime.fromtimestamp(int(text), UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _minute(value: datetime) -> str:
    # Minutes, spaced: the notification seam reads a compact ISO stamp as a token.
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _hours(seconds: float) -> str:
    return f"{Decimal(seconds) / 3600:.1f}".rstrip("0").rstrip(".")


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _cost(seconds: float, hourly_usd: Decimal) -> Decimal:
    return hourly_usd * Decimal(str(seconds)) / Decimal(3600)


def fits_under_hard_max(
    *,
    projected: datetime,
    launch_deadline: datetime,
    budget: Budget,
    hourly_usd: Decimal | None,
) -> bool | None:
    """Whether running to ``projected`` stays within the hard maximum; None when unknown.

    The deadline ``pod_run`` first read is taken as the soft maximum, where the
    guard is armed; the hard maximum allows that much more time and cost on top.
    """

    extension = max(0.0, (projected - launch_deadline).total_seconds())
    if extension > budget.hard_max_seconds - budget.soft_max_seconds:
        return False
    if hourly_usd is None:
        return None
    return _cost(extension, hourly_usd) <= budget.hard_max_cost_usd - budget.soft_max_cost_usd


def deadline_at_risk_message(
    *,
    run_id: str,
    pod_id: str | None,
    estimate: StageEstimate,
    deadline: datetime,
    launch_deadline: datetime,
    budget: Budget | None,
    budget_problem: str | None,
    hourly_usd: Decimal | None,
) -> str:
    """One line for the phone: what is at risk, what it would cost, and how to extend."""

    if estimate.finishes_at is None:
        raise ValueError("only a stage with a finish time can be at risk")
    projected = estimate.finishes_at + timedelta(seconds=RESULTS_HOME_MARGIN_SECONDS)
    extra = max(0.0, (projected - deadline).total_seconds())
    cost = (
        "cost unknown (no --hourly-usd)"
        if hourly_usd is None
        else f"about ${_cents(_cost(extra, hourly_usd))} more at ${hourly_usd}/h"
    )
    if budget is None:
        limits = f"soft and hard max unknown ({budget_problem})"
    else:
        fits = fits_under_hard_max(
            projected=projected,
            launch_deadline=launch_deadline,
            budget=budget,
            hourly_usd=hourly_usd,
        )
        limits = (
            f"soft max {_hours(budget.soft_max_seconds)} h / ${budget.soft_max_cost_usd}, "
            f"hard max {_hours(budget.hard_max_seconds)} h / ${budget.hard_max_cost_usd}; "
            f"fits under the hard max: {'unknown' if fits is None else 'yes' if fits else 'no'}"
        )
    # The one mount path the bootstrap accepts, so the route names it as the lead sees it.
    guard = f"{POD_VOLUME_MOUNT_PATH}/{POD_GUARD_DIRECTORY}"
    route = (
        f"To extend (the lead only, over SSH): write the new deadline in epoch seconds to a "
        f"temporary file in {guard} and mv it over deadline-{pod_id}"
        if pod_id
        else "No pod id: extend by the route in operations/pod/README.md"
    )
    return (
        f"run {run_id}: deadline at risk. Stage {estimate.stage} alone ends about "
        f"{_minute(estimate.finishes_at)} ({estimate.done}/{estimate.total} pages); with "
        f"{RESULTS_HOME_MARGIN_SECONDS // 60} min to bring results home that passes the "
        f"deadline {_minute(deadline)} by {_hours(extra)} h, {cost}. {limits}. {route}."
    )


class DeadlineWatch:
    """One estimate per tick, written beside the run report, and the notice it may raise.

    ``send`` is None when notifications are off; the notice is then recorded as
    not sent. ``deadline`` returns the deadline that ends the pod and where it
    came from; the first one read is taken as the soft maximum.
    """

    def __init__(
        self,
        *,
        run_id: str,
        pod_id: str | None,
        path: Path,
        sample: Callable[[], StageProgress | None],
        budget: Budget | None,
        budget_problem: str | None,
        hourly_usd: Decimal | None,
        deadline: Callable[[], tuple[datetime, str]],
        send: Callable[[str], NotifyOutcome] | None,
        now: Callable[[], datetime],
    ) -> None:
        self._run_id = run_id
        self._pod_id = pod_id
        self._path = path
        self._sample = sample
        self._budget = budget
        self._budget_problem = budget_problem
        self._hourly_usd = hourly_usd
        self._deadline = deadline
        self._send = send
        self._now = now
        self._estimator = FinishEstimator()
        self._launch_deadline: datetime | None = None
        self._crossed: datetime | None = None
        self._attempts = 0
        self._settled = False
        self._notices: list[dict[str, object]] = []
        self._write_failures = 0
        self._last_write_failure: str | None = None

    def tick(self) -> dict[str, object]:
        now = self._now()
        estimate = self._estimator.update(self._sample(), now)
        deadline, source = self._deadline()
        if self._launch_deadline is None:
            self._launch_deadline = deadline
        launch = self._launch_deadline
        projected = (
            None
            if estimate is None or estimate.finishes_at is None
            else estimate.finishes_at + timedelta(seconds=RESULTS_HOME_MARGIN_SECONDS)
        )
        at_risk = projected is not None and projected > deadline
        if at_risk and estimate is not None:
            if self._crossed != deadline:
                self._crossed, self._attempts, self._settled = deadline, 0, False
            if not self._settled:
                self._notify(now, estimate, deadline, launch)
        record: dict[str, object] = {
            "schema": ESTIMATE_SCHEMA,
            "run_id": self._run_id,
            "updated_at": _stamp(now),
            "estimate": None if estimate is None else estimate.to_record(),
            "results_home_margin_seconds": RESULTS_HOME_MARGIN_SECONDS,
            "projected_with_margin": None if projected is None else _stamp(projected),
            "deadline": _stamp(deadline),
            "deadline_source": source,
            "at_risk": at_risk,
            "budget": None if self._budget is None else self._budget.to_record(),
            "budget_problem": self._budget_problem,
            "hourly_usd": None if self._hourly_usd is None else str(self._hourly_usd),
            **self.summary(),
        }
        try:
            atomic_write(self._path, canonical_json(record))
        except OSError as error:
            self._write_failures += 1
            self._last_write_failure = str(error)
            print(
                f"pod_run {self._run_id}: the finish estimate could not be written: {error}",
                file=sys.stderr,
            )
        return record

    def _notify(
        self, now: datetime, estimate: StageEstimate, deadline: datetime, launch: datetime
    ) -> None:
        message = deadline_at_risk_message(
            run_id=self._run_id,
            pod_id=self._pod_id,
            estimate=estimate,
            deadline=deadline,
            launch_deadline=launch,
            budget=self._budget,
            budget_problem=self._budget_problem,
            hourly_usd=self._hourly_usd,
        )
        if self._send is None:
            outcome = NotifyOutcome(False, False, "no --notify")
        else:
            try:
                outcome = self._send(message)
            except Exception as error:  # noqa: BLE001 -- recorded and retried like a failed send
                outcome = NotifyOutcome(True, False, f"{type(error).__name__}: {error}")
        self._attempts += 1
        # Retried only when a send was made and did not arrive; one that could
        # not be attempted would fail the same way again.
        self._settled = (
            outcome.delivered or not outcome.attempted or self._attempts >= NOTICE_ATTEMPTS
        )
        self._notices.append(
            {
                "type": "deadline-at-risk",
                "at": _stamp(now),
                "deadline": _stamp(deadline),
                "attempt": self._attempts,
                "attempted": outcome.attempted,
                "delivered": outcome.delivered,
                "outcome": outcome.line(),
                "message": message,
            }
        )
        print(f"pod_run {self._run_id}: deadline at risk. {outcome.line()}", file=sys.stderr)

    def summary(self) -> dict[str, object]:
        """What the run report keeps: every notice and whether the estimate reached the volume."""

        return {
            "notices": list(self._notices),
            "write_failures": self._write_failures,
            "last_write_failure": self._last_write_failure,
        }
