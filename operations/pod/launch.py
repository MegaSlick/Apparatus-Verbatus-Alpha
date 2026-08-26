"""Gated pod creation and adoption, backed by the verified-shutdown path."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Callable, Final, Iterator

from .arming import (
    ControllerArmer,
    ControllerArming,
    ControllerReadiness,
    FailClosedControllerArmer,
)
from .durable import atomic_write, canonical_json
from .lease import LeaseStore, PodLease
from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    AccountBalanceObservation,
    PendingCreateIntent,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    SpendRefusal,
    utc_now,
)
from .notify_bridge import Notifier, NotifyOutcome, silent
from .provider import AccountBalanceProvider, PodProvider
from .shutdown import CloseReport, VerifiedShutdown
from .spend import (
    SpendAssessment,
    SpendPolicy,
    assess_spend,
    confirmation_phrase,
    mint_challenge,
    require_confirmation,
)


def phraseless(preview: PaidActionPreview | None) -> PaidActionPreview | None:
    """The same preview with its challenge withheld.

    A refused confirmation is printed and logged, and the challenge it carries
    is still spendable -- a typo deliberately does not burn the preview.  The
    refusal report therefore must not carry a phrase that would authorize
    anything, which is the policy `_reassess_actual_price` already states for
    its own refusal.  The operator retypes from the preview they were shown.

    On the returned preview the `confirmation_phrase` property raises rather
    than returning a string; callers rendering a refusal read `to_record()`,
    which shows the phrase as absent instead.

    Public because the operator surface writes durable receipts of the same
    previews and is bound by the same policy. A helper that was private here
    while the rule it enforces applied there is how the phrase ended up in a
    receipt.
    """

    if preview is None or preview.challenge is None:
        return preview
    return PaidActionPreview(preview.action, preview.subject, preview.assessment)


def price_move_note(reviewed: SpendAssessment, actual: SpendAssessment) -> str:
    """Say when the pod that now exists does not bill at the price that was confirmed.

    The confirmation binds the reviewed price, and `create` refuses outright when
    the price has already moved by the time the challenge is claimed. It cannot
    bind what the provider charges *after* that: the pod is created in between,
    and only the configured hourly ceiling bounds the difference. That window is
    the honest limit of this gate, so a launch that lands on the far side of it
    names the fact rather than reporting a price nobody agreed to underneath a
    clean status -- GOVERNANCE 2, and GOVERNANCE 10 on claiming only what was
    actually measured.

    Public for the same reason `phraseless` is: the operator surface writes the
    record a person reads, and a rule that only holds inside this module is a
    rule the surface silently does not keep.
    """

    if (
        actual.estimate.pod_hourly_usd == reviewed.estimate.pod_hourly_usd
        and actual.estimate.volume_hourly_usd == reviewed.estimate.volume_hourly_usd
    ):
        return ""
    return (
        f"; the created pod bills at ${actual.estimate.pod_hourly_usd}/hr plus "
        f"${actual.estimate.volume_hourly_usd}/hr volume, not the "
        f"${reviewed.estimate.pod_hourly_usd}/hr plus "
        f"${reviewed.estimate.volume_hourly_usd}/hr volume that was confirmed: the price "
        "moved after the confirmation was claimed, and stayed inside the configured ceiling"
    )


def _open_lease_found(observed: str) -> str:
    """A paid action here is open, and this one is refused until it is closed."""

    return (
        "a paid action is already armed in this lease root and no verified close is "
        f"recorded for it: {observed}; at most one pod may be live at a time, so this one "
        "is refused -- close the open one and confirm its verified close first; no paid "
        "action occurred"
    )


def _unproven_lease_root(observed: str) -> str:
    """The same refusal, saying only what was actually established.

    Nothing here observed an open paid action; what it observed is that it could
    not rule one out. GOVERNANCE 10 makes that a different sentence, and an
    operator who is told a pod is running when the real fault is an unreadable
    file will go looking for the wrong thing.
    """

    return (
        "this lease root could not be proved clear of an open paid action: "
        f"{observed}; a pod that cannot be ruled out is treated as live, so this one is "
        "refused -- repair or account for the named lease evidence first; no paid action "
        "occurred"
    )


def _spend_refusal_state(assessment: SpendAssessment) -> LaunchState:
    """Distinguish the three ways a spend assessment refuses.

    An observed balance at or below the floor, a balance that could not be
    observed at all, and an ordinary price-ceiling breach are three different
    operational situations -- the first two are money-safety refusals the
    ruling names explicitly, and collapsing either into ``REFUSED_CEILING``
    hides which one actually happened from whoever reads the result.
    """

    if assessment.hard_floor_triggered:
        return LaunchState.REFUSED_BALANCE_FLOOR
    if assessment.balance_unobservable_triggered:
        return LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    return LaunchState.REFUSED_CEILING


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


SPEND_ALERT_DEBOUNCE_SECONDS: Final = 900
"""Matches notify.sh's own ``start`` suppression window (operations/notify/notify.sh).

notify.sh deliberately never suppresses a ``milestone`` itself -- a rate limit there
could swallow a real result (operations/notify/README.md). A hovering balance is not
that: the same alert is reassessed on every preview, on the internal re-preview inside
``create``/``adopt``, and (for ``create``) again after the pod actually exists, so an
unthrottled send pages Tyrel two or more times for one decision. The dedup belongs at
this wiring, before notify.sh is ever called.
"""

SPEND_ALERT_RECOVERY_OBSERVATIONS: Final = 2
"""Consecutive safe readings required before another low episode can page.

One sample above the line followed immediately by another low sample is a
flapping source, not proof of recovery.  Two safe observations re-arm the edge
without waiting out the delivery debounce, so a genuinely new drop is not lost.
"""


class LaunchState(StrEnum):
    """Create/adopt outcomes.  Only guarded creation/adoption is green."""

    PREVIEW = "preview"
    REFUSED_SHUTDOWN_NOT_READY = "refused-shutdown-not-ready"
    REFUSED_CONTROLLER_NOT_READY = "refused-controller-not-ready"
    REFUSED_RUNTIME_CONTRACT = "refused-runtime-contract"
    REFUSED_REQUEST = "refused-request"
    REFUSED_CEILING = "refused-ceiling"
    REFUSED_BALANCE_FLOOR = "refused-balance-floor"
    REFUSED_BALANCE_UNOBSERVABLE = "refused-balance-unobservable"
    REFUSED_ACTIVE_LEASE = "refused-active-lease"
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
    challenge: str | None = None
    """The one-time value this preview issued, or ``None`` when none was outstanding.

    ``None`` is the honest representation of "nobody previewed this in this run", and it
    is what makes a derived phrase useless: there is no phrase to derive.
    """

    @property
    def confirmation_phrase(self) -> str:
        """Bind the typed acknowledgement to this action, price, and preview."""

        return confirmation_phrase(
            self.action,
            self.subject,
            self.assessment.estimate.pod_hourly_usd,
            self.assessment.estimate.volume_hourly_usd,
            self.challenge or "",
        )

    def to_record(self) -> dict[str, object]:
        return {
            "action": self.action,
            "subject": self.subject,
            # A preview with no challenge cannot be confirmed, so it shows no phrase
            # rather than one that would be refused.
            "confirmation_phrase": self.confirmation_phrase if self.challenge else None,
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


@dataclass(frozen=True, slots=True)
class _OutstandingChallenge:
    """One issued preview: what it authorizes, not merely that it exists."""

    challenge: str
    hard_deadline: datetime
    request_digest: str

    def matches_deadline(self, hard_deadline: datetime) -> bool:
        return self.hard_deadline == hard_deadline


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
        challenge_factory: Callable[[], str] = mint_challenge,
        controller_armer: ControllerArmer | None = None,
        balance_source: AccountBalanceProvider | None = None,
        notifier: Notifier = silent,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.spend_policy = spend_policy
        self.lease_root = Path(lease_root)
        if shutdown is None:
            if spend_policy.configured:
                # Narrowing, not a check: `SpendPolicy.__post_init__` raises `SpendRefusal`
                # on a configured policy missing any ceiling, and a `raise` survives `-O`.
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
        self.challenge_factory = challenge_factory
        self.controller_armer = controller_armer or FailClosedControllerArmer(now=now)
        self.balance_source = balance_source or (
            provider if isinstance(provider, AccountBalanceProvider) else None
        )
        self.notifier = notifier
        # Outstanding preview challenges, keyed by (action, subject), each holding the
        # challenge, the hard deadline it was assessed against, and the digest of the
        # exact request that was reviewed. In-memory and per-process on purpose: a
        # challenge that outlived the run would be exactly the replayable credential
        # this gate exists to refuse. The lock makes claiming one atomic, so overlapping
        # callers cannot both spend the same confirmation.
        self._outstanding: dict[tuple[str, str], _OutstandingChallenge] = {}
        self._challenge_lock = threading.Lock()

    def preview_create(self, request: PodCreateRequest, *, mint: bool = True) -> LaunchResult:
        """Prove shutdown wiring first, then show the price estimate and ceilings.

        The estimate's own ``source`` says whether it came from the provider
        or a reviewed local price sheet -- RunPod has no pre-create quote
        endpoint, so a create preview is priced from config, not the provider.

        ``mint`` is ``False`` only when ``create`` re-runs these checks against an
        already-issued challenge. Callers asking to see a price leave it alone.
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
        return self._preview(
            "create",
            request.name,
            estimate,
            request.hard_deadline,
            request.reviewed_digest(),
            mint=mint,
        )

    def create(self, request: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        """Serialize the shared lease-root assessment through durable reservation."""

        try:
            with self._spend_gate_lock():
                return self._create_locked(request, confirmation=confirmation)
        except OSError as error:
            return LaunchResult(
                LaunchState.REFUSED_BALANCE_UNOBSERVABLE,
                detail=(
                    "balance safety could not be established because the spend-reservation "
                    f"lock failed: {error}; no paid action occurred"
                ),
            )

    def _create_locked(
        self, request: PodCreateRequest, *, confirmation: str | None
    ) -> LaunchResult:
        """Arm a durable intent before a provider create, then bind its exact pod id."""

        preview_result = self.preview_create(request, mint=False)
        if preview_result.state is not LaunchState.PREVIEW:
            return preview_result
        # Narrowing, not a check: `_preview` is the only producer of `PREVIEW` and it
        # always carries a `PaidActionPreview`, under `-O` as much as without it.
        assert preview_result.preview is not None
        if not preview_result.preview.assessment.allowed:
            # Same policy as the confirmation refusals: a refusal report never
            # carries a phrase whose challenge is still spendable.
            return LaunchResult(
                _spend_refusal_state(preview_result.preview.assessment),
                phraseless(preview_result.preview),
                detail="; ".join(preview_result.preview.assessment.reasons),
            )
        # After the ceilings, before the challenge is consumed: a refusal here leaves
        # the preview spendable, so an operator who closes the open pod can confirm the
        # phrase they were already shown, and no existing refusal loses its precedence.
        open_lease = self._open_lease_refusal()
        if open_lease is not None:
            return LaunchResult(
                LaunchState.REFUSED_ACTIVE_LEASE,
                phraseless(preview_result.preview),
                detail=open_lease,
            )
        # Nothing outstanding: there is no phrase to build, so say so before trying.
        # This is an early exit, not the guard -- `_claim_challenge` re-reads under the
        # lock, so a challenge consumed between here and there still ends in a refusal.
        claimed = preview_result.preview.challenge is not None
        if claimed:
            try:
                claimed = self._claim_challenge(
                    "create",
                    request.name,
                    request.hard_deadline,
                    request.reviewed_digest(),
                    preview_result.preview.challenge or "",
                    preview_result.preview.confirmation_phrase,
                    confirmation,
                )
            except SpendRefusal as error:
                return LaunchResult(
                    LaunchState.REFUSED_CONFIRMATION,
                    phraseless(preview_result.preview),
                    detail=str(error),
                )
        if not claimed:
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION,
                phraseless(preview_result.preview),
                detail=(
                    "no preview in this run issued a challenge for this create at this "
                    "hard deadline; run the preview and confirm the phrase it prints; "
                    "no paid action occurred"
                ),
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
        if record.state.upper() != "RUNNING":
            # `adopt` refuses a non-RUNNING pod outright; a create response in
            # EXITED or TERMINATED is the same dead-but-billing shape and must
            # end in a close, not a green launch.  Case-insensitive because
            # `PodRecord.state` carries the provider's spelling verbatim.
            close, detail = self._close_and_record(
                record=record,
                reason=f"created pod arrived in state {record.state!r}, not RUNNING",
                store=store,
                owner_token=owner_token,
                situation=f"created pod reports state {record.state!r} rather than RUNNING",
            )
            return LaunchResult(
                LaunchState.REFUSED_RUNTIME_CONTRACT,
                preview_result.preview,
                record=record,
                lease_path=store.path,
                owner_token=owner_token,
                detail=detail,
                close_report=close,
            )
        actual, actual_preview = self._reassess_actual_price(
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
            preview=actual_preview,
            success_state=LaunchState.CREATED_GUARDED,
            price_note=price_move_note(
                preview_result.preview.assessment, actual_preview.assessment
            ),
        )

    def preview_adopt(
        self, pod_id: str, *, expected: PodCreateRequest, mint: bool = True
    ) -> LaunchResult:
        """Use the exact same shutdown proof, estimate display, and ceiling calculation.

        ``mint`` carries the same meaning as on ``preview_create``: only ``adopt``'s own
        re-assessment passes ``False``.
        """

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
        result = self._preview(
            "adopt",
            record.pod_id,
            record.estimate,
            expected.hard_deadline,
            expected.reviewed_digest(),
            mint=mint,
        )
        return LaunchResult(result.state, result.preview, record=record, detail=result.detail)

    def adopt(
        self,
        pod_id: str,
        *,
        expected: PodCreateRequest,
        confirmation: str | None,
    ) -> LaunchResult:
        """Serialize adoption with every other liability in the shared lease root."""

        try:
            with self._spend_gate_lock():
                return self._adopt_locked(
                    pod_id,
                    expected=expected,
                    confirmation=confirmation,
                )
        except OSError as error:
            return LaunchResult(
                LaunchState.REFUSED_BALANCE_UNOBSERVABLE,
                detail=(
                    "balance safety could not be established because the spend-reservation "
                    f"lock failed: {error}; no paid action occurred"
                ),
            )

    def _adopt_locked(
        self,
        pod_id: str,
        *,
        expected: PodCreateRequest,
        confirmation: str | None,
    ) -> LaunchResult:
        """No pod bills its way around the gate: adoption shares every create check."""

        expected = self._policy_bound_request(expected)
        preview_result = self.preview_adopt(pod_id, expected=expected, mint=False)
        if preview_result.state is not LaunchState.PREVIEW:
            return preview_result
        # Narrowing, not a check: `preview_adopt` returns `PREVIEW` from one line, and it
        # passes `_preview`'s preview and a non-None record on it. Neither needs `-O` off.
        assert preview_result.preview is not None and preview_result.record is not None
        if not preview_result.preview.assessment.allowed:
            # Same policy as the confirmation refusals: no live phrase on a
            # refusal report.
            return LaunchResult(
                _spend_refusal_state(preview_result.preview.assessment),
                phraseless(preview_result.preview),
                record=preview_result.record,
                detail="; ".join(preview_result.preview.assessment.reasons),
            )
        # The same single-live-pod refusal as `create`, in the same place: adoption is
        # the other way a second pod becomes this laptop's liability.
        open_lease = self._open_lease_refusal()
        if open_lease is not None:
            return LaunchResult(
                LaunchState.REFUSED_ACTIVE_LEASE,
                phraseless(preview_result.preview),
                record=preview_result.record,
                detail=open_lease,
            )
        # Same early exit as `create`, for the same reason.
        claimed = preview_result.preview.challenge is not None
        if claimed:
            try:
                claimed = self._claim_challenge(
                    "adopt",
                    preview_result.record.pod_id,
                    expected.hard_deadline,
                    expected.reviewed_digest(),
                    preview_result.preview.challenge or "",
                    preview_result.preview.confirmation_phrase,
                    confirmation,
                )
            except SpendRefusal as error:
                return LaunchResult(
                    LaunchState.REFUSED_CONFIRMATION,
                    phraseless(preview_result.preview),
                    record=preview_result.record,
                    detail=str(error),
                )
        if not claimed:
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION,
                phraseless(preview_result.preview),
                record=preview_result.record,
                detail=(
                    "no preview in this run issued a challenge for this adoption at this "
                    "hard deadline; run the preview and confirm the phrase it prints; "
                    "no paid action occurred"
                ),
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

    @contextmanager
    def _spend_gate_lock(self) -> Iterator[None]:
        """Serialize assessment through durable lease reservation across processes.

        The observed provider balance does not promise to reserve future charges.
        Holding this lease-root lock until the new lease exists ensures the next
        create/adopt assessment sees this action's maximum remaining liability.
        A process crash releases the OS lock while leaving the pending lease behind.

        It carries the single-live-pod invariant too. `_open_lease_refusal` runs
        inside this section, so between one caller reading the leases and writing
        its own, no second caller can read them: whoever loses the lock sees the
        winner's armed lease and refuses, rather than creating a second billing
        pod behind a descriptor pointer that can only name one of them.
        """

        # The lock is a sibling of the lease directory.  That lets an invalid
        # lease-root path proceed far enough to retain the existing, precise
        # LEASE_FAILURE handling (including confirmed-adoption close) while still
        # serializing every caller that names the same root.
        self.lease_root.parent.mkdir(parents=True, exist_ok=True)
        path = self.lease_root.parent / f".{self.lease_root.name}.spend-gate.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reserved_liability(
        self, *, now: datetime, exclude: Path | None = None
    ) -> tuple[Decimal, str | None]:
        """Conservatively total every locally known action still able to bill."""

        total = Decimal("0")
        try:
            paths = sorted(self.lease_root.glob("*.json"))
        except OSError as error:
            return total, f"the lease directory could not be listed: {error}"
        for path in paths:
            if exclude is not None and path == exclude:
                continue
            if path.is_symlink():
                return total, f"lease {path} is a symlink"
            try:
                lease = LeaseStore(path).load()
            except Exception as error:
                return total, f"lease {path} could not be read: {error}"
            if lease is None or lease.phase == "closed-verified":
                continue
            if lease.phase == "close-unverified":
                return (
                    total,
                    f"lease {path} has an unverified close and unknown remaining liability",
                )
            remaining = (lease.hard_deadline - now).total_seconds()
            if remaining <= 0:
                return total, f"lease {path} passed its hard deadline without a verified close"
            seconds = math.ceil(remaining)
            total += (
                (lease.pod_hourly_usd + lease.volume_hourly_usd) * Decimal(seconds) / Decimal(3600)
            )
        return total, None

    def _open_lease_refusal(self) -> str | None:
        """Name any other locally armed paid action that has no verified close yet.

        This is the single-live-pod invariant, read where it can actually hold:
        inside `_spend_gate_lock`, before this action arms a lease of its own, so
        every lease it finds belongs to someone else and there is nothing to
        exclude. `_reserved_liability` scans the same evidence for the money
        ceiling, and totalling it answers a different question -- two
        individually affordable pods clear every ceiling and still leave two pods
        billing behind one descriptor pointer that can only name one of them.
        That is the harm GOVERNANCE 8 and CLAUDE.md hard rule 2 exist to prevent,
        and the durable lock that already serializes assessment is what turns the
        check from a descriptor-timing race into a lock-covered fact.

        A lease that cannot be read refuses rather than being skipped, and says
        the weaker thing it actually knows: not that a pod is billing, but that
        this root could not be proved clear of one. `_reserved_liability` reaches
        the same files first today and refuses them at the balance floor, so that
        branch is the fail-closed half of a check whose primary reader is
        elsewhere -- kept because it costs nothing and because a later reordering
        of these gates must not be able to open the hole again.

        Returns the refusal detail, or ``None`` when nothing here is open.
        """

        try:
            paths = sorted(self.lease_root.glob("*.json"))
        except OSError as error:
            return _unproven_lease_root(f"the lease directory could not be listed: {error}")
        for path in paths:
            if path.is_symlink():
                return _unproven_lease_root(f"lease {path} is a symlink")
            try:
                lease = LeaseStore(path).load()
            except Exception as error:
                return _unproven_lease_root(f"lease {path} could not be read: {error}")
            if lease is None or lease.phase == "closed-verified":
                continue
            named = "" if lease.pod_id is None else f" for pod {lease.pod_id}"
            return _open_lease_found(f"lease {path} is {lease.phase}{named}")
        return None

    def _assess(
        self,
        estimate: PodEstimate,
        hard_deadline: datetime,
        *,
        exclude_lease: Path | None = None,
    ) -> SpendAssessment:
        observation, unavailable = self._observe_balance()
        # Timestamp after the source returns: an honest source normally stamps
        # during that call, and comparing it with a clock sampled beforehand can
        # misclassify ordinary microseconds of execution as future evidence.
        observed_now = self.now()
        reserved, reservation_error = self._reserved_liability(
            now=observed_now, exclude=exclude_lease
        )
        return assess_spend(
            self.spend_policy,
            estimate,
            requested_deadline=hard_deadline,
            now=observed_now,
            balance_observation=observation,
            balance_unavailable_detail=unavailable,
            reserved_liability_usd=reserved,
            balance_safety_unavailable_detail=reservation_error,
        )

    def _preview(
        self,
        action: str,
        subject: str,
        estimate: PodEstimate,
        hard_deadline: datetime,
        request_digest: str,
        *,
        mint: bool,
    ) -> LaunchResult:
        """Assess the price, and either issue a challenge or read the outstanding one.

        ``mint`` is the whole gate. A human-facing preview mints a fresh challenge and
        displays it. The re-assessment inside ``create``/``adopt`` passes ``False``, so it
        can only confirm against a challenge some earlier preview actually issued.

        ``request_digest`` is the reviewed request's own digest, recorded with the
        challenge here and checked again by ``_claim_challenge`` under the lock.
        """

        assessment = self._record_spend_notifications(
            self._assess(estimate, hard_deadline), action, subject
        )
        key = (action, subject)
        if mint:
            challenge = self.challenge_factory()
            if not challenge:
                raise ValueError("challenge factory returned no challenge")
            with self._challenge_lock:
                self._outstanding[key] = _OutstandingChallenge(
                    challenge, hard_deadline, request_digest
                )
        else:
            # The deadline is part of what was authorized, not merely part of what was
            # displayed. The phrase names the action, subject and both hourly rates, none
            # of which changes with lifetime -- so without this a ten-minute preview's
            # phrase would confirm a create running to the configured ceiling. The
            # ceiling still bounds the exposure; the operator's consent did not cover it.
            #
            # The rest of the request is checked in `_claim_challenge`, not here: that
            # divergence has its own refusal to name, and the check that decides belongs
            # under the same lock that consumes the challenge.
            with self._challenge_lock:
                held = self._outstanding.get(key)
            challenge = (
                held.challenge
                if held is not None and held.matches_deadline(hard_deadline)
                else None
            )
        return LaunchResult(
            LaunchState.PREVIEW, PaidActionPreview(action, subject, assessment, challenge)
        )

    def _record_spend_notifications(
        self, assessment: SpendAssessment, action: str, subject: str
    ) -> SpendAssessment:
        """Apply edge-aware warning state and retain every attempted delivery."""

        notifications: list[str] = []
        try:
            if assessment.alerts:
                for alert in assessment.alerts:
                    outcome = self._notify_spend_alert(alert, action, subject, assessment)
                    if outcome is not None:
                        notifications.append(outcome.line())
            else:
                self._record_nonalert_balance(action, subject, assessment)
        except Exception as error:  # notification state can never become a spend gate
            notifications.append(f"Phone notification: NOT DELIVERED ({error!r}).")
        if not notifications:
            return assessment
        return replace(assessment, alert_notifications=tuple(notifications))

    def _notify_spend_alert(
        self, alert: str, action: str, subject: str, assessment: SpendAssessment
    ) -> NotifyOutcome | None:
        """Send one new/expired alert episode; delivery never changes the gate."""

        observation = assessment.balance_observation
        if observation is None or not assessment.balance_observation_usable:
            return None
        path = self._spend_alert_stamp_path(action, subject)
        with self._alert_state_lock(path):
            state = self._load_spend_alert_state(path)
            if not self._spend_alert_due(state):
                if state is not None and state["safe_observations"]:
                    self._write_spend_alert_state(
                        path,
                        delivered_at=state["delivered_at"],
                        active=True,
                        safe_observations=0,
                    )
                return None
            message = (
                f"Spend warning: {alert}; {action} {subject}; observed available balance "
                f"${observation.available_usd}."
            )
            try:
                outcome = self.notifier(message)
            except Exception as error:  # a broken notifier is not a broken preview
                return NotifyOutcome(True, False, f"the notifier raised: {type(error).__name__}")
            # Only a delivered warning starts suppression. A failed send leaves
            # the episode due so the next gate retries rather than losing it.
            if getattr(outcome, "delivered", False):
                self._write_spend_alert_state(
                    path,
                    delivered_at=int(self.now().timestamp()),
                    active=True,
                    safe_observations=0,
                )
            return outcome

    def _spend_alert_stamp_path(self, action: str, subject: str) -> Path:
        digest = hashlib.sha256(f"{action}:{subject}".encode("utf-8")).hexdigest()[:16]
        return self.lease_root / f".spend-alert-{digest}.stamp"

    @contextmanager
    def _alert_state_lock(self, path: Path) -> Iterator[None]:
        self.lease_root.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_spend_alert_state(self, path: Path) -> dict[str, object] | None:
        """Read only a closed state shape; corrupt evidence reverts to sending."""

        try:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                return None
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        # Compatibility with the timestamp-only state written by the earlier seat.
        if re.fullmatch(r"-?\d+", text):
            raw: object = {
                "delivered_at": int(text),
                "active": True,
                "safe_observations": 0,
            }
        else:
            try:
                raw = json.loads(text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
        if not isinstance(raw, dict) or set(raw) != {
            "delivered_at",
            "active",
            "safe_observations",
        }:
            return None
        delivered_at = raw["delivered_at"]
        active = raw["active"]
        safe_observations = raw["safe_observations"]
        if (
            not isinstance(delivered_at, int)
            or isinstance(delivered_at, bool)
            or not isinstance(active, bool)
            or not isinstance(safe_observations, int)
            or isinstance(safe_observations, bool)
            or not 0 <= safe_observations <= SPEND_ALERT_RECOVERY_OBSERVATIONS
        ):
            return None
        try:
            datetime.fromtimestamp(delivered_at, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return raw

    def _spend_alert_due(self, state: dict[str, object] | None) -> bool:
        if state is None or state["active"] is False:
            return True
        try:
            stamped = datetime.fromtimestamp(int(state["delivered_at"]), tz=timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            return True
        age = (self.now() - stamped).total_seconds()
        return age < 0 or age >= SPEND_ALERT_DEBOUNCE_SECONDS

    def _record_nonalert_balance(
        self, action: str, subject: str, assessment: SpendAssessment
    ) -> None:
        observation = assessment.balance_observation
        policy = assessment.policy
        if (
            observation is None
            or not assessment.balance_observation_usable
            or not policy.configured
            or policy.account_balance_alert_usd is None
        ):
            return
        safe = observation.available_usd > policy.account_balance_alert_usd
        path = self._spend_alert_stamp_path(action, subject)
        with self._alert_state_lock(path):
            state = self._load_spend_alert_state(path)
            if state is None or state["active"] is False:
                return
            safe_observations = int(state["safe_observations"])
            safe_observations = min(
                SPEND_ALERT_RECOVERY_OBSERVATIONS,
                safe_observations + 1 if safe else 0,
            )
            self._write_spend_alert_state(
                path,
                delivered_at=int(state["delivered_at"]),
                active=safe_observations < SPEND_ALERT_RECOVERY_OBSERVATIONS,
                safe_observations=safe_observations,
            )

    def _write_spend_alert_state(
        self, path: Path, *, delivered_at: int, active: bool, safe_observations: int
    ) -> None:
        try:
            atomic_write(
                path,
                canonical_json(
                    {
                        "delivered_at": delivered_at,
                        "active": active,
                        "safe_observations": safe_observations,
                    }
                ),
            )
        except OSError:
            # Notification state is advisory. Losing it can cause one duplicate,
            # never a blocked action or a swallowed warning.
            pass

    def _observe_balance(self) -> tuple[AccountBalanceObservation | None, str | None]:
        """Observe the balance at every spend gate; unavailable is fail-closed.

        Returns the observation and, when there is none, why -- a swallowed
        provider error leaves an operator staring at "not observed" with no way
        to tell a missing configured source from a timeout (GOVERNANCE 2).
        """

        if self.balance_source is None:
            return None, "no account-balance source is configured for this provider"
        try:
            return self.balance_source.observe_account_balance(), None
        except Exception as error:
            return None, f"{type(error).__name__}: {error}"

    def _claim_challenge(
        self,
        action: str,
        subject: str,
        hard_deadline: datetime,
        request_digest: str,
        challenge: str,
        expected: str,
        typed: str | None,
    ) -> bool:
        """Verify and consume one challenge as a single atomic step.

        Reading the challenge, checking the typed phrase, and consuming it used to be
        three separate operations. Two overlapping calls could therefore both read the
        same outstanding challenge, both pass the check, and both create a billing pod
        from one confirmation -- and a sequential test cannot see that.

        Everything that decides the outcome happens under the lock, and the entry is
        removed before this returns, so exactly one caller can ever win a given
        challenge. A wrong phrase raises without consuming: a typo must not burn the
        preview, and holding the lock makes that safe. A request that diverges from the
        reviewed one raises without consuming for the same reason -- the preview the
        operator was actually shown is still theirs to confirm.

        ``challenge`` is the one the caller's ``expected`` phrase was built from, read
        outside this lock. Comparing it here is what makes ``expected`` trustworthy: a
        preview minted between that read and this call replaces the entry, and without
        this check the stale phrase would still be accepted while the fresh challenge
        was deleted unused -- then reported to the window holding it as "no preview in
        this run issued a challenge", which is false. Refusing without consuming leaves
        the newer preview confirmable, which is the whole point of naming it.

        Returns ``False`` when no challenge matches this action, subject and deadline.
        Raises ``SpendRefusal`` when one exists but the preview, the request or the
        typed phrase does not match it.
        """

        with self._challenge_lock:
            held = self._outstanding.get((action, subject))
            if held is None or not held.matches_deadline(hard_deadline):
                return False
            if held.challenge != challenge:
                raise SpendRefusal(
                    "a newer preview replaced the one this confirmation was issued for: "
                    "a confirmation is valid only for the preview that issued it -- read "
                    "the price the newer preview printed and type its phrase; the newer "
                    "preview is untouched and no paid action occurred"
                )
            if held.request_digest != request_digest:
                raise SpendRefusal(
                    "typed confirmation authorizes a different request than this one: the "
                    "phrase names only the action, the subject and the two hourly rates, "
                    "and the rest of the request changed after the preview that issued "
                    "this challenge -- preview the request you mean and type its phrase; "
                    "no paid action occurred"
                )
            require_confirmation(typed, expected)
            del self._outstanding[(action, subject)]
            return True

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
        # Narrowing, not a check: `SpendPolicy.__post_init__` raises `SpendRefusal` on a
        # configured policy missing any ceiling, and a `raise` survives `-O`.
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
        price_note: str = "",
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
                        f"{price_note}"
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
    ) -> tuple[LaunchResult | None, PaidActionPreview]:
        """Return the actual-price assessment even when it remains allowed.

        Adoption already assesses the observed record, so only `create` needs
        this: it gates on `estimate()` before the pod exists, and the price the
        provider returns afterwards is the one that bills.
        """

        assessment = self._record_spend_notifications(
            self._assess(
                record.estimate,
                request.hard_deadline,
                exclude_lease=store.path,
            ),
            action,
            record.pod_id if action == "adopt" else preview.subject,
        )
        actual_preview = PaidActionPreview(preview.action, preview.subject, assessment)
        if assessment.allowed:
            return None, actual_preview
        # No challenge: this preview reports why a created pod is being closed, and its
        # challenge was consumed by the create that got here. A refusal report must not
        # carry a phrase that would authorize anything.
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
        return (
            LaunchResult(
                _spend_refusal_state(assessment),
                actual_preview,
                record=record,
                lease_path=store.path,
                owner_token=owner_token,
                detail=detail,
                close_report=close,
            ),
            actual_preview,
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
