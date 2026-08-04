"""Genuine format bytes, built in memory, for this stage's tests.

Not a checked-in binary and not register material. Every source here is assembled
from `struct` and `zlib` at call time, for three reasons: the ingress guard only
admits png/jpeg/tiff media types under `proof/fixtures/`, these tests also need
*corrupt* and PDF/GIF/HEIC bytes which no such fixture may be, and a builder is the
only way to prove a validator against a case that has to be exactly wrong in one
named place.

Shared rather than copied into each test module: the door's tests, the decoder's
tests and the renderer's tests all need the same genuine PNG, and three
hand-maintained copies of it would drift the way the old door's two admission
tables did.
"""

import struct
import zlib
from typing import Final

PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS: Final = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

# Adam7 pass geometry, duplicated here on purpose: a builder that imported the
# validator's own table could only ever agree with it, and then the interlace test
# would be checking the table against itself.
_ADAM7: Final = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png_container(*chunks: tuple[bytes, bytes]) -> bytes:
    """A CRC-valid PNG container of exactly the chunks given, and nothing else."""
    return PNG_MAGIC + b"".join(png_chunk(tag, data) for tag, data in chunks)


def png(
    width: int = 4,
    height: int = 3,
    *,
    bit_depth: int = 8,
    color_type: int = 0,
    interlace: int = 0,
    rows: bytes | None = None,
    extra_chunks: bytes = b"",
    palette: bytes | None = None,
) -> bytes:
    """A genuine PNG of the given shape, with well-formed scanlines throughout."""
    raw = (
        rows
        if rows is not None
        else _png_scanlines(width, height, bit_depth, color_type, interlace)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    body = png_chunk(b"IHDR", ihdr)
    if palette is not None:
        body += png_chunk(b"PLTE", palette)
    elif color_type == 3:
        body += png_chunk(b"PLTE", bytes(3 * min(256, 1 << bit_depth)))
    body += extra_chunks
    return PNG_MAGIC + body + png_chunk(b"IDAT", zlib.compress(raw, 9)) + png_chunk(b"IEND", b"")


def _png_scanlines(
    width: int, height: int, bit_depth: int, color_type: int, interlace: int
) -> bytes:
    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth
    passes = ((0, 0, 1, 1),) if interlace == 0 else _ADAM7
    out = bytearray()
    for x, y, step_x, step_y in passes:
        pass_width = 0 if width <= x else (width - x + step_x - 1) // step_x
        pass_height = 0 if height <= y else (height - y + step_y - 1) // step_y
        if not (pass_width and pass_height):
            continue
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        for _ in range(pass_height):
            out.append(0)  # filter type 0 (None)
            out.extend(b"\x7f" * row_bytes)
    return bytes(out)


def jpeg(
    width: int = 5,
    height: int = 4,
    *,
    sof_marker: int = 0xC0,
    scan: bytes = b"\x12\x34\x56",
    eoi: bool = True,
    trailing: bytes = b"",
) -> bytes:
    """A structurally complete JPEG: SOI, SOF, SOS, entropy data, EOI."""
    sof_content = struct.pack(">BHHB", 8, height, width, 1) + bytes([1, 0x11, 0])
    sof = bytes([0xFF, sof_marker]) + struct.pack(">H", len(sof_content) + 2) + sof_content
    sos_content = bytes([1, 1, 0]) + bytes([0, 63, 0])
    sos = b"\xff\xda" + struct.pack(">H", len(sos_content) + 2) + sos_content
    return b"\xff\xd8" + sof + sos + scan + (b"\xff\xd9" if eoi else b"") + trailing


def tiff(
    width: int = 6,
    height: int = 5,
    *,
    little_endian: bool = True,
    tag_type: int = 3,
    strips: bool = True,
    magic: int = 42,
    extra_entries: bytes = b"",
    extra_entry_count: int = 0,
) -> bytes:
    """A classic TIFF with a bounded, in-file strip inventory for its image data."""
    endian = "<" if little_endian else ">"
    order = b"II" if little_endian else b"MM"

    def dimension(value: int) -> bytes:
        if tag_type == 3:
            return struct.pack(endian + "H", value) + b"\x00\x00"
        return struct.pack(endian + "I", value)

    inventory_tags = (273, 279) if strips else ()
    entry_count = 2 + len(inventory_tags) + extra_entry_count
    image_offset = 8 + 2 + entry_count * 12 + 4
    entries = struct.pack(endian + "HHI", 256, tag_type, 1) + dimension(width)
    entries += struct.pack(endian + "HHI", 257, tag_type, 1) + dimension(height)
    for tag, value in zip(inventory_tags, (image_offset, 1), strict=False):
        entries += struct.pack(endian + "HHI", tag, 4, 1) + struct.pack(endian + "I", value)
    entries += extra_entries
    body = struct.pack(endian + "H", entry_count) + entries + struct.pack(endian + "I", 0)
    return order + struct.pack(endian + "H", magic) + struct.pack(endian + "I", 8) + body + b"\x00"


def gif() -> bytes:
    """Enough of a GIF to be sniffed as one. It is refused by name regardless."""
    return b"GIF89a" + b"\x00" * 12


def heic() -> bytes:
    """An ISO-BMFF `ftyp` box declaring a HEIC brand. Refused by name, by bytes."""
    return struct.pack(">I", 24) + b"ftyp" + b"heic" + b"\x00\x00\x00\x00" + b"mif1" + b"heic"


class PdfBuilder:
    """A minimal classic-xref PDF writer: just enough to prove the renderer."""

    def __init__(self):
        self.objects: dict[int, bytes] = {}
        self._next = 1

    def add(self, body: bytes = b"") -> int:
        number = self._next
        self._next += 1
        self.objects[number] = body
        return number

    def build(self, root_number: int, *, extra_trailer: str = "") -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for number in sorted(self.objects):
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode() + self.objects[number] + b"\nendobj\n"
        xref_offset = len(out)
        highest = max(offsets) if offsets else 0
        out += f"xref\n0 {highest + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for number in range(1, highest + 1):
            out += (
                f"{offsets[number]:010d} 00000 n \n".encode()
                if number in offsets
                else b"0000000000 65535 f \n"
            )
        out += (
            f"trailer\n<< /Size {highest + 1} /Root {root_number} 0 R{extra_trailer} >>\n".encode()
        )
        out += f"startxref\n{xref_offset}\n%%EOF".encode()
        return bytes(out)


def stream_object(dictionary: str, raw: bytes) -> bytes:
    return f"{dictionary} /Length {len(raw)} >>".encode() + b"\nstream\n" + raw + b"\nendstream"


def image_page_pdf(
    images: list[dict],
    *,
    rotate: int | None = None,
    extra_page: str = "",
    xobject_count: int = 1,
) -> bytes:
    """One PDF with one page per entry in `images`, each carrying one image XObject.

    Each entry names `width`, `height`, `dictionary` (the XObject dictionary text
    before `/Length`) and `raw` (the stream bytes). `xobject_count` above one
    repeats the same reference under extra names, which is how the
    more-than-one-XObject refusal is exercised.
    """
    builder = PdfBuilder()
    image_numbers = [
        builder.add(stream_object(entry["dictionary"], entry["raw"])) for entry in images
    ]
    catalog = builder.add()
    pages = builder.add()
    page_numbers = []
    rotate_clause = f" /Rotate {rotate}" if rotate is not None else ""
    for entry, image_number in zip(images, image_numbers, strict=True):
        names = " ".join(f"/Im{index} {image_number} 0 R" for index in range(xobject_count))
        page_numbers.append(
            builder.add(
                (
                    f"<< /Type /Page /Parent {pages} 0 R /Resources "
                    f"<< /XObject << {names} >> >> "
                    f"/MediaBox [0 0 {entry['width']} {entry['height']}]"
                    f"{rotate_clause}{extra_page} >>"
                ).encode()
            )
        )
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    builder.objects[pages] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode()
    )
    builder.objects[catalog] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
    return builder.build(catalog)


def gray_image(width: int, height: int, value: int) -> dict:
    """A FlateDecode DeviceGray image XObject entry for `image_page_pdf`."""
    return {
        "width": width,
        "height": height,
        "dictionary": (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            "/BitsPerComponent 8 /ColorSpace /DeviceGray /Filter /FlateDecode"
        ),
        "raw": zlib.compress(bytes([value]) * (width * height)),
    }


def jpeg_image(width: int, height: int) -> dict:
    """A DCTDecode image XObject entry carrying a genuine embedded JPEG."""
    return {
        "width": width,
        "height": height,
        "dictionary": (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            "/BitsPerComponent 8 /ColorSpace /DeviceGray /Filter /DCTDecode"
        ),
        "raw": jpeg(width, height),
    }


def custom_image(
    width: int,
    height: int,
    *,
    bits: int = 8,
    colorspace: str | None = "DeviceGray",
    filter_name: str | None = "FlateDecode",
    raw: bytes = b"",
    declared_width: int | None = None,
    declared_height: int | None = None,
) -> dict:
    """An image XObject entry with every field the decoder inspects as a parameter."""
    filter_clause = f" /Filter /{filter_name}" if filter_name else ""
    colorspace_clause = f" /ColorSpace /{colorspace}" if colorspace else ""
    return {
        "width": width,
        "height": height,
        "dictionary": (
            f"<< /Type /XObject /Subtype /Image "
            f"/Width {declared_width if declared_width is not None else width} "
            f"/Height {declared_height if declared_height is not None else height} "
            f"/BitsPerComponent {bits}{colorspace_clause}{filter_clause}"
        ),
        "raw": raw,
    }


def single_gray_page_pdf(width: int = 4, height: int = 3, value: int = 7, **kwargs) -> bytes:
    return image_page_pdf([gray_image(width, height, value)], **kwargs)


def two_page_pdf(width: int = 4, height: int = 3) -> bytes:
    return image_page_pdf([gray_image(width, height, 11), gray_image(width, height, 22)])
