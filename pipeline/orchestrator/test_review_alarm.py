"""A run holding more of its pages than its sealed review policy allows says so at its stop.

The fixture's `page-review` scenario holds one of its two pages after the
Recensor: a share of 1/2, far above 1/50. The committed policy wants at least
two held pages before it calls a share systemic, so these runs seal a policy of
1/50 from one held page: the run stops before export and its report opens with
the systemic alarm. Under the committed policy, or a policy sealed at exactly
1/2, the same hold is a person's to decide, and no alarm is raised. A person's advance may pass the systemic stop, and the alarm goes
with the run: it is said again at the advance, and the export carries it as a
reason the terminal report names.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.contracts.outcomes import systemic_reason
from common.contracts.stages import ARMARIUM
from common.page_review import held_pages_after_review
from common.review_policy import alarm_line, load_review_policy
from common.runtree.store import RunTree
from common.stage import EXIT_FATAL, EXIT_HELD
from conftest import HELD_RECENSOR_STOP, advance_held_recensor, load_stage

ROOT = Path(__file__).resolve().parents[2]
ALARM = "run r: systemic: 1 of 2 page(s) are held after the recensor"


def _orchestrate(root: Path, *extra: str) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / "pipeline" / "orchestrator" / "run.py"),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        "page-review",
        "--run-id",
        "r",
        "--run-root",
        str(root),
        *extra,
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _review_config(tmp_path: Path, share: str, minimum: int = 1) -> Path:
    path = tmp_path / f"review-{share.replace('/', '-')}-{minimum}.toml"
    path.write_text(
        f'[review]\nmax_held_page_share = "{share}"\nmin_systemic_held_pages = {minimum}\n',
        encoding="utf-8",
    )
    return path


def _alarming(tmp_path: Path) -> tuple[str, str]:
    """The options sealing 1/50 from one held page, so the scenario's one hold sounds it."""
    return ("--review-config", str(_review_config(tmp_path, "1/50")))


def test_a_held_share_above_the_sealed_limit_stops_the_run_with_the_systemic_alarm(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, *_alarming(tmp_path))
    assert result.returncode == EXIT_HELD, result.stderr
    lines = result.stdout.splitlines()
    [stop] = [index for index, line in enumerate(lines) if HELD_RECENSOR_STOP in line]
    [line] = [line for line in lines if "systemic:" in line]
    # The alarm's counts are the tree's own: page 2 of the two reviewed pages.
    assert held_pages_after_review(RunTree(root, "r")) == ([2], 2)
    assert line == alarm_line("r", [2], 2, load_review_policy(_alarming(tmp_path)[1]))
    assert line.startswith(ALARM)
    assert "more than the sealed limit of 1/50" in line and "(held pages: 2)" in line
    # The report opens with it, straight after the stop.
    assert lines[stop + 1] == line


def test_the_stop_record_names_the_systemic_alarm_for_a_caller_without_the_transcript(
    tmp_path,
):
    """The pod route reads the alarm from here to send it to the phone."""
    stop = tmp_path / "stop.json"
    result = _orchestrate(tmp_path / "runs", *_alarming(tmp_path), "--stop-record", str(stop))
    assert result.returncode == EXIT_HELD, result.stderr
    [line] = [line for line in result.stdout.splitlines() if "systemic:" in line]
    assert json.loads(stop.read_text(encoding="utf-8"))["systemic"] == line


def test_a_stop_record_path_that_cannot_be_written_is_refused_before_any_stage(tmp_path):
    """A bad path fails up front, before any stage spends time a record would describe."""
    root = tmp_path / "runs"
    for stop in (tmp_path / "no-such-directory" / "stop.json", tmp_path / "a-file" / "stop.json"):
        (tmp_path / "a-file").write_text("", encoding="utf-8")
        result = _orchestrate(root, "--stop-record", str(stop))
        assert result.returncode == EXIT_FATAL, result.stderr
        assert "is not an existing, writable directory" in result.stderr
        assert not (root / "r").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any directory mode")
def test_a_stop_record_in_an_unwritable_directory_is_refused_before_any_stage(tmp_path):
    root = tmp_path / "runs"
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = _orchestrate(root, "--stop-record", str(locked / "stop.json"))
    finally:
        locked.chmod(0o700)
    assert result.returncode == EXIT_FATAL, result.stderr
    assert "is not an existing, writable directory" in result.stderr
    assert not (root / "r").exists()


def test_a_stop_record_that_cannot_be_written_after_a_refusal_keeps_the_refusal(
    tmp_path, monkeypatch, capsys
):
    """The refusal that ended the selection is the one raised; the lost record is said."""
    orchestrator = load_stage("orchestrator")
    refusal = ContractError("the final seal does not verify")

    def drive(args, names, mode, policy):
        args.systemic_line = ALARM
        raise refusal

    def unwritable(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "_drive", drive)
    monkeypatch.setattr(orchestrator, "atomic_create", unwritable)
    args = argparse.Namespace(stop_record=str(tmp_path / "stop.json"), run_id="r")
    with pytest.raises(ContractError) as raised:
        orchestrator.run_sequence(args, ("armarium",), "manual", {})
    assert raised.value is refusal
    error = capsys.readouterr().err
    assert "could not be written" in error and repr(ALARM) in error


def test_a_stop_record_that_cannot_be_written_is_refused_naming_its_alarm(tmp_path, monkeypatch):
    """A caller reading no record cannot tell no alarm from a lost one, so none is not a stop."""
    orchestrator = load_stage("orchestrator")

    def unwritable(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "atomic_create", unwritable)
    stop = tmp_path / "stop.json"
    args = argparse.Namespace(stop_record=str(stop), run_id="r", systemic_line=ALARM)
    with pytest.raises(ContractError, match="could not be written") as refused:
        orchestrator._record_stop(args, EXIT_HELD, exported=False)
    assert f"exit {EXIT_HELD}, exported False, systemic {ALARM!r}" in str(refused.value)
    assert not stop.exists()


def test_a_refusal_after_the_alarm_still_leaves_a_stop_record_carrying_it(tmp_path, monkeypatch):
    """The alarm already printed reaches the stop record, and the refusal is raised as it was."""
    orchestrator = load_stage("orchestrator")
    refusal = ContractError("the final seal does not verify")

    def drive(args, names, mode, policy):
        args.systemic_line = ALARM
        raise refusal

    monkeypatch.setattr(orchestrator, "_drive", drive)
    stop = tmp_path / "stop.json"
    args = argparse.Namespace(stop_record=str(stop), run_id="r")
    with pytest.raises(ContractError) as raised:
        orchestrator.run_sequence(args, ("armarium",), "manual", {})
    assert raised.value is refusal
    assert json.loads(stop.read_text(encoding="utf-8")) == {
        "schema": "orchestrator-stop.v2",
        "run_id": "r",
        "exit_code": EXIT_FATAL,
        "exported": False,
        "systemic": ALARM,
    }


def test_one_held_page_under_the_committed_policy_raises_no_alarm(tmp_path):
    """One held page is fewer than the committed minimum, though it is half the run."""
    stop = tmp_path / "stop.json"
    result = _orchestrate(tmp_path / "runs", "--stop-record", str(stop))
    assert result.returncode == EXIT_HELD, result.stderr
    assert "stopped at a held recensor, before the archetypus" in result.stdout
    assert "systemic:" not in result.stdout
    assert json.loads(stop.read_text(encoding="utf-8"))["systemic"] is None


def test_a_held_share_at_the_sealed_limit_raises_no_alarm(tmp_path):
    stop = tmp_path / "stop.json"
    result = _orchestrate(
        tmp_path / "runs",
        "--review-config",
        str(_review_config(tmp_path, "1/2")),
        "--stop-record",
        str(stop),
    )
    assert result.returncode == EXIT_HELD, result.stderr
    assert "stopped at a held recensor, before the archetypus" in result.stdout
    assert "systemic:" not in result.stdout
    assert json.loads(stop.read_text(encoding="utf-8"))["systemic"] is None


def test_a_sealed_run_cannot_be_resumed_under_another_review_policy(tmp_path):
    """The limit is the run's: loosening it later cannot quiet the alarm."""
    root = tmp_path / "runs"
    assert _orchestrate(root, *_alarming(tmp_path)).returncode == EXIT_HELD
    result = _orchestrate(root, "--review-config", str(_review_config(tmp_path, "1/2")))
    assert result.returncode not in (0, EXIT_HELD)
    assert "bound to different sealed_config_digests" in result.stderr


def _export_reasons(root: Path) -> list[str]:
    tree = RunTree(root, "r")
    [export] = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    return export["payload"]["aggregate"]["reasons"]


def test_an_advance_past_a_systemic_stop_keeps_the_alarm_in_the_report_and_export(tmp_path):
    """A person may pass a systemic share; the alarm is said again and the export carries it."""
    root = tmp_path / "runs"
    assert _orchestrate(root, *_alarming(tmp_path)).returncode == EXIT_HELD
    advance_held_recensor(root, "r")
    result = _orchestrate(root, *_alarming(tmp_path), "--from", "recensor", "--to", "armarium")
    assert result.returncode == EXIT_HELD, result.stderr
    reason = systemic_reason([2], 2, "1/50")
    lines = result.stdout.splitlines()
    # At the advance check, before the run goes on.
    assert lines.index(f"run r: {reason}") < next(
        index for index, line in enumerate(lines) if "an advance record passes its current" in line
    )
    # In the export's aggregate, so the terminal report names it.
    assert reason in _export_reasons(root)
    assert f"  - {reason}" in lines


def test_an_advance_within_the_sealed_limit_exports_no_systemic_reason(tmp_path):
    root = tmp_path / "runs"
    config = str(_review_config(tmp_path, "1/2"))
    assert _orchestrate(root, "--review-config", config).returncode == EXIT_HELD
    advance_held_recensor(root, "r")
    result = _orchestrate(root, "--review-config", config, "--from", "recensor", "--to", "armarium")
    assert result.returncode == EXIT_HELD, result.stderr
    assert "systemic:" not in result.stdout
    assert not any(reason.startswith("systemic: ") for reason in _export_reasons(root))
