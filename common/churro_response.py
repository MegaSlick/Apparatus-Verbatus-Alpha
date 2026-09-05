"""The Churro page witness's closed response contract (unit 12).

Churro's model card -- ``stanford-oval/churro-3B`` at revision
``ca2150ea465d5a3d67818c50e234b9422619c75d``, named in
``config/models-real.toml`` -- documents a 3B open-weight VLM for historical
document transcription and **no output format at all**: no layout, no bounding
boxes, no reading-order structure, no tag or JSON schema, no example response
body. The one wire shape this repository can point at is the trained
``<output>`` envelope carried verbatim in ``feeding.churro_prompt`` from the
Apache-2.0 release, and that envelope asks for reading order and layout *of the
text*, never for coordinates.

So there is no native layout channel to parse, and writing one blind would be
invention. This is instead the shape *this repository asks for*
(``feeding.churro_layout_prompt``), exactly as ``chandra_response.py`` is the
shape ``chandra.prompt`` asks for and ``common/structure_answer.py`` is the
shape the Designator's structure pass asks for. All three are declared shapes;
none is vendor-measured. A body in any other shape is refused by name with its
bytes already retained, so the first real Churro response either validates this
contract or arrives as a named surprise (GOVERNANCE 10).

**What is accepted, and only this:**

    {"schema": "verbatus-churro-page-response.v1",
     "blocks": [{"box_1000": [x0, y0, x1, y1], "text": "..."}, ...]}

One form, not Chandra's two. Chandra's text-only form exists for a model that
can transcribe a page but cannot place its blocks; Churro's *trained*
``<output>`` envelope already is that form, it stays fully legal on the live
path (``common/native_witness.py::validate_churro_xml``), and spelling the same
fact a second way here would be two records for one thing. ``blocks`` may be
empty: the page holds no text. Any other key, at the top level or inside a
block, is ``unverified-response-schema`` -- nothing outside the declared shape
is read as though it were understood. Nothing here repairs, reorders, trims or
defaults a malformed answer (GOVERNANCE 7); the first malformed block refuses
the whole response.

**Why this module lives in ``common/`` and not beside the adapter.** Its
sibling ``pipeline/3_attestatores/chandra_response.py`` sits in the stage that
owns Chandra, and can, because nothing outside that stage re-derives a Chandra
parse. Churro's is re-derived by three stages:
``native_witness.verify_native_capture_bytes`` re-runs
``derive_churro_capture`` over the retained blob at retention, at the
Perlector's ``verify_native_capture_blob`` and at the Recensor's. Those two
readers cannot import an Attestatores module -- ``pipeline/README.md``'s stage
boundary, pinned by ``pipeline/test_stage_import_boundaries.py`` -- so a parser
under ``3_attestatores/`` would leave both of them re-deriving a
``parser="churro"`` record through a branch they cannot reach. That is a quiet
wrong answer in the readers rather than a refusal, which GOVERNANCE 2 does not
permit. The contract is still Churro's alone and shared with no other chair:
what moved is the directory, not the ownership.

**What is shared with the Chandra contract, and why it is imported.** The
bounded JSON decode with its duplicate-member guard, the ``box_1000`` check and
its quantization, and the page-text join all come from
``common/structure_answer.py`` as public names -- ``decode_json_body``,
``validate_box_1000``, ``join_delivered_texts``, ``to_page_bounds``. Two chairs
reading one page in one normalized coordinate space must agree about what a
legal box is and where a block's text sits, or one chair's reading would land
in geometry the other's page text does not have. What stays here is what is
genuinely this contract's own: its schema name, its outcome names, and its
single block-list form.

**Geometry.** ``box_1000`` entries are finite JSON numbers in ``[0, 1000]`` with
``x1 > x0`` and ``y1 > y0``. Normalized coordinates, not page pixels, so the
inference engine's internal resize (``min_pixels``/``max_pixels``) cannot
corrupt them. A box that passes ``validate_box_1000`` is quantized by that
function's low-edges-floor / far-edges-ceil rule and converted to sealed-page
pixels by ``to_page_bounds``, the one conversion the Designator and Chandra
already share. That conversion clamps to the page, so a normalized box can
never overshoot the sealed page.

**Page text and spans.** ``page_text`` is ``join_delivered_texts``: a newline
between delivered (non-empty) block texts and nowhere else. ``spans`` locates
every block's ``[start, end)`` in that string, an empty block as a zero-width
span where its text would have sat.
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

PAGE_RESPONSE_SCHEMA: Final = "verbatus-churro-page-response.v1"
# Churro's parse ceiling, and the reason it is declared here rather than copied
# from `chandra_response.MAX_RESPONSE_BYTES` (16 MiB). Churro already has a byte
# ceiling: `native_witness.validate_churro_xml` and `derive_churro_capture` both
# refuse past 4 MiB, and `derive_churro_capture` refuses *before* either parser
# or the repetition detector runs. A JSON door at a different number would mean
# a body too large for one of this chair's two legal shapes and admissible under
# the other, with the capture's own oversize record already written. One chair,
# one intake bound. `native_witness.CHURRO_MAX_RESPONSE_BYTES` is this name, so
# the two cannot drift.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
# Chandra's ceiling, adopted deliberately. It is a chosen operational bound, not
# a claim about either model's behaviour: ten thousand layout blocks on one page
# leaves ample headroom while preventing one compact response from expanding
# into an unbounded list of derived geometry records. There is no reason for the
# two page witnesses to differ here, and a different number would read as a
# measurement of Churro that nobody made.
MAX_BLOCKS: Final = 10_000

PARSE_OUTCOMES: Final = frozenset(
    {
        "raw-response-not-bytes",
        "response-too-large",
        "invalid-json",
        "excessive-json-nesting",
        "top-level-not-object",
        "unverified-response-schema",
        "missing-block-list",
        "too-many-blocks",
        "malformed-block",
        "malformed-block-geometry",
        "malformed-block-text",
    }
)

_TOP_LEVEL_FIELDS: Final = frozenset({"schema", "blocks"})
_BLOCK_FIELDS: Final = frozenset({"box_1000", "text"})


class ParsedBlock(TypedDict):
    ordinal: int
    box_1000: list[int]
    text: str


class ParsedPage(TypedDict):
    schema: str
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
    """The page witness's answer, validated whole -- or one named refusal.

    `blocks` absent and `blocks` present as something other than a list are one
    outcome, `missing-block-list`: in both the declared block list is not there
    to read, and the name says exactly that. `malformed-block` is kept for a
    list that exists and holds something this contract cannot read as a block,
    which is a different fact about a different part of the body.
    """
    decoded, problem = decode_json_body(raw, max_bytes=MAX_RESPONSE_BYTES)
    if problem is not None:
        return _refuse(problem)
    if not isinstance(decoded, dict):
        return _refuse("top-level-not-object")
    if set(decoded) - _TOP_LEVEL_FIELDS:
        return _refuse("unverified-response-schema")
    if decoded.get("schema") != PAGE_RESPONSE_SCHEMA:
        return _refuse("unverified-response-schema")
    blocks_raw = decoded.get("blocks")
    if not isinstance(blocks_raw, list):
        return _refuse("missing-block-list")
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
        "blocks": blocks,
        "page_text": page_text,
        "spans": spans,
    }


def is_refusal(parsed: Any) -> bool:
    """Whether `parse` returned a named refusal rather than a page."""
    return isinstance(parsed, dict) and set(parsed) == {"parse_outcome"}


def declares_wire_contract(decoded: Any) -> bool:
    """Whether a decoded JSON value claims this contract's schema.

    The dispatch question `native_witness` and `churro.observe` both ask, in one
    place: a JSON object whose `schema` is this contract's name is answered
    under this contract, and every other body -- including a JSON object with
    another schema or none -- is not. Kept here so the two callers cannot come
    to disagree about which bodies this module owns.
    """
    return isinstance(decoded, dict) and decoded.get("schema") == PAGE_RESPONSE_SCHEMA


def block_page_bounds(block: ParsedBlock, *, page_size: tuple[int, int]) -> Bounds:
    """One quantized block's sealed-page pixel rectangle, by the Designator's own conversion."""
    page_w, page_h = page_size
    return to_page_bounds(block["box_1000"], page_w, page_h)
