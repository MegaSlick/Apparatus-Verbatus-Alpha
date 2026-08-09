"""Synthetic negative tests for the real-material and vendor delivery boundary."""

from __future__ import annotations

import json

import pytest

from common.contracts.approval import (
    ApprovalRecordReference,
    build_approval_record,
    data_gate_policy_hash,
)
from operations.spike_perlector.errors import DisclosureRefusal, MatrixRefusal
from operations.spike_perlector.fakes import FakeCandidate
from operations.spike_perlector.gates import (
    DataGateAuthority,
    RunAuthorization,
    ThirdPartyTransmissionApproval,
)
from operations.spike_perlector.models import DeliveryMode, MaterialClass
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.roster import STOCK_BASE_SOURCE, CandidateRoster
from operations.spike_perlector.runner import run_declared_roster_matrix, run_matrix
from operations.spike_perlector.testkit import (
    digest,
    evaluation_act,
    identity,
    manifest_for,
    normalization_approval_for,
    registry,
    run_plan_approval_for,
    witness_configuration_for,
)

POLICY = {"purpose": "synthetic test-only data-gate policy"}


def private_roster() -> CandidateRoster:
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
        vendor_unaltered_evidence_sha256=digest("vendor-evidence"),
        checkpoint_repository_evidence_sha256=digest("checkpoint-evidence"),
    )


def checked_data_gate_authority() -> DataGateAuthority:
    reference, payload = approval_reference_for(
        action="data-gate", target_version_hash=data_gate_policy_hash(POLICY)
    )
    return DataGateAuthority.load(
        policy_content=POLICY,
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )


def approval_reference_for(*, action: str, target_version_hash: str):
    record = build_approval_record(
        subject_ids=["synthetic-spec05-gate-test"],
        action=action,
        reason="synthetic test fixture; no register material",
        target_version_hash=target_version_hash,
        timestamp="2026-08-08T00:00:00Z",
    )
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_sha256 = digest(payload.decode("utf-8"))
    reference = ApprovalRecordReference(f"receipts/sha256/{record_sha256}.json", record_sha256)
    return reference, payload


def transmission_approval(
    *, vendor: str, candidate_artifact_digest: str, page_ids: frozenset[str], manifest_sha256: str
) -> ThirdPartyTransmissionApproval:
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=ThirdPartyTransmissionApproval.scope_digest(
            vendor=vendor,
            candidate_artifact_digest=candidate_artifact_digest,
            page_ids=page_ids,
            manifest_sha256=manifest_sha256,
        ),
    )
    return ThirdPartyTransmissionApproval.load(
        vendor=vendor,
        candidate_artifact_digest=candidate_artifact_digest,
        page_ids=page_ids,
        manifest_sha256=manifest_sha256,
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )


def declared_private_run(*, approval: ThirdPartyTransmissionApproval | None = None):
    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
    manifest = manifest_for(act)
    candidates = tuple(FakeCandidate(item) for item in roster.identities())
    witnesses = witness_configuration_for(act)
    prompts = registry(*roster.identities())
    authorization = _private_authorization(roster, manifest, witnesses, prompts, approval)
    return (
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=authorization,
        ),
        candidates,
        act,
        manifest,
        roster,
    )


def _private_authorization(
    roster, manifest, witnesses, prompts, approval: ThirdPartyTransmissionApproval | None = None
) -> RunAuthorization:
    return RunAuthorization(
        material_class=MaterialClass.PRIVATE_REGISTER,
        data_gate_authority=checked_data_gate_authority(),
        run_plan_approval=run_plan_approval_for(
            manifest=manifest,
            roster=roster,
            witness_configuration=witnesses,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
        ),
        normalization_approval=normalization_approval_for(GRAPHEMIC_V1),
        external_approvals={roster.vendor_unaltered.candidate_key: approval}
        if approval is not None
        else None,
    )


def test_private_external_candidate_without_vendor_page_approval_is_refused_before_call():
    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
    manifest = manifest_for(act)
    candidates = tuple(FakeCandidate(item) for item in roster.identities())
    witnesses = witness_configuration_for(act)
    prompts = registry(*roster.identities())
    with pytest.raises(DisclosureRefusal, match="no vendor-and-pages"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_private_authorization(roster, manifest, witnesses, prompts),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_wrong_vendor_or_pages_cannot_authorize_private_external_candidate():
    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
    manifest = manifest_for(act)
    wrong = transmission_approval(
        vendor="other-vendor",
        candidate_artifact_digest=roster.vendor_unaltered.artifact_digest,
        page_ids=frozenset({act.image.opaque_page_id}),
        manifest_sha256=manifest.manifest_sha256,
    )
    candidates = tuple(FakeCandidate(item) for item in roster.identities())
    witnesses = witness_configuration_for(act)
    prompts = registry(*roster.identities())
    with pytest.raises(DisclosureRefusal, match="different vendor"):
        run_declared_roster_matrix(
            candidates,
            (act,),
            roster=roster,
            witness_configuration=witnesses,
            manifest=manifest,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_private_authorization(roster, manifest, witnesses, prompts, wrong),
        )
    assert all(candidate.requests == [] for candidate in candidates)


def test_tampered_vendor_approval_record_is_refused_at_construction():
    vendor = "synthetic-vendor"
    artifact = digest("artifact")
    pages = frozenset({"synthetic-page"})
    manifest_sha256 = digest("manifest")
    _reference, payload = approval_reference_for(
        action="other",
        target_version_hash=ThirdPartyTransmissionApproval.scope_digest(
            vendor=vendor,
            candidate_artifact_digest=artifact,
            page_ids=pages,
            manifest_sha256=manifest_sha256,
        ),
    )
    forged_reference = ApprovalRecordReference(f"receipts/sha256/{'0' * 64}.json", "0" * 64)
    with pytest.raises(DisclosureRefusal, match="do not match"):
        ThirdPartyTransmissionApproval.load(
            vendor=vendor,
            candidate_artifact_digest=artifact,
            page_ids=pages,
            manifest_sha256=manifest_sha256,
            approval_reference=forged_reference,
            read_bytes=lambda _path: payload,
        )


def test_vendor_approval_cannot_be_rebound_to_a_different_page_scope():
    vendor = "synthetic-vendor"
    artifact = digest("artifact")
    approved_pages = frozenset({"approved-page"})
    attempted_pages = frozenset({"different-page"})
    manifest_sha256 = digest("manifest")
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=ThirdPartyTransmissionApproval.scope_digest(
            vendor=vendor,
            candidate_artifact_digest=artifact,
            page_ids=approved_pages,
            manifest_sha256=manifest_sha256,
        ),
    )
    with pytest.raises(DisclosureRefusal, match="does not bind this exact"):
        ThirdPartyTransmissionApproval.load(
            vendor=vendor,
            candidate_artifact_digest=artifact,
            page_ids=attempted_pages,
            manifest_sha256=manifest_sha256,
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


def test_shaped_private_approval_exercises_gate_without_a_network_call():
    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
    manifest = manifest_for(act)
    approval = transmission_approval(
        vendor="synthetic-vendor",
        candidate_artifact_digest=roster.vendor_unaltered.artifact_digest,
        page_ids=frozenset({act.image.opaque_page_id}),
        manifest_sha256=manifest.manifest_sha256,
    )
    run, candidates, *_ = declared_private_run(approval=approval)
    assert len(run.cells) == 9
    assert len(candidates[1].requests) == 3


def test_synthetic_fixture_cannot_deliver_to_an_external_looking_adapter():
    resolved = identity(
        "external-looking-fake",
        1,
        delivery=DeliveryMode.EXTERNAL,
        provider="synthetic-vendor",
    )
    candidate = FakeCandidate(resolved)
    with pytest.raises(DisclosureRefusal, match="cannot deliver.*external"):
        run_matrix(
            (candidate,),
            (evaluation_act(),),
            prompt_registry=registry(resolved),
            profile=GRAPHEMIC_V1,
            authorization=RunAuthorization.synthetic_fixture(),
        )
    assert candidate.requests == []


def test_non_synthetic_authorization_requires_a_checked_run_plan_approval():
    with pytest.raises(DisclosureRefusal, match="run-plan"):
        RunAuthorization(
            material_class=MaterialClass.CLEARED_PUBLIC,
            normalization_approval=normalization_approval_for(GRAPHEMIC_V1),
        )


def test_run_authorization_refuses_a_forged_in_memory_approval_object():
    """A SHA-shaped string or an in-memory approver field is not authority (README section 1).

    Each approval field must have actually been built through its own checked
    ``.load()`` classmethod; a duck-typed stand-in with the right method names but
    none of the underlying content-addressed verification must not be accepted in
    its place.
    """

    class _Forged:
        def require_scope(self, **_kwargs):
            return None

        def require_profile(self, _profile):
            return None

    forged = _Forged()
    roster = private_roster()
    manifest = manifest_for(evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER))
    with pytest.raises(DisclosureRefusal, match="DataGateAuthority"):
        RunAuthorization(
            material_class=MaterialClass.PRIVATE_REGISTER,
            data_gate_authority=forged,
            run_plan_approval=run_plan_approval_for(
                manifest=manifest,
                roster=roster,
                witness_configuration=witness_configuration_for(
                    evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
                ),
                prompt_registry=registry(*roster.identities()),
                profile=GRAPHEMIC_V1,
            ),
            normalization_approval=normalization_approval_for(GRAPHEMIC_V1),
        )
    with pytest.raises(DisclosureRefusal, match="RunPlanApproval"):
        RunAuthorization(
            material_class=MaterialClass.PRIVATE_REGISTER,
            data_gate_authority=checked_data_gate_authority(),
            run_plan_approval=forged,
            normalization_approval=normalization_approval_for(GRAPHEMIC_V1),
        )
    with pytest.raises(DisclosureRefusal, match="NormalizationApproval"):
        RunAuthorization(
            material_class=MaterialClass.PRIVATE_REGISTER,
            data_gate_authority=checked_data_gate_authority(),
            run_plan_approval=run_plan_approval_for(
                manifest=manifest,
                roster=roster,
                witness_configuration=witness_configuration_for(
                    evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
                ),
                prompt_registry=registry(*roster.identities()),
                profile=GRAPHEMIC_V1,
            ),
            normalization_approval=forged,
        )
    with pytest.raises(DisclosureRefusal, match="ThirdPartyTransmissionApproval"):
        RunAuthorization(
            material_class=MaterialClass.PRIVATE_REGISTER,
            data_gate_authority=checked_data_gate_authority(),
            run_plan_approval=run_plan_approval_for(
                manifest=manifest,
                roster=roster,
                witness_configuration=witness_configuration_for(
                    evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
                ),
                prompt_registry=registry(*roster.identities()),
                profile=GRAPHEMIC_V1,
            ),
            normalization_approval=normalization_approval_for(GRAPHEMIC_V1),
            external_approvals={roster.vendor_unaltered.candidate_key: forged},
        )


def test_generic_matrix_refuses_real_material_before_any_adapter_call():
    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
    manifest = manifest_for(act)
    witnesses = witness_configuration_for(act)
    prompts = registry(*roster.identities())
    candidate = FakeCandidate(roster.stock_base)
    with pytest.raises(MatrixRefusal, match="declared-roster"):
        run_matrix(
            (candidate,),
            (act,),
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
            authorization=_private_authorization(roster, manifest, witnesses, prompts),
        )
    assert candidate.requests == []
