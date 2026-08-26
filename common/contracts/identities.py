"""The eight derived identities, bound so a forged one is detectable.

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
import unicodedata
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
    "physical-page": "ppg",
    "physical-act": "pac",
    "region": "rgn",
    "variance-experiment": "ve",
    "attempt": "att",
    "artifact": "art",
}

# A run id an operator types, and that is also safe as a directory name on every
# platform this may run on. Deliberately narrow: no spaces, no leading dot, no
# path separators, no case-folding surprises between macOS and Linux.
_RUN_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_ID_PATTERN: Final = re.compile(r"^(pg|act|ppg|pac|rgn|ve|att|art)_[0-9a-f]{%d}$" % _DIGEST_CHARS)


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


# --- The derived identities, each naming exactly what it binds -----------------


def _closed(value: Any, fields: set[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise IdentityRefusal(f"{what} must be the closed record {sorted(fields)}")
    return value


def page_bindings(origin: Any, transform: Any) -> dict[str, Any]:
    """A rendered page binds immutable origin bytes and its parent-space transform.

    Submission ordinals describe manifest rows, not the image.  They are therefore
    deliberately absent: inserting a row cannot rename an existing page.
    """
    origin = (
        _closed(origin, {"kind", "sha256"}, "page origin")
        if isinstance(origin, dict) and origin.get("kind") == "source"
        else _closed(
            origin,
            {"kind", "container_sha256", "container_page_index", "render_contract"},
            "page origin",
        )
    )
    if origin["kind"] == "source":
        if not isinstance(origin["sha256"], str):
            raise IdentityRefusal("source page origin has no sha256")
    elif origin["kind"] == "container-page":
        if (
            not isinstance(origin["container_sha256"], str)
            or not isinstance(origin["container_page_index"], int)
            or isinstance(origin["container_page_index"], bool)
            or origin["render_contract"] is None
        ):
            raise IdentityRefusal("container-page origin has malformed immutable facts")
    else:
        raise IdentityRefusal("page origin kind must be 'source' or 'container-page'")
    transform = (
        _closed(transform, {"operation"}, "page transform")
        if isinstance(transform, dict) and transform.get("operation") == "whole"
        else _closed(transform, {"operation", "bounds"}, "page transform")
    )
    if transform["operation"] == "whole":
        pass
    elif transform["operation"] == "split":
        _closed(transform["bounds"], {"x", "y", "w", "h"}, "split bounds")
    else:
        raise IdentityRefusal("page transform operation must be 'whole' or 'split'")
    return {"origin": origin, "transform": transform}


def page_id(origin: Any, transform: Any) -> str:
    return derive("page", page_bindings(origin, transform))


ACT_CLASSES: Final = frozenset({"proposal", "residual", "page-fallback"})


def act_bindings(page: str, act_class: str, bounds: Any) -> dict[str, Any]:
    """An act binds its image-local page, class and original bounds.

    The *original* proposal bounds, never the current ones. This is the whole
    mechanism behind "act identity survives recropping": a recrop produces new
    bounds and therefore a new region, and cannot reach these bindings at all.

    The class is a closed enum rather than a position in a list of proposals:
    one extra detected region on a page must not rename every act after it.
    That leaves the rectangle as the only thing separating two acts of one
    class on one page, so each minter proves it cannot produce the same
    rectangle twice — the Designator at `_refuse_duplicate_proposal_bounds`
    and `hold_residual_act`.

    Validation lives here rather than in `act_id` so `verify()` — which is
    handed bindings rebuilt from a payload a stage read back — refuses a shape
    the minting path could never have produced, instead of hashing it and
    reporting a mismatch that says nothing about why.
    """
    if act_class not in ACT_CLASSES:
        raise IdentityRefusal("act class must be 'proposal', 'residual', or 'page-fallback'")
    _closed(bounds, {"x", "y", "w", "h"}, "act bounds")
    return {
        "page_id": page,
        "class": act_class,
        "bounds": bounds,
    }


def act_id(page: str, act_class: str, bounds: Any) -> str:
    return derive("act", act_bindings(page, act_class, bounds))


def _declared_text(value: Any) -> str:
    """The one spelling a human-typed declaration is hashed under.

    Every other binding in this system is a digest, an integer, or a closed
    enum. The physical identities are the only ones bound to text a person
    types, and NFC and NFD spellings of one accented folio label are different
    bytes to `canonical_bytes` — so one physical page would be declared twice,
    with nothing anywhere to reconcile the two. Normalising first turns that
    silent split into the corpus register's loud duplicate-record refusal.
    """
    if not isinstance(value, str) or not value:
        return value
    return unicodedata.normalize("NFC", value)


def physical_page_bindings(corpus_id: str, volume_id: str, designation: str) -> dict[str, str]:
    if not all(isinstance(value, str) and value for value in (corpus_id, volume_id, designation)):
        raise IdentityRefusal(
            "physical page bindings require non-empty corpus, volume, and designation"
        )
    return {
        "corpus_id": _declared_text(corpus_id),
        "volume_id": _declared_text(volume_id),
        "designation": _declared_text(designation),
    }


def physical_page_id(corpus_id: str, volume_id: str, designation: str) -> str:
    return derive("physical-page", physical_page_bindings(corpus_id, volume_id, designation))


def physical_act_bindings(physical_page: str, mint_designation: str) -> dict[str, str]:
    if (
        not isinstance(physical_page, str)
        or not physical_page.startswith("ppg_")
        or not isinstance(mint_designation, str)
        or not mint_designation
    ):
        raise IdentityRefusal(
            "physical act bindings require a physical page id and mint designation"
        )
    return {
        "physical_page_id": physical_page,
        "mint_designation": _declared_text(mint_designation),
    }


def physical_act_id(physical_page: str, mint_designation: str) -> str:
    return derive("physical-act", physical_act_bindings(physical_page, mint_designation))


def physical_act_component_designation(physical_page: str, local_act_ids: list[str]) -> str:
    """The order-independent designation for an initially discovered physical act.

    A correspondence component is a set.  In particular its first encountered
    local proposal is not evidence that it is more authoritative than another
    member, so callers may not use a capture order or a representative act as
    the mint input.
    """
    if not isinstance(physical_page, str) or not physical_page.startswith("ppg_"):
        raise IdentityRefusal("physical-act component designation requires a physical page id")
    if (
        not isinstance(local_act_ids, list)
        or not local_act_ids
        or local_act_ids != sorted(set(local_act_ids))
        or not all(isinstance(item, str) and item.startswith("act_") for item in local_act_ids)
    ):
        raise IdentityRefusal("physical-act component designation requires sorted unique act ids")
    return digest_of({"physical_page_id": physical_page, "initial_local_act_ids": local_act_ids})


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
