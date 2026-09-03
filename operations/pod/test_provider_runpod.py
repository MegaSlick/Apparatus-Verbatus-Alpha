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

from . import notify_hooks
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
    BALANCE_QUERY,
    RUNPOD_GRAPHQL_ROOT,
    GraphQLBalanceObserver,
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


@pytest.mark.parametrize("padded", [" RUNNING ", "RUNNING\n", "\tRUNNING"])
def test_status_strips_a_padded_but_usable_desiredstatus_before_storing_it(
    padded: str,
) -> None:
    """The usability decision and the stored word must agree: deciding on the

    stripped word but storing the padded one let `supervise.py`'s RUNNING
    guard (which case-folds but did not used to strip) disagree with this
    seam about the same byte string, closing a healthy pod. See
    `test_supervise.py`'s companion drill for the consuming guard."""

    transport = ScriptedTransport([json_response(pod_payload(desiredStatus=padded))])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.PRESENT
    assert status.provider_state == "RUNNING"
    assert "unusable desiredStatus" not in status.detail


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


# -- the GraphQL balance observer -------------------------------------------
#
# Documented shapes only (module docstring of provider_runpod.py, 2026-09-02).
# No live call: the fake transport answers, and two loopback servers measure
# the query-placed credential the way the redirect test above measures urllib.


def balance_body(**overrides: object) -> bytes:
    myself: dict[str, object] = {"clientBalance": 76.5, "currentSpendPerHr": 0.0}
    myself.update(overrides)
    return json.dumps({"data": {"myself": myself}}).encode()


def observer(transport: ScriptedTransport) -> GraphQLBalanceObserver:
    return GraphQLBalanceObserver(transport, now=lambda: NOW)


def test_the_balance_observer_sends_exactly_the_documented_query() -> None:
    transport = ScriptedTransport([HttpResponse(200, balance_body())])

    observed = observer(transport)()

    assert transport.calls == [("POST", "/graphql", {"query": BALANCE_QUERY})]
    assert observed.available_usd == Decimal("76.5")
    assert observed.observed_at == NOW
    assert "US dollars per the vendor's billing documentation" in observed.source
    assert "currentSpendPerHr=0.0" in observed.source


def test_the_balance_never_exists_as_a_binary_float() -> None:
    exact = "0.1234567890123456789"
    body = json.dumps({"data": {"myself": {"clientBalance": 0.5, "currentSpendPerHr": 0}}})
    transport = ScriptedTransport([HttpResponse(200, body.replace("0.5", exact).encode())])

    assert observer(transport)().available_usd == Decimal(exact)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            json.dumps({"data": {"myself": {"currentSpendPerHr": 0}}}).encode(),
            "missing field data.myself.clientBalance",
        ),
        (
            json.dumps({"data": {"myself": {"clientBalance": 1}}}).encode(),
            "missing field data.myself.currentSpendPerHr",
        ),
        (json.dumps({"data": {}}).encode(), "missing field data.myself"),
        (json.dumps({"myself": {}}).encode(), "missing field data"),
        (balance_body(clientBalance=None), "clientBalance is not a number: NoneType"),
        (balance_body(clientBalance="76.5"), "clientBalance is not a number: str"),
        (balance_body(clientBalance=True), "clientBalance is not a number: bool"),
        (balance_body(currentSpendPerHr="1"), "currentSpendPerHr is not a number: str"),
        (balance_body(clientBalance=-3.25), "is -3.25; a negative"),
        (b"[]", "response is not an object"),
        (b"not json", "response is not JSON"),
        (
            json.dumps({"errors": [{"message": "Unauthorized"}], "data": None}).encode(),
            "answered with errors: Unauthorized",
        ),
    ],
)
def test_the_balance_observer_refuses_each_doubt_by_name(body: bytes, reason: str) -> None:
    transport = ScriptedTransport([HttpResponse(200, body)])

    with pytest.raises(ProviderFailure, match=reason):
        observer(transport)()


def test_a_non_200_balance_answer_is_refused_not_read_as_zero() -> None:
    transport = ScriptedTransport([HttpResponse(401, b'{"errors":[{"message":"nope"}]}')])

    with pytest.raises(ProviderFailure, match="balance query returned HTTP 401"):
        observer(transport)()


def test_a_credential_shaped_field_anywhere_in_the_balance_answer_refuses_unread() -> None:
    body = json.dumps(
        {"data": {"myself": {"clientBalance": 9, "currentSpendPerHr": 0, "apiKeys": [{"id": "k"}]}}}
    ).encode()
    transport = ScriptedTransport([HttpResponse(200, body)])

    with pytest.raises(ProviderFailure, match="credential-shaped field data.myself.apiKeys"):
        observer(transport)()


def test_the_live_transport_is_the_default_balance_source_and_a_fake_gets_none() -> None:
    live = RunPodProvider(
        UrllibRunPodTransport("test-capability-value"),
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
    )
    assert isinstance(live.balance_observer, GraphQLBalanceObserver)
    derived = live.balance_observer.transport
    assert isinstance(derived, UrllibRunPodTransport)
    assert derived.root == RUNPOD_GRAPHQL_ROOT
    assert derived.credential_placement == "query"
    assert derived.capability == "test-capability-value"

    faked = provider(ScriptedTransport([]))
    assert faked.balance_observer is None
    with pytest.raises(ProviderFailure, match="balance source was not configured"):
        faked.observe_account_balance()


def test_the_default_observer_runs_through_the_bounded_balance_read() -> None:
    """The adapter's own timeout and latch apply to the built-in source too."""

    transport = ScriptedTransport([HttpResponse(200, balance_body(clientBalance=12))])
    adapter = provider(transport)
    adapter.balance_observer = observer(transport)

    assert adapter.observe_account_balance().available_usd == Decimal("12")


# -- the balance-observation notification hook -------------------------------


def test_a_bare_live_transport_carries_no_notify_hook() -> None:
    """``--notify`` is the single gate for a phone notification: absent it,
    not even a live-transport provider's default balance observer may carry
    one.

    A pod's own ``timer_context_from_environment`` builds exactly this
    unqualified constructor call, with no way to pass ``--notify`` through --
    so if this defaulted to a hook, a plain ``verbatus pod create`` with no
    ``--notify`` would still page a phone from the pod on every spend
    assessment, before any --notify gate was ever consulted. It must not.
    """

    live = RunPodProvider(
        UrllibRunPodTransport("test-capability-value"),
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
    )

    assert isinstance(live.balance_observer, GraphQLBalanceObserver)
    assert live.balance_observer.notify is None


def test_a_live_transport_built_with_balance_notify_carries_the_hook() -> None:
    """The opposite case: a caller that explicitly supplies ``balance_notify``
    -- the host CLI, only when ``args.notify`` is set -- gets it wired into
    the default observer.

    Never invoked here: calling it would spawn the real
    ``operations/notify/notify.sh``.
    """

    seen: list[tuple[Decimal, Decimal | None]] = []
    live = RunPodProvider(
        UrllibRunPodTransport("test-capability-value"),
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        balance_notify=lambda balance, spend: seen.append((balance, spend)),
    )

    assert isinstance(live.balance_observer, GraphQLBalanceObserver)
    assert live.balance_observer.notify is not None
    assert seen == []


def test_an_injected_observer_carries_no_notify_hook_by_default() -> None:
    """`GraphQLBalanceObserver` built directly, as most of this file's tests
    build it, never touches the shell -- matching `balance_observer=None`'s
    own default two classes up."""

    assert observer(ScriptedTransport([])).notify is None


def test_a_successful_observation_calls_notify_with_balance_and_spend() -> None:
    transport = ScriptedTransport(
        [HttpResponse(200, balance_body(clientBalance=76.5, currentSpendPerHr=1.99))]
    )
    seen: list[tuple[Decimal, Decimal]] = []
    live = GraphQLBalanceObserver(
        transport, now=lambda: NOW, notify=lambda balance, spend: seen.append((balance, spend))
    )

    observed = live()

    assert observed.available_usd == Decimal("76.5")
    assert seen == [(Decimal("76.5"), Decimal("1.99"))]


def test_a_raising_notify_hook_never_prevents_the_observation() -> None:
    """Ruling (b): notifications never become new enforcement."""

    transport = ScriptedTransport([HttpResponse(200, balance_body())])

    def _explode(balance: Decimal, spend: Decimal) -> None:
        raise RuntimeError("notify.sh is not on PATH")

    live = GraphQLBalanceObserver(transport, now=lambda: NOW, notify=_explode)

    observed = live()

    assert observed.available_usd == Decimal("76.5")
    # ... and the failure is not swallowed: GOVERNANCE 2 wants it visible where
    # the money decision is written, which is the observation's own source.
    assert "balance notification raised and was contained" in observed.source
    assert "notify.sh is not on PATH" in observed.source


def test_a_notification_that_was_never_delivered_is_named_in_the_observation() -> None:
    """A ping refused on sight or not delivered is a fact about this reading."""

    transport = ScriptedTransport([HttpResponse(200, balance_body())])
    outcome = notify_hooks.NotifyOutcome(True, False, "no topic configured")
    live = GraphQLBalanceObserver(transport, now=lambda: NOW, notify=lambda *_: outcome)

    observed = live()

    assert "balance notification: Phone notification: NOT DELIVERED" in observed.source
    assert "no topic configured" in observed.source


def test_a_delivered_notification_leaves_the_observation_source_alone() -> None:
    transport = ScriptedTransport([HttpResponse(200, balance_body())])
    outcome = notify_hooks.NotifyOutcome(True, True, "delivered")
    live = GraphQLBalanceObserver(transport, now=lambda: NOW, notify=lambda *_: outcome)

    observed = live()

    assert "balance notification" not in observed.source


def test_set_balance_notify_wires_the_hook_the_host_cli_reaches() -> None:
    """The seam ``cli.py --notify`` calls by duck type.

    The comment here used to claim the CLI wired ``notify_hooks.notify_balance``
    into this observer, and no tracked path could: the provider comes from an
    untracked ``--provider-factory``, so the constructor argument had no caller.
    A named method on the returned object is the one place the host CLI can
    reach a vendor adapter, exactly as ``--record-fixture`` reaches
    ``record_exchanges``.
    """

    live = RunPodProvider(
        UrllibRunPodTransport("test-capability-value"),
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
    )
    assert isinstance(live.balance_observer, GraphQLBalanceObserver)
    assert live.balance_observer.notify is None

    def hook(balance: Decimal, spend: Decimal | None) -> notify_hooks.NotifyOutcome:
        return notify_hooks.NotifyOutcome(True, True, "delivered")

    live.set_balance_notify(hook)

    assert live.balance_observer.notify is hook


def test_set_balance_notify_refuses_a_provider_with_no_balance_source() -> None:
    """A fake transport builds no observer, so ``--notify`` cannot conjure one.

    Refused by name rather than silently doing nothing: a caller that asked for
    balance pings and will get none has to be told which.
    """

    faked = provider(ScriptedTransport([]))
    assert faked.balance_observer is None

    with pytest.raises(ValueError, match="no balance source to notify from"):
        faked.set_balance_notify(lambda balance, spend: None)


def test_set_balance_notify_refuses_an_injected_observer() -> None:
    """An injected observer is an opaque callable; wiring into it is not this
    adapter's to do, and pretending otherwise would report a hook that is not
    there."""

    faked = provider(ScriptedTransport([]))
    faked.balance_observer = lambda: None

    with pytest.raises(ValueError, match="injected"):
        faked.set_balance_notify(lambda balance, spend: None)


def _serve(handler_class):  # type: ignore[no-untyped-def]
    import http.server

    server = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_graphql_transport_places_the_key_in_the_documented_query_and_no_header() -> None:
    """Loopback only: what urllib actually sends, measured, never a live call."""

    import http.server

    seen: dict[str, object] = {}

    class Endpoint(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            seen["content_type"] = self.headers.get("Content-Type")
            seen["body"] = json.loads(self.rfile.read(length))
            payload = balance_body(clientBalance=41.25, currentSpendPerHr=0.77)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    server = _serve(Endpoint)
    try:
        transport = UrllibRunPodTransport(
            "test-capability-value",
            timeout_seconds=5.0,
            root=f"http://127.0.0.1:{server.server_address[1]}",
            credential_placement="query",
        )
        observed = GraphQLBalanceObserver(transport, now=lambda: NOW)()
    finally:
        server.shutdown()
        server.server_close()

    # Composed, not spelled: the repository's credential scanner reads a literal
    # `api_key=<value>` as a secret, and its caution is worth more than the line.
    assert seen["path"] == "/graphql?" + "=".join(("api_key", transport.capability))
    assert seen["authorization"] is None
    assert seen["content_type"] == "application/json"
    assert seen["body"] == {"query": BALANCE_QUERY}
    assert observed.available_usd == Decimal("41.25")
    assert "currentSpendPerHr=0.77" in observed.source


def test_the_query_placed_key_is_not_carried_across_a_redirect_either() -> None:
    import http.server

    seen: dict[str, str] = {}

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    target = _serve(Target)
    redirector = _serve(Redirector)
    try:
        transport = UrllibRunPodTransport(
            "test-capability-value",
            timeout_seconds=5.0,
            root=f"http://127.0.0.1:{redirector.server_address[1]}",
            credential_placement="query",
        )
        with pytest.raises(ProviderFailure, match="redirect was not followed") as refused:
            GraphQLBalanceObserver(transport, now=lambda: NOW)()
    finally:
        for server in (target, redirector):
            server.shutdown()
            server.server_close()

    assert "path" not in seen
    assert "test-capability-value" not in str(refused.value)


def test_a_query_placed_key_refuses_a_path_that_already_has_a_query() -> None:
    transport = UrllibRunPodTransport("test-capability-value", credential_placement="query")

    with pytest.raises(ProviderFailure, match="already carries a query string"):
        transport.request("GET", "/graphql?x=1")


def test_a_connection_failure_in_query_placement_names_no_key() -> None:
    """A refused connection on a closed loopback port: the message carries the
    reason, never the URL the key rides in."""

    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    transport = UrllibRunPodTransport(
        "test-capability-value",
        timeout_seconds=2.0,
        root=f"http://127.0.0.1:{port}",
        credential_placement="query",
    )

    with pytest.raises(ProviderFailure, match="HTTP request failed") as refused:
        transport.request("POST", "/graphql", {"query": BALANCE_QUERY})

    assert "test-capability-value" not in str(refused.value)


def test_record_exchanges_writes_every_exchange_scrubbed_and_replayable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from .fixture import SCRUBBED, FixtureRecorder, read_fixture

    created = pod_payload()
    transport = ScriptedTransport(
        [
            HttpResponse(200, json.dumps([]).encode()),
            HttpResponse(200, json.dumps(created).encode()),
        ]
    )
    balance_transport = ScriptedTransport([HttpResponse(200, balance_body())])
    adapter = provider(transport)
    adapter.balance_observer = observer(balance_transport)
    recorder = FixtureRecorder(tmp_path / "evidence" / "fixture.jsonl", now=lambda: NOW)

    adapter.record_exchanges(recorder)
    adapter.observe_account_balance()
    record = adapter.create(request())

    records = read_fixture(recorder.path)
    assert [(entry["method"], entry["path"], entry["status"]) for entry in records] == [
        ("POST", "/graphql", 200),
        ("GET", "/pods?includeMachine=true&includeNetworkVolume=true", 200),
        ("POST", "/pods", 200),
    ]
    balance, listing, create = records
    assert balance["verbatim"] is True and balance["response_body"] == balance_body().decode()
    assert balance["body_kind"] == "json-text"
    assert listing["verbatim"] is True and listing["response_body"] == "[]"
    assert listing["body_kind"] == "json-text"
    # The launch token rides in `env` both ways, and the predicate scrubs it.
    assert create["request_body"]["env"]["VERBATUS_LAUNCH_TOKEN"] == SCRUBBED
    assert create["verbatim"] is False
    assert create["body_kind"] == "json-object"
    assert create["response_body"]["env"]["VERBATUS_LAUNCH_TOKEN"] == SCRUBBED
    assert "response_body.env.VERBATUS_LAUNCH_TOKEN" in create["scrubbed"]
    # Money in a scrubbed body is still a number, and the same digits.
    assert create["response_body"]["costPerHr"] == Decimal("0.77")
    assert record.pod_id == created["id"]
    assert (recorder.path.stat().st_mode & 0o777) == 0o600


def test_an_existing_fixture_file_is_narrowed_to_0600_before_anything_is_recorded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The mode is set on the file that is there, not only on one this creates.

    `os.open`'s mode argument applies at creation, so a path an earlier step
    left readable stayed readable while the recorder appended the provider's
    own answers to it. The recorder narrows the descriptor it just opened, and
    the first record is written only after that succeeded.
    """

    from .fixture import FixtureRecorder, read_fixture

    path = tmp_path / "prior.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)

    recorder = FixtureRecorder(path, now=lambda: NOW)
    recorder.record("GET", "/pods", None, status=200, response_body=b"[]")
    recorder.close()

    assert (path.stat().st_mode & 0o777) == 0o600
    assert len(read_fixture(path)) == 1


def test_a_body_wearing_the_decimal_mark_is_recorded_as_the_string_it_is(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A provider body cannot forge a money value on its way to disk.

    The Decimal mark used to be a fixed sentinel rewritten by a regex over the
    whole serialized line, and a recorded string keeps whatever NUL bytes it
    carried (`_scrub_body` decodes with errors="replace"). A body echoing
    NUL + `decimal:` + digits + NUL would have been rewritten from a JSON
    string into a bare number: evidence altered with no record of it
    (GOVERNANCE 4). The mark now carries a per-line nonce chosen after the
    body was read.
    """

    from .fixture import FixtureRecorder, read_fixture

    forged = "\x00decimal:99999\x00"
    recorder = FixtureRecorder(tmp_path / "forged.jsonl", now=lambda: NOW)
    recorder.record(
        "GET",
        "/pods",
        None,
        status=200,
        response_body=json.dumps({"costPerHr": 0.77, "note": forged}).encode(),
    )
    recorder.close()

    [entry] = read_fixture(recorder.path)
    body = (
        json.loads(entry["response_body"])
        if isinstance(entry["response_body"], str)
        else entry["response_body"]
    )
    assert body["note"] == forged
    assert body["costPerHr"] == 0.77


def test_a_non_finite_money_value_is_refused_rather_than_written_as_a_marker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`NaN` and `Infinity` are not JSON numbers.

    Marked and left unconverted, one would have written a literal marker string
    into the fixture, which the replay side reads back as a shape no test
    covers. A named failure is the honest answer.
    """

    from .fixture import _dumps

    with pytest.raises(ValueError, match="non-finite Decimal"):
        _dumps({"total": Decimal("NaN")})


def test_a_credential_shaped_value_under_an_innocuous_key_is_scrubbed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The name check alone let a token through under a harmless key.

    The recorder is the one place on this branch that deliberately writes
    provider request and response bodies to disk; a RunPod answer echoing a key
    inside `dockerArgs`, `message` or an env value would have landed there
    marked `verbatim: true` with an empty `scrubbed` list.

    The probe is an opaque mixed-alphanumeric run rather than a value carrying
    a vendor prefix: this repository's own ingress scanner recognises the
    prefixed shape and would refuse the commit that added the test.
    """

    from .fixture import SCRUBBED, FixtureRecorder, read_fixture

    recorder = FixtureRecorder(tmp_path / "leaky.jsonl", now=lambda: NOW)
    recorder.record(
        "POST",
        "/pods",
        None,
        status=200,
        response_body=json.dumps(
            {"message": "started with Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8", "id": "pod-abc123"}
        ).encode(),
    )
    recorder.close()

    [entry] = read_fixture(recorder.path)
    assert entry["verbatim"] is False
    assert entry["response_body"]["message"] == SCRUBBED
    assert "response_body.message" in entry["scrubbed"]
    # And an ordinary provider id is still recorded as itself: the shape test
    # is narrow on purpose, or the fixture stops being replayable.
    assert entry["response_body"]["id"] == "pod-abc123"


def test_concurrent_record_calls_never_share_a_sequence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`record_exchanges` shares one recorder between a provider's own

    transport and its balance observer's, and the observer runs on a daemon
    thread that a caller can abandon on overrun -- its blocked call can still
    be inside `record` when the main thread records its own exchange. Before
    the lock covered the whole method, `self.sequence` was re-read nine
    lines after being incremented, so two concurrent calls could stamp the
    same sequence and skip one entirely.
    """

    from .fixture import FixtureRecorder, read_fixture

    class _BlockingBody(dict):
        """A request body whose ``items()`` parks a thread mid-``_scrub``,

        after `record` has already claimed a sequence number and before it
        stamps the record with it -- exactly the window the race lived in.
        """

        def __init__(
            self,
            *args: object,
            release: threading.Event,
            entered: threading.Event,
            **kwargs: object,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._release = release
            self._entered = entered

        def items(self):  # type: ignore[no-untyped-def]
            self._entered.set()
            self._release.wait(timeout=5.0)
            return super().items()

    recorder = FixtureRecorder(tmp_path / "evidence" / "concurrent.jsonl", now=lambda: NOW)
    release = threading.Event()
    entered = threading.Event()
    parked_body = _BlockingBody({"note": "parked"}, release=release, entered=entered)

    def record_parked() -> None:
        recorder.record("GET", "/parked", parked_body, status=200, response_body=b"{}")

    thread = threading.Thread(target=record_parked)
    thread.start()
    # Wait for the parked call to have actually claimed its sequence and
    # entered `items()`, rather than guessing at a sleep -- a bare sleep
    # cannot promise the handoff happened before the main thread proceeds.
    assert entered.wait(timeout=5.0)
    recorder.record("GET", "/second", {"note": "unparked"}, status=200, response_body=b"{}")
    release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    recorder.close()

    records = read_fixture(recorder.path)
    sequences = [record["sequence"] for record in records]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert [record["path"] for record in sorted(records, key=lambda r: r["sequence"])] == [
        "/parked",
        "/second",
    ]
