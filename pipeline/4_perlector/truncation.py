"""Truncation is detected by an instrument, not assumed.

ARCHITECTURE: the Perlector "reads through to the end"; truncation is a failure,
not an output. Spec 08 requires the signals to be declared, each response
classified `complete | truncated | unknown`, and `unknown` held -- never passed
as complete. Nothing here decides between witnesses; every signal is computed
over the candidate reading and the region it came from, never over a witness's
testimony.

Four declared signals. Three are genuinely computed, over the actual reading
text and the actual region area. The fourth -- the serving engine's own
stop-reason -- needs a real engine to observe honestly, which this chamber does
not have; it is reported by the reader implementation
(`pipeline/4_perlector/reader.py`), which is where a real engine's own answer
will arrive, and named here as a stand-in rather than disguised as a computed
one.

**A reading whose engine reported nothing is never `complete`.** The three
computed signals can only ever say a reading does not *look* cut off, and
"does not look cut off" is not "ran to its own end" -- an engine cut off at a
sentence boundary produces clean-looking text. So `complete` requires a
positive engine observation as well as three clean computed signals; with no
engine observation at all the classification is `unknown`, which holds.

That case is real rather than theoretical, and the old pipeline is where it
was learned: its Chandra serving adapter discarded the OpenAI `finish_reason`
entirely and preserved only `usage.completion_tokens`, so the old code had to
derive truncation from `completion_tokens >= attempt_cap` instead
(`remote/run_batch.py`, read through the window; no line carried). A serving
path here whose adapter drops the stop-reason therefore holds every reading
until it declares a second engine signal of its own -- which is the correct
outcome, and the reason the rule is written as "an engine observation" rather
than "a stop-reason".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, TypedDict

from common.contracts.errors import ContractError
from common.perlector_audit import (
    TRUNCATION_COMPLETE,
    TRUNCATION_TRUNCATED,
    TRUNCATION_UNKNOWN,
    length_signal,
    truncation_classification,
)

# The one vocabulary, owned by the shared surface every consumer re-derives the
# sealed verdict with; these names stay for this stage's own readers.
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
# dimensionless. `config/perlector_protocol.toml`'s `[truncation]` names it:
# a reading is length-suspicious when, scaled from its region to the whole
# page's area, it would carry fewer than the floor's characters. Until
# 2026-09-14 this was `MIN_PIXELS_PER_CHARACTER = 2000`, an absolute ratio set
# to clear this repository's 200x260 fixture pages -- and an absolute ratio
# scales the wrong way: a real 300-DPI act crop has far MORE pixels per
# character than a fixture crop, so the value that cleared the fixture held
# every ordinary act as truncated (pre-launch review, F082). The sealed value
# reaches this module as `truncation_policy`, read once per pass by
# `pipeline/4_perlector/run.py` and proven against the `perlector-protocol`
# seal, so which floor judged a reading is in the run's config_digest (F088).
LENGTH_FLOOR_FIELD: Final = "length_floor_characters_per_page"


class TruncationSignals(TypedDict):
    stop_reason_declared: str | None
    unclosed_structure: bool
    length_suspicious: bool
    ends_abruptly: bool


class TruncationMeasure(TypedDict):
    """What the length signal was judged from, recorded so it can be re-judged.

    The three text signals were the producer's word until 2026-09-14 because
    `region_pixels` was not on the record. It is now, with the page area it was
    read against, the character count, and the floor those three were judged
    under -- every term of the predicate, so a consumer holding nothing but
    this block recomputes `length_suspicious` rather than trusting it. The
    floor travels on the record and not only in the run's config_digest for
    the reason the Armarium's re-measurement row carries its own noise floor
    (`pipeline/7_armarium/run.py::ink_map_page_rows`): configuration protects
    reproducibility going forward, the record itself protects the past
    (GOVERNANCE 6), and a reader who has the record but not that run's
    `config/perlector_protocol.toml` could otherwise only take the signal on
    trust.
    """

    region_pixels: int
    page_pixels: int
    characters: int
    length_floor_characters_per_page: int


class TruncationRecord(TypedDict):
    classification: str
    signals: TruncationSignals
    measure: TruncationMeasure


def _stop_reason_signal(stop_reason: str | None) -> str | None:
    """The one declared, fixture-only signal. `None` means nothing was declared.

    This fixture chamber only ever declares `"stop"` or `"length"`, but the
    reader protocol this stands in for (`pipeline/4_perlector/reader.py`) is the
    seam a real serving engine occupies later, and a real engine's own
    finish-reason string is untrusted input this module has not seen before --
    refused by name rather than let through as an unhandled crash, exactly as
    every other boundary in this stage refuses rather than guesses.
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
    fixture scale and at 300 DPI gets the same verdict. A continuation act's
    `region_pixels` and `page_pixels` are each summed over the pages it spans,
    which keeps the ratio the same one.

    The arithmetic itself is `common/perlector_audit.py::length_signal`, the one
    spelling `validate_truncation_record` re-derives the recorded signal with;
    what this function adds is the bounds a producer owes. An empty reading is
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
) -> TruncationRecord:
    """Classify one reading attempt `complete | truncated | unknown`.

    `truncation_policy` is the sealed `[truncation]` table, keyword-only with
    no default: a caller that forgets it fails loudly rather than judging under
    a floor nobody sealed, the shape `coverage_flag`'s gates already take.

    The engine's declared stop-reason is authoritative when it says `length`:
    an engine that reports it ran out of budget is not something the other
    three signals get to overrule. Otherwise the three computed signals vote,
    and `complete` needs both an engine that said it stopped of its own accord
    and a unanimous clean vote:

      engine `length`                          -> truncated
      engine `stop`, three clean signals       -> complete
      three suspicious signals                 -> truncated
      anything else, including no engine word  -> unknown, which holds

    A split vote is `unknown` for the reason the module docstring gives, and so
    is silence from the engine: neither is resolved toward `complete`, because
    an ambiguous signal is exactly what "unknown holds" means.
    """
    # Absence is refused by name exactly as a wrong type is. The sealed path
    # cannot reach it -- `protocol.validate_truncation_table` guarantees the
    # key -- but a hand-built policy is what the tests and any later caller
    # pass, and a bare `KeyError` is the one boundary in this module that would
    # escape unnamed (independent audit of 2026-09-14).
    if LENGTH_FLOOR_FIELD not in truncation_policy:
        raise ContractError(f"the truncation policy declares no {LENGTH_FLOOR_FIELD}")
    floor = truncation_policy[LENGTH_FLOOR_FIELD]
    # Non-positive is refused here and not left to `is_length_suspicious`: that
    # function raises `ValueError` for a floor of zero, and a `ValueError` is
    # not one of the named contract refusals this stage's boundary classifies,
    # so a hand-built policy carrying zero escaped as an unclassified exception
    # where a wrongly-typed one was named (CodeRabbit on PR #117). The bound is
    # the same one `protocol.validate_truncation_table` applies to the sealed
    # file: a floor of zero never fires and is the signal switched off by a
    # value rather than by a decision.
    if not isinstance(floor, int) or isinstance(floor, bool) or floor <= 0:
        raise ContractError(
            f"the truncation policy's {LENGTH_FLOOR_FIELD} is not a positive integer"
        )
    signals: TruncationSignals = {
        "stop_reason_declared": stop_reason,
        "unclosed_structure": has_unclosed_structure(text),
        "length_suspicious": is_length_suspicious(
            text, region_pixels, page_pixels=page_pixels, length_floor_characters_per_page=floor
        ),
        "ends_abruptly": ends_abruptly(text),
    }
    measure: TruncationMeasure = {
        "region_pixels": region_pixels,
        "page_pixels": page_pixels,
        "characters": len(text),
        "length_floor_characters_per_page": floor,
    }

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
