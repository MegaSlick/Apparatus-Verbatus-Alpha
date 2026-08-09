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
from pathlib import Path
from typing import Any, BinaryIO, Final, NamedTuple

import admission
import pypdfium2 as pdfium
from admission import RefusalReason
from image_formats import MAX_DIMENSION, MAX_PIXELS, MAX_PNG_DECODED_BYTES
from PIL import Image
from render_config import PdfRenderSettings

PDF_SIGNATURE: Final = b"%PDF-"
PDF_HEADER_PREFIX_BYTES: Final = 1024
# The floor this module will not render below. A page is capped *downward* toward
# it rather than refused for being large: a huge legitimate page captured at reduced
# resolution is a poorly read act, and refusing it outright is a missed one. Only a
# page whose declared size is degenerate even at the floor refuses.
MIN_RENDER_DPI: Final = 72
POINTS_PER_INCH: Final = 72
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
    """A native document handle and its declared page count, owned by the door."""

    document: Any
    page_count: int


def renderer_recipe(settings: PdfRenderSettings) -> dict[str, Any]:
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
        "configured_target_dpi": settings.configured_target_dpi,
        "dpi": settings.target_dpi,
        "min_dpi": MIN_RENDER_DPI,
        "scale": {"numerator": settings.target_dpi, "denominator": POINTS_PER_INCH},
        "output": {"codec": RENDER_CODEC, "color_mode": RENDER_COLOR_MODE},
        "background": RENDER_BACKGROUND,
        "draw_annotations": DRAW_ANNOTATIONS,
        "draw_forms": DRAW_FORMS,
    }


def open_document(source: bytes | str | Path | BinaryIO) -> OpenPdf:
    """Open a PDF source, counting pages without rasterising it.

    The real Door gives PDFium an already-open, descriptor-anchored binary stream.
    PDFium then reads the document itself instead of the Door allocating one bytes
    object the size of a complete reel, and the source that was hashed is the same
    source PDFium renders.  A bare path would not do: `pypdfium2` resolves and
    reopens a path itself (`PdfDocument.__init__` calls `Path.resolve()`, confirmed
    in the installed source), which is exactly the second, symlink-following open
    the anchored stream exists to avoid.  Synthetic callers can still supply
    in-memory bytes or a local path directly; all forms use the same renderer and
    page contract.
    """
    header = _pdf_header(source)
    if PDF_SIGNATURE not in header:
        raise PdfRefusal(RefusalReason.CORRUPT, "the source has no PDF header")
    document = None
    try:
        document = pdfium.PdfDocument(source)
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
        # Preserve the primary corrupt-document alarm if cleanup also fails.
        _close_native_document(document)
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF contains no pages")
    try:
        _refuse_implausible_page_count(pages, _source_size(source))
    except PdfRefusal:
        _close_native_document(document)
        raise
    return OpenPdf(document, pages)


# `/Pages` nodes may share one child across several `/Kids`, so PDFium's page count
# trusts a compounding `/Count` rather than the distinct page objects on disk: a ~2KB
# file of self-sharing nodes opens cleanly and reports 500,000+ pages. This floor is
# not the page cap ruling 17 retired — a reel's count stays the document's to declare.
# It is also not a corruption test; see `_refuse_implausible_page_count`.
MIN_BYTES_PER_DECLARED_PAGE: Final = 32


def _source_size(source: bytes | str | Path | BinaryIO) -> int:
    """Byte length of a PDF source, however it is represented.

    For the page-count plausibility check only — never for reading a single byte of
    page content. A stream's position is restored to the start, matching the
    invariant `_pdf_header` already established before PDFium opens it.
    """
    if isinstance(source, bytes):
        return len(source)
    if isinstance(source, (str, Path)):
        return Path(source).stat().st_size
    source.seek(0, 2)
    size = source.tell()
    source.seek(0)
    return size


def _refuse_implausible_page_count(pages: int, container_size: int) -> None:
    """Hold a declared page count this reader cannot tell from a shared page tree.

    `UNSUPPORTED_VARIANT`, not `CORRUPT`, and the distinction is the whole point.
    A blind audit built a *valid* PDF 1.5 — 10,000 distinct page objects, true
    `/Count`, no shared kids, packed into a Flate object stream — at 9.4 bytes per
    page, and PDFium opens and renders it. So this ratio does not establish damage,
    and `CORRUPT` would tell Tyrel his original is broken when it is not: the one
    thing ruling 2 says a refusal must never do. What it does establish is that this
    reader cannot yet distinguish that file from the page-tree bomb it is here to
    stop, which is a gap in this pipeline and is recorded as one.
    """
    if container_size < pages * MIN_BYTES_PER_DECLARED_PAGE:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the document declares {pages} pages in {container_size} bytes, under the "
            f"{MIN_BYTES_PER_DECLARED_PAGE} bytes a page normally needs; this reader "
            "cannot yet tell a densely packed document from a shared page tree, so it "
            "is held rather than read",
        )


# Exactly what `pypdfium2.internal.is_stream` requires of a custom-buffer source,
# read from the installed package rather than guessed. Matching its set precisely,
# rather than a convenient subset, is what keeps a near-stream from passing this
# module's own check and then failing inside `_open_pdf` as a bare `TypeError`
# nothing here catches — a refusal with no name is a refusal that is lost.
_STREAM_METHODS: Final = ("read", "seek", "tell", "readinto")


def _is_stream(source: object) -> bool:
    return all(callable(getattr(source, name, None)) for name in _STREAM_METHODS)


def _pdf_header(source: bytes | str | Path | BinaryIO) -> bytes:
    """Read only a bounded PDF prefix, never a complete streamed source."""
    if isinstance(source, bytes):
        return source[:PDF_HEADER_PREFIX_BYTES]
    if isinstance(source, (str, Path)):
        try:
            with Path(source).open("rb") as handle:
                return handle.read(PDF_HEADER_PREFIX_BYTES)
        except OSError as error:
            raise PdfRefusal(
                RefusalReason.UNREADABLE, f"the PDF source could not be read: {error}"
            ) from error
    if not _is_stream(source):
        raise PdfRefusal(
            RefusalReason.CORRUPT, "the PDF source is neither bytes, a path, nor a stream"
        )
    try:
        source.seek(0)
        header = source.read(PDF_HEADER_PREFIX_BYTES)
        source.seek(0)
    except (OSError, ValueError) as error:
        raise PdfRefusal(
            RefusalReason.UNREADABLE, f"the PDF source stream could not be read: {error}"
        ) from error
    if not isinstance(header, bytes):
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF source stream did not yield bytes")
    return header


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


def count_pages(source: bytes | str | Path | BinaryIO) -> int:
    """Count PDF pages without rasterising them, closing the temporary handle.

    Only the PDFium document this call opened is closed. A caller-owned stream
    (`operations.submit.inventory.open_submission_source`) stays open, because the
    caller still needs it: `pypdfium2` never closes a custom-buffer input unless
    asked to (`autoclose=False` is its default, confirmed in the installed source).
    """
    opened = open_document(source)
    try:
        return opened.page_count
    finally:
        close_document(opened)


def render_page(opened: OpenPdf, page_index: int, settings: PdfRenderSettings) -> RenderedPage:
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
        dpi, expected_width, expected_height = _render_dimensions(
            points_wide, points_high, settings.target_dpi
        )
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
            **renderer_recipe(settings),
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
    """Close an owned native handle, making a normal-path failure visible."""
    try:
        opened.document.close()
    except Exception as error:
        raise PdfRefusal(
            RefusalReason.UNREADABLE,
            f"PDFium could not release the document after reading: {error}",
        ) from error


def _close_native_document(document: Any) -> None:
    """Best-effort cleanup while preserving an open failure already in flight."""
    try:
        document.close()
    except Exception:
        pass


def _render_dimensions(
    points_wide: float, points_high: float, target_dpi: int
) -> tuple[int, int, int]:
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
    # Keep the configured integer out of floating-point conversion. An arbitrarily
    # large positive TOML integer must still cap downward to what this page can
    # hold, not overflow before the code-owned pixel limits get a say.
    allowed_dpi = min(
        target_dpi,
        MAX_DIMENSION * POINTS_PER_INCH / max(points_wide, points_high),
        (budget / (points_wide * points_high)) ** 0.5 * POINTS_PER_INCH,
    )
    # Rounded down to a whole DPI, and that is not cosmetic. The square root above
    # is a float whose last bit is not guaranteed identical across platforms or
    # library versions, and a scale that differs in its last bit renders different
    # pixels, which would make the same page seal a different blob on a different
    # machine. A whole DPI is exactly representable, exactly recordable in a
    # canonical artifact — which carries integers, never floats — and reproducible.
    dpi = int(allowed_dpi)
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
