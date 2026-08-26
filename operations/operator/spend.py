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
            f"- Hard lifetime ceiling: {policy.hard_lifetime_seconds} seconds "
            f"(policy SHA-256 {policy_digest})",
        ]
        observations, alerts, unreadable = self._recorded_balance_history()
        if unreadable:
            lines.append(
                "Saved launch confirmations that could not be read in full "
                "(named, not skipped; no number below came from them):"
            )
            lines.extend(f"- {_recorded_text(note)}" for note in unreadable)
        if observations:
            lines.append(
                "Recorded balance observations (read-only; no new provider check was made):"
            )
            lines.extend(self._balance_line(item) for item in observations)
        else:
            lines.append(
                "Recorded balance observations: none. Only a confirmed launch saves one "
                "in these records; a preview that was never confirmed saves nothing."
            )
        if alerts:
            lines.append(
                "Notification-only alert history (read-only; it did not block a paid action):"
            )
            lines.extend(
                f"- Alert: {_recorded_text(item['alert'])}; "
                f"delivery record: {_recorded_text(item['delivery'])}; "
                f"receipt SHA-256 {item['digest']}"
                for item in alerts
            )
        else:
            lines.append("Notification-only alert history: no alert record was found.")
        return lines

    def _recorded_balance_history(
        self,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
        """Project launch confirmations oldest first, naming unreadable receipts.

        Content digests carry no chronology, so the order comes from the
        validated `recorded_at` instant. A partial money history must retain a
        visible gap for every unreadable receipt.
        """

        observations: list[dict[str, str]] = []
        alerts: list[dict[str, str]] = []
        try:
            records, unreadable = self.receipts.readable_records_of_kind("launch-confirmation")
        except RecordError as error:
            raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error
        for path, record in sorted(
            records, key=lambda item: (_recorded_instant(item[1]), item[0].name)
        ):
            # `readable_records_of_kind` hands back only files it opened by the
            # name it was given, and `ReceiptStore.read` matched that name's
            # digest to the canonical bytes. A link would break both halves.
            digest = path.name.rsplit("-", 1)[-1].removesuffix(".json")
            preview = record["payload"].get("preview")
            spend = preview.get("spend") if isinstance(preview, dict) else None
            ceilings = spend.get("ceilings") if isinstance(spend, dict) else None
            if not isinstance(ceilings, dict):
                # Every confirmation represents a paid action even when its saved
                # preview or ceilings are absent or malformed. A missing preview
                # is the same lost spend fact as an unreadable one, because this
                # tool writes a preview into every confirmation it records.
                unreadable.append(
                    f"{path.name}: its saved preview carries no readable spend ceilings, "
                    "so no number from this confirmed paid action is shown below"
                )
                continue
            observation = ceilings.get("account_balance_observation")
            if isinstance(observation, dict):
                observations.append(
                    {
                        "available_usd": _recorded_field(observation.get("available_usd")),
                        "observed_at": _recorded_field(observation.get("observed_at")),
                        "source": _recorded_field(observation.get("source")),
                        "digest": digest,
                    }
                )
            rows, notes = _alert_rows(ceilings, digest)
            alerts.extend(rows)
            unreadable.extend(f"{path.name}: {note}" for note in notes)
        return observations, alerts, unreadable

    def _balance_line(self, observation: dict[str, str]) -> str:
        try:
            observed_at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
            age = (self.now - observed_at).total_seconds()
            if age < 0:
                # A future stamp cannot be aged; calling it stale would claim it is old.
                staleness = "DATED IN THE FUTURE"
            elif age <= MAX_BALANCE_OBSERVATION_AGE_SECONDS:
                staleness = "CURRENT"
            else:
                staleness = "STALE"
        # Offset-free strings parse as naive datetimes, whose subtraction from
        # aware `now` raises TypeError; one malformed stamp must not hide the view.
        except (TypeError, ValueError):
            staleness = "UNREADABLE TIMESTAMP"
        return (
            f"- Observed balance: ${_recorded_text(observation['available_usd'])}; "
            f"source: {_recorded_text(observation['source'])}; "
            f"observed at: {_recorded_text(observation['observed_at'])}; "
            f"staleness now: {staleness}; receipt SHA-256 {observation['digest']}"
        )


def _alert_rows(ceilings: dict[str, object], digest: str) -> tuple[list[dict[str, str]], list[str]]:
    """Project one receipt's alert episodes without inventing a pairing.

    The record keeps alerts and delivery outcomes as separate flat lists. A
    skipped delivery attempt leaves no placeholder, so position is attributable
    only when the counts match. On a mismatch, both lists remain visible but
    unattributed.
    """

    recorded = ceilings.get("alerts", [])
    deliveries = ceilings.get("alert_notifications", [])
    if not isinstance(recorded, list) or not isinstance(deliveries, list):
        return [], [
            "its saved alerts and delivery outcomes are not both lists, "
            "so no alert episode from it is shown below"
        ]
    paired = len(recorded) == len(deliveries)
    unpaired = (
        f"not attributable: the receipt saved {len(recorded)} alert(s) "
        f"and {len(deliveries)} delivery outcome(s), which position cannot pair"
    )
    rows: list[dict[str, str]] = []
    for position, alert in enumerate(recorded[:MAX_ALERT_ENTRIES_SHOWN]):
        if not isinstance(alert, str):
            # Preserve position so the alert fact and its delivery outcome survive.
            alert = f"{NOT_RECORDED_AS_TEXT} (saved alert {position + 1})"
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
    omitted = max(len(recorded) - MAX_ALERT_ENTRIES_SHOWN, 0)
    if not paired:
        rows.extend(
            {
                "alert": unpaired,
                "delivery": delivery if isinstance(delivery, str) else "invalid delivery outcome",
                "digest": digest,
            }
            for delivery in deliveries[:MAX_ALERT_ENTRIES_SHOWN]
        )
        omitted += max(len(deliveries) - MAX_ALERT_ENTRIES_SHOWN, 0)
    if omitted:
        rows.append(
            {
                "alert": f"{omitted} further saved alert or delivery entries in this receipt "
                "are counted here rather than shown",
                "delivery": "not shown: read the receipt named by the digest below for them",
                "digest": digest,
            }
        )
    return rows, []


# Substitute one malformed field, never the record that contains it.
NOT_RECORDED_AS_TEXT = "not readable: this value was not saved as text"

MAX_ALERT_ENTRIES_SHOWN = 64
"""How many saved alert or delivery entries one receipt may put on this screen.

A launch confirmation records one episode per crossed warning threshold, so a
genuine receipt holds one or two. `records.MAX_RECORD_BYTES` bounds one *file*,
not this projection: measured here, one lawful four-mebibyte receipt holding a
million one-character alerts rendered 1,000,009 lines at 504 MiB resident, and
this screen accumulates every receipt in an append-only store — so a handful of
them reaches exactly the kill that printed nothing at all, which is the failure
that bound was chosen to prevent. The overflow is counted and shown against the
receipt's own digest, so it is bounded on screen and not lost.
"""


def _recorded_instant(record: dict[str, object]) -> datetime:
    """Order the money history by the instant, not by the spelling of the stamp.

    `ReceiptStore.read` validates canonical UTC, but that spelling omits a zero
    microsecond field, so `...:00.500000Z` sorts before `...:00Z` as text while
    following it in time. The parse cannot fail; `read` already made it.
    """

    return datetime.fromisoformat(str(record["recorded_at"]).replace("Z", "+00:00"))


def _recorded_field(value: object) -> str:
    """Keep a malformed field visible as one missing field, not a missing fact.

    Rejecting the containing observation would also hide its readable fields.
    """

    return value if isinstance(value, str) else NOT_RECORDED_AS_TEXT


def _recorded_text(value: str) -> str:
    """Hold one recorded fact to one screen line.

    `cli._print` preserves newlines for refusal framing, but recorded text must
    not create an undigested line that looks like this surface's own output.
    """

    return strip_control_bytes(value)
