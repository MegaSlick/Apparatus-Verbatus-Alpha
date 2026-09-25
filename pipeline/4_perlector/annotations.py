"""Uncertain spans and gap anchors: the one place `text` could quietly stop
being clean, and the schema that keeps it from happening.

Spec_08: "the established text never contains testimony-supplied characters.
No count of agreeing witnesses changes this." A gap is where sight failed;
`witness_evidence` on a gap is linked, displayable evidence -- searchable,
shown as "(illegible -- witnesses agree: ...)" -- never characters inside
`text`. **The declared-gap firewall is structural, not a promise**: a gap's own
bounds must be zero-width inside `text`, so a declared gap cannot carry text.
No count of agreeing witnesses can widen it -- the schema does not read
`witness_evidence` at all when deciding whether the gap's span is legal. This
does not claim to identify an undeclared model echo elsewhere in `text`; Lectio
nuda and dissent are the instruments for that behaviour (principle 3).

An uncertain span is the opposite case: text the Perlector *did* read, held
with less confidence, with alternatives noted. It carries real characters on
purpose -- that is what "read, with alternatives" means -- and it is validated
only for shape (bounds inside `text`, a closed confidence vocabulary), because
whether a span's content was genuinely read or silently borrowed from a witness
is not a thing a bounds check can decide; that is what the dissent record and
Lectio nuda comparison exist for instead.
"""

from __future__ import annotations

import re
from typing import Any, Final

from common.contracts.errors import SchemaRefusal

CONFIDENCE_LEVELS: Final = frozenset({"low", "medium", "high"})
GAP_POSITIONS: Final = frozenset({"leading", "internal", "trailing", "whole-act"})

_SPAN_FIELDS: Final = frozenset({"start", "end", "alternatives", "confidence"})
_GAP_FIELDS: Final = frozenset({"position", "start", "end", "witness_evidence"})
# A gap's evidence names the chair, what it reported, and *which artifact said
# so*. The chair alone is a claim about a witness; the digest-checked reference
# is the witness's own sealed record, which is what goal 4 means by a result
# returning to the witnesses that saw it. Without it a displayed
# "(illegible -- witnesses agree: Chair-A)" cannot be traced back to the
# Testimonium it came from.
_EVIDENCE_FIELDS: Final = frozenset({"chair", "testimonium_id", "reference", "variant"})


def validate_uncertain_spans(spans: Any, text: str) -> list[dict]:
    """Read text held with less confidence. Bounds-checked; content is not this
    function's business."""
    if not isinstance(spans, list):
        raise SchemaRefusal("uncertain_spans is not a list")
    validated = []
    for index, span in enumerate(spans):
        if not isinstance(span, dict) or set(span) != _SPAN_FIELDS:
            raise SchemaRefusal(f"uncertain_spans[{index}] is not the closed span schema")
        start, end = span.get("start"), span.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not (0 <= start <= len(text))
            or not (0 <= end <= len(text))
        ):
            raise SchemaRefusal(
                f"uncertain_spans[{index}] carries a span outside text bounds (0..{len(text)})"
            )
        if start >= end:
            raise SchemaRefusal(
                f"uncertain_spans[{index}] must cover at least one character; a zero-width "
                "span holds no read characters and may not carry alternatives"
            )
        alternatives = span.get("alternatives")
        if not isinstance(alternatives, list) or not all(
            isinstance(alternative, str) for alternative in alternatives
        ):
            raise SchemaRefusal(f"uncertain_spans[{index}] has no list of string alternatives")
        confidence = span.get("confidence")
        # Membership and refusal formatting may invoke subclass-defined
        # behavior, so both require an exact built-in string first.
        if type(confidence) is not str:
            raise SchemaRefusal(
                f"uncertain_spans[{index}] confidence has type "
                f"{type(confidence).__name__!a}, not an exact string level from "
                f"{sorted(CONFIDENCE_LEVELS)}"
            )
        if confidence not in CONFIDENCE_LEVELS:
            raise SchemaRefusal(
                f"uncertain_spans[{index}] confidence {confidence!r} is not one "
                f"of {sorted(CONFIDENCE_LEVELS)}"
            )
        validated.append(span)
    return validated


def validate_gaps(gaps: Any, text: str) -> list[dict]:
    """Where sight failed. The establishment firewall lives here: a gap that is
    not zero-width inside `text` is refused outright, never repaired or trusted."""
    if not isinstance(gaps, list):
        raise SchemaRefusal("gaps is not a list")
    validated = []
    whole_act_rows = 0
    for index, gap in enumerate(gaps):
        if not isinstance(gap, dict) or set(gap) != _GAP_FIELDS:
            raise SchemaRefusal(f"gaps[{index}] is not the closed gap schema")
        position = gap.get("position")
        if type(position) is not str:
            raise SchemaRefusal(
                f"gaps[{index}] position has type {type(position).__name__!a}, not an exact "
                f"string position from {sorted(GAP_POSITIONS)}"
            )
        if position not in GAP_POSITIONS:
            raise SchemaRefusal(
                f"gaps[{index}] position {position!r} is not one of {sorted(GAP_POSITIONS)}"
            )
        start, end = gap.get("start"), gap.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not (0 <= start <= len(text))
            or not (0 <= end <= len(text))
        ):
            raise SchemaRefusal(
                f"gaps[{index}] carries a position outside text bounds (0..{len(text)})"
            )
        # The firewall. A gap whose bounds are not equal claims characters of
        # `text` for a position where sight failed -- exactly the substitution
        # principle 1 and spec_08 forbid by name, whatever those characters
        # happen to equal. Checked before anything about the position label or
        # the evidence it carries, because this is the one rule that must hold
        # regardless of what else about the gap is true.
        if start != end:
            raise SchemaRefusal(
                f"gaps[{index}] claims start {start} != end {end}: a gap is where sight "
                "failed and may carry no characters of its own inside `text`; the "
                "establishment firewall refuses any gap that is not zero-width"
            )
        if position == "leading" and start != 0:
            raise SchemaRefusal(f"gaps[{index}] is declared leading but does not start at 0")
        if position == "trailing" and end != len(text):
            raise SchemaRefusal(f"gaps[{index}] is declared trailing but does not end at len(text)")
        if position == "internal" and not (0 < start < len(text)):
            raise SchemaRefusal(
                f"gaps[{index}] is declared internal but is not strictly inside the text"
            )
        if position == "whole-act":
            if text != "" or start != 0:
                raise SchemaRefusal(
                    f"gaps[{index}] is declared whole-act but the reading is not empty"
                )
            whole_act_rows += 1
        evidence = gap.get("witness_evidence")
        if not isinstance(evidence, list):
            raise SchemaRefusal(f"gaps[{index}] has no witness_evidence list")
        for item_index, item in enumerate(evidence):
            reference = item.get("reference") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or set(item) != _EVIDENCE_FIELDS
                or not isinstance(item.get("chair"), str)
                or not item["chair"]
                or not isinstance(item.get("testimonium_id"), str)
                or not item["testimonium_id"]
                # A string, and deliberately allowed to be blank: a
                # genuinely-empty witness reported "" and that report is the
                # strongest corroboration a whole-act gap can carry. Requiring
                # a non-blank variant here refused exactly the confirmed-blank
                # evidence the Recensor's corroboration is built on.
                or not isinstance(item.get("variant"), str)
                or not isinstance(reference, dict)
                or set(reference) != {"relative_path", "sha256"}
                or not all(isinstance(value, str) and value for value in reference.values())
            ):
                raise SchemaRefusal(
                    f"gaps[{index}].witness_evidence[{item_index}] is not a "
                    "{chair, testimonium_id, reference, variant} record"
                )
        validated.append(gap)
    if whole_act_rows and (whole_act_rows != 1 or len(validated) != 1):
        raise SchemaRefusal(
            "a whole-act gap must be the only gap an empty reading carries; a "
            "reading cannot be simultaneously wholly illegible and partly read"
        )
    return validated


def validate_whole_act_consistency(*, outcome: str, text: str, gaps: list[dict]) -> None:
    """The whole-act gap and the `no-readable-text` outcome must imply each other.

    One direction alone is not enough: requiring `no-readable-text` to carry a
    whole-act gap but not requiring the converse would let an outcome of
    `read` carry an empty `text` plus a whole-act gap and flow onward as though
    something had been established -- an empty text delivered as the one text,
    which ARCHITECTURE invariant 6 (partial or unresolved results can never
    appear complete) forbids exactly as directly as a missing gap would.
    """
    has_whole_act_gap = any(gap["position"] == "whole-act" for gap in gaps)
    if outcome == "no-readable-text" and not (text == "" and has_whole_act_gap):
        raise SchemaRefusal(
            "a 'no-readable-text' outcome must carry an empty text and exactly one "
            "whole-act gap; silence is proved, never merely declared by outcome alone"
        )
    if has_whole_act_gap and outcome != "no-readable-text":
        raise SchemaRefusal(
            f"a whole-act gap was recorded but the outcome is {outcome!r}, not "
            "'no-readable-text'; a reading cannot be wholly illegible and something "
            "other than unreadable at the same time"
        )


def validate_annotations(payload: dict[str, Any], *, outcome: str | None = None) -> None:
    """Validate both annotation layers together against the one `text` they sit over."""
    text = payload.get("text")
    if not isinstance(text, str):
        raise SchemaRefusal("a reading's annotations cannot be validated with no text field")
    for field in ("uncertain_spans", "gaps"):
        if field not in payload:
            raise SchemaRefusal(
                f"a reading carries no {field} record; an absent annotation layer is not "
                "the same claim as an empty one"
            )
    validate_uncertain_spans(payload["uncertain_spans"], text)
    gaps = validate_gaps(payload["gaps"], text)
    if outcome is not None:
        validate_whole_act_consistency(outcome=outcome, text=text, gaps=gaps)


# The reader's own doubt report. `assessed`: the reader was asked and its spans and
# gaps anchor to the text. `not-assessed`: the reader had no way to report doubt, so
# empty layers are an absence, not confidence. `malformed`: a report that could not
# be anchored, kept as a visible fault with empty layers (principle 8).
ASSESSMENT_ASSESSED: Final = "assessed"
ASSESSMENT_NOT_ASSESSED: Final = "not-assessed"
ASSESSMENT_MALFORMED: Final = "malformed"
ASSESSMENT_STATES: Final = frozenset(
    {ASSESSMENT_ASSESSED, ASSESSMENT_NOT_ASSESSED, ASSESSMENT_MALFORMED}
)
_ASSESSMENT_FIELDS: Final = frozenset({"state", "uncertain_spans", "gaps", "problem"})
NOT_ASSESSED_REASON: Final = (
    "the reader reports no doubt assessment; this chair has no channel for one"
)


def not_assessed(problem: str = NOT_ASSESSED_REASON) -> dict[str, Any]:
    """The honest report of a reader that cannot report doubts."""
    return {
        "state": ASSESSMENT_NOT_ASSESSED,
        "uncertain_spans": [],
        "gaps": [],
        "problem": problem,
    }


def malformed_assessment(problem: str) -> dict[str, Any]:
    """A doubt report that could not be anchored: retained as a fault, layers empty."""
    return {
        "state": ASSESSMENT_MALFORMED,
        "uncertain_spans": [],
        "gaps": [],
        "problem": problem,
    }


def validate_assessment(assessment: Any, text: str) -> dict[str, Any]:
    """The closed assessment record, its layers anchored to the exact text.

    Raises `SchemaRefusal` for anything it cannot accept; the producer turns that
    refusal into a `malformed` record rather than dropping the report, so the
    caller sees the problem where a reader would have looked for the doubt.
    A reader-reported gap is zero-width and carries no witness evidence: it says
    where sight failed, and the witnesses that corroborate an absence are the
    Recensor's business, not the reader's.
    """
    if not isinstance(assessment, dict) or set(assessment) != _ASSESSMENT_FIELDS:
        raise SchemaRefusal("a doubt assessment is not its closed record")
    state = assessment["state"]
    if type(state) is not str or state not in ASSESSMENT_STATES:
        raise SchemaRefusal(f"a doubt assessment names an unknown state {state!r}")
    problem = assessment["problem"]
    if problem is not None and (type(problem) is not str or not problem):
        raise SchemaRefusal("a doubt assessment's problem must be null or a non-empty string")
    if state != ASSESSMENT_ASSESSED:
        if assessment["uncertain_spans"] != [] or assessment["gaps"] != []:
            raise SchemaRefusal(
                f"a {state!r} doubt assessment may carry no spans or gaps; an unassessed or "
                "malformed report has nothing anchored to publish"
            )
        if problem is None:
            raise SchemaRefusal(f"a {state!r} doubt assessment must say why")
        return assessment
    if problem is not None:
        raise SchemaRefusal("an assessed doubt report carries no problem")
    validate_uncertain_spans(assessment["uncertain_spans"], text)
    gaps = validate_gaps(assessment["gaps"], text)
    for index, gap in enumerate(gaps):
        if gap["position"] == "whole-act":
            raise SchemaRefusal(
                f"gaps[{index}]: a reader-reported gap cannot be whole-act; an empty reading "
                "is the `no-readable-text` outcome, not a doubt"
            )
        if gap["witness_evidence"] != []:
            raise SchemaRefusal(
                f"gaps[{index}]: a reader-reported gap carries no witness evidence; the reader "
                "reports where its own sight failed, not what the witnesses said"
            )
    return assessment


_MARK = re.compile(r"\[\[([^\[\]]*)\]\]")
ILLEGIBLE_MARK: Final = "[[?]]"


def read_doubt_marks(raw: str) -> tuple[str, dict[str, Any]]:
    """Split a marked reading into its text and the doubts the reader marked.

    `[[?]]` is ink the reader could not read, a zero-width gap. `[[reading]]` or
    `[[reading|other|...]]` is a reading it was unsure of, with any other readings it
    offered; the grammar has one level of doubt, recorded as `low`. A mark that does
    not parse leaves the raw text published as returned, with a `malformed` report.
    """
    pieces: list[str] = []
    spans: list[dict[str, Any]] = []
    gap_offsets: list[int] = []
    length = cursor = 0
    for match in _MARK.finditer(raw):
        pieces.append(raw[cursor : match.start()])
        length += match.start() - cursor
        cursor = match.end()
        body = match.group(1)
        if body == "?":
            gap_offsets.append(length)
            continue
        reading, *alternatives = body.split("|")
        if not reading:
            return raw, malformed_assessment(f"the doubt mark {match.group(0)!r} names no reading")
        spans.append(
            {
                "start": length,
                "end": length + len(reading),
                "alternatives": alternatives,
                "confidence": "low",
            }
        )
        pieces.append(reading)
        length += len(reading)
    pieces.append(raw[cursor:])
    text = "".join(pieces)
    if "[[" in text or "]]" in text:
        return raw, malformed_assessment(
            "the reading holds a [[ or ]] that is not a closed doubt mark"
        )
    gaps = [
        {
            "position": "leading"
            if offset == 0
            else "trailing"
            if offset == len(text)
            else "internal",
            "start": offset,
            "end": offset,
            "witness_evidence": [],
        }
        for offset in gap_offsets
        if text
    ]
    return text, {
        "state": ASSESSMENT_ASSESSED,
        "uncertain_spans": spans,
        "gaps": gaps,
        "problem": None,
    }
