"""The operator layer of an Armarium package: what a person did to a delivered reading.

A reading an operator review decision released to export (a `release` of its
own holds, a `no-missed-act` of its page's) is delivered as the model read it,
and labelled "released by operator" with every decision behind it: who made it,
when, why, and the stored approval it rests on, and the hold codes it cleared.
`reading_hold_codes` are the reading's own holds (its Perlectio's, and a page
with no act) among them, which the export would otherwise refuse to deliver.

A reading a person corrected (an `edit` of the unit) is delivered as the
person's text and labelled "corrected by a person" (`common.correction`): its
row adds the person's `note` and `model_reading`, the model's reading it
corrects (its label, Perlectio reference, text digest, text status and serving
provenance). The rows stay text-free; the model's text itself is shown beside
the person's in `model_readings.jsonl` (with JSONL) and beneath the reading's
section in the text bundle, labelled "model reading (original)".

`sources.json` carries every row (`operator_actions`), so the label travels in
every package; `operator.jsonl` carries the same rows when the JSONL format is
selected, and the text bundle shows each beneath its reading's section. When the
run has review decisions, `sources.json` also names every delivered reading's own
hold codes (`reading_hold_codes`), so a reading delivered over them without its
row is refused. No row changes a reading's text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.canonical import is_sha256
from common.contracts.errors import SchemaRefusal
from common.contracts.outcomes import derive_record_text_status
from common.contracts.uncertainty import PAGE_READ_LECTIO
from common.contracts.uncertainty import validate as validate_uncertainty
from common.correction import (
    CORRECTED_LABEL,
    MODEL_READING_FIELDS,
    ORIGINAL_LABEL,
    text_sha256,
)

OPERATOR_MEMBER: Final = "operator.jsonl"
SOURCES_FIELD: Final = "operator_actions"
# `sources.json`'s map of each delivered reading's own hold codes, by reading id.
READING_HOLDS_FIELD: Final = "reading_hold_codes"
ROW_SCHEMA: Final = "armarium-operator-action.v1"
RELEASED_LABEL: Final = "released by operator"
LABELS: Final = frozenset({RELEASED_LABEL, CORRECTED_LABEL})
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
# A corrected row's further fields.
CORRECTED_ROW_FIELDS: Final = ROW_FIELDS | {"note", "model_reading"}
# The JSONL member that carries each corrected reading's model reading.
MODEL_READINGS_MEMBER: Final = "model_readings.jsonl"
MODEL_READING_SCHEMA: Final = "armarium-model-reading.v1"
MODEL_ROW_FIELDS: Final = frozenset(
    {
        "schema",
        "act_id",
        "act_key",
        "kind",
        "label",
        "text",
        "uncertainty",
        "text_status",
        "perlectio_ref",
    }
)
LABEL_LINE: Final = "operator_label: "
NOTE_LINE: Final = "operator_note:"
MODEL_LABEL_LINE: Final = "model_reading_label: "
MODEL_TEXT_LINE: Final = "model_reading_text:"
MODEL_UNCERTAINTY_LINE: Final = "model_reading_uncertainty:"
MODEL_STATUS_LINE: Final = "model_reading_text_status: "
ROW_LINE: Final = "operator_row:"
# Where `lines_for` puts `ROW_LINE`, by label: after the label line, or after the
# label, the note and the model reading's lines.
_ROW_LINE_AT: Final = {RELEASED_LABEL: 1, CORRECTED_LABEL: 9}
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


def corrected_row(
    row: Mapping[str, Any],
    override: Mapping[str, Any] | None,
    cleared: Sequence[str],
    decisions: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """One corrected reading's row: `released_row`'s, labelled, with the note and model reading.

    `provenance` is the established reading's correction provenance
    (`common.correction.correction_provenance`); `decisions` include its edits.
    """
    return {
        **released_row(row, override, cleared, decisions),
        "label": CORRECTED_LABEL,
        "note": provenance["note"],
        "model_reading": dict(provenance["model_reading"]),
    }


def model_reading_record(model: Mapping[str, Any]) -> dict[str, Any]:
    """The `model_readings.jsonl` row of one corrected reading's model reading."""
    return {
        "schema": MODEL_READING_SCHEMA,
        **{key: model[key] for key in MODEL_ROW_FIELDS - {"schema"}},
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def lines_for(row: Mapping[str, Any], model: Mapping[str, Any] | None = None) -> list[str]:
    """What a text bundle shows beneath a reading an operator acted on.

    The label in words, then for a corrected reading the person's note and the
    model's reading (`model`: its text, uncertainty and text status) under its
    own label, then the whole row as one JSON line, so the bundle recomputes on
    its own and no value can start a line a parser reads.
    """
    decided = "; ".join(
        f"{decision['decision']} by {decision['approver']} at {decision['timestamp']} "
        f"({decision['approval_ref']['run_relative_path']})"
        for decision in row["decisions"]
    )
    lines = [f"{LABEL_LINE}{row['label']}: {decided}; cleared {', '.join(row['cleared_codes'])}"]
    if row["label"] == CORRECTED_LABEL:
        if model is None:
            raise SchemaRefusal("a corrected reading's lines need the model reading beside it")
        lines += [
            NOTE_LINE,
            _json(row["note"]),
            f"{MODEL_LABEL_LINE}{ORIGINAL_LABEL}",
            MODEL_TEXT_LINE,
            _json(model["text"]),
            MODEL_UNCERTAINTY_LINE,
            _json(model["uncertainty"]),
            f"{MODEL_STATUS_LINE}{model['text_status']}",
        ]
    return [*lines, ROW_LINE, _json(row)]


def block_end(lines: Sequence[str], start: int) -> int:
    """The index just past the operator block whose label line is `lines[start]`."""
    for index in range(start, len(lines)):
        if lines[index] == ROW_LINE:
            return index + 2
    raise SchemaRefusal("a text-bundle operator block has no row")


def verify_model_reading(row: Mapping[str, Any], model: Any, subject: str) -> dict[str, Any]:
    """A corrected row's model reading as a format shows it, held to the row.

    Its text must hash to the row's digest, its uncertainty layer must be a
    model reading's and anchor to that text, and its text status must be the
    row's and the one that layer gives.
    """
    named = row["model_reading"]
    try:
        text, uncertainty, status = model["text"], model["uncertainty"], model["text_status"]
        if not isinstance(text, str) or text_sha256(text) != named["text_sha256"]:
            raise SchemaRefusal("text")
        validate_uncertainty(uncertainty, text)
        if (
            uncertainty["lectio_kind"] != PAGE_READ_LECTIO
            or status != named["text_status"]
            or status != derive_record_text_status(text, [], uncertainty)
        ):
            raise SchemaRefusal("layer")
    except (KeyError, TypeError, SchemaRefusal) as error:
        raise SchemaRefusal(
            f"{subject} does not show {row['act_key']}'s model reading (original) as its "
            "operator row names it"
        ) from error
    return {"text": text, "uncertainty": uncertainty, "text_status": status}


def _is_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _require_shape(row: Any) -> dict[str, Any]:
    """Refuse a row that is not the closed shape, before anything reads a field of it."""
    corrected = isinstance(row, dict) and row.get("label") == CORRECTED_LABEL
    fields = CORRECTED_ROW_FIELDS if corrected else ROW_FIELDS
    if not isinstance(row, dict) or set(row) != fields or row["schema"] != ROW_SCHEMA:
        raise SchemaRefusal(f"an operator row is not a {ROW_SCHEMA} row")
    if corrected:
        model = row["model_reading"]
        if (
            not (row["note"] is None or (isinstance(row["note"], str) and row["note"].strip()))
            or not isinstance(model, dict)
            or set(model) != MODEL_READING_FIELDS
            or model["label"] != ORIGINAL_LABEL
            or not isinstance(model["perlectio_ref"], dict)
            or not is_sha256(model["text_sha256"])
            or not isinstance(model["text_status"], str)
            or not isinstance(model["provenance"], dict)
            or not any(
                isinstance(decision, dict) and decision.get("decision") == "edit"
                for decision in row["decisions"]
            )
        ):
            raise SchemaRefusal(
                "a corrected reading's operator row does not name its edit, note and the "
                "model reading it corrects"
            )
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


def text_bundle_rows(
    lines: Sequence[str],
) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """`[(reading id, row, model reading)]` for each operator block in one text-bundle file.

    Each block must be exactly what its row renders, inside the section of the
    reading it names; a corrected row's model reading is read from its block
    and held to the row (`verify_model_reading`), and is None for any other.
    """
    placed: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
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
            start = index - _ROW_LINE_AT[row["label"]]
            model = None
            if row["label"] == CORRECTED_LABEL and start >= 0:
                try:
                    shown = {
                        "text": json.loads(lines[start + 5]),
                        "uncertainty": json.loads(lines[start + 7]),
                        "text_status": lines[start + 8].removeprefix(MODEL_STATUS_LINE),
                    }
                except ValueError as error:
                    raise SchemaRefusal(
                        "a text-bundle model reading (original) is not JSON"
                    ) from error
                model = verify_model_reading(row, shown, "the text bundle")
            if start < 0 or list(lines[start : index + 2]) != lines_for(row, model):
                raise SchemaRefusal("a text bundle's operator lines do not say what its row says")
            if current != row["act_id"]:
                raise SchemaRefusal("an operator row stands beneath another reading's section")
            placed.append((current, row, model))
    return placed
