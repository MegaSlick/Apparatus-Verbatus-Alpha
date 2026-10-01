"""The operator layer of an Armarium package: what a person did to a delivered reading.

A reading an operator review decision released to export (a `release` of its
own holds, a `no-missed-act` of its page's) is delivered as the model read it,
and labelled "released by operator" with every decision behind it: who made it,
when, why, and the stored approval it rests on, and the hold codes it cleared.
`reading_hold_codes` are the reading's own holds (its Perlectio's, and a page
with no act) among them, which the export would otherwise refuse to deliver.

`sources.json` carries every row (`operator_actions`), so the label travels in
every package; `operator.jsonl` carries the same rows when the JSONL format is
selected, and the text bundle shows each beneath its reading's section. No row
changes a reading's text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import SchemaRefusal

OPERATOR_MEMBER: Final = "operator.jsonl"
SOURCES_FIELD: Final = "operator_actions"
ROW_SCHEMA: Final = "armarium-operator-action.v1"
RELEASED_LABEL: Final = "released by operator"
LABELS: Final = frozenset({RELEASED_LABEL})
ROW_FIELDS: Final = frozenset(
    {
        "schema",
        "act_id",
        "act_key",
        "kind",
        "label",
        "cleared_codes",
        "reading_hold_codes",
        "decisions",
    }
)
DECISION_FIELDS: Final = frozenset(
    {
        "decision",
        "scope",
        "subject_id",
        "approver",
        "timestamp",
        "reason",
        "approval_ref",
        "decision_hash",
    }
)
LABEL_LINE: Final = "operator_label: "
ROW_LINE: Final = "operator_row:"
# How many lines `lines_for` writes: the label, `ROW_LINE` and the row.
OPERATOR_LINES: Final = 3
# The headings under which a text bundle shows a reading, by the id line that opens it.
ID_LINES: Final = ("act-id: ", "other-id: ")


def released_row(
    row: Mapping[str, Any],
    override: Mapping[str, Any] | None,
    cleared: Sequence[str],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """One delivered reading's row: its decisions, the codes they cleared, its own overridden.

    `override` is `page_review.operator_override`'s result, None when the
    reading carried no hold of its own; `decisions` each clearing decision
    with the approval it was stored as (`approval_ref` a run-tree reference,
    which the package marks as retained-run evidence).
    """
    return {
        "schema": ROW_SCHEMA,
        "act_id": row["act_id"],
        "act_key": row["act_key"],
        "kind": row["kind"],
        "label": RELEASED_LABEL,
        "cleared_codes": sorted(set(cleared)),
        "reading_hold_codes": [] if override is None else list(override["codes"]),
        "decisions": sorted(
            (dict(decision) for decision in decisions),
            key=lambda decision: (decision["scope"], decision["decision_hash"]),
        ),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def lines_for(row: Mapping[str, Any]) -> list[str]:
    """What a text bundle shows beneath a reading an operator acted on.

    The label in words, then the whole row as one JSON line, so the bundle
    recomputes on its own and no value can start a line a parser reads.
    """
    decided = "; ".join(
        f"{decision['decision']} by {decision['approver']} at {decision['timestamp']} "
        f"({decision['approval_ref']['run_relative_path']})"
        for decision in row["decisions"]
    )
    return [
        f"{LABEL_LINE}{row['label']}: {decided}; cleared {', '.join(row['cleared_codes'])}",
        ROW_LINE,
        _json(row),
    ]


def _is_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _require_shape(row: Any) -> dict[str, Any]:
    """Refuse a row that is not the closed shape, before anything reads a field of it."""
    if not isinstance(row, dict) or set(row) != ROW_FIELDS or row["schema"] != ROW_SCHEMA:
        raise SchemaRefusal(f"an operator row is not a {ROW_SCHEMA} row")
    decisions = row["decisions"]
    if (
        row["label"] not in LABELS
        or row["kind"] not in ("act", "other")
        or not all(isinstance(row[key], str) and row[key] for key in ("act_id", "act_key"))
        or not _is_strings(row["cleared_codes"])
        or not row["cleared_codes"]
        or row["cleared_codes"] != sorted(set(row["cleared_codes"]))
        or not _is_strings(row["reading_hold_codes"])
        or not set(row["reading_hold_codes"]) <= set(row["cleared_codes"])
        or not isinstance(decisions, list)
        or not decisions
    ):
        raise SchemaRefusal("an operator row does not name its reading, label and cleared codes")
    for decision in decisions:
        reference = decision.get("approval_ref") if isinstance(decision, dict) else None
        if (
            not isinstance(decision, dict)
            or set(decision) != DECISION_FIELDS
            or not all(
                isinstance(decision[key], str) and decision[key]
                for key in DECISION_FIELDS - {"approval_ref"}
            )
            or not isinstance(reference, dict)
            or set(reference) != {"availability", "run_relative_path", "sha256"}
            or not all(isinstance(value, str) and value for value in reference.values())
        ):
            raise SchemaRefusal(
                "an operator row's decision does not say what was decided, by whom, when, why "
                "and on which stored approval"
            )
    return row


def verify_rows(
    rows: Any, delivered: Mapping[str, tuple[str, str]], subject: str
) -> list[dict[str, Any]]:
    """A package's operator rows, each in shape and about a reading it delivers.

    `delivered` maps each delivered reading's id to `(act_key, kind)`; a row
    about any other reading, or two about one, is refused.
    """
    if not isinstance(rows, list):
        raise SchemaRefusal(f"{subject} carries no list of operator rows")
    checked = []
    seen: set[str] = set()
    for row in rows:
        row = _require_shape(row)
        if delivered.get(row["act_id"]) != (row["act_key"], row["kind"]) or row["act_id"] in seen:
            raise SchemaRefusal(
                f"{subject} labels {row['act_key']} as acted on by an operator, but it is not a "
                "reading the package delivers once under that key"
            )
        seen.add(row["act_id"])
        checked.append(row)
    return sorted(checked, key=lambda row: row["act_id"])


def text_bundle_rows(lines: Sequence[str]) -> list[tuple[str, dict[str, Any]]]:
    """`[(reading id, row)]` for each operator block in one text-bundle file.

    Each block must be exactly what its row renders, inside the section of the
    reading it names.
    """
    placed: list[tuple[str, dict[str, Any]]] = []
    current: str | None = None
    for index, line in enumerate(lines):
        if line.startswith(ID_LINES):
            current = line.split(": ", 1)[1]
        elif line.startswith("## ") and index + 1 < len(lines):
            if not lines[index + 1].startswith(ID_LINES):
                current = None
        elif line == ROW_LINE:
            try:
                row = _require_shape(json.loads(lines[index + 1]))
            except (IndexError, ValueError) as error:
                raise SchemaRefusal("a text-bundle operator row is not JSON") from error
            expected = lines_for(row)
            start = index + 2 - len(expected)
            if start < 0 or list(lines[start : index + 2]) != expected:
                raise SchemaRefusal("a text bundle's operator lines do not say what its row says")
            if current != row["act_id"]:
                raise SchemaRefusal("an operator row stands beneath another reading's section")
            placed.append((current, row))
    return placed
