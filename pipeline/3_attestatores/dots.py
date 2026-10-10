"""dots.mocr's page-witness adapter: the vendor's prompt and message shape, its layout
grammar read as cells, and the sealed page shown as it is.

The grammar reader lives in `common/dots_layout.py` because dots.mocr captures
are re-derived from their retained blob at later stages, none of which may
import this module.

**What it is shown.** The sealed page itself, unchanged (`presented.kind ==
"page"`): the model's own processor converts it to RGB and resizes it on its
28-pixel grid. The vendor's command line first re-renders the page through
PyMuPDF at 200 dpi; that is its `--no_fitz_preprocess` option turned off, and
it is not reproduced here, because no sealed-page transform can replay
PyMuPDF's resampling. The bake-off ran the vendor's default (with the
re-render), so this path's reading of a page is not the one the bake-off
scored; `workbench/design/2026-10-09-postbakeoff/P3-dots-index-witness.md`
names that as unmeasured.

**What it reports.** Layout cells with boxes in the resized image's pixels,
mapped to sealed-page pixels by `dots_layout.cell_page_bounds`; each cell with
text is one page-feed unit (`common/page_witness_units.py`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

import feeding

from common import dots_layout
from common.contracts.errors import SchemaRefusal
from common.native_witness import dots_capture_view, validate_presented

ADAPTER: Final = dots_layout.ADAPTER
QUANTIZATION_RULE: Final = dots_layout.QUANTIZATION_RULE

#: Declared from the vendor grammar: every cell carries a box, so layout is
#: true; nothing in its categories or formatting rules marks doubt, so
#: uncertainty is false.
FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": False, "can_express_layout": True}
)


def prompt() -> dict[str, str]:
    """The vendor's one user turn: its image placeholder, then `prompt_layout_all_en`."""
    return dict(dots_capture_view()["prompt"])


def generation() -> dict[str, int]:
    """The answer bound the vendor's own command line sends, retained as evidence."""
    return dict(dots_capture_view()["generation"])


def parse(raw_response: bytes) -> Any:
    """The page text of one answer, or `{"parse_outcome": ...}` for a shape it does not know."""
    layout = dots_layout.parse_layout(raw_response)
    if layout["state"] == "parsed":
        return layout["text"]
    if layout["state"] == "unrecognized-shape":
        return {"parse_outcome": layout["reason"]}
    raise SchemaRefusal(layout["reason"])


def retain(
    context: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain one dots.mocr view under its own registry identity only."""
    return feeding.retain_model_view(
        context,
        adapter=ADAPTER,
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """The sealed whole page, shown as it is; nothing is cut, resized or converted here."""
    del context
    validate_presented(presentation)
    if presentation["kind"] != "page":
        raise SchemaRefusal("dots.mocr reads whole pages; it is never shown a crop of one")
    return dict(presentation)


def observe(
    presentation: dict[str, Any], native_payload: Any, *, page_size: tuple[int, int]
) -> list[dict[str, Any]]:
    """Each cell's sealed-page box, from the retained answer, with its span in the page text.

    `native_payload` is the answer's bytes. A cell with no text, or whose box
    places nothing on the page, reports no box. An answer the grammar does not
    parse reports none, and the stage then records the presentation echo.
    """
    validate_presented(presentation)
    if not isinstance(native_payload, (bytes, bytearray)):
        return []
    layout = dots_layout.parse_layout(bytes(native_payload))
    if layout["state"] != "parsed":
        return []
    observed = []
    for cell, span in zip(layout["cells"], dots_layout.cell_spans(layout["cells"]), strict=True):
        if span is None:
            continue
        bounds = dots_layout.cell_page_bounds(cell["bbox"], page_size)
        if bounds is None:
            continue
        observed.append(
            {"ordinal": len(observed), "bounds": bounds, "bounds_source": "native", "span": span}
        )
    return observed
