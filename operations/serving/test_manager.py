"""Fake-first acceptance drills for the vLLM serving lifecycle.

Every fake has only a loopback-process shape.  No test imports vLLM, starts a
server, fetches a model, or contacts a provider.  The paired red cases are
intentional: an apparently successful service that has not answered, advertised
the wrong ID, or ignored an adapter is more expensive than a visible refusal.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import signal
import stat
import string
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Callable, Iterable, Mapping

import pytest
from PIL import Image, ImageDraw

from common.chair_wire import chat_template_kwargs_for
from common.chairs.config import load_models_toml
from common.chairs.errors import ServingRecipeRefusal, UnresolvedChairRefusal
from common.chairs.models import (
    ChairIdentity,
    ModelsConfig,
    ServingDetails,
)
from common.chairs.receipts import build_receipt
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_bytes
from common.runtree.store import RunTree
from common.sealed_config import parse_sealed_toml, read_sealed_toml
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
    assemble_serving_smoke_reader,
)
from .capacity import CapacityPlan, ChairCapacity
from .client import ServingModeRefusal, serving_mode_for
from .config import (
    MAX_JSON_DEPTH,
    FixtureProfile,
    InProcessProfile,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
    SubprocessProfile,
    UnsupportedProfile,
    chair_preflight_identity_digest,
    load_serving_recipes,
    model_and_tokenizer_pins,
    parse_serving_recipes,
    profile_preflight_digest,
    seal_json_object,
    thawed_json,
    verify_recipes_cover_chairs,
)
from .errors import (
    EndpointOccupiedError,
    ReadinessError,
    ReceiptPublicationError,
    ResidencyError,
    ServiceStopError,
    ServingConfigurationError,
)
from .fakes import FakeLauncher, FakePackages, FakeProcess, FakeRegistry
from .http import (
    EndpointUnavailable,
    HttpResponse,
    assert_image_before_text_on_wire,
    parse_model_ids,
    parse_openai_answer,
    require_exact_model_id,
)
from .manager import (
    _ENDPOINT_ANSWERED_UNREADY,
    _ENDPOINT_REFUSED,
    _ENDPOINT_UNREACHABLE,
    _HYBRID_ATTENTION_REPOSITORIES,
    _WATCHDOG_TAIL_BYTES,
    MECHANICS_QUALIFICATION_PURPOSE,
    PROCESSOR_CONFIG_FILENAMES,
    ReceiptPublication,
    ServiceHandle,
    ServingManager,
    StageContextReceiptPublisher,
    _launchable,
    _progress_log_line,
    _redacted,
    _watchdog_timeout,
    assert_no_discoverable_local_env,
    assert_processor_geometry,
)
from .preflight import (
    ServingSmokeReader,
    assert_generation_config_key_coverage,
    prepare_log_root,
)
from .process import SubprocessLauncher
from .residency import FileResidencyLease
from .smoke import VisionSmokeCall

# One sampling row, sent by the smoke for these tests' chairs and by requests
# that exercise the handle below the smoke.
SAMPLING = {"temperature": 0.0, "top_p": 0.1}
CHAIR_SAMPLING = {
    role: SAMPLING
    for role in (
        "reader",
        "perlector",
        "reconstructor",
        "attestator_1",
        "attestator_2",
        "attestator_3",
    )
}

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
        probe_http_status: int | None = None,
        usage: Mapping[str, object] | None = None,
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
        # A fixed non-200 status every probe answers with, for exercising
        # `_is_deterministic_probe_rejection` without a real engine: `None`
        # (default) keeps every existing test's ordinary 200 answers.
        self.probe_http_status = probe_http_status
        self.usage = dict(usage) if usage is not None else None
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
        if self.probe_http_status is not None:
            return HttpResponse(self.probe_http_status, b"{}")
        if self.bad_response:
            return HttpResponse(200, json.dumps({"model": model_id, "choices": []}).encode())
        response_model = self.response_model or model_id
        if url.endswith("/chat/completions"):
            choice: dict[str, object] = {
                "message": {"content": self.outputs.get(model_id, f"answer:{model_id}")}
            }
        else:
            choice = {"text": self.outputs.get(model_id, f"answer:{model_id}")}
        body: dict[str, object] = {"model": response_model, "choices": [choice]}
        if self.usage is not None:
            body["usage"] = self.usage
        return HttpResponse(200, json.dumps(body).encode())

    def _available(self) -> bool:
        return self.occupied_before_launch or (
            self.process is not None and (self.process.poll() is None or self.sticky_after_stop)
        )


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
        "generation_config": "vllm",
        "preflight_state": "proven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "request_timeout_seconds": 30,
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
    """A real serving row without `preflight_state` is refused by name, never taken as proven."""

    row = profile_row(recipe="reader", chair="perlector", served_model_id="reader", port=8100)
    del row["preflight_state"]

    with pytest.raises(ServingConfigurationError, match="preflight_state"):
        recipes(row)


def test_start_refuses_a_serving_profile_that_is_not_preflight_proven(tmp_path: Path) -> None:
    """A structurally 'unproven' profile must refuse launch, not merely round-trip."""

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


def test_explicit_mechanics_qualification_launches_unproven_profile_and_records_purpose(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["preflight_state"] = "unproven"
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
        launch_purpose=MECHANICS_QUALIFICATION_PURPOSE,
    )

    handle = manager.start(chair, TIER)

    assert manager.launch_purpose == "mechanics-qualification"
    assert handle.launch_audit["launch_purpose"] == "mechanics-qualification"
    assert registry.ensure_calls == ["reader"]
    assert len(publisher.calls) == 1
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


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

    return smoke_module._golden_page_payload(fixture, "Read the supplied proof page.")


PAGE_WITNESS = "ABEFGHJMNRTYabdefghijmnqrty23456789ABEFGHJM"


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
        chair_sampling=CHAIR_SAMPLING,
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
        read_sealed_toml(root / "config/serving_recipes.toml", "recipes")[1],
        read_sealed_toml(root / "config/pod_placement.toml", "placement")[1],
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
    log_tails: tuple[str, ...] = (),
    exits_immediately: int | None = None,
    package_version: str = "0.test",
    package_versions: Mapping[str, str] | None = None,
    publisher_fail: bool = False,
    publisher_receipt_only: bool = False,
    ignore_terminate: bool = False,
    ignore_kill: bool = False,
    residency_lease: FileResidencyLease | None = None,
    probe_http_status: int | None = None,
    usage: Mapping[str, object] | None = None,
    launch_purpose: object | None = None,
    capacity_plan: CapacityPlan | None = None,
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
        probe_http_status=probe_http_status,
        usage=usage,
    )
    launcher = FakeLauncher(
        http,
        log_tail=log_tail,
        log_tails=log_tails,
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
        capacity_plan=capacity_plan,
        placement_table=_PLACEMENT if capacity_plan is not None else None,
        _launch_purpose=launch_purpose,
    )
    return manager, clock, http, launcher, registry, publisher


def reader_manager(tmp_path: Path, *, chair: ChairIdentity, **kwargs):
    return manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        **kwargs,
    )


def _value_after(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_start_proves_exact_model_answer_then_publishes_and_stops(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, registry, publisher = reader_manager(tmp_path, chair=chair)

    handle = manager.start(chair, TIER)

    argv, log_path = launcher.calls[0]
    assert argv[:5] == (
        sys.executable,
        "-m",
        "vllm.entrypoints.cli.main",
        "serve",
        str(tmp_path / "reader"),
    )
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
    measured. The three boolean flags are parametrized because
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
        "vllm.entrypoints.cli.main",
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
        "--enable-prompt-tokens-details",
        "--chat-template-content-format",
        "openai",
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
    manager, _, http, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, bad_response=True
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
    manager, _, _, launcher, _, publisher = reader_manager(tmp_path, chair=chair, health_status=503)

    with pytest.raises(
        ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_HEALTH_UNAVAILABLE.*503"
    ):
        manager.start(chair, TIER)

    assert publisher.calls == []
    assert launcher.processes[0].terminate_calls == 1


def test_an_endpoint_answering_as_a_different_model_never_becomes_ready(tmp_path: Path) -> None:
    """The response's own `model` field is checked, not just the advertised list.

    `/v1/models` can advertise the exact id while a different process answers.
    A receipt naming a model that did not produce the answer is exactly the
    provenance defect that traceability exists to catch.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, response_model="reader-api-shadow"
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
        # What vLLM prints when it rejects an adapter or an architecture
        # loudly rather than ignoring it silently: a model class without LoRA
        # support, and an architecture it does not serve.
        (
            "ValueError: Model architectures ['ExampleForCausalLM'] are not supported for now. "
            "Supported architectures: dict_keys(['Qwen3_5ForConditionalGeneration'])",
            "UNKNOWN_MODEL",
        ),
    ],
)
def test_named_fatal_log_signatures_refuse_and_clean_up(
    tmp_path: Path, signature: str, expected_code: str
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, publisher = reader_manager(
        tmp_path, chair=chair, log_tail=signature
    )

    with pytest.raises(ServingRecipeRefusal, match=expected_code):
        manager.start(chair, TIER)

    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []
    assert expected_code in registry.refusals[0][1]


def _never_answering(manager, launcher, http):  # type: ignore[no-untyped-def]
    """Make every readiness request look like a port that is not open yet.

    The watchdog's whole budget then runs out with `last` naming an
    `EndpointUnavailable`, the shape a still-loading engine and a dead one
    both present.
    """

    class NeverUp:
        def request(
            self, method: str, url: str, *, body: bytes | None, timeout_seconds: float
        ) -> HttpResponse:
            if launcher.processes:
                raise EndpointUnavailable("connection refused", definitively_absent=True)
            return http.request(method, url, body=body, timeout_seconds=timeout_seconds)

    manager.http = NeverUp()


def test_a_watchdog_timeout_while_the_engine_is_loading_says_so_and_carries_the_tail(
    tmp_path: Path,
) -> None:
    """A watchdog timeout during loading says so and carries the log tail.

    "Connection refused" and "still reading 51.7 GiB of weights" call for
    opposite responses -- raise this row's `startup_timeout_seconds`, or go
    find out what is broken -- and the launch log distinguishes them.
    """

    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, registry, publisher = reader_manager(
        tmp_path,
        chair=chair,
        log_tails=(
            "INFO 09-14 11:02:01 [gpu_model_runner.py:2213] Starting to load model reader\n"
            "Loading safetensors checkpoint shards:  21% Completed | 6/28 [01:20<04:56]\n",
            "INFO 09-14 11:02:01 [gpu_model_runner.py:2213] Starting to load model reader\n"
            "Loading safetensors checkpoint shards:  43% Completed | 12/28 [02:41<03:35]\n",
        ),
    )
    _never_answering(manager, launcher, http)

    with pytest.raises(ServingRecipeRefusal) as excinfo:
        manager.start(chair, TIER)

    message = str(excinfo.value)
    assert "VLLM_WATCHDOG_TIMEOUT" in message
    assert "still-loading:" in message
    assert "Loading safetensors checkpoint shards: 43% Completed | 12/28" in message
    assert "Launch log tail:" in message
    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []
    assert "still-loading:" in registry.refusals[0][1]


def test_a_watchdog_timeout_with_no_sign_of_loading_says_connection_refused(
    tmp_path: Path,
) -> None:
    """The other half of the same distinction: nothing in the log says it was loading."""

    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, publisher = reader_manager(
        tmp_path,
        chair=chair,
        log_tail="INFO 09-14 11:02:01 [api_server.py:1] vLLM API server version 0.30.0\n",
    )
    _never_answering(manager, launcher, http)

    with pytest.raises(ServingRecipeRefusal) as excinfo:
        manager.start(chair, TIER)

    message = str(excinfo.value)
    assert "refused:" in message
    assert "vLLM API server version 0.30.0" in message
    assert publisher.calls == []


def test_a_watchdog_timeout_on_an_answering_endpoint_claims_neither(tmp_path: Path) -> None:
    """A 503 from `/health` is the engine answering; it is not a refused connection.

    The last readiness answer still leads the message, on its first line.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, health_status=503, log_tail="INFO: nothing interesting here\n"
    )

    with pytest.raises(
        ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_HEALTH_UNAVAILABLE.*503"
    ) as excinfo:
        manager.start(chair, TIER)

    assert "answered-unready:" in str(excinfo.value)
    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []


def test_a_watchdog_timeout_over_an_unreadable_log_refuses_to_guess() -> None:
    """The log stopped being readable between the poll's read and the refusal's.

    Rare, and the honest answer is that this message cannot tell which failure
    it is looking at -- not a silent fallback to either.
    """

    process = FakeProcess(
        4242,
        log_tail="VLLM_LOG_UNREADABLE: could not read launch log /private/child.log: denied",
    )

    error = _watchdog_timeout(
        process,
        last="loopback endpoint unavailable: connection refused",
        endpoint_state=_ENDPOINT_REFUSED,
        budget_seconds=300.0,
    )

    assert error.code == "VLLM_WATCHDOG_TIMEOUT"
    assert "log-unreadable:" in error.detail
    assert "denied" in error.detail


def test_an_empty_launch_log_says_it_is_empty_rather_than_appending_nothing() -> None:
    error = _watchdog_timeout(
        FakeProcess(4242, log_tail="   \n"),
        last="loopback endpoint unavailable: connection refused",
        endpoint_state=_ENDPOINT_REFUSED,
        budget_seconds=300.0,
    )

    assert error.detail.endswith("The launch log is empty")


def test_the_progress_line_is_the_most_recent_one_and_arrives_as_one_line() -> None:
    """The refusal quotes evidence, so it quotes the newest line and keeps it on one line.

    One line matters mechanically: the diagnosis sits ahead of the log tail
    precisely so that a reader (and the `.*` regexes pinned above, which cannot
    cross a newline) finds it and the last readiness answer together.
    """

    tail = (
        "Loading safetensors checkpoint shards:  10% Completed | 3/28\n"
        "Loading safetensors checkpoint shards:  99% Completed | 27/28\n"
        "INFO   Capturing CUDA graph shapes:\t 40%|####      | 26/66\n"
        "INFO: an ordinary line that names nothing\n"
    )

    assert _progress_log_line(tail) == "INFO Capturing CUDA graph shapes: 40%|#### | 26/66"
    assert _progress_log_line("INFO: nothing in here at all\n") is None


def test_a_long_launch_log_is_carried_as_a_bounded_and_labelled_tail() -> None:
    """A refusal travels through journals and notifications; a 16 KiB log does not."""

    error = _watchdog_timeout(
        FakeProcess(
            4242,
            log_tail="x" * 9_000 + "\nLoading weights took 412.10 seconds\n",
        ),
        last="loopback endpoint unavailable: connection refused",
        endpoint_state=_ENDPOINT_REFUSED,
        progress_advanced=True,
        budget_seconds=300.0,
    )

    assert "still-loading:" in error.detail
    assert f"[last {_WATCHDOG_TAIL_BYTES} bytes]" in error.detail
    assert len(error.detail) < 2_500


def test_a_loading_marker_that_never_moved_is_not_reported_as_current_progress(
    tmp_path: Path,
) -> None:
    """An early marker left in a retained log is not evidence of progress now."""

    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, publisher = reader_manager(
        tmp_path,
        chair=chair,
        log_tail="Loading safetensors checkpoint shards:  43% Completed | 12/28 [02:41<03:35]\n",
    )
    _never_answering(manager, launcher, http)

    with pytest.raises(ServingRecipeRefusal) as excinfo:
        manager.start(chair, TIER)

    message = str(excinfo.value)
    # Not the claim an operator raises the bound on.
    assert "stalled:" in message
    assert "still-loading" not in message
    # The evidence itself still travels: the claim is narrowed, not dropped.
    assert "Loading safetensors checkpoint shards: 43% Completed | 12/28" in message
    assert publisher.calls == []


def test_an_endpoint_that_timed_out_is_not_reported_as_a_refused_connection() -> None:
    """`EndpointUnavailable` is not a refusal unless it says it proved absence.

    A timeout, a reset and a malformed local route all raise it with
    `definitively_absent` false, and every one of them was recorded as a
    refused connection -- so a start that timed out was told that raising
    `startup_timeout_seconds` was unlikely to help, which is the opposite of
    the truth.
    """

    process = FakeProcess(4242, log_tail="INFO: nothing interesting here\n")

    timed_out = _watchdog_timeout(
        process,
        last="loopback endpoint unavailable: read timed out",
        endpoint_state=_ENDPOINT_UNREACHABLE,
        budget_seconds=300.0,
    )
    refused = _watchdog_timeout(
        process,
        last="loopback endpoint unavailable: connection refused",
        endpoint_state=_ENDPOINT_REFUSED,
        budget_seconds=300.0,
    )
    answered = _watchdog_timeout(
        process,
        last="VLLM_HEALTH_UNAVAILABLE: /health returned HTTP 503",
        endpoint_state=_ENDPOINT_ANSWERED_UNREADY,
        budget_seconds=300.0,
    )
    nothing = _watchdog_timeout(
        process,
        last="service did not become ready",
        endpoint_state=None,
        budget_seconds=1.0,
    )

    for error, code in (
        (timed_out, "unreachable"),
        (refused, "refused"),
        (answered, "answered-unready"),
        (nothing, "no-probe"),
    ):
        assert f" -- {code}: " in error.detail


def test_the_watchdog_tail_is_bounded_in_bytes_not_in_characters() -> None:
    """The limit is spent in journals, pod reports and notifications, which carry bytes.

    Slicing the string counted characters, so a non-ASCII tail travelled at
    several times the documented size.
    """

    error = _watchdog_timeout(
        FakeProcess(4242, log_tail="\u00e9" * 4_000),
        last="loopback endpoint unavailable: connection refused",
        endpoint_state=_ENDPOINT_REFUSED,
        budget_seconds=300.0,
    )

    label = f"[last {_WATCHDOG_TAIL_BYTES} bytes] "
    assert label in error.detail
    excerpt = error.detail.split(label, 1)[1]
    assert len(excerpt.encode("utf-8")) <= _WATCHDOG_TAIL_BYTES
    # The characters slice this replaced would have carried 1,200 two-byte
    # characters, which is twice the documented limit.
    assert len(excerpt) < 1_200


def test_a_credential_inside_a_structured_log_field_is_redacted() -> None:
    """A log line is not an argv, and a secret in it is rarely a bare token."""

    # Composed rather than written out: a credential-shaped literal in a source
    # file is refused by this repository's own ingress scan, which is the same
    # rule seen from the other side. The prefix comes from the shared list the
    # detector reads, so a prefix added there is exercised here too.
    from common.credentials import CREDENTIAL_VALUE_PREFIXES, looks_like_credential_value

    shaped = CREDENTIAL_VALUE_PREFIXES[1] + string.ascii_lowercase + "012345"
    assert shaped.startswith(CREDENTIAL_VALUE_PREFIXES)
    assert looks_like_credential_value(shaped)
    jwt = ".".join(
        base64.urlsafe_b64encode(part).decode().rstrip("=")
        for part in (b'{"alg":"HS256"}', b'{"sub":"1"}', b"signature-bytes-here")
    )

    assert _redacted(f"INFO start token={shaped} ok") == "INFO start token=[redacted] ok"
    assert _redacted(f'INFO {{"token":"{jwt}"}} sent') == 'INFO {"token":"[redacted]"} sent'
    assert (
        _redacted(f"GET / Authorization: Bearer {jwt}") == "GET / Authorization: Bearer [redacted]"
    )
    assert _redacted(f"field\t{shaped}\tnext") == "field\t[redacted]\tnext"
    assert _redacted(f"api_key = {shaped}") == "api_key = [redacted]"
    # And the lines a reader is meant to recognise are left exactly as they are.
    progress = "Loading safetensors checkpoint shards:  43% Completed | 12/28 [02:41<03:35]"
    assert _redacted(progress) == progress
    commit = "checked out a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
    assert _redacted(commit) == commit
    assert _redacted("weights at /workspace/models/model.safetensors") == (
        "weights at /workspace/models/model.safetensors"
    )


def test_an_unreadable_launch_log_is_a_named_readiness_refusal(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, publisher = reader_manager(
        tmp_path,
        chair=chair,
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
    """A launch log naming ``RuntimeError``/``ValueError`` alone is not fatal.

    Without one of the five named fatal substrings, it must reach a normal
    successful start; ``_fatal_log_signature``'s docstring holds the reasoning.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, _, registry, publisher = reader_manager(
        tmp_path,
        chair=chair,
        log_tail="INFO: warming up\n"
        "RuntimeError: a transient benign message unrelated to any fatal condition\n"
        "ValueError: also benign, also not one of the named signatures\n",
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
    manager, _, _, _, registry, publisher = reader_manager(
        tmp_path,
        chair=chair,
        log_tail="WARNING 08-09 12:00:00 [__init__.py:32] Failed to import from vllm._C\n"
        "Traceback (most recent call last):\n"
        '  File "vllm/_C.py", line 1, in <module>\n'
        "ModuleNotFoundError: no flashinfer\n"
        "INFO: continuing\n",
    )

    handle = manager.start(chair, TIER)

    assert registry.refusals == []
    assert len(publisher.calls) == 1
    handle.stop()


def test_unsupported_architecture_prose_without_vllms_form_does_not_abort_a_start(
    tmp_path: Path,
) -> None:
    """Only vLLM's own registry refusal is fatal, not the words."""

    chair = identity("reader", "reader-v1")
    manager, _, _, _, registry, publisher = reader_manager(
        tmp_path,
        chair=chair,
        log_tail="INFO: some architectures are not supported for now, continuing anyway\n",
    )

    handle = manager.start(chair, TIER)

    assert registry.refusals == []
    assert len(publisher.calls) == 1
    handle.stop()


def test_process_exit_before_readiness_has_its_own_named_refusal(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, exits_immediately=23
    )

    with pytest.raises(ServingRecipeRefusal, match="VLLM_PROCESS_EXITED.*23"):
        manager.start(chair, TIER)

    # The exited child's process group is still signalled: another member may
    # outlive it.
    assert launcher.processes[0].terminate_calls == 1
    assert launcher.processes[0].poll() == 23
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
    Python -- a measurement of something nobody ran.
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=requested, package_version="different"
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
    manager, _, _, launcher, registry, publisher = reader_manager(
        tmp_path, chair=chair, publisher_fail=True
    )

    with pytest.raises(ServingRecipeRefusal, match="SERVING_RECEIPT_PUBLICATION_FAILED"):
        manager.start(chair, TIER)

    assert len(registry.receipts) == 1
    assert len(publisher.calls) == 1
    assert launcher.processes[0].terminate_calls == 1


def test_receipt_publication_requires_a_durable_launch_audit_reference(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, publisher_receipt_only=True
    )

    with pytest.raises(ServingRecipeRefusal, match="durable launch-audit, and combined evidence"):
        manager.start(chair, TIER)
    assert len(publisher.calls) == 1
    assert launcher.processes[0].terminate_calls == 1


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
    manager, _, http, launcher, registry, publisher = reader_manager(
        tmp_path, chair=chair, publisher_fail=True, ignore_terminate=True, ignore_kill=True
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
    manager.recover()
    publisher.fail = False
    launcher.ignore_terminate = False
    launcher.ignore_kill = False
    handle = manager.start(chair, TIER)
    handle.stop()
    assert http.inference_calls >= 2


def test_unexpected_start_failure_names_its_exception_type_in_the_refusal(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, _ = reader_manager(tmp_path, chair=chair)

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
    manager, _, _, launcher, _, publisher = reader_manager(tmp_path, chair=chair)

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

    ``_stop_process`` checks ``process.poll()`` after its signalling ``try``, so
    this arrives at the wrapping handlers exactly as raised. ``str()`` of an
    exception constructed without arguments is the empty string, which is what
    makes a type-less wrapper visible.
    """

    pid = 4242

    def poll(self) -> int | None:
        raise RuntimeError()

    def terminate(self) -> None:
        pass

    def kill(self) -> None:  # pragma: no cover - the group is already empty
        pass

    def wait(self, timeout_seconds: float) -> int:
        del timeout_seconds
        return 0

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
    anywhere.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = reader_manager(tmp_path / "stop", chair=chair)
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

    cleanup_manager, _, _, _, cleanup_registry, _ = reader_manager(
        tmp_path / "cleanup", chair=chair
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
    manager, _, _, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, ignore_terminate=True, ignore_kill=True
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
    manager, _, http, launcher, _, _ = reader_manager(tmp_path, chair=chair, sticky_after_stop=True)
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
    manager, clock, http, launcher, registry, _ = reader_manager(tmp_path, chair=chair)
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, ambiguous_before_launch=True
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, residency_lease=ReleasedHandleLease()
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


def _proc_status_is_live(read_status: Callable[[], str]) -> bool:
    """Classify one `/proc` status sample, including disappearance during its read."""
    try:
        status = read_status()
    except OSError as error:
        if error.errno in {errno.ENOENT, errno.ESRCH}:
            return False
        raise
    return "(zombie)" not in status


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
            # The grandchild's true parent exits under the same killpg. The
            # host may leave the grandchild observable briefly as a zombie or
            # reap it before this sample. Plain os.kill(pid, 0) treats a zombie
            # as present, so /proc's state distinguishes the first case while
            # the two disappearance errnos distinguish the second.
            return _proc_status_is_live(Path(f"/proc/{grandchild_pid}/status").read_text)

        assert _grandchild_alive(), "grandchild must be running before terminate is asserted"
        process.terminate()
        process.wait(5)
        _wait_until(lambda: not _grandchild_alive())
    finally:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="distinguishing a running grandchild from a zombie needs /proc",
)
def test_cleanup_after_the_direct_child_exited_stops_its_group_before_releasing_the_lease(
    tmp_path: Path,
) -> None:
    """A group member that outlives the direct child is stopped before the lease goes.

    vLLM's engine process holds the GPU memory and can outlive the API server
    this manager launched. Cleanup must still reach it, and must not hand the
    card to the next start while it runs.
    """

    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(sys.argv[1], 'w') as handle:\n"
        "    handle.write(str(grandchild.pid))\n"
    )
    process = SubprocessLauncher().launch(
        (sys.executable, "-c", script, str(pidfile)),
        tmp_path / "child.log",
    )
    grandchild_pid = 0
    try:
        _wait_until(lambda: process.poll() is not None)
        grandchild_pid = int(pidfile.read_text())

        def _grandchild_alive() -> bool:
            return _proc_status_is_live(Path(f"/proc/{grandchild_pid}/status").read_text)

        assert _grandchild_alive(), "the grandchild must outlive the direct child"

        released_while_alive: list[bool] = []

        class RecordingHandle:
            def inheritable_fd(self) -> int:  # pragma: no cover - not launched here
                raise AssertionError("cleanup does not launch")

            def release(self) -> None:
                released_while_alive.append(_grandchild_alive())

        chair = identity("reader", "reader-v1")
        manager, _, _, _, _, _ = reader_manager(tmp_path / "manager", chair=chair)
        manager._residency_handle = RecordingHandle()  # type: ignore[assignment]

        assert manager._attempt_cleanup(process, "") is None
        assert released_while_alive == [False]
        assert not _grandchild_alive()
    finally:
        if grandchild_pid:
            with suppress(ProcessLookupError):
                os.kill(grandchild_pid, signal.SIGKILL)


def test_a_group_seen_empty_after_the_leader_is_reaped_is_never_signalled_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel may reuse an empty group's id; a later stop must not reach it."""

    process = SubprocessLauncher().launch((sys.executable, "-c", "pass"), tmp_path / "child.log")
    assert process.wait(3) == 0

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    process.terminate()
    process.kill()

    assert signalled == []


def test_a_failed_signal_after_the_leader_is_reaped_ends_group_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = SubprocessLauncher().launch((sys.executable, "-c", "pass"), tmp_path / "child.log")
    _wait_until(lambda: process.poll() is not None)

    calls: list[int] = []

    def _missing(pgid: int, sig: int) -> None:
        calls.append(sig)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", _missing)
    process.terminate()
    process.kill()

    assert calls == [signal.SIGTERM]


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
    manager, _, _, _, _, _ = reader_manager(tmp_path, chair=chair)

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
    raw["max_lora_rank"] = 16
    with pytest.raises(ServingConfigurationError, match="unknown field"):
        recipes(raw)

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


def test_real_catalogue_covers_each_chair_and_names_unservable_tiers():
    """The real roster is opt-in, and every configured chair has a row.

    Every configured chair has a row at every tier, `attestator_1` included:
    Chandra is served and read in the Attestatores' own call rather than
    reusing the Designator's reading. `secondary_proposer`, DAI's own record
    detector, is an in-process row the Designator runs on the CPU, never a
    launched engine. The smaller Perlector tiers name their measured refusal;
    live rows remain unproven on real silicon.
    """

    root = Path(__file__).resolve().parents[2]
    placement = load_placement_table(root / "config/pod_placement.toml")
    tiers = tuple(tier.identifier for tier in placement.tiers)
    fixture_models = load_models_toml(root / "config/models.toml")
    fixture_catalogue = load_serving_recipes(root / "config/serving_recipes.toml")
    real_models = load_models_toml(root / "config/models-real.toml")
    real_catalogue = load_serving_recipes(root / "config/serving_recipes_real.toml")

    # Each opt-in catalogue must reconcile only with its paired roster; real
    # coverage cannot weaken or silently replace the fixture default.
    verify_recipes_cover_chairs(fixture_models, fixture_catalogue, tiers)
    verify_recipes_cover_chairs(real_models, real_catalogue, tiers)
    configured = [
        value for value in real_models.chairs.values() if isinstance(value, ChairIdentity)
    ]
    assert {identity.role for identity in configured} == {
        "secondary_proposer",
        "attestator_1",
        "attestator_2",
        "attestator_3",
        "perlector",
        "designator_surya",
        "reconstructor",
    }
    assert len(real_catalogue.profiles) == len(configured) * len(tiers)
    for identity in configured:
        for tier in tiers:
            profile = real_catalogue.for_identity(identity, tier)
            if identity.role in ("perlector", "reconstructor") and tier != "generic-80gb-plus":
                assert isinstance(profile, UnsupportedProfile)
                with pytest.raises(ServingModeRefusal, match="51.7 GiB"):
                    serving_mode_for(real_catalogue, identity, tier)
                continue
            if identity.role == "secondary_proposer":
                assert isinstance(profile, InProcessProfile)
                assert (profile.engine, profile.device, profile.imgsz) == (
                    "ultralytics",
                    "cpu",
                    1024,
                )
                assert serving_mode_for(real_catalogue, identity, tier) == "in-process"
                continue
            if identity.role == "designator_surya":
                assert isinstance(profile, SubprocessProfile)
                assert (profile.engine, profile.device, profile.environment) == (
                    "surya",
                    "cpu",
                    "operations/serving/surya",
                )
                continue
            assert isinstance(profile, ServingProfile)
            assert profile.preflight_state == "unproven"
            assert profile.required_packages["vllm"] == "0.30.0"
            if identity.repo in _HYBRID_ATTENTION_REPOSITORIES:
                assert profile.enable_prefix_caching is False, (identity.role, tier)
            assert serving_mode_for(real_catalogue, identity, tier) == "live"
    # A hybrid repository missing from the real roster would make the check
    # above pass without checking anything. The FP8 and NVFP4 builds are served
    # only by config/serving_recipes_real_variants.toml so far, whose rows
    # test_serving_variants.py holds to prefix caching off.
    assert _HYBRID_ATTENTION_REPOSITORIES - {
        "Qwen/Qwen3.8-27B-FP8",
        "nvidia/Qwen3.8-27B-NVFP4",
    } <= {identity.repo for identity in configured}


def test_an_in_process_row_is_never_launched_as_a_server() -> None:
    """The Designator runs the detector itself; the manager refuses to start it."""

    chair = identity("secondary_proposer", "unproven-real-secondary-proposer")
    row = {
        "kind": "in-process",
        "recipe": chair.serving_recipe,
        "chair": chair.role,
        "tier": TIER,
        "engine": "ultralytics",
        "task": "obb",
        "device": "cpu",
        "imgsz": 1024,
        "conf_bp": 2500,
        "iou_bp": 7000,
        "max_det": 300,
        "required_packages": {"ultralytics": "8.4.14", "torch": "2.13.0"},
    }
    catalogue = parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]})
    with pytest.raises(ServingConfigurationError, match="no serving process is ever started"):
        _launchable(catalogue.for_identity(chair, TIER), chair)
    for field, value, refusal in (
        ("device", "cuda", "device must be one of"),
        ("conf_bp", 10_001, "at most 10000"),
        ("required_packages", {"ultralytics": "8.4.14"}, "pins exactly"),
    ):
        with pytest.raises(ServingConfigurationError, match=refusal):
            parse_serving_recipes(
                {"schema": "serving-recipes.v1", "profiles": [{**row, field: value}]}
            )


def test_unsupported_real_profile_refuses_by_cause_before_a_process_starts(
    tmp_path: Path,
) -> None:
    chair = identity("secondary_proposer", "unproven-real-secondary-proposer")
    row = {
        "kind": "unsupported",
        "recipe": chair.serving_recipe,
        "chair": chair.role,
        "tier": TIER,
        "reason": "native Ultralytics object detection serving is not implemented",
    }
    manager, _, _, launcher, registry, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=(),
    )

    with pytest.raises(ServingRecipeRefusal, match="native Ultralytics object detection"):
        manager.start(chair, TIER)

    assert launcher.processes == []
    assert publisher.calls == []
    # The claim is that the row is refused *before* the snapshot, not merely
    # that nothing launched. Without this the refusal could move after
    # `registry.ensure` and the test would stay green, while an operator saw a
    # checkpoint or pin failure instead of the unimplemented-serving cause.
    assert registry.ensure_calls == [], "the refusal reached the snapshot before naming its cause"
    assert "no serving process was started" in registry.refusals[0][1]


def test_real_catalogue_missing_row_refusal_names_the_exact_chair_and_tier() -> None:
    """The opt-in catalogue fails closed with an operator-actionable row name."""

    root = Path(__file__).resolve().parents[2]
    placement = load_placement_table(root / "config/pod_placement.toml")
    tiers = tuple(tier.identifier for tier in placement.tiers)
    models = load_models_toml(root / "config/models-real.toml")
    complete = load_serving_recipes(root / "config/serving_recipes_real.toml")
    removed = complete.profiles[0]
    incomplete = ServingRecipes(profiles=complete.profiles[1:])

    with pytest.raises(ServingConfigurationError) as refusal:
        verify_recipes_cover_chairs(models, incomplete, tiers)

    detail = str(refusal.value)
    assert "missing=" in detail
    assert removed.recipe in detail
    assert removed.chair in detail
    assert removed.tier in detail
    # A refusal that dumps the whole catalogue satisfies every check above,
    # because the removed row is a member of it. What an operator needs is the
    # one row to add, so a retained row must NOT appear.
    retained = next(profile for profile in incomplete.profiles if profile.chair != removed.chair)
    assert retained.chair not in detail


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
        (root / "config/serving_recipes.toml")
        .read_bytes()
        .replace(b"offline walking-skeleton", b"altered walking-skeleton", 1)
    )
    copied_placement.write_bytes(
        (root / "config/pod_placement.toml")
        .read_bytes()
        .replace(b"batch_size = 1\n", b"batch_size = 3\n", 1)
    )

    with pytest.raises(ServingConfigurationError, match="serving recipes differ"):
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
    with pytest.raises(ServingConfigurationError, match="pod placement differs"):
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
    altered = sealed.replace(b"batch_size = 1\n", b"batch_size = 3\n", 1)
    assert altered != sealed, "the fixture no longer contains the batch size this test flips"

    path = tmp_path / "pod_placement.toml"
    path.write_bytes(altered)

    from_bytes = load_placement_table(path, source_bytes=sealed)
    from_path = load_placement_table(path)

    assert from_bytes.choose(Decimal(24)).recipe.batch_size == 1
    assert from_path.choose(Decimal(24)).recipe.batch_size == 3


def test_bound_configuration_parses_the_snapshot_it_digested_not_a_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed-configuration interlock, against the substitution that beat it."""

    root = Path(__file__).resolve().parents[2]
    recipes_path = root / "config/serving_recipes.toml"
    sealed = (root / "config/pod_placement.toml").read_bytes()
    altered = sealed.replace(b"batch_size = 1\n", b"batch_size = 3\n", 1)
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
            read_sealed_toml(recipes_path, "recipes")[1],
            parse_sealed_toml(sealed, "placement")[1],
        ).to_record(),
        recipes_path=recipes_path,
        placement_path=placement_path,
    )

    assert len(reads) == 1, f"the placement file was read {len(reads)} times, not once"
    assert placement.choose(Decimal(24)).recipe.batch_size == 1


def test_bound_configuration_refuses_an_unusable_placement_in_the_serving_vocabulary(
    tmp_path: Path,
) -> None:
    """Every way the placement file can fail refuses as a `ServingError`."""

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


@pytest.mark.hostile_local
def test_prepare_log_root_refuses_a_symlink_rather_than_re_moding_its_target(
    tmp_path: Path,
) -> None:
    """A link pointing at a real directory is the one symlink `mkdir` forgives.

    `mkdir(exist_ok=True)` refuses a file, a symlink to a file and a broken
    symlink. It accepts a symlink to a directory, and the `chmod` that follows
    then re-modes the target — so the run's logs land somewhere it never named,
    under a mode set on a directory it does not own.
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
        copied_recipes.read_bytes().replace(b"offline walking-skeleton", b"another catalogue", 1)
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
        copied_placement.read_bytes().replace(b"batch_size = 1\n", b"batch_size = 3\n", 1)
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
    """The run-sealed-configuration interlock, joined rather than traced by hand."""

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
    manager, _, http, launcher, _, _ = reader_manager(tmp_path, chair=chair)
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
            sampling=SAMPLING,
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
    assert result.receipt["serving_launch_audit"]["schema"] == "serving-launch-audit.v2"  # type: ignore[index]
    assert result.receipt["serving_launch_audit_reference"]["sha256"] == "d" * 64  # type: ignore[index]
    assert result.receipt["serving_evidence_reference"]["sha256"] == "e" * 64  # type: ignore[index]
    assert (
        result.receipt["supplied_fixture_sha256"]
        == hashlib.sha256(fixture.read_bytes()).hexdigest()
    )
    assert result.receipt["smoke_fixture_request_count"] == 1
    assert (
        result.receipt["smoke_fixture_response_sha256"] == result.receipt["fixture_response_sha256"]
    )
    assert isinstance(result.receipt["smoke_fixture_output_sha256"], str)
    assert http.inference_calls >= 2  # readiness proof plus the golden-page call
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_refuses_an_unstarted_service_handle(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = reader_manager(tmp_path, chair=chair)
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": prompt_only_answer}
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
    manager, _, http, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": expected}
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
    assert result.receipt["page_witness_edit_distance"] == 0
    request = http.calls[-1][2]
    assert isinstance(request, dict)
    assert "chat_template_kwargs" not in request
    messages = request["messages"]
    assert isinstance(messages, list)
    image_url = messages[0]["content"][0]["image_url"]["url"]  # type: ignore[index]
    assert isinstance(image_url, str)
    assert image_url == "data:image/png;base64," + base64.b64encode(fixture_bytes).decode("ascii")
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_ignores_whitespace_inside_the_page_witness(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    spaced_witness = f"{PAGE_WITNESS[:8]} \t{PAGE_WITNESS[8:20]}\n{PAGE_WITNESS[20:]}"
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": f"PAGE-WITNESS: {spaced_witness}"},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert result.shape_valid is True
    assert result.format_valid is True
    assert result.receipt["page_witness_matches"] is True
    assert result.receipt["page_witness_edit_distance"] == 0
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_receipt_retains_near_read_distance(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS[:-2]}"}
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    assert result.format_valid is True
    assert result.receipt["page_witness_matches"] is True
    assert result.receipt["page_witness_edit_distance"] == 2
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


@pytest.mark.parametrize(
    "answer",
    [
        f"PAGE-WITNESS: {PAGE_WITNESS[:-3]}CCC",
        PAGE_WITNESS,
    ],
    ids=("three-character-errors", "missing-marker"),
)
def test_vision_smoke_call_refuses_three_code_errors_or_missing_marker(
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
    assert result.format_valid is False
    assert result.receipt["page_witness_matches"] is False
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


@pytest.mark.parametrize(
    "role", ["perlector", "reconstructor", "attestator_1", "attestator_2", "attestator_3"]
)
def test_the_smoke_sends_the_template_switch_the_chair_s_run_calls_send(
    tmp_path: Path, role: str
) -> None:
    # A local-repository checkpoint names no Hub repository; the switch follows
    # the chair, exactly as the run builders read it.
    chair = replace(
        identity(role, f"{role}-v1"),
        source="local-repository",
        repo=None,
        path=role,
        revision=None,
    )
    expected = f"PAGE-WITNESS: {PAGE_WITNESS}"
    answer_with_reasoning = f"I read the page.\n{expected}"
    manager, _, http, _, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe=chair.serving_recipe,
                chair=chair.role,
                served_model_id="reader-api",
                port=8000,
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": answer_with_reasoning},
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)

    result = vision_smoke()(handle, chair, fixture, smoke_placement())

    request = http.calls[-1][2]
    assert isinstance(request, dict)
    assert request.get("chat_template_kwargs") == chat_template_kwargs_for(role)
    # The direct-answer switch never relaxes the exact output rule.
    assert result.format_valid is False
    handle.stop()


def test_vision_smoke_call_reports_multiple_nonempty_choices_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chair = identity("reader", "reader-v1")
    expected = f"PAGE-WITNESS: {PAGE_WITNESS}"
    manager, _, http, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": expected}
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": expected}
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
        f"page-witness: {PAGE_WITNESS}",
        f"PAGE-WITNESS:  {PAGE_WITNESS}",
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": answer}
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


def test_vision_smoke_retains_exact_exchange_for_a_parsed_format_invalid_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chair = identity("reader", "reader-v1")
    invalid_answer = f"PAGE-WITNESS: {PAGE_WITNESS} "
    manager, _, http, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": invalid_answer}
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    retained: dict[str, bytes] = {}
    wire: dict[str, bytes] = {}
    original_request = http.request

    def capture_wire(
        method: str,
        url: str,
        *,
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        response = original_request(method, url, body=body, timeout_seconds=timeout_seconds)
        if method == "POST" and url.endswith("/chat/completions"):
            assert body is not None
            wire["request"] = body
            wire["response"] = response.body
        return response

    monkeypatch.setattr(http, "request", capture_wire)

    def publish(request: bytes, response: bytes) -> tuple[dict[str, str], dict[str, str]]:
        retained.update(request=request, response=response)
        return (
            {
                "relative_path": "smoke-requests/sha256/request.json",
                "sha256": hashlib.sha256(request).hexdigest(),
            },
            {
                "relative_path": "smoke-responses/sha256/response.bin",
                "sha256": hashlib.sha256(response).hexdigest(),
            },
        )

    handle = manager.start(chair, TIER)
    result = VisionSmokeCall(
        PAGE_WITNESS,
        utilization=lambda: (UtilizationSample("71", "31"),),
        chair_sampling=CHAIR_SAMPLING,
        raw_exchange_publisher=publish,
    )(handle, chair, fixture, smoke_placement())

    assert result.format_valid is False
    assert retained == wire
    # The smoke reads the page under the chair's sealed row and the profile seed.
    sent = json.loads(wire["request"])
    assert {key: sent[key] for key in SAMPLING} == SAMPLING
    assert sent["seed"] == handle.profile.seed
    assert (
        result.receipt["smoke_request_reference"]["sha256"]
        == hashlib.sha256(retained["request"]).hexdigest()
    )
    assert (
        result.receipt["smoke_response_reference"]["sha256"]
        == hashlib.sha256(retained["response"]).hexdigest()
    )
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_refuses_a_chair_without_a_sealed_sampling_row(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = reader_manager(tmp_path, chair=chair)
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    with pytest.raises(ServingConfigurationError, match="no sealed sampling values for reader"):
        VisionSmokeCall(PAGE_WITNESS)(handle, chair, fixture, smoke_placement())
    assert handle.fixture_requests_completed == 0
    handle.stop()


def test_vision_smoke_parser_failure_names_retained_exchange_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, _ = reader_manager(tmp_path, chair=chair)
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    retained: dict[str, bytes] = {}
    original_request = http.request

    def malformed_fixture_response(
        method: str,
        url: str,
        *,
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        response = original_request(method, url, body=body, timeout_seconds=timeout_seconds)
        if method == "POST" and url.endswith("/chat/completions"):
            return HttpResponse(200, b'{"model":"reader-api","choices":[]}')
        return response

    def publish(request: bytes, response: bytes) -> tuple[dict[str, str], dict[str, str]]:
        retained.update(request=request, response=response)
        return (
            {"relative_path": "smoke-requests/sha256/request.json", "sha256": "a" * 64},
            {"relative_path": "smoke-responses/sha256/response.bin", "sha256": "b" * 64},
        )

    handle = manager.start(chair, TIER)
    monkeypatch.setattr(http, "request", malformed_fixture_response)
    call = VisionSmokeCall(
        PAGE_WITNESS,
        utilization=lambda: (UtilizationSample("71", "31"),),
        chair_sampling=CHAIR_SAMPLING,
        raw_exchange_publisher=publish,
    )
    with pytest.raises(smoke_module.SmokeExchangeRetainedError) as caught:
        call(handle, chair, fixture, smoke_placement())

    error = caught.value
    assert error.code == "VLLM_PROBE_RESPONSE_INVALID"
    assert isinstance(error.__cause__, ReadinessError)
    assert error.__cause__.code == "VLLM_PROBE_RESPONSE_INVALID"
    assert "smoke-requests/sha256/request.json" in error.detail
    assert "smoke-responses/sha256/response.bin" in error.detail
    assert retained["response"] == b'{"model":"reader-api","choices":[]}'
    assert retained["request"]
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

    with pytest.raises(ServingConfigurationError, match="generator alphabet"):
        VisionSmokeCall(witness)


@pytest.mark.parametrize("witness", ["short", " " * 40, "a" * 44, 1234, None])
def test_vision_smoke_call_refuses_a_non_string_or_wrong_length_witness(
    witness: object,
) -> None:
    with pytest.raises(ServingConfigurationError, match="43 characters"):
        VisionSmokeCall(witness)  # type: ignore[arg-type]


@pytest.mark.parametrize("witness", ["\x00" * 32, "\u200b" * 32, "\u0301" * 32, "!" * 32])
def test_vision_smoke_call_refuses_a_witness_outside_the_generator_alphabet(
    witness: str,
) -> None:
    with pytest.raises(ServingConfigurationError, match="generator alphabet"):
        VisionSmokeCall(witness)


def test_vision_smoke_call_refuses_adjacent_repeated_witness_characters() -> None:
    witness = PAGE_WITNESS[0] * 2 + PAGE_WITNESS[2:]

    with pytest.raises(ServingConfigurationError, match="adjacent repeats"):
        VisionSmokeCall(witness)


def test_vision_smoke_call_refuses_a_prompt_that_carries_its_own_witness() -> None:
    """The page-only claim is asserted, not merely true of today's constant prompt."""

    class LeakedWitnessPrompt(VisionSmokeCall):
        @property
        def prompt(self) -> str:
            return f"Reply with PAGE-WITNESS: {self.page_witness}"

    assert PAGE_WITNESS not in vision_smoke().prompt
    with pytest.raises(ServingConfigurationError, match="occurs in the smoke prompt"):
        LeakedWitnessPrompt(PAGE_WITNESS)


def test_vision_smoke_call_refuses_a_utilization_sampler_that_is_not_callable() -> None:
    with pytest.raises(ServingConfigurationError, match="utilization sampler must be callable"):
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"}
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"}
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
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"}
    )
    fixture = tmp_path / "golden-page.png"
    fixture_bytes = write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    # The refusal below is driven by a lowered bound, so pin the real one too:
    # the serving README states 64 MiB as a fact about this path, and without
    # this the constant could move to any value with the suite still green.
    assert smoke_module._MAXIMUM_PNG_BYTES == 64 * 1024 * 1024
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
    stale_payload = fixture_image_payload(stale_fixture)
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"}
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    monkeypatch.setattr(smoke_module, "_golden_page_payload", lambda *unused: stale_payload)

    with pytest.raises(ServingConfigurationError, match="are not a PNG"):
        call(handle, chair, fixture, smoke_placement())

    assert handle.fixture_requests_completed == 0
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_refuses_an_untyped_utilization_sample_tuple(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"}
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    call = VisionSmokeCall(
        PAGE_WITNESS,
        utilization=lambda: ("71",),  # type: ignore[arg-type]
        chair_sampling=CHAIR_SAMPLING,
    )

    with pytest.raises(ServingConfigurationError, match="tuple of UtilizationSample values"):
        call(handle, chair, fixture, smoke_placement())

    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_vision_smoke_call_bounds_utilization_evidence_for_one_request(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": f"PAGE-WITNESS: {PAGE_WITNESS}"}
    )
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    handle = manager.start(chair, TIER)
    sample = UtilizationSample("71", "31")
    call = VisionSmokeCall(
        PAGE_WITNESS, utilization=lambda: (sample,) * 1_025, chair_sampling=CHAIR_SAMPLING
    )

    with pytest.raises(ServingConfigurationError, match="more than 1024 samples"):
        call(handle, chair, fixture, smoke_placement())

    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_serving_smoke_reader_refuses_a_nominally_green_result_without_service_request(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)
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


def test_the_plain_reader_seam_gives_the_same_log_root_guarantee_as_the_callback(
    tmp_path: Path,
) -> None:
    """The reader refuses a symlinked log root before anything can launch through it."""

    chair = identity("reader", "reader-v1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "logs").symlink_to(elsewhere)
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)
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
    manager, _, _, _, _, _ = reader_manager(tmp_path, chair=chair)
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
            sampling=SAMPLING,
        )
    assert handle.fixture_requests_completed == 0
    handle.stop()


def test_fixture_request_refuses_an_image_hidden_outside_openai_chat_content(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = reader_manager(tmp_path, chair=chair)
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    valid = fixture_image_payload(fixture)
    image_url = valid["messages"][0]["content"][0]["image_url"]  # type: ignore[index]
    hidden_image_payload = {
        "messages": [{"role": "user", "content": "text-only request"}],
        "ignored_extension": {"image_url": image_url},
    }
    handle = manager.start(chair, TIER)

    with pytest.raises(
        ServingConfigurationError,
        match="golden-page request has an image_url outside a role=user content list",
    ):
        handle.request_fixture_image(
            "chat-completions", hidden_image_payload, fixture=fixture, sampling=SAMPLING
        )
    assert handle.fixture_requests_completed == 0
    handle.stop()


def test_fixture_request_requires_an_openai_image_object_at_the_active_content_block(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = reader_manager(tmp_path, chair=chair)
    fixture = tmp_path / "golden-page.png"
    fixture.write_bytes(b"fixture page")
    malformed = json.loads(json.dumps(fixture_image_payload(fixture)))
    malformed["messages"][0]["content"][0]["image_url"] = "data:image/png;base64,ZmFrZQ=="
    handle = manager.start(chair, TIER)

    with pytest.raises(ServingConfigurationError, match="OpenAI image object"):
        handle.request_fixture_image(
            "chat-completions", malformed, fixture=fixture, sampling=SAMPLING
        )
    assert handle.fixture_requests_completed == 0
    handle.stop()


def test_fixture_request_dispatches_the_same_payload_snapshot_it_validates(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, http, _, _, _ = reader_manager(tmp_path, chair=chair)
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
    handle.request_fixture_image(
        "chat-completions", SwitchingMapping(), fixture=fixture, sampling=SAMPLING
    )
    sent = [body for method, _, body in http.calls if method == "POST"][-1]
    assert sent is not None
    assert sent["messages"][0]["content"][0]["type"] == "image_url"  # type: ignore[index]
    handle.stop()


def test_serving_smoke_reader_requires_the_exact_fixture_response_token(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)
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
            "chat-completions", fixture_image_payload(fixture), fixture=fixture, sampling=SAMPLING
        )
        return SmokeResult(True, True, True, {"fixture_response_sha256": "0" * 64}, ())

    with pytest.raises(ServingConfigurationError, match="does not name the exact fixture response"):
        ServingSmokeReader(manager, wrong_response_token, gpu_profile=measured_gpu()).read(
            chair, fixture, placement
        )
    assert launcher.processes[0].terminate_calls == 1


def test_smoke_reader_refuses_a_profile_dtype_not_assessed_by_preflight(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)
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
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)
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
            sampling=SAMPLING,
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


# --- the row's declared image geometry, proved against the model's own file ---


def _geometry_row(patch: int | None = 16, merge: int | None = 2):
    """A row shaped only as `assert_processor_geometry` reads one."""

    return SimpleNamespace(
        patch_size=patch,
        merge_size=merge,
        chair="attestator_2",
        recipe="unproven-real-attestatores",
        tier=TIER,
    )


def _snapshot_carrying(tmp_path: Path, filename: str, document: object):
    root = tmp_path / "snapshot"
    root.mkdir(exist_ok=True)
    (root / filename).write_text(json.dumps(document), encoding="utf-8")
    return SimpleNamespace(root=root)


# The two real shapes, taken from the pinned revisions themselves:
# `preprocessor_config.json` states the pair at the top level (chandra-ocr-2,
# churro-3B, Qwen3.8-27B); `processor_config.json` nests it under
# `image_processor`, and `attestator_2`'s DAI revision ships only that file, so
# the check must read either one.
TOP_LEVEL = {"patch_size": 16, "merge_size": 2, "image_processor_type": "Qwen2VLImageProcessorFast"}
NESTED = {
    "image_processor": {"patch_size": 16, "merge_size": 2},
    "processor_class": "Qwen3VLProcessor",
}


@pytest.mark.parametrize(
    "filename,document",
    [
        (PROCESSOR_CONFIG_FILENAMES[0], TOP_LEVEL),
        (PROCESSOR_CONFIG_FILENAMES[1], NESTED),
    ],
)
def test_a_row_matching_the_models_own_processor_configuration_passes(
    tmp_path: Path, filename: str, document: dict
) -> None:
    assert_processor_geometry(_snapshot_carrying(tmp_path, filename, document), _geometry_row())


@pytest.mark.parametrize(
    "filename,document",
    [
        (PROCESSOR_CONFIG_FILENAMES[0], {**TOP_LEVEL, "patch_size": 14}),
        (PROCESSOR_CONFIG_FILENAMES[1], {"image_processor": {"patch_size": 16, "merge_size": 1}}),
    ],
)
def test_a_row_that_disagrees_with_the_model_is_refused_by_name(
    tmp_path: Path, filename: str, document: dict
) -> None:
    """`patch_size`/`merge_size` decide every image's prompt-token cost, so a
    row whose pair differs from the model's own file is refused by name rather
    than serving under a mis-count the receipt would publish as checked."""

    with pytest.raises(ServingConfigurationError) as error:
        assert_processor_geometry(_snapshot_carrying(tmp_path, filename, document), _geometry_row())
    assert "attestator_2" in str(error.value)
    assert filename in str(error.value)


def test_a_row_that_declares_no_geometry_is_left_alone(tmp_path: Path) -> None:
    """Fixture and synthetic rows declare neither, and are unchanged by this."""

    snapshot = _snapshot_carrying(tmp_path, PROCESSOR_CONFIG_FILENAMES[0], {"patch_size": 14})
    assert_processor_geometry(snapshot, _geometry_row(patch=None, merge=None))


def test_a_snapshot_with_no_processor_configuration_is_passed_not_refused(tmp_path: Path) -> None:
    """The documented boundary. All four pinned repositories ship one of the two
    files, so absence means a synthetic store rather than a wrong declaration
    about a real model -- and whether a materialized store is complete is
    `common/chairs/model_store.py`'s question, answered against the manifest."""

    root = tmp_path / "empty"
    root.mkdir()
    assert_processor_geometry(SimpleNamespace(root=root), _geometry_row())


def test_an_unreadable_processor_configuration_is_refused_rather_than_skipped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    (root / PROCESSOR_CONFIG_FILENAMES[0]).write_text("{not json", encoding="utf-8")
    with pytest.raises(ServingConfigurationError):
        assert_processor_geometry(SimpleNamespace(root=root), _geometry_row())


@pytest.mark.parametrize(
    "filename,document",
    [
        (PROCESSOR_CONFIG_FILENAMES[0], {"patch_size": 16}),
        (PROCESSOR_CONFIG_FILENAMES[1], {"image_processor": {"merge_size": 2}}),
    ],
)
def test_a_present_file_with_neither_pair_complete_is_refused_not_skipped(
    tmp_path: Path, filename: str, document: dict
) -> None:
    """A present file naming only one of the two values must refuse, not pass
    silently: `common/request_capacity.py` uses both row values on every
    image, so an unconfirmed pair cannot be treated as confirmed."""

    with pytest.raises(ServingConfigurationError) as error:
        assert_processor_geometry(_snapshot_carrying(tmp_path, filename, document), _geometry_row())
    assert "attestator_2" in str(error.value)
    assert filename in str(error.value)


@pytest.mark.parametrize("document", [[16, 2], None, "patch_size=16"])
def test_a_present_file_that_is_not_a_json_object_is_refused_not_skipped(
    tmp_path: Path, document: object
) -> None:
    with pytest.raises(ServingConfigurationError, match="not a JSON object") as error:
        assert_processor_geometry(
            _snapshot_carrying(tmp_path, PROCESSOR_CONFIG_FILENAMES[0], document), _geometry_row()
        )
    assert "attestator_2" in str(error.value)


def test_a_split_declaration_across_top_level_and_nested_is_read_and_confirmed(
    tmp_path: Path,
) -> None:
    """A processor file naming one field at the top level and the other under
    `image_processor` still supplies a complete, confirmable pair."""

    document = {"patch_size": 16, "image_processor": {"merge_size": 2}}
    assert_processor_geometry(
        _snapshot_carrying(tmp_path, PROCESSOR_CONFIG_FILENAMES[1], document), _geometry_row()
    )


def test_a_split_declaration_that_disagrees_with_the_row_is_refused(tmp_path: Path) -> None:
    """The counterfactual for the split layout: a check that skipped an
    incomplete nested section would still pass the positive test above; this
    one cannot, because the split pair must be read to be refused."""

    document = {"patch_size": 14, "image_processor": {"merge_size": 2}}
    with pytest.raises(ServingConfigurationError) as error:
        assert_processor_geometry(
            _snapshot_carrying(tmp_path, PROCESSOR_CONFIG_FILENAMES[1], document), _geometry_row()
        )
    assert "attestator_2" in str(error.value)


# --------------------------------------------------------------------------
# generation_config = "vllm" only; hybrid-attention prefix caching;
# --enable-prompt-tokens-details and usage reconciliation; a deterministic
# probe rejection breaking before the watchdog; local.env discoverability;
# and static preflight assertions.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chair", ["attestator_2", "perlector"])
def test_generation_config_auto_is_refused_on_every_row(chair: str) -> None:
    """'auto' would fill an unsent sampling field from the model's file, unseen."""
    row = profile_row(recipe="row-v1", chair=chair, served_model_id="row-api", port=8200)
    row["generation_config"] = "auto"
    with pytest.raises(ServingConfigurationError, match=r"must be one of \['vllm'\]"):
        recipes(row)

    row["generation_config"] = "vllm"
    assert recipes(row).profiles[0].generation_config == "vllm"


def test_generation_config_vllm_is_rendered_on_the_launch(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)

    handle = manager.start(chair, TIER)

    assert handle.launch_audit["profile"]["generation_config"] == "vllm"  # type: ignore[index]
    assert "generation_config_digest" not in handle.launch_audit["profile"]  # type: ignore[operator]
    argv, _log_path = launcher.calls[0]
    handle.stop()
    assert argv[argv.index("--generation-config") + 1] == "vllm"


@pytest.mark.parametrize(
    ("role", "repo", "recipe", "served_model_id"),
    [
        (
            "attestator_1",
            "datalab-to/chandra-ocr-2",
            "unproven-real-attestatores",
            "attestator-1-api",
        ),
        ("perlector", "Qwen/Qwen3.8-27B", "unproven-real-perlector", "perlector-api"),
    ],
)
def test_a_hybrid_attention_checkpoint_refuses_to_launch_with_prefix_caching_on(
    tmp_path: Path, role: str, repo: str, recipe: str, served_model_id: str
) -> None:
    """Chandra-2 and the Perlector, named by repository -- never by role.

    Role names are reused across this whole suite as generic fixture
    identifiers (``test_client.py``'s default identity is literally
    ``attestator_1``), so this refusal must be keyed on the exact real
    checkpoint, not on a role a fixture happens to share. With the switch off,
    the launch must pass the explicit off flag: vLLM turns prefix caching on
    for hybrids when the flag is absent.
    """

    chair = ChairIdentity(
        role=role,
        source="huggingface",
        repo=repo,
        path=None,
        revision=REVISION,
        digest_manifest=MANIFEST,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe=recipe,
        license_note="test identity only",
    )
    row = profile_row(
        recipe=recipe,
        chair=role,
        served_model_id=served_model_id,
        port=8102,
    )
    row["enable_prefix_caching"] = True
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=(served_model_id,),
    )

    with pytest.raises(ServingRecipeRefusal, match="hybrid Mamba/attention"):
        manager.start(chair, TIER)
    assert launcher.processes == []
    assert publisher.calls == []

    off_row = dict(row)
    off_row["enable_prefix_caching"] = False
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path / "off",
        identities={chair.role: chair},
        profiles=(off_row,),
        model_ids=(served_model_id,),
    )
    manager.start(chair, TIER).stop()
    argv, _log_path = launcher.calls[0]
    assert "--no-enable-prefix-caching" in argv
    assert "--enable-prefix-caching" not in argv


def test_a_fixture_role_sharing_the_same_name_is_unaffected_by_the_hybrid_check(
    tmp_path: Path,
) -> None:
    """Same role name (``attestator_1``), a fake repository: no refusal."""

    chair = identity("attestator_1", "reader-v1")  # repo="example/attestator_1"
    row = profile_row(
        recipe="reader-v1", chair="attestator_1", served_model_id="reader-api", port=8000
    )
    row["enable_prefix_caching"] = True
    manager, _, _, launcher, _, _ = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
    )

    manager.start(chair, TIER).stop()
    assert launcher.calls


def test_start_wires_processor_geometry_and_refuses_a_disagreeing_row(tmp_path: Path) -> None:
    """`assert_processor_geometry` is unit-tested on its own above; this proves
    `manager.start` actually calls it, on the exact base snapshot, before launch."""

    chair = identity("reader", "reader-v1")
    row = profile_row(recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000)
    row["patch_size"] = 14
    row["merge_size"] = 2
    snapshot_root = tmp_path / "reader"
    snapshot_root.mkdir(parents=True)
    (snapshot_root / "processor_config.json").write_text(
        json.dumps({"image_processor": {"patch_size": 16, "merge_size": 2}})
    )
    manager, _, _, launcher, _, publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(row,),
        model_ids=("reader-api",),
    )

    with pytest.raises(ServingRecipeRefusal, match="mis-count every request"):
        manager.start(chair, TIER)
    assert launcher.processes == []
    assert publisher.calls == []


def test_a_deterministic_probe_rejection_breaks_before_the_full_watchdog_wait(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, clock, _, launcher, _, publisher = reader_manager(
        tmp_path, chair=chair, probe_http_status=400
    )

    with pytest.raises(ServingRecipeRefusal, match="VLLM_PROBE_HTTP_ERROR") as excinfo:
        manager.start(chair, TIER)

    assert "VLLM_WATCHDOG_TIMEOUT" not in str(excinfo.value)
    # A retry-to-deadline failure would have consumed the full
    # startup_timeout_seconds (3, from profile_row); breaking on a
    # deterministic rejection costs nothing.
    assert clock.seconds == 0
    assert launcher.processes[0].terminate_calls == 1
    assert publisher.calls == []


def test_a_transient_probe_status_still_retries_to_the_watchdog(tmp_path: Path) -> None:
    """502/503 is the engine still booting, not rejecting the request shape."""

    chair = identity("reader", "reader-v1")
    manager, clock, _, launcher, _, _ = reader_manager(tmp_path, chair=chair, probe_http_status=503)

    with pytest.raises(ServingRecipeRefusal, match="VLLM_WATCHDOG_TIMEOUT.*VLLM_PROBE_HTTP_ERROR"):
        manager.start(chair, TIER)

    assert clock.seconds == 3
    assert launcher.processes[0].terminate_calls == 1


def test_assert_no_discoverable_local_env_refuses_only_when_present(tmp_path: Path) -> None:
    assert_no_discoverable_local_env(directory=tmp_path)  # absent: no refusal

    (tmp_path / "local.env").write_text("SOME_TOKEN=x\n")
    with pytest.raises(ServingConfigurationError, match="local.env"):
        assert_no_discoverable_local_env(directory=tmp_path)


def test_assert_no_discoverable_local_env_also_catches_this_projects_own_dotenv_names(
    tmp_path: Path,
) -> None:
    """`.env`/`.env.*` are this repo's own credential-filename convention.

    `.gitignore` ignores `.env` and `.env.*` (keeping only the tracked
    `.env.example`), and `.githooks/check_ingress.py` names `.env` a
    sensitive filename -- `local.env` matches no such convention anywhere in
    this repository or in vLLM, so the check must not rely on that name alone.
    """

    (tmp_path / ".env").write_text("HF_TOKEN=leaked\n")
    with pytest.raises(ServingConfigurationError, match=r"\.env"):
        assert_no_discoverable_local_env(directory=tmp_path)


def test_assert_no_discoverable_local_env_catches_a_dotenv_variant_but_not_the_example(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.example").write_text("HF_TOKEN=replace-me\n")
    assert_no_discoverable_local_env(directory=tmp_path)  # the tracked example is not a leak

    (tmp_path / ".env.production").write_text("HF_TOKEN=leaked\n")
    with pytest.raises(ServingConfigurationError, match=r"\.env\.production"):
        assert_no_discoverable_local_env(directory=tmp_path)


def test_manager_start_refuses_a_discoverable_env_override_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`manager.start` itself must guard every real launch, not only the smoke reader.

    `ChairClient.__enter__` calls `manager.start` directly, with no smoke
    lifecycle in between; a check placed only in `ServingSmokeReader.read`
    would never run on that path.
    """

    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, _ = reader_manager(tmp_path, chair=chair)
    operator_cwd = tmp_path / "operator-cwd"
    operator_cwd.mkdir()
    (operator_cwd / ".env").write_text("HF_TOKEN=leaked\n")
    monkeypatch.chdir(operator_cwd)

    with pytest.raises(ServingConfigurationError, match=r"\.env"):
        manager.start(chair, TIER)

    assert launcher.processes == []
    assert registry.ensure_calls == []


def test_serving_smoke_reader_refuses_a_discoverable_local_env_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, registry, _ = reader_manager(tmp_path, chair=chair)
    fixture = tmp_path / "golden-page.png"
    write_golden_page(fixture)
    operator_cwd = tmp_path / "operator-cwd"
    operator_cwd.mkdir()
    (operator_cwd / "local.env").write_text("HF_TOKEN=leaked\n")
    monkeypatch.chdir(operator_cwd)

    reader = ServingSmokeReader(manager, vision_smoke(), gpu_profile=measured_gpu())

    with pytest.raises(ServingConfigurationError, match="local.env"):
        reader.read(chair, fixture, smoke_placement())

    assert launcher.processes == []
    assert registry.ensure_calls == []


def test_assert_image_before_text_on_wire_checks_the_rendered_order() -> None:
    assert_image_before_text_on_wire(
        [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            {"type": "text", "text": "read this"},
        ]
    )
    with pytest.raises(ServingConfigurationError, match="must open with an image_url part"):
        assert_image_before_text_on_wire(
            [
                {"type": "text", "text": "read this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ]
        )
    with pytest.raises(ServingConfigurationError, match="is empty"):
        assert_image_before_text_on_wire([])


def test_assert_generation_config_key_coverage_names_every_unaccounted_key() -> None:
    vendor = {"temperature": 0.0, "top_k": 1, "repetition_penalty": 1.05}
    assert_generation_config_key_coverage(
        chair="attestator_3",
        vendor_generation_config=vendor,
        sent_keys=("temperature", "repetition_penalty"),
        deliberately_not_sent={"top_k": "recorded reason"},
    )
    with pytest.raises(ServingConfigurationError, match="neither sent"):
        assert_generation_config_key_coverage(
            chair="attestator_3",
            vendor_generation_config=vendor,
            sent_keys=("temperature",),
            deliberately_not_sent={},
        )
    with pytest.raises(ServingConfigurationError, match="both sent and deliberately"):
        assert_generation_config_key_coverage(
            chair="attestator_3",
            vendor_generation_config=vendor,
            sent_keys=("temperature", "top_k"),
            deliberately_not_sent={"top_k": "recorded reason"},
        )


def test_render_vllm_argv_carries_enable_prompt_tokens_details(tmp_path: Path) -> None:
    """The engine's per-modality token counts are requested, so each call can be reconciled."""

    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)

    manager.start(chair, TIER).stop()

    assert "--enable-prompt-tokens-details" in launcher.calls[0][0]


def test_the_readiness_poll_retries_a_transport_refusal_and_then_starts(tmp_path: Path) -> None:
    """A normalised transport refusal must cost an interval, never the launch.

    A broken 4xx/5xx body that escaped the transport as a bare `http.client`
    exception would miss this loop's `except EndpointUnavailable` and reach the
    unexpected-start handler, which refuses and tears the child down.
    Classification is pinned against a real socket in `test_http.py`; this pins
    that the classification is spent on a retry.
    """

    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, _ = reader_manager(tmp_path, chair=chair)

    class RefusesTwiceOnceLaunched:
        """Refuse the first two post-launch health checks, then defer to the fake.

        Only post-launch: `_assert_endpoint_unoccupied` runs the same health URL
        before the child exists and reads an ambiguous refusal there as an
        occupied port, which is a different — and correct — behaviour.
        """

        def __init__(self) -> None:
            self.refusals = 0

        def request(
            self, method: str, url: str, *, body: bytes | None, timeout_seconds: float
        ) -> HttpResponse:
            if launcher.processes and url.endswith("/health") and self.refusals < 2:
                self.refusals += 1
                raise EndpointUnavailable(
                    "GET /health: IncompleteRead: IncompleteRead(0 bytes read)",
                    definitively_absent=False,
                )
            return http.request(method, url, body=body, timeout_seconds=timeout_seconds)

    flaky = RefusesTwiceOnceLaunched()
    manager.http = flaky

    handle = manager.start(chair, TIER)

    assert flaky.refusals == 2
    assert handle.receipt.details.endpoint == "http://127.0.0.1:8000/v1"
    assert launcher.processes[0].terminate_calls == 0


def test_a_readiness_probe_never_outlives_what_is_left_of_the_watchdog(tmp_path: Path) -> None:
    """Each probe gets the smaller of its own budget and the watchdog's remainder.

    With `startup_timeout_seconds` of 3 and a 1-second poll, the third round
    has one second left, and the probe must be told so rather than issued its
    full budget regardless of how little time remains.
    """

    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, _ = reader_manager(tmp_path, chair=chair)

    class RecordingHealthBudgets:
        def __init__(self) -> None:
            self.health_budgets: list[float] = []
            self.rounds = 0

        def request(
            self, method: str, url: str, *, body: bytes | None, timeout_seconds: float
        ) -> HttpResponse:
            if launcher.processes and url.endswith("/health"):
                self.health_budgets.append(timeout_seconds)
                self.rounds += 1
                if self.rounds <= 2:
                    raise EndpointUnavailable("not up yet", definitively_absent=False)
            return http.request(method, url, body=body, timeout_seconds=timeout_seconds)

    recorder = RecordingHealthBudgets()
    manager.http = recorder

    manager.start(chair, TIER)

    assert recorder.health_budgets == [2.0, 2.0, 1.0]


def test_a_credential_shaped_token_in_the_launch_log_never_travels_with_the_refusal() -> None:
    """The tail now reaches journals, pod reports and notifications; the log did not.

    A launch log is the child's own stdout, so it is untrusted content going
    somewhere it has never been. The shape test is the one `bootstrap_main`'s
    argv refusal and `fixture.py`'s drill scrub already share.
    """

    error = _watchdog_timeout(
        FakeProcess(
            4242,
            log_tail=(
                "INFO: retrying with token hf9QRs3xKm7ZtPvN1cWb4L\n"
                "Loading safetensors checkpoint shards: 12% Completed | 3/28\n"
            ),
        ),
        last="loopback endpoint unavailable: connection refused",
        endpoint_state=_ENDPOINT_REFUSED,
        budget_seconds=300.0,
    )

    assert "hf9QRs3xKm7ZtPvN1cWb4L" not in error.detail
    assert "[redacted]" in error.detail
    # The redaction does not cost the diagnosis its evidence.
    assert "Loading safetensors checkpoint shards: 12% Completed | 3/28" in error.detail


def test_a_budget_gone_before_the_first_probe_answers_claims_no_observation() -> None:
    """A one-second `startup_timeout_seconds` can expire before any probe comes back."""

    error = _watchdog_timeout(
        FakeProcess(4242, log_tail="INFO: nothing here\n"),
        last="service did not become ready",
        endpoint_state=None,
        budget_seconds=1.0,
    )

    assert " -- no-probe: " in error.detail


def _nested_json(levels: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": "audit value"}
    for _ in range(levels):
        value = {"nested": value}
    return value


def test_thawed_json_copies_every_level_into_plain_json_up_to_its_depth_bound() -> None:
    inner = MappingProxyType({"port": 8000})
    source = {"outer": {"inner": {"port": 8000}}, "children": ((inner, "plain"),)}
    copied = thawed_json(source)

    assert copied == {"outer": {"inner": {"port": 8000}}, "children": [[{"port": 8000}, "plain"]]}
    assert json.dumps(copied)
    source["outer"]["inner"]["port"] = 9001
    assert copied["outer"]["inner"]["port"] == 8000
    at_the_bound = _nested_json(MAX_JSON_DEPTH)
    assert thawed_json(at_the_bound) == at_the_bound


def test_thawed_json_refuses_a_value_nested_past_its_bound_by_name() -> None:
    chain: object = MappingProxyType({"leaf": "audit value"})
    for _ in range(MAX_JSON_DEPTH + 1):
        chain = (chain,)
    for deep in (_nested_json(MAX_JSON_DEPTH + 1), {"launched": chain}):
        with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_JSON_DEPTH} levels"):
            thawed_json(deep)


# --- The DAI chair's RecordGold smoke page --------------------------------------


def _recordgold_fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A pinned test record fetched from memory, written as the chair's fixture page."""

    from .recordgold_smoke import fetch_recordgold_smoke_page
    from .test_recordgold_smoke import record_pin

    record, fetch = record_pin()
    page = fetch_recordgold_smoke_page(record, fetch=fetch)
    fixture = tmp_path / "recordgold-record.png"
    fixture.write_bytes(page.png)
    return record, page, fixture, hashlib.sha256(page.png).hexdigest()


def _recordgold_smoke(record, text: str, page_sha256: str) -> VisionSmokeCall:  # type: ignore[no-untyped-def]
    return VisionSmokeCall(
        PAGE_WITNESS,
        utilization=lambda: (UtilizationSample("71", "31"),),
        chair_sampling=CHAIR_SAMPLING,
        recordgold_chairs=frozenset({"reader"}),
        recordgold_record=record,
        recordgold_text=text,
        recordgold_page_sha256=page_sha256,
    )


def test_a_recordgold_chair_is_scored_by_cer_against_the_gold_and_passes_a_slip(
    tmp_path: Path,
) -> None:
    from .test_recordgold_smoke import TEST_GOLD_TEXT

    record, page, fixture, page_sha256 = _recordgold_fixture(tmp_path)
    chair = identity("reader", "reader-v1")
    manager, _, http, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": TEST_GOLD_TEXT.replace("Pierre", "Piere")}
    )
    handle = manager.start(chair, TIER)

    result = _recordgold_smoke(record, page.text, page_sha256)(
        handle, chair, fixture, smoke_placement()
    )

    assert (result.shape_valid, result.nonempty, result.format_valid) == (True, True, True)
    receipt = result.receipt
    assert receipt["smoke_page"] == "recordgold-record"
    assert receipt["page_witness_matches"] is True
    assert receipt["page_witness_edit_distance"] == 1
    assert receipt["page_witness_sha256"] == record.text_sha256
    assert receipt["recordgold_page_sha256"] == page_sha256
    assert receipt["recordgold_record"] == record.to_record()
    assert Decimal(receipt["character_error_rate"]) <= Decimal(  # type: ignore[arg-type]
        receipt["character_error_rate_threshold"]  # type: ignore[arg-type]
    )
    assert receipt["reference_units"] == len(TEST_GOLD_TEXT)
    assert "page_witness_reference" not in receipt
    # Neither the gold nor the answer leaves in the receipt, and the gold never
    # reaches the chair in the prompt: it is asked what its run asks, over the page.
    assert TEST_GOLD_TEXT not in json.dumps(receipt)
    request = http.calls[-1][2]
    assert isinstance(request, dict)
    assert TEST_GOLD_TEXT not in json.dumps(request)
    messages = request["messages"]
    assert isinstance(messages, list)
    assert [message["role"] for message in messages] == ["system", "user"]
    image_url = messages[1]["content"][0]["image_url"]["url"]  # type: ignore[index]
    assert image_url == "data:image/png;base64," + base64.b64encode(page.png).decode("ascii")
    handle.stop()
    assert launcher.processes[0].terminate_calls == 1


def test_a_recordgold_reading_over_the_threshold_fails_format_and_keeps_its_measurement(
    tmp_path: Path,
) -> None:
    from .test_recordgold_smoke import TEST_GOLD_TEXT

    record, page, fixture, page_sha256 = _recordgold_fixture(tmp_path)
    chair = identity("reader", "reader-v1")
    manager, _, _, _, _, _ = reader_manager(
        tmp_path, chair=chair, outputs={"reader-api": TEST_GOLD_TEXT[:40]}
    )
    handle = manager.start(chair, TIER)

    result = _recordgold_smoke(record, page.text, page_sha256)(
        handle, chair, fixture, smoke_placement()
    )

    assert (result.shape_valid, result.nonempty, result.format_valid) == (True, True, False)
    receipt = result.receipt
    assert receipt["page_witness_matches"] is False
    assert receipt["page_witness_edit_distance"] == len(TEST_GOLD_TEXT) - 40
    assert Decimal(receipt["character_error_rate"]) > Decimal(  # type: ignore[arg-type]
        receipt["character_error_rate_threshold"]  # type: ignore[arg-type]
    )
    handle.stop()


def test_a_recordgold_chair_handed_another_page_is_refused_by_name_before_any_request(
    tmp_path: Path,
) -> None:
    from .recordgold_smoke import RecordGoldSmokeRefusal

    record, page, _fixture, page_sha256 = _recordgold_fixture(tmp_path)
    chair = identity("reader", "reader-v1")
    manager, _, http, _, _, _ = reader_manager(tmp_path, chair=chair)
    golden = tmp_path / "golden-page.png"
    write_golden_page(golden)
    handle = manager.start(chair, TIER)
    calls_before = len(http.calls)

    with pytest.raises(RecordGoldSmokeRefusal, match="recordgold-smoke-image-mismatch"):
        _recordgold_smoke(record, page.text, page_sha256)(handle, chair, golden, smoke_placement())

    assert len(http.calls) == calls_before
    handle.stop()


def test_the_smoke_refuses_recordgold_chairs_without_a_verified_gold_text(
    tmp_path: Path,
) -> None:
    from .recordgold_smoke import RecordGoldSmokeRefusal
    from .test_recordgold_smoke import TEST_GOLD_TEXT

    record, _page, _fixture, page_sha256 = _recordgold_fixture(tmp_path)
    with pytest.raises(ServingConfigurationError, match="no verified gold transcription"):
        VisionSmokeCall(
            PAGE_WITNESS, chair_sampling=CHAIR_SAMPLING, recordgold_chairs=frozenset({"reader"})
        )
    with pytest.raises(RecordGoldSmokeRefusal, match="recordgold-smoke-text-mismatch"):
        _recordgold_smoke(record, TEST_GOLD_TEXT + ".", page_sha256)


_PLACEMENT = load_placement_table(Path(__file__).resolve().parents[2] / "config/pod_placement.toml")


def _reader_plan(
    *,
    row_max_num_seqs: int = 1,
    max_num_seqs: int = 6,
    inputs: ServingConfigInputs | None = None,
    cap: int = 48,
) -> CapacityPlan:
    return CapacityPlan(
        max_num_seqs_cap=cap,
        vram_gib=Decimal("95.5"),
        gpu_count=1,
        compute_capability="12.0",
        tier=TIER,
        serving_config_inputs=inputs or ServingConfigInputs("1" * 64, "2" * 64),
        chairs={
            "reader": ChairCapacity(
                recipe="reader-v1",
                row_max_num_seqs=row_max_num_seqs,
                max_num_seqs=max_num_seqs,
                weights_gib=Decimal("10"),
                kv_gib_per_seq=Decimal("1"),
                memory_fraction=Decimal("0.85"),
            )
        },
    )


def test_without_a_plan_the_row_launches_as_written_and_the_audit_is_unchanged(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair)
    handle = manager.start(chair, TIER)
    assert _value_after(launcher.calls[0][0], "--max-num-seqs") == "1"
    assert handle.profile.max_num_seqs == 1
    assert "capacity" not in handle.launch_audit
    handle.stop()


def test_a_plan_widens_the_launch_and_the_audit_records_row_derived_card_and_digest(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    plan = _reader_plan()
    manager, _, _, launcher, _, _ = reader_manager(tmp_path, chair=chair, capacity_plan=plan)
    handle = manager.start(chair, TIER)
    argv = launcher.calls[0][0]
    assert _value_after(argv, "--max-num-seqs") == "6"
    # Nothing else in the launch moves.
    assert _value_after(argv, "--max-num-batched-tokens") == "256"
    assert _value_after(argv, "--gpu-memory-utilization") == "0.85"
    assert _value_after(argv, "--max-model-len") == "2048"
    assert handle.profile.max_num_seqs == 6
    audit = handle.launch_audit
    assert audit["capacity"] == {  # type: ignore[index]
        "plan_sha256": plan.digest,
        "card": {"vram_gib": "95.5", "gpu_count": 1, "compute_capability": "12.0"},
        "row_max_num_seqs": 1,
        "max_num_seqs": 6,
        "row_ceiling": 2,
        "planned_ceiling": 48,
    }
    assert audit["profile"]["max_num_seqs"] == 6  # type: ignore[index]
    # The fields the serving spans and the pod watcher read are still there.
    for key in ("schema", "chair", "launch_purpose", "started_at", "readiness", "command"):
        assert key in audit
    expected_argv_digest = hashlib.sha256(
        json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert audit["command"]["argv_sha256"] == expected_argv_digest  # type: ignore[index]
    handle.stop()


def test_a_plan_derived_from_another_row_is_refused_before_launch(tmp_path: Path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, capacity_plan=_reader_plan(row_max_num_seqs=2)
    )
    with pytest.raises(ServingRecipeRefusal, match="max_num_seqs"):
        manager.start(chair, TIER)
    assert launcher.calls == []


def test_a_plan_from_other_serving_configuration_is_refused_at_construction(
    tmp_path: Path,
) -> None:
    chair = identity("reader", "reader-v1")
    with pytest.raises(ServingConfigurationError, match="sealed"):
        reader_manager(
            tmp_path,
            chair=chair,
            capacity_plan=_reader_plan(inputs=ServingConfigInputs("3" * 64, "2" * 64)),
        )


# --- a shared service: handed off by one stage's process, taken over by the next ----


def _shared_rows(**reconstructor_changes: object) -> tuple[dict[str, object], ...]:
    perlector_row = profile_row(
        recipe="perlector-v1", chair="perlector", served_model_id="shared-api", port=8106
    )
    reconstructor_row = {
        **profile_row(
            recipe="reconstructor-v1",
            chair="reconstructor",
            served_model_id="shared-api",
            port=8106,
        ),
        "shares_service_with": "perlector",
        **reconstructor_changes,
    }
    return perlector_row, reconstructor_row


def _shared_identities(**reconstructor_changes: object) -> dict[str, ChairIdentity]:
    perlector_chair = identity("perlector", "perlector-v1")
    reconstructor_chair = replace(
        perlector_chair, role="reconstructor", serving_recipe="reconstructor-v1"
    )
    return {
        "perlector": perlector_chair,
        "reconstructor": replace(reconstructor_chair, **reconstructor_changes),
        "witness": identity("witness", "witness-v1"),
    }


class _AddressedPublisher(FakePublisher):
    """Names the launch audit by its canonical digest, as the run tree's blob store does."""

    def publish(self, receipt, launch_audit):  # type: ignore[no-untyped-def]
        publication = super().publish(receipt, launch_audit)
        digest = hashlib.sha256(canonical_bytes(dict(launch_audit))).hexdigest()
        return ReceiptPublication(
            publication.receipt_reference,
            {"relative_path": f"stages/blobs/sha256/{digest}", "sha256": digest},
            publication.evidence_reference,
        )


def _shared_managers(
    tmp_path: Path,
    *,
    second_scope: str = "run-a",
    identities: Mapping[str, ChairIdentity] | None = None,
    rows: tuple[dict[str, object], ...] | None = None,
    capacity_plan: CapacityPlan | None = None,
):
    """Two managers as two stage processes on one pod: one card (endpoint), one lease
    and one hand-off path, each with its own launcher, registry and publisher."""
    identities = identities or _shared_identities()
    rows = rows or _shared_rows()
    witness_row = profile_row(
        recipe="witness-v1", chair="witness", served_model_id="witness-api", port=8200
    )
    clock = Clock()
    http = FakeHttp(model_ids=("shared-api", "witness-api"))
    built = []
    for scope in ("run-a", second_scope):
        registry = FakeRegistry(identities, tmp_path)
        # One digest-keyed cache directory serves both roles, as the real registry does.
        registry.snapshots = {
            role: replace(snapshot, root=tmp_path / snapshot.manifest_digest)
            for role, snapshot in registry.snapshots.items()
        }
        launcher = FakeLauncher(http)
        publisher = _AddressedPublisher(http)
        manager = ServingManager(
            registry=registry,
            recipes=recipes(*rows, witness_row, identities=identities),
            config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
            launcher=launcher,
            http=http,
            receipt_publisher=publisher,
            log_root=tmp_path / "logs",
            package_inspector=FakePackages({"vllm": "0.test"}),
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
            hand_off_path=tmp_path / "pod-gpu.hand-off.json",
            service_scope=scope,
            capacity_plan=capacity_plan,
            placement_table=_PLACEMENT if capacity_plan is not None else None,
        )
        built.append(SimpleNamespace(manager=manager, launcher=launcher, publisher=publisher))
    return built[0], built[1], identities, clock


def _hand_off_from_first(first, identities, clock) -> ServiceHandle:
    handle = first.manager.start(identities["perlector"], TIER)
    clock.seconds += 600
    assert handle.hand_off() is True
    return handle


def test_a_sharing_row_takes_over_the_handed_off_service_and_records_it(tmp_path: Path) -> None:
    first, second, identities, clock = _shared_managers(tmp_path)
    original = _hand_off_from_first(first, identities, clock)
    assert (tmp_path / "pod-gpu.hand-off.json").is_file()
    # The handing-off manager owns nothing now: it cannot stop or read the service.
    with pytest.raises(ServiceStopError, match="not this manager's active"):
        original.stop()

    adopted = second.manager.start(identities["reconstructor"], TIER)

    assert second.launcher.calls == []
    assert second.launcher.attached == [first.launcher.processes[0]]
    assert not (tmp_path / "pod-gpu.hand-off.json").exists()
    receipt, audit = second.publisher.calls[-1]
    assert receipt.identity == identities["reconstructor"]
    assert receipt.details.started_at == original.receipt.details.started_at
    assert receipt.details.endpoint == original.receipt.details.endpoint
    assert audit["launch_purpose"] == "adopted"
    assert audit["chair"] == "reconstructor"
    assert audit["started_at"] == original.launch_audit["started_at"]
    assert audit["command"]["argv_sha256"] == original.launch_audit["command"]["argv_sha256"]
    assert audit["readiness"] == thawed_json(original.launch_audit["readiness"])
    assert audit["adoption"]["from_chair"] == "perlector"
    assert audit["adoption"]["launched_for"] == "normal"
    assert audit["adoption"]["audit_reference"] == dict(original.audit_reference)
    assert audit["adoption"]["adopted_at"] > audit["started_at"]

    adopted.stop()
    assert first.launcher.processes[0].terminate_calls == 1
    # The lease went with the service: any manager may now start a chair.
    witness = second.manager.start(identities["witness"], TIER)
    witness.stop()


@pytest.mark.parametrize(
    "case,reason",
    [
        ("another-run", "handed off by another run"),
        ("other-checkpoint", "serves another checkpoint"),
        ("other-shape", "render another vLLM command"),
        ("process-gone", "the handed-off process is gone"),
    ],
)
def test_a_refused_take_over_stops_the_service_and_starts_cold_saying_why(
    tmp_path: Path, case: str, reason: str
) -> None:
    options: dict[str, object] = {}
    if case == "another-run":
        options["second_scope"] = "run-b"
    if case == "other-checkpoint":
        options["identities"] = _shared_identities(revision="c" * 40)
    first, second, identities, clock = _shared_managers(tmp_path, **options)
    _hand_off_from_first(first, identities, clock)
    if case == "other-shape":
        # The same rows, but this manager renders its vLLM command another way.
        second.manager.command_prefix = (*second.manager.command_prefix, "--other")
        second.manager.package_inspector = FakePackages({"vllm": "0.test"})
    if case == "process-gone":
        first.launcher.processes[0].exit_code = 0

    started = second.manager.start(identities["reconstructor"], TIER)

    assert len(second.launcher.calls) == 1
    _receipt, audit = second.publisher.calls[-1]
    assert audit["launch_purpose"] == "normal"
    assert reason in audit["adoption_refused"]
    expected = "already-exited" if case == "process-gone" else "stopped"
    assert audit["displaced_service"] == {
        "chair": "perlector",
        "pid": first.launcher.processes[0].pid,
        "outcome": expected,
    }
    if expected == "stopped":
        assert first.launcher.processes[0].terminate_calls == 1
    assert not (tmp_path / "pod-gpu.hand-off.json").exists()
    started.stop()


def test_a_start_that_shares_nothing_stops_a_handed_off_service_first(tmp_path: Path) -> None:
    first, second, identities, clock = _shared_managers(tmp_path)
    _hand_off_from_first(first, identities, clock)

    witness = second.manager.start(identities["witness"], TIER)

    _receipt, audit = second.publisher.calls[-1]
    assert "adoption_refused" not in audit
    assert audit["displaced_service"]["outcome"] == "stopped"
    assert first.launcher.processes[0].terminate_calls == 1
    witness.stop()


def test_an_unreadable_hand_off_record_is_discarded_and_said(tmp_path: Path) -> None:
    _first, second, identities, _clock = _shared_managers(tmp_path)
    (tmp_path / "pod-gpu.hand-off.json").write_text("{not json", encoding="utf-8")

    started = second.manager.start(identities["reconstructor"], TIER)

    _receipt, audit = second.publisher.calls[-1]
    assert "could not be read" in audit["hand_off_discarded"]
    assert not (tmp_path / "pod-gpu.hand-off.json").exists()
    started.stop()


def test_a_stage_that_never_starts_its_chair_reclaims_the_handed_off_service(
    tmp_path: Path,
) -> None:
    first, second, identities, clock = _shared_managers(tmp_path)
    _hand_off_from_first(first, identities, clock)

    reclaimed = second.manager.reclaim_hand_off()

    assert reclaimed == {
        "chair": "perlector",
        "pid": first.launcher.processes[0].pid,
        "outcome": "stopped",
    }
    assert second.manager.reclaim_hand_off() is None
    witness = second.manager.start(identities["witness"], TIER)
    witness.stop()


def test_a_service_without_a_hand_off_path_is_not_handed_off(tmp_path: Path) -> None:
    first, _second, identities, _clock = _shared_managers(tmp_path)
    first.manager.hand_off_path = None
    handle = first.manager.start(identities["perlector"], TIER)

    assert handle.hand_off() is False
    handle.stop()
    assert first.launcher.processes[0].terminate_calls == 1


def test_a_shared_row_must_match_its_partners_launch_fields() -> None:
    perlector_row, reconstructor_row = _shared_rows(max_num_seqs=2)
    with pytest.raises(
        ServingConfigurationError, match=r"launch fields differ: \['max_num_seqs'\]"
    ):
        recipes(perlector_row, reconstructor_row)
    lonely = {**reconstructor_row, "shares_service_with": "nobody", "max_num_seqs": 1}
    with pytest.raises(ServingConfigurationError, match="has no vllm row at that tier"):
        recipes(perlector_row, lonely)


def test_only_a_sharing_pair_may_share_an_endpoint_and_a_served_id() -> None:
    perlector_row, reconstructor_row = _shared_rows()
    catalogue = recipes(perlector_row, reconstructor_row)
    assert {row.chair for row in catalogue.profiles} == {"perlector", "reconstructor"}
    unshared = {
        key: value for key, value in reconstructor_row.items() if key != "shares_service_with"
    }
    with pytest.raises(ServingConfigurationError, match="assigned to more than one chair"):
        recipes(perlector_row, unshared)
    intruder = profile_row(
        recipe="witness-v1", chair="witness", served_model_id="witness-api", port=8106
    )
    with pytest.raises(ServingConfigurationError, match="endpoint"):
        recipes(perlector_row, reconstructor_row, intruder)


def test_the_real_catalogue_shares_the_perlector_service_with_the_reconstructor() -> None:
    catalogue = load_serving_recipes(
        Path(__file__).resolve().parents[2] / "config" / "serving_recipes_real.toml"
    )
    [row] = [
        profile
        for profile in catalogue.profiles
        if isinstance(profile, ServingProfile) and profile.chair == "reconstructor"
    ]
    assert row.shares_service_with == "perlector"


def _planned_shared_rows() -> tuple[dict[str, object], ...]:
    capacity = {"weights_gib": "10", "kv_gib_per_seq": "1"}
    perlector_row, reconstructor_row = _shared_rows(**capacity)
    return {**perlector_row, **capacity}, reconstructor_row


def test_under_a_plan_a_shared_pair_launches_at_one_width_and_the_take_over_holds(
    tmp_path: Path,
) -> None:
    """The plan gives the reconstructor the width it gives the Perlector, so the
    Coniector's chair renders the command the Perlector's service was launched with
    and takes it over: 0.78 x 48 GiB - 10 - 4 leaves 23 sequences of 1 GiB."""
    from operations.serving.capacity import derive_capacity_plan

    identities = _shared_identities()
    rows = _planned_shared_rows()
    plan = derive_capacity_plan(
        vram_gib=Decimal("48"),
        gpu_count=1,
        compute_capability="8.6",
        tier=TIER,
        engine_memory_fraction=Decimal("0.78"),
        recipes=recipes(*rows, identities=identities),
        chairs=identities,
        serving_config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        max_num_seqs_cap=48,
    )
    assert plan.chairs["perlector"].max_num_seqs == plan.chairs["reconstructor"].max_num_seqs
    assert plan.chairs["reconstructor"].max_num_seqs == 23
    first, second, identities, clock = _shared_managers(
        tmp_path, identities=identities, rows=rows, capacity_plan=plan
    )
    original = _hand_off_from_first(first, identities, clock)
    launched = first.launcher.calls[0][0]
    assert launched[launched.index("--max-num-seqs") + 1] == "23"

    adopted = second.manager.start(identities["reconstructor"], TIER)

    assert second.launcher.calls == []
    _receipt, audit = second.publisher.calls[-1]
    assert audit["launch_purpose"] == "adopted"
    assert audit["command"]["argv_sha256"] == original.launch_audit["command"]["argv_sha256"]
    assert audit["capacity"]["max_num_seqs"] == 23
    assert audit["capacity"]["row_max_num_seqs"] == 1
    assert audit["capacity"]["plan_sha256"] == plan.digest
    assert adopted.profile.max_num_seqs == 23
    adopted.stop()


def test_a_plan_that_split_a_shared_pair_would_cost_the_take_over(tmp_path: Path) -> None:
    """Why the pair is planned once: a reconstructor width other than the
    Perlector's renders another command, and the service is stopped and reloaded."""
    identities = _shared_identities()
    figures = {
        "row_max_num_seqs": 1,
        "weights_gib": Decimal("10"),
        "kv_gib_per_seq": Decimal("1"),
        "memory_fraction": Decimal("0.78"),
    }
    split = CapacityPlan(
        max_num_seqs_cap=48,
        vram_gib=Decimal("48"),
        gpu_count=1,
        compute_capability="8.6",
        tier=TIER,
        serving_config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        chairs={
            "perlector": ChairCapacity(recipe="perlector-v1", max_num_seqs=23, **figures),
            "reconstructor": ChairCapacity(recipe="reconstructor-v1", max_num_seqs=8, **figures),
        },
    )
    first, second, identities, clock = _shared_managers(
        tmp_path, identities=identities, rows=_planned_shared_rows(), capacity_plan=split
    )
    _hand_off_from_first(first, identities, clock)

    started = second.manager.start(identities["reconstructor"], TIER)

    _receipt, audit = second.publisher.calls[-1]
    assert audit["launch_purpose"] == "normal"
    assert "render another vLLM command" in audit["adoption_refused"]
    started.stop()


def test_a_server_gone_with_its_engine_still_running_is_stopped_not_forgotten(
    tmp_path: Path,
) -> None:
    """vLLM's engine process can outlive the server that leads its group and keep the
    card. Such a service is not taken over, and not recorded as already exited: its
    group is signalled, and only then is the record consumed."""
    first, second, identities, clock = _shared_managers(tmp_path)
    _hand_off_from_first(first, identities, clock)
    leader = first.launcher.processes[0]
    leader.exit_code = 0
    leader.engine_outlives_leader = True

    started = second.manager.start(identities["reconstructor"], TIER)

    _receipt, audit = second.publisher.calls[-1]
    assert "has exited, though its process group still runs" in audit["adoption_refused"]
    assert audit["displaced_service"]["outcome"] == "stopped"
    assert leader.terminate_calls == 1 and not leader.group_running
    assert not (tmp_path / "pod-gpu.hand-off.json").exists()
    started.stop()


def test_a_gone_service_whose_lease_is_still_held_keeps_its_record(tmp_path: Path) -> None:
    first, second, identities, clock = _shared_managers(tmp_path)
    _hand_off_from_first(first, identities, clock)
    first.launcher.processes[0].exit_code = 0
    holder = FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(identities["witness"])
    try:
        with pytest.raises(ServiceStopError, match="lease is still held"):
            second.manager.reclaim_hand_off()
        assert (tmp_path / "pod-gpu.hand-off.json").exists()
    finally:
        holder.release()
    assert second.manager.reclaim_hand_off()["outcome"] == "already-exited"
    assert not (tmp_path / "pod-gpu.hand-off.json").exists()


def test_a_taken_over_service_stops_while_a_straggler_keeps_the_launcher_s_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live 2026-10-08 (prep73): the Coniector stopped the Perlector's service, its group
    and endpoint were gone, yet the lease the Perlector's manager had taken was still
    held, and the stop failed. The stop now succeeds and says so, and the lease is left
    held, so no chair starts on the card until whatever holds it lets go."""
    first, second, identities, clock = _shared_managers(tmp_path)
    started = first.manager.start(identities["perlector"], TIER)
    # The launching manager's lease descriptor as a process of the service, outside
    # its group, still has it: the same lock, so the hand-off below leaves it held.
    straggler = os.dup(first.launcher.inherited_fds[0][0])
    try:
        clock.seconds += 600
        assert started.hand_off() is True
        adopted = second.manager.start(identities["reconstructor"], TIER)

        adopted.stop()

        assert first.launcher.processes[0].terminate_calls == 1
        assert "the card's lease is still held" in capsys.readouterr().err
        with pytest.raises(ServingRecipeRefusal, match="holds the single-resident lease"):
            second.manager.start(identities["witness"], TIER)
    finally:
        os.close(straggler)
    second.manager.start(identities["witness"], TIER).stop()


def test_a_plan_not_bound_by_the_tier_s_planned_ceiling_is_refused_before_launch(
    tmp_path: Path,
) -> None:
    """generic-48gb sets planned_batch_ceiling 48; a plan derived under 64 could launch
    past it, so it is refused before anything starts."""
    chair = identity("reader", "reader-v1")
    manager, _, _, launcher, _, _ = reader_manager(
        tmp_path, chair=chair, capacity_plan=_reader_plan(max_num_seqs=60, cap=64)
    )
    with pytest.raises(ServingRecipeRefusal, match="planned_batch_ceiling 48"):
        manager.start(chair, TIER)
    assert launcher.calls == []


def test_a_plan_without_the_placement_table_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="placement table"):
        ServingManager(
            registry=FakeRegistry({}, tmp_path),
            recipes=recipes(
                profile_row(
                    recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
                ),
                identities={"reader": identity("reader", "reader-v1")},
            ),
            config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
            launcher=FakeLauncher(FakeHttp(model_ids=())),
            http=FakeHttp(model_ids=()),
            receipt_publisher=FakePublisher(FakeHttp(model_ids=())),
            log_root=tmp_path / "logs",
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
            capacity_plan=_reader_plan(),
        )
