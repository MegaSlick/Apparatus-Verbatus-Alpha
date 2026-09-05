"""Chandra's page-witness adapter.

The vendor has not published a stable response specimen.  This module therefore
accepts exactly two declared shapes -- the repository's own wire contract
(`chandra_response.py`, the shape `prompt` asks for) and the committed fixture's
synthetic placeholder -- retains every response byte before inspection, and
names every other shape in the derived payload instead of pretending it
contained no testimony.  Geometry is derived only from the raw response that
was retained beside it.

**Provenance.** The
occupant is ``datalab-to/chandra`` ("Chandra OCR 2",
https://github.com/datalab-to/chandra), which converts page images to structured
HTML/Markdown/JSON with block identification and reading order.  Unlike
``feeding.churro_prompt``, **no vendor bytes are carried here.** The instruction
below is this repository's own wording and both accepted shapes are this
repository's own declarations, because the vendor publishes no output specimen
to carry: as of the host's reading on 2026-08-22 its README documents what the
model produces but shows no example response body.  There is therefore nothing
to license and no borrowed line to name, and the honest record is that
Chandra's *native* wire shape is **unverified** rather than specified.

That is why the served Chandra witness is asked, in `prompt`, for a closed JSON
shape of this repository's choosing -- the same move the Designator's structure
pass makes with `verbatus-structure-answer.v1` -- rather than parsed in a
native mode nobody has a specimen of.  The first real response either follows
the instruction, and parses, or does not, and lands as a named surprise with
its bytes intact (GOVERNANCE 10).  Replacing that with a measured native shape
is the work a pod reading pays for.

**Two prompts, deliberately.** `prompt()` is what a served chair is asked.
`FIXTURE_PROMPT` is the instruction the committed fixture's synthetic Chandra
rows were declared against, and it is what the fixture posture records in the
retained model view (`run.py::resolve_attempt`), because that view is sealed
into the fixture's pinned bytes: the fixture never asks anything, so its
recorded prompt is a declaration, and rewording the live instruction must not
move a fixture byte.
"""

from __future__ import annotations

import json
import math
from typing import Any, Final

import chandra_response

from common.contracts.errors import SchemaRefusal
from common.native_witness import validate_presented

QUANTIZATION_RULE = "chandra.v1.floor-min-ceil-max.sealed-page-pixels"
FIXTURE_RESPONSE_SCHEMA = "fixture-chandra-response.v1"
PAGE_RESPONSE_SCHEMA = chandra_response.PAGE_RESPONSE_SCHEMA
# Independent parser bounds for bytes that cross the native model boundary.
# The byte ceiling matches the repository's existing RunPod response ceiling;
# keeping it here as well makes direct and future non-RunPod callers obey the
# same finite intake.  The block ceiling is a chosen operational ceiling, not a
# claim about Chandra's behaviour: ten thousand layout blocks on one page leaves
# ample headroom while preventing one compact response from expanding into an
# unbounded list of derived geometry records.
MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
MAX_LAYOUT_BLOCKS: Final = 10_000

# The instruction the committed fixture's synthetic Chandra responses were
# declared against. Recorded by the fixture posture only; see the module
# docstring for why it is frozen here rather than shared with `prompt`.
FIXTURE_PROMPT: Final[dict[str, str]] = {
    "instruction": "Transcribe this complete page and report layout blocks in reading order."
}

_LIVE_INSTRUCTION: Final = (
    "Transcribe this complete page exactly as written -- nothing corrected, "
    "modernized, summarized, or left out -- through to its end, and report its "
    "layout blocks in reading order.\n\n"
    "For each block, report:\n"
    "- box_1000: one rectangle [x0, y0, x1, y1] in normalized integer "
    "coordinates from 0 to 1000, measured against the image exactly as shown "
    "-- x0,y0 the top-left corner and x1,y1 the bottom-right corner.\n"
    "- text: the block's transcription exactly as written.\n\n"
    "Respond with exactly this JSON shape and nothing else -- no explanation, "
    "no markdown fencing, no text outside the JSON object:\n\n"
    f'{{"schema": "{PAGE_RESPONSE_SCHEMA}", "blocks": '
    '[{"box_1000": [x0, y0, x1, y1], "text": "..."}]}\n\n'
    "If you can transcribe the page but cannot place its blocks, respond "
    f'instead with {{"schema": "{PAGE_RESPONSE_SCHEMA}", "text": "..."}} '
    "carrying the whole page's transcription. If the page holds no text, "
    "report an empty blocks list."
)


def prompt() -> dict[str, str]:
    """Frame the page request for the closed shape `chandra_response` parses."""
    return {"instruction": _LIVE_INSTRUCTION}


def _decode(raw_response: Any) -> tuple[Any | None, str | None]:
    """Decode bounded raw JSON into a value or one closed parse outcome."""
    if not isinstance(raw_response, bytes):
        return None, "raw-response-not-bytes"
    if len(raw_response) > MAX_RESPONSE_BYTES:
        return None, "response-too-large"
    try:
        return json.loads(raw_response.decode("utf-8")), None
    except RecursionError:
        # The stdlib scanner raises this separately from JSONDecodeError for a
        # sufficiently deep but otherwise valid value.  It is still one bad
        # witness response, never permission to crash the whole stage.
        return None, "excessive-json-nesting"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid-json"


def parse(raw_response: bytes, *, served: bool = False) -> Any:
    """Return validated page text, or a named shape outcome that preserves custody.

    Dispatches on the declared `schema`: the wire contract goes to
    `chandra_response.parse` (which re-decodes under its own stricter
    duplicate-member guard), the fixture placeholder is validated here exactly
    as it always was, and any other shape is named.

    ``served`` says these bytes came off a chair that actually answered, and it
    changes exactly one thing: the fixture placeholder schema is then refused
    as `unverified-response-schema` like any other undeclared shape (CodeRabbit
    round 1, T7). `FIXTURE_RESPONSE_SCHEMA` is the committed fixture's own
    stand-in, declared by `proof/skeleton_fixture.toml` and never asked for by
    `prompt()`; a served chair answering in it is answering a question nobody
    put to it, and reading that as a page of text would publish a reading whose
    shape this repository never verified against anything (GOVERNANCE 10). The
    bytes are already retained before this runs, so the refusal loses nothing:
    it names a surprise instead of dressing it as a reading. The fixture
    posture passes nothing and keeps the acceptance its pinned bytes depend on.
    """
    decoded, problem = _decode(raw_response)
    if problem is not None:
        return {"parse_outcome": problem}
    if not isinstance(decoded, dict):
        return {"parse_outcome": "top-level-not-object"}
    if decoded.get("schema") == PAGE_RESPONSE_SCHEMA:
        parsed = chandra_response.parse(raw_response)
        if chandra_response.is_refusal(parsed):
            return parsed
        return parsed["page_text"]
    if served or decoded.get("schema") != FIXTURE_RESPONSE_SCHEMA:
        return {"parse_outcome": "unverified-response-schema"}
    if "markdown" in decoded and "text" in decoded and decoded["markdown"] != decoded["text"]:
        return {"parse_outcome": "conflicting-text-fields"}
    text = decoded.get("markdown", decoded.get("text"))
    blocks = decoded.get("blocks")
    if not isinstance(text, str):
        return {"parse_outcome": "missing-text"}
    if not isinstance(blocks, list):
        return {"parse_outcome": "missing-block-list"}
    if len(blocks) > MAX_LAYOUT_BLOCKS:
        return {"parse_outcome": "too-many-layout-blocks"}
    if any(not isinstance(block, dict) or "bbox" not in block for block in blocks):
        return {"parse_outcome": "malformed-block-list"}
    if any(_quantize_box(block["bbox"]) is None for block in blocks):
        return {"parse_outcome": "malformed-block-geometry"}
    return text


def retain(
    tree: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain Chandra's response under its own registry identity only.

    Forwarding `**kwargs` into `retain_model_view` let the caller supply
    `adapter=`, so code that had resolved `chandra.v1` could file a Chandra
    response as `churro.v1`. The retained record would then name the wrong
    model boundary, and read-back would hand a Chandra page's layout blocks to
    Churro's XML parser as an unparseable capture -- the sealed roster no longer
    binding this chair's provenance (GOVERNANCE 6). Churro's and DAI's wrappers
    pin their names for the same reason; this one now does too, and accepts no
    `adapter` argument to pin.
    """
    from feeding import retain_model_view

    return retain_model_view(
        tree,
        adapter="chandra.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Bind either verified compatibility region or the page witness view.

    Scope controls invocation, not presentation kind: the act compatibility
    records retain their original Designator crop while the durable witness
    record carries the whole page. ``context`` remains part of the common
    adapter interface because adapters may publish their own derived crop.
    """
    validate_presented(presentation)
    return presentation


def observe(
    presentation: dict[str, Any],
    native_payload: Any,
    *,
    page_size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Derive Chandra's page-pixel geometry from its retained raw response.

    A wire-contract body's normalized boxes are converted to sealed-page pixels
    with `chandra_response.block_page_bounds`, which needs the sealed page's
    own size: the act view of a page witness presents one crop while restating
    page-level geometry, so the presentation's bounds are not the denominator.
    `run.py` passes ``page_size`` at both of its Chandra call sites; a caller
    that omits it for a body that needs it is refused rather than handed
    geometry in the wrong space. Each block's span indexes the page text
    `parse` returns for the same bytes. A body that reports no block geometry
    -- the page-text form, or an empty blocks list -- derives none, exactly as
    the fixture placeholder's empty block list does; the page record then
    carries the presentation echo `run.py` gives every page with no reported
    geometry (excluded from routing and coverage by its `bounds_source`), and
    the shared page-edge check, which admits only reported geometry, is never
    handed an echo by this adapter.

    The fixture placeholder's page-pixel float boxes are quantized by the
    declared rule as before and need no page size.
    """
    validate_presented(presentation)
    decoded, problem = _decode(native_payload)
    if problem is not None:
        return []
    if isinstance(decoded, dict) and decoded.get("schema") == PAGE_RESPONSE_SCHEMA:
        parsed = chandra_response.parse(native_payload)
        if chandra_response.is_refusal(parsed) or not parsed["blocks"]:
            return []
        if page_size is None:
            raise SchemaRefusal(
                "a Chandra wire-contract response carries normalized block geometry, which "
                "converts to sealed-page pixels only against the sealed page's own size; "
                "pass page_size"
            )
        return [
            {
                "ordinal": block["ordinal"],
                "bounds": chandra_response.block_page_bounds(block, page_size=page_size),
                "bounds_source": "native",
                "span": dict(span),
            }
            for block, span in zip(parsed["blocks"], parsed["spans"], strict=True)
        ]
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != FIXTURE_RESPONSE_SCHEMA
        or not isinstance(decoded.get("blocks"), list)
        or len(decoded["blocks"]) > MAX_LAYOUT_BLOCKS
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
