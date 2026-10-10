"""The derived identities, bound so a forged one is detectable.

Every identity except `run_id` is a digest of exactly the facts it claims to bind,
carried beside those facts in the artifact. That makes identity *verifiable*: a
reader recomputes and refuses a mismatch, instead of trusting a string that arrived
in a file. It is also what makes "act identity survives recropping" a property a
test can prove rather than a habit:

    act_id    binds the original class and bounds  -> a recrop cannot change it
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

from .canonical import digest_of, is_sha256
from .errors import IdentityRefusal

# 64 bits: no practical collision in a run, yet comparable by eye in a listing.
_DIGEST_CHARS: Final = 16

_PREFIXES: Final = {
    "page": "pg",
    "act": "act",
    "physical-page": "ppg",
    "physical-act": "pac",
    "region": "rgn",
    "attempt": "att",
    "artifact": "art",
    "lot": "lot",
}

# Typed by an operator and safe as a directory name on macOS and Linux alike.
_RUN_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Every prefix `derive` can mint, so `verify` never refuses an identity it could derive.
_ID_PATTERN: Final = re.compile(
    r"^(%s)_[0-9a-f]{%d}$" % ("|".join(_PREFIXES.values()), _DIGEST_CHARS)
)


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


_LOT_PATTERN: Final = re.compile(r"^lot_[0-9a-f]{%d}$" % _DIGEST_CHARS)


def lot_id(run_self_hash: Any) -> str:
    """The run's lot: a short id every exported row carries, bound to run.json's self-hash."""
    if not is_sha256(run_self_hash):
        raise IdentityRefusal("a lot is derived from run.json's sha256 self-hash")
    return derive("lot", {"run_self_hash": run_self_hash})


def is_lot(value: Any) -> bool:
    """Shape only: whether `value` reads as a lot."""
    return isinstance(value, str) and bool(_LOT_PATTERN.match(value))


def is_well_formed(identity: Any) -> bool:
    """Shape only — says nothing about whether it verifies against bindings."""
    return isinstance(identity, str) and bool(_ID_PATTERN.match(identity))


# --- The derived identities, each naming exactly what it binds -----------------


def _closed(value: Any, fields: set[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise IdentityRefusal(f"{what} must be the closed record {sorted(fields)}")
    return value


def _sha256(value: Any, what: str) -> None:
    if not is_sha256(value):
        raise IdentityRefusal(f"{what} must be a lowercase SHA-256 digest")


def _bounds(value: Any, what: str) -> None:
    row = _closed(value, {"x", "y", "w", "h"}, what)
    if (
        any(not isinstance(item, int) or isinstance(item, bool) for item in row.values())
        or row["x"] < 0
        or row["y"] < 0
        or row["w"] <= 0
        or row["h"] <= 0
    ):
        raise IdentityRefusal(f"{what} must have non-negative integer x/y and positive integer w/h")


def _identity(value: Any, prefix: str, what: str) -> None:
    if not is_well_formed(value) or not value.startswith(f"{prefix}_"):
        raise IdentityRefusal(f"{what} must be a well-formed {prefix}_ identity")


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
        _sha256(origin["sha256"], "source page origin sha256")
    elif origin["kind"] == "container-page":
        if (
            not isinstance(origin["container_page_index"], int)
            or isinstance(origin["container_page_index"], bool)
            or origin["container_page_index"] < 0
            or not isinstance(origin["render_contract"], dict)
        ):
            raise IdentityRefusal("container-page origin has malformed immutable facts")
        _sha256(origin["container_sha256"], "container page origin sha256")
    else:
        raise IdentityRefusal("page origin kind must be 'source' or 'container-page'")
    transform = (
        _closed(transform, {"operation"}, "page transform")
        if isinstance(transform, dict) and transform.get("operation") == "whole"
        else _closed(transform, {"operation", "bounds"}, "page transform")
    )
    if transform["operation"] == "split":
        _bounds(transform["bounds"], "split bounds")
    elif transform["operation"] != "whole":
        raise IdentityRefusal("page transform operation must be 'whole' or 'split'")
    return {"origin": origin, "transform": transform}


def page_id(origin: Any, transform: Any) -> str:
    return derive("page", page_bindings(origin, transform))


# Rectangle-bound classes. No stage mints them; corpus-register and gold
# records that carry them stay readable.
ACT_CLASSES: Final = frozenset({"proposal", "residual", "page-fallback", "page-residual"})
# The classes the Perlector's whole-page reading mints (`common/page_path.py`,
# `entry_plans`). An entry of a page reading is bound to that reading's attempt,
# its number `n` in the answer and the union box of the ids it cites, so two
# entries with one union box still get two identities. `reading-unplaced` is an
# entry that cites no boxed id: its box is `None`.
READING_ACT_CLASSES: Final = frozenset({"reading", "reading-unplaced"})
# The two classes the page-read denominator (`common.stage.reading_acts`) mints
# for a page with no entry, each over the page rectangle: `page-unread` for a page whose reading is not a parsed, valid answer, and
# `page-blank` for a parsed, valid answer that names no entry. Each stands for the
# page so it is never counted as zero acts; both are held.
PAGE_READING_ROW_CLASSES: Final = frozenset({"page-unread", "page-blank"})
_READING_BINDING_FIELDS: Final = frozenset({"page_reading", "n", "union_box_px"})


def act_bindings(page: str, act_class: str, bounds: Any) -> dict[str, Any]:
    """An act binds its image-local page, class and originally minted bounds.

    The *originally minted* bounds, never the current ones. This is the whole
    mechanism behind "act identity survives recropping": a recrop produces new
    bounds and therefore a new region, and cannot reach these bindings at all.

    The class is a closed enum rather than a position in a list of proposals:
    one extra detected region on a page must not rename every act after it.
    That leaves the rectangle as the only thing separating two acts of one
    class on one page, so a minter must never produce the same rectangle twice.

    The two page-reading classes (`READING_ACT_CLASSES`) bind a binding rather
    than a rectangle: the page reading's attempt, the entry's number `n` and its
    union box (`None` for `reading-unplaced`), so two entries sharing a union box
    still get two identities.

    Classes minted over the *page rectangle* rather than over a detected region
    are each separate because each says something different about the same
    rectangle. A ``page-unread`` row stands for a page whose reading is not a
    parsed, valid answer, and a ``page-blank`` row for a read answer that names
    no entry (`PAGE_READING_ROW_CLASSES`); both are held. One rectangle, one
    page, dispositions that cannot be folded together.

    Validation lives here rather than in `act_id` so `verify()` — which is
    handed bindings rebuilt from a payload a stage read back — refuses a shape
    the minting path could never have produced, instead of hashing it and
    reporting a mismatch that says nothing about why.
    """
    if type(act_class) is not str:
        raise IdentityRefusal(f"act class {act_class!r} is not a string")
    if act_class in READING_ACT_CLASSES:
        _identity(page, "pg", "act page")
        _reading_binding(bounds, act_class)
        return {"page_id": page, "class": act_class, "bounds": bounds}
    if act_class not in ACT_CLASSES | PAGE_READING_ROW_CLASSES:
        allowed = ", ".join(
            repr(name)
            for name in sorted(ACT_CLASSES | READING_ACT_CLASSES | PAGE_READING_ROW_CLASSES)
        )
        raise IdentityRefusal(f"act class must be one of {allowed}")
    _identity(page, "pg", "act page")
    _bounds(bounds, "act bounds")
    return {
        "page_id": page,
        "class": act_class,
        "bounds": bounds,
    }


def _reading_binding(value: Any, act_class: str) -> None:
    """A page-reading entry's binding: `{page_reading, n, union_box_px}`.

    `page_reading` is the page reading's attempt identity, `n` the entry's
    positive number in the answer, and `union_box_px` a rectangle for class
    `reading` and `None` for `reading-unplaced`.
    """
    row = _closed(value, set(_READING_BINDING_FIELDS), "a page-reading act binding")
    _identity(row["page_reading"], "att", "the page reading attempt")
    if not isinstance(row["n"], int) or isinstance(row["n"], bool) or row["n"] < 1:
        raise IdentityRefusal("a page-reading act's n must be a positive integer")
    if act_class == "reading":
        _bounds(row["union_box_px"], "a page-reading act's union box")
    elif row["union_box_px"] is not None:
        raise IdentityRefusal("an unplaced page-reading act binds no box")


def act_id(page: str, act_class: str, bounds: Any) -> str:
    return derive("act", act_bindings(page, act_class, bounds))


_WHITESPACE_RUN: Final = re.compile(r"\s+")


def _declared_text(value: str, what: str) -> str:
    """The one spelling a human-typed declaration is hashed under.

    Every other binding in this system is a digest, an integer, or a closed
    enum. The physical identities are the only ones bound to text a person
    types, and NFC and NFD spellings of one accented folio label are different
    bytes to `canonical_bytes` — so one physical page would be declared twice,
    with nothing anywhere to reconcile the two. Normalising first turns that
    silent split into the corpus register's loud duplicate-record refusal.

    Whitespace is the same accident on a second axis, and it was left open.
    `"12r "`, `"12r"`, and `"folio  12r"` are different bytes for what the typist
    meant as one folio, and a trailing space is not visible in the form they
    typed it into. It splits a physical page exactly as an NFD spelling does, so
    it is folded exactly as far: NFC first, then every whitespace run — including
    a no-break space, which NFC leaves alone — collapsed to one space, then the
    ends trimmed. A label whose meaning turns on double-spacing is not a thing
    parish foliation has; a label silently split in two is.

    A declaration that is nothing but whitespace is refused here rather than
    folded to `""` and hashed, because the callers' non-empty check runs before
    this and would otherwise pass a blank designation into a real identity.
    """
    folded = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if not folded:
        raise IdentityRefusal(f"{what} is only whitespace, so it declares nothing")
    return folded


def physical_page_bindings(corpus_id: str, volume_id: str, designation: str) -> dict[str, str]:
    if not all(isinstance(value, str) and value for value in (corpus_id, volume_id, designation)):
        raise IdentityRefusal(
            "physical page bindings require non-empty corpus, volume, and designation"
        )
    return {
        "corpus_id": _declared_text(corpus_id, "physical page corpus_id"),
        "volume_id": _declared_text(volume_id, "physical page volume_id"),
        "designation": _declared_text(designation, "physical page designation"),
    }


def physical_page_id(corpus_id: str, volume_id: str, designation: str) -> str:
    return derive("physical-page", physical_page_bindings(corpus_id, volume_id, designation))


def physical_act_bindings(physical_page: str, mint_designation: str) -> dict[str, str]:
    if not isinstance(mint_designation, str) or not mint_designation:
        raise IdentityRefusal(
            "physical act bindings require a physical page id and mint designation"
        )
    _identity(physical_page, "ppg", "physical act page")
    return {
        "physical_page_id": physical_page,
        "mint_designation": _declared_text(mint_designation, "physical act mint_designation"),
    }


def physical_act_id(physical_page: str, mint_designation: str) -> str:
    return derive("physical-act", physical_act_bindings(physical_page, mint_designation))


def region_bindings(act: str, transform: Any) -> dict[str, Any]:
    """A region is one act seen through one exact, reproducible transform.

    The transform is recorded in full rather than summarized, so the exact image
    shown to a model is reproducible from the Exemplar plus the recorded
    transforms.
    """
    return {"act_id": act, "transform": transform}


def region_id(act: str, transform: Any) -> str:
    return derive("region", region_bindings(act, transform))


def attempt_bindings(subject: str, operation: str, ordinal: int) -> dict[str, Any]:
    """An attempt is the nth time one operation was tried on one subject.

    Ordinals are monotonic per (subject, operation) and never reused. Attempts are
    append-only — nothing overwrites attempt 1 to record attempt 2 — which is what
    lets a failed re-read derive a current outcome of `failed` while attempt 1
    stays intact and visible as history.
    """
    return {"subject_id": subject, "operation": operation, "ordinal": ordinal}


# The operation a Perlector reading's attempt is named under.
PERLECTOR_READING_OPERATIONS = frozenset({"perlegere"})


def perlector_attempt_id(subject: str, operation: str, ordinal: int) -> str:
    if type(operation) is not str or operation not in PERLECTOR_READING_OPERATIONS:
        raise IdentityRefusal(f"unknown Perlector reading operation {operation!r}")
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
