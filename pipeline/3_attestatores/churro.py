"""Churro's page-witness adapter.

The occupant is ``stanford-oval/churro-3B`` at the revision
``config/models-real.toml`` pins -- a 3B open-weight VLM fine-tuned for
transcribing historical documents. Its model card documents the model, a
cost/accuracy comparison, the Qwen research licence and a paper, and **no
output format at all**: no layout, no bounding boxes, no reading-order
structure, no tag or JSON schema, no example response body. Its own carried
prompt asks for ``<output>extracted text here</output>`` and for reading order
and layout *of the text*, with no coordinate vocabulary anywhere.

So Churro had no native geometry, and its adapter's honest answer was a
``bounds_source="presented"`` echo of the page it was shown -- a source routing
and coverage expressly exclude, which is why a live Churro reading aligned to
the anchor and never attached to an act. **A layout channel has to be asked for,
as Chandra's was, or it does not exist.** This module asks for one.

**One call, two legal shapes.** The live chair is asked, in its own trained
two-message framing, for the closed JSON shape ``common/churro_response.py``
declares (``feeding.churro_layout_prompt``); a body in the trained ``<output>``
envelope stays fully legal and parses exactly as it always did. A model that
ignores the new clause still reads, retains and aligns, and lands where it lands
today. Nothing here can make the transcription worse except the prompt bytes
themselves, which is why the replaced clauses are quoted in
``feeding.churro_layout_prompt``'s docstring and the sent bytes are retained in
``view.prompt`` on every capture.

**Two prompts, deliberately** -- the split ``chandra.FIXTURE_PROMPT`` already
makes. ``prompt()`` is what a served chair is asked. ``feeding.churro_prompt()``
is the instruction the committed fixture's declared Churro rows were recorded
against, and it is what the fixture posture writes into the retained model view,
because that view is sealed into the fixture's pinned bytes: the fixture never
asks anything, so its recorded prompt is a declaration, and rewording the live
instruction must not move a fixture byte.

**Why the parse and the dispatch live in ``common/``.** ``parse`` here is a thin
call onto ``native_witness.parse_churro_response``, and the closed contract is
``common/churro_response.py``. Churro captures -- alone among the three
adapters -- are re-derived from their retained blob by
``verify_native_capture_bytes`` at three stages, and neither the Perlector nor
the Recensor may import an Attestatores module. Keeping one implementation in
``common/`` is what makes those re-derivations reach the same branch this
adapter wrote the record under, instead of a branch they cannot see.

**No native quantization inherited.** ``QUANTIZATION_RULE`` is Churro's own
declared rule and never Chandra's, even though the two currently spell the same
arithmetic: a rule acquired by omission is a rule nobody declared for this
chair.
"""

from __future__ import annotations

import json
from typing import Any, Final

import feeding

from common import churro_response
from common.contracts.errors import SchemaRefusal
from common.native_witness import parse_churro_response, validate_presented

QUANTIZATION_RULE: Final = "churro.v1.floor-min-ceil-max.sealed-page-pixels"
PAGE_RESPONSE_SCHEMA: Final = churro_response.PAGE_RESPONSE_SCHEMA


def prompt() -> dict[str, str]:
    """Frame the served page request for the shape `churro_response` parses."""
    return feeding.churro_layout_prompt()


def parse(raw_response: bytes) -> Any:
    """Return validated page text, or a named shape outcome that preserves custody.

    The live dispatcher: a JSON object declaring this contract's schema is read
    under it, any other JSON object is a shape nobody asked this chair for, and
    every other body goes to the trained `<output>` parser unchanged. Returns the
    page text on a parse, or a `{"parse_outcome": ...}` record naming what it
    could not place -- the same two return kinds `chandra.parse` has, so a caller
    that already handles one page witness handles this one.

    `native_witness.parse_churro_response` is the implementation, and is called
    rather than restated: `derive_churro_capture` and the two readers'
    `verify_native_capture_bytes` re-derive through that same function, and a
    second copy here could come to disagree with the record it wrote.
    """
    result = parse_churro_response(raw_response)
    if result["state"] == "parsed":
        return result["text"]
    if result["state"] == "unrecognized-shape":
        return {"parse_outcome": result["outcome"]}
    raise SchemaRefusal(result["reason"])


def retain(
    tree: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain one Churro view under its own registry identity only.

    Accepts no ``adapter`` argument to pin: forwarding one would let code that
    had resolved ``churro.v1`` file this response under another chair's model
    boundary, and the retained record would then name the wrong one
    (GOVERNANCE 6). Chandra's and DAI's wrappers pin their names the same way.
    """
    return feeding.retain_model_view(
        tree,
        adapter="churro.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Bind the exact image this chair was given; Churro publishes no crop of its own.

    ``context`` remains part of the common adapter interface because adapters
    may publish their own derived crop (DAI does); this one uses the image
    unchanged.
    """
    del context
    validate_presented(presentation)
    return presentation


def observe(
    presentation: dict[str, Any],
    native_payload: Any,
    *,
    page_size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Derive Churro's page-pixel geometry from its retained raw response.

    A wire-contract body's normalized boxes are converted to sealed-page pixels
    with `churro_response.block_page_bounds`, which needs the sealed page's own
    size: a page witness's act view presents one crop while restating page-level
    geometry, so the presentation's bounds are not the denominator. ``page_size``
    is therefore required for a body that needs it and refused when absent
    rather than answered with geometry in the wrong space -- exactly as
    `chandra.observe` does. Each block's span indexes the page text `parse`
    returns for the same bytes.

    Every other body derives the honest no-layout echo this adapter has always
    returned: a single ``bounds_source="presented"`` entry restating the
    presentation, which routing and coverage expressly exclude, so nothing is
    fabricated from the presentation itself. That covers the trained `<output>`
    envelope, an empty block list, and a body this parser refused -- in the last
    case the refusal is already named on the capture beside these bytes.

    Floats never cross `parse`: the conversion happens here, on geometry, so a
    working layout model is never reported as a broken witness by a parse-level
    refusal about a coordinate.
    """
    validate_presented(presentation)
    if isinstance(native_payload, bytes):
        try:
            decoded = json.loads(native_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            decoded = None
        if churro_response.declares_wire_contract(decoded):
            parsed = churro_response.parse(native_payload)
            if churro_response.is_refusal(parsed) or not parsed["blocks"]:
                return _presented_echo(presentation)
            if page_size is None:
                raise SchemaRefusal(
                    "a Churro wire-contract response carries normalized block geometry, which "
                    "converts to sealed-page pixels only against the sealed page's own size; "
                    "pass page_size"
                )
            return [
                {
                    "ordinal": block["ordinal"],
                    "bounds": churro_response.block_page_bounds(block, page_size=page_size),
                    "bounds_source": "native",
                    "span": dict(span),
                }
                for block, span in zip(parsed["blocks"], parsed["spans"], strict=True)
            ]
    return _presented_echo(presentation)


def _presented_echo(presentation: dict[str, Any]) -> list[dict[str, Any]]:
    """The no-layout fallback: the presentation restated, and named as restated."""
    return [
        {
            "ordinal": 0,
            "bounds": dict(presentation["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]
