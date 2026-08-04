"""A bounded, door-private PDF page renderer, built on `pypdfium2`.

**PDF is admitted, rasterised per page** (ruling 2026-08-04, item 3). Tyrel's own
scanning is an iPhone converted to a high-resolution PDF; the rest of the corpus is
archival, microfilm and public-source documents. "This pipeline should handle PDFs
too, it's not a hard process. PDFs are images."

**What was wrong before, and why the fix is a library, not a bigger parser.** The
walking skeleton's renderer read a page's `/Resources/XObject` dictionary, found
exactly one `/Image` entry, and returned that object's decoded bytes — it never
opened `/Contents`, never asked whether anything actually painted that image, and
could not tell a scanned page apart from a page that draws text or vector marks
*beside* the one image it carries. On such a page it sealed the image and lost
everything else: GOALS 1 failing in the one place it must not. That is embedded-image
*extraction*, and no amount of hardening it — more filters, more colour spaces, more
named limits — fixes the actual defect, because the defect is architectural: it
never rendered the page at all.

**The route is page rasterisation.** Render the whole page to pixels, once, exactly
as a human eye or a photocopier would see it — text, vector marks and images alike,
painted together — and seal that. The old pipeline did exactly this, read through
the window as understanding rather than copied: `build_inbox_manifest.py` counted
pages at the door without rasterising; `run_batch.py`'s `render_contract` rasterised
each page once and persisted it; `source_page.py` warned downstream never to
re-render. That shape maps directly onto this repository's door/Exemplar split.

**`pypdfium2` is the library, cleared to enter by name** (ruling 2026-08-04, item 9:
"Zero dependencies is about the vlm models and major tools... I expect you to be
using basic tools and stuff as needed otherwise"). It is what the old pipeline
rasterised with, it is a thin binding onto the same PDFium engine Chrome ships, and
it goes through `pip-audit` in the gate like everything else. Writing a second
general-purpose PDF content-stream interpreter and rasteriser from `zlib`/`struct`
would be exactly the half-handling `common/imaging.py`'s docstring already refuses,
at far higher risk of a subtle rendering bug than a battle-tested library carries.

**What rasterisation removes, by construction rather than by more named limits.**
The walking skeleton refused rotation, non-classic cross-reference tables,
incremental updates, indexed/CMYK/ICCBased colour, `/Decode` remapping, chained
image filters, and any page carrying more than one XObject — every one of those is
a fact about how a page's *content stream* paints, and rendering the page correctly
handles all of them without this module ever needing to know they exist. What
remains bounded is genuinely irreducible: a document PDFium cannot open at all
(garbage bytes, or an encryption password this pipeline has no mechanism to
supply), and a declared page size or count large enough to need a resolution or
page-count ceiling before anything is rendered.

**Bounded before rendering, never after.** A page's declared size in PDF points is
read (`PdfPage.get_size()`) without decoding anything, and the target resolution is
capped so the rendered bitmap can never exceed this project's admission pixel
bounds (`image_formats.MAX_DIMENSION`, `MAX_PIXELS`) — the same "bound from the
header, before the decode" shape every other validator in this stage uses. A page
whose declared size is degenerate even at the floor DPI is refused by name rather
than rasterised at a resolution nobody could read.

**Lossless, always.** The rendered bitmap is encoded as PNG
(`image_formats.encode_png`) — never a lossy re-encode — because the point of
rasterising is to stop losing pixels between Tyrel's disk and the reader's eye, not
to trade one loss for another.

**Door-private by design.** Nothing outside `pipeline/1_exemplar/door.py` imports
this module. Rendering happens once, at admission, and the render module having no
API a later stage could call is what makes "no later stage may re-render" true by
construction rather than by convention (spec 03, test 4).
"""

from typing import Final, NamedTuple

import pypdfium2 as pdfium

import image_formats
from admission import RefusalReason, RenderRefusal

# One scanned register volume is hundreds of pages, not hundreds of thousands. A
# page count read out of an untrusted file is a loop bound an attacker writes, so
# it is bounded here before any page is opened.
MAX_PAGES: Final = 5_000

# The resolution this module renders at, and the floor it will not render below.
# 400 DPI matches "very high resolution" scanning for an already-high-quality
# source; it is capped downward per page so a degenerate declared page size can
# never rasterise to more than this project's admission pixel bounds allow
# (`image_formats.MAX_DIMENSION`, `MAX_PIXELS`) — never capped *upward*, because
# a huge legitimate page is better captured at reduced resolution than refused
# outright (GOALS 1: a missed act is worse than a poorly read act). Only a page
# so degenerate that even the floor DPI would exceed the bounds is refused.
TARGET_DPI: Final = 400
MIN_DPI: Final = 72


class PdfRefusal(RenderRefusal):
    """One PDF page, or the whole document, refused with a closed-set reason."""


class PdfDocument(NamedTuple):
    """One PDF opened once: the native handle and its bounded page count.

    Opened once per file rather than once per page, and kept open across every
    `render_page` call for it — `door.py` calls this once per declared source and
    renders every one of its pages from the same parse, so an N-page scan costs
    one open rather than N. `close_document` releases the native handle once
    every page has been rendered.
    """

    handle: pdfium.PdfDocument
    page_count: int


def _classify_open_failure(error: Exception) -> RefusalReason:
    """A password/encryption failure is a documented limit; anything else is not
    a genuine PDF at all.

    This pipeline has no mechanism to prompt for or supply a password, so an
    encrypted PDF is a real, named gap — the same shape as a format this project
    has no reader for yet — rather than damage. Everything else PDFium refuses to
    open is treated as corrupt: PDFium is the hardened parser here, and a
    document it cannot make sense of at all is not a variant this module can name
    more precisely than that.
    """
    text = str(error).lower()
    if "password" in text or "encrypt" in text:
        return RefusalReason.UNSUPPORTED_VARIANT
    return RefusalReason.CORRUPT


def open_document(data: bytes) -> PdfDocument:
    """Open one PDF and bound its page count. Raises `PdfRefusal` naming why if it
    will not."""
    try:
        handle = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise PdfRefusal(
            _classify_open_failure(error), f"the PDF could not be opened: {error}"
        ) from error
    try:
        page_count = len(handle)
    except pdfium.PdfiumError as error:
        handle.close()
        raise PdfRefusal(
            RefusalReason.CORRUPT, f"the PDF's page count could not be read: {error}"
        ) from error
    if page_count <= 0:
        handle.close()
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF declares no pages")
    if page_count > MAX_PAGES:
        handle.close()
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the PDF declares {page_count} pages, past the {MAX_PAGES}-page admission limit",
        )
    return PdfDocument(handle, page_count)


def close_document(document: PdfDocument) -> None:
    """Release the native handle once every page a caller wants has been rendered."""
    document.handle.close()


def count_pages(data: bytes) -> int:
    """How many pages this PDF declares, without rendering any of them."""
    document = open_document(data)
    try:
        return document.page_count
    finally:
        close_document(document)


def _bounded_scale(width_pt: float, height_pt: float, page_index: int) -> float:
    """The render scale (pixels per PDF point) for one page, capped to this
    project's admission pixel bounds and never above them.

    Capped downward from `TARGET_DPI`, proportionally, so an oversized page keeps
    its aspect ratio rather than being refused; refused only when even the
    `MIN_DPI` floor would still exceed the bounds, which means the page's own
    declared size is degenerate rather than merely large.
    """
    if width_pt <= 0 or height_pt <= 0:
        raise PdfRefusal(
            RefusalReason.CORRUPT,
            f"page {page_index} declares a non-positive size ({width_pt}x{height_pt} points)",
        )
    target = TARGET_DPI / 72
    dimension_cap = image_formats.MAX_DIMENSION / max(width_pt, height_pt)
    pixel_cap = (image_formats.MAX_PIXELS / (width_pt * height_pt)) ** 0.5
    scale = min(target, dimension_cap, pixel_cap)
    if scale * 72 < MIN_DPI:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"page {page_index} is {width_pt}x{height_pt} points; even at the "
            f"{MIN_DPI}-DPI floor it would exceed the admission's pixel bounds "
            f"({image_formats.MAX_DIMENSION} per side, {image_formats.MAX_PIXELS} pixels)",
        )
    return scale


def render_page(document: PdfDocument, page_index: int) -> tuple[bytes, str]:
    """Render one page (0-based) to standalone image bytes and their format name.

    Raises `PdfRefusal` naming exactly why when the page cannot be rasterised —
    never a guess, never a partial image. The whole page is rendered — every mark
    on it, text and image alike — so "a page carrying text beside an image" is
    simply what gets rasterised, not a case this module has to recognise.
    """
    if not (0 <= page_index < document.page_count):
        raise PdfRefusal(
            RefusalReason.CORRUPT,
            f"page index {page_index} is out of range for {document.page_count} pages",
        )
    try:
        page = document.handle[page_index]
    except pdfium.PdfiumError as error:
        raise PdfRefusal(
            RefusalReason.CORRUPT, f"page {page_index} could not be opened: {error}"
        ) from error
    try:
        width_pt, height_pt = page.get_size()
        scale = _bounded_scale(width_pt, height_pt, page_index)
        try:
            bitmap = page.render(scale=scale, rev_byteorder=True)
        except pdfium.PdfiumError as error:
            raise PdfRefusal(
                RefusalReason.CORRUPT, f"page {page_index} could not be rendered: {error}"
            ) from error
        try:
            if bitmap.mode != "RGB":
                raise PdfRefusal(
                    RefusalReason.CORRUPT,
                    f"page {page_index} rendered as pixel mode {bitmap.mode!r} rather "
                    "than RGB",
                )
            png_bytes = image_formats.encode_png(
                bitmap.width, bitmap.height, 2, 3, bytes(bitmap.buffer)
            )
        finally:
            bitmap.close()
    finally:
        page.close()
    return png_bytes, "png"
