from __future__ import annotations

import pytest

from common.chandra_native_retry import (
    CHANDRA_MAX_ATTEMPTS,
    attempt_parameters,
    detect_repeat_token,
    error_backoff_seconds,
    exhausted_condition,
    recipe_record,
    refuse_orphan_intent,
    retry_trigger,
    validate_trace,
)
from common.contracts.errors import SchemaRefusal


def test_pinned_chandra_recipe_declares_seven_exact_physical_requests():
    assert recipe_record() == {
        "schema": "chandra-native-inference.v1",
        "source_repository": "https://github.com/datalab-to/chandra",
        "source_revision": "d4f7467435aa4137d9539f000ddf0b7ced3eb43f",
        "recipe_path": "chandra/model/vllm.py",
        "detector_path": "chandra/model/util.py::detect_repeat_token",
        "settings_path": "chandra/settings.py",
        "max_output_tokens": 12384,
        "max_retries": 6,
        "temperature_schedule": [
            "0.0",
            "0.2",
            "0.4",
            "0.6000000000000001",
            "0.8",
            "0.8",
            "0.8",
        ],
        "initial_top_p": "0.1",
        "retry_top_p": "0.95",
        "error_backoff_seconds": [2, 4, 6, 8, 10, 12],
    }
    assert [attempt_parameters(i) for i in range(1, CHANDRA_MAX_ATTEMPTS + 1)] == [
        {"temperature": "0.0", "top_p": "0.1"},
        {"temperature": "0.2", "top_p": "0.95"},
        {"temperature": "0.4", "top_p": "0.95"},
        {"temperature": "0.6000000000000001", "top_p": "0.95"},
        {"temperature": "0.8", "top_p": "0.95"},
        {"temperature": "0.8", "top_p": "0.95"},
        {"temperature": "0.8", "top_p": "0.95"},
    ]
    assert [error_backoff_seconds(i) for i in range(1, 8)] == [2, 4, 6, 8, 10, 12, None]


def test_repeat_detector_ports_threshold_and_cut_last_fifty_probe_exactly():
    assert detect_repeat_token("a" * 16) is False
    assert detect_repeat_token("a" * 17) is True
    suffix = "".join(chr(0x400 + index) for index in range(50))
    raw = "a" * 17 + suffix
    assert detect_repeat_token(raw) is False
    assert detect_repeat_token(raw, cut_from_end=50) is True
    assert retry_trigger(raw, inference_error=False, attempt_ordinal=1) == "repeat-token"


def test_repeat_precedes_error_and_seventh_result_is_returned_without_an_eighth_call():
    repeated = "x" * 17
    assert retry_trigger(repeated, inference_error=True, attempt_ordinal=1) == "repeat-token"
    assert retry_trigger("", inference_error=True, attempt_ordinal=1) == "inference-error"
    assert retry_trigger(repeated, inference_error=True, attempt_ordinal=7) is None
    assert exhausted_condition(repeated, inference_error=True) == "repeat-token"
    assert exhausted_condition("", inference_error=True) == "inference-error"


def test_trace_counts_physical_requests_but_returns_only_the_last_attempt():
    def reference(name: str, digest: str):
        return {"relative_path": f"3_attestatores/artifacts/x/{name}.json", "sha256": digest * 64}

    trace = {
        "schema": "chandra-native-retry-trace.v1",
        "recipe": recipe_record(),
        "physical_request_count": 2,
        "returned_attempt_ordinal": 2,
        "exhausted_condition": None,
        "attempts": [
            {
                "attempt_ordinal": 1,
                "parameters": attempt_parameters(1),
                "intent_ref": reference("intent-1", "a"),
                "attempt_ref": reference("attempt-1", "b"),
                "trigger": "inference-error",
                "error": True,
            },
            {
                "attempt_ordinal": 2,
                "parameters": attempt_parameters(2),
                "intent_ref": reference("intent-2", "c"),
                "attempt_ref": reference("attempt-2", "d"),
                "trigger": None,
                "error": False,
            },
        ],
    }
    assert validate_trace(trace) is trace
    trace["returned_attempt_ordinal"] = 1
    with pytest.raises(SchemaRefusal, match="vendor-returned final attempt"):
        validate_trace(trace)


def test_trace_refuses_trigger_error_drift_early_exhaustion_and_reused_evidence():
    reference = lambda name, digest: {  # noqa: E731 - compact closed fixture
        "relative_path": f"3_attestatores/artifacts/x/{name}.json",
        "sha256": digest * 64,
    }
    base = {
        "schema": "chandra-native-retry-trace.v1",
        "recipe": recipe_record(),
        "physical_request_count": 2,
        "returned_attempt_ordinal": 2,
        "exhausted_condition": None,
        "attempts": [
            {
                "attempt_ordinal": 1,
                "parameters": attempt_parameters(1),
                "intent_ref": reference("intent-1", "a"),
                "attempt_ref": reference("attempt-1", "b"),
                "trigger": "inference-error",
                "error": True,
            },
            {
                "attempt_ordinal": 2,
                "parameters": attempt_parameters(2),
                "intent_ref": reference("intent-2", "c"),
                "attempt_ref": reference("attempt-2", "d"),
                "trigger": None,
                "error": False,
            },
        ],
    }
    validate_trace(base)

    no_error = {**base, "attempts": [dict(base["attempts"][0], error=False), base["attempts"][1]]}
    with pytest.raises(SchemaRefusal, match="inference-error trigger without an error"):
        validate_trace(no_error)

    early_error = {**base, "attempts": [base["attempts"][0], dict(base["attempts"][1], error=True)]}
    with pytest.raises(SchemaRefusal, match="returned an error before retries exhausted"):
        validate_trace(early_error)

    early_exhaustion = {**base, "exhausted_condition": "repeat-token"}
    with pytest.raises(SchemaRefusal, match="exhaustion before seven attempts"):
        validate_trace(early_exhaustion)

    duplicate = {
        **base,
        "attempts": [
            base["attempts"][0],
            dict(base["attempts"][1], intent_ref=base["attempts"][0]["intent_ref"]),
        ],
    }
    with pytest.raises(SchemaRefusal, match="reuses one retained artifact"):
        validate_trace(duplicate)

    exhausted_attempts = [
        {
            "attempt_ordinal": ordinal,
            "parameters": attempt_parameters(ordinal),
            "intent_ref": reference(f"intent-{ordinal}", "a"),
            "attempt_ref": reference(f"attempt-{ordinal}", "b"),
            "trigger": "inference-error" if ordinal < 7 else None,
            "error": True,
        }
        for ordinal in range(1, 8)
    ]
    hidden_error = {
        **base,
        "physical_request_count": 7,
        "returned_attempt_ordinal": 7,
        "attempts": exhausted_attempts,
    }
    with pytest.raises(SchemaRefusal, match="hides its final inference error"):
        validate_trace(hidden_error)
    false_error = {
        **hidden_error,
        "exhausted_condition": "inference-error",
        "attempts": [*exhausted_attempts[:-1], dict(exhausted_attempts[-1], error=False)],
    }
    with pytest.raises(SchemaRefusal, match="inference-error exhaustion without an error"):
        validate_trace(false_error)


def test_a_persisted_intent_without_terminal_evidence_is_never_replayed():
    with pytest.raises(SchemaRefusal, match="delivery is unknown.*will not be replayed"):
        refuse_orphan_intent()
