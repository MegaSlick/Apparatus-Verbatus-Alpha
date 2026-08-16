"""Perlector Pass-C audit records, shared by their two stage-side halves.

`pipeline/4_perlector/audit.py`'s run.py produces the audit draft and finding
records; `pipeline/5_recensor/run.py`'s `audit_state` consumes them to decide
review routing. The producer and the consumer must validate the exact same
closed schema, or drift between the two would let a malformed record pass one
side and fail the other silently. A stage may not import another stage's
uniquely named module (`pipeline/test_stage_import_boundaries.py`), so the
shared validation surface lives here; `pipeline/4_perlector/audit.py` keeps
its producer-only logic (the flag pass, the change record, the sealed policy
loader) and re-exports these names so its own public API is unchanged.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import digest_of
from common.contracts.errors import SchemaRefusal

FLAG_CLASSES: Final = frozenset(
    {"date-sequence", "numbering", "order", "testimony-diff", "repetition", "within-crop"}
)
_DRAFT_FIELDS: Final = frozenset(
    {"act_key", "attempt_ordinal", "semi_final_text", "page_id", "round_cap", "policy", "flags"}
)
_FINDING_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "page_id",
        "round_cap",
        "policy",
        "flags",
        "change_record",
        "uncertain_spans",
        "unresolved",
    }
)


def neutral_prompt(*, start: int, end: int, text_length: int) -> str:
    if not 0 <= start <= end <= text_length:
        raise SchemaRefusal("an audit re-proof location lies outside the delivered text")
    prompt = (
        "Re-examine the ink at character location "
        f"[{start}, {end}) of the delivered act. Report only what the ink supports there; "
        "if it supports the existing text, record confirmed unchanged."
    )
    lowered = prompt.lower()
    forbidden = ("wrong", "incorrect", "should read", "expected", "replace with", "must change")
    if any(fragment in lowered for fragment in forbidden):
        raise SchemaRefusal("the audit re-proof prompt is not neutral")
    return prompt


def _closed(payload: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise SchemaRefusal(f"an {label} is not its closed schema")
    return payload


def _validate_common(value: dict[str, Any]) -> None:
    if not isinstance(value["act_key"], str) or not isinstance(value["page_id"], str):
        raise SchemaRefusal("an audit record has no act or page identity")
    if not isinstance(value["attempt_ordinal"], int) or isinstance(value["attempt_ordinal"], bool):
        raise SchemaRefusal("an audit record has no integer attempt ordinal")
    if not isinstance(value["round_cap"], int) or isinstance(value["round_cap"], bool):
        raise SchemaRefusal("an audit record has no integer round cap")
    if not isinstance(value["policy"], dict) or set(value["policy"]) != {
        "schema",
        "sha256",
        "approval_ref",
    }:
        raise SchemaRefusal("an audit record has no sealed policy reference")
    if not isinstance(value["flags"], list):
        raise SchemaRefusal("an audit record has no flag list")
    for flag in value["flags"]:
        if not isinstance(flag, dict) or set(flag) != {"class", "location"}:
            raise SchemaRefusal("an audit flag is not its closed schema")
        location = flag["location"]
        if (
            flag["class"] not in FLAG_CLASSES
            or not isinstance(location, dict)
            or set(location) != {"start", "end"}
        ):
            raise SchemaRefusal("an audit flag has an unknown class or malformed location")


def validate_draft(payload: Any) -> dict[str, Any]:
    value = _closed(payload, _DRAFT_FIELDS, "audit draft")
    _validate_common(value)
    return value


def validate_finding(payload: Any, *, text: str | None = None) -> dict[str, Any]:
    value = _closed(payload, _FINDING_FIELDS, "audit finding")
    _validate_common(value)
    if not isinstance(value["change_record"], list) or not isinstance(
        value["uncertain_spans"], list
    ):
        raise SchemaRefusal("an audit finding has malformed change or uncertainty records")
    if not isinstance(value["unresolved"], bool):
        raise SchemaRefusal("an audit finding does not say whether flags remain unresolved")
    for change in value["change_record"]:
        if not isinstance(change, dict) or set(change) != {"start", "end", "triggering_flag_class"}:
            raise SchemaRefusal("an audit change record is not its closed schema")
        if change["triggering_flag_class"] not in FLAG_CLASSES:
            raise SchemaRefusal("an audit change record names an unknown triggering flag class")
    if text is not None:
        for span in value["uncertain_spans"]:
            if not isinstance(span, dict) or set(span) != {"start", "end", "reason"}:
                raise SchemaRefusal("an audit uncertainty span is not its closed schema")
            if not 0 <= span["start"] <= span["end"] <= len(text) or not isinstance(
                span["reason"], str
            ):
                raise SchemaRefusal("an audit uncertainty span lies outside the final text")
    return value


def audit_digest(payload: dict[str, Any]) -> str:
    """A helper for a consumer to bind the exact record it accepted."""
    return digest_of(payload)
