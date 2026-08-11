"""Approval and material-provenance preflight checks for a real Spec 05 run.

This package contains no transport client.  Its boundary is still deliberately
strict: a private-register act is bound to a sealed manifest, a current
content-addressed data-gate approval, a selected normalization profile, and (for
an external candidate) a vendor-and-pages approval before an adapter is called.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable

from common.contracts.approval import (
    ApprovalRecordReference,
    validate_approval_record,
)
from common.contracts.canonical import digest_of

from .encoding import is_sha256, sha256_bytes
from .errors import DisclosureRefusal
from .holdout import EvaluationManifest
from .models import DeliveryMode, EvaluationAct, MaterialClass, ResolvedIdentity
from .normalization import NormalizationProfile

DATA_GATE_APPROVAL_SUBJECT = "spec05-data-gate-approval.v1"
NORMALIZATION_APPROVAL_SUBJECT = "spec05-normalization-approval.v1"
THIRD_PARTY_APPROVAL_SUBJECT = "spec05-third-party-transmission-approval.v1"
RUN_PLAN_APPROVAL_SUBJECT = "spec05-run-plan-approval.v1"


def _deep_freeze(value: Any) -> Any:
    """Detach and recursively freeze JSON-shaped approval evidence."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _require_approval_subject(record: Mapping[str, Any], *, expected: str, label: str) -> None:
    """Make a generic ``other`` approval's typed purpose visible and exact."""

    if record["subject_ids"] != [expected]:
        raise DisclosureRefusal(
            f"{label} record does not name the exact {label} purpose {expected!r}"
        )


def _approval_digest(record: Mapping[str, object]) -> str:
    """Digest one approval scope under the strict repository canonicalization.

    Not this package's own ``canonical_json_bytes``, which is the byte-stable envelope
    for a candidate dossier and is deliberately permissive: it hands the value to
    ``json.dumps``, which silently converts an integer mapping key to its string
    spelling and accepts any finite float.  That is harmless for a dossier and wrong
    for an approval, because an approval scope is a *claim about exactly what was
    approved*.  Under the permissive encoder ``{1: x}`` and ``{"1": x}`` digest
    identically, so a policy could change underneath a live approval without making it
    stale — and the retired ``data_gate_policy_hash`` these scopes replaced refused
    both cases outright.

    ``digest_of`` refuses floats and non-string keys recursively, which restores that
    guarantee.  The three sibling scopes carry only strings and pinned digests, so this
    is a no-op for them; ``DataGateAuthority`` is the one that embeds caller-supplied
    policy content, and the one this protects.
    """

    return digest_of(dict(record))


def _checked_approval_record(reference: ApprovalRecordReference, payload: bytes) -> dict[str, Any]:
    """Verify one generic Tyrel approval artifact from its exact stored bytes."""

    if not isinstance(reference, ApprovalRecordReference):
        raise DisclosureRefusal("approval authority requires a checked approval reference")
    reference_digest = reference.sha256
    if (
        not is_sha256(reference_digest)
        or reference.relative_path != f"receipts/sha256/{reference_digest}.json"
    ):
        raise DisclosureRefusal("approval authority has an invalid content-addressed reference")
    if not isinstance(payload, bytes) or sha256_bytes(payload) != reference_digest:
        raise DisclosureRefusal("approval bytes do not match their content-addressed reference")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise DisclosureRefusal("approval bytes are not JSON") from error
    try:
        return validate_approval_record(decoded)
    except Exception as error:
        raise DisclosureRefusal(f"approval record is invalid: {error}") from error


@dataclass(frozen=True, slots=True)
class DataGateAuthority:
    """A data-gate record that has been checked through its approval reference.

    Binds its own content-addressed scope, the same way ``NormalizationApproval``,
    ``ThirdPartyTransmissionApproval`` and ``RunPlanApproval`` below do: the shared
    approval-record contract's own ``data-gate`` action existed for a different
    question (whether real images ever reach git) and was retired for it (Tyrel,
    2026-08-09); this module's question — whether private-register material may be
    disclosed under a checked, current, content-addressed policy — is unrelated and
    still stands, now carried entirely by this class rather than by a shared action.

    The immutable object retains the exact checked record rather than a
    caller-provided digest string.
    """

    policy_content: Mapping[str, Any]
    approval_reference: ApprovalRecordReference
    approval_record: Mapping[str, Any]
    approval_bytes: bytes
    _bound_scope_sha256: str = field(init=False, repr=False)

    # One builder, for the reason given on NormalizationApproval._scope_record.
    @staticmethod
    def _scope_record(*, policy_content: Mapping[str, Any]) -> dict[str, object]:
        return {
            "schema": DATA_GATE_APPROVAL_SUBJECT,
            "policy_content": dict(policy_content),
        }

    @property
    def scope_sha256(self) -> str:
        return self._bound_scope_sha256

    @classmethod
    def scope_digest(cls, *, policy_content: Mapping[str, Any]) -> str:
        try:
            return _approval_digest(cls._scope_record(policy_content=policy_content))
        except (TypeError, ValueError, RecursionError) as error:
            raise DisclosureRefusal(
                f"data-gate policy content cannot be canonically bound: {error}"
            ) from error

    def __post_init__(self) -> None:
        checked = _checked_approval_record(self.approval_reference, self.approval_bytes)
        if checked != dict(self.approval_record):
            raise DisclosureRefusal(
                "data-gate authority record differs from its checked approval bytes"
            )
        if checked["action"] != "other":
            raise DisclosureRefusal(
                "data-gate authority record must use the recorded approval action"
            )
        _require_approval_subject(
            checked, expected=DATA_GATE_APPROVAL_SUBJECT, label="data-gate approval"
        )
        scope_sha256 = self.scope_digest(policy_content=self.policy_content)
        if checked["target_version_hash"] != scope_sha256:
            raise DisclosureRefusal("data-gate approval is stale for the supplied policy")
        try:
            policy = _deep_freeze(dict(self.policy_content))
            record = _deep_freeze(checked)
        except RecursionError as error:
            raise DisclosureRefusal(
                f"data-gate policy content cannot be canonically bound: {error}"
            ) from error
        object.__setattr__(self, "policy_content", policy)
        object.__setattr__(self, "approval_record", record)
        object.__setattr__(self, "_bound_scope_sha256", scope_sha256)

    @classmethod
    def load(
        cls,
        *,
        policy_content: Mapping[str, Any],
        approval_reference: ApprovalRecordReference,
        read_bytes: Callable[[str], bytes],
    ) -> "DataGateAuthority":
        """Load a current data-gate record through the approved-root reader."""

        # Checked before it is dereferenced, so a missing approval refuses by the
        # governed condition that actually failed.  Reading `.relative_path` first
        # turned the missing case into an AttributeError wearing a refusal's clothes:
        # it still failed closed, but it reported a Python attribute rather than the
        # absence of Tyrel's approval, which is the one fact a reader needs here.
        if not isinstance(approval_reference, ApprovalRecordReference):
            raise DisclosureRefusal(
                "data-gate approval is missing; real input requires a current "
                "approval-record artifact"
            )

        try:
            payload = read_bytes(approval_reference.relative_path)
            record = _checked_approval_record(approval_reference, payload)
        except Exception as error:
            raise DisclosureRefusal(
                f"current data-gate approval is unavailable: {error}"
            ) from error
        return cls(
            policy_content=policy_content,
            approval_reference=approval_reference,
            approval_record=record,
            approval_bytes=payload,
        )


@dataclass(frozen=True, slots=True)
class NormalizationApproval:
    """Tyrel's recorded pre-run selection of one closed normalization profile."""

    profile_id: str
    profile_sha256: str
    approval_reference: ApprovalRecordReference
    approval_record: Mapping[str, Any]
    approval_bytes: bytes

    # The one builder for the sealed scope; scope_sha256 and scope_digest both
    # call it, so what an approval binds cannot drift between a live path and
    # an unused copy.
    @staticmethod
    def _scope_record(*, profile_id: str, profile_sha256: str) -> dict[str, str]:
        return {
            "schema": NORMALIZATION_APPROVAL_SUBJECT,
            "profile_id": profile_id,
            "profile_sha256": profile_sha256,
        }

    @property
    def scope_sha256(self) -> str:
        return self.scope_digest(profile_id=self.profile_id, profile_sha256=self.profile_sha256)

    @classmethod
    def scope_digest(cls, *, profile_id: str, profile_sha256: str) -> str:
        return _approval_digest(
            cls._scope_record(profile_id=profile_id, profile_sha256=profile_sha256)
        )

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise DisclosureRefusal("normalization approval must name a profile")
        if not is_sha256(self.profile_sha256):
            raise DisclosureRefusal("normalization approval requires a profile SHA-256")
        record = MappingProxyType(dict(self.approval_record))
        object.__setattr__(self, "approval_record", record)
        checked = _checked_approval_record(self.approval_reference, self.approval_bytes)
        if checked != dict(record):
            raise DisclosureRefusal("normalization approval record differs from its checked bytes")
        if checked["action"] != "other":
            raise DisclosureRefusal(
                "normalization approval record must use the recorded approval action"
            )
        _require_approval_subject(
            checked, expected=NORMALIZATION_APPROVAL_SUBJECT, label="normalization approval"
        )
        if checked["target_version_hash"] != self.scope_sha256:
            raise DisclosureRefusal(
                "normalization approval record does not bind this exact profile"
            )

    @classmethod
    def load(
        cls,
        *,
        profile_id: str,
        profile_sha256: str,
        approval_reference: ApprovalRecordReference,
        read_bytes: Callable[[str], bytes],
    ) -> "NormalizationApproval":
        """Load a profile selection through an immutable Tyrel approval artifact."""

        # Checked before it is dereferenced, for the reason `DataGateAuthority.load`
        # gives: reading `.relative_path` off a missing reference reports a Python
        # attribute where the governed condition is the absence of Tyrel's approval.
        # That fix reached one of four loaders; this is another. Found by the Opus
        # read of this branch.
        if not isinstance(approval_reference, ApprovalRecordReference):
            raise DisclosureRefusal(
                "normalization approval is missing; this run requires a current "
                "approval-record artifact"
            )

        try:
            payload = read_bytes(approval_reference.relative_path)
            record = _checked_approval_record(approval_reference, payload)
        except Exception as error:
            raise DisclosureRefusal(f"normalization approval is unavailable: {error}") from error
        return cls(
            profile_id=profile_id,
            profile_sha256=profile_sha256,
            approval_reference=approval_reference,
            approval_record=record,
            approval_bytes=payload,
        )

    def require_profile(self, profile: NormalizationProfile) -> None:
        if (self.profile_id, self.profile_sha256) != (profile.profile_id, profile.digest):
            raise DisclosureRefusal("normalization approval does not name this exact profile")


@dataclass(frozen=True, slots=True)
class ThirdPartyTransmissionApproval:
    """A typed vendor/pages approval loaded through a Tyrel approval artifact."""

    vendor: str
    candidate_artifact_digest: str
    page_ids: frozenset[str]
    manifest_sha256: str
    approval_reference: ApprovalRecordReference
    approval_record: Mapping[str, Any]
    approval_bytes: bytes

    # One builder, for the reason given on NormalizationApproval._scope_record.
    @staticmethod
    def _scope_record(
        *,
        vendor: str,
        candidate_artifact_digest: str,
        page_ids: frozenset[str],
        manifest_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema": THIRD_PARTY_APPROVAL_SUBJECT,
            "vendor": vendor,
            "candidate_artifact_digest": candidate_artifact_digest,
            "page_ids": sorted(page_ids),
            "manifest_sha256": manifest_sha256,
        }

    @property
    def scope_sha256(self) -> str:
        return self.scope_digest(
            vendor=self.vendor,
            candidate_artifact_digest=self.candidate_artifact_digest,
            page_ids=self.page_ids,
            manifest_sha256=self.manifest_sha256,
        )

    @classmethod
    def scope_digest(
        cls,
        *,
        vendor: str,
        candidate_artifact_digest: str,
        page_ids: frozenset[str],
        manifest_sha256: str,
    ) -> str:
        return _approval_digest(
            cls._scope_record(
                vendor=vendor,
                candidate_artifact_digest=candidate_artifact_digest,
                page_ids=page_ids,
                manifest_sha256=manifest_sha256,
            )
        )

    def __post_init__(self) -> None:
        if not is_sha256(self.candidate_artifact_digest) or not is_sha256(self.manifest_sha256):
            raise DisclosureRefusal(
                "third-party approval requires candidate and manifest SHA-256 pins"
            )
        if not isinstance(self.vendor, str) or not self.vendor:
            raise DisclosureRefusal("third-party approval must name a vendor")
        if not self.page_ids or any(
            not isinstance(page_id, str) or not page_id for page_id in self.page_ids
        ):
            raise DisclosureRefusal("third-party approval must name exact pages")
        record = MappingProxyType(dict(self.approval_record))
        object.__setattr__(self, "approval_record", record)
        checked = _checked_approval_record(self.approval_reference, self.approval_bytes)
        if checked != dict(record):
            raise DisclosureRefusal("third-party approval record differs from its checked bytes")
        if checked["action"] != "other":
            raise DisclosureRefusal(
                "third-party approval record must use the recorded approval action"
            )
        _require_approval_subject(
            checked,
            expected=THIRD_PARTY_APPROVAL_SUBJECT,
            label="third-party approval",
        )
        if checked["target_version_hash"] != self.scope_sha256:
            raise DisclosureRefusal(
                "third-party approval record does not bind this exact vendor, pages, candidate, and manifest"
            )

    @classmethod
    def load(
        cls,
        *,
        vendor: str,
        candidate_artifact_digest: str,
        page_ids: frozenset[str],
        manifest_sha256: str,
        approval_reference: ApprovalRecordReference,
        read_bytes: Callable[[str], bytes],
    ) -> "ThirdPartyTransmissionApproval":
        """Load an immutable vendor/pages approval from an approved private reader."""

        # Checked before it is dereferenced, for the reason `DataGateAuthority.load`
        # gives: reading `.relative_path` off a missing reference reports a Python
        # attribute where the governed condition is the absence of Tyrel's approval.
        # That fix reached one of four loaders; this is another. Found by the Opus
        # read of this branch.
        if not isinstance(approval_reference, ApprovalRecordReference):
            raise DisclosureRefusal(
                "third-party transmission approval is missing; this run requires a current "
                "approval-record artifact"
            )

        try:
            payload = read_bytes(approval_reference.relative_path)
            record = _checked_approval_record(approval_reference, payload)
        except Exception as error:
            raise DisclosureRefusal(f"third-party approval is unavailable: {error}") from error
        return cls(
            vendor=vendor,
            candidate_artifact_digest=candidate_artifact_digest,
            page_ids=page_ids,
            manifest_sha256=manifest_sha256,
            approval_reference=approval_reference,
            approval_record=record,
            approval_bytes=payload,
        )


@dataclass(frozen=True, slots=True)
class RunPlanApproval:
    """Tyrel's exact-data/models/budget approval for one declared measurement run."""

    protocol_sha256: str
    manifest_sha256: str
    candidate_roster_sha256: str
    witness_configuration_sha256: str
    prompt_registry_sha256: str
    normalization_profile_id: str
    normalization_profile_sha256: str
    budget_evidence_sha256: str
    private_sample_accounting_sha256: str
    approval_reference: ApprovalRecordReference
    approval_record: Mapping[str, Any]
    approval_bytes: bytes

    # One builder, for the reason given on NormalizationApproval._scope_record.
    @staticmethod
    def _scope_record(
        *,
        protocol_sha256: str,
        manifest_sha256: str,
        candidate_roster_sha256: str,
        witness_configuration_sha256: str,
        prompt_registry_sha256: str,
        normalization_profile_id: str,
        normalization_profile_sha256: str,
        budget_evidence_sha256: str,
        private_sample_accounting_sha256: str,
    ) -> dict[str, str]:
        return {
            "schema": RUN_PLAN_APPROVAL_SUBJECT,
            "protocol_sha256": protocol_sha256,
            "manifest_sha256": manifest_sha256,
            "candidate_roster_sha256": candidate_roster_sha256,
            "witness_configuration_sha256": witness_configuration_sha256,
            "prompt_registry_sha256": prompt_registry_sha256,
            "normalization_profile_id": normalization_profile_id,
            "normalization_profile_sha256": normalization_profile_sha256,
            "budget_evidence_sha256": budget_evidence_sha256,
            "private_sample_accounting_sha256": private_sample_accounting_sha256,
        }

    @property
    def scope_sha256(self) -> str:
        return self.scope_digest(
            protocol_sha256=self.protocol_sha256,
            manifest_sha256=self.manifest_sha256,
            candidate_roster_sha256=self.candidate_roster_sha256,
            witness_configuration_sha256=self.witness_configuration_sha256,
            prompt_registry_sha256=self.prompt_registry_sha256,
            normalization_profile_id=self.normalization_profile_id,
            normalization_profile_sha256=self.normalization_profile_sha256,
            budget_evidence_sha256=self.budget_evidence_sha256,
            private_sample_accounting_sha256=self.private_sample_accounting_sha256,
        )

    @classmethod
    def scope_digest(
        cls,
        *,
        protocol_sha256: str,
        manifest_sha256: str,
        candidate_roster_sha256: str,
        witness_configuration_sha256: str,
        prompt_registry_sha256: str,
        normalization_profile_id: str,
        normalization_profile_sha256: str,
        budget_evidence_sha256: str,
        private_sample_accounting_sha256: str,
    ) -> str:
        return _approval_digest(
            cls._scope_record(
                protocol_sha256=protocol_sha256,
                manifest_sha256=manifest_sha256,
                candidate_roster_sha256=candidate_roster_sha256,
                witness_configuration_sha256=witness_configuration_sha256,
                prompt_registry_sha256=prompt_registry_sha256,
                normalization_profile_id=normalization_profile_id,
                normalization_profile_sha256=normalization_profile_sha256,
                budget_evidence_sha256=budget_evidence_sha256,
                private_sample_accounting_sha256=private_sample_accounting_sha256,
            )
        )

    def __post_init__(self) -> None:
        digests = (
            self.protocol_sha256,
            self.manifest_sha256,
            self.candidate_roster_sha256,
            self.witness_configuration_sha256,
            self.prompt_registry_sha256,
            self.normalization_profile_sha256,
            self.budget_evidence_sha256,
            self.private_sample_accounting_sha256,
        )
        if any(not is_sha256(value) for value in digests):
            raise DisclosureRefusal("run-plan approval requires every sealed digest")
        if not isinstance(self.normalization_profile_id, str) or not self.normalization_profile_id:
            raise DisclosureRefusal("run-plan approval must name the normalization profile")
        record = MappingProxyType(dict(self.approval_record))
        object.__setattr__(self, "approval_record", record)
        checked = _checked_approval_record(self.approval_reference, self.approval_bytes)
        if checked != dict(record):
            raise DisclosureRefusal("run-plan approval record differs from its checked bytes")
        if checked["action"] != "other":
            raise DisclosureRefusal(
                "run-plan approval record must use the recorded approval action"
            )
        _require_approval_subject(
            checked, expected=RUN_PLAN_APPROVAL_SUBJECT, label="run-plan approval"
        )
        if checked["target_version_hash"] != self.scope_sha256:
            raise DisclosureRefusal("run-plan approval record does not bind its declared scope")

    @classmethod
    def load(
        cls,
        *,
        protocol_sha256: str,
        manifest_sha256: str,
        candidate_roster_sha256: str,
        witness_configuration_sha256: str,
        prompt_registry_sha256: str,
        normalization_profile_id: str,
        normalization_profile_sha256: str,
        budget_evidence_sha256: str,
        private_sample_accounting_sha256: str,
        approval_reference: ApprovalRecordReference,
        read_bytes: Callable[[str], bytes],
    ) -> "RunPlanApproval":
        """Load Tyrel's immutable approval of this exact private run plan."""

        # Checked before it is dereferenced, for the reason `DataGateAuthority.load`
        # gives: reading `.relative_path` off a missing reference reports a Python
        # attribute where the governed condition is the absence of Tyrel's approval.
        # That fix reached one of four loaders; this is another. Found by the Opus
        # read of this branch.
        if not isinstance(approval_reference, ApprovalRecordReference):
            raise DisclosureRefusal(
                "run-plan approval is missing; this run requires a current approval-record artifact"
            )

        try:
            payload = read_bytes(approval_reference.relative_path)
            record = _checked_approval_record(approval_reference, payload)
        except Exception as error:
            raise DisclosureRefusal(f"run-plan approval is unavailable: {error}") from error
        return cls(
            protocol_sha256=protocol_sha256,
            manifest_sha256=manifest_sha256,
            candidate_roster_sha256=candidate_roster_sha256,
            witness_configuration_sha256=witness_configuration_sha256,
            prompt_registry_sha256=prompt_registry_sha256,
            normalization_profile_id=normalization_profile_id,
            normalization_profile_sha256=normalization_profile_sha256,
            budget_evidence_sha256=budget_evidence_sha256,
            private_sample_accounting_sha256=private_sample_accounting_sha256,
            approval_reference=approval_reference,
            approval_record=record,
            approval_bytes=payload,
        )

    def require_scope(
        self,
        *,
        protocol_sha256: str,
        manifest_sha256: str,
        candidate_roster_sha256: str,
        witness_configuration_sha256: str,
        prompt_registry_sha256: str,
        profile: NormalizationProfile,
        private_sample_accounting_sha256: str,
    ) -> None:
        observed = (
            protocol_sha256,
            manifest_sha256,
            candidate_roster_sha256,
            witness_configuration_sha256,
            prompt_registry_sha256,
            profile.profile_id,
            profile.digest,
            private_sample_accounting_sha256,
        )
        expected = (
            self.protocol_sha256,
            self.manifest_sha256,
            self.candidate_roster_sha256,
            self.witness_configuration_sha256,
            self.prompt_registry_sha256,
            self.normalization_profile_id,
            self.normalization_profile_sha256,
            self.private_sample_accounting_sha256,
        )
        if observed != expected:
            raise DisclosureRefusal(
                "run-plan approval does not name this exact protocol, material, roster, prompts, "
                "witness configuration, normalization profile, and sample accounting"
            )


@dataclass(frozen=True, slots=True)
class RunAuthorization:
    """Run-scoped authorization without a boolean bypass for real material."""

    material_class: MaterialClass
    data_gate_authority: DataGateAuthority | None = None
    run_plan_approval: RunPlanApproval | None = None
    normalization_approval: NormalizationApproval | None = None
    external_approvals: Mapping[str, ThirdPartyTransmissionApproval] | None = None

    @classmethod
    def synthetic_fixture(cls) -> "RunAuthorization":
        """The only approval-free authority shape for local synthetic fakes."""

        return cls(material_class=MaterialClass.SYNTHETIC)

    def __post_init__(self) -> None:
        if not isinstance(self.material_class, MaterialClass):
            raise DisclosureRefusal("run authorization must name a MaterialClass")
        approvals = MappingProxyType(dict(self.external_approvals or {}))
        object.__setattr__(self, "external_approvals", approvals)
        # Each field is supposed to have come through its own content-addressed
        # .load(); these checks are what enforce that rather than trusting it. A
        # duck-typed stand-in is "an in-memory approver field", in README section
        # 1's own words, and is not authority.
        if self.data_gate_authority is not None and not isinstance(
            self.data_gate_authority, DataGateAuthority
        ):
            raise DisclosureRefusal("data_gate_authority must be a checked DataGateAuthority")
        if self.run_plan_approval is not None and not isinstance(
            self.run_plan_approval, RunPlanApproval
        ):
            raise DisclosureRefusal("run_plan_approval must be a checked RunPlanApproval")
        if self.normalization_approval is not None and not isinstance(
            self.normalization_approval, NormalizationApproval
        ):
            raise DisclosureRefusal(
                "normalization_approval must be a checked NormalizationApproval"
            )
        if any(
            not isinstance(approval, ThirdPartyTransmissionApproval)
            for approval in approvals.values()
        ):
            raise DisclosureRefusal(
                "every external approval must be a checked ThirdPartyTransmissionApproval"
            )
        if self.material_class is MaterialClass.PRIVATE_REGISTER:
            if self.data_gate_authority is None:
                raise DisclosureRefusal(
                    "private register material requires a current data-gate authority"
                )
        elif self.data_gate_authority is not None:
            raise DisclosureRefusal("only private register material may carry data-gate authority")
        if self.material_class is MaterialClass.SYNTHETIC:
            if (
                self.run_plan_approval is not None
                or self.normalization_approval is not None
                or approvals
            ):
                raise DisclosureRefusal("synthetic fixture runs may not claim human approvals")
        elif self.run_plan_approval is None or self.normalization_approval is None:
            raise DisclosureRefusal(
                "non-synthetic material requires run-plan and normalization-profile approvals"
            )

    def require_declared_run_plan(
        self,
        *,
        protocol_sha256: str,
        manifest_sha256: str,
        candidate_roster_sha256: str,
        witness_configuration_sha256: str,
        prompt_registry_sha256: str,
        profile: NormalizationProfile,
        private_sample_accounting_sha256: str,
    ) -> None:
        if self.material_class is MaterialClass.SYNTHETIC:
            return
        if self.run_plan_approval is None:  # defensive for type checkers and audits
            raise DisclosureRefusal("real declared run has no run-plan approval")
        self.run_plan_approval.require_scope(
            protocol_sha256=protocol_sha256,
            manifest_sha256=manifest_sha256,
            candidate_roster_sha256=candidate_roster_sha256,
            witness_configuration_sha256=witness_configuration_sha256,
            prompt_registry_sha256=prompt_registry_sha256,
            profile=profile,
            private_sample_accounting_sha256=private_sample_accounting_sha256,
        )


def require_authorized_delivery(
    identities: Iterable[ResolvedIdentity],
    acts: Iterable[EvaluationAct],
    authorization: RunAuthorization,
    *,
    profile: NormalizationProfile,
    manifest: EvaluationManifest | None,
    excluded_opaque_act_ids: Iterable[str] = (),
) -> None:
    """Check material classification and every external delivery before any call."""

    act_values = tuple(acts)
    material_classes = {act.image.material_class for act in act_values}
    if material_classes != {authorization.material_class}:
        raise DisclosureRefusal(
            "run authorization material class does not match every supplied image evidence record"
        )
    if authorization.material_class is not MaterialClass.SYNTHETIC:
        if manifest is None:
            raise DisclosureRefusal("real delivery requires a sealed evaluation manifest")
        manifest.require_scoreable_acts(
            (act.manifest_binding() for act in act_values),
            excluded_opaque_act_ids=excluded_opaque_act_ids,
        )
        manifest_classes = {member.material_class for member in manifest.members}
        if manifest_classes != {authorization.material_class}:
            raise DisclosureRefusal(
                "run authorization material class does not match every selected manifest member"
            )
        manifest_sha256 = manifest.manifest_sha256
    else:
        manifest_sha256 = None
    if authorization.material_class is MaterialClass.PRIVATE_REGISTER:
        # DataGateAuthority was validated on load, like every other approval field.
        if authorization.data_gate_authority is None:  # defensive for type checkers and audits
            raise DisclosureRefusal("private register delivery has no data-gate authority")

    if authorization.material_class is not MaterialClass.SYNTHETIC:
        if authorization.normalization_approval is None:
            raise DisclosureRefusal("real delivery has no normalization-profile approval")
        authorization.normalization_approval.require_profile(profile)

    if authorization.material_class is MaterialClass.SYNTHETIC:
        if any(identity.delivery is DeliveryMode.EXTERNAL for identity in identities):
            raise DisclosureRefusal(
                "synthetic fixture authorization cannot deliver a dossier to an external adapter"
            )
        return
    if authorization.material_class is not MaterialClass.PRIVATE_REGISTER:
        return
    page_ids = frozenset(act.image.opaque_page_id for act in act_values)
    for identity in identities:
        if identity.delivery is not DeliveryMode.EXTERNAL:
            continue
        approval = authorization.external_approvals.get(identity.candidate_key)
        if approval is None:
            raise DisclosureRefusal(
                f"external candidate {identity.candidate_key!r} has no vendor-and-pages approval"
            )
        if approval.vendor != identity.provider:
            raise DisclosureRefusal("third-party approval names a different vendor")
        if approval.candidate_artifact_digest != identity.artifact_digest:
            raise DisclosureRefusal("third-party approval names a different candidate snapshot")
        if approval.manifest_sha256 != manifest_sha256:
            raise DisclosureRefusal("third-party approval names a different evaluation manifest")
        if approval.page_ids != page_ids:
            raise DisclosureRefusal("third-party approval does not name exactly the run's pages")
