"""Synthetic builders shared by Spec 05 tests; these values are not register data."""

from __future__ import annotations

import json

from common.contracts.approval import ApprovalRecordReference, build_approval_record

from .encoding import sha256_bytes
from .gates import (
    RUN_PLAN_APPROVAL_SUBJECT,
    RunAuthorization,
    RunPlanApproval,
)
from .holdout import (
    SPEC05_SELECTION_SEED,
    FrameMember,
    PrivateSampleAccounting,
    SamplingPlan,
    select_manifest,
)
from .models import (
    DeliveryMode,
    EvaluationAct,
    GapSpan,
    GroundTruth,
    ImageEvidence,
    MaterialClass,
    OutputStatus,
    ResolvedIdentity,
    Testimonium,
    WitnessConfiguration,
    WitnessSource,
)
from .prompting import PromptFormat, PromptRegistry
from .protocol import PREDECLARED_PROTOCOL_SHA256


def digest(label: str) -> str:
    return sha256_bytes(label.encode("utf-8"))


def identity(
    key: str,
    slot: int,
    *,
    source_ref: str | None = None,
    delivery: DeliveryMode = DeliveryMode.LOCAL,
    provider: str | None = None,
) -> ResolvedIdentity:
    return ResolvedIdentity(
        candidate_key=key,
        public_slot=slot,
        # `is None`, not `or` — the same defect fixed two lines below in
        # `evaluation_act`, and it survived that fix. An explicitly supplied
        # empty `source_ref` is exactly what a test probing the source-comparison
        # rules wants, and `or` silently replaced it with a well-formed synthetic
        # one, so the test would have proved nothing about the empty case. Found
        # by the Opus read of this branch.
        source_ref=f"synthetic/{key}" if source_ref is None else source_ref,
        revision=f"revision-{key}",
        artifact_digest=digest(f"artifact-{key}"),
        delivery=delivery,
        provider=provider,
    )


def prompt_format(resolved: ResolvedIdentity, raw: bytes | None = None) -> PromptFormat:
    content = raw if raw is not None else f"synthetic-format:{resolved.candidate_key}".encode()
    return PromptFormat(
        candidate_key=resolved.candidate_key,
        source_ref=resolved.source_ref,
        revision=resolved.revision,
        artifact_digest=resolved.artifact_digest,
        raw_bytes=content,
        declared_sha256=sha256_bytes(content),
        source_evidence_sha256=digest(f"format-evidence-{resolved.candidate_key}"),
        modality_contract="image-and-testimonia-v1",
    )


def registry(*identities: ResolvedIdentity) -> PromptRegistry:
    return PromptRegistry({item.candidate_key: prompt_format(item) for item in identities})


def evaluation_act(
    opaque_act_id: str = "synthetic-act-1",
    *,
    text: str = "alpha beta",
    gaps: tuple[GapSpan, ...] = (),
    testimonia: tuple[Testimonium, ...] | None = None,
    material_class: MaterialClass = MaterialClass.SYNTHETIC,
    independent_draft_sha256s: tuple[str, ...] | None = None,
) -> EvaluationAct:
    image_bytes = f"synthetic-image:{opaque_act_id}".encode("utf-8")
    crop_sha256 = sha256_bytes(image_bytes)
    return EvaluationAct(
        opaque_act_id=opaque_act_id,
        image=ImageEvidence(
            opaque_page_id=f"synthetic-page:{opaque_act_id}",
            sha256=crop_sha256,
            source_page_sha256=digest(f"source-page:{opaque_act_id}"),
            material_class=material_class,
            payload=image_bytes,
        ),
        ground_truth=GroundTruth(
            text=text,
            adjudication_digest=digest(f"adjudication:{opaque_act_id}"),
            reference_revision="synthetic-reference-v1",
            # Overridable, and `is None` so an explicit empty tuple survives:
            # every fixture act carried exactly two drafts, so no test in this
            # cohort could reach the runner's refusal of a checked reference
            # adjudicated from any other number. That refusal is what stops one
            # person's guess becoming an established reading, and it could have
            # been deleted with the suite still green.
            independent_draft_sha256s=(
                (
                    digest(f"draft-a:{opaque_act_id}"),
                    digest(f"draft-b:{opaque_act_id}"),
                )
                if independent_draft_sha256s is None
                else independent_draft_sha256s
            ),
            gaps=gaps,
        ),
        # `is None`, not truthiness: an explicitly supplied empty tuple means a
        # caller wants an act with no witnesses, and `or` silently handed it the
        # default witness instead — so a test written to prove behaviour with no
        # Testimonia was proving it with one. Found by CodeRabbit.
        testimonia=testimonia
        if testimonia is not None
        else (
            Testimonium(
                private_source_id="synthetic-source-private",
                public_source_index=1,
                opaque_act_id=opaque_act_id,
                crop_sha256=crop_sha256,
                delivery_attempted=True,
                delivery_confirmed=True,
                text=text,
                status=OutputStatus.COMPLETE,
            ),
        ),
    )


def witness_configuration_for(act: EvaluationAct) -> WitnessConfiguration:
    return WitnessConfiguration(
        sources=tuple(
            WitnessSource(
                private_source_id=item.private_source_id,
                public_source_index=item.public_source_index,
                source_ref=f"synthetic/attestator/{item.private_source_id}",
                revision=f"revision-{item.private_source_id}",
                artifact_digest=digest(f"attestator-artifact:{item.private_source_id}"),
            )
            for item in act.testimonia
        ),
        approval_evidence_sha256=digest("synthetic-witness-configuration"),
    )


def manifest_for(*acts: EvaluationAct):
    members = tuple(
        FrameMember(
            opaque_act_id=act.opaque_act_id,
            page_sha256=act.image.source_page_sha256,
            crop_sha256=act.image.sha256,
            stratum="synthetic",
            material_class=act.image.material_class,
            reference_status=act.ground_truth.status,
            private_evidence_sha256s=tuple(sorted(act.private_evidence_sha256s)),
        )
        for act in acts
    )
    return select_manifest(
        members,
        protocol_sha256=PREDECLARED_PROTOCOL_SHA256,
        plan=SamplingPlan(SPEC05_SELECTION_SEED, {"synthetic": len(members)}),
    )


def _test_approval_reference(
    target_version_hash: str, *, subject_id: str
) -> tuple[ApprovalRecordReference, bytes]:
    """Return a test-only generic approval artifact for one sealed target hash."""

    record = build_approval_record(
        subject_ids=[subject_id],
        action="other",
        reason="synthetic test fixture; no evaluation material or decision",
        target_version_hash=target_version_hash,
        timestamp="2026-08-08T00:00:00Z",
    )
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_sha256 = sha256_bytes(payload)
    reference = ApprovalRecordReference(f"receipts/sha256/{record_sha256}.json", record_sha256)
    return reference, payload


def run_plan_approval_for(
    *,
    manifest,
    roster,
    witness_configuration: WitnessConfiguration,
    prompt_registry,
    profile,
    sample_accounting: PrivateSampleAccounting | None = None,
) -> RunPlanApproval:
    """Build a test-only artifact shaped like Tyrel's real declared run-plan approval."""

    accounting = (
        PrivateSampleAccounting.all_scoreable(manifest)
        if sample_accounting is None
        else sample_accounting
    )
    engineering_declaration = {
        "protocol_sha256": manifest.protocol_sha256,
        "manifest_sha256": manifest.manifest_sha256,
        "candidate_roster_sha256": roster.digest,
        "witness_configuration_sha256": witness_configuration.digest,
        "prompt_registry_sha256": prompt_registry.snapshot_sha256(roster.identities()),
        "normalization_profile_id": profile.profile_id,
        "normalization_profile_sha256": profile.digest,
        "private_sample_accounting_sha256": accounting.digest,
    }
    reserved_scope = {
        "prove_before_scale_evidence_sha256": digest("synthetic-small-reasonable-well"),
        "spend_scope_sha256": digest("synthetic-zero-spend-scope"),
        "disclosure_scope_sha256": digest("synthetic-cleared-disclosure-scope"),
    }
    reference, payload = _test_approval_reference(
        RunPlanApproval.scope_digest(
            **reserved_scope,
        ),
        subject_id=RUN_PLAN_APPROVAL_SUBJECT,
    )
    declaration_reference, declaration_payload = (
        RunPlanApproval.build_engineering_declaration_artifact(**engineering_declaration)
    )
    artifacts = {
        reference.relative_path: payload,
        declaration_reference.relative_path: declaration_payload,
    }
    return RunPlanApproval.load(
        **engineering_declaration,
        **reserved_scope,
        engineering_declaration_reference=declaration_reference,
        approval_reference=reference,
        read_bytes=artifacts.__getitem__,
    )


def cleared_public_authorization_for(
    *,
    manifest,
    roster,
    witness_configuration: WitnessConfiguration,
    prompt_registry,
    profile,
    sample_accounting: PrivateSampleAccounting | None = None,
) -> RunAuthorization:
    """A test-only shape for the non-synthetic declared-run authorization boundary."""

    return RunAuthorization(
        material_class=MaterialClass.CLEARED_PUBLIC,
        run_plan_approval=run_plan_approval_for(
            manifest=manifest,
            roster=roster,
            witness_configuration=witness_configuration,
            prompt_registry=prompt_registry,
            profile=profile,
            sample_accounting=sample_accounting,
        ),
    )
