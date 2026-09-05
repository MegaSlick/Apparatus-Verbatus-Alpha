"""The Chandra page witness's closed response contract (unit 11).

The vendor publishes no response specimen, so this is not a parser for
Chandra's native output mode -- writing one blind would be invention, and a
wrong guess is indistinguishable from a wrong model. It is the shape *this
repository asks for* (`chandra.prompt`), exactly as the Designator's structure
pass asks its own Chandra call for `verbatus-structure-answer.v1`
(`common/structure_answer.py`). Both are declared shapes; neither is
vendor-measured. A body in any other shape is refused by name with its bytes
already retained, so the first real response either validates this contract or
arrives as a named surprise (GOVERNANCE 10).

**What is accepted, and only this:**

    {"schema": "verbatus-chandra-page-response.v1",
     "blocks": [{"box_1000": [x0, y0, x1, y1], "text": "..."}, ...]}

    {"schema": "verbatus-chandra-page-response.v1", "text": "..."}

The first form is text per layout block with geometry; the second is page text
with no geometry at all, for a model that can transcribe the page but not place
its blocks. Exactly one of the two is present. `blocks` may be empty (the page
holds no text). Any other key, at the top level or inside a block, is
`unverified-response-schema`: nothing outside the declared shape is read as
though it were understood. Nothing here repairs, reorders, trims, or defaults a
malformed answer (GOVERNANCE 7); the first malformed block refuses the whole
response.

**What is shared, and why it is imported rather than restated.** The bounded
JSON decode with its duplicate-member guard, the `box_1000` check and its
quantization, and the page-text join all come from
`common/structure_answer.py` as public names -- `decode_json_body`,
`unique_json_object`, `validate_box_1000`, `join_delivered_texts`. This module
carried its own copy of each until CodeRabbit round 1 (T6) named the
duplication. The two contracts are separate readings of one page by one chair,
and a rule that drifted between the copies would mean one reading refusing a
body the other accepted, or placing spans at offsets the other's page text
does not have. What stays here is what is genuinely this contract's own: its
outcome names, its two page forms, and its block schema.

**Geometry.** `box_1000` entries are JSON numbers in `[0, 1000]`, finite, with
`x1 > x0` and `y1 > y0`. Normalized coordinates, not page pixels, so the
inference engine's internal resize (`min_pixels`/`max_pixels`) cannot corrupt
them -- the same tripwire `pipeline/2_designator/structure_prompt.py` names.
A box that passes `validate_box_1000` is quantized by that function's
low-edges-floor / far-edges-ceil rule and converted to sealed-page pixels by
`common.structure_answer.to_page_bounds`, the one conversion the Designator's
own Chandra reading uses, so the two Chandra readings of one page share one
page-pixel mapping. That conversion clamps to the page, so a normalized box
can never overshoot the sealed page.

**Page text and spans.** `page_text` is `common/structure_answer.py`'s own
join rule -- `join_delivered_texts`, called here over the block texts: a
newline between delivered (non-empty) block texts and nowhere else. `spans`
locates every block's `[start, end)` in that string, empty blocks as a
zero-width span where their text would have sat. Those spans are what the
Attestatores publish as each observed box's span into the retained page text,
and what the derived alignment anchor is built from.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from common.imaging import Bounds
from common.structure_answer import (
    decode_json_body,
    join_delivered_texts,
    to_page_bounds,
    validate_box_1000,
)

PAGE_RESPONSE_SCHEMA: Final = "verbatus-chandra-page-response.v1"
# The same operational ceilings `chandra.py` applies to every body that crosses
# the native model boundary: the byte bound matches the repository's RunPod
# response ceiling, the block bound is a chosen ceiling, not a claim about
# Chandra's behaviour.
MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
MAX_BLOCKS: Final = 10_000

PARSE_OUTCOMES: Final = frozenset(
    {
        "raw-response-not-bytes",
        "response-too-large",
        "invalid-json",
        "excessive-json-nesting",
        "top-level-not-object",
        "unverified-response-schema",
        "missing-page-form",
        "conflicting-page-forms",
        "malformed-page-text",
        "too-many-blocks",
        "malformed-block",
        "malformed-block-geometry",
        "malformed-block-text",
    }
)

_TOP_LEVEL_FIELDS: Final = frozenset({"schema", "blocks", "text"})
_BLOCK_FIELDS: Final = frozenset({"box_1000", "text"})


class ParsedBlock(TypedDict):
    ordinal: int
    box_1000: list[int]
    text: str


class ParsedPage(TypedDict):
    schema: str
    # `True` for the blocks form, `False` for the page-text form. An empty
    # blocks list is still the blocks form: the model placed nothing because it
    # saw nothing, which is a different fact from declining to place anything.
    geometry: bool
    blocks: list[ParsedBlock]
    page_text: str
    spans: list[dict[str, int]]


def _refuse(outcome: str) -> dict[str, str]:
    """A closed `{"parse_outcome": ...}` record -- never a code outside the set."""
    if outcome not in PARSE_OUTCOMES:
        raise ValueError(f"undeclared parse outcome {outcome!r}")
    return {"parse_outcome": outcome}


def _parse_block(value: Any, ordinal: int) -> ParsedBlock | str:
    """One block, fully resolved -- or the `PARSE_OUTCOMES` code that refuses it."""
    if not isinstance(value, dict):
        return "malformed-block"
    if set(value) - _BLOCK_FIELDS:
        return "unverified-response-schema"
    if set(value) != _BLOCK_FIELDS:
        return "malformed-block"
    box = validate_box_1000(value["box_1000"])
    if box is None:
        return "malformed-block-geometry"
    if not isinstance(value["text"], str):
        return "malformed-block-text"
    return {"ordinal": ordinal, "box_1000": box, "text": value["text"]}


def parse(raw: Any) -> ParsedPage | dict[str, str]:
    """The page witness's answer, validated whole -- or one named refusal."""
    decoded, problem = decode_json_body(raw, max_bytes=MAX_RESPONSE_BYTES)
    if problem is not None:
        return _refuse(problem)
    if not isinstance(decoded, dict):
        return _refuse("top-level-not-object")
    if set(decoded) - _TOP_LEVEL_FIELDS:
        return _refuse("unverified-response-schema")
    if decoded.get("schema") != PAGE_RESPONSE_SCHEMA:
        return _refuse("unverified-response-schema")
    has_blocks, has_text = "blocks" in decoded, "text" in decoded
    if has_blocks and has_text:
        return _refuse("conflicting-page-forms")
    if not has_blocks and not has_text:
        return _refuse("missing-page-form")
    if has_text:
        text = decoded["text"]
        if not isinstance(text, str):
            return _refuse("malformed-page-text")
        return {
            "schema": PAGE_RESPONSE_SCHEMA,
            "geometry": False,
            "blocks": [],
            "page_text": text,
            "spans": [],
        }
    blocks_raw = decoded["blocks"]
    if not isinstance(blocks_raw, list):
        return _refuse("malformed-block")
    if len(blocks_raw) > MAX_BLOCKS:
        return _refuse("too-many-blocks")
    blocks: list[ParsedBlock] = []
    for ordinal, item in enumerate(blocks_raw):
        result = _parse_block(item, ordinal)
        if isinstance(result, str):
            return _refuse(result)
        blocks.append(result)
    page_text, spans = join_delivered_texts([block["text"] for block in blocks])
    return {
        "schema": PAGE_RESPONSE_SCHEMA,
        "geometry": True,
        "blocks": blocks,
        "page_text": page_text,
        "spans": spans,
    }


def is_refusal(parsed: Any) -> bool:
    """Whether `parse` returned a named refusal rather than a page."""
    return isinstance(parsed, dict) and set(parsed) == {"parse_outcome"}


def block_page_bounds(block: ParsedBlock, *, page_size: tuple[int, int]) -> Bounds:
    """One quantized block's sealed-page pixel rectangle, by the Designator's own conversion."""
    page_w, page_h = page_size
    return to_page_bounds(block["box_1000"], page_w, page_h)
