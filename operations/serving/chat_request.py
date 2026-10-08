"""One chat-completions call to a live chair, and the engine's answer as a reading records it.

Shared by the Perlector's page reading and the Coniector, which send the same
kind of call to the same kind of chair: the caller builds the messages, the
client (`operations.serving.client.ChairClient`) adds the chair's sealed
sampling row and the serving receipt's seed and retains the raw bytes before
anything here reads them. The caller's generation is thinking off and the
admitted output cap; nothing else.

An engine answer that is not a reading -- a body the client could not parse, or
a finish word this build does not recognize -- is refused as
`EngineSignalRefusal`, naming the retained bytes, never guessed at.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from common.chair_wire import chat_template_kwargs_for
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import READER_STOP_REPETITION_LOOP, reading_stop_reason
from operations.serving.client import ChairClient, ChairRequest


class EngineSignalRefusal(ContractError):
    """The engine's response cannot be turned into an honest reading.

    Two causes share it: a `finish_reason` that is neither a completion nor a
    length cut-off (nor absent), or a response the client could not parse at all
    (`parse_problem`). The raw bytes are already retained (`ChairClient.read`
    retains before it parses), named by `raw_response_ref`, so the refused call
    traces back to exactly the evidence that stopped it.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        raw_response_ref: Mapping[str, str],
        call_record_ref: Mapping[str, str],
        request_sha256: str,
        receipt_ref: Mapping[str, str],
        served_model_id: str,
    ) -> None:
        self.code = code
        self.detail = detail
        self.raw_response_ref = dict(raw_response_ref)
        self.call_record_ref = dict(call_record_ref)
        self.request_sha256 = request_sha256
        self.receipt_ref = dict(receipt_ref)
        self.served_model_id = served_model_id
        super().__init__(f"{code}: {detail}")


def _refusal(code: str, detail: str, response: Any) -> EngineSignalRefusal:
    return EngineSignalRefusal(
        code,
        detail,
        raw_response_ref=response.raw_response_ref,
        call_record_ref=response.call_record_ref,
        request_sha256=response.request_sha256,
        receipt_ref=response.receipt_ref,
        served_model_id=response.served_model_id,
    )


def mapped_stop_reason(finish_reason: str | None, *, what: object, response: Any) -> str | None:
    """The engine's finish word as a reading records it (`"stop"`, `"length"`, `None`),
    or `EngineSignalRefusal` for a word this build does not recognize."""
    try:
        return reading_stop_reason(finish_reason)
    except ValueError:
        pass
    raise _refusal(
        "ENGINE_FINISH_REASON_UNRECOGNIZED",
        f"{what} received an engine stop reason {finish_reason!r} this seam does "
        "not recognize (neither a completion nor a length cutoff); the raw response bytes "
        f"are retained at {dict(response.raw_response_ref)!r}",
        response,
    )


def engine_call_of(response: Any) -> dict[str, Any]:
    """The five facts a record keeps of one answered call."""
    return {
        "call_record_ref": dict(response.call_record_ref),
        "raw_response_ref": dict(response.raw_response_ref),
        "response_sha256": response.response_sha256,
        "finish_reason": response.finish_reason,
        "served_model_id": response.served_model_id,
    }


def send_chat_request(
    client: ChairClient,
    *,
    content: str | list[dict[str, Any]],
    image_sha256s: Sequence[str],
    capacity: Mapping[str, Any],
    max_tokens: int,
    what: str,
    loop_guard: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Send one user turn and return what the engine answered.

    `content` is the turn's content as the chair's makers send it: a string for a
    text-only call, or content blocks with every image before the text;
    `image_sha256s` the digests of the images it embeds, in order; `capacity` the
    request-capacity record it was admitted on, copied onto the retained call
    record; `max_tokens` the admitted output cap; `loop_guard` the chair's sealed
    repetition-loop guard, which streams the reply and abandons it at the first
    loop (`ChairRequest.loop_guard`), or `None` for a plain call.

    Returns `{content, stop_reason, finish_reason, request_sha256, engine_call}`,
    and with a `loop_guard` also `loop_stop`: the loop a reply was abandoned on, or
    `None`. A reply abandoned on a loop has `stop_reason` `"repetition-loop"`; its
    content is what arrived before the stop.
    """
    generation_sent: dict[str, Any] = {"max_tokens": max_tokens}
    template_kwargs = chat_template_kwargs_for(client.identity.role)
    if template_kwargs is not None:
        generation_sent["chat_template_kwargs"] = template_kwargs
    request = ChairRequest(
        kind="chat-completions",
        messages=({"role": "user", "content": content},),
        image_sha256s=tuple(image_sha256s),
        generation_declared={},
        generation_sent=generation_sent,
        capacity=capacity,
        loop_guard=loop_guard,
    )
    response = client.read(request)
    if response.parse_problem is not None:
        raise _refusal(
            response.parse_problem,
            f"the response to {what} is not a reading ({response.parse_problem}); the raw "
            f"response bytes are retained at {dict(response.raw_response_ref)!r}",
            response,
        )
    answer = {
        "content": response.content,
        "stop_reason": mapped_stop_reason(response.finish_reason, what=what, response=response),
        "finish_reason": response.finish_reason,
        "request_sha256": response.request_sha256,
        "engine_call": engine_call_of(response),
    }
    if loop_guard is None:
        return answer
    loop_stop = response.loop_stop
    if loop_stop is not None:
        answer["stop_reason"] = READER_STOP_REPETITION_LOOP
    return {**answer, "loop_stop": dict(loop_stop) if loop_stop is not None else None}


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def image_content_blocks(images: Sequence[bytes]) -> list[dict[str, Any]]:
    """One `image_url` block per image, each a PNG data URI, in order."""
    return [{"type": "image_url", "image_url": {"url": _data_uri(image)}} for image in images]


def send_page_request(
    client: ChairClient,
    *,
    images: list[bytes],
    text: str,
    capacity: Mapping[str, Any],
    max_tokens: int,
    what: str,
    loop_guard: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Send one whole-page reading: the page render (and its overlay), then the prompt.

    `images` are the page render and then, when drawn, its overlay, in that
    order; `text` is `page_prompt.build_page_prompt`'s rendered text, sent after
    them; `loop_guard` as `send_chat_request` takes it. Returns what
    `send_chat_request` returns.
    """
    return send_chat_request(
        client,
        content=[*image_content_blocks(images), {"type": "text", "text": text}],
        image_sha256s=[digest_bytes(image) for image in images],
        capacity=capacity,
        max_tokens=max_tokens,
        what=what,
        loop_guard=loop_guard,
    )
