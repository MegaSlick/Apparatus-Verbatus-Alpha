"""End-to-end fake drills for the operator's words and their recovery states."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tracemalloc
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from common.contracts.approval import ApprovalRecordReference
from common.contracts.canonical import canonical_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM
from operations.pod.transfer import TransferReport
from operations.submit import gate
from operations.submit import submit as submission_door
from operations.submit.submit import build_manifest, walk_folder

from . import cli, entry, notify_bridge
from . import surface as surface_module
from .errors import ErrorCode, OperatorError
from .local_volume import LocalFixtureObjectStore
from .records import MAX_RECORD_BYTES, DescriptorStore, ReceiptStore, RecordError, sha256_file
from .surface import (
    DOOR_PROGRAM,
    MAX_NOTIFY_MESSAGE_CHARACTERS,
    Faults,
    OperatorSurface,
    _declared_work,
    _door_module,
    _exported_work,
    _repository_commit_or_reason,
    reconciliation_table,
)
from .volume_s3 import VolumeSpec, VolumeTransferRefusal

UTC = timezone.utc
START = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _manifest(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "submitted-pages"
    source.mkdir()
    (source / "page-one.bin").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic page one\n")
    (source / "page-two.bin").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic page two\n")
    manifest = tmp_path / "sealed-submission.json"
    record = build_manifest(walk_folder(source))
    manifest.write_bytes(canonical_bytes(record))
    return source, manifest


def _approved_submission(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An approved storage root and a matching gate policy.

    Every other upload test in this file starts from an already-sealed
    manifest; this is what `submit_and_upload` needs to seal a new one through
    the submission door itself, mirroring `operations/submit/test_submit.py`'s own
    fixture shape.
    """

    approved = tmp_path / "approved-storage"
    source = approved / "batch"
    source.mkdir(parents=True)
    (source / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest_out = approved / "submission.json"
    return source, manifest_out, policy_path


def _surface(
    tmp_path: Path,
    *,
    workspace: Path = ROOT,
    faults: Faults | None = None,
    output: list[str] | None = None,
) -> OperatorSurface:
    messages = output if output is not None else []
    return OperatorSurface(
        workspace,
        tmp_path / "operator-state",
        now=lambda: START,
        present=messages.append,
        faults=faults,
    )


def _all_files(path: Path) -> dict[Path, bytes]:
    return {
        candidate.relative_to(path): candidate.read_bytes()
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file()
    }


def test_a_non_serializable_payload_is_a_named_record_error_not_a_raw_typeerror(
    tmp_path: Path,
) -> None:
    """A raw Decimal (every current call site carefully str()s one first) must

    still fail as a RecordError, so a future call site that forgets to would
    get this surface's specific write-failed copy rather than an unhandled
    TypeError leaking past it.
    """

    surface = _surface(tmp_path)

    with pytest.raises(RecordError, match="not serializable"):
        surface.receipts.write("run", {"amount": Decimal("1.23")})


def test_a_raw_float_in_a_receipt_payload_is_refused_not_silently_hashed(tmp_path: Path) -> None:
    """The same refusal the rest of the pipeline's canonical form already makes."""

    surface = _surface(tmp_path)

    with pytest.raises(RecordError, match="not serializable"):
        surface.receipts.write("run", {"amount": 1.5})


def test_receipt_reader_binds_the_kind_into_the_filename(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    receipt = surface.receipts.write("run", {"summary": "saved"})
    renamed = receipt.with_name("close-" + receipt.name.rsplit("-", 1)[-1])
    receipt.rename(renamed)

    with pytest.raises(RecordError, match="kind or digest"):
        surface.receipts.read(renamed)


@pytest.mark.hostile_local
def test_write_refuses_a_symlinked_receipts_directory(tmp_path: Path) -> None:
    """`list()` already refused this; `write()` must too, not write through it."""

    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "operator-state"
    state.mkdir()
    (state / "receipts").symlink_to(outside)
    store = ReceiptStore(state)

    with pytest.raises(RecordError, match="not a safe directory"):
        store.write("upload", {"summary": "hello"})

    assert list(outside.iterdir()) == []


@pytest.mark.hostile_local
def test_fixture_store_refuses_a_symlinked_root_without_chmodding_its_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_mode = outside.stat().st_mode
    root = tmp_path / "fixture-volume"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="root is not a safe directory"):
        LocalFixtureObjectStore(root)

    assert outside.stat().st_mode == original_mode


def test_a_repeated_receipt_never_corrupts_the_descriptors_own_history_invariant(
    tmp_path: Path,
) -> None:
    """A retried action that reproduces an earlier receipt's exact bytes must not

    leave the descriptor unreadable. `ReceiptStore.write` is content-addressed —
    identical payload and timestamp return the identical path — so recording the
    same receipt twice for one action, with a different receipt recorded for
    that action in between, is a real, reachable sequence, not a contrived one.
    """

    surface = _surface(tmp_path)
    first = surface.receipts.write("boot", {"summary": "first"})
    second = surface.receipts.write("boot", {"summary": "second"})
    surface.descriptor.record("boot", first)
    surface.descriptor.record("boot", second)

    surface.descriptor.record("boot", first)  # the repeat, after a different one

    loaded = surface.descriptor.load()
    assert loaded is not None
    # Entries are basenames: the receipt is content-addressed and the index
    # must survive the state directory being moved.
    assert loaded["actions"]["boot"] == first.name
    assert loaded["history"]["boot"][-1] == first.name
    assert loaded["history"]["boot"].count(first.name) == 1

    # And the descriptor must still be readable and writable afterward.
    third = surface.receipts.write("boot", {"summary": "third"})
    surface.descriptor.record("boot", third)
    assert surface.descriptor.load() is not None


def test_a_corrupted_saved_record_never_hides_the_intact_ledgers_behind_unexpected(
    tmp_path: Path,
) -> None:
    """A saved file can hold what the shared serializer refuses to hash.

    `status` reports an unreadable record beside the intact ones only for
    `RecordError`; a raw `TypeError` walks past that guard, abandons the whole
    listing, and reports `UNEXPECTED` — the opposite of the honesty ledger this
    verb exists to show.
    """

    surface = _surface(tmp_path)
    surface._write_action("export", {"summary": "saved"}, descriptor_action="export")
    receipt = surface._descriptor_receipt("export")
    assert receipt is not None

    record = json.loads(receipt.read_text(encoding="utf-8"))
    record["payload"] = {"summary": "hand-edited", "amount": 1.5}
    data = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    replacement = receipt.with_name(f"export-{hashlib.sha256(data).hexdigest()}.json")
    receipt.unlink()
    replacement.write_bytes(data)
    surface.descriptor.record("export", replacement)

    with pytest.raises(RecordError, match="not canonical"):
        surface.receipts.read(replacement)
    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE


def test_a_corrupted_descriptor_is_named_unreadable_rather_than_unclassifiable(
    tmp_path: Path,
) -> None:
    """The same guard on the index itself: `status` catches only `RecordError`."""

    surface = _surface(tmp_path)
    surface._write_action("export", {"summary": "saved"}, descriptor_action="export")
    descriptor = surface.descriptor.path
    raw = json.loads(descriptor.read_text(encoding="utf-8"))
    raw["actions"]["export"] = 1.5
    descriptor.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE


@pytest.mark.hostile_local
def test_a_control_sequence_in_a_saved_record_never_reaches_the_terminal(
    tmp_path: Path,
) -> None:
    """`errors.py` makes this argument for a detail; it holds for the channel.

    A page census's own refusal reason is free text that travels up into the
    Armarium aggregate, into `reconciliation_table`, into the export receipt,
    and back out of `status`. An escape sequence in one could clear the screen
    and paint a false "close verified" line above the real result.
    """

    spoof = "held\x1b[2J\x1b[H\x1b[32mVerbatus: close verified, $0.00 billed\x1b[0m"
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    surface._write_action(
        "export",
        {"summary": "saved", "reconciliation": [f"Recorded reason: {spoof}"]},
        descriptor_action="export",
    )
    messages.clear()

    surface.status()

    assert any("Verbatus: close verified" in line for line in messages)
    assert not any("\x1b" in line for line in messages)


def test_a_record_too_large_to_be_one_of_ours_is_refused_rather_than_read(
    tmp_path: Path,
) -> None:
    """`status` reads every recorded receipt, so an unbounded read is its own
    failure — and an out-of-memory kill prints nothing at all.
    """

    surface = _surface(tmp_path)
    surface._write_action("export", {"summary": "saved"}, descriptor_action="export")
    receipt = surface._descriptor_receipt("export")
    assert receipt is not None
    receipt.write_bytes(b"{" + b"x" * (MAX_RECORD_BYTES + 1))

    with pytest.raises(RecordError, match="larger than"):
        surface.receipts.read(receipt)

    surface.descriptor.path.write_bytes(b"{" + b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(RecordError, match="larger than"):
        surface.descriptor.load()


@pytest.mark.hostile_local
def test_a_fifo_at_a_recorded_path_cannot_hang_a_read_only_verb(tmp_path: Path) -> None:
    """Opening a FIFO for reading blocks until a writer appears, so the check
    has to be on the open descriptor and the open has to be non-blocking.
    """

    fifo = tmp_path / "sealed-submission.json"
    os.mkfifo(fifo)
    finished = threading.Event()
    refusal: list[BaseException] = []

    def digest() -> None:
        try:
            sha256_file(fifo)
        except OSError as error:
            refusal.append(error)
        finished.set()

    threading.Thread(target=digest, daemon=True).start()

    assert finished.wait(10), "sha256_file blocked on a FIFO instead of refusing it"
    assert refusal and "regular file" in str(refusal[0])


def _race_one_fixture_object(root: Path, sources: tuple[Path, Path]) -> list[BaseException]:
    """Start both writers on the same key at once and collect what was refused."""

    store = LocalFixtureObjectStore(root)
    refusals: list[BaseException] = []
    gate = threading.Barrier(len(sources))

    def put(source: Path) -> None:
        try:
            gate.wait()
            with source.open("rb") as handle:
                store.put_file(
                    "volume/page.bin",
                    handle,
                    expected_sha=hashlib.sha256(source.read_bytes()).hexdigest(),
                )
        except Exception as error:  # noqa: BLE001 - the refusal is exactly what is counted
            refusals.append(error)

    writers = [threading.Thread(target=put, args=(source,)) for source in sources]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    return refusals


def test_two_writers_cannot_both_claim_one_fixture_object(tmp_path: Path) -> None:
    """Asking `exists()` and replacing afterwards is two steps, and two writers
    that each saw the key absent each replaced the other's bytes with no
    refusal at all — 90 times in 400 before the claim became one step.
    """

    first = tmp_path / "a.bin"
    first.write_bytes(b"A" * 512)
    second = tmp_path / "b.bin"
    second.write_bytes(b"B" * 512)

    for attempt in range(40):
        refusals = _race_one_fixture_object(tmp_path / f"volume-{attempt}", (first, second))

        assert len(refusals) == 1, f"attempt {attempt}: {len(refusals)} refusals, expected 1"
        assert "different bytes" in str(refusals[0])


@pytest.mark.hostile_local
def test_a_fixture_object_key_that_is_a_symlink_is_never_verified(tmp_path: Path) -> None:
    root = tmp_path / "volume"
    (root / "volume").mkdir(parents=True)
    actual = root / "actual.bin"
    actual.write_bytes(b"payload")
    (root / "volume" / "page.bin").symlink_to(actual)

    assert LocalFixtureObjectStore(root).inspect("volume/page.bin") is None


@pytest.mark.hostile_local
@pytest.mark.parametrize("referent_bytes", (b"payload", b"different"))
def test_a_symlink_planted_during_fixture_object_publication_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    referent_bytes: bytes,
) -> None:
    """The FileExistsError comparison must inspect the key, not its referent."""

    root = tmp_path / "volume"
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    referent = tmp_path / "referent.bin"
    referent.write_bytes(referent_bytes)
    target = root / "objects" / "page.bin"
    real_link = os.link

    def plant_link(source_name, target_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        Path(target_name).symlink_to(referent)
        return real_link(source_name, target_name, *args, **kwargs)

    monkeypatch.setattr(os, "link", plant_link)
    store = LocalFixtureObjectStore(root)

    with source.open("rb") as handle:
        with pytest.raises(RuntimeError, match="not a readable object"):
            store.put_file(
                "objects/page.bin",
                handle,
                expected_sha=hashlib.sha256(b"payload").hexdigest(),
            )

    assert target.is_symlink()
    assert store.puts == []


@pytest.mark.hostile_local
def test_inspect_refuses_a_symlink_planted_as_the_object_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "volume"
    target = root / "objects" / "page.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"payload")
    referent = tmp_path / "referent.bin"
    referent.write_bytes(b"payload")
    real_os_open = os.open
    real_path_open = Path.open
    planted = False

    def plant() -> None:
        nonlocal planted
        if not planted:
            planted = True
            target.unlink()
            target.symlink_to(referent)

    def opening_descriptor(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path) == target:
            plant()
        return real_os_open(path, flags, *args, **kwargs)

    def opening_path(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == target:
            plant()
        return real_path_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening_descriptor)
    monkeypatch.setattr(Path, "open", opening_path)

    assert LocalFixtureObjectStore(root).inspect("objects/page.bin") is None
    assert target.is_symlink(), "the test did not drive the read-side substitution"


def test_submit_and_upload_seals_a_new_manifest_then_transfers_it(tmp_path: Path) -> None:
    """The `--manifest-out` route through the submission door, end to end.

    Every other upload test in this file starts from an already-sealed
    manifest; nothing exercised the door itself — sealing a brand new one,
    then transferring it — until this test.
    """

    surface = _surface(tmp_path)
    source, manifest_out, policy_path = _approved_submission(tmp_path)

    receipt = surface.submit_and_upload(
        source,
        manifest_out=manifest_out,
        policy_path=policy_path,
    )

    assert receipt.is_file()
    assert manifest_out.is_file()
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["state"] == "complete"


def test_submit_and_upload_refuses_by_name_when_the_door_refuses(tmp_path: Path) -> None:
    """A submission the gate refuses must reach `UPLOAD_REFUSED`, not a raw exception."""

    surface = _surface(tmp_path)
    _source, manifest_out, policy_path = _approved_submission(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "page.png").write_bytes(b"\x89PNG\r\n\x1a\nunapproved")

    with pytest.raises(OperatorError) as refusal:
        surface.submit_and_upload(
            elsewhere,
            manifest_out=manifest_out,
            policy_path=policy_path,
        )

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    assert not manifest_out.exists()


def test_the_default_upload_target_never_holds_a_whole_file_in_memory(tmp_path: Path) -> None:
    """This store is what `verbatus upload` uses by default, over real
    submitted pages — not test scaffolding. A submission is sized by what a
    person photographed, and reading one whole cost 512 MiB resident for a
    512 MiB page set.
    """

    source = tmp_path / "pages.bin"
    with source.open("wb") as handle:
        for _ in range(32):
            handle.write(b"z" * (1024 * 1024))
    store = LocalFixtureObjectStore(tmp_path / "volume")
    expected_sha = sha256_file(source)

    tracemalloc.start()
    try:
        with source.open("rb") as handle:
            store.put_file("volume/pages.bin", handle, expected_sha=expected_sha)
        _, put_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        observed = store.inspect("volume/pages.bin")
        _, inspect_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert observed is not None and observed.size == 32 * 1024 * 1024
    assert put_peak < 8 * 1024 * 1024, put_peak
    assert inspect_peak < 8 * 1024 * 1024, inspect_peak


def test_a_saved_receipt_is_named_when_its_descriptor_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)

    def refuse_index(self, action, receipt):  # type: ignore[no-untyped-def]
        del self, action, receipt
        raise RecordError("injected descriptor failure")

    monkeypatch.setattr(DescriptorStore, "record", refuse_index)
    with pytest.raises(OperatorError) as failure:
        surface._write_action(
            "run",
            {"summary": "the fact was saved before indexing failed"},
            descriptor_action="run",
        )

    assert failure.value.code is ErrorCode.RECORD_WRITE_FAILED
    assert failure.value.detail is not None and "Receipt saved at" in failure.value.detail
    assert len(surface.receipts.records_of_kind("run")) == 1


def test_upload_run_export_and_status_is_strictly_read_only(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source, manifest = _manifest(tmp_path)

    upload_receipt = surface.upload(source, sealed_manifest=manifest)
    outcome = surface.run(run_id="four-word-run", scenario="page-unbroken")
    bundle = surface.export(run_id="four-word-run")

    assert upload_receipt.is_file()
    assert outcome.state == "complete"
    assert bundle.is_file()
    assert any("page 1" in line and "page 2" in line for line in messages)
    assert any("Acts accounted for: act " in line for line in messages)
    assert not any("%" in line for line in messages)

    before = _all_files(surface.state_root)
    status_lines = surface.status()
    after = _all_files(surface.state_root)

    assert before == after
    assert any(line.startswith("- upload record") for line in status_lines)
    assert any(line.startswith("- export record") for line in status_lines)
    assert any("Reconciliation from the recorded Armarium export:" in line for line in status_lines)
    upload_payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert any(upload_payload["submission_manifest_sha256"] in line for line in status_lines)


def test_partial_upload_is_recorded_and_retries_from_verified_work(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, faults=Faults(partial_upload=True), output=messages)
    source, manifest = _manifest(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        surface.upload(source, sealed_manifest=manifest)

    assert refusal.value.code is ErrorCode.UPLOAD_PARTIAL
    partial = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert partial["state"] == "partial-transfer"
    completed = surface.upload(source, sealed_manifest=manifest)

    assert completed.is_file()
    assert any("zero GPU-hours" in line for line in messages)
    status = surface.status()
    assert any("Upload is partial" in line for line in status)
    assert any("Upload is complete" in line for line in status)


def test_upload_publishes_the_manifest_only_after_every_image_verifies(tmp_path: Path) -> None:
    class FailSecondImage(LocalFixtureObjectStore):
        def put_file(self, key, source_handle, *, expected_sha):  # type: ignore[no-untyped-def]
            if key == "submission/page-two.bin":
                raise RuntimeError("injected second-image failure")
            super().put_file(key, source_handle, expected_sha=expected_sha)

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    volume = tmp_path / "volume"

    with pytest.raises(OperatorError) as refusal:
        surface.upload(
            source,
            sealed_manifest=manifest,
            target=FailSecondImage(volume),
        )

    assert refusal.value.code is ErrorCode.UPLOAD_PARTIAL
    assert (volume / "submission" / "page-one.bin").is_file()
    assert not (volume / "submission-manifest.json").exists()

    surface.upload(
        source,
        sealed_manifest=manifest,
        target=LocalFixtureObjectStore(volume),
    )
    assert (volume / "submission-manifest.json").read_bytes() == manifest.read_bytes()


def test_upload_sends_only_the_sealed_pages_and_their_manifest(tmp_path: Path) -> None:
    """Whatever else sits in the source folder never leaves the machine."""

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    (source / "notes-added-after-sealing.txt").write_bytes(b"private note\n")
    store = LocalFixtureObjectStore(tmp_path / "volume")

    surface.upload(source, sealed_manifest=manifest, target=store)

    assert sorted(store.puts) == [
        "submission-manifest.json",
        "submission-manifest.sha256",
        "submission/page-one.bin",
        "submission/page-two.bin",
    ]


def test_upload_reuses_an_identical_published_submission_without_new_writes(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    store = LocalFixtureObjectStore(tmp_path / "volume")

    surface.upload(source, sealed_manifest=manifest, target=store)
    first_writes = tuple(store.puts)
    surface.upload(source, sealed_manifest=manifest, target=store)

    assert first_writes == (
        "submission-manifest.sha256",
        "submission/page-one.bin",
        "submission/page-two.bin",
        "submission-manifest.json",
    )
    assert tuple(store.puts) == first_writes
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["transfer"]["completed_keys"] == []
    assert payload["transfer"]["skipped_keys"] == [
        "submission/page-one.bin",
        "submission/page-two.bin",
    ]


def test_upload_refuses_a_different_manifest_before_writing_its_foreign_image(
    tmp_path: Path,
) -> None:
    """One immutable manifest owns a prefix; a changed batch must touch no object there."""

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    store = LocalFixtureObjectStore(tmp_path / "volume")
    surface.upload(source, sealed_manifest=manifest, target=store)

    changed_source = tmp_path / "changed-pages"
    shutil.copytree(source, changed_source)
    (changed_source / "foreign-page.bin").write_bytes(
        b"\x89PNG\r\n\x1a\nmust never enter the sealed prefix\n"
    )
    changed_manifest = tmp_path / "changed-submission.json"
    changed_manifest.write_bytes(canonical_bytes(build_manifest(walk_folder(changed_source))))
    before = _all_files(store.root)
    store.puts.clear()

    with pytest.raises(OperatorError) as refusal:
        surface.upload(changed_source, sealed_manifest=changed_manifest, target=store)

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    assert "submission-manifest.json" in str(refusal.value.detail)
    assert store.puts == []
    assert _all_files(store.root) == before
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["state"] == "manifest-conflict"


def test_concurrent_conflicting_uploads_leave_one_permanent_prefix_owner(
    tmp_path: Path,
) -> None:
    barrier = threading.Barrier(2)

    class RacingStore(LocalFixtureObjectStore):
        def create_file(self, key, source_handle, *, expected_sha):  # type: ignore[no-untyped-def]
            if key == "submission-manifest.sha256":
                barrier.wait(timeout=5)
            super().create_file(key, source_handle, expected_sha=expected_sha)

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_source, first_manifest = _manifest(first_root)
    second_source, second_manifest = _manifest(second_root)
    (second_source / "foreign-page.bin").write_bytes(
        b"\x89PNG\r\n\x1a\nbelongs only to the second batch\n"
    )
    second_manifest.write_bytes(canonical_bytes(build_manifest(walk_folder(second_source))))
    store = RacingStore(tmp_path / "volume")
    outcomes: list[Path | OperatorError] = []

    def upload(surface, source, manifest):  # type: ignore[no-untyped-def]
        try:
            outcomes.append(surface.upload(source, sealed_manifest=manifest, target=store))
        except OperatorError as error:
            outcomes.append(error)

    threads = [
        threading.Thread(
            target=upload,
            args=(_surface(tmp_path / "state-one"), first_source, first_manifest),
        ),
        threading.Thread(
            target=upload,
            args=(_surface(tmp_path / "state-two"), second_source, second_manifest),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sum(isinstance(outcome, Path) for outcome in outcomes) == 1
    refusals = [outcome for outcome in outcomes if isinstance(outcome, OperatorError)]
    assert len(refusals) == 1 and refusals[0].code is ErrorCode.UPLOAD_REFUSED
    published_manifest = (store.root / "submission-manifest.json").read_bytes()
    assert published_manifest in {first_manifest.read_bytes(), second_manifest.read_bytes()}
    published_sha = hashlib.sha256(published_manifest).hexdigest()
    assert (store.root / "submission-manifest.sha256").read_bytes() == (
        f"{published_sha}\n".encode("ascii")
    )
    manifest_record = json.loads(published_manifest)
    expected = {
        Path("submission-manifest.json"),
        Path("submission-manifest.sha256"),
        *(Path("submission") / row["relative_path"] for row in manifest_record["files"]),
    }
    assert set(_all_files(store.root)) == expected


def test_a_named_prefix_adds_an_independent_immutable_batch_on_one_volume(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)
    first_source, first_manifest = _manifest(tmp_path)
    store = LocalFixtureObjectStore(tmp_path / "volume")
    surface.upload(first_source, sealed_manifest=first_manifest, target=store)

    second_source = tmp_path / "second-batch"
    second_source.mkdir()
    (second_source / "page-three.bin").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic page three\n")
    second_manifest = tmp_path / "second-submission.json"
    second_manifest.write_bytes(canonical_bytes(build_manifest(walk_folder(second_source))))

    surface.upload(
        second_source,
        sealed_manifest=second_manifest,
        prefix="batch-02",
        target=store,
    )

    assert (store.root / "submission-manifest.json").read_bytes() == first_manifest.read_bytes()
    assert (store.root / "batch-02-manifest.json").read_bytes() == second_manifest.read_bytes()
    assert (
        store.root / "batch-02" / "page-three.bin"
    ).read_bytes() == b"\x89PNG\r\n\x1a\nsynthetic page three\n"


def test_upload_uses_one_sealed_manifest_snapshot_across_the_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid replacement restored before the final hash cannot change what is sent."""

    surface = _surface(tmp_path)
    source = tmp_path / "submitted-pages"
    source.mkdir()
    (source / "page-one.bin").write_bytes(b"\x89PNG\r\n\x1a\nfirst\n")
    manifest = tmp_path / "sealed-submission.json"
    original = canonical_bytes(build_manifest(walk_folder(source)))
    manifest.write_bytes(original)
    (source / "page-two.bin").write_bytes(b"\x89PNG\r\n\x1a\nsecond\n")
    replacement = canonical_bytes(build_manifest(walk_folder(source)))
    real_resume = surface_module.ChecksummedTransfer.resume

    def swap_restore(self):  # type: ignore[no-untyped-def]
        manifest.write_bytes(replacement)
        try:
            return real_resume(self)
        finally:
            manifest.write_bytes(original)

    monkeypatch.setattr(surface_module.ChecksummedTransfer, "resume", swap_restore)

    surface.upload(source, sealed_manifest=manifest)

    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["transfer"]["completed_keys"] == ["submission/page-one.bin"]
    assert payload["submission_manifest_sha256"] == hashlib.sha256(original).hexdigest()


def test_a_nothing_to_transfer_report_does_not_read_as_upload_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The top-level receipt state must agree with the nested transfer record.

    Not reachable today through `upload()` itself -- the sealed manifest
    snapshot it writes always exists by the time `resume()` checks for one --
    but the two fields must still be derived from one fact, not asserted
    separately, so a future caller of this same receipt shape cannot drift.
    """
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source = tmp_path / "submitted-pages"
    source.mkdir()
    (source / "page-one.bin").write_bytes(b"\x89PNG\r\n\x1a\nfirst\n")
    manifest = tmp_path / "sealed-submission.json"
    manifest.write_bytes(canonical_bytes(build_manifest(walk_folder(source))))

    monkeypatch.setattr(
        surface_module.ChecksummedTransfer,
        "resume",
        lambda self: TransferReport((), (), submission_manifest_present=False),
    )

    surface.upload(source, sealed_manifest=manifest)

    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["state"] == "nothing-to-transfer"
    assert payload["transfer"]["state"] == "nothing-to-transfer"
    assert "complete" not in payload["summary"]
    assert not any("Upload complete" in line for line in messages)
    assert any("nothing was transferred" in line for line in messages)


def test_upload_refuses_an_oversized_manifest_before_constructing_a_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)
    source = tmp_path / "submitted-pages"
    source.mkdir()
    manifest = tmp_path / "sealed-submission.json"
    manifest.write_bytes(b" " * (surface_module.MAX_SEALED_MANIFEST_BYTES + 1))

    def should_not_construct(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the transfer was constructed for an oversized manifest")

    monkeypatch.setattr(surface_module, "ChecksummedTransfer", should_not_construct)

    with pytest.raises(OperatorError) as refusal:
        surface.upload(source, sealed_manifest=manifest)

    assert refusal.value.code is ErrorCode.UPLOAD_MANIFEST_MISSING
    assert not (surface.state_root / "fixture-volume").exists()


def test_upload_refuses_a_bad_sealed_manifest_as_refused_not_partial(tmp_path: Path) -> None:
    """G13: upload validates the immutable snapshot through `submission_door.load_manifest`
    before a target is inspected or a file sent."""

    surface = _surface(tmp_path)
    source = tmp_path / "submitted-pages"
    source.mkdir()
    (source / "page-one.bin").write_bytes(b"\x89PNG\r\n\x1a\nfirst\n")
    manifest = tmp_path / "sealed-submission.json"
    manifest.write_bytes(b'{"not": "a canonical submission manifest"}')

    with pytest.raises(OperatorError) as refusal:
        surface.upload(source, sealed_manifest=manifest)

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["state"] == "manifest-refused"
    assert (
        payload["submission_manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert payload["zero_gpu_hours"] is True
    # Nothing was transferred: the fixture volume directory may already exist
    # (it is prepared before the manifest is parsed), but no object may be in it.
    fixture_volume = surface.state_root / "fixture-volume"
    assert list(fixture_volume.rglob("*")) == []


def test_laptop_crash_leaves_resumable_pages_and_acts(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, faults=Faults(laptop_crash=True), output=messages)

    with pytest.raises(OperatorError) as interruption:
        surface.run(run_id="laptop-crash-run", scenario="page-unbroken")

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    interrupted = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert interrupted["state"] == "interrupted-recoverable"
    resumed = surface.run(run_id="laptop-crash-run", scenario="page-unbroken")

    assert resumed.state == "complete"
    assert any("Resuming run laptop-crash-run" in line for line in messages)
    assert any("page 1" in line for line in messages)
    assert any("Acts accounted for: act " in line for line in messages)


def test_status_empty_has_the_same_plain_language_contract(tmp_path: Path) -> None:
    surface = _surface(tmp_path)

    with pytest.raises(OperatorError) as empty:
        surface.status()

    assert empty.value.code is ErrorCode.STATUS_EMPTY
    labels = ("What happened:", "What it means:", "Next step:")
    assert all(label in empty.value.render() for label in labels)


def test_console_parser_never_prints_a_raw_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["launch"])
    captured = capsys.readouterr().out

    assert exit_code == 2
    assert "What happened:" in captured
    assert "What it means:" in captured
    assert "Next step:" in captured
    assert "Traceback" not in captured


def test_every_declared_verb_with_a_required_flag_has_an_interactive_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reflect over the parser's own verbs rather than hand-listing one at a time.

    A hand-picked check of, say, only `ingest` would still pass the day a new verb
    is added to `build_parser()` with a required flag and no matching branch in
    `_interactive_arguments`: the trailing fallthrough there returns the bare verb
    name, and argparse then refuses for "missing required argument" — a person at
    the double-click window gets no prompt for the fact it needed, only a cryptic
    parse failure. Walking the parser's declared subcommands and their `required`
    flags means a future verb is covered automatically, not by remembering to add
    another hand-written case here.
    """
    parser = cli.build_parser()
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert subparsers_action.choices, "the parser declared no verbs to enumerate"

    for verb, subparser in subparsers_action.choices.items():
        required_flags = sorted(
            option
            for action in subparser._actions
            for option in action.option_strings
            if action.required
        )
        # A required mutually exclusive group is the same hazard by another
        # spelling: argparse refuses "one of the arguments ... is required" and
        # the person at the double-click window gets the same cryptic parse
        # failure. `upload` declares one today over --sealed-manifest and
        # --manifest-out, and `action.required` is False on both of its members,
        # so walking actions alone would never see it.
        required_groups = [
            sorted(option for action in group._group_actions for option in action.option_strings)
            for group in subparser._mutually_exclusive_groups
            if group.required
        ]
        answers = iter([verb, *(f"placeholder-{index}" for index in range(20))])
        monkeypatch.setattr("builtins.input", lambda _prompt, answers=answers: next(answers))
        arguments = cli._interactive_arguments()
        missing = [flag for flag in required_flags if flag not in arguments]
        assert not missing, (
            f"verb {verb!r} declares required flag(s) {missing} that the double-click "
            "interactive route never asks for or supplies"
        )
        for options in required_groups:
            assert any(option in arguments for option in options), (
                f"verb {verb!r} declares a required choice between {options} that the "
                "double-click interactive route never asks for or supplies"
            )


def test_sealed_manifest_upload_refuses_a_new_policy_instead_of_ignoring_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[tuple[Path, Path]] = []

    class ObservedSurface:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def upload(self, source: Path, *, sealed_manifest: Path, prefix: str, volume=None) -> None:
            del prefix, volume
            uploads.append((source, sealed_manifest))

    monkeypatch.setattr(cli, "OperatorSurface", ObservedSurface)

    result = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "upload",
            "--source",
            str(tmp_path / "source"),
            "--sealed-manifest",
            str(tmp_path / "sealed.json"),
            "--policy",
            str(tmp_path / "new-policy.toml"),
        ]
    )

    assert result == 2
    assert uploads == []
    assert "existing sealed record already carries the policy" in capsys.readouterr().out


def test_cli_upload_forwards_a_named_prefix_for_both_manifest_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[str, str]] = []

    class ObservedSurface:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def upload(self, _source: Path, *, sealed_manifest: Path, prefix: str, volume=None) -> None:
            del sealed_manifest, volume
            observed.append(("sealed", prefix))

        def submit_and_upload(
            self,
            _source: Path,
            *,
            manifest_out: Path,
            policy_path=None,
            prefix: str,
            volume=None,
        ) -> None:
            del manifest_out, policy_path, volume
            observed.append(("new", prefix))

    monkeypatch.setattr(cli, "OperatorSurface", ObservedSurface)
    common = ["--workspace", str(tmp_path), "upload", "--source", str(tmp_path / "source")]

    assert (
        cli.main(
            [
                *common,
                "--sealed-manifest",
                str(tmp_path / "sealed.json"),
                "--prefix",
                "batch-two",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                *common,
                "--manifest-out",
                str(tmp_path / "new.json"),
                "--prefix",
                "batch-three/",
            ]
        )
        == 0
    )
    assert observed == [("sealed", "batch-two"), ("new", "batch-three")]


# `submission/two` joins these: a nested prefix writes image keys below it
# while control files land as siblings inside the default `submission/`
# inventory.
@pytest.mark.parametrize(
    "prefix", ("/absolute", "../escape", "a//b", "a/./b", "bad\x00key", "submission/two")
)
def test_cli_upload_refuses_an_unsafe_object_prefix(prefix: str) -> None:
    with pytest.raises(OperatorError) as refusal:
        cli.build_parser().parse_args(
            [
                "upload",
                "--source",
                "source",
                "--sealed-manifest",
                "sealed.json",
                "--prefix",
                prefix,
            ]
        )
    assert refusal.value.code is ErrorCode.INVALID_COMMAND
    assert "safe relative key component" in str(refusal.value.detail)


def test_cli_run_carries_real_ingress_options_to_the_operator_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class ObservedSurface:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            observed.update(kwargs)

    monkeypatch.setattr(cli, "OperatorSurface", ObservedSurface)
    source = tmp_path / "approved" / "source"
    ledger = tmp_path / "approved" / "ledger.json"
    policy = tmp_path / "policy.json"
    # `run` is refused before dispatch on a workspace that is not a checkout;
    # this test is about the arguments reaching the surface, so the workspace
    # carries the three directories a run reads.
    for resource in ("pipeline", "config", "proof"):
        (tmp_path / resource).mkdir()

    assert (
        cli.main(
            [
                "--workspace",
                str(tmp_path),
                "run",
                "--run-id",
                "real-run",
                "--submission-folder",
                str(source),
                "--submission-manifest",
                str(ledger),
                "--data-gate-policy",
                str(policy),
            ]
        )
        == 0
    )
    assert observed["submission_folder"] == source
    assert observed["submission_manifest"] == ledger
    assert observed["data_gate_policy"] == policy


@pytest.mark.parametrize(
    "orphan",
    [
        "submission_manifest",
        "data_gate_policy",
        "triage_decision_manifest",
        "triage_producer_recipe",
    ],
)
def test_run_refuses_a_real_ingress_control_without_a_submission_folder(
    tmp_path: Path, orphan: str
) -> None:
    surface, observed = _recording_surface(tmp_path, faults=Faults(laptop_crash=True))

    with pytest.raises(OperatorError) as refusal:
        surface.run(run_id="orphan-real-option", **{orphan: tmp_path / "value"})

    assert refusal.value.code is ErrorCode.INVALID_COMMAND
    assert f"--{orphan.replace('_', '-')}" in str(refusal.value.detail)
    assert "--submission-folder" in str(refusal.value.detail)
    assert not observed, "the fault drill launched the Door before validating its option shape"


def _recording_surface(
    tmp_path: Path, *, faults: Faults | None = None, output: list[str] | None = None
) -> tuple[OperatorSurface, list[tuple[list[str], Path | None]]]:
    """Record child launches; successful stubs let crash drills reach interruption."""

    observed: list[tuple[list[str], Path | None]] = []

    def runner(command, **kwargs):  # type: ignore[no-untyped-def]
        observed.append((command, kwargs.get("cwd")))
        return subprocess.CompletedProcess(command, 0, "", "")

    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        now=lambda: START,
        present=(output if output is not None else []).append,
        faults=faults,
        runner=runner,
    )
    return surface, observed


def _argv_value(command: list[str], flag: str) -> str:
    assert flag in command, f"{flag} never reached the child's argv"
    return command[command.index(flag) + 1]


def test_real_ingress_paths_are_made_absolute_against_the_operators_own_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative input paths bind before the child changes cwd to the workspace."""

    elsewhere = tmp_path / "operator-home"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    surface, observed = _recording_surface(tmp_path)

    with pytest.raises(OperatorError):
        surface.run(
            run_id="relative-real",
            submission_folder=Path("approved/submitted-pages"),
            submission_manifest=Path("approved/submission-ledger.json"),
            data_gate_policy=Path("data-gate-policy.json"),
        )

    command, cwd = observed[0]
    assert cwd == ROOT, "the child no longer runs from the workspace; re-read this test's premise"
    assert _argv_value(command, "--submission-folder") == str(
        elsewhere / "approved" / "submitted-pages"
    )
    assert _argv_value(command, "--submission-manifest") == str(
        elsewhere / "approved" / "submission-ledger.json"
    )
    assert _argv_value(command, "--data-gate-policy") == str(elsewhere / "data-gate-policy.json")


def test_a_triage_manifest_reaches_the_door_beside_the_real_submission(tmp_path: Path) -> None:
    """`verbatus prepare` hands the Door its geometry through these two flags."""

    surface, observed = _recording_surface(tmp_path)
    manifest = tmp_path / "prepared" / "triage-decision-manifest.json"
    recipe = tmp_path / "prepared" / "triage-producer-recipe.json"

    with pytest.raises(OperatorError):
        surface.run(
            run_id="prepared-real",
            submission_folder=tmp_path / "scans",
            triage_decision_manifest=manifest,
            triage_producer_recipe=recipe,
        )

    command, _cwd = observed[0]
    assert _argv_value(command, "--triage-decision-manifest") == str(manifest)
    assert _argv_value(command, "--triage-producer-recipe") == str(recipe)


@pytest.mark.hostile_local
def test_real_ingress_absolutization_preserves_a_symlink_for_the_doors_gate(
    tmp_path: Path,
) -> None:
    """The operator must not erase a redirect before the Door inspects it."""

    approved = tmp_path / "approved"
    actual = approved / "actual-pages"
    actual.mkdir(parents=True)
    submitted_link = approved / "submitted-link"
    submitted_link.symlink_to(actual, target_is_directory=True)
    surface, observed = _recording_surface(tmp_path)

    with pytest.raises(OperatorError):
        surface.run(run_id="symlink-real", submission_folder=submitted_link)

    command, _cwd = observed[0]
    assert Path(_argv_value(command, "--submission-folder")) == submitted_link
    assert Path(_argv_value(command, "--submission-folder")).is_symlink()


def test_the_laptop_crash_drill_still_carries_real_ingress_to_the_door(tmp_path: Path) -> None:
    """A real-route fault drill must not fall back to fixture ingress."""

    source = tmp_path / "approved" / "submitted-pages"
    manifest = tmp_path / "approved" / "submission-ledger.json"
    policy = tmp_path / "data-gate-policy.json"
    messages: list[str] = []
    surface, observed = _recording_surface(
        tmp_path, faults=Faults(laptop_crash=True), output=messages
    )

    with pytest.raises(OperatorError) as interruption:
        surface.run(
            run_id="crash-real",
            submission_folder=source,
            submission_manifest=manifest,
            data_gate_policy=policy,
        )

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    command, _cwd = observed[0]
    assert command[1] == str(ROOT / DOOR_PROGRAM), "the drill no longer runs the Door"
    assert _argv_value(command, "--submission-folder") == str(source)
    assert _argv_value(command, "--submission-manifest") == str(manifest)
    assert _argv_value(command, "--data-gate-policy") == str(policy)
    interrupted = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert interrupted["ingress"] == "real"
    assert interrupted["last_observed_work"] == "The real submission's pages reached the Door."
    assert any("real submission reached the Door" in line for line in messages)
    assert not any("fixture pages" in line for line in messages)


def test_the_laptop_crash_drill_does_not_mask_the_doors_refusal_report(
    tmp_path: Path,
) -> None:
    messages: list[str] = []

    def partial_door(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            "Private named refusal report: 1_exemplar/artifacts/refusal-report/report.json\n",
        )

    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        present=messages.append,
        faults=Faults(laptop_crash=True),
        runner=partial_door,
    )

    with pytest.raises(OperatorError) as interruption:
        surface.run(
            run_id="partial-door-crash",
            submission_folder=tmp_path / "approved" / "submitted-pages",
        )

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    assert any("Private named refusal report:" in line for line in messages)
    assert any("laptop-crash drill interrupted" in line for line in messages)


def test_pipeline_children_do_not_receive_upload_only_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_environment: dict[str, str] = {}
    monkeypatch.setenv("RUNPOD_S3_ACCESS_KEY", "upload-access-secret")
    monkeypatch.setenv("RUNPOD_S3_SECRET_KEY", "upload-secret-secret")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    def record_environment(command, **kwargs):  # type: ignore[no-untyped-def]
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "", "")

    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        present=lambda _line="": None,
        faults=Faults(laptop_crash=True),
        runner=record_environment,
    )

    with pytest.raises(OperatorError) as interruption:
        surface.run(run_id="credential-boundary")

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    assert "RUNPOD_S3_ACCESS_KEY" not in observed_environment
    assert "RUNPOD_S3_SECRET_KEY" not in observed_environment
    assert observed_environment["VERBATUS_STAGE_TEST_SENTINEL"] == "preserved"


def test_pipeline_children_do_not_receive_any_provider_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the transfer's own two S3 keys -- every provider
    credential a decoder RCE in a stage reached by a submitted page could spend
    (pod creation money included) must stay off this environment
    (`credential_free_environment`).
    """

    observed_environment: dict[str, str] = {}
    credential_names = (
        "RUNPOD_API_KEY",
        "RUNPOD_S3_ACCESS_KEY",
        "RUNPOD_S3_SECRET_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
    )
    for name in credential_names:
        monkeypatch.setenv(name, f"secret-for-{name}")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    def record_environment(command, **kwargs):  # type: ignore[no-untyped-def]
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "", "")

    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        present=lambda _line="": None,
        faults=Faults(laptop_crash=True),
        runner=record_environment,
    )

    with pytest.raises(OperatorError) as interruption:
        surface.run(run_id="credential-boundary-2")

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    for name in credential_names:
        assert name not in observed_environment, f"{name} reached a stage subprocess"
    assert observed_environment["VERBATUS_STAGE_TEST_SENTINEL"] == "preserved"


def test_a_failed_real_run_receipt_names_real_ingress(tmp_path: Path) -> None:
    """A receipt may retain fixture configuration without calling it the input."""

    observed: list[list[str]] = []

    def fail_after_recording(command, **_kwargs):  # type: ignore[no-untyped-def]
        observed.append(command)
        return subprocess.CompletedProcess(command, 2, "", "the real Designator refused")

    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        present=lambda _line="": None,
        runner=fail_after_recording,
    )
    with pytest.raises(OperatorError) as failure:
        surface.run(
            run_id="failed-real",
            submission_folder=tmp_path / "approved" / "submitted-pages",
            submission_manifest=tmp_path / "approved" / "submission-ledger.json",
        )

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert observed
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["ingress"] == "real"


def test_a_real_run_is_never_narrated_with_the_declared_fixtures_pages(tmp_path: Path) -> None:
    """Fixture names are false for real runs; policy also keeps real names off-screen."""

    folder = tmp_path / "approved" / "submitted-pages"
    folder.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        (folder / name).write_bytes(b"\x89PNG\r\n\x1a\nnot really a page, and never opened here")
    manifest = tmp_path / "approved" / "submission-ledger.json"
    manifest.write_bytes(canonical_bytes(build_manifest(walk_folder(folder))))
    messages: list[str] = []
    surface, _observed = _recording_surface(tmp_path, output=messages)

    with pytest.raises(OperatorError):
        surface.run(
            run_id="narration",
            submission_folder=folder,
            submission_manifest=manifest,
            data_gate_policy=tmp_path / "data-gate-policy.json",
        )

    declared_pages, declared_ok = _declared_work(ROOT, "happy")
    assert declared_ok, "the declared fixture is unreadable; this test proves nothing"
    for name in declared_pages:
        assert not any(name in line for line in messages), (
            f"a real run was narrated with the declared fixture's {name!r}"
        )
    assert any("extent is recorded by the submitted filename ledger" in line for line in messages)
    for name in ("page-1.png", "page-2.png"):
        assert not any(name in line for line in messages), (
            "a submitted filename reached the terminal; the policy's logging rule allows "
            "counts and the private report location there, not real names"
        )


def test_door_module_leaves_no_trace_in_sys_path_or_sys_modules():
    """Loading door.py in-process runs its own `sys.path.insert` calls and
    bare sibling imports (`admission`, `manifest`, `pdf_render`,
    `render_config`, `image_formats`); both must be fully undone, or a second
    call (potentially against a different --workspace) would silently reuse
    the first call's cached copies instead of loading the new one's, and this
    operator's own sys.path would grow by three entries every single call."""
    import sys

    before_path = list(sys.path)
    before_modules = set(sys.modules)

    module = _door_module(ROOT)

    assert sys.path == before_path
    assert set(sys.modules) == before_modules
    assert hasattr(module, "fixture_pages_for_scenario")

    # Twice, to prove the second call reloads rather than serving the first
    # call's now-purged-from-sys.modules but still-referenced module object.
    _door_module(ROOT)
    assert sys.path == before_path
    assert set(sys.modules) == before_modules


def test_exported_work_names_every_delivered_and_non_delivered_act():
    """The closing accounting line reads a completed run's own export
    record, so it can name an act (like ink-free-page's minted fallback) that
    no fixture declaration could have known about in advance."""
    pages, acts = _exported_work(
        [{"ordinal": 1, "outcome": "sealed"}, {"ordinal": 2, "outcome": "refused"}],
        {
            "delivered": [{"act_key": "a1"}],
            "non_delivered": [{"act_key": "a2"}, {"act_key": "page-fallback:3"}],
        },
    )
    assert pages == ["page 1", "page 2"]
    assert acts == ["act a1", "act a2", "act page-fallback:3"]


def test_exported_work_falls_back_to_a_generic_placeholder_on_empty_records():
    """An export record with no page or act rows at all (rather than a
    malformed one) is not this function's failure to diagnose; it prints a
    placeholder rather than an empty, unreadable list."""
    pages, acts = _exported_work([], {})
    assert pages == ["the recorded pages"]
    assert acts == ["the recorded acts"]


def test_exported_work_discloses_rather_than_silently_drops_malformed_rows():
    """A record this function cannot make sense of does not raise -- the
    accounting line's own job is to report what a completed run produced, not
    to re-validate the export schema a stricter reader already checked -- but
    it is also not simply dropped, which would reopen the names-versus-total
    mismatch: the caller prints `len(page_records)`/`expected_acts` as the total beside
    these names, and naming fewer than that with no explanation is the same
    silent partial result that is always refused."""
    pages, acts = _exported_work(
        [{"ordinal": 1}, {"no_ordinal": True}, "not-a-dict"],
        {"delivered": [{"act_key": "a1"}, {"no_act_key": True}], "non_delivered": ["not-a-dict"]},
    )
    assert pages == ["page 1", "2 unreadable page record(s)"]
    assert acts == ["act a1", "2 unreadable act record(s)"]


def test_exported_work_treats_a_non_list_delivered_or_non_delivered_as_absent():
    """A string is iterable character-by-character; without this guard
    `"a1"` would silently become three one-character 'acts'."""
    pages, acts = _exported_work(
        [{"ordinal": 1}],
        {"delivered": "not-a-list", "non_delivered": [{"act_key": "a2"}]},
    )
    assert pages == ["page 1"]
    assert acts == ["act a2"]


def test_exported_work_never_reads_a_bool_ordinal_as_a_page_number():
    """`isinstance(True, int)` is true in Python; without excluding `bool`
    explicitly a boolean ordinal would render as 'page True'."""
    pages, _acts = _exported_work([{"ordinal": True}], {})
    assert pages == ["1 unreadable page record(s)"]


def test_exported_work_sorts_acts_by_key_regardless_of_delivery_order():
    """Delivered and non-delivered are each already sorted on their own, but
    concatenating two sorted lists is not itself sorted -- a held a2 before a
    delivered a1 must still read a1-then-a2."""
    pages, acts = _exported_work(
        [],
        {"delivered": [{"act_key": "a2"}], "non_delivered": [{"act_key": "a1"}]},
    )
    assert acts == ["act a1", "act a2"]


def test_the_operator_does_not_read_the_ledger_before_the_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Door checks storage policy before any operator-path ledger read."""

    folder = tmp_path / "approved" / "submitted-pages"
    folder.mkdir(parents=True)
    manifest = tmp_path / "approved" / "submission-ledger.json"
    manifest.write_text("{not canonical json", encoding="utf-8")
    monkeypatch.setattr(
        submission_door,
        "load_manifest",
        lambda _path: pytest.fail("the operator read the ledger before the Door's gate"),
    )
    messages: list[str] = []
    surface, observed = _recording_surface(tmp_path, output=messages)

    with pytest.raises(OperatorError):
        surface.run(run_id="bad-ledger", submission_folder=folder, submission_manifest=manifest)

    assert any("Door checks against the data-handling policy" in line for line in messages)
    assert observed, "the run never reached the Door, which is the only thing that can refuse it"


def test_console_interrupt_never_prints_a_raw_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(_value, *, verb):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_network_volume", interrupt)

    assert cli.main(["status"]) == 2
    captured = capsys.readouterr().out
    assert "What happened:" in captured
    assert "What it means:" in captured
    assert "Next step:" in captured
    assert "Traceback" not in captured


def test_console_interrupt_at_the_interactive_prompt_never_prints_a_raw_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl+C while answering "What would you like to do?" is a failure like

    any other, and `cli.main` is a directly-tested entry point: nothing enforces
    that it is only ever reached through `entry.py`'s outer handler.
    """

    monkeypatch.setattr(
        "builtins.input", lambda _prompt="": (_ for _ in ()).throw(KeyboardInterrupt)
    )

    assert cli.main([]) == 2
    captured = capsys.readouterr().out
    assert "What happened:" in captured
    assert "What it means:" in captured
    assert "Next step:" in captured
    assert "Traceback" not in captured


@pytest.mark.hostile_local
def test_cli_print_strips_control_bytes_but_keeps_its_lines_separate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`cli.py`'s own prints must strip control bytes the same way `present()`
    already does — the argument for stripping (a path or reason an operator
    did not choose can carry one) does not stop at the boundary between the
    two output channels. Stripped per line, not over the whole string at
    once: a multi-part rendered error depends on its own newlines.
    """

    cli._print("safe\x1b[2Jfirst line\nsecond line\x00 still here")

    captured = capsys.readouterr().out
    assert "\x1b" not in captured
    assert "\x00" not in captured
    assert "first line" in captured
    assert "second line" in captured
    assert "\n" in captured.strip()


def test_a_run_whose_recorded_aggregate_has_no_status_fails_as_run_failed_not_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed Armarium export record must reach the named `run` failure code.

    `aggregate["status"]` and (below) `run_record["run_root"]` sat outside any
    local try, so a missing key there raised a bare `KeyError` that only the
    CLI's outermost catch-all could reach — reported as `UNEXPECTED` instead of
    the code the rest of this failure path already uses and whose copy
    actually names the right next step.
    """

    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {}
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="broken-aggregate")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "status" in failure.value.render()


def test_a_non_list_pages_record_is_a_named_run_failure_not_a_character_count(
    tmp_path: Path,
) -> None:
    """A string at `pages` must never become a confident wrong page count.

    len() of a string counts characters, and that number would go on the one
    line that says whether a parish was accounted for — and into the milestone
    the phone receives.
    """

    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": "not a list of page records",
        "delivered": [],
        "non_delivered": [],
        "expected_acts": 2,
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="string-pages")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "not a list" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreadable"
    assert receipt["state"] != "complete"


def test_run_refuses_a_complete_aggregate_with_no_act_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aggregate["status"] == "complete"` is not enough on its own."""

    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    import operations.operator.surface as surface_module

    monkeypatch.setattr(
        surface_module,
        "verify_final_seal",
        lambda _tree: {
            "payload": {
                "aggregate": {"status": "complete", "reasons": []},
                "pages": [{"ordinal": 1}],
                "non_delivered": [],
                "expected_acts": 1,
            }
        },
    )

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="missing-delivered")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "missing delivered" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreadable"
    assert receipt["state"] != "complete"


@pytest.mark.parametrize("member", ("pages", "delivered", "non_delivered"))
def test_the_export_reader_refuses_non_list_members_before_any_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    """The real `_armarium_export` guard, driven with a malformed artifact.

    Every other export test monkeypatches `_armarium_export` itself, so the
    validation inside it would be dead code to the suite without this. The
    other two required members are given as well-formed empty lists so the
    failure is unambiguously about `member`, not about one checked earlier
    in the loop.
    """

    surface = _surface(tmp_path)
    import operations.operator.surface as surface_module

    payload = {"aggregate": {}, "pages": [], "delivered": [], "non_delivered": []}
    payload[member] = "not a list"
    monkeypatch.setattr(surface_module, "verify_final_seal", lambda _tree: {"payload": payload})
    with pytest.raises(ValueError, match=f"{member} is not a list"):
        surface._armarium_export(tmp_path, "r1")


@pytest.mark.parametrize("member", ("pages", "delivered", "non_delivered"))
def test_the_export_reader_refuses_a_member_missing_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    """A record that omits `member` outright is refused, not defaulted to empty."""

    surface = _surface(tmp_path)
    import operations.operator.surface as surface_module

    payload = {"aggregate": {}, "pages": [], "delivered": [], "non_delivered": []}
    del payload[member]
    monkeypatch.setattr(surface_module, "verify_final_seal", lambda _tree: {"payload": payload})
    with pytest.raises(ValueError, match=f"missing {member}"):
        surface._armarium_export(tmp_path, "r1")


def test_run_refuses_a_complete_aggregate_whose_partition_undercounts_expected_acts(
    tmp_path: Path,
) -> None:
    """`delivered`/`non_delivered` being present lists is not enough on its own."""

    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [{"ordinal": 1}],
        "delivered": [],
        "non_delivered": [],
        "expected_acts": 3,
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="undercounted-partition")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "reconcile to 3 distinct act" in (failure.value.detail or "")
    assert "0 record(s), 0 distinct" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreconciled"
    assert receipt["state"] != "complete"


def test_run_refuses_a_complete_aggregate_whose_partition_double_counts_one_act(
    tmp_path: Path,
) -> None:
    """A raw `len()` cannot tell a duplicated act from two distinct ones.

    Two entries naming the same `act_key` -- split across `delivered` and
    `non_delivered`, or repeated in one of them -- could pad the raw count
    to match `expected_acts` while a real, different act is missing
    entirely. Reconciliation counts distinct act identities, not rows.
    """
    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [{"ordinal": 1}],
        "delivered": [{"act_key": "a1"}],
        "non_delivered": [{"act_key": "a1"}],
        "expected_acts": 2,
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="doubled-act")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "2 record(s), 1 distinct" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreconciled"
    assert receipt["state"] != "complete"


def test_run_refuses_a_complete_aggregate_with_a_malformed_act_record(
    tmp_path: Path,
) -> None:
    """A partition entry with no readable `act_key` cannot be reconciled --
    counting it toward `expected_acts` anyway would let a malformed entry
    stand in for a real act."""
    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [{"ordinal": 1}],
        "delivered": [{"act_key": "a1"}, {"no_act_key": True}],
        "non_delivered": [],
        "expected_acts": 2,
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="malformed-act")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "not a readable act record" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreconciled"
    assert receipt["state"] != "complete"


def test_run_refuses_a_complete_aggregate_with_an_empty_act_key(
    tmp_path: Path,
) -> None:
    """An empty string satisfies `isinstance(..., str)` but names no act."""
    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [{"ordinal": 1}],
        "delivered": [{"act_key": ""}],
        "non_delivered": [],
        "expected_acts": 1,
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="empty-act-key")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "not a readable act record" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreconciled"
    assert receipt["state"] != "complete"


def test_a_held_run_raises_run_held_not_run_failed(
    tmp_path: Path,
) -> None:
    """A hold is the pipeline asking a person to decide, not a failure."""

    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "held", "reasons": ["one act needs review"]},
        "pages": [],
        "expected_acts": 0,
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="held-run")

    assert failure.value.code is ErrorCode.RUN_HELD
    assert "could not reach" not in failure.value.render()


def test_a_malformed_reasons_field_is_named_unreadable_not_silently_dropped(
    tmp_path: Path,
) -> None:
    """A non-list `reasons` must not vanish into "no reason recorded".

    The guard against a string-as-reasons or mapping-as-reasons value is
    correct: only a list may feed the hold-reason lines. But replacing a
    malformed value with `[]` and saying nothing throws away the operator's
    only clue that the Armarium record held something it could not read.
    """

    messages: list[str] = []
    notifications: list[tuple[str, str]] = []
    surface = _surface(tmp_path, output=messages)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "held", "reasons": "not a list"},
        "pages": [],
        "expected_acts": 0,
    }

    def record_notification(event: str, message: str):  # type: ignore[no-untyped-def]
        notifications.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = record_notification

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="held-run")

    assert failure.value.code is ErrorCode.RUN_HELD
    assert any(line.startswith("Hold reason: UNREADABLE.") for line in messages)
    assert any("hold reasons were not a list and were not read" in line for line in messages)
    assert len(notifications) == 1
    assert notifications[0][0] == "decision"
    assert "hold reasons were not a list and were not read" in notifications[0][1]
    assert "no reason recorded" not in notifications[0][1]


def test_an_unbounded_notification_message_is_truncated_before_it_is_sent(
    tmp_path: Path,
) -> None:
    """A held run with hundreds of unsealed pages must not build a
    notification message with no ceiling at all -- the transport already
    truncates its own failure-detail string this way; the outbound message
    needs the same treatment."""

    notifications: list[tuple[str, str]] = []
    surface = _surface(tmp_path)

    def record_notification(event: str, message: str):  # type: ignore[no-untyped-def]
        notifications.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = record_notification

    long_message = "; ".join(f"page {i} was corrupt: reason {i}" for i in range(200))
    assert len(long_message) > MAX_NOTIFY_MESSAGE_CHARACTERS

    surface._notify("decision", long_message)

    assert len(notifications) == 1
    sent = notifications[0][1]
    # The ceiling this test is named for covers the whole sent message,
    # suffix included -- not the ceiling plus the suffix's own length.
    assert len(sent) <= MAX_NOTIFY_MESSAGE_CHARACTERS
    assert sent.startswith(long_message[:80])
    assert sent.endswith("(truncated; see the run receipt for the full text)")


def test_a_short_notification_message_is_untouched(tmp_path: Path) -> None:
    notifications: list[tuple[str, str]] = []
    surface = _surface(tmp_path)

    def record_notification(event: str, message: str):  # type: ignore[no-untyped-def]
        notifications.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = record_notification

    surface._notify("milestone", "a short message")

    assert notifications == [("milestone", "a short message")]


def test_a_held_runs_missing_expected_act_total_is_named_on_screen(
    tmp_path: Path,
) -> None:
    """ "total not recorded" is still a legitimate display for a *held* run:
    reconciliation is a precondition for claiming `complete`, not for every
    state a record can carry, and a hold is the pipeline asking a person to
    decide, with no completed accounting to reconcile yet.
    """
    messages: list[str] = []
    notifications: list[tuple[str, str]] = []
    surface = _surface(tmp_path, output=messages)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "held", "reasons": ["one act needs review"]},
        "pages": [],
        "delivered": [],
        "non_delivered": [],
    }

    def record_notification(event: str, message: str):  # type: ignore[no-untyped-def]
        notifications.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = record_notification

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="missing-expected-total")

    assert failure.value.code is ErrorCode.RUN_HELD
    assert any("Acts accounted for:" in line and "total not recorded" in line for line in messages)
    assert notifications == [
        (
            "decision",
            "Verbatus run missing-expected-total is held and needs a decision: "
            "one act needs review",
        )
    ]
    assert "None" not in "\n".join(messages + [notifications[0][1]])


def test_a_complete_aggregate_with_no_expected_acts_is_refused_not_displayed_as_unknown(
    tmp_path: Path,
) -> None:
    """`expected_acts` is a precondition for claiming `complete`, not an optional display
    value -- the real producer always writes it alongside
    `delivered`/`non_delivered` (`pipeline/7_armarium/run.py`), so a record missing
    it is exactly the shape a foreign or mismatched-schema record takes. Before this
    fix a `complete` record with no `expected_acts` skipped reconciliation entirely
    and displayed "total not recorded" as if that were a normal, honest outcome."""
    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = lambda run_root, run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete"},
        "pages": [],
        "delivered": [],
        "non_delivered": [],
    }

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="missing-expected-total")

    assert failure.value.code is ErrorCode.RUN_FAILED
    assert "expected_acts" in (failure.value.detail or "")
    receipt = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert receipt["state"] == "armarium-record-unreconciled"
    assert receipt["state"] != "complete"


def test_a_run_whose_declared_fixture_cannot_be_read_says_so(tmp_path: Path) -> None:
    """A fixture that cannot be read must not silently become a placeholder."""

    workspace = tmp_path / "workspace-with-no-fixture"
    workspace.mkdir()
    messages: list[str] = []
    launched: list[object] = []
    surface = OperatorSurface(
        workspace,
        tmp_path / "operator-state",
        now=lambda: START,
        present=messages.append,
    )

    def never(*args, **kwargs):  # type: ignore[no-untyped-def]
        launched.append(args)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    surface.runner = never  # type: ignore[method-assign]

    with pytest.raises(OperatorError) as refusal:
        surface.run(run_id="unreadable-fixture-run")

    assert refusal.value.code is ErrorCode.NOT_A_CHECKOUT
    assert "declared fixture could not be read" in str(refusal.value.detail)
    assert str(workspace / "proof") in str(refusal.value.detail)
    assert launched == [], "the orchestrator must not start on a workspace with no fixture"
    assert not (tmp_path / "operator-state" / "receipts").exists(), (
        "nothing started, nothing recorded"
    )


def test_re_exporting_a_run_after_the_tree_changed_does_not_overwrite_the_first_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier export's receipt names its bundle's path *and* its exact digest."""

    surface = _surface(tmp_path)
    surface.run(run_id="re-export-run", scenario="page-unbroken")

    contents = iter([b"first export bytes", b"second export bytes; run tree since changed"])

    def fake_bundle(self, run_root, run_id, destination):  # type: ignore[no-untyped-def]
        del self, run_root, run_id
        destination.write_bytes(next(contents))

    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", fake_bundle)

    first_bundle = surface.export(run_id="re-export-run")
    first_receipt = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    second_bundle = surface.export(run_id="re-export-run")
    second_receipt = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]

    assert first_bundle != second_bundle
    assert first_bundle.is_file(), "the first bundle must still exist, unmodified"
    assert first_bundle.read_bytes() == b"first export bytes"
    assert second_bundle.read_bytes() == b"second export bytes; run tree since changed"
    assert first_receipt["sha256"] == hashlib.sha256(b"first export bytes").hexdigest()
    assert first_receipt["sha256"] == sha256_file(first_bundle)
    assert second_receipt["sha256"] == sha256_file(second_bundle)


def test_export_refuses_a_run_whose_armarium_completion_seal_is_gone(tmp_path: Path) -> None:
    """An export record under an unsealed Armarium is not a completed export to copy out."""

    surface = _surface(tmp_path)
    outcome = surface.run(run_id="unsealed-export-run", scenario="page-unbroken")
    assert outcome.state == "complete"
    armarium = outcome.run_root / outcome.run_id / "7_armarium"
    (seal,) = (armarium / "artifacts" / "stage-seal").glob("*.json")
    seal.unlink()

    with pytest.raises(OperatorError) as refusal:
        surface.export(run_id="unsealed-export-run")

    assert refusal.value.code is ErrorCode.EXPORT_UNSEALED
    assert "stage-seal" in str(refusal.value.detail)
    exports = surface.state_root / "exports"
    assert not exports.exists() or list(exports.iterdir()) == []


@pytest.mark.parametrize("change", ("seal-refused", "record-replaced"))
def test_export_refuses_a_seal_that_changed_while_the_evidence_was_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The copied evidence must still be what the seal witnessed once the copy is done."""

    surface = _surface(tmp_path)
    surface.run(run_id="copy-window-run", scenario="page-unbroken")
    real_verify = surface_module.verify_final_seal
    calls = 0

    def changes_after_the_first_check(tree):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        record = real_verify(tree)
        if calls == 1:
            return record
        if change == "seal-refused":
            raise SchemaRefusal("simulated: armarium stage-seal no longer verifies")
        return {**record, "payload": {**record["payload"], "scenario": "another"}}

    monkeypatch.setattr(surface_module, "verify_final_seal", changes_after_the_first_check)

    with pytest.raises(OperatorError) as refusal:
        surface.export(run_id="copy-window-run")

    assert calls == 2
    assert refusal.value.code is ErrorCode.EXPORT_UNSEALED
    exports = surface.state_root / "exports"
    assert list(exports.iterdir()) == []
    failure = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    assert failure["state"] != "complete"


def test_export_refuses_a_symlink_at_an_existing_content_addressed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "linked-content-address"
    surface._write_action(
        "run",
        {
            "summary": "test run",
            "state": "complete",
            "run_root": str(run_root),
            "run_id": run_id,
        },
        descriptor_action="run",
    )
    surface._armarium_export = lambda _root, _run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [],
        "delivered": [],
        "non_delivered": [],
        "expected_acts": 0,
    }
    bundle_bytes = b"new evidence bundle"

    def fake_bundle(self, _root, _run_id, destination):  # type: ignore[no-untyped-def]
        del self
        destination.write_bytes(bundle_bytes)

    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", fake_bundle)
    exports = surface.state_root / "exports"
    exports.mkdir(parents=True)
    digest = hashlib.sha256(bundle_bytes).hexdigest()
    destination = exports / f"{run_id}-armarium-base-{digest}.zip"
    outside = tmp_path / "outside-existing-file"
    outside.write_bytes(b"not the evidence bundle")
    destination.symlink_to(outside)

    with pytest.raises(OperatorError) as refusal:
        surface.export(run_id=run_id)

    assert refusal.value.code is ErrorCode.EXPORT_FAILED
    assert outside.read_bytes() == b"not the evidence bundle"
    assert destination.is_symlink()
    failure = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    assert failure["state"] == "local-copy-failed"


def test_status_reads_a_refused_upload_receipt_without_calling_it_unreadable(
    tmp_path: Path,
) -> None:
    """The verb every failure message sends the operator to must survive that failure."""

    output: list[str] = []
    surface = _surface(tmp_path, output=output)
    surface._record_failure("upload", "submission-refused", "the submitted folder was refused")

    surface.status()

    printed = "\n".join(output)
    assert "UNREADABLE" not in printed
    assert "submission-refused" in printed
    # A receipt that does claim bytes moved is still held to the digest.
    surface._write_action(
        "upload",
        {"summary": "Upload is complete.", "state": "complete", "zero_gpu_hours": True},
        descriptor_action="upload",
    )
    with pytest.raises(OperatorError) as refusal:
        surface.status()
    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE


def test_an_empty_armarium_is_refused_rather_than_bundled_as_complete(tmp_path: Path) -> None:
    """A directory proved to exist was never proved to hold anything.

    `7_armarium` was checked for its kind, so an empty one passed: the walk
    wrote zero members, the writer returned normally, and `export` recorded
    `"state": "complete"` for a bundle carrying `run.json` and not one
    established reading. For a parish run that is every act in it missing, with
    a receipt vouching for the absence -- "'complete' is refused
    unless everything reconciles", through one more door.
    """

    surface = _surface(tmp_path)
    run_id = "empty-armarium"
    run_root = tmp_path / "runs"
    (run_root / run_id / "7_armarium").mkdir(parents=True)
    (run_root / run_id / "run.json").write_text("{}", encoding="utf-8")
    surface._write_action(
        "run",
        {
            "summary": "test run",
            "state": "complete",
            "run_root": str(run_root),
            "run_id": run_id,
        },
        descriptor_action="run",
    )
    surface._armarium_export = lambda _root, _run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [],
        "delivered": [],
        "non_delivered": [],
        "expected_acts": 0,
    }

    with pytest.raises(OperatorError) as refusal:
        surface.export(run_id=run_id)

    assert refusal.value.code is ErrorCode.EXPORT_FAILED
    assert "holds no evidence files" in str(refusal.value.detail)
    exports = surface.state_root / "exports"
    assert list(exports.glob("*.zip")) == []
    assert list(exports.glob("*.staged")) == []
    assert list(exports.glob(".*tmp*")) == []


def test_export_refuses_a_complete_record_whose_partition_does_not_reconcile(
    tmp_path: Path,
) -> None:
    """`export` reads the same Armarium record `run()` does and must refuse a
    "complete" claim it cannot reconcile the same way -- a run this session
    independently reviewed pointed out that the reconciliation added for
    `run()` lived only there: a run refused for exactly this reason still
    leaves a receipt naming it, and `export --run-id` reads that receipt's
    run tree fresh, so the same unreconciled record could still be exported
    as complete through the door `run()` had already closed. No bundle
    should be written, and no receipt should ever claim `complete`.
    """
    surface = _surface(tmp_path)
    run_id = "export-side-reconciliation"
    run_root = tmp_path / "runs"
    (run_root / run_id / "7_armarium").mkdir(parents=True)
    (run_root / run_id / "run.json").write_text("{}", encoding="utf-8")
    surface._write_action(
        "run",
        {
            "summary": "test run",
            "state": "complete",
            "run_root": str(run_root),
            "run_id": run_id,
        },
        descriptor_action="run",
    )
    surface._armarium_export = lambda _root, _run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [],
        "delivered": [],
        "non_delivered": [],
        "expected_acts": 3,
    }

    with pytest.raises(OperatorError) as refusal:
        surface.export(run_id=run_id)

    assert refusal.value.code is ErrorCode.EXPORT_UNRECONCILED
    assert "reconcile to 3 distinct act" in str(refusal.value.detail)
    exports = surface.state_root / "exports"
    assert list(exports.glob("*.zip")) == []
    assert list(exports.glob("*.staged")) == []


def test_a_structural_export_refusal_still_leaves_a_receipt_and_no_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` must be able to show a failed export, whatever refused it.

    Every structural refusal in `_write_base_armarium_bundle` -- a missing
    `run.json`, a member of the wrong kind, a name collision, a member that
    changed while it was copied -- raises `OperatorError`, which the handler here
    did not catch. The export failed with no receipt written and the staged file
    left in `exports/`, so the operator was told to run `status` and `status` had
    nothing to show them.
    """

    surface = _surface(tmp_path)
    run_id = "structurally-refused"
    surface._write_action(
        "run",
        {
            "summary": "test run",
            "state": "complete",
            "run_root": str(tmp_path / "runs"),
            "run_id": run_id,
        },
        descriptor_action="run",
    )
    surface._armarium_export = lambda _root, _run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [],
        "delivered": [],
        "non_delivered": [],
        "expected_acts": 0,
    }

    def refuse_structurally(self, _root, _run_id, destination):  # type: ignore[no-untyped-def]
        del self, destination
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail="the Armarium evidence bundle cannot be written as complete: run.json is missing",
        )

    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", refuse_structurally)

    with pytest.raises(OperatorError) as refusal:
        surface.export(run_id=run_id)

    assert refusal.value.code is ErrorCode.EXPORT_FAILED
    failure = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    assert failure["state"] == "local-copy-failed"
    assert "run.json is missing" in str(failure)
    assert list((surface.state_root / "exports").glob("*.staged")) == []


def test_exporting_a_run_record_with_no_saved_run_root_fails_as_export_missing_not_unexpected(
    tmp_path: Path,
) -> None:
    """The same class of bug as above, on `export`'s own read of the run receipt."""

    surface = _surface(tmp_path)
    surface._write_action(
        "run",
        {"summary": "test run record with no run_root", "run_id": "broken-run-root"},
        descriptor_action="run",
    )

    with pytest.raises(OperatorError) as failure:
        surface.export(run_id="broken-run-root")

    assert failure.value.code is ErrorCode.EXPORT_MISSING
    assert "run_root" in failure.value.render()


def test_a_malformed_older_receipt_does_not_block_export_of_a_sound_later_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ambiguity check (above, EXPORT_AMBIGUOUS) reads every matching
    receipt's run_root to compare them; a record it cannot resolve at all
    must not be able to block export of the one that actually gets selected
    (matching[-1]) just by existing earlier under the same run_id -- that
    would be a new failure mode the ambiguity check introduced, not one it
    was meant to guard against."""
    surface = _surface(tmp_path)
    surface._write_action(
        "run",
        {"summary": "an earlier, incomplete record", "run_id": "dup"},
        descriptor_action="run",
    )
    surface._write_action(
        "run",
        {"summary": "test run", "state": "complete", "run_root": "runs", "run_id": "dup"},
        descriptor_action="run",
    )
    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", _fake_bundle(b"sound"))
    surface._armarium_export = lambda root, run_id: _complete_export(root, run_id)  # type: ignore[method-assign]

    bundle = surface.export(run_id="dup")

    assert bundle.read_bytes() == b"sound"


def test_console_entry_renders_an_application_import_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_application():  # type: ignore[no-untyped-def]
        raise RuntimeError("Traceback (most recent call last): missing fixture application")

    monkeypatch.setattr(entry, "_load_application", broken_application)
    # The boundary records the failure in the default state directory;
    # that must be this test's, not the developer's.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))

    assert entry.main(["status"]) == 2
    receipts = list((tmp_path / "xdg-state" / "verbatus" / "receipts").glob("unexpected-*.json"))
    assert len(receipts) == 1
    saved = json.loads(receipts[0].read_text(encoding="utf-8"))["payload"]
    assert saved["exception_type"] == "RuntimeError"
    assert saved["argv"] == ["status"]
    assert "missing fixture application" in saved["traceback"]
    captured = capsys.readouterr().out
    assert "What happened:" in captured
    assert "What it means:" in captured
    assert "Next step:" in captured
    assert "Traceback" not in captured


def test_interactive_first_upload_asks_where_to_seal_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("upload", "/approved/batch", "", "/approved/submission.json"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "upload",
        "--source",
        "/approved/batch",
        "--manifest-out",
        "/approved/submission.json",
    ]


def test_interactive_upload_keeps_an_existing_sealed_manifest_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("upload", "/approved/batch", "/reviewed/submission.json"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "upload",
        "--source",
        "/approved/batch",
        "--sealed-manifest",
        "/reviewed/submission.json",
    ]


def test_interactive_fetch_run_asks_for_each_optional_evidence_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The double-click route asks by name for all ten records that lie under
    neither fetched prefix -- the bootstrap report, the pod-run report, that
    report's ``-hold``, ``-liveness``, ``-timings`` and ``-transcript.log``
    siblings, the pod-timer runtime report and its ``-terminating`` breadcrumb,
    the bootstrap journal and the volume-root transfer journal -- exactly what
    ``--evidence-key`` is for, each independently optional. A saved launch
    receipt is asked for first and derives the
    token-bound keys itself; this is the route for a run whose receipt is not
    to hand."""

    answers = iter(
        (
            "fetch-run",
            "brought-home",
            "/local/into",
            "EU-CZ-1:vol123",
            "",  # no saved launch receipt to hand: name the keys one by one
            "runs/brought-home/bootstrap-report.json",
            "runs/brought-home/pod-run-report.json",
            "runs/brought-home/pod-run-report-hold.json",
            "runs/brought-home/pod-run-report-liveness.json",
            "runs/brought-home/pod-run-report-timings.json",
            "runs/brought-home/pod-run-report-transcript.log",
            "pod-runtime-report.json",
            "pod-runtime-report-terminating.json",
            "",  # bootstrap journal left blank
            "pod-transfer-journal.json",
            "",  # every launch's preflight tree, not one stem
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "fetch-run",
        "--run-id",
        "brought-home",
        "--into",
        "/local/into",
        "--network-volume",
        "EU-CZ-1:vol123",
        "--evidence-key",
        "runs/brought-home/bootstrap-report.json",
        "--evidence-key",
        "runs/brought-home/pod-run-report.json",
        "--evidence-key",
        "runs/brought-home/pod-run-report-hold.json",
        "--evidence-key",
        "runs/brought-home/pod-run-report-liveness.json",
        "--evidence-key",
        "runs/brought-home/pod-run-report-timings.json",
        "--evidence-key",
        "runs/brought-home/pod-run-report-transcript.log",
        "--evidence-key",
        "pod-runtime-report.json",
        "--evidence-key",
        "pod-runtime-report-terminating.json",
        "--evidence-key",
        "pod-transfer-journal.json",
    ]


def test_interactive_watch_asks_for_the_run_the_receipts_and_an_optional_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("watch", "brought-home", "/local/receipts", "/local/lease.json"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments() == [
        "watch",
        "--run-id",
        "brought-home",
        "--receipts",
        "/local/receipts",
        "--lease",
        "/local/lease.json",
    ]

    answers = iter(("watch", "brought-home", "/local/receipts", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments() == [
        "watch",
        "--run-id",
        "brought-home",
        "--receipts",
        "/local/receipts",
    ]


@pytest.mark.parametrize("blank", [1, 2])
def test_interactive_watch_explains_a_blank_required_answer(
    blank: int, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    given = ["watch", "brought-home", "/local/receipts", ""]
    given[blank] = ""
    answers = iter(given)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments() == []
    assert "Watch needs a run ID and the folder" in capsys.readouterr().out


def test_interactive_fetch_run_needs_no_evidence_key_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank answers mean none, not a refusal: the launch receipt, every evidence
    key and the preflight stem are each optional."""

    answers = iter(
        (
            "fetch-run",
            "brought-home",
            "/local/into",
            "EU-CZ-1:vol123",
            "",  # launch receipt
            *([""] * 10),  # the ten evidence keys
            "",  # preflight stem
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "fetch-run",
        "--run-id",
        "brought-home",
        "--into",
        "/local/into",
        "--network-volume",
        "EU-CZ-1:vol123",
    ]


@pytest.mark.parametrize(
    ("verb", "expected"),
    (("upload", "needs a folder"),),
)
def test_interactive_missing_inputs_name_the_required_facts_plainly(
    verb: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter((verb, "", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == []
    output = capsys.readouterr().out
    assert expected in output
    assert "left blank" in output


def test_interactive_first_upload_refuses_a_blank_manifest_destination(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(("upload", "/approved/batch", "", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == []
    output = capsys.readouterr().out
    assert "first upload needs a destination" in output
    assert "nothing changed" in output


def test_mac_wrapper_starts_the_same_console_flow() -> None:
    wrapper = ROOT / "operations" / "operator" / "Verbatus.command"
    assert ".venv/bin/python" in wrapper.read_text(encoding="utf-8")
    if not (ROOT / ".venv" / "bin" / "python").exists():
        # The wrapper's fallback is whatever python3 is on PATH, which need not
        # carry this project's dependencies; exercising the double-click route
        # is only meaningful against the checkout's own environment.
        pytest.skip("the double-click route needs the checkout's .venv")
    completed = subprocess.run(
        [str(wrapper), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "verbatus" in completed.stdout.lower()


def test_mac_wrapper_refuses_an_empty_project_root(
    tmp_path: Path,
) -> None:
    bash_environment = tmp_path / "bash-environment"
    bash_environment.write_text("pwd() { return 1; }\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["BASH_ENV"] = str(bash_environment)

    completed = subprocess.run(
        [str(ROOT / "operations" / "operator" / "Verbatus.command")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )

    assert completed.returncode == 1
    assert "could not open its project folder" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_reconciliation_distinguishes_missing_lists_from_recorded_empty_lists() -> None:
    missing = reconciliation_table({"aggregate": {}, "expected_acts": "unknown"})
    empty = reconciliation_table(
        {
            "aggregate": {},
            "expected_acts": 0,
            "pages": [],
            "delivered": [],
            "non_delivered": [],
        }
    )

    assert "| Submitted pages accounted for | not recorded |" in missing
    assert "| Delivered acts | not recorded |" in missing
    assert "| Acts held for review | not recorded |" in missing
    assert "| Submitted pages accounted for | 0 |" in empty
    assert "| Delivered acts | 0 |" in empty
    assert "| Acts held for review | 0 |" in empty


def test_reconciliation_counts_each_non_delivered_terminal_category() -> None:
    rows = reconciliation_table(
        {
            "aggregate": {"status": "partial", "reasons": []},
            "expected_acts": 5,
            "pages": [],
            "delivered": [{}],
            "non_delivered": [
                {"category": "held-for-review"},
                {"category": "refused-with-reason"},
                {"category": "confirmed-blank"},
                {"category": "excluded-with-approval"},
            ],
        }
    )

    assert "| Acts held for review | 1 |" in rows
    assert "| Acts refused with reason | 1 |" in rows
    assert "| Confirmed blank acts | 1 |" in rows
    assert "| Acts excluded with approval | 1 |" in rows


def test_status_repeats_the_recorded_values_exactly_and_never_recomputes_them(
    tmp_path: Path,
) -> None:
    """Every figure `status` shows is the exact string the receipt stored.

    Not a paraphrase of the receipt and not a fresh calculation, or the ledger
    the operator reads is a second opinion rather than the record.
    """

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    surface.upload(source, sealed_manifest=manifest)
    surface.run(run_id="byte-for-byte-run", scenario="page-unbroken")
    surface.export(run_id="byte-for-byte-run")

    lines = surface.status()
    joined = "\n".join(lines)

    run = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert f"  Saved run state: {run['state']}." in lines

    export = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    for row in export["reconciliation"]:
        assert f"  {row}" in lines

    for action in ("upload", "run", "export"):
        payload = surface.receipts.read(surface._descriptor_receipt(action))["payload"]
        assert payload["summary"] in joined


def test_upload_receipt_retains_manifest_identity_but_no_local_paths(tmp_path: Path) -> None:
    """A receipt names the sealed record by digest, never by local source path."""

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    surface.upload(source, sealed_manifest=manifest)
    receipt = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]

    lines = surface.status()

    assert (
        receipt["submission_manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert "source" not in receipt
    assert "submission_manifest" not in receipt
    joined = "\n".join(lines)
    assert receipt["submission_manifest_sha256"] in joined
    serialized = json.dumps(receipt)
    for local in (source, manifest):
        # Both spellings: on macOS `tmp_path` is under /var/folders while
        # `resolve()` returns /private/var/folders, so asserting only the
        # resolved form would have passed while the receipt carried the
        # caller's unresolved path -- the exact leak this test names.
        assert str(local) not in serialized
        assert str(local.resolve()) not in serialized


@pytest.mark.parametrize("digest", (None, "not-a-sha256", "A" * 64))
def test_status_refuses_to_present_a_malformed_manifest_digest_as_evidence(
    tmp_path: Path, digest: str | None
) -> None:
    surface = _surface(tmp_path)
    payload = {
        "summary": "synthetic upload record",
        "state": "complete",
        "zero_gpu_hours": True,
    }
    if digest is not None:
        payload["submission_manifest_sha256"] = digest
    surface._write_action(
        "upload",
        payload,
        descriptor_action="upload",
    )

    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE
    assert "does not bind its submission record digest" in str(refusal.value.detail)


@pytest.mark.hostile_local
def test_a_symlink_alias_cannot_hide_that_default_state_is_inside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "checkout"
    state_parent = workspace / "state-parent"
    state_parent.mkdir(parents=True)
    alias = tmp_path / "state-alias"
    alias.symlink_to(state_parent, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(alias))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    state = cli._default_state_dir(workspace)

    assert state == tmp_path / "home" / ".local" / "state" / "verbatus"


def test_state_default_failure_uses_the_operator_error_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(_workspace: Path | None = None) -> Path:
        raise RuntimeError("no safe state root")

    monkeypatch.setattr(cli, "_default_state_dir", unavailable)

    assert cli.main(["status"]) == 2
    output = capsys.readouterr().out
    assert "Verbatus met a problem it could not classify" in output
    assert "no safe state root" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("value", ("", ".", "relative/state"))
def test_a_non_absolute_xdg_state_home_does_not_put_records_back_in_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The Base Directory specification requires ignoring non-absolute values."""

    workspace = tmp_path / "checkout"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("XDG_STATE_HOME", value)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    default = cli._default_state_dir()

    assert default.is_absolute()
    assert workspace not in default.parents
    assert default == tmp_path / "home" / ".local" / "state" / "verbatus"


def test_a_relative_home_cannot_make_the_default_state_root_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("HOME", "relative-home")

    default = cli._default_state_dir()

    assert default.is_absolute()
    assert workspace not in default.parents


def test_an_old_in_checkout_verbatus_directory_is_named_not_silently_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A moved default must not make existing receipts disappear silently."""

    workspace = tmp_path / "checkout"
    old_state = workspace / ".verbatus"
    old_state.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))

    cli.main(["status"])

    out = capsys.readouterr().out
    assert str(old_state) in out
    assert "not read here" in out


def test_an_abbreviated_state_dir_flag_is_honoured_rather_than_parsed_and_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse accepts abbreviations, so argv text cannot decide what was named.

    `--state-di /records` set `args.state_dir`, but the scan looked for the full
    spelling, found nothing, and overwrote the operator's folder with the
    computed default: receipts went to `~/.local/state/verbatus`, the abandoned
    state notice appeared, and `status` afterwards read a different set of
    records from the one they had asked for.
    """

    workspace = tmp_path / "checkout"
    old_state = workspace / ".verbatus"
    old_state.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))

    cli.main(["--state-di", str(old_state), "status"])

    out = capsys.readouterr().out
    assert "not read here" not in out


def test_one_unreadable_status_record_does_not_hide_the_intact_ledgers(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source, manifest = _manifest(tmp_path)
    surface._write_action("export", {"summary": "saved export"}, descriptor_action="export")
    surface.upload(source, sealed_manifest=manifest)
    upload_receipt = surface._descriptor_receipt("upload")
    assert upload_receipt is not None
    upload_receipt.write_text("not a receipt\n", encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE
    assert any("export record 1: saved export" in line for line in messages)
    assert any("upload record 1: UNREADABLE" in line for line in messages)


def test_an_evidence_bundle_short_of_run_json_refuses_rather_than_saying_complete(
    tmp_path: Path,
) -> None:
    """A bundle missing the record of which run produced it is not "complete".

    `_write_base_armarium_bundle` selects `run.json` and `7_armarium` and wrote
    each with `if is_file() ... elif is_dir()`. An absent `run.json` matched
    neither arm and was dropped with no record at all, while the export receipt
    still said `"state": "complete"`. This is exactly that case: a partial
    result is visibly partial, and "complete" is refused unless everything
    reconciles.
    """

    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "run-without-its-authority"
    root = run_root / run_id
    (root / "7_armarium").mkdir(parents=True)
    (root / "7_armarium" / "aggregate.json").write_text("{}", encoding="utf-8")
    assert not (root / "run.json").exists(), "this test's premise is a missing run.json"

    destination = tmp_path / "bundle.zip"
    with pytest.raises(OperatorError) as refusal:
        surface._write_base_armarium_bundle(run_root, run_id, destination)

    assert "run.json" in (refusal.value.detail or "")
    assert not destination.exists(), "a refused bundle must not leave a file behind"


def test_an_evidence_bundle_leaves_out_a_stray_publication_temporary(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    root = tmp_path / "runs" / "r"
    (root / "7_armarium").mkdir(parents=True)
    (root / "run.json").write_text("{}", encoding="utf-8")
    (root / "7_armarium" / "aggregate.json").write_text("{}", encoding="utf-8")
    (root / "7_armarium" / ".aggregate.json.tmp-abc123").write_text("{", encoding="utf-8")

    destination = tmp_path / "bundle.zip"
    surface._write_base_armarium_bundle(tmp_path / "runs", "r", destination)

    with zipfile.ZipFile(destination) as bundle:
        assert not any(".tmp-" in name for name in bundle.namelist())
        assert "r/7_armarium/aggregate.json" in bundle.namelist()


def test_an_evidence_bundle_whose_armarium_is_a_file_refuses_too(tmp_path: Path) -> None:
    """Armarium must be a directory; existence cannot prove the required kind."""

    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "run-with-a-file-where-a-stage-belongs"
    root = run_root / run_id
    root.mkdir(parents=True)
    (root / "run.json").write_text("{}", encoding="utf-8")
    (root / "7_armarium").write_text("not a directory", encoding="utf-8")

    destination = tmp_path / "bundle-wrong-kind.zip"
    with pytest.raises(OperatorError) as refusal:
        surface._write_base_armarium_bundle(run_root, run_id, destination)

    assert "7_armarium is not a directory" in (refusal.value.detail or "")
    assert not destination.exists(), "a refused bundle must not leave a file behind"


def test_an_evidence_bundle_whose_run_authority_is_a_directory_refuses(tmp_path: Path) -> None:
    """The mirror case: `run.json` present as a directory would be rglob-ed in whole."""

    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "run-whose-authority-is-a-directory"
    root = run_root / run_id
    (root / "run.json").mkdir(parents=True)
    (root / "7_armarium").mkdir(parents=True)

    with pytest.raises(OperatorError) as refusal:
        surface._write_base_armarium_bundle(run_root, run_id, tmp_path / "bundle-dir.zip")

    assert "run.json is not a regular file" in (refusal.value.detail or "")


@pytest.mark.parametrize("linked_member", ("run.json", "7_armarium"))
def test_an_evidence_bundle_refuses_a_symlinked_top_level_member(
    tmp_path: Path, linked_member: str
) -> None:
    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "run-with-linked-authority"
    root = run_root / run_id
    root.mkdir(parents=True)
    real_run = tmp_path / "real-run.json"
    real_run.write_text("{}", encoding="utf-8")
    real_armarium = tmp_path / "real-armarium"
    real_armarium.mkdir()
    if linked_member == "run.json":
        (root / "run.json").symlink_to(real_run)
        (root / "7_armarium").mkdir()
    else:
        (root / "run.json").write_text("{}", encoding="utf-8")
        (root / "7_armarium").symlink_to(real_armarium, target_is_directory=True)

    destination = tmp_path / "bundle-linked.zip"
    with pytest.raises(OperatorError) as refusal:
        surface._write_base_armarium_bundle(run_root, run_id, destination)

    assert "symbolic link" in (refusal.value.detail or "")
    assert not destination.exists()


def test_an_evidence_bundle_refuses_a_symlinked_armarium_member(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "run-with-linked-armarium-member"
    root = run_root / run_id
    armarium = root / "7_armarium"
    armarium.mkdir(parents=True)
    (root / "run.json").write_text("{}", encoding="utf-8")
    referent = tmp_path / "real-export.json"
    referent.write_text('{"state": "complete"}', encoding="utf-8")
    (armarium / "export.json").symlink_to(referent)
    destination = tmp_path / "bundle-linked-member.zip"

    with pytest.raises(OperatorError) as refusal:
        surface._write_base_armarium_bundle(run_root, run_id, destination)

    assert refusal.value.code is ErrorCode.EXPORT_FAILED
    assert "7_armarium/export.json is not a regular file" in (refusal.value.detail or "")
    assert not destination.exists(), "a refused bundle must not leave a file behind"
    assert not destination.with_name(f".{destination.name}.tmp").exists()


def test_evidence_bundle_does_not_follow_its_old_predictable_temporary_name(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "temporary-link-race"
    root = run_root / run_id
    armarium = root / "7_armarium"
    armarium.mkdir(parents=True)
    (root / "run.json").write_text("{}", encoding="utf-8")
    (armarium / "export.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "bundle.zip"
    victim = tmp_path / "must-not-be-truncated"
    victim.write_bytes(b"outside bytes")
    predictable = destination.with_name(f".{destination.name}.tmp")
    predictable.symlink_to(victim)

    surface._write_base_armarium_bundle(run_root, run_id, destination)

    assert destination.is_file()
    assert victim.read_bytes() == b"outside bytes"
    assert predictable.is_symlink()


def test_evidence_bundle_refuses_case_variant_archive_members() -> None:
    """Byte-distinct members that extract to one default-APFS name are refused.

    The descriptor-based walk cannot plant this condition through the local
    case-insensitive filesystem (two case-variant writes land in one file), so
    the guard is proven at its own seam, where a case-sensitive source tree
    would deliver both names.
    """
    from operations.operator import surface as surface_module

    archive_names: dict[str, str] = {}
    surface_module._register_archive_name("r/7_armarium/Result.json", archive_names)

    with pytest.raises(OperatorError) as refusal:
        surface_module._register_archive_name("r/7_armarium/result.json", archive_names)

    assert refusal.value.code is ErrorCode.EXPORT_FAILED
    assert "collide on the default macOS filesystem" in (refusal.value.detail or "")


def test_evidence_bundle_refuses_a_member_replaced_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)
    run_root = tmp_path / "runs"
    run_id = "member-open-race"
    root = run_root / run_id
    armarium = root / "7_armarium"
    armarium.mkdir(parents=True)
    (root / "run.json").write_text("{}", encoding="utf-8")
    member = armarium / "export.json"
    member.write_text('{"first": true}', encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"replacement": true}', encoding="utf-8")
    destination = tmp_path / "raced-bundle.zip"
    real_open = os.open
    swapped = False

    def replace_before_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if path == "export.json" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            member.unlink()
            replacement.replace(member)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)

    with pytest.raises(OperatorError) as refusal:
        surface._write_base_armarium_bundle(run_root, run_id, destination)

    assert "changed between check and open" in (refusal.value.detail or "")
    assert not destination.exists()


def test_repository_commit_lookup_is_bounded_and_names_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def times_out(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args
        observed.update(kwargs)
        raise subprocess.TimeoutExpired("git", kwargs["timeout"])

    monkeypatch.setattr("operations.operator.surface.subprocess.run", times_out)

    commit, reason = _repository_commit_or_reason(tmp_path)

    assert observed["timeout"] == 30
    assert commit is None
    assert "timed out" in (reason or "")


# --- the run verb carries the real-roster pair, together or not at all --------


def test_run_forwards_the_roster_pair_to_the_door_and_the_orchestrator(tmp_path: Path) -> None:
    surface, observed = _recording_surface(tmp_path, faults=Faults(laptop_crash=True))
    roster = tmp_path / "config" / "models-real.toml"
    catalogue = tmp_path / "config" / "serving_recipes_real.toml"

    with pytest.raises(OperatorError) as interrupted:
        surface.run(
            run_id="real-roster-run",
            models_config=roster,
            serving_recipes_config=catalogue,
        )

    assert interrupted.value.code is ErrorCode.RUN_INTERRUPTED
    [(command, _cwd)] = observed
    assert _argv_value(command, "--models-config") == str(roster.absolute())
    assert _argv_value(command, "--serving-recipes-config") == str(catalogue.absolute())


def test_run_without_a_roster_names_neither_flag(tmp_path: Path) -> None:
    surface, observed = _recording_surface(tmp_path, faults=Faults(laptop_crash=True))

    with pytest.raises(OperatorError):
        surface.run(run_id="fixture-roster-run")

    [(command, _cwd)] = observed
    assert "--models-config" not in command and "--serving-recipes-config" not in command


@pytest.mark.parametrize("supplied", ["models_config", "serving_recipes_config"])
def test_run_refuses_part_of_a_roster_before_any_child_starts(
    tmp_path: Path, supplied: str
) -> None:
    """One half of the pair without the other would seal the real chairs against
    the fixture catalogue, or the fixture chairs against the real one."""

    surface, observed = _recording_surface(tmp_path, faults=Faults(laptop_crash=True))

    with pytest.raises(OperatorError) as refusal:
        surface.run(run_id="part-roster", **{supplied: tmp_path / "part.toml"})

    assert refusal.value.code is ErrorCode.INVALID_COMMAND
    assert "supply both or neither" in str(refusal.value.detail)
    assert not observed


def test_cli_run_carries_the_roster_pair_to_the_operator_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class ObservedSurface:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            observed.update(kwargs)

    monkeypatch.setattr(cli, "OperatorSurface", ObservedSurface)
    roster = tmp_path / "models-real.toml"
    catalogue = tmp_path / "serving_recipes_real.toml"
    # `run` is refused before dispatch on a workspace that is not a checkout;
    # this test is about the arguments reaching the surface.
    for resource in ("pipeline", "config", "proof"):
        (tmp_path / resource).mkdir()

    assert (
        cli.main(
            [
                "--workspace",
                str(tmp_path),
                "run",
                "--run-id",
                "real-run",
                "--models-config",
                str(roster),
                "--serving-recipes-config",
                str(catalogue),
            ]
        )
        == 0
    )
    assert observed["models_config"] == roster
    assert observed["serving_recipes_config"] == catalogue


# --- fetch-run: the tree comes home digest-checked, never overwriting ---------


class DirectoryRunReader:
    """A directory standing in for the volume's S3 view, as the launch drill does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.overrides: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        keys = [
            path.relative_to(self.root).as_posix()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        ]
        return tuple(key for key in keys if key.startswith(prefix))

    def fetch_to(self, key: str, destination: Path, *, max_bytes: int) -> int:
        self.fetched.append(key)
        payload = self.overrides.get(key, (self.root / key).read_bytes())
        if len(payload) > max_bytes:
            # The real reader's own answer (`volume_s3.fetch_to`): a refusal
            # naming the key and the bound, not a bare AssertionError that says
            # nothing to whoever catches it. The bound is reachable on one class
            # of object -- an engine log, which nothing truncates -- so what
            # this raises decides what the caller can do about it.
            raise VolumeTransferRefusal(
                f"the network volume's object {key!r} is larger than the {max_bytes}-byte "
                "bound this fetch will write"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return len(payload)


def _volume_run(tmp_path: Path, run_id: str = "brought-home") -> tuple[Path, DirectoryRunReader]:
    """A real, sealed run tree under `runs/<id>` on a directory standing in for the volume."""

    from common.chairs.models import ChairIdentity, ServingDetails
    from common.chairs.receipts import build_receipt
    from common.contracts.envelope import build_envelope
    from common.contracts.identities import artifact_id as make_artifact_id
    from common.contracts.stages import DESIGNATOR
    from common.runtree.store import RunTree

    volume = tmp_path / "volume"
    page = b"synthetic page one"
    tree = RunTree.create(
        volume / "runs",
        run_id,
        source_manifest=[
            {"relative_path": "proof/page-1.png", "sha256": _sha256(page), "ordinal": 1}
        ],
        config_digest="c" * 64,
        adapter_recipes={"designator": "fake-designator-v0"},
        witness_chairs=["attestator_1", "attestator_2", "attestator_3"],
    )
    tree.put_blob(DESIGNATOR, page)
    tree.publish_artifact(
        build_envelope(
            run_id=run_id,
            artifact_id=make_artifact_id(DESIGNATOR, "proposal", "pg_0123456789abcdef"),
            subject_id="pg_0123456789abcdef",
            stage=DESIGNATOR,
            kind="proposal",
            outcome="proposed",
            config_digest="c" * 64,
            adapter_revision="fake-designator-v0",
            inputs=[],
            payload={"proposals": 2},
        )
    )
    tree.write_manifest(DESIGNATOR)
    # A real serving receipt, so a fetch's never-entered digest arm for
    # `receipts/sha256/` is exercised the same as the blob and artifact arms.
    identity = ChairIdentity(
        role="attestator_1",
        source="local-repository",
        repo=None,
        path="fixture/attestator_1",
        revision=None,
        digest_manifest="a" * 64,
        manifest="manifests/attestator_1.json",
        adapter_of=None,
        serving_recipe="fake-attestator-v0",
        license_note="fixture only",
    )
    details = ServingDetails(
        tokenizer_revision="a" * 64,
        seed=0,
        context_cap=4096,
        pixel_cap=1_000_000,
        engine="fixture-engine",
        engine_version="v0",
        dtype="float32",
        adapter_identity=None,
        endpoint="http://fixture.invalid/seat",
        started_at="2026-08-03T00:00:00Z",
    )
    tree.write_run_receipt(build_receipt(identity, details))
    # Publication residue a crashed pod leaves beside the manifest: skipped by
    # name, never fetched as evidence.
    (tree.root / "2_designator" / ".manifest.json.tmp-residue").write_bytes(b"half")
    return volume, DirectoryRunReader(volume)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    }


def test_fetch_run_brings_the_whole_tree_home_verified_and_reuses_it_next_time(
    tmp_path: Path,
) -> None:
    volume, reader = _volume_run(tmp_path)
    messages: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=messages)
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert _files_under(into / "brought-home") == _files_under(volume / "runs" / "brought-home")
    assert not (into / "brought-home" / "2_designator" / ".manifest.json.tmp-residue").exists()
    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified"
    assert payload["fetched"] == len(_files_under(volume / "runs" / "brought-home"))
    assert payload["reused"] == 0
    assert payload["stages_verified"] == ["designator"]
    # Every artifact here is recorded by a stored manifest, so none of them was
    # verified "by their own envelope" -- naming them all was a false statement
    # about what was measured and made the field useless for
    # telling a crashed stage's artifacts from the rest.
    assert payload["envelope_only_artifacts"] == []
    assert payload["excluded_publication_temporaries"] == [
        "2_designator/.manifest.json.tmp-residue"
    ]
    assert payload["zero_gpu_hours"] is True
    assert any("every one checked against the run tree's own digests" in line for line in messages)
    # The first thing fetched was the authority, then the inventories, then
    # what they account for -- so a bad object stops the fetch at itself.
    assert reader.fetched[0] == "runs/brought-home/run.json"
    assert reader.fetched[1].endswith("/manifest.json")

    again = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    repeated = surface.receipts.read(again)["payload"]
    assert repeated["fetched"] == 0
    assert repeated["reused"] == payload["fetched"]
    assert "canary_verdict" not in repeated
    assert not (surface.workspace / "private" / "canary").exists()


def test_fetch_run_sets_aside_a_syncs_own_files_rather_than_refusing_the_tree(
    tmp_path: Path,
) -> None:
    """A pod's run-tree sync keeps a ledger and leaves a copy in flight behind when
    killed; neither is evidence, so both are set aside like publication residue and the
    rest of the tree still comes home verified."""
    from common.runtree.sync import SYNC_PREFIX

    volume, reader = _volume_run(tmp_path)
    tree = volume / "runs" / "brought-home"
    (tree / f"{SYNC_PREFIX}ledger.jsonl").write_bytes(b'{"target": "/local/run"}\n')
    (tree / "2_designator" / "artifacts" / f"{SYNC_PREFIX}abc123").write_bytes(b"half a copy")
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified"
    assert payload["excluded_publication_temporaries"] == [
        f"{SYNC_PREFIX}ledger.jsonl",
        "2_designator/.manifest.json.tmp-residue",
        f"2_designator/artifacts/{SYNC_PREFIX}abc123",
    ]
    assert not list((into / "brought-home").rglob(f"{SYNC_PREFIX}*"))
    assert _files_under(into / "brought-home") == _files_under(tree)


def test_fetch_run_seals_one_private_alarm_and_sends_one_decision_ping(tmp_path, monkeypatch):
    from common import stage
    from operations.corpus import canary

    volume, reader = _volume_run(tmp_path)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    pings = []
    surface.notifier = lambda event, message: (
        pings.append((event, message)) or notify_bridge.NotifyOutcome(False, True, "sent")
    )
    monkeypatch.setattr(stage, "canary_ordinals", lambda _run: {1})
    verdict = {
        "schema": "canary-verdict.v1",
        "run_id": "brought-home",
        "stages": {"attestator_2": False},
        "dead": [
            {
                "stage": "attestator_2",
                "rule": "DAI failed on a page it was trained on",
            }
        ],
    }
    monkeypatch.setattr(canary, "check_run", lambda _tree, _root: verdict)
    into = tmp_path / "local-runs"
    private = tmp_path / "private-canary"
    for _ in range(2):
        with pytest.raises(OperatorError) as error:
            surface.fetch_run(run_id="brought-home", into=into, reader=reader, canary_root=private)
        assert error.value.code == ErrorCode.CANARY_ALARM
    assert len(pings) == 1
    assert pings[0][0] == "milestone"
    assert (private / "verdicts" / "brought-home.json").read_bytes() == canonical_bytes(verdict)
    monkeypatch.setattr(canary, "check_run", lambda _tree, _root: {**verdict, "dead": []})
    with pytest.raises(OperatorError) as conflict:
        surface.fetch_run(run_id="brought-home", into=into, reader=reader, canary_root=private)
    assert conflict.value.code == ErrorCode.CANARY_VERDICT_CONFLICT
    assert "conflicts" in str(conflict.value)
    assert len(pings) == 1
    assert any(
        surface.receipts.read(path)["payload"]["state"] == "canary-verdict-conflict"
        for path in surface.receipts.receipts.glob("*.json")
    )


def test_fetch_run_with_a_healthy_canary_is_silent(tmp_path, monkeypatch):
    from common import stage
    from operations.corpus import canary

    _volume, reader = _volume_run(tmp_path)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    pings = []
    surface.notifier = lambda event, message: (
        pings.append((event, message)) or notify_bridge.NotifyOutcome(False, True, "sent")
    )
    monkeypatch.setattr(stage, "canary_ordinals", lambda _run: {1})
    monkeypatch.setattr(
        canary,
        "check_run",
        lambda _tree, _root: {
            "schema": "canary-verdict.v1",
            "run_id": "brought-home",
            "stages": {"door": True},
            "dead": [],
        },
    )
    private = tmp_path / "private-canary"
    receipt = surface.fetch_run(
        run_id="brought-home", into=tmp_path / "local-runs", reader=reader, canary_root=private
    )
    assert surface.receipts.read(receipt)["payload"]["state"] == "verified"
    assert (private / "verdicts" / "brought-home.json").exists()
    assert pings == []


def test_configured_canary_root_without_a_sealed_ledger_alarms_once(tmp_path):
    _volume, reader = _volume_run(tmp_path)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    pings = []
    surface.notifier = lambda event, message: (
        pings.append((event, message)) or notify_bridge.NotifyOutcome(False, True, "sent")
    )
    private = tmp_path / "private-canary"
    for _ in range(2):
        with pytest.raises(OperatorError) as error:
            surface.fetch_run(
                run_id="brought-home",
                into=tmp_path / "local-runs",
                reader=reader,
                canary_root=private,
            )
        assert error.value.code == ErrorCode.CANARY_ALARM
    verdict = json.loads((private / "verdicts" / "brought-home.json").read_text())
    assert any(row["rule"] == "no-sealed-canary-ledger" for row in verdict["dead"])
    assert len(pings) == 1


def test_partial_fetch_keeps_its_partial_state_when_a_canary_dies(tmp_path):
    volume, reader = _volume_run(tmp_path)
    (volume / "runs" / "brought-home" / "2_designator" / "manifest.json").unlink()
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    surface.notifier = lambda _event, _message: notify_bridge.NotifyOutcome(False, True, "sent")
    with pytest.raises(OperatorError) as error:
        surface.fetch_run(
            run_id="brought-home",
            into=tmp_path / "local-runs",
            reader=reader,
            canary_root=tmp_path / "private-canary",
        )
    assert error.value.code == ErrorCode.CANARY_ALARM
    receipts = list(surface.receipts.receipts.glob("*.json"))
    payloads = [surface.receipts.read(path)["payload"] for path in receipts]
    partial = next(row for row in payloads if row.get("state") == "verified-partial")
    assert partial["canary_alarm"] is True
    assert partial["unmanifested_stages"] == ["designator"]


def test_fetch_run_seals_and_pings_when_the_canary_check_raises(tmp_path, monkeypatch):
    from common import stage
    from operations.corpus import canary

    _volume, reader = _volume_run(tmp_path)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    pings = []
    surface.notifier = lambda event, message: (
        pings.append((event, message)) or notify_bridge.NotifyOutcome(False, True, "sent")
    )
    monkeypatch.setattr(
        stage, "canary_ordinals", lambda _run: (_ for _ in ()).throw(KeyError("bad"))
    )
    monkeypatch.setattr(
        canary, "canary_ordinals", lambda _run: (_ for _ in ()).throw(KeyError("bad"))
    )
    private = tmp_path / "private-canary"
    with pytest.raises(OperatorError) as error:
        surface.fetch_run(
            run_id="brought-home",
            into=tmp_path / "local-runs",
            reader=reader,
            canary_root=private,
        )
    assert error.value.code == ErrorCode.CANARY_ALARM
    verdict = json.loads((private / "verdicts" / "brought-home.json").read_text())
    assert {row["rule"] for row in verdict["dead"]} == {"check-raised:KeyError"}
    assert len(pings) == 1


def test_verdict_save_failure_says_the_verdict_was_not_saved(tmp_path, monkeypatch):
    from operations.corpus import cache

    _volume, reader = _volume_run(tmp_path)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    monkeypatch.setattr(cache, "write_new_file", lambda *_args: False)
    with pytest.raises(OperatorError) as error:
        surface.fetch_run(
            run_id="brought-home",
            into=tmp_path / "local-runs",
            reader=reader,
            canary_root=tmp_path / "private-canary",
        )
    assert error.value.code == ErrorCode.CANARY_VERDICT_SAVE_FAILED
    assert "verdict could not be saved" in str(error.value)
    receipts = list(surface.receipts.receipts.glob("*.json"))
    assert len(receipts) == 1
    assert surface.receipts.read(receipts[0])["payload"]["state"] == "canary-verdict-unsaved"


# The three stages that serve a chair, and the module each one's
# chair client is built in. Read from source below rather than
# imported, since a stage module pulls the whole serving stack in behind it.
_SERVING_STAGE_SOURCES = {
    "attestatores": "pipeline/3_attestatores/run.py",
    "perlector": "pipeline/4_perlector/run.py",
}


def _served_stage_log_keys(run_id: str) -> dict[str, str]:
    """`<stage> -> volume key` for the engine log each serving stage really writes.

    Derived from `RunTree.serving_log_path`, which is the expression every
    stage builds through `stage_chair_client` for `ServingManager(log_root=...)`
    -- pinned by `test_no_serving_stage_spells_its_own_log_directory` below.
    Restating the directory here instead is what let the fixture pass while the
    Attestatores wrote to `attestatores/serving-logs/`, a path the stage name
    rather than the writing directory named and no inventory scope accounted
    for: the test held what the fixture author believed, not what the stage
    leaves. Only the directory is load-bearing; the file name is
    `ServingManager._next_log_path`'s shape, and the fetch classifies on the
    prefix.
    """

    from common.runtree.store import RunTree

    tree = RunTree(Path("/nonexistent"), run_id)
    names = {
        "designator": "vllm-designator-0123456789ab.log",
        "attestatores": "vllm-attestator_1-abcdef012345.log",
        "perlector": "vllm-perlector-fedcba987654.log",
    }
    return {
        stage: f"runs/{run_id}/{tree.serving_log_path(stage)}/{names[stage]}"
        for stage in _SERVING_STAGE_SOURCES
    }


def test_every_serving_stage_writes_its_engine_log_inside_the_inventory_scope() -> None:
    """The per-stage binding the fixture below cannot make on its own.

    `_fetch_run_tree` reads `RunTree.inventory_scope()` as the whole of what a
    run tree may hold and refuses the tree at the first key outside it. So a
    stage whose `log_root` lands outside the scope does not lose a log -- it
    loses the run, and the receipt names the log while nothing comes home. That
    is what the Attestatores did, and no fixture written by hand could catch it,
    because a hand-written fixture states the path the author believed.
    """

    from common.runtree.store import RunTree

    tree = RunTree(Path("/nonexistent"), "brought-home")
    scope = tree.inventory_scope()
    for stage in _SERVING_STAGE_SOURCES:
        relative = tree.serving_log_path(stage)
        log = f"{relative}/vllm-{stage}-0123456789ab.log"
        assert any(log.startswith(item) for item in scope), (
            f"{stage} writes its engine log to {relative}/, which no inventory-scope "
            "prefix accounts for; fetch-run would refuse the whole served run tree"
        )
        assert surface_module._is_serving_log(log), (
            f"{stage}'s engine log is in scope but is not classified as a serving log, "
            "so the fetch would try to verify it against a manifest nothing wrote"
        )


def test_no_serving_stage_spells_its_own_log_directory() -> None:
    """A stage-local log path can leave serving logs outside the run inventory."""

    assert re.search(
        r"log_root=context\.tree\.resolve\(\s*context\.tree\.serving_log_path\(context\.stage\)",
        (ROOT / "operations/serving/assembly.py").read_text(encoding="utf-8"),
    ), "stage_chair_client does not build its serving log root from RunTree.serving_log_path"
    for relative in sorted(_SERVING_STAGE_SOURCES.values()):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "stage_chair_client" in source, f"{relative} builds its own chair client"
        assert "ServingManager(" not in source and "ChairClient(" not in source, (
            f"{relative} builds its own manager or client instead of stage_chair_client"
        )
        assert '"serving-logs"' not in source and "'serving-logs'" not in source, (
            f"{relative} spells the serving-log directory for itself; the store owns it"
        )


def _served_stage_leavings(volume: Path, run_id: str = "brought-home") -> dict[str, bytes]:
    """Exactly what a stage that served a chair leaves in the run tree, and nothing else.

    `SubprocessLauncher.launch` writes one engine log per started chair under
    the stage's own `serving_log_path`. That is the whole of it now: the
    single-resident lease used to sit at the tree root as `pod-gpu.lock` as
    well, and it moved to `operations.serving.residency.POD_RESIDENCY_LOCK_PATH`
    on container-local disk, because the boundary is the pod's card and not one
    run tree.
    """

    written = {
        key: f"INFO 09-14 00:00:00 api_server.py:1 vLLM API server 0.30.0 ({stage})\n".encode()
        for stage, key in _served_stage_log_keys(run_id).items()
    }
    for key, payload in written.items():
        target = volume / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return written


def test_fetch_run_brings_a_served_run_tree_home_and_names_its_logs_unverified(
    tmp_path: Path,
) -> None:
    """The run the first live test exists to produce, fetched by the verb written for it.

    A served stage leaves an engine log inside the run tree. While
    `RunTree.inventory_scope()` did not name `<stage>/serving-logs/`, this verb
    refused the whole tree at the first log it listed -- zero objects fetched
    from a run that had already billed a card, with the receipt naming the log.
    The log is still not evidence the tree can check: no manifest records it and
    nothing digested it, so it comes home named as unverified side evidence
    rather than counted among what was verified.
    """

    volume, reader = _volume_run(tmp_path)
    logs = _served_stage_leavings(volume)
    messages: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=messages)
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert _files_under(into / "brought-home") == _files_under(volume / "runs" / "brought-home")
    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified"
    named = payload["unverified_serving_logs"]
    # Compared against what the stages' own `serving_log_path` produced, not
    # against two paths restated here: a restatement is what let this test pass
    # while the Attestatores wrote outside the inventory scope.
    prefix = "runs/brought-home/"
    assert [entry["relative_path"] for entry in named] == sorted(key[len(prefix) :] for key in logs)
    assert [entry["sha256"] for entry in named] == [_sha256(logs[key]) for key in sorted(logs)]
    # Named, not folded into the verified count: the summary and the screen both
    # say so, so no reader takes a digested-but-unchecked log for a checked one.
    assert "digested but unverified" in payload["summary"]
    assert any("came home as side evidence" in line for line in messages)
    # And the tree itself is still whole: the log is not an artifact, a blob or
    # a receipt, so nothing it did changed what the stages reconcile to.
    assert payload["stages_verified"] == ["designator"]
    assert payload["envelope_only_artifacts"] == []


def test_a_serving_log_that_grew_since_the_last_fetch_refuses_by_itself(
    tmp_path: Path,
) -> None:
    """A held run fetched twice still comes home the second time.

    An engine log is appended to while a chair serves, so the operator who
    fetches a held run mid-flight and again at the end meets a log whose bytes
    have grown. `_fetch_or_compare` never replaces a local file, which is right
    for immutable evidence and fatal to the whole fetch if a log is held to it:
    the second call brought home nothing at all, with no remedy named anywhere.
    The log is refused by itself instead, named in the receipt and on the
    screen, and the verified run tree still arrives.
    """

    volume, reader = _volume_run(tmp_path)
    logs = _served_stage_leavings(volume)
    messages: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=messages)
    into = tmp_path / "local-runs"

    surface.fetch_run(run_id="brought-home", into=into, reader=reader)
    grew = sorted(logs)[0]
    (volume / grew).write_bytes(logs[grew] + b"INFO 09-14 00:30:00 shutting down\n")

    messages.clear()
    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified"
    relative = grew[len("runs/brought-home/") :]
    assert [item.split(":", 1)[0] for item in payload["refused_serving_logs"]] == [relative]
    # The one that did not grow still came home, and the tree did.
    assert [entry["relative_path"] for entry in payload["unverified_serving_logs"]] == [
        key[len("runs/brought-home/") :] for key in sorted(logs) if key != grew
    ]
    assert payload["stages_verified"] == ["designator"]
    assert any("A serving log did not come home" in line for line in messages)
    # And the local copy is the one the first fetch verified, untouched.
    assert (into / "brought-home" / relative).read_bytes() == logs[grew]


def test_a_serving_log_past_the_object_bound_refuses_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A debug-level log outgrowing `MAX_FETCH_OBJECT_BYTES` costs the log, not the run.

    Nothing truncates an engine log, so this bound is reachable on exactly one
    class of object in the tree -- and every other class is content-addressed or
    manifest-recorded evidence a page blob cannot approach. Raising it here
    would be the same whole-tree refusal by a different route.
    """

    volume, reader = _volume_run(tmp_path)
    logs = _served_stage_leavings(volume)
    huge = sorted(logs)[1]
    (volume / huge).write_bytes(b"D" * 4096)
    monkeypatch.setattr(surface_module, "MAX_FETCH_OBJECT_BYTES", 1024)

    messages: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=messages)
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified"
    relative = huge[len("runs/brought-home/") :]
    assert [item.split(":", 1)[0] for item in payload["refused_serving_logs"]] == [relative]
    assert "bound this fetch will write" in payload["refused_serving_logs"][0]
    assert payload["stages_verified"] == ["designator"]
    # Nothing half-written was left standing where the log belongs.
    assert not (into / "brought-home" / relative).exists()
    assert any("A serving log did not come home" in line for line in messages)


@pytest.mark.hostile_local
def test_a_symlink_where_a_serving_log_belongs_is_refused_and_left_alone(
    tmp_path: Path,
) -> None:
    """Narrowing the blast radius does not narrow the check, or license a deletion.

    `_fetch_or_compare` refuses a symlink before a byte is written -- a run tree
    holds no aliases -- and that still happens for a log. What changed is that
    the refusal is recorded against the log instead of taking the verified tree
    with it. The link itself is something this call did not create, so the
    cleanup after a refused log must leave it exactly where it was.
    """

    volume, reader = _volume_run(tmp_path)
    logs = _served_stage_leavings(volume)
    into = tmp_path / "local-runs"
    aliased = sorted(logs)[0][len("runs/brought-home/") :]
    planted = into / "brought-home" / aliased
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(tmp_path / "somewhere-else.log")

    messages: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=messages)
    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified"
    assert [item.split(":", 1)[0] for item in payload["refused_serving_logs"]] == [aliased]
    assert "symbolic link" in payload["refused_serving_logs"][0]
    assert planted.is_symlink()
    assert not planted.exists()  # still dangling: nothing was written through it
    assert payload["stages_verified"] == ["designator"]


def test_a_refused_serving_log_is_not_counted_among_what_was_verified(
    tmp_path: Path,
) -> None:
    """The counts stay honest across both serving-log states.

    `fetched` and `reused` count arrivals; `verified_objects` counts what was
    checked against a digest the run tree recorded. A log that arrived was not,
    and a log that never arrived is in neither.
    """

    volume, reader = _volume_run(tmp_path)
    logs = _served_stage_leavings(volume)
    messages: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=messages)
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["fetched"] == len(_files_under(volume / "runs" / "brought-home"))
    assert payload["reused"] == 0
    assert payload["verified_objects"] == payload["fetched"] - len(logs)
    assert payload["refused_serving_logs"] == []
    # The screen never says "every one checked" over a count holding the logs.
    joined = "\n".join(messages)
    assert "every one checked" not in joined
    assert f"{payload['verified_objects']} of them checked against the run tree" in joined


def test_fetch_run_still_refuses_an_unaccounted_object_beside_the_serving_logs(
    tmp_path: Path,
) -> None:
    """Naming one prefix is not opening the tree: everything else still refuses."""

    volume, reader = _volume_run(tmp_path)
    _served_stage_leavings(volume)
    (volume / "runs" / "brought-home" / "pod-gpu.lock").write_bytes(b"")

    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local"

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "pod-gpu.lock" in str(refusal.value.detail)
    assert "no stage of a run tree accounts for" in str(refusal.value.detail)
    assert not (into / "brought-home").exists()


def test_a_serving_log_directory_marker_is_not_classified_as_a_log(tmp_path: Path) -> None:
    """A key ending in `/` is not a log, so it is never fetched as one.

    An S3 listing can carry a zero-byte directory marker, and this reader does
    not filter one out. Classifying it as a serving log would fetch it onto the
    directory's own path and count it among the objects that came home;
    requiring a final name leaves it to the arms that refuse loudly instead.
    """

    assert surface_module._is_serving_log(
        "3_attestatores/serving-logs/vllm-attestator_1-abcdef012345.log"
    )
    assert surface_module._is_serving_log("3_attestatores/serving-logs/nested/engine.log")
    assert not surface_module._is_serving_log("3_attestatores/serving-logs/")
    assert not surface_module._is_serving_log("3_attestatores/serving-logs")
    assert not surface_module._is_serving_log("serving-logs/engine.log")
    # The artifact arm keeps its own keys: the two prefixes never overlap.
    assert not surface_module._is_serving_log("3_attestatores/artifacts/testimonium/x.json")


def test_fetch_run_refuses_a_directory_marker_key_rather_than_writing_it(tmp_path: Path) -> None:
    """And end to end: the marker takes the fetch down loudly, writing nothing.

    Which arm refuses it is not the claim -- the claim is that no run tree comes
    home with a file standing where a directory should be, and that the operator
    is told.
    """

    volume, reader = _volume_run(tmp_path)
    _served_stage_leavings(volume)
    marker = "runs/brought-home/3_attestatores/serving-logs/"
    listed = reader.list_keys

    def list_with_marker(prefix: str) -> tuple[str, ...]:
        keys = listed(prefix)
        return tuple(sorted({*keys, marker})) if marker.startswith(prefix) else keys

    reader.list_keys = list_with_marker  # type: ignore[method-assign]
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local"

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert not (into / "brought-home" / "3_attestatores" / "serving-logs").is_file()


def _volume_evidence(volume: Path, stem: str = "boot-a-report") -> dict[str, bytes]:
    """A launch's PREFLIGHT tree beside the run tree, as `bootstrap_main` writes it.

    A golden page, a serving log, and one content-addressed receipt -- the
    three shapes this pass has to handle: opaque bytes, plain text, and an
    object whose name is its own digest.
    """

    page = b"\x89PNG\r\n\x1a\nsynthetic golden page"
    log = b"vllm: served attestator_1\n"
    receipt = b'{"schema":"serving-receipt.v1"}'
    written = {
        f"preflight/{stem}/golden-page/witness-abc.png": page,
        f"preflight/{stem}/logs/attestator_1.log": log,
        f"preflight/{stem}/receipts/sha256/{_sha256(receipt)}.json": receipt,
    }
    for key, payload in written.items():
        target = volume / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return written


def test_fetch_run_brings_the_launch_evidence_home_and_names_what_it_did_not(
    tmp_path: Path,
) -> None:
    """The run tree is not the whole record of a run that billed a card."""

    volume, reader = _volume_run(tmp_path)
    written = _volume_evidence(volume)
    printed: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=printed)
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert any("none came home" in line for line in printed)
    evidence_root = into / "evidence"
    for key, payload in written.items():
        assert (evidence_root / key).read_bytes() == payload
    payload = surface.receipts.read(receipt)["payload"]["evidence"]
    assert payload["fetched"] == len(written)
    assert payload["reused"] == 0
    assert payload["refusals"] == []
    assert {entry["key"] for entry in payload["objects"]} == set(written)
    assert all(entry["sha256"] == _sha256(written[entry["key"]]) for entry in payload["objects"])
    assert "bootstrap journal" in payload["records_only_by_name"]
    assert "--evidence-key" in payload["records_only_by_name"]
    # No key was named, so here the field may say outright that none came home.
    assert "No key was named this call" in payload["records_only_by_name"]

    again = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    repeated = surface.receipts.read(again)["payload"]["evidence"]
    assert repeated["fetched"] == 0
    assert repeated["reused"] == len(written)


def test_fetch_run_takes_a_named_evidence_key_and_records_one_it_cannot_read(
    tmp_path: Path,
) -> None:
    """The launch-bound reports are named by the operator who knows their keys.

    A key that is not there is recorded as a refusal and never brings the
    verified run tree down with it -- losing a proven fetch because a log file
    could not be read would be the wrong trade.

    And the receipt may not contradict itself while it does this: a payload that
    records the pod-run report fetched with its digest cannot also assert that
    the pod-run report was not fetched. The field states the derivation limit
    and how many keys this call named; what arrived is read off ``objects`` and
    ``refusals``.
    """

    volume, reader = _volume_run(tmp_path)
    report = b'{"schema":"pod-run-report.v1"}'
    (volume / "pod-run-report-launch7.json").write_bytes(report)
    printed: list[str] = []
    surface = _surface(tmp_path, workspace=tmp_path / "workspace", output=printed)
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(
        run_id="brought-home",
        into=into,
        reader=reader,
        evidence_keys=("pod-run-report-launch7.json", "pod-run-report-absent.json"),
    )

    outcome = surface.receipts.read(receipt)["payload"]
    assert outcome["state"] == "verified"
    evidence = outcome["evidence"]
    assert (into / "evidence" / "pod-run-report-launch7.json").read_bytes() == report
    assert [entry["key"] for entry in evidence["objects"]] == ["pod-run-report-launch7.json"]
    assert any("pod-run-report-absent.json" in reason for reason in evidence["refusals"])
    limit = evidence["records_only_by_name"]
    assert "2 key(s) were named this call" in limit
    assert "No key was named this call" not in limit
    assert "not fetched" not in limit
    # The console must not contradict the receipt either.
    assert any("2 key(s) were named this call" in line for line in printed)
    assert not any("none came home" in line for line in printed)


def test_fetch_run_records_a_content_addressed_evidence_object_that_forged_its_name(
    tmp_path: Path,
) -> None:
    """An evidence receipt must still hash to its own name."""

    volume, reader = _volume_run(tmp_path)
    written = _volume_evidence(volume)
    [addressed] = [key for key in written if "/receipts/sha256/" in key]
    reader.overrides[addressed] = b'{"forged": true}'
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local-runs"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    outcome = surface.receipts.read(receipt)["payload"]
    assert outcome["state"] == "verified", "the run tree itself is untouched by this"
    evidence = outcome["evidence"]
    assert any("not the one its name claims" in reason for reason in evidence["refusals"])
    assert not (into / "evidence" / addressed).exists()


def test_fetch_run_refuses_a_receipt_that_does_not_hash_to_its_name(tmp_path: Path) -> None:
    """The receipt arm of the content-addressed digest check, not just the blob arm."""

    volume, reader = _volume_run(tmp_path)
    receipts_dir = volume / "runs" / "brought-home" / "receipts" / "sha256"
    [receipt] = [path for path in receipts_dir.iterdir() if path.is_file()]
    relative = receipt.relative_to(volume / "runs" / "brought-home").as_posix()
    reader.overrides[f"runs/brought-home/{relative}"] = b'{"forged": true}'
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local"

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert relative in str(refusal.value.detail)
    assert "content-addressed" in str(refusal.value.detail)
    # The forged bytes were fetched fresh (no local copy existed) before the
    # digest check ran; the refusal must not leave them under their real name.
    assert not (into / "brought-home" / relative).exists()


def test_fetch_run_refuses_an_object_no_stage_accounts_for(tmp_path: Path) -> None:
    volume, reader = _volume_run(tmp_path)
    (volume / "runs" / "brought-home" / "notes.txt").write_text("stray", encoding="utf-8")
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "runs/brought-home/notes.txt" in str(refusal.value.detail)
    assert reader.fetched == []  # refused at the listing, before a byte moved


def test_fetch_run_refuses_an_artifact_whose_bytes_differ_from_its_manifest(
    tmp_path: Path,
) -> None:
    volume, reader = _volume_run(tmp_path)
    manifest = json.loads(
        (volume / "runs" / "brought-home" / "2_designator" / "manifest.json").read_text("utf-8")
    )
    [entry] = manifest["artifacts"]
    reader.overrides[f"runs/brought-home/{entry['relative_path']}"] = b'{"forged": true}'
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "its stage manifest records" in str(refusal.value.detail)
    # The forged bytes were fetched fresh (no local copy existed) before the
    # digest check ran; the refusal must not leave them under their real name.
    assert not (tmp_path / "local" / "brought-home" / entry["relative_path"]).exists()


def test_fetch_run_brings_home_a_stage_that_never_reached_finish(tmp_path: Path) -> None:
    """A stage whose last write never reached ``StageContext.finish()`` -- a crash,

    an ``EXIT_FATAL``, a ``SIGKILL``, or the pod timer destroying the pod at the
    hard deadline -- writes artifacts with no ``manifest.json``. Such a stage is
    still brought home, its artifacts verified through their own envelope
    (the same checks a stored manifest would apply), and the outcome is named
    ``verified-partial``, never ``verified``.
    """

    volume, reader = _volume_run(tmp_path)
    (volume / "runs" / "brought-home" / "2_designator" / "manifest.json").unlink()
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local"

    receipt = surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "verified-partial"
    assert payload["unmanifested_stages"] == ["designator"]
    assert payload["stages_verified"] == []
    [envelope_only] = payload["envelope_only_artifacts"]
    assert envelope_only.startswith("2_designator/artifacts/")
    assert (into / "brought-home" / "2_designator" / "artifacts").exists()
    assert not (into / "brought-home" / "2_designator" / "manifest.json").exists()


def test_fetch_run_still_refuses_a_forged_artifact_in_an_unmanifested_stage(
    tmp_path: Path,
) -> None:
    """No manifest to check a forged artifact against does not mean no check at all."""

    volume, reader = _volume_run(tmp_path)
    (volume / "runs" / "brought-home" / "2_designator" / "manifest.json").unlink()
    artifacts_dir = volume / "runs" / "brought-home" / "2_designator" / "artifacts"
    [artifact_path] = [path for path in artifacts_dir.rglob("*.json") if path.is_file()]
    relative = artifact_path.relative_to(volume / "runs" / "brought-home").as_posix()
    reader.overrides[f"runs/brought-home/{relative}"] = b'{"forged": true}'
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert not (tmp_path / "local" / "brought-home" / relative).exists()
    # And no empty run-tree skeleton either: a refused fetch that left the
    # directories behind put a structure on disk no completed fetch wrote, which
    # the partial receipt does not mention and a later fetch would walk.
    assert not (tmp_path / "local" / "brought-home").exists()


def test_fetch_run_refuses_a_blob_that_does_not_hash_to_its_name(tmp_path: Path) -> None:
    volume, reader = _volume_run(tmp_path)
    blobs = volume / "runs" / "brought-home" / "2_designator" / "blobs" / "sha256"
    [blob] = [path for path in blobs.iterdir() if path.is_file()]
    reader.overrides[f"runs/brought-home/2_designator/blobs/sha256/{blob.name}"] = b"other"
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert "not the one its name claims" in str(refusal.value.detail)


def test_fetch_run_refuses_a_manifest_the_fetched_artifacts_do_not_rebuild(
    tmp_path: Path,
) -> None:
    """A manifest naming an artifact that never arrived does not reconcile."""

    volume, reader = _volume_run(tmp_path)
    manifest_path = volume / "runs" / "brought-home" / "2_designator" / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["artifacts"].append(
        {
            "artifact_id": "proposal_deadbeefdeadbeef",
            "kind": "proposal",
            "subject_id": "pg_deadbeefdeadbeef",
            "outcome": "proposed",
            "relative_path": "2_designator/artifacts/proposal/proposal_deadbeefdeadbeef.json",
            "sha256": "d" * 64,
        }
    )
    reader.overrides["runs/brought-home/2_designator/manifest.json"] = canonical_bytes(manifest)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert "does not match the manifest the fetched artifacts rebuild" in str(refusal.value.detail)


def test_fetch_run_refuses_a_hostilely_nested_manifest_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """G13: a `manifest.json` nested ~10k deep is otherwise well-formed JSON.

    `json`'s scanner recurses per nesting level, so `_fetched_manifest` must
    catch `RecursionError` beside `ValueError` or this escapes as an
    unclassified crash instead of `FETCH_RUN_FAILED` -- and the fetch-run
    handler itself must not let a `RecursionError` from anywhere in the walk
    past it either.
    """

    volume, reader = _volume_run(tmp_path)
    nested = b'{"extra":' + b"[" * 10_000 + b"]" * 10_000 + b"}"
    reader.overrides["runs/brought-home/2_designator/manifest.json"] = nested
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED


def test_fetch_run_records_a_memory_error_from_the_walk_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G13: the handler's `MemoryError` arm is the one that catches what a size
    ceiling did not. A run tree is untrusted input, the walk reads files whole,
    and an allocation that fails anywhere inside it must still leave a receipt
    and `FETCH_RUN_FAILED` rather than an unclassified crash.
    """

    _volume, reader = _volume_run(tmp_path)

    def _starved(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise MemoryError("cannot allocate the manifest")

    monkeypatch.setattr(surface_module, "_fetched_manifest", _starved)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    payload = surface.receipts.read(surface._descriptor_receipt("fetch-run"))["payload"]
    assert payload["state"] == "partial"
    assert "cannot allocate the manifest" in payload["detail"]


def test_fetch_run_refuses_a_rebuildable_index_that_is_not_a_readable_record(
    tmp_path: Path,
) -> None:
    """G13: a stage index or derived receipt is read whole and parsed too.

    Two ways it can defeat a named refusal, and the same arm must answer both:
    nesting thousands deep, which breaks the parser rather than any size bound,
    and sheer length, which `MAX_FETCH_OBJECT_BYTES` alone does not bound to
    anything a *record* may be.
    """

    volume, reader = _volume_run(tmp_path)
    index = volume / "runs" / "brought-home" / "2_designator" / "index.json"
    # Nesting around a 4,301-digit integer: 3.12 refuses the nesting
    # (RecursionError), 3.14 walks it and refuses the integer (ValueError).
    index.write_bytes(b'{"extra":' + b"[" * 10_000 + b"9" * 4301 + b"]" * 10_000 + b"}")
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as nested:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local-nested", reader=reader)

    assert nested.value.code is ErrorCode.FETCH_RUN_FAILED
    payload = surface.receipts.read(surface._descriptor_receipt("fetch-run"))["payload"]
    assert "index.json is not readable JSON" in payload["detail"]

    # The size arm, measured rather than assumed: the ceiling is lowered to a
    # number this index is over and every genuine record in the fixture tree is
    # under, so what refuses is the index and not the manifest beside it.
    ceiling = 4096
    index.write_bytes(b"[" + b'"x",' * 1024 + b'"x"]')
    manifest_path = volume / "runs" / "brought-home" / "2_designator" / "manifest.json"
    assert index.stat().st_size > ceiling
    assert manifest_path.stat().st_size < ceiling
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(surface_module, "MAX_RECORD_READ_BYTES", ceiling)
        with pytest.raises(OperatorError) as oversized:
            surface.fetch_run(run_id="brought-home", into=tmp_path / "local-big", reader=reader)

    assert oversized.value.code is ErrorCode.FETCH_RUN_FAILED
    payload = surface.receipts.read(surface._descriptor_receipt("fetch-run"))["payload"]
    assert "limit for a JSON record" in payload["detail"]


def test_the_tree_read_ceiling_stays_below_what_fetch_run_will_pull() -> None:
    """G13, and the reason this diff needed a second pass: a read ceiling set
    above every other ceiling on the path it guards can never fire there.

    `fetch_run` pulls objects bounded by `MAX_FETCH_OBJECT_BYTES`, so a tree
    read bound above that number is decoration on this path, not a bound. The
    record ceiling is the tighter of the two and must stay that way.
    """

    from common.armarium_formats import MAX_EXPORT_ARCHIVE_BYTES
    from common.runtree import store as runtree_store

    assert runtree_store._MAX_TREE_READ_BYTES < surface_module.MAX_FETCH_OBJECT_BYTES
    assert runtree_store.MAX_RECORD_READ_BYTES <= runtree_store._MAX_TREE_READ_BYTES
    # An export archive that seals must also come home.
    assert MAX_EXPORT_ARCHIVE_BYTES <= surface_module.MAX_FETCH_OBJECT_BYTES


def test_fetch_run_never_overwrites_a_local_file_that_differs(tmp_path: Path) -> None:
    _volume, reader = _volume_run(tmp_path)
    into = tmp_path / "local"
    local_run = into / "brought-home" / "run.json"
    local_run.parent.mkdir(parents=True)
    local_run.write_bytes(b"a different run wearing this name\n")
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=into, reader=reader)

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "was not overwritten" in str(refusal.value.detail)
    assert local_run.read_bytes() == b"a different run wearing this name\n"
    assert [path.name for path in local_run.parent.iterdir()] == ["run.json"]


def test_fetch_run_refuses_a_prefix_with_nothing_under_it(tmp_path: Path) -> None:
    _volume, reader = _volume_run(tmp_path)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="never-written", into=tmp_path / "local", reader=reader)

    assert "nothing is stored under 'runs/never-written/'" in str(refusal.value.detail)


def test_fetch_run_refuses_a_prefix_with_no_run_authority(tmp_path: Path) -> None:
    volume, reader = _volume_run(tmp_path)
    (volume / "runs" / "brought-home" / "run.json").unlink()
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert "no run.json under" in str(refusal.value.detail)
    assert reader.fetched == []


def test_fetch_run_refuses_a_tampered_run_authority(tmp_path: Path) -> None:
    volume, reader = _volume_run(tmp_path)
    authority = json.loads((volume / "runs" / "brought-home" / "run.json").read_text("utf-8"))
    authority["config_digest"] = "e" * 64
    reader.overrides["runs/brought-home/run.json"] = canonical_bytes(authority)
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    assert "self-hash" in str(refusal.value.detail)
    assert reader.fetched == ["runs/brought-home/run.json"]


def test_fetch_run_needs_a_network_volume_when_no_reader_is_supplied(tmp_path: Path) -> None:
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local")

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "--network-volume" in str(refusal.value.detail)


def test_fetch_run_refuses_a_bad_run_id_by_name(tmp_path: Path) -> None:
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError) as refusal:
        surface.fetch_run(
            run_id="Not A Run", into=tmp_path / "local", reader=DirectoryRunReader(tmp_path)
        )

    assert refusal.value.code is ErrorCode.FETCH_RUN_FAILED
    assert "run_id" in str(refusal.value.detail)


def test_fetch_run_writes_a_partial_receipt_when_it_stops(tmp_path: Path) -> None:
    volume, reader = _volume_run(tmp_path)
    (volume / "runs" / "brought-home" / "stray.bin").write_bytes(b"?")
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")

    with pytest.raises(OperatorError):
        surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader)

    receipt = surface._descriptor_receipt("fetch-run")
    assert receipt is not None
    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "partial"
    assert "stray.bin" in payload["detail"]


def test_cli_fetch_run_reads_the_named_volume_and_never_a_local_stand_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class ObservedSurface:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def fetch_run(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            observed.update(kwargs)

    monkeypatch.setattr(cli, "OperatorSurface", ObservedSurface)

    assert (
        cli.main(
            [
                "--workspace",
                str(tmp_path),
                "fetch-run",
                "--run-id",
                "brought-home",
                "--into",
                str(tmp_path / "local"),
                "--network-volume",
                "EU-CZ-1:vol123",
            ]
        )
        == 0
    )
    assert observed["run_id"] == "brought-home"
    assert observed["into"] == tmp_path / "local"
    assert observed["volume"].volume_id == "vol123"
    assert observed["volume"].datacenter_id == "EU-CZ-1"
    assert (
        cli.main(["--workspace", str(tmp_path), "fetch-run", "--run-id", "x", "--into", "y"]) == 2
    )


def test_cli_fetch_run_names_itself_not_upload_on_a_malformed_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed ``--network-volume`` on ``fetch-run`` must not send the
    operator toward ``verbatus upload``.

    ``UPLOAD_VOLUME_UNAVAILABLE``'s registered copy ends "run `verbatus
    upload` again", which is backwards advice for an operator who asked to
    read a run tree home. ``_network_volume`` is verb-aware for exactly this
    reason: on ``fetch-run`` the same malformed-input refusal is
    ``FETCH_RUN_FAILED``, whose copy names ``verbatus fetch-run``, matching
    every other volume failure this verb can hit further downstream
    (``OperatorSurface.fetch_run``).
    """

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "fetch-run",
            "--run-id",
            "brought-home",
            "--into",
            str(tmp_path / "local"),
            "--network-volume",
            "EU-CZ-1",
        ]
    )

    assert exit_code == 2
    printed = capsys.readouterr().out
    assert "verbatus fetch-run" in printed
    assert "verbatus upload" not in printed


def test_cli_upload_still_names_itself_on_a_malformed_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verb-aware error code leaves ``upload``'s own behavior untouched."""

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "upload",
            "--source",
            str(tmp_path / "pages"),
            "--sealed-manifest",
            str(tmp_path / "sealed-manifest.json"),
            "--network-volume",
            "EU-CZ-1",
        ]
    )

    assert exit_code == 2
    printed = capsys.readouterr().out
    assert "verbatus upload" in printed
    assert "verbatus fetch-run" not in printed


# -- operator records that can diagnose a run from the state directory alone ---


def _complete_export(run_root, run_id):  # type: ignore[no-untyped-def]
    del run_root, run_id
    return {
        "aggregate": {"status": "complete", "reasons": []},
        "pages": [{"ordinal": 1}],
        "delivered": [{"act_key": "a1"}],
        "non_delivered": [],
        "expected_acts": 1,
    }


def _run_receipts(surface: OperatorSurface, run_id: str) -> list[dict[str, object]]:
    return [
        payload for _path, payload in surface._run_receipts() if payload.get("run_id") == run_id
    ]


def test_every_run_receipt_carries_identity_configuration_commit_and_output(
    tmp_path: Path,
) -> None:
    """A run's receipt is enough to say what ran, from what, under which commit and config."""

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="run r: complete\n", stderr="1 door refusal(s); see report\n"
    )
    surface._armarium_export = _complete_export  # type: ignore[method-assign]
    roster = ROOT / "config" / "models-real.toml"
    catalogue = ROOT / "config" / "serving_recipes_real.toml"

    surface.run(
        run_id="documented-run",
        models_config=roster,
        serving_recipes_config=catalogue,
    )

    started, finished = _run_receipts(surface, "documented-run")
    assert started["state"] == "started"
    assert finished["state"] == "complete"
    for receipt in (started, finished):
        assert receipt["run_root"] == "runs", "state-relative, so a moved state root still reads"
        assert receipt["scenario"] == "happy"
        assert receipt["started_at"] == "2026-08-09T12:00:00Z"
        argv = receipt["argv"]
        assert isinstance(argv, list) and "--run-id" in argv and "documented-run" in argv
        assert "--models-config" in argv
        configuration = receipt["configuration"]
        assert configuration["models_config"] == {
            "path": str(roster),
            "sha256": sha256_file(roster),
        }
        assert configuration["serving_recipes_config"]["sha256"] == sha256_file(catalogue)
        assert configuration["submission_manifest"] is None
        commit = receipt["repository_commit"]
        assert (commit is None) != (receipt["repository_commit_unreadable"] is None)
        if commit is not None:
            assert commit == _repository_commit_or_reason(ROOT)[0]
            # The orchestrator invocation itself must carry the commit
            # the receipt says the run ran under, not only the receipt.
            assert argv[argv.index("--repository-commit") + 1] == commit
    assert finished["exit_code"] == 0
    assert finished["stderr_tail"] == "1 door refusal(s); see report\n"
    assert finished["stdout_tail"] == "run r: complete\n"
    assert finished["ended_at"] == "2026-08-09T12:00:00Z"
    assert str(finished["armarium_export"]).startswith("runs/documented-run/")
    assert finished["reasons"] == []
    # The stages' own stderr reaches the screen -- the Door's refusal count was
    # swallowed on every exit but the crash drill's.
    assert "1 door refusal(s); see report" in messages
    assert any(
        line == f"Review it read-only with: verbatus review --run-root "
        f"{surface.state_root / 'runs'} --run-id documented-run"
        for line in messages
    )


def test_a_failed_run_names_its_cause_on_screen_in_the_receipt_and_in_status(
    tmp_path: Path,
) -> None:
    """Six different causes produced the same four opaque lines; each had a one-line
    explanation sitting unread in the receipt, and `status` withheld it too."""

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "pipeline/orchestrator/run.py", line 1, in <module>\n'
        "IncompatibleReuse: run failed-run is bound to different config_digest\n"
    )
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=2, stdout="", stderr=stderr
    )

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="failed-run")

    assert failure.value.code is ErrorCode.RUN_FAILED
    rendered = failure.value.render()
    assert "IncompatibleReuse: run failed-run is bound to different config_digest" in rendered
    assert "Saved run receipt:" in rendered
    assert "Traceback" not in rendered
    _started, failed = _run_receipts(surface, "failed-run")
    assert failed["state"] == "failed"
    assert failed["exit_code"] == 2
    assert (
        failed["reason"] == "IncompatibleReuse: run failed-run is bound to different config_digest"
    )
    assert failed["detail"] == stderr
    assert failed["stderr_tail"] == stderr
    assert any(line.startswith("Run failed-run failed: IncompatibleReuse") for line in messages)

    lines = surface.status()

    run_root = surface.state_root / "runs"
    assert f"  Run: failed-run; run root: {run_root}." in lines
    assert "  Saved run state: failed." in lines
    assert (
        "  Reason: IncompatibleReuse: run failed-run is bound to different config_digest" in lines
    )
    assert "  Recorded output:" in lines
    assert "    IncompatibleReuse: run failed-run is bound to different config_digest" in lines
    review = (
        f"  Review it read-only with: verbatus review --run-root {run_root} --run-id failed-run"
    )
    assert review in lines
    receipt = surface._descriptor_receipt("run")
    assert f"  Saved receipt: {receipt}" in lines


def test_a_run_held_before_the_armarium_is_a_held_run_that_keeps_its_reason(
    tmp_path: Path,
) -> None:
    """An Attestatores hold exits 3 with no export record.

    That was filed as `armarium-record-unreadable` under RUN_FAILED with no
    run id and the hold reason -- on stderr -- discarded, so a legitimately
    held run read as a broken tool and `status` could not say why.
    """

    messages: list[str] = []
    notifications: list[tuple[str, str]] = []
    surface = _surface(tmp_path, output=messages)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[],
        returncode=3,
        stdout="",
        stderr="attestatores: held: 2 chair(s) reported no outcome for act a1\n",
    )

    def record_notification(event: str, message: str):  # type: ignore[no-untyped-def]
        notifications.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = record_notification

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="held-early")

    assert failure.value.code is ErrorCode.RUN_HELD
    reason = "attestatores: held: 2 chair(s) reported no outcome for act a1"
    assert reason in failure.value.render()
    _started, held = _run_receipts(surface, "held-early")
    assert held["state"] == "held"
    assert held["run_id"] == "held-early"
    assert held["reasons"] == [reason]
    assert held["armarium_export"] is None
    assert isinstance(held["armarium_export_unreadable"], str)
    assert f"Hold reason: {reason}" in messages
    assert notifications == [
        ("decision", f"Verbatus run held-early is held and needs a decision: {reason}")
    ]
    assert f"  Hold reason: {reason}" in surface.status()


def test_a_held_exit_over_an_export_record_whose_seal_fails_names_the_seal_not_a_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An export record exists, so "held before the Armarium, no export yet" would be false."""

    surface = _surface(tmp_path)
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=3, stdout="", stderr="armarium: held\n"
    )
    tree = surface_module.RunTree(surface.state_root / "runs", "unsealed-held")
    export = tree.resolve(
        tree.artifact_path(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))
    )
    export.parent.mkdir(parents=True)
    export.write_text("{}", encoding="utf-8")

    def refuse(_tree):  # type: ignore[no-untyped-def]
        raise SchemaRefusal("simulated: armarium stage-seal no longer verifies")

    monkeypatch.setattr(surface_module, "verify_final_seal", refuse)

    with pytest.raises(OperatorError) as failure:
        surface.run(run_id="unsealed-held")

    assert failure.value.code is ErrorCode.RUN_FAILED
    detail = str(failure.value.detail)
    assert "could not be read" in detail
    assert "stage-seal no longer verifies" in detail
    _started, ended = _run_receipts(surface, "unsealed-held")
    assert ended["state"] == "armarium-record-unreadable"


def test_an_interrupt_or_a_sigterm_during_the_run_leaves_a_resumable_receipt(
    tmp_path: Path,
) -> None:
    """A signal-killed run left no operator record and `status` reported the machine empty.

    RUN_INTERRUPTED's copy -- "run again with the same run name to resume; this
    is safe" -- was reachable only through the crash drill. SIGTERM ended the
    process with no output at all.
    """

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    before = signal.getsignal(signal.SIGTERM)

    def terminated(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM must interrupt the run before the runner returns")

    surface.runner = terminated  # type: ignore[method-assign]

    with pytest.raises(OperatorError) as interruption:
        surface.run(run_id="killed-run")

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    assert signal.getsignal(signal.SIGTERM) == before, "the handler is scoped to the child"
    _started, interrupted = _run_receipts(surface, "killed-run")
    assert interrupted["state"] == "interrupted-recoverable"
    assert interrupted["run_root"] == "runs"
    assert "interrupted by a signal" in str(interrupted["last_observed_work"])
    assert any("Review it read-only with:" in line for line in messages)

    def keyboard(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    surface.runner = keyboard  # type: ignore[method-assign]
    messages.clear()
    with pytest.raises(OperatorError) as second:
        surface.run(run_id="killed-run")

    assert second.value.code is ErrorCode.RUN_INTERRUPTED
    assert messages[0].startswith("Resuming run killed-run.")
    assert "  Saved run state: interrupted-recoverable." in surface.status()


def test_a_run_killed_outright_is_still_on_record_because_its_start_was_written_first(
    tmp_path: Path,
) -> None:
    """SIGKILL cannot be caught by anyone, so the receipt is written before the child starts."""

    class Killed(BaseException):
        """Stands in for a kill this process never sees."""

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)

    def killed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise Killed

    surface.runner = killed  # type: ignore[method-assign]
    with pytest.raises(Killed):
        surface.run(run_id="vanished-run")

    (started,) = _run_receipts(surface, "vanished-run")
    assert started["state"] == "started"
    lines = surface.status()
    assert "  Saved run state: started." in lines
    assert any(
        "never reported an end state" in line and "`verbatus run --run-id vanished-run`" in line
        for line in lines
    )
    # The next run of that name resumes rather than starting afresh.
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = _complete_export  # type: ignore[method-assign]
    messages.clear()
    surface.run(run_id="vanished-run")
    assert messages[0].startswith("Resuming run vanished-run.")
    # Once the run has ended, the start receipt no longer reads as a lost run.
    assert not any("never reported an end state" in line for line in surface.status())


def _fake_bundle(bundle_bytes: bytes):  # type: ignore[no-untyped-def]
    def write(self, _root, _run_id, destination):  # type: ignore[no-untyped-def]
        del self
        destination.write_bytes(bundle_bytes)

    return write


def test_export_names_its_run_first_and_exits_partial_over_a_held_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` exited 0 over a held run with a receipt hardcoded to `complete`."""

    messages: list[str] = []
    notifications: list[tuple[str, str]] = []
    surface = _surface(tmp_path, output=messages)
    surface._write_action(
        "run",
        {"summary": "test run", "state": "partial", "run_root": "runs", "run_id": "held-run"},
        descriptor_action="run",
    )
    surface._armarium_export = lambda _root, _run_id: {  # type: ignore[method-assign]
        "aggregate": {"status": "partial", "reasons": ["act a2 is held-for-review"]},
        "pages": [{}, {}],
        "delivered": [{}],
        "non_delivered": [{"category": "held-for-review"}],
        "expected_acts": 2,
    }
    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", _fake_bundle(b"partial"))

    def record_notification(event: str, message: str):  # type: ignore[no-untyped-def]
        notifications.append((event, message))
        return notify_bridge.NotifyOutcome(True, True, "delivered")

    surface.notifier = record_notification

    with pytest.raises(OperatorError) as partial:
        surface.export()

    assert partial.value.code is ErrorCode.EXPORT_PARTIAL
    assert "recorded run state partial" in partial.value.render()
    assert messages[0] == (
        "No run was named, so this exports run held-run, the run recorded most recently in "
        "this operator state."
    )
    assert messages[1] == f"Exporting run held-run from run root {surface.state_root / 'runs'}."
    receipt = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    assert receipt["state"] == "partial"
    assert receipt["run_id"] == "held-run"
    assert receipt["run_root"] == "runs"
    assert receipt["reasons"] == ["act a2 is held-for-review"]
    assert receipt["bundle"].startswith("exports/held-run-armarium-base-")
    assert (surface.state_root / receipt["bundle"]).read_bytes() == b"partial"
    assert notifications == [
        (
            "milestone",
            "Verbatus export for run held-run landed as partial, not complete: "
            f"{surface.state_root / receipt['bundle']}",
        )
    ]
    lines = surface.status()
    assert "  Saved export state: partial." in lines
    assert any(line.startswith("  Run: held-run; bundle: ") for line in lines)


def test_export_with_a_run_id_uses_that_run_even_after_another_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)
    for run_id, root in (("older", "older-runs"), ("newer", "runs")):
        surface._write_action(
            "run",
            {"summary": "test run", "state": "complete", "run_root": root, "run_id": run_id},
            descriptor_action="run",
        )
    seen: list[tuple[Path, str]] = []

    def export_of(root, run_id):  # type: ignore[no-untyped-def]
        seen.append((root, run_id))
        return _complete_export(root, run_id)

    surface._armarium_export = export_of  # type: ignore[method-assign]
    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", _fake_bundle(b"older"))

    bundle = surface.export(run_id="older")

    # Read once to check it and once more after the copy, both from the named run.
    assert seen == [(surface.state_root / "older-runs", "older")] * 2
    assert bundle.name.startswith("older-armarium-base-")
    with pytest.raises(OperatorError) as missing:
        surface.export(run_id="never-recorded")
    assert missing.value.code is ErrorCode.EXPORT_MISSING
    assert "never-recorded" in missing.value.render()


def test_export_refuses_a_run_id_recorded_under_two_different_run_roots(
    tmp_path: Path,
) -> None:
    """A run_id is not guaranteed unique across every root this operator state
    has ever recorded; two genuinely different runs colliding on the same name
    is a real ambiguity, not "the same run, re-recorded" (which is what taking
    the latest receipt for an unambiguous run_id already, correctly, does)."""
    surface = _surface(tmp_path)
    for root in ("root-a", "root-b"):
        surface._write_action(
            "run",
            {"summary": "test run", "state": "complete", "run_root": root, "run_id": "dup"},
            descriptor_action="run",
        )

    with pytest.raises(OperatorError) as ambiguous:
        surface.export(run_id="dup")
    assert ambiguous.value.code is ErrorCode.EXPORT_AMBIGUOUS
    rendered = ambiguous.value.render()
    assert str(surface.state_root / "root-a") in rendered
    assert str(surface.state_root / "root-b") in rendered


def test_export_run_root_disambiguates_a_colliding_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)
    for root in ("root-a", "root-b"):
        surface._write_action(
            "run",
            {"summary": "test run", "state": "complete", "run_root": root, "run_id": "dup"},
            descriptor_action="run",
        )
    seen: list[tuple[Path, str]] = []

    def export_of(root, run_id):  # type: ignore[no-untyped-def]
        seen.append((root, run_id))
        return _complete_export(root, run_id)

    surface._armarium_export = export_of  # type: ignore[method-assign]
    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", _fake_bundle(b"root-b"))

    surface.export(run_id="dup", run_root=surface.state_root / "root-b")

    # Read once to check it and once more after the copy, both from the named run.
    assert seen == [(surface.state_root / "root-b", "dup")] * 2


def test_export_run_root_naming_no_matching_receipt_is_refused(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    surface._write_action(
        "run",
        {"summary": "test run", "state": "complete", "run_root": "root-a", "run_id": "solo"},
        descriptor_action="run",
    )

    with pytest.raises(OperatorError) as missing:
        surface.export(run_id="solo", run_root=tmp_path / "not-a-recorded-root")
    assert missing.value.code is ErrorCode.EXPORT_MISSING


def test_derived_evidence_prefixes_reads_the_launch_receipt(tmp_path: Path) -> None:
    """`--evidence-prefix` derives from the same saved launch receipt
    `--evidence-key` already does (`cli._derived_evidence_keys`), so an
    operator is not asked to retype a 32-hex token by hand for one flag while
    the other derives it for free. Real requests write every report path at
    the volume root (boot_a_request.py, boot_b_request.py); only
    bootstrap_main's own report path names this launch's preflight directory
    (`preflight/<that report path's stem>`), so the fixture below mirrors a
    real Boot B nested argv (pod_run's own argv, bootstrap_main's appended
    after the first literal '--') rather than a simplified one."""
    token = "c" * 32
    run_half = [
        "python",
        "-m",
        "operations.pod.pod_run",
        "--report-path",
        f"/workspace/pod-run-report-{token}.json",
        "--run-id",
        "r1",
    ]
    bootstrap_half = [
        "--volume-mount-path",
        "/workspace",
        "--report-path",
        f"/workspace/bootstrap-report-{token}.json",
    ]
    nested = json.dumps([*run_half, "--", *bootstrap_half])
    receipt = tmp_path / "launch-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "payload": {
                    "request": {
                        "docker_start_cmd": [
                            "python",
                            "-m",
                            "operations.pod.pod_timer",
                            "--report-path",
                            f"/workspace/pod-runtime-report-{token}.json",
                            "--bootstrap-command-json",
                            nested,
                        ],
                        "volume_mount_path": "/workspace",
                        "volume_id": "vol123",
                    }
                }
            }
        )
    )
    volume = VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol123")

    prefixes = cli._derived_evidence_prefixes(receipt, volume)

    assert prefixes == (f"preflight/bootstrap-report-{token}",)


def test_status_names_fetch_run_volumes_and_unexpected_failures(tmp_path: Path) -> None:
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    volume, reader = _volume_run(tmp_path)
    spec = VolumeSpec(datacenter_id="EU-CZ-1", volume_id="vol123")
    surface.fetch_run(run_id="brought-home", into=tmp_path / "local", reader=reader, volume=spec)
    fetched = surface.receipts.read(surface._descriptor_receipt("fetch-run"))["payload"]
    assert fetched["volume"] == {
        "datacenter_id": "EU-CZ-1",
        "volume_id": "vol123",
        "endpoint_url": "https://s3api-eu-cz-1.runpod.io/",
    }
    source, manifest = _manifest(tmp_path)
    surface.upload(
        source,
        sealed_manifest=manifest,
        volume=spec,
        target=LocalFixtureObjectStore(tmp_path / "stand-in-volume"),
    )
    uploaded = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert uploaded["volume"]["volume_id"] == "vol123"
    unexpected = cli.record_unexpected(RuntimeError("boom"), ["status"], surface.state_root)
    assert unexpected.code is ErrorCode.UNEXPECTED

    lines = surface.status()

    assert f"  Run: brought-home; fetched into: {(tmp_path / 'local').resolve()}." in lines
    assert lines.count("  Volume: EU-CZ-1:vol123 at https://s3api-eu-cz-1.runpod.io/.") == 2
    assert "  Saved fetch state: verified." in lines
    assert "  RuntimeError: boom" in lines
    assert "  Command: verbatus status" in lines
    assert f"  Working directory: {os.getcwd()}" in lines
    assert any(line.startswith("- unexpected record 1: ") for line in lines)


def test_status_names_an_advance_so_the_operators_sequence_is_reconstructible(
    tmp_path: Path,
) -> None:
    """Status must have an arm for advance.

    Exercises `record_advance` and `_status_projection`'s advance arm directly,
    the way `record_backup`'s own coverage does for the sibling verb --
    `_advance_with_confirmation`'s own boundary/confirmation machinery is
    covered separately in test_advance_modes.py and test_permission_boundary.py.
    """
    surface = _surface(tmp_path)
    reference = ApprovalRecordReference("2_designator/approvals/a.json", "b" * 64)

    receipt = surface.record_advance(
        run_id="staged",
        run_root=tmp_path / "runs",
        stage="designator",
        reason="operator reviewed the completed run",
        seal_digest="c" * 64,
        reference=reference,
    )

    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "complete"
    assert payload["stage"] == "designator"
    assert payload["seal_digest"] == "c" * 64
    assert payload["approval_record"] == {
        "relative_path": "2_designator/approvals/a.json",
        "sha256": "b" * 64,
    }

    lines = surface.status()
    assert any(line.startswith("- advance record 1: ") for line in lines)
    status = "\n".join(lines)
    assert f"Run: staged; run root: {tmp_path / 'runs'}; stage: designator." in status


def test_status_rejoins_a_state_relative_run_root_for_an_advance_record(
    tmp_path: Path,
) -> None:
    """The `advance` status arm must resolve a state-relative run root the same way the
    `run` arm already does (`_display_path`), not print the stored relative fragment
    unchanged."""
    surface = _surface(tmp_path)
    reference = ApprovalRecordReference("2_designator/approvals/a.json", "b" * 64)
    run_root = surface.state_root / "runs"

    surface.record_advance(
        run_id="under-state-root",
        run_root=run_root,
        stage="designator",
        reason="operator reviewed the completed run",
        seal_digest="c" * 64,
        reference=reference,
    )

    status = "\n".join(surface.status())
    assert f"Run: under-state-root; run root: {run_root}; stage: designator." in status
    assert f"Passed boundary sealed at: {'c' * 64}." in status
    assert "Approval record: 2_designator/approvals/a.json" in status
    assert "Reason: operator reviewed the completed run" in status


def test_the_cli_catch_all_writes_an_unexpected_receipt_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one failure class with nothing to hand to a later session leaves a record."""

    class BrokenSurface:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def status(self) -> None:
            raise RuntimeError("boom: Traceback (most recent call last): looks like a trace")

    monkeypatch.setattr(cli, "OperatorSurface", BrokenSurface)
    state = tmp_path / "state"

    assert cli.main(["--state-dir", str(state), "status"]) == 2

    printed = capsys.readouterr().out
    assert "Verbatus met a problem it could not classify." in printed
    receipts = sorted((state / "receipts").glob("unexpected-*.json"))
    assert len(receipts) == 1
    assert f"Saved unexpected receipt: {receipts[0]}" in printed
    saved = json.loads(receipts[0].read_text(encoding="utf-8"))["payload"]
    assert saved["exception_type"] == "RuntimeError"
    assert saved["message"].startswith("boom")
    assert "RuntimeError: boom" in saved["traceback"]
    assert saved["argv"] == ["--state-dir", str(state), "status"]
    assert saved["cwd"] == os.getcwd()
    loaded = DescriptorStore(state).load()
    assert loaded is not None and loaded["actions"]["unexpected"] == receipts[0].name


def test_a_workspace_that_is_not_a_checkout_refuses_run_but_not_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The check is against `--workspace`, for exactly the directories the word
    reads, so a word whose workspace is not the checkout is never refused."""

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state = tmp_path / "state"
    common = ["--workspace", str(elsewhere), "--state-dir", str(state)]

    assert cli.main([*common, "run", "--run-id", "x"]) == 2
    printed = capsys.readouterr().out
    assert "not a source checkout" in printed
    assert str(elsewhere) in printed
    assert "`verbatus run` reads pipeline, config, proof" in printed
    assert not state.exists(), "nothing was started and nothing was recorded"

    # A word whose workspace is legitimately not the checkout is answered on
    # its own terms: here, that there are no records yet.
    assert cli.main([*common, "status"]) == 2
    printed = capsys.readouterr().out
    assert "There are no saved operator records to show." in printed
    assert "not a source checkout" not in printed


def test_a_copied_state_directory_reads_exports_and_resumes_at_its_new_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every reference a receipt makes to a file under the state root survives a move.

    Receipts name state-root paths relatively, so a state directory copied
    elsewhere still reads every intact receipt, exports and resumes.
    """

    original = tmp_path / "original" / "state"
    surface = OperatorSurface(
        ROOT,
        original,
        now=lambda: START,
        present=lambda _line="": None,
    )
    surface.runner = lambda *a, **k: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=[], returncode=0, stdout="", stderr=""
    )
    surface._armarium_export = _complete_export  # type: ignore[method-assign]
    surface.run(run_id="portable-run")
    monkeypatch.setattr(OperatorSurface, "_write_base_armarium_bundle", _fake_bundle(b"bundle"))
    surface.export(run_id="portable-run")

    moved = tmp_path / "restored" / "elsewhere" / "state"
    shutil.copytree(original, moved)
    lines: list[str] = []
    relocated = OperatorSurface(
        ROOT,
        moved,
        now=lambda: START,
        present=lines.append,
    )
    relocated._armarium_export = _complete_export  # type: ignore[method-assign]

    status = relocated.status()

    assert not any("UNREADABLE" in line for line in status)
    assert f"  Run: portable-run; run root: {moved / 'runs'}." in status
    assert relocated._prior_run_state("portable-run") == "complete"
    bundle = relocated.export(run_id="portable-run")
    assert bundle.parent == moved / "exports"
    relocated.runner = surface.runner
    lines.clear()
    relocated.run(run_id="portable-run")
    assert lines[0].startswith("Run portable-run already has saved state complete")


def _fetched_page_run(tmp_path: Path, run_id: str) -> tuple[OperatorSurface, Path]:
    """A page-read fixture run on a volume, brought home by a state that never ran it."""

    volume = tmp_path / "volume"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "page-unbroken",
            "--run-id",
            run_id,
            "--run-root",
            str(volume / "runs"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    into = tmp_path / "local-runs"
    surface.fetch_run(run_id=run_id, into=into, reader=DirectoryRunReader(volume))
    return surface, into


def test_export_takes_a_run_fetch_run_brought_home(tmp_path: Path) -> None:
    surface, into = _fetched_page_run(tmp_path, "fetched-run")
    assert not surface.descriptor.load()["history"].get("run")

    bundle = surface.export(run_id="fetched-run")

    assert bundle.is_file()
    receipt = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    assert receipt["state"] == "complete"
    assert receipt["run_root"] == str(into.resolve())
    assert receipt["sha256"] == sha256_file(bundle)
    # Named or not, the fetched run is the one exported.
    assert surface.export().name == bundle.name


def test_export_does_not_take_a_fetch_that_stopped_before_the_tree_was_verified(
    tmp_path: Path,
) -> None:
    volume, reader = _volume_run(tmp_path, "half-fetched")
    (volume / "runs" / "half-fetched" / "run.json").write_bytes(b"{}")
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    with pytest.raises(OperatorError):
        surface.fetch_run(run_id="half-fetched", into=tmp_path / "local-runs", reader=reader)
    states = [
        surface.receipts.read(path)["payload"]["state"]
        for path in surface.receipts.receipts.glob("*.json")
    ]
    assert states == ["partial"]

    with pytest.raises(OperatorError) as missing:
        surface.export(run_id="half-fetched")

    assert missing.value.code is ErrorCode.EXPORT_MISSING


def test_export_does_not_take_a_fetch_that_verified_a_stage_by_envelope_only(
    tmp_path: Path,
) -> None:
    volume, reader = _volume_run(tmp_path, "envelope-only")
    (volume / "runs" / "envelope-only" / "2_designator" / "manifest.json").unlink()
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    receipt = surface.fetch_run(run_id="envelope-only", into=tmp_path / "local", reader=reader)
    assert surface.receipts.read(receipt)["payload"]["state"] == "verified-partial"

    with pytest.raises(OperatorError) as missing:
        surface.export(run_id="envelope-only")

    assert missing.value.code is ErrorCode.EXPORT_MISSING


def test_export_does_not_take_a_fetch_whose_canary_raised_an_alarm(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "page-unbroken",
            "--run-id",
            "canary-dead",
            "--run-root",
            str(volume / "runs"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    surface = _surface(tmp_path, workspace=tmp_path / "workspace")
    with pytest.raises(OperatorError) as alarm:
        surface.fetch_run(
            run_id="canary-dead",
            into=tmp_path / "local-runs",
            reader=DirectoryRunReader(volume),
            canary_root=tmp_path / "private-canary",
        )
    assert alarm.value.code is ErrorCode.CANARY_ALARM
    receipt = surface._descriptor_receipt("fetch-run")
    assert receipt is not None
    payload = surface.receipts.read(receipt)["payload"]
    assert payload["state"] == "canary-alarm"
    assert payload["unmanifested_stages"] == []

    with pytest.raises(OperatorError) as missing:
        surface.export(run_id="canary-dead")

    assert missing.value.code is ErrorCode.EXPORT_MISSING
    # Refused when choosing a run, before any Armarium export is attempted.
    assert missing.value.detail is None


def test_a_held_run_resumes_from_the_recensor_through_verbatus_run(tmp_path: Path) -> None:
    """After a decision or an advance, `verbatus run --from recensor --to armarium` resumes it."""
    from conftest import advance_held_recensor

    surface = _surface(tmp_path)
    with pytest.raises(OperatorError) as held:
        surface.run(run_id="resumed", scenario="page-review")
    assert held.value.code is ErrorCode.RUN_HELD
    run_root = surface.state_root / "runs"
    assert not (run_root / "resumed" / "7_armarium").exists()

    advance_held_recensor(run_root, "resumed")
    messages: list[str] = []
    surface._present = messages.append
    # No scenario is named: the resume runs under the one the run was started with.
    with pytest.raises(OperatorError) as partial:
        surface.run(run_id="resumed", from_stage="recensor", to_stage="armarium")

    assert partial.value.code is ErrorCode.RUN_HELD
    assert (run_root / "resumed" / "7_armarium").is_dir()
    argv = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]["argv"]
    assert argv[argv.index("--scenario") + 1] == "page-review"
    assert argv[argv.index("--from") :][:4] == ["--from", "recensor", "--to", "armarium"]
    assert any("stages recensor through armarium only" in line for line in messages)


def test_a_resume_range_names_both_ends_ends_at_the_export_and_needs_a_known_run(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)
    for kwargs, refusal in (
        ({"from_stage": "recensor"}, "give both or neither"),
        ({"from_stage": "recensor", "to_stage": "recensor"}, "ends at armarium"),
        ({"from_stage": "recensor", "to_stage": "armarium"}, "no readable run receipt"),
    ):
        with pytest.raises(OperatorError) as refused:
            surface.run(run_id="unknown", **kwargs)
        assert refused.value.code is ErrorCode.INVALID_COMMAND
        assert refusal in (refused.value.detail or "")
    with pytest.raises(OperatorError, match="not understood"):
        cli.build_parser().parse_args(
            ["run", "--run-id", "r", "--from", "recensor", "--to", "recensor"]
        )
    parsed = cli.build_parser().parse_args(
        ["run", "--run-id", "r", "--from", "recensor", "--to", "armarium"]
    )
    assert (parsed.from_stage, parsed.to_stage) == ("recensor", "armarium")


def _triage_documents(
    tmp_path: Path, source: Path, *, skip: str | None = None, actor: dict | None = None
):
    """A decision manifest with one whole-frame row per sealed page, and a recipe."""
    from common.contracts import triage as triage_contract

    rows = []
    for page in sorted(source.iterdir()):
        if page.name == skip:
            continue
        part = triage_contract.make_part(
            {"x": 0, "y": 0, "w": 10, "h": 10},
            {"x": 0, "y": 0, "w": 10, "h": 10},
            0,
            colour_mode="keep",
        )
        rows.append(
            triage_contract.make_row(
                corpus_id="synthetic",
                source_frame_sha256=hashlib.sha256(page.read_bytes()).hexdigest(),
                frame={"width": 10, "height": 10},
                split=triage_contract.make_split([part]),
                re_shoot_cluster_id=None,
                confidence=0,
                mode="auto",
                actor=actor or {"kind": "human", "identity": "lead", "revision": None},
                human_override=False,
            )
        )
    manifest = tmp_path / "triage-decision-manifest.json"
    manifest.write_bytes(
        canonical_bytes(
            {"schema": "triage-decision-manifest-v1", "corpus_id": "synthetic", "records": rows}
        )
    )
    from operations.triage.pagekit_recipe import make_recipe

    recipe = tmp_path / "triage-producer-recipe.json"
    recipe.write_bytes(
        canonical_bytes(
            make_recipe(revision="0.1.0", settings_sha256="b" * 64, detector_methods={})
        )
    )
    return manifest, recipe


def test_upload_carries_the_triage_documents_beside_the_scans(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    triage, recipe = _triage_documents(tmp_path, source)
    store = LocalFixtureObjectStore(tmp_path / "volume")

    surface.upload(
        source,
        sealed_manifest=manifest,
        target=store,
        triage_decision_manifest=triage,
        triage_producer_recipe=recipe,
    )

    # Published before the submission's own manifest, which marks the upload complete.
    assert store.puts[-3:] == [
        "submission-triage-decision-manifest.json",
        "submission-triage-producer-recipe.json",
        "submission-manifest.json",
    ]
    volume = tmp_path / "volume"
    assert (volume / "submission-triage-decision-manifest.json").read_bytes() == (
        triage.read_bytes()
    )
    assert (volume / "submission-triage-producer-recipe.json").read_bytes() == recipe.read_bytes()
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["triage"] == {
        "decision_manifest_sha256": hashlib.sha256(triage.read_bytes()).hexdigest(),
        "producer_recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
    }


def test_upload_refuses_a_triage_manifest_that_misses_a_sealed_scan(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    triage, recipe = _triage_documents(tmp_path, source, skip="page-two.bin")
    store = LocalFixtureObjectStore(tmp_path / "volume")

    with pytest.raises(OperatorError) as refusal:
        surface.upload(
            source,
            sealed_manifest=manifest,
            target=store,
            triage_decision_manifest=triage,
            triage_producer_recipe=recipe,
        )

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    assert "has no row" in str(refusal.value.detail)
    assert store.puts == []


def test_upload_never_replaces_the_triage_documents_a_submission_was_sent_with(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    triage, recipe = _triage_documents(tmp_path, source)
    store = LocalFixtureObjectStore(tmp_path / "volume")
    arguments = dict(sealed_manifest=manifest, target=store, triage_producer_recipe=recipe)
    surface.upload(source, triage_decision_manifest=triage, **arguments)

    corrected = json.loads(triage.read_text())
    corrected["corpus_id"] = "renamed"
    for row in corrected["records"]:
        row["corpus_id"] = "renamed"
        row.pop("manifest_row_sha256")
        from common.contracts import triage as triage_contract

        row.update(triage_contract.make_row(**row))
    triage.write_bytes(canonical_bytes(corrected))

    with pytest.raises(OperatorError) as refusal:
        surface.upload(source, triage_decision_manifest=triage, **arguments)

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    assert "--prefix" in str(refusal.value.detail)


@pytest.mark.parametrize("problem", ["malformed recipe", "rows the recipe does not declare"])
def test_upload_refuses_a_producer_recipe_the_door_would_refuse(tmp_path: Path, problem) -> None:
    """The Door's own recipe checks run before anything is sent, so a pod is never
    started on triage the Door would refuse."""
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    triage, recipe = _triage_documents(
        tmp_path,
        source,
        actor={"kind": "producer", "identity": "operations.triage.producer", "revision": "r1"},
    )
    if problem == "malformed recipe":
        recipe.write_bytes(b'{"schema":"pagekit-producer-recipe.v1"}')
    store = LocalFixtureObjectStore(tmp_path / "volume")

    with pytest.raises(OperatorError) as refusal:
        surface.upload(
            source,
            sealed_manifest=manifest,
            target=store,
            triage_decision_manifest=triage,
            triage_producer_recipe=recipe,
        )

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    assert "producer recipe" in str(refusal.value.detail), refusal.value.detail
    assert store.puts == []


def test_upload_never_adds_triage_to_a_submission_already_marked_complete(
    tmp_path: Path,
) -> None:
    """A pod may already have run the completed submission without triage."""
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    triage, recipe = _triage_documents(tmp_path, source)
    store = LocalFixtureObjectStore(tmp_path / "volume")
    surface.upload(source, sealed_manifest=manifest, target=store)

    with pytest.raises(OperatorError) as refusal:
        surface.upload(
            source,
            sealed_manifest=manifest,
            target=store,
            triage_decision_manifest=triage,
            triage_producer_recipe=recipe,
        )

    assert refusal.value.code is ErrorCode.UPLOAD_REFUSED
    assert "--prefix" in str(refusal.value.detail)
    assert not (tmp_path / "volume" / "submission-triage-decision-manifest.json").exists()
