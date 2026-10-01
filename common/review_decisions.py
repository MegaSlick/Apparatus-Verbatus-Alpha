"""Operator review decisions about held units, applied to the Recensor's own review.

A page-read run's Recensor holds a unit for review with every reason named
(`pipeline/5_recensor/page_review.py`). A person then looks at the held units
and records decisions as `approval-record.v1` (`common.contracts.approval`),
outside the sealed stage records. This module is how current decisions apply
on top of the Recensor's derived review, as pure functions over data: no I/O.

The Recensor's page path (`pipeline/5_recensor/page_review.py`) reads every
decision the run stores (`RunTree.review_decision_records`) and applies them
with `apply_decisions` on every pass, recording the result in its
`review-decisions` record; the Armarium hands that record's clearances and held
pages to `common.contracts.outcomes.run_aggregate` as `review_clearances`
(`aggregate_clearances`) and `review_page_holds` (`held_pages`). The record's
`decisions_digest` (`decisions_digest`) is what the Archetypus and the Armarium
compare with the decisions stored when they run, so neither acts on a pass that
did not see every decision.

The input is the derived review, the Recensor's review as the machine derives
it before any decision:

    {"run_id": str,
     "units": [{"act_id", "page_id", "outcome", "payload",
                "unit_holds": [...], "page_holds": [...]}]}

`payload` is `review_of`'s payload (no `attempt_ordinal`, no `operator_review`),
`unit_holds` the unit's own holds as its Perlectio records them and
`page_holds` its page accounting's holds (the Perlectio's `holds` and
`page_holds` fields, as sealed).

The API:

- `basis_digest(payload)`: what a unit decision binds to, the sha256 of the
  machine's payload. It is recomputed every pass, so a decision stays bound
  while nothing it looked at changes, and goes stale as soon as anything does.
  A page decision binds to `page_basis_digest`, over every unit on the page and
  the act entries current decisions exclude from it, so a page that gains or
  changes a unit, or whose exclusions change, makes it stale.
- `classify_holds`: a unit's hold codes split into `unit` and `page` scope. A
  page-scope hold is cleared only by a page decision, so releasing the entries
  one by one never clears the sign of an act the Perlector never listed.
- `current_basis(derived, decisions)`: every unit and page with its digest and
  scoped holds, its pages bound to the exclusions among `decisions`; what a
  new decision binds to.
- `review_decision(record, basis)`: one decision checked against the current
  basis: `current`, `stale` (with why), or refused when it could never apply.
  Its summary names the record by self-hash and by `record_sha256`, the digest
  the run tree stores it under, so an exclusion can cite its approval.
- `apply_decisions(derived, decisions)`: the review with every current
  decision applied, deterministically. It returns each unit's outcome and
  payload (a unit no decision names keeps the machine's, byte for byte), each
  page's remaining holds (`held_pages`), the applied, stale and conflicting
  decisions, a `decisions_digest` over the set it was given, the `clearances`
  the run aggregate names (`aggregate_clearances`), and the `requests` for a
  re-ask or re-shoot the driver acts on. A stale decision is kept inside the
  review of every unit it concerns; one whose page is gone is returned in
  `unkept` for the caller to record. Disagreeing current decisions about one
  subject are kept as conflicting and hold it; none is applied.

A stale decision never releases anything: a stale `release`, `exclude` or
`no-missed-act` clears no hold, excludes no act and is not counted among a
page's excluded acts, and a stale `re-ask` or `re-shoot` asks the driver for
nothing. A stale decision that holds (a unit `hold`, a page `missed-act` or a
page `hold`) still holds the subject it names while that subject is in the
review and no current decision about it has been recorded: the subject carries
a `-carried` hold code (`CARRIED_CODES`), listed in `carried`, until a person
records a decision about it against the current basis. So an exclusion
recorded after a missed act stales the missed act without losing the hold it
raised. A re-ask or re-shoot is not carried: the re-read it asks for changes
the basis by design, and the machine's own holds on the new reading stand.

Decisions: a unit is released (its own holds cleared), excluded as not an act,
held with a finding, or re-asked; an excluded unit's page keeps its page
holds, and a page whose every act is excluded is held as one with no act. A
page is found to have no missed act (its page-scope holds cleared), to have a
missed act, re-asked, re-shot, or held with a finding. Correcting text,
splitting, merging and clearing a continuation link are not decisions; each is
a `hold` finding and the unit stays held.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from common.contracts.approval import (
    PAGE_SCOPE,
    REVIEW_ACTION,
    UNIT_SCOPE,
    validate_approval_record,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, is_plain_int
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.page_path import NO_AUTOPSIA
from common.stage import (
    NO_ACT_ON_PAGE_HOLD,
    PAGE_BLANK_CLASS,
    PAGE_BLANK_HOLD,
    PAGE_UNREAD_CLASS,
    PAGE_UNREAD_HOLD,
    READING_CLASS,
    READING_UNPLACED_CLASS,
)

# The payload field an applied review adds; a unit no decision names has none.
REVIEW_FIELD: Final = "operator_review"
ACCEPTED: Final = "accepted"
HELD: Final = "held-for-review"
CONFIRMED_BLANK: Final = "confirmed-blank"
EXCLUDED: Final = "excluded"

READING_CLASSES: Final = frozenset({READING_CLASS, READING_UNPLACED_CLASS})
# A page-level row stands for its whole page, so all its holds are page scope.
PAGE_ROW_CLASSES: Final = frozenset({PAGE_UNREAD_CLASS, PAGE_BLANK_CLASS})
# The Recensor's own codes, by scope: a witness floor and residual ink are
# measured per page, an assessment and a continuation edge per entry.
RECENSOR_UNIT_CODES: Final = frozenset(
    {"uncertainty-assessment-malformed", "continuation-off-page-edge"}
)
RECENSOR_PAGE_CODES: Final = frozenset(
    {
        "under-witnessed",
        "unresolved-witness",
        "residual-ink",
        "residual-ink-not-measurable",
        "residual-ink-not-measured",
    }
)
ROW_PAGE_CODES: Final = frozenset({PAGE_UNREAD_HOLD, PAGE_BLANK_HOLD, NO_ACT_ON_PAGE_HOLD})
# Holds stage 4 puts on every entry of a page for a fact about the whole page:
# read without its image, the page may hold acts no entry lists.
PAGE_WIDE_ENTRY_CODES: Final = frozenset({NO_AUTOPSIA})

CURRENT: Final = "current"
STALE: Final = "stale"
# Current decisions about one subject that disagree: kept, never applied.
CONFLICTING: Final = "conflicting"
CONFLICT: Final = "conflict"
BASIS_CHANGED: Final = "basis-changed"
SUBJECT_ABSENT: Final = "subject-absent"

# The hold code each holding decision adds, by scope.
ADDED_CODES: Final = {
    (UNIT_SCOPE, "hold"): "review-hold",
    (UNIT_SCOPE, "re-ask"): "review-reask",
    (PAGE_SCOPE, "missed-act"): "review-missed-act",
    (PAGE_SCOPE, "re-ask"): "review-page-reask",
    (PAGE_SCOPE, "re-shoot"): "review-reshoot",
    (PAGE_SCOPE, "hold"): "review-page-hold",
    (UNIT_SCOPE, CONFLICT): "review-conflict",
    (PAGE_SCOPE, CONFLICT): "review-page-conflict",
}
# The code a unit keeps when decisions would accept a reading its page reading
# holds: a decision clears only the Recensor's own holds, never the reading's.
READING_HELD: Final = "review-reading-held"
CLEARING: Final = frozenset({(UNIT_SCOPE, "release"), (UNIT_SCOPE, "exclude")}) | {
    (PAGE_SCOPE, "no-missed-act")
}
REQUESTS: Final = frozenset({"re-ask", "re-shoot"})
# The hold code a stale holding decision carries onto its subject, by scope.
CARRIED_CODES: Final = {
    (UNIT_SCOPE, "hold"): "review-hold-carried",
    (PAGE_SCOPE, "missed-act"): "review-missed-act-carried",
    (PAGE_SCOPE, "hold"): "review-page-hold-carried",
}


_UNIT_FIELDS: Final = frozenset(
    {"act_id", "page_id", "outcome", "payload", "unit_holds", "page_holds"}
)


# --- the basis ------------------------------------------------------------------------


def basis_digest(payload: Mapping[str, Any]) -> str:
    """The sha256 a unit decision binds to: the machine's review payload, before any decision."""
    if not isinstance(payload, Mapping):
        raise FatalAccounting("a review basis is not a payload object")
    if REVIEW_FIELD in payload or "attempt_ordinal" in payload:
        raise FatalAccounting(
            "a review basis is the payload the machine derives before any decision is "
            "applied or any attempt stamped; this one already carries one"
        )
    return digest_of(dict(payload))


def page_basis_digest(
    page_id: str, unit_digests: Mapping[str, str], excluded_acts: Iterable[str]
) -> str:
    """The sha256 a page decision binds to: its page, every unit basis on it, and its excluded acts.

    Excluding every act holds the page as one with no act, so a page decision
    made before an exclusion has not looked at the page it would now clear.
    """
    return digest_of(
        {"page_id": page_id, "units": dict(unit_digests), "excluded_acts": sorted(excluded_acts)}
    )


def classify_holds(
    hold_codes: Iterable[str],
    *,
    unit_class: str,
    unit_holds: Iterable[str],
    page_holds: Iterable[str],
) -> dict[str, list[str]]:
    """A unit's hold codes by scope, `{"unit": [...], "page": [...]}`, each sorted.

    A page-level row's codes are all page scope. On an entry, a code is unit
    scope when the entry's own reading holds it or the Recensor measures it per
    entry, and page scope when the page accounting holds it, the Recensor
    measures it per page, or stage 4 holds every entry for a fact about the
    whole page; a code both hold is in both, and needs both a unit and a page
    decision to clear. `unit_holds` and `page_holds` are the unit's Perlectio
    `holds` and `page_holds` fields, as sealed. A code in neither is refused, since a hold
    of unknown scope could be cleared by the wrong decision.
    """
    codes = set(hold_codes)
    if type(unit_class) is not str:
        raise FatalAccounting(f"a review names unit class {unit_class!r}")
    if unit_class in PAGE_ROW_CLASSES:
        return {"unit": [], "page": sorted(codes)}
    if unit_class not in READING_CLASSES:
        raise FatalAccounting(f"a review names unit class {unit_class!r}")
    unit = codes & (set(unit_holds) | RECENSOR_UNIT_CODES) - PAGE_WIDE_ENTRY_CODES
    page = codes & (set(page_holds) | RECENSOR_PAGE_CODES | ROW_PAGE_CODES | PAGE_WIDE_ENTRY_CODES)
    if unknown := sorted(codes - unit - page):
        raise FatalAccounting(
            f"hold code(s) {unknown} are neither the entry's own nor its page's, so no "
            "decision can be bound to their scope"
        )
    return {"unit": sorted(unit), "page": sorted(page)}


def current_basis(derived: Mapping[str, Any], decisions: Sequence[Any] = ()) -> dict[str, Any]:
    """Every unit and page of a derived review, with its basis digest and scoped holds.

    `{"run_id", "units": {act_id: {page_id, page_ordinal, act_key, unit_class,
    kind, page_holds, outcome, basis_digest, unit_codes, page_codes}}, "pages":
    {page_id: {page_ordinal, page_holds, basis_digest, units, act_units,
    excluded_acts, page_codes}}}`. A page's codes are the union of its units'
    page-scope codes; its units must name the same page accounting holds. A
    page's `excluded_acts` are its act entries the current unit decisions
    among `decisions` exclude, and its digest binds them.
    """
    run_id = derived.get("run_id") if isinstance(derived, Mapping) else None
    units_in = derived.get("units") if isinstance(derived, Mapping) else None
    if type(run_id) is not str or not run_id or not isinstance(units_in, list):
        raise FatalAccounting("a derived review names no run and no list of units")
    return _basis(run_id, [(unit["act_id"], _unit_basis(unit)) for unit in units_in], decisions)


def published_basis(
    run_id: str, published: Sequence[Mapping[str, Any]], decisions: Sequence[Any] = ()
) -> dict[str, Any]:
    """`current_basis` read back from the reviews a Recensor pass published.

    Each of `published` is a derived unit whose `payload` is the published
    review's, `attempt_ordinal` removed. A review no decision concerns is the
    machine's own, so it is its basis; one a decision concerns keeps the
    machine's basis, outcome and scoped codes in its `operator_review` block.
    So a person can bind a decision to the review they read, without running
    the stage, and the Recensor's next pass finds it current while nothing it
    looked at changes.
    """
    if type(run_id) is not str or not run_id:
        raise FatalAccounting("published reviews name no run")
    entries = []
    for unit in published:
        payload = unit["payload"] if isinstance(unit, Mapping) else None
        block = payload.get(REVIEW_FIELD) if isinstance(payload, Mapping) else None
        if block is None:
            entries.append((unit["act_id"], _unit_basis(unit)))
            continue
        entries.append(
            (
                unit["act_id"],
                {
                    "page_id": unit["page_id"],
                    "page_ordinal": payload["page_ordinal"],
                    "act_key": payload["act_key"],
                    "unit_class": payload["unit_class"],
                    "outcome": block["machine_outcome"],
                    "basis_digest": block["basis_digest"],
                    "kind": payload.get("kind"),
                    "page_holds": sorted(set(unit["page_holds"])),
                    "unit_codes": list(block["scopes"]["unit"]),
                    "page_codes": list(block["scopes"]["page"]),
                },
            )
        )
    return _basis(run_id, entries, decisions)


def _basis(
    run_id: str, entries: Sequence[tuple[str, dict[str, Any]]], decisions: Sequence[Any]
) -> dict[str, Any]:
    """The basis over each unit's entry: its pages, their exclusions and digests."""
    units: dict[str, dict[str, Any]] = {}
    for act_id, entry in entries:
        if act_id in units:
            raise FatalAccounting(f"a derived review names unit {act_id!r} twice")
        units[act_id] = entry
    pages: dict[str, dict[str, Any]] = {}
    for act_id in sorted(units):
        entry = units[act_id]
        page = pages.setdefault(
            entry["page_id"],
            {
                "page_ordinal": entry["page_ordinal"],
                "page_holds": entry["page_holds"],
                "units": [],
                "act_units": [],
                "page_codes": [],
            },
        )
        if page["page_ordinal"] != entry["page_ordinal"]:
            raise FatalAccounting(f"page {entry['page_id']!r} is named with two ordinals")
        # One page accounting holds every entry of a page alike.
        if page["page_holds"] != entry["page_holds"]:
            raise FatalAccounting(
                f"the units of page {entry['page_id']!r} name different page accounting holds"
            )
        page["units"].append(act_id)
        if entry["kind"] == "act" and entry["unit_class"] in READING_CLASSES:
            page["act_units"].append(act_id)
        page["page_codes"] = sorted(set(page["page_codes"]) | set(entry["page_codes"]))
    basis = {"run_id": run_id, "units": units, "pages": pages}
    excluded = _excluded_units(decisions, basis)
    for page_id, page in pages.items():
        page["excluded_acts"] = [act_id for act_id in page["act_units"] if act_id in excluded]
        page["basis_digest"] = page_basis_digest(
            page_id,
            {act_id: units[act_id]["basis_digest"] for act_id in page["units"]},
            page["excluded_acts"],
        )
    return basis


def _excluded_units(decisions: Sequence[Any], basis: Mapping[str, Any]) -> set[str]:
    """The units the current unit decisions exclude; a unit decision binds no page digest."""
    summaries = []
    for record in decisions:
        record = validate_approval_record(record)
        if record["action"] == REVIEW_ACTION and record["review"]["scope"] == UNIT_SCOPE:
            summaries.append(review_decision(record, basis))
    kinds = _current_kind_by_subject(summaries)
    return {subject for (_, subject), kind in kinds.items() if kind == "exclude"}


def _unit_basis(unit: Any) -> dict[str, Any]:
    if not isinstance(unit, Mapping) or set(unit) != _UNIT_FIELDS:
        raise FatalAccounting(f"a derived review unit is not the closed {sorted(_UNIT_FIELDS)}")
    act_id, page_id, outcome, payload = (
        unit["act_id"],
        unit["page_id"],
        unit["outcome"],
        unit["payload"],
    )
    if not all(type(value) is str and value for value in (act_id, page_id)):
        raise FatalAccounting("a derived review unit names no act id or page id")
    if not isinstance(payload, Mapping):
        raise FatalAccounting(f"unit {act_id!r} has no review payload")
    hold_codes = payload.get("hold_codes")
    ordinal = payload.get("page_ordinal")
    if (
        not _strings(hold_codes)
        or not _strings(unit["unit_holds"])
        or not _strings(unit["page_holds"])
    ):
        raise FatalAccounting(f"unit {act_id!r} names its holds as something other than codes")
    if not is_plain_int(ordinal) or type(payload.get("act_key")) is not str:
        raise FatalAccounting(f"unit {act_id!r} names no page ordinal or unit key")
    scopes = classify_holds(
        hold_codes,
        unit_class=payload.get("unit_class"),
        unit_holds=unit["unit_holds"],
        page_holds=unit["page_holds"],
    )
    if outcome != _derived_outcome(hold_codes, payload["unit_class"]):
        raise FatalAccounting(
            f"unit {act_id!r} is {outcome!r} with hold codes {hold_codes}; a machine "
            "review is held exactly when a code holds it"
        )
    return {
        "page_id": page_id,
        "page_ordinal": ordinal,
        "act_key": payload["act_key"],
        "unit_class": payload["unit_class"],
        "outcome": outcome,
        "basis_digest": basis_digest(payload),
        "kind": payload.get("kind"),
        "page_holds": sorted(set(unit["page_holds"])),
        "unit_codes": scopes["unit"],
        "page_codes": scopes["page"],
    }


def _derived_outcome(hold_codes: Sequence[str], unit_class: str) -> str:
    if hold_codes:
        return HELD
    return CONFIRMED_BLANK if unit_class == PAGE_BLANK_CLASS else ACCEPTED


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(type(item) is str and item for item in value)


# --- one decision -----------------------------------------------------------------------


def review_decision(record: Any, basis: Mapping[str, Any]) -> dict[str, Any]:
    """One decision checked against the current basis, as the summary a review keeps.

    `state` is `current` when its subject is in the review and its basis digest
    is the subject's now, and `stale` otherwise, with `stale_because` naming
    `subject-absent` or `basis-changed`. A decision that is not a sound
    approval-record.v1 review, names another run, or that its bound subject
    does not allow (a unit decision about a page-level row, a release with no
    unit hold to clear, no missed act on an unread page) is refused.
    """
    record = validate_approval_record(record)
    if record["action"] != REVIEW_ACTION:
        raise ApprovalRefusal(f"a {record['action']!r} approval is not a review decision")
    review = record["review"]
    if review["run_id"] != basis["run_id"]:
        raise ApprovalRefusal(
            f"a review decision for run {review['run_id']!r} was offered to run {basis['run_id']!r}"
        )
    scope, subject, page_id = review["scope"], record["subject_ids"][0], review["page_id"]
    summary = {
        "decision_hash": record["self_hash"],
        # The digest the run tree stores the record under, for citing it.
        "record_sha256": digest_bytes(canonical_bytes(record)),
        "scope": scope,
        "subject_id": subject,
        "page_id": page_id,
        "decision": review["decision"],
        "finding": review["finding"],
        "basis_digest": record["target_version_hash"],
        "reason": record["reason"],
        "state": CURRENT,
        "stale_because": None,
    }
    found = (basis["units"] if scope == UNIT_SCOPE else basis["pages"]).get(subject)
    if found is None:
        return {**summary, "state": STALE, "stale_because": SUBJECT_ABSENT}
    if scope == UNIT_SCOPE and found["page_id"] != page_id:
        raise ApprovalRefusal(f"a decision about unit {subject!r} names another page")
    if found["basis_digest"] != summary["basis_digest"]:
        return {**summary, "state": STALE, "stale_because": BASIS_CHANGED}
    _require_allowed(summary, found)
    return summary


def _require_allowed(summary: Mapping[str, Any], found: Mapping[str, Any]) -> None:
    """Refuse a bound decision its subject's current state does not allow."""
    what = f"{summary['decision']} of {summary['scope']} {summary['subject_id']!r}"
    if summary["scope"] == UNIT_SCOPE:
        if found["unit_class"] not in READING_CLASSES:
            raise ApprovalRefusal(
                f"a {what}: a page-level row stands for its page and takes page decisions"
            )
        if summary["decision"] == "release" and not found["unit_codes"]:
            raise ApprovalRefusal(f"a {what}: the unit has no hold of its own to release")
    elif summary["decision"] == "no-missed-act":
        if PAGE_UNREAD_HOLD in found["page_codes"]:
            raise ApprovalRefusal(
                f"a {what}: the page was never read, so nothing shows whether an act is on it"
            )


# --- applying decisions ------------------------------------------------------------------


def apply_decisions(derived: Mapping[str, Any], decisions: Sequence[Any]) -> dict[str, Any]:
    """The derived review with every current decision applied, and every stale one kept.

    Deterministic in the decision set: order and repeats do not matter, since
    a record is known by its self-hash. Current decisions about one subject
    that decide differently are never chosen between: none is applied, each is
    kept as `conflicting`, and the subject is held until they agree.
    """
    basis = current_basis(derived, decisions)
    by_hash: dict[str, dict[str, Any]] = {}
    for record in decisions:
        summary = review_decision(record, basis)
        by_hash[summary["decision_hash"]] = summary
    kinds = _current_kind_by_subject(by_hash.values())
    summaries = sorted(
        (_with_conflict(summary, kinds) for summary in by_hash.values()), key=_summary_key
    )

    pages = {
        page_id: _page_result(page_id, page, summaries, kinds)
        for page_id, page in sorted(basis["pages"].items())
    }
    units_in = {unit["act_id"]: unit for unit in derived["units"]}
    units = {
        act_id: _unit_result(
            act_id, units_in[act_id], entry, pages[entry["page_id"]], summaries, kinds
        )
        for act_id, entry in sorted(basis["units"].items())
    }
    applied = [summary for summary in summaries if summary["state"] == CURRENT]
    stale = [summary for summary in summaries if summary["state"] == STALE]
    carried = [s for s in stale if _carried_code(s, basis, kinds) is not None]
    return {
        "decisions_digest": decisions_digest(s["record_sha256"] for s in summaries),
        "units": units,
        "pages": pages,
        "applied": applied,
        "stale": stale,
        "conflicting": [s for s in summaries if s["state"] == CONFLICTING],
        "carried": carried,
        "unkept": [summary for summary in stale if summary["page_id"] not in basis["pages"]],
        "clearances": _clearances(basis, units, pages, applied),
        "requests": _requests(basis, applied),
    }


def decisions_digest(record_sha256s: Iterable[str]) -> str:
    """The digest of a set of decisions, from the digests the run tree stores each one under."""
    return digest_of(sorted(set(record_sha256s)))


def _summary_key(summary: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        summary["scope"],
        summary["subject_id"],
        summary["decision"],
        summary["decision_hash"],
    )


def _current_kind_by_subject(summaries: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """The one decision each subject's current decisions make, or `conflict` when they differ."""
    kinds: dict[tuple[str, str], set[str]] = {}
    for summary in summaries:
        if summary["state"] == CURRENT:
            key = (summary["scope"], summary["subject_id"])
            kinds.setdefault(key, set()).add(summary["decision"])
    return {key: next(iter(found)) if len(found) == 1 else CONFLICT for key, found in kinds.items()}


def _with_conflict(summary: dict[str, Any], kinds: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    if summary["state"] == CURRENT and kinds[(summary["scope"], summary["subject_id"])] == CONFLICT:
        return {**summary, "state": CONFLICTING}
    return summary


def _carried_code(
    summary: Mapping[str, Any],
    basis: Mapping[str, Any],
    kinds: Mapping[tuple[str, str], str],
) -> str | None:
    """The hold a stale holding decision still puts on its subject, or None.

    Its subject must still be in the review, so the hold has something to
    hold, and no current decision about it may be recorded: a person who
    records one has decided against the current basis.
    """
    scope, subject = summary["scope"], summary["subject_id"]
    if summary["state"] != STALE or (scope, subject) in kinds:
        return None
    if subject not in (basis["units"] if scope == UNIT_SCOPE else basis["pages"]):
        return None
    return CARRIED_CODES.get((scope, summary["decision"]))


def _carried(
    scope: str,
    subject: str,
    summaries: Iterable[Mapping[str, Any]],
    kinds: Mapping[tuple[str, str], str],
) -> list[str]:
    """The hold codes stale holding decisions carry onto a subject that is in the review."""
    if (scope, subject) in kinds:
        return []
    return sorted(
        {
            CARRIED_CODES[(scope, s["decision"])]
            for s in summaries
            if s["state"] == STALE
            and s["scope"] == scope
            and s["subject_id"] == subject
            and (scope, s["decision"]) in CARRIED_CODES
        }
    )


def _concerns_page(summary: Mapping[str, Any], page_id: str, units: Iterable[str]) -> bool:
    """A page decision about this page, or a unit decision on it whose unit is gone."""
    if summary["page_id"] != page_id:
        return False
    return summary["scope"] == PAGE_SCOPE or summary["subject_id"] not in units


def _page_result(
    page_id: str,
    page: Mapping[str, Any],
    summaries: list[dict[str, Any]],
    kinds: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    """A page's holds after review: its page-scope codes, less what was cleared, plus added ones.

    A page whose every `act` entry an operator excluded holds its other
    entries as the machine does a page with no act, until a no-missed-act
    decision confirms none of them is one. A stale missed-act or page hold
    still holds the page, carried, while no current page decision is recorded.
    """
    kind = kinds.get((PAGE_SCOPE, page_id))
    codes = set(page["page_codes"])
    acts = page["act_units"]
    if acts and page["excluded_acts"] == acts:
        codes.add(NO_ACT_ON_PAGE_HOLD)
    cleared = sorted(codes) if kind == "no-missed-act" else []
    added = [ADDED_CODES[(PAGE_SCOPE, kind)]] if (PAGE_SCOPE, kind) in ADDED_CODES else []
    carried = _carried(PAGE_SCOPE, page_id, summaries, kinds)
    hold_codes = sorted((codes - set(cleared)) | set(added) | set(carried))
    return {
        "page_ordinal": page["page_ordinal"],
        "basis_digest": page["basis_digest"],
        "decision": kind,
        "hold_codes": hold_codes,
        "cleared": cleared,
        "added": added,
        "carried": carried,
        "raised": sorted(set(hold_codes) - set(page["page_codes"])),
        "decisions": [s for s in summaries if _concerns_page(s, page_id, page["units"])],
    }


def _unit_result(
    act_id: str,
    unit: Mapping[str, Any],
    entry: Mapping[str, Any],
    page: Mapping[str, Any],
    summaries: list[dict[str, Any]],
    kinds: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    own = [s for s in summaries if s["scope"] == UNIT_SCOPE and s["subject_id"] == act_id]
    touching = sorted(own + page["decisions"], key=_summary_key)
    machine = copy.deepcopy(dict(unit["payload"]))
    if not touching and not page["raised"]:
        return {"outcome": unit["outcome"], "payload": machine}
    kind = kinds.get((UNIT_SCOPE, act_id))
    cleared_unit = list(entry["unit_codes"]) if (UNIT_SCOPE, kind) in CLEARING else []
    cleared_page = sorted(set(entry["page_codes"]) & set(page["cleared"]))
    added = sorted(
        ([ADDED_CODES[(UNIT_SCOPE, kind)]] if (UNIT_SCOPE, kind) in ADDED_CODES else [])
        + page["added"]
    )
    # The page's holds this unit carries: its own page-scope codes, less what a
    # page decision cleared, and any the review raised on the page.
    page_held = (set(entry["page_codes"]) - set(page["cleared"])) | set(page["raised"])
    if kind == "exclude":
        page_held -= {NO_ACT_ON_PAGE_HOLD}
    carried = sorted(set(_carried(UNIT_SCOPE, act_id, own, kinds)) | set(page["carried"]))
    hold_codes = sorted(
        (set(entry["unit_codes"]) - set(cleared_unit)) | page_held | set(added) | set(carried)
    )
    if kind == "exclude":
        outcome = EXCLUDED
    else:
        outcome = _derived_outcome(hold_codes, entry["unit_class"])
    findings = sorted(
        {s["finding"] for s in touching if s["state"] == CURRENT and s["finding"] is not None}
    )
    block = {
        "basis_digest": entry["basis_digest"],
        "page_basis_digest": page["basis_digest"],
        "machine_outcome": unit["outcome"],
        "machine_hold_codes": list(machine["hold_codes"]),
        "scopes": {"unit": list(entry["unit_codes"]), "page": list(entry["page_codes"])},
        "cleared": {"unit": cleared_unit, "page": cleared_page},
        "added": added,
        "carried": carried,
        "findings": findings,
        "decisions": touching,
    }
    reason = _reason(
        machine["reason"],
        kind=kind,
        ordinal=entry["page_ordinal"],
        cleared_unit=cleared_unit,
        cleared_page=cleared_page,
        added=added,
        findings=findings,
        stale=sum(s["state"] == STALE for s in touching),
        conflicting=sum(s["state"] == CONFLICTING for s in touching),
        carried=carried,
        still=sorted(set(hold_codes) - set(added) - set(carried)),
    )
    return {
        "outcome": outcome,
        "payload": {**machine, "hold_codes": hold_codes, "reason": reason, REVIEW_FIELD: block},
    }


def _reason(
    machine_reason: str,
    *,
    kind: str | None,
    ordinal: int,
    cleared_unit: list[str],
    cleared_page: list[str],
    added: list[str],
    findings: list[str],
    stale: int,
    conflicting: int,
    carried: list[str],
    still: list[str],
) -> str:
    """A reviewed unit's reason: what the review did, then what the machine found."""
    parts = []
    if kind == "exclude":
        parts.append("operator review excluded it as not an act")
    elif cleared_unit:
        parts.append(f"operator review released its own holds ({', '.join(cleared_unit)})")
    if cleared_page:
        parts.append(
            f"operator review found no missed act on page {ordinal}, clearing "
            f"{', '.join(cleared_page)}"
        )
    if conflicting:
        parts.append(
            f"{conflicting} current operator decision(s) disagree, so none is applied and it "
            "is held until they agree"
        )
    if added and kind != "exclude":
        parts.append(
            f"operator review holds it ({', '.join(added)})"
            + (f" with finding(s) {', '.join(findings)}" if findings else "")
        )
    if stale:
        parts.append(
            f"{stale} operator decision(s) are stale, bound to facts that have since changed, "
            "and release nothing"
        )
    if carried and kind != "exclude":
        parts.append(
            f"held by {', '.join(carried)}, carried from stale operator decision(s) until a "
            "person records a decision against the current basis"
        )
    if kind == "exclude" and (still or added or carried):
        held = sorted(set(still) | set(added) | set(carried))
        parts.append(f"its page stays held by {', '.join(held)}")
    elif still:
        parts.append(f"still held by {', '.join(still)}")
    if not parts:
        parts.append(f"operator review decided about page {ordinal}")
    return "; ".join(parts) + f" (the machine's review: {machine_reason})"


def _clearances(
    basis: Mapping[str, Any],
    units: Mapping[str, Mapping[str, Any]],
    pages: Mapping[str, Mapping[str, Any]],
    applied: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Each subject a clearing decision cleared, with the codes it cleared."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for summary in applied:
        scope, subject = summary["scope"], summary["subject_id"]
        if (scope, summary["decision"]) not in CLEARING:
            continue
        row = rows.get((scope, subject))
        if row is None:
            if scope == UNIT_SCOPE:
                entry = basis["units"][subject]
                cleared = units[subject]["payload"][REVIEW_FIELD]["cleared"]["unit"]
                act_key, ordinal = entry["act_key"], entry["page_ordinal"]
            else:
                cleared = pages[subject]["cleared"]
                act_key, ordinal = None, pages[subject]["page_ordinal"]
            row = rows[(scope, subject)] = {
                "scope": scope,
                "subject_id": subject,
                "act_key": act_key,
                "page_id": summary["page_id"],
                "page_ordinal": ordinal,
                "decision": summary["decision"],
                "cleared": list(cleared),
                "decision_hashes": [],
            }
        row["decision_hashes"].append(summary["decision_hash"])
    return [rows[key] for key in sorted(rows)]


def _requests(basis: Mapping[str, Any], applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each re-ask or re-shoot the current decisions ask the driver for."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for summary in applied:
        if summary["decision"] not in REQUESTS:
            continue
        scope, subject = summary["scope"], summary["subject_id"]
        found = (basis["units"] if scope == UNIT_SCOPE else basis["pages"])[subject]
        row = rows.setdefault(
            (scope, subject),
            {
                "scope": scope,
                "subject_id": subject,
                "page_id": summary["page_id"],
                "page_ordinal": found["page_ordinal"],
                "decision": summary["decision"],
                "decision_hashes": [],
            },
        )
        row["decision_hashes"].append(summary["decision_hash"])
    return [rows[key] for key in sorted(rows)]


def aggregate_clearances(result: Mapping[str, Any], *, unit_key: str) -> list[dict[str, Any]]:
    """`apply_decisions`' clearances as `run_aggregate`'s `review_clearances` rows.

    `unit_key` names what the aggregate calls a unit, `act_id` or `act_key`;
    every row names its page by ordinal, and a page row is its ordinal.
    """
    if unit_key not in ("act_id", "act_key"):
        raise FatalAccounting(f"a unit is named by act_id or act_key, not {unit_key!r}")
    field = "subject_id" if unit_key == "act_id" else "act_key"
    return [
        {
            "scope": row["scope"],
            "subject": row[field] if row["scope"] == UNIT_SCOPE else row["page_ordinal"],
            "page": row["page_ordinal"],
            "decision": row["decision"],
            "cleared": list(row["cleared"]),
        }
        for row in result["clearances"]
    ]


def held_pages(result: Mapping[str, Any]) -> dict[int, list[str]]:
    """Each page still held once decisions are applied, by ordinal, with its page-scope codes.

    `run_aggregate`'s `review_page_holds`: a page hold stands even when every
    unit on the page was excluded, so it is reported by page, not only by unit.
    A page no decision touched is listed too, with its machine holds.
    """
    return {
        page["page_ordinal"]: list(page["hold_codes"])
        for page in result["pages"].values()
        if page["hold_codes"]
    }
