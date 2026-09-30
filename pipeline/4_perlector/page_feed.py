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
  A blank-page or malformed-bbox block is a unit with no box.
* Churro (`churro.v1`): the non-blank lines of its parsed document text in
  document order (`churro_document`), labelled with the `Header`/`Body`/`Footer`
  section they sit in, no box: Churro reports no coordinates.
* DAI (`dai.v1`): one unit per record its own detector found
  (`unit_captures`), box = that record's bounds, text = DAI's response for it
  decoded exactly, and checked against the span the record states.

A witness whose page outcome is not `read` is a row with its outcome and no
units: it is never silently absent.

## Calling it (stage 4, page path)

    feed = build_page_feed(
        page_id=..., page_ordinal=..., page_size=(width, height),
        feed_switches=protocol_config["feed"], witness_regime="named" | "blinded",
        witnesses=[{"chair", "witness_label", "adapter", "testimonium",
                    "testimonium_ref"}, ...],   # one per configured page witness
        surya={"census_ref", "lines": [{"box_px", "ref"}],
               "blocks": [{"box_px", "label", "position", "ref"}]} | None,
        page_render=dossier.build_page_render(...) | None,
        serving_recipe=chair.serving_recipe,
        read_bytes=context.tree.read_bytes,
    )
    text = page_prompt.build_page_prompt(chair.serving_recipe, feed)
    overlay = page_overlay.overlay_image(feed, context.tree.read_bytes)  # when drawn
    images = request_image_sizes(feed)   # what the capacity check charges

Each `testimonium` must already have passed the stage's own page-Testimonium
checks (`run.validate_page_testimonium_record`); this module re-derives units
from its retained bytes but does not re-run those checks. Every ref on the
feed (`testimonium_ref`, Surya's `census_ref` and each line's and block's
`ref`) is an input the caller binds on the page-feed record.

`assemble_page_feed` takes the same arguments with each witness's units
already read (`{chair, witness_label, adapter, outcome, testimonium_ref,
units}`) and no `read_bytes`; `build_page_feed` is `witness_units` then
`assemble_page_feed`, and measurement tools call it directly.
"""

from __future__ import annotations

import re
import string
from fractions import Fraction
from typing import Any, Callable, Final

import page_overlay
import page_prompt
import protocol

from common.chandra_layout import block_page_bounds, is_refusal, parse_layout_html
from common.contracts.canonical import digest_of, is_plain_int
from common.contracts.envelope import digest_ref, read_verified
from common.contracts.errors import SchemaRefusal
from common.native_witness import (
    churro_capture_system_prompt,
    parse_churro_response,
    validate_native_capture,
    verify_native_capture_bytes,
)
from common.witness_regime import BLINDED, NAMED, REGIMES

SCHEMA: Final = "perlector-page-feed.v1"
READING_UNIT: Final = "page"
# The outcome under which a witness's units are shown.
READ_OUTCOME: Final = "read"
BOX_SCALE: Final = 1000

CHANDRA: Final = "chandra.v1"
CHURRO: Final = "churro.v1"
DAI: Final = "dai.v1"
# How finely each adapter's own units cut a page: a layout block or detector
# record is about one act; a line is a fraction of one. The answer reserve
# counts act entries from the coarser kind (`answer_measure`).
UNIT_KINDS: Final = {CHANDRA: "layout-block", DAI: "detector-record", CHURRO: "line"}
# A witness shown `flat` has one unit, its whole page text.
FLAT_UNIT_KIND: Final = "page-text"
_ACT_SIZED_UNIT_KINDS: Final = frozenset({"layout-block", "detector-record"})

_WITNESS_FIELDS: Final = frozenset(
    {"chair", "witness_label", "adapter", "testimonium", "testimonium_ref"}
)
_ASSEMBLED_WITNESS_FIELDS: Final = frozenset(
    {"chair", "witness_label", "adapter", "outcome", "testimonium_ref", "units"}
)
_SURYA_FIELDS: Final = frozenset({"census_ref", "lines", "blocks"})
_SURYA_LINE_FIELDS: Final = frozenset({"box_px", "ref"})
_SURYA_BLOCK_FIELDS: Final = frozenset({"box_px", "label", "position", "ref"})
_BOX_FIELDS: Final = frozenset({"x", "y", "w", "h"})
_NON_BLANK_LINE: Final = re.compile(r"[^\n]+")
_ASCII_WHITESPACE: Final = " \t\n\r\f\v"


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


def _unit(ordinal: int, box_px: dict[str, int] | None, label: str | None, text: str) -> dict:
    return {"ordinal": ordinal, "box_px": box_px, "label": label, "text": text}


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
    capture = validate_native_capture(capture)
    if capture["adapter"] != adapter:
        raise SchemaRefusal(
            f"a page Testimonium's native capture names adapter {capture['adapter']!r}, not "
            f"the chair's configured {adapter!r}"
        )
    raw = read_verified(read_bytes, capture["raw_response_ref"], f"a {adapter} raw response")
    return verify_native_capture_bytes(capture, raw), raw


def _chandra_units(
    payload: dict[str, Any], page_size: tuple[int, int], read_bytes: Callable[[str], bytes]
) -> list[dict[str, Any]]:
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
    if (
        capture["parse"].get("state") == "parsed"
        and capture["parse"]["text"] != parsed["page_text"]
    ):
        raise SchemaRefusal(
            "a Chandra page capture's parsed text differs from its retained raw response"
        )
    return [
        _unit(
            block["ordinal"],
            block_page_bounds(block, page_size=page_size),
            block["label"] if block["label_declared"] else None,
            block["text"],
        )
        for block in parsed["blocks"]
    ]


def _churro_units(
    payload: dict[str, Any], read_bytes: Callable[[str], bytes]
) -> list[dict[str, Any]]:
    capture, raw = _native_capture_bytes(payload, CHURRO, read_bytes)
    document = parse_churro_response(raw, system_prompt=churro_capture_system_prompt(capture))
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
    return units


def _dai_units(
    payload: dict[str, Any], page_size: tuple[int, int], read_bytes: Callable[[str], bytes]
) -> list[dict[str, Any]]:
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
    units = []
    for position, (capture, item) in enumerate(zip(captures, observed, strict=True)):
        if capture is None:
            raise SchemaRefusal(
                f"a DAI page Testimonium read as `read` has no response for record {position}"
            )
        capture = validate_native_capture(capture)
        if capture["adapter"] != DAI:
            raise SchemaRefusal(f"a DAI unit capture names adapter {capture['adapter']!r}")
        raw = read_verified(read_bytes, capture["raw_response_ref"], "a DAI raw response")
        try:
            # DAI's own `text` parser: the response decoded exactly, nothing rewritten.
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SchemaRefusal(f"a DAI response is not UTF-8 text: {error}") from error
        span = item.get("span") if isinstance(item, dict) else None
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
        units.append(
            _unit(
                item["ordinal"],
                _checked_box(item.get("bounds"), page_size, "a DAI record box"),
                None,
                text,
            )
        )
    return units


def witness_units(
    testimonium: dict[str, Any],
    *,
    adapter: str,
    page_size: tuple[int, int],
    read_bytes: Callable[[str], bytes],
) -> list[dict[str, Any]]:
    """One witness's page broken into its own units, or `[]` unless its outcome is `read`.

    `testimonium` is the sealed `page-testimonium` record, `adapter` the chair's
    configured witness adapter, `page_size` the sealed page's `(width, height)`
    and `read_bytes` the run tree's reader. Each unit is `{ordinal, box_px,
    label, text}`, `box_px` being a sealed-page `{x, y, w, h}` or `None`.
    """
    if testimonium.get("outcome") != READ_OUTCOME:
        return []
    payload = testimonium.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a page Testimonium has no payload to read units from")
    if adapter == CHANDRA:
        return _chandra_units(payload, page_size, read_bytes)
    if adapter == CHURRO:
        return _churro_units(payload, read_bytes)
    if adapter == DAI:
        return _dai_units(payload, page_size, read_bytes)
    raise SchemaRefusal(
        f"witness adapter {adapter!r} has no page-unit reader; its page cannot be shown in its "
        f"own units (the readers are {sorted(UNIT_KINDS)})"
    )


# --- the feed -------------------------------------------------------------------


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


def _checked_witnesses(witnesses: Any, fields: frozenset[str], regime: str) -> list[dict]:
    if not isinstance(witnesses, list):
        raise SchemaRefusal("the page feed's witnesses are not a list")
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
    return witnesses


def _witness_row(
    letter: str,
    witness: dict[str, Any],
    *,
    switches: dict[str, Any],
    regime: str,
    page_size: tuple[int, int],
) -> dict[str, Any]:
    units = witness["units"]
    if not isinstance(units, list):
        raise SchemaRefusal(f"shown witness {witness['witness_label']!r} carries no units")
    if witness["outcome"] != READ_OUTCOME and units:
        raise SchemaRefusal("a witness that did not read carries units")
    if switches["witness_units"] == "flat" and witness["outcome"] == READ_OUTCOME:
        # One unit: the witness's units joined in its own order, placed nowhere.
        units = [_unit(1, None, None, "\n".join(unit["text"] for unit in units if unit["text"]))]
    shown = []
    for number, unit in enumerate(units, start=1):
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
                if box_px is not None and switches["witness_coordinates"]
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
        "unit_kind": FLAT_UNIT_KIND
        if switches["witness_units"] == "flat"
        else UNIT_KINDS[witness["adapter"]],
        "outcome": witness["outcome"],
        "testimonium_ref": digest_ref(witness["testimonium_ref"], "a page Testimonium reference"),
        "units": shown,
    }


def answer_measure(rows: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, int]:
    """What the page's answer is reserved on, from the shown witnesses' own units.

    `rows` is `(adapter, own units)` per shown witness whose outcome is `read`.
    The answer transcribes the same ink the witnesses read, so its text is
    measured by the longest witness text; its entries by the unit count of the
    witness whose units are about one act each (a layout block, a detector
    record), or, where only line-level witnesses were shown, by their line
    count, which over-reserves rather than under. Taken before `flat` joins the
    units, so the reserve does not move with how the units are shown.
    """
    longest = max((sum(len(unit["text"]) for unit in units) for _adapter, units in rows), default=0)
    act_sized = [
        len(units) for adapter, units in rows if UNIT_KINDS[adapter] in _ACT_SIZED_UNIT_KINDS
    ]
    lines = [
        len(units) for adapter, units in rows if UNIT_KINDS[adapter] not in _ACT_SIZED_UNIT_KINDS
    ]
    return {
        "longest_witness_characters": longest,
        "act_entries": max(act_sized) if act_sized else max(lines, default=0),
    }


def _surya(surya: Any, switches: dict[str, Any], page_size: tuple[int, int]) -> dict | None:
    if not switches["surya_lines"] and not switches["surya_blocks"]:
        return None
    if not isinstance(surya, dict) or set(surya) != _SURYA_FIELDS:
        raise SchemaRefusal(
            "the sealed feed shows Surya's detections, but no Surya census of "
            f"{sorted(_SURYA_FIELDS)} was given for this page"
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
                "ref": digest_ref(block["ref"], "a Surya block reference"),
            }
        )
    return {
        "census_ref": digest_ref(surya["census_ref"], "the Surya page census reference"),
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


def build_page_feed(
    *,
    page_id: str,
    page_ordinal: int,
    page_size: tuple[int, int],
    feed_switches: dict[str, Any],
    witness_regime: str,
    witnesses: list[dict[str, Any]],
    surya: dict[str, Any] | None,
    page_render: dict[str, Any] | None,
    serving_recipe: str,
    read_bytes: Callable[[str], bytes],
) -> dict[str, Any]:
    """The `perlector-page-feed.v1` payload for one page, deterministic from sealed inputs.

    `witnesses` is every configured page witness's Testimonium for this page, in
    any order: `{chair, witness_label, adapter, testimonium, testimonium_ref}`.
    Units are read from the retained bytes of the shown witnesses only; the rest
    is `assemble_page_feed`.
    """
    switches = protocol.validate_feed_table(feed_switches)
    witnesses = _checked_witnesses(witnesses, _WITNESS_FIELDS, witness_regime)
    shown = _shown_chairs(switches["witnesses"], [witness["chair"] for witness in witnesses])
    render_bytes = None
    if switches["page_overlay"] != "off" and isinstance(page_render, dict):
        render_bytes = read_verified(
            read_bytes,
            {"relative_path": page_render["image_path"], "sha256": page_render["image_sha256"]},
            "the page render",
        )
    return assemble_page_feed(
        page_id=page_id,
        page_ordinal=page_ordinal,
        page_size=page_size,
        feed_switches=switches,
        witness_regime=witness_regime,
        witnesses=[
            {
                "chair": witness["chair"],
                "witness_label": witness["witness_label"],
                "adapter": witness["adapter"],
                "outcome": witness["testimonium"].get("outcome"),
                "testimonium_ref": witness["testimonium_ref"],
                "units": witness_units(
                    witness["testimonium"],
                    adapter=witness["adapter"],
                    page_size=page_size,
                    read_bytes=read_bytes,
                )
                if witness["chair"] in shown
                else None,
            }
            for witness in witnesses
        ],
        surya=surya,
        page_render=page_render,
        serving_recipe=serving_recipe,
        page_render_bytes=render_bytes,
    )


def assemble_page_feed(
    *,
    page_id: str,
    page_ordinal: int,
    page_size: tuple[int, int],
    feed_switches: dict[str, Any],
    witness_regime: str,
    witnesses: list[dict[str, Any]],
    surya: dict[str, Any] | None,
    page_render: dict[str, Any] | None,
    serving_recipe: str,
    page_render_bytes: bytes | None = None,
) -> dict[str, Any]:
    """The feed from witness units already read, for `build_page_feed` and for measurement.

    `witnesses` is every configured page witness, in any order: `{chair,
    witness_label, adapter, outcome, testimonium_ref, units}`, `units` being
    `witness_units`' list for a shown witness (`None` is allowed for one the
    switch hides). The `witnesses` switch picks which are shown; shown ones get
    letters in sorted `witness_label` order. `surya` is the page's Surya census
    as `{census_ref, lines: [{box_px, ref}] in Surya's order, blocks: [{box_px,
    label, position, ref}]}`, `position` being Surya's reading order; it may be
    `None` only when both Surya switches are off. `page_render` is what
    `dossier.build_page_render` returned for the `page_image` switch, or `None`
    when it is off. `page_render_bytes` are the render's bytes, needed only
    when `page_overlay` is on, to draw the overlay and seal its digest.

    Returns `{schema, page_id, page_ordinal, page_size: {w, h}, reading_unit,
    witness_regime, switches, page_render, overlay, witnesses, surya,
    answer_measure, prompt, feed_digest}`; `overlay` is `None` when the switch
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
    witnesses = _checked_witnesses(witnesses, _ASSEMBLED_WITNESS_FIELDS, witness_regime)
    shown = _shown_chairs(switches["witnesses"], [witness["chair"] for witness in witnesses])
    ordered = sorted(
        (witness for witness in witnesses if witness["chair"] in shown),
        key=lambda witness: witness["witness_label"],
    )
    if len(ordered) > len(string.ascii_uppercase):
        raise SchemaRefusal(f"{len(ordered)} witnesses are more than one letter each can name")
    rows = [
        _witness_row(letter, witness, switches=switches, regime=witness_regime, page_size=page_size)
        for letter, witness in zip(string.ascii_uppercase, ordered, strict=False)
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
        "witnesses": rows,
        "surya": _surya(surya, switches, page_size),
        "answer_measure": answer_measure(
            [
                (witness["adapter"], witness["units"])
                for witness in ordered
                if witness["outcome"] == READ_OUTCOME
            ]
        ),
    }
    if switches["page_overlay"] != "off":
        if page_render_bytes is None:
            raise SchemaRefusal(
                "the sealed feed draws the page overlay, but the page render's bytes were not given"
            )
        feed["overlay"] = page_overlay.overlay_record(
            page_render_bytes, page_overlay.overlay_plan(feed)
        )
    feed["prompt"] = page_prompt.page_prompt_evidence(serving_recipe, feed)
    feed["feed_digest"] = digest_of(feed)
    return feed


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
