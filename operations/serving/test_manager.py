"""Fake-first acceptance drills for the vLLM serving lifecycle.

Every fake has only a loopback-process shape.  No test imports vLLM, starts a
server, fetches a model, or contacts a provider.  The paired red cases are
intentional: an apparently successful service that has not answered, advertised
the wrong ID, or ignored an adapter is more expensive than a visible refusal.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping

import pytest

from common.chairs.config import load_models_toml
from common.chairs.errors import ReceiptRefusal, ServingRecipeRefusal
from common.chairs.models import ChairIdentity, ModelsConfig, ServingDetails, VerifiedSnapshot
from common.chairs.receipts import build_receipt
from common.stage import run_config_bindings
from operations.pod.preflight import (
    GpuProfile,
    PlacementRecipe,
    PlacementTable,
    PlacementTier,
    PreflightRunner,
    SmokeResult,
    UtilizationSample,
    load_placement_table,
)

from .assembly import assemble_serving_preflight_callback, assemble_serving_smoke_reader
from .config import (
    FixtureProfile,
    ServingConfigInputs,
    ServingRecipes,
    load_serving_recipes,
    model_and_tokenizer_pins,
    parse_serving_recipes,
    verify_recipes_cover_chairs,
)
from .errors import (
    AdapterActivityError,
    EndpointOccupiedError,
    ReadinessError,
    ReceiptPublicationError,
    ResidencyError,
    ServiceStopError,
    ServingConfigurationError,
)
from .http import (
    EndpointUnavailable,
    HttpResponse,
    parse_model_ids,
    parse_openai_answer,
    require_exact_model_id,
)
from .manager import (
    AdapterCalibration,
    ReceiptPublication,
    ServingManager,
    StageContextReceiptPublisher,
)
from .preflight import ServingSmokeReader
from .process import SubprocessLauncher
from .residency import FileResidencyLease

START = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
TIER = "generic-48gb"
REVISION = "a" * 40
MANIFEST = "b" * 64


@dataclass
class Clock:
    seconds: float = 0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


class FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        log_tail: str = "",
        exits_immediately: int | None = None,
        ignore_terminate: bool = False,
        ignore_kill: bool = False,
    ) -> None:
        self.pid = pid
        self.exit_code = exits_immediately
        self.log_tail = log_tail
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.ignore_terminate = ignore_terminate
        self.ignore_kill = ignore_kill

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.ignore_terminate:
            self.exit_code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        if not self.ignore_kill:
            self.exit_code = -9

    def wait(self, timeout_seconds: float) -> int:
        del timeout_seconds
        self.wait_calls += 1
        if self.exit_code is None:
            raise TimeoutError("fake child is still live")
        return self.exit_code

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        return self.log_tail[-maximum_bytes:]


class FakeHttp:
    def __init__(
        self,
        *,
        model_ids: tuple[str, ...],
        outputs: Mapping[str, str] | None = None,
        health_status: int = 200,
        bad_response: bool = False,
        response_model: str | None = None,
        occupied_before_launch: bool = False,
        ambiguous_before_launch: bool = False,
        sticky_after_stop: bool = False,
        ambiguous_after_stop: bool = False,
    ) -> None:
        self.model_ids = model_ids
        self.outputs = dict(outputs or {})
        self.health_status = health_status
        self.bad_response = bad_response
        self.response_model = response_model
        self.occupied_before_launch = occupied_before_launch
        self.ambiguous_before_launch = ambiguous_before_launch
        self.sticky_after_stop = sticky_after_stop
        self.ambiguous_after_stop = ambiguous_after_stop
        self.process: FakeProcess | None = None
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    @property
    def inference_calls(self) -> int:
        return sum(method == "POST" for method, _, _ in self.calls)

    def bind(self, process: FakeProcess) -> None:
        self.process = process

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        del timeout_seconds
        decoded = json.loads(body) if body is not None else None
        self.calls.append((method, url, decoded))
        if not self._available():
            if self.ambiguous_before_launch or (
                self.ambiguous_after_stop
                and self.process is not None
                and self.process.poll() is not None
            ):
                raise EndpointUnavailable(
                    f"fake loopback outcome at {url} is ambiguous", definitively_absent=False
                )
            raise EndpointUnavailable(
                f"fake loopback is unavailable at {url}", definitively_absent=True
            )
        if url.endswith("/health"):
            return HttpResponse(self.health_status, b'{"status":"ok"}')
        if url.endswith("/models"):
            return HttpResponse(
                200,
                json.dumps({"data": [{"id": item} for item in self.model_ids]}).encode(),
            )
        if method != "POST" or decoded is None:
            return HttpResponse(404, b"{}")
        model_id = decoded["model"]
        assert isinstance(model_id, str)
        if self.bad_response:
            return HttpResponse(200, json.dumps({"model": model_id, "choices": []}).encode())
        response_model = self.response_model or model_id
        if url.endswith("/chat/completions"):
            choice: dict[str, object] = {
                "message": {"content": self.outputs.get(model_id, f"answer:{model_id}")}
            }
        else:
            choice = {"text": self.outputs.get(model_id, f"answer:{model_id}")}
        return HttpResponse(
            200, json.dumps({"model": response_model, "choices": [choice]}).encode()
        )

    def _available(self) -> bool:
        return self.occupied_before_launch or (
            self.process is not None and (self.process.poll() is None or self.sticky_after_stop)
        )


class FakeLauncher:
    def __init__(
        self,
        http: FakeHttp,
        *,
        log_tail: str = "",
        exits_immediately: int | None = None,
        ignore_terminate: bool = False,
        ignore_kill: bool = False,
    ) -> None:
        self.http = http
        self.log_tail = log_tail
        self.exits_immediately = exits_immediately
        self.ignore_terminate = ignore_terminate
        self.ignore_kill = ignore_kill
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.inherited_fds: list[tuple[int, ...]] = []
        self.processes: list[FakeProcess] = []

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> FakeProcess:
        self.calls.append((argv, log_path))
        self.inherited_fds.append(inheritable_fds)
        process = FakeProcess(
            9000 + len(self.processes),
            log_tail=self.log_tail,
            exits_immediately=self.exits_immediately,
            ignore_terminate=self.ignore_terminate,
            ignore_kill=self.ignore_kill,
        )
        self.processes.append(process)
        self.http.bind(process)
        return process


class FakePackages:
    def __init__(self, versions: Mapping[str, str]) -> None:
        self.versions = dict(versions)
        self.calls: list[str] = []

    def version(self, package: str) -> str:
        self.calls.append(package)
        return self.versions[package]


class FakeRegistry:
    def __init__(self, identities: Mapping[str, ChairIdentity], tmp_path: Path) -> None:
        self.identities = dict(identities)
        self.config = ModelsConfig(witness_floor=0, chairs=self.identities)
        self.snapshots = {
            role: VerifiedSnapshot(identity, tmp_path / role, identity.digest_manifest)
            for role, identity in identities.items()
        }
        self.ensure_calls: list[str] = []
        self.resolve_calls: list[str] = []
        self.refusals: list[tuple[str, str]] = []
        self.receipts: list[tuple[str, ServingDetails]] = []

    def resolve(self, role: str) -> ChairIdentity:
        self.resolve_calls.append(role)
        return self.identities[role]

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        assert self.identities[identity.role] == identity
        self.ensure_calls.append(identity.role)
        return self.snapshots[identity.role]

    def receipt(self, identity: ChairIdentity, details: ServingDetails):
        self.receipts.append((identity.role, details))
        return build_receipt(identity, details)

    def refuse_recipe_start(self, identity: ChairIdentity, difference: str) -> None:
        self.refusals.append((identity.role, difference))
        raise ServingRecipeRefusal(identity.role, difference)


class FakePublisher:
    def __init__(
        self,
        http: FakeHttp,
        *,
        fail: bool = False,
        receipt_only_reference: bool = False,
        context: object | None = None,
    ) -> None:
        self.http = http
        self.fail = fail
        self.receipt_only_reference = receipt_only_reference
        self.context = context
        self.calls: list[tuple[object, Mapping[str, object]]] = []

    def publish(self, receipt, launch_audit):  # type: ignore[no-untyped-def]
        assert self.http.inference_calls >= 1, "a receipt must follow an actual inference response"
        self.calls.append((receipt, launch_audit))
        if self.fail:
            raise RuntimeError("injected receipt storage failure")
        if self.receipt_only_reference:
            return {"relative_path": "receipts/sha256/" + "c" * 64 + ".json", "sha256": "c" * 64}
        return ReceiptPublication(
            {"relative_path": "receipts/sha256/" + "c" * 64 + ".json", "sha256": "c" * 64},
            {"relative_path": "stages/preflight/blobs/sha256/test", "sha256": "d" * 64},
            {"relative_path": "stages/preflight/blobs/sha256/evidence", "sha256": "e" * 64},
        )


def identity(
    role: str,
    recipe: str,
    *,
    adapter_of: str | None = None,
    revision: str = REVISION,
) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="huggingface",
        repo=f"example/{role}",
        path=None,
        revision=revision,
        digest_manifest=MANIFEST,
        manifest=f"manifests/{role}.json",
        adapter_of=adapter_of,
        serving_recipe=recipe,
        license_note="test identity only",
    )


def profile_row(
    *,
    recipe: str,
    chair: str,
    served_model_id: str,
    port: int,
    tier: str = TIER,
    tower_connector: bool = False,
) -> dict[str, object]:
    return {
        "kind": "vllm",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "host": "127.0.0.1",
        "port": port,
        "served_model_id": served_model_id,
        "dtype": "bfloat16",
        "seed": 0,
        "required_packages": {"vllm": "0.test"},
        "max_model_len": 2048,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1024,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "enable_tower_connector_lora": tower_connector,
        "max_lora_rank": 16,
        "generation_config": "vllm",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "readiness_probe": {
            "kind": "chat-completions",
            "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
        },
    }


def recipes(*rows: dict[str, object]):
    return parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": list(rows)})


def measured_gpu(dtype: str = "bfloat16") -> GpuProfile:
    return GpuProfile("fake GPU", "12.4", "550", (8, 0), "48", "100", dtype)


def fixture_image_payload(fixture: Path) -> Mapping[str, object]:
    """A local data-URI request used only by fake golden-page smoke tests."""

    return AdapterCalibration.from_image_fixture(
        fixture=fixture,
        prompt="Read the supplied proof page.",
        mime_type="image/png",
    ).request_payload()


def sealed_config_inputs(root: Path) -> dict[str, str]:
    """The exact configuration projection a real ``open_context`` would supply."""

    return ServingConfigInputs(
        hashlib.sha256((root / "config/serving_recipes.toml").read_bytes()).hexdigest(),
        hashlib.sha256((root / "config/pod_placement.toml").read_bytes()).hexdigest(),
    ).to_record()


@dataclass(frozen=True)
class FakeStageContext:
    """The run-sealed context shape required by dormant pod assembly."""

    serving_config_inputs: Mapping[str, str]
    registry: object


def assembly_context(root: Path, registry: object) -> FakeStageContext:
    return FakeStageContext(sealed_config_inputs(root), registry)


def manager_for(
    tmp_path: Path,
    *,
    identities: Mapping[str, ChairIdentity],
    profiles: tuple[dict[str, object], ...],
    model_ids: tuple[str, ...],
    outputs: Mapping[str, str] | None = None,
    health_status: int = 200,
    bad_response: bool = False,
    response_model: str | None = None,
    occupied_before_launch: bool = False,
    ambiguous_before_launch: bool = False,
    sticky_after_stop: bool = False,
    ambiguous_after_stop: bool = False,
    log_tail: str = "",
    exits_immediately: int | None = None,
    package_version: str = "0.test",
    package_versions: Mapping[str, str] | None = None,
    publisher_fail: bool = False,
    publisher_receipt_only: bool = False,
    ignore_terminate: bool = False,
    ignore_kill: bool = False,
    residency_lease: FileResidencyLease | None = None,
):
    clock = Clock()
    http = FakeHttp(
        model_ids=model_ids,
        outputs=outputs,
        health_status=health_status,
        bad_response=bad_response,
        response_model=response_model,
        occupied_before_launch=occupied_before_launch,
        ambiguous_before_launch=ambiguous_before_launch,
        sticky_after_stop=sticky_after_stop,
        ambiguous_after_stop=ambiguous_after_stop,
    )
    launcher = FakeLauncher(
        http,
        log_tail=log_tail,
        exits_immediately=exits_immediately,
        ignore_terminate=ignore_terminate,
        ignore_kill=ignore_kill,
    )
    registry = FakeRegistry(identities, tmp_path)
    publisher = FakePublisher(
        http, fail=publisher_fail, receipt_only_reference=publisher_receipt_only
    )
    observed_packages = {"vllm": package_version, **dict(package_versions or {})}
    manager = ServingManager(
        registry=registry,
        recipes=recipes(*profiles),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=http,
        receipt_publisher=publisher,
        log_root=tmp_path / "logs",
        package_inspector=FakePackages(observed_packages),
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        residency_lease=residency_lease or FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )
    return manager, clock, http, launcher, registry, publisher


def _value_after(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_start_proves_exact_model_answer_then_publishes_and_stops(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )

    handle = manager.start(chair, TIER)

    argv, log_path = launcher.calls[0]
    assert argv[:5] == (sys.executable, "-m", "vllm", "serve", str(tmp_path / "reader"))
    assert _value_after(argv, "--revision") == REVISION
    assert _value_after(argv, "--tokenizer-revision") == REVISION
    assert _value_after(argv, "--served-model-name") == "reader-api"
    assert "--no-enable-log-requests" in argv
    assert "--no-enable-tower-connector-lora" not in argv
    assert log_path.name.startswith("vllm-reader-")
    assert log_path.suffix == ".log"
    assert len(launcher.inherited_fds) == 1
    assert len(launcher.inherited_fds[0]) == 1
    assert launcher.inherited_fds[0][0] >= 0
    assert any(url.endswith("/health") for _, url, _ in http.calls)
    assert any(url.endswith("/v1/models") for _, url, _ in http.calls)
    assert any(url.endswith("/v1/chat/completions") for _, url, _ in http.calls)
    assert handle.receipt.details.engine == "vllm"
    assert handle.receipt.details.engine_version == "0.test"
    assert handle.receipt.details.endpoint == "http://127.0.0.1:8000/v1"
    assert handle.launch_audit["readiness"]
    assert handle.launch_audit["started_at"] == "2026-08-09T12:00:00Z"
    assert (
        handle.launch_audit["configuration_inputs"]
        == ServingConfigInputs("1" * 64, "2" * 64).to_record()
    )
    assert handle.launch_audit["readiness"]["ready_at"] == "2026-08-09T12:00:00Z"  # type: ignore[index]
    assert handle.audit_reference["sha256"] == "d" * 64
    assert handle.evidence_reference["sha256"] == "e" * 64
    assert handle.launch_audit["command"] == {
        "argv_sha256": handle.launch_audit["command"]["argv_sha256"],  # type: ignore[index]
        "model_revision": REVISION,
        "tokenizer_revision": REVISION,
        "revision_kind": "git-commit",
        "served_model_name": "reader-api",
    }
    assert handle.launch_audit["runtime_packages"] == {  # type: ignore[index]
        "required": {"vllm": "0.test"},
        "observed": {"vllm": "0.test"},
    }
    assert handle.launch_audit["profile"]["request_logging"] is False  # type: ignore[index]
    assert handle.launch_audit["profile"]["readiness_probe"]["seed"] == 0  # type: ignore[index]
    assert len(publisher.calls) == 1
    audit_profile = handle.launch_audit["profile"]
    assert isinstance(audit_profile, Mapping)
    with pytest.raises(TypeError):
        audit_profile["tier"] = "substituted-tier"  # type: ignore[index]
    published_profile = publisher.calls[0][1]["profile"]
    assert isinstance(published_profile, Mapping)
    assert published_profile["tier"] == TIER
    assert registry.refusals == []

    result = handle.request("chat-completions", {"messages": [{"role": "user", "content": "read"}]})
    assert result.model_id == "reader-api"
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1
    with pytest.raises(EndpointUnavailable):
        http.request("GET", handle.endpoint.replace("/v1", "/health"), body=None, timeout_seconds=1)


@pytest.mark.parametrize("switches_on", [False, True])
def test_the_argv_carries_every_typed_profile_flag_and_the_audit_digests_that_argv(
    tmp_path: Path, switches_on: bool
) -> None:
    """The launch audit records the profile; only the argv makes it true.

    Every other test here reads two or three flags out of the rendered command.
    That leaves the module's actual job — turning one typed profile into the
    exact vLLM invocation — resting on the audit's own copy of the profile,
    which is read from the same object the argv was rendered from. Drop
    `--max-model-len` from the renderer and the audit still reports the context
    cap as though it had bound the launch: a claim about something nobody
    measured (GOVERNANCE 10). The three boolean flags are parametrized because
    each has two arms and a swapped pair reads identically in a spot check.
    """

    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["enable_prefix_caching"] = switches_on
    row["enforce_eager"] = switches_on
    row["trust_remote_code"] = switches_on
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
    )

    handle = manager.start(chair, TIER)

    snapshot_root = str(tmp_path / "reader")
    assert launcher.calls[0][0] == (
        sys.executable,
        "-m",
        "vllm",
        "serve",
        snapshot_root,
        "--tokenizer",
        snapshot_root,
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--revision",
        REVISION,
        "--tokenizer-revision",
        REVISION,
        "--served-model-name",
        "reader-api",
        "--dtype",
        "bfloat16",
        "--seed",
        "0",
        "--max-model-len",
        "2048",
        "--max-num-seqs",
        "1",
        "--max-num-batched-tokens",
        "256",
        "--gpu-memory-utilization",
        "0.85",
        "--mm-processor-kwargs",
        '{"max_pixels":1024,"min_pixels":1}',
        "--generation-config",
        "vllm",
        "--no-enable-log-requests",
        "--enable-prefix-caching" if switches_on else "--no-enable-prefix-caching",
        "--enforce-eager" if switches_on else "--no-enforce-eager",
        "--trust-remote-code" if switches_on else "--no-trust-remote-code",
    )

    # Recomputed from what the launcher actually received, not from the audit:
    # the audit's digest is only evidence if it names that exact command.
    expected_argv_digest = hashlib.sha256(
        json.dumps(list(launcher.calls[0][0]), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert handle.launch_audit["command"]["argv_sha256"] == expected_argv_digest  # type: ignore[index]

    # Same for the readiness probe: the audit carries a digest instead of the
    # request text, so the digest is the only thing a reader can check it by.
    expected_probe_digest = hashlib.sha256(
        json.dumps(
            {"messages": [{"role": "user", "content": "READY"}], "max_tokens": 4},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert (
        handle.launch_audit["profile"]["readiness_probe"]["request_payload_sha256"]  # type: ignore[index]
        == expected_probe_digest
    )
    handle.stop()


def test_readiness_refuses_http_200_when_exact_model_id_is_missing(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, clock, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api-shadow",),
    )

    with pytest.raises(ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_MODEL_ID_MISSING"):
        manager.start(chair, TIER)

    assert clock.seconds == 3
    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []
    assert registry.refusals[0][0] == "reader"


def test_readiness_refuses_exact_id_that_never_completes_a_valid_answer(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        bad_response=True,
    )

    with pytest.raises(
        ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_PROBE_RESPONSE_INVALID"
    ):
        manager.start(chair, TIER)

    assert http.inference_calls > 0
    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []


def test_a_health_endpoint_answering_non_200_never_becomes_ready(tmp_path: Path) -> None:
    """`/health` is the first of the three readiness conditions and had no test.

    `FakeHttp` has carried a `health_status` knob since the lane merged and no
    test ever moved it off 200, so `VLLM_HEALTH_UNAVAILABLE` was unreachable
    from the suite: an endpoint that answers `/health` with a 503 is exactly the
    routing stub the package's README says cannot publish a receipt.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        health_status=503,
    )

    with pytest.raises(
        ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_HEALTH_UNAVAILABLE.*503"
    ):
        manager.start(chair, TIER)

    assert publisher.calls == []
    assert launcher.processes[0].terminate_calls == 1


def test_an_endpoint_answering_as_a_different_model_never_becomes_ready(tmp_path: Path) -> None:
    """The response's own `model` field is checked, not just the advertised list.

    `/v1/models` can advertise the exact id while the process actually answering
    is a different one; `parse_openai_answer` has a direct parser test for that
    mismatch, but nothing drove it through the manager, so the `FakeHttp`
    `response_model` knob was dead too. A receipt naming a model that did not
    produce the answer is the provenance defect GOVERNANCE 6 exists for.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        response_model="reader-api-shadow",
    )

    with pytest.raises(
        ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_PROBE_MODEL_MISMATCH"
    ):
        manager.start(chair, TIER)

    assert publisher.calls == []
    assert launcher.processes[0].terminate_calls == 1


@pytest.mark.parametrize(
    ("signature", "expected_code"),
    [
        ("CUDA out of memory", "CUDA out of memory"),
        ("EngineDeadError", "EngineDeadError"),
        ("Traceback", "VLLM_ERROR"),
        # The two the old pipeline's own launch scripts grepped for, and the
        # two vLLM prints when it rejects an adapter loudly rather than
        # ignoring one silently (Tyrel's ruling 1).
        (
            "ValueError: Qwen3VLForConditionalGeneration does not support LoRA yet.",
            "LORA_UNSUPPORTED",
        ),
        ("Unknown model: example/reader", "UNKNOWN_MODEL"),
    ],
)
def test_named_fatal_log_signatures_refuse_and_clean_up(
    tmp_path: Path, signature: str, expected_code: str
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        log_tail=signature,
    )

    with pytest.raises(ServingRecipeRefusal, match=expected_code):
        manager.start(chair, TIER)

    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []
    assert expected_code in registry.refusals[0][1]


def test_bare_runtimeerror_or_valueerror_in_the_log_does_not_abort_a_start_that_would_succeed(
    tmp_path: Path,
) -> None:
    """The exact false positive the old pipeline's grep produced, deliberately not carried.

    ``_fatal_log_signature`` docstring: a missed signature costs a bounded
    watchdog wait; a false one costs a relaunch, and on the real path that is
    GPU-hours. A launch log naming ``RuntimeError``/``ValueError`` without one
    of the five named fatal substrings must reach a normal successful start.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, _, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        log_tail=(
            "INFO: warming up\n"
            "RuntimeError: a transient benign message unrelated to any fatal condition\n"
            "ValueError: also benign, also not one of the named signatures\n"
        ),
    )

    handle = manager.start(chair, TIER)

    assert registry.refusals == []
    assert len(publisher.calls) == 1
    handle.stop()


def test_process_exit_before_readiness_has_its_own_named_refusal(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        exits_immediately=23,
    )

    with pytest.raises(ServingRecipeRefusal, match="VLLM_PROCESS_EXITED.*23"):
        manager.start(chair, TIER)

    assert launcher.processes[0].terminate_calls == 0
    assert publisher.calls == []


def test_unknown_endpoint_and_runtime_pin_refuse_before_process_start(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    profile = profile_row(
        recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
    )
    occupied, _, _, occupied_launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(profile,),
        model_ids=("reader-api",),
        occupied_before_launch=True,
    )
    with pytest.raises(ServingRecipeRefusal, match=EndpointOccupiedError.code):
        occupied.start(chair, TIER)
    assert occupied_launcher.calls == []

    mismatch, _, _, mismatch_launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(profile,),
        model_ids=("reader-api",),
        package_version="different",
    )
    with pytest.raises(ServingRecipeRefusal, match="VLLM_RUNTIME_PIN_MISMATCH"):
        mismatch.start(chair, TIER)
    assert mismatch_launcher.calls == []


def test_every_declared_model_stack_package_is_exactly_asserted(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    profile = profile_row(
        recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
    )
    profile["required_packages"] = {"vllm": "0.test", "transformers": "9.test"}
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(profile,),
        model_ids=("reader-api",),
        package_versions={"transformers": "wrong"},
    )

    with pytest.raises(ServingRecipeRefusal, match="transformers='wrong', expected '9.test'"):
        manager.start(chair, TIER)

    assert launcher.calls == []


def test_receipt_publication_failure_never_leaves_a_ready_process_live(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        publisher_fail=True,
    )

    with pytest.raises(ServingRecipeRefusal, match="SERVING_RECEIPT_PUBLICATION_FAILED"):
        manager.start(chair, TIER)

    assert len(registry.receipts) == 1
    assert len(publisher.calls) == 1
    assert launcher.processes[0].terminate_calls == 1


def test_receipt_publication_requires_a_durable_launch_audit_reference(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        publisher_receipt_only=True,
    )

    with pytest.raises(ServingRecipeRefusal, match="durable launch-audit, and combined evidence"):
        manager.start(chair, TIER)
    assert len(publisher.calls) == 1
    assert launcher.processes[0].terminate_calls == 1


def test_adapter_must_change_deterministic_calibration_and_names_only_its_base(
    tmp_path: Path,
) -> None:
    base = identity("base", "base-v1")
    adapter = identity("adapter", "adapter-v1", adapter_of="base")
    unrelated = identity("unrelated", "unrelated-v1")
    profiles = (
        profile_row(recipe="base-v1", chair="base", served_model_id="base-api", port=8000),
        profile_row(
            recipe="adapter-v1",
            chair="adapter",
            served_model_id="adapter-api",
            port=8100,
            tower_connector=True,
        ),
        profile_row(
            recipe="unrelated-v1", chair="unrelated", served_model_id="other-api", port=8200
        ),
    )
    calibration_fixture = tmp_path / "adapter-calibration.png"
    calibration_fixture.write_bytes(b"offline calibration image bytes")
    calibration = AdapterCalibration.from_image_fixture(
        fixture=calibration_fixture,
        prompt="Describe the marked fixture.",
        mime_type="image/png",
    )

    unproven, _, _, unproven_launcher, unproven_registry, unproven_publisher = manager_for(
        tmp_path,
        identities={base.role: base, adapter.role: adapter, unrelated.role: unrelated},
        profiles=profiles,
        model_ids=("base-api", "adapter-api"),
        outputs={"base-api": "same", "adapter-api": "same"},
    )
    with pytest.raises(ServingRecipeRefusal, match="ADAPTER_ACTIVITY_UNPROVEN"):
        unproven.start(adapter, TIER, adapter_calibration=calibration)
    assert unproven_registry.ensure_calls == ["adapter", "base"]
    assert "unrelated" not in unproven_registry.resolve_calls
    assert unproven_publisher.calls == []
    assert unproven_launcher.processes[0].terminate_calls == 1

    proven, _, _, proven_launcher, proven_registry, proven_publisher = manager_for(
        tmp_path,
        identities={base.role: base, adapter.role: adapter, unrelated.role: unrelated},
        profiles=profiles,
        model_ids=("base-api", "adapter-api"),
        outputs={"base-api": "base answer", "adapter-api": "adapter answer"},
    )
    handle = proven.start(adapter, TIER, adapter_calibration=calibration)
    argv = proven_launcher.calls[0][0]
    lora = json.loads(_value_after(argv, "--lora-modules"))
    assert lora == {
        "base_model_name": "example/base",
        "name": "adapter-api",
        "path": str(tmp_path / "adapter"),
    }
    assert _value_after(argv, "--max-lora-rank") == "16"
    assert "--enable-tower-connector-lora" in argv
    assert handle.receipt.details.adapter_identity == base
    assert handle.launch_audit["adapter_activation"]["different"] is True  # type: ignore[index]
    assert proven_registry.ensure_calls == ["adapter", "base"]
    assert len(proven_publisher.calls) == 1
    handle.stop()


def test_image_calibration_builder_binds_data_uri_bytes_to_its_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "calibration.png"
    fixture.write_bytes(b"synthetic image fixture")
    calibration = AdapterCalibration.from_image_fixture(
        fixture=fixture,
        prompt="Read the fixture.",
        mime_type="image/png",
    )
    url = calibration.payload["messages"][0]["content"][1]["image_url"]["url"]  # type: ignore[index]
    assert isinstance(url, str) and url.startswith("data:image/png;base64,")
    assert calibration.fixture_sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()

    with pytest.raises(ServingConfigurationError, match="do not match fixture_sha256"):
        AdapterCalibration(
            kind=calibration.kind,
            payload=calibration.payload,
            fixture_sha256="f" * 64,
            requires_image=True,
        )


def test_image_calibration_seals_nested_payload_against_later_mutation(tmp_path: Path) -> None:
    fixture = tmp_path / "calibration.png"
    fixture.write_bytes(b"synthetic image fixture")
    calibration = AdapterCalibration.from_image_fixture(
        fixture=fixture,
        prompt="Read the fixture.",
        mime_type="image/png",
    )
    nested_url = calibration.payload["messages"][0]["content"][1]["image_url"]  # type: ignore[index]
    nested_url["url"] = "https://example.invalid/replaced.png"  # type: ignore[index]

    sealed_url = calibration.request_payload()["messages"][0]["content"][1]["image_url"][  # type: ignore[index]
        "url"
    ]
    assert isinstance(sealed_url, str) and sealed_url.startswith("data:image/png;base64,")


def test_tower_connector_adapter_refuses_a_text_only_calibration(tmp_path: Path) -> None:
    base = identity("base", "base-v1")
    adapter = identity("adapter", "adapter-v1", adapter_of="base")
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={base.role: base, adapter.role: adapter},
        profiles=(
            profile_row(recipe="base-v1", chair="base", served_model_id="base-api", port=8000),
            profile_row(
                recipe="adapter-v1",
                chair="adapter",
                served_model_id="adapter-api",
                port=8100,
                tower_connector=True,
            ),
        ),
        model_ids=("base-api", "adapter-api"),
    )
    calibration = AdapterCalibration(
        kind="chat-completions",
        payload={"messages": [{"role": "user", "content": "text is insufficient"}]},
        fixture_sha256="c" * 64,
    )

    with pytest.raises(ServingRecipeRefusal, match="image-bearing adapter calibration"):
        manager.start(adapter, TIER, adapter_calibration=calibration)
    assert len(launcher.calls) == 1
    assert publisher.calls == []


@pytest.mark.parametrize(
    "image_url",
    [
        "",
        "https://example.invalid/calibration.png",
        "file:///tmp/calibration.png",
        "data:image/png;base64,!",
    ],
)
def test_vision_adapter_calibration_rejects_empty_remote_file_and_malformed_images(
    image_url: str,
) -> None:
    with pytest.raises(ServingConfigurationError, match="image|URL|base64"):
        AdapterCalibration(
            kind="chat-completions",
            payload={
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": image_url}}],
                    }
                ]
            },
            fixture_sha256="c" * 64,
            requires_image=True,
        )


def test_only_one_chair_can_be_resident_and_the_next_starts_after_stop(tmp_path: Path) -> None:
    first = identity("first", "first-v1")
    second = identity("second", "second-v1")
    manager, _, _, launcher, registry, _ = manager_for(
        tmp_path,
        identities={first.role: first, second.role: second},
        profiles=(
            profile_row(recipe="first-v1", chair="first", served_model_id="first-api", port=8000),
            profile_row(
                recipe="second-v1", chair="second", served_model_id="second-api", port=8100
            ),
        ),
        model_ids=("first-api", "second-api"),
    )
    handle = manager.start(first, TIER)

    with pytest.raises(ServingRecipeRefusal, match="still resident"):
        manager.start(second, TIER)
    assert len(launcher.calls) == 1
    assert registry.refusals[-1][0] == "second"

    handle.stop()
    next_handle = manager.start(second, TIER)
    assert len(launcher.calls) == 2
    next_handle.stop()


def test_two_manager_instances_share_the_pod_single_resident_lease(tmp_path: Path) -> None:
    first = identity("first", "first-v1")
    second = identity("second", "second-v1")
    profiles = (
        profile_row(recipe="first-v1", chair="first", served_model_id="first-api", port=8000),
        profile_row(recipe="second-v1", chair="second", served_model_id="second-api", port=8100),
    )
    first_manager, _, _, _, _, _ = manager_for(
        tmp_path,
        identities={first.role: first, second.role: second},
        profiles=profiles,
        model_ids=("first-api", "second-api"),
    )
    second_manager, _, _, second_launcher, second_registry, _ = manager_for(
        tmp_path,
        identities={first.role: first, second.role: second},
        profiles=profiles,
        model_ids=("first-api", "second-api"),
    )

    handle = first_manager.start(first, TIER)
    with pytest.raises(ServingRecipeRefusal, match=ResidencyError.code):
        second_manager.start(second, TIER)
    assert second_launcher.calls == []
    assert second_registry.refusals[-1][0] == "second"

    handle.stop()
    next_handle = second_manager.start(second, TIER)
    next_handle.stop()


def test_failed_cleanup_surfaces_stop_error_and_keeps_the_residency_lease(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        publisher_fail=True,
        ignore_terminate=True,
        ignore_kill=True,
    )

    with pytest.raises(ServingRecipeRefusal, match=ServiceStopError.code):
        manager.start(chair, TIER)

    process = launcher.processes[0]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert ServiceStopError.code in registry.refusals[-1][1]
    # Both facts, in one refusal. Reporting only the stop failure would tell an
    # operator the child would not go away and never mention why the launch
    # failed; reporting only the launch failure would hide a child that may
    # still hold the card.
    assert ReceiptPublicationError.code in registry.refusals[-1][1]
    assert "injected receipt storage failure" in registry.refusals[-1][1]
    assert "lease is retained" in registry.refusals[-1][1]

    # A second start cannot silently run alongside the unverified owned child.
    with pytest.raises(ServingRecipeRefusal, match="shutdown is not verified"):
        manager.start(chair, TIER)
    assert len(launcher.calls) == 1

    # Recovery uses the same process handle after the operator's concrete
    # condition changes; it does not PID-search or release the lease blindly.
    process.ignore_kill = False
    manager.recover_failed_start()
    publisher.fail = False
    launcher.ignore_terminate = False
    launcher.ignore_kill = False
    handle = manager.start(chair, TIER)
    handle.stop()
    assert http.inference_calls >= 2


def test_registry_refusal_and_failed_cleanup_are_reported_together(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        ignore_terminate=True,
        ignore_kill=True,
    )

    def refuse_receipt(identity: ChairIdentity, details: ServingDetails):
        del details
        raise ReceiptRefusal(identity.role, "injected identity-bearing receipt refusal")

    registry.receipt = refuse_receipt  # type: ignore[method-assign]

    with pytest.raises(ServingRecipeRefusal) as caught:
        manager.start(chair, TIER)

    detail = str(caught.value)
    assert ReceiptRefusal.code in detail
    assert "injected identity-bearing receipt refusal" in detail
    assert ServiceStopError.code in detail
    assert "lease is retained" in detail
    assert launcher.processes[0].kill_calls == 1


def test_interrupt_during_start_stops_the_child_and_preserves_the_interrupt(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )

    def interrupt(*unused):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt("injected operator interrupt")

    publisher.publish = interrupt  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt, match="operator interrupt"):
        manager.start(chair, TIER)

    process = launcher.processes[0]
    assert process.terminate_calls == 1
    assert process.poll() == 0


def test_stop_refuses_to_release_residency_while_its_endpoint_still_answers(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        sticky_after_stop=True,
    )
    handle = manager.start(chair, TIER)

    with pytest.raises(ServiceStopError, match="still answered"):
        handle.stop()
    assert launcher.processes[0].terminate_calls == 1
    with pytest.raises(ServingRecipeRefusal, match="shutdown is not verified"):
        manager.start(chair, TIER)

    http.sticky_after_stop = False
    handle.stop()


def test_stop_retains_the_single_resident_lease_when_endpoint_failure_is_ambiguous(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, clock, http, launcher, registry, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    handle = manager.start(chair, TIER)
    http.ambiguous_after_stop = True

    with pytest.raises(ServiceStopError, match="absence was unproven"):
        handle.stop()
    assert clock.seconds >= manager.shutdown_timeout_seconds
    assert launcher.processes[0].terminate_calls == 1
    with pytest.raises(ServingRecipeRefusal, match="shutdown is not verified"):
        manager.start(chair, TIER)
    assert registry.refusals[-1][0] == "reader"

    http.ambiguous_after_stop = False
    handle.stop()


def test_launch_refuses_an_ambiguous_loopback_endpoint_before_start(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        ambiguous_before_launch=True,
    )

    with pytest.raises(ServingRecipeRefusal, match="did not prove absent"):
        manager.start(chair, TIER)
    assert launcher.calls == []


def test_file_residency_lock_survives_controller_fd_close_until_child_fd_closes(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    lease = FileResidencyLease(tmp_path / "pod-gpu.lock")
    held = lease.acquire(chair)
    child_fd = os.dup(held.inheritable_fd())
    original_handle = held._handle  # type: ignore[attr-defined]
    assert original_handle is not None
    original_handle.close()
    held._handle = None  # type: ignore[attr-defined]
    try:
        with pytest.raises(ResidencyError):
            lease.acquire(chair)
    finally:
        os.close(child_fd)
    replacement = lease.acquire(chair)
    replacement.release()


def test_subprocess_launcher_passes_declared_lease_fd_to_the_owned_child(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    held = FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(chair)
    descriptor = held.inheritable_fd()
    process = SubprocessLauncher().launch(
        (
            sys.executable,
            "-c",
            f"import os; os.fstat({descriptor})",
        ),
        tmp_path / "child.log",
        inheritable_fds=(descriptor,),
    )
    assert process.wait(3) == 0
    held.release()


def test_subprocess_launcher_creates_its_log_file_owner_only_from_the_start(
    tmp_path: Path,
) -> None:
    """No window where the log is world/group-readable between create and chmod."""

    process = SubprocessLauncher().launch(
        (sys.executable, "-c", "pass"),
        tmp_path / "child.log",
    )
    assert process.wait(3) == 0
    mode = stat.S_IMODE((tmp_path / "child.log").stat().st_mode)
    assert mode == 0o600, f"expected owner-only 0o600, got {oct(mode)}"


def test_each_manager_log_path_is_fresh_even_with_the_same_log_root(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )

    first = manager._next_log_path(chair)
    second = manager._next_log_path(chair)
    assert first != second
    assert first.name.startswith("vllm-reader-")
    assert second.name.startswith("vllm-reader-")


def test_config_catalogue_is_complete_for_the_fixture_roster_and_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    catalogue = load_serving_recipes(root / "config/serving_recipes.toml")
    models = load_models_toml(root / "config/models.toml")
    placement = load_placement_table(root / "config/pod_placement.toml")
    tiers = tuple(tier.identifier for tier in placement.tiers)

    # The committed coverage check reads the three files that have to agree,
    # rather than a hard-coded tier list that would go stale the moment
    # `config/pod_placement.toml` gained a tier.
    verify_recipes_cover_chairs(models, catalogue, tiers)

    configured = [value for value in models.chairs.values() if isinstance(value, ChairIdentity)]
    for configured_identity in configured:
        for tier in tiers:
            profile = catalogue.for_identity(configured_identity, tier)
            assert profile.recipe == configured_identity.serving_recipe
            assert profile.chair == configured_identity.role
            assert profile.tier == tier
            # The live roster is the offline walking skeleton, so every
            # committed row must be a fixture row. A `vllm` row here would mean
            # a real chair had been configured to serve without the roster
            # decision that is Tyrel's at S8.
            assert isinstance(profile, FixtureProfile)

    raw = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    raw.pop("enable_tower_connector_lora")
    with pytest.raises(ServingConfigurationError, match="enable_tower_connector_lora"):
        recipes(raw)

    bad_rank = profile_row(
        recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
    )
    bad_rank["max_lora_rank"] = 7
    with pytest.raises(ServingConfigurationError, match="supported static LoRA ranks"):
        recipes(bad_rank)

    duplicate_endpoint = profile_row(
        recipe="other-v1", chair="other", served_model_id="other-api", port=8000
    )
    with pytest.raises(ServingConfigurationError, match="endpoint"):
        recipes(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
            duplicate_endpoint,
        )

    # The catalogue also refuses two chairs sharing one API alias on distinct
    # ports -- a client request naming that alias would be ambiguous about
    # which service actually answered it.
    duplicate_alias = profile_row(
        recipe="other-v1", chair="other", served_model_id="reader-api", port=8100
    )
    with pytest.raises(ServingConfigurationError, match="served model id"):
        recipes(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
            duplicate_alias,
        )

    # A local-repository chair has no Git revision by contract, so it has no
    # commit pins to duplicate into `--revision`/`--tokenizer-revision` — and
    # that is an answer, not a refusal. Its pin is the digest manifest the
    # snapshot was verified against.
    local = next(value for value in models.chairs.values() if isinstance(value, ChairIdentity))
    assert local.source == "local-repository"
    assert model_and_tokenizer_pins(local) is None
    assert model_and_tokenizer_pins(identity("reader", "reader-v1")) == (REVISION, REVISION)
    with pytest.raises(ServingConfigurationError, match="without a commit pin"):
        model_and_tokenizer_pins(identity("reader", "reader-v1", revision="not-a-commit"))


def test_for_identity_refuses_both_zero_and_multiple_matches() -> None:
    """No nearest-tier/nearest-chair fallback: lookup is exact, or it refuses.

    This is also a hard-rule-8 "no picker" check: weakening ``len(matches) !=
    1`` to pick ``matches[0]`` on zero or several rows would be exactly the
    ranking/fallback shape that rule forbids. A multiple-match catalogue can't
    be built through the parser (it forbids a duplicate key), so this
    constructs one directly, as the catalogue's own docstring says lookup
    itself — not just the parser — must still refuse it.
    """

    catalogue = recipes(
        profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    )
    chair = identity("reader", "reader-v1")

    with pytest.raises(ServingConfigurationError, match="returned 0 profiles"):
        catalogue.for_identity(chair, "unconfigured-tier")
    with pytest.raises(ServingConfigurationError, match="returned 0 profiles"):
        catalogue.for_identity(identity("other", "reader-v1"), TIER)

    duplicated = ServingRecipes(profiles=(catalogue.profiles[0], catalogue.profiles[0]))
    with pytest.raises(ServingConfigurationError, match="returned 2 profiles"):
        duplicated.for_identity(chair, TIER)


def test_readiness_probe_request_is_sealed_against_nested_mutation() -> None:
    catalogue = recipes(
        profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    )
    profile = catalogue.profiles[0]
    assert not isinstance(profile, FixtureProfile)
    messages = profile.readiness_probe.payload["messages"]
    assert isinstance(messages, list)
    assert isinstance(messages[0], dict)
    messages[0]["content"] = "a substituted readiness claim"

    sealed = profile.readiness_probe.request_payload()
    assert sealed["messages"][0]["content"] == "READY"  # type: ignore[index]


def fixture_row(
    *, recipe: str, chair: str, tier: str = TIER, description: str = "never launched"
) -> dict[str, object]:
    return {
        "kind": "fixture",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "description": description,
    }


def test_a_fixture_profile_is_refused_by_its_own_reason_before_anything_starts(
    tmp_path: Path,
) -> None:
    """The refusal must say "this is a fixture", not "your vLLM is the wrong version".

    Blocking a walking-skeleton chair with an unsatisfiable package pin makes
    the refusal an accident of one field's value and reports the wrong cause;
    an operator would go and install a version to fix it.
    """

    chair = identity("reader", "fake-reader-v0")
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(fixture_row(recipe="fake-reader-v0", chair="reader"),),
        model_ids=("reader-api",),
    )

    with pytest.raises(ServingRecipeRefusal, match="fixture serving profile"):
        manager.start(chair, TIER)

    assert launcher.processes == []
    assert publisher.calls == []
    assert "never launched" in registry.refusals[0][1]
    # No lease was taken and no endpoint was probed, so a later real start is
    # not blocked by a fixture chair's refusal.
    assert not (tmp_path / "logs").exists()


def test_a_fixture_base_refuses_an_adapter_chair_rather_than_serving_it(tmp_path: Path) -> None:
    base = identity("base", "fake-base-v0")
    adapter = identity("adapter", "adapter-v1", adapter_of="base")
    manager, _, _, launcher, registry, _ = manager_for(
        tmp_path,
        identities={base.role: base, adapter.role: adapter},
        profiles=(
            profile_row(
                recipe="adapter-v1", chair="adapter", served_model_id="adapter-api", port=8000
            ),
            fixture_row(recipe="fake-base-v0", chair="base"),
        ),
        model_ids=("adapter-api", "base-api"),
    )

    with pytest.raises(ServingRecipeRefusal, match="fixture serving profile"):
        manager.start(adapter, TIER, adapter_calibration=None)

    assert launcher.processes == []
    # The chair reported unavailable is the one that was asked for, not its base.
    assert registry.refusals[0][0] == adapter.role


def test_recipe_coverage_names_every_chair_and_tier_a_catalogue_misses() -> None:
    """The gap this closes fails on a rented GPU otherwise, not in a test run."""

    root = Path(__file__).resolve().parents[2]
    models = load_models_toml(root / "config/models.toml")
    tiers = ("generic-24gb", "generic-48gb")

    complete = recipes(
        *[
            fixture_row(recipe=value.serving_recipe, chair=role, tier=tier)
            for role, value in models.chairs.items()
            if isinstance(value, ChairIdentity)
            for tier in tiers
        ]
    )
    verify_recipes_cover_chairs(models, complete, tiers)

    one_tier_short = recipes(
        *[
            fixture_row(recipe=value.serving_recipe, chair=role, tier="generic-24gb")
            for role, value in models.chairs.items()
            if isinstance(value, ChairIdentity)
        ]
    )
    with pytest.raises(ServingConfigurationError, match="generic-48gb"):
        verify_recipes_cover_chairs(models, one_tier_short, tiers)

    misspelt = recipes(
        *[
            fixture_row(
                recipe=value.serving_recipe + ("0" if role == "perlector" else ""),
                chair=role,
                tier=tier,
            )
            for role, value in models.chairs.items()
            if isinstance(value, ChairIdentity)
            for tier in tiers
        ]
    )
    with pytest.raises(ServingConfigurationError, match="perlector"):
        verify_recipes_cover_chairs(models, misspelt, tiers)

    # Iterable means iterable: consuming a generator for the first chair must
    # not silently skip coverage for every chair after it.
    verify_recipes_cover_chairs(models, complete, (tier for tier in tiers))

    extra = recipes(
        *[
            fixture_row(recipe=value.serving_recipe, chair=role, tier=tier)
            for role, value in models.chairs.items()
            if isinstance(value, ChairIdentity)
            for tier in tiers
        ],
        fixture_row(recipe="stale-v0", chair="unconfigured", tier="generic-24gb"),
    )
    with pytest.raises(ServingConfigurationError, match="unexpected=.*unconfigured"):
        verify_recipes_cover_chairs(models, extra, tiers)


def test_a_local_repository_chair_serves_its_verified_snapshot_without_revision_flags(
    tmp_path: Path,
) -> None:
    """A locally trained checkpoint is called like any other model.

    ARCHITECTURE requires exactly that of the Perlector chair. `--revision`
    exists to stop vLLM resolving a *mutable* Hub ref; a verified snapshot
    directory has no ref to resolve, and a local-repository identity has no Git
    revision by contract — so naming one would be inventing provenance.
    """

    chair = ChairIdentity(
        role="perlector",
        source="local-repository",
        repo=None,
        path="checkpoints/perlector",
        revision=None,
        digest_manifest=MANIFEST,
        manifest="manifests/perlector.json",
        adapter_of=None,
        serving_recipe="perlector-v1",
        license_note="test identity only",
    )
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="perlector-v1",
                chair="perlector",
                served_model_id="perlector-api",
                port=8000,
            ),
        ),
        model_ids=("perlector-api",),
    )

    handle = manager.start(chair, TIER)

    argv, _ = launcher.calls[0]
    assert "--revision" not in argv
    assert "--tokenizer-revision" not in argv
    assert _value_after(argv, "--served-model-name") == "perlector-api"
    assert handle.launch_audit["command"] == {  # type: ignore[index]
        "argv_sha256": handle.launch_audit["command"]["argv_sha256"],  # type: ignore[index]
        "model_revision": MANIFEST,
        "tokenizer_revision": MANIFEST,
        "revision_kind": "digest-manifest",
        "served_model_name": "perlector-api",
    }
    assert handle.receipt.details.tokenizer_revision == MANIFEST
    handle.stop()


def test_pod_assembly_factory_builds_the_lifecycle_smoke_reader_without_effects(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    http = FakeHttp(model_ids=("reader-api",))
    launcher = FakeLauncher(http)
    root = Path(__file__).resolve().parents[2]
    context = assembly_context(root, registry)
    publisher = FakePublisher(http, context=context)

    reader = assemble_serving_smoke_reader(
        registry=registry,
        stage_context=context,
        receipt_publisher=publisher,
        smoke_call=lambda *args: pytest.fail("assembly must not start a service"),
        log_root=tmp_path / "logs",
        recipes_path=root / "config/serving_recipes.toml",
        placement_path=root / "config/pod_placement.toml",
        launcher=launcher,
        http=http,
        package_inspector=FakePackages({"vllm": "fixture-v0"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )

    assert isinstance(reader, ServingSmokeReader)
    assert reader.manager.recipes.source_path == root / "config/serving_recipes.toml"
    forged_placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive="64",
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.99", 9999, 9999, 99),
    )
    with pytest.raises(ServingConfigurationError, match="run-sealed placement table"):
        reader.read(chair, tmp_path / "unused.png", forged_placement, measured_gpu())
    assert launcher.calls == []
    assert http.calls == []


def test_pod_assembly_builds_bootstrap_preflight_callback_without_running_it(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    http = FakeHttp(model_ids=("reader-api",))
    launcher = FakeLauncher(http)
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture")
    root = Path(__file__).resolve().parents[2]
    context = assembly_context(root, registry)
    publisher = FakePublisher(http, context=context)

    class Cache:
        def verify(self, supplied_identity):  # type: ignore[no-untyped-def]
            raise AssertionError(f"preflight construction must not verify {supplied_identity.role}")

        def refetch_once(self, supplied_identity):  # type: ignore[no-untyped-def]
            raise AssertionError(f"preflight construction must not repair {supplied_identity.role}")

    class Probe:
        def __init__(self) -> None:
            self.calls = 0

        def profile(self, dtype: str) -> GpuProfile:
            self.calls += 1
            raise AssertionError(f"preflight construction must not probe {dtype}")

    probe = Probe()
    callback = assemble_serving_preflight_callback(
        registry=registry,
        stage_context=context,
        cache_verifier=Cache(),
        receipt_publisher=publisher,
        smoke_call=lambda *args: pytest.fail("preflight construction must not start a service"),
        fixture=fixture,
        dtype="bfloat16",
        log_root=tmp_path / "logs",
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        recipes_path=root / "config/serving_recipes.toml",
        placement_path=root / "config/pod_placement.toml",
        launcher=launcher,
        http=http,
        package_inspector=FakePackages({"vllm": "fixture-v0"}),
        gpu_probe=probe,
    )

    assert callable(callback)
    assert probe.calls == 0
    assert launcher.calls == []
    assert http.calls == []


def test_pod_assembly_refuses_recipe_or_placement_path_substitution_before_effects(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    http = FakeHttp(model_ids=("reader-api",))
    launcher = FakeLauncher(http)
    root = Path(__file__).resolve().parents[2]
    context = assembly_context(root, registry)
    publisher = FakePublisher(http, context=context)
    copied_recipes = tmp_path / "recipes.toml"
    copied_placement = tmp_path / "placement.toml"
    copied_recipes.write_bytes(
        (root / "config/serving_recipes.toml").read_bytes() + b"\n# altered\n"
    )
    copied_placement.write_bytes(
        (root / "config/pod_placement.toml").read_bytes() + b"\n# altered\n"
    )

    with pytest.raises(ServingConfigurationError, match="recipes bytes differ"):
        assemble_serving_smoke_reader(
            registry=registry,
            stage_context=context,
            receipt_publisher=publisher,
            smoke_call=lambda *args: pytest.fail("substituted configuration must not start"),
            log_root=tmp_path / "logs",
            recipes_path=copied_recipes,
            placement_path=root / "config/pod_placement.toml",
            launcher=launcher,
            http=http,
            package_inspector=FakePackages({"vllm": "fixture-v0"}),
            residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        )
    with pytest.raises(ServingConfigurationError, match="placement bytes differ"):
        assemble_serving_smoke_reader(
            registry=registry,
            stage_context=context,
            receipt_publisher=publisher,
            smoke_call=lambda *args: pytest.fail("substituted configuration must not start"),
            log_root=tmp_path / "logs",
            recipes_path=root / "config/serving_recipes.toml",
            placement_path=copied_placement,
            launcher=launcher,
            http=http,
            package_inspector=FakePackages({"vllm": "fixture-v0"}),
            residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        )
    assert launcher.calls == []
    assert http.calls == []


def test_pod_assembly_requires_the_same_stage_context_as_receipt_publication(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    http = FakeHttp(model_ids=("reader-api",))
    launcher = FakeLauncher(http)
    root = Path(__file__).resolve().parents[2]
    context = assembly_context(root, registry)
    publisher = FakePublisher(http, context=assembly_context(root, registry))

    with pytest.raises(ServingConfigurationError, match="publisher must belong"):
        assemble_serving_smoke_reader(
            registry=registry,
            stage_context=context,
            receipt_publisher=publisher,
            smoke_call=lambda *args: pytest.fail("mismatched context must not start"),
            log_root=tmp_path / "logs",
            recipes_path=root / "config/serving_recipes.toml",
            placement_path=root / "config/pod_placement.toml",
            launcher=launcher,
            http=http,
            package_inspector=FakePackages({"vllm": "fixture-v0"}),
            residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        )
    assert launcher.calls == []
    assert http.calls == []


def test_pod_assembly_requires_the_stage_contexts_registry(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    other_registry = FakeRegistry({chair.role: chair}, tmp_path / "other")
    http = FakeHttp(model_ids=("reader-api",))
    root = Path(__file__).resolve().parents[2]
    context = assembly_context(root, registry)
    publisher = FakePublisher(http, context=context)

    with pytest.raises(ServingConfigurationError, match="registry must be the registry owned"):
        assemble_serving_smoke_reader(
            registry=other_registry,
            stage_context=context,
            receipt_publisher=publisher,
            smoke_call=lambda *args: pytest.fail("mismatched registry must not start"),
            log_root=tmp_path / "logs",
            recipes_path=root / "config/serving_recipes.toml",
            placement_path=root / "config/pod_placement.toml",
            residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        )


def test_stage_context_publisher_preserves_internal_attribute_errors() -> None:
    chair = identity("reader", "reader-v1")
    details = ServingDetails(
        tokenizer_revision=REVISION,
        seed=0,
        context_cap=2048,
        pixel_cap=1024,
        engine="vllm",
        engine_version="0.test",
        dtype="bfloat16",
        adapter_identity=None,
        endpoint="http://127.0.0.1:8000/v1",
        started_at="2026-08-09T12:00:00Z",
    )

    class Context:
        def write_serving_receipt(self, *unused):  # type: ignore[no-untyped-def]
            return {"relative_path": "receipts/value.json", "sha256": "c" * 64}

        def write_serving_launch_audit(self, audit):  # type: ignore[no-untyped-def]
            del audit
            raise AttributeError("injected audit-store defect")

        def write_serving_evidence_manifest(self, *unused):  # type: ignore[no-untyped-def]
            pytest.fail("evidence must not follow a failed audit write")

    with pytest.raises(AttributeError, match="audit-store defect"):
        StageContextReceiptPublisher(Context()).publish(
            build_receipt(chair, details), {"schema": "test-audit"}
        )


def test_preflight_assembly_prepares_a_new_log_root_before_default_disk_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chair = identity("reader", "reader-v1")
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    http = FakeHttp(model_ids=("reader-api",))
    launcher = FakeLauncher(http)
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture")
    root = Path(__file__).resolve().parents[2]
    context = assembly_context(root, registry)
    publisher = FakePublisher(http, context=context)
    new_log_root = tmp_path / "new" / "logs"
    observed_disk_paths: list[Path] = []

    class ExistingParentProbe:
        def __init__(self, *, disk_path: Path) -> None:
            assert disk_path.is_dir(), "default disk probe must receive a prepared log root"
            observed_disk_paths.append(disk_path)

        def profile(self, dtype: str) -> GpuProfile:
            return GpuProfile("GPU discovery unavailable", None, None, None, "0", "1", dtype)

    monkeypatch.setattr(
        sys.modules[assemble_serving_preflight_callback.__module__],
        "SystemGpuProbe",
        ExistingParentProbe,
    )

    callback = assemble_serving_preflight_callback(
        registry=registry,
        stage_context=context,
        cache_verifier=object(),
        receipt_publisher=publisher,
        smoke_call=lambda *args: pytest.fail("missing GPU tier must not start a service"),
        fixture=fixture,
        dtype="bfloat16",
        log_root=new_log_root,
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        recipes_path=root / "config/serving_recipes.toml",
        placement_path=root / "config/pod_placement.toml",
        launcher=launcher,
        http=http,
        package_inspector=FakePackages({"vllm": "fixture-v0"}),
    )

    report = callback()
    assert observed_disk_paths == [new_log_root]
    assert report["color"] == "red"
    assert new_log_root.is_dir()
    assert launcher.calls == []


def test_serving_recipe_and_placement_bytes_are_bound_into_the_run_configuration_digest(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    models = load_models_toml(root / "config/models.toml")
    copied_recipes = tmp_path / "serving_recipes.toml"
    copied_placement = tmp_path / "pod_placement.toml"
    copied_recipes.write_bytes((root / "config/serving_recipes.toml").read_bytes())
    copied_placement.write_bytes((root / "config/pod_placement.toml").read_bytes())
    fixture = {"fixture": "test"}
    first = run_config_bindings(
        models,
        fixture,
        "happy",
        serving_recipes_config_path=copied_recipes,
        pod_placement_config_path=copied_placement,
    )["config_digest"]
    copied_recipes.write_bytes(
        copied_recipes.read_bytes() + b"\n# different exact serving profile catalogue\n"
    )
    second = run_config_bindings(
        models,
        fixture,
        "happy",
        serving_recipes_config_path=copied_recipes,
        pod_placement_config_path=copied_placement,
    )["config_digest"]
    assert first != second
    copied_placement.write_bytes(
        copied_placement.read_bytes() + b"\n# different exact placement catalogue\n"
    )
    third = run_config_bindings(
        models,
        fixture,
        "happy",
        serving_recipes_config_path=copied_recipes,
        pod_placement_config_path=copied_placement,
    )["config_digest"]
    assert second != third


def test_http_parsers_reject_substrings_wrong_response_models_and_empty_output() -> None:
    assert not EndpointUnavailable("unspecified transport failure").definitively_absent

    models = HttpResponse(200, b'{"data":[{"id":"reader-api-shadow"}]}')
    with pytest.raises(ReadinessError, match="VLLM_MODEL_ID_MISSING"):
        require_exact_model_id(models, "reader-api")

    wrong_model = HttpResponse(
        200,
        b'{"model":"reader-api-shadow","choices":[{"message":{"content":"yes"}}]}',
    )
    with pytest.raises(ReadinessError, match="VLLM_PROBE_MODEL_MISMATCH"):
        parse_openai_answer(wrong_model, kind="chat-completions", expected_model_id="reader-api")

    blank = HttpResponse(
        200,
        b'{"model":"reader-api","choices":[{"message":{"content":" "}}]}',
    )
    with pytest.raises(ReadinessError, match="no non-empty text"):
        parse_openai_answer(blank, kind="chat-completions", expected_model_id="reader-api")


def test_a_deeply_nested_response_is_a_named_refusal_not_a_recursion_error() -> None:
    """Nesting, not length, is what breaks the JSON parser — and it is cheap.

    A few thousand opening brackets sit far inside the transport's 8 MiB size
    bound and raise `RecursionError`, which is not a `JSONDecodeError` and was
    not caught. It escaped the readiness poll's retry set, escaped the manager's
    `ServingError` handling, and was reported as an unexpected launch failure.
    Reproduced end to end through a real socket before the repair; pinned here
    at the parser, where the condition actually lives.
    """

    nested = b'{"data":' + b"[" * 20_000 + b"]" * 20_000 + b"}"
    with pytest.raises(ReadinessError, match="VLLM_MODELS_RESPONSE_INVALID"):
        parse_model_ids(HttpResponse(200, nested))
    with pytest.raises(ReadinessError, match="VLLM_PROBE_RESPONSE_INVALID"):
        parse_openai_answer(
            HttpResponse(200, nested), kind="chat-completions", expected_model_id="reader-api"
        )


def test_stage_context_publisher_uses_existing_run_receipt_seam() -> None:
    chair = identity("reader", "reader-v1")
    details = ServingDetails(
        tokenizer_revision=REVISION,
        seed=0,
        context_cap=2048,
        pixel_cap=1024,
        engine="vllm",
        engine_version="0.test",
        dtype="bfloat16",
        adapter_identity=None,
        endpoint="http://127.0.0.1:8000/v1",
        started_at="2026-08-09T12:00:00Z",
    )

    class Context:
        def __init__(self) -> None:
            self.calls: list[tuple[ChairIdentity, ServingDetails]] = []
            self.audit_calls: list[dict[str, object]] = []
            self.evidence_calls: list[tuple[dict[str, str], dict[str, str]]] = []

        def write_serving_receipt(self, supplied_identity, supplied_details):  # type: ignore[no-untyped-def]
            self.calls.append((supplied_identity, supplied_details))
            return {"relative_path": "receipts/sha256/" + "c" * 64 + ".json", "sha256": "c" * 64}

        def write_serving_launch_audit(self, audit):  # type: ignore[no-untyped-def]
            self.audit_calls.append(audit)
            return {"relative_path": "stages/preflight/blobs/sha256/audit", "sha256": "e" * 64}

        def write_serving_evidence_manifest(self, receipt_reference, audit_reference):  # type: ignore[no-untyped-def]
            self.evidence_calls.append((receipt_reference, audit_reference))
            return {"relative_path": "stages/preflight/blobs/sha256/evidence", "sha256": "f" * 64}

    context = Context()
    publisher = StageContextReceiptPublisher(context)
    publication = publisher.publish(build_receipt(chair, details), {"schema": "test-audit"})
    assert publication.receipt_reference == {
        "relative_path": "receipts/sha256/" + "c" * 64 + ".json",
        "sha256": "c" * 64,
    }
    assert publication.audit_reference == {
        "relative_path": "stages/preflight/blobs/sha256/audit",
        "sha256": "e" * 64,
    }
    assert publication.evidence_reference == {
        "relative_path": "stages/preflight/blobs/sha256/evidence",
        "sha256": "f" * 64,
    }
    assert context.calls == [(chair, details)]
    assert context.audit_calls == [{"schema": "test-audit"}]
    assert context.evidence_calls == [
        (
            {"relative_path": "receipts/sha256/" + "c" * 64 + ".json", "sha256": "c" * 64},
            {"relative_path": "stages/preflight/blobs/sha256/audit", "sha256": "e" * 64},
        )
    ]


def test_serving_smoke_reader_uses_the_owned_service_and_always_stops(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page, no model data")
    seen: list[str] = []

    def smoke(handle, supplied_identity, supplied_fixture, supplied_placement):  # type: ignore[no-untyped-def]
        assert supplied_identity == chair
        assert supplied_fixture == fixture
        assert supplied_placement == placement
        answer = handle.request_fixture_image(
            "chat-completions",
            fixture_image_payload(supplied_fixture),
            fixture=supplied_fixture,
        )
        seen.extend(answer.outputs)
        return SmokeResult(
            shape_valid=True,
            nonempty=True,
            format_valid=True,
            receipt={
                "fixture": supplied_fixture.name,
                "fixture_response_sha256": answer.response_sha256,
            },
            utilization=(UtilizationSample("71", "31"),),
        )

    reader = ServingSmokeReader(manager, smoke)
    result = reader.read(chair, fixture, placement, measured_gpu())
    assert seen == ["answer:reader-api"]
    assert result.receipt["service_receipt"]["chair"] == "reader"  # type: ignore[index]
    assert result.receipt["receipt_reference"] == {
        "relative_path": "receipts/sha256/" + "c" * 64 + ".json",
        "sha256": "c" * 64,
    }
    assert result.receipt["serving_launch_audit"]["schema"] == "serving-launch-audit.v1"  # type: ignore[index]
    assert result.receipt["serving_launch_audit_reference"]["sha256"] == "d" * 64  # type: ignore[index]
    assert result.receipt["serving_evidence_reference"]["sha256"] == "e" * 64  # type: ignore[index]
    assert (
        result.receipt["supplied_fixture_sha256"]
        == hashlib.sha256(fixture.read_bytes()).hexdigest()
    )
    assert result.receipt["smoke_service_request_count"] == 1
    assert result.receipt["smoke_fixture_request_count"] == 1
    assert (
        result.receipt["smoke_fixture_response_sha256"] == result.receipt["fixture_response_sha256"]
    )
    assert isinstance(result.receipt["smoke_fixture_output_sha256"], str)
    assert http.inference_calls >= 2  # readiness proof plus the golden-page call
    assert launcher.processes[0].terminate_calls == 1


def test_serving_smoke_reader_refuses_a_nominally_green_result_without_service_request(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page, no model data")
    reader = ServingSmokeReader(
        manager,
        lambda *args: SmokeResult(True, True, True, {"claimed": "green"}, ()),
    )

    with pytest.raises(
        ServingConfigurationError, match="without a final completed fixture-bound request"
    ):
        reader.read(chair, fixture, placement, measured_gpu())
    assert launcher.processes[0].terminate_calls == 1


def test_serving_smoke_reader_refuses_a_text_only_request_as_golden_page_evidence(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page, no model data")

    def text_only(handle, *unused):  # type: ignore[no-untyped-def]
        handle.request(
            "chat-completions", {"messages": [{"role": "user", "content": "not the page"}]}
        )
        return SmokeResult(True, True, True, {"claimed": "green"}, ())

    with pytest.raises(
        ServingConfigurationError, match="without a final completed fixture-bound request"
    ):
        ServingSmokeReader(manager, text_only).read(chair, fixture, placement, measured_gpu())
    assert launcher.processes[0].terminate_calls == 1


def test_fixture_request_refuses_image_bytes_from_another_local_page(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    expected_fixture = tmp_path / "expected.png"
    other_fixture = tmp_path / "other.png"
    expected_fixture.write_bytes(b"expected synthetic page")
    other_fixture.write_bytes(b"other synthetic page")
    handle = manager.start(chair, TIER)

    with pytest.raises(ServingConfigurationError, match="do not match"):
        handle.request_fixture_image(
            "chat-completions",
            fixture_image_payload(other_fixture),
            fixture=expected_fixture,
        )
    assert handle.requests_completed == 0
    handle.stop()


def test_fixture_request_refuses_an_image_hidden_outside_openai_chat_content(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    valid = fixture_image_payload(fixture)
    image_url = valid["messages"][0]["content"][1]["image_url"]  # type: ignore[index]
    hidden_image_payload = {
        "messages": [{"role": "user", "content": "text-only request"}],
        "ignored_extension": {"image_url": image_url},
    }
    handle = manager.start(chair, TIER)

    with pytest.raises(ServingConfigurationError, match="active image_url content block"):
        handle.request_fixture_image("chat-completions", hidden_image_payload, fixture=fixture)
    assert handle.requests_completed == 0
    handle.stop()


def test_fixture_request_requires_an_openai_image_object_at_the_active_content_block(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    malformed = json.loads(json.dumps(fixture_image_payload(fixture)))
    malformed["messages"][0]["content"][1]["image_url"] = "data:image/png;base64,ZmFrZQ=="
    handle = manager.start(chair, TIER)

    with pytest.raises(ServingConfigurationError, match="OpenAI image object"):
        handle.request_fixture_image("chat-completions", malformed, fixture=fixture)
    assert handle.requests_completed == 0
    handle.stop()


def test_fixture_request_dispatches_the_same_payload_snapshot_it_validates(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, _, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    validated = json.loads(json.dumps(fixture_image_payload(fixture)))
    sent_text_only = {"messages": [{"role": "user", "content": "not the page"}]}

    class SwitchingMapping(Mapping[str, object]):
        """Shows an image to validation but text-only data to ``dict(payload)``."""

        def __getitem__(self, key: str) -> object:
            return sent_text_only[key]

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(sent_text_only)

        def __len__(self) -> int:
            return len(sent_text_only)

        def get(self, key: str, default: object = None) -> object:
            return validated.get(key, default)

        def items(self):  # type: ignore[no-untyped-def]
            return validated.items()

    handle = manager.start(chair, TIER)
    handle.request_fixture_image("chat-completions", SwitchingMapping(), fixture=fixture)
    sent = [body for method, _, body in http.calls if method == "POST"][-1]
    assert sent is not None
    assert sent["messages"][0]["content"][1]["type"] == "image_url"  # type: ignore[index]
    handle.stop()


def test_serving_smoke_reader_refuses_green_result_after_a_later_text_request(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")

    def discarded_fixture_response(handle, *unused):  # type: ignore[no-untyped-def]
        answer = handle.request_fixture_image(
            "chat-completions", fixture_image_payload(fixture), fixture=fixture
        )
        handle.request("chat-completions", {"messages": [{"role": "user", "content": "text only"}]})
        return SmokeResult(
            True,
            True,
            True,
            {"fixture_response_sha256": answer.response_sha256},
            (),
        )

    with pytest.raises(
        ServingConfigurationError, match="without a final completed fixture-bound request"
    ):
        ServingSmokeReader(manager, discarded_fixture_response).read(
            chair, fixture, placement, measured_gpu()
        )
    assert launcher.processes[0].terminate_calls == 1


def test_serving_smoke_reader_requires_the_exact_fixture_response_token(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")

    def wrong_response_token(handle, *unused):  # type: ignore[no-untyped-def]
        handle.request_fixture_image(
            "chat-completions", fixture_image_payload(fixture), fixture=fixture
        )
        return SmokeResult(True, True, True, {"fixture_response_sha256": "0" * 64}, ())

    with pytest.raises(ServingConfigurationError, match="does not name the exact fixture response"):
        ServingSmokeReader(manager, wrong_response_token).read(
            chair, fixture, placement, measured_gpu()
        )
    assert launcher.processes[0].terminate_calls == 1


def test_smoke_reader_refuses_image_calibration_when_local_fixture_bytes_drift(
    tmp_path: Path,
) -> None:
    base = identity("base", "base-v1")
    adapter = identity("adapter", "adapter-v1", adapter_of="base")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={base.role: base, adapter.role: adapter},
        profiles=(
            profile_row(recipe="base-v1", chair="base", served_model_id="base-api", port=8000),
            profile_row(
                recipe="adapter-v1", chair="adapter", served_model_id="adapter-api", port=8100
            ),
        ),
        model_ids=("base-api", "adapter-api"),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"calibration version one")
    calibration = AdapterCalibration.from_image_fixture(
        fixture=fixture,
        prompt="Read the proof fixture.",
        mime_type="image/png",
    )
    fixture.write_bytes(b"calibration version two")

    reader = ServingSmokeReader(
        manager,
        lambda *args: pytest.fail("a drifted calibration must not launch"),
        calibration_for=lambda supplied_identity, supplied_fixture: calibration,
    )
    with pytest.raises(AdapterActivityError, match="does not match the local golden-page"):
        reader.read(adapter, fixture, placement, measured_gpu())
    assert launcher.calls == []


def test_smoke_reader_refuses_a_profile_dtype_not_assessed_by_preflight(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    reader = ServingSmokeReader(
        manager, lambda *args: pytest.fail("dtype mismatch must not launch")
    )

    with pytest.raises(ServingConfigurationError, match="differs from preflight's measured dtype"):
        reader.read(chair, fixture, placement, measured_gpu("float16"))
    assert launcher.calls == []


@pytest.mark.parametrize(
    ("field", "overage_value"),
    [
        ("max_model_len", 4096),
        # `_assert_profile_within_placement` checks four independent capacity
        # dimensions; max_pixels has its own dedicated test below (the square
        # relation makes an "overage" value less obvious than a plain `>`).
        # These two isolate the remaining pair, so a copy-paste/off-by-one
        # error specific to either comparison (e.g. `>=` vs `>`, or comparing
        # the wrong field) cannot hide behind the other three passing.
        ("gpu_memory_utilization", "0.90"),
        ("max_num_seqs", 2),
    ],
)
def test_smoke_reader_refuses_profile_capacity_above_measured_placement(
    tmp_path: Path, field: str, overage_value: object
) -> None:
    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row[field] = overage_value
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    reader = ServingSmokeReader(manager, lambda *args: pytest.fail("overage must not launch"))

    with pytest.raises(ServingConfigurationError, match="exceeds measured placement limits"):
        reader.read(chair, fixture, placement, measured_gpu())
    assert launcher.calls == []


def test_the_placement_pixel_cap_is_a_longest_edge_and_max_pixels_is_a_count(
    tmp_path: Path,
) -> None:
    """The two fields are not in the same unit, and the check must know that.

    `config/pod_placement.toml` caps the longest edge (its committed values are
    1344/1792/2304). `--mm-processor-kwargs` takes total pixel counts; the old
    pipeline's own proven values are 3136 = 56x56 and 2359296 = 1536x1536,
    read at the window. Compared directly, every realistic profile is refused
    for busting a plan it comfortably fits: 2359296 > 1792.
    """

    chair = identity("reader", "reader-v1")
    within = profile_row(
        recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
    )
    within["min_pixels"] = 3136
    within["max_pixels"] = 2359296  # 1536x1536, from /window/remote/serve_dai.sh
    over = dict(within)
    over["max_pixels"] = 1792 * 1792 + 1

    # A 1792-pixel longest edge admits at most 1792 * 1792 pixels.
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 4096, 1792, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")

    manager, _, _, launcher, _, _ = manager_for(
        tmp_path / "within",
        identities={chair.role: chair},
        profiles=(within,),
        model_ids=("reader-api",),
    )
    reader = ServingSmokeReader(manager, lambda *args: pytest.fail("stop before the launch"))
    # It gets past the capacity check and into the lifecycle, which is the point.
    with pytest.raises(BaseException) as refused:
        reader.read(chair, fixture, placement, measured_gpu())
    assert "exceeds measured placement limits" not in str(refused.value)

    manager, _, _, launcher, _, _ = manager_for(
        tmp_path / "over",
        identities={chair.role: chair},
        profiles=(over,),
        model_ids=("reader-api",),
    )
    reader = ServingSmokeReader(manager, lambda *args: pytest.fail("overage must not launch"))
    with pytest.raises(ServingConfigurationError, match="max_pixels"):
        reader.read(chair, fixture, placement, measured_gpu())
    assert launcher.calls == []


def test_serving_smoke_reader_turns_an_invalid_page_result_into_existing_preflight_red(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    placement = PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive=None,
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.85", 2048, 1024, 1),
    )
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page, no model data")

    def invalid(handle, supplied_identity, supplied_fixture, supplied_placement):  # type: ignore[no-untyped-def]
        del supplied_identity, supplied_placement
        answer = handle.request_fixture_image(
            "chat-completions",
            fixture_image_payload(supplied_fixture),
            fixture=supplied_fixture,
        )
        return SmokeResult(
            False,
            False,
            False,
            {"fixture": "golden-page.png", "fixture_response_sha256": answer.response_sha256},
            (),
        )

    class Cache:
        def verify(self, supplied_identity):  # type: ignore[no-untyped-def]
            assert supplied_identity == chair
            return {"manifest_digest": supplied_identity.digest_manifest}

        def refetch_once(self, supplied_identity):  # type: ignore[no-untyped-def]
            raise AssertionError(f"unexpected cache repair for {supplied_identity.role}")

    runner = PreflightRunner(
        ModelsConfig(witness_floor=0, chairs={chair.role: chair}),
        PlacementTable({"bfloat16": (8, 0)}, (placement,)),
        Cache(),
        ServingSmokeReader(manager, invalid),
        fixture,
    )
    report = runner.run(GpuProfile("fake GPU", "12.4", "550", (8, 0), "48", "100", "bfloat16"))
    assert report.color == "red"
    assert any(
        issue.code == "smoke-output-invalid" and issue.chair == "reader" for issue in report.issues
    )
    assert launcher.processes[0].terminate_calls == 1
