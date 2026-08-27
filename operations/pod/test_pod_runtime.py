"""Fake-first acceptance drills for the money path.

These tests deliberately break each load-bearing guard.  A passing happy path
without the paired red path would not establish that the guard is wired.
"""

from __future__ import annotations

import inspect
import itertools
import json
import subprocess
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.errors import DigestMismatchRefusal

from . import cli
from . import launch as launch_module
from .arming import ControllerArming, ControllerReadiness
from .bootstrap import (
    BootstrapActions,
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapStep,
    BootstrapStepFailure,
    ModelStoreBootstrapAction,
    SubprocessBootstrapActions,
)
from .controllers import ControllerResult, ControllerState, LaptopSupervisor, PodDeadmanTimer
from .fake_provider import FakeProvider
from .launch import (
    SPEND_ALERT_DEBOUNCE_SECONDS,
    LaunchResult,
    LaunchState,
    PodRuntime,
    _bind_report_path_to_launch,
)
from .lease import LeaseStore, PodLease
from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    AbsenceObservation,
    AccountBalanceObservation,
    BillingState,
    CloseState,
    CostCapture,
    CostLine,
    LeaseFormatError,
    PodCreateRequest,
    PodRecord,
    ProviderFailure,
    ProviderStatus,
    SpendRefusal,
)
from .notify_bridge import NotifyOutcome, shell_notifier, silent
from .pod_timer import TimerContext, _persist_or_close, run_with_bootstrap
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
from .spend import (
    CHALLENGE_BYTES,
    CONFIRMATION_PREFIX,
    SpendAssessment,
    SpendPolicy,
    confirmation_phrase,
    mint_challenge,
)

UTC = timezone.utc
START = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

# The fake price sheet's `fake-48gb` row, and the one name `request()` builds.
# The typed phrase is derived from the displayed price, so these tests spell it
# out from the same inputs the preview would rather than reuse a constant.
POD_HOURLY = Decimal("0.77")
VOLUME_HOURLY = Decimal("0.05")

# A real challenge is unpredictable by design, which a test cannot spell out in advance.
# These tests pin it instead, so the phrase stays computable here; the property that it
# is unguessable in production belongs to `mint_challenge`, which is tested separately.
TEST_CHALLENGE = "0F1E2D3C4B5A6978"
CREATE_CONFIRMATION = confirmation_phrase(
    "create", "pod-runtime-test", POD_HOURLY, VOLUME_HOURLY, TEST_CHALLENGE
)


def adopt_confirmation(pod_id: str) -> str:
    return confirmation_phrase("adopt", pod_id, POD_HOURLY, VOLUME_HOURLY, TEST_CHALLENGE)


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


def policy(
    *,
    hourly: str = "1.00",
    lifetime: int = 3600,
    balance_floor: str = "50.00",
    balance_alert: str = "75.00",
    cutoff_margin: int = 3600,
) -> SpendPolicy:
    return SpendPolicy(
        state="configured",
        max_hourly_usd=Decimal(hourly),
        max_estimated_metered_cost_usd=Decimal("2.00"),
        account_balance_floor_usd=Decimal(balance_floor),
        account_balance_alert_usd=Decimal(balance_alert),
        hard_lifetime_seconds=lifetime,
        laptop_heartbeat_timeout_seconds=30,
        shutdown_poll_interval_seconds=1,
        shutdown_deadline_seconds=5,
        billing_cutoff_margin_seconds=cutoff_margin,
    )


@pytest.mark.parametrize("margin", [-1, True, 1.5, "3600", 3601])
def test_spend_policy_refuses_an_out_of_bounds_billing_cutoff_margin(margin: object) -> None:
    with pytest.raises(SpendRefusal, match="billing cutoff margin"):
        replace(policy(), billing_cutoff_margin_seconds=margin)


@pytest.mark.parametrize(
    "field",
    ["account_balance_floor_usd", "account_balance_alert_usd", "billing_cutoff_margin_seconds"],
)
def test_configured_spend_policy_refuses_each_required_new_field(field: str) -> None:
    with pytest.raises(SpendRefusal, match="missing a required ceiling"):
        replace(policy(), **{field: None})


def test_nonpositive_balance_alert_refusal_names_the_alert() -> None:
    with pytest.raises(SpendRefusal, match="^account balance alert must be positive$"):
        replace(policy(), account_balance_alert_usd=Decimal("0"))


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
        metadata={BILLING_CUTOFF_MARGIN_ENV: "3600"},
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


def shutdown(
    provider: FakeProvider, clock: Clock, *, timeout: float = 8, cutoff_margin: int = 3600
) -> VerifiedShutdown:
    return VerifiedShutdown(
        provider,
        timeout_seconds=timeout,
        poll_seconds=1,
        billing_cutoff_margin_seconds=cutoff_margin,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )


class PreviewingRuntime(PodRuntime):
    """A runtime that previews before it confirms, exactly as ``cli.main`` does.

    The paid gate only accepts a challenge some preview actually issued, so a bare
    ``create`` with a typed phrase is refused. Most tests below are about leases,
    arming, timers or shutdown rather than about the gate, and staging a preview in
    each of them would bury what they are testing. This stages it once.

    Tests that *are* about the gate use `PodRuntime` directly, so nothing here can
    hide a missing challenge: see the no-preview and replay tests.
    """

    def create(self, request: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        self.preview_create(request)
        return super().create(request, confirmation=confirmation)

    def adopt(
        self, pod_id: str, *, expected: PodCreateRequest, confirmation: str | None
    ) -> LaunchResult:
        self.preview_adopt(pod_id, expected=expected)
        return super().adopt(pod_id, expected=expected, confirmation=confirmation)


def runtime(
    provider: FakeProvider,
    clock: Clock,
    root: Path,
    *,
    spend: SpendPolicy | None = None,
    armer: FakeControllerArmer | None = None,
    notifier=None,
) -> PodRuntime:
    tokens = iter(("a" * 32, "b" * 32))
    return PreviewingRuntime(
        provider,
        provider_name="fake",
        spend_policy=spend or policy(),
        lease_root=root,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        challenge_factory=lambda: TEST_CHALLENGE,
        controller_armer=armer or FakeControllerArmer(clock, provider),
        notifier=notifier or (lambda message: NotifyOutcome(False, False, "test silent")),
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
    # The refusal must not hand back the still-spendable phrase: a typo does
    # not burn the preview, so the report carries a challenge-free preview and
    # the operator retypes from the preview they were shown.
    assert refused_adopt.preview.challenge is None
    assert refused_adopt.preview.to_record()["confirmation_phrase"] is None
    assert adopt_confirmation(existing.pod_id) not in refused_adopt.detail
    assert list(tmp_path.glob("*.json")) == []


def bare_runtime(
    provider: FakeProvider, clock: Clock, root: Path, *, challenge: str = TEST_CHALLENGE
) -> PodRuntime:
    """A runtime that does *not* preview for you, for testing the gate itself.

    Tokens are unbounded rather than a pair: the concurrency test needs a second create
    to run far enough to be *counted* when the gate leaks, so that its assertion names
    the two pods instead of the fixture dying of `StopIteration` first.
    """

    tokens = (f"{index:032x}" for index in itertools.count())
    return PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=root,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        challenge_factory=lambda: challenge,
        controller_armer=FakeControllerArmer(clock, provider),
    )


def test_a_create_nobody_previewed_is_refused_even_with_the_derived_phrase(
    tmp_path: Path,
) -> None:
    """The price is public; being shown a preview is not.

    Every component of the old phrase came from the price sheet, so any caller able to
    read the config could compute it and spend money without a human ever seeing the
    preview. The challenge is what closes that: with no preview issued in this run there
    is no phrase to derive, and `create` must refuse before it reaches the provider.
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)

    result = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_CONFIRMATION
    assert not result.green
    assert "no preview in this run issued a challenge" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls), "a pod was created anyway"
    assert list(tmp_path.glob("*.json")) == []


def test_one_challenge_authorizes_exactly_one_paid_create(tmp_path: Path) -> None:
    """A confirmation is spent when it is used, so a replay buys nothing.

    Without this, one preview would stand as open authorization for as many pods as a
    caller cared to create -- each of them billing by the hour.
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)
    create_request = request(clock)

    pod_runtime.preview_create(create_request)
    first = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)
    assert first.state is LaunchState.CREATED_GUARDED

    created = sum(1 for verb, _ in provider.calls if verb == "create")
    replay = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)

    assert replay.state is LaunchState.REFUSED_CONFIRMATION
    assert "no preview in this run issued a challenge" in replay.detail
    assert sum(1 for verb, _ in provider.calls if verb == "create") == created


def test_a_confirmation_from_a_different_preview_names_itself(tmp_path: Path) -> None:
    """A stale challenge is not a typo, and must not be reported as one."""

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path, challenge="FFFFFFFFFFFFFFFF")
    create_request = request(clock)

    pod_runtime.preview_create(create_request)
    # CREATE_CONFIRMATION names TEST_CHALLENGE; this runtime issued a different one.
    result = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_CONFIRMATION
    assert "different preview challenge" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_a_challenge_does_not_authorize_a_longer_lifetime_than_was_previewed(
    tmp_path: Path,
) -> None:
    """The phrase names the rates, which do not change with lifetime.

    Preview ten minutes, then create the same pod at the ceiling: the action, subject and
    both hourly rates are identical, so the printed phrase still matches. Only the
    deadline moved, and the deadline is what the money actually depends on. The ceiling
    bounds the exposure; it does not make the authorization cover it.
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)

    short = request(clock, lifetime=600)
    pod_runtime.preview_create(short)
    longer = replace(short, hard_deadline=short.hard_deadline + timedelta(seconds=1800))

    result = pod_runtime.create(longer, confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_CONFIRMATION
    assert "at this hard deadline" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)
    # The preview that was actually issued still works, so this refuses the substitution
    # rather than the mechanism.
    assert pod_runtime.create(short, confirmation=CREATE_CONFIRMATION).state is (
        LaunchState.CREATED_GUARDED
    )


def test_an_adoption_nobody_previewed_is_refused(tmp_path: Path) -> None:
    """`adopt` carries its own copy of the gate, so it needs its own coverage.

    An adopted pod is already billing, which makes an unauthorized adoption worse than an
    unauthorized create, not better.
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)
    expected = request(clock)
    existing = provider.create(expected)

    result = pod_runtime.adopt(
        existing.pod_id, expected=expected, confirmation=adopt_confirmation(existing.pod_id)
    )

    assert result.state is LaunchState.REFUSED_CONFIRMATION
    assert "no preview in this run issued a challenge" in result.detail
    assert list(tmp_path.glob("*.json")) == []


def test_one_challenge_authorizes_exactly_one_adoption(tmp_path: Path) -> None:
    """The replay guard on the adopt side, which `create`'s test cannot cover."""

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)
    expected = request(clock)
    existing = provider.create(expected)
    phrase = adopt_confirmation(existing.pod_id)

    pod_runtime.preview_adopt(existing.pod_id, expected=expected)
    first = pod_runtime.adopt(existing.pod_id, expected=expected, confirmation=phrase)
    assert first.state is LaunchState.ADOPTED_GUARDED

    replay = pod_runtime.adopt(existing.pod_id, expected=expected, confirmation=phrase)

    assert replay.state is LaunchState.REFUSED_CONFIRMATION
    assert "no preview in this run issued a challenge" in replay.detail


def test_two_overlapping_creates_spend_one_challenge_exactly_once(tmp_path: Path) -> None:
    """One confirmation must buy one pod even when two callers race for it.

    Validating the challenge and consuming it used to be separate steps, so both callers
    could read the same outstanding challenge, both pass, and both reach the provider --
    two billing pods from one authorization. A sequential test cannot see that, because
    the second call only fails once the first has already finished consuming.

    The barrier makes the overlap real rather than hoped for: neither thread proceeds
    past the claim until both have arrived at it.
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)
    create_request = request(clock)
    pod_runtime.preview_create(create_request)

    # Hold the window open *inside* the claim, between reading the challenge and
    # consuming it, which is where the old code was interruptible. A barrier on entry to
    # `_claim_challenge` is not enough: one thread still finishes the whole read-and-
    # delete before the other starts, so the broken version passes it.
    #
    # `require_confirmation` runs at exactly that point, so the first caller to reach it
    # waits for a second to arrive. Without the lock both threads get there and both
    # spend the same challenge. With it, the second is still blocked acquiring the lock,
    # the wait expires, and the first proceeds alone — so the fixed code does not
    # deadlock, it just pauses once.
    monkeypatched = launch_module.require_confirmation
    gate = threading.Lock()
    first_here = threading.Event()
    second_here = threading.Event()
    # Generous on purpose. Detection of a re-introduced leak depends on the second
    # thread reaching this window while the first is still in it, so the wait has to
    # outlast any plausible scheduling delay -- a loaded machine, a coverage hook. Too
    # short and a leaking gate looks fixed. The correct code pays this wait once per
    # run, because its second thread is blocked on the lock and never arrives.
    rendezvous_seconds = 5.0
    observed: list[bool] = []

    def rendezvous_then_confirm(typed, expected):
        with gate:
            is_first = not first_here.is_set()
            first_here.set()
        if is_first:
            observed.append(second_here.wait(timeout=rendezvous_seconds))
        else:
            second_here.set()
        return monkeypatched(typed, expected)

    launch_module.require_confirmation = rendezvous_then_confirm
    results: list[LaunchResult] = []
    lock = threading.Lock()

    def attempt() -> None:
        outcome = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        launch_module.require_confirmation = monkeypatched

    assert len(results) == 2
    granted = [outcome for outcome in results if outcome.state is LaunchState.CREATED_GUARDED]
    refused = [outcome for outcome in results if outcome.state is LaunchState.REFUSED_CONFIRMATION]
    assert len(granted) == 1, "one confirmation authorized more than one pod"
    assert len(refused) == 1, f"unexpected outcomes: {[r.state for r in results]}"
    assert sum(1 for verb, _ in provider.calls if verb == "create") == 1, "two pods were created"
    # The staging itself, asserted rather than assumed. `Event.wait` returns False only
    # on timeout, which is what must happen here: the second caller is held on the lock
    # and never reaches the consume window. A True would mean both callers were inside
    # it together and the run above proved nothing about the lock.
    assert observed == [False], (
        "the second caller reached the consume window, so this run did not exercise "
        f"the lock it claims to test (rendezvous returned {observed})"
    )


def test_minted_challenges_are_unpredictable_and_do_not_repeat() -> None:
    """The gate rests on this: a guessable challenge is no challenge at all."""

    minted = {mint_challenge() for _ in range(256)}

    assert len(minted) == 256, "mint_challenge repeated a value"
    assert all(len(value) == CHALLENGE_BYTES * 2 for value in minted)
    assert all(set(value) <= set("0123456789ABCDEF") for value in minted)


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
    provider.now = lambda: datetime.now(UTC)
    armer = FakeControllerArmer(clock, provider)
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        "\n".join(
            (
                'schema = "pod-spend.v2"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                'account_balance_floor_usd = "50.00"',
                'account_balance_alert_usd = "75.00"',
                "hard_lifetime_seconds = 3600",
                "laptop_heartbeat_timeout_seconds = 30",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 5",
                "billing_cutoff_margin_seconds = 3600",
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
    assert "account_balance_floor_usd" in observed["prior_output"]
    assert "billing_cutoff_margin_seconds" in observed["prior_output"]
    assert CONFIRMATION_PREFIX in observed["prompt"]
    # The prompt carries the phrase for *this* pod at *this* price — not a
    # constant an operator could have typed without reading the preview above.
    # The real CLI mints a random challenge, so the price-bearing head is checked
    # literally and the challenge is checked for being present and non-empty: an
    # unguessable tail is exactly the part a test cannot spell out in advance.
    head = confirmation_phrase(
        "create", "cli-preview-test", POD_HOURLY, VOLUME_HOURLY, "X"
    ).removesuffix(" CHALLENGE X")
    assert head in observed["prompt"]
    challenge = observed["prompt"].split(" CHALLENGE ", 1)[1].split("'", 1)[0].strip()
    assert len(challenge) == CHALLENGE_BYTES * 2, "the CLI prompt carried no live challenge"
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
    # A ceiling refusal's challenge is unspent, so its report must not carry
    # the still-live phrase -- the same policy as a confirmation refusal.
    assert create.preview is not None and create.preview.challenge is None
    assert create.preview.to_record()["confirmation_phrase"] is None

    existing = provider.create(create_request)
    adopt = pod_runtime.adopt(
        existing.pod_id,
        expected=create_request,
        confirmation=adopt_confirmation(existing.pod_id),
    )
    assert adopt.state is LaunchState.REFUSED_CEILING
    assert adopt.preview is not None and adopt.preview.challenge is None
    assert list(tmp_path.glob("*.json")) == []


def test_observed_balance_at_or_below_hard_floor_refuses_create_and_adopt_before_arming(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("49.99")
    create_request = request(clock)

    refused_create = runtime(provider, clock, tmp_path).create(
        create_request, confirmation=CREATE_CONFIRMATION
    )

    assert refused_create.state is LaunchState.REFUSED_BALANCE_FLOOR
    assert "hard floor" in refused_create.detail
    assert not any(verb == "create" for verb, _ in provider.calls)
    assert list(tmp_path.glob("*.json")) == []

    existing = provider.create(create_request)
    refused_adopt = runtime(provider, clock, tmp_path).adopt(
        existing.pod_id, expected=create_request, confirmation=adopt_confirmation(existing.pod_id)
    )
    assert refused_adopt.state is LaunchState.REFUSED_BALANCE_FLOOR
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize(
    "error",
    [
        ProviderFailure("RunPod account balance request timed out"),
        ProviderFailure("RunPod account balance response could not be parsed"),
        ValueError("malformed balance payload"),
    ],
)
def test_unobservable_balance_at_the_actual_gate_fails_closed_even_after_a_clean_preview(
    tmp_path: Path, error: BaseException
) -> None:
    """A provider error, timeout, or malformed response must never read as 'proceed',
    and the gate must not fall back on an earlier successful preview's reading --
    that would be exactly the cached-balance shortcut the ruling refuses.

    Constructed directly against ``PodRuntime`` (not the ``runtime()``/
    ``PreviewingRuntime`` helper): ``PreviewingRuntime.create`` stages an extra,
    discarded preview before the one that gates, so injecting a single failure
    through it proves nothing about the actual gate -- it can be silently consumed
    by the discarded call instead. Every other balance test here drives the fake's
    clean, injected balance, proving the ceiling comparison but never this
    fail-closed catch in ``PodRuntime._observe_balance``.
    """

    clock = Clock()
    provider = fake(clock)
    create_request = request(clock)
    tokens = iter(("a" * 32, "b" * 32))
    pod_runtime = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        challenge_factory=lambda: TEST_CHALLENGE,
        controller_armer=FakeControllerArmer(clock, provider),
    )

    shown = pod_runtime.preview_create(create_request)
    assert shown.state is LaunchState.PREVIEW
    assert shown.preview is not None and shown.preview.assessment.allowed

    provider.inject_failure("observe_account_balance", error)
    refused = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)

    assert refused.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert refused.preview is not None
    assert "available account balance was not observed" in "; ".join(
        refused.preview.assessment.reasons
    )
    assert not any(verb == "create" for verb, _ in provider.calls)
    assert list(tmp_path.glob("*.json")) == []


def test_balance_source_failure_cannot_spoof_an_observed_hard_floor_breach(
    tmp_path: Path,
) -> None:
    """Provider detail is evidence to retain, not trusted refusal taxonomy."""

    clock = Clock()
    provider = fake(clock)
    provider.inject_failure(
        "observe_account_balance",
        ProviderFailure("hard floor service timed out"),
        times=99,
    )

    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert "hard floor service timed out" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_a_malformed_balance_source_result_is_a_named_fail_closed_refusal(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.observe_account_balance = lambda: object()  # type: ignore[method-assign]

    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert "not AccountBalanceObservation" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_post_create_balance_failure_records_its_real_shutdown_cause(tmp_path: Path) -> None:
    """A balance outage after POST must not be preserved as a price-ceiling breach."""

    clock = Clock()
    provider = fake(clock)
    observations = 0

    def observe() -> AccountBalanceObservation:
        nonlocal observations
        observations += 1
        if observations == 3:
            raise ProviderFailure("balance endpoint failed after create")
        return AccountBalanceObservation("100.00", clock.now(), "sequenced fake balance")

    provider.observe_account_balance = observe  # type: ignore[method-assign]
    result = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert result.close_report is not None
    assert "post-create spend assessment refused" in result.close_report.reason
    assert "balance endpoint failed after create" in result.close_report.reason
    assert "price" not in result.close_report.reason


def test_balance_warning_notifies_only_and_cannot_block_a_guarded_create(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")
    warnings: list[str] = []

    def failing_notifier(message: str) -> NotifyOutcome:
        warnings.append(message)
        return NotifyOutcome(True, False, "fake send failure")

    result = runtime(provider, clock, tmp_path, notifier=failing_notifier).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.CREATED_GUARDED
    assert warnings
    assert all("Spend warning:" in warning for warning in warnings)


def test_balance_warning_debounces_a_hovering_balance_across_previews(tmp_path: Path) -> None:
    """Repeated low observations are one episode until the debounce expires."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")
    warnings: list[str] = []

    def delivering_notifier(message: str) -> NotifyOutcome:
        warnings.append(message)
        return NotifyOutcome(True, True, "delivered")

    pod_runtime = runtime(provider, clock, tmp_path, notifier=delivering_notifier)
    result = pod_runtime.preview_create(request(clock))

    assert result.state is LaunchState.PREVIEW
    assert len(warnings) == 1

    suppressed = pod_runtime.preview_create(request(clock))
    assert len(warnings) == 1
    assert suppressed.preview is not None
    assert suppressed.preview.assessment.alert_notifications == (
        "Phone notification: not sent (a delivered warning for this action and subject "
        "is still inside the debounce window).",
    )

    clock.sleep(SPEND_ALERT_DEBOUNCE_SECONDS + 1)
    pod_runtime.preview_create(request(clock))
    assert len(warnings) == 2


def test_a_recovered_balance_then_a_second_crossing_notifies_inside_the_debounce_window(
    tmp_path: Path,
) -> None:
    """Duplicate samples are noise; a new low-balance episode is not.

    Two consecutive safe observations establish recovery.  The next drop is a
    new crossing and must reach the phone even though the earlier episode's
    delivery stamp is still inside the ordinary fifteen-minute window.
    """

    clock = Clock()
    provider = fake(clock)
    warnings: list[str] = []

    def preview() -> None:
        # Reconstruct the runtime each time so this proves the durable state a
        # second CLI process would read, not merely an in-memory transition.
        runtime(
            provider,
            clock,
            tmp_path,
            notifier=lambda message: (
                warnings.append(message),
                NotifyOutcome(True, True, "ok"),
            )[1],
        ).preview_create(request(clock))

    provider.set_account_balance("60.00")
    preview()
    provider.set_account_balance("80.00")
    preview()
    preview()
    provider.set_account_balance("60.00")
    preview()

    assert len(warnings) == 2
    assert clock.seconds < SPEND_ALERT_DEBOUNCE_SECONDS


def test_a_flapping_balance_does_not_rearm_or_flood_the_phone(tmp_path: Path) -> None:
    """One safe sample is not a recovery when the next sample falls straight back."""

    clock = Clock()
    provider = fake(clock)
    warnings: list[str] = []

    for balance in ("60.00", "80.00", "60.00", "80.00", "60.00", "80.00", "60.00"):
        provider.set_account_balance(balance)
        runtime(
            provider,
            clock,
            tmp_path,
            notifier=lambda message: (
                warnings.append(message),
                NotifyOutcome(True, True, "ok"),
            )[1],
        ).preview_create(request(clock))

    assert len(warnings) == 1


def test_a_post_create_threshold_crossing_is_delivered_and_recorded(tmp_path: Path) -> None:
    """The provider-returned price gate reads balance a third time after POST.

    If that observation is the first one below the alert threshold, it is still
    a real crossing.  The guarded result must retain the delivery evidence rather
    than returning the pre-create assessment that saw only a safe balance.
    """

    clock = Clock()
    provider = fake(clock)
    balances = iter(("100.00", "100.00", "60.00"))
    warnings: list[str] = []

    def observe() -> AccountBalanceObservation:
        return AccountBalanceObservation(next(balances), clock.now(), "sequenced fake balance")

    provider.observe_account_balance = observe  # type: ignore[method-assign]
    result = runtime(
        provider,
        clock,
        tmp_path,
        notifier=lambda message: (warnings.append(message), NotifyOutcome(True, True, "ok"))[1],
    ).create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.CREATED_GUARDED
    assert len(warnings) == 1
    assert result.preview is not None
    ceilings = result.preview.to_record()["spend"]["ceilings"]  # type: ignore[index]
    assert ceilings["account_balance_observation"]["available_usd"] == "60.00"  # type: ignore[index]
    assert ceilings["alert_notifications"] == ["Phone notification: sent."]  # type: ignore[index]


def test_balance_warning_debounce_is_scoped_per_action_and_subject(tmp_path: Path) -> None:
    """A hovering balance on one pod must not silence the warning for a different one."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")
    warnings: list[str] = []

    def delivering_notifier(message: str) -> NotifyOutcome:
        warnings.append(message)
        return NotifyOutcome(True, True, "delivered")

    pod_runtime = runtime(provider, clock, tmp_path, notifier=delivering_notifier)
    pod_runtime.preview_create(request(clock))
    assert len(warnings) == 1

    other_request = replace(request(clock), name="pod-runtime-test-other")
    pod_runtime.preview_create(other_request)
    assert len(warnings) == 2


def test_a_corrupted_or_out_of_range_spend_alert_stamp_never_crashes_the_preview(
    tmp_path: Path,
) -> None:
    """The debounce stamp is untrusted input the moment it is read back from disk;
    a malformed or absurd value must be treated the same as a missing one -- sent
    again, never a crash. A crash here would break the display-only preview, not
    the spend decision, but ``_preview`` must still be able to return."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")
    warnings: list[str] = []

    def delivering_notifier(message: str) -> NotifyOutcome:
        warnings.append(message)
        return NotifyOutcome(True, True, "delivered")

    pod_runtime = runtime(provider, clock, tmp_path, notifier=delivering_notifier)
    pod_runtime.preview_create(request(clock))
    assert len(warnings) == 1

    stamp = pod_runtime._spend_alert_stamp_path("create", "pod-runtime-test")
    stamp.write_text("not-a-number", encoding="utf-8")
    result = pod_runtime.preview_create(request(clock))
    assert result.state is LaunchState.PREVIEW
    assert len(warnings) == 2

    stamp.write_text("9" * 30, encoding="utf-8")  # far outside datetime's representable range
    result = pod_runtime.preview_create(request(clock))
    assert result.state is LaunchState.PREVIEW
    assert len(warnings) == 3

    stamp.write_bytes(b"\xff")
    result = pod_runtime.preview_create(request(clock))
    assert result.state is LaunchState.PREVIEW
    assert len(warnings) == 4


def test_an_oversized_spend_alert_stamp_cannot_silence_the_low_balance_page(
    tmp_path: Path,
) -> None:
    """A stamp too long to parse must send, not be lost behind a raised parse error.

    ``_load_spend_alert_state`` promises that corrupt evidence reverts to
    sending, but it read the whole file and handed it to ``int``, which refuses a
    decimal string past ``sys.int_max_str_digits`` with a ``ValueError`` rather
    than answering.  That escaped the loader, so every later gate for this action
    and subject recorded a delivery failure instead of paging: one file, written
    once, silences the low-balance warning permanently.  The stamp is written
    into the same directory a paid run writes leases into, so its size is
    untrusted exactly as its content is.
    """

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")
    warnings: list[str] = []

    def delivering_notifier(message: str) -> NotifyOutcome:
        warnings.append(message)
        return NotifyOutcome(True, True, "delivered")

    pod_runtime = runtime(provider, clock, tmp_path, notifier=delivering_notifier)
    pod_runtime.preview_create(request(clock))
    assert len(warnings) == 1

    stamp = pod_runtime._spend_alert_stamp_path("create", "pod-runtime-test")
    stamp.write_text("9" * 5000, encoding="utf-8")

    result = pod_runtime.preview_create(request(clock))

    assert result.state is LaunchState.PREVIEW
    assert len(warnings) == 2
    assert result.preview is not None
    assert result.preview.assessment.alert_notifications == ("Phone notification: sent.",)


def test_an_unreadable_lease_refuses_the_paid_gate_instead_of_raising(tmp_path: Path) -> None:
    """Unknown remaining liability is a named refusal, never an escaping OSError.

    ``Path.is_symlink`` re-raises a permission error rather than answering
    ``False``, so a lease directory the process may list but not stat threw
    straight out of ``create`` -- past the ``_SpendGateLockFailure`` catch, past
    every refusal state, and out of the operator surface as a bare traceback.
    Failing closed under its own name is the whole point of the reserved-
    liability read.
    """

    clock = Clock()
    provider = fake(clock)
    leases = tmp_path / "leases"
    leases.mkdir()
    (leases / ("c" * 32 + ".json")).write_text("{}", encoding="utf-8")
    pod_runtime = runtime(provider, clock, leases)
    leases.chmod(0o600)  # listable, not statable: the lease cannot be read or ruled out
    try:
        result = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)
    finally:
        leases.chmod(0o700)

    assert result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    # The spend surface bounds refusal reasons at 160 characters, so a long
    # tmp-path lease name can truncate the trailing "could not be read" phrase;
    # the lease path itself surviving in the detail proves the same cause.
    assert "could not be read" in result.detail or "lease" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_a_post_create_assessment_fault_still_closes_the_billing_pod(tmp_path: Path) -> None:
    """A pod exists by this line; an escaping exception would leave it billing.

    The re-assessment after POST is the last money gate, and its refusal path
    closes the pod.  A fault *inside* it had no path at all: the exception left
    ``create`` with no close attempted, no close report, and no result state for
    the operator to act on -- the one shape this runtime exists to prevent.
    """

    clock = Clock()
    provider = fake(clock)
    pod_runtime = runtime(provider, clock, tmp_path)
    unfaulted = pod_runtime._assess
    assessments = itertools.count()

    def flaky(*args: object, **kwargs: object) -> SpendAssessment:
        if next(assessments) >= 2:  # the two previews pass; the post-create read does not
            raise PermissionError("lease directory became unstatable after the pod existed")
        return unfaulted(*args, **kwargs)  # type: ignore[arg-type]

    pod_runtime._assess = flaky  # type: ignore[method-assign]

    result = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert result.record is not None
    assert "could not be completed" in result.detail
    assert result.close_report is not None
    assert "could not be completed" in result.close_report.reason
    assert provider.terminate_calls == [result.record.pod_id]
    # A refusal report never carries a phrase that would authorize anything.
    assert result.preview is not None
    assert result.preview.to_record()["confirmation_phrase"] is None


def test_a_balance_exactly_at_the_hard_floor_refuses(tmp_path: Path) -> None:
    """The reserve survives only when the available balance is strictly above it."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("50.00")

    refused = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert refused.state is LaunchState.REFUSED_BALANCE_FLOOR
    assert "at or below the hard floor" in refused.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


@pytest.mark.parametrize("offset_seconds", [-61, 1])
def test_a_stale_or_future_balance_observation_cannot_authorize_a_paid_action(
    tmp_path: Path, offset_seconds: int
) -> None:
    clock = Clock()
    provider = fake(clock)

    def observe() -> AccountBalanceObservation:
        return AccountBalanceObservation(
            "100.00",
            clock.now() + timedelta(seconds=offset_seconds),
            "misdated fake balance",
        )

    provider.observe_account_balance = observe  # type: ignore[method-assign]
    refused = runtime(provider, clock, tmp_path).create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert refused.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert "balance observation" in refused.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_concurrent_paid_actions_reserve_each_others_maximum_liability(tmp_path: Path) -> None:
    """Two individually affordable runs must not jointly spend through the floor."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("51.50")
    spend = policy(balance_floor="50.00", balance_alert="51.00")
    first_request = request(clock, lifetime=3600)
    second_request = replace(first_request, name="pod-runtime-test-other")
    first = bare_runtime(provider, clock, tmp_path)
    second = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=spend,
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: "f" * 32,
        challenge_factory=lambda: TEST_CHALLENGE,
        controller_armer=FakeControllerArmer(clock, provider),
    )
    first.spend_policy = spend
    first.preview_create(first_request)
    second.preview_create(second_request)
    barrier = threading.Barrier(2)
    results: list[LaunchResult] = []

    def launch(pod_runtime: PodRuntime, create_request: PodCreateRequest, phrase: str) -> None:
        barrier.wait(timeout=5)
        results.append(pod_runtime.create(create_request, confirmation=phrase))

    threads = (
        threading.Thread(target=launch, args=(first, first_request, CREATE_CONFIRMATION)),
        threading.Thread(
            target=launch,
            args=(
                second,
                second_request,
                confirmation_phrase(
                    "create", second_request.name, POD_HOURLY, VOLUME_HOURLY, TEST_CHALLENGE
                ),
            ),
        ),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sum(result.state is LaunchState.CREATED_GUARDED for result in results) == 1
    refused = next(result for result in results if result.state is not LaunchState.CREATED_GUARDED)
    assert refused.state is LaunchState.REFUSED_BALANCE_FLOOR
    assert "reserved" in refused.detail


def test_spend_gate_serializes_aliases_of_the_same_lease_root(tmp_path: Path) -> None:
    """A symlink spelling must not give one liability root a second lock."""

    clock = Clock()
    provider = fake(clock)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    alias = tmp_path / "lease-alias"
    alias.symlink_to(lease_root, target_is_directory=True)
    direct_runtime = bare_runtime(provider, clock, lease_root)
    alias_runtime = bare_runtime(provider, clock, alias)
    attempting = threading.Event()
    entered = threading.Event()

    def acquire_alias() -> None:
        attempting.set()
        with alias_runtime._spend_gate_lock():
            entered.set()

    with direct_runtime._spend_gate_lock():
        contender = threading.Thread(target=acquire_alias)
        contender.start()
        assert attempting.wait(timeout=1)
        assert not entered.wait(timeout=0.1)

    contender.join(timeout=2)
    assert entered.is_set()
    assert not contender.is_alive()


def test_only_a_real_spend_lock_failure_is_named_as_a_lock_refusal(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("plain file", encoding="utf-8")

    result = runtime(provider, clock, blocked_parent / "leases").create(
        request(clock), confirmation=CREATE_CONFIRMATION
    )

    assert result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert "spend-reservation lock failed" in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_token_source_failure_is_not_mislabeled_as_a_spend_lock_failure(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    pod_runtime = bare_runtime(provider, clock, tmp_path)

    def fail_token() -> str:
        raise OSError("entropy source unavailable")

    pod_runtime.token_factory = fail_token
    pod_runtime.preview_create(request(clock))
    result = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.LEASE_FAILURE
    assert "could not mint lease identity: entropy source unavailable" in result.detail
    assert "lock failed" not in result.detail
    assert not any(verb == "create" for verb, _ in provider.calls)


def test_a_balance_exactly_at_the_alert_threshold_warns_without_blocking(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("75.00")
    warnings: list[str] = []

    result = runtime(
        provider,
        clock,
        tmp_path,
        notifier=lambda message: (warnings.append(message), NotifyOutcome(True, True, "ok"))[1],
    ).create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.CREATED_GUARDED
    assert len(warnings) == 1


def test_a_run_that_would_spend_through_the_hard_floor_is_refused_before_it_starts(
    tmp_path: Path,
) -> None:
    """The floor is a reserve that has to survive the run, not just start it.

    The balance here is above *both* the floor and the warning threshold, but
    the run's maximum liability would leave the account below the reserve that
    keeps the network volume alive while data is secured. The create gate must
    therefore refuse before the provider sees a paid action.
    """

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("100.00")  # above the floor and above the alert
    warnings: list[str] = []
    hourly_run = request(clock, lifetime=3600)  # $0.77 + $0.05 for one hour

    refused = runtime(
        provider,
        clock,
        tmp_path,
        spend=policy(balance_floor="99.50", balance_alert="99.75"),
        notifier=lambda message: (warnings.append(message), NotifyOutcome(True, True, "ok"))[1],
    ).create(hourly_run, confirmation=CREATE_CONFIRMATION)

    assert refused.state is LaunchState.REFUSED_BALANCE_FLOOR
    assert "would take the observed account balance to or below the hard floor" in refused.detail
    assert warnings == []  # this is the floor stop, not a second warning threshold
    assert not any(verb == "create" for verb, _ in provider.calls)
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize(
    ("balance", "state"),
    [
        ("100.82", LaunchState.REFUSED_BALANCE_FLOOR),  # lands exactly on the floor
        ("100.83", LaunchState.CREATED_GUARDED),  # one cent of reserve survives
    ],
)
def test_the_projected_floor_comparison_is_exact_to_the_cent(
    tmp_path: Path, balance: str, state: LaunchState
) -> None:
    """Decimal money, not binary floats: $100.82 - $0.82 is exactly the floor."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance(balance)

    result = runtime(
        provider,
        clock,
        tmp_path,
        spend=policy(balance_floor="100.00", balance_alert="100.50"),
    ).create(request(clock, lifetime=3600), confirmation=CREATE_CONFIRMATION)

    assert result.state is state


@pytest.mark.parametrize(
    ("provider_error", "expected"),
    [
        (ProviderFailure("RunPod account balance request timed out"), "request timed out"),
        (ValueError("malformed balance payload"), "malformed balance payload"),
    ],
)
def test_an_unobservable_balance_records_why_it_could_not_be_read(
    tmp_path: Path, provider_error: BaseException, expected: str
) -> None:
    """ "Not observed" alone cannot be triaged. GOVERNANCE 2 is about the reason
    as much as the result: a missing configured source, a timeout, and a response
    nobody could parse call for three different actions, and the refusal has to
    say which one happened rather than swallowing the provider's own words."""

    clock = Clock()
    provider = fake(clock)
    provider.inject_failure("observe_account_balance", provider_error, times=99)

    refused = runtime(provider, clock, tmp_path).preview_create(request(clock))

    assert refused.state is LaunchState.PREVIEW
    assert refused.preview is not None
    reasons = "; ".join(refused.preview.assessment.reasons)
    assert "available account balance was not observed" in reasons
    assert expected in reasons


def test_a_provider_with_no_balance_source_says_so_rather_than_only_not_observed(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    pod_runtime = runtime(provider, clock, tmp_path)
    pod_runtime.balance_source = None

    refused = pod_runtime.preview_create(request(clock))

    assert refused.preview is not None
    reasons = "; ".join(refused.preview.assessment.reasons)
    assert "no account-balance source is configured for this provider" in reasons


def test_a_spend_warning_that_never_reached_the_phone_is_recorded_not_swallowed(
    tmp_path: Path,
) -> None:
    """`operations/notify/README.md`: "If a send fails, say so." A warning whose
    delivery failed silently is indistinguishable from one that arrived, and the
    balance it was warning about is the one thing nobody may quietly not hear."""

    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")

    def failing_notifier(message: str) -> NotifyOutcome:
        del message
        return NotifyOutcome(True, False, "no topic configured")

    result = runtime(provider, clock, tmp_path, notifier=failing_notifier).preview_create(
        request(clock)
    )

    assert result.preview is not None
    record = result.preview.to_record()["spend"]
    assert isinstance(record, dict)
    ceilings = record["ceilings"]
    assert isinstance(ceilings, dict)
    assert ceilings["alert_notifications"] == [
        "Phone notification: NOT DELIVERED (no topic configured). The result above still stands."
    ]


def test_a_broken_notifier_is_recorded_and_still_cannot_fail_the_preview(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.set_account_balance("60.00")

    def raising_notifier(message: str) -> NotifyOutcome:
        del message
        raise RuntimeError("notifier exploded")

    result = runtime(provider, clock, tmp_path, notifier=raising_notifier).preview_create(
        request(clock)
    )

    assert result.state is LaunchState.PREVIEW
    assert result.preview is not None
    ceilings = result.preview.to_record()["spend"]["ceilings"]  # type: ignore[index]
    assert ceilings["alert_notifications"] == [  # type: ignore[index]
        "Phone notification: NOT DELIVERED (the notifier raised: RuntimeError). "
        "The result above still stands."
    ]


def test_safe_balance_state_failure_is_not_mislabeled_as_a_failed_delivery(
    tmp_path: Path,
) -> None:
    """Recovery bookkeeping can fail when no phone notification was due."""

    clock = Clock()
    provider = fake(clock)

    class BrokenRecoveryStateRuntime(PreviewingRuntime):
        def _record_nonalert_balance(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs
            raise OSError("recovery state unavailable")

    pod_runtime = BrokenRecoveryStateRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        challenge_factory=lambda: TEST_CHALLENGE,
        controller_armer=FakeControllerArmer(clock, provider),
    )
    result = pod_runtime.preview_create(request(clock))

    assert result.state is LaunchState.PREVIEW
    assert result.preview is not None
    notifications = result.preview.assessment.alert_notifications
    assert notifications == (
        "Phone notification recovery state: NOT RECORDED (OSError). No notification was due.",
    )
    assert "NOT DELIVERED" not in notifications[0]


def test_safe_balance_without_an_alert_episode_writes_no_debounce_state(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    lease_root = tmp_path / "not-created"

    result = runtime(provider, clock, lease_root).preview_create(request(clock))

    assert result.state is LaunchState.PREVIEW
    assert not lease_root.exists()


def test_pod_shell_notifier_keeps_the_reason_notify_sh_printed() -> None:
    """The script's reason distinguishes a missing topic from an ntfy refusal."""

    def runner(command, **kwargs):  # type: ignore[no-untyped-def]
        del command, kwargs
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="notify: NOT DELIVERED (start) — no topic\n"
        )

    outcome = shell_notifier(runner=runner)("Spend warning: balance is low.")

    assert outcome == NotifyOutcome(True, False, "notify: NOT DELIVERED (start) — no topic")
    assert outcome.line().startswith("Phone notification: NOT DELIVERED")


def test_pod_shell_notifier_reports_a_timeout_and_a_refused_message_honestly() -> None:
    def timing_out(command, **kwargs):  # type: ignore[no-untyped-def]
        del command
        raise subprocess.TimeoutExpired(cmd="sh", timeout=kwargs["timeout"])

    timed_out = shell_notifier(runner=timing_out)("Spend warning: balance is low.")
    assert timed_out == NotifyOutcome(
        True, False, "the notification command did not answer within 10 seconds"
    )

    def unreachable(command, **kwargs):  # type: ignore[no-untyped-def]
        del command, kwargs
        raise AssertionError("a malformed message must never reach the script")

    refused = shell_notifier(runner=unreachable)("two\nlines")
    assert refused == NotifyOutcome(False, False, "the message was not one non-empty line")


def test_the_pod_cli_does_not_page_a_phone_unless_asked() -> None:
    """The flag is the consent boundary; without it the seam stays switched off."""

    assert cli._notifier(False) is silent
    assert cli._notifier(True) is not silent


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
    ceilings = record["ceilings"]
    assert isinstance(ceilings, dict)
    assert ceilings["account_balance_floor_usd"] == "50.00"
    assert ceilings["account_balance_alert_usd"] == "75.00"
    assert ceilings["account_balance_observation"] == {
        "available_usd": "100.00",
        "observed_at": START.isoformat(),
        "source": "fake provider account balance",
    }


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
        runtime_contract=replace(existing.runtime_contract, billing_cutoff_margin_seconds=0),
    )
    mismatched_margin = runtime(provider, clock, tmp_path / "margin-mismatch").preview_adopt(
        existing.pod_id, expected=expected
    )

    assert mismatched_margin.state is LaunchState.REFUSED_RUNTIME_CONTRACT

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
    assert submitted.metadata[BILLING_CUTOFF_MARGIN_ENV] == "3600"


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
    assert (
        pod_runtime.shutdown.billing_cutoff_margin_seconds
        == configured.billing_cutoff_margin_seconds
    )


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


def test_billing_capture_that_raises_is_retried_like_any_other_non_captured_answer() -> None:
    """The shipped RunPod adapter raises ProviderFailure on any non-200 billing

    response -- the likeliest real billing failure -- but only the
    returns-an-unavailable-object path was ever driven (audit-d Finding 6).
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.09")
    provider.inject_failure("capture_cost", ProviderFailure("RunPod billing returned HTTP 500"))
    closer = VerifiedShutdown(
        provider,
        timeout_seconds=8,
        poll_seconds=1,
        billing_cutoff_margin_seconds=3600,
        billing_attempts=2,
        billing_retry_seconds=1,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )

    report = closer.close(record, reason="raising billing capture drill")

    assert report.state is CloseState.VERIFIED
    assert report.captured_cost_usd == Decimal("0.09")
    assert report.billing_attempts == 2


def test_billing_capture_that_always_raises_exhausts_its_bounded_retry_and_stays_red() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.inject_failure(
        "capture_cost", ProviderFailure("RunPod billing returned HTTP 500"), times=2
    )
    closer = VerifiedShutdown(
        provider,
        timeout_seconds=8,
        poll_seconds=1,
        billing_cutoff_margin_seconds=3600,
        billing_attempts=2,
        billing_retry_seconds=1,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )

    report = closer.close(record, reason="always-raising billing capture drill")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert report.captured_cost_usd is None
    assert "RunPod billing returned HTTP 500" in report.last_detail


def test_billing_that_never_posts_exhausts_its_bounded_retry_and_stays_red() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    closer = VerifiedShutdown(
        provider,
        timeout_seconds=8,
        poll_seconds=1,
        billing_cutoff_margin_seconds=3600,
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


def test_an_adoption_lease_that_cannot_be_read_back_closes_the_pod(tmp_path: Path) -> None:
    """`store.load()` reads a file back off disk, so `None` is a real answer.

    This was an `assert`, which `python -O` removes; a `None` lease then reached
    controller arming and the durable-store fault surfaced as an arming one.
    Found by CodeRabbit on this branch.  An adopted pod that cannot be guarded
    does not keep billing, so this closes it exactly as failed arming does.
    """

    clock = Clock()
    provider = fake(clock)
    expected = request(clock)
    existing = provider.create(expected)

    class BlindStore(LeaseStore):
        def load(self) -> PodLease | None:
            return None

    class BlindRuntime(PreviewingRuntime):
        def _store(self, lease_id: str) -> LeaseStore:
            return BlindStore(self.lease_root / f"{lease_id}.json")

    tokens = iter(("a" * 32, "b" * 32))
    pod_runtime = BlindRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        challenge_factory=lambda: TEST_CHALLENGE,
        controller_armer=FakeControllerArmer(clock, provider),
    )

    result = pod_runtime.adopt(
        existing.pod_id,
        expected=expected,
        confirmation=adopt_confirmation(existing.pod_id),
    )

    assert result.state is LaunchState.LEASE_FAILURE
    assert not result.green
    assert "could not be read back" in result.detail
    assert existing.pod_id in provider.terminate_calls
    assert provider.status(existing.pod_id).presence.value == "absent"


def test_a_request_that_cannot_be_sealed_is_named_a_request_refusal_not_a_lease_failure(
    tmp_path: Path,
) -> None:
    """Sealing is request preparation; it never touches the durable store.

    Inside the lease block it reported as LEASE_FAILURE, which names the wrong
    subsystem on a money path.  Found by CodeRabbit on this branch.
    """

    clock = Clock()
    provider = fake(clock)

    class UnsealableRuntime(PreviewingRuntime):
        def _sealed_request(
            self, request: PodCreateRequest, launch_token: str, estimate: object
        ) -> PodCreateRequest:
            raise ValueError("docker_start_cmd carries no --report-path flag")

    tokens = iter(("a" * 32, "b" * 32))
    pod_runtime = UnsealableRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        challenge_factory=lambda: TEST_CHALLENGE,
        controller_armer=FakeControllerArmer(clock, provider),
    )

    result = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_REQUEST
    assert not result.green
    assert "--report-path" in result.detail
    assert list(tmp_path.glob("*.json")) == [], "no lease was armed"
    assert not any(verb == "create" for verb, _ in provider.calls), "no pod was created"


def test_report_path_binding_refuses_a_command_it_cannot_bind(tmp_path: Path) -> None:
    """`PodCreateRequest` refuses such a command already, so no launch reaches this.

    The helper states its own contract anyway: enforced only at a distance, a
    bare `tuple.index` failure on a money path names nothing at all.
    """

    with pytest.raises(ValueError, match="no --report-path flag"):
        _bind_report_path_to_launch(("python", "-m", "operations.pod.pod_timer"), "a" * 32)
    with pytest.raises(ValueError, match="carries no value"):
        _bind_report_path_to_launch(("python", "--report-path"), "a" * 32)


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
    recovery_requests = [item for item in provider.create_requests if item.recovery_only]
    assert len(recovery_requests) == 1
    assert recovery_requests[0].metadata[BILLING_CUTOFF_MARGIN_ENV] == "3600"


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
    # Advance the clock so the close cutoff strictly follows the window start:
    # with the two equal, CostCapture refuses to construct and the close would
    # report through the capture-failed branch instead of the one this test
    # is named for.
    clock.sleep(60)

    report = shutdown(provider, clock).close(record, reason="test empty billing")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert report.captured_cost_usd is None
    assert report.manual_action is not None
    # The fake's own sentence is unique to its no-records path, so this cannot
    # be satisfied by the evidence-error or capture-failed branches -- the two
    # wrong reasons earlier versions of this test passed for.
    assert "fake billing deliberately has no records" in report.last_detail


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


def test_close_report_names_the_declared_billing_cutoff() -> None:
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


def test_billing_cutoff_margin_refuses_evidence_one_second_past_the_configured_bound() -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.set_billing(
        CostCapture(
            pod_id=record.pod_id,
            state=BillingState.CAPTURED,
            cutoff_at=clock.now() + timedelta(seconds=31),
            lines=(CostLine(Decimal("0.11"), "one second beyond configured margin"),),
            source="fake provider resolved cutoff",
            window_start_at=record.created_at,
        )
    )

    report = shutdown(provider, clock, cutoff_margin=30).close(
        record, reason="configured cutoff margin drill"
    )

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert "too far beyond the requested window" in report.last_detail


def test_a_billing_cutoff_arbitrarily_far_in_the_future_is_refused_not_verified() -> None:
    """ "Charges captured through a named cutoff" is a claim about measured time

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


def test_a_verified_lease_phase_cannot_be_reached_without_naming_a_pod(tmp_path: Path) -> None:
    """`closed-verified` asserts absence about one exact pod.

    A lease reaching that phase while naming no pod is evidence about nothing,
    and the binding check skipped itself in exactly that case -- so a close
    record with no `pod_id` and a lease that never bound one produced a green
    terminal phase.  Found by CodeRabbit on this branch.  A lease read back off
    disk runs this same validation, which is what makes it reachable.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.05")
    close_record = shutdown(provider, clock).close(record, reason="unbound drill").to_record()
    close_record.pop("pod_id")

    def lease(*, phase: str, close_record: dict[str, object] = close_record) -> PodLease:
        return PodLease(
            lease_id="c" * 32,
            launch_token="c" * 32,
            provider_name="fake",
            pod_id=None,
            volume_id="test-volume",
            pod_hourly_usd=POD_HOURLY,
            volume_hourly_usd=VOLUME_HOURLY,
            created_at=clock.now(),
            started_at=None,
            hard_deadline=clock.now() + timedelta(seconds=60),
            owner_token="laptop",
            heartbeat_at=clock.now(),
            phase=phase,
            close_record=close_record,
        )

    with pytest.raises(ValueError, match="must name the exact pod"):
        lease(phase="closed-verified")

    # The unverified path legitimately closes without one: a create that never
    # bound a pod still has to close, and narrowing that would lose the record.
    unverified = lease(
        phase="close-unverified", close_record={**close_record, "state": "unverified"}
    )
    assert unverified.phase == "close-unverified" and unverified.pod_id is None


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


def test_laptop_supervisor_steady_state_heartbeats_a_healthy_pod_without_closing_it(
    tmp_path: Path,
) -> None:
    """The normal path every few seconds of a live run: nothing to do but

    refresh the heartbeat.  Every other supervisor test ends in a close;
    audit-d Finding 6 names this untested gap.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "steady-state.json")
    before = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=60)

    result = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    ).run_once()

    assert result.state is ControllerState.ACTIVE
    assert result.close_report is None
    assert result.lease is not None and result.lease.heartbeat_at >= before.heartbeat_at
    assert provider.status(record.pod_id).presence.value == "present"
    assert provider.terminate_calls == []


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

    replace(request(clock), docker_start_cmd=command)

    # Construction not raising IS the acceptance check.  The refusal below is
    # what makes it meaningful: the validator binds the module, not merely the
    # interpreter spelling, so the same spelling with another module must fail.
    impostor = interpreter + ("-m", "operations.pod.not_the_timer") + flags_after_module
    with pytest.raises(ValueError, match="primary process"):
        replace(request(clock), docker_start_cmd=impostor)


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
    pod_runtime = PreviewingRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(),
        lease_root=tmp_path,
        shutdown=shutdown(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        challenge_factory=lambda: TEST_CHALLENGE,
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


def test_a_pod_report_write_that_fails_twice_says_the_receipt_also_failed(
    tmp_path: Path,
) -> None:
    """The fallback receipt is the pod's only durable evidence when the first
    write fails.  Its own failure used to be swallowed, so an operator finding
    nothing on the volume could not tell a write that failed twice from one that
    never ran (GOVERNANCE 2).  Found by CodeRabbit on this branch."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.06")
    store = LeaseStore(tmp_path / "timer-double-write.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("block report directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="fallback receipt also failed"):
        _persist_or_close(
            TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
            blocked_parent / "report.json",
            {"bootstrap": {"argv": ["true"], "state": "running"}, "close": None, "green": False},
        )
    assert provider.status(record.pod_id).presence.value == "absent"


def test_a_report_payload_with_no_bootstrap_object_still_closes_the_pod(tmp_path: Path) -> None:
    """This was an `assert`, which `python -O` removes.  A non-dict then reached
    `{**bootstrap, ...}` in the close and raised TypeError instead of filing the
    fallback receipt.  Every caller passes a dict, so the placeholder says the
    payload was unusable rather than inventing a state nobody observed."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.06")
    store = LeaseStore(tmp_path / "timer-no-bootstrap.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("block report directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="mandatory pod report write failed"):
        _persist_or_close(
            TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
            blocked_parent / "report.json",
            {"bootstrap": None, "close": None, "green": False},
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

    def materialize_model_store(self) -> dict[str, object]:
        return self._step(BootstrapStep.MODEL_STORE)

    def verify_chair_cache(self) -> dict[str, object]:
        return self._step(BootstrapStep.CHAIR_CACHE)

    def run_preflight(self) -> dict[str, object]:
        return self._step(BootstrapStep.PREFLIGHT) | {"color": "green"}


def test_model_store_bootstrap_action_delegates_pinned_materialization(
    monkeypatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def materialize(root, fetcher, *, capacity):  # type: ignore[no-untyped-def]
        observed.update(root=root, fetcher=fetcher, capacity=capacity)
        return {"artifacts": [], "complete": False}

    monkeypatch.setattr("operations.pod.bootstrap.materialize_real_roster", materialize)
    fetcher = object()
    action = ModelStoreBootstrapAction(
        tmp_path / "models",
        fetcher,
        capacity={"cleanup_owner": "pod operator"},  # type: ignore[arg-type]
    )

    assert action.materialize() == {"artifacts": [], "complete": False}
    assert observed == {
        "root": tmp_path / "models",
        "fetcher": fetcher,
        "capacity": {"cleanup_owner": "pod operator"},
    }


def test_every_bootstrap_actions_implementation_covers_every_step() -> None:
    """Protocol methods, resumable steps, and concrete actions must stay one-to-one.

    Structural Protocol annotations do not enforce that alignment at runtime.
    """
    from operations.operator.surface import FixtureBootstrapActions

    required = {name for name, _ in inspect.getmembers(BootstrapActions, inspect.isfunction)} - {
        "__init__"
    }
    assert len(required) == len(BootstrapStep), (
        "every bootstrap step needs exactly one action method on the protocol; "
        f"steps={[step.value for step in BootstrapStep]}, methods={sorted(required)}"
    )
    for implementation in (
        FixtureBootstrapActions,
        SubprocessBootstrapActions,
        FakeBootstrapActions,
    ):
        missing = sorted(name for name in required if not hasattr(implementation, name))
        assert not missing, f"{implementation.__name__} implements no {missing}"


def test_bootstrapper_refuses_any_unregistered_structural_implementation_that_drifted(
    tmp_path: Path,
) -> None:
    """The guard applies to future implementations, not only today's test roster."""

    class DriftedActions(FakeBootstrapActions):
        materialize_model_store = None  # type: ignore[assignment]

    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")

    with pytest.raises(TypeError, match="materialize_model_store"):
        Bootstrapper(
            BootstrapJournal(
                tmp_path / "bootstrap.json", BootstrapPlan("c" * 40, lockfile), now=lambda: START
            ),
            DriftedActions(),  # type: ignore[arg-type]
        )


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
    assert actions.calls.count(BootstrapStep.MODEL_STORE) == 1
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


def test_model_store_security_refusal_keeps_its_code_and_stops_bootstrap(tmp_path: Path) -> None:
    class RefusingModelStore(FakeBootstrapActions):
        def materialize_model_store(self) -> dict[str, object]:
            self.calls.append(BootstrapStep.MODEL_STORE)
            raise DigestMismatchRefusal("qwen3.8-27B", "external link target refused")

    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")
    actions = RefusingModelStore()
    report = Bootstrapper(
        BootstrapJournal(
            tmp_path / "bootstrap.json", BootstrapPlan("e" * 40, lockfile), now=lambda: START
        ),
        actions,
    ).run()

    assert report.failure_step is BootstrapStep.MODEL_STORE
    assert report.detail is not None and "security refusal digest-mismatch" in report.detail
    assert "qwen3.8-27B" in report.detail
    assert BootstrapStep.CHAIR_CACHE not in actions.calls
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
        materialize_model_store=lambda: {},
        cache=None,  # type: ignore[arg-type]
        preflight=lambda: {"color": "green"},
        runner=lambda argv, cwd: (_ for _ in ()).throw(AssertionError(f"unexpected {argv}")),
    )

    with pytest.raises(BootstrapStepFailure, match="not repository uv.lock"):
        actions.sync_uv_environment(other)


def test_production_bootstrap_uses_absolute_tools_and_an_explicit_environment(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    commit = "f" * 40
    observed: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setenv("VERBATUS_PARENT_SECRET", "must-not-leak")

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        observed.append((list(argv), dict(kwargs["env"])))
        stdout = f"{commit}\n" if argv[1:] == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("operations.pod.bootstrap.subprocess.run", run)
    actions = SubprocessBootstrapActions(
        repository=repository,
        transfer=lambda: {},
        materialize_model_store=lambda: {},
        cache=None,  # type: ignore[arg-type]
        preflight=lambda: {"color": "green"},
    )

    assert actions.checkout_commit(commit) == {"commit": commit}
    assert observed
    assert all(argv[0] == "/usr/bin/git" for argv, _ in observed)
    assert all(
        environment
        == {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        for _, environment in observed
    )
    assert all("VERBATUS_PARENT_SECRET" not in environment for _, environment in observed)


def test_production_bootstrap_refuses_an_incomplete_model_store_receipt(tmp_path: Path) -> None:
    actions = SubprocessBootstrapActions(
        repository=tmp_path,
        transfer=lambda: {},
        materialize_model_store=lambda: {
            "complete": False,
            "real_roster_complete": False,
        },
        cache=None,  # type: ignore[arg-type]
        preflight=lambda: {"color": "green"},
    )

    with pytest.raises(BootstrapStepFailure, match="every real-roster repository"):
        actions.materialize_model_store()


def test_red_preflight_details_survive_into_the_bootstrap_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    actions = SubprocessBootstrapActions(
        repository=repository,
        transfer=lambda: {},
        materialize_model_store=lambda: {},
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


@pytest.mark.parametrize("source_bytes", [b"\xff", b"schema ="])
def test_supplied_placement_bytes_have_a_distinct_parse_refusal(
    tmp_path: Path, source_bytes: bytes
) -> None:
    path = tmp_path / "sealed-placement.toml"

    with pytest.raises(
        PlacementRefusal, match="cannot parse supplied placement table bytes"
    ) as refused:
        load_placement_table(path, source_bytes=source_bytes)

    assert str(path) in str(refused.value)
    assert refused.value.__cause__ is not None


def test_a_placement_filesystem_failure_remains_a_read_refusal(tmp_path: Path) -> None:
    path = tmp_path / "missing-placement.toml"

    with pytest.raises(PlacementRefusal, match="cannot read placement table") as refused:
        load_placement_table(path)

    assert str(path) in str(refused.value)
    assert isinstance(refused.value.__cause__, OSError)


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
    template = (root / "config/spend.toml").read_text(encoding="utf-8")
    assert 'account_balance_floor_usd = "50.00"' in template
    assert "unverified" in template


def test_spend_configuration_documentation_matches_the_observed_balance_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    documentation = (root / "config" / "README.md").read_text(encoding="utf-8")

    assert "observed `account_balance_floor_usd` hard reserve" in documentation
    assert "`account_balance_alert_usd` notification threshold" in documentation
    assert "explicitly configured source" in documentation
    assert "runtime does not observe account balance" not in documentation


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


def test_a_hung_nvidia_smi_becomes_a_red_gpu_profile_rather_than_a_wedged_preflight() -> None:
    """An unbounded `nvidia-smi` never reaches the red `GpuProfile` path below it.

    A wedged driver or a card mid-reset would block preflight forever on a pod
    that is already billing.  `TimeoutExpired` is an `Exception`, so the same
    handler records it in `discovery_detail`.  Found by CodeRabbit on this branch.
    """

    class Disk:
        free = 100 * 1024**3

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 30.0)

    profile = SystemGpuProbe(disk_path="/", runner=runner, disk_usage=lambda path: Disk()).profile(
        "bfloat16"
    )

    assert profile.name == "GPU discovery unavailable"
    assert "TimeoutExpired" in profile.discovery_detail


def test_the_default_gpu_probe_runner_bounds_the_command_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    SystemGpuProbe(disk_path="/")._run(["nvidia-smi"])

    assert seen["timeout"] == SystemGpuProbe._RUN_TIMEOUT_SECONDS


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


# --- audit/pod-money-path: red paths the 2026-08-12 independent audit found untested ---


def test_a_wrong_confirmation_neither_echoes_the_phrase_nor_burns_the_challenge(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    # The bare runtime never re-previews, so the retry below can only succeed
    # if the FIRST preview's challenge genuinely survived the typo -- a
    # re-previewing helper would re-mint and prove nothing.
    pod_runtime = bare_runtime(provider, clock, tmp_path)
    create_request = request(clock)
    pod_runtime.preview_create(create_request)

    refused = pod_runtime.create(create_request, confirmation="wrong words")

    assert refused.state is LaunchState.REFUSED_CONFIRMATION
    assert refused.detail is not None and CREATE_CONFIRMATION not in refused.detail
    assert refused.preview is not None and refused.preview.challenge is None
    assert refused.preview.to_record()["confirmation_phrase"] is None
    assert not any(verb == "create" for verb, _ in provider.calls)

    # The typo deliberately did not burn the preview: the exact phrase the
    # operator was shown still authorizes exactly this create.
    retried = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)
    assert retried.state is LaunchState.CREATED_GUARDED


def test_an_unconfigured_spend_policy_refuses_both_paid_paths_end_to_end(tmp_path: Path) -> None:
    """The checked-in refusal, driven through create and adopt rather than
    asserted about a parsed dataclass field."""

    clock = Clock()
    provider = fake(clock)
    pod_runtime = runtime(provider, clock, tmp_path, spend=SpendPolicy(state="unconfigured"))
    create_request = request(clock)

    refused_create = pod_runtime.create(create_request, confirmation=CREATE_CONFIRMATION)
    assert refused_create.state is LaunchState.REFUSED_CEILING
    assert "unconfigured" in refused_create.detail
    assert not any(verb == "create" for verb, _ in provider.calls)
    assert list(tmp_path.glob("*.json")) == []

    existing = provider.create(create_request)
    refused_adopt = pod_runtime.adopt(
        existing.pod_id, expected=create_request, confirmation="anything"
    )
    assert refused_adopt.state is LaunchState.REFUSED_CEILING
    assert "unconfigured" in refused_adopt.detail
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"surprise_field": '"1"'}, "unknown field"),
        ({"schema": '"pod-spend.v1"'}, "schema must be"),
        ({"state": '"armed"'}, "state must be"),
        ({"max_hourly_usd": "1.00"}, "decimal string"),
        ({"billing_cutoff_margin_seconds": "3601"}, "must be between 0 and 3600 seconds"),
        ({"hard_lifetime_seconds": None}, "missing a required ceiling"),
        ({"currency": '"EUR"'}, "currency must be USD"),
    ],
)
def test_spend_policy_loader_refuses_each_widening_or_malformed_file(
    tmp_path: Path, mutation: dict[str, str | None], message: str
) -> None:
    from .spend import load_spend_policy

    base: dict[str, str | None] = {
        "schema": '"pod-spend.v2"',
        "state": '"configured"',
        "currency": '"USD"',
        "max_hourly_usd": '"1.00"',
        "max_estimated_metered_cost_usd": '"2.00"',
        "account_balance_floor_usd": '"50.00"',
        "account_balance_alert_usd": '"75.00"',
        "hard_lifetime_seconds": "3600",
        "laptop_heartbeat_timeout_seconds": "30",
        "shutdown_poll_interval_seconds": "1",
        "shutdown_deadline_seconds": "5",
        "billing_cutoff_margin_seconds": "3600",
    }
    base.update(mutation)
    path = tmp_path / "spend.toml"
    path.write_text(
        "\n".join(f"{key} = {value}" for key, value in base.items() if value is not None) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SpendRefusal, match=message):
        load_spend_policy(path)


def test_unconfigured_spend_policy_file_may_carry_only_schema_and_state(tmp_path: Path) -> None:
    from .spend import load_spend_policy

    path = tmp_path / "spend.toml"
    path.write_text(
        'schema = "pod-spend.v2"\nstate = "unconfigured"\nmax_hourly_usd = "9.99"\n',
        encoding="utf-8",
    )

    with pytest.raises(SpendRefusal, match="only schema and state"):
        load_spend_policy(path)


def test_spend_policy_refuses_shutdown_timing_that_could_outrun_the_lifetime() -> None:
    with pytest.raises(SpendRefusal, match="cannot exceed the shutdown deadline"):
        replace(policy(), shutdown_poll_interval_seconds=10, shutdown_deadline_seconds=5)
    with pytest.raises(SpendRefusal, match="billing-retry tail"):
        replace(policy(), hard_lifetime_seconds=40, shutdown_deadline_seconds=15)


def test_a_verified_close_record_is_never_replaced_by_a_lesser_one(tmp_path: Path) -> None:
    from .models import LeaseOwnershipError

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.10")
    store = LeaseStore(tmp_path / "close-guard.json")
    _lease(store, record, owner="laptop", clock=clock)
    report = shutdown(provider, clock).close(record, reason="first, verified close")
    assert report.verified
    store.record_close(
        owner_token="laptop", close_record=report.to_record(), verified=True, now=clock.now()
    )

    with pytest.raises(LeaseOwnershipError, match="verified close record"):
        store.record_close(
            owner_token="laptop",
            close_record={"pod_id": record.pod_id, "state": "failed-shutdown"},
            verified=False,
            now=clock.now(),
        )
    reloaded = store.load()
    assert reloaded is not None and reloaded.phase == "closed-verified"


def test_an_unverified_close_can_still_be_upgraded_to_verified(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "close-upgrade.json")
    _lease(store, record, owner="laptop", clock=clock)
    store.record_close(
        owner_token="laptop",
        close_record={"pod_id": record.pod_id, "state": "failed-shutdown"},
        verified=False,
        now=clock.now(),
    )

    provider.bill(record.pod_id, "0.10")
    report = shutdown(provider, clock).close(record, reason="reconciliation close")
    assert report.verified
    upgraded = store.record_close(
        owner_token="laptop", close_record=report.to_record(), verified=True, now=clock.now()
    )
    assert upgraded.phase == "closed-verified"


def test_a_second_lease_writer_is_refused_at_the_same_path(tmp_path: Path) -> None:
    from .models import LeaseOwnershipError

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "exclusive.json")
    lease = _lease(store, record, owner="first", clock=clock)

    with pytest.raises(LeaseOwnershipError, match="already exists"):
        LeaseStore(store.path).create(replace(lease, owner_token="second"))


@pytest.mark.parametrize(
    ("tamper_path", "value", "message"),
    [
        (("receipt", "pod_id"), "some-other-pod", "exact lease and pod"),
        (("receipt", "hard_deadline"), "2027-01-01T00:00:00Z", "immutable hard deadline"),
        (("laptop_supervisor_started",), False, "must prove both"),
        (("receipt", "pod_timer", "report_path"), "relative/report.json", "must be absolute"),
    ],
)
def test_a_tampered_controller_receipt_is_refused_when_the_lease_is_reloaded(
    tmp_path: Path, tamper_path: tuple[str, ...], value: object, message: str
) -> None:
    """Falsification for `_validate_controller_record`: each binding it checks
    is individually broken and the reload must refuse."""

    from common.contracts.canonical import self_hash

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "tamper.json")
    lease = _lease(store, record, owner="laptop", clock=clock)

    sealed = lease.to_record()
    del sealed["self_hash"]
    target: dict[str, object] = sealed["controller_record"]  # type: ignore[assignment]
    for key in tamper_path[:-1]:
        target = target[key]  # type: ignore[index]
    target[tamper_path[-1]] = value
    sealed["self_hash"] = self_hash(sealed)

    # The binding-specific fragment, so deleting any single validation rule
    # turns exactly its own case red rather than all four staying green on the
    # generic wrapper text.
    with pytest.raises(LeaseFormatError, match=message):
        PodLease.from_record(sealed)


def test_arming_binding_refuses_a_receipt_naming_other_facts(tmp_path: Path) -> None:
    """Falsification for `_validate_arming_binding`'s deadline, identity, and
    report-path bindings, which had no red-path test."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "binding.json")
    lease = _lease(store, record, owner="laptop", clock=clock)
    create_request = replace(request(clock), hard_deadline=lease.hard_deadline)
    stamp = clock.now().isoformat().replace("+00:00", "Z")

    def arming(receipt_overrides: dict[str, object]) -> ControllerArming:
        report_flag = create_request.docker_start_cmd.index("--report-path")
        receipt: dict[str, object] = {
            "lease_id": lease.lease_id,
            "pod_id": record.pod_id,
            "hard_deadline": lease.hard_deadline.isoformat().replace("+00:00", "Z"),
            "laptop_supervisor": {"identity": "test", "started_at": stamp},
            "pod_timer": {
                "report_path": create_request.docker_start_cmd[report_flag + 1],
                "acknowledged_at": stamp,
            },
        }
        receipt.update(receipt_overrides)
        return ControllerArming(True, True, clock.now(), "test arming", receipt)

    validate = PodRuntime._validate_arming_binding
    validate(arming=arming({}), request=create_request, record=record, lease=lease)
    with pytest.raises(ValueError, match="another lease or pod"):
        validate(
            arming=arming({"pod_id": "other"}), request=create_request, record=record, lease=lease
        )
    with pytest.raises(ValueError, match="another hard deadline"):
        validate(
            arming=arming({"hard_deadline": "2027-01-01T00:00:00Z"}),
            request=create_request,
            record=record,
            lease=lease,
        )
    with pytest.raises(ValueError, match="different durable report path"):
        validate(
            arming=arming(
                {"pod_timer": {"report_path": "/elsewhere/r.json", "acknowledged_at": stamp}}
            ),
            request=create_request,
            record=record,
            lease=lease,
        )


def test_pod_timer_expiry_with_a_running_child_closes_verified(tmp_path: Path) -> None:
    """The dead-man's main production path — a workload still running when the
    hard lifetime expires — end to end, green."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.06")
    store = LeaseStore(tmp_path / "timer-expiry.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)

    class RunningChild:
        def poll(self) -> None:
            return None

    report_path = tmp_path / "expiry-report.json"
    result = run_with_bootstrap(
        TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
        bootstrap_command_json='["python","-m","operations.pod.bootstrap"]',
        report_path=report_path,
        sleeper=clock.sleep,
        popen=lambda argv: RunningChild(),  # type: ignore[arg-type]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.close_report is not None and result.close_report.verified
    assert report["green"] is True
    assert report["close_attempts"] == 1
    assert provider.status(record.pod_id).presence.value == "absent"


def test_pod_timer_reattempts_a_red_close_a_bounded_number_of_times(tmp_path: Path) -> None:
    """One failed close window is not the timer's last word — and the retries
    are bounded, recorded, and honestly non-green when they all fail."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = LeaseStore(tmp_path / "timer-red.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    provider.inject_failure("terminate", ProviderFailure("injected terminate outage"), times=100)

    class RunningChild:
        def poll(self) -> None:
            return None

    report_path = tmp_path / "red-report.json"
    result = run_with_bootstrap(
        TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
        bootstrap_command_json='["python","-m","operations.pod.bootstrap"]',
        report_path=report_path,
        sleeper=clock.sleep,
        popen=lambda argv: RunningChild(),  # type: ignore[arg-type]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.close_report is not None and not result.close_report.verified
    assert report["green"] is False
    assert report["close_attempts"] == 3
    # The pod genuinely still exists; the report must not pretend otherwise.
    assert provider.verify_absent(record.pod_id).presence.value == "present"


def test_a_captured_billing_line_dated_outside_the_declared_window_is_unverified(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.set_billing(
        CostCapture(
            pod_id=record.pod_id,
            state=BillingState.CAPTURED,
            cutoff_at=clock.now() + timedelta(minutes=30),
            lines=(
                CostLine(
                    Decimal("0.10"),
                    "a bucket from long before this pod",
                    record.created_at - timedelta(hours=3),
                ),
            ),
            source="fake provider billing",
            window_start_at=record.created_at,
        )
    )

    report = shutdown(provider, clock).close(record, reason="out-of-window line")

    assert report.state is CloseState.UNVERIFIED_BILLING
    assert "dated outside its own declared window" in report.last_detail


@pytest.mark.parametrize("bad_interval", ["0", "-3", "abc", "inf", "nan"])
def test_a_bad_interval_seconds_value_is_refused_before_any_paid_create(bad_interval: str) -> None:
    clock = Clock()
    command = request(clock).docker_start_cmd + ("--interval-seconds", bad_interval)

    with pytest.raises(ValueError, match="interval-seconds"):
        replace(request(clock), docker_start_cmd=command)


def test_a_valid_interval_seconds_value_is_accepted_in_both_spellings() -> None:
    clock = Clock()

    # Construction not raising is the whole check, for both argv spellings the
    # pod-side parser accepts.
    replace(
        request(clock),
        docker_start_cmd=request(clock).docker_start_cmd + ("--interval-seconds", "15"),
    )
    replace(
        request(clock),
        docker_start_cmd=request(clock).docker_start_cmd + ("--interval-seconds=15",),
    )


@pytest.mark.parametrize("bad_flag", ["--interval-seconds=0", "--interval-seconds=abc"])
def test_the_equals_spelling_of_a_bad_interval_is_refused_too(bad_flag: str) -> None:
    clock = Clock()
    command = request(clock).docker_start_cmd + (bad_flag,)

    with pytest.raises(ValueError, match="interval-seconds"):
        replace(request(clock), docker_start_cmd=command)


def test_a_created_pod_that_arrives_not_running_is_closed_not_green(tmp_path: Path) -> None:
    clock = Clock()

    class ExitedOnCreateFake(FakeProvider):
        def create(self, create_request: PodCreateRequest) -> PodRecord:
            record = super().create(create_request)
            return replace(record, state="EXITED")

    provider = ExitedOnCreateFake(now=clock.now)
    provider.price_sheet = {"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}
    pod_runtime = runtime(provider, clock, tmp_path)

    result = pod_runtime.create(request(clock), confirmation=CREATE_CONFIRMATION)

    assert result.state is LaunchState.REFUSED_RUNTIME_CONTRACT
    assert "EXITED" in result.detail and "RUNNING" in result.detail
    assert result.close_report is not None
    assert result.record is not None
    assert provider.status(result.record.pod_id).presence.value == "absent"


def test_cli_exit_code_three_when_a_pod_may_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 must mean 'nothing exists'; a create whose response was lost
    leaves a pending lease and possibly a pod, and must exit 3."""

    clock = Clock()
    provider = fake(clock)
    provider.inject_post_create_failure(ProviderFailure("response lost in flight"))
    exit_code = _drive_cli(tmp_path, monkeypatch, provider, clock)

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "provider-failure" in out
    # The lease path must be a real value, not the null the key carries on
    # every record.
    assert _last_json_object(out).get("lease_path"), "exit 3 must name the pending lease's path"


def test_cli_adopt_refused_at_preview_for_a_real_pod_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI's FIRST exit applies the same rule as its second: a preview
    refusal that observed a real pod (here, a runtime-contract mismatch on
    adopt) must not read as 'nothing exists'."""

    clock = Clock()
    provider = fake(clock)
    existing = provider.create(request(clock))
    exit_code = _drive_cli(
        tmp_path,
        monkeypatch,
        provider,
        clock,
        command=["adopt", "--pod-id", existing.pod_id, "--request"],
        template="a-different-template-than-the-pod-carries",
    )

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "refused-runtime-contract" in out


def test_a_fallback_receipt_counts_the_close_attempts_already_made(tmp_path: Path) -> None:
    """A report-write failure after three close attempts must not file a
    fallback claiming one: the earlier attempts would vanish (GOVERNANCE 2).
    An unserializable close record makes the primary write fail while the
    serializable fallback still lands."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.06")
    store = LeaseStore(tmp_path / "fallback-count.json")
    lease = _lease(store, record, owner="laptop", clock=clock, deadline_seconds=3)
    report_path = tmp_path / "fallback-count-report.json"

    with pytest.raises(RuntimeError, match="mandatory pod report write failed"):
        _persist_or_close(
            TimerContext(PodDeadmanTimer(lease, shutdown(provider, clock), now=clock.now)),
            report_path,
            {
                "bootstrap": {"argv": ["true"], "state": "failed"},
                "close": object(),  # unserializable: the primary write fails
                "close_attempts": 3,
                "green": False,
            },
        )

    fallback = json.loads(report_path.read_text(encoding="utf-8"))
    assert fallback["close_attempts"] == 4
    assert fallback["green"] is False


def test_pod_timer_main_with_a_failing_factory_writes_a_non_green_report(tmp_path: Path) -> None:
    from .pod_timer import main as pod_timer_main

    report = tmp_path / "factory-failure.json"

    exit_code = pod_timer_main(
        [
            "--timer-factory",
            "operations.pod.no_such_module:factory",
            "--bootstrap-command-json",
            '["true"]',
            "--report-path",
            str(report),
        ]
    )

    assert exit_code == 2
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["green"] is False
    assert data["close"] is None
    assert "no close capability" in data["bootstrap"]["detail"]


def _last_json_object(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    found: list[dict[str, object]] = []
    index = 0
    while (start := text.find("{", index)) != -1:
        try:
            parsed, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
        index = start + consumed
    assert found, "no JSON object in CLI output"
    return found[-1]


def test_cli_interrupt_mid_action_prints_an_interrupted_record_then_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock()
    provider = fake(clock)
    provider.inject_failure("create", KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _drive_cli(tmp_path, monkeypatch, provider, clock)

    out = capsys.readouterr().out
    assert '"state": "interrupted"' in out
    assert str(tmp_path / "leases") in out


def _drive_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeProvider,
    clock: Clock,
    *,
    command: list[str] | None = None,
    template: str = "pinned-template",
) -> int:
    """Run `cli.main` against a fake, answering any prompt correctly."""

    provider.now = lambda: datetime.now(UTC)
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        "\n".join(
            (
                'schema = "pod-spend.v2"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                'account_balance_floor_usd = "50.00"',
                'account_balance_alert_usd = "75.00"',
                "hard_lifetime_seconds = 3600",
                "laptop_heartbeat_timeout_seconds = 30",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 5",
                "billing_cutoff_margin_seconds = 3600",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "name": "cli-exit-code-test",
                "gpu_type": "fake-48gb",
                "image": "registry.example/verbatus@sha256:" + "a" * 64,
                "template": template,
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
    monkeypatch.setattr(
        cli, "_controller_armer", lambda reference: FakeControllerArmer(clock, provider)
    )
    monkeypatch.setattr("builtins.input", lambda prompt: prompt.split("'")[1])
    return cli.main(
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
            *(command or ["create", "--request"]),
            str(request_path),
        ]
    )


def test_an_unreadable_lease_is_a_named_failure_not_a_supervisor_crash(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    store = LeaseStore(tmp_path / "garbage.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not a lease", encoding="utf-8")
    supervisor = LaptopSupervisor(
        store,
        shutdown(provider, clock),
        owner_token="laptop",
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )

    ticked = supervisor.run_once()
    beat = supervisor.heartbeat()

    assert ticked.state is ControllerState.LEASE_RECORD_FAILURE
    assert beat.state is ControllerState.LEASE_RECORD_FAILURE
    assert "cannot be read" in ticked.detail


def test_preflight_with_no_smoke_read_anywhere_is_red_with_a_named_issue(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    runner = PreflightRunner(
        load_models_toml(root / "config/models.toml"),
        load_placement_table(root / "config/pod_placement.toml"),
        FakeCache(),
        FakeSmoke(),
        tmp_path / "no-such-fixture.png",
    )

    report = runner.run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal("48"), Decimal("100"), "bfloat16")
    )

    assert report.color == "red"
    assert "no-chair-verified" in {issue.code for issue in report.issues}
    assert not report.smoke_receipts


def test_a_confirmation_spent_before_a_restart_authorizes_nothing_after_one(
    tmp_path: Path,
) -> None:
    """The money gate is per-process, and a restart makes that a refusal, not a hole.

    Unit 17's per-stage lifecycle refuses to spend one recorded grant twice, and
    that refusal is now durable. This is the check underneath it: even where a
    caller carries a phrase that was genuinely previewed, typed and spent before
    a restart, the process that comes up afterwards holds no outstanding
    challenge for it. Both shapes are covered, because an operator restarting a
    run will plausibly try either -- replay the old phrase cold, and replay it
    after asking for a fresh preview. The second is only a refusal because a new
    preview mints a *different* challenge, which `mint_challenge` guarantees and
    `test_minted_challenges_are_unpredictable_and_do_not_repeat` pins; the fixed
    factory here would otherwise hand the restarted process the same one back.
    """

    clock = Clock()
    provider = fake(clock)
    create_request = request(clock)

    before = bare_runtime(provider, clock, tmp_path)
    before.preview_create(create_request)
    assert before.create(create_request, confirmation=CREATE_CONFIRMATION).green
    creates = sum(1 for verb, _ in provider.calls if verb == "create")

    # The same lease root, a new process: nothing of the gate's state survives.
    after = bare_runtime(provider, clock, tmp_path, challenge="1234567890ABCDEF")

    cold = after.create(create_request, confirmation=CREATE_CONFIRMATION)
    assert cold.state is LaunchState.REFUSED_CONFIRMATION
    assert "no preview in this run issued a challenge" in cold.detail

    after.preview_create(create_request)
    previewed = after.create(create_request, confirmation=CREATE_CONFIRMATION)
    assert previewed.state is LaunchState.REFUSED_CONFIRMATION
    assert "different preview challenge" in previewed.detail

    assert sum(1 for verb, _ in provider.calls if verb == "create") == creates, (
        "a phrase spent before the restart created a second billing pod after it"
    )
