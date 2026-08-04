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
                image.load()
                return image.width, image.height
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
            raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error


def _crop_decoded_page(png_bytes: bytes, x: int, y: int, w: int, h: int) -> bytes:
    """Crop a decoded page and encode a PNG-compatible, display-ready result.

    PNG cannot represent CMYK or several decoder-private modes.  The sealed crop
    is a display image for later stages, so non-alpha modes become RGB and alpha
    modes become RGBA; no transparent pixel is flattened against an invented
    background.  The original Exemplar blob remains untouched and traceable.
    """
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            image.load()
            if x < 0 or y < 0 or x + w > image.width or y + h > image.height:
                raise ValueError(
                    f"crop bounds {{'x': {x}, 'y': {y}, 'w': {w}, 'h': {h}}} fall outside a "
                    f"{image.width}x{image.height} page"
                )
            crop = image.crop((x, y, x + w, y + h))
            if crop.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                crop = crop.convert("RGBA" if "A" in crop.getbands() else "RGB")
            output = BytesIO()
            crop.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValueError(f"sealed page bytes are not a decodable image ({error})") from error
