"""The hand route's create: the one mutation input, and what the answer must report."""

from __future__ import annotations

import json

import pytest

from . import create_pod
from .provider_runpod import ACCOUNT_KEY_ENVIRONMENT, POD_CREATE_MUTATION, HttpResponse


class ScriptedTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> HttpResponse:
        self.calls.append((method, path, body))
        return self.responses.pop(0)


START = "bash -c 'd=${POD_GUARD_DIR:-/workspace/private/.pod_guard}; exec sleep infinity'"
ARGV = [
    "--name", "verbatus-bakeoff-w",
    "--gpu", "NVIDIA A40",
    "--image", "runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404",
    "--container-disk-gb", "100",
    "--disk-gb", "150",
    "--min-vcpu", "16",
    "--global-volume", "global-1",
    "--global-mount", "/workspace/global",
    "--start-command", START,
]  # fmt: skip


def created(**overrides: object) -> HttpResponse:
    pod: dict[str, object] = {
        "id": "abc123",
        "name": "verbatus-bakeoff-w",
        "desiredStatus": "RUNNING",
        "costPerHr": 0.59,
        "imageName": "runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404",
        "volumeInGb": 150,
        "volumeMountPath": "/workspace/private",
        "vcpuCount": 16,
        "machine": {"gpuTypeId": "NVIDIA A40"},
        "objectStores": [{"objectStoreId": "global-1", "mountPath": "/workspace/global"}],
    }
    pod.update(overrides)
    return HttpResponse(200, json.dumps({"data": {"podFindAndDeployOnDemand": pod}}).encode())


def test_the_input_names_the_disk_the_global_volume_and_the_guard(capsys) -> None:
    assert create_pod.main([*ARGV, "--dry-run"]) == 0

    sent = json.loads(capsys.readouterr().out)
    assert sent["objectMounts"] == [{"objectStoreId": "global-1", "mountPath": "/workspace/global"}]
    assert sent["volumeInGb"] == 150 and sent["volumeMountPath"] == "/workspace/private"
    assert "networkVolumeId" not in sent and "dataCenterId" not in sent
    assert sent["minVcpuCount"] == 16 and sent["ports"] == "22/tcp" and sent["startSsh"] is True
    assert sent["dockerArgs"] == START and sent["cloudType"] == "SECURE"
    assert sent["gpuTypeId"] == "NVIDIA A40" and sent["gpuCount"] == 1


def test_a_network_volume_replaces_the_disk_and_the_global_volume_is_optional(capsys) -> None:
    argv = [a for a in ARGV if a not in {"--disk-gb", "150", "--global-mount", "/workspace/global"}]
    argv = [a for a in argv if a not in {"--global-volume", "global-1"}]
    argv += ["--network-volume", "vol-1", "--datacenter", "EU-RO-1", "--dry-run"]

    assert create_pod.main(argv) == 0

    sent = json.loads(capsys.readouterr().out)
    assert sent["networkVolumeId"] == "vol-1" and sent["dataCenterId"] == "EU-RO-1"
    assert "volumeInGb" not in sent and "objectMounts" not in sent


@pytest.mark.parametrize(
    "change",
    [
        {"--global-mount": None},
        {"--global-mount": "/workspace/private/results"},
        {"--start-command": "   "},
        {"--disk-gb": "0"},
    ],
)
def test_a_half_named_or_unsafe_request_creates_nothing(
    change: dict[str, str | None], capsys
) -> None:
    argv = list(ARGV)
    for flag, value in change.items():
        at = argv.index(flag)
        if value is None:
            del argv[at : at + 2]
        else:
            argv[at + 1] = value
    transport = ScriptedTransport([])

    assert create_pod.main(argv, transport=transport) == 2

    assert transport.calls == [] and "nothing was created" in capsys.readouterr().err


def test_without_the_key_nothing_is_sent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(create_pod, "graphql_transport_from_environment", lambda: None)

    assert create_pod.main(ARGV) == 2

    assert ACCOUNT_KEY_ENVIRONMENT in capsys.readouterr().err


def test_a_pod_reporting_every_mount_is_printed_and_exit_0(capsys) -> None:
    transport = ScriptedTransport([created()])

    assert create_pod.main(ARGV, transport=transport) == 0

    ((method, path, body),) = transport.calls
    assert (method, path) == ("POST", "/graphql")
    assert body is not None and body["query"] == POD_CREATE_MUTATION
    assert json.loads(capsys.readouterr().out)["id"] == "abc123"


def test_a_pod_without_the_global_volume_is_named_for_deletion(capsys) -> None:
    transport = ScriptedTransport([created(objectStores=[])])

    assert create_pod.main(ARGV, transport=transport) == 3

    err = capsys.readouterr().err
    assert "abc123" in err and "delete it" in err


def test_a_graphql_error_means_no_pod(capsys) -> None:
    answer = {"errors": [{"message": "There are no longer any instances available"}]}
    transport = ScriptedTransport([HttpResponse(200, json.dumps(answer).encode())])

    assert create_pod.main(ARGV, transport=transport) == 2

    assert "no longer any instances" in capsys.readouterr().err


def test_cuda_versions_become_the_allowed_list(capsys) -> None:
    assert create_pod.main([*ARGV, "--cuda", "13.0", "--cuda", "12.9,12.8", "--dry-run"]) == 0

    assert json.loads(capsys.readouterr().out)["allowedCudaVersions"] == ["13.0", "12.9", "12.8"]
    assert create_pod.main([*ARGV, "--cuda", " ", "--dry-run"]) == 2


def test_the_key_falls_back_to_runpodctl_s_config_and_is_never_printed(
    tmp_path, monkeypatch, capsys
) -> None:
    from .provider_runpod import account_key

    monkeypatch.delenv(ACCOUNT_KEY_ENVIRONMENT, raising=False)
    config = tmp_path / "config.toml"
    config.write_text('[default]\napikey = "not-a-key-just-a-test-value"\n')

    assert account_key({}, config) == "not-a-key-just-a-test-value"
    assert account_key({ACCOUNT_KEY_ENVIRONMENT: "fromshell"}, config) == "fromshell"
    assert account_key({}, tmp_path / "missing.toml") is None
    assert "not-a-key" not in capsys.readouterr().out
