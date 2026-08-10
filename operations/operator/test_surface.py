"""End-to-end fake drills for the operator's words and their recovery states."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import tracemalloc
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pytest

from common.contracts.approval import build_approval_record
from common.contracts.canonical import canonical_bytes
from operations.pod.fake_provider import FakeProvider
from operations.pod.lease import LeaseStore
from operations.pod.models import PodCreateRequest, ProviderFailure, require_utc
from operations.pod.shutdown import CloseReport, VerifiedShutdown
from operations.pod.spend import load_spend_policy
from operations.submit import gate
from operations.submit.submit import build_manifest, walk_folder

from . import cli, entry
from .dry_run import make_transcript
from .errors import ErrorCode, OperatorError
from .fakes import LocalFixtureObjectStore
from .records import MAX_RECORD_BYTES, DescriptorStore, ReceiptStore, RecordError, sha256_file
from .surface import (
    OPERATOR_CLOSE_PREFIX,
    Faults,
    OperatorSurface,
    _pod_from_record,
    _pod_record,
    _request_from_record,
    _request_record,
)

UTC = timezone.utc
START = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _request(*, name: str = "operator-test") -> PodCreateRequest:
    return PodCreateRequest(
        name=name,
        gpu_type="fake-48gb",
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="fixture-volume",
        volume_mount_path="/workspace/private",
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "operations.pod.provider_runpod:timer_context_from_environment",
            "--bootstrap-command-json",
            '["python","-m","operations.pod.bootstrap"]',
            "--report-path",
            "/workspace/private/pod-runtime-report.json",
        ),
        hard_deadline=START + timedelta(seconds=900),
        repository_commit="b" * 40,
        template="fixture-template",
    )


def _spend_policy(tmp_path: Path, *, hourly: str = "1.00") -> Path:
    path = tmp_path / "reviewed-spend.toml"
    path.write_text(
        "\n".join(
            (
                'schema = "pod-spend.v1"',
                'state = "configured"',
                'currency = "USD"',
                f'max_hourly_usd = "{hourly}"',
                'max_estimated_metered_cost_usd = "2.00"',
                "hard_lifetime_seconds = 900",
                "laptop_heartbeat_timeout_seconds = 60",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 5",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def _manifest(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "submitted-pages"
    source.mkdir()
    (source / "page-one.bin").write_bytes(b"synthetic page one\n")
    (source / "page-two.bin").write_bytes(b"synthetic page two\n")
    manifest = tmp_path / "sealed-submission.json"
    record = build_manifest(
        walk_folder(source),
        authorized_by={
            "relative_path": "receipts/sha256/" + "a" * 64 + ".json",
            "sha256": "a" * 64,
        },
    )
    manifest.write_bytes(canonical_bytes(record))
    return source, manifest


def _approved_submission(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """An approved storage root, a matching gate policy, and its approval record.

    Every other upload test in this file starts from an already-sealed
    manifest; this is what `submit_and_upload` needs to seal a new one through
    Spec 03's door itself, mirroring `operations/submit/test_submit.py`'s own
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
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(
            build_approval_record(
                subject_ids=["data-handling-policy"],
                action="data-gate",
                reason="synthetic proving record; approves nothing",
                target_version_hash=gate.policy_hash(policy),
                timestamp="2026-08-04T12:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    manifest_out = approved / "submission.json"
    return source, manifest_out, approval_path, policy_path


class FastElapsedClock:
    """A drill's stopwatch: nothing sleeps, and every slept second still elapses.

    The shipped close deadline is the reviewed operational one, so a drill that
    watches a close give up would otherwise wait it out in real seconds. The
    speed-up belongs here, in the test's injected clock, and not in the surface.
    """

    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def _surface(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    faults: Faults | None = None,
    output: list[str] | None = None,
) -> OperatorSurface:
    messages = output if output is not None else []
    clock = FastElapsedClock()
    return OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        provider=provider or FakeProvider(now=lambda: START),
        now=lambda: START,
        present=messages.append,
        faults=faults,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )


def _launch(surface: OperatorSurface, spend: Path, *, name: str = "operator-test"):
    prepared = surface.prepare_launch(_request(name=name), policy_path=spend)
    return surface.launch(prepared, prepared.confirmation_phrase)


def _all_files(path: Path) -> dict[Path, bytes]:
    return {
        candidate.relative_to(path): candidate.read_bytes()
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file()
    }


class ConfirmationAwareProvider(FakeProvider):
    """A fake that fails at the paid seam when a confirmation receipt is absent."""

    def __init__(self) -> None:
        super().__init__(now=lambda: START)
        self.receipt_exists: Callable[[], bool] = lambda: False
        self.create_checked = False

    def create(self, request: PodCreateRequest):  # type: ignore[no-untyped-def]
        assert self.receipt_exists(), "create reached the provider without a saved confirmation"
        self.create_checked = True
        return super().create(request)


def test_launch_does_not_reach_paid_fake_without_a_saved_confirmation(tmp_path: Path) -> None:
    provider = ConfirmationAwareProvider()
    messages: list[str] = []
    surface = _surface(tmp_path, provider=provider, output=messages)
    provider.receipt_exists = lambda: surface._descriptor_receipt("launch-confirmation") is not None
    prepared = surface.prepare_launch(_request(), policy_path=_spend_policy(tmp_path))

    with pytest.raises(OperatorError) as refusal:
        surface.launch(prepared, "not the required words")

    assert refusal.value.code is ErrorCode.CONFIRMATION_REQUIRED
    assert not provider.create_checked
    assert not any(verb == "create" for verb, _ in provider.calls)
    assert surface._descriptor_receipt("launch-confirmation") is None

    result = surface.launch(prepared, prepared.confirmation_phrase)

    assert result.green
    assert provider.create_checked
    receipt = surface.receipts.read(surface._descriptor_receipt("launch-confirmation"))
    assert receipt["payload"]["preview"]["spend"]["allowed"] is True


def test_adoption_rechecks_only_after_its_confirmation_receipt(tmp_path: Path) -> None:
    class AdoptionProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(now=lambda: START)
            self.receipt_exists: Callable[[], bool] = lambda: False
            self.saw_post_confirmation_adopt = False

        def adopt(self, pod_id: str):  # type: ignore[no-untyped-def]
            if self.receipt_exists():
                self.saw_post_confirmation_adopt = True
            return super().adopt(pod_id)

    provider = AdoptionProvider()
    existing = provider.create(_request(name="already-there"))
    provider.calls.clear()
    surface = _surface(tmp_path, provider=provider)
    provider.receipt_exists = lambda: surface._descriptor_receipt("launch-confirmation") is not None
    prepared = surface.prepare_launch(
        _request(name="already-there"),
        policy_path=_spend_policy(tmp_path),
        adopt_pod_id=existing.pod_id,
    )

    with pytest.raises(OperatorError):
        surface.launch(prepared, "no")

    assert not provider.saw_post_confirmation_adopt
    result = surface.launch(prepared, prepared.confirmation_phrase)

    assert result.green
    assert provider.saw_post_confirmation_adopt
    launch = surface.receipts.read(surface._descriptor_receipt("launch"))["payload"]
    assert "adopted" in launch["summary"]


def test_active_fixture_cannot_be_adopted_again_or_hidden_by_a_new_paid_path(
    tmp_path: Path,
) -> None:
    first = _surface(tmp_path)
    spend = _spend_policy(tmp_path)
    launched = _launch(first, spend)
    assert launched.record is not None

    with pytest.raises(OperatorError) as refusal:
        first.prepare_launch(
            _request(),
            policy_path=spend,
            adopt_pod_id=launched.record.pod_id,
        )

    assert refusal.value.code is ErrorCode.ACTIVE_POD_REQUIRES_CLOSE
    assert not any(verb == "adopt" for verb, _ in first.provider.calls)

    prepared_close = first.prepare_close()
    assert first.close(prepared_close, prepared_close.phrase).verified
    assert first.prepare_launch(_request(name="next-fixture"), policy_path=spend).result.preview


def test_two_overlapping_prepared_launches_cannot_both_be_confirmed_into_real_pods(
    tmp_path: Path,
) -> None:
    """Two double-clicks, or two terminal windows, before either types a confirmation.

    Both `prepare_launch` calls see no active pod yet and pass; only the *second*
    `launch` call is where this must be caught, because that is the only point
    where a second real pod would otherwise be created that `status`/`close` can
    never reach again (no `--pod-id` recorded for it, and the descriptor's single
    active-launch pointer only ever names the most recent one).
    """

    surface = _surface(tmp_path)
    spend = _spend_policy(tmp_path)
    first_prepared = surface.prepare_launch(_request(name="window-a"), policy_path=spend)
    second_prepared = surface.prepare_launch(_request(name="window-b"), policy_path=spend)

    first_result = surface.launch(first_prepared, first_prepared.confirmation_phrase)
    assert first_result.green

    with pytest.raises(OperatorError) as refusal:
        surface.launch(second_prepared, second_prepared.confirmation_phrase)

    assert refusal.value.code is ErrorCode.ACTIVE_POD_REQUIRES_CLOSE
    assert not any(verb == "create" and name == "window-b" for verb, name in surface.provider.calls)


def test_saved_request_and_pod_reconstruction_refuse_type_coercion(tmp_path: Path) -> None:
    """The receipt reader must construct the same identities as the live path."""

    request_record = _request_record(_request())
    request_record["name"] = 7
    with pytest.raises(OperatorError):
        _request_from_record(request_record)

    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None
    pod_record = _pod_record(launched.record)
    pod_record["runtime_contract"]["interruptible"] = 0
    with pytest.raises(OperatorError):
        _pod_from_record(pod_record)


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
    assert loaded["actions"]["boot"] == str(first)
    assert loaded["history"]["boot"][-1] == str(first)
    assert loaded["history"]["boot"].count(str(first)) == 1

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
    verb exists to show (GOVERNANCE 2).
    """

    surface = _surface(tmp_path)
    surface.boot()
    receipt = surface._descriptor_receipt("boot")
    assert receipt is not None

    record = json.loads(receipt.read_text(encoding="utf-8"))
    record["payload"] = {"summary": "hand-edited", "amount": 1.5}
    data = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    replacement = receipt.with_name(f"boot-{hashlib.sha256(data).hexdigest()}.json")
    receipt.unlink()
    replacement.write_bytes(data)
    surface.descriptor.record("boot", replacement)

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
    surface.boot()
    descriptor = surface.descriptor.path
    raw = json.loads(descriptor.read_text(encoding="utf-8"))
    raw["actions"]["boot"] = 1.5
    descriptor.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE


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
    failure — and an out-of-memory kill prints nothing at all (GOVERNANCE 2).
    """

    surface = _surface(tmp_path)
    surface.boot()
    receipt = surface._descriptor_receipt("boot")
    assert receipt is not None
    receipt.write_bytes(b"{" + b"x" * (MAX_RECORD_BYTES + 1))

    with pytest.raises(RecordError, match="larger than"):
        surface.receipts.read(receipt)

    surface.descriptor.path.write_bytes(b"{" + b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(RecordError, match="larger than"):
        surface.descriptor.load()


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
            store.put_file("volume/page.bin", source)
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


def test_submit_and_upload_seals_a_new_manifest_then_transfers_it(tmp_path: Path) -> None:
    """The `--manifest-out` route through Spec 03's door, end to end.

    Every other upload test in this file starts from an already-sealed
    manifest; nothing exercised the door itself — sealing a brand new one,
    then transferring it — until this test.
    """

    surface = _surface(tmp_path)
    source, manifest_out, approval_path, policy_path = _approved_submission(tmp_path)

    receipt = surface.submit_and_upload(
        source,
        manifest_out=manifest_out,
        approval_record=approval_path,
        policy_path=policy_path,
    )

    assert receipt.is_file()
    assert manifest_out.is_file()
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["state"] == "complete"


def test_submit_and_upload_refuses_by_name_when_the_door_refuses(tmp_path: Path) -> None:
    """A submission the gate refuses must reach `UPLOAD_REFUSED`, not a raw exception."""

    surface = _surface(tmp_path)
    source, manifest_out, _approval_path, policy_path = _approved_submission(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        surface.submit_and_upload(
            source,
            manifest_out=manifest_out,
            approval_record=tmp_path / "no-such-approval.json",
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

    tracemalloc.start()
    try:
        store.put_file("volume/pages.bin", source)
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


def test_a_secondary_failure_record_error_is_printed_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, faults=Faults(provider_timeout=True), output=messages)

    def refuse_index(self, action, receipt):  # type: ignore[no-untyped-def]
        del self, action, receipt
        raise RecordError("injected descriptor failure")

    monkeypatch.setattr(DescriptorStore, "record", refuse_index)
    with pytest.raises(OperatorError) as operational_failure:
        surface.prepare_launch(_request(), policy_path=_spend_policy(tmp_path))

    assert operational_failure.value.code is ErrorCode.PROVIDER_TIMEOUT
    assert any("Verbatus could not save the result" in line for line in messages)
    assert any("Receipt saved at" in line for line in messages)


def test_later_refused_launch_receipt_does_not_hide_the_active_pod_from_close(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    surface._record_failure("launch", "refused-ceiling", "injected later refusal")

    prepared_close = surface.prepare_close()
    report = surface.close(prepared_close, prepared_close.phrase)
    assert report.verified


def test_unknown_offline_adoption_warns_that_existing_billing_is_not_known(tmp_path: Path) -> None:
    surface = _surface(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        surface.prepare_launch(
            _request(),
            policy_path=_spend_policy(tmp_path),
            adopt_pod_id="fixture-pod-not-recorded-here",
        )

    assert refusal.value.code is ErrorCode.ADOPTION_REFUSED
    assert "may still be billing" in refusal.value.render()


def test_adoption_with_an_unreadable_spend_policy_keeps_the_existing_billing_warning(
    tmp_path: Path,
) -> None:
    surface = _surface(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        surface.prepare_launch(
            _request(),
            policy_path=tmp_path / "missing-reviewed-spend.toml",
            adopt_pod_id="fixture-pod-not-recorded-here",
        )

    assert refusal.value.code is ErrorCode.ADOPTION_REFUSED
    assert "may still be billing" in refusal.value.render()


def test_six_words_end_to_end_and_status_is_strictly_read_only(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    spend = _spend_policy(tmp_path)
    source, manifest = _manifest(tmp_path)

    launched = _launch(surface, spend)
    assert launched.record is not None
    boot_receipt = surface.boot()
    upload_receipt = surface.upload(source, sealed_manifest=manifest)
    outcome = surface.run(run_id="six-word-run")
    bundle = surface.export(run_id="six-word-run")
    prepared_close = surface.prepare_close()
    close = surface.close(prepared_close, prepared_close.phrase)

    assert boot_receipt.is_file()
    assert upload_receipt.is_file()
    assert outcome.state == "complete"
    assert bundle.is_file()
    assert close.verified
    launch_receipt = surface.receipts.read(surface._descriptor_receipt("launch"))["payload"]
    lease = LeaseStore(Path(str(launch_receipt["lease"]))).load()
    assert lease is not None and lease.phase == "closed-verified"
    assert any("page 1" in line and "page 2" in line for line in messages)
    assert any("act a1" in line and "act a2" in line for line in messages)
    assert any("I CONFIRM PAID POD" in line for line in messages)
    assert any("Charges captured through" in line for line in messages)
    assert any("ongoing price is $" in line for line in messages)
    assert any("does not delete the retained volume" in line for line in messages)
    assert not any("%" in line for line in messages)

    before = _all_files(surface.state_root)
    call_count = len(surface.provider.calls)
    status_lines = surface.status()
    after = _all_files(surface.state_root)

    assert before == after
    assert len(surface.provider.calls) == call_count
    assert any(line.startswith("- launch record") for line in status_lines)
    assert any(line.startswith("- export record") for line in status_lines)
    assert any("Saved boot report: GREEN." in line for line in status_lines)
    assert any("Saved charges captured through" in line for line in status_lines)
    assert any("Reconciliation from the recorded Armarium export:" in line for line in status_lines)
    assert any("Saved sealed submission record names 2 file(s)." in line for line in status_lines)
    assert not any("active-launch" in line for line in status_lines)


@pytest.mark.parametrize(
    ("faults", "expected"),
    (
        (Faults(provider_timeout=True), ErrorCode.PROVIDER_TIMEOUT),
        (Faults(provider_error=True), ErrorCode.PROVIDER_ERROR),
    ),
)
def test_provider_preview_faults_are_named_and_a_retry_is_safe(
    tmp_path: Path, faults: Faults, expected: ErrorCode
) -> None:
    surface = _surface(tmp_path, faults=faults)
    spend = _spend_policy(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        surface.prepare_launch(_request(), policy_path=spend)

    assert refusal.value.code is expected
    assert not any(verb == "create" for verb, _ in surface.provider.calls)
    prepared = surface.prepare_launch(_request(), policy_path=spend)
    assert prepared.result.preview is not None


def test_a_post_confirmation_provider_failure_is_named_launch_unresolved_not_retryable(
    tmp_path: Path,
) -> None:
    """The orphan-risk case: the provider accepts the paid call, then the

    client loses the response — after the typed confirmation, not before,
    unlike the preview faults above. This is the one state where a real pod
    could already be billing while the operator has no confirmation it
    exists, and it must never be reported as a plain, retryable failure.
    """

    surface = _surface(tmp_path)
    prepared = surface.prepare_launch(_request(), policy_path=_spend_policy(tmp_path))
    surface.provider.inject_post_create_failure(
        ProviderFailure("client died after the provider accepted")
    )

    with pytest.raises(OperatorError) as failure:
        surface.launch(prepared, prepared.confirmation_phrase)

    assert failure.value.code is ErrorCode.LAUNCH_UNRESOLVED
    rendered = failure.value.render().lower()
    assert "do not launch again" in rendered
    assert "a provider request may already have occurred" in rendered


def test_configured_ceiling_refusal_does_not_claim_the_policy_is_missing(tmp_path: Path) -> None:
    surface = _surface(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        surface.prepare_launch(_request(), policy_path=_spend_policy(tmp_path, hourly="0.10"))

    assert refusal.value.code is ErrorCode.PAID_ACTION_REFUSED
    assert "no reviewed spend limit" not in refusal.value.render().lower()


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


def test_red_boot_is_named_and_can_be_retried(tmp_path: Path) -> None:
    surface = _surface(tmp_path, faults=Faults(cache_failure=True))

    with pytest.raises(OperatorError) as refusal:
        surface.boot()

    assert refusal.value.code is ErrorCode.BOOT_RED
    red = surface.receipts.read(surface._descriptor_receipt("boot"))["payload"]
    assert red["report"]["color"] == "red"
    assert surface.boot().is_file()


def test_laptop_crash_leaves_resumable_pages_and_acts(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, faults=Faults(laptop_crash=True), output=messages)

    with pytest.raises(OperatorError) as interruption:
        surface.run(run_id="laptop-crash-run")

    assert interruption.value.code is ErrorCode.RUN_INTERRUPTED
    interrupted = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert interrupted["state"] == "interrupted-recoverable"
    resumed = surface.run(run_id="laptop-crash-run")

    assert resumed.state == "complete"
    assert any("Resuming run laptop-crash-run" in line for line in messages)
    assert any("page 1" in line for line in messages)
    assert any("act a1" in line for line in messages)


def test_failed_close_is_loud_then_can_be_rechecked(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, faults=Faults(failed_close=True), output=messages)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None
    phrase = f"{OPERATOR_CLOSE_PREFIX} {launched.record.pod_id}"

    with pytest.raises(OperatorError) as failure:
        surface.close(surface.prepare_close(), phrase)

    assert failure.value.code is ErrorCode.CLOSE_UNVERIFIED
    assert any("UNVERIFIED CLOSE" in line for line in messages)
    assert any("both saved checks" in line for line in messages)
    launch_receipt = surface.receipts.read(surface._descriptor_receipt("launch"))["payload"]
    lease = LeaseStore(Path(str(launch_receipt["lease"]))).load()
    assert lease is not None and lease.phase == "close-unverified"
    verified = surface.close(surface.prepare_close(), phrase)

    assert verified.verified
    reconciled = LeaseStore(Path(str(launch_receipt["lease"]))).load()
    assert reconciled is not None and reconciled.phase == "closed-verified"


def test_close_does_not_reach_destructive_fake_without_a_saved_confirmation(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    with pytest.raises(OperatorError) as refusal:
        surface.close(surface.prepare_close(), "not the required close words")

    assert refusal.value.code is ErrorCode.CLOSE_REFUSED
    assert surface.provider.terminate_calls == []
    assert surface._descriptor_receipt("close-confirmation") is None


def test_close_lease_record_failure_shows_captured_cutoff_and_retained_volume_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    def broken_record_close(self, *, owner_token, report, now):  # type: ignore[no-untyped-def]
        del self, owner_token, report, now
        raise OSError("injected lease write failure")

    monkeypatch.setattr(LeaseStore, "record_close", broken_record_close)
    prepared_close = surface.prepare_close()
    with pytest.raises(OperatorError) as failure:
        surface.close(prepared_close, prepared_close.phrase)

    assert failure.value.code is ErrorCode.CLOSE_LEASE_RECORD_FAILED
    assert any("Charges captured through" in line for line in messages)
    assert any("ongoing price is $" in line for line in messages)
    assert any("does not delete the retained volume" in line for line in messages)
    close = surface.receipts.read(surface._descriptor_receipt("close"))["payload"]
    assert close["lease_reconciled"] is False


def test_status_empty_has_the_same_plain_language_contract(tmp_path: Path) -> None:
    surface = _surface(tmp_path)

    with pytest.raises(OperatorError) as empty:
        surface.status()

    assert empty.value.code is ErrorCode.STATUS_EMPTY
    labels = ("What happened:", "What it means:", "Next step:")
    assert all(label in empty.value.render() for label in labels)


def test_surface_refuses_any_non_fake_provider_before_it_can_be_called(tmp_path: Path) -> None:
    with pytest.raises(OperatorError) as refusal:
        OperatorSurface(
            ROOT,
            tmp_path / "operator-state",
            provider=object(),  # type: ignore[arg-type]
            now=lambda: START,
        )

    assert refusal.value.code is ErrorCode.LIVE_PROVIDER_BLOCKED


def test_unknown_fake_failure_verb_cannot_make_a_drill_pass_without_failing() -> None:
    provider = FakeProvider(now=lambda: START)

    with pytest.raises(ValueError, match="unknown fake-provider failure verb"):
        provider.inject_failure("typo", RuntimeError("should not be silently queued"))


def _request_json(tmp_path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "name": "operator-test",
        "gpu_type": "fake-48gb",
        "image": "registry.example/verbatus@sha256:" + "a" * 64,
        "volume_id": "fixture-volume",
        "volume_mount_path": "/workspace/private",
        "docker_start_cmd": [
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "operations.pod.provider_runpod:timer_context_from_environment",
            "--bootstrap-command-json",
            '["python","-m","operations.pod.bootstrap"]',
            "--report-path",
            "/workspace/private/pod-runtime-report.json",
        ],
        "hard_deadline": "2026-08-09T12:15:00Z",
        "repository_commit": "b" * 40,
        "template": "fixture-template",
    }
    payload.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_request_reads_a_well_formed_reviewed_pod_request(tmp_path: Path) -> None:
    """The reader for the reviewed pod-request JSON — a money-path input — must
    parse a well-formed file, not only refuse a broken one.
    """

    request = cli.load_request(_request_json(tmp_path))

    assert request.name == "operator-test"
    assert request.gpu_type == "fake-48gb"
    assert request.docker_start_cmd[:3] == ("python", "-m", "operations.pod.pod_timer")
    assert require_utc(request.hard_deadline, "test hard deadline") == request.hard_deadline


def test_load_request_refuses_unreadable_json(tmp_path: Path) -> None:
    broken = tmp_path / "request.json"
    broken.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        cli.load_request(broken)

    assert refusal.value.code is ErrorCode.INVALID_COMMAND


def test_load_request_refuses_a_non_object_json_value(tmp_path: Path) -> None:
    array = tmp_path / "request.json"
    array.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        cli.load_request(array)

    assert refusal.value.code is ErrorCode.INVALID_COMMAND


def test_load_request_refuses_an_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(OperatorError) as refusal:
        cli.load_request(_request_json(tmp_path, unexpected_field="not allowed"))

    assert refusal.value.code is ErrorCode.INVALID_COMMAND


def test_load_request_refuses_an_incomplete_request(tmp_path: Path) -> None:
    """A missing required key must reach the plain-language contract, not a raw KeyError."""

    path = tmp_path / "request.json"
    payload = json.loads(_request_json(tmp_path).read_text(encoding="utf-8"))
    del payload["name"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        cli.load_request(path)

    assert refusal.value.code is ErrorCode.INVALID_COMMAND


def test_load_request_refuses_a_malformed_docker_start_cmd(tmp_path: Path) -> None:
    with pytest.raises(OperatorError) as refusal:
        cli.load_request(_request_json(tmp_path, docker_start_cmd="not-a-list"))

    assert refusal.value.code is ErrorCode.INVALID_COMMAND


def test_console_parser_never_prints_a_raw_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["launch"])
    captured = capsys.readouterr().out

    assert exit_code == 2
    assert "What happened:" in captured
    assert "What it means:" in captured
    assert "Next step:" in captured
    assert "Traceback" not in captured


def test_console_interrupt_never_prints_a_raw_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(_value):  # type: ignore[no-untyped-def]
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


def test_console_close_with_no_saved_pod_refuses_before_prompting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("close should not ask for a phrase without a saved pod"),
    )

    exit_code = cli.main(["--state-dir", str(tmp_path / "empty-state"), "close"])

    assert exit_code == 2
    captured = capsys.readouterr().out
    assert "What happened:" in captured
    assert "There is no recorded pod" in captured


def test_console_close_shows_its_notice_before_asking_for_the_confirmation_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close notice has to be read before the phrase is typed, not after —

    the same order `launch` already uses: show the price screen, then ask. The
    notice is where "the attached volume keeps its own ongoing price" is said,
    so asking first means asking someone who has not been told what it costs.
    """

    state = tmp_path / "operator-state"
    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    order: list[tuple[str, str]] = []

    def recording_print(*args: object, **kwargs: object) -> None:
        del kwargs
        order.append(("PRINT", " ".join(str(arg) for arg in args)))

    def recording_input(prompt: str = "") -> str:
        order.append(("INPUT", prompt))
        return f"CLOSE {launched.record.pod_id}"

    monkeypatch.setattr("builtins.print", recording_print)
    monkeypatch.setattr("builtins.input", recording_input)

    exit_code = cli.main(["--state-dir", str(state), "close"])

    # Both `cli.main` calls above run within the same second on the real wall
    # clock, so this is a same-process-speed close, not a same-process one:
    # the reconstruction path in `_provider_for_record` still runs, because
    # `close` is a fresh `OperatorSurface`/`FakeProvider` pair. See
    # `test_close_reconstruction_verifies_across_a_real_gap_between_processes`
    # for the case this shape exists to prove: reconstruction staying verified
    # even when real time has passed between the two processes.
    assert exit_code == 0
    notice_index = next(
        index
        for index, (kind, text) in enumerate(order)
        if kind == "PRINT" and "Close will remove fixture pod" in text
    )
    input_index = next(index for index, (kind, _) in enumerate(order) if kind == "INPUT")
    assert notice_index < input_index


def test_close_reconstruction_verifies_across_a_real_gap_between_processes(
    tmp_path: Path,
) -> None:
    """`close` in a fresh process, run well over an hour after `launch` in another.

    `_provider_for_record` recreates the fake pod once the launching process is
    gone. Before the fix, the recreated provider's clock was frozen at
    `record.created_at`, so `bill()`'s captured-cost cutoff never advanced past
    `created_at + 1h`, while `VerifiedShutdown` requested a cutoff at the real,
    later wall-clock close time. Every close past that one-hour mark reported
    UNVERIFIED regardless of how healthy the close actually was — this is the
    fresh-process false alarm the drive in the pre-pull-request audit found.
    """

    def _launch_then_close(name: str, gap: timedelta) -> CloseReport:
        state = tmp_path / f"operator-state-{name}"
        launch_time = START
        launch_clock = FastElapsedClock()
        launch_surface = OperatorSurface(
            ROOT,
            state,
            provider=FakeProvider(now=lambda: launch_time),
            now=lambda: launch_time,
            monotonic=launch_clock.monotonic,
            sleeper=launch_clock.sleep,
        )
        launched = _launch(launch_surface, _spend_policy(tmp_path), name=name)
        assert launched.record is not None

        close_time = launch_time + gap
        close_clock = FastElapsedClock()
        close_surface = OperatorSurface(
            ROOT,
            state,
            provider=FakeProvider(now=lambda: close_time),
            now=lambda: close_time,
            monotonic=close_clock.monotonic,
            sleeper=close_clock.sleep,
        )
        prepared_close = close_surface.prepare_close()
        return close_surface.close(prepared_close, prepared_close.phrase)

    short_gap = _launch_then_close("short-gap", timedelta(minutes=15))
    long_gap = _launch_then_close("long-gap", timedelta(hours=2))

    assert short_gap.verified, short_gap.state
    assert long_gap.verified, long_gap.state


def test_a_corrupted_launch_descriptor_never_claims_close_has_nothing_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A torn/corrupted local index must not read as 'nothing to close, this is
    safe' — the recorded pod may still exist and still be billing, and the
    operator must not be told the opposite of that.
    """

    state = tmp_path / "operator-state"
    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    descriptor_path = state / "operator-surface.json"
    corrupted = json.loads(descriptor_path.read_text(encoding="utf-8"))
    corrupted["self_hash"] = "0" * 64
    descriptor_path.write_text(json.dumps(corrupted), encoding="utf-8")

    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": pytest.fail("close should not prompt when its own record is unreadable"),
    )

    exit_code = cli.main(["--state-dir", str(state), "close"])

    assert exit_code == 2
    captured = capsys.readouterr().out
    # CLOSE_NOTHING's own copy — "there is nothing to close, this is safe" — is
    # exactly the false reassurance a corrupted index must never produce, because
    # the recorded pod may still exist and still be billing.
    assert "There is no recorded pod" not in captured
    assert "No close request was sent and nothing changed" not in captured


def test_refuse_if_active_pod_names_the_specific_reason_it_could_not_check_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked launch must say *which* record or lease failed, and why.

    Before this fix every failure while checking the recorded active pod became
    the same fixed sentence, and an operator blocked from launching could not
    tell a broken lease from a broken receipt without a developer reading the
    code alongside them.
    """

    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    def broken_load(self):  # type: ignore[no-untyped-def]
        del self
        raise OSError("injected lease read failure for this test")

    monkeypatch.setattr(LeaseStore, "load", broken_load)

    with pytest.raises(OperatorError) as failure:
        surface.prepare_launch(_request(name="second"), policy_path=_spend_policy(tmp_path))

    assert failure.value.code is ErrorCode.SAFETY_CHECK_FAILED
    assert "injected lease read failure for this test" in failure.value.render()


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


def test_a_held_run_raises_run_held_not_run_failed(
    tmp_path: Path,
) -> None:
    """A hold is the pipeline asking a person to decide, not a failure.

    Before this fix a legitimately held run raised `RUN_FAILED`, whose copy
    says the run "could not reach its recorded end state" - untrue of a hold,
    whose whole point is that a person decides what happens next, not that the
    run failed to reach one.
    """

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


def test_re_exporting_a_run_after_the_tree_changed_does_not_overwrite_the_first_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier export's receipt names its bundle's path *and* its exact digest.

    Before this fix the bundle path was named by run id alone, so exporting
    the same run twice — after anything in the run tree changed in between —
    silently overwrote the first bundle's bytes at the path the first,
    immutable receipt still vouches for.
    """

    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None
    surface.run(run_id="re-export-run")

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


def test_console_entry_renders_an_application_import_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_application():  # type: ignore[no-untyped-def]
        raise RuntimeError("Traceback: missing fixture application")

    monkeypatch.setattr(entry, "_load_application", broken_application)

    assert entry.main(["status"]) == 2
    captured = capsys.readouterr().out
    assert "What happened:" in captured
    assert "What it means:" in captured
    assert "Next step:" in captured
    assert "Traceback" not in captured


def test_interactive_launch_requests_both_the_reviewed_request_and_spend_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("launch", "/reviewed/request.json", "/reviewed/spend.toml", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    arguments = cli._interactive_arguments()

    assert arguments == [
        "launch",
        "--request",
        "/reviewed/request.json",
        "--spend",
        "/reviewed/spend.toml",
    ]


def test_interactive_launch_can_name_an_exact_recorded_fixture_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("launch", "/reviewed/request.json", "/reviewed/spend.toml", "fake-pod-4"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "launch",
        "--request",
        "/reviewed/request.json",
        "--spend",
        "/reviewed/spend.toml",
        "--adopt-pod",
        "fake-pod-4",
    ]


def test_mac_wrapper_starts_the_same_console_flow() -> None:
    wrapper = ROOT / "operations" / "operator" / "Verbatus.command"
    assert ".venv/bin/python" in wrapper.read_text(encoding="utf-8")
    completed = subprocess.run(
        [str(wrapper), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "verbatus" in completed.stdout.lower()


def test_scripted_dry_run_is_a_readable_six_word_acceptance_artifact(tmp_path: Path) -> None:
    transcript = make_transcript(tmp_path / "operator-dry-run.txt").read_text(encoding="utf-8")

    for heading in (
        "1. launch",
        "2. boot",
        "3. upload",
        "4. run",
        "5. export",
        "6. close",
    ):
        assert heading in transcript
    assert "What happened:" in transcript
    assert "I CONFIRM PAID POD" in transcript
    assert "Charges captured through" in transcript
    assert "zero GPU-hours" in transcript


def test_status_repeats_the_recorded_values_exactly_and_never_recomputes_them(
    tmp_path: Path,
) -> None:
    """Spec 12's "`status` agrees with receipts byte-for-byte".

    Not a paraphrase of the receipt and not a fresh calculation: every figure
    `status` shows has to be the exact string the receipt stored, or the honesty
    ledger the operator reads is a second opinion rather than the record.
    """

    surface = _surface(tmp_path)
    spend = _spend_policy(tmp_path)
    source, manifest = _manifest(tmp_path)
    launched = _launch(surface, spend)
    assert launched.record is not None
    surface.boot()
    surface.upload(source, sealed_manifest=manifest)
    surface.run(run_id="byte-for-byte-run")
    surface.export(run_id="byte-for-byte-run")
    prepared_close = surface.prepare_close()
    surface.close(prepared_close, prepared_close.phrase)

    lines = surface.status()
    joined = "\n".join(lines)

    close = surface.receipts.read(surface._descriptor_receipt("close"))["payload"]
    report = close["close_report"]
    cost = report["cost_capture"]
    assert cost["total_usd"] in joined
    assert cost["cutoff_at"] in joined
    assert report["volume"]["ongoing_hourly_usd"] in joined

    boot = surface.receipts.read(surface._descriptor_receipt("boot"))["payload"]
    assert boot["report"]["color"].upper() in joined

    run = surface.receipts.read(surface._descriptor_receipt("run"))["payload"]
    assert f"  Saved run state: {run['state']}." in lines

    export = surface.receipts.read(surface._descriptor_receipt("export"))["payload"]
    for row in export["reconciliation"]:
        assert f"  {row}" in lines

    for action in ("boot", "upload", "run", "export", "close"):
        payload = surface.receipts.read(surface._descriptor_receipt(action))["payload"]
        assert payload["summary"] in joined


def test_status_names_a_manifest_that_no_longer_matches_the_upload_receipt(
    tmp_path: Path,
) -> None:
    """A path is not identity: status must not treat different bytes at the
    recorded path as the manifest that was actually uploaded.

    But the sealed manifest lives outside `.verbatus/`, at a path this tool
    does not own — re-sealing a later batch to the same filename is ordinary
    drift, not a corrupted *operator* record, so it must not swallow the
    intact, still-correct upload receipt behind `STATUS_UNREADABLE`. Before
    this fix it did: this exact scenario raised `STATUS_UNREADABLE`
    permanently and printed the upload record twice, once correctly and once
    as "UNREADABLE".
    """

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    surface.upload(source, sealed_manifest=manifest)
    receipt = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]

    (source / "later-page.bin").write_bytes(b"not in the uploaded manifest\n")
    replacement = build_manifest(
        walk_folder(source),
        authorized_by={
            "relative_path": "receipts/sha256/" + "b" * 64 + ".json",
            "sha256": "b" * 64,
        },
    )
    manifest.write_bytes(canonical_bytes(replacement))

    lines = surface.status()

    assert (
        receipt["submission_manifest_sha256"] != hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    joined = "\n".join(lines)
    assert "no longer matches what the upload receipt recorded" in joined
    assert "upload receipt itself still stands" in joined
    upload_lines = [line for line in lines if line.startswith("- upload record 1:")]
    assert len(upload_lines) == 1
    assert "UNREADABLE" not in upload_lines[0]


def test_deleting_the_sealed_manifest_after_upload_does_not_permanently_break_status(
    tmp_path: Path,
) -> None:
    """Deleting a temporary sealed manifest, or re-sealing over it, is ordinary
    housekeeping outside `.verbatus/` — a routine act, not record corruption.

    Before this fix, `status` — the verb the README sells as "the one you can
    run any time" — raised `STATUS_UNREADABLE` permanently the moment this
    happened, blamed the intact upload receipt by name, and had no recorded
    way to clear it.
    """

    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    surface.upload(source, sealed_manifest=manifest)

    manifest.unlink()

    lines = surface.status()

    joined = "\n".join(lines)
    assert "is no longer present" in joined
    assert "upload receipt itself still stands" in joined
    upload_lines = [line for line in lines if line.startswith("- upload record 1:")]
    assert len(upload_lines) == 1
    assert "UNREADABLE" not in upload_lines[0]

    # Running status again afterward must behave exactly the same way, not
    # escalate or leave a lingering failure state anywhere.
    again = surface.status()
    assert again == lines


def test_one_unreadable_status_record_does_not_hide_the_intact_ledgers(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source, manifest = _manifest(tmp_path)
    surface.boot()
    surface.upload(source, sealed_manifest=manifest)
    upload_receipt = surface._descriptor_receipt("upload")
    assert upload_receipt is not None
    upload_receipt.write_text("not a receipt\n", encoding="utf-8")

    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE
    assert any("Saved boot report: GREEN" in line for line in messages)
    assert any("upload record 1: UNREADABLE" in line for line in messages)


def test_close_timing_comes_from_the_reviewed_policy_not_from_a_constant(tmp_path: Path) -> None:
    """A close that gives up in milliseconds reports UNVERIFIED every single time.

    That is loud and wrong, and it is the fastest way to teach an operator to
    ignore the one message in this tool they must never ignore.
    """

    surface = _surface(tmp_path)
    policy = load_spend_policy(_spend_policy(tmp_path))
    configured = surface._shutdown(policy)

    assert configured.timeout_seconds == policy.shutdown_deadline_seconds
    assert configured.poll_seconds == policy.shutdown_poll_interval_seconds

    # No reviewed policy: close still runs — it is always the safe direction —
    # and falls back to the runtime's own operational defaults, not to a
    # test-speed constant.
    fallback = surface._shutdown(None)
    assert fallback.timeout_seconds >= 1
    assert fallback.poll_seconds >= 1
    assert (fallback.timeout_seconds, fallback.poll_seconds) == (
        VerifiedShutdown(surface.provider).timeout_seconds,
        VerifiedShutdown(surface.provider).poll_seconds,
    )


def test_an_unreadable_spend_policy_never_stops_a_close(tmp_path: Path) -> None:
    """A close must not depend on `config/spend.toml` even being parseable.

    A prior version of this test pointed `workspace` at the real repository
    root, where the shipped `config/spend.toml` is a perfectly readable
    `state = "unconfigured"` file — it drove `policy.configured is False`, not
    `_close_policy`'s `except Exception: return None` branch, so the one line
    this test is named for was never actually executed by it. Give the
    surface its own workspace with a genuinely malformed policy file instead.
    """

    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "spend.toml").write_text("this is not valid toml [[[", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - proves the file really is unreadable
        load_spend_policy(workspace / "config" / "spend.toml")

    clock = FastElapsedClock()
    surface = OperatorSurface(
        workspace,
        tmp_path / "operator-state",
        provider=FakeProvider(now=lambda: START),
        now=lambda: START,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None

    prepared_close = surface.prepare_close()
    report = surface.close(prepared_close, prepared_close.phrase)

    assert report.verified


def test_a_price_that_moves_after_the_screen_is_named_a_price_change(tmp_path: Path) -> None:
    """The operator typed a phrase built from prices they read. If the provider's
    price has moved by the time the paid call happens, they are told that — not
    that they typed the confirmation wrongly, which is what a re-derived phrase
    alone would have said.
    """

    surface = _surface(tmp_path)
    prepared = surface.prepare_launch(_request(), policy_path=_spend_policy(tmp_path))
    surface.provider.price_sheet["fake-48gb"] = (Decimal("0.90"), Decimal("0.05"))

    with pytest.raises(OperatorError) as refusal:
        surface.launch(prepared, prepared.confirmation_phrase)

    assert refusal.value.code is ErrorCode.PRICE_CHANGED
    assert "was not used to authorize a different price" in refusal.value.render()
    # The confirmation the operator did give is recorded, and no pod was created.
    saved = surface.receipts.read(surface._descriptor_receipt("launch-confirmation"))["payload"]
    assert saved["preview"]["spend"]["pod_hourly_usd"] == "0.77"
    assert not any(verb == "create" for verb, _ in surface.provider.calls)
