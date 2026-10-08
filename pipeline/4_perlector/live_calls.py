"""What a live Perlector call leaves behind, and how a resumed pass reads it back.

A live call is recorded as a `reader-sent` record before it leaves, so a resumed pass
can tell a received reply from a call that never answered. The serving client retains
the reply and its call record; a reading names both, and each is re-derived from the
bytes on disk before it is named. A failed call is recorded as the facts the serving
layer observed, never as an engine incident when the failure was this code's own.

The page path (`page_run.py`) reads its evidence through these helpers; nothing here
publishes a reading.
"""

import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

import operations.serving.errors as serving_errors
from common import retained_replies
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.identities import attempt_id as derived_attempt_id
from common.contracts.serving import CHAIR_CALL_RECORD_SCHEMA as CHAIR_CALL_RECORD_SCHEMA
from common.contracts.stages import PERLECTOR
from common.in_order_window import in_order_window as in_order_window
from common.stage import verify_retained_call_sampling
from operations.serving.chat_request import EngineSignalRefusal
from operations.serving.errors import ChairResponseRefusal
from operations.serving.http import EndpointUnavailable

SENT_KIND: Final = "reader-sent"
SENT_SCHEMA: Final = "perlector-reader-sent.v1"
_SENT_FIELDS: Final = frozenset(
    {
        "schema",
        "act_key",
        "attempt_ordinal",
        "pass",
        "send",
        "receipt_ref",
        "concurrency",
        "image_sha256s",
    }
)


def start_chair(run) -> None:
    """Start the pass's one chair and keep its receipt reference on the pass.

    Every record the pass publishes names the receipt of the service that answered,
    which `ChairClient.__enter__` has checked names this chair and revision.
    """
    # Assigned before entering so `close` covers any failure from here on; closing an
    # unstarted client is a no-op.
    run.service.client = run.client_factory(run.context, run.chair, run.args.placement_tier)
    run.service.client.__enter__()
    run.receipt_ref = dict(run.service.client.handle.receipt_reference)


class BackgroundStart:
    """The pass's chair started on a background thread while the main thread prepares pages.

    `start_chair` only loads and launches; it publishes nothing a page record names
    except the receipt, which every live record reads from `run.receipt_ref` after
    `join`. A failed start is raised by `join` on the main thread.
    """

    def __init__(self, run) -> None:
        self._run = run
        self._error: BaseException | None = None
        self._began = time.monotonic()
        self._thread = threading.Thread(target=self._start, name="chair-start", daemon=True)
        self._thread.start()

    def _start(self) -> None:
        try:
            start_chair(self._run)
        except BaseException as error:  # noqa: BLE001 -- raised again by `join`
            self._error = error

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self._began)

    def join(self) -> None:
        """Wait for the start; raise its failure here, once."""
        self._thread.join()
        error, self._error = self._error, None
        if error is not None:
            raise error

    def join_quietly(self, raised: BaseException) -> None:
        """Wait for the start while `raised` propagates, so the chair can be stopped;
        a failed start is attached to `raised` rather than replacing it."""
        self._thread.join()
        error, self._error = self._error, None
        if error is not None:
            raised.add_note(f"the chair's start also failed: {type(error).__name__}: {error}")


def engine_call_inputs(context, engine_call: dict[str, Any] | None) -> list[dict[str, str]]:
    """Bind the two blobs a live reading's record names as direct inputs.

    A fixture reading has no `engine_call` and adds nothing. Each reference is
    re-derived from the bytes on disk, so a record cannot name a response that is
    absent or has changed, and the call record is held to the Perlector's sealed
    sampling row and to the serving receipt's seed.
    """
    if engine_call is None:
        return []
    if not isinstance(engine_call, dict) or set(engine_call) != {
        "call_record_ref",
        "raw_response_ref",
        "response_sha256",
        "finish_reason",
        "served_model_id",
    }:
        raise SchemaRefusal(f"a live reading's engine_call has the wrong shape: {engine_call!r}")
    if engine_call["response_sha256"] != engine_call["raw_response_ref"]["sha256"]:
        raise SchemaRefusal(
            "a live reading's engine_call names two different digests for one response: "
            f"response_sha256={engine_call['response_sha256']!r}, "
            f"raw_response_ref sha256={engine_call['raw_response_ref']['sha256']!r}"
        )
    references = []
    for name in ("raw_response_ref", "call_record_ref"):
        claimed = engine_call[name]
        observed = context.input_ref(claimed["relative_path"])
        if observed != dict(claimed):
            raise SchemaRefusal(
                f"a live reading's {name} names {claimed!r}, but the retained bytes at that "
                f"path are {observed!r}"
            )
        references.append(observed)
    call = retained_replies.json_object(
        context.tree.read_bytes(engine_call["call_record_ref"]["relative_path"])
    )
    if call is None:
        raise SchemaRefusal("a live reading's call record is not a JSON object")
    try:
        verify_retained_call_sampling(context, call, "perlector")
    except ContractError as error:
        raise SchemaRefusal(
            f"a live reading's call record is not its sealed request: {error}"
        ) from error
    return references


def _sent_id(subject_id: str, ordinal: int, pass_name: str, send: int) -> str:
    return artifact_id(
        PERLECTOR,
        SENT_KIND,
        subject_id,
        derived_attempt_id(subject_id, f"{pass_name}-sent:{ordinal}", send),
    )


def _validate_sent(payload: Any, *, key: str, ordinal: int, pass_name: str, send: int) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != _SENT_FIELDS
        or payload["schema"] != SENT_SCHEMA
        or (payload["act_key"], payload["attempt_ordinal"], payload["pass"], payload["send"])
        != (key, ordinal, pass_name, send)
        or not isinstance(payload["receipt_ref"], dict)
        or type(payload["concurrency"]) is not int
        or payload["concurrency"] < 1
        or not isinstance(payload["image_sha256s"], list)
        or not all(isinstance(value, str) for value in payload["image_sha256s"])
    ):
        raise SchemaRefusal(f"a reader-sent record for {key!r} is not its closed schema")


def sent_records(
    context, subject_id: str, key: str, ordinal: int, pass_name: str
) -> list[dict[str, Any]]:
    """Every send of one pass of this subject, numbered 1..N, in order."""
    records: list[dict[str, Any]] = []
    while True:
        identifier = _sent_id(subject_id, ordinal, pass_name, len(records) + 1)
        if not context.tree.has_artifact(PERLECTOR, SENT_KIND, identifier):
            return records
        record = context.tree.read_artifact(PERLECTOR, SENT_KIND, identifier)
        _validate_sent(
            record["payload"],
            key=key,
            ordinal=ordinal,
            pass_name=pass_name,
            send=len(records) + 1,
        )
        records.append(record)


def sent_refs(context, subject_id: str, key: str, ordinal: int, pass_name: str):
    """References to every send of one pass of this subject, in send order."""
    return [
        context.artifact_ref(PERLECTOR, SENT_KIND, record["artifact_id"])
        for record in sent_records(context, subject_id, key, ordinal, pass_name)
    ]


def publish_sent(
    run,
    subject_id: str,
    key: str,
    ordinal: int,
    pass_name: str,
    image_sha256s: list[str],
) -> None:
    """Record, before it leaves, that this call is being sent, and under what.

    A second send names the first, so a call re-sent after an interruption is visible.
    `concurrency` is the width of the window the call was sent in: how many reader calls
    this pass kept in flight at most, which bounds the batch the engine decoded it in.
    `image_sha256s` are the images the call carries, in the order it sends them. A
    page's send is keyed by its page key, recorded in the schema's `act_key` field.
    """
    earlier = sent_refs(run.context, subject_id, key, ordinal, pass_name)
    send = len(earlier) + 1
    run.context.publish(
        kind=SENT_KIND,
        subject_id=subject_id,
        outcome="read",
        attempt=derived_attempt_id(subject_id, f"{pass_name}-sent:{ordinal}", send),
        inputs=earlier + [dict(run.receipt_ref)],
        payload={
            "schema": SENT_SCHEMA,
            "act_key": key,
            "attempt_ordinal": ordinal,
            "pass": pass_name,
            "send": send,
            "receipt_ref": dict(run.receipt_ref),
            "concurrency": run.concurrency,
            "image_sha256s": list(image_sha256s),
        },
    )


def unrecorded_replies(context) -> tuple[list[dict[str, Any]], bool]:
    """Every retained reply no Perlector record binds (`common.retained_replies`)."""
    return retained_replies.unrecorded_replies(context, PERLECTOR)


def answers_a_send(calls: list[dict[str, Any]], markers: list[dict[str, Any]]) -> bool:
    """Whether an unbound reply came from one of these sends' sessions, about these images."""
    sent = [
        (record["payload"]["receipt_ref"], record["payload"]["image_sha256s"]) for record in markers
    ]
    return any((call.get("receipt_ref"), call.get("image_sha256s")) in sent for call in calls)


def refuse_past_deadline(
    deadline: datetime | None, seconds_needed: int, what: str, *, rate: str, remedy: str
) -> None:
    """Refuse work the reading deadline cannot hold, naming the planning rate and the way out."""
    if deadline is None:
        return
    remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining < seconds_needed:
        raise ContractError(
            f"{what} needs {seconds_needed}s at {rate}, but the reading deadline "
            f"{deadline.isoformat()} leaves {remaining}s; nothing more was started. {remedy}"
        )


# The failures of one call that `failure_record` records as the call's own outcome;
# any other error stops the pass.
CALL_FAILURES: Final = (
    EngineSignalRefusal,
    ChairResponseRefusal,
    EndpointUnavailable,
    serving_errors.ChairTransportFailure,
)

_NO_FAILURE_EVIDENCE: Final = {
    "raw_response_ref": None,
    "call_record_ref": None,
    "request_sha256": None,
    "receipt_ref": None,
    "served_model_id": None,
    "response_completion": None,
}


def _failure_facts(phase: str, kind: str, code: str, detail: str, **evidence) -> dict[str, Any]:
    return {
        "phase": phase,
        "kind": kind,
        "code": code,
        "detail": detail,
        **_NO_FAILURE_EVIDENCE,
        **evidence,
    }


def _copied_ref(reference: Mapping[str, str] | None) -> dict[str, str] | None:
    return dict(reference) if reference is not None else None


def _reported_call_evidence(error: Exception) -> dict[str, Any]:
    return {
        "call_record_ref": _copied_ref(getattr(error, "call_record_ref", None)),
        "request_sha256": getattr(error, "request_sha256", None),
        "receipt_ref": _copied_ref(getattr(error, "receipt_ref", None)),
        "served_model_id": getattr(error, "served_model_id", None),
    }


def failure_record(error: Exception, *, phase: str) -> dict[str, Any] | None:
    """Translate only observed engine and transport failures into retained facts.

    Contract and schema errors return `None`: recorded as an engine incident, one would
    let the stage seal over an integrity defect.
    """
    if isinstance(error, EngineSignalRefusal):
        return _failure_facts(
            phase,
            "engine-signal",
            error.code,
            error.detail,
            raw_response_ref=dict(error.raw_response_ref),
            call_record_ref=dict(error.call_record_ref),
            request_sha256=error.request_sha256,
            receipt_ref=dict(error.receipt_ref),
            served_model_id=error.served_model_id,
            response_completion="complete",
        )
    if isinstance(error, ChairResponseRefusal):
        return _failure_facts(
            phase,
            "chair-response",
            error.code,
            error.detail,
            raw_response_ref=_copied_ref(getattr(error, "raw_response_ref", None)),
            response_completion="complete",
            **_reported_call_evidence(error),
        )
    if isinstance(error, serving_errors.ChairTransportFailure):
        return _failure_facts(
            phase,
            "transport",
            error.code,
            error.detail,
            response_completion=getattr(error, "response_completion", None),
            **_reported_call_evidence(error),
        )
    if isinstance(error, EndpointUnavailable):
        return _failure_facts(
            phase,
            "transport",
            "endpoint-unavailable",
            str(error),
            response_completion="unknown",
        )
    return None
