"""A run holding more of its pages than its sealed review policy allows says so at its stop.

The fixture's `page-review` scenario holds one of its two pages after the
Recensor: a share of 1/2, far above the committed 1/50, so the run stops before
export and its report opens with the systemic alarm. Under a policy sealed at
exactly 1/2 the same holds are a person's to decide one by one, and no alarm
is raised.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from common.stage import EXIT_HELD

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
    result = _orchestrate(tmp_path / "runs")
    assert result.returncode == EXIT_HELD, result.stderr
    assert "stopped at a held recensor, before the archetypus" in result.stdout
    [line] = [line for line in result.stdout.splitlines() if "systemic:" in line]
    assert line.startswith(ALARM)
    assert "more than the sealed limit of 1/50" in line and "(held pages: 2)" in line
    # Nothing was exported: the alarm is a stop, never a pass.
    assert not (tmp_path / "runs" / "r" / "7_armarium").exists()


def test_a_held_share_at_the_sealed_limit_raises_no_alarm(tmp_path):
    result = _orchestrate(
        tmp_path / "runs", "--review-config", str(_review_config(tmp_path, "1/2"))
    )
    assert result.returncode == EXIT_HELD, result.stderr
    assert "stopped at a held recensor, before the archetypus" in result.stdout
    assert "systemic:" not in result.stdout


def test_a_sealed_run_cannot_be_resumed_under_another_review_policy(tmp_path):
    """The limit is the run's: loosening it later cannot quiet the alarm."""
    root = tmp_path / "runs"
    assert _orchestrate(root).returncode == EXIT_HELD
    result = _orchestrate(root, "--review-config", str(_review_config(tmp_path, "1/2")))
    assert result.returncode not in (0, EXIT_HELD)
    assert "bound to different sealed_config_digests" in result.stderr
