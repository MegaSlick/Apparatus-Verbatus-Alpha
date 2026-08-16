"""Deterministic Pass-C flags, neutral span re-proof, and closed audit records.

The flag pass sees one frozen collection of semi-finals for a page.  It never
receives the re-proof result, which makes a second cascade structurally
impossible.  Re-proof is location-only: no prompt text may state a wanted
character or claim the semi-final is wrong.

The validation surface both this stage and the Recensor need
(`validate_draft`, `validate_finding`, `audit_digest`, `FLAG_CLASSES`,
`neutral_prompt`) lives in `common/perlector_audit.py` — a stage may not
import another stage's uniquely named module
(`pipeline/test_stage_import_boundaries.py`) — and is re-exported here so
this module's public API is unchanged for its own run.py and tests.
"""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.perlector_audit import (  # noqa: F401  (re-export)
    FLAG_CLASSES,
    audit_digest,
    neutral_prompt,
    validate_draft,
    validate_finding,
)

SCHEMA: Final = "perlector-audit.v1"
_CONFIG_FIELDS: Final = frozenset(
    {"schema", "default_round_cap", "absolute_round_cap", "round_cap", "approval_ref"}
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
