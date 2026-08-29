"""What a crop's bytes are allowed to depend on.

A crop is evidence written during a run. It is stored under its own digest, so
its bytes name its blob path, the `image_sha256` in its region record, and every
artifact digest above that — and `common/exemplar_boundary.py` re-derives it and
compares. Bytes that depend on which zlib a wheel bundled therefore turn a
Python upgrade, a pod image, or a CI matrix into a renamed run, and a run tree
that cannot be republished on resume.

These tests hold the two halves of that apart. The encoder writes bytes fixed by
the PNG and DEFLATE specifications and nothing else, on both of `crop_png`'s
paths; and the comparison used downstream is on the image, so an old tree
written by a different encoder is still recognised as showing the same crop
rather than reported as forged.
"""

import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image

from common.imaging import (
    PNG_SIGNATURE,
    _encode_crop_deterministic,
    _to_display_mode,
    carries_only_image_chunks,
    crop_png,
    encode_grayscale_png,
    encode_grayscale_png_deterministic,
    image_shown,
    render_triage_derivative,
    resize_png_lanczos,
)

BOUNDS = {"x": 1, "y": 1, "w": 3, "h": 2}


def chunks(png_bytes: bytes) -> list[tuple[bytes, bytes]]:
    """Every (tag, data) pair in a PNG, in file order."""
    found = []
    offset = 8
    while offset < len(png_bytes):
        length, tag = struct.unpack(">I4s", png_bytes[offset : offset + 8])
        found.append((tag, png_bytes[offset + 8 : offset + 8 + length]))
        offset += 12 + length
    return found


def idat_of(png_bytes: bytes) -> bytes:
    return b"".join(data for tag, data in chunks(png_bytes) if tag == b"IDAT")


def stored_block_payload(stream: bytes) -> bytes:
    """The payload of a zlib stream made only of stored DEFLATE blocks.

    Raises if any block is compressed. Read from the stream's own framing rather
    than by asking zlib, because "no compressor chose anything here" is the
    property under test and `zlib.decompress` is exactly the thing that would
    hide a compressed block by handling it.
    """
    assert stream[:2] == b"\x78\x01", "not a zlib stream at the fixed low-compression header"
    payload = bytearray()
    offset, final = 2, 0
    while not final:
        flag = stream[offset]
        final = flag & 1
        block_type = (flag >> 1) & 0b11
        assert block_type == 0, f"a DEFLATE block of type {block_type} let a compressor choose"
        length, complement = struct.unpack("<HH", stream[offset + 1 : offset + 5])
        assert length ^ 0xFFFF == complement, "a stored block's length is not its own complement"
        payload += stream[offset + 5 : offset + 5 + length]
        offset += 5 + length
    assert offset + 4 == len(stream), "trailing bytes after the last DEFLATE block"
    assert struct.unpack(">I", stream[offset:])[0] == zlib.adler32(bytes(payload))
    return bytes(payload)


def grayscale_page(width: int = 6, height: int = 4) -> bytes:
    """A page this module's own codec decodes, so `crop_png` takes its fast path."""
    return encode_grayscale_png(
        width, height, [bytearray(range(row, row + width)) for row in range(height)]
    )


def colour_page(size: tuple[int, int] = (6, 4), **save: object) -> bytes:
    """A page the native codec refuses, so `crop_png` takes its Pillow fallback."""
    image = Image.new("RGB", size)
    image.putdata([tuple(v % 256 for v in (i, i * 3, i * 7)) for i in range(size[0] * size[1])])
    buffer = BytesIO()
    image.save(buffer, format="PNG", **save)
    return buffer.getvalue()


# --- the bytes a run writes ------------------------------------------------------


def test_the_deterministic_encoder_still_writes_the_bytes_it_has_always_written():
    """A byte pin, because this encoder was generalised to serve colour crops too.

    Its output is already load-bearing for the Perlector's page-context render and
    both acceptance run-tree digests. A refactor that moved one byte would move
    every one of those together, and be read as the pipeline changing rather than
    as this function changing.
    """
    written = encode_grayscale_png_deterministic(
        2, 2, [bytearray(b"\x01\x02"), bytearray(b"\x03\x04")]
    )

    assert written.hex() == (
        "89504e470d0a1a0a0000000d494844520000000200000002080000000057dd52f8000000114944"
        "41547801010600f9ff000102000304001d000b7a3837250000000049454e44ae426082"
    )


@pytest.mark.parametrize(
    "page",
    [
        pytest.param(grayscale_page(), id="native-codec-path"),
        pytest.param(colour_page(), id="pillow-fallback-path"),
    ],
)
def test_a_crop_is_framed_so_that_no_compressor_chooses_any_of_its_bytes(page):
    """Both paths, because every real colour or TIFF-derived page takes the second.

    `zlib.compress(level=9)` and Pillow's own writer are both fine PNG encoders
    and both let a library decide the stream: a different zlib build, or Pillow's
    bundled one, legitimately emits different valid bytes for identical pixels.
    Filter 0 on every scanline and stored DEFLATE blocks leave nothing to decide.
    """
    crop = crop_png(page, BOUNDS)

    raw = stored_block_payload(idat_of(crop))
    stride = len(raw) // BOUNDS["h"]
    assert stride * BOUNDS["h"] == len(raw)
    assert all(raw[row * stride] == 0 for row in range(BOUNDS["h"])), "a scanline is not filter 0"


def test_a_grayscale_crop_survives_a_zlib_that_compresses_differently():
    """The audit's own demonstration: a shim that makes `zlib.compress` emit a
    valid stream at a different level, which is precisely what a different zlib
    build legitimately does. It used to rename every crop in the run."""
    page = grayscale_page()
    before = crop_png(page, BOUNDS)

    genuine = zlib.compress
    try:
        zlib.compress = lambda data, level=-1, **kwargs: genuine(data, 6)
        after = crop_png(page, BOUNDS)
    finally:
        zlib.compress = genuine

    assert after == before


def test_a_crop_carries_nothing_but_the_image():
    for page in (grayscale_page(), colour_page()):
        assert carries_only_image_chunks(crop_png(page, BOUNDS))


# --- what the crop still shows ---------------------------------------------------


@pytest.mark.parametrize("mode", ["L", "LA", "RGB", "RGBA"])
def test_a_deterministically_written_crop_shows_what_pillows_writer_showed(mode):
    """Changing the encoder may not change the picture. Each mode is compared
    against the crop Pillow's own writer produces from the same pixels."""
    page = colour_page()
    with Image.open(BytesIO(page)) as image:
        image.load()
        source = image.convert(mode)
    reference = BytesIO()
    source.save(reference, format="PNG", optimize=False, compress_level=9)

    written = _encode_crop_deterministic(source)

    assert written != reference.getvalue(), "the reference is not a differently framed encoding"
    assert image_shown(written) == image_shown(reference.getvalue())
    with Image.open(BytesIO(written)) as reread:
        reread.load()
        assert reread.mode == mode, "the crop changed mode, not only framing"


def test_a_bilevel_crop_stays_one_bit_rather_than_growing_to_eight():
    """Mode `1` packs `tobytes()` in PNG's own bit layout, padded to whole bytes,
    so a bitonal scan needs no expansion to be written deterministically."""
    page = Image.new("1", (11, 3))
    page.putpixel((0, 0), 1)
    page.putpixel((10, 2), 1)
    buffer = BytesIO()
    page.save(buffer, format="PNG")

    crop = crop_png(buffer.getvalue(), {"x": 0, "y": 0, "w": 11, "h": 3})

    _, header = chunks(crop)[0]
    assert struct.unpack(">IIBBBBB", header)[2] == 1, "the bit depth grew"
    assert image_shown(crop) == image_shown(buffer.getvalue())


def test_an_indexed_crop_is_expanded_losslessly_rather_than_re_serialised():
    """A palette written here would be a second place deciding what a Pillow
    palette means — the int-versus-bytes transparency form above all. The
    expansion keeps every rendered pixel, alpha included."""
    page = Image.new("P", (4, 2))
    page.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * (768 - 9))
    page.putpixel((0, 0), 1)
    page.putpixel((1, 0), 2)
    page.info["transparency"] = 1
    buffer = BytesIO()
    page.save(buffer, format="PNG")

    crop = crop_png(buffer.getvalue(), {"x": 0, "y": 0, "w": 4, "h": 2})

    with Image.open(BytesIO(crop)) as reread:
        reread.load()
        assert reread.mode == "RGBA"
    assert image_shown(crop) == image_shown(buffer.getvalue())


def test_an_indexed_crop_whose_alpha_lives_in_the_palette_keeps_it():
    """Alpha hides in two places on a `P` image and only one is the usual one.
    A decoded PNG or GIF puts tRNS in `info["transparency"]`; quantising an RGBA
    image instead leaves `info` empty and the alpha in the palette itself, which
    an `info`-only test converts to RGB and drops without a word."""
    source = Image.new("RGBA", (4, 2), (10, 20, 30, 0))
    quantised = source.convert("P", palette=Image.Palette.ADAPTIVE)
    assert quantised.palette.mode == "RGBA" and "transparency" not in quantised.info

    written = _encode_crop_deterministic(quantised)

    with Image.open(BytesIO(written)) as reread:
        reread.load()
        assert reread.mode == "RGBA"
    assert image_shown(written) == image_shown(_encode_crop_deterministic(source))


def test_a_crop_keeps_the_colour_profile_its_page_carried():
    """Pillow's writer copies `icc_profile` into the crop today. Dropping it would
    not be a framing change: it decides how the samples are meant to be read.

    This is the framing-only path — the page's mode is already PNG-representable,
    so nothing converts. A crop whose mode DOES have to change colour class drops
    the profile instead, and the tests below that one say why."""
    profile = b"a synthetic colour profile, opaque to this pipeline"
    page = colour_page(icc_profile=profile)

    crop = crop_png(page, BOUNDS)

    assert [tag for tag, _ in chunks(crop)] == [b"IHDR", b"iCCP", b"IDAT", b"IEND"]
    assert image_shown(crop).icc_profile == profile
    assert carries_only_image_chunks(crop)


def test_a_conversion_that_changes_colour_class_drops_the_profile_it_made_false():
    """A CMYK profile attached to RGB samples is worse than no profile at all.

    Anything honouring it re-reads the pixels through a transformation that
    describes an image which no longer exists — and the crop is content-
    addressed, so those wrong bytes become the region's `image_sha256`. Pillow
    copies `info` straight across `convert()`, which is what makes this a real
    branch rather than a hypothetical one.
    """
    profile = b"a synthetic CMYK profile, opaque to this pipeline"
    page = Image.new("CMYK", (4, 2))
    page.info["icc_profile"] = profile
    assert "icc_profile" in page.convert("RGB").info, "Pillow stopped carrying it; test is moot"

    converted = _to_display_mode(page)

    assert converted.mode == "RGB"
    assert "icc_profile" not in converted.info


def test_a_rescaled_high_precision_crop_drops_the_profile_its_tone_response_named():
    """The `I;16` family is scaled by its declared range on the way to 8 bits, so
    a profile describing the 16-bit tone response describes nothing afterwards."""
    profile = b"a synthetic 16-bit gray profile"
    page = Image.new("I;16", (4, 2))
    page.info["icc_profile"] = profile

    converted = _to_display_mode(page)

    assert converted.mode == "L"
    assert "icc_profile" not in converted.info


def test_a_class_preserving_hop_keeps_the_profile_it_did_not_invalidate():
    """`La`→`LA` and `RGBa`→`RGBA` change how the same colours are stored, not
    which colours they are. Dropping a profile there would lose a true record."""
    profile = b"a synthetic colour profile, opaque to this pipeline"
    page = Image.new("RGBa", (4, 2))
    page.info["icc_profile"] = profile

    converted = _to_display_mode(page)

    assert converted.mode == "RGBA"
    assert converted.info.get("icc_profile") == profile


def test_an_indexed_crop_expanded_to_true_colour_keeps_its_profile():
    """The palette expansion is the third class-preserving hop: a `P` image's
    palette entries are already the colours the profile describes."""
    profile = b"a synthetic RGB profile, opaque to this pipeline"
    page = Image.new("P", (4, 2))
    page.putpalette([255, 0, 0, 0, 255, 0] + [0] * (768 - 6))
    page.putpixel((1, 0), 1)
    page.info["icc_profile"] = profile

    written = _encode_crop_deterministic(page)

    assert [tag for tag, _ in chunks(written)] == [b"IHDR", b"iCCP", b"IDAT", b"IEND"]
    assert image_shown(written).icc_profile == profile


# --- the comparison the boundary makes -------------------------------------------


def test_two_encodings_of_one_crop_are_the_same_image():
    page = grayscale_page()
    crop = crop_png(page, BOUNDS)
    with Image.open(BytesIO(crop)) as decoded:
        decoded.load()
        reframed = BytesIO()
        decoded.save(reframed, format="PNG", optimize=False, compress_level=1)

    assert reframed.getvalue() != crop
    assert image_shown(reframed.getvalue()) == image_shown(crop)


def test_one_changed_pixel_is_a_different_image():
    page = grayscale_page()
    crop = crop_png(page, BOUNDS)
    with Image.open(BytesIO(crop)) as decoded:
        decoded.load()
        tampered = decoded.copy()
        tampered.putpixel((0, 0), 255 - decoded.getpixel((0, 0)))
        output = BytesIO()
        tampered.save(output, format="PNG")

    assert image_shown(output.getvalue()) != image_shown(crop)


def test_a_colour_change_that_keeps_its_brightness_is_still_a_different_image():
    """`grayscale_rows` would flatten this one: red and blue ink of the same
    luminance are one image to a grayscale projection and two to a reader."""
    red, blue = Image.new("RGB", (2, 2), (255, 0, 0)), Image.new("RGB", (2, 2), (0, 0, 255))
    encoded = []
    for image in (red, blue):
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded.append(buffer.getvalue())

    assert image_shown(encoded[0]) != image_shown(encoded[1])


def test_undecodable_bytes_are_refused_rather_than_compared():
    for bad in (b"", b"not a png", PNG_SIGNATURE):
        with pytest.raises(ValueError, match="not decodable"):
            image_shown(bad)


# --- what may ride beside the pixels ---------------------------------------------


def test_a_text_chunk_beside_the_pixels_is_not_an_image_only_png():
    """The byte comparison used to refuse this for free. Once two framings of one
    image are accepted as equal, payload travelling beside the picture is the
    thing that comparison stopped saying anything about."""
    crop = crop_png(grayscale_page(), BOUNDS)
    tag, data = b"tEXt", b"note\x00anything at all"
    smuggled = (
        crop[:-12]
        + struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data))
        + crop[-12:]
    )

    assert image_shown(smuggled) == image_shown(crop)
    assert not carries_only_image_chunks(smuggled)


def test_a_rendering_intent_chunk_is_not_an_image_only_png():
    """gAMA and its family change how the samples are DISPLAYED without changing
    the samples `image_shown` compares — an allowed-but-uncompared chunk would
    let a crop pass the pixel identity and still render differently."""
    crop = crop_png(grayscale_page(), BOUNDS)
    tag, data = b"gAMA", struct.pack(">I", 45455)
    smuggled = (
        crop[:33]
        + struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data))
        + crop[33:]
    )

    assert image_shown(smuggled) == image_shown(crop)
    assert not carries_only_image_chunks(smuggled)


def test_the_deterministic_encoder_refuses_what_it_cannot_write_by_name():
    """The two refusal paths are contracts, not accidents: an unlisted mode and
    a scanline packing that disagrees with PNG's own stride both refuse."""
    exotic = Image.new("I;16", (4, 2))
    with pytest.raises(ValueError, match="no defined deterministic PNG layout"):
        _encode_crop_deterministic(exotic)

    lying = Image.new("L", (4, 2))
    real_tobytes = Image.Image.tobytes
    try:
        Image.Image.tobytes = lambda self, *a, **k: b"\x00" * 3
        with pytest.raises(ValueError, match="packed 3 bytes where PNG declares"):
            _encode_crop_deterministic(lying)
    finally:
        Image.Image.tobytes = real_tobytes


def test_bytes_after_the_end_marker_are_not_an_image_only_png():
    crop = crop_png(grayscale_page(), BOUNDS)

    assert not carries_only_image_chunks(crop + b"appended payload")
    assert not carries_only_image_chunks(crop[:-1])
    assert not carries_only_image_chunks(b"not a png at all")


# --- Split derivatives report the samples they actually seal ------------------


def _tiff_master(mode: str, size: tuple[int, int] = (6, 4)) -> bytes:
    """A one-frame TIFF master in exactly the mode under test."""
    image = Image.new(mode, size)
    for x in range(size[0]):
        for y in range(size[1]):
            image.putpixel((x, y), (x * 9973 + y) % 65535 if mode == "I;16" else (x + y) % 255)
    output = BytesIO()
    image.save(output, format="TIFF", compression="raw")
    return output.getvalue()


def _part(width: int, height: int, colour_mode: str) -> dict:
    return {
        "region": {"space": "frame", "x": 0, "y": 0, "w": width, "h": height},
        "crop_box": {"space": "part", "x": 0, "y": 0, "w": width, "h": height},
        "rotation": {
            "rotation_millidegrees": 0,
            "direction": "clockwise",
            "origin": "crop-centre",
            "canvas": "expand",
        },
        "colour_mode": colour_mode,
    }


def test_keep_refuses_a_master_this_encoder_would_have_to_convert():
    """`colour_mode: "keep"` declares that no conversion happens.

    A 16-bit master cannot satisfy it through an 8-bit PNG conversion. The
    whole-page path changes codec to retain those samples, while a triage part
    must either declare its conversion or refuse it by name.
    """
    master = _tiff_master("I;16")
    with pytest.raises(ValueError, match="cannot be sealed under colour_mode 'keep'"):
        render_triage_derivative(master, page_index=0, part=_part(6, 4, "keep"))

    # An explicit conversion makes the sample change part of the sealed decision.
    _rendered, geometry = render_triage_derivative(
        master, page_index=0, part=_part(6, 4, "grayscale")
    )
    assert geometry["source_mode"] == "I;16"
    assert geometry["color_mode"] == "L"


def test_the_render_record_names_the_mode_the_sealed_bytes_are_actually_in():
    """Reported from the encoded PNG's own header, not from the pre-encode image.

    A palette master is the case with no loss and a mode change anyway: the
    encoder expands `P` to true colour pixel-for-pixel, so the record must name
    the encoded `RGB` mode rather than the input `P` mode.
    """
    palette = Image.new("P", (6, 4))
    palette.putpalette([(index * 3) % 256 for index in range(768)])
    output = BytesIO()
    palette.save(output, format="PNG")

    rendered, geometry = render_triage_derivative(
        output.getvalue(), page_index=0, part=_part(6, 4, "keep")
    )
    assert geometry["source_mode"] == "P"
    assert geometry["color_mode"] == "RGB"
    assert Image.open(BytesIO(rendered)).mode == "RGB"
    assert (geometry["source_width"], geometry["source_height"]) == (6, 4)


def _plain_tiff_master(mode: str) -> bytes:
    image = Image.new(mode, (6, 4))
    if mode == "P":
        image.putpalette([(index * 5) % 256 for index in range(768)])
    output = BytesIO()
    image.save(output, format="TIFF", compression="raw")
    return output.getvalue()


@pytest.mark.parametrize("mode", ["1", "L", "LA", "RGB", "RGBA", "P"])
def test_keep_admits_exactly_the_modes_the_encoder_carries_without_pixel_loss(mode):
    rendered, geometry = render_triage_derivative(
        _plain_tiff_master(mode), page_index=0, part=_part(6, 4, "keep")
    )

    expected = "RGB" if mode == "P" else mode
    assert geometry["source_mode"] == mode
    assert geometry["color_mode"] == expected
    with Image.open(BytesIO(rendered)) as reread:
        assert reread.mode == expected


@pytest.mark.parametrize("mode", ["I;16", "I", "F", "CMYK", "LAB"])
def test_keep_refuses_each_decodable_mode_that_requires_a_colour_or_sample_conversion(mode):
    with pytest.raises(ValueError, match="cannot be sealed under colour_mode 'keep'"):
        render_triage_derivative(_plain_tiff_master(mode), page_index=0, part=_part(6, 4, "keep"))


@pytest.mark.parametrize("source_mode", ["I;16", "I", "F", "CMYK", "LAB"])
@pytest.mark.parametrize(
    ("declared_mode", "output_mode"),
    [("grayscale", "L"), ("rgb", "RGB"), ("bitonal", "1")],
)
def test_every_declared_conversion_is_executable_and_records_the_bytes_actual_mode(
    source_mode, declared_mode, output_mode
):
    rendered, geometry = render_triage_derivative(
        _plain_tiff_master(source_mode),
        page_index=0,
        part=_part(6, 4, declared_mode),
    )

    assert geometry["source_mode"] == source_mode
    assert geometry["color_mode"] == output_mode
    with Image.open(BytesIO(rendered)) as reread:
        assert reread.mode == output_mode


def test_a_resized_view_is_framed_so_that_no_compressor_chooses_any_of_its_bytes():
    """Content-addressed resize bytes must not depend on Pillow's bundled zlib."""
    resized = resize_png_lanczos(
        crop_png(grayscale_page(20, 12), {"x": 0, "y": 0, "w": 20, "h": 12}), 10, 6
    )

    raw = stored_block_payload(idat_of(resized))
    stride = len(raw) // 6
    assert stride * 6 == len(raw)
    assert all(raw[row * stride] == 0 for row in range(6)), "a scanline is not filter 0"
    assert carries_only_image_chunks(resized)


def test_a_resize_to_the_source_size_is_the_crop_itself_and_not_a_re_encoding():
    """An identity-sized request records a crop, so its bytes must remain identical."""
    crop = crop_png(grayscale_page(20, 12), {"x": 0, "y": 0, "w": 20, "h": 12})

    assert resize_png_lanczos(crop, 20, 12) == crop


def bilevel_page(width: int = 40, height: int = 24) -> bytes:
    """Mode ``1`` reaches crop/resize because the door preserves bilevel pages."""
    image = Image.new("1", (width, height), 1)
    for x in range(0, width, 3):
        for y in range(2, height - 2):
            image.putpixel((x, y), 0)
    return _encode_crop_deterministic(image)


def test_a_bilevel_crop_is_really_resampled_rather_than_silently_decimated():
    """Pillow answers LANCZOS on a `1` image with NEAREST and reports nothing.

    A sealed ``pillow-lanczos`` transform must differ from that silent
    nearest-neighbour substitution; output mode alone cannot prove promotion
    happened before resampling.
    """
    crop = crop_png(bilevel_page(), {"x": 0, "y": 0, "w": 40, "h": 24})
    with Image.open(BytesIO(crop)) as image:
        image.load()
        assert image.mode == "1", "the fixture stopped exercising the substitution"
        decimated = _encode_crop_deterministic(
            image.resize((20, 12), resample=Image.Resampling.NEAREST)
        )

    resized = resize_png_lanczos(crop, 20, 12)

    assert resized != decimated
    with Image.open(BytesIO(resized)) as reread:
        assert reread.mode == "L"
        assert len(set(reread.convert("L").tobytes())) > 2, "no intermediate samples were produced"


def test_a_bilevel_crop_that_needs_no_resize_is_still_returned_untouched():
    """No promotion may change evidence when an identity-sized view runs no filter."""
    crop = crop_png(bilevel_page(), {"x": 0, "y": 0, "w": 40, "h": 24})

    assert resize_png_lanczos(crop, 40, 24) == crop


def test_a_palette_resize_preserves_transparency_while_making_lanczos_run():
    """Pillow silently forces NEAREST for palette images, but promoting a
    transparent palette to RGB would repair the filter by deleting alpha."""
    image = Image.new("P", (4, 4))
    image.putpalette([255, 0, 0, 0, 255, 0] + [0, 0, 0] * 254)
    image.putdata([0, 1, 0, 1] * 4)
    image.info["transparency"] = 0
    encoded = BytesIO()
    image.save(encoded, format="PNG")

    # The alpha assertion below does catch a deleted promotion today -- without
    # it the transparency does not survive to the re-read and alpha comes back
    # all-255. But it catches it indirectly, through what happens to the palette
    # rather than through which resampler ran, and the property in this test's
    # name is that LANCZOS ran. Compare against the nearest-neighbour result the
    # bilevel case already compares against, so the substitution is observed
    # rather than inferred.
    with Image.open(BytesIO(encoded.getvalue())) as opened:
        opened.load()
        assert opened.mode == "P", "the fixture stopped exercising the substitution"
        decimated = _encode_crop_deterministic(
            opened.resize((2, 2), resample=Image.Resampling.NEAREST)
        )

    resized = resize_png_lanczos(encoded.getvalue(), 2, 2)

    assert resized != decimated
    with Image.open(BytesIO(resized)) as reread:
        assert reread.mode == "RGBA"
        assert min(reread.getchannel("A").getextrema()) < 255


def test_a_resize_target_is_bounded_before_pillow_can_allocate_it(monkeypatch):
    crop = crop_png(grayscale_page(20, 12), {"x": 0, "y": 0, "w": 20, "h": 12})
    called = False

    def resize_was_reached(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Pillow reached an over-bound target")

    monkeypatch.setattr(Image.Image, "resize", resize_was_reached)
    with pytest.raises(ValueError, match="resize target.*pixel bound"):
        resize_png_lanczos(crop, 100_000_001, 1)
    assert called is False
