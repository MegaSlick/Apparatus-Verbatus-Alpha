from dataclasses import replace

import pytest

from operations.spike_perlector.errors import (
    MatrixRefusal,
    MeasurementRefusal,
    PromptFidelityRefusal,
)
from operations.spike_perlector.fakes import FakeCandidate, FakeReply
from operations.spike_perlector.gates import RunAuthorization
from operations.spike_perlector.models import (
    ALL_CONDITIONS,
    CandidateResponse,
    Condition,
    DissentSummary,
    GapSpan,
    GroundTruth,
    OutputStatus,
    PublicLimitationCode,
    ReferenceStatus,
)
from operations.spike_perlector.models import (
    Testimonium as WitnessTestimonium,  # bare `Testimonium` is collected as a test class
)
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.runner import (
    FailedAttemptKind,
    FailedCandidateAttempt,
    run_matrix,
)
from operations.spike_perlector.testkit import digest, evaluation_act, identity, registry


def run_three_candidates():
    base = identity("base-private", 1)
    vendor = identity("vendor-private", 2)
    checkpoint = identity("checkpoint-private", 3)
    act = evaluation_act()
    candidates = (
        FakeCandidate(base),
        FakeCandidate(vendor),
        FakeCandidate(checkpoint),
    )
    run = run_matrix(
        candidates,
        (act,),
        prompt_registry=registry(base, vendor, checkpoint),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    return run, candidates, act


def test_every_candidate_act_and_condition_is_accounted_for_once():
    run, candidates, act = run_three_candidates()
    assert len(run.cells) == len(candidates) * len(ALL_CONDITIONS)
    assert {cell.perlectio.condition for cell in run.cells} == set(ALL_CONDITIONS)
    assert len(run.witness_baselines) == len(act.testimonia)
    assert all(len(candidate.requests) == len(ALL_CONDITIONS) for candidate in candidates)


def test_common_dossier_bytes_are_identical_across_candidates_per_condition():
    _, candidates, _ = run_three_candidates()
    for condition in ALL_CONDITIONS:
        requests = [
            request
            for candidate in candidates
            for request in candidate.requests
            if request.dossier.condition is condition
        ]
        assert len({request.dossier.wire_sha256 for request in requests}) == 1
        if condition is Condition.LECTIO_NUDA:
            assert all(
                not request.dossier.testimonia and request.dossier.image for request in requests
            )
        elif condition is Condition.WITNESS_PRIMED:
            assert all(request.dossier.testimonia and request.dossier.image for request in requests)
        else:
            assert all(
                request.dossier.testimonia and request.dossier.image is None for request in requests
            )


def test_candidate_dossier_anonymizes_testimonium_source_identity():
    _, candidates, act = run_three_candidates()
    private_source_ids = {item.private_source_id.encode("utf-8") for item in act.testimonia}
    primed_request = next(
        request
        for request in candidates[0].requests
        if request.dossier.condition is Condition.WITNESS_PRIMED
    )
    assert all(
        not hasattr(testimonium, "private_source_id")
        for testimonium in primed_request.dossier.testimonia
    )
    assert all(
        source_id not in primed_request.dossier.wire_bytes for source_id in private_source_ids
    )


def test_missing_but_proved_response_remains_a_scored_matrix_cell():
    base = identity("base-private", 1)
    act = evaluation_act()
    candidate = FakeCandidate(
        base,
        replies={
            (act.opaque_act_id, Condition.WITNESS_PRIMED): FakeReply(OutputStatus.MISSING, None)
        },
    )
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(base),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    primed = next(
        row for row in run.condition_aggregates() if row.condition is Condition.WITNESS_PRIMED
    )
    assert primed.metrics.cell_count == 1
    assert primed.metrics.missing_count == 1
    assert primed.metrics.cer == 1


@pytest.mark.parametrize(
    "text",
    [
        "x" * 20_001,
        "alpha " + chr(0xD800) + " beta",
        "a" + chr(0x0301) * 31,
    ],
    ids=["over-length", "unpaired-surrogate", "combining-run"],
)
def test_an_unmeasurable_response_scores_malformed_with_the_adapters_own_receipts(text):
    """A delivered, receipted response whose text is outside the measurable bounds
    is README section 7's `malformed` state: the cell keeps the adapter's observed
    receipts, wall time and cost, scores as an empty hypothesis, and every other
    cell survives with it."""

    base = identity("base-private", 1)
    act = evaluation_act()
    candidate = FakeCandidate(
        base,
        replies={
            (act.opaque_act_id, Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, text, elapsed_ms=7.0, cost_usd=0.25
            )
        },
    )
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(base),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert run.failed_attempts == ()
    assert len(run.cells) == len(ALL_CONDITIONS)
    broken = next(cell for cell in run.cells if cell.perlectio.condition is Condition.LECTIO_NUDA)
    request = next(
        item for item in candidate.requests if item.dossier.condition is Condition.LECTIO_NUDA
    )
    assert broken.perlectio.status is OutputStatus.MALFORMED
    assert broken.perlectio.text is None
    assert broken.raw_response_text is None
    assert broken.perlectio.prompt_format_sha256 == request.prompt_format_sha256
    assert broken.perlectio.dossier_sha256 == request.dossier.wire_sha256
    assert broken.perlectio.delivery_sha256 == request.delivery_sha256
    assert broken.perlectio.elapsed_ms == 7.0
    assert broken.perlectio.cost_usd == 0.25
    assert broken.score.cer.rate == 1.0
    assert PublicLimitationCode.MALFORMED_CANDIDATE_RESPONSES_PRESENT in (
        run.derived_limitation_codes()
    )
    survivors = [
        cell for cell in run.cells if cell.perlectio.condition is not Condition.LECTIO_NUDA
    ]
    assert all(cell.perlectio.status is OutputStatus.COMPLETE for cell in survivors)


@pytest.mark.parametrize(
    "reply",
    [
        FakeReply(prompt_digest_override=""),
        FakeReply(dossier_digest_override=""),
        FakeReply(delivery_digest_override=""),
        FakeReply(dossier_digest_override="zz"),
        FakeReply(elapsed_ms=-1.0),
        FakeReply(cost_usd=-1.0),
        FakeReply(OutputStatus.COMPLETE, "   "),
    ],
    ids=[
        "blank-prompt-receipt",
        "blank-dossier-receipt",
        "blank-delivery-receipt",
        "non-hex-dossier-receipt",
        "negative-wall-time",
        "negative-cost",
        "complete-without-text",
    ],
)
def test_a_response_the_adapter_cannot_build_is_a_failed_attempt_not_a_score(reply):
    """An invalid receipt, timing or status/text shape is an adapter defect, not a
    model answer: it is retained as a failed attempt, the later reads still run,
    and the run cannot publish."""

    base = identity("base-private", 1)
    act = evaluation_act()
    candidate = FakeCandidate(base, replies={(act.opaque_act_id, Condition.LECTIO_NUDA): reply})
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(base),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(candidate.requests) == len(ALL_CONDITIONS)
    assert [failure.kind for failure in run.failed_attempts] == [
        FailedAttemptKind.ADAPTER_EXCEPTION
    ]
    assert run.failed_attempts[0].condition is Condition.LECTIO_NUDA
    assert all(cell.perlectio.condition is not Condition.LECTIO_NUDA for cell in run.cells)
    assert len(run.cells) == len(ALL_CONDITIONS) - 1
    with pytest.raises(MatrixRefusal, match="unproved or invalid candidate attempts"):
        run.require_publishable()


def test_adapter_dossier_digest_mismatch_is_retained_as_a_failed_attempt():
    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", dossier_digest_override="0" * 64
            )
        },
    )
    run = run_matrix(
        (candidate,),
        (evaluation_act(),),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(candidate.requests) == len(ALL_CONDITIONS)
    assert [failure.kind for failure in run.failed_attempts] == [
        FailedAttemptKind.DOSSIER_RECEIPT_MISMATCH
    ]
    with pytest.raises(MatrixRefusal, match="unproved or invalid candidate attempts"):
        run.require_publishable()


def test_an_adapter_returning_something_other_than_a_response_is_invalid_and_malformed():
    invalid_identity = identity("invalid-private", 1)

    class InvalidCandidate:
        identity = invalid_identity

        def read(self, request):
            return {"text": "alpha beta"}

    run = run_matrix(
        (InvalidCandidate(),),
        (evaluation_act(),),
        prompt_registry=registry(invalid_identity),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert run.cells == ()
    assert {failure.kind for failure in run.failed_attempts} == {FailedAttemptKind.INVALID_RESPONSE}
    assert run.derived_limitation_codes() == frozenset(
        {
            PublicLimitationCode.CANDIDATE_NONANSWERS_PRESENT,
            PublicLimitationCode.MALFORMED_CANDIDATE_RESPONSES_PRESENT,
        }
    )


def run_with_one_failed_attempt():
    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", prompt_digest_override="0" * 64
            )
        },
    )
    return run_matrix(
        (candidate,),
        (evaluation_act(),),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )


def test_a_planned_cell_cannot_be_both_a_perlectio_and_a_failed_attempt():
    run, _, _ = run_three_candidates()
    cell = run.cells[0]
    duplicate = FailedCandidateAttempt(
        identity=cell.perlectio.identity,
        opaque_act_id=cell.opaque_act_id,
        condition=cell.perlectio.condition,
        prompt_format_sha256=cell.perlectio.prompt_format_sha256,
        dossier_sha256=cell.perlectio.dossier_sha256,
        delivery_sha256=cell.perlectio.delivery_sha256,
        kind=FailedAttemptKind.ADAPTER_EXCEPTION,
        detail="synthetic",
    )
    with pytest.raises(MatrixRefusal, match="both a Perlectio and a failed attempt"):
        replace(run, failed_attempts=(duplicate,))


def test_a_failed_attempt_naming_an_identity_outside_its_run_is_refused():
    run = run_with_one_failed_attempt()
    failure = run.failed_attempts[0]
    forged = replace(failure, identity=replace(failure.identity, revision="revision-other"))
    with pytest.raises(MatrixRefusal, match="failed attempt names evidence outside its run"):
        replace(run, failed_attempts=(forged,))


def test_a_failed_attempt_must_bind_the_runs_exact_dossier():
    run = run_with_one_failed_attempt()
    forged = replace(run.failed_attempts[0], dossier_sha256=digest("other-dossier"))
    with pytest.raises(MatrixRefusal, match="failed attempt does not bind the run's exact dossier"):
        replace(run, failed_attempts=(forged,))


def test_a_cell_whose_dissent_differs_from_the_retained_witnesses_is_refused():
    run, _, _ = run_three_candidates()
    primed = next(
        cell for cell in run.cells if cell.perlectio.condition is Condition.WITNESS_PRIMED
    )
    assert primed.perlectio.dissent.compared > 0
    forged = replace(primed, perlectio=replace(primed.perlectio, dissent=DissentSummary(0, 0, 0)))
    cells = tuple(forged if cell is primed else cell for cell in run.cells)
    with pytest.raises(MatrixRefusal, match="dissent differs from the retained witness evidence"):
        replace(run, cells=cells)


def test_a_cell_whose_identity_differs_from_its_run_is_refused():
    run, _, _ = run_three_candidates()
    first = run.cells[0]
    forged = replace(
        first,
        perlectio=replace(
            first.perlectio,
            identity=replace(first.perlectio.identity, revision="revision-other"),
        ),
    )
    with pytest.raises(MatrixRefusal, match="resolved identity different from its run"):
        replace(run, cells=(forged, *run.cells[1:]))


def test_aggregate_cer_is_micro_averaged_across_acts_of_different_lengths():
    """README section 7: sum edit counts and denominators across acts, then
    divide once -- never average each act's own rate. With acts this
    different in length, the two conventions give different numbers, so a
    regression to macro-averaging cannot pass unnoticed."""

    base = identity("base-private", 1)
    short_act = evaluation_act("micro-short", text="ab")
    long_act = evaluation_act("micro-long", text="c" * 100)
    candidate = FakeCandidate(
        base,
        replies={
            **{
                (short_act.opaque_act_id, condition): FakeReply(OutputStatus.COMPLETE, "ab")
                for condition in ALL_CONDITIONS
            },
            **{
                (long_act.opaque_act_id, condition): FakeReply(OutputStatus.REFUSED, None)
                for condition in ALL_CONDITIONS
            },
        },
    )
    run = run_matrix(
        (candidate,),
        (short_act, long_act),
        prompt_registry=registry(base),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    nuda = next(row for row in run.condition_aggregates() if row.condition is Condition.LECTIO_NUDA)
    assert nuda.metrics.cer_errors == 100
    assert nuda.metrics.cer_reference_units == 102
    micro_cer = 100 / 102
    macro_cer = (0 / 2 + 100 / 100) / 2
    assert micro_cer != macro_cer
    assert nuda.metrics.cer == pytest.approx(micro_cer)


def test_a_refused_cell_retaining_stray_raw_text_is_refused_on_replay():
    """The replay must re-check raw_response_text, not the self-consistent
    Perlectio.text, which is None for a non-reading status either way."""

    base = identity("base-private", 1)
    act = evaluation_act()
    candidate = FakeCandidate(
        base,
        replies={(act.opaque_act_id, Condition.LECTIO_NUDA): FakeReply(OutputStatus.REFUSED, None)},
    )
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(base),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    refused_cell = next(
        cell for cell in run.cells if cell.perlectio.condition is Condition.LECTIO_NUDA
    )
    tampered_cell = replace(refused_cell, raw_response_text="stray text")
    tampered_cells = tuple(tampered_cell if cell is refused_cell else cell for cell in run.cells)
    with pytest.raises(MatrixRefusal, match="retains stray raw response text"):
        replace(run, cells=tampered_cells)


def test_no_readable_text_is_explicit_and_never_complete_empty_text():
    resolved = identity("candidate-private", 1)
    act = evaluation_act()
    candidate = FakeCandidate(
        resolved,
        replies={
            (act.opaque_act_id, condition): FakeReply(OutputStatus.NO_READABLE_TEXT, None)
            for condition in ALL_CONDITIONS
        },
    )
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert all(cell.perlectio.text is None for cell in run.cells)
    assert all(
        row.metrics.no_readable_text_count == row.metrics.cell_count
        for row in run.condition_aggregates()
    )
    request = candidate.requests[0]
    with pytest.raises(MeasurementRefusal, match="non-blank text"):
        CandidateResponse(
            status=OutputStatus.COMPLETE,
            text="",
            elapsed_ms=1.0,
            cost_usd=0.0,
            observed_prompt_sha256=request.prompt_format_sha256,
            observed_dossier_sha256=request.dossier.wire_sha256,
            observed_delivery_sha256=request.delivery_sha256,
        )


def test_a_cell_whose_act_id_disagrees_with_its_perlectio_is_refused():
    """Named for the check it reaches. It stops at the act-id comparison and
    never gets as far as the score replay, which has its own test below."""

    run, _, _ = run_three_candidates()
    first = run.cells[0]
    forged = replace(first, opaque_act_id=run.acts[0].opaque_act_id + "-other")
    with pytest.raises(MatrixRefusal, match="different acts"):
        replace(run, cells=(forged, *run.cells[1:]))


def test_measurement_run_replays_scores_instead_of_trusting_a_second_constructor():
    """The guard that stops a run carrying a CER no scoring pass produced.

    Nothing reached it: the only test naming the replay tripped the act-id check
    three lines earlier, so this refusal could have been deleted with the suite
    green and a tampered score would have travelled as measured evidence.
    """

    run, _, _ = run_three_candidates()
    first = run.cells[0]
    tampered = replace(first, score=replace(first.score, normalized_hypothesis_sha256="0" * 64))
    with pytest.raises(MatrixRefusal, match="score differs from its retained evidence"):
        replace(run, cells=(tampered, *run.cells[1:]))


def test_prompt_preflight_fails_before_any_candidate_is_called():
    known = identity("known-private", 1)
    missing = identity("missing-private", 2)
    candidates = (FakeCandidate(known), FakeCandidate(missing))
    with pytest.raises(PromptFidelityRefusal):
        run_matrix(
            candidates,
            (evaluation_act(),),
            prompt_registry=registry(known),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )
    assert all(not candidate.requests for candidate in candidates)


def test_adapter_prompt_digest_mismatch_is_retained_and_later_reads_still_run():
    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", prompt_digest_override="0" * 64
            )
        },
    )
    run = run_matrix(
        (candidate,),
        (evaluation_act(),),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(candidate.requests) == len(ALL_CONDITIONS)
    assert len(run.cells) == len(ALL_CONDITIONS) - 1
    assert [failure.kind.value for failure in run.failed_attempts] == ["prompt-receipt-mismatch"]
    with pytest.raises(MatrixRefusal, match="unproved or invalid candidate attempts"):
        run.require_publishable()


def test_a_condition_whose_only_read_failed_refuses_deltas_by_name_not_by_key_error():
    """Retaining a failed attempt removes that condition's aggregate entirely.

    `condition_aggregates` groups `self.cells`, and a condition whose every read
    became a `FailedCandidateAttempt` contributes none — so the group is absent
    rather than empty, and the subscript raised a bare `KeyError` out of a method
    whose callers hold on `MatrixRefusal`.
    """

    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.WITNESS_PRIMED): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", prompt_digest_override="0" * 64
            )
        },
    )
    run = run_matrix(
        (candidate,),
        (evaluation_act(),),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    with pytest.raises(MatrixRefusal, match="missing witness_primed"):
        run.condition_deltas()


def test_a_candidate_whose_every_read_failed_is_refused_not_quietly_dropped():
    """Deriving the slot list from the aggregates loses the candidate entirely.

    A candidate with no successful read contributes no aggregate, so a slot set
    built from `condition_aggregates()` never mentions it and `condition_deltas`
    returned the *other* candidates' deltas as though the matrix were whole.
    The loss has to be named, not skipped.
    """

    working = identity("working-private", 1)
    broken_identity = identity("broken-private", 2)

    class BrokenCandidate:
        identity = broken_identity

        def read(self, _request):
            raise RuntimeError("synthetic transport error")

    run = run_matrix(
        (FakeCandidate(working), BrokenCandidate()),
        (evaluation_act(),),
        prompt_registry=registry(working, broken_identity),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(run.failed_attempts) == len(ALL_CONDITIONS)
    with pytest.raises(MatrixRefusal, match="public slot 2"):
        run.condition_deltas()


def test_pairwise_deltas_refuse_an_unscored_denominator_instead_of_subtracting_none():
    """`None - None` is a `TypeError`, and the `KeyError` handler never caught it.

    The denominator refusal existed but sat *after* the subtraction, so it could
    not be reached. A blank reference makes every `cer` `None`, which is exactly
    the state this branch newly made reachable.
    """

    base = identity("base-private", 1)
    compared = identity("compared-private", 2)
    act = evaluation_act()
    blank = replace(
        act,
        ground_truth=GroundTruth(
            status=ReferenceStatus.NO_READABLE_TEXT,
            text=None,
            gaps=(),
            adjudication_digest=act.ground_truth.adjudication_digest,
            reference_revision=act.ground_truth.reference_revision,
        ),
    )
    run = run_matrix(
        (FakeCandidate(base), FakeCandidate(compared)),
        (blank,),
        prompt_registry=registry(base, compared),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    with pytest.raises(MatrixRefusal, match="pairwise deltas require checked CER denominators"):
        run.compare_to_base(compared_public_slot=2, base_public_slot=1)


def test_adapter_delivery_envelope_mismatch_is_retained_without_ending_the_act():
    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", delivery_digest_override="0" * 64
            )
        },
    )
    run = run_matrix(
        (candidate,),
        (evaluation_act(),),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(candidate.requests) == len(ALL_CONDITIONS)
    assert [failure.kind.value for failure in run.failed_attempts] == ["delivery-receipt-mismatch"]


def test_adapter_failure_is_retained_while_every_planned_read_is_attempted():
    broken_identity = identity("broken-private", 1)

    class BrokenCandidate:
        identity = broken_identity
        requests = []

        def read(self, request):
            self.requests.append(request)
            raise RuntimeError("synthetic transport error")

    candidate = BrokenCandidate()
    run = run_matrix(
        (candidate,),
        (evaluation_act(),),
        prompt_registry=registry(broken_identity),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(candidate.requests) == len(ALL_CONDITIONS)
    assert run.cells == ()
    assert len(run.failed_attempts) == len(ALL_CONDITIONS)
    assert {failure.kind.value for failure in run.failed_attempts} == {"adapter-exception"}
    assert run.derived_limitation_codes() == frozenset(
        {PublicLimitationCode.CANDIDATE_NONANSWERS_PRESENT}
    )


def test_blank_reference_is_read_in_every_condition_without_a_cer_or_wer_score():
    resolved = identity("candidate-private", 1)
    act = evaluation_act()
    blank_reference = GroundTruth(
        text=None,
        adjudication_digest=act.ground_truth.adjudication_digest,
        reference_revision=act.ground_truth.reference_revision,
        status=ReferenceStatus.NO_READABLE_TEXT,
        independent_draft_sha256s=act.ground_truth.independent_draft_sha256s,
    )
    unscoreable = replace(act, ground_truth=blank_reference)
    candidate = FakeCandidate(resolved)
    run = run_matrix(
        (candidate,),
        (unscoreable,),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(candidate.requests) == len(ALL_CONDITIONS)
    assert len(run.cells) == len(ALL_CONDITIONS)
    assert all(cell.score is None for cell in run.cells)
    assert all(baseline.score is None for baseline in run.witness_baselines)
    assert all(aggregate.metrics.cer is None for aggregate in run.condition_aggregates())
    with pytest.raises(MatrixRefusal, match="at least one checked CER/WER denominator"):
        run.require_publishable()


def test_unconfirmed_attestator_delivery_is_retained_but_cannot_publish():
    resolved = identity("candidate-private", 1)
    act = evaluation_act()
    unconfirmed = replace(
        act,
        testimonia=(replace(act.testimonia[0], delivery_confirmed=False),),
    )
    run = run_matrix(
        (FakeCandidate(resolved),),
        (unconfirmed,),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert len(run.cells) == len(ALL_CONDITIONS)
    with pytest.raises(MatrixRefusal, match="unconfirmed Attestator deliveries"):
        run.require_publishable()


def test_condition_and_pairwise_deltas_expose_witness_only_advantage_without_verdict():
    base = identity("base-private", 1)
    checkpoint = identity("checkpoint-private", 2)
    act = evaluation_act()
    base_replies = {
        (act.opaque_act_id, condition): FakeReply(OutputStatus.COMPLETE, "noise")
        for condition in ALL_CONDITIONS
    }
    checkpoint_replies = {
        (act.opaque_act_id, Condition.LECTIO_NUDA): FakeReply(OutputStatus.COMPLETE, "noise"),
        (act.opaque_act_id, Condition.WITNESS_PRIMED): FakeReply(
            OutputStatus.COMPLETE, "alpha beta"
        ),
        (act.opaque_act_id, Condition.IMAGE_ABSENT_CONTROL): FakeReply(
            OutputStatus.COMPLETE, "noise"
        ),
    }
    run = run_matrix(
        (FakeCandidate(base, base_replies), FakeCandidate(checkpoint, checkpoint_replies)),
        (act,),
        prompt_registry=registry(base, checkpoint),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    paired = run.compare_to_base(compared_public_slot=2, base_public_slot=1)
    assert paired.nuda_cer_advantage == 0
    assert paired.primed_cer_advantage > 0
    assert paired.witness_only_cer_advantage > 0
    assert {row.public_slot for row in run.condition_deltas()} == {1, 2}


def test_a_gapped_reference_is_scored_against_its_readable_ink_only():
    """A gapped checked reference, the common case, carried through the whole matrix.

    An act with unread ink in the middle of it is a checked reference with a gap,
    not an unreadable crop. A candidate that reproduces the readable ink exactly
    scores zero errors, and the denominator counts only what a human could read.
    """
    reader = identity("base-private", 1)
    act = evaluation_act(text="alpha UNREAD beta", gaps=(GapSpan(6, 13),))
    assert act.ground_truth.scoreable_text == "alpha beta"
    replies = {
        (act.opaque_act_id, condition): FakeReply(OutputStatus.COMPLETE, "alpha beta")
        for condition in ALL_CONDITIONS
    }
    candidate = FakeCandidate(reader, replies)
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(reader),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    for cell in run.cells:
        assert cell.score.cer.edits.errors == 0
        assert cell.score.cer.reference_units == len("alpha beta")


def test_a_cell_with_no_reading_in_it_records_no_dissent():
    """Dissent measures parroting, so a refusal is not maximal independence.

    `pipeline/4_perlector/run.py` publishes `"dissent": []` for an act it could not
    read; this matches that. The refusal is still counted in the cell's response
    state, so nothing is lost by not inventing a comparison.
    """
    reader = identity("base-private", 1)
    act = evaluation_act()
    replies = {
        (act.opaque_act_id, condition): FakeReply(OutputStatus.REFUSED, None)
        for condition in ALL_CONDITIONS
    }
    run = run_matrix(
        (FakeCandidate(reader, replies),),
        (act,),
        prompt_registry=registry(reader),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert all(cell.perlectio.dissent.compared == 0 for cell in run.cells)
    assert all(cell.perlectio.dissent.departed == 0 for cell in run.cells)
    for aggregate in run.condition_aggregates():
        assert aggregate.metrics.dissent_rate is None
        assert aggregate.metrics.refused_count == aggregate.metrics.cell_count


def test_each_witness_is_scored_directly_against_the_same_checked_ink():
    """ "Beats any witness alone" is unmeasurable without measuring the witnesses alone.

    The counts are hand-worked against the reference "alpha beta": an exact
    witness, one that reads the second word wrong (four character edits of ten,
    one word of two), and one that reported nothing (the whole reference deletes).
    """
    reader = identity("base-private", 1)
    act = evaluation_act(
        text="alpha beta",
        testimonia=(
            WitnessTestimonium(
                private_source_id="w-exact",
                public_source_index=1,
                opaque_act_id="synthetic-act-1",
                crop_sha256=digest("synthetic-image:synthetic-act-1"),
                delivery_attempted=True,
                delivery_confirmed=True,
                text="alpha beta",
            ),
            WitnessTestimonium(
                private_source_id="w-wrong",
                public_source_index=2,
                opaque_act_id="synthetic-act-1",
                crop_sha256=digest("synthetic-image:synthetic-act-1"),
                delivery_attempted=True,
                delivery_confirmed=True,
                text="alpha gamma",
            ),
            WitnessTestimonium(
                private_source_id="w-silent",
                public_source_index=3,
                opaque_act_id="synthetic-act-1",
                crop_sha256=digest("synthetic-image:synthetic-act-1"),
                delivery_attempted=True,
                delivery_confirmed=True,
                text=None,
                status=OutputStatus.REFUSED,
            ),
        ),
    )
    run = run_matrix(
        (FakeCandidate(reader),),
        (act,),
        prompt_registry=registry(reader),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    by_index = {row.public_source_index: row.metrics for row in run.witness_aggregates()}
    assert (by_index[1].cer, by_index[1].wer) == (0.0, 0.0)
    assert (by_index[2].cer_errors, by_index[2].cer_reference_units) == (4, 10)
    assert (by_index[2].wer_errors, by_index[2].wer_reference_units) == (1, 2)
    assert (by_index[3].cer, by_index[3].wer) == (1.0, 1.0)
    assert by_index[3].refused_count == 1
    # A witness baseline is an accuracy row, not a candidate cell: this instrument
    # observes no wall time or cost for one, and says so rather than reporting zero.
    assert by_index[1].mean_elapsed_ms is None
    assert by_index[1].mean_cost_usd is None


def test_a_reading_that_matches_a_witness_exactly_is_recorded_as_agreement():
    """The other half of the same instrument: agreement is the correct output.

    Most lines in a register are easy and every witness agrees. A measure that
    rewarded disagreement would reward hallucination.
    """
    reader = identity("base-private", 1)
    act = evaluation_act(text="alpha beta")
    replies = {
        (act.opaque_act_id, condition): FakeReply(OutputStatus.COMPLETE, "alpha beta")
        for condition in ALL_CONDITIONS
    }
    candidate = FakeCandidate(reader, replies)
    run = run_matrix(
        (candidate,),
        (act,),
        prompt_registry=registry(reader),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    assert all(cell.perlectio.dissent.compared == 1 for cell in run.cells)
    assert all(cell.perlectio.dissent.departed == 0 for cell in run.cells)
    nuda_request = next(
        request
        for request in candidate.requests
        if request.dossier.condition is Condition.LECTIO_NUDA
    )
    assert nuda_request.dossier.testimonia == ()
