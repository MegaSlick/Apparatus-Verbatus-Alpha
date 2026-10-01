"""Canonical, text-adjacent transcription uncertainty.

Offsets are Python Unicode code-point offsets in the one established ``text``.
They are never byte offsets and never refer to a normalized or display string.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.envelope import digest_ref
from common.contracts.errors import SchemaRefusal

_FIELDS = frozenset({"uncertain_spans", "gaps", "self_revisions", "assessment", "lectio_kind"})
ASSESSMENT_STATES: Final = frozenset({"assessed", "not-assessed", "malformed"})
_ASSESSMENT_FIELDS = frozenset({"state", "problem"})
CONFIDENCE_LEVELS: Final = frozenset({"low", "medium", "high"})
GAP_POSITIONS: Final = frozenset({"leading", "internal", "trailing", "whole-act"})
# Teklia/DAI-CReTDHI-RecordGold-ATR's two uncertainty markers (MIT licence).
UNCERTAINTY_TOKENS: Final = ("[UNCERTAIN]", "[CROSSED_OUT]")
# What may follow a trailing gap: a reader that stops where the legible text ends
# writes the mark and may still close the line or the sentence after it.
_TRAILING_TAIL: Final = frozenset(".,;:!?)]}»›\"'’”")


def is_trailing_offset(text: str, offset: int) -> bool:
    """Whether a gap at `offset` ends the reading: past its start, and followed only by
    whitespace or closing punctuation."""
    return 0 < offset and all(
        character.isspace() or character in _TRAILING_TAIL for character in text[offset:]
    )


# How the one reading was made: a whole page read with the witnesses beside it and
# no prior draft, so its self-revisions were not measured (`None`).
PAGE_READ_LECTIO: Final = "page-read"

_GAP_EVIDENCE_FIELDS = frozenset({"chair", "testimonium_id", "reference", "variant"})


def from_page_perlectio(payload: dict[str, Any]) -> dict[str, Any]:
    """The exportable uncertainty layer of a page reading's `perlectio.v3`.

    Its lectio kind is `page-read`, whose self-revisions were not measured (`None`).
    """
    if not isinstance(payload, dict):
        raise SchemaRefusal("canonical uncertainty requires an object Perlectio payload")
    # The page reading's record repeats the reader's spans and gaps inside its
    # assessment; the two copies must be one fact, and the layer keeps the
    # closed `{state, problem}` pair.
    recorded = payload.get("uncertainty_assessment")
    if not isinstance(recorded, dict):
        raise SchemaRefusal("the page Perlectio carries no uncertainty_assessment")
    for field in ("uncertain_spans", "gaps"):
        if field not in recorded or recorded[field] != payload.get(field):
            raise SchemaRefusal(
                f"the page Perlectio's {field} differ from the copy in its uncertainty_assessment"
            )
    assessment = validate_assessment_record(
        {"state": recorded.get("state"), "problem": recorded.get("problem")},
        "the page Perlectio's uncertainty_assessment",
    )
    layer = {
        "uncertain_spans": payload.get("uncertain_spans"),
        "gaps": payload.get("gaps"),
        "self_revisions": None,
        "assessment": assessment,
        "lectio_kind": PAGE_READ_LECTIO,
    }
    validate(layer, payload.get("text"))
    return layer


def validate_assessment_record(assessment: Any, subject: str = "canonical uncertainty") -> dict:
    """The closed `{state, problem}` doubt record, refused by name or returned.

    One function, so every reader of an assessment asks the same question.
    """
    if not isinstance(assessment, dict) or set(assessment) != _ASSESSMENT_FIELDS:
        raise SchemaRefusal(f"{subject} has no closed assessment record")
    # Typed first: `in` raises TypeError on an unhashable value.
    if type(assessment["state"]) is not str or assessment["state"] not in ASSESSMENT_STATES:
        raise SchemaRefusal(f"{subject} names an unknown assessment state {assessment['state']!r}")
    if assessment["problem"] is not None and (
        not isinstance(assessment["problem"], str) or not assessment["problem"]
    ):
        raise SchemaRefusal(f"{subject}'s assessment problem is not null or a string")
    if (assessment["state"] == "assessed") != (assessment["problem"] is None):
        raise SchemaRefusal(
            f"{subject}'s assessment carries a problem exactly when it is not assessed"
        )
    return assessment


def validate(layer: Any, text: Any) -> dict[str, Any]:
    """Refuse uncertainty that cannot anchor exactly to the supplied text."""
    if not isinstance(text, str):
        raise SchemaRefusal("uncertainty offsets require exactly one string text field")
    if not isinstance(layer, dict) or set(layer) != _FIELDS:
        raise SchemaRefusal("uncertainty is not its closed canonical schema")
    uncertain = layer["uncertain_spans"]
    gaps = layer["gaps"]
    validate_assessment_record(layer["assessment"])
    if layer["lectio_kind"] != PAGE_READ_LECTIO:
        raise SchemaRefusal("canonical uncertainty names an unknown lectio kind")
    if layer["self_revisions"] is not None:
        raise SchemaRefusal("a page reading's self-revisions are not measured")
    if not isinstance(uncertain, list) or not isinstance(gaps, list):
        raise SchemaRefusal("canonical uncertainty members must all be lists")
    for index, span in enumerate(uncertain):
        if not isinstance(span, dict) or set(span) != {
            "start",
            "end",
            "alternatives",
            "confidence",
        }:
            raise SchemaRefusal(f"uncertain_spans[{index}] is not the canonical span schema")
        _range(span, text, f"uncertain_spans[{index}]", nonempty=True)
        if (
            type(span["confidence"]) is not str
            or span["confidence"] not in CONFIDENCE_LEVELS
            or not isinstance(span["alternatives"], list)
            or not all(isinstance(value, str) for value in span["alternatives"])
        ):
            raise SchemaRefusal(f"uncertain_spans[{index}] is malformed")
    whole_act_rows = 0
    for index, gap in enumerate(gaps):
        if not isinstance(gap, dict) or set(gap) != {
            "position",
            "start",
            "end",
            "witness_evidence",
        }:
            raise SchemaRefusal(f"gaps[{index}] is not the canonical gap schema")
        _range(gap, text, f"gaps[{index}]", nonempty=False)
        if gap["start"] != gap["end"] or not isinstance(gap["witness_evidence"], list):
            raise SchemaRefusal(f"gaps[{index}] is not a zero-width canonical gap")
        position = gap["position"]
        if type(position) is not str or position not in GAP_POSITIONS:
            raise SchemaRefusal(
                f"gaps[{index}] position {position!r} is not one of {sorted(GAP_POSITIONS)}"
            )
        if position == "leading" and gap["start"] != 0:
            raise SchemaRefusal(f"gaps[{index}] is declared leading but does not start at 0")
        if position == "trailing" and text.strip() and not is_trailing_offset(text, gap["end"]):
            raise SchemaRefusal(
                f"gaps[{index}] is declared trailing but text follows it other than whitespace "
                "or closing punctuation"
            )
        if position == "internal" and (
            not 0 < gap["start"] < len(text) or is_trailing_offset(text, gap["start"])
        ):
            raise SchemaRefusal(
                f"gaps[{index}] is declared internal but is not strictly inside the text"
            )
        if position == "whole-act" and (text.strip() != "" or gap["start"] != 0):
            raise SchemaRefusal(f"gaps[{index}] is declared whole-act but the text is not empty")
        if position == "whole-act":
            whole_act_rows += 1
        for evidence_index, evidence in enumerate(gap["witness_evidence"]):
            label = f"gaps[{index}].witness_evidence[{evidence_index}]"
            if (
                not isinstance(evidence, dict)
                or set(evidence) != _GAP_EVIDENCE_FIELDS
                or not isinstance(evidence.get("chair"), str)
                or not evidence["chair"]
                or not isinstance(evidence.get("testimonium_id"), str)
                or not evidence["testimonium_id"]
                or not isinstance(evidence.get("variant"), str)
            ):
                raise SchemaRefusal(f"{label} is not the canonical witness-evidence record")
            digest_ref(evidence["reference"], f"{label}.reference")
    if whole_act_rows and (whole_act_rows != 1 or len(gaps) != 1):
        raise SchemaRefusal(
            "a whole-act gap must be the only gap in canonical uncertainty; a reading "
            "cannot be simultaneously wholly illegible and partly read"
        )
    # Over an empty text the position checks above say nothing, so only
    # `whole-act` may appear. Asked after exclusivity, the stronger statement.
    if text.strip() == "":
        for index, gap in enumerate(gaps):
            if gap["position"] != "whole-act":
                raise SchemaRefusal(
                    f"gaps[{index}] is declared {gap['position']!r} over an empty text; the "
                    "only position that means anything where nothing was read is 'whole-act'"
                )
    return layer


def utf8_round_trip(layer: Any, text: Any) -> None:
    """Assert byte encoding/decoding cannot silently change offset meaning.

    Takes the same unchecked arguments `validate` does, and refuses them the same
    way, so a caller that wants both questions asked need not ask the first one
    twice.
    """
    validate(layer, text)
    restored = text.encode("utf-8").decode("utf-8")
    if restored != text:
        raise SchemaRefusal("projection UTF-8 round trip changed established text")
    validate(layer, restored)


def _range(value: Any, text: str, label: str, *, nonempty: bool) -> None:
    if not isinstance(value, dict) or not {"start", "end"} <= set(value):
        raise SchemaRefusal(f"{label} has no offset range")
    _integers(value, label)
    if not 0 <= value["start"] <= value["end"] <= len(text) or (
        nonempty and value["start"] == value["end"]
    ):
        raise SchemaRefusal(f"{label} is outside the canonical text's Unicode offsets")


def _integers(value: dict[str, Any], label: str) -> None:
    if any(
        not isinstance(value[key], int) or isinstance(value[key], bool) for key in ("start", "end")
    ):
        raise SchemaRefusal(f"{label} has non-integer offsets")
