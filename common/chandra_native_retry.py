"""The revision-pinned Chandra vLLM retry recipe admitted for Attestator 1.

This is a literal port of the inference loop in ``datalab-to/chandra`` at the
revision below.  It is deliberately a closed capability, not a general retry
or sampling API: callers may ask for one of the seven declared attempts and
may derive the vendor's retry trigger from the result of that attempt.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from common.contracts.canonical import is_sha256
from common.contracts.errors import SchemaRefusal

CHANDRA_SOURCE_REPOSITORY: Final = "https://github.com/datalab-to/chandra"
CHANDRA_SOURCE_REVISION: Final = "d4f7467435aa4137d9539f000ddf0b7ced3eb43f"
CHANDRA_RECIPE_PATH: Final = "chandra/model/vllm.py"
CHANDRA_DETECTOR_PATH: Final = "chandra/model/util.py::detect_repeat_token"
CHANDRA_SETTINGS_PATH: Final = "chandra/settings.py"
CHANDRA_MAX_OUTPUT_TOKENS: Final = 12_384
CHANDRA_MAX_RETRIES: Final = 6
CHANDRA_MAX_ATTEMPTS: Final = CHANDRA_MAX_RETRIES + 1

# Decimal text is the provenance form.  The serving boundary converts it to a
# JSON number only while building the exact wire body, then records the number
# through ``wire-decimal.v1`` like every other non-integral generation value.
CHANDRA_PARAMETER_SCHEDULE: Final = (
    ("0", "0.1"),
    ("0.2", "0.95"),
    ("0.4", "0.95"),
    ("0.6", "0.95"),
    ("0.8", "0.95"),
    ("0.8", "0.95"),
    ("0.8", "0.95"),
)
CHANDRA_ERROR_BACKOFF_SECONDS: Final = (2, 4, 6, 8, 10, 12)

RECIPE_SCHEMA: Final = "chandra-native-inference.v1"
INTENT_SCHEMA: Final = "chandra-native-attempt-intent.v1"
ATTEMPT_SCHEMA: Final = "chandra-native-attempt.v1"
TRACE_SCHEMA: Final = "chandra-native-retry-trace.v1"
TRIGGERS: Final = frozenset({"repeat-token", "inference-error"})


def recipe_record() -> dict[str, Any]:
    """Return the exact upstream declaration sealed into new runs."""

    return {
        "schema": RECIPE_SCHEMA,
        "source_repository": CHANDRA_SOURCE_REPOSITORY,
        "source_revision": CHANDRA_SOURCE_REVISION,
        "recipe_path": CHANDRA_RECIPE_PATH,
        "detector_path": CHANDRA_DETECTOR_PATH,
        "settings_path": CHANDRA_SETTINGS_PATH,
        "max_output_tokens": CHANDRA_MAX_OUTPUT_TOKENS,
        "max_retries": CHANDRA_MAX_RETRIES,
        "temperature_schedule": [row[0] for row in CHANDRA_PARAMETER_SCHEDULE],
        "initial_top_p": CHANDRA_PARAMETER_SCHEDULE[0][1],
        "retry_top_p": CHANDRA_PARAMETER_SCHEDULE[1][1],
        "error_backoff_seconds": list(CHANDRA_ERROR_BACKOFF_SECONDS),
    }


def validate_policy_record(value: Any) -> dict[str, Any]:
    """Require a config section to equal the admitted recipe, field for field."""

    expected = recipe_record()
    if not isinstance(value, dict) or value != expected:
        raise SchemaRefusal(
            "decoding chandra_native_inference must equal the revision-pinned Chandra native recipe"
        )
    return value


def attempt_parameters(attempt_ordinal: int) -> dict[str, str]:
    """The exact temperature/top-p pair for one 1-based physical request."""

    if (
        not isinstance(attempt_ordinal, int)
        or isinstance(attempt_ordinal, bool)
        or not 1 <= attempt_ordinal <= CHANDRA_MAX_ATTEMPTS
    ):
        raise SchemaRefusal(f"Chandra native attempt ordinal must be in 1..{CHANDRA_MAX_ATTEMPTS}")
    temperature, top_p = CHANDRA_PARAMETER_SCHEDULE[attempt_ordinal - 1]
    return {"temperature": temperature, "top_p": top_p}


def wire_parameters(attempt_ordinal: int) -> dict[str, float]:
    """Convert the declared decimal texts to the numbers the wire carries."""

    parameters = attempt_parameters(attempt_ordinal)
    return {name: float(value) for name, value in parameters.items()}


def error_backoff_seconds(attempt_ordinal: int) -> int | None:
    """The upstream sleep after an error, or ``None`` after the final call."""

    attempt_parameters(attempt_ordinal)
    if attempt_ordinal == CHANDRA_MAX_ATTEMPTS:
        return None
    return CHANDRA_ERROR_BACKOFF_SECONDS[attempt_ordinal - 1]


def detect_repeat_token(
    predicted_tokens: str,
    base_max_repeats: int = 4,
    window_size: int = 500,
    cut_from_end: int = 0,
    scaling_factor: float = 3.0,
) -> bool:
    """Literal port of the pinned vendor's ``detect_repeat_token``."""

    if cut_from_end > 0:
        predicted_tokens = predicted_tokens[:-cut_from_end]

    for seq_len in range(1, window_size // 2 + 1):
        candidate_seq = predicted_tokens[-seq_len:]
        max_repeats = int(base_max_repeats * (1 + scaling_factor / seq_len))
        repeat_count = 0
        pos = len(predicted_tokens) - seq_len
        if pos < 0:
            continue
        while pos >= 0:
            if predicted_tokens[pos : pos + seq_len] == candidate_seq:
                repeat_count += 1
                pos -= seq_len
            else:
                break
        if repeat_count > max_repeats:
            return True
    return False


def retry_trigger(raw: str, *, inference_error: bool, attempt_ordinal: int) -> str | None:
    """Return the pinned vendor trigger, preserving its predicate order.

    The repetition probes run before the error branch in upstream Chandra.
    The final physical request may exhibit either condition, but it cannot
    trigger another request because ``retries < max_retries`` is then false.
    """

    attempt_parameters(attempt_ordinal)
    has_repeat = detect_repeat_token(raw) or (
        len(raw) > 50 and detect_repeat_token(raw, cut_from_end=50)
    )
    if attempt_ordinal < CHANDRA_MAX_ATTEMPTS and has_repeat:
        return "repeat-token"
    if attempt_ordinal < CHANDRA_MAX_ATTEMPTS and inference_error:
        return "inference-error"
    return None


def exhausted_condition(raw: str, *, inference_error: bool) -> str | None:
    """Name the condition the seventh result still exhibits, if any."""

    has_repeat = detect_repeat_token(raw) or (
        len(raw) > 50 and detect_repeat_token(raw, cut_from_end=50)
    )
    if has_repeat:
        return "repeat-token"
    if inference_error:
        return "inference-error"
    return None


def validate_trace(value: Any) -> dict[str, Any]:
    """Close the compact trace carried by each final Chandra Testimonium."""

    required = {
        "schema",
        "recipe",
        "physical_request_count",
        "returned_attempt_ordinal",
        "exhausted_condition",
        "attempts",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SchemaRefusal("a Chandra native retry trace is not its closed schema")
    if value["schema"] != TRACE_SCHEMA or value["recipe"] != recipe_record():
        raise SchemaRefusal("a Chandra native retry trace names a different recipe")
    attempts = value["attempts"]
    count = value["physical_request_count"]
    returned = value["returned_attempt_ordinal"]
    if (
        not isinstance(attempts, list)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= CHANDRA_MAX_ATTEMPTS
        or len(attempts) != count
        or returned != count
    ):
        raise SchemaRefusal(
            "a Chandra native retry trace does not reconcile its physical requests "
            "with the vendor-returned final attempt"
        )
    exhausted = value["exhausted_condition"]
    if exhausted is not None and exhausted not in TRIGGERS:
        raise SchemaRefusal("a Chandra native retry trace has an unknown exhaustion condition")
    for expected, row in enumerate(attempts, 1):
        if not isinstance(row, dict) or set(row) != {
            "attempt_ordinal",
            "parameters",
            "intent_ref",
            "attempt_ref",
            "trigger",
            "error",
        }:
            raise SchemaRefusal("a Chandra native retry trace has an open attempt row")
        if row["attempt_ordinal"] != expected or row["parameters"] != attempt_parameters(expected):
            raise SchemaRefusal("a Chandra native retry trace has a moved attempt schedule")
        if row["trigger"] is not None and row["trigger"] not in TRIGGERS:
            raise SchemaRefusal("a Chandra native retry trace has an unknown trigger")
        if expected < count and row["trigger"] is None:
            raise SchemaRefusal("a Chandra native retry trace continued without a trigger")
        if expected == count and row["trigger"] is not None:
            raise SchemaRefusal("the vendor-returned Chandra attempt still claims a retry trigger")
        if not isinstance(row["error"], bool):
            raise SchemaRefusal("a Chandra native retry trace has no boolean error fact")
        for field in ("intent_ref", "attempt_ref"):
            ref = row[field]
            if (
                not isinstance(ref, dict)
                or set(ref) != {"relative_path", "sha256"}
                or not isinstance(ref["relative_path"], str)
                or not ref["relative_path"].strip()
                or not is_sha256(ref["sha256"])
            ):
                raise SchemaRefusal(f"a Chandra native retry trace has an invalid {field}")
    return value


def named_trace_summary(trace: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded facts a named dossier may carry without earlier text."""

    checked = validate_trace(dict(trace))
    return {
        "recipe": checked["recipe"],
        "physical_request_count": checked["physical_request_count"],
        "returned_attempt_ordinal": checked["returned_attempt_ordinal"],
        "exhausted_condition": checked["exhausted_condition"],
        "attempts": [
            {
                "attempt_ordinal": row["attempt_ordinal"],
                "parameters": row["parameters"],
                "trigger": row["trigger"],
                "error": row["error"],
            }
            for row in checked["attempts"]
        ],
    }


def refuse_orphan_intent(intent_reused: bool) -> None:
    """Never replay an ordinal whose durable intent lacks terminal evidence."""

    if intent_reused:
        raise SchemaRefusal(
            "a Chandra native attempt intent exists without terminal evidence. Request "
            "delivery is unknown; this ordinal will not be replayed and requires manual "
            "intervention"
        )
