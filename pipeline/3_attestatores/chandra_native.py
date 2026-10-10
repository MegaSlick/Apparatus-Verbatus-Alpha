"""Chandra's pinned vendor retry loop, run against a served chair, and its evidence.

Each physical request has an intent record and a terminal record. A page is
read in three steps so pages can be read side by side and still sealed in page
order: `begin_page` reads what an earlier pass sealed, `read_page` runs the
loop and builds its records without writing them, and `seal_page` writes them,
intent before terminal, when the page's turn comes. An intent sealed without
its terminal is delivery-unknown, and a resume never re-sends it. The loop's
final attempt becomes the page's reading; every request it made is traced on
the record.
"""

from __future__ import annotations

import json
import time
from typing import Any, Final, Mapping, NamedTuple

import live_witness
import witness_adapters
from attempt import Attempt, no_response_health, unrecordable_health
from retained import (
    is_positive_int,
    named_once,
    sorted_refs,
    validate_raw_response_ref,
    validate_retained_response_blob,
    validate_stage_blob_ref,
)

from common.chairs.models import ChairIdentity
from common.chandra_native_retry import (
    ATTEMPT_SCHEMA as CHANDRA_ATTEMPT_SCHEMA,
)
from common.chandra_native_retry import (
    CHANDRA_MAX_ATTEMPTS,
    CHANDRA_MAX_OUTPUT_TOKENS,
)
from common.chandra_native_retry import (
    INTENT_SCHEMA as CHANDRA_INTENT_SCHEMA,
)
from common.chandra_native_retry import (
    TRACE_SCHEMA as CHANDRA_TRACE_SCHEMA,
)
from common.chandra_native_retry import (
    attempt_parameters as chandra_attempt_parameters,
)
from common.chandra_native_retry import (
    error_backoff_seconds as chandra_error_backoff_seconds,
)
from common.chandra_native_retry import (
    exhausted_condition as chandra_exhausted_condition,
)
from common.chandra_native_retry import (
    recipe_record as chandra_recipe_record,
)
from common.chandra_native_retry import (
    refuse_orphan_intent as refuse_chandra_orphan_intent,
)
from common.chandra_native_retry import (
    retry_trigger as chandra_retry_trigger,
)
from common.chandra_native_retry import (
    validate_trace as validate_chandra_trace,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.contracts.envelope import read_verified
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.serving import (
    CHANDRA_NATIVE_CALL_RECORD_FIELDS,
    CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
    RAW_RESPONSE_MODEL_OUTPUT,
    RAW_RESPONSE_TRANSPORT_BODY,
)
from common.contracts.stages import ATTESTATORES
from common.decoding import (
    SAMPLING_FIELDS,
    verify_call_sampling,
)
from common.native_witness import (
    validate_capture_text_view,
    validate_native_capture,
)
from common.stage import (
    sealed_decoding_policy,
)
from operations.serving.assembly import (
    retain_chair_bytes,
)
from operations.serving.client import ChairClient
from operations.serving.errors import (
    ChairResponseRefusal,
    ChairTransportFailure,
    ServingError,
)


def _chandra_native_subject(page_subject_id: str, chair: str, witness_attempt_ordinal: int) -> str:
    return f"{page_subject_id}:{chair}:witness-{witness_attempt_ordinal}"


def _chandra_native_artifact_ref(
    context, kind: str, subject_id: str, native_attempt_ordinal: int
) -> dict[str, str]:
    operation = (
        "chandra-native-intent"
        if kind == "chandra-native-attempt-intent"
        else "chandra-native-attempt"
    )
    native_attempt = attempt_id(subject_id, operation, native_attempt_ordinal)
    return context.artifact_ref(
        ATTESTATORES,
        kind,
        artifact_id(ATTESTATORES, kind, subject_id, native_attempt),
    )


_CHANDRA_RESULT_FIELDS: Final = (
    "outcome",
    "native_payload",
    "witness_reported",
    "format_capabilities",
    "health",
    "reason",
    "raw_response_ref",
    "native_capture",
    "serving_call_ref",
    "receipt_ref",
    "raw_response_kind",
)


def _attempt_evidence_record(attempt: Attempt) -> dict[str, Any]:
    return {field: getattr(attempt, field) for field in _CHANDRA_RESULT_FIELDS}


def _attempt_from_evidence_record(context, value: Any) -> Attempt:
    if not isinstance(value, dict) or set(value) != set(_CHANDRA_RESULT_FIELDS):
        raise SchemaRefusal("a Chandra native terminal artifact has no closed result record")
    observation_payload = None
    capture = value["native_capture"]
    if capture is not None:
        validate_capture_text_view(validate_native_capture(capture))
        reference = validate_raw_response_ref(capture["raw_response_ref"])
        observation_payload = read_verified(
            context.tree.read_bytes,
            reference,
            "a Chandra native terminal artifact's model output",
        )
    return Attempt(**value, observation_payload=observation_payload)


def _chandra_error_attempt(error: ServingError, adapter: Any) -> Attempt:
    reason = f"the Chandra native inference call failed: {error}"
    raw_response_ref = getattr(error, "raw_response_ref", None)
    return Attempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=witness_adapters.declared_format_capabilities(adapter),
        health=no_response_health(reason=reason),
        reason=reason,
        raw_response_ref=dict(raw_response_ref) if raw_response_ref is not None else None,
        native_capture=None,
        serving_call_ref=dict(error.call_record_ref),
        receipt_ref=dict(error.receipt_ref),
        raw_response_kind=(RAW_RESPONSE_TRANSPORT_BODY if raw_response_ref is not None else None),
    )


_CHANDRA_APPLICATION_REFUSAL_PREFIX: Final = "retained Chandra response refused: "


def _chandra_application_refusal_attempt(
    context, response: Any, adapter: Any, error: ContractError, attempt: Attempt | None
) -> Attempt:
    """Retain a known post-response refusal as terminal evidence, never an orphan."""

    reason = _CHANDRA_APPLICATION_REFUSAL_PREFIX + str(error)
    if attempt is not None:
        return attempt._replace(outcome="failed", reason=reason)
    return _chandra_retained_failure(
        context, response, adapter, reason, health=no_response_health(reason=reason)
    )


def _chandra_retained_failure(
    context, response: Any, adapter: Any, reason: str, *, health: dict[str, Any]
) -> Attempt:
    """A failed attempt over a response that is kept but not read: its model output
    retained as terminal evidence, with no capture and no text."""
    model_output_ref = retain_chair_bytes(context, response.content.encode("utf-8"))
    return Attempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=witness_adapters.declared_format_capabilities(adapter),
        health=health,
        reason=reason,
        raw_response_ref=model_output_ref,
        observation_payload=None,
        native_capture=None,
        serving_call_ref=dict(response.call_record_ref),
        receipt_ref=dict(response.receipt_ref),
        raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
    )


def _raise_chandra_application_refusal(attempt: Attempt) -> None:
    reason = attempt.reason
    if isinstance(reason, str) and reason.startswith(_CHANDRA_APPLICATION_REFUSAL_PREFIX):
        raise ContractError(reason.removeprefix(_CHANDRA_APPLICATION_REFUSAL_PREFIX))


def _chandra_ref(value: Any, label: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"relative_path", "sha256"}
        or not isinstance(value.get("relative_path"), str)
        or not value["relative_path"].strip()
        or not is_sha256(value.get("sha256"))
    ):
        raise SchemaRefusal(f"a Chandra native {label} is not a content-addressed reference")
    return value


def _validate_chandra_intent(
    context,
    *,
    subject_id: str,
    native_attempt_ordinal: int,
    intent_ref: Any,
) -> dict[str, Any]:
    reference = _chandra_ref(intent_ref, "intent reference")
    record = context.tree.read_artifact_reference(
        reference,
        stage=ATTESTATORES,
        kind="chandra-native-attempt-intent",
        subject_id=subject_id,
    )
    payload = record.get("payload")
    required = {
        "schema",
        "recipe",
        "page_ordinal",
        "chair",
        "witness_attempt_ordinal",
        "native_attempt_ordinal",
        "parameters",
        "request_sha256",
        "request_body_ref",
        "image_sha256s",
        "receipt_ref",
        "compatibility",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise SchemaRefusal("a Chandra native attempt intent is not its closed schema")
    if (
        payload["schema"] != CHANDRA_INTENT_SCHEMA
        or payload["recipe"] != chandra_recipe_record()
        or payload["chair"] != "attestator_1"
        or payload["native_attempt_ordinal"] != native_attempt_ordinal
        or payload["parameters"] != chandra_attempt_parameters(native_attempt_ordinal)
        or not is_sha256(payload["request_sha256"])
        or not is_positive_int(payload["page_ordinal"])
        or not is_positive_int(payload["witness_attempt_ordinal"])
    ):
        raise SchemaRefusal("a Chandra native attempt intent moved from its pinned request")
    if payload["compatibility"] != {
        "scope": "attestator_1-page-chandra.v1",
        "per_request_seed": "omitted-to-match-pinned-upstream",
        "enable_thinking": "local-vllm-template-compatibility-false",
    }:
        raise SchemaRefusal("a Chandra native attempt intent moved its compatibility declaration")
    if not isinstance(payload["image_sha256s"], list) or any(
        not is_sha256(digest) for digest in payload["image_sha256s"]
    ):
        raise SchemaRefusal("a Chandra native attempt intent has invalid image digests")
    _chandra_ref(payload["receipt_ref"], "receipt reference")
    body_ref = validate_stage_blob_ref(payload["request_body_ref"], "request_body_ref")
    if body_ref["sha256"] != payload["request_sha256"]:
        raise SchemaRefusal("a Chandra native attempt intent's retained request body moved")
    read_verified(
        context.tree.read_bytes,
        body_ref,
        "a Chandra native attempt intent's retained request body",
    )
    expected_inputs = named_once(
        [
            {
                "relative_path": context.tree.blob_path(ATTESTATORES, digest),
                "sha256": digest,
            }
            for digest in payload["image_sha256s"]
        ]
        + [body_ref, payload["receipt_ref"]]
    )
    if record.get("inputs") != sorted_refs(expected_inputs):
        raise SchemaRefusal("a Chandra native attempt intent does not bind its exact request")
    return payload


def _chandra_conditions(
    raw: str, inference_error: bool, native_attempt_ordinal: int
) -> tuple[str | None, str | None]:
    """The retry trigger a response sets and the condition the loop returns with."""
    trigger = chandra_retry_trigger(
        raw, inference_error=inference_error, attempt_ordinal=native_attempt_ordinal
    )
    if native_attempt_ordinal == CHANDRA_MAX_ATTEMPTS:
        return trigger, chandra_exhausted_condition(raw, inference_error=inference_error)
    return trigger, trigger


def _chandra_backoff(completed_attempt_ordinal: int) -> None:
    delay = chandra_error_backoff_seconds(completed_attempt_ordinal)
    if delay is None:
        raise FatalAccounting("the final Chandra attempt requested an impossible retry")
    time.sleep(delay)


def _validated_chandra_serving_call(context, payload, native_attempt_ordinal, intent):
    resolved = _attempt_from_evidence_record(context, payload["resolved_attempt"])
    call_ref = validate_stage_blob_ref(resolved.serving_call_ref, "serving_call_ref")
    validate_retained_response_blob(context.tree, call_ref, "serving_call_ref")
    try:
        call_record = json.loads(context.tree.read_bytes(call_ref["relative_path"]))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SchemaRefusal("a Chandra native serving call record is not JSON") from error
    schemas = {
        CHANDRA_NATIVE_CALL_RECORD_SCHEMA: CHANDRA_NATIVE_CALL_RECORD_FIELDS,
        CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA: (
            CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS
        ),
    }
    expected_fields = (
        schemas.get(call_record.get("schema")) if isinstance(call_record, dict) else None
    )
    if expected_fields is None or set(call_record) != expected_fields:
        raise SchemaRefusal("a Chandra native serving call record is not its closed schema")
    configured = context.registry.resolve("attestator_1")
    if not isinstance(configured, ChairIdentity):
        raise SchemaRefusal("the Chandra native serving call names no configured Attestator 1")
    receipt = context.tree.read_run_receipt(intent["receipt_ref"])
    expected_receipt = {
        "chair": configured.role,
        "source": configured.source,
        "resolved": configured.source_reference,
        "revision": configured.receipt_revision,
        "revision_kind": configured.receipt_revision_kind,
        "digest_manifest": configured.digest_manifest,
    }
    if any(receipt.get(field) != value for field, value in expected_receipt.items()):
        raise SchemaRefusal(
            "a Chandra native serving receipt disagrees with Attestator 1's sealed identity"
        )
    if (
        call_record.get("resolved_identity") != configured.to_record()
        or call_record.get("resolved_revision") != configured.receipt_revision
        or call_record.get("serving_recipe") != configured.serving_recipe
        or call_record.get("kind") != "chat-completions"
        or not isinstance(call_record.get("served_model_id"), str)
        or not call_record["served_model_id"].strip()
    ):
        raise SchemaRefusal(
            "a Chandra native serving call record disagrees with its sealed chair identity"
        )
    context.require_sealed_config("decoding", call_record.get("decoding_config_sha256"))
    inference_error = (
        call_record["schema"] == CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA
        or call_record.get("parse_problem") is not None
    )
    if payload["error"] is not inference_error:
        raise SchemaRefusal(
            "a Chandra native terminal's error fact disagrees with its retained serving call"
        )
    if inference_error and resolved.outcome != "failed":
        raise SchemaRefusal("a Chandra native inference error retained a non-failed attempt")
    if inference_error:
        expected_error_code = (
            ChairTransportFailure.code
            if call_record["schema"] == CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA
            else call_record.get("parse_problem")
        )
        if payload["error_code"] != expected_error_code:
            raise SchemaRefusal(
                "a Chandra native terminal's error code disagrees with its retained serving call"
            )
        if (
            call_record["schema"] == CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA
            and payload["error_detail"] != call_record["transport_problem"]["detail"]
        ):
            raise SchemaRefusal(
                "a Chandra native terminal's transport error detail disagrees with its call"
            )
    sent = call_record.get("generation_sent")
    if (
        call_record.get("native_attempt_intent_ref") != payload["intent_ref"]
        or call_record.get("request_sha256") != payload["request_sha256"]
        or call_record.get("chair") != "attestator_1"
        or call_record.get("receipt_ref") != intent["receipt_ref"]
        or call_record.get("image_sha256s") != intent["image_sha256s"]
        or resolved.receipt_ref != intent["receipt_ref"]
        or call_record.get("generation_declared") != {"max_new_tokens": CHANDRA_MAX_OUTPUT_TOKENS}
        or not isinstance(sent, dict)
        or set(sent) - {"chat_template_kwargs", "max_tokens"} - SAMPLING_FIELDS
        or sent.get("chat_template_kwargs") != {"enable_thinking": False}
        or sent.get("max_tokens", CHANDRA_MAX_OUTPUT_TOKENS) != CHANDRA_MAX_OUTPUT_TOKENS
    ):
        raise SchemaRefusal("a Chandra native serving call record moved its pinned request")
    decoding_policy, _decoding_sha256 = sealed_decoding_policy(context)
    try:
        # The pinned upstream client sends no per-request seed.
        verify_call_sampling(
            call_record,
            decoding_policy,
            "attestator_1",
            attempt_ordinal=native_attempt_ordinal,
            expected_seed=None,
        )
    except ContractError as error:
        raise SchemaRefusal(
            f"a Chandra native serving call record moved its pinned request: {error}"
        ) from error
    return resolved, call_ref, call_record, inference_error


def _validate_chandra_terminal_response(
    context,
    payload,
    record,
    native_attempt_ordinal,
    resolved,
    call_ref,
    call_record,
    inference_error,
    conditions,
):
    response_ref = payload["transport_response_ref"]
    if response_ref is not None:
        validate_stage_blob_ref(response_ref, "transport_response_ref")
        validate_retained_response_blob(context.tree, response_ref, "transport_response_ref")
    if call_record.get("raw_response_ref") != response_ref:
        raise SchemaRefusal(
            "a Chandra native terminal names a different transport response than its call record"
        )
    if (
        (response_ref is None and call_record.get("response_sha256") is not None)
        or (
            response_ref is not None
            and call_record.get("response_sha256") != response_ref["sha256"]
        )
        or (
            not inference_error
            and call_record.get("response_model") != call_record.get("served_model_id")
        )
    ):
        raise SchemaRefusal(
            "a Chandra native terminal's retained response disagrees with its serving call"
        )
    if resolved.native_capture is not None and (
        resolved.raw_response_kind != RAW_RESPONSE_MODEL_OUTPUT
        or resolved.raw_response_ref != resolved.native_capture["raw_response_ref"]
    ):
        raise SchemaRefusal(
            "a Chandra native terminal's final model output disagrees with its retained capture"
        )
    if inference_error and (
        resolved.raw_response_ref != response_ref
        or (response_ref is not None and resolved.raw_response_kind != RAW_RESPONSE_TRANSPORT_BODY)
        or (response_ref is None and resolved.raw_response_kind is not None)
    ):
        raise SchemaRefusal(
            "a Chandra native error terminal disagrees with its retained transport response"
        )

    raw = ""
    if not inference_error:
        if resolved.raw_response_kind != RAW_RESPONSE_MODEL_OUTPUT:
            raise SchemaRefusal(
                "a successful Chandra native terminal retains no final model-output bytes"
            )
        model_output_ref = validate_raw_response_ref(resolved.raw_response_ref)
        model_output = read_verified(
            context.tree.read_bytes,
            model_output_ref,
            "a Chandra native terminal's final model output",
        )
        try:
            raw = model_output.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SchemaRefusal("a Chandra native terminal's model output is not UTF-8") from error
    if conditions != _chandra_conditions(raw, inference_error, native_attempt_ordinal):
        raise SchemaRefusal(
            "a Chandra native terminal's trigger disagrees with its retained response/error"
        )
    expected_inputs: list[dict[str, str]] = [payload["intent_ref"], call_ref]
    for reference in (
        response_ref,
        resolved.raw_response_ref,
        resolved.native_capture["raw_response_ref"] if resolved.native_capture else None,
    ):
        if reference is not None:
            expected_inputs.append(reference)
    if "inputs" in record and record["inputs"] != sorted_refs(named_once(expected_inputs)):
        raise SchemaRefusal("a Chandra native terminal artifact does not bind all call evidence")


def _validate_chandra_terminal(
    context,
    *,
    subject_id: str,
    native_attempt_ordinal: int,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    payload = record.get("payload")
    required = {
        "schema",
        "recipe",
        "native_attempt_ordinal",
        "parameters",
        "request_sha256",
        "intent_ref",
        "trigger",
        "returned_condition",
        "error",
        "error_code",
        "error_detail",
        "transport_response_ref",
        "resolved_attempt",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise SchemaRefusal("a Chandra native terminal artifact is not its closed schema")
    parameters = chandra_attempt_parameters(native_attempt_ordinal)
    trigger = payload["trigger"]
    returned = payload["returned_condition"]
    if (
        payload["schema"] != CHANDRA_ATTEMPT_SCHEMA
        or payload["recipe"] != chandra_recipe_record()
        or payload["native_attempt_ordinal"] != native_attempt_ordinal
        or payload["parameters"] != parameters
        or not is_sha256(payload["request_sha256"])
        or trigger not in {None, "repeat-token", "inference-error"}
        or returned not in {None, "repeat-token", "inference-error"}
        or not isinstance(payload["error"], bool)
    ):
        raise SchemaRefusal("a Chandra native terminal artifact moved from its pinned attempt")
    if native_attempt_ordinal < CHANDRA_MAX_ATTEMPTS and returned != trigger:
        raise SchemaRefusal("a Chandra native terminal artifact disagrees with its retry trigger")
    if native_attempt_ordinal == CHANDRA_MAX_ATTEMPTS and trigger is not None:
        raise SchemaRefusal("the seventh Chandra native terminal artifact still requests a retry")
    if payload["error"]:
        if not isinstance(payload["error_code"], str) or not payload["error_code"].strip():
            raise SchemaRefusal("a failed Chandra native attempt has no error code")
        if not isinstance(payload["error_detail"], str) or not payload["error_detail"].strip():
            raise SchemaRefusal("a failed Chandra native attempt has no error detail")
    elif payload["error_code"] is not None or payload["error_detail"] is not None:
        raise SchemaRefusal("a successful Chandra native attempt carries an invented error")

    intent = _validate_chandra_intent(
        context,
        subject_id=subject_id,
        native_attempt_ordinal=native_attempt_ordinal,
        intent_ref=payload["intent_ref"],
    )
    if intent["request_sha256"] != payload["request_sha256"]:
        raise SchemaRefusal("a Chandra native terminal artifact names a different intended request")

    resolved, call_ref, call_record, inference_error = _validated_chandra_serving_call(
        context, payload, native_attempt_ordinal, intent
    )
    _validate_chandra_terminal_response(
        context,
        payload,
        record,
        native_attempt_ordinal,
        resolved,
        call_ref,
        call_record,
        inference_error,
        (trigger, returned),
    )
    return payload


def trace_inputs(trace: dict[str, Any] | None) -> list[dict[str, str]]:
    if trace is None:
        return []
    checked = validate_chandra_trace(trace)
    return [
        reference
        for row in checked["attempts"]
        for reference in (row["intent_ref"], row["attempt_ref"])
    ]


_INTENT_KIND: Final = "chandra-native-attempt-intent"
_TERMINAL_KIND: Final = "chandra-native-attempt"


class _Unsealed(NamedTuple):
    """One record the loop made, written only when its page's turn comes."""

    fields: dict[str, Any]  # `context.publish` keyword arguments
    reference: dict[str, str]  # what the published artifact's reference will name


def _unsealed(context, **fields: Any) -> _Unsealed:
    envelope = context.envelope(**fields)
    return _Unsealed(
        fields,
        {
            "relative_path": context.tree.artifact_path(
                ATTESTATORES, fields["kind"], envelope["artifact_id"]
            ),
            "sha256": digest_bytes(canonical_bytes(envelope)),
        },
    )


def _chandra_intent_record(
    context,
    *,
    subject_id: str,
    page_ordinal: int,
    chair: str,
    witness_attempt_ordinal: int,
    native_attempt_ordinal: int,
    dispatch: Any,
    receipt_ref: Mapping[str, str],
) -> _Unsealed:
    native_attempt = attempt_id(subject_id, "chandra-native-intent", native_attempt_ordinal)
    image_refs = [
        {
            "relative_path": context.tree.blob_path(ATTESTATORES, digest),
            "sha256": digest,
        }
        for digest in dispatch.request.image_sha256s
    ]
    request_body_ref = retain_chair_bytes(context, dispatch.body)
    payload = {
        "schema": CHANDRA_INTENT_SCHEMA,
        "recipe": chandra_recipe_record(),
        "page_ordinal": page_ordinal,
        "chair": chair,
        "witness_attempt_ordinal": witness_attempt_ordinal,
        "native_attempt_ordinal": native_attempt_ordinal,
        "parameters": chandra_attempt_parameters(native_attempt_ordinal),
        "request_sha256": dispatch.request_sha256,
        "request_body_ref": request_body_ref,
        "image_sha256s": list(dispatch.request.image_sha256s),
        "receipt_ref": dict(receipt_ref),
        "compatibility": {
            "scope": "attestator_1-page-chandra.v1",
            "per_request_seed": "omitted-to-match-pinned-upstream",
            "enable_thinking": "local-vllm-template-compatibility-false",
        },
    }
    return _unsealed(
        context,
        kind=_INTENT_KIND,
        subject_id=subject_id,
        outcome="recorded",
        attempt=native_attempt,
        inputs=named_once(image_refs + [request_body_ref, dict(receipt_ref)]),
        payload=payload,
    )


def _chandra_terminal_record(
    context,
    *,
    subject_id: str,
    native_attempt_ordinal: int,
    intent_ref: dict[str, str],
    request_sha256: str,
    attempt: Attempt,
    trigger: str | None,
    returned_condition: str | None,
    error: bool,
    error_code: str | None,
    error_detail: str | None,
    transport_response_ref: dict[str, str] | None,
) -> _Unsealed:
    native_attempt = attempt_id(subject_id, "chandra-native-attempt", native_attempt_ordinal)
    payload = {
        "schema": CHANDRA_ATTEMPT_SCHEMA,
        "recipe": chandra_recipe_record(),
        "native_attempt_ordinal": native_attempt_ordinal,
        "parameters": chandra_attempt_parameters(native_attempt_ordinal),
        "request_sha256": request_sha256,
        "intent_ref": intent_ref,
        "trigger": trigger,
        "returned_condition": returned_condition,
        "error": error,
        "error_code": error_code,
        "error_detail": error_detail,
        "transport_response_ref": transport_response_ref,
        "resolved_attempt": _attempt_evidence_record(attempt),
    }
    refs: list[dict[str, str]] = [intent_ref]
    for reference in (
        attempt.serving_call_ref,
        transport_response_ref,
        attempt.raw_response_ref,
        attempt.native_capture["raw_response_ref"] if attempt.native_capture else None,
    ):
        if reference is not None:
            refs.append(reference)
    return _unsealed(
        context,
        kind=_TERMINAL_KIND,
        subject_id=subject_id,
        outcome="recorded",
        attempt=native_attempt,
        inputs=named_once(refs),
        payload=payload,
    )


def _publish_chandra_intent(context, record: _Unsealed) -> None:
    published = context.publish(**record.fields)
    if published.reused:
        # An intent this page's resume did not find has no terminal: its request
        # may have reached vLLM in another pass. An operator must decide.
        refuse_chandra_orphan_intent()
    if context.input_ref(record.reference["relative_path"]) != record.reference:
        raise FatalAccounting(
            "a sealed Chandra native intent differs from the one its request named"
        )


def _publish_chandra_terminal(context, record: _Unsealed) -> None:
    payload = record.fields["payload"]
    _validate_chandra_terminal(
        context,
        subject_id=record.fields["subject_id"],
        native_attempt_ordinal=payload["native_attempt_ordinal"],
        record={"payload": payload},
    )
    context.publish(**record.fields)


def _sealed_chandra_records(context, subject_id: str, kind: str, what: str):
    """Yield each retained `kind` record for the subject with its checked native ordinal."""
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != kind or entry["subject_id"] != subject_id:
            continue
        record = context.tree.read_artifact(ATTESTATORES, kind, entry["artifact_id"])
        payload = record.get("payload")
        ordinal = payload.get("native_attempt_ordinal") if isinstance(payload, dict) else None
        if not is_positive_int(ordinal) or ordinal > CHANDRA_MAX_ATTEMPTS:
            raise SchemaRefusal(f"a retained Chandra native {what} has no valid ordinal")
        yield entry, record, ordinal


def _by_native_ordinal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda record: record["payload"]["native_attempt_ordinal"])


def _sealed_chandra_intents(context, subject_id: str) -> list[dict[str, Any]]:
    """Return the validated intent chain, including a possible unmatched tail."""
    rows: list[dict[str, Any]] = []
    kind = "chandra-native-attempt-intent"
    for entry, record, ordinal in _sealed_chandra_records(context, subject_id, kind, "intent"):
        expected_artifact_id = artifact_id(
            ATTESTATORES, kind, subject_id, attempt_id(subject_id, "chandra-native-intent", ordinal)
        )
        if entry["artifact_id"] != expected_artifact_id:
            raise SchemaRefusal("a retained Chandra native intent has a moved identity")
        _validate_chandra_intent(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=ordinal,
            intent_ref=context.artifact_ref(ATTESTATORES, kind, entry["artifact_id"]),
        )
        rows.append(record)
    return _by_native_ordinal(rows)


def _sealed_chandra_attempts(context, subject_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _entry, record, ordinal in _sealed_chandra_records(
        context, subject_id, "chandra-native-attempt", "terminal"
    ):
        _validate_chandra_terminal(
            context, subject_id=subject_id, native_attempt_ordinal=ordinal, record=record
        )
        rows.append(record)
    return _by_native_ordinal(rows)


def _chandra_retry_trace(
    context, subject_id: str, terminal_records: list[dict[str, Any]]
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for expected, record in enumerate(terminal_records, 1):
        payload = _validate_chandra_terminal(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=expected,
            record=record,
        )
        attempts.append(
            {
                "attempt_ordinal": expected,
                "parameters": payload["parameters"],
                "intent_ref": payload["intent_ref"],
                "attempt_ref": _chandra_native_artifact_ref(
                    context, "chandra-native-attempt", subject_id, expected
                ),
                "trigger": payload["trigger"],
                "error": payload["error"],
            }
        )
    final = terminal_records[-1]["payload"]
    trace = {
        "schema": CHANDRA_TRACE_SCHEMA,
        "recipe": chandra_recipe_record(),
        "physical_request_count": len(attempts),
        "returned_attempt_ordinal": len(attempts),
        "exhausted_condition": final["returned_condition"]
        if len(attempts) == CHANDRA_MAX_ATTEMPTS
        else None,
        "attempts": attempts,
    }
    return validate_chandra_trace(trace)


def _with_chandra_trace(
    context, subject_id: str, terminal_records: list[dict[str, Any]], attempt: Attempt
) -> Attempt:
    trace = _chandra_retry_trace(context, subject_id, terminal_records)
    exhausted = trace["exhausted_condition"]
    if exhausted == "repeat-token":
        # There is no partial outcome, so an exhausted repeat is `failed` with its
        # text and capture retained.
        attempt = attempt._replace(
            outcome="failed",
            reason=(
                "the pinned Chandra native recipe exhausted six retries and returned a "
                "response still matching its repeat-token detector; retained as partial"
                # The final answer's own reason (an unmeasured stop word) still holds.
                + (f"; {attempt.reason}" if attempt.reason else "")
            ),
        )
    elif exhausted == "inference-error":
        attempt = attempt._replace(
            outcome="failed",
            reason=(
                attempt.reason
                or "the pinned Chandra native recipe exhausted six retries on inference errors"
            ),
        )
    return attempt._replace(native_inference=trace)


def _resumed_chandra_native_attempt(context, subject_id, intent_records, terminal_records):
    if len(terminal_records) > CHANDRA_MAX_ATTEMPTS:
        raise FatalAccounting("a Chandra native retry chain exceeds seven physical requests")
    if len(intent_records) not in {len(terminal_records), len(terminal_records) + 1}:
        raise FatalAccounting(
            "a Chandra native retry chain has intents that do not match its terminal evidence"
        )
    for expected, record in enumerate(intent_records, 1):
        if record["payload"]["native_attempt_ordinal"] != expected:
            raise FatalAccounting("a Chandra native intent chain has a non-contiguous ordinal")
    if len(intent_records) == len(terminal_records) + 1:
        # Checked before any republish: a resumed service has a new receipt, which
        # would surface as byte drift instead of the real delivery ambiguity.
        refuse_chandra_orphan_intent()

    # A terminal's trigger says whether the loop had another request to make.
    if terminal_records:
        for expected, record in enumerate(terminal_records, 1):
            payload = record["payload"]
            if payload.get("native_attempt_ordinal") != expected:
                raise FatalAccounting("a Chandra native retry chain has a non-contiguous ordinal")
            if expected < len(terminal_records) and payload.get("trigger") is None:
                raise FatalAccounting("a Chandra native retry chain continued after vendor return")
        last_payload = terminal_records[-1]["payload"]
        if last_payload.get("trigger") is None:
            returned_attempt = _attempt_from_evidence_record(
                context, last_payload["resolved_attempt"]
            )
            _raise_chandra_application_refusal(returned_attempt)
            return _with_chandra_trace(context, subject_id, terminal_records, returned_attempt)
    return None


class _ChandraPhysicalResult(NamedTuple):
    attempt: Attempt
    raw: str
    inference_error: bool
    transport_response_ref: Any
    error_code: Any
    error_detail: Any
    application_refusal: ContractError | None


def _read_chandra_native_result(
    context, client, dispatch, intent_ref, page_ordinal, chair, resolved, adapter, framing
) -> _ChandraPhysicalResult:
    response = None
    error: ServingError | None = None
    try:
        response = client.read_chandra_native(dispatch, intent_ref=intent_ref)
    except (ChairResponseRefusal, ChairTransportFailure) as caught:
        error = caught

    application_refusal: ContractError | None = None
    live = None
    if error is not None:
        attempt = _chandra_error_attempt(error, adapter)
        raw = ""
        inference_error = True
        transport_response_ref = getattr(error, "raw_response_ref", None)
        error_code = error.code
        error_detail = getattr(error, "detail", str(error))
    else:
        assert response is not None
        unread = live_witness.unmeasured_stop_reason(
            response, f"the {resolved.witness_adapter} response for page {page_ordinal}"
        )
        if unread is not None:
            # The same health every chair records for an answer kept unread.
            attempt = _chandra_retained_failure(
                context, response, adapter, unread, health=unrecordable_health(unread)
            )
        else:
            try:
                live = live_witness.captured_page_attempt(
                    context,
                    page_ordinal,
                    chair,
                    resolved.witness_adapter,
                    adapter,
                    response,
                    framing=framing,
                )
            except FatalAccounting:
                raise
            except ContractError as caught:
                application_refusal = caught
            attempt = (
                _chandra_application_refusal_attempt(
                    context,
                    response,
                    adapter,
                    application_refusal,
                    live,
                )
                if application_refusal is not None
                else live
            )
        raw = response.content if isinstance(response.content, str) else ""
        inference_error = response.parse_problem is not None
        transport_response_ref = dict(response.raw_response_ref)
        error_code = response.parse_problem
        error_detail = (
            "the retained response could not be parsed as one OpenAI-compatible reading"
            if inference_error
            else None
        )
    return _ChandraPhysicalResult(
        attempt,
        raw,
        inference_error,
        transport_response_ref,
        error_code,
        error_detail,
        application_refusal,
    )


class ChandraPage(NamedTuple):
    """One page's native loop: what was sealed before, and what this pass read."""

    subject_id: str
    resumed: Attempt | None  # the sealed loop already returned; nothing is sent
    terminals: list[dict[str, Any]]  # sealed terminal records, then this pass's
    unsealed: list[_Unsealed]  # this pass's intents and terminals, in loop order
    returned: Attempt | None
    application_refusal: ContractError | None
    error: Exception | None


def begin_page(
    context, *, chair: str, page_subject_id: str, witness_attempt_ordinal: int
) -> ChandraPage:
    """Read what earlier passes sealed for this page. Runs where records are written."""

    subject_id = _chandra_native_subject(page_subject_id, chair, witness_attempt_ordinal)
    intent_records = _sealed_chandra_intents(context, subject_id)
    terminal_records = _sealed_chandra_attempts(context, subject_id)
    resumed = _resumed_chandra_native_attempt(context, subject_id, intent_records, terminal_records)
    return ChandraPage(subject_id, resumed, terminal_records, [], None, None, None)


def read_page(
    context,
    page: ChandraPage,
    *,
    client: ChairClient,
    chair: str,
    resolved: ChairIdentity,
    adapter: Any,
    page_ordinal: int,
    witness_attempt_ordinal: int,
    request: Any,
    framing: str | None,
) -> ChandraPage:
    """Run the pinned vendor loop from where `page` stands; write no record.

    Safe beside other pages' reads. An error is kept, not raised, so the
    records made before it are still sealed in page order.
    """

    if page.resumed is not None:
        return page
    terminals = list(page.terminals)
    unsealed: list[_Unsealed] = []
    try:
        returned, refusal = _run_chandra_loop(
            context,
            subject_id=page.subject_id,
            terminals=terminals,
            unsealed=unsealed,
            client=client,
            chair=chair,
            resolved=resolved,
            adapter=adapter,
            page_ordinal=page_ordinal,
            witness_attempt_ordinal=witness_attempt_ordinal,
            request=request,
            framing=framing,
        )
    except Exception as error:
        return page._replace(terminals=terminals, unsealed=unsealed, error=error)
    return page._replace(
        terminals=terminals,
        unsealed=unsealed,
        returned=returned,
        application_refusal=refusal,
    )


def seal_page(context, page: ChandraPage) -> Attempt:
    """Write the page's records in loop order, then return its traced final attempt."""

    if page.resumed is not None:
        return page.resumed
    for record in page.unsealed:
        if record.fields["kind"] == _INTENT_KIND:
            _publish_chandra_intent(context, record)
        else:
            _publish_chandra_terminal(context, record)
    if page.error is not None:
        raise page.error
    if page.application_refusal is not None:
        raise page.application_refusal
    if page.returned is None:
        raise FatalAccounting("the Chandra native retry loop ended without a returned attempt")
    return _with_chandra_trace(context, page.subject_id, page.terminals, page.returned)


def _run_chandra_loop(
    context,
    *,
    subject_id: str,
    terminals: list[dict[str, Any]],
    unsealed: list[_Unsealed],
    client: ChairClient,
    chair: str,
    resolved: ChairIdentity,
    adapter: Any,
    page_ordinal: int,
    witness_attempt_ordinal: int,
    request: Any,
    framing: str | None,
) -> tuple[Attempt, ContractError | None]:
    next_ordinal = len(terminals) + 1
    if terminals and terminals[-1]["payload"].get("trigger") == "inference-error":
        # A crash during the backoff cannot show how much elapsed, so the full
        # delay is repeated.
        _chandra_backoff(next_ordinal - 1)
    while next_ordinal <= CHANDRA_MAX_ATTEMPTS:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=next_ordinal)
        intent = _chandra_intent_record(
            context,
            subject_id=subject_id,
            page_ordinal=page_ordinal,
            chair=chair,
            witness_attempt_ordinal=witness_attempt_ordinal,
            native_attempt_ordinal=next_ordinal,
            dispatch=dispatch,
            receipt_ref=client.handle.receipt_reference,
        )
        # Kept before the request leaves, so an error from here on seals the
        # intent without a terminal, as delivery-unknown.
        unsealed.append(intent)
        result = _read_chandra_native_result(
            context,
            client,
            dispatch,
            intent.reference,
            page_ordinal,
            chair,
            resolved,
            adapter,
            framing,
        )
        trigger, returned_condition = _chandra_conditions(
            result.raw, result.inference_error, next_ordinal
        )
        terminal = _chandra_terminal_record(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=next_ordinal,
            intent_ref=intent.reference,
            request_sha256=dispatch.request_sha256,
            attempt=result.attempt,
            trigger=trigger,
            returned_condition=returned_condition,
            error=result.inference_error,
            error_code=result.error_code,
            error_detail=result.error_detail,
            transport_response_ref=(
                dict(result.transport_response_ref)
                if result.transport_response_ref is not None
                else None
            ),
        )
        unsealed.append(terminal)
        terminals.append({"payload": terminal.fields["payload"]})
        if trigger is None:
            return result.attempt, result.application_refusal
        if trigger == "inference-error":
            _chandra_backoff(next_ordinal)
        next_ordinal += 1

    raise FatalAccounting("the Chandra native retry loop ended without a returned attempt")
