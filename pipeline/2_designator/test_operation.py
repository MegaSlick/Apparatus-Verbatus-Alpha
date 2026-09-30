"""The Designator's one operation is `initial`; any other refuses before writing."""

import subprocess
import sys
from pathlib import Path

import pytest

from common.stage import EXIT_FATAL
from conftest import programs_through

ROOT = Path(__file__).resolve().parents[2]


def _run(program: str, root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "page-unbroken",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("operation", ["recover", "Initial"])
def test_an_operation_other_than_initial_refuses_before_anything_is_written(tmp_path, operation):
    root = tmp_path / "runs"
    for program in programs_through("ink-map"):
        result = _run(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = _run("pipeline/2_designator/run.py", root, "--operation", operation)
    assert result.returncode == EXIT_FATAL, result.stdout
    assert f"--operation {operation!r} is not 'initial'" in result.stderr
    assert not (root / "r" / "2_designator" / "artifacts").exists()
