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

import unicodedata
from difflib import SequenceMatcher
from typing import Any, Final

from common.contracts.errors import SchemaRefusal
from common.stage import WITNESS_READING_OUTCOMES

# `SequenceMatcher`'s alignment costs the product of the two lengths. Measured
# in this chamber at roughly 12 million character-pairs per second: 5,000 by
# 5,000 is two seconds, 16,000 by 16,000 is twenty, and it keeps squaring.
#
# A witness's `reported` is a model's own output and nothing upstream bounds it
# -- `pipeline/3_attestatores/run.py` records a character count and enforces no
# ceiling on it. A model in a repetition loop emits until its token cap, which
# is the ordinary failure `truncation.py` exists because of, not an exotic one;
# at a 32k-token cap that is well over a hundred thousand characters, and one
# such report would hold the stage for tens of minutes on every act it touched.
#
# **The bound is on the comparison, never on the text.** Nothing is clipped,
# no reading is touched, and the witness keeps its row -- saying honestly that
# the alignment did not run. Spec 08 already declares that shape: "where a
# witness format cannot be compared, dissent for that witness is recorded
# `unknown`, never guessed", and a comparison too large to run is one that
# cannot be run. Set far above any plausible act (a hundred million pairs is a
# 10,000-character reading against a 10,000-character report, where a register
# entry runs to hundreds), so only a runaway reaches it -- a cost ceiling, not
# a calibrated constant, and alpha testing over real reports is what would tune
# it.
MAX_COMPARISON_CHARACTER_PAIRS: Final = 100_000_000


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
    a comparison view, or whose report is too long to align against this reading
    at all (`MAX_COMPARISON_CHARACTER_PAIRS`), is recorded `compared: "unknown"`:
    not guessed at, and not silently dropped from the record either.
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


def validate_dissent(rows: Any, *, text: str, basis_testimonia: list[dict]) -> None:
    """Refuse a dissent record that loses or duplicates a witness.

    Agreement is represented by one row with an empty ``departures`` list, not
    by omitting the row.  Otherwise an empty dissent list makes "all witnesses
    agreed" indistinguishable from "the instrument did not run" -- exactly the
    silent loss this record exists to prevent.
    """
    if not isinstance(rows, list):
        raise SchemaRefusal("a Perlector reading carries no dissent record")
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
            if (
                set(row) != {"chair", "compared", "reason"}
                or not isinstance(row.get("reason"), str)
                or not row["reason"]
            ):
                raise SchemaRefusal(f"dissent[{index}] is not the closed uncomputed-row schema")
        else:
            raise SchemaRefusal(f"dissent[{index}] has an invalid comparison state")
