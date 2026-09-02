"""The one client a stage holds for one chair's reading traffic.

A :class:`ChairClient` composes an already-built :class:`ServingManager`. It
never starts a pod, never picks a chair, and never retries, re-samples, or
edits a response (GOVERNANCE 7). Every call is one request: raw bytes are
retained before they are parsed (GOVERNANCE 2), the receipt is re-read and
matched before any reading is taken (GOVERNANCE 6), and an engine's stop
reason travels verbatim, never defaulted (GOVERNANCE 10).

Selection between a live chair and the offline fixture posture is
``serving_mode_for`` below: a three-name lookup in the sealed serving-recipe
catalogue, with a named refusal on zero, several, or an unsupported match —
never a fallback in either direction (GOVERNANCE 3 / hard rule 8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from common.chairs.models import ChairIdentity
from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.contracts.serving import CHAIR_CALL_RECORD_FIELDS, CHAIR_CALL_RECORD_SCHEMA

from .config import FixtureProfile, ServingProfile, ServingRecipes, UnsupportedProfile
from .errors import (
    ChairRequestRefusal,
    ChairResponseRefusal,
    ServingConfigurationError,
    ServingError,
)
from .http import HttpResponse, chat_image_bytes_all, parse_openai_reading, request_body
from .manager import AdapterCalibration, ServiceHandle, ServingManager

# Never on the wire: these are the manager's/decoding policy's to set, not an
# adapter's or a stage's. A caller that names one is refused before anything
# is built or sent.
_FORBIDDEN_GENERATION_SENT_KEYS = frozenset({"model", "stream", "temperature", "seed", "n"})


class ReceiptDriftRefusal(ServingError):
    """The receipt re-read after start no longer names this chair and revision.

    Not defined in :mod:`operations.serving.errors` because U1 did not need
    it: nothing before this client ever re-read a receipt back through the
    tree it had just written. The check itself is the guarantee GOVERNANCE 6
    asks for — "the record itself protects the past" — applied at the moment
    a client is about to start reading against it.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ServingModeRefusal(ServingError):
    """``serving_mode_for`` could not resolve one coherent serving posture.

    Also not in U1's :mod:`operations.serving.errors`: the manager never had
    to decide *whether* a chair is live before starting it. Every code here
    names a lookup outcome, never a ranking: zero rows, an unresolved tier, a
    catalogue mixing live and fixture rows for one chair, or a real chair with
    no honest implementation (:class:`~operations.serving.config.UnsupportedProfile`).
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


@dataclass(frozen=True, slots=True)
class ChairRequest:
    """One reading request, built by the caller and refused, never repaired.

    ``generation_declared`` is the adapter's carried view, retained verbatim
    as evidence even though it is never sent; ``generation_sent`` is what
    actually goes on the wire, and may never name ``model``, ``stream``,
    ``temperature``, ``seed``, or ``n`` — those are the manager's and the
    decoding policy's alone (GOVERNANCE 7).
    """

    kind: str
    messages: tuple[Mapping[str, object], ...]
    image_sha256s: tuple[str, ...]
    generation_declared: Mapping[str, object]
    generation_sent: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "image_sha256s", tuple(self.image_sha256s))
        object.__setattr__(
            self, "generation_declared", MappingProxyType(dict(self.generation_declared))
        )
        object.__setattr__(self, "generation_sent", MappingProxyType(dict(self.generation_sent)))


@dataclass(frozen=True, slots=True)
class ChairResponse:
    """One retained reading. ``content``/``finish_reason`` are ``None`` only
    together with a ``parse_problem`` naming a ``CHAIR_RESPONSE_*`` code — the
    body arrived and is retained, but it is not a reading."""

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


class ChairClient:
    """The one client a stage holds for one chair, across every call in a pass.

    ``read_receipt`` is the tree's own receipt reader (production:
    ``context.tree.read_run_receipt``); the client never reads run-tree bytes
    itself. ``record_temperature`` is checked once, at construction: this
    client only ever records the sealed reading-of-record temperature (0), so
    a caller that would build it against another policy is refused before any
    chair starts, not silently coerced.
    """

    def __init__(
        self,
        *,
        manager: ServingManager,
        identity: ChairIdentity,
        tier: str,
        retain: RetainBytes,
        decoding_config_sha256: str,
        record_temperature: int,
        read_receipt: Callable[[Mapping[str, str]], Mapping[str, object]],
        adapter_calibration: AdapterCalibration | None = None,
    ) -> None:
        if record_temperature != 0:
            raise ServingConfigurationError(
                "ChairClient only ever records the sealed reading-of-record "
                f"temperature 0; the caller supplied record_temperature={record_temperature!r}. "
                "config/decoding.toml pins reading_of_record.temperature = 0; a caller "
                "that disagrees must refuse here, never be silently recorded as 0."
            )
        if not is_sha256(decoding_config_sha256):
            raise ServingConfigurationError(
                "ChairClient requires the sealed decoding-policy digest as a lowercase SHA-256"
            )
        self._manager = manager
        self._identity = identity
        self._tier = tier
        self._retain = retain
        self._decoding_config_sha256 = decoding_config_sha256
        self._record_temperature = record_temperature
        self._read_receipt = read_receipt
        self._adapter_calibration = adapter_calibration
        self._handle: ServiceHandle | None = None

    @property
    def handle(self) -> ServiceHandle:
        if self._handle is None:
            raise ServingConfigurationError(
                "ChairClient has no active service; enter it as a context manager first"
            )
        return self._handle

    def __enter__(self) -> "ChairClient":
        handle = self._manager.start(
            self._identity, self._tier, adapter_calibration=self._adapter_calibration
        )
        receipt = self._read_receipt(handle.receipt_reference)
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("chair") != self._identity.role
            or receipt.get("revision") != self._identity.receipt_revision
        ):
            handle.stop()
            observed = (
                f"chair={receipt.get('chair')!r}, revision={receipt.get('revision')!r}"
                if isinstance(receipt, Mapping)
                else f"a non-mapping receipt read: {receipt!r}"
            )
            raise ReceiptDriftRefusal(
                "CHAIR_RECEIPT_DRIFT",
                "the receipt re-read after start no longer names this chair and revision: "
                f"observed {observed}; expected chair={self._identity.role!r}, "
                f"revision={self._identity.receipt_revision!r}",
            )
        self._handle = handle
        return self

    def __exit__(self, *exc: object) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        handle.stop()

    def read(self, request: ChairRequest) -> ChairResponse:
        """Issue exactly one reading request. Never retries, never re-samples.

        Order matches the contract exactly: request shape and image-digest
        refusals happen before anything is built or sent; the raw response is
        retained before its content is parsed; a content/choices problem is
        recorded, never raised, because a malformed body from a witness or
        reader is retained evidence (``parse_problem``), not a stage abort.
        """

        handle = self.handle
        _refuse_unbuildable_request(request)
        body = request_body(
            {**request.generation_sent, "messages": list(request.messages)},
            model_id=handle.profile.served_model_id,
            seed=handle.profile.seed,
            deterministic=True,
        )
        request_sha256 = digest_bytes(body)
        response = handle.request_reading(
            request.kind, body, handle.profile.request_timeout_seconds
        )
        _refuse_bytes_from_the_wrong_source(
            response, expected_model_id=handle.profile.served_model_id
        )
        raw_response_ref = self._retain(response.body)

        content: str | None
        finish_reason: str | None = None
        usage: Mapping[str, int] | None = None
        parse_problem: str | None = None
        try:
            result = parse_openai_reading(
                response, kind=request.kind, expected_model_id=handle.profile.served_model_id
            )
        except ChairResponseRefusal as error:
            content = None
            parse_problem = error.code
        else:
            content = result.outputs[0]
            finish_reason = result.finish_reasons[0]
            usage = result.usage

        record = {
            "schema": CHAIR_CALL_RECORD_SCHEMA,
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
            "generation_sent": dict(request.generation_sent),
            "generation_declared": dict(request.generation_declared),
            "raw_response_ref": dict(raw_response_ref),
            "response_sha256": raw_response_ref["sha256"],
            "response_model": _peek_model(response.body),
            "finish_reason": finish_reason,
            "usage": dict(usage) if usage is not None else None,
            "parse_problem": parse_problem,
        }
        if set(record) != CHAIR_CALL_RECORD_FIELDS:
            raise AssertionError(  # pragma: no cover - closed by construction above
                f"chair-call-record.v1 built the wrong field set: {sorted(record)}"
            )
        call_record_ref = self._retain(canonical_bytes(record))

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
        )


def _refuse_unbuildable_request(request: ChairRequest) -> None:
    """Every refusal here happens before a byte is built or sent."""

    if request.kind != "chat-completions":
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"reading kind {request.kind!r} is not supported; vision chairs are chat-completions only",
        )
    forbidden = sorted(_FORBIDDEN_GENERATION_SENT_KEYS & set(request.generation_sent))
    if forbidden:
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"generation_sent must not name {forbidden}; those fields are manager-owned",
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


def _refuse_bytes_from_the_wrong_source(response: HttpResponse, *, expected_model_id: str) -> None:
    """Refuse before retention: bytes from another model are not this chair's evidence.

    Deliberately narrower than :func:`~operations.serving.http.parse_openai_reading`
    — it checks only status and, when the body parses as a JSON object naming a
    model, that name. Anything else (an unparseable body, one with no ``model``
    field) is left for the full parse after retention, because that is
    legitimate retained evidence for a malformed reading, not evidence from
    somewhere else.
    """

    if response.status != 200:
        raise ChairResponseRefusal(
            "CHAIR_RESPONSE_HTTP_ERROR", f"reading response returned HTTP {response.status}"
        )
    model = _peek_model(response.body)
    if model is not None and model != expected_model_id:
        raise ChairResponseRefusal(
            "CHAIR_RESPONSE_MODEL_MISMATCH",
            f"reading response model={model!r}, expected {expected_model_id!r}",
        )


def _peek_model(body: bytes) -> str | None:
    """The response's declared ``model``, or ``None`` when it cannot be read.

    Never raises: an unparseable body or a missing field is exactly the shape
    a malformed reading is allowed to have, and this helper is used both
    before retention (where that must not raise) and while building the call
    record (where ``response_model`` is simply ``null`` for such a body).
    """

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


def serving_mode_for(recipes: ServingRecipes, identity: ChairIdentity, tier: str | None) -> str:
    """``"fixture"`` or ``"live"`` by the sealed serving-recipe row kind alone.

    Three-name lookup, never a ranking: every row for this ``(recipe, chair)``
    is collected first. If every one of them is a fixture row, the chair is
    fixture regardless of a supplied tier. Otherwise a live posture exists
    somewhere in the catalogue, so a tier is required; the row at that exact
    tier decides, with no fallback to another tier or to fixture in either
    direction (GOVERNANCE 3 / hard rule 8).
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
    profile = recipes.for_identity(identity, tier)
    if isinstance(profile, ServingProfile):
        return "live"
    if isinstance(profile, FixtureProfile):
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            f"chair={identity.role!r}, recipe={identity.serving_recipe!r} is a fixture row at "
            f"tier={tier!r} while another tier in this catalogue is live; a catalogue may not "
            "be half live for one chair",
        )
    if isinstance(profile, UnsupportedProfile):
        raise ServingModeRefusal("SERVING_MODE_UNSUPPORTED", profile.reason)
    raise AssertionError(  # pragma: no cover - config.py closes the profile union
        f"unreachable serving profile kind {type(profile).__name__}"
    )
