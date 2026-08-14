"""`records.py`'s two file readers, and the protection only one of them had."""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import Callable

import pytest

from operations.operator import records


def _within_ten_seconds(call: Callable[[], object]) -> BaseException:
    """Run a reader that must refuse, and fail rather than hang if it does not."""

    finished = threading.Event()
    raised: list[BaseException] = []

    def attempt() -> None:
        try:
            call()
        except BaseException as error:  # noqa: BLE001 - the refusal is what is asserted
            raised.append(error)
        finally:
            finished.set()

    threading.Thread(target=attempt, daemon=True).start()
    assert finished.wait(10), "the reader blocked on a FIFO instead of refusing it"
    assert raised, "the reader returned instead of refusing a FIFO"
    return raised[0]


def test_reading_a_record_from_a_fifo_refuses_instead_of_hanging(tmp_path: Path):
    """The protection `sha256_file` carries, applied to the other reader.

    A FIFO left at a path a receipt records blocks on the *open* itself, so
    `status` would hang forever having printed nothing — the failure mode
    `sha256_file`'s own docstring describes and guards against by opening
    non-blocking and refusing anything the open descriptor does not call a
    regular file. `_bounded_bytes` read the same recorded paths without it.
    Found by CodeRabbit.
    """
    fifo = tmp_path / "not-a-record"
    os.mkfifo(fifo)
    caught = _within_ten_seconds(lambda: records._bounded_bytes(fifo, "a record"))
    assert isinstance(caught, OSError)
    assert caught.errno == errno.EINVAL


def test_a_digest_of_a_fifo_refuses_the_same_way(tmp_path: Path):
    """Invariant #14's shape: the sibling this was copied from still refuses too."""
    fifo = tmp_path / "not-a-file"
    os.mkfifo(fifo)
    caught = _within_ten_seconds(lambda: records.sha256_file(fifo))
    assert isinstance(caught, OSError)
    assert caught.errno == errno.EINVAL


def test_an_ordinary_record_is_still_read_whole(tmp_path: Path):
    """And the refusal was not bought by refusing good input."""
    path = tmp_path / "record.json"
    path.write_bytes(b'{"ok": true}')
    assert records._bounded_bytes(path, "a record") == b'{"ok": true}'


def test_a_fifo_in_a_sealed_source_folder_refuses_instead_of_hanging_the_upload(tmp_path: Path):
    """The third site of the same rule, reached through the transfer layer.

    `_open_verified_regular_file` opened with `O_NOFOLLOW` but without
    `O_NONBLOCK`, so a leaf swapped for a **symlink** was refused while a leaf
    swapped for a **FIFO** blocked on the open — before the `S_ISREG` check below
    it could run. Driven through the operator's upload verb, the process printed
    its pre-transfer lines and then hung indefinitely: the one failure that tells
    an operator nothing at all. Found by the Opus read of this branch, reproduced
    end to end.
    """
    from operations.pod import transfer

    fifo = tmp_path / "page-1.png"
    os.mkfifo(fifo)
    caught = _within_ten_seconds(
        lambda: transfer._open_verified_regular_file(fifo, relative="page-1.png")
    )
    assert isinstance(caught, transfer.TransferFailure)
    assert "not a regular file" in str(caught) or "absent" in str(caught)


def test_reusing_a_receipt_path_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    target = tmp_path / "existing.json"
    os.mkfifo(target)

    caught = _within_ten_seconds(lambda: records._atomic_create_or_reuse(target, b"payload"))

    assert isinstance(caught, records.RecordError)
    assert "cannot be read" in str(caught)


def test_reusing_a_receipt_path_refuses_an_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "existing.json"
    target.write_bytes(b"x" * (records.MAX_RECORD_BYTES + 1))

    with pytest.raises(records.RecordError, match="larger than"):
        records._atomic_create_or_reuse(target, b"payload")


def test_descriptor_lock_serializes_a_second_writer(tmp_path: Path) -> None:
    """A second writer cannot enter its read-modify-write while the first owns the lock."""

    store = records.DescriptorStore(tmp_path)
    receipt = tmp_path / "receipt.json"
    started = threading.Event()
    finished = threading.Event()
    raised: list[BaseException] = []

    def second_writer() -> None:
        started.set()
        try:
            store.record("boot", receipt)
        except BaseException as error:  # noqa: BLE001 - surfaced after the timing assertion
            raised.append(error)
        finally:
            finished.set()

    with store._lock():
        writer = threading.Thread(target=second_writer, daemon=True)
        writer.start()
        assert started.wait(1), "the second writer did not start"
        assert not finished.wait(0.2), "the second writer passed the held descriptor lock"

    assert finished.wait(5), "the second writer did not proceed after the lock was released"
    assert raised == []
    loaded = store.load()
    assert loaded is not None and loaded["actions"]["boot"] == str(receipt.resolve())


def test_receipt_and_descriptor_publication_sync_their_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []

    def sync(path: Path, *, strict: bool) -> None:
        assert strict
        synced.append(path)

    monkeypatch.setattr(records, "sync_directory", sync)
    receipt = tmp_path / "receipt.json"
    descriptor = tmp_path / "operator-surface.json"

    records._atomic_create_or_reuse(receipt, b"receipt")
    records._atomic_replace(descriptor, b"descriptor")

    assert synced == [tmp_path, tmp_path]


@pytest.mark.parametrize("publisher", (records._atomic_create_or_reuse, records._atomic_replace))
def test_operator_publication_reports_a_directory_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher: Callable[[Path, bytes], None],
) -> None:
    def refuses(_path: Path, *, strict: bool) -> None:
        assert strict
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(records, "sync_directory", refuses)

    with pytest.raises(records.RecordError, match="could not be written"):
        publisher(tmp_path / "record.json", b"payload")
