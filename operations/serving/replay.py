"""The chair a replay run reads through: the source run's recorded replies, and no model.

A replay run (`common.replay`) asks its Perlector and Coniector exactly as a live run
does, through a real `ChairClient`: the client builds each request's bytes from the
sealed row and the receipt's seed, retains the reply and writes the call record. What
answers is `ReplayManager`'s handle instead of a server: it looks the request's bytes
up among the calls the source run recorded and hands back the reply the source
retained for them, byte for byte. So a request the code under replay builds as the
source did gets the source's reply, and its call record comes out identical.

Nothing else may come out of it. The client may retain only bytes the source retained
for this stage (`_retain_recorded`): a request the source never sent, or a call record
that differs in any field, stops the stage rather than record a reading no model made.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from common import replay
from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

from .client import ChairClient
from .http import HttpResponse


@dataclass(frozen=True)
class RecordedCall:
    """One call the source run sent and was answered: its record and the reply's bytes."""

    record: dict[str, Any]
    record_bytes: bytes
    response_bytes: bytes


@dataclass
class ReplayHandle:
    """A started chair whose every answer is a recorded one; it has no process to stop."""

    identity: ChairIdentity
    profile: Any
    receipt_reference: Mapping[str, str]
    audit_reference: Mapping[str, str]
    source_run_id: str
    calls: dict[str, RecordedCall] = field(repr=False)

    def _recorded(self, kind: str, body: bytes) -> RecordedCall:
        request_sha256 = digest_bytes(body)
        call = self.calls.get(request_sha256)
        if call is None or call.record.get("kind") != kind:
            raise ContractError(
                f"run {self.source_run_id} never sent a {self.identity.role} request with these "
                f"bytes ({request_sha256}), so no recorded reply answers it; a replay calls no "
                "model. Read it in a live run"
            )
        return call

    def request_reading(self, kind: str, body_bytes: bytes, timeout_seconds: float):
        call = self._recorded(kind, body_bytes)
        return HttpResponse(int(call.record["response_status"]), call.response_bytes)

    def stream_reading(
        self,
        kind: str,
        body_bytes: bytes,
        timeout_seconds: float,
        on_chunk: Callable[[bytes], bool],
    ) -> HttpResponse:
        """The recorded reply, handed to the loop watch whole.

        The recorded bytes already end where the live stream was stopped, so the
        watch reads them as one piece and finds the same loop, if any.
        """
        call = self._recorded(kind, body_bytes)
        status = int(call.record["response_status"])
        if status == 200:
            on_chunk(call.response_bytes)
        return HttpResponse(status, call.response_bytes)

    def stop(self) -> None:
        return None

    def hand_off(self) -> bool:
        return False


class ReplayManager:
    """Starts a chair as the source run's recorded session of it, for one replayed stage."""

    def __init__(self, context: Any, profile: Any) -> None:
        self.context = context
        self.profile = profile

    def start(self, identity: ChairIdentity, tier: str) -> ReplayHandle:
        source = replay.open_source(self.context.run)
        calls = recorded_calls(source, self.context.stage)
        mine = {
            sha: call for sha, call in calls.items() if call.record.get("chair") == identity.role
        }
        receipts = {_frozen(call.record["receipt_ref"]) for call in mine.values()}
        audits = {_frozen(call.record["launch_audit_ref"]) for call in mine.values()}
        if len(receipts) > 1 or len(audits) > 1:
            raise ContractError(
                f"run {source.run_id}'s {identity.role} answered in more than one serving "
                "session; a replay answers as one session and cannot say which"
            )
        if not mine:
            raise ContractError(
                f"run {source.run_id} recorded no answered {identity.role} call to replay"
            )
        receipt_reference = dict(next(iter(receipts)))
        audit_reference = dict(next(iter(audits)))
        # The launch audit is the session's own evidence; the stage keeps it as the
        # source's did, at the same address, so each replayed call record names it.
        retained = self.context.retain(source.read(audit_reference), "a recorded launch audit")
        if retained != audit_reference:
            raise ContractError(
                f"run {source.run_id}'s {identity.role} launch audit is not this stage's to keep "
                f"({audit_reference['relative_path']})"
            )
        self.context.tree.note_launch_audit(self.context.stage, audit_reference["sha256"])
        # Bound as a live start binds them, so the stage holds what the source's held.
        self.context.write_serving_evidence_manifest(receipt_reference, audit_reference)
        receipt = self.context.tree.read_run_receipt(receipt_reference)
        if receipt.get("seed") != self.profile.seed:
            raise ContractError(
                f"run {source.run_id}'s {identity.role} receipt names seed {receipt.get('seed')}, "
                f"but this run's sealed row sends {self.profile.seed}"
            )
        return ReplayHandle(
            identity=identity,
            profile=self.profile,
            receipt_reference=receipt_reference,
            audit_reference=audit_reference,
            source_run_id=source.run_id,
            calls=mine,
        )

    def reclaim_hand_off(self) -> None:
        return None


def _frozen(reference: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(reference.items()))


def recorded_calls(source: replay.Source, stage: str) -> dict[str, RecordedCall]:
    """Every answered call the source's records of `stage` name, by the digest of its request."""
    calls: dict[str, RecordedCall] = {}
    for engine_call in source.engine_calls(stage):
        record, record_bytes, response_bytes = replay.call_record(source, engine_call)
        calls[record["request_sha256"]] = RecordedCall(record, record_bytes, response_bytes)
    return calls


def _retain_recorded(context: Any, allowed: frozenset[str], data: bytes) -> dict[str, str]:
    """Keep bytes in the stage's blob store only when the source kept the very same bytes."""
    digest = digest_bytes(data)
    if digest not in allowed:
        raise ContractError(
            "a replayed call produced bytes its source run never retained (a call record or "
            f"reply {digest}); the code being replayed records this call differently, so the "
            "replay stops rather than keep a record no model call made"
        )
    return context.retain(data, label="a recorded chair response")


def replay_chair_client(
    context: Any,
    identity: ChairIdentity,
    tier: str,
    *,
    profile: Any,
    decoding_policy: Mapping[str, Any],
    decoding_config_sha256: str,
) -> ChairClient:
    """A real `ChairClient` over the source run's recorded session of this chair."""
    source = replay.open_source(context.run)
    calls = recorded_calls(source, context.stage)
    allowed = frozenset(
        digest
        for call in calls.values()
        for digest in (digest_bytes(call.record_bytes), digest_bytes(call.response_bytes))
    )
    return ChairClient(
        manager=ReplayManager(context, profile),
        identity=identity,
        tier=tier,
        retain=lambda data: _retain_recorded(context, allowed, data),
        decoding_config_sha256=decoding_config_sha256,
        decoding_policy=decoding_policy,
        read_receipt=context.tree.read_run_receipt,
    )
