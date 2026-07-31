"""Synthetic proof pages: the walking skeleton's only input.

No real register material exists here or may be invented. These are abstract
synthetic marks — filled rectangles and pixel runs standing in for lines of
writing — never simulated handwriting. Everything is deterministic: no
timestamps, no randomness, no font rendering (glyph shapes vary by machine
and by installed font, which would break byte-identical output), and no
floating-point arithmetic in any value that reaches the encoded bytes.

The PNG encoder/decoder pair here is minimal on purpose: 8-bit grayscale,
filter type 0 on every scanline, no interlacing. Pulling in Pillow or numpy
to draw two rectangles would hand the project a dependency it has zero of,
for a task standard library `zlib`/`struct` already do in a few dozen lines.
"""

from __future__ import annotations

import struct
import zlib
from typing import TypedDict

FIXTURE_ID = "synthetic-two-page-v0"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COLOR_TYPE_GRAYSCALE = 0
_BIT_DEPTH = 8
_FILTER_NONE = 0


class Bounds(TypedDict):
    x: int
    y: int
    w: int
    h: int


class Act(TypedDict):
    ordinal: int
    bounds: Bounds
    ink: int


class Page(TypedDict):
    ordinal: int
    width: int
    height: int
    acts: tuple[Act, ...]


# Background is a light gray; each act's ink value is a distinct dark tone so
# the three acts are visibly and byte-wise distinguishable from each other
# and from the page background. Bounds sit inside the page with margin and
# never overlap. Page 2's single act is the geometric continuation of page
# 1's second act — same act ordinal, a fresh rectangle on the next page;
# linking the two into one logical act is a different unit's job.
_BACKGROUND = 230

PAGES: tuple[Page, ...] = (
    {
        "ordinal": 1,
        "width": 200,
        "height": 260,
        "acts": (
            {
                "ordinal": 0,
                "bounds": {"x": 20, "y": 20, "w": 160, "h": 80},
                "ink": 40,
            },
            {
                "ordinal": 1,
                "bounds": {"x": 20, "y": 120, "w": 160, "h": 100},
                "ink": 90,
            },
        ),
    },
    {
        "ordinal": 2,
        "width": 200,
        "height": 260,
        "acts": (
            {
                "ordinal": 1,
                "bounds": {"x": 20, "y": 20, "w": 160, "h": 60},
                "ink": 90,
            },
        ),
    },
)


def _chunk(tag: bytes, data: bytes) -> bytes:
    """Build one length-prefixed, CRC-checked PNG chunk."""
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def _render_rows(width: int, height: int, acts: tuple[Act, ...]) -> list[bytearray]:
    """Paint the background and each act's rectangle into a row buffer.

    Each act is filled with alternating ink/background pixel runs rather than
    a solid block, so a crop of the act is distinguishable pixel-by-pixel
    from a crop of flat fill of the same ink value — this is the "simple
    pixel run standing in for a line of writing" called for by the spec.
    """
    rows = [bytearray([_BACKGROUND]) * width for _ in range(height)]
    for act in acts:
        bounds = act["bounds"]
        ink = act["ink"]
        x0, y0, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
        if x0 < 0 or y0 < 0 or x0 + w > width or y0 + h > height:
            raise ValueError(f"act bounds {bounds} fall outside page {width}x{height}")
        for row_offset in range(h):
            row = rows[y0 + row_offset]
            # A run every 4 rows and a stripe every 5 columns keeps the
            # pattern trivially deterministic and cheap to eyeball in tests.
            if row_offset % 4 < 2:
                for col_offset in range(w):
                    if col_offset % 5 != 0:
                        row[x0 + col_offset] = ink
    return rows


def _encode_grayscale_png(width: int, height: int, rows: list[bytearray]) -> bytes:
    """Encode a list of 8-bit grayscale scanlines as a minimal PNG."""
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
    # level=9 with no other knobs keeps the compressor's output a pure
    # function of the input bytes — nothing timestamp- or platform-derived.
    idat = zlib.compress(bytes(raw), level=9)
    return _PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def render_page(descriptor: Page) -> bytes:
    """Render one page descriptor to deterministic 8-bit grayscale PNG bytes."""
    width, height = descriptor["width"], descriptor["height"]
    rows = _render_rows(width, height, descriptor["acts"])
    return _encode_grayscale_png(width, height, rows)


def page_bytes(ordinal: int) -> bytes:
    """Render the page with the given 1-based ordinal."""
    for page in PAGES:
        if page["ordinal"] == ordinal:
            return render_page(page)
    raise ValueError(f"no page with ordinal {ordinal}")


def _decode_grayscale_png(png_bytes: bytes) -> tuple[int, int, list[bytearray]]:
    """Decode a PNG written by `_encode_grayscale_png` back into rows.

    Refuses anything outside that exact format rather than guessing at a
    fallback: interlaced images, other bit depths or color types, other
    filter types, and truncated or non-PNG input all raise ValueError.
    """
    if png_bytes[:8] != _PNG_SIGNATURE:
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
            raise ValueError("truncated PNG: chunk data runs past end of file")
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
            offset = crc_end
            break

        offset = crc_end

    if not seen_ihdr or width is None or height is None:
        raise ValueError("unsupported PNG: missing IHDR")

    raw = zlib.decompress(bytes(idat))
    stride = width + 1  # one filter-type byte per scanline
    if len(raw) != stride * height:
        raise ValueError("unsupported PNG: decompressed data has the wrong length")

    rows: list[bytearray] = []
    for row_index in range(height):
        row_start = row_index * stride
        filter_type = raw[row_start]
        if filter_type != _FILTER_NONE:
            raise ValueError("unsupported PNG: only filter type 0 is decodable here")
        rows.append(bytearray(raw[row_start + 1 : row_start + stride]))

    return width, height, rows


def crop_png(png_bytes: bytes, bounds: Bounds) -> bytes:
    """Decode a page PNG, cut out `bounds`, and re-encode the rectangle as a PNG."""
    width, height, rows = _decode_grayscale_png(png_bytes)
    x0, y0, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    if w <= 0 or h <= 0:
        raise ValueError(f"crop bounds {bounds} must have positive width and height")
    if x0 < 0 or y0 < 0 or x0 + w > width or y0 + h > height:
        raise ValueError(f"crop bounds {bounds} fall outside page {width}x{height}")

    cropped_rows = [row[x0 : x0 + w] for row in rows[y0 : y0 + h]]
    return _encode_grayscale_png(w, h, cropped_rows)
