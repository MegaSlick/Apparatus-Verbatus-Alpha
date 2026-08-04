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
from image_formats import MAX_DIMENSION, MAX_PIXELS, MAX_PNG_DECODED_BYTES
from PIL import Image

PDF_SIGNATURE: Final = b"%PDF-"
MAX_PAGES: Final = 5_000
# **`RENDER_DPI` is an unmeasured choice standing in for a measurement, and it
# should be read as one.** Both lanes picked a number without measuring anything and
# both said so; 300 is the archival scanning convention and 400 is nearer what "very
# high resolution" iPhone-to-PDF output actually carries. 400 is sealed here because
# the cost of the two errors is not symmetric: too much resolution costs disk and
# render time, too little costs a reading of a faint secretary hand that no later
# stage can recover, and GOALS 1 says which of those is worse. It should be checked
# against a real sample once the data-handling gate is approved (GOVERNANCE 9,
# "prove before scale"), and it is the kind of number that moves after that check.
RENDER_DPI: Final = 400
# The floor this module will not render below. A page is capped *downward* toward
# it rather than refused for being large: a huge legitimate page captured at reduced
# resolution is a poorly read act, and refusing it outright is a missed one. Only a
# page whose declared size is degenerate even at the floor refuses.
MIN_RENDER_DPI: Final = 72
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
    """The complete PDFium recipe bound before a page is ever rasterised.

    `dpi` here is the *target*. A page too large to render at it is rendered at the
    largest resolution that fits this project's pixel bounds instead of being
    refused, so the recipe alone does not say what a given page's pixels are — the
    per-page contract `render_page` returns carries the resolution actually used.
    """
    return {
        "renderer": "pypdfium2",
        "renderer_version": str(pdfium.PYPDFIUM_INFO),
        "pdfium_version": str(pdfium.PDFIUM_INFO),
        "dpi": RENDER_DPI,
        "min_dpi": MIN_RENDER_DPI,
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
            _open_failure_code(error), f"PDFium could not open the document: {error}"
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


# PDFium's own load-error codes for the two cases that mean "locked", not "broken":
# an incorrect or absent password, and a security handler PDFium does not implement.
# `pypdfium2` puts the code on the exception, so this is read rather than inferred.
PDFIUM_INCORRECT_PASSWORD: Final = 4
PDFIUM_UNSUPPORTED_SECURITY_SCHEME: Final = 5
LOCKED_NOT_BROKEN: Final = frozenset(
    {PDFIUM_INCORRECT_PASSWORD, PDFIUM_UNSUPPORTED_SECURITY_SCHEME}
)


def _open_failure_code(error: Exception) -> RefusalReason:
    """An encrypted PDF is a gap we own; anything else PDFium refuses is damage.

    Lane B's distinction, kept — nothing in this pipeline can prompt for or supply
    a password, so a password-protected scan is a real, named thing this project
    cannot yet read, the same shape as a format with no reader. Calling it `CORRUPT`
    would tell Tyrel his original was damaged when it is intact and merely locked,
    which is a different decision for him entirely.

    **Read from the error code, not from the error text.** Lane B matched the words
    "password" or "encrypt" in the message. PDFium has two locked-document codes and
    only one of them says "password": the other renders as "Unsupported security
    scheme error", which contains neither word, so an AES-256 or otherwise
    unimplemented handler — an intact file — was being reported as damaged. The code
    is on the exception and says which case it is without a guess.
    """
    code = getattr(error, "err_code", None)
    if code in LOCKED_NOT_BROKEN:
        return RefusalReason.UNSUPPORTED_VARIANT
    return RefusalReason.CORRUPT


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
        dpi, expected_width, expected_height = _render_dimensions(points_wide, points_high)
        bitmap = page.render(
            scale=dpi / POINTS_PER_INCH,
            draw_annots=DRAW_ANNOTATIONS,
            may_draw_forms=DRAW_FORMS,
            fill_color=(255, 255, 255, 255),
            prefer_bgrx=False,
        )
        width, height = bitmap.width, bitmap.height
        # PDFium rounds each dimension itself, so the bitmap may differ from the
        # arithmetic above by a pixel. What may never differ is the bound: this is
        # the check on the pixels that actually exist, rather than on the ones that
        # were predicted.
        if abs(width - expected_width) > 1 or abs(height - expected_height) > 1:
            raise PdfRefusal(
                RefusalReason.CORRUPT,
                "PDFium rendered dimensions that disagree with the declared page geometry",
            )
        if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the PDF page rendered to {width}x{height}, above the "
                f"{MAX_DIMENSION}-per-side and {MAX_PIXELS}-pixel limits",
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
            # What this page was *actually* rendered at, which is the target unless
            # the page was too large for it. Without this the sealed record could
            # not say how these pixels were made, and ARCHITECTURE's invariant 3 —
            # the image a model saw is reproducible from the Exemplar plus the
            # recorded transforms — would hold only for pages that happened to fit.
            "effective_dpi": dpi,
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


def _render_dimensions(points_wide: float, points_high: float) -> tuple[int, int, int]:
    """Choose the whole DPI for one page and the pixel size it will produce.

    Capped downward from the target, never upward, and refusing only a page so
    degenerate that even `MIN_RENDER_DPI` would still exceed the bounds. Lane B
    found the two reasons the budget is not simply `MAX_PIXELS`, and both are real:
    PDFium rounds width and height independently, so a scale chosen to land exactly
    on the analytic limit can still produce a bitmap a pixel over it; and the render
    is always RGB, so the PNG it becomes is bound by `MAX_PNG_DECODED_BYTES` at
    three bytes a pixel, which is tighter. `admission.inspect_source` re-checks
    exactly that PNG one step later, and a page that passed here only to refuse
    there would be a worse answer than choosing the smaller resolution up front.
    """
    if not isinstance(points_wide, (int, float)) or not isinstance(points_high, (int, float)):
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF page has no numeric dimensions")
    if points_wide <= 0 or points_high <= 0:
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF page has a zero or negative dimension")
    budget = min(MAX_PIXELS, MAX_PNG_DECODED_BYTES // 3) * 0.99
    allowed = min(
        RENDER_SCALE,
        MAX_DIMENSION / max(points_wide, points_high),
        (budget / (points_wide * points_high)) ** 0.5,
    )
    # Rounded down to a whole DPI, and that is not cosmetic. The square root above
    # is a float whose last bit is not guaranteed identical across platforms or
    # library versions, and a scale that differs in its last bit renders different
    # pixels, which would make the same page seal a different blob on a different
    # machine. A whole DPI is exactly representable, exactly recordable in a
    # canonical artifact — which carries integers, never floats — and reproducible.
    dpi = int(allowed * POINTS_PER_INCH)
    if dpi < MIN_RENDER_DPI:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the PDF page is {points_wide}x{points_high} points; even at the "
            f"{MIN_RENDER_DPI}-DPI floor it would exceed the {MAX_DIMENSION}-per-side "
            f"and {MAX_PIXELS}-pixel limits",
        )
    scale = dpi / POINTS_PER_INCH
    return dpi, ceil(points_wide * scale), ceil(points_high * scale)


def _lossless_png(image: Image.Image) -> bytes:
    """Encode a page bitmap once with explicit, lossless PNG settings."""
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
