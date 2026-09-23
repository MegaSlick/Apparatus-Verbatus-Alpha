"""Bind declared runs to the exact committed pre-measurement protocol artifact."""

from __future__ import annotations

from pathlib import Path

from .encoding import is_sha256, sha256_bytes
from .errors import MatrixRefusal

# Updated only in the same reviewable change as the protocol document itself,
# so this digest can never drift silently from what the document says. The
# protocol's measure definitions (bounds, profiles, scoring rules) stay pinned
# and fail closed across a re-pin; only prose, provenance and closed
# limitations move.
PREDECLARED_PROTOCOL_SHA256 = "c1c8f77fa03014bc95220e253609b7279bbd05d6359a3165f2dedb528a3e944a"


def protocol_document_sha256() -> str:
    """Return the digest of the committed protocol document, not a caller assertion."""

    path = Path(__file__).with_name("README.md")
    try:
        payload = path.read_bytes()
    except OSError as error:
        # An absent or unreadable protocol document is the same refusal as a
        # changed one: the run cannot show which protocol it ran under. Raised
        # by name because every caller holds on `MatrixRefusal`, and a bare
        # `OSError` out of a gate is not caught by any of them.
        raise MatrixRefusal(
            f"the committed Spec 05 protocol document cannot be read at {path.name}: {error}"
        ) from error
    return sha256_bytes(payload)


def require_predeclared_protocol() -> str:
    """Refuse a declared run if its committed protocol is no longer the pinned artifact."""

    # The pin itself first. A malformed or miscased constant can never equal a
    # measured digest, so the run would refuse with "the protocol differs" and
    # send the reader to diff a document that had not changed.
    if not is_sha256(PREDECLARED_PROTOCOL_SHA256):
        raise MatrixRefusal(
            "the predeclared Spec 05 protocol pin is not a lowercase SHA-256: "
            f"{PREDECLARED_PROTOCOL_SHA256!r}"
        )
    observed = protocol_document_sha256()
    if observed != PREDECLARED_PROTOCOL_SHA256:
        raise MatrixRefusal(
            "the committed Spec 05 protocol document differs from its predeclared digest: "
            f"observed {observed}, predeclared {PREDECLARED_PROTOCOL_SHA256}"
        )
    # The measured digest, not the constant it matched. They are equal here by
    # the check above; returning the measurement keeps the value a reading.
    return observed
