"""Shared witness feeding contracts: prompts, generation views, retention and
scheduling for the DAI, Chandra and Churro adapters. A response is already
complete when it reaches this module; repetition is inspected *after*
capture, so it can never affect generation or alter the captured bytes.
"""

from __future__ import annotations

import json
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
    detect_repetition,
    parse_churro_response,
)
from common.request_capacity import DECLARED_ANSWER_BOUND_TOKENS
from operations.serving.preflight import assert_generation_config_key_coverage

DAI_MAX_WIDTH_PX = 1_500
# A width-only ceiling misses a narrow, tall crop whose total pixel count
# still exceeds what a served row admits; the engine then resizes it again
# internally, silently contradicting whatever transform this schema recorded.
# `DAI_MAX_TOTAL_PIXELS` closes that: min(max_pixels) over every DAI row in
# `config/serving_recipes_real.toml`, pinned against that file by
# `test_feeding.py::test_dai_total_pixel_ceiling_is_the_smallest_shipped_rows_max_pixels`.
#
# It is the smallest across tiers, not the tier this run serves under,
# because `dai_dimensions` must stay a pure function of the crop's own
# pixels -- `witness_adapters._dai_present`, `validate_adapter_presentation`
# and `publish_attempt` all re-derive the same crop with no served row in
# hand. The cost: a crop close to the width ceiling on a larger tier is cut
# down to what the smallest tier needs, even where its own row could hold
# more.
#
# Both ceilings carry their provenance into the record they seal: the width
# ceiling is the model card itself (Training/Parameters: "Image width: 1500
# pixels (max)"), the total-pixel ceiling is the shipped serving catalogue.
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
        "at every tier since the per-tier pixel ladder was retired -- the floor every deployed tier's engine "
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
        # `html` is Chandra's live vendor layout grammar; `json` is the
        # committed fixture's own placeholder shape, refused for a served
        # chair so retained history cannot become a live reading.
        ("chandra.v1", "html"),
        ("chandra.v1", "json"),
        # Churro has one grammar, `xml` (the vendor's `HistoricalDocument`),
        # in both postures.
        ("churro.v1", "xml"),
        ("dai.v1", "text"),
    }
)
# The expert transcription convention defined on the training dataset's own
# card, `Teklia/DAI-CReTDHI-RecordGold-ATR` (MIT-licensed); its sibling corpus
# `Teklia/DAI-CReTDHI-RecordGeneanet-ATR` declares no licence, so nothing from
# it is carried here. `validate_dai_text` preserves these two strings unchanged.
_UNCERTAINTY_TOKENS = ("[UNCERTAIN]", "[CROSSED_OUT]")

#: Declared from the grammar: plain UTF-8 text carrying the RecordGold card's
#: uncertainty convention (`_UNCERTAINTY_TOKENS`), so it can express doubt but
#: has no coordinate vocabulary, so it cannot express layout. Safe to declare
#: uncertainty only because the Perlector's bracket-marker comparison view is
#: already wired for act-scoped capability-declaring chairs
#: (`pipeline/4_perlector/run.py::dissent_testimonia`); declared earlier, this
#: chair would have gone permanently `compared: unknown`.
DAI_FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": True, "can_express_layout": False}
)


#: This module owns no Churro prompt bytes: what a chair is asked is one of
#: the two vendor-attested system strings in `common/churro_document.py`,
#: resolved by name through `churro.py::FRAMINGS`.


def churro_generation() -> dict[str, int]:
    """Churro's declared answer bound, retained as evidence, not the wire value.

    20,000 tokens, read from ``request_capacity.DECLARED_ANSWER_BOUND_TOKENS``
    so this chair's bound cannot drift from the number the bound seam applies.
    What actually goes out is ``min(this, max_model_len - image - prompt)``
    through ``live_witness.generation_bound_sent``.

    The vendor's ``repetition_penalty`` of 1.05 is deliberately not folded in
    here: this declared view is written through the canonical writer, which
    refuses floats outright. It goes on the wire through
    :func:`churro_wire_decoding` instead.
    """
    return {"max_new_tokens": CHURRO_OUTPUT_TOKENS}


def chandra_generation() -> dict[str, int]:
    """Chandra's own declared answer bound, the vendor's own number.

    ``chandra/settings.py``'s ``MAX_OUTPUT_TOKENS = 12384`` at the pinned
    commit. Declared evidence, not itself the wire value: the vendor's own
    pair overruns its own container (12,384 + a 6,045-token A4 image against
    ``--max-model-len 18000``), so ``request_capacity.sendable_max_tokens``
    clamps it and the record says which of the two bound a given call.
    """
    return {"max_new_tokens": DECLARED_ANSWER_BOUND_TOKENS["attestator_1"]}


def churro_wire_decoding() -> dict[str, float]:
    """Churro's own shipped ``repetition_penalty``, because the engine drops it.

    Every serving row pins ``generation_config = "vllm"``, so vLLM ignores the
    model's file and falls back to its own default of 1.0; the model's
    publisher ships 1.05, and the CHURRO paper documents this model entering
    degeneration loops without it. Declining a vendor's own mitigation by
    accident is what this sends back explicitly. At ``temperature = 0`` the
    penalty is applied before the greedy argmax, so determinism is untouched.

    Not folded into :func:`churro_generation`, whose canonical writer refuses
    floats outright; the retained chair-call record carries the exact wire
    value instead (``wire-decimal.v1``, shared with DAI's identical 1.05).
    """
    return {"repetition_penalty": 1.05}


# The tokenizer's own eos_token, `<|im_end|>`, at the pinned revision -- what
# vLLM already holds for DAI without being told.
DAI_TOKENIZER_EOS_TOKEN_ID: Final = 151645


def dai_wire_stop_token_ids() -> dict[str, list[int]]:
    """DAI's second EOS id, sent explicitly as well as resolved under ``auto``.

    Derived from the carried ``eos_token_id`` (:func:`dai_generation`),
    ``[151645, 151643]``, never re-typed. Sent as redundant request evidence,
    not a claim that the engine actually applied its snapshot before a live
    observation proves that. Only the secondary id is added here: the primary
    already matches `DAI_TOKENIZER_EOS_TOKEN_ID`, so resending it would be a
    no-op that is not worth the seam on an unobserved engine.
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


DAI_WEIGHTS_REPOSITORY: Final = "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR"
DAI_WEIGHTS_REVISION: Final = "e371095d4ffe585f31f4974462931ddbac61ff64"
DAI_CARRIED_FILE_SHA256: Final[Mapping[str, str]] = {
    "system.txt": "b4e7d61d4f27f0aa46ba597ebfac3925b3ed87e72583def4bce2bd4f0393c333",
    "query.txt": "3a5cd8eb3263f2511d207f49f9933b1cf184e95fd7a9534871207d8d8b6a3489",
    "generation_config.json": "f4cd2d54597a1a3cb38ac78d5cb275d06f6fd660fef52ee444a58d81297ff027",
}
DAI_CARRIED_FILE_BYTES: Final[Mapping[str, int]] = {
    "system.txt": 206,
    "query.txt": 33,
    "generation_config.json": 243,
}


def dai_prompt() -> dict[str, str]:
    """DAI's ``system.txt`` and ``query.txt``, carried byte-for-byte from Teklia's
    ``DAI_WEIGHTS_REPOSITORY`` at ``DAI_WEIGHTS_REVISION``."""
    return {
        "system": (
            "Tu es un assistant archiviste. Tu dois lire des actes issus de registres "
            "paroissiaux français, du 16è au 18è siècle. Extrais le texte de la marge, du "
            "corps de l'acte, et éventuellement les signatures.\n"
        ),
        "user": "Extrais le texte de ce document.\n",
    }


def dai_generation() -> dict[str, Any]:
    """DAI's shipped ``generation_config.json`` values, carried unchanged from Teklia's
    ``DAI_WEIGHTS_REPOSITORY`` at ``DAI_WEIGHTS_REVISION``."""
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


def dai_generation_accounting(generation_config: str) -> dict[str, Any]:
    """Account for every carried DAI key under the resolved serving posture.

    This is an account of request construction, not a claim that a live engine
    applied every vendor default. ``auto`` directs the engine to the pinned
    vendor file at launch; the chair-call record separately proves the
    request's explicit fields, including manager-owned temperature zero and
    seed.
    """
    if generation_config != "auto":
        raise SchemaRefusal("DAI native generation accounting requires generation_config='auto'")
    vendor_generation = dai_generation()
    deliberately_not_sent = {
        "bos_token_id": "delegated to the engine's pinned model snapshot under auto",
        "pad_token_id": "delegated to the engine's pinned model snapshot under auto",
        "eos_token_id": (
            "delegated to the pinned generation config under auto; the secondary id is "
            "also sent explicitly as stop_token_ids"
        ),
        "do_sample": "intentionally superseded by the governed temperature-zero request",
        "temperature": (
            f"vendor {json.dumps(vendor_generation['temperature'])} is intentionally "
            "superseded by governed temperature zero"
        ),
        "transformers_version": "vendor metadata, not an OpenAI request field",
    }
    assert_generation_config_key_coverage(
        chair="attestator_2",
        vendor_generation_config=vendor_generation,
        sent_keys=("repetition_penalty", "top_k", "top_p"),
        deliberately_not_sent=deliberately_not_sent,
    )
    return {
        "schema": "dai-generation-accounting.v1",
        "engine_generation_config": "auto",
        "vendor_keys_sent_verbatim": ["repetition_penalty", "top_k", "top_p"],
        "vendor_keys_delegated_to_engine_auto": [
            "bos_token_id",
            "eos_token_id",
            "pad_token_id",
        ],
        "vendor_keys_intentionally_overridden": ["do_sample", "temperature"],
        "vendor_metadata_keys": ["transformers_version"],
        "governed_temperature": 0,
        # Canonical writer refuses floats; retain the shortest JSON decimal.
        "vendor_temperature_decimal": json.dumps(vendor_generation["temperature"]),
        "vendor_do_sample": vendor_generation["do_sample"],
        "explicit_secondary_eos_token_ids": dai_wire_stop_token_ids()["stop_token_ids"],
        "seed_source": "sealed-serving-profile",
    }


def validate_dai_generation_accounting(value: Any) -> dict[str, Any]:
    """Close the retained DAI generation ledger against the carried vendor view."""
    expected = dai_generation_accounting("auto")
    if not isinstance(value, dict) or value != expected:
        raise SchemaRefusal(
            "DAI model view generation accounting differs from the closed auto-policy ledger"
        )
    return value


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
    generation_accounting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build DAI's crop view, referencing carried prompt/config bytes by manifest.

    The identity transform is a claim about bytes, not paths: when no resize
    is needed the two image references must name the same SHA-256, not the
    same reference dict. The source is the Designator's proposal crop under
    `2_designator/`; every image a witness is shown is published into
    `3_attestatores/`, so a byte-identical image legitimately appears at two
    stage-owned paths. Equal digests are equal pixels because
    `verify_exemplar_crop_lineage` already proves the source crop is exactly
    `crop_png(sealed page, bounds)`, which `_dai_present` re-derives the same
    way. Requiring the whole dict to match instead refused every genuine
    no-resize DAI act after its response had already come back.
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
        "adapter": "dai-atr.v1" if generation_accounting is None else "dai-atr.v2",
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
    if generation_accounting is not None:
        validate_dai_generation_accounting(generation_accounting)
        view["generation_accounting"] = generation_accounting
    return validate_dai_model_view(view)


def _dai_image_limits() -> dict[str, Any]:
    """The sealed statement of DAI's executable image ceilings.

    No height ceiling: nothing states one, and the total-pixel ceiling already
    bounds height indirectly for any width this rule can produce.
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
    if not isinstance(value, dict) or value.get("adapter") not in {"dai-atr.v1", "dai-atr.v2"}:
        raise SchemaRefusal("DAI model view is not its closed adapter schema")
    if value["adapter"] == "dai-atr.v2":
        fields.add("generation_accounting")
    if set(value) != fields:
        raise SchemaRefusal("DAI model view is not its closed adapter schema")
    if value["adapter"] == "dai-atr.v2":
        validate_dai_generation_accounting(value["generation_accounting"])

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

    Two floor-rounded, aspect-preserving passes: first the width ceiling
    (`DAI_MAX_WIDTH_PX`), skipped if already under it; then, only if the
    result still carries more total pixels than `DAI_MAX_TOTAL_PIXELS`, a
    second scale-down against that ceiling -- since a narrow, tall crop can
    trip the pixel ceiling while passing the width one. This function stays a
    pure function of the crop's own pixels rather than reaching for the row
    that will actually serve the request (see `DAI_MAX_TOTAL_PIXELS`'s own
    comment); a view within it still fits every shipped tier's `max_pixels`.

    Public because `witness_adapters`' presentation writer and read-back
    validator both have to reach exactly this rule.
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

    Shared by all three adapters' crop step so a filesystem swap cannot cross
    the interval between the artifact check and the imaging call that uses it.

    ``what`` names the adapter in every refusal, so an operator is sent to the
    chair whose presentation could not be built.
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

    Re-rolling a reading until it looks better is recovering *quality*, which
    principle 7 refuses; this only records the fact, after the bytes are
    already retained and parsed, so a degenerated-but-complete answer does not
    reach the Perlector as full testimony under ``stop_reason = "stop"``.

    Shared with Churro's own scan so two page witnesses mean the same thing by
    one finding: inspects ``parse["text"]`` when a parse produced one (markup
    like `<div data-bbox=...>` repeats by construction, so raw bytes would be
    the wrong signal), skips a body past the grammar's parsing ceiling rather
    than scanning unbounded bytes, and lets a parse outcome win over a
    repeated tail (the repetition still lands in ``findings`` either way).
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
    than the committed fixture; it decides only whether Chandra's
    fixture-placeholder parser may run. Retention is posture-blind: the bytes
    are published to the tree before any parser runs.
    """
    if not isinstance(adapter, str) or not adapter:
        raise SchemaRefusal("model-view adapter is blank")
    if not isinstance(raw_response, bytes):
        raise SchemaRefusal("model-view raw response is not bytes")
    if not isinstance(transport_stop_reason, str) or not transport_stop_reason:
        raise SchemaRefusal("model-view transport stop reason is blank")
    # A parser that cannot run would leave `parse.state` at "pending" forever:
    # a finished attempt wearing the look of one still in progress (principle 2).
    if parser is not None and (adapter, parser) not in _RUNNABLE_PARSERS:
        raise SchemaRefusal(f"model-view parser {parser!r} does not run for adapter {adapter!r}")
    # A served chair answering in the fixture's own placeholder schema answers
    # a question nobody put to it; reading that as page text would publish a
    # reading whose shape was never verified (principle 8). Refused at the seam,
    # not the parser, because the parser name is what the record will carry.
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
        # Overwritten below if this boundary finds a more honest reason to give.
        "stop_reason": transport_stop_reason,
        "findings": [],
        "parse": {"state": "not-requested" if parser is None else "pending", "parser": parser},
    }
    if adapter == "churro.v1":
        import churro  # Local: churro.py imports this retention seam.

        # Off the view being retained, so the vendor's prompt-echo trim runs
        # against the framing this request actually sent, and
        # `verify_native_capture_bytes` reads the same string back on re-derive.
        system_prompt = churro_capture_system_prompt(record)
        if (identity := churro.vendor_identity(system_prompt)) is not None:
            record["vendor_identity"] = identity
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
        import chandra  # Local: chandra.py imports this retention seam.

        if parser == "html":
            record["vendor_identity"] = chandra.vendor_identity()
            parsed_layout = chandra.parse_layout(raw_response)
            parsed: Any
            if chandra_layout.is_refusal(parsed_layout):
                parsed = {"parse_outcome": parsed_layout["parse_outcome"]}
            else:
                parsed = parsed_layout["page_text"]
                # The grammar's own findings (malformed box, blank page, text
                # outside every block, ...) travel with the reading (principle 2).
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
    # A repeated chair would look like normal scheduling once deduplicated
    # here, so the duplicate is caught before the set absorbs it.
    if len(ordered_chairs) != len(chair_rows):
        raise SchemaRefusal("schedule repeats a chair")
    rows = list(acts)
    seen_acts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("act_id"), str) or not row["act_id"]:
            raise SchemaRefusal("schedule act has no identity")
        # A duplicate act row would become one duplicate Testimonium per chair.
        if row["act_id"] in seen_acts:
            raise SchemaRefusal("schedule repeats an act")
        seen_acts.add(row["act_id"])
        # Checked rather than left to `sorted`, whose TypeError on a
        # non-integer ordinal would be unnamed.
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

    A failed ``load`` blocks them too, deliberately: it can fail with weights
    already mapped, so clearing the reservation would be guessing that nothing
    was allocated. Recovery is an operator act against observed provider
    state, never an inference this object makes on its own.
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
                # Keep the original refusal visible, and the chair resident.
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
        # A repeat here is a second serving of one act under one load; checked
        # rather than trusted from a schedule this executor did not build.
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
