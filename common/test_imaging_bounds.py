"""The decoder's refusals, especially the one that costs memory rather than correctness.

A PNG's IDAT is a zlib stream, and `zlib.decompress` will materialize whatever that
stream expands to. A few hundred bytes can become gigabytes before any length check
downstream gets a chance to run, so the bound has to be applied *during*
decompression rather than after it. The header already declares exactly how large a
valid image is, which is the number to bound against.

The fixture pages are synthetic and this pipeline is offline, so nothing hostile
reaches this code today. It is bounded anyway: the door is the stage whose whole
job will be admitting real files from outside, and the codec it leans on should not
be the soft place.
"""

import zlib

import pytest

from common.imaging import (
    PNG_SIGNATURE,
    crop_png,
    decode_grayscale_png,
    encode_grayscale_png,
)


def sound_page(width: int = 8, height: int = 4) -> bytes:
    return encode_grayscale_png(width, height, [bytearray([200] * width) for _ in range(height)])


def repack(width: int, height: int, raw: bytes) -> bytes:
    """A structurally valid PNG whose IDAT holds exactly `raw`, uncompressed."""
    import struct

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, level=9))
        + chunk(b"IEND", b"")
    )


def test_a_sound_page_still_decodes():
    width, height, rows = decode_grayscale_png(sound_page())
    assert (width, height) == (8, 4)
    assert len(rows) == 4


def test_a_decompression_bomb_is_refused_without_materializing_it():
    """The header claims a tiny image; the IDAT expands to far more. The refusal
    must come from the bound, not from a length check after the fact."""
    declared = (4 + 1) * 2  # a 4x2 image
    bomb = repack(4, 2, b"\x00" * (declared + 50_000_000))

    assert len(bomb) < 100_000, "the point is that a small file claims a huge expansion"
    with pytest.raises(ValueError) as caught:
        decode_grayscale_png(bomb)
    assert "expands past" in str(caught.value)


def test_a_truncated_stream_that_reaches_the_expected_length_is_refused():
    """A truncated stream can return exactly the expected byte count without
    raising, so the length check alone would pass it."""
    width, height = 8, 4
    raw = b"\x00" + bytes([200] * width)
    raw = raw * height
    complete = zlib.compress(raw, level=9)

    import struct

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    truncated = (
        PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", complete[:-1])
        + chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError):
        decode_grayscale_png(truncated)


def test_a_stream_shorter_than_declared_is_refused():
    with pytest.raises(ValueError) as caught:
        decode_grayscale_png(repack(8, 4, b"\x00" * 10))
    assert "wrong length" in str(caught.value)


def test_undecodable_input_is_refused_rather_than_guessed_at():
    for bad in (b"", b"not a png", PNG_SIGNATURE, PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR"):
        with pytest.raises(ValueError):
            decode_grayscale_png(bad)


def test_crop_refuses_what_the_decoder_refuses():
    with pytest.raises(ValueError):
        crop_png(b"not a png", {"x": 0, "y": 0, "w": 1, "h": 1})
