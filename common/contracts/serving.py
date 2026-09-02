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
