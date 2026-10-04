"""The reader's doubt marks: `[[?]]` where sight failed, `[[reading|other]]` where it doubted.

A reader of the export is shown the diplomatic text with brackets only where the ink
is doubtful: `[illegible]` for a gap and `[reading?]` for a doubtful reading
(`diplomatic_display`, `bracket_doubt_marks`).

The Perlector writes its doubts inline, and this module splits a marked reading into
the established text and a doubt report anchored to it (`read_doubt_marks`), writes
the marks back (`render_doubt_marks`), and maps offsets between the two forms
(`doubt_mark_offsets`). A gap it records is zero-width and carries no witness
evidence, so the text never holds characters standing in for ink the reader could
not see. The schema every stored doubt layer must meet, that zero-width rule
included, is `common.contracts.uncertainty.validate`.
"""

from __future__ import annotations

import re
from typing import Any, Final

from common.contracts.errors import SchemaRefusal
from common.contracts.uncertainty import PAGE_READ_LECTIO, is_trailing_offset, validate

# The reader's own doubt report. `assessed`: the reader was asked and its spans and
# gaps anchor to the text. `malformed`: a report that could not be anchored, kept as
# a visible fault with empty layers. The contract's third state, `not-assessed`,
# marks a person's correction (`common.contracts.uncertainty.corrected_layer`),
# whose empty layers are an absence, not confidence; `read_doubt_marks` never
# reports it.
ASSESSMENT_ASSESSED: Final = "assessed"
ASSESSMENT_MALFORMED: Final = "malformed"
_REPORT_FIELDS: Final = frozenset({"state", "uncertain_spans", "gaps", "problem"})


def malformed_assessment(problem: str) -> dict[str, Any]:
    """A doubt report that could not be anchored: retained as a fault, layers empty."""
    return {
        "state": ASSESSMENT_MALFORMED,
        "uncertain_spans": [],
        "gaps": [],
        "problem": problem,
    }


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
        if not reading or reading == "?" or "" in alternatives:
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
            if is_trailing_offset(text, offset)
            else "internal",
            "start": offset,
            "end": offset,
            "witness_evidence": [],
        }
        for offset in gap_offsets
        if text.strip()
    ]
    return text, {
        "state": ASSESSMENT_ASSESSED,
        "uncertain_spans": spans,
        "gaps": gaps,
        "problem": None,
    }


def render_doubt_marks(clean_text: str, uncertainty: dict[str, Any]) -> str:
    """The marked reading `read_doubt_marks` splits into `clean_text` and `uncertainty`.

    Each uncertain span becomes `[[reading|other|...]]` and each gap `[[?]]`; a gap at
    the offset where a span starts is written before it. A report that is not
    `assessed` carries the reading exactly as returned, so its text is the marked
    reading. A gap in a reading with no text is not recorded by `read_doubt_marks`,
    so there is nothing to render back for it.

    The report is refused unless it is the closed four-field record and its layers
    meet `common.contracts.uncertainty.validate` over `clean_text`; a report no
    marked reading reads back to, such as overlapping spans or a gap carrying
    witness evidence, is refused too.
    """
    if not isinstance(uncertainty, dict) or set(uncertainty) != _REPORT_FIELDS:
        raise SchemaRefusal("a doubt report is not its closed record")
    validate(
        {
            "uncertain_spans": uncertainty["uncertain_spans"],
            "gaps": uncertainty["gaps"],
            "self_revisions": None,
            "assessment": {"state": uncertainty["state"], "problem": uncertainty["problem"]},
            "lectio_kind": PAGE_READ_LECTIO,
        },
        clean_text,
    )
    if uncertainty["state"] != ASSESSMENT_ASSESSED:
        return clean_text
    # (offset, 0 for a gap or 1 for a span, tiebreak, mark): gaps first at a shared offset.
    inserts: list[tuple[int, int, int, str]] = [
        (gap["start"], 0, index, ILLEGIBLE_MARK) for index, gap in enumerate(uncertainty["gaps"])
    ]
    for span in uncertainty["uncertain_spans"]:
        readings = [clean_text[span["start"] : span["end"]], *span["alternatives"]]
        inserts.append((span["start"], 1, span["end"], "[[" + "|".join(readings) + "]]"))
    pieces: list[str] = []
    cursor = 0
    for start, kind, end, mark in sorted(inserts):
        if start < cursor:
            raise SchemaRefusal("the doubt report's marks overlap, so no marked reading holds them")
        pieces.append(clean_text[cursor:start])
        pieces.append(mark)
        cursor = end if kind else start
    pieces.append(clean_text[cursor:])
    rendered = "".join(pieces)
    if read_doubt_marks(rendered) != (clean_text, uncertainty):
        raise SchemaRefusal("the doubt report cannot be written as marks that read back to it")
    return rendered


ILLEGIBLE_DISPLAY: Final = "[illegible]"


def _doubtful_display(reading: str) -> str:
    return f"[{reading}?]"


def diplomatic_display(text: str, layer: dict[str, Any]) -> str:
    """The established `text` as a reader is shown it, from its canonical uncertainty layer.

    Each gap is `[illegible]` and each uncertain span `[reading?]`; a gap at the
    offset where a span starts comes first. Only a reader's assessed report marks
    doubt: a person's correction, or a report that could not be anchored, shows the
    text as it is. Overlapping marks are refused, since no one bracketing holds them.
    """
    if layer["assessment"]["state"] != ASSESSMENT_ASSESSED:
        return text
    inserts = [(gap["start"], 0, gap["start"], ILLEGIBLE_DISPLAY) for gap in layer["gaps"]]
    inserts += [
        (span["start"], 1, span["end"], _doubtful_display(text[span["start"] : span["end"]]))
        for span in layer["uncertain_spans"]
    ]
    pieces: list[str] = []
    cursor = 0
    for start, _kind, end, shown in sorted(inserts):
        if start < cursor:
            raise SchemaRefusal("the doubt layer's marks overlap, so no bracketing shows them")
        pieces += [text[cursor:start], shown]
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def bracket_doubt_marks(raw: str) -> str:
    """A marked reading as a reader is shown it: `[[?]]` as `[illegible]`, `[[a|b]]` as `[a?]`."""
    return _MARK.sub(
        lambda match: (
            ILLEGIBLE_DISPLAY
            if match.group(1) == "?"
            else _doubtful_display(match.group(1).split("|")[0])
        ),
        raw,
    )


BASIS_POINTS: Final = 10_000


def doubt_count(text: str, layer: dict[str, Any]) -> tuple[int, int]:
    """`(doubtful or unread, out of)` for one reading, from its spans and gaps.

    Counted in the text's non-whitespace characters: each one inside an uncertain
    span is doubtful. A gap is zero-width, so how much ink it stands for was never
    measured; each counts as one unread character, on both sides of the share.
    """
    doubtful = {
        offset
        for span in layer["uncertain_spans"]
        for offset in range(span["start"], span["end"])
        if not text[offset].isspace()
    }
    gaps = len(layer["gaps"])
    read = sum(1 for character in text if not character.isspace())
    return len(doubtful) + gaps, read + gaps


def doubt_exceeds(count: tuple[int, int], limit_bp: int) -> bool:
    """Whether more than `limit_bp` basis points of `count` are doubtful or unread.

    Compared exactly, in integers; a reading with nothing read counts as all unread.
    """
    doubtful, out_of = count
    return out_of == 0 or doubtful * BASIS_POINTS > limit_bp * out_of


def doubt_mark_offsets(raw: str) -> tuple[list[int | None], list[int | None]]:
    """`(raw_to_clean, clean_to_raw)`: where each offset of a marked reading lands.

    `raw_to_clean[i]` is the offset in the text `read_doubt_marks(raw)` returns that
    raw offset `i` stands at, and `None` strictly inside a mark, where no clean offset
    corresponds exactly. A mark's two ends map to the ends of its reading, so `[[?]]`
    maps to one zero-width offset. `clean_to_raw[c]` is the first raw offset that maps
    to `c`, `None` inside the reading of a mark. A reading whose marks do not parse is
    published as returned, so both maps are the identity.
    """
    text, assessment = read_doubt_marks(raw)
    if assessment["state"] != ASSESSMENT_ASSESSED:
        identity: list[int | None] = list(range(len(raw) + 1))
        return identity, list(identity)
    raw_to_clean: list[int | None] = []
    length = cursor = 0
    for match in _MARK.finditer(raw):
        for _offset in range(cursor, match.start()):
            raw_to_clean.append(length)
            length += 1
        raw_to_clean.append(length)
        raw_to_clean.extend([None] * (match.end() - match.start() - 1))
        body = match.group(1)
        length += 0 if body == "?" else len(body.split("|")[0])
        cursor = match.end()
    for _offset in range(cursor, len(raw)):
        raw_to_clean.append(length)
        length += 1
    raw_to_clean.append(length)
    clean_to_raw: list[int | None] = [None] * (len(text) + 1)
    for index in reversed(range(len(raw_to_clean))):
        if (clean := raw_to_clean[index]) is not None:
            clean_to_raw[clean] = index
    return raw_to_clean, clean_to_raw
