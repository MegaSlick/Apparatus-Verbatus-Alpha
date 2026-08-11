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
