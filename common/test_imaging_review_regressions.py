"""Regressions from an outside review of `common/imaging.py`.

Source: "GPT-6 review kit, 2026-09-06, Tyrel" — proposed by an outside reviewer
reading `main` on the web, with no checkout, and delivered as
`workbench/raw/gpt6-review-2026-09-06/`. The kit records its own licence as
Apache-2.0 (`ATTRIBUTION.md`, `LICENSE-APACHE-2.0.txt`), which permits this
carry; `cleanroom/README.md` is why it is named here rather than merged in
silently. The fixtures, the independent PNG writer and the case list are the
reviewer's; the assertions below were re-run against the real module and the
ones the fix answered differently are adjusted here, each with its reason.

The kit's own limits are worth keeping: it was syntax-checked, never executed,
and it makes no claim that an admitted source reaches any of these branches.
That trace was done separately and is what decided the fixes — every case below
is reachable from a source the door admits, and the tests that prove *that* live
at the end of this file rather than in the reviewer's original set. The one
exception is the palette-alpha refusal, which guards a route no decoder in this
stack was measured to produce; its own docstring says so rather than borrowing
the claim the rest of the file earns.

All fixtures are tiny, written by an independent PNG writer rather than by the
encoder under test, and Pillow is the rendering oracle.
"""

from __future__ import annotations

import os
import struct
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.imaging import (
    _grayscale_samples,
    _to_display_mode,
    crop_png,
    decode_grayscale_png,
    grayscale_rows,
    image_shown,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _png(
    width: int,
    height: int,
    color_type: int,
    scanlines: bytes,
    ancillary: bytes = b"",
    *,
    compression: int = 0,
    filter_method: int = 0,
    bit_depth: int = 8,
) -> bytes:
    header = struct.pack(
        ">IIBBBBB", width, height, bit_depth, color_type, compression, filter_method, 0
    )
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + ancillary
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


def _rgba(data: bytes) -> tuple[tuple[int, int], bytes]:
    with Image.open(BytesIO(data)) as image:
        image.load()
        return image.size, image.convert("RGBA").tobytes()


@pytest.mark.parametrize("color_type", [0, 2], ids=["grayscale-trns", "truecolor-trns"])
def test_full_crop_preserves_transparency(color_type: int) -> None:
    """Both grayscale and truecolor can express alpha through a tRNS chunk."""
    if color_type == 0:
        scanlines = b"\0\0\xff"
        transparency = struct.pack(">H", 0)
    else:
        scanlines = b"\0\0\0\0\xff\xff\xff"
        transparency = struct.pack(">3H", 0, 0, 0)
    source = _png(2, 1, color_type, scanlines, _chunk(b"tRNS", transparency))
    expected = _rgba(source)
    # The fixture itself must have a transparent first pixel.
    assert expected[1][3] == 0
    result = crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})
    assert _rgba(result) == expected, "A geometric crop must not turn transparency into opaque ink"


def test_a_partial_crop_keeps_the_transparency_of_the_pixels_it_kept() -> None:
    """Not only the full-page case the reviewer wrote: the cut is what a crop is.

    A full-frame crop can pass on a passthrough that never re-encodes anything,
    so the case that actually exercises the encoder is one where the geometry
    changes and the tRNS chunk still has to be written out again.
    """
    source = _png(2, 1, 0, b"\0\0\xff", _chunk(b"tRNS", struct.pack(">H", 0)))
    result = crop_png(source, {"x": 0, "y": 0, "w": 1, "h": 1})
    size, rgba = _rgba(result)
    assert size == (1, 1)
    assert rgba[3] == 0


def test_grayscale_crop_preserves_valid_gray_icc_profile() -> None:
    """Use a locally supplied valid grayscale profile; do not vendor system data."""
    supplied = os.environ.get("VERBATUS_REVIEW_GRAY_ICC")
    candidates = ([Path(supplied)] if supplied else []) + [
        Path("/usr/share/color/icc/ghostscript/default_gray.icc"),
        Path("/System/Library/ColorSync/Profiles/Generic Gray Gamma 2.2 Profile.icc"),
    ]
    profile_path = next((path for path in candidates if path.is_file()), None)
    if profile_path is None:
        pytest.skip(
            "Set VERBATUS_REVIEW_GRAY_ICC to a valid grayscale ICC profile to run this case"
        )
    profile = profile_path.read_bytes()
    assert len(profile) >= 128 and profile[36:40] == b"acsp", "Expected an ICC profile"
    assert profile[16:20] == b"GRAY", "Expected a grayscale ICC profile"
    ancillary = _chunk(b"iCCP", b"gray\0\0" + zlib.compress(profile))
    source = _png(2, 1, 0, b"\0\x40\xc0", ancillary)
    with Image.open(BytesIO(source)) as image:
        image.load()
        assert image.info.get("icc_profile") == profile
    result = crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})
    with Image.open(BytesIO(result)) as image:
        image.load()
        assert image.info.get("icc_profile") == profile


def test_grayscale_crop_preserves_a_profile_with_no_system_profile_to_borrow() -> None:
    """The same assertion as the case above, on a machine that has no profile.

    The reviewer's case skips wherever no valid grayscale profile happens to be
    installed, which on a CI worker is most of the time — and a regression that
    skips on the machine that gates the merge is not a regression test
    (GOVERNANCE 10: a metric that cannot be measured is a failure, not a pass).
    Found by CodeRabbit. The profile here is a minimal, synthetic, structurally
    valid grayscale ICC header rather than a vendored system asset: what is
    under test is that the crop carries the bytes it was given, not that any
    colour management interprets them.
    """
    profile = bytearray(132)
    profile[0:4] = struct.pack(">I", 132)  # profile size
    profile[12:16] = b"mntr"  # device class
    profile[16:20] = b"GRAY"  # data colour space
    profile[20:24] = b"XYZ "  # profile connection space
    profile[36:40] = b"acsp"  # the ICC signature every profile carries
    profile[128:132] = struct.pack(">I", 0)  # an empty tag table
    profile = bytes(profile)
    assert profile[36:40] == b"acsp" and profile[16:20] == b"GRAY"

    source = _png(2, 1, 0, b"\0\x40\xc0", _chunk(b"iCCP", b"gray\0\0" + zlib.compress(profile)))

    assert image_shown(crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})).icc_profile == profile


def test_grayscale_rows_rescales_16bit_samples_consistently() -> None:
    """Use the same full-range /257 policy already described for display crops."""
    image = Image.frombytes("I;16", (4, 1), struct.pack("<4H", 0, 257, 32896, 65535))
    output = BytesIO()
    image.save(output, format="TIFF")
    width, height, rows = grayscale_rows(output.getvalue())
    assert (width, height) == (4, 1)
    assert list(rows[0]) == [0, 1, 128, 255], "Direct convert('L') clips rather than rescales"


def test_grayscale_rows_refuses_an_undefined_sample_range_instead_of_clipping() -> None:
    """The other half of the same policy, which the kit did not reach.

    `I` and `F` declare no range at all, so scaling them would be a guess where
    `I;16` is arithmetic. The crop path has always refused them by name; this is
    the reader agreeing, rather than one path refusing while the other clips.
    """
    image = Image.new("I", (2, 1))
    image.putpixel((0, 0), 300)
    output = BytesIO()
    image.save(output, format="TIFF")
    with pytest.raises(ValueError, match="no defined sample range"):
        grayscale_rows(output.getvalue())


def test_a_16bit_page_with_transparency_crops_with_its_transparency_rescaled() -> None:
    """Where the two fixes meet, and where a careless one would lose a page.

    A 16-bit grayscale PNG names its transparent sample in 16-bit terms. The crop
    path rescales the pixels to 8 bits, so a transparency record carried across
    untouched would name a value the crop's own samples cannot hold — and an
    encoder that refused it there would turn an ordinary page into a refusal.
    The record is scaled with the samples it belongs to.
    """
    source = PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 16, 0, 0, 0, 0))
    source += _chunk(b"tRNS", struct.pack(">H", 65535))
    source += _chunk(b"IDAT", zlib.compress(b"\0" + struct.pack(">2H", 0, 65535)))
    source += _chunk(b"IEND", b"")

    crop = crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})

    with Image.open(BytesIO(crop)) as image:
        image.load()
        assert image.mode == "L"
        assert image.info.get("transparency") == 255
        assert image.convert("RGBA").tobytes()[7] == 0, "the white sample stays transparent"


def test_grayscale_rows_refuses_a_transparent_page_rather_than_counting_it_as_ink() -> None:
    """A crop keeps transparency; a grey *reading* of a page cannot invent paper.

    `convert("L")` drops alpha, so a transparent region reads as whatever sample
    sits under it — usually zero, which every reader in this pipeline counts as
    ink. Compositing against white instead would be this module deciding what
    colour the paper is. Both are policy, so the page is held with a named
    refusal instead. Found by CodeRabbit reviewing the transparency fix.
    """
    tRNS_page = _png(2, 1, 0, b"\0\0\xff", _chunk(b"tRNS", struct.pack(">H", 0)))
    with pytest.raises(ValueError, match="not settled"):
        grayscale_rows(tRNS_page)

    alpha_page = BytesIO()
    Image.new("RGBA", (2, 1), (0, 0, 0, 0)).save(alpha_page, format="PNG")
    with pytest.raises(ValueError, match="not settled"):
        grayscale_rows(alpha_page.getvalue())


def test_grayscale_rows_still_reads_a_page_whose_alpha_channel_says_nothing() -> None:
    """The refusal is about transparency, not about the presence of a channel: a
    fully opaque alpha channel loses nothing, and refusing it would cost a page
    for no reading at all (GOALS 1)."""
    opaque = BytesIO()
    Image.new("RGBA", (2, 1), (10, 10, 10, 255)).save(opaque, format="PNG")

    width, height, rows = grayscale_rows(opaque.getvalue())

    assert (width, height) == (2, 1)
    assert set(rows[0]) == {10}


def _invalid_png(case: str) -> bytes:
    valid = _png(1, 1, 0, b"\0\x80")
    if case == "missing-iend":
        return valid[:-12]
    if case == "bad-ihdr-crc":
        data = bytearray(valid)
        data[29] ^= 1  # First CRC byte of IHDR; samples and dimensions are unchanged.
        return bytes(data)
    if case == "zero-width":
        return _png(0, 1, 0, b"\0")
    if case == "zero-height":
        return _png(1, 0, 0, b"")
    if case == "invalid-compression-method":
        return _png(1, 1, 0, b"\0\x80", compression=1)
    if case == "invalid-filter-method":
        return _png(1, 1, 0, b"\0\x80", filter_method=1)
    if case == "trailing-data":
        return valid + b"unexpected-trailing-payload"
    if case == "image-data-before-ihdr":
        # A valid file with one extra IDAT in front of the header that describes
        # it: the decoder must not collect bytes it has no geometry for.
        return (
            PNG_SIGNATURE + _chunk(b"IDAT", zlib.compress(b"\0\x80")) + valid[len(PNG_SIGNATURE) :]
        )
    if case == "bytes-after-the-zlib-stream":
        # The zlib stream is complete and the IDAT keeps going, which is the
        # bytes-after-IEND smuggling channel one layer down.
        header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
        return (
            PNG_SIGNATURE
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(b"\0\x80") + b"appended")
            + _chunk(b"IEND", b"")
        )
    if case == "second-ihdr":
        header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
        return valid[:-12] + _chunk(b"IHDR", header) + valid[-12:]
    if case == "iend-carrying-data":
        return valid[:-12] + _chunk(b"IEND", b"payload")
    raise AssertionError(f"Unknown test case: {case}")


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-iend", "no IEND"),
        ("bad-ihdr-crc", "fails its own CRC"),
        ("zero-width", "declares no pixels"),
        ("zero-height", "declares no pixels"),
        ("invalid-compression-method", "compression method 1"),
        ("invalid-filter-method", "filter method 1"),
        ("trailing-data", "follow IEND"),
        # Two cases beyond the kit's list, found by CodeRabbit reviewing the fix.
        ("image-data-before-ihdr", "before its IHDR"),
        ("bytes-after-the-zlib-stream", "past the end of its own stream"),
        ("second-ihdr", "more than one IHDR"),
        ("iend-carrying-data", "IEND carries data"),
    ],
)
def test_native_decoder_rejects_invalid_internal_png(case: str, message: str) -> None:
    """Proposal: strict validity at the narrow, internal-codec boundary.

    Trailing-data refusal is a project hardening policy, not a claim that every
    image viewer must reject trailing bytes. Keep structural validity separate
    from fallback support for otherwise-valid formats outside this native codec's
    supported subset.

    Adjusted from the reviewer's version in one way: each case asserts the
    *named* refusal rather than any ValueError, because a decoder that refused
    all seven with one message would pass the original test while telling an
    operator nothing about which fault it found (GOVERNANCE 2).
    """
    with pytest.raises(ValueError, match=message):
        decode_grayscale_png(_invalid_png(case))


def test_native_decoder_positive_control() -> None:
    source = _png(2, 1, 0, b"\0\x40\xc0")
    width, height, rows = decode_grayscale_png(source)
    assert (width, height, [bytes(row) for row in rows]) == (2, 1, [b"\x40\xc0"])


def test_native_decoder_refuses_a_chunk_it_would_have_to_drop() -> None:
    """Why the transparency and profile cases above are fixes and not exceptions.

    This decoder returns bare grey samples, so a tRNS or iCCP chunk it walked
    past would be dropped with no trace. Refusing sends `crop_png` down its
    Pillow path, which carries both — the refusal is what makes the preservation
    happen, and it costs no page, because every caller falls back.
    """
    source = _png(2, 1, 0, b"\0\x40\xc0", _chunk(b"tRNS", struct.pack(">H", 0)))
    with pytest.raises(ValueError, match="does not read"):
        decode_grayscale_png(source)
    assert image_shown(crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})).width == 2


@pytest.mark.parametrize(
    ("mode", "byte_order"),
    [("I;16", "<"), ("I;16L", "<"), ("I;16B", ">"), ("I;16N", "<")],
)
def test_every_16bit_mode_scales_rather_than_refusing_the_page(mode: str, byte_order: str) -> None:
    """All four modes `_HIGH_PRECISION_SCALE` names, not only the one that worked.

    Pillow 12.3.0 compiles a callable `point` for `I`, `I;16` and `F` only; the
    three byte-order spellings raise `ValueError("point operation not supported
    for this mode")` before a pixel is read, which `grayscale_rows` re-worded as
    "not a decodable image" and turned into a dropped page (GOALS 1). Measured,
    not assumed: this parametrisation failed on `I;16L`, `I;16B` and `I;16N`
    before the fix and passes on all four after it.

    `frombytes` rather than a file because `I;16N` has no container that spells
    it; the test below carries the file half.
    """
    samples = struct.pack(f"{byte_order}4H", 0, 257, 32896, 65535)
    image = Image.frombytes(mode, (4, 1), samples)

    assert list(_grayscale_samples(image).tobytes()) == [0, 1, 128, 255]
    assert list(_to_display_mode(image).tobytes()) == [0, 1, 128, 255]


def test_a_big_endian_16bit_tiff_is_read_rather_than_called_undecodable() -> None:
    """The file half: `I;16B` is a mode an ordinary decoder hands back.

    `TiffImagePlugin.OPEN_INFO` maps a `MM` TIFF at 16 bits per sample to
    `I;16B`, so the refusal above was reachable from a real page and not only
    from a constructed image.
    """
    written = BytesIO()
    Image.frombytes("I;16B", (4, 1), struct.pack(">4H", 0, 257, 32896, 65535)).save(
        written, format="TIFF"
    )
    with Image.open(BytesIO(written.getvalue())) as reopened:
        assert reopened.mode == "I;16B", "the fixture must reach the mode under test"

    width, height, rows = grayscale_rows(written.getvalue())

    assert (width, height) == (4, 1)
    assert list(rows[0]) == [0, 1, 128, 255]


def test_a_16bit_truecolour_page_crops_instead_of_being_refused() -> None:
    """Pillow rescales 16-bit truecolour pixels to 8 bits and leaves tRNS alone.

    The record still names `(65535, 65535, 65535)` beside samples that now run to
    255, so the encoder measured a 16-bit record against the 8 bits it writes and
    refused an ordinary page. The record is converted with the pixels instead —
    by the decoder's own map, which for 16 bits keeps the high byte.
    """
    scanline = b"\0" + struct.pack(">6H", 0, 0, 0, 65535, 65535, 65535)
    source = _png(
        2, 1, 2, scanline, _chunk(b"tRNS", struct.pack(">3H", 65535, 65535, 65535)), bit_depth=16
    )
    with Image.open(BytesIO(source)) as opened:
        opened.load()
        assert opened.mode == "RGB", "Pillow reports 16-bit truecolour as plain RGB"
        assert opened.info.get("transparency") == (65535, 65535, 65535)

    result = crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})

    with Image.open(BytesIO(result)) as image:
        image.load()
        assert image.info.get("transparency") == (255, 255, 255)
        assert image.convert("RGBA").tobytes()[7] == 0, "the white sample stays transparent"
        assert image.convert("RGBA").tobytes()[3] == 255, "the black sample stays opaque"


@pytest.mark.parametrize(
    ("bit_depth", "packed", "declared", "expected"),
    [
        (4, 0x5A, 5, 85),
        (2, 0b01100000, 1, 85),
        # Depth 1 is the control: Pillow already restates a bilevel record in the
        # 0/255 terms its decoded samples use, so this one must not be converted
        # a second time.
        (1, 0b10000000, 1, 255),
    ],
    ids=["4-bit", "2-bit", "1-bit-control"],
)
def test_a_sub_byte_grey_page_marks_the_pixel_its_own_tRNS_named(
    bit_depth: int, packed: int, declared: int, expected: int
) -> None:
    """The right pixel, not the source number written out as an 8-bit sample.

    A 2- or 4-bit greyscale PNG decodes to mode `L` with its samples spread over
    the full range — 4-bit sample 5 becomes 85 — while `info["transparency"]`
    still says 5. Writing 5 back out named a value no pixel in the crop holds, so
    the page's declared transparency vanished and, had a pixel held 5, the wrong
    one would have been marked. Pillow's own `convert("RGBA")` of the source
    shows the same loss, which is why the depth-1 row is the oracle here: there
    Pillow converts the record itself, and the crop must agree with it.
    """
    source = _png(
        2,
        1,
        0,
        b"\0" + bytes([packed]),
        _chunk(b"tRNS", struct.pack(">H", declared)),
        bit_depth=bit_depth,
    )

    result = crop_png(source, {"x": 0, "y": 0, "w": 2, "h": 1})

    with Image.open(BytesIO(result)) as image:
        image.load()
        assert image.info.get("transparency") == expected
        rgba = image.convert("RGBA").tobytes()
        assert rgba[3] == 0, "the sample the file named transparent is transparent"
        assert rgba[7] == 255, "and the other one is not"


def test_a_16bit_record_outside_its_own_range_is_refused_rather_than_clamped() -> None:
    """The display path's half of the rule the crop path now states.

    A record no 16-bit sample can hold was clamped to 0 or 255, which marks a
    pixel the file never named — a reading change made silently, where the two
    conversions beside it refuse by name. Found by CodeRabbit reviewing this fix.
    """
    image = Image.frombytes("I;16", (2, 1), struct.pack("<2H", 0, 65535))
    image.info["transparency"] = 70000

    with pytest.raises(ValueError, match="cannot hold"):
        _to_display_mode(image)


def _palette_page(alphas: tuple[int, ...], indices: tuple[int, ...]) -> Image.Image:
    image = Image.new("P", (len(indices), 1))
    palette = bytearray()
    for entry, alpha in enumerate(alphas):
        palette += bytes([entry * 10, entry * 10, entry * 10, alpha])
    image.putpalette(bytes(palette), "RGBA")
    image.putdata(list(indices))
    return image


def test_a_palette_that_hides_its_alpha_is_refused_by_name() -> None:
    """`getbands()` says `("P",)`, so the band scan saw no alpha to refuse.

    With no `info["transparency"]` either, the page went straight to
    `convert("L")`, which reads the palette's *colour* for a fully transparent
    index — 0 for the usual transparent-black entry, which every reader here
    counts as ink.

    Asserted against `_grayscale_samples` rather than `grayscale_rows` on
    purpose: no decoder in this stack was measured to produce an RGBA palette
    from a file (PNG, GIF, BMP, TIFF and WebP all return an `RGB` palette, with
    any alpha in `info["transparency"]`), so claiming a reachable page here would
    be a claim the measurement does not support (GOVERNANCE 10). The guard is
    defence in depth beside the two crop-side callers that already ask the
    palette the same question.
    """
    with pytest.raises(ValueError, match="palette entry 0"):
        _grayscale_samples(_palette_page((0, 255, 255, 255), (0, 1, 2, 3)))


def test_a_palette_whose_used_entries_are_opaque_still_reads() -> None:
    """The refusal is about a transparent entry the page actually draws with.

    An opaque palette reads, and so does a page whose transparent entry nothing
    on it references — refusing either would cost an act (GOALS 1) for a byte
    that changes no pixel.
    """
    assert list(_grayscale_samples(_palette_page((255,) * 4, (0, 1, 2, 3))).tobytes()) == [
        0,
        10,
        20,
        30,
    ]
    assert list(_grayscale_samples(_palette_page((0, 255, 255, 255), (1, 2, 3))).tobytes()) == [
        10,
        20,
        30,
    ]


# The trace the reviewer could not run — that an admitted, ordinary source really
# does reach each of these branches — is asserted against the door itself, in
# `pipeline/1_exemplar/test_image_formats.py`, because only a test beside the door
# may import it.
