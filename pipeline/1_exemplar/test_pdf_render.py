"""The bounded PDF renderer, proven against hand-built PDF bytes.

Every PDF here is assembled by `synthetic_sources.PdfBuilder`, a tiny classic-xref
writer built for these tests alone — never a checked-in binary, for the same ingress
reason `test_image_formats.py` gives, and because a PDF fixture is exactly the kind
of thing GOVERNANCE's synthetic-material rule exists to keep out of git.

The module's whole claim is that it handles one shape completely and refuses
everything else **by name**. So the refusals matter as much as the renders, and each
one asserts which closed-set code it produced rather than only that it raised.
"""

import re
import zlib

import pytest
from admission import RefusalReason
from image_formats import validate_png
from pdf_render import MAX_PAGES, PdfRefusal, count_pages, parse_object, render_page
from synthetic_sources import (
    PdfBuilder,
    custom_image,
    gray_image,
    image_page_pdf,
    jpeg,
    jpeg_image,
    single_gray_page_pdf,
    two_page_pdf,
)

# --- happy paths -----------------------------------------------------------------


def test_count_pages_on_a_single_page_document():
    assert count_pages(single_gray_page_pdf()) == 1


def test_a_multi_page_document_renders_distinct_pages_in_order():
    data = two_page_pdf()
    assert count_pages(data) == 2
    first, first_format = render_page(data, 0)
    second, second_format = render_page(data, 1)
    assert first_format == second_format == "png"
    assert first != second, "the two pages must render distinct bytes"
    assert validate_png(first).width == validate_png(second).width == 4


def test_render_page_flate_decode_gray_round_trips_the_samples():
    rendered, page_format = render_page(single_gray_page_pdf(width=6, height=2, value=42), 0)
    assert page_format == "png"
    geometry = validate_png(rendered)
    assert (geometry.width, geometry.height) == (6, 2)


def test_render_page_flate_decode_rgb_round_trips_the_samples():
    samples = bytes(range(3 * 2 * 3))
    data = image_page_pdf([custom_image(3, 2, colorspace="DeviceRGB", raw=zlib.compress(samples))])
    rendered, page_format = render_page(data, 0)
    assert page_format == "png"
    assert (validate_png(rendered).width, validate_png(rendered).height) == (3, 2)


def test_unfiltered_raw_samples_with_no_filter_key_are_accepted():
    data = image_page_pdf([custom_image(2, 2, filter_name=None, raw=bytes([10, 20, 30, 40]))])
    rendered, page_format = render_page(data, 0)
    assert page_format == "png"
    assert validate_png(rendered).width == 2


def test_render_page_dct_decode_passes_the_embedded_jpeg_through():
    """A scan already stored as JPEG is stored as-is, not re-encoded: re-encoding
    would be a transform nobody asked for and nothing recorded."""
    rendered, page_format = render_page(image_page_pdf([jpeg_image(9, 6)]), 0)
    assert page_format == "jpeg"
    assert rendered == jpeg(9, 6)


def test_rendering_the_same_page_twice_produces_identical_bytes():
    """Render-once-and-seal only means anything if the render is deterministic: two
    runs of one command must leave every byte unchanged."""
    data = single_gray_page_pdf()
    assert render_page(data, 0) == render_page(data, 0)


# --- documented limits, each refused by name -------------------------------------


def test_a_rotated_page_is_refused_as_an_unsupported_variant():
    with pytest.raises(PdfRefusal) as caught:
        render_page(single_gray_page_pdf(rotate=90), 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "Rotate" in str(caught.value)


def test_a_page_with_two_xobjects_is_refused_as_an_unsupported_variant():
    data = image_page_pdf([gray_image(2, 2, 1)], xobject_count=2)
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT


def test_a_page_with_no_xobjects_is_refused_as_an_unsupported_variant():
    data = image_page_pdf([gray_image(2, 2, 1)], xobject_count=0)
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT


def test_an_unsupported_filter_is_refused_by_its_own_name():
    data = image_page_pdf([custom_image(2, 2, filter_name="CCITTFaxDecode", raw=b"\x00\x00")])
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "CCITTFaxDecode" in str(caught.value)


def test_chained_filters_are_refused_as_an_unsupported_variant():
    entry = gray_image(2, 2, 0)
    # Built rather than substituted: a same-name replacement of different length
    # would shift every xref offset and the parser would refuse for that instead.
    entry["dictionary"] = entry["dictionary"].replace(
        "/Filter /FlateDecode", "/Filter [/FlateDecode /RunLengthDecode]"
    )
    data = image_page_pdf([entry])
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "chained" in str(caught.value)


def test_a_non_8_bit_component_is_refused_as_an_unsupported_variant():
    data = image_page_pdf([custom_image(2, 2, bits=1, raw=zlib.compress(b"\x00"))])
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "bit" in str(caught.value)


def test_an_unsupported_colorspace_is_refused_as_an_unsupported_variant():
    data = image_page_pdf(
        [custom_image(2, 2, colorspace="DeviceCMYK", raw=zlib.compress(bytes(2 * 2 * 4)))]
    )
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "colorspace" in str(caught.value)


def test_an_encrypted_pdf_is_refused_as_an_unsupported_variant():
    encrypted = single_gray_page_pdf().replace(
        b"/Root", b"/Encrypt << /Filter /Standard >> /Root", 1
    )
    with pytest.raises(PdfRefusal) as caught:
        count_pages(encrypted)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "encrypt" in str(caught.value).lower()


def test_a_cross_reference_stream_pdf_is_refused_as_an_unsupported_variant():
    """PDF 1.5+ compressed cross-reference streams do not open with the literal
    `xref` keyword this parser requires; that alone is the refusal."""
    data = bytearray(single_gray_page_pdf())
    index = data.rfind(b"xref\n0 ")
    data[index : index + 4] = b"9999"
    with pytest.raises(PdfRefusal) as caught:
        count_pages(bytes(data))
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT


def test_an_incremental_update_chain_is_refused_as_an_unsupported_variant():
    data = single_gray_page_pdf()
    trailer = data.rfind(b"trailer")
    root_end = data.index(b"R >>", trailer)
    # Spliced after xref/startxref are already written, so no byte offset shifts.
    patched = data[: root_end + 1] + b" /Prev 0" + data[root_end + 1 :]
    with pytest.raises(PdfRefusal) as caught:
        count_pages(patched)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "Prev" in str(caught.value)


def test_the_page_fan_out_is_bounded_before_it_runs():
    assert MAX_PAGES == 5_000


def page_tree_pdf(*, kids: list[int], declared_count: int, builder: PdfBuilder) -> bytes:
    catalog = builder.add()
    pages = builder.add()
    builder.objects[pages] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] "
        f"/Count {declared_count} >>"
    ).encode()
    builder.objects[catalog] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
    return builder.build(catalog)


def test_a_declared_page_count_past_the_limit_refuses_before_the_tree_is_walked():
    builder = PdfBuilder()
    data = page_tree_pdf(kids=[], declared_count=MAX_PAGES + 1, builder=builder)
    with pytest.raises(PdfRefusal, match="page admission limit"):
        count_pages(data)


def test_the_page_tree_count_must_equal_the_pages_it_actually_contains():
    builder = PdfBuilder()
    page = builder.add(b"<< /Type /Page >>")
    data = page_tree_pdf(kids=[page], declared_count=2, builder=builder)
    with pytest.raises(PdfRefusal, match="Count"):
        count_pages(data)


def test_one_page_object_cannot_be_counted_twice_through_duplicate_references():
    builder = PdfBuilder()
    page = builder.add(b"<< /Type /Page >>")
    data = page_tree_pdf(kids=[page, page], declared_count=2, builder=builder)
    with pytest.raises(PdfRefusal, match="more than once"):
        count_pages(data)


def test_a_cross_reference_count_is_bounded_before_entries_are_allocated():
    data = single_gray_page_pdf()
    data = re.sub(rb"xref\n0 \d+", b"xref\n0 1000000", data, count=1)
    with pytest.raises(PdfRefusal, match="cross-reference entry limit"):
        count_pages(data)


# --- corrupt documents, refused rather than guessed at ---------------------------


def test_render_page_out_of_range_is_refused():
    with pytest.raises(PdfRefusal, match="out of range"):
        render_page(single_gray_page_pdf(), 5)


def test_not_a_pdf_is_refused():
    with pytest.raises(PdfRefusal, match="missing header"):
        count_pages(b"not a pdf at all")


def test_a_declared_geometry_that_disagrees_with_the_samples_is_refused():
    """A same-length substitution, so every xref offset stays valid and this
    exercises the sample-count check rather than an unrelated parse failure."""
    corrupted = single_gray_page_pdf(width=4, height=3, value=1).replace(
        b"/Width 4", b"/Width 9", 1
    )
    with pytest.raises(PdfRefusal, match="disagrees"):
        render_page(corrupted, 0)


def test_a_flate_stream_that_would_inflate_past_its_declared_geometry_is_bounded():
    """The zip-bomb defence `image_formats.validate_png` has, applied here for the
    same reason: this is the second place in the pipeline that inflates bytes nobody
    has verified yet."""
    data = image_page_pdf([custom_image(2, 2, raw=zlib.compress(bytes(range(256)) * 40))])
    with pytest.raises(PdfRefusal, match="disagrees"):
        render_page(data, 0)


def test_a_flate_stream_with_trailing_encoded_bytes_is_refused_as_corrupt():
    data = image_page_pdf(
        [custom_image(2, 2, raw=zlib.compress(b"\x00\x01\x02\x03") + b"trailing")]
    )
    with pytest.raises(PdfRefusal, match="trailing bytes"):
        render_page(data, 0)


def test_an_image_larger_than_the_admission_limits_is_refused_before_it_inflates():
    data = image_page_pdf(
        [custom_image(2, 2, declared_width=200_000, raw=zlib.compress(b"\x00" * 4))]
    )
    with pytest.raises(PdfRefusal) as caught:
        render_page(data, 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "admission limits" in str(caught.value)


def test_an_embedded_jpeg_whose_geometry_disagrees_with_the_dictionary_is_refused():
    entry = jpeg_image(9, 6)
    entry["dictionary"] = entry["dictionary"].replace("/Width 9 /Height 6", "/Width 5 /Height 5")
    with pytest.raises(PdfRefusal, match="disagrees"):
        render_page(image_page_pdf([entry]), 0)


def test_a_corrupt_embedded_jpeg_is_refused_as_corrupt():
    entry = jpeg_image(5, 4)
    entry["raw"] = b"\xff\xd8not really a jpeg"
    with pytest.raises(PdfRefusal) as caught:
        render_page(image_page_pdf([entry]), 0)
    assert caught.value.reason is RefusalReason.CORRUPT
    assert "JPEG" in str(caught.value)


def test_pathologically_nested_objects_refuse_rather_than_crash():
    """Fail closed applies to the parser's own crash modes, not only to the
    documents it reads: a Python RecursionError is not a named refusal."""
    with pytest.raises(PdfRefusal, match="nesting"):
        parse_object(b"[" * 3000 + b"]" * 3000, 0)


def test_a_pathologically_large_integer_refuses_instead_of_escaping_as_value_error():
    with pytest.raises(PdfRefusal, match="numeric object"):
        parse_object(b"9" * 5_000, 0)


def test_an_object_array_is_bounded_before_it_can_allocate_from_file_length():
    with pytest.raises(PdfRefusal, match="object item limit"):
        parse_object(b"[" + b"0 " * 10_001 + b"]", 0)


def test_raw_render_allocation_is_bounded_independently_of_pixel_count():
    data = image_page_pdf(
        [
            custom_image(
                50_000,
                1_000,
                colorspace="DeviceRGB",
                raw=zlib.compress(b"\x00"),
            )
        ]
    )
    with pytest.raises(PdfRefusal, match="decoded-byte admission limit"):
        render_page(data, 0)


def test_a_non_numeric_cross_reference_offset_refuses_rather_than_crashes():
    """A bare ValueError out of `int()` is not a named refusal."""
    data = bytearray(single_gray_page_pdf())
    match = re.search(rb"\d{10} 00000 n \n", data)
    assert match is not None
    data[match.start() : match.start() + 10] = b"XXXXXXXXXX"
    with pytest.raises(PdfRefusal, match="not numeric"):
        count_pages(bytes(data))
