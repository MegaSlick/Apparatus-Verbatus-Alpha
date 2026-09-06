"""``VLLMReader`` behind a fake OpenAI-compatible endpoint (``operations/serving/fakes.py``).

Every test here runs offline: no vLLM, no pod, no network. The fake endpoint
speaks the same reading contract :mod:`operations.serving.client` sends real
bytes through, so what these tests prove about stop-reason mapping, image
ordering, and prompt fidelity is the same shape a live chair would see.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import tomllib
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
from common.imaging import encode_grayscale_png
from common.perlector_audit import audit_request as build_audit_request
from common.request_capacity import (
    PERLECTOR_MAX_IMAGES_THE_OVERHEAD_COVERS,
    PERLECTOR_PROMPT_OVERHEAD_TOKENS,
    PERLECTOR_PROMPT_TEMPLATE_DIGEST,
    PROMPT_TOKENS_MEASURED_CONSTANT,
    RequestCapacityRefusal,
    perlector_prompt_bound,
    perlector_prompt_tokens,
    request_fits,
)
from operations.serving.client import ChairClient
from operations.serving.config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    parse_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.errors import ChairRequestRefusal, ChairResponseRefusal
from operations.serving.fakes import (
    ABSENT,
    FakeBlobStore,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakePublisher,
    FakeRegistry,
    ScriptedAnswer,
    scripted_prompt_too_long,
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


def _vllm_row(chair: ChairIdentity, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
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
        # Wide enough that the *reserve* is never what refuses an ordinary
        # request in this suite: at this row's `max_pixels = 1024` every image
        # saturates to one token, so a region crop always costs what a page
        # render costs and `_reserved_answer_budget` always reserves the
        # dense-page 1,318. A test that means to prove an overrun states its
        # own narrower `max_model_len`.
        "max_model_len": 8192,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1024,
        # The chair's own vision-encoder geometry, as the shipped real
        # catalogue states it: without it nothing can say what one image costs
        # this chair in prompt tokens, and the request builders refuse by name
        # rather than counting against a default (`common/request_capacity.py`).
        "patch_size": 16,
        "merge_size": 2,
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
    row.update(overrides)
    return row


def _sealed_row(chair: ChairIdentity, **overrides: object) -> dict[str, object]:
    row = dict(_vllm_row(chair, **overrides))
    row["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
    row["preflight_digest"] = profile_preflight_digest(row)
    return row


def _read_receipt(chair: ChairIdentity):
    def read_receipt(reference: Mapping[str, str]) -> dict[str, object]:
        del reference
        return {"chair": chair.role, "revision": chair.receipt_revision}

    return read_receipt


def _built(tmp_path: Path, *, chair: ChairIdentity | None = None, **row_overrides: object):
    chair = chair or _chair()
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(served_model_id=SERVED_MODEL_ID, blob_store=blob_store)
    launcher = FakeLauncher(endpoint)
    registry = FakeRegistry({chair.role: chair}, tmp_path)
    manager = ServingManager(
        registry=registry,
        recipes=parse_serving_recipes(
            {"schema": "serving-recipes.v1", "profiles": [_sealed_row(chair, **row_overrides)]}
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


def _image_bytes(tag: bytes, *, width: int = 40, height: int = 40) -> bytes:
    """A real PNG whose pixels vary with the tag.

    Real rather than a byte string with a PNG signature glued on, because
    ``VLLMReader.read`` now measures every image it is about to send
    (``common.request_capacity.image_sizes`` reads the IHDR): a fake cannot be
    charged for pixels it does not have.
    """

    fill = hashlib.sha256(tag).digest()
    rows = [bytearray(fill[(y % len(fill))] for _ in range(width)) for y in range(height)]
    return encode_grayscale_png(width, height, rows)


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


def _two_view_act(*, region_images: list[bytes], page_images: list[bytes]):
    """One act seen from two capture views: its dossier and its delivered pixels.

    The cross-capture case `TOKEN_COST_REPORT.md` section 7 measures and the
    live reader already builds for -- every region crop across every view, then
    every page render across every view, in one request. Each view carries one
    region and one page render, so the request sends four images.
    """

    assert len(region_images) == len(page_images) == 2
    store: dict[str, bytes] = {}

    def _ref(path: str, image: bytes) -> dict[str, str]:
        store[path] = image
        return {"relative_path": path, "sha256": digest_bytes(image)}

    views = []
    for index, (region_image, page_image) in enumerate(
        zip(region_images, page_images, strict=True)
    ):
        views.append(
            {
                "view_id": f"view-{index + 1}",
                "physical_page_id": "ppg_fixture",
                "source_sha256": chr(ord("a") + index) * 64,
                "page_ids": [f"pg_{index + 1}"],
                "local_act_ids": ["a1"],
                "region_refs": [_ref(f"blobs/region-{index}", region_image)],
                "page_render_refs": [_ref(f"blobs/page-{index}", page_image)],
                "alignment_ref": f"alignment-{index + 1}",
                "visibility_evidence_refs": [
                    _ref(f"blobs/visibility-{index}", f"visibility-{index}".encode("ascii"))
                ],
            }
        )
    autopsia = build_autopsia(
        logical_act_id="pac_a1",
        partition_ref={"relative_path": "blobs/partition", "sha256": digest_bytes(b"partition")},
        required_capture_sha256s=[view["source_sha256"] for view in views],
        views=views,
    )
    dossier = {
        "act_id": "act_0000000000000000",
        "act_key": "a1",
        "witness_regime": "named",
        "regions": [
            {
                "region_id": f"r{index + 1}",
                "image_path": view["region_refs"][0]["relative_path"],
                "image_sha256": view["region_refs"][0]["sha256"],
            }
            for index, view in enumerate(autopsia["views"])
        ],
        "page_renders": [
            {
                "source_page_id": f"pg_000000000000000{index + 1}",
                "source_page_ordinal": index + 1,
                "image_path": view["page_render_refs"][0]["relative_path"],
                "image_sha256": view["page_render_refs"][0]["sha256"],
            }
            for index, view in enumerate(autopsia["views"])
        ],
        "testimonia": [],
        "cross_capture_autopsia": autopsia,
    }
    pixels = atomic_delivered_pixels(autopsia, read_bytes=store.__getitem__, max_images=64)
    return dossier, pixels


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


# --- the pre-send capacity refusal -------------------------------------------


def test_an_act_whose_images_overrun_the_sealed_row_is_refused_before_the_wire(
    tmp_path: Path,
) -> None:
    """The failure this check exists to move off a billing card.

    A page-fallback act carries a full-page region crop and a full-page render.
    At 2,480 pixels wide against this row's `max_pixels` the two cost 1,404 and
    1,715 image tokens; with the sealed upper bound on this dossier's prompt and
    even the smaller single-act answer budget that is well past the 2,048 this
    row states. Before this check the request went out and vLLM answered HTTP 400
    with a body the client discarded; now nothing is built, the endpoint sees
    nothing, and the refusal carries the whole arithmetic.
    """

    # The one row in this suite with a realistic pixel budget: everything else
    # here uses `max_pixels = 1024`, under which a whole page costs one token
    # and no request could overrun anything. The context is stated too, at the
    # 2,048 every real row used to ship, because the suite's default row is now
    # wide enough to hold this request.
    client, endpoint, blob_store, chair = _built(tmp_path, max_pixels=1806336, max_model_len=2048)
    region_image = _image_bytes(b"REGION", width=2480, height=584)
    page_image = _image_bytes(b"PAGE", width=2480, height=3508)
    dossier = _dossier(region_image=region_image, page_image=page_image)
    with client:
        with pytest.raises(RequestCapacityRefusal) as error:
            _reader(client, chair).read(
                dossier,
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    record = error.value.capacity
    assert [entry["image_prompt_tokens"] for entry in record["images"]] == [1404, 1715]
    assert record["answer_budget"] == 216
    assert record["fits"] is False
    assert record["headroom"] < 0
    # The counterfactual half: nothing reached the endpoint and nothing was
    # retained, because nothing was sent.
    assert endpoint.requests == []
    assert len(blob_store) == 0


def test_a_page_sized_crop_reserves_a_page_of_reading_not_one_acts(tmp_path: Path) -> None:
    """The reserve follows the pixels, because the reading will.

    An ordinary act's crop is a slice of a page and its reading is one act:
    216 tokens. A page-fallback act's crop *is* the page, so its reading is a
    page of text: 1,318. Reserving 216 for the second would admit a request
    whose answer the row cannot hold, which is the same HTTP 400 this check
    exists to prevent -- only arriving after the prompt was accepted rather
    than before. Both cases are the same row and the same four measured image
    costs; only the region's own size differs.
    """

    page_image = _image_bytes(b"PAGE", width=2480, height=3508)
    fallback_region = _image_bytes(b"FALLBACK-REGION", width=2480, height=3508)
    ordinary_region = _image_bytes(b"REGION", width=2480, height=584)
    endpoints = []
    blob_stores = []

    def _record(name: str, region_image: bytes) -> dict:
        client, endpoint, blob_store, chair = _built(
            tmp_path / name, max_pixels=1806336, max_model_len=2048
        )
        endpoints.append(endpoint)
        blob_stores.append(blob_store)
        with client:
            with pytest.raises(RequestCapacityRefusal) as error:
                _reader(client, chair).read(
                    _dossier(region_image=region_image, page_image=page_image),
                    pass_kind="perlectio",
                    delivered_pixels=_delivered_pixels(
                        region_image=region_image, page_image=page_image
                    ),
                )
        return error.value.capacity

    fallback = _record("fallback", fallback_region)
    ordinary = _record("ordinary", ordinary_region)
    # The region crop costs exactly what the page render costs, which is what
    # "page-sized" means here, and the reserve moves with it.
    assert [entry["image_prompt_tokens"] for entry in fallback["images"]] == [1715, 1715]
    assert fallback["answer_budget"] == 1318
    assert [entry["image_prompt_tokens"] for entry in ordinary["images"]] == [1404, 1715]
    assert ordinary["answer_budget"] == 216
    # Neither was sent, so neither could have been answered.
    assert [endpoint.requests for endpoint in endpoints] == [[], []]
    assert [len(store) for store in blob_stores] == [0, 0]


def test_the_reserve_is_never_below_the_max_tokens_this_reader_would_send(
    tmp_path: Path,
) -> None:
    """The wire bound and the reserve cannot drift apart.

    `max_tokens` is what the engine is permitted to generate. A reserve below
    it would admit a request whose own permitted answer does not fit the row,
    so the reserve is the larger of the two -- checked here at a bound well
    above every measured answer budget, where the reserve is the bound itself.
    """

    client, _endpoint, _blob_store, chair = _built(tmp_path, max_pixels=1806336, max_model_len=2048)
    region_image = _image_bytes(b"REGION", width=2480, height=584)
    page_image = _image_bytes(b"PAGE", width=2480, height=3508)
    with client:
        with pytest.raises(RequestCapacityRefusal) as error:
            _reader(client, chair, max_tokens=4000).read(
                _dossier(region_image=region_image, page_image=page_image),
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    assert error.value.capacity["answer_budget"] == 4000


@pytest.mark.parametrize(
    "max_model_len,fits",
    [(8192, False), (16384, True)],
    ids=["at-the-old-8192", "at-the-raised-16384"],
)
def test_a_two_view_page_fallback_act_needs_the_raised_perlector_row(
    tmp_path: Path, max_model_len: int, fits: bool
) -> None:
    """Why `perlector@generic-24gb` states 16,384 and not 8,192.

    Two capture views of a page-fallback act send four page-sized images: two
    region crops that are whole pages and two page renders. At the 24 GB tier's
    `max_pixels` each costs 1,715 tokens and a page-fallback act's reading is
    1,318. The prompt is this suite's own small dossier, which the sealed
    tokens-per-character bound puts at 237 -- 4x1,715 + 237 + 1,318 = 8,415. At
    8,192 that is over by 223 and refused on this laptop; at the 16,384 the
    shipped row now states it fits with 7,969 to spare.

    The shipped catalogue is weighed against a real dossier's 1,100 rather than
    against this one's 237
    (`operations/serving/test_serving_catalogue_capacity.py`); what is pinned
    here is the row boundary and the four image costs, which the prompt does not
    move.
    """

    client, endpoint, blob_store, chair = _built(
        tmp_path, max_pixels=1806336, max_model_len=max_model_len
    )
    regions = [
        _image_bytes(b"FALLBACK-REGION-1", width=2480, height=3508),
        _image_bytes(b"FALLBACK-REGION-2", width=2480, height=3508),
    ]
    pages = [
        _image_bytes(b"PAGE-1", width=2480, height=3508),
        _image_bytes(b"PAGE-2", width=2480, height=3508),
    ]
    dossier, pixels = _two_view_act(region_images=regions, page_images=pages)
    endpoint.script(ScriptedAnswer(content="a reading", finish_reason="stop"))
    with client:
        if fits:
            result = _reader(client, chair).read(
                dossier, pass_kind="perlectio", delivered_pixels=pixels
            )
            assert result["stop_reason"] == "stop"
            assert len(endpoint.requests) == 1
            return
        with pytest.raises(RequestCapacityRefusal) as error:
            _reader(client, chair).read(dossier, pass_kind="perlectio", delivered_pixels=pixels)
    record = error.value.capacity
    assert [entry["image_prompt_tokens"] for entry in record["images"]] == [1715] * 4
    assert record["prompt_tokens"] == 237
    assert record["prompt_tokens_basis"] == "measured-upper-bound-for-this-prompt-shape"
    assert record["answer_budget"] == 1318
    assert (record["need"], record["headroom"]) == (8415, -223)
    assert endpoint.requests == []
    assert len(blob_store) == 0


# --- admission is on the upper bound, not on the floor -----------------------

# One 73-word 18th-century French baptism act, the register prose the sealed
# tokens-per-character bound was measured over. Retyped here rather than
# imported: what a testimonium reports is a stage's own evidence, and what is
# being proved is that a dossier carrying real act text is weighed by its real
# size.
_REGISTER_ACT = (
    "L'an mil sept cent quarante et un, le douziesme jour du mois de septembre, a este "
    "baptisee par nous soussigne prestre cure de cette paroisse Marie Anne fille legitime "
    "de Jean Baptiste Dubois laboureur et de Francoise Lemoine son espouse, nee du jour "
    "precedent ; le parrain a este Pierre Dubois oncle paternel de l'enfant, et la marraine "
    "Anne Lemoine tante maternelle, lesquels ont declare ne scavoir signer de ce enquis "
    "suivant l'ordonnance."
)


def _dossier_with_testimonia(*, region_image: bytes, page_image: bytes, witnesses: int, acts: int):
    """The same dossier, carrying `witnesses` testimonia of `acts` acts each."""

    dossier = _dossier(region_image=region_image, page_image=page_image)
    dossier["testimonia"] = [
        {
            "witness_label": f"attestator_{index + 1}",
            "training_domain": "general-ocr",
            "reported": " ".join([_REGISTER_ACT] * acts),
            "model_name": "a witness",
            "resolved_provenance": {
                "repo": "example/witness",
                "revision": "0" * 40,
                "adapter": "witness.v1",
                "scope": "page",
            },
        }
        for index in range(witnesses)
    ]
    return dossier


def test_a_real_dossier_the_floor_admits_and_the_bound_refuses_is_refused(
    tmp_path: Path,
) -> None:
    """The defect: a lower bound was deciding admission.

    Five witnesses reporting four acts each is an ordinary page's testimony, not
    a pathological input. Its prompt costs at least 2,512 tokens by the measured
    floor and at most 4,466 by the measured bound. At a row stating 6,144, with
    a 1,404-token region crop, a 1,715-token page render and one act's
    216-token reading beside it, the floor's 5,847 fits with 297 to spare and
    the bound's 7,801 is over by 1,657.

    The floor admitted it, the engine would not have: `prompt_tokens +
    max_tokens > max_model_len` is answered with HTTP 400 before generation, the
    Perlector has no `failed` shape to publish for an act nothing read, and the
    pass stops. It is refused here instead, on this laptop, with the arithmetic
    on the exception.
    """

    client, endpoint, blob_store, chair = _built(tmp_path, max_pixels=1806336, max_model_len=6144)
    region_image = _image_bytes(b"REGION", width=2480, height=584)
    page_image = _image_bytes(b"PAGE", width=2480, height=3508)
    dossier = _dossier_with_testimonia(
        region_image=region_image, page_image=page_image, witnesses=5, acts=4
    )
    with client:
        row = client.handle.profile
        with pytest.raises(RequestCapacityRefusal) as error:
            _reader(client, chair).read(
                dossier,
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    record = error.value.capacity
    assert [entry["image_prompt_tokens"] for entry in record["images"]] == [1404, 1715]
    assert record["prompt_tokens"] == 4466
    assert record["prompt_tokens_basis"] == "measured-upper-bound-for-this-prompt-shape"
    assert record["prompt_tokens_floor"] == 2512
    assert record["prompt_tokens_floor_basis"] == "measured-tokens-per-word-extrapolation"
    assert record["answer_budget"] == 216
    assert (record["need"], record["headroom"]) == (7801, -1657)
    assert record["fits"] is False
    # Nothing was sent, so nothing could have been answered with a 400.
    assert endpoint.requests == []
    assert len(blob_store) == 0

    # The counterfactual, executed against the same row and the same request:
    # the floor this seam used to admit on says the request fits.
    rendered = prompts.build_prompt(chair.serving_recipe, chair.role, dossier, None)
    floor, floor_basis = perlector_prompt_tokens(rendered)
    admitted_by_the_floor = request_fits(
        row,
        [(2480, 584), (2480, 3508)],
        floor,
        216,
        prompt_tokens_basis=PROMPT_TOKENS_MEASURED_CONSTANT,
    )
    assert (floor, floor_basis) == (2512, "measured-tokens-per-word-extrapolation")
    assert admitted_by_the_floor["need"] == 5847
    assert admitted_by_the_floor["headroom"] == 297
    assert admitted_by_the_floor["fits"] is True


def test_the_same_dossier_is_admitted_where_its_bound_really_fits(tmp_path: Path) -> None:
    """Not a refusal that fires on everything: one more token of context admits it."""

    client, endpoint, _blobs, chair = _built(tmp_path, max_pixels=1806336, max_model_len=7801)
    region_image = _image_bytes(b"REGION", width=2480, height=584)
    page_image = _image_bytes(b"PAGE", width=2480, height=3508)
    dossier = _dossier_with_testimonia(
        region_image=region_image, page_image=page_image, witnesses=5, acts=4
    )
    endpoint.script(ScriptedAnswer(content="a reading", finish_reason="stop"))
    with client:
        result = _reader(client, chair).read(
            dossier,
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    assert result["stop_reason"] == "stop"
    assert len(endpoint.requests) == 1


def test_the_sealed_bound_is_bound_to_the_prompt_builder_this_reader_renders_through():
    """The digest that expires the measurement, reconciled where both are visible.

    `prompts.py` is the Perlector's prompt template: the rendered bytes are the
    module's own f-strings, which is why `prompt_evidence` already records the
    whole module's digest as `builder_sha256`. The sealed tokens-per-character
    bound is measured over those bytes, so it is bound to that same digest --
    edit the builder and the measurement expires rather than describing text
    nobody renders any more.
    """

    chair = _chair()
    dossier = _dossier(region_image=_image_bytes(b"r"), page_image=_image_bytes(b"p"))
    dossier["dossier_digest"] = "0" * 64
    evidence = prompts.prompt_evidence(chair, dossier)
    assert live_reader._PROMPT_TEMPLATE_DIGEST == evidence["builder_sha256"]
    assert PERLECTOR_PROMPT_TEMPLATE_DIGEST == evidence["builder_sha256"]


def test_an_edited_prompt_template_expires_the_measured_bound():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        perlector_prompt_bound("a rendered prompt", template_digest="f" * 64)
    message = str(refusal.value)
    assert "prompt template changed after it was measured" in message
    assert PERLECTOR_PROMPT_TEMPLATE_DIGEST in message


def test_the_measured_overhead_covers_the_protocols_own_image_ceiling():
    """The one config number the sealed overhead is charged at.

    `PERLECTOR_PROMPT_OVERHEAD_TOKENS` is the chat template's measured 52 tokens
    for the turn plus 2 for each image, charged at
    `config/perlector_protocol.toml`'s `max_images` rather than at the images a
    given request carries -- so it is an upper bound for anything this seam can
    build. Raising that ceiling expires the constant, and this is what fails
    when it moves.
    """

    protocol = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "config/perlector_protocol.toml").read_text()
    )
    max_images = protocol["max_images"]
    assert max_images == PERLECTOR_MAX_IMAGES_THE_OVERHEAD_COVERS
    assert PERLECTOR_PROMPT_OVERHEAD_TOKENS == 52 + 2 * max_images


def test_an_act_that_fits_carries_its_capacity_record_onto_the_retained_call_record(
    tmp_path: Path,
) -> None:
    """The admitted path: the arithmetic travels with the request it allowed."""

    import json as _json

    client, endpoint, blob_store, chair = _built(tmp_path)
    endpoint.script(ScriptedAnswer(content="a reading", finish_reason="stop"))
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        result = _reader(client, chair).read(
            _dossier(region_image=region_image, page_image=page_image),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(region_image=region_image, page_image=page_image),
        )
    call_record = next(
        payload
        for payload in (
            _json.loads(written)
            for written in blob_store.written
            if written.lstrip().startswith(b"{")
        )
        if payload.get("schema") == "chair-call-record.v1"
    )
    capacity = call_record["capacity"]
    assert capacity["schema"] == "verbatus-request-capacity.v1"
    assert capacity["fits"] is True
    assert capacity["chair"] == "perlector"
    # Admitted on the measured upper bound, with the measured floor recorded
    # beside it and its own basis named.
    assert capacity["prompt_tokens_basis"] == "measured-upper-bound-for-this-prompt-shape"
    assert capacity["prompt_tokens_floor_basis"] in {
        "measured-floor-for-this-prompt-shape",
        "measured-tokens-per-word-extrapolation",
    }
    assert isinstance(capacity["prompt_tokens_floor"], int)
    assert result["stop_reason"] == "stop"


def test_a_prompt_too_long_400_is_retained_refused_by_name_and_not_a_length_stop(
    tmp_path: Path,
) -> None:
    """The Perlector's own reason for caring which of the two this is.

    This reader sends no `max_tokens` on purpose, so that an engine `"length"`
    honestly means the context itself was exhausted rather than that the
    harness cut the reading short (`truncation.py`). The cost of that honesty
    is that a *prompt*-side overrun cannot arrive as `"length"` at all: it
    arrives as an HTTP 400 with no choices, before generation, and
    `EngineSignalRefusal` never runs. It must therefore surface as the client's
    own named response refusal with the bytes retained, not as a truncated
    reading.
    """

    client, endpoint, blob_store, chair = _built(tmp_path)
    refusal = scripted_prompt_too_long(
        max_model_len=2048,
        requested_tokens=4125,
        prompt_tokens=3909,
        completion_tokens=216,
    )
    endpoint.script(refusal)
    region_image, page_image = _image_bytes(b"r"), _image_bytes(b"p")
    with client:
        with pytest.raises(ChairResponseRefusal) as error:
            _reader(client, chair).read(
                _dossier(region_image=region_image, page_image=page_image),
                pass_kind="perlectio",
                delivered_pixels=_delivered_pixels(
                    region_image=region_image, page_image=page_image
                ),
            )
    assert error.value.code == "CHAIR_RESPONSE_HTTP_ERROR"
    assert "maximum context length is 2048" in error.value.detail
    assert blob_store.has(hashlib.sha256(refusal.body).hexdigest())
    # Not an EngineSignalRefusal, and no LectioResult: nothing here reads a 400
    # as a stop reason of any kind.
    assert not isinstance(error.value, EngineSignalRefusal)


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
