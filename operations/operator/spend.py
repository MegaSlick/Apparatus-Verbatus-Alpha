"""`verbatus spend show`: the reviewed pod spending policy, read and never changed.

It contacts no provider. Every monetary value it shows carries the digest of
the policy bytes it was parsed from.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from operations.pod.spend import load_spend_policy_bytes

from .errors import ErrorCode, OperatorError, strip_control_bytes
from .records import bounded_bytes


def show(policy_path: str | Path) -> list[str]:
    """Render the reviewed policy without writing."""

    source = Path(policy_path)
    try:
        # One read, one byte sequence, so the digest printed beside every
        # ceiling is the digest of the exact bytes it was parsed from,
        # never a second read that could see a widened-then-restored file.
        policy_bytes = bounded_bytes(source, "the spend policy")
        policy_digest = hashlib.sha256(policy_bytes).hexdigest()
        policy = load_spend_policy_bytes(policy_bytes, source=source)
    except Exception as error:
        raise OperatorError(ErrorCode.SPEND_POLICY_UNREADABLE, detail=str(error)) from error
    if not policy.configured:
        raise OperatorError(
            ErrorCode.SPEND_POLICY_UNCONFIGURED,
            detail=(
                f"{source} has state=unconfigured (SHA-256 {policy_digest}); "
                "it contains no ceilings, floor, or alert threshold to display"
            ),
        )

    # Narrowing, not a check: `SpendPolicy.__post_init__` raises `SpendRefusal`
    # on a configured policy missing any ceiling, and a `raise` survives `-O`.
    assert policy.max_hourly_usd is not None
    assert policy.max_estimated_metered_cost_usd is not None
    assert policy.account_balance_floor_usd is not None
    assert policy.account_balance_alert_usd is not None
    assert policy.hard_lifetime_seconds is not None
    return [
        "Reviewed spend policy (read-only):",
        # POSIX paths may contain newlines, which `cli._print` preserves for
        # refusal framing; contain the path so it cannot forge a ceiling line.
        f"- Policy record: {_recorded_text(str(source))} (SHA-256 {policy_digest})",
        f"- Combined hourly ceiling: ${policy.max_hourly_usd} (policy SHA-256 {policy_digest})",
        "- Estimated metered-cost ceiling: "
        f"${policy.max_estimated_metered_cost_usd} (policy SHA-256 {policy_digest})",
        f"- Hard-stop balance floor: ${policy.account_balance_floor_usd} "
        f"(policy SHA-256 {policy_digest})",
        f"- Notification-only balance alert: ${policy.account_balance_alert_usd} "
        f"(policy SHA-256 {policy_digest})",
        "- Launch lifetime (the deadline a launch sets at creation): "
        f"{_duration(policy.hard_lifetime_seconds)} (policy SHA-256 {policy_digest})",
        f"- Soft maximum: {_duration(policy.soft_max_seconds)} and "
        f"${policy.soft_max_cost_usd}, whichever comes first; the guard's deadline sits here "
        f"(policy SHA-256 {policy_digest})",
        f"- Hard maximum: {_duration(policy.hard_max_seconds)} and "
        f"${policy.hard_max_cost_usd}; an extension by the lead may not pass it "
        f"(policy SHA-256 {policy_digest})",
    ]


def _duration(seconds: int | None) -> str:
    """Seconds as the policy records them, with hours beside them for reading."""

    if seconds is None:
        return "unset"
    return f"{seconds} seconds ({seconds / 3600:.3g} h)"


def _recorded_text(value: str) -> str:
    """Hold one recorded fact to one screen line.

    `cli._print` preserves newlines for refusal framing, but recorded text must
    not create an undigested line that looks like this surface's own output.
    """

    return strip_control_bytes(value)
