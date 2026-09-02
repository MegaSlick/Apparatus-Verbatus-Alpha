"""``VLLMReader`` behind a fake OpenAI-compatible endpoint (``operations/serving/fakes.py``).

Every test here runs offline: no vLLM, no pod, no network. The fake endpoint
speaks the same reading contract :mod:`operations.serving.client` sends real
bytes through, so what these tests prove about stop-reason mapping, image
ordering, and prompt fidelity is the same shape a live chair would see.
"""

from __future__ import annotations

import ast
import base64
from pathlib import Path
from typing import Mapping

import live_reader
import prompts
import pytest
from live_reader import EngineSignalRefusal, VLLMReader

from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.perlector_audit import audit_request as build_audit_request
from operations.serving.client import ChairClient
from operations.serving.config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    parse_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.errors import ChairRequestRefusal
from operations.serving.fakes import (
    ABSENT,
    FakeBlobStore,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakePublisher,
    FakeRegistry,
    ScriptedAnswer,
)
from operations.serving.manager import ServingManager
from operations.serving.residency import FileResidencyLease

TIER = "generic-48gb"
REVISION = "a" * 40
MANIFEST = "b" * 64
DECODING_SHA = "c" * 64
SERVED_MODEL_ID = "served-perlector"


def _chair(recipe: str = "unproven-real-perlector") -> ChairIdentity:
    return ChairIdentity(
        role="perlector",
        source="huggingface",
        repo="example/perlector",
        path=None,
        revision=REVISION,
        digest_manifest=MANIFEST,
        manifest="manifests/perlector.json",
        adapter_of=None,
        serving_recipe=recipe,
        license_note="test identity only",
    )


def _vllm_row(chair: ChairIdentity) -> dict[str, object]:
    return {
        "kind": "vllm",
        "recipe": chair.serving_recipe,
        "chair": chair.role,
        "tier": TIER,
        "host": "127.0.0.1",
        "port": 8000,
        "served_model_id": SERVED_MODEL_ID,
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


def _sealed_row(chair: ChairIdentity) -> dict[str, object]:
    row = dict(_vllm_row(chair))
    row["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
    row["preflight_digest"] = profile_preflight_digest(row)
    return row


def _read_receipt(chair: ChairIdentity):
    def read_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {"chair": chair.role, "revision": chair.receipt_revision}

    return read_receipt


def _built(tmp_path: Path, *, chair: ChairIdentity | None = None):
    chair = chair or _chair()
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(served_model_id=SERVED_MODEL_ID, blob_store=blob_store)
    launcher = FakeLauncher(endpoint)
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    manager = ServingManager(
        registry=registry,
        recipes=parse_serving_recipes(
            {"schema": "serving-recipes.v1", "profiles": [_sealed_row(chair)]}
        ),
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
        read_receipt=_read_receipt(chair),
    )
    return client, endpoint, blob_store, chair


def _image_bytes(tag: bytes) -> bytes:
    return b"\x89PNG fixture image " + tag


def _dossier(*, act_key: str = "a1", region_image: bytes, page_image: bytes) -> dict:
    return {
        "act_id": "act_0000000000000000",
        "act_key": act_key,
        "witness_regime": "named",
        "regions": [
            {
                "region_id": "r1",
                "image_path": "transient-region",
                "image_sha256": digest_bytes(region_image),
            }
        ],
        "page_renders": [
            {
                "source_page_id": "pg_0000000000000000",
                "source_page_ordinal": 1,
                "image_path": "transient-page",
                "image_sha256": digest_bytes(page_image),
            }
        ],
        "testimonia": [],
    }


def _delivered_pixels(*, region_image: bytes, page_image: bytes) -> dict:
    return {"region_images": [region_image], "page_render_images": [page_image]}


def _reader(
    client: ChairClient, chair: ChairIdentity, *, max_tokens: int | None = None
) -> VLLMReader:
    return VLLMReader(client=client, chair=chair, protocol_config=None, max_tokens=max_tokens)


# --- pass_kind: the closed refusal, and nowhere else it matters --------------


def test_an_unnamed_pass_kind_is_refused(tmp_path: Path) -> None:
    client, _endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        with pytest.raises(ContractError, match="unknown Perlector pass kind"):
            _reader(client, chair).read(
                _dossier(region_image=region_image, page_image=page_image),
                pass_kind="lectio_prior",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )


def test_the_live_reader_reads_pass_kind_for_nothing_but_the_refusal_and_the_audit_handoff() -> (
    None
):
    """`live_reader.py`'s own claim: `pass_kind` is routing, never evidence.

    A test on the source rather than behaviour, because the property is about
    what the reader is *permitted* to condition on, not what one fixed set of
    inputs happens to produce -- the module docstring's own reasoning
    (mirroring `reader.py`'s: a reader that read the label and behaved
    differently would make the witness-dependence instrument measure its own
    routing rather than the model). Every code line of `VLLMReader.read` that
    names `pass_kind` -- module and method prose excluded, since talking
    about the rule is not applying it -- must be one of exactly two: the
    closed-membership refusal, and the hand-off to `validate_audit_delivery`.
    """
    source = Path(live_reader.__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    read_method = next(
        inner
        for outer in ast.walk(module)
        if isinstance(outer, ast.ClassDef) and outer.name == "VLLMReader"
        for inner in outer.body
        if isinstance(inner, ast.FunctionDef) and inner.name == "read"
    )
    lines = source.splitlines()
    # The parameter declaration itself is excluded: naming a parameter is not
    # reading it. Every other statement in the body is fair game.
    body_lines = lines[read_method.body[0].lineno - 1 : read_method.end_lineno]
    matches = [line for line in body_lines if "pass_kind" in line]
    assert len(matches) == 2, matches
    assert "not in PASS_KINDS" in matches[0]
    assert "pass_kind=pass_kind" in matches[1]
    # The second line is the call's own `pass_kind=` keyword argument; the
    # call it belongs to is `validate_audit_delivery`'s, one line above it.
    call_line = body_lines[body_lines.index(matches[1]) - 1]
    assert "validate_audit_delivery" in call_line


# --- stop-reason mapping, per spec 1.6 ----------------------------------------


def test_stop_reason_stop_maps_to_stop(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="alpha beta", finish_reason="stop"))
        result = _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert result["stop_reason"] == "stop"
    assert result["text"] == "alpha beta"
    assert result["engine_call"]["finish_reason"] == "stop"
    assert result["engine_call"]["served_model_id"] == SERVED_MODEL_ID


def test_stop_reason_length_maps_to_length(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="cut off mid", finish_reason="length"))
        result = _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert result["stop_reason"] == "length"


def test_stop_reason_absent_maps_to_none(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="no stop word", finish_reason=ABSENT))
        result = _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert result["stop_reason"] is None
    assert result["engine_call"]["finish_reason"] is None


def test_an_unrecognized_stop_reason_refuses_by_name_with_the_bytes_retained(
    tmp_path: Path,
) -> None:
    client, endpoint, blob_store, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="whatever it said", finish_reason="abort"))
        with pytest.raises(EngineSignalRefusal) as excinfo:
            _reader(client, chair).read(
                _dossier(act_key="a9", region_image=region_image, page_image=page_image),
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    assert "'abort'" in str(excinfo.value)
    assert "'a9'" in str(excinfo.value)
    assert blob_store.has(excinfo.value.raw_response_ref["sha256"])


def test_a_malformed_response_refuses_by_its_parse_problem_code_with_bytes_retained(
    tmp_path: Path,
) -> None:
    client, endpoint, blob_store, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(body=b"not json at all"))
        with pytest.raises(EngineSignalRefusal) as excinfo:
            _reader(client, chair).read(
                _dossier(region_image=region_image, page_image=page_image),
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    assert "CHAIR_RESPONSE_INVALID" in str(excinfo.value)
    assert blob_store.has(excinfo.value.raw_response_ref["sha256"])


# --- image order and digest binding -------------------------------------------


def test_images_are_regions_then_page_renders_in_dossier_order(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"REGION"), _image_bytes(b"PAGE")
    with client:
        endpoint.script(ScriptedAnswer(content="ok", finish_reason="stop"))
        _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    posted = endpoint.requests[0]
    image_parts = [
        part for part in posted["messages"][0]["content"] if part.get("type") == "image_url"
    ]
    posted_bytes = [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in image_parts
    ]
    assert posted_bytes == [region_image, page_image]


def test_image_sha256s_are_the_dossiers_own_digests_in_the_same_order(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"REGION"), _image_bytes(b"PAGE")
    dossier = _dossier(region_image=region_image, page_image=page_image)
    with client:
        endpoint.script(ScriptedAnswer(content="ok", finish_reason="stop"))
        _reader(client, chair).read(
            dossier,
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    # The client's own pre-send refusal (`_refuse_unbuildable_request`) already
    # proves the claimed digests match the wire bytes exactly and in order; a
    # request that reached the endpoint at all already survived that check, so
    # this pins the claim in terms of the dossier's own declared digests.
    posted = endpoint.requests[0]
    image_parts = [
        part for part in posted["messages"][0]["content"] if part.get("type") == "image_url"
    ]
    posted_digests = [
        digest_bytes(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
        for part in image_parts
    ]
    assert posted_digests == [
        dossier["regions"][0]["image_sha256"],
        dossier["page_renders"][0]["image_sha256"],
    ]


def test_a_drifted_image_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    """The reader's own claim (`image_sha256s` from the dossier, images from
    `delivered_pixels`) is only honest if a mismatch between the two is
    actually caught. Swap in pixels that do not match the dossier's declared
    digest and the client's own drift check must refuse before the wire."""
    client, endpoint, blob_store, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"REGION"), _image_bytes(b"PAGE")
    dossier = _dossier(region_image=region_image, page_image=page_image)
    wrong_region_image = _image_bytes(b"NOT THE SAME BYTES")
    with client:
        with pytest.raises(ChairRequestRefusal):
            _reader(client, chair).read(
                dossier,
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=wrong_region_image, page_image=page_image
                ),
            )
    assert endpoint.requests == []
    assert len(blob_store) == 0


# --- audit re-proof: delivered instrument appended verbatim ------------------


def test_audit_reproof_appends_every_reproof_prompt_verbatim_after_the_rendered_prompt(
    tmp_path: Path,
) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    dossier = _dossier(region_image=region_image, page_image=page_image)
    flags = [
        {"class": "repetition", "location": {"start": 0, "end": 5}},
        {"class": "numbering", "location": {"start": 6, "end": 9}},
    ]
    semi_final_text = "abcde fgh"
    request = build_audit_request(
        act_key=dossier["act_key"],
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/artifacts/draft.json", "sha256": "0" * 64},
        semi_final_text=semi_final_text,
        flags=flags,
    )
    rendered = prompts.build_prompt(chair.serving_recipe, chair.role, dossier, None)
    with client:
        endpoint.script(ScriptedAnswer(content="confirmed unchanged", finish_reason="stop"))
        _reader(client, chair).read(
            dossier,
            pass_kind="audit-reproof",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
            audit_request=request,
        )
    posted = endpoint.requests[0]
    sent_text = next(
        part["text"] for part in posted["messages"][0]["content"] if part.get("type") == "text"
    )
    expected = "\n".join([rendered, *(row["prompt"] for row in request["reproofs"])])
    assert sent_text == expected
    # Verbatim and nothing else: exactly the rendered prompt plus the
    # delivered reproof prompts, in the request's own order.
    assert sent_text.startswith(rendered)
    assert sent_text.count(request["reproofs"][0]["prompt"]) == 1
    assert sent_text.count(request["reproofs"][1]["prompt"]) == 1
    assert sent_text.index(request["reproofs"][0]["prompt"]) < sent_text.index(
        request["reproofs"][1]["prompt"]
    )


def test_audit_reproof_with_no_delivered_request_refuses_exactly_as_the_fixture_reader_does(
    tmp_path: Path,
) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        with pytest.raises(ContractError, match="re-proof plan a Perlectio seals as delivered"):
            _reader(client, chair).read(
                _dossier(region_image=region_image, page_image=page_image),
                pass_kind="audit-reproof",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    assert endpoint.requests == []


# --- generation_sent / generation_declared ------------------------------------


def test_max_tokens_rides_generation_sent_only_when_given(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="a", finish_reason="stop"))
        _reader(client, chair, max_tokens=256).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert endpoint.requests[0]["max_tokens"] == 256


def test_no_max_tokens_sends_none_and_the_client_never_adds_one(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="a", finish_reason="stop"))
        _reader(client, chair, max_tokens=None).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert "max_tokens" not in endpoint.requests[0]


# --- prompt fidelity: every pass kind sends exactly the byte-exact template --


_ORDINARY_PASS_KINDS = ("perlectio", "lectio-nuda", "lectio-prior", "primed-without-prior")


@pytest.mark.parametrize("pass_kind", _ORDINARY_PASS_KINDS)
def test_the_rendered_prompt_sent_matches_prompt_evidences_own_digest(
    tmp_path: Path, pass_kind: str
) -> None:
    """Invariant #49, at the wire: the text this reader actually sent digests
    to exactly the `rendered_sha256` `prompts.prompt_evidence` would compute
    for the same chair and dossier -- proving the byte-exact template claim
    against the request that really went out, not only against the builder
    called in isolation."""
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    dossier = _dossier(region_image=region_image, page_image=page_image)
    with client:
        endpoint.script(ScriptedAnswer(content="a", finish_reason="stop"))
        _reader(client, chair).read(
            dossier,
            pass_kind=pass_kind,
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    posted = endpoint.requests[0]
    sent_text = next(
        part["text"] for part in posted["messages"][0]["content"] if part.get("type") == "text"
    )
    sealed_dossier = dossier | {"dossier_digest": "d" * 64}
    evidence = prompts.prompt_evidence(chair, sealed_dossier, None)
    assert digest_bytes(sent_text.encode("utf-8")) == evidence["rendered_sha256"]


def test_the_audit_reproof_pass_sends_the_same_base_prompt_plus_its_delivered_instrument(
    tmp_path: Path,
) -> None:
    """The one pass kind whose sent text is not *only* the rendered template
    (it also carries the delivered re-proof instrument, per spec 3.1). Fidelity
    still holds over the part `prompt_evidence` actually claims: the sent
    text's own prefix, up to where the reproof prompts begin, digests to
    exactly `rendered_sha256`."""
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    dossier = _dossier(region_image=region_image, page_image=page_image)
    flags = [{"class": "order", "location": {"start": 0, "end": 4}}]
    request = build_audit_request(
        act_key=dossier["act_key"],
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/artifacts/draft.json", "sha256": "0" * 64},
        semi_final_text="abcd efgh",
        flags=flags,
    )
    with client:
        endpoint.script(ScriptedAnswer(content="confirmed unchanged", finish_reason="stop"))
        _reader(client, chair).read(
            dossier,
            pass_kind="audit-reproof",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
            audit_request=request,
        )
    posted = endpoint.requests[0]
    sent_text = next(
        part["text"] for part in posted["messages"][0]["content"] if part.get("type") == "text"
    )
    sealed_dossier = dossier | {"dossier_digest": "d" * 64}
    evidence = prompts.prompt_evidence(chair, sealed_dossier, None)
    rendered = prompts.build_prompt(chair.serving_recipe, chair.role, dossier, None)
    assert sent_text == "\n".join([rendered, request["reproofs"][0]["prompt"]])
    assert digest_bytes(rendered.encode("utf-8")) == evidence["rendered_sha256"]
