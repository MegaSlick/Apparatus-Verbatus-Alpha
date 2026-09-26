"""Failure-path tests for durable publication."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from common import durability
from common.durability import sync_directory

WRITERS = pytest.mark.parametrize(
    "write", [durability.atomic_create, durability.atomic_replace], ids=("create", "replace")
)


@pytest.mark.parametrize("call", ["open", "fsync"])
def test_strict_sync_propagates_either_directory_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    def refuse(*_arguments: object) -> None:
        raise OSError(f"injected directory {call} failure")

    monkeypatch.setattr(durability.os, call, refuse)

    sync_directory(tmp_path)
    with pytest.raises(OSError, match=f"injected directory {call} failure"):
        sync_directory(tmp_path, strict=True)


@WRITERS
@pytest.mark.parametrize("failure", [OSError("injected fsync failure"), KeyboardInterrupt()])
def test_an_interrupted_write_leaves_the_old_state_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, failure: BaseException
) -> None:
    """An empty file at a claim's address would block the retry meant to succeed."""

    target = tmp_path / "grant.json"

    def refuse_fsync(_descriptor: int) -> None:
        raise failure

    monkeypatch.setattr(durability.os, "fsync", refuse_fsync)
    with pytest.raises(type(failure)):
        write(target, b'{"grant":"one"}')
    monkeypatch.undo()

    assert list(tmp_path.iterdir()) == [], "a failed write stranded its temporary"
    write(target, b'{"grant":"one"}')
    assert target.read_bytes() == b'{"grant":"one"}'


@WRITERS
def test_strict_refuses_an_unproved_directory_entry_after_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write
) -> None:
    target = tmp_path / "grant.json"

    def unsyncable(_path: Path, *, strict: bool = False) -> None:
        assert strict is True
        raise OSError("directory fsync refused")

    monkeypatch.setattr(durability, "sync_directory", unsyncable)
    with pytest.raises(durability.PublishedUnsettled, match="directory fsync refused"):
        write(target, b'{"grant":"one"}')

    # The bytes are published, so the caller must not report them absent.
    assert target.read_bytes() == b'{"grant":"one"}'
    assert list(tmp_path.iterdir()) == [target]


@WRITERS
def test_the_final_name_appears_only_after_the_payload_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write
) -> None:
    target = tmp_path / "grant.json"
    real_fsync = durability.os.fsync
    fsync_calls = 0

    def observe_before_sync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            assert not target.exists(), "the final path was visible before its bytes were durable"
        real_fsync(descriptor)

    monkeypatch.setattr(durability.os, "fsync", observe_before_sync)
    write(target, b'{"grant":"one"}')

    assert fsync_calls >= 1, "the payload was published without ever being fsynced"
    assert target.read_bytes() == b'{"grant":"one"}'


def test_a_filesystem_that_refuses_hard_links_is_named_rather_than_an_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_link(_source: str, _target: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(durability.os, "link", refuse_link)

    with pytest.raises(durability.HardLinkUnsupported) as refused:
        durability.atomic_create(tmp_path / "receipt.json", b"{}")

    assert "refuses hard links" in str(refused.value)
    assert str(tmp_path) in str(refused.value)
    assert list(tmp_path.iterdir()) == []


def test_an_existing_name_is_a_file_exists_error_after_its_entry_is_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry path reports success on the winner's bytes, so it must prove them durable."""

    synced: list[Path] = []
    real_sync = durability.sync_directory

    def observe(path: Path, *, strict: bool = False) -> None:
        synced.append(Path(path))
        real_sync(path, strict=strict)

    target = tmp_path / "grant.json"
    durability.atomic_create(target, b'{"grant":"one"}')
    monkeypatch.setattr(durability, "sync_directory", observe)

    with pytest.raises(FileExistsError):
        durability.atomic_create(target, b'{"grant":"two"}')

    assert synced == [target.parent]
    assert target.read_bytes() == b'{"grant":"one"}'


def test_a_replaced_file_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "descriptor.json"
    target.write_bytes(b"old")
    target.chmod(0o644)
    durability.atomic_replace(target, b"new")
    assert target.stat().st_mode & 0o777 == 0o600


def test_an_interrupt_after_the_link_leaves_the_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = durability.os.link

    def link_then_interrupt(source: str, destination: Path) -> None:
        real_link(source, destination)
        raise KeyboardInterrupt

    target = tmp_path / "grant.json"
    monkeypatch.setattr(durability.os, "link", link_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        durability.atomic_create(target, b'{"grant":"one"}')
    assert target.read_bytes() == b'{"grant":"one"}'
    assert list(tmp_path.iterdir()) == [target]


def test_a_temporary_that_cannot_be_created_is_not_a_taken_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def taken(**_keywords: object) -> None:
        raise FileExistsError(17, "File exists")

    monkeypatch.setattr(durability.tempfile, "mkstemp", taken)
    with pytest.raises(OSError) as failure:
        durability.atomic_create(tmp_path / "grant.json", b"{}")
    assert not isinstance(failure.value, FileExistsError)


def test_a_taken_name_whose_entry_cannot_be_proved_is_not_reported_as_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that compares on FileExistsError would report an unproved reuse as success."""

    target = tmp_path / "grant.json"
    durability.atomic_create(target, b'{"grant":"one"}')

    def unsyncable(_path: Path, *, strict: bool = False) -> None:
        raise OSError("directory fsync refused")

    monkeypatch.setattr(durability, "sync_directory", unsyncable)
    with pytest.raises(durability.PublishedUnsettled) as refused:
        durability.atomic_create(target, b'{"grant":"one"}')
    assert not isinstance(refused.value, FileExistsError)
