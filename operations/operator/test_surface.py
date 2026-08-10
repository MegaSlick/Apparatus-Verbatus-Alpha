"""End-to-end fake drills for the operator's words and their recovery states."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pytest

from common.contracts.canonical import canonical_bytes
from operations.pod.fake_provider import FakeProvider
from operations.pod.lease import LeaseStore
from operations.pod.models import PodCreateRequest
from operations.pod.shutdown import VerifiedShutdown
from operations.pod.spend import load_spend_policy
from operations.submit.submit import build_manifest, walk_folder

from . import cli, entry
from .dry_run import make_transcript
from .errors import ErrorCode, OperatorError
from .records import DescriptorStore, ReceiptStore, RecordError
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

    surface.descriptor.record("boot", first)  # the repeat that used to corrupt history

    loaded = surface.descriptor.load()
    assert loaded is not None
    assert loaded["actions"]["boot"] == str(first)
    assert loaded["history"]["boot"][-1] == str(first)
    assert loaded["history"]["boot"].count(str(first)) == 1

    # And the descriptor must still be readable and writable afterward.
    third = surface.receipts.write("boot", {"summary": "third"})
    surface.descriptor.record("boot", third)
    assert surface.descriptor.load() is not None


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

    the same order `launch` already uses (show the price screen, then ask). A
    prior version of this dispatch collected the typed phrase first and only
    printed the notice — including "the attached volume keeps its own ongoing
    price" — once `surface.close()` was already running.
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

    cli.main(["--state-dir", str(state), "close"])

    # Not asserted: the exit code. Closing from a freshly reconstructed process
    # (a real second `verbatus close` invocation, which is what this drives) can
    # legitimately end CLOSE_UNVERIFIED rather than verified — a separate,
    # pre-existing property of that reconstruction path. What this test is for
    # is the order below.
    notice_index = next(
        index
        for index, (kind, text) in enumerate(order)
        if kind == "PRINT" and "Close will remove fixture pod" in text
    )
    input_index = next(index for index, (kind, _) in enumerate(order) if kind == "INPUT")
    assert notice_index < input_index


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


def test_status_refuses_a_different_valid_manifest_at_the_recorded_path(tmp_path: Path) -> None:
    """A path is not identity: status must stay bound to the manifest uploaded."""

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

    with pytest.raises(OperatorError) as refusal:
        surface.status()

    assert refusal.value.code is ErrorCode.STATUS_UNREADABLE
    assert (
        receipt["submission_manifest_sha256"] != hashlib.sha256(manifest.read_bytes()).hexdigest()
    )


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
    surface = _surface(tmp_path)
    launched = _launch(surface, _spend_policy(tmp_path))
    assert launched.record is not None
    assert (surface.workspace / "config" / "spend.toml").exists()  # the shipped one is unconfigured

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
