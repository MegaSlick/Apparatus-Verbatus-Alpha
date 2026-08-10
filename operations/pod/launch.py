"""Gated pod creation and adoption, backed by the verified-shutdown path."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Callable

from .arming import (
    ControllerArmer,
    ControllerArming,
    ControllerReadiness,
    FailClosedControllerArmer,
)
from .lease import LeaseStore, PodLease
from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    PendingCreateIntent,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    SpendRefusal,
    utc_now,
)
from .provider import PodProvider
from .shutdown import CloseReport, VerifiedShutdown
from .spend import (
    SpendAssessment,
    SpendPolicy,
    assess_spend,
    confirmation_phrase,
    require_confirmation,
)


def _bind_report_path_to_launch(command: tuple[str, ...], launch_token: str) -> tuple[str, ...]:
    """Fold the launch token into the pod-side report path's file name.

    A volume outlives any one pod, so an unbound report path lets a second
    launch's durable evidence overwrite the first's (GOVERNANCE 4).  Binding
    happens once, here, at sealing time -- the request's own validation then
    refuses a report path that does not carry the sealed token.

    ``PodCreateRequest.__post_init__`` already refuses a command that does not
    carry exactly one ``--report-path`` with a value, so neither refusal below
    is reachable through ``create``.  They are stated anyway because this
    helper's contract would otherwise be enforced only at a distance, and
    because ``tuple.index`` fails on a money path with a message that names
    nothing.  ``create`` reports either as a request refusal, not a lease one.
    """

    if "--report-path" not in command:
        raise ValueError("pod request docker_start_cmd carries no --report-path flag")
    index = command.index("--report-path") + 1
    if index >= len(command):
        raise ValueError("pod request --report-path flag carries no value")
    original = PurePosixPath(command[index])
    bound_name = f"{original.stem}-{launch_token}{original.suffix}"
    bound_path = str(original.with_name(bound_name))
    return command[:index] + (bound_path,) + command[index + 1 :]


class LaunchState(StrEnum):
    """Create/adopt outcomes.  Only guarded creation/adoption is green."""

    PREVIEW = "preview"
    REFUSED_SHUTDOWN_NOT_READY = "refused-shutdown-not-ready"
    REFUSED_CONTROLLER_NOT_READY = "refused-controller-not-ready"
    REFUSED_RUNTIME_CONTRACT = "refused-runtime-contract"
    REFUSED_REQUEST = "refused-request"
    REFUSED_CEILING = "refused-ceiling"
    REFUSED_CONFIRMATION = "refused-confirmation"
    PROVIDER_FAILURE = "provider-failure"
    LEASE_FAILURE = "lease-failure"
    CREATED_GUARDED = "created-guarded"
    ADOPTED_GUARDED = "adopted-guarded"
    CREATE_UNLEASED = "create-unleased"
    CONTROLLERS_UNARMED = "controllers-unarmed"


@dataclass(frozen=True, slots=True)
class PaidActionPreview:
    """The price and ceilings an operator must see before typing confirmation."""

    action: str
    subject: str
    assessment: SpendAssessment

    @property
    def confirmation_phrase(self) -> str:
        """Bind the typed acknowledgement to this action and displayed price."""

        return confirmation_phrase(
            self.action,
            self.subject,
            self.assessment.estimate.pod_hourly_usd,
            self.assessment.estimate.volume_hourly_usd,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "action": self.action,
            "subject": self.subject,
            "confirmation_phrase": self.confirmation_phrase,
            "spend": self.assessment.to_record(),
        }


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """A named result that keeps provider or lease failure non-green."""

    state: LaunchState
    preview: PaidActionPreview | None = None
    record: PodRecord | None = None
    lease_path: Path | None = None
    owner_token: str | None = None
    detail: str = ""
    close_report: CloseReport | None = None
    controller_arming: ControllerArming | None = None
    controller_readiness: ControllerReadiness | None = None

    @property
    def green(self) -> bool:
        return self.state in {LaunchState.CREATED_GUARDED, LaunchState.ADOPTED_GUARDED}


class PodRuntime:
    """The local controller.  It cannot make a paid action without an explicit call."""

    def __init__(
        self,
        provider: PodProvider,
        *,
        provider_name: str,
        spend_policy: SpendPolicy,
        lease_root: str | Path,
        shutdown: VerifiedShutdown | None = None,
        now: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        controller_armer: ControllerArmer | None = None,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.spend_policy = spend_policy
        self.lease_root = Path(lease_root)
        if shutdown is None:
            if spend_policy.configured:
                assert spend_policy.shutdown_deadline_seconds is not None
                assert spend_policy.shutdown_poll_interval_seconds is not None
                assert spend_policy.billing_cutoff_margin_seconds is not None
                shutdown = VerifiedShutdown(
                    provider,
                    timeout_seconds=spend_policy.shutdown_deadline_seconds,
                    poll_seconds=spend_policy.shutdown_poll_interval_seconds,
                    billing_cutoff_margin_seconds=spend_policy.billing_cutoff_margin_seconds,
                )
            else:
                shutdown = VerifiedShutdown(provider)
        self.shutdown = shutdown
        self.now = now
        self.token_factory = token_factory
        self.controller_armer = controller_armer or FailClosedControllerArmer(now=now)

    def preview_create(self, request: PodCreateRequest) -> LaunchResult:
        """Prove shutdown wiring first, then show the price estimate and ceilings.

        The estimate's own ``source`` says whether it came from the provider
        or a reviewed local price sheet -- RunPod has no pre-create quote
        endpoint, so a create preview is priced from config, not the provider.
        """

        readiness = self.shutdown.prove_ready()
        if not readiness.ready:
            return LaunchResult(
                LaunchState.REFUSED_SHUTDOWN_NOT_READY,
                detail=f"shutdown path is not ready: {', '.join(readiness.missing_verbs)}",
            )
        controller_readiness = self._arming_preflight("create", request)
        if not controller_readiness.ready:
            return LaunchResult(
                LaunchState.REFUSED_CONTROLLER_NOT_READY,
                detail=f"controllers are not ready: {controller_readiness.detail}",
                controller_readiness=controller_readiness,
            )
        try:
            estimate = self.provider.estimate(request)
        except Exception as error:
            return LaunchResult(LaunchState.PROVIDER_FAILURE, detail=f"estimate failed: {error}")
        return self._preview("create", request.name, estimate, request.hard_deadline)

    def create(self, request: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        """Arm a durable intent before a provider create, then bind its exact pod id."""

        preview_result = self.preview_create(request)
        if preview_result.state is not LaunchState.PREVIEW:
            return preview_result
        assert preview_result.preview is not None
        if not preview_result.preview.assessment.allowed:
            return LaunchResult(
                LaunchState.REFUSED_CEILING,
                preview_result.preview,
                detail="; ".join(preview_result.preview.assessment.reasons),
            )
        try:
            require_confirmation(confirmation, preview_result.preview.confirmation_phrase)
        except SpendRefusal as error:
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION, preview_result.preview, detail=str(error)
            )

        try:
            lease_id = self._token("lease id")
            owner_token = self._token("lease owner token")
        except ValueError as error:
            return LaunchResult(
                LaunchState.LEASE_FAILURE, preview_result.preview, detail=str(error)
            )
        # Sealing is request preparation, not lease arming.  Inside the lease
        # try below, a malformed request reported as LEASE_FAILURE -- the durable
        # store named for a fault that never touched it, on a money path.  Found
        # by CodeRabbit on this branch.
        try:
            sealed = self._sealed_request(
                request, lease_id, preview_result.preview.assessment.estimate
            )
            pending = PendingCreateIntent.from_request(sealed, launch_token=lease_id)
        except Exception as error:
            return LaunchResult(
                LaunchState.REFUSED_REQUEST,
                preview_result.preview,
                detail=f"pod request could not be sealed for launch: {error}",
            )
        store = self._store(lease_id)
        try:
            store.create(
                PodLease(
                    lease_id=lease_id,
                    launch_token=lease_id,
                    provider_name=self.provider_name,
                    pod_id=None,
                    volume_id=request.volume_id,
                    pod_hourly_usd=preview_result.preview.assessment.estimate.pod_hourly_usd,
                    volume_hourly_usd=preview_result.preview.assessment.estimate.volume_hourly_usd,
                    created_at=self.now(),
                    started_at=None,
                    hard_deadline=request.hard_deadline,
                    owner_token=owner_token,
                    heartbeat_at=self.now(),
                    pending_create=pending,
                )
            )
        except Exception as error:
            return LaunchResult(
                LaunchState.LEASE_FAILURE,
                preview_result.preview,
                detail=f"could not arm lease: {error}",
            )
        try:
            record = self.provider.create(sealed)
        except Exception as error:
            return LaunchResult(
                LaunchState.PROVIDER_FAILURE,
                preview_result.preview,
                lease_path=store.path,
                detail=f"provider create failed after pending lease was recorded: {error}",
            )
        try:
            bound = store.bind_pod(owner_token=owner_token, record=record, now=self.now())
        except Exception as error:
            close, detail = self._close_and_record(
                record=record,
                reason="create returned before lease binding failed",
                store=store,
                owner_token=owner_token,
                situation=f"created pod could not be bound to its lease: {error}",
            )
            return LaunchResult(
                LaunchState.CREATE_UNLEASED,
                preview_result.preview,
                record=record,
                lease_path=store.path,
                owner_token=owner_token,
                detail=detail,
                close_report=close,
            )
        if record.runtime_contract is None or not record.runtime_contract.matches(sealed):
            return self._contract_mismatch_close(
                action="create",
                record=record,
                store=store,
                owner_token=owner_token,
                preview=preview_result.preview,
            )
        actual = self._reassess_actual_price(
            action="create",
            record=record,
            request=sealed,
            store=store,
            owner_token=owner_token,
            preview=preview_result.preview,
        )
        if actual is not None:
            return actual
        return self._arm_or_close(
            action="create",
            request=sealed,
            record=record,
            lease=bound,
            store=store,
            owner_token=owner_token,
            preview=preview_result.preview,
            success_state=LaunchState.CREATED_GUARDED,
        )

    def preview_adopt(self, pod_id: str, *, expected: PodCreateRequest) -> LaunchResult:
        """Use the exact same shutdown proof, estimate display, and ceiling calculation."""

        expected = self._policy_bound_request(expected)
        readiness = self.shutdown.prove_ready()
        if not readiness.ready:
            return LaunchResult(
                LaunchState.REFUSED_SHUTDOWN_NOT_READY,
                detail=f"shutdown path is not ready: {', '.join(readiness.missing_verbs)}",
            )
        controller_readiness = self._arming_preflight("adopt", expected)
        if not controller_readiness.ready:
            return LaunchResult(
                LaunchState.REFUSED_CONTROLLER_NOT_READY,
                detail=f"controllers are not ready: {controller_readiness.detail}",
                controller_readiness=controller_readiness,
            )
        try:
            record = self.provider.adopt(pod_id)
        except Exception as error:
            return LaunchResult(
                LaunchState.PROVIDER_FAILURE, detail=f"adopt inspection failed: {error}"
            )
        if record.runtime_contract is None or not record.runtime_contract.matches(expected):
            return LaunchResult(
                LaunchState.REFUSED_RUNTIME_CONTRACT,
                record=record,
                detail="adopted pod does not prove the requested on-demand image/template/volume/timer contract",
            )
        result = self._preview("adopt", record.pod_id, record.estimate, expected.hard_deadline)
        return LaunchResult(result.state, result.preview, record=record, detail=result.detail)

    def adopt(
        self,
        pod_id: str,
        *,
        expected: PodCreateRequest,
        confirmation: str | None,
    ) -> LaunchResult:
        """No pod bills its way around the gate: adoption shares every create check."""

        expected = self._policy_bound_request(expected)
        preview_result = self.preview_adopt(pod_id, expected=expected)
        if preview_result.state is not LaunchState.PREVIEW:
            return preview_result
        assert preview_result.preview is not None and preview_result.record is not None
        if not preview_result.preview.assessment.allowed:
            return LaunchResult(
                LaunchState.REFUSED_CEILING,
                preview_result.preview,
                record=preview_result.record,
                detail="; ".join(preview_result.preview.assessment.reasons),
            )
        try:
            require_confirmation(confirmation, preview_result.preview.confirmation_phrase)
        except SpendRefusal as error:
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION,
                preview_result.preview,
                record=preview_result.record,
                detail=str(error),
            )
        try:
            lease_id = self._token("lease id")
            owner_token = self._token("lease owner token")
        except ValueError as error:
            return LaunchResult(
                LaunchState.LEASE_FAILURE,
                preview_result.preview,
                record=preview_result.record,
                detail=str(error),
            )
        store = self._store(lease_id)
        try:
            store.create(
                PodLease(
                    lease_id=lease_id,
                    launch_token=lease_id,
                    provider_name=self.provider_name,
                    pod_id=preview_result.record.pod_id,
                    volume_id=preview_result.record.volume_id,
                    pod_hourly_usd=preview_result.record.estimate.pod_hourly_usd,
                    volume_hourly_usd=preview_result.record.estimate.volume_hourly_usd,
                    created_at=self.now(),
                    started_at=preview_result.record.created_at,
                    hard_deadline=expected.hard_deadline,
                    owner_token=owner_token,
                    heartbeat_at=self.now(),
                    phase="active",
                )
            )
        except Exception as error:
            try:
                close = self.shutdown.close(
                    preview_result.record,
                    reason="confirmed adoption could not arm its durable lease",
                )
            except Exception as close_error:
                close = None
                detail = (
                    f"could not record adoption lease: {error}; "
                    f"immediate close raised: {close_error}"
                )
            else:
                detail = (
                    f"could not record adoption lease: {error}; "
                    f"immediate close is {close.state.value}"
                )
            return LaunchResult(
                LaunchState.LEASE_FAILURE,
                preview_result.preview,
                record=preview_result.record,
                detail=detail,
                close_report=close,
            )
        bound = store.load()
        if bound is None:
            # Not an assert.  `assert` disappears under `python -O`, and a `None`
            # lease would then reach controller arming and surface as an arming
            # fault rather than the durable-store fault it is -- the objection
            # controllers.py records about the same family, on the same money
            # path.  This one is reachable: `load()` reads a file back off disk.
            # An adopted pod that cannot be guarded does not keep billing, so
            # this closes it exactly as a failed arming would.
            close, detail = self._close_and_record(
                record=preview_result.record,
                reason="adoption lease could not be read back after it was written",
                store=store,
                owner_token=owner_token,
                situation="adoption lease was written but could not be read back",
            )
            return LaunchResult(
                LaunchState.LEASE_FAILURE,
                preview_result.preview,
                record=preview_result.record,
                lease_path=store.path,
                owner_token=owner_token,
                detail=detail,
                close_report=close,
            )
        return self._arm_or_close(
            action="adopt",
            request=expected,
            record=preview_result.record,
            lease=bound,
            store=store,
            owner_token=owner_token,
            preview=preview_result.preview,
            success_state=LaunchState.ADOPTED_GUARDED,
        )

    def _preview(
        self, action: str, subject: str, estimate: PodEstimate, hard_deadline: datetime
    ) -> LaunchResult:
        assessment = assess_spend(
            self.spend_policy, estimate, requested_deadline=hard_deadline, now=self.now()
        )
        return LaunchResult(LaunchState.PREVIEW, PaidActionPreview(action, subject, assessment))

    def _store(self, lease_id: str) -> LeaseStore:
        return LeaseStore(self.lease_root / f"{lease_id}.json")

    def _arming_preflight(self, action: str, request: PodCreateRequest) -> ControllerReadiness:
        try:
            return self.controller_armer.preflight(
                action=action, request=request, policy=self.spend_policy
            )
        except Exception as error:
            return ControllerReadiness(
                False, self.now(), f"controller arming preflight failed: {error}", {}
            )

    def _sealed_request(
        self, request: PodCreateRequest, launch_token: str, estimate: PodEstimate
    ) -> PodCreateRequest:
        request = self._policy_bound_request(request)
        return replace(
            request,
            docker_start_cmd=_bind_report_path_to_launch(request.docker_start_cmd, launch_token),
            metadata={
                **dict(request.metadata),
                "VERBATUS_HARD_DEADLINE": request.hard_deadline.isoformat().replace("+00:00", "Z"),
                "VERBATUS_LAUNCH_TOKEN": launch_token,
                "VERBATUS_VOLUME_ID": request.volume_id,
                "VERBATUS_POD_HOURLY_USD": str(estimate.pod_hourly_usd),
                "VERBATUS_VOLUME_ONGOING_HOURLY_USD": str(estimate.volume_hourly_usd),
                "VERBATUS_REQUESTED_AT": self.now().isoformat().replace("+00:00", "Z"),
            },
        )

    def _policy_bound_request(self, request: PodCreateRequest) -> PodCreateRequest:
        """Bind both controllers to the configured billing-evidence margin.

        An adoption never recreates a pod, so it cannot receive a later timer
        configuration. Its provider-observed contract must therefore prove the
        same margin that this runtime would give a newly created pod.
        """

        if not self.spend_policy.configured:
            return request
        assert self.spend_policy.billing_cutoff_margin_seconds is not None
        return replace(
            request,
            metadata={
                **dict(request.metadata),
                BILLING_CUTOFF_MARGIN_ENV: str(self.spend_policy.billing_cutoff_margin_seconds),
            },
        )

    def _arm_or_close(
        self,
        *,
        action: str,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
        store: LeaseStore,
        owner_token: str,
        preview: PaidActionPreview,
        success_state: LaunchState,
    ) -> LaunchResult:
        """Persist both controller acknowledgements or immediately close non-green."""

        try:
            arming = self.controller_armer.arm(
                action=action,
                request=request,
                record=record,
                lease=lease,
                store=store,
                owner_token=owner_token,
                policy=self.spend_policy,
            )
        except Exception as error:
            arming = ControllerArming(
                False, False, self.now(), f"controller arming failed: {error}", {}
            )
        if arming.armed:
            try:
                self._validate_arming_binding(
                    arming=arming, request=request, record=record, lease=lease
                )
            except Exception as error:
                arming = ControllerArming(
                    False,
                    arming.pod_timer_acknowledged,
                    self.now(),
                    f"controller acknowledgement is not bound to this launch: {error}",
                    arming.receipt,
                )
        if arming.armed:
            try:
                store.record_controller_arming(
                    owner_token=owner_token,
                    controller_record=arming.to_record(),
                    now=self.now(),
                )
            except Exception as error:
                arming = ControllerArming(
                    False,
                    arming.pod_timer_acknowledged,
                    self.now(),
                    f"controller receipt could not be durably recorded: {error}",
                    arming.receipt,
                )
            else:
                return LaunchResult(
                    success_state,
                    preview,
                    record=record,
                    lease_path=store.path,
                    owner_token=owner_token,
                    detail=(
                        "pod passed shutdown, ceiling, confirmation, exact runtime-contract, "
                        "lease, laptop-supervisor, and pod-timer acknowledgement gates"
                    ),
                    controller_arming=arming,
                )
        close, close_detail = self._close_and_record(
            record=record,
            reason=f"{action} controller arming failed",
            store=store,
            owner_token=owner_token,
            situation="controller arming failed",
        )
        return LaunchResult(
            LaunchState.CONTROLLERS_UNARMED,
            preview,
            record=record,
            lease_path=store.path,
            owner_token=owner_token,
            detail=close_detail,
            close_report=close,
            controller_arming=arming,
        )

    @staticmethod
    def _validate_arming_binding(
        *,
        arming: ControllerArming,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
    ) -> None:
        """Bind the observed timer report to the path this exact pod was told to write."""

        receipt = arming.receipt
        if receipt.get("lease_id") != lease.lease_id or receipt.get("pod_id") != record.pod_id:
            raise ValueError("receipt names another lease or pod")
        expected_deadline = request.hard_deadline.isoformat().replace("+00:00", "Z")
        if receipt.get("hard_deadline") != expected_deadline:
            raise ValueError("receipt names another hard deadline")
        timer = receipt.get("pod_timer")
        if not isinstance(timer, dict):
            raise ValueError("receipt has no pod-timer observation")
        command = request.docker_start_cmd
        report_flag = command.index("--report-path")
        expected_path = command[report_flag + 1]
        if timer.get("report_path") != expected_path:
            raise ValueError("pod timer acknowledged a different durable report path")

    def _close_and_record(
        self,
        *,
        record: PodRecord,
        reason: str,
        store: LeaseStore,
        owner_token: str,
        situation: str,
    ) -> tuple[CloseReport | None, str]:
        """Attempt an immediate verified close, durably record it, and describe both.

        Shared by every non-green path that must close a pod that already
        exists rather than leave it running: `situation` names why closing
        was necessary; the returned detail always says what the close did
        and, separately, whether the close evidence itself could be persisted.
        """

        try:
            close = self.shutdown.close(record, reason=reason)
        except Exception as error:
            return None, f"{situation}; immediate close raised: {error}"
        detail = f"{situation}; immediate close is {close.state.value}"
        try:
            store.record_close(
                owner_token=owner_token,
                close_record=close.to_record(),
                verified=close.verified,
                now=self.now(),
            )
        except Exception as error:
            detail = f"{detail}; close evidence could not be recorded: {error}"
        return close, detail

    def _reassess_actual_price(
        self,
        *,
        action: str,
        record: PodRecord,
        request: PodCreateRequest,
        store: LeaseStore,
        owner_token: str,
        preview: PaidActionPreview,
    ) -> LaunchResult | None:
        """`None` when the pod that arrived is still inside every ceiling.

        Adoption already assesses the observed record, so only `create` needs
        this: it gates on `estimate()` before the pod exists, and the price the
        provider returns afterwards is the one that bills.
        """

        assessment = assess_spend(
            self.spend_policy,
            record.estimate,
            requested_deadline=request.hard_deadline,
            now=self.now(),
        )
        if assessment.allowed:
            return None
        actual_preview = PaidActionPreview(preview.action, preview.subject, assessment)
        close, detail = self._close_and_record(
            record=record,
            reason=f"{action} price on the created pod exceeded its ceiling",
            store=store,
            owner_token=owner_token,
            situation=(
                "created pod bills outside the configured ceiling ("
                + "; ".join(assessment.reasons)
                + ")"
            ),
        )
        return LaunchResult(
            LaunchState.REFUSED_CEILING,
            actual_preview,
            record=record,
            lease_path=store.path,
            owner_token=owner_token,
            detail=detail,
            close_report=close,
        )

    def _contract_mismatch_close(
        self,
        *,
        action: str,
        record: PodRecord,
        store: LeaseStore,
        owner_token: str,
        preview: PaidActionPreview,
    ) -> LaunchResult:
        """A created pod with an unproven effective shape is never a green launch."""

        close, detail = self._close_and_record(
            record=record,
            reason=f"{action} effective runtime contract was unproven",
            store=store,
            owner_token=owner_token,
            situation="effective runtime contract was unproven",
        )
        return LaunchResult(
            LaunchState.REFUSED_RUNTIME_CONTRACT,
            preview,
            record=record,
            lease_path=store.path,
            owner_token=owner_token,
            detail=detail,
            close_report=close,
        )

    def _token(self, label: str) -> str:
        value = self.token_factory()
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError(f"{label} must be 32 lowercase random hexadecimal characters")
        return value
