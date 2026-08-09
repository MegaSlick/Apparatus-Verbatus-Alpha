"""The proposed uncertainty/gap display convention. A proposal, not a decision.

Spec 11 asks the text bundle to project "uncertainty spans and gaps ... as display
conventions (EpiDoc-style rendering *proposed* via the courtroom note's research;
Tyrel picks the convention at this gate), never altering the stored text", and its
test 2 asks that "rendered displays [be] tested separately by render -> strip ->
hash". So a rendering has to exist for him to pick against, and a way back from it
has to exist for the identity test to mean anything. This module is both, and
nothing more: **it is a proposal awaiting his choice, and the choice changes only
this file.** No hash, no stored field, and no format writer depends on which
brackets are used.

**The split it adopts is EpiDoc's, and it is a semantic one rather than a
typographic one.** EpiDoc distinguishes `<unclear>` -- ink that is present but
doubted -- from `<gap>` -- ink that is simply gone; a reconstruction attributed to
someone else is `<supplied>` and sits outside the established text. That is exactly
the line Tyrel drew on 2026-08-05: a gap carries its evidence beside the text and
never characters inside it, and "we don't want it making shit up". The markers below
are plain text rather than literal XML because the near-term readers are a text file
and a terminal, not an XML toolchain. The two bracket pairs are chosen to be
vanishingly unlikely in transcribed parish-register text.

**What is not exercised against real data, said rather than left to be found.** The
Archetypus record this stage reads carries one `text` field and no uncertainty or gap
layer yet, so every run this repository can produce renders plain established text
with no spans at all and `render_display` is the identity function on it. The round
trip below is exercised only against spans built by hand in this module's tests. That
is a real gap: the pair is ready for the annotation layer the day it lands, and
nothing here has yet run against a real gap or a real uncertain span.
"""

import re
from dataclasses import dataclass, field
from typing import Final

# The name the EXPORT_MANIFEST reports, so a reader of the product can tell which
# convention produced a rendering without reading this file. It says "proposed"
# because it is: spec 11 leaves the choice to Tyrel at this gate.
DISPLAY_CONVENTION: Final = "epidoc-semantics-plaintext-markers.proposed.v1"

GAP_KINDS: Final = frozenset({"leading", "internal", "trailing", "whole-act"})

_UNCERTAIN_OPEN, _UNCERTAIN_CLOSE = "⟨", "⟩"
_GAP_OPEN, _GAP_CLOSE = "⟦", "⟧"
_ALT_SEPARATOR = ";"
_FIELD_SEPARATOR = "|"

_UNCERTAIN_PATTERN = re.compile(
    re.escape(_UNCERTAIN_OPEN) + r"(.*?)" + re.escape(_UNCERTAIN_CLOSE), re.DOTALL
)
_GAP_PATTERN = re.compile(re.escape(_GAP_OPEN) + r".*?" + re.escape(_GAP_CLOSE), re.DOTALL)


@dataclass(frozen=True)
class UncertainSpan:
    """Ink present but doubted. `start`/`end` index into the established text."""

    start: int
    end: int
    alternatives: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"uncertain span [{self.start}, {self.end}) is not a valid range")


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
        if self.position < 0:
            raise ValueError(f"gap position {self.position} is negative")


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
        pieces.append(text[cursor:target])
        cursor = target
    return "".join(pieces)


def strip_display(rendered: str) -> str:
    """Invert `render_display`: drop every marker, keep only the established text.

    Works on the rendered string alone rather than on the spans that produced it, so
    the round trip is a real syntactic property of the markup and not a bookkeeping
    exercise that trusts the caller's own spans back.
    """
    without_gaps = _GAP_PATTERN.sub("", rendered)

    def _keep_body(match: re.Match) -> str:
        inner = match.group(1)
        field_at = inner.find(_FIELD_SEPARATOR)
        return inner if field_at == -1 else inner[:field_at]

    return _UNCERTAIN_PATTERN.sub(_keep_body, without_gaps)


def _render_gap_marker(gap: GapAnchor) -> str:
    fields = [gap.kind]
    if gap.extent_note:
        fields.append(gap.extent_note)
    marker = f"{_GAP_OPEN}{_FIELD_SEPARATOR.join(fields)}"
    if gap.witness_variants:
        marker += f"{_FIELD_SEPARATOR}{_ALT_SEPARATOR.join(gap.witness_variants)}"
    return marker + _GAP_CLOSE


def _render_uncertain_marker(body: str, span: UncertainSpan) -> str:
    if span.alternatives:
        alternatives = _ALT_SEPARATOR.join(span.alternatives)
        return f"{_UNCERTAIN_OPEN}{body}{_FIELD_SEPARATOR}{alternatives}{_UNCERTAIN_CLOSE}"
    return f"{_UNCERTAIN_OPEN}{body}{_UNCERTAIN_CLOSE}"


def _refuse_overlaps(text: str, spans: list[UncertainSpan]) -> None:
    previous_end = 0
    for span in spans:
        if span.start < previous_end:
            raise ValueError("uncertain spans overlap; each character belongs to at most one span")
        if span.end > len(text):
            raise ValueError(f"uncertain span {span} extends past the end of the text")
        previous_end = span.end
