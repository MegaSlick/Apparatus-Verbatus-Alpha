"""The shared chat sender: the Perlector's page request byte for byte, a text-only
turn, and the engine answers it refuses."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from common.chair_wire import chat_template_kwargs_for
from common.contracts.canonical import canonical_bytes, digest_bytes
from operations.serving.chat_request import (
    EngineSignalRefusal,
    send_chat_request,
    send_page_request,
)

# The digest of the Perlector's page request for these inputs; a change to it
# changes what the Perlector sends.
PAGE_REQUEST_SHA256 = "ac2803cbbe83e11df09671c2e0bc455ac813fe3ee4ca4ab7376f6a7f25546d6a"
PAGE_REQUEST_INPUTS = {
    "images": [b"\x89PNG page", b"\x89PNG overlay"],
    "text": "Read this page.\nÉté",
    "capacity": {"schema": "x", "images": [{"w": 1}]},
    "max_tokens": 1234,
    "what": "page p1",
}


class _Client:
    def __init__(
        self, *, parse_problem=None, finish_reason="stop", chair="perlector", loop_stop=None
    ) -> None:
        self.identity = SimpleNamespace(role=chair)
        self.requests = []
        self.parse_problem = parse_problem
        self.finish_reason = finish_reason
        self.loop_stop = loop_stop

    def read(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            parse_problem=self.parse_problem,
            content='{"acts": [], "joins": []}',
            finish_reason=self.finish_reason,
            raw_response_ref={"relative_path": "raw", "sha256": "a" * 64},
            call_record_ref={"relative_path": "call", "sha256": "b" * 64},
            request_sha256="c" * 64,
            receipt_ref={"relative_path": "receipt", "sha256": "d" * 64},
            served_model_id="served",
            response_sha256="a" * 64,
            **({"loop_stop": self.loop_stop} if request.loop_guard is not None else {}),
        )


def _request_bytes(request) -> bytes:
    return canonical_bytes(
        {
            "kind": request.kind,
            "messages": [dict(message) for message in request.messages],
            "image_sha256s": list(request.image_sha256s),
            "generation_declared": dict(request.generation_declared),
            "generation_sent": {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in request.generation_sent.items()
            },
            "capacity": json.loads(json.dumps(request.capacity, default=dict)),
        }
    )


def test_the_perlector_page_request_is_pinned_byte_for_byte():
    client = _Client()
    send_page_request(client, **PAGE_REQUEST_INPUTS)
    (request,) = client.requests
    assert digest_bytes(_request_bytes(request)) == PAGE_REQUEST_SHA256


@pytest.mark.parametrize("chair", ["perlector", "reconstructor", "attestator_3"])
def test_a_turn_carries_the_template_switch_its_chair_s_smoke_sends(chair):
    client = _Client(chair=chair)
    send_chat_request(
        client, content="read", image_sha256s=[], capacity={}, max_tokens=9, what="page 1"
    )
    (request,) = client.requests
    assert request.generation_sent.get("chat_template_kwargs") == chat_template_kwargs_for(chair)


def test_a_guarded_page_request_carries_its_guard_and_a_stopped_reply_says_why():
    guard = {"loop_line_repeats": 30, "loop_block_repeats": 10, "loop_block_max_lines": 8}
    loop = {"kind": "line", "block_lines": 1, "repeats": 30, "line": 70}
    client = _Client(finish_reason=None, loop_stop=loop)
    answer = send_page_request(client, **PAGE_REQUEST_INPUTS, loop_guard=guard)
    (request,) = client.requests
    assert dict(request.loop_guard) == guard
    # The guard rides beside the request, never inside what the caller sends.
    assert digest_bytes(_request_bytes(request)) == PAGE_REQUEST_SHA256
    assert answer["stop_reason"] == "repetition-loop"
    assert answer["finish_reason"] is None
    assert answer["loop_stop"] == loop
    guarded = send_page_request(_Client(), **PAGE_REQUEST_INPUTS, loop_guard=guard)
    assert (guarded["stop_reason"], guarded["loop_stop"]) == ("stop", None)
    # An unguarded call answers exactly what it always did.
    plain = send_page_request(_Client(), **PAGE_REQUEST_INPUTS)
    assert plain["stop_reason"] == "stop" and "loop_stop" not in plain


def test_a_text_only_turn_sends_its_string_content_thinking_off_and_no_image():
    client = _Client()
    answer = send_chat_request(
        client, content="reconstruct", image_sha256s=[], capacity={}, max_tokens=99, what="page 1"
    )
    (request,) = client.requests
    assert [dict(message) for message in request.messages] == [
        {"role": "user", "content": "reconstruct"}
    ]
    assert request.image_sha256s == ()
    assert dict(request.generation_sent) == {
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 99,
    }
    assert answer["stop_reason"] == "stop"
    assert answer["engine_call"]["served_model_id"] == "served"


def test_an_unparsed_body_and_an_unknown_finish_word_are_refused_with_the_bytes_named():
    with pytest.raises(EngineSignalRefusal) as unparsed:
        send_chat_request(
            _Client(parse_problem="NO_CHOICES"),
            content="x",
            image_sha256s=[],
            capacity={},
            max_tokens=1,
            what="page 1",
        )
    assert unparsed.value.code == "NO_CHOICES"
    assert unparsed.value.raw_response_ref == {"relative_path": "raw", "sha256": "a" * 64}
    with pytest.raises(EngineSignalRefusal) as unknown:
        send_chat_request(
            _Client(finish_reason="abort"),
            content="x",
            image_sha256s=[],
            capacity={},
            max_tokens=1,
            what="page 1",
        )
    assert unknown.value.code == "ENGINE_FINISH_REASON_UNRECOGNIZED"
    assert unknown.value.detail.startswith("page 1 received an engine stop reason 'abort'")
