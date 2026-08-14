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
