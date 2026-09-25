"""Shared closed shapes and vocabularies for the live reading seam.

Constants only.  ``common/`` must never import ``operations/`` — stages and
``operations/`` both import this module, and the shared shape has to sit
somewhere neither depends on the other's package.
"""

from typing import Final

SERVING_CONFIG_INPUTS_SCHEMA: Final = "serving-config-inputs.v1"
SERVING_CONFIG_INPUTS_FIELDS: Final = frozenset(
    {"schema", "serving_recipes_sha256", "pod_placement_sha256"}
)

CHAIR_CALL_RECORD_SCHEMA_V1: Final = "chair-call-record.v1"
CHAIR_CALL_RECORD_SCHEMA: Final = "chair-call-record.v2"
CHAIR_CALL_RECORD_SCHEMAS: Final = frozenset(
    {CHAIR_CALL_RECORD_SCHEMA_V1, CHAIR_CALL_RECORD_SCHEMA}
)
CHAIR_CALL_RECORD_FIELDS_V1: Final = frozenset(
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
        "response_model",
        "finish_reason",
        "usage",
        "parse_problem",
        # The request-capacity record checked before the request was built, or
        # null (readiness probe, smoke path).
        "capacity",
    }
)
CHAIR_CALL_RECORD_FIELDS: Final = CHAIR_CALL_RECORD_FIELDS_V1 | frozenset({"response_status"})

# Chandra native route only. The intent reference tells equal wire bodies at
# retry ordinals 5..7 apart after a crash, so recovery never guesses.
CHANDRA_NATIVE_CALL_RECORD_SCHEMA: Final = "chandra-native-call-record.v1"
CHANDRA_NATIVE_CALL_RECORD_FIELDS: Final = CHAIR_CALL_RECORD_FIELDS | frozenset(
    {"native_attempt_intent_ref"}
)
CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA: Final = "chandra-native-transport-failure.v1"
CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS: Final = (
    CHANDRA_NATIVE_CALL_RECORD_FIELDS | frozenset({"transport_problem"})
)

CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA: Final = "chair-transport-failure.v1"
CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS: Final = CHAIR_CALL_RECORD_FIELDS | frozenset(
    {"transport_problem"}
)
CHAIR_TRANSPORT_PROBLEM_SCHEMA: Final = "chair-transport-problem.v1"
CHAIR_TRANSPORT_PROBLEM_FIELDS: Final = frozenset(
    {
        "schema",
        "code",
        "detail",
        "definitively_absent",
        "request_delivery",
        "response_completion",
    }
)

# The engine's stop words: the model chose to stop, or a length bound cut it
# off. Anything else is refused by name.
ENGINE_STOP_COMPLETE: Final = frozenset({"stop"})
ENGINE_STOP_CUT_OFF: Final = frozenset({"length"})

# Recorded when a response carries no `finish_reason`: a label for absence.
STOP_REASON_UNREPORTED: Final = "unreported"

# A vendor float on the wire (DAI's `top_p` 0.001) is recorded as the exact
# decimal text the request body contains, tagged, because canonical artifacts
# refuse floats. `json.dumps` text reads back as the identical double.
WIRE_DECIMAL_SCHEMA: Final = "wire-decimal.v1"
WIRE_DECIMAL_FIELDS: Final = frozenset({"schema", "decimal"})

# Which bytes a live Testimonium's `raw_response_ref` names: the adapter's output,
# or the whole transport body when no adapter saw a reading. Not interchangeable.
RAW_RESPONSE_MODEL_OUTPUT: Final = "model-output"
RAW_RESPONSE_TRANSPORT_BODY: Final = "transport-response-body"
RAW_RESPONSE_KINDS: Final = frozenset({RAW_RESPONSE_MODEL_OUTPUT, RAW_RESPONSE_TRANSPORT_BODY})
