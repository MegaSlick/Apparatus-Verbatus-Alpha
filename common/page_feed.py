"""The Perlector's page feed: everything one whole-page reading is shown.

The Perlector reads one page per call and
establishes the acts itself. What it is shown is this feed: the page image
(carried beside the text, not in it), each witness's page text broken into
that witness's own units, and Surya's detected lines and blocks. Every input
has a switch in the sealed `[feed]` table (`validate_feed_table`); a
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
the order Surya predicted for them. Neither order follows the page's columns,
so lines and blocks are cited one id at a time.

## Units, re-derived from retained bytes

A unit is `{ordinal, box_px | None, label | None, text}` in the witness's own
order, re-derived from the raw response the page Testimonium retains by
`common.page_witness_units.witness_reading`; its docstring describes each
adapter's units.

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
so an entry's region comes only from placing ids -- Surya's lines and the
units of a boxed witness shown in its own units whose text vouches for their
box -- and two entries
citing the same flat witness never share a region through it.

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
        page_render=page_render.build_page_render(...) | None,
        serving_recipe=chair.serving_recipe,
        read_bytes=context.tree.read_bytes,
    )
    text = page_prompt.build_page_prompt(chair.serving_recipe, feed)
    parts = page_prompt.prompt_parts(chair.serving_recipe, feed)  # what the capacity charges
    overlay = page_overlay.overlay_image(feed, context.tree.read_bytes)  # when drawn
    images = request_image_sizes(feed)   # what the capacity check charges
    boxes = page_accounting.placement_boxes(feed, policy)  # each id's box for an entry's region
    nothing = shows_nothing(feed)        # a feed a reading could not be made from

Each `testimonium` must already have passed the page-Testimonium checks
(`common.page_testimonia.validate_page_testimonium_record` and
`verify_page_native_capture`); this module re-derives units
from its retained bytes but does not re-run those checks. It does check that
each `testimonium` is the record its `testimonium_ref` names: the ref's bytes,
read digest-checked, decode to exactly that record, a `page-testimonium` of
this page whose payload names the row's chair; no two rows may share a ref. Every ref on the feed (`testimonium_ref`, Surya's `census_ref` and
each line's and block's `ref`) is an input the caller binds on the page-feed
record.

A page with no page Testimonium at all (the sealed roster seats no page
witness, or the Attestatores recorded none for it) is built with
`no_testimony=True` and no witnesses: its feed records `witness_testimony: "none"` and no row, so the
absence is stated rather than silent. A roster chair missing beside others
that did testify is still refused.

Surya's census states how its blocks were sequenced, recorded on the feed as
`block_sequence`: `surya-order-head` (Surya's reading-order model placed them)
or `raster-fallback` (Surya sorted them top to bottom, then left to right, and
`block_sequence_reason` says why). The prompt says "raster order" rather than
"reading order" for a fallback, and the feed names it `block_sequence` for the
same reason: a raster fallback is not a reading order. Each line's and block's `confidence_bp` is recorded on the feed and never rendered.

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
from fractions import Fraction
from typing import Any, Callable, Final

from common import page_overlay, page_prompt
from common.contracts.canonical import digest_of, is_plain_int
from common.contracts.envelope import digest_ref, read_verified
from common.contracts.errors import ContractError, SchemaRefusal
from common.native_witness import REPETITION_FINDING_KINDS
from common.page_path import (
    PAGE_TESTIMONIUM_KIND,
    SURYA_ORDER_HEAD,
    SURYA_RASTER_FALLBACK,
)
from common.page_witness_units import (
    NO_ANSWER_HEALTH,
    READ_OUTCOME,
    UNIT_KINDS,
    WITNESS_LETTERS,
    checked_box,
    witness_reading,
)
from common.witness_regime import BLINDED, NAMED, REGIMES

SCHEMA: Final = "perlector-page-feed.v2"
BOX_SCALE: Final = 1000

# The Perlector protocol's table holding the feed switches.
FEED_TABLE: Final = "feed"
# The sealed `[feed]` table's switches. Closed: every key required,
# every value one of the listed ones, so a run cannot read under a switch this
# build does not apply.
PAGE_IMAGE_SETTINGS: Final = frozenset({"legible", "full", "off"})
WITNESS_UNIT_SETTINGS: Final = frozenset({"own", "flat"})
ALL_WITNESSES: Final = "all"
# "boxes" adds a second image: a copy of the page render with every shown boxed
# candidate outlined and labelled with its id (`page_overlay.py`).
PAGE_OVERLAY_SETTINGS: Final = frozenset({"off", "boxes"})
_FEED_FIELDS: Final = frozenset(
    {
        "page_image",
        "witnesses",
        "witness_units",
        "witness_coordinates",
        "surya_lines",
        "surya_blocks",
        "page_overlay",
    }
)
_FEED_BOOLEAN_FIELDS: Final = ("witness_coordinates", "surya_lines", "surya_blocks")
# The reading names its page type and each entry's kind (`common.page_types`).
# The switch is required and "named" is its only value, so every feed record
# says what the reader was asked.
PAGE_TYPES_FIELD: Final = "page_types"
PAGE_TYPES_SETTINGS: Final = frozenset({"named"})

# The unit kinds about one act in size (`UNIT_KINDS`); the answer reserve counts
# act entries from these only (`answer_measure`).
_ACT_SIZED_UNIT_KINDS: Final = frozenset({"layout-block", "detector-record"})

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
_UNIT_FIELDS: Final = frozenset({"ordinal", "box_px", "label", "text"})
_SURYA_FIELDS: Final = frozenset(
    {"census_ref", "block_sequence", "block_sequence_reason", "lines", "blocks"}
)
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


# --- the sealed feed switches ----------------------------------------------------


def validate_feed_table(table: Any) -> dict[str, Any]:
    """The sealed `[feed]` table, checked closed and returned as a copy.

    `witnesses` is `"all"` or a list of distinct chair names; whether each name
    is in the run's sealed roster is checked where the roster is known
    (`build_page_feed`).
    """
    where = f"the Perlector protocol declaration's [{FEED_TABLE}]"
    if not isinstance(table, dict) or set(table) != _FEED_FIELDS | {PAGE_TYPES_FIELD}:
        raise ContractError(
            f"{where} is not its closed schema {sorted(_FEED_FIELDS | {PAGE_TYPES_FIELD})}; "
            "a feed switch this build does not read cannot be applied"
        )
    if table[PAGE_TYPES_FIELD] not in PAGE_TYPES_SETTINGS:
        raise ContractError(
            f"{where} {PAGE_TYPES_FIELD} {table[PAGE_TYPES_FIELD]!r} is not one of "
            f"{sorted(PAGE_TYPES_SETTINGS)}"
        )
    if table["page_image"] not in PAGE_IMAGE_SETTINGS:
        raise ContractError(
            f"{where} page_image {table['page_image']!r} is not one of "
            f"{sorted(PAGE_IMAGE_SETTINGS)}"
        )
    if table["witness_units"] not in WITNESS_UNIT_SETTINGS:
        raise ContractError(
            f"{where} witness_units {table['witness_units']!r} is not one of "
            f"{sorted(WITNESS_UNIT_SETTINGS)}"
        )
    if table["page_overlay"] not in PAGE_OVERLAY_SETTINGS:
        raise ContractError(
            f"{where} page_overlay {table['page_overlay']!r} is not one of "
            f"{sorted(PAGE_OVERLAY_SETTINGS)}"
        )
    if table["page_overlay"] != "off" and table["page_image"] == "off":
        raise ContractError(
            f"{where} page_overlay draws on a copy of the page render, but page_image is off"
        )
    for field in _FEED_BOOLEAN_FIELDS:
        if not isinstance(table[field], bool):
            raise ContractError(f"{where} {field} is not true or false")
    witnesses = table["witnesses"]
    if witnesses != ALL_WITNESSES and (
        not isinstance(witnesses, list)
        or not all(isinstance(chair, str) and chair.strip() for chair in witnesses)
        or len(set(witnesses)) != len(witnesses)
    ):
        raise ContractError(
            f"{where} witnesses is neither {ALL_WITNESSES!r} nor a list of distinct chair names"
        )
    return {**table, "witnesses": witnesses if witnesses == ALL_WITNESSES else list(witnesses)}


# --- geometry -------------------------------------------------------------------


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
    if switch == ALL_WITNESSES:
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
            None if unit["box_px"] is None else checked_box(unit["box_px"], page_size, "a unit box")
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
    rows: list[tuple[str, list[dict[str, Any]]]], *, surya_blocks: int, surya_lines: int
) -> dict[str, int]:
    """What the page's answer is reserved on, from what the feed shows of the page.

    `rows` is `(adapter, own units)` per shown witness whose outcome is `read`,
    `surya_blocks` the number of Surya blocks shown and `surya_lines` the
    number of Surya lines shown. The answer transcribes
    the same ink the witnesses read, so its text is measured by the longest
    witness text, text outside its units included. Its entries are the page's
    likely act count: the most of Surya's blocks, DAI's detector records and
    Chandra's layout blocks, each about one act. A line witness's lines are
    fractions of acts and are not counted. With none of the three shown the
    count is 0 and only the text is reserved. Every line shown is cited or
    set aside by its own id, so each is one more cite. The reserve decides
    admission, and the request is sent the page cap or the room left,
    whichever is less.
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
        "surya_lines": surya_lines,
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
        (order == SURYA_ORDER_HEAD and reason is None)
        or (order == SURYA_RASTER_FALLBACK and isinstance(reason, str) and reason.strip())
    ):
        raise SchemaRefusal(
            f"Surya's block_sequence is not {SURYA_ORDER_HEAD!r} with no reason or "
            f"{SURYA_RASTER_FALLBACK!r} with one"
        )
    lines = surya["lines"] if switches["surya_lines"] else []
    blocks = surya["blocks"] if switches["surya_blocks"] else []
    if not isinstance(lines, list) or not isinstance(blocks, list):
        raise SchemaRefusal("Surya's lines and blocks are not lists")
    shown_lines = []
    for number, line in enumerate(lines, start=1):
        if not isinstance(line, dict) or set(line) != _SURYA_LINE_FIELDS:
            raise SchemaRefusal(f"a Surya line is not exactly {sorted(_SURYA_LINE_FIELDS)}")
        box = checked_box(line["box_px"], page_size, "a Surya line box")
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
        box = checked_box(block["box_px"], page_size, "a Surya block box")
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
    """The `perlector-page-feed.v2` payload for one page, deterministic from sealed inputs.

    `roster` is the sealed page-witness roster (chair names) and `witnesses`
    one Testimonium per roster chair for this page, in any order: `{chair,
    witness_label, adapter, testimonium, testimonium_ref}`. Each testimonium is
    checked to be exactly the record its ref's bytes hold. Units are read from
    the retained bytes of the shown witnesses only; the rest is
    `assemble_page_feed`.
    """
    switches = validate_feed_table(feed_switches)
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
    `common.page_render.build_page_render` returned for the `page_image` switch, or `None`
    when it is off. `page_render_bytes` are the render's bytes, needed only
    when `page_overlay` is on, to draw the overlay and seal its digest.

    Returns `{schema, page_id, page_ordinal, page_size: {w, h},
    witness_regime, switches, page_render, overlay, witness_testimony,
    witnesses, surya, answer_measure, prompt, feed_digest}`; `prompt` is `None`
    when `serving_recipe` is `None` or the feed shows nothing; `overlay` is `None` when the switch
    is off, else `page_overlay.overlay_record`'s `{source_image_sha256,
    dimensions, label_scale, colours, drawn: [{id, source, box}],
    renderer_sha256, image_sha256}`; `feed_digest` is the digest of every other
    field.
    """
    switches = validate_feed_table(feed_switches)
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
        surya_lines=0 if feed["surya"] is None else len(feed["surya"]["lines"]),
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
