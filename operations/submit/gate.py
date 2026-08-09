"""The data-handling gate: where real material may live, checked mechanically.

The gate *package* — the written policy Tyrel approves — is this directory's
`README.md`, so its wording and the policy it explains travel with the
implementation.

**Cut 2026-08-09, per Tyrel's ruling that session.** This module used to also make
a per-run approval checkable: a policy hash, an approval record bound to the exact
policy version, and a door that refused real input without a current one. His
ruling: none of this material ever reaches git regardless of any such sign-off —
it runs through the pipeline on a GPU host, `workbench/` is gitignored, and an
ingress check plus a pre-push payload scan already cover that mechanically — so the
approval-record requirement bought nothing and is gone. What remains is the part
that is still a real, mechanical safety net: the policy load and its storage-root
enforcement, which keep real material inside the locations the policy names,
independent of any per-run sign-off. Third-party transmission — sending real
material to a vendor — remains its own decision Tyrel makes in session; this cut
does not touch it and does not invent a replacement for it.

**Ruling 2026-08-04, item 1 — fixture status is never a flag.** The door's fixture
route is selected by the repository's own declared fixture root and loaded
manifest, and the self-hashed run ingress records which route created it. Nothing
here accepts a filename, folder name, command-line switch, or boolean that can
relabel real material as a fixture.

**Why this lives in `operations/submit/` and not beside the door.** The gate is
about material *arriving*, which is the submit door's whole subject, and putting it
here keeps the dependency between the two trees pointing one way:
`pipeline/1_exemplar/door.py` imports this and `inventory.py`, and nothing in
`operations/submit/` imports the pipeline.
"""

import json
from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError

ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH: Final = ROOT / "config" / "data_handling_policy.json"

# Every clause the policy carries, and its shape. Spec 03 names each of these as
# something the gate package must say; `alpha_shortcuts_ledger` was named there and
# missing from this list, so a policy stripped of it loaded clean while a separate
# test asserted the shipped file had one — the check and the claim in different
# places, agreeing with nobody.
#
# **The set is exact and the types are checked.** `if not record.get(field)` was
# pure truthiness, so `logging_rule` could be `True`, `1`, `{"x": 1}` or the string
# `"x"` and load; every prose clause but `storage_roots` could be replaced by a
# boolean and the policy still hashed, loaded and gated. A clause that says nothing
# is not a shorter policy either.
# `policy_version` is deliberately not here: it is a label, not a rule, and holding
# it to the prose floor would refuse an ordinary short version string with a message
# about truthiness checks that has nothing to do with it.
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
# Short enough to be a placeholder rather than a clause. Not a quality judgement —
# nothing here reads what a clause says — only a floor under "present".
_MINIMUM_CLAUSE_LENGTH: Final = 8


class GateRefusal(ContractError):
    """The policy could not be loaded, or a location is outside every approved root.

    Not an `ApprovalRefusal`: that class is about a claimed approval-record artifact
    failing its own schema. Since 2026-08-09 nothing here checks for one — this is
    the storage-location gate refusing on its own, mechanical terms.
    """


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """The canonical policy record, read fresh from disk every time.

    Never cached *by this function*: a cached policy could disagree with a
    concurrent edit to the file on disk, and the gate's whole point is comparing
    against what is currently there. Every clause the spec requires the package to
    carry must be present — a policy missing its retention rule is not a shorter
    policy, it is one Tyrel did not approve.

    **The snapshot point is the start of a command, and that is a ruling rather than
    an accident.** `door.real_submission` loads the policy once, then may spend a
    long time inventorying a folder before storage locations are checked against
    that same in-memory record. So a policy edited *during* a run does not
    retroactively change what that run was checked against.

    **`path` is caller-supplied, which the docstrings here used to omit.** Both
    entry points expose `--policy` / `--data-gate-policy`, so "the current policy" is
    whatever file the invoker names. It is disclosed here because a documented limit
    is not the same thing as a silent one.
    """
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
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
            "nothing here checks is one Tyrel approved and nothing enforces"
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
