"""The Recensor's page-path records: their shape, and how the stages after it read them.

A page-read run's Recensor publishes one `review` per counted unit of
`common.stage.reading_acts` (an act or other reading of a page, or the one row
standing for a page with none) and one `continuation-link` per page break
either side's answer flag names (`pipeline/5_recensor/CONTRACT.md`). When the
run holds operator review decisions, each review a decision concerns carries
an `operator_review` block (`common.review_decisions`), an excluded unit's
review cites its decision as the envelope's `approval_ref`, and one
`review-decisions` record names every decision the pass applied, found stale or
could not keep. The Recensor writes them in the shapes named here, and the
Archetypus and the Armarium read them through this module, so no two stages can
disagree about them.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from common import page_edges
from common.contracts.approval import EDIT_DECISION
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import PERLECTOR, RECENSOR
from common.page_path import (
    DOUBT_MARKS_MALFORMED,
    ENTRY_NO_READABLE_TEXT,
    OPERATOR_REREAD_FIELD,
    PAGE_READING_KIND,
    PERLECTIO_KIND,
    READING_CLASS,
    UNPLACED,
    is_whole_page_reading,
)
from common.review_decisions import CORRECTION_FIELD, EXCLUDED, REVIEW_FIELD, decisions_digest
from common.review_policy import SEALED_CONFIG_NAME as REVIEW_CONFIG_NAME
from common.review_policy import load_review_policy, systemic
from common.sealed_config import require_sealed_config
from common.stage import (
    COUNTED_READING_CLASSES,
    NO_ACT_ON_PAGE_HOLD,
    PAGE_BLANK_HOLD,
    boundary_advanced,
    canary_ordinals,
    exemplar_page_ids,
    latest_attempt,
    real_page_entries,
    real_pages,
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
# The reading holds no operator decision overrides, each with why: an override
# exports the model's reading as read, and the export cannot carry these.
# `EDIT_CARRIES` are the ones a person's edit lifts (`override_refusal`).
NOT_OVERRIDABLE: Final = {
    UNPLACED: "the reading has no region on its page, so the export cannot cite where it is",
    DOUBT_MARKS_MALFORMED: (
        "its doubt marks could not be read, and the export delivers a reading only with a "
        "doubt report it can anchor"
    ),
    ENTRY_NO_READABLE_TEXT: (
        "it has no readable text, and an empty reading is exported only as a proved blank"
    ),
}
# An edit delivers the person's text, which carries no machine doubt layer, so
# neither a model reading with no text nor one whose doubt marks could not be read
# stops the export carrying it: the model's reading is shown beside it as it is,
# with its own recorded assessment. An unplaced reading still has no region to
# cite, whoever wrote its text.
EDIT_CARRIES: Final = frozenset({DOUBT_MARKS_MALFORMED, ENTRY_NO_READABLE_TEXT})
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
        "flag_codes",
        "review_priority",
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
# A held or flagged unit's place in the review queue, from the codes it carries:
# the lowest tier of any code wins. 1, look first: text or ink may be missing
# from the output. 2: structure doubt, entries split or joined wrongly, or a
# reading that may be cut short. 3: thin evidence, not a sign of loss. A code
# this table does not name is tier 1, so a new code is never buried.
REVIEW_PRIORITY_LOOK_FIRST: Final = 1
REVIEW_PRIORITY_STRUCTURE: Final = 2
REVIEW_PRIORITY_INFORMATIONAL: Final = 3
REVIEW_PRIORITY: Final[Mapping[str, int]] = {
    "page-unread": 1,
    "page-answer-incomplete": 1,
    "unknown-id": 1,
    "detection-range": 1,
    "reading-unplaced": 1,
    "unaccounted-witness-unit": 1,
    "set-aside-substantial": 1,
    "witness-text-not-read": 1,
    "unread-line": 1,
    "unread-ink": 1,
    "residual-ink": 1,
    "record-not-read": 1,
    "merged-detection": 1,
    "set-aside-record": 1,
    "reask-set-aside": 1,
    "reask-unread": 1,
    "reask-no-text": 1,
    "entry-no-readable-text": 1,
    "superseded-act-not-read": 1,
    "review-missed-act": 1,
    "review-hold": 1,
    "duplicate-region": 2,
    "shared-line": 2,
    "split-detection": 2,
    "record-read-as-other": 2,
    "reading-incomplete": 2,
    "truncation-not-classified": 2,
    "reask-unplaced": 2,
    "reask-duplicate": 2,
    "continuation-off-page-edge": 2,
    "doubt-marks-malformed": 2,
    "doubt-share-high": 2,
    "page-doubt-share-high": 2,
    "uncertainty-assessment-malformed": 2,
    "under-witnessed": 3,
    "unresolved-witness": 3,
    "witness-not-read": 3,
    "witness-read-no-units": 3,
    "witness-short-unit-not-read": 3,
    "no-detector-record-on-act-page": 3,
    "no-autopsia": 3,
    "unread-line-not-measured": 3,
    "witness-text-not-measured": 3,
    "unread-ink-not-measured": 3,
    "detector-records-not-measured": 3,
    "detector-record-not-measured": 3,
    "record-detector-capped": 3,
    "reask-duplicate-not-measured": 3,
    "residual-ink-not-measurable": 3,
    "residual-ink-not-measured": 3,
    "page-blank-unconfirmed": 3,
    "no-act-on-page-unconfirmed": 3,
}


def review_priority(codes: Collection[str]) -> int | None:
    """The review tier of a unit carrying `codes`, hold and flag codes alike; None with none."""
    if not codes:
        return None
    return min(REVIEW_PRIORITY.get(code, REVIEW_PRIORITY_LOOK_FIRST) for code in codes)


def review_flags(review: Mapping[str, Any]) -> list[str]:
    """The review flags a unit's review records without holding it."""
    return list(_payload(review)["flag_codes"])


# A review an operator decision concerns adds its `operator_review` block.
REVIEWED_PAGE_REVIEW_FIELDS: Final = PAGE_REVIEW_FIELDS | {REVIEW_FIELD}
RELEASE_FIELDS: Final = frozenset({"hold_codes", "reason"})
NOTE_FIELDS: Final = frozenset({"code", "flags"})

# The one record of what a Recensor pass did with the run's operator review
# decisions, published only when the run holds any.
REVIEW_DECISIONS_KIND: Final = "review-decisions"
REVIEW_DECISIONS_SCHEMA: Final = "recensor-review-decisions.v1"
REVIEW_DECISIONS_SUBJECT: Final = "operator-review"
REVIEW_DECISIONS_OPERATION: Final = "decide"
REVIEW_DECISIONS_FIELDS: Final = frozenset(
    {
        "schema",
        "decisions_digest",
        "applied",
        "stale",
        "conflicting",
        "carried",
        "unkept",
        "clearances",
        "corrections",
        "page_holds",
        "requests",
    }
)

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
    superseded = superseded_readings(context.tree)
    strays = sorted(
        subject
        for subject, records in by_subject.items()
        if subject not in counted
        and not all(of_superseded_reading(record, superseded) for record in records)
    )
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


def superseded_readings(tree) -> set[str]:
    """The run-tree paths of every page reading an operator re-read superseded.

    Each operator re-read names the page's earlier readings it supersedes
    (`common.page_path.operator_reread_record`); they stay in the run tree as
    read, and the Recensor's reviews of their units stay beside them, current
    no more.
    """
    found: set[str] = set()
    for entry in tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] != PAGE_READING_KIND:
            continue
        block = _payload(
            tree.read_artifact(PERLECTOR, PAGE_READING_KIND, entry["artifact_id"])
        ).get(OPERATOR_REREAD_FIELD)
        if isinstance(block, Mapping) and isinstance(block.get("supersedes"), list):
            found |= {
                reference["relative_path"]
                for reference in block["supersedes"]
                if isinstance(reference, Mapping)
                and isinstance(reference.get("relative_path"), str)
            }
    return found


def of_superseded_reading(review: Mapping[str, Any], superseded: Collection[str]) -> bool:
    """Whether a review is of a unit of a page reading an operator re-read superseded."""
    reference = _payload(review).get("page_reading_ref")
    return isinstance(reference, Mapping) and reference.get("relative_path") in superseded


def _current_reviews(tree) -> dict[str, dict[str, Any]]:
    """The Recensor's latest review of every unit of a current page reading, by subject."""
    reviews: dict[str, list[dict[str, Any]]] = {}
    for entry in tree.build_manifest(RECENSOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] == REVIEW_KIND:
            reviews.setdefault(entry["subject_id"], []).append(
                tree.read_artifact(RECENSOR, REVIEW_KIND, entry["artifact_id"])
            )
    superseded = superseded_readings(tree)
    current = {}
    for subject_id, records in sorted(reviews.items()):
        review = latest_attempt(records, f"review of {subject_id}", operation=REVIEW_OPERATION)
        if not of_superseded_reading(review, superseded):
            current[subject_id] = review
    return current


def published_units(tree) -> list[dict[str, Any]]:
    """The Recensor's latest review of every current unit, as `published_basis` reads them.

    Each unit's page is the one its page reading names, and its own and page
    holds its Perlectio's, as sealed; a page row has no reading and holds
    for its page. A unit of a reading an operator re-read superseded is not
    among them.
    """
    units = []
    for act_id, review in _current_reviews(tree).items():
        payload = {
            key: value for key, value in review["payload"].items() if key != "attempt_ordinal"
        }
        page = tree.read_artifact_reference(
            payload["page_reading_ref"], stage=PERLECTOR, kind=PAGE_READING_KIND
        )
        holds: list[str] = []
        page_holds: list[str] = []
        if payload["perlectio_ref"] is not None:
            reading = tree.read_artifact_reference(
                payload["perlectio_ref"], stage=PERLECTOR, kind=PERLECTIO_KIND, subject_id=act_id
            )
            holds, page_holds = reading["payload"]["holds"], reading["payload"]["page_holds"]
        units.append(
            {
                "act_id": act_id,
                "page_id": page["subject_id"],
                "outcome": review["outcome"],
                "payload": payload,
                "unit_holds": holds,
                "page_holds": page_holds,
            }
        )
    return units


def _require_review_of_row(review: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """The review is a page review of the row: its key, class, kind, page and records.

    Its reason, hold codes, notes and release must be in the page-review shape,
    and it is held exactly when it names a hold code.
    """
    payload = _payload(review)
    what = f"the Recensor review of {row['act_key']}"
    fields = PUBLISHED_PAGE_REVIEW_FIELDS | ({REVIEW_FIELD} if REVIEW_FIELD in payload else set())
    if set(payload) != fields:
        raise FatalAccounting(
            f"{what} is not the closed page-review shape: {sorted(set(payload) ^ fields)}"
        )
    # Only an operator decision excludes, and an excluded unit keeps its page's holds.
    excluded = review.get("outcome") == EXCLUDED
    if excluded and REVIEW_FIELD not in payload:
        raise FatalAccounting(f"{what} is excluded, but no operator review decided it")
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
    flags = payload["flag_codes"]
    if not _strings(flags) or payload["review_priority"] != review_priority(
        set(payload["hold_codes"]) | set(flags) if _strings(payload["hold_codes"]) else set(flags)
    ):
        raise FatalAccounting(
            f"{what} does not carry its review flags as codes with the review priority they "
            "and its hold codes give"
        )
    if (
        not isinstance(payload["reason"], str)
        or not _strings(payload["hold_codes"])
        or (not excluded and (review.get("outcome") == HELD) != bool(payload["hold_codes"]))
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
            "page-review shape, held exactly when it names a hold code unless excluded"
        )


def override_refusal(row: Mapping[str, Any], *, edit: bool = False) -> str | None:
    """Why no operator decision can send this held reading to export, or None when one can.

    An override exports the model's reading as read, so the reading must be
    one the export can carry: placed on its page, with a doubt report it can
    anchor and text to deliver. With `edit`, the person's text is delivered
    instead, so only the codes outside `EDIT_CARRIES` refuse it.
    """
    if row["perlectio_ref"] is None or row["class"] != READING_CLASS:
        return (
            f"{row['act_key']} is a {row['class']} row, which has no region on its page to export"
        )
    blocked = sorted(
        set(row["hold_codes"]) & set(NOT_OVERRIDABLE) - (EDIT_CARRIES if edit else set())
    )
    if blocked:
        return "; ".join(f"{code}: {NOT_OVERRIDABLE[code]}" for code in blocked)
    return None


def operator_correction(
    row: Mapping[str, Any],
    review: Mapping[str, Any],
    applied: Collection[str] | None = None,
) -> list[dict[str, Any]] | None:
    """The current edits that correct this unit's reading, or None when none does.

    An edit corrects the reading of an accepted review whose `operator_review`
    block names it as a current `edit` of this unit at the review's basis;
    `applied` as in `operator_override`, so an edit the Recensor's current
    `review-decisions` record did not apply corrects nothing. Several current
    edits of one unit name the same text and note, or the Recensor would have
    held it as conflicting (`common.review_decisions`). Returned in hash order.
    """
    payload = _payload(review)
    block = payload.get(REVIEW_FIELD)
    if review.get("outcome") != "accepted" or not isinstance(block, Mapping):
        return None
    try:
        decisions, basis = list(block["decisions"]), block["basis_digest"]
    except (KeyError, TypeError) as error:
        raise FatalAccounting(
            f"the operator review of {row['act_key']} has no decisions to rest on"
        ) from error
    edits = sorted(
        (
            dict(summary)
            for summary in decisions
            if isinstance(summary, Mapping)
            and summary.get("scope") == "unit"
            and summary.get("subject_id") == row["act_id"]
            and summary.get("decision") == EDIT_DECISION
            and summary.get("state") == "current"
            and summary.get("basis_digest") == basis
            and (applied is None or summary.get("decision_hash") in applied)
        ),
        key=lambda summary: summary["decision_hash"],
    )
    if len({summary.get(CORRECTION_FIELD) for summary in edits}) > 1:
        raise FatalAccounting(
            f"the operator review of {row['act_key']} applies edits that say different things"
        )
    return edits or None


def operator_override(
    row: Mapping[str, Any],
    review: Mapping[str, Any],
    applied: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """The current operator decisions that release a held reading to export, or None.

    A row's own hold codes (its Perlectio's `holds` and `page_holds`, and a
    page with no act) are overridden when its accepted review's
    `operator_review` block shows every one of them cleared: a unit-scope code
    by a current `release` of this unit at the review's basis (or, for a
    reading a person corrected, by the current `edit` of it,
    `operator_correction`), a page-scope one by a current `no-missed-act` of
    its page at the page basis. `applied` is the decision hashes the
    Recensor's current `review-decisions` record applied; a stage after the
    Recensor passes it, so a decision that record did not apply overrides
    nothing. Returns `{"codes", "decisions"}`: the codes overridden and the
    decision summaries that did it. Refused when the block claims an override
    no current decision makes, or of a reading that `override_refusal` says no
    decision may send to export.
    """
    payload = _payload(review)
    block = payload.get(REVIEW_FIELD)
    codes = sorted(set(row["hold_codes"]))
    if review.get("outcome") != "accepted" or not isinstance(block, Mapping) or not codes:
        return None
    what = f"the operator override of {row['act_key']}"
    try:
        cleared = {scope: set(block["cleared"][scope]) for scope in ("unit", "page")}
        decisions = list(block["decisions"])
    except (KeyError, TypeError) as error:
        raise FatalAccounting(f"{what} has no operator review block to rest on") from error
    if not set(codes) <= cleared["unit"] | cleared["page"]:
        return None
    edit = operator_correction(row, review, applied) is not None
    if (refusal := override_refusal(row, edit=edit)) is not None:
        raise FatalAccounting(f"{what} is refused: {refusal}")
    needed = []
    if set(codes) & cleared["unit"]:
        unit_decision = EDIT_DECISION if edit else "release"
        needed.append(("unit", row["act_id"], unit_decision, block["basis_digest"]))
    if set(codes) & cleared["page"]:
        needed.append(("page", row["page_id"], "no-missed-act", block["page_basis_digest"]))
    found = []
    for scope, subject, decision, basis in needed:
        matching = sorted(
            (
                summary
                for summary in decisions
                if isinstance(summary, Mapping)
                and summary.get("scope") == scope
                and summary.get("subject_id") == subject
                and summary.get("decision") == decision
                and summary.get("state") == "current"
                and summary.get("basis_digest") == basis
                and (applied is None or summary.get("decision_hash") in applied)
            ),
            key=lambda summary: summary["decision_hash"],
        )
        if not matching:
            raise FatalAccounting(
                f"{what} clears its {scope} holds with no current {decision} of its {scope} "
                "that the Recensor's review-decisions record applied"
            )
        found.extend(dict(summary) for summary in matching)
    return {"codes": codes, "decisions": found}


def require_establishable(
    row: Mapping[str, Any],
    review: Mapping[str, Any],
    applied: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """An accepted review stands over a reading the denominator reads, or releases its hold.

    A row with no reading has no text to establish. A held row may be accepted
    when every hold it carries is one a review may release and the review
    names exactly those codes in its `release`, or when current operator
    decisions override every one of them (`operator_override`, whose result is
    returned; `applied` as there). Otherwise a stage after the Recensor would
    be resurrecting a held reading.
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
        return None
    if release is None and (override := operator_override(row, review, applied)) is not None:
        return override
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
    return None


def reading_holds_allowed(reading: Mapping[str, Any], override: Mapping[str, Any] | None) -> bool:
    """A reading is establishable as read: unheld, or held only on codes an override cleared."""
    payload = reading.get("payload") or {}
    holds, page_holds = payload.get("holds"), payload.get("page_holds")
    if not isinstance(holds, list) or not isinstance(page_holds, list):
        return False
    if override is None:
        return reading.get("outcome") == "read" and not holds and not page_holds
    return reading.get("outcome") in ("read", "held") and set(holds) | set(page_holds) <= set(
        override["codes"]
    )


def applied_decision_hashes(decisions: Mapping[str, Any] | None) -> frozenset[str]:
    """The decisions a `review-decisions` record applied, by self-hash; none without a record."""
    if decisions is None:
        return frozenset()
    return frozenset(summary["decision_hash"] for summary in decisions["applied"])


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


def current_review_decisions(context) -> dict[str, Any] | None:
    """The payload of the Recensor's current `review-decisions` record, or None when it has none.

    Only a run that holds operator review decisions has one.
    """
    return _review_decisions_payload(
        [
            context.tree.read_artifact(RECENSOR, REVIEW_DECISIONS_KIND, entry["artifact_id"])
            for entry in stage_manifest(context, RECENSOR)["artifacts"]
            if entry["kind"] == REVIEW_DECISIONS_KIND
        ]
    )


def _review_decisions_payload(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The current one of the Recensor's `review-decisions` records, checked; None for none."""
    if not records:
        return None
    record = latest_attempt(
        records, "Recensor review-decisions record", operation=REVIEW_DECISIONS_OPERATION
    )
    payload = _payload(record)
    if (
        record.get("subject_id") != REVIEW_DECISIONS_SUBJECT
        or set(payload) != REVIEW_DECISIONS_FIELDS | {"attempt_ordinal"}
        or payload["schema"] != REVIEW_DECISIONS_SCHEMA
    ):
        raise FatalAccounting(
            f"Recensor review-decisions record {record.get('artifact_id')!r} is not a "
            f"{REVIEW_DECISIONS_SCHEMA} record"
        )
    return dict(payload)


def require_current_review_decisions(context) -> dict[str, Any] | None:
    """`current_review_decisions`, refused unless the pass behind it saw every decision stored now.

    A decision recorded after the Recensor's last pass is in no review, so a
    stage after the Recensor acting on that pass would silently ignore it. The
    stored set's digest (`common.review_decisions.decisions_digest`) must be
    the record's `decisions_digest`, and a run storing no decision must have
    no record.
    """
    recorded = current_review_decisions(context)
    stored = context.tree.review_decision_records()
    now = decisions_digest(reference.sha256 for reference, _record in stored) if stored else None
    seen = None if recorded is None else recorded["decisions_digest"]
    if now != seen:
        raise ApprovalRefusal(
            f"this run stores {len(stored)} operator review decision(s) and the Recensor's last "
            f"pass applied {'none' if recorded is None else 'another set'}; re-run the Recensor "
            "so every decision reaches its reviews before this stage runs"
        )
    return recorded


def held_by_recensor(tree) -> list[dict[str, Any]]:
    """Everything the Recensor's current records hold, read from the run tree alone.

    Each held unit's current review as `{subject_id, what, hold_codes}`, with
    `what` its unit key, then each held continuation link as `{subject_id,
    what: "continuation link", hold_codes: []}`, then each page the current
    `review-decisions` record still holds as `{subject_id: "operator-review",
    what: "page <ordinal>", hold_codes}`. A page hold stands even when every
    unit on the page was excluded, so it is counted by page. This is the same
    held total the Recensor exits held on, so a driver can tell a held
    Recensor from its records without opening a stage.
    """
    links: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for entry in tree.build_manifest(RECENSOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] == CONTINUATION_LINK_KIND and entry["outcome"] == HELD:
            links.append(
                {"subject_id": entry["subject_id"], "what": "continuation link", "hold_codes": []}
            )
        elif entry["kind"] == REVIEW_DECISIONS_KIND:
            decisions.append(
                tree.read_artifact(RECENSOR, REVIEW_DECISIONS_KIND, entry["artifact_id"])
            )
    held = []
    for subject_id, review in _current_reviews(tree).items():
        if review.get("outcome") == HELD:
            payload = _payload(review)
            held.append(
                {
                    "subject_id": subject_id,
                    "what": payload.get("act_key"),
                    "hold_codes": list(payload.get("hold_codes") or []),
                }
            )
    recorded = _review_decisions_payload(decisions)
    pages = [
        {
            "subject_id": REVIEW_DECISIONS_SUBJECT,
            "what": f"page {row['page_ordinal']}",
            "hold_codes": list(row["hold_codes"]),
        }
        for row in ([] if recorded is None else recorded["page_holds"])
    ]
    return held + sorted(links, key=lambda link: link["subject_id"]) + pages


def require_recensor_passed(tree) -> None:
    """Refuse a stage after the Recensor while the Recensor holds what no person has passed.

    A held Recensor (`held_by_recensor`) waits for a person: nothing is
    established or exported over it until nothing is held, or until a person's
    advance record binds the Recensor's current seal (`boundary_advanced`).
    """
    held = held_by_recensor(tree)
    if held and not boundary_advanced(tree, RECENSOR):
        raise ApprovalRefusal(
            f"the Recensor holds {len(held)} item(s) and no advance record passes its current "
            "seal; record review decisions and re-run the Recensor, or advance its seal with "
            "`verbatus advance --stage recensor`, before this stage runs"
        )


def held_pages_after_review(tree) -> tuple[list[int], int]:
    """The pages the Recensor's current records hold, and how many distinct pages it reviewed.

    The count is the distinct page ordinals of the Recensor's current `review`
    records, not the census: a page is counted when the Recensor reviewed a
    unit on it (a page with no reading is reviewed through its page row). That
    is the right denominator because only a reviewed page can be held after
    review, so the share compares held pages with the pages that could have
    been; a page the Recensor never reached (refused at the Door, say) is the
    census's to report, not a page the review passed. A page is held when any
    unit on it is held, or when the current `review-decisions` record still
    holds it (a page whose every unit was excluded keeps its page holds). A
    canary page is neither held nor counted: it is a control, not a page of
    the register. Read from the run tree alone, like `held_by_recensor`.
    """
    canaries = canary_ordinals(tree.read_run())
    decisions: list[dict[str, Any]] = []
    for entry in tree.build_manifest(RECENSOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] == REVIEW_DECISIONS_KIND:
            decisions.append(
                tree.read_artifact(RECENSOR, REVIEW_DECISIONS_KIND, entry["artifact_id"])
            )
    pages: set[int] = set()
    held: set[int] = set()
    for review in _current_reviews(tree).values():
        ordinal = _payload(review)["page_ordinal"]
        if ordinal in canaries:
            continue
        pages.add(ordinal)
        if review.get("outcome") == HELD:
            held.add(ordinal)
    if decisions:
        record = latest_attempt(
            decisions, "Recensor review-decisions record", operation=REVIEW_DECISIONS_OPERATION
        )
        held |= {row["page_ordinal"] for row in _payload(record)["page_holds"]} - canaries
    return sorted(held), len(pages)


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


def page_breaks(
    pages: Mapping[int, str], acts: Sequence[Mapping[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Every page break an answer flags, as `(subject, payload)`, in page order.

    The last whole-page `act` entry of page p and the first of page p+1
    (`common.page_edges.page_edges`) are the break's two sides; either side's
    flag records the break, `agreed` only when both say so, and a break whose
    sides disagree is still recorded. A side with no `act` entry (a page not
    read, blank, of `other` entries only, or outside the run) is null. The link holds no unit and joins nothing.
    """
    edges = page_edges.page_edges(page_edges.whole_page_entries(acts))
    ordinals = sorted(pages)
    links = []
    for left in range(ordinals[0] - 1, ordinals[-1] + 1):
        right = left + 1
        last = edges[left][1] if left in edges else None
        first = edges[right][0] if right in edges else None
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


def run_page_breaks(context, acts: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """`page_breaks` over the run's real pages: a canary page is in no page break.

    Canary pages are controls the Door appends after the real pages, so the
    last real page and the first canary sit at adjacent ordinals with nothing
    running between them. Leaving canaries out keeps a canary act from being
    either side of a link (and so from a join in the real export), and keeps a
    real run's breaks the same with or without canaries beside it.
    """
    pages = real_pages(context.run, exemplar_page_ids(context))
    return page_breaks(pages, real_page_entries(context.run, acts))


LINK_OPERATION: Final = "link"


def link_generations(tree, subject: str) -> list[dict[str, Any]]:
    """Every attempt of one page break's continuation-link, in order: 1, then each later one.

    A Recensor pass over readings an operator re-read changed files a link that
    differs from the last as the break's next attempt; the last is current.
    """
    found = []
    while True:
        identifier = artifact_id(
            RECENSOR,
            CONTINUATION_LINK_KIND,
            subject,
            attempt_id(subject, LINK_OPERATION, len(found) + 1),
        )
        if not tree.has_artifact(RECENSOR, CONTINUATION_LINK_KIND, identifier):
            return found
        found.append(tree.read_artifact(RECENSOR, CONTINUATION_LINK_KIND, identifier))


def current_link_records(tree) -> dict[str, dict[str, Any]]:
    """Each page break's current continuation-link record, by subject.

    The last attempt of each break, unless it is of readings an operator
    re-read superseded (a break the current readings no longer flag), which is
    kept as published and current no more. Every link record must be one of
    its break's attempts.
    """
    subjects: dict[str, int] = {}
    for entry in tree.build_manifest(RECENSOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] == CONTINUATION_LINK_KIND:
            subjects[entry["subject_id"]] = subjects.get(entry["subject_id"], 0) + 1
    superseded = superseded_readings(tree)
    current = {}
    for subject, count in sorted(subjects.items()):
        generations = link_generations(tree, subject)
        if len(generations) != count:
            raise FatalAccounting(
                f"the continuation-links of {subject} are not its attempts 1..{count}"
            )
        record = generations[-1]
        if not _of_superseded_inputs(tree, record, superseded):
            current[subject] = record
    return current


def _of_superseded_inputs(tree, record: Mapping[str, Any], superseded: Collection[str]) -> bool:
    """Whether a link names a reading, or a unit of one, an operator re-read superseded."""
    if not superseded:
        return False
    for reference in record.get("inputs", []):
        path = reference.get("relative_path") if isinstance(reference, Mapping) else None
        if path in superseded:
            return True
        named = _payload(json.loads(tree.read_bytes(path))).get("page_reading_ref")
        if isinstance(named, Mapping) and named.get("relative_path") in superseded:
            return True
    return False


def continuation_links(context, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every `continuation-link`, as `{ref, from_page_ordinal, to_page_ordinal,
    head_act_id, tail_act_id, agreed}`.

    One link per page break an answer flags, subject `page-break:<p>:<p+1>`:
    `head` (`from_act_id`) is the last `act` entry of page p and `tail`
    (`to_act_id`) the first of page p+1, either `None` where that side has no
    act entry. Each named side must be a counted row under its own key whose
    reading the link inputs; the link is `agreed` exactly when both flags say
    an act crosses the break, and is `accepted` exactly when it agrees. A break
    has one link, and each link is a break `run_page_breaks` derives from `rows`,
    its sides its pages' act edges; a flagged break with no link is named by
    the aggregate (`unpaired_continuations`) and keeps the run partial.
    """
    counted = {row["act_id"]: row for row in rows}
    derived = dict(run_page_breaks(context, rows))
    links: list[dict[str, Any]] = []
    subjects: set[str] = set()
    entries = {
        entry["artifact_id"]: entry
        for entry in stage_manifest(context, RECENSOR)["artifacts"]
        if entry["kind"] == CONTINUATION_LINK_KIND
    }
    for record in current_link_records(context.tree).values():
        entry = entries[record["artifact_id"]]
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
            if not is_whole_page_reading(row["reading_attempt"]):
                raise FatalAccounting(
                    f"{what} names {act_key}, an entry the re-ask recovered; a page's edges "
                    "are its current whole-page reading's, and a recovered entry is never a "
                    "side of a break"
                )
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


def held_share(tree, sealed: Mapping[str, str], review_config_path) -> dict[str, Any]:
    """The run's held share after the Recensor, measured against its sealed review policy.

    `{held_pages, pages, max_held_page_share, systemic}` (`held_pages_after_review`
    and `common.review_policy.systemic`). `sealed` is the run's sealed
    configuration digests; the policy at `review_config_path` is refused unless
    its bytes are the ones the run sealed, and a run that sealed none is refused.
    """
    policy = load_review_policy(review_config_path)
    require_sealed_config(sealed, REVIEW_CONFIG_NAME, policy["config_sha256"])
    held, pages = held_pages_after_review(tree)
    return {
        "held_pages": held,
        "pages": pages,
        "max_held_page_share": policy["max_held_page_share"],
        "systemic": bool(pages) and systemic(len(held), pages, policy),
    }
