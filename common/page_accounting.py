"""The model-free check that nothing on a page was missed by its page reading.

A page reading is one Perlector answer for a whole page: entries (`act` or
`other`) that cite the candidate ids of the page feed -- witness units `A1..`,
Surya lines `L1..` and blocks `S1..` -- plus the ids it set aside with a reason.
This module takes that answer, the feed and the page's sealed witnesses and
detections as plain data and says, rule by rule, whether every witness unit,
every detected line, every detector record, every witness's text and the
page's ink are accounted for. It reads no file, calls no model and chooses nothing among the
witnesses. A hold only asks a human to look; a measurement that cannot be
taken holds; any hold holds the page's acts for review.

`placement_boxes`, `expand_cites`, `validate_answer` and `duplicate_regions`
are the one reading of an answer's ids and regions that both this check and
the stage writing the act-region records use, under the sealed policy, and
`validate_answer` reads the answer's grammar through `common.page_answer`, the
one grammar a page answer has.

An entry's region is exactly the ink it names, id by id: the list of its placing
boxes, never the rectangle around them. Every "inside" test and every region
area reads the union of that list, so an entry citing lines of two columns does
not claim the ink between them, and a Surya block, which may hold the whole page
or several acts, lends its area to no entry.

A page may be asked once more about the ids its first reading left
unaccounted for (`common/page_reask.py`). That second reading's accounting
measures both readings together: the first reading's entries exactly as they
were, then the re-ask's, numbered on after them. Rule (j) says what became of
the re-ask; nothing of the first reading is changed, dropped or out-counted.

Every box in and out is the repository's `bounds` `{x, y, w, h}` in sealed-page
pixels, as the feed records `box_px`; the geometry below reads corners from it.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from bisect import bisect_left
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final, Protocol

from common.contracts.errors import ContractError
from common.contracts.outcomes import WITNESS_READING_OUTCOMES
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from common.imaging import Bounds
from common.page_answer import grammar_problems
from common.page_edges import FIRST_READING, OPERATOR_REREAD_FIRST, REASK_READING
from common.page_witness_units import DETECTION_LETTERS
from common.perlector_audit import TRUNCATION_COMPLETE
from common.residual_ink import CoverageAuditPolicy, residual_ink_from_runs
from common.sealed_config import read_sealed_toml

SCHEMA: Final = "page-accounting.v2"
SEALED_CONFIG_NAME: Final = "page-accounting"
DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "page_accounting.toml"
)
BASIS_POINTS: Final = 10_000

# Answer problems, recorded on the page reading and held by the rule named in
# `_PROBLEM_RULE` (rule a when not named). `answer-grammar` carries one problem
# `common.page_answer.grammar_problems` names.
ANSWER_GRAMMAR: Final = "answer-grammar"
UNKNOWN_ID: Final = "unknown-id"
MALFORMED_RANGE: Final = "malformed-range"
# A range over Surya lines or blocks: their numbering follows the detector, not
# the page's columns, so a range could name ink the entry never read. Under a
# policy (`validate_answer`) a range is read when it can name no such ink: a
# block range (blocks place nothing), or a line range whose every line lies
# inside the entry's own cited witness units.
DETECTION_RANGE: Final = "detection-range"
CITED_AND_SET_ASIDE: Final = "cited-and-set-aside"
SET_ASIDE_TWICE: Final = "set-aside-twice"
SET_ASIDE_WITHOUT_REASON: Final = "set-aside-without-reason"
# A re-ask's entry that says it continues across a page break: a re-ask reads
# ids inside the page, so it holds the re-ask whole.
REASK_CONTINUATION: Final = "reask-continuation"

# Finding codes. Every code in `HOLD_CODES` holds the page; the others are recorded only.
PAGE_ANSWER_INCOMPLETE: Final = "page-answer-incomplete"
# Two entries claiming mostly the same ink (`duplicate_regions`): rule (h), and a
# hold on each entry's act records.
DUPLICATE_REGION: Final = "duplicate-region"
READING_UNPLACED: Final = "reading-unplaced"
UNACCOUNTED_WITNESS_UNIT: Final = "unaccounted-witness-unit"
SET_ASIDE_SUBSTANTIAL: Final = "set-aside-substantial"
WITNESS_NOT_READ: Final = "witness-not-read"
WITNESS_READ_NO_UNITS: Final = "witness-read-no-units"
WITNESS_READ_BLANK: Final = "witness-read-blank"
UNREAD_LINE: Final = "unread-line"
UNREAD_LINE_NOT_MEASURED: Final = "unread-line-not-measured"
WITNESS_TEXT_NOT_READ: Final = "witness-text-not-read"
WITNESS_TEXT_NOT_MEASURED: Final = "witness-text-not-measured"
TOO_FEW_DISTINCTIVE_PIECES: Final = "too-few-distinctive-pieces"
UNREAD_INK: Final = "unread-ink"
UNREAD_INK_NOT_MEASURED: Final = "unread-ink-not-measured"
READING_INCOMPLETE: Final = "reading-incomplete"
# An entry with no truncation classification (one citing no boxed id has no
# region to classify over): whether it is complete was not measured.
TRUNCATION_NOT_CLASSIFIED: Final = "truncation-not-classified"
SHARED_LINE: Final = "shared-line"
MERGED_DETECTION: Final = "merged-detection"
RECORD_READ_AS_OTHER: Final = "record-read-as-other"
RECORD_NOT_READ: Final = "record-not-read"
# The record detector looked below its cap and found no record on a page whose
# reading establishes acts: the detector and the reading disagree about the page.
NO_RECORD_ON_ACT_PAGE: Final = "no-detector-record-on-act-page"
SET_ASIDE_RECORD: Final = "set-aside-record"
SPLIT_DETECTION: Final = "split-detection"
RECORDS_NOT_MEASURED: Final = "detector-records-not-measured"
RECORD_NOT_MEASURED: Final = "detector-record-not-measured"
RECORD_DETECTOR_CAPPED: Final = "record-detector-capped"
NO_RECORD_DETECTOR: Final = "no-record-detector"
NO_PARSED_ANSWER: Final = "no-parsed-answer"
# Rule (j), what became of a page's re-ask: a named id it set aside, a re-ask
# that is not a parsed, valid answer finished on `stop`, an entry of it citing
# no placing id or giving no text, an entry whose text one first-reading entry
# already holds, and a duplicate check the work budget ran out on.
REASK_SET_ASIDE: Final = "reask-set-aside"
REASK_UNREAD: Final = "reask-unread"
REASK_UNPLACED: Final = "reask-unplaced"
REASK_DUPLICATE: Final = "reask-duplicate"
# A re-ask entry that gives no text: a named id it cites is accounted for by
# nothing read, so the page holds.
REASK_NO_TEXT: Final = "reask-no-text"
REASK_DUPLICATE_NOT_MEASURED: Final = "reask-duplicate-not-measured"
HOLD_CODES: Final = frozenset(
    {
        PAGE_ANSWER_INCOMPLETE,
        UNKNOWN_ID,
        READING_UNPLACED,
        UNACCOUNTED_WITNESS_UNIT,
        SET_ASIDE_SUBSTANTIAL,
        WITNESS_NOT_READ,
        WITNESS_READ_NO_UNITS,
        UNREAD_LINE,
        UNREAD_LINE_NOT_MEASURED,
        WITNESS_TEXT_NOT_READ,
        WITNESS_TEXT_NOT_MEASURED,
        UNREAD_INK,
        UNREAD_INK_NOT_MEASURED,
        READING_INCOMPLETE,
        TRUNCATION_NOT_CLASSIFIED,
        DUPLICATE_REGION,
        MERGED_DETECTION,
        RECORD_READ_AS_OTHER,
        RECORD_NOT_READ,
        NO_RECORD_ON_ACT_PAGE,
        SET_ASIDE_RECORD,
        RECORDS_NOT_MEASURED,
        RECORD_NOT_MEASURED,
        RECORD_DETECTOR_CAPPED,
        REASK_SET_ASIDE,
        REASK_UNREAD,
        REASK_UNPLACED,
        REASK_DUPLICATE,
        REASK_NO_TEXT,
        REASK_DUPLICATE_NOT_MEASURED,
    }
)
# Findings that say a rule could not be measured: the rule is `not-measured`, and the
# page is held (by the finding's own code, or by rule (a) when there is no answer).
NOT_MEASURED_CODES: Final = frozenset(
    {
        UNREAD_LINE_NOT_MEASURED,
        WITNESS_TEXT_NOT_MEASURED,
        UNREAD_INK_NOT_MEASURED,
        RECORDS_NOT_MEASURED,
        RECORD_NOT_MEASURED,
        RECORD_DETECTOR_CAPPED,
        NO_PARSED_ANSWER,
        TRUNCATION_NOT_CLASSIFIED,
        REASK_DUPLICATE_NOT_MEASURED,
    }
)
_PROBLEM_RULE: Final = {UNKNOWN_ID: "b"}

PASS: Final = "pass"
HOLD: Final = "hold"
NOT_MEASURED: Final = "not-measured"
NOT_APPLICABLE: Final = "not-applicable"
RULES: Final = ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")

PARSED: Final = "parsed"
FAILED_PARSE_STATES: Final = frozenset(
    {"cut-off", "repetition-loop", "call-failed", "refused-capacity", "not-run"}
)
PARSE_STATES: Final = FAILED_PARSE_STATES | {PARSED, "malformed"}
# What an accounting's entries are: one reading's, or a first reading's and its re-ask's.
ANSWER_BASIS_FIRST: Final = "attempt-1"
ANSWER_BASIS_COMBINED: Final = "combined"


def answer_basis(attempt: int) -> str:
    """The `answer_basis` of one whole-page reading's accounting: "attempt-<ordinal>"."""
    return ANSWER_BASIS_FIRST if attempt == FIRST_READING else f"attempt-{attempt}"


RECORD_DETECTOR_CONFIGURED: Final = "configured"
RECORD_DETECTOR_ABSENT: Final = "absent"

_ID: Final = re.compile(r"([A-Z])([1-9][0-9]*)")
_RANGE: Final = re.compile(r"([A-Z])([1-9][0-9]*)-([A-Z])([1-9][0-9]*)")
_DOUBT_MARK: Final = re.compile(r"\[\[([^\[\]]*)\]\]")
# Only well-formed markup is removed: a tag that opens with a name and closes on
# the same `<...>`, and an entity that names a character. A stray `<` is ink.
_TAG: Final = re.compile(r"</?[A-Za-z][A-Za-z0-9:_.-]*(?:\s[^<>]*)?/?>")
_ENTITY: Final = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
# What `[[?]]` becomes in a normalized reading: one mark that stands for ink the
# reader flagged unreadable. A private-use character, so no letter or digit of
# any witness can equal it.
UNREADABLE: Final = ""
_WITNESS_KEYS: Final = frozenset({"letter", "outcome", "blank", "units"})
_DETECTIONS_KEYS: Final = frozenset({"surya", "records", "record_detector", "record_census"})
_SURYA_KEYS: Final = frozenset({"lines", "blocks"})
_RECORD_CENSUS_KEYS: Final = frozenset({"detection_count", "max_det", "max_det_reached"})
_BOX_KEYS: Final = frozenset({"x", "y", "w", "h"})
WITNESS_UNITS_FLAT: Final = "flat"

Box = Bounds


@dataclass(frozen=True)
class PageAccountingPolicy:
    """The sealed thresholds, with the digest of the bytes they came from."""

    inside_min_area_bp: int
    max_unread_characters: int
    max_set_aside_characters: int
    max_unread_share_bp: int
    min_block_characters: int
    anchor_characters: int
    anchor_neighbours: int
    anchor_offset_tolerance: int
    band_slack: int
    direct_alignment_max_pairs: int
    max_alignment_pairs: int
    max_characters: int
    max_alignment_steps: int
    piece_characters: int
    piece_edits: int
    window_slack: int
    min_pieces: int
    min_distinctive_share_bp: int
    max_short_unit_distance_bp: int
    max_shared_share_bp: int
    max_unit_area_per_character_bp: int
    max_act_doubt_share_bp: int
    max_page_doubt_share_bp: int
    sha256: str


_POLICY_TABLES: Final = {
    "inside": ("min_area_bp",),
    "witness_text": (
        "max_unread_characters",
        "max_set_aside_characters",
        "max_unread_share_bp",
        "min_block_characters",
    ),
    "alignment": (
        "anchor_characters",
        "anchor_neighbours",
        "anchor_offset_tolerance",
        "band_slack",
        "direct_alignment_max_pairs",
        "max_alignment_pairs",
        "max_characters",
        "max_alignment_steps",
    ),
    "identity": (
        "piece_characters",
        "piece_edits",
        "window_slack",
        "min_pieces",
        "min_distinctive_share_bp",
        "max_short_unit_distance_bp",
    ),
    "region": ("max_shared_share_bp", "max_unit_area_per_character_bp"),
    # Read by `page_path.entry_plans`, which holds each entry and page over them.
    "doubt": ("max_act_doubt_share_bp", "max_page_doubt_share_bp"),
}
_BASIS_POINT_FIELDS: Final = frozenset(
    {
        "min_area_bp",
        "max_unread_share_bp",
        "min_distinctive_share_bp",
        "max_short_unit_distance_bp",
        "max_shared_share_bp",
        "max_unit_area_per_character_bp",
        "max_act_doubt_share_bp",
        "max_page_doubt_share_bp",
    }
)


def load_page_accounting_policy(
    path: str | Path = DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH,
) -> PageAccountingPolicy:
    """Read the closed, sealed page-accounting configuration."""
    record, digest = read_sealed_toml(path, "page accounting configuration")
    if set(record) != set(_POLICY_TABLES) or any(
        not isinstance(record[table], dict) or set(record[table]) != set(fields)
        for table, fields in _POLICY_TABLES.items()
    ):
        raise ContractError("page accounting configuration has the wrong closed schema")
    values = {
        field: record[table][field] for table, fields in _POLICY_TABLES.items() for field in fields
    }
    for field, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ContractError(f"page accounting {field} must be a positive integer")
        if field in _BASIS_POINT_FIELDS and value > BASIS_POINTS:
            raise ContractError(f"page accounting {field} must be in 1..10000")
    if values["direct_alignment_max_pairs"] > values["max_alignment_pairs"]:
        raise ContractError(
            "page accounting direct_alignment_max_pairs exceeds max_alignment_pairs"
        )
    inside = values.pop("min_area_bp")
    return PageAccountingPolicy(inside_min_area_bp=inside, **values, sha256=digest)


class _SealedContext(Protocol):
    def require_sealed_config(self, name: str, observed_sha256: str) -> None: ...


def require_page_accounting_policy(
    context: _SealedContext, path: str | Path = DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH
) -> PageAccountingPolicy:
    """The policy at `path`, refused unless its bytes are the ones this run sealed.

    The stage that publishes a `page-accounting` record reads its policy here, at
    the point of use, so a configuration edited after the run was bound is
    refused by name rather than silently applied.
    """
    policy = load_page_accounting_policy(path)
    context.require_sealed_config(SEALED_CONFIG_NAME, policy.sha256)
    return policy


# --- the finish and the re-ask ------------------------------------------------------


def finished_on_stop(reading: Mapping[str, Any]) -> bool:
    """Whether a reading ran to its own end: its `finish_reason` is `stop`.

    The one test of a finish that rule (a), the re-ask's standing (rule (j))
    and the re-ask plan (`common/page_reask.py`) all read.
    """
    return reading["finish_reason"] == "stop"


def reask_stood(accounting: Mapping[str, Any]) -> bool:
    """Whether a page accounting counts its page's re-ask among the entries it measures.

    Only a combined accounting can; it does unless rule (j) holds `reask-unread`.
    """
    return accounting["answer_basis"] == ANSWER_BASIS_COMBINED and all(
        finding["code"] != REASK_UNREAD for finding in accounting["rules"]["j"]["findings"]
    )


# --- candidates and the answer's ids -------------------------------------------------


def _box(value: Any, where: str) -> Box:
    if (
        not isinstance(value, Mapping)
        or set(value) != _BOX_KEYS
        or any(not isinstance(value[k], int) or isinstance(value[k], bool) for k in _BOX_KEYS)
        or value["x"] < 0
        or value["y"] < 0
        or value["w"] <= 0
        or value["h"] <= 0
    ):
        raise ContractError(f"{where} box_px is not {{x, y, w, h}} with x, y >= 0 and w, h > 0")
    return {"x": value["x"], "y": value["y"], "w": value["w"], "h": value["h"]}


def _corners(box: Box) -> tuple[int, int, int, int]:
    """A box's half-open corners `(x0, y0, x1, y1)`, the one form the geometry reads."""
    return box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]


def _from_corners(x0: int, y0: int, x1: int, y1: int) -> Box:
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def feed_items(feed: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Every citable item of a page feed, `(kind, item)`, in feed order.

    `kind` is `unit` for a witness unit and `surya` for a Surya line or block;
    each item carries its `id`, `box_px` and `box_1000` as the feed holds them.
    """
    items: list[tuple[str, Mapping[str, Any]]] = [
        ("unit", unit) for witness in feed["witnesses"] for unit in witness["units"]
    ]
    surya = feed["surya"]
    if surya is not None:
        items += [("surya", item) for item in surya["lines"] + surya["blocks"]]
    return items


def placement_boxes(feed: Mapping[str, Any], policy: PageAccountingPolicy) -> dict[str, Box | None]:
    """Every id a page feed defines, with the box it places an entry by, or `None`.

    Surya's lines and the witness units whose text vouches for their box place
    an entry. A line is text Surya found; rule (d) checks only that each line
    lies inside some entry's region, so whether the entry citing a line read
    it is measured no further than the truncation length signal (rule g) and
    rule (e) where a witness unit covers the same ink. A unit places its box
    only when its text is long enough for that box (`_vouches_for`), since
    rule (e) holds the citing reading to the unit's text and to nothing else
    under its box. A Surya block places nothing: one block can be the whole
    page or hold several acts. Under the feed's `witness_units = "flat"`
    switch a witness is shown as one unit with no box, so its units place
    nothing (their `box_px` stays on the feed as the witness's sealed
    geometry). Every box on the feed is checked, placing or not. An entry
    takes its region from the placing ids it cites. The stage cuts act regions
    from this map and the accounting measures against it, so the two never
    read different regions.
    """
    flat = feed["switches"]["witness_units"] == WITNESS_UNITS_FLAT
    page_area = feed["page_size"]["w"] * feed["page_size"]["h"]
    boxes: dict[str, Box | None] = {}
    for witness in feed["witnesses"]:
        for unit in witness["units"]:
            box = None if unit["box_px"] is None else _box(unit["box_px"], unit["id"])
            places = (
                not flat and box is not None and _vouches_for(unit["text"], box, page_area, policy)
            )
            boxes[unit["id"]] = box if places else None
    surya = feed["surya"]
    if surya is not None:
        for line in surya["lines"]:
            boxes[line["id"]] = _box(line["box_px"], line["id"])
        for block in surya["blocks"]:
            _box(block["box_px"], block["id"])
            boxes[block["id"]] = None
    return boxes


def _vouches_for(text: str, box: Box, page_area: int, policy: PageAccountingPolicy) -> bool:
    """Whether a unit's text is long enough to claim its box's share of the page.

    Each normalized character may claim at most `max_unit_area_per_character_bp`
    of the page's area. A unit with no text claims nothing; a folio number
    reported on a column-sized box claims nothing either, rather than lending
    an entry ink no rule shows it read.
    """
    characters = len(normalized_text(text))
    return (
        characters > 0
        and _area(box) * BASIS_POINTS
        <= policy.max_unit_area_per_character_bp * characters * page_area
    )


def feed_candidates(feed: Mapping[str, Any], policy: PageAccountingPolicy) -> dict[str, Box | None]:
    """Every citable id of a page feed and the box it places an entry by (`placement_boxes`).

    `feed` is the `page-feed` payload: `page_size`, `switches.witness_units`,
    `witnesses[].units[]` with `id`, `box_px` and `text`, and `surya` (or
    `None`) with `lines[]` / `blocks[]` of `id` and `box_px`. Each letter's ids
    are numbered `1..n` without a gap; a feed that skips one is refused, since
    a range citation reads every id between its ends.
    """
    seen: set[str] = set()
    for identifier in (item["id"] for _kind, item in feed_items(feed)):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ContractError(f"feed id {identifier!r} is not a letter and a number")
        if identifier in seen:
            raise ContractError(f"feed id {identifier} appears twice")
        seen.add(identifier)
    candidates = placement_boxes(feed, policy)
    numbers: dict[str, list[int]] = {}
    for identifier in candidates:
        numbers.setdefault(identifier[0], []).append(int(identifier[1:]))
    for letter, found in sorted(numbers.items()):
        if sorted(found) != list(range(1, len(found) + 1)):
            raise ContractError(f"feed ids of letter {letter} are not numbered 1..n without a gap")
    return candidates


def expand_cites(
    cites: Sequence[Any], candidates: Mapping[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """The known ids a list of citations names, in the order given, and its problems.

    A citation is one id (`A2`) or a range of witness units (`A2-A5`: one
    letter, ascending, both ends inclusive). A range over Surya lines or
    blocks is `detection-range`: they are cited one by one, since their
    numbering follows the detector rather than the page's columns. An id or
    range end the feed does not define is `unknown-id`; any other shape is
    `malformed-range`. Nothing is guessed: a problem contributes no id.
    """
    ids: list[str] = []
    problems: list[dict[str, Any]] = []
    for cite in cites:
        if not isinstance(cite, str):
            problems.append({"code": MALFORMED_RANGE, "cite": repr(cite)})
            continue
        if _ID.fullmatch(cite):
            if cite in candidates:
                ids.append(cite)
            else:
                problems.append({"code": UNKNOWN_ID, "id": cite})
            continue
        match = _RANGE.fullmatch(cite)
        if match is None or match[1] != match[3] or int(match[2]) > int(match[4]):
            problems.append({"code": MALFORMED_RANGE, "cite": cite})
            continue
        if match[1] in DETECTION_LETTERS:
            problems.append({"code": DETECTION_RANGE, "cite": cite})
            continue
        letter, first, last = match[1], int(match[2]), int(match[4])
        span = [f"{letter}{number}" for number in range(first, last + 1)]
        missing = [identifier for identifier in (span[0], span[-1]) if identifier not in candidates]
        if missing:
            problems.extend({"code": UNKNOWN_ID, "id": identifier} for identifier in missing)
            continue
        # `feed_candidates` refuses a gap, so both ends known means every id between is.
        ids.extend(span)
    return list(dict.fromkeys(ids)), problems


def _union_box(boxes: Sequence[Box]) -> Box | None:
    if not boxes:
        return None
    corners = [_corners(box) for box in boxes]
    return _from_corners(
        min(c[0] for c in corners),
        min(c[1] for c in corners),
        max(c[2] for c in corners),
        max(c[3] for c in corners),
    )


def region_boxes(cited_ids: Sequence[str], candidates: Mapping[str, Box | None]) -> list[Box]:
    """The boxes of the placing ids among `cited_ids`, each box once, in first-cited order."""
    boxes: dict[tuple[int, ...], Box] = {}
    for identifier in cited_ids:
        box = candidates[identifier]
        if box is not None:
            boxes.setdefault(_corners(box), box)
    return list(boxes.values())


def region_area(boxes: Sequence[Box]) -> int:
    """The area of the union of `boxes`, each pixel once."""
    bounding = _union_box(boxes)
    return 0 if bounding is None else _area_inside(bounding, boxes)


def _shared_area(region: Sequence[Box], other: Sequence[Box]) -> int:
    """The area both regions claim: the union of their boxes' pairwise overlaps."""
    overlaps = []
    for a in (_corners(box) for box in region):
        for b in (_corners(box) for box in other):
            x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
            if x0 < x1 and y0 < y1:
                overlaps.append(_from_corners(x0, y0, x1, y1))
    return region_area(overlaps)


def duplicate_regions(
    entries: Sequence[Mapping[str, Any]], policy: PageAccountingPolicy
) -> list[dict[str, Any]]:
    """`duplicate-region` for every two placed entries that claim mostly the same ink.

    Regions are compared as ink, whatever boxes name it: two entries are held
    when the area both claim exceeds `max_shared_share_bp` of the smaller
    region. That holds one region inside another, and the same ink named by
    other ids (a range of units against the lines under them). An act sharing
    one line with its neighbour at its edge stays under the share; rule (h)
    records that line as `shared-line`.
    """
    placed = [
        (entry["n"], entry["region_boxes_px"], region_area(entry["region_boxes_px"]))
        for entry in entries
        if entry["region_boxes_px"]
    ]
    findings = []
    for index, (n, region, area) in enumerate(placed):
        for other_n, other, other_area in placed[index + 1 :]:
            shared = _shared_area(region, other)
            smaller = min(area, other_area)
            if shared * BASIS_POINTS > policy.max_shared_share_bp * smaller:
                findings.append(
                    {
                        "code": DUPLICATE_REGION,
                        "ns": sorted((n, other_n)),
                        "shared_px": shared,
                        "smaller_region_px": smaller,
                    }
                )
    return sorted(findings, key=lambda finding: finding["ns"])


def _range_ids(cite: str) -> list[str]:
    match = _RANGE.fullmatch(cite)
    letter, first, last = match[1], int(match[2]), int(match[4])
    return [f"{letter}{number}" for number in range(first, last + 1)]


def _covered_detection_ranges(
    cited_ids: list[str],
    problems: list[dict[str, Any]],
    candidates: Mapping[str, Box | None],
    policy: PageAccountingPolicy,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Read the detection ranges of one entry that can name no ink it did not read.

    `expand_cites` refuses every range over Surya lines or blocks, because their
    numbering follows the detector rather than the page's columns. Two kinds
    cannot name unread ink, and are read here: a range of blocks, since a block
    places nothing (`placement_boxes`), and a range of lines each of which lies
    inside (`is_inside`) the boxes of the witness units the same entry cites,
    since those units already claim that ink for the entry. Any other detection
    range stays `detection-range`, contributing no id. Returns the entry's ids
    and its remaining problems.
    """
    own_units = [
        candidates[identifier]
        for identifier in cited_ids
        if identifier[0] not in DETECTION_LETTERS and candidates[identifier] is not None
    ]
    ids = list(cited_ids)
    kept: list[dict[str, Any]] = []
    for problem in problems:
        cite = problem.get("cite")
        if problem["code"] != DETECTION_RANGE or not isinstance(cite, str):
            kept.append(problem)
            continue
        span = _range_ids(cite)
        if any(identifier not in candidates for identifier in (span[0], span[-1])):
            kept.append(problem)
            continue
        boxes = [candidates[identifier] for identifier in span]
        blocks = all(box is None for box in boxes)
        lines_read = all(
            box is not None and own_units and is_inside(box, own_units, policy) for box in boxes
        )
        if blocks or lines_read:
            ids.extend(span)
        else:
            kept.append(problem)
    return list(dict.fromkeys(ids)), kept


def _shared_units(
    cited: Sequence[Sequence[str]], candidates: Mapping[str, Box | None]
) -> frozenset[str]:
    """The placing witness units more than one entry of an answer cites."""
    counts = Counter(
        identifier
        for ids in cited
        for identifier in set(ids)
        if identifier[0] not in DETECTION_LETTERS and candidates.get(identifier) is not None
    )
    return frozenset(identifier for identifier, count in counts.items() if count > 1)


def _placing_ids(
    cited_ids: Sequence[str],
    shared: frozenset[str],
    candidates: Mapping[str, Box | None],
    policy: PageAccountingPolicy | None,
) -> list[str]:
    """The cited ids that place an entry: a shared unit it reads a part of places it not.

    A witness unit several entries cite (a whole index table, or two acts the
    witness ran together) cannot say which part of its box is whose. It lends
    no area to an entry whose own placing ids (its lines and the units no other
    entry cites) all lie inside it (`is_inside`): that entry claims a part of
    the unit, and is placed by that part. A shared unit an entry cites beside
    ink of its own elsewhere on the page, or an entry with no placing id of its
    own, keeps the unit's box: that is two entries claiming the same ink, and
    rule (h) holds them as duplicates. Without a policy every cited id places.
    """
    if policy is None:
        return list(cited_ids)
    own = [
        candidates[identifier]
        for identifier in cited_ids
        if identifier not in shared and candidates.get(identifier) is not None
    ]
    if not own:
        return list(cited_ids)
    return [
        identifier
        for identifier in cited_ids
        if identifier not in shared
        or not all(is_inside(box, [candidates[identifier]], policy) for box in own)
    ]


def validate_answer(
    answer: Any,
    candidates: Mapping[str, Box | None],
    *,
    policy: PageAccountingPolicy | None = None,
) -> dict[str, Any]:
    """Read a parsed page answer against its feed's candidates, repairing nothing.

    With `policy` (every reading the pipeline measures), a detection range that
    can name no unread ink is read (`_covered_detection_ranges`); without it,
    every detection range is `detection-range`.

    `candidates` is `feed_candidates(feed, policy)`. The answer's grammar is
    `common.page_answer.grammar_problems`'s; an answer outside it has no entries
    and one `answer-grammar` problem per departure. Returns `entries` (one per
    entry, in the order given: `n`, `kind`, `label`, `cites` as given,
    `cited_ids` expanded, `region_boxes_px` -- the boxes of the cited placing
    ids (`region_boxes`), the region every rule measures -- `union_box_px`,
    their bounding box, unpadded, which only crops and names the act, or
    `None` when the entry cites no placing id, and `text`), `set_aside`
    (`{id: reason}` for every id set aside with a reason) and `problems`
    (every finding; any one holds the page). An entry with no placing
    citation is not a problem here: it is published unplaced and held by the
    accounting's rule (b); two entries on one region are `duplicate_regions`.
    """
    grammar = grammar_problems(answer)
    if grammar:
        return {
            "entries": [],
            "set_aside": {},
            "problems": [
                {"code": ANSWER_GRAMMAR, "grammar": problem["code"], "detail": problem["detail"]}
                for problem in grammar
            ],
        }
    problems: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    expanded = []
    for raw in answer["acts"]:
        cited_ids, cite_problems = expand_cites(raw["cites"], candidates)
        if policy is not None:
            cited_ids, cite_problems = _covered_detection_ranges(
                cited_ids, cite_problems, candidates, policy
            )
        problems.extend({**problem, "n": raw["n"]} for problem in cite_problems)
        expanded.append((raw, cited_ids))
    shared = _shared_units([cited_ids for _raw, cited_ids in expanded], candidates)
    for raw, cited_ids in expanded:
        boxes = region_boxes(_placing_ids(cited_ids, shared, candidates, policy), candidates)
        entries.append(
            {
                "n": raw["n"],
                "kind": raw["kind"],
                "label": raw.get("label"),
                "cites": list(raw["cites"]),
                "cited_ids": cited_ids,
                "region_boxes_px": boxes,
                "union_box_px": _union_box(boxes),
                "text": raw["text"],
                "continues_from_previous_page": raw["continues_from_previous_page"],
                "continues_to_next_page": raw["continues_to_next_page"],
            }
        )

    cited = {identifier for entry in entries for identifier in entry["cited_ids"]}
    set_aside: dict[str, str] = {}
    seen: set[str] = set()
    for index, raw in enumerate(answer["set_aside"]):
        ids, id_problems = expand_cites([raw["id"]], candidates)
        problems.extend({**problem, "set_aside_index": index} for problem in id_problems)
        reason = raw["reason"]
        has_reason = bool(reason.strip())
        for identifier in ids:
            if identifier in seen:
                problems.append({"code": SET_ASIDE_TWICE, "id": identifier})
            seen.add(identifier)
            if identifier in cited:
                problems.append({"code": CITED_AND_SET_ASIDE, "id": identifier})
            if not has_reason:
                problems.append({"code": SET_ASIDE_WITHOUT_REASON, "id": identifier})
            elif identifier not in set_aside:
                set_aside[identifier] = reason
    return {"entries": entries, "set_aside": set_aside, "problems": problems}


def named_candidates(
    candidates: Mapping[str, Box | None], named: Sequence[str]
) -> dict[str, Box | None]:
    """The candidates a re-ask may cite: the ids it was asked about, each with a placing box."""
    if not named or len(set(named)) != len(named):
        raise ContractError("a re-ask names no id, or one id twice")
    for identifier in named:
        if candidates.get(identifier) is None:
            raise ContractError(f"a re-ask names {identifier!r}, which the feed does not place")
    return {identifier: candidates[identifier] for identifier in named}


def validate_reask_answer(
    answer: Any, candidates: Mapping[str, Box | None], named: Sequence[str]
) -> dict[str, Any]:
    """`validate_answer` for a re-ask: only the named ids are known, and nothing continues.

    Citing or setting aside any other id is `unknown-id`, and an entry with a
    continuation flag set is `reask-continuation`; either holds the re-ask whole.
    """
    validated = validate_answer(answer, named_candidates(candidates, named))
    problems = list(validated["problems"])
    for entry in validated["entries"]:
        for flag in ("continues_from_previous_page", "continues_to_next_page"):
            if entry[flag] is True:
                problems.append({"code": REASK_CONTINUATION, "n": entry["n"], "flag": flag})
    return {**validated, "problems": problems}


def _combined(
    first: dict[str, Any], reask: Mapping[str, Any], candidates: Mapping[str, Box | None]
) -> dict[str, Any]:
    """The first reading's valid entries, then the re-ask's, and whether the re-ask stands.

    The first reading's entries are kept exactly; a re-ask that is a parsed,
    valid answer finished on `stop` adds its entries numbered on from the
    first's (`n = k + j`, `reading_n = j`) and its set-asides. An id both read
    and set aside across the two, or one set aside twice, holds the re-ask
    whole, as it would within one answer.
    """
    reading, named = reask["reading"], reask["named"]
    parsed = reading["parse_state"] == PARSED and reading.get("answer") is not None
    validated = (
        validate_reask_answer(reading["answer"], candidates, named)
        if parsed
        else {"entries": [], "set_aside": {}, "problems": []}
    )
    problems = list(validated["problems"])
    cited_first = {i for entry in first["entries"] for i in entry["cited_ids"]}
    cited_second = {i for entry in validated["entries"] for i in entry["cited_ids"]}
    problems += [
        {"code": CITED_AND_SET_ASIDE, "id": identifier}
        for identifier in sorted(
            (cited_first & set(validated["set_aside"])) | (cited_second & set(first["set_aside"])),
            key=id_key,
        )
    ]
    problems += [
        {"code": SET_ASIDE_TWICE, "id": identifier}
        for identifier in sorted(set(first["set_aside"]) & set(validated["set_aside"]), key=id_key)
    ]
    stands = parsed and finished_on_stop(reading) and not problems
    k = len(first["entries"])
    added = (
        [
            {
                **entry,
                "n": k + entry["n"],
                "reading_attempt": REASK_READING,
                "reading_n": entry["n"],
            }
            for entry in sorted(validated["entries"], key=lambda entry: entry["n"])
        ]
        if stands
        else []
    )
    return {
        "stands": stands,
        "problems": problems,
        "entries": added,
        "set_aside": dict(validated["set_aside"]) if stands else {},
    }


# --- the sealed detections -----------------------------------------------------------


def _ref_key(item: Mapping[str, Any]) -> tuple[tuple[int, ...], str]:
    box = item["box_px"]
    return (
        () if box is None else _corners(box),
        json.dumps(item["ref"], sort_keys=True, default=str),
    )


def _census(
    items: Any,
    where: str,
    shown: Mapping[str, Box | None],
    shown_ids: Sequence[str],
    *,
    unboxed: bool = False,
) -> list[dict[str, Any]]:
    """Sealed detections as `{id | None, box_px, ref}`, checked against what the feed showed.

    An item the feed showed carries its feed id and the same box; every id the
    feed showed of this kind is in the census. The feed switches what the model
    saw, never what the check measures. With `unboxed` an item may have no box
    (a detector record whose corners enclose no crop): it is kept, to be
    reported as not measured.
    """
    if not isinstance(items, list):
        raise ContractError(f"sealed {where} are not a list")
    census: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if (
            not isinstance(item, dict)
            or not {"box_px", "ref"} <= set(item)
            or (set(item) - {"id", "box_px", "ref"})
        ):
            raise ContractError(f"a sealed {where[:-1]} is not {{id?, box_px, ref}}")
        box = (
            None
            if unboxed and item["box_px"] is None
            else _box(item["box_px"], f"sealed {where[:-1]}")
        )
        identifier = item["id"] if "id" in item else None
        if identifier is not None:
            if identifier not in shown or shown[identifier] != box or identifier in seen:
                raise ContractError(
                    f"sealed {where[:-1]} {identifier!r} is not the feed's {identifier} "
                    "with the same box, once"
                )
            seen.add(identifier)
        census.append({"id": identifier, "box_px": box, "ref": item["ref"]})
    missing = sorted(set(shown_ids) - seen, key=id_key)
    if missing:
        raise ContractError(f"the feed shows {missing} that the sealed {where} do not hold")
    return sorted(census, key=_ref_key)


def _read_detections(
    detections: Mapping[str, Any], feed: Mapping[str, Any]
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None, str, bool]:
    if not isinstance(detections, Mapping) or set(detections) != _DETECTIONS_KEYS:
        raise ContractError("detections are not {surya, records, record_detector, record_census}")
    detector = detections["record_detector"]
    if detector not in (RECORD_DETECTOR_CONFIGURED, RECORD_DETECTOR_ABSENT):
        raise ContractError("detections record_detector is not configured or absent")
    feed_surya = feed["surya"]
    shown_lines = [] if feed_surya is None else [line["id"] for line in feed_surya["lines"]]
    shown_blocks = [] if feed_surya is None else [block["id"] for block in feed_surya["blocks"]]
    # Each detection the feed shows, with the sealed box the feed recorded for it
    # (a block places nothing, but its box is still the detector's).
    feed_boxes = (
        {}
        if feed_surya is None
        else {
            item["id"]: _box(item["box_px"], item["id"])
            for item in feed_surya["lines"] + feed_surya["blocks"]
        }
    )
    surya = detections["surya"]
    lines = None
    if surya is None:
        if shown_lines or shown_blocks:
            raise ContractError("the feed shows Surya detections the page has no census for")
    else:
        if not isinstance(surya, Mapping) or set(surya) != _SURYA_KEYS:
            raise ContractError("detections surya is not {lines, blocks}")
        lines = _census(surya["lines"], "Surya lines", feed_boxes, shown_lines)
        _census(surya["blocks"], "Surya blocks", feed_boxes, shown_blocks)
    records = detections["records"]
    census = detections["record_census"]
    if (records is None) != (census is None):
        raise ContractError(
            "detections carry detector records and their census together or neither"
        )
    capped = False
    if records is not None:
        if detector == RECORD_DETECTOR_ABSENT:
            raise ContractError("detections carry records from a detector stated absent")
        if (
            not isinstance(census, Mapping)
            or set(census) != _RECORD_CENSUS_KEYS
            or any(
                not isinstance(census[key], int) or isinstance(census[key], bool) or census[key] < 0
                for key in ("detection_count", "max_det")
            )
            or not isinstance(census["max_det_reached"], bool)
        ):
            raise ContractError(
                "detections record_census is not {detection_count, max_det, max_det_reached}"
            )
        capped = census["max_det_reached"]
        # A record the feed shows as a witness unit has that unit's sealed box,
        # whatever the unit places by under the feed's switches.
        witness_units = {
            unit["id"]: unit["box_px"] for witness in feed["witnesses"] for unit in witness["units"]
        }
        records = _census(records, "detector records", witness_units, [], unboxed=True)
    return lines, records, detector, capped


def _read_witnesses(
    witnesses: Sequence[Mapping[str, Any]], feed: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Every sealed witness in letter order, checked against the feed, and the letters shown.

    The feed's `witnesses` switch chooses what the model saw, never what the
    check measures: a witness the feed shows must be here with its outcome and
    units unchanged, and one it hides is still measured.
    """
    if not isinstance(witnesses, Sequence) or isinstance(witnesses, str):
        raise ContractError("sealed witnesses are not a list")
    sealed: dict[str, dict[str, Any]] = {}
    for witness in witnesses:
        if not isinstance(witness, Mapping) or set(witness) != _WITNESS_KEYS:
            raise ContractError("a sealed witness is not {letter, outcome, blank, units}")
        letter = witness["letter"]
        if not isinstance(letter, str) or not re.fullmatch("[A-Z]", letter) or letter in sealed:
            raise ContractError(f"sealed witness letter {letter!r} is not one new capital")
        if witness["blank"] not in (True, False, None) or not isinstance(witness["units"], list):
            raise ContractError(f"sealed witness {letter} blank or units are malformed")
        units = []
        for unit in witness["units"]:
            if not isinstance(unit, Mapping) or not isinstance(unit.get("text"), str):
                raise ContractError(f"a sealed witness {letter} unit is not {{id, box_px, text}}")
            box = unit.get("box_px")
            units.append(
                {
                    "id": unit.get("id"),
                    "box_px": None if box is None else _box(box, f"sealed {unit.get('id')}"),
                    "text": unit["text"],
                }
            )
        expected = [f"{letter}{number}" for number in range(1, len(units) + 1)]
        if sorted((unit["id"] for unit in units), key=str) != sorted(expected):
            raise ContractError(f"sealed witness {letter} unit ids are not {letter}1..n once each")
        units.sort(key=lambda unit: id_key(unit["id"]))
        if witness["blank"] is True and units:
            raise ContractError(f"sealed witness {letter} reports a blank page and gives units")
        sealed[letter] = {**witness, "units": units}
    shown: set[str] = set()
    for witness in feed["witnesses"]:
        letter = witness["letter"]
        match = sealed.get(letter)
        shown_units = sorted(
            ((u["id"], u.get("box_px"), u["text"]) for u in witness["units"]),
            key=lambda unit: id_key(unit[0]),
        )
        if (
            match is None
            or witness.get("outcome") != match["outcome"]
            or shown_units != [(u["id"], u["box_px"], u["text"]) for u in match["units"]]
        ):
            raise ContractError(
                f"the feed shows witness {letter} other than the sealed witness {letter}"
            )
        shown.add(letter)
    return [sealed[letter] for letter in sorted(sealed)], shown


# --- geometry --------------------------------------------------------------------------


def _area(box: Box) -> int:
    return box["w"] * box["h"]


def _area_inside(box: Box, regions: Sequence[Box]) -> int:
    """The area of `box` covered by the union of `regions`."""
    b = _corners(box)
    clipped = [
        (max(b[0], r[0]), max(b[1], r[1]), min(b[2], r[2]), min(b[3], r[3]))
        for r in (_corners(region) for region in regions)
    ]
    clipped = [c for c in clipped if c[0] < c[2] and c[1] < c[3]]
    if not clipped:
        return 0
    xs = sorted({c[0] for c in clipped} | {c[2] for c in clipped})
    covered = 0
    for x0, x1 in zip(xs, xs[1:], strict=False):
        spans = sorted((c[1], c[3]) for c in clipped if c[0] <= x0 and c[2] >= x1)
        length = 0
        top = bottom = None
        for y0, y1 in spans:
            if bottom is None or y0 > bottom:
                if bottom is not None:
                    length += bottom - top
                top, bottom = y0, y1
            else:
                bottom = max(bottom, y1)
        if bottom is not None:
            length += bottom - top
        covered += length * (x1 - x0)
    return covered


def is_inside(box: Box, regions: Sequence[Box], policy: PageAccountingPolicy) -> bool:
    """Whether the sealed share of `box`'s area lies inside the union of `regions`."""
    return _area_inside(box, regions) * BASIS_POINTS >= policy.inside_min_area_bp * _area(box)


# --- witness text coverage (rule e) ------------------------------------------------------


def normalized_text(raw: str, *, unreadable: bool = False) -> str:
    """Letters and digits only, casefolded and without accents, for coverage.

    A doubt mark `[[reading|alt]]` keeps its first reading. `[[?]]` is ink the
    reader flagged unreadable: with `unreadable` (a reading) it becomes one
    `UNREADABLE` mark, which the coverage rule lets account for a bounded run of
    witness text opposite it; without (a witness) it keeps nothing. The DAI
    uncertainty markers, well-formed markup tags and entity spelling are
    removed; whitespace and punctuation go, because witnesses and the reader
    break lines, hyphenate and punctuate differently and none of that is ink
    missed. What remains is the evidence a witness read something.
    """
    marker = UNREADABLE if unreadable else " "
    text = raw.replace(UNREADABLE, " ")
    text = _DOUBT_MARK.sub(
        lambda mark: marker if mark[1] == "?" else " " + mark[1].split("|")[0] + " ", text
    )
    for token in UNCERTAINTY_TOKENS:
        text = text.replace(token, " ")
    text = _TAG.sub(" ", text)
    text = _ENTITY.sub(lambda entity: html.unescape(entity[0]), text)
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text).casefold()
        if character.isalnum() or character == UNREADABLE
    )


class _NotMeasured(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _WorkBudget:
    """The alignment work one page's rule (e) may do, counted in steps.

    A step is one range taken from a work list or one character pair the
    alignment compares, or may compare. Work is charged before it is done, so
    running out stops the rule before the work, and whether a page is measured
    depends only on its inputs and the sealed policy, never on the machine.
    """

    def __init__(self, steps: int) -> None:
        self.left = steps

    def spend(self, steps: int) -> None:
        self.left -= steps
        if self.left < 0:
            raise _NotMeasured("work-bound")


def _longest_in_band(
    matcher: SequenceMatcher,
    a: str,
    b: str,
    box: tuple[int, int, int, int],
    band: tuple[int, int],
    budget: _WorkBudget,
) -> tuple[int, int, int]:
    """The longest common run of `a[i0:i1]` and `b[j0:j1]` whose diagonal `i - j` is in `band`.

    The unbanded search is charged every pair of the box, the most it compares;
    the banded scan each pair on the diagonals it walks.
    """
    i0, i1, j0, j1 = box
    low, high = band
    budget.spend((i1 - i0) * (j1 - j0))
    i, j, size = matcher.find_longest_match(i0, i1, j0, j1)
    if size == 0 or low <= i - j <= high:
        return i, j, size
    best = (i0, j0, 0)
    for diagonal in range(max(low, i0 - j1 + 1), min(high, i1 - 1 - j0) + 1):
        start, stop = max(i0, j0 + diagonal), min(i1, j1 + diagonal)
        budget.spend(max(0, stop - start))
        run = 0
        for x in range(start, stop):
            if a[x] == b[x - diagonal]:
                run += 1
                if run > best[2]:
                    best = (x - run + 1, x - run + 1 - diagonal, run)
            else:
                run = 0
    return best


def _direct_blocks(
    a: str,
    b: str,
    alo: int,
    ahi: int,
    blo: int,
    bhi: int,
    policy: PageAccountingPolicy,
    budget: _WorkBudget,
) -> list[tuple[int, int, int]]:
    """Ratcliff-Obershelp matching blocks of at least `min_block_characters`, banded.

    Longest common run first, then recursively either side of it, so blocks are
    ordered on both texts and each reading character matches at most one witness
    character. A range whose longest run is shorter than the minimum holds no
    longer one, so it is not searched further.

    Each range only accepts runs whose offset `i - j` lies between the offsets
    at its two corners, widened by `band_slack` either side: a misreading moves
    the offset by what it inserts or drops, which the corners already bound. A
    name the entry repeats (the father who is also the godfather) otherwise
    pairs its second writing with the first and opens a false gap either side.
    """
    sliced_a, sliced_b = a[alo:ahi], b[blo:bhi]
    matcher = SequenceMatcher(None, sliced_a, sliced_b, autojunk=False)
    slack = policy.band_slack
    blocks: list[tuple[int, int, int]] = []
    work = [(0, ahi - alo, 0, bhi - blo)]
    while work:
        budget.spend(1)
        i0, i1, j0, j1 = work.pop()
        if i0 >= i1 or j0 >= j1:
            continue
        corners = (i0 - j0, i1 - j1)
        band = (min(corners) - slack, max(corners) + slack)
        i, j, size = _longest_in_band(matcher, sliced_a, sliced_b, (i0, i1, j0, j1), band, budget)
        if size < policy.min_block_characters:
            continue
        blocks.append((alo + i, blo + j, size))
        work.append((i0, i, j0, j))
        work.append((i + size, i1, j + size, j1))
    return blocks


def _anchor_blocks(
    a: str, b: str, alo: int, ahi: int, blo: int, bhi: int, k: int
) -> list[tuple[int, int, int]]:
    """Blocks from the k-grams that occur exactly once in each range, in order.

    Long texts repeat the register's formula in every entry, so a longest-match
    search can pair one entry's formula with another's. A k-gram unique on both
    sides (a name, a date) pins the pairing; the longest chain of them that is
    ordered on both texts is kept, as in patience diff.
    """

    def unique(text: str, lo: int, hi: int) -> dict[str, int]:
        counts = Counter(text[i : i + k] for i in range(lo, hi - k + 1))
        return {text[i : i + k]: i for i in range(lo, hi - k + 1) if counts[text[i : i + k]] == 1}

    in_a, in_b = unique(a, alo, ahi), unique(b, blo, bhi)
    pairs = sorted((i, in_b[gram]) for gram, i in in_a.items() if gram in in_b)
    # Longest chain increasing in j (i already increases): patience sorting.
    tails: list[int] = []
    tail_index: list[int] = []
    previous: list[int] = [-1] * len(pairs)
    for index, (_i, j) in enumerate(pairs):
        position = bisect_left(tails, j)
        if position == len(tails):
            tails.append(j)
            tail_index.append(index)
        else:
            tails[position] = j
            tail_index[position] = index
        previous[index] = tail_index[position - 1] if position else -1
    chain: list[tuple[int, int]] = []
    index = tail_index[-1] if tail_index else -1
    while index != -1:
        chain.append(pairs[index])
        index = previous[index]
    chain.reverse()
    blocks: list[tuple[int, int, int]] = []
    for i, j in chain:
        if blocks:
            bi, bj, size = blocks[-1]
            if i - j == bi - bj and i <= bi + size:
                blocks[-1] = (bi, bj, max(size, i + k - bi))
                continue
            if i < bi + size or j < bj + size:
                continue
        blocks.append((i, j, k))
    return blocks


def _consistent_anchors(
    blocks: list[tuple[int, int, int]], policy: PageAccountingPolicy
) -> list[tuple[int, int, int]]:
    """The anchors whose offset agrees with their neighbours' within the sealed tolerance.

    An anchor's diagonal `i - j` is the offset between the two texts at that
    point. Misreadings move it a little; a missed passage moves it once and for
    good. A short run anchored out of place -- a noisy name that happens to
    equal the same name a record later -- moves it and moves it straight back.
    An anchor is kept when its diagonal is within the tolerance of the median of
    the sealed number of anchors either side of it (itself included). A dropped
    anchor only splits the alignment less: the range around it is still matched
    directly, so dropping one never credits a reading with anything.
    """
    diagonals = [i - j for i, j, _size in blocks]
    reach = policy.anchor_neighbours
    kept = []
    for index, block in enumerate(blocks):
        window = sorted(diagonals[max(0, index - reach) : index + reach + 1])
        if abs(diagonals[index] - window[len(window) // 2]) <= policy.anchor_offset_tolerance:
            kept.append(block)
    return kept


def _matching_blocks(
    a: str, b: str, policy: PageAccountingPolicy, budget: _WorkBudget
) -> list[tuple[int, int, int]]:
    """Ordered, non-overlapping matching blocks of `a` against `b`, or `_NotMeasured`.

    Small ranges are matched directly; larger ones are first split at unique
    k-gram anchors. A large range with no anchor is matched directly only
    under the sealed pair bound; beyond it the alignment is not measured.
    """
    if len(a) > policy.max_characters or len(b) > policy.max_characters:
        raise _NotMeasured("size-bound")
    blocks: list[tuple[int, int, int]] = []
    work = [(0, len(a), 0, len(b))]
    while work:
        budget.spend(1)
        alo, ahi, blo, bhi = work.pop()
        if alo >= ahi or blo >= bhi:
            continue
        pairs = (ahi - alo) * (bhi - blo)
        if pairs <= policy.direct_alignment_max_pairs:
            blocks.extend(_direct_blocks(a, b, alo, ahi, blo, bhi, policy, budget))
            continue
        # Anchoring reads each character of both ranges once per k-gram.
        budget.spend((ahi - alo + bhi - blo) * policy.anchor_characters)
        anchors = _consistent_anchors(
            _anchor_blocks(a, b, alo, ahi, blo, bhi, policy.anchor_characters), policy
        )
        if not anchors:
            if pairs > policy.max_alignment_pairs:
                raise _NotMeasured("size-bound")
            blocks.extend(_direct_blocks(a, b, alo, ahi, blo, bhi, policy, budget))
            continue
        blocks.extend(anchors)
        cursor_a, cursor_b = alo, blo
        for i, j, size in anchors:
            work.append((cursor_a, i, cursor_b, j))
            cursor_a, cursor_b = i + size, j + size
        work.append((cursor_a, ahi, cursor_b, bhi))
    return sorted(blocks)


@dataclass(frozen=True)
class TextCoverage:
    """How much of one normalized witness text a normalized reading accounts for.

    `unread_run` is the longest run left unaccounted for, `unread_characters`
    all of them; `matched_characters` is the witness text inside matched
    blocks; `position` maps each witness offset (and the end) to the reading
    offset the alignment puts opposite it.
    """

    witness_characters: int
    matched_characters: int
    unread_run: int
    unread_characters: int
    position: tuple[int, ...]

    @property
    def unread_share_bp(self) -> int:
        return self.unread_characters * BASIS_POINTS // max(self.witness_characters, 1)


def _text_coverage(
    witness: str, reading: str, policy: PageAccountingPolicy, budget: _WorkBudget
) -> TextCoverage:
    """Align a normalized witness text with a normalized reading and count what is unread.

    Between two consecutive matched blocks the witness has `gap_w` unmatched
    characters and the reading `gap_r` characters and `k` unreadable marks.
    Reading characters opposite a witness gap account for it one for one -- a
    misread letter or word substitutes, it does not vanish -- but at most
    `max_unread_characters`, since a whole entry replaced by different text is
    not a misreading. Each unreadable mark accounts for up to
    `max_unread_characters` instead: the reader said the ink is there and could
    not be read. The gap leaves `gap_w - credit` unread. Every matched block
    counts, none is dropped after matching, so no gap is widened into credit.
    """
    threshold = policy.max_unread_characters
    blocks = _matching_blocks(witness, reading, policy, budget)
    longest = total = matched = 0
    position = [0] * (len(witness) + 1)
    cursor_a = cursor_b = 0
    for i, j, size in [*blocks, (len(witness), len(reading), 0)]:
        gap_w, gap_r = i - cursor_a, j - cursor_b
        opposite = reading[cursor_b:j]
        marks = opposite.count(UNREADABLE)
        credit = marks * threshold if marks else min(len(opposite), threshold)
        unread = max(0, gap_w - credit)
        longest = max(longest, unread)
        total += unread
        matched += size
        for offset in range(gap_w):
            position[cursor_a + offset] = cursor_b + offset * gap_r // gap_w
        for offset in range(size):
            position[i + offset] = j + offset
        cursor_a, cursor_b = i + size, j + size
    position[len(witness)] = len(reading)
    return TextCoverage(len(witness), matched, longest, total, tuple(position))


def best_substring_distance(pattern: str, text: str) -> int:
    """The fewest edits turning `pattern` into some substring of `text` (Myers, bit-parallel)."""
    m = len(pattern)
    if m == 0:
        return 0
    full = (1 << m) - 1
    high = 1 << (m - 1)
    equal: dict[str, int] = {}
    for index, character in enumerate(pattern):
        equal[character] = equal.get(character, 0) | (1 << index)
    plus, minus, score = full, 0, m
    best = m
    for character in text:
        eq = equal.get(character, 0)
        xv = eq | minus
        xh = ((((eq & plus) + plus) & full) ^ plus) | eq
        horizontal_plus = minus | (~(xh | plus) & full)
        horizontal_minus = plus & xh
        if horizontal_plus & high:
            score += 1
        elif horizontal_minus & high:
            score -= 1
        horizontal_plus = (horizontal_plus << 1) & full
        horizontal_minus = (horizontal_minus << 1) & full
        plus = horizontal_minus | (~(xv | horizontal_plus) & full)
        minus = horizontal_plus & xv
        best = min(best, score)
    return best


def _pieces(text: str, k: int) -> set[str]:
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _one_deletions(piece: str) -> set[str]:
    return {piece[:i] + piece[i + 1 :] for i in range(len(piece))}


class _NearPieces:
    """The pieces of one text, indexed to find a piece within one edit of a substring."""

    def __init__(self, text: str, k: int) -> None:
        self.exact = _pieces(text, k)
        self.substituted = set().union(*(_one_deletions(piece) for piece in self.exact))
        self.inserted = set().union(*(_one_deletions(piece) for piece in _pieces(text, k + 1)))

    def near(self, piece: str) -> bool:
        return (
            piece in self.exact
            or piece in self.inserted
            or any(shorter in self.substituted for shorter in _one_deletions(piece))
        )


def _distinctive_pieces(
    units: list[dict[str, Any]], readings: Mapping[int, str], k: int
) -> tuple[dict[str, str], dict[str, dict[str, int]]]:
    """Each unit's normalized text and its distinctive pieces with their first offset.

    A unit's distinctive pieces are the pieces that are not the register's
    formula -- no other unit of the same witness holds them within one edit,
    and no two readings on the page do -- and that the page corroborates:
    another witness holds them exactly, or some reading within one edit. A
    witness's own misrecognitions make pieces that are in no ink; counting them
    would hold every correct reading of a noisy witness.
    """
    texts = {unit["id"]: normalized_text(unit["text"]) for unit in units}
    pieces = {identifier: _pieces(text, k) for identifier, text in texts.items()}
    near_units = {identifier: _NearPieces(text, k) for identifier, text in texts.items()}
    near_readings = [_NearPieces(text, k) for text in readings.values()]
    distinctive: dict[str, dict[str, int]] = {}
    for identifier, text in texts.items():
        letter = identifier[0]
        same = [near_units[other] for other in texts if other != identifier and other[0] == letter]
        others = set().union(*(pieces[other] for other in texts if other[0] != letter))
        kept: dict[str, int] = {}
        for offset in range(len(text) - k + 1):
            piece = text[offset : offset + k]
            if piece in kept or any(index.near(piece) for index in same):
                continue
            in_readings = sum(1 for index in near_readings if index.near(piece))
            if in_readings < 2 and (piece in others or in_readings == 1):
                kept[piece] = offset
        distinctive[identifier] = kept
    return texts, distinctive


def _distinctive_share(
    witness: str,
    reading: str,
    distinctive: Mapping[str, int],
    coverage: TextCoverage,
    policy: PageAccountingPolicy,
) -> int:
    """The share (basis points) of distinctive pieces the reading holds where it aligns them.

    A piece is held when the reading opposite it, widened by `window_slack`
    either side, holds it within `piece_edits` edits, or carries a `[[?]]`
    there. Looking only where the alignment puts it keeps a neighbouring
    record's reading -- same formula, other names -- from lending its names.
    """
    k, slack = policy.piece_characters, policy.window_slack
    found = 0
    for piece, offset in distinctive.items():
        start = max(0, coverage.position[offset] - slack)
        window = reading[start : coverage.position[offset + k] + slack]
        if UNREADABLE in window or best_substring_distance(piece, window) <= policy.piece_edits:
            found += 1
    return found * BASIS_POINTS // len(distinctive)


def _short_unit_distance_bp(
    witness: str, reading: str, coverage: TextCoverage, policy: PageAccountingPolicy
) -> int:
    """Edits turning a unit's text into the best stretch of its readings, per unit letter.

    A `[[?]]` opposite the unit, where the alignment puts it and `window_slack`
    letters either side, accounts for it (distance 0), as it does for a
    distinctive piece: the reader flagged that ink unreadable.
    """
    slack = policy.window_slack
    start = max(0, coverage.position[0] - slack)
    if UNREADABLE in reading[start : coverage.position[len(witness)] + slack]:
        return 0
    return best_substring_distance(witness, reading) * BASIS_POINTS // len(witness)


def _rule(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """`hold` on any held finding, else `not-measured` on any unmeasured one, else `pass`."""
    found = {finding["code"] for finding in findings}
    if found & (HOLD_CODES - NOT_MEASURED_CODES):
        status = HOLD
    elif found & NOT_MEASURED_CODES:
        status = NOT_MEASURED
    else:
        status = PASS
    return {"status": status, "findings": findings}


def id_key(identifier: str) -> tuple[str, int]:
    """A feed id's sort key: its letter, then its number."""
    return identifier[0], int(identifier[1:])


def _located(finding: dict[str, Any], box: Box | None) -> dict[str, Any]:
    return finding if box is None else {**finding, "box_px": box}


# --- the accounting --------------------------------------------------------------------


def page_accounting(
    *,
    feed: Mapping[str, Any],
    witnesses: Sequence[Mapping[str, Any]],
    detections: Mapping[str, Any],
    reading: Mapping[str, Any],
    entry_truncation: Mapping[int, str],
    ink: Mapping[str, Any] | None,
    policy: PageAccountingPolicy,
    feed_ref: Any,
    page_reading_ref: Any,
    reask: Mapping[str, Any] | None = None,
    attempt: int = FIRST_READING,
) -> dict[str, Any]:
    """The `page-accounting.v2` payload for one page reading, or for a reading and its re-ask.

    `attempt` is the ordinal of the whole-page reading accounted: 1 for a first
    reading (`answer_basis` "attempt-1"), or an operator re-read's, 3 or more
    (`answer_basis` "attempt-<n>", each entry's `reading_attempt` that ordinal),
    which is never accounted with a re-ask.

    - `feed`: the `page-feed` payload (`page_id`, `page_ordinal`,
      `switches.witness_units`, `witnesses[]` with `letter`, `outcome` and
      `units[]` of `{id, box_px | None, text}`, `surya` with `lines[]` and
      `blocks[]` of `{id, box_px}` as shown, or `None`). Entries are placed by
      `placement_boxes`, the map the stage cuts its act regions from.
    - `witnesses`: every witness the run sealed for the page, whatever the
      feed's `witnesses` switch showed the model: `[{letter, outcome, blank,
      units}]`, `units[]` of `{id, box_px | None, text}` with ids of the
      witness's own letter numbered `1..n`, and `blank` whether its retained
      page text is blank, measured from that text (`None` when it did not
      read, `read` and `genuinely-empty` being reading, or its retained
      payload is not text). A
      witness the feed shows is here with the same outcome and the same
      units; one it hides is measured by rule (e) against every reading on
      the page, and rule (c) does not apply to it (its units' disposition is
      `not-shown`).
    - `detections`: the page's sealed detections, whatever the feed showed the
      model: `{"surya": {"lines": [...], "blocks": [...]}
      | None, "records": [...] | None, "record_detector": "configured" |
      "absent", "record_census": {"detection_count", "max_det",
      "max_det_reached"} | None}`, each line, block and record `{id?, box_px,
      ref}` with `id` the feed id when the feed showed it. `surya` is `None`
      when the page has no Surya census.
      `records` and `record_census` are the record detector's records and page
      census, both `None` when it did not run or failed for the page; a record
      whose corners enclose no crop has `box_px: None` and is reported not
      measured by rule (i). `record_detector` is `"absent"` only when the
      sealed roster has none.
    - `reading`: the `page-reading` payload's `parse_state`, `finish_reason` and
      `answer` (the parsed object as given, or `None`).
    - `entry_truncation`: `{n: "complete" | "truncated" | "unknown"}`, each
      entry's truncation classification; an entry missing here is
      `truncation-not-classified` (not measured, which holds).
    - `ink`: `{"runs": <ink-runs.v2 evidence> | None, "coverage_policy":
      <CoverageAuditPolicy resolved for this page>}`, or `None`.
    - `reask`: `None` for a first reading's accounting, whose rule (j) is
      `not-applicable`. For its re-ask's, `{"reading": {parse_state,
      finish_reason, answer}, "named": [id, ...], "entry_truncation": {j:
      classification}}`: `reading` is then the first reading, which must be
      a parsed answer, and `page_reading_ref` the re-ask's. The first
      reading is read against every candidate, the re-ask against the named
      ids only (`validate_reask_answer`). The entries measured are the first
      reading's, exactly, then the re-ask's numbered on after them when it
      stands (`_combined`); rule (a) reads the first reading alone, rules
      (b) to (i) the entries together, rule (g) each entry's truncation under
      its own reading, and rule (j) the re-ask.

    A missing input is never a pass: without a Surya census rule (d), without
    ink runs rule (f), and without detector records from a configured detector,
    or with a detector that reached its cap, rule (i) is `not-measured`, which
    holds. The verdict does not depend on the
    order of any input list.
    """
    if attempt != FIRST_READING and (
        reask is not None or not isinstance(attempt, int) or attempt < OPERATOR_REREAD_FIRST
    ):
        raise ContractError(
            "a page accounting is of a first reading (1) or an operator re-read (3 or more), "
            f"never with a re-ask; not attempt {attempt!r}"
        )
    candidates = feed_candidates(feed, policy)
    census_lines, records, detector, capped = _read_detections(detections, feed)
    parse_state = reading["parse_state"]
    finish_reason = reading["finish_reason"]
    if parse_state not in PARSE_STATES:
        raise ContractError(f"page reading parse_state {parse_state!r} is not a known state")
    sealed, shown_letters = _read_witnesses(witnesses, feed)
    shown = [witness for witness in sealed if witness["letter"] in shown_letters]
    units = sorted(
        (unit for witness in sealed for unit in witness["units"]),
        key=lambda unit: id_key(unit["id"]),
    )
    shown_units = [unit for unit in units if unit["id"][0] in shown_letters]
    unit_boxes = {unit["id"]: unit["box_px"] for unit in units}

    rules: dict[str, dict[str, Any]] = {}
    answered = parse_state == PARSED and reading.get("answer") is not None
    validated = (
        validate_answer(reading["answer"], candidates, policy=policy)
        if answered
        else {"entries": [], "set_aside": {}, "problems": []}
    )
    entries = sorted(validated["entries"], key=lambda entry: entry["n"])
    set_aside = validated["set_aside"]
    problems = validated["problems"]
    combined = None
    if reask is not None:
        if not answered:
            raise ContractError("a re-ask is accounted only over a first reading that parsed")
        combined = _combined({**validated, "entries": entries}, reask, candidates)
        entries = entries + combined["entries"]
        set_aside = {**set_aside, **combined["set_aside"]}

    # (a) the answer is complete: finished on `stop`, parsed and valid.
    incomplete = []
    if not finished_on_stop(reading):
        incomplete.append({"code": "finish-reason", "finish_reason": finish_reason})
    if parse_state != PARSED:
        incomplete.append({"code": "parse-state", "parse_state": parse_state})
    elif reading.get("answer") is None:
        incomplete.append({"code": "no-answer"})
    incomplete.extend(p for p in problems if _PROBLEM_RULE.get(p["code"], "a") == "a")
    rules["a"] = _rule(
        [{"code": PAGE_ANSWER_INCOMPLETE, "problem": problem} for problem in incomplete]
    )

    # (g) a truncated or failed reading holds; measured with or without an answer.
    incomplete_reading = []
    if parse_state in FAILED_PARSE_STATES or finish_reason == "length":
        incomplete_reading.append(
            {"code": READING_INCOMPLETE, "parse_state": parse_state, "finish_reason": finish_reason}
        )
    for entry in entries:
        classification = (
            reask["entry_truncation"].get(entry["reading_n"])
            if entry.get("reading_attempt") == REASK_READING
            else entry_truncation.get(entry["n"])
        )
        if classification is None:
            incomplete_reading.append({"code": TRUNCATION_NOT_CLASSIFIED, "n": entry["n"]})
        elif classification != TRUNCATION_COMPLETE:
            incomplete_reading.append(
                {"code": READING_INCOMPLETE, "n": entry["n"], "truncation": classification}
            )

    cited_by: dict[str, list[int]] = {}
    for entry in entries:
        for identifier in entry["cited_ids"]:
            cited_by.setdefault(identifier, []).append(entry["n"])
    unit_rows = [
        {
            "id": unit["id"],
            "disposition": "not-shown"
            if unit["id"][0] not in shown_letters
            else "cited"
            if unit["id"] in cited_by
            else "set-aside"
            if unit["id"] in set_aside
            else "unaccounted",
            "by": sorted(cited_by.get(unit["id"], [])),
        }
        for unit in units
    ]
    regions = {entry["n"]: entry["region_boxes_px"] for entry in entries}
    line_rows = [
        {
            "id": line["id"],
            "ref": line["ref"],
            "inside": sorted(
                n for n, region in regions.items() if is_inside(line["box_px"], region, policy)
            ),
        }
        for line in census_lines or []
    ]
    record_rows = _record_rows(records, entries, set_aside, policy)
    unboxed_records = [
        {"code": RECORD_NOT_MEASURED, "id": record["id"], "ref": record["ref"]}
        for record in records or []
        if record["box_px"] is None
    ]

    if not answered:
        no_answer = [{"code": NO_PARSED_ANSWER}]
        for rule in ("b", "c", "d", "e", "f", "h", "i"):
            rules[rule] = _rule(list(no_answer))
        rules["g"] = _rule(incomplete_reading)
        rules["j"] = _reask_rule(None, [], [], policy)
        return _record(
            feed,
            rules,
            unit_rows,
            line_rows,
            record_rows,
            policy,
            feed_ref,
            page_reading_ref,
            entries,
            reask,
            attempt,
        )

    # (b) every cited id exists, and every entry cites a boxed id.
    rules["b"] = _rule(
        [p for p in problems if p["code"] == UNKNOWN_ID]
        + [
            {"code": READING_UNPLACED, "n": entry["n"]}
            for entry in entries
            if not entry["region_boxes_px"]
        ]
    )

    # (c) every witness the feed showed read the page, and every unit it showed is
    # cited or set aside with a reason; a set-aside unit carrying more than a folio number or a short header
    # is a set-aside act until a human says otherwise.
    unaccounted = [
        _located({"code": UNACCOUNTED_WITNESS_UNIT, "id": row["id"]}, unit_boxes[row["id"]])
        for row in unit_rows
        if row["disposition"] == "unaccounted"
    ]
    substantial = [
        _located(
            {
                "code": SET_ASIDE_SUBSTANTIAL,
                "id": unit["id"],
                "reason": set_aside[unit["id"]],
                "unit_characters": len(normalized_text(unit["text"])),
            },
            unit_boxes[unit["id"]],
        )
        for unit in shown_units
        if unit["id"] in set_aside
        and len(normalized_text(unit["text"])) > policy.max_set_aside_characters
    ]
    rules["c"] = _rule(_witness_findings(shown) + unaccounted + substantial)

    # (d) every detected line lies inside the reading regions or is set aside,
    # shown to the model or not.
    all_regions = [box for region in regions.values() for box in region]
    if census_lines is None:
        rules["d"] = _rule([{"code": UNREAD_LINE_NOT_MEASURED}])
    else:
        rules["d"] = _rule(
            [
                {
                    "code": UNREAD_LINE,
                    "id": line["id"],
                    "ref": line["ref"],
                    "box_px": line["box_px"],
                }
                for line in census_lines
                if line["id"] not in set_aside
                and not is_inside(line["box_px"], all_regions, policy)
            ]
        )

    rules["e"] = _witness_text_rule(sealed, units, entries, set_aside, cited_by, unit_boxes, policy)
    rules["f"] = _ink_rule(ink, all_regions)
    rules["g"] = _rule(incomplete_reading)

    # (h) two entries on one region hold; a line inside two regions is recorded.
    rules["h"] = _rule(
        duplicate_regions(entries, policy)
        + [
            {"code": SHARED_LINE, "id": row["id"], "ref": row["ref"], "inside": row["inside"]}
            for row in line_rows
            if len(row["inside"]) > 1
        ]
    )
    rules["i"] = _detection_rule(record_rows, detector, capped, entries, unboxed_records)
    rules["j"] = _reask_rule(combined, entries, units, policy, reask)
    return _record(
        feed,
        rules,
        unit_rows,
        line_rows,
        record_rows,
        policy,
        feed_ref,
        page_reading_ref,
        entries,
        reask,
        attempt,
    )


def _witness_findings(witnesses: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each witness that did not read the page, or read it and gave no unit.

    A witness that read the page (`read` or `genuinely-empty`) and gave no unit
    holds unless its retained page text is blank; then it is recorded.
    """
    findings = []
    for witness in witnesses:
        where = {"letter": witness["letter"], "outcome": witness["outcome"]}
        if witness["outcome"] not in WITNESS_READING_OUTCOMES:
            findings.append({"code": WITNESS_NOT_READ, **where})
        elif not witness["units"]:
            code = WITNESS_READ_BLANK if witness["blank"] is True else WITNESS_READ_NO_UNITS
            findings.append({"code": code, **where})
    return findings


def _witness_text_rule(
    witnesses: list[Mapping[str, Any]],
    units: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    set_aside: Mapping[str, str],
    cited_by: Mapping[str, list[int]],
    unit_boxes: Mapping[str, Box | None],
    policy: PageAccountingPolicy,
) -> dict[str, Any]:
    """(e) Every witness unit's own text appears in the readings that cite it.

    A unit is compared with the readings of the entries citing it, joined in
    `n` order; an uncited unit, or one of a witness the feed hid, with every
    reading. Scoping to the citing
    entries is what catches a unit that merged two records while only one was
    read: the other record's formula would otherwise find itself in a
    neighbouring entry's reading. A unit is held (`witness-text-not-read`,
    naming every reason) when:

    - `unread-run`: a run of its text longer than `max_unread_characters` is unread;
    - `unread-share`: its unread text exceeds `max_unread_share_bp` of it;
    - `no-match`: nothing of it matched and no `[[?]]` accounts for it;
    - `distinctive-share`: under `min_distinctive_share_bp` of its distinctive
      pieces (see `_distinctive_pieces`) are held where the citing readings
      align them (see `_distinctive_share`). This is what holds a reading of the neighbouring
      record: same formula, other names;
    - `short-unit-distance`: a unit with fewer than `min_pieces` distinctive
      pieces, whose share is mostly chance, is instead held when its text is
      further than `max_short_unit_distance_bp` of its length from every
      stretch of the citing readings (see `_short_unit_distance_bp`). A long
      entry's text stands opposite every letter of a short unit it does not
      transcribe, so no run of it is unread; only this distance shows it. The
      unit is recorded as `too-few-distinctive-pieces` with that distance.

    `[[?]]` credit is deliberate: an entry whose text is only `[[?]]` marks
    passes this rule for every unit its marks can account for (up to
    `max_unread_characters` each), because the reading's own uncertainty
    already routes those gaps to review.

    Every measured unit's run, shares and piece count are kept as
    `measurements`, so the proof run can set these thresholds from real pages.
    """
    budget = _WorkBudget(policy.max_alignment_steps)
    readings = {entry["n"]: normalized_text(entry["text"], unreadable=True) for entry in entries}
    texts, distinctive_by_unit = _distinctive_pieces(units, readings, policy.piece_characters)
    findings: list[dict[str, Any]] = _witness_findings(witnesses)
    measurements: list[dict[str, Any]] = []
    for unit in units:
        identifier = unit["id"]
        witness = texts[identifier]
        if identifier in set_aside or not witness:
            continue
        scope = sorted(cited_by.get(identifier, readings))
        joined = "".join(readings[n] for n in scope)
        try:
            coverage = _text_coverage(witness, joined, policy, budget)
        except _NotMeasured as error:
            findings.append(
                {"code": WITNESS_TEXT_NOT_MEASURED, "id": identifier, "reason": error.reason}
            )
            continue
        reasons = []
        if coverage.unread_run > policy.max_unread_characters:
            reasons.append("unread-run")
        if coverage.unread_share_bp > policy.max_unread_share_bp:
            reasons.append("unread-share")
        if (
            coverage.matched_characters == 0
            and coverage.unread_characters > 0
            and len(witness) >= policy.min_block_characters
        ):
            reasons.append("no-match")
        distinctive = distinctive_by_unit[identifier]
        share_bp = distance_bp = None
        if len(distinctive) < policy.min_pieces:
            distance_bp = _short_unit_distance_bp(witness, joined, coverage, policy)
            findings.append(
                {
                    "code": TOO_FEW_DISTINCTIVE_PIECES,
                    "id": identifier,
                    "distinctive_pieces": len(distinctive),
                    "distance_bp": distance_bp,
                }
            )
            if distance_bp > policy.max_short_unit_distance_bp:
                reasons.append("short-unit-distance")
        else:
            share_bp = _distinctive_share(witness, joined, distinctive, coverage, policy)
            if share_bp < policy.min_distinctive_share_bp:
                reasons.append("distinctive-share")
        measurements.append(
            {
                "id": identifier,
                "unit_characters": len(witness),
                "unread_run": coverage.unread_run,
                "unread_share_bp": coverage.unread_share_bp,
                "distinctive_pieces": len(distinctive),
                "distinctive_share_bp": share_bp,
                "distance_bp": distance_bp,
            }
        )
        if reasons:
            findings.append(
                _located(
                    {
                        "code": WITNESS_TEXT_NOT_READ,
                        "id": identifier,
                        "reasons": reasons,
                        "unread_characters": coverage.unread_run,
                        "unread_share_bp": coverage.unread_share_bp,
                        "distinctive_share_bp": share_bp,
                        "distinctive_pieces": len(distinctive),
                        "distance_bp": distance_bp,
                        "unit_characters": len(witness),
                        "compared_with": scope,
                    },
                    unit_boxes[identifier],
                )
            )
    return {**_rule(findings), "measurements": measurements}


def _record_rows(
    records: list[dict[str, Any]] | None,
    entries: list[dict[str, Any]],
    set_aside: Mapping[str, str],
    policy: PageAccountingPolicy,
) -> list[dict[str, Any]] | None:
    """Each detector record with the `act` and `other` regions it lies inside."""
    if records is None:
        return None
    rows = []
    for record in records:
        if record["box_px"] is None:
            continue
        inside = {
            kind: sorted(
                entry["n"]
                for entry in entries
                if entry["kind"] == kind
                and is_inside(record["box_px"], entry["region_boxes_px"], policy)
            )
            for kind in ("act", "other")
        }
        rows.append(
            {
                "id": record["id"],
                "ref": record["ref"],
                "box_px": record["box_px"],
                "act": inside["act"],
                "other": inside["other"],
                "set_aside": record["id"] in set_aside,
            }
        )
    return rows


def _detection_rule(
    rows: list[dict[str, Any]] | None,
    detector: str,
    capped: bool,
    entries: list[dict[str, Any]],
    unboxed: list[dict[str, Any]],
) -> dict[str, Any]:
    """(i) Each detector record lies inside exactly one `act` region.

    The detector's records are independent evidence of where one entry ends.
    Two of them inside one reading region is the Perlector reading two entries
    as one, citing and transcribing both, which no text or coverage rule can
    see: `merged-detection`, held. A record inside only an `other` region is
    `record-read-as-other`, one inside no reading region `record-not-read`, and
    one set aside `set-aside-record`: each an entry the reading did not
    establish as an act, held. One inside two act regions is `split-detection`,
    recorded: a detector record that merged two entries the Perlector read apart.
    A detector that found no record at all below its cap on a page whose
    reading establishes acts disagrees with the whole reading:
    `no-detector-record-on-act-page`, held, naming the act entries. Its
    record reader's page testimony there is that the page holds nothing.

    Without the detector's records for the page, or when the detector reached
    its detection cap (`record-detector-capped`), the rule is not measured,
    which holds; it does not apply only when the sealed roster has no record
    detector. It cannot see a detector record that itself merged two entries,
    read as one act: that is one record inside one region. The proof run
    measures that case against gold.

    A record whose corners enclose no crop has no box to measure: each is a
    `detector-record-not-measured` finding, which holds, and the rule states
    how many (`records_not_measured`: 0 with no detector, null when the page
    has no records to count).
    """
    if rows is None:
        if detector == RECORD_DETECTOR_ABSENT:
            return {
                "status": NOT_APPLICABLE,
                "findings": [{"code": NO_RECORD_DETECTOR}],
                "records_not_measured": 0,
            }
        # No records to count: how many would go unmeasured is itself unknown.
        return {**_rule([{"code": RECORDS_NOT_MEASURED}]), "records_not_measured": None}
    if capped:
        # The detector stopped at its cap: records past it were never cut, so
        # "every record is inside one act" cannot be measured.
        return {
            **_rule([{"code": RECORD_DETECTOR_CAPPED}, *unboxed]),
            "records_not_measured": len(unboxed),
        }
    kinds = {entry["n"]: entry["kind"] for entry in entries}
    findings: list[dict[str, Any]] = list(unboxed)
    acts = [entry["n"] for entry in entries if entry["kind"] == "act"]
    if not rows and not unboxed and acts:
        findings.append({"code": NO_RECORD_ON_ACT_PAGE, "acts": acts})
    inside_region: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        where = {"id": row["id"], "ref": row["ref"], "box_px": row["box_px"]}
        if row["set_aside"]:
            findings.append({"code": SET_ASIDE_RECORD, **where})
        if len(row["act"]) > 1:
            findings.append({"code": SPLIT_DETECTION, **where, "inside": row["act"]})
        if not row["act"] and row["other"]:
            findings.append({"code": RECORD_READ_AS_OTHER, **where, "inside": row["other"]})
        if not row["act"] and not row["other"] and not row["set_aside"]:
            findings.append({"code": RECORD_NOT_READ, **where})
        for n in row["act"] + row["other"]:
            inside_region.setdefault(n, []).append(where)
    findings.extend(
        {
            "code": MERGED_DETECTION,
            "n": n,
            "kind": kinds[n],
            "records": [{"id": item["id"], "ref": item["ref"]} for item in inside],
        }
        for n, inside in sorted(inside_region.items())
        if len(inside) > 1
    )
    return {**_rule(findings), "records_not_measured": len(unboxed)}


def _ink_rule(ink: Mapping[str, Any] | None, regions: list[Box]) -> dict[str, Any]:
    """(f) No substantial ink lies outside every reading region.

    Only reading regions cover ink: a set-aside box is the reader's claim that
    its ink is not an act, which is what this rule checks rather than assumes.
    """
    if ink is None or ink.get("runs") is None:
        return _rule([{"code": UNREAD_INK_NOT_MEASURED}])
    coverage_policy: CoverageAuditPolicy = ink["coverage_policy"]
    measured = residual_ink_from_runs(ink["runs"], list(regions), coverage_policy=coverage_policy)
    counts = {
        "total_ink_pixels": measured["total_ink_pixels"],
        "outside_ink_pixels": measured["outside_ink_pixels"],
        "substantial_ink_pixels": measured["substantial_ink_pixels"],
    }
    if measured["flagged"]:
        return _rule([{"code": UNREAD_INK, **counts}])
    return {"status": PASS, "findings": [], "measurement": counts}


def _reask_rule(
    combined: Mapping[str, Any] | None,
    entries: list[dict[str, Any]],
    units: list[dict[str, Any]],
    policy: PageAccountingPolicy,
    reask: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """(j) What became of the page's re-ask; not applicable to a first reading's accounting.

    A re-ask that is not a parsed, valid answer finished on `stop` is
    `reask-unread`, and the page stands on its first reading alone. One that
    stands holds each named id it set aside (`reask-set-aside`), each entry of
    it citing no placing id (`reask-unplaced`) or giving no text beyond
    `[[?]]` (`reask-no-text`), and each entry whose text a first-reading
    entry already holds (`reask-duplicate`, `_reask_duplicates`). So a named
    id's hold clears only through a placed entry that gives text and passes
    every rule.
    """
    if combined is None or reask is None:
        return {"status": NOT_APPLICABLE, "findings": []}
    if not combined["stands"]:
        reading = reask["reading"]
        return _rule(
            [
                {
                    "code": REASK_UNREAD,
                    "parse_state": reading["parse_state"],
                    "finish_reason": reading["finish_reason"],
                    "problems": [problem["code"] for problem in combined["problems"]],
                }
            ]
        )
    findings: list[dict[str, Any]] = [
        {"code": REASK_SET_ASIDE, "id": identifier, "reason": combined["set_aside"][identifier]}
        for identifier in sorted(combined["set_aside"], key=id_key)
    ]
    added = [entry for entry in entries if entry.get("reading_attempt") == REASK_READING]
    findings += [
        {"code": REASK_UNPLACED, "n": entry["n"], "reading_n": entry["reading_n"]}
        for entry in added
        if not entry["region_boxes_px"]
    ]
    findings += [
        {"code": REASK_NO_TEXT, "n": entry["n"], "reading_n": entry["reading_n"]}
        for entry in added
        if not normalized_text(entry["text"])
    ]
    first = [entry for entry in entries if entry.get("reading_attempt") != REASK_READING]
    findings += _reask_duplicates(first, added, units, policy)
    return _rule(findings)


@dataclass(frozen=True)
class _PageFormula:
    """The page's witness units and first-reading entries, indexed to tell formula from names."""

    by_letter: dict[str, list[_NearPieces]]
    exact: set[str]
    readings: list[_NearPieces]

    @classmethod
    def of(cls, units: list[dict[str, Any]], readings: Mapping[int, str], k: int) -> _PageFormula:
        by_letter: dict[str, list[_NearPieces]] = {}
        exact: set[str] = set()
        for unit in units:
            text = normalized_text(unit["text"])
            by_letter.setdefault(unit["id"][0], []).append(_NearPieces(text, k))
            exact |= _pieces(text, k)
        return cls(by_letter, exact, [_NearPieces(reading, k) for reading in readings.values()])


def _entry_distinctive_pieces(text: str, formula: _PageFormula, k: int) -> dict[str, int]:
    """A re-ask entry's distinctive pieces with their first offset, as rule (e) keeps a unit's.

    A piece is the register's formula when two units of one witness hold it
    within one edit -- the witness wrote it in two records -- or two
    first-reading entries do; any other piece is kept when a witness unit
    on the page holds it exactly, so a piece of the entry's own invention
    never counts. What is left are the names and dates that tell one record
    from another.

    It differs from rule (e)'s `_distinctive_pieces` because the text is a
    reading, not a witness unit. Rule (e) drops a piece another unit of the
    unit's own witness holds, since that witness wrote it twice; an entry
    belongs to no witness, so one unit holding its piece is the corroboration
    and only two units of one witness make it formula. And rule (e) takes
    corroboration from a reading within one edit, which here would let the
    first-reading entry under test corroborate the duplicate it is tested for,
    so only the witnesses corroborate.
    """
    kept: dict[str, int] = {}
    for offset in range(len(text) - k + 1):
        piece = text[offset : offset + k]
        if piece in kept or piece not in formula.exact:
            continue
        if sum(index.near(piece) for index in formula.readings) >= 2 or any(
            sum(index.near(piece) for index in indexes) >= 2
            for indexes in formula.by_letter.values()
        ):
            continue
        kept[piece] = offset
    return kept


def _reask_duplicates(
    first: list[dict[str, Any]],
    added: list[dict[str, Any]],
    units: list[dict[str, Any]],
    policy: PageAccountingPolicy,
) -> list[dict[str, Any]]:
    """Each re-ask entry whose text one first-reading entry already holds, by rule (e)'s test.

    The re-ask entry stands as a unit and each first-reading entry as a
    reading: it is inside that entry when rule (e) would find a witness unit
    with its text read there -- no unread run or share past the sealed
    bounds, some of it matched, and its distinctive pieces held (or, with
    too few, its text within the short-unit distance). Its distinctive pieces
    are `_entry_distinctive_pieces`', so the register's formula never makes
    two entries one. A re-ask may copy a first reading's entry it was never
    shown the text of only by reading the same ink, so each such entry is
    held for a human to look at.
    """
    if not added or not first:
        return []
    budget = _WorkBudget(policy.max_alignment_steps)
    readings = {entry["n"]: normalized_text(entry["text"], unreadable=True) for entry in first}
    formula = _PageFormula.of(units, readings, policy.piece_characters)
    findings: list[dict[str, Any]] = []
    for entry in added:
        text = normalized_text(entry["text"])
        if not text:
            continue
        distinctive = _entry_distinctive_pieces(text, formula, policy.piece_characters)
        for n, reading in sorted(readings.items()):
            try:
                coverage = _text_coverage(text, reading, policy, budget)
            except _NotMeasured as error:
                findings.append(
                    {
                        "code": REASK_DUPLICATE_NOT_MEASURED,
                        "n": entry["n"],
                        "reading_n": entry["reading_n"],
                        "reason": error.reason,
                    }
                )
                break
            if (
                coverage.unread_run > policy.max_unread_characters
                or coverage.unread_share_bp > policy.max_unread_share_bp
                or coverage.matched_characters == 0
            ):
                continue
            if len(distinctive) < policy.min_pieces:
                held = (
                    _short_unit_distance_bp(text, reading, coverage, policy)
                    <= policy.max_short_unit_distance_bp
                )
            else:
                held = (
                    _distinctive_share(text, reading, distinctive, coverage, policy)
                    >= policy.min_distinctive_share_bp
                )
            if held:
                findings.append(
                    {
                        "code": REASK_DUPLICATE,
                        "n": entry["n"],
                        "reading_n": entry["reading_n"],
                        "attempt_1_n": n,
                    }
                )
    return findings


def _record(
    feed: Mapping[str, Any],
    rules: dict[str, dict[str, Any]],
    unit_rows: list[dict[str, Any]],
    line_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]] | None,
    policy: PageAccountingPolicy,
    feed_ref: Any,
    page_reading_ref: Any,
    entries: list[dict[str, Any]],
    reask: Mapping[str, Any] | None,
    attempt: int = FIRST_READING,
) -> dict[str, Any]:
    holds = sorted(
        {
            finding["code"]
            for rule in rules.values()
            for finding in rule["findings"]
            if finding["code"] in HOLD_CODES
        }
    )
    return {
        "schema": SCHEMA,
        "page_id": feed["page_id"],
        "page_ordinal": feed["page_ordinal"],
        "page_reading_ref": page_reading_ref,
        "feed_ref": feed_ref,
        "answer_basis": answer_basis(attempt) if reask is None else ANSWER_BASIS_COMBINED,
        # Each measured entry by the number the rules name it by, and the
        # reading and number it has there.
        "entries": [
            {
                "n": entry["n"],
                "reading_attempt": entry.get("reading_attempt", attempt),
                "reading_n": entry.get("reading_n", entry["n"]),
                "kind": entry["kind"],
                "cited_ids": sorted(entry["cited_ids"], key=id_key),
                "union_box_px": entry["union_box_px"],
            }
            for entry in entries
        ],
        "rules": {name: rules[name] for name in RULES},
        "units": unit_rows,
        "lines": line_rows,
        "records": record_rows,
        "holds": holds,
        "policy_sha256": policy.sha256,
    }
