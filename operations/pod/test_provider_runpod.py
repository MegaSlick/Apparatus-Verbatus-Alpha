"""The RunPod REST v1 adapter is exercised through a fake transport only.

Every payload below is built from the shapes RunPod's own documentation
publishes for `rest.runpod.io/v1` (fetched 2026-08-09; the URLs are named in
`provider_runpod.py`'s module docstring). No live call has been made, so these
tests prove the adapter's *handling* of a documented shape, never that the
provider actually answers that way — that is the first authorised live run's
job, and this file does not pretend otherwise.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    AccountBalanceObservation,
    BillingState,
    PodCreateRequest,
    Presence,
    ProviderFailure,
)
from .provider_runpod import (
    _MAX_RESPONSE_BYTES,
    HttpResponse,
    RunPodProvider,
    UrllibRunPodTransport,
    _bounded_read,
    timer_context_from_environment,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
TOKEN = "a" * 32


class ScriptedTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> HttpResponse:
        self.calls.append((method, path, body))
        return self.responses.pop(0)


def request(**overrides: object) -> PodCreateRequest:
    fields: dict[str, object] = {
        "name": "safe-pod",
        "gpu_type": "NVIDIA RTX 6000 Ada Generation",
        "image": "registry.example/verbatus@sha256:" + "a" * 64,
        "template": "template-immutable-reference",
        "volume_id": "volume-1",
        "volume_mount_path": "/workspace/private",
        "docker_start_cmd": (
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "operations.pod.provider_runpod:timer_context_from_environment",
            "--bootstrap-command-json",
            # PLACEHOLDER: bootstrap.py has no __main__ and exits 0 immediately.
            # Not a template for a real request file -- see test_pod_runtime.py's
            # request() fixture.
            '["python","-m","operations.pod.bootstrap"]',
            "--report-path",
            f"/workspace/private/pod-runtime-report-{TOKEN}.json",
        ),
        "hard_deadline": NOW + timedelta(hours=1),
        "repository_commit": "b" * 40,
        "metadata": {"VERBATUS_LAUNCH_TOKEN": TOKEN, BILLING_CUTOFF_MARGIN_ENV: "3600"},
    }
    fields.update(overrides)
    return PodCreateRequest(**fields)  # type: ignore[arg-type]


def provider(transport: ScriptedTransport) -> RunPodProvider:
    return RunPodProvider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        now=lambda: NOW,
    )


def test_runpod_balance_source_is_injected_and_never_an_implicit_http_read() -> None:
    transport = ScriptedTransport([])
    observed = AccountBalanceObservation(Decimal("76.50"), NOW, "fake RunPod balance source")
    adapter = RunPodProvider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        balance_observer=lambda: observed,
        now=lambda: NOW,
    )

    assert adapter.observe_account_balance() is observed
    assert transport.calls == []


def test_a_balance_source_that_never_answers_becomes_a_named_timeout_refusal() -> None:
    """A hang here would leave a created pod billing with nothing recorded.

    The post-create spend assessment observes the balance after `create` has
    returned a billing pod and before anything has been armed to stop it. Every
    caller downstream already fails closed on a raised exception, so the only
    outcome that escapes them is a source that blocks instead of failing. The
    observer is never released -- that is what "blocked" means -- so the assertion
    is on the deadline the caller sees.
    """

    transport = ScriptedTransport([])
    blocked = threading.Event()
    entered = threading.Event()

    def never_answers() -> AccountBalanceObservation:
        entered.set()
        blocked.wait()
        raise AssertionError("released only by this test's cleanup")

    adapter = RunPodProvider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        balance_observer=never_answers,
        balance_timeout_seconds=0.05,
        now=lambda: NOW,
    )

    try:
        with pytest.raises(ProviderFailure, match="did not answer within"):
            adapter.observe_account_balance()
        assert entered.is_set(), "the source was never called, so nothing was bounded"
    finally:
        blocked.set()

    assert transport.calls == []


def test_a_source_that_overran_its_deadline_is_never_called_again() -> None:
    """One abandoned thread per adapter, and no second stall on a money path.

    The blocked call cannot be cancelled -- it is an arbitrary injected callable
    -- so the only question is what the next gate does. Calling again would pay
    the deadline a second time while a pod may already be billing, and abandon a
    second thread. Refusing at once is fail-closed in the direction that
    matters, and it cannot strand a running pod: closing goes through
    `VerifiedShutdown`, which never assesses spend.
    """

    transport = ScriptedTransport([])
    blocked = threading.Event()
    calls = 0

    def never_answers() -> AccountBalanceObservation:
        nonlocal calls
        calls += 1
        blocked.wait()
        raise AssertionError("released only by this test's cleanup")

    deadline = 0.5
    adapter = RunPodProvider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        balance_observer=never_answers,
        balance_timeout_seconds=deadline,
        now=lambda: NOW,
    )

    # Taken here rather than at module scope: this file's other blocked observer
    # may still be unwinding, and an upper bound measured from now tolerates
    # that while still catching accumulation.
    baseline = threading.active_count()
    try:
        with pytest.raises(ProviderFailure, match="did not answer within"):
            adapter.observe_account_balance()
        assert calls == 1

        started = time.monotonic()
        for _ in range(4):
            with pytest.raises(ProviderFailure, match="not consulted again"):
                adapter.observe_account_balance()
        elapsed = time.monotonic() - started

        # The source is untouched: four more gates would otherwise be four more
        # abandoned threads.
        assert calls == 1
        assert threading.active_count() <= baseline + 1
        # And the refusals are immediate, which the counts above cannot show. A
        # regression that waited out the deadline before refusing would leave
        # `calls` at 1 and pass every other assertion here while stalling four
        # deadlines -- 2.0 seconds -- with a created pod billing through them.
        # The bound is one whole deadline for all four, which is still roughly a
        # thousand times what four lock acquisitions need.
        assert elapsed < deadline, (
            f"four latched refusals took {elapsed:.3f}s against a {deadline}s deadline; "
            "they are supposed to refuse without consulting anything"
        )
    finally:
        blocked.set()

    assert transport.calls == []


def test_a_second_observation_during_the_first_is_refused_rather_than_started() -> None:
    """The latch check and the worker start are one transaction.

    Two callers that both read "not abandoned" before either started would both
    start a worker, and the at-most-one-abandoned-thread guarantee would hold
    for the sequential case only. Nothing in this repository drives one
    `RunPodProvider` from two threads today -- the sole production construction,
    `timer_context_from_environment`, passes no observer at all -- so this is
    the guarantee being made true before something relies on it, not a live bug
    being repaired.
    """

    transport = ScriptedTransport([])
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    answer = AccountBalanceObservation(Decimal("76.50"), NOW, "fake RunPod balance source")

    def slow_source() -> AccountBalanceObservation:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait()
        return answer

    adapter = RunPodProvider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        balance_observer=slow_source,
        # Long enough that the first observation is still in flight while the
        # second arrives; the test releases it rather than waiting this out.
        balance_timeout_seconds=30.0,
        now=lambda: NOW,
    )

    first: list[object] = []

    def observe_on_another_thread() -> None:
        try:
            first.append(adapter.observe_account_balance())
        except BaseException as error:  # noqa: BLE001 - reported on the main thread
            first.append(error)

    caller = threading.Thread(target=observe_on_another_thread)
    caller.start()
    try:
        assert entered.wait(10), "the first observation never reached the source"
        with pytest.raises(ProviderFailure, match="already in progress"):
            adapter.observe_account_balance()
        # Refused, not queued and not started: the source saw one call.
        assert calls == 1
    finally:
        release.set()
        caller.join(10)

    assert first == [answer]
    assert not caller.is_alive()
    # The in-flight refusal is not the latch: an ordinary observation still works
    # once the first one has finished.
    assert adapter.observe_account_balance() is answer
    assert calls == 2
    assert transport.calls == []


def test_a_failing_balance_source_still_raises_its_own_error_not_the_timeout() -> None:
    """Bounding the call must not rewrite the cause of an ordinary failure."""

    transport = ScriptedTransport([])

    def refuses() -> AccountBalanceObservation:
        raise ProviderFailure("balance endpoint returned 503")

    adapter = RunPodProvider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        balance_observer=refuses,
        balance_timeout_seconds=30.0,
        now=lambda: NOW,
    )

    with pytest.raises(ProviderFailure, match="returned 503"):
        adapter.observe_account_balance()


def test_runpod_without_an_observed_balance_source_refuses_without_http() -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ProviderFailure, match="balance source"):
        provider(transport).observe_account_balance()

    assert transport.calls == []


def pod_payload(**overrides: object) -> dict[str, object]:
    """One documented v1 Pod object, as POST/GET return it."""

    payload: dict[str, object] = {
        "id": "pod-1",
        "name": "safe-pod",
        "desiredStatus": "RUNNING",
        "costPerHr": 0.77,
        "interruptible": False,
        "image": "registry.example/verbatus@sha256:" + "a" * 64,
        "templateId": "template-immutable-reference",
        "volumeMountPath": "/workspace/private",
        "dockerStartCmd": list(request().docker_start_cmd),
        "networkVolume": {"id": "volume-1"},
        "machine": {"gpuTypeId": "NVIDIA RTX 6000 Ada Generation"},
        "env": {"VERBATUS_LAUNCH_TOKEN": TOKEN, BILLING_CUTOFF_MARGIN_ENV: "3600"},
        "lastStartedAt": "2026-08-08T11:59:00Z",
    }
    payload.update(overrides)
    return payload


def billing_row(**overrides: object) -> dict[str, object]:
    """One documented v1 billing record: a bare-array row, no metadata envelope."""

    row: dict[str, object] = {
        "podId": "pod-1",
        "amount": "1.23",
        "time": "2026-08-08T12:00:00Z",
        "timeBilledMs": 3_600_000,
        "gpuTypeId": "NVIDIA RTX 6000 Ada Generation",
        "diskSpaceBilledGb": 50,
    }
    row.update(overrides)
    return row


def json_response(value: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(value).encode())


# -- estimate and the price sheet -----------------------------------------


def test_estimate_uses_explicit_price_resolvers_not_a_provider_page() -> None:
    estimate = provider(ScriptedTransport([])).estimate(request())

    assert estimate.pod_hourly_usd == Decimal("0.77")
    assert estimate.volume_hourly_usd == Decimal("0.05")


# -- create, and the launch token that keeps an ambiguous POST recoverable --


def test_create_correlates_the_launch_token_before_it_posts() -> None:
    transport = ScriptedTransport([json_response([]), json_response(pod_payload(), 201)])

    record = provider(transport).create(request())

    assert record.pod_id == "pod-1"
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/pods?includeMachine=true&includeNetworkVolume=true"),
        ("POST", "/pods"),
    ]
    body = transport.calls[1][2]
    assert body is not None
    assert body["interruptible"] is False
    assert body["networkVolumeId"] == "volume-1"
    assert body["volumeMountPath"] == "/workspace/private"
    assert body["gpuTypeIds"] == ["NVIDIA RTX 6000 Ada Generation"]
    assert body["env"] == {"VERBATUS_LAUNCH_TOKEN": TOKEN, BILLING_CUTOFF_MARGIN_ENV: "3600"}


def test_create_returns_the_existing_token_pod_without_posting_again() -> None:
    transport = ScriptedTransport([json_response([pod_payload()])])

    record = provider(transport).create(request())

    assert record.pod_id == "pod-1"
    assert [method for method, _, _ in transport.calls] == ["GET"]


def test_recovery_only_create_never_posts_and_says_so_when_nothing_matched() -> None:
    transport = ScriptedTransport([json_response([])])

    with pytest.raises(ProviderFailure, match="no create request was issued"):
        provider(transport).create(request().recovery_request())

    assert [method for method, _, _ in transport.calls] == ["GET"]


def test_a_name_match_whose_env_is_absent_refuses_rather_than_matching_on_name() -> None:
    transport = ScriptedTransport([json_response([{"id": "pod-9", "name": "safe-pod"}])])

    with pytest.raises(ProviderFailure, match="returned no env"):
        provider(transport).create(request())

    assert [method for method, _, _ in transport.calls] == ["GET"]


def test_two_pods_carrying_one_launch_token_refuse_rather_than_choose() -> None:
    transport = ScriptedTransport([json_response([pod_payload(), pod_payload(id="pod-2")])])

    with pytest.raises(ProviderFailure, match="more than one pod carrying this exact launch token"):
        provider(transport).create(request())


def test_create_without_a_launch_token_refuses_before_any_request() -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ProviderFailure, match="VERBATUS_LAUNCH_TOKEN"):
        provider(transport).create(request(metadata={}))

    assert transport.calls == []


# -- the effective runtime contract the pod actually got -------------------


def test_the_runtime_contract_reports_what_the_provider_says_it_created() -> None:
    transport = ScriptedTransport([json_response([]), json_response(pod_payload(), 201)])

    record = provider(transport).create(request())

    assert record.runtime_contract is not None
    assert record.runtime_contract.matches(request())


def test_an_interruptible_pod_is_refused_rather_than_recorded() -> None:
    transport = ScriptedTransport(
        [json_response([]), json_response(pod_payload(interruptible=True), 201)]
    )

    with pytest.raises(ProviderFailure, match="silent-loss machine"):
        provider(transport).create(request())


def test_a_missing_interruptible_field_is_never_read_as_on_demand() -> None:
    payload = pod_payload()
    del payload["interruptible"]
    transport = ScriptedTransport([json_response([]), json_response(payload, 201)])

    with pytest.raises(ProviderFailure, match="on-demand cannot be assumed"):
        provider(transport).create(request())


def test_a_pod_with_no_attached_volume_is_refused() -> None:
    payload = pod_payload()
    del payload["networkVolume"]
    transport = ScriptedTransport([json_response([]), json_response(payload, 201)])

    with pytest.raises(ProviderFailure, match="no attached network volume"):
        provider(transport).create(request())


# -- adopt ----------------------------------------------------------------


def test_adopt_reads_the_exact_pod_and_carries_its_runtime_contract() -> None:
    transport = ScriptedTransport([json_response(pod_payload())])

    record = provider(transport).adopt("pod-1")

    assert record.pod_id == "pod-1"
    assert record.runtime_contract is not None
    assert record.estimate.pod_hourly_usd == Decimal("0.77")
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/pods/pod-1?includeMachine=true&includeNetworkVolume=true")
    ]


def test_adopting_an_absent_pod_refuses_rather_than_inventing_a_record() -> None:
    transport = ScriptedTransport([HttpResponse(404, b"{}")])

    with pytest.raises(ProviderFailure, match="reports it absent"):
        provider(transport).adopt("pod-1")


@pytest.mark.parametrize("desired_status", ["EXITED", "TERMINATED"])
def test_adopting_a_non_running_pod_is_refused_with_its_status_named(desired_status: str) -> None:
    """desiredStatus is validated then must actually gate adoption (audit-d Finding 13).

    A pod that is EXITED or TERMINATED can still answer 200 on GET; walking it
    through the full paid-gate would manufacture a lease and controller
    receipt for a pod that will never run.
    """

    transport = ScriptedTransport([json_response(pod_payload(desiredStatus=desired_status))])

    with pytest.raises(ProviderFailure, match=f"desiredStatus is {desired_status!r}, not RUNNING"):
        provider(transport).adopt("pod-1")


@pytest.mark.parametrize("pod_id", [".", "..", "pod\nheader", "pod/child", "pod?query"])
def test_provider_refuses_unsafe_pod_ids_before_transport(pod_id: str) -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ProviderFailure, match="unsafe for a path"):
        provider(transport).adopt(pod_id)
    assert transport.calls == []


# -- status, list absence, terminate ---------------------------------------


def test_status_list_absence_and_terminate_use_documented_v1_paths_and_shapes() -> None:
    transport = ScriptedTransport(
        [HttpResponse(404, b"{}"), json_response([{"id": "other-pod"}]), HttpResponse(204, b"")]
    )
    runpod = provider(transport)

    status = runpod.status("pod-1")
    listed = runpod.verify_absent("pod-1")
    runpod.terminate("pod-1")

    assert status.presence is Presence.ABSENT
    assert status.http_status == 404
    assert status.provider_state is None
    assert listed.presence is Presence.ABSENT
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/pods/pod-1"),
        ("GET", "/pods?includeMachine=true&includeNetworkVolume=true"),
        ("DELETE", "/pods/pod-1"),
    ]


def test_status_parses_desiredstatus_verbatim_from_the_200_body_it_already_fetches() -> None:
    """An EXITED pod is still PRESENT to this seam -- lifecycle is a separate
    fact from presence, and the word is reported exactly as the provider
    spelled it, never normalized against the create/adopt vocabulary."""

    transport = ScriptedTransport([json_response(pod_payload(desiredStatus="EXITED"))])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.PRESENT
    assert status.provider_state == "EXITED"


def test_status_reports_a_lifecycle_word_unknown_to__pod_states_rather_than_raising() -> None:
    """`status` is an observation, not a gate: an unfamiliar future lifecycle
    word must reach the caller verbatim, and casing must survive untouched,
    or a live supervisor polling a billing pod gets a raised ProviderFailure
    on a read-only GET the moment RunPod renames a state word."""

    transport = ScriptedTransport([json_response(pod_payload(desiredStatus="Provisioning_v3"))])

    status = provider(transport).status("pod-1")

    assert status.provider_state == "Provisioning_v3"


def test_status_yields_no_lifecycle_word_when_the_200_body_omits_desiredstatus() -> None:
    payload = pod_payload()
    del payload["desiredStatus"]
    transport = ScriptedTransport([json_response(payload)])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.PRESENT
    assert status.provider_state is None


@pytest.mark.parametrize("malformed", [123, "", "   "])
def test_status_drops_a_malformed_desiredstatus_and_names_it_rather_than_reporting_it(
    malformed: object,
) -> None:
    """A malformed value is not the same fact as an absent one: `None` from
    this seam must never mean "the provider actually sent something we could
    not use" without that being visible somewhere -- here, in `detail`."""

    transport = ScriptedTransport([json_response(pod_payload(desiredStatus=malformed))])

    status = provider(transport).status("pod-1")

    assert status.provider_state is None
    assert f"unusable desiredStatus {malformed!r}" in status.detail


def test_pod_timer_reuses_the_prearmed_launch_lease_identity() -> None:
    context = timer_context_from_environment(
        {
            "RUNPOD_POD_ID": "pod-1",
            "RUNPOD_API_KEY": "test-" + "capability",
            "VERBATUS_VOLUME_ID": "volume-1",
            "VERBATUS_HARD_DEADLINE": "2026-08-08T13:00:00Z",
            "VERBATUS_REQUESTED_AT": "2026-08-08T12:00:00Z",
            "VERBATUS_POD_HOURLY_USD": "0.77",
            "VERBATUS_VOLUME_ONGOING_HOURLY_USD": "0.05",
            BILLING_CUTOFF_MARGIN_ENV: "3600",
            "VERBATUS_LAUNCH_TOKEN": TOKEN,
        }
    )

    assert context.timer.lease.lease_id == TOKEN
    assert context.timer.lease.launch_token == TOKEN
    assert context.timer.shutdown.billing_cutoff_margin_seconds == 3600


@pytest.mark.parametrize("margin", ["3601", "01"])
def test_pod_timer_refuses_an_unbounded_or_noncanonical_sealed_cutoff_margin(
    margin: str,
) -> None:
    environment = {
        "RUNPOD_POD_ID": "pod-1",
        "RUNPOD_API_KEY": "test-" + "capability",
        "VERBATUS_VOLUME_ID": "volume-1",
        "VERBATUS_HARD_DEADLINE": "2026-08-08T13:00:00Z",
        "VERBATUS_REQUESTED_AT": "2026-08-08T12:00:00Z",
        "VERBATUS_POD_HOURLY_USD": "0.77",
        "VERBATUS_VOLUME_ONGOING_HOURLY_USD": "0.05",
        BILLING_CUTOFF_MARGIN_ENV: margin,
        "VERBATUS_LAUNCH_TOKEN": TOKEN,
    }

    with pytest.raises(ProviderFailure, match="VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS"):
        timer_context_from_environment(environment)


def test_a_present_pod_reports_present_with_its_200() -> None:
    transport = ScriptedTransport([json_response(pod_payload())])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.PRESENT
    assert status.http_status == 200


def test_terminate_treats_a_404_as_an_idempotent_repeat_not_an_error() -> None:
    transport = ScriptedTransport([HttpResponse(404, b"{}")])

    provider(transport).terminate("pod-1")

    assert [(method, path) for method, path, _ in transport.calls] == [("DELETE", "/pods/pod-1")]
    assert transport.responses == []


def test_terminate_refuses_an_undocumented_status_rather_than_assuming_success() -> None:
    transport = ScriptedTransport([HttpResponse(500, b"upstream fell over")])

    with pytest.raises(ProviderFailure, match="terminate returned HTTP 500"):
        provider(transport).terminate("pod-1")


def test_the_adapter_refuses_a_v2_shaped_pod_list_rather_than_reading_absence() -> None:
    transport = ScriptedTransport([json_response({"pods": []})])

    with pytest.raises(ProviderFailure, match="not the documented bare array"):
        provider(transport).verify_absent("pod-1")


@pytest.mark.parametrize("pods", [[{}], ["not-a-pod"]])
def test_malformed_list_entries_refuse_rather_than_read_as_absence(pods: list[object]) -> None:
    transport = ScriptedTransport([json_response(pods)])

    with pytest.raises(ProviderFailure, match="pod-list entry"):
        provider(transport).verify_absent("pod-1")


# -- billing ---------------------------------------------------------------


def test_billing_captures_exact_pod_amounts_from_the_bare_v1_array() -> None:
    transport = ScriptedTransport([json_response([billing_row()])])

    capture = provider(transport).capture_cost("pod-1", NOW, NOW + timedelta(minutes=1))

    assert capture.state is BillingState.CAPTURED
    assert capture.total_usd == Decimal("1.23")
    assert capture.window_start_at == NOW
    assert capture.cutoff_at == NOW + timedelta(minutes=1)
    path = transport.calls[0][1]
    assert path.startswith("/billing/pods?podId=pod-1")
    assert "bucketSize=hour" in path
    assert "grouping=podId" in path


def test_an_empty_billing_response_is_unavailable_never_zero() -> None:
    transport = ScriptedTransport([json_response([])])

    capture = provider(transport).capture_cost("pod-1", NOW, NOW + timedelta(minutes=1))

    assert capture.state is BillingState.UNAVAILABLE
    assert capture.total_usd is None
    assert "zero was not inferred" in capture.reason


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            {"amount": "5.00", "time": "2026-08-08T12:00:00Z", "timeBilledMs": 1},
            "does not name the requested pod",
        ),
        (billing_row(podId="other-pod"), "does not name the requested pod"),
        (billing_row(amount="not-money"), "structurally unverifiable"),
        (billing_row(timeBilledMs="lots"), "invalid timeBilledMs"),
        (billing_row(time="2026-08-01T00:00:00Z"), "outside the requested window"),
        (billing_row(time="2026-08-09T00:00:00Z"), "outside the requested window"),
    ],
)
def test_unbindable_billing_rows_are_unavailable_not_verified(
    row: dict[str, object], reason: str
) -> None:
    transport = ScriptedTransport([json_response([row])])

    capture = provider(transport).capture_cost("pod-1", NOW, NOW + timedelta(minutes=1))

    assert capture.state is BillingState.UNAVAILABLE
    assert capture.total_usd is None
    assert reason in capture.reason


def test_the_hour_bucket_containing_creation_is_allowed_to_start_before_the_window() -> None:
    """One bucket of slack, and no more: a bucket start is not a window start."""

    started = NOW + timedelta(minutes=30)
    transport = ScriptedTransport([json_response([billing_row()])])

    capture = provider(transport).capture_cost("pod-1", started, started + timedelta(minutes=1))

    assert capture.state is BillingState.CAPTURED


def test_a_v2_shaped_billing_envelope_refuses_rather_than_totalling_nothing() -> None:
    transport = ScriptedTransport([json_response({"records": [], "metadata": {}})])

    with pytest.raises(ProviderFailure, match="not the documented bare array"):
        provider(transport).capture_cost("pod-1", NOW, NOW + timedelta(minutes=1))


def test_a_non_200_billing_response_refuses_rather_than_reporting_no_cost() -> None:
    transport = ScriptedTransport([HttpResponse(503, b"try later")])

    with pytest.raises(ProviderFailure, match="billing returned HTTP 503"):
        provider(transport).capture_cost("pod-1", NOW, NOW + timedelta(minutes=1))


# -- the seam boundary -----------------------------------------------------


def test_provider_endpoint_vocabulary_is_isolated_to_the_runpod_adapter() -> None:
    from pathlib import Path

    pod_root = Path(__file__).resolve().parent
    sources = [source for source in pod_root.glob("*.py") if not source.name.startswith("test_")]

    for marker in ("rest.runpod.io", "api.runpod.io", "RUNPOD_"):
        occurrences = [source for source in sources if marker in source.read_text(encoding="utf-8")]
        assert occurrences == [pod_root / "provider_runpod.py"], marker


# -- response-size bound ----------------------------------------------------


class _OversizedStream:
    def read(self, amount: int) -> bytes:
        return b"x" * amount


class _ExactSizedStream:
    """Exactly the cap, then EOF, serving no more than the bytes it was asked for.

    It used to answer every ``read`` with the full cap regardless of ``amount``,
    which no stream does and which a reader accumulating short reads correctly
    reads as an over-cap body.  The case under test is unchanged: a response of
    exactly ``_MAX_RESPONSE_BYTES`` is allowed through.
    """

    def __init__(self) -> None:
        self.remaining = _MAX_RESPONSE_BYTES

    def read(self, amount: int) -> bytes:
        served = min(amount, self.remaining)
        self.remaining -= served
        return b"y" * served


class _ShortReadStream:
    """Serves one small chunk per call, as ``read(amt)`` is documented to be free to."""

    def __init__(self, body: bytes, *, chunk: int) -> None:
        self.body = body
        self.chunk = chunk
        self.offset = 0
        self.calls = 0

    def read(self, amount: int) -> bytes:
        self.calls += 1
        served = self.body[self.offset : self.offset + min(amount, self.chunk)]
        self.offset += len(served)
        return served


def test_bounded_read_refuses_a_response_over_the_size_cap() -> None:
    with pytest.raises(ProviderFailure, match="exceeded"):
        _bounded_read(_OversizedStream())  # type: ignore[arg-type]


def test_bounded_read_allows_a_response_exactly_at_the_size_cap() -> None:
    body = _bounded_read(_ExactSizedStream())  # type: ignore[arg-type]
    assert len(body) == _MAX_RESPONSE_BYTES


def test_bounded_read_accumulates_a_short_read_instead_of_truncating_it() -> None:
    """``HTTPResponse.read(amt)`` returns *up to* ``amt`` bytes.

    A single read that stops early would hand `_json` a truncated billing
    response, which is then refused as malformed -- a money-path answer lost to
    a transport detail rather than to anything RunPod said.
    """

    payload = json.dumps([{"id": "pod-1", "amount": "0.42"}]).encode("utf-8")
    stream = _ShortReadStream(payload, chunk=7)

    body = _bounded_read(stream)  # type: ignore[arg-type]

    assert body == payload
    assert stream.calls > 1, "the fake never short-read, so the accumulation was not exercised"


# -- the live transport's own behaviour ------------------------------------
#
# The one thing in this file that cannot be proven through the fake transport:
# the leak is in urllib, underneath the seam. Two loopback servers, no network.


def test_the_live_transport_does_not_hand_the_bearer_token_to_a_redirect() -> None:
    """urllib re-sends Authorization across a redirect, cross-host included."""

    import http.server
    import threading

    seen: dict[str, str | None] = {}

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        transport = UrllibRunPodTransport(
            "test-capability-value",
            timeout_seconds=5.0,
            root=f"http://127.0.0.1:{redirector.server_address[1]}",
        )
        with pytest.raises(ProviderFailure, match="redirect was not followed"):
            transport.request("GET", "/pods")
    finally:
        for server in (target, redirector):
            server.shutdown()
            server.server_close()

    assert "authorization" not in seen


# --- audit/pod-money-path: red paths the 2026-08-12 independent audit found untested ---


def test_provider_money_fields_never_exist_as_binary_floats() -> None:
    """`costPerHr` arrives as a JSON number; it must land as an exact Decimal
    without a float intermediate (config/spend.toml: money does not survive
    that).  The literal below has more digits than a double can carry, and it
    is spliced into the raw response bytes so no Python float exists on the
    way in -- through a float, the tail digits are lost and this fails."""

    exact = "0.1234567890123456789"
    body = json.dumps(pod_payload()).replace("0.77", exact).encode()
    transport = ScriptedTransport(
        [
            HttpResponse(200, json.dumps([]).encode()),
            HttpResponse(200, body),
        ]
    )

    record = provider(transport).create(request())

    assert record.estimate.pod_hourly_usd == Decimal(exact)


def test_exact_token_recovery_finds_a_renamed_pod_without_a_second_post() -> None:
    """A provider- or console-side rename must not hide the pod this client
    already paid for: an invisible pod means a second POST for one authorised
    launch."""

    renamed = pod_payload(name="console-renamed-this-pod")
    transport = ScriptedTransport([HttpResponse(200, json.dumps([renamed]).encode())])

    record = provider(transport).create(request())

    assert record.pod_id == "pod-1"
    assert [call[0] for call in transport.calls] == ["GET"]


def test_a_same_name_pod_with_no_env_still_refuses_token_correlation() -> None:
    no_env = pod_payload()
    del no_env["env"]
    transport = ScriptedTransport([HttpResponse(200, json.dumps([no_env]).encode())])

    with pytest.raises(ProviderFailure, match="returned no env"):
        provider(transport).create(request())

    # The refusal happened during correlation: the list GET ran, no POST did.
    assert [call[0] for call in transport.calls] == ["GET"]
