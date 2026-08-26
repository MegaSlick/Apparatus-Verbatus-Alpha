"""Read-only operator projection of the reviewed pod spend controls.

This module deliberately has no provider seam. A balance shown here is an
already-recorded launch-preview observation, never a new account query; the
runtime remains the only place that may observe a balance at a paid-action
gate. The policy is read, never changed, and every monetary value carries the
digest of the record that supplied it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from operations.pod.spend import MAX_BALANCE_OBSERVATION_AGE_SECONDS, load_spend_policy

from .errors import ErrorCode, OperatorError, strip_control_bytes
from .records import ReceiptStore, RecordError, sha256_file


@dataclass(frozen=True, slots=True)
class SpendSurface:
    """The policy and immutable receipts a person can inspect without spending."""

    receipts: ReceiptStore
    now: datetime

    def show(self, policy_path: str | Path) -> list[str]:
        """Render the reviewed policy and warning history without writing."""

        source = Path(policy_path)
        try:
            policy_digest = sha256_file(source)
            policy = load_spend_policy(source)
            if sha256_file(source) != policy_digest:
                raise ValueError("spend policy changed while it was being read")
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

        assert policy.max_hourly_usd is not None
        assert policy.max_estimated_metered_cost_usd is not None
        assert policy.account_balance_floor_usd is not None
        assert policy.account_balance_alert_usd is not None
        assert policy.hard_lifetime_seconds is not None
        lines = [
            "Reviewed spend policy (read-only):",
            f"- Policy record: {source} (SHA-256 {policy_digest})",
            f"- Combined hourly ceiling: ${policy.max_hourly_usd} (policy SHA-256 {policy_digest})",
            "- Estimated metered-cost ceiling: "
            f"${policy.max_estimated_metered_cost_usd} (policy SHA-256 {policy_digest})",
            f"- Hard-stop balance floor: ${policy.account_balance_floor_usd} "
            f"(policy SHA-256 {policy_digest})",
            f"- Notification-only balance alert: ${policy.account_balance_alert_usd} "
            f"(policy SHA-256 {policy_digest})",
            f"- Hard lifetime ceiling: {policy.hard_lifetime_seconds} seconds "
            f"(policy SHA-256 {policy_digest})",
        ]
        observations, alerts, unreadable = self._recorded_balance_history()
        if unreadable:
            lines.append(
                "Saved launch confirmations that could not be read "
                "(named, not skipped; no number below came from them):"
            )
            lines.extend(f"- {_recorded_text(note)}" for note in unreadable)
        if observations:
            lines.append(
                "Recorded balance observations (read-only; no new provider check was made):"
            )
            lines.extend(self._balance_line(item) for item in observations)
        else:
            lines.append("Recorded balance observations: none in the saved launch-preview records.")
        if alerts:
            lines.append(
                "Notification-only alert history (read-only; it did not block a paid action):"
            )
            lines.extend(self._alert_line(item) for item in alerts)
        else:
            lines.append("Notification-only alert history: no alert record was found.")
        return lines

    def _recorded_balance_history(
        self,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
        """Every saved launch confirmation, oldest first: projected, or named as unread.

        `records_of_kind` reads the whole receipt directory and raises on the
        first file that will not read, so one damaged receipt -- of any kind,
        including kinds this view never shows -- deleted the ceilings, the
        floor and every sound observation from the screen at once. `status`
        survives that because it prints as it goes and marks the one record it
        could not read; this view returns its lines all together, so it has to
        carry the same gap in them (GOVERNANCE 2).

        Oldest first, by the receipt's own `recorded_at`: content-addressed
        filenames sort by digest, which is no order at all, and a money history
        listed in no order invites its last line to be read as its latest.
        `ReceiptStore.read` has already refused any stamp that is not canonical
        UTC, so these sort lexicographically in time.
        """

        observations: list[dict[str, str]] = []
        alerts: list[dict[str, str]] = []
        try:
            records, unreadable = self.receipts.readable_records_of_kind("launch-confirmation")
        except RecordError as error:
            raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error
        for path, record in sorted(
            records, key=lambda item: (item[1]["recorded_at"], item[0].name)
        ):
            preview = record["payload"].get("preview")
            if not isinstance(preview, dict):
                continue
            spend = preview.get("spend")
            if not isinstance(spend, dict):
                continue
            ceilings = spend.get("ceilings")
            if not isinstance(ceilings, dict):
                continue
            digest = _receipt_digest(path)
            observation = ceilings.get("account_balance_observation")
            if isinstance(observation, dict):
                available = observation.get("available_usd")
                observed_at = observation.get("observed_at")
                source_name = observation.get("source")
                if (
                    isinstance(available, str)
                    and isinstance(observed_at, str)
                    and isinstance(source_name, str)
                ):
                    observations.append(
                        {
                            "available_usd": available,
                            "observed_at": observed_at,
                            "source": source_name,
                            "digest": digest,
                        }
                    )
            alerts.extend(_alert_rows(ceilings, digest))
        return observations, alerts, unreadable

    def _balance_line(self, observation: dict[str, str]) -> str:
        try:
            observed_at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
            age = (self.now - observed_at).total_seconds()
            staleness = "CURRENT" if 0 <= age <= MAX_BALANCE_OBSERVATION_AGE_SECONDS else "STALE"
        # TypeError as well as ValueError: `fromisoformat` accepts a stamp with no
        # offset and returns a naive datetime, and subtracting one from this
        # surface's aware `now` raises TypeError, not ValueError. The saved
        # stamp is free text out of a record -- which is why this branch exists
        # at all -- so the narrower catch turned one malformed observation into
        # the loss of the whole view: ceilings, floor and every other sound
        # observation with it. GOVERNANCE 2 wants the partial result shown as
        # partial, and this line named as the part that is missing.
        except (TypeError, ValueError):
            staleness = "UNREADABLE TIMESTAMP"
        return (
            f"- Observed balance: ${_recorded_text(observation['available_usd'])}; "
            f"source: {_recorded_text(observation['source'])}; "
            f"observed at: {_recorded_text(observation['observed_at'])}; "
            f"staleness now: {staleness}; receipt SHA-256 {observation['digest']}"
        )

    @staticmethod
    def _alert_line(alert: dict[str, str]) -> str:
        return (
            f"- Alert: {_recorded_text(alert['alert'])}; "
            f"delivery record: {_recorded_text(alert['delivery'])}; "
            f"receipt SHA-256 {alert['digest']}"
        )


def _receipt_digest(path: Path) -> str:
    """ReceiptStore has already verified the name's digest against the bytes."""

    return path.name.rsplit("-", 1)[-1].removesuffix(".json")


def _alert_rows(ceilings: dict[str, object], digest: str) -> list[dict[str, str]]:
    """Project one receipt's alert episodes without inventing a pairing.

    The record keeps two flat lists, `alerts` and `alert_notifications`, not
    pairs. The writer appends a delivery line only for an episode it actually
    attempted: `operations/pod/launch.py:_record_spend_notifications` skips an
    alert whose balance observation is absent or unusable, and skips one whose
    stamp says it is not yet due, so a skipped episode leaves no hole at its own
    index -- the later outcomes simply shift down. Equal lengths are therefore
    the only state in which position means correspondence.

    Today they are always equal, because `operations/pod/spend.py` has exactly
    one `alerts.append` and it is not in a loop, so at most one alert exists per
    assessment. That is a property of Unit 22's current thresholds, not of this
    record's shape, and it is not this surface's to rely on: the day a second
    threshold is added, reading position as identity would print one alert's
    delivery outcome under another alert's name. A display that asserts a
    pairing its record does not carry is reporting the reader rather than the
    reading (GOVERNANCE 10), so when the counts disagree this says so and shows
    both sides unattributed -- neither list is dropped (GOVERNANCE 2).
    """

    recorded = ceilings.get("alerts")
    deliveries = ceilings.get("alert_notifications")
    if not isinstance(recorded, list) or not isinstance(deliveries, list):
        return []
    paired = len(recorded) == len(deliveries)
    unpaired = (
        f"not attributable: the receipt saved {len(recorded)} alert(s) "
        f"and {len(deliveries)} delivery outcome(s), which position cannot pair"
    )
    rows: list[dict[str, str]] = []
    for position, alert in enumerate(recorded):
        if not isinstance(alert, str):
            continue
        if not paired:
            rows.append({"alert": alert, "delivery": unpaired, "digest": digest})
            continue
        delivery = deliveries[position]
        rows.append(
            {
                "alert": alert,
                "delivery": delivery if isinstance(delivery, str) else "invalid delivery outcome",
                "digest": digest,
            }
        )
    if not paired:
        rows.extend(
            {
                "alert": unpaired,
                "delivery": delivery if isinstance(delivery, str) else "invalid delivery outcome",
                "digest": digest,
            }
            for delivery in deliveries
        )
    return rows


def _recorded_text(value: str) -> str:
    """Hold one recorded fact to one screen line.

    `cli._print` strips control bytes but keeps newlines, deliberately: a
    three-part refusal depends on its own. Every string this surface renders
    from a receipt is free text the record supplied, so one carrying a newline
    would print a second line that no receipt digest is attached to, reading
    exactly like a policy line this surface vouched for. The receipt keeps the
    bytes it was given -- GOVERNANCE 4 -- the screen simply does not gain a line.
    """

    return strip_control_bytes(value)
