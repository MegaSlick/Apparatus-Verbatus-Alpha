"""One-receipt Chandra custody: the binding a retained response is written under.

A serving receipt carries no reference back to any response, so two
independently-supplied, individually-valid references are not proof they came
from the same Chandra call. `retain_chandra_response` writes a small
content-addressed custody record naming exactly the receipt and response it
was given; `read_retained_chandra_response` requires that record back and
refuses a response paired with any other receipt. The receipt is already
published by the time a response is retained, so this only ever binds a
response to a receipt that exists. Both ends validate the
receipt through the same `_validated_designator_receipt`, so they cannot drift
on what "the Chandra receipt" means, and the writer cannot seal a binding its
own reader would refuse.

The read half has no caller on the served path today -- kept because it is the
half that says what the binding *means*, exercised by
`pipeline/2_designator/test_geometry_layer.py`.

This lives in `common/` rather than a stage because a stage may not import
another stage's module (`pipeline/test_stage_import_boundaries.py`), and both
halves are one rule that duplication would let drift.
"""

from __future__ import annotations

import json
from typing import Any

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import DESIGNATOR, writing_directory
from common.runtree.store import BLOBS_DIR

CUSTODY_BINDING_SCHEMA = "chandra-custody-binding.v1"

# Derived from writing_directory, not a literal: put_blob writes under
# "2_designator/…", not the bare stage name "designator/…".
RESPONSE_BLOB_PREFIX = f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}/"
_RECEIPT_PREFIX = "receipts/sha256/"
# Named once so the writer, reader and `_is_custody_binding` cannot drift on
# what a binding's shape is.
_BINDING_FIELDS = frozenset(
    {"schema", "page_id", "page_ordinal", "receipt_sha256", "response_sha256"}
)


def _sha(value: Any, what: str) -> str:
    if not is_sha256(value):
        raise SchemaRefusal(f"{what} is not a lowercase sha256")
    return value


def custody_reference(value: object, prefix: str, what: str) -> dict[str, str]:
    """A closed digest reference that must name the given custody root."""
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"{what} is not its closed schema")
    if not isinstance(value["relative_path"], str) or not value["relative_path"].startswith(prefix):
        raise SchemaRefusal(f"{what} does not name {prefix}")
    return {"relative_path": value["relative_path"], "sha256": _sha(value["sha256"], what)}


def retain_chandra_response(
    tree: Any,
    response: bytes,
    receipt_ref: dict[str, str],
    *,
    page_id: str,
    page_ordinal: int,
) -> dict[str, Any]:
    """Store one raw response blob and record its binding to the one serving receipt.

    Returns ``{"response_ref": ..., "custody_ref": ...}``. ``response_ref`` is
    the plain two-key digest reference geometry proposals also carry;
    ``custody_ref`` is a separate content-addressed record naming both digests
    together, which `read_retained_chandra_response` re-verifies rather than
    trusting that a caller's two loose references belong together.
    """
    receipt = custody_reference(receipt_ref, _RECEIPT_PREFIX, "Chandra receipt reference")
    _page_identity(page_id, page_ordinal)
    if not isinstance(response, bytes):
        raise SchemaRefusal("Chandra raw response is not bytes")
    # Before any bytes are written: a binding sealed under a receipt the reader
    # will refuse is unreadable custody, not custody.
    _validated_designator_receipt(tree, receipt)
    # Bindings and responses share one blob namespace, so a response that *is*
    # a canonical binding could otherwise be read back as proof of a pairing
    # nothing recorded. Refusing it holds this module's own writer to that;
    # it does not bind a caller who writes blobs through the tree directly.
    if _is_custody_binding(response):
        raise SchemaRefusal("Chandra raw response is itself a custody binding record")
    digest, published = tree.put_blob(DESIGNATOR, response)
    response_ref = custody_reference(
        {"relative_path": published.relative_path, "sha256": digest},
        RESPONSE_BLOB_PREFIX,
        "Chandra response reference",
    )
    binding = canonical_bytes(
        {
            "schema": CUSTODY_BINDING_SCHEMA,
            "page_id": page_id,
            "page_ordinal": page_ordinal,
            "receipt_sha256": receipt["sha256"],
            "response_sha256": response_ref["sha256"],
        }
    )
    binding_digest, binding_published = tree.put_blob(DESIGNATOR, binding)
    custody_ref = custody_reference(
        {"relative_path": binding_published.relative_path, "sha256": binding_digest},
        RESPONSE_BLOB_PREFIX,
        "Chandra custody binding reference",
    )
    return {"response_ref": response_ref, "custody_ref": custody_ref}


def read_retained_chandra_response(
    tree: Any,
    response_ref: object,
    receipt_ref: object,
    custody_ref: object,
    *,
    page_id: str,
    page_ordinal: int,
) -> bytes:
    """Forged, mismatched, or tampered references are refused."""
    _page_identity(page_id, page_ordinal)
    response = custody_reference(response_ref, RESPONSE_BLOB_PREFIX, "Chandra response reference")
    receipt = custody_reference(receipt_ref, _RECEIPT_PREFIX, "Chandra receipt reference")
    custody = custody_reference(
        custody_ref, RESPONSE_BLOB_PREFIX, "Chandra custody binding reference"
    )
    binding_bytes = _read_custody_bytes(tree, custody["relative_path"], "custody binding")
    if digest_bytes(binding_bytes) != custody["sha256"]:
        raise SchemaRefusal("Chandra custody binding blob differs from its sealed reference")
    try:
        recorded = json.loads(binding_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        # `RecursionError` because nesting, not length, is what breaks the JSON
        # parser; uncaught it would escape this boundary's named refusal.
        raise SchemaRefusal(f"Chandra custody binding is not valid JSON: {error}") from error
    if not isinstance(recorded, dict) or set(recorded) != _BINDING_FIELDS:
        raise SchemaRefusal("Chandra custody binding is not its closed schema")
    try:
        exact_canonical_bytes = canonical_bytes(recorded)
    except (TypeError, ValueError) as error:
        raise SchemaRefusal(f"Chandra custody binding is not canonical JSON: {error}") from error
    if binding_bytes != exact_canonical_bytes:
        raise SchemaRefusal("Chandra custody binding is not exact canonical JSON bytes")
    if recorded["page_id"] != page_id or recorded["page_ordinal"] != page_ordinal:
        raise SchemaRefusal("Chandra custody binding belongs to a different page")
    if recorded["schema"] != CUSTODY_BINDING_SCHEMA:
        raise SchemaRefusal("Chandra custody binding does not carry its own schema label")
    if recorded["receipt_sha256"] != receipt["sha256"]:
        raise SchemaRefusal(
            "Chandra response was retained under a different receipt than the one given here"
        )
    if recorded["response_sha256"] != response["sha256"]:
        raise SchemaRefusal(
            "Chandra custody binding names a different response than the one given here"
        )
    # This proves the receipt is authentic; the binding checked above proves
    # the pairing.
    _validated_designator_receipt(tree, receipt)
    data = _read_custody_bytes(tree, response["relative_path"], "response blob")
    if digest_bytes(data) != response["sha256"]:
        raise SchemaRefusal("Chandra response blob differs from its sealed reference")
    return data


def _read_custody_bytes(tree: Any, relative_path: str, what: str) -> bytes:
    """Read one sealed blob, refusing a reference whose file is no longer there.

    A well-formed reference to a removed blob is a custody failure, not a
    crash: refusing provenance includes provenance that is no longer there.
    """
    try:
        return tree.read_bytes(relative_path)
    except OSError as error:
        raise SchemaRefusal(f"Chandra {what} {relative_path} could not be read: {error}") from error


def _is_custody_binding(data: bytes) -> bool:
    """Are these bytes exactly what this module's binding writer would produce?"""
    try:
        recorded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    if not isinstance(recorded, dict) or set(recorded) != _BINDING_FIELDS:
        return False
    if recorded["schema"] != CUSTODY_BINDING_SCHEMA:
        return False
    try:
        return data == canonical_bytes(recorded)
    except (TypeError, ValueError):
        return False


def _validated_designator_receipt(tree: Any, receipt: dict[str, str]) -> dict[str, Any]:
    """Read the receipt through the run tree and require the Designator's chair.

    This custody covers only the Chandra call the Designator serves in
    `designator_structure`; a receipt naming any other chair is a different
    call however honestly it verifies. `common.stage` is imported inside the
    function so a small shared custody rule does not drag in the whole
    stage-program harness at import time.
    """
    from common.stage import DESIGNATOR_CHAIR

    record = tree.read_run_receipt(receipt)
    if record["chair"] != DESIGNATOR_CHAIR:
        raise SchemaRefusal(
            f"Chandra custody receipt was not issued for chair {DESIGNATOR_CHAIR!r}"
        )
    return record


def _page_identity(page_id: object, page_ordinal: object) -> None:
    if not isinstance(page_id, str) or not page_id:
        raise SchemaRefusal("Chandra custody page identity is blank")
    if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool) or page_ordinal < 0:
        raise SchemaRefusal("Chandra custody page ordinal is not a non-negative integer")
