"""The one byte-led admission route for the Exemplar door.

The door does not keep a list of image formats to reject.  The route says only
whether a decoder reads a source as one raster or fans a container into pages.  A
real file that the installed decoders cannot read is a named pipeline alarm, never
a policy decision about the submitter's format.

**There are two actions, and the difference between them is whether the format is
*always* a container.**  PDF always is: every PDF is a document of pages, and a
one-page PDF is still a page that has to be painted before there are any pixels at
all.  Every raster format is *usually* one image and may hold more — multi-page
TIFF is the clearest case, but APNG, animated GIF, animated WebP and multi-picture
JPEG are the same shape.  So a raster source is admitted **or** fanned out, and the
decoder's own frame count decides which: one frame is sealed as its own original
bytes, unmodified, and more than one is fanned out to per-page ordinals and rendered.

So there is no bare `admit`: every raster is frame-counted before it is sealed,
because sealing a source whole without asking how many frames it holds would keep
page one of a multi-page scan and lose the rest.  `inspect_source` refuses a
multi-frame raster that reaches it without a page index for the same reason.
`render-pages` is PDF alone, by construction of `FORMAT_ROUTES`: routing a raster
format through it would re-encode every ordinary single-page file for nothing, and
a single-page TIFF that seals cleanly as its own bytes must keep them — the
Exemplar is the immutable source.
"""

from __future__ import annotations

from typing import Final, NamedTuple

import image_formats
from image_formats import (
    MAX_RENDERED_PAGE_BYTES,
    MAX_SOURCE_BYTES,
    FormatRefusal,
    FormatVerdict,
    decode_raster,
    sniff,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.stages import RefusalReason

# A raster format: decoded, and then admitted as its own unmodified bytes when the
# decoder reports one frame, or fanned out to one ordinal per frame when it reports
# more.
ADMIT_OR_FAN_OUT: Final = "admit-or-fan-out"
# A format that is always a container of pages: PDF alone.
RENDER_PAGES: Final = "render-pages"
ALWAYS_A_CONTAINER: Final = frozenset({"pdf"})
SNIFFABLE_FORMATS: Final = image_formats.SNIFFABLE_FORMATS


class AdmissionOutcome(NamedTuple):
    """One source's decision, including the bytes-derived format and geometry."""

    outcome: str
    reason: str | None
    detected_format: str | None
    digest: str | None
    geometry: tuple[int, int] | None


def reason(code: RefusalReason, detail: str) -> str:
    """The one spelling of a closed-set alarm reason."""
    return f"{code.value}: {detail}"


def too_large_detail(byte_count: int) -> str:
    """The one spelling of a TOO_LARGE detail, by whichever caller counted it."""
    return f"{byte_count} bytes exceeds the {MAX_SOURCE_BYTES}-byte admission limit"


def reason_code(text: object) -> RefusalReason:
    """Read and validate an alarm code from a published reason."""
    if not isinstance(text, str) or ":" not in text:
        raise ContractError(f"refusal reason {text!r} does not open with a closed-set code")
    try:
        return RefusalReason(text.split(":", 1)[0])
    except ValueError:
        raise ContractError(
            f"refusal reason {text!r} names a code outside "
            f"{[member.value for member in RefusalReason]}"
        ) from None


def route_for(detected: str | None) -> str:
    """The decoder route for a sniffed format; an unknown signature gets a raster attempt.

    Pillow supports more formats than the small signature sniffer can responsibly
    name. Giving those bytes a raster attempt lets a valid installed decoder
    establish what they are; failing that attempt becomes an explicit
    `unrecognized-format` alarm rather than a silent omission.
    """
    return RENDER_PAGES if detected in ALWAYS_A_CONTAINER else ADMIT_OR_FAN_OUT


# The route of every named format, sealed into a real run's `config_digest`.
FORMAT_ROUTES: Final = {
    format_name: route_for(format_name) for format_name in sorted(SNIFFABLE_FORMATS)
}


def inspect_source(data: bytes, *, declared_sha256: str | None) -> AdmissionOutcome:
    """Decode one single-raster submitted source and compare its declared digest.

    The order below is the contract, and the container check is not first.  Empty
    input, a digest mismatch and an oversized source are refused ahead of any
    look at byte structure, so bytes failing one of those are refused as an
    outcome whatever they would have sniffed as -- a page container included.
    The caller-error raise applies only to what survives those three: a container
    that gets that far was never fanned out by the door, and raising says so
    rather than filing a refusal the door would record as an artifact.  A source
    that is neither refused earlier nor a container is decoded into pixels before
    admission, and must hold exactly one frame; extension spelling is never read.
    """
    return _inspect(
        data,
        declared_sha256=declared_sha256,
        byte_limit=MAX_SOURCE_BYTES,
        too_large=too_large_detail(len(data)),
    )


def inspect_rendered_page(data: bytes) -> AdmissionOutcome:
    """Decode one page the Door rendered, under the rendered-page byte bound.

    The same checks as `inspect_source`, except that a rendered page is bounded
    by `MAX_RENDERED_PAGE_BYTES`, the size every later stage can read back, not by
    the limit on submitted files.
    """
    return _inspect(
        data,
        declared_sha256=None,
        byte_limit=MAX_RENDERED_PAGE_BYTES,
        too_large=(
            f"the rendered page is {len(data)} bytes, above the "
            f"{MAX_RENDERED_PAGE_BYTES}-byte rendered-page limit"
        ),
    )


def _inspect(
    data: bytes,
    *,
    declared_sha256: str | None,
    byte_limit: int,
    too_large: str,
) -> AdmissionOutcome:
    if not data:
        return AdmissionOutcome(
            "refused", reason(RefusalReason.EMPTY, "the source is empty"), None, None, None
        )
    digest = digest_bytes(data)
    # This comparison is deliberately before byte-structure inspection.  The
    # ledger's whole purpose at this boundary is to tell a changed copy from a
    # source that this decoder cannot read.  If a transfer has changed the bytes,
    # a later PNG/JPEG decoder error must not conceal that more useful fact.
    if declared_sha256 is not None and digest != declared_sha256:
        return AdmissionOutcome(
            "refused",
            reason(
                RefusalReason.DIGEST_MISMATCH,
                f"computed {digest}, but {declared_sha256} was declared",
            ),
            sniff(data),
            digest,
            None,
        )
    if len(data) > byte_limit:
        return AdmissionOutcome(
            "refused", reason(RefusalReason.TOO_LARGE, too_large), sniff(data), digest, None
        )
    detected = sniff(data)
    if route_for(detected) == RENDER_PAGES:
        raise ValueError(
            f"{detected} is a page container; the door fans it out rather than "
            "admitting it as one image"
        )
    try:
        decoded = decode_raster(data)
    except FormatRefusal as error:
        return AdmissionOutcome(
            "refused", reason(_refusal_code(error), str(error)), detected, digest, None
        )
    if decoded.frame_count != 1:
        # Sealing these bytes whole would admit frame one as the entire source.
        return AdmissionOutcome(
            "refused",
            reason(
                RefusalReason.UNSUPPORTED_VARIANT,
                f"{decoded.format} holds {decoded.frame_count} frames but reached admission "
                "without a page index, so the door did not fan it out",
            ),
            decoded.format,
            digest,
            None,
        )
    return AdmissionOutcome(
        "admitted", None, decoded.format, digest, (decoded.width, decoded.height)
    )


def _refusal_code(error: FormatRefusal) -> RefusalReason:
    if error.verdict is FormatVerdict.UNRECOGNIZED:
        return RefusalReason.UNRECOGNIZED_FORMAT
    if error.verdict is FormatVerdict.UNSUPPORTED:
        return RefusalReason.UNSUPPORTED_VARIANT
    return RefusalReason.CORRUPT
