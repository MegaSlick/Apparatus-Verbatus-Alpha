"""Each witness's page broken into that witness's own units, re-derived from retained bytes.

The Perlector's page feed shows these units to the reading, and the page
accounting measures every sealed witness by them, shown or hidden; a later
stage recomputing that accounting reads them here too, so there is one reading
of a witness's units.

A unit is `{ordinal, box_px | None, label | None, text}` in the witness's own
order. Each adapter's units are re-derived from the raw response the page
Testimonium retains, read digest-checked, never taken from a field the record
merely states:

* Chandra (`chandra.v1`): the top-level layout blocks of its answer
  (`chandra_layout.parse_layout_html`), each mapped to sealed-page pixels by
  `chandra_layout.block_page_bounds`, as the adapter's own `observe` maps them.
  A blank-page or malformed-bbox block is a unit with no box. A block's text is
  Chandra's text view of its blocks (`chandra-layout-text.v2`): markup removed,
  character references resolved, whitespace runs outside `<pre>` made one
  space and block-level tags made line breaks.
* Churro (`churro.v1`): the non-blank lines of its parsed document text in
  document order (`churro_document`), labelled with the `Header`/`Body`/`Footer`
  section they sit in, no box: Churro reports no coordinates.
* The synthetic fixture's Chandra page, joined from its declared act
  placeholders with no native capture: one unit per placeholder, read only
  when the caller says the run is synthetic (`fixture_placeholders`).
* dots.mocr (`dots-mocr.v1`): one unit per layout cell of its answer that has
  text under its text view (`common/dots_layout.py`), in the model's order,
  labelled with the cell's category, its box the cell's box mapped to
  sealed-page pixels (`dots_layout.cell_page_bounds`) or none where it places
  nothing on the page. A `Picture` cell and a cell with no text are not units.
* DAI (`dai.v1`): one unit per record its own detector found
  (`unit_captures`), box = that record's bounds, text = DAI's response for it
  decoded exactly, and checked against the span the record states. Two
  records whose spans overlap are refused by name.

Text a witness's own parse places outside its units -- Chandra's character
data outside every block (`content-outside-blocks`), Churro's text outside
every section or every page (`page-text-outside-sections`,
`document-text-outside-pages`), DAI's page text outside every record's span --
is one more unit at the end of that witness's order, with no ordinal, no box
and the label `OUTSIDE_UNITS_LABEL`, shown and accounted like any other:
nothing a witness said is absent from the feed. Each row also carries the
findings its parse of the retained bytes names, and its `answer_health`: the
Testimonium's `content_health.truncated` and every repetition finding its
native captures carry, which the prompt states on the witness's line.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Callable, Final

from common import dots_layout
from common.chairs.models import ChairIdentity
from common.chandra_layout import (
    block_page_bounds,
    is_refusal,
    outside_blocks_text,
    parse_layout_html,
)
from common.contracts.canonical import is_plain_int
from common.contracts.envelope import read_verified
from common.contracts.errors import SchemaRefusal
from common.native_witness import (
    REPETITION_FINDING_KINDS,
    churro_capture_system_prompt,
    churro_text_outside_sections,
    parse_churro_response,
    validate_native_capture,
    verify_native_capture_bytes,
)

# The outcome under which a witness's units are shown.
READ_OUTCOME: Final = "read"

CHANDRA: Final = "chandra.v1"
CHURRO: Final = "churro.v1"
DAI: Final = "dai.v1"
DOTS: Final = dots_layout.ADAPTER
# How finely each adapter's own units cut a page: a layout block or detector
# record is about one act; a line is a fraction of one.
UNIT_KINDS: Final = {
    CHANDRA: "layout-block",
    DAI: "detector-record",
    CHURRO: "line",
    DOTS: "layout-block",
}
DETECTOR_RECORD_UNIT: Final = "detector-record"
# The label of the unit holding a witness's text outside its own units.
OUTSIDE_UNITS_LABEL: Final = "outside units"
# The letters of Surya's lines (`L`) and blocks (`S`) in a page feed's ids.
DETECTION_LETTERS: Final = frozenset("LS")
# The letters a witness may take, in order: every capital but the detection letters.
WITNESS_LETTERS: Final = tuple(
    letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if letter not in DETECTION_LETTERS
)
# The answer health of a witness that did not read, or whose answer shows nothing.
NO_ANSWER_HEALTH: Final = {"truncated": None, "repetition": []}
_BOX_FIELDS: Final = frozenset({"x", "y", "w", "h"})
_NON_BLANK_LINE: Final = re.compile(r"[^\n]+")
_ASCII_WHITESPACE: Final = " \t\n\r\f\v"
_ASCII_WHITESPACE_RUN: Final = re.compile(f"[{re.escape(_ASCII_WHITESPACE)}]+")


def reads_detector_records(identity: Any) -> bool:
    """Whether a chair reads its page one detector record at a time.

    A page-scoped chair whose adapter's units are detector records (DAI) is
    shown one crop per record its own project's detector found, never the
    whole page. The one answer to this question, for the stage that serves the
    chair and for every reader of its page record.
    """
    return (
        isinstance(identity, ChairIdentity)
        and identity.witness_scope == "page"
        and UNIT_KINDS.get(identity.witness_adapter) == DETECTOR_RECORD_UNIT
    )


def checked_box(box: Any, page_size: tuple[int, int], what: str) -> dict[str, int]:
    """A sealed-page `{x, y, w, h}` rectangle wholly inside the page."""
    width, height = page_size
    if (
        not isinstance(box, dict)
        or set(box) != _BOX_FIELDS
        or not all(is_plain_int(box[key]) for key in _BOX_FIELDS)
        or box["x"] < 0
        or box["y"] < 0
        or box["w"] <= 0
        or box["h"] <= 0
        or box["x"] + box["w"] > width
        or box["y"] + box["h"] > height
    ):
        raise SchemaRefusal(
            f"{what} is not a positive {{x, y, w, h}} rectangle inside the {width}x{height} page"
        )
    return {key: box[key] for key in ("x", "y", "w", "h")}


# --- each witness's own units ---------------------------------------------------


def witness_unit(ordinal: int | None, box_px: dict[str, int] | None, label: str | None, text: str):
    return {"ordinal": ordinal, "box_px": box_px, "label": label, "text": text}


def _with_outside(units: list[dict[str, Any]], outside: str) -> list[dict[str, Any]]:
    """The units, then the text outside them as one more unit when there is any."""
    return units + ([witness_unit(None, None, OUTSIDE_UNITS_LABEL, outside)] if outside else [])


def _repetition(captures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every repetition finding the witness's native captures carry, in capture order."""
    return [
        dict(finding)
        for capture in captures
        for finding in capture["findings"]
        if finding.get("kind") in REPETITION_FINDING_KINDS
    ]


def _answer_health(payload: dict[str, Any], captures: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the witness's answer was cut off, and whether it repeats itself."""
    health = payload.get("content_health")
    truncated = health.get("truncated", "absent") if isinstance(health, dict) else "absent"
    if truncated not in (True, False, None):
        raise SchemaRefusal(
            "a page Testimonium read as `read` records no content_health.truncated of true, "
            "false or null, so whether its answer was cut off cannot be shown"
        )
    return {"truncated": truncated, "repetition": _repetition(captures)}


def _checked_capture(
    capture: Any, adapter: str, read_bytes: Callable[[str], bytes]
) -> tuple[dict[str, Any], bytes]:
    """One native capture and its retained raw response, digest-checked and re-derived."""
    capture = validate_native_capture(capture)
    if capture["adapter"] != adapter:
        raise SchemaRefusal(
            f"a page Testimonium's native capture names adapter {capture['adapter']!r}, not "
            f"the chair's configured {adapter!r}"
        )
    raw = read_verified(read_bytes, capture["raw_response_ref"], f"a {adapter} raw response")
    return verify_native_capture_bytes(capture, raw), raw


def _native_capture_bytes(
    payload: dict[str, Any], adapter: str, read_bytes: Callable[[str], bytes]
) -> tuple[dict[str, Any], bytes]:
    """The page capture and its retained raw response, digest-checked and re-derived."""
    capture = payload.get("native_capture")
    if capture is None:
        raise SchemaRefusal(
            f"a {adapter} page Testimonium read as `read` retains no native capture, so its "
            "own units cannot be re-derived from what the chair answered; the page feed "
            "reads only native page captures"
        )
    return _checked_capture(capture, adapter, read_bytes)


def _chandra_reading(
    payload: dict[str, Any], page_size: tuple[int, int], read_bytes: Callable[[str], bytes]
) -> dict[str, Any]:
    capture, raw = _native_capture_bytes(payload, CHANDRA, read_bytes)
    if capture["parse"].get("parser") != "html":
        raise SchemaRefusal(
            "a Chandra page capture was not read under the vendor layout grammar ('html'), "
            "so it carries no layout blocks to break its page into"
        )
    parsed = parse_layout_html(raw)
    if is_refusal(parsed):
        raise SchemaRefusal(
            "a Chandra page Testimonium read as `read` retains a response with no layout "
            f"blocks ({parsed['parse_outcome']})"
        )
    # The bytes parse, so the capture must say so: one recorded as another state
    # was not read by this parser from these bytes.
    if capture["parse"].get("state") != "parsed":
        raise SchemaRefusal(
            f"a Chandra page capture records its parse as {capture['parse'].get('state')!r}, "
            "but its retained raw response parses"
        )
    if capture["parse"]["text"] != parsed["page_text"]:
        raise SchemaRefusal(
            "a Chandra page capture's parsed text differs from its retained raw response"
        )
    # The repetition scan's finding is the Attestatores' own; the rest are the
    # grammar's and must be what this parse finds in the same bytes.
    grammar_findings = [
        finding
        for finding in capture["findings"]
        if finding.get("kind") not in REPETITION_FINDING_KINDS
    ]
    if grammar_findings != parsed["findings"]:
        raise SchemaRefusal(
            "a Chandra page capture's findings differ from its retained raw response"
        )
    units = [
        witness_unit(
            block["ordinal"],
            block_page_bounds(block, page_size=page_size),
            block["label"] if block["label_declared"] else None,
            block["text"],
        )
        for block in parsed["blocks"]
    ]
    return {
        "units": _with_outside(units, outside_blocks_text(raw)),
        "findings": parsed["findings"],
        "answer_health": _answer_health(payload, [capture]),
    }


FIXTURE_CHANDRA_SCHEMA: Final = "fixture-chandra-response.v1"


def _fixture_chandra_reading(
    payload: dict[str, Any], page_size: tuple[int, int], read_bytes: Callable[[str], bytes]
) -> dict[str, Any]:
    """A synthetic fixture's Chandra page, joined from its declared act responses.

    The committed fixture declares Chandra as JSON placeholders, one per act,
    each `{schema, markdown, blocks: [{bbox}]}` with the bbox in sealed-page
    pixels; they are joined into a page Testimonium with no native capture.
    Each placeholder is one unit, in the order the record names them: its box
    is the union of its blocks, widened to whole pixels as Chandra's own
    quantization widens them (floor the minimum, ceil the maximum), and its
    text is its `markdown`. A page with no placeholder of its own is one unit
    with no box carrying the record's joined page text. Only the fixture path
    calls this (`build_page_feed(fixture_placeholders=True)`); a served chair is
    never retained as a placeholder.
    """
    # Absent where the page carries no response of its own.
    references = payload.get("raw_response_refs", [])
    if not isinstance(references, list):
        raise SchemaRefusal("a joined fixture Chandra page names no retained responses")
    units = []
    for reference in references:
        raw = read_verified(read_bytes, reference, "a fixture Chandra response")
        try:
            declared = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SchemaRefusal(f"a fixture Chandra response is not JSON: {error}") from error
        if (
            not isinstance(declared, dict)
            or declared.get("schema") != FIXTURE_CHANDRA_SCHEMA
            or not isinstance(declared.get("markdown"), str)
            or not isinstance(declared.get("blocks"), list)
        ):
            raise SchemaRefusal(
                "a Chandra page Testimonium with no native capture retains a response that is "
                "not the fixture's own placeholder"
            )
        corners = []
        for block in declared["blocks"]:
            bbox = block.get("bbox") if isinstance(block, dict) else None
            if (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or not all(isinstance(value, (int, float)) for value in bbox)
            ):
                raise SchemaRefusal("a fixture Chandra block has no four-number bbox")
            corners.append(bbox)
        box = None
        if corners:
            x0 = math.floor(min(corner[0] for corner in corners))
            y0 = math.floor(min(corner[1] for corner in corners))
            x1 = math.ceil(max(corner[2] for corner in corners))
            y1 = math.ceil(max(corner[3] for corner in corners))
            box = checked_box(
                {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                page_size,
                "a fixture Chandra block box",
            )
        units.append(witness_unit(len(units), box, None, declared["markdown"]))
    if not units:
        text = payload.get("payload")
        if not isinstance(text, str):
            raise SchemaRefusal("a joined fixture Chandra page carries no page text")
        units.append(witness_unit(0, None, None, text))
    return {"units": units, "findings": [], "answer_health": _answer_health(payload, [])}


def _churro_reading(payload: dict[str, Any], read_bytes: Callable[[str], bytes]) -> dict[str, Any]:
    capture, raw = _native_capture_bytes(payload, CHURRO, read_bytes)
    system_prompt = churro_capture_system_prompt(capture)
    document = parse_churro_response(raw, system_prompt=system_prompt)
    if document["state"] != "parsed":
        raise SchemaRefusal(
            "a Churro page Testimonium read as `read` retains a response its grammar does not "
            f"parse ({document['state']})"
        )
    text = document["text"]
    sections = document["sections"]
    units = []
    for match in _NON_BLANK_LINE.finditer(text):
        if not match.group().strip(_ASCII_WHITESPACE):
            continue
        label = next(
            (
                section["section"]
                for section in sections
                if section["span"]["start"] <= match.start() < section["span"]["end"]
            ),
            None,
        )
        units.append(witness_unit(len(units) + 1, None, label, match.group()))
    outside = churro_text_outside_sections(raw, system_prompt=system_prompt)
    return {
        "units": _with_outside(units, outside),
        "findings": document["findings"],
        "answer_health": _answer_health(payload, [capture]),
    }


def _dots_reading(
    payload: dict[str, Any], page_size: tuple[int, int], read_bytes: Callable[[str], bytes]
) -> dict[str, Any]:
    capture, raw = _native_capture_bytes(payload, DOTS, read_bytes)
    layout = dots_layout.parse_layout(raw)
    if layout["state"] != "parsed" or capture["parse"].get("state") != "parsed":
        raise SchemaRefusal(
            "a dots.mocr page Testimonium read as `read` retains a response its layout grammar "
            f"does not parse ({layout['state']})"
        )
    if capture["parse"]["text"] != layout["text"]:
        raise SchemaRefusal(
            "a dots.mocr page capture's parsed text differs from its retained raw response"
        )
    units = []
    for cell in layout["cells"]:
        if not cell["text"]:
            continue
        box = dots_layout.cell_page_bounds(cell["bbox"], page_size)
        units.append(
            witness_unit(
                len(units) + 1,
                None if box is None else checked_box(box, page_size, "a dots.mocr cell box"),
                cell["category"],
                cell["text"],
            )
        )
    return {
        "units": units,
        "findings": layout["findings"],
        "answer_health": _answer_health(payload, [capture]),
    }


def _dai_reading(
    payload: dict[str, Any], page_size: tuple[int, int], read_bytes: Callable[[str], bytes]
) -> dict[str, Any]:
    captures = payload.get("unit_captures")
    observed = payload.get("observed")
    page_text = payload.get("payload")
    if (
        not isinstance(captures, list)
        or not isinstance(observed, list)
        or len(captures) != len(observed)
        or not isinstance(page_text, str)
    ):
        raise SchemaRefusal(
            "a DAI page Testimonium read as `read` does not carry one unit capture and one "
            "observed box per record its detector found"
        )
    units, checked, spans = [], [], []
    for position, (capture, item) in enumerate(zip(captures, observed, strict=True)):
        if capture is None:
            raise SchemaRefusal(
                f"a DAI page Testimonium read as `read` has no response for record {position}"
            )
        capture, raw = _checked_capture(capture, DAI, read_bytes)
        checked.append(capture)
        try:
            # DAI's own `text` parser: the response decoded exactly, nothing rewritten.
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SchemaRefusal(f"a DAI response is not UTF-8 text: {error}") from error
        if capture["parse"].get("state") == "parsed" and capture["parse"].get("text") != text:
            raise SchemaRefusal(
                f"DAI record {position}'s parsed text differs from its retained raw response"
            )
        if not isinstance(item, dict) or not is_plain_int(item.get("ordinal")):
            raise SchemaRefusal(f"DAI record {position} has no integer ordinal")
        span = item.get("span")
        if (
            not isinstance(span, dict)
            or not is_plain_int(span.get("start"))
            or not is_plain_int(span.get("end"))
            or page_text[span["start"] : span["end"]] != text
        ):
            raise SchemaRefusal(
                f"DAI's page text does not carry record {position}'s retained response at "
                "the span the record states"
            )
        spans.append((span["start"], span["end"], position))
        units.append(
            witness_unit(
                item["ordinal"],
                checked_box(item.get("bounds"), page_size, "a DAI record box"),
                None,
                text,
            )
        )
    # DAI's text parser reads each response exactly and names no finding.
    return {
        "units": _with_outside(units, _dai_outside_text(page_text, spans)),
        "findings": [],
        "answer_health": _answer_health(payload, checked),
    }


def _dai_outside_text(page_text: str, spans: list[tuple[int, int, int]]) -> str:
    """DAI's page text outside every record's span, or a refusal naming two that overlap.

    The Attestatores join the records' responses with a newline between them,
    so the text between spans is normally whitespace alone and nothing is
    returned. Each run between spans has its whitespace runs made one space
    and its ends stripped, as Churro's outside text is; the non-empty runs are
    joined by a newline.
    """
    runs, cursor, previous = [], 0, None
    for start, end, position in sorted(spans):
        if start < cursor:
            raise SchemaRefusal(
                f"DAI records {previous} and {position} state overlapping spans of the page "
                "text, so one stretch of text would be shown as two records' units"
            )
        runs.append(page_text[cursor:start])
        if end > start:
            cursor, previous = end, position
    runs.append(page_text[cursor:])
    collapsed = (
        " ".join(part for part in _ASCII_WHITESPACE_RUN.split(run) if part) for run in runs
    )
    return "\n".join(run for run in collapsed if run)


def witness_reading(
    testimonium: dict[str, Any],
    *,
    adapter: str,
    page_size: tuple[int, int],
    read_bytes: Callable[[str], bytes],
    fixture_placeholders: bool = False,
) -> dict[str, Any]:
    """One witness's page as `{units, findings, answer_health}`, empty unless it read.

    `testimonium` is the sealed `page-testimonium` record, `adapter` the chair's
    configured witness adapter, `page_size` the sealed page's `(width, height)`
    and `read_bytes` the run tree's reader. Each unit is `{ordinal, box_px,
    label, text}`, `box_px` being a sealed-page `{x, y, w, h}` or `None`; the
    last may be the witness's text outside its own units (`OUTSIDE_UNITS_LABEL`,
    no ordinal). `findings` are what the witness's parse of its retained bytes
    names; `answer_health` is `{truncated, repetition}`: the Testimonium's
    `content_health.truncated` and every repetition finding its native
    captures carry. `fixture_placeholders` lets a synthetic run's joined
    Chandra page be read (`_fixture_chandra_reading`); a real run leaves it off.
    """
    if "outcome" not in testimonium:
        raise SchemaRefusal("a page Testimonium records no outcome")
    if testimonium["outcome"] != READ_OUTCOME:
        return {"units": [], "findings": [], "answer_health": copy.deepcopy(NO_ANSWER_HEALTH)}
    payload = testimonium.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a page Testimonium has no payload to read units from")
    if adapter == CHANDRA:
        if fixture_placeholders and payload.get("native_capture") is None:
            return _fixture_chandra_reading(payload, page_size, read_bytes)
        return _chandra_reading(payload, page_size, read_bytes)
    if adapter == CHURRO:
        return _churro_reading(payload, read_bytes)
    if adapter == DAI:
        return _dai_reading(payload, page_size, read_bytes)
    if adapter == DOTS:
        return _dots_reading(payload, page_size, read_bytes)
    raise SchemaRefusal(
        f"witness adapter {adapter!r} has no page-unit reader; its page cannot be shown in its "
        f"own units (the readers are {sorted(UNIT_KINDS)})"
    )
