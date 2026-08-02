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

No dependency. Pillow and numpy are both the obvious answer and the wrong one for
drawing rectangles into a project that has zero dependencies and intends to keep
its list short.
"""

import struct
import zlib
from typing import TypedDict

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
    """Cut a rectangle out of a page and re-encode it through the same encoder.

    The crop is genuinely derived from the page's pixels rather than described,
    which is what makes ARCHITECTURE's third invariant — the exact image shown to
    a model is reproducible from the Exemplar plus the recorded transforms —
    a property of this code rather than a claim about it.
    """
    width, height, rows = decode_grayscale_png(png_bytes)
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]

    if w <= 0 or h <= 0:
        raise ValueError(f"crop bounds {bounds} must have positive width and height")
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise ValueError(f"crop bounds {bounds} fall outside a {width}x{height} page")

    return encode_grayscale_png(w, h, [row[x : x + w] for row in rows[y : y + h]])


def dimensions(png_bytes: bytes) -> tuple[int, int]:
    """The width and height of a decodable image, for verifying a region reference."""
    width, height, _ = decode_grayscale_png(png_bytes)
    return width, height
