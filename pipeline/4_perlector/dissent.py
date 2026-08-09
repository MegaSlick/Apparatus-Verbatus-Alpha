"""Dissent, computed against derived comparison views -- never against raw bytes
picked to make a witness look right or wrong.

ARCHITECTURE: "The Perlectio records where the reading departed from every
witness. This is structural, not evaluative... It is not a quality signal on
its own." Spec_08 asks for the comparison to run on "derived comparison views --
loss-accounted normalizations built beside the verbatim payloads (which are
never coerced, per spec 07); where a witness format cannot be compared, dissent
for that witness is recorded `unknown`, never guessed."

Two things this module must never do, because either would be a picker with
extra steps: choose which view "wins" (there is no winning; a view is compared
to the fixed established reading, computed *after* the reading exists, and
never fed back into it), and coerce an incomparable witness into a fake
agreement or disagreement rather than naming the comparison itself as unknown.

**Pinned forever: equality only, never a distance metric.** `comparison_view`
takes no per-chair parameter and no similarity threshold, and never will --
"closest match" needs a metric, and refusing metrics is what keeps a future
edit from turning a normalization into a fuzzy-match picker one similarity
score at a time. A reviewer reading a change to this file should refuse
anything that adds a threshold, a weight, or a "close enough" comparison.

`departed` is computed on the *normalized* view; `departed_raw` is computed on
the untouched strings. Both are recorded, because when either side's
normalization drops characters, view-equality alone cannot say whether the raw
strings actually matched -- and GOVERNANCE 2 does not let that distinction go
unrecorded merely because it is cheap to recompute.

**`departures` says *where*, and a boolean cannot.** Spec_08's own dissent test
asks that "dissent matches expected spans", and the reason is the instrument's
purpose: a checkpoint that has learned to echo witnesses instead of reading ink
shows up as agreement everywhere *except* a few short spans, which one boolean
per chair cannot distinguish from wholesale disagreement. The spans are an
alignment produced by `difflib.SequenceMatcher.get_opcodes`, over the raw
strings so the offsets are valid in the Perlectio's own `text`.

That is not the distance metric pinned against above, and the difference is
worth stating because it is exactly the line a later edit will cross by
accident: an opcode list is a *description* of where two strings differ, with
no number attached and nothing to compare against a threshold.
`SequenceMatcher.ratio()` is the metric, it is not called here, and it is the
thing a reviewer should refuse if it appears.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from common.contracts.errors import SchemaRefusal
from common.stage import WITNESS_READING_OUTCOMES


def comparison_view(text: str) -> dict[str, object]:
    """A loss-accounted normalization: whitespace-collapsed text, and what that
    collapse dropped, so the normalization is honest about what it discarded
    rather than silently lossy. Case is never folded -- a case difference is a
    real disagreement about the ink, not a formatting artifact."""
    normalized = " ".join(text.split())
    return {"normalized": normalized, "dropped_characters": len(text) - len(normalized)}


def departures(reading: str, reported: str) -> list[dict[str, dict[str, int]]]:
    """Every span where the established reading and one witness's report differ.

    `autojunk=False` is load-bearing rather than stylistic: with it on,
    `SequenceMatcher` treats any element appearing in more than 1% of a
    sequence longer than 200 characters as junk, which on French prose means
    spaces and common letters stop counting as matches. The alignment would
    then change shape purely because the act was long, and a dissent record
    that means something different on long acts than on short ones is not a
    structural record.

    An equal reading and report produce no departures at all -- the correct
    output on the easy line every witness agrees about (ARCHITECTURE: "a metric
    that rewards disagreement rewards hallucination").
    """
    return [
        {
            "reading_span": {"start": reading_start, "end": reading_end},
            "testimonium_span": {"start": witness_start, "end": witness_end},
        }
        for tag, reading_start, reading_end, witness_start, witness_end in SequenceMatcher(
            a=reading, b=reported, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]


def is_comparable(record: dict[str, Any]) -> bool:
    """Whether a Testimonium's own declared format admits a plain comparison view.

    A witness whose format can express uncertainty
    (`format_capabilities.can_express_uncertainty`, spec_07) may embed
    alternative-reading markup inline in `reported` -- diffing that raw string
    against clean established text would count markup characters as
    disagreement, which is not what dissent means. No witness in this fixture
    declares this today (every configured chair reports plain text), so this
    branch is presently unreachable from a live producer; it exists so an
    incomparable format is refused a fake comparison rather than silently
    treated as one, the day a witness that does declare it exists.

    **Known watch item, named rather than hidden:** this is a per-capability
    exemption, so a witness adapter that self-declares
    `can_express_uncertainty` goes permanently uncompared on this axis -- it
    cannot touch the reading (dissent is read-only, computed after the fact),
    so it is not a picker, but it does blind the one instrument ARCHITECTURE
    names for catching a checkpoint that "learned to agree with witnesses
    rather than to read ink." A real markup-aware comparison view for such a
    format is future work; until it exists, every `compared: "unknown"` row
    this function produces must stay visible in the record rather than
    disappear into a coverage count, which is exactly what `dissent_against`
    below does -- it never drops a chair from the list.
    """
    payload = record.get("payload", {})
    capabilities = payload.get("format_capabilities", {})
    return not bool(capabilities.get("can_express_uncertainty", False))


def dissent_against(reading: str, testimonia: list[dict]) -> list[dict]:
    """Where the reading departed from each witness that actually reported.

    Computed after the reading is fixed. A chair that failed or never ran has
    no opinion to depart from, and is recorded as having none rather than as
    agreeing -- silence is not assent. A chair whose format cannot be reduced to
    a comparison view is recorded `compared: "unknown"`: not guessed at, and not
    silently dropped from the record either.
    """
    reading_view = comparison_view(reading)
    rows = []
    for record in testimonia:
        chair = record["payload"]["chair"]
        if record["outcome"] not in WITNESS_READING_OUTCOMES:
            rows.append({"chair": chair, "compared": False, "reason": record["outcome"]})
            continue
        reported = record["payload"].get("reported")
        if not isinstance(reported, str):
            raise SchemaRefusal(
                f"completed Testimonium from chair {chair!r} carries no text to compare"
            )
        if not is_comparable(record):
            rows.append(
                {
                    "chair": chair,
                    "compared": "unknown",
                    "reason": (
                        "this witness's declared format cannot be reduced to a plain "
                        "comparison view"
                    ),
                }
            )
            continue
        witness_view = comparison_view(reported)
        rows.append(
            {
                "chair": chair,
                "compared": True,
                "departed": witness_view["normalized"] != reading_view["normalized"],
                "departed_raw": reported != reading,
                # Spans over the raw strings, so `reading_span` indexes the
                # Perlectio's own `text`. A whitespace-only difference therefore
                # shows departures here while `departed` above stays False:
                # those are two honest answers to two different questions, and
                # collapsing them would lose the one the instrument needs.
                "departures": departures(reading, reported),
                "comparison_loss": {
                    "reading_dropped_characters": reading_view["dropped_characters"],
                    "witness_dropped_characters": witness_view["dropped_characters"],
                },
            }
        )
    return rows
