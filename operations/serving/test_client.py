"""Fake-first drills for ChairClient, its record, and serving_mode_for.

Every test runs offline against :mod:`operations.serving.fakes`. No test
imports vLLM, starts a server, or contacts a provider.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Mapping

import pytest

from common.chairs.models import ChairIdentity, ServingDetails
from common.chairs.receipts import build_receipt, receipt_record
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.serving import CHAIR_CALL_RECORD_FIELDS, CHAIR_CALL_RECORD_SCHEMA

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
)
from .errors import (
    ChairRequestRefusal,
    ChairResponseRefusal,
    ServiceStopError,
    ServingConfigurationError,
)
from .fakes import (
    ABSENT,
    FakeBlobStore,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakePublisher,
    FakeRegistry,
    ScriptedAnswer,
)
from .manager import ServingManager
from .residency import FileResidencyLease

TIER = "generic-48gb"
REVISION = "a" * 40
MANIFEST = "b" * 64
DECODING_SHA = "c" * 64


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
        return {"chair": chair.role, "revision": chair.receipt_revision}

    return read_receipt


def _built(
    tmp_path: Path,
    *,
    chair: ChairIdentity | None = None,
    row: dict[str, object] | None = None,
    read_receipt=None,
    record_temperature: int = 0,
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
    client = ChairClient(
        manager=manager,
        identity=chair,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256=DECODING_SHA,
        record_temperature=record_temperature,
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


# --- construction refuses a policy that is not the sealed 0 ------------------


def test_construction_refuses_a_nonzero_record_temperature(tmp_path: Path) -> None:
    with pytest.raises(ServingConfigurationError):
        _built(tmp_path, record_temperature=1)


# --- pre-send refusals: nothing is built or sent ------------------------------


def test_kind_refusal_sends_nothing(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(_request(kind="completions"))
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_forbidden_generation_sent_keys_refused(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        for key, value in (
            ("model", "x"),
            ("stream", True),
            ("temperature", 0),
            ("seed", 1),
            ("n", 2),
        ):
            with pytest.raises(ChairRequestRefusal) as excinfo:
                client.read(_request(generation_sent={key: value}))
            assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


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


# --- refuse-before-retain: model mismatch and non-200 -------------------------


def test_response_model_mismatch_refuses_with_no_blob_written(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(
            ScriptedAnswer(content="hello", finish_reason="stop", model="someone-elses-model")
        )
        with pytest.raises(ChairResponseRefusal) as excinfo:
            client.read(_request())
        assert excinfo.value.code == "CHAIR_RESPONSE_MODEL_MISMATCH"
    assert len(blob_store) == 0


def test_non_200_refuses_with_no_blob_written(tmp_path: Path) -> None:
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(status=500, body=b'{"error":"boom"}'))
        with pytest.raises(ChairResponseRefusal) as excinfo:
            client.read(_request())
        assert excinfo.value.code == "CHAIR_RESPONSE_HTTP_ERROR"
    assert len(blob_store) == 0


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


# --- finish_reason, verbatim -------------------------------------------------


def test_finish_reason_absent_becomes_none(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="", finish_reason=ABSENT))
        response = client.read(_request())
    assert response.finish_reason is None
    assert response.content == ""
    assert response.parse_problem is None


def test_finish_reason_null_becomes_none(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="text", finish_reason=None))
        response = client.read(_request())
    assert response.finish_reason is None


def test_finish_reason_unknown_string_carried_verbatim(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="text", finish_reason="abort"))
        response = client.read(_request())
    assert response.finish_reason == "abort"
    assert response.parse_problem is None


def test_finish_reason_stop_and_length_carried_verbatim(tmp_path: Path) -> None:
    client, endpoint, _, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="a", finish_reason="stop"))
        assert client.read(_request()).finish_reason == "stop"
        endpoint.script(ScriptedAnswer(content="b", finish_reason="length"))
        assert client.read(_request()).finish_reason == "length"


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
        record_temperature=0,
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
    assert record["generation_sent"] == {}
    assert record["generation_declared"] == {"top_k": 1}
    assert record["raw_response_ref"] == dict(response.raw_response_ref)
    assert record["response_sha256"] == response.response_sha256
    assert record["finish_reason"] == "stop"
    assert record["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert response.usage == record["usage"]
    assert record["parse_problem"] is None
    # GOVERNANCE 7: the exact bytes the engine returned, never stripped,
    # cased, or trimmed — carried verbatim into both the response and the
    # blob the record was built alongside.
    assert response.content == exact_content
    assert json.loads(response.raw_response)["choices"][0]["message"]["content"] == exact_content
    # Canonical: re-serializing the parsed record reproduces the stored bytes.
    assert canonical_bytes(record) == record_bytes
    assert response.call_record_ref["sha256"] == digest_bytes(record_bytes)


# --- receipt re-verification on __enter__ -------------------------------------


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
    """The drift diagnosis must not be replaced by a shutdown failure.

    ``handle.stop()`` runs before the drift is raised; if the stop itself
    cannot be verified (the endpoint keeps answering after the owned process
    is told to exit), the resulting ``ServiceStopError`` must be chained onto
    the ``ReceiptDriftRefusal``, never swap places with it.
    """

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
        record_temperature=0,
        read_receipt=wrong_receipt,
    )
    with pytest.raises(ReceiptDriftRefusal) as excinfo:
        with client:
            pass
    assert excinfo.value.code == "CHAIR_RECEIPT_DRIFT"
    assert isinstance(excinfo.value.__cause__, ServiceStopError)


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


def test_serving_mode_vllm_without_tier_is_unresolved(tmp_path: Path) -> None:
    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="x"), chair
    )
    recipes = _recipes(row)
    with pytest.raises(ServingModeRefusal) as excinfo:
        serving_mode_for(recipes, chair, None)
    assert excinfo.value.code == "SERVING_MODE_UNRESOLVED"


def test_serving_mode_vllm_with_tier_is_live(tmp_path: Path) -> None:
    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="x"), chair
    )
    recipes = _recipes(row)
    assert serving_mode_for(recipes, chair, TIER) == "live"


def test_serving_mode_absent_tier_is_unresolved(tmp_path: Path) -> None:
    """A tier with no configured row for this chair is still this function's
    own refusal vocabulary — never a bare `ServingConfigurationError` leaking
    out of `recipes.for_identity`'s zero-match lookup."""

    chair = _identity()
    row = _seal(
        _vllm_row(recipe=chair.serving_recipe, chair=chair.role, served_model_id="x"), chair
    )
    recipes = _recipes(row)
    with pytest.raises(ServingModeRefusal) as excinfo:
        serving_mode_for(recipes, chair, "tier-does-not-exist")
    assert excinfo.value.code == "SERVING_MODE_UNRESOLVED"


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
