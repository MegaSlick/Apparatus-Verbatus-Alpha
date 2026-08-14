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


def _refuse_floats(value: Any, path: str = "$") -> None:
    """Walk the structure and refuse any float before it can be serialized."""
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise TypeError(
            f"float at {path}: canonical artifacts carry integers, not floats — "
            "a float's JSON form is not stable enough to hash against"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key at {path}: {key!r}")
            _refuse_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_floats(item, f"{path}[{index}]")


def canonical_bytes(value: Any) -> bytes:
    """The one serialization. Same content in, same bytes out, on every machine."""
    _refuse_floats(value)
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_text(value: Any) -> str:
    """The canonical form as text, for writing a file a human may also read."""
    return canonical_bytes(value).decode("utf-8")


def digest_bytes(data: bytes) -> str:
    """The digest of raw bytes — an image, a blob, a file already on disk."""
    return hashlib.sha256(data).hexdigest()


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
    except RecursionError:
        # A record nested deep enough exhausts the stack here even when it was
        # shallow enough for the JSON scanner that read it: `_refuse_floats`
        # recurses once per nesting level walking the *parsed* structure, a
        # second recursive pass a reader's own RecursionError guard on the read
        # itself does not cover. The fact this call exists to establish is the
        # same either way — a record whose self-hash cannot be trusted — so it
        # is refused exactly as a mismatched hash would be, not crashed on.
        return False
