"""Shared validation for an act-local failed Perlectio.

The producer, resume path, and Recensor all use this module so a failure is a
closed evidence record rather than a routing label.  It deliberately validates
both the failure payload and the retained files it cites.
"""

from __future__ import annotations

import json
from typing import Any, Final, Mapping

from common.contracts.canonical import is_sha256
from common.contracts.envelope import validate_envelope, validate_input_refs
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id, perlector_attempt_id
from common.contracts.serving import (
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_FIELDS_V1,
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_CALL_RECORD_SCHEMA_V1,
    CHAIR_CALL_RECORD_SCHEMAS,
    CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_PROBLEM_FIELDS,
    CHAIR_TRANSPORT_PROBLEM_SCHEMA,
)
from common.contracts.stages import PERLECTOR

PRE_PERLECTIO_ARTIFACTS: Final = (
    ("lectio-prior", "lectio-prior"),
    ("lectio-nuda", "lectio-nuda"),
    ("primed-without-prior", "primed-without-prior"),
    ("audit-draft", "perlegere"),
    ("audit-finding", "perlegere"),
)

_PAYLOAD_FIELDS: Final = frozenset(
    {"act_key", "attempt_ordinal", "reason", "failure", "provenance"}
)
_FAILURE_FIELDS: Final = frozenset(
    {
        "phase",
        "kind",
        "code",
        "detail",
        "raw_response_ref",
        "call_record_ref",
        "request_sha256",
        "receipt_ref",
        "served_model_id",
        "response_completion",
    }
)
_KINDS: Final = frozenset(
    {"engine-signal", "chair-response", "transport", "reproof-response", "request-capacity"}
)
_PHASES: Final = frozenset({"establishing", "audit-reproof"})
_CALL_RECORD_SCHEMAS: Final = CHAIR_CALL_RECORD_SCHEMAS | {CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA}


def _closed(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    missing = sorted(fields - set(value))
    unexpected = sorted(set(value) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"{label} is not its closed schema: missing {missing}, unexpected {unexpected}"
        )


def validate_failed_payload(payload: Any) -> dict[str, Any]:
    """Validate the context-free half of one failed-Perlectio contract."""
    if not isinstance(payload, dict):
        raise SchemaRefusal("a failed Perlectio payload is not an object")
    _closed(payload, _PAYLOAD_FIELDS, "a failed Perlectio payload")
    if not isinstance(payload["act_key"], str) or not payload["act_key"]:
        raise SchemaRefusal("a failed Perlectio has no act key")
    ordinal = payload["attempt_ordinal"]
    if type(ordinal) is not int or ordinal < 1:
        raise SchemaRefusal("a failed Perlectio has no positive integer attempt ordinal")
    if not isinstance(payload["reason"], str) or not payload["reason"]:
        raise SchemaRefusal("a failed Perlectio has no reason")

    failure = payload["failure"]
    if not isinstance(failure, dict):
        raise SchemaRefusal("a failed Perlectio has no failure object")
    _closed(failure, _FAILURE_FIELDS, "a failed Perlectio failure record")
    if not isinstance(failure["phase"], str) or failure["phase"] not in _PHASES:
        raise SchemaRefusal("a failed Perlectio names an unknown failure phase")
    if not isinstance(failure["kind"], str) or failure["kind"] not in _KINDS:
        raise SchemaRefusal("a failed Perlectio names an unknown failure kind")
    if failure["kind"] == "reproof-response" and failure["phase"] != "audit-reproof":
        raise SchemaRefusal("a malformed re-proof response is not bound to the audit phase")
    if (
        not isinstance(failure["code"], str)
        or not failure["code"]
        or not isinstance(failure["detail"], str)
    ):
        raise SchemaRefusal("a failed Perlectio has no named failure code and detail")

    evidence = (
        failure["raw_response_ref"],
        failure["call_record_ref"],
        failure["request_sha256"],
        failure["receipt_ref"],
        failure["served_model_id"],
    )
    present = tuple(item is not None for item in evidence)
    completion = failure["response_completion"]
    if completion is not None and (
        not isinstance(completion, str) or completion not in {"complete", "unknown"}
    ):
        raise SchemaRefusal("a failed Perlectio names an unknown response-completion state")
    if failure["kind"] in {"engine-signal", "chair-response"} and (
        completion != "complete" or not all(present)
    ):
        raise SchemaRefusal(
            "a completed engine or chair response failure has no retained response evidence"
        )
    if failure["kind"] == "request-capacity" and (completion is not None or any(present)):
        raise SchemaRefusal("a request refused before it was sent carries no response evidence")
    if failure["kind"] == "transport":
        retained_transport = failure["raw_response_ref"] is None and all(
            item is not None for item in evidence[1:]
        )
        if completion != "unknown" or (any(present) and not retained_transport):
            raise SchemaRefusal(
                "a transport failure does not carry either no request evidence or its closed "
                "unknown-completion call evidence"
            )
    if failure["kind"] == "reproof-response":
        if completion is None and any(present):
            raise SchemaRefusal("a fixture re-proof failure claims live response evidence")
        if completion == "complete" and not all(present):
            raise SchemaRefusal("a live re-proof failure has incomplete response evidence")
        if completion == "unknown":
            raise SchemaRefusal("a malformed re-proof response cannot have unknown completion")
    references = [
        reference
        for reference in (
            failure["raw_response_ref"],
            failure["call_record_ref"],
            failure["receipt_ref"],
        )
        if reference is not None
    ]
    if references:
        validate_input_refs(references)
    if any(present):
        if not is_sha256(failure["request_sha256"]):
            raise SchemaRefusal("a failed Perlectio response has no request digest")
        if not isinstance(failure["served_model_id"], str) or not failure["served_model_id"]:
            raise SchemaRefusal("a failed Perlectio response has no served model identity")
    return payload


def _require_input(reading: Mapping[str, Any], reference: Mapping[str, str], label: str) -> None:
    if dict(reference) not in reading["inputs"]:
        raise SchemaRefusal(f"a failed Perlectio does not account for its {label} as an input")


def _verify_ref(context: Any, reference: Mapping[str, str], label: str) -> None:
    observed = context.input_ref(reference["relative_path"])
    if observed != dict(reference):
        raise SchemaRefusal(f"a failed Perlectio {label} reference does not match retained bytes")


def validate_failed_perlectio(
    context: Any, reading: Any, act_id: str, *, expected_act_key: str | None = None
) -> dict[str, Any]:
    """Validate identity, lineage, provenance, and response evidence end to end."""
    envelope = validate_envelope(reading)
    if (
        envelope["stage"] != PERLECTOR
        or envelope["kind"] != "perlectio"
        or envelope["outcome"] != "failed"
        or envelope["subject_id"] != act_id
    ):
        raise SchemaRefusal("a failed Perlectio is not bound to the expected act and kind")
    payload = validate_failed_payload(envelope["payload"])
    if expected_act_key is not None and payload["act_key"] != expected_act_key:
        raise SchemaRefusal("a failed Perlectio disagrees with its expected act key")
    expected_attempt = perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"])
    if envelope["attempt_id"] != expected_attempt:
        raise SchemaRefusal("a failed Perlectio is not bound to its claimed attempt ordinal")

    # Imported here to keep this small contract module outside stage startup's
    # import graph while still sharing the exact provenance validator.
    from common.stage import validate_serving_provenance

    identity = validate_serving_provenance(
        context, payload["provenance"], producer_stage=PERLECTOR, require_receipt=True
    )
    serving_receipt = context.tree.read_run_receipt(payload["provenance"]["receipt_ref"])
    fixture_serving = (
        serving_receipt.get("endpoint") == "fixture://offline-chair-runner"
        and serving_receipt.get("engine_version") == "fixture-v0"
        and serving_receipt.get("dtype") == "fixture"
    )
    validate_input_refs(envelope["inputs"])
    for reference in envelope["inputs"]:
        _verify_ref(context, reference, "direct input")
    failure = payload["failure"]
    if (
        failure["kind"] == "reproof-response"
        and failure["response_completion"] is None
        and not fixture_serving
    ):
        raise SchemaRefusal(
            "a live re-proof failure omits the completed response evidence its receipt requires"
        )
    if failure["receipt_ref"] is not None and failure["receipt_ref"] != payload["provenance"].get(
        "receipt_ref"
    ):
        raise SchemaRefusal(
            "a failed Perlectio response receipt differs from its serving provenance"
        )
    for name in ("raw_response_ref", "call_record_ref", "receipt_ref"):
        reference = failure[name]
        if reference is not None:
            _require_input(envelope, reference, name)
            _verify_ref(context, reference, name)

    if failure["call_record_ref"] is not None:
        try:
            call = json.loads(context.tree.read_bytes(failure["call_record_ref"]["relative_path"]))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SchemaRefusal("a failed Perlectio call record is not JSON") from error
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("schema"), str)
            or call["schema"] not in _CALL_RECORD_SCHEMAS
        ):
            raise SchemaRefusal("a failed Perlectio names an unsupported chair call record")
        expected_fields = {
            CHAIR_CALL_RECORD_SCHEMA_V1: CHAIR_CALL_RECORD_FIELDS_V1,
            CHAIR_CALL_RECORD_SCHEMA: CHAIR_CALL_RECORD_FIELDS,
            CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA: CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
        }[call["schema"]]
        if set(call) != expected_fields:
            raise SchemaRefusal("a failed Perlectio chair call record is not its closed schema")
        if call["kind"] != "chat-completions":
            raise SchemaRefusal("a failed Perlectio chair call names the wrong wire kind")
        if (
            not isinstance(call["image_sha256s"], list)
            or any(not is_sha256(digest) for digest in call["image_sha256s"])
            or not isinstance(call["generation_sent"], dict)
            or not isinstance(call["generation_declared"], dict)
            or (call["capacity"] is not None and not isinstance(call["capacity"], dict))
        ):
            raise SchemaRefusal("a failed Perlectio chair call has malformed request facts")
        validate_input_refs([call["launch_audit_ref"]])
        _verify_ref(context, call["launch_audit_ref"], "serving launch audit")
        if (
            failure["kind"] == "transport"
            and call["schema"] != CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA
        ):
            raise SchemaRefusal(
                "a retained transport failure does not name its transport failure record"
            )
        if call["schema"] == CHAIR_CALL_RECORD_SCHEMA and (
            type(call["response_status"]) is not int or not 100 <= call["response_status"] <= 599
        ):
            raise SchemaRefusal("a failed Perlectio chair call has no HTTP response status")
        if call["schema"] == CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA:
            if failure["kind"] != "transport":
                raise SchemaRefusal(
                    "a non-transport failed Perlectio names a transport failure record"
                )
            if any(
                call[field] is not None
                for field in (
                    "raw_response_ref",
                    "response_sha256",
                    "response_status",
                    "response_model",
                    "finish_reason",
                    "usage",
                    "parse_problem",
                )
            ):
                raise SchemaRefusal("a transport failure record invents a completed response fact")
            problem = call["transport_problem"]
            if not isinstance(problem, dict) or set(problem) != CHAIR_TRANSPORT_PROBLEM_FIELDS:
                raise SchemaRefusal("a failed Perlectio transport problem is not its closed schema")
            if (
                problem["schema"] != CHAIR_TRANSPORT_PROBLEM_SCHEMA
                or problem["code"] != "ENDPOINT_UNAVAILABLE"
                or not isinstance(problem["detail"], str)
                or type(problem["definitively_absent"]) is not bool
                or problem["request_delivery"] != "unknown"
                or problem["response_completion"] != "unknown"
                or problem["detail"] != failure["detail"]
            ):
                raise SchemaRefusal(
                    "a failed Perlectio transport problem disagrees with its typed failure"
                )
        expected = {
            "raw_response_ref": failure["raw_response_ref"],
            "request_sha256": failure["request_sha256"],
            "receipt_ref": failure["receipt_ref"],
            "served_model_id": failure["served_model_id"],
        }
        for field, value in expected.items():
            if call.get(field) != value:
                raise SchemaRefusal(
                    f"a failed Perlectio {field} disagrees with its retained chair call record"
                )
        if identity is None or any(
            (
                call.get("chair") != identity.role,
                call.get("resolved_identity") != identity.to_record(),
                call.get("resolved_revision") != identity.receipt_revision,
                call.get("serving_recipe") != identity.serving_recipe,
            )
        ):
            raise SchemaRefusal(
                "a failed Perlectio chair call identity disagrees with its serving provenance"
            )
        if not is_sha256(call.get("decoding_config_sha256")):
            raise SchemaRefusal("a failed Perlectio chair call has no decoding-policy digest")
        context.require_sealed_config("decoding", call["decoding_config_sha256"])
        if (
            failure["raw_response_ref"] is not None
            and call.get("response_sha256") != failure["raw_response_ref"]["sha256"]
        ):
            raise SchemaRefusal(
                "a failed Perlectio chair call response digest disagrees with its raw response"
            )
        if failure["kind"] == "chair-response" and call.get("parse_problem") != failure["code"]:
            raise SchemaRefusal(
                "a failed Perlectio chair refusal code disagrees with its call record"
            )
        if failure["kind"] == "engine-signal":
            parse_problem = call.get("parse_problem")
            finish_reason = call.get("finish_reason")
            parse_failure = (
                isinstance(parse_problem, str)
                and parse_problem
                and failure["code"] == parse_problem
            )
            finish_failure = (
                parse_problem is None
                and isinstance(finish_reason, str)
                and finish_reason not in {"stop", "length"}
                and failure["code"] == "ENGINE_FINISH_REASON_UNRECOGNIZED"
            )
            if not (parse_failure or finish_failure):
                raise SchemaRefusal(
                    "a failed Perlectio engine-signal record does not exhibit its parse or "
                    "finish problem"
                )
        if failure["kind"] == "reproof-response" and (
            call.get("parse_problem") is not None
            or call.get("finish_reason") not in {"stop", "length", None}
            or call.get("response_model") != failure["served_model_id"]
            or (call["schema"] == CHAIR_CALL_RECORD_SCHEMA and call["response_status"] != 200)
        ):
            raise SchemaRefusal(
                "an exact-edit re-proof failure does not name a successful parse-clean chair call"
            )

    existing: set[str] = set()
    for kind, operation in PRE_PERLECTIO_ARTIFACTS:
        identifier = artifact_id(
            PERLECTOR,
            kind,
            act_id,
            perlector_attempt_id(act_id, operation, payload["attempt_ordinal"]),
        )
        if context.tree.has_artifact(PERLECTOR, kind, identifier):
            reference = context.artifact_ref(PERLECTOR, kind, identifier)
            _require_input(envelope, reference, f"completed {kind}")
            existing.add(kind)
    if failure["phase"] == "audit-reproof" and "audit-draft" not in existing:
        raise SchemaRefusal("an audit-reproof failure has no retained audit draft")
    if failure["phase"] == "audit-reproof" and "audit-finding" in existing:
        raise SchemaRefusal("an audit-reproof failure cannot follow a completed audit finding")
    if failure["phase"] == "establishing" and existing & {"audit-draft", "audit-finding"}:
        raise SchemaRefusal("an establishing failure claims an audit-stage artifact")
    return payload
