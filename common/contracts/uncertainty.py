"""Canonical, text-adjacent transcription uncertainty.

Offsets are Python Unicode code-point offsets in the one established ``text``.
They are never byte offsets and never refer to a normalized or display string.
"""

from __future__ import annotations

from typing import Any

from common.contracts.errors import SchemaRefusal

_FIELDS = frozenset({"uncertain_spans", "gaps", "self_revisions"})
_CONFIDENCE = frozenset({"low", "medium", "high"})
# Mirrors pipeline/4_perlector/annotations.py's GAP_POSITIONS exactly (as
# `_CONFIDENCE` above already mirrors that module's CONFIDENCE_LEVELS): the
# producer's vocabulary already excludes anything this canonical projection
# layer would need to invent, and importing pipeline code from common/ would
# invert the dependency direction.
_GAP_POSITIONS = frozenset({"leading", "internal", "trailing", "whole-act"})


def from_perlectio(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the one exportable uncertainty representation from a Perlectio."""
    layer = {
        "uncertain_spans": payload.get("uncertain_spans"),
        "gaps": payload.get("gaps"),
        "self_revisions": [
            {"reading_span": item.get("reading_span"), "prior_span": item.get("testimonium_span")}
            for item in payload.get("self_revision", [])
        ],
    }
    validate(layer, payload.get("text"))
    return layer


def validate(layer: Any, text: Any) -> dict[str, Any]:
    """Refuse uncertainty that cannot anchor exactly to the supplied text."""
    if not isinstance(text, str):
        raise SchemaRefusal("uncertainty offsets require exactly one string text field")
    if not isinstance(layer, dict) or set(layer) != _FIELDS:
        raise SchemaRefusal("uncertainty is not its closed canonical schema")
    uncertain = layer["uncertain_spans"]
    gaps = layer["gaps"]
    revisions = layer["self_revisions"]
    if (
        not isinstance(uncertain, list)
        or not isinstance(gaps, list)
        or not isinstance(revisions, list)
    ):
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
            span["confidence"] not in _CONFIDENCE
            or not isinstance(span["alternatives"], list)
            or not all(isinstance(value, str) for value in span["alternatives"])
        ):
            raise SchemaRefusal(f"uncertain_spans[{index}] is malformed")
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
        if position not in _GAP_POSITIONS:
            raise SchemaRefusal(
                f"gaps[{index}] position {position!r} is not one of {sorted(_GAP_POSITIONS)}"
            )
        if position == "leading" and gap["start"] != 0:
            raise SchemaRefusal(f"gaps[{index}] is declared leading but does not start at 0")
        if position == "trailing" and gap["end"] != len(text):
            raise SchemaRefusal(f"gaps[{index}] is declared trailing but does not end at len(text)")
        if position == "internal" and not 0 < gap["start"] < len(text):
            raise SchemaRefusal(
                f"gaps[{index}] is declared internal but is not strictly inside the text"
            )
        if position == "whole-act" and (text != "" or gap["start"] != 0):
            raise SchemaRefusal(f"gaps[{index}] is declared whole-act but the text is not empty")
    for index, revision in enumerate(revisions):
        if not isinstance(revision, dict) or set(revision) != {"reading_span", "prior_span"}:
            raise SchemaRefusal(f"self_revisions[{index}] is not the canonical revision schema")
        _range(
            revision["reading_span"], text, f"self_revisions[{index}].reading_span", nonempty=False
        )
        prior = revision["prior_span"]
        if not isinstance(prior, dict) or set(prior) != {"start", "end"}:
            raise SchemaRefusal(f"self_revisions[{index}].prior_span is malformed")
        _integers(prior, f"self_revisions[{index}].prior_span")
        if prior["start"] < 0:
            raise SchemaRefusal(f"self_revisions[{index}].prior_span has a negative offset")
        if prior["start"] > prior["end"]:
            raise SchemaRefusal(f"self_revisions[{index}].prior_span is reversed")
    return layer


def utf8_round_trip(layer: dict[str, Any], text: str) -> None:
    """Assert byte encoding/decoding cannot silently change offset meaning."""
    validate(layer, text)
    restored = text.encode("utf-8").decode("utf-8")
    if restored != text:
        raise SchemaRefusal("projection UTF-8 round trip changed established text")
    validate(layer, restored)


def _range(value: Any, text: str, label: str, *, nonempty: bool) -> None:
    if not isinstance(value, dict) or set(value) < {"start", "end"}:
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
