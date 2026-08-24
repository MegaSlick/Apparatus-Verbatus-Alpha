"""Failure-path tests for durable directory publication."""

from __future__ import annotations

from pathlib import Path

import pytest

from . import durable


def test_strict_sync_propagates_a_directory_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_open(_path: Path, _flags: int) -> int:
        raise OSError("injected directory open failure")

    monkeypatch.setattr(durable.os, "open", refuse_open)

    durable.sync_directory(tmp_path)
    with pytest.raises(OSError, match="injected directory open failure"):
        durable.sync_directory(tmp_path, strict=True)


def test_strict_sync_propagates_a_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_fsync(_descriptor: int) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(durable.os, "fsync", refuse_fsync)

    durable.sync_directory(tmp_path)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        durable.sync_directory(tmp_path, strict=True)


def test_exclusive_write_refuses_a_second_create_and_keeps_the_first_bytes(
    tmp_path: Path,
) -> None:
    """The create *is* the exclusion: two holders of one grant cannot both proceed."""

    target = tmp_path / "claims" / "grant.json"
    durable.exclusive_write(target, b'{"grant":"one"}')

    with pytest.raises(FileExistsError):
        durable.exclusive_write(target, b'{"grant":"two"}')

    assert target.read_bytes() == b'{"grant":"one"}'
    assert target.stat().st_mode & 0o077 == 0, "a money record must not be world-readable"


def test_exclusive_write_leaves_no_half_written_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write must not leave a path that now refuses every later claim.

    An empty file at a claim's address would be indistinguishable from a spent
    grant and would block the retry that is supposed to succeed.
    """

    def refuse_fsync(_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    target = tmp_path / "claims" / "grant.json"
    monkeypatch.setattr(durable.os, "fsync", refuse_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        durable.exclusive_write(target, b'{"grant":"one"}')
    monkeypatch.undo()

    assert not target.exists()
    durable.exclusive_write(target, b'{"grant":"one"}')
    assert target.read_bytes() == b'{"grant":"one"}'


def test_exclusive_write_strict_refuses_an_unproved_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Money evidence cannot degrade to best-effort directory durability."""

    target = tmp_path / "claims" / "grant.json"

    def unsyncable(_path: Path, *, strict: bool = False) -> None:
        assert strict is True
        raise OSError("directory fsync refused")

    monkeypatch.setattr(durable, "sync_directory", unsyncable)
    with pytest.raises(OSError, match="directory fsync refused"):
        durable.exclusive_write(target, b'{"grant":"one"}', strict=True)

    # The bytes may already exist, so the caller must still refuse the paid action.
    assert target.read_bytes() == b'{"grant":"one"}'
