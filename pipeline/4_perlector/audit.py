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
    SCHEMA,
    audit_digest,
    change_record,
    neutral_prompt,
    text_change_span,
    validate_chain,
    validate_draft,
    validate_finding,
    validate_perlectio_audit,
)

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
    if policy["round_cap"] > 1:
        # `absolute_round_cap` is the sealed declaration of how far the cap may
        # ever be raised; this is what the code can currently honour. Pass C runs
        # exactly ONE span-scoped re-proof (design v2.1 §3: "ONE span-scoped
        # re-proof pass ... no cascade re-opening"; two seats read GOVERNANCE
        # 7/11 against multi-round text-changing loops), so a second round has no
        # implementation to run. Accepting `round_cap = 2` would seal that number
        # into every audit draft and finding on the run while still performing one
        # round: a recorded budget nothing measured (GOVERNANCE 10), and an
        # approval Tyrel granted for work that never happens. Refuse it here
        # rather than in the config file, so the sealed ceiling stays the standing
        # declaration and this refusal is what a multi-round build lifts.
        raise ContractError(
            "this build performs exactly one span-scoped audit re-proof, so an audit round "
            f"cap of {policy['round_cap']} would be sealed into every audit record without "
            "ever being run; raising it needs the multi-round pass, not only an approval "
            "reference"
        )
    return policy, digest_bytes(raw)


def _flag(flag_class: str, start: int, end: int) -> dict[str, Any]:
    if flag_class not in FLAG_CLASSES:
        raise SchemaRefusal(f"unknown audit flag class {flag_class!r}")
    return {"class": flag_class, "location": {"start": start, "end": end}}


def _numeric_key(digits: str) -> tuple[int, str]:
    """Order one decimal run against another without converting it to an int.

    `int()` raises `ValueError` on a digit run longer than CPython's 4300-digit
    string-conversion limit, and the text these flags are computed over is
    whatever the reader emitted. A degenerate run of digits from a real reader
    would have ended the Perlector mid-page with an unnamed `ValueError`
    instead of a flag -- and surviving what a model emits is this stage's job,
    not the model's (GOVERNANCE 7: feed it completely and measure it honestly).

    Length-then-lexicographic over the run with leading zeros stripped is
    exactly `int` ordering for non-negative decimals, at any length, with no
    limit to reach. The values are only ever compared with each other.
    """
    trimmed = digits.lstrip("0") or "0"
    return len(trimmed), trimmed


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
        if not isinstance(row.get("order"), int) or isinstance(row.get("order"), bool):
            raise SchemaRefusal("an audit semi-final has no integer declared order")
        geometry_order = row.get("geometry_order")
        # Proved comparable, not merely present: this is a sort key, and a
        # shape `sorted` cannot compare ends the whole page's flag pass in an
        # unnamed TypeError rather than one named refusal.
        if (
            not isinstance(geometry_order, tuple)
            or len(geometry_order) != 2
            or any(not isinstance(part, int) or isinstance(part, bool) for part in geometry_order)
        ):
            raise SchemaRefusal("an audit semi-final has no two-integer geometry order")
        # Proved present like every other field in this loop: `.get` with a
        # True default read a row that never stated its crop containment as
        # "fully inside", and the within-crop flag class silently stopped
        # firing for any future producer that omitted the key.
        if not isinstance(row.get("within_crop"), bool):
            raise SchemaRefusal("an audit semi-final does not say whether it stays within its crop")
        by_page[row["page_id"]].append(row)
    output: dict[str, list[dict[str, Any]]] = {row["act_id"]: [] for row in semi_finals}
    for rows in by_page.values():
        ordered = sorted(rows, key=lambda row: (row["order"], row["act_id"]))
        dates: list[tuple[tuple[int, str], dict[str, Any]]] = []
        numbers: list[tuple[tuple[int, str], dict[str, Any]]] = []
        for row in ordered:
            text = row["text"]
            for testimony in row["testimonia"]:
                if not isinstance(testimony, str):
                    raise SchemaRefusal("an audit testimony comparison is not text")
                if testimony != text:
                    start, end = text_change_span(text, testimony)
                    output[row["act_id"]].append(_flag("testimony-diff", start, end))
            repeated = re.search(r"\b(\w+)\s+\1\b", text, flags=re.IGNORECASE)
            if repeated:
                output[row["act_id"]].append(_flag("repetition", repeated.start(), repeated.end()))
            date = re.search(r"\b(\d{4})\b", text)
            if date:
                dates.append((_numeric_key(date.group(1)), row))
            number = re.search(r"\b(?:no\.?|number)\s*(\d+)\b", text, flags=re.IGNORECASE)
            if number:
                numbers.append((_numeric_key(number.group(1)), row))
            # The audit only records locations in the text delivered from this
            # act's crop. It deliberately has no page partition or residual-ink
            # predicate; those belong to the Recensor.
            if not row["within_crop"]:
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
