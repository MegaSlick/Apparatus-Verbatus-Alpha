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
import protocol
import pytest
from live_reader import EngineSignalRefusal, VLLMReader
from reader import FixtureReader

from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.cross_capture_autopsia import atomic_delivered_pixels, build_autopsia
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

ROOT = Path(__file__).resolve().parents[2]

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


def _autopsia_and_refs(
    *,
    act_key: str,
    region_images: list[bytes],
    page_images: list[bytes],
    region_paths: list[str] | None = None,
    page_paths: list[str] | None = None,
):
    """One act's sealed ``cross_capture_autopsia`` plus the pre-sort refs
    (one per given image, in the caller's own order) it was built from.

    Mirrors production exactly: ``build_autopsia`` canonicalizes ``region_refs``
    by ``(relative_path, sha256)`` -- a key independent of the order the
    dossier's own ``regions`` row happens to declare -- and ``delivered_pixels``
    (via ``atomic_delivered_pixels`` below) walks that same canonical view. A
    caller that needs the two orders to diverge picks ``region_paths`` whose
    sort order is not the caller's input order; every other caller may leave it
    to the default, under which a single region or render trivially agrees with
    itself.
    """
    store: dict[str, bytes] = {}
    region_paths = region_paths or [f"blobs/region-{i}" for i in range(len(region_images))]
    page_paths = page_paths or [f"blobs/page-{i}" for i in range(len(page_images))]

    def _ref(path: str, image: bytes) -> dict[str, str]:
        store[path] = image
        return {"relative_path": path, "sha256": digest_bytes(image)}

    region_refs = [
        _ref(path, image) for path, image in zip(region_paths, region_images, strict=True)
    ]
    page_refs = [_ref(path, image) for path, image in zip(page_paths, page_images, strict=True)]
    view = {
        "view_id": "view-1",
        "physical_page_id": "ppg_fixture",
        "source_sha256": "a" * 64,
        "page_ids": ["pg_1"],
        "local_act_ids": [act_key],
        "region_refs": region_refs,
        "page_render_refs": page_refs,
        "alignment_ref": "alignment-1",
        "visibility_evidence_refs": [_ref("blobs/visibility-1", b"visibility-evidence")],
    }
    autopsia = build_autopsia(
        logical_act_id=f"pac_{act_key}",
        partition_ref={"relative_path": "blobs/partition", "sha256": digest_bytes(b"partition")},
        required_capture_sha256s=["a" * 64],
        views=[view],
    )
    return autopsia, region_refs, page_refs, store


def _dossier(*, act_key: str = "a1", region_image: bytes, page_image: bytes) -> dict:
    autopsia, region_refs, page_refs, _store = _autopsia_and_refs(
        act_key=act_key, region_images=[region_image], page_images=[page_image]
    )
    return {
        "act_id": "act_0000000000000000",
        "act_key": act_key,
        "witness_regime": "named",
        "regions": [
            {
                "region_id": "r1",
                "image_path": region_refs[0]["relative_path"],
                "image_sha256": region_refs[0]["sha256"],
            }
        ],
        "page_renders": [
            {
                "source_page_id": "pg_0000000000000000",
                "source_page_ordinal": 1,
                "image_path": page_refs[0]["relative_path"],
                "image_sha256": page_refs[0]["sha256"],
            }
        ],
        "testimonia": [],
        "cross_capture_autopsia": autopsia,
    }


def _delivered_pixels(*, region_image: bytes, page_image: bytes) -> dict:
    autopsia, _region_refs, _page_refs, store = _autopsia_and_refs(
        act_key="a1", region_images=[region_image], page_images=[page_image]
    )
    return atomic_delivered_pixels(autopsia, read_bytes=store.__getitem__, max_images=64)


def _reader(
    client: ChairClient,
    chair: ChairIdentity,
    *,
    max_tokens: int | None = None,
    protocol_config: Mapping[str, str | int] | None = None,
) -> VLLMReader:
    return VLLMReader(
        client=client, chair=chair, protocol_config=protocol_config, max_tokens=max_tokens
    )


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


def test_missing_delivered_pixels_is_refused_before_the_wire(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        with pytest.raises(ContractError, match="no delivered pixels"):
            _reader(client, chair).read(
                _dossier(region_image=region_image, page_image=page_image),
                pass_kind="perlectio",
                delivered_pixels=None,
            )
    assert endpoint.requests == []


def test_an_autopsia_view_missing_region_refs_is_refused_not_a_bare_keyerror(
    tmp_path: Path,
) -> None:
    client, _endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    dossier = _dossier(region_image=region_image, page_image=page_image)
    view = dict(dossier["cross_capture_autopsia"]["views"][0])
    del view["region_refs"]
    dossier["cross_capture_autopsia"] = dict(dossier["cross_capture_autopsia"], views=[view])
    with client:
        with pytest.raises(ContractError, match="does not name both region_refs"):
            _reader(client, chair).read(
                dossier,
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )


def test_text_is_returned_exactly_never_stripped(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="  ragged edge \n", finish_reason="stop"))
        result = _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert result["text"] == "  ragged edge \n"


def test_genuinely_empty_text_with_stop_is_reported_empty_not_refused(tmp_path: Path) -> None:
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        endpoint.script(ScriptedAnswer(content="", finish_reason="stop"))
        result = _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert result["text"] == ""
    assert result["stop_reason"] == "stop"


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


def test_two_regions_whose_region_id_order_reverses_their_blob_path_order(tmp_path: Path) -> None:
    """The seam finding: ``dossier['regions']`` sorts on ``region_id``
    (``dossier.py``); ``cross_capture_autopsia`` sorts region refs on
    ``(relative_path, sha256)`` (``cross_capture_autopsia.py``'s ``_ref_list``)
    -- independent keys, since a region's blob path is content-addressed. This
    act's two regions are built so the two orders are exact reverses of one
    another: ``r1`` (declared first, by ``region_id``) is the region whose blob
    path sorts *last*. A live read must post images in the order
    ``delivered_pixels`` actually carries them (always the autopsia's own walk)
    and derive ``image_sha256s`` from that same walk, or the two disagree and
    the read is refused by the client's own "exactly and in order" check on
    every act with more than one region.
    """
    client, endpoint, _blobs, chair = _built(tmp_path)
    image_r1, image_r2 = _image_bytes(b"R1-IMAGE"), _image_bytes(b"R2-IMAGE")
    page_image = _image_bytes(b"PAGE")
    autopsia, region_refs, page_refs, store = _autopsia_and_refs(
        act_key="a1",
        region_images=[image_r1, image_r2],
        page_images=[page_image],
        # r1's blob path sorts after r2's: the declared region_id order
        # (r1, r2) is the exact reverse of the path-sorted autopsia order.
        region_paths=["blobs/region-z-for-r1", "blobs/region-a-for-r2"],
    )
    dossier = {
        "act_id": "act_0000000000000000",
        "act_key": "a1",
        "witness_regime": "named",
        "regions": [
            {
                "region_id": "r1",
                "image_path": region_refs[0]["relative_path"],
                "image_sha256": region_refs[0]["sha256"],
            },
            {
                "region_id": "r2",
                "image_path": region_refs[1]["relative_path"],
                "image_sha256": region_refs[1]["sha256"],
            },
        ],
        "page_renders": [
            {
                "source_page_id": "pg_0000000000000000",
                "source_page_ordinal": 1,
                "image_path": page_refs[0]["relative_path"],
                "image_sha256": page_refs[0]["sha256"],
            }
        ],
        "testimonia": [],
        "cross_capture_autopsia": autopsia,
    }
    pixels = atomic_delivered_pixels(autopsia, read_bytes=store.__getitem__, max_images=64)
    # The pixels are in path order (r2's image first), the opposite of the
    # dossier's declared region_id order -- exactly the divergence the finding
    # named.
    assert pixels["region_images"] == [image_r2, image_r1]
    with client:
        endpoint.script(ScriptedAnswer(content="ok", finish_reason="stop"))
        _reader(client, chair).read(dossier, pass_kind="perlectio", delivered_pixels=pixels)
    posted = endpoint.requests[0]
    image_parts = [
        part for part in posted["messages"][0]["content"] if part.get("type") == "image_url"
    ]
    posted_bytes = [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in image_parts
    ]
    assert posted_bytes == [image_r2, image_r1, page_image]


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


# --- FixtureReader carries no engine, so it must never publish engine_call ----


def test_a_fixture_reading_never_publishes_the_engine_call_key() -> None:
    """`reader.py`'s own docstring claim: `FixtureReader` has no engine behind
    it and never sets `engine_call`. `LectioResult`'s key set is otherwise open
    (`total=False`), so nothing but a test closes it -- a `FixtureReader` that
    started emitting a synthetic `engine_call` would publish fabricated engine
    provenance on a fixture Perlectio with every other check green."""
    fixture = {
        "act": [{"key": "a1", "text": "final one"}],
        "page": [],
        "scenario": [{"name": "happy"}],
    }
    result = FixtureReader(fixture, "happy").read(
        {"act_id": "act_0000000000000000", "act_key": "a1", "regions": [], "page_renders": []},
        pass_kind="perlectio",
    )
    assert set(result) == {"text", "stop_reason"}


# --- prompt fidelity: every pass kind sends exactly the byte-exact template --


_ORDINARY_PASS_KINDS = ("perlectio", "lectio-nuda", "lectio-prior", "primed-without-prior")

_TESTIMONIUM = {
    "witness_label": "attestator_1",
    "model_name": "fixture/attestator-1",
    "resolved_provenance": {"resolved_revision": "fixture-v1"},
    "training_domain": "a synthetic fixture witness",
    "outcome": "read",
    "reported": "alpha beta gamma",
}


def _witnessed_dossier(*, region_image: bytes, page_image: bytes, pass_kind: str) -> dict:
    """A dossier carrying a testimonium on every pass, and -- for `perlectio`,
    the one production pass a prior draft is actually fed to -- a fed prior
    draft too. The degenerate all-defaults dossier the fidelity check used to
    run against never rendered the testimonium row or the sealed Pass-B
    fragment for any pass kind, so it proved fidelity only on the least
    interesting bytes the builder can produce."""
    dossier = _dossier(region_image=region_image, page_image=page_image)
    dossier["testimonia"] = [dict(_TESTIMONIUM)]
    if pass_kind == "perlectio":
        dossier["prior_draft"] = {
            "reference": {"relative_path": "4_perlector/artifacts/x.json", "sha256": "0" * 64},
            "text": "PRIOR DRAFT TEXT",
        }
        dossier["prior_draft_view"] = "fed"
    return dossier


@pytest.mark.parametrize("pass_kind", _ORDINARY_PASS_KINDS)
def test_the_rendered_prompt_sent_matches_prompt_evidences_own_digest(
    tmp_path: Path, pass_kind: str
) -> None:
    """Invariant #49, at the wire: the text this reader actually sent digests
    to exactly the `rendered_sha256` `prompts.prompt_evidence` would compute
    for the same chair and dossier -- proving the byte-exact template claim
    against the request that really went out, not only against the builder
    called in isolation. The dossier carries a testimonium (every pass) and a
    fed prior draft (`perlectio`), and the reader is given the sealed R5a
    protocol config, so the wire check covers the testimonia and Pass-B
    fragment branches `_neutral_dossier_lines` renders, not only the
    no-witness, no-prior default."""
    client, endpoint, _blobs, chair = _built(tmp_path)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    dossier = _witnessed_dossier(
        region_image=region_image, page_image=page_image, pass_kind=pass_kind
    )
    protocol_config, _protocol_sha256 = protocol.load(ROOT / "config" / "perlector_protocol.toml")
    with client:
        endpoint.script(ScriptedAnswer(content="a", finish_reason="stop"))
        _reader(client, chair, protocol_config=protocol_config).read(
            dossier,
            pass_kind=pass_kind,
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    posted = endpoint.requests[0]
    sent_text = next(
        part["text"] for part in posted["messages"][0]["content"] if part.get("type") == "text"
    )
    sealed_dossier = dossier | {"dossier_digest": "d" * 64}
    evidence = prompts.prompt_evidence(chair, sealed_dossier, protocol_config)
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
