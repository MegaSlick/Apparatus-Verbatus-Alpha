"""A copy of a shared fixture run is the tree a fresh run at the copy's place would write.

Tests that only need a sealed fixture run take a copy from the `orchestrated_run` fixture
instead of running the orchestrator themselves; this is what makes that substitution sound.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from conftest import file_bytes_snapshot, run_orchestrator


def _shape(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under `root` with its file type, permission bits and link count.

    The run-tree readers refuse a hard-linked file, so a copy must not add or drop a link.
    """
    return {
        str(path.relative_to(root)): (path.lstat().st_mode, path.lstat().st_nlink)
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize(("scenario", "expected_exit"), [("page-unbroken", 0), ("page-review", 3)])
def test_a_copied_fixture_run_is_byte_for_byte_a_fresh_run_in_its_place(
    orchestrated_run, tmp_path, scenario, expected_exit
):
    copied = orchestrated_run(tmp_path / "copied" / "runs", "r", scenario, expected_exit)
    fresh = tmp_path / "fresh" / "runs"
    assert run_orchestrator(fresh, "r", scenario).returncode == expected_exit

    assert file_bytes_snapshot(copied) == file_bytes_snapshot(fresh)
    assert _shape(copied) == _shape(fresh)
    assert all(not stat.S_ISLNK(mode) for mode, _ in _shape(fresh).values())
