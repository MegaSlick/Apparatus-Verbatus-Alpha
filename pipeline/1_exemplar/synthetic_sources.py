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


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg_dqt(*table_ids: int) -> bytes:
    """A DQT segment defining one 8-bit quantization table per id given."""
    return jpeg_segment(
        0xDB, b"".join(bytes([identifier]) + bytes([16]) * 64 for identifier in table_ids)
    )


def jpeg_dht(*tables: tuple[int, int]) -> bytes:
    """A DHT segment defining one minimal Huffman table per (class, id) given.

    One code of length one and one symbol: structurally complete, which is exactly
    what `validate_jpeg` proves and the limit of what it claims — the entropy stream
    is not decoded, here or there.
    """
    payload = b"".join(
        bytes([(table_class << 4) | identifier]) + bytes([1] + [0] * 15) + bytes([0])
        for table_class, identifier in tables
    )
    return jpeg_segment(0xC4, payload)


def jpeg(
    width: int = 5,
    height: int = 4,
    *,
    sof_marker: int = 0xC0,
    scan: bytes = b"\x12\x34\x56",
    eoi: bool = True,
    trailing: bytes = b"",
    components: int = 1,
    quantization_tables: tuple[int, ...] | None = (0,),
    huffman_tables: tuple[tuple[int, int], ...] | None = ((0, 0), (1, 0)),
    spectral_start: int = 0,
) -> bytes:
    """A structurally complete JPEG: SOI, DQT, DHT, SOF, SOS, entropy data, EOI.

    The tables are parameters so a test can build a JPEG *missing* one and prove the
    refusal. They default to present because a JPEG without them is not a file any
    decoder could read, and this builder is what the suite means by "genuine": it
    used to emit SOI/SOF/SOS/EOI and three arbitrary bytes, and the validator
    admitted those 30 bytes as a 5x4 image.
    """
    tables = b""
    if quantization_tables:
        tables += jpeg_dqt(*quantization_tables)
    if huffman_tables:
        tables += jpeg_dht(*huffman_tables)
    frame = struct.pack(">BHHB", 8, height, width, components)
    frame += b"".join(bytes([index + 1, 0x11, 0]) for index in range(components))
    sos_payload = bytes([components])
    sos_payload += b"".join(bytes([index + 1, 0x00]) for index in range(components))
    sos_payload += bytes([spectral_start, 63, 0])
    return (
        b"\xff\xd8"
        + tables
        + jpeg_segment(sof_marker, frame)
        + jpeg_segment(0xDA, sos_payload)
        + scan
        + (b"\xff\xd9" if eoi else b"")
        + trailing
    )


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
    bits_per_sample: int = 8,
    compression: int = 1,
    photometric: int | None = 1,
    strip_bytes: int | None = None,
    next_ifd: int = 0,
) -> bytes:
    """A classic single-page TIFF whose stored strip really holds its own image.

    One strip covering every row, sized `height * ceil(width * bits / 8)` and
    actually present in the file. The shipped fixture used to declare 6x5 pixels
    behind a **one-byte** strip and be admitted as genuine, so "the strip is in the
    file" was the whole of what the validator could check. `strip_bytes` overrides
    the size so a test can build the mismatch and prove the refusal.

    Entries are emitted in ascending tag order, as TIFF requires, and ImageWidth is
    always first — `tiff_ifd_entry_offset` and `tiff_next_ifd_offset` give a test
    the two positions it needs without hard-coding an entry count that moves.
    """
    endian = "<" if little_endian else ">"
    order = b"II" if little_endian else b"MM"

    def dimension(value: int) -> bytes:
        if tag_type == 3:
            return struct.pack(endian + "H", value) + b"\x00\x00"
        return struct.pack(endian + "I", value)

    def short(tag: int, value: int) -> bytes:
        return (
            struct.pack(endian + "HHI", tag, 3, 1) + struct.pack(endian + "H", value) + b"\x00\x00"
        )

    inventory_tags = (273, 279) if strips else ()
    baseline = [(258, bits_per_sample), (259, compression)]
    if photometric is not None:
        baseline.append((262, photometric))
    entry_count = 2 + len(baseline) + len(inventory_tags) + 2 + extra_entry_count
    image_offset = 8 + 2 + entry_count * 12 + 4
    stored = height * ((width * bits_per_sample + 7) // 8) if strip_bytes is None else strip_bytes

    entries = struct.pack(endian + "HHI", 256, tag_type, 1) + dimension(width)
    entries += struct.pack(endian + "HHI", 257, tag_type, 1) + dimension(height)
    for tag, value in baseline:
        entries += short(tag, value)
    if strips:
        entries += struct.pack(endian + "HHI", 273, 4, 1) + struct.pack(endian + "I", image_offset)
    # 277 SamplesPerPixel and 278 RowsPerStrip sit between 273 and 279 in tag order.
    entries += short(277, 1)
    entries += struct.pack(endian + "HHI", 278, 4, 1) + struct.pack(endian + "I", height)
    if strips:
        entries += struct.pack(endian + "HHI", 279, 4, 1) + struct.pack(endian + "I", stored)
    entries += extra_entries
    body = struct.pack(endian + "H", entry_count) + entries + struct.pack(endian + "I", next_ifd)
    header = order + struct.pack(endian + "H", magic) + struct.pack(endian + "I", 8)
    return header + body + bytes(max(1, stored))


def tiff_ifd_entry_offset(index: int) -> int:
    """Where entry `index` of the first IFD starts. Entry 0 is always ImageWidth."""
    return 8 + 2 + index * 12


def tiff_next_ifd_offset(data: bytes, *, little_endian: bool = True) -> int:
    """Where the first IFD's next-directory pointer sits, whatever its entry count."""
    endian = "<" if little_endian else ">"
    (count,) = struct.unpack_from(endian + "H", data, 8)
    return 8 + 2 + count * 12


def gif() -> bytes:
    """Enough GIF bytes to exercise a named decoder-gap route in a test."""
    return b"GIF89a" + b"\x00" * 12


def heic() -> bytes:
    """An ISO-BMFF `ftyp` header declaring HEIC for decoder-gap tests."""
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


def content_page_pdf(
    content: bytes,
    *,
    width: int = 144,
    height: int = 72,
    images: list[dict] | None = None,
    rotate: int | None = None,
) -> bytes:
    """One page whose complete display list is authored by a test.

    Images are exposed as ``/Im0``, ``/Im1`` and so on. ``/F1`` is a standard
    Helvetica resource, letting tests paint synthetic text without checking a
    font file into the repository. The content stream remains caller-owned so a
    test can place text, vectors and images independently and prove that the
    whole page, rather than one convenient object, was rasterised.
    """
    builder = PdfBuilder()
    image_numbers = [
        builder.add(stream_object(entry["dictionary"], entry["raw"])) for entry in (images or [])
    ]
    font = builder.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    contents = builder.add(stream_object("<<", content))
    catalog = builder.add()
    pages = builder.add()
    names = " ".join(f"/Im{index} {number} 0 R" for index, number in enumerate(image_numbers))
    rotation = f" /Rotate {rotate}" if rotate is not None else ""
    resources = f"<< /Font << /F1 {font} 0 R >> /XObject << {names} >> >>"
    page = builder.add(
        (
            f"<< /Type /Page /Parent {pages} 0 R /Resources {resources} "
            f"/MediaBox [0 0 {width} {height}] /Contents {contents} 0 R{rotation} >>"
        ).encode()
    )
    builder.objects[pages] = f"<< /Type /Pages /Kids [{page} 0 R] /Count 1 >>".encode()
    builder.objects[catalog] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
    return builder.build(catalog)


def form_text_pdf(value: str = "FORM") -> bytes:
    """One synthetic AcroForm text field whose value must be drawn by PDFium.

    The widget is a terminal field on a one-page document, using a built-in font
    and default appearance.  There is intentionally no page content stream that
    paints the value: a render only shows its ink when the PDF form environment
    was initialized and `may_draw_forms` is honored.
    """
    builder = PdfBuilder()
    font = builder.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    catalog = builder.add()
    pages = builder.add()
    form = builder.add()
    widget = builder.add()
    contents = builder.add(stream_object("<<", b""))
    page = builder.add(
        (
            f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 144 72] "
            f"/Resources << /Font << /Helv {font} 0 R >> >> "
            f"/Annots [{widget} 0 R] /Contents {contents} 0 R >>"
        ).encode()
    )
    builder.objects[widget] = (
        f"<< /Type /Annot /Subtype /Widget /FT /Tx /T (field) /Rect [20 20 130 52] "
        f"/V ({value}) /DA (/Helv 18 Tf 0 g) /F 4 /P {page} 0 R >>"
    ).encode()
    builder.objects[form] = (
        f"<< /Fields [{widget} 0 R] /DR << /Font << /Helv {font} 0 R >> >> "
        "/DA (/Helv 18 Tf 0 g) /NeedAppearances true >>"
    ).encode()
    builder.objects[pages] = f"<< /Type /Pages /Kids [{page} 0 R] /Count 1 >>".encode()
    builder.objects[catalog] = (
        f"<< /Type /Catalog /Pages {pages} 0 R /AcroForm {form} 0 R >>"
    ).encode()
    return builder.build(catalog)


def blank_pages_pdf(count: int, *, width: int = 1, height: int = 1) -> bytes:
    """A real page tree of ``count`` empty synthetic pages.

    This intentionally emits every leaf rather than trusting a forged ``/Count``:
    PDFium, not this test writer, decides how many pages the document contains.
    """
    builder = PdfBuilder()
    catalog = builder.add()
    pages = builder.add()
    page_numbers = [
        builder.add(
            f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {width} {height}] >>".encode()
        )
        for _ in range(count)
    ]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    builder.objects[pages] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode()
    )
    builder.objects[catalog] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
    return builder.build(catalog)


def page_tree_bomb_pdf(levels: int, *, fanout: int = 2) -> tuple[bytes, int]:
    """A tiny, hostile page tree: ``fanout ** levels`` declared pages from one leaf.

    Unlike ``blank_pages_pdf``, every ``/Kids`` entry at every level repeats the
    *same* child object reference ``fanout`` times, so the declared page count
    compounds multiplicatively while the object count stays linear in ``levels``.
    This is the shape a real scanned reel can never take -- its declared page
    count grows only with real, distinct page bytes. Returns the bytes and the
    declared page count. ``levels`` must be at least 1.
    """
    builder = PdfBuilder()
    leaf = builder.add()
    level_numbers = [builder.add() for _ in range(levels)]
    catalog = builder.add()

    builder.objects[leaf] = (
        f"<< /Type /Page /Parent {level_numbers[0]} 0 R /MediaBox [0 0 72 72] >>".encode()
    )
    count = 1
    child = leaf
    for index, number in enumerate(level_numbers):
        count *= fanout
        parent = level_numbers[index + 1] if index + 1 < len(level_numbers) else catalog
        kids = " ".join(f"{child} 0 R" for _ in range(fanout))
        builder.objects[number] = (
            f"<< /Type /Pages /Parent {parent} 0 R /Kids [{kids}] /Count {count} >>".encode()
        )
        child = number

    builder.objects[catalog] = f"<< /Type /Catalog /Pages {level_numbers[-1]} 0 R >>".encode()
    return builder.build(catalog), count


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
    repeats the same reference under extra names, which builds a page carrying
    several XObject names so the whole-page render can be proved against it.
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
        commands = (
            f"q {entry['width']} 0 0 {entry['height']} 0 0 cm /Im0 Do Q".encode()
            if xobject_count
            else b""
        )
        contents = builder.add(stream_object("<<", commands))
        page_numbers.append(
            builder.add(
                (
                    f"<< /Type /Page /Parent {pages} 0 R /Resources "
                    f"<< /XObject << {names} >> >> "
                    f"/MediaBox [0 0 {entry['width']} {entry['height']}]"
                    f" /Contents {contents} 0 R{rotate_clause}{extra_page} >>"
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
