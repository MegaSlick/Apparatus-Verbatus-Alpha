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

CHAIR_CALL_RECORD_SCHEMA: Final = "chair-call-record.v1"
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
        "raw_response_ref",
        "response_sha256",
        "response_model",
        "finish_reason",
        "usage",
        "parse_problem",
    }
)

# The engine's own stop-reason vocabulary, split by what it means for a
# reading: `ENGINE_STOP_COMPLETE` is the engine's word for "the model chose to
# stop"; `ENGINE_STOP_CUT_OFF` is its word for "a length bound ended
# generation before the model did".  Anything else is an unrecognized engine
# string, refused by name rather than folded into either bucket.
ENGINE_STOP_COMPLETE: Final = frozenset({"stop"})
ENGINE_STOP_CUT_OFF: Final = frozenset({"length"})

# The transport word recorded when an engine's response carries no
# `finish_reason` at all — never a default for a value that has meaning; a
# label for its literal absence.
STOP_REASON_UNREPORTED: Final = "unreported"

# A vendor's decoding value is sometimes a float: DAI's carried
# `generation_config.json` names `repetition_penalty` 1.05 and `top_p` 0.001,
# and those exact numbers go on the wire. The canonical writer refuses floats
# outright, deliberately — their JSON form is not stable enough to hash
# against — so a call record cannot carry the Python float and must not carry a
# rounded stand-in for it either. What it carries instead is the *exact decimal
# text the request body itself contains*, tagged so a reader can tell a number
# recorded this way from a string the vendor really declared. `json.dumps`
# emits the shortest text that reads back as the identical double, so nothing
# is lost and nothing is invented: the record is a transcription of the bytes
# sent, not a re-measurement of them.
WIRE_DECIMAL_SCHEMA: Final = "wire-decimal.v1"
WIRE_DECIMAL_FIELDS: Final = frozenset({"schema", "decimal"})

# What kind of bytes a live Testimonium's `raw_response_ref` names. A live act
# record reaches its retained blob by two different routes — the adapter's own
# output bytes when a parser ran over them, and the whole transport body when
# no adapter ever saw a reading — and the two are not interchangeable evidence.
# The record names which one it holds rather than leaving a later reader to
# infer it from which other fields happen to be present.
RAW_RESPONSE_MODEL_OUTPUT: Final = "model-output"
RAW_RESPONSE_TRANSPORT_BODY: Final = "transport-response-body"
RAW_RESPONSE_KINDS: Final = frozenset({RAW_RESPONSE_MODEL_OUTPUT, RAW_RESPONSE_TRANSPORT_BODY})
