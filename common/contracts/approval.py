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

from typing import Any, Final

from .canonical import self_hash, verify_self_hash
from .errors import ApprovalRefusal

# The only human in these rules. Recorded as a value rather than assumed, so an
# artifact naming anyone else is refused by the schema rather than by convention.
APPROVER: Final = "Tyrel"

ACTIONS: Final = ("exclusion", "salvage-promotion", "data-gate", "other")

_REQUIRED: Final = (
    "subject_ids",
    "action",
    "approver",
    "reason",
    "target_version_hash",
    "timestamp",
    "self_hash",
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
    if not subject_ids:
        raise ApprovalRefusal("an approval that names no subject approves nothing")
    if not reason or not reason.strip():
        raise ApprovalRefusal(
            "an approval with no reason is unreviewable later; the reason is the "
            "part a reader six weeks out actually needs"
        )
    if not target_version_hash:
        raise ApprovalRefusal(
            "an approval must name the exact policy or target version it approved, "
            "or it goes on approving something that changed underneath it"
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
    if not isinstance(record["subject_ids"], list) or not record["subject_ids"]:
        raise ApprovalRefusal("approval record names no subjects")
    # The validator is the gate for records read off disk, so it has to be at
    # least as strict as the builder. It was not: the builder refuses an empty
    # reason or target version, and this only checked that the keys existed — so a
    # record written by hand or by another tool could pass with both blank, and
    # its self-hash would verify happily, because a hash covers whatever bytes
    # were sealed rather than whether they meant anything. The exact-version
    # binding this module exists for would then name no target at all.
    for field in ("reason", "target_version_hash", "timestamp"):
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise ApprovalRefusal(
                f"approval record field {field!r} is empty or not a string; an "
                "approval that does not say what it approved, why, or when is "
                "unreviewable later, which is the whole point of writing it down"
            )
    if not verify_self_hash(record):
        raise ApprovalRefusal(
            "approval record fails its own self-hash: it was edited after it was "
            "sealed, and an edited approval is not an approval"
        )
    return record
