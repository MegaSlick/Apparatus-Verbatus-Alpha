from dataclasses import replace

import pytest

from operations.spike_perlector.errors import (
    CandidateRosterRefusal,
    DisclosureRefusal,
    HoldoutRefusal,
    MatrixRefusal,
    MeasurementRefusal,
)
from operations.spike_perlector.fakes import FakeCandidate
from operations.spike_perlector.gates import RunAuthorization
from operations.spike_perlector.holdout import PrivateSampleAccounting, ReferenceExclusion
from operations.spike_perlector.models import (
    DeliveryMode,
    GroundTruth,
    MaterialClass,
    ReferenceStatus,
    WitnessConfiguration,
    WitnessSource,
)
from operations.spike_perlector.models import (
    Testimonium as WitnessTestimonium,
)
from operations.spike_perlector.normalization import GRAPHEMIC_V1, NormalizationProfile
from operations.spike_perlector.prompting import PromptRegistry
from operations.spike_perlector.roster import (
    FORBIDDEN_ATTESTATOR_SOURCE,
    STOCK_BASE_SOURCE,
    CandidateRoster,
    validate_perlector_candidate,
)
from operations.spike_perlector.runner import run_declared_roster_matrix, run_matrix
from operations.spike_perlector.testkit import (
    cleared_public_authorization_for,
    digest,
    evaluation_act,
    identity,
    manifest_for,
    prompt_format,
    registry,
    witness_configuration_for,
)


def valid_roster():
    return CandidateRoster(
        stock_base=identity("stock-private", 1, source_ref=STOCK_BASE_SOURCE),
        vendor_unaltered=identity(
            "vendor-private",
            2,
            source_ref="synthetic/vendor",
            delivery=DeliveryMode.EXTERNAL,
            provider="synthetic-vendor",
        ),
        trained_checkpoint=identity("checkpoint-private", 3),
        vendor_unaltered_evidence_sha256=digest("vendor-unaltered-evidence"),
        checkpoint_repository_evidence_sha256=digest("checkpoint-repository-evidence"),
    )


def test_settled_qwen_base_and_three_ordinary_identities_validate():
    valid_roster().validate()


def test_teklia_attestator_is_structurally_refused_as_candidate():
    roster = CandidateRoster(
        stock_base=identity("stock-private", 1, source_ref=STOCK_BASE_SOURCE),
        vendor_unaltered=identity(
            "vendor-private",
            2,
            source_ref=FORBIDDEN_ATTESTATOR_SOURCE,
            delivery=DeliveryMode.EXTERNAL,
            provider="synthetic-vendor",
        ),
        trained_checkpoint=identity("checkpoint-private", 3),
        vendor_unaltered_evidence_sha256=digest("vendor-unaltered-evidence"),
        checkpoint_repository_evidence_sha256=digest("checkpoint-repository-evidence"),
    )
    with pytest.raises(CandidateRosterRefusal, match="Attestator"):
        roster.validate()


def test_wrong_stock_base_source_is_refused():
    roster = CandidateRoster(
        stock_base=identity("stock-private", 1, source_ref="other/base"),
        vendor_unaltered=identity(
            "vendor-private",
            2,
            delivery=DeliveryMode.EXTERNAL,
            provider="synthetic-vendor",
        ),
        trained_checkpoint=identity("checkpoint-private", 3),
        vendor_unaltered_evidence_sha256=digest("vendor-unaltered-evidence"),
        checkpoint_repository_evidence_sha256=digest("checkpoint-repository-evidence"),
    )
    with pytest.raises(CandidateRosterRefusal, match="settled"):
        roster.validate()


def test_execution_boundary_also_refuses_teklia_without_a_roster_wrapper():
    forbidden = identity("forbidden-private", 1, source_ref=FORBIDDEN_ATTESTATOR_SOURCE)
    with pytest.raises(CandidateRosterRefusal, match="Attestator"):
        run_matrix(
            (FakeCandidate(forbidden),),
            (evaluation_act(),),
            prompt_registry=registry(forbidden),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )


def test_declared_run_entry_accepts_the_full_sealed_three_candidate_roster():
    """The happy path. Named for what it does; the refusal has its own test below."""

    roster = valid_roster()
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    witnesses = witness_configuration_for(act)
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    run = run_declared_roster_matrix(
        candidates,
        (act,),
        roster=roster,
        witness_configuration=witnesses,
        manifest=manifest,
        prompt_registry=prompts,
        profile=GRAPHEMIC_V1,
        authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
    )
    assert len(run.cells) == 9


def test_declared_run_entry_refuses_fewer_than_the_sealed_three_candidates():
    """The refusal the test above was named for but never reached.

    Its body supplied the whole roster and asserted success, so the
    three-candidate requirement could have been deleted with the suite green and
    a declared run could have gone ahead against two of the three sealed models.
    """

    roster = valid_roster()
    two_of_three = tuple(FakeCandidate(resolved) for resolved in roster.identities())[:2]
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    witnesses = witness_configuration_for(act)
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    with pytest.raises(MatrixRefusal, match="exactly the sealed three-candidate roster"):
        run_declared_roster_matrix(
            two_of_three,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )


class _ForgedIdentity:
    """A caller-controlled stand-in that agrees with anything it is compared to.

    Not a ResolvedIdentity at all -- dict equality (``actual != expected``)
    calls the *left* operand's own ``__eq__`` first, so an untrusted object
    like this one, given the real vendor candidate's ``candidate_key``, could
    otherwise satisfy the roster-match check regardless of what it claims
    about itself (e.g. a ``delivery`` of "local" that would skip the
    third-party-transmission approval a real external candidate requires).
    """

    def __init__(self, candidate_key: str) -> None:
        self.candidate_key = candidate_key
        self.delivery = DeliveryMode.LOCAL

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


def test_declared_run_refuses_a_candidate_whose_identity_is_not_really_one():
    roster = valid_roster()
    forged = _ForgedIdentity(roster.vendor_unaltered.candidate_key)
    candidates = tuple(
        FakeCandidate(forged if resolved is roster.vendor_unaltered else resolved)
        for resolved in roster.identities()
    )
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    witnesses = witness_configuration_for(act)
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    with pytest.raises(MatrixRefusal, match="not a stand-in"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )


def test_declared_run_refuses_a_caller_supplied_protocol_digest():
    roster = valid_roster()
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    witnesses = witness_configuration_for(act)
    approved_manifest = manifest_for(act)
    substituted_manifest = replace(
        approved_manifest,
        protocol_sha256=digest("post-hoc-protocol-substitution"),
    )
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(MatrixRefusal, match="exact predeclared Spec 05 protocol"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=substituted_manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(
                substituted_manifest,
                roster,
                witnesses,
                prompts,
            ),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def _cleared_public_authorization(
    manifest, roster: CandidateRoster, witnesses, prompts, sample_accounting=None
) -> RunAuthorization:
    return cleared_public_authorization_for(
        manifest=manifest,
        roster=roster,
        witness_configuration=witnesses,
        prompt_registry=prompts,
        profile=GRAPHEMIC_V1,
        sample_accounting=sample_accounting,
    )


def test_declared_run_refuses_an_unselected_act_before_any_candidate_call():
    roster = valid_roster()
    selected = evaluation_act("selected", material_class=MaterialClass.CLEARED_PUBLIC)
    substituted = evaluation_act("substituted", material_class=MaterialClass.CLEARED_PUBLIC)
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    witnesses = witness_configuration_for(substituted)
    manifest = manifest_for(selected)
    prompts = registry(*roster.identities())
    with pytest.raises(HoldoutRefusal, match="not exactly"):
        run_declared_roster_matrix(
            candidates,
            (substituted,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_declared_run_refuses_a_changed_same_name_normalization_profile_before_call():
    roster = valid_roster()
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    witnesses = witness_configuration_for(act)
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    with pytest.raises(MeasurementRefusal, match="differs from its predeclared canonical"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=NormalizationProfile("graphemic-v1", map_long_s=False),
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_declared_run_plan_refuses_a_changed_declared_prompt_before_any_call():
    roster = valid_roster()
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    witnesses = witness_configuration_for(act)
    manifest = manifest_for(act)
    approved_prompts = registry(*roster.identities())
    changed_prompts = PromptRegistry(
        {
            item.candidate_key: prompt_format(
                item,
                raw=(b"changed-declared-format" if item is roster.stock_base else None),
            )
            for item in roster.identities()
        }
    )
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(DisclosureRefusal, match="does not name this exact protocol"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=changed_prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(
                manifest, roster, witnesses, approved_prompts
            ),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_roster_refuses_duplicate_artifacts_under_different_candidate_roles():
    roster = valid_roster()
    duplicate_vendor = replace(
        roster.vendor_unaltered, artifact_digest=roster.stock_base.artifact_digest
    )
    with pytest.raises(CandidateRosterRefusal, match="distinct source repositories and artifacts"):
        replace(roster, vendor_unaltered=duplicate_vendor).validate()


def test_declared_run_refuses_a_missing_witness_stub_before_any_candidate_call():
    roster = valid_roster()
    first = evaluation_act("first", material_class=MaterialClass.CLEARED_PUBLIC)
    second = evaluation_act(
        "second",
        material_class=MaterialClass.CLEARED_PUBLIC,
        testimonia=(
            WitnessTestimonium(
                private_source_id="different-source-without-stub",
                public_source_index=2,
                opaque_act_id="second",
                crop_sha256=digest("synthetic-image:second"),
                delivery_attempted=True,
                delivery_confirmed=True,
                text="alpha beta",
            ),
        ),
    )
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    witnesses = witness_configuration_for(first)
    manifest = manifest_for(first, second)
    prompts = registry(*roster.identities())
    with pytest.raises(MatrixRefusal, match="same Testimonium sources"):
        run_declared_roster_matrix(
            candidates,
            (first, second),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_declared_run_refuses_a_witness_configuration_not_matching_the_act():
    roster = valid_roster()
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    wrong_witnesses = WitnessConfiguration(
        sources=(
            WitnessSource(
                private_source_id="other-private-source",
                public_source_index=2,
                source_ref="synthetic/other-attestator",
                revision="other-revision",
                artifact_digest=digest("other-attestator-artifact"),
            ),
        ),
        approval_evidence_sha256=digest("different-witness-configuration"),
    )
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(MatrixRefusal, match="sealed witness configuration"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=wrong_witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, wrong_witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_declared_run_refuses_a_candidate_that_is_its_own_attestator():
    roster = valid_roster()
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    ordinary_witnesses = witness_configuration_for(act)
    overlapping_witnesses = replace(
        ordinary_witnesses,
        sources=(
            replace(
                ordinary_witnesses.sources[0],
                source_ref=roster.vendor_unaltered.source_ref,
                revision=roster.vendor_unaltered.revision,
                artifact_digest=roster.vendor_unaltered.artifact_digest,
            ),
        ),
    )
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(MatrixRefusal, match="shares an Attestator"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=overlapping_witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(
                manifest, roster, overlapping_witnesses, prompts
            ),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_declared_run_refuses_an_image_classification_not_bound_by_manifest():
    roster = valid_roster()
    cleared_act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    relabelled_act = replace(
        cleared_act,
        image=replace(cleared_act.image, material_class=MaterialClass.PRIVATE_REGISTER),
    )
    witnesses = witness_configuration_for(relabelled_act)
    manifest = manifest_for(cleared_act)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(HoldoutRefusal, match="classification"):
        run_declared_roster_matrix(
            candidates,
            (relabelled_act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_declared_run_refuses_private_reference_evidence_not_bound_by_manifest():
    roster = valid_roster()
    checked_act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    altered_act = replace(
        checked_act,
        ground_truth=replace(checked_act.ground_truth, text="different checked ink"),
    )
    witnesses = witness_configuration_for(altered_act)
    manifest = manifest_for(checked_act)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(
        HoldoutRefusal,
        match="run acts are not exactly the material, classification, and evidence in the sealed manifest",
    ):
        run_declared_roster_matrix(
            candidates,
            (altered_act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(manifest, roster, witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_a_declared_run_refuses_a_checked_reference_nobody_independently_drafted():
    """Nothing could reach this refusal, because every fixture act had two drafts.

    `GroundTruth` refuses one draft outright, so the runner's own check is
    reachable only with none — which the model does allow, for a fixture. The
    rule it enforces is that two people transcribed an act independently before
    any machine reading is compared against it: the guard against one person's
    guess becoming the established reading for a parish. It could have been
    deleted with the whole suite still green.
    """

    roster = valid_roster()
    undrafted = evaluation_act(
        "undrafted",
        material_class=MaterialClass.CLEARED_PUBLIC,
        independent_draft_sha256s=(),
    )
    witnesses = witness_configuration_for(undrafted)
    manifest = manifest_for(undrafted)
    accounting = PrivateSampleAccounting.all_scoreable(manifest)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(
        MatrixRefusal,
        match="lacks two independent transcription drafts before adjudication",
    ):
        run_declared_roster_matrix(
            candidates,
            (undrafted,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(
                manifest,
                roster,
                witnesses,
                prompts,
                sample_accounting=accounting,
            ),
            sample_accounting=accounting,
        )


def test_declared_run_reads_an_unresolved_selected_act_without_scoring_it():
    roster = valid_roster()
    scoreable = evaluation_act("scoreable", material_class=MaterialClass.CLEARED_PUBLIC)
    selected_but_unread = evaluation_act(
        "unread",
        material_class=MaterialClass.CLEARED_PUBLIC,
    )
    unread_reference = GroundTruth(
        text=None,
        adjudication_digest=selected_but_unread.ground_truth.adjudication_digest,
        reference_revision=selected_but_unread.ground_truth.reference_revision,
        status=ReferenceStatus.UNRESOLVED_GAP,
        independent_draft_sha256s=selected_but_unread.ground_truth.independent_draft_sha256s,
    )
    selected_but_unread = replace(selected_but_unread, ground_truth=unread_reference)
    manifest = manifest_for(scoreable, selected_but_unread)
    accounting = PrivateSampleAccounting(
        manifest_sha256=manifest.manifest_sha256,
        scoreable_opaque_act_ids=(scoreable.opaque_act_id,),
        exclusions=(
            ReferenceExclusion(
                opaque_act_id=selected_but_unread.opaque_act_id,
                status=ReferenceStatus.UNRESOLVED_GAP,
                reason_evidence_sha256=digest("unread-selected-act-reason"),
            ),
        ),
    )
    witnesses = witness_configuration_for(scoreable)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    run = run_declared_roster_matrix(
        candidates,
        (scoreable, selected_but_unread),
        roster=roster,
        witness_configuration=witnesses,
        manifest=manifest,
        prompt_registry=prompts,
        profile=GRAPHEMIC_V1,
        authorization=_cleared_public_authorization(
            manifest,
            roster,
            witnesses,
            prompts,
            sample_accounting=accounting,
        ),
        sample_accounting=accounting,
    )
    assert len(run.cells) == 18
    assert all(len(candidate.requests) == 6 for candidate in candidates)
    assert all(
        cell.score is None
        for cell in run.cells
        if cell.opaque_act_id == selected_but_unread.opaque_act_id
    )
    assert run.sample_accounting == accounting


def test_declared_run_refuses_mixed_selected_material_classes_even_when_one_is_excluded():
    roster = valid_roster()
    scoreable = evaluation_act("clear-scoreable", material_class=MaterialClass.CLEARED_PUBLIC)
    private_unread = evaluation_act("private-unread", material_class=MaterialClass.PRIVATE_REGISTER)
    private_unread = replace(
        private_unread,
        ground_truth=GroundTruth(
            text=None,
            adjudication_digest=private_unread.ground_truth.adjudication_digest,
            reference_revision=private_unread.ground_truth.reference_revision,
            status=ReferenceStatus.NO_READABLE_TEXT,
            independent_draft_sha256s=private_unread.ground_truth.independent_draft_sha256s,
        ),
    )
    manifest = manifest_for(scoreable, private_unread)
    accounting = PrivateSampleAccounting(
        manifest_sha256=manifest.manifest_sha256,
        scoreable_opaque_act_ids=(scoreable.opaque_act_id,),
        exclusions=(
            ReferenceExclusion(
                opaque_act_id=private_unread.opaque_act_id,
                status=ReferenceStatus.NO_READABLE_TEXT,
                reason_evidence_sha256=digest("private-unread-reason"),
            ),
        ),
    )
    witnesses = witness_configuration_for(scoreable)
    prompts = registry(*roster.identities())
    candidates = tuple(FakeCandidate(resolved) for resolved in roster.identities())
    with pytest.raises(DisclosureRefusal, match="every supplied image evidence record"):
        run_declared_roster_matrix(
            candidates,
            (scoreable, private_unread),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_cleared_public_authorization(
                manifest,
                roster,
                witnesses,
                prompts,
                sample_accounting=accounting,
            ),
            sample_accounting=accounting,
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_the_no_self_witness_refusal_survives_a_differently_spelled_source_ref():
    """The rule is structural, so the comparison must not be a string match.

    A source reference is written by hand into configuration, and the same model
    arrives spelled several ways. Compared exactly, each of these walked the
    Attestator straight past the one check standing in its way — and a refusal
    that does not fire leaves no trace. GOVERNANCE 3 and hard rule 8: the
    Perlector reads and never picks among witnesses, so a witness in the
    candidate roster is that rule broken at the root. Found by CodeRabbit.
    """

    for spelling in (
        f"  {FORBIDDEN_ATTESTATOR_SOURCE}  ",
        FORBIDDEN_ATTESTATOR_SOURCE.upper(),
        FORBIDDEN_ATTESTATOR_SOURCE.lower(),
        f"{FORBIDDEN_ATTESTATOR_SOURCE}@main",
        f"{FORBIDDEN_ATTESTATOR_SOURCE}@3f9a1c2",
    ):
        with pytest.raises(CandidateRosterRefusal, match="Attestator"):
            validate_perlector_candidate(identity("forbidden-private", 1, source_ref=spelling))


def test_a_genuinely_different_model_is_still_accepted_at_that_boundary():
    """Invariant #14: the strictness above must not have been bought by refusing
    good input. A repository whose name merely contains the forbidden one, or
    shares its owner, is a different model and passes."""

    for allowed in (
        STOCK_BASE_SOURCE,
        "Teklia/Some-Other-Model",
        f"NotTeklia/{FORBIDDEN_ATTESTATOR_SOURCE.split('/', 1)[1]}",
    ):
        validate_perlector_candidate(identity("fine-private", 1, source_ref=allowed))


def test_a_configured_witness_cannot_be_a_candidate_under_any_spelling():
    """The same structural rule as `validate_perlector_candidate`, against the
    *actually configured* witnesses rather than one hardcoded constant — which
    makes this the one that matters more. It stayed an exact string match after
    the other was normalized, so all four spellings walked a live witness into
    the candidate roster. GOVERNANCE 3, hard rule 8: self-witness agreement is
    not evidence. Found by the Opus read of this branch.
    """

    act = evaluation_act()
    witnesses = witness_configuration_for(act)
    witness_source = witnesses.sources[0].source_ref
    for spelling in (
        witness_source,
        f"  {witness_source}  ",
        witness_source.upper(),
        witness_source.lower(),
        f"{witness_source}@main",
    ):
        candidate = identity("shadow-private", 1, source_ref=spelling)
        with pytest.raises(MatrixRefusal, match="shares an Attestator"):
            witnesses.require_distinct_from_candidates((candidate,))


def test_a_roster_naming_one_model_three_ways_is_not_three_distinct_sources():
    """`len(set(sources))` on raw strings counted three spellings as three models.

    The three roles exist to be genuinely different — a stock base, an unaltered
    vendor model and a trained checkpoint — and a roster that is one model wearing
    three spellings defeats the comparison the whole instrument is for. Found by
    the Opus read of this branch.
    """

    roster = CandidateRoster(
        stock_base=identity("a", 1, source_ref=STOCK_BASE_SOURCE),
        vendor_unaltered=identity(
            "b",
            2,
            source_ref=f"{STOCK_BASE_SOURCE} ",
            delivery=DeliveryMode.EXTERNAL,
            provider="synthetic-vendor",
        ),
        trained_checkpoint=identity("c", 3, source_ref=f"{STOCK_BASE_SOURCE}@main"),
        vendor_unaltered_evidence_sha256=digest("vendor-evidence"),
        checkpoint_repository_evidence_sha256=digest("checkpoint-evidence"),
    )
    with pytest.raises(CandidateRosterRefusal, match="three distinct source repositories"):
        roster.validate()
