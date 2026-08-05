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

import re
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.imaging import (
    MAX_PIXELS,
    PNG_SIGNATURE,
    _to_display_mode,
    crop_png,
    decode_grayscale_png,
    dimensions,
    encode_grayscale_png,
)

ROOT = Path(__file__).resolve().parents[1]


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


def test_crop_converts_an_admitted_cmyk_jpeg_to_a_png_compatible_display_mode():
    source = BytesIO()
    Image.new("CMYK", (3, 2), (0, 255, 255, 0)).save(source, format="JPEG")

    cropped = crop_png(source.getvalue(), {"x": 1, "y": 0, "w": 2, "h": 2})
    with Image.open(BytesIO(cropped)) as image:
        image.load()
        assert image.mode == "RGB"
        assert image.size == (2, 2)


def test_crop_decodes_a_sealed_single_frame_heic_and_emits_lossless_png():
    source = BytesIO()
    Image.new("RGB", (4, 3), (17, 34, 51)).save(source, format="HEIF", lossless=True)

    cropped = crop_png(source.getvalue(), {"x": 1, "y": 1, "w": 2, "h": 2})

    with Image.open(BytesIO(cropped)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (2, 2)


# --- what the pixels survive, which a size check cannot see ----------------------


def test_a_sixteen_bit_page_is_scaled_to_eight_bits_rather_than_clipped_to_white():
    """`convert("RGB")` maps a 16-bit sample straight through, so everything above
    255 lands on 255 and a real scan came out as near-white. The door seals these
    modes losslessly as TIFF, so they genuinely arrive here."""
    source = BytesIO()
    page = Image.new("I;16", (4, 1))
    page.putdata([0, 16384, 32768, 65535])
    page.save(source, format="TIFF")

    cropped = crop_png(source.getvalue(), {"x": 0, "y": 0, "w": 4, "h": 1})

    with Image.open(BytesIO(cropped)) as image:
        image.load()
        samples = list(image.convert("L").get_flattened_data())
    # Scaled by the mode's declared range: the midpoint stays a midpoint instead of
    # joining the top value at 255, which is exactly what clipping destroyed.
    assert samples[0] == 0
    assert samples[3] == 255
    assert 100 < samples[2] < 160, f"the midtone was flattened: {samples}"
    assert samples[1] < samples[2] < samples[3], f"ordering was not preserved: {samples}"


def test_a_premultiplied_alpha_crop_keeps_its_alpha_channel():
    """Pillow spells that band lower case — mode `La` reports ('L', 'a') — so an
    `"A" in bands` test dropped the alpha of the very page it meant to preserve.

    Driven against the helper rather than through a file, because no image format
    here writes `La` back out: Pillow's TIFF encoder refuses the mode outright. The
    branch is still the one a decoder can hand us, so it is the branch under test.
    """
    page = Image.new("La", (2, 1))
    page.putdata([(10, 0), (200, 255)])

    converted = _to_display_mode(page)

    # `LA`, not `RGBA`: Pillow converts `La` to exactly one other mode, and `LA` is
    # PNG-representable, so the crop keeps both channels without a second hop.
    assert converted.mode == "LA", "the alpha channel was flattened away"
    assert [pixel[1] for pixel in converted.get_flattened_data()] == [0, 255]


@pytest.mark.parametrize("mode", ["I", "F"])
def test_an_undefined_range_page_refuses_by_name_rather_than_guessing_a_mapping(mode):
    """`I` is unbounded signed integer and `F` is float. Neither declares a range, so
    any mapping to 8 bits decides what black and white mean. Refused out loud rather
    than flattened silently."""
    source = BytesIO()
    Image.new(mode, (2, 1)).save(source, format="TIFF")

    with pytest.raises(ValueError, match="no defined sample range"):
        crop_png(source.getvalue(), {"x": 0, "y": 0, "w": 2, "h": 1})


def test_a_decompression_bomb_is_refused_as_a_value_error_not_a_pillow_exception():
    """Pillow's bomb error descends from `Exception`, not `ValueError`, so it escaped
    both decoding paths and past this module's stated contract."""
    # RGB, so the tiny grayscale codec refuses it and both paths fall through to
    # Pillow — which is where the bomb error is raised and where it escaped.
    huge = BytesIO()
    Image.new("RGB", (2, 2)).save(huge, format="PNG")
    data = huge.getvalue()

    original = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 1
        with pytest.raises(ValueError):
            dimensions(data)
        with pytest.raises(ValueError):
            crop_png(data, {"x": 0, "y": 0, "w": 1, "h": 1})
    finally:
        Image.MAX_IMAGE_PIXELS = original


def test_this_modules_pixel_bound_matches_the_door_that_admits_the_pages():
    """Restated rather than imported, because `common/` may not import `pipeline/`.
    A drift between the two would let the door admit a page this module refuses."""
    door_limits = (ROOT / "pipeline" / "1_exemplar" / "image_formats.py").read_text(
        encoding="utf-8"
    )
    declared = re.search(r"^MAX_PIXELS: Final = ([0-9_]+)$", door_limits, re.MULTILINE)
    assert declared, "the door no longer declares MAX_PIXELS where this test can read it"
    assert int(declared.group(1).replace("_", "")) == MAX_PIXELS
