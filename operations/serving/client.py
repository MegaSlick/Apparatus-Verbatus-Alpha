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
from common.contracts.serving import (
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_SCHEMA,
    WIRE_DECIMAL_FIELDS,
    WIRE_DECIMAL_SCHEMA,
)

from .config import (
    AnyProfile,
    CapturedProfile,
    FixtureProfile,
    ServingProfile,
    ServingRecipes,
    UnsupportedProfile,
)
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

# One JSON serialization, used for both halves of the generation round-trip
# check below, so the comparison is between two texts rather than between two
# Python values whose `==` is looser than the wire's.
_JSON = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}

# A sentinel distinct from every legitimate decoded generation value (always a
# JSON-native dict, list, string, number, bool, or None), so the malformed-
# decimal branch below can force a mismatch without risking a coincidental
# equality against `None`.
_UNRECORDABLE = object()


def _recorded_generation(view: Mapping[str, object]) -> dict[str, object]:
    """The generation view in a form the canonical writer can hold, losslessly.

    A vendor's decoding value may be a float — DAI's carried
    ``generation_config.json`` names ``repetition_penalty`` 1.05 and ``top_p``
    0.001 — and :func:`common.contracts.canonical.canonical_bytes` refuses
    floats outright, so a call record that simply carried them could not be
    written at all. That refusal is right and is not loosened here: a float's
    JSON form is not stable enough to hash against.

    What is recorded instead is the exact decimal text the request body itself
    carries for that value, tagged with ``wire-decimal.v1`` so a reader can
    tell it from a string the vendor genuinely declared. ``json.dumps`` emits
    the shortest text that reads back as the identical double, which is why
    this is a transcription rather than a rounding: :func:`_decoded_generation`
    returns the very same value, and ``read`` proves that on every call before
    it writes the record.
    """

    return {key: _recorded_value(item) for key, item in view.items()}


def _recorded_value(value: object) -> object:
    if isinstance(value, float) and not isinstance(value, bool):
        return {"schema": WIRE_DECIMAL_SCHEMA, "decimal": json.dumps(value)}
    if isinstance(value, Mapping):
        return {key: _recorded_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_recorded_value(item) for item in value]
    return value


class _UnrecordableWireDecimal(Exception):
    """A tagged ``wire-decimal.v1`` form whose ``decimal`` text is not a number.

    Raised inside :func:`_decoded_generation`, never let escape past
    :func:`_refuse_generation_that_cannot_be_recorded_as_sent`: a malformed
    tagged form is vendor-carried evidence the client does not control, and it
    must surface as the same named ``CHAIR_REQUEST_INVALID`` refusal every
    other unrecordable generation value gets, not as a bare exception out of
    the client's own decoding check.
    """


def _decoded_generation(value: object) -> object:
    """The inverse of :func:`_recorded_generation`, used to check it, not to trust it."""

    if isinstance(value, dict):
        if set(value) == WIRE_DECIMAL_FIELDS and value.get("schema") == WIRE_DECIMAL_SCHEMA:
            try:
                return float(value["decimal"])
            except (TypeError, ValueError) as error:
                raise _UnrecordableWireDecimal(repr(value["decimal"])) from error
        return {key: _decoded_generation(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decoded_generation(item) for item in value]
    return value


def _refuse_generation_that_cannot_be_recorded_as_sent(
    recorded: object, view: Mapping[str, object], field: str
) -> None:
    """Prove the recorded view re-encodes to the exact JSON the wire carried.

    Not a formality: it is the whole claim gap 1 of the Attestatores HANDOFF
    turns on. The record may say what was sent only if it can be shown to say
    it, so the client checks its own transcription on every call — before the
    record is written — and refuses rather than filing a request it cannot
    account for byte-for-byte.
    """

    try:
        decoded: object = _decoded_generation(recorded)
    except _UnrecordableWireDecimal:
        # A decimal text that cannot be parsed back is unrecordable outright:
        # there is no decoded form to compare, so the sentinel below never
        # equals a real view and the refusal below always fires.
        decoded = _UNRECORDABLE
    if decoded is _UNRECORDABLE or json.dumps(decoded, **_JSON) != json.dumps(  # type: ignore[arg-type]
        dict(view), **_JSON
    ):
        raise ChairRequestRefusal(
            "CHAIR_REQUEST_INVALID",
            f"{field} cannot be recorded as the values that were sent; the call record would "
            "describe a request other than the one on the wire",
        )


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
    """One retained reading. ``content`` is ``None`` only together with a
    ``parse_problem`` naming a ``CHAIR_RESPONSE_*`` code — the body arrived
    and is retained, but it is not a reading. ``finish_reason`` is not the
    same signal: it travels verbatim from the chair and is never defaulted,
    so it is also ``None`` on a successful reading whose engine reported no
    stop reason at all. ``parse_problem`` is the field a caller checks to
    tell a retained body apart from a reading; a stage records the
    engine-silent case as ``STOP_REASON_UNREPORTED``, not by inferring it
    from a ``None`` here."""

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
        # Normalized here, at the one seam that knows both sides.
        # `ReceiptPublication` freezes its reference into a `MappingProxyType`
        # so nothing can edit a published provenance record; `RunTree.
        # read_run_receipt` accepts its own reference type or a plain `dict`
        # and refuses anything else by name. Both rules are right about their
        # own boundary, and neither is loosened: the client copies. Without
        # this, every stage wiring a real tree had to carry a private
        # converter, and `read_receipt=context.tree.read_run_receipt` — the
        # wiring this client's own README describes — refused every live start.
        receipt = self._read_receipt(dict(handle.receipt_reference))
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("chair") != self._identity.role
            or receipt.get("revision") != self._identity.receipt_revision
        ):
            observed = (
                f"chair={receipt.get('chair')!r}, revision={receipt.get('revision')!r}"
                if isinstance(receipt, Mapping)
                else f"a non-mapping receipt read: {receipt!r}"
            )
            drift = ReceiptDriftRefusal(
                "CHAIR_RECEIPT_DRIFT",
                "the receipt re-read after start no longer names this chair and revision: "
                f"observed {observed}; expected chair={self._identity.role!r}, "
                f"revision={self._identity.receipt_revision!r}",
            )
            # The drift diagnosis is the thing GOVERNANCE 2 asks not to lose
            # here. Stop the handle so a refused start never leaks the
            # process, but if the shutdown itself is unverifiable, chain it
            # onto the drift refusal rather than letting it replace it.
            try:
                handle.stop()
            except BaseException as stop_error:
                raise drift from stop_error
            raise drift
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
        # Built and checked before the request leaves: a generation value this
        # client could not record as sent must stop the call, not be discovered
        # after a chair has already answered it.
        generation_sent = _recorded_generation(request.generation_sent)
        generation_declared = _recorded_generation(request.generation_declared)
        _refuse_generation_that_cannot_be_recorded_as_sent(
            generation_sent, request.generation_sent, "generation_sent"
        )
        _refuse_generation_that_cannot_be_recorded_as_sent(
            generation_declared, request.generation_declared, "generation_declared"
        )
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
            if (
                parse_problem == "CHAIR_RESPONSE_MODEL_MISMATCH"
                and _peek_model(response.body) is None
            ):
                # `_refuse_bytes_from_the_wrong_source` already let this body
                # through retention because it names no model at all — that is
                # a malformed body, not evidence of a foreign source, and the
                # parser's own comparison (`payload.get("model") !=
                # expected_model_id`) cannot tell the two apart. Recorded
                # verbatim, "model mismatch" would assert a foreign-model
                # observation that was never made (GOVERNANCE 10).
                parse_problem = "CHAIR_RESPONSE_INVALID"
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
            "generation_sent": generation_sent,
            "generation_declared": generation_declared,
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


def _other_tiers_posture(rows: tuple[AnyProfile, ...], tier: str) -> str:
    """Name the posture(s) the *other* tiers hold, for a mixed-posture refusal.

    Never assumes "live": a mix can just as well be captured-and-fixture, so
    the message names whatever kind is actually sitting at the other tiers
    rather than a fixed guess.
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
    """``"fixture"``, ``"captured"`` or ``"live"`` by the sealed row kind alone.

    Three-name lookup, never a ranking: every row for this ``(recipe, chair)``
    is collected first. If every one of them is a fixture row, the chair is
    fixture regardless of a supplied tier; if every one is a captured row
    naming one source chair, the chair is captured regardless of tier, for the
    same reason — neither posture has a serving moment a tier could shape.
    Otherwise a live posture exists somewhere in the catalogue, so a tier is
    required; the row at that exact tier decides, with no fallback to another
    tier or to another posture in either direction (GOVERNANCE 3 / hard rule
    8).
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
    if all(isinstance(row, CapturedProfile) for row in rows):
        sources = sorted({row.captured_from for row in rows})
        if len(sources) != 1:
            raise ServingModeRefusal(
                "SERVING_MODE_UNRESOLVED",
                f"chair={identity.role!r}, recipe={identity.serving_recipe!r} is captured from "
                f"{sources} across its tiers; one chair is captured from exactly one source",
            )
        return "captured"
    if tier is None:
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            "a live serving profile needs the measured placement tier; pass --placement-tier",
        )
    try:
        profile = recipes.for_identity(identity, tier)
    except ServingConfigurationError as error:
        # A chair that is live somewhere but has no row at exactly this tier
        # (a mistyped or unmeasured --placement-tier) is still this
        # function's own refusal vocabulary, not a bare configuration error
        # leaking past it — the docstring and README both promise
        # ServingModeRefusal as the caller-facing contract here.
        raise ServingModeRefusal("SERVING_MODE_UNRESOLVED", str(error)) from error
    if isinstance(profile, ServingProfile):
        return "live"
    if isinstance(profile, FixtureProfile):
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            f"chair={identity.role!r}, recipe={identity.serving_recipe!r} is a fixture row at "
            f"tier={tier!r} while {_other_tiers_posture(rows, tier)} in this catalogue; a "
            "catalogue may not be half fixture for one chair",
        )
    if isinstance(profile, CapturedProfile):
        raise ServingModeRefusal(
            "SERVING_MODE_UNRESOLVED",
            f"chair={identity.role!r}, recipe={identity.serving_recipe!r} is a captured row at "
            f"tier={tier!r} while {_other_tiers_posture(rows, tier)} in this catalogue; a "
            "catalogue may not be half captured for one chair",
        )
    if isinstance(profile, UnsupportedProfile):
        raise ServingModeRefusal("SERVING_MODE_UNSUPPORTED", profile.reason)
    raise AssertionError(  # pragma: no cover - config.py closes the profile union
        f"unreachable serving profile kind {type(profile).__name__}"
    )
