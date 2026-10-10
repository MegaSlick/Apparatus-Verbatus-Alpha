"""Fake-first drills for ChairClient, its record, and serving_mode_for.

Every test runs offline against :mod:`operations.serving.fakes`. No test
imports vLLM, starts a server, or contacts a provider.
"""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Mapping

import pytest

from common import page_path
from common.chairs.models import ChairIdentity, ServingDetails
from common.chairs.receipts import build_receipt, receipt_record
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import (
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_CALL_RECORD_FIELDS,
    CHAIR_STREAM_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHANDRA_NATIVE_CALL_RECORD_FIELDS,
    CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
)
from common.decoding import (
    READING_CHAIRS,
    SAMPLING_FIELDS,
    chair_decoding,
    engine_effective_sampling,
    load_decoding_policy,
    perlector_loop_guard,
    recorded_wire_decimals,
    witness_loop_guard,
)
from common.sealed_config import table_seal

from .assembly import SERVING_READER
from .client import (
    ChairClient,
    ChairRequest,
    ReceiptDriftRefusal,
    ServingModeRefusal,
    serving_mode_for,
)
from .config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    parse_serving_recipes,
    profile_preflight_digest,
    thawed_json,
)
from .errors import (
    ChairRequestRefusal,
    ChairResponseRefusal,
    ChairTransportFailure,
    ServiceStopError,
    ServingConfigurationError,
)
from .fakes import (
    FakeBlobStore,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakePublisher,
    FakeRegistry,
    ScriptedAnswer,
)
from .http import request_body
from .manager import ServingManager
from .residency import FileResidencyLease

TIER = "generic-48gb"
REVISION = "a" * 40
MANIFEST = "b" * 64
SHIPPED_POLICY, DECODING_SHA = load_decoding_policy()


def _policy_with_row(chair: str, sampling: Mapping[str, object]) -> dict[str, object]:
    """The shipped policy with some of one chair's sampling values changed."""

    policy = copy.deepcopy(SHIPPED_POLICY)
    policy["chair_decoding"][chair] = {**policy["chair_decoding"][chair], **sampling}
    return policy


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
    )


def _vllm_row(
    *, recipe: str, chair: str, served_model_id: str, tier: str = TIER
) -> dict[str, object]:
    return {
        "kind": "vllm",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
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


def _fixture_row(*, recipe: str, chair: str, tier: str = TIER) -> dict[str, object]:
    return {
        "kind": "fixture",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "description": "walking-skeleton stand-in",
    }


def _unsupported_row(
    *, recipe: str, chair: str, tier: str = TIER, reason: str
) -> dict[str, object]:
    return {
        "kind": "unsupported",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "reason": reason,
    }


def _seal(row: dict[str, object], chair: ChairIdentity) -> dict[str, object]:
    sealed = dict(row)
    if sealed.get("preflight_state") == "proven":
        sealed["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
        sealed["preflight_digest"] = profile_preflight_digest(sealed)
    return sealed


def _recipes(*rows: dict[str, object]):
    return parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": list(rows)})


def _default_read_receipt(chair: ChairIdentity):
    def read_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {
            "chair": chair.role,
            "source": chair.source,
            "resolved": chair.source_reference,
            "revision": chair.receipt_revision,
            "revision_kind": chair.receipt_revision_kind,
            "digest_manifest": chair.digest_manifest,
        }

    return read_receipt


def _built(
    tmp_path: Path,
    *,
    chair: ChairIdentity | None = None,
    row: dict[str, object] | None = None,
    read_receipt=None,
    sampling: Mapping[str, object] | None = None,
):
    chair = chair or _identity()
    row = _seal(
        row
        or _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="served-alias"),
        chair,
    )
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(served_model_id="served-alias", blob_store=blob_store)
    launcher = FakeLauncher(endpoint)
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    publisher = FakePublisher()
    manager = ServingManager(
        registry=registry,
        recipes=_recipes(row),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=endpoint,
        receipt_publisher=publisher,
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )
    policy = SHIPPED_POLICY if sampling is None else _policy_with_row(chair.role, sampling)
    client = ChairClient(
        manager=manager,
        identity=chair,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256=table_seal(policy),
        decoding_policy=policy,
        read_receipt=read_receipt or _default_read_receipt(chair),
    )
    return client, endpoint, blob_store, chair


def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _real_read_receipt(chair: ChairIdentity):
    """Build the receipt through the real schema, not a hand-typed dict.

    Couples this test's expectations to `common.chairs.receipts` rather than
    to inspection: a rename of either the client's drift-check field names
    (`client.py`'s `"chair"`/`"revision"`) or the receipt schema's own would
    show up here rather than passing silently on both sides.
    """

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
        started_at="2026-08-09T12:00:00+00:00",
    )
    record = receipt_record(build_receipt(chair, details))

    def read_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return record

    return read_receipt


def _request(**overrides: object) -> ChairRequest:
    fields: dict[str, object] = {
        "kind": "chat-completions",
        "messages": ({"role": "user", "content": "read the ink"},),
        "image_sha256s": (),
        "generation_declared": {},
        "generation_sent": {},
    }
    fields.update(overrides)
    return ChairRequest(**fields)  # type: ignore[arg-type]


# --- construction takes the sealed policy and selects its own chair's row ---


def _client_over(tmp_path: Path, policy: object, digest: str) -> ChairClient:
    client, *_ = _built(tmp_path)
    return ChairClient(
        manager=client._manager,
        identity=_identity(),
        tier=TIER,
        retain=lambda data: {"relative_path": "x", "sha256": digest_bytes(data)},
        decoding_config_sha256=digest,
        decoding_policy=policy,  # type: ignore[arg-type]
        read_receipt=_default_read_receipt(_identity()),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda policy: policy["chair_decoding"].pop("attestator_1"),
        lambda policy: policy["chair_decoding"]["attestator_1"].update(temperature=0.5),
        lambda policy: policy.update(schema="decoding.v4"),
    ],
    ids=["no-row", "moved-chandra-row", "legacy-schema"],
)
def test_construction_refuses_a_policy_that_is_not_a_sealed_decoding_policy(
    tmp_path: Path, mutate
) -> None:
    policy = copy.deepcopy(SHIPPED_POLICY)
    mutate(policy)
    with pytest.raises(ServingConfigurationError, match="sealed decoding policy"):
        _client_over(tmp_path, policy, DECODING_SHA)


def test_construction_refuses_a_policy_that_does_not_seal_to_its_digest(tmp_path: Path) -> None:
    policy = _policy_with_row("attestator_2", {"temperature": 0.2})
    with pytest.raises(ServingConfigurationError, match="does not seal to the decoding digest"):
        _client_over(tmp_path, policy, DECODING_SHA)


def test_each_sealed_chair_row_is_sent_exactly_and_retained(tmp_path: Path) -> None:
    """Every reading chair sends its row of the shipped table, selected by its own
    role, and records what the pinned engine samples under beside it."""

    for chair in sorted(READING_CHAIRS):
        sampling = chair_decoding(SHIPPED_POLICY, chair)
        client, endpoint, blob_store, _ = _built(tmp_path / chair, chair=_identity(role=chair))
        with client:
            endpoint.script(ScriptedAnswer(content="layout", finish_reason="stop"))
            response = client.read(_request(generation_sent={"max_tokens": 10}))
        posted = endpoint.requests[0]
        record = json.loads(
            next(data for data in blob_store.written if data != response.raw_response)
        )
        assert {key: posted[key] for key in SAMPLING_FIELDS if key in posted} == sampling
        assert posted["seed"] == 7
        assert record["generation_sent"] == recorded_wire_decimals(
            {"max_tokens": 10, **sampling, "seed": 7}
        )
        assert record["sampling_effective"] == recorded_wire_decimals(
            engine_effective_sampling(sampling)
        )


def test_the_engine_effective_values_are_recorded_beside_the_sent_ones(tmp_path: Path) -> None:
    """Churro's 1e-06 is sent as the maker wrote it; vLLM 0.30.0 samples at 0.01."""

    client, endpoint, blob_store, _ = _built(tmp_path, chair=_identity(role="attestator_3"))
    with client:
        endpoint.script(ScriptedAnswer(content="layout", finish_reason="stop"))
        response = client.read(_request())
    record = json.loads(next(data for data in blob_store.written if data != response.raw_response))
    assert endpoint.requests[0]["temperature"] == 1e-06
    assert record["generation_sent"]["temperature"]["decimal"] == "1e-06"
    assert record["sampling_effective"]["temperature"]["decimal"] == "0.01"


def test_a_sampled_temperature_and_seed_are_sent_and_retained(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(
        tmp_path,
        chair=_identity(role="perlector"),
        sampling={"temperature": 0.7, "top_p": 0.8, "top_k": 20},
    )
    with client:
        endpoint.script(ScriptedAnswer(content="layout", finish_reason="stop"))
        response = client.read(_request())
    record = json.loads(next(data for data in blob_store.written if data != response.raw_response))
    assert endpoint.requests[0]["temperature"] == 0.7
    assert endpoint.requests[0]["top_p"] == 0.8
    assert endpoint.requests[0]["top_k"] == 20
    assert endpoint.requests[0]["seed"] == 7
    assert record["generation_sent"]["temperature"] == {
        "schema": "wire-decimal.v1",
        "decimal": "0.7",
    }
    assert record["generation_sent"]["top_k"] == 20
    assert record["generation_sent"]["seed"] == 7


def test_chandra_native_capability_is_attestator_1_only_and_omits_request_seed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chair = ChairIdentity(
        **{
            **_identity().to_record(),
            "witness_adapter": "chandra.v1",
            "witness_scope": "page",
        }
    )
    client, endpoint, blob_store, _ = _built(tmp_path, chair=chair)
    intent_ref = {"relative_path": "3_attestatores/artifacts/intent.json", "sha256": "d" * 64}
    request = _request(
        generation_declared={"max_new_tokens": 12384},
        generation_sent={"chat_template_kwargs": {"enable_thinking": False}},
    )
    with client:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=4)

        def unexpected_reprepare(*_args, **_kwargs):
            raise AssertionError("a prepared native dispatch must not be rebuilt after intent")

        monkeypatch.setattr(client, "prepare_chandra_native", unexpected_reprepare)
        endpoint.script(ScriptedAnswer(content="layout", finish_reason="stop"))
        response = client.read_chandra_native(dispatch, intent_ref=intent_ref)
    expected = {
        "temperature": 0.6000000000000001,
        "top_p": 0.95,
        "top_k": 0,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
    }
    assert {field: endpoint.requests[0][field] for field in expected} == expected
    assert "seed" not in endpoint.requests[0]
    record = json.loads(next(data for data in blob_store.written if data != response.raw_response))
    assert record["schema"] == CHANDRA_NATIVE_CALL_RECORD_SCHEMA
    assert set(record) == CHANDRA_NATIVE_CALL_RECORD_FIELDS
    assert record["native_attempt_intent_ref"] == intent_ref
    assert record["sampling_effective"] == recorded_wire_decimals(expected)


def test_chandra_native_dispatch_is_a_one_use_client_minted_capability(tmp_path: Path) -> None:
    chair = ChairIdentity(
        **{
            **_identity().to_record(),
            "witness_adapter": "chandra.v1",
            "witness_scope": "page",
        }
    )
    client, endpoint, _blob_store, _ = _built(tmp_path, chair=chair)
    request = _request(
        generation_declared={"max_new_tokens": 12384},
        generation_sent={"chat_template_kwargs": {"enable_thinking": False}},
    )
    intent_ref = {"relative_path": "3_attestatores/artifacts/intent.json", "sha256": "d" * 64}
    with client:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=1)
        forged = replace(dispatch)
        with pytest.raises(ChairRequestRefusal, match="not prepared by this client"):
            client.read_chandra_native(forged, intent_ref=intent_ref)
        endpoint.script(ScriptedAnswer(content="layout", finish_reason="stop"))
        client.read_chandra_native(dispatch, intent_ref=intent_ref)
        with pytest.raises(ChairRequestRefusal, match="already used"):
            client.read_chandra_native(dispatch, intent_ref=intent_ref)
    assert len(endpoint.requests) == 1


def test_chandra_native_capability_refuses_a_different_chair(tmp_path: Path) -> None:
    chair = ChairIdentity(
        **{
            **_identity(role="attestator_2").to_record(),
            "witness_adapter": "chandra.v1",
            "witness_scope": "page",
        }
    )
    client, endpoint, _blob_store, _ = _built(tmp_path, chair=chair)
    with client, pytest.raises(ChairRequestRefusal, match="only to Attestator 1"):
        client.prepare_chandra_native(_request(), attempt_ordinal=1)
    assert endpoint.requests == []


def test_chandra_native_call_refuses_without_durable_intent_before_http(tmp_path: Path) -> None:
    chair = ChairIdentity(
        **{
            **_identity().to_record(),
            "witness_adapter": "chandra.v1",
            "witness_scope": "page",
        }
    )
    client, endpoint, blob_store, _ = _built(tmp_path, chair=chair)
    request = _request(
        generation_declared={"max_new_tokens": 12384},
        generation_sent={"chat_template_kwargs": {"enable_thinking": False}},
    )
    with client:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=1)
        with pytest.raises(ChairRequestRefusal, match="no durable attempt intent"):
            client.read_chandra_native(dispatch, intent_ref={})
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_chandra_native_transport_failure_retains_intent_and_physical_request(
    tmp_path: Path,
) -> None:
    chair = ChairIdentity(
        **{
            **_identity().to_record(),
            "witness_adapter": "chandra.v1",
            "witness_scope": "page",
        }
    )
    client, endpoint, blob_store, _ = _built(tmp_path, chair=chair)
    request = _request(
        generation_declared={"max_new_tokens": 12384},
        generation_sent={"chat_template_kwargs": {"enable_thinking": False}},
    )
    intent_ref = {"relative_path": "3_attestatores/artifacts/intent.json", "sha256": "d" * 64}
    with client:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=6)
        endpoint.script(ScriptedAnswer(transport_failure="whole-call deadline exceeded"))
        with pytest.raises(ChairTransportFailure):
            client.read_chandra_native(dispatch, intent_ref=intent_ref)
    record = json.loads(blob_store.written[0])
    assert record["schema"] == CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA
    assert set(record) == CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS
    assert record["native_attempt_intent_ref"] == intent_ref
    assert record["generation_sent"]["temperature"]["decimal"] == "0.8"
    assert record["generation_sent"]["top_p"]["decimal"] == "0.95"
    assert "seed" not in record["generation_sent"]
    assert record["transport_problem"]["request_delivery"] == "unknown"


# --- pre-send refusals: nothing is built or sent ------------------------------


def test_kind_refusal_sends_nothing(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(_request(kind="completions"))
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_generation_sent_outside_the_caller_allow_list_is_refused_by_name(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        for key, value in (
            ("model", "x"),
            ("stream", True),
            ("seed", 1),
            ("n", 2),
            ("max_completion_tokens", 10),
            ("stop", ["</s>"]),
            ("logit_bias", {"1": 5}),
            ("tempreature", 0),
            *((field, 0) for field in sorted(SAMPLING_FIELDS)),
        ):
            with pytest.raises(ChairRequestRefusal, match=repr([key])) as excinfo:
                client.read(_request(generation_sent={key: value}))
            assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_a_caller_cannot_move_a_sealed_sampling_value(tmp_path: Path) -> None:
    """The sealed row is the only source of sampling fields on the wire.

    A caller that names any sampling field -- to change one the row sets, or to
    add one it leaves to the engine -- is refused before a byte is sent.
    """

    sealed = {
        "temperature": 0.1,
        "top_k": 1,
        "top_p": 0.001,
        "repetition_penalty": 1.05,
        "min_p": 0.0,
    }
    client, endpoint, _, _ = _built(tmp_path, chair=_identity(role="attestator_2"))
    with client:
        endpoint.script(ScriptedAnswer(content="read", finish_reason="stop"))
        client.read(_request())
        for smuggled in (
            {"temperature": 0},
            {"top_k": 20},
            {"min_p": 0.05},
            {"presence_penalty": 1.5},
        ):
            with pytest.raises(ChairRequestRefusal, match="sealed decoding table"):
                client.read(_request(generation_sent=smuggled))
    assert len(endpoint.requests) == 1
    posted = endpoint.requests[0]
    assert {key: posted[key] for key in SAMPLING_FIELDS if key in posted} == sealed


def test_image_digest_drift_refused_before_any_request_is_sent(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    image_bytes = b"\x89PNG not a real image but bytes"
    data_uri = _data_uri(image_bytes)
    messages = (
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": data_uri}}],
        },
    )
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(_request(messages=messages, image_sha256s=("0" * 64,)))
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == [], "the fake recorded zero reading requests"
    assert len(blob_store) == 0


def test_image_digests_correct_and_in_order_succeed_and_are_recorded(tmp_path: Path) -> None:
    """The success direction of the digest binding: two distinct images,
    posted and recorded in exactly the order their digests were claimed."""

    client, endpoint, _, _ = _built(tmp_path)
    image_a = b"\x89PNG first image bytes"
    image_b = b"\x89PNG second, different image bytes"
    sha_a, sha_b = digest_bytes(image_a), digest_bytes(image_b)
    messages = (
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _data_uri(image_a)}},
                {"type": "image_url", "image_url": {"url": _data_uri(image_b)}},
            ],
        },
    )
    with client:
        endpoint.script(ScriptedAnswer(content="two images", finish_reason="stop"))
        response = client.read(_request(messages=messages, image_sha256s=(sha_a, sha_b)))
    assert response.parse_problem is None
    posted = endpoint.requests[0]
    posted_parts = posted["messages"][0]["content"]
    posted_bytes = [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in posted_parts
    ]
    assert [digest_bytes(data) for data in posted_bytes] == [sha_a, sha_b]


def test_image_digests_correct_but_swapped_order_are_refused(tmp_path: Path) -> None:
    """Membership is not enough: the claimed order must match the wire order."""

    client, endpoint, blob_store, _ = _built(tmp_path)
    image_a = b"\x89PNG first image bytes"
    image_b = b"\x89PNG second, different image bytes"
    sha_a, sha_b = digest_bytes(image_a), digest_bytes(image_b)
    messages = (
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _data_uri(image_a)}},
                {"type": "image_url", "image_url": {"url": _data_uri(image_b)}},
            ],
        },
    )
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(_request(messages=messages, image_sha256s=(sha_b, sha_a)))
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


# --- request digest --------------------------------------------------------


def test_request_sha256_is_the_digest_of_the_body_actually_posted(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="hello", finish_reason="stop"))
        response = client.read(_request())
    assert len(endpoint.requests) == 1
    posted_body = json.dumps(
        endpoint.requests[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert response.request_sha256 == digest_bytes(posted_body)


# --- retain, then refuse: model mismatch and non-200 --------------------------


def test_response_model_mismatch_refuses_with_the_body_retained_and_named(
    tmp_path: Path,
) -> None:
    """Retention is not attribution.

    A body from another model is not this chair's evidence and never becomes
    a reading: no `ChairResponse` comes back. The bytes are retained by their
    own digest, so a reader can see what actually arrived instead of taking
    the refusal's word for it.

    And what the refusal does *not* carry: the foreign reading itself. The
    model's name is a field this check compared and says so; the text that
    model produced is a reading from somewhere else, and a reading from
    somewhere else does not enter an exception message to be logged and quoted
    onward. It is on disk, by its digest, which the refusal names.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    foreign = ScriptedAnswer(
        content="A READING NO CHAIR HERE ASKED FOR",
        finish_reason="stop",
        model="someone-elses-model",
    )
    with client:
        endpoint.script(foreign)
        with pytest.raises(ChairResponseRefusal) as excinfo:
            client.read(_request())
        assert excinfo.value.code == "CHAIR_RESPONSE_MODEL_MISMATCH"
    assert len(blob_store) == 2
    assert b"someone-elses-model" in blob_store.written[0]
    assert digest_bytes(blob_store.written[0]) in excinfo.value.detail
    assert "someone-elses-model" in excinfo.value.detail
    assert "A READING NO CHAIR HERE ASKED FOR" not in excinfo.value.detail
    assert str(len(blob_store.written[0])) in excinfo.value.detail
    call_record = json.loads(blob_store.written[1])
    assert call_record["parse_problem"] == "CHAIR_RESPONSE_MODEL_MISMATCH"
    assert call_record["response_status"] == 200
    assert call_record["served_model_id"] == "served-alias"
    assert call_record["response_model"] == "someone-elses-model"
    assert excinfo.value.raw_response_ref == call_record["raw_response_ref"]
    assert excinfo.value.call_record_ref["sha256"] == digest_bytes(blob_store.written[1])
    assert excinfo.value.request_sha256 == call_record["request_sha256"]
    assert excinfo.value.receipt_ref == call_record["receipt_ref"]
    assert excinfo.value.served_model_id == call_record["served_model_id"]


def test_a_non_200_body_is_retained_before_the_refusal_and_quoted_in_it(
    tmp_path: Path,
) -> None:
    """The one artefact a rented card exists to produce must not be discarded.

    When vLLM refuses a request it says why in the body of a non-200, and that
    sentence is the whole diagnostic: nothing is lost silently.
    """

    body = (
        b'{"object":"error","message":"This model\'s maximum context length is 2048 tokens. '
        b'However, you requested 3994 tokens.","type":"BadRequestError","code":400}'
    )
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(status=400, body=body))
        with pytest.raises(ChairResponseRefusal) as excinfo:
            client.read(_request())
        assert excinfo.value.code == "CHAIR_RESPONSE_HTTP_ERROR"
    assert blob_store.written[0] == body
    assert len(blob_store.written) == 2
    assert blob_store.has(digest_bytes(body))
    assert "HTTP 400" in excinfo.value.detail
    assert "maximum context length is 2048" in excinfo.value.detail
    assert digest_bytes(body) in excinfo.value.detail
    call_record = json.loads(blob_store.written[1])
    assert call_record["parse_problem"] == "CHAIR_RESPONSE_HTTP_ERROR"
    assert call_record["response_status"] == 400
    assert call_record["response_model"] is None
    assert excinfo.value.raw_response_ref == call_record["raw_response_ref"]
    assert excinfo.value.call_record_ref["sha256"] == digest_bytes(blob_store.written[1])


def test_a_long_refused_body_is_retained_whole_and_previewed_short(tmp_path: Path) -> None:
    """The detail carries a bounded head; the blob carries all of it."""

    body = b'{"error":"' + b"x" * 4000 + b'"}'
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(status=502, body=body))
        with pytest.raises(ChairResponseRefusal) as excinfo:
            client.read(_request())
    assert blob_store.written[0] == body
    assert len(blob_store.written) == 2
    assert len(excinfo.value.detail) < 1000
    assert f"first 512 of {len(body)} bytes" in excinfo.value.detail


def test_transport_timeout_retains_the_known_request_and_explicit_response_uncertainty(
    tmp_path: Path,
) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(transport_failure="whole-call deadline exceeded"))
        with pytest.raises(ChairTransportFailure) as excinfo:
            client.read(_request())
    assert len(endpoint.requests) == 1
    assert len(blob_store.written) == 1
    record = json.loads(blob_store.written[0])
    assert record["schema"] == CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA
    assert set(record) == CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS
    assert record["request_sha256"] == excinfo.value.request_sha256
    assert record["image_sha256s"] == []
    assert record["generation_sent"] == recorded_wire_decimals(
        {**chair_decoding(SHIPPED_POLICY, "attestator_1"), "seed": 7}
    )
    assert record["generation_declared"] == {}
    assert record["capacity"] is None
    assert record["receipt_ref"] == excinfo.value.receipt_ref
    for field in (
        "raw_response_ref",
        "response_sha256",
        "response_status",
        "response_model",
        "finish_reason",
        "usage",
        "parse_problem",
    ):
        assert record[field] is None
    assert record["transport_problem"] == {
        "schema": "chair-transport-problem.v1",
        "code": "ENDPOINT_UNAVAILABLE",
        "detail": "whole-call deadline exceeded",
        "definitively_absent": False,
        "request_delivery": "unknown",
        "response_completion": "unknown",
    }
    assert excinfo.value.raw_response_ref is None
    assert excinfo.value.response_completion == "unknown"
    assert excinfo.value.call_record_ref["sha256"] == digest_bytes(blob_store.written[0])


# --- raw bytes retained before parsing; malformed body never raises -----------


def test_malformed_body_retains_raw_blob_and_yields_parse_problem_never_raises(
    tmp_path: Path,
) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(body=b"not json at all"))
        response = client.read(_request())
    assert response.content is None
    assert response.parse_problem == "CHAIR_RESPONSE_INVALID"
    assert response.finish_reason is None
    assert len(blob_store) >= 1
    assert b"not json at all" in blob_store.written[0]


def test_body_naming_no_model_at_all_is_recorded_invalid_not_mismatched(tmp_path: Path) -> None:
    """A body that names no model at all is a malformed body, not evidence
    from a foreign source — `parse_openai_reading`'s own comparison cannot
    tell the two apart (``None != expected_model_id``), so the client must
    remap its ``CHAIR_RESPONSE_MODEL_MISMATCH`` to name the true problem."""

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        body = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
        endpoint.script(ScriptedAnswer(body=body))
        response = client.read(_request())
    assert response.content is None
    assert response.parse_problem == "CHAIR_RESPONSE_INVALID"
    assert len(blob_store) >= 1
    assert body in blob_store.written


def test_content_missing_retains_and_yields_parse_problem_never_raises(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        body = json.dumps({"model": "served-alias", "choices": [{"message": {}}]}).encode()
        endpoint.script(ScriptedAnswer(body=body))
        response = client.read(_request())
    assert response.content is None
    assert response.parse_problem == "CHAIR_RESPONSE_CONTENT_MISSING"


# --- response-as-arrival -------------------------------------------------------


def test_response_as_arrival_raw_bytes_exist_before_the_next_request(tmp_path: Path) -> None:
    """The fake asserts, in its own POST handler, that a read's raw response
    was already retained before the next read's request reaches it."""

    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="served-alias"),
        chair,
    )
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(
        served_model_id="served-alias",
        blob_store=blob_store,
        assert_retained_before_next_request=True,
    )
    launcher = FakeLauncher(endpoint)
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    manager = ServingManager(
        registry=registry,
        recipes=_recipes(row),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=endpoint,
        receipt_publisher=FakePublisher(),
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )
    client = ChairClient(
        manager=manager,
        identity=chair,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256=DECODING_SHA,
        decoding_policy=SHIPPED_POLICY,
        read_receipt=_default_read_receipt(chair),
    )
    with client:
        endpoint.script(ScriptedAnswer(content="a", finish_reason="stop"))
        client.read(_request())
        endpoint.script(ScriptedAnswer(content="b", finish_reason="stop"))
        # If the client had not already retained the first response's raw
        # bytes, the fake's own POST handler raises here, before this second
        # read's request is even answered.
        client.read(_request())
    assert len(blob_store) >= 4  # two raw responses plus two call records


def test_raw_bytes_are_retained_even_when_parsing_itself_blows_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only test that can distinguish retain-before-parse from
    retain-after-parse: force the parse step to fail with something that is
    not even a ``ChairResponseRefusal``, and prove the raw bytes were already
    on disk before that call was ever made."""

    client, endpoint, blob_store, _ = _built(tmp_path)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("parser blew up")

    monkeypatch.setattr("operations.serving.client.parse_openai_reading", explode)
    with client:
        endpoint.script(ScriptedAnswer(content="hello", finish_reason="stop"))
        with pytest.raises(RuntimeError, match="parser blew up"):
            client.read(_request())
    assert len(blob_store) == 1
    payload = json.loads(blob_store.written[0])
    assert payload["choices"][0]["message"]["content"] == "hello"


# --- chair-call-record.v1: exact closed field set, canonical bytes ------------


def test_call_record_has_the_exact_closed_field_set_and_canonical_bytes(tmp_path: Path) -> None:
    client, endpoint, blob_store, chair = _built(tmp_path)
    exact_content = "  L'an mil sept cent \n trente\t "
    with client:
        endpoint.script(
            ScriptedAnswer(
                content=exact_content,
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            )
        )
        response = client.read(_request(generation_declared={"top_k": 1}))
    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    record = json.loads(record_bytes)
    assert set(record) == CHAIR_CALL_RECORD_FIELDS
    assert record["schema"] == CHAIR_CALL_RECORD_SCHEMA
    assert record["chair"] == chair.role
    assert record["resolved_revision"] == chair.receipt_revision
    assert record["resolved_identity"] == chair.to_record()
    assert record["serving_recipe"] == chair.serving_recipe
    assert record["served_model_id"] == "served-alias"
    assert record["response_model"] == "served-alias"
    assert record["receipt_ref"] == dict(response.receipt_ref)
    assert record["launch_audit_ref"] == dict(response.launch_audit_ref)
    assert record["decoding_config_sha256"] == DECODING_SHA
    assert record["kind"] == "chat-completions"
    assert record["request_sha256"] == response.request_sha256
    assert record["image_sha256s"] == []
    assert record["generation_sent"] == recorded_wire_decimals(
        {**chair_decoding(SHIPPED_POLICY, "attestator_1"), "seed": 7}
    )
    assert record["sampling_effective"] == recorded_wire_decimals(
        {**chair_decoding(SHIPPED_POLICY, "attestator_1"), "top_p": 1.0}
    )
    assert record["generation_declared"] == {"top_k": 1}
    assert record["raw_response_ref"] == dict(response.raw_response_ref)
    assert record["response_sha256"] == response.response_sha256
    assert record["response_status"] == 200
    assert record["finish_reason"] == "stop"
    assert record["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert response.usage == record["usage"]
    assert record["parse_problem"] is None
    # No caller stated one here, and the client never invents one.
    assert record["capacity"] is None
    # The exact bytes the engine returned, never stripped,
    # cased, or trimmed — carried verbatim into both the response and the
    # blob the record was built alongside.
    assert response.content == exact_content
    assert json.loads(response.raw_response)["choices"][0]["message"]["content"] == exact_content
    # Canonical: re-serializing the parsed record reproduces the stored bytes.
    assert canonical_bytes(record) == record_bytes
    assert response.call_record_ref["sha256"] == digest_bytes(record_bytes)


def test_a_callers_capacity_record_reaches_the_call_record_verbatim(tmp_path: Path) -> None:
    """The client carries the arithmetic; it neither computes nor checks it.

    Only the caller knows which prompt and which answer shape a call is, so
    whether a request fits its sealed row is decided in the stage
    (`common/request_capacity.py`). What the client owes is that the record
    lands on the retained call record, beside the request it admitted, so every
    stage can reach it through the reference it already keeps.
    """

    capacity = {
        "schema": "verbatus-request-capacity.v1",
        "need": 3619,
        "headroom": 4573,
        "fits": True,
    }
    client, endpoint, blob_store, _chair = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="ok", finish_reason="stop"))
        response = client.read(_request(capacity=capacity))
    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    assert json.loads(record_bytes)["capacity"] == capacity


@pytest.mark.parametrize(
    ("usage", "findings"),
    [
        (
            {
                "prompt_tokens": 1040,
                "prompt_tokens_details": {"multimodal_tokens": {"image": 1000}},
            },
            [],
        ),
        # The engine resized the page differently: the row's pixel bounds never reached it.
        (
            {
                "prompt_tokens": 4040,
                "prompt_tokens_details": {"multimodal_tokens": {"image": 4000}},
            },
            ["image-tokens-differ", "prompt-tokens-above-admitted"],
        ),
        ({"prompt_tokens": 1040}, ["image-tokens-unreported"]),
    ],
)
def test_the_engine_s_token_counts_are_reconciled_on_the_record_without_touching_the_reading(
    tmp_path: Path, usage: dict[str, object], findings: list[str]
) -> None:
    usage = {"completion_tokens": 3, "total_tokens": 3 + int(usage["prompt_tokens"]), **usage}
    capacity = {
        "schema": "verbatus-request-capacity.v1",
        "image_prompt_tokens": 1000,
        "prompt_tokens": 50,
        "fits": True,
    }
    client, endpoint, blob_store, _chair = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="la page", finish_reason="stop", usage=usage))
        response = client.read(_request(capacity=capacity))
    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    reconciliation = json.loads(record_bytes)["usage_reconciliation"]

    assert reconciliation["expected_image_tokens"] == 1000
    assert reconciliation["admitted_prompt_tokens"] == 1050
    assert reconciliation["findings"] == findings
    assert (response.content, response.parse_problem) == ("la page", None)


def test_a_call_without_a_capacity_record_has_nothing_to_reconcile(tmp_path: Path) -> None:
    client, endpoint, blob_store, _chair = _built(tmp_path)
    with client:
        endpoint.script(
            ScriptedAnswer(
                content="ok",
                finish_reason="stop",
                usage={"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
            )
        )
        response = client.read(_request())
    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    assert json.loads(record_bytes)["usage_reconciliation"] is None


def test_a_capacity_record_mutated_after_construction_does_not_reach_the_call_record(
    tmp_path: Path,
) -> None:
    """The retained arithmetic is the arithmetic the request was admitted on."""

    capacity: dict[str, object] = {
        "schema": "verbatus-request-capacity.v1",
        "images": [{"width": 2480, "height": 3508, "image_prompt_tokens": 1715}],
        "image_prompt_tokens": 1715,
        "prompt_tokens": 790,
        "answer_budget": 216,
        "need": 2721,
        "headroom": 13663,
        "fits": True,
    }
    admitted = copy.deepcopy(capacity)
    request = _request(capacity=capacity)

    # The caller still holds the object it passed in, and rewrites it.
    images = capacity["images"]
    assert isinstance(images, list)
    images[0]["image_prompt_tokens"] = 1
    images.append({"width": 1, "height": 1, "image_prompt_tokens": 1})
    capacity["fits"] = False

    assert request.capacity is not None
    assert thawed_json(request.capacity) == admitted
    # And the request's own view refuses a write rather than taking one.
    with pytest.raises(TypeError):
        request.capacity["images"][0]["image_prompt_tokens"] = 1  # type: ignore[index]

    client, endpoint, blob_store, _chair = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="ok", finish_reason="stop"))
        response = client.read(request)
    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    assert json.loads(record_bytes)["capacity"] == admitted


def test_a_capacity_record_the_canonical_writer_cannot_hold_is_refused_at_construction(
    tmp_path: Path,
) -> None:
    """Refused where the caller can still see it, not inside serialization."""

    with pytest.raises(ChairRequestRefusal):
        _request(capacity={"schema": "verbatus-request-capacity.v1", "headroom": 1.5})
    with pytest.raises(ChairRequestRefusal):
        _request(capacity={"schema": "verbatus-request-capacity.v1", 7: "no"})


# --- a vendor's float decoding values, recorded exactly as they were sent -----


# DAI's carried `generation_config.json`, the values `pipeline/3_attestatores/
# feeding.py::dai_generation` returns. Retyped here rather than imported: a
# stage's carried vendor evidence is not this module's to depend on, and what
# is being proven is that *these numbers* survive the record, whoever declares
# them.
_DAI_GENERATION: dict[str, object] = {
    "bos_token_id": 151643,
    "do_sample": True,
    "eos_token_id": [151645, 151643],
    "pad_token_id": 151643,
    "repetition_penalty": 1.05,
    "temperature": 0.1,
    "top_k": 1,
    "top_p": 0.001,
    "transformers_version": "5.2.0",
}


def test_a_vendors_float_generation_values_are_recorded_as_the_wire_carried_them(
    tmp_path: Path,
) -> None:
    """A float in a generation view is recorded as the exact decimal text found in the posted bytes.

    The call record goes through `canonical_bytes`, which refuses floats, so a
    live `dai.v1` request records each float as the decimal text the request
    body itself contains, tagged `wire-decimal.v1`. This test reads that text
    back out of the *bytes the endpoint actually received* rather than out of
    the client's own Python values.
    """

    client, endpoint, blob_store, _ = _built(tmp_path, chair=_identity(role="attestator_2"))
    with client:
        endpoint.script(ScriptedAnswer(content="texte transcrit", finish_reason="stop"))
        response = client.read(_request(generation_declared=_DAI_GENERATION))

    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    record = json.loads(record_bytes)
    assert canonical_bytes(record) == record_bytes

    # The body the fake endpoint received, re-serialized exactly as
    # `test_request_sha256_is_the_digest_of_the_body_actually_posted` does, so
    # the comparison below is against the wire and not against the client.
    posted = endpoint.requests[0]
    posted_body = json.dumps(
        posted, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert digest_bytes(posted_body) == record["request_sha256"]

    for key, wire_value in (("repetition_penalty", 1.05), ("top_p", 0.001)):
        recorded = record["generation_sent"][key]
        assert recorded == {"schema": "wire-decimal.v1", "decimal": json.dumps(wire_value)}
        # The recorded text is literally in the bytes that were posted, as the
        # value of that key — not merely a number that reads back equal.
        assert f'"{key}":{recorded["decimal"]}'.encode("utf-8") in posted_body
        assert float(recorded["decimal"]) == wire_value
        assert posted[key] == wire_value
    # Integers, booleans, strings and lists are untouched: only a float needs
    # the decimal form, and dressing the rest in it would lose the distinction.
    assert record["generation_sent"]["top_k"] == 1
    assert record["generation_declared"]["do_sample"] is True
    assert record["generation_declared"]["eos_token_id"] == [151645, 151643]
    assert record["generation_declared"]["transformers_version"] == "5.2.0"
    assert record["generation_declared"]["temperature"] == {
        "schema": "wire-decimal.v1",
        "decimal": "0.1",
    }


def test_a_declared_float_that_is_never_sent_is_still_recorded_exactly(tmp_path: Path) -> None:
    """`generation_declared` is evidence, not traffic, and gets the same care.

    A declared value never reaches the wire by being declared -- only the
    sealed row does -- but the record must still say what the vendor declared,
    to the digit.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="x", finish_reason="stop"))
        response = client.read(_request(generation_declared={"temperature": 0.1}))
    record = json.loads(next(data for data in blob_store.written if data != response.raw_response))
    assert record["generation_declared"] == {
        "temperature": {"schema": "wire-decimal.v1", "decimal": "0.1"}
    }
    # The posted body carries the sealed row's 0.0, never the declared 0.1.
    assert endpoint.requests[0]["temperature"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    ("view", "field"),
    [("generation_sent", "max_tokens"), ("generation_declared", "temperature")],
)
def test_a_nonfinite_generation_value_is_refused_before_anything_is_sent(
    tmp_path: Path, value: float, view: str, field: str
) -> None:
    """NaN and Infinity are not JSON; Python's encoder emits them anyway.

    Each value sits in a field the caller may otherwise name, so only the
    non-finite check can refuse it.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        with pytest.raises(ChairRequestRefusal, match="not a finite number") as excinfo:
            client.read(_request(**{view: {field: value}}))
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
        assert f"{view}[{field!r}]" in str(excinfo.value)
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_a_declared_view_that_collides_with_the_decimal_form_is_refused_not_mangled(
    tmp_path: Path,
) -> None:
    """The one shape the tagged form cannot represent, named rather than lost."""

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(
                _request(
                    generation_declared={
                        "vendor_note": {"schema": "wire-decimal.v1", "decimal": "1.05"}
                    }
                )
            )
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_a_declared_view_with_a_malformed_decimal_form_is_refused_not_crashed(
    tmp_path: Path,
) -> None:
    """A tagged form whose ``decimal`` is not a number is the vendor's malformed

    evidence, not the client's to trust or to raise a bare exception over. It
    fails the same round-trip the well-formed collision above fails, so it
    surfaces as the same named refusal instead of an untyped ``ValueError``
    escaping the client's refusal boundary.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(
                _request(
                    generation_declared={
                        "vendor_note": {"schema": "wire-decimal.v1", "decimal": "abc"}
                    }
                )
            )
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


# --- receipt re-verification on __enter__ -------------------------------------


def test_the_tree_receipt_reader_is_wired_bare_with_no_stage_side_converter(
    tmp_path: Path,
) -> None:
    """`RunTree.read_run_receipt`'s own rule, satisfied by the client itself."""

    chair = _identity()
    seen: list[object] = []

    def tree_shaped_read_receipt(reference: object) -> dict[str, object]:
        seen.append(reference)
        if type(reference) is not dict or set(reference) != {"relative_path", "sha256"}:
            raise TypeError(
                "run receipt reference must contain exactly relative_path and sha256, "
                f"as a plain dict; got {type(reference).__name__}"
            )
        return {
            "chair": chair.role,
            "source": chair.source,
            "resolved": chair.source_reference,
            "revision": chair.receipt_revision,
            "revision_kind": chair.receipt_revision_kind,
            "digest_manifest": chair.digest_manifest,
        }

    client, _, _, _ = _built(tmp_path, chair=chair, read_receipt=tree_shaped_read_receipt)
    with client as entered:
        assert entered.handle is not None
    assert seen and type(seen[0]) is dict


def test_receipt_drift_on_enter_refuses_and_sends_nothing(tmp_path: Path) -> None:
    def wrong_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {"chair": "someone-else", "revision": "0" * 40}

    client, endpoint, blob_store, _ = _built(tmp_path, read_receipt=wrong_receipt)
    with pytest.raises(ReceiptDriftRefusal) as excinfo:
        with client:
            pass
    assert excinfo.value.code == "CHAIR_RECEIPT_DRIFT"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_receipt_match_enters_cleanly(tmp_path: Path) -> None:
    chair = _identity()
    # Built through the real receipt schema (`common.chairs.receipts`), not a
    # hand-typed `{"chair": ..., "revision": ...}` — this couples the drift
    # check's field names to the real schema rather than to inspection.
    client, _, _, _ = _built(tmp_path, chair=chair, read_receipt=_real_read_receipt(chair))
    with client as entered:
        assert entered.handle is not None


def test_receipt_reader_failure_stops_the_started_service_and_preserves_the_failure(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("the published receipt could not be read")

    def unreadable_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        raise failure

    client, endpoint, blob_store, _ = _built(tmp_path, read_receipt=unreadable_receipt)
    with pytest.raises(RuntimeError) as excinfo:
        with client:
            pass

    assert excinfo.value is failure
    assert endpoint._available() is False
    assert endpoint.requests == []
    assert len(blob_store) == 0


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (("digest_manifest", "d" * 64), ("resolved", "example/a-different-attestator-1")),
)
def test_receipt_identity_drift_stops_before_http_even_when_chair_and_revision_match(
    tmp_path: Path, field: str, wrong_value: str
) -> None:
    chair = _identity()
    read_real_receipt = _real_read_receipt(chair)

    def drifted_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        receipt = read_real_receipt(reference)
        receipt[field] = wrong_value
        return receipt

    client, endpoint, blob_store, _ = _built(tmp_path, chair=chair, read_receipt=drifted_receipt)
    with pytest.raises(ReceiptDriftRefusal, match="exact configured identity"):
        with client:
            pass

    assert endpoint._available() is False
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_receipt_revision_drift_alone_refuses(tmp_path: Path) -> None:
    """A chair repointed to a different revision under the same role must
    still refuse — the ``chair`` clause alone must not carry the check."""

    chair = _identity()

    def right_chair_wrong_revision(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {"chair": chair.role, "revision": "0" * 40}

    client, endpoint, blob_store, _ = _built(
        tmp_path, chair=chair, read_receipt=right_chair_wrong_revision
    )
    with pytest.raises(ReceiptDriftRefusal) as excinfo:
        with client:
            pass
    assert excinfo.value.code == "CHAIR_RECEIPT_DRIFT"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_receipt_drift_refusal_survives_an_unverifiable_shutdown(tmp_path: Path) -> None:
    """The drift diagnosis must not be replaced by a shutdown failure."""

    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="served-alias"),
        chair,
    )
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(
        served_model_id="served-alias", blob_store=blob_store, sticky_after_stop=True
    )
    launcher = FakeLauncher(endpoint)
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    manager = ServingManager(
        registry=registry,
        recipes=_recipes(row),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=endpoint,
        receipt_publisher=FakePublisher(),
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
        shutdown_timeout_seconds=0.01,
    )

    def wrong_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {"chair": "someone-else", "revision": "0" * 40}

    client = ChairClient(
        manager=manager,
        identity=chair,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256=DECODING_SHA,
        decoding_policy=SHIPPED_POLICY,
        read_receipt=wrong_receipt,
    )
    with pytest.raises(ReceiptDriftRefusal) as excinfo:
        with client:
            pass
    assert excinfo.value.code == "CHAIR_RECEIPT_DRIFT"
    assert isinstance(excinfo.value.__cause__, ServiceStopError)

    # The unstopped service is still reachable through its manager: once the
    # endpoint goes away, recovery stops it and frees the card for the next start.
    endpoint.sticky_after_stop = False
    manager.recover()
    FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(chair).release()


def test_a_failed_stop_on_exit_can_be_retried_through_the_client(tmp_path: Path) -> None:
    client, endpoint, _, chair = _built(tmp_path)
    client._manager.shutdown_timeout_seconds = 0.01
    client.__enter__()
    endpoint.sticky_after_stop = True
    with pytest.raises(ServiceStopError):
        client.__exit__(None, None, None)
    # A service whose stop failed is kept for the retry, never read from.
    with pytest.raises(ServingConfigurationError, match="being stopped"):
        _ = client.handle

    endpoint.sticky_after_stop = False
    client.__exit__(None, None, None)
    with pytest.raises(ServingConfigurationError, match="no active service"):
        _ = client.handle
    # The retried stop was verified, so the card's lease is free again.
    FileResidencyLease(tmp_path / "pod-gpu.lock").acquire(chair).release()


def test_a_failed_stop_never_hides_the_error_that_ended_the_block(tmp_path: Path) -> None:
    client, endpoint, _, _chair = _built(tmp_path)
    client._manager.shutdown_timeout_seconds = 0.01

    with pytest.raises(RuntimeError, match="page 3 could not be read") as caught:
        with client:
            endpoint.sticky_after_stop = True
            raise RuntimeError("page 3 could not be read")

    assert isinstance(caught.value.__cause__, ServiceStopError)


# --- never a retry ------------------------------------------------------------


def test_no_retry_ever_one_request_per_read(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(status=500, body=b"{}"))
        with pytest.raises(ChairResponseRefusal):
            client.read(_request())
        assert len(endpoint.requests) == 1
        # A following read is a distinct call the caller made, not a retry the
        # client performed on its own; it consumes the next scripted answer.
        endpoint.script(ScriptedAnswer(content="ok", finish_reason="stop"))
        client.read(_request())
        assert len(endpoint.requests) == 2


# --- serving_mode_for ----------------------------------------------------------


def test_serving_mode_all_fixture_is_fixture(tmp_path: Path) -> None:
    chair = _identity()
    recipes = _recipes(_fixture_row(recipe=chair.serving_recipe, chair=chair.role))
    assert serving_mode_for(recipes, chair, None) == "fixture"
    assert serving_mode_for(recipes, chair, TIER) == "fixture"


@pytest.mark.parametrize("tier", [None, "tier-does-not-exist"])
def test_serving_mode_vllm_without_matching_tier_is_unresolved(
    tmp_path: Path, tier: str | None
) -> None:
    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="x"), chair
    )
    recipes = _recipes(row)
    with pytest.raises(ServingModeRefusal) as excinfo:
        serving_mode_for(recipes, chair, tier)
    assert excinfo.value.code == "SERVING_MODE_UNRESOLVED"


def test_serving_mode_vllm_with_tier_is_live(tmp_path: Path) -> None:
    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="x"), chair
    )
    recipes = _recipes(row)
    assert serving_mode_for(recipes, chair, TIER) == "live"


def test_serving_mode_unsupported_profile_refuses_by_its_own_reason(tmp_path: Path) -> None:
    chair = _identity()
    row = _unsupported_row(
        recipe=chair.serving_recipe, chair=chair.role, reason="no engine implements this yet"
    )
    recipes = _recipes(row)
    with pytest.raises(ServingModeRefusal) as excinfo:
        serving_mode_for(recipes, chair, TIER)
    assert excinfo.value.code == "SERVING_MODE_UNSUPPORTED"
    assert excinfo.value.detail == "no engine implements this yet"


def test_serving_mode_mixed_kinds_for_one_chair_refuse(tmp_path: Path) -> None:
    chair = _identity()
    live_row = _seal(
        _vllm_row(
            recipe=chair.serving_recipe, chair=chair.role, served_model_id="x", tier="tier-a"
        ),
        chair,
    )
    fixture_row = _fixture_row(recipe=chair.serving_recipe, chair=chair.role, tier="tier-b")
    recipes = _recipes(live_row, fixture_row)
    # The chair is not all-fixture (a live row exists), so a tier is required
    # and the fixture tier itself must refuse rather than silently answer
    # "fixture" for a catalogue that is live elsewhere.
    with pytest.raises(ServingModeRefusal):
        serving_mode_for(recipes, chair, "tier-b")
    assert serving_mode_for(recipes, chair, "tier-a") == "live"


def test_serving_mode_mixed_kinds_name_the_posture_the_other_tiers_actually_hold() -> None:
    """The refusal reports what is at the other tiers, never a guess of "live".

    A chair can be fixture at one tier and *unsupported* at another without a
    live row anywhere: an operator told "another tier is live" would go looking
    for a launch shape that does not exist. The message names the kind it can
    see, and says so in the plural when the other tiers disagree among
    themselves.
    """

    chair = _identity()
    fixture_row = _fixture_row(recipe=chair.serving_recipe, chair=chair.role, tier="tier-a")
    unsupported_row = _unsupported_row(
        recipe=chair.serving_recipe,
        chair=chair.role,
        tier="tier-b",
        reason="no native engine implements this",
    )
    with pytest.raises(ServingModeRefusal) as excinfo:
        serving_mode_for(_recipes(fixture_row, unsupported_row), chair, "tier-a")
    assert "another tier is unsupported" in excinfo.value.detail

    live_row = _seal(
        _vllm_row(
            recipe=chair.serving_recipe, chair=chair.role, served_model_id="x", tier="tier-c"
        ),
        chair,
    )
    with pytest.raises(ServingModeRefusal) as excinfo:
        serving_mode_for(_recipes(fixture_row, unsupported_row, live_row), chair, "tier-a")
    assert "other tiers are ['live', 'unsupported']" in excinfo.value.detail


# --- the Perlector's streamed reading, stopped on a repetition loop ---

GUARD = perlector_loop_guard(SHIPPED_POLICY)
# Synthetic index rows: one surname for long runs, each row its own given name and folio.
DENSE_ROWS = [
    f"Tremblay, {name} f. {12 + index // 4}"
    for index, name in enumerate(["Jean", "Marie", "Joseph", "Louise"] * 30)
]


def _streamed(tmp_path: Path, content: str):
    client, endpoint, blob_store, _ = _built(tmp_path, chair=_identity(role="perlector"))
    with client:
        endpoint.script(ScriptedAnswer(content=content, finish_reason="stop"))
        response = client.read(_request(generation_sent={"max_tokens": 99}, loop_guard=GUARD))
    record = json.loads((blob_store.root / response.call_record_ref["relative_path"]).read_bytes())
    return response, record, endpoint, blob_store


def _reread(blob_store: FakeBlobStore, response) -> dict[str, object]:
    """The reply read again from the retained bytes, as a later stage reads it."""
    return page_path.retained_reply(
        lambda path: (blob_store.root / path).read_bytes(),
        {
            "raw_response_ref": dict(response.raw_response_ref),
            "call_record_ref": dict(response.call_record_ref),
            "served_model_id": response.served_model_id,
        },
        SERVING_READER,
    )


def test_a_dense_index_reply_streams_through_untouched(tmp_path: Path) -> None:
    content = "\n".join(DENSE_ROWS) + "\n"
    response, record, endpoint, blob_store = _streamed(tmp_path, content)
    posted = endpoint.requests[0]
    assert posted["stream"] is True and posted["stream_options"] == {"include_usage": True}
    assert posted["max_tokens"] == 99
    assert endpoint.streams_stopped == 0
    assert (response.content, response.finish_reason, response.loop_stop) == (content, "stop", None)
    assert response.raw_response.endswith(b"data: [DONE]\n\n")
    assert set(record) == CHAIR_STREAM_CALL_RECORD_FIELDS
    assert record["schema"] == CHAIR_STREAM_CALL_RECORD_SCHEMA
    assert record["stream"] == {"schema": "chair-stream.v1", "loop_guard": GUARD, "stopped": None}
    assert record["response_model"] == "served-alias"
    assert record["response_sha256"] == digest_bytes(response.raw_response)
    assert record["request_sha256"] == digest_bytes(
        request_body(
            {**{k: v for k, v in posted.items() if k not in {"model", "stream", "stream_options"}}},
            model_id="served-alias",
            seed=7,
            deterministic=False,
            stream=True,
        )
    )
    assert _reread(blob_store, response) == {
        "content": content,
        "finish_reason": "stop",
        "stop_reason": "stop",
    }


@pytest.mark.parametrize(
    ("looped", "finding"),
    [
        (
            ["Tremblay, Jean f. 12"] * 200,
            {"kind": "line", "block_lines": 1, "repeats": 30, "line": 120 + 30},
        ),
        (
            ["Roy, Pierre f. 40", "Roy, Marie f. 40", "Gagnon, Jean f. 41"] * 80,
            {"kind": "block", "block_lines": 3, "repeats": 10, "line": 120 + 30},
        ),
    ],
    ids=["exact-line", "three-line-block"],
)
def test_a_looping_reply_is_stopped_early_and_retained_as_received(
    tmp_path: Path, looped: list[str], finding: dict[str, object]
) -> None:
    content = "\n".join(DENSE_ROWS + looped) + "\n"
    response, record, endpoint, blob_store = _streamed(tmp_path, content)
    assert endpoint.streams_stopped == 1
    assert dict(response.loop_stop) == finding
    # Stopped at the line that reached the threshold: nothing after it was read.
    assert response.content == "\n".join((DENSE_ROWS + looped)[: finding["line"]]) + "\n"
    assert response.finish_reason is None and response.parse_problem is None
    assert b"[DONE]" not in response.raw_response
    assert record["schema"] == CHAIR_STREAM_CALL_RECORD_SCHEMA
    assert record["stream"]["stopped"] == finding
    assert record["finish_reason"] is None
    assert record["raw_response_ref"] == dict(response.raw_response_ref)
    assert record["response_sha256"] == digest_bytes(response.raw_response)
    assert (blob_store.root / response.raw_response_ref["relative_path"]).read_bytes() == (
        response.raw_response
    )
    reread = _reread(blob_store, response)
    assert reread == {
        "content": response.content,
        "finish_reason": None,
        "stop_reason": "repetition-loop",
    }


@pytest.mark.parametrize(
    ("content", "forged", "refusal"),
    [
        # A finished reply its record claims was stopped: the reply shows no loop.
        (
            "\n".join(DENSE_ROWS) + "\n",
            {"kind": "line", "block_lines": 1, "repeats": 30, "line": 30},
            "shows the repetition loop None",
        ),
        # A stopped reply its record claims ran to its end: it has no [DONE].
        ("\n".join(DENSE_ROWS + ["same"] * 40) + "\n", None, "without \\[DONE\\]"),
    ],
)
def test_a_call_record_that_misstates_the_loop_is_refused_on_reading_again(
    tmp_path: Path, content: str, forged: dict[str, object] | None, refusal: str
) -> None:
    response, record, _endpoint, blob_store = _streamed(tmp_path, content)
    record["stream"]["stopped"] = forged
    with pytest.raises(ContractError, match=refusal):
        page_path.retained_reply(
            lambda path: (blob_store.root / path).read_bytes(),
            {
                "raw_response_ref": dict(response.raw_response_ref),
                "call_record_ref": blob_store.retain(canonical_bytes(record)),
                "served_model_id": response.served_model_id,
            },
            SERVING_READER,
        )


@pytest.mark.parametrize(
    ("role", "guard"),
    [
        ("reconstructor", GUARD),
        ("perlector", {**GUARD, "loop_line_repeats": 31}),
    ],
)
def test_only_the_perlector_streams_and_only_under_its_sealed_guard(
    tmp_path: Path, role: str, guard: dict[str, int]
) -> None:
    client, endpoint, _blob_store, _ = _built(tmp_path, chair=_identity(role=role))
    with client:
        with pytest.raises(ChairRequestRefusal, match="sealed repetition-loop guard"):
            client.read(_request(loop_guard=guard))
    assert endpoint.requests == []


WITNESS_GUARD = witness_loop_guard(SHIPPED_POLICY, "attestator_3")


@pytest.mark.parametrize("role", ["attestator_2", "attestator_3"])
def test_a_witness_reply_streams_under_its_sealed_guard_and_stops_on_a_loop(
    tmp_path: Path, role: str
) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path, chair=_identity(role=role))
    content = "\n".join(DENSE_ROWS + ["<line>same</line>"] * 50) + "\n"
    with client:
        endpoint.script(ScriptedAnswer(content=content, finish_reason="length"))
        response = client.read(_request(loop_guard=witness_loop_guard(SHIPPED_POLICY, role)))
    record = json.loads((blob_store.root / response.call_record_ref["relative_path"]).read_bytes())
    finding = {"kind": "line", "block_lines": 1, "repeats": 30, "line": 150}
    assert endpoint.requests[0]["stream"] is True and endpoint.streams_stopped == 1
    assert dict(response.loop_stop) == finding
    assert response.content == "\n".join((DENSE_ROWS + ["<line>same</line>"] * 50)[:150]) + "\n"
    assert record["schema"] == CHAIR_STREAM_CALL_RECORD_SCHEMA
    assert record["stream"] == {
        "schema": "chair-stream.v1",
        "loop_guard": WITNESS_GUARD,
        "stopped": finding,
    }


def test_a_witness_reply_with_no_loop_is_read_to_its_end(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path, chair=_identity(role="attestator_3"))
    content = "\n".join(DENSE_ROWS) + "\n"
    with client:
        endpoint.script(ScriptedAnswer(content=content, finish_reason="stop"))
        response = client.read(_request(loop_guard=WITNESS_GUARD))
    record = json.loads((blob_store.root / response.call_record_ref["relative_path"]).read_bytes())
    assert endpoint.streams_stopped == 0
    assert (response.content, response.finish_reason, response.loop_stop) == (content, "stop", None)
    assert record["stream"]["stopped"] is None


@pytest.mark.parametrize(
    ("role", "guard"),
    [
        # Chandra reads under its native recipe and is never streamed.
        ("attestator_1", WITNESS_GUARD),
        ("attestator_3", {**WITNESS_GUARD, "loop_block_repeats": 11}),
        ("attestator_2", GUARD | {"loop_line_repeats": 29}),
    ],
)
def test_a_witness_streams_only_under_its_own_sealed_guard(
    tmp_path: Path, role: str, guard: dict[str, int]
) -> None:
    client, endpoint, _blob_store, _ = _built(tmp_path, chair=_identity(role=role))
    with client:
        with pytest.raises(ChairRequestRefusal, match="sealed repetition-loop guard"):
            client.read(_request(loop_guard=guard))
    assert endpoint.requests == []


def test_a_chandra_native_request_is_never_streamed(tmp_path: Path) -> None:
    chair = ChairIdentity(
        **{**_identity().to_record(), "witness_adapter": "chandra.v1", "witness_scope": "page"}
    )
    client, endpoint, _blob_store, _ = _built(tmp_path, chair=chair)
    intent_ref = {"relative_path": "3_attestatores/artifacts/intent.json", "sha256": "d" * 64}
    request = _request(
        generation_declared={"max_new_tokens": 12384},
        generation_sent={"chat_template_kwargs": {"enable_thinking": False}},
        loop_guard=WITNESS_GUARD,
    )
    with client:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=1)
        with pytest.raises(ChairRequestRefusal, match="sealed repetition-loop guard"):
            client.read_chandra_native(dispatch, intent_ref=intent_ref)
    assert endpoint.requests == []


def test_a_streamed_transport_failure_is_recorded_under_the_stream_schema(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path, chair=_identity(role="perlector"))
    with client:
        endpoint.script(ScriptedAnswer(transport_failure="connection reset"))
        with pytest.raises(ChairTransportFailure) as failure:
            client.read(_request(loop_guard=GUARD))
    record = json.loads(
        (blob_store.root / failure.value.call_record_ref["relative_path"]).read_bytes()
    )
    assert set(record) == CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_FIELDS
    assert record["schema"] == CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA
    assert record["stream"] == {"schema": "chair-stream.v1", "loop_guard": GUARD, "stopped": None}
