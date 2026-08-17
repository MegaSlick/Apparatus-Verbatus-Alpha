"""The six identities, derived from their bindings so a forged one is detectable.

Every identity except `run_id` is a digest of exactly the facts it claims to bind,
carried beside those facts in the artifact. That makes identity *verifiable*: a
reader recomputes and refuses a mismatch, instead of trusting a string that arrived
in a file. It is also what makes the architecture's first invariant — act identity
survives recropping — a property a test can prove rather than a habit:

    act_id    binds the original proposal          -> a recrop cannot change it
    region_id binds the act AND the transform      -> a recrop must change it

Both statements fall out of what each identity hashes, so no code has to remember
to keep the act id stable; keeping it stable is the only thing the derivation can do.

`run_id` is the exception and is caller-supplied on purpose: an operator has to be
able to name a run, say it aloud, and find it again. Its integrity comes from
`run.json` binding it to the source, configuration and adapter-recipe digests, and
from incompatible reuse failing before anything is written.
"""

import re
from typing import Any, Final

from .canonical import digest_of
from .errors import IdentityRefusal

# Sixteen hex characters, 64 bits. Long enough that a collision inside one run is
# not a practical concern, short enough that a human can compare two ids by eye in
# a directory listing — which is a thing the operator surface will actually ask
# them to do. Widen it here if a real corpus ever makes that judgement wrong; every
# id in the system is derived through this module, so it is one edit.
_DIGEST_CHARS: Final = 16

_PREFIXES: Final = {
    "page": "pg",
    "act": "act",
    "region": "rgn",
    "attempt": "att",
    "artifact": "art",
}

# A run id an operator types, and that is also safe as a directory name on every
# platform this may run on. Deliberately narrow: no spaces, no leading dot, no
# path separators, no case-folding surprises between macOS and Linux.
_RUN_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_ID_PATTERN: Final = re.compile(r"^(pg|act|rgn|att|art)_[0-9a-f]{%d}$" % _DIGEST_CHARS)


def validate_run_id(run_id: Any) -> str:
    """Refuse a run id that is not a plain, typeable, filesystem-safe name."""
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.match(run_id):
        raise IdentityRefusal(
            f"run_id {run_id!r} is not a plain lowercase name of 1-64 characters "
            "from [a-z0-9._-] starting alphanumeric; it names a directory and is "
            "typed by an operator, so it is kept boring on purpose"
        )
    return run_id


def derive(kind: str, bindings: dict[str, Any]) -> str:
    """The identity of `kind` bound to exactly `bindings`.

    The kind is hashed alongside the bindings so two kinds that happened to bind
    the same facts could never collide into one string.
    """
    try:
        prefix = _PREFIXES[kind]
    except KeyError:
        raise IdentityRefusal(
            f"no identity kind {kind!r}; known kinds: {sorted(_PREFIXES)}"
        ) from None
    return f"{prefix}_{digest_of({'kind': kind, 'bindings': bindings})[:_DIGEST_CHARS]}"


def verify(identity: Any, kind: str, bindings: dict[str, Any]) -> None:
    """Refuse an identity that does not recompute from the bindings beside it."""
    if not isinstance(identity, str) or not _ID_PATTERN.match(identity):
        raise IdentityRefusal(f"{identity!r} is not a well-formed identity")
    expected = derive(kind, bindings)
    if identity != expected:
        raise IdentityRefusal(
            f"{kind} identity {identity} does not verify against its own bindings "
            f"(recomputed {expected}); the identity or the bindings were altered "
            "after it was derived"
        )


def is_well_formed(identity: Any) -> bool:
    """Shape only — says nothing about whether it verifies against bindings."""
    return isinstance(identity, str) and bool(_ID_PATTERN.match(identity))


# --- The five derived identities, each naming exactly what it binds -------------


def page_bindings(source_sha256: str, ordinal: int) -> dict[str, Any]:
    """A page is the nth image out of one sealed source."""
    return {"source_sha256": source_sha256, "ordinal": ordinal}


def page_id(source_sha256: str, ordinal: int) -> str:
    return derive("page", page_bindings(source_sha256, ordinal))


def act_bindings(page: str, proposal_ordinal: int, proposal_bounds: Any) -> dict[str, Any]:
    """An act is the nth proposal the Designator marked out on a page.

    The *original* proposal bounds, never the current ones. This is the whole
    mechanism behind "act identity survives recropping": a recrop produces new
    bounds and therefore a new region, and cannot reach these bindings at all.
    """
    return {
        "page_id": page,
        "proposal_ordinal": proposal_ordinal,
        "proposal_bounds": proposal_bounds,
    }


def act_id(page: str, proposal_ordinal: int, proposal_bounds: Any) -> str:
    return derive("act", act_bindings(page, proposal_ordinal, proposal_bounds))


def region_bindings(act: str, transform: Any) -> dict[str, Any]:
    """A region is one act seen through one exact, reproducible transform.

    The transform is recorded in full rather than summarized, so ARCHITECTURE's
    third invariant holds: the exact image shown to a model is reproducible from
    the Exemplar plus the recorded transforms.
    """
    return {"act_id": act, "transform": transform}


def region_id(act: str, transform: Any) -> str:
    return derive("region", region_bindings(act, transform))


def attempt_bindings(subject: str, operation: str, ordinal: int) -> dict[str, Any]:
    """An attempt is the nth time one operation was tried on one subject.

    Ordinals are monotonic per (subject, operation) and never reused. Attempts are
    append-only — nothing overwrites attempt 1 to record attempt 2 — which is what
    lets a failed re-read derive a current outcome of `failed` while attempt 1
    stays intact and visible as history (GOVERNANCE 4, and Tyrel's 2026-07-30
    retention ruling).
    """
    return {"subject_id": subject, "operation": operation, "ordinal": ordinal}


# Perlector's passes share an act and ordinal, so their operation names are part
# of the identity boundary rather than informal labels. The generic identity
# constructor remains stage-neutral; the Perlector calls this vocabulary before
# deriving one of its reading attempts.
PERLECTOR_READING_OPERATIONS = frozenset(
    {"perlegere", "lectio-nuda", "lectio-prior", "primed-without-prior"}
)


def perlector_attempt_id(subject: str, operation: str, ordinal: int) -> str:
    if operation not in PERLECTOR_READING_OPERATIONS:
        raise ValueError(f"unknown Perlector reading operation {operation!r}")
    return attempt_id(subject, operation, ordinal)


def attempt_id(subject: str, operation: str, ordinal: int) -> str:
    return derive("attempt", attempt_bindings(subject, operation, ordinal))


def artifact_bindings(
    stage: str, kind: str, subject: str, attempt: str | None = None
) -> dict[str, Any]:
    """An artifact is one kind of thing one stage wrote about one subject.

    `attempt` is None for artifacts a stage writes once per subject rather than
    per try — a seal, a manifest entry — and present otherwise, which is what
    keeps two attempts from colliding onto one filename.
    """
    return {"stage": stage, "kind": kind, "subject_id": subject, "attempt_id": attempt}


def artifact_id(stage: str, kind: str, subject: str, attempt: str | None = None) -> str:
    return derive("artifact", artifact_bindings(stage, kind, subject, attempt))
