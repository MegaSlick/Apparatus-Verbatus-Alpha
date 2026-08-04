"""A bounded, door-private multi-page TIFF renderer. Standard library only.

Fans a multi-page TIFF out into per-page ordinals and renders each directory as its
own page — exactly the shape `pdf_render.py` already gives PDF. Ruling 2026-08-04,
item 2, on the walking skeleton's blanket multi-page refusal: "That is the door's
defect, not the file's. Fan it out, exactly as PDF is fanned out."

**The common case never reaches this module at all.** A single-directory TIFF is
admitted exactly as it always was — sealed as its own unmodified bytes, through
`image_formats.validate_tiff` and `admission.inspect_source`, no rendering, no
re-encoding. This module exists only for the second directory onward: `door.py`
calls `image_formats.tiff_directory_offsets` first, and only a file that
structurally holds more than one directory is fanned out through the functions
below.

**Scope, stated first, the same way `pdf_render.py` states its own.** Every
directory this module can actually render is uncompressed (`Compression` tag = 1),
8 bits per sample, non-tiled (stripped), with `SamplesPerPixel` of 1 (paired with
`PhotometricInterpretation` 0 WhiteIsZero or 1 BlackIsZero) or 3 (paired with 2,
RGB) — the shapes `image_formats.validate_tiff_directory` has already structurally
reconciled against the file's own declared geometry before a single sample is
copied. A directory outside that scope — any other compression codec (LZW,
Deflate, PackBits, CCITT Group 3/4, ...), tiled storage, more than 8 bits per
sample, or an unusual sample layout — has no decoder here yet. That is refused as
`UNSUPPORTED_VARIANT`, ruling 2026-08-04's own wording for exactly this case: a
named gap in this pipeline, never a `CORRUPT` refusal that would tell Tyrel a real
page was damaged when it is not. Extending this to a new compression codec is
additive — a new branch in `render_page`, not a different door.

**Door-private by design**, the same way `pdf_render.py` is: nothing outside
`pipeline/1_exemplar/door.py` imports this module, so a multi-page TIFF is fanned
out and rendered once, at admission, and no later stage has an API to re-render
with (spec 03, test 4).
"""

from typing import Final, NamedTuple

import admission
import image_formats
from admission import RefusalReason, RenderRefusal

# One scanned register volume is hundreds of pages, not hundreds of thousands —
# `image_formats.MAX_TIFF_DIRECTORIES` already bounds the chain walk itself; this
# is the same ceiling, named here for readers of this module who never open that
# one.
MAX_PAGES: Final = image_formats.MAX_TIFF_DIRECTORIES

# `PhotometricInterpretation` values this module can extract. 0 (WhiteIsZero) and
# 1 (BlackIsZero) are both one-sample-per-pixel grayscale, differing only in
# whether a raw sample of 0 renders black or white; 2 is three-sample RGB. Anything
# else (a palette, CMYK, YCbCr, CIE Lab, ...) is a documented limit.
_GRAYSCALE_PHOTOMETRIC: Final = frozenset({0, 1})
_RGB_PHOTOMETRIC: Final = 2


class TiffRenderRefusal(RenderRefusal):
    """One TIFF page, or the whole directory chain, refused with a closed-set reason."""


class TiffDocument(NamedTuple):
    """One multi-page TIFF parsed once: its byte order and every directory offset.

    Parsed once per file rather than once per page — the same reasoning
    `pdf_render.PdfDocument` states for itself — because `door.py` calls
    `render_page` once per fanned-out ordinal and re-walking the whole directory
    chain each time would cost the parse N times over for an N-page scan.
    """

    data: bytes
    endian: str
    offsets: tuple[int, ...]


def open_document(data: bytes) -> TiffDocument:
    """Parse one TIFF's directory chain, once. Raises `TiffRenderRefusal` naming why
    if it will not."""
    try:
        endian, _first_offset = image_formats.read_tiff_header(data)
        offsets = tuple(image_formats.tiff_directory_offsets(data))
    except image_formats.FormatRefusal as error:
        raise TiffRenderRefusal(
            admission.refusal_code_for_format_error(error), str(error)
        ) from error
    return TiffDocument(data, endian, offsets)


def count_pages(data: bytes) -> int:
    """How many image directories this TIFF declares, without rendering any of them."""
    return len(open_document(data).offsets)


def close_document(document: TiffDocument) -> None:
    """A no-op, kept for symmetry with `pdf_render.close_document`.

    A `TiffDocument` holds plain bytes and a tuple of offsets — no native handle —
    so there is nothing to release. `door.py`'s cleanup calls this uniformly
    across every renderer in `_RENDERERS` regardless of which one produced a given
    document, rather than knowing which renderers need closing and which do not.
    """


def render_page(document: TiffDocument, page_index: int) -> tuple[bytes, str]:
    """Render one directory (0-based) to standalone image bytes and their format name.

    Raises `TiffRenderRefusal` naming exactly why when the directory is outside
    this module's scope — never a guess, never a partial image. The bytes
    returned are always a lossless PNG: extracted samples are copied, never
    recompressed lossily, matching Tyrel's ruling that lossless is preferred
    wherever this pipeline chooses an encoding.
    """
    if not (0 <= page_index < len(document.offsets)):
        raise TiffRenderRefusal(
            RefusalReason.CORRUPT,
            f"page index {page_index} is out of range for {len(document.offsets)} "
            "image directories",
        )
    offset = document.offsets[page_index]
    try:
        geometry, storage = image_formats.validate_tiff_directory(
            document.data, document.endian, offset
        )
    except image_formats.FormatRefusal as error:
        raise TiffRenderRefusal(
            admission.refusal_code_for_format_error(error), str(error)
        ) from error

    if storage.is_tiled:
        raise TiffRenderRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            "this page is stored as tiles rather than strips; tiled multi-page TIFF "
            "extraction has no reader yet — a gap in this pipeline, not a decision "
            "about this page",
        )
    if storage.compression != image_formats.TIFF_UNCOMPRESSED:
        raise TiffRenderRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"this page uses compression codec {storage.compression}, which has no "
            "decoder yet for a multi-page TIFF page — a gap in this pipeline, not a "
            "decision about this page",
        )
    if set(storage.bits_per_sample) != {8}:
        raise TiffRenderRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"this page carries {storage.bits_per_sample}-bit samples; only 8-bit "
            "samples have an extractor yet for a multi-page TIFF page",
        )
    if storage.photometric in _GRAYSCALE_PHOTOMETRIC:
        if storage.samples_per_pixel != 1:
            raise TiffRenderRefusal(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"this page declares a grayscale photometric interpretation with "
                f"{storage.samples_per_pixel} samples per pixel; only one sample per "
                "pixel has an extractor yet for that interpretation",
            )
        channels, color_type = 1, 0
    elif storage.photometric == _RGB_PHOTOMETRIC:
        if storage.samples_per_pixel != 3:
            raise TiffRenderRefusal(
                RefusalReason.CORRUPT,
                f"this page declares RGB (PhotometricInterpretation 2) with "
                f"{storage.samples_per_pixel} samples per pixel rather than 3",
            )
        channels, color_type = 3, 2
    else:
        raise TiffRenderRefusal(
            RefusalReason.UNSUPPORTED_VARIANT,
            f"this page declares PhotometricInterpretation {storage.photometric}, "
            "which has no extractor yet for a multi-page TIFF page — a gap in this "
            "pipeline, not a decision about this page",
        )

    samples = bytearray()
    for strip_offset, strip_count in zip(storage.offsets, storage.counts, strict=True):
        samples += document.data[strip_offset : strip_offset + strip_count]
    if storage.photometric == 0:
        # WhiteIsZero: a raw sample of 0 renders as maximum intensity (white).
        # Inverting here is what makes the sealed pixels mean what the file
        # declares — an unrecorded transform is exactly what ARCHITECTURE's third
        # invariant cannot survive, so this is done once, deterministically, and
        # the result is what gets sealed.
        samples = bytearray(255 - value for value in samples)

    png_bytes = image_formats.encode_png(
        geometry.width, geometry.height, color_type, channels, bytes(samples)
    )
    return png_bytes, "png"
