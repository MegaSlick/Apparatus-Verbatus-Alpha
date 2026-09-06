"""Fake-first drills for ChairClient, its record, and serving_mode_for.

Every test runs offline against :mod:`operations.serving.fakes`. No test
imports vLLM, starts a server, or contacts a provider.
"""

from __future__ import annotations

import base64
import copy
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
    _plain_capacity,
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


# --- retain, then refuse: model mismatch and non-200 --------------------------


def test_response_model_mismatch_refuses_with_the_body_retained_and_named(
    tmp_path: Path,
) -> None:
    """Retention is not attribution.

    A body from another model is still not this chair's evidence and still
    never becomes a reading -- the refusal is unchanged and no `ChairResponse`
    comes back. What changed is that the bytes exist afterwards, by their own
    digest, so a reader can see what actually arrived instead of taking the
    refusal's word for it.

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
    assert len(blob_store) == 1
    assert b"someone-elses-model" in blob_store.written[0]
    assert digest_bytes(blob_store.written[0]) in excinfo.value.detail
    assert "someone-elses-model" in excinfo.value.detail
    assert "A READING NO CHAIR HERE ASKED FOR" not in excinfo.value.detail
    assert str(len(blob_store.written[0])) in excinfo.value.detail


def test_a_non_200_body_is_retained_before_the_refusal_and_quoted_in_it(
    tmp_path: Path,
) -> None:
    """The one artefact a rented card exists to produce, no longer discarded.

    When vLLM refuses a request it says *why* in the body of a non-200, and
    that sentence is the whole diagnostic. It used to be thrown away here --
    `_refuse_bytes_from_the_wrong_source` ran before `self._retain` -- so the
    predicted first real boot returned a stack trace and no engine account of
    what went wrong. GOVERNANCE 2: nothing is lost silently.
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
    assert blob_store.written == [body]
    assert blob_store.has(digest_bytes(body))
    assert "HTTP 400" in excinfo.value.detail
    assert "maximum context length is 2048" in excinfo.value.detail
    assert digest_bytes(body) in excinfo.value.detail


def test_a_long_refused_body_is_retained_whole_and_previewed_short(tmp_path: Path) -> None:
    """The detail carries a bounded head; the blob carries all of it."""

    body = b'{"error":"' + b"x" * 4000 + b'"}'
    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(status=502, body=body))
        with pytest.raises(ChairResponseRefusal) as excinfo:
            client.read(_request())
    assert blob_store.written == [body]
    assert len(excinfo.value.detail) < 1000
    assert f"first 512 of {len(body)} bytes" in excinfo.value.detail


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
    # No caller stated one here, and the client never invents one.
    assert record["capacity"] is None
    # GOVERNANCE 7: the exact bytes the engine returned, never stripped,
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


def test_a_capacity_record_mutated_after_construction_does_not_reach_the_call_record(
    tmp_path: Path,
) -> None:
    """The retained arithmetic is the arithmetic the request was admitted on.

    A capacity record is not flat -- `request_fits` returns an `images` list of
    per-image dictionaries -- and every production builder passes that record
    straight into `ChairRequest` while keeping its own reference to it. Freezing
    only the outer mapping left the nested data live: a caller could rewrite an
    image's token cost, or drop an image, after the request was built and
    before the client retained the record, and the receipt would then carry
    numbers no admission decision was ever made on.

    Both directions are exercised: a rewrite through the caller's own reference
    changes nothing on the request or in the retained record, and a write
    through the request's own view raises instead of succeeding quietly.
    """

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
    assert _plain_capacity(request.capacity) == admitted
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
    """Refused where the caller can still see it, not inside serialization.

    `canonical_bytes` refuses floats, non-string keys and cycles, and the call
    record goes through it. Sealing the snapshot through the same writer moves
    that refusal to `ChairRequest` construction: before the wire call, with the
    caller's own frame on the stack, rather than after a pod has answered.
    """

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
    """The whole of the Attestatores HANDOFF's first owed gap, closed and proven.

    A live `dai.v1` request could not be recorded at all: the call record goes
    through `canonical_bytes`, which refuses floats outright, so writing it
    raised and the request was therefore never made. The record now carries
    each float as the exact decimal text the request body itself contains,
    tagged `wire-decimal.v1`, and this test reads that text back out of the
    *bytes the endpoint actually received* rather than out of the client's own
    Python values.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    sent = {"repetition_penalty": 1.05, "top_k": 1, "top_p": 0.001}
    with client:
        endpoint.script(ScriptedAnswer(content="texte transcrit", finish_reason="stop"))
        response = client.read(_request(generation_sent=sent, generation_declared=_DAI_GENERATION))

    record_bytes = next(data for data in blob_store.written if data != response.raw_response)
    record = json.loads(record_bytes)
    # Canonical bytes at all — the refusal that used to stop this request.
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

    DAI's `temperature` 0.1 never reaches the wire — the sealed reading-of-
    record posture is 0 and the client refuses to be built against anything
    else — but the record must still say what the vendor declared, to the
    digit, or the two halves of GOVERNANCE 7's account disagree.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        endpoint.script(ScriptedAnswer(content="x", finish_reason="stop"))
        response = client.read(_request(generation_declared={"temperature": 0.1}))
    record = json.loads(next(data for data in blob_store.written if data != response.raw_response))
    assert record["generation_declared"] == {
        "temperature": {"schema": "wire-decimal.v1", "decimal": "0.1"}
    }
    # The posted body carries the sealed 0, never the declared 0.1 — asserted
    # strictly, since a client that stopped pinning temperature at all would
    # silently drop the reading-of-record posture (GOVERNANCE 7) and this
    # weaker form (`not in ... or ... == 0`) would not catch it.
    assert endpoint.requests[0]["temperature"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_nonfinite_generation_value_is_refused_before_anything_is_sent(
    tmp_path: Path, value: float
) -> None:
    """NaN and Infinity are not JSON; Python's encoder emits them anyway.

    A request carrying one would put a body on the wire no conforming reader
    can parse, and no honest record could transcribe it. Refused before the
    body exists, rather than rounded to some finite number nobody declared.
    """

    client, endpoint, blob_store, _ = _built(tmp_path)
    with client:
        with pytest.raises(ChairRequestRefusal) as excinfo:
            client.read(_request(generation_sent={"top_p": value}))
        assert excinfo.value.code == "CHAIR_REQUEST_INVALID"
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_a_declared_view_that_collides_with_the_decimal_form_is_refused_not_mangled(
    tmp_path: Path,
) -> None:
    """The one shape the tagged form cannot represent, named rather than lost.

    A vendor that genuinely declared `{"schema": "wire-decimal.v1", "decimal":
    "1.05"}` as a *value* would be indistinguishable, on read-back, from a
    float this client encoded. The client proves its own transcription round
    trips on every call, so this collision surfaces as a named refusal before
    the record is written instead of as a silently retyped vendor value.
    """

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
    """`RunTree.read_run_receipt`'s own rule, satisfied by the client itself.

    `ServiceHandle.receipt_reference` is a read-only mapping proxy — a
    published provenance reference nothing may edit — and the tree's reader
    accepts its own reference type or a plain `dict` and refuses anything else
    by name. Both boundaries are right, and neither is loosened: the client
    copies on the way in. Before it did, every stage that wired a real tree had
    to carry a private converter, and the wiring the serving README describes
    (`read_receipt=context.tree.read_run_receipt`) refused every live start.

    The reader below refuses exactly what the real one refuses, so passing it
    bare is the assertion.
    """

    chair = _identity()
    seen: list[object] = []

    def tree_shaped_read_receipt(reference: object) -> dict[str, object]:
        seen.append(reference)
        if type(reference) is not dict or set(reference) != {"relative_path", "sha256"}:
            raise TypeError(
                "run receipt reference must contain exactly relative_path and sha256, "
                f"as a plain dict; got {type(reference).__name__}"
            )
        return {"chair": chair.role, "revision": chair.receipt_revision}

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
