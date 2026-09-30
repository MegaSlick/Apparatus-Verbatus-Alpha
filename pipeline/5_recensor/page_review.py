"""The Recensor's page path: one review for every unit a page-read run counts.

Under the sealed `reading_unit = "page"` the Perlector read each sealed page
whole and established the acts on it (`pipeline/4_perlector/CONTRACT.md`,
"Page reading"). The units counted here are `common.stage.reading_acts`, whose
rows the shared denominator has already proven from the Perlector's records,
page accounting and hold codes included. On top of each row this stage adds
what it measures itself:

- the witness floor, from each configured page witness's latest
  `page-testimonium` for the row's page (every unit on a page shares it);
- residual ink, from the sealed page's own pixels against every reading region
  cut on it, `act` and `other` alike;
- for a page the reading says holds no act, whether that is confirmed;
- the answer's continuation flags, recorded as `continuation-link` records.

A unit is `accepted` only when its row is `read`, the floor holds, no chair is
unresolved and the page's ink is covered; every other unit is `held-for-review`
with every reason named. Nothing here reads, repairs or chooses text, and
nothing asks for a recovery: `recoveries_used` is always 0.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from common.chairs.models import ChairIdentity
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.identities import attempt_id
from common.contracts.outcomes import WITNESS_READING_OUTCOMES, classify, witness_coverage
from common.contracts.stages import ATTESTATORES, PERLECTOR, RECENSOR
from common.native_witness import validate_page_testimonium_payload
from common.page_accounting import PASS
from common.recensor_receipt import build_recensor_reading_receipt
from common.stage import (
    PAGE_ACCOUNTING_KIND,
    PAGE_BLANK_CLASS,
    PAGE_BLANK_HOLD,
    exemplar_page_ids,
    latest_attempt,
    latest_per_chair,
    reading_denominator,
    stage_manifest,
)

PAGE_TESTIMONIUM_KIND: Final = "page-testimonium"
CONTINUATION_LINK_KIND: Final = "continuation-link"
CONTINUATION_LINK_SCHEMA: Final = "recensor-continuation-link.v1"
ACCEPTED: Final = "accepted"
HELD: Final = "held-for-review"
CONFIRMED_BLANK: Final = "confirmed-blank"

# A read page whose entries are all `other`: the reading says the page holds no
# act, which this stage confirms or holds as it does a blank page.
NO_ACT_HOLD: Final = "no-act-on-page-unconfirmed"

# The codes this stage adds to a unit's own, each with the sentence its reason uses.
UNDER_WITNESSED: Final = "under-witnessed"
UNRESOLVED_WITNESS: Final = "unresolved-witness"
RESIDUAL_INK: Final = "residual-ink"
RESIDUAL_INK_NOT_MEASURABLE: Final = "residual-ink-not-measurable"
RESIDUAL_INK_NOT_MEASURED: Final = "residual-ink-not-measured"
ASSESSMENT_MALFORMED: Final = "uncertainty-assessment-malformed"

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
        "recoveries_used",
    }
)
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


# --- the page witnesses ------------------------------------------------------------


def declared_page_witness_chairs(context) -> set[str]:
    """The sealed roster's page-scoped witnesses, read here rather than trusted upstream."""
    roster = context.witness_chairs
    # Exact `str`, not `isinstance`: a subclass could override hashing or
    # formatting used below.
    if (
        not isinstance(roster, list)
        or any(type(chair) is not str for chair in roster)
        or len(roster) != len(set(roster))
    ):
        raise SchemaRefusal(
            "the sealed witness roster is not a unique list of chair names. Page-witness scope "
            "cannot be derived from this run authority. Start a new run from the sealed models "
            "configuration; do not edit the existing run"
        )
    configured = context.registry.config.chairs
    unknown = set(roster) - set(configured)
    if unknown:
        raise SchemaRefusal(
            "the sealed witness roster names chair(s) absent from the current models "
            "configuration: "
            f"{sorted(unknown)} not in {sorted(configured)}. The run authority and current models "
            "configuration do not describe the same witness set. Reopen the run with its original "
            "models configuration or start a new run; do not edit sealed evidence"
        )
    return {
        chair
        for chair in roster
        if isinstance(configured[chair], ChairIdentity)
        and configured[chair].witness_scope == "page"
    }


def current_page_testimonia(context, chairs: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Each page's latest `page-testimonium` per chair, each validated, by page id.

    A page no witness testified to is absent. A page some chair testified to
    must carry every configured page witness and no other, as the Perlector
    required when it read the page.
    """
    by_page: dict[str, list[dict[str, Any]]] = {}
    for entry in stage_manifest(context, ATTESTATORES)["artifacts"]:
        if entry["kind"] != PAGE_TESTIMONIUM_KIND:
            continue
        record = context.tree.read_artifact(
            ATTESTATORES, PAGE_TESTIMONIUM_KIND, entry["artifact_id"]
        )
        try:
            validate_page_testimonium_payload(
                record.get("payload"),
                testimonium_id=record.get("artifact_id"),
                read_bytes=context.tree.read_bytes,
            )
        except SchemaRefusal as error:
            raise FatalAccounting(
                f"page Testimonium {record.get('artifact_id')!r} is not a valid page "
                f"Testimonium: {error}"
            ) from error
        by_page.setdefault(record["subject_id"], []).append(record)
    current = {
        page_id: latest_per_chair(records, f"page Testimonium for page {page_id}")
        for page_id, records in by_page.items()
    }
    for page_id, records in current.items():
        present = {record["payload"]["chair"] for record in records}
        if present - chairs:
            raise FatalAccounting(
                f"page {page_id} carries page Testimonia from chair(s) {sorted(present - chairs)}, "
                "which this run did not seal as page witnesses"
            )
        if chairs - present:
            raise FatalAccounting(
                f"page {page_id} has no current page Testimonium for configured page witness(es) "
                f"{sorted(chairs - present)}; its witness floor cannot be counted over a "
                "shortened roster"
            )
    return current


def page_witness_coverage(records: list[dict[str, Any]], floor: int) -> dict[str, Any]:
    """One page's witness coverage, in the shape the v3 receipt recomputes.

    `witness_coverage` over each chair's current outcome, judged on page reads:
    a page-read run's witnesses read the whole page, so none is attached to an
    act, and the floor counts chairs that read the page (`read` or
    `genuinely-empty`). `health_unrecorded` counts reading chairs whose
    Testimonium records no truncation state; `shortfalls` the failed and
    truncated ones. A page no witness testified to has no configured outcome
    and is under-witnessed.
    """
    outcomes = {record["payload"]["chair"]: record["outcome"] for record in records}
    base = witness_coverage(outcomes, floor)
    reading = [record for record in records if record["outcome"] in WITNESS_READING_OUTCOMES]
    truncation = [_health(record).get("truncated") for record in reading]
    return {
        "configured": base["configured"],
        "floor": base["floor"],
        "by_outcome": base["by_outcome"],
        "by_class": base["by_class"],
        "under_witnessed": len(reading) < floor,
        "unresolved_chairs": base["unresolved_chairs"],
        "health_unrecorded": sum(1 for state in truncation if state is None),
        "shortfalls": {
            "failed": base["by_outcome"].get("failed", 0),
            "truncated": sum(1 for state in truncation if state is True),
            "unaligned": 0,
        },
    }


def _health(record: dict[str, Any]) -> dict[str, Any]:
    health = record["payload"].get("content_health")
    return health if isinstance(health, dict) else {}


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


def page_coverage_of(ordinal: int, findings: dict[int, dict]) -> dict[str, list[int]]:
    """One page's residual-ink fact, in the shape every review records."""
    finding = findings.get(ordinal)
    unmeasurable = finding is not None and finding.get("ink_measurable") is False
    checked = finding is not None and not unmeasurable
    return {
        "checked_pages": [ordinal] if checked else [],
        "flagged_pages": [ordinal] if checked and finding.get("flagged") else [],
        "unmeasurable_pages": [ordinal] if unmeasurable else [],
    }


# --- a page that holds no act -------------------------------------------------------


def _accounting(context, act: dict) -> dict[str, Any] | None:
    """The unit's page accounting record, or `None` for a page with none."""
    reference = act["accounting_ref"]
    if reference is None:
        return None
    return context.tree.read_artifact_reference(
        reference, stage=PERLECTOR, kind=PAGE_ACCOUNTING_KIND, subject_id=act["page_id"]
    )


def confirmation(accounting: dict | None, records: list[dict], *, blank: bool) -> dict[str, Any]:
    """Whether a page the reading says holds no act is confirmed so, and why not.

    Both shapes need the page accounting's rules (d), (e) and (f) to pass:
    every detected line, every witness's text and the page's ink are accounted
    for by the page's readings. A blank page also needs no detected line at
    all and every witness that read the page to report its text blank.
    """
    rules = (accounting or {}).get("rules", {})
    statuses = {rule: rules.get(rule, {}).get("status") for rule in ("d", "e", "f")}
    lines = len((accounting or {}).get("lines") or [])
    witnesses = [
        {
            "chair": record["payload"]["chair"],
            "outcome": record["outcome"],
            "blank": _health(record).get("blank"),
        }
        for record in records
    ]
    failures = [
        f"page accounting rule ({rule}) is {status or 'absent'}, not pass"
        for rule, status in statuses.items()
        if status != PASS
    ]
    if blank:
        if lines:
            failures.append(f"Surya detected {lines} line(s) on the page")
        reading = [w for w in witnesses if w["outcome"] in WITNESS_READING_OUTCOMES]
        if not reading:
            failures.append("no witness read the page")
        failures.extend(
            f"witness {w['chair']} read the page and does not report it blank"
            for w in reading
            if w["outcome"] == "read" and w["blank"] is not True
        )
    return {
        "confirms": "page-blank" if blank else "no-act-on-page",
        "rules": statuses,
        "surya_lines": lines,
        "witnesses": witnesses,
        "confirmed": not failures,
        "failures": failures,
    }


# --- one review --------------------------------------------------------------------


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


def own_findings(
    coverage: dict, page_coverage: dict, assessment: dict | None, ordinal: int
) -> list[tuple[str, str]]:
    """This stage's own reasons to hold a unit, as `(code, sentence)` pairs."""
    findings = []
    if coverage["under_witnessed"]:
        reads = sum(coverage["by_outcome"].get(o, 0) for o in WITNESS_READING_OUTCOMES)
        findings.append(
            (
                UNDER_WITNESSED,
                f"{reads} page witness(es) read page {ordinal} against a floor of "
                f"{coverage['floor']}",
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
    return findings


def review_of(
    act: dict,
    *,
    coverage: dict,
    page_coverage: dict,
    assessment: dict | None,
    confirmed: dict | None,
) -> tuple[str, dict[str, Any]]:
    """The outcome and payload of one unit's review, without its attempt ordinal."""
    ordinal = act["page_ordinal"]
    own = own_findings(coverage, page_coverage, assessment, ordinal)
    row_codes = list(act["hold_codes"])
    releasable = {PAGE_BLANK_HOLD, NO_ACT_HOLD}
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
        and set(row_codes) <= releasable
    )
    if released:
        release = {
            "hold_codes": row_codes,
            "reason": (
                "confirmed: the page accounting's rules (d), (e) and (f) pass"
                + (
                    ", no line was detected and every witness that read the page reports it blank"
                    if confirmed["confirms"] == "page-blank"
                    else ", so every reading on the page is accounted for and none is an act"
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


# --- continuation --------------------------------------------------------------------


def continuation_links(pages: dict[int, str], acts: list[dict]) -> list[tuple[str, dict]]:
    """Every page break an answer flags, as `(subject, payload)`, in page order.

    The last entry of page p and the first of page p+1 are the break's two
    sides; either side's flag records the break, `agreed` only when both say
    so. A side with no entry (a page not read, blank, or outside the run) is
    null. The link holds no unit: Train 3 builds acts across pages from it.
    """
    entries: dict[int, list[dict]] = {}
    for act in acts:
        if act["n"] is not None:
            entries.setdefault(act["page_ordinal"], []).append(act)
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


# --- the pass --------------------------------------------------------------------------


def review_pages(
    context,
    denominator: dict[str, Any],
    *,
    page_coverage_findings: Callable[..., dict[int, dict]],
    publish_review: Callable[..., dict],
    current_review: Callable[[Any, str], dict | None],
) -> int:
    """Review every counted unit and record every flagged page break; return the held count.

    Every fact is measured before anything is published, so a refusal found
    at a later unit never leaves a partial set of reviews behind.
    """
    pages, acts = denominator["pages"], denominator["acts"]
    chairs = declared_page_witness_chairs(context)
    testimonia = current_page_testimonia(context, chairs)
    findings = page_coverage_findings(
        context, regions=reading_regions_by_page(context, pages, acts)
    )
    page_ids = exemplar_page_ids(context)
    floor = context.witness_floor
    planned = []
    for act in acts:
        records = testimonia.get(act["page_id"], [])
        accounting = _accounting(context, act)
        _require_accounted_testimonia(context, act, accounting, records)
        confirmed = None
        if PAGE_BLANK_HOLD in act["hold_codes"] or NO_ACT_HOLD in act["hold_codes"]:
            confirmed = confirmation(
                accounting["payload"] if accounting else None,
                records,
                blank=act["class"] == PAGE_BLANK_CLASS,
            )
        outcome, payload = review_of(
            act,
            coverage=page_witness_coverage(records, floor),
            page_coverage=page_coverage_of(act["page_ordinal"], findings),
            assessment=_assessment(context, act),
            confirmed=confirmed,
        )
        inputs = [
            reference
            for reference in (
                act["reading_ref"],
                act["accounting_ref"],
                act["region_ref"],
                act["perlectio_ref"],
            )
            if reference is not None
        ] + [
            context.artifact_ref(ATTESTATORES, PAGE_TESTIMONIUM_KIND, record["artifact_id"])
            for record in records
        ]
        planned.append((act, outcome, payload, inputs))
    links = continuation_links(page_ids, acts)

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
    by_id = {act["act_id"]: act for act in acts}
    for subject, payload in links:
        inputs = [
            by_id[act_id]["perlectio_ref"]
            for act_id in (payload["from_act_id"], payload["to_act_id"])
            if act_id is not None
        ]
        context.publish(
            kind=CONTINUATION_LINK_KIND,
            subject_id=subject,
            outcome=ACCEPTED if payload["agreed"] else HELD,
            attempt=attempt_id(subject, "link", 1),
            inputs=inputs,
            payload=payload,
        )
    return held


def _require_accounted_testimonia(
    context, act: dict, accounting: dict | None, records: list[dict]
) -> None:
    """The witnesses counted for the floor are the ones the page accounting measured."""
    if accounting is None:
        return
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


def write_reading_receipt(context) -> None:
    """Rebuild the v3 partition receipt from disk: the units, their reviews, their coverage.

    The units are re-derived through `reading_denominator`, each review's
    coverage is recomputed from the Testimonia, and a Recensor review or
    recovery request of anything outside `reading_acts` is refused.
    """
    for stage in (ATTESTATORES, PERLECTOR, RECENSOR):
        if not context.tree.manifest_agrees_with_disk(stage):
            raise FatalAccounting(
                f"the stored {stage} manifest disagrees with its on-disk artifacts; the "
                "Recensor partition receipt refuses a cache as its denominator"
            )
    denominator = reading_denominator(context)
    pages, acts = denominator["pages"], denominator["acts"]
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
    testimonia = current_page_testimonia(context, declared_page_witness_chairs(context))
    items = []
    for act_id, act in sorted(by_id.items()):
        if not reviews[act_id]:
            raise FatalAccounting(f"unit {act_id} ({act['act_key']}) has no Recensor review")
        review = latest_attempt(
            reviews[act_id], f"Recensor review of {act_id}", operation="recense"
        )
        payload = review.get("payload")
        coverage = page_witness_coverage(testimonia.get(act["page_id"], []), context.witness_floor)
        if (
            not isinstance(payload, dict)
            or payload.get("act_key") != act["act_key"]
            or payload.get("coverage") != coverage
        ):
            raise FatalAccounting(
                f"Recensor review of {act_id} does not retain the unit key and witness coverage "
                "recomputed from disk"
            )
        items.append(
            {
                "act_id": act_id,
                "act_key": act["act_key"],
                "page_disposition": act["disposition"],
                "review_ref": context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
                "review_outcome": review["outcome"],
                "partition_class": classify(RECENSOR, review["outcome"]).value,
                "coverage": coverage,
            }
        )
    receipt = build_recensor_reading_receipt(
        run_id=context.tree.run_id,
        config_digest=context.run["config_digest"],
        page_reading_refs=[pages[ordinal]["reading_ref"] for ordinal in sorted(pages)],
        items=items,
    )
    context.tree.write_recensor_partition_receipt(receipt)
