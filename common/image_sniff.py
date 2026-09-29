"""The byte-signature table for page sources, and the test for "could be a page".

The Exemplar door (`pipeline/1_exemplar`) and the submit door (`operations/submit`)
both read the signatures here. A signature match is not proof the bytes are valid;
the door's decoder decides that. Bytes with no known signature still get a generic
Pillow attempt at the door, so `identifies_as_image` applies the same two steps to
an opening prefix: a named signature, else Pillow naming a format.
"""

from __future__ import annotations

import struct
import warnings
from io import BytesIO
from typing import Final

from PIL import Image

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE: Final = b"\xff\xd8"
# BigTIFF shares TIFF's tags and images, only with 64-bit offsets, so it is sniffed
# here as TIFF by name rather than falling through to the unknown-magic case; the
# structural walker in `image_formats` cannot read its offset table and the decoder answers.
TIFF_SIGNATURES: Final = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
PDF_SIGNATURE: Final = b"%PDF-"
# PDFium accepts a PDF header after a bounded leading transport preamble; the door
# reads this much from the head of a streamed source to match that.
PDF_HEADER_PREFIX_BYTES: Final = 1024
GIF_SIGNATURES: Final = (b"GIF87a", b"GIF89a")
BMP_SIGNATURES: Final = (b"BM", b"BA", b"CI", b"CP", b"IC", b"PT")
WEBP_SIGNATURE: Final = b"WEBP"

# The one table sniff() walks, so the formats the door can detect are derived from
# what the sniffer executes rather than hand-copied beside it.
_SIGNATURES: Final = (
    ("png", (PNG_SIGNATURE,)),
    ("jpeg", (JPEG_SIGNATURE,)),
    ("tiff", TIFF_SIGNATURES),
    ("pdf", (PDF_SIGNATURE,)),
    ("gif", GIF_SIGNATURES),
    ("bmp", BMP_SIGNATURES),
)

# ISO base media file format "brand" codes marking a file as HEIC/HEIF, read from
# the `ftyp` box that opens every such container — never from a file extension,
# since admission is by bytes only.
_HEIC_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
)
_AVIF_BRANDS: Final = frozenset({b"avif", b"avis"})
_HEIF_BRANDS: Final = frozenset({b"mif1", b"msf1"})
# A real ftyp box's brand list is a handful of 4-byte codes; this ceiling keeps
# _iso_bmff_image_format's scan near-constant-time regardless of an
# attacker-declared box size or the file's own length.
_FTYP_BRAND_SCAN_CEILING: Final = 16 + 256 * 4

# How much of a file's start every signature reader takes. It must cover what `sniff`
# reads (the PDF header window and the ISO-BMFF brand scan) and enough for Pillow to
# name a header; the tests pin the first.
SIGNATURE_PREFIX_BYTES: Final = 4096


def sniff(data: bytes) -> str | None:
    """The format the bytes' own signature claims, or None for none recognized.

    Signature only — this says what a validator should be asked to prove, not that
    the bytes are a valid instance of it. `admission.py` calls the matching
    validator before ever admitting anything.
    """
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
    readable_end = min(max(box_size, 12), len(data), _FTYP_BRAND_SCAN_CEILING)
    brands = {major_brand} | {data[offset : offset + 4] for offset in range(16, readable_end, 4)}
    if brands & _HEIC_BRANDS:
        return "heic"
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & _HEIF_BRANDS:
        return "heif"
    return None


def identifies_as_image(prefix: bytes) -> bool:
    """Whether an opening prefix is a named format or one Pillow can name.

    Mirrors what the door does with a source: a sniffed signature routes it, and
    unknown bytes get a generic Pillow attempt rather than a refusal on sight.
    """
    if sniff(prefix) is not None:
        return True
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(BytesIO(prefix)):
                return True
    except Exception:
        return False
