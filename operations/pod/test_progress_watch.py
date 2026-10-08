"""The progress check pod_run runs on each liveness tick, and the line the pod guard reads.

A stage counted in pages is judged by its pages: too few over ten minutes is slow, none
for too long is stalled. Any other stage is judged by new output. The clock is a test
clock, so nothing here waits.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.contracts.stages import DESIGNATOR, PERLECTOR
from common.cpus import usable_cpus

from . import progress_watch
from .finish_estimate import StageProgress
from .progress_watch import (
    ExpectedRate,
    ProgressTicker,
    ProgressWatch,
    TickSample,
    bootstrap_step,
    expected_rates,
    transcript_stage,
)

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Run:
    """A watch over one fake run: set `pages` and `hint`, then `tick`."""

    def __init__(self, tmp_path: Path, expected: dict[str, ExpectedRate] | None = None) -> None:
        self.clock = Clock()
        self.pages = 0
        self.stage: str | None = PERLECTOR
        self.hint: str | None = None
        self.mark = 0
        self.record = tmp_path / "report-progress.json"
        self.line = tmp_path / "progress-pod123"
        self.watch = ProgressWatch(
            run_id="run-1",
            path=self.record,
            guard_line=self.line,
            expected=expected or {},
            expected_problems=[],
            stage_hint=lambda: self.hint,
            count=lambda stage: StageProgress(stage, 0, 10),
            change=lambda: self.mark,
            now=self.clock.now,
        )

    def tick(self, after: float = 15) -> list[str]:
        self.clock.advance(after)
        sample = None if self.stage is None else StageProgress(self.stage, self.pages, 100)
        self.watch.tick(sample)
        return self.line.read_text(encoding="ascii").rstrip("\n").split(" ", 4)

    def json(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))


PERLECTOR_PLAN = {PERLECTOR: ExpectedRate(60.0, 600, "test plan")}


def test_the_real_configuration_plans_the_perlector_and_surya_rates() -> None:
    rates, problems = expected_rates(
        models_config=ROOT / "config" / "models-real.toml",
        serving_recipes_config=ROOT / "config" / "serving_recipes_real.toml",
        decoding_config=ROOT / "config" / "decoding.toml",
        tier="generic-80gb-plus",
    )
    assert problems == []
    # 12,288 answer tokens at 33 s per 441 is 920 s a call, four calls at once.
    assert rates[PERLECTOR].seconds_per_page == 920 / 4
    assert "920 s a page over 4 calls" in rates[PERLECTOR].source
    # Surya's real row runs 8 threads a runner; the Designator sizes runners by
    # the usable CPUs, so the planned rate follows this host.
    runners = max(1, usable_cpus() // 8)
    assert rates[DESIGNATOR].seconds_per_page == 60 / runners
    assert f"over {runners} runner processes" in rates[DESIGNATOR].source
    assert rates[DESIGNATOR].startup_seconds == 600


def test_an_unreadable_roster_leaves_every_stage_to_its_own_pace(tmp_path: Path) -> None:
    rates, problems = expected_rates(
        models_config=tmp_path / "absent.toml",
        serving_recipes_config=ROOT / "config" / "serving_recipes_real.toml",
        decoding_config=ROOT / "config" / "decoding.toml",
        tier="generic-80gb-plus",
    )
    assert rates == {}
    assert len(problems) == 1


def test_a_stage_keeping_its_planned_pace_is_ok(tmp_path: Path) -> None:
    run = Run(tmp_path, PERLECTOR_PLAN)
    for _ in range(80):
        run.pages += 1
        line = run.tick(60)
        assert line[2] == "ok"
        assert line[0] == line[1]
    record = run.json()
    assert record["schema"] == "pod-run-progress.v1"
    assert record["stage"] == PERLECTOR and record["measure"] == "pages"
    assert record["expected_pages_per_minute"] == 1.0
    assert record["findings"] == []


def test_fewer_than_half_the_expected_pages_over_ten_minutes_is_slow(tmp_path: Path) -> None:
    run = Run(tmp_path, PERLECTOR_PLAN)
    for _ in range(12):
        run.pages += 1
        run.tick(60)
    # Then a page every five minutes: 0.2 a minute against 1.0 expected, never quiet
    # for longer than the ten minutes a page may take.
    statuses = []
    for _ in range(4):
        run.pages += 1
        statuses.append(run.tick(300))
    assert [line[2] for line in statuses][-1] == "slow"
    assert statuses[-1][3] == "page-rate"
    last_ok = int(statuses[-1][1])
    assert int(statuses[-1][0]) - last_ok >= 300
    assert run.json()["findings"][0].startswith("perlector: 0.20 pages a minute")


def test_no_new_page_for_longer_than_allowed_is_stalled(tmp_path: Path) -> None:
    run = Run(tmp_path, {PERLECTOR: ExpectedRate(300.0, 600, "test plan")})
    run.pages = 1
    run.tick()
    run.pages = 2
    run.tick()
    # Allowed: max(10 min, 3 x 300 s) = 900 s. Before then the empty ten minutes are
    # only slow.
    assert run.tick(899)[2:4] == ["slow", "page-rate"]
    line = run.tick(2)
    assert line[2] == "stalled" and line[3] == "page-quiet"
    assert line[4].startswith("no new page for 901 s, 900 s allowed")


def test_the_first_page_may_take_the_startup_time_and_three_pages(tmp_path: Path) -> None:
    run = Run(tmp_path, PERLECTOR_PLAN)
    run.tick()
    assert run.tick(600 + 180)[2] == "ok"
    line = run.tick(1)
    assert line[2] == "stalled" and line[3] == "first-page"


def test_without_a_plan_the_first_page_may_take_thirty_minutes(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.tick()
    assert run.tick(1800)[2] == "ok"
    assert run.tick(1)[2] == "stalled"


def test_without_a_plan_a_stage_is_held_to_its_own_pace(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.tick()
    for _ in range(20):
        run.pages += 1
        assert run.tick(30)[2] == "ok"
    # Its own pace is a page every 30 s: ten empty minutes are slow, and the eleventh
    # minute without a page is past the limit.
    assert run.json()["own_seconds_per_page"] == 30.0
    assert run.tick(600)[2] == "slow"
    assert run.tick(1)[2] == "stalled"


def test_the_transcript_names_the_stage_before_its_first_page(tmp_path: Path) -> None:
    run = Run(tmp_path, PERLECTOR_PLAN)
    run.stage = None
    run.hint = PERLECTOR
    run.tick()
    record = run.json()
    assert record["stage"] == PERLECTOR and record["measure"] == "pages"
    assert record["checks"][0]["name"] == "first-page"


def test_a_stage_not_counted_in_pages_is_judged_by_its_output(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.stage = None
    run.hint = "coniector"
    run.tick()
    assert run.tick(14 * 60)[2] == "ok"
    line = run.tick(60)
    assert line[2] == "stalled" and line[3] == "output"
    run.mark += 1
    assert run.tick()[2] == "ok"


def test_stage_rates_name_each_page_stage_seen(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.stage = DESIGNATOR
    run.tick()
    run.pages = 10
    run.tick(120)
    run.stage = PERLECTOR
    run.pages = 0
    run.tick()
    run.pages = 3
    run.tick(60)
    assert run.watch.stage_rates() == [
        {
            "stage": DESIGNATOR,
            "pages_done": 10,
            "pages_total": 100,
            "pages_observed": 10,
            "seconds": 120,
            "pages_per_minute": 5.0,
        },
        {
            "stage": PERLECTOR,
            "pages_done": 3,
            "pages_total": 100,
            "pages_observed": 3,
            "seconds": 60,
            "pages_per_minute": 3.0,
        },
    ]


def test_a_failing_tick_never_raises_and_is_counted(tmp_path: Path) -> None:
    run = Run(tmp_path)

    def broken(stage: str) -> StageProgress:
        raise RuntimeError("unreadable tree")

    run.stage = None
    run.hint = PERLECTOR
    run.watch._count = broken  # type: ignore[assignment]
    assert run.watch.tick(None) == "ok"
    assert run.watch.failures == 1
    assert "unreadable tree" in (run.watch.last_failure or "")


def test_the_guard_line_carries_only_plain_characters(tmp_path: Path) -> None:
    line = tmp_path / "progress-pod123"
    progress_watch.write_progress_line(
        line,
        now=100.9,
        last_ok=50.2,
        status="stalled",
        check="page quiet",
        detail="a\nb;$(rm -rf)`x`",
    )
    assert line.read_text(encoding="ascii") == "100 50 stalled page-quiet abrm -rfx\n"


def test_a_shared_sample_is_taken_once_and_its_failure_raises_for_each_reader() -> None:
    calls = []

    def sample() -> StageProgress:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("torn record")
        return StageProgress(PERLECTOR, 1, 2)

    shared = TickSample(sample)
    assert shared.refresh() == StageProgress(PERLECTOR, 1, 2)
    assert shared() == shared() == StageProgress(PERLECTOR, 1, 2)
    assert shared.refresh() is None
    with pytest.raises(RuntimeError, match="torn record"):
        shared()
    assert len(calls) == 2


def test_the_transcript_stage_is_the_last_started_and_not_ended(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.log"
    assert transcript_stage(transcript) is None
    transcript.write_text(
        "run r: designator started at 2026-10-08T10:00:00Z\n"
        "run r: designator ended, exit 0, after 300s\n"
        "run r: volume sync after designator started\n"
        "run r: perlector started at 2026-10-08T10:05:00Z\n"
        "reading page 1\n",
        encoding="utf-8",
    )
    assert transcript_stage(transcript) == PERLECTOR
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("run r: perlector ended, exit 0, after 900s\n")
    assert transcript_stage(transcript) is None


def test_the_bootstrap_step_is_the_first_not_completed(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    assert bootstrap_step(journal) == "starting"
    journal.write_text(json.dumps({"completed": ["repository", "configuration"]}))
    assert bootstrap_step(journal) == "cuda-compat"


def test_the_ticker_says_bootstrapping_and_stops_vouching_after_an_hour(tmp_path: Path) -> None:
    clock = Clock()
    line = tmp_path / "progress-pod123"
    step = ["model-store"]
    ticker = ProgressTicker(
        line,
        status="bootstrapping",
        late_status="bootstrapping",
        check="bootstrap",
        step=lambda: step[0],
        now=clock.now,
        interval_seconds=60,
    )
    began = int(clock.now().timestamp())
    ticker.write()
    assert line.read_text().split(" ", 4)[:4] == [
        str(began),
        str(began),
        "bootstrapping",
        "bootstrap",
    ]
    clock.advance(3600)
    ticker.write()
    assert line.read_text().split()[1] == str(began + 3600)
    clock.advance(600)
    ticker.write()
    fields = line.read_text().split(" ", 4)
    assert fields[1] == str(began + 3600), "a step past its hour no longer moves the last ok"
    assert fields[4].startswith("model-store for 4200 s")
    step[0] = "chair-cache"
    ticker.write()
    assert line.read_text().split()[1] == fields[0], "a new step counts as working again"


def test_the_ticker_writes_from_its_thread_until_stopped(tmp_path: Path) -> None:
    line = tmp_path / "progress-pod123"
    written = threading.Event()
    clock = Clock()

    def step() -> str:
        if line.exists():
            written.set()
        return "preflight"

    with ProgressTicker(
        line,
        status="bootstrapping",
        late_status="bootstrapping",
        check="bootstrap",
        step=step,
        now=clock.now,
        interval_seconds=0.01,
    ):
        assert written.wait(5)
    assert "preflight" in line.read_text()


def test_a_ticker_with_no_guard_writes_nothing(tmp_path: Path) -> None:
    with ProgressTicker(
        None,
        status="ok",
        late_status="stalled",
        check="final-sync",
        step=lambda: "sync",
        now=Clock().now,
        interval_seconds=0.01,
    ):
        pass
    assert list(tmp_path.iterdir()) == []
