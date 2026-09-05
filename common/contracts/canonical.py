"""One serialization, so a digest means the same thing on every machine.

Two artifacts with the same content must produce the same bytes and therefore the
same digest, or "reruns reuse valid artifacts byte-for-byte" is unenforceable and
resume becomes a guess. Everything that is hashed or written in this pipeline goes
through `canonical_bytes`.

The choices, each because the alternative varies across machines or runs:

  sort_keys        dict order is insertion order in Python, so two equal payloads
                   built by different code paths would otherwise differ in bytes.
  no whitespace    the default separators pad with spaces; the padding carries no
                   information and would make the digest depend on json's defaults.
  ensure_ascii off a name in a parish register is not ASCII. Escaping it would
                   still be deterministic, but the stored bytes should be the text
                   itself — this is a project about the very words.
  allow_nan off    NaN and Infinity are not JSON. Emitting them produces a file
                   that this package would refuse to read back.
  UTF-8            named explicitly rather than left to the platform.

Floats are refused outright. They round-trip through JSON at the mercy of repr and
this pipeline has no need of them: ordinals, counts, and pixel bounds are integers.
A float that reached an artifact would be a silent determinism defect, so it is a
loud one instead.
"""

import hashlib
import json
from typing import Any

# Everything this system writes in its first form carries this label. It is
# deliberately disposable: spec 01 calls the contracts intentionally throwaway
# before alpha, and DATA_CONTRACT.md is reserved until 01-03 have stabilized.
#
# v1 adds the attempt binding and a self-hash to every envelope.  An earlier
# envelope carried an artifact id derived from that binding without carrying the
# binding itself, so a consumer could only check the id's shape rather than
# recomputing it.  It also left ordinary stage payloads mutable without any
# integrity evidence. Reusing a v0 run under the repaired rule would be an
# incompatible interpretation of its evidence, so the label changes rather than
# pretending the two shapes agree.
SCHEMA_LABEL = "skeleton.v1"

# CPython's integer-to-decimal limit may be configured down to 640 digits;
# canonical acceptance must not vary with that host setting.
_MAX_INTEGER_MAGNITUDE = 10**640


def _segment(key: str) -> str:
    """Keep ordinary Unicode readable while making refusal paths safe for stderr."""
    return key if key.isprintable() else ascii(key)


# One walk position, as a crumb and its parent, so a walk carries where it is
# without paying for the rendered string at every level. `None` is the root's
# parent. Rendered by `_at`, and only when a refusal has to name a place.
_Trail = tuple[str, Any] | None

# A refusal an operator cannot read has not named anything. A million-deep
# payload's position is its top, its bottom, and how far apart they are; the
# whole path would be several million characters of prose on stderr. No real
# record comes near this, so ordinary refusals are rendered whole.
_MAX_RENDERED_CRUMBS = 32


def _at(trail: _Trail) -> str:
    """Render a walk position. Called on the refusal path, never on the clean one."""
    crumbs: list[str] = []
    while trail is not None:
        crumb, trail = trail
        crumbs.append(crumb)
    crumbs.reverse()
    if len(crumbs) > 2 * _MAX_RENDERED_CRUMBS:
        head = "".join(crumbs[:_MAX_RENDERED_CRUMBS])
        tail = "".join(crumbs[-_MAX_RENDERED_CRUMBS:])
        return f"{head}...({len(crumbs) - 2 * _MAX_RENDERED_CRUMBS} more levels)...{tail}"
    return "".join(crumbs)


# How deep a canonical artifact may nest. This is a contract about what this
# pipeline seals, not a tuning knob: the deepest record it builds is a handful
# of levels, and a payload that nests past this is a defect in whatever produced
# it rather than evidence anything can hash against.
#
# It exists because the alternative is an interpreter detail. Before this bound,
# the depth at which `canonical_bytes` refused was wherever one of two walks ran
# out of stack: `_refuse_floats`'s Python frames at roughly the recursion limit,
# or, once that walk stopped recursing, `json.dumps`'s C encoder — which on this
# machine absorbs about 9,997 levels and on another absorbs a different number,
# because CPython's C recursion limit is platform-dependent (the same fact
# `common/test_corpus_register.py` records about the JSON *parser*). A hasher
# whose acceptance depends on which machine ran it cannot say two artifacts with
# the same content produce the same bytes, which is the one thing this module is
# for.
_MAX_CANONICAL_DEPTH = 256


def _enter_container(container: Any, open_path: set[int], trail: _Trail) -> None:
    """Open one container: refuse a cycle, refuse excessive depth, mark it open.

    `open_path` holds exactly the containers between the root and here, so its
    size is the current depth and no separate counter can drift from it. Every
    open container is still referenced by the walk, so its identity cannot be
    reused underneath this set while it is being watched for.
    """
    marker = id(container)
    if marker in open_path:
        raise TypeError(
            f"structure is recursive or nests too deeply for canonical JSON: {_at(trail)} "
            "contains itself; no canonical artifact can carry it or be hashed against it"
        )
    open_path.add(marker)
    if len(open_path) > _MAX_CANONICAL_DEPTH:
        raise TypeError(
            "structure is recursive or nests too deeply for canonical JSON: "
            f"{_at(trail)} is past the {_MAX_CANONICAL_DEPTH}-level limit; no canonical "
            "artifact can carry it or be hashed against it"
        )


def _refuse_floats(value: Any, path: str = "$") -> None:
    """Walk the structure and refuse numbers outside the canonical vocabulary.

    Iterative for the reason the preference screens are (see
    `common/corpus_register.py::refuse_capture_preference`): the value handed to
    this walk is whatever a caller asks the pipeline to seal, and much of it
    began as model or witness JSON that passed through several stages before
    reaching the one hasher every artifact goes through. A recursive walk over a
    deeply nested payload exhausted the interpreter stack, and while
    `canonical_bytes` does convert that `RecursionError` into a named refusal,
    the refusal then depended on which of two walks ran out of stack first
    rather than on anything this module decides. Depth is this walk's own list
    now, and its own declared bound; `json.dumps` recurses in C below it and
    keeps the existing guard, but never sees a structure deep enough to need it.

    The path is assembled only when something is refused. Concatenating it at
    every level would cost a deeply nested payload the square of its depth in
    string bytes, which is the same denial the rewrite exists to remove.

    Cycles and depth are named here rather than left to run out of stack. The
    recursive walk refused both by exhausting itself, which a walk with no stack
    to exhaust would have turned into a hang for the first and a
    platform-dependent threshold for the second -- so the containers on the
    current path are tracked, and one reached from inside itself, or past
    `_MAX_CANONICAL_DEPTH`, is refused in the same terms `canonical_bytes` uses
    for a structure it cannot serialize. Only the *current path* is tracked, so
    a value that legitimately appears twice in a record is walked twice rather
    than mistaken for a loop.
    """
    # (kind, payload, trail). A "key" task checks one dict key's type at the
    # point the recursive form checked it -- after the preceding sibling's whole
    # subtree, before its own value's -- so a payload carrying both a bad key and
    # a bad number is still named by whichever the old walk reached first. An
    # "exit" task is stacked under a container's children and clears it from the
    # open path once they are all walked.
    pending: list[tuple[str, Any, _Trail]] = [("value", value, (path, None))]
    open_path: set[int] = set()
    while pending:
        kind, current, trail = pending.pop()
        if kind == "exit":
            open_path.discard(current)
            continue
        if kind == "key":
            # A hostile or huge key may fail while being rendered; its type
            # and location identify the schema defect without formatting it.
            raise TypeError(f"non-string key of type {type(current).__name__} at {_at(trail)}")
        if isinstance(current, bool):
            continue
        if isinstance(current, float):
            raise TypeError(
                f"float at {_at(trail)}: canonical artifacts carry integers, not floats — "
                "a float's JSON form is not stable enough to hash against"
            )
        if isinstance(current, int):
            if not -_MAX_INTEGER_MAGNITUDE < current < _MAX_INTEGER_MAGNITUDE:
                raise TypeError(
                    f"integer at {_at(trail)} exceeds 640 decimal digits: canonical artifacts "
                    "refuse magnitudes whose JSON conversion depends on the host's "
                    "integer-string safety limit"
                )
            continue
        if isinstance(current, dict):
            _enter_container(current, open_path, trail)
            tasks: list[tuple[str, Any, _Trail]] = []
            for key, item in current.items():
                if not isinstance(key, str):
                    # Queued rather than raised here: the value tasks already
                    # stacked for earlier siblings must run first, exactly as
                    # the recursive walk ran them before reaching this key.
                    tasks.append(("key", key, trail))
                    continue
                tasks.append(("value", item, (f".{_segment(key)}", trail)))
            pending.append(("exit", id(current), None))
            pending.extend(reversed(tasks))
        elif isinstance(current, (list, tuple)):
            _enter_container(current, open_path, trail)
            pending.append(("exit", id(current), None))
            for index in range(len(current) - 1, -1, -1):
                pending.append(("value", current[index], (f"[{index}]", trail)))


def _unencodable_path(value: Any, path: str = "$") -> str | None:
    """Locate the first UTF-8 failure in canonical order for a refusal message.

    Iterative for `_refuse_floats`'s reason and for one more: this runs inside
    `canonical_bytes`'s `UnicodeEncodeError` handler, which is outside the guard
    that turns exhausted traversal into a named refusal. A `RecursionError`
    raised in here escaped as an implementation traceback from the function
    every sealed artifact is hashed through.

    Positions are carried as trails and rendered once, for `_refuse_floats`'s
    reason: this locator's whole output is one path, so building the other
    million on the way to it is pure cost.

    No cycle check, deliberately. This is reached only after `json.dumps` has
    serialized the same value, and json refuses a circular structure before it
    can return -- so a cycle cannot arrive here, and a check for one would be a
    guard whose failing case no caller can construct.
    """
    pending: list[tuple[str, Any, _Trail]] = [("value", value, (path, None))]
    while pending:
        kind, current, trail = pending.pop()
        if kind == "key":
            if _is_unencodable(current):
                return f"{_at(trail)}, in the key {current!a}"
        elif isinstance(current, str):
            if _is_unencodable(current):
                return _at(trail)
        elif isinstance(current, dict):
            # The encoder sees sorted keys; insertion order could pair its offender
            # with a different field's path when several strings are unencodable.
            tasks: list[tuple[str, Any, _Trail]] = []
            for key in sorted(current):
                # This locator must not replace the original refusal if called
                # independently of the canonical-vocabulary walk.
                if not isinstance(key, str):
                    continue
                tasks.append(("key", key, trail))
                tasks.append(("value", current[key], (f".{_segment(key)}", trail)))
            pending.extend(reversed(tasks))
        elif isinstance(current, (list, tuple)):
            for index in range(len(current) - 1, -1, -1):
                pending.append(("value", current[index], (f"[{index}]", trail)))
    return None


def _is_unencodable(text: str) -> bool:
    """Whether a string survives a JSON read but has no UTF-8 form."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def canonical_bytes(value: Any) -> bytes:
    """The one serialization. Same content in, same bytes out, on every machine."""
    try:
        _refuse_floats(value)
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except RecursionError as error:
        # `_refuse_floats` no longer recurses and names a cycle itself, so what
        # is left under this guard is `json.dumps`, which recurses in C and
        # cannot be rewritten here. Excessive nesting still means this process
        # cannot establish a canonical form, and it may not escape as an
        # implementation traceback.
        raise TypeError(
            "structure is recursive or nests too deeply for canonical JSON; no "
            "canonical artifact can carry it or be hashed against it"
        ) from error
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        # json.loads accepts lone surrogates, but they have no UTF-8 form.
        # TypeError is the established boundary for values outside this
        # canonical vocabulary and is already handled by its consumers.
        offender = error.object[error.start : error.end]
        raise TypeError(
            f"unencodable character {offender!a} at {_unencodable_path(value) or '$'}: "
            "a lone surrogate survives a JSON read but has no UTF-8 form, so no "
            "canonical artifact can carry it or be hashed against it"
        ) from error


def canonical_text(value: Any) -> str:
    """The canonical form as text, for writing a file a human may also read."""
    return canonical_bytes(value).decode("utf-8")


def digest_bytes(data: bytes) -> str:
    """The digest of raw bytes — an image, a blob, a file already on disk."""
    return hashlib.sha256(data).hexdigest()


def is_sha256(value: Any) -> bool:
    """Whether a value is the lowercase hex shape every digest in this system uses."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def digest_of(value: Any) -> str:
    """The digest of a structure, via the one serialization."""
    return digest_bytes(canonical_bytes(value))


def self_hash(record: dict[str, Any], field: str = "self_hash") -> str:
    """The digest a record carries of itself, computed over the record without it.

    A self-hash that included itself would be impossible to compute and trivially
    unverifiable, so the field is removed first. `verify_self_hash` recomputes the
    same way, which is what lets a reader detect a record edited after sealing.
    """
    without = {key: item for key, item in record.items() if key != field}
    return digest_of(without)


def verify_self_hash(record: dict[str, Any], field: str = "self_hash") -> bool:
    """True when the record's stored self-hash matches its current content."""
    stored = record.get(field)
    if not isinstance(stored, str):
        return False
    try:
        return stored == self_hash(record, field)
    except (RecursionError, TypeError):
        # A boolean verifier must safely reject both an unrecomputable digest
        # and a mismatch; human-facing boundaries recover the cause separately.
        return False


def self_hash_refusal(record: dict[str, Any], field: str = "self_hash") -> str | None:
    """Name why no digest can be computed, without claiming when damage arose.

    Kept separate from the boolean verifier because only human-facing refusal
    paths need the diagnostic and its extra serialization.
    """
    try:
        self_hash(record, field)
    except TypeError as error:
        return str(error)
    except RecursionError:
        return (
            "it nests too deeply for this machine to walk, so its sealed hash was "
            "never recomputable here and nothing can be checked against it"
        )
    return None
