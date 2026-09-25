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

# v1 envelopes carry the attempt binding and a self-hash, so a v0 run is not
# reusable under them.
SCHEMA_LABEL = "skeleton.v1"

# CPython's integer-to-decimal limit may be configured down to 640 digits;
# canonical acceptance must not vary with that host setting.
_MAX_INTEGER_MAGNITUDE = 10**640


def _segment(key: str) -> str:
    """Keep ordinary Unicode readable while making refusal paths safe for stderr."""
    return key if key.isprintable() else ascii(key)


# One walk position as (crumb, parent), rendered by `_at` only on refusal.
_Trail = tuple[str, Any] | None

# A deep position is rendered as its top, its bottom and the gap between.
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


# Declared, so acceptance never depends on the host's platform-dependent C
# recursion limit in `json.dumps`. Real records nest a handful of levels.
_MAX_CANONICAL_DEPTH = 256


def _enter_container(container: Any, open_path: set[int], trail: _Trail) -> None:
    """Open one container: refuse a cycle, refuse excessive depth, mark it open.

    `open_path` holds exactly the containers between the root and here, so its
    size is the current depth.
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

    Iterative, with its own declared depth bound and cycle check, so a deep or
    self-containing payload is refused in this module's terms rather than
    wherever a stack runs out. Only the current path is tracked, so a value that
    appears twice is walked twice. The path is rendered only on refusal.
    """
    # A "key" task refuses a bad key in depth-first order, after the preceding
    # sibling's subtree; an "exit" task closes a container once its children ran.
    pending: list[tuple[str, Any, _Trail]] = [("value", value, (path, None))]
    open_path: set[int] = set()
    while pending:
        kind, current, trail = pending.pop()
        if kind == "exit":
            open_path.discard(current)
            continue
        if kind == "key":
            # Named by type: a hostile key may fail while being rendered.
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

    Iterative: it runs inside `canonical_bytes`'s encode handler, outside the
    guard that names a `RecursionError`. No cycle check: `json.dumps` has already
    refused any cycle before this can run.
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
            # Sorted, as the encoder saw it.
            tasks: list[tuple[str, Any, _Trail]] = []
            for key in sorted(current):
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
        # Left for `json.dumps`, which recurses in C.
        raise TypeError(
            "structure is recursive or nests too deeply for canonical JSON; no "
            "canonical artifact can carry it or be hashed against it"
        ) from error
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        # json.loads accepts lone surrogates, but they have no UTF-8 form.
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
    """The digest a record carries of itself, computed over the record without that field."""
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
        return False


def self_hash_refusal(record: dict[str, Any], field: str = "self_hash") -> str | None:
    """Name why no digest can be computed, for human-facing refusals only."""
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
