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
  is a note in its review.

A unit is `accepted` only when its row is `read`, the floor holds, no chair is
unresolved and the page's ink is covered; every other unit is `held-for-review`
with every reason named. Nothing here reads, repairs or chooses text, and
nothing asks for a recovery: `recoveries_used` is always 0.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.outcomes import (
    WITNESS_READING_OUTCOMES,
    classify,
    witness_coverage,
    witnessed_count,
)
from common.contracts.stages import ATTESTATORES, EXEMPLAR, PERLECTOR, RECENSOR
from common.page_accounting import NOT_APPLICABLE, PASS
from common.page_path import PAGE_ACCOUNTING_KIND, refs_by_path
from common.page_review import (
    CONTINUATION_LINK_KIND,
    HELD,
    PAGE_REVIEW_FIELDS,
    RELEASABLE_HOLDS,
    act_entries_by_page,
    page_breaks,
    reviewed_rows,
)
from common.page_testimonia import (
    PAGE_TESTIMONIUM_KIND,
    current_page_testimonia,
    declared_page_witness_chairs,
    require_page_roster,
)
from common.recensor_receipt import build_recensor_reading_receipt
from common.stage import (
    NO_ACT_ON_PAGE_HOLD,
    PAGE_BLANK_CLASS,
    PAGE_BLANK_HOLD,
    exemplar_page_ids,
    latest_attempt,
    reading_denominator,
)

ACCEPTED: Final = "accepted"
CONFIRMED_BLANK: Final = "confirmed-blank"
# A chair of the sealed page roster with no Testimonium for a page has not been
# attempted there: unresolved, never a reading.
NO_TESTIMONIUM_OUTCOME: Final = "not-run"

# The codes this stage adds to a unit's own, each with the sentence its reason uses.
UNDER_WITNESSED: Final = "under-witnessed"
UNRESOLVED_WITNESS: Final = "unresolved-witness"
RESIDUAL_INK: Final = "residual-ink"
RESIDUAL_INK_NOT_MEASURABLE: Final = "residual-ink-not-measurable"
RESIDUAL_INK_NOT_MEASURED: Final = "residual-ink-not-measured"
ASSESSMENT_MALFORMED: Final = "uncertainty-assessment-malformed"
# A continuation flag on an `act` entry that is not the last (runs on) or the
# first (runs on from before) `act` entry of its page: no page break has that
# entry as a side, so the flag would be lost.
CONTINUATION_OFF_EDGE: Final = "continuation-off-page-edge"
# A continuation flag on an `other` entry: recorded as a note, never a hold.
CONTINUATION_ON_OTHER: Final = "continuation-flag-on-other"
OWN_CODES: Final = frozenset(
    {
        UNDER_WITNESSED,
        UNRESOLVED_WITNESS,
        RESIDUAL_INK,
        RESIDUAL_INK_NOT_MEASURABLE,
        RESIDUAL_INK_NOT_MEASURED,
        ASSESSMENT_MALFORMED,
        CONTINUATION_OFF_EDGE,
    }
)

# The page accounting rules a page said to hold no act must pass. A blank page
# has no entry for a detector record to be read as, so without a record
# detector its rule (i) does not apply; a page of `other` entries does need it.
NO_ACT_RULES: Final = ("d", "e", "f", "i")
BLANK_RULES: Final = {"d": {PASS}, "e": {PASS}, "f": {PASS}, "i": {PASS, NOT_APPLICABLE}}


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
    """One page's witness coverage over the sealed page roster, in the v3 receipt's shape.

    `witness_coverage` over each roster chair's current outcome, judged on page
    reads: a page-read run's witnesses read the whole page, so none is
    attached to an act. A roster chair with no Testimonium for the page is
    `not-run`. The floor counts chairs that read the page (`read` or
    `genuinely-empty`) and were not cut off, by the v3 receipt's own formula
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
    # The page-read floor formula the v3 receipt checks, not `witness_coverage`'s own.
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
    """Every reading region's union box by sealed page, `act` and `other` alike.

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
        box = region["payload"].get("union_box_px")
        if box is not None:
            regions[act["page_ordinal"]].append(box)
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


def confirmation(accounting: dict, records: list[dict], *, blank: bool) -> dict[str, Any]:
    """Whether a page the reading says holds no act is confirmed so, and why not.

    Every detected line, every witness's text and the page's ink must be
    accounted for by the page's readings: the page accounting's rules (d), (e)
    and (f) pass. A page of `other` entries also needs rule (i) to pass, so no
    detector record lies in an `other` region; without a record detector it
    stays held. A blank page needs rule (i) to pass or not apply, no detected
    line at all, and every witness that read the page to have retained blank
    text, with at least one such witness.
    """
    rules = accounting.get("rules", {})
    allowed = BLANK_RULES if blank else {rule: {PASS} for rule in NO_ACT_RULES}
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

    A page's act edge is its last `act` entry (for running on) and its first
    (for running on from before); `other` entries around them, a catchword
    for one, do not move it.
    """
    found: dict[str, list[str]] = {}
    for page in act_entries_by_page(acts).values():
        first = min(act["n"] for act in page)
        last = max(act["n"] for act in page)
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
) -> tuple[str, dict[str, Any]]:
    """The outcome and payload of one unit's review, without its attempt ordinal."""
    ordinal = act["page_ordinal"]
    own = own_findings(coverage, page_coverage, assessment, ordinal, off_edge)
    row_codes = list(act["hold_codes"])
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
    if hold_codes:
        outcome = HELD
        reason = "; ".join(sentences)
    elif act["class"] == PAGE_BLANK_CLASS:
        outcome, reason = CONFIRMED_BLANK, f"page {ordinal} is confirmed blank"
    else:
        outcome = ACCEPTED
        reason = (
            "read, witnessed to the floor, and no ink on its page lies outside the reading regions"
        )
    payload = {
        "act_key": act["act_key"],
        "unit_class": act["class"],
        "kind": act["kind"],
        "page_ordinal": ordinal,
        "reason": reason,
        "hold_codes": hold_codes,
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
        "recoveries_used": 0,
    }
    return outcome, payload


def validate_page_review_payload(subject_id: str, payload: dict) -> None:
    """Refuse a page review whose payload is not the closed page-review shape."""
    if set(payload) != PAGE_REVIEW_FIELDS:
        raise FatalAccounting(
            f"the Recensor page review of {subject_id!r} is not the closed page-review shape: "
            f"{sorted(set(payload) ^ PAGE_REVIEW_FIELDS)}"
        )


# --- the pass --------------------------------------------------------------------------


def plan_reviews(
    context, denominator: dict[str, Any], page_coverage_findings: Callable[..., dict[int, dict]]
) -> tuple[list[dict], list[tuple[dict, str, dict, list[dict]]]]:
    """Every counted unit, and the `(unit, outcome, payload, inputs)` of its review.

    Every fact is measured from disk: the page witnesses, the residual ink,
    each unit's page accounting and uncertainty assessment, and whether a page
    said to hold no act is confirmed so. The Recensor publishes these; its v3
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
    planned = []
    for act in acts:
        records = testimonia.get(act["page_id"], [])
        accounting = _accounting(context, act)
        _require_accounted_testimonia(context, act, accounting, records)
        confirmed = None
        if PAGE_BLANK_HOLD in act["hold_codes"] or NO_ACT_ON_PAGE_HOLD in act["hold_codes"]:
            confirmed = confirmation(
                accounting["payload"], records, blank=act["class"] == PAGE_BLANK_CLASS
            )
        outcome, payload = review_of(
            act,
            coverage=page_witness_coverage(records, floor, chairs),
            page_coverage=page_coverage_of(act["page_ordinal"], findings),
            assessment=_assessment(context, act),
            confirmed=confirmed,
            off_edge=off_edge.get(act["act_id"]),
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
    current_review: Callable[[Any, str], dict | None],
) -> int:
    """Review every counted unit and record every flagged page break.

    Returns how many records are held: units and one-sided page breaks alike,
    so a run with an unresolved break does not exit complete. Every fact is
    measured before anything is published, so a refusal found at a later unit
    never leaves a partial set of reviews behind.
    """
    pages = denominator["pages"]
    acts, planned = plan_reviews(context, denominator, page_coverage_findings)
    by_id = {act["act_id"]: act for act in acts}
    links = [
        (subject, payload, link_inputs(payload, by_id, pages))
        for subject, payload in page_breaks(exemplar_page_ids(context), acts)
    ]

    held = 0
    for act, outcome, payload, inputs in planned:
        publish_review(
            context,
            subject_id=act["act_id"],
            outcome=outcome,
            prior=current_review(context, act["act_id"]),
            inputs=inputs,
            payload=payload,
            check=validate_page_review_payload,
        )
        held += outcome == HELD
    for subject, payload, inputs in links:
        outcome = link_outcome(payload)
        context.publish(
            kind=CONTINUATION_LINK_KIND,
            subject_id=subject,
            outcome=outcome,
            attempt=attempt_id(subject, "link", 1),
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


def require_derived_outcome(act: dict, review: dict, coverage: dict, off_edge: list[str]) -> None:
    """A unit's review outcome follows from its row's holds, its release and its own codes.

    Each row hold code is either kept or named in a release, and a release
    names exactly the row's codes, only codes this stage may release, on a
    confirmed page. The coverage and continuation codes are recomputed; a
    unit is held exactly when any code remains.
    """
    act_id = act["act_id"]
    payload = review["payload"]
    hold = payload.get("hold_codes")
    release = payload.get("release")
    confirmed = payload.get("confirmation")
    row = set(act["hold_codes"])
    if not isinstance(hold, list) or not set(hold) <= row | OWN_CODES:
        raise FatalAccounting(
            f"Recensor review of {act_id} holds on codes neither its row nor this stage names"
        )
    released: set[str] = set()
    if release is not None:
        if (
            not isinstance(release, dict)
            or release.get("hold_codes") != sorted(row)
            or not row
            or not row <= RELEASABLE_HOLDS
            or not isinstance(confirmed, dict)
            or confirmed.get("confirmed") is not True
        ):
            raise FatalAccounting(
                f"Recensor review of {act_id} releases hold codes other than its row's "
                "releasable ones on a confirmed page"
            )
        released = row
    if not (row - released) <= set(hold):
        raise FatalAccounting(
            f"Recensor review of {act_id} drops row hold code(s) "
            f"{sorted(row - released - set(hold))} without naming a release"
        )
    expected_own = {code for code, _sentence in coverage_findings(coverage, act["page_ordinal"])}
    if off_edge:
        expected_own.add(CONTINUATION_OFF_EDGE)
    recomputed = {UNDER_WITNESSED, UNRESOLVED_WITNESS, CONTINUATION_OFF_EDGE}
    if set(hold) & recomputed != expected_own:
        raise FatalAccounting(
            f"Recensor review of {act_id} names witness-floor or continuation holds "
            f"{sorted(set(hold) & recomputed)}, but disk derives {sorted(expected_own)}"
        )
    expected = HELD if hold else CONFIRMED_BLANK if act["class"] == PAGE_BLANK_CLASS else ACCEPTED
    if review["outcome"] != expected:
        raise FatalAccounting(
            f"Recensor review of {act_id} is {review['outcome']!r}, but its hold codes and "
            f"release derive {expected!r}"
        )


def current_links(context, expected: list[tuple[str, dict]], by_id, pages) -> list[dict]:
    """Every continuation-link on disk, matched one to one against the breaks disk derives."""
    derived = {subject: payload for subject, payload in expected}
    found: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] == CONTINUATION_LINK_KIND:
            record = context.tree.read_artifact(
                RECENSOR, CONTINUATION_LINK_KIND, entry["artifact_id"]
            )
            found.setdefault(record["subject_id"], []).append(record)
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
            or record.get("attempt_id") != attempt_id(subject, "link", 1)
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
    """Rebuild the v3 partition receipt from disk: units, reviews, coverage, page breaks.

    The units are re-derived through `reading_denominator` and every review is
    measured again (`plan_reviews`): its coverage, residual ink, confirmation,
    release, codes, outcome and inputs must be exactly what disk gives, the
    Testimonia counted for the floor must be the ones the page accounting
    measured, every continuation-link is matched against the breaks the
    answers flag, and a Recensor review or recovery request of anything outside
    `reading_acts` is refused.
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
    by_id = {act["act_id"]: act for act in acts}
    reviews: dict[str, list[dict]] = {act_id: [] for act_id in by_id}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] == "recovery-request":
            raise FatalAccounting(
                f"Recensor recovery request {entry['artifact_id']} exists in a page-read run, "
                "which asks for no recovery"
            )
        if entry["kind"] != "review":
            continue
        record = context.tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        if record["subject_id"] not in by_id:
            raise FatalAccounting(
                f"Recensor review {record['artifact_id']} names unit {record['subject_id']!r}, "
                "which is outside this page-read run's reading_acts"
            )
        reviews[record["subject_id"]].append(record)
    off_edge = continuation_off_edge(acts)
    items = []
    for act, outcome, expected, inputs in sorted(planned, key=lambda plan: plan[0]["act_id"]):
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
        require_derived_outcome(act, review, coverage, off_edge.get(act_id, []))
        sealed = {name: value for name, value in payload.items() if name != "attempt_ordinal"}
        differing = sorted(
            name for name in set(sealed) | set(expected) if sealed.get(name) != expected.get(name)
        )
        if review["outcome"] != outcome:
            differing.insert(0, "outcome")
        if refs_by_path(review.get("inputs", [])) != refs_by_path(inputs):
            differing.append("inputs")
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
                "release_reason": (payload.get("release") or {}).get("reason"),
                "review_ref": context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
                "review_outcome": review["outcome"],
                "partition_class": classify(RECENSOR, review["outcome"]).value,
                "coverage": coverage,
            }
        )
    links = current_links(context, page_breaks(exemplar_page_ids(context), acts), by_id, pages)
    receipt = build_recensor_reading_receipt(
        run_id=context.tree.run_id,
        config_digest=context.run["config_digest"],
        page_reading_refs=[
            {"page_ordinal": ordinal, "reading_ref": pages[ordinal]["reading_ref"]}
            for ordinal in sorted(pages)
        ],
        items=items,
        continuation_links=links,
    )
    context.tree.write_recensor_partition_receipt(receipt)
