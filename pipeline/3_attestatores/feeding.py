"""Fixture-only witness feeding contracts for R3.

These adapters describe exactly what a real witness call must receive and retain,
but deliberately make no model call.  They are small enough to exercise against
fixtures while preserving the operational boundary: a response is already
complete when it reaches this module.  In particular, repetition is inspected
*after* capture; it cannot affect generation or alter the captured bytes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import contextmanager
from itertools import groupby
from threading import RLock
from typing import Any, Callable, Iterator

from common.contracts.canonical import digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.native_witness import (
    CHURRO_OUTPUT_TOKENS,
    derive_churro_capture,
    validate_churro_xml,
)
from common.native_witness import (
    detect_churro_repetition as detect_repetition,
)

DAI_MAX_WIDTH_PX = 1_500
DAI_MAX_HEIGHT_PX = 4_096
DAI_MAX_TOTAL_PIXELS = 2_359_296
# Two of these three ceilings are read off something; one is chosen. Sealing them
# side by side as bare integers made all three read as measured model bounds, and
# a number nobody can trace is exactly what GOVERNANCE 10 refuses -- so each one
# carries its own provenance into the record it seals, and a chosen ceiling says
# out loud that it was chosen.
DAI_LIMIT_SOURCES = {
    "max_width_px": ("design v2.1 section 2: DAI is fed act crops at most 1500 px wide"),
    "max_height_px": (
        "R3 policy, no model source: nothing in the design, the roster or DAI's own "
        "serving flags names a pixel height (the serving script's 4096 is "
        "--max-model-len, a token count). Chosen so the sealed pixel budget still "
        "affords 576 px of width at the ceiling (4096 x 576 = 2359296 exactly); "
        "below it the total-pixel ceiling governs, so it binds only strips past "
        "about 7:1 that carry too little width to read"
    ),
    "max_total_pixels": (
        "DAI's own serving profile max_pixels (the old pipeline's serve_dai.sh), "
        "already recorded in this repository at operations/serving/preflight.py"
    ),
}
SCHEDULING_POLICY = "chair-outer-act-inner.stage-major-parish.v1"
# Where an act with no page ordinal sorts: before every placed act, and named
# rather than spelled -1 at the two places that have to agree on it.
_UNPLACED_ORDINAL = -1
# The (adapter, parser) pairs `retain_model_view` can actually carry to a state.
_RUNNABLE_PARSERS = frozenset({("chandra.v1", "json"), ("churro.v1", "xml"), ("dai.v1", "text")})
_UNCERTAINTY_TOKENS = ("[UNCERTAIN]", "[CROSSED_OUT]")


def churro_prompt() -> dict[str, str]:
    """The trained two-message XML framing, retained verbatim.

    These strings are carried bytes from the quarantined prior adapter,
    ``/window/remote/pilot_churro.py`` (its transcription of ``prompts/ocr.py``
    from the Churro release, https://github.com/stanford-oval/churro), named as
    carried in the commit that brought them per the quarantine rule. Licensing,
    per the upstream repository's own split: the Churro code — these prompt
    strings included — is Apache-2.0, so carrying and redistributing them with
    this attribution is permitted; the weights are under the Qwen research
    license and the training dataset is research-use-only, so weights are never
    vendored, this repository's use of the model stays on the research track,
    and any merging decision needs its own licensing decision first. They are
    split by role because joining or summarizing them changes the actual
    chat-template bytes the model receives.
    """
    system = (
        "You are an expert in diplomatic transcription of historical documents from various "
        "languages. Your task is to extract the full text from a given page. Only output the "
        "transcribed text between <output> and </output> tags."
    )
    user = (
        "Follow these instructions:\n\n"
        "1. You will be provided with a scanned document page.\n\n"
        "2. Perform transcription on the entirety of the page, converting all visible text into "
        "the following format. Include handwritten and print text, if any. Include tables, "
        "captions, headers, main text and all other visible text.\n\n"
        "3. If you encounter any non-text elements, simply skip them without attempting to "
        "describe them.\n\n"
        "4. Do not modernize or standardize the text. For example, if the transcription is using "
        '"ſ" instead of "s" or "а" instead of "a", keep it that way.\n\n'
        "5. When you come across text in languages other than English, transcribe it as "
        "accurately as possible without translation.\n\n"
        "6. Output the OCR result in the following format:\n\n"
        "<output>\nextracted text here\n</output>\n\n"
        "Remember, your goal is to accurately transcribe the text from the scanned page as much "
        "as possible. Process the entire page, even if it contains a large amount of text, and "
        "provide clear, well-formatted output. Pay attention to the appropriate reading order "
        "and layout of the text."
    )
    return {"system": system, "user": user}


def churro_generation() -> dict[str, int]:
    """The predeclared operational bound, not a content or repetition control."""
    return {"max_new_tokens": CHURRO_OUTPUT_TOKENS}


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
    """The one sealed statement of DAI's executable image ceilings."""
    return {
        "schema": "dai-image-limits.v2",
        "max_width_px": DAI_MAX_WIDTH_PX,
        "max_height_px": DAI_MAX_HEIGHT_PX,
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
    """Largest integer aspect-preserving view within every DAI ceiling.

    Height and area must remain search predicates: pre-flooring a height-derived
    width can undercut the largest feasible view by one pixel.

    Public because it decides which pixels a DAI witness is actually shown, and
    the presentation writer and the read-back validator in `witness_adapters`
    both have to reach exactly this rule. Under a private name, renaming it here
    would break those two together with nothing to say they were coupled.
    """
    upper_width = min(width_px, DAI_MAX_WIDTH_PX)
    if upper_width < 1:
        raise SchemaRefusal("DAI image aspect cannot fit the sealed height ceiling")
    low, high = 1, upper_width
    while low < high:
        candidate = (low + high + 1) // 2
        candidate_height = max(1, height_px * candidate // width_px)
        if candidate * candidate_height <= DAI_MAX_TOTAL_PIXELS and (
            candidate_height <= DAI_MAX_HEIGHT_PX
        ):
            low = candidate
        else:
            high = candidate - 1
    target_width = low
    target_height = max(1, height_px * target_width // width_px)
    if target_height > DAI_MAX_HEIGHT_PX:
        raise SchemaRefusal("DAI image aspect cannot fit the sealed height ceiling")
    return target_width, target_height


def retain_model_view(
    tree: Any,
    *,
    adapter: str,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
) -> dict[str, Any]:
    """Retain a reproducible view and raw response, including parser failure bytes."""
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
        # The blob is immutable before either derived operation. Pass these
        # module bindings explicitly so a pinning test can observe that order.
        record.update(
            derive_churro_capture(
                raw_response,
                transport_stop_reason,
                parser=parser,
                xml_parser=validate_churro_xml,
                repetition_detector=detect_repetition,
            )
        )
    elif adapter == "chandra.v1" and parser == "json":
        # Import locally: the runnable sibling module imports this retention
        # seam, while its parser must remain the one owner of Chandra's shapes
        # (the wire contract in `chandra_response`, and the fixture placeholder).
        from chandra import parse as parse_chandra

        parsed = parse_chandra(raw_response)
        if isinstance(parsed, dict) and set(parsed) == {"parse_outcome"}:
            record["parse"] = {
                "state": "unrecognized-shape",
                "parser": "json",
                "outcome": parsed["parse_outcome"],
            }
            record["stop_reason"] = "partial-parse-unrecognized-shape"
        else:
            record["parse"] = {"state": "parsed", "parser": "json", "text": parsed}
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
