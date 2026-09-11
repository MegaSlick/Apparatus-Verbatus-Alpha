"""Render a review projection as plain language a person can read on a terminal.

The confined console child hands the projection back as JSON and nothing else
(`console.py`: `json.dumps` is what makes a hostile filename or review reason
inert across the custody boundary). This module runs in the parent, over that
already-checked JSON, and turns it into the lines the operator actually reads:
which stages ran, which pages and crops exist, where each act stands, what is
held and why, and the one supported next action. Before it existed the
`review` verb printed the raw projection, and a person following an unfinished
run had to read JSON to learn why it stopped (independent audit of 2026-09-10,
finding F3).

Every string from the projection passes through `inert` before it reaches a
line: a control character becomes its escaped spelling rather than reaching the
terminal, and nothing else about the text changes. `cli._print` strips control
bytes again on the way out; two layers, because this is the surface where a run
tree's own bytes meet a person's screen.
"""

from __future__ import annotations

from typing import Any


class ProjectionShapeError(ValueError):
    """A projection list carries an entry that is not an object.

    Raised by name -- the list and the index -- rather than skipped: a row the
    renderer silently passed over would be a row a person never sees, and the
    parent reports this as a fault of its own pipe, never as a claim about the
    run tree (`cli._review_in_custody`). Found by CodeRabbit on the first
    candidate.
    """

    def __init__(self, field: str, index: int, entry: Any) -> None:
        super().__init__(
            f"projection field {field!r} entry {index} is {type(entry).__name__}, not an object"
        )
        self.field = field
        self.index = index


def _rows(projection: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Every entry of one projection list, each proved to be an object first."""
    rows = projection.get(field) or ()
    if not isinstance(rows, (list, tuple)):
        raise ProjectionShapeError(field, -1, rows)
    for index, entry in enumerate(rows):
        if not isinstance(entry, dict):
            raise ProjectionShapeError(field, index, entry)
    return list(rows)


def _nested_rows(parent: dict[str, Any], field: str, label: str) -> list[dict[str, Any]]:
    rows = parent.get(field) or ()
    if not isinstance(rows, (list, tuple)):
        raise ProjectionShapeError(label, -1, rows)
    for index, entry in enumerate(rows):
        if not isinstance(entry, dict):
            raise ProjectionShapeError(label, index, entry)
    return list(rows)


def inert(value: Any) -> str:
    """One projection value as terminal-safe text, control characters escaped."""
    text = value if isinstance(value, str) else repr(value)
    return "".join(
        character if character == " " or character.isprintable() else f"\\u{ord(character):04x}"
        for character in text
    )


def _digest(value: Any) -> str:
    return inert(value)[:12] + "…" if isinstance(value, str) and len(value) > 12 else inert(value)


def _one_line(value: Any, limit: int = 160) -> str:
    text = inert(value).replace("\n", " / ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(projection: dict[str, Any]) -> list[str]:
    """The operator's view of one run, in reading order.

    Sections appear in the order a person needs them when a run has stopped:
    what ran, whether an export exists, what to do next, then the holds, then
    every page and act with the exact image files behind them. A section with
    nothing to show says so rather than vanishing, because an absent heading
    reads as "nothing happened" and an empty one reads as "nothing here".
    """
    lines: list[str] = [f"Run {inert(projection.get('run_id'))}"]

    progress = _rows(projection, "progress")
    lines.append("")
    lines.append("Stages")
    if not progress:
        lines.append("  (no stage progress in this projection)")
    for row in progress:
        lines.append(
            f"  {inert(row.get('stage'))}: {inert(row.get('state'))} — {_one_line(row.get('note'))}"
        )

    export = projection.get("export") or {}
    lines.append("")
    if export.get("present"):
        lines.append(f"Export: present — {_one_line(export.get('note'))}")
    elif export:
        lines.append(f"Export: none — {_one_line(export.get('note'), limit=400)}")
    else:
        lines.append("Export: (not described in this projection)")

    next_action = projection.get("next_action") or {}
    lines.append("")
    lines.append("What you can do next")
    lines.append(f"  {_one_line(next_action.get('summary'), limit=1200)}")

    holds = _rows(projection, "holds")
    lines.append("")
    lines.append(f"Held or unresolved acts ({len(holds)})")
    for hold in holds:
        examination = hold.get("audit_examination")
        audit_note = f"; audit examination {inert(examination)}" if examination else ""
        lines.append(
            f"  {inert(hold.get('act_key'))} ({inert(hold.get('act_id'))}): "
            f"{inert(hold.get('outcome'))} by the {inert(hold.get('source'))}{audit_note}"
        )
        lines.append(f"    reason: {_one_line(hold.get('reason'), limit=600)}")
        record_ref = hold.get("record_ref") or {}
        if record_ref:
            lines.append(f"    record: {inert(record_ref.get('relative_path'))}")

    pages = _rows(projection, "pages")
    lines.append("")
    lines.append(f"Pages ({len(pages)})")
    for page in pages:
        head = f"  page {inert(page.get('ordinal'))}: {inert(page.get('outcome'))}"
        if page.get("page_id"):
            head += f" ({inert(page.get('page_id'))})"
        if page.get("reason"):
            head += f" — {_one_line(page.get('reason'))}"
        lines.append(head)
        if page.get("image_path"):
            lines.append(
                f"    image: {inert(page.get('image_path'))} sha256 {_digest(page.get('image_sha256'))}"
            )

    acts = _rows(projection, "acts")
    lines.append("")
    lines.append(f"Acts ({len(acts)})")
    for act in acts:
        lines.append(
            f"  {inert(act.get('act_key'))} ({inert(act.get('act_id'))}): "
            f"{inert(act.get('category'))}"
        )
        if act.get("reason"):
            lines.append(f"    reason: {_one_line(act.get('reason'), limit=600)}")
        row = act.get("row") or {}
        reading = row.get("reading") if isinstance(row, dict) else None
        if isinstance(reading, dict):
            audit = reading.get("audit") or {}
            lines.append(
                f"    reading: {inert(reading.get('outcome'))}; truncation "
                f"{inert(reading.get('truncation'))}; audit examination "
                f"{inert(audit.get('examination'))}"
            )
            if isinstance(reading.get("text"), str):
                lines.append(f"    machine reading: {_one_line(reading.get('text'), limit=300)}")
        elif isinstance(row, dict) and isinstance(row.get("text"), str):
            lines.append(f"    delivered text: {_one_line(row.get('text'), limit=300)}")
        review = row.get("review") if isinstance(row, dict) else None
        if isinstance(review, dict):
            lines.append(
                f"    review: {inert(review.get('outcome'))} — {_one_line(review.get('reason'), limit=600)}"
            )
        testimonia = (
            _nested_rows(row, "testimonia", "acts[].row.testimonia")
            if isinstance(row, dict)
            else []
        )
        if testimonia:
            witnessed = ", ".join(
                f"{inert(entry.get('chair'))} {inert(entry.get('outcome'))}" for entry in testimonia
            )
            lines.append(f"    witnesses: {witnessed}")
        crops = _nested_rows(act, "crops", "acts[].crops")
        for crop in crops:
            origin = f" ({inert(crop.get('origin'))})" if crop.get("origin") else ""
            lines.append(
                f"    crop {inert(crop.get('region_id'))} on page {inert(crop.get('ordinal'))}"
                f"{origin}: {inert(crop.get('image_path'))} sha256 "
                f"{_digest(crop.get('image_sha256'))}"
            )
        if not crops:
            lines.append("    crops: none recorded")

    review_items = projection.get("review_items")
    lines.append("")
    if review_items is None:
        lines.append("Review queue: not produced (the Armarium has not run)")
    else:
        review_items = _rows(projection, "review_items")
        lines.append(f"Review queue ({len(review_items)})")
        for item in review_items:
            row = item.get("row") if isinstance(item.get("row"), dict) else item
            lines.append(
                f"  {inert(row.get('act_key', row.get('act_id')))}: "
                f"{inert(row.get('category'))} — {_one_line(row.get('reason'), limit=600)}"
            )

    advances = _rows(projection, "advance_records")
    lines.append("")
    lines.append(f"Advance records ({len(advances)})")
    for record in advances:
        current = "binds its boundary" if record.get("boundary_current") else "stale"
        note = f" — {_one_line(record.get('boundary_note'))}" if record.get("boundary_note") else ""
        lines.append(
            f"  {inert(record.get('boundary_stage'))}: {current}{note}; "
            f"record {inert(record.get('relative_path'))}"
        )
    return lines
