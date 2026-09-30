"""The Perlector's page feed: everything one whole-page reading is shown.

Under `reading_unit = "page"` the Perlector reads one page per call and
establishes the acts itself. What it is shown is this feed: the page image
(carried beside the text, not in it), each witness's page text broken into
that witness's own units, and Surya's detected lines and blocks. Every input
has a switch in the sealed `[feed]` table (`protocol.validate_feed_table`); a
switched-off input is absent from the prompt and the feed records the
switches it was built under.

Nothing here chooses among the witnesses. Witness letters are assigned
`A, B, C, ...` in sorted `witness_label` order, which is a pseudonym under a
blinded regime, so a letter carries no preference and reveals no chair.
`L` and `S` are skipped (`WITNESS_LETTERS`): they name Surya's lines and
blocks, so a witness letter never makes an id ambiguous.

## Ids

The feed defines every id the Perlector may cite, and nothing recomputes them
later: witness units `A1..An` (the unit's 1-based position in that witness's
own order), Surya lines `L1..Ln` in Surya's order and Surya blocks `S1..Sn` in
Surya's reading order.

## Units, re-derived from retained bytes

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

A shown witness whose page outcome is not `read` is a row with its outcome
and no units: it is never silently absent. Every chair of the sealed
page-witness roster must be given with its Testimonium (a roster chair with
none is refused), but only the chairs the `witnesses` switch shows become
rows; a hidden chair appears only in the recorded `switches`.

Under `witness_units = "flat"` the rows keep the same units, ids, sealed
boxes (`box_px`) and unit kind, so the accounting reads them unchanged; only
what is shown changes: no unit box_1000, and the prompt shows each witness as
one unit with no box, its units' texts joined, cited by the range of its unit
ids. Such a witness places nothing: `common.page_accounting.placement_boxes`
gives its units no box,
so an entry's region comes only from boxed ids -- Surya's detections and the
units of a boxed witness shown in its own units -- and two entries citing the
same flat witness never share a region through it.

Letters and labels are shown as the regime gives them. The blinded regime
hides chair and model names (`witness_label` is a pseudonym, `chair` is
`None`); the labels a witness wrote on its own units -- Chandra's block labels,
Churro's section names -- are part of its report and are shown as given.

## Calling it (stage 4, page path)

    feed = build_page_feed(
        page_id=..., page_ordinal=..., page_size=(width, height),
        feed_switches=protocol_config["feed"], witness_regime="named" | "blinded",
        roster=[chair, ...],                    # the sealed page-witness roster
        witnesses=[{"chair", "witness_label", "adapter", "testimonium",
                    "testimonium_ref"}, ...],   # one per roster chair
        surya={"census_ref", "block_sequence", "block_sequence_reason",
               "lines": [{"box_px", "confidence_bp", "ref"}],
               "blocks": [{"box_px", "label", "position", "confidence_bp", "ref"}]} | None,
        page_render=dossier.build_page_render(...) | None,
        serving_recipe=chair.serving_recipe,
        read_bytes=context.tree.read_bytes,
    )
    text = page_prompt.build_page_prompt(chair.serving_recipe, feed)
    parts = page_prompt.prompt_parts(chair.serving_recipe, feed)  # what the capacity charges
    overlay = page_overlay.overlay_image(feed, context.tree.read_bytes)  # when drawn
    images = request_image_sizes(feed)   # what the capacity check charges
    boxes = page_accounting.placement_boxes(feed)  # each id's box for an entry's region
    nothing = shows_nothing(feed)        # a feed a reading could not be made from

Each `testimonium` must already have passed the stage's own page-Testimonium
checks (`run.validate_page_testimonium_record`); this module re-derives units
from its retained bytes but does not re-run those checks. It does check that
each `testimonium` is the record its `testimonium_ref` names: the ref's bytes,
read digest-checked, decode to exactly that record, a `page-testimonium` of
this page whose payload names the row's chair; no two rows may share a ref. Every ref on the feed (`testimonium_ref`, Surya's `census_ref` and
each line's and block's `ref`) is an input the caller binds on the page-feed
record.

A page the Attestatores served no page Testimonium at all (stage 3 serves only
pages with a proposed Designator act) is built with `no_testimony=True` and no
witnesses: its feed records `witness_testimony: "none"` and no row, so the
absence is stated rather than silent. A roster chair missing beside others
that did testify is still refused.

Surya's census states how its blocks were sequenced, recorded on the feed as
`block_sequence`: `surya-order-head` (Surya's reading-order model placed them)
or `raster-fallback` (Surya sorted them top to bottom, then left to right, and
`block_sequence_reason` says why). The prompt says "raster order" rather than
"reading order" for a fallback. The feed names it `block_sequence` because the
dossier sweep refuses any key naming an order. Each
line's and block's `confidence_bp` is recorded on the feed and never rendered.

`serving_recipe` may be `None` (the Perlector chair is absent): the feed is
built and sealed with `prompt: None`, as it is for a feed that shows nothing
(`shows_nothing`), since no request is made from either.

`assemble_page_feed` takes the same arguments with each witness's reading
already made (`{chair, witness_label, adapter, outcome, testimonium_ref,
units, findings, answer_health}`) and no `read_bytes`; `build_page_feed` is `witness_reading`
then `assemble_page_feed`, and measurement tools call it directly.
"""

from __future__ import annotations

import json
import math
import re
from fractions import Fraction
from typing import Any, Callable, Final

import page_overlay
import page_prompt
import protocol

from common.chandra_layout import (
    block_page_bounds,
    is_refusal,
    outside_blocks_text,
    parse_layout_html,
)
from common.contracts.canonical import digest_of, is_plain_int
from common.contracts.envelope import digest_ref, read_verified
from common.contracts.errors import SchemaRefusal
from common.native_witness import (
    REPETITION_FINDING_KINDS,
    churro_capture_system_prompt,
    churro_text_outside_sections,
    parse_churro_response,
    validate_native_capture,
    verify_native_capture_bytes,
)
from common.witness_regime import BLINDED, NAMED, REGIMES
from operations.serving.surya_detector import contract as surya_contract

SCHEMA: Final = "perlector-page-feed.v1"
READING_UNIT: Final = "page"
# The outcome under which a witness's units are shown.
READ_OUTCOME: Final = "read"
BOX_SCALE: Final = 1000

CHANDRA: Final = "chandra.v1"
CHURRO: Final = "churro.v1"
DAI: Final = "dai.v1"
PAGE_TESTIMONIUM_KIND: Final = "page-testimonium"
# How finely each adapter's own units cut a page: a layout block or detector
# record is about one act; a line is a fraction of one. The answer reserve
# counts act entries from the act-sized kinds only (`answer_measure`).
UNIT_KINDS: Final = {CHANDRA: "layout-block", DAI: "detector-record", CHURRO: "line"}
_ACT_SIZED_UNIT_KINDS: Final = frozenset({"layout-block", "detector-record"})
# The label of the unit holding a witness's text outside its own units.
OUTSIDE_UNITS_LABEL: Final = "outside units"
# The letters a shown witness may take, in order: every capital but `L` and
# `S`, which name Surya's lines and blocks.
WITNESS_LETTERS: Final = tuple(
    letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if letter not in "LS"
)

_WITNESS_FIELDS: Final = frozenset(
    {"chair", "witness_label", "adapter", "testimonium", "testimonium_ref"}
)
_ASSEMBLED_WITNESS_FIELDS: Final = frozenset(
    {
        "chair",
        "witness_label",
        "adapter",
        "outcome",
        "testimonium_ref",
        "units",
        "findings",
        "answer_health",
    }
)
_ANSWER_HEALTH_FIELDS: Final = frozenset({"truncated", "repetition"})
# The answer health of a witness that did not read, or whose answer shows nothing.
NO_ANSWER_HEALTH: Final = {"truncated": None, "repetition": []}
_UNIT_FIELDS: Final = frozenset({"ordinal", "box_px", "label", "text"})
_SURYA_FIELDS: Final = frozenset(
    {"census_ref", "block_sequence", "block_sequence_reason", "lines", "blocks"}
)
# How Surya ordered a page's blocks: its reading-order model, or a raster sort
# (top to bottom, then left to right) with the reason Surya fell back to it.
ORDER_HEAD: Final = surya_contract.ORDER_HEAD
RASTER_FALLBACK: Final = surya_contract.RASTER_FALLBACK
# Given as `surya` when the run holds no Surya census at all: the feed then
# records Surya as absent and shows no line or block, whatever the switches say.
SURYA_ABSENT: Final = "absent"
SURYA_ABSENT_REASON: Final = "no Surya page census was sealed in this run"
_SURYA_LINE_FIELDS: Final = frozenset({"box_px", "confidence_bp", "ref"})
_SURYA_BLOCK_FIELDS: Final = frozenset({"box_px", "label", "position", "confidence_bp", "ref"})
# Whether the page's witnesses testified: `none` when stage 3 served the page no
# page Testimonium at all.
TESTIMONY_PRESENT: Final = "present"
TESTIMONY_NONE: Final = "none"
_BOX_FIELDS: Final = frozenset({"x", "y", "w", "h"})
_NON_BLANK_LINE: Final = re.compile(r"[^\n]+")
_ASCII_WHITESPACE: Final = " \t\n\r\f\v"
_ASCII_WHITESPACE_RUN: Final = re.compile(f"[{re.escape(_ASCII_WHITESPACE)}]+")


# --- geometry -------------------------------------------------------------------


def _checked_box(box: Any, page_size: tuple[int, int], what: str) -> dict[str, int]:
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


def box_1000(box_px: dict[str, int], page_size: tuple[int, int]) -> list[int]:
    """`[x0, y0, x1, y1]` on a 0-1000 grid of the page, rounded half to even.

    The grid is Qwen3-VL's own convention for boxes. Exact in rationals, so the
    rounding never depends on float representation.
    """
    width, height = page_size
    return [
        round(Fraction(box_px["x"] * BOX_SCALE, width)),
        round(Fraction(box_px["y"] * BOX_SCALE, height)),
        round(Fraction((box_px["x"] + box_px["w"]) * BOX_SCALE, width)),
        round(Fraction((box_px["y"] + box_px["h"]) * BOX_SCALE, height)),
    ]


# --- each witness's own units ---------------------------------------------------


def _unit(ordinal: int | None, box_px: dict[str, int] | None, label: str | None, text: str):
    return {"ordinal": ordinal, "box_px": box_px, "label": label, "text": text}


def _with_outside(units: list[dict[str, Any]], outside: str) -> list[dict[str, Any]]:
    """The units, then the text outside them as one more unit when there is any."""
    return units + ([_unit(None, None, OUTSIDE_UNITS_LABEL, outside)] if outside else [])


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
        _unit(
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
            box = _checked_box(
                {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                page_size,
                "a fixture Chandra block box",
            )
        units.append(_unit(len(units), box, None, declared["markdown"]))
    if not units:
        text = payload.get("payload")
        if not isinstance(text, str):
            raise SchemaRefusal("a joined fixture Chandra page carries no page text")
        units.append(_unit(0, None, None, text))
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
        units.append(_unit(len(units) + 1, None, label, match.group()))
    outside = churro_text_outside_sections(raw, system_prompt=system_prompt)
    return {
        "units": _with_outside(units, outside),
        "findings": document["findings"],
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
            _unit(
                item["ordinal"],
                _checked_box(item.get("bounds"), page_size, "a DAI record box"),
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
        return {"units": [], "findings": [], "answer_health": {"truncated": None, "repetition": []}}
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
    raise SchemaRefusal(
        f"witness adapter {adapter!r} has no page-unit reader; its page cannot be shown in its "
        f"own units (the readers are {sorted(UNIT_KINDS)})"
    )


def witness_units(
    testimonium: dict[str, Any],
    *,
    adapter: str,
    page_size: tuple[int, int],
    read_bytes: Callable[[str], bytes],
    fixture_placeholders: bool = False,
) -> list[dict[str, Any]]:
    """`witness_reading`'s units alone."""
    return witness_reading(
        testimonium,
        adapter=adapter,
        page_size=page_size,
        read_bytes=read_bytes,
        fixture_placeholders=fixture_placeholders,
    )["units"]


# --- the feed -------------------------------------------------------------------


def _checked_roster(roster: Any) -> list[str]:
    if (
        not isinstance(roster, list)
        or not all(isinstance(chair, str) and chair.strip() for chair in roster)
        or len(set(roster)) != len(roster)
    ):
        raise SchemaRefusal("the sealed page-witness roster is not a list of distinct chair names")
    return roster


def _shown_chairs(switch: Any, roster: list[str]) -> set[str]:
    if switch == protocol.ALL_WITNESSES:
        return set(roster)
    unknown = sorted(set(switch) - set(roster))
    if unknown:
        raise SchemaRefusal(
            f"the sealed feed shows witness chair(s) {unknown}, which are not in this page's "
            f"roster {sorted(roster)}"
        )
    return set(switch)


def _checked_witnesses(
    witnesses: Any, fields: frozenset[str], regime: str, roster: list[str], *, no_testimony: bool
) -> list[dict]:
    if not isinstance(witnesses, list):
        raise SchemaRefusal("the page feed's witnesses are not a list")
    if no_testimony:
        if witnesses:
            raise SchemaRefusal("a page with no witness testimony is given witnesses")
        return witnesses
    for witness in witnesses:
        if not isinstance(witness, dict) or set(witness) != fields:
            raise SchemaRefusal(f"a page-feed witness is not exactly {sorted(fields)}")
        if regime == NAMED and witness["witness_label"] != witness["chair"]:
            raise SchemaRefusal("under the named regime a witness's label is its chair")
        if regime == BLINDED and witness["witness_label"] == witness["chair"]:
            raise SchemaRefusal("under the blinded regime a witness's label is not its chair")
        if witness["adapter"] not in UNIT_KINDS:
            raise SchemaRefusal(f"witness adapter {witness['adapter']!r} has no page-unit reader")
    for field in ("chair", "witness_label"):
        values = [witness[field] for witness in witnesses]
        if len(set(values)) != len(values):
            raise SchemaRefusal(f"two page-feed witnesses share a {field}")
    refs = [
        digest_of(digest_ref(witness["testimonium_ref"], "a page Testimonium reference"))
        for witness in witnesses
    ]
    if len(set(refs)) != len(refs):
        raise SchemaRefusal(
            "two page-feed witnesses share a testimonium_ref; each row is its own chair's "
            "page Testimonium"
        )
    chairs = {witness["chair"] for witness in witnesses}
    missing, extra = sorted(set(roster) - chairs), sorted(chairs - set(roster))
    if missing:
        raise SchemaRefusal(
            f"configured page witness(es) {missing} of the sealed roster have no row on this "
            "page's feed; a witness is never silently absent"
        )
    if extra:
        raise SchemaRefusal(
            f"page-feed witness(es) {extra} are not in the sealed page-witness roster"
        )
    return witnesses


def _checked_unit(unit: Any) -> dict[str, Any]:
    if (
        not isinstance(unit, dict)
        or set(unit) != _UNIT_FIELDS
        or not (unit["ordinal"] is None or is_plain_int(unit["ordinal"]))
        or not (unit["label"] is None or isinstance(unit["label"], str))
        or not isinstance(unit["text"], str)
    ):
        raise SchemaRefusal(f"a witness unit is not exactly {sorted(_UNIT_FIELDS)}")
    return unit


def _witness_row(
    letter: str,
    witness: dict[str, Any],
    *,
    switches: dict[str, Any],
    regime: str,
    page_size: tuple[int, int],
) -> dict[str, Any]:
    units, findings, health = witness["units"], witness["findings"], witness["answer_health"]
    if not isinstance(units, list) or not isinstance(findings, list):
        raise SchemaRefusal(
            f"shown witness {witness['witness_label']!r} carries no units or no findings list"
        )
    if (
        not isinstance(health, dict)
        or set(health) != _ANSWER_HEALTH_FIELDS
        or health["truncated"] not in (True, False, None)
        or not isinstance(health["repetition"], list)
        or not all(
            isinstance(finding, dict) and finding.get("kind") in REPETITION_FINDING_KINDS
            for finding in health["repetition"]
        )
    ):
        raise SchemaRefusal(
            f"shown witness {witness['witness_label']!r} carries no answer health of "
            f"{sorted(_ANSWER_HEALTH_FIELDS)}"
        )
    if witness["outcome"] != READ_OUTCOME and (units or findings or health != NO_ANSWER_HEALTH):
        raise SchemaRefusal("a witness that did not read carries units, findings or answer health")
    # Boxes are shown only with coordinates on and units shown as their own.
    boxes_shown = switches["witness_coordinates"] and switches["witness_units"] == "own"
    shown = []
    for number, unit in enumerate(units, start=1):
        unit = _checked_unit(unit)
        box_px = (
            None
            if unit["box_px"] is None
            else _checked_box(unit["box_px"], page_size, "a unit box")
        )
        shown.append(
            {
                "id": f"{letter}{number}",
                "ordinal": unit["ordinal"],
                # Kept whether or not coordinates are shown: the geometry is the
                # witness's sealed evidence, which the stage's accounting reads.
                "box_px": box_px,
                "box_1000": box_1000(box_px, page_size)
                if box_px is not None and boxes_shown
                else None,
                "label": unit["label"],
                "text": unit["text"],
            }
        )
    return {
        "letter": letter,
        "witness_label": witness["witness_label"],
        "chair": witness["chair"] if regime == NAMED else None,
        # What one unit of this row is. `detector-record` marks DAI's units as
        # the records its own detector found, which the page accounting holds
        # to one act each; it is never rendered into the prompt.
        "unit_kind": UNIT_KINDS[witness["adapter"]],
        "outcome": witness["outcome"],
        "testimonium_ref": digest_ref(witness["testimonium_ref"], "a page Testimonium reference"),
        "findings": findings,
        # Stated on the witness's line in the prompt: a cut-off or repeating
        # answer is part of what the witness reported.
        "answer_health": health,
        "units": shown,
    }


def answer_measure(
    rows: list[tuple[str, list[dict[str, Any]]]], *, surya_blocks: int
) -> dict[str, int]:
    """What the page's answer is reserved on, from what the feed shows of the page.

    `rows` is `(adapter, own units)` per shown witness whose outcome is `read`,
    and `surya_blocks` the number of Surya blocks shown. The answer transcribes
    the same ink the witnesses read, so its text is measured by the longest
    witness text, text outside its units included. Its entries are the page's
    likely act count: the most of Surya's blocks, DAI's detector records and
    Chandra's layout blocks, each about one act. A line witness's lines are
    fractions of acts and are not counted. With none of the three shown the
    count is 0 and only the text is reserved; the reserve decides admission,
    and the request is sent the page cap or the room left, whichever is less.
    """
    longest = max((sum(len(unit["text"]) for unit in units) for _adapter, units in rows), default=0)
    act_sized = [
        sum(1 for unit in units if unit["ordinal"] is not None)
        for adapter, units in rows
        if UNIT_KINDS[adapter] in _ACT_SIZED_UNIT_KINDS
    ]
    return {
        "longest_witness_characters": longest,
        "act_entries": max([surya_blocks, *act_sized]),
    }


def _confidence(value: Any) -> int | None:
    if value is not None and not (is_plain_int(value) and 0 <= value <= 10_000):
        raise SchemaRefusal("a Surya confidence is not null or basis points in 0..10000")
    return value


def _surya(surya: Any, switches: dict[str, Any], page_size: tuple[int, int]) -> dict | None:
    if not switches["surya_lines"] and not switches["surya_blocks"]:
        return None
    if surya == SURYA_ABSENT:
        return {
            "census_ref": None,
            "absent": SURYA_ABSENT_REASON,
            "block_sequence": None,
            "block_sequence_reason": None,
            "lines": [],
            "blocks": [],
        }
    if not isinstance(surya, dict) or set(surya) != _SURYA_FIELDS:
        raise SchemaRefusal(
            "the sealed feed shows Surya's detections, but no Surya census of "
            f"{sorted(_SURYA_FIELDS)} was given for this page"
        )
    order, reason = surya["block_sequence"], surya["block_sequence_reason"]
    if not (
        (order == ORDER_HEAD and reason is None)
        or (order == RASTER_FALLBACK and isinstance(reason, str) and reason.strip())
    ):
        raise SchemaRefusal(
            f"Surya's block_sequence is not {ORDER_HEAD!r} with no reason or "
            f"{RASTER_FALLBACK!r} with one"
        )
    lines = surya["lines"] if switches["surya_lines"] else []
    blocks = surya["blocks"] if switches["surya_blocks"] else []
    if not isinstance(lines, list) or not isinstance(blocks, list):
        raise SchemaRefusal("Surya's lines and blocks are not lists")
    shown_lines = []
    for number, line in enumerate(lines, start=1):
        if not isinstance(line, dict) or set(line) != _SURYA_LINE_FIELDS:
            raise SchemaRefusal(f"a Surya line is not exactly {sorted(_SURYA_LINE_FIELDS)}")
        box = _checked_box(line["box_px"], page_size, "a Surya line box")
        shown_lines.append(
            {
                "id": f"L{number}",
                "box_px": box,
                "box_1000": box_1000(box, page_size),
                "confidence_bp": _confidence(line["confidence_bp"]),
                "ref": digest_ref(line["ref"], "a Surya line reference"),
            }
        )
    for block in blocks:
        if (
            not isinstance(block, dict)
            or set(block) != _SURYA_BLOCK_FIELDS
            or not is_plain_int(block["position"])
            or not (block["label"] is None or isinstance(block["label"], str))
        ):
            raise SchemaRefusal(f"a Surya block is not exactly {sorted(_SURYA_BLOCK_FIELDS)}")
    positions = [block["position"] for block in blocks]
    if len(set(positions)) != len(positions):
        raise SchemaRefusal("two Surya blocks share a reading-order position")
    shown_blocks = []
    for number, block in enumerate(sorted(blocks, key=lambda item: item["position"]), start=1):
        box = _checked_box(block["box_px"], page_size, "a Surya block box")
        shown_blocks.append(
            {
                "id": f"S{number}",
                "box_px": box,
                "box_1000": box_1000(box, page_size),
                "label": block["label"],
                "confidence_bp": _confidence(block["confidence_bp"]),
                "ref": digest_ref(block["ref"], "a Surya block reference"),
            }
        )
    return {
        "census_ref": digest_ref(surya["census_ref"], "the Surya page census reference"),
        "block_sequence": order,
        "block_sequence_reason": reason,
        "lines": shown_lines,
        "blocks": shown_blocks,
    }


def _checked_page_render(
    page_render: Any, page_image: str, page_size: tuple[int, int]
) -> dict[str, Any] | None:
    """The render the sealed `page_image` switch asks for, or `None` when it is off."""
    if page_image == "off":
        if page_render is not None:
            raise SchemaRefusal("the sealed feed shows no page image, but a page render was given")
        return None
    if not isinstance(page_render, dict) or not isinstance(page_render.get("transform"), dict):
        raise SchemaRefusal(
            f"the sealed feed shows the page image ({page_image}) but none was given"
        )
    transform = page_render["transform"]
    if page_image == "full" and (
        transform.get("resampler") != "identity"
        or transform.get("target_dimensions") != {"w": page_size[0], "h": page_size[1]}
    ):
        raise SchemaRefusal("the sealed feed shows the full page, but the render is resized")
    if page_image == "legible" and page_render.get("reason") != "legible-ink":
        raise SchemaRefusal(
            "the sealed feed shows the legible page render, but this render is not one"
        )
    return page_render


def _check_testimonium_ref(
    witness: dict[str, Any], page_id: str, read_bytes: Callable[[str], bytes]
) -> None:
    """Refuse a testimonium that is not the `page-testimonium` its ref's bytes hold."""
    ref = digest_ref(witness["testimonium_ref"], "a page Testimonium reference")
    data = read_verified(read_bytes, ref, "a page Testimonium")
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SchemaRefusal(
            f"page Testimonium {ref['relative_path']} is not JSON: {error}"
        ) from error
    testimonium = witness["testimonium"]
    if record != testimonium:
        raise SchemaRefusal(
            f"witness {witness['chair']!r}'s page Testimonium is not the record its reference "
            f"{ref['relative_path']} holds"
        )
    if not isinstance(testimonium, dict):
        raise SchemaRefusal("a page Testimonium is not a record")
    if testimonium.get("kind") != PAGE_TESTIMONIUM_KIND or testimonium.get("subject_id") != page_id:
        raise SchemaRefusal(
            f"witness {witness['chair']!r}'s Testimonium is not a {PAGE_TESTIMONIUM_KIND} of "
            f"page {page_id!r}"
        )
    if "outcome" not in testimonium:
        raise SchemaRefusal(f"witness {witness['chair']!r}'s page Testimonium records no outcome")
    payload = testimonium.get("payload")
    if not isinstance(payload, dict) or payload.get("chair") != witness["chair"]:
        raise SchemaRefusal(
            f"witness {witness['chair']!r}'s page Testimonium names chair "
            f"{payload.get('chair') if isinstance(payload, dict) else None!r}, not the row's"
        )


def build_page_feed(
    *,
    page_id: str,
    page_ordinal: int,
    page_size: tuple[int, int],
    feed_switches: dict[str, Any],
    witness_regime: str,
    roster: list[str],
    witnesses: list[dict[str, Any]],
    surya: dict[str, Any] | None,
    page_render: dict[str, Any] | None,
    serving_recipe: str | None,
    read_bytes: Callable[[str], bytes],
    fixture_placeholders: bool = False,
    no_testimony: bool = False,
) -> dict[str, Any]:
    """The `perlector-page-feed.v1` payload for one page, deterministic from sealed inputs.

    `roster` is the sealed page-witness roster (chair names) and `witnesses`
    one Testimonium per roster chair for this page, in any order: `{chair,
    witness_label, adapter, testimonium, testimonium_ref}`. Each testimonium is
    checked to be exactly the record its ref's bytes hold. Units are read from
    the retained bytes of the shown witnesses only; the rest is
    `assemble_page_feed`.
    """
    switches = protocol.validate_feed_table(feed_switches)
    roster = _checked_roster(roster)
    witnesses = _checked_witnesses(
        witnesses, _WITNESS_FIELDS, witness_regime, roster, no_testimony=no_testimony
    )
    for witness in witnesses:
        _check_testimonium_ref(witness, page_id, read_bytes)
    shown = _shown_chairs(switches["witnesses"], roster)
    readings = {
        witness["chair"]: witness_reading(
            witness["testimonium"],
            adapter=witness["adapter"],
            page_size=page_size,
            read_bytes=read_bytes,
            fixture_placeholders=fixture_placeholders,
        )
        if witness["chair"] in shown
        else {"units": None, "findings": None, "answer_health": None}
        for witness in witnesses
    }
    render_bytes = None
    if switches["page_overlay"] != "off" and isinstance(page_render, dict):
        render_bytes = read_verified(
            read_bytes, page_overlay.render_ref(page_render), "the page render"
        )
    return assemble_page_feed(
        page_id=page_id,
        page_ordinal=page_ordinal,
        page_size=page_size,
        feed_switches=switches,
        witness_regime=witness_regime,
        roster=roster,
        witnesses=[
            {
                "chair": witness["chair"],
                "witness_label": witness["witness_label"],
                "adapter": witness["adapter"],
                "outcome": witness["testimonium"]["outcome"],
                "testimonium_ref": witness["testimonium_ref"],
                **readings[witness["chair"]],
            }
            for witness in witnesses
        ],
        surya=surya,
        page_render=page_render,
        serving_recipe=serving_recipe,
        page_render_bytes=render_bytes,
        no_testimony=no_testimony,
    )


def assemble_page_feed(
    *,
    page_id: str,
    page_ordinal: int,
    page_size: tuple[int, int],
    feed_switches: dict[str, Any],
    witness_regime: str,
    roster: list[str],
    witnesses: list[dict[str, Any]],
    surya: dict[str, Any] | None,
    page_render: dict[str, Any] | None,
    serving_recipe: str | None,
    page_render_bytes: bytes | None = None,
    no_testimony: bool = False,
) -> dict[str, Any]:
    """The feed from witness readings already made, for `build_page_feed` and for measurement.

    `roster` is the sealed page-witness roster, and `witnesses` one entry per
    roster chair, in any order: `{chair, witness_label, adapter, outcome,
    testimonium_ref, units, findings, answer_health}`, the last three being
    `witness_reading`'s for a shown witness (`None` is allowed for one the
    switch hides). The `witnesses` switch picks which become rows; shown ones
    get `WITNESS_LETTERS` in sorted `witness_label` order, and a hidden chair
    appears only in the recorded `switches`. `surya` is the page's Surya census
    as `{census_ref, block_sequence, block_sequence_reason, lines: [{box_px,
    confidence_bp, ref}] in Surya's order, blocks: [{box_px, label, position,
    confidence_bp, ref}]}`, `position` being the block's place in the sequence
    `block_sequence` names; it may be
    `None` only when both Surya switches are off, or `SURYA_ABSENT` when the
    run holds no Surya census at all, which the feed records as
    `{census_ref: None, absent: <reason>, block_sequence: None,
    block_sequence_reason: None, lines: [], blocks: []}`. `no_testimony`
    states that the page has no page Testimonium at all (`witnesses` is then
    empty). `page_render` is what
    `dossier.build_page_render` returned for the `page_image` switch, or `None`
    when it is off. `page_render_bytes` are the render's bytes, needed only
    when `page_overlay` is on, to draw the overlay and seal its digest.

    Returns `{schema, page_id, page_ordinal, page_size: {w, h}, reading_unit,
    witness_regime, switches, page_render, overlay, witness_testimony,
    witnesses, surya, answer_measure, prompt, feed_digest}`; `prompt` is `None`
    when `serving_recipe` is `None` or the feed shows nothing; `overlay` is `None` when the switch
    is off, else `page_overlay.overlay_record`'s `{source_image_sha256,
    dimensions, label_scale, colours, drawn: [{id, source, box}],
    renderer_sha256, image_sha256}`; `feed_digest` is the digest of every other
    field.
    """
    switches = protocol.validate_feed_table(feed_switches)
    if witness_regime not in REGIMES:
        raise SchemaRefusal(f"witness regime {witness_regime!r} is not one of {sorted(REGIMES)}")
    width, height = page_size
    if not (is_plain_int(width) and is_plain_int(height) and width > 0 and height > 0):
        raise SchemaRefusal(f"page size {page_size!r} is not two positive integers")
    roster = _checked_roster(roster)
    witnesses = _checked_witnesses(
        witnesses, _ASSEMBLED_WITNESS_FIELDS, witness_regime, roster, no_testimony=no_testimony
    )
    shown = _shown_chairs(switches["witnesses"], roster)
    ordered = sorted(
        (witness for witness in witnesses if witness["chair"] in shown),
        key=lambda witness: witness["witness_label"],
    )
    if len(ordered) > len(WITNESS_LETTERS):
        raise SchemaRefusal(
            f"{len(ordered)} witnesses are shown, more than the {len(WITNESS_LETTERS)} letters "
            "a witness may take (every capital but L and S)"
        )
    rows = [
        _witness_row(letter, witness, switches=switches, regime=witness_regime, page_size=page_size)
        for letter, witness in zip(WITNESS_LETTERS, ordered, strict=False)
    ]
    feed: dict[str, Any] = {
        "schema": SCHEMA,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "page_size": {"w": width, "h": height},
        "reading_unit": READING_UNIT,
        "witness_regime": witness_regime,
        "switches": switches,
        "page_render": _checked_page_render(page_render, switches["page_image"], page_size),
        "overlay": None,
        "witness_testimony": TESTIMONY_NONE if no_testimony else TESTIMONY_PRESENT,
        "witnesses": rows,
        "surya": _surya(surya, switches, page_size),
    }
    feed["answer_measure"] = answer_measure(
        [
            (witness["adapter"], witness["units"])
            for witness in ordered
            if witness["outcome"] == READ_OUTCOME
        ],
        surya_blocks=0 if feed["surya"] is None else len(feed["surya"]["blocks"]),
    )
    if switches["page_overlay"] != "off":
        if page_render_bytes is None:
            raise SchemaRefusal(
                "the sealed feed draws the page overlay, but the page render's bytes were not given"
            )
        feed["overlay"] = page_overlay.overlay_record(
            page_render_bytes, page_overlay.overlay_plan(feed)
        )
    feed["prompt"] = (
        None
        if serving_recipe is None or shows_nothing(feed)
        else page_prompt.page_prompt_evidence(serving_recipe, feed)
    )
    feed["feed_digest"] = digest_of(feed)
    return feed


def shows_nothing(feed: dict[str, Any]) -> bool:
    """Whether the feed shows no page image, no witness text and no detection.

    A reading would have nothing to be made from, so no request is made: the
    page is held by name.
    """
    surya = feed["surya"]
    return feed["page_render"] is None and not (
        any(unit["text"] for row in feed["witnesses"] for unit in row["units"])
        or (surya is not None and (surya["lines"] or surya["blocks"]))
    )


def request_image_sizes(feed: dict[str, Any]) -> list[tuple[int, int]]:
    """The `(width, height)` of each image the page request sends, in order.

    The page render when shown, then the overlay when drawn; what
    `request_capacity.page_request_capacity` charges.
    """
    sizes = []
    if feed["page_render"] is not None:
        dimensions = feed["page_render"]["transform"]["target_dimensions"]
        sizes.append((dimensions["w"], dimensions["h"]))
    if feed["overlay"] is not None:
        sizes.append((feed["overlay"]["dimensions"]["w"], feed["overlay"]["dimensions"]["h"]))
    return sizes


def verify_feed_digest(feed: dict[str, Any]) -> None:
    """Refuse a feed whose recorded digest is not the digest of its other fields."""
    body = {key: value for key, value in feed.items() if key != "feed_digest"}
    if feed.get("feed_digest") != digest_of(body):
        raise SchemaRefusal("a page feed's feed_digest is not the digest of its contents")
