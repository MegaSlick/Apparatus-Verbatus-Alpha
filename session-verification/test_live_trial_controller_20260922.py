"""Offline money-transition checks for the session-only RunPod v2 helper."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping
import unittest
from unittest import mock

MODULE_PATH = Path(
    os.environ.get(
        "VERBATUS_LIVE_CONTROLLER_PATH",
        str(Path(__file__).with_name("live_trial_controller_20260922.py")),
    )
)
if not MODULE_PATH.is_file():
    MODULE_PATH = Path(__file__).parents[1] / "tools" / "live_trial_controller_20260922.py"
SPEC = importlib.util.spec_from_file_location("live_trial_controller_20260922", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


def authorization(identity: Mapping[str, object], now: dt.datetime | None = None) -> dict[str, object]:
    now = now or controller.utc_now()
    value = controller.auth_template(identity)
    value.update(
        {
            "authorize_paid_actions": True,
            "authorized_at": controller.iso(now - dt.timedelta(minutes=1)),
            "expires_at": controller.iso(now + dt.timedelta(minutes=29)),
            "acknowledges_combined_hourly_above_usd_1": True,
            "data_center_id": "EU-RO-1",
            "max_pod_hourly_usd": "1.618",
            "max_volume_hourly_usd": "0.029",
            "max_combined_hourly_usd": "1.647",
            "merged_repository_commit": "a" * 40,
        }
    )
    return value


def make_session(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, object]]:
    session_dir = controller.prepare_session(tmp_path)
    identity = controller.session_identity(session_dir)
    return session_dir, identity, authorization(identity)


def volume(identity: Mapping[str, object], auth: Mapping[str, object], volume_id: str = "vol-1") -> dict[str, object]:
    return {"id": volume_id, **controller.expected_volume(identity, auth)}


def deadlines(session_dir: Path, auth: Mapping[str, object]) -> dict[str, object]:
    life = controller.lifecycle(session_dir)
    controller.record_deadlines(life, auth, controller.utc_now())
    controller.write_lifecycle(session_dir, life)
    return {
        "hard_deadline_epoch": life["hard_deadline_epoch"],
        "cleanup_epoch": life["cleanup_epoch"],
    }


def pod(
    identity: Mapping[str, object],
    auth: Mapping[str, object],
    volume_id: str,
    bound_deadlines: Mapping[str, object],
    key: str = "private-test-key",
    pod_id: str = "pod-1",
) -> dict[str, object]:
    args, entrypoint, cmd = controller.pod_command()
    return {
        "id": pod_id,
        "name": identity["pod_name"],
        "status": "PROVISIONING",
        "actions": ["terminate"],
        "image": auth["image"],
        "args": args,
        "entrypoint": entrypoint,
        "cmd": cmd,
        "disk": auth["pod_disk_gb"],
        "mounts": {"network": [{"volumeId": volume_id, "path": "/workspace/private"}]},
        "ports": ["22/tcp"],
        "env": controller.pod_environment(identity, bound_deadlines, key),
        "registry": None,
        "cloud": auth["cloud"],
        "dataCenterId": auth["data_center_id"],
        "gpu": {"id": auth["gpu_id"], "count": auth["gpu_count"], "vcpuCount": 16, "memory": 120},
        "cost": float(str(auth["expected_pod_hourly_usd"])),
        "createdAt": controller.iso(controller.utc_now()),
    }


class ScriptedTransport:
    def __init__(self, answers: list[tuple[str, str, object]]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def __call__(
        self, method: str, route: str, body: Mapping[str, object] | None
    ) -> tuple[int, object | None]:
        self.calls.append((method, route, body))
        if not self.answers:
            raise AssertionError(f"unexpected call {method} {route}")
        expected_method, expected_route, result = self.answers.pop(0)
        assert method == expected_method
        assert route == expected_route
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, tuple)
        return result


def pod_page(rows: list[dict[str, object]], *, next_cursor: str | None = None) -> dict[str, object]:
    return {
        "pods": rows,
        "pagination": {"hasNextPage": next_cursor is not None, "nextCursor": next_cursor},
    }


def case_outbound_request_explicitly_binds_entrypoint_and_cmd(tmp_path: Path) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    bound_deadlines = deadlines(session_dir, auth)
    request = controller.pod_request(
        identity, auth, "vol-1", bound_deadlines, "private-test-key"
    )
    encoded = json.loads(request["args"])
    args, entrypoint, cmd = controller.pod_command()
    assert request["args"] == args
    assert encoded == {"entrypoint": entrypoint, "cmd": cmd}


def case_authorization_is_exact_session_closed_and_explicit_about_above_target(
    test: unittest.TestCase, tmp_path: Path
) -> None:
    _, identity, auth = make_session(tmp_path)
    checked = controller.validate_authorization(auth, identity)
    assert checked["authorization_sha256"]

    auth["acknowledges_combined_hourly_above_usd_1"] = False
    with test.assertRaisesRegex(controller.Refusal, "above USD 1/hour"):
        controller.validate_authorization(auth, identity)

    auth["acknowledges_combined_hourly_above_usd_1"] = True
    auth["unreviewed"] = True
    with test.assertRaisesRegex(controller.Refusal, "closed shape"):
        controller.validate_authorization(auth, identity)

    future = authorization(identity)
    future["authorized_at"] = controller.iso(controller.utc_now() + dt.timedelta(minutes=5))
    with test.assertRaisesRegex(controller.Refusal, "not currently valid"):
        controller.validate_authorization(future, identity)


def case_pod_inventory_walks_every_page_and_includes_cluster_pods() -> None:
    transport = ScriptedTransport(
        [
            (
                "GET",
                "pods?includeClusterPods=true&limit=1000",
                (200, pod_page([{"id": "one"}], next_cursor="opaque")),
            ),
            (
                "GET",
                "pods?includeClusterPods=true&limit=1000&cursor=opaque",
                (200, pod_page([{"id": "two"}])),
            ),
        ]
    )
    assert [row["id"] for row in controller.RunPodV2(transport).list_pods()] == ["one", "two"]
    assert not transport.answers


def case_uncertain_volume_create_is_never_reposted_and_exact_match_is_adopted(
    tmp_path: Path,
) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    deadlines(session_dir, auth)
    row = volume(identity, auth)
    transport = ScriptedTransport(
        [
            ("POST", "network-volumes", controller.TransportUncertain("timeout")),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            ("GET", "network-volumes", (200, {"networkVolumes": [row]})),
            ("GET", "network-volumes/vol-1", (200, row)),
        ]
    )
    api = controller.RunPodV2(transport)
    assert controller.create_volume_once(api, session_dir, identity, auth) is None
    assert controller.create_volume_once(api, session_dir, identity, auth) == "vol-1"
    assert [call[0] for call in transport.calls].count("POST") == 1


def case_volume_billing_anchor_is_durable_before_post_and_cross_hour_close(
    tmp_path: Path,
) -> None:
    session_dir = controller.prepare_session(tmp_path)
    identity = controller.session_identity(session_dir)
    observed = controller.utc_now()
    before_post = (observed - dt.timedelta(hours=1)).replace(
        minute=59, second=59, microsecond=0
    )
    after_response = before_post + dt.timedelta(seconds=2)
    auth = authorization(identity, now=before_post)
    checked = controller.validate_authorization(auth, identity, now=before_post)
    clock = [before_post]
    with mock.patch.object(controller, "utc_now", side_effect=lambda: clock[0]):
        deadlines(session_dir, checked)
        row = volume(identity, checked)

        def create_transport(method, route, body):
            assert (method, route) == ("POST", "network-volumes")
            assert body == controller.expected_volume(identity, checked)
            assert (
                controller.lifecycle(session_dir)["volume_billing_anchor"]
                == controller.iso(before_post)
            )
            clock[0] = after_response
            return 201, row

        volume_id = controller.create_volume_once(
            controller.RunPodV2(create_transport), session_dir, identity, checked
        )
    assert volume_id == "vol-1"
    life = controller.lifecycle(session_dir)
    assert life["volume_billing_anchor"] == controller.iso(before_post)

    with controller.lifecycle_locked(session_dir) as current:
        current["authorization_public"] = {
            key: value for key, value in checked.items() if key != "authorization_sha256"
        }
        current["close_requested_at"] = controller.iso(after_response)
        current["billing_cutoff"] = controller.iso(after_response)
        current["volume_delete_requested_at"] = controller.iso(after_response)
        current["volume_billing_cutoff"] = controller.iso(after_response)
    volume_route = "billing/network-volumes?" + controller.urllib.parse.urlencode(
        {
            "networkVolumeId": "vol-1",
            "startTime": controller.iso(before_post),
            "endTime": controller.iso(after_response),
            "bucketSize": "hour",
        }
    )
    transport = ScriptedTransport(
        [
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            ("DELETE", "network-volumes/vol-1", (204, None)),
            ("GET", "network-volumes/vol-1", (404, None)),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            (
                "GET",
                volume_route,
                (
                    200,
                    billing(
                        "networkVolumeId", "vol-1", before_post, after_response
                    ),
                ),
            ),
        ]
    )
    result = controller.close_resources(controller.RunPodV2(transport), session_dir)
    assert result["green"] is True
    assert not transport.answers


def case_uncertain_pod_create_is_never_reposted_and_get_contract_is_rechecked(
    tmp_path: Path,
) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    checked = controller.validate_authorization(auth, identity)
    bound_deadlines = deadlines(session_dir, checked)
    life = controller.lifecycle(session_dir)
    life["volume_id"] = "vol-1"
    controller.write_lifecycle(session_dir, life)
    row = pod(identity, checked, "vol-1", bound_deadlines)
    transport = ScriptedTransport(
        [
            ("POST", "pods", controller.TransportUncertain("timeout")),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([row]))),
            ("GET", "pods/pod-1", (200, row)),
        ]
    )
    api = controller.RunPodV2(transport)
    assert (
        controller.create_pod_once(
            api, session_dir, identity, checked, bound_deadlines, "private-test-key"
        )
        is None
    )
    assert (
        controller.create_pod_once(
            api, session_dir, identity, checked, bound_deadlines, "private-test-key"
        )
        == "pod-1"
    )
    assert [call[0] for call in transport.calls].count("POST") == 1
    assert "private-test-key" not in (session_dir / "events.jsonl").read_text()
    assert "private-test-key" not in (session_dir / "launch-plan-redacted.json").read_text()


def case_mismatched_created_pod_is_immediately_sent_to_delete(
    test: unittest.TestCase, tmp_path: Path
) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    checked = controller.validate_authorization(auth, identity)
    bound_deadlines = deadlines(session_dir, checked)
    life = controller.lifecycle(session_dir)
    life["volume_id"] = "vol-1"
    controller.write_lifecycle(session_dir, life)
    row = pod(identity, checked, "vol-1", bound_deadlines)
    row["disk"] = 199
    transport = ScriptedTransport(
        [("POST", "pods", (201, row)), ("DELETE", "pods/pod-1", (204, None))]
    )
    with test.assertRaisesRegex(controller.Refusal, "pod response disk"):
        controller.create_pod_once(
            controller.RunPodV2(transport),
            session_dir,
            identity,
            checked,
            bound_deadlines,
            "private-test-key",
        )
    assert [call[:2] for call in transport.calls][-1] == ("DELETE", "pods/pod-1")


def billing(
    id_field: str,
    resource_id: str,
    start: dt.datetime,
    cutoff: dt.datetime,
) -> dict[str, object]:
    query_start = start.replace(minute=0, second=0, microsecond=0)
    query_end = cutoff.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    if id_field == "podId":
        amounts = {"totalAmount": 10, "gpuAmount": 8, "cpuAmount": 0, "diskAmount": 2}
        unique_field = "uniquePodCount"
    else:
        amounts = {"totalAmount": 10, "standardAmount": 10, "highPerformanceAmount": 0}
        unique_field = "uniqueNetworkVolumeCount"
    records: list[dict[str, object]] = []
    bucket_start = query_start
    while bucket_start < query_end:
        bucket_end = bucket_start + dt.timedelta(hours=1)
        records.append(
            {
                id_field: resource_id,
                "startTime": controller.iso(bucket_start),
                "endTime": controller.iso(bucket_end),
                **amounts,
            }
        )
        bucket_start = bucket_end
    totals = {field: value * len(records) for field, value in amounts.items()}
    return {
        "records": records,
        "metadata": {
            "query": {
                id_field: resource_id,
                "startTime": controller.iso(query_start),
                "endTime": controller.iso(query_end),
                "bucketSize": "hour",
            },
            "recordCount": len(records),
            unique_field: 1,
            "totals": totals,
        },
    }


def close_transport(
    session_dir: Path,
    *,
    pod_billing: object,
    volume_billing: object,
) -> ScriptedTransport:
    life = controller.lifecycle(session_dir)
    pod_route = "billing/pods?" + controller.urllib.parse.urlencode(
        {
            "podId": "pod-1",
            "startTime": life["pod_created_at"],
            "endTime": life["billing_cutoff"],
            "bucketSize": "hour",
        }
    )
    volume_route = "billing/network-volumes?" + controller.urllib.parse.urlencode(
        {
            "networkVolumeId": "vol-1",
            "startTime": life["volume_billing_anchor"],
            "endTime": life["volume_billing_cutoff"],
            "bucketSize": "hour",
        }
    )
    return ScriptedTransport(
        [
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("DELETE", "pods/pod-1", (204, None)),
            ("GET", "pods/pod-1", (404, None)),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("GET", pod_route, (200, pod_billing)),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            ("DELETE", "network-volumes/vol-1", (204, None)),
            ("GET", "network-volumes/vol-1", (404, None)),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            ("GET", volume_route, (200, volume_billing)),
        ]
    )


def close_fixture(tmp_path: Path) -> tuple[Path, dt.datetime, dt.datetime]:
    session_dir, identity, auth = make_session(tmp_path)
    checked = controller.validate_authorization(auth, identity)
    created = controller.utc_now() - dt.timedelta(minutes=5)
    cutoff = controller.utc_now()
    life = controller.lifecycle(session_dir)
    controller.record_deadlines(life, checked, created)
    life.update(
        {
            "authorization_public": {k: v for k, v in checked.items() if k != "authorization_sha256"},
            "pod_id": "pod-1",
            "volume_id": "vol-1",
            "pod_created_at": controller.iso(created),
            "volume_billing_anchor": controller.iso(created),
            "close_requested_at": controller.iso(cutoff),
            "billing_cutoff": controller.iso(cutoff),
            "volume_delete_requested_at": controller.iso(cutoff),
            "volume_billing_cutoff": controller.iso(cutoff),
        }
    )
    controller.write_lifecycle(session_dir, life)
    return session_dir, created, cutoff


def case_close_requires_both_absence_proofs_and_nonempty_exact_billing(tmp_path: Path) -> None:
    session_dir, created, cutoff = close_fixture(tmp_path)
    transport = close_transport(
        session_dir,
        pod_billing=billing("podId", "pod-1", created, cutoff),
        volume_billing=billing("networkVolumeId", "vol-1", created, cutoff),
    )
    result = controller.close_resources(controller.RunPodV2(transport), session_dir)
    assert result["green"] is True
    assert result["status"] == "closed-verified"


def case_empty_billing_is_lag_not_zero_or_verified(tmp_path: Path) -> None:
    session_dir, created, cutoff = close_fixture(tmp_path)
    empty = {"records": [], "metadata": {"query": {}}}
    transport = close_transport(
        session_dir,
        pod_billing=empty,
        volume_billing=billing("networkVolumeId", "vol-1", created, cutoff),
    )
    result = controller.close_resources(controller.RunPodV2(transport), session_dir)
    assert result["green"] is False
    assert result["billing_lag"] is True
    assert result["status"] == "close-unverified"


def case_duplicate_candidates_persist_and_converge_on_later_close(tmp_path: Path) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    checked = controller.validate_authorization(auth, identity)
    created = controller.utc_now() - dt.timedelta(minutes=5)
    life = controller.lifecycle(session_dir)
    controller.record_deadlines(life, checked, created)
    life.update(
        {
            "authorization_public": {k: v for k, v in checked.items() if k != "authorization_sha256"},
            "pod_create_attempted": True,
            "pod_create_outcome": "unknown",
            "volume_create_attempted": True,
            "volume_create_outcome": "confirmed",
            "volume_id": "vol-1",
            "volume_billing_anchor": controller.iso(created),
        }
    )
    controller.write_lifecycle(session_dir, life)
    rows = [
        {"id": "pod-1", "name": identity["pod_name"], "createdAt": controller.iso(created)},
        {"id": "pod-2", "name": identity["pod_name"], "createdAt": controller.iso(created)},
    ]
    first = ScriptedTransport(
        [
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page(rows))),
            ("DELETE", "pods/pod-1", (204, None)),
            ("GET", "pods/pod-1", (200, {"id": "pod-1"})),
            ("DELETE", "pods/pod-2", (204, None)),
            ("GET", "pods/pod-2", (200, {"id": "pod-2"})),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page(rows))),
        ]
    )
    assert controller.close_resources(controller.RunPodV2(first), session_dir)["green"] is False
    saved = controller.lifecycle(session_dir)
    assert saved["pod_candidate_ids"] == ["pod-1", "pod-2"]

    cutoff = saved["billing_cutoff"]
    with controller.lifecycle_locked(session_dir) as current:
        current["volume_delete_requested_at"] = cutoff
        current["volume_billing_cutoff"] = cutoff
    pod_route_1 = "billing/pods?" + controller.urllib.parse.urlencode(
        {
            "podId": "pod-1",
            "startTime": controller.iso(created),
            "endTime": cutoff,
            "bucketSize": "hour",
        }
    )
    pod_route_2 = "billing/pods?" + controller.urllib.parse.urlencode(
        {
            "podId": "pod-2",
            "startTime": controller.iso(created),
            "endTime": cutoff,
            "bucketSize": "hour",
        }
    )
    volume_route = "billing/network-volumes?" + controller.urllib.parse.urlencode(
        {
            "networkVolumeId": "vol-1",
            "startTime": controller.iso(created),
            "endTime": cutoff,
            "bucketSize": "hour",
        }
    )
    cutoff_time = controller.parse_time(cutoff, "cutoff")
    second = ScriptedTransport(
        [
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("DELETE", "pods/pod-1", (204, None)),
            ("GET", "pods/pod-1", (404, None)),
            ("DELETE", "pods/pod-2", (204, None)),
            ("GET", "pods/pod-2", (404, None)),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("GET", pod_route_1, (200, billing("podId", "pod-1", created, cutoff_time))),
            ("GET", pod_route_2, (200, billing("podId", "pod-2", created, cutoff_time))),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            ("DELETE", "network-volumes/vol-1", (204, None)),
            ("GET", "network-volumes/vol-1", (404, None)),
            ("GET", "network-volumes", (200, {"networkVolumes": []})),
            (
                "GET",
                volume_route,
                (200, billing("networkVolumeId", "vol-1", created, cutoff_time)),
            ),
        ]
    )
    result = controller.close_resources(controller.RunPodV2(second), session_dir)
    assert result["green"] is True
    assert result["pod_candidate_ids"] == ["pod-1", "pod-2"]


def case_unknown_create_with_no_exact_name_match_never_reads_as_absent(tmp_path: Path) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    checked = controller.validate_authorization(auth, identity)
    life = controller.lifecycle(session_dir)
    controller.record_deadlines(life, checked, controller.utc_now())
    life.update(
        {
            "authorization_public": {k: v for k, v in checked.items() if k != "authorization_sha256"},
            "pod_create_attempted": True,
            "pod_create_outcome": "unknown",
            "volume_id": "vol-1",
            "volume_create_attempted": True,
            "volume_create_outcome": "confirmed",
        }
    )
    controller.write_lifecycle(session_dir, life)
    transport = ScriptedTransport(
        [
            (
                "GET",
                "pods?includeClusterPods=true&limit=1000",
                (200, pod_page([])),
            ),
            (
                "GET",
                "pods?includeClusterPods=true&limit=1000",
                (200, pod_page([])),
            ),
        ]
    )
    result = controller.close_resources(controller.RunPodV2(transport), session_dir)
    assert result["green"] is False
    assert result["pod_create_outcome_uncertain"] is True
    assert result["pod_get_404_and_full_list_absent"] is False
    assert all(call[0] != "DELETE" for call in transport.calls)


def case_runtime_ack_binds_pid1_deadline_worker_probe_and_receipt_digest(
    test: unittest.TestCase, tmp_path: Path
) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    bound_deadlines = deadlines(session_dir, auth)
    life = controller.lifecycle(session_dir)
    life["pod_id"] = "pod-1"
    controller.write_lifecycle(session_dir, life)
    receipt = {
        "schema": controller.SCHEMA_RUNTIME_RECEIPT,
        "session_id": identity["session_id"],
        "pod_id": "pod-1",
        "controller_challenge": identity["controller_challenge"],
        "hard_deadline_epoch": bound_deadlines["hard_deadline_epoch"],
        "cleanup_epoch": bound_deadlines["cleanup_epoch"],
        "pid": 1,
        "uid": 0,
        "runtime_verified": True,
        "provider_key_removed_before_start": True,
        "worker_probe": {
            "provider_env_absent": True,
            "proc1_environ_denied": True,
            "root_receipt_denied": True,
            "sudo_unavailable": True,
            "cap_eff_zero": True,
            "no_new_privs": True,
        },
    }
    receipt_path = tmp_path / "runtime-receipt.json"
    receipt_path.write_bytes(controller.canonical_bytes(receipt))
    output = tmp_path / "controller-ack.json"
    ack = controller.record_runtime_ack(session_dir, receipt_path, output)
    assert ack["runtime_receipt_sha256"] == controller.sha256_bytes(receipt_path.read_bytes())
    prepared = controller.lifecycle(session_dir)
    assert prepared["runtime_ack_prepared"] is True
    assert prepared["runtime_ack_consumed_by_pod"] is False

    pod_ack_path = tmp_path / "pod-acknowledged.json"
    pod_ack_path.write_bytes(
        controller.canonical_bytes(
            {
                "schema": "verbatus-pod-controller-acknowledgement.v1",
                "session_id": identity["session_id"],
                "pod_id": "pod-1",
                "controller_challenge": identity["controller_challenge"],
                "hard_deadline_epoch": bound_deadlines["hard_deadline_epoch"],
                "runtime_receipt_sha256": ack["runtime_receipt_sha256"],
                "acknowledged_at": controller.utc_now().timestamp(),
            }
        )
    )
    bad_pod_ack = json.loads(pod_ack_path.read_text())
    bad_pod_ack["runtime_receipt_sha256"] = "0" * 64
    bad_pod_ack_path = tmp_path / "bad-pod-acknowledged.json"
    bad_pod_ack_path.write_bytes(controller.canonical_bytes(bad_pod_ack))
    with test.assertRaisesRegex(controller.Refusal, "does not prove consumption"):
        controller.record_pod_acknowledgement(session_dir, bad_pod_ack_path)
    assert controller.lifecycle(session_dir)["runtime_ack_consumed_by_pod"] is False
    controller.record_pod_acknowledgement(session_dir, pod_ack_path)
    assert controller.lifecycle(session_dir)["runtime_ack_consumed_by_pod"] is True
    controller.record_runtime_ack(
        session_dir, receipt_path, tmp_path / "second-controller-ack.json"
    )
    repeated = controller.lifecycle(session_dir)
    assert repeated["runtime_ack_consumed_by_pod"] is True
    assert repeated["phase"] == "runtime-verified-for-inference"

    receipt["worker_probe"]["proc1_environ_denied"] = False
    receipt_path.write_bytes(controller.canonical_bytes(receipt))
    with test.assertRaisesRegex(controller.Refusal, "worker separation"):
        controller.record_runtime_ack(session_dir, receipt_path, tmp_path / "bad-ack.json")

    with test.assertRaisesRegex(controller.Refusal, "cannot read runtime receipt"):
        controller.record_runtime_ack(
            session_dir, tmp_path / "missing-receipt.json", tmp_path / "missing-ack.json"
        )


def case_deadman_source_has_last_resort_and_persistent_delete_contract() -> None:
    source = controller.POD_DEADMAN_SOURCE
    compile(source, "<pod-deadman>", "exec")
    assert "api_key=os.environ.pop('VERBATUS_RUNPOD_API_KEY',None)" in source
    assert "def best_effort_write" in source
    assert "def terminate_forever" in source
    assert "while True:" in source
    assert "except BaseException as error:" in source
    assert "range(1,5)" not in source
    assert source.index("opener.open(req,timeout=15)") < source.index(
        "best_effort_write(root/f'deadman-attempt-{attempt}.json'"
    )

    # Load definitions without invoking boot, then prove evidence-write failures and a
    # provider outage lasting beyond the old four-attempt limit do not end the loop.
    definitions = source.rsplit("try: boot()", 1)[0]
    namespace: dict[str, object] = {}
    with mock.patch.dict(
        os.environ,
        {
            "VERBATUS_RUNPOD_API_KEY": "private-test-key",
            "RUNPOD_POD_ID": "pod-1",
            "VERBATUS_SESSION_ID": "offline-deadman-test",
        },
    ):
        exec(compile(definitions, "<pod-deadman-definitions>", "exec"), namespace)

    class StopLoop(Exception):
        pass

    class FakeTime:
        sleeps = 0

        @staticmethod
        def time() -> float:
            return 1.0

        @classmethod
        def sleep(cls, _seconds: float) -> None:
            cls.sleeps += 1
            if cls.sleeps >= 6:
                raise StopLoop

    class FailingOpener:
        @staticmethod
        def open(*_args, **_kwargs):
            raise OSError("offline transport failure")

    namespace["time"] = FakeTime
    namespace["write"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("offline receipt failure")
    )
    urllib_module = namespace["urllib"]
    original_build_opener = urllib_module.request.build_opener
    urllib_module.request.build_opener = lambda *_args, **_kwargs: FailingOpener()
    try:
        try:
            namespace["terminate_forever"]("offline-failure-drill")
        except StopLoop:
            pass
        else:
            raise AssertionError("deadman retry drill did not remain active")
    finally:
        urllib_module.request.build_opener = original_build_opener
    assert FakeTime.sleeps == 6


def case_runtime_marker_permissions_allow_exact_traversal_but_deny_private_tree(
    tmp_path: Path,
) -> None:
    source = controller.POD_DEADMAN_SOURCE
    assert "runtime_parent=Path('/run/verbatus-live')" in source
    assert "os.chown(runtime_parent,0,0); os.chmod(runtime_parent,0o711)" in source
    assert "os.chown(runtime,0,0); os.chmod(runtime,0o711)" in source
    assert "os.chown(root,0,0); os.chmod(root,0o700)" in source

    def unavailable(reason: str) -> None:
        if sys.platform.startswith("linux") and os.environ.get("GITHUB_ACTIONS") == "true":
            raise AssertionError(
                f"GitHub Linux must exercise the distinct-uid permission drill: {reason}"
            )
        raise unittest.SkipTest(f"distinct-uid permission drill unavailable: {reason}")

    sudo = shutil.which("sudo")
    if sys.platform != "linux" or sudo is None:
        unavailable("Linux sudo is absent")
    identity_probe = subprocess.run(
        [sudo, "-n", "-u", "nobody", "--", sys.executable, "-c", "import os;print(os.geteuid())"],
        capture_output=True,
        text=True,
    )
    if identity_probe.returncode != 0:
        unavailable("passwordless sudo to nobody is unavailable")
    try:
        worker_uid = int(identity_probe.stdout.strip())
    except ValueError:
        unavailable("nobody uid probe was not numeric")
    if worker_uid in {0, os.geteuid()}:
        unavailable("nobody did not produce a distinct non-root uid")

    original_tmp_mode = tmp_path.stat().st_mode & 0o777
    os.chmod(tmp_path, 0o711)
    run_parent = tmp_path / "run"
    runtime_parent = run_parent / "verbatus-live"
    runtime = runtime_parent / "known-session"
    runtime.mkdir(parents=True)
    marker = runtime / "inference-enabled.json"
    marker.write_text('{"enabled":true}\n')
    os.chmod(run_parent, 0o711)
    os.chmod(runtime_parent, 0o711)
    os.chmod(runtime, 0o711)
    os.chmod(marker, 0o644)
    private_tree = tmp_path / "root-receipts"
    private_tree.mkdir()
    private_receipt = private_tree / "receipt.json"
    private_receipt.write_text('{"private":true}\n')
    os.chmod(private_tree, 0o700)
    os.chmod(private_receipt, 0o600)
    probe = """import json,os,sys
marker,parent,session,private,receipt=sys.argv[1:]
result={'uid':os.geteuid(),'marker':open(marker).read(),'parent_list_denied':False,'session_list_denied':False,'private_list_denied':False,'private_read_denied':False}
try: os.listdir(parent)
except PermissionError: result['parent_list_denied']=True
try: os.listdir(session)
except PermissionError: result['session_list_denied']=True
try: os.listdir(private)
except PermissionError: result['private_list_denied']=True
try: open(receipt).read()
except PermissionError: result['private_read_denied']=True
print(json.dumps(result,sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [
                sudo,
                "-n",
                "-u",
                "nobody",
                "--",
                sys.executable,
                "-c",
                probe,
                str(marker),
                str(runtime_parent),
                str(runtime),
                str(private_tree),
                str(private_receipt),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        os.chmod(private_tree, 0o700)
        os.chmod(run_parent, 0o700)
        os.chmod(runtime_parent, 0o700)
        os.chmod(runtime, 0o700)
        os.chmod(tmp_path, original_tmp_mode)
    result = json.loads(completed.stdout)
    assert result == {
        "uid": worker_uid,
        "marker": '{"enabled":true}\n',
        "parent_list_denied": True,
        "session_list_denied": True,
        "private_list_denied": True,
        "private_read_denied": True,
    }


def case_deadline_is_rechecked_after_intent_and_before_each_post(
    test: unittest.TestCase, tmp_path: Path
) -> None:
    session_dir, identity, auth = make_session(tmp_path / "volume")
    checked = controller.validate_authorization(auth, identity)
    life = controller.lifecycle(session_dir)
    controller.record_deadlines(life, checked, controller.utc_now() - dt.timedelta(hours=3))
    controller.write_lifecycle(session_dir, life)
    volume_transport = ScriptedTransport([])
    with test.assertRaisesRegex(controller.Refusal, "work window has closed"):
        controller.create_volume_once(
            controller.RunPodV2(volume_transport), session_dir, identity, checked
        )
    assert not volume_transport.calls
    assert controller.lifecycle(session_dir)["volume_create_attempted"] is True
    assert (
        controller.lifecycle(session_dir)["volume_create_outcome"]
        == "not-sent-window-closed"
    )

    pod_session, pod_identity, pod_auth = make_session(tmp_path / "pod")
    pod_checked = controller.validate_authorization(pod_auth, pod_identity)
    pod_life = controller.lifecycle(pod_session)
    controller.record_deadlines(
        pod_life, pod_checked, controller.utc_now() - dt.timedelta(hours=3)
    )
    pod_life["volume_id"] = "vol-1"
    controller.write_lifecycle(pod_session, pod_life)
    pod_transport = ScriptedTransport([])
    with test.assertRaisesRegex(controller.Refusal, "work window has closed"):
        controller.create_pod_once(
            controller.RunPodV2(pod_transport),
            pod_session,
            pod_identity,
            pod_checked,
            {
                "hard_deadline_epoch": pod_life["hard_deadline_epoch"],
                "cleanup_epoch": pod_life["cleanup_epoch"],
            },
            "private-test-key",
        )
    assert not pod_transport.calls
    assert controller.lifecycle(pod_session)["pod_create_attempted"] is True
    assert (
        controller.lifecycle(pod_session)["pod_create_outcome"]
        == "not-sent-window-closed"
    )


def case_launch_gates_touch_no_provider(test: unittest.TestCase, tmp_path: Path) -> None:
    messages = {
        "absent": "cannot read watchdog readiness",
        "stale": "stale or future-dated",
        "unlocked": "watchdog lock is not held",
    }
    for variant in ("absent", "stale", "unlocked"):
        session_dir, identity, auth = make_session(tmp_path / variant)
        if variant != "absent":
            heartbeat = controller.utc_now()
            if variant == "stale":
                heartbeat -= dt.timedelta(minutes=5)
            controller.durable_write(
                session_dir / "watchdog-ready.json",
                {
                    "session_id": identity["session_id"],
                    "pid": os.getpid(),
                    "heartbeat_at": controller.iso(heartbeat),
                    "lock_owned": True,
                },
            )
        transport = ScriptedTransport([])
        with test.assertRaisesRegex(controller.Refusal, messages[variant]):
            controller.launch(
                controller.RunPodV2(transport), session_dir, auth, "private-test-key"
            )
        assert not transport.calls

    session_dir, identity, auth = make_session(tmp_path / "changed-auth")
    life = controller.lifecycle(session_dir)
    controller.record_deadlines(life, auth, controller.utc_now())
    life["authorization_sha256"] = "different"
    controller.write_lifecycle(session_dir, life)
    transport = ScriptedTransport([])
    with mock.patch.object(controller, "verify_watchdog_prearmed", return_value=None):
        with test.assertRaisesRegex(controller.Refusal, "exact original authorization"):
            controller.launch(
                controller.RunPodV2(transport), session_dir, auth, "private-test-key"
            )
    assert not transport.calls

    with mock.patch.object(controller, "launch") as launch_mock:
        code = controller.main(
            [
                "launch",
                "--session-dir",
                str(session_dir),
                "--authorization",
                str(tmp_path / "does-not-need-to-exist.json"),
                "--execute-exact-session",
                "wrong-session",
                "--i-understand-this-creates-billable-resources",
            ]
        )
    assert code == 2
    launch_mock.assert_not_called()


def case_durable_stop_refuses_launch_and_both_paid_posts(
    test: unittest.TestCase, tmp_path: Path
) -> None:
    session_dir, identity, auth = make_session(tmp_path / "launch")
    controller.durable_touch(session_dir / "stop.requested.json")
    transport = ScriptedTransport([])
    with test.assertRaisesRegex(controller.Refusal, "durable stop flag is present"):
        controller.launch(
            controller.RunPodV2(transport), session_dir, auth, "private-test-key"
        )
    assert not transport.calls

    with (
        mock.patch.object(controller, "read_json", wraps=controller.read_json) as read_mock,
        mock.patch.object(controller, "load_api_key") as key_mock,
        mock.patch.object(controller, "launch") as launch_mock,
    ):
        assert (
            controller.main(
                [
                    "launch",
                    "--session-dir",
                    str(session_dir),
                    "--authorization",
                    str(tmp_path / "must-not-be-read.json"),
                    "--execute-exact-session",
                    str(identity["session_id"]),
                    "--i-understand-this-creates-billable-resources",
                ]
            )
            == 2
        )
    assert read_mock.call_count == 1
    key_mock.assert_not_called()
    launch_mock.assert_not_called()

    volume_session, volume_identity, volume_auth = make_session(tmp_path / "volume")
    volume_checked = controller.validate_authorization(volume_auth, volume_identity)
    deadlines(volume_session, volume_checked)
    controller.durable_touch(volume_session / "stop.requested.json")
    volume_transport = ScriptedTransport([])
    with test.assertRaisesRegex(controller.Refusal, "durable stop flag is present"):
        controller.create_volume_once(
            controller.RunPodV2(volume_transport),
            volume_session,
            volume_identity,
            volume_checked,
        )
    assert not volume_transport.calls
    assert (
        controller.lifecycle(volume_session)["volume_create_outcome"]
        == "not-sent-window-closed"
    )

    pod_session, pod_identity, pod_auth = make_session(tmp_path / "pod")
    pod_checked = controller.validate_authorization(pod_auth, pod_identity)
    bound_deadlines = deadlines(pod_session, pod_checked)
    with controller.lifecycle_locked(pod_session) as life:
        life["volume_id"] = "vol-1"
    controller.durable_touch(pod_session / "stop.requested.json")
    pod_transport = ScriptedTransport([])
    with test.assertRaisesRegex(controller.Refusal, "durable stop flag is present"):
        controller.create_pod_once(
            controller.RunPodV2(pod_transport),
            pod_session,
            pod_identity,
            pod_checked,
            bound_deadlines,
            "private-test-key",
        )
    assert not pod_transport.calls
    assert (
        controller.lifecycle(pod_session)["pod_create_outcome"]
        == "not-sent-window-closed"
    )


def case_lifecycle_lock_preserves_concurrent_fields(tmp_path: Path) -> None:
    session_dir, _, auth = make_session(tmp_path)
    bound_deadlines = deadlines(session_dir, auth)
    entered = threading.Event()
    release = threading.Event()

    def first() -> None:
        with controller.lifecycle_locked(session_dir) as life:
            life["pod_id"] = "pod-concurrent"
            entered.set()
            release.wait(timeout=5)

    def second() -> None:
        with controller.lifecycle_locked(session_dir) as life:
            life["volume_id"] = "volume-concurrent"

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert entered.wait(timeout=5)
    second_thread.start()
    time.sleep(0.02)
    release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    life = controller.lifecycle(session_dir)
    assert life["pod_id"] == "pod-concurrent"
    assert life["volume_id"] == "volume-concurrent"
    assert life["pod_create_attempted"] is False
    assert life["volume_create_attempted"] is False
    assert life["hard_deadline_epoch"] == bound_deadlines["hard_deadline_epoch"]
    assert life["cleanup_epoch"] == bound_deadlines["cleanup_epoch"]

    with controller.exclusive_lock(session_dir / "lifecycle.lock", blocking=False):
        with mock.patch.object(controller, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0):
            try:
                with controller.lifecycle_locked(session_dir):
                    raise AssertionError("contended lifecycle lock unexpectedly opened")
            except controller.Refusal as error:
                assert "timed out acquiring" in str(error)
            else:
                raise AssertionError("contended lifecycle lock did not fail closed")


def case_watchdog_survives_uncertain_pod_and_volume_reconciliation(tmp_path: Path) -> None:
    for resource in ("pod", "volume"):
        session_dir, identity, auth = make_session(tmp_path / resource)
        checked = controller.validate_authorization(auth, identity)
        life = controller.lifecycle(session_dir)
        controller.record_deadlines(life, checked, controller.utc_now())
        life["authorization_public"] = {
            key: value for key, value in checked.items() if key != "authorization_sha256"
        }
        if resource == "pod":
            life.update(
                {
                    "volume_id": "vol-1",
                    "pod_create_attempted": True,
                    "pod_create_outcome": "unknown",
                }
            )
            route = "pods?includeClusterPods=true&limit=1000"
        else:
            life.update(
                {
                    "volume_create_attempted": True,
                    "volume_create_outcome": "unknown",
                }
            )
            route = "network-volumes"
        controller.write_lifecycle(session_dir, life)
        transport = ScriptedTransport(
            [("GET", route, controller.TransportUncertain("timeout"))]
        )
        with (
            mock.patch.object(controller, "load_api_key", return_value="private-test-key"),
            mock.patch.object(controller, "close_until_bounded", return_value=7) as close_mock,
        ):
            assert (
                controller.watch_loop(
                    controller.RunPodV2(transport), session_dir, poll_seconds=0.001
                )
                == 7
            )
        close_mock.assert_called_once()


def case_reconcile_close_command_persists_after_uncertain_tick(tmp_path: Path) -> None:
    session_dir, _, _ = make_session(tmp_path)
    with controller.lifecycle_locked(session_dir) as life:
        life["volume_create_attempted"] = True
        life["volume_create_outcome"] = "unknown"
    with (
        mock.patch.object(controller, "load_api_key", return_value="private-test-key"),
        mock.patch.object(
            controller,
            "UrllibTransport",
            return_value=lambda _method, _route, _body: (_ for _ in ()).throw(
                AssertionError("offline command reached provider transport")
            ),
        ),
        mock.patch.object(
            controller,
            "close_resources",
            side_effect=[
                controller.TransportUncertain("first close tick uncertain"),
                {"green": True},
            ],
        ) as close_mock,
        mock.patch.object(controller.time, "sleep", return_value=None),
    ):
        assert (
            controller.main(
                ["reconcile-close", "--session-dir", str(session_dir)]
            )
            == 0
        )
    assert close_mock.call_count == 2


def case_close_error_exits_only_when_no_paid_create_was_attempted(tmp_path: Path) -> None:
    session_dir, _, _ = make_session(tmp_path)
    api = controller.RunPodV2(
        lambda _method, _route, _body: (_ for _ in ()).throw(
            AssertionError("no-attempt close reached provider transport")
        )
    )
    with (
        mock.patch.object(
            controller,
            "close_resources",
            side_effect=controller.Refusal("no recorded authorization"),
        ) as close_mock,
        mock.patch.object(controller.time, "sleep") as sleep_mock,
    ):
        assert controller.close_until_bounded(api, session_dir, poll_seconds=0.001) == 0
    close_mock.assert_called_once()
    sleep_mock.assert_not_called()
    assert "close-complete-no-paid-action-was-attempted" in (
        session_dir / "events.jsonl"
    ).read_text()


def case_billing_rejects_gaps_bad_metadata_and_bad_money() -> None:
    created = controller.utc_now().replace(minute=5, second=0, microsecond=0)
    cutoff = created.replace(minute=45)
    valid = billing("podId", "pod-1", created, cutoff)
    assert controller.billing_verified(
        valid,
        id_field="podId",
        resource_id="pod-1",
        created_at=controller.iso(created),
        cutoff=controller.iso(cutoff),
    )

    for mutation in (
        "gap",
        "count",
        "unique",
        "amount",
        "total",
        "components",
        "bucket",
        "duration",
        "boundary",
    ):
        value = json.loads(json.dumps(valid))
        if mutation == "gap":
            first = dict(value["records"][0])
            second = dict(value["records"][0])
            bucket_start = created.replace(minute=0)
            second["startTime"] = controller.iso(bucket_start + dt.timedelta(hours=2))
            second["endTime"] = controller.iso(bucket_start + dt.timedelta(hours=3))
            value["records"] = [first, second]
            value["metadata"]["recordCount"] = 2
            value["metadata"]["query"]["endTime"] = second["endTime"]
            for field in ("totalAmount", "gpuAmount", "cpuAmount", "diskAmount"):
                value["metadata"]["totals"][field] *= 2
        elif mutation == "count":
            value["metadata"]["recordCount"] = 2
        elif mutation == "unique":
            value["metadata"]["uniquePodCount"] = 2
        elif mutation == "amount":
            del value["records"][0]["diskAmount"]
        elif mutation == "total":
            value["metadata"]["totals"]["totalAmount"] = 99
        elif mutation == "components":
            value["records"][0]["gpuAmount"] = 7
            value["metadata"]["totals"]["gpuAmount"] = 7
        elif mutation == "bucket":
            value["metadata"]["query"]["bucketSize"] = "day"
        elif mutation == "duration":
            value["records"][0]["endTime"] = controller.iso(
                created.replace(minute=0) + dt.timedelta(hours=2)
            )
            value["metadata"]["query"]["endTime"] = value["records"][0]["endTime"]
        else:
            bucket_start = created.replace(minute=0)
            value["records"][0]["startTime"] = controller.iso(
                bucket_start + dt.timedelta(minutes=5)
            )
            value["records"][0]["endTime"] = controller.iso(
                bucket_start + dt.timedelta(hours=1, minutes=5)
            )
            value["metadata"]["query"]["endTime"] = controller.iso(
                bucket_start + dt.timedelta(hours=2)
            )
        assert not controller.billing_verified(
            value,
            id_field="podId",
            resource_id="pod-1",
            created_at=controller.iso(created),
            cutoff=controller.iso(cutoff),
        )

    two_hour_cutoff = created.replace(minute=0) + dt.timedelta(hours=1, minutes=45)
    two_hours = billing("podId", "pod-1", created, two_hour_cutoff)
    assert len(two_hours["records"]) == 2
    assert controller.billing_verified(
        two_hours,
        id_field="podId",
        resource_id="pod-1",
        created_at=controller.iso(created),
        cutoff=controller.iso(two_hour_cutoff),
    )


class OfflineControllerTests(unittest.TestCase):
    def temporary_path(self):
        return tempfile.TemporaryDirectory(prefix="verbatus-controller-test-")

    def test_authorization_gate(self) -> None:
        with self.temporary_path() as directory:
            case_authorization_is_exact_session_closed_and_explicit_about_above_target(
                self, Path(directory)
            )

    def test_outbound_entrypoint_and_cmd(self) -> None:
        with self.temporary_path() as directory:
            case_outbound_request_explicitly_binds_entrypoint_and_cmd(Path(directory))

    def test_paginated_inventory(self) -> None:
        case_pod_inventory_walks_every_page_and_includes_cluster_pods()

    def test_uncertain_volume_create(self) -> None:
        with self.temporary_path() as directory:
            case_uncertain_volume_create_is_never_reposted_and_exact_match_is_adopted(
                Path(directory)
            )

    def test_volume_billing_anchor(self) -> None:
        with self.temporary_path() as directory:
            case_volume_billing_anchor_is_durable_before_post_and_cross_hour_close(
                Path(directory)
            )

    def test_uncertain_pod_create(self) -> None:
        with self.temporary_path() as directory:
            case_uncertain_pod_create_is_never_reposted_and_get_contract_is_rechecked(
                Path(directory)
            )

    def test_mismatched_pod_fail_close(self) -> None:
        with self.temporary_path() as directory:
            case_mismatched_created_pod_is_immediately_sent_to_delete(self, Path(directory))

    def test_verified_close(self) -> None:
        with self.temporary_path() as directory:
            case_close_requires_both_absence_proofs_and_nonempty_exact_billing(Path(directory))

    def test_billing_lag(self) -> None:
        with self.temporary_path() as directory:
            case_empty_billing_is_lag_not_zero_or_verified(Path(directory))

    def test_duplicate_candidate_convergence(self) -> None:
        with self.temporary_path() as directory:
            case_duplicate_candidates_persist_and_converge_on_later_close(Path(directory))

    def test_unknown_create_is_not_absence(self) -> None:
        with self.temporary_path() as directory:
            case_unknown_create_with_no_exact_name_match_never_reads_as_absent(Path(directory))

    def test_runtime_ack(self) -> None:
        with self.temporary_path() as directory:
            case_runtime_ack_binds_pid1_deadline_worker_probe_and_receipt_digest(
                self, Path(directory)
            )

    def test_deadman_source_contract(self) -> None:
        case_deadman_source_has_last_resort_and_persistent_delete_contract()

    def test_runtime_marker_permissions(self) -> None:
        with self.temporary_path() as directory:
            case_runtime_marker_permissions_allow_exact_traversal_but_deny_private_tree(
                Path(directory)
            )

    def test_deadline_rechecks(self) -> None:
        with self.temporary_path() as directory:
            case_deadline_is_rechecked_after_intent_and_before_each_post(
                self, Path(directory)
            )

    def test_launch_gates_make_no_provider_call(self) -> None:
        with self.temporary_path() as directory:
            case_launch_gates_touch_no_provider(self, Path(directory))

    def test_durable_stop_gates_all_paid_posts(self) -> None:
        with self.temporary_path() as directory:
            case_durable_stop_refuses_launch_and_both_paid_posts(
                self, Path(directory)
            )

    def test_lifecycle_concurrency(self) -> None:
        with self.temporary_path() as directory:
            case_lifecycle_lock_preserves_concurrent_fields(Path(directory))

    def test_watchdog_transport_uncertainty(self) -> None:
        with self.temporary_path() as directory:
            case_watchdog_survives_uncertain_pod_and_volume_reconciliation(Path(directory))

    def test_reconcile_close_persists(self) -> None:
        with self.temporary_path() as directory:
            case_reconcile_close_command_persists_after_uncertain_tick(Path(directory))

    def test_close_without_paid_attempt_exits(self) -> None:
        with self.temporary_path() as directory:
            case_close_error_exits_only_when_no_paid_create_was_attempted(
                Path(directory)
            )

    def test_adversarial_billing(self) -> None:
        case_billing_rejects_gaps_bad_metadata_and_bad_money()


if __name__ == "__main__":
    unittest.main()
