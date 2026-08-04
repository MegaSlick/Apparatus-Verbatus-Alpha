"""Structural validators for PNG, JPEG and TIFF: door-private, standard library only.

Spec 03's ruling (settled 2026-08-04, item 5): the door brings a real decoder in,
where decoding is the point, and `common/imaging.py` stays the narrow synthetic-only
codec it always was — it says so in its own docstring, naming this spec as the place
a real one is answered. Pillow and numpy are both the obvious answer and the wrong
one for a project with zero dependencies.

**"Real" here means structural, not photometric.** Each validator walks the real
container far enough to prove the bytes are a genuine, uncorrupted instance of the
format they claim, and reads true geometry off them: PNG chunk CRCs and an inflate
that yields exactly the declared byte count, JPEG marker segments well-formed and
terminating in EOI, TIFF's IFD internally consistent and inside the file. None of
the three reconstructs actual pixels — that is a **named, documented limit, not a
shortcut**. A file that passes here is provably the format it claims; what its
pixels show is not this module's question.

**Every walk is bounded before it begins.** The bytes reaching these validators are
untrusted local input, and a validator that inflates or iterates on numbers a file
declared about itself is a validator an attacker writes the loop counter for. The
limits below are admission policy, not caller options: a source past one of them is
refused, never partially inspected.

Lives beside the door (`pipeline/1_exemplar/`) and is door-private: nothing outside
this stage imports it, so structural inspection only ever happens once, at admission.
"""

import struct
import zlib
from typing import Any, Final, NamedTuple

# Bounds on what may be inspected at all. A source larger than `MAX_SOURCE_BYTES`
# is refused before a validator sees it; a declared geometry past `MAX_DIMENSION`
# or `MAX_PIXELS` is refused before anything is decompressed, so the inflate below
# is always bounded by a number that has already been sanity-checked.
MAX_SOURCE_BYTES: Final = 64 * 1024 * 1024
MAX_DIMENSION: Final = 100_000
MAX_PIXELS: Final = 100_000_000
MAX_PNG_CHUNKS: Final = 10_000
MAX_PNG_DECODED_BYTES: Final = 128 * 1024 * 1024
MAX_TIFF_IFDS: Final = 64


class ImageGeometry(NamedTuple):
    """What a structural validator hands back: the format it proved, and its size."""

    format: str
    width: int
    height: int


class FormatRefusal(ValueError):
    """Bytes claim to be an instance of a format and are not a genuine one.

    A ValueError subclass, not a ContractError: this module has no notion of a
    pipeline run, an artifact, or a stage. The caller turns this into a named
    admission refusal.
    """


PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE: Final = b"\xff\xd8"
TIFF_SIGNATURES: Final = (b"II*\x00", b"MM\x00*")
PDF_SIGNATURE: Final = b"%PDF-"
GIF_SIGNATURES: Final = (b"GIF87a", b"GIF89a")

# ISO base media file format "brand" codes that mark a file as HEIC/HEIF. Detected
# from the `ftyp` box that opens every such container, never from a `.heic` file
# extension — admission is by bytes, and the extension plays no part in what a file
# actually is (harvest Q12/Q14, spec 03's admission-by-bytes invariant).
_HEIC_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1"}
)


def sniff(data: bytes) -> str | None:
    """The format the bytes' own signature claims, or None for none recognized.

    Signature only — this says what a validator should be asked to prove, not that
    the bytes are a valid instance of it. `admission.py` calls the matching
    validator before ever admitting anything.
    """
    if data.startswith(PNG_SIGNATURE):
        return "png"
    if data.startswith(JPEG_SIGNATURE):
        return "jpeg"
    if data[:4] in TIFF_SIGNATURES:
        return "tiff"
    if data.startswith(PDF_SIGNATURE):
        return "pdf"
    if data[:6] in GIF_SIGNATURES:
        return "gif"
    if _is_heic(data):
        return "heic"
    return None


def _is_heic(data: bytes) -> bool:
    """True when the bytes open with an `ftyp` box naming a HEIC/HEIF brand.

    Every ISO-BMFF file (HEIC, HEIF, but also MP4/MOV) opens with a box: a 4-byte
    size, then a 4-byte type. `ftyp` at offset 4 is the type; the brand list that
    follows is what actually distinguishes a HEIC image from an unrelated
    container sharing the same outer shape, so this checks the brand rather than
    stopping at `ftyp`.
    """
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    box_size = struct.unpack(">I", data[:4])[0]
    if box_size < 16 or box_size > len(data):
        return False
    major_brand = data[8:12]
    # minor_version at [12:16], then compatible brands, 4 bytes each, to box_size.
    compatible = [data[i : i + 4] for i in range(16, box_size, 4)]
    return major_brand in _HEIC_BRANDS or any(brand in _HEIC_BRANDS for brand in compatible)


def _geometry(format_name: str, width: Any, height: Any) -> ImageGeometry:
    """Bound a declared geometry before anything is sized or decompressed from it."""
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise FormatRefusal(f"corrupt {format_name.upper()}: a zero or negative dimension")
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise FormatRefusal(
            f"unsupported {format_name.upper()}: {width}x{height} exceeds the admission "
            f"limits ({MAX_DIMENSION} per side, {MAX_PIXELS} pixels)"
        )
    return ImageGeometry(format_name, width, height)


# --- PNG -------------------------------------------------------------------------

_PNG_CHANNELS: Final = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_VALID_BIT_DEPTHS: Final = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
# Adam7: (x offset, y offset, x step, y step) per pass. Interlaced PNGs are
# *accounted for* rather than refused — the per-pass stride arithmetic is exact,
# and a refused page is a page nobody reads (GOALS 1).
_ADAM7: Final = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def validate_png(data: bytes) -> ImageGeometry:
    """Walk every chunk, verify every CRC, and inflate IDAT to its declared size."""
    if not data.startswith(PNG_SIGNATURE):
        raise FormatRefusal("not a PNG: missing signature")

    offset = 8
    chunks = 0
    geometry: ImageGeometry | None = None
    bit_depth = color_type = interlace = None
    idat: list[bytes] = []
    saw_idat = ended_idat = saw_palette = seen_iend = False

    while offset < len(data):
        if len(data) - offset < 12:
            raise FormatRefusal("corrupt PNG: truncated chunk header")
        if chunks >= MAX_PNG_CHUNKS:
            raise FormatRefusal(f"unsupported PNG: more than {MAX_PNG_CHUNKS} chunks")
        (length,) = struct.unpack_from(">I", data, offset)
        end = offset + 12 + length
        if end > len(data):
            raise FormatRefusal("corrupt PNG: chunk data runs past the end of the file")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        (stored_crc,) = struct.unpack_from(">I", data, offset + 8 + length)
        if not _is_png_chunk_type(chunk_type):
            raise FormatRefusal("corrupt PNG: chunk type is not four ASCII letters")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != stored_crc:
            raise FormatRefusal(f"corrupt PNG: chunk {chunk_type!r} fails its own CRC")
        chunks += 1

        if chunks == 1:
            if chunk_type != b"IHDR" or length != 13:
                raise FormatRefusal("corrupt PNG: the file does not open with a 13-byte IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            geometry = _geometry("png", width, height)
            if compression != 0 or filtering != 0:
                raise FormatRefusal("unsupported PNG: unknown compression or filter method")
            if color_type not in _PNG_VALID_BIT_DEPTHS:
                raise FormatRefusal(f"corrupt PNG: unknown color type {color_type}")
            if bit_depth not in _PNG_VALID_BIT_DEPTHS[color_type]:
                raise FormatRefusal(
                    f"corrupt PNG: bit depth {bit_depth} is not valid for color type {color_type}"
                )
            if interlace not in (0, 1):
                raise FormatRefusal("corrupt PNG: unknown interlace method")
        elif chunk_type == b"IHDR":
            raise FormatRefusal("corrupt PNG: more than one IHDR")
        elif chunk_type == b"PLTE":
            if saw_palette or saw_idat:
                raise FormatRefusal("corrupt PNG: palette is repeated or arrives after image data")
            _validate_png_palette(chunk_data, color_type, bit_depth)
            saw_palette = True
        elif chunk_type == b"IDAT":
            if ended_idat:
                raise FormatRefusal("corrupt PNG: IDAT chunks are not consecutive")
            if color_type == 3 and not saw_palette:
                raise FormatRefusal("corrupt PNG: indexed image carries no palette")
            saw_idat = True
            idat.append(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or end != len(data) or not saw_idat:
                raise FormatRefusal("corrupt PNG: malformed IEND, or trailing bytes after it")
            seen_iend = True
            break
        else:
            if _is_png_critical(chunk_type):
                raise FormatRefusal(
                    f"unsupported PNG: unknown critical chunk {chunk_type!r} cannot be ignored"
                )
            if saw_idat:
                ended_idat = True
        offset = end

    if not seen_iend:
        raise FormatRefusal("corrupt PNG: no IEND; truncated file")
    if geometry is None or color_type is None or bit_depth is None or interlace is None:
        raise FormatRefusal("corrupt PNG: no IHDR")

    expected = _png_inflated_size(geometry, color_type, bit_depth, interlace)
    if expected > MAX_PNG_DECODED_BYTES:
        raise FormatRefusal("unsupported PNG: declared image data exceeds the admission limit")
    raw = _inflate_exactly(b"".join(idat), expected)
    _validate_png_filter_bytes(raw, geometry, color_type, bit_depth, interlace)
    return geometry


def _inflate_exactly(compressed: bytes, expected: int) -> bytes:
    """Inflate at most one byte beyond a declared size, flush included.

    `decompress(..., max_length)` alone is not a bound: an unbounded `flush()`
    afterwards emits the rest of a compression bomb regardless. Both calls get
    the same `expected + 1` budget, leaving exactly one byte of headroom to tell
    "the right length" from "longer than declared" without ever holding the
    overrun in memory.
    """
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(compressed, expected + 1)
        if len(raw) <= expected:
            raw += inflater.flush(expected + 1 - len(raw))
    except zlib.error as error:
        raise FormatRefusal(f"corrupt PNG: image data would not decompress ({error})") from error
    if len(raw) > expected:
        raise FormatRefusal("corrupt PNG: image data expands past its own declared size")
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise FormatRefusal("corrupt PNG: image data stream is truncated or has trailing bytes")
    if len(raw) != expected:
        raise FormatRefusal("corrupt PNG: decompressed data has the wrong length")
    return raw


def _png_rows(geometry: ImageGeometry, color_type: int, bit_depth: int, interlace: int):
    """(row count, bytes per row) for each pass — one pass when not interlaced."""
    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth
    passes = ((0, 0, 1, 1),) if interlace == 0 else _ADAM7
    for x, y, step_x, step_y in passes:
        width = 0 if geometry.width <= x else (geometry.width - x + step_x - 1) // step_x
        height = 0 if geometry.height <= y else (geometry.height - y + step_y - 1) // step_y
        if width and height:
            yield height, (width * bits_per_pixel + 7) // 8


def _png_inflated_size(
    geometry: ImageGeometry, color_type: int, bit_depth: int, interlace: int
) -> int:
    return sum(
        height * (1 + row_bytes)
        for height, row_bytes in _png_rows(geometry, color_type, bit_depth, interlace)
    )


def _validate_png_filter_bytes(
    raw: bytes, geometry: ImageGeometry, color_type: int, bit_depth: int, interlace: int
) -> None:
    """Check every scanline's filter selector without reconstructing pixels."""
    cursor = 0
    for height, row_bytes in _png_rows(geometry, color_type, bit_depth, interlace):
        for _ in range(height):
            if raw[cursor] > 4:
                raise FormatRefusal(
                    f"corrupt PNG: a scanline carries unknown filter type {raw[cursor]}"
                )
            cursor += 1 + row_bytes
    if cursor != len(raw):
        raise FormatRefusal("corrupt PNG: scanlines do not account for the decompressed data")


def _validate_png_palette(payload: bytes, color_type: int | None, bit_depth: int | None) -> None:
    if color_type not in (2, 3, 6) or bit_depth is None:
        raise FormatRefusal("corrupt PNG: a palette is not legal for this color type")
    if not payload or len(payload) % 3 or len(payload) > 256 * 3:
        raise FormatRefusal("corrupt PNG: palette has an invalid number of entries")
    if color_type == 3 and len(payload) // 3 > 1 << bit_depth:
        raise FormatRefusal("corrupt PNG: palette has more entries than its bit depth can index")


def _is_png_chunk_type(kind: bytes) -> bool:
    return len(kind) == 4 and all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind)


def _is_png_critical(kind: bytes) -> bool:
    return not bool(kind[0] & 0x20)


# --- JPEG --------------------------------------------------------------------------

# Start-of-frame markers that carry geometry. 0xC4 (DHT), 0xC8 (JPG extension,
# never produced by an encoder), and 0xCC (DAC) are excluded on purpose: they sit
# inside the same numeric run but are not frame headers.
_JPEG_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def validate_jpeg(data: bytes) -> ImageGeometry:
    """Walk marker segments to an EOI that ends the file, reading geometry off SOF.

    Entropy-coded scan data is skipped by scanning for the next unstuffed marker
    rather than parsed, because Huffman decoding is pixel reconstruction and this
    validator's job stops at "well-formed, framed, scanned, and terminating in
    EOI at the end of the file". Trailing bytes after EOI are refused: they are
    either a second concatenated image or an appended payload, and neither is the
    single page the door believes it admitted.
    """
    if not data.startswith(JPEG_SIGNATURE):
        raise FormatRefusal("not a JPEG: missing SOI")

    geometry: ImageGeometry | None = None
    frame_components: int | None = None
    saw_sos = False
    marker, cursor = _jpeg_marker_at(data, 2)

    while True:
        if marker == 0xD9:  # EOI
            if geometry is None:
                raise FormatRefusal("corrupt JPEG: no start-of-frame marker; no geometry to read")
            if not saw_sos:
                raise FormatRefusal("corrupt JPEG: EOI before any scan")
            if cursor != len(data):
                raise FormatRefusal("corrupt JPEG: trailing bytes after EOI")
            return geometry
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            raise FormatRefusal(f"corrupt JPEG: standalone marker 0xFF{marker:02X} is misplaced")
        if cursor + 2 > len(data):
            raise FormatRefusal("corrupt JPEG: truncated segment length")
        (length,) = struct.unpack_from(">H", data, cursor)
        if length < 2 or cursor + length > len(data):
            raise FormatRefusal(f"corrupt JPEG: marker 0xFF{marker:02X} segment runs past EOF")
        payload = data[cursor + 2 : cursor + length]
        cursor += length

        if marker in _JPEG_SOF_MARKERS:
            if geometry is not None:
                raise FormatRefusal("corrupt JPEG: a second start-of-frame marker")
            if len(payload) < 6:
                raise FormatRefusal("corrupt JPEG: SOF segment too short to carry geometry")
            height, width, components = struct.unpack_from(">HHB", payload, 1)
            if not 1 <= components <= 4:
                raise FormatRefusal("corrupt JPEG: SOF declares no usable component count")
            if len(payload) != 6 + 3 * components:
                raise FormatRefusal("corrupt JPEG: SOF component count disagrees with its length")
            geometry = _geometry("jpeg", width, height)
            frame_components = components

        if marker == 0xDA:  # SOS: entropy-coded data follows, scan past it
            if geometry is None or frame_components is None:
                raise FormatRefusal("corrupt JPEG: a scan precedes its frame header")
            scan_components = payload[0] if payload else 0
            if (
                not 1 <= scan_components <= frame_components
                or len(payload) != 1 + 2 * scan_components + 3
            ):
                raise FormatRefusal("corrupt JPEG: scan components disagree with the segment")
            saw_sos = True
            marker, cursor = _jpeg_marker_after_entropy(data, cursor)
        else:
            marker, cursor = _jpeg_marker_at(data, cursor)


def _jpeg_marker_at(data: bytes, cursor: int) -> tuple[int, int]:
    if cursor >= len(data) or data[cursor] != 0xFF:
        raise FormatRefusal(f"corrupt JPEG: expected a marker at byte {cursor}")
    while cursor < len(data) and data[cursor] == 0xFF:
        cursor += 1
    if cursor >= len(data) or data[cursor] == 0:
        raise FormatRefusal("corrupt JPEG: marker is truncated, or stuffed outside a scan")
    return data[cursor], cursor + 1


def _jpeg_marker_after_entropy(data: bytes, cursor: int) -> tuple[int, int]:
    while cursor < len(data):
        if data[cursor] != 0xFF:
            cursor += 1
            continue
        cursor += 1
        while cursor < len(data) and data[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(data):
            raise FormatRefusal("corrupt JPEG: scan data ends in a marker prefix")
        marker = data[cursor]
        cursor += 1
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            continue  # a stuffed byte or an in-scan restart marker: still scan data
        return marker, cursor
    raise FormatRefusal("corrupt JPEG: truncated scan data, no terminating marker")


# --- TIFF ----------------------------------------------------------------------

_TIFF_TYPE_SIZES: Final = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
}
_TIFF_TAG_IMAGE_WIDTH: Final = 256
_TIFF_TAG_IMAGE_LENGTH: Final = 257
# Where the actual samples live. A TIFF that names none of these declares an image
# with no image data, and one whose ranges leave the file is not an instance of the
# format it claims however tidy its header looks.
_TIFF_STRIP_TAGS: Final = (273, 279)
_TIFF_TILE_TAGS: Final = (324, 325)
_TIFF_DATA_TAGS: Final = frozenset(_TIFF_STRIP_TAGS + _TIFF_TILE_TAGS)


def validate_tiff(data: bytes) -> ImageGeometry:
    """Walk the IFD chain, refusing an entry whose value escapes the file.

    Classic (32-bit offset) TIFF only: BigTIFF is a documented limit, refused by
    name. So is multi-page TIFF — a second image directory declaring its own
    geometry is more than one page in one file, and the door assigns one ordinal
    per page, so it must not silently seal only the first.
    """
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        raise FormatRefusal("not a TIFF: missing byte-order header")
    endian = "<" if data[:2] == b"II" else ">"
    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic != 42:
        raise FormatRefusal(
            "unsupported TIFF: not classic TIFF (BigTIFF or an unknown magic is a documented limit)"
        )
    (offset,) = struct.unpack_from(endian + "I", data, 4)

    seen_offsets: set[int] = set()
    width = height = None
    image_data: dict[int, list[int]] = {}
    for _ in range(MAX_TIFF_IFDS):
        if offset == 0:
            break
        if offset in seen_offsets:
            raise FormatRefusal("corrupt TIFF: the IFD chain contains a cycle")
        if offset + 2 > len(data):
            raise FormatRefusal("corrupt TIFF: IFD offset falls outside the file")
        seen_offsets.add(offset)
        (count,) = struct.unpack_from(endian + "H", data, offset)
        table_end = offset + 2 + count * 12 + 4
        if table_end > len(data):
            raise FormatRefusal("corrupt TIFF: IFD entries run past the end of the file")

        for index in range(count):
            entry = offset + 2 + index * 12
            tag, field_type, value_count = struct.unpack_from(endian + "HHI", data, entry)
            size = _TIFF_TYPE_SIZES.get(field_type)
            if size is None:
                raise FormatRefusal(f"corrupt TIFF: entry names unknown field type {field_type}")
            if value_count > len(data) // size:
                raise FormatRefusal(f"corrupt TIFF: tag {tag} declares more values than the file")
            value_size = value_count * size
            value_field = data[entry + 8 : entry + 12]
            if value_size > 4:
                (value_offset,) = struct.unpack(endian + "I", value_field)
                if value_offset + value_size > len(data):
                    raise FormatRefusal(f"corrupt TIFF: tag {tag} value escapes the file bounds")
                value_bytes = data[value_offset : value_offset + value_size]
            else:
                value_bytes = value_field[:value_size]

            if tag in (_TIFF_TAG_IMAGE_WIDTH, _TIFF_TAG_IMAGE_LENGTH):
                value = _tiff_dimension(tag, value_bytes, field_type, value_count, endian)
                if tag == _TIFF_TAG_IMAGE_WIDTH:
                    if width is not None:
                        raise FormatRefusal(
                            "unsupported TIFF: a second image directory declares its own "
                            "geometry; multi-page TIFF is a documented limit"
                        )
                    width = value
                else:
                    if height is not None:
                        raise FormatRefusal(
                            "unsupported TIFF: a second image directory declares its own "
                            "geometry; multi-page TIFF is a documented limit"
                        )
                    height = value
            if tag in _TIFF_DATA_TAGS:
                if tag in image_data:
                    raise FormatRefusal(f"corrupt TIFF: image-data tag {tag} appears twice")
                image_data[tag] = _tiff_unsigned_values(
                    value_bytes, field_type, value_count, endian
                )
        (offset,) = struct.unpack_from(endian + "I", data, table_end - 4)
    else:
        raise FormatRefusal(f"unsupported TIFF: more than {MAX_TIFF_IFDS} image directories")

    if width is None or height is None:
        raise FormatRefusal("corrupt TIFF: no ImageWidth/ImageLength tag")
    _validate_tiff_image_data_ranges(data, image_data)
    return _geometry("tiff", width, height)


def _tiff_dimension(tag: int, value: bytes, field_type: int, count: int, endian: str) -> int:
    """A dimension is one SHORT or one LONG. A `count` of anything else leaves
    `value` the wrong length for the unpack, and a bare `struct.error` is not a
    named refusal — this is the fail-closed check that keeps a malformed count a
    refusal the door can name rather than a crash that aborts every other source
    still waiting to be decided."""
    if count != 1 or field_type not in (3, 4):
        raise FormatRefusal(f"corrupt TIFF: tag {tag} is not one SHORT or LONG")
    return struct.unpack(endian + ("H" if field_type == 3 else "I"), value)[0]


def _tiff_unsigned_values(value: bytes, field_type: int, count: int, endian: str) -> list[int]:
    """Decode strip/tile positions without touching a byte of pixel data."""
    if not count or field_type not in (3, 4):
        raise FormatRefusal("corrupt TIFF: image-data offsets or counts are not unsigned integers")
    unit = 2 if field_type == 3 else 4
    if len(value) != count * unit:
        raise FormatRefusal("corrupt TIFF: image-data value length disagrees with its IFD entry")
    return list(struct.unpack(endian + ("H" if field_type == 3 else "I") * count, value))


def _validate_tiff_image_data_ranges(data: bytes, image_data: dict[int, list[int]]) -> None:
    """Require one complete, in-file strip or tile inventory for the image."""
    strips = [image_data.get(tag) for tag in _TIFF_STRIP_TAGS]
    tiles = [image_data.get(tag) for tag in _TIFF_TILE_TAGS]
    if any(part is not None for part in strips) and any(part is not None for part in tiles):
        raise FormatRefusal("corrupt TIFF: both strip and tile image-data inventories are named")
    offsets, counts = strips if any(part is not None for part in strips) else tiles
    if offsets is None and counts is None:
        raise FormatRefusal("corrupt TIFF: no strip or tile image-data inventory")
    if offsets is None or counts is None or len(offsets) != len(counts):
        raise FormatRefusal("corrupt TIFF: image-data offsets and byte counts do not reconcile")
    for offset, count in zip(offsets, counts, strict=True):
        if offset < 8 or count <= 0 or offset + count > len(data):
            raise FormatRefusal("corrupt TIFF: an image-data range falls outside the file")


VALIDATORS: Final = {"png": validate_png, "jpeg": validate_jpeg, "tiff": validate_tiff}


def validate(format_name: str, data: bytes) -> ImageGeometry:
    """Dispatch to the validator for a sniffed format; refuse anything else."""
    try:
        validator = VALIDATORS[format_name]
    except KeyError:
        raise FormatRefusal(f"no structural validator for format {format_name!r}") from None
    return validator(data)
