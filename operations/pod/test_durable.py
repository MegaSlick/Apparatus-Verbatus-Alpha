"""Failure-path tests for durable directory publication."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from common.durability import sync_directory

from . import durable


def test_strict_sync_propagates_a_directory_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_open(_path: Path, _flags: int) -> int:
        raise OSError("injected directory open failure")

    monkeypatch.setattr(durable.os, "open", refuse_open)

    sync_directory(tmp_path)
    with pytest.raises(OSError, match="injected directory open failure"):
        sync_directory(tmp_path, strict=True)


def test_strict_sync_propagates_a_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_fsync(_descriptor: int) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(durable.os, "fsync", refuse_fsync)

    sync_directory(tmp_path)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        sync_directory(tmp_path, strict=True)


def test_exclusive_write_refuses_a_second_create_and_keeps_the_first_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create *is* the exclusion: two holders of one grant cannot both proceed."""

    modes: list[int] = []
    real_fchmod = durable.os.fchmod

    def observe_fchmod(descriptor: int, mode: int) -> None:
        modes.append(mode)
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(durable.os, "fchmod", observe_fchmod)
    target = tmp_path / "claims" / "grant.json"
    durable.exclusive_write(target, b'{"grant":"one"}')
    assert modes == [0o600], "this module must set the money record's mode, not inherit it"

    with pytest.raises(FileExistsError):
        durable.exclusive_write(target, b'{"grant":"two"}')

    assert target.read_bytes() == b'{"grant":"one"}'
    # Exact mode, and the call that sets it. `& 0o077 == 0` passed with
    # `os.fchmod` deleted, because `tempfile.mkstemp` already creates at 0600 --
    # so the assertion measured the standard library rather than this module.
    # The mode is the property that matters and is pinned exactly; the observer
    # below pins the line that establishes it rather than inherits it.
    assert target.stat().st_mode & 0o777 == 0o600, "a money record must be owner-only"


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
    # The absence above is true from the environment -- the injected failure
    # fires before `os.link`, so no target was ever created and this assertion
    # held with the `finally` unlink deleted. What the name promises is that
    # nothing half-written is left behind, and the leftover a failed write
    # actually strands is the temporary, so that is what is asserted.
    assert list(target.parent.iterdir()) == [], "a failed write stranded its temporary"
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


def test_exclusive_write_publishes_only_after_the_payload_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent reader must never observe a half-written final record."""

    target = tmp_path / "claims" / "grant.json"
    real_fsync = durable.os.fsync
    fsync_calls = 0

    def observe_before_sync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            assert not target.exists(), "the final path was visible before its bytes were durable"
        real_fsync(descriptor)

    monkeypatch.setattr(durable.os, "fsync", observe_before_sync)
    durable.exclusive_write(target, b'{"grant":"one"}')

    # Without this the test passes when nothing is fsynced at all: the callback
    # would never run, the ordering assertion inside it would never be reached,
    # and only the read-back below would be checked -- green while every grant
    # record became a file a power loss can take.
    assert fsync_calls >= 1, "the payload was published without ever being fsynced"
    assert target.read_bytes() == b'{"grant":"one"}'


def test_a_filesystem_that_refuses_hard_links_is_named_rather_than_an_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names where the directory was put, not the syscall that failed.

    Every other link site in this repository (`common/runtree/store.py`,
    `gold/core.py`, `operations/corpus/cache.py`,
    `operations/operator/backup.py`) already names EPERM/EOPNOTSUPP/ENOSYS this
    way; this one did not, and it is the site a pod's preflight evidence goes
    through onto a network volume -- exactly the kind of mount that answers
    them. Unnamed, it surfaced as a serving failure for what is a filesystem
    capability problem.
    """

    def refuse_link(_source: str, _target: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(durable.os, "link", refuse_link)

    with pytest.raises(durable.HardLinkUnsupported) as refused:
        durable.exclusive_write(tmp_path / "evidence" / "receipt.json", b"{}")

    detail = str(refused.value)
    assert "refuses hard links" in detail
    assert str(tmp_path / "evidence") in detail
    assert isinstance(refused.value, OSError), "existing callers catch OSError"
    # The temporary is still cleaned up: a refusal must not leave debris beside
    # the name it could not publish.
    assert list((tmp_path / "evidence").iterdir()) == []


def test_an_already_published_name_is_still_a_file_exists_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`FileExistsError` is an `OSError` too, and it keeps its own branch.

    That branch syncs the directory entry before re-raising, which is what
    makes the retry path honest; a wide `except OSError` placed ahead of it
    would take the exception away from it silently.
    """

    synced: list[Path] = []
    real_sync = durable.sync_directory

    def observe(path: Path, *, strict: bool = False) -> None:
        synced.append(Path(path))
        real_sync(path, strict=strict)

    target = tmp_path / "claims" / "grant.json"
    durable.exclusive_write(target, b'{"grant":"one"}')
    monkeypatch.setattr(durable, "sync_directory", observe)

    with pytest.raises(FileExistsError):
        durable.exclusive_write(target, b'{"grant":"two"}')

    assert synced == [target.parent]
    assert target.read_bytes() == b'{"grant":"one"}'
