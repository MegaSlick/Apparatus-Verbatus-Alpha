"""`records.py`'s two file readers, and the protection only one of them had."""

from __future__ import annotations

import errno
import fcntl
import os
import threading
from contextlib import contextmanager
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
    regular file. `bounded_bytes` read the same recorded paths without it.
    Found by CodeRabbit.
    """
    fifo = tmp_path / "not-a-record"
    os.mkfifo(fifo)
    caught = _within_ten_seconds(lambda: records.bounded_bytes(fifo, "a record"))
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
    assert records.bounded_bytes(path, "a record") == b'{"ok": true}'


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


def test_descriptor_lock_serializes_a_second_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second writer cannot enter its read-modify-write while the first owns the lock."""

    store = records.DescriptorStore(tmp_path)
    receipt = tmp_path / "receipt.json"
    attempted_lock = threading.Event()
    entered_record = threading.Event()
    finished = threading.Event()
    raised: list[BaseException] = []
    order: list[str] = []

    original_lock = store._lock
    original_record_unlocked = store._record_unlocked

    def observed_record_unlocked(action: str, path: Path):
        entered_record.set()
        order.append("second-entered")
        return original_record_unlocked(action, path)

    def second_writer() -> None:
        try:
            store.record("boot", receipt)
        except BaseException as error:  # noqa: BLE001 - surfaced after the timing assertion
            raised.append(error)
        finally:
            finished.set()

    with original_lock():
        order.append("first-holds")

        @contextmanager
        def observed_lock():
            attempted_lock.set()
            with original_lock():
                yield

        monkeypatch.setattr(store, "_lock", observed_lock)
        monkeypatch.setattr(store, "_record_unlocked", observed_record_unlocked)
        writer = threading.Thread(target=second_writer, daemon=True)
        writer.start()
        assert attempted_lock.wait(1), "the second writer did not reach the descriptor lock"
        assert not entered_record.wait(0.2), "the second writer passed the held descriptor lock"
        order.append("first-releases")

    assert finished.wait(5), "the second writer did not proceed after the lock was released"
    assert raised == []
    assert order == ["first-holds", "first-releases", "second-entered"]
    loaded = store.load()
    assert loaded is not None and loaded["actions"]["boot"] == str(receipt.resolve())


def test_descriptor_lock_acquisition_failure_is_named_and_closes_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = records.DescriptorStore(tmp_path)
    opened = []
    real_open = Path.open

    def observed_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_open(path, *args, **kwargs)
        if path == store.lock_path:
            opened.append(handle)
        return handle

    def refuse_lock(_descriptor: int, _operation: int) -> None:
        raise OSError("injected descriptor lock failure")

    monkeypatch.setattr(Path, "open", observed_open)
    monkeypatch.setattr(fcntl, "flock", refuse_lock)

    with pytest.raises(records.RecordError, match="lock could not be taken"):
        with store._lock():
            pytest.fail("an untaken lock must not enter its protected body")

    assert len(opened) == 1 and opened[0].closed


def test_descriptor_unlock_failure_preserves_the_body_error_and_closes_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = records.DescriptorStore(tmp_path)
    opened = []
    real_open = Path.open

    class BodyFailure(RuntimeError):
        pass

    def observed_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_open(path, *args, **kwargs)
        if path == store.lock_path:
            opened.append(handle)
        return handle

    def fail_only_unlock(_descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("injected descriptor unlock failure")

    monkeypatch.setattr(Path, "open", observed_open)
    monkeypatch.setattr(fcntl, "flock", fail_only_unlock)

    with pytest.raises(BodyFailure, match="protected body failed"):
        with store._lock():
            raise BodyFailure("protected body failed")

    assert len(opened) == 1 and opened[0].closed


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


def test_operator_receipt_publication_names_a_directory_sync_failure_after_the_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuses(_path: Path, *, strict: bool) -> None:
        assert strict
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(records, "sync_directory", refuses)

    target = tmp_path / "record.json"
    with pytest.raises(
        records.RecordError,
        match="was written but its directory entry could not be made durable",
    ):
        records._atomic_create_or_reuse(target, b"payload")

    assert target.read_bytes() == b"payload"


def test_reused_operator_receipt_reproves_directory_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "record.json"
    records._atomic_create_or_reuse(target, b"payload")

    def refuses(_path: Path, *, strict: bool) -> None:
        assert strict
        raise OSError("injected reuse directory sync failure")

    monkeypatch.setattr(records, "sync_directory", refuses)

    with pytest.raises(
        records.RecordError,
        match="exists but its directory entry could not be made durable",
    ):
        records._atomic_create_or_reuse(target, b"payload")

    assert target.read_bytes() == b"payload"


def test_operator_descriptor_publication_reports_a_directory_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuses(_path: Path, *, strict: bool) -> None:
        assert strict
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(records, "sync_directory", refuses)

    target = tmp_path / "record.json"
    with pytest.raises(records.RecordError, match="written but its directory entry"):
        records._atomic_replace(target, b"payload")
    # The message and the disk agree: the replace succeeded, only durability
    # of the directory entry is unproven — the descriptor must be present.
    assert target.read_bytes() == b"payload"


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda store: store.list(), id="list"),
        pytest.param(
            lambda store: store.readable_records_of_kind("balance-observation"),
            id="readable_records_of_kind",
        ),
    ],
)
def test_a_dangling_receipt_directory_link_refuses_rather_than_reading_as_empty(
    tmp_path: Path, read: Callable[[records.ReceiptStore], object]
) -> None:
    """`exists()` follows the link, so a dangling one is not "no receipts yet".

    Both readers asked `exists()` first and returned an empty history, so an
    unsafe receipt location was reported to the operator as nothing recorded —
    no saved balance observation, no alert, and no sign that anything was
    wrong. The link is named before anything asks whether it resolves.
    """

    state = tmp_path / "operator-state"
    state.mkdir()
    (state / "receipts").symlink_to(tmp_path / "nowhere")
    store = records.ReceiptStore(state)

    assert not (state / "receipts").exists()
    assert (state / "receipts").is_symlink()
    with pytest.raises(records.RecordError, match="not a safe directory"):
        read(store)


def test_an_absent_receipt_directory_is_still_an_empty_history(tmp_path: Path) -> None:
    """The refusal above must not turn "nothing written yet" into a failure."""

    store = records.ReceiptStore(tmp_path / "operator-state")

    assert store.list() == []
    assert store.readable_records_of_kind("balance-observation") == ([], [])
