"""Small deterministic encodings used only inside this instrument.

The generic pipeline artifact encoding is intentionally not imported here: this
instrument has its own private request shape and must not pretend it is a pipeline
artifact.  The encoding below is only a byte-stable envelope for a candidate dossier.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a candidate dossier in a deterministic, permissive JSON envelope.

    This follows ``json.dumps`` for supported values, including finite floats and
    integer keys converted to strings. Approval scopes use the strict repository
    canonicalizer in ``gates._approval_digest``; this helper must not be used to
    claim that arbitrary caller content was bound without conversion.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase digest spelling recorded by this framework."""

    return hashlib.sha256(value).hexdigest()


def is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
