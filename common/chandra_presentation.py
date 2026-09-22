"""Chandra's native whole-page image presentation, shared by both callers.

The pinned vendor pipeline converts an input page to RGB, applies
``chandra/model/util.py::scale_to_fit`` and serializes the result as PNG before
the image is put on the wire.  The Designator and Attestator 1 both serve that
same model, so they share this pixel operation rather than carrying two copies
that can drift.

Source: ``datalab-to/chandra`` at
``d4f7467435aa4137d9539f000ddf0b7ced3eb43f`` (Apache-2.0), specifically
``chandra/input.py`` and ``chandra/model/util.py``.  Dimension arithmetic is
ported in :mod:`common.imaging_ports`; the pixel operations use the repository's
sealed Pillow-LANCZOS implementation.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from common.imaging import convert_png_to_rgb, crop_png, resize_png_lanczos
from common.imaging_ports import scale_to_fit_chandra

PRESENT_OPERATION: Final = "chandra-scale-to-fit.v1"
PRESENT_COLOUR_MODE: Final = "rgb"
STRUCTURE_REQUEST_IMAGE_KIND: Final = "structure-request-image"
STRUCTURE_REQUEST_IMAGE_SCHEMA: Final = "designator-structure-request-image.v1"
STRUCTURE_REQUEST_IMAGE_FIELDS: Final = frozenset(
    {"schema", "page_id", "page_ordinal", "source_image_ref", "presented"}
)


def render_page(
    page_bytes: bytes, bounds: Mapping[str, int]
) -> tuple[bytes, tuple[int, int]]:
    """Return the exact RGB/grid-28 PNG Chandra's pipeline presents."""
    source_width, source_height = bounds["w"], bounds["h"]
    target = scale_to_fit_chandra(source_width, source_height)
    converted = convert_png_to_rgb(crop_png(page_bytes, dict(bounds)))
    return resize_png_lanczos(converted, *target), target


def presented_transform(
    page_id: str,
    page_ordinal: int,
    bounds: Mapping[str, int],
    target: tuple[int, int],
) -> dict[str, Any]:
    """Describe the shared native presentation in the executable vocabulary."""
    return {
        "operation": PRESENT_OPERATION,
        "source_page_id": page_id,
        "source_page_ordinal": page_ordinal,
        "bounds": dict(bounds),
        "colour_mode": PRESENT_COLOUR_MODE,
        "resize": {
            "resampler": "pillow-lanczos",
            "dimension_rounding": "grid-28",
            "source_width_px": bounds["w"],
            "source_height_px": bounds["h"],
            "target_width_px": target[0],
            "target_height_px": target[1],
        },
    }
