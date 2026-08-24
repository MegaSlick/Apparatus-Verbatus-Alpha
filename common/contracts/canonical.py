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

# CPython permits an integer-to-decimal safety limit as low as 640 digits.  A
# canonical vocabulary whose accepted values changed with that process setting
# would not be one vocabulary at all, and no pipeline count, ordinal, or pixel
# coordinate has a legitimate need for hundreds of digits.  Refuse at the
# portable floor, before ``json.dumps`` can raise its bare, host-specific
# ``ValueError``.
_MAX_INTEGER_MAGNITUDE = 10**640


def _segment(key: str) -> str:
    """One path segment that is always printable.

    A refusal message ends up on an operator's stderr, and a key carrying a lone
    surrogate would raise `UnicodeEncodeError` a second time out of the `print`
    that was reporting the first one — the boundary crashing while naming the
    reason it refused.

    The test is printability, not ASCII-ness. This is a project about the very
    words: `$.Étienne` is the path an operator wants to read, and escaping every
    non-ASCII key to spare the one bad case would mangle the ordinary ones. A
    surrogate is unprintable, and so is every control character that would
    garble the line it lands in; those are escaped and nothing else is.
    """
    return key if key.isprintable() else ascii(key)


def _refuse_floats(value: Any, path: str = "$") -> None:
    """Walk the structure and refuse numbers outside the canonical vocabulary."""
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise TypeError(
            f"float at {path}: canonical artifacts carry integers, not floats — "
            "a float's JSON form is not stable enough to hash against"
        )
    if isinstance(value, int):
        if not -_MAX_INTEGER_MAGNITUDE < value < _MAX_INTEGER_MAGNITUDE:
            raise TypeError(
                f"integer at {path} exceeds 640 decimal digits: canonical artifacts "
                "refuse magnitudes whose JSON conversion depends on the host's "
                "integer-string safety limit"
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                # Rendering an integer key with thousands of digits can itself
                # raise ValueError on the refusal path.  The type and location
                # name the schema error without asking the hostile value to
                # format itself.
                raise TypeError(f"non-string key of type {type(key).__name__} at {path}")
            _refuse_floats(item, f"{path}.{_segment(key)}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_floats(item, f"{path}[{index}]")


def _unencodable_path(value: Any, path: str = "$") -> str | None:
    """Where the first string lives that UTF-8 cannot encode, or None.

    Walked only after `str.encode` has already failed, so the ordinary path
    pays nothing for it and the cost falls on the record that is being refused.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return path
        return None
    if isinstance(value, dict):
        # Match ``json.dumps(sort_keys=True)`` above.  The encoding error names
        # the first surrogate in canonical key order; walking insertion order
        # could pair that offender with a different field's path when a record
        # carried more than one unencodable string.
        for key in sorted(value):
            item = value[key]
            # Keys are always strings by the time this runs: `_refuse_floats`
            # has already refused any other kind. Guarded anyway rather than
            # assumed, because a locator that raises while locating would put
            # the wrong exception at the boundary.
            if not isinstance(key, str):
                continue
            if _unencodable_path(key) is not None:
                return f"{path}, in the key {key!a}"
            found = _unencodable_path(item, f"{path}.{_segment(key)}")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _unencodable_path(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


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
        # The parsed structure can be shallower than the JSON scanner's limit
        # and still exceed the pure-Python vocabulary walk above.  A recursive
        # in-memory list reaches the same exception.  Both mean there is no
        # canonical form this process can establish, so name that fact at the
        # serializer rather than leaking an implementation traceback.
        raise TypeError(
            "structure is recursive or nests too deeply for canonical JSON; no "
            "canonical artifact can carry it or be hashed against it"
        ) from error
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        # A lone surrogate is the one value that passes every check above and
        # still has no bytes. `json.loads` builds one happily from a `\udXXX`
        # escape, so a damaged or hand-edited artifact on disk parses, reaches
        # `verify_self_hash`, and used to leave this function as an uncaught
        # `UnicodeEncodeError` — a traceback at exactly the boundary whose job
        # is to name its refusals.
        #
        # Raised as `TypeError` deliberately, and not as a new exception class:
        # `TypeError` is already this module's word for "outside the canonical
        # vocabulary" (`_refuse_floats` above), and four guards written for the
        # float case name it by that meaning — `verify_self_hash` below,
        # `common/contracts/envelope.py::validate_envelope`,
        # `common/runtree/store.py`'s partition-receipt reread, and
        # `common/stage.py`'s serving-evidence reader. A new class would have
        # walked past every one of them.
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
        # A record nested deep enough exhausts the stack here even when it was
        # shallow enough for the JSON scanner that read it: `_refuse_floats`
        # recurses once per nesting level walking the *parsed* structure, a
        # second recursive pass a reader's own RecursionError guard on the read
        # itself does not cover. The fact this call exists to establish is the
        # same either way — a record whose self-hash cannot be trusted — so it
        # is refused exactly as a mismatched hash would be, not crashed on. The
        # same boundary also catches the serializer's TypeError for an unsupported
        # parsed value (a float, a non-string key, a lone surrogate) and lets a
        # refusing caller name that refusal through `self_hash_refusal` below.
        return False


def self_hash_refusal(record: dict[str, Any], field: str = "self_hash") -> str | None:
    """Why this record's current contents cannot be hashed, or None if they can.

    `verify_self_hash` answers one boolean for its many callers, which is the
    right shape for a check but throws away *which* of two very different facts
    it found: current contents whose computed digest differs from the stored
    digest, and current contents for which no digest can be computed at all. A
    malformed record may have begun that way or may have been damaged or edited
    after sealing; its present bytes cannot establish that history. They can and
    should name the canonicalization failure that prevents the comparison.

    A refusing caller — never the check itself — recomputes here to recover the
    distinction, so the breadth `verify_self_hash` needs for its other callers
    costs no diagnostic at the boundaries that report to a human. The cost is
    one extra serialization on a path that is already ending in a refusal.
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
