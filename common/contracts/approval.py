"""The approval-record artifact — the one shape every Tyrel-approval is recorded in.

GOVERNANCE: only Tyrel approves an exclusion, declares the pipeline proven, or
grants the permissions the rules require. No automated agent may act as the human
in any rule. This module cannot enforce that — a file says what it says — but it
can make an approval *checkable*: one shape, self-hashed, naming the exact policy
version it approved, so a claimed approval with no artifact is refused at the
schema and an artifact edited afterwards fails its own hash.

The exact-version binding is the part that earns its keep. An approval that named
only the action would silently keep approving after the thing it approved changed
underneath it; naming the target's hash means a changed target needs a new
approval, which is the honest behaviour.

`timestamp` is present here and absent from every other artifact in this package.
Deterministic artifacts carry no timestamps, because two identical runs must
produce identical bytes. An approval is not deterministic output — it is a record
of a human act at a moment — so the moment is the point.
"""

import json
from collections.abc import Callable
from typing import Any, Final

from .canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from .errors import ApprovalRefusal

# The only human in these rules. Recorded as a value rather than assumed, so an
# artifact naming anyone else is refused by the schema rather than by convention.
APPROVER: Final = "Tyrel"

ACTIONS: Final = ("exclusion", "salvage-promotion", "data-gate", "other")

# Ingress status must be part of self-hashed run authority. An absent gate field
# in a mutable door artifact is never proof that the run began as a fixture.
SYNTHETIC_FIXTURE_INGRESS: Final = "synthetic-fixture"
APPROVAL_GATED_REAL_INGRESS: Final = "approval-gated-real"

_REQUIRED: Final = (
    "subject_ids",
    "action",
    "approver",
    "reason",
    "target_version_hash",
    "timestamp",
    "self_hash",
)


class ApprovalRecordReference:
    """A digest-checked reference to an approval-record artifact.

    An approval is evidence of Tyrel's act, not a caller assertion.  Carrying the
    path and digest together lets a consumer verify the stored bytes before it
    trusts the record they decode to.  This mirrors ``RunReceiptReference`` while
    keeping the approval contract independent of the run-tree writer.
    """

    __slots__ = ("relative_path", "sha256")

    def __init__(self, relative_path: str, sha256: str):
        self.relative_path = relative_path
        self.sha256 = sha256

    def to_record(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "sha256": self.sha256}

    def __repr__(self) -> str:
        return f"ApprovalRecordReference({self.relative_path!r}, sha256={self.sha256!r})"


def approval_record_reference_from_record(value: Any) -> ApprovalRecordReference:
    """Decode a persisted reference once into the typed approval boundary.

    Artifact payloads are JSON records, so a consumer must decode that transport
    shape at its edge.  The returned object, rather than the raw dictionary,
    is what every approval reader receives thereafter.
    """
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise ApprovalRefusal(
            "data-gate approval reference record must contain exactly relative_path and sha256"
        )
    return _approval_record_reference(
        ApprovalRecordReference(value["relative_path"], value["sha256"])
    )


def synthetic_fixture_ingress_record() -> dict[str, str]:
    """Return the only approval-free ingress record System 03 recognizes."""
    return {"mode": SYNTHETIC_FIXTURE_INGRESS}


def approval_gated_real_ingress_record(
    policy_hash: str, reference: ApprovalRecordReference
) -> dict[str, Any]:
    """Serialize real-input approval evidence into self-hashed run authority."""
    if not _is_sha256(policy_hash):
        raise ApprovalRefusal("data-gate ingress has no lowercase policy hash")
    parsed = _approval_record_reference(reference)
    return {
        "mode": APPROVAL_GATED_REAL_INGRESS,
        "data_gate_policy_hash": policy_hash,
        "data_gate_approval_ref": parsed.to_record(),
    }


def parse_data_gate_ingress_record(
    value: Any,
) -> tuple[str, str | None, ApprovalRecordReference | None]:
    """Decode the closed ingress record from a run authority."""
    if not isinstance(value, dict):
        raise ApprovalRefusal("run ingress evidence is missing or not an object")
    if value == synthetic_fixture_ingress_record():
        return SYNTHETIC_FIXTURE_INGRESS, None, None
    expected = {"mode", "data_gate_policy_hash", "data_gate_approval_ref"}
    if set(value) != expected or value.get("mode") != APPROVAL_GATED_REAL_INGRESS:
        raise ApprovalRefusal("run ingress evidence is not a closed fixture or real-input record")
    policy_hash = value["data_gate_policy_hash"]
    if not _is_sha256(policy_hash):
        raise ApprovalRefusal("real run ingress has no lowercase data-gate policy hash")
    return (
        APPROVAL_GATED_REAL_INGRESS,
        policy_hash,
        approval_record_reference_from_record(value["data_gate_approval_ref"]),
    )


def build_approval_record(
    subject_ids: list[str],
    action: str,
    reason: str,
    target_version_hash: str,
    timestamp: str,
) -> dict[str, Any]:
    """Build a well-formed approval record, self-hash included."""
    if action not in ACTIONS:
        raise ApprovalRefusal(f"action {action!r} is not one of {list(ACTIONS)}")
    if not subject_ids or any(
        not isinstance(subject, str) or not subject.strip() for subject in subject_ids
    ):
        raise ApprovalRefusal("an approval that names no subject approves nothing")
    if not reason or not reason.strip():
        raise ApprovalRefusal(
            "an approval with no reason is unreviewable later; the reason is the "
            "part a reader six weeks out actually needs"
        )
    if not _is_sha256(target_version_hash):
        raise ApprovalRefusal(
            "an approval must name the lowercase sha256 of the exact policy or target version "
            "it approved, or it goes on approving something that changed underneath it"
        )
    record: dict[str, Any] = {
        "schema": "approval-record.v0",
        "subject_ids": sorted(subject_ids),
        "action": action,
        "approver": APPROVER,
        "reason": reason,
        "target_version_hash": target_version_hash,
        "timestamp": timestamp,
    }
    record["self_hash"] = self_hash(record)
    return record


def validate_approval_record(record: Any) -> dict[str, Any]:
    """Refuse anything that is not a sound, unedited approval record."""
    if not isinstance(record, dict):
        raise ApprovalRefusal("approval record is not an object")
    missing = [field for field in _REQUIRED if field not in record]
    if missing:
        raise ApprovalRefusal(f"approval record is missing {missing}")
    if record.get("schema") != "approval-record.v0":
        raise ApprovalRefusal(f"approval record has schema {record.get('schema')!r}")
    if record["approver"] != APPROVER:
        raise ApprovalRefusal(
            f"approval record names approver {record['approver']!r}; only "
            f"{APPROVER} approves, and no agent stands in for him"
        )
    if record["action"] not in ACTIONS:
        raise ApprovalRefusal(f"approval record has action {record['action']!r}")
    if (
        not isinstance(record["subject_ids"], list)
        or not record["subject_ids"]
        or any(
            not isinstance(subject, str) or not subject.strip() for subject in record["subject_ids"]
        )
    ):
        raise ApprovalRefusal("approval record names no subjects")
    # The validator is the gate for records read off disk, so it has to be at
    # least as strict as the builder. It was not: the builder refuses an empty
    # reason or target version, and this only checked that the keys existed — so a
    # record written by hand or by another tool could pass with both blank, and
    # its self-hash would verify happily, because a hash covers whatever bytes
    # were sealed rather than whether they meant anything. The exact-version
    # binding this module exists for would then name no target at all.
    for field in ("reason", "timestamp"):
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise ApprovalRefusal(
                f"approval record field {field!r} is empty or not a string; an "
                "approval that does not say what it approved, why, or when is "
                "unreviewable later, which is the whole point of writing it down"
            )
    if not _is_sha256(record["target_version_hash"]):
        raise ApprovalRefusal(
            "approval record target_version_hash is not a lowercase sha256; an approval "
            "without a checkable target version cannot be current"
        )
    if not verify_self_hash(record):
        raise ApprovalRefusal(
            "approval record fails its own self-hash: it was edited after it was "
            "sealed, and an edited approval is not an approval"
        )
    return record


def data_gate_policy_hash(policy_content: Any) -> str:
    """Hash the policy's recorded content through the canonical serialization.

    The policy version is its content, not a caller-supplied label.  A policy that
    cannot be represented canonically has no checkable version and therefore
    cannot be approved.
    """
    try:
        return digest_bytes(canonical_bytes(policy_content))
    except (TypeError, ValueError) as error:
        raise ApprovalRefusal(f"canonical policy hash could not be computed: {error}") from error


def require_current_data_gate_approval(
    policy_content: Any,
    reference: ApprovalRecordReference | None,
    read_bytes: Callable[[str], bytes],
) -> dict[str, Any]:
    """Return the current data-gate approval or refuse by the failed check.

    ``read_bytes`` is the door's path-resolution boundary.  This contract owns
    the reference and record checks, but it does not invent a policy file, a
    storage root, or a sixth run-tree shape.  The callback receives only the
    checked relative path carried by the reference.
    """
    if reference is None:
        raise ApprovalRefusal(
            "data-gate approval is missing; real input requires a current approval-record artifact"
        )

    parsed = _approval_record_reference(reference)
    try:
        data = read_bytes(parsed.relative_path)
    except OSError as error:
        raise ApprovalRefusal(
            f"data-gate approval reference {parsed.relative_path!r} could not be read: {error}"
        ) from error
    if not isinstance(data, bytes):
        raise ApprovalRefusal(
            f"data-gate approval reference {parsed.relative_path!r} did not resolve to bytes"
        )

    actual_digest = digest_bytes(data)
    if actual_digest != parsed.sha256:
        raise ApprovalRefusal(
            "data-gate approval reference digest mismatch: "
            f"{parsed.relative_path!r} has {actual_digest}, not {parsed.sha256}"
        )

    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ApprovalRefusal(
            f"data-gate approval reference {parsed.relative_path!r} is not a JSON "
            f"approval record: {error}"
        ) from error
    record = validate_approval_record(decoded)
    if record["action"] != "data-gate":
        raise ApprovalRefusal(
            f"data-gate approval record has action {record['action']!r}, not 'data-gate'"
        )

    current_hash = data_gate_policy_hash(policy_content)
    if record["target_version_hash"] != current_hash:
        raise ApprovalRefusal(
            "data-gate approval is stale: it names policy hash "
            f"{record['target_version_hash']}, not the current {current_hash}"
        )
    return record


def _approval_record_reference(value: ApprovalRecordReference) -> ApprovalRecordReference:
    if isinstance(value, ApprovalRecordReference):
        relative_path, digest = value.relative_path, value.sha256
    else:
        raise ApprovalRefusal(
            "data-gate approval reference must be an ApprovalRecordReference, not a raw dictionary"
        )

    if not isinstance(relative_path, str) or not relative_path:
        raise ApprovalRefusal("data-gate approval reference has no relative_path")
    if relative_path.startswith("/") or ".." in relative_path.split("/"):
        raise ApprovalRefusal(
            f"data-gate approval reference {relative_path!r} is not a safe relative path"
        )
    if not _is_sha256(digest):
        raise ApprovalRefusal("data-gate approval reference has no lowercase sha256")
    expected_path = f"receipts/sha256/{digest}.json"
    if relative_path != expected_path:
        raise ApprovalRefusal(
            f"data-gate approval reference {relative_path!r} is not its "
            f"content-addressed path {expected_path!r}"
        )
    return ApprovalRecordReference(relative_path, digest)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
