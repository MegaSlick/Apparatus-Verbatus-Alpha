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
building a request, and turning one retained response into an `Attempt`
-- with the real `churro.v1` and `chandra.v1` adapters brought in only where
a test specifically wants to prove real wiring, not a stub's promise.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

STAGE = Path(__file__).resolve().parent
if str(STAGE) not in sys.path:
    sys.path.insert(0, str(STAGE))

import chandra  # noqa: E402
import churro  # noqa: E402
import feeding  # noqa: E402
import live_witness  # noqa: E402
import witness_adapters  # noqa: E402
from chandra import FIXTURE_RESPONSE_SCHEMA as CHANDRA_FIXTURE_SCHEMA  # noqa: E402

from common import native_witness  # noqa: E402
from common.chairs.models import ChairIdentity  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.contracts.serving import STOP_REASON_UNREPORTED  # noqa: E402
from common.contracts.stages import ATTESTATORES  # noqa: E402
from common.decoding import witness_loop_guard  # noqa: E402
from common.imaging import encode_grayscale_png  # noqa: E402
from common.native_witness import CHURRO_OUTPUT_TOKENS  # noqa: E402
from common.request_capacity import (  # noqa: E402
    DECLARED_ANSWER_BOUND_TOKENS,
    RequestCapacityRefusal,
    request_fits,
    sealed_prompt_tokens,
)
from common.stage import StageContext  # noqa: E402
from operations.serving.client import ChairClient, ChairRequest  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    ServingProfile,
    chair_preflight_identity_digest,
    load_serving_recipes,
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
    shipped_decoding_policy,
)
from operations.serving.manager import ServingManager  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

REVISION = "a" * 40
MANIFEST = "b" * 64
DECODING_SHA = shipped_decoding_policy()[1]
TIER = "generic-48gb"
REPO_ROOT = STAGE.parents[1]
REAL_RECIPES = REPO_ROOT / "config" / "serving_recipes_real.toml"
CHURRO_SERVED_MODEL_ID = "attestator-3-churro"


def _sealed_churro_rows() -> tuple[ServingProfile, ...]:
    """Every Churro row in the shipped real catalogue, at every tier.

    Read from the file the operator actually ships, not a hand-typed copy: the
    defect this guards against was a sent bound that no shipped row could
    accept, so the shipped bytes are the only ones worth asserting against.
    """

    rows = tuple(
        profile
        for profile in load_serving_recipes(REAL_RECIPES).profiles
        if isinstance(profile, ServingProfile) and profile.served_model_id == CHURRO_SERVED_MODEL_ID
    )
    assert rows, f"the shipped real catalogue names no {CHURRO_SERVED_MODEL_ID} serving row"
    return rows


DAI_SERVED_MODEL_ID = "attestator-2-dai"


def _sealed_row(served_model_id: str, tier: str = TIER) -> ServingProfile:
    """One shipped real row, read from the catalogue the operator actually ships."""

    rows = [
        profile
        for profile in load_serving_recipes(REAL_RECIPES).profiles
        if isinstance(profile, ServingProfile)
        and profile.served_model_id == served_model_id
        and profile.tier == tier
    ]
    assert len(rows) == 1, f"{served_model_id} at {tier} is not exactly one shipped row"
    return rows[0]


def _dai_row(tier: str = TIER) -> ServingProfile:
    return _sealed_row(DAI_SERVED_MODEL_ID, tier)


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


class _Context(SimpleNamespace):
    stage = ATTESTATORES
    sealed = False
    retain = StageContext.retain


def _png(width: int, height: int) -> bytes:
    """A real PNG of a named size.

    The request builders now measure what they are about to embed
    (`live_witness.request_capacity_or_refuse` reads the bytes' own IHDR), so a
    presentation whose "image" is a short byte string no longer describes
    anything a chair could be charged for.  Small on purpose: these drills are
    about framing, not about the pixel budget, which
    `common/test_request_capacity.py` pins directly.
    """

    return encode_grayscale_png(width, height, [bytearray(width) for _ in range(height)])


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


def test_record_chair_request_builds_the_dai_two_message_framing_and_generation_split():
    context = _Context(tree=_FakeTree())
    image_bytes = _png(40, 30)
    presentation = _presentation(kind="region", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt)

    built = live_witness.record_chair_request(context, adapter, presentation, profile=_dai_row())
    request = built.request

    assert request.kind == "chat-completions"
    assert request.image_sha256s == (digest_bytes(image_bytes),)
    assert _decoded_images(request) == [image_bytes]
    system, user = request.messages
    # DAI's README sends the system turn as a one-element list of text parts,
    # not a bare string.
    assert system == {
        "role": "system",
        "content": [{"type": "text", "text": feeding.dai_prompt()["system"]}],
    }
    assert user["role"] == "user"
    assert user["content"][0]["type"] == "image_url"
    assert user["content"][1] == {"type": "text", "text": feeding.dai_prompt()["user"]}
    # `presented`/`prompt` are carried forward so `live_attempt_from_response`
    # never has to run `adapter.present` a second time for this same record.
    assert built.presented == presentation
    assert built.prompt == feeding.dai_prompt()
    assert built.generation_accounting == feeding.dai_generation_accounting()

    declared = feeding.dai_generation()
    assert request.generation_declared == declared
    capacity = built.capacity
    # Sampling values are the sealed table's, added by the client, never the builder's.
    assert dict(request.generation_sent) == {
        # DAI's own model card runs it at `max_new_tokens=1024`, and this crop
        # leaves the row far more room than that, so the declared bound wins.
        "max_tokens": DECLARED_ANSWER_BOUND_TOKENS["attestator_2"],
        # The second EOS id in the carried config, sent as well as read by the
        # engine from the pinned file.
        "stop_token_ids": [151643],
    }
    assert declared["eos_token_id"] == [feeding.DAI_TOKENIZER_EOS_TOKEN_ID, 151643]
    # The declared bound is what binds here -- far below what the row leaves --
    # which is why it goes on the wire at all, and the difference is the margin
    # against vLLM counting the prompt higher than this seam can.
    assert (
        capacity["max_model_len"] - capacity["image_prompt_tokens"] - capacity["prompt_tokens"]
        > DECLARED_ANSWER_BOUND_TOKENS["attestator_2"]
    )
    for forbidden in ("temperature", "do_sample", "bos_token_id", "eos_token_id", "pad_token_id"):
        assert forbidden not in request.generation_sent


def test_record_chair_request_refuses_a_presented_image_that_does_not_match_its_own_digest():
    context = _Context(tree=_FakeTree())
    presentation = _presentation(kind="region", image_bytes=_png(40, 30))
    context.tree.seed(presentation["image_path"], _png(41, 30))
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt)

    with pytest.raises(SchemaRefusal):
        live_witness.record_chair_request(context, adapter, presentation, profile=_dai_row())


def _churro_page_request(profile: Any):
    context = _Context(tree=_FakeTree())
    image_bytes = _png(50, 70)
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    # The adapter's own prompt: what the registry binds for a served chair is
    # the vendor's registry-resolved system string, and the capacity check
    # weighs the prompt the request will really carry.
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=churro.prompt)
    request = live_witness.page_chair_request(
        context, adapter, "churro.v1", presentation, profile=profile
    )
    return request, digest_bytes(image_bytes)


def test_page_chair_request_builds_churros_system_only_framing_and_declares_the_token_bound():
    row = _sealed_churro_rows()[0]
    request, image_sha256 = _churro_page_request(row)

    assert request.image_sha256s == (image_sha256,)
    system, user = request.messages
    # Churro's own `HFChatTemplate.build_conversation` sends the system turn
    # as a one-element list of text parts, not a bare string, and the profile
    # sets `user_prompt=None`, so the user turn carries the image alone.
    assert system == {
        "role": "system",
        "content": [{"type": "text", "text": churro.prompt()["system"]}],
    }
    assert user["content"] == [user["content"][0]]
    assert user["content"][0]["type"] == "image_url"
    # The declaration is unchanged and still retained on every request.
    assert request.generation_declared == {"max_new_tokens": CHURRO_OUTPUT_TOKENS}
    # The row holds the vendor's whole 25,000-token bound beside the page, so
    # the bound is what binds and it goes on the wire.
    assert CHURRO_OUTPUT_TOKENS < row.max_model_len
    # Sampling values are the sealed table's, added by the client, never the builder's.
    assert dict(request.generation_sent) == {"max_tokens": CHURRO_OUTPUT_TOKENS}


def test_every_sealed_churro_row_at_every_tier_takes_the_bound_this_seam_sends():
    """The defect, asserted against the shipped catalogue rather than one row.

    vLLM admits a request when `prompt_tokens + max_tokens <= max_model_len`,
    so that whole sum is what is asserted here -- against the request's own
    capacity record, which counted this exact page image and this exact prompt
    against this exact row.
    """

    rows = _sealed_churro_rows()
    assert {row.tier for row in rows} == {"generic-24gb", "generic-48gb", "generic-80gb-plus"}
    for row in rows:
        request, _ = _churro_page_request(row)
        assert request.generation_declared == {"max_new_tokens": CHURRO_OUTPUT_TOKENS}
        sent = dict(request.generation_sent)
        assert set(sent) <= {"max_tokens"}
        capacity = request.capacity
        assert capacity is not None
        prompt = capacity["image_prompt_tokens"] + capacity["prompt_tokens"]
        # Every tier holds the vendor's whole bound, so it is always sent.
        assert sent["max_tokens"] == CHURRO_OUTPUT_TOKENS, row.tier
        assert prompt + sent["max_tokens"] < row.max_model_len, row.tier


def test_a_churro_row_too_short_for_the_vendor_bound_refuses_the_page_instead_of_cutting_it():

    rows = _sealed_churro_rows()
    assert [row.max_model_len for row in rows] == [32768, 32768, 32768]
    short = dataclasses.replace(rows[0], max_model_len=8192)
    with pytest.raises(RequestCapacityRefusal) as error:
        _churro_page_request(short)
    assert error.value.capacity["answer_budget"] == CHURRO_OUTPUT_TOKENS
    assert error.value.capacity["fits"] is False


def _stand_in_row(max_model_len, chair="attestator_3"):
    """A row of a stated length, with the geometry a capacity record needs."""

    return SimpleNamespace(
        max_model_len=max_model_len,
        min_pixels=3136,
        max_pixels=3211264,
        patch_size=14,
        merge_size=2,
        recipe="r",
        chair=chair,
        tier="t",
    )


def _capacity_for(chair, *, max_model_len, prompt_tokens):
    """One closed capacity record carrying an exact prompt cost and no images."""

    return request_fits(_stand_in_row(max_model_len, chair), [], prompt_tokens, 1)


@pytest.mark.parametrize("chair", sorted(DECLARED_ANSWER_BOUND_TOKENS))
def test_the_declared_bound_is_sent_only_where_it_is_what_binds(chair):
    """`min(declared, max_model_len - image - prompt)`, with the row term
    expressed by sending no field -- checked either side of the crossover and
    exactly on it.

    Sending no bound at all lets the engine set the budget to
    `max_model_len - prompt` -- some 7,700 tokens for a DAI record crop whose own
    publisher runs it at 1,024.
    """

    declared = DECLARED_ANSWER_BOUND_TOKENS[chair]
    prompt = 500

    # The row leaves less than the declared bound: nothing is sent, and the
    # engine bounds generation by `max_model_len` exactly as it always did.
    for room in (declared - 1, declared):
        assert (
            live_witness.generation_bound_sent(
                chair, _capacity_for(chair, max_model_len=prompt + room, prompt_tokens=prompt)
            )
            == {}
        )
    # One token past the crossover, and far past it: the declared bound is what
    # binds, so it goes out and stops growing with the row.
    for extra in (1, 10_000):
        assert live_witness.generation_bound_sent(
            chair,
            _capacity_for(chair, max_model_len=prompt + declared + extra, prompt_tokens=prompt),
        ) == {"max_tokens": declared}


def test_the_row_term_is_never_put_on_the_wire_as_our_own_count_of_it():
    """The failure a `min` sent literally would have introduced.

    vLLM admits on `prompt + max_tokens <= max_model_len` measured by *its* own
    prompt assembly, and this repository's count is a measured floor that has
    never been observed to agree with it (`common/request_capacity.py`). A
    request that sent the row's remainder as `max_tokens` would turn a
    one-token undercount into an HTTP 400 before generation, on a card billing
    by the hour -- so where the row is what binds, nothing is sent, and where
    the vendor's bound is what binds, the gap between the two is the margin.
    """

    page_prompt = 2280 + 441
    declared = DECLARED_ANSWER_BOUND_TOKENS["attestator_3"]
    # A row one token short of holding the declared bound beside this prompt:
    # a literal `min` would have sent `declared - 1`, with zero slack.
    assert (
        live_witness.generation_bound_sent(
            "attestator_3",
            _capacity_for(
                "attestator_3",
                max_model_len=declared + page_prompt - 1,
                prompt_tokens=page_prompt,
            ),
        )
        == {}
    )
    # And where a bound is sent, the slack is the whole difference.
    row_length = declared + page_prompt + 5_000
    sent = live_witness.generation_bound_sent(
        "attestator_3",
        _capacity_for("attestator_3", max_model_len=row_length, prompt_tokens=page_prompt),
    )
    assert sent == {"max_tokens": declared}
    assert row_length - page_prompt - sent["max_tokens"] == 5_000


def test_a_generation_bound_is_never_decided_against_something_that_is_not_a_capacity_record():
    """The bound is derived from the record the request was admitted on, or not
    at all: a plain mapping of the same numbers is refused rather than read."""

    real = _capacity_for("attestator_3", max_model_len=8192, prompt_tokens=500)
    for bad in (
        {"max_model_len": 8192, "image_prompt_tokens": 0, "prompt_tokens": 500},
        {key: value for key, value in real.items() if key != "prompt_tokens"},
        {**real, "unexpected": 1},
    ):
        with pytest.raises(RequestCapacityRefusal):
            live_witness.generation_bound_sent("attestator_3", bad)


def test_a_chair_with_no_declared_upstream_bound_is_refused_by_name():
    with pytest.raises(RequestCapacityRefusal) as error:
        live_witness.generation_bound_sent(
            "perlector", _capacity_for("attestator_3", max_model_len=8192, prompt_tokens=500)
        )
    assert "declares no upstream generation bound" in str(error.value)


@pytest.mark.parametrize("value", [None, 0, -1, True, "2048", 2048.0])
def test_page_chair_request_refuses_a_row_that_cannot_state_its_context_bound(value):
    """A row with every other field stated and no usable `max_model_len`.

    The geometry fields are stated so this drill still isolates the context
    bound: the capacity check now runs first and would otherwise refuse on the
    missing patch size instead, which is a different defect.
    """

    row = SimpleNamespace(
        max_model_len=value,
        min_pixels=3136,
        max_pixels=3211264,
        patch_size=14,
        merge_size=2,
        recipe="unproven-real-attestatores",
        chair="attestator_3",
        tier="t",
    )
    with pytest.raises(SchemaRefusal) as error:
        _churro_page_request(row)
    assert "max_model_len" in str(error.value)
    assert "attestator_3" in str(error.value)


@pytest.mark.parametrize("field", ["patch_size", "merge_size", "min_pixels", "max_pixels"])
def test_page_chair_request_refuses_a_row_that_cannot_state_its_image_geometry(field):
    """The token cost of an image is never counted against a default.

    Chandra and the Perlector spend 1,024 px per image token; DAI and Churro
    spend 784.  A row silent about which is refused by name rather than
    counted wrong by a third.
    """

    row = SimpleNamespace(
        max_model_len=4096,
        min_pixels=3136,
        max_pixels=3211264,
        patch_size=14,
        merge_size=2,
        recipe="unproven-real-attestatores",
        chair="attestator_3",
        tier="generic-48gb",
    )
    setattr(row, field, None)
    with pytest.raises(SchemaRefusal) as error:
        _churro_page_request(row)
    assert field in str(error.value)


# --------------------- the pre-send capacity refusal --------------------------


def _page_request_of_size(width: int, height: int, row):
    context = _Context(tree=_FakeTree())
    image_bytes = _png(width, height)
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=churro.prompt)
    return live_witness.page_chair_request(context, adapter, "churro.v1", presentation, profile=row)


def test_a_page_that_fits_carries_its_capacity_record_onto_the_request():
    request = _page_request_of_size(50, 70, _sealed_churro_rows()[0])
    capacity = dict(request.capacity)
    assert capacity["schema"] == "verbatus-request-capacity.v1"
    assert capacity["fits"] is True
    assert capacity["chair"] == "attestator_3"
    # Churro's own measured prompt cost and dense-page answer budget, not a
    # guess. The prompt cost is measured for the vendor's registry-resolved
    # system string, a single sentence.
    assert capacity["prompt_tokens"] == 27
    # Measured over the vendor's own `HistoricalDocument` grammar.
    # Churro reserves the vendor's whole answer bound, not the measured page.
    assert capacity["answer_budget"] == CHURRO_OUTPUT_TOKENS


def test_a_real_page_is_refused_before_anything_is_sent_and_the_refusal_names_the_numbers():
    """The counterfactual this unit exists for, at the context the tree shipped.

    A 300-dpi A4 page is 2,480x3,508. Against the Churro row's own
    `max_pixels` (401,408 / 4,014,080, its trained geometry, the same at every
    tier) it costs 5,100 image tokens; with the measured 27-token vendor
    system string and the vendor's 25,000-token answer bound that is 30,127 --
    against a `max_model_len = 2048` row, which answers HTTP 400 for real, so
    nothing is built. The shipped row is 32,768 and admits the same page,
    which `operations/serving/test_serving_catalogue_capacity.py` asserts; the
    row is reconstructed here because the drill is about the refusal, not the
    row.
    """

    shipped = [row for row in _sealed_churro_rows() if row.tier == "generic-24gb"][0]
    row = dataclasses.replace(shipped, max_model_len=2048)
    with pytest.raises(RequestCapacityRefusal) as error:
        _page_request_of_size(2480, 3508, row)
    record = error.value.capacity
    assert record["image_prompt_tokens"] == 5100
    assert record["need"] == 30127
    assert record["headroom"] == 2048 - 30127
    assert record["fits"] is False
    assert "downscaled" in str(error.value)
    # And the row the catalogue actually ships admits it.
    assert shipped.max_model_len == 32768
    admitted = _page_request_of_size(2480, 3508, shipped)
    assert admitted.capacity["fits"] is True


def test_a_page_sized_record_crop_is_refused_at_the_same_row():
    """A DAI record crop as large as the whole page is refused at the same row.

    The measured case from the token study: a 1,291x1,826 crop costs 2,990
    image tokens against DAI's own `max_pixels` (12,845,056, the vendor
    processor's own), which a 2,048-token row cannot hold beside an 84-token
    prompt even with the *smaller* one-record answer budget reserved.
    ``adapter.present`` is stubbed to hand the presentation back unchanged, so
    this drill exercises `request_capacity_or_refuse`'s own arithmetic on a
    fixed image size, not the resize rule. The image cost alone is what
    settles it -- which is why reserving one record's answer rather than a
    page's does not let a page-sized record through.
    """

    context = _Context(tree=_FakeTree())
    image_bytes = _png(1291, 1826)
    presentation = _presentation(kind="region", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt)
    # At the context this catalogue shipped until this branch. The shipped row
    # is 8,192 now and holds it.
    row = dataclasses.replace(_dai_row("generic-24gb"), max_model_len=2048)

    with pytest.raises(RequestCapacityRefusal) as error:
        live_witness.record_chair_request(context, adapter, presentation, profile=row)
    record = error.value.capacity
    assert record["image_prompt_tokens"] == 2990
    assert record["need"] == 2990 + 84 + 230
    assert record["fits"] is False
    assert _dai_row("generic-24gb").max_model_len == 8192


def test_an_ordinary_record_crop_still_fits_the_smallest_row():

    context = _Context(tree=_FakeTree())
    image_bytes = _png(1500, 353)
    presentation = _presentation(kind="region", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt)

    built = live_witness.record_chair_request(
        context, adapter, presentation, profile=_dai_row("generic-24gb")
    )
    assert built.capacity["image_prompt_tokens"] == 702
    assert built.capacity["fits"] is True
    # The request seals a detached, deep-frozen snapshot of the record it was
    # admitted on (`operations/serving/client.py::_sealed_capacity`): mappings
    # are `MappingProxyType` and the `images` list is a tuple, so the two are
    # compared as content rather than as objects.
    sealed = built.request.capacity
    assert sealed is not None
    assert {key: value for key, value in sealed.items() if key != "images"} == {
        key: value for key, value in built.capacity.items() if key != "images"
    }
    assert [dict(image) for image in sealed["images"]] == built.capacity["images"]


def test_every_measured_witness_prompt_constant_still_matches_the_prompt_that_is_sent():
    """The digests that expire the measured constants, checked where they live.

    A prompt edit that leaves `common/request_capacity.py`'s measured token
    count in place fails here rather than reaching a pod with a stale number.
    """

    chandra_module = sys.modules.get("chandra") or __import__("chandra")
    # The vendor's own carried prompt bytes, measured for them: 593 over the
    # 2,161-character `OCR_LAYOUT_PROMPT`.
    assert sealed_prompt_tokens("attestator_1", chandra_module.prompt()["user"]) == 593
    dai = feeding.dai_prompt()
    assert sealed_prompt_tokens("attestator_2", dai["system"], dai["user"]) == 84
    # Churro carries a constant per declared framing, because a run can ask it
    # in either and a framing whose cost nobody measured could not be sent at
    # all.  Both are sealed to their own text, so an edit to one does not
    # silently borrow the other's number.  Both are the vendor's own bytes: a
    # single system sentence.
    registry = churro.prompt("registry-v0.3.0")
    assert sealed_prompt_tokens("attestator_3", registry["system"]) == 27
    paper = churro.prompt("paper-harness-ed09bc7")
    assert sealed_prompt_tokens("attestator_3", paper["system"]) == 29
    with pytest.raises(RequestCapacityRefusal) as expired:
        sealed_prompt_tokens("attestator_3", registry["system"] + " ")
    assert "the prompt changed after it was measured" in str(expired.value)


def test_page_chair_request_builds_chandras_single_user_turn():
    context = _Context(tree=_FakeTree())
    image_bytes = _png(51, 70)
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    chandra_module = sys.modules.get("chandra") or __import__("chandra")
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=chandra_module.prompt)

    # Reused for its shape only: `sendable_max_tokens` now refuses a capacity
    # record admitted for a different chair than the one it is asked to bound,
    # so a row borrowed across chairs must be relabelled to the chair this
    # request is actually for.
    chandra_row = dataclasses.replace(
        _sealed_churro_rows()[0], chair="attestator_1", max_model_len=8192
    )
    request = live_witness.page_chair_request(
        context, adapter, "chandra.v1", presentation, profile=chandra_row
    )

    assert len(request.messages) == 1
    (message,) = request.messages
    assert message["role"] == "user"
    assert message["content"][0]["type"] == "image_url"
    assert message["content"][1] == {"type": "text", "text": chandra_module.prompt()["user"]}
    # `chandra/settings.py::MAX_OUTPUT_TOKENS` at the pinned commit: the
    # vendor's own declared answer bound, retained as evidence whether or not
    # it is what binds on the wire.
    assert request.generation_declared == {"max_new_tokens": 12384}
    capacity = request.capacity
    room = capacity["max_model_len"] - capacity["image_prompt_tokens"] - capacity["prompt_tokens"]
    # The row is what binds here -- 12,384 is far above what it leaves -- so no
    # bound goes on the wire and the engine's own budget governs, exactly as
    # before (`common/request_capacity.py::sendable_max_tokens`).
    assert room < DECLARED_ANSWER_BOUND_TOKENS["attestator_1"]
    assert dict(request.generation_sent) == {
        # Thinking mode closed, whichever of the revision's two disagreeing
        # chat templates the engine resolves (`common/chair_wire.py`).
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_page_messages_builds_churros_registry_fixed_system_only_framing():
    """The shape a served Churro chair is asked in, and the only one it has.

    `providers/specs.py::churro_3b_profile()` sets `user_prompt=None`, so the
    system turn carries the whole instruction and the user turn carries the
    image alone. Exercised directly against `_page_messages` as well as through
    `page_chair_request` elsewhere, because the dispatch is pure and provable
    without a sealed row standing in the way.
    """

    image_bytes = _png(12, 9)
    system_text = churro.prompt()["system"]
    messages = live_witness._page_messages("churro.v1", {"system": system_text}, image_bytes)

    assert len(messages) == 2
    system, user = messages
    # A one-element list of text parts, the same shape DAI's system turn takes.
    assert system == {"role": "system", "content": [{"type": "text", "text": system_text}]}
    assert user["role"] == "user"
    # Image-only: no vendor user text exists to send under this framing, so no
    # text part is built for it -- unlike every other shape this seam knows.
    assert user["content"] == [
        {"type": "image_url", "image_url": {"url": live_witness._data_uri(image_bytes)}}
    ]
    assert live_witness._prompt_texts({"system": system_text}) == (system_text,)


def test_page_messages_builds_chandras_user_only_framing():
    """Chandra's shape: one user turn, image part first, and no system message.

    Exercised directly against `_page_messages` with a stand-in text, so the
    dispatch is provable without a sealed row or a measured prompt-token
    constant; `test_page_chair_request_builds_chandras_single_user_turn` above
    is the same shape built end to end from the adapter's real carried bytes.
    """

    image_bytes = _png(13, 8)
    user_text = "Transcribe this complete page and report layout blocks in reading order."
    messages = live_witness._page_messages("chandra.v1", {"user": user_text}, image_bytes)

    assert len(messages) == 1
    (message,) = messages
    assert message["role"] == "user"
    assert message["content"][0]["type"] == "image_url"
    assert message["content"][1] == {"type": "text", "text": user_text}
    assert live_witness._prompt_texts({"user": user_text}) == (user_text,)


def test_page_messages_refuses_a_prompt_shape_it_does_not_recognize():
    with pytest.raises(SchemaRefusal, match="unrecognized prompt shape"):
        live_witness._page_messages("churro.v1", {"caption": "x"}, _png(4, 4))


def test_every_live_witness_builder_puts_the_image_part_before_the_text_part():
    """The rebuild regression, pinned once for all three witness chairs.

    Each occupant was fine-tuned with the vision block before the instruction
    (DAI's model-card snippet and our own old `pilot_crops_dai.py`; Chandra's
    `model/vllm.py`; Churro's provider), and each chat template emits a
    message's parts in list order -- so the order here *is* the token sequence
    the model sees. Asserted at every builder together rather than only inside
    each chair's own shape test, because the defect was that all three agreed
    with each other and disagreed with every upstream.
    """

    context = _Context(tree=_FakeTree())
    image_bytes = _png(53, 71)
    chandra_module = sys.modules.get("chandra") or __import__("chandra")

    act_presentation = _presentation(kind="region", image_bytes=image_bytes)
    context.tree.seed(act_presentation["image_path"], image_bytes)
    dai = live_witness.record_chair_request(
        context,
        SimpleNamespace(present=lambda ctx, pres: pres, prompt=feeding.dai_prompt),
        act_presentation,
        profile=_dai_row(),
    ).request

    page_presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(page_presentation["image_path"], image_bytes)
    row = _sealed_churro_rows()[0]
    # Reused for its shape only, relabelled per adapter for the same reason as
    # `test_page_chair_request_builds_chandras_single_user_turn` above.
    page_requests = [
        live_witness.page_chair_request(
            context,
            SimpleNamespace(present=lambda ctx, pres: pres, prompt=prompt),
            adapter_name,
            page_presentation,
            profile=dataclasses.replace(row, chair="attestator_1"),
        )
        for adapter_name, prompt in (("chandra.v1", chandra_module.prompt),)
    ]

    for request in [dai, *page_requests]:
        (user,) = [message for message in request.messages if message["role"] == "user"]
        types = [part["type"] for part in user["content"]]
        assert types == ["image_url", "text"]

    # Churro's user turn carries no text at all under either attested framing,
    # so "image first" is the whole of its content rather than an order.
    churro_request, _ = _churro_page_request(row)
    (user,) = [message for message in churro_request.messages if message["role"] == "user"]
    assert [part["type"] for part in user["content"]] == ["image_url"]


# --- the recorded framing selector (not a picker) ----------------


def test_the_default_framing_is_the_vendors_own_registry_answer():
    """Both arms are a vendor artifact's bytes; the default is the current one.

    `providers/specs.py::resolve_ocr_profile("stanford-oval/churro-3B")` at tag
    `v0.3.0` is what the vendor ships today, and which of the two strings the
    fine-tuning itself saw is stated nowhere -- so the default is the attested
    current answer and the comparison is a Stage 2 arm, not a guess made here.
    """

    assert churro.DEFAULT_FRAMING == "registry-v0.3.0"
    assert churro.prompt() == churro.prompt("registry-v0.3.0")
    assert churro.resolve_framing(None) == churro.DEFAULT_FRAMING


def test_each_declared_framing_asks_its_own_prompt():
    registry = churro.prompt("registry-v0.3.0")
    paper = churro.prompt("paper-harness-ed09bc7")
    assert set(registry) == set(paper) == {"system"}
    assert registry != paper
    # The paper-era harness's two spelling errors are part of the bytes it
    # actually sent, and are carried unaltered.
    assert "entiretly" in paper["system"] and "documents" in paper["system"]
    assert set(churro.FRAMINGS) == {"registry-v0.3.0", "paper-harness-ed09bc7"}


# `None` is deliberately absent from this list: it is the *valid* request for
# the default framing (`churro.resolve_framing`), pinned by
# `test_the_default_framing_is_the_vendors_own_registry_answer` above, and
# putting it here would assert a refusal the adapter is written never to make.
@pytest.mark.parametrize("bad", ["churro-layout-prompt", "", "trained", 1])
def test_an_undeclared_framing_is_refused_rather_than_resolved_to_a_near_match(bad):
    with pytest.raises(SchemaRefusal) as error:
        churro.prompt(bad)
    assert "has no framing named" in str(error.value)


def test_both_declared_framings_are_measured_and_therefore_sendable():
    """A framing whose prompt cost nobody measured is a framing no run can
    send: `sealed_prompt_tokens` refuses it at the capacity check, which would
    make the selector a choice between one option and an error."""

    for framing, expected in (("registry-v0.3.0", 27), ("paper-harness-ed09bc7", 29)):
        prompt = churro.prompt(framing)
        assert sealed_prompt_tokens("attestator_3", prompt["system"]) == expected


def test_a_named_framing_reaches_the_request_and_its_capacity_record():
    """End to end at the builder: the prompt bytes and the measured cost both
    follow the name, so a request under the paper-era harness's framing is
    admitted on that framing's own arithmetic."""

    context = _Context(tree=_FakeTree())
    image_bytes = _png(54, 72)
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=churro.prompt)
    request = live_witness.page_chair_request(
        context,
        adapter,
        "churro.v1",
        presentation,
        profile=_sealed_churro_rows()[0],
        framing="paper-harness-ed09bc7",
    )
    system, user = request.messages
    paper = churro.prompt("paper-harness-ed09bc7")
    assert system["content"] == [{"type": "text", "text": paper["system"]}]
    assert user["content"] == [user["content"][0]]
    assert request.capacity["prompt_tokens"] == 29


def test_the_resolved_framing_is_written_onto_the_capture(tmp_path: Path):
    """Not a picker, stated as a test: this selects the question before the page
    is read, never among readings, and the name it selected is on the record."""

    response, _, _ = _read_one(
        tmp_path,
        script=ScriptedAnswer(
            content="<HistoricalDocument><Page><Body><Line>read</Line></Body></Page></HistoricalDocument>",
            finish_reason="stop",
        ),
    )
    adapter = witness_adapters.resolve_runnable_adapter("churro.v1")
    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()),
        1,
        "attestator_3",
        "churro.v1",
        adapter,
        response,
        framing="paper-harness-ed09bc7",
    )
    view = attempt.native_capture["view"]
    assert view["framing"] == "paper-harness-ed09bc7"
    assert view["prompt"] == churro.prompt("paper-harness-ed09bc7")
    # And the vendor pin follows the bytes, not the name: this framing's string
    # comes from a different file at a different commit.
    assert attempt.native_capture["vendor_identity"]["sha"] == (
        "ed09bc7fd6475c333a25427f3d0b9227af46ce27"
    )


def test_page_chair_request_refuses_an_unrecognized_prompt_shape():
    context = _Context(tree=_FakeTree())
    image_bytes = _png(52, 70)
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=lambda: {"weird": "shape"})

    with pytest.raises(SchemaRefusal):
        live_witness.page_chair_request(
            context, adapter, "made-up.v1", presentation, profile=_sealed_churro_rows()[0]
        )


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
        witness_scope="page",
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
        # The chair's own vision-encoder geometry, as the shipped real
        # catalogue states it: without it nothing can say what one image costs
        # this chair in prompt tokens, and the request builders refuse by name
        # rather than counting against a default (`common/request_capacity.py`).
        "patch_size": 14,
        "merge_size": 2,
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
        return {
            "chair": chair.role,
            "source": chair.source,
            "resolved": chair.source_reference,
            "revision": chair.receipt_revision,
            "revision_kind": chair.receipt_revision_kind,
            "digest_manifest": chair.digest_manifest,
        }

    client = ChairClient(
        manager=manager,
        identity=chair,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256=DECODING_SHA,
        decoding_policy=shipped_decoding_policy()[0],
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

    def retain_fn(tree, *, view, raw_response, transport_stop_reason, parser=None, served=False):
        # `served` is the retention seam's posture flag: every real adapter
        # wrapper takes it and forwards it, and both live call sites pass it,
        # so a stub standing in for one takes it too. It reaches only Chandra's
        # parser, and these stubs run no parser at all,
        # which is why they record it and derive nothing from it.
        del tree, view, parser
        digest = hashlib.sha256(raw_response).hexdigest()
        retained.append(served)
        return {
            **retain_result,
            "transport_stop_reason": transport_stop_reason,
            "raw_response_ref": {"relative_path": f"blobs/sha256/{digest}", "sha256": digest},
        }

    retained: list[bool] = []
    return SimpleNamespace(
        prompt=prompt_fn,
        retain=retain_fn,
        retained_served=retained,
        format_capabilities={"can_express_uncertainty": False, "can_express_layout": False},
    )


def _dai_presentation(*, width: int = 3_000, height: int = 1_001) -> dict[str, Any]:
    """A DAI record presentation shaped to force a resize (`feeding.dai_dimensions`
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


def _dai_identity_view_kwargs(*, crop_bytes: bytes = b"the designator's own record crop"):
    """A DAI record small enough that no resize runs.

    `feeding.dai_dimensions(100, 50)` is `(100, 50)`, so the adapter's crop is
    the Designator's crop, byte for byte. The two references therefore carry
    one digest under two stage-owned paths, which is what a content-addressed
    store means by "the same retained blob": every image a witness is shown is
    inventoried under `3_attestatores/`, while the proposal crop it was cut
    from lives under `2_designator/`. Held to the whole reference dict, this
    record would be refused after its response had already come back.
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
        "generation_accounting": feeding.dai_generation_accounting(),
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
        _Context(tree=_FakeTree()),
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


def test_format_capabilities_is_read_from_the_adapter_when_it_declares_one(tmp_path: Path):
    """The other half: once an adapter names its own grammar's capability, the
    seam reports that rather than the blanket default -- a Testimonium stops
    claiming every witness reports identically the moment its adapter says so.
    """

    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})
    declared = {"can_express_uncertainty": True, "can_express_layout": False}
    adapter.format_capabilities = declared

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.format_capabilities == declared


def test_format_capabilities_on_a_malformed_response_still_names_the_adapters_own_grammar(
    tmp_path: Path,
) -> None:
    """The malformed branch never ran an adapter parser, but what the adapter's
    *grammar* can carry is a fact about the chair, not about whether this one
    body happened to parse -- so it is read the same way there too."""

    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(body=b"not json at all"))
    assert response.parse_problem is not None
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})
    declared = {"can_express_uncertainty": True, "can_express_layout": True}
    adapter.format_capabilities = declared

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.native_capture is None  # the malformed branch, confirmed
    assert attempt.format_capabilities == declared


@pytest.mark.parametrize(
    "bad_declaration",
    [
        "can_express_layout",  # not an object at all
        {"can_express_layout": True},  # missing can_express_uncertainty
        {"can_express_uncertainty": False, "can_express_layout": False, "extra": True},
        {"can_express_uncertainty": "yes", "can_express_layout": False},  # not a bool
        None,
    ],
)
def test_format_capabilities_for_refuses_a_malformed_adapter_declaration(bad_declaration):
    """A declaration that is not the two-key boolean object this seam knows is
    this seam's own bug -- an adapter is code in this tree, not a vendor
    response -- and is refused here, before an immutable Testimonium can carry
    it, rather than only later at the attempt tally."""

    adapter = SimpleNamespace(format_capabilities=bad_declaration)
    with pytest.raises(SchemaRefusal, match="format_capabilities"):
        witness_adapters.declared_format_capabilities(adapter)


def test_format_capabilities_for_propagates_a_malformed_declaration_through_a_live_attempt(
    tmp_path: Path,
):
    """The same refusal reaches a caller that only asked for an `Attempt`,
    so a broken adapter cannot slip a bad declaration past this seam merely by
    being read from a different call site."""

    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})
    adapter.format_capabilities = {"can_express_layout": "not-a-bool"}

    with pytest.raises(SchemaRefusal, match="format_capabilities"):
        live_witness.live_attempt_from_response(
            _Context(tree=_FakeTree()),
            adapter,
            "dai.v1",
            response,
            generation_declared={},
            parser="text",
            **_dai_view_kwargs(),
        )


def test_live_attempt_from_response_refuses_a_non_dai_adapter_name(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})

    with pytest.raises(SchemaRefusal):
        live_witness.live_attempt_from_response(
            _Context(tree=_FakeTree()),
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
        _Context(tree=_FakeTree()),
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
    # ARCHITECTURE's "truncation is a refused reading, never an
    # output": an empty response the engine itself cut off at its token bound
    # is not evidence of a genuinely blank record, on DAI's record reading
    # exactly as on a whole-page reading.
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="length"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank record" in attempt.reason
    assert attempt.health["truncated"] is True


def test_live_attempt_from_response_unreported_empty_is_failed_not_confirmed_blank(
    tmp_path: Path,
):
    # An empty response whose stop boundary was never reported at all is no
    # more a confirmed blank record than one the engine admits it cut off.
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason=ABSENT))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank record" in attempt.reason
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-recorded"


def test_live_attempt_from_response_truncated_true_on_length(tmp_path: Path):
    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="cut off tex", finish_reason="length")
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "cut off tex"}})

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
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
        _Context(tree=_FakeTree()),
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
        _Context(tree=_FakeTree()),
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
    # off" -- an unread signal is never defaulted to a meaning.
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
        _Context(tree=_FakeTree()),
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


@pytest.mark.parametrize("path", ["record", "page"])
def test_cut_off_and_parser_failure_name_both_on_each_witness_path(tmp_path: Path, path: str):
    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="<output>unclosed", finish_reason="length")
    )
    adapter = _stub_adapter(
        retain_result={"parse": {"state": "failed", "reason": "unterminated output element"}}
    )
    context = _Context(tree=_FakeTree())
    if path == "record":
        attempt = live_witness.live_attempt_from_response(
            context,
            adapter,
            "dai.v1",
            response,
            generation_declared={},
            parser="text",
            **_dai_view_kwargs(),
        )
    else:
        attempt = live_witness.captured_page_attempt(
            context, 1, "attestator_1", "churro.v1", adapter, response
        )

    assert attempt.outcome == "failed"
    assert "stopped the response at its bound" in attempt.reason
    assert "unterminated output element" in attempt.reason
    assert "length" in attempt.health["truncation_basis"]
    assert "unterminated output element" in attempt.health["truncation_basis"]


def test_live_attempt_from_response_parser_failure_without_cut_off_keeps_verbatim_reason(
    tmp_path: Path,
):
    # Without a recognized cut-off, DAI's record parse-failure reason and
    # content-health basis stay exactly the parse reason -- no truncation
    # language gets folded in when the provider never reported one.
    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="not valid for this parser", finish_reason="stop")
    )
    adapter = _stub_adapter(
        retain_result={"parse": {"state": "failed", "reason": "could not decode as text"}}
    )

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "failed"
    assert attempt.reason == (
        "the provider response was retained but not usable: could not decode as text"
    )
    assert attempt.health["truncation_basis"] == "could not decode as text"
    assert "stopped the response at its bound" not in attempt.reason


def test_live_attempt_from_response_failed_on_a_malformed_wire_body(tmp_path: Path):
    # No choices at all: parse_openai_reading refuses before any native parse
    # is possible, and `ChairClient.read` records `parse_problem`, not content.
    response, endpoint, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(body=b'{"model":"served-alias","choices":[]}')
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "never reached"}})

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
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
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.receipt_ref == dict(response.receipt_ref)
    assert attempt.serving_call_ref == dict(response.call_record_ref)


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
        _Context(tree=_FakeTree()),
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
    assert attempt.native_capture["view"]["adapter"] == "dai-atr.v2"
    assert blob_store.has(response.response_sha256)


def test_a_no_resize_dai_record_is_carried_rather_than_refused_after_its_answer(tmp_path: Path):
    """The identity transform: no resize, carried rather than refused.

    Every record crop in the reference fixture is small enough that DAI needs
    no resize, so this is the ordinary DAI record. `dai_model_view`'s identity
    rule compares the digest the two references share across two stages' blob
    namespaces, so the model was shown exactly the source bytes.
    """

    response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="texte transcrit", finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("dai.v1")
    view_kwargs = _dai_identity_view_kwargs()
    assert view_kwargs["presentation"]["image_sha256"] == view_kwargs["presented"]["image_sha256"]
    assert view_kwargs["presentation"]["image_path"] != view_kwargs["presented"]["image_path"]

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **view_kwargs,
        generation_accounting=feeding.dai_generation_accounting(),
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


def test_a_no_resize_dai_record_whose_model_image_is_other_bytes_is_still_refused(tmp_path: Path):
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
            _Context(tree=_FakeTree()),
            adapter,
            "dai.v1",
            response,
            generation_declared=feeding.dai_generation(),
            parser="text",
            **view_kwargs,
            generation_accounting=feeding.dai_generation_accounting(),
        )


def test_a_live_record_says_which_kind_of_bytes_it_retained(tmp_path: Path):
    """`raw_response_ref` names which kind of bytes it holds.

    It means the adapter's own output on every branch where a
    parser ran, and the whole transport body on the one branch where none
    could. Two kinds of evidence under one field name, and nothing said which.
    """

    parsed_response, _, _ = _read_one(
        tmp_path, script=ScriptedAnswer(content="texte transcrit", finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("dai.v1")
    parsed = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        parsed_response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **_dai_identity_view_kwargs(),
        generation_accounting=feeding.dai_generation_accounting(),
    )
    assert parsed.raw_response_kind == "model-output"
    assert parsed.raw_response_ref == dict(parsed.native_capture["raw_response_ref"])

    malformed_response, _, _ = _read_one(
        tmp_path / "second", script=ScriptedAnswer(body=b"not json at all")
    )
    assert malformed_response.parse_problem is not None
    malformed = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        malformed_response,
        generation_declared=feeding.dai_generation(),
        parser="text",
        **_dai_identity_view_kwargs(),
        generation_accounting=feeding.dai_generation_accounting(),
    )
    assert malformed.raw_response_kind == "transport-response-body"
    assert malformed.native_capture is None
    # The two really are different bytes: the envelope, and the model's output.
    assert malformed.raw_response_ref == dict(malformed_response.raw_response_ref)
    assert parsed.raw_response_ref != dict(parsed_response.raw_response_ref)


# =========================== captured_page_attempt =============================


def test_captured_page_attempt_read_on_a_complete_stop(tmp_path: Path):
    response, _, blob_store = _read_one(
        tmp_path,
        script=ScriptedAnswer(
            content="<HistoricalDocument><Page><Body><Line>page text</Line></Body></Page></HistoricalDocument>",
            finish_reason="stop",
        ),
    )
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "page text"}})

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "page text"
    assert blob_store.has(response.response_sha256)


def test_both_live_retention_call_sites_declare_the_served_posture(tmp_path: Path):
    """The flag is only worth having if the live seam actually sets it.

    `feeding.retain_model_view` defaults `served` to False, which is what the
    fixture posture wants and what every offline call site relies on -- so a
    live call site that forgot to pass it would restore exactly the acceptance
    this guards against, silently and with every other test still green. Both
    live sites are pinned here, the whole page and the DAI record.
    """
    response, _, _ = _read_one(
        tmp_path,
        script=ScriptedAnswer(
            content="<HistoricalDocument><Page><Body><Line>page text</Line></Body></Page></HistoricalDocument>",
            finish_reason="stop",
        ),
    )
    page_adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "page text"}})
    live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "churro.v1", page_adapter, response
    )
    assert page_adapter.retained_served == [True]

    act_adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "page text"}})
    live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        act_adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )
    assert act_adapter.retained_served == [True]


def test_captured_page_attempt_cut_off_empty_is_failed_not_confirmed_blank(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="length"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank page" in attempt.reason
    assert attempt.health["truncated"] is True


def test_captured_page_attempt_unreported_empty_is_failed_not_confirmed_blank(tmp_path: Path):
    # An empty page response whose stop boundary was never reported is no more
    # a confirmed blank page than one the provider admits it cut off -- the
    # same guard applies whether the unknown is "cut off" or
    # "never said."
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason=ABSENT))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "not a confirmed blank page" in attempt.reason
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-recorded"


def test_captured_page_attempt_genuinely_empty_on_a_confirmed_blank_page(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": ""}})

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "genuinely-empty"


def test_captured_page_attempt_failed_on_a_malformed_wire_body_retains_raw_bytes(tmp_path: Path):
    response, _, blob_store = _read_one(tmp_path, script=ScriptedAnswer(body=b"not even json"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "never reached"}})

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert attempt.native_capture is None
    assert blob_store.has(response.raw_response_ref["sha256"])


def test_captured_page_attempt_refuses_an_unsupported_adapter_name(tmp_path: Path):
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content="x", finish_reason="stop"))
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": "x"}})

    with pytest.raises(SchemaRefusal):
        live_witness.captured_page_attempt(
            _Context(tree=_FakeTree()), 1, "attestator_1", "dai.v1", adapter, response
        )


def test_captured_page_attempt_real_churro_adapter_round_trip(tmp_path: Path):
    """One integration point through the real churro.v1 adapter, not a stub.

    The vendor's own `HistoricalDocument` grammar, read under the one parser
    name this chair has. The bytes ride along as `observation_payload` for both
    page-scoped adapters, because `captured_page_attempt` cannot know which of
    them derives geometry from them; Churro derives none, and says so through
    its registry entry rather than by being withheld here.
    """
    body = (
        "<HistoricalDocument><Page><Body><Line>real churro text</Line>"
        "</Body></Page></HistoricalDocument>"
    )
    response, _, blob_store = _read_one(
        tmp_path,
        script=ScriptedAnswer(content=body, finish_reason="stop"),
    )
    adapter = witness_adapters.resolve_runnable_adapter("churro.v1")

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_2", "churro.v1", adapter, response
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "real churro text"
    assert attempt.native_capture["adapter"] == "churro.v1"
    assert attempt.native_capture["parse"]["parser"] == "xml"
    assert attempt.native_capture["findings"] == []
    assert attempt.observation_payload == body.encode("utf-8")
    assert blob_store.has(response.response_sha256)


def test_a_churro_body_in_neither_declared_shape_is_retained_and_refused_by_name(tmp_path: Path):
    """A body nobody asked this chair for is a named surprise, not a failure.

    The parser ran, read the whole response and could name no shape it knows,
    and the outcome says *which* root element arrived rather than only that
    something did. The bytes are retained before the parse, so the refusal
    loses nothing.
    """
    response, _, blob_store = _read_one(
        tmp_path,
        script=ScriptedAnswer(
            content="<transcription><page>x</page></transcription>", finish_reason="stop"
        ),
    )
    adapter = witness_adapters.resolve_runnable_adapter("churro.v1")

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_2", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    parse = attempt.native_capture["parse"]
    assert parse["state"] == "unrecognized-shape"
    assert parse["parser"] == "xml"
    assert "transcription" in parse["outcome"]
    assert attempt.native_capture["stop_reason"] == "partial-parse-unrecognized-shape"
    assert "transcription" in attempt.reason
    assert blob_store.has(response.response_sha256)


def test_captured_page_attempt_real_chandra_adapter_reads_the_vendor_grammar(tmp_path: Path):
    """Chandra live: a body in the vendor's own layout grammar is a reading.

    The page text is the block texts joined by the shared delivered-text rule,
    and the bytes ride along as `observation_payload` so `run.py` can derive
    the page's block geometry from the very response the text came from.
    """
    body = (
        '<div data-bbox="100 77 900 385" data-label="Text">SYNTHETIC ACT ONE</div>\n'
        '<div data-bbox="100 462 900 846" data-label="Text">SYNTHETIC ACT TWO</div>'
    )
    response, _, blob_store = _read_one(
        tmp_path, script=ScriptedAnswer(content=body, finish_reason="stop")
    )
    adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_1", "chandra.v1", adapter, response
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == "SYNTHETIC ACT ONE\nSYNTHETIC ACT TWO"
    assert attempt.native_capture["parse"] == {
        "state": "parsed",
        "parser": "html",
        "text": "SYNTHETIC ACT ONE\nSYNTHETIC ACT TWO",
    }
    assert attempt.native_capture["view"] == {
        "prompt": adapter.prompt(),
        "generation": {"max_new_tokens": 12384},
    }
    # The vendor pin the prompt bytes came from travels with the reading, beside
    # the model identity provenance already requires.
    assert attempt.native_capture["vendor_identity"] == chandra.vendor_identity()
    # A clean page reports nothing the grammar could not resolve.
    assert attempt.native_capture["findings"] == []
    assert attempt.observation_payload == body.encode("utf-8")
    assert attempt.health["truncated"] is False
    assert attempt.raw_response_kind == "model-output"
    assert blob_store.has(response.response_sha256)


def test_captured_page_attempt_real_chandra_adapter_is_honest_about_an_unrecognized_shape(
    tmp_path: Path,
):
    """Chandra live: a body the layout grammar can place nothing in -- a real
    model's own markdown output, say -- lands as a named, honest failure with
    its bytes retained, never a fabricated reading."""
    response, _, blob_store = _read_one(
        tmp_path,
        script=ScriptedAnswer(
            content="## A markdown heading, and no layout block anywhere in it.",
            finish_reason="stop",
        ),
    )
    adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_3", "chandra.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "no-layout-blocks" in attempt.reason
    assert blob_store.has(response.response_sha256)
    # `unrecognized-shape` -- the parser ran, read the whole body, and could
    # place no shape it knows -- keeps the retained model view beside the blob
    # it describes rather than dropping it for want of a state name. The
    # outcome separates the two ways an answer yields no block:
    # `no-layout-blocks` is an answer with no `<div>` at all, and
    # `blocks-not-at-top-level` an answer that wrapped every one of them.
    assert attempt.native_capture["parse"] == {
        "state": "unrecognized-shape",
        "parser": "html",
        "outcome": "no-layout-blocks",
    }
    # The vendor pin is recorded whatever the answer turned out to be: it is a
    # fact about the request, not about whether the response parsed.
    assert attempt.native_capture["vendor_identity"] == chandra.vendor_identity()
    # Whether the shared contract accepts this capture is proven against a real
    # run tree in `test_attestatores_live_pass.py`, not here: this module's
    # `_FakeTree` addresses blobs by its own path scheme, which
    # `validate_native_capture`'s content-addressed check would refuse for
    # reasons that have nothing to do with the parse state.
    assert attempt.raw_response_kind == "model-output"


def test_captured_page_attempt_refuses_the_fixture_placeholder_schema_from_a_served_chair(
    tmp_path: Path,
):
    """A served chair's answer in the fixture's stand-in schema is a named
    surprise, not a reading.

    `fixture-chandra-response.v1` is the committed fixture's own placeholder,
    declared in `proof/skeleton_fixture.toml` and asked for by nothing:
    `chandra.prompt()` asks a served chair for the vendor's layout grammar and
    only that. Two things keep the two apart now. The live capture is written
    under the `html` parser, which reads the vendor grammar and can place
    nothing in a JSON object -- so the placeholder body lands as
    `no-layout-blocks`, a named surprise beside its retained bytes. And the
    retention seam refuses the placeholder parser outright for a served chair,
    so no route exists by which retained history could be read back as a live
    reading.

    The offline posture keeps the acceptance the fixture's pinned bytes depend
    on, through `parse_fixture_placeholder` -- `test_chandra_adapter.py` and
    `test_attestatores_retention.py` are that half.
    """
    body = f'{{"schema":"{CHANDRA_FIXTURE_SCHEMA}","markdown":"chandra text","blocks":[]}}'
    response, _, _ = _read_one(tmp_path, script=ScriptedAnswer(content=body, finish_reason="stop"))
    adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")

    tree = _FakeTree()
    attempt = live_witness.captured_page_attempt(
        _Context(tree=tree), 1, "attestator_3", "chandra.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert attempt.native_capture["parse"] == {
        "state": "unrecognized-shape",
        "parser": "html",
        "outcome": "no-layout-blocks",
    }
    # The bytes stay beside the record that could not read them: read the
    # referenced blob back rather than trusting that a reference exists,
    # so a retention that returns a plausible reference
    # without storing the body fails here.
    assert attempt.raw_response_ref
    assert tree.read_bytes(attempt.raw_response_ref["relative_path"]) == body.encode("utf-8")
    assert attempt.raw_response_ref["sha256"] == digest_bytes(body.encode("utf-8"))
    assert attempt.raw_response_kind == "model-output"
    # The same body still parses on the offline posture, where the fixture's
    # pinned bytes depend on it -- through the placeholder reader, which is the
    # only thing that reads it, and which a served chair can never reach.
    assert chandra.parse_fixture_placeholder(body.encode("utf-8")) == "chandra text"
    with pytest.raises(SchemaRefusal, match="placeholder parser"):
        chandra.retain(
            _Context(tree=tree),
            view={"prompt": chandra.prompt()},
            raw_response=body.encode("utf-8"),
            transport_stop_reason="stop",
            parser="json",
            served=True,
        )


# ===================== streamed under the witness loop guard =====================

WITNESS_GUARD = witness_loop_guard(shipped_decoding_policy()[0], "attestator_3")
# Synthetic rows, then one line over and over until the guard stops the reply.
_ROWS = [f"Tremblay, {name} f. {12 + index}" for index, name in enumerate(["Jean", "Marie"] * 5)]
_LOOPING = "\n".join(_ROWS + ["Tremblay, Jean f. 12"] * 80) + "\n"


def _read_streamed(tmp_path: Path, *, role: str, content: str, finish_reason: str = "length"):
    chair = dataclasses.replace(_identity(role=role), witness_adapter="churro.v1")
    client, endpoint, blob_store = _world(tmp_path, chair=chair)
    with client:
        endpoint.script(ScriptedAnswer(content=content, finish_reason=finish_reason))
        response = client.read(
            ChairRequest(
                kind="chat-completions",
                messages=({"role": "user", "content": "read the page"},),
                image_sha256s=(),
                generation_declared={},
                generation_sent={},
                loop_guard=WITNESS_GUARD,
            )
        )
    return response, endpoint


def test_a_looping_witness_reply_is_abandoned_and_kept_as_a_cut_off_reading(tmp_path: Path):
    response, endpoint = _read_streamed(tmp_path, role="attestator_2", content=_LOOPING)
    assert endpoint.streams_stopped == 1 and response.loop_stop is not None
    # Nothing after the loop line was read, and nothing is defaulted to a finish.
    assert response.content.count("Tremblay, Jean f. 12") == 31
    assert response.finish_reason is None
    assert live_witness.unmeasured_stop_reason(response, "the reply") is None
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": response.content}})

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "read"
    assert attempt.native_payload == response.content
    assert attempt.native_capture["transport_stop_reason"] == "repetition-loop"
    assert attempt.health["truncated"] is True
    assert attempt.health["truncation_basis"] == "trusted-response-boundary"


def test_a_looping_witness_reply_that_does_not_parse_names_the_loop(tmp_path: Path):
    response, _endpoint = _read_streamed(tmp_path, role="attestator_3", content=_LOOPING)
    adapter = _stub_adapter(
        retain_result={"parse": {"state": "failed", "reason": "unterminated output element"}}
    )

    attempt = live_witness.captured_page_attempt(
        _Context(tree=_FakeTree()), 1, "attestator_3", "churro.v1", adapter, response
    )

    assert attempt.outcome == "failed"
    assert "stopped the response on a repetition loop" in attempt.reason
    assert attempt.health["truncation_basis"] == (
        "response stopped by the client on a repetition loop ('repetition-loop'); "
        "unterminated output element"
    )


def test_a_churro_capture_stopped_on_a_loop_keeps_that_stop_over_the_post_hoc_scan():
    raw = _LOOPING.encode("utf-8")
    capture = native_witness.derive_churro_capture(
        raw, "repetition-loop", parser="xml", system_prompt=churro.prompt()["system"]
    )
    assert capture["parse"]["state"] == "parsed"
    # The post-hoc scan still records the repeated tail; the guard's stop is the reason.
    assert capture["findings"][-1]["kind"] == "post-hoc-repetition"
    assert capture["stop_reason"] == "repetition-loop"
    finished = native_witness.derive_churro_capture(
        raw, "stop", parser="xml", system_prompt=churro.prompt()["system"]
    )
    assert finished["stop_reason"] == "partial-post-hoc-repetition-detected"


def test_a_witness_reply_with_no_loop_is_read_as_before(tmp_path: Path):
    content = "\n".join(_ROWS) + "\n"
    response, endpoint = _read_streamed(
        tmp_path, role="attestator_2", content=content, finish_reason="stop"
    )
    assert endpoint.streams_stopped == 0
    assert (response.content, response.finish_reason, response.loop_stop) == (content, "stop", None)
    adapter = _stub_adapter(retain_result={"parse": {"state": "parsed", "text": content}})

    attempt = live_witness.live_attempt_from_response(
        _Context(tree=_FakeTree()),
        adapter,
        "dai.v1",
        response,
        generation_declared={},
        parser="text",
        **_dai_view_kwargs(),
    )

    assert attempt.outcome == "read"
    assert attempt.native_capture["transport_stop_reason"] == "stop"
    assert attempt.health["truncated"] is False


def test_the_page_builder_carries_a_guard_and_refuses_one_for_chandra():
    row = _sealed_churro_rows()[0]
    context = _Context(tree=_FakeTree())
    image_bytes = _png(50, 70)
    presentation = _presentation(kind="page", image_bytes=image_bytes)
    context.tree.seed(presentation["image_path"], image_bytes)
    adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=churro.prompt)
    request = live_witness.page_chair_request(
        context, adapter, "churro.v1", presentation, profile=row, loop_guard=WITNESS_GUARD
    )
    assert dict(request.loop_guard) == WITNESS_GUARD

    chandra_adapter = SimpleNamespace(present=lambda ctx, pres: pres, prompt=chandra.prompt)
    with pytest.raises(SchemaRefusal, match="never streamed"):
        live_witness.page_chair_request(
            context,
            chandra_adapter,
            "chandra.v1",
            presentation,
            profile=dataclasses.replace(row, chair="attestator_1"),
            loop_guard=WITNESS_GUARD,
        )
