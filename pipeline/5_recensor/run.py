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

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    OutcomeClass,
    classify,
    terminal_category,
    witness_coverage,
)
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    PERLECTOR,
    RECENSOR,
)
from common.recovery import load_recovery_policy  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
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


def recovery_state(context, act_id: str) -> dict:
    """Reconcile this act's requested, cut, and reviewed recovery history.

    A recovery request is not evidence that its recrop happened, and a recovery
    crop is not evidence that the Perlector read it.  The three append-only
    histories must therefore agree before another Recensor review can be written:
    exactly one recovery-requested review per request, at most one recrop per
    request, and a later Perlectio for every recrop.  No branch here establishes
    text or selects among readings; disagreement is fatal accounting.
    """
    requests = artifacts_for(context, RECENSOR, "recovery-request", act_id)
    request_refs: dict[str, dict] = {}
    requests_by_ordinal: dict[int, dict] = {}
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
        requests_by_ordinal[ordinal] = request
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
        if payload.get("perlectio_ref") != request_payload.get("perlectio_ref"):
            raise FatalAccounting(
                f"recovery-requested review of {act_id} does not name the Perlectio its "
                "request assessed"
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
        "requests": requests,
        "regions": regions,
        "recovery_regions": recovery_regions,
        "outstanding_request_ids": [
            request_id for request_id, recrops in recrops_by_request.items() if not recrops
        ],
    }


def preflight_recovery_history(context) -> None:
    """Name every broken recovery pair before publishing another review."""
    for act in expected_acts(context):
        recovery_state(context, act["act_id"])


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


def preflight_review_evidence(context) -> None:
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
        state = recovery_state(context, act_id)
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


def recoveries_so_far(context, act_id: str) -> int:
    """Requests already made for this act, after their paired history reconciles."""
    return len(recovery_state(context, act_id)["requests"])


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
    preflight_recovery_history(context)
    preflight_review_evidence(context)

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
                    "recoveries_used": 0,
                    "budget_allowed": budget["allowed"],
                    "absolute_cap": budget["absolute_cap"],
                },
            )
            held += 1
            continue

        _refuse_an_unhandled_designator_terminal(act)

        state = recovery_state(context, act_id)
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

        # The seal's continuation claim against the regions the tree actually
        # holds. A claim with only one proposal region — drift, tampering, or a
        # future bug reintroducing the declared-not-cut gap — may not be
        # accepted: the reading it would bless covers one side of a page break
        # while the record says it covers the act.
        proposal_count = len(
            [
                record
                for record in state["regions"]
                if _payload(record, f"Designator region of {act_id}").get("origin") == "proposal"
            ]
        )
        continuation_shortfall = act["has_continuation"] and proposal_count < 2

        used = recoveries_so_far(context, act_id)
        wants_recovery = act_key in scenario["recover_acts"] and used == 0
        ordinal = used + 1

        if not continuation_shortfall and wants_recovery and used < budget["allowed"]:
            # The Recensor asks; the Designator cuts. Recording the request as an
            # artifact is what keeps the loop countable from the tree alone.
            request = context.publish(
                kind="recovery-request",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recover", ordinal),
                inputs=[reading_ref],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "reason": "the crop may be incomplete; an expanded recrop is requested",
                    "budget_allowed": budget["allowed"],
                    "budget_used": used,
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
                    "coverage": coverage,
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
                f"the seal claims a continuation but only {proposal_count} proposal "
                "region(s) exist; accepting would deliver part of an act as the act",
            )
            held += 1
        elif act_key in scenario["hold_acts"]:
            outcome, reason = "held-for-review", "the act did not reconcile and needs a human"
            held += 1
        elif wants_recovery:
            outcome, reason = (
                "held-for-review",
                f"recovery budget of {budget['allowed']} is spent; holding rather than "
                "re-rolling, because recovery recovers coverage and never quality",
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
                "recoveries_used": used,
                "budget_allowed": budget["allowed"],
                "absolute_cap": budget["absolute_cap"],
                "perlectio_ref": reading_ref,
            },
        )

    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
