"""A bounded, door-private PDF page renderer. Standard library only.

**Named limit, stated up front, the same shape of decision `image_formats.py`
makes for interlaced PNG and BigTIFF.** This module does not render arbitrary PDF
content — that is a document layout engine, fonts, content-stream interpretation,
and colour management, and building one from `zlib`/`struct` would be exactly the
kind of half-handling `common/imaging.py`'s docstring already refuses. What it
does handle, honestly and completely within its scope, is the case this project's
sources actually are: **a flatbed scan saved as PDF, one image per page.**

A page renders here only when: the PDF uses a classic (non-stream) cross-reference
table with no `/Prev` chain, is not encrypted, declares no page rotation, and its
`/Resources/XObject` dictionary holds **exactly one** entry, an `/Image` XObject
compressed with `DCTDecode` (embedded JPEG, decoded by `image_formats.validate_jpeg`
and stored as-is) or `FlateDecode` (raw 8-bit `DeviceGray`/`DeviceRGB` samples,
re-encoded as a PNG through this module's own minimal encoder). Anything else —
vector content, multiple images, CCITT/JBIG2/JPX filters, indexed or CMYK colour,
incremental updates, encryption, rotation — is refused **by name**, never guessed
at. A page that fails this test is not a page this door can seal; it is held for a
human to look at, exactly like any other refusal.

**Door-private by design.** Nothing outside `pipeline/1_exemplar/door.py` imports
this module. Rendering happens once, at admission, and the render module having no
API a later stage could call is what makes "no later stage may re-render" true by
construction rather than by convention (spec 03, test 4).
"""

import re
import struct
import zlib
from typing import Any, Final, NamedTuple

import admission
from admission import RefusalReason
from image_formats import MAX_DIMENSION, MAX_PIXELS, FormatRefusal, validate_jpeg

PDF_SIGNATURE: Final = b"%PDF-"

# One scanned register volume is hundreds of pages, not hundreds of thousands. A
# page count read out of an untrusted file is a loop bound an attacker writes, so
# it is bounded here before the fan-out ever runs.
MAX_PAGES: Final = 5_000

_WHITESPACE: Final = b"\x00\t\n\x0c\r "
_DELIMITER_BYTES: Final = b"()<>[]{}/%"


class PdfRefusal(ValueError):
    """One page or one whole document refused, with a reason from the closed set.

    The reason string is assembled by `admission.reason()` — the same single
    spelling every other refusal in this stage uses, so a PDF refusal reads back
    through `admission.reason_code()` exactly like a raster one.
    """

    def __init__(self, code: RefusalReason, detail: str):
        self.reason = code
        self.detail = detail
        super().__init__(admission.reason(code, detail))


class PdfRef(NamedTuple):
    """An unresolved indirect reference, `N G R`."""

    num: int
    gen: int


class PdfStream:
    """A dictionary and the raw bytes of the stream that followed it."""

    __slots__ = ("dict", "raw")

    def __init__(self, dictionary: dict[str, Any], raw: bytes):
        self.dict = dictionary
        self.raw = raw


# --- The object parser: enough of PDF syntax to read a page tree ---------------


def _skip_ws_comments(data: bytes, pos: int) -> int:
    while pos < len(data):
        c = data[pos]
        if c in _WHITESPACE:
            pos += 1
        elif c == 0x25:  # '%' comment to end of line
            while pos < len(data) and data[pos] not in b"\r\n":
                pos += 1
        else:
            break
    return pos


def _word_boundary(data: bytes, pos: int) -> bool:
    if pos >= len(data):
        return True
    return data[pos] in _WHITESPACE or data[pos] in _DELIMITER_BYTES


_NAME_RE = re.compile(rb"/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_NUMBER_RE = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)")


def _parse_name(data: bytes, pos: int) -> tuple[str, int]:
    match = _NAME_RE.match(data, pos)
    if not match:
        raise PdfRefusal(RefusalReason.CORRUPT, f"malformed name object at byte {pos}")
    raw = match.group(0)[1:]
    decoded = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
    return decoded.decode("latin-1"), match.end()


def _parse_literal_string(data: bytes, pos: int) -> tuple[bytes, int]:
    pos += 1  # skip '('
    depth = 1
    out = bytearray()
    while depth > 0:
        if pos >= len(data):
            raise PdfRefusal(RefusalReason.CORRUPT, "unterminated literal string")
        c = data[pos]
        if c == 0x5C:  # backslash: the following byte is escaped, whatever it is
            pos += 1
            if pos >= len(data):
                raise PdfRefusal(RefusalReason.CORRUPT, "unterminated escape in literal string")
            out.append(data[pos])
            pos += 1
            continue
        if c == 0x28:  # '('
            depth += 1
        elif c == 0x29:  # ')'
            depth -= 1
            if depth == 0:
                pos += 1
                break
        out.append(c)
        pos += 1
    return bytes(out), pos


def _parse_hex_string(data: bytes, pos: int) -> tuple[bytes, int]:
    pos += 1  # skip '<'
    end = data.find(b">", pos)
    if end == -1:
        raise PdfRefusal(RefusalReason.CORRUPT, "unterminated hex string")
    digits = re.sub(rb"\s+", b"", data[pos:end])
    if len(digits) % 2:
        digits += b"0"
    try:
        value = bytes.fromhex(digits.decode("ascii"))
    except ValueError as error:
        raise PdfRefusal(RefusalReason.CORRUPT, f"malformed hex string: {error}") from error
    return value, end + 1


# The recursive-descent parser's own depth bound, independent of `resolve`'s
# reference-chain bound above: an array or dictionary nested this deep is not a
# PDF a scanner ever produced, and Python's own call-stack limit is not a
# refusal this module controls the wording of. Refusing before that limit is
# what keeps a pathological file a named PdfRefusal rather than an uncaught
# RecursionError — GOVERNANCE's "fail closed, always" applies to this parser's
# own crash modes as much as to the documents it reads.
_MAX_OBJECT_NESTING: Final = 100


def _parse_array(data: bytes, pos: int, depth: int) -> tuple[list, int]:
    pos += 1  # skip '['
    items: list[Any] = []
    while True:
        pos = _skip_ws_comments(data, pos)
        if pos >= len(data):
            raise PdfRefusal(RefusalReason.CORRUPT, "unterminated array")
        if data[pos : pos + 1] == b"]":
            return items, pos + 1
        value, pos = parse_object(data, pos, depth + 1)
        items.append(value)


def _parse_dict(data: bytes, pos: int, depth: int) -> tuple[dict, int]:
    pos += 2  # skip '<<'
    result: dict[str, Any] = {}
    while True:
        pos = _skip_ws_comments(data, pos)
        if pos >= len(data):
            raise PdfRefusal(RefusalReason.CORRUPT, "unterminated dictionary")
        if data[pos : pos + 2] == b">>":
            return result, pos + 2
        if data[pos : pos + 1] != b"/":
            raise PdfRefusal(RefusalReason.CORRUPT, f"dictionary key is not a name at byte {pos}")
        key, pos = _parse_name(data, pos)
        pos = _skip_ws_comments(data, pos)
        value, pos = parse_object(data, pos, depth + 1)
        result[key] = value


def _parse_number_token(data: bytes, pos: int) -> tuple[int | float, int] | None:
    match = _NUMBER_RE.match(data, pos)
    if not match:
        return None
    text = match.group(0)
    return (float(text) if b"." in text else int(text)), match.end()


def parse_object(data: bytes, pos: int, depth: int = 0) -> tuple[Any, int]:
    """Parse one PDF object starting at `pos`. Returns `(value, next_pos)`.

    `depth` counts array/dictionary nesting, never top-level calls: `_get_object`
    and `_parse_xref_table` both call this at `depth=0` for a fresh object, so
    depth reflects how deep *inside* one object's structure the parser has gone.
    """
    if depth > _MAX_OBJECT_NESTING:
        raise PdfRefusal(RefusalReason.CORRUPT, "object nesting is too deep, likely malformed")
    pos = _skip_ws_comments(data, pos)
    if pos >= len(data):
        raise PdfRefusal(RefusalReason.CORRUPT, "unexpected end of file while parsing an object")
    c = data[pos : pos + 1]
    if c == b"/":
        return _parse_name(data, pos)
    if c == b"(":
        return _parse_literal_string(data, pos)
    if data[pos : pos + 2] == b"<<":
        return _parse_dict(data, pos, depth)
    if c == b"<":
        return _parse_hex_string(data, pos)
    if c == b"[":
        return _parse_array(data, pos, depth)
    if data[pos : pos + 4] == b"true" and _word_boundary(data, pos + 4):
        return True, pos + 4
    if data[pos : pos + 5] == b"false" and _word_boundary(data, pos + 5):
        return False, pos + 5
    if data[pos : pos + 4] == b"null" and _word_boundary(data, pos + 4):
        return None, pos + 4

    number = _parse_number_token(data, pos)
    if number is None:
        raise PdfRefusal(RefusalReason.CORRUPT, f"unrecognized object syntax at byte {pos}")
    value, end = number
    if isinstance(value, int) and value >= 0:
        # Lookahead for "N G R": a reference is two non-negative integers and a
        # bare "R", so a plain number must not be mistaken for one that merely
        # happens to be followed by another number for unrelated reasons.
        pos2 = _skip_ws_comments(data, end)
        second = _parse_number_token(data, pos2)
        if second is not None and isinstance(second[0], int) and second[0] >= 0:
            gen, end2 = second
            pos3 = _skip_ws_comments(data, end2)
            if data[pos3 : pos3 + 1] == b"R" and _word_boundary(data, pos3 + 1):
                return PdfRef(value, gen), pos3 + 1
    return value, end


# --- Cross-reference table and object lookup ------------------------------------


def _find_startxref(data: bytes) -> int:
    index = data.rfind(b"startxref")
    if index == -1:
        raise PdfRefusal(RefusalReason.CORRUPT, "no startxref keyword")
    match = re.search(rb"startxref\s+(\d+)", data[index:])
    if not match:
        raise PdfRefusal(RefusalReason.CORRUPT, "startxref names no offset")
    return int(match.group(1))


def _parse_xref_table(data: bytes, offset: int) -> tuple[dict[int, int], dict[str, Any]]:
    """The one xref section at `offset`, and its trailer.

    Incremental updates (`/Prev` chains) and cross-reference *streams* (PDF 1.5+)
    are both a documented limit, refused by name: single-section classic xref
    tables cover every scanned PDF this door will actually be handed.
    """
    if offset < 0 or offset >= len(data):
        raise PdfRefusal(RefusalReason.CORRUPT, "startxref offset falls outside the file")
    match = re.match(rb"xref\s*", data[offset:])
    if not match:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            "only classic (non-stream) cross-reference tables are decodable here",
        )
    pos = offset + match.end()
    entries: dict[int, int] = {}
    while True:
        sub_match = re.match(rb"(\d+)\s+(\d+)\s*[\r\n]+", data[pos:])
        if not sub_match:
            break
        start_num, count = int(sub_match.group(1)), int(sub_match.group(2))
        pos += sub_match.end()
        for i in range(count):
            row = data[pos : pos + 20]
            if len(row) < 18:
                raise PdfRefusal(RefusalReason.CORRUPT, "truncated cross-reference entry")
            kind = row[17:18]
            if kind == b"n":
                try:
                    offset_value = int(row[0:10])
                except ValueError as error:
                    raise PdfRefusal(
                        RefusalReason.CORRUPT,
                        f"cross-reference entry offset is not numeric: {error}",
                    ) from error
                entries.setdefault(start_num + i, offset_value)
            elif kind != b"f":
                raise PdfRefusal(RefusalReason.CORRUPT, "cross-reference entry is neither n nor f")
            pos += 20

    trailer_match = re.match(rb"\s*trailer\s*", data[pos:])
    if not trailer_match:
        raise PdfRefusal(RefusalReason.CORRUPT, "xref table has no trailer")
    pos = pos + trailer_match.end()
    trailer, _ = parse_object(data, pos)
    if not isinstance(trailer, dict):
        raise PdfRefusal(RefusalReason.CORRUPT, "trailer is not a dictionary")
    if "Prev" in trailer:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            "incremental-update PDFs (/Prev cross-reference chains) are a documented limit",
        )
    return entries, trailer


def _get_object(data: bytes, xref: dict[int, int], num: int) -> Any:
    if num not in xref:
        raise PdfRefusal(RefusalReason.CORRUPT, f"object {num} is not in the cross-reference table")
    offset = xref[num]
    if offset < 0 or offset >= len(data):
        raise PdfRefusal(RefusalReason.CORRUPT, f"object {num} offset falls outside the file")
    header = re.match(rb"(\d+)\s+(\d+)\s+obj\s*", data[offset:])
    if not header or int(header.group(1)) != num:
        raise PdfRefusal(RefusalReason.CORRUPT, f"object {num} has no valid indirect-object header")
    pos = offset + header.end()
    value, pos = parse_object(data, pos)

    stream_probe = _skip_ws_comments(data, pos)
    if data[stream_probe : stream_probe + 6] != b"stream":
        return value
    if not isinstance(value, dict):
        raise PdfRefusal(RefusalReason.CORRUPT, "stream keyword without a preceding dictionary")
    body_start = stream_probe + 6
    if data[body_start : body_start + 2] == b"\r\n":
        body_start += 2
    elif data[body_start : body_start + 1] == b"\n":
        body_start += 1
    else:
        raise PdfRefusal(RefusalReason.CORRUPT, "stream keyword not followed by an end-of-line")
    length = value.get("Length")
    if isinstance(length, PdfRef):
        length = resolve(length, data, xref)
    if not isinstance(length, int) or length < 0:
        raise PdfRefusal(
            RefusalReason.CORRUPT, "stream /Length is missing or not a non-negative integer"
        )
    body_end = body_start + length
    if body_end > len(data):
        raise PdfRefusal(RefusalReason.CORRUPT, "stream runs past the end of the file")
    after = _skip_ws_comments(data, body_end)
    if data[after : after + 9] != b"endstream":
        raise PdfRefusal(RefusalReason.CORRUPT, "declared stream /Length does not reach endstream")
    return PdfStream(value, data[body_start:body_end])


def resolve(obj: Any, data: bytes, xref: dict[int, int], depth: int = 0) -> Any:
    """Follow indirect references to their value. Bounded, so a cycle refuses."""
    if depth > 32:
        raise PdfRefusal(RefusalReason.CORRUPT, "reference chain too deep, likely a cycle")
    if isinstance(obj, PdfRef):
        return resolve(_get_object(data, xref, obj.num), data, xref, depth + 1)
    return obj


# --- The page tree ---------------------------------------------------------------


def _page_list(data: bytes, xref: dict[int, int], trailer: dict[str, Any]) -> list[dict[str, Any]]:
    if "Encrypt" in trailer:
        raise PdfRefusal(RefusalReason.UNSUPPORTED_VARIANT, "encrypted PDFs are a documented limit")
    root = resolve(trailer.get("Root"), data, xref)
    if not isinstance(root, dict):
        raise PdfRefusal(RefusalReason.CORRUPT, "trailer /Root does not resolve to a dictionary")
    pages_root = resolve(root.get("Pages"), data, xref)

    result: list[dict[str, Any]] = []
    _walk_pages(pages_root, data, xref, result, {}, depth=0, seen=frozenset())
    if not result:
        raise PdfRefusal(RefusalReason.CORRUPT, "the PDF declares no pages")
    if len(result) > MAX_PAGES:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the PDF declares {len(result)} pages, past the {MAX_PAGES}-page admission limit",
        )
    return result


def _walk_pages(
    node: Any,
    data: bytes,
    xref: dict[int, int],
    result: list[dict[str, Any]],
    inherited: dict[str, Any],
    *,
    depth: int,
    seen: frozenset,
) -> None:
    if depth > 64:
        raise PdfRefusal(RefusalReason.CORRUPT, "page tree is too deep, likely a cycle")
    if not isinstance(node, dict):
        raise PdfRefusal(RefusalReason.CORRUPT, "page tree node is not a dictionary")

    merged = dict(inherited)
    for key in ("Resources", "Rotate"):
        if key in node:
            merged[key] = node[key]

    node_type = node.get("Type")
    if "Kids" in node or node_type == "Pages":
        kids = resolve(node.get("Kids"), data, xref)
        if not isinstance(kids, list):
            raise PdfRefusal(RefusalReason.CORRUPT, "/Kids is not an array")
        for kid_ref in kids:
            key = (kid_ref.num, kid_ref.gen) if isinstance(kid_ref, PdfRef) else id(kid_ref)
            if key in seen:
                raise PdfRefusal(RefusalReason.CORRUPT, "page tree contains a cycle")
            kid = resolve(kid_ref, data, xref)
            _walk_pages(kid, data, xref, result, merged, depth=depth + 1, seen=seen | {key})
    elif node_type == "Page":
        result.append({**merged, **node})
    else:
        raise PdfRefusal(RefusalReason.CORRUPT, f"unknown page tree node type {node_type!r}")


def _page_image_object(page: dict[str, Any], data: bytes, xref: dict[int, int]) -> PdfStream:
    rotate = page.get("Rotate", 0)
    if rotate not in (0, None):
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"page declares /Rotate {rotate}; rendering a rotated page is a documented limit",
        )
    resources = resolve(page.get("Resources"), data, xref)
    if not isinstance(resources, dict):
        raise PdfRefusal(RefusalReason.CORRUPT, "page has no /Resources dictionary")
    xobjects = resolve(resources.get("XObject"), data, xref)
    if not isinstance(xobjects, dict) or len(xobjects) != 1:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            "a decodable page carries exactly one XObject; this page carries "
            f"{len(xobjects) if isinstance(xobjects, dict) else 'none'}",
        )
    ((_, only_ref),) = xobjects.items()
    xobj = resolve(only_ref, data, xref)
    if not isinstance(xobj, PdfStream):
        raise PdfRefusal(RefusalReason.CORRUPT, "the page's XObject is not a stream")
    if xobj.dict.get("Subtype") != "Image":
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT, "the page's one XObject is not an /Image"
        )
    return xobj


# --- Decoding the one image XObject a page may carry ----------------------------

_RAW_COLORSPACES: Final = {"DeviceGray": (1, 0), "DeviceRGB": (3, 2)}


def _decode_image_xobject(xobj: PdfStream, data: bytes, xref: dict[int, int]) -> tuple[bytes, str]:
    fields = xobj.dict
    width, height = fields.get("Width"), fields.get("Height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise PdfRefusal(RefusalReason.CORRUPT, "image XObject has no valid /Width or /Height")
    # Bounded before `expected` is computed from it, for the same reason
    # `image_formats._geometry` bounds a raster header: the inflate below is
    # sized by a number this file declared about itself.
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the page's image is {width}x{height}, past the admission limits "
            f"({MAX_DIMENSION} per side, {MAX_PIXELS} pixels)",
        )

    bits = fields.get("BitsPerComponent", 8)
    colorspace = resolve(fields.get("ColorSpace"), data, xref)
    image_filter = resolve(fields.get("Filter"), data, xref)
    if isinstance(image_filter, list):
        if len(image_filter) != 1:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT, "chained image filters are a documented limit"
            )
        image_filter = image_filter[0]

    if image_filter == "DCTDecode":
        try:
            geometry = validate_jpeg(xobj.raw)
        except FormatRefusal as error:
            raise PdfRefusal(
                RefusalReason.CORRUPT, f"embedded JPEG failed to validate: {error}"
            ) from error
        if geometry.width != width or geometry.height != height:
            raise PdfRefusal(
                RefusalReason.CORRUPT, "embedded JPEG geometry disagrees with the image dictionary"
            )
        return xobj.raw, "jpeg"

    if image_filter in (None, "FlateDecode"):
        # Geometry is validated *before* any decompression happens, so `expected`
        # is known and the inflate below can be bounded by it — the same
        # zip-bomb defence `image_formats.validate_png` uses, applied here for
        # the same reason: this is the one place in the pipeline that inflates
        # bytes nobody has verified yet.
        if bits != 8:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"{bits}-bit-per-component raw samples are a documented limit; only 8-bit is decodable here",
            )
        if colorspace not in _RAW_COLORSPACES:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"colorspace {colorspace!r} is a documented limit; only DeviceGray/DeviceRGB are decodable here",
            )
        channels, color_type = _RAW_COLORSPACES[colorspace]
        expected = width * height * channels

        if image_filter == "FlateDecode":
            decompressor = zlib.decompressobj()
            try:
                samples = decompressor.decompress(xobj.raw, expected + 1)
            except zlib.error as error:
                raise PdfRefusal(
                    RefusalReason.CORRUPT, f"image stream would not inflate ({error})"
                ) from error
            if len(samples) > expected:
                raise PdfRefusal(
                    RefusalReason.CORRUPT,
                    "raw image sample count disagrees with its declared geometry",
                )
            if not decompressor.eof:
                raise PdfRefusal(RefusalReason.CORRUPT, "image stream is truncated")
        else:
            samples = xobj.raw

        if len(samples) != expected:
            raise PdfRefusal(
                RefusalReason.CORRUPT,
                "raw image sample count disagrees with its declared geometry",
            )
        return _encode_png(width, height, color_type, channels, samples), "png"

    raise PdfRefusal(
        RefusalReason.UNSUPPORTED_VARIANT,
        f"image filter {image_filter!r} is a documented limit; only DCTDecode and FlateDecode are decodable here",
    )


def _png_chunk(tag: bytes, chunk_data: bytes) -> bytes:
    return (
        struct.pack(">I", len(chunk_data))
        + tag
        + chunk_data
        + struct.pack(">I", zlib.crc32(tag + chunk_data))
    )


def _encode_png(width: int, height: int, color_type: int, channels: int, samples: bytes) -> bytes:
    """The minimal general PNG encoder a PDF-extracted raw raster needs.

    Deliberately separate from `common/imaging.py`'s grayscale-only encoder: this
    one carries whatever colour type the extracted samples actually are (grayscale
    or RGB), which that module is explicit it is not for (`common/imaging.py`
    serves the Designator and Perlector on synthetic input, never the door).
    """
    row_bytes = width * channels
    rows = bytearray()
    for row in range(height):
        rows.append(0)  # filter type 0 (None) on every scanline
        rows.extend(samples[row * row_bytes : (row + 1) * row_bytes])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


# --- Public surface: door.py is the only caller ---------------------------------


def _prepare(data: bytes) -> tuple[dict[int, int], dict[str, Any]]:
    if not data.startswith(PDF_SIGNATURE):
        raise PdfRefusal(RefusalReason.CORRUPT, "not a PDF: missing header")
    return _parse_xref_table(data, _find_startxref(data))


def count_pages(data: bytes) -> int:
    """How many pages this PDF declares, without rendering any of them."""
    xref, trailer = _prepare(data)
    return len(_page_list(data, xref, trailer))


def render_page(data: bytes, page_index: int) -> tuple[bytes, str]:
    """Render one page (0-based) to standalone image bytes and their format name.

    Raises `PdfRefusal` naming exactly why when the page is not the single-image
    scanned shape this module handles — never a guess, never a partial image.
    """
    xref, trailer = _prepare(data)
    pages = _page_list(data, xref, trailer)
    if not (0 <= page_index < len(pages)):
        raise PdfRefusal(
            RefusalReason.CORRUPT, f"page index {page_index} is out of range for {len(pages)} pages"
        )
    xobj = _page_image_object(pages[page_index], data, xref)
    return _decode_image_xobject(xobj, data, xref)
