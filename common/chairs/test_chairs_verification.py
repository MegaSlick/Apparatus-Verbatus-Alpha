"""Verification: a fixture snapshot with one flipped byte fails **naming the
file**; a complete match passes; an extra file fails; a partial cache re-fetches
exactly the missing files. Network is mocked, so this measures the call the mock
received, not Hugging Face's behaviour.

Two more properties are checked here, because nothing else would catch them:
verification covers the *whole* fetched snapshot, and a failed verification
leaves the previously verified snapshot untouched.

Every fetch below goes through `RecordingFetcher`, the one seam. No test in this
file asserts anything about Hugging Face; each asserts what the registry asked
the seam for, and what it did with what came back.
"""

import json
import os
import threading
import unittest.mock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs import manifests
from common.chairs.config import load_models_toml
from common.chairs.errors import (
    ConfigurationRefusal,
    DigestMismatchRefusal,
    DiskSpaceRefusal,
    UnresolvedChairRefusal,
)
from common.chairs.manifests import (
    build_manifest,
    file_digest,
    inspect_snapshot_for_repair,
    manifest_digest,
    read_manifest,
    verify_snapshot,
    write_manifest,
)
from common.chairs.models import ChairIdentity
from common.chairs.registry import (
    CACHE_DESCRIPTOR,
    PRE_MATERIALIZATION_SENTINEL,
    ChairRegistry,
    HuggingFaceMaterializationFetcher,
)
from common.contracts.canonical import canonical_bytes, digest_bytes

from .conftest import (
    RecordingFetcher,
    config_of,
    hf_chair,
    pin_snapshot,
    registry_for,
    serving_details,
    wait_for_a_later_ctime,
    write_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]


def test_file_digest_streams_the_snapshot_file_in_bounded_chunks(tmp_path, monkeypatch):
    """Large model weights are hashed in bounded chunks, never loaded whole.

    ``Path.read_bytes`` is blocked outright, and every read on the opened handle
    is bounded, so a regression to *either* whole-file shape — ``read_bytes()``
    or ``open().read()`` — fails by name. The bound is judged from the requested
    size, not the bytes returned, so the fixture file can stay small.
    """
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"fixture weights\n")

    def whole_file_read_would_be_a_memory_regression(_path):
        pytest.fail("snapshot digest read the entire file into memory")

    monkeypatch.setattr(type(weights), "read_bytes", whole_file_read_would_be_a_memory_regression)

    bound = manifests.HASH_CHUNK_BYTES

    class BoundedHandle:
        """Proxy that refuses any single read larger than the bound.

        Deliberately carries no ``getbuffer``: a future file helper could take
        the whole buffer at once down that path, which is exactly the shape refused.
        """

        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._handle.__exit__(*exc_info)

        def readable(self):
            return self._handle.readable()

        def readinto(self, buffer):
            if len(buffer) > bound:
                pytest.fail("snapshot digest read the entire file into memory")
            return self._handle.readinto(buffer)

        def read(self, size=-1):
            if size is None or size < 0 or size > bound:
                pytest.fail("snapshot digest read the entire file into memory")
            return self._handle.read(size)

    real_open = type(weights).open

    def bounded_open(self, *args, **kwargs):
        return BoundedHandle(real_open(self, *args, **kwargs))

    monkeypatch.setattr(type(weights), "open", bounded_open)

    assert file_digest(weights, "attestator_1", "weights.bin") == digest_bytes(b"fixture weights\n")


def test_parallel_and_serial_hashes_publish_identical_manifests(tmp_path, monkeypatch):
    snapshot = write_snapshot(
        tmp_path / "snapshot",
        {"a.bin": b"first\n", "nested/b.bin": b"second\n", "z.bin": b"third\n"},
    )
    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 1)
    serial = build_manifest(snapshot)

    barrier = threading.Barrier(2)
    threads: set[str] = set()
    real_digest = manifests.file_digest

    def paired_digest(path, chair, relative):
        if relative in {"a.bin", "nested/b.bin"}:
            threads.add(threading.current_thread().name)
            barrier.wait(timeout=5)
        return real_digest(path, chair, relative)

    monkeypatch.setattr(manifests, "file_digest", paired_digest)
    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 4)
    parallel = build_manifest(snapshot)
    monkeypatch.setattr(manifests, "file_digest", real_digest)

    assert len(threads) == 2
    assert parallel.to_record() == serial.to_record()
    assert canonical_bytes(parallel.to_record()) == canonical_bytes(serial.to_record())
    assert manifest_digest(parallel) == manifest_digest(serial)

    identity = config_of(
        tmp_path, {"attestator_1": hf_chair("attestator_1", manifest_digest(serial))}
    ).chairs["attestator_1"]
    assert isinstance(identity, ChairIdentity)
    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 1)
    verified_serial = verify_snapshot(identity, snapshot, serial)
    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 4)
    assert verify_snapshot(identity, snapshot, parallel) == verified_serial


def test_parallel_verification_refuses_the_same_first_corrupt_file(tmp_path, monkeypatch):
    snapshot = write_snapshot(
        tmp_path / "snapshot", {"a.bin": b"first\n", "b.bin": b"second\n", "z.bin": b"third\n"}
    )
    manifest = build_manifest(snapshot)
    identity = config_of(
        tmp_path, {"attestator_1": hf_chair("attestator_1", manifest_digest(manifest))}
    ).chairs["attestator_1"]
    assert isinstance(identity, ChairIdentity)
    (snapshot / "a.bin").write_bytes(b"FIRST\n")
    (snapshot / "b.bin").write_bytes(b"SECOND\n")

    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 1)
    with pytest.raises(DigestMismatchRefusal) as serial:
        verify_snapshot(identity, snapshot, manifest)
    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 4)
    with pytest.raises(DigestMismatchRefusal) as parallel:
        verify_snapshot(identity, snapshot, manifest)

    assert "a.bin" in str(serial.value)
    assert str(parallel.value) == str(serial.value)

    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 1)
    with pytest.raises(DigestMismatchRefusal) as repair_serial:
        inspect_snapshot_for_repair(identity, snapshot, manifest)
    monkeypatch.setattr(manifests.os, "cpu_count", lambda: 4)
    with pytest.raises(DigestMismatchRefusal) as repair_parallel:
        inspect_snapshot_for_repair(identity, snapshot, manifest)
    assert "a.bin" in str(repair_serial.value)
    assert str(repair_parallel.value) == str(repair_serial.value)


# --- Hashing while copying ---------------------------------------------------------


def _row_for(snapshot, relative):
    return next(row for row in build_manifest(snapshot).rows if row.path == relative)


def test_copy_and_digest_returns_the_digest_of_the_bytes_it_wrote(tmp_path):
    remote = write_snapshot(tmp_path / "remote", {"weights.bin": b"fixture weights\n"})
    row = _row_for(remote, "weights.bin")

    digest = manifests.copy_and_digest(
        remote / "weights.bin", tmp_path / "copy" / "weights.bin", row, chair="attestator_1"
    )

    assert digest == row.sha256 == digest_bytes(b"fixture weights\n")
    assert (tmp_path / "copy" / "weights.bin").read_bytes() == b"fixture weights\n"


def test_copy_and_digest_refuses_a_size_mismatch_before_reading_or_writing(tmp_path, monkeypatch):
    remote = write_snapshot(tmp_path / "remote", {"weights.bin": b"fixture weights\n"})
    row = replace(_row_for(remote, "weights.bin"), size=999)
    monkeypatch.setattr(
        manifests.os, "fdopen", lambda *a, **k: pytest.fail("bytes read before the size check")
    )

    with pytest.raises(DigestMismatchRefusal, match="weights.bin: size 16, expected 999"):
        manifests.copy_and_digest(
            remote / "weights.bin", tmp_path / "copy.bin", row, chair="attestator_1"
        )
    assert not (tmp_path / "copy.bin").exists()


def test_copy_and_digest_refuses_bytes_whose_digest_differs_naming_the_file(tmp_path):
    remote = write_snapshot(tmp_path / "remote", {"weights.bin": b"fixture weights\n"})
    row = _row_for(remote, "weights.bin")
    (remote / "weights.bin").write_bytes(b"fixture weightX\n")

    with pytest.raises(DigestMismatchRefusal, match="weights.bin: sha256 .*, expected"):
        manifests.copy_and_digest(
            remote / "weights.bin", tmp_path / "copy.bin", row, chair="attestator_1"
        )


@pytest.mark.hostile_local
def test_copy_and_digest_never_follows_a_source_link(tmp_path):
    remote = write_snapshot(tmp_path / "remote", {"weights.bin": b"fixture weights\n"})
    row = _row_for(remote, "weights.bin")
    (tmp_path / "link.bin").symlink_to(remote / "weights.bin")

    with pytest.raises(DigestMismatchRefusal, match="cannot be read"):
        manifests.copy_and_digest(
            tmp_path / "link.bin", tmp_path / "copy.bin", row, chair="attestator_1"
        )


def test_a_pool_of_copies_runs_largest_first_and_refuses_the_lexically_first_file(
    tmp_path, monkeypatch
):
    files = {"a.bin": b"a\n", "b.bin": b"bbbbbbbb\n", "c.bin": b"cccc\n"}
    remote = write_snapshot(tmp_path / "remote", files)
    rows = {row.path: row for row in build_manifest(remote).rows}
    monkeypatch.setenv("VERBATUS_IO_WORKERS", "1")
    order: list[str] = []
    real = manifests.copy_and_digest

    def record(source, target, row, *, chair):
        order.append(row.path)
        return real(source, target, row, chair=chair)

    monkeypatch.setattr(manifests, "copy_and_digest", record)
    items = [(remote / name, tmp_path / "copy" / name, rows[name]) for name in sorted(files)]
    ledger = manifests.copy_and_digest_files(items, chair="attestator_1")

    assert order == ["b.bin", "c.bin", "a.bin"]
    assert ledger.digests == {name: rows[name].sha256 for name in files}
    assert ledger.to_record() == {
        "copied_files": 3,
        "io_workers": {"workers": 1, "source": "VERBATUS_IO_WORKERS"},
    }

    (remote / "a.bin").write_bytes(b"A\n")
    (remote / "c.bin").write_bytes(b"CCCC\n")
    for workers in ("1", "4"):
        monkeypatch.setenv("VERBATUS_IO_WORKERS", workers)
        target = tmp_path / f"copy-{workers}"
        items = [(remote / name, target / name, rows[name]) for name in sorted(files)]
        with pytest.raises(DigestMismatchRefusal) as caught:
            manifests.copy_and_digest_files(items, chair="attestator_1")
        assert "snapshot differs at a.bin" in str(caught.value)


def test_one_pool_takes_the_largest_waiting_file_across_every_fill(tmp_path):
    """Two fills share one worker; it always takes the largest file still waiting."""
    from common.cpus import IoWorkers

    order: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def job(name):
        def run():
            if name == "first":
                started.set()
                assert release.wait(timeout=5)
            order.append(name)
            return name

        return run

    with manifests.CopyPool(IoWorkers(1, "test")) as pool:
        results: dict[str, list[str]] = {}
        blocker = threading.Thread(
            target=lambda: results.update(a=pool.run([(100, job("first")), (5, job("a-small"))]))
        )
        blocker.start()
        assert started.wait(timeout=5)
        other = threading.Thread(
            target=lambda: results.update(b=pool.run([(10, job("b-large")), (2, job("b-tiny"))]))
        )
        other.start()
        deadline = 50
        while len(pool._waiting) < 3 and deadline:
            deadline -= 1
            threading.Event().wait(0.01)
        release.set()
        blocker.join(timeout=5)
        other.join(timeout=5)

    assert order[0] == "first"
    assert order[1:] == ["b-large", "a-small", "b-tiny"]
    assert results == {"a": ["first", "a-small"], "b": ["b-large", "b-tiny"]}
    assert len(pool._threads) == 1


def test_files_copied_through_a_shared_pool_record_its_worker_count(tmp_path):
    from common.cpus import IoWorkers

    files = {"a.bin": b"a\n", "b.bin": b"bbbb\n"}
    remote = write_snapshot(tmp_path / "remote", files)
    rows = {row.path: row for row in build_manifest(remote).rows}
    items = [(remote / name, tmp_path / "copy" / name, rows[name]) for name in sorted(files)]

    with manifests.CopyPool(IoWorkers(3, "shared-test")) as pool:
        ledger = manifests.copy_and_digest_files(items, chair="attestator_1", pool=pool)

    assert ledger.digests == {name: rows[name].sha256 for name in files}
    assert ledger.workers == IoWorkers(3, "shared-test")
    assert len(pool._threads) == 2, "never more threads than files waiting"


def test_a_malformed_worker_override_is_a_configuration_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBATUS_IO_WORKERS", "many")

    with pytest.raises(ConfigurationRefusal, match="VERBATUS_IO_WORKERS"):
        manifests.copy_and_digest_files([], chair="attestator_1")


class LedgerFetcher(RecordingFetcher):
    """A fetch seam that copies through `copy_and_digest_files`, as the store fetcher does."""

    def __init__(self, remote, manifest):
        super().__init__({})
        self.remote = remote
        self.rows = {row.path: row for row in manifest.rows}

    def fetch(self, identity, destination, paths):
        self.calls.append((identity.role, paths))
        return manifests.copy_and_digest_files(
            [(self.remote / p, destination / p, self.rows[p]) for p in paths],
            chair=identity.role,
        )


def _digest_log(monkeypatch):
    digested: list[str] = []
    real_digest = manifests.file_digest

    def record_digest(path, chair, relative):
        digested.append(relative)
        return real_digest(path, chair, relative)

    monkeypatch.setattr(manifests, "file_digest", record_digest)
    return digested


def test_a_fill_through_a_verifying_copy_reads_no_copied_byte_twice(hf_world, monkeypatch):
    manifest = hf_world.registry.manifest(hf_world.identity())
    hf_world.registry.fetcher = LedgerFetcher(hf_world.tmp_path / "remote", manifest)
    digested = _digest_log(monkeypatch)

    snapshot = hf_world.registry.ensure(hf_world.identity())

    assert digested == []
    assert snapshot.manifest_digest == hf_world.pin
    assert snapshot.verification["bytes"] == "hashed while copying"
    assert snapshot.verification["hashed_at_copy"] == 2
    assert snapshot.verification["rehashed"] == 0


def test_a_repair_through_a_verifying_copy_rehashes_only_files_already_present(
    hf_world, monkeypatch
):
    manifest = hf_world.registry.manifest(hf_world.identity())
    first = hf_world.registry.ensure(hf_world.identity())
    (first.root / "nested/weights.bin").unlink()
    hf_world.registry.fetcher = LedgerFetcher(hf_world.tmp_path / "remote", manifest)
    digested = _digest_log(monkeypatch)

    repaired = hf_world.registry.ensure(hf_world.identity())

    # Once while inspecting the damaged cache, once after carrying it over.
    assert digested == ["config.json", "config.json"]
    assert repaired.verification["hashed_at_copy"] == 1
    assert repaired.verification["rehashed"] == 1


def test_a_ledger_never_vouches_for_a_file_the_fetch_was_not_asked_for(hf_world):
    """Carried-over files are re-hashed even when the fetcher claims their digest."""

    first = hf_world.registry.ensure(hf_world.identity())
    (first.root / "nested/weights.bin").unlink()
    manifest = hf_world.registry.manifest(hf_world.identity())
    rows = {row.path: row for row in manifest.rows}

    class Vouching(LedgerFetcher):
        def fetch(self, identity, destination, paths):
            ledger = super().fetch(identity, destination, paths)
            # The carried-over copy changes inside the candidate, same size.
            carried = destination / "config.json"
            original = carried.read_bytes()
            carried.write_bytes(original.upper()[: len(original)])
            assert carried.read_bytes() != original
            return replace(
                ledger, digests={**ledger.digests, "config.json": rows["config.json"].sha256}
            )

    vouching = Vouching(hf_world.tmp_path / "remote", manifest)
    hf_world.registry.fetcher = vouching
    with pytest.raises(DigestMismatchRefusal, match="config.json: sha256"):
        hf_world.registry.ensure(hf_world.identity())
    assert vouching.calls == [("attestator_1", ("nested/weights.bin",))]


def test_a_verifying_copy_still_refuses_an_extra_file(hf_world):
    manifest = hf_world.registry.manifest(hf_world.identity())

    class Generous(LedgerFetcher):
        def fetch(self, identity, destination, paths):
            ledger = super().fetch(identity, destination, paths)
            (destination / "z-unpinned.json").write_bytes(b"{}\n")
            return ledger

    hf_world.registry.fetcher = Generous(hf_world.tmp_path / "remote", manifest)
    with pytest.raises(DigestMismatchRefusal, match="z-unpinned.json: extra file"):
        hf_world.registry.ensure(hf_world.identity())


# --- A complete match ---------------------------------------------------------------


def test_a_complete_match_verifies_and_fetches_exactly_the_pinned_paths(hf_world):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)

    assert snapshot.identity == identity
    assert snapshot.manifest_digest == hf_world.pin
    # Exactly the pinned paths, in sorted order, and nothing besides.
    assert hf_world.fetcher.calls == [("attestator_1", ("config.json", "nested/weights.bin"))]
    assert (snapshot.root / CACHE_DESCRIPTOR).is_file()


def test_a_second_ensure_in_another_process_hashes_each_cache_file_once_and_fetches_nothing(
    hf_world, monkeypatch
):
    """The re-verification that keeping provenance intact requires has to be survivable.

    A registry that leaves its own bookkeeping inside the snapshot directory
    passes the first verification and then refuses every one after it, because
    its own marker file is an unpinned extra. The descriptor is excluded by name
    from the comparison, and this is the test that says so. A fresh registry
    stands for a new process, which has verified nothing yet.
    """
    identity = hf_world.identity()
    first = hf_world.registry.ensure(identity)
    digested = _digest_log(monkeypatch)
    restarted = registry_for(hf_world.registry.config, hf_world.tmp_path, hf_world.fetcher)
    second = restarted.ensure(identity)

    assert second.root == first.root
    assert second.manifest_digest == first.manifest_digest
    assert sorted(digested) == ["config.json", "nested/weights.bin"]
    assert len(hf_world.fetcher.calls) == 1, "a complete verified cache is not re-fetched"


def test_a_second_ensure_in_the_same_process_reads_no_byte_of_an_unchanged_cache(
    hf_world, monkeypatch
):
    """Preflight verifies a chair and its smoke then starts it: one hash, not two."""
    identity = hf_world.identity()
    first = hf_world.registry.ensure(identity)
    digested = _digest_log(monkeypatch)

    second = hf_world.registry.ensure(identity)
    bystander = hf_world.registry.ensure(hf_world.identity("attestator_2"))

    assert digested == []
    assert second.root == first.root
    assert second.identity == identity
    assert bystander.identity.role == "attestator_2"
    assert second.verification == {
        "bytes": "verified earlier in this process; no file changed since"
    }
    assert len(hf_world.fetcher.calls) == 1


@pytest.mark.parametrize("change", ["same-bytes-rewrite", "touch", "replace", "extra", "remove"])
def test_any_change_to_a_cache_file_sends_the_next_ensure_back_to_the_bytes(
    hf_world, monkeypatch, change
):
    identity = hf_world.identity()
    first = hf_world.registry.ensure(identity)
    weights = first.root / "nested/weights.bin"
    data = weights.read_bytes()
    if change == "same-bytes-rewrite":
        wait_for_a_later_ctime(weights, hf_world.tmp_path)
        os.utime(weights, ns=(1, 1))
        weights.write_bytes(data)
    elif change == "touch":
        os.utime(weights, ns=(5, 5))
    elif change == "replace":
        replacement = weights.with_name("weights.tmp")
        replacement.write_bytes(data)
        os.replace(replacement, weights)
    elif change == "extra":
        (first.root / "z-unpinned.json").write_bytes(b"{}\n")
    else:
        weights.unlink()
    digested = _digest_log(monkeypatch)

    if change == "extra":
        with pytest.raises(DigestMismatchRefusal, match="z-unpinned.json"):
            hf_world.registry.ensure(identity)
        return
    hf_world.registry.ensure(identity)

    assert digested, "a changed file is verified from its bytes again"


def test_a_tampered_file_with_restored_times_is_still_caught_by_its_ctime(hf_world):
    identity = hf_world.identity()
    first = hf_world.registry.ensure(identity)
    weights = first.root / "nested/weights.bin"
    status = weights.stat()
    wait_for_a_later_ctime(weights, hf_world.tmp_path)
    weights.write_bytes(b"fixture weightX\n")
    os.utime(weights, ns=(status.st_atime_ns, status.st_mtime_ns))

    with pytest.raises(DigestMismatchRefusal, match="nested/weights.bin"):
        hf_world.registry.ensure(identity)


def test_a_failed_verification_is_never_remembered(hf_world, monkeypatch):
    identity = hf_world.identity()
    first = hf_world.registry.ensure(identity)
    hf_world.registry._verified.clear()
    (first.root / "nested/weights.bin").write_bytes(b"fixture weightX\n")
    for _ in range(2):
        with pytest.raises(DigestMismatchRefusal):
            hf_world.registry.ensure(identity)
    assert hf_world.registry._verified == {}


class PerRoleFetcher(RecordingFetcher):
    """The fetch seam when each role is pinned to its own bytes."""

    def __init__(self, files_by_role):
        super().__init__({})
        self.files_by_role = files_by_role

    def fetch(self, identity, destination, paths):
        self.files = self.files_by_role[identity.role]
        super().fetch(identity, destination, paths)


def _distinct_pins(tmp_path, roles):
    """A registry whose roles each pin different bytes, so each has its own cache."""
    files_by_role = {role: {"weights.bin": f"{role} weights\n".encode()} for role in roles}
    chairs = {}
    for role in roles:
        remote = write_snapshot(tmp_path / "remote" / role, files_by_role[role])
        chairs[role] = hf_chair(role, pin_snapshot(remote, tmp_path / "manifests" / f"{role}.json"))
    fetcher = PerRoleFetcher(files_by_role)
    return registry_for(config_of(tmp_path, chairs), tmp_path, fetcher), fetcher


def test_two_roles_bound_to_one_pin_share_one_verified_copy(hf_world):
    """attestator_1 and attestator_2 pin the same manifest: one copy serves both."""
    first = hf_world.registry.ensure(hf_world.identity("attestator_1"))
    second = hf_world.registry.ensure(hf_world.identity("attestator_2"))

    assert first.root == second.root
    assert first.root == hf_world.registry.cache_root / "by-digest" / hf_world.pin
    assert first.identity.role == "attestator_1"
    assert second.identity.role == "attestator_2"
    assert first.manifest_digest == second.manifest_digest == hf_world.pin
    assert hf_world.fetcher.calls == [("attestator_1", ("config.json", "nested/weights.bin"))]
    caches = [
        entry.name
        for entry in (hf_world.registry.cache_root / "by-digest").iterdir()
        if not entry.name.startswith(".")
    ]
    assert caches == [hf_world.pin]
    assert sorted(entry.name for entry in hf_world.registry.cache_root.iterdir()) == ["by-digest"]


def test_concurrent_ensures_of_one_pin_fetch_it_once(hf_world):
    """Two chairs on one pin, ensured at once, wait on the digest's lock, not race."""
    entered = threading.Event()
    release = threading.Event()
    real_fetch = hf_world.fetcher.fetch

    def slow_fetch(identity, destination, paths):
        entered.set()
        release.wait(timeout=5)
        real_fetch(identity, destination, paths)

    hf_world.fetcher.fetch = slow_fetch
    results = {}

    def ensure(role):
        results[role] = hf_world.registry.ensure(hf_world.identity(role))

    first = threading.Thread(target=ensure, args=("attestator_1",))
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=ensure, args=("attestator_2",))
    second.start()
    second.join(timeout=0.2)
    assert second.is_alive(), "the second ensure waits for the first fill to finish"
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert results["attestator_1"].root == results["attestator_2"].root
    assert hf_world.fetcher.roles == ["attestator_1"]


def test_an_incoming_chair_keeps_other_complete_caches_when_space_suffices(tmp_path, monkeypatch):
    registry, fetcher = _distinct_pins(tmp_path, ("attestator_1", "attestator_2"))
    first = registry.ensure(registry.resolve("attestator_1"))
    monkeypatch.setattr(
        "common.chairs.registry.shutil.disk_usage",
        lambda path: SimpleNamespace(free=10**12),
    )

    second = registry.ensure(registry.resolve("attestator_2"))
    registry.ensure(registry.resolve("attestator_1"))
    registry.ensure(registry.resolve("attestator_2"))

    assert first.root.is_dir()
    assert second.root.is_dir()
    assert first.root != second.root
    assert fetcher.calls == [
        ("attestator_1", ("weights.bin",)),
        ("attestator_2", ("weights.bin",)),
    ]


def test_insufficient_space_evicts_least_recently_used_digest_first(tmp_path, monkeypatch):
    registry, fetcher = _distinct_pins(tmp_path, ("old", "recent", "incoming"))
    old = registry.ensure(registry.resolve("old"))
    recent = registry.ensure(registry.resolve("recent"))
    os.utime(old.root, ns=(1, 1))
    os.utime(recent.root, ns=(2, 2))
    registry.ensure(registry.resolve("old"))

    monkeypatch.setattr(
        "common.chairs.registry.shutil.disk_usage",
        lambda path: SimpleNamespace(free=10**6 if not recent.root.exists() else 0),
    )
    incoming = registry.ensure(registry.resolve("incoming"))

    assert old.root.is_dir()
    assert not recent.root.exists()
    assert incoming.root == registry.cache_root / "by-digest" / incoming.manifest_digest
    assert fetcher.roles == ["old", "recent", "incoming"]


def test_a_digest_another_ensure_holds_is_not_evicted(tmp_path, monkeypatch):
    from common.chairs import registry as registry_module

    registry, fetcher = _distinct_pins(tmp_path, ("busy", "incoming"))
    busy = registry.ensure(registry.resolve("busy"))
    monkeypatch.setattr(
        "common.chairs.registry.shutil.disk_usage", lambda path: SimpleNamespace(free=0)
    )

    with registry_module._digest_lock(busy.root.parent, busy.manifest_digest, "busy"):
        with pytest.raises(DiskSpaceRefusal):
            registry.ensure(registry.resolve("incoming"))

    assert busy.root.is_dir()


def test_insufficient_space_after_eviction_refuses_the_incoming_chair(tmp_path, monkeypatch):
    registry, fetcher = _distinct_pins(tmp_path, ("attestator_1", "attestator_2"))
    first = registry.ensure(registry.resolve("attestator_1"))
    monkeypatch.setattr(
        "common.chairs.registry.shutil.disk_usage", lambda path: SimpleNamespace(free=0)
    )

    with pytest.raises(DiskSpaceRefusal, match="container disk too small for chair"):
        registry.ensure(registry.resolve("attestator_2"))

    assert not first.root.exists()
    assert fetcher.roles == ["attestator_1"]


def test_a_fill_that_may_not_evict_refuses_and_keeps_every_other_cache(tmp_path, monkeypatch):
    """A background fill must not take a cache another step may be about to use."""
    registry, fetcher = _distinct_pins(tmp_path, ("attestator_1", "attestator_2"))
    first = registry.ensure(registry.resolve("attestator_1"))
    monkeypatch.setattr(
        "common.chairs.registry.shutil.disk_usage", lambda path: SimpleNamespace(free=0)
    )

    with pytest.raises(DiskSpaceRefusal, match="container disk too small for chair"):
        registry.ensure(registry.resolve("attestator_2"), evict=False)

    assert first.root.is_dir()
    assert fetcher.roles == ["attestator_1"]
    assert not [entry for entry in first.root.parent.iterdir() if ".candidate-" in entry.name], (
        "a refused fill leaves no candidate behind"
    )


def test_a_registry_adopts_another_s_verifications_only_for_the_same_roster(hf_world, monkeypatch):
    """PREFLIGHT's registry takes over what the bootstrap's background fill verified."""
    identity = hf_world.identity()
    hf_world.registry.ensure(identity)
    reads: list[str] = []
    real_digest = manifests.file_digest
    monkeypatch.setattr(
        manifests,
        "file_digest",
        lambda path, chair, relative: reads.append(relative) or real_digest(path, chair, relative),
    )

    adopting = registry_for(hf_world.registry.config, hf_world.tmp_path)
    adopting.adopt_verifications(hf_world.registry)
    remembered = adopting.ensure(identity)

    assert reads == []
    assert remembered.verification == {
        "bytes": "verified earlier in this process; no file changed since"
    }
    other_root = ChairRegistry(
        hf_world.registry.config,
        manifest_root=hf_world.tmp_path,
        cache_root=hf_world.tmp_path / "elsewhere",
    )
    other_root.adopt_verifications(hf_world.registry)
    assert other_root._verified == {}


# --- One flipped byte, a missing file, an extra file --------------------------------


def test_one_flipped_byte_fails_naming_that_file(hf_world):
    identity = hf_world.identity()
    hf_world.fetcher.files["nested/weights.bin"] = b"one corrupted byte lands here\n"

    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "nested/weights.bin" in str(caught.value)
    assert caught.value.chair == "attestator_1"


def test_a_file_the_fetch_never_produced_fails_naming_it(hf_world):
    identity = hf_world.identity()

    class Partial(RecordingFetcher):
        def fetch(self, identity, destination, paths):
            super().fetch(identity, destination, tuple(p for p in paths if p != "config.json"))

    hf_world.registry.fetcher = Partial(hf_world.files)
    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "config.json" in str(caught.value)
    assert "missing" in str(caught.value)


def test_an_extra_file_is_a_mismatch_not_a_shrug(hf_world):
    """Verification covers the whole fetched snapshot: a file that arrived but is
    not pinned is exactly as unverifiable as one whose bytes changed."""
    identity = hf_world.identity()

    class Generous(RecordingFetcher):
        def fetch(self, identity, destination, paths):
            super().fetch(identity, destination, paths)
            (destination / "z-unpinned.json").write_bytes(b"{}\n")

    hf_world.registry.fetcher = Generous(hf_world.files)
    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "z-unpinned.json" in str(caught.value)
    assert "extra" in str(caught.value)


def test_a_file_of_the_right_digest_but_the_wrong_size_is_refused(tmp_path):
    """Size and digest are both pinned, so a manifest row that agrees with the
    bytes on only one of them is a manifest that has been edited."""
    files = {"weights.bin": b"fixture weights\n"}
    write_snapshot(tmp_path / "remote", files)
    manifest = build_manifest(tmp_path / "remote")
    tampered = [dict(row.to_record(), size=999) for row in manifest.rows]
    manifest_path = tmp_path / "manifests" / "attestator_1.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(canonical_bytes(tampered))
    pin = digest_bytes(manifest_path.read_bytes())

    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", pin)})
    fetcher = RecordingFetcher(files)
    registry = registry_for(config, tmp_path, fetcher)

    with pytest.raises(DigestMismatchRefusal) as caught:
        registry.ensure(registry.resolve("attestator_1"))
    assert "size" in str(caught.value)


# --- A partial cache re-fetches exactly the missing files ---------------------------


def test_a_partial_cache_re_fetches_exactly_the_missing_files(hf_world):
    """The registry computes the missing set and asks for precisely that.

    This is the whole of the claim: the seam is handed one tuple of paths, and
    the assertion is on that tuple. The seam has no skip-if-present logic of its
    own, so a registry that simply re-requested everything would fail here.
    """
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    (snapshot.root / "nested/weights.bin").unlink()

    hf_world.registry.ensure(identity)

    assert hf_world.fetcher.calls == [
        ("attestator_1", ("config.json", "nested/weights.bin")),
        ("attestator_1", ("nested/weights.bin",)),
    ]


def test_a_repair_moves_the_files_it_keeps_instead_of_copying_them(hf_world, monkeypatch):
    """Carried files keep their inode, are hashed once by the repair, and stay remembered."""
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    kept_inode = (snapshot.root / "config.json").stat().st_ino
    (snapshot.root / "nested/weights.bin").unlink()
    monkeypatch.setattr(
        "common.chairs.registry.shutil.copyfile",
        lambda *args, **kwargs: pytest.fail("a carried file was copied"),
    )

    repaired = hf_world.registry.ensure(identity)
    digested = _digest_log(monkeypatch)
    again = hf_world.registry.ensure(identity)

    assert (repaired.root / "config.json").stat().st_ino == kept_inode
    assert repaired.root == snapshot.root
    assert digested == [], "the repaired cache is remembered, not hashed again"
    assert again.verification == {
        "bytes": "verified earlier in this process; no file changed since"
    }


def test_a_failed_repair_puts_the_files_it_carried_back(hf_world):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    kept_inode = (snapshot.root / "config.json").stat().st_ino
    (snapshot.root / "nested/weights.bin").unlink()
    hf_world.fetcher.files["nested/weights.bin"] = b"corrupted on the way back\n"

    with pytest.raises(DigestMismatchRefusal):
        hf_world.registry.ensure(identity)

    assert (snapshot.root / "config.json").stat().st_ino == kept_inode
    assert not (snapshot.root / "nested/weights.bin").exists()


def test_a_cache_holding_a_file_the_pin_does_not_name_is_refused_before_any_refetch(hf_world):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    (snapshot.root / "z-unpinned.json").write_bytes(b"{}\n")
    hf_world.fetcher.calls.clear()

    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "z-unpinned.json" in str(caught.value)
    assert hf_world.fetcher.calls == []


def test_a_cache_holding_tampered_bytes_is_refused_before_any_refetch(hf_world):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    # Keep the pinned size so this reaches the digest comparison itself.
    (snapshot.root / "nested/weights.bin").write_bytes(b"fixture weightX\n")
    hf_world.fetcher.calls.clear()

    with pytest.raises(DigestMismatchRefusal, match="cached bytes do not match") as caught:
        hf_world.registry.ensure(identity)
    assert "nested/weights.bin" in str(caught.value)
    assert hf_world.fetcher.calls == []


@pytest.mark.hostile_local
def test_a_cached_symlink_directory_is_refused_before_any_refetch(hf_world, tmp_path):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    outside = write_snapshot(tmp_path / "outside", {"weights.bin": b"outside\n"})
    (snapshot.root / "linked").symlink_to(outside, target_is_directory=True)
    hf_world.fetcher.calls.clear()

    with pytest.raises(DigestMismatchRefusal, match="linked: symlink directory"):
        hf_world.registry.ensure(identity)
    assert hf_world.fetcher.calls == []


# --- A failed verification leaves the previously verified snapshot untouched --------


def test_a_failed_verification_leaves_the_previously_verified_snapshot_untouched(hf_world):
    identity = hf_world.identity()
    verified = hf_world.registry.ensure(identity)
    before = {
        path.name: path.read_bytes() for path in sorted(verified.root.rglob("*")) if path.is_file()
    }

    # The cache is complete, so the corrupted byte has to arrive on a re-fetch of
    # a file the cache lost — the shape a real interrupted download leaves.
    (verified.root / "config.json").unlink()
    hf_world.fetcher.files["config.json"] = b"corrupted on the way back\n"
    with pytest.raises(DigestMismatchRefusal):
        hf_world.registry.ensure(identity)

    surviving = {
        path.name: path.read_bytes() for path in sorted(verified.root.rglob("*")) if path.is_file()
    }
    assert surviving == {name: data for name, data in before.items() if name != "config.json"}
    assert not any(
        ".candidate-" in entry.name
        for entry in (hf_world.registry.cache_root / "by-digest").iterdir()
    ), "a refused candidate snapshot is removed rather than left beside the cache"


def test_a_failed_fetch_leaves_the_previously_verified_snapshot_untouched(hf_world):
    identity = hf_world.identity()
    verified = hf_world.registry.ensure(identity)
    kept = (verified.root / "config.json").read_bytes()

    (verified.root / "config.json").unlink()
    hf_world.fetcher.fail = RuntimeError("the connection went away mid-download")
    with pytest.raises(UnresolvedChairRefusal):
        hf_world.registry.ensure(identity)

    hf_world.fetcher.fail = None
    assert hf_world.registry.ensure(identity).root == verified.root
    assert (verified.root / "config.json").read_bytes() == kept


# --- The manifest artifact is the thing the pin names -------------------------------


def test_the_pin_names_the_manifest_artifact_s_exact_canonical_bytes(tmp_path):
    """Not "a JSON file that happens to parse to the same rows": the pin is the
    digest of the artifact, so a second serialization of the same content is a
    different artifact and is refused before verification is even considered."""
    write_snapshot(tmp_path / "snapshot", {"weights.bin": b"fixture bytes\n"})
    manifest_path = tmp_path / "manifests" / "attestator_1.json"
    pin = pin_snapshot(tmp_path / "snapshot", manifest_path)

    assert digest_bytes(manifest_path.read_bytes()) == pin
    assert read_manifest(manifest_path, expected_digest=pin, chair="attestator_1").rows

    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    with pytest.raises(DigestMismatchRefusal, match="canonical artifact bytes"):
        read_manifest(manifest_path, expected_digest=pin, chair="attestator_1")


def test_a_manifest_whose_digest_is_not_the_pin_is_refused(tmp_path):
    write_snapshot(tmp_path / "snapshot", {"weights.bin": b"fixture bytes\n"})
    manifest_path = tmp_path / "manifests" / "attestator_1.json"
    pin_snapshot(tmp_path / "snapshot", manifest_path)

    with pytest.raises(DigestMismatchRefusal, match="expected digest"):
        read_manifest(manifest_path, expected_digest="0" * 64, chair="attestator_1")


@pytest.mark.parametrize(
    "rows",
    [
        {"rows": []},  # an object where the artifact is a bare sorted row list
        [{"path": "a", "sha256": "0" * 64}],  # no size
        [{"path": "a", "sha256": "0" * 64, "size": -1}],
        [{"path": "a", "sha256": "0" * 63, "size": 1}],
        [{"path": "../a", "sha256": "0" * 64, "size": 1}],
        [{"path": "/a", "sha256": "0" * 64, "size": 1}],
        [{"path": ".", "sha256": "0" * 64, "size": 1}],
        [  # "./" also names the directory itself: no parts, same rule as "."
            {"path": "./", "sha256": "0" * 64, "size": 1}
        ],
        [  # not sorted by path
            {"path": "b", "sha256": "0" * 64, "size": 1},
            {"path": "a", "sha256": "0" * 64, "size": 1},
        ],
        [  # the same path twice
            {"path": "a", "sha256": "0" * 64, "size": 1},
            {"path": "a", "sha256": "1" * 64, "size": 1},
        ],
    ],
)
def test_a_malformed_manifest_artifact_is_refused(tmp_path, rows):
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_bytes(rows))
    with pytest.raises(DigestMismatchRefusal):
        read_manifest(path, expected_digest=digest_bytes(path.read_bytes()), chair="attestator_1")


def test_a_manifest_that_cannot_be_read_at_all_is_refused_naming_the_chair(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b"{not json at all")
    with pytest.raises(DigestMismatchRefusal) as caught:
        read_manifest(path, expected_digest="0" * 64, chair="attestator_1")
    assert caught.value.chair == "attestator_1"


def test_manifest_read_is_bounded_before_json_deserialization(tmp_path, monkeypatch):
    """A manifest is a small control artifact, not model weight bytes, so an
    oversized one is refused before any of it is parsed as JSON."""
    monkeypatch.setattr(manifests, "MAX_MANIFEST_BYTES", 32)
    path = tmp_path / "manifest.json"
    path.write_bytes(b"{" + b"x" * 32)

    with pytest.raises(DigestMismatchRefusal, match="32-byte control-artifact limit"):
        read_manifest(path, expected_digest="0" * 64, chair="attestator_1")


@pytest.mark.hostile_local
def test_a_manifest_path_that_is_a_symlink_is_refused_rather_than_followed(tmp_path):
    """The target is a genuinely valid, one-row manifest pinned to its own real
    digest -- not an empty one `_validate_manifest` would reject outright -- so
    an unfixed reader that followed the symlink would return successfully
    (`DID NOT RAISE`), not fail for an unrelated reason. The refusal below is
    the symlink protection, and nothing else."""
    write_snapshot(tmp_path / "snapshot", {"weights.bin": b"fixture bytes\n"})
    real = tmp_path / "real-manifest.json"
    pin = pin_snapshot(tmp_path / "snapshot", real)
    link = tmp_path / "manifest.json"
    link.symlink_to(real)

    with pytest.raises(DigestMismatchRefusal, match="cannot read manifest"):
        read_manifest(link, expected_digest=pin, chair="attestator_1")


@pytest.mark.hostile_local
def test_a_manifest_fifo_is_refused_before_any_blocking_read(tmp_path):
    """A regression here is a hang, not a failure -- asserted from a worker
    thread with a deadline so it surfaces as a failed test rather than a suite
    that never finishes, matching this file's own established pattern
    (`test_a_validated_file_swapped_for_a_fifo_is_refused_instead_of_hanging_the_boot`)."""
    import threading

    path = tmp_path / "manifest.json"
    os.mkfifo(path)
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            read_manifest(path, expected_digest="0" * 64, chair="attestator_1")
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=15)

    assert not worker.is_alive(), "the read blocked on the FIFO instead of refusing"
    assert isinstance(outcome[0], DigestMismatchRefusal)
    assert "must be a regular file" in str(outcome[0])


@pytest.mark.hostile_local
def test_a_symlinked_file_inside_a_snapshot_is_refused_rather_than_followed(tmp_path):
    """Hashing through a symlink would let a snapshot verify against bytes that
    are not in it, and that live somewhere nothing pinned."""
    outside = write_snapshot(tmp_path / "outside", {"weights.bin": b"elsewhere\n"})
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "weights.bin").symlink_to(outside / "weights.bin")

    with pytest.raises(DigestMismatchRefusal, match="not a regular file"):
        build_manifest(root)


# --- The production client seam ------------------------------------------------------


def test_the_huggingface_adapter_passes_the_pin_through_to_the_official_client(tmp_path):
    """What the adapter asks the real client for: this repo, this revision, and
    exactly these paths. Nothing about the service's own behaviour is claimed."""
    from common.chairs.registry import HuggingFaceFetcher

    source = write_snapshot(tmp_path / "official-cache", {"nested/model.bin": b"official\n"})

    class OfficialClientFake:
        def __init__(self):
            self.kwargs = None

        def snapshot_download(self, **kwargs):
            self.kwargs = kwargs
            return source

    client = OfficialClientFake()
    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", "d" * 64)})
    identity = config.chairs["attestator_1"]
    destination = tmp_path / "candidate"

    HuggingFaceFetcher(client).fetch(identity, destination, ("nested/model.bin",))

    assert client.kwargs == {
        "repo_id": "fixture-org/attestator_1",
        "revision": "a" * 40,
        "allow_patterns": ["nested/model.bin"],
    }
    assert (destination / "nested/model.bin").read_bytes() == b"official\n"


def test_the_huggingface_adapter_refuses_a_pinned_file_the_client_did_not_return(tmp_path):
    from common.chairs.registry import HuggingFaceFetcher

    source = write_snapshot(tmp_path / "official-cache", {"other.bin": b"not what was asked\n"})

    class OfficialClientFake:
        def snapshot_download(self, **kwargs):
            return source

    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", "d" * 64)})
    with pytest.raises(UnresolvedChairRefusal, match="no requested file"):
        HuggingFaceFetcher(OfficialClientFake()).fetch(
            config.chairs["attestator_1"], tmp_path / "candidate", ("nested/model.bin",)
        )


def test_the_written_manifest_round_trips_through_its_own_reader(tmp_path):
    """The one writer and the one reader agree, which is what lets the pin be a
    constant the artifact must match rather than a value the artifact supplies."""
    root = write_snapshot(tmp_path / "snapshot", {"a.bin": b"a\n", "nested/b.bin": b"b\n"})
    manifest = build_manifest(root)
    path = tmp_path / "manifest.json"
    pin = write_manifest(manifest, path)

    assert json.loads(path.read_text(encoding="utf-8")) == manifest.to_record()
    assert manifest_digest(manifest) == pin
    assert read_manifest(path, expected_digest=pin, chair="attestator_1") == manifest


def test_the_measured_real_roster_pins_each_shipped_manifest():
    """The real roster's configured chairs name readable, measured manifests.

    This verifies only the committed manifest metadata and roster bindings. It
    does not fetch a snapshot, start a serving process, or claim an inference.
    """
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    configured = {
        role: chair for role, chair in real.chairs.items() if isinstance(chair, ChairIdentity)
    }

    assert set(configured) == {
        "attestator_1",
        "attestator_2",
        "attestator_3",
        "perlector",
        "reconstructor",
        "secondary_proposer",
        "designator_surya",
    }
    assert configured["perlector"].digest_manifest == configured["reconstructor"].digest_manifest
    for role, identity in configured.items():
        assert identity.digest_manifest != PRE_MATERIALIZATION_SENTINEL, role
        assert read_manifest(
            ROOT / "config" / identity.manifest,
            expected_digest=identity.digest_manifest,
            chair=role,
        ).rows


def test_the_real_surya_chair_and_the_store_pin_one_measured_bundle():
    """Surya's bundle has no Hub revision, so its manifest digest is the pin the
    launch-time fetch is checked against and the roster binds; the two are one fact."""
    from common.chairs.model_store import REQUIRED_ARTIFACTS

    identity = load_models_toml(ROOT / "config" / "models-real.toml").chairs["designator_surya"]
    (requirement,) = [item for item in REQUIRED_ARTIFACTS if item.chair == "designator_surya"]
    assert identity.source == requirement.source == "local-repository"
    assert identity.digest_manifest == requirement.digest_manifest
    assert identity.manifest == f"manifests/{requirement.artifact}.json"
    rows = read_manifest(
        ROOT / "config" / identity.manifest,
        expected_digest=identity.digest_manifest,
        chair="designator_surya",
    ).rows
    paths = {row.path for row in rows}
    assert {"surya-bundle.json", requirement.license_file} <= paths
    assert {
        "text_detection/2025_05_07/model.safetensors",
        "surya_layout2/rfdetr_layout.pth",
        "surya_layout2/order/order_ar.pt",
    } <= paths


def test_an_unmeasured_all_zero_pin_is_refused_by_name_before_anything_reads_it(tmp_path):
    """A synthetic pre-materialization sentinel is refused before it can serve.

    The parseable sentinel lets a launch materialize a roster, but every door
    that relies on a pin must refuse it before reading a manifest or producing
    provenance.  The shipped real roster has measured pins; this fixture keeps
    the generic refusal boundary covered without claiming otherwise.
    """
    snapshot = write_snapshot(tmp_path / "cache" / "attestator_1", {"model.bin": b"weights\n"})
    pin_snapshot(snapshot, tmp_path / "manifests" / "attestator_1.json")
    config = config_of(
        tmp_path,
        {"attestator_1": hf_chair("attestator_1", PRE_MATERIALIZATION_SENTINEL)},
    )
    registry = registry_for(config, tmp_path)
    identity = config.chairs["attestator_1"]

    with pytest.raises(ConfigurationRefusal, match="pre-materialization sentinel"):
        registry.ensure(identity)
    with pytest.raises(ConfigurationRefusal, match="pre-materialization sentinel"):
        registry.receipt(identity, serving_details())


def test_the_materialization_fetcher_separates_client_state_without_deleting_repo_bytes(tmp_path):
    """The Hugging Face client's bookkeeping must not become part of a pin.

    Client cache data can be nondeterministic, while a pinned repository may own
    its own `.cache` bytes. The adapter must isolate the client namespace rather
    than manifest it or delete repository content by name.
    """

    class CachedSnapshotClientFake:
        def snapshot_download(self, **kwargs):
            assert "local_dir" not in kwargs
            cache = Path(kwargs["cache_dir"])
            source = cache / "snapshot"
            source.mkdir(parents=True)
            (source / "config.json").write_bytes(b'{"pinned":true}')
            repository_cache = source / ".cache"
            repository_cache.mkdir()
            (repository_cache / "repository-owned.json").write_text(
                "pinned bytes", encoding="utf-8"
            )
            return str(source)

    destination = tmp_path / "staging"
    destination.mkdir()
    HuggingFaceMaterializationFetcher(CachedSnapshotClientFake()).fetch(
        "fixture-org/pinned", "a" * 40, destination
    )

    assert [
        path.relative_to(destination).as_posix()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    ] == [".cache/repository-owned.json", "config.json"]
    assert not Path(f"{destination}.huggingface-cache").exists()


@pytest.mark.hostile_local
def test_the_materialization_fetcher_refuses_a_symlinked_destination(tmp_path):
    class ClientMustNotRun:
        def snapshot_download(self, **kwargs):
            raise AssertionError("a symlinked destination must be refused before download")

    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "staging"
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DigestMismatchRefusal, match="existing empty regular directory"):
        HuggingFaceMaterializationFetcher(ClientMustNotRun()).fetch(
            "fixture-org/pinned", "a" * 40, destination
        )

    assert sorted(outside.iterdir()) == []


def test_the_materialization_fetcher_refuses_a_snapshot_outside_its_per_call_cache(tmp_path):
    outside = tmp_path / "unrelated-snapshot"
    outside.mkdir()
    (outside / "model.safetensors").write_bytes(b"unrelated bytes")

    class ReturnsUnrelatedDirectory:
        def snapshot_download(self, **kwargs):
            return outside

    destination = tmp_path / "staging"
    destination.mkdir()

    with pytest.raises(DigestMismatchRefusal, match="outside its per-call cache"):
        HuggingFaceMaterializationFetcher(ReturnsUnrelatedDirectory()).fetch(
            "fixture-org/pinned", "a" * 40, destination
        )

    assert sorted(destination.iterdir()) == []


@pytest.mark.hostile_local
def test_the_materialization_fetcher_never_reads_an_external_cache_symlink(tmp_path):
    outside = tmp_path / "operator-secret"
    outside.write_bytes(b"must not enter model evidence")

    class ReturnsExternalFileLink:
        def snapshot_download(self, **kwargs):
            source = Path(kwargs["cache_dir"]) / "snapshot"
            source.mkdir(parents=True)
            (source / "model.safetensors").symlink_to(outside)
            return source

    destination = tmp_path / "staging"
    destination.mkdir()

    # Unchanged bytes are not proof that nothing read them, and the claim in this
    # test's name is about the read. `_copy_verified_file` is the only step that
    # opens a returned file, and it opens through `os.open`, so a regression that
    # copied the operator's secret and then refused for some later reason trips
    # this guard rather than passing on an intact file.
    real_open = os.open

    def refuse_external_open(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == outside:
            raise AssertionError("the external symlink target was opened")
        return real_open(path, *args, **kwargs)

    with pytest.raises(DigestMismatchRefusal, match="external link targets are never read"):
        with unittest.mock.patch.object(os, "open", refuse_external_open):
            HuggingFaceMaterializationFetcher(ReturnsExternalFileLink()).fetch(
                "fixture-org/pinned", "a" * 40, destination
            )

    assert outside.read_bytes() == b"must not enter model evidence"
    assert sorted(destination.iterdir()) == []


@pytest.mark.hostile_local
def test_the_materialization_fetcher_copies_only_internal_cache_symlink_bytes(tmp_path):
    class ReturnsInternalBlobLink:
        def snapshot_download(self, **kwargs):
            cache = Path(kwargs["cache_dir"])
            blob = cache / "blobs" / "model"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"pinned model bytes")
            source = cache / "snapshots" / "revision"
            source.mkdir(parents=True)
            (source / "model.safetensors").symlink_to("../../blobs/model")
            return source

    destination = tmp_path / "staging"
    destination.mkdir()

    HuggingFaceMaterializationFetcher(ReturnsInternalBlobLink()).fetch(
        "fixture-org/pinned", "a" * 40, destination
    )

    copied = destination / "model.safetensors"
    assert not copied.is_symlink()
    assert copied.read_bytes() == b"pinned model bytes"


@pytest.mark.hostile_local
def test_the_materialization_fetcher_refuses_default_apfs_name_collisions(tmp_path):
    class ReturnsCaseCollidingFiles:
        def snapshot_download(self, **kwargs):
            source = Path(kwargs["cache_dir"]) / "snapshot"
            source.mkdir(parents=True)
            (source / "Weights.bin").write_bytes(b"first")
            (source / "weights.bin").write_bytes(b"second")
            return source

    destination = tmp_path / "staging"
    destination.mkdir()

    import unittest.mock

    from common.chairs import registry as registry_module

    # The collision check runs in `registry`, not in `model_store`. `os` is one
    # shared module object, so this replacement is process-wide for the block
    # below; it is spelled through `registry_module` only to say where the
    # checked code lives. The real `os.walk` signature is kept so an unrelated
    # positional caller inside the block cannot fail with `TypeError`.
    original_walk = registry_module.os.walk

    def case_sensitive_walk(top, topdown=True, onerror=None, followlinks=False):
        # A case-insensitive host filesystem collapses the planted spellings
        # into one file; deliver the listing a case-sensitive fetch cache would.
        for directory, directories, filenames in original_walk(
            top, topdown=topdown, onerror=onerror, followlinks=followlinks
        ):
            if any(name.lower() == "weights.bin" for name in filenames):
                filenames = sorted(set(filenames) | {"Weights.bin", "weights.bin"})
            yield directory, directories, filenames

    with unittest.mock.patch.object(registry_module.os, "walk", case_sensitive_walk):
        with pytest.raises(DigestMismatchRefusal, match="collide on default APFS"):
            HuggingFaceMaterializationFetcher(ReturnsCaseCollidingFiles()).fetch(
                "fixture-org/pinned", "a" * 40, destination
            )

    assert sorted(destination.iterdir()) == []


def test_a_per_call_cache_cleanup_failure_does_not_fail_the_fetch(tmp_path, monkeypatch):
    class CachedSnapshotClientFake:
        def snapshot_download(self, **kwargs):
            source = Path(kwargs["cache_dir"]) / "snapshot"
            source.mkdir(parents=True)
            (source / "model.safetensors").write_bytes(b"pinned bytes")
            return source

    cleanup_calls = []

    def refuse_cleanup(path, **kwargs):
        cleanup_calls.append(path)
        raise PermissionError("cache cleanup denied")

    monkeypatch.setattr("common.chairs.registry.shutil.rmtree", refuse_cleanup)
    destination = tmp_path / "staging"
    destination.mkdir()

    HuggingFaceMaterializationFetcher(CachedSnapshotClientFake()).fetch(
        "fixture-org/pinned", "a" * 40, destination
    )
    assert (destination / "model.safetensors").read_bytes() == b"pinned bytes"
    assert cleanup_calls == [Path(f"{destination}.huggingface-cache")]
    assert Path(f"{destination}.huggingface-cache").exists()


@pytest.mark.hostile_local
def test_a_validated_file_swapped_for_a_fifo_is_refused_instead_of_hanging_the_boot(tmp_path):
    """A check/use swap must end in a refusal, never in an open that never returns.

    Validation proves the name is a regular file, and the Hugging Face client
    still owns the per-call cache between then and the copy. Opening the name
    again without ``O_NONBLOCK`` blocks forever on a FIFO -- inside a pod boot,
    with the GPU billing, no journal step recorded and no reason printed -- so
    the identity check below it never runs, so this open carries ``O_NONBLOCK``
    as ``read_limited_bytes`` does.

    The refusal is asserted from a worker thread with a deadline: a regression
    here is a hang, and a hang must surface as a failed test rather than a suite
    that never finishes.
    """
    import os
    import threading
    import unittest.mock

    from common.chairs import registry as registry_module

    client_cache = tmp_path / "staging.huggingface-cache"

    class ReturnsOneRegularFile:
        def snapshot_download(self, **kwargs):
            snapshot = client_cache / "snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "model.safetensors").write_bytes(b"weights")
            return snapshot

    real_validate = registry_module._validated_materialization_files

    def swap_for_a_fifo(source, cache, repo):
        files = real_validate(source, cache, repo)
        for _relative, origin, _identity in files:
            origin.unlink()
            os.mkfifo(origin)
        return files

    destination = tmp_path / "staging"
    destination.mkdir()
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            with unittest.mock.patch.object(
                registry_module, "_validated_materialization_files", swap_for_a_fifo
            ):
                HuggingFaceMaterializationFetcher(ReturnsOneRegularFile()).fetch(
                    "fixture-org/pinned", "a" * 40, destination
                )
            outcome.append(None)
        # Broad on purpose: the worker thread must hand whatever it raised back
        # to the assertions below rather than die with it on a daemon thread.
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=15)

    assert not worker.is_alive(), "the copy blocked on the FIFO instead of refusing"
    assert isinstance(outcome[0], DigestMismatchRefusal)
    assert "no longer a regular file" in str(outcome[0])
    assert sorted(destination.iterdir()) == []
