"""The multi-page TIFF renderer, proven against hand-built directory chains.

Every fixture is built in memory by `synthetic_sources.multi_page_tiff` — a
genuine, chained, multi-directory TIFF with absolute offsets, never a checked-in
binary and never register material.
"""

import struct
import zlib

import pytest
import tiff_render
from admission import RefusalReason
from image_formats import PNG_SIGNATURE, ImageGeometry, validate_png
from synthetic_sources import multi_page_tiff, tiff


def _decode_png_samples(png_bytes: bytes) -> tuple[int, int, bytes]:
    """A tiny local decoder, independent of `common/imaging.py`'s grayscale-only
    one, so this test does not certify `encode_png`'s RGB output by decoding it
    with the same assumptions the encoder made."""
    assert png_bytes.startswith(PNG_SIGNATURE)
    offset = 8
    idat = b""
    width = height = color_type = None
    while offset < len(png_bytes):
        (length,) = struct.unpack_from(">I", png_bytes, offset)
        tag = png_bytes[offset + 4 : offset + 8]
        chunk = png_bytes[offset + 8 : offset + 8 + length]
        if tag == b"IHDR":
            width, height, _bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
        offset += 12 + length
    channels = 3 if color_type == 2 else 1
    raw = zlib.decompress(idat)
    row_bytes = width * channels
    samples = bytearray()
    for row in range(height):
        start = row * (1 + row_bytes)
        assert raw[start] == 0
        samples += raw[start + 1 : start + 1 + row_bytes]
    return width, height, bytes(samples)


def test_count_pages_matches_the_directory_chain():
    assert tiff_render.count_pages(tiff(6, 5)) == 1
    assert tiff_render.count_pages(multi_page_tiff(((6, 5), (4, 3), (2, 2)))) == 3


def test_render_page_produces_a_lossless_png_per_directory():
    data = multi_page_tiff(((6, 5), (4, 3)))
    document = tiff_render.open_document(data)
    try:
        for index, (width, height) in enumerate(((6, 5), (4, 3))):
            png_bytes, fmt = tiff_render.render_page(document, index)
            assert fmt == "png"
            assert validate_png(png_bytes) == ImageGeometry("png", width, height)
    finally:
        tiff_render.close_document(document)


def test_render_page_out_of_range_is_a_named_refusal():
    document = tiff_render.open_document(multi_page_tiff(((6, 5), (4, 3))))
    try:
        with pytest.raises(tiff_render.TiffRenderRefusal, match="out of range"):
            tiff_render.render_page(document, 2)
    finally:
        tiff_render.close_document(document)


def test_a_directory_this_module_cannot_parse_at_all_is_refused_naming_why():
    with pytest.raises(tiff_render.TiffRenderRefusal):
        tiff_render.open_document(b"not a tiff at all")


def test_close_document_is_safe_to_call_on_a_document_never_rendered():
    document = tiff_render.open_document(tiff(6, 5))
    tiff_render.close_document(document)


def test_rgb_directory_extracts_all_three_channels():
    """`SamplesPerPixel=3`, `PhotometricInterpretation=2` (RGB) is a documented
    scope this module supports, distinct from the grayscale default every other
    fixture in this file uses. `multi_page_tiff` only builds grayscale pages, so
    this builds a single-directory RGB TIFF by hand instead."""
    width, height = 4, 3
    pixel = bytes([10, 20, 30])
    rows = pixel * width
    single_rgb = _rgb_tiff(width, height, rows * height)
    document = tiff_render.open_document(single_rgb)
    try:
        png_bytes, fmt = tiff_render.render_page(document, 0)
    finally:
        tiff_render.close_document(document)
    assert fmt == "png"
    decoded_width, decoded_height, samples = _decode_png_samples(png_bytes)
    assert (decoded_width, decoded_height) == (width, height)
    assert samples == rows * height


def _rgb_tiff(width: int, height: int, samples: bytes) -> bytes:
    """A minimal single-directory RGB (PhotometricInterpretation 2,
    SamplesPerPixel 3) TIFF, built the same way `synthetic_sources.tiff` builds a
    grayscale one but with three bits-per-sample entries rather than one."""
    endian = "<"

    def short(tag: int, value: int) -> bytes:
        return (
            struct.pack(endian + "HHI", tag, 3, 1) + struct.pack(endian + "H", value) + b"\x00\x00"
        )

    def long_tag(tag: int, value: int) -> bytes:
        return struct.pack(endian + "HHI", tag, 4, 1) + struct.pack(endian + "I", value)

    # BitsPerSample (258) needs three SHORT values, which do not fit inline, so
    # they are stored out-of-line like any array value over 4 bytes.
    bits_values = struct.pack(endian + "HHH", 8, 8, 8)
    entry_count = 9
    ifd_size = 2 + entry_count * 12 + 4
    bits_offset = 8 + ifd_size
    strip_offset = bits_offset + len(bits_values)

    entries = (
        struct.pack(endian + "HHI", 256, 3, 1) + struct.pack(endian + "H", width) + b"\x00\x00"
    )
    entries += (
        struct.pack(endian + "HHI", 257, 3, 1) + struct.pack(endian + "H", height) + b"\x00\x00"
    )
    entries += struct.pack(endian + "HHI", 258, 3, 3) + struct.pack(endian + "I", bits_offset)
    entries += short(259, 1)
    entries += short(262, 2)
    entries += long_tag(273, strip_offset)
    entries += short(277, 3)
    entries += long_tag(278, height)
    entries += long_tag(279, len(samples))

    body = struct.pack(endian + "H", entry_count) + entries + struct.pack(endian + "I", 0)
    header = b"II" + struct.pack(endian + "H", 42) + struct.pack(endian + "I", 8)
    return header + body + bits_values + samples


def test_whiteiszero_photometric_is_inverted_on_extraction():
    """PhotometricInterpretation 0 (WhiteIsZero) means a raw sample of 0 renders as
    white; sealing the raw byte unchanged would seal a page whose ink and paper are
    swapped from what the source declares — an unrecorded transform ARCHITECTURE's
    third invariant cannot survive."""
    width, height = 3, 2
    raw = bytes([0, 128, 255]) * height
    data = _grayscale_tiff(width, height, raw, photometric=0)
    document = tiff_render.open_document(data)
    try:
        png_bytes, _fmt = tiff_render.render_page(document, 0)
    finally:
        tiff_render.close_document(document)
    _decoded_width, _decoded_height, samples = _decode_png_samples(png_bytes)
    assert samples == bytes(255 - value for value in raw)


def _grayscale_tiff(width: int, height: int, samples: bytes, *, photometric: int) -> bytes:
    endian = "<"

    def short(tag: int, value: int) -> bytes:
        return (
            struct.pack(endian + "HHI", tag, 3, 1) + struct.pack(endian + "H", value) + b"\x00\x00"
        )

    def long_tag(tag: int, value: int) -> bytes:
        return struct.pack(endian + "HHI", tag, 4, 1) + struct.pack(endian + "I", value)

    entry_count = 9
    ifd_size = 2 + entry_count * 12 + 4
    strip_offset = 8 + ifd_size

    entries = (
        struct.pack(endian + "HHI", 256, 3, 1) + struct.pack(endian + "H", width) + b"\x00\x00"
    )
    entries += (
        struct.pack(endian + "HHI", 257, 3, 1) + struct.pack(endian + "H", height) + b"\x00\x00"
    )
    entries += short(258, 8)
    entries += short(259, 1)
    entries += short(262, photometric)
    entries += long_tag(273, strip_offset)
    entries += short(277, 1)
    entries += long_tag(278, height)
    entries += long_tag(279, len(samples))
    body = struct.pack(endian + "H", entry_count) + entries + struct.pack(endian + "I", 0)
    header = b"II" + struct.pack(endian + "H", 42) + struct.pack(endian + "I", 8)
    return header + body + samples


def test_a_compressed_directory_is_a_named_gap_not_a_corruption_claim():
    """Ruling 2026-08-04, item 2: a format or variant nothing here decodes yet is
    counted as a pipeline defect, never framed as damage to the file."""
    width, height = 4, 3
    data = _grayscale_tiff(width, height, b"\x00" * (width * height), photometric=1)
    patched = bytearray(data)
    # Compression (259) is the fourth entry in `_grayscale_tiff`'s fixed tag order
    # (256, 257, 258, 259, ...); its SHORT value sits 8 bytes into its own
    # 12-byte entry, right after tag/type/count.
    compression_entry = 8 + 2 + 12 * 3  # header + count + three entries (256, 257, 258)
    struct.pack_into("<H", patched, compression_entry + 8, 5)  # LZW
    document = tiff_render.open_document(bytes(patched))
    try:
        with pytest.raises(tiff_render.TiffRenderRefusal) as excinfo:
            tiff_render.render_page(document, 0)
    finally:
        tiff_render.close_document(document)
    assert excinfo.value.reason is RefusalReason.UNSUPPORTED_VARIANT
    assert "gap in this pipeline" in str(excinfo.value)


def test_a_tiled_directory_is_a_named_gap():
    with pytest.raises(tiff_render.TiffRenderRefusal, match="stored as tiles"):
        # Tiled storage is not producible by this file's own builders without a
        # good deal of extra machinery; the validator-level refusal is proven in
        # test_image_formats.py's TIFF section, and this exercises the wrapping
        # renderer's own branch using the same reconciled facts a tiled directory
        # would have to satisfy. A minimal tiled IFD is built inline instead.
        tiff_render.render_page(tiff_render.open_document(_minimal_tiled_tiff()), 0)


def _minimal_tiled_tiff() -> bytes:
    endian = "<"
    width = height = 16
    tile_width = tile_length = 16
    tile_bytes = tile_width * tile_length

    def short(tag: int, value: int) -> bytes:
        return (
            struct.pack(endian + "HHI", tag, 3, 1) + struct.pack(endian + "H", value) + b"\x00\x00"
        )

    def long_tag(tag: int, value: int) -> bytes:
        return struct.pack(endian + "HHI", tag, 4, 1) + struct.pack(endian + "I", value)

    entry_count = 10
    ifd_size = 2 + entry_count * 12 + 4
    tile_offset = 8 + ifd_size

    entries = (
        struct.pack(endian + "HHI", 256, 3, 1) + struct.pack(endian + "H", width) + b"\x00\x00"
    )
    entries += (
        struct.pack(endian + "HHI", 257, 3, 1) + struct.pack(endian + "H", height) + b"\x00\x00"
    )
    entries += short(258, 8)
    entries += short(259, 1)
    entries += short(262, 1)
    entries += short(277, 1)
    entries += short(322, tile_width)
    entries += short(323, tile_length)
    entries += long_tag(324, tile_offset)
    entries += long_tag(325, tile_bytes)
    body = struct.pack(endian + "H", entry_count) + entries + struct.pack(endian + "I", 0)
    header = b"II" + struct.pack(endian + "H", 42) + struct.pack(endian + "I", 8)
    return header + body + bytes(tile_bytes)
