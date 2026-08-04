"""The data-handling gate: fail-closed machinery, not prose.

The gate *package* — the written policy Tyrel approves — is a deliverable outside
this repository (spec 03: "deliverable to Tyrel, not code"). What belongs here is
the machinery that makes his approval checkable: a policy whose bytes hash the same
way every other record in this tree hashes, an approval record bound to the exact
policy version it approved, and a door that refuses real input without a current
one.

**Ruling 2026-08-04, item 1 — fixture status is never a flag.** The gate exposes
only a real-input check. The door's fixture route is selected by the repository's
own declared fixture root and loaded manifest, and the self-hashed run ingress
records which route created it. Nothing here accepts a filename, folder name,
command-line switch, or boolean that can relabel real material as a fixture.

**Ruling 2026-08-04, item 2 — the canonical policy bytes are the policy file
itself.** `policy_hash` is `digest_bytes(canonical_bytes(...))` over the record
`load_policy` reads, the same function the rest of the tree hashes with. That
function lives in `common/contracts/approval.py` as `data_gate_policy_hash`; this
module holds the file's location and its shape checks, not a second hash.

**Ruling 2026-08-04, item 3 — the approval travels as a digest-checked
reference**, exactly as a serving receipt does. The reference type, the record
checks and the staleness comparison are all
`common.contracts.approval.require_current_data_gate_approval`; this module supplies
the boundary that turns a relative path into bytes, and nothing else.

The approval a *run* was admitted under is stronger than either: it is sealed into
`run.json`'s self-hashed authority as the run's `ingress`, so "was this run
approval-gated or synthetic?" is answered by the run authority rather than by an
optional field on a stage artifact that could simply be absent.

**Why this lives in `operations/submit/` and not beside the door.** The gate is
about material *arriving*, which is the submit door's whole subject, and putting it
here keeps the dependency between the two trees pointing one way:
`pipeline/1_exemplar/door.py` imports this and `inventory.py`, and nothing in
`operations/submit/` imports the pipeline. Enforcement is still at the door — the
door calls `enforce` itself, on its own admission loop, rather than trusting that
some earlier tool did.
"""

import json
from pathlib import Path
from typing import Any, Final

from common.contracts.approval import (
    ApprovalRecordReference,
    data_gate_policy_hash,
    require_current_data_gate_approval,
)
from common.contracts.errors import ApprovalRefusal, ContractError

ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH: Final = ROOT / "config" / "data_handling_policy.json"

# Only this action authorizes real input at this door. `common/contracts/approval.py`
# also allows `exclusion`, `salvage-promotion` and `other`; those approve different
# things and do not authorize this gate, whatever their target hash says.
GATE_ACTION: Final = "data-gate"

_REQUIRED_POLICY_FIELDS: Final = (
    "policy_version",
    "storage_roots",
    "logging_rule",
    "temp_file_handling",
    "retention_and_deletion",
    "routing_rule",
    "third_party_transmission",
    "cleanup_drill",
)


class GateRefusal(ContractError):
    """Real input arrived without a current, valid data-handling approval.

    Not an `ApprovalRefusal`: that class means a claimed approval failed its own
    schema. This means the gate itself refuses — including the case where no
    approval was offered at all, which is not a malformed approval, it is none.
    """


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """The canonical policy record, read fresh from disk every time.

    Never cached: a cached policy could disagree with the file a stale approval
    was actually compared against, and the gate's whole point is comparing against
    what is *currently* on disk. Every clause the spec requires the package to
    carry must be present — a policy missing its retention rule is not a shorter
    policy, it is one Tyrel did not approve.
    """
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateRefusal(f"data-handling policy at {path} could not be read: {error}") from error
    if not isinstance(record, dict):
        raise GateRefusal(f"{path} is not a data-handling policy record")
    missing = [field for field in _REQUIRED_POLICY_FIELDS if not record.get(field)]
    if missing:
        raise GateRefusal(
            f"{path} is missing required policy clause(s) {missing}; a policy with a "
            "clause absent is not a shorter policy, it is one that was never approved"
        )
    return record


def policy_hash(policy: dict[str, Any]) -> str:
    """The exact-version binding an approval names."""
    return data_gate_policy_hash(policy)


def load_approval(
    reference: ApprovalRecordReference | None,
    *,
    root: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Read and verify one approval through its digest-checked reference.

    Every check that matters — the reference's shape and content address, the
    bytes' digest, the record's schema and self-hash, the `data-gate` action, and
    the policy-version comparison — belongs to the contract. This function is only
    the boundary that resolves a checked relative path to bytes without leaving
    `root`.
    """
    resolved_root = Path(root).resolve()

    def read_bytes(relative_path: str) -> bytes:
        path = (resolved_root / relative_path).resolve()
        if not path.is_relative_to(resolved_root):
            raise OSError(f"approval reference {relative_path!r} resolves outside its root")
        return path.read_bytes()

    return require_current_data_gate_approval(policy, reference, read_bytes)


def enforce(*, approval: dict[str, Any] | None, policy: dict[str, Any] | None) -> None:
    """Refuse real input without a current, valid data-handling-gate approval.

    This function has no fixture switch. The declared fixture route does not call
    the real-input gate; every route that does call it is real. A boolean here would
    let any caller relabel real material as fixture material, which is precisely the
    bypass the gate exists to prevent.

    This is deliberately checkable against a validated record already in hand.
    The door first reloads that record through the digest-checked reference sealed
    into run authority, then applies this policy/action check at its own admission
    loop. That keeps the second boundary independent of an earlier caller's word.
    """
    if policy is None:
        raise GateRefusal(
            "real input cannot be checked against an absent policy; a missing policy "
            "is a failed check, never a passed one"
        )
    if approval is None:
        raise GateRefusal(
            "real input requires a current data-handling-gate approval-record "
            "artifact naming this exact policy version; none was supplied"
        )
    if approval.get("action") != GATE_ACTION:
        raise GateRefusal(
            f"approval record names action {approval.get('action')!r}, not {GATE_ACTION!r}; "
            "it does not authorize real input at this door"
        )
    current = policy_hash(policy)
    if approval.get("target_version_hash") != current:
        raise GateRefusal(
            f"approval names policy version {approval.get('target_version_hash')}, but the "
            f"current policy is {current}: the approval is stale and refuses"
        )


def approved_storage_roots(policy: dict[str, Any]) -> tuple[Path, ...]:
    """The exact locations the approved policy allows real material to live in.

    A relative entry is resolved against the repository root, so `private/` in the
    policy means this checkout's `private/` and not whatever `private/` the current
    working directory happens to sit beside.
    """
    raw_roots = policy.get("storage_roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise GateRefusal("the data-handling policy names no approved storage roots")
    roots: list[Path] = []
    for raw_root in raw_roots:
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise GateRefusal("a data-handling storage root is not a non-empty path")
        candidate = Path(raw_root)
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise GateRefusal(
                f"approved storage root {raw_root!r} does not exist; an unresolvable "
                "root is a failed check, never an unrestricted one"
            ) from error
        if not resolved.is_dir():
            raise GateRefusal(f"approved storage root {raw_root!r} is not a directory")
        roots.append(resolved)
    return tuple(roots)


def require_approved_storage_location(
    location: Path, approved_roots: tuple[Path, ...], label: str
) -> Path:
    """Refuse a real input or output location outside the approved storage roots."""
    location = Path(location)
    if location.is_symlink():
        raise GateRefusal(
            f"the {label} is a symlink; an approved storage root cannot be entered by redirect"
        )
    try:
        resolved = location.resolve(strict=False)
    except OSError as error:
        raise GateRefusal(
            f"the {label} could not be resolved against the approved storage roots"
        ) from error
    if not any(resolved.is_relative_to(root) for root in approved_roots):
        raise GateRefusal(
            f"the {label} is outside every approved storage root "
            f"{[str(root) for root in approved_roots]}; the policy decides where real "
            "material may live, and an unlisted location is refused rather than allowed"
        )
    return resolved


def read_external_approval(
    path: Path, policy: dict[str, Any]
) -> tuple[dict[str, Any], ApprovalRecordReference]:
    """Check an approval record supplied as a loose file, before a run tree exists.

    The door needs the approval verified *before* it creates the run, because the
    run authority seals the approval reference into itself. The reference returned
    is the content address the record will occupy once stored, so the caller can
    compare what it verified against what the store actually wrote — a record that
    changed in between names a different address and is caught rather than trusted.
    """
    from common.contracts.canonical import canonical_bytes, digest_bytes

    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        canonical = canonical_bytes(record)
    except (OSError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ApprovalRefusal(
            f"the approval record at {path} could not be read as canonical JSON: {error}"
        ) from error
    digest = digest_bytes(canonical)
    reference = ApprovalRecordReference(f"receipts/sha256/{digest}.json", digest)

    def read_supplied(relative_path: str) -> bytes:
        if relative_path != reference.relative_path:
            raise OSError("the approval reference does not name the supplied record")
        return canonical

    checked = require_current_data_gate_approval(policy, reference, read_supplied)
    return checked, reference
