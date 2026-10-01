"""Dissent, computed against derived comparison views -- never against raw bytes
picked to make a witness look right or wrong.

Recording where the established reading departs from a witness is structural,
not evaluative, and not a quality signal on its own. The comparison runs on
loss-accounted normalizations built beside the verbatim payloads, which are
never coerced; where a witness format cannot be compared, dissent for that
witness is recorded `unknown`, never guessed.

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
from bisect import bisect_left
from difflib import SequenceMatcher
from typing import Any, Final

from common.alignment import markup_text_view
from common.contracts.outcomes import WITNESS_READING_OUTCOMES

# A cheap refusal before any alignment: a witness stuck in a repetition loop
# until its token cap can report far more text than any act holds.
MAX_COMPARISON_CHARACTER_PAIRS: Final = 100_000_000

# The work one alignment may do, counted in steps: a step is one position of
# the witness report `SequenceMatcher`'s longest-match search visits. A report
# that differs from the reading in many scattered places costs close to the
# cube of its length, far more than the pair bound above admits, so the count
# is what bounds it. Charged before the work is done, so whether a comparison
# aligns depends only on its two texts, never on the machine or the clock.
MAX_COMPARISON_STEPS: Final = 50_000_000


class _OutOfSteps(Exception):
    """Raised only inside `_bounded_departures`, never let escape it."""


class _CountedMatcher(SequenceMatcher):
    """`SequenceMatcher` whose longest-match searches spend a counted budget."""

    def __init__(self, a: str, b: str, steps: int) -> None:
        self.steps_left = steps
        super().__init__(None, a, b, autojunk=False)

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        ahi = len(self.a) if ahi is None else ahi
        bhi = len(self.b) if bhi is None else bhi
        # The search visits, for each character of `a[alo:ahi]`, every position
        # of that character in `b` before `bhi`, and one more to stop.
        self.steps_left -= sum(
            bisect_left(self.b2j.get(self.a[i], ()), bhi) + 1 for i in range(alo, ahi)
        )
        if self.steps_left < 0:
            raise _OutOfSteps()
        return super().find_longest_match(alo, ahi, blo, bhi)


def _bounded_departures(reading: str, reported: str, *, steps: int) -> list | None:
    """`departures(reading, reported)`, or `None` when its alignment needs more than `steps`."""
    try:
        return _departure_spans(_CountedMatcher(reading, reported, steps))
    except _OutOfSteps:
        return None


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
    return _departure_spans(SequenceMatcher(a=reading, b=reported, autojunk=False))


def _departure_spans(matcher: SequenceMatcher) -> list[dict[str, dict[str, int]]]:
    return [
        {
            "reading_span": {"start": reading_start, "end": reading_end},
            "testimonium_span": {"start": witness_start, "end": witness_end},
        }
        for tag, reading_start, reading_end, witness_start, witness_end in matcher.get_opcodes()
        if tag != "equal"
    ]


def is_comparable(record: dict[str, Any]) -> bool:
    """Whether a Testimonium's own declared format admits a plain comparison view.

    A witness whose format can express uncertainty
    (`format_capabilities.can_express_uncertainty`) may embed alternative-
    reading markup inline in `reported` -- diffing that raw string against
    clean established text would count markup characters as disagreement,
    which is not what dissent means. Such a chair stays unmeasurable UNLESS a
    derived comparison view already exists for it (`comparison_reported`,
    never the raw `reported`): the text of the units an entry cites with its
    doubt markers removed (`common/alignment.py::bracket_marker_view`). A
    chair with one rejoins the instrument through that safe view; one without
    stays honestly unknown with its reason recorded rather than folded into a
    coverage count. A chair given the bracket view whose notation is not
    brackets would rejoin as comparable anyway, its own markers surviving as
    false disagreement; nothing here reads a notation field to catch that.
    """
    payload = record.get("payload", {})
    capabilities = payload.get("format_capabilities", {})
    if not bool(capabilities.get("can_express_uncertainty", False)):
        return True
    return isinstance(payload.get("comparison_reported"), str)


def dissent_against(
    reading: str, testimonia: list[dict], *, steps: int = MAX_COMPARISON_STEPS
) -> list[dict]:
    """Where the reading departed from each witness that actually reported.

    Computed after the reading is fixed. A chair that failed or never ran has
    no opinion to depart from, and is recorded as having none rather than as
    agreeing -- silence is not assent. `compared: "unknown"` is what a chair that
    did report but could not be compared receives, and it has four causes:
    retained testimony that is not text, a declared format that cannot be reduced
    to a comparison view, a report large enough to refuse outright
    (`MAX_COMPARISON_CHARACTER_PAIRS`), and an alignment that needed more than
    `steps` counted steps (`MAX_COMPARISON_STEPS`). The same texts always give
    the same rows. Never guessed at, and never silently dropped from the record
    either.
    """
    reading_view = comparison_view(reading)
    rows = []
    for record in testimonia:
        chair = record["payload"]["chair"]
        if record["outcome"] not in WITNESS_READING_OUTCOMES:
            rows.append({"chair": chair, "compared": False, "reason": record["outcome"]})
            continue
        # A derived comparison view (`comparison_reported`) is compared first;
        # otherwise the retained `payload`, or `reported` where a record names
        # its text so.
        reported = record["payload"].get(
            "comparison_reported",
            record["payload"].get("payload", record["payload"].get("reported")),
        )
        if not isinstance(reported, str):
            # A structured report remains visible as incomparable; coercing it
            # would invent text, while the witness floor requires comparability.
            rows.append(
                {
                    "chair": chair,
                    "compared": "unknown",
                    "reason": (
                        "no comparable text for this act: retained derived testimony is not text"
                    ),
                }
            )
            continue
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
        pairs = len(reading) * len(reported)
        if pairs > MAX_COMPARISON_CHARACTER_PAIRS:
            rows.append(
                {
                    "chair": chair,
                    "compared": "unknown",
                    "reason": (
                        f"a {len(reading)}-character reading against a {len(reported)}-"
                        f"character report is {pairs} character pairs to align, past this "
                        f"module's {MAX_COMPARISON_CHARACTER_PAIRS} bound; neither text is "
                        "clipped and neither is changed, the alignment simply did not run"
                    ),
                }
            )
            continue
        spans = _bounded_departures(reading, reported, steps=steps)
        if spans is None:
            rows.append(unaligned_row(chair, reading, reported, steps))
            continue
        markup_view = markup_text_view(reported)
        witness_view = comparison_view(markup_view["text"])
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
                "departures": spans,
                "comparison_loss": {
                    # `reading_dropped_characters` charges collapsed whitespace
                    # only; `witness_dropped_characters` below additionally
                    # charges markup and entity spelling removed from the
                    # report. Removal only, never re-encoding: NFC
                    # composition is not a loss (see `comparison_view`).
                    "reading_dropped_characters": reading_view["dropped_characters"],
                    "witness_dropped_characters": witness_view["dropped_characters"]
                    + markup_view["loss"]["markup_characters"]
                    + markup_view["loss"]["whitespace_characters"],
                },
            }
        )
    return rows


def unaligned_row(
    chair: str, reading: str, reported: str, steps: int = MAX_COMPARISON_STEPS
) -> dict[str, Any]:
    """The row of a comparison whose alignment needed more than `steps` counted steps."""
    return {
        "chair": chair,
        "compared": "unknown",
        "reason": (
            f"a {len(reading)}-character reading against a {len(reported)}-"
            f"character report needs more than this module's {steps}-step "
            "alignment bound; neither text is clipped and neither is changed, the "
            "alignment simply did not run"
        ),
    }
