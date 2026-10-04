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
manual extension route. It is sent once for each deadline value it crosses: a
deadline moved by hand to a new value re-arms it. A send that did not arrive is
recorded and tried again on later ticks, a bounded number of times. Nothing here
moves the deadline; extending is the lead's own act over SSH, and under the pod
timer there is no hand route at all.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from common.chairs.models import ChairIdentity
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
from .spend import POD_BUDGET_ENVIRONMENT, SpendPolicy, load_spend_policy_bytes

ESTIMATE_SCHEMA: Final = "pod-run-estimate.v1"
RESULTS_HOME_MARGIN_SECONDS: Final = 20 * 60
"""Time after the run to bring results home before the deadline (the design's margin)."""
MIN_PAGES_FOR_RATE: Final = 5
MIN_SECONDS_FOR_RATE: Final = 10 * 60
"""Pages and time a stage must show after its first sight before it has a pace.

Ticks fall every 15 seconds and pages finish in bursts, so a pace from two pages
can be off several times over; a notice it raised would use up the one notice
for that deadline. Five pages over ten minutes smooths that and still warns
hours ahead on a multi-hour stage."""
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

    def __init__(self, run_directory: Path, chairs: Mapping[str, object] | None) -> None:
        """``chairs`` is the run's models configuration, which says which witnesses read
        whole pages; None leaves the Attestatores total unknown."""

        self._root = Path(run_directory)
        self._chairs = chairs
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
            pages = len(
                {unit for unit, outcome in self._records(EXEMPLAR, "page") if outcome == "sealed"}
            )
        if PAGE_RECORDS[stage].per_witness:
            witnesses = self._page_witnesses(run)
            if witnesses is None:
                return None
            pages *= witnesses
        return pages

    def _page_witnesses(self, run: dict[str, Any] | None) -> int | None:
        """How many of the sealed roster's witnesses read whole pages, as the Attestatores
        decide it (``common.page_path.declared_page_witness_chairs``); None when the roster
        and the configuration do not say."""

        roster = None if run is None else run.get("witness_chairs")
        if (
            self._chairs is None
            or not isinstance(roster, list)
            or any(type(chair) is not str for chair in roster)
            or len(roster) != len(set(roster))
            or set(roster) - set(self._chairs)
        ):
            return None
        return sum(
            isinstance(self._chairs[chair], ChairIdentity)
            and self._chairs[chair].witness_scope == "page"
            for chair in roster
        )

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
        # A clock that steps back never makes a negative pace.
        elapsed = max(0.0, (now - since).total_seconds())
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
        elif observed < MIN_PAGES_FOR_RATE or elapsed < MIN_SECONDS_FOR_RATE or pace is None:
            reason = (
                f"{observed} page(s) in {elapsed:.0f} s since this stage was first seen; a "
                f"pace needs {MIN_PAGES_FOR_RATE} pages and {MIN_SECONDS_FOR_RATE} s"
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


def load_budget(path: Path) -> tuple[Budget | None, str | None, str | None]:
    """The budget the spend policy at ``path`` names, or None and why not, and the
    policy's SHA-256 (None when unreadable)."""

    try:
        data = path.read_bytes()
    except OSError as error:
        return None, f"cannot read spend policy {path}: {error}", None
    digest = hashlib.sha256(data).hexdigest()
    try:
        return Budget.from_policy(load_spend_policy_bytes(data, source=path)), None, digest
    except SpendRefusal as refusal:
        return None, str(refusal), digest


def sealed_budget(environment: Mapping[str, str | None]) -> tuple[Budget | None, str | None]:
    """The budget a launch sealed into the pod's environment, or None and why not.

    Each value is required: a budget sealed in part is not filled from elsewhere."""

    values = {field: environment.get(name) for field, name in POD_BUDGET_ENVIRONMENT.items()}
    missing = [POD_BUDGET_ENVIRONMENT[field] for field, value in values.items() if value is None]
    if missing:
        return None, f"{' and '.join(missing)} missing"
    parsed: dict[str, int | Decimal] = {}
    for field, value in values.items():
        try:
            parsed[field] = _positive(field, value or "")
        except ValueError:
            return None, f"unusable {POD_BUDGET_ENVIRONMENT[field]}"
    for soft, hard in (
        ("soft_max_seconds", "hard_max_seconds"),
        ("soft_max_cost_usd", "hard_max_cost_usd"),
    ):
        if parsed[soft] > parsed[hard]:
            return None, (
                f"unusable {POD_BUDGET_ENVIRONMENT[soft]} above {POD_BUDGET_ENVIRONMENT[hard]}"
            )
    return Budget(**parsed), None  # type: ignore[arg-type]


def _positive(field: str, text: str) -> int | Decimal:
    """A positive whole number of seconds, or a positive finite dollar amount, written
    plainly: no surrounding space and no digit separators."""

    if text != text.strip() or "_" in text:
        raise ValueError(text)
    if field.endswith("_seconds"):
        if not (text.isascii() and text.isdigit()) or int(text) <= 0:
            raise ValueError(text)
        return int(text)
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(text) from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError(text)
    return amount


GUARD_HORIZON_SECONDS: Final = 7 * 86_400
"""The guard ignores a deadline further out than this (`pod_guard.sh`, `sane_deadline`)."""


class GuardDeadline:
    """This pod's guard deadline, read the way ``pod_guard.sh`` reads it.

    The file's text, less trailing newlines, must be epoch seconds no more than
    a week ahead. Anything else (empty, unreadable, a typo) is ignored and the
    last valid deadline stands, as it does for the guard. Before any valid read
    there is none.
    """

    def __init__(self, volume_mount: Path, pod_id: str, *, now: Callable[[], datetime]) -> None:
        self._path = Path(volume_mount) / POD_GUARD_DIRECTORY / f"deadline-{pod_id}"
        self._now = now
        self._valid: datetime | None = None
        self._last_text: str | None = None
        self.ignored: list[str] = []

    def read(self) -> datetime | None:
        try:
            text = self._path.read_text(encoding="ascii").rstrip("\n")
            readable = True
        except (OSError, UnicodeDecodeError):
            text, readable = "", False
        if text == self._last_text:
            return self._valid
        self._last_text = text
        horizon = self._now().timestamp() + GUARD_HORIZON_SECONDS
        if text.isascii() and text.isdigit() and int(text) <= horizon:
            self._valid = datetime.fromtimestamp(int(text), UTC)
        elif (readable or self._valid is not None) and text not in self.ignored:
            # Ignored as the guard logs it; a file not there yet before any deadline is
            # no value at all.
            self.ignored.append(text)
        return self._valid


@dataclass(frozen=True, slots=True)
class Deadline:
    """The deadline that ends the pod, where it came from, and whether the lead can move
    it by hand."""

    at: datetime
    source: str
    extendable: bool
    guard_unread: bool = False
    """True when this pod's guard deadline was never readable, so the bootstrap's stands."""


class PodDeadline:
    """The deadline that ends this pod, for ``DeadlineWatch``.

    Under the pod timer, its hard deadline (or an earlier guard deadline) ends
    the pod and no file the lead edits moves it. Otherwise the guard's deadline
    does, and once one has been read it is never replaced by the bootstrap's;
    with none ever read, the bootstrap's hard deadline is the only one known.
    """

    def __init__(
        self, *, guard: GuardDeadline | None, bootstrap: datetime, pod_timer: bool
    ) -> None:
        self._guard = guard
        self._bootstrap = bootstrap
        self._pod_timer = pod_timer

    @property
    def ignored(self) -> list[str]:
        return [] if self._guard is None else list(self._guard.ignored)

    def __call__(self) -> Deadline:
        guard = None if self._guard is None else self._guard.read()
        if self._pod_timer:
            if guard is not None and guard < self._bootstrap:
                return Deadline(guard, "the pod guard's deadline, under the pod timer", False)
            return Deadline(self._bootstrap, "the pod timer's hard deadline", False)
        if guard is None:
            return Deadline(
                self._bootstrap,
                "the bootstrap's hard deadline (no guard deadline read)",
                False,
                guard_unread=True,
            )
        return Deadline(guard, "the pod guard's deadline", True)


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


def _relative(value: datetime, now: datetime) -> str:
    seconds = (value - now).total_seconds()
    span = abs(seconds)
    amount = f"{_hours(span)} h" if span >= 3600 else f"{int(span // 60)} min"
    return f"in about {amount}" if seconds >= 0 else f"about {amount} ago"


def _when(value: datetime, now: datetime) -> str:
    return f"{_minute(value)} ({_relative(value, now)})"


def deadline_at_risk_message(
    *,
    run_id: str,
    pod_id: str | None,
    estimate: StageEstimate,
    deadline: Deadline,
    budget: Budget | None,
    budget_problem: str | None,
    hourly_usd: Decimal | None,
    now: datetime,
    budget_source: str | None = None,
    hourly_source: str | None = None,
) -> str:
    """One line for the phone: what is at risk, what it would cost, and how to extend."""

    if estimate.finishes_at is None:
        raise ValueError("only a stage with a finish time can be at risk")
    projected = estimate.finishes_at + timedelta(seconds=RESULTS_HOME_MARGIN_SECONDS)
    extra = max(0.0, (projected - deadline.at).total_seconds())
    cost = (
        "cost unknown (no --hourly-usd)"
        if hourly_usd is None
        else f"about ${_cents(_cost(extra, hourly_usd))} more at ${hourly_usd}/h"
        + ("" if hourly_source is None else f" ({hourly_source})")
    )
    # The pod's creation time is not known here, so the hard maximum is stated, not
    # turned into an instant; the lead judges the extension against it.
    if budget is None:
        why = budget_problem if budget_source is None else f"{budget_problem}; {budget_source}"
        limits = f"soft and hard max unknown ({why})"
    else:
        origin = "" if budget_source is None else f" ({budget_source})"
        limits = (
            f"Budget from creation{origin}: soft max {_hours(budget.soft_max_seconds)} h / "
            f"${budget.soft_max_cost_usd}, hard max {_hours(budget.hard_max_seconds)} h / "
            f"${budget.hard_max_cost_usd}"
        )
    if deadline.guard_unread:
        route = (
            f"This deadline is {deadline.source}: no guard deadline was readable; check the "
            "guard before relying on any deadline"
        )
    elif not deadline.extendable:
        route = (
            f"This deadline ({deadline.source}) cannot be extended by hand; moving the "
            "guard's deadline file does not change it"
        )
    elif not pod_id:
        route = "No pod id is known here: extend by the route in operations/pod/README.md"
    else:
        suggested = math.ceil(projected.timestamp() / 60) * 60
        # The one mount path the bootstrap accepts, so the route names it as the lead sees it.
        guard = f"{POD_VOLUME_MOUNT_PATH}/{POD_GUARD_DIRECTORY}"
        route = (
            "To extend to the projected end (the lead only, over SSH; checked against no "
            f"budget): G={guard}; echo {suggested} > $G/deadline.new && mv $G/deadline.new "
            f"$G/deadline-{pod_id}"
        )
        if suggested > now.timestamp() + GUARD_HORIZON_SECONDS:
            route += " (more than a week out: the guard would ignore it)"
    return (
        f"run {run_id}: deadline at risk. Stage {estimate.stage} alone ends about "
        f"{_when(estimate.finishes_at, now)} ({estimate.done}/{estimate.total} pages); with "
        f"{RESULTS_HOME_MARGIN_SECONDS // 60} min to bring results home that passes the "
        f"deadline {_when(deadline.at, now)} by {_hours(extra)} h, {cost}. {limits}. {route}."
    )


class DeadlineWatch:
    """One estimate per tick, written beside the run report, and the notice it may raise.

    ``send`` is None when notifications are off; the notice is then recorded once
    as not sent. ``deadline`` returns the deadline that ends the pod. A tick
    never raises: a failure is said on stderr, written to the estimate file and
    kept in ``summary``.
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
        deadline: Callable[[], Deadline],
        send: Callable[[str], NotifyOutcome] | None,
        now: Callable[[], datetime],
        hourly_source: str | None = None,
        budget_source: str | None = None,
        ignored: Callable[[], list[str]] = list,
    ) -> None:
        self._run_id = run_id
        self._pod_id = pod_id
        self._path = path
        self._sample = sample
        self._budget = budget
        self._budget_problem = budget_problem
        self._budget_source = budget_source
        self._hourly_usd = hourly_usd
        self._hourly_source = hourly_source
        self._deadline = deadline
        self._send = send
        self._now = now
        self._ignored = ignored
        self._estimator = FinishEstimator()
        self._attempts: dict[datetime, int] = {}
        self._settled: set[datetime] = set()
        self._notices: list[dict[str, object]] = []
        self._write_failures = 0
        self._last_write_failure: str | None = None
        self._tick_failures = 0
        self._last_tick_failure: str | None = None

    def tick(self) -> None:
        now = self._now()
        try:
            record = self._tick(now)
        except Exception as error:  # noqa: BLE001 -- an estimate never stops a running stage
            self.note_failure(error)
            record = {"schema": ESTIMATE_SCHEMA, "run_id": self._run_id}
        self._write({**record, "updated_at": _stamp(now), **self.summary()})

    def note_failure(self, error: BaseException) -> None:
        self._tick_failures += 1
        self._last_tick_failure = f"{type(error).__name__}: {error}"
        print(
            f"pod_run {self._run_id}: the finish estimate failed this tick: {error}",
            file=sys.stderr,
        )

    def _tick(self, now: datetime) -> dict[str, object]:
        estimate = self._estimator.update(self._sample(), now)
        deadline = self._deadline()
        projected = (
            None
            if estimate is None or estimate.finishes_at is None
            else estimate.finishes_at + timedelta(seconds=RESULTS_HOME_MARGIN_SECONDS)
        )
        at_risk = projected is not None and projected > deadline.at
        if at_risk and estimate is not None and deadline.at not in self._settled:
            self._notify(now, estimate, deadline)
        return {
            "schema": ESTIMATE_SCHEMA,
            "run_id": self._run_id,
            "estimate": None if estimate is None else estimate.to_record(),
            "results_home_margin_seconds": RESULTS_HOME_MARGIN_SECONDS,
            "projected_with_margin": None if projected is None else _stamp(projected),
            "deadline": _stamp(deadline.at),
            "deadline_source": deadline.source,
            "deadline_extendable_by_hand": deadline.extendable,
            "at_risk": at_risk,
            "budget": None if self._budget is None else self._budget.to_record(),
            "budget_problem": self._budget_problem,
            "budget_source": self._budget_source,
            "hourly_usd": None if self._hourly_usd is None else str(self._hourly_usd),
            "hourly_usd_source": self._hourly_source,
        }

    def _write(self, record: dict[str, object]) -> None:
        try:
            atomic_write(self._path, canonical_json(record))
        except OSError as error:
            self._write_failures += 1
            self._last_write_failure = str(error)
            print(
                f"pod_run {self._run_id}: the finish estimate could not be written: {error}",
                file=sys.stderr,
            )

    def _notify(self, now: datetime, estimate: StageEstimate, deadline: Deadline) -> None:
        message = deadline_at_risk_message(
            run_id=self._run_id,
            pod_id=self._pod_id,
            estimate=estimate,
            deadline=deadline,
            budget=self._budget,
            budget_problem=self._budget_problem,
            hourly_usd=self._hourly_usd,
            now=now,
            budget_source=self._budget_source,
            hourly_source=self._hourly_source,
        )
        if self._send is None:
            outcome = NotifyOutcome(False, False, "no --notify")
        else:
            try:
                outcome = self._send(message)
            except Exception as error:  # noqa: BLE001 -- recorded and retried like a failed send
                outcome = NotifyOutcome(True, False, f"{type(error).__name__}: {error}")
        attempt = self._attempts.get(deadline.at, 0) + 1
        self._attempts[deadline.at] = attempt
        # Anything short of delivery is tried again on a later tick (a guard topic may
        # be armed after the run starts), up to the bound; notifications switched off
        # are recorded once.
        if outcome.delivered or self._send is None or attempt >= NOTICE_ATTEMPTS:
            self._settled.add(deadline.at)
        self._notices.append(
            {
                "type": "deadline-at-risk",
                "at": _stamp(now),
                "deadline": _stamp(deadline.at),
                "attempt": attempt,
                "attempted": outcome.attempted,
                "delivered": outcome.delivered,
                "outcome": outcome.line(),
                "message": message,
            }
        )
        print(f"pod_run {self._run_id}: deadline at risk. {outcome.line()}", file=sys.stderr)

    def summary(self) -> dict[str, object]:
        """What the run report keeps: every notice, every ignored deadline file value, and
        whether the estimate was computed and reached the volume."""

        return {
            "notices": list(self._notices),
            "ignored_deadline_file_values": self._ignored(),
            "write_failures": self._write_failures,
            "last_write_failure": self._last_write_failure,
            "tick_failures": self._tick_failures,
            "last_tick_failure": self._last_tick_failure,
        }
