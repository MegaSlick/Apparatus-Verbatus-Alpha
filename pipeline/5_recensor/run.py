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

import copy
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
from common.perlector_audit import validate_chain  # noqa: E402
from common.recensor_receipt import build_recensor_partition_receipt  # noqa: E402
from common.recovery import (  # noqa: E402
    FALLBACK_RECROP,
    RECOVERY_KINDS,
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
    require_current_witness_basis,
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


def audit_state(context, reading: dict, act_id: str) -> bool | None:
    """Verify the two R5b artifacts behind a Perlectio's audit claim.

    The Perlectio's self-hash only proves that somebody sealed its references;
    this consumer proves they name the matching act, exact kinds, and the exact
    finding bytes whose unresolved state governs review routing.

    `not-run` is the one Perlector outcome published without a reading attempt
    (`pipeline/4_perlector/run.py`: a Designator-held act, and an explicitly
    absent Perlector chair). It never reaches Pass C, so there is no chain to
    verify and no unresolved span to route on — the act is held on its own
    outcome further down. Demanding a chain here turned the absent-chair hold
    this stage is built to report into a traceback about missing final text,
    which is exactly the trap the `basis_regions` guard below is named for.
    Every attempted outcome (`read`, `truncated`, `no-readable-text`, `failed`)
    publishes the pair and is verified; a forged `not-run` buys nothing, because
    that class is held rather than accepted.

    `None`, not `False`: this act has no audit at all — the same fact a
    Designator-held act's review records. `False` means audited and resolved,
    and claiming it here would tell R8's canonical export that a reading
    nobody examined came back clean. Routing is unchanged (`elif
    audit_unresolved:` treats both as falsy); only the record is honest.
    """
    if reading["outcome"] == "not-run":
        return None
    chain = validate_chain(context.tree, reading, act_id)
    return chain["record"]["unresolved"]


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

    `read_evidence` joins them for the same reason again: it is a fact about
    the same current attempt, and `blank_corroboration` may not read it from a
    second, independently derived idea of which attempt that is.
    """
    records = artifacts_for(context, ATTESTATORES, "testimonium", act_id)
    return {
        record["payload"]["chair"]: {
            "outcome": record["outcome"],
            "content_health": record["payload"].get("content_health"),
            "read_evidence": _read_evidence(record["payload"]),
        }
        for record in latest_per_chair(records, f"testimonium for {act_id}")
    }


def _read_evidence(payload: dict) -> dict[str, bool]:
    """The two facts an Attestatores attempt leaves behind when a chair looked.

    `pipeline/3_attestatores/HANDOFF.md`: `read` and `genuinely-empty` "mean a
    chair actually read the exact regions and carry a serving receipt", and its
    one write path sets both together for every attempted outcome. So these are
    not a quality signal about the reading -- they are whether the record even
    claims a request was made, and they are read here rather than assumed
    because a completed *absence* is the outcome whose whole content is that
    nothing was there, and therefore the one that can be produced without
    anything having been asked (Sol-S1).
    """
    regions = payload.get("regions")
    provenance = payload.get("provenance")
    receipt = provenance.get("receipt_ref") if isinstance(provenance, dict) else None
    return {
        "regions": isinstance(regions, list) and bool(regions),
        "receipt": receipt is not None,
    }


def chair_outcomes(current_attempts: dict[str, dict]) -> dict[str, str]:
    """The current outcome per chair, projected from ONE passed collapse.

    Takes the `chair_current_attempts` result rather than re-deriving it, so a
    caller that needs outcomes and read evidence together provably reads both
    from the same collapse instead of two identical walks happening to agree.
    """
    return {chair: fact["outcome"] for chair, fact in current_attempts.items()}


def chair_read_evidence(current_attempts: dict[str, dict]) -> dict[str, dict[str, bool]]:
    """Each chair's read evidence, projected from the same passed collapse."""
    return {chair: fact["read_evidence"] for chair, fact in current_attempts.items()}


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
        if not isinstance(entry.get("attached"), bool):
            raise FatalAccounting(f"act {act_id} has ambiguous derived act-attachment facts")
        health = entry.get("content_health")
        # A malformed health record and an absent one are different facts: only
        # the absent one is honestly "health not recorded", and only the
        # malformed one tells the operator to look at the artifact.
        if health is not None and not isinstance(health, dict):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        truncated = health.get("truncated") if isinstance(health, dict) else None
        # The same malformed-versus-absent rule `attached` and `content_health`
        # get above: any non-boolean flag read as "act-scoped" would skip the
        # alignment-consistency check and halt later on the outcome check with
        # a message blaming the Testimonium, when the real fault is this field.
        page_witness = entry.get("page_witness")
        if not isinstance(page_witness, bool):
            raise FatalAccounting(
                f"act {act_id} attachment entry for chair {chair!r} carries no boolean "
                "page_witness flag; its scope decides which consistency check applies"
            )
        if page_witness:
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
            # The documented closed shapes (pipeline/3_attestatores/HANDOFF.md),
            # enforced where the floor is counted, not only at the Perlector: an
            # attached record missing its geometry -- or its anchor_basis, which
            # the blank gate below reads -- must not count as valid coverage,
            # and a reason-free unaligned record leaves an operator with no
            # statement of why comparison failed.
            if alignment["status"] == "aligned":
                if set(alignment) != {
                    "status",
                    "anchor_basis",
                    "anchor_span",
                    "witness_span",
                    "line_geometry",
                    "loss",
                    "offset_maps",
                } or alignment["anchor_basis"] not in {
                    "act-anchor",
                    "no-page-anchor",
                    "act-line-not-located",
                }:
                    raise FatalAccounting(
                        f"act {act_id} page witness {chair!r} carries a malformed aligned "
                        "alignment record; the witness floor may not be counted from "
                        "geometry evidence that is missing or unrecognised"
                    )
            elif set(alignment) != {"status", "reason"} or not (
                isinstance(alignment["reason"], str) and alignment["reason"].strip()
            ):
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} carries an unaligned record with "
                    "no usable reason; an unexplained failure is a silent loss"
                )
        fact = {
            "attached": entry["attached"],
            "truncated": truncated,
            "health_unrecorded": truncated is None,
            "page_witness": page_witness,
            "content_health": health,
            # None for act-scoped chairs and unaligned page witnesses; the
            # producer's disclosure of what an aligned page witness aligned
            # against ("act-anchor" | "no-page-anchor" | "act-line-not-located").
            # `blank_corroboration`
            # is the consumer that must see it.
            "anchor_basis": (
                entry["alignment"].get("anchor_basis")
                if page_witness and entry["attached"]
                else None
            ),
        }
        if chair in facts:
            if not (page_witness and facts[chair]["page_witness"]):
                raise FatalAccounting(f"act {act_id} has ambiguous derived act-attachment facts")
            # A page witness has one attachment for every contributing page.
            # Its act-level floor remains the one act attempt, so a continuation
            # whose page has no anchor cannot erase the primary page's valid
            # attachment; all page references remain separately checked by the
            # content denominator below.
            facts[chair]["attached"] = facts[chair]["attached"] or fact["attached"]
            continue
        facts[chair] = fact
    return facts


def blank_corroboration(
    coverage: dict,
    outcomes: dict[str, str],
    attachments: dict[str, dict],
    read_evidence: dict[str, dict[str, bool]],
    *,
    witness_uncovered: bool = False,
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

    `attachments` is `act_attachment_facts`'s per-chair record. A page witness
    whose trivial attach discloses `anchor_basis: "act-line-not-located"`
    still counts toward the floor (the chair did complete, and that is
    disclosed rather than hidden), but it may not corroborate a TERMINAL
    blank: the page's Chandra anchor exists yet locates no line for this act,
    so the geometry does not reconcile, and confirmed-blank is a proved
    absence -- the act holds for a human instead (GOVERNANCE 2/9; GOALS 1:
    the unproved direction costs a review, never an act). `no-page-anchor` is
    the different fact of a page with no anchor at all -- an ink-free or
    fallback page has nothing for Chandra to anchor, and refusing blank there
    would make the intended blank-page path unreachable.

    `read_evidence` is `chair_current_attempts`'s per-chair record of the two
    facts an actual request leaves behind: the regions the chair was shown, and
    the serving receipt for the attempt. The sentence this function's caller
    publishes is a claim about what happened -- "every witness that actually read
    this act ... independently reports the same absence" -- and until Sol-S1 that
    claim was made without anyone checking it. The Sol-S1 repair itself is
    upstream (the minting branch is deleted); the fabricated records carried
    regions AND receipts, so this gate is defence in depth against a resealed
    or foreign artifact, not a second catch for that finding. A completed-class
    outcome missing either fact is a record this pipeline's own writer cannot
    produce, so it is `FatalAccounting` rather than a quiet `None`: a hold
    would say the evidence was weak, and what is actually true is that the
    evidence is not this stage's to interpret. A presence check -- the strong
    per-byte counterpart runs at the Perlector over the same artifacts.
    """
    completed = sorted(
        chair for chair, outcome in outcomes.items() if outcome in WITNESS_READING_OUTCOMES
    )
    # Named per chair AND per missing fact: "this record shows no request was
    # made" sends an operator to the producer, and which half is absent says
    # which producer branch to look at. One message for two faults would not.
    unproved = []
    for chair in completed:
        evidence = read_evidence.get(chair, {})
        missing = [
            label
            for fact, label in (("regions", "region inputs"), ("receipt", "serving receipt"))
            if not evidence.get(fact)
        ]
        if missing:
            unproved.append(f"{chair} has no {' and no '.join(missing)}")
    if unproved:
        raise FatalAccounting(
            "a completed witness outcome for this act records no request having been made: "
            f"{'; '.join(unproved)}. A blank may not be corroborated by a read that nothing "
            "records having happened"
        )
    # **Below the validation, deliberately.** These two are ordinary "this act
    # cannot be confirmed blank" facts and they return a quiet `None`; the check
    # above is a claim that the run tree holds a record this pipeline's own writer
    # could not have produced. Ordering the short-circuit first made that alarm
    # conditional on the act being otherwise eligible, so exactly the trees most
    # likely to be malformed — a recovery region, a run still missing a chair —
    # were the ones where a forged completed record travelled unexamined. A
    # writer-impossible record is fatal on every path or it is not fatal at all.
    if witness_uncovered or coverage["unresolved_chairs"]:
        return None
    if (
        len(completed) < coverage["floor"]
        or not completed
        or any(outcomes[chair] != "genuinely-empty" for chair in completed)
        or any(
            attachments.get(chair, {}).get("anchor_basis") == "act-line-not-located"
            # Defence in depth beside `act_attachment_facts`' own refusal: a
            # page witness whose fact somehow carries no basis at all is
            # geometry nobody checked, and a terminal blank may not rest on it.
            or (
                attachments.get(chair, {}).get("page_witness")
                and attachments.get(chair, {}).get("anchor_basis") is None
            )
            for chair in completed
        )
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
    outcomes = chair_outcomes(current_attempts)
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
    unaccounted = sorted(set(outcomes) ^ set(attachments))
    if unaccounted:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment and its current Testimonia disagree on "
            f"chair(s) {unaccounted}; an absent fact would silently read as unattached, and "
            "an extra one would attach a chair that never testified for this act"
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
        if fact["content_health"] != current_attempts[chair]["content_health"]
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


def geometry_coverage_inputs(context) -> dict[int, dict]:
    """Consume, and independently reconcile, R2's conservation denominator.

    A conservation record is not a conclusion this stage may copy into its own
    review.  Every sealed page represented by a non-held act must have one, and
    its residual components must have become exactly the held residual acts in
    the proposal seal.  Reading both sides here makes a broken R2 invariant-8
    partition or a missing sealed-page denominator a refusal, rather than a
    reassuring Recensor record.  An all-held page is the distinct door-refusal
    shape: it never reached sealing, so absence remains absence for its reviews.
    """
    acts = expected_acts(context)
    residual_keys = {act["act_key"] for act in acts if act["act_key"].startswith("residual:")}
    findings: dict[int, dict] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "conservation":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "conservation", entry["artifact_id"])
        payload = _payload(record, f"Designator conservation {record['artifact_id']}")
        ordinal = payload.get("page_ordinal")
        measurable = payload.get("ink_measurable")
        components = payload.get("residual_components")
        pixel_count_fields = (
            "total_ink_pixel_count",
            "claimed_pixel_count",
            "residual_pixel_count",
        )
        pixel_counts = {field: payload.get(field) for field in pixel_count_fields}
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(measurable, bool)
            or not isinstance(components, list)
            or ordinal in findings
        ):
            raise FatalAccounting("Designator conservation has malformed or duplicate page facts")
        if not measurable and components:
            raise FatalAccounting(
                f"unmeasured Designator conservation page {ordinal} must carry no residual "
                "components"
            )
        for index, component in enumerate(components):
            bounds = component.get("bounds") if isinstance(component, dict) else None
            pixel_count = component.get("pixel_count") if isinstance(component, dict) else None
            if (
                not isinstance(component, dict)
                or not isinstance(bounds, dict)
                or set(bounds) != {"x", "y", "w", "h"}
                or any(
                    not isinstance(bounds[side], int) or isinstance(bounds[side], bool)
                    for side in ("x", "y", "w", "h")
                )
                or not isinstance(pixel_count, int)
                or isinstance(pixel_count, bool)
                or pixel_count < 0
            ):
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} residual component {index} "
                    "is malformed"
                )
        if measurable:
            if any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in pixel_counts.values()
            ):
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} has malformed measured pixel "
                    "counts; total, claimed, and residual must be non-negative integers"
                )
            total = pixel_counts["total_ink_pixel_count"]
            claimed = pixel_counts["claimed_pixel_count"]
            residual = pixel_counts["residual_pixel_count"]
            if claimed + residual != total:
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} pixel accounting does not "
                    "reconcile: claimed_pixel_count + residual_pixel_count does not equal "
                    "total_ink_pixel_count"
                )
            if sum(component["pixel_count"] for component in components) != residual:
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} residual component pixel sum "
                    "does not equal residual_pixel_count"
                )
        elif any(count is not None for count in pixel_counts.values()):
            raise FatalAccounting(
                f"unmeasured Designator conservation page {ordinal} must carry None for "
                "total_ink_pixel_count, claimed_pixel_count, and residual_pixel_count"
            )
        expected = {f"residual:{ordinal}:{index}" for index in range(len(components))}
        actual = {key for key in residual_keys if key.startswith(f"residual:{ordinal}:")}
        if measurable and actual != expected:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} has residual components whose "
                "invariant-8 held-act partition diverges from the components it measured"
            )
        if not measurable and actual:
            raise FatalAccounting(
                f"unmeasured Designator conservation page {ordinal} minted residual acts"
            )
        findings[ordinal] = {
            "ink_measurable": measurable,
            "residual_component_count": len(components),
            "residual_act_count": len(actual),
        }
    required_ordinals = {act["page_ordinal"] for act in acts if act["outcome"] != "held"}
    missing = sorted(required_ordinals - findings.keys())
    if missing:
        pages = ", ".join(str(ordinal) for ordinal in missing)
        if len(missing) == 1:
            raise FatalAccounting(
                f"Designator conservation page {pages} carries a non-held expected act "
                "but has no conservation record"
            )
        raise FatalAccounting(
            f"Designator conservation pages {pages} carry non-held expected acts "
            "but have no conservation records"
        )
    return findings


def current_act_attachments(context) -> dict[str, dict]:
    """The current act-attachment record per act, from one manifest pass.

    `latest_attempt` is the one shared derivation of "current", exactly as
    `act_attachment_facts` above and `pipeline/4_perlector/run.py`'s
    `act_attachment_view` derive it. Keeping the last record the manifest
    happens to list is the defect both of those already record: manifest order
    is a hash, so a second Attestatores pass would hand this check whichever
    attachment sorted last rather than the current one, and a third consumer
    deriving "current" its own way is the F-O1/F-O3 drift shape itself.
    """
    records: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        records.setdefault(entry["subject_id"], []).append(
            context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        )
    return {
        act_id: latest_attempt(group, f"act-attachment for {act_id}", operation="act-attachment")
        for act_id, group in records.items()
    }


def current_page_testimonia(context) -> dict[tuple[int, str], dict]:
    """The current page Testimonium per page and chair, from retained history.

    Testimony is append-only, so the Attestatores manifest legitimately carries
    superseded page records after a later pass.  Group by the semantic subject
    and use the shared attempt derivation: manifest order is a hash, not a
    currency signal, and duplicate or gapped ordinals are accounting failures.
    """
    records: dict[tuple[int, str], list[dict]] = {}
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        ordinal, chair = payload.get("page_ordinal"), payload.get("chair")
        # A boolean ordinal hashes as its integer counterpart, so accepting one
        # here would merge page `true` into page 1 before currency is derived.
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not isinstance(chair, str):
            raise FatalAccounting("page Testimonium has no textual page identity")
        records.setdefault((ordinal, chair), []).append(record)
    return {
        (ordinal, chair): latest_attempt(
            group,
            f"page Testimonium for page {ordinal}, chair {chair}",
            operation=f"read:{chair}",
        )
        for (ordinal, chair), group in records.items()
    }


def uncovered_non_whitespace_ranges(text: str, covered: list[bool]) -> dict:
    """Losslessly compact uncovered non-whitespace offsets into half-open ranges."""
    ranges = []
    count = 0
    for index, char in enumerate(text):
        if covered[index] or char.isspace():
            continue
        count += 1
        if ranges and ranges[-1]["end"] == index:
            ranges[-1]["end"] = index + 1
        else:
            ranges.append({"start": index, "end": index + 1})
    return {"ranges": ranges, "count": count}


def reconcile_page_roles(
    context,
    attachments: dict[str, dict],
    page_testimonia: dict[tuple[int, str], dict],
) -> None:
    """Re-derive every page Testimonium's `page_role` from the whole page.

    The Perlector already refuses a role that one act's own sealed
    `page_ordinal` contradicts, but it reads one act at a time and so can only
    catch the two labels a single act disproves
    (`pipeline/4_perlector/run.py::act_attachment_view`, which says as much).
    `mixed` contradicts no single act: it claims the page holds a primary region
    AND a continuation, and only a stage holding every act on the page can tell
    whether that is true. So a resealed continuation-only page could wear
    `mixed` and pass every check downstream of the producer — the same hole as
    the `primary` forgery, one label along.

    The denominator here is the producer's own published attachment set, not a
    second walk of the Designator: an act contributes to page P exactly when its
    current attachment carries a page-witness row for P. Deriving it from the
    regions again would be a second spelling of the producer's grouping, free to
    drift from it — and the Perlector already binds each act's attachment pages
    to the regions it actually read, so the attachments cannot quietly claim a
    page the ink does not support.
    """
    primary_page = {act["act_id"]: act["page_ordinal"] for act in expected_acts(context)}
    contributors: dict[int, set[str]] = {}
    for act_id, attachment in attachments.items():
        rows = _payload(attachment, f"attachment for {act_id}").get("attachments")
        if not isinstance(rows, list):
            raise FatalAccounting(f"attachment for {act_id} has no rows")
        for row in rows:
            if not isinstance(row, dict) or not row.get("page_witness"):
                continue
            ordinal = row.get("page_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise FatalAccounting(
                    f"act {act_id} page-witness attachment carries no integer page ordinal"
                )
            if act_id not in primary_page:
                raise FatalAccounting(
                    f"act {act_id} carries a page-witness attachment but the proposal seal "
                    "names no such act"
                )
            contributors.setdefault(ordinal, set()).add(
                "primary" if primary_page[act_id] == ordinal else "continuation"
            )
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        roles = contributors.get(ordinal)
        if not roles:
            # No act's attachment reaches this page, so there is no denominator
            # to derive a role from and nothing here to refuse. That is not a
            # silent pass: the content-coverage loop below reads the same page
            # record and finds every character of it uncovered, which is a
            # visible shortfall routed to review.
            continue
        # `next(iter(...))`, never `set.pop()`: `roles` is the shared set held in
        # `contributors`, and popping it would empty the page's own denominator
        # for the second chair that reads it.
        expected = next(iter(roles)) if len(roles) == 1 else "mixed"
        if payload.get("page_role") != expected:
            raise FatalAccounting(
                f"page {ordinal}'s Testimonium for chair {chair!r} claims page_role "
                f"{payload.get('page_role')!r}; the acts attached to that page make it "
                f"{expected!r}"
            )


def testimony_content_findings(context) -> dict[int, dict]:
    """Compare each page witness's text to its own aligned act attachments.

    This is deliberately testimony-to-testimony: no Perlectio text participates
    until R5b's Pass-C output exists.  The retained alignment spans are the loss
    map; a non-whitespace page character outside their ordered union is a visible
    coverage shortfall, never a verdict about which witness is right.
    """
    attachments = current_act_attachments(context)
    page_testimonia = current_page_testimonia(context)
    reconcile_page_roles(context, attachments, page_testimonia)
    # Read and validated once, not once per page witness: `expected_acts` re-reads
    # the proposal seal and re-verifies its self-hash on every call, and this stage
    # never writes to the Designator's seal while it runs.
    acts_by_page: dict[int, list[dict]] = {}
    for act in expected_acts(context):
        found_page = False
        for region in artifacts_for(context, DESIGNATOR, "region", act["act_id"]):
            payload = _payload(region, f"Designator region of {act['act_id']}")
            transform = payload.get("transform")
            if payload.get("origin") != "proposal" or not isinstance(transform, dict):
                continue
            ordinal = transform.get("source_page_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise FatalAccounting(f"Designator region of {act['act_id']} has no page ordinal")
            page_acts = acts_by_page.setdefault(ordinal, [])
            if act not in page_acts:
                page_acts.append(act)
            found_page = True
        # Unit-level consumers can supply only the proposal denominator, before
        # a Designator-region fixture exists. That is still one known primary
        # page, not an empty denominator; real continuation evidence always
        # takes the branch above.
        if not found_page:
            acts_by_page.setdefault(act["page_ordinal"], []).append(act)
    for ordinal, acts in acts_by_page.items():
        for act in acts:
            attachment = attachments.get(act["act_id"])
            if attachment is None:
                raise FatalAccounting(f"act {act['act_id']} has no attachment for content coverage")
            rows = _payload(attachment, f"attachment for {act['act_id']}").get("attachments")
            if not isinstance(rows, list):
                raise FatalAccounting(f"attachment for {act['act_id']} has no rows")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not row.get("page_witness")
                    or not row.get("attached")
                    # Indexed, not defaulted: `reconcile_page_roles` above has
                    # already proved every page-witness row carries an integer
                    # page ordinal. A default of `ordinal` read "absent means
                    # this page", so a row that lost its ordinal would have
                    # counted on EVERY page it was compared against.
                    or row["page_ordinal"] != ordinal
                ):
                    continue
                chair = row.get("chair")
                page_testimonium = page_testimonia.get((ordinal, chair))
                if page_testimonium is None or row.get("testimonium_ref") != context.artifact_ref(
                    ATTESTATORES, "page-testimonium", page_testimonium["artifact_id"]
                ):
                    raise FatalAccounting(
                        f"act {act['act_id']} attached page witness {chair!r} references no "
                        "current page Testimonium"
                    )
    findings: dict[int, dict] = {}
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        if "reported" not in payload:
            if record.get("outcome") in WITNESS_READING_OUTCOMES:
                raise FatalAccounting(
                    "reading page Testimonium has no reported text for content coverage"
                )
            # A page witness that read nothing across every act on this page --
            # every configured act was `dead`, `not-run`, or otherwise non-reading
            # for this chair -- carries no `reported` text: `testimonium_payload`'s
            # reading-only bridge (pipeline/3_attestatores/run.py) never sets it for
            # a non-reading outcome. The outcome check above distinguishes that
            # legitimate absence from a malformed producer that claims it read the
            # page but lost the text. There is no witness content to diff against
            # attachments in the former case; the chair's absence stays visible
            # through its act-scoped Testimonia and the act's witness-coverage floor.
            continue
        text = payload.get("reported")
        if not isinstance(text, str):
            # Deliberately not the identity refusal above. A record that names its
            # page and chair but carries a non-textual `reported` is a different
            # fault from one that names neither, and one string for two faults
            # sends whoever reads the exit to the wrong producer.
            raise FatalAccounting("page Testimonium's reported page text is not text")
        spans = []
        for act in acts_by_page.get(ordinal, []):
            attachment = attachments.get(act["act_id"])
            if attachment is None:
                raise FatalAccounting(f"act {act['act_id']} has no attachment for content coverage")
            rows = _payload(attachment, f"attachment for {act['act_id']}").get("attachments")
            if not isinstance(rows, list):
                raise FatalAccounting(f"attachment for {act['act_id']} has no rows")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("chair") != chair
                    or not row.get("page_witness")
                    # Indexed, not defaulted: `reconcile_page_roles` above has
                    # already proved every page-witness row carries an integer
                    # page ordinal. A default of `ordinal` read "absent means
                    # this page", so a row that lost its ordinal would have
                    # counted on EVERY page it was compared against.
                    or row["page_ordinal"] != ordinal
                ):
                    continue
                if row.get("testimonium_ref") != context.artifact_ref(
                    ATTESTATORES, "page-testimonium", record["artifact_id"]
                ):
                    raise FatalAccounting("act attachment points to a different page Testimonium")
                alignment = row.get("alignment")
                if (
                    row.get("attached")
                    and isinstance(alignment, dict)
                    and alignment.get("status") == "aligned"
                ):
                    span = alignment.get("witness_span")
                    if not isinstance(span, dict) or not all(
                        isinstance(span.get(k), int) and not isinstance(span.get(k), bool)
                        for k in ("start", "end")
                    ):
                        raise FatalAccounting("attached page witness has malformed alignment span")
                    spans.append((span["start"], span["end"], act["act_id"]))
        covered = [False] * len(text)
        for start, end, _ in spans:
            if start < 0 or end < start or end > len(text):
                raise FatalAccounting("act attachment span lies outside its page Testimonium")
            for index in range(start, end):
                covered[index] = True
        uncovered = uncovered_non_whitespace_ranges(text, covered)
        finding = findings.setdefault(ordinal, {"by_chair": {}, "shortfall": False})
        finding["by_chair"][chair] = {
            "attached_spans": [
                {"start": start, "end": end, "act_id": act_id}
                for start, end, act_id in sorted(spans)
            ],
            "uncovered_non_whitespace": uncovered,
        }
        finding["shortfall"] = finding["shortfall"] or bool(uncovered["count"])
    return findings


NO_PAGE_CONSERVATION = {
    "ink_measurable": None,
    "residual_component_count": None,
    "residual_act_count": None,
    "reason": (
        "the Designator published no conservation record for this page, so nothing on it "
        "was measured; its acts are held for the reason the page itself carries"
    ),
}


NO_PAGE_CONTENT_COVERAGE = {
    "by_chair": None,
    "shortfall": None,
    "reason": (
        "no page witness reported text for this page, so testimony content coverage "
        "was not measured; its acts are already held or floored by their own causes"
    ),
}


def geometry_coverage_for(findings: dict[int, dict], ordinal: int) -> dict:
    """Return one review's private copy of a page's geometry-coverage fact.

    A page with no conservation record at all is **not** a page the Designator
    measured and found unmeasurable. It publishes one record per page it sealed,
    unmeasurable pages included, precisely "because a page with no conservation
    record at all is the silent gap this artifact exists to close"
    (`pipeline/2_designator/run.py::_publish_conservation_and_secondary`), so an
    absent record means the page never reached that stage — a door refusal, whose
    acts are already held for the page loss itself. Defaulting to
    `ink_measurable: False` here would restate a measurement nobody took, in a
    record byte-identical to a real unmeasurable page's (GOVERNANCE 10). The
    absence is recorded as absence instead, and every act gets its own object for
    the reason `testimony_content_for_page` does.
    """
    return copy.deepcopy(findings.get(ordinal, NO_PAGE_CONSERVATION))


def testimony_content_for_page(findings: dict[int, dict], ordinal: int) -> dict:
    """Return one review's private copy of a page-level content finding.

    The measurement is intentionally computed once per page, but review payloads
    are act-scoped consumers. Giving each consumer its own nested object prevents
    an in-process mutation made while preparing one act from changing a sibling
    act's still-to-be-published evidence. A page absent from `findings` had no
    page witness report text to measure; its None-valued fallback records that
    absence rather than restating it as a measured, clean page.
    """
    return copy.deepcopy(findings.get(ordinal, NO_PAGE_CONTENT_COVERAGE))


def review_route_from_findings(
    *,
    testimony_shortfall: bool | None,
    audit_unresolved: bool | None,
    under_witnessed: bool,
    unreconciled: bool = False,
) -> tuple[str, str] | None:
    """Compose independent review findings without last-writer-wins routing.

    Coverage comes first under GOALS 1, followed by R5b's reading-audit finding,
    then the witness floor. Every active reason is retained in that stable order;
    they all map to the same `held-for-review` outcome. `None` testimony coverage
    means no page witness reported text and routes like `False`: the act's own
    held or witness-floor cause already routes it, while an absent measurement
    is not itself a measured shortfall. `audit_unresolved` is
    wired to the Recensor's verified `audit_state` since the wave restacked R5b
    below this branch; `None` means no audit exists and routes like `False` by
    design, because absence of an audit is not an unresolved audit. `unreconciled`
    folds the scenario hold into the composer (R6 audit F-O5): it was the one
    preempted cause with no independent field, so an act simultaneously
    under-witnessed and scenario-held recorded only the floor cause.
    """
    reasons = []
    if testimony_shortfall:
        reasons.append(
            "a page Testimonium contains non-whitespace text outside the ordered union "
            "of that witness's aligned act attachments; testimony coverage is incomplete "
            "at the whole-page level, so the uncovered text may belong to another act on "
            "the same page and the hold is page-scoped by design"
        )
    if audit_unresolved:
        reasons.append(
            "the Perlector exhausted its sealed audit re-proof cap with unresolved span(s); "
            "they remain explicit uncertainty rather than a silent retry"
        )
    if under_witnessed:
        reasons.append(
            "the configured act-level witness floor is not met; a witness failure is not coverage"
        )
    if unreconciled:
        reasons.append("the act did not reconcile and needs a human")
    if not reasons:
        return None
    return "held-for-review", "; ".join(reasons)


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
        # The earliest stage that can say so: a reading whose witness basis a
        # later Testimonium has superseded is not reconciled, and the Recensor is
        # what decides whether a reading may be accepted at all. Checked here as
        # well as at the Archetypus and the export because one derivation with
        # three consumers is what kept `recovery_region_count` from drifting, and
        # each of the three can be reached first by hand.
        require_current_witness_basis(
            act_id,
            latest,
            artifacts_for(context, ATTESTATORES, "testimonium", act_id),
            f"the current reading of {act_id}",
        )
        context.artifact_ref(PERLECTOR, "perlectio", latest["artifact_id"])
        audit_state(context, latest, act_id)
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
    # The run's own sealed policy, parsed once when the run's binding was checked,
    # never reopened here. `config/recovery.toml` used to be read a second time at
    # this line: a rewrite landing between `open_context` and it published reviews
    # and recovery-requests carrying an allowance the run never sealed — measured
    # as both acts held for review with `budget_allowed: 0` under a run whose
    # digest bound the stock allowance, and unrecoverable afterwards because the
    # correct rerun computes different bytes under the same immutable review
    # identity and stops with IncompatibleReuse (audit S3). The recheck below
    # proves the carried policy is the sealed one, so a reintroduced second read
    # refuses instead of publishing.
    budget = context.recovery_policy
    context.require_sealed_config("recovery", budget["config_sha256"])

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
    geometry_inputs = geometry_coverage_inputs(context)
    content_findings = testimony_content_findings(context)

    held = 0
    for act in expected_acts(context):
        act_id, act_key = act["act_id"], act["act_key"]

        coverage = validate_chair_coverage(context, act_id, floor)
        content_coverage = testimony_content_for_page(content_findings, act["page_ordinal"])
        geometry_coverage = geometry_coverage_for(geometry_inputs, act["page_ordinal"])

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
                    "geometry_coverage": geometry_coverage,
                    "testimony_content_coverage": content_coverage,
                    "continuation": recensor_continuation_link(hold_regions, act_id),
                    "page_coverage": page_coverage_for(hold_regions, page_findings),
                    "recoveries_used": 0,
                    "budget_allowed": budget["allowed"],
                    "absolute_cap": budget["absolute_cap"],
                    # None, not absent, and not False: a Designator-held act
                    # has no Perlectio and therefore no audit to report. The
                    # field stays universal so a consumer can tell "no audit
                    # exists" (here) from "audited, resolved" (False) and
                    # "audited, unresolved" (True).
                    "audit_unresolved": None,
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
        audit_unresolved = audit_state(context, latest, act_id)
        # WAVE WIRING (was the pre-wave seam `False`): R5b's Pass-C producer
        # now sits below this branch, so the composer receives the verified
        # audit state the seat-era candidate could not have. Computed here,
        # after audit_state, because a held act has no reading and no audit
        # chain to consult — it takes its own branch above and never reaches
        # the routing that consumes this.
        findings_route = review_route_from_findings(
            testimony_shortfall=content_coverage["shortfall"],
            audit_unresolved=audit_unresolved,
            under_witnessed=coverage["under_witnessed"],
            unreconciled=act_key in scenario["hold_acts"],
        )
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
                    "geometry_coverage": geometry_coverage,
                    "testimony_content_coverage": content_coverage,
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
                    # R6 audit F-O7: this was the only review shape carrying
                    # neither field, so its consumers could not tell "checked
                    # and clear" from "never checked".
                    "geometry_coverage": geometry_coverage,
                    "testimony_content_coverage": content_coverage,
                    "continuation": continuation_link,
                    "page_coverage": page_coverage,
                    "perlectio_ref": reading_ref,
                    "recovery_request_ref": request_ref,
                    "recovery_policy": budget,
                    # This act WAS audited (computed above for every act with a
                    # Perlectio); omitting the field here would read back as
                    # None -- "no audit exists" -- which is false, and R8's
                    # canonical export is the consumer that would believe it.
                    "audit_unresolved": audit_unresolved,
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
            # One `chair_current_attempts` collapse feeds both maps, so "the
            # gate and the outcomes cannot disagree about which attempt is
            # current" is structural rather than two identical walks agreeing.
            current_attempts = chair_current_attempts(context, act_id)
            corroborating_chairs = (
                blank_corroboration(
                    coverage,
                    chair_outcomes(current_attempts),
                    act_attachment_facts(context, act_id),
                    chair_read_evidence(current_attempts),
                    witness_uncovered=bool(state["recovery_regions"]),
                )
                if (
                    latest["outcome"] == "no-readable-text"
                    and not continuation_shortfall
                    and not flagged_pages
                    # Every hold cause the ordinary chain below would apply,
                    # asked once. `confirmed-blank` is COMPLETED-class and
                    # terminal, so a cause that only appears in that chain is a
                    # cause this seal silently overrides: an act whose page
                    # carries witness text outside every aligned attachment, or
                    # whose Perlector exhausted its audit re-proof cap, would be
                    # sealed complete over a shortfall this stage had already
                    # measured (GOVERNANCE 2; invariant 6). The three page-level
                    # conditions above are named separately because they are
                    # already refused before the route is consulted; this
                    # subsumes the scenario hold `act_key not in
                    # scenario["hold_acts"]` used to state, through the
                    # composer's own `unreconciled` cause.
                    and findings_route is None
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
                route_reason = (
                    f"; its corroboration is blocked because {findings_route[1]}"
                    if findings_route is not None
                    else ""
                )
                outcome, reason = (
                    "held-for-review",
                    f"the latest reading is {latest['outcome']!r} ({reading_class.value}); "
                    "accepting would establish text that nobody successfully read"
                    f"{route_reason}",
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
        elif findings_route is not None:
            outcome, reason = findings_route
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
                "geometry_coverage": geometry_coverage,
                "testimony_content_coverage": content_coverage,
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
                # The Pass-C verdict, recorded as data for every act for the
                # same reason: an act held for an exhausted audit cap must be
                # separable from every other hold without matching prose, and
                # an audit that resolved cleanly must be tellable from one
                # never checked. R8's canonical export reads uncertainty spans
                # whose review-side "why" lives exactly here.
                "audit_unresolved": audit_unresolved,
                # Present only on a `confirmed-blank`, because it is the evidence
                # that outcome rests on and nothing else has any. Every other
                # review carries the fields above and no more.
                **({"blank_evidence": blank_evidence} if blank_evidence is not None else {}),
            },
        )

    context.seal_boundary()
    context.finish()
    # After `finish()`, so the manifest the receipt checks against disk is the
    # one this pass just wrote rather than the previous pass's.
    write_partition_receipt(context, budget)
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
