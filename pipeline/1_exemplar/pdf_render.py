"""A bounded, door-private PDF page renderer. Standard library only.

**This module is not reachable under the shipped admission list, and that is
deliberate.** `config/admitted_formats.toml` ships `pdf = "refuse"`, so nothing
here runs until Tyrel turns that row to `render-pages`. It stays in the tree,
tested, because the work is not wasted — it is not yet trustworthy. Read the next
paragraph before turning the row.

**What this module does not do, stated first because it is the important part.**
It does not interpret a page's content stream. `_page_image_object` accepts a page
whose `/Resources/XObject` dictionary holds exactly one `/Image` entry, and
`render_page` returns that object's decoded bytes — it never opens `/Contents`,
never verifies that a `Do` operator paints that image, never reads the graphics
state, the transformation matrix, or any clip. So it cannot tell a scanned page
apart from a page that draws text and vector marks *beside* the one image it
carries, and on such a page it would seal the image and lose everything else. That
is GOALS 1 — a missed act is worse than a poorly read act — failing in the one
place it must not, which is why the admission list refuses PDF rather than this
module pretending otherwise. Proving that exactly one image is painted exactly as
recorded, with no other visible content, is its own spec and is not built here.

**Named limits, the same shape of decision `image_formats.py` makes for interlaced
PNG and BigTIFF.** Rendering arbitrary PDF content is a document layout engine,
fonts, content-stream interpretation and colour management, and building one from
`zlib`/`struct` would be exactly the half-handling `common/imaging.py`'s docstring
already refuses. What this handles, within that scope, is the shape this project's
sources actually are: **a flatbed scan saved as PDF, one image per page.**

A page renders here only when: the PDF uses a classic (non-stream) cross-reference
table with no `/Prev` chain, ends at its own `%%EOF` with nothing appended, is not
encrypted, declares no page rotation, and its `/Resources/XObject` dictionary holds
**exactly one** entry, an `/Image` XObject in `DeviceGray` or `DeviceRGB` with no
sample-remapping `/Decode` array, compressed with `DCTDecode` (embedded JPEG,
structurally validated by `image_formats.validate_jpeg` against the colour space
the dictionary declares, and stored as-is) or `FlateDecode` (raw 8-bit samples,
re-encoded as a PNG through this module's own minimal encoder). Anything else —
vector content, multiple images, CCITT/JBIG2/JPX filters, indexed or CMYK colour,
a `/Decode` array, incremental updates, encryption, rotation — is refused **by
name**, never guessed at. A page that fails this test is not a page this door can
seal; it is held for a human to look at, exactly like any other refusal.

**Every walk is bounded before it begins, including the ones an attacker sizes.**
The cross-reference parser bounds declared entries *and* subsection headers, and
matches in place rather than re-slicing the file per iteration — a subsection may
legally declare zero entries, so an entry cap alone leaves the header count
unbounded and each fresh `data[pos:]` copy turns that into quadratic work on a file
well under the admission ceiling. The page-tree walk bounds intermediate nodes as
well as leaf pages. And the document is parsed **once per file**, by
`open_document`, rather than once per page: `render_page` takes the prepared
document, so admitting an N-page scan costs one parse rather than N.

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
from image_formats import (
    MAX_DIMENSION,
    MAX_PIXELS,
    MAX_PNG_DECODED_BYTES,
    FormatRefusal,
    validate_jpeg,
)

PDF_SIGNATURE: Final = b"%PDF-"

# One scanned register volume is hundreds of pages, not hundreds of thousands. A
# page count read out of an untrusted file is a loop bound an attacker writes, so
# it is bounded here before the fan-out ever runs.
MAX_PAGES: Final = 5_000
# A scan-only PDF needs a small, bounded number of objects per page. Without a
# separate xref bound, a 64 MiB source can still allocate millions of dictionary
# entries before page counting begins.
MAX_XREF_ENTRIES: Final = 200_000
# And a separate bound on the number of *subsection headers*, because a subsection
# may legally declare zero entries: `MAX_XREF_ENTRIES` counts entries, so a file of
# `0 0` headers never reaches it and the header loop is bounded only by the
# remaining bytes. Two seats measured the resulting blow-up independently.
MAX_XREF_SUBSECTIONS: Final = 10_000
# Leaf pages are bounded by MAX_PAGES; the intermediate /Pages nodes between them
# are not, and each one costs an object lookup. A page tree with more nodes than
# twice its own page limit is not a tree a scanner produced.
MAX_PAGE_TREE_NODES: Final = 2 * MAX_PAGES

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
_MAX_OBJECT_ITEMS: Final = 10_000


def _parse_array(data: bytes, pos: int, depth: int) -> tuple[list, int]:
    pos += 1  # skip '['
    items: list[Any] = []
    while True:
        pos = _skip_ws_comments(data, pos)
        if pos >= len(data):
            raise PdfRefusal(RefusalReason.CORRUPT, "unterminated array")
        if data[pos : pos + 1] == b"]":
            return items, pos + 1
        if len(items) >= _MAX_OBJECT_ITEMS:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"an array exceeds the {_MAX_OBJECT_ITEMS}-object item limit",
            )
        value, pos = parse_object(data, pos, depth + 1)
        items.append(value)


def _parse_dict(data: bytes, pos: int, depth: int) -> tuple[dict, int]:
    pos += 2  # skip '<<'
    result: dict[str, Any] = {}
    parsed_items = 0
    while True:
        pos = _skip_ws_comments(data, pos)
        if pos >= len(data):
            raise PdfRefusal(RefusalReason.CORRUPT, "unterminated dictionary")
        if data[pos : pos + 2] == b">>":
            return result, pos + 2
        if parsed_items >= _MAX_OBJECT_ITEMS:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"a dictionary exceeds the {_MAX_OBJECT_ITEMS}-object item limit",
            )
        if data[pos : pos + 1] != b"/":
            raise PdfRefusal(RefusalReason.CORRUPT, f"dictionary key is not a name at byte {pos}")
        key, pos = _parse_name(data, pos)
        pos = _skip_ws_comments(data, pos)
        value, pos = parse_object(data, pos, depth + 1)
        result[key] = value
        parsed_items += 1


def _parse_number_token(data: bytes, pos: int) -> tuple[int | float, int] | None:
    match = _NUMBER_RE.match(data, pos)
    if not match:
        return None
    text = match.group(0)
    try:
        value = float(text) if b"." in text else int(text)
    except (OverflowError, ValueError) as error:
        raise PdfRefusal(
            RefusalReason.CORRUPT, "numeric object is too large to decode safely"
        ) from error
    return value, match.end()


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


_XREF_HEADER_RE: Final = re.compile(rb"xref\s*")
_XREF_SUBSECTION_RE: Final = re.compile(rb"(\d+)\s+(\d+)\s*[\r\n]+")
_TRAILER_KEYWORD_RE: Final = re.compile(rb"\s*trailer\s*")
_INDIRECT_HEADER_RE: Final = re.compile(rb"(\d+)\s+(\d+)\s+obj\s*")
# The file must *end* at its own trailer. `image_formats.validate_jpeg` refuses
# trailing bytes after EOI for the same reason and says so in the same words: they
# are either a second document or an appended payload, and neither is the single
# page the door believes it admitted. Without this the parser reads back from the
# last `startxref` and never looks at what follows, so an arbitrary payload rides
# inside the digest the run authority seals while nothing ever inspected it.
_STARTXREF_TAIL_RE: Final = re.compile(rb"startxref\s+(\d+)\s*%%EOF\s*\Z")


def _find_startxref(data: bytes) -> int:
    index = data.rfind(b"startxref")
    if index == -1:
        raise PdfRefusal(RefusalReason.CORRUPT, "no startxref keyword")
    match = _STARTXREF_TAIL_RE.match(data, index)
    if not match:
        raise PdfRefusal(
            RefusalReason.CORRUPT,
            "the file does not end at its own startxref/%%EOF trailer; bytes after a "
            "PDF's end are either a second document or an appended payload, and the "
            "digest sealed for this source would cover bytes nothing inspected",
        )
    try:
        return int(match.group(1))
    except ValueError as error:
        raise PdfRefusal(
            RefusalReason.CORRUPT, "startxref offset is too large to decode safely"
        ) from error


def _parse_xref_table(data: bytes, offset: int) -> tuple[dict[int, int], dict[str, Any]]:
    """The one xref section at `offset`, and its trailer.

    Incremental updates (`/Prev` chains) and cross-reference *streams* (PDF 1.5+)
    are both a documented limit, refused by name: single-section classic xref
    tables cover every scanned PDF this door will actually be handed.
    """
    if offset < 0 or offset >= len(data):
        raise PdfRefusal(RefusalReason.CORRUPT, "startxref offset falls outside the file")
    match = _XREF_HEADER_RE.match(data, offset)
    if not match:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            "only classic (non-stream) cross-reference tables are decodable here",
        )
    pos = match.end()
    entries: dict[int, int] = {}
    declared_entries = 0
    subsections = 0
    while True:
        # Matched in place. `re.match(pattern, data[pos:])` copies the rest of the
        # file on every iteration, which is what turns an unbounded header count
        # into quadratic work rather than merely a long loop.
        sub_match = _XREF_SUBSECTION_RE.match(data, pos)
        if not sub_match:
            break
        subsections += 1
        if subsections > MAX_XREF_SUBSECTIONS:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the PDF declares more than the {MAX_XREF_SUBSECTIONS}-subsection "
                "cross-reference limit; a subsection declaring no entries costs "
                "nothing against the entry limit and everything against this one",
            )
        try:
            start_num, count = int(sub_match.group(1)), int(sub_match.group(2))
        except ValueError as error:
            raise PdfRefusal(
                RefusalReason.CORRUPT,
                "cross-reference subsection bounds are too large to decode safely",
            ) from error
        declared_entries += count
        if declared_entries > MAX_XREF_ENTRIES:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the PDF declares more than the {MAX_XREF_ENTRIES}-entry "
                "cross-reference entry limit",
            )
        pos = sub_match.end()
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

    trailer_match = _TRAILER_KEYWORD_RE.match(data, pos)
    if not trailer_match:
        raise PdfRefusal(RefusalReason.CORRUPT, "xref table has no trailer")
    pos = trailer_match.end()
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
    header = _INDIRECT_HEADER_RE.match(data, offset)
    if not header or int(header.group(1)) != num:
        raise PdfRefusal(RefusalReason.CORRUPT, f"object {num} has no valid indirect-object header")
    pos = header.end()
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
    _walk_pages(
        pages_root,
        data,
        xref,
        result,
        {},
        depth=0,
        seen=frozenset(),
        visited=set(),
    )
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
    visited: set,
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
        declared_count = resolve(node.get("Count"), data, xref)
        if (
            not isinstance(declared_count, int)
            or isinstance(declared_count, bool)
            or declared_count < 0
        ):
            raise PdfRefusal(
                RefusalReason.CORRUPT, "page-tree /Count is not a non-negative integer"
            )
        if declared_count > MAX_PAGES:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the page tree declares {declared_count} pages, past the "
                f"{MAX_PAGES}-page admission limit",
            )
        kids = resolve(node.get("Kids"), data, xref)
        if not isinstance(kids, list):
            raise PdfRefusal(RefusalReason.CORRUPT, "/Kids is not an array")
        if len(kids) > MAX_PAGES:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"a page-tree node carries {len(kids)} children, past the "
                f"{MAX_PAGES}-page admission limit",
            )
        before = len(result)
        for kid_ref in kids:
            key = (kid_ref.num, kid_ref.gen) if isinstance(kid_ref, PdfRef) else id(kid_ref)
            if key in seen:
                raise PdfRefusal(RefusalReason.CORRUPT, "page tree contains a cycle")
            if key in visited:
                raise PdfRefusal(
                    RefusalReason.CORRUPT,
                    "page tree references one page or page-tree object more than once",
                )
            visited.add(key)
            if len(visited) > MAX_PAGE_TREE_NODES:
                raise PdfRefusal(
                    RefusalReason.UNSUPPORTED_VARIANT,
                    f"the page tree visits more than {MAX_PAGE_TREE_NODES} nodes; "
                    "MAX_PAGES bounds the leaves, and this bounds the branches "
                    "between them, each of which costs its own object lookup",
                )
            kid = resolve(kid_ref, data, xref)
            _walk_pages(
                kid,
                data,
                xref,
                result,
                merged,
                depth=depth + 1,
                seen=seen | {key},
                visited=visited,
            )
        actual_count = len(result) - before
        if actual_count != declared_count:
            raise PdfRefusal(
                RefusalReason.CORRUPT,
                f"page-tree /Count declares {declared_count}, but its descendants contain "
                f"{actual_count} pages",
            )
    elif node_type == "Page":
        if len(result) >= MAX_PAGES:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the PDF contains more than the {MAX_PAGES}-page admission limit",
            )
        result.append({**merged, **node})
    else:
        raise PdfRefusal(RefusalReason.CORRUPT, f"unknown page tree node type {node_type!r}")


def _page_image_object(page: dict[str, Any], data: bytes, xref: dict[int, int]) -> PdfStream:
    rotate = page.get("Rotate", 0)
    # `rotate is not None and rotate != 0` would pass `/Rotate false`, because
    # `False == 0` in Python. `/Rotate` is defined as an integer, so a boolean
    # there is malformed and is refused rather than read as "unrotated".
    if rotate is not None and (isinstance(rotate, bool) or not isinstance(rotate, int) or rotate):
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"page declares /Rotate {rotate!r}; rendering a rotated page is a documented "
            "limit, and a /Rotate that is not an integer is not a rotation this can read",
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
# The keys that change what a page's pixels *look like* and that this module does
# not apply. A page carrying one of them renders to something other than what the
# PDF declares, so it is refused by name rather than rendered with the key dropped.
# `/Decode` is handled separately because its identity value is legal and harmless.
_APPEARANCE_KEYS: Final = ("ImageMask", "Mask", "SMask", "DecodeParms", "Intent")


def _refuse_sample_remapping(fields: dict[str, Any], channels: int) -> None:
    """Refuse a page whose samples do not mean what their raw values say.

    `/Decode [1 0]` on a DeviceGray image declares that sample 0 renders as maximum
    intensity — an ordinary way to store an inverted scan. Dropping it silently
    seals a black page where the source declares a white one: no error, no refusal,
    no signal of any kind, and an unrecorded transform is exactly what
    ARCHITECTURE's third invariant cannot survive. The identity array is legal and
    means nothing, so it passes; anything else is a documented limit.
    """
    decode = fields.get("Decode")
    if decode is not None and list(decode or ()) != [0, 1] * channels:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"the image declares /Decode {decode!r}, which remaps its samples; this "
            "module does not apply it, and rendering the raw values would seal a page "
            "the source says is a different one",
        )
    for key in _APPEARANCE_KEYS:
        if key in fields:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the image declares /{key}, which changes what the page shows; "
                "applying it is a documented limit and ignoring it would seal a page "
                "that is not the one the source declares",
            )


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

    # Read once, for every filter, before either branch. `/ColorSpace` is legally an
    # array — `[/ICCBased 5 0 R]` and `[/Indexed ...]` are ordinary scanner output —
    # and a list is unhashable, so testing membership against a dict raised a bare
    # `TypeError` out of this module rather than the `PdfRefusal` the door catches.
    # That aborted the whole run: a healthy source later in the same submission got
    # no decision at all, which is invariant #3 failing, not merely a missing name.
    if colorspace is not None and not isinstance(colorspace, str):
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            "the image declares a /ColorSpace that is not a plain device name "
            f"({type(colorspace).__name__}); ICCBased, Indexed and every other "
            "array colour space is a documented limit",
        )
    if colorspace not in _RAW_COLORSPACES:
        raise PdfRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"colorspace {colorspace!r} is a documented limit; only DeviceGray/DeviceRGB "
            "are decodable here",
        )
    channels, color_type = _RAW_COLORSPACES[colorspace]
    _refuse_sample_remapping(fields, channels)

    if image_filter == "DCTDecode":
        try:
            # The colour space the dictionary declares is checked *against the
            # embedded JPEG's own frame*, not merely alongside it. Without this the
            # DCT branch never consulted /ColorSpace at all, so a page declaring
            # /DeviceCMYK with a four-component JPEG rendered and would have been
            # sealed — while this module's docstring said CMYK was refused by name.
            geometry = validate_jpeg(xobj.raw, expected_components=channels)
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
        expected = width * height * channels
        if expected > MAX_PNG_DECODED_BYTES:
            raise PdfRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"the page declares {expected} decoded sample bytes, past the "
                f"{MAX_PNG_DECODED_BYTES}-byte decoded-byte admission limit",
            )

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
            if decompressor.unused_data or decompressor.unconsumed_tail:
                raise PdfRefusal(
                    RefusalReason.CORRUPT,
                    "image stream has trailing bytes after its compressed data",
                )
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


class PdfDocument(NamedTuple):
    """One PDF parsed once: its cross-reference table, trailer, and page list.

    `render_page` takes one of these rather than raw bytes, because it used to
    re-parse the whole cross-reference table and re-walk the whole page tree on
    every call and the door calls it once per page. An N-page scan therefore paid
    the parse N+1 times, which two seats measured as cleanly quadratic and which
    made the declared `MAX_PAGES` limit unreachable in practice — a bound the
    implementation cannot serve is a bound in name only.
    """

    data: bytes
    xref: dict[int, int]
    trailer: dict[str, Any]
    pages: list[dict[str, Any]]


def open_document(data: bytes) -> PdfDocument:
    """Parse one PDF's structure, once. Raises `PdfRefusal` naming why if it will not."""
    if not data.startswith(PDF_SIGNATURE):
        raise PdfRefusal(RefusalReason.CORRUPT, "not a PDF: missing header")
    xref, trailer = _parse_xref_table(data, _find_startxref(data))
    return PdfDocument(data, xref, trailer, _page_list(data, xref, trailer))


def count_pages(data: bytes) -> int:
    """How many pages this PDF declares, without rendering any of them."""
    return len(open_document(data).pages)


def render_page(document: PdfDocument, page_index: int) -> tuple[bytes, str]:
    """Render one page (0-based) to standalone image bytes and their format name.

    Raises `PdfRefusal` naming exactly why when the page is not the single-image
    scanned shape this module handles — never a guess, never a partial image.

    **It does not read the page's content stream**, so "the page carries exactly one
    image" is a claim about the page's *resources*, not about what the page paints.
    See this module's docstring; that gap is why the admission list refuses PDF.
    """
    if not (0 <= page_index < len(document.pages)):
        raise PdfRefusal(
            RefusalReason.CORRUPT,
            f"page index {page_index} is out of range for {len(document.pages)} pages",
        )
    xobj = _page_image_object(document.pages[page_index], document.data, document.xref)
    return _decode_image_xobject(xobj, document.data, document.xref)
