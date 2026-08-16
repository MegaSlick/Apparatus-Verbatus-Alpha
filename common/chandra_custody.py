"""One-receipt Chandra custody, shared by its two stage-side halves.

R2's Designator geometry adapter writes the raw Chandra response once
(text-free geometry beside a retained blob), and R3's Attestatores capture
intake reads that blob back under the same serving receipt. The write and the
read are one custody rule with two stage-side callers, so the rule lives in
`common/` rather than in either stage — a stage may not import another stage's
uniquely named module (`pipeline/test_stage_import_boundaries.py`), and
duplicating the check would let the two halves drift (the R0 freeze-note
derived-record pattern).
"""

from __future__ import annotations

from typing import Any

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import DESIGNATOR


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
    tree: Any, response: bytes, receipt_ref: dict[str, str]
) -> dict[str, str]:
    """Store one raw response blob and bind it jointly to the one serving receipt."""
    custody_reference(receipt_ref, "receipts/sha256/", "Chandra receipt reference")
    if not isinstance(response, bytes):
        raise SchemaRefusal("Chandra raw response is not bytes")
    digest, published = tree.put_blob(DESIGNATOR, response)
    reference = {"relative_path": published.relative_path, "sha256": digest}
    return custody_reference(reference, "designator/blobs/sha256/", "Chandra response reference")


def read_retained_chandra_response(tree: Any, response_ref: object, receipt_ref: object) -> bytes:
    """R3's intake boundary: forged blob or receipt references are refused."""
    response = custody_reference(
        response_ref, "designator/blobs/sha256/", "Chandra response reference"
    )
    receipt = custody_reference(receipt_ref, "receipts/sha256/", "Chandra receipt reference")
    # Receipt validation is delegated to the run tree, which verifies its schema,
    # path, and bytes.  The response itself is opaque textual custody, never
    # parsed by this module.
    tree.read_run_receipt(receipt)
    data = tree.read_bytes(response["relative_path"])
    if digest_bytes(data) != response["sha256"]:
        raise SchemaRefusal("Chandra response blob differs from its sealed reference")
    return data
