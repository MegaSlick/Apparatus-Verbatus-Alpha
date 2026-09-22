"""Offline money-transition checks for the session-only RunPod v2 helper."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping
import unittest

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


def case_uncertain_pod_create_is_never_reposted_and_get_contract_is_rechecked(
    tmp_path: Path,
) -> None:
    session_dir, identity, auth = make_session(tmp_path)
    bound_deadlines = deadlines(session_dir, auth)
    life = controller.lifecycle(session_dir)
    life["volume_id"] = "vol-1"
    controller.write_lifecycle(session_dir, life)
    row = pod(identity, auth, "vol-1", bound_deadlines)
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
            api, session_dir, identity, auth, bound_deadlines, "private-test-key"
        )
        is None
    )
    assert (
        controller.create_pod_once(
            api, session_dir, identity, auth, bound_deadlines, "private-test-key"
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
    bound_deadlines = deadlines(session_dir, auth)
    life = controller.lifecycle(session_dir)
    life["volume_id"] = "vol-1"
    controller.write_lifecycle(session_dir, life)
    row = pod(identity, auth, "vol-1", bound_deadlines)
    row["disk"] = 199
    transport = ScriptedTransport(
        [("POST", "pods", (201, row)), ("DELETE", "pods/pod-1", (204, None))]
    )
    with test.assertRaisesRegex(controller.Refusal, "pod response disk"):
        controller.create_pod_once(
            controller.RunPodV2(transport),
            session_dir,
            identity,
            auth,
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
    return {
        "records": [
            {
                id_field: resource_id,
                "startTime": controller.iso(query_start),
                "endTime": controller.iso(query_end),
                "totalAmount": 0.01,
            }
        ],
        "metadata": {
            "query": {
                id_field: resource_id,
                "startTime": controller.iso(query_start),
                "endTime": controller.iso(query_end),
                "bucketSize": "hour",
            },
            "recordCount": 1,
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
            "startTime": life["volume_created_at"],
            "endTime": life["volume_billing_cutoff"],
            "bucketSize": "hour",
        }
    )
    return ScriptedTransport(
        [
            ("DELETE", "pods/pod-1", (204, None)),
            ("GET", "pods/pod-1", (404, None)),
            ("GET", "pods?includeClusterPods=true&limit=1000", (200, pod_page([]))),
            ("GET", pod_route, (200, pod_billing)),
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
            "volume_created_at": controller.iso(created),
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
            )
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

    receipt["worker_probe"]["proc1_environ_denied"] = False
    receipt_path.write_bytes(controller.canonical_bytes(receipt))
    with test.assertRaisesRegex(controller.Refusal, "worker separation"):
        controller.record_runtime_ack(session_dir, receipt_path, tmp_path / "bad-ack.json")


class OfflineControllerTests(unittest.TestCase):
    def temporary_path(self):
        return tempfile.TemporaryDirectory(prefix="verbatus-controller-test-")

    def test_authorization_gate(self) -> None:
        with self.temporary_path() as directory:
            case_authorization_is_exact_session_closed_and_explicit_about_above_target(
                self, Path(directory)
            )

    def test_paginated_inventory(self) -> None:
        case_pod_inventory_walks_every_page_and_includes_cluster_pods()

    def test_uncertain_volume_create(self) -> None:
        with self.temporary_path() as directory:
            case_uncertain_volume_create_is_never_reposted_and_exact_match_is_adopted(
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

    def test_unknown_create_is_not_absence(self) -> None:
        with self.temporary_path() as directory:
            case_unknown_create_with_no_exact_name_match_never_reads_as_absent(Path(directory))

    def test_runtime_ack(self) -> None:
        with self.temporary_path() as directory:
            case_runtime_ack_binds_pid1_deadline_worker_probe_and_receipt_digest(
                self, Path(directory)
            )


if __name__ == "__main__":
    unittest.main()
