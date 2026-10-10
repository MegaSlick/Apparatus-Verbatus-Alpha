"""Truncation is detected by an instrument, not assumed.

The Perlector reads through to the end, so a truncated reading is a failure,
not an output. Every signal is declared, each response is classified
`complete | truncated | unknown`, and `unknown` is held -- never passed as
complete. Nothing here decides between witnesses; every signal is computed
over the candidate reading and the region it came from, never over a witness's
testimony.

Four declared signals. Three are genuinely computed, over the actual reading
text and the actual region area. The fourth -- the serving engine's own
stop-reason -- is observed, not computed: the reader
(`operations/serving/chat_request.py::send_page_request`) passes on the engine's own
answer, and a fixture run's declared stand-in is named as one rather than disguised as a
computed signal.

**A reading whose engine reported nothing is never `complete`.** The three
computed signals can only ever say a reading does not *look* cut off, and
"does not look cut off" is not "ran to its own end" -- an engine cut off at a
sentence boundary produces clean-looking text. So `complete` requires a
positive engine observation as well as three clean computed signals; with no
engine observation, `truncated` can still fire on three suspicious computed
signals, and anything less than that is `unknown`, which holds.

That case is real rather than theoretical: a serving adapter can drop the
engine's stop-reason and expose only a token count, with no way to derive a
positive engine observation from that alone. A serving path here whose
adapter drops the stop-reason therefore cannot produce `complete`, but it can
still produce `truncated` when every judged computed signal is suspicious;
otherwise the result is `unknown` -- the reason the rule is written as "an
engine observation" rather than "a stop-reason".

The length signal is judged only where the page is at least the sealed legible
size; where it is not, it is recorded as not judged and votes
neither way, so the verdict comes from the other two signals and the record
says length was not consulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, TypedDict

from common.contracts.errors import ContractError
from common.perlector_audit import (
    TRUNCATION_COMPLETE,
    TRUNCATION_TRUNCATED,
    TRUNCATION_UNKNOWN,
    length_judged,
    length_signal,
    truncation_classification,
)

# The one vocabulary, from the shared surface every consumer re-derives the
# sealed verdict with, under the names this module's readers use.
COMPLETE: Final = TRUNCATION_COMPLETE
TRUNCATED: Final = TRUNCATION_TRUNCATED
UNKNOWN: Final = TRUNCATION_UNKNOWN

CLASSIFICATIONS: Final = frozenset({COMPLETE, TRUNCATED, UNKNOWN})

# A mark left open at the end of a reading is the shape an engine cut off
# mid-emission produces: an opened quotation or parenthetical the model never
# closed. Genuinely unbalanced ink is rare in a parish register and is not this
# module's business to adjudicate -- it only says the reading looks cut off.
_STRUCTURE_PAIRS: Final = (("(", ")"), ("[", "]"), ("“", "”"))

# The length signal's floor is sealed, not a module constant, and it is
# dimensionless rather than an absolute pixels-per-character ratio: an
# absolute ratio scales the wrong way, since a real 300-DPI page has far
# more pixels per character than a fixture page, so a ratio tuned to the
# fixture would hold every ordinary reading as truncated.
# `config/perlector_protocol.toml`'s `[truncation]` names the floor: a reading
# is length-suspicious when, scaled from its region to the whole page's area,
# it would carry fewer than the floor's characters. It reaches this module as
# `truncation_policy`, read once per pass and proven against the
# `perlector-protocol` seal, so which floor judged a reading is in the run's
# config_digest.
LENGTH_FLOOR_FIELD: Final = "length_floor_characters_per_page"
# The sealed size below which a page cannot hold the lines the floor's density
# describes, so the length signal is not judged on it.
LEGIBLE_PAGE_FIELD: Final = "legible_page_pixels"


class TruncationSignals(TypedDict):
    stop_reason_declared: str | None
    unclosed_structure: bool
    # `None` when the length was not judged: neutral, neither clean nor suspicious.
    length_suspicious: bool | None
    ends_abruptly: bool


class TruncationMeasure(TypedDict):
    """What the length signal was judged from, recorded so it can be re-judged.

    Carries every term of the predicate -- region pixels, page pixels (the size
    the legibility gate is judged on), character count, floor, legibility gate --
    so a consumer holding nothing but this block recomputes `length_suspicious`
    rather than trusting it. The floor travels
    on the record and not only in the run's config_digest because
    configuration protects reproducibility going forward while the record
    protects the past: a reader with the record but not that
    run's `config/perlector_protocol.toml` could otherwise only take the
    signal on trust.
    """

    region_pixels: int
    page_pixels: int
    characters: int
    length_floor_characters_per_page: int
    legible_page_pixels: int
    length_judged: bool


class TruncationRecord(TypedDict):
    classification: str
    signals: TruncationSignals
    measure: TruncationMeasure


def _stop_reason_signal(stop_reason: str | None) -> str | None:
    """The one declared signal. `None` means nothing was declared.

    This module only ever sees `"stop"` or `"length"`: the fixture reader
    declares them directly, and `operations/serving/chat_request.py::mapped_stop_reason` maps a
    real engine's own finish-reason word into the same two before it reaches
    here, refusing anything it does not recognize rather than letting an
    unmapped word through.
    """
    if stop_reason is None:
        return None
    if stop_reason == "length":
        return TRUNCATED
    if stop_reason == "stop":
        return COMPLETE
    raise ContractError(f"stop_reason {stop_reason!r} is neither 'stop' nor 'length'")


def has_unclosed_structure(text: str) -> bool:
    """True when an opening and closing mark are unbalanced in `text`.

    A count comparison, not an opener-without-close scan, so a surplus closer
    ("a reading)") is flagged too. That is the right shape for a truncation
    signal: either imbalance says the reading did not come out whole.
    """
    return any(text.count(opener) != text.count(closer) for opener, closer in _STRUCTURE_PAIRS)


def is_length_suspicious(
    text: str, region_pixels: int, *, page_pixels: int, length_floor_characters_per_page: int
) -> bool:
    """True when a region this large produced a reading this short.

    Scale-invariant: the reading's characters are scaled from its region to
    the whole page's area and compared with the sealed floor, in integers --
    `characters * page_pixels < floor * region_pixels` -- so the same crop at
    fixture scale and at 300 DPI gets the same verdict.

    The arithmetic itself is `common/perlector_audit.py::length_signal`, the one
    spelling of the signal, which a reader of the recorded measure can apply
    again; what this function adds is the bounds a producer owes. An empty reading is
    not this check's business -- `no-readable-text` is the honest outcome for
    that, decided elsewhere, never smuggled in here as a truncation.
    """
    if region_pixels <= 0:
        raise ValueError("region_pixels must be positive to judge a reading against it")
    if page_pixels <= 0:
        raise ValueError("page_pixels must be positive to judge a reading against it")
    if length_floor_characters_per_page <= 0:
        raise ValueError("length_floor_characters_per_page must be positive; zero never fires")
    return length_signal(
        characters=len(text),
        region_pixels=region_pixels,
        page_pixels=page_pixels,
        floor=length_floor_characters_per_page,
    )


def ends_abruptly(text: str) -> bool:
    """True when `text` looks cut off mid-token.

    Trailing whitespace is stripped before the hyphen is read, so `"word-   "`
    is abrupt: whitespace must not hide a truncation.

    Deliberately not "does the reading end in terminal punctuation": a genuine
    parish-register act routinely ends on a name or a signature, not a period,
    and requiring punctuation would misclassify most honest complete readings in
    this project's own domain as abrupt.
    """
    stripped = text.rstrip()
    return bool(stripped) and stripped.endswith("-")


def classify(
    text: str,
    *,
    region_pixels: int,
    page_pixels: int,
    truncation_policy: Mapping[str, object],
    stop_reason: str | None = None,
    length_exempt_kind: str | None = None,
) -> TruncationRecord:
    """Classify one reading attempt `complete | truncated | unknown`.

    The legibility gate is judged on `page_pixels`, the page the reading is
    of. `truncation_policy` is the sealed `[truncation]` table as the protocol
    loader validated it, keyword-only with no default: a caller that forgets it
    fails loudly rather than judging under a floor nobody sealed.

    The engine's declared stop-reason is authoritative when it says `length`:
    an engine that reports it ran out of budget is not something the other
    three signals get to overrule. Otherwise the three computed signals vote,
    and `complete` needs both an engine that said it stopped of its own accord
    and a unanimous clean vote:

      engine `length`                          -> truncated
      engine `stop`, three clean signals       -> complete
      every judged computed signal suspicious  -> truncated
      anything else, including no engine word  -> unknown, which holds

    A split vote is `unknown` for the reason the module docstring gives, and so
    is silence from the engine: neither is resolved toward `complete`, because
    an ambiguous signal is exactly what "unknown holds" means.

    `length_exempt_kind` names the entry kind of a reading the length signal was
    not calibrated for (`common.page_types.length_signal_applies`: it was
    measured on register acts, and an index row in a row-sized box is not short
    for its region). The signal is then not judged, as on a page below the
    legible size, and the measure records the kind as `length_exempt_kind`; the
    record carries no such field otherwise.
    """
    floor = truncation_policy[LENGTH_FLOOR_FIELD]
    legible = truncation_policy[LEGIBLE_PAGE_FIELD]
    judged = length_exempt_kind is None and length_judged(
        page_pixels=page_pixels, legible_page_pixels=legible
    )
    signals: TruncationSignals = {
        "stop_reason_declared": stop_reason,
        "unclosed_structure": has_unclosed_structure(text),
        "length_suspicious": is_length_suspicious(
            text, region_pixels, page_pixels=page_pixels, length_floor_characters_per_page=floor
        )
        if judged
        else None,
        "ends_abruptly": ends_abruptly(text),
    }
    measure: TruncationMeasure = {
        "region_pixels": region_pixels,
        "page_pixels": page_pixels,
        "characters": len(text),
        "length_floor_characters_per_page": floor,
        "legible_page_pixels": legible,
        "length_judged": judged,
    }
    if length_exempt_kind is not None:
        measure["length_exempt_kind"] = length_exempt_kind  # type: ignore[typeddict-unknown-key]

    # Refuses an unrecognised engine word by name before any decision is made;
    # the decision itself is the shared rule every consumer re-derives the
    # sealed verdict with (`common/perlector_audit.py::truncation_classification`),
    # so producer and validators cannot drift into two spellings of it.
    _stop_reason_signal(stop_reason)  # for its refusal alone; the decision is shared
    return {
        "classification": truncation_classification(signals),
        "signals": signals,
        "measure": measure,
    }


def holds_as_failure(classification: str) -> bool:
    """`truncated` and `unknown` both hold; only `complete` may proceed."""
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"{classification!r} is not a declared truncation classification")
    return classification != COMPLETE
