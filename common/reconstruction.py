"""The Coniector's model-free rules: what is asked, and how an answer applies.

Every act carries two readings. The diplomatic reading is exactly what the ink on
its page shows and is the established reading. The reconstruction is a labelled,
unconfirmed layer beneath it, made by the Coniector from text alone after the
Perlector has read: the acts around it, the formula and, on consecutive pages, the
neighbouring pages' edge acts and the other pieces of an act that runs across a
page break. It never replaces the diplomatic.

The Coniector answers with findings and departures (`common.reconstruction_answer`),
and this module applies the departures to the diplomatic raw text. A departure is
`{diplomatic, reconstruction, reason?}`: the exact diplomatic span it departs from,
what it reads there instead and, optionally, why. A departure that cannot be
applied exactly leaves that act's (or join's) reconstruction not made, with a
not-made code; the other departures of the act are never applied alone, and the
rest of the page stands. Nothing here holds an act: a reconstruction that is not
made leaves the diplomatic delivered as it is.

## The plan

    plan = reconstruction_plan(entries, mode=..., pages_are_consecutive=...)

One call per page, `{page_ordinal, subjects, chains, context}`. A subject is an
`act` entry read on that page. Only when the pages are consecutive leaves of one
register does a call reach across a page: `context` names the previous page's last
act and the next page's first act, and a chain is the pieces of an act that both
sides of each page break it crosses flag (`common.page_edges`), asked once, on the
page holding its last piece, as a join. A chain's pieces are not also subjects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError
from common.page_accounting import normalized_text
from common.page_edges import (
    FIRST_READING,
    act_entries_by_page,
    break_chains,
    first_attempt_entries,
    page_edges,
)
from common.reading_annotations import ASSESSMENT_MALFORMED, doubt_mark_offsets, read_doubt_marks
from common.sealed_config import read_sealed_toml

MODE_OFF: Final = "off"
MODE_ON: Final = "on"
MODES: Final = frozenset({MODE_OFF, MODE_ON})

DEPARTURE_REQUIRED: Final = frozenset({"diplomatic", "reconstruction"})
DEPARTURE_OPTIONAL: Final = frozenset({"reason"})
JOIN_SEPARATOR: Final = "\n"
BASIS_POINTS: Final = 10_000

# Not-made codes: each leaves the act's, or the join's, reconstruction not made.
DEPARTURE_INVALID: Final = "departure-invalid"
DEPARTURE_SPAN_NOT_FOUND: Final = "departure-span-not-found"
DEPARTURE_SPAN_AMBIGUOUS: Final = "departure-span-ambiguous"
DEPARTURE_SPLITS_DOUBT_MARK: Final = "departure-splits-doubt-mark"
RECONSTRUCTION_TOO_LARGE: Final = "reconstruction-too-large"
RECONSTRUCTION_MARKS_MALFORMED: Final = "reconstruction-marks-malformed"
JOIN_DEPARTURES_WITHOUT_CONTINUATION: Final = "join-departures-without-continuation"
NOT_MADE_CODES: Final = frozenset(
    {
        DEPARTURE_INVALID,
        DEPARTURE_SPAN_NOT_FOUND,
        DEPARTURE_SPAN_AMBIGUOUS,
        DEPARTURE_SPLITS_DOUBT_MARK,
        RECONSTRUCTION_TOO_LARGE,
        RECONSTRUCTION_MARKS_MALFORMED,
        JOIN_DEPARTURES_WITHOUT_CONTINUATION,
    }
)
# Recorded on an applied departure, never refusing.
DEPARTURE_NO_CHANGE: Final = "departure-no-change"

DEFAULT_RECONSTRUCTION_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "reconstruction.toml"
)
_POLICY_INTEGERS: Final = (
    "max_departures_per_act",
    "max_departure_characters",
    "max_changed_share_bp",
    "changed_floor_characters",
    "max_reason_characters",
)


# --- policy ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconstructionPolicy:
    mode: str
    pages_are_consecutive: bool
    max_departures_per_act: int
    max_departure_characters: int
    max_changed_share_bp: int
    changed_floor_characters: int
    max_reason_characters: int
    sha256: str


def load_reconstruction_policy(
    path: str | Path = DEFAULT_RECONSTRUCTION_CONFIG_PATH,
) -> ReconstructionPolicy:
    """Read the closed, sealed reconstruction configuration, with its seal."""
    record, digest = read_sealed_toml(path, "reconstruction configuration")
    expected = set(_POLICY_INTEGERS) | {"mode", "pages_are_consecutive"}
    if set(record) != expected:
        raise ContractError(
            "the reconstruction configuration is not its closed schema: "
            f"{sorted(set(record) ^ expected)}"
        )
    if not isinstance(record["mode"], str) or record["mode"] not in MODES:
        raise ContractError(f"reconstruction mode must be one of {sorted(MODES)}")
    if not isinstance(record["pages_are_consecutive"], bool):
        raise ContractError("reconstruction pages_are_consecutive must be true or false")
    for field in _POLICY_INTEGERS:
        value = record[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ContractError(f"reconstruction {field} must be a positive integer")
    if record["max_changed_share_bp"] > BASIS_POINTS:
        raise ContractError("reconstruction max_changed_share_bp must be in 1..10000")
    return ReconstructionPolicy(**record, sha256=digest)


# --- the plan --------------------------------------------------------------------------


def reconstruction_plan(
    entries: Iterable[Mapping[str, Any]], *, mode: str, pages_are_consecutive: bool
) -> list[dict[str, Any]]:
    """The Coniector's calls for one run, one per page with anything to ask, in page order.

    Each entry is `{act_key, page_ordinal, n, kind, continues_from_previous_page,
    continues_to_next_page, reading_attempt}`. A call is `{page_ordinal, subjects,
    chains, context}`: `subjects` the `act_key` of each `act` entry on the page in
    answer order, except chain pieces; `chains` the pieces' keys of each chain
    whose last piece is on the page; and `context` the keys of the previous page's
    last act and the next page's first act, where those pages hold one. Chains and
    context both reach across a page, so both exist only when the pages are
    consecutive leaves of one register. An `other` entry is shown as the page's
    text but never a subject.

    An entry a page's re-ask recovered (`reading_attempt` 2) is a diplomatic
    reading like any other and may be a subject, but a page's edges are its first
    reading's (`first_attempt_entries`): the re-ask was asked about ids alone and
    may set no continuation flag, so a recovered entry is never a chain piece or
    another page's context, and one carrying a continuation flag is refused.
    """
    if mode not in MODES:
        raise ContractError(f"reconstruction mode {mode!r} is not one of {sorted(MODES)}")
    if not isinstance(pages_are_consecutive, bool):
        raise ContractError("pages_are_consecutive must be true or false")
    if mode == MODE_OFF:
        return []
    entries = list(entries)
    for entry in entries:
        if entry["reading_attempt"] != FIRST_READING and (
            entry["continues_from_previous_page"] is True or entry["continues_to_next_page"] is True
        ):
            raise ContractError(
                f"{entry['act_key']} was recovered by its page's re-ask yet carries a "
                "continuation flag; a re-ask may set none, so it is never a side of a page break"
            )
    read = first_attempt_entries(entries)
    chains = break_chains(read) if pages_are_consecutive else []
    edges = page_edges(read) if pages_are_consecutive else {}
    pieces = {piece["act_key"] for chain in chains for piece in chain}
    calls: dict[int, dict[str, Any]] = {}

    def call(ordinal: int) -> dict[str, Any]:
        return calls.setdefault(
            ordinal, {"page_ordinal": ordinal, "subjects": [], "chains": [], "context": []}
        )

    for ordinal, acts in act_entries_by_page(entries).items():
        subjects = [act["act_key"] for act in acts if act["act_key"] not in pieces]
        if subjects:
            call(ordinal)["subjects"] = subjects
    for chain in chains:
        call(chain[-1]["page_ordinal"])["chains"].append([piece["act_key"] for piece in chain])
    for ordinal, planned in calls.items():
        before, after = edges.get(ordinal - 1), edges.get(ordinal + 1)
        planned["context"] = [
            *([before[1]["act_key"]] if before else []),
            *([after[0]["act_key"]] if after else []),
        ]
    return [calls[ordinal] for ordinal in sorted(calls)]


# --- applying departures ---------------------------------------------------------------


def _not_made(code: str, departure: int | None, detail: str) -> dict[str, Any]:
    return {"code": code, "departure": departure, "detail": detail}


def _shape_problems(index: int, departure: Any, policy: ReconstructionPolicy) -> list[dict]:
    """Why a departure is not a well-formed `{diplomatic, reconstruction, reason?}`."""
    if not isinstance(departure, Mapping):
        return [_not_made(DEPARTURE_INVALID, index, "the departure is not an object")]
    keys = set(departure)
    if not DEPARTURE_REQUIRED <= keys <= DEPARTURE_REQUIRED | DEPARTURE_OPTIONAL:
        return [
            _not_made(
                DEPARTURE_INVALID,
                index,
                f"the departure's keys are {sorted(map(str, keys))}, not "
                f"{sorted(DEPARTURE_REQUIRED)} with an optional reason",
            )
        ]
    diplomatic, reconstruction = departure["diplomatic"], departure["reconstruction"]
    if not isinstance(diplomatic, str) or not diplomatic:
        return [_not_made(DEPARTURE_INVALID, index, "diplomatic is not a non-empty string")]
    if not isinstance(reconstruction, str):
        return [_not_made(DEPARTURE_INVALID, index, "reconstruction is not a string")]
    reason = departure.get("reason", "")
    if not isinstance(reason, str):
        return [_not_made(DEPARTURE_INVALID, index, "reason is not a string")]
    if len(reason) > policy.max_reason_characters:
        return [
            _not_made(
                DEPARTURE_INVALID,
                index,
                f"reason is {len(reason)} characters, above {policy.max_reason_characters}",
            )
        ]
    return []


def _splits_mark(raw_to_clean: Sequence[int | None], start: int, end: int) -> bool:
    return raw_to_clean[start] is None or raw_to_clean[end] is None


def _replacement_in_context(reconstruction: str, shown: Sequence[str]) -> bool:
    """Whether the reconstruction's letters and digits occur in any text shown in the call."""
    needle = normalized_text(reconstruction)
    return bool(needle) and any(needle in normalized_text(text) for text in shown)


def _apply(
    base_raw: str, departures: Any, shown: Sequence[str], policy: ReconstructionPolicy
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(departures, list):
        return None, [], [_not_made(DEPARTURE_INVALID, None, "departures is not a list")]
    not_made: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    if len(departures) > policy.max_departures_per_act:
        not_made.append(
            _not_made(
                RECONSTRUCTION_TOO_LARGE,
                None,
                f"{len(departures)} departures, above {policy.max_departures_per_act}",
            )
        )
    raw_to_clean, _clean_to_raw = doubt_mark_offsets(base_raw)
    pieces: list[str] = []
    cursor = changed = 0
    for index, departure in enumerate(departures):
        shape = _shape_problems(index, departure, policy)
        if shape:
            not_made.extend(shape)
            continue
        diplomatic, reconstruction = departure["diplomatic"], departure["reconstruction"]
        longest = max(len(diplomatic), len(reconstruction))
        if longest > policy.max_departure_characters:
            not_made.append(
                _not_made(
                    RECONSTRUCTION_TOO_LARGE,
                    index,
                    f"a side is {longest} characters, above {policy.max_departure_characters}",
                )
            )
        start = base_raw.find(diplomatic, cursor)
        if start < 0:
            not_made.append(
                _not_made(
                    DEPARTURE_SPAN_NOT_FOUND,
                    index,
                    f"{diplomatic!r} is not in the diplomatic text at or after offset {cursor}",
                )
            )
            continue
        if base_raw.find(diplomatic, start + 1) >= 0:
            not_made.append(
                _not_made(
                    DEPARTURE_SPAN_AMBIGUOUS,
                    index,
                    f"{diplomatic!r} occurs more than once at or after offset {cursor}",
                )
            )
            continue
        end = start + len(diplomatic)
        if _splits_mark(raw_to_clean, start, end):
            not_made.append(
                _not_made(
                    DEPARTURE_SPLITS_DOUBT_MARK,
                    index,
                    f"the span {start}..{end} starts or ends inside a doubt mark",
                )
            )
            continue
        no_change = diplomatic == reconstruction
        applied.append(
            {
                "departure": index,
                "diplomatic": diplomatic,
                "reconstruction": reconstruction,
                "reason": departure.get("reason"),
                "raw_span": {"start": start, "end": end},
                "clean_span": {"start": raw_to_clean[start], "end": raw_to_clean[end]},
                "notes": [DEPARTURE_NO_CHANGE] if no_change else [],
                "replacement_in_context": _replacement_in_context(reconstruction, shown),
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
        not_made.append(
            _not_made(
                RECONSTRUCTION_TOO_LARGE,
                None,
                f"departures replace {changed} characters, above {allowed}",
            )
        )
    text = "".join(pieces)
    if read_doubt_marks(text)[1]["state"] == ASSESSMENT_MALFORMED:
        not_made.append(
            _not_made(
                RECONSTRUCTION_MARKS_MALFORMED,
                None,
                "the reconstruction's doubt marks do not parse",
            )
        )
    if not_made:
        return None, [], not_made
    return text, applied, []


def apply_departures(
    base_raw: str, departures: Any, shown: Sequence[str], policy: ReconstructionPolicy
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """`(reconstruction raw text | None, applied departures, not made)` for one act.

    `base_raw` is the act's diplomatic raw text, doubt marks included; `shown` is
    every text the call showed, against which `replacement_in_context` is measured.
    Departures apply in order, each found at or after the end of the one before, so
    no two overlap. Each applied departure is recorded with its span in raw offsets
    and in offsets of the clean text (`read_doubt_marks`), which is exact because no
    applied span splits a mark: `[[?]]` maps to a zero-width span. With no
    departures the reconstruction is the diplomatic.

    Any not-made code means no reconstruction (`None`) and no applied departures:
    one failing departure refuses the whole act, because applying only the others
    would repair the answer. `departures` has passed the answer grammar only as a
    list of anything, so each departure's shape is checked here.
    """
    return _apply(base_raw, departures, shown, policy)


def apply_join(
    pieces_raw: Sequence[str],
    join: Mapping[str, Any],
    shown: Sequence[str],
    policy: ReconstructionPolicy,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """`(joined reconstruction | None, applied departures, not made)` for one chain's join.

    The base is the pieces' diplomatic raw texts joined by one newline, and the
    departures apply to it as to an act's. A join the Coniector says does not
    continue has no reconstruction, and is not made if it still carries departures.
    """
    departures = join["departures"]
    if join["continues"] is not True:
        if departures:
            return (
                None,
                [],
                [
                    _not_made(
                        JOIN_DEPARTURES_WITHOUT_CONTINUATION,
                        None,
                        "the join carries departures but says the act does not continue",
                    )
                ],
            )
        return None, [], []
    return _apply(JOIN_SEPARATOR.join(pieces_raw), departures, shown, policy)
