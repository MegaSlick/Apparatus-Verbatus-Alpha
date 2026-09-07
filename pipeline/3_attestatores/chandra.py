"""Chandra's page-witness adapter, running the vendor's own system.

Tonight's ruling (Tyrel, 2026-09-06) is that each witness runs as its
developers intended: the vendor's preprocessing, prompt bytes, message shape,
generation values and output grammar are adopted verbatim and pinned by digest,
and the vendor's harness is not. This module is where that ruling reaches the
Chandra chair. The grammar itself -- the carried prompt bytes and the reader
for the answer they ask for -- lives in `common/chandra_layout.py`, because the
Designator's structure pass reads the same grammar for its own purpose and two
readers of one page that disagreed about what a `data-bbox` means would be two
page-pixel mappings for one chair.

**What this module retired, and why the old premise was false.** Until now
`prompt()` asked the served chair for a closed JSON shape of this repository's
own invention (`_LIVE_INSTRUCTION`, parsed by `chandra_response.py`), on the
stated premise that "the vendor publishes no output specimen to carry ...
there is therefore nothing to license and no borrowed line to name." That was
true of the *model card* and false of the vendor's own repository, which ships
both the prompt every caller sends (`chandra/prompts.py::OCR_LAYOUT_PROMPT`)
and the parser for the answer it asks for (`chandra/output.py::parse_layout`)
under Apache-2.0. Both are now carried, cited and digest-pinned in
`common/chandra_layout.py`, and the instruction, its wire contract and its
module are gone. The churn is deliberate and is recorded as such in the vendor
systems design.

**The four vendor pieces this adapter is responsible for.**

* *Preprocess.* `present` reproduces `chandra/model/util.py::scale_to_fit` --
  through `common/imaging_ports.py::scale_to_fit_chandra`, which is the port,
  and `common/imaging.py`, which moves the pixels -- and records it as an
  `adapter-crop` under the vendor's own operation name
  `chandra-scale-to-fit.v1`, so the exact image the chair saw re-derives from
  the sealed Exemplar (ARCHITECTURE invariant 3).
* *Prompt.* `prompt` sends `chandra_layout.OCR_LAYOUT_PROMPT`'s carried bytes
  as one `user` turn with no system message, which is the shape
  `chandra/model/vllm.py` builds.
* *Parse.* `parse_layout` is `chandra_layout.parse_layout_html`, the
  re-expression of the vendor's own `parse_layout` over the standard library.
* *Observe.* `observe` converts each block's `data-bbox` to sealed-page pixels
  through `chandra_layout.block_page_bounds`, whose denominator is the sealed
  page and not the resized view -- because the vendor's own denominator is the
  same one (`InferenceManager` runs `parse_chunks` against the original image).

**Two answer shapes, and only one of them is a grammar.** The live grammar is
HTML, read under the parser name `html`. The committed fixture's synthetic rows
still declare `fixture-chandra-response.v1`, a JSON placeholder this repository
invented for a fixture that asks nothing of anybody, and those pinned bytes stay
until U16 re-declares the `proof/` rows in the vendor grammars. So the
placeholder keeps its own parser name, `json`, and its own reader
(`parse_fixture_placeholder`) -- **retained as history, never parsed as the live
grammar**. The two are told apart by the answer's own declared `schema` member,
which the vendor grammar has no field for, and the posture is told apart at the
retention seam by the parser name the record is written under: a *served* chair
may not be retained under `json` at all (`feeding.retain_model_view`), so the
placeholder cannot become a live reading by accident. That refusal is the same
one `served=` used to make inside `parse`; it now lives at the seam, where the
parser name that will be written onto the record is what it is checked against.

**Two prompts, deliberately.** `prompt()` is what a served chair is asked.
`FIXTURE_PROMPT` is the instruction the committed fixture's synthetic Chandra
rows were declared against, and it is what the fixture posture records in the
retained model view (`run.py::resolve_attempt`), because that view is sealed
into the fixture's pinned bytes: the fixture never asks anything, so its
recorded prompt is a declaration, and changing what a served chair is asked must
not move a fixture byte.

**Provenance.** `datalab-to/chandra` at commit
`d4f7467435aa4137d9539f000ddf0b7ced3eb43f` (`chandra-ocr` 0.2.0, Apache-2.0)
for the prompt bytes, the resize rule and the layout grammar;
`datalab-to/chandra-ocr-2` at `af93b47dba1b47b6640c86ccf487ed2260ab9a09` for
the weights. No vendor package is installed: every carried byte is cited where
it is carried and re-checked against the vendor by
`common/test_vendor_parity.py`.
"""

from __future__ import annotations

import json
import math
from types import MappingProxyType
from typing import Any, Final, Mapping

import feeding

from common import chandra_layout
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.imaging import crop_png, dimensions, resize_png_lanczos
from common.imaging_ports import scale_to_fit_chandra
from common.native_witness import validate_presented

QUANTIZATION_RULE = "chandra.v1.floor-min-ceil-max.sealed-page-pixels"
FIXTURE_RESPONSE_SCHEMA = "fixture-chandra-response.v1"

#: The vendor preprocessing this adapter records over its own presented pixels.
#: The name is `common/native_witness.py`'s, where the operation is admitted,
#: its rounding rule declared (`grid-28`) and its replay from sealed page bytes
#: implemented; spelled once here so the writer and the vocabulary cannot drift.
PRESENT_OPERATION: Final = "chandra-scale-to-fit.v1"
#: `scale_to_fit` performs no colour conversion of its own. Recorded explicitly
#: rather than omitted: `keep` says a conversion did not run, which an absent
#: field on a record allowed to omit it cannot say.
PRESENT_COLOUR_MODE: Final = "keep"
# One ceiling per fact, declared beside the grammar that also enforces it and
# re-exported here because the fixture placeholder's own reader below has to
# apply the same finite intake to bytes crossing the same boundary.
MAX_RESPONSE_BYTES: Final = chandra_layout.MAX_RESPONSE_BYTES
MAX_LAYOUT_BLOCKS: Final = chandra_layout.MAX_LAYOUT_BLOCKS

#: What Chandra's grammar can carry, declared from the grammar rather than
#: assumed from a blanket default (vendor systems design, Contract boundary:
#: "Chandra false/true"). Its answer is a list of labelled blocks each carrying
#: a `data-bbox`, so it expresses layout; it has no vocabulary for uncertainty
#: at all -- no confidence attribute, no bracket marker, nothing the grammar
#: could carry a doubt in -- so it does not express uncertainty, and saying so
#: is what keeps `dissent.is_comparable` from being asked to compare a doubt
#: this chair had no way to report.
FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": False, "can_express_layout": True}
)

# The instruction the committed fixture's synthetic Chandra responses were
# declared against. Recorded by the fixture posture only; see the module
# docstring for why it is frozen here rather than shared with `prompt`.
FIXTURE_PROMPT: Final[dict[str, str]] = {
    "instruction": "Transcribe this complete page and report layout blocks in reading order."
}


def prompt() -> dict[str, str]:
    """The vendor's own layout prompt, as the one `user` turn it is sent in.

    `chandra/model/vllm.py:64-76` builds a single user message whose content is
    the image block followed by the text prompt, and sends no system message at
    all; `live_witness._page_messages` builds exactly that from this shape. The
    bytes are `chandra_layout.OCR_LAYOUT_PROMPT`, checked against their recorded
    digest at import of that module, so a chair cannot be asked something no
    vendor commit names.
    """

    return {"user": chandra_layout.OCR_LAYOUT_PROMPT}


def vendor_identity() -> dict[str, Any]:
    """Which vendor pin the prompt bytes beside a reading were taken from.

    GOVERNANCE 6 already puts the model's resolved identity on every stored
    reading. This is the other half once the chair runs the vendor's own
    system: the prompt is the vendor's, taken at one commit, and a later
    re-parse under a different pin produces a different reading of the same
    retained response. Returned as fresh, plain containers because it travels
    into a retained record (`common/native_witness.py::validate_vendor_identity`
    closes its shape).

    Both carried strings are named, not only the prompt that is sent:
    `PROMPT_ENDING` is interpolated into `OCR_LAYOUT_PROMPT`, so a record that
    digested only the composite could not say which half of it moved.
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
    `{"parse_outcome": ...}` record from `chandra_layout.PARSE_OUTCOMES`. This
    is the shape `feeding.retain_model_view` needs, because a capture retains
    the findings beside the reading; `parse` below is the same call reduced to
    the two return kinds every adapter's `parse` has.
    """

    return chandra_layout.parse_layout_html(raw_response)


def parse(raw_response: bytes) -> Any:
    """Return the page text of a vendor layout answer, or a named shape outcome.

    The two return kinds `churro.parse` has, for the same reason: a caller that
    already handles one page witness handles this one. Nothing here repairs,
    reorders or defaults an answer -- a block whose geometry could not be
    resolved is still a block, and the fact is a finding on the capture beside
    the retained bytes rather than a substituted rectangle (GOVERNANCE 2).
    """

    parsed = parse_layout(raw_response)
    if chandra_layout.is_refusal(parsed):
        return {"parse_outcome": parsed["parse_outcome"]}
    return parsed["page_text"]


def declares_fixture_placeholder(raw_response: Any) -> bool:
    """Whether these bytes are the committed fixture's own JSON placeholder.

    A shape question, not a choice among readings (hard rule 8): the placeholder
    declares `FIXTURE_RESPONSE_SCHEMA` in a top-level JSON object, and the
    vendor grammar is HTML with no schema member anywhere in it, so no body can
    be both. Used by `observe`, which is handed bytes without being told which
    posture retained them, and by nothing that decides what a reading says.
    """

    decoded, problem = _decode(raw_response)
    return (
        problem is None
        and isinstance(decoded, dict)
        and decoded.get("schema") == FIXTURE_RESPONSE_SCHEMA
    )


def parse_fixture_placeholder(raw_response: bytes) -> Any:
    """The committed fixture's placeholder, read exactly as it always was.

    Retained history, never the live grammar. `proof/skeleton_fixture.toml`'s
    Chandra rows declare `fixture-chandra-response.v1` bodies and their bytes
    are pinned into the fixture's own digests, so this reader stays until U16
    re-declares those rows in the vendor grammar. A *served* chair is never
    retained under this parser at all -- `feeding.retain_model_view` refuses the
    pair -- so a live answer in this shape lands as a named surprise rather than
    as a reading whose shape nobody verified against anything (GOVERNANCE 10).
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

    return feeding.retain_model_view(
        tree,
        adapter="chandra.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Size a whole page the way Chandra's own pipeline sizes it, and record it.

    `chandra/model/util.py::scale_to_fit` runs on every image the vendor's
    inference path sends, and under tonight's ruling it runs here too: the crop
    comes off the sealed Exemplar, `common/imaging_ports.scale_to_fit_chandra`
    decides the target, `common/imaging.resize_png_lanczos` moves the pixels,
    and the result is published as an `adapter-crop` whose transform names the
    vendor operation. That is what makes the exact image the chair saw
    re-derivable from the Exemplar plus the record (ARCHITECTURE invariant 3):
    `common/native_witness.py::validate_presented_page_binding` replays exactly
    these three steps against the sealed page and refuses a digest that does not
    come back.

    **Only a whole-page presentation is resized, and that is the honest line.**
    Chandra is a page witness: the image a chair is ever shown is a page
    (`live_witness.page_chair_request`), and the vendor resize is a fact about
    that image. An act view of a page witness is a compatibility record that
    restates one page reading against one act's Designator crop; no chair was
    ever shown those pixels, and minting a vendor resize recipe over them would
    record a preprocessing step that never ran on an image nobody sent. So a
    `region` presentation is returned exactly as it arrived, as it always was,
    and `witness_adapters.validate_adapter_presentation` re-derives both cases
    apart.

    **An identity-sized target still records the vendor operation**, unlike
    DAI's crop step, which falls back to a plain `crop` when its resize would
    change nothing. The two rules differ because the two operation names mean
    different things: DAI's `crop-resize-preserve-aspect` names a resampler, and
    naming one that never ran would be false, while `chandra-scale-to-fit.v1`
    names the vendor function, which runs on every call and sometimes returns
    the size it was given. `resize_png_lanczos` is still called and still frames
    the output, so no step is claimed that did not happen.
    """

    validate_presented(presentation)
    if presentation["kind"] != "page":
        return presentation
    transform = presentation["transform"]
    page_id = transform["source_page_id"]
    page_bytes = feeding.sealed_page_bytes(context, page_id, what="Chandra")
    # Bounds failures must stay SchemaRefusals so callers can hold the attempt;
    # `crop_png` alone would expose a bare ValueError at this boundary.
    validate_presented(presentation, page_size=dimensions(page_bytes))
    bounds = dict(transform["bounds"])
    source_width, source_height = bounds["w"], bounds["h"]
    target_width, target_height = scale_to_fit_chandra(source_width, source_height)
    model_image = resize_png_lanczos(crop_png(page_bytes, bounds), target_width, target_height)
    digest, published = context.tree.put_blob(ATTESTATORES, model_image)
    return {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": published.relative_path,
        "image_sha256": digest,
        "transform": presented_transform(
            page_id,
            transform["source_page_ordinal"],
            bounds,
            (target_width, target_height),
        ),
    }


def presented_transform(
    page_id: str,
    page_ordinal: int,
    bounds: dict[str, int],
    target: tuple[int, int],
) -> dict[str, Any]:
    """The one transform this adapter writes, so the re-deriver reads it here.

    `witness_adapters.validate_adapter_presentation` has to state what this
    adapter could have produced without running it. Two hand-written copies of
    one recipe agree only for as long as both are edited together, which is the
    failure `_validate_resize_recipe` already names for the vendor's own trim
    loop; there is nothing to gain by repeating it, so the writer and the
    re-deriver call this.
    """

    return {
        "operation": PRESENT_OPERATION,
        "source_page_id": page_id,
        "source_page_ordinal": page_ordinal,
        "bounds": dict(bounds),
        "colour_mode": PRESENT_COLOUR_MODE,
        "resize": {
            "resampler": "pillow-lanczos",
            "dimension_rounding": "grid-28",
            "source_width_px": bounds["w"],
            "source_height_px": bounds["h"],
            "target_width_px": target[0],
            "target_height_px": target[1],
        },
    }


def observe(
    presentation: dict[str, Any],
    native_payload: Any,
    *,
    page_size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Derive Chandra's page-pixel geometry from its retained raw response.

    A layout answer's `data-bbox` values are normalized 0-1000 against the
    *sealed page*, which is the vendor's own denominator, so `page_size` is
    required and a caller that omits it for a body carrying geometry is refused
    rather than handed rectangles in the wrong space. `run.py` passes it at both
    Chandra call sites. Each entry's span indexes the page text `parse` returns
    for the same bytes.

    A block whose `data-bbox` was absent or malformed, and a `Blank-Page` block,
    report no rectangle at all: `chandra_layout.block_page_bounds` returns
    `None` for them and this returns no entry for them. It does not return a
    substituted box, and it does not drop the block -- the block, its text and
    the finding that named its geometry unresolved are all on the capture beside
    these bytes. Ordinals here are dense over the entries that *do* carry
    geometry, because that is what the closed `observed` schema requires
    (`native_witness.validate_observed`); the block a rectangle came from is
    recoverable through its span and through the retained response.

    An answer with no resolvable geometry at all -- a refused parse, a page of
    blocks that all report none -- derives nothing. The page record then carries
    the presentation echo `run.py` gives every page with no reported geometry,
    which routing and coverage exclude by its `bounds_source`, so the shared
    page-edge check is never handed an echo by this adapter.

    The committed fixture's placeholder is recognized by its own declared schema
    and keeps the page-pixel float boxes and the declared quantization rule it
    always had; it needs no page size, because those boxes are already page
    pixels.
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
    yields no geometry for the whole body: `parse_fixture_placeholder` names
    that before any record is written, and this keeps a direct caller that
    bypassed the retention seam equally conservative.
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
        # The stdlib scanner raises this separately from JSONDecodeError for a
        # sufficiently deep but otherwise valid value.  It is still one bad
        # witness response, never permission to crash the whole stage.
        return None, "excessive-json-nesting"
    except (UnicodeDecodeError, json.JSONDecodeError):
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
