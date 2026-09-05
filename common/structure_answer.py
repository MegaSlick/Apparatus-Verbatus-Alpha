"""The structure chair's closed answer contract (SPEC_D §1.2).

This lives in `common/` rather than in `pipeline/2_designator/` because two
stages read Chandra on the same page and must land its geometry in one
page-pixel mapping: the Designator publishes this answer, and the
Attestatores' own Chandra witness contract
(`pipeline/3_attestatores/chandra_response.py`) converts its normalized boxes
through `to_page_bounds` below. The two calls are separate readings -- every
witness runs its own full pass and nothing is captured from one call into
another (Tyrel's ruling, 2026-09-02) -- so what they share is the conversion,
never the bytes. A stage may not import another stage's module, so that
conversion has to live where neither owns it.

**What is accepted, and only this:**
`{"schema": "verbatus-structure-answer.v1", "acts": [{"box_1000": [x0,y0,x1,y1],
"text": "...", "label": "..."}]}` -- `label` optional, `acts` may be
empty (the "sees no text" case). Any other key, at the top level or inside an act, is
`unverified-response-schema`: the wire shape is unverified (`chandra.py`'s own
docstring says why -- the vendor publishes no response specimen), so nothing
outside the declared shape is read as though it were understood. Nothing here
repairs, reorders, trims, or defaults a malformed answer (GOVERNANCE 7); a
truncated or malformed response is refused whole, never salvaged in part.

**Geometry.** Box entries are JSON numbers in `[0, 1000]`, finite, with
`x1 > x0` and `y1 > y0` -- else `malformed-act-geometry`. A box that passes
that check is never refused for being a float: it is quantized by the
declared `QUANTIZATION_RULE`, low edges floored and far edges ceiled, the same
"a rectangle can only ever grow" rule `pipeline/2_designator/geometry.py`
names and `pipeline/3_attestatores/chandra.py::_quantize_box` already applies
to Chandra's other output shape. `to_page_bounds` then converts the quantized
`box_1000` to page pixels using `pipeline/2_designator/geometry_layer.py`'s
`chandra_layout` conversion, re-derived here byte-for-byte (a Designator-side
test in `pipeline/2_designator/test_structure_prompt.py` asserts equality
against that function's own `aabb` over a grid of boxes, edges 0 and 1000
included, because `geometry_layer` lives in `pipeline/2_designator/` and this
module may not import it).

**Label, and where it may go.** `label` is the chair's own word for what it
thinks a rectangle is, and this module returns it verbatim: a caller that has
the retained bytes must be able to see what they say. It is still the chair's
*reading* of the ink, though -- a marginal name or an index row is a whole act
in a page of this kind (GLOSSARY, "act"), and a short string is enough to hold
one -- so it is bounded by nothing here and published by no one: the
Designator's record carries `label_digest` and `label_length` and no label,
exactly as it does for `text` (`pipeline/2_designator/structure_pass.py`).
The Designator never establishes the authoritative transcription
(ARCHITECTURE), and a rule enforced only by a length ceiling would be a rule
about how much of the reading may escape rather than whether any of it may.

**Nesting.** JSON decoding is the one place actual recursion could reach this
module, and it is the stdlib's, not this file's: `json.loads` raises
`RecursionError` on a sufficiently deep value (the same defect class
`common/corpus_register.py` already names as "nested too deeply", pinned there
at depth 1,000,000 for the same platform-dependent-C-recursion-limit reason
this module's own test pins it too). Everything past that point -- the
top-level shape, the act list, each act -- is two flat loops, never a walk
that recurses with the input, so nothing here can itself exhaust the stack.

**Page text.** `page_text` is `pipeline/3_attestatores/run.py::page_join`'s own
rule, restated for one page's acts rather than one chair's attempts:
"[s]eparators are placed only *between* delivered characters" -- an empty
act's text contributes nothing to `page_text`, and the newline that joins two
acts appears only when both a preceding and a following act delivered
characters. `spans` locates every act's `[start, end)` in that string,
including the empty ones, as a zero-width span at the point their (absent)
text would have sat.
"""

from __future__ import annotations

import json
import math
from typing import Any, Final, TypedDict

from common.contracts.canonical import digest_bytes
from common.imaging import Bounds

STRUCTURE_ANSWER_SCHEMA: Final = "verbatus-structure-answer.v1"
QUANTIZATION_RULE: Final = "structure-answer.v1.box1000-floor-low-ceil-far.sealed-page-pixels"
PAGE_TEXT_RULE: Final = "structure-answer.v1.newline-between-delivered-acts"
# chandra.py's own ceiling: the byte bound matches the repository's existing
# RunPod response ceiling, kept here too so every caller across the wire
# boundary obeys the same finite intake.
MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
# chandra.py's own MAX_LAYOUT_BLOCKS, same reason: a chosen operational
# ceiling, not a claim about the structure chair's behaviour.
MAX_ACTS: Final = 10_000
PARSE_OUTCOMES: Final = frozenset(
    {
        "raw-response-not-bytes",
        "response-too-large",
        "invalid-json",
        "excessive-json-nesting",
        "top-level-not-object",
        "unverified-response-schema",
        "missing-act-list",
        "too-many-acts",
        "malformed-act",
        "malformed-act-geometry",
        "malformed-act-text",
    }
)

_TOP_LEVEL_FIELDS: Final = frozenset({"schema", "acts"})
_ACT_FIELDS: Final = frozenset({"box_1000", "text", "label"})
_ACT_REQUIRED_FIELDS: Final = frozenset({"box_1000", "text"})


class ParsedAct(TypedDict):
    ordinal: int
    box_1000: list[int]
    raw_bounds: Bounds
    text: str
    label: str | None


class ParsedAnswer(TypedDict):
    schema: str
    acts: list[ParsedAct]
    page_text: str
    spans: list[dict[str, int]]


def _refuse(outcome: str) -> dict[str, str]:
    """A closed `{"parse_outcome": ...}` record -- never a code outside the set.

    A named error, not a bare `assert`: the check must survive `python -O`
    and fail as a refusal this module can be caught raising, not as an
    `AssertionError` that vanishes under the optimize flag.
    """
    if outcome not in PARSE_OUTCOMES:
        raise ValueError(f"undeclared parse outcome {outcome!r}")
    return {"parse_outcome": outcome}


class DuplicateJsonMember(ValueError):
    """Raised by `unique_json_object` -- caught in `decode_json_body`, never let past it."""


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Materialize one JSON object only when every member name occurs once.

    The stdlib's default resolves a duplicate key last-wins, silently: two
    values for one field and only one survives, with nothing reporting the
    loss. `pipeline/1_exemplar/door.py::_unique_json_object` closes the same
    defect for the triage recipe; this is that same guard for a wire answer.

    Public, and imported by `pipeline/3_attestatores/chandra_response.py`
    rather than copied there: the two closed wire contracts read the same
    normalized answer shape from the same chair on the same page, and a
    duplicate-member rule that drifted between them would mean one reading
    refusing a body the other accepted (CodeRabbit round 1, T6).
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonMember(key)
        result[key] = value
    return result


def decode_json_body(raw: Any, *, max_bytes: int) -> tuple[Any, str | None]:
    """Bounded bytes to a JSON value, or one named outcome. Never recurses itself.

    `max_bytes` is the caller's own ceiling rather than this module's constant,
    so each contract keeps -- and each contract's tests can still move -- the
    finite intake it declares. Every outcome named here is in both callers'
    `PARSE_OUTCOMES`.
    """
    if not isinstance(raw, bytes):
        return None, "raw-response-not-bytes"
    if len(raw) > max_bytes:
        return None, "response-too-large"
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_object), None
    except RecursionError:
        # The stdlib scanner raises this separately from JSONDecodeError for a
        # sufficiently deep but otherwise valid value -- one bad response, not
        # permission to crash the stage that reads it.
        return None, "excessive-json-nesting"
    except DuplicateJsonMember:
        # Two values for one field is not malformed JSON -- it decodes fine
        # under the stdlib's own last-wins default. It is an answer this
        # module does not understand: the same code an unknown key gets.
        return None, "unverified-response-schema"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid-json"


def _quantize(x0: float, y0: float, x1: float, y1: float) -> list[int]:
    """`QUANTIZATION_RULE`: low edges floor, far edges ceil. Never refuses."""
    return [math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)]


def validate_box_1000(value: Any) -> list[int] | None:
    """The raw `box_1000`, quantized -- or `None` if the raw values are malformed.

    `None` means malformed geometry, and the *caller* names the outcome
    (`malformed-act-geometry` here, `malformed-block-geometry` for the page
    witness): this function names none itself so it stays a pure geometry
    check, the same split `chandra.py::_quantize_box` keeps.

    Public and shared with `pipeline/3_attestatores/chandra_response.py`
    (CodeRabbit round 1, T6). One normalized coordinate space, one bound, one
    quantization rule: if the two contracts disagreed about what a legal
    `box_1000` is, the Designator's and the page witness's readings of the same
    page would land in different geometry.
    """
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except OverflowError:
        # JSON integers have arbitrary precision in Python; a coordinate too
        # large for float quantization is malformed geometry, not a crash.
        return None
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        return None
    if not (0 <= x0 <= 1000 and 0 <= y0 <= 1000 and 0 <= x1 <= 1000 and 0 <= y1 <= 1000):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return _quantize(x0, y0, x1, y1)


def to_page_bounds(box_1000: list[int], page_w: int, page_h: int) -> Bounds:
    """`geometry_layer.chandra_layout`'s exact conversion, re-derived.

    `chandra_layout` builds the four corner points
    ``x0*page_w//1000`` .. ``min(page_w-1, (x1*page_w+999)//1000 - 1)`` (and the
    matching pair on the y axis), then collapses them to an enclosing rectangle
    via `enclosing_aabb`. Because the box is axis-aligned, that rectangle is
    exactly ``{"x": left, "y": top, "w": right-left+1, "h": bottom-top+1}`` for
    the same `left`/`top`/`right`/`bottom` -- which is what this computes
    directly, without building intermediate points `common/` has no reason to
    depend on `geometry_layer` for. A Designator-side test asserts the two stay
    equal wherever `chandra_layout` actually returns a proposal: a box whose
    page-pixel conversion is one pixel wide or tall is ordinary `Bounds` here
    and to `geometry.validate_bounds`, but `chandra_layout` refuses it before
    it produces an `aabb` at all (its four corner points collapse to fewer
    than three distinct ones), so that hairline case sits outside the domain
    where the two converters are comparable -- and is pinned separately,
    rather than dropped silently, alongside the grid test.
    """
    x0, y0, x1, y1 = box_1000
    left = x0 * page_w // 1000
    top = y0 * page_h // 1000
    right = min(page_w - 1, (x1 * page_w + 999) // 1000 - 1)
    bottom = min(page_h - 1, (y1 * page_h + 999) // 1000 - 1)
    return {"x": left, "y": top, "w": right - left + 1, "h": bottom - top + 1}


def text_digest(text: str) -> str:
    """The digest that lets a reader prove it derived the same text from the same bytes."""
    return digest_bytes(text.encode("utf-8"))


def _parse_act(value: Any, ordinal: int, page_w: int, page_h: int) -> ParsedAct | str:
    """One act, fully resolved -- or the `PARSE_OUTCOME` code that refuses it."""
    if not isinstance(value, dict):
        return "malformed-act"
    extra = set(value) - _ACT_FIELDS
    if extra:
        return "unverified-response-schema"
    missing = _ACT_REQUIRED_FIELDS - set(value)
    if missing:
        return "malformed-act"
    label = value.get("label")
    # `label` is declared an optional string (SPEC_D §1.2): present-as-string,
    # or absent. An explicit JSON `null` is neither -- accepting it as a
    # synonym for absent would be this module quietly normalizing a value the
    # declared shape does not contain (GOVERNANCE 7), so it refuses like any
    # other non-string label.
    if "label" in value and not isinstance(label, str):
        return "malformed-act"
    box = validate_box_1000(value["box_1000"])
    if box is None:
        return "malformed-act-geometry"
    text = value["text"]
    if not isinstance(text, str):
        return "malformed-act-text"
    return {
        "ordinal": ordinal,
        "box_1000": box,
        "raw_bounds": to_page_bounds(box, page_w, page_h),
        "text": text,
        "label": label,
    }


def join_delivered_texts(texts: list[str]) -> tuple[str, list[dict[str, int]]]:
    """`PAGE_TEXT_RULE`: newline only between delivered (non-empty) texts.

    Takes plain strings rather than this module's own `ParsedAct` so the page
    witness's blocks join by the same rule, from the same code
    (`pipeline/3_attestatores/chandra_response.py`, CodeRabbit round 1, T6).
    Both readings publish spans into their retained page text, and a join rule
    that drifted would put one reading's spans at offsets the other's page text
    does not have.
    """
    parts: list[str] = []
    spans: list[dict[str, int]] = []
    cursor = 0
    wrote_any = False
    for text in texts:
        if text == "":
            spans.append({"start": cursor, "end": cursor})
            continue
        if wrote_any:
            parts.append("\n")
            cursor += 1
        start = cursor
        parts.append(text)
        cursor += len(text)
        spans.append({"start": start, "end": cursor})
        wrote_any = True
    return "".join(parts), spans


def parse(raw: bytes, *, page_w: int, page_h: int) -> ParsedAnswer | dict[str, str]:
    """The structure chair's answer, validated whole -- or one named refusal.

    Accepts only the closed `{"schema", "acts"}` shape this module's docstring
    describes. Nothing is repaired, reordered, trimmed, or defaulted: the first
    malformed act refuses the whole response (GOVERNANCE 7).
    """
    decoded, problem = decode_json_body(raw, max_bytes=MAX_RESPONSE_BYTES)
    if problem is not None:
        return _refuse(problem)
    if not isinstance(decoded, dict):
        return _refuse("top-level-not-object")
    extra = set(decoded) - _TOP_LEVEL_FIELDS
    if extra:
        return _refuse("unverified-response-schema")
    if decoded.get("schema") != STRUCTURE_ANSWER_SCHEMA:
        return _refuse("unverified-response-schema")
    acts_raw = decoded.get("acts")
    if not isinstance(acts_raw, list):
        return _refuse("missing-act-list")
    if len(acts_raw) > MAX_ACTS:
        return _refuse("too-many-acts")

    acts: list[ParsedAct] = []
    for ordinal, item in enumerate(acts_raw):
        result = _parse_act(item, ordinal, page_w, page_h)
        if isinstance(result, str):
            return _refuse(result)
        acts.append(result)

    page_text, spans = join_delivered_texts([act["text"] for act in acts])
    return {
        "schema": STRUCTURE_ANSWER_SCHEMA,
        "acts": acts,
        "page_text": page_text,
        "spans": spans,
    }
