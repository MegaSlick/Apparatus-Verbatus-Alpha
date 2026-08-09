"""Recensor: establishes that the text is complete. It establishes no text.

It reconciles what the proposal seal expected against what actually happened, and
gives every expected act exactly one outcome. Three of those outcomes end the act
here; two send it onward. Nothing it does touches a reading.

**Recovery is bounded and recorded.** The budget comes from `config/recovery.toml`,
whose absolute cap is Tyrel's "PURE ABSOLUTE, STOP AT 3". When the budget is spent
the act is held for review — it is never re-rolled until it looks better, because
recovery recovers coverage and not quality (GOVERNANCE 11). Every request is an
artifact, so nothing can disappear inside a loop.

**It does not select among witnesses.** Witness outcomes are aggregated into a
coverage record and used for exactly two things: marking an act under-witnessed,
and forcing the run's aggregate visibly partial. They never decide an act's
outcome, and no count of agreeing chairs can change a reading.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from residual_ink import page_residual_ink  # noqa: E402

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    OutcomeClass,
    classify,
    terminal_category,
    witness_coverage,
)
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
)
from common.recensor_receipt import build_recensor_partition_receipt  # noqa: E402
from common.recovery import (  # noqa: E402
    FALLBACK_RECROP,
    RECOVERY_KINDS,
    load_recovery_policy,
    recovery_kind_budget,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    latest_attempt,
    latest_per_chair,
    open_context,
    reading_basis_regions,
    recovery_region_count,
    run_stage,
    scenario_for,
    stage_parser,
)


def recovery_budget(path: str) -> dict:
    """The run-bound recovery policy, read through the common validator."""
    return load_recovery_policy(path)


def designator_hold(context, act_id: str) -> tuple[dict, str]:
    """The Designator's hold record for a seal-held act, and its path.

    Refused loudly when absent: a seal entry that says `held` with no record of
    why is a claim with no evidence, and absent evidence never reads cleaner
    than damaged evidence.
    """
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "hold" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "hold", entry["artifact_id"])
            return record, entry["relative_path"]
    raise FatalAccounting(
        f"the seal holds act {act_id} but the Designator published no hold record "
        "saying why; a hold with no evidence cannot be reviewed"
    )


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def chair_outcomes(context, act_id: str) -> dict[str, str]:
    """The current outcome per chair: the latest attempt, with its honest status.

    Derived, never stored as a pointer. A failed attempt 2 over a successful
    attempt 1 therefore reads as `failed`, with attempt 1 intact as history.
    `latest_per_chair` is the one shared derivation of "current" per chair,
    also used by `pipeline/4_perlector/run.py::testimonia_of` over the same
    upstream artifacts, so the two consumers cannot drift on what "current" means.
    """
    records = artifacts_for(context, ATTESTATORES, "testimonium", act_id)
    return {
        record["payload"]["chair"]: record["outcome"]
        for record in latest_per_chair(records, f"testimonium for {act_id}")
    }


def blank_corroboration(coverage: dict, outcomes: dict[str, str]) -> list[str] | None:
    """The corroborating chairs if every witness that read this act's ink agrees
    nothing was there, or `None` if the evidence does not support that.

    ARCHITECTURE and spec 09 both name blank confirmation as a candidate
    completeness check ("a zero-output unit is diagnosed, then either sealed
    confirmed-blank with evidence or held unresolved-with-evidence"), and the
    old pipeline's own hard-won lesson (window pass, 2026-08-05; see
    `/out/report.md`) is that a blank verdict may never rest on fewer than
    several genuinely INDEPENDENT completed reads, and never on a reader's own
    second opinion. This is **unanimity about an absence, never a selection
    among presences** — the distinction that kept the old pipeline's own blank
    ladder G3-clean even under adversarial review: nothing here chooses a
    reading or establishes text. The Perlector's own direct examination of the
    ink (autopsia, not testimony) already produced `no-readable-text`; this
    only asks whether the witnesses corroborate or contradict that finding. A
    single chair that actually read text is exactly the disagreement GOALS 1
    says must never be silently resolved — it holds the act for a human,
    never outvotes the dissenter.

    Requires the configured witness floor to have been met by chairs that
    actually completed a read (not merely configured), and no chair still
    unresolved — a floor met only by `failed`/`dead` chairs, or a run that
    has not yet heard from every configured chair, corroborates nothing.
    """
    if coverage["under_witnessed"] or coverage["unresolved_chairs"]:
        return None
    completed = sorted(
        chair for chair, outcome in outcomes.items() if outcome in WITNESS_READING_OUTCOMES
    )
    if not completed or any(outcomes[chair] != "genuinely-empty" for chair in completed):
        return None
    return completed


def validate_chair_coverage(context, act_id: str, floor: int) -> dict[str, object]:
    """Return one act's coverage after refusing ambiguous witness history.

    This is deliberately callable before Recensor publishes anything.  A bad
    testimonium for the second act used to leave a review for the first act on
    disk before the ambiguity was discovered.  That unpublished fragment was
    not a completed stage, but it was still an easy thing for a later retry to
    mistake for history.  Validate the entire witness denominator first.
    """
    outcomes = chair_outcomes(context, act_id)
    sealed = set(context.witness_chairs)
    missing = sealed - set(outcomes)
    if missing:
        raise FatalAccounting(
            f"act {act_id} has no outcome for configured chair(s) {sorted(missing)}. "
            "Every configured chair gets an explicit outcome for every act"
        )
    unsealed = set(outcomes) - sealed
    if unsealed:
        raise FatalAccounting(
            f"act {act_id} carries a testimonium from chair(s) {sorted(unsealed)}, "
            f"which this run was not sealed with. `run.json` names its witness "
            "chairs and nothing may add one after the seal"
        )
    return witness_coverage(outcomes, floor)


def preflight_witness_denominator(context, floor: int) -> None:
    """Refuse all witness ambiguity before this stage writes its first review."""
    for act in expected_acts(context):
        validate_chair_coverage(context, act["act_id"], floor)


def _payload(record: dict, what: str) -> dict:
    """One record payload, refusing an untyped recovery fact before using it."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise FatalAccounting(f"{what} has no object payload")
    return payload


def recovery_state(context, act_id: str, budget: dict) -> dict:
    """Reconcile this act's requested, cut, and reviewed recovery history.

    A recovery request is not evidence that its recrop happened, and a recovery
    crop is not evidence that the Perlector read it.  The three append-only
    histories must therefore agree before another Recensor review can be written:
    exactly one recovery-requested review per request, at most one recrop per
    request, and a later Perlectio for every recrop.  No branch here establishes
    text or selects among readings; disagreement is fatal accounting.

    The counters each request recorded are reconciled against the requests that
    preceded it, per kind and in total, rather than trusted because they sit
    inside a self-hashed payload: the self-hash proves nobody edited the record
    after publication, not that the number was ever right.
    """
    requests = artifacts_for(context, RECENSOR, "recovery-request", act_id)
    request_refs: dict[str, dict] = {}
    requests_by_ordinal: dict[int, dict] = {}
    requests_by_kind: dict[str, list[dict]] = {kind: [] for kind in RECOVERY_KINDS}
    for request in requests:
        payload = _payload(request, f"recovery request for {act_id}")
        ordinal = payload.get("attempt_ordinal")
        if (
            request.get("outcome") != "recovery-requested"
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or request.get("attempt_id") != attempt_id(act_id, "recover", ordinal)
        ):
            raise FatalAccounting(
                f"recovery request for {act_id} does not carry its bound recovery ordinal"
            )
        if ordinal in requests_by_ordinal:
            raise FatalAccounting(
                f"act {act_id} carries two recovery requests for ordinal {ordinal}; recovery "
                "has no rule for choosing one"
            )
        # ARCHITECTURE and spec 09 both name two distinct recovery operations — a
        # Designator recrop and a Perlector page-level/continuation-aware reread —
        # and `config/recovery.toml` already budgets them separately. A request
        # that does not say which one it means cannot be checked against the
        # kind-specific budget or dispatched to the right owning stage, so this is
        # refused here rather than left for the Designator or orchestrator to
        # guess at.
        kind = payload.get("recovery_kind")
        if kind not in RECOVERY_KINDS:
            raise FatalAccounting(
                f"recovery request for {act_id} does not carry a recognized recovery_kind "
                f"(one of {sorted(RECOVERY_KINDS)}); a request must name which recovery "
                "operation it means"
            )
        requests_by_ordinal[ordinal] = request
        request_refs[request["artifact_id"]] = context.artifact_ref(
            RECENSOR, "recovery-request", request["artifact_id"]
        )

    # Ordinals are contiguous from 1, and each request's recorded counters agree
    # with the requests before it. A missing attempt renumbered away would let a
    # spent budget read as an unspent one, which is the one arithmetic this
    # bounded loop cannot afford to get wrong.
    expected_ordinals = set(range(1, len(requests_by_ordinal) + 1))
    if set(requests_by_ordinal) != expected_ordinals:
        raise FatalAccounting(
            f"act {act_id} has non-contiguous recovery request ordinal(s) "
            f"{sorted(requests_by_ordinal)}; a missing attempt may not be renumbered away"
        )
    ordered_requests = [requests_by_ordinal[ordinal] for ordinal in sorted(requests_by_ordinal)]
    previously_used_by_kind = {kind: 0 for kind in RECOVERY_KINDS}
    for total_used, request in enumerate(ordered_requests):
        payload = _payload(request, f"recovery request for {act_id}")
        kind = payload["recovery_kind"]
        kind_allowed = recovery_kind_budget(budget, kind)
        counters = ("budget_allowed", "budget_used", "kind_budget_allowed", "kind_budget_used")
        if (
            any(
                not isinstance(payload.get(field), int) or isinstance(payload.get(field), bool)
                for field in counters
            )
            or payload.get("recovery_policy") != budget
            or payload.get("budget_allowed") != budget["allowed"]
            or payload.get("budget_used") != total_used
            or payload.get("kind_budget_allowed") != kind_allowed
            or payload.get("kind_budget_used") != previously_used_by_kind[kind]
        ):
            raise FatalAccounting(
                f"recovery request for {act_id} has a recorded total or kind budget that does "
                "not reconcile to its preceding immutable requests"
            )
        requests_by_kind[kind].append(request)
        previously_used_by_kind[kind] += 1

    if len(ordered_requests) > budget["allowed"] or len(ordered_requests) > budget["absolute_cap"]:
        raise FatalAccounting(
            f"act {act_id} has {len(ordered_requests)} recovery request(s), above its sealed "
            "total budget"
        )
    for kind, requests_of_kind in requests_by_kind.items():
        if len(requests_of_kind) > recovery_kind_budget(budget, kind):
            raise FatalAccounting(
                f"act {act_id} has {len(requests_of_kind)} {kind!r} request(s), above that "
                "kind's sealed budget"
            )

    reviews_by_request = {request_id: [] for request_id in request_refs}
    for review in artifacts_for(context, RECENSOR, "review", act_id):
        if review.get("outcome") != "recovery-requested":
            continue
        payload = _payload(review, f"recovery-requested review of {act_id}")
        ordinal = payload.get("attempt_ordinal")
        request_ref = payload.get("recovery_request_ref")
        matching_request = next(
            (
                request_id
                for request_id, reference in request_refs.items()
                if request_ref == reference
            ),
            None,
        )
        if (
            matching_request is None
            or request_ref not in review.get("inputs", [])
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or review.get("attempt_id") != attempt_id(act_id, "recense", ordinal)
        ):
            raise FatalAccounting(
                f"recovery-requested review of {act_id} has no exact matching recovery request"
            )
        request = requests_by_ordinal.get(ordinal)
        if request is None or request["artifact_id"] != matching_request:
            raise FatalAccounting(
                f"recovery-requested review of {act_id} disagrees with its request ordinal"
            )
        request_payload = _payload(request, f"recovery request for {act_id}")
        if (
            payload.get("perlectio_ref") != request_payload.get("perlectio_ref")
            or payload.get("recovery_kind") != request_payload.get("recovery_kind")
            or payload.get("recovery_policy") != budget
        ):
            raise FatalAccounting(
                f"recovery-requested review of {act_id} does not name the Perlectio, recovery "
                "kind, and policy its request assessed"
            )
        reviews_by_request[matching_request].append(review)

    missing_reviews = [
        request_id for request_id, reviews in reviews_by_request.items() if len(reviews) != 1
    ]
    if missing_reviews:
        raise FatalAccounting(
            f"act {act_id} has recovery request(s) without exactly one matching "
            f"recovery-requested review {sorted(missing_reviews)}; a crash between the two "
            "publications is named rather than turned into a later ordinal"
        )

    regions = artifacts_for(context, DESIGNATOR, "region", act_id)
    recrops_by_request = {request_id: [] for request_id in request_refs}
    recovery_regions = []
    # Validate the whole origin vocabulary through the one shared reader, so this
    # stage and the two downstream ones cannot disagree about what counts as a
    # recovery crop. The count is discarded here; the per-region binding below is
    # what this function additionally needs and the shared reader does not do.
    recovery_region_count(act_id, regions)
    for region in regions:
        payload = _payload(region, f"Designator region of {act_id}")
        if payload.get("origin") != "recovery":
            continue
        inputs = region.get("inputs")
        if not isinstance(inputs, list):
            raise FatalAccounting(f"recovery region of {act_id} has no input list")
        matches = [
            request_id for request_id, reference in request_refs.items() if reference in inputs
        ]
        if len(matches) != 1:
            raise FatalAccounting(
                f"recovery region of {act_id} is not bound to exactly one recorded recovery request"
            )
        recovery_regions.append(region)
        recrops_by_request[matches[0]].append(region)

    repeated_recrops = [
        request_id for request_id, recrops in recrops_by_request.items() if len(recrops) > 1
    ]
    if repeated_recrops:
        raise FatalAccounting(
            f"act {act_id} has more than one recovery crop for request(s) "
            f"{sorted(repeated_recrops)}; one request may not silently create a second reread"
        )
    return {
        "requests": ordered_requests,
        "regions": regions,
        "recovery_regions": recovery_regions,
        "requests_by_kind": requests_by_kind,
        "outstanding_request_ids": [
            request_id for request_id, recrops in recrops_by_request.items() if not recrops
        ],
    }


def preflight_recovery_history(context, budget: dict) -> None:
    """Name every broken recovery pair before publishing another review."""
    for act in expected_acts(context):
        recovery_state(context, act["act_id"], budget)


def _basis_facts(region: dict, what: str) -> dict:
    """The read-side facts that identify one Designator crop without its witness flag."""
    if not isinstance(region, dict):
        raise FatalAccounting(f"{what} is not an object")
    fields = (
        "region_id",
        "image_path",
        "image_sha256",
        "source_page_ordinal",
        "source_page_id",
        "transform",
    )
    facts = {field: region.get(field) for field in fields}
    if not isinstance(facts["region_id"], str) or not facts["region_id"]:
        raise FatalAccounting(f"{what} has no region identity")
    if not isinstance(facts["image_path"], str) or not facts["image_path"]:
        raise FatalAccounting(f"{what} has no crop image path")
    return facts


def _expected_basis_facts(region: dict, act_id: str) -> dict:
    payload = _payload(region, f"Designator region of {act_id}")
    transform = payload.get("transform")
    return _basis_facts(
        {
            "region_id": payload.get("region_id"),
            "image_path": payload.get("image_path"),
            "image_sha256": payload.get("image_sha256"),
            "source_page_ordinal": transform.get("source_page_ordinal")
            if isinstance(transform, dict)
            else None,
            "source_page_id": transform.get("source_page_id")
            if isinstance(transform, dict)
            else None,
            "transform": transform,
        },
        f"Designator region of {act_id}",
    )


def recensor_continuation_link(regions: list[dict], act_id: str) -> dict:
    """The Recensor's own continuation fact, derived from evidence alone.

    ARCHITECTURE and spec 09 agree the Recensor's link is the authoritative
    continuation relation; the Designator's proposal seal carries
    `has_continuation` as its own PROPOSAL, not a settled fact this stage may
    inherit unexamined ("the Designator proposes continuations"). This derives
    the answer directly from the original proposal regions actually cut — never
    from the seal's flag — so the seal's claim can be checked against it rather
    than trusted in its place. A genuine continuation cuts two proposal regions
    on two distinct source pages; counting bare regions without checking the
    pages would call two regions on the SAME page a continuation, which they
    are not.
    """
    facts = [
        _expected_basis_facts(region, act_id)
        for region in regions
        if _payload(region, f"Designator region of {act_id}").get("origin") == "proposal"
    ]
    page_ordinals = sorted({row["source_page_ordinal"] for row in facts})
    return {
        "is_continuation": len(page_ordinals) > 1,
        "page_ordinals": page_ordinals,
        "region_ids": sorted(row["region_id"] for row in facts),
    }


def reconcile_continuation(act: dict, continuation_link: dict, act_id: str) -> bool:
    """Reconcile the seal's proposed continuation against the Recensor's own link.

    Returns whether the act's reading covers only part of a claimed
    continuation — never established as a whole act while that is true. Raises
    when the seal instead denies a continuation its own evidence already
    proves: silently agreeing with a seal that under-claims against the
    evidence would let the Designator's proposal override the Recensor's own
    authoritative continuation fact, which is exactly what stage ownership of
    this relation (ARCHITECTURE, spec 09) exists to prevent.
    """
    if act["has_continuation"] and not continuation_link["is_continuation"]:
        return True
    if not act["has_continuation"] and continuation_link["is_continuation"]:
        raise FatalAccounting(
            f"act {act_id}'s own proposal regions span pages "
            f"{continuation_link['page_ordinals']}, but the Designator's sealed "
            "proposal claims no continuation for it; the Recensor's own "
            "reconciliation is the authoritative continuation fact and may not "
            "silently agree with a seal that under-claims against the evidence"
        )
    return False


def regions_by_source_page(context) -> dict[int, list[dict]]:
    """Every currently-cut Designator region's page-pixel bounds, by source page.

    Proposal and recovery together, from every act that touches a page — the
    residual-ink check (`residual_ink.py`) asks about the PAGE's own pixels,
    never any one act's denominator, so a region cut for a different act on the
    same page still counts as coverage here. A page nobody cut a region on at
    all has no entry: there is no evidence to read a region's absence against
    yet (see HANDOFF.md and `/out/report.md`).
    """
    by_page: dict[int, list[dict]] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        payload = record.get("payload")
        transform = payload.get("transform") if isinstance(payload, dict) else None
        if not isinstance(transform, dict):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has no object transform"
            )
        ordinal = transform.get("source_page_ordinal")
        bounds = transform.get("bounds")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(bounds, dict)
        ):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has an invalid transform"
            )
        by_page.setdefault(ordinal, []).append(bounds)
    return by_page


def sealed_page_images(context) -> dict[int, dict]:
    """Every sealed Exemplar page's own artifact, by ordinal."""
    pages: dict[int, dict] = {}
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        if record["outcome"] != "sealed":
            continue
        ordinal = record["payload"].get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                f"Exemplar page {record.get('artifact_id')} carries no integer ordinal"
            )
        pages[ordinal] = record
    return pages


def page_coverage_findings(context) -> dict[int, dict]:
    """Residual-ink findings for every sealed page with at least one region cut
    on it (ARCHITECTURE's candidate list: "a residual-ink check whose input is
    the page image itself, never the proposal set"), computed once per run and
    reused by every act that reaches one of these pages.

    Deterministic core, cheapest instrument first: pure geometry over the
    page's own pixels, entirely independent of any witness or reading. A page
    with zero regions cut on it at all is not checked here — see
    `regions_by_source_page`.
    """
    regions = regions_by_source_page(context)
    if not regions:
        return {}
    pages = sealed_page_images(context)
    findings: dict[int, dict] = {}
    for ordinal, bounds in regions.items():
        page = pages.get(ordinal)
        if page is None:
            raise FatalAccounting(
                f"a Designator region names source page {ordinal}, which the Exemplar "
                "did not seal; a crop of unsealed pixels is invariant #10's imbalance"
            )
        image_bytes = context.tree.read_bytes(page["payload"]["image_path"])
        findings[ordinal] = page_residual_ink(image_bytes, bounds)
    return findings


def flagged_pages_for(act_regions: list[dict], findings: dict[int, dict]) -> list[int]:
    """The source pages this act's own current regions touch that are flagged.

    Every page the act's proposal or recovery regions were cut from, not only
    its primary `page_ordinal` — a continuation's far-side page is examined
    exactly as its near side is, and a successful recovery crop that reaches
    previously-missed ink clears the page's finding on the very next pass.
    """
    ordinals = sorted(
        {
            region["payload"]["transform"]["source_page_ordinal"]
            for region in act_regions
            if isinstance(region.get("payload"), dict)
            and isinstance(region["payload"].get("transform"), dict)
        }
    )
    return [ordinal for ordinal in ordinals if findings.get(ordinal, {}).get("flagged")]


def _reconcile_reading_regions(reading: dict, regions: list[dict], act_id: str) -> list[dict]:
    """Require a completed Perlectio to name exactly every region currently cut."""
    basis = reading_basis_regions(reading, f"reading of {act_id}")
    expected_by_id = {
        facts["region_id"]: facts
        for facts in (_expected_basis_facts(region, act_id) for region in regions)
    }
    actual_by_id = {
        facts["region_id"]: facts
        for facts in (_basis_facts(region, f"reading of {act_id}") for region in basis)
    }
    if len(expected_by_id) != len(regions) or len(actual_by_id) != len(basis):
        raise FatalAccounting(
            f"act {act_id} repeats a crop identity in its cut or read basis; duplicate evidence "
            "cannot count as recovered coverage"
        )
    if actual_by_id != expected_by_id:
        raise FatalAccounting(
            f"act {act_id}'s latest Perlectio does not name exactly the Designator regions "
            "currently cut for it; a recovery crop may not disappear before it is reread"
        )
    return basis


def _refuse_an_unhandled_designator_terminal(act: dict) -> None:
    """Name a Designator outcome that ends an act but has no handling here yet.

    `held` is not the only terminal Designator outcome. `excluded` and `failed` are
    terminal in the same table (`common/contracts/outcomes.py`), and both stages of
    this file tested only for `held` — so either would fall through to the reading
    path and be reported as "reached the Recensor with no reading at all". That
    message is false: the act was never going to have a reading, and the imbalance
    was invented by the check rather than found by it.

    What review record such an act should get is genuinely undecided — `excluded` is
    approval-bound and `failed` is a refusal, and the Designator emits neither today.
    So this says exactly that, rather than inventing the policy or letting a wrong
    message stand. Whoever teaches the Designator to emit one lands the handling here
    and this refusal stops firing.
    """
    category = terminal_category(DESIGNATOR, act["outcome"])
    if category is None:
        return
    raise FatalAccounting(
        f"act {act['act_id']} carries the terminal Designator outcome {act['outcome']!r} "
        f"({category.value}), which ends the act before any reading — but the Recensor has "
        "no review record for it yet, and nothing may pass through this stage unaccounted "
        "for. Only 'held' is handled today"
    )


def preflight_review_evidence(context, budget: dict) -> None:
    """Validate every readable act before publishing a review for any one of them."""
    for act in expected_acts(context):
        act_id = act["act_id"]
        if act["outcome"] == "held":
            designator_hold(context, act_id)
            continue
        _refuse_an_unhandled_designator_terminal(act)
        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
        if not readings:
            raise FatalAccounting(
                f"act {act_id} reached the Recensor with no reading at all. A unit "
                "in no terminal set is a fatal accounting imbalance (#10)"
            )
        state = recovery_state(context, act_id, budget)
        expected_readings = len(state["recovery_regions"]) + 1
        if len(readings) != expected_readings:
            raise FatalAccounting(
                f"act {act_id} carries {len(readings)} Perlectio attempt(s) for "
                f"{len(state['recovery_regions'])} recovery crop(s); every reread must answer "
                "one recorded recrop and no reading may appear unrequested"
            )
        latest = latest_attempt(readings, f"reading of {act_id}", operation="perlegere")
        context.artifact_ref(PERLECTOR, "perlectio", latest["artifact_id"])
        if classify(PERLECTOR, latest["outcome"]) is OutcomeClass.COMPLETED:
            for region in _reconcile_reading_regions(latest, state["regions"], act_id):
                context.input_ref(region["image_path"])


def write_partition_receipt(context, budget: dict) -> None:
    """Rebuild the scoped Recensor partition receipt from disk, never manifests.

    Spec 09: "a self-hashed run receipt that **recomputes every denominator from
    the artifacts on disk** rather than trusting stage manifests." The mutable
    stage manifests stay a cache, so their agreement with disk is checked before
    either may stand beside a receipt; the denominator itself is rederived
    through `expected_acts`, and every review and coverage record is read afresh
    from the immutable artifacts.

    Its status is deliberately scoped, and the scope is part of the record: it
    speaks for the proposal-act and configured-witness denominators at the moment
    the Recensor reviewed them. It does not claim to be the run's final export
    verdict — the page-level residual-ink and continuation facts are recorded in
    the review payloads this receipt cites, and a page nobody cut a region on is
    outside every denominator here. Claiming otherwise would be exactly the
    "complete" GOVERNANCE 2 refuses.
    """
    for stage in (DESIGNATOR, ATTESTATORES, PERLECTOR, RECENSOR):
        if not context.tree.manifest_agrees_with_disk(stage):
            raise FatalAccounting(
                f"the stored {stage} manifest disagrees with its on-disk artifacts; the "
                "Recensor partition receipt refuses a cache as its denominator"
            )
    acts = expected_acts(context)
    expected_by_id = {act["act_id"]: act for act in acts}
    reviews_by_act: dict[str, list[dict]] = {act_id: [] for act_id in expected_by_id}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] not in {"review", "recovery-request"}:
            continue
        record = context.tree.read_artifact(RECENSOR, entry["kind"], entry["artifact_id"])
        if record["subject_id"] not in expected_by_id:
            raise FatalAccounting(
                f"Recensor {entry['kind']} {record['artifact_id']} names act "
                f"{record['subject_id']!r} outside the proposal-act denominator"
            )
        if entry["kind"] == "review":
            reviews_by_act[record["subject_id"]].append(record)

    proposal_seal_ref = context.artifact_ref(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    items = []
    for act_id in sorted(expected_by_id):
        act = expected_by_id[act_id]
        review = latest_attempt(
            reviews_by_act[act_id], f"Recensor review of {act_id}", operation="recense"
        )
        payload = _payload(review, f"Recensor review of {act_id}")
        coverage = validate_chair_coverage(context, act_id, context.witness_floor)
        if payload.get("act_key") != act["act_key"] or payload.get("coverage") != coverage:
            raise FatalAccounting(
                f"Recensor review of {act_id} does not retain the act key and witness coverage "
                "recomputed from disk"
            )
        recovery_state(context, act_id, budget)
        items.append(
            {
                "act_id": act_id,
                "act_key": act["act_key"],
                "designator_outcome": act["outcome"],
                "review_ref": context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
                "review_outcome": review["outcome"],
                "partition_class": classify(RECENSOR, review["outcome"]).value,
                "coverage": coverage,
            }
        )
    receipt = build_recensor_partition_receipt(
        run_id=context.tree.run_id,
        config_digest=context.run["config_digest"],
        proposal_seal_ref=proposal_seal_ref,
        items=items,
    )
    context.tree.write_recensor_partition_receipt(receipt)


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, RECENSOR, registry_factory=registry_factory)
    budget = recovery_budget(args.recovery_config)

    scenario = scenario_for(context.fixture, context.scenario)
    floor = context.witness_floor

    # This pass must precede publication.  `latest_attempt` refuses duplicate
    # semantic ordinals rather than selecting an arbitrary hash-sorted record;
    # doing that only as each act is published can leave an earlier act's review
    # behind when a later act is malformed.
    preflight_witness_denominator(context, floor)
    preflight_recovery_history(context, budget)
    preflight_review_evidence(context, budget)

    # Deterministic core, cheapest instrument first (spec 09): pure geometry
    # over every sealed page's own pixels, computed once and reused by every
    # act that reaches one of these pages, entirely independent of any
    # witness or reading below.
    page_findings = page_coverage_findings(context)

    held = 0
    for act in expected_acts(context):
        act_id, act_key = act["act_id"], act["act_key"]

        coverage = validate_chair_coverage(context, act_id, floor)

        if act["outcome"] == "held":
            # The Designator could not mark this act out. There is no reading to
            # review and no recovery to request — recovery recovers coverage on
            # sealed ink, and this act's missing ink was never sealed. The act
            # still gets this stage's explicit outcome, so its terminal category
            # derives from a review like every other act's.
            hold, hold_path = designator_hold(context, act_id)
            context.publish(
                kind="review",
                subject_id=act_id,
                outcome="held-for-review",
                attempt=attempt_id(act_id, "recense", 1),
                inputs=[context.input_ref(hold_path)],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": 1,
                    "reason": f"the Designator held this act: {hold['payload']['reason']}",
                    "coverage": coverage,
                    # No region was ever cut for a Designator-held act, so no
                    # proposal evidence exists to derive a continuation fact
                    # from; recorded the same way as every other act's, rather
                    # than omitted, so a reader of review payloads never has to
                    # ask which acts carry this field.
                    "continuation": recensor_continuation_link([], act_id),
                    "recoveries_used": 0,
                    "budget_allowed": budget["allowed"],
                    "absolute_cap": budget["absolute_cap"],
                },
            )
            held += 1
            continue

        _refuse_an_unhandled_designator_terminal(act)

        state = recovery_state(context, act_id, budget)
        if state["outstanding_request_ids"]:
            # The matching review is already the durable record of this hold. A
            # direct Recensor retry must not turn it into a later acceptance while
            # the Designator has not yet cut the requested recovery crop.
            held += 1
            continue

        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
        if not readings:
            raise FatalAccounting(
                f"act {act_id} reached the Recensor with no reading at all. A unit "
                "in no terminal set is a fatal accounting imbalance (#10)"
            )

        # Every review is about one specific Perlectio, not merely the current
        # object a later stage happens to find.  The reference is both an input
        # digest and a payload fact so Archetypus can prove it establishes the
        # exact reading Recensor assessed.
        latest = latest_attempt(readings, f"reading of {act_id}", operation="perlegere")
        latest_payload = _payload(latest, f"reading of {act_id}")
        reading_class = classify(PERLECTOR, latest["outcome"])
        reading_ref = context.artifact_ref(PERLECTOR, "perlectio", latest["artifact_id"])
        basis_regions = (
            reading_basis_regions(latest, f"reading of {act_id}")
            if reading_class is OutcomeClass.COMPLETED
            else []
        )

        # The Recensor's own continuation fact, derived only from the original
        # proposal regions actually cut — never from the Designator's seal flag,
        # which is that stage's own PROPOSAL rather than a settled fact this
        # stage may inherit unexamined. ARCHITECTURE and spec 09: "the Designator
        # proposes continuations; the Recensor's link is the authoritative
        # relation."
        continuation_link = recensor_continuation_link(state["regions"], act_id)
        continuation_shortfall = reconcile_continuation(act, continuation_link, act_id)

        # The residual-ink check, against the page(s) this act's own current
        # regions were cut from — never against what any stage claimed to
        # find. A flagged page holds every act that touches it: nobody yet
        # knows which act, if any, the uncovered ink belongs to, so a human
        # needs the whole page, not a guess at which one act is "responsible".
        flagged_pages = flagged_pages_for(state["regions"], page_findings)

        used_total = len(state["requests"])
        used_fallback = len(state["requests_by_kind"][FALLBACK_RECROP])
        allowed_fallback = recovery_kind_budget(budget, FALLBACK_RECROP)
        wants_recovery = act_key in scenario["recover_acts"] and used_total == 0
        ordinal = used_total + 1

        # The cap is enforced at the request boundary rather than by convention
        # (spec 09's third test): the kind's own allowance, the pooled total, and
        # Tyrel's absolute cap all have to permit this request before it is made.
        # `allowed` can only ever be the smaller of the three today, but a policy
        # is a file somebody edits and a bound nobody checks is not a bound.
        if (
            not continuation_shortfall
            and wants_recovery
            and used_fallback < allowed_fallback
            and used_total < budget["allowed"]
            and used_total < budget["absolute_cap"]
        ):
            # The Recensor asks; the Designator cuts. Recording the request as an
            # artifact is what keeps the loop countable from the tree alone. Only
            # `fallback-recrop` is requested here: it is the one recovery
            # operation this pipeline can actually dispatch today (a Designator
            # recrop). `page-level-reread` stays a real, distinct, budgeted kind
            # in the policy and the payload schema below, ready for the day a
            # Perlector continuation-aware reread exists to answer it — but this
            # stage does not request an operation nothing downstream can honor,
            # because a request the orchestrator can only refuse turns a graceful
            # hold into a hard failure for no gain.
            request = context.publish(
                kind="recovery-request",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recover", ordinal),
                inputs=[reading_ref],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "recovery_kind": FALLBACK_RECROP,
                    "reason": "the crop may be incomplete; an expanded recrop is requested",
                    "budget_allowed": budget["allowed"],
                    "budget_used": used_total,
                    "kind_budget_allowed": allowed_fallback,
                    "kind_budget_used": used_fallback,
                    "coverage": coverage,
                    "perlectio_ref": reading_ref,
                    "recovery_policy": budget,
                },
            )
            request_ref = context.input_ref(request.relative_path)
            context.publish(
                kind="review",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recense", ordinal),
                inputs=[reading_ref, request_ref],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "recovery_kind": FALLBACK_RECROP,
                    "coverage": coverage,
                    "continuation": continuation_link,
                    "perlectio_ref": reading_ref,
                    "recovery_request_ref": request_ref,
                    "recovery_policy": budget,
                },
            )
            held += 1
            continue

        # Whether the reading *succeeded*, not merely whether one exists. The
        # check above asks only that `readings` is non-empty, and the Archetypus
        # copies `payload["text"]` out of whatever the latest reading is — so a
        # `truncated` or `failed` Perlectio carrying stale text was established
        # as the one text, and a `not-run` record crashed on the missing field.
        # GOALS 2 is accuracy against the ink; text nobody successfully read is
        # not a reading, and GOVERNANCE 2 says it may not vanish behind a
        # successful status either. So it is held, visibly, with the outcome named.
        if reading_class is not OutcomeClass.COMPLETED:
            # `no-readable-text` is the one non-completed Perlector outcome that
            # can end here rather than in the ordinary hold below: it is the
            # Perlector's own direct finding (autopsia against the ink, not
            # testimony), and `blank_corroboration` asks only whether the
            # witnesses corroborate or contradict it — never a selection among
            # them. Every other non-completed outcome (`failed`, `truncated`,
            # `not-run`) falls straight through to the ordinary hold: none of
            # them is a positive claim of absence, so there is no absence here
            # to confirm.
            corroborating_chairs = (
                blank_corroboration(coverage, chair_outcomes(context, act_id))
                if (
                    latest["outcome"] == "no-readable-text"
                    and not continuation_shortfall
                    and not flagged_pages
                    and act_key not in scenario["hold_acts"]
                )
                else None
            )
            if corroborating_chairs is not None:
                outcome, reason = (
                    "confirmed-blank",
                    "the Perlector's own reading found no-readable-text, and every witness "
                    f"that actually read this act ({', '.join(corroborating_chairs)}) "
                    "independently reports the same absence; sealed blank with that evidence",
                )
            else:
                outcome, reason = (
                    "held-for-review",
                    f"the latest reading is {latest['outcome']!r} ({reading_class.value}); "
                    "accepting would establish text that nobody successfully read",
                )
                held += 1
        elif not isinstance(latest_payload.get("text"), str) or not latest_payload["text"].strip():
            outcome, reason = (
                "held-for-review",
                "the latest reading establishes no readable text; silence is not blank proof and "
                "is held until the Recensor can seal one",
            )
            held += 1
        elif continuation_shortfall:
            outcome, reason = (
                "held-for-review",
                "the seal claims a continuation but the Recensor's own reconciliation "
                f"finds proposal regions on only {len(continuation_link['page_ordinals'])} "
                "distinct page(s); accepting would deliver part of an act as the act",
            )
            held += 1
        elif flagged_pages:
            outcome, reason = (
                "held-for-review",
                f"page(s) {flagged_pages} carry ink outside every region currently cut on "
                "them (a residual-ink check against the page image itself, never the "
                "proposal set — GOALS 1: a missed act is worse than a poorly read one); "
                "accepting this act would leave that ink unaccounted for",
            )
            held += 1
        elif act_key in scenario["hold_acts"]:
            outcome, reason = "held-for-review", "the act did not reconcile and needs a human"
            held += 1
        elif wants_recovery:
            outcome, reason = (
                "held-for-review",
                f"fallback-recrops use {used_fallback} of their budget of {allowed_fallback}; "
                "a page-level reread is not a substitute and remains unimplemented, so the act "
                "is held rather than re-rolled because recovery recovers coverage and never "
                "quality",
            )
            held += 1
        else:
            outcome, reason = "accepted", "coverage and geometry reconcile"

        context.publish(
            kind="review",
            subject_id=act_id,
            outcome=outcome,
            attempt=attempt_id(act_id, "recense", ordinal),
            # The reading this outcome is actually about, not whichever artifact
            # id happened to sort first. `readings[0]` is manifest order, which is
            # a hash: after a recovery it could cite the superseded attempt's crop
            # as the basis for accepting the new one. Deterministic, and wrong.
            # `latest`, computed once above, rather than a second `latest_attempt`
            # over the same list. A completed reading must cite the regions it
            # read, and is indexed strictly so a missing basis stays a loud
            # failure. A held one need not: a `not-run` Perlectio carries no
            # `basis` key at all, and dereferencing it would turn an honest hold
            # into a traceback — the raw missing-field crash a reviewer filed.
            inputs=[reading_ref]
            + [context.input_ref(reference["image_path"]) for reference in basis_regions],
            payload={
                "act_key": act_key,
                "attempt_ordinal": ordinal,
                "reason": reason,
                "coverage": coverage,
                "continuation": continuation_link,
                "recoveries_used": used_total,
                "budget_allowed": budget["allowed"],
                "absolute_cap": budget["absolute_cap"],
                "perlectio_ref": reading_ref,
                # The residual-ink finding for every page this act's own current
                # regions were cut from, recorded the same way for every act
                # rather than only when it flags something — the same reasoning
                # `continuation` above is recorded under.
                "page_coverage": {
                    "checked_pages": sorted(
                        {
                            region["payload"]["transform"]["source_page_ordinal"]
                            for region in state["regions"]
                        }
                    ),
                    "flagged_pages": flagged_pages,
                },
            },
        )

    context.finish()
    # After `finish()`, so the manifest the receipt checks against disk is the
    # one this pass just wrote rather than the previous pass's.
    write_partition_receipt(context, budget)
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
