"""The Recensor's page path: one review for every unit a page-read run counts.

The Perlector read each sealed page whole and established the acts on it (`pipeline/4_perlector/CONTRACT.md`,
"Page reading"). The units counted here are `common.stage.reading_acts`, whose
rows the shared denominator has already proven from the Perlector's records,
page accounting and hold codes included. On top of each row this stage adds
what it measures itself:

- the witness floor, from each configured page witness's latest
  `page-testimonium` for the row's page (every unit on a page shares it);
- residual ink, from the sealed page's own pixels against every reading region
  cut on it, `act` and `other` alike;
- for a page the reading says holds no act, whether that is confirmed;
- the answer's continuation flags: every page break a flag on an `act` entry
  at its page's act edge names is recorded as a `continuation-link` between
  `act` entries, held when only one side says the text runs across; a flag on
  an `act` entry off that edge holds the entry, and a flag on an `other` entry
  is a note in its review. A page's edges are its current whole-page
  reading's (its first, or the operator re-read that superseded it): an entry
  the Perlector's re-ask recovered never moves them and is never a side.

A unit is `accepted` only when its row is `read`, the floor holds, no chair is
unresolved and the page's ink is covered; every other unit is `held-for-review`
with every reason named. A finding whose code the sealed `[flags]` policy
names (`common.page_accounting`, the row's `flag_codes` and this stage's
`residual-ink`) is a review flag: recorded in `flag_codes`, named in the
reason, placed in the queue by `review_priority`, holding nothing. After the
receipt the pass writes `run-health/recensor-review-summary.json`
(`review_summary`): holds and flags per page and per unit, by code and by page
type, and the queue. Nothing here reads, repairs or chooses text, and
nothing asks for a recovery: `recoveries_used` is the page's re-asks, which
stage 4 planned itself (0 or 1, from the page's row), and the receipt binds
each page's re-ask and what it did.

The run's operator review decisions (`approval-record.v1` under
`receipts/sha256/`) apply on top of the machine's reviews
(`common.review_decisions.apply_decisions`): a current decision clears or adds
the holds it names, a stale one releases nothing and keeps any hold it raised,
and an excluded unit's review cites its decision. The pass records what it
applied, found stale or could not keep in one `review-decisions` record. A run
with no decision publishes exactly what the machine derives.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from common import page_accounting, page_edges
from common.contracts.approval import PAGE_SCOPE, UNIT_SCOPE
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.outcomes import (
    WITNESS_READING_OUTCOMES,
    OutcomeClass,
    classify,
    witness_coverage,
    witnessed_count,
)
from common.contracts.stages import ARCHETYPUS, ATTESTATORES, EXEMPLAR, PERLECTOR, RECENSOR
from common.page_accounting import FLAG, NOT_APPLICABLE, PASS, RESIDUAL_INK
from common.page_path import (
    ACT_REGION_SCHEMA,
    PAGE_ACCOUNTING_KIND,
    PAGE_READING_KIND,
    refs_by_path,
)
from common.page_reask import reask_outcome
from common.page_review import (
    CONTINUATION_LINK_KIND,
    HELD,
    LINK_OPERATION,
    PAGE_REVIEW_FIELDS,
    RELEASABLE_HOLDS,
    REVIEW_DECISIONS_KIND,
    REVIEW_DECISIONS_OPERATION,
    REVIEW_DECISIONS_SCHEMA,
    REVIEW_DECISIONS_SUBJECT,
    REVIEWED_PAGE_REVIEW_FIELDS,
    current_link_records,
    current_review_decisions,
    link_generations,
    of_superseded_reading,
    operator_correction,
    override_refusal,
    require_establishable,
    review_priority,
    reviewed_rows,
    run_page_breaks,
    superseded_readings,
)
from common.page_testimonia import (
    PAGE_TESTIMONIUM_KIND,
    current_page_testimonia,
    declared_page_witness_chairs,
    is_detector_blank_testimony,
    require_page_roster,
)
from common.recensor_receipt import build_recensor_reading_receipt
from common.review_decisions import (
    EXCLUDED,
    READING_HELD,
    REVIEW_FIELD,
    apply_decisions,
    held_pages,
)
from common.stage import (
    NO_ACT_ON_PAGE_HOLD,
    PAGE_BLANK_CLASS,
    PAGE_BLANK_HOLD,
    latest_attempt,
    reading_denominator,
    real_pages,
)

# The kind of the Archetypus record that establishes one reading.
ESTABLISHED_KIND: Final = "archetypus"
ACCEPTED: Final = "accepted"
CONFIRMED_BLANK: Final = "confirmed-blank"
# A chair of the sealed page roster with no Testimonium for a page has not been
# attempted there: unresolved, never a reading.
NO_TESTIMONIUM_OUTCOME: Final = "not-run"

# The codes this stage adds to a unit's own, each with the sentence its reason uses.
# `residual-ink` is named in `common.page_accounting`, where the sealed `[flags]`
# policy may make it a review flag: it repeats rule (f)'s check on the same regions.
UNDER_WITNESSED: Final = "under-witnessed"
UNRESOLVED_WITNESS: Final = "unresolved-witness"
RESIDUAL_INK_NOT_MEASURABLE: Final = "residual-ink-not-measurable"
RESIDUAL_INK_NOT_MEASURED: Final = "residual-ink-not-measured"
ASSESSMENT_MALFORMED: Final = "uncertainty-assessment-malformed"
# A continuation flag on an `act` entry that is not the last (runs on) or the
# first (runs on from before) `act` entry of its page: no page break has that
# entry as a side, so the flag would be lost.
CONTINUATION_OFF_EDGE: Final = "continuation-off-page-edge"
# A continuation flag on an `other` entry: recorded as a note, never a hold.
CONTINUATION_ON_OTHER: Final = "continuation-flag-on-other"

# The page accounting rules a page said to hold no act must pass. A blank page
# has no entry for a detector record to be read as, so without a record
# detector its rule (i) does not apply; a page of `other` entries does need it.
# A rule whose every finding is a review flag under the sealed `[flags]` policy
# (`flag`) confirms too: the finding is recorded on every unit of the page and
# reaches the flagged export, and the lead chose to review it rather than hold.
NO_ACT_RULES: Final = ("d", "e", "f", "i")
CONFIRMING: Final = frozenset({PASS, FLAG})
BLANK_RULES: Final = {
    "d": CONFIRMING,
    "e": CONFIRMING,
    "f": CONFIRMING,
    "i": CONFIRMING | {NOT_APPLICABLE},
}


# --- the page witnesses ------------------------------------------------------------


def page_testimonia(context, chairs: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Each page's latest, fully validated `page-testimonium` per chair, by page id.

    A page no witness testified to is absent. A page some chair testified to
    must carry every configured page witness and no other, as the Perlector
    required when it read the page.
    """
    current = current_page_testimonia(context)
    for page_id, records in current.items():
        require_page_roster(page_id, records, chairs)
    return current


def page_witness_coverage(records: list[dict[str, Any]], floor: int, chairs: set[str]) -> dict:
    """One page's witness coverage over the sealed page roster, in the page-read receipt's shape.

    `witness_coverage` over each roster chair's current outcome, judged on page
    reads: a page-read run's witnesses read the whole page, so none is
    attached to an act. A roster chair with no Testimonium for the page is
    `not-run`. The floor counts chairs that read the page (`read` or
    `genuinely-empty`) and were not cut off, by the page-read receipt's own formula
    (`outcomes.witnessed_count`). A reading chair whose Testimonium
    records no truncation state counts toward the floor, and is named in
    `health_unrecorded`; `shortfalls` counts the failed and truncated ones.
    """
    outcomes = {record["payload"]["chair"]: record["outcome"] for record in records}
    for chair in chairs - set(outcomes):
        outcomes[chair] = NO_TESTIMONIUM_OUTCOME
    base = witness_coverage(outcomes, floor)
    reading = [record for record in records if record["outcome"] in WITNESS_READING_OUTCOMES]
    truncation = [_health(record).get("truncated") for record in reading]
    coverage = {
        "configured": base["configured"],
        "floor": base["floor"],
        "by_outcome": base["by_outcome"],
        "by_class": base["by_class"],
        "unresolved_chairs": base["unresolved_chairs"],
        "health_unrecorded": sum(1 for state in truncation if state is None),
        "shortfalls": {
            "failed": base["by_outcome"].get("failed", 0),
            "truncated": sum(1 for state in truncation if state is True),
            "unaligned": 0,
        },
    }
    # The page-read floor formula the page-read receipt checks, not `witness_coverage`'s own.
    coverage["under_witnessed"] = witnessed_count(coverage) < floor
    return coverage


def _health(record: dict[str, Any]) -> dict[str, Any]:
    health = record["payload"].get("content_health")
    return health if isinstance(health, dict) else {}


def retained_text_blank(record: dict[str, Any]) -> bool | None:
    """Whether a witness's retained page text is blank, measured from the text itself.

    `None` when the retained payload is not text, so blankness cannot be read
    from it; a self-reported health field is never the measurement.
    """
    text = record["payload"].get("payload")
    return text.strip() == "" if isinstance(text, str) else None


# --- residual ink -------------------------------------------------------------------


def reading_regions_by_page(context, pages: dict[int, dict], acts: list[dict]) -> dict[int, list]:
    """Every reading region's boxes by sealed page, `act` and `other` alike.

    A region is the boxes of the ids its entry cited (`region_boxes_px`), never
    the rectangle around them, so ink between two cited columns stays residual.
    Every sealed page has an entry, one with no placed region an empty list, so
    its whole ink is measured against nothing.
    """
    regions: dict[int, list[dict[str, int]]] = {ordinal: [] for ordinal in pages}
    for act in acts:
        if act["region_ref"] is None:
            continue
        region = context.tree.read_artifact_reference(
            act["region_ref"], stage=PERLECTOR, kind="act-region", subject_id=act["act_id"]
        )
        schema = region["payload"].get("schema")
        if schema != ACT_REGION_SCHEMA:
            raise FatalAccounting(
                f"act {act['act_id']}'s act-region is {schema!r}, not {ACT_REGION_SCHEMA}; "
                "its region cannot be read as the boxes its entry cited"
            )
        regions[act["page_ordinal"]].extend(region["payload"]["region_boxes_px"])
    return regions


def page_coverage_of(ordinal: int, findings: dict[int, dict]) -> dict[str, Any]:
    """One page's residual-ink fact, with the ink measured and the policy it was measured under.

    `ink` is `None` for a page with no finding; a page whose paper value was
    refused carries the refusal instead of counts.
    """
    finding = findings.get(ordinal)
    unmeasurable = finding is not None and finding.get("ink_measurable") is False
    checked = finding is not None and not unmeasurable
    if finding is None:
        ink = None
    elif unmeasurable:
        ink = {
            "background_refusal": finding["background_refusal"],
            "ink_map_config_sha256": finding["background_config_sha256"],
        }
    else:
        ink = {
            "page_ink_pixels": finding["page_ink_pixels"],
            "page_spanning_ink_pixels": finding["page_spanning_ink_pixels"],
            "total_ink_pixels": finding["total_ink_pixels"],
            "outside_ink_pixels": finding["outside_ink_pixels"],
            "background_level": finding["background"]["background_level"],
            "ink_map_config_sha256": finding["background_config_sha256"],
        }
    return {
        "checked_pages": [ordinal] if checked else [],
        "flagged_pages": [ordinal] if checked and finding.get("flagged") else [],
        "unmeasurable_pages": [ordinal] if unmeasurable else [],
        "ink": ink,
    }


# --- a page that holds no act -------------------------------------------------------


def confirmation(
    accounting: dict, records: list[dict], *, blank: bool, census: frozenset[str]
) -> dict[str, Any]:
    """Whether a page the reading says holds no act is confirmed so, and why not.

    Every detected line, every witness's text and the page's ink must be
    accounted for by the page's readings: the page accounting's rules (d), (e)
    and (f) pass. A page of `other` entries also needs rule (i) to pass, so no
    detector record lies in an `other` region; without a record detector it
    stays held. A blank page needs rule (i) to pass or not apply, no detected
    line at all, and every witness that read the page to have retained blank
    text, with at least one such witness that is not a census: `census` names
    the chairs whose page record is their record detector's look rather than
    a reading of the page's text.
    """
    rules = accounting.get("rules", {})
    allowed = BLANK_RULES if blank else {rule: CONFIRMING for rule in NO_ACT_RULES}
    statuses = {rule: rules.get(rule, {}).get("status") for rule in allowed}
    lines = len(accounting.get("lines") or [])
    witnesses = [
        {
            "chair": record["payload"]["chair"],
            "outcome": record["outcome"],
            "blank": retained_text_blank(record),
        }
        for record in records
    ]
    failures = [
        f"page accounting rule ({rule}) is {status or 'absent'}, not "
        + " or ".join(sorted(allowed[rule]))
        for rule, status in statuses.items()
        if status not in allowed[rule]
    ]
    if blank:
        if lines:
            failures.append(f"Surya detected {lines} line(s) on the page")
        reading = [w for w in witnesses if w["outcome"] in WITNESS_READING_OUTCOMES]
        if not reading:
            failures.append("no witness read the page")
        elif all(w["chair"] in census for w in reading):
            failures.append(
                "only a record detector's census found the page blank; no witness read its text"
            )
        failures.extend(
            f"witness {w['chair']} read the page and its retained text is not blank"
            for w in reading
            if w["blank"] is not True
        )
    return {
        "confirms": "page-blank" if blank else "no-act-on-page",
        "rules": statuses,
        "surya_lines": lines,
        "witnesses": witnesses,
        "confirmed": not failures,
        "failures": failures,
    }


# --- continuation --------------------------------------------------------------------


def _flags(act: dict) -> list[str]:
    return [
        flag
        for flag in ("continues_from_previous_page", "continues_to_next_page")
        if act[flag] is True
    ]


def continuation_off_edge(acts: list[dict]) -> dict[str, list[str]]:
    """Each `act` entry whose continuation flag is not at its page's act edge, flags named.

    A page's act edge is its last whole-page `act` entry (for running on) and its
    first (for running on from before), in answer order. The `other` entries
    around them, a catchword for one, do not move it, and neither does an entry
    the re-ask recovered: it was asked about ids alone, with no continuation flag
    allowed, so its place in page order is not established.
    """
    found: dict[str, list[str]] = {}
    whole_page = page_edges.whole_page_entries(acts)
    for page in page_edges.act_entries_by_page(whole_page).values():
        first, last = page[0]["n"], page[-1]["n"]
        for act in page:
            flags = []
            if act["continues_from_previous_page"] is True and act["n"] != first:
                flags.append("continues_from_previous_page")
            if act["continues_to_next_page"] is True and act["n"] != last:
                flags.append("continues_to_next_page")
            if flags:
                found[act["act_id"]] = flags
    return found


def continuation_notes(act: dict) -> list[dict[str, Any]]:
    """The notes a unit's review records without holding it: a flag on an `other` entry.

    An `other` entry is never a side of a page break, so its continuation flag
    joins nothing; it is kept, named, for a reader.
    """
    flags = _flags(act) if act["n"] is not None and act["kind"] != "act" else []
    return [{"code": CONTINUATION_ON_OTHER, "flags": flags}] if flags else []


def link_inputs(payload: dict, by_id: dict[str, dict], pages: dict[int, dict]) -> list[dict]:
    """A link's evidence: each named side's Perlectio, and the page reading of a null side."""
    inputs = []
    for side, ordinal in (
        (payload["from_act_id"], payload["from_page_ordinal"]),
        (payload["to_act_id"], payload["to_page_ordinal"]),
    ):
        if side is not None:
            inputs.append(by_id[side]["perlectio_ref"])
        elif ordinal in pages:
            inputs.append(pages[ordinal]["reading_ref"])
    return inputs


def link_outcome(payload: dict) -> str:
    return ACCEPTED if payload["agreed"] else HELD


def page_reasks(page: dict) -> int:
    """How many times stage 4 re-asked a page, from its `page_readings` row: 0 or 1."""
    return 0 if page["reask_ref"] is None else 1


def page_receipt_row(context, page: dict) -> dict[str, Any]:
    """A page as the receipt binds it: its readings, last accounting and what its re-ask did."""
    reask = None
    if page["reask_ref"] is not None:
        reading = context.tree.read_artifact_reference(
            page["reask_ref"], stage=PERLECTOR, kind=PAGE_READING_KIND, subject_id=page["page_id"]
        )
        accounting = context.tree.read_artifact_reference(
            page["accounting_ref"],
            stage=PERLECTOR,
            kind=PAGE_ACCOUNTING_KIND,
            subject_id=page["page_id"],
        )
        reask = reask_outcome(reading["payload"]["reask"]["named"], accounting["payload"])
    return {
        "page_ordinal": page["page_ordinal"],
        "reading_ref": page["reading_ref"],
        "reask_ref": page["reask_ref"],
        "accounting_ref": page["accounting_ref"],
        "reask": reask,
    }


# --- one review --------------------------------------------------------------------


def _accounting(context, act: dict) -> dict[str, Any]:
    """The unit's page accounting record; every counted unit's page has one."""
    return context.tree.read_artifact_reference(
        act["accounting_ref"], stage=PERLECTOR, kind=PAGE_ACCOUNTING_KIND, subject_id=act["page_id"]
    )


def _assessment(context, act: dict) -> dict[str, Any] | None:
    if act["perlectio_ref"] is None:
        return None
    reading = context.tree.read_artifact_reference(
        act["perlectio_ref"], stage=PERLECTOR, kind="perlectio", subject_id=act["act_id"]
    )
    assessment = reading["payload"].get("uncertainty_assessment")
    if not isinstance(assessment, dict):
        return None
    return {"state": assessment.get("state"), "problem": assessment.get("problem")}


def coverage_findings(coverage: dict, ordinal: int) -> list[tuple[str, str]]:
    """This stage's witness-floor reasons to hold a unit, as `(code, sentence)` pairs."""
    findings = []
    if coverage["under_witnessed"]:
        truncated = coverage["shortfalls"]["truncated"]
        findings.append(
            (
                UNDER_WITNESSED,
                f"{witnessed_count(coverage)} page witness(es) read page "
                f"{ordinal} against a floor of "
                f"{coverage['floor']}"
                + (f" ({truncated} truncated reading(s) not counted)" if truncated else ""),
            )
        )
    if coverage["unresolved_chairs"]:
        findings.append(
            (
                UNRESOLVED_WITNESS,
                f"{coverage['unresolved_chairs']} page witness(es) have no outcome for page "
                f"{ordinal} yet",
            )
        )
    return findings


def own_findings(
    coverage: dict,
    page_coverage: dict,
    assessment: dict | None,
    ordinal: int,
    off_edge: list[str] | None = None,
) -> list[tuple[str, str]]:
    """This stage's own reasons to hold a unit, as `(code, sentence)` pairs."""
    findings = coverage_findings(coverage, ordinal)
    if page_coverage["flagged_pages"]:
        findings.append(
            (
                RESIDUAL_INK,
                f"page {ordinal} carries ink outside every reading region cut on it (a "
                "residual-ink check against the page image itself); accepting would leave "
                "that ink unaccounted for",
            )
        )
    elif page_coverage["unmeasurable_pages"]:
        findings.append(
            (
                RESIDUAL_INK_NOT_MEASURABLE,
                f"page {ordinal}'s paper value could not be inferred, so its residual ink "
                "was not measured",
            )
        )
    elif not page_coverage["checked_pages"]:
        findings.append(
            (
                RESIDUAL_INK_NOT_MEASURED,
                f"page {ordinal} has no sealed pixels to measure residual ink on",
            )
        )
    if (assessment or {}).get("state") == "malformed":
        findings.append(
            (
                ASSESSMENT_MALFORMED,
                f"the reading's uncertainty assessment is malformed: {assessment.get('problem')}",
            )
        )
    if off_edge:
        findings.append(
            (
                CONTINUATION_OFF_EDGE,
                f"the entry says {' and '.join(off_edge)} but is not at the edge of page "
                f"{ordinal}, so no page break has it as a side",
            )
        )
    return findings


def review_of(
    act: dict,
    *,
    coverage: dict,
    page_coverage: dict,
    assessment: dict | None,
    confirmed: dict | None,
    off_edge: list[str] | None = None,
    recoveries_used: int = 0,
    flag_codes: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any]]:
    """The outcome and payload of one unit's review, without its attempt ordinal.

    `flag_codes` is the sealed `[flags]` policy (`common.page_accounting`): a
    finding of this stage whose code it names is recorded as a review flag,
    named in `flag_codes` and the reason, and holds nothing. The row's own
    `flag_codes` are the page accounting's, measured the same way.
    """
    ordinal = act["page_ordinal"]
    found = own_findings(coverage, page_coverage, assessment, ordinal, off_edge)
    own = [(code, sentence) for code, sentence in found if code not in flag_codes]
    flagged = [(code, sentence) for code, sentence in found if code in flag_codes]
    row_codes = list(act["hold_codes"])
    flags = sorted(set(act["flag_codes"]) | {code for code, _sentence in flagged})
    release = None
    sentences = [sentence for _code, sentence in own]
    if confirmed is not None and not confirmed["confirmed"]:
        sentences.append(
            f"the page is not confirmed to hold no act: {'; '.join(confirmed['failures'])}"
        )
    released = (
        confirmed is not None
        and confirmed["confirmed"]
        and not own
        and row_codes
        and set(row_codes) <= RELEASABLE_HOLDS
    )
    if released:
        release = {
            "hold_codes": row_codes,
            "reason": (
                "confirmed: the page accounting's rules (d), (e) and (f) pass"
                + (
                    ", rule (i) passes or has no record detector to apply, no line was "
                    "detected and every witness that read the page retained blank text"
                    if confirmed["confirms"] == "page-blank"
                    else " and so does rule (i), so every reading on the page is accounted "
                    "for and none is an act"
                )
            ),
        }
        row_codes = []
    elif row_codes:
        sentences.insert(0, f"the page-read records hold this unit: {', '.join(row_codes)}")
    hold_codes = sorted(set(row_codes) | {code for code, _sentence in own})
    if flags:
        sentences.append(
            f"flagged for review, not held, under the sealed review flags: {', '.join(flags)}"
            + "".join(f"; {sentence}" for _code, sentence in flagged)
        )
    if hold_codes:
        outcome = HELD
        reason = "; ".join(sentences)
    elif act["class"] == PAGE_BLANK_CLASS:
        outcome, reason = CONFIRMED_BLANK, f"page {ordinal} is confirmed blank"
    elif flags:
        outcome = ACCEPTED
        reason = "read, and nothing holds it"
    else:
        outcome = ACCEPTED
        reason = (
            "read, witnessed to the floor, and no ink on its page lies outside the reading regions"
        )
    if outcome != HELD and flags:
        reason = "; ".join([reason, *sentences])
    payload = {
        "act_key": act["act_key"],
        "unit_class": act["class"],
        "kind": act["kind"],
        "page_ordinal": ordinal,
        "reason": reason,
        "hold_codes": hold_codes,
        "flag_codes": flags,
        "review_priority": review_priority(set(hold_codes) | set(flags)),
        "coverage": coverage,
        "page_reading_ref": act["reading_ref"],
        "page_accounting_ref": act["accounting_ref"],
        "act_region_ref": act["region_ref"],
        "perlectio_ref": act["perlectio_ref"],
        "page_coverage": page_coverage,
        "continuation": {
            "continues_from_previous_page": act["continues_from_previous_page"],
            "continues_to_next_page": act["continues_to_next_page"],
        },
        "uncertainty_assessment": assessment,
        "confirmation": confirmed,
        "release": release,
        "notes": continuation_notes(act),
        "recoveries_used": recoveries_used,
    }
    return outcome, payload


def validate_page_review_payload(subject_id: str, payload: dict) -> None:
    """Refuse a page review whose payload is not the closed page-review shape."""
    fields = REVIEWED_PAGE_REVIEW_FIELDS if REVIEW_FIELD in payload else PAGE_REVIEW_FIELDS
    if set(payload) != fields:
        raise FatalAccounting(
            f"the Recensor page review of {subject_id!r} is not the closed page-review shape: "
            f"{sorted(set(payload) ^ fields)}"
        )


# --- operator review decisions -----------------------------------------------------------


def derived_review(context, planned: list[tuple[dict, str, dict, list[dict]]]) -> dict[str, Any]:
    """The machine's reviews as `common.review_decisions` reads them, before any decision.

    Each unit's own and page holds are its Perlectio's `holds` and
    `page_holds`, as sealed; a page row has no reading and holds for its page.
    """
    units = []
    for act, outcome, payload, _inputs in planned:
        holds: list[str] = []
        page_holds: list[str] = []
        if act["perlectio_ref"] is not None:
            reading = context.tree.read_artifact_reference(
                act["perlectio_ref"], stage=PERLECTOR, kind="perlectio", subject_id=act["act_id"]
            )
            holds, page_holds = reading["payload"]["holds"], reading["payload"]["page_holds"]
        units.append(
            {
                "act_id": act["act_id"],
                "page_id": act["page_id"],
                "outcome": outcome,
                "payload": payload,
                "unit_holds": holds,
                "page_holds": page_holds,
            }
        )
    return {"run_id": context.tree.run_id, "units": units}


def decide_reviews(
    context, planned: list[tuple[dict, str, dict, list[dict]]]
) -> tuple[list[tuple[dict, str, dict, list[dict], str | None]], dict[str, Any] | None]:
    """Every planned review with the run's operator decisions applied, and their record.

    Returns `(act, outcome, payload, inputs, approval_ref)` per unit, in the
    plan's order, and the `review-decisions` payload (`None` when the run holds
    no decision, so every review is the machine's own). An excluded unit's
    `approval_ref` is the stored path of a current exclusion of it. Decisions
    that clear every hold of a reading its page reading holds override those
    holds (`common.page_review.operator_override`), and the Archetypus
    establishes it, labelled; one the export cannot carry
    (`common.page_review.override_refusal`) stays held under `READING_HELD`.
    """
    stored = context.tree.review_decision_records()
    if not stored:
        return [(*plan, None) for plan in planned], None
    paths = {reference.sha256: reference.relative_path for reference, _record in stored}
    result = apply_decisions(derived_review(context, planned), [record for _, record in stored])
    decided = []
    # Each unit kept held under `READING_HELD`, and each page with such a unit,
    # by the codes that hold it again.
    reheld: dict[tuple[str, str], set[str]] = {}
    for act, machine_outcome, _payload, inputs in planned:
        unit = result["units"][act["act_id"]]
        outcome, payload = unit["outcome"], unit["payload"]
        approval_ref = None
        if outcome == EXCLUDED:
            approval_ref = paths[
                min(
                    s["record_sha256"]
                    for s in result["applied"]
                    if s["scope"] == UNIT_SCOPE
                    and s["subject_id"] == act["act_id"]
                    and s["decision"] == "exclude"
                )
            ]
        if outcome == ACCEPTED and machine_outcome != ACCEPTED:
            refusal = _not_establishable(act, payload)
            if refusal is not None:
                outcome, payload = HELD, _reading_held(act, payload, refusal)
                codes = set(payload["hold_codes"])
                reheld[(UNIT_SCOPE, act["act_id"])] = codes
                reheld.setdefault((PAGE_SCOPE, act["page_id"]), set()).update(codes)
        # A decision moved the unit's hold codes; its place in the queue follows them.
        payload = {
            **payload,
            "review_priority": review_priority(
                set(payload["hold_codes"]) | set(payload["flag_codes"])
            ),
        }
        decided.append((act, outcome, payload, inputs, approval_ref))
    record = {
        "schema": REVIEW_DECISIONS_SCHEMA,
        "decisions_digest": result["decisions_digest"],
        "applied": result["applied"],
        "stale": result["stale"],
        "conflicting": result["conflicting"],
        "carried": result["carried"],
        "unkept": result["unkept"],
        "clearances": _effective_clearances(result["clearances"], reheld),
        # A correction kept held did not take effect.
        "corrections": [
            row for row in result["corrections"] if (UNIT_SCOPE, row["subject_id"]) not in reheld
        ],
        "page_holds": [
            {"page_ordinal": ordinal, "hold_codes": codes}
            for ordinal, codes in sorted(held_pages(result).items())
        ],
        "requests": result["requests"],
    }
    return decided, record


def _not_establishable(act: dict, payload: dict) -> str | None:
    """Why the Archetypus would not establish this unit's reading were its review accepted.

    None when it would: a reading the Perlector did not hold, a machine
    release, or an operator override of every hold the reading carries.
    """
    if act["perlectio_ref"] is None:
        return None
    edit = operator_correction(act, {"outcome": ACCEPTED, "payload": payload}) is not None
    if (act["hold_codes"] or edit) and (refusal := override_refusal(act, edit=edit)) is not None:
        return refusal
    try:
        require_establishable(act, {"outcome": ACCEPTED, "payload": payload})
    except FatalAccounting as error:
        return str(error)
    return None


def _effective_clearances(
    clearances: list[dict], reheld: dict[tuple[str, str], set[str]]
) -> list[dict]:
    """The clearances that took effect: a unit or page row loses the codes that hold again.

    A page row loses every code that holds again any unit on its page. A row
    left with no code cleared is dropped, so the aggregate never reports a
    release that held nothing less.
    """
    rows = []
    for row in clearances:
        held = reheld.get((row["scope"], row["subject_id"]))
        if held:
            row = {**row, "cleared": [code for code in row["cleared"] if code not in held]}
            if not row["cleared"]:
                continue
        rows.append(row)
    return rows


def _reading_held(act: dict, payload: dict, refusal: str) -> dict:
    """A decided review that stays held because no decision can send its reading to export.

    The reading's own codes stay and `READING_HELD` names why the decisions
    did not complete it; the `operator_review` block records the added code,
    and its `cleared` keeps only the codes that no longer hold the unit.
    """
    block = payload[REVIEW_FIELD]
    reading_codes = ", ".join(act["hold_codes"]) or "no code"
    held = set(payload["hold_codes"]) | set(act["hold_codes"]) | {READING_HELD}
    cleared = {
        scope: [code for code in codes if code not in held]
        for scope, codes in block["cleared"].items()
    }
    return {
        **payload,
        "hold_codes": sorted(held),
        "reason": (
            f"operator review would release it, but its page reading holds it ({reading_codes}) "
            f"and no decision can send that reading to export ({refusal}), so it stays held "
            f"({READING_HELD}); {payload['reason']}"
        ),
        REVIEW_FIELD: {
            **block,
            "cleared": cleared,
            "added": sorted(set(block["added"]) | {READING_HELD}),
        },
    }


def _review_decisions_ordinal(context, record: dict[str, Any]) -> int:
    """The attempt ordinal the pass's `review-decisions` record is published at.

    The current one's when nothing changed, so a repeat reuses its bytes, and
    the next one otherwise. A decision recorded after the Archetypus
    established a reading cannot reach what it established, so a changed
    record is refused once any established record exists; the Archetypus's
    seal and index alone establish nothing and do not stop it.
    """
    current = current_review_decisions(context)
    if current is not None:
        ordinal = current.pop("attempt_ordinal")
        if current == record:
            return ordinal
    established = [
        entry
        for entry in context.tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == ESTABLISHED_KIND
    ]
    if established:
        raise ApprovalRefusal(
            f"operator review decisions changed after the Archetypus established "
            f"{len(established)} reading(s), which a decision recorded now cannot reach. "
            "Record them in a new run of this submission instead: an unattended run stops at "
            "a held Recensor, before the Archetypus, so its decisions are made in time"
        )
    return 1 if current is None else ordinal + 1


def publish_review_decisions(context, record: dict[str, Any], ordinal: int) -> None:
    """Publish the pass's `review-decisions` record at `_review_decisions_ordinal`'s ordinal."""
    context.publish(
        kind=REVIEW_DECISIONS_KIND,
        subject_id=REVIEW_DECISIONS_SUBJECT,
        outcome="recorded",
        attempt=attempt_id(REVIEW_DECISIONS_SUBJECT, REVIEW_DECISIONS_OPERATION, ordinal),
        payload={**record, "attempt_ordinal": ordinal},
    )


# --- the pass --------------------------------------------------------------------------


def plan_reviews(
    context, denominator: dict[str, Any], page_coverage_findings: Callable[..., dict[int, dict]]
) -> tuple[list[dict], list[tuple[dict, str, dict, list[dict]]]]:
    """Every counted unit, and the `(unit, outcome, payload, inputs)` of its review.

    Every fact is measured from disk: the page witnesses, the residual ink,
    each unit's page accounting and uncertainty assessment, and whether a page
    said to hold no act is confirmed so. The Recensor publishes these; its
    receipt measures them again and requires the reviews on disk to be them.
    """
    pages = denominator["pages"]
    acts = reviewed_rows(denominator["acts"])
    chairs = declared_page_witness_chairs(context)
    testimonia = page_testimonia(context, chairs)
    findings = page_coverage_findings(
        context, regions=reading_regions_by_page(context, pages, acts)
    )
    off_edge = continuation_off_edge(acts)
    floor = context.witness_floor
    # The sealed `[flags]` policy the page accounting was measured under decides
    # which of this stage's own findings are review flags too.
    flag_codes = page_accounting.require_page_accounting_policy(
        context, context.page_accounting_config_path
    ).flag_codes
    planned = []
    for act in acts:
        records = testimonia.get(act["page_id"], [])
        accounting = _accounting(context, act)
        _require_accounted_testimonia(context, act, accounting, records)
        confirmed = None
        if PAGE_BLANK_HOLD in act["hold_codes"] or NO_ACT_ON_PAGE_HOLD in act["hold_codes"]:
            confirmed = confirmation(
                accounting["payload"],
                records,
                blank=act["class"] == PAGE_BLANK_CLASS,
                census=frozenset(
                    record["payload"]["chair"]
                    for record in records
                    if is_detector_blank_testimony(context, record)
                ),
            )
        outcome, payload = review_of(
            act,
            coverage=page_witness_coverage(records, floor, chairs),
            page_coverage=page_coverage_of(act["page_ordinal"], findings),
            assessment=_assessment(context, act),
            confirmed=confirmed,
            off_edge=off_edge.get(act["act_id"]),
            recoveries_used=page_reasks(pages[act["page_ordinal"]]),
            flag_codes=flag_codes,
        )
        exemplar_page = context.artifact_ref(
            EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", act["page_id"])
        )
        inputs = [
            reference
            for reference in (
                act["reading_ref"],
                act["accounting_ref"],
                act["region_ref"],
                act["perlectio_ref"],
                exemplar_page,
            )
            if reference is not None
        ] + [
            context.artifact_ref(ATTESTATORES, PAGE_TESTIMONIUM_KIND, record["artifact_id"])
            for record in records
        ]
        planned.append((act, outcome, payload, inputs))
    return acts, planned


def review_pages(
    context,
    denominator: dict[str, Any],
    *,
    page_coverage_findings: Callable[..., dict[int, dict]],
    publish_review: Callable[..., dict],
) -> int:
    """Review every counted unit and record every flagged page break.

    Returns how many records are held: units, one-sided page breaks, and pages
    the applied decisions leave held (`common.review_decisions.held_pages`),
    alike, so a run with an unresolved break, or a held page whose every unit
    was excluded, does not exit complete. Every fact is
    measured before anything is published, so a refusal found at a later unit
    never leaves a partial set of reviews behind.
    """
    pages = denominator["pages"]
    acts, planned = plan_reviews(context, denominator, page_coverage_findings)
    decided, decisions = decide_reviews(context, planned)
    ordinal = None if decisions is None else _review_decisions_ordinal(context, decisions)
    by_id = {act["act_id"]: act for act in acts}
    # A null side's evidence is its page's reading, and a canary page is no side.
    real = real_pages(context.run, pages)
    links = [
        (subject, payload, link_inputs(payload, by_id, real))
        for subject, payload in run_page_breaks(context, acts)
    ]
    prior_by_id: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(RECENSOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] == "review" and entry["subject_id"] in by_id:
            prior_by_id.setdefault(entry["subject_id"], []).append(
                context.tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            )
    priors = {
        act_id: latest_attempt(records, f"Recensor review of {act_id}", operation="recense")
        for act_id, records in prior_by_id.items()
    }

    held = 0
    for act, outcome, payload, inputs, approval_ref in decided:
        publish_review(
            context,
            subject_id=act["act_id"],
            outcome=outcome,
            prior=priors.get(act["act_id"]),
            inputs=inputs,
            payload=payload,
            check=validate_page_review_payload,
            approval_ref=approval_ref,
        )
        held += outcome == HELD
    if decisions is not None:
        publish_review_decisions(context, decisions, ordinal)
        held += len(decisions["page_holds"])
    for subject, payload, inputs in links:
        outcome = link_outcome(payload)
        # A break's link is filed again, as its next attempt, only when the
        # readings an operator re-read changed what the break's sides say.
        generations = link_generations(context.tree, subject)
        last = generations[-1] if generations else None
        unchanged = (
            last is not None
            and last["payload"] == payload
            and refs_by_path(last["inputs"]) == refs_by_path(inputs)
        )
        context.publish(
            kind=CONTINUATION_LINK_KIND,
            subject_id=subject,
            outcome=outcome,
            attempt=attempt_id(
                subject, LINK_OPERATION, len(generations) if unchanged else len(generations) + 1
            ),
            inputs=inputs,
            payload=payload,
        )
        held += outcome == HELD
    return held


def _require_accounted_testimonia(
    context, act: dict, accounting: dict, records: list[dict]
) -> None:
    """The witnesses counted for the floor are the ones the page accounting measured."""
    measured = accounting.get("inputs", [])
    for testimonium in records:
        reference = context.artifact_ref(
            ATTESTATORES, PAGE_TESTIMONIUM_KIND, testimonium["artifact_id"]
        )
        if reference not in measured:
            raise FatalAccounting(
                f"page {act['page_id']}'s current page Testimonium from "
                f"{testimonium['payload']['chair']} is not one its page accounting measured; "
                "the witness floor and the accounting would speak about different witnesses"
            )


# --- the receipt -------------------------------------------------------------------------


def release_reason(act: dict, review: dict) -> str | None:
    """Why a unit its page reading held is completed at review, or None for any other unit.

    The machine's release names its confirmation; a unit an operator decision
    completed names that decision through its review's reason.
    """
    payload = review["payload"]
    if (payload.get("release") or {}).get("reason") is not None:
        return payload["release"]["reason"]
    completed = classify(RECENSOR, review["outcome"]) is OutcomeClass.COMPLETED
    if act["disposition"] == "held" and completed and REVIEW_FIELD in payload:
        return payload["reason"]
    return None


def current_links(context, expected: list[tuple[str, dict]], by_id, pages) -> list[dict]:
    """Every continuation-link on disk, matched one to one against the breaks disk derives."""
    derived = {subject: payload for subject, payload in expected}
    found = {subject: [record] for subject, record in current_link_records(context.tree).items()}
    if missing := sorted(set(derived) - set(found)):
        raise FatalAccounting(f"the flagged page break(s) {missing} have no continuation-link")
    if stray := sorted(set(found) - set(derived)):
        raise FatalAccounting(
            f"continuation-link(s) {stray} name a page break no answer of this run flags"
        )
    rows = []
    for subject, payload in expected:
        records = found[subject]
        record = records[0]
        if (
            len(records) != 1
            or record.get("payload") != payload
            or record.get("outcome") != link_outcome(payload)
            or refs_by_path(record["inputs"]) != refs_by_path(link_inputs(payload, by_id, pages))
        ):
            raise FatalAccounting(
                f"the continuation-link of {subject} is not the one record the answers' "
                "flags derive"
            )
        rows.append(
            {
                "subject_id": subject,
                "link_ref": context.artifact_ref(
                    RECENSOR, CONTINUATION_LINK_KIND, record["artifact_id"]
                ),
                "outcome": record["outcome"],
            }
        )
    return rows


def write_reading_receipt(
    context, *, page_coverage_findings: Callable[..., dict[int, dict]]
) -> None:
    """Rebuild the partition receipt from disk: pages, units, reviews, coverage, page breaks.

    The units are re-derived through `reading_denominator` and every review is
    measured again (`plan_reviews`): its coverage, residual ink, confirmation,
    release, codes, outcome and inputs must be exactly what disk gives, the
    Testimonia counted for the floor must be the ones the page accounting
    measured, every continuation-link is matched against the breaks the
    answers flag, and a Recensor review of anything outside `reading_acts` is
    refused.
    """
    for stage in (ATTESTATORES, PERLECTOR, RECENSOR):
        if not context.tree.manifest_agrees_with_disk(stage):
            raise FatalAccounting(
                f"the stored {stage} manifest disagrees with its on-disk artifacts; the "
                "Recensor partition receipt refuses a cache as its denominator"
            )
    denominator = reading_denominator(context)
    pages = denominator["pages"]
    acts, planned = plan_reviews(context, denominator, page_coverage_findings)
    decided, decisions = decide_reviews(context, planned)
    recorded = current_review_decisions(context)
    if recorded is not None:
        recorded.pop("attempt_ordinal")
    if recorded != decisions:
        raise FatalAccounting(
            "the Recensor's review-decisions record is not what the run's operator review "
            "decisions give against the reviews disk measures"
        )
    by_id = {act["act_id"]: act for act in acts}
    reviews: dict[str, list[dict]] = {act_id: [] for act_id in by_id}
    superseded: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "review":
            continue
        record = context.tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        if record["subject_id"] in reviews:
            reviews[record["subject_id"]].append(record)
        else:
            superseded.setdefault(record["subject_id"], []).append(record)
    # A unit outside the counted ones is reviewed only as a unit of a reading an
    # operator re-read superseded, kept as published.
    old_readings = superseded_readings(context.tree)
    for subject, records in sorted(superseded.items()):
        if not all(of_superseded_reading(record, old_readings) for record in records):
            raise FatalAccounting(
                f"Recensor review {records[0]['artifact_id']} names unit {subject!r}, which is "
                "outside this page-read run's reading_acts"
            )
    items = []
    for act, outcome, expected, inputs, approval_ref in sorted(
        decided, key=lambda plan: plan[0]["act_id"]
    ):
        act_id = act["act_id"]
        if not reviews[act_id]:
            raise FatalAccounting(f"unit {act_id} ({act['act_key']}) has no Recensor review")
        review = latest_attempt(
            reviews[act_id], f"Recensor review of {act_id}", operation="recense"
        )
        payload = review.get("payload")
        coverage = expected["coverage"]
        if (
            not isinstance(payload, dict)
            or payload.get("act_key") != act["act_key"]
            or payload.get("coverage") != coverage
        ):
            raise FatalAccounting(
                f"Recensor review of {act_id} does not retain the unit key and witness coverage "
                "recomputed from disk"
            )
        sealed = {name: value for name, value in payload.items() if name != "attempt_ordinal"}
        differing = sorted(
            name for name in set(sealed) | set(expected) if sealed.get(name) != expected.get(name)
        )
        if review["outcome"] != outcome:
            differing.insert(0, "outcome")
        if refs_by_path(review.get("inputs", [])) != refs_by_path(inputs):
            differing.append("inputs")
        if review.get("approval_ref") != approval_ref:
            differing.append("approval_ref")
        if differing:
            raise FatalAccounting(
                f"Recensor review of {act_id} is not the review disk measures: its "
                f"{', '.join(differing)} differ from what its page's records give"
            )
        items.append(
            {
                "act_id": act_id,
                "act_key": act["act_key"],
                "page_disposition": act["disposition"],
                "release_reason": release_reason(act, review),
                "review_ref": context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
                "review_outcome": review["outcome"],
                "partition_class": classify(RECENSOR, review["outcome"]).value,
                "coverage": coverage,
            }
        )
    links = current_links(
        context, run_page_breaks(context, acts), by_id, real_pages(context.run, pages)
    )
    receipt = build_recensor_reading_receipt(
        run_id=context.tree.run_id,
        config_digest=context.run["config_digest"],
        pages=[page_receipt_row(context, pages[ordinal]) for ordinal in sorted(pages)],
        items=items,
        continuation_links=links,
        page_holds=[] if decisions is None else decisions["page_holds"],
    )
    context.tree.write_recensor_partition_receipt(receipt)
    context.tree.write_recensor_review_summary(
        review_summary(
            context,
            pages,
            [(act, outcome, payload) for act, outcome, payload, _inputs, _ref in decided],
            [] if decisions is None else decisions["page_holds"],
        )
    )


# --- the review summary --------------------------------------------------------------

REVIEW_SUMMARY_SCHEMA: Final = "recensor-review-summary.v1"
# A page whose reading names no type: the Perlector's page types, when it names
# them, are read from the page reading (`page_type_of`).
UNTYPED_PAGE: Final = "untyped"


def page_type_of(reading: Mapping[str, Any]) -> str:
    """The page type a page reading names, or `untyped`.

    Read from the reading payload's `page_type`, or its answer's, so a reading
    that carries one in either place is counted by it; nothing here decides
    what a type means or which checks it turns on, which is the page
    accounting's business.
    """
    payload = reading.get("payload") or {}
    answer = payload.get("answer")
    for candidate in (payload.get("page_type"), (answer or {}).get("page_type")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return UNTYPED_PAGE


def review_summary(
    context,
    pages: Mapping[int, Mapping[str, Any]],
    reviews: list[tuple[Mapping[str, Any], str, Mapping[str, Any]]],
    page_holds: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Holds and review flags counted apart, per page and per unit, by code and by page type.

    A page-level finding reaches every unit of its page, so counting units
    alone multiplies one finding by the page's entries; `by_code` counts the
    pages a code is on beside the units carrying it. A page is `held` when any
    of its units is held or a decision holds the page, `flagged` when none is
    held and some unit carries a review flag, and `clean` otherwise. The queue
    lists every held or flagged unit in review priority, then page order.
    """
    types = {
        ordinal: page_type_of(
            context.tree.read_artifact_reference(
                page["reading_ref"], stage=PERLECTOR, kind=PAGE_READING_KIND
            )
        )
        for ordinal, page in pages.items()
    }
    decided_pages = {row["page_ordinal"] for row in page_holds}
    held: set[int] = set(decided_pages)
    flagged: set[int] = set()
    by_code: dict[str, dict[str, Any]] = {}
    queue = []
    held_units = flagged_units = 0
    for act, outcome, payload in reviews:
        ordinal = act["page_ordinal"]
        codes = {code: "hold" for code in payload["hold_codes"]}
        codes.update({code: "flag" for code in payload["flag_codes"]})
        if outcome == HELD:
            held.add(ordinal)
            held_units += 1
        elif payload["flag_codes"]:
            flagged.add(ordinal)
            flagged_units += 1
        for code, role in codes.items():
            row = by_code.setdefault(code, {"as": set(), "pages": set(), "units": 0})
            row["as"].add(role)
            row["pages"].add(ordinal)
            row["units"] += 1
        if codes:
            queue.append(
                {
                    "review_priority": payload["review_priority"],
                    "page_ordinal": ordinal,
                    "act_key": act["act_key"],
                    "act_id": act["act_id"],
                    "kind": act["kind"],
                    "page_type": types.get(ordinal, UNTYPED_PAGE),
                    "outcome": outcome,
                    "hold_codes": list(payload["hold_codes"]),
                    "flag_codes": list(payload["flag_codes"]),
                }
            )
    flagged -= held
    by_type: dict[str, dict[str, Any]] = {}
    for ordinal, page_type in types.items():
        row = by_type.setdefault(
            page_type, {"pages": 0, "held_pages": 0, "flagged_pages": 0, "clean_pages": 0}
        )
        row["pages"] += 1
        if ordinal in held:
            row["held_pages"] += 1
        elif ordinal in flagged:
            row["flagged_pages"] += 1
        else:
            row["clean_pages"] += 1
    queue.sort(key=lambda row: (row["review_priority"], row["page_ordinal"], row["act_key"]))
    return {
        "schema": REVIEW_SUMMARY_SCHEMA,
        "run_id": context.tree.run_id,
        "config_digest": context.run["config_digest"],
        "pages": len(pages),
        "units": len(reviews),
        "held_pages": sorted(held),
        "flagged_pages": sorted(flagged),
        "clean_pages": sorted(set(pages) - held - flagged),
        "held_units": held_units,
        "flagged_units": flagged_units,
        "by_code": {
            code: {
                "as": "both" if len(row["as"]) == 2 else next(iter(row["as"])),
                "pages": len(row["pages"]),
                "units": row["units"],
            }
            for code, row in sorted(by_code.items())
        },
        "by_page_type": dict(sorted(by_type.items())),
        "queue": queue,
    }
