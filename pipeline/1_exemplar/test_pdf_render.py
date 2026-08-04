"""Whole-page PDF rasterisation, proven only with synthetic documents.

The fixtures are assembled in memory by ``synthetic_sources.PdfBuilder``.  The
decisive case paints text beside a separate image: returning the image XObject
cannot pass, because the assertions inspect ink in both regions of the page-sized
PNG.  No binary PDF and no real image enters the repository.
"""

from io import BytesIO
from math import ceil

import pdf_render
import pypdfium2
import pypdfium2.internal
import pytest
from admission import RefusalReason
from image_formats import MAX_DIMENSION, MAX_PIXELS, validate_png
from pdf_render import MAX_PAGES, PdfRefusal, close_document, count_pages, open_document
from PIL import Image
from synthetic_sources import (
    blank_pages_pdf,
    content_page_pdf,
    form_text_pdf,
    gray_image,
    single_gray_page_pdf,
    two_page_pdf,
)


def render_page(data: bytes, page_index: int = 0) -> pdf_render.RenderedPage:
    """Open, render and close one synthetic document like the door owns it."""
    opened = open_document(data)
    try:
        return pdf_render.render_page(opened, page_index)
    finally:
        close_document(opened)


def rendered_image(page: pdf_render.RenderedPage) -> Image.Image:
    image = Image.open(BytesIO(page.png_bytes))
    image.load()
    return image.convert("RGB")


def point_box(left: int, bottom: int, right: int, top: int, *, page_height: int = 72):
    """Convert a PDF-point region to an enclosing top-left-origin pixel box."""
    scale = pdf_render.RENDER_SCALE
    return (
        int(left * scale),
        int((page_height - top) * scale),
        ceil(right * scale),
        ceil((page_height - bottom) * scale),
    )


def dark_pixels(image: Image.Image, box: tuple[int, int, int, int]) -> int:
    return sum(
        1
        for red, green, blue in image.crop(box).get_flattened_data()
        if max(red, green, blue) < 100
    )


def test_page_count_drives_fan_out_and_each_page_paints_distinct_pixels():
    data = two_page_pdf()
    assert count_pages(data) == 2

    opened = open_document(data)
    try:
        assert opened.page_count == 2
        first = pdf_render.render_page(opened, 0)
        second = pdf_render.render_page(opened, 1)
    finally:
        close_document(opened)

    assert first.png_bytes != second.png_bytes
    assert (first.width, first.height) == (
        ceil(4 * pdf_render.RENDER_SCALE),
        ceil(3 * pdf_render.RENDER_SCALE),
    )
    assert validate_png(first.png_bytes).width == first.width
    assert validate_png(second.png_bytes).height == second.height


def test_text_beside_a_separately_painted_image_survives_the_same_page_render():
    content = b"q 40 0 0 40 8 12 cm /Im0 Do Q BT /F1 18 Tf 80 24 Td (TEXT) Tj ET"
    page = render_page(content_page_pdf(content, images=[gray_image(4, 4, 0)]))
    image = rendered_image(page)

    canvas = (
        ceil(144 * pdf_render.RENDER_SCALE),
        ceil(72 * pdf_render.RENDER_SCALE),
    )
    assert image.size == canvas, "the sealed pixels are the page canvas, not the 4x4 XObject"
    assert dark_pixels(image, point_box(8, 12, 48, 52)) > 20_000
    assert dark_pixels(image, point_box(76, 20, 140, 48)) > 100


def test_an_interactive_form_field_is_painted_into_the_sealed_page():
    page = render_page(form_text_pdf())
    image = rendered_image(page)

    # The document's content stream is empty.  Ink here proves the AcroForm was
    # initialized before page access and drawn as part of the full-page render.
    assert dark_pixels(image, point_box(20, 20, 130, 52)) > 100


def test_a_text_only_page_is_a_page_even_without_an_image_xobject():
    page = render_page(content_page_pdf(b"BT /F1 24 Tf 20 24 Td (WORDS) Tj ET"))
    image = rendered_image(page)

    assert dark_pixels(image, point_box(15, 18, 130, 54)) > 300
    assert image.getpixel((10, 10)) == (255, 255, 255)


def test_a_vector_only_page_is_painted_in_its_declared_colour():
    page = render_page(content_page_pdf(b"1 0 0 rg 20 10 60 30 re f"))
    image = rendered_image(page)

    center_x = int(50 * pdf_render.RENDER_SCALE)
    center_y = int((72 - 25) * pdf_render.RENDER_SCALE)
    red, green, blue = image.getpixel((center_x, center_y))
    assert red > 240 and green < 20 and blue < 20


def test_multiple_image_xobjects_can_both_contribute_to_one_page():
    content = b"q 30 0 0 30 10 10 cm /Im0 Do Q q 30 0 0 30 90 10 cm /Im1 Do Q"
    page = render_page(
        content_page_pdf(
            content,
            images=[gray_image(3, 3, 0), gray_image(3, 3, 180)],
        )
    )
    image = rendered_image(page)

    left = image.getpixel(
        (int(25 * pdf_render.RENDER_SCALE), int((72 - 25) * pdf_render.RENDER_SCALE))
    )
    right = image.getpixel(
        (int(105 * pdf_render.RENDER_SCALE), int((72 - 25) * pdf_render.RENDER_SCALE))
    )
    assert max(left) < 10
    assert all(160 <= channel <= 200 for channel in right)


def test_normal_page_rotation_is_rendered_and_swaps_the_canvas_dimensions():
    page = render_page(
        content_page_pdf(b"0 0 1 rg 10 10 30 20 re f", width=144, height=72, rotate=90)
    )
    image = rendered_image(page)

    assert (page.width, page.height) == (
        ceil(72 * pdf_render.RENDER_SCALE),
        ceil(144 * pdf_render.RENDER_SCALE),
    )
    assert (
        sum(
            1
            for red, green, blue in image.get_flattened_data()
            if red < 20 and green < 20 and blue > 240
        )
        > 10_000
    )


def test_rendering_twice_from_one_open_document_is_byte_deterministic():
    data = content_page_pdf(
        b"q 40 0 0 40 8 12 cm /Im0 Do Q BT /F1 18 Tf 80 24 Td (TEXT) Tj ET",
        images=[gray_image(4, 4, 32)],
    )
    opened = open_document(data)
    try:
        first = pdf_render.render_page(opened, 0)
        second = pdf_render.render_page(opened, 0)
    finally:
        close_document(opened)

    assert first == second


def test_the_render_contract_records_every_pixel_affecting_choice():
    rendered = render_page(single_gray_page_pdf(width=72, height=36, value=64))
    contract = rendered.contract

    assert contract["renderer"] == "pypdfium2"
    assert contract["renderer_version"]
    assert contract["pdfium_version"]
    assert contract["container_page_index"] == 0
    assert contract["dpi"] == pdf_render.RENDER_DPI == 400
    assert contract["min_dpi"] == pdf_render.MIN_RENDER_DPI == 72
    assert contract["effective_dpi"] == 400
    assert contract["scale"] == pdf_render.RENDER_SCALE_RECORD
    assert contract["output"] == {"codec": "png", "color_mode": "RGB"}
    assert contract["background"] == "white"
    assert contract["draw_annotations"] is True
    assert contract["draw_forms"] is True
    assert (contract["width"], contract["height"]) == (rendered.width, rendered.height)
    assert validate_png(rendered.png_bytes).width == rendered.width


def test_an_oversized_page_is_an_alarm_before_a_bitmap_is_returned():
    """Only a page degenerate even at the floor DPI refuses; large is not enough."""
    data = content_page_pdf(b"", width=100_000, height=100_000)
    with pytest.raises(PdfRefusal) as caught:
        render_page(data)

    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "floor" in str(caught.value)


def test_a_large_legitimate_page_renders_at_reduced_resolution_rather_than_refusing():
    """A page too big for the target DPI is captured, not lost.

    GOALS 1: a poorly read act can be corrected later, a missed one cannot. So the
    resolution is capped downward toward the floor and the page still seals, and
    the contract records what it was actually rendered at — a recipe naming only
    the target would describe pixels these are not.
    """
    # An A0-sized page: legitimate, and far past what 400 DPI would fit inside the
    # project's pixel bounds.
    rendered = render_page(content_page_pdf(b"", width=2384, height=3370))

    assert 72 <= rendered.contract["effective_dpi"] < pdf_render.RENDER_DPI
    assert rendered.width * rendered.height <= MAX_PIXELS
    assert max(rendered.width, rendered.height) <= MAX_DIMENSION
    assert validate_png(rendered.png_bytes).width == rendered.width


@pytest.mark.parametrize(
    "width,height",
    [(0, 72), (-1, 72), (72, 0), (72, -1)],
)
def test_a_non_positive_page_dimension_is_a_corrupt_alarm(width: int, height: int):
    with pytest.raises(PdfRefusal) as caught:
        pdf_render._render_dimensions(width, height)
    assert caught.value.reason is RefusalReason.CORRUPT


def test_the_page_fan_out_limit_is_enforced_on_a_real_synthetic_page_tree():
    assert MAX_PAGES == 5_000
    with pytest.raises(PdfRefusal) as caught:
        count_pages(blank_pages_pdf(MAX_PAGES + 1))

    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert f"above the {MAX_PAGES}-page" in str(caught.value)


def test_a_zero_page_document_is_a_corrupt_alarm():
    with pytest.raises(PdfRefusal) as caught:
        count_pages(blank_pages_pdf(0))
    assert caught.value.reason is RefusalReason.CORRUPT


def test_non_pdf_and_truncated_pdf_bytes_are_corrupt_alarms():
    with pytest.raises(PdfRefusal) as missing_header:
        count_pages(b"not a PDF")
    assert missing_header.value.reason is RefusalReason.CORRUPT

    with pytest.raises(PdfRefusal) as truncated:
        count_pages(b"%PDF-1.7\nthis stops before any objects")
    assert truncated.value.reason is RefusalReason.CORRUPT


@pytest.mark.parametrize("page_index", [-1, 1, True, 0.5])
def test_invalid_page_indexes_are_named_alarms(page_index):
    data = single_gray_page_pdf()
    opened = open_document(data)
    try:
        with pytest.raises(PdfRefusal) as caught:
            pdf_render.render_page(opened, page_index)
    finally:
        close_document(opened)
    assert caught.value.reason is RefusalReason.CORRUPT


def test_counting_a_page_does_not_rasterise_it(monkeypatch):
    renders = 0

    def unexpected_render(*args, **kwargs):
        nonlocal renders
        renders += 1
        raise AssertionError("page counting reached the rasteriser")

    monkeypatch.setattr(pdf_render, "render_page", unexpected_render)
    assert count_pages(single_gray_page_pdf()) == 1
    assert renders == 0


# --- A locked document is not a damaged one -------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(4, RefusalReason.UNSUPPORTED_VARIANT, id="incorrect-password"),
        pytest.param(5, RefusalReason.UNSUPPORTED_VARIANT, id="unsupported-security-scheme"),
        pytest.param(3, RefusalReason.CORRUPT, id="data-format-error"),
        pytest.param(2, RefusalReason.CORRUPT, id="file-access-error"),
        pytest.param(1, RefusalReason.CORRUPT, id="unknown-error"),
        pytest.param(None, RefusalReason.CORRUPT, id="no-code-at-all"),
    ],
)
def test_a_locked_pdf_is_a_gap_we_own_and_a_broken_one_is_damage(code, expected):
    """The two facts a failed open can carry, and they are different decisions.

    Nothing here can supply a password, so a locked document is work this project
    owes rather than a damaged original — telling Tyrel an intact scan is corrupt
    is the wrong sentence about his own file.

    Both locked codes are checked because only one of them says "password":
    PDFium renders code 5 as "Unsupported security scheme error", which contains
    neither "password" nor "encrypt". A classification matching the message text
    reported that intact file as damaged.
    """
    error = pypdfium2.PdfiumError("Failed to load document.")
    if code is not None:
        error.err_code = code

    assert pdf_render._open_failure_code(error) is expected


def test_pdfiums_own_error_table_still_names_the_two_locked_codes():
    """The codes above are PDFium's, so they are read back from PDFium.

    Two integers written into this repository are two integers that can drift from
    the library that defines them. This is what notices if a version bump renumbers
    them — without it, the classification would keep returning `CORRUPT` forever and
    nothing would say so.
    """
    table = dict(pypdfium2.internal.ErrorToStr)

    assert "password" in table[pdf_render.PDFIUM_INCORRECT_PASSWORD].lower()
    assert "security" in table[pdf_render.PDFIUM_UNSUPPORTED_SECURITY_SCHEME].lower()
