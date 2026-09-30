from __future__ import annotations

import json

import pytest

from common.decoding import chair_attempt_decoding, load_decoding_policy
from operations.serving.errors import ServingConfigurationError
from operations.serving.http import chandra_native_request_body


def _sampling(attempt_ordinal: int) -> dict[str, int | float]:
    policy, _digest = load_decoding_policy()
    return chair_attempt_decoding(policy, "attestator_1", attempt_ordinal)


def test_chandra_native_body_sends_the_attempts_sealed_row_and_omits_per_request_seed():
    body = chandra_native_request_body(
        {"messages": [{"role": "user", "content": "page"}], "max_tokens": 12384},
        model_id="attestator-1-chandra",
        sampling=_sampling(3),
    )
    decoded = json.loads(body)
    assert {field: decoded[field] for field in _sampling(3)} == {
        "temperature": 0.4,
        "top_p": 0.95,
        "top_k": 0,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
    }
    assert decoded["max_tokens"] == 12384
    assert decoded["stream"] is False
    assert "seed" not in decoded


@pytest.mark.parametrize("field", ["temperature", "top_p", "top_k", "seed", "stream"])
def test_chandra_native_body_refuses_ambient_overrides(field):
    with pytest.raises(ServingConfigurationError, match="may not predeclare"):
        chandra_native_request_body(
            {"messages": [], field: 1},
            model_id="attestator-1-chandra",
            sampling=_sampling(1),
        )
