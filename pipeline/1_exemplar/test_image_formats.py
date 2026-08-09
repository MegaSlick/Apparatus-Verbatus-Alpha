"""Structural validators and decoder-backed page handling, proven synthetically.

Every fixture here is built in memory by `synthetic_sources.py`, never a checked-in
binary: the ingress guard only allows png/jpeg/tiff media types under
`proof/fixtures/`, this module also needs to prove corrupt and PDF/GIF/HEIC bytes,
and nothing here is register material in the first place — GOVERNANCE's
synthetic-fixture rule applies to bytes that stand for a page, and these do not.

The structural walkers distinguish malformed bytes from documented decoder limits.
Their errors become closed admission alarms; no test below permits a format-policy
refusal to take their place.
"""

import struct
import tracemalloc
import zlib
from io import BytesIO

import image_formats
import pytest
from image_formats import (
    MAX_DIMENSION,
    MIN_BYTES_PER_DECLARED_FRAME,
    MIN_BYTES_PER_DECLARED_TIFF_PAGE,
    DecodedRaster,
    FormatRefusal,
    ImageGeometry,
    _tiff_unsigned_values,
    count_raster_pages,
    decode_raster,
    render_raster_page,
    sniff,
    validate,
    validate_jpeg,
    validate_png,
    validate_tiff,
)
from PIL import Image
from synthetic_sources import (
    PNG_MAGIC,
    gif,
    heic,
    jpeg,
    png,
    png_chunk,
    png_container,
    tiff,
    tiff_ifd_entry_offset,
    tiff_next_ifd_offset,
)

# --- sniff -----------------------------------------------------------------------


def test_sniff_recognizes_every_explicitly_named_format():
    assert sniff(png()) == "png"
    assert sniff(jpeg()) == "jpeg"
    assert sniff(tiff()) == "tiff"
    assert sniff(b"%PDF-1.4\n%...") == "pdf"
    assert sniff(b"\n\n%PDF-1.4\n%...") == "pdf"
    assert sniff(gif()) == "gif"
    assert sniff(heic()) == "heic"
    assert sniff(struct.pack(">I", 16) + b"ftyp" + b"avif" + b"\x00" * 4) == "avif"
    assert sniff(struct.pack(">I", 16) + b"ftyp" + b"mif1" + b"\x00" * 4) == "heif"
    assert sniff(b"BM" + b"\x00" * 16) == "bmp"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBP") == "webp"


def test_sniff_returns_none_for_unrecognized_bytes():
    assert sniff(b"not an image at all, just text\x00\x01\x02") is None


def test_sniff_does_not_call_an_unrelated_iso_container_a_heic():
    """`ftyp` alone is every MP4 and MOV ever made; the brand is what distinguishes
    a HEIC image, and mistaking one for the other would refuse a video by the wrong
    name and admit nothing either way."""
    mp4 = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00" * 4 + b"isom" + b"mp42"
    assert sniff(mp4) is None


def test_heic_brand_sniffing_does_not_allocate_from_the_ftyp_box_size():
    """A reader route still crosses the sniffer; its header cannot size a list."""
    box_size = 256 * 1024
    data = (
        struct.pack(">I", box_size)
        + b"ftyp"
        + b"isom"
        + b"\x00" * 4
        + b"mp42" * ((box_size - 16) // 4)
    )

    tracemalloc.start()
    try:
        assert sniff(data) is None
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024


# --- PNG -------------------------------------------------------------------------


def test_validate_png_reads_geometry_off_a_genuine_file():
    assert validate_png(png(width=10, height=7)) == ImageGeometry("png", 10, 7)


def test_validate_png_accepts_every_valid_color_type_and_bit_depth():
    checked = 0
    for color_type, depths in (
        (0, (1, 2, 4, 8, 16)),
        (2, (8, 16)),
        (3, (1, 2, 4, 8)),
        (4, (8, 16)),
        (6, (8, 16)),
    ):
        for depth in depths:
            geometry = validate_png(png(9, 2, bit_depth=depth, color_type=color_type))
            assert (geometry.width, geometry.height) == (9, 2)
            checked += 1
    assert checked == 15


def test_validate_png_accounts_for_an_interlaced_image_rather_than_refusing_it():
    """Adam7's per-pass byte accounting is exact, so an interlaced scan is checked
    the same way a progressive one is. A refused page is a page nobody reads."""
    assert validate_png(png(9, 5, interlace=1)) == ImageGeometry("png", 9, 5)


def test_validate_png_refuses_an_interlaced_image_whose_passes_do_not_add_up():
    """The interlaced path is only worth having if it counts: a stream one byte
    short of the Adam7 total must fail, or "accounted for" means "waved through"."""
    good = png(9, 5, interlace=1)
    start = good.find(b"IDAT") + 4
    length = struct.unpack_from(">I", good, start - 8)[0]
    raw = zlib.decompress(good[start : start + length])
    broken = png_container(
        (b"IHDR", struct.pack(">IIBBBBB", 9, 5, 8, 0, 0, 0, 1)),
        (b"IDAT", zlib.compress(raw[:-1], 9)),
        (b"IEND", b""),
    )
    with pytest.raises(FormatRefusal, match="wrong length"):
        validate_png(broken)


def test_validate_png_refuses_a_bad_chunk_crc():
    data = bytearray(png())
    data[data.find(b"IDAT") + 6] ^= 0xFF
    with pytest.raises(FormatRefusal, match="CRC"):
        validate_png(bytes(data))


def test_validate_png_refuses_a_truncated_file():
    data = png()
    with pytest.raises(FormatRefusal):
        validate_png(data[: data.find(b"IDAT") + 20])


def test_validate_png_refuses_an_invalid_bit_depth_for_its_color_type():
    with pytest.raises(FormatRefusal, match="bit depth"):
        validate_png(png(bit_depth=16, color_type=3))  # palette has no 16-bit form


def test_validate_png_refuses_a_row_with_an_unknown_filter_byte():
    rows = b"".join(bytes([9]) + bytes([1] * 4) for _ in range(3))
    with pytest.raises(FormatRefusal, match="filter"):
        validate_png(png(4, 3, rows=rows))


def test_validate_png_refuses_an_indexed_image_with_no_palette():
    body = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 2, 8, 3, 0, 0, 0))
    body += png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00" * 2, 9))
    with pytest.raises(FormatRefusal, match="no palette"):
        validate_png(PNG_MAGIC + body + png_chunk(b"IEND", b""))


def test_validate_png_refuses_a_palette_larger_than_its_bit_depth_can_index():
    with pytest.raises(FormatRefusal, match="palette"):
        validate_png(png(4, 2, bit_depth=2, color_type=3, palette=bytes(3 * 8)))


def test_validate_png_refuses_an_unknown_critical_chunk():
    """An unrecognized *ancillary* chunk is skippable by the format's own rules; an
    unrecognized *critical* one means the file needs something this reader does not
    have, and reading past it would be a guess."""
    # An uppercase first letter is what makes a chunk critical, per the format's own
    # case convention — the same bit `_is_png_critical` reads.
    extra = png_chunk(b"CrIt", b"\x00\x01")
    with pytest.raises(FormatRefusal, match="critical"):
        validate_png(png(extra_chunks=extra))


def test_validate_png_accepts_an_unknown_ancillary_chunk():
    assert validate_png(png(extra_chunks=png_chunk(b"tEXt", b"note\x00hello"))).width == 4


def test_validate_png_refuses_a_chunk_type_with_the_reserved_bit_set():
    """Byte 3's case is PNG's reserved bit and the format requires it clear. A chunk
    typed `abcd` with a valid CRC is a name no conforming encoder can emit, and
    "four ASCII letters" waved it through as an ordinary ancillary chunk."""
    with pytest.raises(FormatRefusal, match="reserved bit"):
        validate_png(png(extra_chunks=png_chunk(b"abcd", b"reserved-bit-set")))
    # The control: the same chunk with the reserved bit clear is legal and skipped.
    assert validate_png(png(extra_chunks=png_chunk(b"abCd", b"reserved-bit-set"))).width == 4


def test_validate_png_retains_trailing_bytes_after_iend():
    assert validate_png(png() + b"appended payload") == ImageGeometry("png", 4, 3)


def test_validate_png_bounds_a_compression_bomb_by_the_declared_geometry():
    """The one place in this pipeline that inflates bytes nobody has verified. The
    inflate is capped at one byte past the declared size, `flush()` included, so
    the overrun is never held in memory to be measured."""
    bomb = png_container(
        (b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 0)),
        (b"IDAT", zlib.compress(b"\x00" * (8 * 1024 * 1024), 9)),
        (b"IEND", b""),
    )
    with pytest.raises(FormatRefusal, match="past its own declared size"):
        validate_png(bomb)


def test_validate_png_refuses_a_geometry_past_the_admission_limits():
    header = struct.pack(">IIBBBBB", MAX_DIMENSION + 1, 2, 8, 0, 0, 0, 0)
    with pytest.raises(FormatRefusal, match="exceeds the admission limits"):
        validate_png(png_container((b"IHDR", header), (b"IEND", b"")))


def test_validate_png_refuses_non_png_bytes():
    with pytest.raises(FormatRefusal, match="signature"):
        validate_png(b"definitely not a png")


# --- JPEG ----------------------------------------------------------------------


def test_validate_jpeg_reads_geometry_off_a_genuine_file():
    assert validate_jpeg(jpeg(12, 9)) == ImageGeometry("jpeg", 12, 9)


def test_validate_jpeg_accepts_progressive_frames_and_restart_markers_in_scan_data():
    scan = b"\x11\xff\xd0\x22\xff\xd1\x33\x00\xff"  # RST0, RST1, and a stuffed 0xFF00
    assert validate_jpeg(jpeg(sof_marker=0xC2, scan=scan)) == ImageGeometry("jpeg", 5, 4)


def test_validate_jpeg_refuses_a_missing_eoi():
    with pytest.raises(FormatRefusal, match="no terminating marker"):
        validate_jpeg(jpeg(eoi=False))


def test_validate_jpeg_accepts_scanner_bytes_appended_after_eoi():
    """EOI closes a JPEG; scanners commonly preserve harmless suffix bytes."""
    assert validate_jpeg(jpeg(trailing=b"scanner-metadata-after-eoi")) == ImageGeometry(
        "jpeg", 5, 4
    )


def test_validate_jpeg_refuses_no_start_of_frame():
    with pytest.raises(FormatRefusal, match="frame"):
        validate_jpeg(b"\xff\xd8\xff\xd9")


def test_validate_jpeg_refuses_a_segment_that_runs_past_eof():
    data = bytearray(jpeg())
    struct.pack_into(">H", data, 4, 0xFFFF)  # the SOF segment's declared length
    with pytest.raises(FormatRefusal, match="past EOF"):
        validate_jpeg(bytes(data))


def test_validate_jpeg_refuses_a_scan_declaring_more_components_than_its_frame():
    data = bytearray(jpeg())
    sos = data.find(b"\xff\xda")
    data[sos + 4] = 4  # the scan claims four components; the frame declared one
    with pytest.raises(FormatRefusal, match="scan components"):
        validate_jpeg(bytes(data))


def test_validate_jpeg_refuses_non_jpeg_bytes():
    with pytest.raises(FormatRefusal, match="SOI"):
        validate_jpeg(b"definitely not a jpeg")


# --- the tables a scan selects have to exist ---------------------------------------
#
# The validator claimed to prove "a genuine, uncorrupted instance of the format".
# It checked no quantization or Huffman table at all, and the project's own
# "genuine" JPEG builder emitted neither: thirty bytes, four markers, three
# arbitrary scan bytes, admitted as a 5x4 image no decoder could have read.


def test_validate_jpeg_refuses_a_frame_whose_quantization_table_was_never_defined():
    with pytest.raises(FormatRefusal, match="quantization table 0, which no DQT defined"):
        validate_jpeg(jpeg(quantization_tables=None))


def test_validate_jpeg_refuses_a_scan_whose_huffman_tables_were_never_defined():
    with pytest.raises(FormatRefusal, match="Huffman table 0, which no DHT defined"):
        validate_jpeg(jpeg(huffman_tables=None))


def test_validate_jpeg_refuses_a_scan_selecting_only_half_the_tables_it_needs():
    """A sequential scan uses both a DC and an AC table. Defining one is not enough,
    and refusing on "no tables at all" alone would miss this."""
    with pytest.raises(FormatRefusal, match="AC Huffman table 0"):
        validate_jpeg(jpeg(huffman_tables=((0, 0),)))


def test_a_progressive_ac_scan_needs_only_its_ac_table():
    """Which tables a scan needs depends on what kind of scan it is, and demanding
    both of a progressive scan would refuse ordinary progressive JPEGs — a page
    nobody reads, for a check that was never owed."""
    assert validate_jpeg(
        jpeg(sof_marker=0xC2, spectral_start=1, huffman_tables=((1, 0),))
    ) == ImageGeometry("jpeg", 5, 4)
    with pytest.raises(FormatRefusal, match="DC Huffman table 0"):
        validate_jpeg(jpeg(sof_marker=0xC2, spectral_start=0, huffman_tables=((1, 0),)))


def test_validate_jpeg_refuses_a_dht_segment_that_runs_past_its_own_length():
    """A table declaring more codes than it carries leaves a scan pointing at
    something never fully written."""
    truncated = bytes([0x00]) + bytes([9] + [0] * 15) + bytes([0])
    with pytest.raises(FormatRefusal, match="DHT table runs past its own segment"):
        validate_jpeg(
            jpeg(huffman_tables=None, quantization_tables=(0,)).replace(
                b"\xff\xc0",
                b"\xff\xc4" + struct.pack(">H", len(truncated) + 2) + truncated + b"\xff\xc0",
            )
        )


def test_the_container_around_an_embedded_jpeg_can_pin_its_component_count():
    """What the PDF renderer needs to stop guessing: a page declaring one colour
    space and embedding a JPEG with a different number of components is not the
    image its own dictionary describes."""
    assert validate_jpeg(jpeg(components=3), expected_components=3).width == 5
    with pytest.raises(FormatRefusal, match="the container around it declares 1"):
        validate_jpeg(jpeg(components=4), expected_components=1)


# --- TIFF ------------------------------------------------------------------------


def test_validate_tiff_reads_geometry_little_and_big_endian():
    assert validate_tiff(tiff(6, 5)) == ImageGeometry("tiff", 6, 5)
    assert validate_tiff(tiff(8, 3, little_endian=False)) == ImageGeometry("tiff", 8, 3)


def test_tiff_strip_counts_are_bounded_before_struct_allocates_from_the_header():
    count = 100_001
    with pytest.raises(FormatRefusal, match="segment limit"):
        _tiff_unsigned_values(b"\x00\x00" * count, 3, count, "<")


def test_validate_tiff_accepts_long_typed_tags():
    assert validate_tiff(tiff(70000, 2, tag_type=4)) == ImageGeometry("tiff", 70000, 2)


def test_validate_tiff_refuses_bigtiff_magic():
    with pytest.raises(FormatRefusal, match="BigTIFF"):
        validate_tiff(tiff(magic=43))


def test_validate_tiff_refuses_an_image_with_no_strip_or_tile_inventory():
    """A header with no image data behind it is a description of an image, not one."""
    with pytest.raises(FormatRefusal, match="no strip or tile"):
        validate_tiff(tiff(strips=False))


def test_validate_tiff_refuses_an_image_data_range_outside_the_file():
    data = bytearray(tiff())
    strip_count_entry = data.find(struct.pack("<HHI", 279, 4, 1)) + 8
    struct.pack_into("<I", data, strip_count_entry, 1 << 20)
    with pytest.raises(FormatRefusal, match="outside the file"):
        validate_tiff(bytes(data))


def test_validate_tiff_refuses_a_value_offset_outside_the_file():
    data = bytearray(tiff())
    # Entry 0 is ImageWidth; a count of 100 forces its value out of line.
    struct.pack_into("<I", data, tiff_ifd_entry_offset(0) + 4, 100)
    with pytest.raises(FormatRefusal, match="escapes the file bounds|more values than the file"):
        validate_tiff(bytes(data))


def test_validate_tiff_refuses_missing_geometry_tags():
    header = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    body = struct.pack("<H", 0) + struct.pack("<I", 0)
    with pytest.raises(FormatRefusal, match="ImageWidth"):
        validate_tiff(header + body)


def test_validate_tiff_refuses_a_geometry_tag_with_a_zero_count_rather_than_crashing():
    """A `count == 0` ImageWidth entry leaves no value bytes at all. This proves the
    fail-closed check: a named refusal rather than a bare `struct.error` escaping
    uncaught and aborting every other source still waiting to be decided."""
    data = bytearray(tiff())
    struct.pack_into("<I", data, tiff_ifd_entry_offset(0) + 4, 0)
    with pytest.raises(FormatRefusal, match="not one SHORT or LONG"):
        validate_tiff(bytes(data))


def _two_page_tiff() -> bytes:
    """Two synthetic TIFF pages with deliberately different geometries."""
    output = BytesIO()
    first = Image.new("L", (4, 3), 17)
    second = Image.new("L", (2, 5), 231)
    first.save(output, format="TIFF", save_all=True, append_images=[second])
    return output.getvalue()


def test_multi_page_tiff_is_a_page_container_that_can_be_counted_and_rendered():
    """The first structural walk remains useful; the door owns every later page."""
    data = _two_page_tiff()
    assert validate_tiff(data) == ImageGeometry("tiff", 4, 3)
    assert count_raster_pages(data) == 2
    assert decode_raster(data, page_index=1).width == 2
    rendered, geometry, contract = render_raster_page(data, 1)
    assert validate_png(rendered) == ImageGeometry("png", 2, 5)
    assert geometry == ImageGeometry("tiff", 2, 5)
    assert contract["container_page_index"] == 1


@pytest.mark.parametrize(
    ("mode", "first", "second", "codec", "transform"),
    [
        ("RGBA", (1, 2, 3, 4), (5, 6, 7, 8), "png", "identity"),
        ("I;16", 1_000, 50_000, "png", "identity"),
        # PNG cannot hold these Pillow sample modes without turning them into
        # 8-bit display pixels. The fanned Exemplar page must preserve values, so
        # it uses lossless TIFF instead.
        ("I", 1_000, 70_000, "tiff", "lossless-tiff-samples"),
        ("F", 0.5, 1_000.25, "tiff", "lossless-tiff-samples"),
    ],
)
def test_raster_fan_out_preserves_alpha_and_high_precision_samples(
    mode, first, second, codec, transform
):
    output = BytesIO()
    Image.new(mode, (2, 2), first).save(
        output,
        format="TIFF",
        save_all=True,
        append_images=[Image.new(mode, (2, 2), second)],
    )

    rendered, _, contract = render_raster_page(output.getvalue(), 1)
    with Image.open(BytesIO(rendered)) as page:
        page.load()
        assert page.mode == mode
        assert page.getpixel((0, 0)) == second
    assert contract["source_mode"] == mode
    assert contract["mode_transform"] == transform
    assert contract["output"] == {"codec": codec, "color_mode": mode}


def test_a_cyclic_ifd_chain_is_still_a_named_decoder_failure():
    """Normal later directories are fanned out; a loop is not a normal container."""
    first = tiff()
    data = bytearray(first)
    struct.pack_into("<I", data, tiff_next_ifd_offset(first), 8)
    with pytest.raises(FormatRefusal):
        count_raster_pages(bytes(data))


def test_a_classic_tiff_naming_no_image_directory_is_corrupt_not_silently_empty():
    """A first-IFD offset of 0 is damage, not an empty container of zero pages.

    `validate_tiff` already refuses this exact header shape as CORRUPT. Before this
    test, `_validate_classic_tiff_page_chain`'s `while offset:` loop disagreed: it
    returned a page count of 0 for the identical bytes, which routed the source to
    zero fanned ordinals -- admitted nowhere, refused nowhere, and absent from the
    run's own source manifest. A page count of zero must never be how a real file
    goes unaccounted for.
    """
    data = b"II*\x00" + struct.pack("<I", 0) + b"\x00" * 4
    with pytest.raises(FormatRefusal, match="the header names no image directory"):
        count_raster_pages(data)


def test_a_chain_of_empty_directories_cannot_declare_more_pages_than_bytes_allow():
    """Six bytes may not buy a page ordinal, however many times it is repeated.

    A directory the chain walk accepts costs two bytes of entry count and four of
    next-offset, and nothing in that shape requires an actual image. Chaining empty
    directories six bytes apart therefore declared one page per six submitted bytes:
    measured before the floor existed, 1.2 MB fanned out to 200,000 ordinals through
    the real `expand_sources` in 0.25 seconds, and the 64 MiB source ceiling put the
    worst case near eleven million. Each ordinal is a decode attempt and an artifact.

    This is the TIFF half of the page-tree amplification already refused in
    `pdf_render`, and it is deliberately not the page cap ruling 17 retired: a real
    reel's page count is the document's to declare, and the test below proves a
    genuine thousand-page TIFF still passes with room to spare.
    """
    pages = 10_000
    stride = 6
    body = bytearray(stride * pages)
    for index in range(pages):
        at = stride * index
        struct.pack_into("<H", body, at, 0)
        struct.pack_into("<I", body, at + 2, 0 if index == pages - 1 else 8 + stride * (index + 1))
    data = b"II*\x00" + struct.pack("<I", 8) + bytes(body)

    with pytest.raises(FormatRefusal, match="below the .* bytes any real page needs"):
        count_raster_pages(data)


def test_an_apng_cannot_declare_more_frames_than_its_bytes_could_hold():
    """A frame count read out of a header is a loop bound somebody else wrote.

    APNG takes its count straight from the `acTL` chunk, so unlike the TIFF chain it
    does not scale with file size at all: measured before this floor existed, a
    125-byte APNG declaring a million frames was counted as a million pages and
    `expand_sources` fanned it out. The classic-TIFF walk returns before this check,
    so this is the bound for every container that walk never sees.
    """

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    data = (
        PNG_MAGIC
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"acTL", struct.pack(">II", 1_000_000, 0))
        + chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 1, 1, 0, 0, 1, 1, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00", 9))
        + chunk(b"IEND", b"")
    )

    assert len(data) < 32 * 1_000_000
    with pytest.raises(FormatRefusal, match="declared frames in"):
        count_raster_pages(data)


def test_a_real_animation_is_counted_and_never_reaches_the_frame_floor():
    """The floor above must never refuse a genuine multi-frame raster."""
    frames = []
    for index in range(60):
        image = Image.new("P", (16, 16), 0)
        pixels = image.load()
        for x in range(16):
            for y in range(16):
                pixels[x, y] = (x * y + index * 7) % 256
        frames.append(image)
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:])
    data = output.getvalue()

    counted = count_raster_pages(data)
    assert counted > 1
    assert len(data) / counted > MIN_BYTES_PER_DECLARED_FRAME


def test_a_real_thousand_page_tiff_is_counted_and_never_reaches_the_page_floor():
    """The floor above must never be what refuses a genuine multi-page scan.

    Pages this small are the closest a legitimate document can get to it: 1x1
    pixels is the least page Pillow will write, and it still costs ~128 bytes,
    four times the floor. A real scanned register page is orders larger again.
    """
    output = BytesIO()
    first = Image.new("L", (1, 1), 17)
    rest = [Image.new("L", (1, 1), 200) for _ in range(999)]
    first.save(output, format="TIFF", save_all=True, append_images=rest)
    data = output.getvalue()

    assert count_raster_pages(data) == 1000
    assert len(data) / 1000 > MIN_BYTES_PER_DECLARED_TIFF_PAGE


def test_a_bad_later_tiff_page_keeps_a_good_earlier_page_counted_and_decodable():
    """A per-page tag defect must not erase a good earlier TIFF page.

    The classic IFD chain remains complete, so it supplies the fan-out denominator
    without asking Pillow to traverse the bad page. Pillow then reads page zero on
    its own merits and refuses only page one when its own tag layout is reached.
    """
    data = bytearray(_two_page_tiff())
    little_endian = data[:2] == b"II"
    endian = "<" if little_endian else ">"
    (first_entries,) = struct.unpack_from(endian + "H", data, 8)
    next_ifd_at = 8 + 2 + first_entries * 12
    (second_ifd,) = struct.unpack_from(endian + "I", data, next_ifd_at)
    second_image_width_entry = second_ifd + 2  # entry 0 is always ImageWidth
    # LONG, count=2 (size 8 > 4) forces an out-of-line read; the offset is garbage.
    struct.pack_into(endian + "I", data, second_image_width_entry + 4, 2)
    struct.pack_into(endian + "I", data, second_image_width_entry + 8, len(data) + 999_999)

    assert count_raster_pages(bytes(data)) == 2
    assert decode_raster(bytes(data), page_index=0).width == 4
    with pytest.raises(FormatRefusal):
        decode_raster(bytes(data), page_index=1)


# --- the decoder boundary: whose code raised it, not which class it was -----------


def _two_frame_gif() -> bytes:
    """A real animated GIF, written by the encoder rather than hand-assembled."""
    first, second = Image.new("P", (16, 16), 0), Image.new("P", (16, 16), 0)
    for x in range(16):
        for y in range(16):
            first.putpixel((x, y), (x + y) % 8)
            second.putpixel((x, y), (x * y) % 8)
    output = BytesIO()
    first.save(output, "GIF", save_all=True, append_images=[second])
    return output.getvalue()


def test_no_complete_gif_prefix_is_admitted_after_its_header():
    """The whole-Door crash, one byte along from where it was closed.

    A previous round widened this module's catch set to the classes Pillow had been
    seen to raise; the next narrowed it by removing `KeyError`, `IndexError` and
    `AttributeError`, reasoning that the guarded block also held project-owned
    routing and geometry code and a programming defect must not be relabelled as a
    corrupt image. The reasoning was right and its premise was false: Pillow raises
    `IndexError` on ordinary malformed input. Cut this GIF inside its second image
    descriptor and `n_frames` fails at `GifImagePlugin._seek` with a bare
    `IndexError` — which escaped `decode_raster`, escaped `expand_sources`, and took
    down every other source in the submission before any admission could be written.

    Sweeping every truncation rather than pinning the one offset is deliberate:
    Pillow admits some strict prefixes as a one-frame GIF. The container trailer and
    block walk now make every prefix that still names GIF a named corruption alarm,
    instead of an immutable Exemplar page that silently lost its later frame."""
    data = _two_frame_gif()
    for cut in range(len(image_formats.GIF_SIGNATURES[0]), len(data)):
        with pytest.raises(FormatRefusal) as caught:
            decode_raster(data[:cut])
        assert caught.value.verdict is image_formats.FormatVerdict.CORRUPT


def test_a_project_corruption_verdict_is_not_relabelled_as_a_decoder_gap():
    """`FormatRefusal` subclasses `ValueError`, and the broad clause used to catch
    it: a `corrupt` verdict this module raised itself came back out as
    `unsupported`, with its real reason inside a parenthesis. Ruling 2 makes those
    two different sentences to write — damaged bytes, or a reader this build owes —
    so collapsing them into the wrong one loses the alarm's meaning."""
    with pytest.raises(FormatRefusal) as caught:
        decode_raster(_two_frame_gif(), page_index=7)
    assert caught.value.verdict is image_formats.FormatVerdict.CORRUPT
    assert "page index 7 is outside" in str(caught.value)


# --- what a TIFF actually stores has to hold the image it declares ----------------


def test_validate_tiff_refuses_a_strip_too_small_for_the_geometry_it_declares():
    """The shipped fixture used to declare 6x5 pixels behind a one-byte strip, and
    "the strip is inside the file" was the whole of the check. An uncompressed image
    stores exactly what its rows occupy or it is not that image."""
    with pytest.raises(FormatRefusal, match="needs 30"):
        validate_tiff(tiff(6, 5, strip_bytes=1))


def test_an_uncompressed_tiff_can_retain_padding_after_its_last_strip():
    """A decoder ignores end padding; larger storage is not missing pixel data."""
    data = bytearray(tiff(4, 3)) + b"\x00" * 4
    count_offset = data.find(struct.pack("<HHI", 279, 4, 1)) + 8
    struct.pack_into("<I", data, count_offset, 16)

    assert validate_tiff(bytes(data)) == ImageGeometry("tiff", 4, 3)
    assert decode_raster(bytes(data)) == DecodedRaster("tiff", 4, 3, 1)


def test_validate_tiff_refuses_a_header_declaring_far_more_pixels_than_it_stores():
    """The sharper form: a 123-byte file declaring a million pixels, which the old
    validator accepted because the one stored byte was technically inside it."""
    with pytest.raises(FormatRefusal, match=r"stores 1 byte\(s\) where .* needs 1000000"):
        validate_tiff(tiff(1000, 1000, strip_bytes=1))


def test_validate_tiff_counts_the_strips_the_geometry_requires():
    """Three rows at one row per strip is three strips. Declaring one is a file that
    does not carry the image its own header describes."""
    data = tiff(4, 3)
    rows_per_strip = data.find(struct.pack("<HHI", 278, 4, 1)) + 8
    patched = bytearray(data)
    struct.pack_into("<I", patched, rows_per_strip, 1)
    with pytest.raises(FormatRefusal, match="needs 3"):
        validate_tiff(bytes(patched))


def test_validate_tiff_refuses_a_baseline_image_with_no_photometric_tag():
    """`PhotometricInterpretation` says what the stored samples mean. A baseline IFD
    is required to carry it, and defaulting it would be inventing the answer."""
    with pytest.raises(FormatRefusal, match="PhotometricInterpretation"):
        validate_tiff(tiff(photometric=None))


def test_a_compressed_tiff_reconciles_its_segment_count_and_not_its_byte_counts():
    """The named limit, stated as a test rather than only as prose: byte counts are
    whatever the codec produced and reconciling them would mean decompressing, so a
    compressed strip of any size passes — while the *number* of strips is still the
    number the geometry requires."""
    assert validate_tiff(tiff(6, 5, compression=5, strip_bytes=3)) == ImageGeometry("tiff", 6, 5)
    data = tiff(4, 3, compression=5)
    patched = bytearray(data)
    struct.pack_into("<I", patched, data.find(struct.pack("<HHI", 278, 4, 1)) + 8, 1)
    with pytest.raises(FormatRefusal, match="needs 3"):
        validate_tiff(bytes(patched))


def test_validate_tiff_refuses_non_tiff_bytes():
    with pytest.raises(FormatRefusal, match="byte-order"):
        validate_tiff(b"definitely not a tiff")


# --- dispatch --------------------------------------------------------------------


def test_validate_dispatches_by_format_name():
    assert validate("png", png()).format == "png"
    assert validate("jpeg", jpeg()).format == "jpeg"
    assert validate("gif", _two_frame_gif()).format == "gif"
    assert validate("tiff", tiff()).format == "tiff"


def test_validate_refuses_a_format_it_has_no_validator_for():
    with pytest.raises(FormatRefusal, match="no structural validator"):
        validate("webp", b"anything")


def test_a_lossless_frame_needs_only_its_dc_table():
    """A lossless frame (SOF3/C7/CB/CF) codes DC coefficients only and legally
    defines no AC table. The first form of this check demanded both and refused a
    conforming file — a page nobody reads, for a check that was never owed. Caught
    by testing the widening against variants rather than only against the gap."""
    for marker in (0xC3, 0xC7):
        assert validate_jpeg(
            jpeg(sof_marker=marker, huffman_tables=((0, 0),), spectral_start=1)
        ) == ImageGeometry("jpeg", 5, 4)
    with pytest.raises(FormatRefusal, match="DC Huffman table 0"):
        validate_jpeg(jpeg(sof_marker=0xC3, huffman_tables=((1, 0),), spectral_start=1))


def test_an_arithmetic_coded_frame_needs_no_huffman_table_at_all():
    """Arithmetic coding replaces the Huffman tables with conditioning that may be
    left at its default, so there is no selector to reconcile — while the
    quantization table every frame component names is still required."""
    assert validate_jpeg(jpeg(sof_marker=0xC9, huffman_tables=None)) == ImageGeometry("jpeg", 5, 4)
    with pytest.raises(FormatRefusal, match="quantization table 0"):
        validate_jpeg(jpeg(sof_marker=0xC9, huffman_tables=None, quantization_tables=None))


def test_a_conforming_lossless_jpeg_carries_no_quantization_table_and_is_admitted():
    """A lossless frame does not quantize, so it legally carries no DQT and its Tq
    field is zero. The first form of this repair exempted lossless frames from the
    *Huffman* check and not the *quantization* one, so it still refused every
    conforming lossless JPEG while its own test passed — because the builder emitted
    a DQT no real lossless encoder would."""
    for marker in (0xC3, 0xC7, 0xCB, 0xCF):
        assert validate_jpeg(
            jpeg(
                sof_marker=marker,
                quantization_tables=None,
                huffman_tables=((0, 0),),
                spectral_start=1,
            )
        ) == ImageGeometry("jpeg", 5, 4)


def test_a_frame_with_more_components_than_this_door_decodes_is_unsupported_not_corrupt():
    """T.81 permits up to 255 components; four is the baseline convention and the
    limit of what anything here handles. A genuine instance of a variant this door
    does not decode is "unsupported"; calling it corruption would tell Tyrel a real
    scan was damaged when the truth is that we cannot read that flavour of it."""
    with pytest.raises(FormatRefusal, match="unsupported JPEG: 5 components"):
        validate_jpeg(jpeg(components=5))


def test_an_unknown_field_type_in_a_tag_this_validator_never_reads_is_skipped():
    """TIFF 6.0 tells a reader to skip a field whose type it does not recognise
    rather than reject the file. Refusing the whole image over an unknown type in a
    tag nobody here touches is a page nobody reads for a field nobody read."""
    unknown_type = struct.pack("<HHI", 700, 129, 1) + b"\x00\x00\x00\x00"
    assert validate_tiff(
        tiff(6, 5, extra_entries=unknown_type, extra_entry_count=1)
    ) == ImageGeometry("tiff", 6, 5)

    # But a tag this validator has to read is a check that cannot run, which fails.
    unreadable_width = struct.pack("<HHI", 278, 129, 1) + b"\x00\x00\x00\x00"
    with pytest.raises(FormatRefusal, match="has to read that tag"):
        validate_tiff(tiff(6, 5, extra_entries=unreadable_width, extra_entry_count=1))


# --- Structural walking is corruption detection, and only that --------------------


def test_structurally_damaged_tiff_bytes_refuse_before_a_decoder_sees_them():
    """A `corrupt ...` structural verdict still refuses, and refuses as damage.

    This is the half of structural validation the spec keeps: proving the bytes are
    a genuine instance of what they claim. Here an uncompressed image's stored strip
    is truncated to a byte, so the file claims a 6x5 image behind storage that
    cannot hold one. A permissive decoder can render most of that as a page, which
    is how a damaged transfer becomes a plausible-looking Exemplar.
    """
    data = bytearray(tiff(6, 5))
    strip_byte_count = _tiff_tag_value_offset(bytes(data), 279)
    struct.pack_into("<I", data, strip_byte_count, 1)
    with pytest.raises(FormatRefusal, match="corrupt TIFF"):
        decode_raster(bytes(data))


def test_a_layout_this_walker_cannot_read_defers_to_the_real_decoder():
    """An `unsupported ...` structural verdict is about this module, not the file.

    Spec 03: structural validation "may no longer decide a real file is inadmissible
    because nothing here reconstructs its pixels". `validate_tiff` refuses BigTIFF by
    name — it walks 32-bit offsets and says so — but the installed decoder reads
    BigTIFF perfectly well, and a large archival scan in that layout is exactly the
    "real, uncorrupted file refused by policy" ruling 2 deletes. So the decoder
    answers instead, and the file is admitted.

    BigTIFF is sniffed as TIFF for this to be the route it takes. Left unsniffed it
    would be admitted anyway, through the unknown-magic fallback — but by accident
    rather than by name, and it would turn into `unrecognized-format` the day that
    fallback is ever tightened.
    """
    output = BytesIO()
    Image.new("RGB", (8, 6), (12, 34, 56)).save(output, format="TIFF", big_tiff=True)
    data = output.getvalue()

    assert sniff(data) == "tiff"
    with pytest.raises(FormatRefusal, match="unsupported TIFF: not classic TIFF"):
        validate_tiff(data)
    assert decode_raster(data) == DecodedRaster("tiff", 8, 6, 1)


def _tiff_tag_value_offset(data: bytes, tag: int) -> int:
    """Where one IFD-0 entry's inline value field starts, for damaging it."""
    (offset,) = struct.unpack_from("<I", data, 4)
    (count,) = struct.unpack_from("<H", data, offset)
    for index in range(count):
        entry = offset + 2 + index * 12
        (found,) = struct.unpack_from("<H", data, entry)
        if found == tag:
            return entry + 8
    raise AssertionError(f"the synthetic TIFF carries no tag {tag}")


def test_a_classic_tiff_past_the_retired_5000_page_cap_keeps_its_denominator():
    """The document declares the page count; a project policy number does not.

    The directories are spaced rather than packed, and the spacing is the point.
    This test used to chain them six bytes apart, which is the least the walk will
    accept — and that is byte-for-byte the amplification shape
    `test_a_chain_of_empty_directories_cannot_declare_more_pages_than_bytes_allow`
    now refuses, so the two tests asserted opposite things about identical bytes.
    Ruling 17 retired the 5,000-page *cap*, which is what this test exists to hold:
    a document says how many pages it has. It never said six bytes buy a page. A
    real page of this era costs ~128 bytes at Pillow's absolute smallest, so a
    document declaring 5,001 pages weighs at least this much, and the count still
    passes with no policy number anywhere near it.
    """
    pages = 5_001
    stride = 40
    data = bytearray(b"II*\x00" + struct.pack("<I", 8))
    for index in range(pages):
        next_offset = 8 + (index + 1) * stride if index + 1 < pages else 0
        data.extend(struct.pack("<HI", 0, next_offset))
        data.extend(b"\x00" * (stride - 6))

    assert count_raster_pages(bytes(data)) == pages
    assert pages > 5_000
    assert len(data) / pages > MIN_BYTES_PER_DECLARED_TIFF_PAGE


def test_bigtiff_leaves_its_page_count_to_the_decoder():
    """BigTIFF's 64-bit chain leaves its declared count to the real decoder."""
    output = BytesIO()
    first = Image.new("L", (4, 3), 19)
    second = Image.new("L", (2, 5), 231)
    first.save(output, format="TIFF", save_all=True, append_images=[second], big_tiff=True)
    data = output.getvalue()

    assert sniff(data) == "tiff"
    assert count_raster_pages(data) == 2
