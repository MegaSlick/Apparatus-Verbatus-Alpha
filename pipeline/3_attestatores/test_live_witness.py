"""Drills for the live Attestatores request builders and response derivation.

Every test runs offline. `live_attempt_from_response`/`captured_page_attempt`
are proven against a genuine :class:`~operations.serving.client.ChairResponse`
built through a real :class:`~operations.serving.client.ChairClient` against
:mod:`operations.serving.fakes` -- the same fake-endpoint machinery
`operations/serving/test_client.py` uses -- so the seam this module owns is
exercised with the actual wire-to-record plumbing behind it, not a
hand-typed stand-in response. Adapters, by contrast, are small stubs: what
each real adapter's `retain`/`parse` does is already proven by
`test_witness_adapters.py`, `test_churro_native_capture.py`, and
`test_chandra_adapter.py`; this module is tested for its own logic --
building a request, and turning one retained response into a `LiveAttempt`
-- with the real `churro.v1` and `chandra.v1` adapters brought in only where
a test specifically wants to prove real wiring, not a stub's promise.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

STAGE = Path(__file__).resolve().parent
if str(STAGE) not in sys.path:
    sys.path.insert(0, str(STAGE))

import feeding  # noqa: E402
import live_witness  # noqa: E402
import witness_adapters  # noqa: E402
from chandra import FIXTURE_RESPONSE_SCHEMA as CHANDRA_FIXTURE_SCHEMA  # noqa: E402

from common.chairs.models import ChairIdentity  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.contracts.serving import STOP_REASON_UNREPORTED  # noqa: E402
from common.native_witness import CHURRO_OUTPUT_TOKENS  # noqa: E402
from operations.serving.client import ChairClient, ChairRequest  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    chair_preflight_identity_digest,
    parse_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import (  # noqa: E402
    ABSENT,
    FakeBlobStore,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakePublisher,
    FakeRegistry,
    ScriptedAnswer,
)
from operations.serving.manager import ServingManager  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

REVISION = "a" * 40
MANIFEST = "b" * 64
DECODING_SHA = "c" * 64
TIER = "generic-48gb"


# --- a fake run tree: image bytes by path, plus a content-addressed blob sink -----


class _FakeTree:
    def __init__(self) -> None:
        self._by_path: dict[str, bytes] = {}

    def seed(self, path: str, data: bytes) -> None:
        self._by_path[path] = data

    def read_bytes(self, path: str) -> bytes:
        return self._by_path[path]

    def put_blob(self, stage: str, data: bytes):
        digest = digest_bytes(data)
        relative_path = f"{stage}/blobs/sha256/{digest}"
        self._by_path[relative_path] = data
        return digest, SimpleNamespace(relative_path=relative_path)


def _presentation(
    *, kind: str, image_bytes: bytes, image_path: str = "exemplar/blobs/sha256/img"
) -> dict[str, Any]:
    digest = digest_bytes(image_bytes)
    presentation: dict[str, Any] = {
        "kind": kind,
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": image_path,
        "image_sha256": digest,
        "transform": {
            "operation": "whole" if kind == "page" else "crop",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
        },
    }
    if kind == "region":
        presentation["region_ref"] = {"region_id": "act-1-region-0"}
    return presentation


def _image_url_parts(request: ChairRequest) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for message in request.messages:
        content = message.get("content")
        if isinstance(content, list):
            parts.extend(part for part in content if part.get("type") == "image_url")
    return parts


def _decoded_images(request: ChairRequest) -> list[bytes]:
    return [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        for part in _image_url_parts(request)
    ]


# =========================== request builders ================================


def test_act_chair_request_builds_the_dai_two_message_framing_and_generation_split():
    context = SimpleNamespace(tree=_FakeTree())
    image_bytes = b"dai-crop-bytes"
    presentation = _presentation(kind="region", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt)

    act_request = live_witness.act_chair_request(context, adapter, presentation)
    request = act_request.request

    assert request.kind == "chat-completions"
    assert request.image_sha256s == (digest_bytes(image_bytes),)
    assert _decoded_images(request) == [image_bytes]
    system, user = request.messages
    assert system == {"role": "system", "content": feeding.dai_prompt()["system"]}
    assert user["role"] == "user"
    assert user["content"][0] == {"type": "text", "text": feeding.dai_prompt()["user"]}
    # `presented`/`prompt` are carried forward so `live_attempt_from_response`
    # never has to run `adapter.present` a second time for this same act.
    assert act_request.presented == presentation
    assert act_request.prompt == feeding.dai_prompt()

    declared = feeding.dai_generation()
    assert request.generation_declared == declared
    assert dict(request.generation_sent) == {
        "repetition_penalty": declared["repetition_penalty"],
        "top_k": declared["top_k"],
        "top_p": declared["top_p"],
    }
    for forbidden in ("temperature", "do_sample", "bos_token_id", "eos_token_id", "pad_token_id"):
        assert forbidden not in request.generation_sent


def test_act_chair_request_refuses_a_presented_image_that_does_not_match_its_own_digest():
    context = SimpleNamespace(tree=_FakeTree())
    presentation = _presentation(kind="region", image_bytes=b"real-bytes")
    context.tree.seed(presentation["image_path"], b"different-bytes-entirely")
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt)

    with pytest.raises(SchemaRefusal):
        live_witness.act_chair_request(context, adapter, presentation)


def test_page_chair_request_builds_churros_two_message_framing_and_renames_the_token_bound():
    context = SimpleNamespace(tree=_FakeTree())
    image_bytes = b"whole-page-bytes"
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.churro_prompt)

    request = live_witness.page_chair_request(context, adapter, "churro.v1", presentation)

    assert request.image_sha256s == (digest_bytes(image_bytes),)
    system, user = request.messages
    assert system == {"role": "system", "content": feeding.churro_prompt()["system"]}
    assert user["content"][0]["text"] == feeding.churro_prompt()["user"]
    assert request.generation_declared == {"max_new_tokens": CHURRO_OUTPUT_TOKENS}
    assert dict(request.generation_sent) == {"max_tokens": CHURRO_OUTPUT_TOKENS}


def test_page_chair_request_builds_chandras_single_instruction_framing():
    context = SimpleNamespace(tree=_FakeTree())
    image_bytes = b"whole-page-bytes-2"
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    chandra_module = sys.modules.get("chandra") or __import__("chandra")
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=chandra_module.prompt)

    request = live_witness.page_chair_request(context, adapter, "chandra.v1", presentation)

    assert len(request.messages) == 1
    (message,) = request.messages
    assert message["role"] == "user"
    assert message["content"][0] == {"type": "text", "text": chandra_module.prompt()["instruction"]}
    assert request.generation_declared == {}
    assert request.generation_sent == {}


def test_page_chair_request_refuses_an_unrecognized_prompt_shape():
    context = SimpleNamespace(tree=_FakeTree())
    image_bytes = b"page-bytes-3"
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=lambda: {"weird": "shape"})

    with pytest.raises(SchemaRefusal):
        live_witness.page_chair_request(context, adapter, "made-up.v1", presentation)


# =========================== response derivation harness ======================


def _identity(role: str = "attestator_1", recipe: str = "recipe-1") -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="huggingface",
        repo=f"example/{role}",
        path=None,
        revision=REVISION,
        digest_manifest=MANIFEST,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe=recipe,
        license_note="test identity only",
        witness_adapter="dai.v1",
        witness_scope="act",
    )


def _vllm_row(*, recipe: str, chair: str, served_model_id: str) -> dict[str, object]:
    return {
        "kind": "vllm",
        "recipe": recipe,
        "chair": chair,
        "tier": TIER,
        "host": "127.0.0.1",
        "port": 8000,
        "served_model_id": served_model_id,
        "dtype": "bfloat16",
        "seed": 7,
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
        "enable_tower_connector_lora": False,
        "max_lora_rank": 16,
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


def _world(tmp_path: Path, *, chair: ChairIdentity | None = None):
    chair = chair or _identity()
    row = _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="served-alias")
    row["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
    row["preflight_digest"] = profile_preflight_digest(row)
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(served_model_id="served-alias", blob_store=blob_store)
    launcher = FakeLauncher(endpoint)
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    manager = ServingManager(
        registry=registry,
        recipes=parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]}),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=endpoint,
        receipt_publisher=FakePublisher(),
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )

    def read_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {"chair": chair.role, "revision": chair.receipt_revision}

    client = ChairClient(
        manager=manager,
        identity=chair,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256=DECODING_SHA,
        record_temperature=0,
        read_receipt=read_receipt,
    )
    return client, endpoint, blob_store


def _read_one(tmp_path: Path, *, script: ScriptedAnswer):
    client, endpoint, blob_store = _world(tmp_path)
    with client:
        endpoint.script(script)
        data_uri = "data:image/png;base64," + base64.b64encode(b"one-image").decode("ascii")
        request = ChairRequest(
            kind="chat-completions",
            messages=(
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": data_uri}}],
                },
            ),
            image_sha256s=(digest_bytes(b"one-image"),),
            generation_declared={},
            generation_sent={},
        )
        response = client.read(request)
    return response, endpoint, blob_store


def _stub_adapter(*, retain_result: dict[str, Any], prompt: dict[str, Any] | None = None) -> Any:
    def prompt_fn() -> dict[str, Any]:
        return dict(prompt) if prompt is not None else {"instruction": "read"}

    def retain_fn(tree, *, view, raw_response, transport_stop_reason, parser=None):
        del tree, view, parser
        digest = hashlib.sha256(raw_response).hexdigest()
        return {
            **retain_result,
            "transport_stop_reason": transport_stop_reason,
            "raw_response_ref": {"relative_path": f"blobs/sha256/{digest}", "sha256": digest},
        }

    return SimpleNamespace(prompt=prompt_fn, retain=retain_fn)


def _dai_presentation(*, width: int = 3_000, height: int = 1_001) -> dict[str, Any]:
    """A DAI act presentation shaped to force a resize (`feeding.dai_dimensions`
    maps 3000x1001 to 1500x500, per `test_feeding.py`), so `source_image_ref`
    and `model_image_ref` are never required to collide in these stub-adapter
    tests -- the identity-transform gap the module docstring names is
    deliberately not what these tests are proving."""
    return {
        "kind": "region",
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": "designator/blobs/sha256/source-crop",
        "image_sha256": digest_bytes(b"designator-source-crop"),
        "transform": {
            "operation": "crop",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": width, "h": height},
        },
        "region_ref": {"region_id": "act-1-region-0"},
    }


def _dai_presented(*, image_bytes: bytes = b"dai-model-image") -> dict[str, Any]:
    return {
        "kind": "adapter-crop",
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": "attestatores/blobs/sha256/model-crop",
        "image_sha256": digest_bytes(image_bytes),
        "transform": {
            "operation": "crop-resize-preserve-aspect",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": 3_000, "h": 1_001},
            "resize": {
                "resampler": "pillow-lanczos",
                "dimension_rounding": "floor",
                "source_width_px": 3_000,
                "source_height_px": 1_001,
                "target_width_px": 1_500,
                "target_height_px": 500,
            },
        },
    }


def _dai_identity_view_kwargs(*, crop_bytes: bytes = b"the designator's own act crop"):
    """A DAI act small enough that no resize runs -- the case U8 unblocks.

    `feeding.dai_dimensions(100, 50)` is `(100, 50)`, so the adapter's crop is
    the Designator's crop, byte for byte. The two references therefore carry
    one digest under two stage-owned paths, which is what a content-addressed
    store means by "the same retained blob": every image a witness is shown is
    inventoried under `3_attestatores/`, while the proposal crop it was cut
    from lives under `2_designator/`. Held to the whole reference dict, as
    `dai_model_view` once was, this act was refused after its response had
    already come back.
    """

    digest = digest_bytes(crop_bytes)
    bounds = {"x": 0, "y": 0, "w": 100, "h": 50}
    return {
        "presentation": {
            "kind": "region",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "image_path": f"2_designator/blobs/sha256/{digest}",
            "image_sha256": digest,
            "transform": {
                "operation": "crop",
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": dict(bounds),
            },
            "region_ref": {"region_id": "act-1-region-0"},
        },
        "presented": {
            "kind": "adapter-crop",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "image_path": f"3_attestatores/blobs/sha256/{digest}",
            "image_sha256": digest,
            "transform": {
                "operation": "crop",
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": dict(bounds),
            },
        },
        "prompt": feeding.dai_prompt(),
    }


def _dai_view_kwargs() -> dict[str, Any]:
    return {
        "presentation": _dai_presentation(),
        "presented": _dai_presented(),
        "prompt": feeding.dai_prompt(),
    }


# =========================== live_attempt_from_response ========================


def test_live_attempt_from_response_read_on_a_complete_stop(tmp_path: Path):
    response, endpoint, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(content="transcribed text", finish_reason="stop")
    )
    adapter = _stub_adapter(
        retain_result={"parse": {"state": "parsed", "text": "transcribed text"}}
    )

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "transcribed text"
    assert attempt.health["truncated"] is False
    assert attempt.health["truncation_basis"] == "trusted-response-boundary"
    assert attempt.native_capture["transport_stop_reason"] == "stop"
    assert len(endpoint.requests) == 1  # no retry
    assert blob_store.has(response.response_sha256)  # raw blob retained


def test_live_attempt_from_response_refuses_a_non_dai_adapter_name(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})

    with pytest.raises(SchemaRefusal):
        live_witness.live_attempt_from_response(
            SimpleNamespace(tree=_FakeTree()),
            adapter,
            "churro.v1",
            response,
            generation_declared={},
            parser="text",
            **_dai_view_kwargs(),
        )


def test_live_attempt_from_response_genuinely_empty_on_a_confirmed_blank(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "genuinely-empty"
    assert attempt.health["empty"] is True


def test_live_attempt_from_response_cut_off_empty_is_failed_not_confirmed_blank(tmp_path: Path):
    # GOVERNANCE 10 / ARCHITECTURE "truncation is a refused reading, never an
    # output": an empty response the engine itself cut off at its token bound
    # is not evidence of a genuinely blank act, on the act path exactly as on
    # the page path.
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="length"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank act" in attempt.reason
    assert attempt.health["truncated"] is True


def test_live_attempt_from_response_unreported_empty_is_failed_not_confirmed_blank(
    tmp_path: Path,
):
    # An empty response whose stop boundary was never reported at all is no
    # more a confirmed blank act than one the engine admits it cut off.
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason=ABSENT))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank act" in attempt.reason
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-recorded"


def test_live_attempt_from_response_truncated_true_on_length(tmp_path: Path):
    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="cut off tex", finish_reason="length")
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "cut off tex"}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "read"
    assert attempt.health["truncated"] is True
    assert attempt.native_capture["transport_stop_reason"] == "length"


def test_live_attempt_from_response_unreported_stop_reason_is_truncation_unknown(tmp_path: Path):
    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="some text", finish_reason=ABSENT)
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "some text"}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert response.finish_reason is None
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-recorded"
    assert attempt.native_capture["transport_stop_reason"] == STOP_REASON_UNREPORTED


def test_live_attempt_from_response_unknown_stop_reason_carried_verbatim(tmp_path: Path):
    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="text", finish_reason="abort")
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "text"}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    # Not in ENGINE_STOP_COMPLETE or ENGINE_STOP_CUT_OFF: this system does not
    # recognize "abort", so it is carried verbatim but never coerced into
    # either "the engine confirmed completion" or "the engine confirmed a cut
    # off" -- GOVERNANCE 10 refuses to default an unread signal to a meaning.
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-recorded"
    assert attempt.native_capture["transport_stop_reason"] == "abort"


def test_live_attempt_from_response_failed_on_a_parser_failure(tmp_path: Path):
    response, _, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(content="not valid for this parser", finish_reason="stop")
    )
    adapter = _stub_adapter(
        retain_result={"parse": {"state": "failed", "reason": "could not decode as text"}}
    )

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "failed"
    assert attempt.native_payload is None
    assert "could not decode as text" in attempt.reason
    assert attempt.health["recordable"] is False
    assert blob_store.has(response.response_sha256)  # raw blob retained even on failure


def test_live_attempt_from_response_failed_on_a_malformed_wire_body(tmp_path: Path):
    # No choices at all: parse_openai_reading refuses before any native parse
    # is possible, and `ChairClient.read` records `parse_problem`, not content.
    response, endpoint, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(body=b'{"model":"served-alias","choices":[]}')
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "never reached"}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert response.parse_problem is not None
    assert response.content is None
    assert attempt.outcome == "failed"
    assert attempt.native_capture is None  # the adapter's own parser never ran
    assert attempt.raw_response_ref == dict(response.raw_response_ref)
    assert blob_store.has(response.raw_response_ref["sha256"])  # retained before parsing
    assert len(endpoint.requests) == 1  # still no retry despite the malformed body


def test_live_attempt_carries_the_receipt_and_call_record_references(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.receipt_ref == dict(response.receipt_ref)
    assert attempt.call_record_ref == dict(response.call_record_ref)


def test_live_attempt_from_response_real_dai_adapter_round_trip(tmp_path: Path):
    """One integration point through the real dai.v1 adapter, not a stub.

    Proves `feeding.dai_model_view`/`validate_dai_model_view` actually accept
    the closed view this module now builds -- the shape the stub adapter's
    `retain_fn` discards and every other test in this module never exercises.
    """
    response, _, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(content="texte transcrit", finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("dai.v1")

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "texte transcrit"
    assert attempt.native_capture["adapter"] == "dai.v1"
    assert attempt.native_capture["view"]["adapter"] == "dai-atr.v1"
    assert blob_store.has(response.response_sha256)


def test_a_no_resize_dai_act_is_carried_rather_than_refused_after_its_answer(tmp_path: Path):
    """U8, the HANDOFF's second owed gap: the identity transform, closed.

    Every act crop in the reference fixture is small enough that DAI needs no
    resize, so this was not an edge case -- it was the ordinary DAI act, and
    it was refused *after* the chair had already answered it, by
    `dai_model_view`'s identity rule comparing whole reference dicts across two
    stages' blob namespaces. The invariant that mattered (the model was shown
    exactly the source bytes) is kept, and checked here on the digest the two
    references share.
    """

    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="texte transcrit", finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("dai.v1")
    view_kwargs = _dai_identity_view_kwargs()
    assert view_kwargs["presentation"]["image_sha256"] == view_kwargs["presented"]["image_sha256"]
    assert view_kwargs["presentation"]["image_path"] != view_kwargs["presented"]["image_path"]

    attempt = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **view_kwargs,
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "texte transcrit"
    transform = attempt.native_capture["view"]["transform"]
    assert transform["kind"] == "identity"
    assert transform["resampler"] is None
    # Both references name the same bytes, each in the store its own stage owns.
    view = attempt.native_capture["view"]
    assert view["source_image_ref"]["sha256"] == view["model_image_ref"]["sha256"]
    assert view["source_image_ref"]["relative_path"].startswith("2_designator/")
    assert view["model_image_ref"]["relative_path"].startswith("3_attestatores/")


def test_a_no_resize_dai_act_whose_model_image_is_other_bytes_is_still_refused(tmp_path: Path):
    """The invariant the digest comparison keeps: same bytes, or refusal.

    Relaxing the identity rule from "the same reference" to "the same content"
    must not relax it to "any two references": a model shown something other
    than the source crop, on a path that claims no resize ran, is exactly the
    lie the rule exists to catch.
    """

    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="texte transcrit", finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("dai.v1")
    view_kwargs = _dai_identity_view_kwargs()
    other = digest_bytes(b"some other image entirely")
    view_kwargs["presented"] = {
        **view_kwargs["presented"],
        "image_path": f"3_attestatores/blobs/sha256/{other}",
        "image_sha256": other,
    }

    with pytest.raises(SchemaRefusal, match="identity transform does not retain the source"):
        live_witness.live_attempt_from_response(
            SimpleNamespace(tree=_FakeTree()),
            adapter,
            "dai.v1",
            response,
            generation_declared=feeding.dai_generation(),
            parser="text",
            **view_kwargs,
        )


def test_a_live_act_says_which_kind_of_bytes_it_retained(tmp_path: Path):
    """U8's sixth gap, at the seam that decides it.

    `raw_response_ref` means the adapter's own output on every branch where a
    parser ran, and the whole transport body on the one branch where none
    could. Two kinds of evidence under one field name, and nothing said which.
    """

    parsed_response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="texte transcrit", finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("dai.v1")
    parsed = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        parsed_response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **_dai_identity_view_kwargs(),
    )
    assert parsed.raw_response_kind == "model-output"
    assert parsed.raw_response_ref == dict(parsed.native_capture["raw_response_ref"])

    malformed_response, _, _ = _read_one(
        tmp_path / "second", script=ScriptedAnswer(body=b"not json at all")
    )
    assert malformed_response.parse_problem is not None
    malformed = live_witness.live_attempt_from_response(
        SimpleNamespace(tree=_FakeTree()),
        adapter,
        "dai.v1",
        malformed_response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **_dai_identity_view_kwargs(),
    )
    assert malformed.raw_response_kind == "transport-response-body"
    assert malformed.native_capture is None
    # The two really are different bytes: the envelope, and the model's output.
    assert malformed.raw_response_ref == dict(malformed_response.raw_response_ref)
    assert parsed.raw_response_ref != dict(parsed_response.raw_response_ref)


# =========================== captured_page_attempt =============================


def test_captured_page_attempt_read_on_a_complete_stop(tmp_path: Path):
    response, _, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(content="<output>page text</output>", finish_reason="stop")
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "page text"}})

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "page text"
    assert blob_store.has(response.response_sha256)


def test_captured_page_attempt_cut_off_empty_is_failed_not_confirmed_blank(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="length"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank page" in attempt.reason
    assert attempt.health["truncated"] is True


def test_captured_page_attempt_unreported_empty_is_failed_not_confirmed_blank(tmp_path: Path):
    # An empty page response whose stop boundary was never reported is no more
    # a confirmed blank page than one the provider admits it cut off -- the
    # same GOVERNANCE 10 guard applies whether the unknown is "cut off" or
    # "never said."
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason=ABSENT))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank page" in attempt.reason
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-recorded"


def test_captured_page_attempt_genuinely_empty_on_a_confirmed_blank_page(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "genuinely-empty"


def test_captured_page_attempt_failed_on_a_malformed_wire_body_retains_raw_bytes(tmp_path: Path):
    response, _, blob_store = _read_one(tmp_path, script=ScriptedAnswer(body=b"not even json"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "never reached"}})

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert attempt.native_capture is None
    assert blob_store.has(response.raw_response_ref["sha256"])


def test_captured_page_attempt_refuses_an_unsupported_adapter_name(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})

    with pytest.raises(SchemaRefusal):
        live_witness.captured_page_attempt(
            SimpleNamespace(tree=_FakeTree()), 1, "attestator_1", "dai.v1", adapter, response
        )


def test_captured_page_attempt_real_churro_adapter_round_trip(tmp_path: Path):
    """One integration point through the real churro.v1 adapter, not a stub."""
    response, _, blob_store = _read_one(
        tmp_path,
        script=ScriptedAnswer(content="<output>real churro text</output>", finish_reason="stop"),
    )
    adapter = witness_adapters.resolve_runnable_adapter("churro.v1")

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_2", "churro.v1", adapter, response
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "real churro text"
    assert attempt.native_capture["adapter"] == "churro.v1"
    assert attempt.observation_payload is None  # Chandra-only; run.py never reads it for Churro
    assert blob_store.has(response.response_sha256)


def test_captured_page_attempt_real_chandra_adapter_is_honest_about_the_unverified_wire_shape(
    tmp_path: Path,
):
    """Chandra live: transported and retained, `chandra.parse` recognizes only
    the fixture schema, so a real model's markdown/JSON output that is not
    that exact placeholder lands as a named, honest parse failure -- never a
    fabricated reading."""
    response, _, blob_store = _read_one(
        tmp_path,
        script=ScriptedAnswer(
            content='{"schema":"a-real-vendor-schema.v1","markdown":"hi","blocks":[]}',
            finish_reason="stop",
        ),
    )
    adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_3", "chandra.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "unverified-response-schema" in attempt.reason
    assert blob_store.has(response.response_sha256)
    # U8's fourth gap: the adapter's own account of those bytes is now
    # attachable. It reached `unrecognized-shape` -- the parser ran, read the
    # whole body, and could place no shape it knows -- which the shared capture
    # contract admits, so the retained model view stays beside the blob it
    # describes instead of being dropped for want of a state name.
    assert attempt.native_capture["parse"] == {
        "state": "unrecognized-shape",
        "parser": "json",
        "outcome": "unverified-response-schema",
    }
    # Whether the shared contract accepts this capture is proven against a real
    # run tree in `test_attestatores_live_pass.py`, not here: this module's
    # `_FakeTree` addresses blobs by its own path scheme, which
    # `validate_native_capture`'s content-addressed check would refuse for
    # reasons that have nothing to do with the parse state.
    assert attempt.raw_response_kind == "model-output"


def test_captured_page_attempt_real_chandra_adapter_reads_the_fixture_placeholder_schema(
    tmp_path: Path,
):
    """The one shape `chandra.parse` accepts is the fixture's own stand-in
    schema string, not a real vendor wire shape (its companion test above pins
    that a genuine vendor schema is honestly refused). This module is the
    first thing to route *live* bytes into `chandra.parse`, so this pins the
    placeholder's reach into live mode -- not a design this module chose. U6
    owes a HANDOFF.md line naming that Unit 11 must remove the placeholder
    acceptance once the real wire schema lands, rather than inheriting it
    silently."""
    body = f'{{"schema":"{CHANDRA_FIXTURE_SCHEMA}","markdown":"chandra text","blocks":[]}}'
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content=body, finish_reason="stop"))
    adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")

    attempt = live_witness.captured_page_attempt(
        SimpleNamespace(tree=_FakeTree()), 1, "attestator_3", "chandra.v1", adapter, response
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "chandra text"
    # `run.py` prefers `observation_payload` over `native_payload` to feed
    # `adapter.observe` for Chandra page geometry (run.py:2408-2409).
    assert attempt.observation_payload == body.encode("utf-8")
