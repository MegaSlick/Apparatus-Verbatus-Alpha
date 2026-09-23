from __future__ import annotations

import json

import pytest

from operations.serving.errors import ServingConfigurationError
from operations.serving.http import chandra_native_request_body


def test_chandra_native_body_sends_schedule_and_omits_per_request_seed():
    body = chandra_native_request_body(
        {"messages": [{"role": "user", "content": "page"}], "max_tokens": 12384},
        model_id="attestator-1-chandra",
        temperature=0.4,
        top_p=0.95,
    )
    decoded = json.loads(body)
    assert decoded["temperature"] == 0.4
    assert decoded["top_p"] == 0.95
    assert decoded["max_tokens"] == 12384
    assert decoded["stream"] is False
    assert "seed" not in decoded


@pytest.mark.parametrize("field", ["temperature", "top_p", "seed", "stream"])
def test_chandra_native_body_refuses_ambient_overrides(field):
    with pytest.raises(ServingConfigurationError, match="may not predeclare"):
        chandra_native_request_body(
            {"messages": [], field: 1},
            model_id="attestator-1-chandra",
            temperature=0,
            top_p=0.1,
        )
