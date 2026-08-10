"""The proposed uncertainty/gap display convention. A proposal, not a decision.

Spec 11 leaves the uncertainty/gap convention to Tyrel at this gate, so a rendering has
to exist for him to pick against, and its test 2 asks for render -> strip -> hash, so a
way back from it has to exist for the identity test to mean anything. This module is
both and nothing more: **the choice changes only this file.** No hash, no stored field
and no format writer depends on which brackets are used.

**The split it adopts is EpiDoc's, and it is a semantic one rather than a
typographic one.** EpiDoc distinguishes `<unclear>` -- ink that is present but
doubted -- from `<gap>` -- ink that is simply gone; a reconstruction attributed to
someone else is `<supplied>` and sits outside the established text. That is exactly
the line Tyrel drew on 2026-08-05: a gap carries its evidence beside the text and
never characters inside it, and "we don't want it making shit up". The markers below
are plain text rather than literal XML because the near-term readers are a text file
and a terminal, not an XML toolchain. Literal bracket glyphs are escaped before
rendering, so even an established text containing the proposed delimiters remains
byte-identical after stripping; rarity is not used as a correctness argument.

**What is not exercised against real data, said rather than left to be found.** The
Archetypus record this stage reads carries one `text` field and no uncertainty or gap
layer yet, so every run this repository can produce renders established text with no
generated span markers (literal delimiter glyphs are escaped reversibly). The round
trip below is exercised only against spans built by hand in this module's tests. That
is a real gap: the pair is ready for the annotation layer the day it lands, and
nothing here has yet run against a real gap or a real uncertain span.
"""

import json
from dataclasses import dataclass, field
from typing import Final

# The name the EXPORT_MANIFEST reports, so a reader of the product can tell which
# convention produced a rendering without reading this file. It says "proposed"
# because it is: spec 11 leaves the choice to Tyrel at this gate.
DISPLAY_CONVENTION: Final = "epidoc-semantics-plaintext-markers.proposed.v2"

GAP_KINDS: Final = frozenset({"leading", "internal", "trailing", "whole-act"})

_UNCERTAIN_OPEN, _UNCERTAIN_CLOSE = "⟨", "⟩"
_GAP_OPEN, _GAP_CLOSE = "⟦", "⟧"
_ESCAPED_MARKERS: Final = {
    _UNCERTAIN_OPEN: r"\u27e8",
    _UNCERTAIN_CLOSE: r"\u27e9",
    _GAP_OPEN: r"\u27e6",
    _GAP_CLOSE: r"\u27e7",
}


@dataclass(frozen=True)
class UncertainSpan:
    """Ink present but doubted. `start`/`end` index into the established text."""

    start: int
    end: int
    alternatives: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.start, bool)
            or not isinstance(self.start, int)
            or isinstance(self.end, bool)
            or not isinstance(self.end, int)
        ):
            raise TypeError("an uncertain span needs integer bounds")
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"uncertain span [{self.start}, {self.end}) is not a valid range")
        if any(not isinstance(alternative, str) for alternative in self.alternatives):
            raise TypeError("uncertain alternatives must be strings")


@dataclass(frozen=True)
class GapAnchor:
    """Ink that is gone. `position` is an insertion point, never a range of text.

    A gap is never characters in the established text, so unlike an uncertain span it
    names only where in the text it belongs, plus a human-readable extent where one is
    known and whatever witnesses reported there as evidence *beside* the text. Nothing
    here can point outside the text or reorder it.
    """

    kind: str
    position: int
    extent_note: str = ""
    witness_variants: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in GAP_KINDS:
            raise ValueError(f"gap kind {self.kind!r} is not one of {sorted(GAP_KINDS)}")
        if isinstance(self.position, bool) or not isinstance(self.position, int):
            raise TypeError("a gap position must be an integer")
        if self.position < 0:
            raise ValueError(f"gap position {self.position} is negative")
        if not isinstance(self.extent_note, str) or any(
            not isinstance(variant, str) for variant in self.witness_variants
        ):
            raise TypeError("gap notes and witness variants must be strings")


def render_display(
    text: str,
    *,
    uncertain: list[UncertainSpan] | None = None,
    gaps: list[GapAnchor] | None = None,
) -> str:
    """Render the established text plus its uncertainty/gap layer for a human.

    Never mutates `text`, and its output never reaches a hash that is supposed to
    describe the canonical field. `strip_display` is the only sanctioned way back.

    Walks `text` once, merging the two event streams -- span boundaries and gap
    positions -- by position, rather than rendering spans first and then trying to
    relocate gaps in the widened result. A gap exactly at a span boundary renders
    immediately before the span; a gap strictly inside a span is refused, because a
    position cannot be both present-and-doubted and gone.
    """
    if not isinstance(text, str):
        raise TypeError("a display rendering needs one established text string")
    uncertain = sorted(uncertain or [], key=lambda span: span.start)
    _refuse_overlaps(text, uncertain)
    gaps = sorted(gaps or [], key=lambda gap: gap.position)
    for gap in gaps:
        if gap.position > len(text):
            raise ValueError(f"gap {gap} points past the end of the text")
        for span in uncertain:
            if span.start < gap.position < span.end:
                raise ValueError(
                    f"gap at {gap.position} falls inside uncertain span "
                    f"[{span.start}, {span.end}); a position cannot be both "
                    "present-and-doubted and gone"
                )

    pieces: list[str] = []
    cursor = 0
    span_index = gap_index = 0
    while cursor < len(text) or span_index < len(uncertain) or gap_index < len(gaps):
        if gap_index < len(gaps) and gaps[gap_index].position == cursor:
            pieces.append(_render_gap_marker(gaps[gap_index]))
            gap_index += 1
            continue
        if span_index < len(uncertain) and uncertain[span_index].start == cursor:
            span = uncertain[span_index]
            pieces.append(_render_uncertain_marker(text[span.start : span.end], span))
            cursor = span.end
            span_index += 1
            continue
        target = len(text)
        if gap_index < len(gaps):
            target = min(target, gaps[gap_index].position)
        if span_index < len(uncertain):
            target = min(target, uncertain[span_index].start)
        pieces.append(_escape_literal(text[cursor:target]))
        cursor = target
    return "".join(pieces)


def strip_display(rendered: str) -> str:
    """Invert `render_display`: drop every marker, keep only the established text.

    Works on the rendered string alone rather than on the spans that produced it, so
    the round trip is a real syntactic property of the markup and not a bookkeeping
    exercise that trusts the caller's own spans back.
    """
    if not isinstance(rendered, str):
        raise TypeError("a display stripping operation needs one string")
    pieces: list[str] = []
    cursor = 0
    while cursor < len(rendered):
        openings = [
            (position, opening, closing, kind)
            for opening, closing, kind in (
                (_UNCERTAIN_OPEN, _UNCERTAIN_CLOSE, "uncertain"),
                (_GAP_OPEN, _GAP_CLOSE, "gap"),
            )
            if (position := rendered.find(opening, cursor)) != -1
        ]
        if not openings:
            pieces.append(_unescape_literal(rendered[cursor:]))
            break
        position, opening, closing, kind = min(openings, key=lambda item: item[0])
        pieces.append(_unescape_literal(rendered[cursor:position]))
        end = rendered.find(closing, position + len(opening))
        if end == -1:
            raise ValueError(f"an opened {kind} display marker is not closed")
        encoded = rendered[position + len(opening) : end]
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError(f"a {kind} display marker is not valid JSON") from error
        if kind == "uncertain":
            if (
                not isinstance(payload, dict)
                or set(payload) != {"alternatives", "text"}
                or not isinstance(payload["text"], str)
                or not isinstance(payload["alternatives"], list)
                or any(not isinstance(value, str) for value in payload["alternatives"])
            ):
                raise ValueError("an uncertain display marker has an invalid field set")
            pieces.append(payload["text"])
        else:
            if (
                not isinstance(payload, dict)
                or set(payload) != {"extent_note", "kind", "witness_variants"}
                or payload["kind"] not in GAP_KINDS
                or not isinstance(payload["extent_note"], str)
                or not isinstance(payload["witness_variants"], list)
                or any(not isinstance(value, str) for value in payload["witness_variants"])
            ):
                raise ValueError("a gap display marker has an invalid field set")
        cursor = end + len(closing)
    return "".join(pieces)


def _render_gap_marker(gap: GapAnchor) -> str:
    payload = {
        "extent_note": gap.extent_note,
        "kind": gap.kind,
        "witness_variants": list(gap.witness_variants),
    }
    return _GAP_OPEN + json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + _GAP_CLOSE


def _render_uncertain_marker(body: str, span: UncertainSpan) -> str:
    payload = {"alternatives": list(span.alternatives), "text": body}
    return (
        _UNCERTAIN_OPEN
        + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        + _UNCERTAIN_CLOSE
    )


def _escape_literal(value: str) -> str:
    """Keep literal marker glyphs distinguishable from generated markup."""
    escaped = value.replace("\\", "\\\\")
    for marker, replacement in _ESCAPED_MARKERS.items():
        escaped = escaped.replace(marker, replacement)
    return escaped


def _unescape_literal(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    reverse = {escape[1:]: marker for marker, escape in _ESCAPED_MARKERS.items()}
    while cursor < len(value):
        if value[cursor] != "\\":
            pieces.append(value[cursor])
            cursor += 1
            continue
        if cursor + 1 >= len(value):
            raise ValueError("a display literal ends with an incomplete escape")
        if value[cursor + 1] == "\\":
            pieces.append("\\")
            cursor += 2
            continue
        escape = value[cursor + 1 : cursor + 6]
        marker = reverse.get(escape)
        if marker is None:
            raise ValueError("a display literal carries an unknown escape")
        pieces.append(marker)
        cursor += 6
    return "".join(pieces)


def _refuse_overlaps(text: str, spans: list[UncertainSpan]) -> None:
    previous_end = 0
    for span in spans:
        if span.start < previous_end:
            raise ValueError("uncertain spans overlap; each character belongs to at most one span")
        if span.end > len(text):
            raise ValueError(f"uncertain span {span} extends past the end of the text")
        previous_end = span.end
