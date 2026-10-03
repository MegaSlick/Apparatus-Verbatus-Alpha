"""One chair's outcome for one request, and the channel facts drawn from its output.

Both the fixture and the live posture build an `Attempt`, so the record a
response becomes does not depend on which posture read it.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# A witness response is untrusted: deep nesting would raise an uncaught
# `RecursionError` in `native_problem` and kill the whole run, not one attempt.
# Real output nests a few levels, so this is headroom.
_MAX_NATIVE_DEPTH = 64


class Attempt(NamedTuple):
    """One chair's resolved outcome for one page on one attempt.

    Describes one chair only; nothing here compares or ranks witnesses.
    """

    outcome: str
    native_payload: Any
    witness_reported: Any
    format_capabilities: dict[str, Any] | None
    health: dict[str, Any]
    reason: str | None
    raw_response_ref: dict[str, str] | None = None
    observation_payload: Any = None
    # Live-only fields, appended last so positional fixture constructors are
    # unchanged. `serving_call_ref` names this request's call-record blob.
    native_capture: dict[str, Any] | None = None
    serving_call_ref: dict[str, str] | None = None
    receipt_ref: dict[str, str] | None = None
    # Which sort of bytes `raw_response_ref` names; `None` on the fixture path.
    raw_response_kind: str | None = None
    # Chandra-only provenance over every physical request; not the payload.
    native_inference: dict[str, Any] | None = None
    # Fixture responses `(bytes, reference)` the page geometry is derived from,
    # each named in the page record's `raw_response_refs`.
    retained_responses: tuple[tuple[bytes, dict[str, str]], ...] = ()


def native_problem(value: Any, path: str = "payload", *, depth: int = 0) -> str | None:
    """Return why a native response cannot be retained as canonical JSON.

    Checked here so a bad response becomes a retained ``failed`` attempt rather than
    a crash in the artifact writer or a silent repair.
    """
    if depth > _MAX_NATIVE_DEPTH:
        return f"{path} nests deeper than {_MAX_NATIVE_DEPTH} levels"
    if value is None or isinstance(value, (bool, int)):
        return None
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            return f"{path} contains text that is not valid UTF-8"
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            if problem := native_problem(item, f"{path}[{index}]", depth=depth + 1):
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} has a non-string object key"
            try:
                key.encode("utf-8", "strict")
            except UnicodeEncodeError:
                return f"{path} has an object key that is not valid UTF-8"
            if problem := native_problem(item, f"{path}.{key}", depth=depth + 1):
                return problem
        return None
    return f"{path} has unsupported native type {type(value).__name__!r}"


def _native_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# Shared by the writer and `validate_content_health` so both agree on "no response".
NO_RESPONSE_HEALTH = {
    "native_type": None,
    "encoding": "not-applicable",
    "recordable": None,
    "empty": None,
    "blank": None,
    "truncated": None,
    "characters": None,
}


def no_response_health(*, reason: str) -> dict[str, Any]:
    """Health for a chair with no native response, never an empty reading."""
    return {**NO_RESPONSE_HEALTH, "truncation_basis": reason}


def unrecordable_health(basis: str, *, native_type: str = "unrecordable") -> dict[str, Any]:
    return {
        "native_type": native_type,
        "encoding": "invalid-or-unrecordable",
        "recordable": False,
        "empty": None,
        "blank": None,
        "truncated": None,
        "characters": None,
        "truncation_basis": basis,
    }


def content_health(native_payload: Any, *, completed: bool | None = None) -> dict[str, Any]:
    """Compute deterministic channel facts from native output alone.

    ``witness_reported`` is deliberately not an input: a self-report never becomes
    health. ``completed`` must come from a trusted response boundary, or be None.
    """
    if (problem := native_problem(native_payload)) is not None:
        return unrecordable_health(problem, native_type=_native_type(native_payload))

    if isinstance(native_payload, str):
        empty = native_payload == ""
        blank = native_payload.strip() == ""
        characters: int | None = len(native_payload)
    elif isinstance(native_payload, (dict, list)):
        empty = len(native_payload) == 0
        blank = None
        characters = None
    else:
        empty = False
        blank = None
        characters = None
    return {
        "native_type": _native_type(native_payload),
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": empty,
        "blank": blank,
        "truncated": None if completed is None else not completed,
        "characters": characters,
        "truncation_basis": (
            "trusted-response-boundary" if completed is not None else "not-recorded"
        ),
    }
