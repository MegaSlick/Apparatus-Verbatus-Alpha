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
    """A projection field is not the shape this renderer reads out.

    Raised by name -- the field, and the index when there is one -- rather than
    skipped: a row the renderer silently passed over would be a row a person
    never sees, and the parent reports this as a fault of its own pipe, never as
    a claim about the run tree (`cli._review_in_custody`). Found by CodeRabbit on
    the first candidate.

    Three shapes, three sentences. `index=None` means the field itself is wrong,
    not one of its rows: `entry -1` read as a row number and sent a person
    looking for a row that was never there.
    """

    def __init__(self, field: str, index: int | None, entry: Any, *, expected: str = "") -> None:
        if index is None:
            message = (
                f"projection field {field!r} is {type(entry).__name__}, "
                f"not {expected or 'the shape this view reads'}"
            )
        else:
            message = (
                f"projection field {field!r} entry {index} is {type(entry).__name__}, not an object"
            )
        super().__init__(message)
        self.field = field
        self.index = index


def _checked_rows(rows: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        raise ProjectionShapeError(label, None, rows, expected="a list of rows")
    for index, entry in enumerate(rows):
        if not isinstance(entry, dict):
            raise ProjectionShapeError(label, index, entry)
    return list(rows)


def _rows(projection: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Every entry of one projection list, each proved to be an object first."""
    return _checked_rows(projection.get(field) or (), field)


def _nested_rows(parent: dict[str, Any], field: str, label: str) -> list[dict[str, Any]]:
    return _checked_rows(parent.get(field) or (), label)


def _object(parent: dict[str, Any], field: str, label: str | None = None) -> dict[str, Any]:
    """One object-valued projection field, proved to be an object before it is used.

    The list fields were shape-checked from the first candidate and the object
    fields were not: `export`, `next_action`, a hold's `record_ref`, a reading's
    `audit` and an act's `row` were each used with `.get` after a truthiness
    test, so a string or a number in any of them raised `AttributeError` out of
    the renderer and `cli.main`'s catch-all turned it into "Verbatus met a
    problem it could not classify" -- when the true fact is the one
    `CONSOLE_PROJECTION_UNREADABLE` states, that this tool's own pipe returned
    something it cannot read out. Absent stays absent: `None` is an empty
    object, which is how a projection says a field has nothing in it.
    """
    value = parent.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectionShapeError(label or field, None, value, expected="an object")
    return value


def inert(value: Any) -> str:
    """One projection value as terminal-safe text, control characters escaped.

    A backslash in the run tree's own text is doubled before anything is
    escaped, so the tool's escaping is distinguishable from text that merely
    looks like it: without that, a reason string containing the six literal
    characters `\\u001b` reached the screen identically to a real escape
    character this function had just neutralised, and a hostile run tree could
    imitate the tool's own voice. No execution risk either way; the layer below
    (`cli._print`) strips control bytes again.
    """
    text = value if isinstance(value, str) else repr(value)
    return "".join(
        "\\\\"
        if character == "\\"
        else (
            character if character == " " or character.isprintable() else f"\\u{ord(character):04x}"
        )
        for character in text
    )


_REVIEW_QUEUE_SILENCES: dict[str | None, str] = {
    "no-export": "there is no Armarium export record, so no review queue was written",
    "bundle-has-no-review-items-member": (
        "the export bundle carries no review-items.jsonl member; the run exported without "
        "that format configured"
    ),
    None: "this projection does not say why",
}


def _digest(value: Any) -> str:
    return inert(value)[:12] + "…" if isinstance(value, str) and len(value) > 12 else inert(value)


def _one_line(value: Any, limit: int = 160) -> str:
    """One projection value on one line, saying so whenever it was cut.

    A parish act's text routinely passes any limit this screen can hold, and the
    one screen whose purpose is "review each act against its images" showed a
    silently shortened, reflowed version of it. The cut is now named where it
    happens, with the full length, so a person reading the plain view knows to
    reach for `--json` -- or for the record the line already names.
    """
    text = inert(value).replace("\n", " / ")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (first {limit} characters of {len(text)})"


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

    export = _object(projection, "export")
    lines.append("")
    if export.get("present"):
        # Present and complete are two facts, and the second is the one a person
        # is about to act on. A partial export says so in the heading, not only
        # in the note that follows it.
        state = "present and complete" if export.get("complete") else "present but partial"
        lines.append(f"Export: {state} — {_one_line(export.get('note'), limit=400)}")
    elif export.get("record_present"):
        lines.append(
            f"Export: an export record is here but the Armarium did not seal — "
            f"{_one_line(export.get('note'), limit=400)}"
        )
    elif export:
        lines.append(f"Export: none — {_one_line(export.get('note'), limit=400)}")
    else:
        lines.append("Export: (not described in this projection)")

    next_action = _object(projection, "next_action")
    lines.append("")
    lines.append("What you can do next")
    lines.append(f"  {_one_line(next_action.get('summary'), limit=1600)}")

    holds = _rows(projection, "holds")
    lines.append("")
    lines.append(f"Held or unresolved acts ({len(holds)})")
    for hold in holds:
        examination = hold.get("audit_examination")
        audit_note = f"; audit examination {inert(examination)}" if examination else ""
        # The label says which record this row is. One act held by the
        # Designator and reviewed as held by the Recensor produces two rows, and
        # unlabelled they read as two acts.
        label = hold.get("label")
        which = f" [{inert(label)}]" if label else ""
        lines.append(
            f"  {inert(hold.get('act_key'))} ({inert(hold.get('act_id'))}): "
            f"{inert(hold.get('outcome'))} by the {inert(hold.get('source'))}{which}{audit_note}"
        )
        lines.append(f"    reason: {_one_line(hold.get('reason'), limit=600)}")
        record_ref = _object(hold, "record_ref", "holds[].record_ref")
        if record_ref:
            lines.append(f"    record: {inert(record_ref.get('relative_path'))}")

    pages = _rows(projection, "pages")
    declared = projection.get("pages_declared")
    declared_note = projection.get("pages_declared_note")
    lines.append("")
    if isinstance(declared, int) and not isinstance(declared, bool):
        lines.append(f"Pages ({len(pages)} of {declared} declared)")
    elif declared_note:
        lines.append(f"Pages ({len(pages)}; {_one_line(declared_note, limit=300)})")
    else:
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
        row = _object(act, "row", "acts[].row")
        reading = _object(row, "reading", "acts[].row.reading")
        if reading:
            audit = _object(reading, "audit", "acts[].row.reading.audit")
            lines.append(
                f"    reading: {inert(reading.get('outcome'))}; truncation "
                f"{inert(reading.get('truncation'))}; audit examination "
                f"{inert(audit.get('examination'))}"
            )
            if isinstance(reading.get("text"), str):
                lines.append(f"    machine reading: {_one_line(reading.get('text'), limit=300)}")
        elif isinstance(row.get("text"), str):
            lines.append(f"    delivered text: {_one_line(row.get('text'), limit=300)}")
        review = _object(row, "review", "acts[].row.review")
        if review:
            lines.append(
                f"    review: {inert(review.get('outcome'))} — "
                f"{_one_line(review.get('reason'), limit=600)}"
            )
        review_ref = _object(row, "recensor_ref", "acts[].row.recensor_ref")
        if review_ref:
            lines.append(f"    review record: {inert(review_ref.get('relative_path'))}")
        testimonia = _nested_rows(row, "testimonia", "acts[].row.testimonia")
        if testimonia:
            witnessed = ", ".join(
                f"{inert(entry.get('chair'))} {inert(entry.get('outcome'))}"
                + (
                    f" (attempt {inert(entry.get('attempt_ordinal'))})"
                    if entry.get("attempt_ordinal") is not None
                    else ""
                )
                for entry in testimonia
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
        # Two different silences. An absent queue used to be reported as "the
        # Armarium has not run" whether or not it had: an export whose formats
        # do not include `review-items` produces a bundle with no such member,
        # and the projection now says which of the two this is.
        because = export.get("review_items_absent_because")
        said = _REVIEW_QUEUE_SILENCES.get(because if isinstance(because, str) else None)
        if said is None:
            said = f"this projection gives the reason as {inert(because)}"
        lines.append(f"Review queue: not produced ({said})")
    else:
        review_items = _rows(projection, "review_items")
        lines.append(f"Review queue ({len(review_items)})")
        for item in review_items:
            row = _object(item, "row", "review_items[].row") or item
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
