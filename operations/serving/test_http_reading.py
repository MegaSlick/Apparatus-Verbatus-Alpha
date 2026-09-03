"""Coverage for the reading half of the serving-manager HTTP boundary.

``operations/serving/test_manager.py`` and ``test_http.py`` cover readiness:
the probe parser, the real transport, the manager lifecycle.  Nothing there
exercises the parser a live witness or reader needs — one that accepts an
empty answer as legitimate evidence, carries the engine's stop reason and
token usage verbatim, and never retries, normalizes, or defaults what the
wire actually said.  This file is that coverage, plus
``ServiceHandle.request_reading``, the one new wire primitive a reading needs
beyond the existing readiness ``request``.
"""

from __future__ import annotations

import base64
import json

import pytest

from .errors import ChairRequestRefusal, ChairResponseRefusal, ServiceStopError
from .http import (
    HttpResponse,
    chat_image_bytes_all,
    parse_openai_answer,
    parse_openai_reading,
)
from .test_manager import TIER, identity, manager_for, profile_row


def _png_data_uri(byte: int) -> str:
    return "data:image/png;base64," + base64.b64encode(bytes([byte])).decode("ascii")


def _response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"))


# --- parse_openai_answer: still a probe, now also records the engine's word ---


def test_parse_openai_answer_still_refuses_blank_content_for_the_probe() -> None:
    response = _response({"model": "reader-api", "choices": [{"message": {"content": ""}}]})

    with pytest.raises(Exception, match="VLLM_PROBE_RESPONSE_INVALID"):
        parse_openai_answer(response, kind="chat-completions", expected_model_id="reader-api")


def test_parse_openai_answer_now_records_finish_reasons_and_usage() -> None:
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


def test_parse_openai_answer_defaults_are_backward_compatible() -> None:
    from .http import OpenAIResult

    result = OpenAIResult(model_id="reader-api", outputs=("hi",), response_sha256="a" * 64)

    assert result.finish_reasons == ()
    assert result.usage is None


# --- parse_openai_reading: every CHAIR_RESPONSE_* refusal fires by its own reason ---


def test_reading_refuses_a_non_chat_kind_by_name() -> None:
    with pytest.raises(ChairRequestRefusal) as excinfo:
        parse_openai_reading(_response({}), kind="completions", expected_model_id="reader-api")

    assert excinfo.value.code == "CHAIR_REQUEST_INVALID"


def test_reading_refuses_a_non_200_status_by_name() -> None:
    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(
            _response({}, status=500), kind="chat-completions", expected_model_id="reader-api"
        )

    assert excinfo.value.code == "CHAIR_RESPONSE_HTTP_ERROR"


def test_reading_refuses_a_body_that_is_not_json_by_name() -> None:
    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(
            HttpResponse(200, b"not json"), kind="chat-completions", expected_model_id="reader-api"
        )

    assert excinfo.value.code == "CHAIR_RESPONSE_INVALID"


def test_reading_refuses_a_body_that_is_not_a_json_object_by_name() -> None:
    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(
            _response([1, 2, 3]), kind="chat-completions", expected_model_id="reader-api"
        )

    assert excinfo.value.code == "CHAIR_RESPONSE_INVALID"


def test_reading_refuses_a_model_mismatch_by_name() -> None:
    response = _response({"model": "some-other-model", "choices": [{"message": {"content": "x"}}]})

    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert excinfo.value.code == "CHAIR_RESPONSE_MODEL_MISMATCH"


@pytest.mark.parametrize(
    "choices", [[], [{"message": {"content": "a"}}, {"message": {"content": "b"}}]]
)
def test_reading_refuses_anything_but_exactly_one_choice_by_name(choices: list[object]) -> None:
    response = _response({"model": "reader-api", "choices": choices})

    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert excinfo.value.code == "CHAIR_RESPONSE_CHOICES_NOT_ONE"


def test_reading_refuses_a_non_object_choice_by_name() -> None:
    response = _response({"model": "reader-api", "choices": ["not-an-object"]})

    with pytest.raises(ChairResponseRefusal) as excinfo:
        parse_openai_reading(response, kind="chat-completions", expected_model_id="reader-api")

    assert excinfo.value.code == "CHAIR_RESPONSE_CHOICES_NOT_ONE"


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
