"""Shared closed shapes and vocabularies for the live reading seam.

Constants only.  ``common/`` must never import ``operations/`` — stages and
``operations/`` both import this module, and the shared shape has to sit
somewhere neither depends on the other's package.
"""

from typing import Final

# The two digests are the files' seals (`common/sealed_config.py`).
SERVING_CONFIG_INPUTS_SCHEMA: Final = "serving-config-inputs.v2"
SERVING_CONFIG_INPUTS_FIELDS: Final = frozenset(
    {"schema", "serving_recipes_sha256", "pod_placement_sha256"}
)

# `sampling_effective` carries the values the pinned engine samples under for the
# sampling fields `generation_sent` carries (`common.decoding`).
# The serving manager's operational record of one launch, kept beside its receipt.
SERVING_LAUNCH_AUDIT_SCHEMA: Final = "serving-launch-audit.v2"

CHAIR_CALL_RECORD_SCHEMA: Final = "chair-call-record.v4"
CHAIR_CALL_RECORD_FIELDS: Final = frozenset(
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
        "sampling_effective",
        "raw_response_ref",
        "response_sha256",
        "response_status",
        "response_model",
        "finish_reason",
        "usage",
        "parse_problem",
        # The request-capacity record checked before the request was built, or
        # null (readiness probe, smoke path).
        "capacity",
        # The engine's reported token counts beside the capacity record's, with
        # named disagreements, or null when either side is missing.
        "usage_reconciliation",
    }
)

# The only generation fields a caller's request may carry, for every chair
# alike: the answer bound, the chat-template switch and a stop-id list. Each
# chair's request builder decides which of them it sends; the sampling values
# are the sealed decoding table's, and `model`, `stream`, `seed` and `n` the
# serving client's.
CALLER_GENERATION_FIELDS: Final = frozenset(
    {"max_tokens", "chat_template_kwargs", "stop_token_ids"}
)

# Chandra native route only. The intent reference tells equal wire bodies at
# retry ordinals 5..7 apart after a crash, so recovery never guesses.
CHANDRA_NATIVE_CALL_RECORD_SCHEMA: Final = "chandra-native-call-record.v3"
CHANDRA_NATIVE_CALL_RECORD_FIELDS: Final = CHAIR_CALL_RECORD_FIELDS | frozenset(
    {"native_attempt_intent_ref"}
)
CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA: Final = "chandra-native-transport-failure.v3"
CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS: Final = (
    CHANDRA_NATIVE_CALL_RECORD_FIELDS | frozenset({"transport_problem"})
)

CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA: Final = "chair-transport-failure.v3"
CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS: Final = CHAIR_CALL_RECORD_FIELDS | frozenset(
    {"transport_problem"}
)

# A call whose reply was streamed so a repetition loop could stop it early (the
# Perlector's page reading, `common/repetition_loop.py`). `raw_response_ref` names
# the server-sent event bytes exactly as received, up to the stop; `stream` is
# `{schema, loop_guard, stopped}`: the sealed guard the reply was watched under,
# and the loop that stopped it, or null when the engine ended the stream itself.
# Every other field is the plain call record's. Calls that are not streamed keep
# the plain schemas above.
CHAIR_STREAM_CALL_RECORD_SCHEMA: Final = "chair-stream-call-record.v1"
CHAIR_STREAM_CALL_RECORD_FIELDS: Final = CHAIR_CALL_RECORD_FIELDS | frozenset({"stream"})
CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA: Final = "chair-stream-transport-failure.v1"
CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_FIELDS: Final = CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS | (
    frozenset({"stream"})
)
CHAIR_STREAM_SCHEMA: Final = "chair-stream.v1"
# Every schema a retained call record or transport failure may be written under.
CALL_RECORD_SCHEMAS: Final = frozenset(
    {
        CHAIR_CALL_RECORD_SCHEMA,
        CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
        CHAIR_STREAM_CALL_RECORD_SCHEMA,
        CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA,
        CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
        CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
    }
)
CHAIR_STREAM_FIELDS: Final = frozenset({"schema", "loop_guard", "stopped"})
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
# Not an engine word: the client abandoned a streamed reply on a repetition loop.
# A reading records it as its `stop_reason`, with the engine's own `finish_reason`
# (none, as a rule) beside it.
READER_STOP_REPETITION_LOOP: Final = "repetition-loop"


def reading_stop_reason(finish_reason: str | None) -> str | None:
    """An engine's finish word as a reading records it: `"stop"`, `"length"` or `None`.

    `None` when the engine gave none. Any other word raises `ValueError`: a
    reading is never recorded under a finish this build does not recognize.
    """
    if finish_reason is None:
        return None
    if finish_reason in ENGINE_STOP_COMPLETE:
        return "stop"
    if finish_reason in ENGINE_STOP_CUT_OFF:
        return "length"
    raise ValueError(f"engine finish reason {finish_reason!r} is neither a completion nor a cut")


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
