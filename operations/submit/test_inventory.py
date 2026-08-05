"""Reading a submitted folder without following anything out of it.

A submitted folder is untrusted local material. The properties under test are all
properties of the filesystem — what an open follows, what a listing races with —
so every case here builds a real directory and reads it through the real opener.
"""

import os

import pytest

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
