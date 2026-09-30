"""The reconstruction layer's model-free rules: what is asked, and how an answer applies.

Every act carries two readings. The diplomatic reading is exactly what the ink on
its page shows and is the established reading. The reconstruction is a labelled,
unconfirmed layer beneath it, where the Perlector may depart from the diplomatic
on text context alone: the acts around it, the formula and, for an act that runs
across pages, the other page's piece and the witnesses' text for it. It never
replaces the diplomatic.

The Perlector returns departures only (`common.reconstruction_answer`), and this
module applies them to the diplomatic raw text. A departure is
`{diplomatic, reconstruction, basis, reason}`: the exact diplomatic span it
departs from, what it reads there instead, the cites it rests on and why. A
departure that cannot be applied exactly, or rests on nothing shown, holds its
act (or every piece of a join); nothing is repaired.

## The plan

    plan = reconstruction_plan(entries, mode=..., pages_are_consecutive=...)

One call per page, `{page_ordinal, subjects, chains}`. A subject is an `act`
entry read on that page; a chain is the pieces of an act that both sides of each
page break it crosses flag (`common.page_edges`), asked once, on the page
holding its last piece, as a join. A chain's pieces are not also subjects.

## Cites

`p<ordinal>:<n>` names an entry, exactly its `act_key`. `p<ordinal>:<unit id>`
names a witness unit of that page's feed, such as `p4:A7`, and is shown only
for join pieces. Surya's lines and blocks (`L`, `S`) are detections, not text,
and are never citeable.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError
from common.page_accounting import normalized_text
from common.page_edges import act_entries_by_page, break_chains, first_attempt_entries
from common.reading_annotations import ASSESSMENT_MALFORMED, doubt_mark_offsets, read_doubt_marks
from common.sealed_config import read_sealed_toml

MODE_OFF: Final = "off"
MODE_ON: Final = "on"
MODES: Final = frozenset({MODE_OFF, MODE_ON})

DEPARTURE_FIELDS: Final = frozenset({"diplomatic", "reconstruction", "basis", "reason"})
JOIN_SEPARATOR: Final = "\n"
BASIS_POINTS: Final = 10_000

ENTRY_CITE: Final = re.compile(r"p(0|[1-9][0-9]*):([1-9][0-9]*)")
UNIT_CITE: Final = re.compile(r"p(0|[1-9][0-9]*):([A-KM-RT-Z][1-9][0-9]*)")

# Hold codes: each holds the act, or every piece of a join.
DEPARTURE_NO_BASIS: Final = "departure-no-basis"
DEPARTURE_INVALID: Final = "departure-invalid"
DEPARTURE_SPAN_NOT_FOUND: Final = "departure-span-not-found"
DEPARTURE_SPAN_AMBIGUOUS: Final = "departure-span-ambiguous"
DEPARTURE_SPLITS_DOUBT_MARK: Final = "departure-splits-doubt-mark"
BASIS_NOT_SHOWN: Final = "basis-not-shown"
BASIS_NO_READING: Final = "basis-no-reading"
RECONSTRUCTION_TOO_LARGE: Final = "reconstruction-too-large"
RECONSTRUCTION_MARKS_MALFORMED: Final = "reconstruction-marks-malformed"
JOIN_DEPARTURES_WITHOUT_CONTINUATION: Final = "join-departures-without-continuation"
HOLD_CODES: Final = frozenset(
    {
        DEPARTURE_NO_BASIS,
        DEPARTURE_INVALID,
        DEPARTURE_SPAN_NOT_FOUND,
        DEPARTURE_SPAN_AMBIGUOUS,
        DEPARTURE_SPLITS_DOUBT_MARK,
        BASIS_NOT_SHOWN,
        BASIS_NO_READING,
        RECONSTRUCTION_TOO_LARGE,
        RECONSTRUCTION_MARKS_MALFORMED,
        JOIN_DEPARTURES_WITHOUT_CONTINUATION,
    }
)
# Recorded on a departure's finding, never holding.
DEPARTURE_NO_CHANGE: Final = "departure-no-change"

DEFAULT_RECONSTRUCTION_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "reconstruction.toml"
)
_POLICY_INTEGERS: Final = (
    "max_departures_per_act",
    "max_departure_characters",
    "max_changed_share_bp",
    "changed_floor_characters",
    "max_basis_cites",
    "max_reason_characters",
)
_POLICY_FLAGS: Final = ("require_other_entry_basis",)


# --- policy ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconstructionPolicy:
    max_departures_per_act: int
    max_departure_characters: int
    max_changed_share_bp: int
    changed_floor_characters: int
    max_basis_cites: int
    max_reason_characters: int
    require_other_entry_basis: bool
    sha256: str


def load_reconstruction_policy(
    path: str | Path = DEFAULT_RECONSTRUCTION_CONFIG_PATH,
) -> ReconstructionPolicy:
    """Read the closed, sealed reconstruction configuration, with its seal."""
    record, digest = read_sealed_toml(path, "reconstruction configuration")
    expected = set(_POLICY_INTEGERS) | set(_POLICY_FLAGS)
    if set(record) != expected:
        raise ContractError(
            "the reconstruction configuration is not its closed schema: "
            f"{sorted(set(record) ^ expected)}"
        )
    for field in _POLICY_INTEGERS:
        value = record[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ContractError(f"reconstruction {field} must be a positive integer")
    if record["max_changed_share_bp"] > BASIS_POINTS:
        raise ContractError("reconstruction max_changed_share_bp must be in 1..10000")
    for field in _POLICY_FLAGS:
        if not isinstance(record[field], bool):
            raise ContractError(f"reconstruction {field} must be true or false")
    return ReconstructionPolicy(**record, sha256=digest)


# --- cites -----------------------------------------------------------------------------


def entry_cite(page_ordinal: int, n: int) -> str:
    """The cite of an entry: its `act_key`."""
    return f"p{page_ordinal}:{n}"


def unit_cite(page_ordinal: int, unit_id: str) -> str:
    """The cite of a witness unit of a page's feed; a Surya line or block is refused."""
    cite = f"p{page_ordinal}:{unit_id}"
    if UNIT_CITE.fullmatch(cite) is None:
        raise ContractError(f"{unit_id!r} is not a citeable witness unit id")
    return cite


def is_entry_cite(cite: str) -> bool:
    return ENTRY_CITE.fullmatch(cite) is not None


# --- the plan --------------------------------------------------------------------------


def reconstruction_plan(
    entries: Iterable[Mapping[str, Any]], *, mode: str, pages_are_consecutive: bool
) -> list[dict[str, Any]]:
    """The reconstruction calls one run asks, one per page with anything to ask, in page order.

    Each entry is `{act_key, page_ordinal, n, kind, continues_from_previous_page,
    continues_to_next_page, reading_attempt}`; only a page's first reading attempt
    is planned. A call is `{page_ordinal, subjects, chains}`: `subjects` the
    `act_key` of each `act` entry on the page in answer order, except chain pieces,
    and `chains` the pieces' keys of the chain whose last piece is on the page.
    Chains exist only when the pages are consecutive leaves of one register; an
    `other` entry is citeable but never a subject.
    """
    if mode not in MODES:
        raise ContractError(f"reconstruction mode {mode!r} is not one of {sorted(MODES)}")
    if not isinstance(pages_are_consecutive, bool):
        raise ContractError("pages_are_consecutive must be true or false")
    if mode == MODE_OFF:
        return []
    read = first_attempt_entries(entries)
    chains = break_chains(read) if pages_are_consecutive else []
    pieces = {piece["act_key"] for chain in chains for piece in chain}
    calls: dict[int, dict[str, Any]] = {}

    def call(ordinal: int) -> dict[str, Any]:
        return calls.setdefault(ordinal, {"page_ordinal": ordinal, "subjects": [], "chains": []})

    for ordinal, acts in act_entries_by_page(read).items():
        subjects = [act["act_key"] for act in acts if act["act_key"] not in pieces]
        if subjects:
            call(ordinal)["subjects"] = subjects
    for chain in chains:
        call(chain[-1]["page_ordinal"])["chains"].append([piece["act_key"] for piece in chain])
    return [calls[ordinal] for ordinal in sorted(calls)]


# --- applying departures ---------------------------------------------------------------


def _hold(code: str, departure: int | None, detail: str) -> dict[str, Any]:
    return {"code": code, "departure": departure, "detail": detail}


def _shape_holds(index: int, departure: Any, policy: ReconstructionPolicy) -> list[dict]:
    """Why a departure is not a well-formed `{diplomatic, reconstruction, basis, reason}`."""
    if not isinstance(departure, Mapping):
        return [_hold(DEPARTURE_INVALID, index, "the departure is not an object")]
    keys = set(departure)
    if keys == DEPARTURE_FIELDS - {"basis"} or (
        keys == DEPARTURE_FIELDS and departure["basis"] == []
    ):
        return [_hold(DEPARTURE_NO_BASIS, index, "the departure cites nothing it rests on")]
    if keys != DEPARTURE_FIELDS:
        return [
            _hold(
                DEPARTURE_INVALID,
                index,
                f"the departure's keys are {sorted(map(str, keys))}, not exactly "
                f"{sorted(DEPARTURE_FIELDS)}",
            )
        ]
    diplomatic, reconstruction = departure["diplomatic"], departure["reconstruction"]
    basis, reason = departure["basis"], departure["reason"]
    if not isinstance(diplomatic, str) or not diplomatic:
        return [_hold(DEPARTURE_INVALID, index, "diplomatic is not a non-empty string")]
    if not isinstance(reconstruction, str):
        return [_hold(DEPARTURE_INVALID, index, "reconstruction is not a string")]
    if not isinstance(basis, list) or not all(isinstance(cite, str) for cite in basis):
        return [_hold(DEPARTURE_INVALID, index, "basis is not a list of strings")]
    if len(basis) > policy.max_basis_cites:
        return [
            _hold(
                DEPARTURE_INVALID,
                index,
                f"basis names {len(basis)} cites, above {policy.max_basis_cites}",
            )
        ]
    if not isinstance(reason, str) or not reason.strip():
        return [_hold(DEPARTURE_INVALID, index, "reason is not a non-blank string")]
    if len(reason) > policy.max_reason_characters:
        return [
            _hold(
                DEPARTURE_INVALID,
                index,
                f"reason is {len(reason)} characters, above {policy.max_reason_characters}",
            )
        ]
    return []


def _basis_holds(
    index: int,
    basis: Sequence[str],
    citeable: Mapping[str, str],
    own_keys: Collection[str],
    policy: ReconstructionPolicy,
) -> list[dict]:
    unshown = [cite for cite in basis if cite not in citeable]
    holds = []
    if unshown:
        holds.append(
            _hold(BASIS_NOT_SHOWN, index, f"the basis cites {unshown}, which were not shown")
        )
    if policy.require_other_entry_basis and not any(
        cite in citeable and is_entry_cite(cite) and cite not in own_keys for cite in basis
    ):
        holds.append(_hold(BASIS_NO_READING, index, "the basis cites no other entry's reading"))
    return holds


def _splits_mark(raw_to_clean: Sequence[int | None], start: int, end: int) -> bool:
    return raw_to_clean[start] is None or raw_to_clean[end] is None


def _basis_text_found(
    reconstruction: str, basis: Sequence[str], citeable: Mapping[str, str]
) -> bool:
    """Whether the reconstruction's letters and digits occur in the text of any shown cite."""
    needle = normalized_text(reconstruction)
    return bool(needle) and any(
        needle in normalized_text(citeable[cite]) for cite in basis if cite in citeable
    )


def _apply(
    base_raw: str,
    departures: Any,
    citeable: Mapping[str, str],
    own_keys: Collection[str],
    policy: ReconstructionPolicy,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(departures, list):
        return None, [], [_hold(DEPARTURE_INVALID, None, "departures is not a list")]
    holds: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if len(departures) > policy.max_departures_per_act:
        holds.append(
            _hold(
                RECONSTRUCTION_TOO_LARGE,
                None,
                f"{len(departures)} departures, above {policy.max_departures_per_act}",
            )
        )
    raw_to_clean, _clean_to_raw = doubt_mark_offsets(base_raw)
    pieces: list[str] = []
    cursor = changed = 0
    for index, departure in enumerate(departures):
        shape = _shape_holds(index, departure, policy)
        if shape:
            holds.extend(shape)
            continue
        diplomatic, reconstruction = departure["diplomatic"], departure["reconstruction"]
        basis = departure["basis"]
        holds.extend(_basis_holds(index, basis, citeable, own_keys, policy))
        longest = max(len(diplomatic), len(reconstruction))
        if longest > policy.max_departure_characters:
            holds.append(
                _hold(
                    RECONSTRUCTION_TOO_LARGE,
                    index,
                    f"a side is {longest} characters, above {policy.max_departure_characters}",
                )
            )
        start = base_raw.find(diplomatic, cursor)
        if start < 0:
            holds.append(
                _hold(
                    DEPARTURE_SPAN_NOT_FOUND,
                    index,
                    f"{diplomatic!r} is not in the diplomatic text at or after offset {cursor}",
                )
            )
            continue
        if base_raw.find(diplomatic, start + 1) >= 0:
            holds.append(
                _hold(
                    DEPARTURE_SPAN_AMBIGUOUS,
                    index,
                    f"{diplomatic!r} occurs more than once at or after offset {cursor}",
                )
            )
            continue
        end = start + len(diplomatic)
        if _splits_mark(raw_to_clean, start, end):
            holds.append(
                _hold(
                    DEPARTURE_SPLITS_DOUBT_MARK,
                    index,
                    f"the span {start}..{end} starts or ends inside a doubt mark",
                )
            )
            continue
        no_change = diplomatic == reconstruction
        findings.append(
            {
                "departure": index,
                "diplomatic": diplomatic,
                "reconstruction": reconstruction,
                "basis": list(basis),
                "reason": departure["reason"],
                "raw_span": {"start": start, "end": end},
                "clean_span": {"start": raw_to_clean[start], "end": raw_to_clean[end]},
                "notes": [DEPARTURE_NO_CHANGE] if no_change else [],
                "basis_text_found": _basis_text_found(reconstruction, basis, citeable),
            }
        )
        if not no_change:
            changed += longest
        pieces.append(base_raw[cursor:start])
        pieces.append(reconstruction)
        cursor = end
    pieces.append(base_raw[cursor:])
    allowed = max(
        policy.changed_floor_characters, len(base_raw) * policy.max_changed_share_bp // BASIS_POINTS
    )
    if changed > allowed:
        holds.append(
            _hold(
                RECONSTRUCTION_TOO_LARGE,
                None,
                f"departures replace {changed} characters, above {allowed}",
            )
        )
    text = "".join(pieces)
    if read_doubt_marks(text)[1]["state"] == ASSESSMENT_MALFORMED:
        holds.append(
            _hold(
                RECONSTRUCTION_MARKS_MALFORMED,
                None,
                "the reconstruction's doubt marks do not parse",
            )
        )
    return (None if holds else text), findings, holds


def apply_departures(
    base_raw: str,
    departures: Any,
    citeable: Mapping[str, str],
    subject_key: str,
    policy: ReconstructionPolicy,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """`(reconstruction raw text | None, findings, holds)` for one act's departures.

    `base_raw` is the act's diplomatic raw text, doubt marks included; `citeable`
    maps each cite shown for the act to its text. Departures apply in order, each
    found at or after the end of the one before, so no two overlap. A finding
    records each applied departure with its span in raw offsets and in offsets of
    the clean text (`read_doubt_marks`), which is exact because no applied span
    splits a mark: `[[?]]` maps to a zero-width span. Any hold means no
    reconstruction (`None`); with no departures the reconstruction is the
    diplomatic.

    `departures` has passed the answer grammar only as a list of anything, so
    each departure's shape is checked here and a bad one holds its act alone.
    """
    return _apply(base_raw, departures, citeable, {subject_key}, policy)


def apply_join(
    pieces_raw: Sequence[str],
    join: Mapping[str, Any],
    citeable: Mapping[str, str],
    policy: ReconstructionPolicy,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """`(joined reconstruction | None, findings, holds)` for one chain's join answer.

    The base is the pieces' diplomatic raw texts joined by one newline, and the
    departures apply to it as to an act's; every piece is the join's own, so a
    basis must cite some entry outside the chain. A join the Perlector says does
    not continue has no reconstruction, and holds every piece if it still
    carries departures.
    """
    departures = join["departures"]
    if join["continues"] is not True:
        if departures:
            return (
                None,
                [],
                [
                    _hold(
                        JOIN_DEPARTURES_WITHOUT_CONTINUATION,
                        None,
                        "the join carries departures but says the act does not continue",
                    )
                ],
            )
        return None, [], []
    return _apply(JOIN_SEPARATOR.join(pieces_raw), departures, citeable, set(join["acts"]), policy)
