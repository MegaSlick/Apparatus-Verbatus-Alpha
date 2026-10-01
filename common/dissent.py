"""Dissent, computed against derived comparison views -- never against raw bytes
picked to make a witness look right or wrong.

Recording where the established reading departs from a witness is structural,
not evaluative, and not a quality signal on its own. The comparison runs on
loss-accounted normalizations built beside the verbatim payloads, which are
never coerced; where a comparison cannot run, it is recorded `unknown`, never
guessed.

Two things here would be a picker with extra steps: choosing which view "wins"
(there is no winning -- a view is compared to the already-fixed reading and
never fed back into it), and coercing an incomparable witness into a fake
agreement rather than naming the comparison itself unknown.

**Pinned forever: equality only, never a distance metric.** `comparison_view`
takes no per-chair parameter and no similarity threshold, and never will --
"closest match" needs a metric, and refusing metrics is what stops a future
edit turning a normalization into a fuzzy-match picker one similarity score at
a time. `departures` is not that metric and the distinction is the line an edit
will cross by accident: an opcode alignment *describes* where two strings
differ, with no number attached and nothing to threshold, where
`SequenceMatcher.ratio()` is a number. **A reviewer reading a change to this
file should refuse anything that adds a threshold, a weight, a ratio, or a
"close enough" comparison.**

Spans rather than a boolean per chair, because the instrument's whole purpose
needs them: a checkpoint that has learned to echo witnesses instead of reading
ink agrees everywhere *except* a few short spans, which one boolean cannot tell
from wholesale disagreement.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Final

from common.alignment import AlignmentStepLimit, StepCountedMatcher, markup_text_view
from common.contracts.errors import SchemaRefusal
from common.contracts.outcomes import WITNESS_READING_OUTCOMES

# `SequenceMatcher`'s alignment cost is not simply the product of the two
# lengths: a reading and a report that differ in many scattered places --
# exactly what a systematically-mistaken witness produces, the case this
# instrument exists to catch -- can cost far more than the square. This
# constant is a cheap prefilter for a witness stuck in a repetition loop until
# its token cap -- the witness stage puts no ceiling on report length, and
# Churro's own 24,000-token cap can run well over a hundred thousand
# characters. It does not bound the matcher's work on its own; the sealed
# `[dissent] max_comparison_steps` in `config/alignment.toml`, which every
# caller passes in, does that.
MAX_COMPARISON_CHARACTER_PAIRS: Final = 100_000_000

# A comparison stopped by its sealed step budget. Named so the record says the
# instrument stopped, not that the reading and the witness were found to agree.
COMPARISON_STEP_LIMIT_REASON: Final = "comparison-step-limit"


def unmeasured_comparison(max_comparison_steps: int) -> dict[str, Any]:
    """The explicit non-verdict of a comparison that would pass its step budget."""
    return {
        "measured": False,
        "reason": COMPARISON_STEP_LIMIT_REASON,
        "max_comparison_steps": max_comparison_steps,
    }


def comparison_view(text: str) -> dict[str, object]:
    """A loss-accounted normalization: Unicode-canonicalized, whitespace-collapsed
    text, and what that collapse dropped, so the normalization is honest about
    what it discarded rather than silently lossy. Case is never folded -- a case
    difference is a real disagreement about the ink, not a formatting artifact.

    NFC normalization runs first. A precomposed "e with acute" and a bare "e"
    followed by a combining acute accent render identically and are the same
    ink, but compare unequal codepoint-by-codepoint -- an OCR engine and a
    witness model are not guaranteed to emit the same normalization form for
    the same character, and parish-register French is exactly the kind of text
    this would otherwise misclassify as dissent.

    **`dropped_characters` measures the collapse alone, from the composed
    string.** NFC discards nothing -- it re-encodes a character, it does not
    remove one -- so composing four combining marks away is not four characters
    lost, and charging them to the loss account would put a wrong number on
    every diacritic-heavy act in the corpus this project exists to read.
    """
    composed = unicodedata.normalize("NFC", text)
    normalized = " ".join(composed.split())
    return {"normalized": normalized, "dropped_characters": len(composed) - len(normalized)}


def departures(
    reading: str, reported: str, max_comparison_steps: int
) -> list[dict[str, dict[str, int]]] | dict[str, Any]:
    """Every span where the established reading and one witness's report differ.

    `autojunk=False` (the `StepCountedMatcher` default) is load-bearing rather
    than stylistic: with it on, `SequenceMatcher` treats any element appearing
    in more than 1% of a sequence longer than 200 characters as junk, which on
    French prose means spaces and common letters stop counting as matches. The
    alignment would then change shape purely because the act was long, and a
    dissent record that means something different on long acts than on short
    ones is not a structural record.

    The matcher's work is counted against `max_comparison_steps`, the sealed
    dissent budget. A comparison that would pass it returns the explicit
    non-verdict `unmeasured_comparison` instead of spans: nothing about either
    text is touched, the comparison simply did not finish, and whether it
    finishes depends only on the two texts and the budget.

    An equal reading and report produce no departures at all -- the correct
    output on the easy line every witness agrees about (ARCHITECTURE: "a metric
    that rewards disagreement rewards hallucination").
    """
    try:
        opcodes = StepCountedMatcher(reading, reported, max_comparison_steps).get_opcodes()
    except AlignmentStepLimit:
        return unmeasured_comparison(max_comparison_steps)
    return _departure_spans(opcodes)


def _departure_spans(opcodes: list) -> list[dict[str, dict[str, int]]]:
    return [
        {
            "reading_span": {"start": reading_start, "end": reading_end},
            "testimonium_span": {"start": witness_start, "end": witness_end},
        }
        for tag, reading_start, reading_end, witness_start, witness_end in opcodes
        if tag != "equal"
    ]


def dissent_against(reading: str, reported: str, *, max_comparison_steps: int) -> dict:
    """Where the reading departed from one witness's comparison text.

    Computed after the reading is fixed. `compared: "unknown"` is what a
    comparison that cannot run receives: a report large enough to refuse
    outright (`MAX_COMPARISON_CHARACTER_PAIRS`), or an alignment that would pass
    `max_comparison_steps`, the sealed dissent budget, which that row records
    beside its reason. The same texts always give the same row.
    """
    pairs = len(reading) * len(reported)
    if pairs > MAX_COMPARISON_CHARACTER_PAIRS:
        return {
            "compared": "unknown",
            "reason": (
                f"a {len(reading)}-character reading against a {len(reported)}-"
                f"character report is {pairs} character pairs to align, past this "
                f"module's {MAX_COMPARISON_CHARACTER_PAIRS} bound; neither text is "
                "clipped and neither is changed, the alignment simply did not run"
            ),
        }
    spans = departures(reading, reported, max_comparison_steps)
    if not isinstance(spans, list):
        return unaligned_row(reading, reported, max_comparison_steps)
    reading_view = comparison_view(reading)
    markup_view = markup_text_view(reported)
    witness_view = comparison_view(markup_view["text"])
    return {
        "compared": True,
        "departed": witness_view["normalized"] != reading_view["normalized"],
        "departed_raw": reported != reading,
        # Spans over the raw strings, so `reading_span` indexes the Perlectio's
        # own `text`. A whitespace-only difference therefore shows departures
        # here while `departed` above stays False: two honest answers to two
        # different questions.
        "departures": spans,
        "comparison_loss": {
            # `reading_dropped_characters` charges collapsed whitespace only;
            # `witness_dropped_characters` also charges markup and entity
            # spelling removed from the report. Removal only, never
            # re-encoding: NFC composition is not a loss (see `comparison_view`).
            "reading_dropped_characters": reading_view["dropped_characters"],
            "witness_dropped_characters": witness_view["dropped_characters"]
            + markup_view["loss"]["markup_characters"]
            + markup_view["loss"]["whitespace_characters"],
        },
    }


def unaligned_row(reading: str, reported: str, max_comparison_steps: int) -> dict[str, Any]:
    """The row of a comparison the sealed dissent budget stopped before it finished."""
    return {
        "compared": "unknown",
        "reason": (
            f"a {len(reading)}-character reading against a {len(reported)}-"
            f"character report did not align within the sealed "
            f"{max_comparison_steps}-step dissent budget; neither text is clipped "
            "and neither is changed, the alignment simply did not finish"
        ),
        "max_comparison_steps": max_comparison_steps,
    }


def validate_dissent(
    rows: Any,
    *,
    text: str,
    basis_testimonia: list[dict],
    max_comparison_steps: int | None = None,
) -> None:
    """Refuse a dissent record that loses or duplicates a witness.

    Agreement is represented by one row with an empty ``departures`` list, not
    by omitting the row.  Otherwise an empty dissent list makes "all witnesses
    agreed" indistinguishable from "the instrument did not run" -- exactly the
    silent loss this record exists to prevent.

    Given `max_comparison_steps`, the run's sealed dissent budget, a row the
    budget stopped must name exactly that budget: any other is one the run
    never sealed.
    """
    if not isinstance(rows, list):
        raise SchemaRefusal("a Perlector reading carries no dissent record")
    if not all(isinstance(row, dict) for row in basis_testimonia):
        raise SchemaRefusal("a Perlector reading has a malformed Testimonium basis row")
    expected = [row.get("chair") for row in basis_testimonia]
    if any(not isinstance(chair, str) or not chair for chair in expected):
        raise SchemaRefusal("a Perlector reading has a Testimonium basis with no chair")
    actual = [row.get("chair") if isinstance(row, dict) else None for row in rows]
    if len(expected) != len(set(expected)) or len(actual) != len(set(actual)):
        raise SchemaRefusal("a Perlector dissent record repeats a witness chair")
    if set(actual) != set(expected):
        raise SchemaRefusal(
            "a Perlector dissent record does not account for exactly every witness in its basis"
        )
    outcomes = {row["chair"]: row.get("outcome") for row in basis_testimonia}

    for index, row in enumerate(rows):
        compared = row.get("compared")
        reported = outcomes[row["chair"]] in WITNESS_READING_OUTCOMES
        if reported and compared not in (True, "unknown"):
            raise SchemaRefusal(f"dissent[{index}] drops a witness that produced a reading")
        if not reported and (compared is not False or row.get("reason") != outcomes[row["chair"]]):
            raise SchemaRefusal(
                f"dissent[{index}] invents a comparison for a witness that did not report"
            )
        if compared is True:
            if set(row) != {
                "chair",
                "compared",
                "departed",
                "departed_raw",
                "departures",
                "comparison_loss",
            }:
                raise SchemaRefusal(f"dissent[{index}] is not the closed compared-row schema")
            if not isinstance(row["departed"], bool) or not isinstance(row["departed_raw"], bool):
                raise SchemaRefusal(f"dissent[{index}] has no boolean departure findings")
            loss = row["comparison_loss"]
            if (
                not isinstance(loss, dict)
                or set(loss) != {"reading_dropped_characters", "witness_dropped_characters"}
                or any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                    for value in loss.values()
                )
            ):
                raise SchemaRefusal(f"dissent[{index}] has no loss-accounted comparison view")
            spans = row["departures"]
            if not isinstance(spans, list):
                raise SchemaRefusal(f"dissent[{index}] has no departure span list")
            if bool(spans) is not row["departed_raw"] or (
                row["departed"] and not row["departed_raw"]
            ):
                raise SchemaRefusal(f"dissent[{index}] contradicts its own departure spans")
            # Asked of `comparison_view`, never re-derived here: a second copy
            # of the formula agrees with the first only until one of them is
            # corrected.
            if loss["reading_dropped_characters"] != comparison_view(text)["dropped_characters"]:
                raise SchemaRefusal(
                    f"dissent[{index}] misstates the Perlectio comparison view's loss"
                )
            for span_index, span in enumerate(spans):
                if not isinstance(span, dict) or set(span) != {
                    "reading_span",
                    "testimonium_span",
                }:
                    raise SchemaRefusal(
                        f"dissent[{index}].departures[{span_index}] is not the closed span schema"
                    )
                for name, bounds in span.items():
                    if (
                        not isinstance(bounds, dict)
                        or set(bounds) != {"start", "end"}
                        or any(
                            not isinstance(value, int) or isinstance(value, bool)
                            for value in bounds.values()
                        )
                        or bounds["start"] < 0
                        or bounds["end"] < bounds["start"]
                        or (name == "reading_span" and bounds["end"] > len(text))
                    ):
                        raise SchemaRefusal(
                            f"dissent[{index}].departures[{span_index}].{name} has invalid bounds"
                        )
        elif compared is False or compared == "unknown":
            # Only a row the step budget stopped carries the budget it ran out of.
            budget = row.get("max_comparison_steps")
            if (
                set(row) - {"max_comparison_steps"} != {"chair", "compared", "reason"}
                or not isinstance(row.get("reason"), str)
                or not row["reason"]
                or (
                    "max_comparison_steps" in row
                    and (compared != "unknown" or type(budget) is not int or budget <= 0)
                )
            ):
                raise SchemaRefusal(f"dissent[{index}] is not the closed uncomputed-row schema")
            if (
                "max_comparison_steps" in row
                and max_comparison_steps is not None
                and budget != max_comparison_steps
            ):
                raise SchemaRefusal(
                    f"dissent[{index}] records a {budget}-step dissent budget, but this run "
                    f"sealed {max_comparison_steps}"
                )
        else:
            raise SchemaRefusal(f"dissent[{index}] has an invalid comparison state")
