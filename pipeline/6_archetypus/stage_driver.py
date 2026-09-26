"""The one subprocess driver the stage's test files share, so a stage script's
argv or added flag needs updating in one place, not every test that drives it.

Test support, not stage code, exactly as `reseal_chain.py`: `run.py` never
imports this, and test modules import it by name (pytest puts the directory
on `sys.path` for them).
"""

import subprocess
import sys
from pathlib import Path

from conftest import programs_through

ROOT = Path(__file__).resolve().parents[2]


def invoke(
    root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_through_recensor(root: Path, run_id: str, scenario: str = "happy") -> None:
    for program in programs_through("recensor"):
        result = invoke(root, run_id, scenario, program)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
