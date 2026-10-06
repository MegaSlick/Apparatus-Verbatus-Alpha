"""A deterministic lossless TIFF writer for 8-bit grey and RGB images.

Pillow's TIFF writer (through libtiff) leaves a pad byte before the image directory
when the compressed data ends on an odd byte, and does not set its value, so two runs
can write different files for the same pixels. Here every byte is written by pagekit:
little-endian, strips of `ROWS_PER_STRIP` rows compressed with zlib (compression 8,
"Adobe deflate"), one directory, every pad byte zero. The bytes depend only on the
pixels and the resolution. With no resolution, no resolution tags are written and the
unit is "none", so readers report no dpi (Pillow's own writer would claim 1 dpi).

The file layout follows the TIFF 6.0 specification (Adobe, 1992). This writer was first
written for the tone view (pagekit/tone.py) and is shared with the prepared pages.
"""

from __future__ import annotations

import struct
import zlib
from fractions import Fraction

from PIL import Image

ROWS_PER_STRIP = 256
_BANDS = {"L": 1, "RGB": 3}


def _rational(number: float) -> tuple[int, int]:
    fraction = Fraction(number).limit_denominator(10000)
    return fraction.numerator, fraction.denominator


def tiff_bytes(image: Image.Image, dpi: list[float] | tuple[float, float] | None = None) -> bytes:
    """`image` (mode L or RGB) as a deflate TIFF whose bytes depend only on the pixels and
    `dpi`."""
    if image.mode not in _BANDS:
        raise ValueError(f"only 8-bit grey or RGB images are written, not {image.mode!r}")
    bands = _BANDS[image.mode]
    width, height = image.size
    raw = image.tobytes()
    row = width * bands
    strips = [
        zlib.compress(raw[start * row : (start + ROWS_PER_STRIP) * row], 6)
        for start in range(0, height, ROWS_PER_STRIP)
    ]
    offsets, position = [], 8
    for strip in strips:
        offsets.append(position)
        position += len(strip)
    directory_offset = position + (position & 1)  # word-aligned, pad byte zero

    def entry(tag: int, kind: int, values: list[int]) -> tuple[bytes, bytes]:
        """The 12-byte directory entry and any data that goes after the directory."""
        count = len(values) if kind != 5 else len(values) // 2
        packed = struct.pack(f"<{len(values)}{'H' if kind == 3 else 'I'}", *values)
        if len(packed) <= 4:
            return struct.pack("<HHI", tag, kind, count) + packed.ljust(4, b"\0"), b""
        return struct.pack("<HHI", tag, kind, count), packed + (b"\0" if len(packed) & 1 else b"")

    fields = [
        (256, 4, [width]),
        (257, 4, [height]),
        (258, 3, [8] * bands),
        (259, 3, [8]),
        (262, 3, [1 if bands == 1 else 2]),
        (273, 4, offsets),
        (277, 3, [bands]),
        (278, 4, [min(height, ROWS_PER_STRIP)]),
        (279, 4, [len(strip) for strip in strips]),
    ]
    if dpi:
        fields += [
            (282, 5, list(_rational(dpi[0]))),
            (283, 5, list(_rational(dpi[1]))),
            (284, 3, [1]),
            (296, 3, [2]),
        ]
    else:
        fields += [(284, 3, [1]), (296, 3, [1])]  # no absolute unit: readers report no dpi
    entries, trailing = [], []
    after_directory = directory_offset + 2 + 12 * len(fields) + 4
    for tag, kind, values in fields:
        head, data = entry(tag, kind, values)
        if data:
            head = head[:8] + struct.pack("<I", after_directory)
            after_directory += len(data)
            trailing.append(data)
        entries.append(head)
    directory = struct.pack("<H", len(fields)) + b"".join(entries) + struct.pack("<I", 0)
    body = b"".join(strips)
    return (
        struct.pack("<2sHI", b"II", 42, directory_offset)
        + body
        + (b"\0" if position & 1 else b"")
        + directory
        + b"".join(trailing)
    )
