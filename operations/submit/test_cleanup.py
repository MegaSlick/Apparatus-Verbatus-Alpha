"""Synthetic cleanup-drill tests for the submit door's data-handling boundary."""

from __future__ import annotations

import pytest

from operations.submit.cleanup import CleanupDrillRefusal, verify_synthetic_cleanup


def test_synthetic_cleanup_drill_measures_all_declared_observable_bounds(tmp_path):
    """A synthetic disposal leaves no checked target, temp, log, or volume object."""
    private_target = tmp_path / "private" / "synthetic-page.bin"
    temporary = tmp_path / "tmp" / "render.tmp"
    log = tmp_path / "logs" / "door.log"
    for path in (private_target, temporary, log):
        path.parent.mkdir(parents=True, exist_ok=True)
    private_target.write_bytes(b"synthetic page bytes")
    temporary.write_bytes(b"synthetic temporary bytes")
    log.write_bytes(b"cleanup completed without source content\n")

    private_target.unlink()
    temporary.unlink()

    result = verify_synthetic_cleanup(
        target_paths=(private_target,),
        temporary_paths=(temporary,),
        log_paths=(log,),
        forbidden_markers=(b"synthetic page bytes", b"synthetic-page.bin"),
        volume_objects=(),
    )

    assert result.target_paths_checked == 1
    assert result.temporary_paths_checked == 1
    assert result.logs_scanned == 1
    assert result.volume_objects_seen == 0


def test_synthetic_cleanup_drill_treats_a_dangling_symlink_as_a_remaining_target(tmp_path):
    target = tmp_path / "private" / "synthetic-page.bin"
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "already-gone.bin")
    temporary = tmp_path / "tmp" / "render.tmp"
    log = tmp_path / "logs" / "door.log"
    log.parent.mkdir()
    log.write_bytes(b"clean log\n")

    with pytest.raises(CleanupDrillRefusal, match="target"):
        verify_synthetic_cleanup(
            target_paths=(target,),
            temporary_paths=(temporary,),
            log_paths=(log,),
            forbidden_markers=(b"synthetic-page.bin",),
            volume_objects=(),
        )


def test_synthetic_cleanup_drill_refuses_an_unmeasured_noop():
    """A drill with no declared target, temp, or log bound cannot pass clean."""
    with pytest.raises(CleanupDrillRefusal, match="no declared target paths"):
        verify_synthetic_cleanup(
            target_paths=(),
            temporary_paths=(),
            log_paths=(),
            forbidden_markers=(b"synthetic-page",),
            volume_objects=None,
        )


@pytest.mark.parametrize("incomplete", ("target", "temporary", "log", "volume"))
def test_synthetic_cleanup_drill_refuses_every_incomplete_observable_bound(tmp_path, incomplete):
    target = tmp_path / "private" / "synthetic.bin"
    temporary = tmp_path / "tmp" / "render.tmp"
    log = tmp_path / "logs" / "door.log"
    for path in (target, temporary, log):
        path.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"clean log\n")

    if incomplete == "target":
        target.write_bytes(b"left behind")
    elif incomplete == "temporary":
        temporary.write_bytes(b"left behind")
    elif incomplete == "log":
        log.write_bytes(b"source-name-left-behind")

    with pytest.raises(CleanupDrillRefusal, match=incomplete):
        verify_synthetic_cleanup(
            target_paths=(target,),
            temporary_paths=(temporary,),
            log_paths=(log,),
            forbidden_markers=(b"source-name-left-behind",),
            volume_objects=("left-behind",) if incomplete == "volume" else (),
        )
