"""Strict, intentionally unconfigured spend policy and shared paid-action gate."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .models import PodEstimate, SpendRefusal, as_decimal, require_utc

SPEND_SCHEMA = "pod-spend.v1"

CONFIRMATION_PREFIX = "I CONFIRM PAID POD"
"""The fixed opening of the typed phrase; the rest is derived from the price.

A constant phrase can be typed from memory without referring to one action.
CLAUDE.md's Gate rule is that a clear answer authorises *that action and nothing
adjacent*, so this phrase names the action, subject, and both displayed hourly
rates. It binds the acknowledgement to one preview; it is not proof of Tyrel's
permission and does not claim automation cannot derive the phrase.
"""


def confirmation_phrase(
    action: str, subject: str, pod_hourly_usd: Decimal, volume_hourly_usd: Decimal
) -> str:
    """The exact text an operator must type back for this one paid action."""

    return (
        f"{CONFIRMATION_PREFIX} {action} {subject} "
        f"AT ${pod_hourly_usd}/HR PLUS ${volume_hourly_usd}/HR VOLUME"
    )


@dataclass(frozen=True, slots=True)
class SpendPolicy:
    """Ceilings Tyrel configures; the checked-in policy intentionally has none.

    ``max_hourly_usd`` and ``max_estimated_metered_cost_usd`` apply to all
    launch-time metering: the pod plus its attached volume. Ongoing volume
    retention or deletion after close remains a separately authorized decision.
    """

    state: str
    max_hourly_usd: Decimal | None = None
    max_estimated_metered_cost_usd: Decimal | None = None
    hard_lifetime_seconds: int | None = None
    laptop_heartbeat_timeout_seconds: int | None = None
    shutdown_poll_interval_seconds: int | None = None
    shutdown_deadline_seconds: int | None = None

    @property
    def configured(self) -> bool:
        return self.state == "configured"

    def __post_init__(self) -> None:
        if self.state not in {"unconfigured", "configured"}:
            raise SpendRefusal("spend state must be 'unconfigured' or 'configured'")
        values = (
            self.max_hourly_usd,
            self.max_estimated_metered_cost_usd,
            self.hard_lifetime_seconds,
            self.laptop_heartbeat_timeout_seconds,
            self.shutdown_poll_interval_seconds,
            self.shutdown_deadline_seconds,
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
        if self.max_hourly_usd <= 0 or self.max_estimated_metered_cost_usd <= 0:
            raise SpendRefusal("money ceilings must be positive")
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

    def to_record(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "pod_hourly_usd": str(self.estimate.pod_hourly_usd),
            "volume_ongoing_hourly_usd": str(self.estimate.volume_hourly_usd),
            "estimated_pod_cost_usd": str(self.estimated_pod_cost_usd),
            "estimated_attached_volume_cost_usd": str(self.estimated_volume_cost_usd),
            "estimated_total_metered_cost_usd": str(self.estimated_total_cost_usd),
            "requested_lifetime_seconds": self.requested_lifetime_seconds,
            "ceilings": None
            if not self.policy.configured
            else {
                "max_hourly_usd": str(self.policy.max_hourly_usd),
                "max_estimated_metered_cost_usd": str(self.policy.max_estimated_metered_cost_usd),
                "hard_lifetime_seconds": self.policy.hard_lifetime_seconds,
            },
        }


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
        "hard_lifetime_seconds",
        "laptop_heartbeat_timeout_seconds",
        "shutdown_poll_interval_seconds",
        "shutdown_deadline_seconds",
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
            hard_lifetime_seconds=raw.get("hard_lifetime_seconds"),
            laptop_heartbeat_timeout_seconds=raw.get("laptop_heartbeat_timeout_seconds"),
            shutdown_poll_interval_seconds=raw.get("shutdown_poll_interval_seconds"),
            shutdown_deadline_seconds=raw.get("shutdown_deadline_seconds"),
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
) -> SpendAssessment:
    """Apply the same checks to an estimated create or an adopted pod's rate."""

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
    metered_hourly = estimate.pod_hourly_usd + estimate.volume_hourly_usd
    reasons: list[str] = []
    if not policy.configured:
        reasons.append("spend policy is unconfigured; paid actions are refused")
    elif requested_seconds <= 0:
        reasons.append("hard lifetime must end in the future")
    else:
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
    return SpendAssessment(
        allowed=not reasons,
        reasons=tuple(reasons),
        estimate=estimate,
        requested_lifetime_seconds=requested_seconds,
        estimated_pod_cost_usd=pod_cost,
        estimated_volume_cost_usd=volume_cost,
        estimated_total_cost_usd=total_cost,
        policy=policy,
    )


def require_confirmation(value: str | None, expected: str) -> None:
    """The same gate for create and adoption, without a side door.

    No stripping and no case-folding: a near-miss is not a confirmation.
    """

    if value != expected:
        raise SpendRefusal(
            f"typed confirmation must be exactly {expected!r}; no paid action occurred"
        )


def _decimal_text(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise SpendRefusal(f"{label} must be a decimal string, not a TOML number")
    return as_decimal(value, label)
