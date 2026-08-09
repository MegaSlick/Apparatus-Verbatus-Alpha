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
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from .models import BillingState, PodCreateRequest, Presence, ProviderFailure
from .provider_runpod import HttpResponse, RunPodProvider

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
            '["python","-m","operations.pod.bootstrap"]',
            "--report-path",
            "/workspace/private/pod-runtime-report.json",
        ),
        "hard_deadline": NOW + timedelta(hours=1),
        "repository_commit": "b" * 40,
        "metadata": {"VERBATUS_LAUNCH_TOKEN": TOKEN},
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
        "env": {"VERBATUS_LAUNCH_TOKEN": TOKEN},
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
        ("GET", "/pods"),
        ("POST", "/pods"),
    ]
    body = transport.calls[1][2]
    assert body is not None
    assert body["interruptible"] is False
    assert body["networkVolumeId"] == "volume-1"
    assert body["volumeMountPath"] == "/workspace/private"
    assert body["gpuTypeIds"] == ["NVIDIA RTX 6000 Ada Generation"]
    assert body["env"] == {"VERBATUS_LAUNCH_TOKEN": TOKEN}


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
    assert [(method, path) for method, path, _ in transport.calls] == [("GET", "/pods/pod-1")]


def test_adopting_an_absent_pod_refuses_rather_than_inventing_a_record() -> None:
    transport = ScriptedTransport([HttpResponse(404, b"{}")])

    with pytest.raises(ProviderFailure, match="reports it absent"):
        provider(transport).adopt("pod-1")


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
    assert listed.presence is Presence.ABSENT
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/pods/pod-1"),
        ("GET", "/pods"),
        ("DELETE", "/pods/pod-1"),
    ]


def test_a_present_pod_reports_present_with_its_200() -> None:
    transport = ScriptedTransport([json_response(pod_payload())])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.PRESENT
    assert status.http_status == 200


def test_terminate_treats_a_404_as_an_idempotent_repeat_not_an_error() -> None:
    transport = ScriptedTransport([HttpResponse(404, b"{}")])

    provider(transport).terminate("pod-1")


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
