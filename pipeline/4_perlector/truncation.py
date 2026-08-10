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

from typing import Final, TypedDict

from common.contracts.errors import ContractError

COMPLETE: Final = "complete"
TRUNCATED: Final = "truncated"
UNKNOWN: Final = "unknown"

CLASSIFICATIONS: Final = frozenset({COMPLETE, TRUNCATED, UNKNOWN})

# A mark left open at the end of a reading is the shape an engine cut off
# mid-emission produces: an opened quotation or parenthetical the model never
# closed. Genuinely unbalanced ink is rare in a parish register and is not this
# module's business to adjudicate -- it only says the reading looks cut off.
_STRUCTURE_PAIRS: Final = (("(", ")"), ("[", "]"), ("“", "”"))

# Pixels-per-character floor: a region this large that produced a reading this
# short is suspicious, not proof. A heuristic bound, not a calibrated constant --
# alpha testing over real ink is what would tune it, and this module says so
# rather than presenting the number as settled. Set high enough that this
# repository's tiny synthetic fixture pages (tens of thousands of pixels, tens
# of characters) never trip it by accident of scale; a real photographed page
# is orders of magnitude larger per character and this is where alpha testing
# would actually tune the number.
MIN_PIXELS_PER_CHARACTER: Final = 2000


class TruncationSignals(TypedDict):
    stop_reason_declared: str | None
    unclosed_structure: bool
    length_suspicious: bool
    ends_abruptly: bool


class TruncationRecord(TypedDict):
    classification: str
    signals: TruncationSignals


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
    """True when an opening mark in `text` has no matching close."""
    return any(text.count(opener) != text.count(closer) for opener, closer in _STRUCTURE_PAIRS)


def is_length_suspicious(text: str, region_pixels: int) -> bool:
    """True when a region this large produced a reading this short.

    An empty reading is not this check's business -- `no-readable-text` is
    the honest outcome for that, decided elsewhere, never smuggled in here as
    a truncation.
    """
    if region_pixels <= 0:
        raise ValueError("region_pixels must be positive to judge a reading against it")
    if not text:
        return False
    return region_pixels / len(text) > MIN_PIXELS_PER_CHARACTER


def ends_abruptly(text: str) -> bool:
    """True when `text` looks cut off mid-token.

    The same rubric `pipeline/3_attestatores/run.py::content_health` already
    uses for a witness's own report ("a report ending mid-token is the shape of
    a truncation") extended here to the Perlector's own reading, so the two
    stages judge an abrupt ending the same way. Deliberately not "does the
    reading end in terminal punctuation": a genuine parish-register act
    routinely ends on a name or a signature, not a period, and requiring
    punctuation would misclassify most honest complete readings in this
    project's own domain as abrupt.
    """
    stripped = text.rstrip()
    return bool(stripped) and stripped.endswith("-")


def classify(text: str, *, region_pixels: int, stop_reason: str | None = None) -> TruncationRecord:
    """Classify one reading attempt `complete | truncated | unknown`.

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
    signals: TruncationSignals = {
        "stop_reason_declared": stop_reason,
        "unclosed_structure": has_unclosed_structure(text),
        "length_suspicious": is_length_suspicious(text, region_pixels),
        "ends_abruptly": ends_abruptly(text),
    }

    declared = _stop_reason_signal(stop_reason)
    if declared == TRUNCATED:
        return {"classification": TRUNCATED, "signals": signals}

    suspicious_votes = sum(
        (signals["unclosed_structure"], signals["length_suspicious"], signals["ends_abruptly"])
    )
    if suspicious_votes == 3:
        classification = TRUNCATED
    elif suspicious_votes == 0 and declared == COMPLETE:
        classification = COMPLETE
    else:
        classification = UNKNOWN
    return {"classification": classification, "signals": signals}


def holds_as_failure(classification: str) -> bool:
    """`truncated` and `unknown` both hold; only `complete` may proceed."""
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"{classification!r} is not a declared truncation classification")
    return classification != COMPLETE
