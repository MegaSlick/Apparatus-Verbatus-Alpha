from dataclasses import replace

import pytest

from operations.spike_perlector.errors import HoldoutRefusal
from operations.spike_perlector.holdout import (
    SPEC05_SELECTION_SEED,
    FrameMember,
    HeldOutUseGuard,
    MaterialUse,
    PrivateSampleAccounting,
    ReferenceExclusion,
    SamplingPlan,
    select_manifest,
)
from operations.spike_perlector.models import MaterialClass, ReferenceStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1, normalize_text
from operations.spike_perlector.testkit import digest, evaluation_act


def member(number: int, stratum: str) -> FrameMember:
    return FrameMember(
        opaque_act_id=f"opaque-{number}",
        page_sha256=digest(f"page-{number}"),
        crop_sha256=digest(f"crop-{number}"),
        stratum=stratum,
        material_class=MaterialClass.CLEARED_PUBLIC,
        reference_status=ReferenceStatus.CHECKED,
        private_evidence_sha256s=(digest(f"private-evidence-{number}"),),
    )


def test_selection_is_deterministic_and_records_full_page_and_crop_material():
    frame = (member(1, "damaged"), member(2, "damaged"), member(3, "clear"))
    plan = SamplingPlan(SPEC05_SELECTION_SEED, {"damaged": 1, "clear": 1})
    first = select_manifest(frame, protocol_sha256=digest("protocol"), plan=plan)
    second = select_manifest(frame, protocol_sha256=digest("protocol"), plan=plan)
    assert first.members == second.members
    assert first.selected_sha256 == second.selected_sha256
    assert len(first.material_digests) == 6


def test_unapproved_seed_and_short_stratum_are_refused():
    with pytest.raises(HoldoutRefusal, match="predeclared"):
        SamplingPlan("post-hoc", {"clear": 1})
    plan = SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 2})
    with pytest.raises(HoldoutRefusal, match="below sealed quota"):
        select_manifest((member(1, "clear"),), protocol_sha256=digest("protocol"), plan=plan)


@pytest.mark.parametrize(
    "use",
    [
        MaterialUse.PROMPT_TUNING,
        MaterialUse.PADDING_CALIBRATION,
        MaterialUse.ADJUSTMENT,
        MaterialUse.BENCH,
        MaterialUse.DRESS_REHEARSAL,
    ],
)
def test_held_out_material_cannot_enter_any_prohibited_use(use):
    selected = member(1, "clear")
    manifest = select_manifest(
        (selected,),
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    guard = HeldOutUseGuard(manifest)
    with pytest.raises(HoldoutRefusal, match="overlaps"):
        guard.require_allowed((selected.page_sha256,), use=use)


def test_held_out_guard_refuses_unlineaged_later_material_and_neighbouring_crop():
    selected = member(1, "clear")
    manifest = select_manifest(
        (selected,),
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    guard = HeldOutUseGuard(manifest)
    with pytest.raises(HoldoutRefusal, match="unlineaged"):
        guard.require_allowed((digest("other-page"),), use=MaterialUse.BENCH)
    with pytest.raises(HoldoutRefusal):
        guard.require_allowed((selected.crop_sha256,), use=MaterialUse.DRESS_REHEARSAL)


def test_held_out_guard_hashes_and_refuses_reference_and_testimonium_payloads():
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    manifest = select_manifest(
        (
            FrameMember(
                opaque_act_id=act.opaque_act_id,
                page_sha256=act.image.source_page_sha256,
                crop_sha256=act.image.sha256,
                stratum="clear",
                material_class=act.image.material_class,
                reference_status=act.ground_truth.status,
                private_evidence_sha256s=tuple(sorted(act.private_evidence_sha256s)),
            ),
        ),
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    guard = HeldOutUseGuard(manifest)
    with pytest.raises(HoldoutRefusal, match="overlaps"):
        guard.require_allowed_payloads(
            (act.ground_truth.text or "",),
            use=MaterialUse.PROMPT_TUNING,
        )
    with pytest.raises(HoldoutRefusal, match="overlaps"):
        guard.require_allowed_payloads(
            (act.testimonia[0].text or "",),
            use=MaterialUse.BENCH,
        )


def test_held_out_guard_refuses_predeclared_normalized_evaluation_text():
    act = evaluation_act(text="alpha\tbeta", material_class=MaterialClass.CLEARED_PUBLIC)
    manifest = select_manifest(
        (
            FrameMember(
                opaque_act_id=act.opaque_act_id,
                page_sha256=act.image.source_page_sha256,
                crop_sha256=act.image.sha256,
                stratum="clear",
                material_class=act.image.material_class,
                reference_status=act.ground_truth.status,
                private_evidence_sha256s=tuple(sorted(act.private_evidence_sha256s)),
            ),
        ),
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    with pytest.raises(HoldoutRefusal, match="overlaps"):
        HeldOutUseGuard(manifest).require_allowed_payloads(
            (normalize_text(act.ground_truth.text or "", GRAPHEMIC_V1),),
            use=MaterialUse.PROMPT_TUNING,
        )


def test_lineage_bound_evaluation_text_cannot_leak_after_an_arbitrary_transformation():
    act = evaluation_act(text="alpha beta", material_class=MaterialClass.CLEARED_PUBLIC)
    manifest = select_manifest(
        (
            FrameMember(
                opaque_act_id=act.opaque_act_id,
                page_sha256=act.image.source_page_sha256,
                crop_sha256=act.image.sha256,
                stratum="clear",
                material_class=act.image.material_class,
                reference_status=act.ground_truth.status,
                private_evidence_sha256s=tuple(sorted(act.private_evidence_sha256s)),
            ),
        ),
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    guard = HeldOutUseGuard(manifest)
    bound = guard.bind_selected_payload(act.opaque_act_id, act.ground_truth.text or "")
    derived = bound.transform(lambda payload: payload.upper())  # type: ignore[union-attr]
    assert guard.material_for_use(derived, use=MaterialUse.EVALUATION) == "ALPHA BETA"
    with pytest.raises(HoldoutRefusal, match="cannot receive"):
        guard.material_for_use(derived, use=MaterialUse.PROMPT_TUNING)


def test_manifest_refuses_a_claimed_selection_that_is_not_its_own_deterministic_draw():
    frame = (member(1, "clear"), member(2, "clear"))
    manifest = select_manifest(
        frame,
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    replacement = next(item for item in frame if item not in manifest.members)
    with pytest.raises(HoldoutRefusal, match="deterministic selection"):
        replace(manifest, members=(replacement,))


def test_private_sample_accounting_requires_every_selected_act_to_score_or_exclude():
    frame = (
        member(1, "clear"),
        replace(member(2, "clear"), reference_status=ReferenceStatus.UNRESOLVED_GAP),
    )
    manifest = select_manifest(
        frame,
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 2}),
    )
    complete = PrivateSampleAccounting(
        manifest_sha256=manifest.manifest_sha256,
        scoreable_opaque_act_ids=(manifest.members[0].opaque_act_id,),
        exclusions=(
            ReferenceExclusion(
                opaque_act_id=manifest.members[1].opaque_act_id,
                status=ReferenceStatus.UNRESOLVED_GAP,
                reason_evidence_sha256=digest("unresolved-ink-reason"),
            ),
        ),
    )
    complete.require_complete_for(manifest)
    incomplete = PrivateSampleAccounting(
        manifest_sha256=manifest.manifest_sha256,
        scoreable_opaque_act_ids=(manifest.members[0].opaque_act_id,),
    )
    with pytest.raises(HoldoutRefusal, match="does not account"):
        incomplete.require_complete_for(manifest)
    false_status = PrivateSampleAccounting(
        manifest_sha256=manifest.manifest_sha256,
        scoreable_opaque_act_ids=(manifest.members[0].opaque_act_id,),
        exclusions=(
            ReferenceExclusion(
                opaque_act_id=manifest.members[1].opaque_act_id,
                status=ReferenceStatus.NO_READABLE_TEXT,
                reason_evidence_sha256=digest("wrong-reference-status"),
            ),
        ),
    )
    with pytest.raises(HoldoutRefusal, match="status differs"):
        false_status.require_complete_for(manifest)


def test_held_out_guard_refuses_an_unknown_runtime_use_name():
    selected = member(1, "clear")
    manifest = select_manifest(
        (selected,),
        protocol_sha256=digest("protocol"),
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"clear": 1}),
    )
    with pytest.raises(HoldoutRefusal, match="declared MaterialUse"):
        HeldOutUseGuard(manifest).require_allowed((digest("other"),), use="untyped")  # type: ignore[arg-type]
