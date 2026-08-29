"""Gated pod creation and adoption, backed by the verified-shutdown path."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import re
import secrets
import threading
import time
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


def _phraseless(preview: PaidActionPreview | None) -> PaidActionPreview | None:
    """The same preview with its challenge withheld.

    A refused confirmation is printed and logged, and the challenge it carries
    is still spendable -- a typo deliberately does not burn the preview.  The
    refusal report therefore must not carry a phrase that would authorize
    anything. The operator retypes from the preview they were shown.

    On the returned preview the `confirmation_phrase` property raises rather
    than returning a string; callers rendering a refusal read `to_record()`,
    which shows the phrase as absent instead.
    """

    if preview is None or preview.challenge is None:
        return preview
    return PaidActionPreview(preview.action, preview.subject, preview.assessment)


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

MAX_SPEND_ALERT_STATE_BYTES: Final = 4096
"""How much of a debounce stamp is read before it is called corrupt.

The stamp this package writes is about sixty bytes.  It is read back from a
directory a paid run writes to, so its size is untrusted input like its content:
without a bound, one oversized file is read into memory at every spend gate, and
one *long* file defeats the corrupt-reverts-to-sending rule outright, because
``int`` refuses a decimal string past ``sys.int_max_str_digits`` with a
``ValueError`` rather than a value.  A stamp that cannot be believed must send.
"""


SPEND_LOCK_WAIT_SECONDS: Final = 30.0
"""How long either cross-process lock may be waited for before it is a failure.

``operations/pod/`` forbids unbounded waits on the money path, and a blocking
``flock`` is exactly that: one holder that never finishes -- a provider call
hung inside the spend gate, or a crashed process on a filesystem that keeps the
lock -- would leave the next operator with no output at all rather than a named
refusal.

What this bound is *not*: a duration chosen to let every ordinary overlap wait
and win. ``create`` and ``adopt`` hold this lock around the whole of
``_create_locked``/``_adopt_locked``, which includes the provider call,
controller arming, and any ``_close_and_record`` -- and a close polls to
``shutdown_deadline_seconds`` and then retries billing reconciliation, so a
holder can legitimately keep it for well over thirty seconds during an entirely
ordinary failed launch. A second caller that waits this long is refused by name
*while an earlier launch is still unresolved*, and that is the intended outcome
rather than an accident of the number: two paid actions overlapping on one
account balance is the state this lock exists to prevent. Nothing is spent, and
the refusal names the lock.
"""

SPEND_LOCK_POLL_SECONDS: Final = 0.05

ALERT_STATE_LOCK_WAIT_SECONDS: Final = 5.0
"""The debounce stamp's own bound, deliberately shorter than the spend gate's.

This lock guards notification bookkeeping and is taken *inside* the spend gate on
the create/adopt path.  A notification-only feature may never hold a paid action
open, so it gives up early; ``_record_spend_notifications`` then records that the
warning was not delivered, which is the outcome GOVERNANCE 2 asks for and is not
a refusal.
"""


def _acquire_bounded(handle, *, deadline_seconds: float, sleep: Callable[[float], None]) -> None:
    """Take an exclusive lock, or raise rather than wait for it forever."""

    limit = time.monotonic() + deadline_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            if time.monotonic() >= limit:
                raise TimeoutError(
                    f"another process still holds this lock after {deadline_seconds:g} seconds"
                ) from error
            sleep(SPEND_LOCK_POLL_SECONDS)


class _SpendGateLockFailure(OSError):
    """The reservation lock could not be acquired before a paid action."""


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
        lock_sleeper: Callable[[float], None] = time.sleep,
        lock_wait_seconds: float = SPEND_LOCK_WAIT_SECONDS,
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
        self.lock_sleeper = lock_sleeper
        self.lock_wait_seconds = lock_wait_seconds
        # Outstanding preview challenges, keyed by (action, subject), each holding the
        # challenge and the hard deadline it was assessed against. In-memory and
        # per-process on purpose: a challenge that outlived the run would be exactly the
        # replayable credential this gate exists to refuse. The lock makes claiming one
        # atomic, so overlapping callers cannot both spend the same confirmation.
        self._outstanding: dict[tuple[str, str], tuple[str, datetime]] = {}
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
        return self._preview("create", request.name, estimate, request.hard_deadline, mint=mint)

    def create(self, request: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        """Serialize the shared lease-root assessment through durable reservation."""

        try:
            with self._spend_gate_lock():
                return self._create_locked(request, confirmation=confirmation)
        except _SpendGateLockFailure as error:
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
                _phraseless(preview_result.preview),
                detail="; ".join(preview_result.preview.assessment.reasons),
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
                    preview_result.preview.confirmation_phrase,
                    confirmation,
                )
            except SpendRefusal as error:
                return LaunchResult(
                    LaunchState.REFUSED_CONFIRMATION,
                    _phraseless(preview_result.preview),
                    detail=str(error),
                )
        if not claimed:
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION,
                _phraseless(preview_result.preview),
                detail=(
                    "no preview in this run issued a challenge for this create at this "
                    "hard deadline; run the preview and confirm the phrase it prints; "
                    "no paid action occurred"
                ),
            )

        try:
            lease_id = self._token("lease id")
            owner_token = self._token("lease owner token")
        except Exception as error:
            return LaunchResult(
                LaunchState.LEASE_FAILURE,
                preview_result.preview,
                detail=f"could not mint lease identity: {error}",
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
            "adopt", record.pod_id, record.estimate, expected.hard_deadline, mint=mint
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
        except _SpendGateLockFailure as error:
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
                _phraseless(preview_result.preview),
                record=preview_result.record,
                detail="; ".join(preview_result.preview.assessment.reasons),
            )
        # Same early exit as `create`, for the same reason.
        claimed = preview_result.preview.challenge is not None
        if claimed:
            try:
                claimed = self._claim_challenge(
                    "adopt",
                    preview_result.record.pod_id,
                    expected.hard_deadline,
                    preview_result.preview.confirmation_phrase,
                    confirmation,
                )
            except SpendRefusal as error:
                return LaunchResult(
                    LaunchState.REFUSED_CONFIRMATION,
                    _phraseless(preview_result.preview),
                    record=preview_result.record,
                    detail=str(error),
                )
        if not claimed:
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION,
                _phraseless(preview_result.preview),
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
        except Exception as error:
            return LaunchResult(
                LaunchState.LEASE_FAILURE,
                preview_result.preview,
                record=preview_result.record,
                detail=f"could not mint lease identity: {error}",
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
        """

        # The lock is a sibling of the lease directory.  That lets an invalid
        # lease-root path proceed far enough to retain the existing, precise
        # LEASE_FAILURE handling (including confirmed-adoption close) while still
        # serializing every caller that names the same root.
        handle = None
        try:
            lock_root = self.lease_root.resolve(strict=False)
            lock_root.parent.mkdir(parents=True, exist_ok=True)
            path = lock_root.parent / f".{lock_root.name}.spend-gate.lock"
            handle = path.open("a+b")
            # Bounded, never blocking: a holder that never finishes must end as a
            # named balance-safety refusal, not as a create with no output at all.
            _acquire_bounded(
                handle,
                deadline_seconds=self.lock_wait_seconds,
                sleep=self.lock_sleeper,
            )
        except (OSError, RuntimeError) as error:
            if handle is not None:
                handle.close()
            raise _SpendGateLockFailure(str(error)) from error
        try:
            yield
        finally:
            # Closing the descriptor releases flock. There is no separate
            # unlock operation whose failure can overwrite a result after the
            # provider has already created or adopted a billing pod.
            handle.close()

    def _reserved_liability(
        self, *, now: datetime, exclude: Path | None = None
    ) -> tuple[Decimal, str | None]:
        """Conservatively total every locally known action still able to bill."""

        total = Decimal("0")
        try:
            paths = sorted(self.lease_root.glob("*.json"))
        except Exception as error:
            return total, f"the lease directory could not be listed: {error}"
        for path in paths:
            if exclude is not None and path == exclude:
                continue
            # The link test is inside the same guard as the read.  `is_symlink`
            # re-raises a permission error rather than answering False, and an
            # unreadable lease directory therefore used to throw out of the paid
            # gate instead of refusing through it -- past `create`'s own
            # `_SpendGateLockFailure` catch, and past the post-create
            # re-assessment that closes a pod it will not authorize.  Whatever
            # stops this file being read leaves the remaining liability unknown,
            # which is one answer with one name.
            # Cause first here too, and for the two phase refusals below. The
            # reason is truncated at 160 characters and a lease path is as long
            # as its root: putting the path first pushed "could not be read" off
            # the end on an ordinary macOS temporary directory, leaving an
            # operator -- and the test that names this cause -- with a bare path
            # and no diagnosis.
            try:
                if path.is_symlink():
                    return total, f"a lease is a symlink: {path}"
                lease = LeaseStore(path).load()
            except Exception as error:
                return total, f"a lease could not be read: {path}: {error}"
            if lease is None:
                # `glob` listed this path, so something was accounted for here a
                # moment ago and is now gone.  Excluding it would silently drop a
                # liability that may still be billing, which is the one answer this
                # total is not allowed to give.
                # Cause first: the reason is truncated at 160 characters, and a long
                # lease path would otherwise push the only diagnosis off the end.
                return total, f"a listed lease vanished before it could be read: {path}"
            if lease.phase == "closed-verified":
                continue
            if lease.phase == "close-unverified":
                return (
                    total,
                    f"a lease has an unverified close and unknown remaining liability: {path}",
                )
            remaining = (lease.hard_deadline - now).total_seconds()
            if remaining <= 0:
                return total, f"a lease passed its hard deadline without a verified close: {path}"
            seconds = math.ceil(remaining)
            total += (
                (lease.pod_hourly_usd + lease.volume_hourly_usd) * Decimal(seconds) / Decimal(3600)
            )
        return total, None

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
        *,
        mint: bool,
    ) -> LaunchResult:
        """Assess the price, and either issue a challenge or read the outstanding one.

        ``mint`` is the whole gate. A human-facing preview mints a fresh challenge and
        displays it. The re-assessment inside ``create``/``adopt`` passes ``False``, so it
        can only confirm against a challenge some earlier preview actually issued.
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
                self._outstanding[key] = (challenge, hard_deadline)
        else:
            # The deadline is part of what was authorized, not merely part of what was
            # displayed. The phrase names the action, subject and both hourly rates, none
            # of which changes with lifetime -- so without this a ten-minute preview's
            # phrase would confirm a create running to the configured ceiling. The
            # ceiling still bounds the exposure; the operator's consent did not cover it.
            with self._challenge_lock:
                held = self._outstanding.get(key)
            challenge = held[0] if held is not None and held[1] == hard_deadline else None
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
                    outcome, stamp_error = self._notify_spend_alert(
                        alert, action, subject, assessment
                    )
                    if outcome is not None:
                        notifications.append(outcome.line())
                    if stamp_error is not None:
                        notifications.append(
                            "Debounce state: NOT RECORDED "
                            f"({stamp_error}). The same warning may be sent again."
                        )
            else:
                stamp_error = self._record_nonalert_balance(action, subject, assessment)
                if stamp_error is not None:
                    notifications.append(
                        "Phone notification recovery state: NOT RECORDED "
                        f"({stamp_error}). No notification was due."
                    )
        except Exception as error:  # notification state can never become a spend gate
            if assessment.alerts:
                notifications.append(f"Phone notification: NOT DELIVERED ({error!r}).")
            else:
                notifications.append(
                    "Phone notification recovery state: NOT RECORDED "
                    f"({type(error).__name__}). No notification was due."
                )
        if not notifications:
            return assessment
        return replace(assessment, alert_notifications=tuple(notifications))

    def _notify_spend_alert(
        self, alert: str, action: str, subject: str, assessment: SpendAssessment
    ) -> tuple[NotifyOutcome | None, str | None]:
        """Send one new/expired alert episode; delivery never changes the gate.

        The second element names a debounce stamp that could not be written, so a
        duplicate warning on the next gate has a recorded cause rather than none.
        """

        observation = assessment.balance_observation
        if observation is None or not assessment.balance_observation_usable:
            return None, None
        path = self._spend_alert_stamp_path(action, subject)
        with self._alert_state_lock(path):
            state = self._load_spend_alert_state(path)
            due = state is None or state["active"] is False
            if not due:
                stamped = datetime.fromtimestamp(int(state["delivered_at"]), tz=timezone.utc)
                age = (self.now() - stamped).total_seconds()
                due = age < 0 or age >= SPEND_ALERT_DEBOUNCE_SECONDS
            if not due:
                stamp_error = None
                if state is not None and state["safe_observations"]:
                    stamp_error = self._write_spend_alert_state(
                        path,
                        delivered_at=state["delivered_at"],
                        active=True,
                        safe_observations=0,
                    )
                return (
                    NotifyOutcome(
                        False,
                        False,
                        "a delivered warning for this action and subject is still inside "
                        "the debounce window",
                    ),
                    stamp_error,
                )
            message = (
                f"Spend warning: {alert}; {action} {subject}; observed available balance "
                f"${observation.available_usd}."
            )
            try:
                outcome = self.notifier(message)
            except Exception as error:  # a broken notifier is not a broken preview
                return (
                    NotifyOutcome(True, False, f"the notifier raised: {type(error).__name__}"),
                    None,
                )
            # Only a delivered warning starts suppression. A failed send leaves
            # the episode due so the next gate retries rather than losing it.
            stamp_error = None
            if getattr(outcome, "delivered", False):
                stamp_error = self._write_spend_alert_state(
                    path,
                    delivered_at=int(self.now().timestamp()),
                    active=True,
                    safe_observations=0,
                )
            return outcome, stamp_error

    def _spend_alert_stamp_path(self, action: str, subject: str) -> Path:
        digest = hashlib.sha256(f"{action}:{subject}".encode("utf-8")).hexdigest()[:16]
        return self.lease_root / f".spend-alert-{digest}.stamp"

    @contextmanager
    def _alert_state_lock(self, path: Path) -> Iterator[None]:
        # This creates the lease root, and `_record_nonalert_balance` deliberately
        # does not -- an asymmetry worth stating, because the next reader will see
        # one guard and remove the other. A delivered warning has to be remembered
        # across processes or every later preview pages the phone again, so the
        # alert path must be able to write its stamp. The safe path never reaches
        # here: it checks the stamp already exists first, which is what keeps a
        # preview that has nothing to warn about from creating anything at all.
        self.lease_root.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
            _acquire_bounded(
                handle,
                deadline_seconds=min(ALERT_STATE_LOCK_WAIT_SECONDS, self.lock_wait_seconds),
                sleep=self.lock_sleeper,
            )
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_spend_alert_state(self, path: Path) -> dict[str, object] | None:
        """Read only a closed state shape; corrupt evidence reverts to sending."""

        try:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                return None
            with path.open("rb") as handle:
                blob = handle.read(MAX_SPEND_ALERT_STATE_BYTES + 1)
        except OSError:
            return None
        if len(blob) > MAX_SPEND_ALERT_STATE_BYTES:
            return None
        try:
            text = blob.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        # Existing state files may contain only the original Unix timestamp.
        if re.fullmatch(r"-?\d+", text):
            try:
                legacy = int(text)
            except ValueError:
                # `sys.int_max_str_digits` is an interpreter setting, not this
                # module's to assume, so the bound above is not the only reason
                # a digit string can refuse to become a number.
                return None
            raw: object = {
                "delivered_at": legacy,
                "active": True,
                "safe_observations": 0,
            }
        else:
            try:
                raw = json.loads(text)
            except ValueError:
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

    def _record_nonalert_balance(
        self, action: str, subject: str, assessment: SpendAssessment
    ) -> str | None:
        """Advance recovery state; return why the stamp was not written, if it was not."""

        observation = assessment.balance_observation
        policy = assessment.policy
        if (
            observation is None
            or not assessment.balance_observation_usable
            or not policy.configured
            or policy.account_balance_alert_usd is None
        ):
            return None
        safe = observation.available_usd > policy.account_balance_alert_usd
        path = self._spend_alert_stamp_path(action, subject)
        try:
            path.lstat()
        except FileNotFoundError:
            # No delivered low-balance episode exists to re-arm. In particular,
            # a safe preview should not create the lease directory or a lock file.
            return None
        with self._alert_state_lock(path):
            state = self._load_spend_alert_state(path)
            if state is None or state["active"] is False:
                return None
            safe_observations = int(state["safe_observations"])
            safe_observations = min(
                SPEND_ALERT_RECOVERY_OBSERVATIONS,
                safe_observations + 1 if safe else 0,
            )
            return self._write_spend_alert_state(
                path,
                delivered_at=int(state["delivered_at"]),
                active=safe_observations < SPEND_ALERT_RECOVERY_OBSERVATIONS,
                safe_observations=safe_observations,
            )

    def _write_spend_alert_state(
        self, path: Path, *, delivered_at: int, active: bool, safe_observations: int
    ) -> str | None:
        """Persist the debounce stamp; return why it was not written, if it was not.

        Notification state is advisory: losing it can cause one duplicate warning,
        never a blocked action or a swallowed one, so a failure here is not a
        refusal.  It is still evidence, and a receipt that reads only ``sent``
        while the stamp is missing leaves the next duplicate unexplained.
        """

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
        except OSError as error:
            return f"{type(error).__name__}: {error}"
        return None

    def _observe_balance(self) -> tuple[AccountBalanceObservation | None, str | None]:
        """Observe the balance at every spend gate; unavailable is fail-closed.

        Returns the observation and, when there is none, why -- a swallowed
        provider error leaves an operator staring at "not observed" with no way
        to tell a missing configured source from a timeout (GOVERNANCE 2).
        """

        if self.balance_source is None:
            return None, "no account-balance source is configured for this provider"
        try:
            observation = self.balance_source.observe_account_balance()
        except Exception as error:
            return None, f"{type(error).__name__}: {error}"
        if not isinstance(observation, AccountBalanceObservation):
            return (
                None,
                "account-balance source returned "
                f"{type(observation).__name__}, not AccountBalanceObservation",
            )
        return observation, None

    def _claim_challenge(
        self, action: str, subject: str, hard_deadline: datetime, expected: str, typed: str | None
    ) -> bool:
        """Verify and consume one challenge as a single atomic step.

        Reading the challenge, checking the typed phrase, and consuming it used to be
        three separate operations. Two overlapping calls could therefore both read the
        same outstanding challenge, both pass the check, and both create a billing pod
        from one confirmation -- and a sequential test cannot see that.

        Everything that decides the outcome happens under the lock, and the entry is
        removed before this returns, so exactly one caller can ever win a given
        challenge. A wrong phrase raises without consuming: a typo must not burn the
        preview, and holding the lock makes that safe.

        Returns ``False`` when no challenge matches this action, subject and deadline.
        Raises ``SpendRefusal`` when one exists but the typed phrase does not match it.
        """

        with self._challenge_lock:
            held = self._outstanding.get((action, subject))
            if held is None or held[1] != hard_deadline:
                return False
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
    ) -> tuple[LaunchResult | None, PaidActionPreview]:
        """Return the post-create spend assessment even when it remains allowed.

        Adoption already assesses the observed record, so only `create` needs
        this: it gates on `estimate()` before the pod exists, and the price the
        provider returns afterwards is the one that bills.
        """

        subject = record.pod_id if action == "adopt" else preview.subject
        try:
            assessment = self._record_spend_notifications(
                self._assess(
                    record.estimate,
                    request.hard_deadline,
                    exclude_lease=store.path,
                ),
                action,
                subject,
            )
        except Exception as error:
            # Everything this assessment touches already fails closed with a
            # named reason, so nothing here is expected to raise.  But a pod is
            # billing by this line, and an escaping exception is the one outcome
            # that leaves it running with no close attempted and no result to
            # read.  An assessment that could not be completed is exactly the
            # balance-safety fact that could not be established.
            close, detail = self._close_and_record(
                record=record,
                reason=f"{action} post-create spend assessment could not be completed: {error}",
                store=store,
                owner_token=owner_token,
                situation=(
                    "created pod's post-create spend assessment could not be completed "
                    f"({type(error).__name__}: {error})"
                ),
            )
            # The pre-create assessment, carried without its challenge: it is the
            # last one that actually ran, and the state and detail say plainly
            # that the one after the pod existed did not.
            unassessed = PaidActionPreview(preview.action, preview.subject, preview.assessment)
            return (
                LaunchResult(
                    LaunchState.REFUSED_BALANCE_UNOBSERVABLE,
                    unassessed,
                    record=record,
                    lease_path=store.path,
                    owner_token=owner_token,
                    detail=detail,
                    close_report=close,
                ),
                unassessed,
            )
        actual_preview = PaidActionPreview(preview.action, preview.subject, assessment)
        if assessment.allowed:
            return None, actual_preview
        # No challenge: this preview reports why a created pod is being closed, and its
        # challenge was consumed by the create that got here. A refusal report must not
        # carry a phrase that would authorize anything.
        close, detail = self._close_and_record(
            record=record,
            reason=(
                f"{action} post-create spend assessment refused: " + "; ".join(assessment.reasons)
            ),
            store=store,
            owner_token=owner_token,
            situation=(
                "created pod failed its post-create spend assessment ("
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
