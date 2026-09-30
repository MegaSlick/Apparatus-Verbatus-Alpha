"""The Recensor's page-path records: their shape, and how the stages after it read them.

A page-read run's Recensor publishes one `review` per counted unit of
`common.stage.reading_acts` (an act or other reading of a page, or the one row
standing for a page with none) and one `continuation-link` per page break
either side's answer flag names (`pipeline/5_recensor/CONTRACT.md`). The
Recensor writes them in the shapes named here, and the Archetypus and the
Armarium read them through this module, so no two stages can disagree about
them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import FatalAccounting
from common.contracts.stages import RECENSOR
from common.stage import (
    COUNTED_READING_CLASSES,
    NO_ACT_ON_PAGE_HOLD,
    PAGE_BLANK_HOLD,
    exemplar_page_ids,
    latest_attempt,
    stage_manifest,
)

REVIEW_KIND: Final = "review"
REVIEW_OPERATION: Final = "recense"
HELD: Final = "held-for-review"
# The row holds a review may release by name, on a page the Recensor confirms
# blank or holding no act. Every other row hold keeps its unit held.
RELEASABLE_HOLDS: Final = frozenset({PAGE_BLANK_HOLD, NO_ACT_ON_PAGE_HOLD})
# Of those, the one a reading can carry: once the Recensor confirms that a page
# read as holding no act holds none, the page's other readings are established.
# A blank page has no reading to establish.
RELEASABLE_READING_HOLDS: Final = frozenset({NO_ACT_ON_PAGE_HOLD})
# A page review's payload, as the Recensor builds it; `publish_review` adds
# `attempt_ordinal`.
PAGE_REVIEW_FIELDS: Final = frozenset(
    {
        "act_key",
        "unit_class",
        "kind",
        "page_ordinal",
        "reason",
        "hold_codes",
        "coverage",
        "page_reading_ref",
        "page_accounting_ref",
        "act_region_ref",
        "perlectio_ref",
        "page_coverage",
        "continuation",
        "uncertainty_assessment",
        "confirmation",
        "release",
        "notes",
        "recoveries_used",
    }
)
PUBLISHED_PAGE_REVIEW_FIELDS: Final = PAGE_REVIEW_FIELDS | {"attempt_ordinal"}
RELEASE_FIELDS: Final = frozenset({"hold_codes", "reason"})
NOTE_FIELDS: Final = frozenset({"code", "flags"})

CONTINUATION_LINK_KIND: Final = "continuation-link"
CONTINUATION_LINK_SCHEMA: Final = "recensor-continuation-link.v1"
CONTINUATION_LINK_FIELDS: Final = frozenset(
    {
        "schema",
        "from_page_ordinal",
        "to_page_ordinal",
        "from_act_id",
        "from_act_key",
        "to_act_id",
        "to_act_key",
        "continues_to_next_page",
        "continues_from_previous_page",
        "agreed",
    }
)


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise FatalAccounting(
            f"Recensor {record.get('kind')} {record.get('artifact_id')!r} has no payload"
        )
    return payload


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def reviewed_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows the Recensor decides about: every counted unit, so not a refused page's row."""
    return [row for row in rows if row["class"] in COUNTED_READING_CLASSES]


def current_page_reviews(context, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The current Recensor review of every counted unit, by act id.

    Every row must have one, and every review must name a row: a unit with no
    review reached no decision, and a review of a unit the denominator does not
    count decided about nothing this run counts.
    """
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for entry in stage_manifest(context, RECENSOR)["artifacts"]:
        if entry["kind"] != REVIEW_KIND:
            continue
        record = context.tree.read_artifact(RECENSOR, REVIEW_KIND, entry["artifact_id"])
        by_subject.setdefault(entry["subject_id"], []).append(record)
    counted = {row["act_id"] for row in rows}
    strays = sorted(set(by_subject) - counted)
    if strays:
        raise FatalAccounting(
            f"the Recensor reviewed {strays}, which this page-read run does not count; a "
            "review of an uncounted unit decided about nothing"
        )
    reviews: dict[str, dict[str, Any]] = {}
    for row in rows:
        act_id = row["act_id"]
        if act_id not in by_subject:
            raise FatalAccounting(
                f"{row['act_key']} ({act_id}) has no Recensor review; a counted unit nobody "
                "decided about has no terminal category"
            )
        review = latest_attempt(
            by_subject[act_id], f"review of {row['act_key']}", operation=REVIEW_OPERATION
        )
        _require_review_of_row(review, row)
        reviews[act_id] = review
    return reviews


def _require_review_of_row(review: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """The review is a page review of the row: its key, class, kind, page and records.

    Its reason, hold codes, notes and release must be in the page-review shape,
    and it is held exactly when it names a hold code.
    """
    payload = _payload(review)
    what = f"the Recensor review of {row['act_key']}"
    if set(payload) != PUBLISHED_PAGE_REVIEW_FIELDS:
        raise FatalAccounting(
            f"{what} is not the closed page-review shape: "
            f"{sorted(set(payload) ^ PUBLISHED_PAGE_REVIEW_FIELDS)}"
        )
    reading_ref = payload["perlectio_ref"]
    if (
        payload["act_key"] != row["act_key"]
        or payload["unit_class"] != row["class"]
        or payload["kind"] != row["kind"]
        or payload["page_ordinal"] != row["page_ordinal"]
        or reading_ref != row["perlectio_ref"]
        or payload["act_region_ref"] != row["region_ref"]
        or payload["page_reading_ref"] != row["reading_ref"]
        or payload["page_accounting_ref"] != row["accounting_ref"]
        or (reading_ref is not None and reading_ref not in review.get("inputs", []))
    ):
        raise FatalAccounting(
            f"{what} does not name that unit's key, class, kind, page and records as the "
            "denominator counts them"
        )
    release, notes = payload["release"], payload["notes"]
    if (
        not isinstance(payload["reason"], str)
        or not _strings(payload["hold_codes"])
        or (review.get("outcome") == HELD) != bool(payload["hold_codes"])
        or not (
            release is None
            or (
                isinstance(release, Mapping)
                and set(release) == RELEASE_FIELDS
                and _strings(release["hold_codes"])
                and isinstance(release["reason"], str)
            )
        )
        or not isinstance(notes, list)
        or not all(
            isinstance(note, Mapping)
            and set(note) == NOTE_FIELDS
            and isinstance(note["code"], str)
            and _strings(note["flags"])
            for note in notes
        )
    ):
        raise FatalAccounting(
            f"{what} does not carry its reason, hold codes, release and notes in the "
            "page-review shape, held exactly when it names a hold code"
        )


def require_establishable(row: Mapping[str, Any], review: Mapping[str, Any]) -> None:
    """An accepted review stands over a reading the denominator reads, or releases its hold.

    A row with no reading has no text to establish. A held row may be accepted
    only when every hold it carries is one a review may release and the review
    names exactly those codes in its `release`; otherwise a stage after the
    Recensor would be resurrecting a held reading.
    """
    if review.get("outcome") != "accepted":
        raise FatalAccounting(f"the review of {row['act_key']} did not accept it")
    if row["perlectio_ref"] is None:
        raise FatalAccounting(
            f"{row['act_key']} is a {row['class']} row with no reading, yet the Recensor "
            "accepted it; a page with no reading has no text to establish"
        )
    release = _payload(review).get("release")
    if row["disposition"] == "read" and release is None:
        return
    codes = list(row["hold_codes"])
    if (
        not codes
        or not set(codes) <= RELEASABLE_READING_HOLDS
        or not isinstance(release, Mapping)
        or release.get("hold_codes") != sorted(codes)
    ):
        raise FatalAccounting(
            f"{row['act_key']} is held ({', '.join(codes) or 'no code'}), yet the Recensor "
            "accepted it without releasing exactly those holds; a stage may not resurrect a "
            "held reading into an established one"
        )


def review_reason(review: Mapping[str, Any]) -> str:
    """The review's own reason, with its hold codes named when it holds."""
    payload = _payload(review)
    reason = payload.get("reason")
    reason = reason if isinstance(reason, str) else ""
    codes = payload.get("hold_codes")
    if isinstance(codes, list) and codes:
        named = ", ".join(str(code) for code in codes)
        return f"{reason} (holds: {named})" if reason else f"holds: {named}"
    return reason


def review_coverage(review: Mapping[str, Any]) -> dict[str, Any]:
    """The witness-coverage record the Recensor measured for the unit's page."""
    coverage = _payload(review).get("coverage")
    if not isinstance(coverage, dict):
        raise FatalAccounting(
            f"Recensor review {review.get('artifact_id')!r} carries no witness coverage record"
        )
    return coverage


def review_notes(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the review records for a reader without holding the unit, `{code, flags}` each."""
    return [dict(note) for note in _payload(review)["notes"]]


# --- page breaks ---------------------------------------------------------------------


def act_entries_by_page(acts: Sequence[Mapping[str, Any]]) -> dict[int, list[Mapping[str, Any]]]:
    """Each page's `act` entries: the only entries a page break can join."""
    entries: dict[int, list[Mapping[str, Any]]] = {}
    for act in acts:
        if act["n"] is not None and act["kind"] == "act":
            entries.setdefault(act["page_ordinal"], []).append(act)
    return entries


def page_breaks(
    pages: Mapping[int, str], acts: Sequence[Mapping[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Every page break an answer flags, as `(subject, payload)`, in page order.

    The last `act` entry of page p and the first of page p+1 are the break's
    two sides; either side's flag records the break, `agreed` only when both
    say so, and a break whose sides disagree is still recorded. A side with no
    `act` entry (a page not read, blank, of `other` entries only, or outside
    the run) is null. The link holds no unit and joins nothing.
    """
    entries = act_entries_by_page(acts)
    ordinals = sorted(pages)
    links = []
    for left in range(ordinals[0] - 1, ordinals[-1] + 1):
        right = left + 1
        last = max(entries.get(left, []), key=lambda act: act["n"], default=None)
        first = min(entries.get(right, []), key=lambda act: act["n"], default=None)
        to_next = last is not None and last["continues_to_next_page"] is True
        from_previous = first is not None and first["continues_from_previous_page"] is True
        if not (to_next or from_previous):
            continue
        links.append(
            (
                f"page-break:{left}:{right}",
                {
                    "schema": CONTINUATION_LINK_SCHEMA,
                    "from_page_ordinal": left,
                    "to_page_ordinal": right,
                    "from_act_id": last["act_id"] if last else None,
                    "from_act_key": last["act_key"] if last else None,
                    "to_act_id": first["act_id"] if first else None,
                    "to_act_key": first["act_key"] if first else None,
                    "continues_to_next_page": to_next,
                    "continues_from_previous_page": from_previous,
                    "agreed": to_next and from_previous,
                },
            )
        )
    return links


def continuation_links(context, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every `continuation-link`, as `{ref, from_page_ordinal, to_page_ordinal,
    head_act_id, tail_act_id, agreed}`.

    One link per page break an answer flags, subject `page-break:<p>:<p+1>`:
    `head` (`from_act_id`) is the last `act` entry of page p and `tail`
    (`to_act_id`) the first of page p+1, either `None` where that side has no
    act entry. Each named side must be a counted row under its own key whose
    reading the link inputs; the link is `agreed` exactly when both flags say
    an act crosses the break, and is `accepted` exactly when it agrees. A break
    has one link, and each link is a break `page_breaks` derives from `rows`,
    its sides its pages' act edges; a flagged break with no link is named by
    the aggregate (`unpaired_continuations`) and keeps the run partial.
    """
    counted = {row["act_id"]: row for row in rows}
    derived = dict(page_breaks(exemplar_page_ids(context), rows))
    links: list[dict[str, Any]] = []
    subjects: set[str] = set()
    for entry in stage_manifest(context, RECENSOR)["artifacts"]:
        if entry["kind"] != CONTINUATION_LINK_KIND:
            continue
        record = context.tree.read_artifact(RECENSOR, CONTINUATION_LINK_KIND, entry["artifact_id"])
        payload = _payload(record)
        what = f"Recensor continuation-link {entry['artifact_id']!r}"
        if set(payload) != CONTINUATION_LINK_FIELDS or payload["schema"] != (
            CONTINUATION_LINK_SCHEMA
        ):
            raise FatalAccounting(f"{what} is not a {CONTINUATION_LINK_SCHEMA} record")
        from_page, to_page = payload["from_page_ordinal"], payload["to_page_ordinal"]
        flags = (payload["continues_to_next_page"], payload["continues_from_previous_page"])
        sides = []
        for side, flag in (
            ("from", "continues_to_next_page"),
            ("to", "continues_from_previous_page"),
        ):
            act_id, act_key = payload[f"{side}_act_id"], payload[f"{side}_act_key"]
            if act_id is None and act_key is None:
                sides.append(None)
                continue
            row = counted.get(act_id) if isinstance(act_id, str) else None
            if row is None or row["act_key"] != act_key:
                raise FatalAccounting(f"{what} names a reading this run does not count")
            if row["page_ordinal"] != payload[f"{side}_page_ordinal"]:
                raise FatalAccounting(
                    f"{what} names {act_key}, which is not on its side of the page break"
                )
            if payload[flag] is not (row[flag] is True):
                raise FatalAccounting(f"{what} does not carry {act_key}'s own {flag} flag")
            if row["perlectio_ref"] not in record.get("inputs", []):
                raise FatalAccounting(f"{what} does not input the reading of {act_key}")
            sides.append(act_id)
        if (
            not all(
                isinstance(ordinal, int) and not isinstance(ordinal, bool)
                for ordinal in (from_page, to_page)
            )
            or to_page != from_page + 1
            or record.get("subject_id") != f"page-break:{from_page}:{to_page}"
            or not all(isinstance(flag, bool) for flag in flags)
            or not any(flags)
            or (flags[0] and sides[0] is None)
            or (flags[1] and sides[1] is None)
            or payload["agreed"] is not (flags[0] and flags[1])
            or record.get("outcome") != ("accepted" if payload["agreed"] else "held-for-review")
        ):
            raise FatalAccounting(
                f"{what} does not name one flagged page break, its flags, and whether they agree"
            )
        if record["subject_id"] in subjects:
            raise FatalAccounting(f"{record['subject_id']} has more than one continuation-link")
        if derived.get(record["subject_id"]) != payload:
            raise FatalAccounting(
                f"{what} is not the page break the counted rows' flags derive: no break is "
                "flagged there, or its sides are not the last and first act entries of their "
                "pages"
            )
        subjects.add(record["subject_id"])
        links.append(
            {
                "ref": {"relative_path": entry["relative_path"], "sha256": entry["sha256"]},
                "from_page_ordinal": from_page,
                "to_page_ordinal": to_page,
                "head_act_id": sides[0],
                "tail_act_id": sides[1],
                "continues_to_next_page": flags[0],
                "continues_from_previous_page": flags[1],
                "agreed": payload["agreed"],
            }
        )
    return sorted(links, key=lambda link: (link["from_page_ordinal"], link["to_page_ordinal"]))
