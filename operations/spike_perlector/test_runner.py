from dataclasses import replace

import pytest

from operations.spike_perlector.errors import MatrixRefusal, PromptFidelityRefusal
from operations.spike_perlector.fakes import FakeCandidate, FakeReply
from operations.spike_perlector.gates import RunAuthorization
from operations.spike_perlector.models import (
    ALL_CONDITIONS,
    Condition,
    GroundTruth,
    OutputStatus,
    ReferenceStatus,
)
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.runner import run_matrix
from operations.spike_perlector.testkit import evaluation_act, identity, registry


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


def test_adapter_prompt_digest_mismatch_is_a_named_fidelity_refusal():
    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", prompt_digest_override="0" * 64
            )
        },
    )
    with pytest.raises(PromptFidelityRefusal, match="did not observe"):
        run_matrix(
            (candidate,),
            (evaluation_act(),),
            prompt_registry=registry(resolved),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )


def test_adapter_delivery_envelope_mismatch_is_a_named_fidelity_refusal():
    resolved = identity("candidate-private", 1)
    candidate = FakeCandidate(
        resolved,
        replies={
            ("synthetic-act-1", Condition.LECTIO_NUDA): FakeReply(
                OutputStatus.COMPLETE, "alpha beta", delivery_digest_override="0" * 64
            )
        },
    )
    with pytest.raises(PromptFidelityRefusal, match="delivery envelope"):
        run_matrix(
            (candidate,),
            (evaluation_act(),),
            prompt_registry=registry(resolved),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )


def test_adapter_failure_invalidates_the_matrix_instead_of_scoring_a_harness_defect():
    broken_identity = identity("broken-private", 1)

    class BrokenCandidate:
        identity = broken_identity

        def read(self, _request):
            raise RuntimeError("synthetic transport error")

    with pytest.raises(MatrixRefusal, match="invalid"):
        run_matrix(
            (BrokenCandidate(),),
            (evaluation_act(),),
            prompt_registry=registry(broken_identity),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )


def test_blank_and_unresolved_ink_are_not_empty_perfect_reference_rows():
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
    with pytest.raises(MatrixRefusal, match="private sample accounting"):
        run_matrix(
            (candidate,),
            (unscoreable,),
            prompt_registry=registry(resolved),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )
    assert candidate.requests == []


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
