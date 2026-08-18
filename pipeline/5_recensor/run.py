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
coverage record that marks an act under-witnessed and forces the run's aggregate
visibly partial. On an explicit Perlector `no-readable-text` finding, unanimous
region-bound absence may additionally corroborate a blank; it never supplies
characters, and no count of chairs can change a reading.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from residual_ink import page_residual_ink  # noqa: E402

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting  # noqa: E402
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
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.recensor_receipt import build_recensor_partition_receipt  # noqa: E402
from common.recovery import (  # noqa: E402
    FALLBACK_RECROP,
    RECOVERY_KINDS,
    load_recovery_policy,
    reconcile_recovery_requests,
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


def chair_current_attempts(context, act_id: str) -> dict[str, dict]:
    """Each chair's current attempt facts, from ONE latest-attempt collapse.

    Derived, never stored as a pointer. A failed attempt 2 over a successful
    attempt 1 therefore reads as `failed`, with attempt 1 intact as history.
    `latest_per_chair` is the one shared derivation of "current" per chair,
    also used by `pipeline/4_perlector/run.py::testimonia_of` over the same
    upstream artifacts, so the consumers cannot drift on what "current" means.
    `outcome` and `content_health` come out of the same collapse for the same
    reason: two functions that each re-derived "current" independently could
    drift apart, and the staleness check below would then compare two
    different ideas of the current attempt.

    A page witness's act-attachment `content_health` is recorded from this
    exact per-(act, chair) attempt stream (`pipeline/3_attestatores/run.py`'s
    `attempts_by_pair`), not from its page-level Testimonium -- a targeted
    reread appends to this stream whether or not the chair is page-scoped, so
    this is a staleness signal for every chair alike (REOPENED F-O1).
    """
    records = artifacts_for(context, ATTESTATORES, "testimonium", act_id)
    return {
        record["payload"]["chair"]: {
            "outcome": record["outcome"],
            "content_health": record["payload"].get("content_health"),
        }
        for record in latest_per_chair(records, f"testimonium for {act_id}")
    }


def chair_outcomes(context, act_id: str) -> dict[str, str]:
    """The current outcome per chair, from `chair_current_attempts`'s collapse."""
    return {
        chair: fact["outcome"] for chair, fact in chair_current_attempts(context, act_id).items()
    }


def act_attachment_facts(context, act_id: str) -> dict[str, dict]:
    """Read R0's derived attachment record before counting the witness floor."""
    records = artifacts_for(context, ATTESTATORES, "act-attachment", act_id)
    if not records:
        raise FatalAccounting(f"act {act_id} has no derived act-attachment record")
    # The one shared derivation of "current", exactly as the Perlector's
    # act_attachment_view selects it. The local sort this replaces defaulted a
    # missing ordinal to 0 and took the last record blind, so a duplicate or
    # gapped ordinal chain picked an arbitrary attachment where the strict
    # helper refuses — the same two-predicates-drifting shape as F-O1/F-O3
    # (CodeRabbit chain-end review, critical; host disposition: fixed).
    record = latest_attempt(records, f"act-attachment for {act_id}", operation="act-attachment")
    payload = record.get("payload")
    entries = payload.get("attachments") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise FatalAccounting(f"act {act_id} has malformed derived act-attachment payload")
    facts: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("chair"), str):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        chair = entry["chair"]
        if chair in facts or not isinstance(entry.get("attached"), bool):
            raise FatalAccounting(f"act {act_id} has ambiguous derived act-attachment facts")
        health = entry.get("content_health")
        # A malformed health record and an absent one are different facts: only
        # the absent one is honestly "health not recorded", and only the
        # malformed one tells the operator to look at the artifact.
        if health is not None and not isinstance(health, dict):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        truncated = health.get("truncated") if isinstance(health, dict) else None
        if entry.get("page_witness") is True:
            alignment = entry.get("alignment")
            if not isinstance(alignment, dict) or alignment.get("status") not in {
                "aligned",
                "unaligned",
            }:
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} has no computed alignment fact"
                )
            if entry["attached"] != (alignment["status"] == "aligned"):
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} contradicts its computed alignment"
                )
        facts[chair] = {
            "attached": entry["attached"],
            "truncated": truncated,
            "health_unrecorded": truncated is None,
            "page_witness": entry.get("page_witness") is True,
            "content_health": health,
        }
    return facts


def blank_corroboration(
    coverage: dict, outcomes: dict[str, str], *, witness_uncovered: bool = False
) -> list[str] | None:
    """The corroborating chairs if every witness that read this act's ink agrees
    nothing was there, or `None` if the evidence does not support that.

    ARCHITECTURE and spec 09 both name blank confirmation as a candidate
    completeness check ("a zero-output unit is diagnosed, then either sealed
    confirmed-blank with evidence or held unresolved-with-evidence"). A blank
    verdict may not rest on fewer than several genuinely INDEPENDENT completed
    reads, and never on a reader's own second opinion — the old pipeline paid to
    learn that (window pass, 2026-08-05).

    This is **unanimity about an absence, never a selection among presences**:
    the Perlector's own direct examination of the ink (autopsia, not testimony)
    already produced `no-readable-text`, and this asks only whether the witnesses
    corroborate or contradict that finding. A single chair that actually read
    text is exactly the disagreement GOALS 1 says must never be silently
    resolved — it holds the act for a human, and never outvotes the dissenter.

    A recovery region is witness-uncovered by contract: the inherited
    testimonia remain bound to the original proposal regions. They therefore
    cannot corroborate absence in an expanded region they never saw.

    Requires the configured witness floor to have been met by chairs that
    actually completed a read (not merely configured), and no chair still
    unresolved — a floor met only by `failed`/`dead` chairs, or a run that
    has not yet heard from every configured chair, corroborates nothing.

    The floor is checked against `completed` below, never against
    `coverage["under_witnessed"]`: that flag is `ATTESTATORES`'s own
    COMPLETED class, which also counts an approval-bound `excluded` chair
    (`common/contracts/outcomes.py`) — a chair Tyrel excluded from witnessing
    at all, not one that read the ink and found nothing. `under_witnessed`
    can therefore be `False` while the actual reading evidence is one chair
    short of the floor; trusting it here would let an excluded chair stand
    in for a witness that never looked.
    """
    if witness_uncovered or coverage["unresolved_chairs"]:
        return None
    completed = sorted(
        chair for chair, outcome in outcomes.items() if outcome in WITNESS_READING_OUTCOMES
    )
    if (
        len(completed) < coverage["floor"]
        or not completed
        or any(outcomes[chair] != "genuinely-empty" for chair in completed)
    ):
        return None
    return completed


def validate_chair_coverage(context, act_id: str, floor: int) -> dict[str, object]:
    """Return one act's coverage after refusing ambiguous witness history.

    Deliberately callable before the Recensor publishes anything: an ambiguity
    discovered while reviewing the second act would otherwise leave a review for
    the first one already on disk. That fragment is not a completed stage, but it
    is an easy thing for a later retry to mistake for history, so the whole
    witness denominator is validated before any of it is published.
    """
    current_attempts = chair_current_attempts(context, act_id)
    outcomes = {chair: fact["outcome"] for chair, fact in current_attempts.items()}
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
    attachments = act_attachment_facts(context, act_id)
    # R4's attachment is an independent computed fact for a PAGE witness.  It
    # must not be forced back into the act attempt outcome there: a page
    # witness can have read its page while the bounded text-to-anchor
    # calculation honestly remains unaligned, and `act_attachment_facts`
    # already checks that fact for internal consistency against its own
    # computed alignment.
    unaccounted = sorted(set(outcomes) - set(attachments))
    if unaccounted:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment records no fact for configured "
            f"chair(s) {unaccounted}; an absent fact would silently read as unattached"
        )
    # An ACT-SCOPED chair carries no independent computed fact: its `attached`
    # is a restatement of that chair's own current Testimonium outcome, not a
    # second measurement of anything. `reread_pass` (`pipeline/3_attestatores/
    # run.py`) appends a new act-scoped attempt without writing a new
    # attachment record, so the derived attachment is a THIRD consumer of the
    # same artifacts as `chair_outcomes`/`testimonia_of` and can drift from
    # both exactly as it did before R4 (F-O1): a targeted reread would
    # otherwise count the witness floor from an attempt the reread already
    # superseded. Restored on R4's audit (REOPENED F-O1) after removing it
    # here left this hole open for every act-scoped chair; page witnesses are
    # exempted because their own alignment-consistency check above is the
    # genuinely independent fact this check would otherwise wrongly demand
    # agreement from.
    superseded = sorted(
        chair
        for chair, outcome in outcomes.items()
        if not attachments[chair]["page_witness"]
        and attachments[chair]["attached"] != (outcome in WITNESS_READING_OUTCOMES)
    )
    if superseded:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment disagrees with the current Testimonium "
            f"outcome for chair(s) {superseded}; the witness floor may not be counted from "
            "a superseded attempt"
        )
    # NOT scoped to act-scoped chairs, unlike the outcome check just above: a
    # page witness's attachment `content_health` is recorded from this exact
    # per-(act, chair) attempt stream, not from page-level text, so it is a
    # valid staleness signal for every chair (`chair_current_attempts`'s
    # docstring; mirrors `pipeline/4_perlector/run.py::act_attachment_view`'s
    # identical, symmetric check -- REOPENED F-O1).
    stale_health = sorted(
        chair
        for chair, fact in attachments.items()
        if fact["content_health"]
        != (current_attempts[chair]["content_health"] if chair in current_attempts else None)
    )
    if stale_health:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment describes an attempt that is no longer "
            f"the current Testimonium for chair(s) {stale_health}; the witness floor may not "
            "be counted from a superseded attempt"
        )
    return witness_coverage(outcomes, floor, attachments=attachments)


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

    The request history itself — ordinals, kinds, and the counters each request
    recorded — is reconciled by `common/recovery.py::reconcile_recovery_requests`,
    the one implementation the Designator and orchestrator boundary also uses.
    What this function adds is the binding between that history and the reviews,
    recrops and rereads that answered it.
    """
    ordered_requests = reconcile_recovery_requests(
        artifacts_for(context, RECENSOR, "recovery-request", act_id), act_id, budget
    )
    requests_by_ordinal = dict(enumerate(ordered_requests, start=1))
    requests_by_kind: dict[str, list[dict]] = {kind: [] for kind in RECOVERY_KINDS}
    request_refs: dict[str, dict] = {}
    for request in ordered_requests:
        requests_by_kind[request["payload"]["recovery_kind"]].append(request)
        request_refs[request["artifact_id"]] = context.artifact_ref(
            RECENSOR, "recovery-request", request["artifact_id"]
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
    yet. That gap is named, not papered over, in `HANDOFF.md`.
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
        # Every one of the four numbers, not merely that `bounds` is an object:
        # `residual_ink` indexes all four, so a rectangle that is a dict and
        # nothing more reaches the pixel arithmetic and leaves by traceback
        # instead of by the named refusal this check exists to give.
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(bounds, dict)
            or any(
                not isinstance(bounds.get(side), int) or isinstance(bounds.get(side), bool)
                for side in ("x", "y", "w", "h")
            )
        ):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has an invalid transform"
            )
        by_page.setdefault(ordinal, []).append(bounds)
    return by_page


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted source-manifest row for each ordinal, by ordinal.

    The same reconciliation `pipeline/2_designator/run.py::_source_rows` and
    `pipeline/7_armarium/run.py::page_census` each carry locally rather than
    share — every stage that touches sealed Exemplar pixels rebuilds its own
    view of the submitted denominator before trusting a page's own claim
    about them.
    """
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise FatalAccounting("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise FatalAccounting("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                "run.json carries a source-manifest row without an integer ordinal"
            )
        if ordinal in sources:
            raise FatalAccounting(f"run.json repeats source ordinal {ordinal}")
        sources[ordinal] = row
    return sources


def sealed_page_images(context) -> dict[int, dict]:
    """Every sealed Exemplar page's own artifact, by ordinal.

    Verified against the run's own submitted source manifest before this
    stage trusts its pixels — the residual-ink check reads raw bytes off
    `payload["image_path"]`, a self-declared field `validate_envelope` never
    relates to a page's digest-checked `inputs`. Every other stage that reads
    sealed page pixels (`pipeline/2_designator/run.py`,
    `pipeline/7_armarium/run.py`) calls this exact check first; reading raw
    bytes off an unverified path here would be the one place in the pipeline
    that skips it.
    """
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
        if ordinal in pages:
            raise FatalAccounting(
                f"the Exemplar carries more than one sealed page for ordinal {ordinal}; "
                "the Recensor has no rule for selecting one page image"
            )
        pages[ordinal] = record

    # Second pass, so the structural denominator above is settled before any
    # pixel is touched.
    sources = _source_rows(context.run)
    for ordinal, record in pages.items():
        source = sources.get(ordinal)
        if source is None:
            raise FatalAccounting(
                f"a sealed Exemplar page names ordinal {ordinal}, which run.json never submitted"
            )
        try:
            verify_sealed_page_pixels(context.tree, context.run, source, record)
        except ContractError as error:
            raise FatalAccounting(
                f"sealed Exemplar page {ordinal} failed pixel verification; the residual-ink "
                "check may not read bytes over an unverified image_path"
            ) from error
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
        # The bytes measured, digested — not the separate earlier read
        # `sealed_page_images` verified. Verifying one read and measuring
        # another records a finding derived from pixels nobody checked, which is
        # a metric that was not measured passing as one (GOVERNANCE 10).
        image_bytes = context.tree.read_bytes(page["payload"]["image_path"])
        if digest_bytes(image_bytes) != page["payload"]["source_sha256"]:
            raise FatalAccounting(
                f"the sealed Exemplar page {ordinal} the residual-ink check read does not "
                "match the pixel digest its own page record verified"
            )
        findings[ordinal] = page_residual_ink(image_bytes, bounds)
    return findings


def page_coverage_for(act_regions: list[dict], findings: dict[int, dict]) -> dict[str, list[int]]:
    """The residual-ink fact every review records: which source pages this act's
    own current regions were cut from, and which of those are flagged.

    Every page the act's proposal or recovery regions touch, not only its primary
    `page_ordinal` — a continuation's far-side page is examined exactly as its
    near side is, and a successful recovery crop that reaches previously-missed
    ink clears the page's finding on the very next pass.

    `checked_pages` is narrowed to pages `findings` actually has an entry for,
    not merely every ordinal an act's regions name: in `main()`'s real call the
    two sets always coincide (`page_coverage_findings` derives its keys from
    the same region set this function reads), but the field's own purpose --
    "a consumer cannot tell 'checked and clear' from 'never checked'" -- only
    holds if a page absent from `findings` is never reported as checked.

    One derivation for all four review shapes, including a Designator-held act
    whose own near-side region really was cut: a shape that records this fact
    empty rather than deriving it drops a flagged page's only evidence whenever
    that act is the only one touching the page.
    """
    ordinals = sorted(
        {
            region["payload"]["transform"]["source_page_ordinal"]
            for region in act_regions
            if isinstance(region.get("payload"), dict)
            and isinstance(region["payload"].get("transform"), dict)
        }
    )
    checked = [ordinal for ordinal in ordinals if ordinal in findings]
    return {
        "checked_pages": checked,
        "flagged_pages": [ordinal for ordinal in checked if findings[ordinal].get("flagged")],
    }


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
    budget = load_recovery_policy(args.recovery_config)

    scenario = scenario_for(context.fixture, context.scenario)
    floor = context.witness_floor

    # This pass must precede publication.  `latest_attempt` refuses duplicate
    # semantic ordinals rather than selecting an arbitrary hash-sorted record;
    # doing that only as each act is published can leave an earlier act's review
    # behind when a later act is malformed.
    preflight_witness_denominator(context, floor)
    preflight_recovery_history(context, budget)
    preflight_review_evidence(context, budget)

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
            # A Designator hold has two distinct shapes (`pipeline/2_designator/
            # run.py::initial_pass`): the act's own page never sealed, and no
            # region of it is cut at all; or the act's own page sealed and its
            # near-side region WAS cut, but a declared continuation's page
            # never sealed. `hold_regions` reads what was actually cut rather
            # than assuming the first shape for both — a real near-side region
            # has real continuation and page-coverage facts to report, and
            # hardcoding them empty would silently drop a flagged page's
            # evidence for the one act that touches it.
            hold_regions = artifacts_for(context, DESIGNATOR, "region", act_id)
            context.publish(
                kind="review",
                subject_id=act_id,
                outcome="held-for-review",
                attempt=attempt_id(act_id, "recense", 1),
                inputs=[context.input_ref(hold_path)]
                + [context.input_ref(region["payload"]["image_path"]) for region in hold_regions],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": 1,
                    "reason": f"the Designator held this act: {hold['payload']['reason']}",
                    "coverage": coverage,
                    "continuation": recensor_continuation_link(hold_regions, act_id),
                    "page_coverage": page_coverage_for(hold_regions, page_findings),
                    "recoveries_used": 0,
                    "budget_allowed": budget["allowed"],
                    "absolute_cap": budget["absolute_cap"],
                },
            )
            held += 1
            continue

        state = recovery_state(context, act_id, budget)
        if state["outstanding_request_ids"]:
            # The matching review is already the durable record of this hold. A
            # direct Recensor retry must not turn it into a later acceptance while
            # the Designator has not yet cut the requested recovery crop.
            held += 1
            continue

        # `preflight_review_evidence`, above, already refused a non-held act with
        # no reading at all, over this same list and the same Designator seal
        # this process never writes to.
        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)

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

        continuation_link = recensor_continuation_link(state["regions"], act_id)
        continuation_shortfall = reconcile_continuation(act, continuation_link, act_id)

        # The residual-ink check, against the page(s) this act's own current
        # regions were cut from — never against what any stage claimed to
        # find. A flagged page holds every act that touches it: nobody yet
        # knows which act, if any, the uncovered ink belongs to, so a human
        # needs the whole page, not a guess at which one act is "responsible".
        page_coverage = page_coverage_for(state["regions"], page_findings)
        flagged_pages = page_coverage["flagged_pages"]

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
                    "page_coverage": page_coverage,
                    "perlectio_ref": reading_ref,
                    "recovery_request_ref": request_ref,
                    "recovery_policy": budget,
                },
            )
            held += 1
            continue

        # Whether the reading *succeeded*, not merely whether one exists. The
        # Archetypus copies `payload["text"]` out of whatever the latest reading
        # is, so a `truncated` or `failed` Perlectio carrying stale text would be
        # established as the one text and a `not-run` one would crash on the
        # missing field. GOALS 2 is accuracy against the ink; text nobody
        # successfully read is not a reading, and GOVERNANCE 2 says it may not
        # vanish behind a successful status either. Held, visibly, outcome named.
        blank_evidence = None
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
                blank_corroboration(
                    coverage,
                    chair_outcomes(context, act_id),
                    witness_uncovered=bool(state["recovery_regions"]),
                )
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
                # Spec 09 seals a blank "with evidence", and a sentence is not
                # evidence a consumer can read. The review queue, the Armarium
                # and anyone re-deriving this outcome get the same facts as
                # data: what the reading itself found, which chairs corroborated
                # it, and which pages this check found no ink outside coverage
                # on. That is narrower than "clear": the check never looks
                # inside the act's own crop, so the field is named for exactly
                # what it measured and no more (GOVERNANCE 10). The `reason`
                # above stays, for a human reading one record.
                blank_evidence = {
                    "perlector_outcome": latest["outcome"],
                    "corroborating_chairs": corroborating_chairs,
                    "pages_without_residual_ink_outside_coverage": page_coverage["checked_pages"],
                }
            else:
                outcome, reason = (
                    "held-for-review",
                    f"the latest reading is {latest['outcome']!r} ({reading_class.value}); "
                    "accepting would establish text that nobody successfully read",
                )
        elif not isinstance(latest_payload.get("text"), str) or not latest_payload["text"].strip():
            outcome, reason = (
                "held-for-review",
                "the latest reading establishes no readable text; silence is not blank proof and "
                "is held until the Recensor can seal one",
            )
        elif continuation_shortfall:
            outcome, reason = (
                "held-for-review",
                "the seal claims a continuation but the Recensor's own reconciliation "
                f"finds proposal regions on only {len(continuation_link['page_ordinals'])} "
                "distinct page(s); accepting would deliver part of an act as the act",
            )
        elif flagged_pages:
            outcome, reason = (
                "held-for-review",
                f"page(s) {flagged_pages} carry ink outside every region currently cut on "
                "them (a residual-ink check against the page image itself, never the "
                "proposal set — GOALS 1: a missed act is worse than a poorly read one); "
                "accepting this act would leave that ink unaccounted for",
            )
        elif act_key in scenario["hold_acts"]:
            outcome, reason = "held-for-review", "the act did not reconcile and needs a human"
        elif wants_recovery:
            outcome, reason = (
                "held-for-review",
                f"fallback-recrops use {used_fallback} of their budget of {allowed_fallback}; "
                "a page-level reread is not a substitute and remains unimplemented, so the act "
                "is held rather than re-rolled because recovery recovers coverage and never "
                "quality",
            )
        else:
            outcome, reason = "accepted", "coverage and geometry reconcile"

        # Derived from the outcome's own class rather than counted by hand in each
        # branch above, so a review shape added later cannot land in the tree
        # without also landing in this stage's exit code.
        if classify(RECENSOR, outcome) is not OutcomeClass.COMPLETED:
            held += 1

        context.publish(
            kind="review",
            subject_id=act_id,
            outcome=outcome,
            attempt=attempt_id(act_id, "recense", ordinal),
            # `latest`, never `readings[0]`: manifest order is a hash, so after a
            # recovery the first record can be the superseded attempt, and citing
            # its crop as the basis for accepting the new reading is deterministic
            # and wrong. `basis_regions` is empty unless the reading completed —
            # a `not-run` Perlectio carries no `basis` key at all, and indexing it
            # would turn an honest hold into a traceback.
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
                # Recorded the same way for every act rather than only when it
                # flags something — the same reasoning `continuation` above is
                # recorded under: a consumer that only ever sees the field
                # populated cannot tell "checked and clear" from "never checked".
                "page_coverage": page_coverage,
                # Present only on a `confirmed-blank`, because it is the evidence
                # that outcome rests on and nothing else has any. Every other
                # review carries the fields above and no more.
                **({"blank_evidence": blank_evidence} if blank_evidence is not None else {}),
            },
        )

    context.finish()
    # After `finish()`, so the manifest the receipt checks against disk is the
    # one this pass just wrote rather than the previous pass's.
    write_partition_receipt(context, budget)
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
