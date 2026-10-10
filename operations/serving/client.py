"""The one client a stage holds for one chair's reading traffic.

A :class:`ChairClient` composes an already-built :class:`ServingManager`. It
never starts a pod, never picks a chair, and never retries, re-samples, or
edits a response. Every call is one request: raw bytes are retained before
they are parsed, the receipt is re-read and matched before any reading is
taken, and an engine's stop reason travels verbatim, never defaulted.

Which posture serves a chair (live vLLM, in-process, subprocess, or the
offline fixture) is ``serving_mode_for`` below: a three-name lookup in the
sealed serving-recipe catalogue, with a named refusal on zero, several, or an
unsupported match — never a fallback in any direction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, cast

from common.chair_wire import chandra_wire_fields
from common.chairs.models import ChairIdentity
from common.chandra_native_retry import CHANDRA_MAX_OUTPUT_TOKENS, attempt_parameters
from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.contracts.errors import ContractError
from common.contracts.serving import (
    CALLER_GENERATION_FIELDS,
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_CALL_RECORD_FIELDS,
    CHAIR_STREAM_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_SCHEMA,
    CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_PROBLEM_FIELDS,
    CHAIR_TRANSPORT_PROBLEM_SCHEMA,
    CHANDRA_NATIVE_CALL_RECORD_FIELDS,
    CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
)
from common.decoding import (
    STREAMED_WITNESS_CHAIRS,
    chair_attempt_decoding,
    chair_decoding,
    decoded_wire_decimals,
    engine_effective_sampling,
    perlector_loop_guard,
    recorded_wire_decimals,
    witness_loop_guard,
)
from common.repetition_loop import LoopScanner
from common.sealed_config import table_seal

from .config import (
    FixtureProfile,
    InProcessProfile,
    ServingProfile,
    ServingRecipes,
    SubprocessProfile,
    UnsupportedProfile,
    frozen_json,
    thawed_json,
)
from .errors import (
    ChairRequestRefusal,
    ChairResponseRefusal,
    ChairTransportFailure,
    ServingConfigurationError,
    ServingError,
)
from .http import (
    SSE_DONE,
    EndpointUnavailable,
    HttpResponse,
    chandra_native_request_body,
    chat_image_bytes_all,
    parse_openai_reading,
    parse_openai_stream_reading,
    peek_stream_model,
    request_body,
    sse_data,
    sse_events,
    stream_chunk_content,
)
from .manager import ServiceHandle, ServingManager

# One JSON serialization, used for both halves of the generation round-trip
# check below, so the comparison is between two texts rather than between two
# Python values whose `==` is looser than the wire's.
_JSON = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}


def _refuse_generation_that_cannot_be_recorded_as_sent(
    recorded: object, view: Mapping[str, object], field: str
) -> None:
    """Prove the recorded view re-encodes to the exact JSON the wire carried.

    A call record may claim what was sent only if it re-encodes to the wire
    bytes, so the client checks its own transcription on every call — before
    the record is written — and refuses rather than filing a request it cannot
    account for byte-for-byte. A tagged decimal that does not decode is
    vendor-carried evidence the client does not control, and gets the same
    named refusal.
    """

    try:
        recordable = json.dumps(decoded_wire_decimals(recorded), **_JSON) == json.dumps(
            dict(view), **_JSON
        )
    except ContractError:
        recordable = False
    if not recordable:
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"{field} cannot be recorded as the values that were sent; the call record would "
            "describe a request other than the one on the wire",
        )


class ReceiptDriftRefusal(ServingError):
    """The receipt re-read after start no longer names this chair's exact identity.

    Nothing may retroactively change what the receipt recorded, so this fires
    at the moment a client is about to start reading against it.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ServingModeRefusal(ServingError):
    """``serving_mode_for`` could not resolve one coherent serving posture.

    Every code here names a lookup outcome, never a ranking: zero rows, an
    unresolved tier, a catalogue mixing live and fixture rows for one chair,
    or a real chair with no honest implementation
    (:class:`~operations.serving.config.UnsupportedProfile`).
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class RetainBytes(Protocol):
    """Durably store content-addressed bytes; return their reference.

    Owned by the caller (a stage's ``StageContext``-backed writer in
    production, a small tmp-directory store in tests) so this module never
    decides where evidence lives — only that it is written before it is used.
    """

    def __call__(self, data: bytes) -> dict[str, str]:
        """Return ``{"relative_path": ..., "sha256": ...}`` for ``data``."""


def _sealed_capacity(value: Mapping[str, object]) -> Mapping[str, object]:
    """A detached, canonical, deep-frozen snapshot of one capacity record.

    Detached: the whole structure is rebuilt from canonical bytes, so no list
    or nested mapping the caller still holds is reachable from the request.
    Canonical: the round trip is through :func:`canonical_bytes`, the one
    writer every retained artifact goes through, so a record carrying a float,
    a non-string key, a cycle or an unencodable string is refused now — at the
    construction the caller can still see — rather than when the call record is
    serialized after the wire call has already happened. Frozen: mappings
    become ``MappingProxyType`` and sequences tuples, so a later write anywhere
    in the structure raises instead of silently rewriting admission evidence.
    """

    try:
        detached = json.loads(canonical_bytes(thawed_json(value)))
    except (TypeError, ValueError, RecursionError, ServingConfigurationError) as error:
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            "a request's capacity record cannot be written canonically, so it could not be "
            f"retained beside the call it admitted: {error}",
        ) from error
    if not isinstance(detached, dict):
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            "a request's capacity record must be a mapping of the arithmetic one request was "
            f"admitted on, not {type(detached).__name__}",
        )
    return frozen_json(detached)


@dataclass(frozen=True, slots=True)
class ChairRequest:
    """One reading request, built by the caller and refused, never repaired.

    ``generation_declared`` is the adapter's carried view, retained verbatim
    as evidence even though it is never sent; ``generation_sent`` is the
    caller's part of the wire body, limited to ``CALLER_GENERATION_FIELDS``.
    The client adds the chair's sealed sampling values and the seed, so a
    caller never names a sampling field, ``model``, ``stream``, ``seed`` or
    ``n``.

    ``capacity`` is the caller's own
    ``common.request_capacity`` record for this request against the sealed row
    it is about to be sent to. The client neither computes nor checks it — only
    the caller knows which prompt and which answer shape this call is — but it
    copies it onto the retained call record, so a run's receipts carry the
    arithmetic a request was admitted on beside the request itself. ``None``
    where the caller states none.

    ``loop_guard``, when given, streams the reply and abandons it at the first
    repetition loop (``common.repetition_loop``); it must be the sealed guard of
    the client's chair (``common.decoding.perlector_loop_guard`` for the Perlector,
    ``witness_loop_guard`` for a streamed witness chair), and the call is recorded
    under the stream schemas. ``None`` sends the plain, whole-response request.

    **The capacity record is sealed at construction, all the way down.** It is
    not flat -- it carries an ``images`` list of per-image dictionaries -- and
    every production builder keeps its own reference to the same object it
    passes in, so a caller touching a nested entry afterward could make the
    retained call record disagree with the evidence the request was actually
    admitted on. :func:`_sealed_capacity` takes a detached recursive snapshot
    instead, canonicalized through the same writer the call record uses (so an
    unwritable record is refused here, at construction, not later inside
    receipt serialization) and then deep-frozen.
    """

    kind: str
    messages: tuple[Mapping[str, object], ...]
    image_sha256s: tuple[str, ...]
    generation_declared: Mapping[str, object]
    generation_sent: Mapping[str, object]
    capacity: Mapping[str, object] | None = None
    loop_guard: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if self.loop_guard is not None:
            object.__setattr__(self, "loop_guard", MappingProxyType(dict(self.loop_guard)))
        object.__setattr__(self, "image_sha256s", tuple(self.image_sha256s))
        if self.capacity is not None:
            object.__setattr__(self, "capacity", _sealed_capacity(self.capacity))
        object.__setattr__(
            self, "generation_declared", MappingProxyType(dict(self.generation_declared))
        )
        object.__setattr__(self, "generation_sent", MappingProxyType(dict(self.generation_sent)))


@dataclass(frozen=True, slots=True)
class ChairResponse:
    """One retained reading. ``content`` is ``None`` only together with a
    ``parse_problem`` naming a ``CHAIR_RESPONSE_*`` code — the body arrived
    and is retained, but it is not a reading. ``finish_reason`` is not the
    same signal: it travels verbatim from the chair and is never defaulted,
    so it is also ``None`` on a successful reading whose engine reported no
    stop reason at all. ``parse_problem`` is the field a caller checks to
    tell a retained body apart from a reading; a stage records the
    engine-silent case as ``STOP_REASON_UNREPORTED``, not by inferring it
    from a ``None`` here.

    ``loop_stop`` is the repetition loop a streamed reply was abandoned on
    (``common.repetition_loop.LoopScanner.finding``); ``content`` is then the
    reply up to the stop, and ``finish_reason`` the engine's word, if any had
    arrived (as a rule none). ``None`` for every reply the engine ended itself."""

    chair: str
    served_model_id: str
    content: str | None
    finish_reason: str | None
    usage: Mapping[str, int] | None
    raw_response: bytes
    response_sha256: str
    request_sha256: str
    raw_response_ref: Mapping[str, str]
    call_record_ref: Mapping[str, str]
    receipt_ref: Mapping[str, str]
    launch_audit_ref: Mapping[str, str]
    parse_problem: str | None
    loop_stop: Mapping[str, object] | None = None


class _LoopWatch:
    """Reads a streamed reply's events as they arrive and says when to stop it.

    Each complete event's content goes to a `LoopScanner`; the read stops at the
    piece in which the scanner first finds a loop. An event this cannot read adds
    nothing here; the parser judges the whole stream once it is retained.
    """

    def __init__(self, guard: Mapping[str, int]) -> None:
        self.scanner = LoopScanner(guard)
        self._pending = b""

    def __call__(self, chunk: bytes) -> bool:
        events, self._pending = sse_events(self._pending + chunk)
        for event in events:
            try:
                data = sse_data(event)
            except UnicodeDecodeError:
                continue
            if data is None or data == SSE_DONE:
                continue
            if self.scanner.feed(stream_chunk_content(data)) is not None:
                return True
        return False


@dataclass(frozen=True, slots=True)
class ChandraNativeDispatch:
    """One pre-built physical request under the sealed Chandra capability.

    The stage persists an intent containing these facts before it hands this
    value back for dispatch.  Keeping the exact body here prevents a mutation
    between intent publication and HTTP from changing what that intent meant.
    """

    request: ChairRequest
    attempt_ordinal: int
    parameters: Mapping[str, str]
    generation_sent: Mapping[str, object]
    generation_sent_record: Mapping[str, object]
    generation_declared_record: Mapping[str, object]
    sampling_effective_record: Mapping[str, object]
    body: bytes
    request_sha256: str


class ChairClient:
    """The one client a stage holds for one chair, across every call in a pass.

    ``read_receipt`` is the tree's own receipt reader (production:
    ``context.tree.read_run_receipt``); the client never reads run-tree bytes
    itself. ``decoding_policy`` is the run's sealed decoding policy, checked at
    construction against ``decoding_config_sha256``; the client selects its
    chair's row by its own ``identity.role`` and sends it unchanged on each
    request with the manager-owned seed. Both are on every call record's
    ``generation_sent``, and ``sampling_effective`` beside them holds what the
    pinned engine samples under (``common.decoding.engine_effective_sampling``).

    The seed makes a sampled reading repeatable where the engine allows: vLLM
    draws a seeded request's samples from that request's own generator, so the
    same request on the same engine build, model and hardware draws the same
    samples. It does not promise bitwise-equal logits across batch compositions
    or kernels, so a repeat is reproducible in intent, and the call record keeps
    what this call received.
    """

    def __init__(
        self,
        *,
        manager: ServingManager,
        identity: ChairIdentity,
        tier: str,
        retain: RetainBytes,
        decoding_config_sha256: str,
        decoding_policy: Mapping[str, object],
        read_receipt: Callable[[Mapping[str, str]], Mapping[str, object]],
    ) -> None:
        if not is_sha256(decoding_config_sha256):
            raise ServingConfigurationError(
                "ChairClient requires the sealed decoding-policy digest as a lowercase SHA-256"
            )
        try:
            policy = dict(decoding_policy)
            chair_decoding(policy, identity.role)
            seal = table_seal(policy, "decoding configuration")
        except (ContractError, TypeError, ValueError) as error:
            raise ServingConfigurationError(
                f"ChairClient requires the run's sealed decoding policy: {error}"
            ) from error
        if seal != decoding_config_sha256:
            raise ServingConfigurationError(
                "ChairClient's decoding policy does not seal to the decoding digest it records"
            )
        self._manager = manager
        self._identity = identity
        self._tier = tier
        self._retain = retain
        self._decoding_config_sha256 = decoding_config_sha256
        self._decoding_policy = policy
        self._read_receipt = read_receipt
        # A native dispatch is a one-use capability minted only after every
        # request and evidence field has been checked.  Holding the object
        # itself in this private registry prevents a caller-created dataclass,
        # a dispatch prepared by another client, or a second use from crossing
        # the HTTP boundary merely because its public fields look plausible.
        self._prepared_chandra_dispatches: dict[int, ChandraNativeDispatch] = {}
        self._handle: ServiceHandle | None = None
        # Set once an exit has asked the service to stop; a service whose stop
        # failed is kept only so the exit can be retried, never read from.
        self._exiting = False

    @property
    def handle(self) -> ServiceHandle:
        if self._handle is None:
            raise ServingConfigurationError(
                "ChairClient has no active service; enter it as a context manager first"
            )
        if self._exiting:
            raise ServingConfigurationError(
                "ChairClient's service is being stopped; only a retried exit may use it"
            )
        return self._handle

    @property
    def identity(self) -> ChairIdentity:
        """The chair this client reads."""

        return self._identity

    def __enter__(self) -> "ChairClient":
        self._prepared_chandra_dispatches.clear()
        handle = self._manager.start(self._identity, self._tier)
        # Normalized here, at the one seam that knows both sides: a frozen
        # `MappingProxyType` reference is copied to a plain dict, since
        # `RunTree.read_run_receipt` accepts only its own type or a plain dict.
        try:
            expected_identity = {
                "chair": self._identity.role,
                "source": self._identity.source,
                "resolved": self._identity.source_reference,
                "revision": self._identity.receipt_revision,
                "revision_kind": self._identity.receipt_revision_kind,
                "digest_manifest": self._identity.digest_manifest,
            }
            receipt = self._read_receipt(dict(handle.receipt_reference))
            observed_identity = (
                {field: receipt.get(field) for field in expected_identity}
                if isinstance(receipt, Mapping)
                else None
            )
            if observed_identity != expected_identity:
                observed = (
                    f"identity={observed_identity!r}"
                    if observed_identity is not None
                    else f"a non-mapping receipt read: {receipt!r}"
                )
                raise ReceiptDriftRefusal(
                    "CHAIR_RECEIPT_DRIFT",
                    "the receipt re-read after start no longer names this chair's exact "
                    f"configured identity: observed {observed}; expected "
                    f"identity={expected_identity!r}",
                )
        except BaseException as receipt_error:
            # Reading and comparison are one post-start boundary. Any refusal
            # here owns the exact handle that was just started, including a
            # missing, corrupt or invalid receipt whose reader raises instead
            # of returning a value. Stop it before propagating that original
            # refusal; if shutdown is itself unverifiable, retain it as the
            # cause rather than replacing the receipt diagnosis; the manager
            # keeps the service, and `ServingManager.recover` retries its stop.
            try:
                handle.stop()
            except BaseException as stop_error:
                raise receipt_error from stop_error
            raise
        self._handle = handle
        self._exiting = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: object = None,
    ) -> None:
        if self._handle is None:
            return
        self._prepared_chandra_dispatches.clear()
        self._exiting = True
        # Cleared only once the stop is verified, so a failed stop can be retried.
        try:
            self._handle.stop()
        except BaseException as stop_error:
            # The error that ended the block names the page; an unverified stop
            # rides along as its cause rather than replacing it.
            if exc is not None:
                raise exc from stop_error
            raise
        self._handle = None
        self._exiting = False

    def hand_off(self) -> bool:
        """Leave the running service for the next stage's process instead of stopping it.

        `True` when the manager handed it off (`ServingManager.hand_off`); this
        client then holds nothing and its exit stops nothing. `False` leaves the
        service with this client, to be stopped by its exit as usual.
        """

        if self._handle is None or self._exiting:
            return False
        self._prepared_chandra_dispatches.clear()
        if not self._handle.hand_off():
            return False
        self._handle = None
        return True

    def reclaim_hand_off(self) -> Mapping[str, object] | None:
        """Stop a service an earlier stage handed off that this client never took over.

        For a stage that ends without starting its chair; see
        `ServingManager.reclaim_hand_off`.
        """

        if self._handle is not None:
            return None
        return self._manager.reclaim_hand_off()

    def read(self, request: ChairRequest) -> ChairResponse:
        """Issue exactly one reading request. Never retries, never re-samples.

        Order matches the contract exactly: request shape and image-digest
        refusals happen before anything is built or sent. Once dispatch is
        attempted, a transport failure retains the exact request facts and
        explicit response uncertainty. A received raw response is retained
        before its content is parsed; a content/choices problem is recorded,
        never raised, because a malformed body from a witness or reader is
        retained evidence (``parse_problem``), not a stage abort.
        """

        return self._read(request)

    def prepare_chandra_native(
        self, request: ChairRequest, *, attempt_ordinal: int
    ) -> ChandraNativeDispatch:
        """Build one exact Chandra request before its intent record is made."""

        handle = self.handle
        if (
            self._identity.role != "attestator_1"
            or self._identity.witness_adapter != "chandra.v1"
            or self._identity.witness_scope != "page"
        ):
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                "the Chandra native inference capability belongs only to Attestator 1's "
                "page-scoped chandra.v1 reading route",
            )
        _refuse_unbuildable_request(request)
        expected_wire = chandra_wire_fields()
        if request.generation_sent.get("chat_template_kwargs") != expected_wire[
            "chat_template_kwargs"
        ] or set(request.generation_sent) - {"chat_template_kwargs", "max_tokens"}:
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                "a Chandra native request must carry only the declared local "
                "enable_thinking compatibility field and its optional capacity-bound max_tokens",
            )
        if request.generation_sent.get("max_tokens", CHANDRA_MAX_OUTPUT_TOKENS) != (
            CHANDRA_MAX_OUTPUT_TOKENS
        ):
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                f"a Chandra native request moved the pinned {CHANDRA_MAX_OUTPUT_TOKENS}-token "
                "upstream bound",
            )
        if dict(request.generation_declared) != {"max_new_tokens": CHANDRA_MAX_OUTPUT_TOKENS}:
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                "a Chandra native request does not declare the pinned upstream output bound",
            )
        declared = attempt_parameters(attempt_ordinal)
        try:
            # The sealed policy's native recipe is validated with the policy, at
            # construction; its schedule is the attempt's temperature and top_p.
            sampling = chair_attempt_decoding(
                self._decoding_policy, self._identity.role, attempt_ordinal
            )
        except ContractError as error:
            raise ChairRequestRefusal("CHAIR_REQUEST_INVALID", str(error)) from error
        sent = {**request.generation_sent, **sampling}
        sent_record = recorded_wire_decimals(sent)
        declared_record = recorded_wire_decimals(request.generation_declared)
        _refuse_generation_that_cannot_be_recorded_as_sent(sent_record, sent, "generation_sent")
        _refuse_generation_that_cannot_be_recorded_as_sent(
            declared_record, request.generation_declared, "generation_declared"
        )
        body = chandra_native_request_body(
            {**request.generation_sent, "messages": list(request.messages)},
            model_id=handle.profile.served_model_id,
            sampling=sampling,
        )
        dispatch = ChandraNativeDispatch(
            request=request,
            attempt_ordinal=attempt_ordinal,
            parameters=MappingProxyType(declared),
            generation_sent=MappingProxyType(sent),
            generation_sent_record=cast(Mapping[str, object], frozen_json(sent_record)),
            generation_declared_record=cast(Mapping[str, object], frozen_json(declared_record)),
            sampling_effective_record=cast(
                Mapping[str, object],
                frozen_json(recorded_wire_decimals(engine_effective_sampling(sampling))),
            ),
            body=body,
            request_sha256=digest_bytes(body),
        )
        self._prepared_chandra_dispatches[id(dispatch)] = dispatch
        return dispatch

    def read_chandra_native(
        self,
        dispatch: ChandraNativeDispatch,
        *,
        intent_ref: Mapping[str, str],
    ) -> ChairResponse:
        """Issue the one pre-intended Chandra physical request; never loops."""

        if (
            not isinstance(intent_ref, Mapping)
            or set(intent_ref) != {"relative_path", "sha256"}
            or not isinstance(intent_ref.get("relative_path"), str)
            or not is_sha256(intent_ref.get("sha256"))
        ):
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID", "a Chandra native call has no durable attempt intent"
            )
        prepared = self._prepared_chandra_dispatches.pop(id(dispatch), None)
        if prepared is not dispatch:
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                "the Chandra native dispatch was not prepared by this client or was already used",
            )
        return self._read(
            prepared.request,
            native_dispatch=prepared,
            native_intent_ref=dict(intent_ref),
        )

    def _sampling_and_seed(self) -> tuple[dict[str, int | float], int]:
        """This request's sealed sampling values and seed, chosen by this client's chair."""

        try:
            sampling = chair_decoding(self._decoding_policy, self._identity.role)
        except ContractError as error:
            raise ChairRequestRefusal("CHAIR_REQUEST_INVALID", str(error)) from error
        return sampling, self.handle.profile.seed

    def _read(
        self,
        request: ChairRequest,
        *,
        native_dispatch: ChandraNativeDispatch | None = None,
        native_intent_ref: dict[str, str] | None = None,
    ) -> ChairResponse:
        """Shared one-call implementation; native use is an already-sealed capability."""

        handle = self.handle
        guard = self._loop_guard(request, native=native_dispatch is not None)
        if native_dispatch is None:
            _refuse_unbuildable_request(request)
            # Built and checked before the request leaves: a generation value
            # this client could not record as sent must stop the call, not be
            # discovered after a chair has already answered it.
            generation_sent = recorded_wire_decimals(request.generation_sent)
            generation_declared = recorded_wire_decimals(request.generation_declared)
            _refuse_generation_that_cannot_be_recorded_as_sent(
                generation_sent, request.generation_sent, "generation_sent"
            )
            _refuse_generation_that_cannot_be_recorded_as_sent(
                generation_declared, request.generation_declared, "generation_declared"
            )
            sampling, actual_seed = self._sampling_and_seed()
            actual_generation_sent = {
                **request.generation_sent,
                **sampling,
                "seed": actual_seed,
            }
            actual_generation_record = recorded_wire_decimals(actual_generation_sent)
            sampling_effective = recorded_wire_decimals(engine_effective_sampling(sampling))
            body = request_body(
                {**request.generation_sent, "messages": list(request.messages)},
                model_id=handle.profile.served_model_id,
                seed=actual_seed,
                deterministic=False,
                sampling=sampling,
                stream=guard is not None,
            )
        else:
            if native_intent_ref is None:
                raise ChairRequestRefusal(
                    "CHAIR_REQUEST_INVALID", "a Chandra native dispatch lost its intent"
                )
            actual_generation_record = thawed_json(native_dispatch.generation_sent_record)
            generation_declared = thawed_json(native_dispatch.generation_declared_record)
            sampling_effective = thawed_json(native_dispatch.sampling_effective_record)
            body = native_dispatch.body
        request_sha256 = (
            digest_bytes(body) if native_dispatch is None else native_dispatch.request_sha256
        )
        watch = _LoopWatch(guard) if guard is not None else None
        try:
            if watch is None:
                response = handle.request_reading(
                    request.kind, body, handle.profile.request_timeout_seconds
                )
            else:
                response = handle.stream_reading(
                    request.kind, body, handle.profile.request_timeout_seconds, watch
                )
        except EndpointUnavailable as error:
            transport_problem = {
                "schema": CHAIR_TRANSPORT_PROBLEM_SCHEMA,
                "code": "ENDPOINT_UNAVAILABLE",
                "detail": str(error),
                "definitively_absent": error.definitively_absent,
                "request_delivery": "unknown",
                "response_completion": "unknown",
            }
            if set(transport_problem) != CHAIR_TRANSPORT_PROBLEM_FIELDS:
                raise AssertionError(  # pragma: no cover - closed by construction
                    "chair transport problem built the wrong field set"
                ) from error
            failure_record = {
                "schema": (
                    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA
                    if native_dispatch is not None
                    else CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA
                    if guard is not None
                    else CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA
                ),
                "chair": self._identity.role,
                "resolved_identity": self._identity.to_record(),
                "resolved_revision": self._identity.receipt_revision,
                "serving_recipe": self._identity.serving_recipe,
                "served_model_id": handle.profile.served_model_id,
                "receipt_ref": dict(handle.receipt_reference),
                "launch_audit_ref": dict(handle.audit_reference),
                "decoding_config_sha256": self._decoding_config_sha256,
                "kind": request.kind,
                "request_sha256": request_sha256,
                "image_sha256s": list(request.image_sha256s),
                "generation_sent": actual_generation_record,
                "generation_declared": generation_declared,
                "sampling_effective": sampling_effective,
                "raw_response_ref": None,
                "response_sha256": None,
                "response_status": None,
                "response_model": None,
                "finish_reason": None,
                "usage": None,
                "parse_problem": None,
                "capacity": (
                    thawed_json(request.capacity) if request.capacity is not None else None
                ),
                "usage_reconciliation": None,
                "transport_problem": transport_problem,
            }
            if native_dispatch is not None:
                failure_record["native_attempt_intent_ref"] = native_intent_ref
            if guard is not None:
                failure_record["stream"] = _stream_record(guard, None)
            expected_failure_fields = (
                CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS
                if native_dispatch is not None
                else CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_FIELDS
                if guard is not None
                else CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS
            )
            if set(failure_record) != expected_failure_fields:
                raise AssertionError(  # pragma: no cover - closed by construction
                    "chair transport failure record built the wrong field set"
                ) from error
            call_record_ref = self._retain(canonical_bytes(failure_record))
            raise ChairTransportFailure(
                str(error),
                call_record_ref=call_record_ref,
                request_sha256=request_sha256,
                receipt_ref=handle.receipt_reference,
                served_model_id=handle.profile.served_model_id,
            ) from error
        # Retention comes first: vLLM's own refusal reason lives in the body of
        # a non-200, and dropping it before retaining would waste the one
        # artefact a rented card exists to produce. Retention is
        # not attribution -- a foreign-model body still never becomes a
        # reading -- but the bytes exist afterward so the refusal can name them.
        raw_response_ref = self._retain(response.body)
        loop_stop = watch.scanner.finding if watch is not None else None
        # A streamed 200 is server-sent events; anything else is one JSON body.
        peek = peek_stream_model if watch is not None and response.status == 200 else _peek_model
        early_refusal: ChairResponseRefusal | None = None
        try:
            _refuse_bytes_from_the_wrong_source(
                response,
                expected_model_id=handle.profile.served_model_id,
                raw_response_ref=raw_response_ref,
                peek=peek,
            )
        except ChairResponseRefusal as error:
            early_refusal = error

        content: str | None
        finish_reason: str | None = None
        usage: Mapping[str, int] | None = None
        parse_problem: str | None = None
        if early_refusal is not None:
            content = None
            parse_problem = early_refusal.code
        else:
            try:
                result = (
                    parse_openai_reading(
                        response,
                        kind=request.kind,
                        expected_model_id=handle.profile.served_model_id,
                    )
                    if watch is None
                    else parse_openai_stream_reading(
                        response,
                        kind=request.kind,
                        expected_model_id=handle.profile.served_model_id,
                        stopped=loop_stop is not None,
                    )
                )
            except ChairResponseRefusal as error:
                content = None
                parse_problem = error.code
                if parse_problem == "CHAIR_RESPONSE_MODEL_MISMATCH" and peek(response.body) is None:
                    # `_refuse_bytes_from_the_wrong_source` already let this body
                    # through retention because it names no model at all — that is
                    # a malformed body, not evidence of a foreign source, and the
                    # parser's own comparison (`payload.get("model") !=
                    # expected_model_id`) cannot tell the two apart. Recorded
                    # verbatim, "model mismatch" would assert a foreign-model
                    # observation that was never made.
                    parse_problem = "CHAIR_RESPONSE_INVALID"
            else:
                content = result.outputs[0]
                finish_reason = result.finish_reasons[0]
                usage = result.usage

        record = {
            "schema": (
                CHANDRA_NATIVE_CALL_RECORD_SCHEMA
                if native_dispatch is not None
                else CHAIR_STREAM_CALL_RECORD_SCHEMA
                if guard is not None
                else CHAIR_CALL_RECORD_SCHEMA
            ),
            "chair": self._identity.role,
            "resolved_identity": self._identity.to_record(),
            "resolved_revision": self._identity.receipt_revision,
            "serving_recipe": self._identity.serving_recipe,
            "served_model_id": handle.profile.served_model_id,
            "receipt_ref": dict(handle.receipt_reference),
            "launch_audit_ref": dict(handle.audit_reference),
            "decoding_config_sha256": self._decoding_config_sha256,
            "kind": request.kind,
            "request_sha256": request_sha256,
            "image_sha256s": list(request.image_sha256s),
            "generation_sent": actual_generation_record,
            "generation_declared": generation_declared,
            "sampling_effective": sampling_effective,
            "raw_response_ref": dict(raw_response_ref),
            "response_sha256": raw_response_ref["sha256"],
            "response_status": response.status,
            "response_model": peek(response.body),
            "finish_reason": finish_reason,
            "usage": dict(usage) if usage is not None else None,
            "parse_problem": parse_problem,
            # Thawed out of the sealed snapshot rather than out of whatever the
            # caller passed: the canonical writer holds dicts and lists, and
            # what is written is exactly the evidence the request carried.
            "capacity": thawed_json(request.capacity) if request.capacity is not None else None,
            "usage_reconciliation": usage_against_capacity(usage, request.capacity),
        }
        if native_dispatch is not None:
            record["native_attempt_intent_ref"] = native_intent_ref
        if guard is not None:
            record["stream"] = _stream_record(guard, loop_stop)
        expected_record_fields = (
            CHANDRA_NATIVE_CALL_RECORD_FIELDS
            if native_dispatch is not None
            else CHAIR_STREAM_CALL_RECORD_FIELDS
            if guard is not None
            else CHAIR_CALL_RECORD_FIELDS
        )
        if set(record) != expected_record_fields:
            raise AssertionError(  # pragma: no cover - closed by construction above
                f"chair call record built the wrong field set: {sorted(record)}"
            )
        call_record_ref = self._retain(canonical_bytes(record))

        if early_refusal is not None:
            raise ChairResponseRefusal(
                early_refusal.code,
                early_refusal.detail,
                raw_response_ref=raw_response_ref,
                call_record_ref=call_record_ref,
                request_sha256=request_sha256,
                receipt_ref=handle.receipt_reference,
                served_model_id=handle.profile.served_model_id,
            ) from early_refusal

        return ChairResponse(
            chair=self._identity.role,
            served_model_id=handle.profile.served_model_id,
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            raw_response=response.body,
            response_sha256=raw_response_ref["sha256"],
            request_sha256=request_sha256,
            raw_response_ref=raw_response_ref,
            call_record_ref=call_record_ref,
            receipt_ref=dict(handle.receipt_reference),
            launch_audit_ref=dict(handle.audit_reference),
            parse_problem=parse_problem,
            loop_stop=MappingProxyType(dict(loop_stop)) if loop_stop is not None else None,
        )

    def _loop_guard(self, request: ChairRequest, *, native: bool) -> dict[str, int] | None:
        """The sealed guard a streamed request is watched under, or ``None`` for a plain one.

        A request may ask only for its chair's sealed guard: the Perlector's, or a
        streamed witness chair's; a Chandra native request is never streamed.
        """

        if request.loop_guard is None:
            return None
        role = self._identity.role
        try:
            if native:
                sealed = None
            elif role == "perlector":
                sealed = perlector_loop_guard(self._decoding_policy)
            elif role in STREAMED_WITNESS_CHAIRS:
                sealed = witness_loop_guard(self._decoding_policy, role)
            else:
                sealed = None
        except ContractError as error:
            raise ChairRequestRefusal("CHAIR_REQUEST_INVALID", str(error)) from error
        if sealed is None or dict(request.loop_guard) != sealed:
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                f"a streamed request's loop guard {dict(request.loop_guard)!r} is not the sealed "
                f"repetition-loop guard of chair {self._identity.role!r}",
            )
        return sealed


def _stream_record(
    guard: Mapping[str, int], stopped: Mapping[str, object] | None
) -> dict[str, object]:
    """The call record's ``stream``: the guard watched under, and the loop that stopped it."""

    return {
        "schema": CHAIR_STREAM_SCHEMA,
        "loop_guard": dict(guard),
        "stopped": dict(stopped) if stopped is not None else None,
    }


def usage_against_capacity(
    usage: Mapping[str, object] | None, capacity: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The engine's own token counts set beside the counts the request was admitted on.

    A served image is resized by the engine under the row's pixel bounds; if
    those bounds never reached it, the page is read at another scale and the
    reading itself shows no error. The engine's reported image-token count is
    the one place that shows, so each call record carries the comparison and
    names what disagrees. It is evidence only: the reading is kept as it came.

    `None` when there is nothing to compare: no capacity record (readiness
    probe, smoke) or no parsed usage (the parser keeps usage only when its three
    counters are counts). Findings:
    - `image-tokens-differ`: the engine counted other image tokens than the
      resize arithmetic predicts.
    - `image-tokens-unreported`: the request carried images and the engine gave
      no per-modality count.
    - `prompt-tokens-above-admitted`: the engine counted more prompt tokens than
      admission allowed for, so the capacity check rested on an undercount.
    """

    if usage is None or capacity is None:
        return None
    expected_image = capacity.get("image_prompt_tokens")
    text_bound = capacity.get("prompt_tokens")
    if not _is_count(expected_image) or not _is_count(text_bound):
        return None
    observed_prompt = cast(int, usage["prompt_tokens"])
    observed_image = _reported_image_tokens(usage)
    admitted_prompt = expected_image + text_bound
    findings = []
    if observed_image is None:
        if expected_image > 0:
            findings.append("image-tokens-unreported")
    elif observed_image != expected_image:
        findings.append("image-tokens-differ")
    if observed_prompt > admitted_prompt:
        findings.append("prompt-tokens-above-admitted")
    return {
        "expected_image_tokens": expected_image,
        "observed_image_tokens": observed_image,
        "admitted_prompt_tokens": admitted_prompt,
        "observed_prompt_tokens": observed_prompt,
        "findings": findings,
    }


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reported_image_tokens(usage: Mapping[str, object]) -> int | None:
    """vLLM's `usage.prompt_tokens_details.multimodal_tokens.image`, or None.

    The engine adds it when launched with `--enable-prompt-tokens-details` and
    the request carried an image.
    """

    details = usage.get("prompt_tokens_details")
    if not isinstance(details, Mapping):
        return None
    multimodal = details.get("multimodal_tokens")
    if not isinstance(multimodal, Mapping):
        return None
    value = multimodal.get("image")
    return value if _is_count(value) else None


def _refuse_unbuildable_request(request: ChairRequest) -> None:
    """Every refusal here happens before a byte is built or sent."""

    if request.kind != "chat-completions":
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"reading kind {request.kind!r} is not supported; vision chairs are chat-completions only",
        )
    refused = sorted(set(request.generation_sent) - CALLER_GENERATION_FIELDS)
    if refused:
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"generation_sent must not name {refused}; a caller may send only "
            f"{sorted(CALLER_GENERATION_FIELDS)}, and sampling values are the sealed decoding "
            "table's to set",
        )
    for field, view in (
        ("generation_sent", request.generation_sent),
        ("generation_declared", request.generation_declared),
    ):
        # `NaN`/`Infinity` are not JSON. Python's encoder emits them anyway, so
        # a request carrying one would put a body on the wire that no
        # conforming reader can parse and no record can transcribe. Refused
        # here, before the body exists, rather than substituted with a finite
        # number nobody declared.
        if _nonfinite_path(view) is not None:
            raise ChairRequestRefusal(
                "CHAIR_REQUEST_INVALID",
                f"{field}{_nonfinite_path(view)} is not a finite number; NaN and Infinity are "
                "not JSON and cannot be sent or recorded",
            )
    actual = tuple(
        digest_bytes(data) for data in chat_image_bytes_all({"messages": list(request.messages)})
    )
    if actual != request.image_sha256s:
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"request image digests {list(actual)} do not match the claimed image_sha256s "
            f"{list(request.image_sha256s)}, exactly and in order",
        )


def _nonfinite_path(value: object, path: str = "") -> str | None:
    """Where the first non-finite float sits, in a form a refusal can name."""

    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return path if (value != value or value in (float("inf"), float("-inf"))) else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (found := _nonfinite_path(item, f"{path}[{key!r}]")) is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if (found := _nonfinite_path(item, f"{path}[{index}]")) is not None:
                return found
    return None


# How much of a refused body travels in the refusal's own detail. Enough to
# carry vLLM's own overflow sentence, which is the artefact a real run is
# paying for; short enough that a megabyte of HTML from something that is not
# vLLM at all cannot be pasted into a traceback. The whole body is retained
# either way and the refusal names where. Used on the non-200 branch alone:
# a 200 from the wrong model carries a foreign reading, and its words do not
# go into an exception message.
_REFUSAL_BODY_PREVIEW_BYTES = 512


def _body_preview(body: bytes) -> str:
    """The head of a refused body, decoded loosely, for the refusal's detail."""

    head = body[:_REFUSAL_BODY_PREVIEW_BYTES]
    text = head.decode("utf-8", errors="replace")
    if len(body) > _REFUSAL_BODY_PREVIEW_BYTES:
        return f"{text!r} (first {_REFUSAL_BODY_PREVIEW_BYTES} of {len(body)} bytes)"
    return f"{text!r} ({len(body)} bytes)"


def _refuse_bytes_from_the_wrong_source(
    response: HttpResponse,
    *,
    expected_model_id: str,
    raw_response_ref: Mapping[str, str],
    peek: Callable[[bytes], str | None] | None = None,
) -> None:
    """Refuse a response that is not this chair's, with its bytes already retained.

    Narrower than :func:`~operations.serving.http.parse_openai_reading`: it
    checks only status and, when the body names a model, that name. Anything
    else (unparseable, or no ``model`` field) is left for the full parse, since
    that is legitimate evidence for a malformed reading, not a foreign source.
    ``raw_response_ref`` names the already-written blob, carried by both
    refusals. Only the non-200 quotes the body: that is the engine's own
    refusal reason, while a 200 from the wrong model is a foreign reading whose
    words never enter an exception message -- named by digest instead.
    """

    if response.status != 200:
        raise ChairResponseRefusal(
            "CHAIR_RESPONSE_HTTP_ERROR",
            f"reading response returned HTTP {response.status}; its body is retained at "
            f"{dict(raw_response_ref)!r} and begins {_body_preview(response.body)}",
            raw_response_ref=raw_response_ref,
        )
    model = (peek or _peek_model)(response.body)
    if model is not None and model != expected_model_id:
        raise ChairResponseRefusal(
            "CHAIR_RESPONSE_MODEL_MISMATCH",
            f"reading response model={model!r}, expected {expected_model_id!r}; its "
            f"{len(response.body)} bytes are retained at {dict(raw_response_ref)!r} and are "
            "not quoted here: a reading from another model is not this chair's evidence and "
            "does not travel in a refusal message",
            raw_response_ref=raw_response_ref,
        )


def _peek_model(body: bytes) -> str | None:
    """The response's declared ``model``, or ``None`` when it cannot be read.

    Never raises: an unparseable body or a missing field is exactly the shape
    a malformed reading is allowed to have. The result decides the
    wrong-source refusal; it separates a real foreign-model observation from a
    body that named no model at all, which the parser's own comparison cannot
    tell apart; and it fills ``response_model`` on the call record, which is
    simply ``null`` for such a body. Each of these uses runs *after* the bytes
    are retained.
    """

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


def _other_tiers_posture(
    rows: tuple[
        "ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile",
        ...,
    ],
    tier: str,
) -> str:
    """Name the posture(s) the *other* tiers hold, for a mixed-posture refusal.

    Never assumes "live": the other tiers may just as well be unsupported, so
    the message names whatever kind is actually sitting there rather than a
    fixed guess a reader would then have to disbelieve.
    """

    others = tuple(row for row in rows if row.tier != tier)
    kinds = {
        "live"
        if isinstance(row, ServingProfile)
        else type(row).__name__.removesuffix("Profile").lower()
        for row in others
    }
    if len(kinds) == 1:
        return f"another tier is {next(iter(kinds))}"
    return f"other tiers are {sorted(kinds)}"


def serving_mode_for(recipes: ServingRecipes, identity: ChairIdentity, tier: str | None) -> str:
    """``"fixture"``, ``"live"``, ``"in-process"`` or ``"subprocess"`` by row kind alone.

    Three-name lookup, never a ranking: every row for this ``(recipe, chair)``
    is collected first. If every one of them is a fixture row, the chair is
    fixture regardless of a supplied tier. Otherwise a tier is required and
    the row at that exact tier decides: a vLLM row is ``"live"``, an in-process
    or subprocess row names its own posture, and an unsupported row, or a
    fixture row beside non-fixture ones, is refused. There is no fallback to
    another tier or to fixture in either direction.
    """

    rows = tuple(
        profile
        for profile in recipes.profiles
        if profile.recipe == identity.serving_recipe and profile.chair == identity.role
    )
    if not rows:
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            f"no serving profile is configured for chair={identity.role!r}, "
            f"recipe={identity.serving_recipe!r}",
        )
    if all(isinstance(row, FixtureProfile) for row in rows):
        return "fixture"
    if tier is None:
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            "a live serving profile needs the measured placement tier; pass --placement-tier",
        )
    try:
        profile = recipes.for_identity(identity, tier)
    except ServingConfigurationError as error:
        # A chair with rows elsewhere but none at exactly this tier (a mistyped
        # or unmeasured --placement-tier) is refused as ServingModeRefusal, like
        # every other outcome of this lookup.
        raise ServingModeRefusal("SERVING_MODE_UNRESOLVED", str(error)) from error
    if isinstance(profile, ServingProfile):
        return "live"
    if isinstance(profile, InProcessProfile):
        return "in-process"
    if isinstance(profile, SubprocessProfile):
        return "subprocess"
    if isinstance(profile, FixtureProfile):
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            f"chair={identity.role!r}, recipe={identity.serving_recipe!r} is a fixture row at "
            f"tier={tier!r} while {_other_tiers_posture(rows, tier)} in this catalogue; a "
            "catalogue may not be half fixture for one chair",
        )
    if isinstance(profile, UnsupportedProfile):
        raise ServingModeRefusal("SERVING_MODE_UNSUPPORTED", profile.reason)
    raise AssertionError(  # pragma: no cover - config.py closes the profile union
        f"unreachable serving profile kind {type(profile).__name__}"
    )
