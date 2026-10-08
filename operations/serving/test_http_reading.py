"""Coverage for the reading half of the serving-manager HTTP boundary.

``operations/serving/test_manager.py`` and ``test_http.py`` cover readiness:
the probe parser, the real transport, the manager lifecycle.  Nothing there
exercises the parser a live witness or reader needs — one that accepts an
empty answer as legitimate evidence, carries the engine's stop reason and
token usage verbatim, and never retries, normalizes, or defaults what the
wire actually said.  This file is that coverage, plus
``ServiceHandle.request_reading``, the wire primitive a reading needs beside
the readiness ``request``.
"""

from __future__ import annotations

import base64
import json

import pytest

from .errors import (
    ChairRequestRefusal,
    ChairResponseRefusal,
    ServiceStopError,
    ServingConfigurationError,
)
from .http import (
    HttpResponse,
    assert_wire_part_order,
    chat_image_bytes_all,
    parse_openai_answer,
    parse_openai_reading,
    parse_openai_stream_reading,
    request_body,
)
from .test_manager import TIER, identity, manager_for, profile_row


def _png_data_uri(byte: int) -> str:
    return "data:image/png;base64," + base64.b64encode(bytes([byte])).decode("ascii")


def _response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"))


# --- parse_openai_answer: a probe that also records the engine's word ---


def test_parse_openai_answer_refuses_blank_content() -> None:
    response = _response({"model": "reader-api", "choices": [{"message": {"content": ""}}]})

    with pytest.raises(Exception, match="VLLM_PROBE_RESPONSE_INVALID"):
        parse_openai_answer(response, kind="chat-completions", expected_model_id="reader-api")


def test_parse_openai_answer_records_finish_reasons_and_usage() -> None:
    response = _response(
        {
            "model": "reader-api",
            "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
    )

    result = parse_openai_answer(response, kind="chat-completions", expected_model_id="reader-api")

    assert result.finish_reasons == ("stop",)
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}


# --- parse_openai_reading: every CHAIR_RESPONSE_* refusal fires by its own reason ---


@pytest.mark.parametrize(
    ("response", "kind", "refusal_type", "code"),
    (
        (_response({}), "completions", ChairRequestRefusal, "CHAIR_REQUEST_INVALID"),
        (
            _response({}, status=500),
            "chat-completions",
            ChairResponseRefusal,
            "CHAIR_RESPONSE_HTTP_ERROR",
        ),
        (
            HttpResponse(200, b"not json"),
            "chat-completions",
            ChairResponseRefusal,
            "CHAIR_RESPONSE_INVALID",
        ),
        (_response([1, 2, 3]), "chat-completions", ChairResponseRefusal, "CHAIR_RESPONSE_INVALID"),
        (
            _response({"model": "some-other-model", "choices": [{"message": {"content": "x"}}]}),
            "chat-completions",
            ChairResponseRefusal,
            "CHAIR_RESPONSE_MODEL_MISMATCH",
        ),
        (
            _response({"model": "reader-api", "choices": []}),
            "chat-completions",
            ChairResponseRefusal,
            "CHAIR_RESPONSE_CHOICES_NOT_ONE",
        ),
        (
            _response(
                {
                    "model": "reader-api",
                    "choices": [{"message": {"content": "a"}}, {"message": {"content": "b"}}],
                }
            ),
            "chat-completions",
            ChairResponseRefusal,
            "CHAIR_RESPONSE_CHOICES_NOT_ONE",
        ),
        (
            _response({"model": "reader-api", "choices": ["not-an-object"]}),
            "chat-completions",
            ChairResponseRefusal,
            "CHAIR_RESPONSE_CHOICES_NOT_ONE",
        ),
    ),
)
def test_reading_refusals_name_the_bad_wire_fact(
    response: HttpResponse, kind: str, refusal_type: type[Exception], code: str
) -> None:
    with pytest.raises(refusal_type) as excinfo:
        parse_openai_reading(response, kind=kind, expected_model_id="reader-api")
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"content": None},
        {"content": 4},
        None,
    ],
)
def test_reading_refuses_missing_or_non_string_content_by_name(message: object) -> None:
    response = _response({"model": "reader-api", "choices": [{"message": message}]})

    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert excinfo.value.code == "CHAIR_RESPONSE_CONTENT_MISSING"


def test_reading_accepts_empty_content_as_a_legitimate_reading() -> None:
    response = _response(
        {
            "model": "reader-api",
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        }
    )

    result = parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert result.outputs == ("",)
    assert result.finish_reasons == ("stop",)


@pytest.mark.parametrize(
    "choice",
    [
        {"message": {"content": "x"}},
        {"message": {"content": "x"}, "finish_reason": None},
    ],
)
def test_reading_records_an_absent_finish_reason_as_none_never_a_default(choice: dict) -> None:
    response = _response({"model": "reader-api", "choices": [choice]})

    result = parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert result.finish_reasons == (None,)


def test_reading_carries_an_unrecognized_finish_reason_verbatim() -> None:
    response = _response(
        {
            "model": "reader-api",
            "choices": [{"message": {"content": "x"}, "finish_reason": "abort"}],
        }
    )

    result = parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert result.finish_reasons == ("abort",)


@pytest.mark.parametrize("finish_reason", ["", {"x": 1}])
def test_reading_refuses_a_non_string_or_empty_finish_reason_by_name(finish_reason: object) -> None:
    response = _response(
        {
            "model": "reader-api",
            "choices": [{"message": {"content": "x"}, "finish_reason": finish_reason}],
        }
    )

    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert excinfo.value.code == "CHAIR_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "not-an-object",
        {"prompt_tokens": 1, "completion_tokens": 1},  # total_tokens missing
        {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},  # negative
        {"prompt_tokens": 1.5, "completion_tokens": 1, "total_tokens": 2},  # non-int
        {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},  # bool, not int
    ],
)
def test_reading_treats_malformed_usage_as_none(usage: object) -> None:
    payload: dict[str, object] = {
        "model": "reader-api",
        "choices": [{"message": {"content": "x"}}],
    }
    if usage is not None:
        payload["usage"] = usage
    response = _response(payload)

    result = parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert result.usage is None


def test_reading_carries_valid_usage_verbatim() -> None:
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
        "extra": {"deep": [1]},
    }
    response = _response(
        {"model": "reader-api", "choices": [{"message": {"content": "x"}}], "usage": usage}
    )

    result = parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert result.usage == usage
    with pytest.raises(TypeError):
        result.usage["prompt_tokens"] = 0


# --- chat_image_bytes_all: the multi-image generalization ---


def test_chat_image_bytes_all_returns_every_image_in_order() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                    {"type": "text", "text": "between"},
                    {"type": "image_url", "image_url": {"url": _png_data_uri(2)}},
                ],
            }
        ]
    }

    images = chat_image_bytes_all(payload)

    assert images == [bytes([1]), bytes([2])]


def test_chat_image_bytes_all_rejects_an_image_url_outside_a_user_content_list() -> None:
    payload = {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "image_url", "image_url": {"url": _png_data_uri(1)}}],
            }
        ]
    }

    with pytest.raises(Exception, match="role=user"):
        chat_image_bytes_all(payload)


def test_chat_image_bytes_all_rejects_an_image_url_key_that_is_not_an_active_block() -> None:
    payload = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "extension": {"image_url": {"url": _png_data_uri(1)}},
    }

    with pytest.raises(Exception, match="outside a role=user content list"):
        chat_image_bytes_all(payload)


def test_chat_image_bytes_all_accepts_no_images() -> None:
    payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}

    assert chat_image_bytes_all(payload) == []


def test_chat_image_bytes_all_finds_an_image_in_tuple_shaped_messages_and_content() -> None:
    # A tuple serializes onto the wire exactly like a list, and
    # `_all_image_url_candidates`'s own list-only walk would otherwise miss
    # it too, so `active_candidates` and `all_candidates` would undercount
    # equally and the mismatch this function exists to catch would never fire.
    payload = {
        "messages": (
            {
                "role": "user",
                "content": ({"type": "image_url", "image_url": {"url": _png_data_uri(1)}},),
            },
        )
    }

    assert chat_image_bytes_all(payload) == [bytes([1])]


# --- ServiceHandle.request_reading ---


def test_request_reading_posts_with_the_given_timeout_and_returns_the_raw_response(
    tmp_path,
) -> None:
    chair = identity("reader", "reader-v1")
    manager, _clock, http, _launcher, _registry, _publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
        outputs={"reader-api": "the reading"},
    )
    handle = manager.start(chair, TIER)

    real_request = http.request
    captured: dict[str, float] = {}

    def recording(method, url, *, body, timeout_seconds):
        captured["timeout_seconds"] = timeout_seconds
        captured["url"] = url
        return real_request(method, url, body=body, timeout_seconds=timeout_seconds)

    http.request = recording

    body = json.dumps({"model": "reader-api", "messages": []}).encode("utf-8")
    response = handle.request_reading("chat-completions", body, 42.0)

    assert captured["timeout_seconds"] == 42.0
    assert captured["url"].endswith("/chat/completions")
    assert response.status == 200
    parsed = json.loads(response.body)
    assert parsed["choices"][0]["message"]["content"] == "the reading"


def test_request_reading_refuses_once_the_handle_is_no_longer_active(tmp_path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _clock, _http, _launcher, _registry, _publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    handle = manager.start(chair, TIER)
    handle.stop()

    with pytest.raises(ServiceStopError):
        handle.request_reading("chat-completions", b"{}", 5.0)


def test_request_reading_refuses_once_the_owned_process_has_exited(tmp_path) -> None:
    chair = identity("reader", "reader-v1")
    manager, _clock, _http, _launcher, _registry, _publisher = manager_for(
        tmp_path,
        identities={chair.role: chair},
        profiles=(
            profile_row(
                recipe="reader-v1", chair="reader", served_model_id="reader-api", port=8000
            ),
        ),
        model_ids=("reader-api",),
    )
    handle = manager.start(chair, TIER)
    handle.process.exit_code = 1  # the child died between calls; never claimed live

    with pytest.raises(Exception, match="VLLM_PROCESS_EXITED"):
        handle.request_reading("chat-completions", b"{}", 5.0)


# --- assert_wire_part_order: the request-wide walker request_body wires in  ---


@pytest.mark.parametrize(
    ("payload", "refuses"),
    (
        ({"messages": "not-a-list"}, False),
        ({}, False),
        ({"messages": [{"role": "system", "content": [{"type": "image_url"}]}]}, False),
        ({"messages": [{"role": "user", "content": "READY"}]}, False),
        ({"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}, False),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                            {"type": "text", "text": "hi"},
                        ],
                    }
                ]
            },
            False,
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hi"},
                            {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                        ],
                    }
                ]
            },
            True,
        ),
    ),
)
def test_assert_wire_part_order_handles_each_content_shape(
    payload: dict[str, object], refuses: bool
) -> None:
    if refuses:
        with pytest.raises(ServingConfigurationError, match="request for reader-api"):
            assert_wire_part_order(payload, label="request for reader-api")
    else:
        assert_wire_part_order(payload, label="probe")


def test_assert_wire_part_order_catches_a_text_first_tuple_content_list() -> None:
    # A tuple serializes onto the wire as a JSON array exactly like a list, so
    # a direct caller of this walker (unlike `request_body`, which checks a
    # `json.loads` decode -- always a plain list) must not have that shape
    # silently skip the check.
    payload = {
        "messages": (
            {
                "role": "user",
                "content": (
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                ),
            },
        )
    }

    with pytest.raises(ServingConfigurationError):
        assert_wire_part_order(payload, label="tuple probe")


# --- request_body: proving the walker actually refuses on the seam it is wired into ---


def test_request_body_refuses_a_text_before_image_user_message() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this page"},
                    {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                ],
            }
        ]
    }

    with pytest.raises(ServingConfigurationError, match="request for reader-api"):
        request_body(payload, model_id="reader-api", seed=0, deterministic=True)


def test_request_body_accepts_an_image_before_text_user_message() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                    {"type": "text", "text": "describe this page"},
                ],
            }
        ]
    }

    body = request_body(payload, model_id="reader-api", seed=0, deterministic=True)

    assert json.loads(body)["messages"][0]["content"][0]["type"] == "image_url"


def test_request_body_accepts_the_readiness_probes_bare_string_content() -> None:
    payload = {"messages": [{"role": "user", "content": "READY"}]}

    body = request_body(payload, model_id="reader-api", seed=0, deterministic=True)

    assert json.loads(body)["messages"][0]["content"] == "READY"


def test_request_body_accepts_a_system_preamble_with_an_image_first_user_turn() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "you read pages"}]},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                    {"type": "text", "text": "read this"},
                ],
            },
        ]
    }

    body = request_body(payload, model_id="reader-api", seed=0, deterministic=True)

    assert json.loads(body)["messages"][1]["content"][0]["type"] == "image_url"


def test_request_body_refuses_a_content_part_whose_get_disagrees_with_its_own_wire_value() -> None:
    # A `dict` subclass can make `.get("type")` answer one thing while
    # `json.dumps` -- which reads `__class__`/`items()`, never `.get` --
    # serializes a different stored value, so the check must read the
    # rendered wire body rather than trust the mutable Python object.
    class LyingPart(dict):
        def get(self, key, default=None):
            if key == "type":
                return "image_url"
            return super().get(key, default)

    part = LyingPart(type="text", text="describe this page")
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    part,
                    {"type": "image_url", "image_url": {"url": _png_data_uri(1)}},
                ],
            }
        ]
    }

    with pytest.raises(ServingConfigurationError, match="request for reader-api"):
        request_body(payload, model_id="reader-api", seed=0, deterministic=True)


# --- a streamed reply (`request_body(stream=True)`), read as vLLM sends it ---


def _sse(*events: object) -> bytes:
    return b"".join(
        b"data: " + (event if isinstance(event, bytes) else json.dumps(event).encode()) + b"\n\n"
        for event in events
    )


def _chunk(content: str | None, finish: str | None = None, model: str = "served") -> dict:
    delta = {} if content is None else {"content": content}
    return {
        "model": model,
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        "usage": None,
    }


USAGE = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
WHOLE = _sse(
    {
        "model": "served",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}],
    },
    _chunk("Tremblay, Jean\n"),
    _chunk("Tremblay, Marie\n"),
    _chunk(None, "stop"),
    {"model": "served", "choices": [], "usage": USAGE},
    b"[DONE]",
)


def test_a_streamed_reply_is_its_deltas_in_order_with_the_engine_s_own_finish_and_usage():
    result = parse_openai_stream_reading(
        HttpResponse(200, WHOLE), kind="chat-completions", expected_model_id="served", stopped=False
    )
    assert result.outputs == ("Tremblay, Jean\nTremblay, Marie\n",)
    assert result.finish_reasons == ("stop",)
    assert dict(result.usage) == USAGE
    # The same bytes with CRLF line ends read the same.
    crlf = parse_openai_stream_reading(
        HttpResponse(200, WHOLE.replace(b"\n", b"\r\n")),
        kind="chat-completions",
        expected_model_id="served",
        stopped=False,
    )
    assert crlf.outputs == result.outputs


def test_a_stream_the_client_stopped_reads_up_to_its_last_complete_event():
    cut = _sse(_chunk("a\n"), _chunk("a\n")) + b'data: {"model": "ser'
    result = parse_openai_stream_reading(
        HttpResponse(200, cut), kind="chat-completions", expected_model_id="served", stopped=True
    )
    assert (result.outputs, result.finish_reasons, result.usage) == (("a\na\n",), (None,), None)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        # Ended without [DONE], and the client did not stop it: not a reading.
        (_sse(_chunk("a\n"), _chunk(None, "stop")), "CHAIR_RESPONSE_INVALID"),
        (WHOLE + _sse(_chunk("late")), "CHAIR_RESPONSE_INVALID"),
        (_sse({"error": {"message": "engine died"}}, b"[DONE]"), "CHAIR_RESPONSE_INVALID"),
        (_sse(_chunk("a", model="other"), b"[DONE]"), "CHAIR_RESPONSE_MODEL_MISMATCH"),
        (_sse(_chunk("a", "stop"), _chunk(None, "length"), b"[DONE]"), "CHAIR_RESPONSE_INVALID"),
        (_sse(b"[DONE]"), "CHAIR_RESPONSE_CONTENT_MISSING"),
    ],
    ids=["no-done", "after-done", "error-event", "other-model", "two-finishes", "no-chunk"],
)
def test_a_streamed_reply_that_is_not_a_reading_is_refused_by_name(body, code):
    with pytest.raises(ChairResponseRefusal) as refusal:
        parse_openai_stream_reading(
            HttpResponse(200, body),
            kind="chat-completions",
            expected_model_id="served",
            stopped=False,
        )
    assert refusal.value.code == code


def test_a_streamed_request_asks_for_events_and_usage_and_a_whole_one_is_unchanged():
    payload = {"messages": [{"role": "user", "content": "read"}], "max_tokens": 9}
    whole = request_body(payload, model_id="served", seed=7, deterministic=False)
    streamed = request_body(payload, model_id="served", seed=7, deterministic=False, stream=True)
    assert json.loads(whole)["stream"] is False and "stream_options" not in json.loads(whole)
    assert json.loads(streamed)["stream"] is True
    assert json.loads(streamed)["stream_options"] == {"include_usage": True}
    with pytest.raises(ServingConfigurationError, match="manager-owned"):
        request_body(
            {**payload, "stream_options": {}}, model_id="served", seed=7, deterministic=False
        )
