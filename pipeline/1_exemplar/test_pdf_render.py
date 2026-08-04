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

import pdf_render
import pytest
from admission import RefusalReason
from image_formats import validate_png
from pdf_render import MAX_PAGES, PdfRefusal, count_pages, open_document, parse_object
from synthetic_sources import (
    PdfBuilder,
    custom_image,
    gray_image,
    image_page_pdf,
    jpeg,
    jpeg_image,
    single_gray_page_pdf,
    stream_object,
    two_page_pdf,
)


def render_page(data: bytes, page_index: int):
    """Render one page straight from bytes, the way these tests read best.

    The module's own `render_page` takes a document parsed by `open_document`,
    because the door calls it once per page and re-parsing the whole
    cross-reference table and page tree each time was cleanly quadratic. That is
    the caller's saving to make; a test naming one page wants one call.
    """
    return pdf_render.render_page(open_document(data), page_index)


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


# --- what four reviewing seats found, each with the case that would have caught it -
#
# Every one of these was reproduced against this branch before it was repaired: a
# crash that took a healthy source down with it, a quadratic parse an attacker sizes,
# a transform silently dropped, a colour space claimed and never checked, and a
# payload riding inside a sealed digest nothing had inspected.


def test_an_array_colorspace_is_a_named_refusal_and_never_a_crash():
    """`/ColorSpace [/ICCBased 5 0 R]` and `[/Indexed ...]` are ordinary scanner
    output. A list is unhashable, so testing it against a dict raised a bare
    TypeError that `door.decide`'s `except PdfRefusal` could not catch — aborting the
    whole run, so a healthy source later in the same submission got no decision at
    all. That is invariant #3 failing, not merely a missing refusal name."""
    for colorspace in ("[/ICCBased 5 0 R]", "[/Indexed /DeviceRGB 1 <000000FFFFFF>]"):
        entry = gray_image(2, 2, 0)
        entry["dictionary"] = entry["dictionary"].replace(
            "/ColorSpace /DeviceGray", f"/ColorSpace {colorspace}"
        )
        with pytest.raises(PdfRefusal) as caught:
            render_page(image_page_pdf([entry]), 0)
        assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
        assert "not a plain device name" in str(caught.value)


def test_a_decode_array_that_remaps_samples_is_refused_rather_than_dropped():
    """`/Decode [1 0]` on a DeviceGray image declares that sample 0 renders white.
    Dropping it sealed a black page where the source declares a white one — no
    error, no refusal, no signal at all. The identity array is legal and passes."""

    def with_decode(decode: str) -> bytes:
        entry = gray_image(2, 2, 0)
        entry["dictionary"] += f" /Decode {decode}"
        return image_page_pdf([entry])

    with pytest.raises(PdfRefusal) as caught:
        render_page(with_decode("[1 0]"), 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "remaps its samples" in str(caught.value)
    assert render_page(with_decode("[0 1]"), 0)[1] == "png"


@pytest.mark.parametrize("key", ["ImageMask", "Mask", "SMask", "DecodeParms"])
def test_every_key_that_changes_what_a_page_shows_is_refused_by_name(key):
    entry = gray_image(2, 2, 0)
    entry["dictionary"] += f" /{key} true"
    with pytest.raises(PdfRefusal) as caught:
        render_page(image_page_pdf([entry]), 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert key in str(caught.value)


def test_a_rendering_intent_is_not_a_sample_transform_and_does_not_refuse():
    """The other direction, and the one this repair got wrong first. `/Intent`
    annotates colour *conversion*, which this module performs none of — it stores
    DeviceGray and DeviceRGB samples as they are. Refusing every image carrying one
    would refuse a large share of real PDF output for a key that changes no sample,
    and a refused page is a page nobody reads (GOALS 1)."""
    entry = gray_image(2, 2, 0)
    entry["dictionary"] += " /Intent /RelativeColorimetric"
    assert render_page(image_page_pdf([entry]), 0)[1] == "png"


def test_a_decode_array_that_is_not_a_plain_array_is_refused_not_coerced():
    entry = gray_image(2, 2, 0)
    entry["dictionary"] += " /Decode 7 0 R"
    with pytest.raises(PdfRefusal, match="not a plain array"):
        render_page(image_page_pdf([entry]), 0)


def test_a_dct_page_is_checked_against_the_colour_space_its_dictionary_declares():
    """The DCTDecode branch never read /ColorSpace, so a page declaring DeviceCMYK
    with a four-component embedded JPEG rendered and would have been sealed — while
    this module's docstring said CMYK was refused by name."""
    cmyk = custom_image(
        5, 4, colorspace="DeviceCMYK", filter_name="DCTDecode", raw=jpeg(5, 4, components=4)
    )
    with pytest.raises(PdfRefusal) as caught:
        render_page(image_page_pdf([cmyk]), 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT

    # And a component count that disagrees with a *supported* colour space is corrupt
    # rather than unsupported: the container and the stream contradict each other.
    mismatch = custom_image(
        5, 4, colorspace="DeviceGray", filter_name="DCTDecode", raw=jpeg(5, 4, components=3)
    )
    with pytest.raises(PdfRefusal) as caught:
        render_page(image_page_pdf([mismatch]), 0)
    assert caught.value.reason is RefusalReason.CORRUPT


def test_rotate_false_is_refused_rather_than_read_as_unrotated():
    """`False == 0` in Python, so `/Rotate false` passed a `not in (0, None)` test."""
    with pytest.raises(PdfRefusal) as caught:
        render_page(single_gray_page_pdf(extra_page=" /Rotate false"), 0)
    assert caught.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "Rotate" in str(caught.value)


def test_bytes_appended_after_the_trailer_are_refused():
    """`validate_jpeg` refuses trailing bytes after EOI for exactly this reason and
    said so; the PDF path read back from the last `startxref` and never looked at
    what followed, so a payload rode inside the digest the run authority seals."""
    with pytest.raises(PdfRefusal, match="does not end at its own startxref"):
        count_pages(single_gray_page_pdf() + b"APPENDED NON-PDF PAYLOAD")
    with pytest.raises(PdfRefusal, match="does not end at its own startxref"):
        count_pages(single_gray_page_pdf().replace(b"%%EOF", b""))


def test_zero_entry_cross_reference_subsections_are_bounded():
    """A subsection may legally declare zero entries, so it costs nothing against
    MAX_XREF_ENTRIES. Two seats measured the resulting quadratic blow-up: unbounded
    header count, times an O(file) slice per header."""
    data = single_gray_page_pdf()
    padding = b"0 0\n" * (pdf_render.MAX_XREF_SUBSECTIONS + 1)
    stuffed = data.replace(b"xref\n0 ", b"xref\n" + padding + b"0 ", 1)
    # The padding goes *inside* the xref table, so the file still ends at its own
    # `%%EOF`: what refuses it is this bound and not the trailing-bytes rule added
    # beside it. A check that fires for the wrong reason is a check nobody has seen.
    assert stuffed.endswith(b"%%EOF")
    with pytest.raises(PdfRefusal, match="subsection"):
        count_pages(stuffed)


def test_the_cross_reference_parser_does_not_re_slice_the_file_per_subsection():
    """The bound alone is not the fix. `re.match(pattern, data[pos:])` copies the
    rest of the file every iteration, so the cost is quadratic in (headers x size)
    below the bound as well as above it. Matching in place is what removes it: a
    large tail must not change how long a fixed header count takes."""
    import time

    def elapsed(tail_bytes: int) -> float:
        data = single_gray_page_pdf()
        padding = b"0 0\n" * 2_000
        stuffed = data.replace(b"xref\n0 ", b"xref\n" + padding + b"0 ", 1)
        # Padding *after* %%EOF would be refused; this grows the file before the
        # xref instead, which is the size the old slice-per-iteration copied.
        stuffed = stuffed.replace(b"%PDF-1.4\n", b"%PDF-1.4\n%" + b"x" * tail_bytes + b"\n", 1)
        start = time.perf_counter()
        try:
            count_pages(stuffed)
        except PdfRefusal:
            pass
        return time.perf_counter() - start

    small, large = elapsed(10_000), elapsed(2_000_000)
    # A 200x larger file, the same 2,000 headers. Quadratic behaviour showed up as
    # roughly 200x here; a generous ceiling still catches a regression to slicing.
    assert large < max(0.05, small * 20), (small, large)


def test_a_document_is_parsed_once_however_many_pages_are_rendered():
    """`render_page` used to re-parse the whole cross-reference table and page tree
    per call, and the door calls it once per page, so an N-page scan paid it N+1
    times — which made the declared MAX_PAGES limit unreachable in practice."""
    parses = 0
    real_parse = pdf_render._parse_xref_table

    def counting(*args, **kwargs):
        nonlocal parses
        parses += 1
        return real_parse(*args, **kwargs)

    data = image_page_pdf([gray_image(2, 2, value) for value in range(8)])
    document = open_document(data)
    pdf_render._parse_xref_table = counting
    try:
        for index in range(len(document.pages)):
            pdf_render.render_page(document, index)
    finally:
        pdf_render._parse_xref_table = real_parse
    assert parses == 0, "rendering out of a prepared document re-parses nothing"


def test_the_page_tree_bounds_its_branches_and_not_only_its_leaves():
    """MAX_PAGES bounds the leaves; each intermediate node costs its own object
    lookup, and nothing bounded how many of those a file could declare. A verifying
    seat built a *valid, renderable, one-page* PDF with 80,000 empty `/Pages` nodes
    and measured 87 seconds for the door's two walks over it.

    Built with a proper trailer and a real page, so what refuses it is this bound and
    not the trailing-bytes rule added beside it — a check that fires for the wrong
    reason is a check nobody has seen work.
    """
    builder = PdfBuilder()
    image = builder.add(
        stream_object(gray_image(2, 2, 7)["dictionary"], gray_image(2, 2, 7)["raw"])
    )
    catalog, root = builder.add(), builder.add()
    page = builder.add(
        f"<< /Type /Page /Parent {root} 0 R /Resources << /XObject << /Im0 {image} 0 R >> >> "
        "/MediaBox [0 0 2 2] >>".encode()
    )
    groups = []
    for _ in range(3):
        empties = [builder.add(b"<< /Type /Pages /Kids [] /Count 0 >>") for _ in range(4_000)]
        kids = " ".join(f"{number} 0 R" for number in empties)
        groups.append(builder.add(f"<< /Type /Pages /Kids [{kids}] /Count 0 >>".encode()))
    branches = " ".join(f"{number} 0 R" for number in groups)
    builder.objects[root] = f"<< /Type /Pages /Kids [{page} 0 R {branches}] /Count 1 >>".encode()
    builder.objects[catalog] = f"<< /Type /Catalog /Pages {root} 0 R >>".encode()

    with pytest.raises(PdfRefusal, match="page tree visits more than"):
        count_pages(builder.build(catalog))


def test_a_few_empty_cross_reference_subsections_are_legal_and_parse():
    """The control for the bound above, and the reason the bound is a bound rather
    than a ban: a subsection declaring zero entries is legal PDF. Twenty of them,
    against a limit of ten thousand, must still parse — otherwise the fix for a
    cost problem would have become a refusal of conforming files."""
    data = single_gray_page_pdf()
    stuffed = data.replace(b"xref\n0 ", b"xref\n" + b"0 0\n" * 20 + b"0 ", 1)
    assert stuffed.endswith(b"%%EOF")
    assert count_pages(stuffed) == 1
