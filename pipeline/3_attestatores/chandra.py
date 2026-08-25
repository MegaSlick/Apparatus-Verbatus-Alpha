"""Chandra's page-witness adapter.

The vendor has not published a stable response specimen.  This module therefore
accepts only the small fixture shape below, retains every response byte before
inspection, and names every other shape in the derived payload instead of
pretending it contained no testimony.  Geometry is derived only from the raw
response that was retained beside it.

**Provenance.** The
occupant is ``datalab-to/chandra`` ("Chandra OCR 2",
https://github.com/datalab-to/chandra), which converts page images to structured
HTML/Markdown/JSON with block identification and reading order.  Unlike
``feeding.churro_prompt``, **no vendor bytes are carried here.** The instruction
below is this repository's own wording and the accepted shape below is this
repository's own declared placeholder, because the vendor publishes no output
specimen to carry: as of the host's reading on 2026-08-22 its README documents
what the model produces but shows no example response body.  There is therefore
nothing to license and no borrowed line to name, and the honest record is that
the wire shape is **unverified** rather than specified.

That is the reason for the width of `parse`'s outcome vocabulary.  A specimen
would let this module assert a schema; without one it may only state what it can
read and name everything else, so the first real response either validates the
placeholder or arrives as a named surprise with its bytes intact.  Replacing the
placeholder with a measured shape is the work a pod reading pays for, and until
then no claim here goes further than the fixture can support (GOVERNANCE 10).
"""

from __future__ import annotations

import json
import math
from typing import Any

from common.native_witness import validate_presented

QUANTIZATION_RULE = "chandra.v1.floor-min-ceil-max.sealed-page-pixels"
FIXTURE_RESPONSE_SCHEMA = "fixture-chandra-response.v1"


def prompt() -> dict[str, str]:
    """Frame the page-layout request without claiming an unverified wire schema."""
    return {
        "instruction": "Transcribe this complete page and report layout blocks in reading order."
    }


def parse(raw_response: bytes) -> Any:
    """Return validated text, or a named shape outcome that preserves custody."""
    try:
        decoded = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"parse_outcome": "invalid-json"}
    if not isinstance(decoded, dict):
        return {"parse_outcome": "top-level-not-object"}
    if decoded.get("schema") != FIXTURE_RESPONSE_SCHEMA:
        return {"parse_outcome": "unverified-response-schema"}
    if "markdown" in decoded and "text" in decoded and decoded["markdown"] != decoded["text"]:
        return {"parse_outcome": "conflicting-text-fields"}
    text = decoded.get("markdown", decoded.get("text"))
    blocks = decoded.get("blocks")
    if not isinstance(text, str):
        return {"parse_outcome": "missing-text"}
    if not isinstance(blocks, list):
        return {"parse_outcome": "missing-block-list"}
    if any(not isinstance(block, dict) or "bbox" not in block for block in blocks):
        return {"parse_outcome": "malformed-block-list"}
    if any(_quantize_box(block["bbox"]) is None for block in blocks):
        return {"parse_outcome": "malformed-block-geometry"}
    return text


def retain(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Retain Chandra's response before its parser inspects the retained bytes."""
    from feeding import retain_model_view

    return retain_model_view(*args, **kwargs)


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Bind either verified compatibility region or the page witness view.

    Scope controls invocation, not presentation kind: the act compatibility
    records retain their original Designator crop while the durable witness
    record carries the whole page. ``context`` remains part of the common
    adapter interface because adapters may publish their own derived crop.
    """
    validate_presented(presentation)
    return presentation


def observe(presentation: dict[str, Any], native_payload: Any) -> list[dict[str, Any]]:
    """Quantize Chandra block boxes from its retained raw JSON into page pixels."""
    validate_presented(presentation)
    if not isinstance(native_payload, bytes):
        return []
    try:
        decoded = json.loads(native_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != FIXTURE_RESPONSE_SCHEMA
        or not isinstance(decoded.get("blocks"), list)
    ):
        return []
    observed: list[dict[str, Any]] = []
    for block in decoded["blocks"]:
        bounds = _quantize_box(block.get("bbox") if isinstance(block, dict) else None)
        if bounds is None:
            # `parse` names this before the record is written. This guard keeps
            # direct callers equally conservative if they bypass that seam.
            return []
        observed.append(
            {"ordinal": len(observed), "bounds": bounds, "bounds_source": "native", "span": None}
        )
    return observed


def _quantize_box(value: Any) -> dict[str, int] | None:
    """Apply the declared min-floor/max-ceil rule, never Python coercion."""
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except OverflowError:
        # JSON integers have arbitrary precision in Python.  A coordinate too
        # large for the float-based quantization rule is malformed geometry,
        # not an interpreter error that may escape the named parse boundary.
        return None
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        return None
    left, top, right, bottom = math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)
    if right <= left or bottom <= top or left < 0 or top < 0:
        return None
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}
