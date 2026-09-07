"""Churro's own request framing and answer grammar, re-expressed over stdlib.

Churro (`attestator_3`, adapter `churro.v1`) is asked in the vendor's own
framing and answers in the vendor's own grammar.  This module holds both ends
of that: the exact system string the vendor resolves for
`stanford-oval/churro-3B`, per prompt variant and with its digest, and a parser
for the `HistoricalDocument` XML that string asks for.  It is deliberately
`common/`-level and imports nothing from `pipeline/` or `operations/`: the
adapter that sends the prompt and the contract that closes the retained capture
are different stages, and neither may own the other's bytes.

## The carried strings

Two vendor-attested framings exist for this one model id, and both are
system-only -- no user text at all, the image being the whole user turn:

* `registry-v0.3.0` -- `CHURRO_3B_XML_TEMPLATE.system_message` from
  `src/churro_ocr/templates/presets.py` in `github.com/stanford-oval/Churro` at
  tag `v0.3.0` (`4abb17386d9656199c2776195926545fc527a691`), which
  `providers/specs.py::resolve_ocr_profile` returns for this model id, with
  `user_prompt=None`.
* `paper-harness-ed09bc7` -- the module constant `SYSTEM_MESSAGE` at
  `ocr/systems/finetuned_ocr.py:17` in the paper-era release
  `ed09bc7fd6475c333a25427f3d0b9227af46ce27` (short `ed09bc7fd6`, which is what
  the variant is named after), read at line 33 by `FineTunedOCR`, the class the
  benchmark harness ran churro-3B through, with `user_message_text=None`.
  Its two spelling errors ("entiretly", "documents") are part of the bytes that
  harness actually sent and are carried unaltered.  The same release's CLI
  default at `run_churro_ocr.py:230` spells both correctly, which is why these
  are two named variants rather than one string with a typo nobody can
  attribute: which of them the fine-tuning itself saw is not stated anywhere in
  the paper, the model card, or the code, and this module does not guess.

The Churro *code* is Apache-2.0, so carrying these strings with this
attribution is permitted; the weights are under the Qwen research licence at
the pinned revision and are never vendored.  Nothing here reads the network.
The digests recorded beside each string are what an on-demand, network-gated
parity test diffs against the pinned raw files; the assertion inside this
repository is the offline one in `common/test_churro_document.py`, which
re-digests the constants so an edit here cannot pass silently.

**No default variant is named here.**  Which variant a chair sends is a
configuration fact written onto the Testimonium as `prompt_variant`; a default
in this module would be a second place claiming to answer that, and the two
would drift.

## The grammar

`HistoricalDocument` is the XML the vendor's own guide
(`docs/guides/historical-document-xml.md`) and XSD
(`evaluation/historical_doc.xsd` at `ed09bc7fd6`) specify.  Elements are matched
by *local* name, because the vendor's own worked example carries a default
namespace and its own extractor strips the prefix before comparing.

`parse_churro_document` returns one of three parse states, and no other:

* `parsed` -- with `shape` naming which of the three legal answer shapes was
  read: `historical-document`, `plain-text`, or `output-element`.
* `unrecognized-shape` -- well-formed XML this parser can name no shape for.
  The parser ran and read the whole response; it is not a parse failure, and
  the bytes stay retained under their own digest.
* `failed` -- the bytes could not be read as the response they were offered as.

`output-element` is Churro's *retired* framing: a bare `<output>text</output>`
envelope, which the prompt this pipeline now sends never asks for.  It is
accepted so that retained history still parses, and it carries the finding
`retired-output-envelope` so that a live response arriving in a shape nobody
asked for is visible rather than silent (GOVERNANCE 2).

## Departures from the vendor's own flattener, and why each

`evaluation/xml_utils.py::extract_actual_text_from_xml` is the vendor's
reading-order flattener.  Five of its behaviours are deliberately not
reproduced.  Each departure is toward keeping evidence, never toward changing a
reading.

1. **Marked-up text is kept, not deleted.**  The vendor removes `Description`,
   `Deletion`, `Illegible` and `Gap` elements *with their contents* by regex
   before parsing.  Deleting a struck-out or damaged word from the reading
   loses ink this project exists to capture (GOALS 1).  Every one of the six
   markup kinds in `MARKED_SPAN_KINDS` is instead kept as ordinary text, and
   the code-point range it occupies in the returned text is recorded in
   `marked_spans`, so a consumer that wants the vendor's flattening can
   reproduce it exactly by deleting those ranges, and one that wants the ink
   has it.  `Illegible` and `Gap` are empty elements in the XSD, so their spans
   are zero-length markers at the point they occurred.
2. **A `Line` element is one line; the vendor makes every text node one.**  The
   vendor calls `itertext()` and strips each text node into its own line, which
   breaks a line wherever inline markup appears -- `Le <Addition>dit</Addition>
   jour` becomes three lines in the vendor's output and one here.  Reading
   order is what the model was fine-tuned to produce (paper §3, "a single text
   string per page in correct reading order"), and splitting a line at its
   markup is not that order.  Text that is *not* inside a `Line` (a
   `PageNumber`, a `CatchWord`, a `Formula`'s mixed content) still becomes its
   own line, so nothing in a walked section is dropped.
3. **Structural repair is refused; a lossless escape is not repair.**  The
   vendor does two separable things before it reads, and only one of them is
   refused here.  It escapes stray `&`, `<` and `>` outside its known tag list,
   which loses nothing -- those characters come back out of the parser as the
   characters the model wrote; and it parses with `recover=True`, so a broken
   response yields a partial reading that no record distinguishes from a whole
   one, returning `""` on an outright parse error, which is a silent empty
   reading.  The `recover=True` half is refused.  The escape is kept, narrowed,
   and counted: a response that offers the grammar and does not parse is
   escaped once and re-parsed, only `<` and `&` that cannot open markup are
   touched (an `&amp;` the model wrote correctly is left alone, where the
   vendor would escape it a second time), and a response that parses on the
   first attempt is never escaped at all.  Where the escape is what made a
   response readable, the finding `stray-markup-escaped` counts the characters,
   because `&c.` is a routine abbreviation in these registers and refusing a
   whole page over one ampersand loses ink this project exists to capture
   (GOALS 1).  Everything structural is still refused: nothing here reorders,
   trims, closes, or defaults a malformed answer, and a response that still
   will not parse is `failed` with the parser's own reason for what survived
   the escape and the count of what it escaped, its bytes retained under their
   digest for a later re-parse.
4. **No `<lb/>`/`<br>` scrubbing pass.**  The vendor regexes those out of the
   extracted text.  This parser rewrites no characters of the response beyond
   the whitespace collapsing named below and the counted escape in departure 3,
   so a stray tag survives as the text it is rather than being silently
   removed.
5. **Each section is walked once, by its nearest owner.**  The scope itself is
   the vendor's: `Header`, `Body` and `Footer` are matched among a `Page`'s
   *descendants* (`page.xpath(".//Header")` and its two siblings,
   `xml_utils.py:104`), never among its direct children, so a page that wraps
   its sections in an element the grammar does not name still reads instead of
   coming back empty under state `parsed`.  Two bounds the vendor does not
   draw: a section inside a nested `Page` belongs to that page's own walk, and
   a section inside another section is walked by the section enclosing it.  The
   vendor's `.//` queries emit each of those twice, and a doubled reading is
   ink the response never wrote (GOALS 2).  Text a `Page` carries outside every
   section of its own is outside the vendor's transcription and stays outside
   it -- but the page says so, through the finding
   `page-text-outside-sections` and its page ordinal, so ink the response put
   out of the walk's reach is visible rather than simply absent (GOVERNANCE 2).

Kept from the vendor unchanged: the walk scope (`Page` descendants, then their
`Header`, `Body` and `Footer` descendants in that fixed order regardless of the
order they appear in, bounded as departure 5 says), the `"\\n"` join between a
page's sections and the `"\\n\\n"` join between pages, `Metadata` being outside
the transcription (its `Language` and `Script` are not ink, and they remain in
the retained bytes), and
`trim_leading_prompt` (`run_churro_ocr.py`), reproduced exactly including its
`lstrip("\\n ")`, which strips newlines and spaces and nothing else.

**Whitespace inside a line is collapsed.**  A `HistoricalDocument` answer is
indented XML, so the raw concatenation of a `Line`'s descendant text carries
the pretty-printer's newlines and indentation.  Runs of whitespace inside a
line become one space and the line's ends are trimmed; a boundary with no
whitespace at all in the source stays joined with no space, because in XML that
is what it means.  Whitespace here is *ASCII* whitespace and nothing else: a
non-breaking space, or any other Unicode space a model writes, is a character
it wrote and stays exactly where it is.  Plain-text and `output-element`
answers are returned exactly
as they decoded, with no collapsing at all -- there is no markup there whose
serialisation could have introduced whitespace the model did not write.

## The byte ceiling lives with its owner

This module applies no response-size limit of its own unless one is passed.
`common/native_witness.py::CHURRO_MAX_RESPONSE_BYTES` is the single ceiling and
`derive_churro_capture` applies it before any parser is called.  Restating the
constant here would be a second copy free to drift, and importing it would put
a cycle between this module and the contract that dispatches to it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Final

from common.contracts.errors import SchemaRefusal


class _DocumentTooDeep(Exception):
    """Raised inside the flattener; `parse_churro_document` turns it into `failed`."""


# The named rule that projects retained Churro bytes into the `payload` text.
# One rule, three shapes: whichever shape the response took, this is the view
# whose name a record carries.
CHURRO_TEXT_VIEW: Final = "churro-historical-document-text.v1"
# The parser word for this adapter, as `feeding._RUNNABLE_PARSERS` and the
# capture contract spell it.
CHURRO_PARSER: Final = "xml"

DOCUMENT_ROOT_ELEMENT: Final = "HistoricalDocument"
OUTPUT_ROOT_ELEMENT: Final = "output"
PAGE_ELEMENT: Final = "Page"
LINE_ELEMENT: Final = "Line"
# The vendor walks exactly these three, in exactly this order, whatever order
# they appear in inside a `Page`.
PAGE_SECTIONS: Final = ("Header", "Body", "Footer")
# The XSD's inline and block markup this parser keeps as text and indexes by
# span.  `Illegible` and `Gap` are empty elements; the other four carry text.
MARKED_SPAN_KINDS: Final = frozenset(
    {"Addition", "Deletion", "Illegible", "Gap", "InterlinearNote", "Description"}
)

DOCUMENT_SHAPES: Final = frozenset({"historical-document", "plain-text", "output-element"})
# How deep a response's element nesting may be before the walk refuses it.
# `xml.etree`'s own parser is iterative and will happily build a tree thousands
# of elements deep; this flattener is recursive, so without a declared bound a
# deeply nested answer would exhaust the interpreter stack and leave a
# `RecursionError` where a named `failed` record belongs (GOVERNANCE 2). The
# grammar's own deepest legal path -- HistoricalDocument > Page > Body >
# RecordEntry > List > Item > Line > Above > Emphasis -- is nine.
_MAX_DOCUMENT_DEPTH: Final = 256
# Whitespace, for the purpose of collapsing an indented answer, means ASCII
# whitespace and nothing else. A non-breaking space or any other Unicode space
# a model writes is a character it wrote, and this parser does not rewrite it
# into a plain space (GOALS 2).
_ASCII_WHITESPACE: Final = " \t\n\r\f\v"
_WHITESPACE_RUN: Final = re.compile(f"[{re.escape(_ASCII_WHITESPACE)}]+")
# What this parser can conclude.  `not-requested` and `pending` are states of
# the caller's capture record, never of a parse that ran.
PARSE_STATES: Final = frozenset({"parsed", "failed", "unrecognized-shape"})
DOCUMENT_FINDING_KINDS: Final = frozenset(
    {
        "prompt-echo-trimmed",
        "retired-output-envelope",
        "stray-markup-escaped",
        "page-text-outside-sections",
    }
)
# The findings that count characters rather than name a place.
_COUNTED_FINDING_KINDS: Final = frozenset({"prompt-echo-trimmed", "stray-markup-escaped"})

# A `&` that opens one of XML's own references is markup the response meant; any
# other `&` is a character it wrote, and only those are escaped (departure 3).
# A `<` outside a markup token can never be markup, so every one of those is.
_XML_REFERENCE_BODY: Final = r"(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z_:][A-Za-z0-9_.:-]*);"
_STRAY_MARKUP: Final = re.compile(f"<|&(?!{_XML_REFERENCE_BODY})")
# The markup an escape pass steps over rather than into: a CDATA section, a
# comment, a declaration or processing instruction, and a tag.  A tag's
# attribute values may not carry `<` or `>` unescaped, so a token never spans
# one.
_MARKUP_TOKEN: Final = re.compile(
    r"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<[!?][^>]*>|</?[A-Za-z_:][^<>]*>",
    re.DOTALL,
)

CHURRO_PROMPT_VARIANTS: Final = {
    "registry-v0.3.0": {
        "system": "Transcribe the entirety of this historical document to XML format.",
        "user": None,
        "system_sha256": "13592f5580805cf12d2aa14c963b872e3a4e6a5834e5d2afd7b86effd42a8b4d",
        "repository": "github.com/stanford-oval/Churro",
        "commit": "4abb17386d9656199c2776195926545fc527a691",
        "path": "src/churro_ocr/templates/presets.py",
        "symbol": "CHURRO_3B_XML_TEMPLATE.system_message",
        "licence": "Apache-2.0",
    },
    "paper-harness-ed09bc7": {
        "system": "Transcribe the entiretly of this historical documents to XML format.",
        "user": None,
        "system_sha256": "dd7408ca72cf94f724b0522806427533a746f08cfa0cfd047e0272ebc4c4b489",
        "repository": "github.com/stanford-oval/Churro",
        "commit": "ed09bc7fd6475c333a25427f3d0b9227af46ce27",
        "path": "ocr/systems/finetuned_ocr.py",
        "symbol": "SYSTEM_MESSAGE",
        "licence": "Apache-2.0",
    },
}
_PROVENANCE_FIELDS: Final = ("repository", "commit", "path", "symbol", "licence", "system_sha256")


def _variant(name: Any) -> dict[str, Any]:
    if not isinstance(name, str) or name not in CHURRO_PROMPT_VARIANTS:
        known = ", ".join(sorted(CHURRO_PROMPT_VARIANTS))
        raise SchemaRefusal(
            f"unknown Churro prompt variant {name!r}; the vendor-attested variants are {known}"
        )
    return CHURRO_PROMPT_VARIANTS[name]


def churro_system_prompt(variant: str) -> str:
    """The carried system string for one vendor-attested prompt variant."""
    return _variant(variant)["system"]


def churro_prompt_view(variant: str) -> dict[str, str]:
    """The retained prompt view for a Churro request: a system turn, and nothing else.

    Both attested framings set the user prompt to `None`, so the user turn
    carries the image alone.  Returning `{"system": ...}` rather than
    `{"system": ..., "user": ""}` is the difference between recording that no
    user text was sent and recording that empty user text was.
    """
    return {"system": _variant(variant)["system"]}


def churro_prompt_provenance(variant: str) -> dict[str, Any]:
    """The vendor code identity to record beside the model identity (GOVERNANCE 6)."""
    entry = _variant(variant)
    provenance: dict[str, Any] = {"prompt_variant": variant}
    provenance.update({field: entry[field] for field in _PROVENANCE_FIELDS})
    return provenance


def trim_leading_prompt(text: str, prompt: str | None) -> str:
    """Remove a duplicated leading prompt, exactly as the vendor's harness does.

    Reproduced from `run_churro_ocr.py::trim_leading_prompt` at `ed09bc7fd6`,
    including the `lstrip("\\n ")` that follows it: newlines and spaces are
    removed after the prompt, and no other whitespace is -- a leading tab or
    carriage return survives, because that is what the vendor's own harness
    left in place.

    Only the string a request actually sent may be trimmed.  Trimming against a
    framing that was not sent could remove real transcription that happens to
    begin the same way, which is why the caller passes the prompt rather than
    this module trying every variant it knows.
    """
    if not isinstance(text, str):
        raise SchemaRefusal("a Churro response body is not text")
    if prompt is not None and not isinstance(prompt, str):
        raise SchemaRefusal("a Churro system prompt is not text")
    if prompt and text.startswith(prompt):
        return text[len(prompt) :].lstrip("\n ")
    return text


def _local(tag: object) -> str:
    """The local name of an element tag, namespace prefix removed.

    Comments and processing instructions carry a callable tag rather than a
    string; they can never be a grammar element, so they resolve to no name.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rpartition("}")[2]


class _TextBuilder:
    """Accumulates the flattened reading and answers offsets into it.

    Separators are pending until a character actually follows them, so a
    section or page that contributes nothing leaves no dangling newline, and a
    span opened before a pending separator starts after it rather than
    swallowing it.
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self.length = 0
        self._pending_break = 0
        self._pending_space = False
        self._marks: list[dict[str, int | None]] = []

    def _emit(self, text: str) -> None:
        self._chunks.append(text)
        self.length += len(text)

    def _flush(self) -> None:
        if self.length == 0:
            # Nothing to separate from: leading whitespace never becomes text.
            self._pending_break = 0
            self._pending_space = False
            return
        if self._pending_break:
            self._emit("\n" * self._pending_break)
            self._pending_break = 0
        elif self._pending_space:
            self._emit(" ")
        self._pending_space = False

    def _open_marks(self) -> None:
        for mark in self._marks:
            if mark["start"] is None:
                mark["start"] = self.length

    def add(self, text: str) -> None:
        """Append text, collapsing every run of ASCII whitespace to one space."""
        if not text:
            return
        runs = [run for run in _WHITESPACE_RUN.split(text) if run]
        if not runs:
            if self.length:
                self._pending_space = True
            return
        if text[0] in _ASCII_WHITESPACE:
            self._pending_space = True
        for index, run in enumerate(runs):
            if index:
                self._pending_space = True
            self._flush()
            self._open_marks()
            self._emit(run)
        if text[-1] in _ASCII_WHITESPACE:
            self._pending_space = True

    def break_line(self, count: int = 1) -> None:
        """Ask for `count` newlines before the next character, if any follows."""
        if self.length:
            self._pending_break = max(self._pending_break, count)
        self._pending_space = False

    def begin_mark(self) -> dict[str, int | None]:
        mark: dict[str, int | None] = {"start": None}
        self._marks.append(mark)
        return mark

    def end_mark(self, mark: dict[str, int | None]) -> tuple[int, int]:
        # Marks nest strictly, so the one being closed is the one on top.
        # Removing by equality instead would take the first *equal* record --
        # and two marks that have not emitted anything yet are equal -- which
        # would leave the wrong cell open to receive the next start offset.
        if not self._marks or self._marks[-1] is not mark:
            raise SchemaRefusal("a Churro markup span was closed out of order")
        self._marks.pop()
        start = mark["start"]
        # An element that emitted nothing is a zero-length marker where it
        # occurred, not a span over its neighbours' text.
        return (self.length, self.length) if start is None else (start, self.length)

    def text(self) -> str:
        return "".join(self._chunks)


def _add_text(builder: _TextBuilder, text: str | None, *, inside_line: bool) -> None:
    """Add one text node; outside a `Line`, a non-blank node is its own line.

    Inside a line, a whitespace-only node is handed to the builder rather than
    skipped: it is the separator between two inline elements, and dropping it
    would join `<Addition>a</Addition> <Deletion>b</Deletion>` into one word.
    Outside a line it is only indentation between blocks, which the newline
    between them already carries.
    """
    if not text:
        return
    if inside_line:
        builder.add(text)
        return
    if not text.strip(_ASCII_WHITESPACE):
        return
    builder.break_line(1)
    builder.add(text)
    builder.break_line(1)


def _walk(
    element: ET.Element,
    builder: _TextBuilder,
    spans: list[dict[str, Any]],
    *,
    inside_line: bool,
    depth: int,
) -> int:
    """Flatten one subtree in document order; return the `Line` count under it."""
    if depth > _MAX_DOCUMENT_DEPTH:
        raise _DocumentTooDeep(f"Churro response nests elements deeper than {_MAX_DOCUMENT_DEPTH}")
    lines = 0
    _add_text(builder, element.text, inside_line=inside_line)
    for child in element:
        name = _local(child.tag)
        # A `Line` inside a `Line` is not in the grammar; if one arrives it
        # continues the line it is in rather than manufacturing a break.
        is_line = name == LINE_ELEMENT and not inside_line
        mark = builder.begin_mark() if name in MARKED_SPAN_KINDS else None
        if is_line:
            builder.break_line(1)
        lines += _walk(child, builder, spans, inside_line=inside_line or is_line, depth=depth + 1)
        if is_line:
            lines += 1
            builder.break_line(1)
        if mark is not None:
            start, end = builder.end_mark(mark)
            spans.append({"kind": name, "start": start, "end": end})
        _add_text(builder, child.tail, inside_line=inside_line)
    return lines


def _page_sections(page: ET.Element) -> tuple[list[tuple[str, ET.Element]], bool]:
    """One `Page`'s own sections in document order, and whether ink lies outside them.

    The vendor asks each page for `.//Header`, `.//Body` and `.//Footer`
    (`xml_utils.py:104`), so a section is a *descendant* of its page and an
    intermediate element the grammar does not name cannot hide one.  Two bounds
    the vendor does not draw are drawn here, and departure 5 says why: a section
    under a nested `Page` is left to that page, and a section under another
    section is left to the section enclosing it, so no element is walked twice.

    The descent is iterative because a response may nest as deeply as it likes;
    only the walk that follows declares a depth bound, and a recursive search
    would have exhausted the stack before reaching it.
    """
    sections: list[tuple[str, ET.Element]] = []
    outside = bool((page.text or "").strip(_ASCII_WHITESPACE))
    stack = list(reversed(page))
    while stack:
        element = stack.pop()
        name = _local(element.tag)
        # A tail belongs to the flow the element sits in, never to the element,
        # so it is outside a section even when the element is one.
        if (element.tail or "").strip(_ASCII_WHITESPACE):
            outside = True
        if name == PAGE_ELEMENT:
            continue
        if name in PAGE_SECTIONS:
            sections.append((name, element))
            continue
        if (element.text or "").strip(_ASCII_WHITESPACE):
            outside = True
        stack.extend(reversed(element))
    return sections, outside


def _flatten_document(root: ET.Element) -> dict[str, Any]:
    """Walk `Page` descendants and their three sections in the vendor's order."""
    builder = _TextBuilder()
    spans: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    pages = [
        element
        for element in root.iter()
        if element is not root and _local(element.tag) == PAGE_ELEMENT
    ]
    for page_index, page in enumerate(pages, start=1):
        if page_index > 1:
            builder.break_line(2)
        owned, outside = _page_sections(page)
        if outside:
            # Text no section of this page encloses is outside the vendor's
            # transcription; saying so is what keeps it from disappearing
            # behind a successful state (GOVERNANCE 2, departure 5).
            findings.append({"kind": "page-text-outside-sections", "page_ordinal": page_index})
        for section_name in PAGE_SECTIONS:
            for section in [element for name, element in owned if name == section_name]:
                builder.break_line(1)
                mark = builder.begin_mark()
                lines = _walk(section, builder, spans, inside_line=False, depth=1)
                start, end = builder.end_mark(mark)
                # `lines` counts the `Line` elements the response actually
                # wrote, which is the fact worth keeping. It is not the number
                # of newlines in the span: a `Line` holding only whitespace
                # emits no text and therefore no break.
                sections.append(
                    {
                        "page_ordinal": page_index,
                        "section": section_name,
                        "span": {"start": start, "end": end},
                        "lines": lines,
                    }
                )
    return {
        "text": builder.text(),
        "sections": sections,
        # Offsets order the index; the six kinds do not nest inside one another
        # in the XSD, so this is the order they were read in as well.
        "marked_spans": sorted(spans, key=lambda span: (span["start"], span["end"], span["kind"])),
        "pages": len(pages),
        "findings": findings,
    }


def _escape_stray_markup(body: str) -> tuple[str, int]:
    """Escape the `<` and `&` that cannot be markup, and count them (departure 3).

    Everything the grammar could have meant as markup -- a tag, a comment, a
    CDATA section, a declaration -- is stepped over whole, and in the text
    between those tokens only a `<` and a `&` that opens no XML reference are
    escaped.  The escape is lossless: the parser hands the same characters back.
    """
    escaped = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal escaped
        escaped += 1
        return "&lt;" if match.group(0) == "<" else "&amp;"

    parts: list[str] = []
    last = 0
    for token in _MARKUP_TOKEN.finditer(body):
        parts.append(_STRAY_MARKUP.sub(_replace, body[last : token.start()]))
        parts.append(token.group(0))
        last = token.end()
    parts.append(_STRAY_MARKUP.sub(_replace, body[last:]))
    return "".join(parts), escaped


def _base(state: str, response_bytes: int, findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "state": state,
        "parser": CHURRO_PARSER,
        "response_bytes": response_bytes,
        "findings": findings,
    }


def _parsed(
    shape: str,
    text: str,
    response_bytes: int,
    findings: list[dict[str, Any]],
    *,
    sections: list[dict[str, Any]] | None = None,
    marked_spans: list[dict[str, Any]] | None = None,
    pages: int = 0,
) -> dict[str, Any]:
    record = _base("parsed", response_bytes, findings)
    record.update(
        {
            "shape": shape,
            "view": CHURRO_TEXT_VIEW,
            "text": text,
            "sections": [] if sections is None else sections,
            "marked_spans": [] if marked_spans is None else marked_spans,
            "pages": pages,
        }
    )
    return record


def parse_churro_document(
    raw: bytes,
    *,
    system_prompt: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Read one retained Churro response into its parse record.

    `system_prompt` is the exact string this request sent, and only that
    string is ever trimmed from the head of the response.  `max_bytes`, when
    given, is the caller's own retained-parsing ceiling; this module declares
    none of its own (see the module docstring).

    The three legal shapes are told apart by parsing first, not by guessing
    from the first character.  A response that parses is classified by its root
    element; a response that does not parse is `failed` only if it offered the
    grammar at all -- otherwise it is the plain reading-order text the paper-era
    harness itself expected, and a transcription that merely happens to open
    with `<` is not thrown away for it.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise SchemaRefusal("a Churro response is not raw bytes")
    payload = bytes(raw)
    response_bytes = len(payload)
    findings: list[dict[str, Any]] = []
    if max_bytes is not None and response_bytes > max_bytes:
        return _base("failed", response_bytes, findings) | {
            "reason": (
                f"Churro response exceeds the retained parsing limit of {max_bytes} bytes "
                f"(received {response_bytes})"
            )
        }
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        return _base("failed", response_bytes, findings) | {
            "reason": f"Churro response is not UTF-8 text: {error}"
        }

    body = trim_leading_prompt(decoded, system_prompt)
    if len(body) != len(decoded):
        findings.append({"kind": "prompt-echo-trimmed", "characters": len(decoded) - len(body)})

    # A DOCTYPE is refused before any parser sees it. ElementTree's expat will
    # expand internal entities, so a declaration is an amplification hazard
    # rather than a grammar this pipeline ever asked for; refusing it keeps the
    # bytes retained and unread instead of expanded.
    if "<!DOCTYPE" in body.upper():
        return _base("failed", response_bytes, findings) | {
            "reason": "Churro response carries a DOCTYPE; the HistoricalDocument grammar has none"
        }

    # Leading whitespace is dropped from the *parser's* input only; the
    # plain-text shape below still returns the body exactly as it decoded. XML
    # permits whitespace before a root element but not before a declaration, so
    # without this a `"\n<?xml ...?><HistoricalDocument>"` answer would be a
    # parse failure over a leading newline that is not part of any reading.
    offered = body.lstrip()
    try:
        root = ET.fromstring(offered)
    except ET.ParseError as error:
        if DOCUMENT_ROOT_ELEMENT not in body:
            return _parsed("plain-text", body, response_bytes, findings)
        failed = _base("failed", response_bytes, findings) | {
            "reason": f"Churro response is not parseable XML: {error}"
        }
        # One escape pass over characters that cannot be markup, then one
        # re-parse (departure 3).  Nothing structural is repaired: a response
        # that still will not parse is `failed`, and its reason names the
        # failure that survived the escape rather than the stray character the
        # escape had already dealt with -- with the count, so the reason is
        # never mistaken for what the bytes said as they arrived.
        escaped, characters = _escape_stray_markup(offered)
        if not characters:
            return failed
        try:
            root = ET.fromstring(escaped)
        except ET.ParseError as retry_error:
            return failed | {
                "reason": (
                    f"Churro response is not parseable XML: {retry_error} "
                    f"(after escaping {characters} stray markup characters)"
                )
            }
        findings.append({"kind": "stray-markup-escaped", "characters": characters})

    name = _local(root.tag)
    if name == DOCUMENT_ROOT_ELEMENT:
        try:
            flattened = _flatten_document(root)
        except _DocumentTooDeep as error:
            return _base("failed", response_bytes, findings) | {"reason": str(error)}
        findings.extend(flattened["findings"])
        return _parsed(
            "historical-document",
            flattened["text"],
            response_bytes,
            findings,
            sections=flattened["sections"],
            marked_spans=flattened["marked_spans"],
            pages=flattened["pages"],
        )
    if name == OUTPUT_ROOT_ELEMENT and not root.attrib and not list(root):
        findings.append({"kind": "retired-output-envelope"})
        return _parsed("output-element", root.text or "", response_bytes, findings)
    return _base("unrecognized-shape", response_bytes, findings) | {
        "reason": (
            f"Churro response is well-formed XML rooted at {name!r}, which is neither the "
            f"{DOCUMENT_ROOT_ELEMENT} grammar nor a bare <{OUTPUT_ROOT_ELEMENT}> element"
        )
    }


_PARSED_FIELDS: Final = frozenset(
    {
        "state",
        "parser",
        "response_bytes",
        "findings",
        "shape",
        "view",
        "text",
        "sections",
        "marked_spans",
        "pages",
    }
)
_UNPARSED_FIELDS: Final = frozenset({"state", "parser", "response_bytes", "findings", "reason"})
_SECTION_FIELDS: Final = frozenset({"page_ordinal", "section", "span", "lines"})


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_churro_document_parse(value: Any) -> dict[str, Any]:
    """Close the record `parse_churro_document` produces, at any consumer seam.

    A consumer that indexes `text`, `sections` or `marked_spans` on a record it
    did not derive itself gets a named refusal here rather than a bare KeyError
    three stages away.
    """
    if not isinstance(value, dict):
        raise SchemaRefusal("a Churro document parse is not an object")
    state = value.get("state")
    if state not in PARSE_STATES:
        raise SchemaRefusal(f"a Churro document parse has unknown state {state!r}")
    expected = _PARSED_FIELDS if state == "parsed" else _UNPARSED_FIELDS
    if set(value) != expected:
        raise SchemaRefusal(f"a Churro document parse in state {state!r} is not its closed schema")
    if value["parser"] != CHURRO_PARSER:
        raise SchemaRefusal("a Churro document parse does not name the Churro parser")
    if not _positive_int(value["response_bytes"]):
        raise SchemaRefusal("a Churro document parse has no response byte count")
    findings = value["findings"]
    if not isinstance(findings, list):
        raise SchemaRefusal("a Churro document parse findings block is not a list")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("kind") not in DOCUMENT_FINDING_KINDS:
            raise SchemaRefusal("a Churro document parse has an unknown finding kind")
        kind = finding["kind"]
        if kind in _COUNTED_FINDING_KINDS:
            if set(finding) != {"kind", "characters"} or not _positive_int(finding["characters"]):
                raise SchemaRefusal(f"a Churro {kind} finding is malformed")
        elif kind == "page-text-outside-sections":
            ordinal = finding.get("page_ordinal")
            if (
                set(finding) != {"kind", "page_ordinal"}
                or not _positive_int(ordinal)
                or ordinal < 1
            ):
                raise SchemaRefusal(f"a Churro {kind} finding is malformed")
        elif set(finding) != {"kind"}:
            raise SchemaRefusal("a Churro retired-envelope finding is malformed")
    if state != "parsed":
        reason = value["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal(f"a {state!r} Churro document parse carries no reason")
        return value
    if value["shape"] not in DOCUMENT_SHAPES:
        raise SchemaRefusal(f"a parsed Churro document has unknown shape {value['shape']!r}")
    if value["view"] != CHURRO_TEXT_VIEW:
        raise SchemaRefusal("a parsed Churro document does not name the Churro text view")
    text = value["text"]
    if not isinstance(text, str):
        raise SchemaRefusal("a parsed Churro document carries no text")
    if not _positive_int(value["pages"]):
        raise SchemaRefusal("a parsed Churro document has no page count")
    if not isinstance(value["sections"], list) or not isinstance(value["marked_spans"], list):
        raise SchemaRefusal("a parsed Churro document's sections and spans are not lists")
    # Only the grammar carries page structure. A plain-text or `<output>`
    # answer that claimed sections, spans or a page count would be asserting a
    # structure nobody read out of it.
    if value["shape"] != "historical-document" and (
        value["sections"] or value["marked_spans"] or value["pages"]
    ):
        raise SchemaRefusal(
            f"a {value['shape']!r} Churro document claims page structure it cannot have read"
        )
    for section in value["sections"]:
        if not isinstance(section, dict) or set(section) != _SECTION_FIELDS:
            raise SchemaRefusal("a Churro document section record is not its closed schema")
        if section["section"] not in PAGE_SECTIONS:
            raise SchemaRefusal(f"a Churro document section is named {section['section']!r}")
        if not _positive_int(section["page_ordinal"]) or section["page_ordinal"] < 1:
            raise SchemaRefusal("a Churro document section has no 1-based page ordinal")
        if not _positive_int(section["lines"]):
            raise SchemaRefusal("a Churro document section has no line count")
        span = section["span"]
        if not isinstance(span, dict) or set(span) != {"start", "end"}:
            raise SchemaRefusal("a Churro document section span is not its closed schema")
        _validate_span(span["start"], span["end"], text, "a Churro document section")
    for span in value["marked_spans"]:
        if not isinstance(span, dict) or set(span) != {"kind", "start", "end"}:
            raise SchemaRefusal("a Churro marked span is not its closed schema")
        if span["kind"] not in MARKED_SPAN_KINDS:
            raise SchemaRefusal(f"a Churro marked span has unknown kind {span['kind']!r}")
        _validate_span(span["start"], span["end"], text, "a Churro marked span")
    return value


def _validate_span(start: Any, end: Any, text: str, what: str) -> None:
    """A span may be empty, but it may never name an offset the text cannot answer."""
    if not _positive_int(start) or not _positive_int(end) or end < start:
        raise SchemaRefusal(f"{what} is not a non-negative [start, end) span")
    if end > len(text):
        raise SchemaRefusal(f"{what} runs past the end of the text it addresses")
