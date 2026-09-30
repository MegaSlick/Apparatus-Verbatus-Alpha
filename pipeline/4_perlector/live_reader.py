"""The Perlector's live page request, behind one ``ChairClient``
(``operations/serving/client.py``) already entered for this chair's pass.

``send_page_request`` sends one whole-page reading and returns what the engine
answered, with the call's retained evidence named.

**The stop-reason mapping is where an unrecognized engine answer becomes a
loud stop, not a silent guess.** ``common/truncation.py::classify``'s docstring
documents the rule this implements: an engine's own word is authoritative for
``length``, but a string this seam does not recognize is not folded into either
bucket -- it is refused by name, with the raw response bytes already retained
(``ChairClient.read`` retains them before this module is asked to interpret
them), so nothing is lost even though the page publishes no answer.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import reading_stop_reason
from operations.serving.client import ChairClient, ChairRequest


class EngineSignalRefusal(ContractError):
    """The engine's response cannot be turned into an honest ``LectioResult``.

    Two distinct causes share this refusal, because both leave a Perlector
    reading with no honest text to publish: a ``finish_reason`` this seam does
    not recognize (neither in ``ENGINE_STOP_COMPLETE`` nor
    ``ENGINE_STOP_CUT_OFF``, nor absent), or a response
    :class:`~operations.serving.client.ChairClient` could not parse at all
    (``parse_problem``). The stage publishes such an act as a failed Perlectio
    (``run.py``) rather than a reading, so a body that is not a reading never
    becomes text. Nothing is
    lost: the raw bytes are already retained (``ChairClient.read`` retains
    before it parses), named here by ``raw_response_ref`` so the stopped act
    can be traced back to exactly the evidence that stopped it.
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


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _image_content_blocks(images: list[bytes]) -> list[dict[str, Any]]:
    return [{"type": "image_url", "image_url": {"url": _data_uri(image)}} for image in images]


def _mapped_stop_reason(finish_reason: str | None, *, act_key: object, response: Any) -> str | None:
    """The engine's own word, translated into the reader-protocol's closed
    vocabulary (``common/truncation.py``'s own ``"stop"``/``"length"``/``None``), or
    a named refusal for anything else."""
    try:
        return reading_stop_reason(finish_reason)
    except ValueError:
        pass
    raise EngineSignalRefusal(
        "ENGINE_FINISH_REASON_UNRECOGNIZED",
        f"act {act_key!r} received an engine stop reason {finish_reason!r} this seam does "
        "not recognize (neither a completion nor a length cutoff); the raw response bytes "
        f"are retained at {dict(response.raw_response_ref)!r}",
        raw_response_ref=response.raw_response_ref,
        call_record_ref=response.call_record_ref,
        request_sha256=response.request_sha256,
        receipt_ref=response.receipt_ref,
        served_model_id=response.served_model_id,
    )


def send_page_request(
    client: ChairClient,
    *,
    images: list[bytes],
    text: str,
    capacity: Mapping[str, Any],
    max_tokens: int,
    what: str,
) -> dict[str, Any]:
    """Send one whole-page reading request and return what the engine answered.

    `images` are the page render and then, when drawn, its overlay, in that
    order; `text` is `page_prompt.build_page_prompt`'s rendered text, sent after
    them; `capacity` is the request-capacity record the request was admitted on
    (`common.request_capacity.page_request_capacity`), copied onto the retained
    call record; `max_tokens` is the admitted output cap. The caller's
    generation is the one a reading sends, thinking off and the cap; the client
    adds the Perlector's sealed sampling row and the serving receipt's seed, as
    for an act reading (attempt 1, no variance arm).

    Returns `{content, stop_reason, finish_reason, request_sha256, engine_call}`:
    `stop_reason` is the engine's word mapped as a reading's (`"stop"`,
    `"length"` or `None`), and an unrecognized word or an unparsed body is
    refused as `EngineSignalRefusal` with the retained bytes named, as a
    reading's is.
    """
    content: list[dict[str, Any]] = _image_content_blocks(images)
    content.append({"type": "text", "text": text})
    request = ChairRequest(
        kind="chat-completions",
        messages=({"role": "user", "content": content},),
        image_sha256s=tuple(digest_bytes(image) for image in images),
        generation_declared={},
        generation_sent={
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": max_tokens,
        },
        capacity=capacity,
    )
    response = client.read(request)
    if response.parse_problem is not None:
        raise EngineSignalRefusal(
            response.parse_problem,
            f"the response to {what} is not a reading ({response.parse_problem}); the raw "
            f"response bytes are retained at {dict(response.raw_response_ref)!r}",
            raw_response_ref=response.raw_response_ref,
            call_record_ref=response.call_record_ref,
            request_sha256=response.request_sha256,
            receipt_ref=response.receipt_ref,
            served_model_id=response.served_model_id,
        )
    return {
        "content": response.content,
        "stop_reason": _mapped_stop_reason(response.finish_reason, act_key=what, response=response),
        "finish_reason": response.finish_reason,
        "request_sha256": response.request_sha256,
        "engine_call": {
            "call_record_ref": dict(response.call_record_ref),
            "raw_response_ref": dict(response.raw_response_ref),
            "response_sha256": response.response_sha256,
            "finish_reason": response.finish_reason,
            "served_model_id": response.served_model_id,
        },
    }
