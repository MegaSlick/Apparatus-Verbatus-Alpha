"""Bind declared runs to the exact committed pre-measurement protocol artifact."""

from __future__ import annotations

from pathlib import Path

from .encoding import sha256_bytes
from .errors import MatrixRefusal

# Updated only in the same reviewable change as the protocol document itself.
PREDECLARED_PROTOCOL_SHA256 = "9c939d27edde2e1429f6cc57b261b3f74be952c7f25255490c763c9a0acf4648"


def protocol_document_sha256() -> str:
    """Return the digest of the committed protocol document, not a caller assertion."""

    return sha256_bytes(Path(__file__).with_name("README.md").read_bytes())


def require_predeclared_protocol() -> str:
    """Refuse a declared run if its committed protocol is no longer the pinned artifact."""

    observed = protocol_document_sha256()
    if observed != PREDECLARED_PROTOCOL_SHA256:
        raise MatrixRefusal(
            "the committed Spec 05 protocol document differs from its predeclared digest"
        )
    return PREDECLARED_PROTOCOL_SHA256
