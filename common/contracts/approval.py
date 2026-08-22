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

**Cut 2026-08-09, per Tyrel's ruling that session.** `data-gate` used to be a third
action here, backing a per-run approval-record requirement for real input: none of
this pipeline's material ever reaches git (it runs on a GPU host, `workbench/` is
gitignored, and an ingress check plus a pre-push payload scan already cover that
mechanically), so the extra sign-off bought nothing and is gone. `exclusion` and
`salvage-promotion` remain — GOVERNANCE 1 still requires Tyrel's approval for an
exclusion, and that is governance, not something this cut touches.
"""

from typing import Any, Final

from .canonical import self_hash, verify_self_hash
from .errors import ApprovalRefusal

# The only human in these rules. Recorded as a value rather than assumed, so an
# artifact naming anyone else is refused by the schema rather than by convention.
APPROVER: Final = "Tyrel"

ACTIONS: Final = ("exclusion", "salvage-promotion", "other")

# Ingress status must be part of self-hashed run authority. An absent field in a
# mutable door artifact is never proof that the run began as a fixture.
SYNTHETIC_FIXTURE_INGRESS: Final = "synthetic-fixture"
REAL_INGRESS: Final = "real"

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


class ApprovalRecordBinding:
    """The subject/version facts verified with one approval-record reference.

    A bare content address cannot tell a sampling arm which experiment the
    record approved.  The sampling gate returns this binding only after reading
    the referenced record and checking its exact subject and target version;
    the arm can then refuse a valid approval for the other experiment instead
    of trusting that its caller threaded the right reference.
    """

    __slots__ = ("reference", "subject", "target_version_hash")

    def __init__(
        self,
        reference: ApprovalRecordReference,
        subject: str,
        target_version_hash: str,
    ):
        if not isinstance(reference, ApprovalRecordReference):
            raise ApprovalRefusal("an approval-record binding has no typed reference")
        if not isinstance(subject, str) or not subject.strip():
            raise ApprovalRefusal("an approval-record binding names no subject")
        if not _is_sha256(target_version_hash):
            raise ApprovalRefusal("an approval-record binding names no target version")
        self.reference = reference
        self.subject = subject
        self.target_version_hash = target_version_hash


def synthetic_fixture_ingress_record() -> dict[str, str]:
    """Return the ingress record for the walking skeleton's declared synthetic pages."""
    return {"mode": SYNTHETIC_FIXTURE_INGRESS}


def real_ingress_record() -> dict[str, str]:
    """Return the ingress record for a real submission.

    Carries no approval evidence: cut 2026-08-09, this mode used to bind a
    data-gate policy hash and an approval reference here. Real material never
    reaches git regardless of any run-level sign-off, so the record now says only
    which of the two known routes created the run.
    """
    return {"mode": REAL_INGRESS}


def parse_ingress_record(value: Any) -> str:
    """Decode the closed ingress record from a run authority: fixture or real."""
    if (
        not isinstance(value, dict)
        or set(value) != {"mode"}
        or value.get("mode")
        not in (
            SYNTHETIC_FIXTURE_INGRESS,
            REAL_INGRESS,
        )
    ):
        raise ApprovalRefusal("run ingress evidence is not a closed fixture-or-real record")
    return value["mode"]


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
    # The last asymmetry between this and the validator. The validator refuses a
    # blank or non-string timestamp; this did not check it at all, so a caller
    # could seal `timestamp="   "` here and no reader would ever accept it back.
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ApprovalRefusal(
            "an approval with no timestamp cannot be reviewed later; when it was given "
            "is half of what makes it checkable against the version it approved"
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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
