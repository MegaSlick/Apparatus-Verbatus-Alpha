"""`verbatus watch` on synthetic copies of a pod run's report files."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from operations.pod.lease import PodLease

from . import cli, watch
from .errors import ErrorCode, OperatorError

RUN = "demo"
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _write(path: Path, record: dict[str, object]) -> None:
    path.write_text(json.dumps(record), encoding="utf-8")


def _receipts(
    folder: Path,
    *,
    seen: datetime = NOW - timedelta(seconds=20),
    state: str = "running",
    estimate: dict[str, object] | None = None,
    notices: list[dict[str, object]] | None = None,
) -> Path:
    report = folder / f"pod-run-report-{RUN}.json"
    _write(
        report,
        {
            "schema": "pod-run-report.v1",
            "run_id": RUN,
            "state": state,
            "exit_code": None if state == "running" else 0,
            "started_at": _stamp(NOW - timedelta(hours=2)),
            "hard_deadline": _stamp(NOW + timedelta(hours=9)),
            **({} if state == "running" else {"finished_at": _stamp(seen)}),
        },
    )
    _write(
        folder / f"pod-run-report-{RUN}-liveness.json",
        {"schema": "pod-run-liveness.v1", "run_id": RUN, "alive": True, "last_seen": _stamp(seen)},
    )
    (folder / f"pod-run-report-{RUN}-timings.json").write_text(
        json.dumps({"run_id": RUN, "stage": "door", "duration_ms": 180_000, "exit_code": 0}) + "\n",
        encoding="utf-8",
    )
    _write(
        folder / f"pod-run-report-{RUN}-estimate.json",
        {
            "schema": "pod-run-estimate.v1",
            "run_id": RUN,
            "updated_at": _stamp(seen),
            "estimate": estimate
            if estimate is not None
            else {
                "stage": "perlector",
                "pages_done": 120,
                "pages_total": 400,
                "finishes_at": _stamp(NOW + timedelta(hours=3)),
                "reason": None,
            },
            "deadline": _stamp(NOW + timedelta(hours=4)),
            "deadline_source": "the pod guard's deadline",
            "at_risk": False,
            "budget": {
                "soft_max_seconds": 14400,
                "hard_max_seconds": 21600,
                "soft_max_cost_usd": "20.00",
                "hard_max_cost_usd": "30.00",
            },
            "budget_problem": None,
            "hourly_usd": "2.00",
            "notices": notices or [],
        },
    )
    return report


def _show(report: Path, *, lease: Path | None = None, now: datetime = NOW) -> str:
    receipts = watch.read_receipts(RUN, report, lease)
    return "\n".join(watch.render(receipts, now))


def test_a_live_run_shows_progress_finish_deadline_budget_and_spend(tmp_path: Path) -> None:
    report = _receipts(
        tmp_path,
        notices=[
            {
                "type": "deadline-at-risk",
                "at": _stamp(NOW - timedelta(minutes=30)),
                "delivered": True,
                "outcome": "sent",
            }
        ],
    )
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    shown = _show(report)

    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert "STALE" not in shown
    assert "Stage perlector: 120/400 pages." in shown
    assert "This stage finishes about 2030-01-01 15:00 UTC (in 3.0 h)" in shown
    assert "Deadline 2030-01-01 16:00 UTC (in 4.0 h), the pod guard's deadline;" in shown
    assert "soft max 4 h / $20.00, hard max 6 h / $30.00" in shown
    # No lease: counted from pod_run's start, so only a floor.
    assert "Spend to now: at least $4.00 of soft $20.00 / hard $30.00 (2.0 h at $2.00/h" in shown
    assert "Last notice: deadline-at-risk at 2030-01-01 11:30 UTC, delivered" in shown
    assert "Stage runs: door 3 min." in shown


def test_old_copies_are_called_stale_and_dated_not_shown_as_current(tmp_path: Path) -> None:
    report = _receipts(tmp_path, seen=NOW - timedelta(minutes=47))

    first = watch.render(watch.read_receipts(RUN, report, None), NOW)[0]

    lines = watch.render(watch.read_receipts(RUN, report, None), NOW)
    assert first.startswith("STALE liveness: last written 47 min ago")
    assert "Stage perlector: 120/400 pages (as of 2030-01-01 11:13 UTC, 47 min old)." in lines


def test_an_ended_run_is_not_called_stale(tmp_path: Path) -> None:
    report = _receipts(tmp_path, state="complete", seen=NOW - timedelta(hours=1))

    shown = _show(report)

    assert "STALE" not in shown
    assert "pod_run report says complete (exit 0, 2030-01-01 11:00 UTC)" in shown


def test_no_estimate_is_said_to_be_unknown(tmp_path: Path) -> None:
    report = _receipts(
        tmp_path,
        estimate={
            "stage": "perlector",
            "pages_done": 2,
            "pages_total": 400,
            "finishes_at": None,
            "reason": "2 page(s) in 60 s since this stage was first seen",
        },
    )
    assert "Estimated finish: unknown (2 page(s) in 60 s" in _show(report)

    (tmp_path / f"pod-run-report-{RUN}-estimate.json").unlink()
    shown = _show(report)
    assert "Estimated finish: unknown (no estimate copy here)." in shown
    assert "Budget: unknown" in shown


def test_spend_from_a_lease_counts_from_the_pod_creation(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    lease = PodLease(
        lease_id="a" * 32,
        launch_token="b" * 32,
        provider_name="runpod",
        pod_id="pod1",
        volume_id="vol1",
        pod_hourly_usd=Decimal("1.50"),
        volume_hourly_usd=Decimal("0.50"),
        created_at=NOW - timedelta(hours=3),
        started_at=NOW - timedelta(hours=3),
        hard_deadline=NOW + timedelta(hours=3),
        owner_token="c" * 32,
        heartbeat_at=NOW,
        phase="active",
    )
    lease_path = tmp_path / "lease.json"
    _write(lease_path, lease.to_record())

    assert (
        "Spend to now: $6.00 of soft $20.00 / hard $30.00 (3.0 h at $2.00/h pod and volume"
        in _show(report, lease=lease_path)
    )

    tampered = lease.to_record() | {"pod_hourly_usd": "0.01"}
    _write(lease_path, tampered)
    shown = _show(report, lease=lease_path)
    assert "Spend to now: at least" in shown
    assert "Note: the lease lease.json was not used" in shown


def test_another_runs_report_or_none_is_refused(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    with pytest.raises(OperatorError) as missing:
        watch.read_receipts(RUN, tmp_path / "absent.json", None)
    assert missing.value.code is ErrorCode.WATCH_UNREADABLE
    with pytest.raises(OperatorError) as other:
        watch.read_receipts("other", report, None)
    assert other.value.code is ErrorCode.WATCH_UNREADABLE


def test_following_prints_each_change_and_stops_when_the_run_ends(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    printed: list[str] = []
    passes = iter(range(10))

    def sleep(_: float) -> None:
        step = next(passes)
        if step == 1:
            _receipts(
                tmp_path, estimate={"stage": "perlector", "pages_done": 130, "pages_total": 400}
            )
        if step == 2:
            _receipts(tmp_path, state="complete")

    watch.watch(
        RUN,
        report,
        lease_path=None,
        stale_minutes=2,
        interval=30,
        timeout=None,
        printer=printed.append,
        now=lambda: NOW,
        sleep=sleep,
    )

    assert sum(line.startswith("Stage perlector") for line in printed) == 2
    assert printed[-1] == "The run has ended; watch stops."


def test_the_command_reads_the_hand_route_names_in_a_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _receipts(tmp_path)

    code = cli.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "watch",
            "--run-id",
            RUN,
            "--receipts",
            str(tmp_path),
        ]
    )

    assert code == 0
    assert "Stage perlector: 120/400 pages." in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def _estimate_path(folder: Path) -> Path:
    return folder / f"pod-run-report-{RUN}-estimate.json"


def _edit(path: Path, **changes: object) -> None:
    _write(path, json.loads(path.read_text(encoding="utf-8")) | changes)


def test_an_old_estimate_beside_fresh_liveness_is_called_stale(tmp_path: Path) -> None:
    report = _receipts(tmp_path, seen=NOW - timedelta(minutes=50))
    _edit(tmp_path / f"pod-run-report-{RUN}-liveness.json", last_seen=_stamp(NOW))
    _edit(_estimate_path(tmp_path), write_failures=7, last_write_failure="disk full")

    shown = _show(report)

    assert "STALE estimate: written 50 min ago" in shown
    assert "Stage perlector: 120/400 pages (as of 2030-01-01 11:10 UTC, 50 min old)." in shown
    assert "The estimate could not be written 7 times; last: disk full." in shown


def test_a_failing_estimate_is_not_called_an_uncounted_stage(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    _write(
        _estimate_path(tmp_path),
        {
            "schema": "pod-run-estimate.v1",
            "run_id": RUN,
            "updated_at": _stamp(NOW),
            "tick_failures": 40,
            "last_tick_failure": "KeyError: x",
        },
    )

    shown = _show(report)

    assert "the estimate is failing (40 ticks): KeyError: x" in shown
    assert "not counted in pages" not in shown
    assert (
        "the bootstrap's hard deadline from the report, because the estimate names no deadline"
        in shown
    )


def test_an_ended_run_that_holds_the_pod_still_bills(tmp_path: Path) -> None:
    report = _receipts(tmp_path, state="complete", seen=NOW - timedelta(minutes=5))
    _edit(report, held_to_hard_deadline=True, hold_detail="keeping the pod up for the timer")

    shown = _show(report)

    assert "Pod still billing until 2030-01-01 21:00 UTC" in shown
    assert "keeping the pod up for the timer" in shown
    assert "Spend to now: at least $4.00" in shown
    assert "finishes about" not in shown and "Stage perlector" not in shown


def test_the_guard_is_not_claimed_armed(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    _edit(
        _estimate_path(tmp_path),
        deadline_extendable_by_hand=True,
        ignored_deadline_file_values=["12x"],
    )

    shown = _show(report)

    assert "watch cannot tell whether the guard is armed" in shown
    assert "extendable by hand: yes" in shown
    assert "Ignored deadline file values: '12x'." in shown


def test_spend_is_set_against_the_maximums_and_rounded_half_up(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    # 2 h at $0.0025/h is $0.005, which rounds half up to $0.01.
    _edit(_estimate_path(tmp_path), hourly_usd="0.0025")

    assert "at least $0.01 of soft $20.00 / hard $30.00" in _show(report)


def test_unparsable_timings_lines_are_counted(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    with (tmp_path / f"pod-run-report-{RUN}-timings.json").open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "de\n')

    assert "1 timings line(s) could not be read" in _show(report)


def test_following_retries_once_on_a_half_written_copy(tmp_path: Path) -> None:
    report = _receipts(tmp_path)
    whole = report.read_bytes()
    printed: list[str] = []
    passes = iter(range(10))

    def sleep(_: float) -> None:
        step = next(passes)
        if step == 0:
            report.write_bytes(whole[:10])
        if step == 1:
            report.write_bytes(whole)
            _receipts(tmp_path, state="complete")

    watch.watch(
        RUN,
        report,
        lease_path=None,
        stale_minutes=2,
        interval=30,
        timeout=None,
        printer=printed.append,
        now=lambda: NOW,
        sleep=sleep,
    )

    assert printed[-1] == "The run has ended; watch stops."
