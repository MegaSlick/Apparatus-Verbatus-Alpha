"""The data-handling gate: where real material may live, checked mechanically.

The gate *policy* is this directory's `README.md`; this module is only its
mechanical enforcement -- policy load and storage-root checks. Per-run
sign-off is a separate, human decision this module does not make: git
ingress and CI's history scan only keep real material out of git, and
sending it to a vendor is a separate lead decision on top of that.
Fixture status is never a flag: the door's fixture route comes from the
repository's own declared fixture root and manifest, never a caller-supplied
name or boolean. This lives in `operations/submit/`, not beside the door,
so the dependency stays one-way: `pipeline/1_exemplar/door.py` imports this
and `inventory.py`, never the reverse.
"""

import json
import os
from pathlib import Path
from typing import Any, Final, NamedTuple

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH: Final = ROOT / "config" / "data_handling_policy.json"

# Every prose clause the policy must carry, checked for type and a minimum
# length so a boolean or empty value cannot pass as a stated rule.
# `policy_version` is excluded: it is a label, not a rule, so it is checked
# separately rather than held to a prose floor.
_REQUIRED_PROSE_CLAUSES: Final = (
    "storage_roots_note",
    "logging_rule",
    "temp_file_handling",
    "retention_and_deletion",
    "routing_rule",
    "third_party_transmission",
    "cleanup_drill",
    "alpha_shortcuts_ledger",
)
_POLICY_FIELDS: Final = frozenset({*_REQUIRED_PROSE_CLAUSES, "storage_roots", "policy_version"})
# A floor under "present", not a quality judgement of what a clause says.
_MINIMUM_CLAUSE_LENGTH: Final = 8


class GateRefusal(ContractError):
    """The policy could not be loaded, or a location is outside every approved root.

    Not an `ApprovalRefusal`: that class is about a claimed approval-record
    artifact failing its own schema, which this gate never checks for.
    """


class DataHandlingPolicyBinding(NamedTuple):
    """One loaded policy and the digest of the parsed bytes it came from.

    The digest identifies the parsed bytes, not the file or its path: two
    policy files with identical content hash identically. It is of the same
    read the record was parsed from, since two separate reads could straddle
    a rewrite. Provenance only: nothing here reinstates a per-run approval
    requirement.
    """

    policy: dict[str, Any]
    config_sha256: str


def load_policy_binding(path: Path = DEFAULT_POLICY_PATH) -> DataHandlingPolicyBinding:
    """The canonical policy record and the digest of the bytes behind it."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise GateRefusal(f"data-handling policy at {path} could not be read: {error}") from error
    return DataHandlingPolicyBinding(_parse_policy(raw, path), digest_bytes(raw))


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """The canonical policy record, read fresh from disk every time.

    Never cached: the gate's point is comparing against what is currently on
    disk, and a run checks storage locations against the record loaded at its
    start, so a policy edited mid-run does not retroactively change what it
    was checked against. `path` is caller-supplied via `--policy` /
    `--data-gate-policy`. A caller sealing a run wants `load_policy_binding`
    instead, so the record and its digest come from one read.
    """
    return load_policy_binding(path).policy


def _parse_policy(raw: bytes, path: Path | str) -> dict[str, Any]:
    """Validate one already-read policy document. The only parser for this file."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise GateRefusal(f"data-handling policy at {path} could not be read: {error}") from error
    if not isinstance(record, dict):
        raise GateRefusal(f"{path} is not a data-handling policy record")
    if set(record) != _POLICY_FIELDS:
        missing = sorted(_POLICY_FIELDS - set(record))
        unknown = sorted(set(record) - _POLICY_FIELDS)
        raise GateRefusal(
            f"{path} does not carry exactly the clauses this gate enforces. "
            f"Missing: {missing}. Unknown: {unknown}. A policy with a clause absent is "
            "not a shorter policy, it is one that was never approved; and a clause "
            "nothing here checks is one the project lead approved and nothing enforces"
        )
    for field in _REQUIRED_PROSE_CLAUSES:
        value = record[field]
        if not isinstance(value, str) or len(value.strip()) < _MINIMUM_CLAUSE_LENGTH:
            raise GateRefusal(
                f"{path} gives clause {field!r} a value that is not a stated rule "
                f"({type(value).__name__}); a truthiness check accepted `true`, `1` and "
                "`{...}` here, which is a policy that says nothing passing as one that does"
            )
    version = record["policy_version"]
    if not isinstance(version, str) or not version.strip():
        raise GateRefusal(f"{path} gives no policy version")
    roots = record["storage_roots"]
    if not isinstance(roots, list) or not roots:
        raise GateRefusal(f"{path} names no approved storage roots")
    if any(not isinstance(root, str) or not root.strip() for root in roots):
        raise GateRefusal(f"{path} names a storage root that is not a non-empty path")
    return record


class ResolvedStorageRoots(NamedTuple):
    """The roots that resolved, and every listed root that did not.

    `skipped` is returned, not just logged, because a narrowed approved-root
    set on this machine is a fact the caller should write into its own record,
    not something visible only inside a refusal that never happened.
    """

    roots: tuple[Path, ...]
    skipped: tuple[str, ...]


def resolve_storage_roots(policy: dict[str, Any]) -> ResolvedStorageRoots:
    """The exact locations the approved policy allows real material to live in,
    beside every listed root that did not resolve here.

    A relative entry resolves against the repository root, not the process's
    working directory. Each root is resolved independently, not all-or-nothing:
    the shipped policy names both a local root (present on every checkout) and
    a pod network-volume root (present only while mounted), so a root missing
    on this machine is skipped rather than failing the whole policy. Every
    skipped root is still returned -- on the success path, not only in the
    `GateRefusal` raised when none resolve -- so a caller can record which
    roots this machine lacked.
    """
    raw_roots = policy.get("storage_roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise GateRefusal("the data-handling policy names no approved storage roots")
    roots: list[Path] = []
    skipped: list[str] = []
    for raw_root in raw_roots:
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise GateRefusal("a data-handling storage root is not a non-empty path")
        candidate = Path(raw_root)
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            skipped.append(f"{raw_root!r} (does not exist: {error})")
            continue
        if not resolved.is_dir():
            skipped.append(f"{raw_root!r} (not a directory)")
            continue
        roots.append(resolved)
    if not roots:
        raise GateRefusal(
            "none of the data-handling policy's approved storage roots resolve on this "
            f"machine; an unresolvable root is a failed check, never an unrestricted one. "
            f"Skipped: {skipped}"
        )
    return ResolvedStorageRoots(tuple(roots), tuple(skipped))


def approved_storage_roots(policy: dict[str, Any]) -> tuple[Path, ...]:
    """Just the resolved roots, for a caller with nowhere to record the rest.

    Every caller that keeps a durable record of what it enforced should use
    :func:`resolve_storage_roots` and write the skipped list down beside the
    approved one.
    """

    return resolve_storage_roots(policy).roots


def require_approved_storage_location(
    location: Path, approved_roots: tuple[Path, ...], label: str
) -> Path:
    """Refuse a real input or output location outside the approved storage roots."""
    location = Path(os.path.abspath(location))
    _refuse_redirect_below_root(location, approved_roots, label)
    try:
        resolved = location.resolve(strict=False)
    except OSError as error:
        raise GateRefusal(
            f"the {label} could not be resolved against the approved storage roots"
        ) from error
    if not any(same_or_inside(root, resolved) for root in approved_roots):
        raise GateRefusal(
            f"the {label} is outside every approved storage root "
            f"{[str(root) for root in approved_roots]}; the policy decides where real "
            "material may live, and an unlisted location is refused rather than allowed"
        )
    return resolved


def _refuse_redirect_below_root(
    location: Path, approved_roots: tuple[Path, ...], label: str
) -> None:
    """Reject every symlink below an approved root, not only the final name.

    The approved root has already been resolved from the reviewed policy, so
    an alias above it is not an operator-controlled redirect -- only a
    component beneath that inode is. Walking the unresolved spelling is
    essential: `resolve()`'s result would erase the link being checked.
    """

    root_identities = {_identity(root) for root in approved_roots}
    if None in root_identities:
        raise GateRefusal("an approved storage root could not be identified")
    candidates = (location, *location.parents)
    # Skip the walk when no approved root is on this path: it would otherwise
    # run to "/" and report the first platform alias found (e.g. macOS's
    # `/tmp`) as a planted redirect, when the real problem -- the caller's to
    # report -- is that the location is not approved at all.
    location_has_approved_root = any(
        _identity(candidate) in root_identities for candidate in candidates
    )
    if not location_has_approved_root:
        return
    for position, candidate in enumerate(candidates):
        if position > 0 and _identity(candidate) in root_identities:
            return
        if candidate.is_symlink():
            relation = "is a symlink" if position == 0 else "crosses a symlink"
            raise GateRefusal(
                f"the {label} {relation}; an approved storage root cannot be entered by redirect"
            )
        if position == 0 and _identity(candidate) in root_identities:
            return


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def same_or_inside(ancestor: Path, descendant: Path) -> bool:
    """Whether one path is the other, or holds it, by filesystem identity.

    Not `is_relative_to`, which compares spellings: default-case-insensitive
    APFS treats `/approved/masters` and `/approved/Masters` as one directory
    that a textual check would call different, and `Path.resolve` does not
    correct case on macOS. Device and inode settle it, including for a bind
    mount. A not-yet-existing descendant is judged by its parents instead.
    """
    target = _identity(ancestor)
    if target is None:
        return False
    return any(_identity(candidate) == target for candidate in (descendant, *descendant.parents))
