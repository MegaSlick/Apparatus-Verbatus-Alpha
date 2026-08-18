"""Perlector Pass-C audit records, shared by their two stage-side halves.

`pipeline/4_perlector/run.py` produces the audit draft and finding records
(`pipeline/4_perlector/audit.py` holds the flag pass and the sealed policy
loader, and re-exports these names); `pipeline/5_recensor/run.py`'s
`audit_state` consumes them to decide review routing. The producer and the consumer must validate the exact same
closed schema, or drift between the two would let a malformed record pass one
side and fail the other silently. A stage may not import another stage's
uniquely named module (`pipeline/test_stage_import_boundaries.py`), so the
shared validation surface lives here; `pipeline/4_perlector/audit.py` keeps
its producer-only logic (the flag pass and sealed policy loader) and re-exports
these names so its own public API is unchanged.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import digest_of, is_sha256
from common.contracts.envelope import validate_input_refs
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import PERLECTOR

SCHEMA: Final = "perlector-audit.v1"
AUDIT_CAP_EXHAUSTED: Final = "audit-round-cap-exhausted"
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
_PERLECTIO_AUDIT_FIELDS: Final = frozenset(
    {"draft_ref", "finding_ref", "finding_digest", "unresolved", "reproofs"}
)


# Module scope so tests can assert against the same closed list the runtime
# screen uses. The screen below is unreachable over today's fixed literal, and
# that is its job: it fires on every call the moment an editor softens the
# wording (or a future variant is built through this function), which is a
# stronger guard than a test that must remember to run.
FORBIDDEN_PROMPT_FRAGMENTS: Final = (
    "wrong",
    "incorrect",
    "should read",
    "expected",
    "replace with",
    "must change",
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
    if any(fragment in lowered for fragment in FORBIDDEN_PROMPT_FRAGMENTS):
        raise SchemaRefusal("the audit re-proof prompt is not neutral")
    return prompt


def _closed(payload: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise SchemaRefusal(f"an {label} is not its closed schema")
    return payload


def _location(value: Any, *, text_length: int | None, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise SchemaRefusal(f"an {label} has no closed character location")
    start, end = value["start"], value["end"]
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end
        or (text_length is not None and end > text_length)
    ):
        raise SchemaRefusal(f"an {label} lies outside the delivered text")
    return value


def _validate_common(value: dict[str, Any], *, text_length: int) -> None:
    if (
        not isinstance(value["act_key"], str)
        or not value["act_key"]
        or not isinstance(value["page_id"], str)
        or not value["page_id"]
    ):
        raise SchemaRefusal("an audit record has no act or page identity")
    if (
        not isinstance(value["attempt_ordinal"], int)
        or isinstance(value["attempt_ordinal"], bool)
        or value["attempt_ordinal"] < 1
    ):
        raise SchemaRefusal("an audit record has no integer attempt ordinal")
    if (
        not isinstance(value["round_cap"], int)
        or isinstance(value["round_cap"], bool)
        or value["round_cap"] < 0
    ):
        raise SchemaRefusal("an audit record has no integer round cap")
    if not isinstance(value["policy"], dict) or set(value["policy"]) != {
        "schema",
        "sha256",
        "approval_ref",
    }:
        raise SchemaRefusal("an audit record has no sealed policy reference")
    if (
        value["policy"]["schema"] != SCHEMA
        or not is_sha256(value["policy"]["sha256"])
        or not isinstance(value["policy"]["approval_ref"], str)
    ):
        raise SchemaRefusal("an audit record has a malformed sealed policy reference")
    if not isinstance(value["flags"], list):
        raise SchemaRefusal("an audit record has no flag list")
    for flag in value["flags"]:
        if not isinstance(flag, dict) or set(flag) != {"class", "location"}:
            raise SchemaRefusal("an audit flag is not its closed schema")
        if flag["class"] not in FLAG_CLASSES:
            raise SchemaRefusal("an audit flag has an unknown class or malformed location")
        _location(flag["location"], text_length=text_length, label="audit flag")


def validate_draft(payload: Any) -> dict[str, Any]:
    value = _closed(payload, _DRAFT_FIELDS, "audit draft")
    if not isinstance(value["semi_final_text"], str):
        raise SchemaRefusal("an audit draft has no semi-final text")
    _validate_common(value, text_length=len(value["semi_final_text"]))
    return value


def validate_finding(payload: Any, *, text: str, flag_text: str | None = None) -> dict[str, Any]:
    value = _closed(payload, _FINDING_FIELDS, "audit finding")
    if not isinstance(text, str):
        raise SchemaRefusal("an audit finding was validated without its final text")
    if flag_text is not None and not isinstance(flag_text, str):
        raise SchemaRefusal("an audit finding was validated without its frozen flag text")
    _validate_common(value, text_length=len(flag_text if flag_text is not None else text))
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
        _location(
            {"start": change["start"], "end": change["end"]},
            text_length=len(flag_text if flag_text is not None else text),
            label="audit change record",
        )
    for span in value["uncertain_spans"]:
        if not isinstance(span, dict) or set(span) != {"start", "end", "reason"}:
            raise SchemaRefusal("an audit uncertainty span is not its closed schema")
        _location(
            {"start": span["start"], "end": span["end"]},
            text_length=len(text),
            label="audit uncertainty span",
        )
        if span["start"] == span["end"] or span["reason"] != AUDIT_CAP_EXHAUSTED:
            raise SchemaRefusal("an audit uncertainty span has no exhausted-cap reason or width")
    if value["unresolved"] != (bool(value["flags"]) and value["round_cap"] == 0):
        raise SchemaRefusal("an audit finding's unresolved state contradicts its flags and cap")
    if bool(value["uncertain_spans"]) and not value["unresolved"]:
        raise SchemaRefusal("an audit finding carries uncertainty without an unresolved state")
    return value


def validate_perlectio_audit(record: Any, *, text_length: int | None) -> dict[str, Any]:
    value = _closed(record, _PERLECTIO_AUDIT_FIELDS, "Perlectio audit record")
    # Validate each typed reference here. Whether the two paths accidentally
    # alias is settled by `validate_chain`'s kind-specific reads, which then
    # names the actual draft-vs-finding contract violation rather than the
    # less useful generic duplicate-path refusal.
    validate_input_refs([value["draft_ref"]])
    validate_input_refs([value["finding_ref"]])
    if not is_sha256(value["finding_digest"]):
        raise SchemaRefusal("a Perlectio audit record has no finding payload digest")
    if not isinstance(value["unresolved"], bool) or not isinstance(value["reproofs"], list):
        raise SchemaRefusal("a Perlectio audit record has malformed resolution facts")
    for reproof in value["reproofs"]:
        if not isinstance(reproof, dict) or set(reproof) != {"class", "location", "prompt"}:
            raise SchemaRefusal("a Perlectio audit re-proof is not its closed schema")
        if reproof["class"] not in FLAG_CLASSES or not isinstance(reproof["prompt"], str):
            raise SchemaRefusal("a Perlectio audit re-proof has an unknown class or prompt")
        location = _location(
            reproof["location"], text_length=text_length, label="Perlectio audit re-proof"
        )
        if reproof["prompt"] != neutral_prompt(
            start=location["start"],
            end=location["end"],
            text_length=text_length if text_length is not None else location["end"],
        ):
            raise SchemaRefusal("a Perlectio audit re-proof is not a neutral location-only prompt")
    return value


def text_change_span(before: str, after: str) -> tuple[int, int]:
    """Smallest semi-final span affected by an exact textual comparison."""
    start = 0
    limit = min(len(before), len(after))
    while start < limit and before[start] == after[start]:
        start += 1
    end = len(before)
    after_end = len(after)
    while end > start and after_end > start and before[end - 1] == after[after_end - 1]:
        end -= 1
        after_end -= 1
    return start, end


def change_record(before: str, after: str, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attribute the re-proof's change to the flag that actually located it.

    The triggering class is the whole point of this record: spec 03's Pass C
    keeps it so that "witness-diff-triggered changes that moved toward the
    witness" — the soft-picker signature — is computable from the tree. So the
    change must be attributed to the *narrowest* flag that contains it, not to
    whichever flag the caller happened to list first. Flags reach here sorted by
    `(location.start, class)`, and the cross-act classes (`date-sequence`,
    `numbering`, `order`) all span the whole text from offset 0, so first-listed
    meant "the widest flag on the act wins": a correction squarely inside a
    narrow `testimony-diff` span was recorded as `date-sequence`, and the one
    measurement this record exists to support silently lost it (GOVERNANCE 10).

    Width ties break on `(start, class)` so the attribution is a function of the
    frozen flag set alone. Consumers re-derive this record exactly
    (`validate_chain`), so the rule may use nothing but the recorded facts.

    One re-proof still yields at most one changed span: the span is the
    prefix/suffix-trimmed envelope of an exact comparison, and a re-proof whose
    envelope escapes every single flag is refused rather than attributed by
    guesswork. Decomposing an envelope that covers two disjoint flags would need
    a real alignment, and this record is recomputed byte-for-byte by every later
    consumer — a diff heuristic here would make a sealed record's validity depend
    on the differ. That decomposition belongs with the real reader in R6, which
    is the first thing that can answer two locations in one pass.
    """
    if before == after:
        return []
    start, end = text_change_span(before, after)
    containing = [
        flag
        for flag in flags
        if flag["location"]["start"] <= start and end <= flag["location"]["end"]
    ]
    if not containing:
        raise SchemaRefusal("an audit re-proof changed text outside every flagged location")
    triggering = min(
        containing,
        key=lambda flag: (
            flag["location"]["end"] - flag["location"]["start"],
            flag["location"]["start"],
            flag["class"],
        ),
    )
    return [{"start": start, "end": end, "triggering_flag_class": triggering["class"]}]


def validate_chain(tree, reading: dict[str, Any], act_id: str) -> dict[str, Any]:
    """Validate the exact draft/finding/Perlectio relationship once for every reader."""
    payload = reading.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise SchemaRefusal(f"reading of {act_id} has no final text for its Pass-C audit")
    record = validate_perlectio_audit(payload.get("audit"), text_length=None)
    draft = tree.read_artifact_reference(
        record["draft_ref"], stage=PERLECTOR, kind="audit-draft", subject_id=act_id
    )
    finding = tree.read_artifact_reference(
        record["finding_ref"], stage=PERLECTOR, kind="audit-finding", subject_id=act_id
    )
    draft_payload = validate_draft(draft.get("payload"))
    validate_perlectio_audit(record, text_length=len(draft_payload["semi_final_text"]))
    finding_payload = validate_finding(
        finding.get("payload"),
        text=payload["text"],
        flag_text=draft_payload["semi_final_text"],
    )
    shared_fields = ("act_key", "attempt_ordinal", "page_id", "round_cap", "policy", "flags")
    if any(draft_payload[field] != finding_payload[field] for field in shared_fields):
        raise SchemaRefusal(f"audit draft and finding for {act_id} restate different frozen facts")
    if draft_payload["act_key"] != payload.get("act_key") or draft_payload[
        "attempt_ordinal"
    ] != payload.get("attempt_ordinal"):
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit identity")
    expected_changes = change_record(
        draft_payload["semi_final_text"], payload["text"], draft_payload["flags"]
    )
    if finding_payload["change_record"] != expected_changes:
        raise SchemaRefusal(f"reading of {act_id} disagrees with its exact audit change record")
    if record["finding_digest"] != audit_digest(finding_payload):
        raise SchemaRefusal(f"reading of {act_id} names an audit finding with a mismatched digest")
    if record["unresolved"] != finding_payload["unresolved"]:
        raise SchemaRefusal(f"reading of {act_id} contradicts its audit finding's unresolved state")
    if finding["inputs"] != [record["draft_ref"]]:
        raise SchemaRefusal(f"audit finding for {act_id} does not bind exactly its audit draft")
    if record["draft_ref"] not in reading.get("inputs", []) or record[
        "finding_ref"
    ] not in reading.get("inputs", []):
        raise SchemaRefusal(f"reading of {act_id} does not bind both audit artifacts as inputs")
    expected_reproofs = [
        {
            "class": flag["class"],
            "location": flag["location"],
            "prompt": neutral_prompt(
                start=flag["location"]["start"],
                end=flag["location"]["end"],
                text_length=len(draft_payload["semi_final_text"]),
            ),
        }
        for flag in draft_payload["flags"]
    ]
    if record["reproofs"] != expected_reproofs:
        raise SchemaRefusal(f"reading of {act_id} does not retain exactly its frozen re-proof plan")
    expected_uncertainty = [
        {
            "start": span["start"],
            "end": span["end"],
            "alternatives": [],
            "confidence": "low",
        }
        for span in finding_payload["uncertain_spans"]
    ]
    if payload.get("uncertain_spans") != expected_uncertainty:
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit uncertainty projection")
    return {"record": record, "draft": draft, "finding": finding}


def audit_digest(payload: dict[str, Any]) -> str:
    """A helper for a consumer to bind the exact record it accepted."""
    return digest_of(payload)
