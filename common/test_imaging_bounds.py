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
    grayscale_rows,
    render_triage_derivative,
)

ROOT = Path(__file__).resolve().parents[1]


def sound_page(width: int = 8, height: int = 4) -> bytes:
    return encode_grayscale_png(width, height, [bytearray([200] * width) for _ in range(height)])


def repack(width: int, height: int, raw: bytes, color_type: int = 0) -> bytes:
    """A structurally valid PNG whose IDAT holds exactly `raw`, uncompressed.

    `color_type` defaults to 0 (grayscale), what this module's own codec
    writes. Pass 2 for RGB to reach the Pillow fallback paths, which refuse on
    the declared IHDR dimensions before decoding, so an oversized page can be
    declared here without ever being materialised.
    """
    import struct

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
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


def test_a_declared_size_past_the_pixel_bound_is_refused_before_any_decompression():
    """`MAX_PIXELS` bounds the IHDR's own declared width/height, not only what
    the IDAT expands to relative to them. Before this check, a declared size
    alone past the ceiling decoded cleanly -- the module's own comment claimed
    the same bound the Pillow-fallback paths (`dimensions`, `grayscale_rows`)
    already enforce, but this native path never checked it, so a compact,
    highly compressible IDAT matching a huge declared size paid full
    decompression cost with no refusal at all."""
    width, height = 20_000, 6_000  # 120,000,000 declared pixels, over MAX_PIXELS
    assert width * height > MAX_PIXELS
    over_the_limit = repack(width, height, b"never reached")
    with pytest.raises(ValueError, match="pixel bound"):
        decode_grayscale_png(over_the_limit)


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


def test_crop_refuses_a_declared_size_past_the_bound_on_its_pillow_fallback_path():
    """`crop_png` falls back to `_crop_decoded_page` for anything this module's own
    native codec cannot decode (RGB, CMYK, 16-bit, ...). That fallback called
    `Image.open` + `image.load()` with no `MAX_PIXELS` check of its own, so a
    declared size between `MAX_PIXELS` and Pillow's own 2x hard-raise ceiling
    decoded and materialized cleanly with nothing but a non-fatal
    `DecompressionBombWarning` -- exactly the gap `dimensions` and
    `grayscale_rows` were already closed for on the same bytes.

    RGB rather than this module's own grayscale encoding, so `decode_grayscale_png`
    refuses for a reason unrelated to size and this exercises the Pillow fallback,
    not the native path `test_a_declared_size_past_the_pixel_bound_is_refused_
    before_any_decompression` already covers.

    The oversized page is *declared*, never materialised: a real
    100,500,000-pixel RGB image is over 300 MB of source pixels, and a CI worker
    can die building the fixture before the assertion it exists for ever runs.
    `_crop_decoded_page` refuses on `image.width * image.height` before
    `image.load()`, so a compact IDAT under a large IHDR reaches the same
    refusal by the same route. Found by CodeRabbit."""
    # 100,500,000 pixels: over MAX_PIXELS, under Pillow's own 2x raise ceiling
    width, height = 10_050, 10_000
    assert MAX_PIXELS < width * height < 2 * MAX_PIXELS
    source = repack(width, height, b"", color_type=2)

    with pytest.raises(ValueError, match="pixel bound"):
        crop_png(source, {"x": 0, "y": 0, "w": 4, "h": 4})


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


# --- grayscale_rows: the one place outside decode_grayscale_png that reads ------
# --- pixel VALUES rather than only dimensions or a cropped rectangle ------------


def test_grayscale_rows_decodes_the_fast_native_path():
    rows_in = [bytearray([10, 20, 30, 40]), bytearray([200, 210, 220, 230])]
    width, height, rows = grayscale_rows(encode_grayscale_png(4, 2, rows_in))
    assert (width, height) == (4, 2)
    assert [list(row) for row in rows] == [list(row) for row in rows_in]


def test_grayscale_rows_falls_back_to_pillow_for_a_page_this_codec_cannot_decode():
    """An RGB page is not this module's own 8-bit-grayscale format, so it takes
    the Pillow fallback -- exactly the branch `dimensions` and `crop_png` also
    fall back through, proven here against actual pixel VALUES rather than only
    a size."""
    source = BytesIO()
    page = Image.new("RGB", (2, 1))
    page.putdata([(0, 0, 0), (255, 255, 255)])
    page.save(source, format="PNG")

    width, height, rows = grayscale_rows(source.getvalue())
    assert (width, height) == (2, 1)
    assert list(rows[0]) == [0, 255]


def test_grayscale_rows_refuses_a_decompression_bomb_without_materializing_it():
    huge = BytesIO()
    Image.new("RGB", (2, 2)).save(huge, format="PNG")
    data = huge.getvalue()

    original = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 1
        with pytest.raises(ValueError):
            grayscale_rows(data)
    finally:
        Image.MAX_IMAGE_PIXELS = original


def test_grayscale_rows_refuses_a_declared_size_bomb_regardless_of_which_path_observes_it():
    """`residual_ink.py` is a new caller of `grayscale_rows` in this diff, so the
    same declared-size bomb `decode_grayscale_png` refuses must not reach
    `residual_ink`'s own pixel loops either -- from either of `grayscale_rows`'s
    two internal paths. Not "on its native path" specifically: `grayscale_rows`
    catches any `ValueError` from the native decode, including this one, and
    retries through the Pillow fallback -- so this fixture's bound refusal is
    actually observed there, not on the native path that raised it first."""
    width, height = 20_000, 6_000
    assert width * height > MAX_PIXELS
    over_the_limit = repack(width, height, b"never reached")
    with pytest.raises(ValueError, match="pixel bound"):
        grayscale_rows(over_the_limit)


def test_grayscale_rows_refuses_undecodable_input_rather_than_guessing():
    for bad in (b"", b"not an image", PNG_SIGNATURE):
        with pytest.raises(ValueError):
            grayscale_rows(bad)


def triage_part(width: int = 4, height: int = 4) -> dict:
    return {
        "region": {"space": "frame", "x": 0, "y": 0, "w": width, "h": height},
        "crop_box": {"space": "part", "x": 0, "y": 0, "w": width, "h": height},
        "rotation": {
            "rotation_millidegrees": 0,
            "direction": "clockwise",
            "origin": "crop-centre",
            "canvas": "expand",
        },
        "colour_mode": "grayscale",
    }


def test_the_triage_render_refuses_a_declared_size_past_the_pixel_bound():
    """The triage render was the module's one Pillow decode that opened, sought and
    loaded a frame without `_refuse_past_pixel_bound`, so a master between
    `MAX_PIXELS` and Pillow's own 2x hard-raise ceiling was materialised under a
    warning while every sibling path refused the same bytes. Declared rather than
    materialised, as in the `crop_png` fallback case above: the check runs on the
    IHDR's dimensions before `load`, so a compact IDAT reaches it by the same route
    and the refusal must name the bound rather than the truncation `load` would
    otherwise report. Found by CodeRabbit."""
    width, height = 10_050, 10_000
    assert MAX_PIXELS < width * height < 2 * MAX_PIXELS

    with pytest.raises(ValueError, match="pixel bound"):
        render_triage_derivative(
            repack(width, height, b"", color_type=2), page_index=0, part=triage_part()
        )


def test_the_triage_render_refuses_a_page_index_past_the_last_frame_as_a_decode_failure():
    """`Image.seek` past the end raises `EOFError`, which descends from `Exception`
    rather than `OSError` and so left this function by a route the module's stated
    contract does not have. The Exemplar boundary converts only `ValueError` into a
    refusal, so the escape arrived there as an unhandled error."""
    single_frame = BytesIO()
    Image.new("L", (8, 8), 200).save(single_frame, format="PNG")

    with pytest.raises(ValueError, match="not a decodable image"):
        render_triage_derivative(single_frame.getvalue(), page_index=7, part=triage_part())


@pytest.mark.parametrize(
    ("forge", "names"),
    [
        pytest.param(
            lambda p: p["region"].update(w=40, h=40), "split region", id="region-past-the-master"
        ),
        pytest.param(
            lambda p: p["region"].update(x=2, y=2), "split region", id="region-offset-past-the-edge"
        ),
        pytest.param(
            lambda p: p["crop_box"].update(w=9, h=9), "crop box", id="crop-past-its-own-region"
        ),
    ],
)
def test_the_triage_render_refuses_geometry_that_falls_outside_what_it_decoded(forge, names):
    """`Image.crop` pads rather than refuses, filling anything past the edge with
    black. A part declaring a frame larger than the master it was cut from therefore
    rendered a page that was almost entirely invented pixels, and sealed it: the
    probe that found this produced a 40x40 page from a 4x4 master, 99% of it black.
    The Exemplar boundary does reconcile the declared frame against the decoded
    master, but only after this function has already rendered the padded page, and
    the door's producing path (`render_raster_page`) reconciles nothing at all.
    Found by CodeRabbit."""
    master = BytesIO()
    Image.new("L", (4, 4), 255).save(master, format="PNG")
    part = triage_part()
    forge(part)

    with pytest.raises(ValueError, match=f"{names}.*falls outside.*invented pixels"):
        render_triage_derivative(master.getvalue(), page_index=0, part=part)


def test_the_triage_render_still_applies_geometry_that_is_contained():
    """The refusal above must not have closed the ordinary path with it.

    The master's pixels are all different, and the decoded output is read back and
    compared. Against a uniform white master this test asserted only the output
    size, and a regression that ignored `crop_box["x"]` and `crop_box["y"]`
    entirely still returned a correctly sized 2x2 page of the right colour and
    passed. What the function is for is *which* pixels it cuts."""
    master = BytesIO()
    source = Image.frombytes("L", (4, 4), bytes(16 * y + x for y in range(4) for x in range(4)))
    source.save(master, format="PNG")
    part = triage_part()
    part["crop_box"].update(x=1, y=1, w=2, h=2)

    rendered, geometry = render_triage_derivative(master.getvalue(), page_index=0, part=part)

    assert (geometry["width"], geometry["height"]) == (2, 2)
    assert (geometry["source_width"], geometry["source_height"]) == (4, 4)
    with Image.open(BytesIO(rendered)) as decoded:
        assert decoded.tobytes() == bytes([17, 18, 33, 34])


def test_the_triage_render_refuses_a_rotation_that_would_expand_past_the_pixel_bound(monkeypatch):
    """`expand=True` grows the canvas to the rotated bounding box, and that box is
    not bounded by the crop's area: a 200000x2 strip is 400000 pixels, inside the
    bound by three orders of magnitude, and at 45 degrees its bounding box is about
    4e10 pixels. Nothing between the crop and `Image.rotate` looked at that number,
    so Pillow was asked for the allocation and the run ended there instead of at a
    recorded refusal — the Door checks the geometry of the page it is handed, which
    is a page that no longer exists. The strip is the cheap shape to provoke it with:
    the master itself decodes to 400KB. Found by CodeRabbit."""
    master = BytesIO()
    Image.new("L", (200_000, 2), 255).save(master, format="PNG")
    part = triage_part(width=200_000, height=2)
    part["rotation"]["rotation_millidegrees"] = 45_000

    def refuse_to_allocate(*args, **kwargs):
        raise AssertionError(
            "the guard regressed: rotation was reached and would allocate ~4e10 pixels"
        )

    # Without this, a regressed guard does not fail the test — it exhausts the
    # worker's memory allocating the canvas and the run dies without a report.
    monkeypatch.setattr(Image.Image, "rotate", refuse_to_allocate)

    with pytest.raises(ValueError, match="pixel bound"):
        render_triage_derivative(master.getvalue(), page_index=0, part=part)


def test_the_triage_render_still_rotates_what_stays_inside_the_bound():
    """The expansion check must refuse the bounding box it would actually allocate,
    not every rotation. A 4x4 crop turned 45 degrees expands to 6x6, and the record
    has to report the expanded page rather than the crop it came from."""
    master = BytesIO()
    Image.new("L", (4, 4), 255).save(master, format="PNG")
    part = triage_part()
    part["rotation"]["rotation_millidegrees"] = 45_000

    _rendered, geometry = render_triage_derivative(master.getvalue(), page_index=0, part=part)

    assert (geometry["width"], geometry["height"]) == (6, 6)


def test_this_modules_pixel_bound_matches_the_door_that_admits_the_pages():
    """Restated rather than imported, because `common/` may not import `pipeline/`.
    A drift between the two would let the door admit a page this module refuses."""
    door_limits = (ROOT / "pipeline" / "1_exemplar" / "image_formats.py").read_text(
        encoding="utf-8"
    )
    declared = re.search(r"^MAX_PIXELS: Final = ([0-9_]+)$", door_limits, re.MULTILINE)
    assert declared, "the door no longer declares MAX_PIXELS where this test can read it"
    assert int(declared.group(1).replace("_", "")) == MAX_PIXELS
