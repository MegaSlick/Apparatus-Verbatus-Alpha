"""Request builders and response derivation for the live Attestatores boundary.

This module owns exactly the seam between an already-issued
:class:`~operations.serving.client.ChairResponse` and the ``Attempt``-shaped
facts `run.py::resolve_attempt` derives from a retained recordable response.
It never wires a pass, never schedules a chair, and never imports ``run.py``.

``act_chair_request``/``page_chair_request`` take a ready-made
``presentation`` (the exact shape ``run.py``'s ``presentation_for_region``/
``presentation_for_page`` build) rather than computing it themselves, to keep
one source of truth for how a presentation is built and avoid a circular
import.

The retained native bytes are ``response.content``, never ``response.raw_response``
(the whole HTTP/JSON envelope): every adapter's native parser expects the
model's own output bytes. The envelope is not lost -- it survives through
``LiveAttempt.call_record_ref`` on a parsed branch, or *is*
``raw_response_ref`` on the malformed branch -- and ``raw_response_kind``
names which of the two a given record holds. A wire body that could not be
parsed at all produces ``native_capture = None``: no adapter ever ran, so
there is no adapter-shaped capture to retain.

**The generation split** (DAI, act-scoped): DAI's carried
``generation_config.json`` includes fields vLLM's endpoint does not accept as
extra decoding parameters. ``generation_declared`` retains the whole carried
view as evidence; ``generation_sent`` allow-lists only the three fields vLLM
does accept, so an unnamed carried key defaults to not being sent rather than
leaking onto the wire (principle 3).

**Every chair's generation bound is decided by one rule, against the sealed
row**: ``common/request_capacity.py::sendable_max_tokens`` -- ``min(the
chair's declared upstream bound, max_model_len - image tokens - prompt
tokens)``. ``generation_bound_sent`` below is this module's name for it. The
row term is expressed by sending no field and the vendor's term by sending
one, because the prompt cost this seam measures is a floor vLLM's own
assembly may disagree with; putting it on the wire could turn a one-token
undercount into an HTTP 400 on a billing card, while sending nothing cannot
fail that way and is what the engine already did.

**Whether a request fits is a different question from what may be sent**, and
is asked of every chair before the request is built
(``request_capacity_or_refuse``), from the sealed row's own pixel/token
ceilings and each chair's measured prompt and answer cost -- never a guess,
and never a silent downscale.

**DAI's closed model view**: unlike Churro or Chandra, DAI's retained view is
closed to its own schema (``feeding.validate_dai_model_view``).
``live_attempt_from_response`` builds it with ``feeding.dai_model_view`` from
the act's presentation, the adapter's published crop, its exact prompt text
and declared generation config. The no-resize case is satisfied by content,
not path identity: DAI's crop on that path is ``crop_png`` of the same sealed
page at the same bounds as the Designator's proposal crop, so the two
references share one digest under two stage-owned paths.

**Confirming a blank response**: ``genuinely-empty`` is confirmed only when
the transport word is a recognized natural completion; an empty response
whose stop word is a cut-off, unreported, or unrecognized is held as
``failed`` instead (principle 8 forbids defaulting any of those to "finished
naturally"). This applies on both the page-scoped and act-scoped paths alike.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

import feeding
import witness_adapters

from common.chair_wire import chandra_wire_fields
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.serving import (
    ENGINE_STOP_COMPLETE,
    ENGINE_STOP_CUT_OFF,
    RAW_RESPONSE_MODEL_OUTPUT,
    RAW_RESPONSE_TRANSPORT_BODY,
    STOP_REASON_UNREPORTED,
)
from common.contracts.stages import ATTESTATORES
from common.imaging import dimensions
from common.native_witness import native_parse_refusal
from common.request_capacity import (
    act_answer_budget,
    dense_page_answer_budget,
    refuse_unless_it_fits,
    sealed_prompt_tokens,
    sendable_max_tokens,
)
from operations.serving.client import ChairRequest, ChairResponse

# The subset of `feeding.dai_generation()` vLLM's endpoint accepts as extra
# decoding parameters. Everything else is retained evidence but never sent
# (principle 3: never silently substitute our own reading of a vendor field).
_DAI_GENERATION_SENT_KEYS = ("repetition_penalty", "top_k", "top_p")


@dataclass(frozen=True, slots=True)
class LiveAttempt:
    """One live chair's resolved outcome for one request -- `Attempt`'s live twin.

    Field-for-field compatible with `run.py::Attempt` so converting to it is a
    rename, not a remap. ``observation_payload`` is populated only on a
    page-scoped adapter's parsed-and-accepted branch, to feed `adapter.observe`
    for page geometry; every other branch leaves it ``None``. The three
    trailing fields are live-only: ``native_capture`` is admitted on every live
    attempted record whose bytes reached an adapter parser; ``call_record_ref``
    and ``receipt_ref`` carry the serving call and receipt this record needs.
    """

    outcome: str
    native_payload: Any
    witness_reported: Any
    format_capabilities: Mapping[str, bool] | None
    health: dict[str, Any]
    reason: str | None
    raw_response_ref: Mapping[str, str] | None
    native_capture: Mapping[str, Any] | None
    call_record_ref: Mapping[str, str] | None
    receipt_ref: Mapping[str, str] | None
    # Which sort of bytes `raw_response_ref` names: `model-output` wherever an
    # adapter parsed, `transport-response-body` on the one branch where none
    # could, `None` only when nothing was retained at all.
    raw_response_kind: str | None = None
    observation_payload: Any = None


@dataclass(frozen=True, slots=True)
class ActChairRequest:
    """One DAI request, plus the exact presented crop and prompt it came from.

    Carried forward so `live_attempt_from_response` can build DAI's closed
    model view once the response comes back without running `adapter.present`
    -- a real crop and resize -- a second time for the same act.
    """

    request: ChairRequest
    presented: Mapping[str, Any]
    prompt: Mapping[str, Any]
    capacity: Mapping[str, Any]
    generation_accounting: Mapping[str, Any] | None


def _data_uri(image_bytes: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _user_content(text: str, image_bytes: bytes) -> list[dict[str, Any]]:
    """One user turn's content parts: **the image first, then the text.**

    Every chat template these three chairs ship emits content parts in list
    order, so this order is the token sequence the model sees, and all three
    were fine-tuned with the vision block before the instruction (DAI's model
    card, Chandra's `model/vllm.py`, Churro's provider all build image-first).

    No token count moves with this: the measured prompt constants are sealed
    against the message *texts*, which an image part carries none of.
    """

    return [
        {"type": "image_url", "image_url": {"url": _data_uri(image_bytes)}},
        {"type": "text", "text": text},
    ]


def _system_content(text: str) -> list[dict[str, str]]:
    """One system turn's content: a single-element list of ``{type: text}`` parts.

    DAI's README and Churro's `HFChatTemplate.build_conversation` both render a
    system message this way, not as a bare string. Shared here so both
    builders below carry it identically. No token count
    moves: a plain string and a one-element list of the same text carry the
    same text; only the JSON shape the chat template sees changes.
    """

    return [{"type": "text", "text": text}]


def _presented_image_bytes(context: Any, presented: Mapping[str, Any]) -> bytes:
    """Read back exactly the bytes an adapter's own presentation names.

    ARCHITECTURE invariant 3: the exact image shown must be reproducible from
    what was recorded. Checked here, by digest, rather than assumed.
    """

    image_bytes = context.tree.read_bytes(presented["image_path"])
    actual = digest_bytes(image_bytes)
    if actual != presented["image_sha256"]:
        raise SchemaRefusal(
            "an adapter's presented image bytes do not match its own declared digest: "
            f"expected {presented['image_sha256']}, read {actual}"
        )
    return image_bytes


# Which numbered chair each adapter name occupies -- the serving row and
# receipt are keyed on chair, but this seam is handed the adapter name.
_ADAPTER_CHAIRS: Mapping[str, str] = {
    "chandra.v1": "attestator_1",
    "dai.v1": "attestator_2",
    "churro.v1": "attestator_3",
}


def _prompt_texts(prompt: Mapping[str, Any]) -> tuple[str, ...]:
    """One adapter's prompt as the ordered text parts its request will carry."""

    if set(prompt) == {"system", "user"}:
        return (prompt["system"], prompt["user"])
    if set(prompt) == {"user"}:
        return (prompt["user"],)  # Chandra: single user turn, no system message.
    if set(prompt) == {"system"}:
        return (prompt["system"],)  # Churro: whole instruction in the system turn.
    raise SchemaRefusal(
        f"a witness adapter returned an unrecognized prompt shape {sorted(prompt)}; this seam "
        "knows the churro.v1/dai.v1 system/user framing, churro.v1's system-only framing, "
        "and chandra.v1's single user turn"
    )


def request_capacity_or_refuse(
    profile: Any,
    adapter_name: str,
    prompt: Mapping[str, Any],
    image_bytes_list: list[bytes],
    *,
    scope: str,
    what: str,
) -> dict[str, Any]:
    """Whether this witness request fits the sealed row, refused by name if not.

    Asked before the request is built, so a request the engine would answer
    with HTTP 400 never reaches a billing card. The image cost is read off the
    bytes this request will actually embed, after the adapter's own crop and
    resize. The prompt cost is the measured constant for this chair, bound to
    a digest of the exact text (no tokenizer is available offline here). The
    answer budget is that chair's own measured response at the scope it was
    asked at: a page chair reserves a dense page's answer, an act chair one
    act's answer, since reserving a page's would refuse ordinary act crops.

    Never a silent downscale: the alternative is showing the model fewer
    pixels than the render config argues are needed to read the ink, which is
    a reading-quality decision this seam does not own.
    """

    chair = _ADAPTER_CHAIRS.get(adapter_name)
    if chair is None:
        raise SchemaRefusal(
            f"witness adapter {adapter_name!r} occupies no chair this seam can name, so its "
            f"request cannot be checked against a serving row; the named adapters are "
            f"{sorted(_ADAPTER_CHAIRS)}"
        )
    budget = dense_page_answer_budget if scope == "page" else act_answer_budget
    return refuse_unless_it_fits(
        profile,
        [dimensions(image) for image in image_bytes_list],
        sealed_prompt_tokens(chair, *_prompt_texts(prompt)),
        budget(chair),
        what=what,
    )


def act_chair_request(
    context: Any, adapter: Any, presentation: Mapping[str, Any], *, profile: Any
) -> ActChairRequest:
    """Build one act-scoped (DAI) reading request from an act's proposal presentation.

    ``presentation`` is exactly what `run.py::presentation_for_region` returns
    for the act's one proposal region; ``adapter.present`` is DAI's own
    crop-and-resize step, publishing and returning the image this request
    embeds. ``profile`` is the sealed serving row this chair runs under. Even
    a page-fallback act's crop (one fallback band, not a whole page) is real
    pixels this seam weighs against the row rather than assuming away.
    """

    presented = adapter.present(context, dict(presentation))
    image_bytes = _presented_image_bytes(context, presented)
    prompt = adapter.prompt()
    capacity = request_capacity_or_refuse(
        profile,
        "dai.v1",
        prompt,
        [image_bytes],
        scope="act",
        what=f"the dai.v1 request for region {presentation.get('region_ref')!r}",
    )
    messages = (
        {"role": "system", "content": _system_content(prompt["system"])},
        {"role": "user", "content": _user_content(prompt["user"], image_bytes)},
    )
    generation_declared = feeding.dai_generation()
    # Sealed fixture/live-test profiles still name the historical `vllm`
    # posture and keep the truthful legacy dai-atr.v1 shape; production
    # profiles are `auto` and get the versioned closed ledger.
    generation_accounting = (
        feeding.dai_generation_accounting("auto") if profile.generation_config == "auto" else None
    )
    generation_sent = {
        key: generation_declared[key]
        for key in _DAI_GENERATION_SENT_KEYS
        if key in generation_declared
    }
    generation_sent.update(generation_bound_sent(_ADAPTER_CHAIRS["dai.v1"], capacity))
    generation_sent.update(feeding.dai_wire_stop_token_ids())
    request = ChairRequest(
        kind="chat-completions",
        messages=messages,
        image_sha256s=(presented["image_sha256"],),
        generation_declared=generation_declared,
        generation_sent=generation_sent,
        capacity=capacity,
    )
    return ActChairRequest(
        request=request,
        presented=presented,
        prompt=prompt,
        capacity=capacity,
        generation_accounting=generation_accounting,
    )


def generation_bound_sent(chair: str, capacity: Mapping[str, Any]) -> dict[str, Any]:
    """The ``max_tokens`` this chair's request carries, from the sealed row.

    Attestatores-local name for `common.request_capacity.sendable_max_tokens`,
    kept so the three builders below and the Designator's own call site agree.
    Returns ``{}`` where the sealed row is what binds.
    """

    return sendable_max_tokens(chair, capacity)


#: The adapters this seam builds a page request for and captures a page
#: response from.
_PAGE_SCOPED_ADAPTERS: Final = frozenset({"churro.v1", "chandra.v1"})


def _framed_prompt(adapter: Any, framing: str | None) -> Mapping[str, Any]:
    """One adapter's prompt, under the framing this run resolved for its chair.

    ``None`` means the adapter has one framing and there is nothing to name;
    it is then asked exactly as it always was.
    """

    return adapter.prompt() if framing is None else adapter.prompt(framing)


def _page_messages(
    adapter_name: str, prompt: Mapping[str, Any], image_bytes: bytes
) -> tuple[Mapping[str, object], ...]:
    """The message tuple for one page-scoped request, dispatched on prompt shape.

    Two shapes, closed and exact -- a third is refused rather than guessed at.

    * ``{"user"}`` -- Chandra's framing: a single user turn carrying the image
      and the vendor's ``OCR_LAYOUT_PROMPT`` bytes, no system message.
    * ``{"system"}`` -- Churro's framing: the whole instruction sits in the
      system turn and the user turn carries the image alone.
    """

    if set(prompt) == {"user"}:
        return ({"role": "user", "content": _user_content(prompt["user"], image_bytes)},)
    if set(prompt) == {"system"}:
        return (
            {"role": "system", "content": _system_content(prompt["system"])},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": _data_uri(image_bytes)}}],
            },
        )
    raise SchemaRefusal(
        f"page-scoped adapter {adapter_name!r} returned an unrecognized prompt shape "
        f"{sorted(prompt)}; this seam knows churro.v1's system-only framing and chandra.v1's "
        "single user turn"
    )


def page_chair_request(
    context: Any,
    adapter: Any,
    adapter_name: str,
    presentation: Mapping[str, Any],
    *,
    profile: Any,
    framing: str | None = None,
) -> ChairRequest:
    """Build one page-scoped (Churro or Chandra) reading request from a whole page.

    ``presentation`` is exactly what `run.py::presentation_for_page` returns,
    but both adapters use the image `adapter.present` returns, not the source
    image unchanged: `chandra.present` runs the vendor's own `scale_to_fit`
    and publishes a resized blob under a different digest, while
    `churro.present` resizes, converts to RGB and publishes its own
    adapter-crop. Everything below reads the bytes, digest and size off
    `adapter.present`'s own return, never off ``presentation``.

    ``profile`` is the sealed serving row this chair runs under. What may be
    sent (`generation_bound_sent`) and whether the request fits
    (`request_capacity_or_refuse`) are two separate questions: at every row in
    the shipped real catalogue, both page chairs' declared bounds fit within
    what the row leaves, so neither sends a ``max_tokens`` today -- a fact
    about the catalogue, not the adapter.
    """

    presented = adapter.present(context, dict(presentation))
    image_bytes = _presented_image_bytes(context, presented)
    prompt = _framed_prompt(adapter, framing)
    image_sha256s = (presented["image_sha256"],)
    messages = _page_messages(adapter_name, prompt, image_bytes)
    # After the prompt shape is recognized, so an unrecognized framing keeps
    # its own clear refusal instead of surfacing as a capacity refusal.
    capacity = request_capacity_or_refuse(
        profile,
        adapter_name,
        prompt,
        [image_bytes],
        scope="page",
        what=f"the {adapter_name} request for page {presentation.get('source_page_ordinal')!r}",
    )
    # Each page chair carries its own declared generation view as evidence,
    # plus the wire fields `generation_config = "vllm"` would otherwise decide
    # for it (Churro's repetition penalty, Chandra's thinking-mode flag).
    # Dispatched by name and total, so a third page adapter is refused rather
    # than quietly handed the other one's vendor fields.
    generation_declared: dict[str, Any]
    if adapter_name == "churro.v1":
        generation_declared = dict(feeding.churro_generation())
        wire_fields: dict[str, Any] = dict(feeding.churro_wire_decoding())
    elif adapter_name == "chandra.v1":
        generation_declared = dict(feeding.chandra_generation())
        wire_fields = chandra_wire_fields()
    else:
        raise SchemaRefusal(
            f"page-scoped adapter {adapter_name!r} has no declared generation view at this "
            f"seam; the page-scoped adapters are {sorted(_PAGE_SCOPED_ADAPTERS)}"
        )
    generation_sent = generation_bound_sent(_ADAPTER_CHAIRS[adapter_name], capacity)
    generation_sent.update(wire_fields)
    return ChairRequest(
        kind="chat-completions",
        messages=messages,
        image_sha256s=image_sha256s,
        generation_declared=generation_declared,
        generation_sent=generation_sent,
        capacity=capacity,
    )


def _finish_reason_facts(response: ChairResponse) -> tuple[str, bool | None, bool | None]:
    """``(transport_stop_reason, completed, cut_off)`` from one retained response.

    The transport word travels verbatim, defaulted only to the literal absence
    marker, never to a meaning. ``completed``/``cut_off`` are ``True``/``False``
    only when the word positively says so; an absent or unrecognized
    ``finish_reason`` leaves both ``None`` rather than guessing (principle 8:
    an unread engine signal is never defaulted to a meaning).
    """

    finish_reason = response.finish_reason
    if finish_reason is None:
        return STOP_REASON_UNREPORTED, None, None
    if finish_reason in ENGINE_STOP_COMPLETE:
        return finish_reason, True, False
    if finish_reason in ENGINE_STOP_CUT_OFF:
        return finish_reason, False, True
    return finish_reason, None, None


def _content_health(text: str, *, completed: bool | None) -> dict[str, Any]:
    """The recordable-text branch of `run.py::content_health`, reproduced.

    Every native payload this module hands here is a decoded ``str``, so only
    that one branch needs reproducing; importing `run.py` here would be
    circular, since `run.py` imports this module.
    """

    return {
        "native_type": "string",
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": text == "",
        "blank": text.strip() == "",
        "truncated": None if completed is None else not completed,
        "characters": len(text),
        "truncation_basis": (
            "trusted-response-boundary" if completed is not None else "not-recorded"
        ),
    }


def _unrecordable_health(reason: str) -> dict[str, Any]:
    """The shape `validate_content_health` requires for `recordable=False`."""

    return {
        "native_type": "unrecordable",
        "encoding": "invalid-or-unrecordable",
        "recordable": False,
        "empty": None,
        "blank": None,
        "truncated": None,
        "characters": None,
        "truncation_basis": reason,
    }


def _unconfirmed_blank_reason(kind: str, transport_stop_reason: str, cut_off: bool | None) -> str:
    """Why an empty response is held rather than confirmed as ``genuinely-empty``.

    ``kind`` names what was read (``"page"`` or ``"act"``); ``cut_off`` is
    ``True`` for a recognized cut-off word and ``None`` for an unreported or
    unrecognized one -- both land here rather than becoming a confirmed blank.
    """

    if cut_off:
        return (
            "the provider response parsed empty after the provider stopped it at its bound "
            f"(transport_stop_reason {transport_stop_reason!r}); a cut-off response is not a "
            f"confirmed blank {kind}"
        )
    return (
        "the provider response parsed empty and the provider's stop boundary was never "
        f"confirmed complete (transport_stop_reason {transport_stop_reason!r}); an unconfirmed "
        f"boundary is not a confirmed blank {kind}"
    )


def _failed_parse_composition(
    parse_reason: str, transport_stop_reason: str, cut_off: bool | None
) -> tuple[str, str]:
    """Compose a parse failure's ``reason`` suffix and content-health basis.

    Shared by the act-scoped and page-scoped parse-failure branches so a
    provider-truncated response cannot go on being folded into one and
    dropped from the other. When ``cut_off`` is ``True`` — the provider's stop
    word was a recognized cut-off — both strings name the truncation ahead of
    the underlying parse reason; otherwise both are the parse reason verbatim.
    """

    if cut_off:
        cut_note = (
            f"the provider stopped the response at its bound "
            f"(transport_stop_reason {transport_stop_reason!r}) and "
        )
        basis = f"response cut off by the provider ({transport_stop_reason!r}); {parse_reason}"
        return f"{cut_note}{parse_reason}", basis
    return parse_reason, parse_reason


def _blob_ref(context: Any, data: bytes) -> dict[str, str]:
    """Retain ``data`` and return the closed ``{relative_path, sha256}`` reference shape."""

    digest, published = context.tree.put_blob(ATTESTATORES, data)
    return {"relative_path": published.relative_path, "sha256": digest}


def _dai_model_view(
    context: Any,
    presentation: Mapping[str, Any],
    presented: Mapping[str, Any],
    prompt: Mapping[str, Any],
    generation_declared: Mapping[str, Any],
    generation_accounting: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build DAI's closed model view (`feeding.dai_model_view`) for this act.

    ``source_image_ref`` is the Designator's own region crop; ``model_image_ref``
    is DAI's further crop-and-resize output. The no-resize case is satisfied by
    content, not path identity: `_dai_present` republishes its crop as a fresh
    blob, but on that path it is `crop_png` of the same sealed page at the same
    bounds as the Designator's own crop, so the two references share one digest
    under two stage-owned paths.

    ``generation_declared`` is serialized with plain JSON rather than the
    canonical writer, because DAI's carried config includes a float
    (``temperature``), which canonical encoding refuses outright.
    """

    bounds = presentation["transform"]["bounds"]
    return feeding.dai_model_view(
        source_image_ref={
            "relative_path": presentation["image_path"],
            "sha256": presentation["image_sha256"],
        },
        model_image_ref={
            "relative_path": presented["image_path"],
            "sha256": presented["image_sha256"],
        },
        width_px=bounds["w"],
        height_px=bounds["h"],
        system_prompt_ref=_blob_ref(context, prompt["system"].encode("utf-8")),
        query_prompt_ref=_blob_ref(context, prompt["user"].encode("utf-8")),
        generation_config_ref=_blob_ref(
            context, json.dumps(dict(generation_declared), sort_keys=True).encode("utf-8")
        ),
        generation_accounting=(
            None if generation_accounting is None else dict(generation_accounting)
        ),
    )


def _live_attempt_from_capture(
    adapter: Any,
    capture: Mapping[str, Any],
    response: ChairResponse,
    *,
    kind: str,
    completed: bool | None,
    cut_off: bool | None,
    transport_stop_reason: str,
    parse_failure_reason: Callable[[Mapping[str, Any]], str],
    observation_payload: Any = None,
) -> LiveAttempt:
    """Turn one adapter's retained capture into a `LiveAttempt`, act or page alike.

    Shared by `live_attempt_from_response` and `captured_page_attempt`: both
    mirror `resolve_attempt`'s three-way split -- ``read``/``genuinely-empty``
    (confirmed only on a recognized natural stop), ``failed`` for an
    unconfirmed empty response, and ``failed`` for a parse failure -- and
    differ only in ``kind`` (used in the unconfirmed-blank reason),
    ``parse_failure_reason`` (dai.v1's own reason vs `native_parse_refusal`
    for a page chair -- shared with `common/native_witness.py`'s page
    validator, which re-derives and compares this same reason, so the two
    must never drift apart), and whether an ``observation_payload`` is
    carried.
    """
    base = {
        "witness_reported": None,
        "format_capabilities": witness_adapters.declared_format_capabilities(adapter),
        "raw_response_ref": dict(capture["raw_response_ref"]),
        "native_capture": capture,
        "call_record_ref": dict(response.call_record_ref),
        "receipt_ref": dict(response.receipt_ref),
        "raw_response_kind": RAW_RESPONSE_MODEL_OUTPUT,
    }
    parsed = capture["parse"]
    if parsed["state"] == "parsed" and (completed is True or parsed["text"] != ""):
        text = parsed["text"]
        return LiveAttempt(
            outcome="genuinely-empty" if text == "" else "read",
            native_payload=text,
            health=_content_health(text, completed=completed),
            reason=None,
            observation_payload=observation_payload,
            **base,
        )
    if parsed["state"] == "parsed":
        # An interrupted or unconfirmed empty response is not evidence of a blank.
        return LiveAttempt(
            outcome="failed",
            native_payload="",
            health=_content_health("", completed=completed),
            reason=_unconfirmed_blank_reason(kind, transport_stop_reason, cut_off),
            **base,
        )
    reason_suffix, basis = _failed_parse_composition(
        parse_failure_reason(parsed), transport_stop_reason, cut_off
    )
    return LiveAttempt(
        outcome="failed",
        native_payload=None,
        health=_unrecordable_health(basis),
        reason=f"the provider response was retained but not usable: {reason_suffix}",
        **base,
    )


def _malformed_response_attempt(response: ChairResponse, *, adapter: Any) -> LiveAttempt:
    """A wire response `ChairClient` could not parse into a reading at all.

    Retained (the raw bytes are already on disk via ``raw_response_ref``),
    never repaired, never re-requested -- the same "malformed" branch
    `resolve_attempt` takes for a fixture-declared malformed response.
    ``format_capabilities`` still names the adapter's own grammar
    (`witness_adapters.declared_format_capabilities`): what a chair's grammar can carry is a fact
    about the chair, not about whether this one body happened to parse.
    """

    reason = f"the provider response was refused without repair: {response.parse_problem}"
    return LiveAttempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=witness_adapters.declared_format_capabilities(adapter),
        health=_unrecordable_health(reason),
        reason=reason,
        raw_response_ref=dict(response.raw_response_ref),
        native_capture=None,
        call_record_ref=dict(response.call_record_ref),
        receipt_ref=dict(response.receipt_ref),
        raw_response_kind=RAW_RESPONSE_TRANSPORT_BODY,
    )


def live_attempt_from_response(
    context: Any,
    adapter: Any,
    adapter_name: str,
    response: ChairResponse,
    *,
    presentation: Mapping[str, Any],
    presented: Mapping[str, Any],
    prompt: Mapping[str, Any],
    generation_declared: Mapping[str, Any],
    parser: str,
    generation_accounting: Mapping[str, Any] | None = None,
) -> LiveAttempt:
    """Derive one act-scoped chair's `LiveAttempt` from its retained response.

    ``dai.v1`` is the only act-scoped adapter today; refuses any other name
    rather than guessing at a view shape it does not know.
    ``presentation``/``presented``/``prompt`` are exactly what
    `act_chair_request` computed for this same act, reused here so DAI's real
    crop and resize runs once per act, not twice.
    """

    if adapter_name != "dai.v1":
        raise SchemaRefusal(
            f"live_attempt_from_response has no capture recipe for adapter {adapter_name!r}; "
            "only dai.v1 is act-scoped today"
        )
    if response.parse_problem is not None:
        return _malformed_response_attempt(response, adapter=adapter)

    transport_stop_reason, completed, cut_off = _finish_reason_facts(response)
    view = _dai_model_view(
        context,
        presentation,
        presented,
        prompt,
        generation_declared,
        generation_accounting,
    )
    # `served=True`: these bytes came off a chair that answered, so Chandra's
    # fixture-placeholder parser may not run (a served chair answering in a
    # shape it was never asked in is a named surprise, not a reading).
    capture = adapter.retain(
        context.tree,
        view=view,
        raw_response=response.content.encode("utf-8"),
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=True,
    )
    return _live_attempt_from_capture(
        adapter,
        capture,
        response,
        kind="act",
        completed=completed,
        cut_off=cut_off,
        transport_stop_reason=transport_stop_reason,
        parse_failure_reason=lambda parsed: parsed["reason"],
    )


def captured_page_attempt(
    context: Any,
    page_ordinal: int,
    chair: str,
    adapter_name: str,
    adapter: Any,
    response: ChairResponse,
    framing: str | None = None,
) -> LiveAttempt:
    """The live twin of `run.py::captured_churro_page_attempt`, generalized.

    Takes an already-retained `ChairResponse` instead of a fixture row, and
    keeps every branch that function has. Runs for both page-scoped adapters:
    Chandra reads the vendor's layout grammar (`common/chandra_layout.py`), a
    body with no top-level block landing on `unrecognized-shape`; Churro reads
    `HistoricalDocument` (`common/churro_document.py`). Both carry their bytes
    forward as ``observation_payload`` for `run.py` to derive page geometry
    from, though Churro's own grammar reports none.

    ``page_ordinal`` and ``chair`` are unread here; accepted only to keep this
    call site self-describing.
    """

    del page_ordinal, chair
    if response.parse_problem is not None:
        return _malformed_response_attempt(response, adapter=adapter)

    transport_stop_reason, completed, cut_off = _finish_reason_facts(response)
    if adapter_name == "churro.v1":
        generation_declared: dict[str, Any] = dict(feeding.churro_generation())
        parser = "xml"  # This chair has one grammar, HistoricalDocument.
    elif adapter_name == "chandra.v1":
        generation_declared = dict(feeding.chandra_generation())
        parser = "html"  # The vendor layout grammar; "json" is fixture-only.
    else:
        raise SchemaRefusal(
            f"captured_page_attempt has no capture recipe for adapter {adapter_name!r}; "
            f"the page-scoped adapters are {sorted(_PAGE_SCOPED_ADAPTERS)}"
        )
    view: dict[str, Any] = {"prompt": _framed_prompt(adapter, framing)}
    if generation_declared:
        view["generation"] = generation_declared
    if framing is not None:
        # By name, on the record, so one run's readings compare with another's
        # without digesting two prompts to discover they differ (principle 6).
        view["framing"] = framing
    # `served=True`: these bytes came off a chair that answered, so Chandra's
    # fixture-placeholder parser may not run (a served chair answering in a
    # shape it was never asked in is a named surprise, not a reading).
    capture = adapter.retain(
        context.tree,
        view=view,
        raw_response=response.content.encode("utf-8"),
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=True,
    )
    return _live_attempt_from_capture(
        adapter,
        capture,
        response,
        kind="page",
        completed=completed,
        cut_off=cut_off,
        transport_stop_reason=transport_stop_reason,
        parse_failure_reason=native_parse_refusal,
        # Unconditional, on purpose: this dispatch has already refused every
        # adapter but the two page-scoped ones, so a membership test here
        # would only be a second, quieter copy of that list -- and a third
        # page chair added above and forgotten here would then silently
        # derive no geometry (principle 2). Both page-scoped adapters derive
        # their block geometry in `run.py` from these same bytes rather than
        # from the parsed text (Churro reports none, but the bytes still
        # travel the same way as Chandra's).
        observation_payload=response.content.encode("utf-8"),
    )
