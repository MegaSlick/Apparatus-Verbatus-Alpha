"""A minimal grayscale PNG codec: encode, decode, crop. One implementation.

Shared by the Designator (cropping a sealed page), the Perlector (verifying a
handed region decodes to the claimed size) and the proof fixtures, so the
pipeline and its fixtures read bytes from exactly one encoder. Deliberately
narrow -- 8-bit grayscale, filter 0, no interlacing -- and anything else is
refused rather than guessed at; Pillow and PDFium handle real decoding
elsewhere (the Exemplar).

Reading is Pillow's job wherever this codec cannot; *writing* run evidence is
not. A crop is content-addressed, so its bytes name its blob path and every
digest above it, and bytes that depend on which zlib a wheel bundled are not
reproducible from the Exemplar plus the record. Every encoder a run writes
through is therefore this module's own, with only the PNG and DEFLATE
specifications deciding any byte.
"""

import hashlib
import math
import struct
import sys
import zlib
from collections.abc import Mapping
from io import BytesIO
from typing import Final, NamedTuple, TypedDict

import pillow_heif
from PIL import Image, UnidentifiedImageError

pillow_heif.register_heif_opener()

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_COLOR_TYPE_GRAYSCALE = 0
_COLOR_TYPE_TRUECOLOR = 2
_COLOR_TYPE_GRAYSCALE_ALPHA = 4
_COLOR_TYPE_TRUECOLOR_ALPHA = 6
_BIT_DEPTH = 8
_FILTER_NONE = 0

# The same ceiling the door admits pages under (`pipeline/1_exemplar/image_formats.py`),
# restated because `common/` may not import `pipeline/`; a test compares the two.
# Pillow's own decompression-bomb ceiling (89,478,485 pixels) sits below it, so this
# module needs its own bound rather than relying on Pillow's default.
MAX_PIXELS = 100_000_000

# Raised past this module's own bound so this module's refusal speaks, naming the
# page, rather than Pillow's internal decompression-bomb limit.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# The modes a decoded crop may keep; `_crop_decoded_page` routes everything else
# through `_to_display_mode` first. `P` reaches the encoder, which expands it to
# true colour, so it survives as a mode here but not in the written bytes.
PNG_CROP_MODES: Final = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})


def _refuse_past_pixel_bound(width: int, height: int, what: str = "page") -> None:
    """Keep every decode and allocation under the door's sealed pixel ceiling."""
    if width * height > MAX_PIXELS:
        raise ValueError(
            f"a {width}x{height} {what} is past this pipeline's {MAX_PIXELS}-pixel bound"
        )


# Pillow's bomb error descends from `Exception`, not `ValueError`, and every
# decode path here raises `ValueError` on an undecodable page.
_DECODE_FAILURES = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    Image.DecompressionBombError,
)

# `convert("RGB")` maps a high-precision sample straight through rather than
# scaling it, so 1024 and 65535 both land on 255. Scaled by the mode's own
# declared range, not the page's own maximum, so the same ink stays one grey.
_HIGH_PRECISION_SCALE = {"I;16": 1 / 257, "I;16L": 1 / 257, "I;16B": 1 / 257, "I;16N": 1 / 257}

# `I;16N` is the native-order spelling of the same 16-bit samples, so it is the
# same bytes as whichever explicit-order mode this machine's byte order names.
_NATIVE_16_BIT_MODE: Final = "I;16L" if sys.byteorder == "little" else "I;16B"


def _point_scalable(image: Image.Image) -> Image.Image:
    """The same samples in a mode Pillow's callable `point` will actually scale.

    Pillow 12.3.0 compiles a callable `point` for `I`, `I;16` and `F` only; the
    byte-order variants `I;16L`, `I;16B` and `I;16N` raise `ValueError` before
    any pixel is touched. A big-endian 16-bit TIFF opens as exactly `I;16B`, so
    this is a real source, not only a synthetic one.

    `convert("I")` is the one hop measured to keep the samples exact: `I;16L`
    and `I;16B` convert to `I` losslessly, while `convert("I;16")` clips every
    sample above 255 -- the clip this module exists to refuse -- and `I;16N`
    clips through every other target. `I;16N` is therefore respelled through
    its own bytes first, carrying `info` (and its transparency record) across.
    """
    if image.mode == "I;16N":
        respelled = Image.frombytes(_NATIVE_16_BIT_MODE, image.size, image.tobytes())
        respelled.info.update(image.info)
        image = respelled
    if image.mode in {"I;16L", "I;16B"}:
        return image.convert("I")
    return image


class _UnsettledReadingPolicy(ValueError):
    """A page this module can decode and has no settled way to read as grey.

    Its own `ValueError` subclass so paths that re-word a decode failure can
    let it through unchanged: this says the pipeline has no settled policy,
    not that the bytes are damaged.
    """


class _UndefinedSampleRange(_UnsettledReadingPolicy):
    """A mode whose samples declare no range, so no 8-bit reading of it is honest."""


class _UnreadableTransparency(_UnsettledReadingPolicy):
    """A page that declares transparency, which no grey value can stand for."""


def _refuse_undefined_sample_range(mode: str) -> None:
    """Refuse `I` and `F` by name rather than picking a black point for them.

    `I` is unbounded signed integer and `F` is float: neither declares a range,
    so any mapping to 8 bits is a policy choice about what black and white
    mean, and this module does not get to make it. The door keeps both modes
    losslessly, so refusing here surfaces an alarm instead of a silently
    flattened reading.
    """
    if mode in {"I", "F"}:
        raise _UndefinedSampleRange(
            f"a sealed page in mode {mode!r} has no defined sample range to read as 8 bits, "
            "and reading it would decide one silently; the door keeps these modes losslessly "
            "and the value-range policy for reading them is not settled"
        )


def _refuse_unreadable_palette_alpha(image: Image.Image) -> None:
    """The third place a page's transparency can hide: inside its own palette.

    A `P` page with alpha in an RGBA palette reports no `info["transparency"]`,
    so the checks above let it through, and `convert("L")` then reads the
    palette's *colour* for a fully transparent index -- usually 0, which every
    reader here counts as ink. No decoder in this stack was measured to
    produce this shape (PNG/GIF/BMP/TIFF/WebP all decode alpha to
    `info["transparency"]`), but an RGBA palette can still arrive from
    `convert("P")` or `quantize()` run in this process, so the guard stays.

    Only the entries the page actually uses are asked, via `getcolors` on the
    already-bounded page, so refusing a colour nothing on the page references
    would cost an act for nothing.
    """
    palette = image.palette
    palette_mode = getattr(palette, "mode", "")
    alpha = palette_mode.find("A")
    if palette is None or alpha < 0:
        return
    entries = palette.tobytes()
    stride = len(palette_mode)
    # `P` has at most 256 indices, so `or []` only covers a Pillow that
    # decides otherwise rather than a missing count reading as "no entry".
    counts = image.getcolors(1 << 8) or []
    for _count, index in counts:
        offset = index * stride + alpha
        if offset >= len(entries):
            # An index the palette does not describe reads through
            # `convert("L")` as 0 -- ink invented from an undefined byte.
            raise _UnsettledReadingPolicy(
                f"a sealed page draws with palette entry {index}, which its own palette of "
                f"{len(entries) // stride} entries does not describe, so there is no sample "
                "to read there and no settled policy for reading one that is not there"
            )
        if entries[offset] < 255:
            raise _UnreadableTransparency(
                f"a sealed page draws with palette entry {index}, which its own palette "
                f"marks {entries[offset]} of 255 opaque, and reading it as grey would "
                "count that entry as ink or as paper without either being recorded; the "
                "policy for reading a transparent page is not settled"
            )


def _refuse_unreadable_transparency(image: Image.Image) -> None:
    """Refuse to read a transparent page as grey rather than inventing paper.

    `convert("L")` drops an alpha channel and ignores a tRNS record, so a
    transparent pixel reads as whatever sample sits under it -- usually zero,
    which readers here count as ink. Compositing against white would just be
    this module guessing the paper's colour instead. `crop_png` is unaffected:
    a crop is a display image and PNG can carry the transparency; this is only
    about the grey *values* a stage will count.
    """
    if "transparency" in image.info:
        raise _UnreadableTransparency(
            "a sealed page declares a transparent sample, and reading it as grey would "
            "count that sample as ink or as paper without either being recorded; the "
            "policy for reading a transparent page is not settled"
        )
    if image.mode == "P":
        _refuse_unreadable_palette_alpha(image)
        return
    bands = image.getbands()
    alpha = next((index for index, band in enumerate(bands) if band.upper() == "A"), None)
    if alpha is None:
        return
    # `getchannel`, not `split()[alpha]`: splitting materialises every band to
    # read one of them.
    minimum, _maximum = image.getchannel(alpha).getextrema()
    if minimum < 255:
        raise _UnreadableTransparency(
            "a sealed page carries pixels that are not fully opaque, and reading it as "
            "grey would count them as ink or as paper without either being recorded; the "
            "policy for reading a transparent page is not settled"
        )


def _grayscale_samples(image: Image.Image) -> Image.Image:
    """One sealed page as 8-bit grey, scaling the modes a bare convert would clip.

    `convert("L")` maps a high-precision sample straight through, so a 16-bit
    scan reads as near-white ink returned as a blank page. Scaled here by
    `_HIGH_PRECISION_SCALE`, the same rule `crop_png` already applies, so the
    grey a stage measures and the grey a model is shown agree.
    """
    _refuse_unreadable_transparency(image)
    if image.mode == "L":
        return image
    scale = _HIGH_PRECISION_SCALE.get(image.mode)
    if scale is not None:
        # No `int()` inside the expression: Pillow probes the callable with an
        # `ImagePointTransform` to compile a scale and offset, and `int()`
        # would raise on that probe rather than on any pixel.
        return _point_scalable(image).point(lambda value: value * scale).convert("L")
    _refuse_undefined_sample_range(image.mode)
    return image.convert("L")


class Bounds(TypedDict):
    x: int
    y: int
    w: int
    h: int


def _chunk(tag: bytes, data: bytes) -> bytes:
    """One length-prefixed, CRC-checked PNG chunk."""
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def encode_grayscale_png(width: int, height: int, rows: list[bytearray]) -> bytes:
    """Encode 8-bit grayscale scanlines as a PNG.

    zlib at a fixed level with no other knobs keeps the output a pure function of
    the input bytes — nothing timestamp- or platform-derived enters the file. It
    is still only pure *for a given zlib build*, which is why the fixture bytes
    are checked in rather than regenerated and compared.
    """
    if len(rows) != height:
        raise ValueError(f"expected {height} scanlines, got {len(rows)}")
    if any(len(row) != width for row in rows):
        raise ValueError("a scanline is not the declared width")

    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        _BIT_DEPTH,
        _COLOR_TYPE_GRAYSCALE,
        0,  # compression method
        0,  # filter method
        0,  # interlace method
    )
    raw = bytearray()
    for row in rows:
        raw.append(_FILTER_NONE)
        raw.extend(row)
    idat = zlib.compress(bytes(raw), level=9)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


_STORED_BLOCK_MAX: Final = 65535


def _deterministic_stored_deflate(data: bytes) -> bytes:
    """A zlib stream whose every byte is fixed by RFC 1950/1951, not by a library.

    Stored (uncompressed) blocks only: the 0x78 0x01 header, then per block one
    flag byte (BFINAL in bit 0, BTYPE=00), LEN and NLEN little-endian, the raw
    bytes, and the adler32 of the whole payload. No compressor makes a choice
    anywhere in this framing, so two zlib builds cannot produce two streams.
    """
    out = bytearray(b"\x78\x01")
    if not data:
        out += b"\x01\x00\x00\xff\xff"
    else:
        for offset in range(0, len(data), _STORED_BLOCK_MAX):
            block = data[offset : offset + _STORED_BLOCK_MAX]
            final = 1 if offset + _STORED_BLOCK_MAX >= len(data) else 0
            out.append(final)
            out += struct.pack("<HH", len(block), len(block) ^ 0xFFFF)
            out += block
    out += struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF)
    return bytes(out)


def _deterministic_png(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    rows: list[bytearray],
    *,
    ancillary: bytes = b"",
) -> bytes:
    """The one PNG framing whose every byte is fixed by the specifications.

    Filter 0 on every scanline and a stored-block DEFLATE stream, so no
    compressor and no library version gets to choose anything. `ancillary`
    carries already-built chunks that belong between IHDR and IDAT.
    """
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        0,  # compression method
        0,  # filter method
        0,  # interlace method
    )
    raw = bytearray()
    for row in rows:
        raw.append(_FILTER_NONE)
        raw.extend(row)
    idat = _deterministic_stored_deflate(bytes(raw))
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + ancillary
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def encode_grayscale_png_deterministic(width: int, height: int, rows: list[bytearray]) -> bytes:
    """`encode_grayscale_png`, but byte-identical on every platform.

    The ordinary encoder is pure only for a given zlib build, and a run-time
    blob whose bytes depend on the wheel renames its content-addressed path.
    Evidence written *during* a run therefore uses this encoder instead: every
    byte fixed by the PNG and DEFLATE specifications, at the cost of size
    (stored blocks do not compress) -- acceptable for a run's bounded renders
    and crops, wrong for checked-in fixtures, so both encoders exist.
    """
    if len(rows) != height:
        raise ValueError(f"expected {height} scanlines, got {len(rows)}")
    if any(len(row) != width for row in rows):
        raise ValueError("a scanline is not the declared width")

    return _deterministic_png(width, height, _BIT_DEPTH, _COLOR_TYPE_GRAYSCALE, rows)


# Pillow mode -> (PNG bit depth, PNG colour type, samples per pixel), for the
# modes a crop can be in after `_crop_decoded_page`; an unlisted mode is
# refused rather than guessed at. `P` is deliberately absent -- see
# `_encode_crop_deterministic`.
_PNG_LAYOUT: Final = {
    "1": (1, _COLOR_TYPE_GRAYSCALE, 1),
    "L": (_BIT_DEPTH, _COLOR_TYPE_GRAYSCALE, 1),
    "LA": (_BIT_DEPTH, _COLOR_TYPE_GRAYSCALE_ALPHA, 2),
    "RGB": (_BIT_DEPTH, _COLOR_TYPE_TRUECOLOR, 3),
    "RGBA": (_BIT_DEPTH, _COLOR_TYPE_TRUECOLOR_ALPHA, 4),
}

# What Pillow's own PNG writer names an embedded colour profile.
_ICC_PROFILE_NAME: Final = b"ICC Profile"


def _png_source_bit_depth(png_bytes: bytes) -> int | None:
    """The bit depth an image's own IHDR declares, or `None` if it declares none.

    Read from the bytes rather than asked of Pillow, which discards it on
    decode: nothing on a decoded image says whether its `L` samples came from
    a 2-, 4- or 8-bit file. Anything that is not a PNG with a 13-byte IHDR
    answers `None`, read by every caller as "no depth to convert from".
    """
    if not png_bytes.startswith(PNG_SIGNATURE) or len(png_bytes) < 26:
        return None
    length, tag = struct.unpack(">I4s", png_bytes[8:16])
    if tag != b"IHDR" or length != 13:
        return None
    return png_bytes[24]


def _transparency_to_decoded_range(image: Image.Image, source_bit_depth: int | None) -> None:
    """Restate a tRNS record in the range the decoder just put the pixels in.

    PNG names a transparent sample in the file's own bit depth, but Pillow
    rescales the pixels while leaving `info["transparency"]` in that original
    depth -- so a 16-bit record like `(65535, 65535, 65535)` would otherwise be
    checked against the 8-bit samples a crop is written in, and a sub-byte
    depth's record would name a value no rescaled pixel holds, marking the
    wrong pixel transparent with no error anywhere.

    The conversions are the decoder's own (measured on Pillow 12.3.0): 16 bits
    keep the high byte; sub-byte depths use `value * 255 // source_maximum`.
    The record is validated in the source's range first, so an out-of-range
    record is refused by name rather than silently rescaled as if 16-bit.

    Mode `1` and the `I;16` family are deliberately absent: Pillow already
    reports a 1-bit record in decoded terms, and `_to_display_mode` rescales
    an `I;16` record together with its still-16-bit pixels.
    """
    if source_bit_depth is None or source_bit_depth == _BIT_DEPTH:
        return
    if image.mode not in {"L", "RGB"}:
        return
    record = image.info.get("transparency")
    if record is None:
        return
    grouped = isinstance(record, tuple | list)
    samples = tuple(record) if grouped else (record,)
    if any(not isinstance(sample, int) or isinstance(sample, bool) for sample in samples):
        return  # `_transparency_chunk` names the required shape and refuses it there
    source_maximum = (1 << source_bit_depth) - 1
    if any(not 0 <= sample <= source_maximum for sample in samples):
        raise ValueError(
            f"a page decoded to mode {image.mode!r} carries a transparency record {record!r} "
            f"that its own {source_bit_depth}-bit source samples cannot name"
        )
    if source_bit_depth == 16:
        converted = tuple(sample >> 8 for sample in samples)
    else:
        converted = tuple(sample * 255 // source_maximum for sample in samples)
    image.info["transparency"] = converted if grouped else converted[0]


def _transparency_chunk(crop: Image.Image, bit_depth: int) -> bytes:
    """The tRNS chunk a crop's own transparency record needs, or nothing.

    A grayscale or truecolour PNG names one sample value as fully transparent
    rather than carrying an alpha channel, and Pillow keeps that value in
    `info["transparency"]` across `open` and `crop`; without writing it back
    out, a crop of a transparent page would return the background turned to
    ink -- a reading change ARCHITECTURE's third invariant forbids.

    `LA` and `RGBA` carry alpha in the samples and never reach here with a
    record to add. A record this encoder cannot express is refused by name.
    """
    transparency = crop.info.get("transparency")
    if transparency is None or crop.mode in {"LA", "RGBA"}:
        return b""
    maximum = (1 << bit_depth) - 1
    if crop.mode in {"1", "L"}:
        if not isinstance(transparency, int) or isinstance(transparency, bool):
            raise ValueError(
                f"a {crop.mode!r} crop carries a transparency record of type "
                f"{type(transparency).__name__}, which is not one grey sample"
            )
        # Pillow reports a bilevel image's transparent sample in `1`-mode terms
        # (0 or 255); PNG names it in the file's own one-bit sample space.
        if crop.mode == "1":
            if transparency not in (0, 255):
                raise ValueError(
                    f"a '1' crop names sample {transparency} as transparent, which is "
                    "neither of the two samples a bilevel image has"
                )
            transparency = 0 if transparency == 0 else 1
        if not 0 <= transparency <= maximum:
            raise ValueError(
                f"a {crop.mode!r} crop names sample {transparency} as transparent, which "
                f"its own {bit_depth}-bit depth cannot hold"
            )
        return _chunk(b"tRNS", struct.pack(">H", transparency))
    if crop.mode == "RGB":
        if (
            not isinstance(transparency, tuple | list)
            or len(transparency) != 3
            or any(not isinstance(value, int) or isinstance(value, bool) for value in transparency)
            or any(not 0 <= value <= maximum for value in transparency)
        ):
            raise ValueError(
                f"an 'RGB' crop carries a transparency record {transparency!r} that is not "
                "three samples this encoder can name"
            )
        return _chunk(b"tRNS", struct.pack(">3H", *transparency))
    raise ValueError(
        f"a crop in mode {crop.mode!r} carries a transparency record this encoder has no "
        "defined tRNS layout for"
    )


def _encode_crop_deterministic(crop: Image.Image) -> bytes:
    """Write a decoded crop as a PNG no library gets to choose the bytes of.

    Pillow's wheels bundle their own zlib, so its PNG writer emits a different
    valid stream on a different build -- and a crop is content-addressed, so
    those bytes name its blob path and every digest above it. The run-tree
    store is write-once, so a resumed run re-cutting the same crop under a
    different wheel would refuse to publish rather than merely differ.

    Pixels, alpha and colour profile are carried over exactly; only the
    framing changes. A crop that changed colour class on the way here already
    lost its profile in `_to_display_mode`; this encoder writes what it gets.
    """
    profile = crop.info.get("icc_profile")
    if crop.mode == "P":
        # Expanded to true colour rather than re-serialised as a palette, so
        # this module stays the one place that decides what a Pillow palette
        # means. Both places alpha can hide are checked: a decoded PNG/GIF
        # puts its tRNS in `info["transparency"]`; a palette carrying alpha in
        # its own bytes would otherwise convert to RGB and lose it silently.
        keeps_alpha = "transparency" in crop.info or "A" in getattr(crop.palette, "mode", "RGB")
        crop = crop.convert("RGBA" if keeps_alpha else "RGB")
    layout = _PNG_LAYOUT.get(crop.mode)
    if layout is None:
        raise ValueError(f"a crop in mode {crop.mode!r} has no defined deterministic PNG layout")
    bit_depth, color_type, samples = layout
    width, height = crop.width, crop.height
    # Pillow packs `tobytes()` in exactly PNG's own scanline layout for these
    # modes, so no repacking is needed -- only the assertion that it did.
    stride = (width * samples * bit_depth + 7) // 8
    data = crop.tobytes()
    if len(data) != stride * height:
        raise ValueError(
            f"a {crop.mode!r} crop packed {len(data)} bytes where PNG declares {stride * height}"
        )
    ancillary = b""
    if isinstance(profile, bytes) and profile:
        ancillary = _chunk(
            b"iCCP",
            _ICC_PROFILE_NAME + b"\x00\x00" + _deterministic_stored_deflate(profile),
        )
    ancillary += _transparency_chunk(crop, bit_depth)  # iCCP precedes tRNS in PNG's own order
    return _deterministic_png(
        width,
        height,
        bit_depth,
        color_type,
        [bytearray(data[row * stride : (row + 1) * stride]) for row in range(height)],
        ancillary=ancillary,
    )


def decode_grayscale_png(png_bytes: bytes) -> tuple[int, int, list[bytearray]]:
    """Decode a PNG this module wrote. Refuses anything else rather than guessing.

    Strict about the file, not only the pixels: every chunk's CRC is verified,
    interlacing and other bit depths/colour/filter types are refused, IEND
    must be present with nothing after it. This is an internal codec whose
    whole purpose is that the bytes a stage reads are the bytes this module
    wrote, so a broken CRC or trailing payload must not be silently accepted.

    A refusal here is never a refused page: `crop_png`, `dimensions` and
    `grayscale_rows` all fall back to Pillow on a ValueError from this
    function, which preserves the colour profile and transparency this
    narrower codec would have thrown away.
    """
    if png_bytes[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG: missing signature")

    offset = 8
    width = height = None
    idat = bytearray()
    seen_ihdr = seen_iend = False

    while offset < len(png_bytes):
        if offset + 8 > len(png_bytes):
            raise ValueError("truncated PNG: incomplete chunk header")
        length, tag = struct.unpack(">I4s", png_bytes[offset : offset + 8])
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(png_bytes):
            raise ValueError("truncated PNG: chunk data runs past the end of the file")
        data = png_bytes[data_start:data_end]
        (stored_crc,) = struct.unpack(">I", png_bytes[data_end:crc_end])
        if zlib.crc32(tag + data) & 0xFFFFFFFF != stored_crc:
            raise ValueError(f"corrupt PNG: chunk {tag!r} fails its own CRC")

        if tag == b"IHDR":
            if seen_ihdr:
                raise ValueError("unsupported PNG: more than one IHDR")
            if length != 13:
                raise ValueError("unsupported PNG: malformed IHDR")
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            if width < 1 or height < 1:
                raise ValueError(
                    f"unsupported PNG: a {width}x{height} image declares no pixels at all"
                )
            if bit_depth != _BIT_DEPTH or color_type != _COLOR_TYPE_GRAYSCALE:
                raise ValueError("unsupported PNG: only 8-bit grayscale is decodable here")
            if compression != 0:
                raise ValueError(
                    f"unsupported PNG: compression method {compression} is not the one "
                    "method PNG defines"
                )
            if filter_method != 0:
                raise ValueError(
                    f"unsupported PNG: filter method {filter_method} is not the one "
                    "method PNG defines"
                )
            if interlace != 0:
                raise ValueError("unsupported PNG: interlaced images are not decodable here")
            seen_ihdr = True
        elif tag == b"IDAT":
            if not seen_ihdr:
                # PNG requires IHDR first, and without this an IDAT ahead of it is
                # simply collected: a later, valid IHDR would then decode a stream
                # made of bytes from before the header that describes it.
                raise ValueError("corrupt PNG: image data arrives before its IHDR")
            idat.extend(data)
        elif tag == b"IEND":
            if length != 0:
                raise ValueError("corrupt PNG: IEND carries data")
            seen_iend = True
            offset = crc_end
            break
        else:
            # A chunk saying how the returned bare grey samples are to be shown
            # (tRNS, iCCP, gAMA/sRGB) would be dropped silently; refused by name
            # instead, since `crop_png`'s Pillow fallback carries what it can.
            raise ValueError(
                f"unsupported PNG: chunk {tag!r} carries information this grayscale "
                "codec does not read, and decoding here would drop it silently"
            )

        offset = crc_end

    if not seen_ihdr or width is None or height is None:
        raise ValueError("unsupported PNG: missing IHDR")
    if not seen_iend:
        raise ValueError("truncated PNG: no IEND chunk")
    if offset != len(png_bytes):
        raise ValueError(
            f"unsupported PNG: {len(png_bytes) - offset} byte(s) follow IEND, so this file "
            "is not only the image it declares"
        )

    # Bounded before `expected` is computed: an IHDR naming an enormous
    # width/height would otherwise turn `stride * height` into a multi-gigabyte
    # `max_length` a small, highly compressible IDAT can actually fill.
    _refuse_past_pixel_bound(width, height)

    # `expected+1` bounds the decompression itself, not just its result:
    # unbounded `zlib.decompress` on attacker-shaped input would materialize
    # whatever the stream expands to before the length check below ever runs.
    stride = width + 1  # one filter-type byte per scanline
    expected = stride * height

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(bytes(idat), expected + 1)
    except zlib.error as error:
        raise ValueError(f"corrupt PNG: image data would not decompress ({error})") from error

    if len(raw) > expected:
        raise ValueError(
            f"corrupt PNG: image data expands past the {expected} bytes its own header declares"
        )
    # A truncated stream can return exactly `expected` bytes without raising;
    # `eof` distinguishes a complete stream from one that merely got far enough.
    if not decompressor.eof:
        raise ValueError("corrupt PNG: image data stream is truncated")
    if decompressor.unused_data or decompressor.unconsumed_tail:
        # Bytes after the zlib stream ended are not image data -- the same
        # smuggling channel as bytes after IEND, one layer down.
        raise ValueError("corrupt PNG: image data carries bytes past the end of its own stream")
    if len(raw) != expected:
        raise ValueError("corrupt PNG: decompressed data has the wrong length")

    rows: list[bytearray] = []
    for index in range(height):
        start = index * stride
        if raw[start] != _FILTER_NONE:
            raise ValueError("unsupported PNG: only filter type 0 is decodable here")
        rows.append(bytearray(raw[start + 1 : start + stride]))
    return width, height, rows


def crop_png(png_bytes: bytes, bounds: Bounds) -> bytes:
    """Cut a rectangle out of a sealed page and return lossless PNG bytes.

    Genuinely derived from the page's pixels rather than described, which is
    what makes ARCHITECTURE's third invariant a property of this code rather
    than a claim about it. Both paths encode deterministically, since a crop
    is content-addressed and its bytes may not depend on which zlib a wheel
    happened to bundle.
    """
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    if w <= 0 or h <= 0:
        raise ValueError(f"crop bounds {bounds} must have positive width and height")
    try:
        width, height, rows = decode_grayscale_png(png_bytes)
    except ValueError:
        return _crop_decoded_page(png_bytes, x, y, w, h)
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise ValueError(f"crop bounds {bounds} fall outside a {width}x{height} page")
    return encode_grayscale_png_deterministic(w, h, [row[x : x + w] for row in rows[y : y + h]])


# Pillow 12.3.0 silently forces NEAREST for modes `1` and `P`, so both must be
# promoted before a transform may truthfully name LANCZOS. This behavior is
# carried from `src/PIL/Image.py:2404-2405` under Pillow's MIT-CMU licence:
# https://github.com/python-pillow/Pillow/blob/12.3.0/src/PIL/Image.py#L2404-L2405
# https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE
def resize_png_lanczos(png_bytes: bytes, width: int, height: int) -> bytes:
    """Resize an image with Pillow LANCZOS and deterministic PNG framing.

    Bilevel and palette sources are promoted before Pillow can replace LANCZOS
    with nearest-neighbour; palette alpha is retained.

    An identity-sized request skips the resampler and mode promotion but still
    goes through this module's deterministic framing rather than being handed
    back untouched -- this is not a passthrough, though re-encoding one of this
    module's own crops reproduces the input exactly.
    """
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        raise ValueError("resize dimensions must be positive integers")
    # The source bound protects decoding; the target needs its own check before
    # Pillow allocates it. A sealed resize recipe is untrusted input on read-back,
    # and positive integers alone otherwise admit an arbitrarily large image.
    _refuse_past_pixel_bound(width, height, "resize target")
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            _refuse_past_pixel_bound(image.width, image.height)
            image.load()
            source = image
            resizing = (image.width, image.height) != (width, height)
            if resizing and image.mode == "1":
                source = image.convert("L")
            elif resizing and image.mode == "P":
                # A palette's transparency can live either in image metadata or
                # its palette; RGB promotion would discard those samples.
                keeps_alpha = "transparency" in image.info or "A" in getattr(
                    image.palette, "mode", "RGB"
                )
                source = image.convert("RGBA" if keeps_alpha else "RGB")
            resized = source.resize((width, height), resample=Image.Resampling.LANCZOS)
            return encode_image_deterministic(resized)
    except _DECODE_FAILURES as error:
        raise ValueError(f"image bytes are not decodable for resize ({error})") from error


def convert_png_to_rgb(png_bytes: bytes) -> bytes:
    """Expand an image to three 8-bit colour samples, deterministically framed.

    A vendor preprocessor converts before handing the model an image (Churro's
    ``ensure_rgb``), so that conversion has to be executed here and recorded in
    the transform -- left to the engine it would happen server-side,
    unrecorded, breaking ARCHITECTURE invariant 3.

    It is exactly ``Image.convert("RGB")``, the whole body of both vendors' own
    step, so the samples produced are the samples the vendor's model is given.

    An alpha channel is dropped, not refused or composited, because that is
    the path both vendors take; refusing it would leave a page the door
    legitimately admitted (``LA``/``RGBA``) with no legal presentation at all.
    The drop is recorded: the presentation names ``colour_mode: "rgb"``, and
    the alpha samples stay in the sealed page untouched.
    """
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            _refuse_past_pixel_bound(image.width, image.height, "colour conversion")
            image.load()
            if image.mode == "RGB":
                return encode_image_deterministic(image)
            if image.mode not in PNG_CROP_MODES:
                raise ValueError(
                    f"image mode {image.mode!r} is not a mode a sealed crop arrives in, so the "
                    f"vendor's own RGB conversion of it cannot be replayed here (crop modes "
                    f"{sorted(PNG_CROP_MODES)})"
                )
            return encode_image_deterministic(_without_colour_profile(image.convert("RGB")))
    except _DECODE_FAILURES as error:
        raise ValueError(
            f"image bytes are not decodable for colour conversion ({error})"
        ) from error


def dimensions(png_bytes: bytes) -> tuple[int, int]:
    """The dimensions of a sealed page, including RGB PNG renders from the door."""
    try:
        width, height, _ = decode_grayscale_png(png_bytes)
        return width, height
    except ValueError:
        try:
            with Image.open(BytesIO(png_bytes)) as image:
                _refuse_past_pixel_bound(image.width, image.height)
                image.load()
                return image.width, image.height
        except (*_DECODE_FAILURES, ValueError) as error:
            raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error


def grayscale_rows(png_bytes: bytes) -> tuple[int, int, list[bytearray]]:
    """Every pixel of a sealed page as 8-bit grayscale intensity, 0 (black) to
    255 (white).

    The fast path is this module's own lossless codec. Real imagery falls back
    to Pillow under the same `MAX_PIXELS` bound as `dimensions`, converting to
    grey through `_grayscale_samples` so a 16-bit scan is scaled rather than
    clipped and an `I`/`F` page is refused by name, exactly as the crop path.
    """
    try:
        return decode_grayscale_png(png_bytes)
    except ValueError:
        pass
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            _refuse_past_pixel_bound(image.width, image.height)
            image.load()
            grayscale = _grayscale_samples(image)
            width, height = grayscale.width, grayscale.height
            data = grayscale.tobytes()
    except _UnsettledReadingPolicy:
        raise  # not "not a decodable image": this is a policy refusal, not damage
    except (*_DECODE_FAILURES, ValueError) as error:
        raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error
    stride = width
    return (
        width,
        height,
        [bytearray(data[row * stride : (row + 1) * stride]) for row in range(height)],
    )


class ShownImage(NamedTuple):
    """What a PNG puts in front of a reader, independent of how it was written."""

    width: int
    height: int
    # The digest of the straight-RGBA samples rather than the samples: a full
    # page crop is 100 million pixels at this module's own ceiling, and two
    # callers comparing identities should not have to hold 800 MB of them to
    # find out they agree.
    pixel_sha256: str
    icc_profile: bytes | None


def image_shown(png_bytes: bytes) -> ShownImage:
    """The image a PNG shows: its size, every sample as straight RGBA, and the
    colour profile that says how to read them.

    Two encodings of one image are equal here and two different images are
    not, whatever bit depth, colour type, palette or compression stream each
    was written with -- unlike comparing the bytes, which asserts the stronger
    and unrelated claim that two encoders agreed (a claim a pod, a CI matrix or
    a resumed run can break). RGBA rather than each mode's own layout, since
    the two sides may legitimately hold the same picture in different modes.
    The profile is part of the identity: it changes how the samples are meant
    to be read, which is not an encoding difference.
    """
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            _refuse_past_pixel_bound(image.width, image.height)
            image.load()
            profile = image.info.get("icc_profile")
            rgba = image if image.mode == "RGBA" else image.convert("RGBA")
            return ShownImage(
                image.width,
                image.height,
                hashlib.sha256(rgba.tobytes()).hexdigest(),
                profile if isinstance(profile, bytes) else None,
            )
    except (*_DECODE_FAILURES, ValueError) as error:
        raise ValueError(f"image bytes are not decodable ({error})") from error


# Every tag here either declares the pixels or says how to interpret them; a
# text chunk, EXIF block or private tag is payload riding inside an image
# rather than part of it. The wider PNG metadata family (sRGB, gAMA, cHRM,
# sBIT, bKGD, pHYs) changes how pixels *render* without changing the samples
# `image_shown` compares, so allowing one would let a crop pass pixel identity
# and still display differently; this module's own encoders never emit them.
_IMAGE_ONLY_CHUNKS: Final = frozenset(
    {
        b"IHDR",
        b"PLTE",
        b"IDAT",
        b"IEND",
        b"tRNS",
        b"iCCP",
    }
)


def carries_only_image_chunks(png_bytes: bytes) -> bool:
    """True when a PNG holds its image and nothing else.

    "The pixels match" says nothing about a text chunk or trailing bytes; this
    says the file is the picture and no more. Its own walk rather than
    `decode_grayscale_png`'s, since this must verdict any PNG including colour
    types that decoder refuses, with a malformed structure reading `False`
    rather than raising. Chunk CRCs are not rechecked here: the caller has
    already decoded the image.
    """
    if png_bytes[:8] != PNG_SIGNATURE:
        return False
    offset = 8
    while offset + 8 <= len(png_bytes):
        length, tag = struct.unpack(">I4s", png_bytes[offset : offset + 8])
        end = offset + 12 + length  # 4 length + 4 tag + data + 4 CRC
        if end > len(png_bytes) or tag not in _IMAGE_ONLY_CHUNKS:
            return False
        if tag == b"IEND":
            return end == len(png_bytes)
        offset = end
    return False


def _crop_decoded_page(png_bytes: bytes, x: int, y: int, w: int, h: int) -> bytes:
    """Crop a decoded page and encode a PNG-compatible, display-ready result.

    PNG cannot represent CMYK or several decoder-private modes.  The sealed crop
    is a display image for later stages, so non-alpha modes become RGB and alpha
    modes become RGBA; no transparent pixel is flattened against an invented
    background.  The original Exemplar blob remains untouched and traceable.
    """
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            _refuse_past_pixel_bound(image.width, image.height)
            image.load()
            # Before the cut: `crop()` copies `info`, and this is the only place
            # that still has the source bytes to say what depth the record used.
            _transparency_to_decoded_range(image, _png_source_bit_depth(png_bytes))
            if x < 0 or y < 0 or x + w > image.width or y + h > image.height:
                raise ValueError(
                    f"crop bounds {{'x': {x}, 'y': {y}, 'w': {w}, 'h': {h}}} fall outside a "
                    f"{image.width}x{image.height} page"
                )
            crop = image.crop((x, y, x + w, y + h))
            if crop.mode not in PNG_CROP_MODES:
                crop = _to_display_mode(crop)
            return _encode_crop_deterministic(crop)
    except _DECODE_FAILURES as error:
        raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error


def _without_colour_profile(crop: Image.Image) -> Image.Image:
    """Drop an ICC profile the conversion just made false.

    Pillow copies `info` across `convert()`, so a CMYK page's profile arrives
    attached to RGB samples and a 16-bit scan's profile survives a rescale that
    changed its tone response -- a worse record than no profile, since
    anything honouring it re-interprets pixels through a transformation
    describing an image that no longer exists. Class-preserving hops (a
    palette expanded to true colour, `La` to `LA`) keep theirs.
    """

    crop.info.pop("icc_profile", None)
    return crop


def _to_display_mode(crop: Image.Image) -> Image.Image:
    """Convert a crop to a PNG-representable mode without crushing its samples.

    The door seals `I`, `F` and the `I;16*` family losslessly as TIFF, so those
    modes really do arrive here. A bare `convert("RGB")` maps their samples
    straight through instead of scaling, so a 16-bit scan would come out near-
    white with none of the page's ink.
    """
    mode = crop.mode
    scale = _HIGH_PRECISION_SCALE.get(mode)
    if scale is not None:
        # No `int()` inside the expression: Pillow probes the callable with an
        # `ImagePointTransform` to compile a scale/offset pair, and `int()`
        # would raise on that probe rather than on any pixel.
        # `_point_scalable` first: `I;16L`/`I;16B`/`I;16N` refuse a callable
        # `point` on Pillow 12.3.0.
        scalable = _point_scalable(crop)
        display = _without_colour_profile(scalable.point(lambda value: value * scale).convert("L"))
        # Pillow carries a 16-bit transparency record through `point`/`convert`
        # untouched, so it must be rescaled by the same factor as the pixels.
        transparency = display.info.get("transparency")
        if isinstance(transparency, int) and not isinstance(transparency, bool):
            # Refused rather than clamped: a record outside its own mode's
            # range is one nobody can account for, and clamping would mark a
            # pixel the file never named.
            if not 0 <= transparency <= 65535:
                raise ValueError(
                    f"a crop in mode {mode!r} names sample {transparency} as transparent, "
                    "which its own 16-bit samples cannot hold"
                )
            display.info["transparency"] = int(transparency * scale)
        return display
    _refuse_undefined_sample_range(mode)
    # Premultiplied alpha first: `La` converts only to `LA` and `RGBa` only to
    # `RGBA`, and the lower-case band name means a plain `"A" in bands` check
    # would read `La` as having no alpha and ask for RGB, which raises instead
    # of dropping the channel.
    unpremultiplied = {"La": "LA", "RGBa": "RGBA"}.get(mode)
    if unpremultiplied is not None:
        return crop.convert(unpremultiplied)
    # Everything reaching here (CMYK, YCbCr, LAB, HSV) changes colour class, so
    # the profile describing the source space describes nothing about the result.
    has_alpha = any(band.upper() == "A" for band in crop.getbands())
    return _without_colour_profile(crop.convert("RGBA" if has_alpha else "RGB"))


# The modes this module's PNG encoder carries without losing a sample: the
# layouts it writes directly, plus `P`, which `_encode_crop_deterministic`
# expands to true colour pixel-for-pixel. Anything outside this set reaches
# `_to_display_mode`, and that is a *conversion* — for `I;16` it is an 8-bit
# crush of 16-bit samples. Named here so a caller can ask before it converts.
ENCODER_LOSSLESS_MODES: Final = frozenset(_PNG_LAYOUT) | {"P"}


def encode_image_deterministic(image: Image.Image) -> bytes:
    """Encode one rendered derivative with the project-owned PNG encoder.

    Door page derivatives are evidence just as Designator crops are, so
    wheel-bundled zlib differences must not rename identical pages on another
    host -- the same encoder `crop_png` uses.
    """
    if image.mode not in ENCODER_LOSSLESS_MODES:
        image = _to_display_mode(image)
    return _encode_crop_deterministic(image)


def imaging_library_versions() -> dict[str, str]:
    """The decoder versions that can change a derivative page's pixels.

    One place, so a Door render's recorded recipe and a drift boundary's
    report can never name different numbers.
    """
    return {
        "renderer": "Pillow",
        "renderer_version": Image.__version__,
        "pillow_heif_version": pillow_heif.__version__,
        "libheif_version": pillow_heif.libheif_info()["libheif"],
    }


def _refuse_uncontained_rectangle(
    rectangle: Mapping[str, int], width: int, height: int, what: str
) -> None:
    """Refuse a rectangle Pillow would silently pad instead of rejecting."""
    if (
        rectangle["x"] < 0
        or rectangle["y"] < 0
        or rectangle["x"] + rectangle["w"] > width
        or rectangle["y"] + rectangle["h"] > height
    ):
        raise ValueError(
            f"{what} of {rectangle['w']}x{rectangle['h']} at "
            f"({rectangle['x']}, {rectangle['y']}) falls outside its {width}x{height} source, "
            "so the page it describes would be part invented pixels"
        )


def _png_colour_mode(png_bytes: bytes) -> str:
    """The Pillow mode of a PNG this module wrote, read out of its own IHDR.

    Eight bytes rather than a second full decode, and exact: the layout table
    below is the same one `_encode_crop_deterministic` wrote the header from, so
    a record built on this cannot drift from the pixels it describes.
    """
    if png_bytes[:8] != PNG_SIGNATURE or len(png_bytes) < 26:
        raise ValueError("a derivative page was not written as a PNG this module owns")
    layout = (png_bytes[24], png_bytes[25])
    for mode, (bit_depth, colour_type, _samples) in _PNG_LAYOUT.items():
        if (bit_depth, colour_type) == layout:
            return mode
    raise ValueError(
        f"a derivative page declares PNG layout {layout}, which this module never writes"
    )


def _expanded_rotation_size(width: int, height: int, angle_degrees: float) -> tuple[int, int]:
    """The size `Image.rotate(angle, expand=True)` would allocate, without allocating it.

    Restates Pillow's own arithmetic (transform the four corners, take
    `ceil(max) - floor(min)` per axis) so the bound applies before the
    rotation materialises. Multiples of 90 are Pillow's fast paths, which
    transpose rather than transform.
    """
    angle = angle_degrees % 360.0
    if angle in (0.0, 180.0):
        return width, height
    if angle in (90.0, 270.0):
        return height, width
    radians = -math.radians(angle)
    cos = round(math.cos(radians), 15)
    sin = round(math.sin(radians), 15)
    centre_x, centre_y = width / 2.0, height / 2.0
    offset_x = cos * -centre_x + sin * -centre_y + centre_x
    offset_y = -sin * -centre_x + cos * -centre_y + centre_y
    corners = ((0, 0), (width, 0), (width, height), (0, height))
    xs = [cos * x + sin * y + offset_x for x, y in corners]
    ys = [-sin * x + cos * y + offset_y for x, y in corners]
    return (
        math.ceil(max(xs)) - math.floor(min(xs)),
        math.ceil(max(ys)) - math.floor(min(ys)),
    )


def render_triage_derivative(
    source_bytes: bytes,
    *,
    page_index: int,
    part: dict,
) -> tuple[bytes, dict[str, int | str | list[str]]]:
    """Apply closed part-local triage geometry and encode a sealed PNG.

    The caller has already validated the triage row. Keeping the operation
    here fixes its exact order at both the Door and Exemplar boundary:
    frame-region split, part-local crop, clockwise expanded rotation, then the
    part's colour conversion.

    The returned record's `color_mode` is read back from the encoded bytes,
    not the pre-encode image, so a colour mode this encoder cannot carry is
    always an explicit conversion or a refusal, never silently mismatched.
    """
    try:
        region, crop_box, rotation = part["region"], part["crop_box"], part["rotation"]
        colour_mode = part["colour_mode"]
        with Image.open(BytesIO(source_bytes)) as image:
            image.seek(page_index)
            # After `seek`, so the bound checks the frame actually being decoded.
            _refuse_past_pixel_bound(image.width, image.height)
            image.load()
            source_mode = image.mode
            source_bands = list(image.getbands())
            source_width, source_height = image.width, image.height
            if colour_mode == "keep" and source_mode not in ENCODER_LOSSLESS_MODES:
                # `keep` forbids the sample or colour-class conversion this PNG
                # encoder would otherwise perform for an unsupported mode.
                raise ValueError(
                    f"a master in mode {source_mode!r} cannot be sealed under colour_mode "
                    "'keep': the deterministic PNG encoder would convert it. Declare the "
                    "conversion this page needs ('grayscale', 'rgb' or 'bitonal') so it is "
                    "recorded, or submit the frame without a split decision"
                )
            # `Image.crop` pads rather than refuses: a rectangle past the
            # master's edge comes back filled with black. Refused here, the
            # one place both the Door and Exemplar paths pass through.
            _refuse_uncontained_rectangle(
                region, source_width, source_height, "a triage split region"
            )
            _refuse_uncontained_rectangle(crop_box, region["w"], region["h"], "a triage crop box")
            split = image.crop(
                (
                    region["x"],
                    region["y"],
                    region["x"] + region["w"],
                    region["y"] + region["h"],
                )
            )
            cropped = split.crop(
                (
                    crop_box["x"],
                    crop_box["y"],
                    crop_box["x"] + crop_box["w"],
                    crop_box["y"] + crop_box["h"],
                )
            )
            # Pillow's positive degrees are counter-clockwise, while the sealed
            # recipe records clockwise millidegrees.
            angle_degrees = -rotation["rotation_millidegrees"] / 1000
            # `expand=True` grows the canvas to the rotated bounding box, so a
            # crop inside `MAX_PIXELS` before rotation can land far outside it
            # after (a thin strip at 45 degrees can bound-box hundreds of
            # times larger).
            _refuse_past_pixel_bound(
                *_expanded_rotation_size(crop_box["w"], crop_box["h"], angle_degrees)
            )
            rotated = cropped.rotate(
                angle_degrees,
                resample=Image.Resampling.BICUBIC,
                expand=True,
            )
            if colour_mode == "keep":
                rendered = rotated
            elif colour_mode in {"grayscale", "bitonal"}:
                try:
                    grayscale = rotated.convert("L")
                except ValueError:
                    # Pillow refuses LAB -> L but provides LAB -> RGB -> L; the
                    # sealed apply recipe explicitly permits this two-hop path.
                    grayscale = rotated.convert("RGB").convert("L")
                rendered = (
                    grayscale
                    if colour_mode == "grayscale"
                    else grayscale.convert("1", dither=Image.Dither.NONE)
                )
                rendered = _without_colour_profile(rendered)
            elif colour_mode == "rgb":
                rendered = _without_colour_profile(rotated.convert("RGB"))
            else:
                raise ValueError(f"undeclared triage colour mode {colour_mode!r}")
            encoded = encode_image_deterministic(rendered)
            return encoded, {
                "width": rendered.width,
                "height": rendered.height,
                # What the sealed bytes are actually in, read back from them, not
                # what the image was in on the way into the encoder.
                "color_mode": _png_colour_mode(encoded),
                "source_mode": source_mode,
                "source_bands": source_bands,
                "source_width": source_width,
                "source_height": source_height,
            }
    # `EOFError` because this is the one path that seeks: a `page_index` past
    # the last frame raises it, and it descends from `Exception`, not `OSError`.
    except (*_DECODE_FAILURES, EOFError, KeyError, TypeError) as error:
        raise ValueError(f"source frame bytes are not a decodable image ({error})") from error
