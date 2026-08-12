"""Small deterministic encodings used only inside this instrument.

The generic pipeline artifact encoding is intentionally not imported here: this
instrument has its own private request shape and must not pretend it is a pipeline
artifact.  The encoding below is only a byte-stable envelope for a candidate dossier.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import MeasurementRefusal


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a candidate dossier in a deterministic, permissive JSON envelope.

    This follows ``json.dumps`` for supported values, including finite floats and
    integer keys converted to strings. Approval scopes use the strict repository
    canonicalizer in ``gates._approval_digest``; this helper must not be used to
    claim that arbitrary caller content was bound without conversion.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        # `TypeError` for an unsupported value or a mixed key type, `ValueError`
        # for a non-finite float under `allow_nan=False`. Raised by name because
        # this helper sits under every digest and wire-generation path, and a
        # bare exception there names no record and is caught by no caller.
        raise MeasurementRefusal(
            f"a record could not be canonically encoded for digesting: {error}"
        ) from error


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase digest spelling recorded by this framework."""

    return hashlib.sha256(value).hexdigest()


def is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
