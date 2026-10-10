"""Cross-package proofs for the serving configuration input contract, and for
the closed shapes and stop-reason vocabularies the live reading seam adds.
"""

import pytest

from common.contracts.serving import (
    CALL_RECORD_SCHEMAS,
    CALLER_GENERATION_FIELDS,
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_PROBLEM_FIELDS,
    CHAIR_TRANSPORT_PROBLEM_SCHEMA,
    CHANDRA_NATIVE_CALL_RECORD_FIELDS,
    CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
    ENGINE_STOP_COMPLETE,
    ENGINE_STOP_CUT_OFF,
    SERVING_CONFIG_INPUTS_FIELDS,
    SERVING_CONFIG_INPUTS_SCHEMA,
    STOP_REASON_UNREPORTED,
    reading_stop_reason,
)
from common.stage import _serving_config_inputs
from operations.serving.config import CONFIG_INPUTS_SCHEMA, ServingConfigInputs


def test_serving_config_serializer_and_validators_share_one_contract() -> None:
    inputs = ServingConfigInputs("1" * 64, "2" * 64)
    record = inputs.to_record()

    assert set(record) == SERVING_CONFIG_INPUTS_FIELDS
    assert record["schema"] == SERVING_CONFIG_INPUTS_SCHEMA
    assert CONFIG_INPUTS_SCHEMA == SERVING_CONFIG_INPUTS_SCHEMA
    assert ServingConfigInputs.from_record(record) == inputs
    assert _serving_config_inputs(record, "contract test") == record


def test_chair_call_record_field_set_is_closed_and_exact() -> None:
    assert isinstance(CHAIR_CALL_RECORD_FIELDS, frozenset)
    assert CHAIR_CALL_RECORD_SCHEMA == "chair-call-record.v4"
    assert CHAIR_CALL_RECORD_FIELDS == frozenset(
        {
            "schema",
            "chair",
            "resolved_identity",
            "resolved_revision",
            "serving_recipe",
            "served_model_id",
            "receipt_ref",
            "launch_audit_ref",
            "decoding_config_sha256",
            "kind",
            "request_sha256",
            "image_sha256s",
            "generation_sent",
            "generation_declared",
            "raw_response_ref",
            "response_sha256",
            "response_status",
            "response_model",
            "finish_reason",
            "usage",
            "parse_problem",
            "capacity",
            "sampling_effective",
            "usage_reconciliation",
        }
    )
    assert CALL_RECORD_SCHEMAS == {
        "chair-call-record.v4",
        "chair-transport-failure.v3",
        "chair-stream-call-record.v1",
        "chair-stream-transport-failure.v1",
        "chandra-native-call-record.v3",
        "chandra-native-transport-failure.v3",
    }
    assert CALLER_GENERATION_FIELDS == {"max_tokens", "chat_template_kwargs", "stop_token_ids"}
    assert CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA == "chair-transport-failure.v3"
    assert CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS == CHAIR_CALL_RECORD_FIELDS | {"transport_problem"}
    assert CHAIR_TRANSPORT_PROBLEM_SCHEMA == "chair-transport-problem.v1"
    assert CHAIR_TRANSPORT_PROBLEM_FIELDS == {
        "schema",
        "code",
        "detail",
        "definitively_absent",
        "request_delivery",
        "response_completion",
    }

    assert CHANDRA_NATIVE_CALL_RECORD_SCHEMA == "chandra-native-call-record.v3"
    assert CHANDRA_NATIVE_CALL_RECORD_FIELDS == CHAIR_CALL_RECORD_FIELDS | {
        "native_attempt_intent_ref"
    }
    assert CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA == "chandra-native-transport-failure.v3"
    assert CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS == (
        CHANDRA_NATIVE_CALL_RECORD_FIELDS | {"transport_problem"}
    )


def test_the_two_engine_stop_vocabularies_are_frozensets_with_exact_members() -> None:
    assert isinstance(ENGINE_STOP_COMPLETE, frozenset)
    assert isinstance(ENGINE_STOP_CUT_OFF, frozenset)
    assert ENGINE_STOP_COMPLETE == frozenset({"stop"})
    assert ENGINE_STOP_CUT_OFF == frozenset({"length"})
    # The two vocabularies never overlap: one engine word is never both a
    # complete stop and a cut-off in the same reading.
    assert not (ENGINE_STOP_COMPLETE & ENGINE_STOP_CUT_OFF)


def test_a_reading_records_an_engine_finish_as_stop_length_or_none() -> None:
    assert [reading_stop_reason(word) for word in ("stop", "length", None)] == [
        "stop",
        "length",
        None,
    ]
    with pytest.raises(ValueError, match="neither a completion nor a cut"):
        reading_stop_reason("tool_calls")


def test_stop_reason_unreported_is_its_own_word_outside_both_vocabularies() -> None:
    assert STOP_REASON_UNREPORTED == "unreported"
    assert STOP_REASON_UNREPORTED not in ENGINE_STOP_COMPLETE
    assert STOP_REASON_UNREPORTED not in ENGINE_STOP_CUT_OFF
