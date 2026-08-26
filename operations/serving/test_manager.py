"""Fake-first acceptance drills for the vLLM serving lifecycle.

Every fake has only a loopback-process shape.  No test imports vLLM, starts a
server, fetches a model, or contacts a provider.  The paired red cases are
intentional: an apparently successful service that has not answered, advertised
the wrong ID, or ignored an adapter is more expensive than a visible refusal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Mapping

import pytest
from PIL import Image, ImageDraw

from common.chairs.config import load_models_toml
from common.chairs.errors import ReceiptRefusal, ServingRecipeRefusal, UnresolvedChairRefusal
from common.chairs.models import ChairIdentity, ModelsConfig, ServingDetails, VerifiedSnapshot
from common.chairs.receipts import build_receipt
from common.chairs.registry import ChairRegistry
from common.runtree.store import RunTree
from common.stage import StageContext, run_config_bindings
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

from . import smoke as smoke_module
from .assembly import (
    _load_bound_configuration,
    assemble_serving_preflight_callback,
    assemble_serving_smoke_reader,
)
from .config import (
    FixtureProfile,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
    chair_preflight_identity_digest,
    load_serving_recipes,
    model_and_tokenizer_pins,
    parse_serving_recipes,
    profile_preflight_digest,
    seal_json_object,
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
    ServiceHandle,
    ServingManager,
    StageContextReceiptPublisher,
)
from .preflight import ServingSmokeReader, prepare_log_root
from .process import SubprocessLauncher
from .residency import FileResidencyLease
from .smoke import VisionSmokeCall

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
        "preflight_state": "proven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "readiness_probe": {
            "kind": "chat-completions",
            "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
        },
    }


def seal_rows(
    rows: Iterable[dict[str, object]],
    identities: Mapping[str, ChairIdentity] | None = None,
) -> list[dict[str, object]]:
    """Stamp the proof mark a real operator stamps after a green preflight.

    A proven row's mark is (row bytes, chair identity), so the identity digest
    goes in first and the row digest is taken over it. A row for a chair no
    identity was supplied for is stamped against a stand-in identity, so tests
    that only exercise catalogue parsing keep a well-formed mark.
    """

    sealed: list[dict[str, object]] = []
    for source in rows:
        row = dict(source)
        if row.get("preflight_state") == "proven":
            chair = (identities or {}).get(str(row["chair"])) or identity(
                str(row["chair"]), str(row["recipe"])
            )
            row.setdefault("preflight_identity_digest", chair_preflight_identity_digest(chair))
            row.setdefault("preflight_digest", profile_preflight_digest(row))
        sealed.append(row)
    return sealed


def recipes(*rows: dict[str, object], identities: Mapping[str, ChairIdentity] | None = None):
    return parse_serving_recipes(
        {"schema": "serving-recipes.v1", "profiles": seal_rows(rows, identities)}
    )


def test_real_serving_profile_is_structurally_unproven_until_preflight(tmp_path: Path) -> None:
    row = profile_row(recipe="reader", chair="perlector", served_model_id="reader", port=8100)
    row["preflight_state"] = "unproven"

    profile = recipes(row).profiles[0]

    assert profile.preflight_state == "unproven"
    assert profile.preflight_digest is None
    chair = identity("perlector", "reader")
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader",),
    )
    with pytest.raises(ServingRecipeRefusal, match="preflight_state='unproven'"):
        manager.start(chair, TIER)
    assert launcher.calls == []


def test_proven_profile_digest_launches_then_a_runtime_edit_is_refused_by_name(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    with pytest.raises(
        ServingConfigurationError,
        match=r"recipe='reader-v1', chair='reader', tier='generic-48gb'.*marked proven",
    ):
        parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]})

    row["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
    row["preflight_digest"] = profile_preflight_digest(row)
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
    )

    handle = manager.start(chair, TIER)
    assert launcher.calls
    handle.stop()

    edited = dict(row)
    edited["max_model_len"] = 4096
    with pytest.raises(
        ServingConfigurationError,
        match=r"recipe='reader-v1', chair='reader', tier='generic-48gb'.*stale preflight_digest",
    ):
        parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [edited]})


def test_proven_profile_digest_refuses_a_different_chair_identity_digest() -> None:
    proven = identity("reader", "reader-v1")
    different = identity("attestator-1", "witness-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["preflight_identity_digest"] = chair_preflight_identity_digest(proven)
    row["preflight_digest"] = profile_preflight_digest(row)

    different_digest = chair_preflight_identity_digest(different)
    assert different_digest != row["preflight_identity_digest"]
    row["preflight_identity_digest"] = different_digest

    with pytest.raises(ServingConfigurationError, match="stale preflight_digest"):
        parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]})


def test_a_real_serving_profile_missing_preflight_state_refuses_by_name():
    """Migration honesty: an older row that predates this field is not silently proven."""

    row = profile_row(recipe="reader", chair="perlector", served_model_id="reader", port=8100)
    del row["preflight_state"]

    with pytest.raises(ServingConfigurationError, match="preflight_state"):
        recipes(row)


def test_start_refuses_a_serving_profile_that_is_not_preflight_proven(tmp_path: Path) -> None:
    """A structurally 'unproven' profile must refuse launch, not merely round-trip.

    ``test_real_serving_profile_is_structurally_unproven_until_preflight`` only
    proves parsing preserves the value; this proves ``manager.start`` actually
    enforces it before any process, lease, or endpoint action.
    """

    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["preflight_state"] = "unproven"
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
    )

    with pytest.raises(ServingRecipeRefusal, match="preflight"):
        manager.start(chair, TIER)

    assert launcher.processes == []
    assert publisher.calls == []
    assert not (tmp_path / "logs").exists()
    # The refusal is at the recipe door, before any snapshot or residency work:
    # no store verification ran, and the pod/GPU lease file was never created,
    # so a following named start is not blocked by this one.
    assert registry.ensure_calls == []
    assert not (tmp_path / "pod-gpu.lock").exists()


def test_start_refuses_a_proven_adapter_over_an_unproven_base_before_any_snapshot(
    tmp_path: Path,
) -> None:
    """The recipe door covers every participating profile, the base's included.

    A proven adapter over an unproven base must refuse with no registry.ensure
    work behind it — not verify the adapter snapshot (or the base's) first and
    refuse afterwards.
    """

    base = identity("base", "base-v1")
    adapter = identity("adapter", "adapter-v1", adapter_of="base")
    base_row = profile_row(recipe="base-v1", chair="base", served_model_id="base-api", port=8000)
    base_row["preflight_state"] = "unproven"
    profiles = (
        base_row,
        profile_row(
            recipe="adapter-v1",
            chair="adapter",
            served_model_id="adapter-api",
            port=8100,
            tower_connector=True,
        ),
    )
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={base.role: base, adapter.role: adapter},
        profiles=profiles,
        model_ids=("base-api", "adapter-api"),
    )

    with pytest.raises(ServingRecipeRefusal, match="preflight"):
        manager.start(adapter, TIER)

    assert registry.ensure_calls == []
    assert launcher.processes == []
    assert publisher.calls == []
    assert not (tmp_path / "pod-gpu.lock").exists()


def test_a_proven_profile_does_not_carry_over_onto_a_repointed_chair(tmp_path: Path) -> None:
    """The proof is over (row, checkpoint); the row digest can only see one half.

    `config/models.toml` owns the model artifact, so repointing a chair at other
    weights — or bumping its revision — never touches the catalogue row. Its
    `preflight_digest` still verifies and `preflight_state` still reads
    `proven`, which is exactly the shape R1's O-a finding named: a proven claim
    surviving the edit that invalidated it. Nothing in the row can catch this,
    so the launch boundary holds both halves and refuses there.
    """

    proven = identity("reader", "reader-v1", revision="a" * 40)
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["preflight_identity_digest"] = chair_preflight_identity_digest(proven)
    row["preflight_digest"] = profile_preflight_digest(row)

    manager, _, _, launcher, _, _ = manager_for(
        tmp_path, identities={"reader": proven}, profiles=(row,), model_ids=("reader-api",)
    )
    manager.start(proven, TIER).stop()
    assert launcher.calls

    # models.toml now points the same chair, at the same recipe and tier, at a
    # different checkpoint. The catalogue bytes are untouched.
    repointed = identity("reader", "reader-v1", revision="b" * 40)
    catalogue = parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [dict(row)]})
    assert catalogue.profiles[0].preflight_state == "proven"

    manager, _, _, launcher, _, _ = manager_for(
        tmp_path / "repointed",
        identities={"reader": repointed},
        profiles=(row,),
        model_ids=("reader-api",),
    )
    with pytest.raises(ServingRecipeRefusal, match="must be preflighted again before launch"):
        manager.start(repointed, TIER)
    assert launcher.calls == []


def test_recipe_coverage_catches_a_repointed_chair_offline_not_on_the_rented_gpu() -> None:
    """`_launchable` refuses this, but only with the meter already running."""

    proven = identity("reader", "reader-v1", revision="a" * 40)
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["preflight_identity_digest"] = chair_preflight_identity_digest(proven)
    row["preflight_digest"] = profile_preflight_digest(row)
    catalogue = parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]})

    verify_recipes_cover_chairs(
        ModelsConfig(witness_floor=0, chairs={"reader": proven}), catalogue, (TIER,)
    )

    repointed = identity("reader", "reader-v1", revision="b" * 40)
    with pytest.raises(
        ServingConfigurationError,
        match=r"no longer configures.*recipe='reader-v1', chair='reader', tier='generic-48gb'",
    ):
        verify_recipes_cover_chairs(
            ModelsConfig(witness_floor=0, chairs={"reader": repointed}), catalogue, (TIER,)
        )


def test_an_unproven_profile_may_not_retain_either_half_of_a_proof_mark() -> None:
    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
    row["preflight_digest"] = profile_preflight_digest(row)
    row["preflight_state"] = "unproven"

    with pytest.raises(
        ServingConfigurationError,
        match=r"unproven and must not carry \['preflight_digest', 'preflight_identity_digest'\]",
    ):
        parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]})

    without_identity = {
        key: value for key, value in row.items() if key != "preflight_identity_digest"
    }
    without_identity["preflight_state"] = "proven"
    with pytest.raises(
        ServingConfigurationError, match="without a lowercase SHA-256 preflight_identity_digest"
    ):
        parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [without_identity]})


def measured_gpu(dtype: str = "bfloat16") -> GpuProfile:
    return GpuProfile("fake GPU", "12.4", "550", (8, 0), "48", "100", dtype)


def fixture_image_payload(fixture: Path) -> Mapping[str, object]:
    """A local data-URI request used only by fake golden-page smoke tests."""

    return AdapterCalibration.from_image_fixture(
        fixture=fixture,
        prompt="Read the supplied proof page.",
        mime_type="image/png",
    ).request_payload()


PAGE_WITNESS = "h6GMQDVxeNmr7RYvT82PqWkJz3BLaF9C"


def write_golden_page(fixture: Path) -> bytes:
    """Keep the witness in decodable pixels, never only in a prompt or filename."""

    page = Image.new("L", (640, 96), color="white")
    ImageDraw.Draw(page).text(
        (16, 36),
        f"PAGE-WITNESS: {PAGE_WITNESS}",
        fill="black",
    )
    page.save(fixture, format="PNG")
    encoded = fixture.read_bytes()
    with Image.open(fixture) as reopened:
        reopened.verify()
    return encoded


def vision_smoke() -> VisionSmokeCall:
    return VisionSmokeCall(
        PAGE_WITNESS,
        utilization=lambda: (UtilizationSample("71", "31"),),
    )


def smoke_placement() -> PlacementTier:
    return PlacementTier(
        identifier=TIER,
        min_vram_gib="40",
        max_vram_gib_exclusive="64",
        residency="single",
        detector_device="cpu",
        recipe=PlacementRecipe("0.90", 4096, 1792, 1),
    )


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
        recipes=recipes(*profiles, identities=identities),
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
    """The launch audit records the profile; only the argv makes that true.

    Its `profile` block is read from the same object the argv was rendered
    from, so dropping `--max-model-len` from the renderer leaves the audit still
    reporting a context cap that bound nothing — a claim about something nobody
    measured (GOVERNANCE 10). The three boolean flags are parametrized because
    a swapped pair reads identically in a spot check.
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

    An endpoint answering 503 there is the routing stub the package's README
    says cannot publish a receipt.
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

    `/v1/models` can advertise the exact id while a different process answers.
    A receipt naming a model that did not produce the answer is the provenance
    defect GOVERNANCE 6 exists for.
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
        ("VLLM_ERROR: fatal engine startup failure", "VLLM_ERROR"),
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


def test_an_unreadable_launch_log_is_a_named_readiness_refusal(tmp_path: Path) -> None:
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
        log_tail="VLLM_LOG_UNREADABLE: could not read launch log /private/child.log: denied",
    )

    with pytest.raises(
        ServingRecipeRefusal, match="VLLM_LOG_UNREADABLE.*could not read launch log"
    ):
        manager.start(chair, TIER)

    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []
    assert "VLLM_LOG_UNREADABLE" in registry.refusals[0][1]


def test_bare_runtimeerror_or_valueerror_in_the_log_does_not_abort_a_start_that_would_succeed(
    tmp_path: Path,
) -> None:
    """The exact false positive the old pipeline's grep produced, not carried here.

    A launch log naming ``RuntimeError``/``ValueError`` without one of the five
    named fatal substrings must reach a normal successful start;
    ``_fatal_log_signature``'s docstring holds the reasoning.
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


def test_a_benign_startup_traceback_does_not_abort_a_start_that_would_succeed(
    tmp_path: Path,
) -> None:
    """A logged-and-swallowed optional-backend traceback must not be fatal.

    vLLM has printed a benign traceback at startup for an optional backend
    that failed to import (FlashInfer probing is the documented case:
    vllm-project/vllm#12513, #30240) while still serving normally afterward.
    Because the readiness poll re-reads the whole launch tail every interval,
    treating any ``traceback`` line as fatal would make a chair that prints
    this deterministically unstartable, not merely cost one relaunch.
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
            "WARNING 08-09 12:00:00 [__init__.py:32] Failed to import from vllm._C\n"
            "Traceback (most recent call last):\n"
            '  File "vllm/_C.py", line 1, in <module>\n'
            "ModuleNotFoundError: no flashinfer\n"
            "INFO: continuing\n"
        ),
    )

    handle = manager.start(chair, TIER)

    assert registry.refusals == []
    assert len(publisher.calls) == 1
    handle.stop()


def test_bare_unknown_model_prose_without_a_colon_does_not_abort_a_start(
    tmp_path: Path,
) -> None:
    """Only vLLM's own 'Unknown model:' rejection form is fatal, not the words."""

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
        log_tail="INFO: this is an unknown model type warning, continuing anyway\n",
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


def test_the_default_package_inspector_binds_the_pin_to_the_launched_interpreter(
    tmp_path: Path,
) -> None:
    """A pin asserted against one Python and launched into another proves nothing.

    ``InstalledPackages`` reads *this* interpreter's distributions. The
    ``command_prefix`` seam let a caller launch a different absolute
    interpreter while that default inspector stayed in place, so
    ``_assert_runtime`` passed against an environment the engine never imports
    and the launch audit recorded ``runtime_packages.observed`` for the wrong
    Python -- a measurement of something nobody ran (GOVERNANCE 6, 10).
    """

    chair = identity("reader", "reader-v1")
    profiles = (
        profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000),
    )
    http = FakeHttp(model_ids=("reader-api",))
    other_interpreter = str(Path(sys.executable).parent / "python-from-another-venv")

    def build(**overrides: object) -> ServingManager:
        arguments: dict[str, object] = {
            "registry": FakeRegistry({chair.role: chair}, tmp_path),
            "recipes": recipes(*profiles),
            "config_inputs": ServingConfigInputs("1" * 64, "2" * 64),
            "launcher": FakeLauncher(http),
            "http": http,
            "receipt_publisher": FakePublisher(http),
            "log_root": tmp_path / "logs",
            "residency_lease": FileResidencyLease(tmp_path / "pod-gpu.lock"),
        }
        arguments.update(overrides)
        return ServingManager(**arguments)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must launch"):
        build(command_prefix=(other_interpreter, "-m", "vllm"))

    # Two escapes, both explicit. Naming this interpreter is always allowed,
    # and a caller that owns the pairing may launch elsewhere by supplying the
    # inspector for the environment it actually launches.
    build(command_prefix=(sys.executable, "-m", "vllm"))
    build(
        command_prefix=(other_interpreter, "-m", "vllm"),
        package_inspector=FakePackages({"vllm": "0.test"}),
    )

    # Resolved-path equality would not do: two virtualenvs routinely symlink
    # one real interpreter while holding entirely different site-packages, so
    # a link to this very executable is still another environment's python.
    linked = tmp_path / "linked-python"
    linked.symlink_to(sys.executable)
    with pytest.raises(ValueError, match="must launch"):
        build(command_prefix=(str(linked), "-m", "vllm"))


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


def test_pin_drift_refusal_retains_the_serving_failure_detail(tmp_path: Path) -> None:
    requested = identity("reader", "reader-v1")
    configured = identity("reader", "reader-v1", revision="c" * 40)
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={requested.role: requested},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        package_version="different",
    )
    manager.registry = ChairRegistry(ModelsConfig(witness_floor=0, chairs={"reader": configured}))

    with pytest.raises(UnresolvedChairRefusal) as caught:
        manager.start(requested, TIER)

    assert "identity differs from the configured pin" in caught.value.difference
    assert "VLLM_RUNTIME_PIN_MISMATCH" in caught.value.difference
    assert "runtime package pin mismatch: vllm='different', expected '0.test'" in (
        caught.value.difference
    )
    assert launcher.calls == []


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


def test_unexpected_start_failure_names_its_exception_type_in_the_refusal(tmp_path: Path) -> None:
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
    )

    def fail_audit(**unused):  # type: ignore[no-untyped-def]
        raise LookupError("injected audit construction failure")

    manager._launch_audit = fail_audit  # type: ignore[method-assign]

    with pytest.raises(ServingRecipeRefusal, match="LookupError"):
        manager.start(chair, TIER)

    assert "LookupError: injected audit construction failure" in registry.refusals[-1][1]
    assert launcher.processes[0].terminate_calls == 1


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


class SilentPollFailure:
    """A ``ServerProcess`` whose observation raises with no message at all.

    ``_stop_process`` calls ``process.poll()`` outside its own ``try``, so this
    arrives at the wrapping handlers exactly as raised. ``str()`` of an
    exception constructed without arguments is the empty string, which is what
    makes a type-less wrapper visible.
    """

    pid = 4242

    def poll(self) -> int | None:
        raise RuntimeError()

    def terminate(self) -> None:  # pragma: no cover - never reached past poll
        raise AssertionError("an unobservable child must not be signalled")

    def kill(self) -> None:  # pragma: no cover - never reached past poll
        raise AssertionError("an unobservable child must not be signalled")

    def wait(self, timeout_seconds: float) -> int:  # pragma: no cover - never reached
        raise AssertionError("an unobservable child must not be waited on")

    def read_tail(self, maximum_bytes: int = 16_384) -> str:  # pragma: no cover - never reached
        return ""


def test_an_unobservable_child_reaches_the_refusal_by_name_not_as_an_empty_reason(
    tmp_path: Path,
) -> None:
    """Both wrappers must name the exception type, not only its message.

    A `ServerProcess` implementation whose `poll()` raises reaches `stop()` and
    `_attempt_cleanup` as it stands. Wrapping it as `ServiceStopError(str(error))`
    turned a message-less exception into `VLLM_STOP_FAILED: ` and nothing else --
    and the registry raises one refusal, so whatever is not in it is not
    anywhere (GOVERNANCE 2).
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = manager_for(
        tmp_path / "stop",
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    handle = manager.start(chair, TIER)
    handle.process = SilentPollFailure()  # type: ignore[assignment]

    with pytest.raises(ServiceStopError, match="RuntimeError") as stopped:
        handle.stop()
    assert str(stopped.value).strip() != ServiceStopError.code

    # The same loss, on the failed-launch side, where it lands inside the one
    # refusal the registry raises rather than in a caller's own exception.
    class UnobservableLauncher:
        def __init__(self) -> None:
            self.calls = 0

        def launch(self, argv, log_path, *, inheritable_fds=()):  # type: ignore[no-untyped-def]
            del argv, log_path, inheritable_fds
            self.calls += 1
            return SilentPollFailure()

    cleanup_manager, _, _, _, cleanup_registry, _ = manager_for(
        tmp_path / "cleanup",
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    launcher = UnobservableLauncher()
    cleanup_manager.launcher = launcher  # type: ignore[assignment]

    with pytest.raises(ServingRecipeRefusal):
        cleanup_manager.start(chair, TIER)

    assert launcher.calls == 1
    detail = cleanup_registry.refusals[-1][1]
    assert "unexpected serving start failure: RuntimeError" in detail
    assert "cleanup after failed serving launch could not complete: RuntimeError" in detail
    assert "lease is retained" in detail


def test_an_interrupt_whose_cleanup_also_fails_reports_the_stop_failure_instead(
    tmp_path: Path,
) -> None:
    """The half of the interrupt branch nothing exercised, and the cost it pays.

    ``test_interrupt_during_start_stops_the_child_and_preserves_the_interrupt``
    covers only the case where cleanup succeeds. When it does not, the two
    facts cannot both be this exception and the possibly-resident child wins:
    the ``KeyboardInterrupt`` is deliberately spent to carry the stop failure,
    so an ordinary ``except Exception`` above this frame now catches what was a
    Ctrl-C. Pinned because that trade is a decision, not an accident.
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
        ignore_terminate=True,
        ignore_kill=True,
    )

    def interrupt(*unused):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt("injected operator interrupt")

    publisher.publish = interrupt  # type: ignore[method-assign]

    with pytest.raises(ServiceStopError) as caught:
        manager.start(chair, TIER)

    detail = str(caught.value)
    assert "start=KeyboardInterrupt: injected operator interrupt" in detail
    assert "stop=" in detail
    assert isinstance(caught.value.__cause__, KeyboardInterrupt)
    assert launcher.processes[0].kill_calls == 1
    # And it really is catchable as an ordinary exception now, which is the
    # documented cost of the trade.
    assert isinstance(caught.value, Exception)
    # The lease is retained, so the next start refuses by name rather than
    # putting a second process on the card.
    with pytest.raises(ServingRecipeRefusal, match="shutdown is not verified"):
        manager.start(chair, TIER)


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


def test_prelaunch_residency_descriptor_fault_is_a_launch_refusal(tmp_path: Path) -> None:
    class ReleasedHandleLease:
        def acquire(self, requested: ChairIdentity):  # type: ignore[no-untyped-def]
            handle = FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(requested)
            handle.release()
            return handle

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
        residency_lease=ReleasedHandleLease(),  # type: ignore[arg-type]
    )

    with pytest.raises(ServingRecipeRefusal) as caught:
        manager.start(chair, TIER)

    assert "VLLM_LAUNCH_ERROR" in caught.value.difference
    assert "VLLM_STOP_FAILED" not in caught.value.difference
    assert launcher.calls == []


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


def test_subprocess_launcher_creates_new_parent_segments_owner_only(
    tmp_path: Path,
) -> None:
    preexisting = tmp_path / "preexisting"
    preexisting.mkdir()
    preexisting.chmod(0o755)
    log_path = preexisting / "first-new" / "second-new" / "child.log"

    old_umask = os.umask(0o022)
    try:
        process = SubprocessLauncher().launch((sys.executable, "-c", "pass"), log_path)
    finally:
        os.umask(old_umask)
    assert process.wait(3) == 0

    assert stat.S_IMODE(preexisting.stat().st_mode) == 0o755
    for created in (preexisting / "first-new", log_path.parent):
        mode = stat.S_IMODE(created.stat().st_mode)
        assert mode == 0o700, f"expected owner-only 0o700, got {oct(mode)} for {created}"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def _wait_until(predicate: Callable[[], bool], *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("condition did not become true in time")
        time.sleep(0.02)


def test_a_still_running_child_polls_none_and_a_terminated_one_reports_its_signal(
    tmp_path: Path,
) -> None:
    """``poll``/``terminate``/``wait`` against a real child, not ``FakeProcess``."""

    process = SubprocessLauncher().launch(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        tmp_path / "child.log",
    )
    try:
        assert process.poll() is None
        process.terminate()
        exit_code = process.wait(5)
        assert exit_code == -signal.SIGTERM
        assert process.poll() == -signal.SIGTERM
    finally:
        with suppress(ProcessLookupError):
            process.kill()


def test_kill_reaches_a_child_that_ignores_sigterm(tmp_path: Path) -> None:
    """A child that ignores SIGTERM must still fall to SIGKILL."""

    ready = tmp_path / "ready"
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(30)\n"
    )
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", script),
        tmp_path / "child.log",
    )
    try:
        # The ready file is only written after the SIGTERM handler is
        # installed, so terminate() below cannot race the default
        # disposition and kill the child before it starts ignoring the
        # signal.
        _wait_until(lambda: ready.exists())
        process.terminate()
        with pytest.raises(TimeoutError):
            process.wait(0.5)
        process.kill()
        assert process.wait(5) == -signal.SIGKILL
    finally:
        with suppress(ProcessLookupError):
            process.kill()


def _require_the_parser_actually_recurses(payload) -> None:
    """Assert the premise these deep-nesting tests rest on, before trusting them.

    They prove that a `RecursionError` from `json` is turned into a named refusal
    rather than escaping. On an interpreter whose parser absorbs this depth there
    is no `RecursionError` to turn into anything, the refusal correctly does not
    fire, and the test fails while the code is perfectly right — which is what
    happened on Python 3.14 while the same commit passed on 3.13 and in CI on
    3.12. A skip naming the reason is honest; a failure blaming the code is not.
    """
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    try:
        json.loads(text)
    except RecursionError:
        return
    except ValueError as error:
        # A malformed payload is a defect in this test, not a capability of the
        # interpreter. Swallowing it here would skip while asserting a fact about
        # the parser that was never established — the exact failure this helper
        # exists to prevent, reproduced inside the helper itself.
        pytest.fail(f"this test's own deep-nesting payload is not valid JSON: {error}")
    pytest.skip(
        "this interpreter's JSON parser absorbs 20,000 levels, so there is no "
        "RecursionError for the named refusal to catch and this test proves nothing"
    )


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="distinguishing a signalled grandchild from a zombie needs /proc, which "
    "macOS does not have; this test asserts nothing here and failed at its own "
    "precondition rather than saying so",
)
def test_terminate_reaches_a_grandchild_in_the_same_owned_session(tmp_path: Path) -> None:
    """``os.killpg`` against the launch's own session, not just the direct child.

    ``start_new_session=True`` puts the direct child in a fresh process group;
    an ordinary grandchild it spawns (no ``setsid`` of its own) inherits that
    same group.  ``terminate`` must reach both, the way a real vLLM process
    tree does, not merely the one PID this manager launched.
    """

    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(sys.argv[1], 'w') as handle:\n"
        "    handle.write(str(grandchild.pid))\n"
        "time.sleep(30)\n"
    )
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", script, str(pidfile)),
        tmp_path / "child.log",
    )
    try:
        _wait_until(lambda: pidfile.exists() and pidfile.read_text())
        grandchild_pid = int(pidfile.read_text())

        def _grandchild_alive() -> bool:
            # The grandchild is orphaned once its true parent (the direct
            # child this manager owns) is also killed by the same killpg, so
            # nothing left in this process tree ever reaps it: a signalled
            # grandchild becomes an unreapable zombie rather than
            # disappearing, and plain os.kill(pid, 0) still succeeds against
            # a zombie. /proc's own state character is the one thing that
            # distinguishes "signalled and exited" from "still running" here.
            try:
                status = Path(f"/proc/{grandchild_pid}/status").read_text()
            except FileNotFoundError:
                return False
            return "(zombie)" not in status

        assert _grandchild_alive(), "grandchild must be running before terminate is asserted"
        process.terminate()
        process.wait(5)
        _wait_until(lambda: not _grandchild_alive())
    finally:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)


def test_read_tail_returns_only_the_bounded_tail_of_a_real_log(tmp_path: Path) -> None:
    marker = "END-OF-LOG-MARKER"
    script = (
        "import sys\n"
        "sys.stdout.write('x' * 4000 + chr(10))\n"
        f"sys.stdout.write({marker!r} + chr(10))\n"
        "sys.stdout.flush()\n"
    )
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", script),
        tmp_path / "child.log",
    )
    assert process.wait(5) == 0

    tail = process.read_tail(maximum_bytes=64)
    assert len(tail.encode("utf-8", errors="replace")) <= 64
    assert tail.strip().endswith(marker)

    whole = process.read_tail(maximum_bytes=1_000_000)
    assert marker in whole
    assert "x" * 4000 in whole


def test_close_log_clears_the_handle_once_exit_is_observed(tmp_path: Path) -> None:
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", "pass"),
        tmp_path / "child.log",
    )
    assert process.wait(3) == 0
    assert process._log_handle is None  # type: ignore[attr-defined]


def test_poll_closes_the_log_when_it_observes_a_self_terminated_child(tmp_path: Path) -> None:
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", "pass"),
        tmp_path / "child.log",
    )

    _wait_until(lambda: process.poll() is not None)

    assert process._log_handle is None  # type: ignore[attr-defined]


def test_read_tail_reports_a_launch_log_read_failure(tmp_path: Path) -> None:
    log_path = tmp_path / "child.log"
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", "pass"),
        log_path,
    )
    assert process.wait(3) == 0
    log_path.unlink()

    tail = process.read_tail()

    assert "could not read launch log" in tail
    assert str(log_path) in tail


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
            # a real chair had been configured to serve before the real roster
            # was activated with verified manifests and real serving profiles.
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
    local = next(
        value
        for value in models.chairs.values()
        if isinstance(value, ChairIdentity) and value.source == "local-repository"
    )
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

    ``_launchable``'s docstring holds why that difference is load-bearing.
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


def test_failing_manager_start_routes_through_the_real_chair_registry(tmp_path: Path) -> None:
    """Prove the production manager/registry no-substitution wiring end to end."""

    root = Path(__file__).resolve().parents[2]
    registry = ChairRegistry.from_toml(root / "config/models.toml")
    chair = registry.resolve("attestator_1")
    assert isinstance(chair, ChairIdentity)
    http = FakeHttp(model_ids=())
    launcher = FakeLauncher(http)
    publisher = FakePublisher(http)
    manager = ServingManager(
        registry=registry,
        recipes=recipes(
            fixture_row(
                recipe=chair.serving_recipe,
                chair=chair.role,
                description="checked-in fixture chairs are never launched",
            )
        ),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=http,
        receipt_publisher=publisher,
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )

    with pytest.raises(ServingRecipeRefusal) as caught:
        manager.start(chair, TIER)

    assert caught.value.chair == chair.role
    assert "fixture serving profile" in caught.value.difference
    assert "checked-in fixture chairs are never launched" in caught.value.difference
    assert launcher.processes == []
    assert publisher.calls == []


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

    ARCHITECTURE requires exactly that of the Perlector chair, and the absent
    revision flags are the answer rather than a gap: see
    ``model_and_tokenizer_pins``.
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
        gpu_profile=measured_gpu(),
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
        reader.read(chair, tmp_path / "unused.png", forged_placement)
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
            gpu_profile=measured_gpu(),
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
            gpu_profile=measured_gpu(),
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


def test_load_placement_table_parses_the_bytes_it_is_given_and_not_the_path(
    tmp_path: Path,
) -> None:
    """The single-snapshot contract, asserted at the loader itself.

    `source_bytes` exists so a caller that has already digested a file can parse
    those exact bytes. Nothing tested that it does. This is the cheap half of the
    guard: if the parameter is ever dropped, ignored, or reordered into a second
    read, this fails immediately and by name rather than somewhere downstream.
    """

    root = Path(__file__).resolve().parents[2]
    sealed = (root / "config/pod_placement.toml").read_bytes()
    altered = sealed.replace(b"batch_size = 1\n", b"batch_size = 9\n", 1)
    assert altered != sealed, "the fixture no longer contains the batch size this test flips"

    path = tmp_path / "pod_placement.toml"
    path.write_bytes(altered)

    from_bytes = load_placement_table(path, source_bytes=sealed)
    from_path = load_placement_table(path)

    assert from_bytes.choose(Decimal(24)).recipe.batch_size == 1
    assert from_path.choose(Decimal(24)).recipe.batch_size == 9


def test_bound_configuration_parses_the_snapshot_it_digested_not_a_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed-configuration interlock, against the substitution that beat it.

    The first version of this repair read the placement file twice — once to
    digest and once to parse — so an ordinary replacement between the two reads
    produced a table the run never sealed while `require_loaded` compared the
    sealed digest and passed. Nothing in the suite caught it: the existing
    substitution test alters the file *before* the call, so both reads see the
    same bytes and even the two-read code refuses.

    This one makes the two reads distinguishable. The path yields the sealed bytes
    once and the altered bytes to every later reader, so a second read is visible
    in the parsed result and nowhere else.
    """

    root = Path(__file__).resolve().parents[2]
    recipes_path = root / "config/serving_recipes.toml"
    sealed = (root / "config/pod_placement.toml").read_bytes()
    altered = sealed.replace(b"batch_size = 1\n", b"batch_size = 9\n", 1)
    assert altered != sealed, "the fixture no longer contains the batch size this test flips"

    placement_path = tmp_path / "pod_placement.toml"
    placement_path.write_bytes(sealed)

    real_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def read_bytes_that_changes_after_the_first(self: Path) -> bytes:
        if self == placement_path:
            reads.append(self)
            return sealed if len(reads) == 1 else altered
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes_that_changes_after_the_first)

    _, placement, _ = _load_bound_configuration(
        sealed_config_inputs=ServingConfigInputs(
            hashlib.sha256(recipes_path.read_bytes()).hexdigest(),
            hashlib.sha256(sealed).hexdigest(),
        ).to_record(),
        recipes_path=recipes_path,
        placement_path=placement_path,
    )

    assert len(reads) == 1, f"the placement file was read {len(reads)} times, not once"
    assert placement.choose(Decimal(24)).recipe.batch_size == 1


def test_bound_configuration_refuses_an_unusable_placement_in_the_serving_vocabulary(
    tmp_path: Path,
) -> None:
    """Every way the placement file can fail refuses as a `ServingError`.

    `PlacementRefusal` is a `ValueError` and `ServingConfigurationError` is a
    `ServingError`, so a handler written for this boundary catches one and not the
    other. Before this test, a missing file refused in the serving vocabulary while
    malformed TOML and a non-UTF-8 file escaped as `PlacementRefusal` — the same
    rule enforced in two places and repaired in one, which is the shape this branch
    has now found five times.
    """

    root = Path(__file__).resolve().parents[2]
    recipes_path = root / "config/serving_recipes.toml"
    sealed = (root / "config/pod_placement.toml").read_bytes()

    missing = tmp_path / "absent.toml"
    malformed = tmp_path / "malformed.toml"
    malformed.write_bytes(b"schema = \n")
    not_utf8 = tmp_path / "not_utf8.toml"
    not_utf8.write_bytes(b"\xff\xfe not utf-8 at all\n")

    sealed_inputs = ServingConfigInputs(
        hashlib.sha256(recipes_path.read_bytes()).hexdigest(),
        hashlib.sha256(sealed).hexdigest(),
    ).to_record()

    for placement_path in (missing, malformed, not_utf8):
        with pytest.raises(ServingConfigurationError):
            _load_bound_configuration(
                sealed_config_inputs=sealed_inputs,
                recipes_path=recipes_path,
                placement_path=placement_path,
            )


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
            gpu_profile=measured_gpu(),
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
            gpu_profile=measured_gpu(),
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


def test_prepare_log_root_is_owner_only_regardless_of_umask_or_prior_mode(
    tmp_path: Path,
) -> None:
    """The log root's listing (chair roles, launch UUIDs) is as owner-only as its files.

    Each per-launch log file is already forced 0600 at creation. The directory
    itself was left at the ambient umask, which is commonly world-readable/
    executable, so its listing was visible to any other user on the pod even
    though the file contents were not.
    """

    fresh = tmp_path / "fresh-logs"
    old_umask = os.umask(0o022)
    try:
        prepared = prepare_log_root(fresh)
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE(prepared.stat().st_mode)
    assert mode == 0o700, f"expected owner-only 0o700, got {oct(mode)}"

    loose = tmp_path / "preexisting-logs"
    loose.mkdir(mode=0o755)
    prepared_again = prepare_log_root(loose)
    mode_again = stat.S_IMODE(prepared_again.stat().st_mode)
    assert mode_again == 0o700, (
        f"expected a pre-existing directory tightened, got {oct(mode_again)}"
    )


def test_prepare_log_root_refuses_a_symlink_rather_than_re_moding_its_target(
    tmp_path: Path,
) -> None:
    """A link pointing at a real directory is the one symlink `mkdir` forgives.

    `mkdir(exist_ok=True)` refuses a file, a symlink to a file and a broken
    symlink. It accepts a symlink to a directory, and the `chmod` that follows
    then re-modes the target — so the run's logs land somewhere it never named,
    under a mode set on a directory it does not own. Found by CodeRabbit.
    """

    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir(mode=0o755)
    linked = tmp_path / "logs"
    linked.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ServingConfigurationError, match="symbolic link"):
        prepare_log_root(linked)

    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755, (
        "the refusal must leave the link's target exactly as it found it"
    )


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
    bound and raise `RecursionError`, which is not a `JSONDecodeError`. Left
    uncaught it escapes every named refusal between here and the operator.
    """

    nested = b'{"data":' + b"[" * 20_000 + b"]" * 20_000 + b"}"
    _require_the_parser_actually_recurses(nested)
    with pytest.raises(ReadinessError, match="VLLM_MODELS_RESPONSE_INVALID"):
        parse_model_ids(HttpResponse(200, nested))
    with pytest.raises(ReadinessError, match="VLLM_PROBE_RESPONSE_INVALID"):
        parse_openai_answer(
            HttpResponse(200, nested), kind="chat-completions", expected_model_id="reader-api"
        )


def test_seal_json_object_refuses_deep_or_cyclic_input_as_a_named_error() -> None:
    """The outbound/config sealing side of the same RecursionError gap.

    `_json_object`'s own comment (above) explains that a `RecursionError` from
    nesting escapes every named refusal if left uncaught; `seal_json_object`
    recurses the same way on its way *out*, and must catch its own.
    """

    deep: object = "leaf"
    for _ in range(5_000):
        deep = [deep]
    with pytest.raises(ServingConfigurationError, match="must be JSON-compatible"):
        seal_json_object({"payload": deep}, label="test payload")

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ServingConfigurationError, match="must be JSON-compatible"):
        seal_json_object(cyclic, label="test payload")


def test_readiness_probe_refuses_a_deeply_nested_request_json_as_a_named_error() -> None:
    deep_request_json = '{"messages":' + "[" * 20_000 + "]" * 20_000 + "}"
    _require_the_parser_actually_recurses(deep_request_json)
    with pytest.raises(ServingConfigurationError, match="not JSON"):
        recipes(
            profile_row(
                recipe="reader-v1",
                chair="reader",
                served_model_id="reader-api",
                port=8000,
            )
            | {
                "readiness_probe": {
                    "kind": "chat-completions",
                    "request_json": deep_request_json,
                }
            }
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


def test_a_manager_built_audit_reaches_a_real_stage_context_end_to_end(tmp_path: Path) -> None:
    """The run-sealed-configuration interlock, joined rather than traced by hand.

    Every other manager test publishes through ``FakePublisher``; every assembly
    test uses ``FakeStageContext``, a two-field frozen dataclass. Nothing before
    this test drove a manager-built launch audit into a real
    ``StageContext``/``StageContextReceiptPublisher`` -- the interlock that stops
    a launch running under configuration bytes the run did not seal was only
    ever exercised on its two sides separately.
    """

    root = Path(__file__).resolve().parents[2]
    registry = ChairRegistry.from_toml(root / "config/models.toml")
    bindings = run_config_bindings(registry.config, {"fixture": "none"}, "test")
    tree = RunTree.create(
        tmp_path,
        "sm-fix-f6",
        source_manifest=[],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
    )
    run = tree.read_run()
    context = StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario="test",
        stage="attestatores",
        adapter_revision=None,
        args=object(),
        registry=registry,
        serving_config_inputs=bindings["serving_config_inputs"],
    )
    chair = registry.resolve("attestator_1")
    assert isinstance(chair, ChairIdentity)

    http = FakeHttp(model_ids=("reader-api",))
    launcher = FakeLauncher(http)
    manager = ServingManager(
        registry=registry,
        recipes=recipes(
            profile_row(
                recipe=chair.serving_recipe,
                chair=chair.role,
                served_model_id="reader-api",
                port=8000,
            ),
            identities={chair.role: chair},
        ),
        config_inputs=ServingConfigInputs.from_record(bindings["serving_config_inputs"]),
        launcher=launcher,
        http=http,
        receipt_publisher=StageContextReceiptPublisher(context),
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )

    handle = manager.start(chair, TIER)
    try:
        assert handle.receipt_reference["relative_path"]
        assert handle.audit_reference["relative_path"]
        assert handle.evidence_reference["relative_path"]
        stored_audit = tree.read_bytes(handle.audit_reference["relative_path"])
        assert bindings["serving_config_inputs"]["serving_recipes_sha256"].encode() in stored_audit
        stored_receipt = tree.read_run_receipt(dict(handle.receipt_reference))
        assert stored_receipt["chair"] == chair.role
    finally:
        handle.stop()


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

    reader = ServingSmokeReader(manager, smoke, gpu_profile=measured_gpu())
    result = reader.read(chair, fixture, placement)
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


def test_vision_smoke_call_refuses_an_unstarted_service_handle(tmp_path: Path) -> None:
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
    profile = manager.recipes.for_identity(chair, TIER)
    assert isinstance(profile, ServingProfile)
    unstarted = ServiceHandle(
        manager,
        chair,
        profile,
        FakeProcess(9999),
        build_receipt(
            chair,
            ServingDetails(
                chair.receipt_revision,
                profile.seed,
                profile.max_model_len,
                profile.max_pixels,
                "vllm",
                "0.test",
                profile.dtype,
                None,
                profile.endpoint,
                "2026-08-21T00:00:00Z",
            ),
        ),
        {},
        {},
        {},
        {},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)

    with pytest.raises(ServiceStopError, match="not this manager's active owned service"):
        vision_smoke()(unstarted, chair, fixture, smoke_placement())


def test_vision_smoke_call_marks_an_answer_producible_from_its_prompt_invalid(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    expected = f"PAGE-WITNESS: {PAGE_WITNESS}"
    prompt_only_answer = "PAGE-WITNESS: <the page witness string>"
    assert PAGE_WITNESS not in vision_smoke().prompt
    assert prompt_only_answer != expected
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": prompt_only_answer},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert result.shape_valid is True
    assert result.nonempty is True
    assert result.format_valid is False
    assert result.receipt["page_witness_matches"] is False
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_accepts_the_exact_model_answer_and_records_identity(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1", revision="b" * 40)
    expected = f"PAGE-WITNESS: {PAGE_WITNESS}"
    manager, _, http, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": expected},
    )
    fixture = tmp_path / "golden-page.png"
    fixture_bytes = write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert (result.shape_valid, result.nonempty, result.format_valid) == (True, True, True)
    assert result.receipt["served_model_id"] == "reader-api"
    assert result.receipt["resolved_identity"] == chair.to_record()
    assert result.receipt["resolved_revision"] == "b" * 40
    assert result.receipt["resolved_revision_kind"] == "git-commit"
    request = http.calls[-1][2]
    assert isinstance(request, dict)
    messages = request["messages"]
    assert isinstance(messages, list)
    image_url = messages[0]["content"][1]["image_url"]["url"]  # type: ignore[index]
    assert isinstance(image_url, str)
    assert image_url == "data:image/png;base64," + base64.b64encode(fixture_bytes).decode("ascii")
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_reports_multiple_nonempty_choices_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chair = identity("reader", "reader-v1")
    expected = f"PAGE-WITNESS: {PAGE_WITNESS}"
    manager, _, http, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": expected},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    original_request = http.request

    def two_choice_request(
        method: str,
        url: str,
        *,
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        response = original_request(
            method,
            url,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        if method == "POST" and url.endswith("/chat/completions"):
            response_body = json.loads(response.body)
            response_body["choices"].append(response_body["choices"][0])
            return HttpResponse(response.status, json.dumps(response_body).encode())
        return response

    monkeypatch.setattr(http, "request", two_choice_request)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert result.shape_valid is False
    assert result.nonempty is True
    assert result.format_valid is False
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_receipt_does_not_retain_a_witness_bearing_fixture_name(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    expected = f"PAGE-WITNESS: {PAGE_WITNESS}"
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": expected},
    )
    fixture = tmp_path / f"{PAGE_WITNESS}.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert "fixture" not in result.receipt
    assert PAGE_WITNESS not in json.dumps(result.receipt, sort_keys=True)
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


@pytest.mark.parametrize(
    "answer",
    [
        f" PAGE-WITNESS: {PAGE_WITNESS}",
        f"PAGE-WITNESS: {PAGE_WITNESS} ",
        f"PAGE-WITNESS: {PAGE_WITNESS}\n",
        f"\nPAGE-WITNESS: {PAGE_WITNESS}",
    ],
)
def test_vision_smoke_call_marks_text_outside_the_exact_witness_line_invalid(
    tmp_path: Path,
    answer: str,
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
        outputs={"reader-api": answer},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert result.shape_valid is True
    assert result.nonempty is True
    assert result.format_valid is False
    assert result.receipt["page_witness_matches"] is False
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


@pytest.mark.parametrize(
    "witness",
    [
        f"{PAGE_WITNESS} ",
        f" {PAGE_WITNESS}",
        f"{PAGE_WITNESS[:16]} {PAGE_WITNESS[17:]}",
        f"{PAGE_WITNESS}\n",
    ],
)
def test_vision_smoke_call_refuses_ambiguous_whitespace_in_a_witness_token(
    witness: str,
) -> None:
    """A page token must not depend on preserving ambiguous whitespace glyphs."""

    assert len(witness) >= 32
    with pytest.raises(ValueError, match="must contain no whitespace"):
        VisionSmokeCall(witness)


@pytest.mark.parametrize("witness", ["short", " " * 40, "a" * 129, 1234, None])
def test_vision_smoke_call_refuses_a_non_string_blank_or_out_of_bounds_witness(
    witness: object,
) -> None:
    with pytest.raises(ValueError, match="non-blank string between 32 and 128"):
        VisionSmokeCall(witness)  # type: ignore[arg-type]


@pytest.mark.parametrize("witness", ["\x00" * 32, "\u200b" * 32, "\u0301" * 32, "!" * 32])
def test_vision_smoke_call_refuses_a_witness_that_is_not_a_visible_url_safe_token(
    witness: str,
) -> None:
    with pytest.raises(ValueError, match="visible URL-safe ASCII"):
        VisionSmokeCall(witness)


def test_vision_smoke_call_refuses_a_prompt_that_carries_its_own_witness() -> None:
    """The page-only claim is asserted, not merely true of today's constant prompt."""

    class LeakedWitnessPrompt(VisionSmokeCall):
        @property
        def prompt(self) -> str:
            return f"Reply with PAGE-WITNESS: {self.page_witness}"

    assert PAGE_WITNESS not in vision_smoke().prompt
    with pytest.raises(ValueError, match="occurs in the smoke prompt"):
        LeakedWitnessPrompt(PAGE_WITNESS)


def test_vision_smoke_call_refuses_a_utilization_sampler_that_is_not_callable() -> None:
    with pytest.raises(ValueError, match="utilization sampler must be callable"):
        VisionSmokeCall(PAGE_WITNESS, utilization=())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fixture_bytes",
    [
        b"\xff\xd8\xff\xe0not a png at all",
        b"\x89PNG\r\n\x1a\nnot a complete png",
    ],
)
def test_vision_smoke_call_refuses_bytes_that_are_not_a_complete_decodable_png(
    tmp_path: Path,
    fixture_bytes: bytes,
) -> None:
    """A signature alone must not send corrupt bytes under an image/png declaration."""

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
        outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"},
    )
    fixture = tmp_path / f"{PAGE_WITNESS}.png"
    fixture.write_bytes(fixture_bytes)
    handle = manager.start(chair, TIER)

    with pytest.raises(ServingConfigurationError) as caught:
        vision_smoke()(handle, chair, fixture, smoke_placement())

    assert "PNG" in str(caught.value)
    assert PAGE_WITNESS not in str(caught.value)
    assert handle.fixture_requests_completed == 0
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_refuses_png_geometry_past_the_measured_placement(
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
        outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"},
    )
    fixture = tmp_path / "oversized-golden-page.png"
    Image.new("L", (2_000, 2_000), color="white").save(fixture, format="PNG")
    handle = manager.start(chair, TIER)

    with pytest.raises(ServingConfigurationError, match="past the measured placement"):
        vision_smoke()(handle, chair, fixture, smoke_placement())

    assert handle.fixture_requests_completed == 0
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_bounds_encoded_png_bytes_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"},
    )
    fixture = tmp_path / "golden-page.png"
    fixture_bytes = write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    monkeypatch.setattr(smoke_module, "_MAXIMUM_PNG_BYTES", len(fixture_bytes) - 1)

    with pytest.raises(ServingConfigurationError, match="byte smoke request bound"):
        vision_smoke()(handle, chair, fixture, smoke_placement())

    assert handle.fixture_requests_completed == 0
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_checks_the_format_of_the_sealed_request_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path replacement cannot make non-PNG request bytes inherit a PNG declaration."""

    chair = identity("reader", "reader-v1")
    call = vision_smoke()
    stale_fixture = tmp_path / "before-replacement.png"
    stale_fixture.write_bytes(b"not a PNG")
    stale_calibration = AdapterCalibration.from_image_fixture(
        fixture=stale_fixture,
        prompt=call.prompt,
        mime_type="image/png",
    )
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    monkeypatch.setattr(
        AdapterCalibration,
        "from_image_fixture",
        staticmethod(lambda **unused: stale_calibration),
    )

    with pytest.raises(ServingConfigurationError, match="are not a PNG"):
        call(handle, chair, fixture, smoke_placement())

    assert handle.requests_completed == 0
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_refuses_an_untyped_utilization_sample_tuple(tmp_path: Path) -> None:
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
        outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    call = VisionSmokeCall(PAGE_WITNESS, utilization=lambda: ("71",))  # type: ignore[arg-type]

    with pytest.raises(ServingConfigurationError, match="tuple of UtilizationSample values"):
        call(handle, chair, fixture, smoke_placement())

    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_bounds_utilization_evidence_for_one_request(tmp_path: Path) -> None:
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
        outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    sample = UtilizationSample("71", "31")
    call = VisionSmokeCall(PAGE_WITNESS, utilization=lambda: (sample,) * 1_025)

    with pytest.raises(ServingConfigurationError, match="more than 1024 samples"):
        call(handle, chair, fixture, smoke_placement())

    handle.stop()
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
        gpu_profile=measured_gpu(),
    )

    with pytest.raises(
        ServingConfigurationError, match="without a final completed fixture-bound request"
    ):
        reader.read(chair, fixture, placement)
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
        ServingSmokeReader(manager, text_only, gpu_profile=measured_gpu()).read(
            chair, fixture, placement
        )
    assert launcher.processes[0].terminate_calls == 1


def test_the_plain_reader_seam_gives_the_same_log_root_guarantee_as_the_callback(
    tmp_path: Path,
) -> None:
    """The reader refuses a symlinked log root before anything can launch through it.

    ``prepare_log_root``'s symlink refusal and 0700 chmod used to run only
    inside ``assemble_serving_preflight_callback``'s returned callable, so the
    documented plain seam wrote a run's logs wherever a pre-existing link
    pointed (audit finding F7). The reader now prepares the root before each
    start, on whichever seam assembled it.
    """

    chair = identity("reader", "reader-v1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "logs").symlink_to(elsewhere)
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
        lambda *args: pytest.fail("a refused log root must stop before any launch"),
        gpu_profile=measured_gpu(),
    )
    with pytest.raises(ServingConfigurationError, match="is a symbolic link"):
        reader.read(chair, fixture, placement)
    assert launcher.calls == []


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
        ServingSmokeReader(manager, discarded_fixture_response, gpu_profile=measured_gpu()).read(
            chair, fixture, placement
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
        ServingSmokeReader(manager, wrong_response_token, gpu_profile=measured_gpu()).read(
            chair, fixture, placement
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
        gpu_profile=measured_gpu(),
    )
    with pytest.raises(AdapterActivityError, match="does not match the local golden-page"):
        reader.read(adapter, fixture, placement)
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
        manager,
        lambda *args: pytest.fail("dtype mismatch must not launch"),
        gpu_profile=measured_gpu("float16"),
    )

    with pytest.raises(ServingConfigurationError, match="differs from preflight's measured dtype"):
        reader.read(chair, fixture, placement)
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
    reader = ServingSmokeReader(
        manager, lambda *args: pytest.fail("overage must not launch"), gpu_profile=measured_gpu()
    )

    with pytest.raises(ServingConfigurationError, match="exceeds measured placement limits"):
        reader.read(chair, fixture, placement)
    assert launcher.calls == []


def test_the_placement_pixel_cap_is_a_longest_edge_and_max_pixels_is_a_count(
    tmp_path: Path,
) -> None:
    """The two fields are not in the same unit, and the check must know that.

    Compared directly, every realistic profile is refused for busting a plan it
    comfortably fits: 2359296 > 1792. `_assert_profile_within_placement`'s
    docstring holds the units and where their values were read.
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
    reader = ServingSmokeReader(
        manager, lambda *args: pytest.fail("stop before the launch"), gpu_profile=measured_gpu()
    )
    # It gets past the capacity check and into the lifecycle, which is the point.
    with pytest.raises(pytest.fail.Exception, match=r"^stop before the launch$") as refused:
        reader.read(chair, fixture, placement)
    assert "exceeds measured placement limits" not in str(refused.value)

    manager, _, _, launcher, _, _ = manager_for(
        tmp_path / "over",
        identities={chair.role: chair},
        profiles=(over,),
        model_ids=("reader-api",),
    )
    reader = ServingSmokeReader(
        manager, lambda *args: pytest.fail("overage must not launch"), gpu_profile=measured_gpu()
    )
    with pytest.raises(ServingConfigurationError, match="max_pixels"):
        reader.read(chair, fixture, placement)
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
