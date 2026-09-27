from pathlib import Path

import pytest

from operations.pod.durable import atomic_write, exclusive_write


@pytest.mark.parametrize("write", [atomic_write, exclusive_write])
def test_pod_evidence_writer_refuses_symlinked_directory(write, tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)

    with pytest.raises(OSError, match="symlinked directory"):
        write(tmp_path / "alias" / "report.json", b"new evidence")

    assert list(actual.iterdir()) == []
