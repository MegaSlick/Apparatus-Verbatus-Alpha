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
import render_config
from admission import RefusalReason
from image_formats import MAX_DIMENSION, MAX_PIXELS, validate_png
from pdf_render import PdfRefusal, close_document, count_pages, open_document
from PIL import Image
from synthetic_sources import (
    blank_pages_pdf,
    content_page_pdf,
    form_text_pdf,
    gray_image,
    page_tree_bomb_pdf,
    single_gray_page_pdf,
    two_page_pdf,
)

PDF_SETTINGS = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
RENDER_SCALE = PDF_SETTINGS.target_dpi / pdf_render.POINTS_PER_INCH


def render_page(data: bytes, page_index: int = 0) -> pdf_render.RenderedPage:
    """Open, render and close one synthetic document like the door owns it."""
    opened = open_document(data)
    try:
        return pdf_render.render_page(opened, page_index, PDF_SETTINGS)
    finally:
        close_document(opened)


def rendered_image(page: pdf_render.RenderedPage) -> Image.Image:
    image = Image.open(BytesIO(page.png_bytes))
    image.load()
    return image.convert("RGB")


def point_box(left: int, bottom: int, right: int, top: int, *, page_height: int = 72):
    """Convert a PDF-point region to an enclosing top-left-origin pixel box."""
    scale = RENDER_SCALE
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
        first = pdf_render.render_page(opened, 0, PDF_SETTINGS)
        second = pdf_render.render_page(opened, 1, PDF_SETTINGS)
    finally:
        close_document(opened)

    assert first.png_bytes != second.png_bytes
    assert (first.width, first.height) == (
        ceil(4 * RENDER_SCALE),
        ceil(3 * RENDER_SCALE),
    )
    assert validate_png(first.png_bytes).width == first.width
    assert validate_png(second.png_bytes).height == second.height


def test_text_beside_a_separately_painted_image_survives_the_same_page_render():
    content = b"q 40 0 0 40 8 12 cm /Im0 Do Q BT /F1 18 Tf 80 24 Td (TEXT) Tj ET"
    page = render_page(content_page_pdf(content, images=[gray_image(4, 4, 0)]))
    image = rendered_image(page)

    canvas = (
        ceil(144 * RENDER_SCALE),
        ceil(72 * RENDER_SCALE),
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

    center_x = int(50 * RENDER_SCALE)
    center_y = int((72 - 25) * RENDER_SCALE)
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

    left = image.getpixel((int(25 * RENDER_SCALE), int((72 - 25) * RENDER_SCALE)))
    right = image.getpixel((int(105 * RENDER_SCALE), int((72 - 25) * RENDER_SCALE)))
    assert max(left) < 10
    assert all(160 <= channel <= 200 for channel in right)


def test_normal_page_rotation_is_rendered_and_swaps_the_canvas_dimensions():
    page = render_page(
        content_page_pdf(b"0 0 1 rg 10 10 30 20 re f", width=144, height=72, rotate=90)
    )
    image = rendered_image(page)

    assert (page.width, page.height) == (
        ceil(72 * RENDER_SCALE),
        ceil(144 * RENDER_SCALE),
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
        first = pdf_render.render_page(opened, 0, PDF_SETTINGS)
        second = pdf_render.render_page(opened, 0, PDF_SETTINGS)
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
    # Stated as relationships against the resolved settings, not as the shipped
    # number. What this test is for is that the contract records every choice that
    # moves a pixel; *which* DPI ships is one fact owned by one test
    # (`test_render_config.py::test_the_shipped_default_is_documented_run_configuration`),
    # and repeating it here only means two files to edit and one of them forgotten.
    target = PDF_SETTINGS.target_dpi
    assert contract["configured_target_dpi"] == PDF_SETTINGS.configured_target_dpi
    assert contract["dpi"] == target
    assert contract["min_dpi"] == pdf_render.MIN_RENDER_DPI == 72
    assert contract["effective_dpi"] == target
    assert contract["scale"] == {"numerator": target, "denominator": 72}
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

    assert 72 <= rendered.contract["effective_dpi"] < PDF_SETTINGS.target_dpi
    assert rendered.width * rendered.height <= MAX_PIXELS
    assert max(rendered.width, rendered.height) <= MAX_DIMENSION
    assert validate_png(rendered.png_bytes).width == rendered.width


def test_an_arbitrarily_large_configured_target_still_caps_before_float_conversion():
    dpi, width, height = pdf_render._render_dimensions(72, 72, 10**1_000)

    assert dpi < 10**1_000
    assert width * height <= MAX_PIXELS
    assert max(width, height) <= MAX_DIMENSION


@pytest.mark.parametrize(
    "width,height",
    [(0, 72), (-1, 72), (72, 0), (72, -1)],
)
def test_a_non_positive_page_dimension_is_a_corrupt_alarm(width: int, height: int):
    with pytest.raises(PdfRefusal) as caught:
        pdf_render._render_dimensions(width, height, PDF_SETTINGS.target_dpi)
    assert caught.value.reason is RefusalReason.CORRUPT


def test_a_pdf_page_count_past_the_retired_policy_cap_remains_the_fan_out_denominator():
    """A long reel's declared page tree, not an old 5,000-page policy, fans out."""
    pages = 5_001
    assert count_pages(blank_pages_pdf(pages)) == pages


def test_a_page_tree_that_shares_one_leaf_declares_no_more_pages_than_its_bytes_allow():
    """Ruling 17 retired the absolute page cap for a genuine reel; it did not clear a
    few-KB file to declare hundreds of thousands of pages via shared page-tree
    nodes. PDFium itself trusts the declared, compounding ``/Count`` -- confirmed
    directly: a 19-level, 2-way fan-out tree (22 objects, ~2KB) opens and reports
    524,288 pages -- so nothing stops `door.expand_sources` from fanning out and
    rendering that many pages from an attacker-sized file unless something checks
    plausibility before the fan-out denominator is trusted.

    The refusal is `UNSUPPORTED_VARIANT`, not `CORRUPT`, and that is not a detail.
    A blind audit built a *valid* PDF 1.5 at 9.4 bytes per page -- 10,000 distinct
    page objects, true `/Count`, packed into a Flate object stream -- which PDFium
    opens and renders. So this ratio cannot establish damage, and `CORRUPT` is the
    code that tells Tyrel his original is broken. What the ratio does establish is
    that this reader cannot yet tell the two apart, which is a gap in this pipeline.
    """
    data, declared = page_tree_bomb_pdf(19, fanout=2)
    assert declared == 524_288
    with pytest.raises(PdfRefusal) as caught:
        open_document(data)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "cannot yet tell" in str(caught.value)


def test_an_open_binary_pdf_stream_is_handed_to_pdfium_without_a_path_reopen(tmp_path, monkeypatch):
    """The real Door keeps one anchored stream for its hash and PDFium document."""
    path = tmp_path / "reel.pdf"
    path.write_bytes(single_gray_page_pdf())
    calls = []
    original = pdf_render.pdfium.PdfDocument

    def record_source(source, *args, **kwargs):
        calls.append(source)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(pdf_render.pdfium, "PdfDocument", record_source)
    with path.open("rb") as source:
        assert count_pages(source) == 1
        assert calls == [source]


def test_a_local_path_is_still_accepted_for_a_caller_that_owns_no_stream(tmp_path, monkeypatch):
    """A path is still a supported input; it is only the *real* Door that may not
    use one. Unit callers with no submission folder behind them keep it, so the
    branch stays covered rather than becoming an untested leftover."""
    path = tmp_path / "reel.pdf"
    path.write_bytes(single_gray_page_pdf())
    calls = []
    original = pdf_render.pdfium.PdfDocument

    def record_source(source, *args, **kwargs):
        calls.append(source)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(pdf_render.pdfium, "PdfDocument", record_source)
    assert count_pages(path) == 1
    assert calls == [path]


def test_the_stream_pdfium_read_is_left_open_for_its_owner_to_close(tmp_path):
    """`pypdfium2` does not close a custom-buffer input unless asked to, and the
    Door depends on that: the descriptor it hashed must survive the document it
    rendered, so the stability recheck afterwards still has something to stat."""
    path = tmp_path / "reel.pdf"
    path.write_bytes(two_page_pdf())
    with path.open("rb") as stream:
        opened = open_document(stream)
        try:
            assert opened.page_count == 2
        finally:
            close_document(opened)
        assert not stream.closed
    assert stream.closed


def test_a_normal_document_close_failure_is_a_named_alarm():
    """Best-effort cleanup belongs only on a path already reporting a failure."""

    class BrokenClose:
        def close(self):
            raise RuntimeError("synthetic native close failure")

    with pytest.raises(PdfRefusal) as caught:
        close_document(pdf_render.OpenPdf(BrokenClose(), 1))

    assert caught.value.reason is RefusalReason.UNREADABLE
    assert "could not release" in str(caught.value)


def test_a_source_that_is_neither_bytes_a_path_nor_a_stream_is_a_named_refusal():
    """Matching `pypdfium2.internal.is_stream` exactly is what keeps this a named
    refusal: a near-stream missing `readinto` would otherwise pass this module's
    own check and die inside PDFium as a bare TypeError nothing here catches."""

    class NearlyAStream:
        def read(self, size=-1):  # pragma: no cover - never reached
            return b""

        def seek(self, offset, whence=0):  # pragma: no cover - never reached
            return 0

        def tell(self):  # pragma: no cover - never reached
            return 0

    with pytest.raises(PdfRefusal) as caught:
        count_pages(NearlyAStream())
    assert caught.value.reason is RefusalReason.CORRUPT
    assert "neither bytes, a path, nor a stream" in str(caught.value)


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


def test_a_pdfium_accepted_prefix_preamble_is_not_called_an_unknown_format():
    """A transfer preamble does not erase an otherwise readable PDF's route."""
    assert count_pages(b"\n\n" + single_gray_page_pdf()) == 1


@pytest.mark.parametrize("page_index", [-1, 1, True, 0.5])
def test_invalid_page_indexes_are_named_alarms(page_index):
    data = single_gray_page_pdf()
    opened = open_document(data)
    try:
        with pytest.raises(PdfRefusal) as caught:
            pdf_render.render_page(opened, page_index, PDF_SETTINGS)
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
