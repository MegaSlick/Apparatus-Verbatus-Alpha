"""Chandra's own layout grammar: the vendor's prompt bytes, and a reader for its answer.

Tonight's ruling (Tyrel, 2026-09-06) is that each witness runs as its developers
intended: the vendor's preprocessing, prompt bytes, message shape, generation
values and output grammar are adopted verbatim and pinned by digest, and the
vendor's harness is not. This module is the Chandra half of the grammar end of
that line. It carries two things and nothing else:

1. **The prompt the vendor sends.** `OCR_LAYOUT_PROMPT` -- the `"ocr_layout"`
   entry of `PROMPT_MAPPING`, which is the prompt every vendor caller uses for
   a layout read -- reproduced from `chandra/prompts.py` at the pinned commit,
   built by the vendor's own f-string over the vendor's own 36 tags and 14
   attributes so the rendered bytes are identical rather than merely similar.
   `OCR_LAYOUT_PROMPT_SHA256` and `PROMPT_ENDING_SHA256` are checked against
   the rendered strings at import: a byte edited here, by anyone, for any
   reason, fails to import rather than quietly changing what a chair is asked.
   The vendor file itself is **not** stored (the standing "fetched at boot,
   never stored" ruling); the digests are what lives in the tree, and
   `common/test_vendor_parity.py`'s network-gated arm re-fetches the pinned
   file and proves the equality against the vendor rather than against us.

2. **A reader for the answer that prompt asks for.** `parse_layout_html`
   re-expresses `chandra/output.py::parse_layout` over the standard library's
   `html.parser`, so nothing in `pyproject.toml` grows and nothing on the live
   path imports a vendor package (the namespace guard in
   `common/test_vendor_parity.py` pins that). It is a re-expression, not a
   copy, and it departs from the vendor in five recorded places -- see
   **Departures** below. Every departure moves in one direction: the vendor
   drops or substitutes, and we retain and name (GOVERNANCE 2).

**What this module does not do.** It establishes no text and selects nothing
(GOVERNANCE 3, hard rule 8). It is a grammar: bytes in, one named reading of
those bytes out, with every fact it could not resolve carried beside the
reading as a finding rather than resolved for it. The adapter that decides
what a chair is asked and what a Testimonium records is
`pipeline/3_attestatores/chandra.py`; the Designator's structure pass reads the
same grammar for its own purpose. Both call this; neither restates it, because
two readings of one page that disagreed about what a `data-bbox` means would be
two page-pixel mappings for one chair.

**Provenance.** Prompt bytes and parser behaviour:
`github.com/datalab-to/chandra` at commit
`d4f7467435aa4137d9539f000ddf0b7ced3eb43f` (`pyproject.toml` declares
`chandra-ocr 0.2.0`, Apache-2.0). `chandra/prompts.py` for the prompt,
`chandra/output.py::parse_layout` for the grammar, `chandra/settings.py` for
`BBOX_SCALE`. Apache-2.0 permits the carry; the citation is the licence's
condition and it is discharged here and in the commit that adds this file
(`cleanroom/README.md`).

## Departures from `chandra/output.py::parse_layout`, and why each one

* **A malformed `data-bbox` yields `bbox_1000: None` and a `malformed-bbox`
  finding.** The vendor prints `f"Invalid bbox format: {bbox}, defaulting to
  full image"` and substitutes `[0, 0, 1, 1]` -- which, scaled, is a rectangle
  of a few pixels in the page's top-left corner, not the full image the message
  claims. Either way a value the model never reported would be published as
  though it had been, and the print goes to a stdout nobody retains. Under
  GOVERNANCE 2 and 10 the block is retained with its geometry unresolved and
  the fact named. `block_page_bounds` returns `None` for it, so no caller can
  reach a substituted rectangle by accident.
* **A `Blank-Page` block is retained.** The vendor `continue`s past it in both
  `parse_layout` and `parse_html`, so a page the model declared blank leaves no
  record at all and is indistinguishable from a page it never answered about.
  Here the block is kept, with `blank_page: True`, no text in the page text
  (its span is zero-width where its text would have sat) and no page geometry
  (`block_page_bounds` returns `None`). Its declared `data-bbox` is still
  parsed and recorded, because discarding it would be the same silent loss in
  a smaller place.
* **Nested `data-bbox` attributes are recorded, not stripped.** The vendor
  deletes them from the block's content ("not needed in open source"). They are
  geometry the model reported; `nested_bboxes` lists them per block in document
  order and `content` keeps the answer's own bytes. Nothing derives page
  geometry from them -- they are evidence, not a second geometry channel.
* **Character data outside every top-level block is counted and named.**
  The vendor's `find_all("div", recursive=False)` does not see it, and neither
  does any block here: a block the model answered as a top-level `<p>` or
  `<table>`, or a line of ink it left between two divs, would otherwise yield
  a page that parsed cleanly -- `findings == []`, `parse` complete -- with
  those words absent from `page_text` and from every span. That is a missed
  act arriving under a successful status, which GOALS 1 rates worst and
  GOVERNANCE 2 forbids. It is a `content-outside-blocks` finding carrying the
  number of non-whitespace characters that were outside. The count and not the
  text, for the reason the `malformed-bbox` finding quotes under a bound: the
  response bytes are retained whole upstream and are where the words live,
  and a chair's own reading is published here as a length rather than as
  prose. Character data is what is counted because character data is the
  whole of what `LAYOUT_TEXT_VIEW` reads, so markup outside a block drops no
  ink the text view would have read either; whitespace between blocks is
  source formatting and produces nothing.
* **The returned block count is reconciled against the raw HTML's own
  top-level `<div>` count.** `_count_top_level_divs` is a second, deliberately
  different scan -- a depth counter over `div` tags alone, blind to every other
  element -- so a block the main reader loses to unbalanced markup shows up as
  a `block-count-mismatch` finding instead of as a shorter list nobody
  compares. A reconciliation computed by the code it is reconciling proves
  nothing.

Two smaller re-expressions are faithful rather than departures, and are noted
so a reader is not left to infer them. The vendor's `if not label: label =
"block"` default is reproduced exactly, with `label_declared` recording whether
the answer carried a `data-label` at all. And the vendor's `int()` over each
space-separated bbox component is narrowed to a full match of
`[+-]?[0-9]+`, because Python's `int` also accepts underscore-separated
literals (`int("1_0") == 10`) and surrounding whitespace: the first is a
convenience of the Python lexer, not a shape any model was ever asked for, and
reading it as ten would be exactly the substituted value the first departure
exists to prevent. It is a *full* match rather than an anchored one because
`$` matches before a trailing newline too, which would have left the narrowing
claiming more than it did.

## Geometry

`data-bbox` is `"x0 y0 x1 y1"`, four integers normalized to `BBOX_SCALE`
(1000), which is what the prompt tells the model and what
`chandra/settings.py` sets. `block_page_bounds` converts to sealed-page pixels
through `common.structure_answer.to_page_bounds` -- the one conversion the
Designator's own Chandra reading uses, so both readings of a page land in one
page-pixel mapping. The sealed page is the right denominator because the vendor
uses the same one: `InferenceManager` runs `parse_chunks` against the original
image, not the resized one it sent.

`vendor_scaled_bbox` re-expresses the vendor's own scaling arithmetic. It is on
no live path and exists so the relationship between the two conversions is
asserted by a test rather than described in a comment. That relationship is not
"ours is the vendor's, ceiled", and the difference was found by running them
against each other rather than by reading them:

* Ours is the rule in exact integer arithmetic -- near edges floored, far edges
  ceiled -- so our rectangle always contains the real-valued rectangle the
  model's normalized box denotes. Nothing the model placed is cropped away.
* The vendor computes `width / 1000` as a float first and multiplies. That
  division is not exact in binary, so at coordinates where the true product is
  a whole number the float lands a hair below it and `int()` truncates a pixel
  off: on a 2550-pixel-wide page, `x0 = 100` is exactly 255, and the vendor's
  arithmetic returns 254. Its near edge is then one pixel *outside* the box the
  model reported, and its far edge one pixel inside.

So against the vendor's own numbers ours can start one pixel further in on a
near edge -- never because it crops the model's box, but because the vendor's
does not land on it. The test states both halves separately; neither is folded
into the other.

## The text view `chandra-layout-text.v1`

The vendor's own text path is `parse_markdown`, which routes through
`markdownify` and `BeautifulSoup`. Neither is a dependency here and neither
would be a *reading*: it drops `Blank-Page`, drops headers and footers by
default, rewrites `<img>` and hallucinated image tags, and wraps bare text
blocks in `<p>`. So the text view is ours, named, and stated in full:

* over one block's `content` (its inner HTML), in document order;
* character data is emitted as text, with character references already
  resolved (`html.parser` with `convert_charrefs=True`);
* outside `<pre>`, each run of whitespace in the data becomes one space, and
  two spaces never end up adjacent -- HTML source line breaks and indentation
  are markup formatting, not ink;
* inside `<pre>`, data is emitted exactly as written;
* `<br>` emits a line break; so do the start and the end of `p`, `div`,
  `table`, `tr`, `li`, `h1`-`h5`, `ul`, `ol`, `pre`, `caption`, `blockquote`,
  `thead` and `tbody`; `<td>` and `<th>` emit a space, so cells of one row do
  not run together into one word;
* every other tag emits nothing of its own and its children are emitted in
  place, so `<b>`, `<i>`, `<u>`, `<del>`, `<sup>`, `<sub>`, `<span>`, `<a>`,
  `<small>`, `<big>`, `<strong>`, `<code>`, `<math>` and `<chem>` are
  transparent -- their content is the reading;
* `<img>` contributes nothing: its `alt` is the vendor prompt's *description*
  of a picture, not a transcription of ink, and it stays in `content` where a
  reader can see it for what it is;
* consecutive line breaks collapse to one, and the block's text is stripped.

`page_text` is `common.structure_answer.join_delivered_texts` over the block
texts -- a newline between delivered (non-empty) texts and nowhere else -- and
`spans` locates each block in it, empty and blank blocks as a zero-width span.
That is the same join and the same span rule the Designator's answer and the
retired wire contract both used, so a span published against this page text
lands where every other Chandra reading of the page puts it.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Any, Final, TypedDict

from common.imaging import Bounds
from common.structure_answer import join_delivered_texts, to_page_bounds

VENDOR_REPOSITORY: Final = "github.com/datalab-to/chandra"
VENDOR_COMMIT: Final = "d4f7467435aa4137d9539f000ddf0b7ced3eb43f"
VENDOR_LICENCE: Final = "Apache-2.0"
VENDOR_PROMPT_SOURCE: Final = "chandra/prompts.py"
VENDOR_PARSER_SOURCE: Final = "chandra/output.py::parse_layout"
VENDOR_SETTINGS_SOURCE: Final = "chandra/settings.py"

# `chandra/prompts.py::ALLOWED_TAGS` and `ALLOWED_ATTRIBUTES`, in the vendor's
# own order. Order is load-bearing, not cosmetic: both lists are interpolated
# into `PROMPT_ENDING` by their Python `repr`, so a re-sorted list is different
# prompt bytes. Lists rather than tuples for the same reason.
ALLOWED_TAGS: Final[list[str]] = [
    "math",
    "br",
    "i",
    "b",
    "u",
    "del",
    "sup",
    "sub",
    "table",
    "tr",
    "td",
    "p",
    "th",
    "div",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "ul",
    "ol",
    "li",
    "input",
    "a",
    "span",
    "img",
    "hr",
    "tbody",
    "small",
    "caption",
    "strong",
    "thead",
    "big",
    "code",
    "chem",
]
ALLOWED_ATTRIBUTES: Final[list[str]] = [
    "class",
    "colspan",
    "rowspan",
    "display",
    "checked",
    "type",
    "border",
    "value",
    "style",
    "href",
    "alt",
    "align",
    "data-bbox",
    "data-label",
]

PROMPT_ENDING: Final = f"""
Only use these tags {ALLOWED_TAGS}, and these attributes {ALLOWED_ATTRIBUTES}.

Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural.
""".strip()  # noqa: E501

OCR_LAYOUT_PROMPT: Final = f"""
OCR this image to HTML, arranged as layout blocks.  Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format.  Bboxes are normalized 0-1000. The data-label attribute is the label for the block.

Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page

{PROMPT_ENDING}
""".strip()  # noqa: E501

# The nineteen labels the prompt above offers, in the order it offers them.
# They are not interpolated into the prompt -- the vendor writes them out as a
# literal bullet list -- so this tuple is checked against the prompt at import
# rather than trusted: a label added here that the model was never offered, or
# a label dropped from the prompt that a caller still switches on, is a
# disagreement between what we ask for and what we claim to have asked for.
OCR_LAYOUT_LABELS: Final[tuple[str, ...]] = (
    "Caption",
    "Footnote",
    "Equation-Block",
    "List-Group",
    "Page-Header",
    "Page-Footer",
    "Image",
    "Section-Header",
    "Table",
    "Text",
    "Complex-Block",
    "Code-Block",
    "Form",
    "Table-Of-Contents",
    "Figure",
    "Chemical-Block",
    "Diagram",
    "Bibliography",
    "Blank-Page",
)
BLANK_PAGE_LABEL: Final = "Blank-Page"
# `chandra/output.py::parse_layout`: `if not label: label = "block"`. Not one
# of the nineteen -- it is what the vendor calls a block whose answer declared
# no label at all.
UNLABELLED_BLOCK_LABEL: Final = "block"

# SHA-256 of the two carried strings as rendered, against
# `{VENDOR_REPOSITORY} @ {VENDOR_COMMIT}` `{VENDOR_PROMPT_SOURCE}`. Checked at
# import (below) and again, against the vendor rather than against us, by
# `common/test_vendor_parity.py`.
PROMPT_ENDING_SHA256: Final = "f5d1ed0fb0ead54db6271c3e5dba9d581dcd8f9aa1709ab3b029761c00cb2233"
OCR_LAYOUT_PROMPT_SHA256: Final = "025935f3e1de1acdfadd4c7d581ab17eb82e8caaffef7b64962621c80b7ca9a8"
# SHA-256 of the whole vendor file at that commit, 2,820 bytes. The commit sha
# already pins those bytes, so this is not a second pin -- it is what the
# network-gated arm of `common/test_vendor_parity.py` checks *before* it
# executes the fetched `prompts.py` in an isolated namespace to render the
# prompt. Executing a file that arrived over the network and only then asking
# what it was would be the wrong order.
VENDOR_PROMPT_FILE_SHA256: Final = (
    "53101d315a9923dac2fd65bf64047a73d396c69c409b3680c753315837b151eb"
)

# `chandra/settings.py::Settings.BBOX_SCALE`. The prompt states the same number
# in prose; `_NORMALIZED_CLAIM` below is checked against the prompt text at
# import, so the two can never drift apart silently -- a re-pin that changed
# the scale in `settings.py` but not the sentence, or the sentence but not the
# scale, would leave every reported box scaled by the wrong denominator with
# nothing to show for it.
BBOX_SCALE: Final = 1000
_NORMALIZED_CLAIM: Final = f"Bboxes are normalized 0-{BBOX_SCALE}."

# The named rule this module's `page_text` and `spans` are produced by. It is
# ours, not the vendor's; see the module docstring for the rule in full.
LAYOUT_TEXT_VIEW: Final = "chandra-layout-text.v1"

# The same operational ceilings the Chandra adapter has always applied to bytes
# crossing the native model boundary: the byte bound matches the repository's
# RunPod response ceiling, and the block bound is a chosen ceiling rather than
# a claim about Chandra's behaviour -- ten thousand layout blocks on one page
# leaves ample headroom while keeping one compact answer from expanding into an
# unbounded list of derived records.
MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
MAX_LAYOUT_BLOCKS: Final = 10_000
# A finding is published; the response bytes it describes are retained whole
# and are where the whole story lives. So the one piece of model-written text a
# finding quotes -- the `data-bbox` it could not read -- is quoted to a bounded
# length and says when it was cut. Without this, a model that emitted a
# megabyte inside one attribute would put a megabyte into the record through a
# field whose purpose is to show a reader what "1_0 2 3 4" looked like.
MAX_QUOTED_ATTRIBUTE_CHARACTERS: Final = 120

PARSE_OUTCOMES: Final = frozenset(
    {
        "raw-response-not-bytes",
        "response-too-large",
        "invalid-utf8",
        "no-layout-blocks",
        "blocks-not-at-top-level",
        "too-many-layout-blocks",
    }
)
LAYOUT_FINDING_KINDS: Final = frozenset(
    {
        "malformed-bbox",
        "blank-page-retained",
        "nested-bbox-retained",
        "unclosed-block",
        "block-count-mismatch",
        "content-outside-blocks",
    }
)


class LayoutBlock(TypedDict):
    ordinal: int
    # The vendor's resolved label: the answer's `data-label`, or
    # `UNLABELLED_BLOCK_LABEL` where it declared none.
    label: str
    label_declared: bool
    blank_page: bool
    # Four integers in `[0, BBOX_SCALE]` with `x1 > x0` and `y1 > y0`, or
    # `None` where the answer's `data-bbox` was absent or malformed. Never a
    # substituted rectangle.
    bbox_1000: list[int] | None
    # The block's inner HTML, exactly as the answer wrote it, nested
    # `data-bbox` attributes included.
    content: str
    # `content` under `LAYOUT_TEXT_VIEW`. Empty for a `Blank-Page` block.
    text: str
    # Every `data-bbox` a descendant of this block carried, in document order.
    nested_bboxes: list[str]


class ParsedLayout(TypedDict):
    text_view: str
    blocks: list[LayoutBlock]
    page_text: str
    spans: list[dict[str, int]]
    findings: list[dict[str, Any]]


def _refuse(outcome: str) -> dict[str, str]:
    """A closed `{"parse_outcome": ...}` record -- never a code outside the set."""
    if outcome not in PARSE_OUTCOMES:
        raise ValueError(f"undeclared parse outcome {outcome!r}")
    return {"parse_outcome": outcome}


def _finding(kind: str, **facts: Any) -> dict[str, Any]:
    """One finding, refused unless its kind is declared."""
    if kind not in LAYOUT_FINDING_KINDS:
        raise ValueError(f"undeclared layout finding kind {kind!r}")
    return {"kind": kind, **facts}


def is_refusal(parsed: Any) -> bool:
    """Whether `parse_layout_html` returned a named refusal rather than a page."""
    return isinstance(parsed, dict) and set(parsed) == {"parse_outcome"}


def _quoted(value: str | None) -> dict[str, Any]:
    """One model-written attribute, quoted into a finding under a stated bound."""
    if value is None or len(value) <= MAX_QUOTED_ATTRIBUTE_CHARACTERS:
        return {"data_bbox": value, "data_bbox_truncated": False}
    return {
        "data_bbox": value[:MAX_QUOTED_ATTRIBUTE_CHARACTERS],
        "data_bbox_truncated": True,
    }


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

# Matched with `fullmatch`, and anchorless on purpose. `re.match` against
# `r"^[+-]?[0-9]+$"` accepts `"7\n"`, because Python's `$` also matches just
# before a final newline -- so a component carrying a line break would have
# been read as a number by a rule whose refusal says it is not a plain decimal
# integer. `int("7\n")` is 7, so nothing was ever misread; the claim in the
# record was simply false, and a check that does not enforce what it states is
# the kind of thing GOVERNANCE 10 is about.
_BBOX_COMPONENT: Final = re.compile(r"[+-]?[0-9]+")


def parse_bbox_attribute(value: str | None) -> tuple[list[int] | None, str | None]:
    """`"x0 y0 x1 y1"` as four normalized integers, or `(None, reason)`.

    The vendor's own acceptance is `bbox.split(" ")`, `list(map(int, ...))` and
    `assert len(bbox) == 4`, with every failure funnelled into one `except
    Exception` that substitutes `[0, 0, 1, 1]`. The split and the arity are
    reproduced; the component pattern narrows `int` away from Python literal
    forms (see the module docstring); the last two checks are ours, for two
    different reasons.

    The ordering check exists because `to_page_bounds` is not a clamp on that
    axis: handed `x1 == x0` it returns width 0 and handed `x1 < x0` it returns a
    negative width, and a negative low edge comes back negative. Those are
    substituted values wearing a different shape, and a test measures each of
    them rather than taking this paragraph's word for it. The upper-range check
    is different: `to_page_bounds` does clamp a far edge past the page, so
    nothing malformed would reach a caller. It refuses anyway, because a
    component above `BBOX_SCALE` means the model is not scaling to the
    denominator the prompt gave it, and a box quietly clamped to the page edge
    would publish a plausible rectangle for a reading that had already gone
    wrong -- the failure GOALS 2 rates worst.

    Each refusal names which rule it failed, so a page of malformed boxes says
    whether the model is scaling wrongly or formatting wrongly.
    """
    if value is None:
        return None, "no data-bbox attribute"
    parts = value.split(" ")
    if len(parts) != 4:
        return None, f"expected 4 space-separated components, found {len(parts)}"
    if not all(_BBOX_COMPONENT.fullmatch(part) for part in parts):
        return None, "components are not plain decimal integers"
    box = [int(part) for part in parts]
    if not all(0 <= component <= BBOX_SCALE for component in box):
        return None, f"components outside [0, {BBOX_SCALE}]"
    if box[2] <= box[0] or box[3] <= box[1]:
        return None, "x1 <= x0 or y1 <= y0"
    return box, None


def block_page_bounds(block: LayoutBlock, *, page_size: tuple[int, int]) -> Bounds | None:
    """One block's sealed-page rectangle, or `None` where it reports no geometry.

    `None` is returned for a malformed or absent `data-bbox` and for a
    `Blank-Page` block. There is no third answer and no default: a caller that
    wants a rectangle for such a block has to decide that for itself, in the
    open, rather than receive one from here.
    """
    if block["blank_page"] or block["bbox_1000"] is None:
        return None
    page_w, page_h = page_size
    return to_page_bounds(block["bbox_1000"], page_w, page_h)


def vendor_scaled_bbox(bbox_1000: list[int], *, page_size: tuple[int, int]) -> list[int]:
    """`chandra/output.py::parse_layout`'s own scaling, re-expressed for comparison.

    On no live path; see the module docstring's Geometry section for what it is
    for and what the comparison actually shows. The float division is the
    vendor's and is reproduced rather than corrected -- correcting it here would
    hide the one-pixel difference this function exists to measure. Returns the
    vendor's `[x0, y0, x1, y1]` in page pixels, far edges exclusive.
    """
    page_w, page_h = page_size
    width_scaler = page_w / BBOX_SCALE
    height_scaler = page_h / BBOX_SCALE
    return [
        max(0, int(bbox_1000[0] * width_scaler)),
        max(0, int(bbox_1000[1] * height_scaler)),
        min(int(bbox_1000[2] * width_scaler), page_w),
        min(int(bbox_1000[3] * height_scaler), page_h),
    ]


# ---------------------------------------------------------------------------
# The text view
# ---------------------------------------------------------------------------

# HTML's void elements: they never nest and never close, so they are never
# pushed onto an element stack.
_VOID_ELEMENTS: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_LINE_BREAK_TAGS: Final = frozenset(
    {
        "p",
        "div",
        "table",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "ul",
        "ol",
        "pre",
        "caption",
        "blockquote",
        "thead",
        "tbody",
    }
)
_CELL_TAGS: Final = frozenset({"td", "th"})
_WHITESPACE_RUN: Final = re.compile(r"\s+")


class _BlockTextReader(HTMLParser):
    """`LAYOUT_TEXT_VIEW` over one block's inner HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._pre_depth = 0

    # -- assembly -----------------------------------------------------------
    def _last(self) -> str:
        return self._out[-1][-1] if self._out and self._out[-1] else ""

    def _append_text(self, text: str) -> None:
        if not text:
            return
        if text.startswith(" ") and self._last() in {"", " ", "\n"}:
            text = text.lstrip(" ")
            if not text:
                return
        self._out.append(text)

    def _append_space(self) -> None:
        if self._last() not in {"", " ", "\n"}:
            self._out.append(" ")

    def _append_break(self) -> None:
        while self._last() == " ":
            trimmed = self._out.pop().rstrip(" ")
            if trimmed:
                self._out.append(trimmed)
        if self._last() not in {"", "\n"}:
            self._out.append("\n")

    # -- events -------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._append_break()
        elif tag in _LINE_BREAK_TAGS:
            self._append_break()
            if tag == "pre":
                self._pre_depth += 1
        elif tag in _CELL_TAGS:
            self._append_space()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<p/>` and `<br/>` open and close in one token; a self-closing
        # `<pre/>` must not leave `_pre_depth` raised.
        if tag == "br" or tag in _LINE_BREAK_TAGS:
            self._append_break()
        elif tag in _CELL_TAGS:
            self._append_space()

    def handle_endtag(self, tag: str) -> None:
        if tag in _LINE_BREAK_TAGS:
            if tag == "pre" and self._pre_depth:
                self._pre_depth -= 1
            self._append_break()
        elif tag in _CELL_TAGS:
            self._append_space()

    def handle_data(self, data: str) -> None:
        if self._pre_depth:
            self._out.append(data)
            return
        self._append_text(_WHITESPACE_RUN.sub(" ", data))

    def text(self) -> str:
        return "".join(self._out).strip()


def layout_block_text(content_html: str) -> str:
    """One block's `LAYOUT_TEXT_VIEW` reading of its own inner HTML."""
    reader = _BlockTextReader()
    reader.feed(content_html)
    reader.close()
    return reader.text()


# ---------------------------------------------------------------------------
# The layout reader
# ---------------------------------------------------------------------------


class _TopLevelDivReader(HTMLParser):
    """Top-level `<div>` boundaries, their attributes, and their inner HTML.

    "Top level" is the vendor's `soup.find_all("div", recursive=False)`: a div
    that is a direct child of the document fragment. A model that wrapped its
    answer in `<html><body>` gives the vendor no blocks at all, and gives this
    reader none either -- the same answer, reported (as
    `blocks-not-at-top-level`) rather than repaired.

    Inner HTML is taken as a slice of the answer's own characters between the
    end of the opening tag and the start of the closing one, so `content` is
    the model's bytes and not a re-serialization of them.

    `outside_characters` counts the ink that landed nowhere: character data
    the answer wrote while no top-level block was open. Neither the vendor nor
    this reader puts it in a block, so counting it is the only thing standing
    between that text and a page that parses clean without it.
    """

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self._html = html
        self._line_starts = _line_starts(html)
        self._stack: list[str] = []
        self._open: dict[str, Any] | None = None
        self.blocks: list[dict[str, Any]] = []
        self.overflowed = False
        self.unclosed = False
        self.outside_characters = 0

    def _index(self) -> int:
        line, offset = self.getpos()
        return self._line_starts[line - 1] + offset

    def _open_block(self, attrs: list[tuple[str, str | None]], content_start: int) -> bool:
        """Begin one top-level block, or report that the ceiling refuses it."""
        if len(self.blocks) >= MAX_LAYOUT_BLOCKS:
            self.overflowed = True
            return False
        # `html.parser` reports a valueless attribute (`<div data-bbox>`) as
        # `None`, which would then be indistinguishable from the attribute
        # being absent -- two different answers, one record. BeautifulSoup
        # hands the vendor `""` for it, so `""` is both faithful and
        # distinguishable: it reads as a malformed box rather than as no box.
        # A repeated attribute takes the last value, which is also what
        # BeautifulSoup's default duplicate handling gives the vendor.
        lookup = {name: "" if value is None else value for name, value in attrs}
        self._open = {
            "label_raw": lookup.get("data-label"),
            "bbox_raw": lookup.get("data-bbox"),
            "content_start": content_start,
            "nested_bboxes": [],
        }
        return True

    def _close_block(self, content: str) -> None:
        block = self._open
        if block is None:
            return
        block["content"] = content
        self.blocks.append(block)
        self._open = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_ELEMENTS:
            self._note_nested_bbox(attrs)
            return
        if tag == "div" and not self._stack:
            start_text = self.get_starttag_text() or ""
            if not self._open_block(attrs, self._index() + len(start_text)):
                return
        else:
            self._note_nested_bbox(attrs)
        self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # An `<div ... />` opens and closes in one token: an empty block, not a
        # container. Nothing is pushed, so the element stack stays balanced.
        if tag == "div" and not self._stack:
            if self._open_block(attrs, self._index()):
                self._close_block("")
            return
        self._note_nested_bbox(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_ELEMENTS or tag not in self._stack:
            # A stray end tag closes nothing. `html.parser` reports it; the
            # vendor's parser drops it; neither invents an element for it.
            return
        while self._stack:
            if self._stack.pop() == tag:
                break
        if tag == "div" and not self._stack and self._open is not None:
            self._close_block(self._html[self._open["content_start"] : self._index()])

    def handle_data(self, data: str) -> None:
        """Character data, counted when no top-level block is open to hold it.

        Whitespace is not counted: the source's own line breaks and
        indentation between blocks are markup formatting, exactly as they are
        inside a block under `LAYOUT_TEXT_VIEW`. What is counted is text the
        model wrote that no block will carry -- between two divs, or inside a
        top-level `<p>` or `<table>` it answered a block as.
        """
        if self._open is None:
            self.outside_characters += len(_WHITESPACE_RUN.sub("", data))

    def _note_nested_bbox(self, attrs: list[tuple[str, str | None]]) -> None:
        if self._open is None:
            return
        for name, value in attrs:
            if name == "data-bbox":
                self._open["nested_bboxes"].append("" if value is None else value)

    def finish(self) -> None:
        """Close a block the answer left open, keeping the bytes it did send."""
        if self._open is not None:
            self.unclosed = True
            self._close_block(self._html[self._open["content_start"] :])


def _line_starts(text: str) -> list[int]:
    """Absolute index of the first character of each line, for `getpos()`."""
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts


class _TopLevelDivCounter(HTMLParser):
    """A second, deliberately different count of the same thing.

    It knows one element: `div`. Depth rises on every `<div>` and falls on
    every `</div>`, and a `<div>` seen at depth zero is a top-level block. It
    keeps no element stack and matches no other tag, so unbalanced markup moves
    the two counts apart instead of moving them together -- which is the whole
    point of reconciling them. A count computed by the reader it reconciles
    would agree with the reader by construction and report nothing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div":
            return
        if self._depth == 0:
            self.count += 1
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div" and self._depth == 0:
            self.count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._depth:
            self._depth -= 1


def _count_top_level_divs(html: str) -> int:
    counter = _TopLevelDivCounter()
    counter.feed(html)
    counter.close()
    return counter.count


def parse_layout_html(raw: Any) -> ParsedLayout | dict[str, str]:
    """Chandra's layout answer, read whole -- or one named refusal.

    Nothing here repairs, reorders, trims or defaults an answer (GOVERNANCE 7).
    A block whose geometry cannot be resolved is still a block, and the fact
    that it could not be resolved is a finding beside it; the caller decides
    what an unplaced block means for its own record, and the retained bytes
    outlive every decision either of us makes.

    An answer that yields no block is refused rather than returned as an empty
    page, and the two ways that happens are named apart: `no-layout-blocks` is
    an answer with no `<div>` in it at all -- prose, most likely -- while
    `blocks-not-at-top-level` is an answer that does have divs, every one of
    them nested inside something else. Only the second is a wrapper the vendor
    would also have found nothing in (`recursive=False`), and telling them
    apart is what lets a first real reading say which of the two happened
    without a person opening the blob.

    An answer that does yield blocks can still have written ink outside all of
    them -- between two divs, or in a top-level element it answered a block as.
    That text is in no block and in no span, so it is counted and named
    (`content-outside-blocks`) rather than left to a caller who would have no
    way of knowing it existed.
    """
    if not isinstance(raw, (bytes, bytearray)):
        return _refuse("raw-response-not-bytes")
    if len(raw) > MAX_RESPONSE_BYTES:
        return _refuse("response-too-large")
    try:
        html = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return _refuse("invalid-utf8")

    reader = _TopLevelDivReader(html)
    reader.feed(html)
    reader.close()
    reader.finish()
    if reader.overflowed:
        return _refuse("too-many-layout-blocks")
    raw_divs = _count_top_level_divs(html)
    if not reader.blocks:
        return _refuse("blocks-not-at-top-level" if raw_divs else "no-layout-blocks")

    findings: list[dict[str, Any]] = []
    if reader.unclosed:
        findings.append(
            _finding(
                "unclosed-block",
                ordinal=len(reader.blocks) - 1,
                detail="the answer ended before its last top-level div closed",
            )
        )
    blocks: list[LayoutBlock] = []
    nested_blocks = 0
    nested_attributes = 0
    for ordinal, found in enumerate(reader.blocks):
        label_raw = found["label_raw"]
        declared = bool(label_raw)
        label = label_raw if declared else UNLABELLED_BLOCK_LABEL
        blank = label == BLANK_PAGE_LABEL
        bbox_raw = found["bbox_raw"]
        bbox, reason = parse_bbox_attribute(bbox_raw)
        if bbox is None:
            # `label` is deliberately not repeated into the finding. It is the
            # chair's own word for what it thinks a rectangle is -- a reading,
            # unbounded in length, which this repository publishes as a digest
            # and a length and never as text (`common/structure_answer.py`).
            # `ordinal` joins the finding to the block, which carries it.
            findings.append(
                _finding("malformed-bbox", ordinal=ordinal, reason=reason, **_quoted(bbox_raw))
            )
        if blank:
            findings.append(_finding("blank-page-retained", ordinal=ordinal))
        nested = list(found["nested_bboxes"])
        if nested:
            nested_blocks += 1
            nested_attributes += len(nested)
        content = found["content"]
        blocks.append(
            {
                "ordinal": ordinal,
                "label": label,
                "label_declared": declared,
                "blank_page": blank,
                "bbox_1000": bbox,
                "content": content,
                "text": "" if blank else layout_block_text(content),
                "nested_bboxes": nested,
            }
        )
    if nested_attributes:
        findings.append(
            _finding("nested-bbox-retained", blocks=nested_blocks, attributes=nested_attributes)
        )
    if reader.outside_characters:
        findings.append(
            _finding(
                "content-outside-blocks",
                characters=reader.outside_characters,
                detail=(
                    "the answer wrote character data outside every top-level block; "
                    "no block carries it and the page text does not contain it"
                ),
            )
        )
    if raw_divs != len(blocks):
        findings.append(
            _finding("block-count-mismatch", parsed_blocks=len(blocks), top_level_divs=raw_divs)
        )

    page_text, spans = join_delivered_texts([block["text"] for block in blocks])
    return {
        "text_view": LAYOUT_TEXT_VIEW,
        "blocks": blocks,
        "page_text": page_text,
        "spans": spans,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# What the carry is, checked at import
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seal() -> None:
    """Refuse to import on any drift between the carry and what it claims to be.

    Every check here is about this file's own honesty, and each one has a
    failure it prevents. A digest that no longer matches means the prompt bytes
    changed after the vendor pin they are recorded against -- the chair would
    then be asked something no commit sha names. A count that no longer matches
    means a tag, attribute or label was edited in one of the two places it
    appears. The `0-1000` sentence and `BBOX_SCALE` disagreeing means every
    reported box is scaled by a denominator the model was never told about,
    which is the silent wrong reading GOALS 2 rates worst. An `AssertionError`
    would vanish under `python -O`; these raise.
    """
    if len(ALLOWED_TAGS) != 36:
        raise RuntimeError(f"carried ALLOWED_TAGS is {len(ALLOWED_TAGS)} tags, not the vendor's 36")
    if len(ALLOWED_ATTRIBUTES) != 14:
        raise RuntimeError(
            f"carried ALLOWED_ATTRIBUTES is {len(ALLOWED_ATTRIBUTES)}, not the vendor's 14"
        )
    if len(OCR_LAYOUT_LABELS) != 19:
        raise RuntimeError(f"carried OCR_LAYOUT_LABELS is {len(OCR_LAYOUT_LABELS)}, not 19")
    if len(set(OCR_LAYOUT_LABELS)) != len(OCR_LAYOUT_LABELS):
        # Without this, a repeated label would pad the tuple back to nineteen
        # and let the count check below pass while a real label went unnamed.
        raise RuntimeError("carried OCR_LAYOUT_LABELS repeats a label")
    for label in OCR_LAYOUT_LABELS:
        if f"\n- {label}\n" not in OCR_LAYOUT_PROMPT:
            raise RuntimeError(f"label {label!r} is not offered by the carried prompt")
    if OCR_LAYOUT_PROMPT.count("\n- ") != len(OCR_LAYOUT_LABELS):
        raise RuntimeError("the carried prompt offers labels this module does not name")
    if _NORMALIZED_CLAIM not in OCR_LAYOUT_PROMPT:
        raise RuntimeError(
            f"the carried prompt no longer states {_NORMALIZED_CLAIM!r}; "
            f"BBOX_SCALE={BBOX_SCALE} would then be a denominator the model was "
            f"never told about"
        )
    for name, rendered, recorded in (
        ("PROMPT_ENDING", PROMPT_ENDING, PROMPT_ENDING_SHA256),
        ("OCR_LAYOUT_PROMPT", OCR_LAYOUT_PROMPT, OCR_LAYOUT_PROMPT_SHA256),
    ):
        actual = _sha256(rendered)
        if actual != recorded:
            raise RuntimeError(
                f"carried {name} renders to sha256 {actual}, not the {recorded} recorded "
                f"against {VENDOR_REPOSITORY} @ {VENDOR_COMMIT} {VENDOR_PROMPT_SOURCE}"
            )


_seal()
