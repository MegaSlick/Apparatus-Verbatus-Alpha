"""The one byte-led admission route for the Exemplar door.

The door does not keep a list of image formats to reject.  Its configuration says
whether a decoder reads a source as one raster or fans a container into pages.  A
real file that the installed decoders cannot read is a named pipeline alarm, never
a policy decision about the submitter's format.
"""

from __future__ import annotations

import tomllib
from enum import Enum
from pathlib import Path
from typing import Final, NamedTuple

import image_formats
from image_formats import MAX_SOURCE_BYTES, FormatRefusal, decode_raster, sniff

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

DEFAULT_FORMAT_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "admitted_formats.toml"
)

RASTER: Final = "raster"
RENDER_PAGES: Final = "render-pages"
ACTIONS: Final = frozenset({RASTER, RENDER_PAGES})
SNIFFABLE_FORMATS: Final = image_formats.SNIFFABLE_FORMATS


class FormatPolicyRefusal(ContractError):
    """The decoder-routing configuration cannot be read or covers the wrong set."""


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


def load_format_policy(path: Path = DEFAULT_FORMAT_POLICY_PATH) -> dict[str, str]:
    """Load every named decoder route fresh for each door run."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise FormatPolicyRefusal(
            f"the decoder routing at {path} could not be read: {error}"
        ) from error
    table = document.get("format")
    if set(document) != {"format"} or not isinstance(table, dict):
        raise FormatPolicyRefusal(f"{path} is not a single [format] table of format-name rows")
    named = set(table)
    if named != SNIFFABLE_FORMATS:
        missing = sorted(SNIFFABLE_FORMATS - named)
        unknown = sorted(named - SNIFFABLE_FORMATS)
        raise FormatPolicyRefusal(
            f"{path} must name exactly the formats the door can sniff. "
            f"Missing: {missing}. Unknown: {unknown}."
        )
    for format_name, action in sorted(table.items()):
        if action not in ACTIONS:
            raise FormatPolicyRefusal(
                f"{path} gives format {format_name!r} the action {action!r}, "
                f"which is not one of {sorted(ACTIONS)}"
            )
    return dict(sorted(table.items()))


def classify_detected_format(detected: str | None, policy: dict[str, str]) -> str:
    """Choose a decoder route, with a generic raster attempt for unknown magic.

    Pillow supports more formats than the small signature sniffer can responsibly
    name.  Giving those bytes a generic raster attempt lets a valid installed
    decoder establish what they are; failing that attempt becomes an explicit
    `unrecognized-format` alarm rather than a silent omission.
    """
    if detected is None:
        return RASTER
    try:
        return policy[detected]
    except KeyError:
        # `load_format_policy` prevents this for a shipped policy.  A hand-built
        # caller policy still never gains a policy refusal path.
        return RASTER


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
    text = str(error)
    if text.startswith("unrecognized"):
        return RefusalReason.UNRECOGNIZED_FORMAT
    if text.startswith("unsupported"):
        return RefusalReason.UNSUPPORTED_VARIANT
    return RefusalReason.CORRUPT


def duplicate_reason(first_ordinal: int) -> str:
    """The alarm for a second submitted file whose bytes match an admission."""
    return reason(
        RefusalReason.DUPLICATE,
        f"identical content already admitted as source-{first_ordinal}",
    )
