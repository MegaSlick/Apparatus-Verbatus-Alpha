"""The RunPod REST v2 adapter, exercised through a fake transport only.

Every payload below is built from the shapes RunPod's v2 documentation
publishes (read 2026-09-24; the pages are named in `provider_runpod.py`'s
module docstring). No live call has been made, so these tests prove the
adapter's *handling* of a documented shape, never that the provider answers
that way. `test_provider_runpod.py` keeps the v1 shapes until v1 is deleted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from . import provider_runpod
from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    BillingState,
    CloseState,
    PodCreateRequest,
    Presence,
    ProviderFailure,
)
from .provider import PodProvider
from .provider_runpod import (
    RUNPOD_DEFAULT_ROUTE,
    RUNPOD_REST_ROOT,
    RUNPOD_V2_ROOT,
    HttpResponse,
    RunPodProvider,
    RunPodV2Provider,
    UrllibRunPodTransport,
    live_runpod_provider,
    timer_context_from_environment,
)
from .shutdown import VerifiedShutdown

UTC = timezone.utc
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
TOKEN = "a" * 32
CREATED = "2026-09-24T11:40:00Z"


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
            # PLACEHOLDER, as in test_provider_runpod.py: not a real request.
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


def exec_args(command: tuple[str, ...] | None = None) -> str:
    argv = request().docker_start_cmd if command is None else command
    return json.dumps({"entrypoint": [argv[0]], "cmd": list(argv[1:])})


def pod_payload(**overrides: object) -> dict[str, object]:
    """One documented v2 Pod object, as create (201) and get (200) return it."""

    payload: dict[str, object] = {
        "id": "pod-1",
        "name": "safe-pod",
        "status": "RUNNING",
        "actions": ["stop", "restart", "terminate"],
        "image": "registry.example/verbatus@sha256:" + "a" * 64,
        "args": exec_args(),
        "disk": 200,
        "ports": [],
        "env": {"VERBATUS_LAUNCH_TOKEN": TOKEN, BILLING_CUTOFF_MARGIN_ENV: "3600"},
        "registry": None,
        "mounts": {"network": [{"volumeId": "volume-1", "path": "/workspace/private"}]},
        "gpu": {"id": "NVIDIA RTX 6000 Ada Generation", "count": 1, "vcpuCount": 16, "memory": 64},
        "cloud": "SECURE",
        "dataCenterId": "US-KS-2",
        "cudaVersion": "12.8",
        "ssh": {},
        "template": "template-immutable-reference",
        "cost": 0.77,
        "locked": False,
        "globalNetworking": {"enabled": False},
        "runtime": None,
        "createdAt": CREATED,
        "startedAt": "2026-09-24T11:42:00Z",
    }
    payload.update(overrides)
    return payload


def page(pods: list[object], next_cursor: str | None = None) -> dict[str, object]:
    return {
        "pods": pods,
        "pagination": {"nextCursor": next_cursor, "hasNextPage": next_cursor is not None},
    }


def json_response(value: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(value).encode())


def problem(status: int, title: str, detail: str, **extra: object) -> HttpResponse:
    return json_response({"title": title, "status": status, "detail": detail, **extra}, status)


def provider(transport: ScriptedTransport) -> RunPodV2Provider:
    return RunPodV2Provider(
        transport,
        pod_price=lambda gpu: Decimal("0.77"),
        volume_price=lambda volume: Decimal("0.05"),
        now=lambda: NOW,
    )


@pytest.fixture
def on_demand_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the project lead's reviewed decision, for the paths behind it."""

    monkeypatch.setattr(provider_runpod, "V2_ON_DEMAND_BASIS", "test basis, not a real one")


# -- route selection --------------------------------------------------------


def test_v2_is_the_default_route_and_v1_stays_selectable() -> None:
    price = {"pod_price": lambda gpu: Decimal("1"), "volume_price": lambda volume: Decimal("0")}

    default = live_runpod_provider("test-capability-value", **price)
    v1 = live_runpod_provider("test-capability-value", route="v1", **price)

    assert RUNPOD_DEFAULT_ROUTE == "v2"
    assert type(default) is RunPodV2Provider
    assert default.transport.root == RUNPOD_V2_ROOT
    assert type(v1) is RunPodProvider
    assert v1.transport.root == RUNPOD_REST_ROOT
    with pytest.raises(ValueError, match="route must be one of"):
        live_runpod_provider("test-capability-value", route="v3", **price)


def test_each_route_refuses_a_live_transport_pointed_at_the_other_root() -> None:
    price = {"pod_price": lambda gpu: Decimal("1"), "volume_price": lambda volume: Decimal("0")}

    with pytest.raises(ValueError, match="REST v2"):
        RunPodV2Provider(UrllibRunPodTransport("test-capability-value"), **price)
    with pytest.raises(ValueError, match="REST v1"):
        RunPodProvider(UrllibRunPodTransport("test-capability-value", root=RUNPOD_V2_ROOT), **price)


def test_the_v2_adapter_satisfies_the_seven_verb_seam() -> None:
    assert isinstance(provider(ScriptedTransport([])), PodProvider)


def timer_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "RUNPOD_POD_ID": "pod-1",
        "RUNPOD_API_KEY": "test-" + "capability",
        "VERBATUS_VOLUME_ID": "volume-1",
        "VERBATUS_HARD_DEADLINE": "2026-09-24T13:00:00Z",
        "VERBATUS_REQUESTED_AT": "2026-09-24T12:00:00Z",
        "VERBATUS_POD_HOURLY_USD": "0.77",
        "VERBATUS_VOLUME_ONGOING_HOURLY_USD": "0.05",
        BILLING_CUTOFF_MARGIN_ENV: "3600",
        "VERBATUS_LAUNCH_TOKEN": TOKEN,
    }
    environment.update(overrides)
    return environment


def test_the_pod_timer_uses_the_default_route_unless_its_environment_names_one() -> None:
    default = timer_context_from_environment(timer_environment())
    v1 = timer_context_from_environment(timer_environment(VERBATUS_RUNPOD_ROUTE="v1"))

    assert type(default.timer.shutdown.provider) is RunPodV2Provider
    assert type(v1.timer.shutdown.provider) is RunPodProvider
    with pytest.raises(RuntimeError, match="VERBATUS_RUNPOD_ROUTE"):
        timer_context_from_environment(timer_environment(VERBATUS_RUNPOD_ROUTE="v9"))


# -- create: the on-demand stop, then the documented body and statuses --------


def test_create_refuses_before_any_post_while_on_demand_cannot_be_shown() -> None:
    transport = ScriptedTransport([json_response(page([]))])

    with pytest.raises(ProviderFailure, match="cannot show that a pod is on-demand") as refused:
        provider(transport).create(request())

    assert 'route="v1"' in str(refused.value)
    assert "V2_ON_DEMAND_BASIS" in str(refused.value)
    assert [(method, path) for method, path, _ in transport.calls] == [("GET", "/pods")]


def test_a_pod_this_launch_already_created_is_still_returned_for_closing() -> None:
    """The refusal is of a new POST, never of the pod this launch already paid for."""

    transport = ScriptedTransport([json_response(page([pod_payload()]))])

    record = provider(transport).create(request())

    assert record.pod_id == "pod-1"
    assert record.runtime_contract is None
    assert record.contract_refusal is not None
    assert "on-demand" in record.contract_refusal
    assert [method for method, _, _ in transport.calls] == ["GET"]


def test_create_posts_the_documented_nested_body(on_demand_settled: None) -> None:
    created = pod_payload(status="PROVISIONING", startedAt=None)
    transport = ScriptedTransport([json_response(page([])), json_response(created, 201)])

    record = provider(transport).create(request(container_disk_gb=200))

    method, path, body = transport.calls[1]
    assert (method, path) == ("POST", "/pods")
    assert body == {
        "name": "safe-pod",
        "cloud": "SECURE",
        "image": "registry.example/verbatus@sha256:" + "a" * 64,
        "gpu": {"id": "NVIDIA RTX 6000 Ada Generation", "count": 1},
        "disk": 200,
        "mounts": {"network": [{"volumeId": "volume-1", "path": "/workspace/private"}]},
        "args": json.dumps(
            {"entrypoint": ["python"], "cmd": list(request().docker_start_cmd[1:])},
            separators=(",", ":"),
        ),
        "env": {"VERBATUS_LAUNCH_TOKEN": TOKEN, BILLING_CUTOFF_MARGIN_ENV: "3600"},
        "startJupyter": False,
        "startSsh": False,
        "templateId": "template-immutable-reference",
    }
    assert "interruptible" not in body
    assert record.state == "PROVISIONING"
    assert record.runtime_contract is not None
    assert record.runtime_contract.matches(request(container_disk_gb=200))


def test_the_close_window_anchors_on_created_at_not_on_container_start(
    on_demand_settled: None,
) -> None:
    transport = ScriptedTransport([json_response(page([])), json_response(pod_payload(), 201)])

    record = provider(transport).create(request())

    assert record.created_at == datetime(2026, 9, 24, 11, 40, tzinfo=UTC)


def test_a_pod_without_created_at_is_refused_rather_than_anchored_on_now(
    on_demand_settled: None,
) -> None:
    payload = pod_payload()
    del payload["createdAt"]
    transport = ScriptedTransport([json_response(payload)])

    with pytest.raises(ProviderFailure, match="createdAt"):
        provider(transport).adopt("pod-1")


@pytest.mark.parametrize(
    ("status", "named"),
    [
        (402, "insufficient account balance"),
        (400, "not malformed"),
        (422, "does not match the v2 contract"),
        (409, "HTTP 409"),
        (503, "HTTP 503"),
    ],
)
def test_a_refused_create_is_named_and_never_retried(
    on_demand_settled: None, status: int, named: str
) -> None:
    transport = ScriptedTransport(
        [
            json_response(page([])),
            problem(status, "Refused", "the provider's own detail", errors=[{"field": "gpu"}]),
        ]
    )

    with pytest.raises(ProviderFailure, match=named) as refused:
        provider(transport).create(request())

    assert "the provider's own detail" in str(refused.value)
    assert [method for method, _, _ in transport.calls] == ["GET", "POST"]


def test_a_422_names_each_violation_the_provider_listed(on_demand_settled: None) -> None:
    transport = ScriptedTransport(
        [
            json_response(page([])),
            problem(422, "Unprocessable", "invalid body", errors=[{"path": "gpu.id"}]),
        ]
    )

    with pytest.raises(ProviderFailure, match="gpu.id"):
        provider(transport).create(request())


def test_a_create_answered_200_is_not_the_documented_201(on_demand_settled: None) -> None:
    transport = ScriptedTransport([json_response(page([])), json_response(pod_payload(), 200)])

    with pytest.raises(ProviderFailure, match="HTTP 200"):
        provider(transport).create(request())


def test_recovery_only_create_never_posts() -> None:
    transport = ScriptedTransport([json_response(page([]))])

    with pytest.raises(ProviderFailure, match="no create request was issued"):
        provider(transport).create(request().recovery_request())

    assert [method for method, _, _ in transport.calls] == ["GET"]


def test_create_without_a_launch_token_refuses_before_any_request() -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ProviderFailure, match="VERBATUS_LAUNCH_TOKEN"):
        provider(transport).create(request(metadata={BILLING_CUTOFF_MARGIN_ENV: "3600"}))

    assert transport.calls == []


def test_two_pods_carrying_one_launch_token_refuse_rather_than_choose() -> None:
    transport = ScriptedTransport([json_response(page([pod_payload(), pod_payload(id="pod-2")]))])

    with pytest.raises(ProviderFailure, match="more than one pod"):
        provider(transport).create(request())


def test_a_same_name_pod_with_no_env_refuses_token_correlation() -> None:
    payload = pod_payload(id="pod-2")
    del payload["env"]
    transport = ScriptedTransport([json_response(page([payload]))])

    with pytest.raises(ProviderFailure, match="returned no env"):
        provider(transport).create(request())


# -- the pod list: every page, or a refusal -------------------------------------


def test_token_correlation_follows_every_page_of_the_pod_list() -> None:
    transport = ScriptedTransport(
        [
            json_response(page([pod_payload(id="other", env={})], next_cursor="c/2+")),
            json_response(page([pod_payload()])),
        ]
    )

    record = provider(transport).create(request())

    assert record.pod_id == "pod-1"
    assert [path for _, path, _ in transport.calls] == ["/pods", "/pods?cursor=c%2F2%2B"]


def test_list_absence_reads_every_page_before_it_answers() -> None:
    transport = ScriptedTransport(
        [
            json_response(page([{"id": "other"}], next_cursor="next")),
            json_response(page([{"id": "pod-1"}])),
        ]
    )

    observed = provider(transport).verify_absent("pod-1")

    assert observed.presence is Presence.PRESENT
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([], "not an object"),
        ({"pods": []}, "pagination.hasNextPage"),
        ({"pods": [], "pagination": {"nextCursor": None}}, "pagination.hasNextPage"),
        ({"pods": [], "pagination": {"nextCursor": "x", "hasNextPage": False}}, "still names"),
        ({"pods": [], "pagination": {"nextCursor": None, "hasNextPage": True}}, "no new cursor"),
        (
            {"pods": [{"name": "no-id"}], "pagination": {"nextCursor": None, "hasNextPage": False}},
            "entry 0 id",
        ),
        (
            {"pods": ["x"], "pagination": {"nextCursor": None, "hasNextPage": False}},
            "not an object",
        ),
    ],
)
def test_a_pod_list_that_cannot_be_shown_complete_refuses_rather_than_reads_absence(
    body: object, reason: str
) -> None:
    transport = ScriptedTransport([json_response(body)])

    with pytest.raises(ProviderFailure, match=reason):
        provider(transport).verify_absent("pod-1")


def test_a_repeated_cursor_refuses_rather_than_loops() -> None:
    transport = ScriptedTransport(
        [json_response(page([], next_cursor="same")), json_response(page([], next_cursor="same"))]
    )

    with pytest.raises(ProviderFailure, match="no new cursor"):
        provider(transport).verify_absent("pod-1")


def test_a_list_longer_than_the_page_bound_refuses_rather_than_stops_early() -> None:
    pages = [
        json_response(page([], next_cursor=f"c{index}"))
        for index in range(provider_runpod._MAX_POD_LIST_PAGES)
    ]
    transport = ScriptedTransport(pages)

    with pytest.raises(ProviderFailure, match="ran past"):
        provider(transport).verify_absent("pod-1")


def test_a_non_200_pod_list_refuses_naming_the_problem() -> None:
    transport = ScriptedTransport([problem(429, "Too Many Requests", "slow down")])

    with pytest.raises(ProviderFailure, match="HTTP 429: Too Many Requests: slow down"):
        provider(transport).verify_absent("pod-1")


# -- adopt and the record ----------------------------------------------------


def test_adopt_reads_the_exact_pod_with_no_query_parameters(on_demand_settled: None) -> None:
    transport = ScriptedTransport([json_response(pod_payload())])

    record = provider(transport).adopt("pod-1")

    assert record.runtime_contract is not None
    assert record.estimate.pod_hourly_usd == Decimal("0.77")
    assert [(method, path) for method, path, _ in transport.calls] == [("GET", "/pods/pod-1")]


def test_adopt_without_an_on_demand_basis_carries_no_contract_and_says_why() -> None:
    transport = ScriptedTransport([json_response(pod_payload())])

    record = provider(transport).adopt("pod-1")

    assert record.runtime_contract is None
    assert record.contract_refusal is not None and "on-demand" in record.contract_refusal


@pytest.mark.parametrize("word", ["PROVISIONING", "STARTING", "EXITED", "ERROR", "TERMINATED"])
def test_adopting_a_pod_that_is_not_running_is_refused_naming_its_status(word: str) -> None:
    cost = 0 if word in {"EXITED", "TERMINATED"} else 0.77
    transport = ScriptedTransport([json_response(pod_payload(status=word, cost=cost))])

    with pytest.raises(ProviderFailure, match=f"status is {word!r}, not RUNNING"):
        provider(transport).adopt("pod-1")


def test_adopting_an_absent_pod_refuses() -> None:
    transport = ScriptedTransport([problem(404, "Not Found", "pod not found")])

    with pytest.raises(ProviderFailure, match="reports it absent"):
        provider(transport).adopt("pod-1")


def test_an_unrecognised_status_is_refused_by_the_record() -> None:
    transport = ScriptedTransport([json_response(pod_payload(status="HIBERNATING"))])

    with pytest.raises(ProviderFailure, match="unrecognised status"):
        provider(transport).adopt("pod-1")


def test_a_zero_rate_is_refused_where_v2_does_not_document_it() -> None:
    with pytest.raises(ProviderFailure, match="non-positive cost while PROVISIONING"):
        provider(ScriptedTransport([]))._record(pod_payload(status="PROVISIONING", cost=0))


def test_a_zero_rate_is_accepted_for_an_exited_pod_as_v2_documents() -> None:
    record = provider(ScriptedTransport([]))._record(pod_payload(status="EXITED", cost=0))

    assert record.estimate.pod_hourly_usd == Decimal("0")


@pytest.mark.parametrize(
    "mounts",
    [None, {}, {"network": []}, {"network": [{"volumeId": "v"}]}, {"network": [{}, {}]}],
)
def test_a_pod_without_one_attached_network_volume_is_refused(mounts: object) -> None:
    with pytest.raises(ProviderFailure, match="mounts.network"):
        provider(ScriptedTransport([]))._record(pod_payload(mounts=mounts))


# -- the effective runtime contract ---------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"cloud": "COMMUNITY"}, "not the SECURE requested"),
        ({"gpu": {"id": "NVIDIA RTX 6000 Ada Generation", "count": 2}}, "gpu.count 2"),
        ({"gpu": None}, "no gpu object"),
        ({"args": ""}, "reports no args"),
        ({"args": "python -m operations.pod.pod_timer"}, "exec-form"),
        ({"args": json.dumps({"cmd": ["python"]})}, "exec-form"),
        ({"args": json.dumps({"entrypoint": [], "cmd": ["python"]})}, "empty entrypoint"),
        ({"args": json.dumps({"entrypoint": ["python"], "cmd": [""]})}, "non-blank strings"),
        ({"entrypoint": ["/bin/sh"]}, "entrypoint that disagrees"),
        ({"env": None}, "reports no env"),
    ],
)
def test_an_unprovable_shape_leaves_a_closable_record_naming_the_reason(
    on_demand_settled: None, overrides: dict[str, object], reason: str
) -> None:
    record = provider(ScriptedTransport([]))._record(pod_payload(**overrides))

    assert record.runtime_contract is None
    assert record.contract_refusal is not None and reason in record.contract_refusal


def test_the_deconstructed_entrypoint_and_cmd_are_accepted_when_they_agree(
    on_demand_settled: None,
) -> None:
    argv = request().docker_start_cmd
    record = provider(ScriptedTransport([]))._record(
        pod_payload(entrypoint=[argv[0]], cmd=list(argv[1:]))
    )

    assert record.runtime_contract is not None
    assert record.runtime_contract.docker_start_cmd == argv


def test_a_start_command_the_provider_changed_does_not_match_the_request(
    on_demand_settled: None,
) -> None:
    changed = request().docker_start_cmd[:-1] + ("/workspace/private/elsewhere.json",)
    record = provider(ScriptedTransport([]))._record(pod_payload(args=exec_args(changed)))

    assert record.runtime_contract is not None
    assert not record.runtime_contract.matches(request())


# -- status and terminate ----------------------------------------------------


@pytest.mark.parametrize(
    "word", ["PROVISIONING", "STARTING", "RUNNING", "EXITED", "ERROR", "TERMINATED", "NEW"]
)
def test_status_reports_every_lifecycle_word_verbatim(word: str) -> None:
    transport = ScriptedTransport([json_response(pod_payload(status=word))])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.PRESENT
    assert status.provider_state == word
    assert transport.calls[0][:2] == ("GET", "/pods/pod-1")


def test_status_surfaces_started_at_and_reports_none_before_the_container_runs() -> None:
    started = provider(ScriptedTransport([json_response(pod_payload())])).status("pod-1")
    waiting = provider(
        ScriptedTransport([json_response(pod_payload(status="STARTING", startedAt=None))])
    ).status("pod-1")

    assert started.started_at == datetime(2026, 9, 24, 11, 42, tzinfo=UTC)
    assert waiting.started_at is None


def test_status_names_an_unparseable_started_at_rather_than_raising() -> None:
    transport = ScriptedTransport([json_response(pod_payload(startedAt="yesterday"))])

    status = provider(transport).status("pod-1")

    assert status.started_at is None
    assert "unusable startedAt" in status.detail


def test_status_reports_a_404_as_absent() -> None:
    transport = ScriptedTransport([problem(404, "Not Found", "pod not found")])

    status = provider(transport).status("pod-1")

    assert status.presence is Presence.ABSENT
    assert status.http_status == 404


@pytest.mark.parametrize("code", [204, 404])
def test_terminate_accepts_the_documented_delete_and_an_idempotent_repeat(code: int) -> None:
    transport = ScriptedTransport([HttpResponse(code, b"")])

    provider(transport).terminate("pod-1")

    assert transport.calls == [("DELETE", "/pods/pod-1", None)]


def test_terminate_names_a_cluster_pod_it_cannot_stop_and_does_not_retry() -> None:
    transport = ScriptedTransport(
        [problem(409, "Conflict", "pod belongs to cluster; cannot terminate via pod endpoints")]
    )

    with pytest.raises(ProviderFailure, match="belongs to a cluster") as refused:
        provider(transport).terminate("pod-1")

    assert "console" in str(refused.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("code", [200, 202, 500])
def test_terminate_refuses_an_undocumented_answer_rather_than_assume_success(code: int) -> None:
    transport = ScriptedTransport([HttpResponse(code, b"")])

    with pytest.raises(ProviderFailure, match=f"HTTP {code}"):
        provider(transport).terminate("pod-1")


@pytest.mark.parametrize("pod_id", [".", "..", "pod\nheader", "pod/child", "pod?query"])
def test_unsafe_pod_ids_are_refused_before_transport(pod_id: str) -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ProviderFailure, match="unsafe for a path"):
        provider(transport).status(pod_id)
    assert transport.calls == []


# -- billing -----------------------------------------------------------------

START = datetime(2026, 9, 24, 11, 40, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 24, 12, 5, tzinfo=UTC)


def billing_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "startTime": "2026-09-24T11:00:00Z",
        "endTime": "2026-09-24T12:00:00Z",
        "podId": "pod-1",
        "totalAmount": 0.21,
        "gpuAmount": 0.2,
        "cpuAmount": 0,
        "diskAmount": 0.01,
    }
    record.update(overrides)
    return record


def resolved_query(**overrides: object) -> dict[str, object]:
    query: dict[str, object] = {
        "startTime": "2026-09-24T11:00:00Z",
        "endTime": "2026-09-24T13:00:00Z",
        "bucketSize": "hour",
        "podId": "pod-1",
    }
    query.update(overrides)
    return query


def billing(
    records: list[object], *, query: object = "default", record_count: object = "auto"
) -> HttpResponse:
    metadata: dict[str, object] = {
        "recordCount": len(records) if record_count == "auto" else record_count,
        "uniquePodCount": 1 if records else 0,
        "totals": {"totalAmount": 0, "gpuAmount": 0, "cpuAmount": 0, "diskAmount": 0},
    }
    if record_count is None:
        del metadata["recordCount"]
    if query == "default":
        metadata["query"] = resolved_query()
    elif query is not None:
        metadata["query"] = query
    return json_response({"records": records, "metadata": metadata})


def test_billing_captures_exact_pod_amounts_inside_the_resolved_window() -> None:
    transport = ScriptedTransport(
        [
            billing(
                [
                    billing_record(),
                    billing_record(
                        startTime="2026-09-24T12:00:00Z",
                        endTime="2026-09-24T13:00:00Z",
                        totalAmount=0.05,
                    ),
                ]
            )
        ]
    )

    capture = provider(transport).capture_cost("pod-1", START, CUTOFF)

    assert capture.state is BillingState.CAPTURED
    assert capture.total_usd == Decimal("0.26")
    # The provider's resolved start, snapped down to the hour, is the declared
    # window start: its statement, not this adapter's echo of the request.
    assert capture.window_start_at == datetime(2026, 9, 24, 11, 0, tzinfo=UTC)
    assert capture.source == "RunPod REST v2 GET /billing/pods"
    assert "gpuAmount=0.2" in capture.lines[0].description
    path = transport.calls[0][1]
    assert path.startswith("/billing/pods?")
    assert "grouping" not in path
    assert "bucketSize=hour" in path and "podId=pod-1" in path


def test_billing_money_never_exists_as_a_binary_float() -> None:
    exact = b"0.10000000000000000555"
    body = billing([billing_record()]).body.replace(b"0.21", exact)
    transport = ScriptedTransport([HttpResponse(200, body)])

    capture = provider(transport).capture_cost("pod-1", START, CUTOFF)

    assert capture.total_usd == Decimal(exact.decode())


def test_an_empty_answer_in_a_resolved_window_is_pending_not_zero() -> None:
    transport = ScriptedTransport([billing([])])

    capture = provider(transport).capture_cost("pod-1", START, CUTOFF)

    assert capture.state is BillingState.PENDING_RECONCILIATION
    assert capture.total_usd is None
    assert "no records in it yet" in capture.reason


def test_an_empty_answer_without_a_resolved_window_is_unavailable_never_pending() -> None:
    transport = ScriptedTransport([billing([], query=None)])

    capture = provider(transport).capture_cost("pod-1", START, CUTOFF)

    assert capture.state is BillingState.UNAVAILABLE
    assert "zero was not inferred" in capture.reason


def test_records_without_a_resolved_window_fall_back_to_containment_and_say_so() -> None:
    transport = ScriptedTransport([billing([billing_record()], query=None)])

    capture = provider(transport).capture_cost("pod-1", START, CUTOFF)

    assert capture.state is BillingState.CAPTURED
    assert capture.window_start_at == START
    assert "no resolved window declared" in capture.source


@pytest.mark.parametrize(
    ("records", "query", "record_count", "reason"),
    [
        ([billing_record(podId="pod-2")], "default", "auto", "does not name the requested pod"),
        (["row"], "default", "auto", "non-object record"),
        ([billing_record(totalAmount=None)], "default", "auto", "structurally unverifiable"),
        ([billing_record(startTime=None)], "default", "auto", "structurally unverifiable"),
        ([billing_record(endTime="2026-09-24T11:00:00Z")], "default", "auto", "ends at or before"),
        (
            [billing_record(startTime="2026-09-24T09:00:00Z", endTime="2026-09-24T10:00:00Z")],
            "default",
            "auto",
            "outside the requested window",
        ),
        (
            [billing_record(startTime="2026-09-24T13:00:00Z", endTime="2026-09-24T14:00:00Z")],
            None,
            "auto",
            "outside the requested window",
        ),
        ([billing_record()], resolved_query(podId="pod-2"), "auto", "podId filter"),
        ([billing_record()], resolved_query(podId=None), "auto", "podId filter"),
        ([billing_record()], resolved_query(bucketSize="day"), "auto", "bucketSize"),
        ([billing_record()], resolved_query(startTime="2026-09-24T11:50:00Z"), "auto", "narrower"),
        ([billing_record()], resolved_query(endTime="2026-09-24T12:00:00Z"), "auto", "narrower"),
        ([billing_record()], resolved_query(endTime="soon"), "auto", "unreadable"),
        ([billing_record()], ["not", "an", "object"], "auto", "not an object"),
        ([billing_record()], "default", 2, "recordCount"),
        ([billing_record()], "default", True, "recordCount"),
    ],
)
def test_billing_that_cannot_be_bound_to_this_pod_and_window_is_unavailable(
    records: list[object], query: object, record_count: object, reason: str
) -> None:
    transport = ScriptedTransport([billing(records, query=query, record_count=record_count)])

    capture = provider(transport).capture_cost("pod-1", START, CUTOFF)

    assert capture.state is BillingState.UNAVAILABLE
    assert reason in capture.reason


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([], "not an object"),
        ({"records": []}, "documented v2 envelope"),
        ({"metadata": {}}, "documented v2 envelope"),
        ({"records": {}, "metadata": {}}, "documented v2 envelope"),
    ],
)
def test_a_billing_answer_that_is_not_the_v2_envelope_refuses(body: object, reason: str) -> None:
    transport = ScriptedTransport([json_response(body)])

    with pytest.raises(ProviderFailure, match=reason):
        provider(transport).capture_cost("pod-1", START, CUTOFF)


def test_a_non_200_billing_answer_refuses_rather_than_reports_no_cost() -> None:
    transport = ScriptedTransport([problem(403, "Forbidden", "no billing rights")])

    with pytest.raises(ProviderFailure, match="HTTP 403: Forbidden: no billing rights"):
        provider(transport).capture_cost("pod-1", START, CUTOFF)


# -- the verified close, end to end through the adapter ------------------------


def test_a_v2_close_is_verified_over_a_window_anchored_on_creation() -> None:
    """Terminate 204, exact GET 404, list absence, then billing from createdAt."""

    adapter = provider(
        ScriptedTransport(
            [
                HttpResponse(204, b""),
                problem(404, "Not Found", "pod not found"),
                json_response(page([{"id": "other"}])),
                billing([billing_record()]),
            ]
        )
    )
    record = provider(ScriptedTransport([]))._record(pod_payload())
    shutdown = VerifiedShutdown(
        adapter,
        timeout_seconds=5,
        poll_seconds=1,
        billing_cutoff_margin_seconds=3600,
        monotonic=lambda: 0.0,
        sleeper=lambda seconds: None,
        now=lambda: CUTOFF,
    )

    report = shutdown.close(record, reason="drill")

    assert report.state is CloseState.VERIFIED
    billing_path = adapter.transport.calls[-1][1]  # type: ignore[attr-defined]
    assert "startTime=2026-09-24T11%3A40%3A00Z" in billing_path


# -- the catalogue cross-check ---------------------------------------------------


def catalogue(*gpus: dict[str, object]) -> HttpResponse:
    return json_response({"gpus": list(gpus)})


def gpu(gpu_id: str, secure_price: object, *, secure: bool = True) -> dict[str, object]:
    return {
        "id": gpu_id,
        "name": gpu_id,
        "pool": "POOL",
        "manufacturer": "NVIDIA",
        "memory": 48,
        "secure": secure,
        "community": True,
        "price": {"secure": secure_price, "community": 0.1},
        "maxCount": {"secure": 8, "community": 4},
    }


def test_the_catalogue_cross_check_is_one_read_and_empty_when_the_sheet_agrees() -> None:
    transport = ScriptedTransport([catalogue(gpu("NVIDIA A40", 0.44))])

    findings = provider(transport).cross_check_catalogue({"NVIDIA A40": Decimal("0.44")})

    assert findings == ()
    assert transport.calls == [("GET", "/catalog/gpus", None)]


def test_the_catalogue_cross_check_names_every_disagreement() -> None:
    transport = ScriptedTransport(
        [
            catalogue(
                gpu("NVIDIA A40", 0.49),
                gpu("NVIDIA RTX A5000", 0.27, secure=False),
                gpu("NVIDIA L4", None),
            )
        ]
    )

    findings = provider(transport).cross_check_catalogue(
        {
            "NVIDIA A40": Decimal("0.44"),
            "NVIDIA RTX A5000": Decimal("0.27"),
            "NVIDIA L4": Decimal("0.30"),
            "NVIDIA Missing": Decimal("1.00"),
        }
    )

    assert findings == (
        "'NVIDIA A40' lists 0.49 USD/h on Secure cloud; the reviewed sheet says 0.44",
        "'NVIDIA L4' has no readable Secure list price: None",
        "'NVIDIA Missing' is not in the RunPod GPU catalogue",
        "'NVIDIA RTX A5000' is not offered on Secure cloud",
    )


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (json_response([]), "not an object"),
        (json_response({"gpus": {}}), "envelope"),
        (json_response({"gpus": [{"name": "no id"}]}), "entry 0 id"),
        (problem(401, "Unauthorized", "bad key"), "HTTP 401"),
    ],
)
def test_an_unreadable_catalogue_refuses(response: HttpResponse, reason: str) -> None:
    with pytest.raises(ProviderFailure, match=reason):
        provider(ScriptedTransport([response])).cross_check_catalogue({})
