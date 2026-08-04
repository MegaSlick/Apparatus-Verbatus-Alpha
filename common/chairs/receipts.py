"""Serving-receipt construction and validation.

Receipts are intentionally only values here.  The run-tree writer owns where a
non-deterministic receipt is stored; this module makes sure the value contains every
fact required to reproduce the model identity that actually answered.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .errors import ReceiptRefusal
from .models import ChairIdentity, ServingDetails, ServingReceipt, is_hf_revision, is_sha256

RECEIPT_SCHEMA = "chair-serving-receipt.v1"
_REQUIRED = {
    "schema",
    "chair",
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
    _validate_details(identity, details)
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
    chair = record.get("chair") if isinstance(record.get("chair"), str) else "receipt"
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
            "chair",
            "resolved",
            "engine",
            "engine_version",
            "dtype",
            "endpoint",
        ),
    )
    # The only place these two are checked. `build_receipt` reaches here through
    # `receipt_record`, so checking them in `_validate_details` as well was the same
    # refusal raised twice from one call. This is also the door a receipt written by
    # an older revision of this code comes back through — content addressing catches
    # a *tampered* receipt long before this, but not one that was valid when written.
    _validate_tokenizer_revision(record["tokenizer_revision"], chair)
    _validate_started_at(record["started_at"], chair)
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
        raise ReceiptRefusal("receipt", "identity has no chair role")
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


def _validate_details(identity: ChairIdentity, details: ServingDetails) -> None:
    """Check the serving half directly, against the value's own attributes.

    Deliberately not by building a throwaway `ChairIdentity` and reading the
    assembled record back: a stand-in identity constructed to satisfy a
    validator is the one shape this package exists to keep out of its own code,
    and it also made the refusal name a chair whose fields were invented here.
    """
    chair = identity.role
    if not isinstance(details, ServingDetails):
        raise ReceiptRefusal(chair, "serving details are not a ServingDetails value")
    for field in (
        "engine",
        "engine_version",
        "dtype",
        "endpoint",
    ):
        value = getattr(details, field)
        if not isinstance(value, str) or not value.strip():
            raise ReceiptRefusal(chair, f"receipt field {field!r} must be a non-blank string")
    # `tokenizer_revision` and `started_at` are deliberately absent here: every path
    # into this function continues into `validate_receipt`, which owns them.
    for field in ("seed", "context_cap", "pixel_cap"):
        value = getattr(details, field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReceiptRefusal(chair, f"{field} must be a non-negative integer")
    # **The adapter half is bound to the configuration, not merely well formed.**
    # An adapter chair is an adapter *of* a named base, and the base artifact
    # genuinely participates in the reading, so a receipt that omitted it or named
    # some other chair lost the identity of a model that answered — GOVERNANCE 6
    # applies to every model in the serving moment, not only the one in the role.
    if details.adapter_identity is not None:
        _validate_identity(details.adapter_identity)
    if identity.adapter_of is None:
        if details.adapter_identity is not None:
            raise ReceiptRefusal(
                chair,
                f"receipt names adapter base {details.adapter_identity.role!r} for a chair "
                "configured with no adapter_of",
            )
    elif details.adapter_identity is None:
        raise ReceiptRefusal(
            chair,
            f"chair is an adapter of {identity.adapter_of!r}, and its receipt carries no "
            "adapter base identity; the base artifact that answered would be lost",
        )
    elif details.adapter_identity.role != identity.adapter_of:
        raise ReceiptRefusal(
            chair,
            f"receipt names adapter base {details.adapter_identity.role!r}, but this chair "
            f"is configured as an adapter of {identity.adapter_of!r}",
        )


def _validate_tokenizer_revision(value: object, chair: str) -> None:
    """A tokenizer revision is a pin, on the same terms as the model revision.

    `config.py` refuses a branch name for `revision` because "a branch name is not
    a pin"; a receipt naming `main` as the tokenizer state makes the same claim
    unreproducible one field to the right, and nothing downstream would notice.
    Both revision shapes this package issues are accepted: a 40-hex git commit and
    a 64-hex digest-manifest hash.
    """
    if not (is_hf_revision(value) or is_sha256(value)):
        raise ReceiptRefusal(
            chair,
            f"tokenizer_revision {value!r} is not a pin; it must be a 40-hex git commit "
            "or a 64-hex digest-manifest hash, never a mutable name",
        )


def _validate_started_at(value: object, chair: str) -> None:
    """The serving moment has to be readable as a moment (#41).

    A non-blank string was the whole of the check, so `"not-a-timestamp"` recorded
    a serving moment nothing could ever recover. UTC is required rather than merely
    offered: receipts from two machines are compared, and a naive timestamp is only
    a moment if you already know which clock wrote it.
    """
    if not isinstance(value, str) or not value.strip():
        raise ReceiptRefusal(chair, "receipt field 'started_at' must be a non-blank string")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReceiptRefusal(
            chair, f"started_at {value!r} is not an ISO 8601 timestamp: {error}"
        ) from error
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ReceiptRefusal(chair, f"started_at {value!r} does not carry an explicit UTC offset")


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
