"""The per-stage finish estimate, the deadline-at-risk notice, and the budget they name.

The run-tree reader is checked against a real fixture orchestrator run, so a stage
that renames its per-page record shows here rather than as an estimate that never
appears. Everything else runs on fakes: no pod, no phone, no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import AbsentChair
from common.contracts.stages import PERLECTOR
from common.stage import EXIT_HELD
from operations.notify.client import NotifyOutcome

from .finish_estimate import (
    BUDGET_OFF,
    ESTIMATE_SCHEMA,
    NOTICE_ATTEMPTS,
    PAGE_RECORDS,
    Budget,
    Deadline,
    DeadlineWatch,
    FinishEstimator,
    GuardDeadline,
    PodDeadline,
    RunTreeProgress,
    StageProgress,
    deadline_at_risk_message,
    pod_created_at,
    sealed_budget,
)
from .models import SpendRefusal
from .notify_hooks import NO_GUARD_TOPIC, _unsafe_reason
from .spend import POD_BUDGET_ENVIRONMENT, SpendPolicy, load_spend_policy

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_SPEND = ROOT / "config" / "spend.toml"
T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
# The budget `config/spend.toml` ships, for when the lead turns it on.
SHIPPED_BUDGET = Budget(
    soft_max_seconds=7_200,
    hard_max_seconds=10_800,
    soft_max_cost_usd=Decimal("6.00"),
    hard_max_cost_usd=Decimal("9.00"),
)
# An explicit budget for the notice tests, independent of the shipped file.
BUDGET = Budget(
    soft_max_seconds=14_400,
    hard_max_seconds=21_600,
    soft_max_cost_usd=Decimal("2.00"),
    hard_max_cost_usd=Decimal("3.00"),
)


# --- the spend policy's soft and hard maximums ---------------------------------------


def _shipped_policy_with_budget_on() -> SpendPolicy:
    return replace(load_spend_policy(SHIPPED_SPEND), pod_budget="on")


def test_the_shipped_policy_has_the_budget_off_and_carries_the_default_budget() -> None:
    policy = load_spend_policy(SHIPPED_SPEND)

    with pytest.raises(SpendRefusal, match="budget off"):
        Budget.from_policy(policy)
    assert sealed_budget(policy.budget_environment()) == (None, "budget off (lead's choice)")
    on = _shipped_policy_with_budget_on()
    assert Budget.from_policy(on) == SHIPPED_BUDGET
    # The guard is armed from the launch ceilings, at the soft maximum, never past it.
    assert on.hard_lifetime_seconds == on.soft_max_seconds
    assert on.max_estimated_metered_cost_usd == on.soft_max_cost_usd


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "NaN", "Infinity", " 2", "2 ", "1_0", " 1_0 "])
def test_a_sealed_budget_value_that_is_not_a_positive_number_leaves_the_budget_unknown(
    bad: str,
) -> None:
    sealed = _shipped_policy_with_budget_on().budget_environment()
    assert sealed_budget(sealed) == (SHIPPED_BUDGET, None)

    for name in POD_BUDGET_ENVIRONMENT.values():
        assert sealed_budget({**sealed, name: bad}) == (None, f"unusable {name}"), bad


@pytest.mark.parametrize(
    ("soft", "hard", "value"),
    [
        ("VERBATUS_SOFT_MAX_SECONDS", "VERBATUS_HARD_MAX_SECONDS", "999999"),
        ("VERBATUS_SOFT_MAX_COST_USD", "VERBATUS_HARD_MAX_COST_USD", "999.00"),
    ],
)
def test_a_sealed_soft_maximum_above_its_hard_maximum_leaves_the_budget_unknown(
    soft: str, hard: str, value: str
) -> None:
    sealed = _shipped_policy_with_budget_on().budget_environment()

    assert sealed_budget({**sealed, soft: value}) == (None, f"unusable {soft} above {hard}")


# --- the estimate --------------------------------------------------------------------


def test_no_estimate_until_enough_pages_and_time_have_passed() -> None:
    """The first sight of a stage is its anchor: pages done before it, and the model load
    before the first page, never count as this stage's pace. A pace needs five pages and
    ten minutes, and a clock that steps back never yields a negative one."""
    estimator = FinishEstimator()

    first = estimator.update(StageProgress(PERLECTOR, done=30, total=100), T0)
    few = estimator.update(StageProgress(PERLECTOR, done=33, total=100), T0 + timedelta(hours=1))
    quick = estimator.update(
        StageProgress(PERLECTOR, done=40, total=100), T0 + timedelta(seconds=300)
    )
    stepped = estimator.update(
        StageProgress(PERLECTOR, done=40, total=100), T0 - timedelta(seconds=60)
    )
    paced = estimator.update(
        StageProgress(PERLECTOR, done=40, total=100), T0 + timedelta(seconds=1000)
    )

    for early in (first, few, quick, stepped):
        assert early is not None and early.finishes_at is None and early.reason
    assert stepped is not None and (stepped.seconds_per_page or 0) >= 0
    assert paced is not None
    assert paced.seconds_per_page == 100.0
    assert paced.finishes_at == T0 + timedelta(seconds=1000 + 60 * 100)
    assert estimator.update(None, T0) is None


@pytest.mark.parametrize(
    ("total", "done", "finished"),
    [(None, 9, False), (5, 9, False), (9, 9, True)],
    ids=["total-unknown", "more-done-than-total", "all-done"],
)
def test_an_estimate_never_invents_a_total(total: int | None, done: int, finished: bool) -> None:
    estimator = FinishEstimator()
    estimator.update(StageProgress(PERLECTOR, done=1, total=total), T0)

    estimate = estimator.update(
        StageProgress(PERLECTOR, done=done, total=total), T0 + timedelta(minutes=8)
    )

    assert estimate is not None
    if finished:
        assert estimate.finishes_at == T0 + timedelta(minutes=8)
    else:
        assert estimate.finishes_at is None and estimate.reason


# --- the run tree, read the way a real orchestrator writes it -------------------------

FIXTURE_CHAIRS = load_models_toml(ROOT / "config" / "models.toml").chairs


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runs")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "estimate",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # The happy scenario ends held: the act that may cross its page break goes to review.
    assert completed.returncode == EXIT_HELD, completed.stderr[-2000:]
    return root / "estimate"


def test_every_page_counted_stage_is_read_from_its_real_records(fixture_run: Path) -> None:
    progress = RunTreeProgress(fixture_run, FIXTURE_CHAIRS)

    for stage in PAGE_RECORDS:
        counted = progress.count(stage)
        assert counted.done == counted.total and counted.done > 0, stage
    # Three witnesses read each of the two pages.
    assert progress.count("attestatores").total == 6
    assert progress.sample() is None, "every page-counted stage is sealed"


def test_only_page_witnesses_count_toward_the_attestatores_total(fixture_run: Path) -> None:
    absent = {**FIXTURE_CHAIRS, "attestator_3": AbsentChair("attestator_3", "not rented")}

    assert RunTreeProgress(fixture_run, absent).count("attestatores").total == 4
    assert RunTreeProgress(fixture_run, None).count("attestatores").total is None
    unknown = {name: chair for name, chair in FIXTURE_CHAIRS.items() if name != "attestator_2"}
    assert RunTreeProgress(fixture_run, unknown).count("attestatores").total is None


def test_the_stage_in_progress_is_the_first_unsealed_one(fixture_run: Path, tmp_path: Path) -> None:
    run = tmp_path / "estimate"
    shutil.copytree(fixture_run, run)
    perlector = run / "4_perlector" / "artifacts"
    shutil.rmtree(perlector / "stage-seal")
    next(iter(sorted((perlector / "page-accounting").iterdir()))).unlink()

    assert RunTreeProgress(run, FIXTURE_CHAIRS).sample() == StageProgress(
        PERLECTOR, done=1, total=2
    )


# --- the deadline that ends the pod --------------------------------------------------

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
VALID = int((NOW + timedelta(hours=4)).timestamp())


def _guard(tmp_path: Path) -> tuple[GuardDeadline, Path]:
    directory = tmp_path / ".pod_guard"
    directory.mkdir(exist_ok=True)
    return GuardDeadline(tmp_path, "pod123", now=lambda: NOW), directory / "deadline-pod123"


def test_the_guard_deadline_is_read_as_the_guard_reads_it(tmp_path: Path) -> None:
    """Only epoch seconds no more than a week out; anything else is ignored and the last
    valid deadline stands, as `pod_guard.sh` does."""
    guard, path = _guard(tmp_path)
    assert guard.read() is None  # no file yet: nothing read, nothing ignored
    path.write_text("soon\n", encoding="ascii")
    assert guard.read() is None
    assert guard.ignored == ["soon"]

    path.write_text(f"{VALID}\n", encoding="ascii")
    assert guard.read() == datetime.fromtimestamp(VALID, UTC)

    too_far = int((NOW + timedelta(days=8)).timestamp())
    for value in (f"{too_far}\n", f"{VALID}0\n", "\n", f" {VALID}\n"):
        path.write_text(value, encoding="ascii")
        assert guard.read() == datetime.fromtimestamp(VALID, UTC), value
    path.unlink()
    assert guard.read() == datetime.fromtimestamp(VALID, UTC)
    assert str(too_far) in guard.ignored and "" in guard.ignored

    later = VALID + 3600
    path.write_text(f"{later}\n", encoding="ascii")
    assert guard.read() == datetime.fromtimestamp(later, UTC)


def test_once_a_guard_deadline_is_seen_the_bootstrap_deadline_never_replaces_it(
    tmp_path: Path,
) -> None:
    guard, path = _guard(tmp_path)
    bootstrap = NOW + timedelta(hours=1)
    deadline = PodDeadline(guard=guard, bootstrap=bootstrap, pod_timer=False)
    unread = deadline()
    assert (unread.at, unread.extendable, unread.guard_unread) == (bootstrap, False, True)

    path.write_text(f"{VALID}\n", encoding="ascii")
    seen = deadline()
    assert seen.at == datetime.fromtimestamp(VALID, UTC) and seen.extendable
    path.write_text("garbage\n", encoding="ascii")
    assert deadline() == seen
    assert deadline.ignored == ["garbage"]


def test_under_the_pod_timer_the_deadline_cannot_be_extended_by_hand(tmp_path: Path) -> None:
    guard, path = _guard(tmp_path)
    path.write_text(f"{VALID}\n", encoding="ascii")
    timer = NOW + timedelta(hours=2)

    deadline = PodDeadline(guard=guard, bootstrap=timer, pod_timer=True)()

    assert deadline.at == timer and not deadline.extendable


def test_with_no_guard_deadline_and_no_bootstrap_deadline_the_pod_has_none(
    tmp_path: Path,
) -> None:
    """A pod whose budget is off, started with no hours: until a deadline is written,
    there is none, and a missing file is not an ignored value."""
    guard, path = _guard(tmp_path)
    deadline = PodDeadline(guard=guard, bootstrap=None, pod_timer=False)
    assert deadline() is None
    assert deadline.ignored == []

    path.write_text(f"{VALID}\n", encoding="ascii")
    later = deadline()
    assert later is not None and later.at == datetime.fromtimestamp(VALID, UTC)


# --- the deadline-at-risk notice -----------------------------------------------------


class Ticks:
    """A run whose progress, clock and deadline the test moves by hand."""

    def __init__(self, deadline: datetime | None) -> None:
        self.now = T0
        self.progress: StageProgress | None = StageProgress(PERLECTOR, done=10, total=100)
        self.deadline = deadline
        self.sent: list[str] = []
        self.outcome = NotifyOutcome(True, True, "delivered")

    def send(self, message: str) -> NotifyOutcome:
        self.sent.append(message)
        return self.outcome

    def sample(self) -> StageProgress | None:
        if self.progress is None:
            raise RuntimeError("unreadable tree")
        return self.progress

    def watch(self, path: Path, *, send: bool = True) -> DeadlineWatch:
        return DeadlineWatch(
            run_id="run-1",
            pod_id="pod123",
            path=path,
            sample=self.sample,
            budget=BUDGET,
            budget_problem=None,
            hourly_usd=Decimal("2.00"),
            deadline=lambda: (
                None
                if self.deadline is None
                else Deadline(self.deadline, "the pod guard's deadline", extendable=True)
            ),
            send=self.send if send else None,
            now=lambda: self.now,
        )

    def at(self, minutes: float, done: int) -> None:
        self.now = T0 + timedelta(minutes=minutes)
        self.progress = StageProgress(PERLECTOR, done=done, total=100)


def test_the_notice_goes_once_for_each_deadline_it_crosses(tmp_path: Path) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=2))
    path = tmp_path / "pod-run-report-estimate.json"
    watch = run.watch(path)

    watch.tick()  # first sight: no pace yet
    run.at(10, 15)  # 120 s a page: 85 pages left ends at 12:00, past 11:00
    watch.tick()
    run.at(20, 20)
    watch.tick()
    assert len(run.sent) == 1

    run.deadline = T0 + timedelta(hours=6)  # the lead extended by hand
    run.at(30, 25)
    watch.tick()
    assert len(run.sent) == 1

    run.at(180, 26)  # the pace collapses: past the new deadline too
    watch.tick()
    run.at(181, 26)
    watch.tick()
    assert len(run.sent) == 2

    run.deadline = T0 + timedelta(hours=2)  # a value already notified comes back
    run.at(182, 26)
    watch.tick()
    assert len(run.sent) == 2

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == ESTIMATE_SCHEMA
    assert record["estimate"]["stage"] == PERLECTOR
    assert record["at_risk"] is True
    assert [notice["deadline"] for notice in record["notices"]] == [
        "2026-10-03T11:00:00Z",
        "2026-10-03T15:00:00Z",
    ]
    assert all(notice["delivered"] for notice in record["notices"])


def test_with_no_deadline_nothing_is_at_risk_and_no_notice_goes(tmp_path: Path) -> None:
    run = Ticks(deadline=None)
    path = tmp_path / "estimate.json"
    watch = run.watch(path)

    watch.tick()
    for step in range(1, 8):
        run.at(step * 10, 10 + step)  # a pace that would pass any near deadline
        watch.tick()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["estimate"]["finishes_at"] is not None
    assert record["deadline"] is None
    assert record["deadline_source"] == "no deadline"
    assert record["at_risk"] is False
    assert record["notices"] == [] and run.sent == []


@pytest.mark.parametrize(
    "outcome",
    [NotifyOutcome(True, False, "notify.sh exited 1"), NotifyOutcome(False, False, NO_GUARD_TOPIC)],
    ids=["not-delivered", "no-guard-topic-yet"],
)
def test_a_notice_that_did_not_arrive_is_recorded_and_retried_a_bounded_number_of_times(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], outcome: NotifyOutcome
) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=1))
    run.outcome = outcome
    path = tmp_path / "estimate.json"
    watch = run.watch(path)

    watch.tick()
    for step in range(1, 8):
        run.at(step * 10, 10 + 5 * step)
        watch.tick()

    assert len(run.sent) == NOTICE_ATTEMPTS
    notices = json.loads(path.read_text(encoding="utf-8"))["notices"]
    assert [notice["delivered"] for notice in notices] == [False] * NOTICE_ATTEMPTS
    assert all(outcome.detail in notice["outcome"] for notice in notices)
    assert outcome.line() in capsys.readouterr().err
    assert watch.summary()["notices"] == notices


def test_a_notice_with_notifications_off_is_recorded_once(tmp_path: Path) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=1))
    path = tmp_path / "estimate.json"
    watch = run.watch(path, send=False)

    watch.tick()
    for step in range(1, 5):
        run.at(step * 10, 10 + 5 * step)
        watch.tick()

    [notice] = json.loads(path.read_text(encoding="utf-8"))["notices"]
    assert notice["attempted"] is False and "no --notify" in notice["outcome"]


def test_an_estimate_that_cannot_be_written_is_said_and_kept_in_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=1))
    blocker = tmp_path / "a-file-not-a-directory"
    blocker.write_text("", encoding="utf-8")
    watch = run.watch(blocker / "estimate.json")

    watch.tick()
    run.at(10, 15)
    watch.tick()

    assert run.sent, "a failed write never stops the notice"
    assert "could not be written" in capsys.readouterr().err
    assert watch.summary()["write_failures"] == 2


def test_a_tick_that_fails_is_written_to_the_estimate_file_and_never_raised(
    tmp_path: Path,
) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=1))
    run.progress = None
    path = tmp_path / "estimate.json"
    watch = run.watch(path)

    watch.tick()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == ESTIMATE_SCHEMA
    assert "unreadable tree" in record["last_tick_failure"]
    assert watch.summary()["tick_failures"] == 1


def _estimate():  # type: ignore[no-untyped-def]
    estimator = FinishEstimator()
    estimator.update(StageProgress(PERLECTOR, done=10, total=100), T0)
    estimate = estimator.update(
        StageProgress(PERLECTOR, done=15, total=100), T0 + timedelta(minutes=10)
    )
    assert estimate is not None and estimate.finishes_at is not None
    return estimate


def test_the_notice_names_both_maximums_the_finish_the_cost_and_one_command_to_extend() -> None:
    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=_estimate(),
        deadline=Deadline(T0 + timedelta(hours=2), "the pod guard's deadline", extendable=True),
        budget=BUDGET,
        budget_problem=None,
        hourly_usd=Decimal("2.00"),
        now=T0 + timedelta(minutes=10),
    )

    assert "soft max 4 h / $2.00" in message
    assert "hard max 6 h / $3.00" in message
    assert "this pod's creation time is unknown" in message
    assert "2026-10-03 12:00 UTC (in about 2.8 h)" in message  # 09:10 and 85 pages at 120 s
    assert "2026-10-03 11:00 UTC (in about 1.8 h)" in message
    # 12:00 plus 20 minutes to bring results home, past an 11:00 deadline: 80 minutes.
    assert "1.3 h" in message and "$2.67" in message
    suggested = int((T0 + timedelta(hours=3, minutes=20)).timestamp())
    assert (
        f"G=/workspace/private/.pod_guard; echo {suggested} > $G/deadline.new && "
        "mv $G/deadline.new $G/deadline-pod123" in message
    )
    assert "\n" not in message
    assert _unsafe_reason(message) is None


def test_a_pod_timer_deadline_offers_no_hand_route() -> None:
    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=_estimate(),
        deadline=Deadline(
            T0 + timedelta(hours=2), "the pod timer's hard deadline", extendable=False
        ),
        budget=BUDGET,
        budget_problem=None,
        hourly_usd=Decimal("2.00"),
        now=T0 + timedelta(minutes=10),
    )

    assert "cannot be extended by hand" in message
    assert "mv " not in message and "deadline.new" not in message
    assert _unsafe_reason(message) is None


def test_with_no_guard_deadline_read_the_notice_says_to_check_the_guard() -> None:
    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=_estimate(),
        deadline=Deadline(
            T0 + timedelta(hours=2), "the bootstrap's", extendable=False, guard_unread=True
        ),
        budget=BUDGET,
        budget_problem=None,
        hourly_usd=Decimal("2.00"),
        now=T0 + timedelta(minutes=10),
    )

    assert "no guard deadline was readable; check the guard" in message
    assert "cannot be extended by hand" not in message
    assert _unsafe_reason(message) is None


def test_a_missing_budget_or_price_is_named_not_guessed() -> None:
    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id=None,
        estimate=_estimate(),
        deadline=Deadline(T0 + timedelta(hours=2), "the bootstrap's", extendable=False),
        budget=None,
        budget_problem="spend policy is unconfigured",
        hourly_usd=None,
        now=T0 + timedelta(minutes=10),
    )

    assert "soft and hard max unknown (spend policy is unconfigured)" in message
    assert "cost unknown (no --hourly-usd)" in message
    assert _unsafe_reason(message) is None


def test_a_budget_switched_off_is_named_as_off_not_unknown() -> None:
    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=_estimate(),
        deadline=Deadline(T0 + timedelta(hours=2), "the pod guard's deadline", extendable=True),
        budget=None,
        budget_problem=BUDGET_OFF,
        hourly_usd=Decimal("2.00"),
        now=T0 + timedelta(minutes=10),
    )

    assert "No soft or hard maximum: budget off (lead's choice)" in message
    assert "not checked against the hard maximum: the budget is off" in message
    assert "unknown" not in message
    assert _unsafe_reason(message) is None


def _at_risk(created_at: datetime | None) -> str:
    """09:10, the stage ends about 12:00 and with the margin 12:20; the deadline is 11:00
    and the hard maximum six hours from ``created_at``."""
    return deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=_estimate(),
        deadline=Deadline(T0 + timedelta(hours=2), "the pod guard's deadline", extendable=True),
        budget=BUDGET,
        budget_problem=None,
        hourly_usd=Decimal("2.00"),
        now=T0 + timedelta(minutes=10),
        created_at=created_at,
    )


def test_an_extension_within_the_hard_maximum_names_it_as_a_clock_time() -> None:
    message = _at_risk(T0 - timedelta(hours=1))  # hard maximum 14:00
    suggested = int((T0 + timedelta(hours=3, minutes=20)).timestamp())
    assert "within the hard maximum 2026-10-03 14:00 UTC" in message
    assert f"echo {suggested} > $G/deadline.new" in message
    assert "checked against no budget" not in message


def test_an_extension_past_the_hard_maximum_is_capped_there_and_said_plainly() -> None:
    message = _at_risk(T0 - timedelta(hours=3))  # hard maximum 12:00, before 12:20
    cap = int((T0 + timedelta(hours=3)).timestamp())
    assert "passes the hard maximum 2026-10-03 12:00 UTC" in message
    assert f"echo {cap} > $G/deadline.new" in message
    assert str(int((T0 + timedelta(hours=3, minutes=20)).timestamp())) not in message
    assert _unsafe_reason(message) is None


def test_a_deadline_already_at_the_hard_maximum_offers_no_extension() -> None:
    message = _at_risk(T0 - timedelta(hours=4))  # hard maximum 11:00, the deadline
    assert "passes the hard maximum 2026-10-03 11:00 UTC" in message
    assert "deadline.new" not in message


def test_the_creation_instant_is_read_from_the_start_command_s_stamp(tmp_path: Path) -> None:
    guard = tmp_path / ".pod_guard"
    assert pod_created_at(tmp_path, "pod123") is None
    guard.mkdir()
    (guard / "created-pod123").write_text(f"{int(T0.timestamp())}\n", encoding="ascii")
    assert pod_created_at(tmp_path, "pod123") == T0
    (guard / "created-pod123").write_text("garbage\n", encoding="ascii")
    assert pod_created_at(tmp_path, "pod123") is None


@pytest.mark.parametrize("text", ["9" * 30, "99999999999999999"])
def test_an_out_of_range_creation_stamp_is_unknown_not_a_failed_tick(
    tmp_path: Path, text: str
) -> None:
    guard = tmp_path / ".pod_guard"
    guard.mkdir()
    (guard / "created-pod123").write_text(f"{text}\n", encoding="ascii")
    assert pod_created_at(tmp_path, "pod123") is None


def test_the_week_warning_judges_the_capped_extension_it_offers() -> None:
    """A stage projected weeks out is offered only the hard maximum, which the guard
    accepts; the warning would wrongly say it ignores that."""
    estimator = FinishEstimator()
    estimator.update(StageProgress(PERLECTOR, done=10, total=100_000), T0)
    estimate = estimator.update(
        StageProgress(PERLECTOR, done=15, total=100_000), T0 + timedelta(minutes=10)
    )
    assert estimate is not None and estimate.finishes_at is not None
    now = T0 + timedelta(minutes=10)
    assert estimate.finishes_at > now + timedelta(days=7)
    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=estimate,
        deadline=Deadline(T0 + timedelta(hours=2), "the pod guard's deadline", extendable=True),
        budget=BUDGET,
        budget_problem=None,
        hourly_usd=Decimal("2.00"),
        now=now,
        created_at=T0,
    )
    cap = int((T0 + timedelta(hours=6)).timestamp())
    assert f"echo {cap} > $G/deadline.new" in message
    assert "more than a week out" not in message
