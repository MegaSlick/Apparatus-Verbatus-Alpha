"""One-receipt Chandra custody, shared by its two stage-side halves.

R2's Designator geometry adapter writes the raw Chandra response once
(text-free geometry beside a retained blob), and R3's Attestatores capture
intake reads that blob back under the same serving receipt. The write and the
read are one custody rule with two stage-side callers, so the rule lives in
`common/` rather than in either stage — a stage may not import another stage's
uniquely named module (`pipeline/test_stage_import_boundaries.py`), and
duplicating the check would let the two halves drift (the R0 freeze-note
derived-record pattern).

A serving receipt carries no reference back to any response (`common/chairs/
receipts.py`'s schema has no such field), so two independently-supplied,
individually-valid references are not proof they came from the same Chandra
call. `retain_chandra_response` therefore also writes a small content-addressed
custody record naming exactly the receipt and response it was given, and
`read_retained_chandra_response` requires that same record back and refuses a
response paired with any receipt other than the one recorded here.
"""

from __future__ import annotations

import json
from typing import Any

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import DESIGNATOR, writing_directory
from common.runtree.store import BLOBS_DIR

CUSTODY_BINDING_SCHEMA = "chandra-custody-binding.v1"

# Derived from the run tree's own directory naming (`writing_directory`) rather
# than written out as a literal: a literal here previously read "designator/…"
# while `tree.put_blob(DESIGNATOR, …)` actually writes under "2_designator/…"
# (`common/contracts/stages.STAGE_DIRECTORIES`), so every real response and
# custody-binding blob failed this module's own prefix check on the first real
# write. The mismatch was invisible in both existing test suites because their
# fixture trees fabricated paths from the bare stage name instead of exercising
# the real numbered layout — the derived-record pattern the R0 freeze note
# names, reproduced in the test doubles meant to catch it.
_RESPONSE_PREFIX = f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}/"
_RECEIPT_PREFIX = "receipts/sha256/"


def _sha(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
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
    digest, published = tree.put_blob(DESIGNATOR, response)
    response_ref = custody_reference(
        {"relative_path": published.relative_path, "sha256": digest},
        _RESPONSE_PREFIX,
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
        _RESPONSE_PREFIX,
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
    """R3's intake boundary: forged, mismatched, or tampered references are refused."""
    _page_identity(page_id, page_ordinal)
    response = custody_reference(response_ref, _RESPONSE_PREFIX, "Chandra response reference")
    receipt = custody_reference(receipt_ref, _RECEIPT_PREFIX, "Chandra receipt reference")
    custody = custody_reference(custody_ref, _RESPONSE_PREFIX, "Chandra custody binding reference")
    binding_bytes = tree.read_bytes(custody["relative_path"])
    if digest_bytes(binding_bytes) != custody["sha256"]:
        raise SchemaRefusal("Chandra custody binding blob differs from its sealed reference")
    try:
        recorded = json.loads(binding_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SchemaRefusal(f"Chandra custody binding is not valid JSON: {error}") from error
    binding_fields = {
        "schema",
        "page_id",
        "page_ordinal",
        "receipt_sha256",
        "response_sha256",
    }
    if not isinstance(recorded, dict) or set(recorded) != binding_fields:
        raise SchemaRefusal("Chandra custody binding is not its closed schema")
    try:
        exact_canonical_bytes = canonical_bytes(recorded)
    except (TypeError, ValueError) as error:
        raise SchemaRefusal(f"Chandra custody binding is not canonical JSON: {error}") from error
    if binding_bytes != exact_canonical_bytes:
        raise SchemaRefusal("Chandra custody binding is not exact canonical JSON bytes")
    if recorded["page_id"] != page_id or recorded["page_ordinal"] != page_ordinal:
        raise SchemaRefusal("Chandra custody binding belongs to a different page")
    if (
        recorded["schema"] != CUSTODY_BINDING_SCHEMA
        or recorded["receipt_sha256"] != receipt["sha256"]
        or recorded["response_sha256"] != response["sha256"]
    ):
        raise SchemaRefusal(
            "Chandra response was retained under a different receipt than the one given here"
        )
    # Receipt validation is delegated to the run tree, which verifies its schema,
    # path, and bytes -- that proves the receipt itself is authentic, not that it
    # is paired with this response. The custody binding checked above is what
    # proves the pairing; the response bytes are opaque textual custody, never
    # parsed by this module.
    receipt_record = tree.read_run_receipt(receipt)
    from common.stage import DESIGNATOR_CHAIR

    if receipt_record["chair"] != DESIGNATOR_CHAIR:
        raise SchemaRefusal(
            f"Chandra custody receipt was not issued for chair {DESIGNATOR_CHAIR!r}"
        )
    data = tree.read_bytes(response["relative_path"])
    if digest_bytes(data) != response["sha256"]:
        raise SchemaRefusal("Chandra response blob differs from its sealed reference")
    return data


def _page_identity(page_id: object, page_ordinal: object) -> None:
    if not isinstance(page_id, str) or not page_id:
        raise SchemaRefusal("Chandra custody page identity is blank")
    if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool) or page_ordinal < 0:
        raise SchemaRefusal("Chandra custody page ordinal is not a non-negative integer")
