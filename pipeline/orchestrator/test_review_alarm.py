"""A run holding more of its pages than its sealed review policy allows says so at its stop.

The fixture's `page-review` scenario holds one of its two pages after the
Recensor: a share of 1/2, far above the committed 1/50, so the run stops before
export and its report opens with the systemic alarm. Under a policy sealed at
exactly 1/2 the same holds are a person's to decide one by one, and no alarm
is raised. A person's advance may pass the systemic stop, and the alarm goes
with the run: it is said again at the advance, and the export carries it as a
reason the terminal report names.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from common.contracts.outcomes import systemic_reason
from common.contracts.stages import ARMARIUM
from common.page_review import held_pages_after_review
from common.review_policy import alarm_line, load_review_policy
from common.runtree.store import RunTree
from common.stage import EXIT_HELD
from conftest import HELD_RECENSOR_STOP, advance_held_recensor

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


def _review_config(tmp_path: Path, share: str) -> Path:
    path = tmp_path / "review.toml"
    path.write_text(f'[review]\nmax_held_page_share = "{share}"\n', encoding="utf-8")
    return path


def test_a_held_share_above_the_sealed_limit_stops_the_run_with_the_systemic_alarm(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root)
    assert result.returncode == EXIT_HELD, result.stderr
    lines = result.stdout.splitlines()
    [stop] = [index for index, line in enumerate(lines) if HELD_RECENSOR_STOP in line]
    [line] = [line for line in lines if "systemic:" in line]
    # The alarm's counts are the tree's own: page 2 of the two reviewed pages.
    assert held_pages_after_review(RunTree(root, "r")) == ([2], 2)
    assert line == alarm_line("r", [2], 2, load_review_policy())
    assert line.startswith(ALARM)
    assert "more than the sealed limit of 1/50" in line and "(held pages: 2)" in line
    # The report opens with it, straight after the stop.
    assert lines[stop + 1] == line


def test_the_stop_record_names_the_systemic_alarm_for_a_caller_without_the_transcript(
    tmp_path,
):
    """The pod route reads the alarm from here to send it to the phone."""
    stop = tmp_path / "stop.json"
    result = _orchestrate(tmp_path / "runs", "--stop-record", str(stop))
    assert result.returncode == EXIT_HELD, result.stderr
    [line] = [line for line in result.stdout.splitlines() if "systemic:" in line]
    assert json.loads(stop.read_text(encoding="utf-8"))["systemic"] == line


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
    assert _orchestrate(root).returncode == EXIT_HELD
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
    assert _orchestrate(root).returncode == EXIT_HELD
    advance_held_recensor(root, "r")
    result = _orchestrate(root, "--from", "recensor", "--to", "armarium")
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
