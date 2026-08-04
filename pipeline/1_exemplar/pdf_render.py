"""Door-private whole-page PDF rasterisation.

PDF pages are painted into one lossless PNG at the door.  This module deliberately
does not inspect image XObjects or parse page content itself: a page can carry text,
vectors, several images, clipping, rotation, annotations, and form appearances, and
the Exemplar must preserve what is visible rather than a convenient subset of it.

Only the submit door imports this module in production.  It counts pages before
fan-out, renders each page once, and persists the result as an Exemplar blob.  Later
stages receive those sealed pixels and have no renderer API to call.
"""

from __future__ import annotations

import warnings
from io import BytesIO
from math import ceil
from typing import Any, Final, NamedTuple

import admission
import pypdfium2 as pdfium
from admission import RefusalReason
from image_formats import MAX_DIMENSION, MAX_PIXELS
from PIL import Image

PDF_SIGNATURE: Final = b"%PDF-"
MAX_PAGES: Final = 5_000
RENDER_DPI: Final = 300
POINTS_PER_INCH: Final = 72
RENDER_SCALE: Final = RENDER_DPI / POINTS_PER_INCH
RENDER_SCALE_RECORD: Final = {"numerator": RENDER_DPI, "denominator": POINTS_PER_INCH}
RENDER_COLOR_MODE: Final = "RGB"
RENDER_CODEC: Final = "png"
RENDER_BACKGROUND: Final = "white"
DRAW_ANNOTATIONS: Final = True
DRAW_FORMS: Final = True


class PdfRefusal(ValueError):
    """A PDF cannot be counted or rendered, using the door's alarm vocabulary."""

    def __init__(self, code: RefusalReason, detail: str):
        self.reason = code
        self.detail = detail
        super().__init__(admission.reason(code, detail))


class RenderedPage(NamedTuple):
    """The one sealed image and the facts that made its pixels."""

    png_bytes: bytes
    width: int
    height: int
    contract: dict[str, Any]


class OpenPdf(NamedTuple):
    """A native document handle and its bounded page count, owned by the door."""

    document: Any
    page_count: int


def renderer_recipe() -> dict[str, Any]:
    """The complete PDFium recipe bound before a page is ever rasterised."""
    return {
        "renderer": "pypdfium2",
        "renderer_version": str(pdfium.PYPDFIUM_INFO),
        "pdfium_version": str(pdfium.PDFIUM_INFO),
        "dpi": RENDER_DPI,
        "scale": dict(RENDER_SCALE_RECORD),
        "output": {"codec": RENDER_CODEC, "color_mode": RENDER_COLOR_MODE},
        "background": RENDER_BACKGROUND,
        "draw_annotations": DRAW_ANNOTATIONS,
        "draw_forms": DRAW_FORMS,
    }


def open_document(data: bytes) -> OpenPdf:
    """Open bytes already selected by the door, counting pages without rendering."""
    if not data.startswith(PDF_SIGNATURE):
        raise PdfRefusal(RefusalReason.CORRUPT, "the source has no PDF header")
    document = None
    try:
        document = pdfium.PdfDocument(data)
    except (pdfium.PdfiumError, ValueError, OSError) as error:
        raise PdfRefusal(
            RefusalReason.CORRUPT, f"PDFium could not open the document: {error}"
        ) from error
    try:
        # PDFium only draws interactive fields after this is called, and its API
        # requires it immediately after construction, before a page handle or
        # page count is requested.  A form-init warning is not permitted to scroll
        # past as an unsealed terminal side effect: it is a named decoder gap.
        with warnings.catch_warnings():
            warnings.simplefilter("error", pdfium.PdfiumWarning)
            document.init_forms()
        pages = len(document)
    except pdfium.PdfiumWarning as error:
        _close_native_document(document)
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"PDFium could not initialise the PDF form environment: {error}",
        ) from error
    except (pdfium.PdfiumError, ValueError, OSError) as error:
        _close_native_document(document)
        raise PdfRefusal(
            RefusalReason.CORRUPT, f"PDFium could not prepare the document: {error}"
        ) from error
    if pages <= 0:
        close_document(OpenPdf(document, pages))
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF contains no pages")
    if pages > MAX_PAGES:
        close_document(OpenPdf(document, pages))
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the PDF contains {pages} pages, above the {MAX_PAGES}-page fan-out limit",
        )
    return OpenPdf(document, pages)


def count_pages(data: bytes) -> int:
    """Count PDF pages without rasterising them, closing the temporary handle."""
    opened = open_document(data)
    try:
        return opened.page_count
    finally:
        close_document(opened)


def render_page(opened: OpenPdf, page_index: int) -> RenderedPage:
    """Paint one complete page once and encode it losslessly as RGB PNG.

    The output dimensions are calculated and bounded before PDFium allocates a
    bitmap.  PDFium's own page semantics include content streams, transforms,
    rotation and visible annotations; no embedded object is ever extracted.
    """
    if not isinstance(page_index, int) or isinstance(page_index, bool):
        raise PdfRefusal(RefusalReason.CORRUPT, "the requested PDF page index is not an integer")
    if not 0 <= page_index < opened.page_count:
        raise PdfRefusal(
            RefusalReason.CORRUPT,
            f"requested PDF page {page_index} is outside 0..{opened.page_count - 1}",
        )

    page = bitmap = None
    try:
        page = opened.document[page_index]
        points_wide, points_high = page.get_size()
        width, height = _render_dimensions(points_wide, points_high)
        bitmap = page.render(
            scale=RENDER_SCALE,
            draw_annots=DRAW_ANNOTATIONS,
            may_draw_forms=DRAW_FORMS,
            fill_color=(255, 255, 255, 255),
            prefer_bgrx=False,
        )
        if bitmap.width != width or bitmap.height != height:
            raise PdfRefusal(
                RefusalReason.CORRUPT,
                "PDFium rendered dimensions that disagree with the declared page geometry",
            )
        image = bitmap.to_pil().convert(RENDER_COLOR_MODE)
        rendered = _lossless_png(image)
    except PdfRefusal:
        raise
    except (pdfium.PdfiumError, OSError, ValueError) as error:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"PDFium could not rasterise page {page_index}: {error}",
        ) from error
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()

    return RenderedPage(
        rendered,
        width,
        height,
        {
            **renderer_recipe(),
            "container_page_index": page_index,
            "width": width,
            "height": height,
        },
    )


def close_document(opened: OpenPdf) -> None:
    """Close an owned native handle without hiding the original failure."""
    _close_native_document(opened.document)


def _close_native_document(document: Any) -> None:
    """Best-effort cleanup for a handle that may have failed before opening fully."""
    try:
        document.close()
    except (AttributeError, pdfium.PdfiumError):
        pass


def _render_dimensions(points_wide: float, points_high: float) -> tuple[int, int]:
    """Convert page points to pixels, refusing implausible work before allocation."""
    if not isinstance(points_wide, (int, float)) or not isinstance(points_high, (int, float)):
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF page has no numeric dimensions")
    if points_wide <= 0 or points_high <= 0:
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF page has a zero or negative dimension")
    width, height = ceil(points_wide * RENDER_SCALE), ceil(points_high * RENDER_SCALE)
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the PDF page would render to {width}x{height}, above the "
            f"{MAX_DIMENSION}-per-side and {MAX_PIXELS}-pixel limits",
        )
    return width, height


def _lossless_png(image: Image.Image) -> bytes:
    """Encode a page bitmap once with explicit, lossless PNG settings."""
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
