"""Structural checks and decoder-backed raster helpers for the door.

Spec 03's ruling (settled 2026-08-04, item 5): the door uses a real decoder where
decoding is the point. Pillow supplies ordinary raster support, while the structural
walkers below keep catching malformed common containers before a later stage could
mistake them for a page. A decoder gap is an alarm about this pipeline, never a
format policy that rejects the submitted image.

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
import warnings
import zlib
from contextlib import contextmanager
from enum import Enum
from io import BytesIO
from typing import Any, Final, NamedTuple

import pillow_heif
from PIL import Image, UnidentifiedImageError

pillow_heif.register_heif_opener()


# Bounds on what may be inspected at all. A source larger than `MAX_SOURCE_BYTES`
# is refused before a validator sees it; a declared geometry past `MAX_DIMENSION`
# or `MAX_PIXELS` is refused before anything is decompressed, so the inflate below
# is always bounded by a number that has already been sanity-checked.
MAX_SOURCE_BYTES: Final = 64 * 1024 * 1024
MAX_DIMENSION: Final = 100_000
MAX_PIXELS: Final = 100_000_000
MAX_PNG_CHUNKS: Final = 10_000
MAX_PNG_DECODED_BYTES: Final = 128 * 1024 * 1024
MAX_TIFF_DATA_SEGMENTS: Final = 100_000
# One scanned register volume is hundreds of pages, not hundreds of thousands.
# `MAX_TIFF_PAGES` bounds the classic-TIFF directory chain walk; `MAX_RASTER_FRAMES`
# bounds whatever any decoder reports, for the containers that never reach that walk.
MAX_TIFF_PAGES: Final = 5_000
MAX_RASTER_FRAMES: Final = 5_000


class ImageGeometry(NamedTuple):
    """What a structural validator hands back: the format it proved, and its size."""

    format: str
    width: int
    height: int


class FormatVerdict(str, Enum):
    """Why bytes did not yield a page, independent of exception wording."""

    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    UNRECOGNIZED = "unrecognized"


class FormatRefusal(ValueError):
    """Typed decoder alarm whose presentation text is not its control flow.

    A ValueError subclass, not a ContractError: this module has no notion of a
    pipeline run, an artifact, or a stage. The caller turns this into a named
    admission refusal.
    """

    def __init__(self, verdict: FormatVerdict, detail: str):
        self.verdict = verdict
        self.detail = detail
        super().__init__(f"{verdict.value} {detail}")


def corrupt(detail: str) -> FormatRefusal:
    return FormatRefusal(FormatVerdict.CORRUPT, detail)


def unsupported(detail: str) -> FormatRefusal:
    return FormatRefusal(FormatVerdict.UNSUPPORTED, detail)


def unrecognized(detail: str) -> FormatRefusal:
    return FormatRefusal(FormatVerdict.UNRECOGNIZED, detail)


PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE: Final = b"\xff\xd8"
# Classic TIFF and BigTIFF alike. BigTIFF is TIFF — the same tags and the same
# image, with 64-bit offsets — and "TIFF 100% must work" does not carve it out.
# It is sniffed here rather than left to the unknown-magic fallback so that a
# large archival scan is admitted *as a TIFF, by name*, instead of by the accident
# of no signature matching: the structural walker below cannot read its 64-bit
# offset table and says so, and the decoder answers instead.
TIFF_SIGNATURES: Final = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
PDF_SIGNATURE: Final = b"%PDF-"
# PDFium accepts a PDF header after a bounded transport preamble. The Door reads
# this much for a path-backed source, so the route stays byte-based while a
# preamble cannot make an otherwise readable document look unrecognized.
PDF_HEADER_PREFIX_BYTES: Final = 1024
GIF_SIGNATURES: Final = (b"GIF87a", b"GIF89a")
BMP_SIGNATURES: Final = (b"BM", b"BA", b"CI", b"CP", b"IC", b"PT")
WEBP_SIGNATURE: Final = b"WEBP"

# The one table `sniff()` walks, so the list of formats the door can detect is
# *derived* from what the sniffer executes rather than hand-copied beside it. A
# second hand-kept list is the same shape of drift as two admission tables: the
# policy-coverage check would compare the copy against the copy and agree.
_SIGNATURES: Final = (
    ("png", (PNG_SIGNATURE,)),
    ("jpeg", (JPEG_SIGNATURE,)),
    ("tiff", TIFF_SIGNATURES),
    ("pdf", (PDF_SIGNATURE,)),
    ("gif", GIF_SIGNATURES),
    ("bmp", BMP_SIGNATURES),
)

# ISO base media file format "brand" codes that mark a file as HEIC/HEIF. Detected
# from the `ftyp` box that opens every such container, never from a `.heic` file
# extension — admission is by bytes, and the extension plays no part in what a file
# actually is (harvest Q12/Q14, spec 03's admission-by-bytes invariant).
_HEIC_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
)
_AVIF_BRANDS: Final = frozenset({b"avif", b"avis"})
_HEIF_BRANDS: Final = frozenset({b"mif1", b"msf1"})


def sniff(data: bytes) -> str | None:
    """The format the bytes' own signature claims, or None for none recognized.

    Signature only — this says what a validator should be asked to prove, not that
    the bytes are a valid instance of it. `admission.py` calls the matching
    validator before ever admitting anything.
    """
    # Unlike raster signatures, PDFium accepts this header within a bounded
    # leading transport preamble. PDFium still validates the document before a
    # page is admitted.
    if PDF_SIGNATURE in data[:PDF_HEADER_PREFIX_BYTES]:
        return "pdf"
    for name, signatures in _SIGNATURES:
        if any(data.startswith(signature) for signature in signatures):
            return name
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == WEBP_SIGNATURE:
        return "webp"
    iso_format = _iso_bmff_image_format(data)
    if iso_format is not None:
        return iso_format
    return None


def _iso_bmff_image_format(data: bytes) -> str | None:
    """Name a HEIC, generic HEIF, or AVIF container from its own brands.

    Every ISO-BMFF file (HEIC, HEIF, but also MP4/MOV) opens with a box: a 4-byte
    size, then a 4-byte type. `ftyp` at offset 4 is the type; the brand list that
    follows is what actually distinguishes a HEIC image from an unrelated
    container sharing the same outer shape, so this checks the brand rather than
    stopping at `ftyp`.
    """
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None
    box_size = struct.unpack(">I", data[:4])[0]
    major_brand = data[8:12]
    readable_end = min(max(box_size, 12), len(data))
    brands = {major_brand} | {data[offset : offset + 4] for offset in range(16, readable_end, 4)}
    if brands & _HEIC_BRANDS:
        return "heic"
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & _HEIF_BRANDS:
        return "heif"
    return None


def _validate_iso_bmff_image_header(data: bytes, format_name: str) -> None:
    """Catch an objectively truncated or mis-sized opening `ftyp` box."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        raise corrupt(f"{format_name.upper()}: truncated ISO-BMFF file-type box")
    box_size = struct.unpack(">I", data[:4])[0]
    if box_size < 16 or box_size > len(data) or box_size % 4:
        raise corrupt(f"{format_name.upper()}: malformed ISO-BMFF file-type box size")


def _geometry(format_name: str, width: Any, height: Any) -> ImageGeometry:
    """Bound a declared geometry before anything is sized or decompressed from it."""
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise corrupt(f"{format_name.upper()}: a zero or negative dimension")
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise unsupported(
            f"{format_name.upper()}: {width}x{height} exceeds the admission "
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
        raise corrupt("PNG: missing signature")

    offset = 8
    chunks = 0
    geometry: ImageGeometry | None = None
    bit_depth = color_type = interlace = None
    idat: list[bytes] = []
    saw_idat = ended_idat = saw_palette = seen_iend = False

    while offset < len(data):
        if len(data) - offset < 12:
            raise corrupt("PNG: truncated chunk header")
        if chunks >= MAX_PNG_CHUNKS:
            raise unsupported(f"PNG: more than {MAX_PNG_CHUNKS} chunks")
        (length,) = struct.unpack_from(">I", data, offset)
        end = offset + 12 + length
        if end > len(data):
            raise corrupt("PNG: chunk data runs past the end of the file")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        (stored_crc,) = struct.unpack_from(">I", data, offset + 8 + length)
        if not _is_png_chunk_type(chunk_type):
            raise corrupt("PNG: chunk type is not four ASCII letters with the reserved bit clear")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != stored_crc:
            raise corrupt(f"PNG: chunk {chunk_type!r} fails its own CRC")
        chunks += 1

        if chunks == 1:
            if chunk_type != b"IHDR" or length != 13:
                raise corrupt("PNG: the file does not open with a 13-byte IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            geometry = _geometry("png", width, height)
            if compression != 0 or filtering != 0:
                raise unsupported("PNG: unknown compression or filter method")
            if color_type not in _PNG_VALID_BIT_DEPTHS:
                raise corrupt(f"PNG: unknown color type {color_type}")
            if bit_depth not in _PNG_VALID_BIT_DEPTHS[color_type]:
                raise corrupt(
                    f"PNG: bit depth {bit_depth} is not valid for color type {color_type}"
                )
            if interlace not in (0, 1):
                raise corrupt("PNG: unknown interlace method")
        elif chunk_type == b"IHDR":
            raise corrupt("PNG: more than one IHDR")
        elif chunk_type == b"PLTE":
            if saw_palette or saw_idat:
                raise corrupt("PNG: palette is repeated or arrives after image data")
            _validate_png_palette(chunk_data, color_type, bit_depth)
            saw_palette = True
        elif chunk_type == b"IDAT":
            if ended_idat:
                raise corrupt("PNG: IDAT chunks are not consecutive")
            if color_type == 3 and not saw_palette:
                raise corrupt("PNG: indexed image carries no palette")
            saw_idat = True
            idat.append(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat:
                raise corrupt("PNG: malformed IEND")
            # IEND ends PNG's datastream. Scanner padding or appended metadata is
            # retained just as JPEG suffix bytes are retained; it is not evidence
            # that the pixels before the terminal chunk were damaged.
            seen_iend = True
            break
        else:
            if _is_png_critical(chunk_type):
                raise unsupported(f"PNG: unknown critical chunk {chunk_type!r} cannot be ignored")
            if saw_idat:
                ended_idat = True
        offset = end

    if not seen_iend:
        raise corrupt("PNG: no IEND; truncated file")
    if geometry is None or color_type is None or bit_depth is None or interlace is None:
        raise corrupt("PNG: no IHDR")

    expected = _png_inflated_size(geometry, color_type, bit_depth, interlace)
    if expected > MAX_PNG_DECODED_BYTES:
        raise unsupported("PNG: declared image data exceeds the admission limit")
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
        raise corrupt(f"PNG: image data would not decompress ({error})") from error
    if len(raw) > expected:
        raise corrupt("PNG: image data expands past its own declared size")
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise corrupt("PNG: image data stream is truncated or has trailing bytes")
    if len(raw) != expected:
        raise corrupt("PNG: decompressed data has the wrong length")
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
                raise corrupt(f"PNG: a scanline carries unknown filter type {raw[cursor]}")
            cursor += 1 + row_bytes
    if cursor != len(raw):
        raise corrupt("PNG: scanlines do not account for the decompressed data")


def _validate_png_palette(payload: bytes, color_type: int | None, bit_depth: int | None) -> None:
    if color_type not in (2, 3, 6) or bit_depth is None:
        raise corrupt("PNG: a palette is not legal for this color type")
    if not payload or len(payload) % 3 or len(payload) > 256 * 3:
        raise corrupt("PNG: palette has an invalid number of entries")
    if color_type == 3 and len(payload) // 3 > 1 << bit_depth:
        raise corrupt("PNG: palette has more entries than its bit depth can index")


def _is_png_chunk_type(kind: bytes) -> bool:
    """Four ASCII letters, with PNG's reserved bit clear.

    Byte 3's case is the *reserved* bit, and the specification requires it to be
    uppercase in this version of the format. Checking only "four letters" accepted a
    chunk typed `abcd` — a name no conforming encoder can emit — as an ordinary
    ancillary chunk to be skipped, which is a claim of "genuine, uncorrupted
    instance" this module makes and was not performing.
    """
    if len(kind) != 4 or not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind):
        return False
    return not kind[2] & 0x20


def _is_png_critical(kind: bytes) -> bool:
    return not bool(kind[0] & 0x20)


# --- JPEG --------------------------------------------------------------------------

# Start-of-frame markers that carry geometry. 0xC4 (DHT), 0xC8 (JPG extension,
# never produced by an encoder), and 0xCC (DAC) are excluded on purpose: they sit
# inside the same numeric run but are not frame headers.
_JPEG_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
# Which of those code progressively, and which replace the Huffman tables with
# arithmetic conditioning. Both change *which* tables a scan is required to have
# defined, so the scan check has to know them apart rather than demanding one shape.
_JPEG_PROGRESSIVE_MARKERS: Final = frozenset({0xC2, 0xC6, 0xCA, 0xCE})
_JPEG_ARITHMETIC_MARKERS: Final = frozenset({0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
# Lossless frames code DC coefficients only and legally define no AC table at all.
# Demanding one of them would refuse a conforming file — a page nobody reads, for a
# check that was never owed, which is the exact regression a widened validator has
# to avoid being.
_JPEG_LOSSLESS_MARKERS: Final = frozenset({0xC3, 0xC7, 0xCB, 0xCF})


def validate_jpeg(data: bytes, *, expected_components: int | None = None) -> ImageGeometry:
    """Walk marker segments to an EOI, reading geometry off SOF.

    **What is proven, exactly.** The file is framed by SOI and a terminating EOI at
    the very end; every marker segment's declared length lies inside the file; there
    is exactly one start-of-frame, whose component count agrees with its own segment
    length; every DQT, DHT and DAC segment's own internal lengths add up; and — the
    part this validator used to claim and not perform — **every quantization and
    Huffman table a scan selects has actually been defined by a preceding DQT or
    DHT**. A frame whose components name a quantization table nobody defined, or a
    scan whose components name a Huffman table nobody defined, is not a decodable
    JPEG and is refused. Before that check, the project's own synthetic "genuine"
    JPEG — SOI, SOF, SOS, three arbitrary bytes, EOI, no tables at all — was admitted
    as a 5x4 image, which no decoder on earth could have turned into pixels.

    **An arithmetic-coded frame's conditioning is checked for shape and not
    reconciled**, because a DAC segment is optional — the default conditioning is
    legal — so there is no selector that must have been defined. Saying "DQT, DHT or
    DAC" here would be claiming a reconciliation that does not happen, which is the
    defect this docstring exists downstream of.

    **What is not proven.** The entropy-coded scan data is skipped by scanning for
    the next unstuffed marker rather than Huffman-decoded, because that is pixel
    reconstruction. So this proves the container and its table references are whole
    and mutually consistent; it does not prove the compressed samples decode, and it
    does not claim to. That is the same named, documented limit the module docstring
    states for all three formats.

    Trailing bytes after EOI are retained. Some scanners append metadata or padding;
    the EOI still closes the JPEG image and this door must not call that normal form
    corrupted merely because it carries bytes after it.

    `expected_components` is the colour-space reconciliation the PDF renderer needs —
    the number of components the *container around this JPEG* declares. A mismatch
    means the embedded stream is not the image its dictionary describes.
    """
    if not data.startswith(JPEG_SIGNATURE):
        raise corrupt("JPEG: missing SOI")

    geometry: ImageGeometry | None = None
    frame_components: list[tuple[int, int]] = []  # (component id, quantization table id)
    progressive = arithmetic = lossless = False
    quantization_tables: set[int] = set()
    huffman_tables: set[tuple[int, int]] = set()  # (class, id): class 0 = DC, 1 = AC
    arithmetic_conditioning: set[tuple[int, int]] = set()
    saw_sos = False
    marker, cursor = _jpeg_marker_at(data, 2)

    while True:
        if marker == 0xD9:  # EOI
            if geometry is None:
                raise corrupt("JPEG: no start-of-frame marker; no geometry to read")
            if not saw_sos:
                raise corrupt("JPEG: EOI before any scan")
            return geometry
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            raise corrupt(f"JPEG: standalone marker 0xFF{marker:02X} is misplaced")
        if cursor + 2 > len(data):
            raise corrupt("JPEG: truncated segment length")
        (length,) = struct.unpack_from(">H", data, cursor)
        if length < 2 or cursor + length > len(data):
            raise corrupt(f"JPEG: marker 0xFF{marker:02X} segment runs past EOF")
        payload = data[cursor + 2 : cursor + length]
        cursor += length

        if marker == 0xDB:  # DQT
            quantization_tables |= _jpeg_quantization_tables(payload)
        elif marker == 0xC4:  # DHT
            huffman_tables |= _jpeg_huffman_tables(payload)
        elif marker == 0xCC:  # DAC: arithmetic coding replaces the Huffman tables
            arithmetic_conditioning |= _jpeg_arithmetic_conditioning(payload)
        elif marker in _JPEG_SOF_MARKERS:
            if geometry is not None:
                raise corrupt("JPEG: a second start-of-frame marker")
            if len(payload) < 6:
                raise corrupt("JPEG: SOF segment too short to carry geometry")
            height, width, components = struct.unpack_from(">HHB", payload, 1)
            if components < 1:
                raise corrupt("JPEG: SOF declares no component at all")
            if components > 4:
                # T.81 permits up to 255; four is the baseline/JFIF convention and
                # the limit of what anything downstream here handles. A genuine
                # instance of a variant this door does not decode is "unsupported",
                # not "corrupt" — the two words are different facts and
                # `admission._refusal_code` turns them into different reasons.
                raise unsupported(
                    f"JPEG: {components} components is past the four this "
                    "door decodes; more is a documented limit"
                )
            if len(payload) != 6 + 3 * components:
                raise corrupt("JPEG: SOF component count disagrees with its length")
            if expected_components is not None and components != expected_components:
                raise corrupt(
                    f"JPEG: the frame declares {components} component(s), but the "
                    f"container around it declares {expected_components}"
                )
            geometry = _geometry("jpeg", width, height)
            progressive = marker in _JPEG_PROGRESSIVE_MARKERS
            arithmetic = marker in _JPEG_ARITHMETIC_MARKERS
            lossless = marker in _JPEG_LOSSLESS_MARKERS
            frame_components = [
                (payload[6 + 3 * index], payload[8 + 3 * index]) for index in range(components)
            ]

        if marker == 0xDA:  # SOS: entropy-coded data follows, scan past it
            if geometry is None or not frame_components:
                raise corrupt("JPEG: a scan precedes its frame header")
            _validate_jpeg_scan(
                payload,
                frame_components=frame_components,
                progressive=progressive,
                arithmetic=arithmetic,
                lossless=lossless,
                quantization_tables=quantization_tables,
                huffman_tables=huffman_tables,
                arithmetic_conditioning=arithmetic_conditioning,
            )
            saw_sos = True
            marker, cursor = _jpeg_marker_after_entropy(data, cursor)
        else:
            marker, cursor = _jpeg_marker_at(data, cursor)


# --- GIF -------------------------------------------------------------------------


def _gif_skip_sub_blocks(data: bytes, offset: int, *, context: str) -> int:
    """Advance through one GIF sub-block sequence, requiring its zero terminator."""
    while True:
        if offset >= len(data):
            raise corrupt(f"GIF: {context} ends before its sub-block length")
        length = data[offset]
        offset += 1
        if length == 0:
            return offset
        if offset + length > len(data):
            raise corrupt(f"GIF: {context} sub-block runs past the end of the file")
        offset += length


def _gif_color_table_end(data: bytes, offset: int, packed: int, *, context: str) -> int:
    """Advance over an optional GIF colour table whose size its header declares."""
    if not packed & 0x80:
        return offset
    entries = 1 << ((packed & 0x07) + 1)
    end = offset + 3 * entries
    if end > len(data):
        raise corrupt(f"GIF: {context} colour table runs past the end of the file")
    return end


def validate_gif(data: bytes) -> ImageGeometry:
    """Require a complete GIF block stream before Pillow can trust a frame count.

    Pillow can decode a strict prefix of an animated GIF as a complete one-frame
    image.  That is useful recovery behaviour for an interactive viewer, but it is
    not a sufficient immutable Exemplar: without the GIF trailer and every block
    framing its second frame, a damaged source would be sealed as a complete page.
    This small walker proves framing only; Pillow still owns LZW pixel decoding.
    """
    if not any(data.startswith(signature) for signature in GIF_SIGNATURES):
        raise corrupt("GIF: missing GIF87a or GIF89a header")
    if len(data) < 13:
        raise corrupt("GIF: truncated logical screen descriptor")
    width, height = struct.unpack_from("<HH", data, 6)
    geometry = _geometry("gif", width, height)
    offset = _gif_color_table_end(data, 13, data[10], context="logical-screen")
    images = 0

    while offset < len(data):
        marker = data[offset]
        offset += 1
        if marker == 0x3B:  # trailer
            if images < 1:
                raise corrupt("GIF: trailer arrives before an image descriptor")
            # Appended bytes do not alter the complete GIF datastream, just as
            # JPEG padding after EOI does not make an otherwise sound image corrupt.
            return geometry
        if marker == 0x21:  # extension introducer + label + sub-blocks
            if offset >= len(data):
                raise corrupt("GIF: truncated extension label")
            label = data[offset]
            offset += 1
            if label == 0xF9:  # Graphic Control Extension: fixed four-byte payload.
                if offset >= len(data) or data[offset] != 4:
                    raise corrupt("GIF: malformed graphic-control extension length")
                offset += 1
                if offset + 4 >= len(data):
                    raise corrupt("GIF: graphic-control extension runs past the file")
                offset += 4
                if data[offset] != 0:
                    raise corrupt("GIF: graphic-control extension has no terminator")
                offset += 1
            else:
                offset = _gif_skip_sub_blocks(data, offset, context="extension")
            continue
        if marker != 0x2C:  # image separator
            raise corrupt(f"GIF: unknown block marker 0x{marker:02X}")
        if offset + 9 > len(data):
            raise corrupt("GIF: truncated image descriptor")
        # The image descriptor begins with left/top/width/height and ends with
        # packed flags. Its local dimensions are bounded as well as the screen.
        _left, _top, image_width, image_height, packed = struct.unpack_from("<HHHHB", data, offset)
        _geometry("gif", image_width, image_height)
        offset = _gif_color_table_end(data, offset + 9, packed, context="image")
        if offset >= len(data):
            raise corrupt("GIF: image has no LZW minimum code size")
        # Any byte is a syntactically present code-size field. Pillow owns whether
        # it actually supports the encoded LZW stream.
        offset += 1
        offset = _gif_skip_sub_blocks(data, offset, context="image data")
        images += 1

    raise corrupt("GIF: missing trailer")


def _jpeg_quantization_tables(payload: bytes) -> set[int]:
    """Every quantization table id this DQT segment actually defines."""
    defined: set[int] = set()
    cursor = 0
    while cursor < len(payload):
        precision, identifier = payload[cursor] >> 4, payload[cursor] & 0x0F
        if precision not in (0, 1) or identifier > 3:
            raise corrupt("JPEG: DQT declares an out-of-range precision or table id")
        cursor += 1 + 64 * (2 if precision else 1)
        if cursor > len(payload):
            raise corrupt("JPEG: a DQT table runs past its own segment")
        defined.add(identifier)
    return defined


def _jpeg_huffman_tables(payload: bytes) -> set[tuple[int, int]]:
    """Every (class, id) Huffman table this DHT segment actually defines.

    The 16 code-length counts are summed to find where the table ends, so a segment
    that declares more codes than it carries is refused here rather than leaving a
    scan pointing at a table that was never fully written.
    """
    defined: set[tuple[int, int]] = set()
    cursor = 0
    while cursor < len(payload):
        if cursor + 17 > len(payload):
            raise corrupt("JPEG: a DHT table header runs past its own segment")
        table_class, identifier = payload[cursor] >> 4, payload[cursor] & 0x0F
        if table_class not in (0, 1) or identifier > 3:
            raise corrupt("JPEG: DHT declares an out-of-range table class or id")
        symbols = sum(payload[cursor + 1 : cursor + 17])
        cursor += 17 + symbols
        if cursor > len(payload):
            raise corrupt("JPEG: a DHT table runs past its own segment")
        defined.add((table_class, identifier))
    return defined


def _jpeg_arithmetic_conditioning(payload: bytes) -> set[tuple[int, int]]:
    """Every (class, id) conditioning slot this DAC segment defines."""
    if len(payload) % 2:
        raise corrupt("JPEG: DAC segment is not a whole number of entries")
    defined: set[tuple[int, int]] = set()
    for cursor in range(0, len(payload), 2):
        table_class, identifier = payload[cursor] >> 4, payload[cursor] & 0x0F
        if table_class not in (0, 1) or identifier > 3:
            raise corrupt("JPEG: DAC declares an out-of-range table class or id")
        defined.add((table_class, identifier))
    return defined


def _validate_jpeg_scan(
    payload: bytes,
    *,
    frame_components: list[tuple[int, int]],
    progressive: bool,
    arithmetic: bool,
    lossless: bool,
    quantization_tables: set[int],
    huffman_tables: set[tuple[int, int]],
    arithmetic_conditioning: set[tuple[int, int]],
) -> None:
    """Reconcile one scan header against the tables defined before it.

    Which entropy tables a scan needs depends on what kind of scan it is, and
    getting that wrong in either direction is a real cost: demanding both DC and AC
    tables of a progressive scan would refuse ordinary progressive JPEGs, and
    demanding neither is the hole this closes. In a sequential frame every scanned
    component uses both. In a progressive frame a first DC scan uses its DC table,
    a DC *refinement* scan (`Ah > 0`) is coded as raw bits and uses no table at all,
    and an AC scan uses only its AC table. A **lossless** frame codes DC only and
    legally defines no AC table — demanding one refused a conforming file, which
    this validator caught itself doing before it shipped.

    A quantization table is a different matter: every frame component names one, in
    every frame type, so that check is unconditional.
    """
    scan_components = payload[0] if payload else 0
    if (
        not 1 <= scan_components <= len(frame_components)
        or len(payload) != 1 + 2 * scan_components + 3
    ):
        raise corrupt("JPEG: scan components disagree with the segment")

    spectral_start, _spectral_end, approximation = payload[-3], payload[-2], payload[-1]
    high_approximation = approximation >> 4
    by_id = dict(frame_components)

    for index in range(scan_components):
        component_id = payload[1 + 2 * index]
        selectors = payload[2 + 2 * index]
        if component_id not in by_id:
            raise corrupt("JPEG: a scan names a component the frame does not declare")
        # A lossless frame does not quantize, so it legally carries no DQT at all
        # and its Tq field is required to be zero. Demanding one refused a conforming
        # file — and the first form of this repair did exactly that while believing
        # it had exempted lossless frames, because it exempted only the *Huffman*
        # check. An adversarial read caught it; the test below is the other half.
        quantization_id = by_id[component_id]
        if not lossless and quantization_id not in quantization_tables:
            raise corrupt(
                f"JPEG: a scanned component selects quantization table "
                f"{quantization_id}, which no DQT defined"
            )
        if arithmetic:
            # An arithmetic-coded scan may legally use the default conditioning, so
            # a DAC segment is optional and there is no selector to reconcile. The
            # conditioning that *was* declared is still parsed, above, for shape.
            continue
        needed: list[tuple[int, int]]
        if lossless:
            needed = [(0, selectors >> 4)]
        elif not progressive:
            needed = [(0, selectors >> 4), (1, selectors & 0x0F)]
        elif spectral_start == 0:
            # A DC refinement scan carries one raw bit per coefficient, no table.
            needed = [] if high_approximation else [(0, selectors >> 4)]
        else:
            needed = [(1, selectors & 0x0F)]
        for table in needed:
            if table not in huffman_tables:
                kind = "DC" if table[0] == 0 else "AC"
                raise corrupt(
                    f"JPEG: a scan selects the {kind} Huffman table {table[1]}, "
                    "which no DHT defined"
                )


def _jpeg_marker_at(data: bytes, cursor: int) -> tuple[int, int]:
    if cursor >= len(data) or data[cursor] != 0xFF:
        raise corrupt(f"JPEG: expected a marker at byte {cursor}")
    while cursor < len(data) and data[cursor] == 0xFF:
        cursor += 1
    if cursor >= len(data) or data[cursor] == 0:
        raise corrupt("JPEG: marker is truncated, or stuffed outside a scan")
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
            raise corrupt("JPEG: scan data ends in a marker prefix")
        marker = data[cursor]
        cursor += 1
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            continue  # a stuffed byte or an in-scan restart marker: still scan data
        return marker, cursor
    raise corrupt("JPEG: truncated scan data, no terminating marker")


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
_TIFF_TAG_BITS_PER_SAMPLE: Final = 258
_TIFF_TAG_COMPRESSION: Final = 259
_TIFF_TAG_PHOTOMETRIC: Final = 262
_TIFF_TAG_SAMPLES_PER_PIXEL: Final = 277
_TIFF_TAG_ROWS_PER_STRIP: Final = 278
_TIFF_TAG_PLANAR_CONFIGURATION: Final = 284
_TIFF_TAG_TILE_WIDTH: Final = 322
_TIFF_TAG_TILE_LENGTH: Final = 323
_TIFF_UNCOMPRESSED: Final = 1
# Where the actual samples live. A TIFF that names none of these declares an image
# with no image data, and one whose ranges leave the file is not an instance of the
# format it claims however tidy its header looks.
_TIFF_STRIP_TAGS: Final = (273, 279)
_TIFF_TILE_TAGS: Final = (324, 325)
_TIFF_DATA_TAGS: Final = frozenset(_TIFF_STRIP_TAGS + _TIFF_TILE_TAGS)
# The sample-layout tags this validator interprets. Everything else in an IFD is
# still bounds-checked as an entry, but its meaning is not this module's business.
_TIFF_LAYOUT_TAGS: Final = (
    _TIFF_TAG_BITS_PER_SAMPLE,
    _TIFF_TAG_COMPRESSION,
    _TIFF_TAG_PHOTOMETRIC,
    _TIFF_TAG_SAMPLES_PER_PIXEL,
    _TIFF_TAG_ROWS_PER_STRIP,
    _TIFF_TAG_PLANAR_CONFIGURATION,
    _TIFF_TAG_TILE_WIDTH,
    _TIFF_TAG_TILE_LENGTH,
)


def validate_tiff(data: bytes) -> ImageGeometry:
    """Prove one image directory whose stored samples reconcile with its geometry.

    **What is proven, exactly.** Classic (32-bit offset) little- or big-endian TIFF;
    one image directory, every entry's value inside the file; the baseline tags a
    reader needs to know what the samples *are* — `PhotometricInterpretation`,
    `Compression`, `BitsPerSample`, `SamplesPerPixel`; and the strip or tile
    inventory reconciled against the declared geometry, so the number of segments is
    the number the image's own rows and tiles require. For an **uncompressed** image
    the byte counts are checked exactly, row by row: a 6x5 8-bit image must carry 30
    bytes and not one. Before that check, a 63-byte file could declare a 1000x1000
    image behind a single stored byte and be admitted as genuine.

    **What is not proven.** For a *compressed* image the stored byte counts cannot be
    reconciled without decompressing, which is pixel reconstruction — so the segment
    count is checked against the geometry and the byte counts are not. That is a
    named, documented limit, not a shortcut, and it is why this module says a file
    that passes here is provably the format it claims rather than provably intact.

    A later image directory is normal multi-page TIFF, not a refusal. The door asks
    Pillow for the bounded page count and renders each directory separately; this
    first-directory walker remains a structural check of the source's opening page.
    """
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        raise corrupt("TIFF: missing byte-order header")
    endian = "<" if data[:2] == b"II" else ">"
    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic != 42:
        raise unsupported(
            "TIFF: not classic TIFF (BigTIFF or an unknown magic is a documented limit)"
        )
    (offset,) = struct.unpack_from(endian + "I", data, 4)
    if offset == 0:
        raise corrupt("TIFF: the header names no image directory")
    if offset + 2 > len(data):
        raise corrupt("TIFF: IFD offset falls outside the file")

    width = height = None
    image_data: dict[int, list[int]] = {}
    layout: dict[int, list[int]] = {}
    (count,) = struct.unpack_from(endian + "H", data, offset)
    table_end = offset + 2 + count * 12 + 4
    if table_end > len(data):
        raise corrupt("TIFF: IFD entries run past the end of the file")

    for index in range(count):
        entry = offset + 2 + index * 12
        tag, field_type, value_count = struct.unpack_from(endian + "HHI", data, entry)
        interpreted = (
            tag in (_TIFF_TAG_IMAGE_WIDTH, _TIFF_TAG_IMAGE_LENGTH)
            or tag in _TIFF_DATA_TAGS
            or tag in _TIFF_LAYOUT_TAGS
        )
        size = _TIFF_TYPE_SIZES.get(field_type)
        if size is None:
            # TIFF 6.0 tells a reader to skip a field whose type it does not
            # recognise rather than reject the file. Refusing the whole image over an
            # unknown type in a tag this validator never reads would refuse a
            # conforming file for a field nobody here touches — a page nobody reads,
            # GOALS 1. A tag we *do* interpret is a different matter: an unreadable
            # value there is a check that cannot run, which is a failure.
            if interpreted:
                raise corrupt(
                    f"TIFF: tag {tag} carries unknown field type {field_type}, and "
                    "this validator has to read that tag"
                )
            continue
        if value_count > len(data) // size:
            raise corrupt(f"TIFF: tag {tag} declares more values than the file")
        value_size = value_count * size
        value_field = data[entry + 8 : entry + 12]
        if value_size > 4:
            (value_offset,) = struct.unpack(endian + "I", value_field)
            if value_offset + value_size > len(data):
                raise corrupt(f"TIFF: tag {tag} value escapes the file bounds")
            value_bytes = data[value_offset : value_offset + value_size]
        else:
            value_bytes = value_field[:value_size]

        if tag in (_TIFF_TAG_IMAGE_WIDTH, _TIFF_TAG_IMAGE_LENGTH):
            value = _tiff_dimension(tag, value_bytes, field_type, value_count, endian)
            if tag == _TIFF_TAG_IMAGE_WIDTH:
                if width is not None:
                    raise corrupt(f"TIFF: tag {tag} appears twice in one directory")
                width = value
            else:
                if height is not None:
                    raise corrupt(f"TIFF: tag {tag} appears twice in one directory")
                height = value
        elif tag in _TIFF_DATA_TAGS or tag in _TIFF_LAYOUT_TAGS:
            target = image_data if tag in _TIFF_DATA_TAGS else layout
            if tag in target:
                raise corrupt(f"TIFF: tag {tag} appears twice in one directory")
            target[tag] = _tiff_unsigned_values(value_bytes, field_type, value_count, endian, tag)

    # A non-zero next IFD is a further page.  Its own pixels are decoded and bound
    # when the door fans it out; rejecting it here would lose a normal scan.

    if width is None or height is None:
        raise corrupt("TIFF: no ImageWidth/ImageLength tag")
    geometry = _geometry("tiff", width, height)
    _validate_tiff_sample_storage(data, geometry, image_data, layout)
    return geometry


def _tiff_single(layout: dict[int, list[int]], tag: int, default: int | None, name: str) -> int:
    """One tag that must carry exactly one value, or its baseline default."""
    values = layout.get(tag)
    if values is None:
        if default is None:
            raise corrupt(f"TIFF: no {name} tag; a baseline image declares one")
        return default
    if len(values) != 1:
        raise corrupt(f"TIFF: {name} declares {len(values)} values, not one")
    return values[0]


def _tiff_dimension(tag: int, value: bytes, field_type: int, count: int, endian: str) -> int:
    """A dimension is one SHORT or one LONG. A `count` of anything else leaves
    `value` the wrong length for the unpack, and a bare `struct.error` is not a
    named refusal — this is the fail-closed check that keeps a malformed count a
    refusal the door can name rather than a crash that aborts every other source
    still waiting to be decided."""
    if count != 1 or field_type not in (3, 4):
        raise corrupt(f"TIFF: tag {tag} is not one SHORT or LONG")
    return struct.unpack(endian + ("H" if field_type == 3 else "I"), value)[0]


def _tiff_unsigned_values(
    value: bytes, field_type: int, count: int, endian: str, tag: int = 0
) -> list[int]:
    """Decode a SHORT/LONG tag's values without touching a byte of pixel data."""
    if not count or field_type not in (3, 4):
        raise corrupt(f"TIFF: tag {tag} values are not a non-empty list of unsigned integers")
    if count > MAX_TIFF_DATA_SEGMENTS:
        raise unsupported(
            f"TIFF: {count} image-data segments exceed the {MAX_TIFF_DATA_SEGMENTS}-segment limit"
        )
    unit = 2 if field_type == 3 else 4
    if len(value) != count * unit:
        raise corrupt("TIFF: image-data value length disagrees with its IFD entry")
    return list(struct.unpack(endian + ("H" if field_type == 3 else "I") * count, value))


def _validate_tiff_sample_storage(
    data: bytes,
    geometry: ImageGeometry,
    image_data: dict[int, list[int]],
    layout: dict[int, list[int]],
) -> None:
    """Reconcile the stored strip or tile inventory against the declared image.

    The inventory being *in the file* was the whole of the old check, which let a
    63-byte file declare a million pixels behind one stored byte. Here the number of
    segments must be the number the geometry requires, and for an uncompressed image
    each segment's byte count must be exactly what its rows occupy.
    """
    samples = _tiff_single(layout, _TIFF_TAG_SAMPLES_PER_PIXEL, 1, "SamplesPerPixel")
    if not 1 <= samples <= 8:
        raise unsupported(f"TIFF: {samples} samples per pixel is a documented limit")
    compression = _tiff_single(layout, _TIFF_TAG_COMPRESSION, _TIFF_UNCOMPRESSED, "Compression")
    # Refused rather than defaulted: it is the tag that says what the samples *mean*,
    # a baseline IFD is required to carry it, and a file omitting it is not one.
    _tiff_single(layout, _TIFF_TAG_PHOTOMETRIC, None, "PhotometricInterpretation")
    planar = _tiff_single(layout, _TIFF_TAG_PLANAR_CONFIGURATION, 1, "PlanarConfiguration")
    if planar != 1:
        raise unsupported(
            "TIFF: planar (component-separated) storage is a documented limit; "
            "its strip arithmetic is not the chunky one this reconciles"
        )
    bits = layout.get(_TIFF_TAG_BITS_PER_SAMPLE, [1] * samples)
    if len(bits) != samples or any(not 1 <= bit <= 64 for bit in bits):
        raise corrupt("TIFF: BitsPerSample does not carry one usable width per sample")
    row_bytes = (geometry.width * sum(bits) + 7) // 8

    strips = [image_data.get(tag) for tag in _TIFF_STRIP_TAGS]
    tiles = [image_data.get(tag) for tag in _TIFF_TILE_TAGS]
    has_strips = any(part is not None for part in strips)
    has_tiles = any(part is not None for part in tiles)
    if has_strips and has_tiles:
        raise corrupt("TIFF: both strip and tile image-data inventories are named")
    offsets, counts = strips if has_strips else tiles
    if offsets is None and counts is None:
        raise corrupt("TIFF: no strip or tile image-data inventory")
    if offsets is None or counts is None or len(offsets) != len(counts):
        raise corrupt("TIFF: image-data offsets and byte counts do not reconcile")

    expected = (
        _tiff_expected_tile_sizes(geometry, layout, bits)
        if has_tiles
        else _tiff_expected_strip_sizes(geometry, layout, row_bytes)
    )
    if len(offsets) != len(expected):
        raise corrupt(
            f"TIFF: the image declares {len(offsets)} image-data segment(s), but its "
            f"{geometry.width}x{geometry.height} geometry needs {len(expected)}"
        )
    for offset, count, needed in zip(offsets, counts, expected, strict=True):
        if offset < 8 or count <= 0 or offset + count > len(data):
            raise corrupt("TIFF: an image-data range falls outside the file")
        # Only an uncompressed segment's size is derivable from the geometry. For a
        # compressed one the count is whatever the codec produced, and reconciling it
        # would mean decompressing — the named limit this module keeps.
        if compression == _TIFF_UNCOMPRESSED and count < needed:
            raise corrupt(
                f"TIFF: an uncompressed image-data segment stores {count} byte(s) "
                f"where its share of a {geometry.width}x{geometry.height} image needs {needed}"
            )


def _tiff_expected_strip_sizes(
    geometry: ImageGeometry, layout: dict[int, list[int]], row_bytes: int
) -> list[int]:
    """The byte count each strip would occupy uncompressed, in order."""
    rows_per_strip = _tiff_single(layout, _TIFF_TAG_ROWS_PER_STRIP, geometry.height, "RowsPerStrip")
    if rows_per_strip <= 0:
        raise corrupt("TIFF: RowsPerStrip is not a positive row count")
    rows_per_strip = min(rows_per_strip, geometry.height)
    whole, remainder = divmod(geometry.height, rows_per_strip)
    return [rows_per_strip * row_bytes] * whole + ([remainder * row_bytes] if remainder else [])


def _tiff_expected_tile_sizes(
    geometry: ImageGeometry, layout: dict[int, list[int]], bits: list[int]
) -> list[int]:
    """The byte count each tile would occupy uncompressed. Tiles are padded, so
    every tile is the same size regardless of where the image edge falls."""
    tile_width = _tiff_single(layout, _TIFF_TAG_TILE_WIDTH, None, "TileWidth")
    tile_length = _tiff_single(layout, _TIFF_TAG_TILE_LENGTH, None, "TileLength")
    if tile_width <= 0 or tile_length <= 0:
        raise corrupt("TIFF: a tile dimension is not positive")
    across = -(-geometry.width // tile_width)
    down = -(-geometry.height // tile_length)
    if across * down > MAX_TIFF_DATA_SEGMENTS:
        raise unsupported(
            f"TIFF: {across * down} tiles exceed the {MAX_TIFF_DATA_SEGMENTS}-segment limit"
        )
    return [tile_length * ((tile_width * sum(bits) + 7) // 8)] * (across * down)


VALIDATORS: Final = {
    "png": validate_png,
    "jpeg": validate_jpeg,
    "gif": validate_gif,
    "tiff": validate_tiff,
}

# Both derived from what this module can actually do, never written out a second
# time: `SNIFFABLE_FORMATS` from the table `sniff()` walks (plus HEIC, whose
# detection is a brand check rather than a prefix), and `STRUCTURALLY_VALIDATED`
# from the dispatch table `validate()` uses. `admission.py` reads these to check
# the policy's coverage, and a hand-kept copy there would only ever agree with
# another hand-kept copy.
SNIFFABLE_FORMATS: Final = frozenset(
    {name for name, _ in _SIGNATURES} | {"heic", "heif", "avif", "webp"}
)
STRUCTURALLY_VALIDATED: Final = frozenset(VALIDATORS)


def validate(format_name: str, data: bytes) -> ImageGeometry:
    """Dispatch to the validator for a sniffed format; refuse anything else."""
    try:
        validator = VALIDATORS[format_name]
    except KeyError:
        raise unsupported(f"format {format_name!r}: no structural validator") from None
    return validator(data)


class DecodedRaster(NamedTuple):
    """One decoded raster page, preserving the decoder's detected format."""

    format: str
    width: int
    height: int
    frame_count: int


def _structural_corruption_check(format_name: str | None, data: bytes) -> None:
    """Run a structural walker as a corruption detector, and only as that.

    Spec 03 keeps structural validation and says exactly what it may no longer do:
    "Walking the container to prove the bytes are a genuine, uncorrupted instance of
    the format they claim ... is exactly the corruption detection ruling 2 asks for,
    and it stays. **What it may no longer do is decide a real file is inadmissible
    because nothing here reconstructs its pixels.**"

    Those two halves are distinguishable in the walkers' own vocabulary, and neither
    lane split them. A `corrupt ...` refusal says the bytes are not a genuine
    instance of what they claim — that is damage, and it is refused here before a
    permissive decoder can render half of it as a page. An `unsupported ...` refusal
    says only that *this narrow walker* does not interpret that layout: BigTIFF,
    planar storage, a lossless JPEG process. That is a statement about this module,
    not about the file, so the real decoder gets to answer instead. If the decoder
    cannot read it either, its own refusal is what surfaces — an
    `unsupported-variant` alarm naming a reader this project owes.

    Lane A dropped TIFF's structural walk entirely for fear it would refuse valid
    compressed and multi-page scans; Lane B kept the walk and let it decide
    admission. Splitting the verdict keeps the corruption detection over every
    format without either cost.
    """
    if format_name not in VALIDATORS:
        return
    try:
        validate(format_name, data)
    except FormatRefusal as error:
        if error.verdict is FormatVerdict.CORRUPT:
            raise


@contextmanager
def _decoder_only(detail: str, *, format_name: str | None = None):
    """Guard a region that contains nothing but the installed decoder's own work.

    A previous round widened this module's catch set to whatever classes Pillow had
    been *seen* to raise, and a later one narrowed it back by removing
    `KeyError`, `IndexError` and `AttributeError` — on the reasoning that the
    protected block also contained this project's routing and geometry code, so
    catching those would mislabel a programming defect as image corruption. The
    reasoning was right and its premise was wrong: Pillow raises `IndexError` on
    ordinary malformed input. A two-frame GIF truncated inside its second image
    descriptor makes `n_frames` fail at `GifImagePlugin._seek` with a bare
    `IndexError`, which escaped `decode_raster` entirely and took the whole
    submission's expansion down with it — the exact whole-Door crash the
    `TypeError` arm exists to prevent, reopened one byte along.

    An exception class cannot carry the distinction that argument needs, because
    the question is *whose code raised it*. A region can. Everything inside this
    guard is Pillow's, so any exception from it is about the bytes; this project's
    own checks stay outside, where a defect in them still surfaces as itself.

    `FormatRefusal` subclasses `ValueError`, so it is re-raised explicitly ahead of
    the broad clause. Without that, a `corrupt` verdict raised by this module came
    back out relabelled `unsupported`, with the real reason buried in a
    parenthesis — two different sentences to write to Tyrel (ruling 2), collapsed
    into the wrong one.
    """
    try:
        yield
    except (FormatRefusal, UnidentifiedImageError, Image.DecompressionBombError):
        raise
    except (Image.DecompressionBombWarning, MemoryError):
        # A bomb warning has its own named arm at the call site, and a MemoryError
        # is about this machine rather than about these bytes.
        raise
    except Exception as error:
        if format_name is not None and has_reader(format_name):
            raise corrupt(
                f"{format_name}: the installed decoder could not {detail} ({error})"
            ) from error
        if format_name is not None:
            raise unsupported(missing_reader_detail(format_name)) from error
        raise unsupported(
            f"image variant: the installed decoder could not {detail} ({error})"
        ) from error


def decode_raster(data: bytes, *, page_index: int = 0) -> DecodedRaster:
    """Decode one page through Pillow without trusting a filename or extension.

    Pillow is asked to load pixels, not merely identify a header.  Its normal
    truncated-image behaviour is left enabled, so a partial scan cannot become a
    silently grey page.  A known format that this build cannot decode is an
    unsupported variant; bytes that name no image at all are unrecognized.
    """
    detected_by_signature = sniff(data)
    if detected_by_signature in {"heic", "heif", "avif"}:
        _validate_iso_bmff_image_header(data, detected_by_signature)
    classic_tiff_pages: int | None = None
    if detected_by_signature == "tiff":
        # A resource bound, not a verdict about the layout: it propagates whatever
        # the structural walker below decides, because a decoder that happily
        # enumerates fifty thousand directories is not a reason to fan out fifty
        # thousand ordinals.
        classic_tiff_pages = _validate_classic_tiff_page_chain(data)
    _structural_corruption_check(detected_by_signature, data)
    try:
        # Pillow warns for a decompression bomb while opening some formats.  A
        # warning emitted to a terminal is neither a sealed outcome nor a useful
        # failure mode, so turn it into the same named decoder alarm as its error
        # form.  Our own geometry check below is the project limit; Pillow's is a
        # conservative first line before it allocates decoder state.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                with _decoder_only(
                    "read what these bytes contain", format_name=detected_by_signature
                ):
                    reported_format = image.format
                    # Pillow's ``n_frames`` walk can touch a later bad IFD before
                    # we have asked it for page zero.  A structurally complete
                    # classic TIFF chain gives us the same finite count without
                    # interpreting every page's tags, so page zero stays eligible
                    # for its own decode and a bad later page gets its own ordinal
                    # and named outcome.
                    frames = (
                        classic_tiff_pages
                        if classic_tiff_pages is not None
                        else getattr(image, "n_frames", 1)
                    )
                detected = _pillow_format_name(reported_format)
                if not isinstance(frames, int) or frames < 1:
                    raise corrupt("image: decoder returned no frames")
                # The fan-out bound, checked on whatever the decoder reports rather
                # than only on a classic TIFF's directory chain. A BigTIFF, an
                # animated GIF or a WebP reaches this line without ever passing the
                # chain walker, and a frame count read out of an untrusted file is
                # a loop bound somebody else wrote.
                if frames > MAX_RASTER_FRAMES:
                    raise unsupported(
                        f"{detected}: {frames} frames exceed the "
                        f"{MAX_RASTER_FRAMES}-frame fan-out limit"
                    )
                if not isinstance(page_index, int) or isinstance(page_index, bool):
                    raise corrupt("image: page index is not an integer")
                if not 0 <= page_index < frames:
                    raise corrupt(f"{detected}: page index {page_index} is outside 0..{frames - 1}")
                # Seeking chooses the frame but does not ask Pillow to decode its
                # pixels.  Bound that frame's declared dimensions before `load()`
                # can inflate attacker-controlled data into memory.
                with _decoder_only(f"reach page {page_index}", format_name=detected_by_signature):
                    image.seek(page_index)
                    declared = (image.width, image.height)
                geometry = _geometry(detected, *declared)
                with _decoder_only(f"decode page {page_index}", format_name=detected_by_signature):
                    image.load()
                return DecodedRaster(detected, geometry.width, geometry.height, frames)
    except FormatRefusal:
        # This module's own verdicts, raised by the project-owned checks between the
        # guarded regions. `FormatRefusal` subclasses `ValueError`, so without this
        # arm the clause below relabelled every one of them `unsupported` and buried
        # the real reason in a parenthesis — a `corrupt` page, which says the bytes
        # are damaged, arriving as a statement about a decoder this build lacks.
        raise
    except UnidentifiedImageError as error:
        if detected_by_signature is not None:
            if has_reader(detected_by_signature):
                raise corrupt(
                    f"{detected_by_signature}: the installed decoder could not open these bytes"
                ) from error
            raise unsupported(missing_reader_detail(detected_by_signature)) from error
        raise unrecognized("image format: no installed decoder recognizes these bytes") from error
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise unsupported(
            f"image variant: decoder rejected unsafe pixel dimensions ({error})"
        ) from error
    except (OSError, SyntaxError, ValueError) as error:
        # `Image.open` itself is the one decoder call outside a `_decoder_only`
        # region — it has to be, because its two named arms above want their own
        # wording and its return value owns the context manager that closes the
        # file. Its plugins convert most of their own faults to
        # UnidentifiedImageError; these three are what an eager header read can
        # still raise before a plugin has been chosen. Everything after it is
        # guarded by region, so no class list here has to guess at Pillow's
        # internals again.
        if detected_by_signature is not None and has_reader(detected_by_signature):
            raise corrupt(
                f"{detected_by_signature}: the installed decoder could not open these bytes ({error})"
            ) from error
        if detected_by_signature is not None:
            raise unsupported(missing_reader_detail(detected_by_signature)) from error
        raise unsupported(
            f"image variant: the installed decoder could not open it ({error})"
        ) from error


# The Pillow plugin name for each format this door can sniff, so "is there a reader
# for this format at all" is answered from what is actually installed rather than
# from a list somebody keeps up to date by hand.
_DECODER_PLUGINS: Final = {
    "png": "PNG",
    "jpeg": "JPEG",
    "tiff": "TIFF",
    "gif": "GIF",
    "bmp": "BMP",
    "webp": "WEBP",
    "heic": "HEIF",
    "heif": "HEIF",
    "avif": "AVIF",
}


def has_reader(format_name: str) -> bool:
    """Whether this build has any decoder for that format at all.

    A capability question about the installation, answerable without looking at a
    file — which is what makes it possible to tell "we cannot read this format yet"
    apart from "this particular file will not decode".
    """
    Image.init()
    plugin = _DECODER_PLUGINS.get(format_name)
    return plugin is not None and plugin in Image.OPEN


def missing_reader_detail(format_name: str) -> str:
    """Why a sniffed format did not decode, worded as whose defect it is.

    Ruling 2: "If things are failing the image got corrupted … or the pipeline is
    broken." Those are different sentences to write to Tyrel, and which one is true
    is knowable: if nothing installed here reads that format at all, this project
    owes him a reader and says so; if a reader exists and still could not open the
    file, that is about these bytes. Lane B worded the first case correctly and had
    no way to reach the second; Lane A could reach both and worded them alike.
    """
    if not has_reader(format_name):
        return (
            f"{format_name}: this build has no reader for {format_name} at all "
            "yet, which is a gap in this pipeline rather than anything about this file"
        )
    return f"{format_name}: this build has a {format_name} reader and it could not open these bytes"


def count_raster_pages(data: bytes) -> int:
    """Read a decoder-backed page count without creating output pixels."""
    if sniff(data) == "tiff":
        classic_pages = _validate_classic_tiff_page_chain(data)
        if classic_pages is not None:
            if classic_pages > MAX_RASTER_FRAMES:
                raise unsupported(
                    f"TIFF: {classic_pages} frames exceed the {MAX_RASTER_FRAMES}-frame fan-out limit"
                )
            return classic_pages
    return decode_raster(data).frame_count


def raster_renderer_recipe() -> dict[str, Any]:
    """The Pillow facts that affect a door-produced page render.

    This is also bound before a real run is created.  A Pillow upgrade must make a
    resumed render a new run rather than discover different immutable pixels only
    after it has published a blob.
    """
    return {
        "renderer": "Pillow",
        "renderer_version": Image.__version__,
        "pillow_heif_version": pillow_heif.__version__,
        "libheif_version": pillow_heif.libheif_info()["libheif"],
        "output": {
            "codec": "png-or-tiff",
            "mode_policy": "preserve-standard-png-or-high-precision-tiff-else-convert-by-alpha",
        },
    }


_HIGH_PRECISION_TIFF_MODES: Final = {
    # Pillow's PNG encoder cannot represent the unbounded signed integer or float
    # modes.  TIFF can, so use it rather than clipping real samples to 8-bit RGB.
    "I": "I",
    "F": "F",
    "I;16B": "I;16B",
    # Pillow normalises a little-endian 16-bit TIFF to ``I;16`` when re-opened.
    "I;16L": "I;16",
}
_PNG_IDENTITY_MODES: Final = frozenset({"1", "L", "LA", "RGB", "RGBA", "I;16"})


def render_raster_page(data: bytes, page_index: int) -> tuple[bytes, ImageGeometry, dict[str, Any]]:
    """Render a fanned raster page to lossless PNG or lossless TIFF pixels.

    Standard Pillow modes retain compact PNG output.  The modes whose samples PNG
    cannot faithfully hold keep their native samples in TIFF; an Exemplar page is
    immutable evidence, not a display preview allowed to crush its values.
    """
    decoded = decode_raster(data, page_index=page_index)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                # Every call here is Pillow's, so the whole body is one guarded
                # region: this stage owns the mode policy below, not the decoding,
                # and a decoder fault on a frame `decode_raster` already accepted
                # is still a statement about the bytes rather than about us.
                with _decoder_only(f"rasterise page {page_index}"):
                    image.seek(page_index)
                    # `decode_raster()` above already made this exact source/frame
                    # pass the project geometry bound before loading it.
                    image.load()
                    source_mode = image.mode
                    source_bands = list(image.getbands())
                    if source_mode in _HIGH_PRECISION_TIFF_MODES:
                        rendered = image.copy()
                        mode_transform = "lossless-tiff-samples"
                        output_codec = "tiff"
                    elif source_mode in _PNG_IDENTITY_MODES:
                        rendered = image.copy()
                        mode_transform = "identity"
                        output_codec = "png"
                    else:
                        target_mode = "RGBA" if "A" in source_bands else "RGB"
                        rendered = image.convert(target_mode)
                        mode_transform = f"convert-to-{target_mode.lower()}"
                        output_codec = "png"
                    output = BytesIO()
                    if output_codec == "tiff":
                        rendered.save(output, format="TIFF", compression="raw")
                    else:
                        rendered.save(output, format="PNG", optimize=False, compress_level=9)
    except FormatRefusal:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise unsupported(
            f"{decoded.format}: the installed decoder could not rasterise page {page_index} ({error})"
        ) from error
    return (
        output.getvalue(),
        ImageGeometry(decoded.format, decoded.width, decoded.height),
        {
            **raster_renderer_recipe(),
            "source_mode": source_mode,
            "source_bands": source_bands,
            "mode_transform": mode_transform,
            "output": {"codec": output_codec, "color_mode": rendered.mode},
            "container_page_index": page_index,
            "width": decoded.width,
            "height": decoded.height,
        },
    )


def _pillow_format_name(format_name: str | None) -> str:
    """Use a stable lowercase name even for a decoder format no sniffer names."""
    if not format_name:
        return "unknown-raster"
    aliases = {"JPEG": "jpeg", "TIFF": "tiff", "PNG": "png", "WEBP": "webp"}
    return aliases.get(format_name, format_name.lower())


def _validate_classic_tiff_page_chain(data: bytes) -> int | None:
    """Return a bounded classic-TIFF page count without decoding later pages.

    Pillow owns actual pixel decoding and accepts several valid TIFF layouts this
    project's narrow first-page structural walker deliberately does not interpret.
    The link chain is different: every classic directory gives a finite next-IFD
    offset, so a loop is objectively malformed and could otherwise leave a decoder
    reporting an arbitrary first-frame count.  Check only that universal shape;
    BigTIFF is left entirely to the installed decoder rather than rejected for
    being a wider offset layout.
    """
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if data[:2] == b"II" else ">"
    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic != 42:
        return None
    (offset,) = struct.unpack_from(endian + "I", data, 4)
    seen: set[int] = set()
    pages = 0
    while offset:
        if offset in seen:
            raise corrupt("TIFF: image-directory chain contains a cycle")
        if offset + 2 > len(data):
            raise corrupt("TIFF: image-directory offset falls outside the file")
        seen.add(offset)
        (entries,) = struct.unpack_from(endian + "H", data, offset)
        next_offset_at = offset + 2 + entries * 12
        if next_offset_at + 4 > len(data):
            raise corrupt("TIFF: image-directory entries run past the file")
        pages += 1
        if pages > MAX_TIFF_PAGES:
            raise unsupported(f"TIFF: more than {MAX_TIFF_PAGES} pages exceed the fan-out limit")
        (offset,) = struct.unpack_from(endian + "I", data, next_offset_at)
    return pages
