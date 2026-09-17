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
    silently shortened, reflowed version of it. The cut is named where it
    happens, so a person reading the plain view knows to reach for `--json` --
    or for the record the line already names.

    The newline becomes " / " *before* the escaping, not after. Escaping first
    turned every newline into `\\u000a`, so the replacement found nothing to
    replace and the multiline rendering the README describes never happened.

    Two lengths, because they are two different facts and one of them was
    reported as the other: the cut is counted in characters as they appear on
    screen, where one control character occupies six and one backslash two, and
    the value's own length is the length of the value.
    """
    text = inert(value.replace("\n", " / ") if isinstance(value, str) else value)
    if len(text) <= limit:
        return text
    raw = value if isinstance(value, str) else repr(value)
    return f"{text[:limit]}… (first {limit} characters as shown, of a {len(raw)}-character value)"


def _uncertainty_entries(value: Any, label: str) -> list[dict[str, Any]]:
    """One uncertainty layer, each entry proved an object, or a named shape fault.

    `None` is an absence -- the record carried no such layer -- and renders as
    nothing. Everything else goes through `_checked_rows` like every other list
    on this screen: a second copy of that rule is how the two drifted apart once
    already, when this one kept the `entry -1` sentinel F3 had removed.
    """
    return _checked_rows(() if value is None else value, label)


def _uncertainty_folds(spans: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Identical span entries folded into one, with how many the layer carried.

    The published layer keeps an exhausted-cap projection and an identical
    reader-reported span as two entries, because the layer records that those
    characters were doubted twice
    (`pipeline/4_perlector/run.py::_union_with_projection`). Printing them as two
    doubts would say something else again, so they are shown once with the count
    beside them. Order is first appearance, which keeps the projection ahead of
    the reader's report as the layer does.

    What a repeat MEANS is not decided here, or anywhere else on this screen: no
    artifact names the instrument behind any one span, and two audit flags of
    different classes may share one location, so a fold is evidence of a repeat
    and of nothing else.
    """
    folded: list[tuple[dict[str, Any], int]] = []
    for span in spans:
        for index, (seen, count) in enumerate(folded):
            if seen == span:
                folded[index] = (seen, count + 1)
                break
        else:
            folded.append((span, 1))
    return folded


def _uncertainty_alternatives(span: dict[str, Any], label: str) -> list[str]:
    """One span's alternative readings, each proved a string.

    Unchecked, a bare string here iterated character by character and printed
    `a, b, c` as though the reader had offered three readings, and a number
    raised out of the renderer into `cli.main`'s catch-all -- the failure
    `_object` exists to end.
    """
    values = span.get("alternatives")
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ProjectionShapeError(label, None, values, expected="a list of alternative readings")
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ProjectionShapeError(label, index, value, expected="a reading")
    return list(values)


_UNCERTAINTY_STATES = ("assessed", "not-assessed", "malformed")


def _uncertainty_lines(
    assessment: Any,
    *,
    spans: Any,
    gaps: Any,
    revisions: Any,
    text: Any,
    label: str,
    assessment_key: str,
    attributable: bool,
    outcome: Any,
) -> list[str]:
    """The reader's own doubt report, rendered the same way wherever it is carried.

    `outcome` is the record's own word for whether a reading exists. Only a
    `not-run` record carries no text; a reading with any other outcome and no
    string text is a damaged record, refused by field rather than printed as an
    act that was never read (CodeRabbit on the round-5 head).

    The state line always comes first and always says which of the three states
    this record carries, so an empty layer is never displayed as a reader's
    confidence (independent audit of 2026-09-10, F2). The spans and gaps are
    then printed whenever there are any, whatever the state: the exhausted-cap
    projection mints spans on acts whose reader has no doubt channel, which is
    what a chair whose prompt has no doubt grammar reports -- the one bound in
    `config/models.toml` today has none, so under that configuration
    `not-assessed` beside real published spans is the only way a span reaches
    this surface at all. Returning after the state line hid exactly those.

    `attributable` says whether every span in the layer is the reader's own.
    Where the layer is a union of the audit's exhausted-cap projection and the
    reader's report, this surface cannot tell which entry is whose, and says so
    rather than crediting the reader with both. Only a record with no audit
    behind it is attributable, and every Perlectio carries an audit, so that
    form is reserved for a record kind no projection puts on this screen today.
    That last sentence depends on `audit` being in the Perlectio's closed field
    set (`pipeline/4_perlector/run.py::_PERLECTIO_FIELDS`), which is where it
    would stop being true (the independent review of 2026-09-11; GOVERNANCE 10).
    """
    if assessment is not None and not isinstance(assessment, dict):
        raise ProjectionShapeError(
            f"{label}.{assessment_key}", None, assessment, expected="an object"
        )
    # The layers are validated and rendered whether or not the assessment is
    # there. Returning on the absence first meant a malformed layer BESIDE a
    # missing assessment was never looked at, and the spans such a record did
    # carry were never shown -- the same silence one field over (found by
    # CodeRabbit on the round-4 head).
    spans = _uncertainty_entries(spans, f"{label}.uncertain_spans")
    gaps = _uncertainty_entries(gaps, f"{label}.gaps")
    revisions = _uncertainty_entries(revisions, f"{label}.self_revisions")
    alternatives = [
        _uncertainty_alternatives(span, f"{label}.uncertain_spans[{index}].alternatives")
        for index, span in enumerate(spans)
    ]
    state = assessment.get("state") if assessment is not None else None
    counted = f"{len(spans)} uncertain span(s), {len(gaps)} gap(s)"
    if revisions:
        counted += f", {len(revisions)} self-revision(s)"
    if assessment is None:
        # Absent, not malformed. Two different records arrive here and they are
        # two different facts: a reading sealed before the doubt report was part
        # of the record, and a `not-run` record, which is no reading at all --
        # an act held before the Perlector, a chair that was not there, a run
        # over capacity. Telling the second that its reading predates a contract
        # says something about a reading that does not exist. A `not-run`
        # payload carries no text either, which is what separates them here.
        #
        # Either way the line is printed rather than the doubt shown as no doubt
        # at all, which is exactly the silence F2 was about. The canonical layer
        # refuses a pre-contract record by name at the Archetypus; this surface
        # is where a person meets it first.
        if outcome != "not-run" and not isinstance(text, str):
            raise ProjectionShapeError(
                f"{label}.text", None, text, expected="a string on a reading that ran"
            )
        lines = [
            "    doubts: not recorded — this act was not read"
            if text is None
            else (
                "    doubts: not recorded — this reading was sealed before the reader's doubt "
                "report was part of the record"
            )
        ]
        if spans or gaps:
            lines.append(f"      published beside that absence: {counted}")
    elif state == "assessed":
        who = (
            "assessed by the reader"
            if attributable
            else (
                "assessed by the reader; this view cannot tell which of the span(s) below are "
                "its report and which the audit's"
            )
        )
        lines = [f"    doubts: {who}; {counted}"]
    else:
        if state in _UNCERTAINTY_STATES:
            named = inert(state)
        elif state is None:
            # A record whose assessment object has no `state` key at all. "None"
            # is this language's word, not a person's, and read as a measurement
            # it says nothing.
            named = "no state recorded"
        else:
            named = f"an unrecognised state ({_one_line(state, limit=60)})"
        lines = [f"    doubts: {named} — {_one_line(assessment.get('problem'), limit=300)}"]
        if spans or gaps:
            # Named as what they are: not the reader's report, which is what
            # the state above just said this record does not have.
            lines.append(f"      published beside that state, not by the reader: {counted}")
    folded = _uncertainty_folds(spans)
    folded_source = [spans.index(span) for span, _ in folded]
    if len(folded) != len(spans):
        lines.append(f"      (shown as {len(folded)} line(s); identical entries are folded)")
    shown_text = text if isinstance(text, str) else ""
    for position, (span, carried) in enumerate(folded):
        start, end = span.get("start"), span.get("end")
        anchors = (
            isinstance(start, int)
            and isinstance(end, int)
            and not isinstance(start, bool)
            and not isinstance(end, bool)
            and 0 <= start <= end <= len(shown_text)
        )
        # Python slices never complain, so a negative offset from a damaged run
        # tree would show characters from the END of the act under a line
        # printing the negative numbers -- ink presented as the doubted region
        # that is not it (GOALS 5).
        shown = (
            f"'{_one_line(shown_text[start:end], limit=120)}'"
            if anchors
            else "(these offsets do not anchor to the text shown)"
        )
        # Identical entries fold together, so the first occurrence's alternatives
        # are the fold's: the entries are equal field for field or they would
        # not have folded.
        offered = ", ".join(alternatives[folded_source[position]])
        # The count, never a provenance. No artifact records which instrument
        # wrote which span: two audit flags of different classes may share one
        # location, so a fold is evidence that the layer carried the entry
        # twice and of nothing else -- under `assessed` exactly as under every
        # other state (found by CodeRabbit on the round-4 head; GOVERNANCE 10).
        repeated = f"; carried {carried} times in the layer" if carried > 1 else ""
        lines.append(
            f"      [{inert(start)}, {inert(end)}) {shown} confidence "
            f"{inert(span.get('confidence'))}"
            + (f"; alternatives: {_one_line(offered, limit=160)}" if offered else "")
            + repeated
        )
    for index, gap in enumerate(gaps):
        witness_evidence = gap.get("witness_evidence")
        # Only absent defaults to empty. `or ()` swallowed `""`, `0` and `{}`,
        # each of which is a malformed value this screen would then have shown
        # as a gap nobody corroborated (found by CodeRabbit on the round-4 head).
        evidence = _checked_rows(
            () if witness_evidence is None else witness_evidence,
            f"{label}.gaps[{index}].witness_evidence",
        )
        chairs = ", ".join(inert(row.get("chair")) for row in evidence)
        start, end = gap.get("start"), gap.get("end")
        # A gap is where sight failed and carries no characters of its own, so
        # its two offsets are one position inside the text. Anything else is a
        # damaged record, and printing it as an anchored position would point a
        # person at ink the record does not name.
        anchored = (
            isinstance(start, int)
            and isinstance(end, int)
            and not isinstance(start, bool)
            and not isinstance(end, bool)
            and start == end
            and 0 <= start <= len(shown_text)
        )
        where = (
            f"at {inert(start)}"
            if anchored
            else (
                f"at [{inert(start)}, {inert(end)}), which does not anchor to the text shown "
                "as one zero-width position"
            )
        )
        lines.append(
            f"      gap ({inert(gap.get('position'))}) {where}"
            + (f"; corroborated by {_one_line(chairs, limit=160)}" if chairs else "")
        )
    return lines


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
    # F041: one act held by the Designator and reviewed as held by the Recensor
    # is two rows below and one act to resolve; counting rows here reported two
    # acts held on a run that had one, contradicting README.md's own account of
    # this header and the distinct count `review.py` already computes for the
    # summary sentence just above. `.get()`, not a subscript: every other read
    # of a `holds` entry in this function goes through `.get()`, because this
    # renderer's whole job is to turn a malformed projection into
    # `ProjectionShapeError` rather than an uncaught exception (see this
    # module's own docstring) -- a subscript here would be the one place that
    # promise did not hold.
    held_acts = len({hold.get("act_id") for hold in holds})
    lines.append(f"Held or unresolved acts ({held_acts})")
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
    acts_note = projection.get("acts_denominator_note")
    lines.append("")
    if acts_note:
        lines.append(f"Acts ({len(acts)}; {_one_line(acts_note, limit=300)})")
    else:
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
            truncation = _object(reading, "truncation", "acts[].row.reading.truncation")
            lines.append(
                f"    reading: {inert(reading.get('outcome'))}; truncation "
                f"{inert(truncation.get('classification'))}; audit examination "
                f"{inert(audit.get('examination'))}"
            )
            if isinstance(reading.get("text"), str):
                lines.append(f"    machine reading: {_one_line(reading.get('text'), limit=300)}")
            lines.extend(
                _uncertainty_lines(
                    reading.get("uncertainty_assessment"),
                    spans=reading.get("uncertain_spans"),
                    gaps=reading.get("gaps"),
                    revisions=reading.get("self_revision"),
                    text=reading.get("text"),
                    label="acts[].row.reading",
                    assessment_key="uncertainty_assessment",
                    # The Perlectio's own audit record. Where it is present the
                    # published layer is a union with the audit's projection,
                    # and no entry says which instrument wrote it.
                    attributable=not isinstance(reading.get("audit"), dict),
                    outcome=reading.get("outcome"),
                )
            )
        elif isinstance(row.get("text"), str):
            lines.append(f"    delivered text: {_one_line(row.get('text'), limit=300)}")
            # A delivered act has no Perlectio row in this view -- the export
            # row is what the console shows -- so the doubt report is read off
            # the canonical layer the export carries. Without this an act that
            # reached the product unassessed would look, to the one person
            # reviewing it, exactly like one whose reader found no doubt
            # (independent audit of 2026-09-10, F2).
            uncertainty = _object(row, "uncertainty", "acts[].row.uncertainty")
            lines.extend(
                _uncertainty_lines(
                    uncertainty.get("assessment"),
                    spans=uncertainty.get("uncertain_spans"),
                    gaps=uncertainty.get("gaps"),
                    revisions=uncertainty.get("self_revisions"),
                    text=row.get("text"),
                    label="acts[].row.uncertainty",
                    assessment_key="assessment",
                    # A delivered act reached the product through the audit
                    # chain, so its layer is a union like any other established
                    # reading's and nothing here says which entry is whose.
                    attributable=False,
                    # A delivered act was read by definition; its text is the
                    # string this branch was entered on.
                    outcome="read",
                )
            )
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
            attempt = (
                f" (attempt {inert(crop.get('attempt_ordinal'))})"
                if crop.get("attempt_ordinal") is not None
                else ""
            )
            lines.append(
                f"    crop {inert(crop.get('region_id'))} on page {inert(crop.get('ordinal'))}"
                f"{origin}{attempt}: {inert(crop.get('image_path'))} sha256 "
                f"{_digest(crop.get('image_sha256'))}"
            )
        # The projection says why a crop list is empty, because "none recorded"
        # was a statement about the export row read as a statement about the run.
        if not crops:
            lines.append(f"    crops: {_one_line(act.get('crops_note') or 'none recorded', 300)}")
        elif act.get("crops_note"):
            lines.append(f"    crops: {_one_line(act.get('crops_note'), 300)}")

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
            # Three cases, because the old `or item` fallback collapsed two of
            # them. This projection wraps each queue row with the bundle member
            # and line number it came from, so an entry carrying a `row` key is
            # that wrapper: an empty row there is an empty row, and it is named
            # by its line rather than printed as "None: None — None" out of the
            # wrapper's own keys. An entry with no `row` key at all is not this
            # projection's wrapper, and the entry itself is the row -- dropping
            # its content would hide what the queue said.
            if "row" not in item:
                row = item
            else:
                row = _object(item, "row", "review_items[].row")
                if not row:
                    lines.append(
                        f"  line {inert(item.get('line'))} of "
                        f"{inert(item.get('member'))}: this queue row is empty"
                    )
                    continue
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
