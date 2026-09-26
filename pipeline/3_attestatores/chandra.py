"""Chandra's page-witness adapter: the vendor's own preprocessing, prompt,
message shape and output grammar, adopted verbatim and pinned by digest. The
grammar reader lives in `common/chandra_layout.py`, shared with the
Designator's structure pass, so the two readers of one page cannot disagree
about what a `data-bbox` means.

Two answer shapes exist, only one a grammar: the live answer is HTML, read
under parser name `html`. The committed fixture's synthetic rows still declare
a JSON placeholder, `fixture-chandra-response.v1`, told apart by its own
`schema` member (which the vendor grammar has no field for) and, at the
retention seam, by parser name -- a served chair may never be retained under
`json` (`feeding.retain_model_view`), so the placeholder cannot become a live
reading by accident.

Provenance: `datalab-to/chandra` at commit
`d4f7467435aa4137d9539f000ddf0b7ced3eb43f` (`chandra-ocr` 0.2.0, Apache-2.0)
for the prompt bytes, the resize rule and the layout grammar;
`datalab-to/chandra-ocr-2` at `af93b47dba1b47b6640c86ccf487ed2260ab9a09` for
the weights. No vendor package is installed; every carried byte is re-checked
against the vendor by `common/test_vendor_parity.py`.
"""

from __future__ import annotations

import json
import math
from types import MappingProxyType
from typing import Any, Final, Mapping

import feeding

from common import chandra_layout
from common.chandra_presentation import presented_transform, render_page
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR
from common.exemplar_boundary import sealed_page_bytes
from common.imaging import dimensions
from common.native_witness import validate_presented

QUANTIZATION_RULE = "chandra.v1.floor-min-ceil-max.sealed-page-pixels"
FIXTURE_RESPONSE_SCHEMA = "fixture-chandra-response.v1"

# Re-exported so the fixture placeholder's reader below applies the same
# finite intake as the grammar that owns these ceilings.
MAX_RESPONSE_BYTES: Final = chandra_layout.MAX_RESPONSE_BYTES
MAX_LAYOUT_BLOCKS: Final = chandra_layout.MAX_LAYOUT_BLOCKS

#: Declared from the grammar, not assumed. Its answer is labelled blocks each
#: carrying a `data-bbox`, so it expresses layout; it has no confidence
#: attribute or doubt marker anywhere, so it cannot express uncertainty.
FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": False, "can_express_layout": True}
)

# The instruction the committed fixture's synthetic Chandra responses were
# declared against; recorded by the fixture posture only, frozen rather than
# shared with `prompt` because the fixture never asks anything.
FIXTURE_PROMPT: Final[dict[str, str]] = {
    "instruction": "Transcribe this complete page and report layout blocks in reading order."
}


def prompt() -> dict[str, str]:
    """The vendor's own layout prompt, as the one `user` turn it is sent in,
    with no system message -- the shape `chandra/model/vllm.py` builds.
    """

    return {"user": chandra_layout.OCR_LAYOUT_PROMPT}


def vendor_identity() -> dict[str, Any]:
    """Which vendor commit the prompt bytes beside a reading were taken from.

    Both carried strings are named, not only the sent prompt: `PROMPT_ENDING`
    is interpolated into `OCR_LAYOUT_PROMPT`, so digesting only the composite
    could not say which half of it moved.
    """

    return {
        "repository": chandra_layout.VENDOR_REPOSITORY,
        "sha": chandra_layout.VENDOR_COMMIT,
        "carried_strings": {
            "OCR_LAYOUT_PROMPT": chandra_layout.OCR_LAYOUT_PROMPT_SHA256,
            "PROMPT_ENDING": chandra_layout.PROMPT_ENDING_SHA256,
        },
    }


def parse_layout(raw_response: Any) -> Any:
    """The vendor's layout answer read whole, or one named refusal.

    A `ParsedLayout` -- blocks, page text, spans and findings -- or a
    `{"parse_outcome": ...}` record from `chandra_layout.PARSE_OUTCOMES`, the
    shape `feeding.retain_model_view` needs to retain findings beside the
    reading. `parse` below reduces this to the two kinds every adapter's
    `parse` has.
    """

    return chandra_layout.parse_layout_html(raw_response)


def parse(raw_response: bytes) -> Any:
    """Return the page text of a vendor layout answer, or a named shape outcome.

    Same two return kinds as `churro.parse`. Never repairs or reorders an
    answer: a block whose geometry could not be resolved is still a block, and
    that fact is a finding on the capture rather than a substituted rectangle
    (principle 2).
    """

    parsed = parse_layout(raw_response)
    if chandra_layout.is_refusal(parsed):
        return {"parse_outcome": parsed["parse_outcome"]}
    return parsed["page_text"]


def declares_fixture_placeholder(raw_response: Any) -> bool:
    """Whether these bytes are the committed fixture's own JSON placeholder.

    A shape question, not a choice among readings (principle 1): the vendor
    grammar is HTML with no `schema` member anywhere, so a body declaring
    `FIXTURE_RESPONSE_SCHEMA` cannot be a vendor answer too.
    """

    decoded, problem = _decode(raw_response)
    return (
        problem is None
        and isinstance(decoded, dict)
        and decoded.get("schema") == FIXTURE_RESPONSE_SCHEMA
    )


def parse_fixture_placeholder(raw_response: bytes) -> Any:
    """The committed fixture's placeholder, read exactly as it always was.

    Retained history, never the live grammar: a served chair is never retained
    under this parser (`feeding.retain_model_view` refuses the pair), so a live
    answer in this shape lands as a named surprise rather than an unverified
    reading (principle 8).
    """

    decoded, problem = _decode(raw_response)
    if problem is not None:
        return {"parse_outcome": problem}
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
    if len(blocks) > MAX_LAYOUT_BLOCKS:
        return {"parse_outcome": "too-many-layout-blocks"}
    if any(not isinstance(block, dict) or "bbox" not in block for block in blocks):
        return {"parse_outcome": "malformed-block-list"}
    if any(_quantize_box(block["bbox"]) is None for block in blocks):
        return {"parse_outcome": "malformed-block-geometry"}
    return text


def retain(
    context: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain Chandra's response under its own registry identity only.

    Accepts no `adapter` argument to pin: forwarding one would let code that
    had resolved `chandra.v1` file the response under another chair's model
    boundary (principle 6). Churro's and DAI's wrappers pin their names the
    same way.
    """

    return feeding.retain_model_view(
        context,
        adapter="chandra.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Size a whole page the way Chandra's own pipeline sizes it, and record it.

    Adapted from `chandra/model/util.py::scale_to_fit`.
    Converts to RGB before resizing, matching the vendor's own load order:
    on `LA`/`RGBA` pages the two orders are not the same pixels (Pillow's
    resampler treats an alpha band differently), so converting afterwards
    would record a departure from what the vendor actually did.
    `native_witness.py::validate_presented_page_binding` replays these same
    steps against the sealed page and refuses a digest mismatch.

    Only a whole-page presentation is resized and recorded; a `region`
    presentation (an act view of this page witness) is returned unchanged,
    since no chair was ever shown those pixels. Unlike DAI's crop step, an
    identity-sized target still records the vendor operation here, because
    `chandra-scale-to-fit.v1` names the vendor function -- which runs on every
    call, sometimes returning the size it was given -- not a resampler that
    may or may not have run.
    """

    validate_presented(presentation)
    if presentation["kind"] != "page":
        return presentation
    transform = presentation["transform"]
    page_id = transform["source_page_id"]
    page_bytes = sealed_page_bytes(
        context.tree,
        context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id)),
        what="Chandra",
    )
    # Keep bounds failures as SchemaRefusals, not crop_png's bare ValueError.
    validate_presented(presentation, page_size=dimensions(page_bytes))
    bounds = dict(transform["bounds"])
    try:
        # Shared so this chair and the Designator's own Chandra call agree.
        model_image, target = render_page(page_bytes, bounds)
    except ValueError as error:
        # Name load_image's conversion failure here rather than raise a bare error.
        raise SchemaRefusal(
            f"Chandra's presented page cannot be converted to RGB, which the vendor's own "
            f"loader performs on every image before scale_to_fit sees it: {error}"
        ) from error
    published = context.retain(model_image)
    return {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": published["relative_path"],
        "image_sha256": published["sha256"],
        "transform": presented_transform(
            page_id,
            transform["source_page_ordinal"],
            bounds,
            target,
        ),
    }


def observe(
    presentation: dict[str, Any],
    native_payload: Any,
    *,
    page_size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Derive Chandra's page-pixel geometry from its retained raw response.

    A layout answer's `data-bbox` values are normalized 0-1000 against the
    *sealed page*, the vendor's own denominator, so `page_size` is required and
    a body carrying geometry is refused without it rather than handed
    rectangles in the wrong space. Each entry's span indexes the page text
    `parse` returns for the same bytes.

    A block with no resolvable `data-bbox`, and a `Blank-Page` block, report no
    rectangle: the block, its text and the finding that named its geometry
    unresolved stay on the capture, never a substituted box. Ordinals here are
    dense over the entries that carry geometry, since the block a rectangle
    came from is still recoverable through its span.

    An answer with no resolvable geometry at all derives nothing; the page
    record then carries the presentation echo `run.py` gives every such page,
    excluded from routing and coverage by its `bounds_source`.

    The committed fixture's placeholder is recognized by its own declared
    schema and needs no page size, since its boxes are already page pixels.
    """

    validate_presented(presentation)
    if declares_fixture_placeholder(native_payload):
        return _placeholder_observed(native_payload)
    parsed = parse_layout(native_payload)
    if chandra_layout.is_refusal(parsed):
        return []
    located = [
        (block, span)
        for block, span in zip(parsed["blocks"], parsed["spans"], strict=True)
        if not block["blank_page"] and block["bbox_1000"] is not None
    ]
    if not located:
        return []
    if page_size is None:
        raise SchemaRefusal(
            "a Chandra layout answer carries block geometry normalized against the sealed "
            "page, which converts to page pixels only against that page's own size; "
            "pass page_size"
        )
    return [
        {
            "ordinal": ordinal,
            "bounds": chandra_layout.block_page_bounds(block, page_size=page_size),
            "bounds_source": "native",
            "span": dict(span),
        }
        for ordinal, (block, span) in enumerate(located)
    ]


def _placeholder_observed(native_payload: Any) -> list[dict[str, Any]]:
    """The committed fixture placeholder's own geometry, unchanged.

    Page-pixel float boxes quantized by `QUANTIZATION_RULE`. One malformed box
    yields no geometry for the whole body, matching `parse_fixture_placeholder`.
    """

    decoded, _ = _decode(native_payload)
    blocks = decoded.get("blocks") if isinstance(decoded, dict) else None
    if not isinstance(blocks, list) or len(blocks) > MAX_LAYOUT_BLOCKS:
        return []
    observed: list[dict[str, Any]] = []
    for block in blocks:
        bounds = _quantize_box(block.get("bbox") if isinstance(block, dict) else None)
        if bounds is None:
            return []
        observed.append(
            {"ordinal": len(observed), "bounds": bounds, "bounds_source": "native", "span": None}
        )
    return observed


def _decode(raw_response: Any) -> tuple[Any | None, str | None]:
    """Decode bounded raw JSON into a value or one closed parse outcome."""
    if not isinstance(raw_response, (bytes, bytearray)):
        return None, "raw-response-not-bytes"
    if len(raw_response) > MAX_RESPONSE_BYTES:
        return None, "response-too-large"
    try:
        return json.loads(bytes(raw_response).decode("utf-8")), None
    except RecursionError:
        # A deeply nested but valid value; still one bad witness response.
        return None, "excessive-json-nesting"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid-json"
    except ValueError:
        # A huge integer literal makes the scanner's int() raise ValueError,
        # not JSONDecodeError; keep it inside the named parse-outcome boundary.
        return None, "invalid-json"


def _quantize_box(value: Any) -> dict[str, int] | None:
    """Apply the declared min-floor/max-ceil rule, never Python coercion."""
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except OverflowError:
        # A coordinate too large to convert to float is malformed geometry,
        # not an interpreter error escaping the named parse boundary.
        return None
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        return None
    left, top, right, bottom = math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)
    if right <= left or bottom <= top or left < 0 or top < 0:
        return None
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}
