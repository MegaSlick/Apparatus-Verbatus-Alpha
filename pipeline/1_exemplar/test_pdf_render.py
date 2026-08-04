"""The PDF page rasteriser, proven against hand-built PDF bytes.

Every PDF here is assembled by `synthetic_sources.PdfBuilder`/`image_page_pdf`,
never a checked-in binary — the ingress guard only allows png/jpeg/tiff media
types under `proof/fixtures/`, and a PDF fixture is exactly the kind of thing
GOVERNANCE's synthetic-material rule exists to keep out of git.

**What changed, and why this file is a rewrite rather than an extension.** The
walking skeleton's renderer read one image XObject out of a page's resource
dictionary and returned its bytes, never touching `/Contents` — so almost every
test in the old version of this file proved a *documented limit* of that approach:
rotation refused, chained filters refused, more than one XObject refused, an
unsupported colour space refused, encryption refused by a hand-rolled parser that
had to know what encryption looked like. Rasterisation via `pypdfium2` removes
every one of those by construction — the renderer paints the page and does not
care what shape its content stream takes — so those tests are gone, not adapted.
What remains bounded is genuinely irreducible: a document PDFium cannot open at
all, and a page whose declared size would need more pixels than this project
admits.
"""

import pdf_render
import pytest
from admission import RefusalReason
from image_formats import validate_png
from synthetic_sources import (
    PdfBuilder,
    gray_image,
    image_page_pdf,
    single_gray_page_pdf,
    two_page_pdf,
)


def render_page(data: bytes, page_index: int):
    """Render one page straight from bytes, opening and closing the document
    around it — the shape a test naming one page wants."""
    document = pdf_render.open_document(data)
    try:
        return pdf_render.render_page(document, page_index)
    finally:
        pdf_render.close_document(document)


# --- happy paths -----------------------------------------------------------------


def test_count_pages_on_a_single_page_document():
    assert pdf_render.count_pages(single_gray_page_pdf()) == 1


def test_a_multi_page_document_renders_distinct_pages_in_order():
    data = two_page_pdf(10, 8)
    assert pdf_render.count_pages(data) == 2
    document = pdf_render.open_document(data)
    try:
        first, first_format = pdf_render.render_page(document, 0)
        second, second_format = pdf_render.render_page(document, 1)
    finally:
        pdf_render.close_document(document)
    assert first_format == second_format == "png"
    assert first != second, "the two pages must render distinct bytes"


def test_a_page_carrying_text_beside_an_image_rasterises_both():
    """The case that proves the architecture changed (spec 03, test 4). The
    walking skeleton's renderer read one XObject and never `/Contents`, so a page
    drawing text beside an image lost the text. Rasterising the whole page paints
    both, because neither is special to a renderer that paints everything."""
    width, height = 40, 30
    image = gray_image(width, height, 200)
    data = image_page_pdf([image], text="Hi")
    rendered, page_format = render_page(data, 0)
    assert page_format == "png"
    geometry = validate_png(rendered)
    scale = pdf_render._bounded_scale(width, height, 0)
    # Within one pixel of the expected scale: pdfium's own rounding of a
    # fractional pixel count is not this module's contract to pin exactly.
    assert abs(geometry.width - width * scale) <= 1
    assert abs(geometry.height - height * scale) <= 1
    # A page filled entirely by one gray value, with black text drawn on top of
    # it, must show a third colour: the gray fill, black ink, and antialiased
    # blends between them are not the same as an untouched fill.
    colors = _rgb_pixel_colors(rendered, geometry.width, geometry.height)
    assert len(colors) > 2, "expected the gray fill, black text ink, and antialiasing blends"


def _rgb_pixel_colors(png_bytes: bytes, width: int, height: int) -> set[bytes]:
    """Every distinct RGB pixel value in a PNG this module encoded (filter 0,
    8-bit, no stride padding), read without depending on any decoder this module
    itself provides."""
    import struct
    import zlib

    offset = 8
    idat = b""
    while offset < len(png_bytes):
        (length,) = struct.unpack_from(">I", png_bytes, offset)
        tag = png_bytes[offset + 4 : offset + 8]
        chunk = png_bytes[offset + 8 : offset + 8 + length]
        if tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
        offset += 12 + length
    raw = zlib.decompress(idat)
    row_bytes = width * 3
    colors: set[bytes] = set()
    for row in range(height):
        start = row * (1 + row_bytes)
        assert raw[start] == 0
        pixels = raw[start + 1 : start + 1 + row_bytes]
        colors.update(pixels[index : index + 3] for index in range(0, row_bytes, 3))
    return colors


def test_rendering_the_same_page_twice_produces_identical_bytes():
    """Render-once-and-seal only means anything if the render is deterministic:
    two runs of one command must leave every byte unchanged."""
    data = single_gray_page_pdf()
    assert render_page(data, 0) == render_page(data, 0)


def test_a_rotated_page_renders_at_its_visual_orientation_rather_than_refusing():
    """Rasterisation is what removes rotation as a documented limit: pdfium
    applies the page's own `/Rotate` before this module ever sees pixels, so a
    90-degree-rotated page simply renders with width and height swapped, exactly
    matching `PdfPage.get_size()`'s own post-rotation report."""
    data = single_gray_page_pdf(width=12, height=8, rotate=90)
    rendered, page_format = render_page(data, 0)
    assert page_format == "png"
    geometry = validate_png(rendered)
    # The source is landscape (12 wide, 8 tall); rotated 90 degrees it renders
    # portrait — narrower than it is tall.
    assert geometry.width < geometry.height


# --- bounds: read from the header, never from a decode ---------------------------


def test_the_page_fan_out_is_bounded_before_it_runs():
    assert pdf_render.MAX_PAGES == 5_000


def test_a_declared_page_count_past_the_limit_refuses_before_any_page_is_opened(monkeypatch):
    monkeypatch.setattr(pdf_render, "MAX_PAGES", 1)
    with pytest.raises(pdf_render.PdfRefusal, match="page admission limit") as caught:
        pdf_render.open_document(two_page_pdf())
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT


def test_a_degenerate_page_size_refuses_rather_than_rasterising_at_no_resolution():
    """`_bounded_scale` caps resolution downward for a huge page rather than
    refusing it — GOALS 1: a missed act is worse than a poorly read one. Only a
    page so large that even the floor DPI would exceed the admission's pixel
    bounds is refused, which is what this MediaBox is sized to force."""
    huge_points = 20_000_000
    data = single_gray_page_pdf().replace(
        b"[0 0 4 3]", f"[0 0 {huge_points} {huge_points}]".encode()
    )
    with pytest.raises(pdf_render.PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "DPI floor" in str(caught.value)


def test_an_oversized_but_not_degenerate_page_is_admitted_at_reduced_resolution():
    """The other side of the same bound: a legitimately large page (well past
    what 400 DPI would keep under the pixel ceiling) is still rendered, just at a
    lower effective resolution, rather than refused outright. 3,000 x 2,000
    points at `TARGET_DPI` would be roughly 16,700 x 11,100 pixels — past the
    render's own RGB-decoded-bytes bound — but comfortably fits at a reduced
    scale well above the 72-DPI floor."""
    data = single_gray_page_pdf().replace(b"[0 0 4 3]", b"[0 0 3000 2000]")
    scale = pdf_render._bounded_scale(3000, 2000, 0)
    assert scale * 72 > pdf_render.MIN_DPI, "the test should exercise a reduced, not floor, scale"
    assert scale < pdf_render.TARGET_DPI / 72, "the test should exercise a page that gets capped"
    rendered, page_format = render_page(data, 0)
    assert page_format == "png"
    geometry = validate_png(rendered)
    assert geometry.width > 0 and geometry.height > 0


# --- corrupt or unopenable documents, refused rather than guessed at -------------


def test_render_page_out_of_range_is_refused():
    with pytest.raises(pdf_render.PdfRefusal, match="out of range"):
        render_page(single_gray_page_pdf(), 5)


def test_not_a_pdf_is_refused_as_corrupt():
    with pytest.raises(pdf_render.PdfRefusal) as caught:
        pdf_render.open_document(b"not a pdf at all")
    assert caught.value.reason is RefusalReason.CORRUPT


def test_a_pdf_declaring_no_pages_is_refused():
    """PDFium itself refuses to open a `/Pages` tree with zero kids and a
    declared `/Count` of zero, so this never reaches this module's own
    `page_count <= 0` check — kept as a fail-closed safety net regardless, the
    same shape as `admission.classify_detected_format`'s unreachable-while-covered
    branch. Either way, an empty document is refused rather than silently
    admitted as zero pages."""
    builder = PdfBuilder()
    catalog, pages = builder.add(), builder.add()
    builder.objects[pages] = b"<< /Type /Pages /Kids [] /Count 0 >>"
    builder.objects[catalog] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
    with pytest.raises(pdf_render.PdfRefusal):
        pdf_render.open_document(builder.build(catalog))


def test_an_encrypted_pdf_is_refused_as_an_unsupported_variant_not_corrupt():
    """This pipeline has no mechanism to supply a password, so an encrypted PDF is
    a named, documented gap — the same shape as a format with no reader yet —
    rather than damage. A password-protected PDF is nontrivial to hand-build
    correctly, so this proves the classification rule directly against the
    message PDFium itself reports for a document it cannot open."""
    assert pdf_render._classify_open_failure(ValueError("Password required")) is (
        RefusalReason.UNSUPPORTED_VARIANT
    )
    assert pdf_render._classify_open_failure(ValueError("the file is encrypted")) is (
        RefusalReason.UNSUPPORTED_VARIANT
    )
    assert pdf_render._classify_open_failure(ValueError("Data format error")) is (
        RefusalReason.CORRUPT
    )


def test_close_document_releases_the_native_handle_without_raising():
    document = pdf_render.open_document(single_gray_page_pdf())
    pdf_render.render_page(document, 0)
    pdf_render.close_document(document)
