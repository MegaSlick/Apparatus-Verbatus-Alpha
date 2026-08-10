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
"""

import struct
import zlib
from io import BytesIO
from typing import TypedDict

import pillow_heif
from PIL import Image, UnidentifiedImageError

pillow_heif.register_heif_opener()

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_COLOR_TYPE_GRAYSCALE = 0
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
    if width * height > MAX_PIXELS:
        raise ValueError(
            f"a {width}x{height} page is past this pipeline's {MAX_PIXELS}-pixel bound"
        )

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
    return encode_grayscale_png(w, h, [row[x : x + w] for row in rows[y : y + h]])


def dimensions(png_bytes: bytes) -> tuple[int, int]:
    """The dimensions of a sealed page, including RGB PNG renders from the door."""
    try:
        width, height, _ = decode_grayscale_png(png_bytes)
        return width, height
    except ValueError:
        try:
            with Image.open(BytesIO(png_bytes)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError(
                        f"a {image.width}x{image.height} page is past this pipeline's "
                        f"{MAX_PIXELS}-pixel bound"
                    )
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
            if image.width * image.height > MAX_PIXELS:
                raise ValueError(
                    f"a {image.width}x{image.height} page is past this pipeline's "
                    f"{MAX_PIXELS}-pixel bound"
                )
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


def _crop_decoded_page(png_bytes: bytes, x: int, y: int, w: int, h: int) -> bytes:
    """Crop a decoded page and encode a PNG-compatible, display-ready result.

    PNG cannot represent CMYK or several decoder-private modes.  The sealed crop
    is a display image for later stages, so non-alpha modes become RGB and alpha
    modes become RGBA; no transparent pixel is flattened against an invented
    background.  The original Exemplar blob remains untouched and traceable.
    """
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError(
                    f"a {image.width}x{image.height} page is past this pipeline's "
                    f"{MAX_PIXELS}-pixel bound"
                )
            image.load()
            if x < 0 or y < 0 or x + w > image.width or y + h > image.height:
                raise ValueError(
                    f"crop bounds {{'x': {x}, 'y': {y}, 'w': {w}, 'h': {h}}} fall outside a "
                    f"{image.width}x{image.height} page"
                )
            crop = image.crop((x, y, x + w, y + h))
            if crop.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                crop = _to_display_mode(crop)
            output = BytesIO()
            crop.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
    except _DECODE_FAILURES as error:
        raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error


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
        return crop.point(lambda value: value * scale).convert("L")
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
    has_alpha = any(band.upper() == "A" for band in crop.getbands())
    return crop.convert("RGBA" if has_alpha else "RGB")
