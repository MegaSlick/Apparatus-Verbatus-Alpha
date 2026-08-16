"""Fixture-only witness feeding contracts for R3.

These adapters describe exactly what a real witness call must receive and retain,
but deliberately make no model call.  They are small enough to exercise against
fixtures while preserving the operational boundary: a response is already
complete when it reaches this module.  In particular, repetition is inspected
*after* capture; it cannot affect generation or alter the captured bytes.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal

CHURRO_OUTPUT_TOKENS = 24_000
DAI_MAX_WIDTH_PX = 1_500
SCHEDULING_POLICY = "chair-outer-act-inner.stage-major-parish.v1"
_UNCERTAINTY_TOKENS = ("[UNCERTAIN]", "[CROSSED_OUT]")
_REPETITION_WINDOW = 24
_REPETITION_MIN_REPEATS = 3


def churro_prompt() -> str:
    """The trained XML framing, retained verbatim in every Churro model view."""
    return (
        "You are an expert in diplomatic transcription of historical documents from various "
        "languages. Your task is to extract the full text from a given page. Only output the "
        "transcribed text between <output> and </output> tags.\n\n"
        "Follow these instructions:\n"
        "1. Transcribe the entirety of the scanned document page, including handwritten and "
        "printed text, tables, captions, headers, and main text.\n"
        "2. Skip non-text elements without describing them.\n"
        "3. Do not modernize, standardize, or translate text.\n"
        "4. Return exactly <output>\\nextracted text\\n</output>."
    )


def churro_generation() -> dict[str, int]:
    """The predeclared operational bound, not a content or repetition control."""
    return {"max_new_tokens": CHURRO_OUTPUT_TOKENS}


def validate_churro_xml(raw: bytes) -> str:
    """Validate the native Churro XML while retaining raw bytes on every failure."""
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"Churro response is not parseable XML: {error}") from error
    if root.tag != "output" or set(root.attrib) or list(root):
        raise SchemaRefusal("Churro response must be a plain <output> XML element")
    return root.text or ""


def detect_repetition(raw: bytes) -> dict[str, Any] | None:
    """Report a repeated tail after capture; this function has no generation input."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < _REPETITION_WINDOW * _REPETITION_MIN_REPEATS:
        return None
    for width in range(
        _REPETITION_WINDOW, min(256, len(normalized) // _REPETITION_MIN_REPEATS) + 1
    ):
        unit = normalized[-width:]
        repeats = 1
        while normalized.endswith(unit * (repeats + 1)):
            repeats += 1
        if repeats >= _REPETITION_MIN_REPEATS:
            return {"kind": "post-hoc-repetition", "unit_characters": width, "repeats": repeats}
    return None


def dai_model_view(
    *,
    image_ref: dict[str, str],
    width_px: int,
    height_px: int,
    system_prompt_ref: dict[str, str],
    query_prompt_ref: dict[str, str],
    generation_config_ref: dict[str, str],
) -> dict[str, Any]:
    """Build DAI's crop view, referencing carried prompt/config bytes by manifest."""
    for name, reference in (
        ("image", image_ref),
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
    resized_width = min(width_px, DAI_MAX_WIDTH_PX)
    resized_height = (height_px * resized_width + width_px - 1) // width_px
    return {
        "adapter": "dai-atr.v1",
        "image_ref": image_ref,
        "transform": {
            "kind": "resize-preserve-aspect",
            "source_width_px": width_px,
            "source_height_px": height_px,
            "target_width_px": resized_width,
            "target_height_px": resized_height,
        },
        "prompts": {"system": system_prompt_ref, "query": query_prompt_ref},
        "generation_config_ref": generation_config_ref,
        "uncertainty_tokens_preserved": list(_UNCERTAINTY_TOKENS),
    }


def chandra_capture_intake(
    tree: Any, *, response_ref: object, receipt_ref: object
) -> dict[str, Any]:
    """Consume R2's one-receipt raw response without re-serving Chandra.

    The shared custody rule lives in common/chandra_custody.py (a stage may not
    import another stage's module).  The result intentionally carries the two
    original references alongside the exact raw bytes' digest.
    """
    from common.chandra_custody import read_retained_chandra_response

    raw = read_retained_chandra_response(tree, response_ref, receipt_ref)
    return {
        "adapter": "chandra-capture.v1",
        "response_ref": response_ref,
        "receipt_ref": receipt_ref,
        "raw_response_sha256": digest_bytes(raw),
    }


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
    raw_digest, published = tree.put_blob("attestatores", raw_response)
    record: dict[str, Any] = {
        "schema": "attestatores-model-view.v1",
        "adapter": adapter,
        "view": view,
        "raw_response_ref": {"relative_path": published.relative_path, "sha256": raw_digest},
        "transport_stop_reason": transport_stop_reason,
        "findings": [],
        "parse": {"state": "not-requested" if parser is None else "pending", "parser": parser},
    }
    if adapter == "churro.v1":
        if finding := detect_repetition(raw_response):
            record["findings"].append(finding)
            record["stop_reason"] = "partial-post-hoc-repetition-detected"
        else:
            record["stop_reason"] = transport_stop_reason
        if parser == "xml":
            try:
                record["parse"] = {
                    "state": "parsed",
                    "parser": "xml",
                    "text": validate_churro_xml(raw_response),
                }
            except SchemaRefusal as error:
                record["parse"] = {"state": "failed", "parser": "xml", "reason": str(error)}
                record["stop_reason"] = "partial-parse-failed"
    else:
        record["stop_reason"] = transport_stop_reason
    return record


def stage_major_schedule(
    parish_id: str, acts: Iterable[dict[str, Any]], chairs: Iterable[str]
) -> list[dict[str, str]]:
    """One resident chair at a time; deterministic chair-outer, act-inner order."""
    if not isinstance(parish_id, str) or not parish_id:
        raise SchemaRefusal("schedule parish identity is blank")
    chair_rows = list(chairs)
    ordered_chairs = sorted(set(chair_rows))
    if len(ordered_chairs) != len(chair_rows):
        # Iterables in production are lists; accepting duplicates makes a repeated
        # serving action look like normal scheduling, so materialize once below.
        raise SchemaRefusal("schedule repeats a chair")
    rows = list(acts)
    if any(not isinstance(row.get("act_id"), str) or not row["act_id"] for row in rows):
        raise SchemaRefusal("schedule act has no identity")
    ordered_acts = sorted(rows, key=lambda row: (row.get("page_ordinal", -1), row["act_id"]))
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


def _reference(value: object, name: str) -> None:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"DAI {name} reference has no closed digest shape")
    if not isinstance(value["relative_path"], str) or not value["relative_path"]:
        raise SchemaRefusal(f"DAI {name} reference path is blank")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise SchemaRefusal(f"DAI {name} reference digest is invalid")
