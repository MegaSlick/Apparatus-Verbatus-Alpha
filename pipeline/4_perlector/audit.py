"""Deterministic Pass-C flags, neutral span re-proof, and closed audit records.

The flag pass sees one frozen collection of semi-finals for a page.  It never
receives the re-proof result, which makes a second cascade structurally
impossible.  Re-proof is location-only: no prompt text may state a wanted
character or claim the semi-final is wrong.
"""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import ContractError, SchemaRefusal

SCHEMA: Final = "perlector-audit.v1"
FLAG_CLASSES: Final = frozenset(
    {"date-sequence", "numbering", "order", "testimony-diff", "repetition", "within-crop"}
)
_CONFIG_FIELDS: Final = frozenset(
    {"schema", "default_round_cap", "absolute_round_cap", "round_cap", "approval_ref"}
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


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    try:
        raw = Path(path).read_bytes()
        policy = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the Perlector audit declaration at {path} could not be read"
        ) from error
    if (
        not isinstance(policy, dict)
        or set(policy) != _CONFIG_FIELDS
        or policy.get("schema") != SCHEMA
    ):
        raise ContractError("the Perlector audit declaration is not its closed schema")
    numeric = ("default_round_cap", "absolute_round_cap", "round_cap")
    if any(
        not isinstance(policy.get(key), int) or isinstance(policy[key], bool) for key in numeric
    ):
        raise ContractError("the Perlector audit declaration has non-integer round caps")
    if policy["default_round_cap"] != 1 or policy["absolute_round_cap"] < 1:
        raise ContractError(
            "the Perlector audit declaration must retain default cap 1 and a positive ceiling"
        )
    if not 0 <= policy["round_cap"] <= policy["absolute_round_cap"]:
        raise ContractError("the Perlector audit round cap is outside its sealed ceiling")
    if not isinstance(policy["approval_ref"], str):
        raise ContractError("the Perlector audit approval_ref must be a string")
    if policy["round_cap"] > policy["default_round_cap"] and not policy["approval_ref"].strip():
        raise ContractError(
            "an audit round cap above the default needs Tyrel's approval reference; "
            "the audit loop may not raise itself"
        )
    return policy, digest_bytes(raw)


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


def _span(text: str, other: str) -> tuple[int, int]:
    """Smallest semi-final span affected by an exact textual comparison."""
    start = 0
    limit = min(len(text), len(other))
    while start < limit and text[start] == other[start]:
        start += 1
    end = len(text)
    other_end = len(other)
    while end > start and other_end > start and text[end - 1] == other[other_end - 1]:
        end -= 1
        other_end -= 1
    return start, end


def _flag(flag_class: str, start: int, end: int) -> dict[str, Any]:
    if flag_class not in FLAG_CLASSES:
        raise SchemaRefusal(f"unknown audit flag class {flag_class!r}")
    return {"class": flag_class, "location": {"start": start, "end": end}}


def flags_once_per_page(semi_finals: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Compute every deterministic flag from the frozen semi-finals exactly once.

    The result is keyed by act id.  It contains no re-proof result or mutable
    state, so callers cannot make a changed result trigger flags for another act.
    """
    by_page: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in semi_finals:
        if not isinstance(row.get("act_id"), str) or not isinstance(row.get("page_id"), str):
            raise SchemaRefusal("an audit semi-final has no act or page identity")
        if not isinstance(row.get("text"), str) or not isinstance(row.get("testimonia"), list):
            raise SchemaRefusal("an audit semi-final has no text or testimonia")
        by_page[row["page_id"]].append(row)
    output: dict[str, list[dict[str, Any]]] = {row["act_id"]: [] for row in semi_finals}
    for rows in by_page.values():
        ordered = sorted(rows, key=lambda row: (row["order"], row["act_id"]))
        dates: list[tuple[int, dict[str, Any]]] = []
        numbers: list[tuple[int, dict[str, Any]]] = []
        for row in ordered:
            text = row["text"]
            for testimony in row["testimonia"]:
                if not isinstance(testimony, str):
                    raise SchemaRefusal("an audit testimony comparison is not text")
                if testimony != text:
                    start, end = _span(text, testimony)
                    output[row["act_id"]].append(_flag("testimony-diff", start, end))
            repeated = re.search(r"\b(\w+)\s+\1\b", text, flags=re.IGNORECASE)
            if repeated:
                output[row["act_id"]].append(_flag("repetition", repeated.start(), repeated.end()))
            date = re.search(r"\b(\d{4})\b", text)
            if date:
                dates.append((int(date.group(1)), row))
            number = re.search(r"\b(?:no\.?|number)\s*(\d+)\b", text, flags=re.IGNORECASE)
            if number:
                numbers.append((int(number.group(1)), row))
            # The audit only records locations in the text delivered from this
            # act's crop. It deliberately has no page partition or residual-ink
            # predicate; those belong to the Recensor.
            if not row.get("within_crop", True):
                output[row["act_id"]].append(_flag("within-crop", 0, len(text)))
        for values, flag_class in ((dates, "date-sequence"), (numbers, "numbering")):
            for previous, current in zip(values, values[1:], strict=False):
                if current[0] < previous[0]:
                    output[current[1]["act_id"]].append(
                        _flag(flag_class, 0, len(current[1]["text"]))
                    )
        expected_order = sorted(rows, key=lambda row: (row["geometry_order"], row["act_id"]))
        for declared, geometric in zip(ordered, expected_order, strict=True):
            if declared["act_id"] != geometric["act_id"]:
                output[declared["act_id"]].append(_flag("order", 0, len(declared["text"])))
    return {
        act_id: sorted(flags, key=lambda row: (row["location"]["start"], row["class"]))
        for act_id, flags in output.items()
    }


def _closed(payload: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise SchemaRefusal(f"an {label} is not its closed schema")
    return payload


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


def policy_record(policy: dict[str, Any], sha256: str) -> dict[str, str]:
    return {"schema": SCHEMA, "sha256": sha256, "approval_ref": policy["approval_ref"]}


def change_record(before: str, after: str, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if before == after:
        return []
    start, end = _span(before, after)
    triggering = next(
        (
            flag["class"]
            for flag in flags
            if flag["location"]["start"] <= start <= flag["location"]["end"]
        ),
        None,
    )
    if triggering is None:
        raise SchemaRefusal("an audit re-proof changed text outside every flagged location")
    return [{"start": start, "end": end, "triggering_flag_class": triggering}]


def audit_digest(payload: dict[str, Any]) -> str:
    """A helper for a consumer to bind the exact record it accepted."""
    return digest_of(payload)
