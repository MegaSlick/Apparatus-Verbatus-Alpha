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
nuda and dissent are the instruments for that behaviour (GOVERNANCE 7).

An uncertain span is the opposite case: text the Perlector *did* read, held
with less confidence, with alternatives noted. It carries real characters on
purpose -- that is what "read, with alternatives" means -- and it is validated
only for shape (bounds inside `text`, a closed confidence vocabulary), because
whether a span's content was genuinely read or silently borrowed from a witness
is not a thing a bounds check can decide; that is what the dissent record and
Lectio nuda comparison exist for instead.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.errors import SchemaRefusal

CONFIDENCE_LEVELS: Final = frozenset({"low", "medium", "high"})
GAP_POSITIONS: Final = frozenset({"leading", "internal", "trailing", "whole-act"})

_SPAN_FIELDS: Final = frozenset({"start", "end", "alternatives", "confidence"})
_GAP_FIELDS: Final = frozenset({"position", "start", "end", "witness_evidence"})
# A gap's evidence names the chair, what it reported, and *which artifact said
# so*. The chair alone is a claim about a witness; the digest-checked reference
# is the witness's own sealed record, which is what GOALS 5 means by a result
# returning to the witnesses that saw it. Without it a displayed
# "(illegible -- witnesses agree: Tyrel)" cannot be traced back to the
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
            or not (0 <= start <= end <= len(text))
        ):
            raise SchemaRefusal(
                f"uncertain_spans[{index}] carries a span outside text bounds (0..{len(text)})"
            )
        alternatives = span.get("alternatives")
        if not isinstance(alternatives, list) or not all(
            isinstance(alternative, str) for alternative in alternatives
        ):
            raise SchemaRefusal(f"uncertain_spans[{index}] has no list of string alternatives")
        if span.get("confidence") not in CONFIDENCE_LEVELS:
            raise SchemaRefusal(
                f"uncertain_spans[{index}] confidence {span.get('confidence')!r} is not one "
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
        # GOVERNANCE 3 and spec_08 forbid by name, whatever those characters
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
    validate_uncertain_spans(payload.get("uncertain_spans", []), text)
    gaps = validate_gaps(payload.get("gaps", []), text)
    if outcome is not None:
        validate_whole_act_consistency(outcome=outcome, text=text, gaps=gaps)
