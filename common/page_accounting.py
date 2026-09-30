"""The model-free check that nothing on a page was missed by its page reading.

A page reading is one Perlector answer for a whole page: entries (`act` or
`other`) that cite the candidate ids of the page feed -- witness units `A1..`,
Surya lines `L1..` and blocks `S1..` -- plus the ids it set aside with a reason.
This module takes that answer and the feed as plain data and says, rule by rule,
whether every witness unit, every Surya line, every witness's text and the
page's ink are accounted for. It reads no file, calls no model and chooses
nothing among the witnesses; any hold holds the page's acts for review.

`expand_cites` and `validate_answer` are the one reading of an answer's ids that
both this check and the stage writing the act-region records use.

Boxes are `[x0, y0, x1, y1]` in sealed-page pixels, half-open, as the feed
records `box_px`.
"""

from __future__ import annotations

import re
import time
import unicodedata
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final

from common.alignment import markup_text_view
from common.contracts.errors import ContractError
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from common.imaging import Bounds
from common.perlector_audit import TRUNCATION_COMPLETE
from common.residual_ink import CoverageAuditPolicy, residual_ink_from_runs
from common.sealed_config import read_sealed_toml

SCHEMA: Final = "page-accounting.v1"
DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "page_accounting.toml"
)
BASIS_POINTS: Final = 10_000

# Answer problems, recorded on the page reading and held by the rule named in
# `_PROBLEM_RULE` (rule a when not named).
ANSWER_NOT_AN_OBJECT: Final = "answer-not-an-object"
MALFORMED_ENTRY: Final = "malformed-entry"
UNKNOWN_ID: Final = "unknown-id"
MALFORMED_RANGE: Final = "malformed-range"
CITED_AND_SET_ASIDE: Final = "cited-and-set-aside"
SET_ASIDE_TWICE: Final = "set-aside-twice"
SET_ASIDE_WITHOUT_REASON: Final = "set-aside-without-reason"
NON_CONTIGUOUS_N: Final = "non-contiguous-n"
CONTINUATION_ON_NON_EDGE_ACT: Final = "continuation-flag-on-non-edge-act"
DUPLICATE_REGION: Final = "duplicate-region"

# Finding codes. Every code in `HOLD_CODES` holds the page; `shared-line` is recorded only.
PAGE_ANSWER_INCOMPLETE: Final = "page-answer-incomplete"
READING_UNPLACED: Final = "reading-unplaced"
UNACCOUNTED_WITNESS_UNIT: Final = "unaccounted-witness-unit"
UNREAD_LINE: Final = "unread-line"
WITNESS_TEXT_NOT_READ: Final = "witness-text-not-read"
WITNESS_TEXT_NOT_MEASURED: Final = "witness-text-not-measured"
UNREAD_INK: Final = "unread-ink"
UNREAD_INK_NOT_MEASURED: Final = "unread-ink-not-measured"
READING_INCOMPLETE: Final = "reading-incomplete"
SHARED_LINE: Final = "shared-line"
MERGED_DETECTION: Final = "merged-detection"
UNDETECTED_READ: Final = "undetected-read"
SPLIT_DETECTION: Final = "split-detection"
NO_PARSED_ANSWER: Final = "no-parsed-answer"
HOLD_CODES: Final = frozenset(
    {
        PAGE_ANSWER_INCOMPLETE,
        UNKNOWN_ID,
        READING_UNPLACED,
        UNACCOUNTED_WITNESS_UNIT,
        UNREAD_LINE,
        WITNESS_TEXT_NOT_READ,
        WITNESS_TEXT_NOT_MEASURED,
        UNREAD_INK,
        UNREAD_INK_NOT_MEASURED,
        READING_INCOMPLETE,
        DUPLICATE_REGION,
        MERGED_DETECTION,
    }
)
# Findings that say a rule could not be measured: the rule is `not-measured`, and the
# page is held (by the finding's own code, or by rule (a) when there is no answer).
NOT_MEASURED_CODES: Final = frozenset(
    {WITNESS_TEXT_NOT_MEASURED, UNREAD_INK_NOT_MEASURED, NO_PARSED_ANSWER}
)
_PROBLEM_RULE: Final = {UNKNOWN_ID: "b", DUPLICATE_REGION: "h"}

PASS: Final = "pass"
HOLD: Final = "hold"
NOT_MEASURED: Final = "not-measured"
RULES: Final = ("a", "b", "c", "d", "e", "f", "g", "h", "i")

PARSED: Final = "parsed"
FAILED_PARSE_STATES: Final = frozenset({"cut-off", "call-failed", "refused-capacity", "not-run"})
PARSE_STATES: Final = FAILED_PARSE_STATES | {PARSED, "malformed"}

_ID: Final = re.compile(r"([A-Z])([1-9][0-9]*)")
_RANGE: Final = re.compile(r"([A-Z])([1-9][0-9]*)-([A-Z])([1-9][0-9]*)")
_DOUBT_MARK: Final = re.compile(r"\[\[([^\[\]]*)\]\]")
_ENTRY_REQUIRED: Final = frozenset(
    {"n", "kind", "cites", "text", "continues_from_previous_page", "continues_to_next_page"}
)
_ENTRY_KINDS: Final = frozenset({"act", "other"})
MAX_LABEL_CHARACTERS: Final = 80

Box = list[int]


@dataclass(frozen=True)
class PageAccountingPolicy:
    """The sealed thresholds, with the digest of the bytes they came from."""

    inside_min_area_bp: int
    max_unread_characters: int
    min_block_characters: int
    anchor_characters: int
    direct_alignment_max_pairs: int
    max_alignment_pairs: int
    max_characters: int
    deadline_milliseconds: int
    sha256: str


_POLICY_TABLES: Final = {
    "inside": ("min_area_bp",),
    "witness_text": (
        "max_unread_characters",
        "min_block_characters",
        "anchor_characters",
        "direct_alignment_max_pairs",
        "max_alignment_pairs",
        "max_characters",
        "deadline_milliseconds",
    ),
}


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
        table: {field: record[table][field] for field in fields}
        for table, fields in _POLICY_TABLES.items()
    }
    for table in values.values():
        for field, value in table.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError(f"page accounting {field} must be a non-negative integer")
    inside = values["inside"]["min_area_bp"]
    if not 0 < inside <= BASIS_POINTS:
        raise ContractError("page accounting inside.min_area_bp must be in 1..10000")
    text = values["witness_text"]
    if any(value <= 0 for value in text.values()):
        raise ContractError("page accounting witness_text limits must be positive")
    if text["direct_alignment_max_pairs"] > text["max_alignment_pairs"]:
        raise ContractError(
            "page accounting direct_alignment_max_pairs exceeds max_alignment_pairs"
        )
    return PageAccountingPolicy(inside_min_area_bp=inside, **text, sha256=digest)


# --- candidates and the answer's ids -------------------------------------------------


def _box(value: Any, where: str) -> Box:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(v, int) or isinstance(v, bool) for v in value)
        or not (0 <= value[0] < value[2] and 0 <= value[1] < value[3])
    ):
        raise ContractError(f"{where} box_px is not [x0, y0, x1, y1] with x0 < x1 and y0 < y1")
    return list(value)


def feed_candidates(feed: Mapping[str, Any]) -> dict[str, Box | None]:
    """Every citable id of a page feed and its box (`None` for an unboxed unit).

    `feed` is the `page-feed` payload: `witnesses[].units[]` with `id`, `box_px`
    and `text`, and `surya.lines[]` / `surya.blocks[]` with `id` and `box_px`.
    """
    candidates: dict[str, Box | None] = {}

    def add(identifier: Any, box: Any, *, boxed: bool) -> None:
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ContractError(f"feed id {identifier!r} is not a letter and a number")
        if identifier in candidates:
            raise ContractError(f"feed id {identifier} appears twice")
        candidates[identifier] = _box(box, identifier) if boxed or box is not None else None

    for witness in feed["witnesses"]:
        for unit in witness["units"]:
            add(unit["id"], unit.get("box_px"), boxed=False)
    surya = feed.get("surya") or {}
    for line in surya.get("lines", []):
        add(line["id"], line["box_px"], boxed=True)
    for block in surya.get("blocks", []):
        add(block["id"], block["box_px"], boxed=True)
    return candidates


def expand_cites(
    cites: Sequence[Any], candidates: Mapping[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """The known ids a list of citations names, in the order given, and its problems.

    A citation is one id (`A2`) or a range (`L10-L17`: one letter, ascending,
    both ends inclusive). An id or range end the feed does not define is
    `unknown-id`; any other shape is `malformed-range`. Nothing is guessed: a
    problem contributes no id.
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
        letter, first, last = match[1], int(match[2]), int(match[4])
        span = [f"{letter}{number}" for number in range(first, last + 1)]
        missing = [identifier for identifier in (span[0], span[-1]) if identifier not in candidates]
        if missing:
            problems.extend({"code": UNKNOWN_ID, "id": identifier} for identifier in missing)
            continue
        # Ids are numbered 1..n per letter, so both ends known means every id between is.
        ids.extend(identifier for identifier in span if identifier in candidates)
    return list(dict.fromkeys(ids)), problems


def _union_box(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _entry_shape_problem(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "not an object"
    keys = set(entry)
    if not _ENTRY_REQUIRED <= keys or keys - _ENTRY_REQUIRED - {"label"}:
        return "fields are not the closed entry grammar"
    if not isinstance(entry["n"], int) or isinstance(entry["n"], bool):
        return "n is not an integer"
    if entry["kind"] not in _ENTRY_KINDS:
        return "kind is not act or other"
    if not isinstance(entry["cites"], list):
        return "cites is not a list"
    if not isinstance(entry["text"], str):
        return "text is not a string"
    if not isinstance(entry["continues_from_previous_page"], bool) or not isinstance(
        entry["continues_to_next_page"], bool
    ):
        return "a continuation flag is not a boolean"
    label = entry.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_CHARACTERS):
        return "label is not text of at most 80 characters"
    return None


def validate_answer(answer: Any, candidates: Mapping[str, Box | None]) -> dict[str, Any]:
    """Read a parsed page answer against its feed's candidates, repairing nothing.

    Returns `entries` (one per well-formed entry, in the order given: `n`, `kind`,
    `label`, `cites` as given, `cited_ids` expanded, `union_box_px` -- the
    bounding box of the cited boxed ids, unpadded, or `None` when the entry
    cites no boxed id), `set_aside` (`{id: reason}` for every id set aside with
    a reason) and `problems` (every finding; any one holds the page). An entry
    with no boxed citation is not a problem here: it is published unplaced and
    held by the accounting's rule (b).
    """
    problems: list[dict[str, Any]] = []
    if (
        not isinstance(answer, dict)
        or set(answer) != {"acts", "set_aside"}
        or not isinstance(answer["acts"], list)
        or not isinstance(answer["set_aside"], list)
    ):
        return {
            "entries": [],
            "set_aside": {},
            "problems": [{"code": ANSWER_NOT_AN_OBJECT}],
        }
    entries: list[dict[str, Any]] = []
    last = len(answer["acts"]) - 1
    for index, raw in enumerate(answer["acts"]):
        reason = _entry_shape_problem(raw)
        if reason is not None:
            problems.append({"code": MALFORMED_ENTRY, "index": index, "reason": reason})
            continue
        if (raw["continues_from_previous_page"] and index != 0) or (
            raw["continues_to_next_page"] and index != last
        ):
            problems.append({"code": CONTINUATION_ON_NON_EDGE_ACT, "n": raw["n"]})
        cited_ids, cite_problems = expand_cites(raw["cites"], candidates)
        problems.extend({**problem, "n": raw["n"]} for problem in cite_problems)
        entries.append(
            {
                "n": raw["n"],
                "kind": raw["kind"],
                "label": raw.get("label"),
                "cites": list(raw["cites"]),
                "cited_ids": cited_ids,
                "union_box_px": _union_box(
                    [box for box in (candidates[i] for i in cited_ids) if box is not None]
                ),
                "text": raw["text"],
                "continues_from_previous_page": raw["continues_from_previous_page"],
                "continues_to_next_page": raw["continues_to_next_page"],
            }
        )
    numbers = [raw.get("n") if isinstance(raw, dict) else None for raw in answer["acts"]]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append({"code": NON_CONTIGUOUS_N})

    cited = {identifier for entry in entries for identifier in entry["cited_ids"]}
    set_aside: dict[str, str] = {}
    seen: set[str] = set()
    for index, raw in enumerate(answer["set_aside"]):
        if not isinstance(raw, dict) or set(raw) != {"id", "reason"}:
            problems.append({"code": MALFORMED_ENTRY, "set_aside_index": index})
            continue
        ids, id_problems = expand_cites([raw["id"]], candidates)
        problems.extend({**problem, "set_aside_index": index} for problem in id_problems)
        reason = raw["reason"]
        has_reason = isinstance(reason, str) and bool(reason.strip())
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

    by_box: dict[tuple[int, ...], list[int]] = {}
    for entry in entries:
        if entry["union_box_px"] is not None:
            by_box.setdefault(tuple(entry["union_box_px"]), []).append(entry["n"])
    for box, ns in sorted(by_box.items()):
        if len(ns) > 1:
            problems.append({"code": DUPLICATE_REGION, "ns": sorted(ns), "union_box_px": list(box)})
    return {"entries": entries, "set_aside": set_aside, "problems": problems}


# --- geometry --------------------------------------------------------------------------


def _area(box: Sequence[int]) -> int:
    return (box[2] - box[0]) * (box[3] - box[1])


def _area_inside(box: Box, regions: Sequence[Box]) -> int:
    """The area of `box` covered by the union of `regions`."""
    clipped = [
        (max(box[0], r[0]), max(box[1], r[1]), min(box[2], r[2]), min(box[3], r[3]))
        for r in regions
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


def _bounds(box: Box) -> Bounds:
    return {"x": box[0], "y": box[1], "w": box[2] - box[0], "h": box[3] - box[1]}


# --- witness text coverage (rule e) ------------------------------------------------------


def normalized_text(raw: str) -> str:
    """Letters and digits only, casefolded and without accents, for coverage.

    A doubt mark `[[reading|alt]]` keeps its first reading and `[[?]]` keeps
    nothing; the DAI uncertainty markers, markup tags and entity spelling are
    removed; whitespace and punctuation go, because witnesses and the reader
    break lines, hyphenate and punctuate differently and none of that is ink
    missed. What remains is the evidence a witness read something.
    """
    text = _DOUBT_MARK.sub(
        lambda mark: " " if mark[1] == "?" else " " + mark[1].split("|")[0] + " ", raw
    )
    for token in UNCERTAINTY_TOKENS:
        text = text.replace(token, " ")
    text = markup_text_view(text)["text"]
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text).casefold()
        if character.isalnum()
    )


class _NotMeasured(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Deadline:
    clock: Callable[[], float]
    start: float
    seconds: float

    def check(self) -> None:
        if self.clock() - self.start >= self.seconds:
            raise _NotMeasured("deadline")


def _direct_blocks(
    a: str, b: str, alo: int, ahi: int, blo: int, bhi: int, minimum: int, deadline: _Deadline
) -> list[tuple[int, int, int]]:
    """Ratcliff-Obershelp matching blocks of at least `minimum` characters.

    Longest common run first, then recursively either side of it, so blocks are
    ordered on both texts and each reading character matches at most one witness
    character. A range whose longest run is shorter than `minimum` holds no
    longer one, so it is not searched further.
    """
    matcher = SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
    blocks: list[tuple[int, int, int]] = []
    work = [(0, ahi - alo, 0, bhi - blo)]
    while work:
        deadline.check()
        i0, i1, j0, j1 = work.pop()
        if i0 >= i1 or j0 >= j1:
            continue
        i, j, size = matcher.find_longest_match(i0, i1, j0, j1)
        if size < minimum:
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


_NEIGHBOURS: Final = 3


def _consistent_blocks(
    blocks: list[tuple[int, int, int]], tolerance: int
) -> list[tuple[int, int, int]]:
    """The blocks whose offset agrees with their neighbours' within `tolerance`.

    A block's diagonal `i - j` is the offset between the two texts at that point.
    Misreadings move it a little; a missed passage moves it once and for good.
    A short run matched out of place -- a noisy name that happens to equal the
    same name a record later -- moves it and moves it straight back, and would
    otherwise open a false gap beside it. A block is kept when its diagonal is
    within `tolerance` of the median of the `_NEIGHBOURS` blocks either side
    of it (itself included); dropping blocks keeps the rest ordered on both texts.
    """
    diagonals = [i - j for i, j, _size in blocks]
    kept = []
    for index, block in enumerate(blocks):
        window = sorted(diagonals[max(0, index - _NEIGHBOURS) : index + _NEIGHBOURS + 1])
        if abs(diagonals[index] - window[len(window) // 2]) <= tolerance:
            kept.append(block)
    return kept


def _matching_blocks(
    a: str, b: str, policy: PageAccountingPolicy, deadline: _Deadline
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
        deadline.check()
        alo, ahi, blo, bhi = work.pop()
        if alo >= ahi or blo >= bhi:
            continue
        pairs = (ahi - alo) * (bhi - blo)
        if pairs <= policy.direct_alignment_max_pairs:
            blocks.extend(
                _direct_blocks(a, b, alo, ahi, blo, bhi, policy.min_block_characters, deadline)
            )
            continue
        anchors = _consistent_blocks(
            _anchor_blocks(a, b, alo, ahi, blo, bhi, policy.anchor_characters),
            policy.max_unread_characters,
        )
        if not anchors:
            if pairs > policy.max_alignment_pairs:
                raise _NotMeasured("size-bound")
            blocks.extend(
                _direct_blocks(a, b, alo, ahi, blo, bhi, policy.min_block_characters, deadline)
            )
            continue
        blocks.extend(anchors)
        cursor_a, cursor_b = alo, blo
        for i, j, size in anchors:
            work.append((cursor_a, i, cursor_b, j))
            cursor_a, cursor_b = i + size, j + size
        work.append((cursor_a, ahi, cursor_b, bhi))
    return sorted(blocks)


def unread_run(
    witness: str, reading: str, policy: PageAccountingPolicy, deadline: _Deadline
) -> int:
    """The longest run of normalized witness text the reading does not account for.

    Between two consecutive matched blocks the witness has `gap_w` unmatched
    characters and the reading `gap_r`. Reading characters opposite a witness gap
    account for it one for one -- a misread letter or word substitutes, it does
    not vanish -- but at most one threshold's worth, since a whole entry
    replaced by different text is not a misreading. The unread run is
    `gap_w - min(gap_r, threshold)`.
    """
    threshold = policy.max_unread_characters
    blocks = _consistent_blocks(_matching_blocks(witness, reading, policy, deadline), threshold)
    longest = 0
    cursor_a = cursor_b = 0
    for i, j, size in [*blocks, (len(witness), len(reading), 0)]:
        gap_w, gap_r = i - cursor_a, j - cursor_b
        longest = max(longest, gap_w - min(gap_r, threshold))
        cursor_a, cursor_b = i + size, j + size
    return longest


def unread_characters(
    witness: str,
    reading: str,
    policy: PageAccountingPolicy,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """`unread_run` over raw texts under a fresh deadline; `None` when not measured."""
    deadline = _Deadline(clock, clock(), policy.deadline_milliseconds / 1000)
    try:
        return unread_run(normalized_text(witness), normalized_text(reading), policy, deadline)
    except _NotMeasured:
        return None


# --- the accounting --------------------------------------------------------------------


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


def _id_key(identifier: str) -> tuple[str, int]:
    return identifier[0], int(identifier[1:])


def page_accounting(
    *,
    feed: Mapping[str, Any],
    reading: Mapping[str, Any],
    entry_truncation: Mapping[int, str],
    ink: Mapping[str, Any] | None,
    policy: PageAccountingPolicy,
    feed_ref: Any,
    page_reading_ref: Any,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The `page-accounting.v1` payload for one page reading.

    - `feed`: the `page-feed` payload (`page_id`, `page_ordinal`, `witnesses[]`
      with `letter` and `units[]` of `{id, box_px | None, text,
      detector_record?}`, `surya` with `lines[]` and `blocks[]` of `{id,
      box_px}`). `detector_record: true` marks a unit that is one of the record
      detector's records (DAI's units); it must carry that record's box.
    - `reading`: the `page-reading` payload's `parse_state`, `finish_reason` and
      `answer` (the parsed object as given, or `None`).
    - `entry_truncation`: `{n: "complete" | "truncated" | "unknown"}`, each
      entry's truncation classification; an entry missing here is incomplete.
    - `ink`: `{"runs": <ink-runs.v2 evidence> | None, "coverage_policy":
      <CoverageAuditPolicy resolved for this page>}`, or `None`. Without runs
      rule (f) is not measured, which holds.
    - `clock`: the deadline's clock; a parameter so a test can expire it.

    The verdict does not depend on the order of any input list.
    """
    candidates = feed_candidates(feed)
    parse_state = reading["parse_state"]
    finish_reason = reading["finish_reason"]
    if parse_state not in PARSE_STATES:
        raise ContractError(f"page reading parse_state {parse_state!r} is not a known state")
    units = sorted(
        (unit for witness in feed["witnesses"] for unit in witness["units"]),
        key=lambda unit: _id_key(unit["id"]),
    )
    surya = feed.get("surya") or {}
    lines = sorted(surya.get("lines", []), key=lambda line: _id_key(line["id"]))

    rules: dict[str, dict[str, Any]] = {}
    answered = parse_state == PARSED and reading.get("answer") is not None
    validated = (
        validate_answer(reading["answer"], candidates)
        if answered
        else {"entries": [], "set_aside": {}, "problems": []}
    )
    entries = sorted(validated["entries"], key=lambda entry: entry["n"])
    set_aside = validated["set_aside"]
    problems = validated["problems"]

    # (a) the answer is complete: finished on `stop`, parsed and valid.
    incomplete = []
    if finish_reason != "stop":
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
        classification = entry_truncation.get(entry["n"], "not-classified")
        if classification != TRUNCATION_COMPLETE:
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
            "disposition": "cited"
            if unit["id"] in cited_by
            else "set-aside"
            if unit["id"] in set_aside
            else "unaccounted",
            "by": sorted(cited_by.get(unit["id"], [])),
        }
        for unit in units
    ]
    regions = {entry["n"]: entry["union_box_px"] for entry in entries if entry["union_box_px"]}
    line_rows = [
        {
            "id": line["id"],
            "inside": sorted(
                n for n, region in regions.items() if is_inside(line["box_px"], [region], policy)
            ),
        }
        for line in lines
    ]

    if not answered:
        no_answer = [{"code": NO_PARSED_ANSWER}]
        for rule in ("b", "c", "d", "e", "f", "h", "i"):
            rules[rule] = _rule(list(no_answer))
        rules["g"] = _rule(incomplete_reading)
        return _record(feed, rules, unit_rows, line_rows, policy, feed_ref, page_reading_ref)

    # (b) every cited id exists, and every entry cites a boxed id.
    rules["b"] = _rule(
        [p for p in problems if p["code"] == UNKNOWN_ID]
        + [
            {"code": READING_UNPLACED, "n": entry["n"]}
            for entry in entries
            if entry["union_box_px"] is None
        ]
    )

    # (c) every witness unit is cited or set aside with a reason.
    rules["c"] = _rule(
        [
            {"code": UNACCOUNTED_WITNESS_UNIT, "id": row["id"]}
            for row in unit_rows
            if row["disposition"] == "unaccounted"
        ]
    )

    # (d) every Surya line lies inside the reading regions or is set aside.
    all_regions = list(regions.values())
    rules["d"] = _rule(
        [
            {"code": UNREAD_LINE, "id": line["id"]}
            for line in lines
            if line["id"] not in set_aside and not is_inside(line["box_px"], all_regions, policy)
        ]
    )

    rules["e"] = _witness_text_rule(units, entries, set_aside, cited_by, policy, clock)
    rules["f"] = _ink_rule(ink, all_regions, set_aside, candidates)
    rules["g"] = _rule(incomplete_reading)

    # (h) two entries on one region hold; a line inside two regions is recorded.
    rules["h"] = _rule(
        [p for p in problems if p["code"] == DUPLICATE_REGION]
        + [
            {"code": SHARED_LINE, "id": row["id"], "inside": row["inside"]}
            for row in line_rows
            if len(row["inside"]) > 1
        ]
    )
    rules["i"] = _detection_rule(units, entries, policy)
    return _record(feed, rules, unit_rows, line_rows, policy, feed_ref, page_reading_ref)


def _witness_text_rule(
    units: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    set_aside: Mapping[str, str],
    cited_by: Mapping[str, list[int]],
    policy: PageAccountingPolicy,
    clock: Callable[[], float],
) -> dict[str, Any]:
    """(e) Every witness unit's own text appears in the readings that cite it.

    A unit is compared with the readings of the entries citing it, joined in
    `n` order; an uncited unit with every reading. Scoping to the citing
    entries is what catches a unit that merged two records while only one was
    read: the other record's formula would otherwise find itself in a
    neighbouring entry's reading.
    """
    deadline = _Deadline(clock, clock(), policy.deadline_milliseconds / 1000)
    readings = {entry["n"]: normalized_text(entry["text"]) for entry in entries}
    findings: list[dict[str, Any]] = []
    for unit in units:
        if unit["id"] in set_aside:
            continue
        witness = normalized_text(unit["text"])
        if not witness:
            continue
        scope = sorted(cited_by.get(unit["id"], readings))
        try:
            deadline.check()
            longest = unread_run(witness, "".join(readings[n] for n in scope), policy, deadline)
        except _NotMeasured as error:
            findings.append(
                {"code": WITNESS_TEXT_NOT_MEASURED, "id": unit["id"], "reason": error.reason}
            )
            continue
        if longest > policy.max_unread_characters:
            findings.append(
                {
                    "code": WITNESS_TEXT_NOT_READ,
                    "id": unit["id"],
                    "unread_characters": longest,
                    "unit_characters": len(witness),
                    "compared_with": scope,
                }
            )
    return _rule(findings)


def _detection_rule(
    units: list[dict[str, Any]], entries: list[dict[str, Any]], policy: PageAccountingPolicy
) -> dict[str, Any]:
    """(i) Each detector record lies inside exactly one `act` region.

    The detector's records are independent evidence of where one entry ends.
    Two of them inside one act region is the Perlector reading two entries as
    one act, citing and transcribing both, which no text or coverage rule can
    see: `merged-detection`, held. A record inside no act region is
    `undetected-read`, recorded (rules c-e account for its unit), and one
    inside two act regions is `split-detection`, recorded: a detector record
    that merged two entries the Perlector read apart.
    """
    acts = {
        entry["n"]: entry["union_box_px"]
        for entry in entries
        if entry["kind"] == "act" and entry["union_box_px"] is not None
    }
    inside_act: dict[int, list[str]] = {}
    findings: list[dict[str, Any]] = []
    for unit in units:
        if not unit.get("detector_record"):
            continue
        box = unit.get("box_px")
        if box is None:
            raise ContractError(f"detector record {unit['id']} has no box")
        inside = sorted(n for n, region in acts.items() if is_inside(box, [region], policy))
        if not inside:
            findings.append({"code": UNDETECTED_READ, "id": unit["id"]})
        elif len(inside) > 1:
            findings.append({"code": SPLIT_DETECTION, "id": unit["id"], "inside": inside})
        for n in inside:
            inside_act.setdefault(n, []).append(unit["id"])
    findings.extend(
        {"code": MERGED_DETECTION, "n": n, "ids": ids}
        for n, ids in sorted(inside_act.items())
        if len(ids) > 1
    )
    return _rule(findings)


def _ink_rule(
    ink: Mapping[str, Any] | None,
    regions: list[Box],
    set_aside: Mapping[str, str],
    candidates: Mapping[str, Box | None],
) -> dict[str, Any]:
    """(f) No substantial ink lies outside every reading region and set-aside box."""
    if ink is None or ink.get("runs") is None:
        return _rule([{"code": UNREAD_INK_NOT_MEASURED}])
    coverage_policy: CoverageAuditPolicy = ink["coverage_policy"]
    covered = [_bounds(box) for box in regions] + [
        _bounds(candidates[identifier])
        for identifier in sorted(set_aside, key=_id_key)
        if candidates.get(identifier) is not None
    ]
    measured = residual_ink_from_runs(ink["runs"], covered, coverage_policy=coverage_policy)
    counts = {
        "total_ink_pixels": measured["total_ink_pixels"],
        "outside_ink_pixels": measured["outside_ink_pixels"],
        "substantial_ink_pixels": measured["substantial_ink_pixels"],
    }
    if measured["flagged"]:
        return _rule([{"code": UNREAD_INK, **counts}])
    return {"status": PASS, "findings": [], "measurement": counts}


def _record(
    feed: Mapping[str, Any],
    rules: dict[str, dict[str, Any]],
    unit_rows: list[dict[str, Any]],
    line_rows: list[dict[str, Any]],
    policy: PageAccountingPolicy,
    feed_ref: Any,
    page_reading_ref: Any,
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
        "rules": {name: rules[name] for name in RULES},
        "units": unit_rows,
        "lines": line_rows,
        "holds": holds,
        "policy_sha256": policy.sha256,
    }
