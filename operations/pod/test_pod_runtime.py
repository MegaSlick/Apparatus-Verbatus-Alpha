"""Fake-first acceptance drills for the money path.

These tests deliberately break each load-bearing guard.  A passing happy path
without the paired red path would not establish that the guard is wired.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml

from . import cli
from .arming import ControllerArming, ControllerReadiness
from .bootstrap import (
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapStep,
    BootstrapStepFailure,
    SubprocessBootstrapActions,
)
from .controllers import ControllerResult, ControllerState, LaptopSupervisor, PodDeadmanTimer
from .fake_provider import FakeProvider
from .launch import LaunchState, PodRuntime
from .lease import LeaseStore, PodLease
from .models import (
    AbsenceObservation,
    BillingState,
    CloseState,
    CostCapture,
    CostLine,
    LeaseFormatError,
    PodCreateRequest,
    PodRecord,
    ProviderStatus,
)
from .pod_timer import TimerContext, run_with_bootstrap
from .preflight import (
    CacheMismatch,
    GpuProfile,
    PlacementRefusal,
    PlacementTable,
    PreflightRunner,
    SmokeResult,
    SystemGpuProbe,
    UtilizationSample,
    load_placement_table,
)
from .shutdown import VerifiedShutdown
from .spend import CONFIRMATION_PREFIX, SpendPolicy, confirmation_phrase

UTC = timezone.utc
START = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

# The fake price sheet's `fake-48gb` row, and the one name `request()` builds.
# The typed phrase is derived from the displayed price, so these tests spell it
# out from the same inputs the preview would rather than reuse a constant.
POD_HOURLY = Decimal("0.77")
VOLUME_HOURLY = Decimal("0.05")
CREATE_CONFIRMATION = confirmation_phrase("create", "pod-runtime-test", POD_HOURLY, VOLUME_HOURLY)


def adopt_confirmation(pod_id: str) -> str:
    return confirmation_phrase("adopt", pod_id, POD_HOURLY, VOLUME_HOURLY)


@dataclass
class Clock:
    seconds: float = 0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


class LaunchedFakeControllers:
    """A fake launch handshake holding the two independently killable controllers."""

    def __init__(
        self,
        *,
        provider: FakeProvider,
        clock: Clock,
        store: LeaseStore,
        lease: PodLease,
        owner: str,
        heartbeat_timeout: timedelta,
    ) -> None:
        self.provider = provider
        self.clock = clock
        self.store = store
        self.lease = lease
        self.owner = owner
        close = shutdown(self.provider, self.clock)
        self.laptop_supervisor = LaptopSupervisor(
            self.store,
            close,
            owner_token=self.owner,
            heartbeat_timeout=heartbeat_timeout,
            now=self.clock.now,
        )
        self.pod_timer = PodDeadmanTimer(self.lease, close, now=self.clock.now)

    def kill_laptop(self) -> ControllerResult:
        """The already-armed pod timer remains after laptop-controller death."""

        return self.pod_timer.run_once()

    def kill_timer(self) -> ControllerResult:
        """The already-armed laptop supervisor remains after pod-timer death."""

        return self.laptop_supervisor.run_once()


class FakeControllerArmer:
    """An injected fake handshake; no fake provider endpoint is involved."""

    def __init__(
        self, clock: Clock, provider: FakeProvider, *, arm: bool = True, preflight: bool = True
    ) -> None:
        self.clock = clock
        self.provider = provider
        self.arm_result = arm
        self.preflight_result = preflight
        self.handshakes: list[LaunchedFakeControllers] = []

    def preflight(
        self, *, action: str, request: PodCreateRequest, policy: SpendPolicy
    ) -> ControllerReadiness:
        return ControllerReadiness(
            self.preflight_result,
            self.clock.now(),
            "fake controller arming procedure is available"
            if self.preflight_result
            else "injected arming preflight refusal",
            {
                "action": action,
                "phase": "preflight",
                "heartbeat_timeout_seconds": policy.laptop_heartbeat_timeout_seconds,
            },
        )

    def arm(self, *, action, request, record, lease, store, owner_token, policy):  # type: ignore[no-untyped-def]
        if self.arm_result:
            self.handshakes.append(
                LaunchedFakeControllers(
                    provider=self.provider,
                    clock=self.clock,
                    store=store,
                    lease=lease,
                    owner=owner_token,
                    heartbeat_timeout=timedelta(
                        seconds=policy.laptop_heartbeat_timeout_seconds or 1
                    ),
                )
            )
        return ControllerArming(
            self.arm_result,
            self.arm_result,
            self.clock.now(),
            "fake laptop supervisor started and fake pod timer acknowledged"
            if self.arm_result
            else "injected post-create controller arming failure",
            {
                "lease_id": lease.lease_id,
                "pod_id": record.pod_id,
                "hard_deadline": request.hard_deadline.isoformat().replace("+00:00", "Z"),
                "laptop_supervisor": {
                    "identity": f"fake-laptop-supervisor-{lease.generation}",
                    "started_at": self.clock.now().isoformat().replace("+00:00", "Z"),
                },
                "pod_timer": {
                    "report_path": request.docker_start_cmd[
                        request.docker_start_cmd.index("--report-path") + 1
                    ],
                    "acknowledged_at": self.clock.now().isoformat().replace("+00:00", "Z"),
                },
            },
        )


def policy(*, hourly: str = "1.00", lifetime: int = 3600) -> SpendPolicy:
    return SpendPolicy(
        state="configured",
        max_hourly_usd=Decimal(hourly),
        max_estimated_metered_cost_usd=Decimal("2.00"),
        hard_lifetime_seconds=lifetime,
        laptop_heartbeat_timeout_seconds=30,
        shutdown_poll_interval_seconds=1,
        shutdown_deadline_seconds=5,
    )


def request(clock: Clock, *, gpu: str = "fake-48gb", lifetime: int = 300) -> PodCreateRequest:
    return PodCreateRequest(
        name="pod-runtime-test",
        gpu_type=gpu,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        template="pinned-template",
        volume_id="test-volume",
        volume_mount_path="/workspace/private",
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "operations.pod.provider_runpod:timer_context_from_environment",
            "--bootstrap-command-json",
            # PLACEHOLDER: bootstrap.py is a library module with no __main__, so
            # this exits 0 immediately.  Tests rely on that to drill the
            # completed-early close path; it is not a template for a real
            # request file, which needs a long-running bootstrap/service entrypoint.
            '["python","-m","operations.pod.bootstrap"]',
            "--report-path",
            "/workspace/private/pod-runtime-report.json",
        ),
        hard_deadline=clock.now() + timedelta(seconds=lifetime),
        repository_commit="b" * 40,
    )


def fake(clock: Clock) -> FakeProvider:
    return FakeProvider({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)


class WrongAbsenceEvidenceFake(FakeProvider):
    """A deliberately faulty adapter that labels one close observation wrongly."""

    def __init__(self, clock: Clock, *, wrong_observation: str) -> None:
        super().__init__({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
        if wrong_observation not in {"status", "list", "status-200", "status-none"}:
            raise ValueError("wrong observation must be status, list, status-200, or status-none")
        self.wrong_observation = wrong_observation

    def status(self, pod_id: str) -> ProviderStatus:
        observation = super().status(pod_id)
        if self.wrong_observation == "status":
            return ProviderStatus(
                "another-pod",
                observation.presence,
                observation.observed_at,
                "deliberately mismatched fake status identity",
                observation.http_status,
            )
        if self.wrong_observation == "status-200":
            return ProviderStatus(
                pod_id,
                observation.presence,
                observation.observed_at,
                "deliberately false fake absence with HTTP 200",
                200,
            )
        if self.wrong_observation == "status-none":
            return ProviderStatus(
                pod_id,
                observation.presence,
                observation.observed_at,
                "deliberately false fake absence without an HTTP status",
                None,
            )
        return observation

    def verify_absent(self, pod_id: str) -> AbsenceObservation:
        observation = super().verify_absent(pod_id)
        if self.wrong_observation != "list":
            return observation
        return AbsenceObservation(
            "another-pod",
            observation.presence,
            observation.observed_at,
            "deliberately mismatched fake list identity",
        )


class WrongBillingEvidenceFake(FakeProvider):
    """A deliberately faulty adapter that narrows or mislabels billing evidence."""

    def __init__(self, clock: Clock, *, defect: str) -> None:
        super().__init__({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
        if defect not in {"pod", "cutoff", "window"}:
            raise ValueError("billing defect must be pod, cutoff, or window")
        self.defect = defect

    def capture_cost(self, pod_id: str, started_at: datetime, cutoff_at: datetime) -> CostCapture:
        self._call("capture_cost", pod_id)
        capture_pod_id = "another-pod" if self.defect == "pod" else pod_id
        capture_cutoff = (
            cutoff_at - timedelta(seconds=1)
            if self.defect == "cutoff"
            else cutoff_at + timedelta(seconds=2)
        )
        window_start = (
            cutoff_at - timedelta(seconds=2)
            if self.defect == "cutoff"
            else started_at + timedelta(seconds=1)
            if self.defect == "window"
            else started_at
        )
        return CostCapture(
            pod_id=capture_pod_id,
            state=BillingState.CAPTURED,
            cutoff_at=capture_cutoff,
            lines=(CostLine(Decimal("0.13"), "deliberately faulty fake billing"),),
            source="deliberately faulty fake billing",
            window_start_at=window_start,
        )


def shutdown(provider: FakeProvider, clock: Clock, *, timeout: float = 8) -> VerifiedShutdown:
    return VerifiedShutdown(
        provider,
        timeout_seconds=timeout,
        poll_seconds=1,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )


def runtime(
    provider: FakeProvider,
    clock: Clock,
    root: Path,
    *,
    spend: SpendPolicy | None = None,
    armer: FakeControllerArmer | None = None,
) -> PodRuntime:
    tokens = iter(("a" * 32, "b" * 32))
    return PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=spend or policy(),
        lease_root=root,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        controller_armer=armer or FakeControllerArmer(clock, provider),
    )


def test_create_and_adopt_share_the_price_ceiling_and_typed_confirmation_gate(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    pod_runtime = runtime(provider, clock, tmp_path)
    create_request = request(clock)

    preview = pod_runtime.preview_create(create_request)
    assert preview.state is LaunchState.PREVIEW
    assert preview.preview is not None
    assert preview.preview.assessment.estimate.pod_hourly_usd == Decimal("0.77")
    assert preview.preview.assessment.estimate.volume_hourly_usd == Decimal("0.05")
    assert preview.preview.confirmation_phrase == CREATE_CONFIRMATION
    assert preview.preview.assessment.to_record()["price_source"] == "fake price sheet"

    refused_create = pod_runtime.create(create_request, confirmation=None)
    assert refused_create.state is LaunchState.REFUSED_CONFIRMATION
    assert not any(verb == "create" for verb, _ in provider.calls)

    existing = provider.create(create_request)
    refused_adopt = pod_runtime.adopt(
        existing.pod_id,
        expected=create_request,
        confirmation="wrong words",
    )
    assert refused_adopt.state is LaunchState.REFUSED_CONFIRMATION
    assert refused_adopt.preview is not None
    assert refused_adopt.preview.confirmation_phrase == adopt_confirmation(existing.pod_id)
    assert list(tmp_path.glob("*.json")) == []


def test_a_price_move_between_preview_and_confirmation_names_itself(tmp_path: Path) -> None:
    """create() re-assesses the price internally, so a moved price must not

    read like a typo'd confirmation (audit-d Finding 12).
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = runtime(provider, clock, tmp_path)
    create_request = request(clock)

    preview = pod_runtime.preview_create(create_request)
    assert preview.state is LaunchState.PREVIEW
    assert preview.preview is not None
    shown_phrase = preview.preview.confirmation_phrase

    provider.price_sheet["fake-48gb"] = (Decimal("0.90"), Decimal("0.05"))

    result = pod_runtime.create(create_request, confirmation=shown_phrase)

    assert result.state is LaunchState.REFUSED_CONFIRMATION
    assert result.detail is not None
    assert "price may have changed" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_cli_prints_preview_before_collecting_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock()
    provider = fake(clock)
    armer = FakeControllerArmer(clock, provider)
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        "\n".join(
            (
                'schema = "pod-spend.v1"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                "hard_lifetime_seconds = 3600",
                "laptop_heartbeat_timeout_seconds = 30",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 5",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "name": "cli-preview-test",
                "gpu_type": "fake-48gb",
                "image": "registry.example/verbatus@sha256:" + "a" * 64,
                "template": "pinned-template",
                "volume_id": "test-volume",
                "volume_mount_path": "/workspace/private",
                "docker_start_cmd": list(request(clock).docker_start_cmd),
                "hard_deadline": (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
                "repository_commit": "b" * 40,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_provider", lambda reference: provider)
    monkeypatch.setattr(cli, "_controller_armer", lambda reference: armer)
    observed: dict[str, str] = {}

    def wrong_confirmation(prompt: str) -> str:
        observed["prompt"] = prompt
        observed["prior_output"] = capsys.readouterr().out
        return "wrong words"

    monkeypatch.setattr("builtins.input", wrong_confirmation)

    exit_code = cli.main(
        [
            "--provider-factory",
            "unused:factory",
            "--controller-armer-factory",
            "unused:factory",
            "--spend",
            str(spend_path),
            "--leases",
            str(tmp_path / "leases"),
            "--provider-name",
            "fake",
            "create",
            "--request",
            str(request_path),
        ]
    )

    assert exit_code == 2
    assert "estimated_total_metered_cost_usd" in observed["prior_output"]
    assert CONFIRMATION_PREFIX in observed["prompt"]
    # The prompt carries the phrase for *this* pod at *this* price — not a
    # constant an operator could have typed without reading the preview above.
    assert (
        confirmation_phrase("create", "cli-preview-test", POD_HOURLY, VOLUME_HOURLY)
        in observed["prompt"]
    )
    assert "refused-confirmation" in capsys.readouterr().out
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_successful_fake_adoption_uses_the_same_guarded_controller_handshake(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    expected = request(clock)
    existing = provider.create(expected)

    result = runtime(provider, clock, tmp_path).adopt(
        existing.pod_id,
        expected=expected,
        confirmation=adopt_confirmation(existing.pod_id),
    )

    assert result.state is LaunchState.ADOPTED_GUARDED
    assert result.green
    assert result.controller_arming is not None and result.controller_arming.armed


def test_both_paid_paths_refuse_a_price_outside_spend_ceiling(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    create_request = request(clock)
    pod_runtime = runtime(provider, clock, tmp_path, spend=policy(hourly="0.50"))

    create = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)
    assert create.state is LaunchState.REFUSED_CEILING
    assert not any(verb == "create" for verb, _ in provider.calls)

    existing = provider.create(create_request)
    adopt = pod_runtime.adopt(
        existing.pod_id,
        expected=create_request,
        confirmation=adopt_confirmation(existing.pod_id),
    )
    assert adopt.state is LaunchState.REFUSED_CEILING
    assert list(tmp_path.glob("*.json")) == []


def test_spend_ceiling_counts_attached_volume_during_the_hard_lifetime(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path, spend=policy(hourly="0.80")).preview_create(
        request(clock)
    )

    assert result.state is LaunchState.PREVIEW
    assert result.preview is not None
    assert not result.preview.assessment.allowed
    assert (
        result.preview.assessment.estimated_total_cost_usd
        > result.preview.assessment.estimated_pod_cost_usd
    )
    assert any("attached-volume" in reason for reason in result.preview.assessment.reasons)


def test_confirmation_preview_shows_cost_rounded_to_the_cent(tmp_path: Path) -> None:
    """The one screen read slowly before spending money should not be noisy

    with repeating-decimal division (audit-d Finding 15). The exact value
    still governs the ceiling check; only the displayed record is rounded.
    """

    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path).preview_create(request(clock, lifetime=300))

    assert result.state is LaunchState.PREVIEW
    assert result.preview is not None
    assert result.preview.assessment.estimated_pod_cost_usd != Decimal("0.06")
    record = result.preview.assessment.to_record()
    assert record["estimated_pod_cost_usd"] == "0.06"


def test_spend_guard_rounds_fractional_lifetime_up_not_down(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    exact = policy()
    request_over_limit = replace(
        request(clock, lifetime=exact.hard_lifetime_seconds or 3600),
        hard_deadline=clock.now()
        + timedelta(seconds=exact.hard_lifetime_seconds or 3600, microseconds=1),
    )

    preview = runtime(provider, clock, tmp_path, spend=exact).preview_create(request_over_limit)

    assert preview.state is LaunchState.PREVIEW
    assert preview.preview is not None
    assert not preview.preview.assessment.allowed
    assert preview.preview.assessment.requested_lifetime_seconds == 3601
    assert any("hard lifetime" in reason for reason in preview.preview.assessment.reasons)


def test_adoption_uses_current_price_and_proves_the_expected_runtime_contract(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    expected = request(clock)
    existing = provider.create(expected)
    provider.price_sheet["fake-48gb"] = (Decimal("1.50"), Decimal("0.05"))

    stale_price = runtime(provider, clock, tmp_path, spend=policy(hourly="1.00")).adopt(
        existing.pod_id,
        expected=expected,
        confirmation=CREATE_CONFIRMATION,
    )

    assert stale_price.state is LaunchState.REFUSED_CEILING
    assert stale_price.preview is not None
    assert stale_price.preview.assessment.estimate.pod_hourly_usd == Decimal("1.50")

    provider.price_sheet["fake-48gb"] = (Decimal("0.77"), Decimal("0.05"))
    assert existing.runtime_contract is not None
    provider.pods[existing.pod_id] = replace(
        existing,
        runtime_contract=replace(
            existing.runtime_contract, image="registry.example/other@sha256:" + "c" * 64
        ),
    )
    mismatched = runtime(provider, clock, tmp_path / "mismatch").preview_adopt(
        existing.pod_id, expected=expected
    )

    assert mismatched.state is LaunchState.REFUSED_RUNTIME_CONTRACT


def test_guarded_create_seals_dead_man_facts_into_the_creation_request(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CREATED_GUARDED
    assert result.lease_path is not None and result.lease_path.is_file()
    assert result.owner_token is not None
    submitted = provider.create_requests[-1]
    assert "operations.pod.pod_timer" in " ".join(submitted.docker_start_cmd)
    assert submitted.metadata["VERBATUS_HARD_DEADLINE"].endswith("Z")
    assert submitted.metadata["VERBATUS_LAUNCH_TOKEN"] == "a" * 32
    assert submitted.metadata["VERBATUS_VOLUME_ID"] == "test-volume"
    assert submitted.metadata["VERBATUS_POD_HOURLY_USD"] == "0.77"
    assert submitted.metadata["VERBATUS_VOLUME_ONGOING_HOURLY_USD"] == "0.05"


def test_default_runtime_refuses_paid_create_without_an_approved_controller_harness(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    unarmed = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: "a" * 32,
    )

    result = unarmed.create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_CONTROLLER_NOT_READY
    assert not result.green
    assert not provider.calls


def test_create_refuses_when_the_shutdown_provider_seam_is_incomplete(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.capture_cost = None  # type: ignore[method-assign]

    result = runtime(provider, clock, tmp_path).preview_create(request(clock))

    assert result.state is LaunchState.REFUSED_SHUTDOWN_NOT_READY
    assert "capture_cost" in result.detail
    assert not provider.calls


def test_configured_spend_timing_wires_the_default_verified_shutdown_controller(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    configured = policy()
    pod_runtime = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=configured,
        lease_root=tmp_path,
        now=clock.now,
        controller_armer=FakeControllerArmer(clock, provider),
    )

    assert pod_runtime.shutdown.timeout_seconds == configured.shutdown_deadline_seconds
    assert pod_runtime.shutdown.poll_seconds == configured.shutdown_poll_interval_seconds


def test_green_launch_requires_and_persists_two_controller_acknowledgements(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    armer = FakeControllerArmer(clock, provider)

    result = runtime(provider, clock, tmp_path, armer=armer).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.green
    assert result.controller_arming is not None and result.controller_arming.armed
    assert len(armer.handshakes) == 1
    assert result.lease_path is not None
    lease = LeaseStore(result.lease_path).load()
    assert lease is not None and lease.controller_record is not None
    assert lease.controller_record["pod_timer_acknowledged"] is True


def test_unbound_controller_receipt_closes_the_pod_and_cannot_make_launch_green(
    tmp_path: Path,
) -> None:
    class UnboundArmer(FakeControllerArmer):
        def arm(self, **kwargs):  # type: ignore[no-untyped-def]
            observed = super().arm(**kwargs)
            receipt = dict(observed.receipt)
            receipt["pod_id"] = "wrong-pod"
            return replace(observed, receipt=receipt)

    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path, armer=UnboundArmer(clock, provider)).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CONTROLLERS_UNARMED
    assert not result.green
    assert result.record is not None
    assert provider.status(result.record.pod_id).presence.value == "absent"


def test_timer_acknowledgement_for_another_report_path_cannot_make_launch_green(
    tmp_path: Path,
) -> None:
    class WrongReportPathArmer(FakeControllerArmer):
        def arm(self, **kwargs):  # type: ignore[no-untyped-def]
            observed = super().arm(**kwargs)
            receipt = dict(observed.receipt)
            timer = dict(receipt["pod_timer"])
            timer["report_path"] = "/workspace/private/unrelated-report.json"
            receipt["pod_timer"] = timer
            return replace(observed, receipt=receipt)

    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path, armer=WrongReportPathArmer(clock, provider)).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CONTROLLERS_UNARMED
    assert not result.green
    assert result.controller_arming is not None
    assert "different durable report path" in result.controller_arming.detail
    assert result.record is not None
    assert provider.status(result.record.pod_id).presence.value == "absent"


def test_controller_arming_failure_closes_a_created_pod_and_stays_non_green(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    armer = FakeControllerArmer(clock, provider, arm=False)

    result = runtime(provider, clock, tmp_path, armer=armer).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CONTROLLERS_UNARMED
    assert not result.green
    assert result.close_report is not None
    assert result.close_report.state is CloseState.UNVERIFIED_BILLING
    assert result.record is not None
    assert provider.status(result.record.pod_id).presence.value == "absent"


def test_one_controller_acknowledgement_is_not_a_green_launch(tmp_path: Path) -> None:
    class HalfArmer(FakeControllerArmer):
        def arm(self, **kwargs):  # type: ignore[no-untyped-def]
            return ControllerArming(
                True,
                False,
                self.clock.now(),
                "fake laptop started but fake pod timer did not acknowledge",
                {"phase": "half-armed"},
            )

    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path, armer=HalfArmer(clock, provider)).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CONTROLLERS_UNARMED
    assert not result.green
    assert result.controller_arming is not None
    assert not result.controller_arming.armed


def test_controller_arming_value_requires_both_independent_acknowledgements() -> None:
    only_laptop = ControllerArming(True, False, START, "only laptop", {"phase": "test"})
    only_timer = ControllerArming(False, True, START, "only timer", {"phase": "test"})

    assert not only_laptop.armed
    assert not only_timer.armed


def test_created_pod_with_unproven_effective_runtime_contract_is_closed(tmp_path: Path) -> None:
    class ContractBlindFake(FakeProvider):
        def create(self, create_request: PodCreateRequest) -> PodRecord:
            return replace(super().create(create_request), runtime_contract=None)

    clock = Clock()
    provider = ContractBlindFake({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.REFUSED_RUNTIME_CONTRACT
    assert not result.green
    assert result.record is not None
    assert provider.status(result.record.pod_id).presence.value == "absent"


def test_a_created_pod_priced_above_the_ceiling_is_closed_not_left_running(
    tmp_path: Path,
) -> None:
    """The estimate authorises; the pod that arrives is what bills.

    A stale price sheet can only under-estimate, so the ceiling is re-applied to
    the rate the provider actually returned. A pod that fails it already exists
    and is already billing, which is why this closes it rather than refusing.
    """

    class DriftedPriceFake(FakeProvider):
        def create(self, create_request: PodCreateRequest) -> PodRecord:
            record = super().create(create_request)
            return replace(
                record, estimate=replace(record.estimate, pod_hourly_usd=Decimal("9.99"))
            )

    clock = Clock()
    provider = DriftedPriceFake({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.REFUSED_CEILING
    assert not result.green
    assert result.record is not None
    assert result.close_report is not None
    assert provider.terminate_calls == [result.record.pod_id]
    assert "exceeds configured ceiling" in result.detail


def test_billing_reconciliation_is_bounded_and_absorbs_a_lagging_first_answer() -> None:
    """Billing lags termination; the retry is bounded, and empty still ends red."""

    class LaggingBillingFake(FakeProvider):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.billing_calls = 0

        def capture_cost(
            self, pod_id: str, started_at: datetime, cutoff_at: datetime
        ) -> CostCapture:
            self.billing_calls += 1
            # The deterministic test clock has not advanced between create and
            # close, so the fake resolves a later cutoff exactly as
            # ``FakeProvider.bill`` does; a zero-width window is not a window.
            resolved = cutoff_at + timedelta(hours=1)
            if self.billing_calls < 3:
                return CostCapture(
                    pod_id=pod_id,
                    state=BillingState.UNAVAILABLE,
                    cutoff_at=resolved,
                    reason="fake billing has not posted yet",
                    window_start_at=started_at,
                )
            return CostCapture(
                pod_id=pod_id,
                state=BillingState.CAPTURED,
                cutoff_at=resolved,
                lines=(CostLine(Decimal("0.13"), "fake late billing"),),
                window_start_at=started_at,
            )

    clock = Clock()
    provider = LaggingBillingFake({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
    record = provider.create(request(clock))

    report = shutdown(provider, clock).close(record, reason="lagging billing drill")

    assert report.state is CloseState.VERIFIED
    assert report.captured_cost_usd == Decimal("0.13")
    assert report.billing_attempts == 3
    assert provider.billing_calls == 3


def test_billing_that_never_posts_exhausts_its_bounded_retry_and_stays_red() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    closer = VerifiedShutdown(
        provider,
        timeout_seconds=8,
        poll_seconds=1,
        billing_attempts=2,
        billing_retry_seconds=1,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )

    report = closer.close(record, reason="never-posting billing drill")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert report.captured_cost_usd is None
    assert report.billing_attempts == 2
    assert sum(1 for verb, _ in provider.calls if verb == "capture_cost") == 2


def test_create_5xx_is_a_named_non_green_result_with_pending_evidence(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.inject_failure("create", RuntimeError("HTTP 500"))
    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.PROVIDER_FAILURE
    assert result.lease_path is not None and result.lease_path.is_file()
    assert "pending lease" in result.detail


def test_lease_creation_failure_refuses_before_provider_create(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("block lease directory", encoding="utf-8")

    result = runtime(provider, clock, blocked_root).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.LEASE_FAILURE
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_confirmed_adoption_closes_if_its_lease_cannot_be_recorded(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    expected = request(clock)
    existing = provider.create(expected)
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("block lease directory", encoding="utf-8")

    result = runtime(provider, clock, blocked_root).adopt(
        existing.pod_id,
        expected=expected,
        confirmation=adopt_confirmation(existing.pod_id),
    )

    assert result.state is LaunchState.LEASE_FAILURE
    assert result.close_report is not None
    assert existing.pod_id in provider.terminate_calls
    assert provider.status(existing.pod_id).presence.value == "absent"


def test_an_altered_lease_is_refused_rather_than_acted_on(tmp_path: Path) -> None:
    """A lease says which pod is still billing. An edited one is not evidence."""

    clock = Clock()
    provider = fake(clock)
    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )
    assert result.lease_path is not None
    store = LeaseStore(result.lease_path)
    assert store.load() is not None

    record = json.loads(result.lease_path.read_text(encoding="utf-8"))
    record["hard_deadline"] = "2099-01-01T00:00:00Z"
    result.lease_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(LeaseFormatError, match="self_hash does not recompute"):
        store.load()


def test_a_lease_with_no_seal_at_all_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "unsealed.json"
    path.write_text(json.dumps({"schema": "pod-lease.v1"}), encoding="utf-8")

    with pytest.raises(LeaseFormatError, match="carries no self_hash"):
        LeaseStore(path).load()


def test_created_pod_is_immediately_closed_when_lease_binding_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bind_failure(self, *, owner_token, record, now=None):  # type: ignore[no-untyped-def]
        raise OSError("injected bind write failure")

    monkeypatch.setattr(LeaseStore, "bind_pod", bind_failure)
    clock = Clock()
    provider = fake(clock)

    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CREATE_UNLEASED
    assert not result.green
    assert result.record is not None
    assert provider.status(result.record.pod_id).presence.value == "absent"

    assert result.lease_path is not None
    lease = LeaseStore(result.lease_path).load()
    assert lease is not None
    assert lease.phase in {"close-unverified", "closed-verified"}
    assert lease.pod_id == result.record.pod_id
    assert lease.pending_create is None
    assert lease.close_record is not None and lease.close_record["pod_id"] == result.record.pod_id


def test_restart_recovers_exact_pending_launch_token_and_closes_only_that_orphan(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.inject_post_create_failure(RuntimeError("client died after provider accepted POST"))
    initial = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert initial.state is LaunchState.PROVIDER_FAILURE
    assert initial.lease_path is not None
    orphan_id = "fake-pod-1"
    provider.bill(orphan_id, "0.12")
    unrelated = provider.create(request(clock))
    clock.seconds = 31

    recovered = LaptopSupervisor(
        LeaseStore(initial.lease_path),
        shutdown(provider, clock),
        owner_token="restarted-laptop",
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    ).run_once()

    assert recovered.state is ControllerState.PENDING_CREATE_RECOVERED
    assert recovered.close_report is not None and recovered.close_report.verified
    assert orphan_id in provider.terminate_calls
    assert unrelated.pod_id not in provider.terminate_calls
    assert any(item.recovery_only for item in provider.create_requests)


def test_pending_create_recovery_attempts_are_bounded_before_manual_review(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.inject_failure("create", RuntimeError("initial provider 5xx"), times=4)
    initial = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )
    assert initial.lease_path is not None

    supervisor = LaptopSupervisor(
        LeaseStore(initial.lease_path),
        shutdown(provider, clock),
        owner_token="b" * 32,
        heartbeat_timeout=timedelta(seconds=1),
        now=clock.now,
    )
    results = [supervisor.run_once() for _ in range(4)]

    assert all(result.state is ControllerState.PENDING_CREATE_REVIEW for result in results)
    assert "bounded attempt limit" in results[-1].detail
    assert sum(1 for verb, _ in provider.calls if verb == "create") == 4


def test_verified_shutdown_reissues_termination_until_get_and_list_are_both_absent() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.set_disappearance_lag(record.pod_id, get_polls=2, list_polls=2)
    provider.bill(record.pod_id, "0.13")

    report = shutdown(provider, clock).close(record, reason="test close")

    assert report.state is CloseState.VERIFIED
    assert report.pod_get_absent and report.pod_list_absent
    assert report.captured_cost_usd == Decimal("0.13")
    assert len(provider.terminate_calls) >= 3, "still-present fake must force reissued terminate"
    assert report.to_record()["volume"]["decision"] == (
        "unchanged by pod close; retention or deletion requires separate authorization"
    )


@pytest.mark.parametrize("wrong_observation", ("status", "list"))
def test_shutdown_rejects_absence_evidence_for_a_different_pod(wrong_observation: str) -> None:
    clock = Clock()
    provider = WrongAbsenceEvidenceFake(clock, wrong_observation=wrong_observation)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.13")

    report = shutdown(provider, clock, timeout=3).close(
        record, reason="wrong absence identity falsification"
    )

    assert report.state is CloseState.FAILED_SHUTDOWN
    assert not report.verified
    assert "different pod" in report.last_detail
    if wrong_observation == "status":
        assert not report.pod_get_absent and report.pod_list_absent
    else:
        assert report.pod_get_absent and not report.pod_list_absent


@pytest.mark.parametrize("wrong_observation", ("status-200", "status-none"))
def test_shutdown_requires_an_exact_pod_get_404(wrong_observation: str) -> None:
    clock = Clock()
    provider = WrongAbsenceEvidenceFake(clock, wrong_observation=wrong_observation)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.13")

    report = shutdown(provider, clock, timeout=3).close(
        record, reason="missing GET-404 falsification"
    )

    assert report.state is CloseState.FAILED_SHUTDOWN
    assert not report.verified
    assert not report.pod_get_absent and report.pod_list_absent
    assert "GET-404 proof" in report.last_detail


@pytest.mark.parametrize(
    ("defect", "expected_detail"),
    (
        ("pod", "different pod"),
        ("cutoff", "precedes the requested shutdown cutoff"),
        ("window", "starts after pod creation"),
    ),
)
def test_shutdown_rejects_misbound_or_narrowed_billing_evidence(
    defect: str, expected_detail: str
) -> None:
    clock = Clock()
    provider = WrongBillingEvidenceFake(clock, defect=defect)
    record = provider.create(request(clock))

    report = shutdown(provider, clock).close(record, reason="wrong billing evidence falsification")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert not report.verified
    assert expected_detail in report.last_detail
    assert report.manual_action is not None and "did not prove" in report.manual_action


def test_empty_billing_is_unverified_not_zero_cost() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))

    report = shutdown(provider, clock).close(record, reason="test empty billing")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert report.captured_cost_usd is None
    assert report.manual_action is not None


def test_pending_billing_reconciliation_stays_non_green_and_names_recheck() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.set_billing(
        CostCapture(
            pod_id=record.pod_id,
            state=BillingState.PENDING_RECONCILIATION,
            cutoff_at=clock.now() + timedelta(seconds=1),
            reason="fake provider has not posted the final bucket",
            source="fake delayed billing",
            window_start_at=record.created_at,
        )
    )

    report = shutdown(provider, clock).close(record, reason="pending billing drill")

    assert report.state is CloseState.PENDING_RECONCILIATION
    assert not report.verified
    assert (
        report.manual_action is not None and "Re-run billing reconciliation" in report.manual_action
    )


def test_close_report_names_the_provider_resolved_billing_cutoff() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    resolved_cutoff = clock.now() + timedelta(hours=1)
    provider.set_billing(
        CostCapture(
            pod_id=record.pod_id,
            state=BillingState.CAPTURED,
            cutoff_at=resolved_cutoff,
            lines=(CostLine(Decimal("0.11"), "provider resolved hour bucket"),),
            source="fake provider resolved bucket",
            window_start_at=record.created_at,
        )
    )

    report = shutdown(provider, clock).close(record, reason="resolved cutoff drill")

    assert report.state is CloseState.VERIFIED
    assert report.cutoff_at == resolved_cutoff
    cost_record = report.to_record()["cost_capture"]
    assert isinstance(cost_record, dict)
    assert cost_record["lines"] == [
        {
            "amount_usd": "0.11",
            "description": "provider resolved hour bucket",
            "recorded_at": None,
        }
    ]


def test_a_billing_cutoff_arbitrarily_far_in_the_future_is_refused_not_verified() -> None:
    """"Charges captured through a named cutoff" is a claim about measured time

    (GOVERNANCE 10); a cutoff nobody has reached yet is not that (audit-d
    Finding 2).
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    far_future_cutoff = clock.now() + timedelta(days=3650)
    provider.set_billing(
        CostCapture(
            pod_id=record.pod_id,
            state=BillingState.CAPTURED,
            cutoff_at=far_future_cutoff,
            lines=(CostLine(Decimal("0.11"), "implausible far-future bucket"),),
            window_start_at=record.created_at,
        )
    )

    report = shutdown(provider, clock).close(record, reason="far future cutoff drill")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert "too far beyond the requested window" in report.last_detail


def test_follow_up_supervisor_never_turns_unverified_close_green(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "unverified.json")
    _lease(store, record, owner="laptop", clock=clock, deadline_seconds=5)
    close = shutdown(provider, clock).close(record, reason="unverifiable billing drill")
    assert close.state is CloseState.UNVERIFIED_BILLING
    store.record_close(
        owner_token="laptop", close_record=close.to_record(), verified=False, now=clock.now()
    )

    result = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=1),
        now=clock.now,
    ).run_once()

    assert result.state is ControllerState.CLOSE_UNVERIFIED
    assert not result.green


def test_lease_cannot_construct_verified_phase_from_misbound_close_evidence(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.04")
    store = LeaseStore(tmp_path / "misbound-close.json")
    _lease(store, record, owner="laptop", clock=clock, deadline_seconds=5)
    close_record = shutdown(provider, clock).close(record, reason="binding drill").to_record()
    close_record["pod_id"] = "another-pod"

    with pytest.raises(ValueError, match="exact pod"):
        store.record_close(
            owner_token="laptop",
            close_record=close_record,
            verified=True,
            now=clock.now(),
        )


def test_status_timeout_reaches_a_named_non_green_shutdown_state() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.inject_failure("status", TimeoutError("provider status timed out"), times=20)

    report = shutdown(provider, clock, timeout=3).close(record, reason="status timeout drill")

    assert report.state is CloseState.FAILED_SHUTDOWN
    assert not report.pod_get_absent
    assert report.manual_action is not None


def test_repeated_terminate_failure_reaches_a_named_non_green_shutdown_state() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.inject_failure("terminate", RuntimeError("provider terminate 5xx"), times=20)

    report = shutdown(provider, clock, timeout=3).close(record, reason="terminate failure drill")

    assert report.state is CloseState.FAILED_SHUTDOWN
    assert not report.pod_get_absent
    assert "terminate failed" in report.last_detail
    assert report.manual_action is not None


def _lease(
    store: LeaseStore,
    record: PodRecord,
    *,
    owner: str,
    clock: Clock,
    deadline_seconds: int = 60,
    armed: bool = True,
) -> PodLease:
    hard_deadline = clock.now() + timedelta(seconds=deadline_seconds)
    stamp = clock.now().isoformat().replace("+00:00", "Z")
    receipt = {
        "laptop_supervisor_started": True,
        "pod_timer_acknowledged": True,
        "observed_at": stamp,
        "detail": "pre-armed fake controller fixture",
        "receipt": {
            "lease_id": "c" * 32,
            "pod_id": record.pod_id,
            "hard_deadline": hard_deadline.isoformat().replace("+00:00", "Z"),
            "laptop_supervisor": {
                "identity": "pre-armed-fake-laptop-supervisor",
                "started_at": stamp,
            },
            "pod_timer": {
                "report_path": "/workspace/private/pod-runtime-report.json",
                "acknowledged_at": stamp,
            },
        },
    }
    lease = PodLease(
        lease_id="c" * 32,
        launch_token="d" * 32,
        provider_name="fake",
        pod_id=record.pod_id,
        volume_id=record.volume_id,
        pod_hourly_usd=record.estimate.pod_hourly_usd,
        volume_hourly_usd=record.estimate.volume_hourly_usd,
        created_at=clock.now(),
        started_at=record.created_at,
        hard_deadline=hard_deadline,
        owner_token=owner,
        heartbeat_at=clock.now(),
        phase="active",
        controller_record=receipt if armed else None,
    )
    store.create(lease)
    return lease


def test_laptop_lifetime_expiry_terminates_through_verified_path(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.15")
    store = LeaseStore(tmp_path / "lease.json")
    _lease(store, record, owner="laptop", clock=clock, deadline_seconds=5)
    clock.seconds = 5

    result = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=2),
        now=clock.now,
    ).run_once()

    assert result.state is ControllerState.LIFETIME_EXPIRED
    assert result.close_report is not None and result.close_report.verified
    assert provider.status(record.pod_id).presence.value == "absent"


def test_lease_record_failure_after_a_verified_close_is_never_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close whose durable evidence failed to persist must never report green."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.12")
    store = LeaseStore(tmp_path / "lease.json")
    _lease(store, record, owner="laptop", clock=clock, deadline_seconds=5)
    clock.seconds = 5

    def _broken_record_close(*args: object, **kwargs: object) -> PodLease:
        raise RuntimeError("disk full")

    monkeypatch.setattr(LeaseStore, "record_close", _broken_record_close)

    result = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=2),
        now=clock.now,
    ).run_once()

    assert result.state is ControllerState.LEASE_RECORD_FAILURE
    assert result.close_report is not None and result.close_report.verified
    assert result.green is False


def test_heartbeat_loss_and_orphan_reconciliation_each_terminate(tmp_path: Path) -> None:
    clock = Clock(seconds=10)
    provider = fake(clock)
    first = provider.create(request(clock, lifetime=300))
    provider.bill(first.pod_id, "0.10")
    own_store = LeaseStore(tmp_path / "own.json")
    _lease(own_store, first, owner="laptop", clock=Clock(), deadline_seconds=300)
    own_result = LaptopSupervisor(
        own_store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=5),
        now=clock.now,
    ).run_once()
    assert own_result.state is ControllerState.HEARTBEAT_LOST
    assert own_result.close_report is not None and own_result.close_report.verified

    second = provider.create(request(clock, lifetime=300))
    provider.bill(second.pod_id, "0.11")
    orphan_store = LeaseStore(tmp_path / "orphan.json")
    _lease(orphan_store, second, owner="dead-laptop", clock=Clock(), deadline_seconds=300)
    orphan_result = LaptopSupervisor(
        orphan_store,
        shutdown(provider, clock),
        owner_token="replacement",
        heartbeat_timeout=timedelta(seconds=5),
        now=clock.now,
    ).run_once()
    assert orphan_result.state is ControllerState.ORPHAN_RECONCILED
    assert orphan_result.close_report is not None and orphan_result.close_report.verified


def test_laptop_controller_death_leaves_pod_timer_to_terminate(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.08")
    store = LeaseStore(tmp_path / "timer.json")
    lease = _lease(store, record, owner="dead-laptop", clock=clock, deadline_seconds=3)
    clock.seconds = 3

    # No LaptopSupervisor is started: this is the controller-death drill.
    result = PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now).run_once()

    assert result.state is ControllerState.TIMER_EXPIRED
    assert result.close_report is not None and result.close_report.verified


def test_pod_timer_death_leaves_laptop_supervisor_to_terminate(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.09")
    store = LeaseStore(tmp_path / "supervisor.json")
    _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    clock.seconds = 3

    # The timer is deliberately absent/dead; the laptop sees the same hard deadline.
    result = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=1),
        now=clock.now,
    ).run_once()

    assert result.state is ControllerState.LIFETIME_EXPIRED
    assert result.close_report is not None and result.close_report.verified


def test_a_supervisor_ticking_inside_the_arming_window_does_not_kill_its_own_pod(
    tmp_path: Path,
) -> None:
    """`arm` starts the laptop supervisor; the receipt is written after it returns.

    A supervisor that runs even once inside that window sees an active lease with
    no arming evidence.  Treating that alone as a defect terminates the pod the
    supervisor was started to guard, and the authorised launch ends non-green.
    """

    class StartsSupervisorArmer(FakeControllerArmer):
        def arm(self, **kwargs):  # type: ignore[no-untyped-def]
            self.first_tick = LaptopSupervisor(
                kwargs["store"],
                shutdown(self.provider, self.clock),
                owner_token=kwargs["owner_token"],
                heartbeat_timeout=timedelta(seconds=30),
                now=self.clock.now,
            ).run_once()
            return super().arm(**kwargs)

    clock = Clock()
    provider = fake(clock)
    armer = StartsSupervisorArmer(clock, provider)

    result = runtime(provider, clock, tmp_path, armer=armer).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert armer.first_tick.state is ControllerState.CONTROLLER_UNARMED
    assert not armer.first_tick.green
    assert armer.first_tick.close_report is None
    assert result.state is LaunchState.CREATED_GUARDED
    assert result.record is not None
    assert provider.terminate_calls == []
    assert provider.status(result.record.pod_id).presence.value == "present"


def test_an_unarmed_lease_whose_launch_stopped_heartbeating_is_still_closed(
    tmp_path: Path,
) -> None:
    """Waiting out the arming window may not become a way to leave a pod unguarded."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.14")
    store = LeaseStore(tmp_path / "unarmed.json")
    _lease(store, record, owner="laptop", clock=clock, deadline_seconds=300, armed=False)
    clock.seconds = 31

    result = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    ).run_once()

    assert result.state is ControllerState.CONTROLLER_UNARMED
    assert result.close_report is not None and result.close_report.verified
    assert provider.status(record.pod_id).presence.value == "absent"


def test_armed_launch_controller_death_drills_use_the_launch_handshake(tmp_path: Path) -> None:
    """Kill each controller after a green launch, not merely a standalone object."""

    clock = Clock()
    provider = fake(clock)
    timer_armer = FakeControllerArmer(clock, provider)
    timer_launch = runtime(provider, clock, tmp_path / "timer", armer=timer_armer).create(
        request(clock, lifetime=3), confirmation=CREATE_CONFIRMATION
    )
    assert timer_launch.record is not None
    provider.bill(timer_launch.record.pod_id, "0.08")
    clock.seconds = 3
    timer_result = timer_armer.handshakes[0].kill_laptop()
    assert timer_result.close_report is not None and timer_result.close_report.verified

    second_clock = Clock()
    second_provider = fake(second_clock)
    laptop_armer = FakeControllerArmer(second_clock, second_provider)
    laptop_launch = runtime(
        second_provider, second_clock, tmp_path / "laptop", armer=laptop_armer
    ).create(request(second_clock, lifetime=3), confirmation=CREATE_CONFIRMATION)
    assert laptop_launch.record is not None
    second_provider.bill(laptop_launch.record.pod_id, "0.09")
    second_clock.seconds = 3
    laptop_result = laptop_armer.handshakes[0].kill_timer()
    assert laptop_result.close_report is not None and laptop_result.close_report.verified


def test_pod_timer_requires_bootstrap_and_persists_a_red_bootstrap_close_report(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.07")
    store = LeaseStore(tmp_path / "timer-bootstrap.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)

    class FailedChild:
        def poll(self) -> int:
            return 17

    report_path = tmp_path / "pod-report.json"
    result = run_with_bootstrap(
        TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
        bootstrap_command_json='["python","-m","operations.pod.bootstrap"]',
        report_path=report_path,
        sleeper=clock.sleep,
        interval_seconds=1,
        popen=lambda argv: FailedChild(),  # type: ignore[arg-type]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.close_report is not None and result.close_report.verified
    assert report["bootstrap"]["state"] == "failed"
    assert report["close"]["state"] == CloseState.VERIFIED
    assert report["green"] is False


def test_bare_timer_command_is_rejected_before_a_paid_create() -> None:
    clock = Clock()
    with pytest.raises(ValueError, match="--timer-factory|--bootstrap-command-json"):
        PodCreateRequest(
            name="missing-bootstrap",
            gpu_type="fake-48gb",
            image="registry.example/verbatus@sha256:" + "a" * 64,
            volume_id="test-volume",
            volume_mount_path="/workspace/private",
            docker_start_cmd=("python", "-m", "operations.pod.pod_timer"),
            hard_deadline=clock.now() + timedelta(seconds=5),
            repository_commit="b" * 40,
        )


@pytest.mark.parametrize(
    "interpreter",
    [
        ("python3",),
        ("/opt/venv/bin/python",),
        ("uv", "run", "python"),
    ],
)
def test_pod_timer_primary_process_check_accepts_any_interpreter_spelling(
    interpreter: tuple[str, ...],
) -> None:
    clock = Clock()
    flags_after_module = request(clock).docker_start_cmd[3:]
    command = interpreter + ("-m", "operations.pod.pod_timer") + flags_after_module

    created = replace(request(clock), docker_start_cmd=command)

    assert created.docker_start_cmd[: len(interpreter)] == interpreter


def test_pod_timer_primary_process_check_refuses_a_shell_ahead_of_the_interpreter() -> None:
    clock = Clock()
    command = ("sh", "-c") + request(clock).docker_start_cmd

    with pytest.raises(ValueError, match="must not invoke a shell"):
        replace(request(clock), docker_start_cmd=command)


@pytest.mark.parametrize("field", ["HF_TOKEN", "hf-token", "APIKEY", "MY_TOKEN", "SERVICE_KEY"])
def test_credential_shaped_metadata_is_refused_by_bare_key_and_token_markers(field: str) -> None:
    """A credential-like field cannot ride in pod metadata under any common naming style."""

    clock = Clock()
    with pytest.raises(ValueError, match="credential-like field"):
        PodCreateRequest(
            name="credential-leak-test",
            gpu_type="fake-48gb",
            image="registry.example/verbatus@sha256:" + "a" * 64,
            volume_id="test-volume",
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
            hard_deadline=clock.now() + timedelta(seconds=5),
            repository_commit="b" * 40,
            metadata={field: "should-never-be-accepted"},
        )


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/verbatus@sha256:short",
        "registry.example/verbatus@sha256:" + "g" * 64,
        "registry.example/verbatus@sha256:" + "a" * 64 + ":mutable",
    ],
)
def test_image_pin_must_be_one_complete_lowercase_sha256_digest(image: str) -> None:
    with pytest.raises(ValueError, match="immutable sha256 digest"):
        replace(request(Clock()), image=image)


def test_timer_report_path_cannot_lexically_escape_the_attached_volume() -> None:
    command = list(request(Clock()).docker_start_cmd)
    command[command.index("--report-path") + 1] = "/workspace/private/../outside.json"

    with pytest.raises(ValueError, match="inside the attached volume"):
        replace(request(Clock()), docker_start_cmd=tuple(command))


def test_a_sealed_report_path_that_omits_its_launch_token_is_refused() -> None:
    """A volume outlives any one pod; an unbound report path lets a second

    launch on the same volume silently overwrite the first's durable close
    evidence.  Once the launch token is sealed into metadata, the report
    path must carry it.
    """

    clock = Clock()
    with pytest.raises(ValueError, match="must include this launch's token"):
        replace(
            request(clock),
            metadata={"VERBATUS_LAUNCH_TOKEN": "a" * 32},
        )


def test_guarded_create_binds_the_report_path_to_this_launchs_token(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)

    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CREATED_GUARDED
    submitted = provider.create_requests[-1]
    command = submitted.docker_start_cmd
    bound_report_path = command[command.index("--report-path") + 1]
    assert bound_report_path == "/workspace/private/pod-runtime-report-" + "a" * 32 + ".json"


def test_two_launches_on_one_volume_get_distinct_report_paths(tmp_path: Path) -> None:
    """The volume is retained across pods by design; two launches that reuse

    the same request-file report path must still land on non-colliding
    durable evidence.
    """

    clock = Clock()
    provider = fake(clock)
    tokens = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    pod_runtime = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        controller_armer=FakeControllerArmer(clock, provider),
    )

    first = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)
    assert first.state is LaunchState.CREATED_GUARDED
    first_command = provider.create_requests[-1].docker_start_cmd
    first_path = first_command[first_command.index("--report-path") + 1]

    second = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)
    assert second.state is LaunchState.CREATED_GUARDED
    second_command = provider.create_requests[-1].docker_start_cmd
    second_path = second_command[second_command.index("--report-path") + 1]

    assert first_path != second_path


def test_pod_timer_bootstrap_failed_to_start_records_its_own_reason_not_a_write_failure(
    tmp_path: Path,
) -> None:
    """The durable close evidence must name the failure that actually happened.

    A popen failure and a durable-write failure are different events; the
    close reason and the fallback error key must not always claim the write
    failed.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.06")
    store = LeaseStore(tmp_path / "timer-popen-failure.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    report_path = tmp_path / "popen-failure-report.json"

    def failing_popen(argv: list[str]) -> subprocess.Popen[bytes]:
        raise OSError("injected popen failure")

    with pytest.raises(
        RuntimeError,
        match=r"mandatory bootstrap process failed to start \(injected popen failure\)",
    ):
        run_with_bootstrap(
            TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
            bootstrap_command_json='["python","-m","operations.pod.bootstrap"]',
            report_path=report_path,
            popen=failing_popen,  # type: ignore[arg-type]
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["bootstrap"]["state"] == "failed-to-start"
    assert report["bootstrap"]["failure_detail"] == "injected popen failure"
    assert "report_write_error" not in report["bootstrap"]
    assert report["close"]["reason"] == "mandatory bootstrap process failed to start"


def test_pod_timer_report_write_failure_immediately_closes_and_never_returns_green(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.06")
    store = LeaseStore(tmp_path / "timer-report-write.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("block report directory", encoding="utf-8")

    class RunningChild:
        def poll(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="immediate close result is verified, never green"):
        run_with_bootstrap(
            TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
            bootstrap_command_json='["python","-m","operations.pod.bootstrap"]',
            report_path=blocked_parent / "report.json",
            popen=lambda argv: RunningChild(),  # type: ignore[arg-type]
        )
    assert provider.status(record.pod_id).presence.value == "absent"


def test_pod_timer_closes_when_bootstrap_exits_early_to_avoid_idle_spend(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.05")
    store = LeaseStore(tmp_path / "timer-early-exit.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)

    class CompletedChild:
        def poll(self) -> int:
            return 0

    report_path = tmp_path / "early-exit-report.json"
    result = run_with_bootstrap(
        TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
        bootstrap_command_json='["python","-m","operations.pod.bootstrap"]',
        report_path=report_path,
        popen=lambda argv: CompletedChild(),  # type: ignore[arg-type]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.close_report is not None and result.close_report.verified
    assert report["bootstrap"]["state"] == "completed-early"
    assert report["green"] is False


class FakeBootstrapActions:
    def __init__(
        self, *, crash_once: BootstrapStep | None = None, fail: BootstrapStep | None = None
    ) -> None:
        self.crash_once = crash_once
        self.fail = fail
        self.calls: list[BootstrapStep] = []

    def _step(self, step: BootstrapStep) -> dict[str, object]:
        self.calls.append(step)
        if self.crash_once is step:
            self.crash_once = None
            raise KeyboardInterrupt(f"injected crash during {step.value}")
        if self.fail is step:
            raise BootstrapStepFailure(step, "injected failure", "repair the injected failure")
        return {"step": step.value}

    def checkout_commit(self, commit: str) -> dict[str, object]:
        return self._step(BootstrapStep.REPOSITORY) | {"commit": commit}

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        return self._step(BootstrapStep.UV_ENVIRONMENT) | {"lockfile": str(lockfile)}

    def resume_transfer(self) -> dict[str, object]:
        return self._step(BootstrapStep.TRANSFER)

    def verify_chair_cache(self) -> dict[str, object]:
        return self._step(BootstrapStep.CHAIR_CACHE)

    def run_preflight(self) -> dict[str, object]:
        return self._step(BootstrapStep.PREFLIGHT) | {"color": "green"}


def test_bootstrap_crash_resumes_only_the_unfinished_idempotent_step(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")
    actions = FakeBootstrapActions(crash_once=BootstrapStep.UV_ENVIRONMENT)
    plan = BootstrapPlan("c" * 40, lockfile)
    journal = BootstrapJournal(tmp_path / "bootstrap.json", plan, now=lambda: START)
    bootstrapper = Bootstrapper(journal, actions)

    with pytest.raises(KeyboardInterrupt):
        bootstrapper.run()
    report = bootstrapper.run()

    assert report.green
    assert actions.calls.count(BootstrapStep.REPOSITORY) == 1
    assert actions.calls.count(BootstrapStep.UV_ENVIRONMENT) == 2
    assert actions.calls.count(BootstrapStep.PREFLIGHT) == 1


def test_bootstrap_journal_cannot_claim_green_with_unaccounted_steps(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")
    path = tmp_path / "bootstrap.json"
    journal = BootstrapJournal(path, BootstrapPlan("c" * 40, lockfile), now=lambda: START)
    record = journal.load_or_create()
    record["status"] = "green"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(BootstrapStepFailure, match="does not account for every step"):
        journal.load_or_create()


def test_partial_transfer_becomes_named_red_bootstrap_result(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")
    actions = FakeBootstrapActions(fail=BootstrapStep.TRANSFER)
    report = Bootstrapper(
        BootstrapJournal(
            tmp_path / "bootstrap.json", BootstrapPlan("d" * 40, lockfile), now=lambda: START
        ),
        actions,
    ).run()

    assert not report.green
    assert report.failure_step is BootstrapStep.TRANSFER
    assert BootstrapStep.PREFLIGHT not in actions.calls


def test_production_bootstrap_refuses_a_lockfile_other_than_checked_out_uv_lock(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "uv.lock").write_text("locked = true\n", encoding="utf-8")
    other = tmp_path / "other.lock"
    other.write_text("locked = false\n", encoding="utf-8")

    actions = SubprocessBootstrapActions(
        repository=repository,
        transfer=lambda: {},
        cache=None,  # type: ignore[arg-type]
        preflight=lambda: {"color": "green"},
        runner=lambda argv, cwd: (_ for _ in ()).throw(AssertionError(f"unexpected {argv}")),
    )

    with pytest.raises(BootstrapStepFailure, match="not repository uv.lock"):
        actions.sync_uv_environment(other)


def test_red_preflight_details_survive_into_the_bootstrap_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    actions = SubprocessBootstrapActions(
        repository=repository,
        transfer=lambda: {},
        cache=None,  # type: ignore[arg-type]
        preflight=lambda: {
            "color": "red",
            "issues": [{"code": "smoke-output-invalid", "chair": "attestator_2"}],
        },
    )

    with pytest.raises(BootstrapStepFailure) as caught:
        actions.run_preflight()

    assert "smoke-output-invalid" in caught.value.detail
    assert "attestator_2" in caught.value.detail


class FakeCache:
    def __init__(self, *, always_mismatch: str | None = None) -> None:
        self.always_mismatch = always_mismatch
        self.verify_calls: dict[str, int] = {}
        self.refetch_calls: dict[str, int] = {}

    def verify(self, identity):  # type: ignore[no-untyped-def]
        self.verify_calls[identity.role] = self.verify_calls.get(identity.role, 0) + 1
        if identity.role == self.always_mismatch:
            raise CacheMismatch("digest differs")
        return {"manifest_digest": identity.digest_manifest}

    def refetch_once(self, identity):  # type: ignore[no-untyped-def]
        self.refetch_calls[identity.role] = self.refetch_calls.get(identity.role, 0) + 1


class FakeSmoke:
    def __init__(self, *, invalid_chair: str | None = None) -> None:
        self.invalid_chair = invalid_chair

    def read(self, identity, fixture, placement):  # type: ignore[no-untyped-def]
        return SmokeResult(
            shape_valid=True,
            nonempty=identity.role != self.invalid_chair,
            format_valid=True,
            receipt={"fixture": fixture.name, "tier": placement.identifier},
            utilization=(UtilizationSample(Decimal("71"), Decimal("31")),),
        )


def _preflight(cache: FakeCache, smoke: FakeSmoke) -> PreflightRunner:
    root = Path(__file__).resolve().parents[2]
    return PreflightRunner(
        load_models_toml(root / "config/models.toml"),
        load_placement_table(root / "config/pod_placement.toml"),
        cache,
        smoke,
        root / "proof/fixtures/synthetic-two-page-v0/page-1.png",
    )


@pytest.mark.parametrize(
    ("vram", "expected"),
    [("24", "generic-24gb"), ("48", "generic-48gb"), ("80", "generic-80gb-plus")],
)
def test_preflight_uses_the_configured_vram_table(vram: str, expected: str) -> None:
    report = _preflight(FakeCache(), FakeSmoke()).run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal(vram), Decimal("100"), "bfloat16")
    )

    assert report.color == "green"
    assert report.tier == expected
    assert not report.assembly_proven, "fixture chairs are not a real pod assembly claim"
    assert report.placements
    planned_chairs = {item.chair for item in report.placements if item.state == "planned"}
    assert planned_chairs == {receipt["chair"] for receipt in report.cache_receipts}
    assert planned_chairs == {receipt["chair"] for receipt in report.smoke_receipts}
    assert all(item.residency in {"single", None} for item in report.placements)


def _table() -> PlacementTable:
    root = Path(__file__).resolve().parents[2]
    return load_placement_table(root / "config/pod_placement.toml")


def test_the_shipped_table_carries_a_prebuilt_profile_for_every_card_spec_04_names() -> None:
    names = {profile.name for profile in _table().card_profiles}

    assert {"RTX 6000 Ada", "RTX PRO 6000 Blackwell", "A40", "RTX A5000"} <= names


@pytest.mark.parametrize("card_name", ["RTX 6000 Ada", "NVIDIA RTX 6000 Ada Generation"])
def test_a_prebuilt_profile_matches_by_either_its_name_or_its_gpu_type_id(card_name: str) -> None:
    matched = _table().profile_for(card_name)

    assert matched is not None and matched.tier == "generic-48gb"


def test_an_unknown_card_falls_back_to_computed_placement_rather_than_a_guess() -> None:
    table = _table()

    assert table.profile_for("Some Card Nobody Reviewed") is None
    assert table.choose(Decimal("48")).identifier == "generic-48gb"


def test_the_preflight_report_says_which_plan_it_chose_and_why() -> None:
    matched = _preflight(FakeCache(), FakeSmoke()).run(
        GpuProfile(
            "NVIDIA RTX 6000 Ada Generation",
            "12.4",
            "550",
            (8, 9),
            Decimal("48"),
            Decimal("100"),
            "bfloat16",
        )
    )
    unknown = _preflight(FakeCache(), FakeSmoke()).run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal("48"), Decimal("100"), "bfloat16")
    )

    assert matched.card_profile == "RTX 6000 Ada"
    assert "prebuilt card profile" in matched.plan_source
    assert matched.card_profile_note
    assert matched.to_record()["placement_card_profile_note"] == matched.card_profile_note
    assert unknown.card_profile is None
    assert unknown.card_profile_note is None
    assert unknown.plan_source == "computed from measured VRAM"


def test_a_profile_and_a_measurement_that_disagree_keep_the_measurement() -> None:
    """The profile is a planning convenience; the card that arrived is the fact."""

    report = _preflight(FakeCache(), FakeSmoke()).run(
        GpuProfile(
            "RTX PRO 6000 Blackwell",
            "12.4",
            "550",
            (8, 9),
            Decimal("48"),
            Decimal("100"),
            "bfloat16",
        )
    )

    assert report.tier == "generic-48gb"
    assert "prebuilt profile 'RTX PRO 6000 Blackwell' expects tier" in report.plan_source


def test_the_price_sheet_refuses_a_card_nobody_reviewed() -> None:
    table = _table()

    assert table.price_for("NVIDIA RTX 6000 Ada Generation") == Decimal("0.77")
    with pytest.raises(PlacementRefusal, match="no reviewed card_profile"):
        table.price_for("NVIDIA H200")


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ('tier = "generic-48gb"', "does not cover"),
        ('tier = "no-such-tier"', "undefined placement tier"),
        ("hourly_usd = 0.77", "decimal string"),
    ],
)
def test_a_drifted_card_profile_row_refuses_at_load(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "config/pod_placement.toml").read_text(encoding="utf-8")
    key = mutation.split(" =")[0]
    broken = source.replace(
        f'{key} = "0.27"' if key == "hourly_usd" else f'{key} = "generic-24gb"',
        mutation,
        1,
    )
    path = tmp_path / "pod_placement.toml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(PlacementRefusal, match=reason):
        load_placement_table(path)


def test_digest_mismatch_refetches_once_then_refuses_without_substitution() -> None:
    cache = FakeCache(always_mismatch="attestator_1")
    report = _preflight(cache, FakeSmoke()).run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal("48"), Decimal("100"), "bfloat16")
    )

    assert report.color == "red"
    assert cache.refetch_calls["attestator_1"] == 1
    assert cache.verify_calls["attestator_1"] == 2
    assert any(
        issue.code == "cache-mismatch-after-refetch" and issue.chair == "attestator_1"
        for issue in report.issues
    )


def test_smoke_read_failure_is_red_and_names_the_chair() -> None:
    report = _preflight(FakeCache(), FakeSmoke(invalid_chair="attestator_2")).run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal("48"), Decimal("100"), "bfloat16")
    )

    assert report.color == "red"
    assert any(
        issue.chair == "attestator_2" and issue.code == "smoke-output-invalid"
        for issue in report.issues
    )


def test_missing_utilization_is_a_red_measurement_failure() -> None:
    class UnmeasuredSmoke(FakeSmoke):
        def read(self, identity, fixture, placement):  # type: ignore[no-untyped-def]
            return replace(super().read(identity, fixture, placement), utilization=())

    report = _preflight(FakeCache(), UnmeasuredSmoke()).run(
        GpuProfile(
            "synthetic",
            "12.4",
            "550",
            (8, 0),
            Decimal("48"),
            Decimal("100"),
            "bfloat16",
        )
    )

    assert report.color == "red"
    assert all(
        any(issue.code == "utilization-missing" and issue.chair == role for issue in report.issues)
        for role in {
            "designator_structure",
            "attestator_1",
            "attestator_2",
            "attestator_3",
            "perlector",
        }
    )


def test_smoke_receipt_cannot_replace_its_chair_identity() -> None:
    class MisboundSmoke(FakeSmoke):
        def read(self, identity, fixture, placement):  # type: ignore[no-untyped-def]
            return replace(
                super().read(identity, fixture, placement),
                receipt={"chair": "attestator_1", "fixture": fixture.name},
            )

    report = _preflight(FakeCache(), MisboundSmoke()).run(
        GpuProfile(
            "synthetic",
            "12.4",
            "550",
            (8, 0),
            Decimal("48"),
            Decimal("100"),
            "bfloat16",
        )
    )

    assert report.color == "red"
    assert any(issue.code == "smoke-receipt-misbound" for issue in report.issues)


def test_cache_receipt_cannot_replace_runtime_retry_accounting() -> None:
    class MiscountedCache(FakeCache):
        def verify(self, identity):  # type: ignore[no-untyped-def]
            return {"manifest_digest": identity.digest_manifest, "repaired_once": False}

    report = _preflight(MiscountedCache(), FakeSmoke()).run(
        GpuProfile(
            "synthetic",
            "12.4",
            "550",
            (8, 0),
            Decimal("48"),
            Decimal("100"),
            "bfloat16",
        )
    )

    assert report.color == "red"
    assert any(issue.code == "cache-receipt-invalid" for issue in report.issues)


@pytest.mark.parametrize(
    ("profile", "expected_issue"),
    [
        (
            GpuProfile(
                "pre-ampere", "12.4", "550", (7, 5), Decimal("48"), Decimal("100"), "bfloat16"
            ),
            "dtype-floor-failed",
        ),
        (
            GpuProfile(
                "missing-cuda", None, None, (8, 0), Decimal("48"), Decimal("100"), "bfloat16"
            ),
            "cuda-driver-missing",
        ),
        (
            GpuProfile(
                "missing-resources", "12.4", "550", (8, 0), Decimal("0"), Decimal("0"), "bfloat16"
            ),
            "vram-missing",
        ),
    ],
)
def test_preflight_environment_failures_are_red_with_named_remediation(
    profile: GpuProfile, expected_issue: str
) -> None:
    report = _preflight(FakeCache(), FakeSmoke()).run(profile)

    assert report.color == "red"
    assert any(issue.code == expected_issue and issue.remediation for issue in report.issues)
    if expected_issue == "vram-missing":
        assert any(issue.code == "disk-missing" for issue in report.issues)


def test_pod_runtime_checked_in_spend_policy_refuses_paid_actions() -> None:
    from .spend import load_spend_policy

    root = Path(__file__).resolve().parents[2]
    configured = load_spend_policy(root / "config/spend.toml")
    assert not configured.configured


def test_system_gpu_probe_measures_fields_or_returns_red_input_without_a_gpu() -> None:
    class Disk:
        free = 100 * 1024**3

    calls: list[list[str]] = []

    def runner(argv: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        calls.append(argv)
        if len(argv) == 1:
            return subprocess.CompletedProcess(argv, 0, "CUDA Version: 12.4\n", "")
        # nvidia-smi's memory.total is MiB, not GiB: 49152 MiB is a real 48 GiB card.
        return subprocess.CompletedProcess(argv, 0, "Synthetic GPU, 550.54, 49152, 8.0\n", "")

    profile = SystemGpuProbe(disk_path="/", runner=runner, disk_usage=lambda path: Disk()).profile(
        "bfloat16"
    )

    assert profile.name == "Synthetic GPU"
    assert profile.cuda_version == "12.4"
    assert profile.driver_version == "550.54"
    assert profile.compute_capability == (8, 0)
    assert profile.vram_gib == Decimal("48")
    assert profile.disk_gib == Decimal("100")
    assert len(calls) == 2


def test_system_gpu_probe_returns_red_input_when_nvidia_smi_fails() -> None:
    class Disk:
        free = 100 * 1024**3

    def runner(argv: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        return subprocess.CompletedProcess(argv, 1, "", "nvidia-smi: command not found")

    profile = SystemGpuProbe(disk_path="/", runner=runner, disk_usage=lambda path: Disk()).profile(
        "bfloat16"
    )

    assert profile.name == "GPU discovery unavailable"
    assert profile.cuda_version is None
    assert profile.driver_version is None
    assert profile.compute_capability is None
    assert profile.vram_gib == Decimal("0")
    assert profile.disk_gib == Decimal("100")
    assert "nvidia-smi: command not found" in profile.discovery_detail


def test_system_gpu_probe_preserves_a_disk_measurement_error_when_the_gpu_is_fine() -> None:
    """A failed disk read must not discard its own reason (audit-d Finding 4).

    Mirrors the GPU leg of the same method, which already preserves its error
    text into discovery_detail.
    """

    def runner(argv: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        if len(argv) == 1:
            return subprocess.CompletedProcess(argv, 0, "CUDA Version: 12.4\n", "")
        return subprocess.CompletedProcess(argv, 0, "Synthetic GPU, 550.54, 49152, 8.0\n", "")

    def failing_disk_usage(path: Path) -> object:
        raise PermissionError("disk usage refused for /workspace")

    profile = SystemGpuProbe(
        disk_path="/workspace", runner=runner, disk_usage=failing_disk_usage
    ).profile("bfloat16")

    assert profile.disk_gib == Decimal("0")
    assert "disk usage refused for /workspace" in profile.discovery_detail

    report = _preflight(FakeCache(), FakeSmoke()).run(profile)
    disk_issue = next(issue for issue in report.issues if issue.code == "disk-missing")
    assert "disk usage refused for /workspace" in disk_issue.message


def test_a_red_report_carries_what_happened_not_only_what_to_do_next() -> None:
    """Spec 04 asks a red preflight for "what happened, what to do next"."""

    report = _preflight(FakeCache(), FakeSmoke()).run(
        GpuProfile(
            "GPU discovery unavailable",
            None,
            None,
            None,
            Decimal("0"),
            Decimal("100"),
            "bfloat16",
            discovery_detail="RuntimeError: nvidia-smi: command not found",
        )
    )

    assert report.color == "red"
    assert report.to_record()["environment"]["discovery_detail"] == (
        "RuntimeError: nvidia-smi: command not found"
    )
    assert any(
        issue.code == "cuda-driver-missing" and "nvidia-smi: command not found" in issue.message
        for issue in report.issues
    )


def test_a_credential_nested_in_a_receipt_list_is_refused() -> None:
    """A receipt reaches the durable lease and the operator's terminal.

    A harness describing two controllers writes a list, so a scrubber that
    walked only mappings would let real capability material through a shape
    nobody had to be devious to produce.
    """

    from .models import assert_nonsecret_receipt

    # The value is deliberately not key-shaped: the scrubber reads field names,
    # and this repository's own ingress hook refuses a credential-shaped literal
    # in a tracked file — including one written to demonstrate a scrubber.
    with pytest.raises(ValueError, match="credential or token material"):
        assert_nonsecret_receipt({"controllers": [{"api_key": "would be a capability"}]})
    with pytest.raises(ValueError, match="credential or token material"):
        ControllerReadiness(False, START, "armer refused", {"seen": [[{"bearer": "value"}]]})
