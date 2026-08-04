"""The one byte-led admission route for the Exemplar door.

The door does not keep a list of image formats to reject.  Its configuration says
whether a decoder reads a source as one raster or fans a container into pages.  A
real file that the installed decoders cannot read is a named pipeline alarm, never
a policy decision about the submitter's format.

**There are two actions, and the difference between them is whether the format is
*always* a container.**  PDF always is: every PDF is a document of pages, and a
one-page PDF is still a page that has to be painted before there are any pixels at
all.  Every raster format is *usually* one image and may hold more — multi-page
TIFF is the case Tyrel named, but APNG, animated GIF, animated WebP and multi-picture
JPEG are the same shape.  So a raster source is admitted **or** fanned out, and the
decoder's own frame count decides which: one frame is sealed as its own original
bytes, unmodified, and more than one is fanned out to per-page ordinals and rendered.

That is why there is no bare `admit`.  An action that seals a source without ever
asking how many frames it holds is the door defect that lost every page after the
first of a multi-page TIFF; removing the action removes the failure rather than
testing for it.  `render-pages` is restricted at load time to PDF alone for the
mirror-image reason: routing a raster format through it would re-encode every
ordinary single-page file for nothing, and a single-page TIFF that seals cleanly as
its own bytes must keep them (GOVERNANCE 4 — the Exemplar is the immutable source).
"""

from __future__ import annotations

from enum import Enum
from typing import Final, NamedTuple

import image_formats
from image_formats import (
    MAX_SOURCE_BYTES,
    FormatRefusal,
    FormatVerdict,
    decode_raster,
    sniff,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

# A raster format: decoded, and then admitted as its own unmodified bytes when the
# decoder reports one frame, or fanned out to one ordinal per frame when it reports
# more.  Named for both halves because both really happen; a name that said only
# "raster" hid the fan-out that keeps page two of a scan.
ADMIT_OR_FAN_OUT: Final = "admit-or-fan-out"
# A format that is always a container of pages.  PDF alone, enforced at load.
RENDER_PAGES: Final = "render-pages"
ALWAYS_A_CONTAINER: Final = frozenset({"pdf"})
ACTIONS: Final = frozenset({ADMIT_OR_FAN_OUT, RENDER_PAGES})
SNIFFABLE_FORMATS: Final = image_formats.SNIFFABLE_FORMATS


class RefusalReason(str, Enum):
    """Closed alarm vocabulary for damage and decoder failures.

    A format-policy refusal deliberately does not exist.  `UNSUPPORTED_VARIANT`
    names a real decoder gap so it is visible work for the pipeline, rather than a
    routine reason to abandon a submitted page.
    """

    EMPTY = "empty"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too-large"
    UNRECOGNIZED_FORMAT = "unrecognized-format"
    CORRUPT = "corrupt"
    UNSUPPORTED_VARIANT = "unsupported-variant"
    DIGEST_MISMATCH = "digest-mismatch"
    DUPLICATE = "duplicate"


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


FORMAT_ROUTES: Final = {
    format_name: RENDER_PAGES if format_name in ALWAYS_A_CONTAINER else ADMIT_OR_FAN_OUT
    for format_name in sorted(SNIFFABLE_FORMATS)
}


def load_format_policy() -> dict[str, str]:
    """Return the complete code-owned routing map.

    Ruling 2 leaves no operator choice here: every raster is decoded and fanned out
    when needed, while PDF is always painted page by page. Deriving the map from the
    sniffer means a new named format cannot be omitted, and keeping it in code avoids
    presenting the one legal routing as a configurable decision.
    """
    return dict(FORMAT_ROUTES)


def classify_detected_format(detected: str | None, policy: dict[str, str]) -> str:
    """Choose a decoder route, with a generic raster attempt for unknown magic.

    Pillow supports more formats than the small signature sniffer can responsibly
    name.  Giving those bytes a generic raster attempt lets a valid installed
    decoder establish what they are; failing that attempt becomes an explicit
    `unrecognized-format` alarm rather than a silent omission.
    """
    if detected is None:
        return ADMIT_OR_FAN_OUT
    try:
        return policy[detected]
    except KeyError:
        # `load_format_policy` prevents this for a shipped policy.  A hand-built
        # caller policy still never gains a policy refusal path.
        return ADMIT_OR_FAN_OUT


def inspect_source(
    data: bytes, *, declared_sha256: str | None, policy: dict[str, str]
) -> AdmissionOutcome:
    """Decode one single-raster source and compare its declared digest.

    Page containers are deliberately returned to the door for fan-out.  Everything
    else is decoded into pixels before admission; extension spelling is never read.
    """
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
    if len(data) > MAX_SOURCE_BYTES:
        return AdmissionOutcome(
            "refused",
            reason(
                RefusalReason.TOO_LARGE,
                f"{len(data)} bytes exceeds the {MAX_SOURCE_BYTES}-byte admission limit",
            ),
            sniff(data),
            digest,
            None,
        )
    detected = sniff(data)
    if classify_detected_format(detected, policy) == RENDER_PAGES:
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
    return AdmissionOutcome(
        "admitted", None, decoded.format, digest, (decoded.width, decoded.height)
    )


def _refusal_code(error: FormatRefusal) -> RefusalReason:
    if error.verdict is FormatVerdict.UNRECOGNIZED:
        return RefusalReason.UNRECOGNIZED_FORMAT
    if error.verdict is FormatVerdict.UNSUPPORTED:
        return RefusalReason.UNSUPPORTED_VARIANT
    return RefusalReason.CORRUPT


def duplicate_reason(first_ordinal: int) -> str:
    """The alarm for a second submitted file whose bytes match an admission."""
    return reason(
        RefusalReason.DUPLICATE,
        f"identical content already admitted as source-{first_ordinal}",
    )
