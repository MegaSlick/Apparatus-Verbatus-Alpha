"""Synthetic negative tests for the real-material and vendor delivery boundary."""

from __future__ import annotations

import json

import pytest

import operations.spike_perlector.gates as gates_module
from common.contracts.approval import (
    ApprovalRecordReference,
    build_approval_record,
)
from operations.spike_perlector.errors import DisclosureRefusal, MatrixRefusal
from operations.spike_perlector.fakes import FakeCandidate
from operations.spike_perlector.gates import (
    DATA_GATE_APPROVAL_SUBJECT,
    DATA_GATE_POLICY_IDENTITY,
    DATA_GATE_POLICY_REPOSITORY_PATH,
    THIRD_PARTY_APPROVAL_SUBJECT,
    DataGateAuthority,
    RunAuthorization,
    RunPlanApproval,
    ThirdPartyTransmissionApproval,
    _approval_digest,
)
from operations.spike_perlector.models import DeliveryMode, MaterialClass
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.roster import STOCK_BASE_SOURCE, CandidateRoster
from operations.spike_perlector.runner import run_declared_roster_matrix, run_matrix
from operations.spike_perlector.testkit import (
    cleared_public_authorization_for,
    digest,
    evaluation_act,
    identity,
    manifest_for,
    registry,
    run_plan_approval_for,
    witness_configuration_for,
)
from operations.submit.gate import DEFAULT_POLICY_PATH, load_policy

POLICY = load_policy()
RUN_ENGINEERING_DECLARATION = {
    "protocol_sha256": digest("protocol"),
    "manifest_sha256": digest("manifest"),
    "candidate_roster_sha256": digest("roster"),
    "witness_configuration_sha256": digest("witnesses"),
    "prompt_registry_sha256": digest("prompts"),
    "normalization_profile_id": "graphemic-v1",
    "normalization_profile_sha256": digest("normalization"),
    "private_sample_accounting_sha256": digest("accounting"),
}


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
        action="other", target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY)
    )
    return DataGateAuthority.load(
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )


def approval_reference_for(
    *,
    action: str,
    target_version_hash: str,
    subject_ids: list[str] | None = None,
):
    record = build_approval_record(
        subject_ids=subject_ids or ["spec05-data-gate-approval.v1"],
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
        subject_ids=["spec05-third-party-transmission-approval.v1"],
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
        subject_ids=["spec05-third-party-transmission-approval.v1"],
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
        subject_ids=["spec05-third-party-transmission-approval.v1"],
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


def test_cleared_public_external_candidate_delivers_without_vendor_transmission_approval():
    """Spec 03/05's named carve-out: cleared-public material needs a run-plan and
    reserved-scope run approval but never a ThirdPartyTransmissionApproval, even for
    an external candidate -- unlike private-register material, which always
    does (asserted here alongside it, not left to an incidentally-passing test,
    per audit-d finding F7)."""

    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.CLEARED_PUBLIC)
    manifest = manifest_for(act)
    candidates = tuple(FakeCandidate(item) for item in roster.identities())
    witnesses = witness_configuration_for(act)
    prompts = registry(*roster.identities())
    authorization = cleared_public_authorization_for(
        manifest=manifest,
        roster=roster,
        witness_configuration=witnesses,
        prompt_registry=prompts,
        profile=GRAPHEMIC_V1,
    )
    assert not authorization.external_approvals
    run = run_declared_roster_matrix(
        candidates,
        (act,),
        roster=roster,
        witness_configuration=witnesses,
        manifest=manifest,
        prompt_registry=prompts,
        profile=GRAPHEMIC_V1,
        authorization=authorization,
    )
    assert len(run.cells) == 9
    vendor_candidate = next(
        candidate
        for candidate in candidates
        if candidate.identity.delivery is DeliveryMode.EXTERNAL
    )
    assert vendor_candidate.requests

    with pytest.raises(DisclosureRefusal, match="no vendor-and-pages"):
        declared_private_run()


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
        RunAuthorization(material_class=MaterialClass.CLEARED_PUBLIC)


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
        )
    with pytest.raises(DisclosureRefusal, match="RunPlanApproval"):
        RunAuthorization(
            material_class=MaterialClass.PRIVATE_REGISTER,
            data_gate_authority=checked_data_gate_authority(),
            run_plan_approval=forged,
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


# --- DataGateAuthority's own refusals -----------------------------------------
#
# This class decides whether private-register material may be disclosed to an
# external adapter.  Until now its stale/missing/tampered behaviour was covered
# only through `common/contracts/test_contracts_approval.py`, against the shared
# "data-gate" action Tyrel retired on 2026-08-09.  When the rebase onto that cut
# moved the behaviour into this class, the coverage did not come with it — and two
# real regressions then passed the whole suite unnoticed.  These tests are the
# protection moving to where the behaviour now lives.


def test_a_missing_data_gate_approval_refuses_by_name_not_by_attribute_error():
    """The refusal must name the governed condition, not leak a Python attribute."""

    with pytest.raises(DisclosureRefusal, match="data-gate approval is missing"):
        DataGateAuthority.load(
            approval_reference=None,
            read_bytes=lambda _path: b"",
        )


def test_a_superseded_policy_with_its_valid_stale_approval_is_refused():
    superseded = {**POLICY, "policy_version": "synthetic-data-gate-v0"}
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=superseded),
    )
    with pytest.raises(DisclosureRefusal, match="stale"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


def test_data_gate_resolves_the_single_repository_policy_identity_and_revision():
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY),
    )
    authority = DataGateAuthority.load(
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )

    assert DEFAULT_POLICY_PATH.as_posix().endswith(DATA_GATE_POLICY_REPOSITORY_PATH)
    assert authority.policy_identity == DATA_GATE_POLICY_IDENTITY
    assert authority.policy_repository_path == DATA_GATE_POLICY_REPOSITORY_PATH
    assert authority.policy_revision == POLICY["policy_version"]


def test_approval_bytes_that_do_not_match_their_content_address_refuse():
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY),
    )
    with pytest.raises(DisclosureRefusal, match="do not match their content-addressed reference"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload + b" ",
        )


def test_rehashed_approval_bytes_still_refuse_an_invalid_record_self_hash():
    _reference, payload = approval_reference_for(
        action="other",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY),
    )
    record = json.loads(payload)
    record["reason"] = "edited after Tyrel's recorded act"
    tampered = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tampered_sha256 = digest(tampered.decode("utf-8"))
    reference = ApprovalRecordReference(f"receipts/sha256/{tampered_sha256}.json", tampered_sha256)

    with pytest.raises(DisclosureRefusal, match="fails its own self-hash"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: tampered,
        )


def test_a_record_whose_action_is_not_the_recorded_one_refuses():
    """'exclusion' is a real action in the shared taxonomy — and not this one.

    The retired 'data-gate' spelling cannot be used here at all: `build_approval_record`
    now refuses it, which is itself the cut this branch had to absorb.
    """

    reference, payload = approval_reference_for(
        action="exclusion",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY),
    )
    with pytest.raises(DisclosureRefusal, match="recorded approval action"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


@pytest.mark.parametrize(
    "sibling",
    (
        ThirdPartyTransmissionApproval.scope_digest(
            vendor="synthetic-vendor",
            candidate_artifact_digest=digest("candidate"),
            page_ids=frozenset({"p1"}),
            manifest_sha256=digest("manifest"),
        ),
        RunPlanApproval.scope_digest(
            prove_before_scale_evidence_sha256=digest("scale"),
            spend_scope_sha256=digest("spend"),
            disclosure_scope_sha256=digest("disclosure"),
        ),
    ),
)
def test_a_sibling_scope_digest_cannot_satisfy_the_data_gate(sibling):
    """A sibling scope's digest cannot satisfy this gate.

    The docstring used to say the schema tag is what separates the four scopes
    and that this proves it. It does not: each scope record carries different
    field names, so the digests differ whether or not the tag is there, and the
    assertion below passes with every `"schema"` key deleted. The tag is pinned
    separately by the test underneath. The refusal exercised here is real; only
    the stated reason was wrong.
    """

    assert sibling != DataGateAuthority.scope_digest(policy_content=POLICY)
    reference, payload = approval_reference_for(action="other", target_version_hash=sibling)
    with pytest.raises(DisclosureRefusal, match="stale"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


def test_the_schema_tag_alone_separates_two_otherwise_identical_scopes():
    """Pins the claim the test above used to make and could not support.

    Two scope records identical but for their `"schema"` value must digest
    differently, or the tag is decoration and two scopes with the same fields
    would alias.
    """

    shared = {"policy_content": dict(POLICY)}
    first = _approval_digest({"schema": DATA_GATE_APPROVAL_SUBJECT, **shared})
    second = _approval_digest({"schema": THIRD_PARTY_APPROVAL_SUBJECT, **shared})
    assert first != second


def test_policy_content_that_cannot_be_canonicalized_refuses_rather_than_aliasing():
    """An integer key and its string spelling must not digest to the same scope.

    Under a permissive JSON encoder they do, because `json.dumps` converts the key
    silently — so a policy could change underneath a live approval without ever
    becoming stale.  The retired `data_gate_policy_hash` refused both this and any
    float; the scope digest refuses them again.
    """

    with pytest.raises(DisclosureRefusal, match="cannot be canonically bound.*non-string key"):
        DataGateAuthority.scope_digest(policy_content={"policy_version": "test-v1", 1: "same-json"})
    with pytest.raises(DisclosureRefusal, match="cannot be canonically bound.*float"):
        DataGateAuthority.scope_digest(
            policy_content={"policy_version": "test-v1", "retention_days": 1.5}
        )
    with pytest.raises(
        DisclosureRefusal, match="cannot be canonically bound.*surrogates not allowed"
    ):
        DataGateAuthority.scope_digest(
            policy_content={"policy_version": "test-v1", "purpose": chr(0xD800)}
        )


def test_non_canonical_policy_content_refuses_as_a_disclosure_not_a_type_error(monkeypatch):
    """The gate's refusal must be the governed one, not a bare `TypeError`.

    Strict canonicalization refuses a float or a non-string key by raising
    `TypeError`, and the construction that triggers it sat outside the `try` that
    converts failures into `DisclosureRefusal` — so a caller catching
    `DisclosureRefusal` in order to hold would not have caught this at all. The
    refusal was restored when the digest was made strict; the *governed* refusal
    was not. Found by the Opus read of this branch.
    """

    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY),
    )
    monkeypatch.setattr(
        gates_module,
        "_resolve_authoritative_data_gate_policy",
        lambda: {"policy_version": "test-v1", "retention_days": 1.5},
    )
    with pytest.raises(DisclosureRefusal, match="cannot be canonically bound"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )
    monkeypatch.setattr(
        gates_module,
        "_resolve_authoritative_data_gate_policy",
        lambda: {
            "policy_version": "test-v1",
            "outer": [{"retention_days": 1.5}],
        },
    )
    with pytest.raises(
        DisclosureRefusal,
        match=r"cannot be canonically bound: float at \$\.outer\[0\]\.retention_days",
    ):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


def test_every_approval_loader_names_a_missing_approval_rather_than_an_attribute():
    """The fix reached one loader first; the remaining loaders said `'NoneType' object
    has no attribute 'relative_path'` — verbatim the diagnostic the commit that
    made it identified as wrong, in the same file. Lower stakes than the data
    gate, since these do not gate private-register disclosure, but the reasoning
    is unchanged. Found by the Opus read of this branch."""

    with pytest.raises(DisclosureRefusal, match="third-party transmission approval is missing"):
        ThirdPartyTransmissionApproval.load(
            vendor="synthetic-vendor",
            candidate_artifact_digest=digest("candidate"),
            page_ids=frozenset({"p1"}),
            manifest_sha256=digest("manifest"),
            approval_reference=None,
            read_bytes=lambda _path: b"",
        )
    with pytest.raises(DisclosureRefusal, match="run-plan approval is missing"):
        RunPlanApproval.load(
            **RUN_ENGINEERING_DECLARATION,
            prove_before_scale_evidence_sha256=digest("scale"),
            spend_scope_sha256=digest("spend"),
            disclosure_scope_sha256=digest("disclosure"),
            approval_reference=None,
            read_bytes=lambda _path: b"",
        )


def test_data_gate_authority_deeply_owns_the_policy_bound_by_approval(monkeypatch):
    policy = {"policy_version": "test-v1", "limits": {"retention_days": [30]}}
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=DataGateAuthority.scope_digest(policy_content=policy),
    )
    monkeypatch.setattr(
        gates_module,
        "_resolve_authoritative_data_gate_policy",
        lambda: policy,
    )
    authority = DataGateAuthority.load(
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )
    approved_scope = authority.scope_sha256

    policy["limits"]["retention_days"][0] = 3650

    assert authority.policy_content["limits"]["retention_days"] == (30,)
    assert authority.scope_sha256 == approved_scope
    with pytest.raises(TypeError, match="does not support item assignment"):
        authority.policy_content["limits"]["retention_days"] = (3650,)


def test_every_typed_approval_retains_an_immutable_checked_record():
    roster = private_roster()
    act = evaluation_act(material_class=MaterialClass.PRIVATE_REGISTER)
    manifest = manifest_for(act)
    witnesses = witness_configuration_for(act)
    prompts = registry(*roster.identities())
    approvals = (
        checked_data_gate_authority(),
        transmission_approval(
            vendor="synthetic-vendor",
            candidate_artifact_digest=roster.vendor_unaltered.artifact_digest,
            # The page identifier, as every other `transmission_approval` call
            # in this file uses. `page_ids` is a page-scoped field and an act ID
            # is not a page ID.
            page_ids=frozenset({act.image.opaque_page_id}),
            manifest_sha256=manifest.manifest_sha256,
        ),
        run_plan_approval_for(
            manifest=manifest,
            roster=roster,
            witness_configuration=witnesses,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
        ),
    )
    for approval in approvals:
        with pytest.raises(AttributeError, match="'tuple' object has no attribute 'append'"):
            approval.approval_record["subject_ids"].append("mutated-after-check")


def test_run_plan_keeps_engineering_declaration_out_of_tyrels_approval_scope():
    reserved = {
        "prove_before_scale_evidence_sha256": digest("scale"),
        "spend_scope_sha256": digest("spend"),
        "disclosure_scope_sha256": digest("disclosure"),
    }
    reference, payload = approval_reference_for(
        action="other",
        subject_ids=["spec05-run-plan-approval.v1"],
        target_version_hash=RunPlanApproval.scope_digest(**reserved),
    )
    first = RunPlanApproval.load(
        **RUN_ENGINEERING_DECLARATION,
        **reserved,
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )
    second = RunPlanApproval.load(
        **{**RUN_ENGINEERING_DECLARATION, "prompt_registry_sha256": digest("other-prompts")},
        **reserved,
        approval_reference=reference,
        read_bytes=lambda _path: payload,
    )

    assert first.scope_sha256 == second.scope_sha256
    assert first.engineering_declaration_sha256 != second.engineering_declaration_sha256


def test_generic_other_approval_for_another_purpose_cannot_open_the_data_gate():
    reference, payload = approval_reference_for(
        action="other",
        subject_ids=["documentation-change"],
        target_version_hash=DataGateAuthority.scope_digest(policy_content=POLICY),
    )

    with pytest.raises(DisclosureRefusal, match="data-gate approval purpose"):
        DataGateAuthority.load(
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


def test_each_typed_approval_refuses_a_generic_other_record_for_another_purpose():
    transmission = {
        "vendor": "synthetic-vendor",
        "candidate_artifact_digest": digest("candidate"),
        "page_ids": frozenset({"p1"}),
        "manifest_sha256": digest("manifest"),
    }
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=ThirdPartyTransmissionApproval.scope_digest(**transmission),
    )
    with pytest.raises(DisclosureRefusal, match="third-party approval purpose"):
        ThirdPartyTransmissionApproval.load(
            **transmission,
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )

    run_plan = {
        **RUN_ENGINEERING_DECLARATION,
        "prove_before_scale_evidence_sha256": digest("scale"),
        "spend_scope_sha256": digest("spend"),
        "disclosure_scope_sha256": digest("disclosure"),
    }
    reference, payload = approval_reference_for(
        action="other",
        target_version_hash=RunPlanApproval.scope_digest(
            prove_before_scale_evidence_sha256=run_plan["prove_before_scale_evidence_sha256"],
            spend_scope_sha256=run_plan["spend_scope_sha256"],
            disclosure_scope_sha256=run_plan["disclosure_scope_sha256"],
        ),
    )
    with pytest.raises(DisclosureRefusal, match="run-plan approval purpose"):
        RunPlanApproval.load(
            **run_plan,
            approval_reference=reference,
            read_bytes=lambda _path: payload,
        )


def test_a_policy_too_deep_to_canonicalize_refuses_in_the_gate_vocabulary():
    policy = {"policy_version": "test-v1"}
    cursor = policy
    for _ in range(1_200):
        child = {}
        cursor["nested"] = child
        cursor = child

    with pytest.raises(DisclosureRefusal, match="cannot be canonically bound"):
        DataGateAuthority.scope_digest(policy_content=policy)
