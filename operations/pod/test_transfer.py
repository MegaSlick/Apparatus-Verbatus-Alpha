"""Checksummed transfer drills use only synthetic bytes and an in-memory target."""

from __future__ import annotations

import hashlib
from pathlib import Path

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

    def put_file(self, key: str, source: Path) -> None:
        self.puts.append(key)
        if key == self.fail_key:
            self.fail_key = None
            raise RuntimeError("injected partial transfer")
        self.objects[key] = source.read_bytes()


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
