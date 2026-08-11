"""`records.py`'s two file readers, and the protection only one of them had."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from operations.operator import records


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
    with pytest.raises(OSError) as caught:
        records._bounded_bytes(fifo, "a record")
    assert caught.value.errno == errno.EINVAL


def test_a_digest_of_a_fifo_refuses_the_same_way(tmp_path: Path):
    """Invariant #14's shape: the sibling this was copied from still refuses too."""
    fifo = tmp_path / "not-a-file"
    os.mkfifo(fifo)
    with pytest.raises(OSError) as caught:
        records.sha256_file(fifo)
    assert caught.value.errno == errno.EINVAL


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
    with pytest.raises(transfer.TransferFailure, match="not a regular file|absent"):
        transfer._open_verified_regular_file(fifo, relative="page-1.png")
