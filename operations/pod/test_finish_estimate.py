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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from common.contracts.stages import PERLECTOR
from operations.notify.client import NotifyOutcome

from . import finish_estimate
from .finish_estimate import (
    ESTIMATE_SCHEMA,
    NOTICE_ATTEMPTS,
    PAGE_RECORDS,
    Budget,
    DeadlineWatch,
    FinishEstimator,
    RunTreeProgress,
    StageProgress,
    deadline_at_risk_message,
)
from .notify_hooks import _unsafe_reason
from .spend import load_spend_policy

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_SPEND = ROOT / "config" / "spend.toml"
T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
BUDGET = Budget(
    soft_max_seconds=14_400,
    hard_max_seconds=21_600,
    soft_max_cost_usd=Decimal("2.00"),
    hard_max_cost_usd=Decimal("3.00"),
)


# --- the spend policy's soft and hard maximums ---------------------------------------


def test_the_shipped_policy_carries_the_default_budget() -> None:
    policy = load_spend_policy(SHIPPED_SPEND)

    assert Budget.from_policy(policy) == BUDGET
    # The guard is armed from the launch ceilings, at the soft maximum, never past it.
    assert policy.hard_lifetime_seconds == policy.soft_max_seconds
    assert policy.max_estimated_metered_cost_usd == policy.soft_max_cost_usd


# --- the estimate --------------------------------------------------------------------


def test_no_estimate_until_pages_have_been_seen_to_finish() -> None:
    """The first sight of a stage is its anchor: pages done before it, and the model load
    before the first page, never count as this stage's pace."""
    estimator = FinishEstimator()

    first = estimator.update(StageProgress(PERLECTOR, done=30, total=100), T0)
    still = estimator.update(StageProgress(PERLECTOR, done=31, total=100), T0 + timedelta(hours=1))
    paced = estimator.update(
        StageProgress(PERLECTOR, done=34, total=100), T0 + timedelta(seconds=400)
    )

    assert first is not None and first.finishes_at is None and first.reason
    assert still is not None and still.finishes_at is None and still.reason
    assert paced is not None
    assert paced.seconds_per_page == 100.0
    assert paced.finishes_at == T0 + timedelta(seconds=400 + 66 * 100)
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


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runs")
    subprocess.run(
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
        check=False,
    )
    return root / "estimate"


def test_every_page_counted_stage_is_read_from_its_real_records(fixture_run: Path) -> None:
    progress = RunTreeProgress(fixture_run)

    for stage in PAGE_RECORDS:
        counted = progress.count(stage)
        assert counted.done == counted.total and counted.done > 0, stage
    # Three witnesses read each of the two pages.
    assert progress.count("attestatores").total == 6
    assert progress.sample() is None, "every page-counted stage is sealed"


def test_the_stage_in_progress_is_the_first_unsealed_one(fixture_run: Path, tmp_path: Path) -> None:
    run = tmp_path / "estimate"
    shutil.copytree(fixture_run, run)
    perlector = run / "4_perlector" / "artifacts"
    shutil.rmtree(perlector / "stage-seal")
    next(iter(sorted((perlector / "page-accounting").iterdir()))).unlink()

    assert RunTreeProgress(run).sample() == StageProgress(PERLECTOR, done=1, total=2)


# --- the deadline-at-risk notice -----------------------------------------------------


class Ticks:
    """A run whose progress, clock and deadline the test moves by hand."""

    def __init__(self, deadline: datetime) -> None:
        self.now = T0
        self.progress = StageProgress(PERLECTOR, done=10, total=100)
        self.deadline = deadline
        self.sent: list[str] = []
        self.outcome = NotifyOutcome(True, True, "delivered")

    def send(self, message: str) -> NotifyOutcome:
        self.sent.append(message)
        return self.outcome

    def watch(self, path: Path, *, send: bool = True) -> DeadlineWatch:
        return DeadlineWatch(
            run_id="run-1",
            pod_id="pod123",
            path=path,
            sample=lambda: self.progress,
            budget=BUDGET,
            budget_problem=None,
            hourly_usd=Decimal("2.00"),
            deadline=lambda: (self.deadline, "the pod guard's deadline"),
            send=self.send if send else None,
            now=lambda: self.now,
        )

    def at(self, minutes: float, done: int) -> None:
        self.now = T0 + timedelta(minutes=minutes)
        self.progress = StageProgress(PERLECTOR, done=done, total=100)


def test_the_notice_goes_once_per_crossing_of_a_deadline(tmp_path: Path) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=3))
    path = tmp_path / "pod-run-report-estimate.json"
    watch = run.watch(path)

    watch.tick()  # first sight: no pace yet
    run.at(10, 12)  # 300 s a page: 88 pages left is over seven hours
    watch.tick()
    run.at(20, 14)
    watch.tick()
    assert len(run.sent) == 1

    run.deadline = T0 + timedelta(hours=10)  # the lead extended by hand
    run.at(30, 16)
    watch.tick()
    assert len(run.sent) == 1

    run.at(180, 17)  # the pace collapses: past the new deadline too
    watch.tick()
    run.at(181, 17)
    watch.tick()
    assert len(run.sent) == 2

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == ESTIMATE_SCHEMA
    assert record["estimate"]["stage"] == PERLECTOR
    assert record["at_risk"] is True
    assert [notice["deadline"] for notice in record["notices"]] == [
        "2026-10-03T12:00:00Z",
        "2026-10-03T19:00:00Z",
    ]
    assert all(notice["delivered"] for notice in record["notices"])


def test_a_notice_that_did_not_arrive_is_recorded_and_retried_a_bounded_number_of_times(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=1))
    run.outcome = NotifyOutcome(True, False, "notify.sh exited 1")
    path = tmp_path / "estimate.json"
    watch = run.watch(path)

    watch.tick()
    for minute in range(1, 8):
        run.at(minute * 10, 10 + 2 * minute)
        watch.tick()

    assert len(run.sent) == NOTICE_ATTEMPTS
    notices = json.loads(path.read_text(encoding="utf-8"))["notices"]
    assert [notice["delivered"] for notice in notices] == [False] * NOTICE_ATTEMPTS
    assert all("notify.sh exited 1" in notice["outcome"] for notice in notices)
    assert "NOT DELIVERED" in capsys.readouterr().err
    assert watch.summary()["notices"] == notices


def test_a_notice_with_nowhere_to_go_is_recorded_once(tmp_path: Path) -> None:
    run = Ticks(deadline=T0 + timedelta(hours=1))
    path = tmp_path / "estimate.json"
    watch = run.watch(path, send=False)

    watch.tick()
    for minute in range(1, 5):
        run.at(minute * 10, 10 + 2 * minute)
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
    run.at(10, 12)
    watch.tick()

    assert run.sent, "a failed write never stops the notice"
    assert "could not be written" in capsys.readouterr().err
    assert watch.summary()["write_failures"] == 2


def test_the_notice_names_both_maximums_the_finish_the_cost_and_the_manual_route() -> None:
    estimator = FinishEstimator()
    estimator.update(StageProgress(PERLECTOR, done=10, total=100), T0)
    estimate = estimator.update(
        StageProgress(PERLECTOR, done=12, total=100), T0 + timedelta(minutes=10)
    )
    assert estimate is not None and estimate.finishes_at is not None

    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id="pod123",
        estimate=estimate,
        deadline=T0 + timedelta(hours=3),
        launch_deadline=T0 + timedelta(hours=3),
        budget=BUDGET,
        budget_problem=None,
        hourly_usd=Decimal("2.00"),
    )

    assert "soft max 4 h / $2.00" in message
    assert "hard max 6 h / $3.00" in message
    assert "2026-10-03 16:30 UTC" in message  # 09:10 and 88 pages at 300 s
    # 16:30 plus 20 minutes to bring results home, past a 12:00 deadline: 4 h 50 min.
    assert "4.8 h" in message and "$9.67" in message
    assert "fits under the hard max: no" in message
    assert "/workspace/private/.pod_guard" in message and "deadline-pod123" in message
    assert "\n" not in message
    assert _unsafe_reason(message) is None


def test_a_missing_budget_or_price_is_named_not_guessed() -> None:
    estimator = FinishEstimator()
    estimator.update(StageProgress(PERLECTOR, done=10, total=100), T0)
    estimate = estimator.update(
        StageProgress(PERLECTOR, done=12, total=100), T0 + timedelta(minutes=10)
    )
    assert estimate is not None

    message = deadline_at_risk_message(
        run_id="run-1",
        pod_id=None,
        estimate=estimate,
        deadline=T0 + timedelta(hours=3),
        launch_deadline=T0 + timedelta(hours=3),
        budget=None,
        budget_problem="spend policy is unconfigured",
        hourly_usd=None,
    )

    assert "soft and hard max unknown (spend policy is unconfigured)" in message
    assert "cost unknown (no --hourly-usd)" in message
    assert _unsafe_reason(message) is None


def test_the_guard_deadline_is_read_only_as_epoch_seconds(tmp_path: Path) -> None:
    guard = tmp_path / ".pod_guard"
    guard.mkdir()
    (guard / "deadline-pod123").write_text("1790000000\n", encoding="ascii")
    (guard / "deadline-pod456").write_text("soon\n", encoding="ascii")

    assert finish_estimate.guard_deadline(tmp_path, "pod123") == datetime.fromtimestamp(
        1_790_000_000, UTC
    )
    assert finish_estimate.guard_deadline(tmp_path, "pod456") is None
    assert finish_estimate.guard_deadline(tmp_path, None) is None
