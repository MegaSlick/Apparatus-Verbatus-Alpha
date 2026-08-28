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
                # A hostile or huge key may fail while being rendered; its type
                # and location identify the schema defect without formatting it.
                raise TypeError(f"non-string key of type {type(key).__name__} at {path}")
            _refuse_floats(item, f"{path}.{_segment(key)}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_floats(item, f"{path}[{index}]")


def _unencodable_path(value: Any, path: str = "$") -> str | None:
    """Locate the first UTF-8 failure in canonical order for a refusal message."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return path
        return None
    if isinstance(value, dict):
        # The encoder sees sorted keys; insertion order could pair its offender
        # with a different field's path when several strings are unencodable.
        for key in sorted(value):
            item = value[key]
            # This locator must not replace the original refusal if called
            # independently of the canonical-vocabulary walk.
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
        # Both excessive nesting and cycles mean this process cannot establish
        # a canonical form; neither may escape as an implementation traceback.
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
