"""Reading a submitted folder without following anything out of it.

A submitted folder is untrusted local material. The properties under test are all
properties of the filesystem — what an open follows, what a listing races with —
so every case here builds a real directory and reads it through the real opener.
"""

import os
import time

import pytest

from operations.submit import inventory
from operations.submit.inventory import SubmissionInputError, read_submission

LIMIT = 1024


def test_every_regular_file_is_found_sorted_hashed_and_retained(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a.png").write_bytes(b"first")
    (tmp_path / "b" / "c.png").write_bytes(b"second")

    found = read_submission(tmp_path, max_bytes=LIMIT)

    assert [source.relative_path for source in found] == ["a.png", "b/c.png"]
    assert [source.data for source in found] == [b"first", b"second"]
    assert [source.size for source in found] == [5, 6]
    assert found[0].sha256 == __import__("hashlib").sha256(b"first").hexdigest()


def test_a_symlink_inside_a_submission_is_refused_rather_than_followed(tmp_path):
    """A link points at something the submitter did not submit. Following it would
    put bytes nobody chose into a sealed corpus."""
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not submitted")
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "real.png").write_bytes(b"submitted")
    (folder / "link.png").symlink_to(outside)

    with pytest.raises(SubmissionInputError, match="symlink"):
        read_submission(folder, max_bytes=LIMIT)


def test_a_submitted_folder_that_is_itself_a_symlink_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "page.png").write_bytes(b"bytes")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(SubmissionInputError, match="symlink"):
        read_submission(link, max_bytes=LIMIT)


def test_a_non_regular_entry_is_refused_rather_than_read(tmp_path):
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "page.png").write_bytes(b"bytes")
    os.mkfifo(folder / "pipe")

    with pytest.raises(SubmissionInputError, match="non-regular entry"):
        read_submission(folder, max_bytes=LIMIT)


def test_a_folder_that_is_not_a_directory_is_refused(tmp_path):
    plain = tmp_path / "plain.png"
    plain.write_bytes(b"bytes")
    with pytest.raises(SubmissionInputError, match="not a directory"):
        read_submission(plain, max_bytes=LIMIT)


def test_an_oversized_source_keeps_its_exact_digest_and_drops_only_its_bytes(tmp_path):
    """It still arrived, so it stays in the denominator. The door refuses it by name
    on the strength of the digest the stream produced."""
    folder = tmp_path / "batch"
    folder.mkdir()
    payload = b"x" * (LIMIT + 1)
    (folder / "huge.png").write_bytes(payload)

    found = read_submission(folder, max_bytes=LIMIT)

    assert len(found) == 1
    assert found[0].data is None
    assert found[0].size == LIMIT + 1
    assert found[0].sha256 == __import__("hashlib").sha256(payload).hexdigest()


def test_a_source_changed_while_its_digest_is_read_is_a_named_refusal(tmp_path, monkeypatch):
    """A ledger may only bind one stable file, never a sequence of its revisions.

    The real in-place rewrite still happens mid-read, so `_read_once` genuinely
    reads a file that changed under it. What is mocked is only the *detection*
    side (`_stable_file_metadata`, called exactly once before and once after):
    a same-size rewrite's real effect on `mtime_ns`/`ctime_ns` is at the mercy of
    the filesystem's clock resolution, and under enough scheduler load two writes
    microseconds apart can land in the same tick -- making this test flake on a
    real timer without the check itself being wrong. Forcing the two metadata
    reads to disagree proves `_walk`'s own comparison, not the host clock.
    """
    folder = tmp_path / "batch"
    folder.mkdir()
    source = folder / "page.png"
    payload = b"a" * (inventory._CHUNK + 17)
    source.write_bytes(payload)
    original_read = inventory.os.read
    original_metadata = inventory._stable_file_metadata
    reads = 0
    metadata_calls = 0

    def replace_after_first_chunk(descriptor: int, count: int) -> bytes:
        nonlocal reads
        chunk = original_read(descriptor, count)
        reads += 1
        if reads == 1:
            # Rewriting the open inode exercises the post-read descriptor check;
            # an atomic path replacement alone would leave this descriptor stable.
            source.write_bytes(b"b" * len(payload))
        return chunk

    def force_disagreement_on_the_second_read(details):
        nonlocal metadata_calls
        metadata_calls += 1
        real = original_metadata(details)
        return real if metadata_calls == 1 else (*real[:-1], real[-1] + 1)

    monkeypatch.setattr(inventory.os, "read", replace_after_first_chunk)
    monkeypatch.setattr(inventory, "_stable_file_metadata", force_disagreement_on_the_second_read)

    with pytest.raises(SubmissionInputError, match="changed while it was being read") as caught:
        read_submission(folder, max_bytes=0)

    assert caught.value.entry == "page.png"


def test_an_unreadable_source_fails_the_whole_inventory_rather_than_vanishing(tmp_path):
    """A per-file refusal is the door's job and needs bytes to refuse. An inventory
    that silently omitted what it could not open would shrink the denominator the
    Armarium's census reconciles against."""
    folder = tmp_path / "batch"
    folder.mkdir()
    blocked = folder / "locked.png"
    blocked.write_bytes(b"bytes")
    blocked.chmod(0o000)
    try:
        if os.access(blocked, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("this process can read a mode-000 file; the case cannot be built here")
        with pytest.raises(SubmissionInputError, match="could not be opened"):
            read_submission(folder, max_bytes=LIMIT)
    finally:
        blocked.chmod(0o600)


def test_an_empty_folder_yields_nothing_and_says_so_plainly(tmp_path):
    """The inventory does not decide that an empty submission is an error — its
    callers do, loudly. What it must not do is invent an entry."""
    folder = tmp_path / "batch"
    folder.mkdir()
    assert read_submission(folder, max_bytes=LIMIT) == []


def test_reopening_a_source_reads_an_ordinary_nested_file(tmp_path):
    folder = tmp_path / "batch"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "page.png").write_bytes(b"real bytes")

    with inventory.open_submission_source(folder, "sub/page.png") as opened:
        assert opened.handle.read() == b"real bytes"
        opened.assert_unchanged()


def test_reopening_refuses_a_leaf_swapped_for_a_symlink_after_the_walk(tmp_path):
    """The exact case a second plain-path open would miss.

    `read_submission` already proved this path held a regular file; something else
    is there by the time the door reads the bytes it will seal. The refusal must
    happen at the open, before the swap target's bytes are read at all — not after
    they have been read and found not to match a digest.
    """
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not submitted")
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "page.png").write_bytes(b"real bytes")
    found = read_submission(folder, max_bytes=LIMIT)
    assert [source.relative_path for source in found] == ["page.png"]

    (folder / "page.png").unlink()
    (folder / "page.png").symlink_to(outside)

    with pytest.raises(SubmissionInputError, match="redirect"):
        with inventory.open_submission_source(folder, "page.png"):
            pass


def test_reopening_refuses_a_symlinked_intermediate_directory(tmp_path):
    """A component earlier in the path, not only the leaf, can be swapped."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.png").write_bytes(b"not submitted")
    folder = tmp_path / "batch"
    real_sub = tmp_path / "real-sub"
    real_sub.mkdir()
    folder.mkdir()
    (folder / "sub").symlink_to(real_sub, target_is_directory=True)

    with pytest.raises(SubmissionInputError, match="redirect"):
        with inventory.open_submission_source(folder, "sub/page.png"):
            pass


@pytest.mark.parametrize(
    "requested", ["../outside.png", "/etc/passwd", "", "a/./b.png", "a//b.png"]
)
def test_reopening_refuses_anything_that_is_not_a_plain_relative_name(tmp_path, requested):
    folder = tmp_path / "batch"
    folder.mkdir()
    with pytest.raises(SubmissionInputError, match="plain relative filename"):
        with inventory.open_submission_source(folder, requested):
            pass


def test_reopening_a_name_with_an_embedded_nul_is_a_named_refusal_not_a_crash(tmp_path):
    """No real directory listing can produce this; only a forged manifest row can.

    `os.open` raises a bare `ValueError` for an embedded NUL, which is not this
    project's alarm vocabulary. Left unguarded, that error is not caught by any
    handler above `open_submission_source`, so it does not merely refuse the one
    forged source -- it crashes the whole run mid-submission.
    """
    folder = tmp_path / "batch"
    folder.mkdir()
    with pytest.raises(SubmissionInputError, match="NUL byte"):
        with inventory.open_submission_source(folder, "a\x00b"):
            pass


def test_an_atomic_replacement_of_the_name_leaves_the_held_descriptor_admissible(tmp_path):
    """Replacing the *name* is not a change to the bytes this descriptor holds.

    `assert_unchanged` exists to catch a source rewritten under the reader, not to
    re-litigate the pathname race the anchored descriptor already closed. An
    `os.replace` unlinks this inode's name and can move its ctime; the open inode
    still holds exactly the bytes that were hashed, so refusing here would discard
    a correct read and reintroduce the very race the stream exists to avoid.
    """
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "page.png").write_bytes(b"original bytes")
    replacement = tmp_path / "replacement.png"
    replacement.write_bytes(b"different bytes entirely")

    with inventory.open_submission_source(folder, "page.png") as opened:
        os.replace(replacement, folder / "page.png")
        assert opened.handle.read() == b"original bytes"
        opened.assert_unchanged()


def test_a_source_rewritten_in_place_under_the_reader_is_a_named_refusal(tmp_path):
    """The other half: the same inode, different bytes, while it is being read."""
    folder = tmp_path / "batch"
    folder.mkdir()
    source = folder / "page.png"
    source.write_bytes(b"original bytes")

    with inventory.open_submission_source(folder, "page.png") as opened:
        assert opened.handle.read() == b"original bytes"
        source.write_bytes(b"rewritten in place")
        with pytest.raises(SubmissionInputError, match="changed while it was being read"):
            opened.assert_unchanged()


def test_a_same_size_rewrite_cannot_hide_by_restoring_its_mtime(tmp_path):
    """Size and mtime alone are not a stable identity; ctime exposes the write."""
    folder = tmp_path / "batch"
    folder.mkdir()
    source = folder / "page.png"
    source.write_bytes(b"original bytes!")

    with inventory.open_submission_source(folder, "page.png") as opened:
        before = os.fstat(opened._descriptor)
        assert opened.handle.read() == b"original bytes!"
        # Some test filesystems quantize change time; cross one tick so the
        # regression does not depend on two writes receiving distinct nanoseconds.
        time.sleep(0.01)
        source.write_bytes(b"changed! bytes!")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

        after = os.fstat(opened._descriptor)
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        assert after.st_ctime_ns != before.st_ctime_ns
        with pytest.raises(SubmissionInputError, match="changed while it was being read"):
            opened.assert_unchanged()
