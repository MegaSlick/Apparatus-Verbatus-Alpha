"""Serving-receipt construction and validation.

Receipts are intentionally only values here.  The run-tree writer owns where a
non-deterministic receipt is stored; this module makes sure the value contains every
fact required to reproduce the model identity that actually answered.
"""

from __future__ import annotations

from typing import Any

from .errors import ReceiptRefusal
from .models import ChairIdentity, ServingDetails, ServingReceipt, is_hf_revision, is_sha256

RECEIPT_SCHEMA = "seat-serving-receipt.v1"
_REQUIRED = {
    "schema",
    "seat",
    "source",
    "resolved",
    "revision",
    "revision_kind",
    "digest_manifest",
    "tokenizer_revision",
    "seed",
    "context_cap",
    "pixel_cap",
    "engine",
    "engine_version",
    "dtype",
    "adapter_identity",
    "endpoint",
    "started_at",
}


def build_receipt(identity: ChairIdentity, details: ServingDetails) -> ServingReceipt:
    """Build a receipt only when every provenance field is present and coherent."""

    _validate_identity(identity)
    _validate_details(identity.role, details)
    receipt = ServingReceipt(identity=identity, details=details)
    receipt_record(receipt)
    return receipt


def receipt_record(receipt: ServingReceipt) -> dict[str, Any]:
    """Return the validated record a run-receipt writer may store verbatim."""

    if not isinstance(receipt, ServingReceipt):
        raise ReceiptRefusal("receipt", "value is not a ServingReceipt")
    record = receipt.to_record()
    return validate_receipt(record)


def validate_receipt(record: Any) -> dict[str, Any]:
    """Refuse a malformed receipt read from a run receipt store."""

    if not isinstance(record, dict):
        raise ReceiptRefusal("receipt", "receipt is not an object")
    chair = record.get("seat") if isinstance(record.get("seat"), str) else "receipt"
    missing = sorted(_REQUIRED - set(record))
    extra = sorted(set(record) - _REQUIRED)
    if missing:
        raise ReceiptRefusal(chair, f"receipt is missing field(s) {missing}")
    if extra:
        raise ReceiptRefusal(chair, f"receipt has unknown field(s) {extra}")
    if record["schema"] != RECEIPT_SCHEMA:
        raise ReceiptRefusal(
            chair, f"receipt schema {record['schema']!r} is not {RECEIPT_SCHEMA!r}"
        )
    _nonblank_fields(
        record,
        chair,
        (
            "seat",
            "resolved",
            "tokenizer_revision",
            "engine",
            "engine_version",
            "dtype",
            "endpoint",
            "started_at",
        ),
    )
    source = record["source"]
    if source not in ("huggingface", "local-repository"):
        raise ReceiptRefusal(chair, "source must be 'huggingface' or 'local-repository'")
    if not is_sha256(record["digest_manifest"]):
        raise ReceiptRefusal(chair, "digest_manifest must be a lowercase sha256")
    kind = record["revision_kind"]
    if source == "huggingface":
        if kind != "git-commit" or not is_hf_revision(record["revision"]):
            raise ReceiptRefusal(
                chair, "huggingface receipt revision must be its exact 40-hex git commit"
            )
    elif kind != "digest-manifest" or record["revision"] != record["digest_manifest"]:
        raise ReceiptRefusal(
            chair,
            "local-repository receipt revision must be its verified digest-manifest hash",
        )
    for field in ("seed", "context_cap", "pixel_cap"):
        value = record[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReceiptRefusal(chair, f"{field} must be a non-negative integer")
    adapter = record["adapter_identity"]
    if adapter is not None:
        _validate_identity_record(adapter, chair)
    return record


def _validate_identity(identity: ChairIdentity) -> None:
    if not isinstance(identity.role, str) or not identity.role:
        raise ReceiptRefusal("receipt", "identity has no seat role")
    if identity.source == "huggingface":
        if not identity.repo or identity.path is not None or not is_hf_revision(identity.revision):
            raise ReceiptRefusal(
                identity.role, "huggingface identity is incomplete or not exactly pinned"
            )
    elif identity.source == "local-repository":
        if not identity.path or identity.repo is not None or identity.revision is not None:
            raise ReceiptRefusal(
                identity.role, "local-repository identity is incomplete or carries a git revision"
            )
    else:
        raise ReceiptRefusal(identity.role, f"identity has unknown source {identity.source!r}")
    if not is_sha256(identity.digest_manifest):
        raise ReceiptRefusal(identity.role, "identity has no valid digest-manifest hash")


def _validate_details(chair: str, details: ServingDetails) -> None:
    """Check the serving half directly, against the value's own attributes.

    Deliberately not by building a throwaway `SeatIdentity` and reading the
    assembled record back: a stand-in identity constructed to satisfy a
    validator is the one shape this package exists to keep out of its own code,
    and it also made the refusal name a seat whose fields were invented here.
    """
    if not isinstance(details, ServingDetails):
        raise ReceiptRefusal(chair, "serving details are not a ServingDetails value")
    for field in (
        "tokenizer_revision",
        "engine",
        "engine_version",
        "dtype",
        "endpoint",
        "started_at",
    ):
        value = getattr(details, field)
        if not isinstance(value, str) or not value.strip():
            raise ReceiptRefusal(chair, f"receipt field {field!r} must be a non-blank string")
    for field in ("seed", "context_cap", "pixel_cap"):
        value = getattr(details, field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReceiptRefusal(chair, f"{field} must be a non-negative integer")
    if details.adapter_identity is not None:
        _validate_identity(details.adapter_identity)


def _validate_identity_record(value: Any, chair: str) -> None:
    if not isinstance(value, dict):
        raise ReceiptRefusal(chair, "adapter_identity is not an identity object")
    required = {
        "role",
        "source",
        "repo",
        "path",
        "revision",
        "digest_manifest",
        "manifest",
        "adapter_of",
        "serving_recipe",
        "license_note",
    }
    if set(value) != required:
        raise ReceiptRefusal(chair, "adapter_identity does not carry a complete resolved identity")
    identity = ChairIdentity(**value)
    _validate_identity(identity)


def _nonblank_fields(record: dict[str, Any], chair: str, fields: tuple[str, ...]) -> None:
    for field in fields:
        if not isinstance(record[field], str) or not record[field].strip():
            raise ReceiptRefusal(chair, f"receipt field {field!r} must be a non-blank string")
