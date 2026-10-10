"""Shared witness feeding contracts: prompts, generation views, retention and
scheduling for the DAI, Chandra and Churro adapters. A response is already
complete when it reaches this module; repetition is inspected *after*
capture, so it can never affect generation or alter the captured bytes.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

from common import chandra_layout, dots_layout
from common.contracts.canonical import digest_of, is_sha256
from common.contracts.errors import SchemaRefusal
from common.decoding import SAMPLING_FIELDS
from common.native_witness import (
    CHURRO_OUTPUT_TOKENS,
    capture_text_view,
    churro_capture_system_prompt,
    derive_churro_capture,
    derive_dots_capture,
    detect_repetition,
    parse_churro_response,
    validate_capture_text_view,
)
from common.request_capacity import DECLARED_ANSWER_BOUND_TOKENS
from operations.serving.preflight import assert_generation_config_key_coverage

DAI_MAX_WIDTH_PX = 1_500
# The width ceiling is the model card itself (Training/Parameters: "Image width:
# 1500 pixels (max)"). No total-pixel ceiling is ours to invent: the vendor's
# own processor admits up to 12,845,056 pixels, and the serving row carries
# that (`config/serving_recipes_real.toml`), so a tall crop is either read at
# its own size or refused by `request_capacity` -- never shrunk a second time.
DAI_LIMIT_SOURCES = {
    "max_width_px": (
        "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR model card, "
        "Training/Parameters: 'Image width: 1500 pixels (max)' "
        "(https://huggingface.co/Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR "
        "@ e371095d4ffe585f31f4974462931ddbac61ff64)"
    ),
}
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
        # dots.mocr has one grammar, its JSON layout cells, in both postures.
        (dots_layout.ADAPTER, dots_layout.PARSER),
    }
)
# The expert transcription convention defined on the training dataset's own
# card, `Teklia/DAI-CReTDHI-RecordGold-ATR` (MIT-licensed); its sibling corpus
# `Teklia/DAI-CReTDHI-RecordGeneanet-ATR` declares no licence, so nothing from
# it is carried here. `validate_dai_text` preserves these two strings unchanged.
_UNCERTAINTY_TOKENS = ("[UNCERTAIN]", "[CROSSED_OUT]")

#: Declared from the grammar: plain UTF-8 text carrying the RecordGold card's
#: uncertainty convention (`_UNCERTAINTY_TOKENS`), so it can express doubt but
#: has no coordinate vocabulary, so it cannot express layout. Dissent removes
#: the doubt markers of a chair that declares uncertainty before comparing
#: (`common/page_path.py::_comparison_text`).
DAI_FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": True, "can_express_layout": False}
)


#: This module owns no Churro prompt bytes: what a chair is asked is one of
#: the two vendor-attested system strings in `common/churro_document.py`,
#: resolved by name through `churro.py::FRAMINGS`.


def churro_generation() -> dict[str, int]:
    """Churro's declared answer bound, retained as evidence, not the wire value.

    25,000 tokens (the vendor's own `DEFAULT_OCR_MAX_TOKENS`), read from ``request_capacity.DECLARED_ANSWER_BOUND_TOKENS``
    so this chair's bound cannot drift from the number the bound seam applies.
    What actually goes out is ``min(this, max_model_len - image - prompt)``
    through ``live_witness.generation_bound_sent``. The sampling values are the
    sealed decoding table's row for this chair.
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


# The tokenizer's own eos_token, `<|im_end|>`, at the pinned revision -- what
# vLLM already holds for DAI without being told.
DAI_TOKENIZER_EOS_TOKEN_ID: Final = 151645


def dai_wire_stop_token_ids() -> dict[str, list[int]]:
    """DAI's second EOS id, sent explicitly as well as read by the engine from the file.

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


def dai_generation_accounting() -> dict[str, Any]:
    """Account for every carried DAI key under the serving posture.

    This is an account of request construction, not a claim that a live engine
    applied every vendor value. Every serving row runs vLLM under
    ``generation_config = "vllm"``, which fills no sampling field from the file
    but still reads its ``eos_token_id``; the sampling values are sent from the
    sealed decoding table's DAI row, which carries this file's values, and the
    chair-call record proves every explicit field, including the seed.
    """
    vendor_generation = dai_generation()
    # Read off the carried file, not typed again: the sealed table's DAI row sends
    # these values (`common/test_vendor_parity.py` proves the two equal).
    sampling_keys = tuple(sorted(set(vendor_generation) & SAMPLING_FIELDS))
    deliberately_not_sent = {
        "bos_token_id": "not a request field; the served tokenizer's own id applies",
        "pad_token_id": "not a request field; a single unbatched sequence is never padded",
        "eos_token_id": (
            "read by the engine from the pinned file under either generation-config "
            "setting; the secondary id is also sent explicitly as stop_token_ids"
        ),
        "do_sample": "an OpenAI request has no such field; the sent temperature samples",
        "transformers_version": "vendor metadata, not an OpenAI request field",
    }
    assert_generation_config_key_coverage(
        chair="attestator_2",
        vendor_generation_config=vendor_generation,
        sent_keys=sampling_keys,
        deliberately_not_sent=deliberately_not_sent,
    )
    return {
        "schema": "dai-generation-accounting.v3",
        "engine_generation_config": "vllm",
        "vendor_keys_sent_by_sealed_decoding": list(sampling_keys),
        "vendor_keys_read_by_engine": ["eos_token_id"],
        "vendor_keys_without_request_field": ["bos_token_id", "do_sample", "pad_token_id"],
        "vendor_metadata_keys": ["transformers_version"],
        "vendor_do_sample": vendor_generation["do_sample"],
        "explicit_secondary_eos_token_ids": dai_wire_stop_token_ids()["stop_token_ids"],
        "seed_source": "sealed-serving-profile",
    }


def validate_dai_generation_accounting(value: Any) -> dict[str, Any]:
    """Close the retained DAI generation ledger against the carried vendor view."""
    expected = dai_generation_accounting()
    if not isinstance(value, dict) or value != expected:
        raise SchemaRefusal("DAI model view generation accounting differs from the closed ledger")
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
    generation_accounting: dict[str, Any],
) -> dict[str, Any]:
    """Build DAI's crop view, referencing carried prompt/config bytes by manifest.

    The identity transform is a claim about bytes, not paths: when no resize
    is needed the two image references must name the same SHA-256, not the
    same reference dict. The source is the Designator's record crop under
    `2_designator/`; every image a witness is shown is published into
    `3_attestatores/`, so a byte-identical image legitimately appears at two
    stage-owned paths. Equal digests are equal pixels because
    `_verify_detector_region` already proves the source crop is exactly
    `crop_png(sealed page, bounds)`, which `_dai_present` re-derives the same
    way.
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
        "adapter": "dai-atr.v2",
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
        "generation_accounting": validate_dai_generation_accounting(generation_accounting),
    }
    return validate_dai_model_view(view)


def _dai_image_limits() -> dict[str, Any]:
    """The sealed statement of DAI's executable image ceiling.

    Width only: the model card states no height or area ceiling, and the
    serving row's own `max_pixels` and `request_capacity` bound the rest.
    """
    return {
        "schema": "dai-image-limits.v5",
        "max_width_px": DAI_MAX_WIDTH_PX,
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
        "generation_accounting",
    }
    if not isinstance(value, dict) or value.get("adapter") != "dai-atr.v2" or set(value) != fields:
        raise SchemaRefusal("DAI model view is not its closed adapter schema")
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
    if not is_sha256(limits_digest) or limits_digest != digest_of(limits):
        raise SchemaRefusal("DAI model view image-limits digest does not match its limits")
    if value["uncertainty_tokens_preserved"] != list(_UNCERTAINTY_TOKENS):
        raise SchemaRefusal("DAI model view does not preserve the declared uncertainty tokens")
    return value


def dai_dimensions(width_px: int, height_px: int) -> tuple[int, int]:
    """Largest aspect-preserving view within DAI's width ceiling.

    One floor-rounded, aspect-preserving pass against `DAI_MAX_WIDTH_PX`,
    skipped if the crop is already under it. Public because `witness_adapters`'
    presentation writer and read-back validator both have to reach exactly
    this rule.
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
        return width_px, height_px
    return DAI_MAX_WIDTH_PX, max(1, height_px * DAI_MAX_WIDTH_PX // width_px)


def _record_post_hoc_repetition(
    record: dict[str, Any], raw_response: bytes, *, ceiling: int
) -> None:
    """Scan a retained capture for a repeated tail and record what it found.

    Re-rolling a reading until it looks better is recovering *quality*, which
    recovery never does; this only records the fact, after the bytes are
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
    context: Any,
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
    are stored before any parser runs.
    """
    if not isinstance(adapter, str) or not adapter:
        raise SchemaRefusal("model-view adapter is blank")
    if not isinstance(raw_response, bytes):
        raise SchemaRefusal("model-view raw response is not bytes")
    if not isinstance(transport_stop_reason, str) or not transport_stop_reason:
        raise SchemaRefusal("model-view transport stop reason is blank")
    # A parser that cannot run would leave `parse.state` at "pending" forever:
    # a finished attempt wearing the look of one still in progress.
    if parser is not None and (adapter, parser) not in _RUNNABLE_PARSERS:
        raise SchemaRefusal(f"model-view parser {parser!r} does not run for adapter {adapter!r}")
    # A served chair answering in the fixture's own placeholder schema answers
    # a question nobody put to it; reading that as page text would publish a
    # reading whose shape was never verified. Refused at the seam,
    # not the parser, because the parser name is what the record will carry.
    if served and adapter == "chandra.v1" and parser == "json":
        raise SchemaRefusal(
            "a served chandra.v1 response cannot be retained under the committed fixture's "
            "placeholder parser 'json'; a live reading is taken only in the vendor layout "
            "grammar ('html')"
        )
    if adapter == "dai.v1":
        validate_dai_model_view(view)
    record: dict[str, Any] = {
        "schema": "attestatores-model-view.v1",
        "adapter": adapter,
        "view": view,
        "raw_response_ref": context.retain(raw_response, "a raw chair response"),
        "transport_stop_reason": transport_stop_reason,
        # Overwritten below if this boundary finds a more honest reason to give.
        "stop_reason": transport_stop_reason,
        "findings": [],
        "parse": {"state": "not-requested" if parser is None else "pending", "parser": parser},
    }
    # The view this grammar reads under, so a later build can refuse it by name;
    # replaced below by the view the parser itself reports wherever it reports one.
    if (text_view := capture_text_view(adapter, parser)) is not None:
        record["text_view"] = text_view
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
                record["text_view"] = parsed_layout["text_view"]
                # The grammar's own findings (malformed box, blank page, text
                # outside every block, ...) travel with the reading.
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
    elif adapter == dots_layout.ADAPTER:
        record["vendor_identity"] = {
            "repository": dots_layout.PROMPT_PROVENANCE["repository"],
            "sha": dots_layout.PROMPT_PROVENANCE["sha"],
            "carried_strings": {
                dots_layout.PROMPT_PROVENANCE["symbol"]: dots_layout.LAYOUT_PROMPT_SHA256
            },
        }
        record.update(derive_dots_capture(raw_response, transport_stop_reason, parser=parser))
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
    # A parser reporting a view this build does not read under refuses here, at
    # the seam, rather than in the first reader of the sealed record.
    return validate_capture_text_view(record)


def _reference(value: object, name: str) -> None:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"DAI {name} reference has no closed digest shape")
    if not isinstance(value["relative_path"], str) or not value["relative_path"]:
        raise SchemaRefusal(f"DAI {name} reference path is blank")
    path = value["relative_path"]
    if path.startswith("/") or ".." in path.split("/"):
        raise SchemaRefusal(f"DAI {name} reference path escapes the run tree")
    if not is_sha256(value["sha256"]):
        raise SchemaRefusal(f"DAI {name} reference digest is invalid")
