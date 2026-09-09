"""Fixture-only witness feeding contracts for R3.

These adapters describe exactly what a real witness call must receive and retain,
but deliberately make no model call.  They are small enough to exercise against
fixtures while preserving the operational boundary: a response is already
complete when it reaches this module.  In particular, repetition is inspected
*after* capture; it cannot affect generation or alter the captured bytes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from contextlib import contextmanager
from itertools import groupby
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Final, Iterator, Mapping

from common import chandra_layout
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ATTESTATORES, EXEMPLAR
from common.native_witness import (
    CHURRO_OUTPUT_TOKENS,
    churro_capture_system_prompt,
    derive_churro_capture,
    # The scan under its own chair-neutral name. It was imported here through
    # `detect_churro_repetition` while that was the only name it had; two page
    # chairs now call it, and importing the chair-named alias under the
    # chair-neutral name would say the Chandra branch borrowed Churro's.
    detect_repetition,
    parse_churro_response,
)
from common.request_capacity import DECLARED_ANSWER_BOUND_TOKENS

DAI_MAX_WIDTH_PX = 1_500
# `DAI_MAX_HEIGHT_PX` (4096, "no model source") retired with the vendor
# systems design: it read off nothing the model itself states. The
# total-pixel ceiling did **not** get to retire the same way, and `v4`
# restores one -- a hostile review (U11) demonstrated the `v3` claim below
# false on the unit's own kept test case: a DAI crop under 1,500px wide can
# still carry more total pixels than a served row admits (a tall, narrow
# marginal-note or signature-column crop, not merely an adversarial input),
# and when it does, vLLM's own Qwen2.5-VL image processor (`smart_resize`,
# `common/request_capacity.py`) resizes it *again* inside the engine before
# the model sees it -- a second resize this schema had no field for and the
# Testimonium's `identity`/`resize-preserve-aspect` claim then contradicted.
#
# `DAI_MAX_TOTAL_PIXELS` is `min(max_pixels)` over every DAI (`attestator_2`)
# row in the shipped real catalogue (`config/serving_recipes_real.toml`) --
# 2,359,296 as of U15 (Tyrel's ruling, 2026-09-06: the per-tier pixel ladder
# is retired, so every tier now ships the same DAI `max_pixels`, its own
# trained-crop ceiling rather than a generic-24gb leftover) -- pinned against
# that file by
# `test_feeding.py::test_dai_total_pixel_ceiling_is_the_smallest_shipped_rows_max_pixels`
# so a future tier change cannot leave this stale. It is the *smallest*
# across tiers, not the tier this run actually serves under, on purpose:
# `dai_dimensions` is a pure function of the crop's own pixels, read back
# unchanged by `witness_adapters._dai_present` (which publishes the model
# bytes before any chair answers) and by `validate_adapter_presentation` /
# `publish_attempt` (which re-derive the same crop from the sealed regions
# alone, with no served row in hand, to prove a Testimonium was not forged).
# Threading the live row into that function would make it depend on state
# none of those three callers has, which is a bigger seam than one hostile
# finding earns; a fixed, sourced, worst-tier ceiling keeps `dai_dimensions`
# pure and still guarantees no row's engine ever needs a second resize. The
# cost, named rather than hidden (GOVERNANCE 10): a crop close to the width
# ceiling served on a larger tier is cut down to what the *smallest* tier
# would need, even where its own row could have held more. That is a
# resolution cost on a minority of wide, tall crops, not a correctness gap.
#
# Both ceilings are read off something. The width ceiling is the model card
# itself (`https://huggingface.co/Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`,
# revision `e371095d4ffe585f31f4974462931ddbac61ff64`, Training/Parameters:
# "Image width: 1500 pixels (max)") rather than "design v2.1 section 2", which
# named the same number but not the model's own source for it. The total-pixel
# ceiling is the shipped serving catalogue itself, named above. A number
# nobody can trace is exactly what GOVERNANCE 10 refuses, so both ceilings
# carry their provenance into the record they seal.
DAI_MAX_TOTAL_PIXELS = 2_359_296
DAI_LIMIT_SOURCES = {
    "max_width_px": (
        "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR model card, "
        "Training/Parameters: 'Image width: 1500 pixels (max)' "
        "(https://huggingface.co/Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR "
        "@ e371095d4ffe585f31f4974462931ddbac61ff64)"
    ),
    "max_total_pixels": (
        "config/serving_recipes_real.toml: the smallest max_pixels shipped for "
        "the dai.v1 (attestator_2) row across every tier -- 2,359,296, the same "
        "at every tier since U15 (Tyrel's ruling, 2026-09-06) retired the "
        "per-tier pixel ladder -- the floor every deployed tier's engine "
        "actually admits, so a client-side crop within it is never re-resized "
        "by any of them"
    ),
}
SCHEDULING_POLICY = "chair-outer-act-inner.stage-major-parish.v1"
# Where an act with no page ordinal sorts: before every placed act, and named
# rather than spelled -1 at the two places that have to agree on it.
_UNPLACED_ORDINAL = -1
# The (adapter, parser) pairs `retain_model_view` can actually carry to a state.
_RUNNABLE_PARSERS = frozenset(
    {
        # Chandra's two postures, the same way Churro's two are named. `html` is
        # the live path's: the vendor's own layout grammar
        # (`common/chandra_layout.py`), which is what a served chair is asked for
        # and the only shape a live reading is ever taken from. `json` is the
        # committed fixture's placeholder alone -- `fixture-chandra-response.v1`,
        # a shape this repository invented for a fixture that asks nothing of
        # anybody -- kept because the fixture's declared bytes are pinned into
        # its own digests until U16 re-declares those rows in the vendor grammar.
        # `retain_model_view` refuses `json` for a *served* chair, so retained
        # history cannot become a live reading by taking the wrong branch.
        ("chandra.v1", "html"),
        ("chandra.v1", "json"),
        # Churro has one grammar and therefore one parser name, in both
        # postures: `xml`, the vendor's own `HistoricalDocument`
        # (`common/churro_document.py`). Unit 12 carried a second name for a
        # live dispatcher that also read this repository's own JSON coordinate
        # contract; that contract is retired with the channel it invented
        # (`churro.py` says why), so a fixture body and a served answer are read
        # by the same parser under the same name and re-derive through the same
        # branch.
        ("churro.v1", "xml"),
        ("dai.v1", "text"),
    }
)
# `[UNCERTAIN]` and `[CROSSED_OUT]` are not this repository's invention: they
# are the expert transcription convention defined on the training dataset's
# own card, `Teklia/DAI-CReTDHI-RecordGold-ATR`
# (https://huggingface.co/datasets/Teklia/DAI-CReTDHI-RecordGold-ATR), which
# declares `license: mit`. Its sibling training corpus,
# `Teklia/DAI-CReTDHI-RecordGeneanet-ATR`, declares no licence at all; nothing
# from that card is carried here; it is named so a later reader does not read
# one dataset's MIT term as covering both. These two literal strings are the
# whole of what crosses from the Gold card into this module: `validate_dai_text`
# preserves them unchanged, and their presence in the grammar is the reason
# `DAI_FORMAT_CAPABILITIES` below can name a doubt at all.
_UNCERTAINTY_TOKENS = ("[UNCERTAIN]", "[CROSSED_OUT]")

#: What DAI's grammar can carry, declared from the grammar rather than assumed
#: from a blanket default (vendor systems design, Contract boundary: "DAI
#: true/false"). Its answer is plain UTF-8 text carrying the RecordGold card's
#: own `[UNCERTAIN]`/`[CROSSED_OUT]` convention (`_UNCERTAINTY_TOKENS` above),
#: so the grammar *can* carry a doubt; it has no coordinate vocabulary
#: anywhere, so `can_express_layout` is false and stays false.
#:
#: `can_express_uncertainty` is **true, and only because the comparison view it
#: needs is already wired**. A chair that declares uncertainty before the
#: Perlector can compare one against a bracket-marker view is permanently
#: `compared: unknown` under `dissent.is_comparable` -- the judges' second fatal
#: flaw. U6 landed the view (`common/alignment.py::bracket_marker_view`), U12
#: wired it at `pipeline/4_perlector/run.py::dissent_testimonia` for exactly the
#: act-scoped capability-declaring chairs, and the flip is this integration's,
#: after both. The order is the whole of the safety here: flipped earlier, this
#: chair would have gone dark on the one axis ARCHITECTURE names for catching a
#: reader that learned to agree with witnesses.
DAI_FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": True, "can_express_layout": False}
)


#: The vendor systems ruling retired three things at this seam, and they are
#: named here because their absence is the point rather than an oversight:
#: `churro_prompt` (the model-agnostic diplomatic-transcription prompt this
#: repository carried and described as "the trained two-message XML framing"
#: -- it is the Churro library's *generic fallback*, a drifted copy of the
#: paper's zero-shot comparison-VLM prompt, and the fine-tune never saw it),
#: `churro_layout_prompt` (a modified carry of that prompt asking for a JSON
#: coordinate channel Churro-DS carries no geometry for), and the two
#: `CHURRO_*_PROMPT_VERSION` names for them. What a Churro chair is asked is
#: now one of the two vendor-attested system strings in
#: `common/churro_document.py`, resolved by name through
#: `pipeline/3_attestatores/churro.py::FRAMINGS`. This module owns no Churro
#: prompt bytes at all any more, which is why there is nothing between the
#: parser table above and the generation view below.


def churro_generation() -> dict[str, int]:
    """Churro's declared answer bound, retained as evidence and not as the wire value.

    20,000 tokens, from the three vendor artifacts
    ``common/native_witness.py::CHURRO_OUTPUT_TOKENS`` names and reads out of
    ``request_capacity.DECLARED_ANSWER_BOUND_TOKENS`` -- the one table the wire
    value is computed from, so this chair's bound cannot drift from the number
    the bound seam applies. It is *declared* evidence: what actually goes out is
    ``min(this, max_model_len - image - prompt)`` through
    ``live_witness.generation_bound_sent``, and at every row in the shipped real
    catalogue the row is what binds, so no ``max_tokens`` is sent at all.

    The vendor's ``repetition_penalty`` of 1.05 is deliberately **not** folded in
    here. This is the view retained inside every Churro model view, written
    through ``common/contracts/canonical.py``, which refuses floats outright; a
    declared view that quietly re-encoded 1.05 would be worse than one that does
    not claim to carry it. It goes on the wire through
    :func:`churro_wire_decoding`, where the retained chair-call record
    transcribes it exactly.
    """
    return {"max_new_tokens": CHURRO_OUTPUT_TOKENS}


def chandra_generation() -> dict[str, int]:
    """Chandra's own declared answer bound, retained as the vendor's own number.

    ``chandra/settings.py``'s ``MAX_OUTPUT_TOKENS = 12384`` at the pinned commit
    -- the value every vendor caller passes as its generation limit. It is
    *declared* evidence, not by itself what goes on the wire: as for Churro,
    ``common/request_capacity.py::sendable_max_tokens`` sends
    ``min(this, max_model_len - image - prompt)`` against this request's own
    capacity record, and the vendor's own pair overruns the vendor's own
    container (12,384 + a 6,045-token A4 image against
    ``--max-model-len 18000``), so the clamp is ours and the record says which
    of the two bound a given call.

    This module states it once by reading
    ``request_capacity.DECLARED_ANSWER_BOUND_TOKENS``, the one table the wire
    value is computed from, exactly as ``CHURRO_OUTPUT_TOKENS`` does -- a second
    literal here could drift from the number the bound seam actually applies.
    """
    return {"max_new_tokens": DECLARED_ANSWER_BOUND_TOKENS["attestator_1"]}


def churro_wire_decoding() -> dict[str, float]:
    """Churro's own shipped ``repetition_penalty``, because the engine drops it.

    Carried third-party content: one value from ``generation_config.json`` (260
    source bytes, SHA-256
    ``90e92cbc8634d6f5b1cb1ae58a3c48724a1ce1f11f8b7aecb5b9b3fd5d5a06bf``) in
    ``stanford-oval/churro-3B`` at the revision ``config/models-real.toml``
    pins. Only this one value crosses; the file's ``do_sample``/``temperature``
    describe a sampling posture `config/decoding.toml` owns and the serving
    seam already fixes at 0, and its ``bos``/``eos``/``pad`` ids are the
    tokenizer's own, which vLLM reads for itself.

    **Why it has to be sent.** Every serving row pins
    ``generation_config = "vllm"``, and vLLM's ``get_diff_sampling_param``
    then returns ``{}`` instead of the model's file, so the request falls back
    to ``_DEFAULT_SAMPLING_PARAMS`` -- ``repetition_penalty 1.0``. The model's
    publisher ships 1.05 and the CHURRO paper section D.5 documents this model
    entering degeneration loops. Declining a vendor's own mitigation by
    accident is exactly what GOVERNANCE 7 forbids in the other direction: the
    pipeline does not gate model behaviour, and it does not silently substitute
    its own value for the vendor's either.

    **Determinism is untouched.** At ``temperature = 0`` vLLM takes the greedy
    path, and the penalty is applied to the logits *before* that argmax; the
    same request still returns the same tokens.

    Not folded into :func:`churro_generation`, which is the *declared* view
    retained inside every Churro model view: that record is written by
    ``common/contracts/canonical.py``, which refuses floats outright, and a
    declared view that quietly re-encoded 1.05 would be worse than one that
    does not claim to carry it. What is sent is recorded, exactly, on the
    retained chair-call record -- ``operations/serving/client.py`` transcribes
    it as ``wire-decimal.v1``, the machinery that exists for DAI's identical
    1.05.
    """
    return {"repetition_penalty": 1.05}


# The stop token vLLM already holds for DAI without being told: the tokenizer's
# own ``eos_token``, ``<|im_end|>``, at the pinned revision. Named so
# :func:`dai_wire_stop_token_ids` can say which of the carried ids is the *new*
# one rather than re-typing a literal for it.
DAI_TOKENIZER_EOS_TOKEN_ID: Final = 151645


def dai_wire_stop_token_ids() -> dict[str, list[int]]:
    """DAI's second EOS id, which ``generation_config = "vllm"`` never reads.

    Derived from the carried ``generation_config.json`` (:func:`dai_generation`,
    under its own digest), never re-typed: its ``eos_token_id`` is
    ``[151645, 151643]``, and vLLM takes only the first from the tokenizer's
    ``eos_token``. The second, ``<|endoftext|>``, is dropped with the rest of
    the model's file when the row pins ``generation_config = "vllm"``, so a
    response that ends on it would not stop -- and with the answer budget then
    running to the row's context, that is length billed by the hour rather than
    a reading.

    Only the ids vLLM does not already have are sent. Adding the primary EOS
    back would be a no-op in principle and a change to the one stop that is
    already working in practice, which is not a trade this seam makes on an
    unobserved engine.
    """

    declared = dai_generation()["eos_token_id"]
    extra = [token_id for token_id in declared if token_id != DAI_TOKENIZER_EOS_TOKEN_ID]
    if not extra:
        raise SchemaRefusal(
            "DAI's carried generation config names no stop token beyond the tokenizer's own "
            f"{DAI_TOKENIZER_EOS_TOKEN_ID}, so this seam has nothing to add; the carried ids "
            f"are {declared!r} and the carry itself has changed"
        )
    return {"stop_token_ids": extra}


def dai_prompt() -> dict[str, str]:
    """Return DAI's two carried prompt files byte-for-byte as UTF-8 text.

    Carried third-party content: ``system.txt`` (206 bytes, SHA-256
    ``b4e7d61d4f27f0aa46ba597ebfac3925b3ed87e72583def4bce2bd4f0393c333``)
    and ``query.txt`` (33 bytes, SHA-256
    ``3a5cd8eb3263f2511d207f49f9933b1cf184e95fd7a9534871207d8d8b6a3489``)
    from Teklia's pinned
    ``Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`` repository at
    ``e371095d4ffe585f31f4974462931ddbac61ff64``:
    https://huggingface.co/Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR/tree/e371095d4ffe585f31f4974462931ddbac61ff64.
    The source declares no licence; its research-track use is Tyrel's settled
    2026-08-20 ruling. These are named carries, not reconstructed instructions:
    changing any character changes the trained request framing.
    """
    return {
        "system": (
            "Tu es un assistant archiviste. Tu dois lire des actes issus de registres "
            "paroissiaux français, du 16è au 18è siècle. Extrais le texte de la marge, du "
            "corps de l'acte, et éventuellement les signatures.\n"
        ),
        "user": "Extrais le texte de ce document.\n",
    }


def dai_generation() -> dict[str, Any]:
    """Return DAI's carried ``generation_config.json`` without changing its values.

    Carried third-party content: every value in ``generation_config.json`` (243
    source bytes, SHA-256
    ``f4cd2d54597a1a3cb38ac78d5cb275d06f6fd660fef52ee444a58d81297ff027``),
    from the same pinned Teklia source and under the same no-licence/ruling
    citation as :func:`dai_prompt`. What crosses is the nine values, re-typed as
    a Python mapping; the source file's bytes are its JSON framing, which this
    function does not return, so this is a source-file digest rather than a
    byte-count claim about the mapping. This is the shipped generation
    configuration, not a locally chosen decoding policy; in particular,
    ``do_sample`` remains true.
    """
    return {
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


def validate_dai_text(raw: bytes) -> str:
    """Decode DAI's text response exactly; uncertainty markers are not normalized.

    ``[UNCERTAIN]`` and ``[CROSSED_OUT]`` are ordinary retained response text.
    This parser does no whitespace, Unicode, or token rewriting, so both the
    native payload and the DAI model view preserve them unaltered.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise SchemaRefusal("DAI response is not raw bytes")
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise SchemaRefusal(f"DAI response is not UTF-8 text: {error}") from error


def dai_model_view(
    *,
    source_image_ref: dict[str, str],
    model_image_ref: dict[str, str],
    width_px: int,
    height_px: int,
    system_prompt_ref: dict[str, str],
    query_prompt_ref: dict[str, str],
    generation_config_ref: dict[str, str],
) -> dict[str, Any]:
    """Build DAI's crop view, referencing carried prompt/config bytes by manifest.

    **The identity transform is a claim about bytes, not about paths.** When no
    resize is needed the model must have been shown exactly the source image,
    and the two references are therefore required to name the same content —
    the same SHA-256, which is what "the same retained blob" means in a
    content-addressed store. They are deliberately not required to be the same
    *reference dict*: the source is the Designator's own proposal crop under
    `2_designator/`, and every blob this stage shows a witness is published
    into `3_attestatores/blobs/sha256/`, so a byte-identical image legitimately
    appears at two stage-owned paths. `verify_exemplar_crop_lineage` already
    proves a proposal crop is exactly `crop_png(sealed page, bounds)`, and
    `_dai_present` re-derives the same crop from the same sealed page on that
    path, so equal digests here are equal pixels and not a coincidence. Held to
    the whole dict instead, this rule refused every genuine no-resize DAI act
    after its response had already come back — the Attestatores HANDOFF's
    second owed gap. Refusing on digest keeps the invariant that mattered (the
    model saw the source bytes) and drops only the one that never did (both
    stages spell the same bytes' location the same way).
    """
    for name, reference in (
        ("source image", source_image_ref),
        ("model image", model_image_ref),
        ("system prompt", system_prompt_ref),
        ("query prompt", query_prompt_ref),
        ("generation config", generation_config_ref),
    ):
        _reference(reference, name)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (width_px, height_px)
    ):
        raise SchemaRefusal("DAI input dimensions must be positive integers")
    resized_width, resized_height = dai_dimensions(width_px, height_px)
    resized = (resized_width, resized_height) != (width_px, height_px)
    if resized and source_image_ref["sha256"] == model_image_ref["sha256"]:
        raise SchemaRefusal("DAI resized model image is not distinct from its source bytes")
    if not resized and source_image_ref["sha256"] != model_image_ref["sha256"]:
        raise SchemaRefusal("DAI identity transform does not retain the source image bytes exactly")
    limits = _dai_image_limits()
    view = {
        "adapter": "dai-atr.v1",
        "source_image_ref": source_image_ref,
        "model_image_ref": model_image_ref,
        "transform": {
            "kind": "resize-preserve-aspect" if resized else "identity",
            "resampler": "pillow-lanczos" if resized else None,
            "dimension_rounding": "floor" if resized else None,
            "source_width_px": width_px,
            "source_height_px": height_px,
            "target_width_px": resized_width,
            "target_height_px": resized_height,
        },
        "image_limits": limits,
        "image_limits_sha256": digest_of(limits),
        "prompts": {"system": system_prompt_ref, "query": query_prompt_ref},
        "generation_config_ref": generation_config_ref,
        "uncertainty_tokens_preserved": list(_UNCERTAINTY_TOKENS),
    }
    return validate_dai_model_view(view)


def _dai_image_limits() -> dict[str, Any]:
    """The sealed statement of DAI's executable image ceilings.

    ``v3`` dropped the height ceiling (no model source) and the total-pixel
    ceiling together, on the claim that the served row's own ``max_pixels``
    made the second redundant. ``v4`` restores the total-pixel ceiling alone
    (``feeding.DAI_MAX_WIDTH_PX``'s own comment names the hostile-review
    finding that claim did not survive) -- the height ceiling stays retired,
    since nothing states one and a total-pixel ceiling already bounds height
    indirectly for any width this rule can produce.
    """
    return {
        "schema": "dai-image-limits.v4",
        "max_width_px": DAI_MAX_WIDTH_PX,
        "max_total_pixels": DAI_MAX_TOTAL_PIXELS,
        "sources": dict(DAI_LIMIT_SOURCES),
    }


def validate_dai_model_view(value: Any) -> dict[str, Any]:
    """Close a DAI view before request retention trusts its references or digest."""
    fields = {
        "adapter",
        "source_image_ref",
        "model_image_ref",
        "transform",
        "image_limits",
        "image_limits_sha256",
        "prompts",
        "generation_config_ref",
        "uncertainty_tokens_preserved",
    }
    if not isinstance(value, dict) or set(value) != fields or value["adapter"] != "dai-atr.v1":
        raise SchemaRefusal("DAI model view is not its closed adapter schema")

    for name, reference in (
        ("source image", value["source_image_ref"]),
        ("model image", value["model_image_ref"]),
        ("generation config", value["generation_config_ref"]),
    ):
        _reference(reference, name)
    prompts = value["prompts"]
    if not isinstance(prompts, dict) or set(prompts) != {"system", "query"}:
        raise SchemaRefusal("DAI model view does not bind its two prompt references")
    _reference(prompts["system"], "system prompt")
    _reference(prompts["query"], "query prompt")

    transform = value["transform"]
    transform_fields = {
        "kind",
        "resampler",
        "dimension_rounding",
        "source_width_px",
        "source_height_px",
        "target_width_px",
        "target_height_px",
    }
    if not isinstance(transform, dict) or set(transform) != transform_fields:
        raise SchemaRefusal("DAI model view has no closed executable transform")
    dimensions_px = tuple(
        transform[field]
        for field in (
            "source_width_px",
            "source_height_px",
            "target_width_px",
            "target_height_px",
        )
    )
    if not all(
        isinstance(dimension, int) and not isinstance(dimension, bool) and dimension > 0
        for dimension in dimensions_px
    ):
        raise SchemaRefusal("DAI model view transform dimensions must be positive integers")
    source_width, source_height, target_width, target_height = dimensions_px
    expected_target = dai_dimensions(source_width, source_height)
    resized = expected_target != (source_width, source_height)
    expected_transform_facts = (
        ("resize-preserve-aspect", "pillow-lanczos", "floor")
        if resized
        else ("identity", None, None)
    )
    if (target_width, target_height) != expected_target or (
        transform["kind"],
        transform["resampler"],
        transform["dimension_rounding"],
    ) != expected_transform_facts:
        raise SchemaRefusal("DAI model view transform differs from its sealed image ceilings")
    source_ref = value["source_image_ref"]
    model_ref = value["model_image_ref"]
    if resized and source_ref["sha256"] == model_ref["sha256"]:
        raise SchemaRefusal("DAI resized model image is not distinct from its source bytes")
    if not resized and source_ref["sha256"] != model_ref["sha256"]:
        raise SchemaRefusal("DAI identity transform does not retain the source image bytes exactly")

    limits = value["image_limits"]
    expected_limits = _dai_image_limits()
    if limits != expected_limits:
        raise SchemaRefusal("DAI model view image limits differ from the sealed executable limits")
    limits_digest = value["image_limits_sha256"]
    if (
        not isinstance(limits_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", limits_digest)
        or limits_digest != digest_of(limits)
    ):
        raise SchemaRefusal("DAI model view image-limits digest does not match its limits")
    if value["uncertainty_tokens_preserved"] != list(_UNCERTAINTY_TOKENS):
        raise SchemaRefusal("DAI model view does not preserve the declared uncertainty tokens")
    return value


def dai_dimensions(width_px: int, height_px: int) -> tuple[int, int]:
    """Largest aspect-preserving view within DAI's two sealed ceilings.

    Two floor-rounded, aspect-preserving passes, applied in order: first the
    width ceiling (`DAI_MAX_WIDTH_PX`), same as `v3`; then, only if the
    resulting crop still carries more total pixels than `DAI_MAX_TOTAL_PIXELS`,
    a second aspect-preserving scale-down against that ceiling. A width already
    under 1,500px skips the first pass entirely, but a crop that is narrow and
    *tall* can still carry more total pixels than any shipped row admits --
    `v3` let exactly that case through as a claimed identity view, and a
    hostile review (U11) demonstrated it against the unit's own kept test case
    (a 500x10,000 crop, 5,000,000px, against a smallest shipped row of
    1,806,336). This is why the ceiling is a second pass rather than folded
    into one search the way `v2`'s used to run: the two ceilings come from
    different places (the model's own trained width; the serving catalogue's
    smallest admitted pixel count) and a crop can trip either alone.

    Nothing here reaches for the row that will actually serve this request --
    see `DAI_MAX_TOTAL_PIXELS`'s own comment for why this function stays a
    pure function of the crop's own pixels. The guarantee it keeps is still
    real: a view within `DAI_MAX_TOTAL_PIXELS` fits every shipped tier's own
    `max_pixels`, so none of their engines needs to resize it again.

    Public because it decides which pixels a DAI witness is actually shown, and
    the presentation writer and the read-back validator in `witness_adapters`
    both have to reach exactly this rule. Under a private name, renaming it here
    would break those two together with nothing to say they were coupled.
    """
    if (
        not isinstance(width_px, int)
        or isinstance(width_px, bool)
        or not isinstance(height_px, int)
        or isinstance(height_px, bool)
        or width_px < 1
        or height_px < 1
    ):
        raise SchemaRefusal("DAI source dimensions must be positive integers")
    if width_px <= DAI_MAX_WIDTH_PX:
        target_width, target_height = width_px, height_px
    else:
        target_width = DAI_MAX_WIDTH_PX
        target_height = max(1, height_px * target_width // width_px)
    if target_width * target_height > DAI_MAX_TOTAL_PIXELS:
        beta = math.sqrt((target_width * target_height) / DAI_MAX_TOTAL_PIXELS)
        target_width = max(1, math.floor(target_width / beta))
        target_height = max(1, math.floor(target_height / beta))
    return target_width, target_height


def sealed_page_bytes(context: Any, page_id: str, *, what: str) -> bytes:
    """The sealed Exemplar page's exact bytes, read once and digest-bound.

    All three adapters cut their own presented image out of a sealed page --
    DAI's act crop, Chandra's `scale_to_fit` and Churro's `prepare_ocr_image` --
    and each must read it the same way: through `read_artifact`, which verifies
    the record's inputs, and then against the *one* byte object the imaging call
    will use, so a filesystem swap cannot cross the interval between the check
    and the use. Written once here rather than three times, because the copies
    would agree only for as long as all of them were edited together.

    ``what`` names the adapter in every refusal, so an operator reading one is
    sent to the chair whose presentation could not be built rather than to
    whichever adapter happened to own the shared code.
    """

    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    payload = page.get("payload")
    image_path = payload.get("image_path") if isinstance(payload, dict) else None
    if not isinstance(image_path, str) or not image_path:
        raise SchemaRefusal(f"{what}'s sealed source page has no image path to crop")
    try:
        page_bytes = context.tree.read_bytes(image_path)
    except OSError as error:
        raise SchemaRefusal(f"{what} sealed page bytes could not be read: {error}") from error
    if digest_bytes(page_bytes) != payload.get("source_sha256"):
        raise SchemaRefusal(
            f"{what} sealed page bytes changed between artifact verification and crop use"
        )
    return page_bytes


def _record_post_hoc_repetition(
    record: dict[str, Any], raw_response: bytes, *, ceiling: int
) -> None:
    """Scan a retained capture for a repeated tail and record what it found.

    The vendor's own answer to a degenerate reading is a retry ladder --
    Chandra's ``_should_retry`` re-rolls the same page up the temperature
    ladder until the answer stops looking stuck. That is not carried: re-rolling
    a reading until it looks better is recovering *quality*, which GOVERNANCE 11
    reserves to a review flag and refuses to a recovery loop. What is carried is
    the fact. The scan runs after the bytes are already retained and already
    parsed, it changes nothing about the response, and it publishes a finding
    beside the reading plus a stop reason that says the reading is partial --
    without which a Chandra answer that degenerated but still ended under its
    bound would reach the Perlector as full testimony under
    ``transport_stop_reason = "stop"`` (GOVERNANCE 2).

    Written here rather than inside ``derive_churro_capture`` because that
    function derives *Churro's* whole capture -- its grammar, its byte ceiling,
    its parse states -- while this is the one chair-neutral half of it. The
    detector itself is already chair-neutral
    (``common/native_witness.py::detect_repetition``); this is the seam that
    applies it to a capture some other chair's derivation did not build.

    Three rules, each the same as Churro's, so two page witnesses cannot come to
    mean different things by one finding:

    * **What is inspected.** ``parse["text"]`` where a parse produced one, and
      the raw bytes otherwise, with the choice named in the finding's
      ``inspected`` field. Repetition is a fact about what the model
      transcribed, not about the markup it arrived in -- a page of `<div
      data-bbox=...>` wrappers repeats by construction.
    * **The ceiling.** A body past the grammar's own retained parsing limit was
      never read by the parser, and normalizing it here to count a tail would
      spend the memory the ceiling exists to refuse. The scan says it did not
      run rather than running on bytes nobody bounded.
    * **Precedence.** A parse outcome wins over a repeated tail: a body this
      grammar could not place is the more load-bearing fact about the capture,
      and the repetition stays in ``findings`` either way, so nothing is lost by
      the ordering.
    """

    if len(raw_response) > ceiling:
        finding: dict[str, Any] | None = {
            "kind": "post-hoc-repetition-uninspected",
            "reason": (
                f"response exceeds the retained parsing limit of {ceiling} bytes "
                f"(received {len(raw_response)})"
            ),
        }
        basis = "raw-response"
    else:
        parsed_text = record["parse"].get("text")
        inspected: str | bytes
        inspected, basis = (
            (parsed_text, "parsed-text")
            if isinstance(parsed_text, str)
            else (raw_response, "raw-response")
        )
        finding = detect_repetition(inspected)
    if finding is None:
        return
    record["findings"].append({**finding, "inspected": basis})
    if finding["kind"] == "post-hoc-repetition" and record["parse"]["state"] not in {
        "failed",
        "unrecognized-shape",
    }:
        record["stop_reason"] = "partial-post-hoc-repetition-detected"


def retain_model_view(
    tree: Any,
    *,
    adapter: str,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain a reproducible view and raw response, including parser failure bytes.

    ``served`` says the bytes came off a chair that actually answered rather
    than out of the committed fixture. It decides exactly one thing -- whether
    Chandra's fixture-placeholder parser may run at all (CodeRabbit round 1,
    T7) -- and changes nothing else here. Retention itself is posture-blind,
    and stays so: the bytes are published to the tree before any parser runs,
    so a refusal names a surprise without losing it.
    """
    if not isinstance(adapter, str) or not adapter:
        raise SchemaRefusal("model-view adapter is blank")
    if not isinstance(raw_response, bytes):
        raise SchemaRefusal("model-view raw response is not bytes")
    if not isinstance(transport_stop_reason, str) or not transport_stop_reason:
        raise SchemaRefusal("model-view transport stop reason is blank")
    # A parser this boundary cannot run would leave `parse.state` at "pending"
    # forever: a finished attempt wearing the look of one still in progress, which
    # is the shape GOVERNANCE 2 refuses. Ask for a parse that runs, or ask for none.
    if parser is not None and (adapter, parser) not in _RUNNABLE_PARSERS:
        raise SchemaRefusal(f"model-view parser {parser!r} does not run for adapter {adapter!r}")
    # A served chair answering in the committed fixture's own placeholder schema
    # is answering a question nobody put to it, and reading that as a page of
    # text would publish a reading whose shape this repository never verified
    # against anything (GOVERNANCE 10). The refusal is at the seam rather than
    # inside the parser because the parser name is what the record will carry:
    # a live capture written under `json` could never be re-derived as the live
    # grammar it was actually asked in. The bytes are retained by the caller's
    # own route either way, so nothing is lost by refusing here.
    if served and adapter == "chandra.v1" and parser == "json":
        raise SchemaRefusal(
            "a served chandra.v1 response cannot be retained under the committed fixture's "
            "placeholder parser 'json'; a live reading is taken only in the vendor layout "
            "grammar ('html')"
        )
    if adapter == "dai.v1":
        validate_dai_model_view(view)
    raw_digest, published = tree.put_blob(ATTESTATORES, raw_response)
    record: dict[str, Any] = {
        "schema": "attestatores-model-view.v1",
        "adapter": adapter,
        "view": view,
        "raw_response_ref": {"relative_path": published.relative_path, "sha256": raw_digest},
        "transport_stop_reason": transport_stop_reason,
        # What the transport said, until this boundary finds a reason to say
        # something more honest. Stated once here rather than reassigned
        # identically down each branch that finds nothing.
        "stop_reason": transport_stop_reason,
        "findings": [],
        "parse": {"state": "not-requested" if parser is None else "pending", "parser": parser},
    }
    if adapter == "churro.v1":
        # Which vendor pin the prompt bytes beside this reading were taken from,
        # recorded whatever the answer turned out to be: the pin is a fact about
        # the request, not about whether the response parsed (GOVERNANCE 6).
        # Imported locally for the reason Chandra's is: the runnable sibling
        # module imports this retention seam.
        import churro

        # The system string comes off the view being retained, so the vendor's
        # own prompt-echo trim runs against the framing this request actually
        # sent -- and `verify_native_capture_bytes` reads the same string back
        # off the same record when it re-derives.
        system_prompt = churro_capture_system_prompt(record)
        if (identity := churro.vendor_identity(system_prompt)) is not None:
            record["vendor_identity"] = identity
        # The blob is immutable before either derived operation. Pass these
        # module bindings explicitly so a pinning test can observe that order.
        record.update(
            derive_churro_capture(
                raw_response,
                transport_stop_reason,
                parser=parser,
                system_prompt=system_prompt,
                document_parser=parse_churro_response,
                repetition_detector=detect_repetition,
            )
        )
    elif adapter == "chandra.v1" and parser in {"html", "json"}:
        # Import locally: the runnable sibling module imports this retention
        # seam, while it must remain the one owner of Chandra's two shapes --
        # the vendor layout grammar, and the committed fixture's placeholder.
        import chandra

        if parser == "html":
            # Which vendor pin the prompt bytes beside this reading were taken
            # from, recorded whatever the answer turned out to be: the pin is a
            # fact about the request, not about whether the response parsed
            # (GOVERNANCE 6).
            record["vendor_identity"] = chandra.vendor_identity()
            parsed_layout = chandra.parse_layout(raw_response)
            parsed: Any
            if chandra_layout.is_refusal(parsed_layout):
                parsed = {"parse_outcome": parsed_layout["parse_outcome"]}
            else:
                parsed = parsed_layout["page_text"]
                # The grammar's own findings travel with the reading. They are
                # the whole of what the vendor's parser would have printed to a
                # stdout nobody retains -- a malformed box, a retained blank
                # page, text outside every block, a block count that does not
                # reconcile -- and each one names a fact about this response
                # that the page text alone cannot show (GOVERNANCE 2).
                record["findings"] = list(parsed_layout["findings"])
        else:
            parsed = chandra.parse_fixture_placeholder(raw_response)
        if isinstance(parsed, dict) and set(parsed) == {"parse_outcome"}:
            record["parse"] = {
                "state": "unrecognized-shape",
                "parser": parser,
                "outcome": parsed["parse_outcome"],
            }
            record["stop_reason"] = "partial-parse-unrecognized-shape"
        else:
            record["parse"] = {"state": "parsed", "parser": parser, "text": parsed}
        _record_post_hoc_repetition(record, raw_response, ceiling=chandra.MAX_RESPONSE_BYTES)
    elif adapter == "dai.v1" and parser == "text":
        try:
            record["parse"] = {
                "state": "parsed",
                "parser": "text",
                "text": validate_dai_text(raw_response),
            }
        except SchemaRefusal as error:
            record["parse"] = {"state": "failed", "parser": "text", "reason": str(error)}
            record["stop_reason"] = "partial-parse-failed"
    return record


def stage_major_schedule(
    parish_id: str, acts: Iterable[dict[str, Any]], chairs: Iterable[str]
) -> list[dict[str, str]]:
    """One resident chair at a time; deterministic chair-outer, act-inner order."""
    if not isinstance(parish_id, str) or not parish_id:
        raise SchemaRefusal("schedule parish identity is blank")
    chair_rows = list(chairs)
    if any(not isinstance(chair, str) or not chair for chair in chair_rows):
        raise SchemaRefusal("schedule chair identity is blank")
    ordered_chairs = sorted(set(chair_rows))
    if len(ordered_chairs) != len(chair_rows):
        # Iterables in production are lists; accepting duplicates makes a repeated
        # serving action look like normal scheduling, so materialize once below.
        raise SchemaRefusal("schedule repeats a chair")
    rows = list(acts)
    seen_acts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("act_id"), str) or not row["act_id"]:
            raise SchemaRefusal("schedule act has no identity")
        # The same reason the chair check gives, in the other dimension: a
        # repeated act row is a second serving of one act wearing the look of
        # ordinary scheduling, and every chair would carry it, so one duplicate
        # in becomes one duplicate Testimonium per chair out.
        if row["act_id"] in seen_acts:
            raise SchemaRefusal("schedule repeats an act")
        seen_acts.add(row["act_id"])
        # Checked rather than left to `sorted`, which answers a non-integer
        # ordinal with an unnamed TypeError from inside a comparison.
        ordinal = row.get("page_ordinal", _UNPLACED_ORDINAL)
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise SchemaRefusal("schedule act page ordinal is not an integer")
    ordered_acts = sorted(
        rows, key=lambda row: (row.get("page_ordinal", _UNPLACED_ORDINAL), row["act_id"])
    )
    return [
        {
            "policy": SCHEDULING_POLICY,
            "parish_id": parish_id,
            "chair": chair,
            "act_id": act["act_id"],
        }
        for chair in ordered_chairs
        for act in ordered_acts
    ]


class SingleChairResidency:
    """Fail-closed ownership of the one model resource an orchestrator may load.

    The resident name is reserved before ``load`` runs and is cleared only after
    ``unload`` succeeds. A failed unload therefore blocks every later acquire;
    it can never be mistaken for proof that the resource became vacant.

    **A failed load blocks them too, and that is the point.** The reservation is
    taken before ``load``, so a load that raises leaves the chair marked resident
    with no matching ``unload`` -- a load can fail with weights already mapped,
    and this guard exists to keep a second chair off a card whose occupancy is
    unknown. Clearing the reservation would be guessing that nothing was
    allocated. Recovering from it is an operator act against observed provider
    state, exactly as GOVERNANCE 8 requires of a shutdown, never an inference
    this object may make on its own.
    """

    def __init__(
        self,
        load: Callable[[str], Any],
        unload: Callable[[str, Any], None],
    ) -> None:
        self._load = load
        self._unload = unload
        self._resident: str | None = None
        self._lock = RLock()

    @property
    def resident(self) -> str | None:
        with self._lock:
            return self._resident

    @contextmanager
    def occupy(self, chair: str) -> Iterator[Any]:
        if not isinstance(chair, str) or not chair:
            raise SchemaRefusal("residency chair identity is blank")
        with self._lock:
            if self._resident is not None:
                raise SchemaRefusal(
                    f"cannot load chair {chair!r} while chair {self._resident!r} is resident"
                )
            self._resident = chair
        resource = self._load(chair)
        try:
            yield resource
        except BaseException as refusal:
            try:
                self._release(chair, resource)
            except BaseException as cleanup_error:
                # The refusal is the security decision the caller must see. Keep a
                # failed cleanup as its cause, and keep the chair resident, rather
                # than replacing the refusal with a generic unload exception.
                raise refusal from cleanup_error
            raise
        else:
            self._release(chair, resource)

    def _release(self, chair: str, resource: Any) -> None:
        """Clear residency only after the resource and guard both verify release."""
        self._unload(chair, resource)
        with self._lock:
            if self._resident != chair:
                raise SchemaRefusal("single-chair residency state diverged during unload")
            self._resident = None


def execute_stage_major_schedule(
    schedule: Iterable[dict[str, str]],
    *,
    residency: SingleChairResidency,
    serve: Callable[[Any, dict[str, str]], Any],
) -> list[Any]:
    """Execute only contiguous chair blocks through the shared residency guard."""
    rows = list(schedule)
    expected_fields = {"policy", "parish_id", "chair", "act_id"}
    if any(
        not isinstance(row, dict)
        or set(row) != expected_fields
        or row["policy"] != SCHEDULING_POLICY
        or any(not isinstance(row[field], str) or not row[field] for field in expected_fields)
        for row in rows
    ):
        raise SchemaRefusal("stage-major execution received a malformed schedule row")
    chair_blocks = []
    for chair, chair_rows in groupby(rows, key=lambda row: row["chair"]):
        chair_blocks.append(chair)
        served = [row["act_id"] for row in chair_rows]
        # The block is what one residency actually serves, so a repeat inside it
        # is a second serving of one act under one load -- the same defect the
        # builder refuses, arriving through a schedule this executor did not
        # build. Checked here rather than trusted from there.
        if len(set(served)) != len(served):
            raise SchemaRefusal("stage-major execution schedule serves one act twice to a chair")
    if len(chair_blocks) != len(set(chair_blocks)):
        raise SchemaRefusal("stage-major execution schedule returns to an unloaded chair")
    if len({row["parish_id"] for row in rows}) > 1:
        raise SchemaRefusal("stage-major execution schedule mixes parish identities")
    results = []
    for chair, chair_rows in groupby(rows, key=lambda row: row["chair"]):
        with residency.occupy(chair) as resource:
            results.extend(serve(resource, row) for row in chair_rows)
    return results


def _reference(value: object, name: str) -> None:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"DAI {name} reference has no closed digest shape")
    if not isinstance(value["relative_path"], str) or not value["relative_path"]:
        raise SchemaRefusal(f"DAI {name} reference path is blank")
    path = value["relative_path"]
    if path.startswith("/") or ".." in path.split("/"):
        raise SchemaRefusal(f"DAI {name} reference path escapes the run tree")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise SchemaRefusal(f"DAI {name} reference digest is invalid")
