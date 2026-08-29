"""Strict, intentionally unconfigured spend policy and shared paid-action gate."""

from __future__ import annotations

import math
import secrets
import tomllib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path

from .models import (
    AccountBalanceObservation,
    PodEstimate,
    SpendRefusal,
    as_decimal,
    require_billing_cutoff_margin_seconds,
    require_utc,
)
from .shutdown import BILLING_RECONCILIATION_ATTEMPTS, BILLING_RECONCILIATION_RETRY_SECONDS

SPEND_SCHEMA = "pod-spend.v3"

RETIRED_SPEND_SCHEMAS = {
    "pod-spend.v2": (
        "a configured pod-spend.v2 policy predates the required "
        "account_balance_alert_usd warning threshold"
    ),
}
"""Schemas this loader once accepted, and what changed under each name.

``account_balance_alert_usd`` became a required ceiling, so a file that was a
valid configured v2 policy is now an incomplete v3 one. Left at the same schema
name, that file failed as "missing a required ceiling" and blamed the operator's
configuration for a change in this code. The version identifier is what tells
those two apart, so it moves when the required shape moves.
"""
MAX_BALANCE_OBSERVATION_AGE_SECONDS = 60
"""A gate may use only a current observation, never an indefinitely cached balance."""

CONFIRMATION_PREFIX = "I CONFIRM PAID POD"
"""The fixed opening of the typed phrase; the rest names one action and one challenge.

A constant phrase can be typed from memory without referring to one action.
CLAUDE.md's hard rule 1 reserves paid actions to Tyrel and requires the exact action to
be named, so this phrase names the action, subject, and both displayed hourly rates.

Those four values are all derivable from the price sheet, so they alone proved only that
the caller knew the price -- not that anyone had been shown a preview. The phrase
therefore also carries a challenge that only a preview issued in this process can supply.

What that does and does not establish, stated plainly because GOVERNANCE 10 forbids
claiming more than was measured: it binds the confirmation to a preview produced in this
run, and makes the phrase unguessable and single-use. It is still not proof of *Tyrel's*
identity -- nothing local can supply that -- so GOVERNANCE 8 continues to rest on his
permission in the session, with this gate refusing everything that never saw a preview.
"""

CHALLENGE_BYTES = 8
"""Wide enough that a phrase cannot be guessed; short enough to retype from a screen."""


def mint_challenge() -> str:
    """A fresh, unpredictable challenge for exactly one preview."""

    return secrets.token_hex(CHALLENGE_BYTES).upper()


def confirmation_phrase(
    action: str,
    subject: str,
    pod_hourly_usd: Decimal,
    volume_hourly_usd: Decimal,
    challenge: str,
) -> str:
    """The exact text an operator must type back for this one paid action."""

    if not challenge:
        raise SpendRefusal("a paid action cannot be confirmed without a preview challenge")
    return (
        f"{CONFIRMATION_PREFIX} {action} {subject} "
        f"AT ${pod_hourly_usd}/HR PLUS ${volume_hourly_usd}/HR VOLUME "
        f"CHALLENGE {challenge}"
    )


@dataclass(frozen=True, slots=True)
class SpendPolicy:
    """Ceilings Tyrel configures; the checked-in policy intentionally has none.

    ``max_hourly_usd`` and ``max_estimated_metered_cost_usd`` apply to all
    launch-time metering: the pod plus its attached volume. Ongoing volume
    retention or deletion after close remains a separately authorized decision.

    ``account_balance_floor_usd`` is the hard observed-balance stop. It is a
    reserve that must *remain* available after the action being authorized has
    run to its hard deadline, so the floor is tested against the observed
    balance both now and net of this action's own estimated cost plus every
    locally reserved paid-action liability -- a reserve concurrent runs may
    spend through is not a reserve. The documented
    ``"50.00"`` config-template default is unverified and must be checked
    against RunPod before a live run. ``account_balance_alert_usd`` is a higher
    warning threshold: it never blocks a paid action.
    """

    state: str
    max_hourly_usd: Decimal | None = None
    max_estimated_metered_cost_usd: Decimal | None = None
    # "$50.00" is unverified and must be checked against RunPod before a live run.
    account_balance_floor_usd: Decimal | None = None
    account_balance_alert_usd: Decimal | None = None
    hard_lifetime_seconds: int | None = None
    laptop_heartbeat_timeout_seconds: int | None = None
    shutdown_poll_interval_seconds: int | None = None
    shutdown_deadline_seconds: int | None = None
    billing_cutoff_margin_seconds: int | None = None

    @property
    def configured(self) -> bool:
        return self.state == "configured"

    def __post_init__(self) -> None:
        if self.state not in {"unconfigured", "configured"}:
            raise SpendRefusal("spend state must be 'unconfigured' or 'configured'")
        values = (
            self.max_hourly_usd,
            self.max_estimated_metered_cost_usd,
            self.account_balance_floor_usd,
            self.account_balance_alert_usd,
            self.hard_lifetime_seconds,
            self.laptop_heartbeat_timeout_seconds,
            self.shutdown_poll_interval_seconds,
            self.shutdown_deadline_seconds,
            self.billing_cutoff_margin_seconds,
        )
        if not self.configured:
            if any(value is not None for value in values):
                raise SpendRefusal("unconfigured spend policy cannot carry latent paid ceilings")
            return
        if any(value is None for value in values):
            raise SpendRefusal("configured spend policy is missing a required ceiling")
        object.__setattr__(self, "max_hourly_usd", as_decimal(self.max_hourly_usd, "max hourly"))
        object.__setattr__(
            self,
            "max_estimated_metered_cost_usd",
            as_decimal(self.max_estimated_metered_cost_usd, "max estimated metered cost"),
        )
        object.__setattr__(
            self,
            "account_balance_floor_usd",
            as_decimal(self.account_balance_floor_usd, "account balance floor"),
        )
        object.__setattr__(
            self,
            "account_balance_alert_usd",
            as_decimal(self.account_balance_alert_usd, "account balance alert"),
        )
        if self.account_balance_alert_usd <= 0:
            raise SpendRefusal("account balance alert must be positive")
        if (
            self.max_hourly_usd <= 0
            or self.max_estimated_metered_cost_usd <= 0
            or self.account_balance_floor_usd <= 0
        ):
            raise SpendRefusal("money ceilings and account-balance floor must be positive")
        if self.account_balance_alert_usd <= self.account_balance_floor_usd:
            raise SpendRefusal("account balance alert must be above the hard floor")
        for label, value in (
            ("hard lifetime", self.hard_lifetime_seconds),
            ("laptop heartbeat timeout", self.laptop_heartbeat_timeout_seconds),
            ("shutdown poll interval", self.shutdown_poll_interval_seconds),
            ("shutdown deadline", self.shutdown_deadline_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SpendRefusal(f"{label} must be a positive integer")
        if self.laptop_heartbeat_timeout_seconds >= self.hard_lifetime_seconds:
            raise SpendRefusal("laptop heartbeat timeout must be shorter than hard lifetime")
        if self.shutdown_poll_interval_seconds > self.shutdown_deadline_seconds:
            raise SpendRefusal("shutdown poll interval cannot exceed the shutdown deadline")
        # One laptop-side close may run to the configured deadline plus the
        # fixed-in-code billing reconciliation tail (the shared symbols keep
        # this bound honest if that schedule ever changes).  This bounds only
        # what it names: a single close on the controller that reads this
        # policy.  The pod-side timer builds its shutdown from code defaults,
        # not this policy, and may re-attempt a bounded number of closes --
        # both facts are recorded in the audit note rather than claimed here.
        # Ceil, not int: a fractional retry interval must round the bound up,
        # never silently shave it.
        billing_tail = math.ceil(
            (BILLING_RECONCILIATION_ATTEMPTS - 1) * BILLING_RECONCILIATION_RETRY_SECONDS
        )
        if self.shutdown_deadline_seconds + billing_tail >= self.hard_lifetime_seconds:
            raise SpendRefusal(
                f"shutdown deadline plus the fixed {billing_tail}s billing-retry tail "
                "must be shorter than hard lifetime"
            )
        try:
            object.__setattr__(
                self,
                "billing_cutoff_margin_seconds",
                require_billing_cutoff_margin_seconds(
                    self.billing_cutoff_margin_seconds, "billing cutoff margin"
                ),
            )
        except ValueError as error:
            raise SpendRefusal(str(error)) from error


class SpendRefusalCause(StrEnum):
    """Why a spend assessment refused, recorded where the reason is raised.

    ``launch._spend_refusal_state`` turns this into the ``LaunchState`` an
    operator reads, and it used to derive it by matching the text of
    ``reasons``. Reflowing one of those strings -- fixing a typo, rewrapping a
    line -- silently reclassified a money-safety refusal as a price-ceiling one,
    and no test could catch it because the tests assert on the same prose. The
    wording is for people; this is what the code decides on.
    """

    HARD_FLOOR = "hard-floor"
    BALANCE_UNOBSERVABLE = "balance-unobservable"


@dataclass(frozen=True, slots=True)
class SpendAssessment:
    """The exact ceilings and current estimate shown at both paid-action gates."""

    allowed: bool
    reasons: tuple[str, ...]
    estimate: PodEstimate
    requested_lifetime_seconds: int
    estimated_pod_cost_usd: Decimal
    estimated_volume_cost_usd: Decimal
    estimated_total_cost_usd: Decimal
    policy: SpendPolicy
    balance_observation: AccountBalanceObservation | None = None
    reserved_liability_usd: Decimal = Decimal("0")
    refusal_causes: frozenset[SpendRefusalCause] = frozenset()
    alerts: tuple[str, ...] = ()
    alert_notifications: tuple[str, ...] = ()
    """What the notification seam actually did with each alert above.

    ``operations/notify/README.md`` requires a failed send to be said out loud
    rather than swallowed, and GOVERNANCE 2 forbids losing it behind a
    successful result.  A warning is notification-only, so its delivery never
    changes ``allowed`` -- but whether the phone got it is part of the record.
    """

    @property
    def hard_floor_triggered(self) -> bool:
        return SpendRefusalCause.HARD_FLOOR in self.refusal_causes

    @property
    def balance_unobservable_triggered(self) -> bool:
        """True when no current, complete balance-safety fact was usable.

        A provider outage, stale observation, or unknown existing liability is not
        the same failure as an observed balance sitting at/below the floor. An
        operator must be able to distinguish all of them from an ordinary price
        ceiling breach; each is the floor mechanism failing closed on missing proof.
        """

        return SpendRefusalCause.BALANCE_UNOBSERVABLE in self.refusal_causes

    @property
    def balance_observation_usable(self) -> bool:
        """Whether this assessment actually relied on a current complete observation."""

        return self.balance_observation is not None and not self.balance_unobservable_triggered

    def to_record(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "pod_hourly_usd": str(self.estimate.pod_hourly_usd),
            "volume_ongoing_hourly_usd": str(self.estimate.volume_hourly_usd),
            "price_source": self.estimate.source,
            # Displayed to a human, so rounded to the cent; the ceiling
            # comparison already ran against the exact Decimal above.
            "estimated_pod_cost_usd": str(_quantize_to_cents(self.estimated_pod_cost_usd)),
            "estimated_attached_volume_cost_usd": str(
                _quantize_to_cents(self.estimated_volume_cost_usd)
            ),
            "estimated_total_metered_cost_usd": str(
                _quantize_to_cents(self.estimated_total_cost_usd)
            ),
            "requested_lifetime_seconds": self.requested_lifetime_seconds,
            "ceilings": None
            if not self.policy.configured
            else {
                "max_hourly_usd": str(self.policy.max_hourly_usd),
                "max_estimated_metered_cost_usd": str(self.policy.max_estimated_metered_cost_usd),
                "account_balance_floor_usd": str(self.policy.account_balance_floor_usd),
                "account_balance_alert_usd": str(self.policy.account_balance_alert_usd),
                "account_balance_observation": None
                if self.balance_observation is None
                else {
                    "available_usd": str(self.balance_observation.available_usd),
                    "observed_at": self.balance_observation.observed_at.isoformat(),
                    "source": self.balance_observation.source,
                },
                "other_reserved_liability_usd": str(
                    _quantize_to_cents(self.reserved_liability_usd)
                ),
                "alerts": list(self.alerts),
                "alert_notifications": list(self.alert_notifications),
                "hard_lifetime_seconds": self.policy.hard_lifetime_seconds,
                "billing_cutoff_margin_seconds": self.policy.billing_cutoff_margin_seconds,
            },
        }


def _one_line(detail: str | None) -> str:
    """Collapse a provider error into one bounded line fit for a printed reason."""

    if not detail:
        return ""
    text = " ".join(str(detail).split())
    if len(text) > 160:
        text = f"{text[:160]} (reason truncated at 160 characters)"
    return text


def _quantize_to_cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def load_spend_policy(path: str | Path) -> SpendPolicy:
    """Read a closed TOML policy.  Unknown fields cannot quietly widen spend."""

    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SpendRefusal(f"cannot read spend policy {source}: {error}") from error
    if not isinstance(raw, dict):
        raise SpendRefusal("spend policy root must be a TOML table")
    state = raw.get("state")
    schema = raw.get("schema")
    if schema != SPEND_SCHEMA:
        retired = RETIRED_SPEND_SCHEMAS.get(schema) if isinstance(schema, str) else None
        if retired is not None:
            raise SpendRefusal(
                f"spend policy schema {schema!r} is retired: {retired}. Add that ceiling "
                f"deliberately and rename the schema to {SPEND_SCHEMA!r}"
            )
        raise SpendRefusal(f"spend policy schema must be {SPEND_SCHEMA!r}")
    if state == "unconfigured":
        if set(raw) != {"schema", "state"}:
            raise SpendRefusal("unconfigured spend policy may contain only schema and state")
        return SpendPolicy(state="unconfigured")
    if state != "configured":
        raise SpendRefusal("spend policy state must be 'unconfigured' or 'configured'")
    allowed = {
        "schema",
        "state",
        "currency",
        "max_hourly_usd",
        "max_estimated_metered_cost_usd",
        "account_balance_floor_usd",
        "account_balance_alert_usd",
        "hard_lifetime_seconds",
        "laptop_heartbeat_timeout_seconds",
        "shutdown_poll_interval_seconds",
        "shutdown_deadline_seconds",
        "billing_cutoff_margin_seconds",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SpendRefusal(f"spend policy has unknown field(s) {unknown}")
    if raw.get("currency") != "USD":
        raise SpendRefusal("configured spend policy currency must be USD")
    try:
        return SpendPolicy(
            state="configured",
            max_hourly_usd=_decimal_text(raw.get("max_hourly_usd"), "max_hourly_usd"),
            max_estimated_metered_cost_usd=_decimal_text(
                raw.get("max_estimated_metered_cost_usd"), "max_estimated_metered_cost_usd"
            ),
            account_balance_floor_usd=_decimal_text(
                raw.get("account_balance_floor_usd"), "account_balance_floor_usd"
            ),
            account_balance_alert_usd=_decimal_text(
                raw.get("account_balance_alert_usd"), "account_balance_alert_usd"
            ),
            hard_lifetime_seconds=raw.get("hard_lifetime_seconds"),
            laptop_heartbeat_timeout_seconds=raw.get("laptop_heartbeat_timeout_seconds"),
            shutdown_poll_interval_seconds=raw.get("shutdown_poll_interval_seconds"),
            shutdown_deadline_seconds=raw.get("shutdown_deadline_seconds"),
            billing_cutoff_margin_seconds=raw.get("billing_cutoff_margin_seconds"),
        )
    except (TypeError, ValueError, SpendRefusal) as error:
        if isinstance(error, SpendRefusal):
            raise
        raise SpendRefusal(f"invalid configured spend policy: {error}") from error


def assess_spend(
    policy: SpendPolicy,
    estimate: PodEstimate,
    *,
    requested_deadline: datetime,
    now: datetime,
    balance_observation: AccountBalanceObservation | None = None,
    balance_unavailable_detail: str | None = None,
    reserved_liability_usd: Decimal = Decimal("0"),
    balance_safety_unavailable_detail: str | None = None,
) -> SpendAssessment:
    """Apply the same checks to an estimated create or an adopted pod's rate.

    ``balance_unavailable_detail`` names *why* the balance could not be read when
    ``balance_observation`` is ``None``. Both cases fail closed, but an unobservable
    balance remains distinct from an observed floor breach so the refusal can name
    whether the source was missing, timed out, or returned an unusable response.
    """

    start = require_utc(now, "spend assessment now")
    deadline = require_utc(requested_deadline, "requested hard deadline")
    elapsed = deadline - start
    # A hard deadline may carry microseconds.  Round *up*, never down, so a
    # fractional extra second cannot fit through either spend ceiling.
    requested_seconds = 0
    if deadline > start:
        requested_seconds = elapsed.days * 86_400 + elapsed.seconds + bool(elapsed.microseconds)
    pod_cost = estimate.pod_hourly_usd * Decimal(requested_seconds) / Decimal(3600)
    volume_cost = estimate.volume_hourly_usd * Decimal(requested_seconds) / Decimal(3600)
    total_cost = pod_cost + volume_cost
    reserved_liability = as_decimal(reserved_liability_usd, "other reserved liability")
    metered_hourly = estimate.pod_hourly_usd + estimate.volume_hourly_usd
    reasons: list[str] = []
    causes: set[SpendRefusalCause] = set()
    alerts: list[str] = []
    if not policy.configured:
        reasons.append("spend policy is unconfigured; paid actions are refused")
    elif requested_seconds <= 0:
        reasons.append("hard lifetime must end in the future")
    else:
        # Narrowing, not a check: this branch is reached only on a configured policy, and
        # `SpendPolicy.__post_init__` raises `SpendRefusal` when one is missing a ceiling.
        # A `raise` survives `-O`, so the invariant holds where these asserts do not.
        assert policy.hard_lifetime_seconds is not None
        assert policy.max_hourly_usd is not None
        assert policy.max_estimated_metered_cost_usd is not None
        if requested_seconds > policy.hard_lifetime_seconds:
            reasons.append("requested hard lifetime exceeds configured ceiling")
        if metered_hourly > policy.max_hourly_usd:
            reasons.append(
                "combined pod and attached-volume hourly price exceeds configured ceiling"
            )
        if total_cost > policy.max_estimated_metered_cost_usd:
            reasons.append(
                "estimated combined pod and attached-volume cost exceeds configured ceiling"
            )
        assert policy.account_balance_floor_usd is not None
        assert policy.account_balance_alert_usd is not None
        if balance_safety_unavailable_detail:
            causes.add(SpendRefusalCause.BALANCE_UNOBSERVABLE)
            reasons.append(
                "balance safety could not be established ("
                + _one_line(balance_safety_unavailable_detail)
                + "); paid actions are refused"
            )
        elif balance_observation is None:
            causes.add(SpendRefusalCause.BALANCE_UNOBSERVABLE)
            cause = _one_line(balance_unavailable_detail)
            reasons.append(
                "available account balance was not observed"
                + (f" ({cause})" if cause else "")
                + "; paid actions are refused"
            )
        else:
            available = balance_observation.available_usd
            observation_age = (start - balance_observation.observed_at).total_seconds()
            if observation_age < 0:
                causes.add(SpendRefusalCause.BALANCE_UNOBSERVABLE)
                reasons.append(
                    "balance observation is dated in the future; paid actions are refused"
                )
            elif observation_age > MAX_BALANCE_OBSERVATION_AGE_SECONDS:
                causes.add(SpendRefusalCause.BALANCE_UNOBSERVABLE)
                reasons.append(
                    f"balance observation is stale by {observation_age:g} seconds; "
                    "paid actions are refused"
                )
            elif available <= policy.account_balance_floor_usd:
                causes.add(SpendRefusalCause.HARD_FLOOR)
                reasons.append("observed account balance is at or below the hard floor")
            elif available - reserved_liability - total_cost <= policy.account_balance_floor_usd:
                # No balance gate runs after create/adopt, so the reserve must
                # survive this action's maximum cost through its hard deadline.
                causes.add(SpendRefusalCause.HARD_FLOOR)
                reasons.append(
                    "other reserved paid-action liability plus the estimated cost of this "
                    "action would take the observed account balance to or below the hard floor"
                )
            elif available <= policy.account_balance_alert_usd:
                alerts.append("observed account balance is below the notification threshold")
    return SpendAssessment(
        allowed=not reasons,
        reasons=tuple(reasons),
        estimate=estimate,
        requested_lifetime_seconds=requested_seconds,
        estimated_pod_cost_usd=pod_cost,
        estimated_volume_cost_usd=volume_cost,
        estimated_total_cost_usd=total_cost,
        policy=policy,
        balance_observation=balance_observation,
        reserved_liability_usd=reserved_liability,
        refusal_causes=frozenset(causes),
        alerts=tuple(alerts),
    )


def require_confirmation(value: str | None, expected: str) -> None:
    """The same gate for create and adoption, without a side door.

    No stripping and no case-folding: a near-miss is not a confirmation.

    ``create``/``adopt`` re-assess the price internally after the operator's
    preview, so ``expected`` may carry a rate the operator never saw typed
    back at them.  A mismatch that still names the same action and subject
    (only the rate suffix differs) is named as a possible price move rather
    than left to read like a plain typo.
    """

    if value == expected:
        return
    challenge_start = expected.rfind(" CHALLENGE ")
    price_start = expected.rfind(" AT $")
    if isinstance(value, str) and challenge_start != -1:
        # Everything but the challenge matches: the operator retyped a phrase from an
        # older preview. Naming that is the difference between "you mistyped" and
        # "this authorization was for a different preview".
        if value[:challenge_start] == expected[:challenge_start]:
            raise SpendRefusal(
                "typed confirmation names a different preview challenge; a confirmation "
                "is valid only for the preview that issued it -- re-run the preview and "
                "type its phrase; no paid action occurred"
            )
    if (
        isinstance(value, str)
        and price_start != -1
        and value[:price_start] == expected[:price_start]
    ):
        raise SpendRefusal(
            "typed confirmation does not match this preview's phrase; the price may "
            "have changed between preview and confirmation -- re-run the preview and "
            "confirm the current price; no paid action occurred"
        )
    # The expected phrase is deliberately not reproduced here: this refusal is
    # printed and logged, and the phrase it would quote is still spendable
    # because a typo does not burn the preview.
    raise SpendRefusal(
        "typed confirmation does not match the phrase this preview displayed; "
        "type it exactly as shown; no paid action occurred"
    )


def _decimal_text(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise SpendRefusal(f"{label} must be a decimal string, not a TOML number")
    return as_decimal(value, label)
