"""dots.mocr's layout grammar: its prompt, its answer read as layout cells, and its text view.

dots.mocr (`dots-studio/dots.mocr`, adapter `dots-mocr.v1`) is asked the
vendor's own `prompt_layout_all_en` behind the vendor's image placeholder, in
one user turn after the image, as `dots_mocr/model/inference.py::
inference_with_vllm` asks it. It answers one JSON list of layout cells in
reading order, `{"bbox": [x1, y1, x2, y2], "category": ..., "text": ...}`, the
box in pixels of the image the model's processor saw.

Read here, never repaired: an answer that is not UTF-8, not JSON (a loop cut
at the token bound leaves broken JSON) or not a list of objects is not
parsed. The vendor's own `OutputCleaner` salvages such an answer; that is a
repair this pipeline does not make (ARCHITECTURE invariant 10), so the attempt
fails with the answer retained.

**Text view** (`dots-layout-text.v1`), the bake-off's rule
(`operations/bakeoff/native/dots_mocr.py::cells_lines`), per cell, in the
model's order: a `Picture` cell carries no text; a `Table` cell's HTML gives one
line per row, its cells joined by a space; a `Formula` cell's LaTeX is kept
without `$$`; every other cell is Markdown, read with heading marks, bullets,
`**`/`__`/`~~`/`$$` and backslash escapes removed. Within a cell, whitespace
runs become one space and empty lines are dropped. A cell's text is its lines
joined by newlines; the page text joins the non-empty cell texts by newlines.

**Geometry.** The model's processor resizes the image it is sent with
Qwen2-VL's `smart_resize` (a 28-pixel grid, between the snapshot's 3,136 and
11,289,600 pixels), and the boxes are in that resized image. A box is mapped to
sealed-page pixels by scaling each axis back, low edges floored and far edges
ceiled (`dots-smart-resize-floor-ceil.v1`). A box that is malformed, empty or
outside the resized image is kept as reported in the retained bytes and given
no page box.

Provenance: `github.com/rednote-hilab/dots.mocr` at
`23f3e5612fb8066d4034d5ecfc8f33a9243533eb`, `dots_mocr/utils/prompts.py`
(`dict_promptmode_to_prompt["prompt_layout_all_en"]`) and
`dots_mocr/model/inference.py` (the placeholder); weights
`dots-studio/dots.mocr` at `e539fbb52280393adc081b289ec597430a0f9031`. The
prompt's SHA-256 equals the one the bake-off's dots.mocr arm recorded on every
request it sent the real model (`operations/bakeoff/cards/dots-mocr.md`).
"""

from __future__ import annotations

import json
import math
import re
from fractions import Fraction
from html import unescape
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal

ADAPTER: Final = "dots-mocr.v1"
PARSER: Final = "layout-json"
TEXT_VIEW: Final = "dots-layout-text.v1"
RETIRED_TEXT_VIEWS: Final = frozenset()
QUANTIZATION_RULE: Final = "dots-smart-resize-floor-ceil.v1"

IMAGE_PLACEHOLDER: Final = "<|img|><|imgpad|><|endofimg|>"
LAYOUT_PROMPT: Final = (
    "Please output the layout information from the PDF image, including each layout "
    "element's bbox, its category, and the corresponding text content within the bbox.\n"
    "\n"
    "1. Bbox format: [x1, y1, x2, y2]\n"
    "\n"
    "2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', "
    "'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', "
    "'Text', 'Title'].\n"
    "\n"
    "3. Text Extraction & Formatting Rules:\n"
    "    - Picture: For the 'Picture' category, the text field should be omitted.\n"
    "    - Formula: Format its text as LaTeX.\n"
    "    - Table: Format its text as HTML.\n"
    "    - All Others (Text, Title, etc.): Format their text as Markdown.\n"
    "\n"
    "4. Constraints:\n"
    "    - The output text must be the original text from the image, with no translation.\n"
    "    - All layout elements must be sorted according to human reading order.\n"
    "\n"
    "5. Final Output: The entire output must be a single JSON object.\n"
)
LAYOUT_PROMPT_SHA256: Final = "16ff71ac5d218f35e5b3db41240b6e70741498bc099db3fa922ce1ff972e3b2f"
PROMPT_PROVENANCE: Final = {
    "repository": "github.com/rednote-hilab/dots.mocr",
    "sha": "23f3e5612fb8066d4034d5ecfc8f33a9243533eb",
    "symbol": 'dict_promptmode_to_prompt["prompt_layout_all_en"]',
}
#: The answer bound the vendor's own command line asks for (`dots_mocr/parser.py`).
MAX_COMPLETION_TOKENS: Final = 16_384
CATEGORIES: Final = frozenset(
    {
        "Caption",
        "Footnote",
        "Formula",
        "List-item",
        "Page-footer",
        "Page-header",
        "Picture",
        "Section-header",
        "Table",
        "Text",
        "Title",
    }
)
#: The grammar's own findings, re-derived from the retained bytes.
FINDING_KINDS: Final = frozenset({"malformed-bbox", "unknown-category", "text-not-string"})
#: The largest answer read; a larger one is retained unparsed and says so.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
# Qwen2-VL's `smart_resize`, with the snapshot's processor bounds.
GRID_PX: Final = 28
MIN_PIXELS: Final = 3_136
MAX_PIXELS: Final = 11_289_600

if digest_bytes(LAYOUT_PROMPT.encode("utf-8")) != LAYOUT_PROMPT_SHA256:  # pragma: no cover
    raise SchemaRefusal("the carried dots.mocr layout prompt is not the vendor's bytes")


def prompt_text() -> str:
    """The one text part of the request: the vendor's placeholder, then its prompt."""
    return IMAGE_PLACEHOLDER + LAYOUT_PROMPT


# --- the text view ------------------------------------------------------------------

_ROW_BREAK = re.compile(r"<\s*(/\s*tr|br|/\s*p|/\s*caption|/\s*h[1-6]|/\s*li)\b[^>]*>", re.I)
_CELL = re.compile(r"<\s*/?\s*t[dh]\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_MARK = re.compile(r"(?<!\\)(\*\*|__|~~|\$\$)")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!$|])")


def _html_lines(text: str) -> list[str]:
    rows = _ROW_BREAK.sub("\n", text)
    return unescape(_TAG.sub("", _CELL.sub(" ", rows))).splitlines()


def _markdown_lines(text: str) -> list[str]:
    lines = []
    for line in _IMAGE.sub("", text).splitlines():
        line = _BULLET.sub("", _HEADING.sub("", line))
        lines.append(_ESCAPE.sub(r"\1", _MARK.sub("", line)))
    return lines


def _plain(lines: list[str]) -> str:
    cleaned = (" ".join(line.split()) for line in lines)
    return "\n".join(line for line in cleaned if line)


def cell_text(category: Any, text: str) -> str:
    """One cell's text under `dots-layout-text.v1`."""
    if category == "Picture":
        return ""
    if category == "Table":
        return _plain(_html_lines(text))
    if category == "Formula":
        return _plain(text.replace("$$", "").splitlines())
    return _plain(_markdown_lines(text))


# --- the grammar ----------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


def parse_layout(raw: bytes) -> dict[str, Any]:
    """One dots.mocr answer as `{state, ...}`: `parsed`, `failed` or `unrecognized-shape`.

    `parsed` carries `cells` (each `{ordinal, bbox, category, text}` in the
    model's order: `bbox` the four reported numbers or `None` when malformed,
    `text` the cell's text view), `text` (the page text), `findings` and
    `view`. `failed` and `unrecognized-shape` carry `reason`.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise SchemaRefusal("a dots.mocr answer is not bytes")
    if len(raw) > MAX_RESPONSE_BYTES:
        return {
            "state": "failed",
            "reason": (
                "dots.mocr response exceeds the retained parsing limit of "
                f"{MAX_RESPONSE_BYTES} bytes (received {len(raw)})"
            ),
        }
    try:
        decoded = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        return {"state": "failed", "reason": f"the dots.mocr answer is not UTF-8 text: {error}"}
    try:
        answer = json.loads(decoded)
    except (json.JSONDecodeError, RecursionError) as error:
        return {
            "state": "failed",
            "reason": (
                f"the dots.mocr answer is not JSON ({error}); a broken answer is retained, "
                "never salvaged"
            ),
        }
    if not isinstance(answer, list):
        return {
            "state": "unrecognized-shape",
            "reason": (
                f"the dots.mocr answer is a JSON {type(answer).__name__}, not the list of "
                "layout cells its grammar answers"
            ),
        }
    cells: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for ordinal, cell in enumerate(answer):
        if not isinstance(cell, dict):
            return {
                "state": "unrecognized-shape",
                "reason": f"dots.mocr layout cell {ordinal} is not a JSON object",
            }
        bbox = cell.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4 and all(map(_is_number, bbox))):
            findings.append({"kind": "malformed-bbox", "cell": ordinal})
            bbox = None
        category = cell.get("category")
        if category not in CATEGORIES:
            findings.append({"kind": "unknown-category", "cell": ordinal})
        text = cell.get("text", "")
        if not isinstance(text, str):
            findings.append({"kind": "text-not-string", "cell": ordinal})
            text = ""
        cells.append(
            {
                "ordinal": ordinal,
                "bbox": bbox,
                "category": category if isinstance(category, str) else None,
                "text": cell_text(category, text),
            }
        )
    return {
        "state": "parsed",
        "cells": cells,
        "text": "\n".join(cell["text"] for cell in cells if cell["text"]),
        "findings": findings,
        "view": TEXT_VIEW,
    }


def cell_spans(cells: list[dict[str, Any]]) -> list[dict[str, int] | None]:
    """Each cell's `[start, end)` span in the page text, `None` for a cell with no text."""
    spans: list[dict[str, int] | None] = []
    cursor, wrote = 0, False
    for cell in cells:
        if not cell["text"]:
            spans.append(None)
            continue
        if wrote:
            cursor += 1
        spans.append({"start": cursor, "end": cursor + len(cell["text"])})
        cursor += len(cell["text"])
        wrote = True
    return spans


# --- geometry -----------------------------------------------------------------------


def smart_resize(height: int, width: int) -> tuple[int, int]:
    """The `(height, width)` the model's processor resizes a `height` x `width` image to."""
    if height <= 0 or width <= 0:
        raise SchemaRefusal("a dots.mocr page has no positive size")
    h_bar = max(GRID_PX, round(height / GRID_PX) * GRID_PX)
    w_bar = max(GRID_PX, round(width / GRID_PX) * GRID_PX)
    if h_bar * w_bar > MAX_PIXELS:
        beta = math.sqrt((height * width) / MAX_PIXELS)
        h_bar = max(GRID_PX, math.floor(height / beta / GRID_PX) * GRID_PX)
        w_bar = max(GRID_PX, math.floor(width / beta / GRID_PX) * GRID_PX)
    elif h_bar * w_bar < MIN_PIXELS:
        beta = math.sqrt(MIN_PIXELS / (height * width))
        h_bar = math.ceil(height * beta / GRID_PX) * GRID_PX
        w_bar = math.ceil(width * beta / GRID_PX) * GRID_PX
    return h_bar, w_bar


def cell_page_bounds(bbox: Any, page_size: tuple[int, int]) -> dict[str, int] | None:
    """One cell's sealed-page rectangle, or `None` when its box places nothing on the page.

    `page_size` is the sealed page's `(width, height)`, the image the chair is
    sent. A box with its far edge not beyond its near edge, or outside the
    resized image the model saw, is `None`: nothing is clamped.
    """
    if bbox is None:
        return None
    width, height = page_size
    seen_h, seen_w = smart_resize(height, width)
    x1, y1, x2, y2 = (Fraction(value) for value in bbox)
    if not (0 <= x1 < x2 <= seen_w and 0 <= y1 < y2 <= seen_h):
        return None
    left = math.floor(x1 * width / seen_w)
    top = math.floor(y1 * height / seen_h)
    right = math.ceil(x2 * width / seen_w)
    bottom = math.ceil(y2 * height / seen_h)
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}
