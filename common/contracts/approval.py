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

from .canonical import self_hash, self_hash_refusal, verify_self_hash
from .errors import ApprovalRefusal

# The only human in these rules. Recorded as a value rather than assumed, so an
# artifact naming anyone else is refused by the schema rather than by convention.
APPROVER: Final = "Tyrel"

# ``advance`` is deliberately distinct from ``other``. It is the one operator
# decision that can move a staged run forward, and readers must be able to find
# it without treating a free-text label as authority.
ACTIONS: Final = ("advance", "exclusion", "salvage-promotion", "other")

# Approval records are small operator-authored evidence, but both entry points
# hash the whole object and the builder sorts every subject.  Bounds make a
# planted object a named refusal rather than an unbounded allocation.  The
# subject ceiling still permits a large explicit batch while keeping its maximum
# encoded text below the receipt reader's four-mebibyte record bound.
MAX_APPROVAL_SUBJECTS: Final = 384
MAX_APPROVAL_SUBJECT_BYTES: Final = 1024
MAX_APPROVAL_REASON_BYTES: Final = 256 * 1024
MAX_APPROVAL_TIMESTAMP_BYTES: Final = 256

# Ingress status must be part of self-hashed run authority. An absent field in a
# mutable door artifact is never proof that the run began as a fixture.
SYNTHETIC_FIXTURE_INGRESS: Final = "synthetic-fixture"
REAL_INGRESS: Final = "real"

_REQUIRED: Final = (
    # `schema` is required like every other field, so a record that simply omits
    # it is named as absent rather than reported below as a schema of the wrong
    # type — a diagnostic that sent the reader looking for a value that was
    # never there.
    "schema",
    "subject_ids",
    "action",
    "approver",
    "reason",
    "target_version_hash",
    "timestamp",
    "self_hash",
)
_FIELDS: Final = frozenset({"schema", *_REQUIRED})


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
        # `type(...) is str`, not `isinstance`, for the reason this module states
        # at `_text_field_refusal`: a str subclass can override comparison, and
        # the arm that decides which experiment an approval covers does so by
        # comparing this subject against a named constant.
        if type(subject) is not str or not subject.strip():
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
    if type(action) is not str:
        raise ApprovalRefusal(
            "an approval action is not an exact string from the closed vocabulary"
        )
    if action not in ACTIONS:
        raise ApprovalRefusal(f"action {action!r} is not one of {list(ACTIONS)}")
    if type(subject_ids) is not list or not subject_ids:
        raise ApprovalRefusal("an approval that names no subject approves nothing")
    if len(subject_ids) > MAX_APPROVAL_SUBJECTS:
        raise ApprovalRefusal(
            f"an approval names more than {MAX_APPROVAL_SUBJECTS} subjects; the explicit "
            "approval record is bounded"
        )
    if any(not _bounded_text(subject, MAX_APPROVAL_SUBJECT_BYTES) for subject in subject_ids):
        raise ApprovalRefusal(
            f"an approval subject must be non-blank UTF-8 text no larger than "
            f"{MAX_APPROVAL_SUBJECT_BYTES} bytes"
        )
    if len(set(subject_ids)) != len(subject_ids):
        raise ApprovalRefusal("an approval may name each subject only once")
    if not _bounded_text(reason, MAX_APPROVAL_REASON_BYTES):
        raise ApprovalRefusal(
            "an approval with no reason is unreviewable later; the reason is the "
            f"part a reader six weeks out actually needs, and it must be no larger than "
            f"{MAX_APPROVAL_REASON_BYTES} UTF-8 bytes"
        )
    if not _is_sha256(target_version_hash):
        raise ApprovalRefusal(
            "an approval must name the lowercase sha256 of the exact policy or target version "
            "it approved, or it goes on approving something that changed underneath it"
        )
    # The last asymmetry between this and the validator. The validator refuses a
    # blank or non-string timestamp; this did not check it at all, so a caller
    # could seal `timestamp="   "` here and no reader would ever accept it back.
    if not _bounded_text(timestamp, MAX_APPROVAL_TIMESTAMP_BYTES):
        raise ApprovalRefusal(
            "an approval with no timestamp cannot be reviewed later; when it was given "
            f"is half of what makes it checkable against the version it approved, and it must "
            f"be no larger than {MAX_APPROVAL_TIMESTAMP_BYTES} UTF-8 bytes"
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
    unexpected = []
    more_unexpected = False
    for key in record:
        if key not in _FIELDS:
            if len(unexpected) == len(_FIELDS):
                more_unexpected = True
                break
            unexpected.append(key)
    unexpected.sort(key=repr)
    if unexpected:
        suffix = " or more" if more_unexpected else ""
        raise ApprovalRefusal(
            f"approval record has unexpected fields {unexpected}{suffix}; its schema is closed"
        )
    missing = [field for field in _REQUIRED if field not in record]
    if missing:
        raise ApprovalRefusal(f"approval record is missing {missing}")
    schema = record["schema"]
    if type(schema) is not str:
        raise ApprovalRefusal("approval record schema is not an exact string")
    if schema != "approval-record.v0":
        raise ApprovalRefusal(f"approval record has schema {schema!r}")
    approver = record["approver"]
    if type(approver) is not str:
        raise ApprovalRefusal("approval record approver is not an exact string")
    if approver != APPROVER:
        raise ApprovalRefusal(
            f"approval record names approver {approver!r}; only "
            f"{APPROVER} approves, and no agent stands in for him"
        )
    action = record["action"]
    if type(action) is not str:
        raise ApprovalRefusal("approval record action is not an exact string")
    if action not in ACTIONS:
        raise ApprovalRefusal(f"approval record has action {action!r}")
    subjects = record["subject_ids"]
    if type(subjects) is not list or not subjects:
        raise ApprovalRefusal("approval record names no subjects")
    if len(subjects) > MAX_APPROVAL_SUBJECTS:
        raise ApprovalRefusal(f"approval record names more than {MAX_APPROVAL_SUBJECTS} subjects")
    if any(not _bounded_text(subject, MAX_APPROVAL_SUBJECT_BYTES) for subject in subjects):
        raise ApprovalRefusal(
            f"approval record subjects must be non-blank UTF-8 text no larger than "
            f"{MAX_APPROVAL_SUBJECT_BYTES} bytes"
        )
    if len(set(subjects)) != len(subjects):
        raise ApprovalRefusal("approval record names the same subject more than once")
    if subjects != sorted(subjects):
        raise ApprovalRefusal(
            "approval record subjects are not in canonical order; one subject set must have "
            "one content address"
        )
    # The validator is the gate for records read off disk, so it has to be at
    # least as strict as the builder. It was not: the builder refuses an empty
    # reason or target version, and this only checked that the keys existed — so a
    # record written by hand or by another tool could pass with both blank, and
    # its self-hash would verify happily, because a hash covers whatever bytes
    # were sealed rather than whether they meant anything. The exact-version
    # binding this module exists for would then name no target at all.
    for field, maximum in (
        ("reason", MAX_APPROVAL_REASON_BYTES),
        ("timestamp", MAX_APPROVAL_TIMESTAMP_BYTES),
    ):
        problem = _text_field_refusal(record[field], maximum)
        if problem is not None:
            raise ApprovalRefusal(
                f"approval record field {field!r} {problem}; an approval that does not "
                "say what it approved, why, or when is unreviewable later, which is the "
                f"whole point of writing it down; the field is bounded to {maximum} "
                "UTF-8 bytes"
            )
    if not _is_sha256(record["target_version_hash"]):
        raise ApprovalRefusal(
            "approval record target_version_hash is not a lowercase sha256; an approval "
            "without a checkable target version cannot be current"
        )
    if not verify_self_hash(record):
        # Unhashable current contents permit no digest comparison, so they must
        # not be described as proof that an approval was edited after sealing.
        unhashable = self_hash_refusal(record)
        if unhashable is not None:
            raise ApprovalRefusal(f"approval record fails its own self-hash: {unhashable}")
        raise ApprovalRefusal(
            "approval record fails its own self-hash: it was edited after it was "
            "sealed, and an edited approval is not an approval"
        )
    return record


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _text_field_refusal(value: Any, maximum_bytes: int) -> str | None:
    """Why this is not sound approval text, named, or None if it is.

    Named rather than boolean because the four ways a field fails are not one
    fact. A surrogate in `reason` in particular must be reported as the
    unencodable character it is: describing it as "empty or not a string" tells a
    reader the wrong thing about a record that will never hash, and the seal's
    own diagnostic (`self_hash_refusal`) is not reached once this check fires.

    `type(value) is not str`, not `isinstance`: a str subclass can override
    comparison, hashing or encoding, and an approval is a record whose exact
    bytes are the evidence.
    """
    if type(value) is not str:
        return "is not an exact string"
    if not value.strip():
        return "is empty"
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        offender = value[error.start : error.start + 1]
        return f"contains an unencodable character {offender!a}"
    if len(encoded) > maximum_bytes:
        return f"exceeds {maximum_bytes} UTF-8 bytes"
    return None


def _bounded_text(value: Any, maximum_bytes: int) -> bool:
    return _text_field_refusal(value, maximum_bytes) is None
