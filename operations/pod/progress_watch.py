"""Whether the run in progress still moves at the pace its stage should, and the one
line the pod guard reads to decide whether the pod is working.

``pod_run`` calls ``ProgressWatch.tick`` on each liveness tick while the
orchestrator lives, with the same run-tree sample the finish estimate uses. A
page-counted stage is judged by its pages: the rate over the last ten minutes
against the rate the stage is expected to keep, and how long it has gone without
a new page. Any other stage is judged by its output: new transcript text or a new
record in the run tree. Each tick writes ``<report stem>-progress.json`` beside
the run report and, on a guarded pod, ``.pod_guard/progress-<pod id>``, one line:

    <epoch now> <epoch last ok> <ok|slow|stalled|bootstrapping> <check> <detail>

The guard trusts a line written in the last five minutes over its own CPU, GPU
and network counters: ``ok`` resets its idle ladder, anything else runs the
ladder from the last ``ok``. ``ProgressTicker`` writes the same line from a
thread through the bootstrap and the final volume sync, which have no liveness
tick of their own.

The expected rates are planning values, not measurements: the Perlector's planned
seconds a page over the calls its engine serves at once, Surya's seconds a page
over its runner processes. A stage with neither is held to its own pace once it
has one.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.contracts.stages import DESIGNATOR, PERLECTOR, STAGES
from common.decoding import load_decoding_policy, perlector_page_max_tokens
from operations.serving.config import ServingProfile, SubprocessProfile, load_serving_recipes

from .bootstrap import ORDERED_STEPS
from .durable import atomic_write, canonical_json
from .finish_estimate import (
    MIN_PAGES_FOR_RATE,
    MIN_SECONDS_FOR_RATE,
    PAGE_RECORDS,
    StageProgress,
)

PROGRESS_SCHEMA: Final = "pod-run-progress.v1"
RATE_WINDOW_SECONDS: Final = 10 * 60
SLOW_FRACTION: Final = 0.5
"""A stage whose rate over the window falls below this share of its expected rate is slow."""
QUIET_FLOOR_SECONDS: Final = 10 * 60
QUIET_EXPECTED_PAGES: Final = 3
"""A page stage is stalled after max(QUIET_FLOOR_SECONDS, this many expected page times)
with no new page."""
FIRST_PAGE_FALLBACK_SECONDS: Final = 30 * 60
"""How long a page stage with no known startup time may take to its first page, and how
long one with no expected pace at all may go between pages."""
OUTPUT_QUIET_SECONDS: Final = 15 * 60
"""How long a stage not counted in pages may show no new output or record. UNMEASURED: no
such stage's longest quiet stretch has been measured yet."""
LONG_STEP_SECONDS: Final = 60 * 60
"""How long one bootstrap step, or the final sync, counts as working with no other sign;
the slowest step seen, the model store's hash, takes about 40 minutes."""
TRANSCRIPT_TAIL_BYTES: Final = 64 * 1024
DETAIL_CHARACTERS: Final = 120

PERLECTOR_THROUGHPUT_MODULE: Final = (
    Path(__file__).resolve().parents[2] / "pipeline" / "4_perlector" / "throughput.py"
)
PERLECTOR_CHAIR: Final = "perlector"
SURYA_CHAIR: Final = "designator_surya"

_STAGE_LINE = re.compile(r"^run \S+: (?P<stage>[a-z-]+) (?P<event>started at|ended,) ")


@dataclass(frozen=True, slots=True)
class ExpectedRate:
    """The pace a stage is planned to keep, and how long it may take to its first page."""

    seconds_per_page: float
    startup_seconds: int
    source: str

    def to_record(self) -> dict[str, object]:
        return {
            "seconds_per_page": self.seconds_per_page,
            "startup_seconds": self.startup_seconds,
            "source": self.source,
        }


def _perlector_throughput():
    """The Perlector's planning module, by path: its stage folder is not a package."""

    name = "verbatus_perlector_throughput"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, PERLECTOR_THROUGHPUT_MODULE)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        sys.modules[name] = module
    return sys.modules[name]


def expected_rates(
    *, models_config: Path, serving_recipes_config: Path, decoding_config: Path, tier: str
) -> tuple[dict[str, ExpectedRate], list[str]]:
    """The planned pace of each stage that has one at this placement tier, and why any
    stage that should have one does not. Never raises: a stage without a plan is held to
    its own pace."""

    rates: dict[str, ExpectedRate] = {}
    problems: list[str] = []
    try:
        chairs = load_models_toml(models_config).chairs
        recipes = load_serving_recipes(serving_recipes_config)
    except Exception as error:  # noqa: BLE001 -- the stages then keep their own pace
        return rates, [f"the roster or the serving recipes could not be read: {error}"]

    perlector = chairs.get(PERLECTOR_CHAIR)
    if isinstance(perlector, ChairIdentity):
        try:
            profile = recipes.for_identity(perlector, tier)
            if isinstance(profile, ServingProfile):
                policy, _ = load_decoding_policy(decoding_config)
                planned = _perlector_throughput().planned_seconds_per_page(
                    perlector_page_max_tokens(policy)
                )
                calls = profile.max_num_seqs
                rates[PERLECTOR] = ExpectedRate(
                    planned / calls,
                    profile.startup_timeout_seconds,
                    f"planned {planned} s a page over {calls} calls at once",
                )
        except Exception as error:  # noqa: BLE001
            problems.append(f"{PERLECTOR}: {error}")

    surya = chairs.get(SURYA_CHAIR)
    if isinstance(surya, ChairIdentity):
        try:
            profile = recipes.for_identity(surya, tier)
            if isinstance(profile, SubprocessProfile):
                rates[DESIGNATOR] = ExpectedRate(
                    profile.seconds_per_page / profile.workers,
                    profile.startup_timeout_seconds,
                    f"Surya's {profile.seconds_per_page} s a page over "
                    f"{profile.workers} runner processes",
                )
        except Exception as error:  # noqa: BLE001
            problems.append(f"{DESIGNATOR}: {error}")
    return rates, problems


def transcript_stage(path: Path) -> str | None:
    """The stage the orchestrator's transcript says is running: the last stage it
    announced as started and not yet as ended. None when the tail names none."""

    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    current = None
    for line in tail.splitlines():
        match = _STAGE_LINE.match(line)
        if match is None:
            continue
        if match["event"] == "started at":
            current = match["stage"]
        elif match["stage"] == current:
            current = None
    return current if current in STAGES else None


def bootstrap_step(journal: Path) -> str:
    """The bootstrap step under way, from the bootstrap journal's completed steps."""

    try:
        record = json.loads(journal.read_bytes())
        completed = set(record["completed"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return "starting"
    for step in ORDERED_STEPS:
        if step.value not in completed:
            return step.value
    return "finishing"


def _detail(text: str) -> str:
    """Text the guard can quote: one line of plain characters."""

    return re.sub(r"[^A-Za-z0-9 ,._:/=-]", "", text).strip()[:DETAIL_CHARACTERS] or "-"


def write_progress_line(
    path: Path | None, *, now: float, last_ok: float, status: str, check: str, detail: str
) -> str | None:
    """Replace the guard's progress line; returns why it could not be, or None."""

    if path is None:
        return None
    line = (
        f"{int(now)} {int(last_ok)} {status} {_detail(check).replace(' ', '-')} {_detail(detail)}\n"
    )
    try:
        atomic_write(path, line.encode("ascii"))
    except OSError as error:
        return str(error)
    return None


@dataclass(slots=True)
class _Stage:
    """What one page-counted stage has shown since pod_run first saw it."""

    first_seen: datetime
    base: int
    done: int
    total: int | None
    last_seen: datetime
    first_page_at: datetime | None = None
    first_page_done: int = 0
    last_page_at: datetime | None = None
    history: deque[tuple[datetime, int]] = field(default_factory=deque)

    def observe(self, progress: StageProgress, now: datetime) -> None:
        if progress.done > self.done:
            if self.first_page_at is None:
                self.first_page_at = now
                self.first_page_done = progress.done
            self.last_page_at = now
        self.done = progress.done
        self.total = progress.total
        self.last_seen = now
        self.history.append((now, progress.done))
        while len(self.history) > 1 and (now - self.history[1][0]).total_seconds() >= (
            RATE_WINDOW_SECONDS
        ):
            self.history.popleft()

    def own_pace(self) -> float | None:
        """Seconds a page since the first new page, once there are enough to trust."""

        if self.first_page_at is None:
            return None
        pages = self.done - self.first_page_done
        elapsed = (self.last_seen - self.first_page_at).total_seconds()
        if (
            pages < MIN_PAGES_FOR_RATE
            or (self.last_seen - self.first_seen).total_seconds() < MIN_SECONDS_FOR_RATE
            or elapsed <= 0
        ):
            return None
        return elapsed / pages

    def window_pages(self) -> int:
        return self.done - self.history[0][1]

    def to_rate(self, stage: str) -> dict[str, object]:
        seconds = max(0.0, (self.last_seen - self.first_seen).total_seconds())
        observed = self.done - self.base
        return {
            "stage": stage,
            "pages_done": self.done,
            "pages_total": self.total,
            "pages_observed": observed,
            "seconds": round(seconds),
            "pages_per_minute": round(observed * 60 / seconds, 3) if seconds > 0 else None,
        }


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    failing_status: str = "stalled"

    def to_record(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class TickSample:
    """One run-tree sample per liveness tick, shared by the finish estimate and the
    progress check. A sample that failed raises again for each reader that asks."""

    def __init__(self, sample: Callable[[], StageProgress | None]) -> None:
        self._sample = sample
        self._latest: StageProgress | None = None
        self._error: Exception | None = None

    def refresh(self) -> StageProgress | None:
        """Take this tick's sample; None when it failed."""

        try:
            self._latest, self._error = self._sample(), None
        except Exception as error:  # noqa: BLE001 -- each reader reports it its own way
            self._latest, self._error = None, error
        return self._latest

    def __call__(self) -> StageProgress | None:
        if self._error is not None:
            raise self._error
        return self._latest


class ProgressWatch:
    """One verdict per liveness tick: ok, slow or stalled, written for the guard.

    ``stage_hint`` names the stage the transcript says is running, ``count`` counts a
    page stage's pages, and ``change`` returns anything that moves when a stage writes
    output or a record. A tick never raises: a failure is said on stderr and counted.
    """

    def __init__(
        self,
        *,
        run_id: str,
        path: Path,
        guard_line: Path | None,
        expected: Mapping[str, ExpectedRate],
        expected_problems: list[str],
        stage_hint: Callable[[], str | None],
        count: Callable[[str], StageProgress],
        change: Callable[[], object],
        now: Callable[[], datetime],
    ) -> None:
        self._run_id = run_id
        self._path = path
        self._guard_line = guard_line
        self._expected = dict(expected)
        self._expected_problems = list(expected_problems)
        self._stage_hint = stage_hint
        self._count = count
        self._change = change
        self._now = now
        self._stages: dict[str, _Stage] = {}
        self._mark: object = None
        self._last_change: datetime | None = None
        self._last_ok: datetime | None = None
        self.status = "ok"
        self.failures = 0
        self.last_failure: str | None = None

    def tick(self, sample: StageProgress | None) -> str:
        """Judge this tick and write the verdict; returns the status."""

        now = self._now()
        try:
            record, check = self._judge(sample, now)
        except Exception as error:  # noqa: BLE001 -- a progress check never stops a stage
            self._fail(f"the progress check failed: {type(error).__name__}: {error}")
            return self.status
        for problem in (
            self._write_record(record),
            write_progress_line(
                self._guard_line,
                now=now.timestamp(),
                last_ok=(self._last_ok or now).timestamp(),
                status=self.status,
                check=check.name,
                detail=check.detail,
            ),
        ):
            if problem is not None:
                self._fail(f"the progress record could not be written: {problem}")
        return self.status

    def _fail(self, message: str) -> None:
        self.failures += 1
        self.last_failure = message
        print(f"pod_run {self._run_id}: {message}", file=sys.stderr)

    def _judge(
        self, sample: StageProgress | None, now: datetime
    ) -> tuple[dict[str, object], Check]:
        mark = self._change()
        if self._last_change is None or mark != self._mark:
            self._mark, self._last_change = mark, now
        stage = self._stage_hint() or (None if sample is None else sample.stage)
        if stage in PAGE_RECORDS:
            progress = sample if sample is not None and sample.stage == stage else None
            checks, facts = self._page_checks(stage, progress or self._count(stage), now)
        else:
            quiet = (now - self._last_change).total_seconds()
            checks = [
                Check(
                    "output",
                    quiet < OUTPUT_QUIET_SECONDS,
                    f"no new output or record for {quiet:.0f} s"
                    if quiet >= OUTPUT_QUIET_SECONDS
                    else f"last output or record {quiet:.0f} s ago",
                )
            ]
            facts = {"measure": "output"}
        failing = [check for check in checks if not check.ok]
        if not failing:
            self.status = "ok"
        elif any(check.failing_status == "stalled" for check in failing):
            self.status = "stalled"
        else:
            self.status = failing[0].failing_status
        if not failing or self._last_ok is None:
            self._last_ok = now
        verdict = failing[0] if failing else checks[0]
        record = {
            "schema": PROGRESS_SCHEMA,
            "run_id": self._run_id,
            "updated_at": _stamp(now),
            "status": self.status,
            "last_ok": _stamp(self._last_ok),
            "stage": stage,
            **facts,
            "checks": [check.to_record() for check in checks],
            "findings": [f"{stage or 'run'}: {check.detail}" for check in failing],
            "expected_problems": self._expected_problems,
            "stage_rates": self.stage_rates(),
            "failures": self.failures,
            "last_failure": self.last_failure,
        }
        return record, verdict

    def _page_checks(
        self, stage: str, progress: StageProgress, now: datetime
    ) -> tuple[list[Check], dict[str, object]]:
        state = self._stages.get(stage)
        if state is None or progress.done < state.base:
            state = self._stages[stage] = _Stage(
                first_seen=now,
                base=progress.done,
                done=progress.done,
                total=progress.total,
                last_seen=now,
            )
        state.observe(progress, now)
        planned = self._expected.get(stage)
        own = state.own_pace()
        per_page = planned.seconds_per_page if planned is not None else own
        checks: list[Check] = []
        window_rate = None
        if state.first_page_at is None:
            allowance = (
                planned.startup_seconds + QUIET_EXPECTED_PAGES * planned.seconds_per_page
                if planned is not None
                else FIRST_PAGE_FALLBACK_SECONDS
            )
            waited = (now - state.first_seen).total_seconds()
            checks.append(
                Check(
                    "first-page",
                    waited <= allowance,
                    f"no new page in {waited:.0f} s since the stage was first seen, "
                    f"{allowance:.0f} s allowed",
                )
            )
        else:
            limit = (
                max(QUIET_FLOOR_SECONDS, QUIET_EXPECTED_PAGES * per_page)
                if per_page is not None
                else FIRST_PAGE_FALLBACK_SECONDS
            )
            quiet = (now - (state.last_page_at or state.first_page_at)).total_seconds()
            checks.append(
                Check(
                    "page-quiet",
                    quiet <= limit,
                    f"no new page for {quiet:.0f} s, {limit:.0f} s allowed",
                )
            )
            window_rate = state.window_pages() * 60 / RATE_WINDOW_SECONDS
            if (
                per_page is not None
                and (now - state.first_page_at).total_seconds() >= RATE_WINDOW_SECONDS
            ):
                expected = 60 / per_page
                checks.append(
                    Check(
                        "page-rate",
                        window_rate >= SLOW_FRACTION * expected,
                        f"{window_rate:.2f} pages a minute over {RATE_WINDOW_SECONDS // 60} min, "
                        f"{expected:.2f} expected",
                        failing_status="slow",
                    )
                )
        facts: dict[str, object] = {
            "measure": "pages",
            "pages_done": progress.done,
            "pages_total": progress.total,
            "expected": None if planned is None else planned.to_record(),
            "own_seconds_per_page": None if own is None else round(own, 1),
            "window_pages_per_minute": None if window_rate is None else round(window_rate, 3),
            "expected_pages_per_minute": None if per_page is None else round(60 / per_page, 3),
        }
        return checks, facts

    def _write_record(self, record: dict[str, object]) -> str | None:
        try:
            atomic_write(self._path, canonical_json(record))
        except OSError as error:
            return str(error)
        return None

    def stage_rates(self) -> list[dict[str, object]]:
        """Each page stage's pages and pace as pod_run saw them, in the order first seen."""

        return [state.to_rate(stage) for stage, state in self._stages.items()]

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status,
            "last_ok": None if self._last_ok is None else _stamp(self._last_ok),
            "failures": self.failures,
            "last_failure": self.last_failure,
        }


class ProgressTicker:
    """Writes the guard's progress line from a thread while a long step runs that has no
    liveness tick of its own: the bootstrap, the final volume sync.

    ``step`` names the step under way. A step counts as working for
    ``LONG_STEP_SECONDS``; after that the line stops moving its last-ok moment, says
    ``late_status``, and the guard's ladder climbs.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        status: str,
        late_status: str,
        check: str,
        step: Callable[[], str],
        now: Callable[[], datetime],
        interval_seconds: float,
    ) -> None:
        self._path = path
        self._status = status
        self._late_status = late_status
        self._check = check
        self._step = step
        self._now = now
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: tuple[str, float] | None = None

    def write(self) -> None:
        now = self._now().timestamp()
        step = self._step()
        if self._current is None or self._current[0] != step:
            self._current = (step, now)
        began = self._current[1]
        late = now - began > LONG_STEP_SECONDS
        problem = write_progress_line(
            self._path,
            now=now,
            last_ok=began + LONG_STEP_SECONDS if late else now,
            status=self._late_status if late else self._status,
            check=self._check,
            detail=f"{step} for {now - began:.0f} s",
        )
        if problem is not None:
            print(
                f"pod_run: the guard's progress line could not be written: {problem}",
                file=sys.stderr,
            )

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.write()
            except Exception as error:  # noqa: BLE001 -- a keep-alive never stops the step
                print(f"pod_run: the progress ticker failed: {error}", file=sys.stderr)

    def __enter__(self) -> ProgressTicker:
        if self._path is not None:
            self.write()
            self._thread = threading.Thread(target=self._loop, name="progress-ticker", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, 2 * self._interval))


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "PROGRESS_SCHEMA",
    "ExpectedRate",
    "ProgressTicker",
    "ProgressWatch",
    "TickSample",
    "bootstrap_step",
    "expected_rates",
    "transcript_stage",
    "write_progress_line",
]
