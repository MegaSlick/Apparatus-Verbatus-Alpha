"""Checksummed transfer drills use only synthetic bytes and an in-memory target."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

import pytest

from common.contracts.canonical import canonical_bytes
from operations.submit.submit import build_manifest

from .transfer import ChecksummedTransfer, RemoteObject, TransferFailure


class FakeTarget:
    def __init__(self, *, fail_key: str | None = None) -> None:
        self.fail_key = fail_key
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def inspect(self, key: str) -> RemoteObject | None:
        value = self.objects.get(key)
        if value is None:
            return None
        return RemoteObject(hashlib.sha256(value).hexdigest(), len(value))

    def put_file(self, key: str, source: BinaryIO) -> None:
        self.puts.append(key)
        if key == self.fail_key:
            self.fail_key = None
            raise RuntimeError("injected partial transfer")
        self.objects[key] = source.read()


def manifest(source: Path, destination: Path) -> None:
    entries = []
    for path in sorted(source.iterdir()):
        payload = path.read_bytes()
        entries.append(
            {
                "relative_path": path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        )
    record = build_manifest(
        entries,
        authorized_by={"relative_path": f"receipts/sha256/{'a' * 64}.json", "sha256": "a" * 64},
    )
    destination.write_bytes(canonical_bytes(record))


def test_partial_transfer_resumes_only_verified_rows(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"first synthetic page")
    (source / "b.bin").write_bytes(b"second synthetic page")
    manifest_path = tmp_path / "submission.json"
    manifest(source, manifest_path)
    target = FakeTarget(fail_key="run/b.bin")
    transfer = ChecksummedTransfer(
        source_root=source,
        submission_manifest=manifest_path,
        target=target,
        prefix="run",
        journal_path=tmp_path / "transfer.json",
    )

    with pytest.raises(TransferFailure, match="partial transfer"):
        transfer.resume()
    report = transfer.resume()

    assert report.completed_keys == ("run/b.bin",)
    assert report.skipped_keys == ("run/a.bin",)
    assert target.puts.count("run/a.bin") == 1
    assert target.puts.count("run/b.bin") == 2


def test_conflicting_remote_bytes_are_refused_not_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.bin").write_bytes(b"expected synthetic bytes")
    manifest_path = tmp_path / "submission.json"
    manifest(source, manifest_path)
    target = FakeTarget()
    target.objects["run/page.bin"] = b"wrong bytes"

    with pytest.raises(TransferFailure, match="differs"):
        ChecksummedTransfer(
            source_root=source,
            submission_manifest=manifest_path,
            target=target,
            prefix="run",
            journal_path=tmp_path / "transfer.json",
        ).resume()
    assert target.puts == []


def test_resume_with_no_submission_manifest_yet_is_a_vacuous_success(tmp_path: Path) -> None:
    """A freshly launched pod that has processed nothing yet has nothing to transfer."""

    source = tmp_path / "source"
    source.mkdir()
    target = FakeTarget()

    report = ChecksummedTransfer(
        source_root=source,
        submission_manifest=tmp_path / "no-submission-yet.json",
        target=target,
        prefix="run",
        journal_path=tmp_path / "transfer.json",
    ).resume()

    assert report.completed_keys == ()
    assert report.skipped_keys == ()
    assert target.puts == []
    # An empty receipt has to say which empty it is, or a green bootstrap journal
    # reads the same for "nothing to send" and "I never found the manifest".
    assert report.to_record()["submission_manifest"] == "absent"


def test_a_journaled_row_the_target_lost_is_reported_as_sent_again(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.bin").write_bytes(b"synthetic page bytes")
    manifest_path = tmp_path / "submission.json"
    manifest(source, manifest_path)
    target = FakeTarget()
    transfer = ChecksummedTransfer(
        source_root=source,
        submission_manifest=manifest_path,
        target=target,
        prefix="run",
        journal_path=tmp_path / "transfer.json",
    )
    first = transfer.resume()
    assert first.completed_keys == ("run/page.bin",)

    target.objects.clear()
    second = transfer.resume()

    assert second.completed_keys == ("run/page.bin",)
    assert second.skipped_keys == ()
    assert target.puts == ["run/page.bin", "run/page.bin"]
    assert second.to_record()["submission_manifest"] == "present"


def test_open_verified_regular_file_refuses_a_symlink_leaf_directly(tmp_path: Path) -> None:
    """The final O_NOFOLLOW open, not only `_under`'s pre-check, refuses a symlink.

    `_under` only sees the path as it is at the moment it walks it; this is
    the guard that still holds if the leaf is swapped for a symlink in the
    window between that check and the read that follows it.
    """

    from .transfer import _open_verified_regular_file

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"should never be read as the source")
    leaf = tmp_path / "page.bin"
    leaf.symlink_to(outside)

    with pytest.raises(TransferFailure, match="not a regular file"):
        _open_verified_regular_file(leaf, relative="page.bin")


def test_submission_path_cannot_traverse_a_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside synthetic bytes")
    (source / "page.bin").symlink_to(outside)
    manifest_path = tmp_path / "submission.json"
    manifest(source, manifest_path)

    with pytest.raises(TransferFailure, match="symbolic link"):
        ChecksummedTransfer(
            source_root=source,
            submission_manifest=manifest_path,
            target=FakeTarget(),
            prefix="run",
            journal_path=tmp_path / "transfer.json",
        ).resume()


@pytest.mark.parametrize("declared", ["page\x00.bin", "./page.bin", "sub//page.bin", "."])
def test_every_unsafe_declared_path_becomes_a_named_refusal(declared: str) -> None:
    """Spec 03 stops at non-empty, not-absolute, no dot-dot, so these all arrive here.

    Left to `os.lstat`, the NUL is a bare ValueError rather than a named
    refusal, and the un-normalized spellings resolve to a real file whose object
    key still carries the spelling rather than the path that was opened.
    """

    from .transfer import _under

    with pytest.raises(TransferFailure, match="unsafe relative path"):
        _under(Path("/tmp"), declared)
