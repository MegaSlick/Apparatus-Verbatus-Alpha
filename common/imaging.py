"""A minimal grayscale PNG codec: encode, decode, crop. One implementation.

Two stages need this, which is the stated condition for code entering `common/`:
the Designator cuts crops from a sealed page, and the Perlector verifies that the
region it was handed is a decodable image of the size the reference claims. The
proof fixtures render through it as well, so the bytes the pipeline reads and the
bytes the fixtures declare come from exactly one encoder — a second copy would be
a second thing to drift.

Deliberately narrow: 8-bit grayscale, filter type 0 on every scanline, no
interlacing. Anything else is refused rather than guessed at. That is not a
limitation to apologize for at this stage — the skeleton's job is wiring and
bookkeeping on synthetic input, and a codec that quietly half-handled a real
photograph would be worse than one that says no. Spec 03 brings a real decoder in
at the door, where decoding is the point.

This module keeps its tiny grayscale codec for deterministic synthetic fixtures.
The project may also use ordinary imaging libraries where full decoding is needed:
Tyrel's ruling permits basic tools such as Pillow and PDFium, and the Exemplar uses
them to preserve real source pages rather than refusing formats for lack of a
decoder.

Reading is Pillow's job wherever this codec cannot do it; *writing* run-time
evidence is not. A crop is content-addressed, so its bytes name its blob path and
every artifact digest above it, and bytes that depend on which zlib a wheel
bundled are not reproducible from the Exemplar plus the record. Every encoder here
that a run writes through is therefore this module's own, framed so that only the
PNG and DEFLATE specifications decide any byte.
"""

import hashlib
import struct
import zlib
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

# The same ceiling the door admits pages under (`pipeline/1_exemplar/image_formats.py`).
# This module accepts bytes directly rather than only sealed pages, so it needs its own
# bound: without one, Pillow's default decompression-bomb ceiling (89,478,485 pixels)
# sits *below* the door's, and a page the door legitimately admitted could raise here.
# Restated rather than imported because `common/` may not import `pipeline/` — the
# import-boundary test in `common/chairs/` enforces that — and the drift is covered by
# a test that reads the door's constant and compares the two.
MAX_PIXELS = 100_000_000

# Pillow refuses above twice `MAX_IMAGE_PIXELS` and warns above it. Raising its
# ceiling past this module's own bound makes this module's refusal the one that
# speaks, with a message naming the page rather than a library's internal limit.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def _refuse_past_pixel_bound(width: int, height: int) -> None:
    """The one place `MAX_PIXELS` is enforced against a pair of dimensions —
    four call sites (the native path, both Pillow-fallback decode paths, and
    the CMYK/mode-conversion crop) needed the identical check and message."""
    if width * height > MAX_PIXELS:
        raise ValueError(
            f"a {width}x{height} page is past this pipeline's {MAX_PIXELS}-pixel bound"
        )


# Pillow's bomb error descends from `Exception`, not `ValueError`, so neither
# decoding path below caught it and it escaped past this module's stated contract
# that an undecodable page raises `ValueError`.
_DECODE_FAILURES = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    Image.DecompressionBombError,
)

# Modes whose samples are wider than 8 bits per channel. `convert("RGB")` does not
# scale them: Pillow maps the value straight through, so 1024 and 65535 both land on
# 255 and a 16-bit scan comes out clipped to near-white. Scaled explicitly instead,
# by the mode's own declared range rather than by the page's own maximum — a per-image
# maximum would make the same ink a different grey on a different page.
_HIGH_PRECISION_SCALE = {"I;16": 1 / 257, "I;16L": 1 / 257, "I;16B": 1 / 257, "I;16N": 1 / 257}


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

    The ordinary encoder's output is pure only for a given zlib build — Pillow's
    Linux wheels bundle a different zlib than macOS ships, and a run-time blob
    whose bytes depend on the wheel renames its content-addressed path and every
    artifact digest downstream of it. Evidence written *during* a run therefore
    uses this encoder: filter 0 and a stored-block DEFLATE stream, every byte
    fixed by the PNG and DEFLATE specifications. The cost is size — stored
    blocks do not compress — which is acceptable for the bounded renders and
    crops a run writes, and wrong for checked-in fixtures, so both encoders
    exist.
    """
    if len(rows) != height:
        raise ValueError(f"expected {height} scanlines, got {len(rows)}")
    if any(len(row) != width for row in rows):
        raise ValueError("a scanline is not the declared width")

    return _deterministic_png(width, height, _BIT_DEPTH, _COLOR_TYPE_GRAYSCALE, rows)


# Pillow mode -> (PNG bit depth, PNG colour type, samples per pixel). Exactly the
# modes a crop can still be in once `_crop_decoded_page` has put it into one PNG
# can hold, and no others: an unlisted mode is refused rather than guessed at, the
# same way the decoder refuses an encoding it does not own. `P` is deliberately
# absent — see `_encode_crop_deterministic`.
_PNG_LAYOUT: Final = {
    "1": (1, _COLOR_TYPE_GRAYSCALE, 1),
    "L": (_BIT_DEPTH, _COLOR_TYPE_GRAYSCALE, 1),
    "LA": (_BIT_DEPTH, _COLOR_TYPE_GRAYSCALE_ALPHA, 2),
    "RGB": (_BIT_DEPTH, _COLOR_TYPE_TRUECOLOR, 3),
    "RGBA": (_BIT_DEPTH, _COLOR_TYPE_TRUECOLOR_ALPHA, 4),
}

# What Pillow's own PNG writer names an embedded colour profile.
_ICC_PROFILE_NAME: Final = b"ICC Profile"


def _encode_crop_deterministic(crop: Image.Image) -> bytes:
    """Write a decoded crop as a PNG no library gets to choose the bytes of.

    Pillow's PNG writer is a good encoder and the wrong one for evidence. Its
    wheels bundle their own zlib, so `crop.save(..., compress_level=9)` emits a
    different valid stream on a different build of the same version — and a crop
    is content-addressed, so those bytes name its blob path, its region record's
    `image_sha256`, and every artifact digest above it. The run-tree store is
    write-once, so a resumed run that re-cut the same crop under a different
    wheel would not merely differ, it would refuse to publish. This is the same
    reason the Perlector's page-context render already goes through this
    module's deterministic encoder rather than Pillow's.

    Pixels, alpha and colour profile are carried over exactly; only the framing
    changes. A crop whose mode had to change colour class on the way here has
    already lost its profile in `_to_display_mode`, which is where that decision
    belongs — this encoder writes whatever it is handed.
    """
    profile = crop.info.get("icc_profile")
    if crop.mode == "P":
        # Expanded to true colour rather than re-serialised as a palette. PNG can
        # hold PLTE and tRNS, but writing them here would make this module a
        # second place that decides what a Pillow palette means — the int-versus-
        # bytes transparency form, a palette shorter than the largest index in
        # use, an RGBA palette — and this module exists so there is exactly one
        # such place. The expansion is lossless in every rendered pixel; it costs
        # bytes on an indexed page and buys no second palette semantics to drift.
        #
        # Both places alpha can hide are asked, not only the usual one. A decoded
        # PNG or GIF puts its tRNS in `info["transparency"]`; a palette that
        # carries alpha in the palette itself would otherwise convert to RGB and
        # lose it silently, which is the one outcome this branch may not have.
        keeps_alpha = "transparency" in crop.info or "A" in getattr(crop.palette, "mode", "RGB")
        crop = crop.convert("RGBA" if keeps_alpha else "RGB")
    layout = _PNG_LAYOUT.get(crop.mode)
    if layout is None:
        raise ValueError(f"a crop in mode {crop.mode!r} has no defined deterministic PNG layout")
    bit_depth, color_type, samples = layout
    width, height = crop.width, crop.height
    # Pillow packs `tobytes()` in exactly PNG's own scanline layout for each of
    # these modes, including the byte-aligned rows of a bilevel image, so no
    # repacking is needed — only the assertion that it really did.
    stride = (width * samples * bit_depth + 7) // 8
    data = crop.tobytes()
    if len(data) != stride * height:
        raise ValueError(
            f"a {crop.mode!r} crop packed {len(data)} bytes where PNG declares {stride * height}"
        )
    ancillary = b""
    if isinstance(profile, bytes) and profile:
        # Carried, not dropped. Pillow's own writer copies `icc_profile` into the
        # crop today, and a crop that quietly lost its colour profile is a
        # different image to anything that honours one — which is not an encoding
        # change. Compressed by this module's stored-block deflate for the same
        # reason the pixels are.
        ancillary = _chunk(
            b"iCCP",
            _ICC_PROFILE_NAME + b"\x00\x00" + _deterministic_stored_deflate(profile),
        )
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

    Interlaced images, other bit depths or colour types, other filter types, and
    truncated or non-PNG input all raise ValueError. A decoder that fell back to a
    best guess would hand a stage pixels nobody can account for.
    """
    if png_bytes[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG: missing signature")

    offset = 8
    width = height = None
    idat = bytearray()
    seen_ihdr = False

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

        if tag == b"IHDR":
            if length != 13:
                raise ValueError("unsupported PNG: malformed IHDR")
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if bit_depth != _BIT_DEPTH or color_type != _COLOR_TYPE_GRAYSCALE:
                raise ValueError("unsupported PNG: only 8-bit grayscale is decodable here")
            if interlace != 0:
                raise ValueError("unsupported PNG: interlaced images are not decodable here")
            seen_ihdr = True
        elif tag == b"IDAT":
            idat.extend(data)
        elif tag == b"IEND":
            break

        offset = crc_end

    if not seen_ihdr or width is None or height is None:
        raise ValueError("unsupported PNG: missing IHDR")

    # Bounded before `expected` is even computed, not only before the
    # decompression that follows it. `stride * height` below is itself an
    # attacker-declared number: an IHDR naming an enormous width/height turns
    # it into a multi-gigabyte `max_length` that a small, highly compressible
    # IDAT can actually fill, which is the same decompression-bomb shape the
    # length check after decompression exists to catch, just reached by
    # inflating the bound rather than the output. The Pillow-fallback paths
    # below (`dimensions`, `grayscale_rows`) already refuse past this same
    # ceiling before decoding; this native path claimed the identical bound
    # in its own module comment but never enforced it.
    _refuse_past_pixel_bound(width, height)

    # Bound the decompression before it happens, not after. `zlib.decompress` on
    # attacker-shaped input will materialize whatever the stream expands to, so a
    # few hundred bytes of IDAT can become gigabytes in memory and the length
    # check below never gets to run. The header already tells us exactly how many
    # bytes a valid image must produce, so ask for one more than that and refuse
    # anything that keeps going.
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
    # A truncated stream can return exactly `expected` bytes without raising, so
    # the length check alone would pass it. `eof` is what distinguishes a complete
    # stream from one that merely got far enough.
    if not decompressor.eof:
        raise ValueError("corrupt PNG: image data stream is truncated")
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

    The crop is genuinely derived from the page's pixels rather than described,
    which is what makes ARCHITECTURE's third invariant — the exact image shown to
    a model is reproducible from the Exemplar plus the recorded transforms —
    a property of this code rather than a claim about it.

    Both paths encode deterministically. A crop is evidence written during a run
    and it is content-addressed, so its bytes may not depend on which zlib a
    wheel happened to bundle: see `encode_grayscale_png_deterministic` and
    `_encode_crop_deterministic`.
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
    255 (white) -- the one place outside `decode_grayscale_png` that reads pixel
    VALUES rather than only dimensions (`dimensions`) or a cropped rectangle of
    them (`crop_png`).

    The fast path is this module's own lossless codec, so a synthetic fixture
    page decodes through exactly the same reader that verifies its crops. Real
    imagery -- anything this module's own encoder never wrote -- falls back to
    Pillow's own grayscale conversion, under the same `MAX_PIXELS` bound and the
    same decode failures as `dimensions`. A third hand-rolled decode-and-fallback
    pair here, beside the two `dimensions` and `crop_png` already carry, is
    exactly the drift this module exists to prevent.
    """
    try:
        return decode_grayscale_png(png_bytes)
    except ValueError:
        pass
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            _refuse_past_pixel_bound(image.width, image.height)
            image.load()
            grayscale = image if image.mode == "L" else image.convert("L")
            width, height = grayscale.width, grayscale.height
            data = grayscale.tobytes()
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

    Two encodings of one image are equal here and two different images are not,
    whatever bit depth, colour type, palette or compression stream each was
    written with. This is the comparison ARCHITECTURE's third invariant actually
    asks for — "the exact *image* shown to a model is reproducible from the
    Exemplar plus the recorded transforms". Comparing the bytes instead asserts
    something stronger and unrelated: that two encoders agreed. They are the same
    assertion only while one build writes both sides, which is exactly the
    condition a pod, a CI matrix, or a resumed run breaks.

    RGBA rather than each mode's own layout, because the two sides may
    legitimately hold the same picture in different modes — an indexed crop and
    its expansion, a bilevel crop and its 8-bit twin. The profile is part of the
    identity: dropping or swapping it changes how the samples are meant to be
    read, which is not an encoding difference.
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


# Every tag here either declares the pixels or says how to interpret them, and
# each is a small fixed record. Anything else — a text chunk, an EXIF block, a
# private tag — is payload riding inside an image rather than part of it.
# Exactly the chunks this pipeline's own encoders can produce, and no other.
# The wider PNG metadata family (sRGB, gAMA, cHRM, sBIT, bKGD, pHYs) changes
# how pixels are *rendered* without changing the samples `image_shown`
# compares — an allowed-but-uncompared chunk would be a channel for a crop
# that passes the pixel identity and still displays differently. Neither the
# deterministic encoder nor the previous zlib/Pillow paths emitted any of
# them for run evidence, so refusing them costs no legitimate crop.
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

    A caller that accepts two encodings of one image as equal has stopped
    asserting anything about the bytes, and "the pixels match" says nothing about
    a text chunk or a block of trailing bytes travelling beside them. This says
    what the byte comparison used to say and the pixel comparison does not: that
    the file is the picture and no more.

    Its own walk rather than `decode_grayscale_png`'s, which decodes one narrow
    encoding and raises a named ValueError per fault. This one must return a
    verdict for any PNG at all, including every colour type that decoder refuses,
    and a malformed structure is a `False` here rather than an error. Chunk CRCs
    are not rechecked: whoever calls this has already decoded the image.
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
            if x < 0 or y < 0 or x + w > image.width or y + h > image.height:
                raise ValueError(
                    f"crop bounds {{'x': {x}, 'y': {y}, 'w': {w}, 'h': {h}}} fall outside a "
                    f"{image.width}x{image.height} page"
                )
            crop = image.crop((x, y, x + w, y + h))
            if crop.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                crop = _to_display_mode(crop)
            return _encode_crop_deterministic(crop)
    except _DECODE_FAILURES as error:
        raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error


def _without_colour_profile(crop: Image.Image) -> Image.Image:
    """Drop an ICC profile the conversion just made false.

    Pillow copies `info` across `convert()`, so a CMYK page's CMYK profile
    arrives attached to RGB samples and a 16-bit scan's profile survives a
    rescale that changed its tone response. Either one is a *worse* record than
    no profile: anything honouring it re-interprets pixels through a
    transformation describing an image that no longer exists, and the crop is
    content-addressed, so the wrong bytes become the region's `image_sha256`.

    The class-preserving hops keep theirs — a palette expanded to true colour,
    `La` to `LA`, `RGBa` to `RGBA` — because those change the storage of the
    same colours and leave the profile exactly as true as it was.
    """

    crop.info.pop("icc_profile", None)
    return crop


def _to_display_mode(crop: Image.Image) -> Image.Image:
    """Convert a crop to a PNG-representable mode without crushing its samples.

    The door seals `I`, `F` and the `I;16*` family losslessly as TIFF rather than
    clipping them (`pipeline/1_exemplar/image_formats.py`), so those modes really do
    arrive here — this is not a hypothetical branch. A bare `convert("RGB")` maps
    their samples straight through instead of scaling, which puts every value above
    255 on 255: a 16-bit scan came out as near-white, and the crop a model was asked
    to read held none of the ink the page held.
    """
    mode = crop.mode
    scale = _HIGH_PRECISION_SCALE.get(mode)
    if scale is not None:
        # Scaled by the mode's declared range, not by this page's own maximum:
        # a per-image maximum would render identical ink as a different grey on a
        # page that happened to contain a brighter pixel somewhere else.
        #
        # No `int()` inside the expression. On the `I` family Pillow probes the
        # callable with an `ImagePointTransform` to compile it into a scale/offset
        # pair, and `int()` raises a `TypeError` on that probe rather than on any
        # pixel — a plain multiply is what it can compile, and `convert("L")` does
        # the truncation to 8 bits.
        # Rescaled samples: the profile described the 16-bit tone response and
        # no longer describes these bytes.
        return _without_colour_profile(crop.point(lambda value: value * scale).convert("L"))
    if mode in {"I", "F"}:
        # Genuinely undecided rather than quietly guessed. `I` is unbounded signed
        # integer and `F` is float: neither declares a range, so any mapping to 8
        # bits is a policy choice about what black and white mean, and this module
        # is not the place that gets to make it. Refused by name so it surfaces as
        # an alarm (ruling 2) instead of a silently flattened reading.
        raise ValueError(
            f"a sealed page in mode {mode!r} has no defined sample range to display, and "
            "cropping it to 8 bits would decide one silently; the door keeps these modes "
            "losslessly and the value-range policy for reading them is not settled"
        )
    # Premultiplied alpha first, because Pillow will not convert it to anything
    # else: `La` converts *only* to `LA`, and `RGBa` only to `RGBA`. The band name
    # is the tell and it is spelled in lower case, so `"A" in bands` read `La` as
    # having no alpha at all and asked for RGB — which does not merely drop the
    # channel, it raises "conversion from La to L not supported" out of a helper
    # whose caller catches OSError and friends but not that. One hop to the straight
    # -alpha counterpart lands in a mode PNG can hold, with the channel intact.
    unpremultiplied = {"La": "LA", "RGBa": "RGBA"}.get(mode)
    if unpremultiplied is not None:
        return crop.convert(unpremultiplied)
    # Everything reaching here changes colour class — CMYK, YCbCr, LAB and HSV
    # are the modes the caller's allowed set leaves for this branch — so the
    # profile that described the source space describes nothing about the result.
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

    Door page derivatives are evidence just as Designator crops are.  Pillow is
    used to decode and transform their pixels, but it must not choose the PNG
    framing: wheel-bundled zlib differences would otherwise rename identical
    derivative pages on another host.  This is deliberately the same encoder
    used by :func:`crop_png`.
    """
    rendered = image.copy()
    if rendered.mode not in ENCODER_LOSSLESS_MODES:
        rendered = _to_display_mode(rendered)
    return _encode_crop_deterministic(rendered)


def imaging_library_versions() -> dict[str, str]:
    """The decoder versions that can change a derivative page's pixels.

    One place, so the recipe a Door render records and the drift a boundary
    reports can never name different numbers. `pillow_heif` is imported inside
    the call rather than at module scope: every stage in the tree imports this
    module, and only the two callers here need the HEIF decoder resolved.
    """
    import pillow_heif

    return {
        "renderer": "Pillow",
        "renderer_version": Image.__version__,
        "pillow_heif_version": pillow_heif.__version__,
        "libheif_version": pillow_heif.libheif_info()["libheif"],
    }


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


def render_triage_derivative(
    source_bytes: bytes,
    *,
    page_index: int,
    part: dict,
) -> tuple[bytes, dict[str, int | str]]:
    """Apply Unit 5's closed part-local geometry and encode a sealed PNG.

    The caller has already validated the Unit 5 row.  Keeping the operation
    here nevertheless makes the exact order executable at both the Door and
    the Exemplar boundary: frame-region split, part-local crop, clockwise
    expanded rotation, then the part's colour conversion.

    The returned record describes the master and the bytes that were actually
    written — the master's own mode and bands, its full frame size, and the mode
    the encoded PNG is in.  Reporting the *pre-encode* mode was how a 16-bit
    master could be sealed as an 8-bit page under a record that still said
    ``I;16``; a colour mode this encoder cannot carry is now either an explicitly
    declared conversion or a refusal, never a silent one.
    """
    try:
        region, crop_box, rotation = part["region"], part["crop_box"], part["rotation"]
        colour_mode = part["colour_mode"]
        with Image.open(BytesIO(source_bytes)) as image:
            image.seek(page_index)
            image.load()
            source_mode = image.mode
            source_bands = list(image.getbands())
            source_width, source_height = image.width, image.height
            if colour_mode == "keep" and source_mode not in ENCODER_LOSSLESS_MODES:
                # `keep` is Unit 5 declaring that no colour conversion happens.
                # This encoder cannot honour that for a high-precision or
                # non-RGB-class master: `_to_display_mode` would crush `I;16`
                # samples to 8 bits under a record claiming nothing converted.
                # Refused by name, with the remedy in the message, rather than
                # degrading evidence quietly (GOVERNANCE 2, GOVERNANCE 4).
                raise ValueError(
                    f"a master in mode {source_mode!r} cannot be sealed under colour_mode "
                    "'keep': the deterministic PNG encoder would convert it. Declare the "
                    "conversion this page needs ('grayscale', 'rgb' or 'bitonal') so it is "
                    "recorded, or submit the frame without a split decision"
                )
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
            # Pillow's positive degrees are counter-clockwise.  Unit 5 records
            # clockwise millidegrees, so the sign inversion is part of the
            # sealed implementation rather than an implicit convention.
            rotated = cropped.rotate(
                -rotation["rotation_millidegrees"] / 1000,
                resample=Image.Resampling.BICUBIC,
                expand=True,
            )
            if colour_mode == "keep":
                rendered = rotated
            elif colour_mode == "grayscale":
                try:
                    rendered = rotated.convert("L")
                except ValueError:
                    # Pillow exposes a direct LAB -> RGB conversion but refuses
                    # LAB -> L.  An explicitly declared grayscale conversion is
                    # still executable through that owned conversion path; the
                    # two-hop fallback is part of the sealed apply recipe below,
                    # not an implicit decoder choice.
                    rendered = rotated.convert("RGB").convert("L")
                rendered = _without_colour_profile(rendered)
            elif colour_mode == "rgb":
                rendered = _without_colour_profile(rotated.convert("RGB"))
            elif colour_mode == "bitonal":
                try:
                    grayscale = rotated.convert("L")
                except ValueError:
                    grayscale = rotated.convert("RGB").convert("L")
                rendered = _without_colour_profile(grayscale.convert("1", dither=Image.Dither.NONE))
            else:  # The Unit 5 schema normally makes this unreachable.
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
    except (*_DECODE_FAILURES, KeyError, TypeError) as error:
        raise ValueError(f"source frame bytes are not a decodable image ({error})") from error
