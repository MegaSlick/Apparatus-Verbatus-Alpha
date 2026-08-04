"""Structural validators, proven against hand-built bytes.

Every fixture here is built in memory by `synthetic_sources.py`, never a checked-in
binary: the ingress guard only allows png/jpeg/tiff media types under
`proof/fixtures/`, this module also needs to prove corrupt and PDF/GIF/HEIC bytes,
and nothing here is register material in the first place — GOVERNANCE's
synthetic-fixture rule applies to bytes that stand for a page, and these do not.

The validators refuse in two distinct voices and the difference is load-bearing.
"corrupt ..." means the bytes are not a genuine instance of the format at all;
"unsupported ..." means they are genuine and name a variant this door does not
decode. `admission._refusal_code` turns those into two different closed reasons, so
a test that only checked "it raised" would let the two collapse.
"""

import struct
import tracemalloc
import zlib

import pytest
from image_formats import (
    MAX_DIMENSION,
    MAX_TIFF_IFDS,
    FormatRefusal,
    ImageGeometry,
    _tiff_unsigned_values,
    sniff,
    validate,
    validate_jpeg,
    validate_png,
    validate_tiff,
)
from synthetic_sources import PNG_MAGIC, gif, heic, jpeg, png, png_chunk, png_container, tiff

# --- sniff -----------------------------------------------------------------------


def test_sniff_recognizes_every_format_the_admission_list_can_name():
    assert sniff(png()) == "png"
    assert sniff(jpeg()) == "jpeg"
    assert sniff(tiff()) == "tiff"
    assert sniff(b"%PDF-1.4\n%...") == "pdf"
    assert sniff(gif()) == "gif"
    assert sniff(heic()) == "heic"


def test_sniff_returns_none_for_unrecognized_bytes():
    assert sniff(b"not an image at all, just text\x00\x01\x02") is None


def test_sniff_does_not_call_an_unrelated_iso_container_a_heic():
    """`ftyp` alone is every MP4 and MOV ever made; the brand is what distinguishes
    a HEIC image, and mistaking one for the other would refuse a video by the wrong
    name and admit nothing either way."""
    mp4 = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00" * 4 + b"isom" + b"mp42"
    assert sniff(mp4) is None


def test_heic_brand_sniffing_does_not_allocate_from_the_ftyp_box_size():
    """A refused format still crosses the sniffer; its header cannot size a list."""
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


def test_validate_png_refuses_trailing_bytes_after_iend():
    with pytest.raises(FormatRefusal, match="IEND"):
        validate_png(png() + b"appended payload")


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


def test_validate_jpeg_refuses_bytes_appended_after_eoi():
    """Two concatenated images, or an appended payload. Either way the file is not
    the single page the door would believe it admitted."""
    with pytest.raises(FormatRefusal, match="trailing bytes after EOI"):
        validate_jpeg(jpeg(trailing=b"a second document entirely"))


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
    struct.pack_into("<I", data, 14, 100)  # count 100 forces an out-of-line value
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
    struct.pack_into("<I", data, 14, 0)
    with pytest.raises(FormatRefusal, match="not one SHORT or LONG"):
        validate_tiff(bytes(data))


def test_validate_tiff_refuses_a_second_image_directory_as_a_documented_limit():
    """Multi-page TIFF: the door assigns one ordinal per page, so silently sealing
    only the first would lose every page after it without a refusal to show."""
    first = tiff(4, 3)
    # Point the first IFD's "next IFD" pointer back at itself minus nothing: build a
    # second directory after the first, declaring its own geometry.
    endian = "<"
    second_offset = len(first)
    data = bytearray(first)
    struct.pack_into(endian + "I", data, 8 + 2 + 4 * 12, second_offset)
    entries = struct.pack(endian + "HHI", 256, 3, 1) + struct.pack(endian + "H", 4) + b"\x00\x00"
    entries += struct.pack(endian + "HHI", 257, 3, 1) + struct.pack(endian + "H", 3) + b"\x00\x00"
    data += struct.pack(endian + "H", 2) + entries + struct.pack(endian + "I", 0)
    with pytest.raises(FormatRefusal, match="multi-page TIFF is a documented limit"):
        validate_tiff(bytes(data))


def test_validate_tiff_refuses_a_cyclic_ifd_chain():
    data = bytearray(tiff())
    struct.pack_into("<I", data, 8 + 2 + 4 * 12, 8)  # next IFD points at the first
    with pytest.raises(FormatRefusal, match="cycle"):
        validate_tiff(bytes(data))


def test_the_tiff_ifd_walk_is_bounded_before_it_begins():
    assert MAX_TIFF_IFDS == 64


def test_validate_tiff_refuses_non_tiff_bytes():
    with pytest.raises(FormatRefusal, match="byte-order"):
        validate_tiff(b"definitely not a tiff")


# --- dispatch --------------------------------------------------------------------


def test_validate_dispatches_by_format_name():
    assert validate("png", png()).format == "png"
    assert validate("jpeg", jpeg()).format == "jpeg"
    assert validate("tiff", tiff()).format == "tiff"


def test_validate_refuses_a_format_it_has_no_validator_for():
    with pytest.raises(FormatRefusal, match="no structural validator"):
        validate("gif", b"anything")
