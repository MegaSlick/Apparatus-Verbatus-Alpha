"""The approval-record artifact — the one shape every approval is recorded in.

An approval is a human act: the project lead approves an exclusion, declares the
pipeline proven, or grants a permission. This module cannot enforce who signed —
a file says what it says — but it makes an approval *checkable*: one shape, self-hashed and bound to the exact
policy version it approved, so a claimed approval with no artifact is refused at
the schema and an artifact edited afterwards fails its own hash.

Naming the target's hash means a changed target needs a new approval; an approval
that named only the action would keep approving after the target changed.

Two schemas share this family. `approval-record.v0` carries every action except
`review`. `approval-record.v1` is an
operator review decision about one held unit or page of a run: action `review`, a
closed `review` block naming the run, scope, page, decision and finding, and a
`target_version_hash` that is the review's basis digest
(`common.review_decisions`). Readers accept both.

`timestamp` is present here and absent from every other artifact in this package.
Deterministic artifacts carry no timestamps, because two identical runs must
produce identical bytes. An approval is not deterministic output — it is a record
of a human act at a moment — so the moment is the point.
"""

from typing import Any, Final

from .canonical import is_sha256, self_hash, self_hash_refusal, verify_self_hash
from .errors import ApprovalRefusal

# The only approver, recorded as a role rather than a person's name, and as a value
# rather than assumed, so an artifact naming anyone else is refused by the schema.
APPROVER: Final = "project-lead"

# ``advance`` is deliberately distinct from ``other``. It is the one operator
# decision that can move a staged run forward, and readers must be able to find
# it without treating a free-text label as authority.
ACTIONS: Final = ("advance", "exclusion", "salvage-promotion", "review", "other")

SCHEMA_V0: Final = "approval-record.v0"
SCHEMA_V1: Final = "approval-record.v1"
REVIEW_ACTION: Final = "review"

# A review decision is about one unit (an act or other reading of a page) or one page.
UNIT_SCOPE: Final = "unit"
PAGE_SCOPE: Final = "page"
UNIT_DECISIONS: Final = ("release", "exclude", "hold", "re-ask")
PAGE_DECISIONS: Final = ("no-missed-act", "missed-act", "re-ask", "re-shoot", "hold")
REVIEW_DECISIONS: Final = {UNIT_SCOPE: UNIT_DECISIONS, PAGE_SCOPE: PAGE_DECISIONS}
# What a `hold` names. Correcting text, splitting and merging are findings, never
# decisions: the unit stays held and nothing is filed as gold.
FINDINGS: Final = (
    "text-misread",
    "split-needed",
    "merge-needed",
    "continuation-wrong",
    "region-wrong",
    "other",
)
REVIEW_FIELDS: Final = frozenset({"run_id", "scope", "page_id", "decision", "finding"})

# Bounds make a planted object a named refusal, not an unbounded allocation; the
# subject ceiling stays below the receipt reader's four-mebibyte record bound.
MAX_APPROVAL_SUBJECTS: Final = 384
MAX_APPROVAL_SUBJECT_BYTES: Final = 1024
MAX_APPROVAL_REASON_BYTES: Final = 256 * 1024
MAX_APPROVAL_TIMESTAMP_BYTES: Final = 256

# Ingress status must be part of self-hashed run authority. An absent field in a
# mutable door artifact is never proof that the run began as a fixture.
SYNTHETIC_FIXTURE_INGRESS: Final = "synthetic-fixture"
REAL_INGRESS: Final = "real"

_REQUIRED: Final = (
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
_V1_REQUIRED: Final = (*_REQUIRED, "review")
_V1_FIELDS: Final = frozenset({"schema", *_V1_REQUIRED})


class ApprovalRecordReference:
    """A digest-checked reference to an approval-record artifact.

    An approval is evidence of the project lead's act, not a caller assertion.  Carrying the
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
        # `type(...) is str`: a str subclass could override the comparison below.
        if type(subject) is not str or not subject.strip():
            raise ApprovalRefusal("an approval-record binding names no subject")
        if not is_sha256(target_version_hash):
            raise ApprovalRefusal("an approval-record binding names no target version")
        self.reference = reference
        self.subject = subject
        self.target_version_hash = target_version_hash


def synthetic_fixture_ingress_record() -> dict[str, str]:
    """Return the ingress record for the walking skeleton's declared synthetic pages."""
    return {"mode": SYNTHETIC_FIXTURE_INGRESS}


def real_ingress_record() -> dict[str, str]:
    """Return the ingress record for a real submission.

    Carries no approval evidence: real material never reaches git regardless of
    any run-level sign-off, so this names only which of the two known routes
    created the run.
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
    """Build a well-formed `approval-record.v0`, self-hash included."""
    if type(action) is not str:
        raise ApprovalRefusal(
            "an approval action is not an exact string from the closed vocabulary"
        )
    if action not in ACTIONS:
        raise ApprovalRefusal(f"action {action!r} is not one of {list(ACTIONS)}")
    if action == REVIEW_ACTION:
        raise ApprovalRefusal(
            "a review decision is an approval-record.v1; build it with "
            "build_review_decision_record, which binds its scope and basis"
        )
    _require_common_fields(subject_ids, reason, target_version_hash, timestamp)
    record: dict[str, Any] = {
        "schema": SCHEMA_V0,
        "subject_ids": sorted(subject_ids),
        "action": action,
        "approver": APPROVER,
        "reason": reason,
        "target_version_hash": target_version_hash,
        "timestamp": timestamp,
    }
    record["self_hash"] = self_hash(record)
    return record


def build_review_decision_record(
    *,
    run_id: str,
    scope: str,
    subject_id: str,
    page_id: str,
    decision: str,
    finding: str | None,
    basis_digest: str,
    reason: str,
    timestamp: str,
) -> dict[str, Any]:
    """Build one operator review decision as an `approval-record.v1`, self-hash included.

    `subject_id` is the unit's act id for a unit decision and the page id for a
    page decision; `basis_digest` is the review basis it was made against and
    becomes `target_version_hash`.
    """
    review = {
        "run_id": run_id,
        "scope": scope,
        "page_id": page_id,
        "decision": decision,
        "finding": finding,
    }
    _require_review_block(review, [subject_id])
    _require_common_fields([subject_id], reason, basis_digest, timestamp)
    record: dict[str, Any] = {
        "schema": SCHEMA_V1,
        "subject_ids": [subject_id],
        "action": REVIEW_ACTION,
        "approver": APPROVER,
        "reason": reason,
        "target_version_hash": basis_digest,
        "timestamp": timestamp,
        "review": review,
    }
    record["self_hash"] = self_hash(record)
    return record


def _require_review_block(review: Any, subjects: Any) -> None:
    """Refuse a review block that is not closed, or that its scope does not allow."""
    if not isinstance(review, dict) or set(review) != REVIEW_FIELDS:
        raise ApprovalRefusal(
            f"a review decision's review block must hold exactly {sorted(REVIEW_FIELDS)}"
        )
    for field in ("run_id", "page_id"):
        if not _bounded_text(review[field], MAX_APPROVAL_SUBJECT_BYTES):
            raise ApprovalRefusal(
                f"a review decision's {field} must be non-blank UTF-8 text no larger than "
                f"{MAX_APPROVAL_SUBJECT_BYTES} bytes"
            )
    scope, decision, finding = review["scope"], review["decision"], review["finding"]
    if type(scope) is not str or scope not in REVIEW_DECISIONS:
        raise ApprovalRefusal(
            f"a review decision's scope {scope!r} is not one of {sorted(REVIEW_DECISIONS)}"
        )
    if type(decision) is not str or decision not in REVIEW_DECISIONS[scope]:
        raise ApprovalRefusal(
            f"{decision!r} is not a {scope} decision; a {scope} decision is one of "
            f"{list(REVIEW_DECISIONS[scope])}"
        )
    if decision == "hold":
        if type(finding) is not str or finding not in FINDINGS:
            raise ApprovalRefusal(
                f"a hold names its finding, one of {list(FINDINGS)}; {finding!r} is not one"
            )
    elif finding is not None:
        raise ApprovalRefusal(f"only a hold names a finding; a {decision} names none")
    if type(subjects) is not list or len(subjects) != 1:
        raise ApprovalRefusal("a review decision names exactly one subject")
    if scope == PAGE_SCOPE and subjects[0] != review["page_id"]:
        raise ApprovalRefusal("a page decision's subject is the page it names")


def _require_common_fields(
    subject_ids: Any, reason: Any, target_version_hash: Any, timestamp: Any
) -> None:
    """The checks every approval record's builder makes, whatever its schema."""
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
    if not is_sha256(target_version_hash):
        raise ApprovalRefusal(
            "an approval must name the lowercase sha256 of the exact policy or target version "
            "it approved, or it goes on approving something that changed underneath it"
        )
    if not _bounded_text(timestamp, MAX_APPROVAL_TIMESTAMP_BYTES):
        raise ApprovalRefusal(
            "an approval with no timestamp cannot be reviewed later; when it was given "
            f"is half of what makes it checkable against the version it approved, and it must "
            f"be no larger than {MAX_APPROVAL_TIMESTAMP_BYTES} UTF-8 bytes"
        )


def validate_approval_record(record: Any) -> dict[str, Any]:
    """Refuse anything that is not a sound, unedited approval record."""
    if not isinstance(record, dict):
        raise ApprovalRefusal("approval record is not an object")
    v1 = type(record.get("schema")) is str and record["schema"] == SCHEMA_V1
    fields, required = (_V1_FIELDS, _V1_REQUIRED) if v1 else (_FIELDS, _REQUIRED)
    unexpected = []
    more_unexpected = False
    for key in record:
        if key not in fields:
            if len(unexpected) == len(fields):
                more_unexpected = True
                break
            unexpected.append(key)
    unexpected.sort(key=repr)
    if unexpected:
        suffix = " or more" if more_unexpected else ""
        raise ApprovalRefusal(
            f"approval record has unexpected fields {unexpected}{suffix}; its schema is closed"
        )
    missing = [field for field in required if field not in record]
    if missing:
        raise ApprovalRefusal(f"approval record is missing {missing}")
    schema = record["schema"]
    if type(schema) is not str:
        raise ApprovalRefusal("approval record schema is not an exact string")
    if schema not in (SCHEMA_V0, SCHEMA_V1):
        raise ApprovalRefusal(f"approval record has schema {schema!r}")
    approver = record["approver"]
    if type(approver) is not str:
        raise ApprovalRefusal("approval record approver is not an exact string")
    if approver != APPROVER:
        raise ApprovalRefusal(
            f"approval record names approver {approver!r}; only the project lead "
            f"({APPROVER!r}) approves, and no agent stands in for them"
        )
    action = record["action"]
    if type(action) is not str:
        raise ApprovalRefusal("approval record action is not an exact string")
    if action not in ACTIONS:
        raise ApprovalRefusal(f"approval record has action {action!r}")
    if (action == REVIEW_ACTION) != v1:
        raise ApprovalRefusal(
            f"approval record {schema} has action {action!r}; the review action is "
            f"{SCHEMA_V1}'s, and only its"
        )
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
    # At least as strict as the builder: a self-hash verifies blank fields too.
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
    if not is_sha256(record["target_version_hash"]):
        raise ApprovalRefusal(
            "approval record target_version_hash is not a lowercase sha256; an approval "
            "without a checkable target version cannot be current"
        )
    if v1:
        _require_review_block(record["review"], subjects)
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
