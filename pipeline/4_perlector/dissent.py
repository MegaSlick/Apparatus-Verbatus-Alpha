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

import signal
import threading
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Final

from common.alignment import markup_text_view
from common.contracts.errors import SchemaRefusal
from common.stage import WITNESS_READING_OUTCOMES

# `SequenceMatcher`'s alignment cost is not simply the product of the two
# lengths: a reading and a report that differ in many scattered places --
# exactly what a systematically-mistaken witness produces, the case this
# instrument exists to catch -- costs close to the *cube* of the length, not
# the square. Measured in this chamber: a 6,800-character reading against an
# equally long, scattered-difference report is 46.2M pairs, comfortably under
# the bound below, and took 127 seconds. This constant is kept as a cheap
# prefilter for a witness stuck in a repetition loop until its token cap --
# the witness stage puts no ceiling on report length, and Churro's own
# 24,000-token cap can run well over a hundred thousand characters -- but it
# no longer bounds wall-clock time on its own -- `MAX_COMPARISON_SECONDS`
# below does that. Both numbers are untuned; alpha testing over real reports
# would tune them.
MAX_COMPARISON_CHARACTER_PAIRS: Final = 100_000_000

# The real backstop. `SequenceMatcher.get_opcodes()` is pure Python, so a
# `SIGALRM` fired while it is running interrupts it cleanly -- verified in this
# chamber. Where `SIGALRM` does not exist (non-Unix), the comparison runs to
# completion exactly as it did before this bound existed; there is no silent
# narrowing, only a platform on which this particular backstop cannot fire.
MAX_COMPARISON_SECONDS: Final = 5


class _ComparisonTimedOut(Exception):
    """Raised only inside `_aligned_within_deadline`, never let escape it."""


def _deadline_handler(signum: int, frame: Any) -> None:
    raise _ComparisonTimedOut()


def _aligned_within_deadline(reading: str, reported: str, *, seconds: int) -> list | None:
    """`departures(reading, reported)`, abandoned rather than awaited past `seconds`.

    Returns `None` on timeout. Nothing about `reading` or `reported` is
    touched either way -- the alignment simply does not finish, exactly as
    the pair-count bound already declares of itself.
    """
    # The same ownership rule `common/alignment.py::align_to_anchor` carries:
    # `SIGALRM` and `ITIMER_REAL` are process-global, so arming unconditionally
    # would replace a caller's own real-time timer and then cancel it in
    # `finally`, destroying a deadline this module never owned. From a
    # non-main thread `signal.signal` raises outright. Arm only where nothing
    # else owns the timer; otherwise run without installing a timeout -- a
    # caller deadline applies only when the caller provides one, as in the
    # missing-SIGALRM branch below.
    if (
        not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "ITIMER_REAL")
        or threading.current_thread() is not threading.main_thread()
        or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)
    ):
        return departures(reading, reported)
    previous_handler = signal.signal(signal.SIGALRM, _deadline_handler)
    signal.alarm(seconds)
    try:
        result = departures(reading, reported)
        # Cancelled inside the `try`, not only in `finally`: an alarm that
        # fires after `departures` returns but before `finally` runs would
        # otherwise raise `_ComparisonTimedOut` past the `except` above and
        # propagate a timeout out of a function whose contract is to return
        # `None` instead. A firing in the remaining instructions is still
        # caught below and understates a finished comparison rather than
        # crashing one -- the safe direction of the two.
        signal.alarm(0)
        return result
    except _ComparisonTimedOut:
        return None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


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
    (`format_capabilities.can_express_uncertainty`) may embed alternative-
    reading markup inline in `reported` -- diffing that raw string against
    clean established text would count markup characters as disagreement,
    which is not what dissent means. Such a chair stays unmeasurable UNLESS a
    derived comparison view already exists for it (`comparison_reported`,
    never the raw `reported`): an anchored, markup-stripped page slice for a
    page witness, or `common/alignment.py::bracket_marker_view` for an
    act-scoped one. A chair with one rejoins the instrument through that safe
    view; one without -- a page witness whose alignment failed -- stays
    honestly unknown with its reason recorded rather than folded into a
    coverage count. An act-scoped chair always gets the bracket view
    (`run.py::comparison_views`), so a future chair whose notation is not
    brackets would rejoin as comparable anyway, its own markers surviving as
    false disagreement; nothing here reads a notation field to catch that.
    """
    payload = record.get("payload", {})
    capabilities = payload.get("format_capabilities", {})
    if not bool(capabilities.get("can_express_uncertainty", False)):
        return True
    return isinstance(payload.get("comparison_reported"), str)


def dissent_against(reading: str, testimonia: list[dict]) -> list[dict]:
    """Where the reading departed from each witness that actually reported.

    Computed after the reading is fixed. A chair that failed or never ran has
    no opinion to depart from, and is recorded as having none rather than as
    agreeing -- silence is not assent. `compared: "unknown"` is what a chair that
    did report but could not be compared receives, and it has five causes:
    retained testimony that is not text, a declared format that cannot be reduced
    to a comparison view, a page witness unattached to this act and carrying no
    `comparison_reported`, a report large enough to refuse outright
    (`MAX_COMPARISON_CHARACTER_PAIRS`), and an alignment that did not finish
    within `MAX_COMPARISON_SECONDS`. Never guessed at, and never silently dropped
    from the record either.
    """
    reading_view = comparison_view(reading)
    rows = []
    for record in testimonia:
        chair = record["payload"]["chair"]
        if record["outcome"] not in WITNESS_READING_OUTCOMES:
            rows.append({"chair": chair, "compared": False, "reason": record["outcome"]})
            continue
        # Legacy fixture inputs may carry `reported`; production records use
        # `payload`, while an act-aligned page slice still takes priority.
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
        if (
            record["payload"].get("page_witness") is True
            and "comparison_reported" not in record["payload"]
        ):
            rows.append(
                {
                    "chair": chair,
                    "compared": "unknown",
                    # An attached page witness always carries a comparison
                    # view, so absence means exactly one thing: the chair's
                    # recorded alignment is explicitly unaligned.
                    "reason": (
                        "page witness is not attached to this act; its recorded alignment "
                        "is explicitly unaligned, so there is no act-anchored comparison "
                        "view to diff"
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
        spans = _aligned_within_deadline(reading, reported, seconds=MAX_COMPARISON_SECONDS)
        if spans is None:
            rows.append(
                {
                    "chair": chair,
                    "compared": "unknown",
                    "reason": (
                        f"a {len(reading)}-character reading against a {len(reported)}-"
                        f"character report did not align within this module's "
                        f"{MAX_COMPARISON_SECONDS}-second bound; neither text is clipped and "
                        "neither is changed, the alignment simply did not run"
                    ),
                }
            )
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


def validate_dissent(rows: Any, *, text: str, basis_testimonia: list[dict]) -> None:
    """Refuse a dissent record that loses or duplicates a witness.

    Agreement is represented by one row with an empty ``departures`` list, not
    by omitting the row.  Otherwise an empty dissent list makes "all witnesses
    agreed" indistinguishable from "the instrument did not run" -- exactly the
    silent loss this record exists to prevent.
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
            if (
                set(row) != {"chair", "compared", "reason"}
                or not isinstance(row.get("reason"), str)
                or not row["reason"]
            ):
                raise SchemaRefusal(f"dissent[{index}] is not the closed uncomputed-row schema")
        else:
            raise SchemaRefusal(f"dissent[{index}] has an invalid comparison state")
