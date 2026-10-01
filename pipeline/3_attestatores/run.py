"""Retain every witness attempt without changing its history.

Attempts are append-only. A whole pass reads every chair, each on its whole page,
at one ordinal. A witness's self-reported confidence is retained but never used
for channel health, which comes from the response and transport.
"""

import json
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Final, Mapping, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chandra  # noqa: E402
import feeding  # noqa: E402
import live_witness  # noqa: E402
import witness_adapters  # noqa: E402

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.chandra_native_retry import (  # noqa: E402
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
from common.contracts.canonical import digest_bytes, is_sha256  # noqa: E402
from common.contracts.envelope import read_verified  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id, region_id  # noqa: E402
from common.contracts.serving import (  # noqa: E402
    CHANDRA_NATIVE_CALL_RECORD_FIELDS,
    CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
    RAW_RESPONSE_MODEL_OUTPUT,
    RAW_RESPONSE_TRANSPORT_BODY,
    STOP_REASON_UNREPORTED,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, PERLECTOR  # noqa: E402
from common.decoding import (  # noqa: E402
    SAMPLING_FIELDS,
    load_decoding_policy,
    refuse_retired_call_record,
    verify_call_sampling,
)
from common.exemplar_boundary import (  # noqa: E402
    read_sealed_page,
    sealed_page_bytes,
)
from common.imaging import crop_png, dimensions  # noqa: E402
from common.native_witness import (  # noqa: E402
    REPORTED_BOUNDS_SOURCES,
    native_parse_refusal,
    record_presentations,
    split_page_edge_overshoots,
    validate_capture_text_view,
    validate_native_capture,
    validate_native_witness_geometry,
    validate_presented_page_binding,
)
from common.native_witness import (
    validate_page_testimonium_payload as validate_shared_page_testimonium_payload,
)
from common.page_path import (  # noqa: E402
    PAGE_FEED_KIND,
    PAGE_TESTIMONIUM_KIND,
    empty_detector_page,
)
from common.page_testimonia import (  # noqa: E402
    BLANK_TESTIMONY_HEALTH,
    NO_DETECTOR_RECORD_REASON,
    declared_page_witness_chairs,
    is_detector_blank_testimony,
    validate_page_testimonium_record,
    verify_page_native_capture,
)
from common.page_witness_units import reads_detector_records  # noqa: E402
from common.request_capacity import RequestCapacityRefusal  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    EXIT_HELD,
    WITNESS_READING_OUTCOMES,
    exemplar_page_ids,
    fixture_serving_details,
    is_real_ingress,
    latest_attempt,
    open_stage_context,
    run_stage,
    sealed_decoding_policy,
    stage_manifest,
    stage_parser,
    validate_serving_provenance,
    verify_retained_call_sampling,
)
from operations.serving.assembly import (  # noqa: E402
    bound_serving_recipes,
    retain_chair_bytes,
    stage_chair_client,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.config import ServingRecipes  # noqa: E402
from operations.serving.errors import (  # noqa: E402
    ChairResponseRefusal,
    ChairTransportFailure,
    ServingError,
)

DESCRIPTION = "Attestatores: retain every witness attempt without changing its history."

# Self-assessments a witness may report. They are retained as testimony, never used
# to rank or choose a witness. `uncertain` and `unsure` are both admitted because
# real adapters emit both spellings.
WITNESS_CONFIDENCE_ORDINALS = frozenset({"certain", "high", "medium", "low", "uncertain", "unsure"})

DEFAULT_FORMAT_CAPABILITIES = {
    "can_express_uncertainty": False,
    "can_express_layout": False,
}


def _declared_format_capabilities(adapter: Any) -> dict[str, Any]:
    """Record adapter expressiveness even when a request is refused pre-send."""
    return witness_adapters.declared_format_capabilities(adapter)


def real_ingress(context) -> bool:
    """Read real ingress from self-hashed run authority, never fixture scenario."""
    return is_real_ingress(context.run)


def page_subject(context, page_ordinal: int, *, page_ids: dict[int, str] | None = None) -> str:
    """Name the Exemplar page; a supplied page map avoids repeated inventory walks."""
    pages = page_ids if page_ids is not None else exemplar_page_ids(context)
    if page_ordinal not in pages:
        raise FatalAccounting(
            f"page ordinal {page_ordinal} names no Exemplar page in this run (accounted "
            f"for, sealed or refused: {sorted(pages)}); no witness can be shown, and no "
            "record published for, a page the Exemplar never accounted for"
        )
    return pages[page_ordinal]


def _confidence_problem(value: Any, path: str = "witness_reported") -> str | None:
    """Validate every confidence claim in retained witness self-report JSON."""
    if isinstance(value, dict):
        for key in sorted(value):
            item = value[key]
            if key == "confidence" and (
                not isinstance(item, str) or item not in WITNESS_CONFIDENCE_ORDINALS
            ):
                return (
                    f"{path}.confidence is not a member of the closed ordinal set "
                    f"{sorted(WITNESS_CONFIDENCE_ORDINALS)}"
                )
            if problem := _confidence_problem(item, f"{path}.{key}"):
                return problem
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if problem := _confidence_problem(item, f"{path}[{index}]"):
                return problem
    return None


# A witness response is untrusted: deep nesting would raise an uncaught
# `RecursionError` in `_native_problem` and kill the whole run, not one attempt.
# Real output nests a few levels, so this is headroom.
_MAX_NATIVE_DEPTH = 64


REGION_PRESENTATION_FIELDS: Final = ("region_id", "image_path", "image_sha256")
REGION_TRANSFORM_FIELDS: Final = ("source_page_id", "source_page_ordinal")


def presentation_for_region(region: dict[str, Any]) -> dict[str, Any]:
    """The sealed Designator crop a record reader is shown, or a refusal naming what it lacks."""
    payload = region.get("payload")
    transform = payload.get("transform") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(transform, dict):
        raise SchemaRefusal(
            "a sealed Designator region has no payload and page-space transform to present"
        )
    missing = [field for field in REGION_PRESENTATION_FIELDS if field not in payload]
    missing += [field for field in REGION_TRANSFORM_FIELDS if field not in transform]
    if missing:
        raise SchemaRefusal(
            f"a sealed Designator region lacks the field(s) {sorted(missing)} its presentation "
            "must name. The record cannot be traced to the exact pixels a witness was shown"
        )
    return {
        "kind": "region",
        "source_page_id": transform["source_page_id"],
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": payload["image_path"],
        "image_sha256": payload["image_sha256"],
        "transform": transform,
        "region_ref": {"region_id": payload["region_id"]},
    }


def presentation_for_page(
    context, page_ordinal: int, *, page_ids: dict[int, str] | None = None
) -> dict[str, Any]:
    """Bind a page witness to the sealed whole-page pixels it was shown."""
    page_id = page_subject(context, page_ordinal, page_ids=page_ids)
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    if page.get("outcome") != "sealed":
        raise FatalAccounting(
            f"page ordinal {page_ordinal} was refused at the Door and carries no sealed "
            "pixels; no witness can be shown a page that was never admitted"
        )
    image_path = page["payload"]["image_path"]
    page_bytes = sealed_page_bytes(context.tree, page)
    width, height = dimensions(page_bytes)
    return {
        "kind": "page",
        "source_page_id": page_id,
        "source_page_ordinal": page_ordinal,
        "image_path": image_path,
        "image_sha256": page["payload"]["source_sha256"],
        "transform": {
            "operation": "whole",
            "source_page_id": page_id,
            "source_page_ordinal": page_ordinal,
            "bounds": {"x": 0, "y": 0, "w": width, "h": height},
        },
    }


def observed_from_presentation(presented: dict[str, Any]) -> list[dict[str, Any]]:
    """The fixture's default is one observation of exactly its presentation."""
    return [
        {
            "ordinal": 0,
            "bounds": dict(presented["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]


def _fixture_native_observations(
    context, *, chair: str, page_ordinal: int
) -> list[dict[str, Any]] | None:
    """Fixture geometry is a stimulus; its routing threshold remains unmeasured."""
    rows = [
        row
        for row in context.fixture.get("native_observation", [])
        if row.get("chair") == chair
        and row.get("page_ordinal") == page_ordinal
        and row.get("scenario") in (None, context.scenario)
    ]
    if not rows:
        return None
    observations = []
    for ordinal, row in enumerate(rows):
        # Refused here, where the error can name the chair, page and row.
        missing = sorted(key for key in ("x", "y", "w", "h") if key not in row)
        if missing:
            raise SchemaRefusal(
                f"fixture native_observation row {ordinal} for chair {chair!r} on page "
                f"{page_ordinal} lacks the coordinate(s) {missing}; a declared witness "
                "observation must be a complete page-pixel box"
            )
        observations.append(
            {
                "ordinal": ordinal,
                "bounds": {key: row[key] for key in ("x", "y", "w", "h")},
                "bounds_source": "native",
                "span": None,
            }
        )
    return observations


#: Adapters whose fixture rows may declare `raw_response` bytes;
#: `_fixture_raw_response_attempt` refuses any other adapter, since fixture bytes
#: may not be attributed to a model that never produced them.
FIXTURE_NATIVE_RESPONSE_ADAPTERS: Final = frozenset({"chandra.v1"})


def _derives_partition_from_response(resolved: Any, live: bool) -> bool:
    """Use response geometry live, or for fixture adapters that permit it."""
    if not isinstance(resolved, ChairIdentity):
        return False
    # Unguarded on purpose: an adapter with no runnable binding must be refused,
    # not routed down the no-geometry branch.
    adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
    if not adapter.takes_page_size:
        return False
    return live or resolved.witness_adapter in FIXTURE_NATIVE_RESPONSE_ADAPTERS


def _partition_geometry(observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse a mix of reported boxes and presentation echoes.

    Filtering a mix would make half a report look complete; an echo is shown
    pixels, not witness geometry.
    """
    reported = [item for item in observed if item["bounds_source"] in REPORTED_BOUNDS_SOURCES]
    if reported and len(reported) != len(observed):
        raise SchemaRefusal(
            "a page witness reported geometry and a presentation echo in one response. "
            "A partition derived from half of that record would look complete. "
            "Return reported geometry or the no-geometry echo, not both."
        )
    # Ordinals stay dense and 0-based, as the page-edge check requires.
    return reported


def page_partition_entries(
    observed: list[dict[str, Any]],
    *,
    page_size: tuple[int, int],
    raw_response_ref: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split boxes against the sealed page and retain auditable edge findings.

    A box may cross the shown crop; a finding must name its raw response.
    """
    survivors, overshoots = split_page_edge_overshoots(observed, page_size=page_size)
    if overshoots and (
        not isinstance(raw_response_ref, dict) or not is_sha256(raw_response_ref.get("sha256"))
    ):
        raise SchemaRefusal(
            "a page-edge finding has no retained response reference. "
            "The rejected block cannot be traced to the response that produced it. "
            "Retain the raw page-witness response before deriving the page partition."
        )
    return survivors, [
        {**finding, "response_sha256": raw_response_ref["sha256"]} for finding in overshoots
    ]


def _sealed_source_page(
    context, presented: dict[str, Any]
) -> tuple[dict[str, Any], bytes, tuple[int, int]]:
    """The sealed Exemplar page, exact verified bytes used, and decoded size."""
    page_id = presented["source_page_id"]
    page, page_bytes = read_sealed_page(context.tree, page_id)
    return page, page_bytes, dimensions(page_bytes)


def validate_testimonium_presentation(context, record: dict[str, Any]) -> None:
    """Re-derive the presentation's sealed page and its blob binding.

    An unpresented record binds no input, except a record reader's blank
    testimony, whose one input is its detector's census
    (`common.page_testimonia.validate_page_testimonium_record` checks it).
    """
    payload = record["payload"]
    presented = payload["presented"]
    validate_native_witness_geometry(payload)
    if presented == {}:
        if record.get("inputs") != [] and not is_detector_blank_testimony(context, record):
            raise SchemaRefusal("an unpresented Testimonium carries image inputs")
        return
    page, page_bytes, page_size = _sealed_source_page(context, presented)
    validate_native_witness_geometry(payload, page_size=page_size)
    for shown in record_presentations(payload):
        validate_presented_page_binding(
            shown,
            page_ordinal=page["payload"]["ordinal"],
            page_image_path=page["payload"]["image_path"],
            page_sha256=page["payload"]["source_sha256"],
            page_size=page_size,
            page_bytes=page_bytes,
        )
        if not any(
            item == {"relative_path": shown["image_path"], "sha256": shown["image_sha256"]}
            for item in record.get("inputs", [])
        ):
            raise SchemaRefusal(
                "a Testimonium presented image is not digest-bound in record.inputs"
            )


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _sorted_refs(references: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(references, key=lambda ref: (ref.get("relative_path", ""), ref.get("sha256", "")))


def _declared_for_ordinal(row: dict[str, Any], ordinal: int) -> bool:
    """An unnumbered fixture row belongs to attempt one, never to a later attempt."""
    declared = row.get("attempt_ordinal", 1)
    if not _is_positive_int(declared):
        raise SchemaRefusal("a fixture witness declaration has no positive attempt ordinal")
    return declared == ordinal


def _scenario_rows(context, rows) -> list[dict[str, Any]]:
    """The rows declared for this scenario, or else the scenario-agnostic ones."""
    base: list[dict[str, Any]] = []
    scoped: list[dict[str, Any]] = []
    for row in rows:
        declared_scenario = row.get("scenario")
        if declared_scenario is None:
            base.append(row)
        elif declared_scenario == context.scenario:
            scoped.append(row)
    return scoped or base


def _native_problem(value: Any, path: str = "payload", *, depth: int = 0) -> str | None:
    """Return why a native response cannot be retained as canonical JSON.

    Checked here so a bad response becomes a retained ``failed`` attempt rather than
    a crash in the artifact writer or a silent repair.
    """
    if depth > _MAX_NATIVE_DEPTH:
        return f"{path} nests deeper than {_MAX_NATIVE_DEPTH} levels"
    if value is None or isinstance(value, (bool, int)):
        return None
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            return f"{path} contains text that is not valid UTF-8"
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            if problem := _native_problem(item, f"{path}[{index}]", depth=depth + 1):
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} has a non-string object key"
            try:
                key.encode("utf-8", "strict")
            except UnicodeEncodeError:
                return f"{path} has an object key that is not valid UTF-8"
            if problem := _native_problem(item, f"{path}.{key}", depth=depth + 1):
                return problem
        return None
    return f"{path} has unsupported native type {type(value).__name__!r}"


def _native_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# Shared by the writer and `validate_content_health` so both agree on "no response".
NO_RESPONSE_HEALTH = {
    "native_type": None,
    "encoding": "not-applicable",
    "recordable": None,
    "empty": None,
    "blank": None,
    "truncated": None,
    "characters": None,
}


def no_response_health(*, reason: str) -> dict[str, Any]:
    """Health for a chair with no native response, never an empty reading."""
    return {**NO_RESPONSE_HEALTH, "truncation_basis": reason}


def _unrecordable_health(basis: str, *, native_type: str = "unrecordable") -> dict[str, Any]:
    return {
        "native_type": native_type,
        "encoding": "invalid-or-unrecordable",
        "recordable": False,
        "empty": None,
        "blank": None,
        "truncated": None,
        "characters": None,
        "truncation_basis": basis,
    }


def content_health(native_payload: Any, *, completed: bool | None = None) -> dict[str, Any]:
    """Compute deterministic channel facts from native output alone.

    ``witness_reported`` is deliberately not an input: a self-report never becomes
    health. ``completed`` must come from a trusted response boundary, or be None.
    """
    if (problem := _native_problem(native_payload)) is not None:
        return _unrecordable_health(problem, native_type=_native_type(native_payload))

    if isinstance(native_payload, str):
        empty = native_payload == ""
        blank = native_payload.strip() == ""
        characters: int | None = len(native_payload)
    elif isinstance(native_payload, (dict, list)):
        empty = len(native_payload) == 0
        blank = None
        characters = None
    else:
        empty = False
        blank = None
        characters = None
    return {
        "native_type": _native_type(native_payload),
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": empty,
        "blank": blank,
        "truncated": None if completed is None else not completed,
        "characters": characters,
        "truncation_basis": (
            "trusted-response-boundary" if completed is not None else "not-recorded"
        ),
    }


def validate_content_health(native_payload: Any, health: Any) -> None:
    """Refuse a resealed health record that is not this stage's deterministic shape.

    A valid self-hash does not prove the field was computed here. Whether an
    unrecordable channel is an accounted failure is decided by
    `require_accounted_unrecordable_channel`, not here.
    """
    if not isinstance(health, dict):
        raise SchemaRefusal("a Testimonium carries no object content_health record")
    required = set(NO_RESPONSE_HEALTH) | {"truncation_basis"}
    if missing := sorted(required - set(health)):
        raise SchemaRefusal(f"a Testimonium content_health record lacks field(s) {missing}")
    if unexpected := sorted(set(health) - required):
        raise SchemaRefusal(
            f"a Testimonium content_health record carries unknown field(s) {unexpected}; "
            "health is computed here and its schema is closed, so a field nothing "
            "validates is a self-report wearing a computed field's name"
        )

    recordable = health["recordable"]
    if recordable is True:
        if problem := _native_problem(native_payload):
            raise SchemaRefusal(problem)
        expected = content_health(native_payload, completed=None)
        for field in ("native_type", "encoding", "recordable", "empty", "blank", "characters"):
            if health[field] != expected[field]:
                raise SchemaRefusal(f"a Testimonium has inconsistent content_health.{field}")
        truncated = health["truncated"]
        basis = health["truncation_basis"]
        if truncated is None:
            if basis != "not-recorded":
                raise SchemaRefusal(
                    "a Testimonium with unknown truncation lacks the not-recorded basis"
                )
        elif isinstance(truncated, bool):
            if basis != "trusted-response-boundary":
                raise SchemaRefusal(
                    "a Testimonium with a known truncation state lacks a trusted boundary"
                )
        else:
            raise SchemaRefusal("a Testimonium content_health.truncated is not boolean or null")
        return

    if recordable is None:
        if native_payload is not None:
            raise SchemaRefusal("a no-response Testimonium retains a native payload")
        for field, value in NO_RESPONSE_HEALTH.items():
            if health[field] != value:
                raise SchemaRefusal(
                    f"a no-response Testimonium has inconsistent content_health.{field}"
                )
        if (
            not isinstance(health["truncation_basis"], str)
            or not health["truncation_basis"].strip()
        ):
            raise SchemaRefusal("a no-response Testimonium has no health reason")
        return

    if recordable is False:
        # Nothing was kept, so every measured field is fixed; otherwise a resealed
        # record could assert measurements nothing made.
        if native_payload is not None:
            raise SchemaRefusal(
                "a Testimonium whose native channel was unrecordable retains a native "
                "payload; either nothing could be kept or something could"
            )
        if health["encoding"] != "invalid-or-unrecordable":
            raise SchemaRefusal("an unrecordable Testimonium channel claims a valid encoding")
        for field in ("empty", "blank", "truncated", "characters"):
            if health[field] is not None:
                raise SchemaRefusal(
                    f"an unrecordable Testimonium channel asserts content_health.{field}, "
                    "which nothing was able to measure"
                )
        for field in ("native_type", "truncation_basis"):
            if not isinstance(health[field], str) or not health[field].strip():
                raise SchemaRefusal(f"an unrecordable Testimonium channel has no {field}")
        return
    raise SchemaRefusal("a Testimonium content_health.recordable is not boolean or null")


def format_capabilities_for(row: dict[str, Any]) -> dict[str, Any]:
    """The output format's declared expressiveness, not a confidence score."""
    capabilities = row.get("format_capabilities", DEFAULT_FORMAT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        raise SchemaRefusal("a witness format_capabilities declaration is not an object")
    for field in ("can_express_uncertainty", "can_express_layout"):
        if not isinstance(capabilities.get(field), bool):
            raise SchemaRefusal(f"witness format_capabilities.{field} is not a boolean")
    if problem := _native_problem(capabilities, "format_capabilities"):
        raise SchemaRefusal(problem)
    return capabilities


def prepared_response(
    row: dict[str, Any],
) -> tuple[Any, Any, dict[str, Any] | None, dict[str, Any], str | None]:
    """Return native output and any recording defect without normalizing either.

    The final element, when set, is the reason this attempt is ``failed``.
    """
    if "payload" not in row:
        health = no_response_health(reason="fixture response declared no native payload")
        return (
            None,
            None,
            DEFAULT_FORMAT_CAPABILITIES,
            health,
            "the witness response had no native payload",
        )
    native_payload = row["payload"]
    health = content_health(native_payload, completed=True)
    if health["recordable"] is not True:
        return None, None, None, health, str(health["truncation_basis"])
    witness_reported = row.get("witness_reported")
    report_problem = _native_problem(witness_reported, "witness_reported")
    if report_problem is None:
        report_problem = _confidence_problem(witness_reported)
    if report_problem is not None:
        witness_reported = None
    try:
        capabilities = format_capabilities_for(row)
    except SchemaRefusal as error:
        reason = f"the witness format capabilities could not be retained: {error}"
        if report_problem is not None:
            reason = f"{reason}; the witness self-report could not be retained: {report_problem}"
        return native_payload, witness_reported, None, health, reason
    if report_problem is not None:
        return (
            native_payload,
            None,
            capabilities,
            health,
            f"the witness self-report could not be retained: {report_problem}",
        )
    return native_payload, witness_reported, capabilities, health, None


def provenance_for(
    context,
    resolved: ChairIdentity,
    *,
    attempted: bool,
    receipt_ref: dict[str, str] | None = None,
) -> dict:
    """The exact configured identity and actual serving moment for one outcome.

    A live chair already published its receipt when serving started, so the live
    pass passes ``receipt_ref``. Without it, an attempted fixture chair gets a
    receipt that says `fixture://`.
    """
    if receipt_ref is not None and not attempted:
        raise ContractError(
            "a witness attempt that was never made carries a serving receipt reference; "
            "a receipt names a serving moment, and there was none"
        )
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("only a configured chair is asked for a page")
    if receipt_ref is None and attempted:
        receipt_ref = context.write_serving_receipt(resolved, fixture_serving_details(resolved))
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": receipt_ref,
        "adapter_revision": context.adapter_revision,
    }


def _set_present(record: dict[str, Any], **optional: Any) -> None:
    """Write each optional field that has a value; an absent field is omitted, not null."""
    record.update({field: value for field, value in optional.items() if value is not None})


def declared_adapter_metadata(
    resolved: ChairIdentity, *, has_raw_response: bool
) -> dict[str, str] | None:
    """Declare only this occupant's conversion rule and only beside raw bytes."""
    if not has_raw_response:
        return None
    rule = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter).quantization
    return None if rule is None else {"geometry_quantization": rule}


def validate_stage_blob_ref(reference: Any, field: str) -> dict[str, str]:
    """Close one content-addressed reference to this stage's own blob store."""
    prefix = "3_attestatores/blobs/sha256/"
    if (
        not isinstance(reference, dict)
        or set(reference) != {"relative_path", "sha256"}
        or not isinstance(reference["relative_path"], str)
        or not is_sha256(reference["sha256"])
        or reference["relative_path"] != prefix + reference["sha256"]
    ):
        raise SchemaRefusal(f"a Testimonium {field} is not an Attestatores blob reference")
    return reference


def validate_raw_response_ref(reference: Any) -> dict[str, str]:
    """Close one retained-response reference to this stage's own blob store."""
    return validate_stage_blob_ref(reference, "raw_response_ref")


def _provenance_adapter_name(payload: dict[str, Any]) -> Any:
    provenance = payload.get("provenance")
    identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
    return identity.get("witness_adapter") if isinstance(identity, dict) else None


def validate_adapter_metadata(payload: Any) -> None:
    """Require a bound rule and reconcile it with the record's own adapter."""
    if not isinstance(payload, dict) or "adapter_metadata" not in payload:
        return
    metadata = payload["adapter_metadata"]
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"geometry_quantization"}
        or metadata["geometry_quantization"] not in witness_adapters.declared_quantization_rules()
    ):
        raise SchemaRefusal(
            "a Testimonium adapter metadata is not a quantization rule any bound adapter declares"
        )
    adapter_name = _provenance_adapter_name(payload)
    if isinstance(adapter_name, str):
        expected = witness_adapters.resolve_runnable_adapter(adapter_name).quantization
        if metadata["geometry_quantization"] != expected:
            raise SchemaRefusal(
                "a Testimonium adapter metadata does not belong to its resolved witness adapter"
            )


def _named_once(references: list[Any]) -> list[Any]:
    """Keep the first mention of each input reference, in the order given.

    A repeat is not a second response and must not read as one.
    """
    seen: list[str] = []
    kept: list[Any] = []
    for reference in references:
        key = (
            json.dumps(reference, sort_keys=True)
            if isinstance(reference, dict)
            else repr(reference)
        )
        if key in seen:
            continue
        seen.append(key)
        kept.append(reference)
    return kept


def validate_retained_response_pairing(payload: dict[str, Any]) -> None:
    """Require retained bytes and their adapter rule to describe one record."""
    has_references = bool(payload.get("raw_response_refs"))
    if "adapter_metadata" in payload and not has_references:
        raise SchemaRefusal("a Testimonium declares adapter metadata without a retained response")
    adapter_name = _provenance_adapter_name(payload)
    if isinstance(adapter_name, str):
        quantization = witness_adapters.resolve_runnable_adapter(adapter_name).quantization
        if has_references and quantization is not None and "adapter_metadata" not in payload:
            raise SchemaRefusal(
                "a Testimonium retained a quantized adapter response without naming its rule"
            )


def validate_retained_response_blob(
    tree: Any, reference: Any, field: str = "raw_response_ref"
) -> None:
    """Re-hash the named response or call blob during tally read-back."""
    checked = validate_stage_blob_ref(reference, field)
    what = f"retained witness {field}"
    read_verified(tree.read_bytes, checked, what)


def validate_page_testimonium_payload(
    payload: Any, *, testimonium_id: str | None = None
) -> dict[str, Any]:
    """The page-record seam is closed before publication and on later reads."""
    if isinstance(payload, dict):
        for reference in payload.get("raw_response_refs", []):
            validate_raw_response_ref(reference)
        validate_adapter_metadata(payload)
        validate_retained_response_pairing(payload)
        if "native_inference" in payload:
            _require_chandra_native_testimonium_scope(payload)
        if problem := _confidence_problem(payload.get("witness_reported")):
            raise SchemaRefusal(problem)
    return validate_shared_page_testimonium_payload(payload, testimonium_id=testimonium_id)


def _require_chandra_native_testimonium_scope(payload: dict[str, Any]) -> None:
    """Keep the native retry capability on Chandra's one admitted chair/scope."""
    provenance = payload.get("provenance")
    identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
    if (
        payload.get("chair") != "attestator_1"
        or not isinstance(identity, dict)
        or identity.get("role") != "attestator_1"
        or identity.get("witness_adapter") != "chandra.v1"
        or identity.get("witness_scope") != "page"
    ):
        raise SchemaRefusal(
            "native_inference belongs only to page-scoped attestator_1 with adapter chandra.v1"
        )


def require_accounted_unrecordable_channel(record: dict[str, Any], payload: dict[str, Any]) -> None:
    """Count an unrecordable response as failed, without holding other witnesses.

    A claimed reading with no retained channel makes the tally UNKNOWN.
    """
    if record["outcome"] in WITNESS_READING_OUTCOMES:
        raise SchemaRefusal(
            f"a Testimonium claims outcome {record['outcome']!r} while recording that its own "
            "native channel was unrecordable; a reading nothing could retain is not a reading, "
            "and its tally cannot be counted as known"
        )
    if record["outcome"] != "failed":
        raise SchemaRefusal(
            f"a Testimonium with outcome {record['outcome']!r} records an unrecordable native "
            "channel; only an attempted-and-failed reading has a channel to be unrecordable"
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SchemaRefusal(
            "a failed Testimonium with an unrecordable native channel records no reason; an "
            "absence with no reason is the silent loss this stage exists to refuse"
        )


def _positive_ordinal(value: str) -> int:
    try:
        ordinal = int(value)
    except ValueError as error:
        raise ValueError("attempt ordinal must be an integer") from error
    if ordinal < 1:
        raise ValueError("attempt ordinal must be positive")
    return ordinal


class Attempt(NamedTuple):
    """One chair's resolved outcome for one page on one attempt.

    Describes one chair only; nothing here compares or ranks witnesses.
    """

    outcome: str
    native_payload: Any
    witness_reported: Any
    format_capabilities: dict[str, Any] | None
    health: dict[str, Any]
    reason: str | None
    raw_response_ref: dict[str, str] | None = None
    observation_payload: Any = None
    # Live-only fields, appended last so positional fixture constructors are
    # unchanged. `serving_call_ref` names this request's call-record blob.
    native_capture: dict[str, Any] | None = None
    serving_call_ref: dict[str, str] | None = None
    receipt_ref: dict[str, str] | None = None
    # Which sort of bytes `raw_response_ref` names; `None` on the fixture path.
    raw_response_kind: str | None = None
    # Chandra-only provenance over every physical request; not the payload.
    native_inference: dict[str, Any] | None = None
    # Fixture responses `(bytes, reference)` the page geometry is derived from,
    # each named in the page record's `raw_response_refs`.
    retained_responses: tuple[tuple[bytes, dict[str, str]], ...] = ()


_CHURRO_PAGE_RESPONSE_FIELDS: Final = frozenset(
    {"scenario", "page_ordinal", "chair", "raw_xml", "transport_stop_reason"}
)
_CHURRO_PAGE_RESPONSE_REQUIRED_FIELDS: Final = frozenset(
    {"page_ordinal", "chair", "raw_xml", "transport_stop_reason"}
)
_CHURRO_CUTOFF_STOP_REASONS: Final = frozenset({"length", "max_new_tokens"})
_CHURRO_STOP_REASONS: Final = frozenset({"eos", "stop"}) | _CHURRO_CUTOFF_STOP_REASONS


def churro_page_response_bytes(row: dict[str, Any]) -> tuple[bytes, str]:
    """Validate one declared transport result and return its exact UTF-8 bytes."""
    if missing := sorted(_CHURRO_PAGE_RESPONSE_REQUIRED_FIELDS - set(row)):
        raise SchemaRefusal(f"a Churro page response lacks required field(s) {missing}")
    raw = row["raw_xml"]
    if not isinstance(raw, str):
        raise SchemaRefusal("a Churro page response raw_xml is not text")
    try:
        raw_bytes = raw.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise SchemaRefusal(
            f"a Churro page response raw_xml is not valid UTF-8 text: {error}"
        ) from error
    stop = row["transport_stop_reason"]
    if not isinstance(stop, str) or stop not in _CHURRO_STOP_REASONS:
        raise SchemaRefusal(
            f"a Churro page response declares unknown transport_stop_reason {stop!r}; "
            f"expected one of {sorted(_CHURRO_STOP_REASONS)}"
        )
    return raw_bytes, stop


def validate_declared_churro_page_responses(context, page_chairs: set[str]) -> None:
    """Refuse unusable current-scenario rows; absent chairs remain valid roster facts."""
    declared_pages = {
        page.get("ordinal") for page in context.fixture.get("page", []) if isinstance(page, dict)
    }
    configured = context.registry.config.chairs
    absent_chairs = {
        chair for chair in context.witness_chairs if isinstance(configured.get(chair), AbsentChair)
    }
    seen: set[tuple[str | None, int, str]] = set()
    for row in context.fixture.get("churro_page_response", []):
        if not isinstance(row, dict):
            raise SchemaRefusal("a fixture [[churro_page_response]] row is not a table")
        scenario = row.get("scenario")
        if scenario is not None and (not isinstance(scenario, str) or not scenario):
            raise SchemaRefusal("a Churro page response scenario is not nonblank text")
        if scenario is not None and scenario != context.scenario:
            continue
        if unknown := sorted(set(row) - _CHURRO_PAGE_RESPONSE_FIELDS):
            raise SchemaRefusal(
                f"a Churro page response declares unknown field(s) {unknown}; a field this "
                "seam does not read is a declaration nothing carries"
            )
        page_ordinal, chair = row.get("page_ordinal"), row.get("chair")
        if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool):
            raise SchemaRefusal(
                f"a Churro page response declares a non-integer page ordinal {page_ordinal!r}"
            )
        if not isinstance(chair, str) or not chair:
            raise SchemaRefusal("a Churro page response declares no chair")
        churro_page_response_bytes(row)
        if page_ordinal not in declared_pages:
            raise SchemaRefusal(
                f"a Churro page response names page {page_ordinal}, which the sealed fixture "
                f"does not declare (declared: {sorted(o for o in declared_pages if o is not None)})"
            )
        key = (scenario, page_ordinal, chair)
        if key in seen:
            raise SchemaRefusal(
                "the fixture declares more than one Churro page response for "
                f"{(page_ordinal, chair)!r} at declaration scope {scenario!r}"
            )
        seen.add(key)
        if chair in absent_chairs:
            continue
        if chair not in page_chairs:
            raise SchemaRefusal(
                f"a Churro page response names chair {chair!r}, which this run does not seal "
                f"as a page witness (page witnesses: {sorted(page_chairs)}, absent: "
                f"{sorted(absent_chairs)}); a response no page-scoped chair can be asked for "
                "would never be captured at all"
            )
        configured_chair = configured[chair]
        if not isinstance(configured_chair, ChairIdentity) or (
            configured_chair.witness_adapter != "churro.v1"
        ):
            raise SchemaRefusal(
                f"a Churro page response names chair {chair!r}, whose configured adapter is "
                f"{getattr(configured_chair, 'witness_adapter', None)!r}, not 'churro.v1'; "
                "fixture bytes may not be attributed to a different model boundary"
            )


def captured_churro_page_attempt(
    context, row: dict[str, Any], chair: str, adapter_name: str
) -> tuple[Attempt, dict[str, Any]]:
    """Capture one declared response before parsing it; never repair or retry it."""
    raw, stop = churro_page_response_bytes(row)
    if adapter_name != "churro.v1":
        raise SchemaRefusal(
            f"a Churro page response for chair {chair!r} reached adapter {adapter_name!r}; "
            "fixture bytes may not be attributed to a different model boundary"
        )
    adapter = witness_adapters.resolve_runnable_adapter(adapter_name)
    capture = adapter.retain(
        context,
        # Churro fixture rows are real vendor-grammar answers, so the view records
        # the adapter's own prompt; no framing, as no request was made.
        view={"prompt": adapter.prompt(), "generation": feeding.churro_generation()},
        raw_response=raw,
        transport_stop_reason=stop,
        # Same parser name as the live path.
        parser="xml",
    )
    # Read from the adapter, as the live path does, so the two cannot diverge.
    capabilities = _declared_format_capabilities(adapter)
    parsed = capture["parse"]
    # Post-hoc findings cannot decide whether the transport cut off the response.
    cut_off = stop in _CHURRO_CUTOFF_STOP_REASONS
    if parsed["state"] == "parsed" and not (cut_off and parsed["text"] == ""):
        text = parsed["text"]
        complete = not cut_off
        return (
            Attempt(
                # Partial characters remain evidence when truncation is visible.
                "genuinely-empty" if text == "" else "read",
                text,
                None,
                capabilities,
                content_health(text, completed=complete),
                None,
            ),
            capture,
        )
    if parsed["state"] == "parsed":
        # An interrupted empty response is not evidence of a blank page.
        return (
            Attempt(
                "failed",
                "",
                None,
                capabilities,
                content_health("", completed=False),
                (
                    f"Churro response parsed empty after the provider stopped it at its bound "
                    f"(transport_stop_reason {stop!r}); a cut-off response is not a confirmed "
                    "blank page"
                ),
            ),
            capture,
        )
    # `recordable=None` is reserved for no response. An unrecordable response
    # cannot carry a measured truncation flag, so its basis and reason name a cut.
    cut_note = (
        f"the provider stopped the response at its bound (transport_stop_reason {stop!r}) and "
        if cut_off
        else ""
    )
    # Shared with the live path and the page validator, which re-derives and
    # compares these sentences.
    parse_refusal = native_parse_refusal(parsed)
    basis = (
        f"response cut off by the provider ({stop!r}); {parse_refusal}"
        if cut_off
        else parse_refusal
    )
    return (
        Attempt(
            "failed",
            None,
            None,
            capabilities,
            _unrecordable_health(basis),
            f"Churro response retained but not usable: {cut_note}{parse_refusal}",
        ),
        capture,
    )


def _renumbered_onto(observed: list[dict[str, Any]], items) -> None:
    for item in items:
        observed.append({**item, "ordinal": len(observed)})


# --- What the fixture declares a chair answered for one page ------------------------
#
# Each table declares one chair's response to one whole page, keyed by
# `page_ordinal` and `chair`. A row binds attempt ordinal 1 unless it names
# another, and a row scoped to the running scenario replaces every unscoped row
# for its page and chair. DAI is declared record by record
# (`[[dai_record_response]]`), never here.

PAGE_RESPONSE_TABLES: Final = (
    "testimony",
    "churro_page_response",
    "witness_empty",
    "witness_failure",
    "witness_not_run",
    "witness_malformed",
)
# Tables whose rows describe one scenario's departure, never a base response.
SCENARIO_ONLY_TABLES: Final = frozenset(
    {"witness_empty", "witness_failure", "witness_not_run", "witness_malformed"}
)
_DECLARATION_KEYS: Final = frozenset({"scenario", "page_ordinal", "chair", "attempt_ordinal"})
# The fields each table's row may carry; a field nothing reads is refused, so no
# declared response is silently discarded. Churro's rows are closed by
# `validate_declared_churro_page_responses`.
_DECLARATION_FIELDS: Final = {
    "testimony": _DECLARATION_KEYS
    | {"payload", "raw_responses", "witness_reported", "format_capabilities"},
    "witness_empty": _DECLARATION_KEYS,
    "witness_failure": _DECLARATION_KEYS,
    "witness_not_run": _DECLARATION_KEYS,
    "witness_malformed": _DECLARATION_KEYS | {"reason"},
}


def validate_declared_page_responses(context, page_chairs: set[str]) -> None:
    """Refuse a page declaration no chair of this run can be asked for.

    A row naming a record reader would never be read, so it is refused like a
    row naming an undeclared page; rows naming an absent chair are roster facts.
    """
    validate_declared_churro_page_responses(context, page_chairs)
    declared_pages = {
        page.get("ordinal") for page in context.fixture.get("page", []) if isinstance(page, dict)
    }
    configured = context.registry.config.chairs
    for table in PAGE_RESPONSE_TABLES:
        for number, row in enumerate(context.fixture.get(table, []), start=1):
            if not isinstance(row, dict):
                raise SchemaRefusal(f"fixture [[{table}]] row {number} is not a table")
            scenario = row.get("scenario")
            if table in SCENARIO_ONLY_TABLES and (not isinstance(scenario, str) or not scenario):
                raise SchemaRefusal(f"fixture [[{table}]] row {number} has no scenario: {row!r}")
            fields = _DECLARATION_FIELDS.get(table)
            if fields is not None and (unknown := sorted(set(row) - fields)):
                raise SchemaRefusal(
                    f"fixture [[{table}]] row {number} declares unknown field(s) {unknown}; a "
                    "field this seam does not read is a declaration nothing carries"
                )
            if row.get("page_ordinal") not in declared_pages:
                raise SchemaRefusal(
                    f"fixture [[{table}]] row {number} names page {row.get('page_ordinal')!r}, "
                    "which the sealed fixture does not declare"
                )
            chair = row.get("chair")
            if isinstance(configured.get(chair), AbsentChair):
                continue
            if chair not in page_chairs or reads_detector_records(configured.get(chair)):
                raise SchemaRefusal(
                    f"fixture [[{table}]] row {number} names chair {chair!r}, which this run "
                    "does not ask for a whole page; a response no chair is asked for would "
                    "never be read"
                )


def declared_page_response(
    context, page_ordinal: int, chair: str, ordinal: int
) -> tuple[str, dict[str, Any]] | None:
    """The one declared response for this page, chair and attempt, with its table."""
    base: list[tuple[str, dict[str, Any]]] = []
    scoped: list[tuple[str, dict[str, Any]]] = []
    for table in PAGE_RESPONSE_TABLES:
        for row in context.fixture.get(table, []):
            if row.get("page_ordinal") != page_ordinal or row.get("chair") != chair:
                continue
            if not _declared_for_ordinal(row, ordinal):
                continue
            if row.get("scenario") is None:
                base.append((table, row))
            elif row["scenario"] == context.scenario:
                scoped.append((table, row))
    matches = scoped or base
    if len(matches) > 1:
        raise SchemaRefusal(
            f"fixture declares {len(matches)} responses {sorted(table for table, _ in matches)} "
            f"for chair {chair!r} on page {page_ordinal} at attempt ordinal {ordinal}; two "
            "answers to one question may not collapse silently into one"
        )
    return matches[0] if matches else None


def _fixture_chandra_attempt(context, row: dict[str, Any], witness_adapter: str) -> Attempt:
    """Derive Chandra fixture text and geometry from declared response bytes.

    Each declared response is retained as it would be served; the page text joins
    their non-empty readings and must equal the row's declared payload.
    """
    if witness_adapter not in FIXTURE_NATIVE_RESPONSE_ADAPTERS:
        raise SchemaRefusal(
            f"fixture raw_responses have no native byte route for adapter {witness_adapter!r}; "
            "fixture bytes may not be attributed to a model that never produced them"
        )
    # This is Chandra's recipe; any other adapter's bytes would be filed under
    # Chandra's model boundary.
    if witness_adapter != "chandra.v1":
        raise SchemaRefusal(
            f"fixture raw_responses for adapter {witness_adapter!r} would be retained through "
            "Chandra's recipe -- its own retained view, prompt and parser -- and filed under "
            "Chandra's model boundary; write that adapter's own fixture retain branch before "
            "adding it to FIXTURE_NATIVE_RESPONSE_ADAPTERS"
        )
    declared = row["raw_responses"]
    if (
        not isinstance(declared, list)
        or not declared
        or not all(isinstance(item, str) for item in declared)
    ):
        raise SchemaRefusal("fixture raw_responses is not a list of retained response texts")
    adapter = witness_adapters.resolve_runnable_adapter(witness_adapter)
    retained: list[tuple[bytes, dict[str, str]]] = []
    texts: list[str] = []
    unparsed: str | None = None
    for text in declared:
        raw_response = text.encode("utf-8")
        capture = adapter.retain(
            context,
            # The fixture's frozen prompt, not `adapter.prompt()`: this view is sealed
            # into pinned fixture bytes, and the served prompt must be free to change
            # without moving them.
            view={"prompt": dict(chandra.FIXTURE_PROMPT)},
            raw_response=raw_response,
            transport_stop_reason=FIXTURE_COMPLETE_STOP,
            parser="json",
        )
        retained.append((raw_response, capture["raw_response_ref"]))
        parsed = capture["parse"]
        if parsed["state"] != "parsed":
            unparsed = unparsed or parsed["outcome"]
        else:
            texts.append(parsed["text"])
    native_payload: Any = (
        {"parse_outcome": unparsed} if unparsed else "\n".join(text for text in texts if text)
    )
    if unparsed is None and row.get("payload") != native_payload:
        raise SchemaRefusal("fixture Chandra raw response text differs from its declared payload")
    # Health is kept as `prepared_response` computed it; recomputing it from a
    # `None` payload would erase an unrecordable channel.
    native_payload, witness_reported, capabilities, health, recording_problem = prepared_response(
        {**row, "payload": native_payload}
    )
    if unparsed is not None:
        outcome = "failed"
        reason = f"the Chandra response shape was not recognized: {unparsed}"
        if recording_problem is not None:
            reason = f"{reason}; {recording_problem}"
    elif recording_problem is not None:
        outcome = "failed"
        reason = f"the provider response was refused without repair: {recording_problem}"
    else:
        outcome = "genuinely-empty" if native_payload == "" else "read"
        reason = None
    return Attempt(
        outcome,
        native_payload,
        witness_reported,
        capabilities,
        health,
        reason,
        retained_responses=tuple(retained),
    )


def not_run_attempt(reason: str) -> Attempt:
    """A configured chair this pass never asked about the page."""
    return Attempt(
        outcome="not-run",
        native_payload=None,
        witness_reported=None,
        format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
        health=no_response_health(reason="not-attempted"),
        reason=reason,
    )


def fixture_page_attempt(
    context, page_ordinal: int, chair: str, resolved: ChairIdentity, ordinal: int
) -> Attempt:
    """One chair's fixture-declared outcome for one whole page at this attempt ordinal."""
    declared = declared_page_response(context, page_ordinal, chair, ordinal)
    if declared is None:
        return not_run_attempt("no response is declared for this configured chair on this page")
    table, row = declared
    if table == "churro_page_response":
        attempt, capture = captured_churro_page_attempt(
            context, row, chair, resolved.witness_adapter
        )
        return attempt._replace(native_capture=capture)
    if table == "witness_not_run":
        return not_run_attempt("fixture declares that this configured chair was never attempted")
    if table == "witness_failure":
        return Attempt(
            "failed",
            None,
            None,
            DEFAULT_FORMAT_CAPABILITIES,
            no_response_health(reason="attempted-but-no-usable-response"),
            "the chair returned no usable response",
        )
    if table == "witness_malformed":
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal("a malformed witness declaration has no reason")
        return Attempt(
            "failed",
            None,
            None,
            DEFAULT_FORMAT_CAPABILITIES,
            _unrecordable_health(reason),
            f"the provider response was refused without repair: {reason}",
        )
    if table == "witness_empty":
        row = {"payload": ""}
    elif "raw_responses" in row:
        return _fixture_chandra_attempt(context, row, resolved.witness_adapter)
    native_payload, witness_reported, capabilities, health, recording_problem = prepared_response(
        row
    )
    if recording_problem is not None:
        outcome = "failed"
        reason = f"the provider response was refused without repair: {recording_problem}"
    else:
        # Only a retained, recordable empty response reaches `genuinely-empty`.
        outcome = "genuinely-empty" if native_payload == "" else "read"
        reason = None
    return Attempt(outcome, native_payload, witness_reported, capabilities, health, reason)


# --- One page Testimonium ------------------------------------------------------------


def _response_partition(
    context,
    *,
    resolved: ChairIdentity,
    presented: dict[str, Any],
    attempt: Attempt,
    fixture_observed: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """Partition page geometry from the retained responses, with response-linked edge findings.

    A served response is named by its capture, so only fixture responses are
    listed in the record's `raw_response_refs`.
    """
    adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
    page_size = _sealed_source_page(context, presented)[2]
    sources: list[tuple[bytes, dict[str, str] | None, bool]] = [
        (raw, reference, True) for raw, reference in attempt.retained_responses
    ]
    if not sources and attempt.observation_payload is not None:
        sources.append((attempt.observation_payload, attempt.raw_response_ref, False))
    observed: list[dict[str, Any]] = []
    response_refs: list[dict[str, str]] = []
    edge_overshoots: list[dict[str, Any]] = []
    for raw, reference, name_in_partition in sources:
        source_observed, overshoots = page_partition_entries(
            _partition_geometry(adapter.observe(presented, raw, page_size=page_size)),
            page_size=page_size,
            raw_response_ref=reference,
        )
        # A page-edge finding's response must be reachable from the partition list.
        if (
            (name_in_partition or overshoots)
            and reference is not None
            and reference not in response_refs
        ):
            response_refs.append(reference)
        # A response retained twice must not name one finding twice.
        edge_overshoots.extend(
            overshoot
            for overshoot in overshoots
            if (overshoot["response_sha256"], overshoot["ordinal"])
            not in {(seen["response_sha256"], seen["ordinal"]) for seen in edge_overshoots}
        )
        _renumbered_onto(observed, source_observed)
    if not observed:
        # No reported geometry: the presentation echo stands in, excluded from
        # routing and coverage.
        _renumbered_onto(observed, observed_from_presentation(presented))
    if fixture_observed is not None:
        _renumbered_onto(observed, fixture_observed)
    return observed, response_refs, edge_overshoots


def _page_observations(
    context,
    *,
    resolved: ChairIdentity,
    adapter: Any,
    chair: str,
    page_ordinal: int,
    presented: dict[str, Any],
    attempt: Attempt,
    live: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """What the chair located on the page, the responses named, and edge findings."""
    if not presented:
        return [], [], []
    # Declared fixture geometry; a live response carries its own.
    fixture_observed = (
        None
        if live
        else _fixture_native_observations(context, chair=chair, page_ordinal=page_ordinal)
    )
    if _derives_partition_from_response(resolved, live):
        return _response_partition(
            context,
            resolved=resolved,
            presented=presented,
            attempt=attempt,
            fixture_observed=fixture_observed,
        )
    if fixture_observed is not None:
        return fixture_observed, [], []
    return adapter.observe(presented, attempt.native_payload), [], []


def page_testimonium_payload(
    *,
    chair: str,
    page_ordinal: int,
    ordinal: int,
    provenance: dict[str, Any],
    attempt: Attempt,
    presented: dict[str, Any],
    observed: list[dict[str, Any]],
    testimonium_id: str,
    page_edge_overshoots: list[dict[str, Any]] | None = None,
    raw_response_refs: list[dict[str, str]] | None = None,
    adapter_metadata: dict[str, str] | None = None,
    presentations: list[dict[str, Any]] | None = None,
    unit_captures: list[dict[str, Any] | None] | None = None,
    unit_call_refs: list[dict[str, str] | None] | None = None,
) -> dict[str, Any]:
    """One chair's closed page record, validated before it is published."""
    record: dict[str, Any] = {
        "chair": chair,
        "attempt_ordinal": ordinal,
        "provenance": provenance,
        "format_capabilities": attempt.format_capabilities,
        "payload": attempt.native_payload,
        "witness_reported": attempt.witness_reported,
        "content_health": attempt.health,
        "presented": presented,
        "observed": observed,
        "scope": "page",
        "page_ordinal": page_ordinal,
    }
    if raw_response_refs:
        record["raw_response_refs"] = raw_response_refs
    _set_present(
        record,
        reason=attempt.reason,
        page_edge_overshoots=page_edge_overshoots,
        adapter_metadata=adapter_metadata,
        native_capture=attempt.native_capture,
        native_inference=attempt.native_inference,
        presentations=presentations,
        unit_captures=unit_captures,
        unit_call_refs=unit_call_refs,
        serving_call_ref=attempt.serving_call_ref,
    )
    validate_page_testimonium_payload(record, testimonium_id=testimonium_id)
    validate_page_record_facts(record, attempt.outcome)
    return record


def validate_page_record_facts(payload: dict[str, Any], outcome: str) -> None:
    """The facts this stage computes and the shared schema does not close."""
    validate_content_health(payload["payload"], payload["content_health"])
    if payload["content_health"]["recordable"] is False:
        require_accounted_unrecordable_channel({"outcome": outcome}, payload)
    if payload["format_capabilities"] is None:
        if outcome != "failed":
            raise SchemaRefusal("a non-failed Testimonium carries no format_capabilities record")
    else:
        format_capabilities_for({"format_capabilities": payload["format_capabilities"]})
    if outcome not in WITNESS_READING_OUTCOMES:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal(
                f"a {outcome} Testimonium records no reason for its non-reading outcome"
            )


def publish_page_testimonium(
    context,
    *,
    chair: str,
    resolved: ChairIdentity,
    page_ordinal: int,
    attempt: Attempt,
    ordinal: int,
    page_ids: dict[int, str],
    live: bool,
) -> None:
    """Seal one whole-page chair's Testimonium for one page: the only write path for it."""
    # First, so a chair the run did not seal page-scoped refuses before any record is built.
    page_chairs = declared_page_witness_chairs(context)
    if chair not in page_chairs:
        raise FatalAccounting(
            f"chair {chair!r} is not a page witness in the sealed roster; a page record "
            "for it would claim a scope the run never declared"
        )
    attempted = attempt.outcome in ATTEMPTED_WITNESS_OUTCOMES
    page_subject_id = page_subject(context, page_ordinal, page_ids=page_ids)
    page_attempt = attempt_id(page_subject_id, f"read:{chair}", ordinal)
    page_artifact_id = artifact_id(ATTESTATORES, "page-testimonium", page_subject_id, page_attempt)
    presented: dict[str, Any] = {}
    adapter = None
    if attempted:
        adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
        source = presentation_for_page(context, page_ordinal, page_ids=page_ids)
        presented = adapter.present(context, source)
        witness_adapters.validate_adapter_presentation(resolved.witness_adapter, source, presented)
    observed, response_refs, edge_overshoots = _page_observations(
        context,
        resolved=resolved,
        adapter=adapter,
        chair=chair,
        page_ordinal=page_ordinal,
        presented=presented,
        attempt=attempt,
        live=live,
    )
    payload = page_testimonium_payload(
        chair=chair,
        page_ordinal=page_ordinal,
        ordinal=ordinal,
        # Every attempted outcome, failed included, is receipt-backed.
        provenance=provenance_for(
            context, resolved, attempted=attempted, receipt_ref=attempt.receipt_ref
        ),
        attempt=attempt,
        presented=presented,
        observed=observed,
        testimonium_id=page_artifact_id,
        # Absent for a page never presented: no box was reported to reject.
        page_edge_overshoots=edge_overshoots if presented else None,
        raw_response_refs=response_refs,
        adapter_metadata=declared_adapter_metadata(resolved, has_raw_response=bool(response_refs)),
    )
    inputs = [context.input_ref(presented["image_path"])] if presented else []
    validate_testimonium_presentation(context, {"payload": payload, "inputs": inputs})
    verify_page_call_sampling(context, payload, chair)
    context.publish(
        kind="page-testimonium",
        subject_id=page_subject_id,
        outcome=attempt.outcome,
        attempt=page_attempt,
        # Every retained response is an input, because `read_artifact` re-hashes
        # only `inputs`. A live Chandra page reaches one blob twice, so each is
        # named once.
        inputs=_named_once(
            inputs
            + response_refs
            + (
                [attempt.native_capture["raw_response_ref"]]
                if attempt.native_capture is not None
                else []
            )
            + _chandra_trace_inputs(attempt.native_inference)
            + ([attempt.serving_call_ref] if attempt.serving_call_ref is not None else [])
        ),
        payload=payload,
    )


# --- Which pages are witnessed, and at which attempt ordinal -------------------------


def sealed_pages(context, page_ids: dict[int, str]) -> list[tuple[int, str]]:
    """Every page the Exemplar sealed, in page order: the pages the Perlector reads.

    A page the Exemplar refused has no pixels to show, so no witness reads it.
    """
    pages = []
    for page_ordinal, page_id in sorted(page_ids.items()):
        page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
        if page.get("outcome") == "sealed":
            pages.append((page_ordinal, page_id))
    return pages


def page_witness_roster(context) -> list[str]:
    """The configured chairs asked for every sealed page, in roster order.

    An absent chair is never asked; the page path counts it against the floor.
    """
    page_chairs = declared_page_witness_chairs(context)
    return [chair for chair in context.witness_chairs if chair in page_chairs]


PageHistory = dict[tuple[str, str], list[dict[str, Any]]]


def page_history(context) -> PageHistory:
    """This stage's own page records by (page, chair), read once for append decisions."""
    history: PageHistory = {}
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != PAGE_TESTIMONIUM_KIND:
            continue
        record = context.tree.read_artifact(
            ATTESTATORES, PAGE_TESTIMONIUM_KIND, entry["artifact_id"]
        )
        payload = record.get("payload")
        chair = payload.get("chair") if isinstance(payload, dict) else None
        if isinstance(chair, str):
            history.setdefault((entry["subject_id"], chair), []).append(record)
    return history


def _current_ordinal(history: PageHistory, page_id: str, chair: str) -> int | None:
    records = history.get((page_id, chair))
    if not records:
        return None
    current = latest_attempt(
        records, f"page Testimonium for {(page_id, chair)!r}", operation=f"read:{chair}"
    )
    return current["payload"]["attempt_ordinal"]


def require_appendable_ordinal(
    history: PageHistory, page_id: str, chair: str, ordinal: int
) -> None:
    """Allow a repeat of an ordinal a page holds, or its next one; RunTree checks repeat bytes."""
    current = _current_ordinal(history, page_id, chair)
    if current is None:
        if ordinal != 1:
            raise SchemaRefusal(
                f"page Testimonium for {(page_id, chair)!r} has no attempt 1; cannot append "
                f"ordinal {ordinal} across a missing history"
            )
        return
    if ordinal > current + 1:
        raise SchemaRefusal(
            f"page Testimonium for {(page_id, chair)!r} is current at ordinal {current}; "
            f"ordinal {ordinal} is neither a rerun of an attempt it holds nor its next "
            "append-only attempt"
        )


def witness_bound_pages(context) -> frozenset[str]:
    """The pages whose Perlector feed already showed a witness.

    A reading is established over the testimony it was shown, so a new
    attempt there would supersede the very records that reading names.
    """
    bound = set()
    for entry in context.tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != PAGE_FEED_KIND or entry["subject_id"] in bound:
            continue
        feed = context.tree.read_artifact(PERLECTOR, PAGE_FEED_KIND, entry["artifact_id"])
        if feed.get("payload", {}).get("witnesses"):
            bound.add(entry["subject_id"])
    return frozenset(bound)


def _refuse_write_collision(
    history: PageHistory, page_id: str, chair: str, ordinal: int, attempt: Attempt
) -> None:
    """Refuse a pass that would record a different answer at an ordinal already sealed.

    Checked for every page before any record is written; earlier raw response
    blobs stay in custody.
    """
    sealed = [
        record
        for record in history.get((page_id, chair), [])
        if record["payload"]["attempt_ordinal"] == ordinal
    ]
    if not sealed:
        return
    (record,) = sealed
    payload = record["payload"]
    retained = [reference for _raw, reference in attempt.retained_responses]
    if (
        record["outcome"] != attempt.outcome
        or payload.get("payload") != attempt.native_payload
        or payload.get("witness_reported") != attempt.witness_reported
        or payload.get("format_capabilities") != attempt.format_capabilities
        or payload.get("content_health") != attempt.health
        or payload.get("reason") != attempt.reason
        or payload.get("native_capture") != attempt.native_capture
        or payload.get("raw_response_refs", []) != retained
    ):
        raise SchemaRefusal(
            f"a whole pass at ordinal {ordinal} would record a different attempt for "
            f"{(page_id, chair)!r} than the one already sealed there: sealed outcome "
            f"{record['outcome']!r}, this pass would write {attempt.outcome!r}. No Testimonium "
            "was written for this pass; any raw response custody retained before this refusal "
            "remains visible in the blob inventory"
        )


def preflight(
    context,
    pages: list[tuple[int, str]],
    ordinal: int,
    history: PageHistory,
    *,
    fixture: bool,
) -> dict[tuple[int, str], Attempt]:
    """Check every page's history, and resolve every fixture answer, before any write.

    Returns the fixture attempts of the chairs that read a whole page in one
    response; a live pass and a record reader resolve as they read.
    """
    roster = page_witness_roster(context)
    if fixture:
        validate_declared_page_responses(context, set(roster))
    appending = [
        page_id
        for _page_ordinal, page_id in pages
        if any((_current_ordinal(history, page_id, chair) or 0) < ordinal for chair in roster)
    ]
    for page_id, chair in ((page_id, chair) for _o, page_id in pages for chair in roster):
        require_appendable_ordinal(history, page_id, chair, ordinal)
    if bound := sorted(witness_bound_pages(context).intersection(appending)):
        raise ContractError(
            f"page(s) {bound} were already shown to the Perlector with their witnesses, so their "
            f"witness layer is closed: a whole pass at ordinal {ordinal} would append testimony "
            "no established reading was shown. Re-asking a witness because it spoke again is a "
            "re-roll. The reading stands; to witness these pages again, start a new run"
        )
    planned: dict[tuple[int, str], Attempt] = {}
    if not fixture:
        return planned
    for chair in roster:
        resolved = context.registry.resolve(chair)
        if reads_detector_records(resolved):
            continue
        for page_ordinal, page_id in pages:
            attempt = fixture_page_attempt(context, page_ordinal, chair, resolved, ordinal)
            _refuse_write_collision(history, page_id, chair, ordinal, attempt)
            planned[(page_ordinal, chair)] = attempt
    return planned


# --- The attempt tally ----------------------------------------------------------------


def _unknown_tally(reason: str) -> dict[str, Any]:
    return {"state": "UNKNOWN", "count": None, "hold": True, "reason": reason}


def attempt_tally(
    context,
    *,
    pages: list[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Compare the stored inventory with the rebuilt and validated page Testimonia.

    With ``pages``, every sealed page must carry a record from every
    roster chair; without it the pass that fills the denominator has not run
    yet. Any inventory damage or divergence makes the count UNKNOWN, and the
    caller holds.
    """
    tree = context.tree
    try:
        stored_path = tree.resolve(tree.manifest_path(ATTESTATORES))
        stored = json.loads(stored_path.read_bytes().decode("utf-8"))
        rebuilt = tree.build_manifest(ATTESTATORES)
    except FatalAccounting:
        raise
    except (ContractError, OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        # json recurses per nesting level, so a deeply nested manifest raises
        # RecursionError here; it must become UNKNOWN and hold, not a traceback.
        return _unknown_tally(str(error))
    if stored != rebuilt:
        return _unknown_tally(
            "the stored Attestatores manifest does not equal its rebuilt inventory"
        )
    try:
        roster = page_witness_roster(context)
        by_pair: PageHistory = {}
        for entry in rebuilt["artifacts"]:
            if entry["kind"] != PAGE_TESTIMONIUM_KIND:
                continue
            record = tree.read_artifact(ATTESTATORES, PAGE_TESTIMONIUM_KIND, entry["artifact_id"])
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise SchemaRefusal("a page Testimonium carries no object payload")
            validate_page_testimonium_payload(payload, testimonium_id=record.get("artifact_id"))
            validate_page_record_facts(payload, record["outcome"])
            chair = payload["chair"]
            if chair not in roster:
                raise SchemaRefusal("a page Testimonium tally record names no roster page witness")
            validate_page_testimonium_record(context, record)
            if payload.get("native_capture") is not None:
                verify_page_native_capture(
                    context,
                    f"page {record['subject_id']}",
                    chair,
                    record,
                    payload["native_capture"],
                )
            verify_page_call_sampling(context, payload, chair)
            by_pair.setdefault((record["subject_id"], chair), []).append(record)
        if pages is not None:
            expected = {(page_id, chair) for _ordinal, page_id in pages for chair in roster}
            if set(by_pair) != expected:
                raise SchemaRefusal(
                    "the rebuilt Testimonium inventory does not account for every sealed "
                    "page/chair pair"
                )
        for (page_id, chair), records in by_pair.items():
            latest_attempt(
                records,
                f"page Testimonium tally for {(page_id, chair)!r}",
                operation=f"read:{chair}",
            )
    except FatalAccounting:
        # A broken partition, not an unknown count; it must never become a hold.
        raise
    except (ContractError, OSError) as error:
        return _unknown_tally(str(error))
    return {
        "state": "KNOWN",
        "count": sum(len(records) for records in by_pair.values()),
        "hold": False,
        "reason": None,
    }


def witness_serving_modes(context, recipes: ServingRecipes, tier: str | None) -> dict[str, str]:
    """Refuse a roster mixing unmarked fixture and live evidence in one layer."""
    modes: dict[str, str] = {}
    for chair in context.witness_chairs:
        resolved = context.registry.resolve(chair)
        if not isinstance(resolved, ChairIdentity):
            continue
        try:
            modes[chair] = serving_mode_for(recipes, resolved, tier)
        except ServingError as error:
            raise ContractError(
                f"the serving posture of chair {chair!r} could not be resolved: {error}"
            ) from error
    postures = {
        mode: sorted(name for name, value in modes.items() if value == mode)
        for mode in modes.values()
    }
    if len(postures) > 1:
        raise ContractError(
            f"this run's witness roster mixes serving postures {postures}; one run reads its "
            "witnesses one way. Seal a catalogue whose rows for every configured witness chair "
            "are the same kind, or run the fixture catalogue"
        )
    return modes


def require_every_witness_served(modes: dict[str, str]) -> None:
    """Require served chairs on real ingress, where fixture chairs cannot answer."""
    unserved = sorted(chair for chair, mode in modes.items() if mode != "live")
    if unserved:
        raise ContractError(
            f"this run is a real submission and its sealed serving catalogue gives witness "
            f"chair(s) {unserved} a fixture posture; a real submission has no fixture to "
            "answer for a witness, so every configured witness chair must be served. Seal "
            "the run under a catalogue whose row for every witness chair is live"
        )
    if not modes:
        raise ContractError(
            "this run is a real submission and no configured witness chair serves; a pass in "
            "which no witness is shown any ink is not a real posture"
        )


def production_serving_factory(
    decoding_policy: Mapping[str, Any], decoding_sha256: str
) -> Callable[[Any, ChairIdentity, str], ChairClient]:
    """The factory a live pass reads each chair through, under the decoding policy
    `main` loaded and sealed. Tests inject their own factory in-process; it is
    deliberately not a CLI flag, so no fake can answer under a configured chair's
    name."""
    return partial(
        stage_chair_client,
        decoding_policy=decoding_policy,
        decoding_config_sha256=decoding_sha256,
    )


def attempt_from_live(live: live_witness.LiveAttempt) -> Attempt:
    """Convert one `LiveAttempt` into the `Attempt` every write path shares."""
    return Attempt(
        outcome=live.outcome,
        native_payload=live.native_payload,
        witness_reported=live.witness_reported,
        format_capabilities=(
            dict(live.format_capabilities) if live.format_capabilities is not None else None
        ),
        health=dict(live.health),
        reason=live.reason,
        raw_response_ref=dict(live.raw_response_ref) if live.raw_response_ref else None,
        observation_payload=live.observation_payload,
        native_capture=dict(live.native_capture) if live.native_capture is not None else None,
        serving_call_ref=dict(live.call_record_ref) if live.call_record_ref else None,
        receipt_ref=dict(live.receipt_ref) if live.receipt_ref else None,
        raw_response_kind=live.raw_response_kind,
        native_inference=None,
    )


# vLLM's `stop` and `length`, the fixture transport's synonyms for them, and the
# no-stop-reason marker. Any other word has no measured meaning.
_LIVE_ENGINE_STOP_WORDS: Final = _CHURRO_STOP_REASONS | {STOP_REASON_UNREPORTED}


def refuse_unpublishable_stop_word(transport_stop_reason: str, what: str) -> None:
    """Refuse an unknown engine stop word, even when the body did not parse.

    Calling it complete or cut off would invent a measurement; bytes are retained.
    """
    if transport_stop_reason not in _LIVE_ENGINE_STOP_WORDS:
        raise ContractError(
            f"{what} reports transport_stop_reason {transport_stop_reason!r}, which this "
            "pipeline has never measured a meaning for; recording it as complete or as cut "
            "off would assert a boundary nobody observed. The response bytes are retained "
            "and nothing was published for it"
        )


def _refuse_unpublishable_response(response: Any, what: str) -> None:
    stop_word = response.finish_reason
    refuse_unpublishable_stop_word(STOP_REASON_UNREPORTED if stop_word is None else stop_word, what)


def capacity_refusal_attempt(
    error: RequestCapacityRefusal,
    *,
    receipt_ref: Mapping[str, str],
    what: str,
    adapter: Any = None,
) -> Attempt:
    """Fail only this oversized request, keeping later pages available to read.

    No response arrived; the real receipt marks this attempt live for resume.
    Adapter capabilities stay recorded even for a pre-send refusal.
    """

    reason = f"{what} was refused before it was sent: {error}"
    return Attempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=(
            _declared_format_capabilities(adapter)
            if adapter is not None
            else DEFAULT_FORMAT_CAPABILITIES
        ),
        health=no_response_health(reason=reason),
        reason=reason,
        receipt_ref=dict(receipt_ref),
    )


# --- A page witness shown one record at a time (DAI) ----------------------------
#
# DAI was trained on crops of the records its own project's detector finds, so
# page-scoped it reads the Designator's `detector-region` crops of its page, one
# request each, in the detector's own order. Its page Testimonium lists every
# image it was shown, and its text joins the unit readings in that order.

DETECTOR_PAGE_KIND: Final = "detector-page"
DETECTOR_RECORD_KIND: Final = "detector-record"
DETECTOR_REGION_KIND: Final = "detector-region"
UNCAPPED_DETECTOR_REASON: Final = (
    "DAI's own record detector found no record on this page, but its run facts are incomplete "
    "(they state no cap), so its census is not taken as having looked at the page, and DAI "
    "was shown nothing here"
)


def no_detector_unit_reason(detection_count: int) -> str:
    """Why a page's DAI record is `not-run` with no request.

    The detector found no record and its run facts state no cap, or the
    records it found enclosed no crop.
    """
    if detection_count == 0:
        return UNCAPPED_DETECTOR_REASON
    return (
        f"DAI's own record detector found {detection_count} record(s) on this page and none "
        "enclosed a crop, so DAI was shown nothing here"
    )


def _verify_detector_region(context, region: dict[str, Any]) -> None:
    """A record crop re-derives from its sealed page at its own recorded bounds."""
    payload = region.get("payload")
    transform = payload.get("transform") if isinstance(payload, dict) else None
    if (
        not isinstance(transform, dict)
        or payload.get("origin") != "detector"
        or payload.get("padding") is not None
        or payload.get("raw_bounds") != transform.get("bounds")
        or payload.get("region_id") != region_id(region["subject_id"], transform)
    ):
        raise SchemaRefusal(
            f"detector region {region.get('artifact_id')} is not a detector crop of its record"
        )
    _, page_bytes = read_sealed_page(context.tree, transform["source_page_id"], what="DAI")
    crop = crop_png(page_bytes, transform["bounds"])
    if digest_bytes(crop) != payload.get("image_sha256"):
        raise SchemaRefusal(
            f"detector region {region.get('artifact_id')} does not re-derive from its sealed page"
        )
    validate_serving_provenance(
        context, payload.get("provenance"), producer_stage=DESIGNATOR, require_receipt=True
    )


def detector_units_by_page(
    context,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    """Each sealed page's record crops, in the detector's own order, and its census count.

    Read from the Designator's per-page census, so a page the detector found
    nothing on has no units and a missing record refuses by name. A record whose
    box encloses no crop is kept by the Designator and is not a unit, but it is
    counted.
    """
    kinds = (DETECTOR_PAGE_KIND, DETECTOR_RECORD_KIND, DETECTOR_REGION_KIND)
    by_kind: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in kinds}
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] in by_kind:
            record = context.tree.read_artifact(DESIGNATOR, entry["kind"], entry["artifact_id"])
            if record["subject_id"] in by_kind[entry["kind"]]:
                raise FatalAccounting(
                    f"the Designator carries two {entry['kind']} records for "
                    f"{record['subject_id']!r}"
                )
            by_kind[entry["kind"]][record["subject_id"]] = record
    units: dict[int, list[dict[str, Any]]] = {}
    detections: dict[int, int] = {}
    for page in by_kind[DETECTOR_PAGE_KIND].values():
        payload = page["payload"]
        ordinal = payload["page_ordinal"]
        subjects = payload["record_subjects"]
        if len(subjects) != payload["detection_count"]:
            raise FatalAccounting(
                f"page {ordinal}'s detector census names {len(subjects)} records for "
                f"{payload['detection_count']} detections"
            )
        page_units = []
        for position, subject in enumerate(subjects):
            record = by_kind[DETECTOR_RECORD_KIND].get(subject)
            if (
                record is None
                or record["payload"]["page_ordinal"] != ordinal
                or record["payload"]["detector_ordinal"] != position
            ):
                raise FatalAccounting(
                    f"page {ordinal}'s detector record {subject!r} is missing or out of order"
                )
            if not record["payload"]["cut"]:
                continue
            region = by_kind[DETECTOR_REGION_KIND].get(subject)
            if region is None or record["payload"]["region_ref"] != context.artifact_ref(
                DESIGNATOR, DETECTOR_REGION_KIND, region["artifact_id"]
            ):
                raise FatalAccounting(
                    f"page {ordinal}'s detector record {subject!r} names no sealed crop"
                )
            _verify_detector_region(context, region)
            page_units.append(region)
        units[ordinal] = page_units
        detections[ordinal] = payload["detection_count"]
    return units, detections


# The fixture's declared DAI answers: one row per detector record, keyed by the
# record's page and detector ordinal, and optionally scoped to one scenario.
_DAI_RECORD_RESPONSE_FIELDS: Final = frozenset(
    {"scenario", "page_ordinal", "detector_ordinal", "chair", "text"}
)
_DAI_RECORD_RESPONSE_REQUIRED_FIELDS: Final = _DAI_RECORD_RESPONSE_FIELDS - {"scenario"}
# The stop word a fixture-declared response is retained under: declared, never
# an engine's.
FIXTURE_COMPLETE_STOP: Final = "fixture-complete"


def _detector_ordinal(region: dict[str, Any]) -> int:
    """The detector ordinal a record crop's subject names."""
    _, separator, ordinal = region["subject_id"].rpartition("-detector-")
    if not separator or not ordinal.isdigit():
        raise SchemaRefusal(
            f"detector region {region.get('artifact_id')} does not name its detector ordinal"
        )
    return int(ordinal)


def declared_dai_record_text(context, chair: str, page_ordinal: int, detector_ordinal: int) -> str:
    """The fixture's declared DAI answer for one record; one row, or a refusal."""
    rows = _scenario_rows(
        context,
        (
            row
            for row in context.fixture.get("dai_record_response", [])
            if isinstance(row, dict)
            and row.get("chair") == chair
            and row.get("page_ordinal") == page_ordinal
            and row.get("detector_ordinal") == detector_ordinal
        ),
    )
    if len(rows) != 1:
        raise SchemaRefusal(
            f"the fixture declares {len(rows)} DAI answers for chair {chair!r}, page "
            f"{page_ordinal}, record {detector_ordinal}; a record DAI is shown needs exactly one"
        )
    row = rows[0]
    if set(row) - _DAI_RECORD_RESPONSE_FIELDS or _DAI_RECORD_RESPONSE_REQUIRED_FIELDS - set(row):
        raise SchemaRefusal(
            f"a fixture DAI answer declares fields {sorted(row)}, not "
            f"{sorted(_DAI_RECORD_RESPONSE_REQUIRED_FIELDS)} and an optional scenario"
        )
    if not isinstance(row["text"], str):
        raise SchemaRefusal("a fixture DAI answer's text is not text")
    return row["text"]


def fixture_detector_units(
    context,
    *,
    chair: str,
    resolved: ChairIdentity,
    page_ordinal: int,
    units: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any], Attempt]]:
    """Each record shown to DAI as a live pass shows it, answered by the fixture.

    The presentation, the closed model view and the retained capture are built
    exactly as for a served record; only the answer is declared.
    """
    adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
    capabilities = _declared_format_capabilities(adapter)
    prompt = adapter.prompt()
    served = []
    for region in units:
        source = presentation_for_region(region)
        presented = adapter.present(context, dict(source))
        witness_adapters.validate_adapter_presentation(resolved.witness_adapter, source, presented)
        text = declared_dai_record_text(context, chair, page_ordinal, _detector_ordinal(region))
        capture = adapter.retain(
            context,
            view=live_witness.dai_model_view(
                context,
                source,
                presented,
                prompt,
                feeding.dai_generation(),
                feeding.dai_generation_accounting(),
            ),
            raw_response=text.encode("utf-8"),
            transport_stop_reason=FIXTURE_COMPLETE_STOP,
            parser="text",
        )
        parsed = capture["parse"]
        if parsed["state"] == "parsed":
            attempt = Attempt(
                "genuinely-empty" if parsed["text"] == "" else "read",
                parsed["text"],
                None,
                capabilities,
                content_health(parsed["text"], completed=True),
                None,
                raw_response_ref=capture["raw_response_ref"],
                native_capture=capture,
                raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
            )
        else:
            reason = f"the provider response was retained but not usable: {parsed['reason']}"
            attempt = Attempt(
                "failed",
                None,
                None,
                capabilities,
                _unrecordable_health(parsed["reason"]),
                reason,
                raw_response_ref=capture["raw_response_ref"],
                native_capture=capture,
                raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
            )
        served.append((region, presented, attempt))
    return served


def _detector_page_reading(
    served: list[tuple[dict[str, Any], dict[str, Any], Attempt]],
) -> tuple[str, list[dict[str, Any]]]:
    """The page text joined in the detector's own order, and one observed box per unit."""
    text = ""
    spans: list[dict[str, int] | None] = [None] * len(served)
    for index, (_region, _presented, attempt) in enumerate(served):
        if attempt.outcome not in WITNESS_READING_OUTCOMES or not isinstance(
            attempt.native_payload, str
        ):
            continue
        if attempt.native_payload:
            text += "\n" if text else ""
            start = len(text)
            text += attempt.native_payload
        else:
            start = len(text)
        spans[index] = {"start": start, "end": len(text)}
    observed = [
        {
            "ordinal": index,
            "bounds": dict(region["payload"]["transform"]["bounds"]),
            # DAI reports no geometry: the box is the detector crop it was shown,
            # a presentation echo, never reported ink.
            "bounds_source": "presented",
            "span": spans[index],
        }
        for index, (region, _presented, _attempt) in enumerate(served)
    ]
    return text, observed


def _detector_page_outcome(
    served: list[tuple[dict[str, Any], dict[str, Any], Attempt]], text: str
) -> tuple[str, str | None, bool | None]:
    """The page's outcome, its reason, and whether every unit's response completed.

    One failed record fails the page: it goes under-witnessed for DAI, visibly,
    rather than read from part of what DAI was shown.
    """
    failed = [
        (index, attempt)
        for index, (_region, _presented, attempt) in enumerate(served)
        if attempt.outcome not in WITNESS_READING_OUTCOMES
    ]
    truncated = [attempt.health.get("truncated") for _r, _p, attempt in served]
    completed = False if True in truncated else None if None in truncated else True
    if failed:
        detail = "; ".join(f"record {index}: {attempt.reason}" for index, attempt in failed)
        return (
            "failed",
            f"DAI's reading of {len(failed)} of the {len(served)} records its detector found "
            f"on this page failed ({detail}); the page is not read from the rest",
            completed,
        )
    return ("genuinely-empty" if text == "" else "read"), None, completed


def publish_detector_page_testimonium(
    context,
    *,
    chair: str,
    resolved: ChairIdentity,
    page_ordinal: int,
    ordinal: int,
    served: list[tuple[dict[str, Any], dict[str, Any], Attempt]],
    receipt_ref: dict[str, str] | None,
    page_ids: dict[int, str],
    detection_count: int | None,
) -> None:
    """Seal one DAI page record over every unit it read.

    A page with no unit is DAI's blank testimony when its detector found no
    record below a stated cap: `genuinely-empty`, empty text, binding the
    detector's census. Otherwise it is sealed `not-run`; ``detection_count``
    is its census count, which says whether the detector found nothing or found
    records that enclosed no crop. A caller with served units passes `None`.
    """
    # First, so a chair the run did not seal page-scoped refuses before any record is built.
    page_chairs = declared_page_witness_chairs(context)
    if chair not in page_chairs:
        raise FatalAccounting(
            f"chair {chair!r} is not a page witness in the sealed roster; a page record "
            "for it would claim a scope the run never declared"
        )
    page_subject_id = page_subject(context, page_ordinal, page_ids=page_ids)
    page_attempt_id = attempt_id(page_subject_id, f"read:{chair}", ordinal)
    page_artifact_id = artifact_id(
        ATTESTATORES, "page-testimonium", page_subject_id, page_attempt_id
    )
    adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
    capabilities = _declared_format_capabilities(adapter)
    if not served:
        if detection_count is None:
            raise FatalAccounting(
                f"page {page_ordinal}'s record reader was served no unit, and no census count "
                "says whether its detector found nothing or found records that enclosed no crop"
            )
        census = (
            empty_detector_page(
                context, stage_manifest(context, DESIGNATOR)["artifacts"], page_subject_id
            )
            if detection_count == 0
            else None
        )
        if census is not None:
            attempt = Attempt(
                "genuinely-empty",
                "",
                None,
                capabilities,
                dict(BLANK_TESTIMONY_HEALTH),
                NO_DETECTOR_RECORD_REASON,
            )
        else:
            reason = no_detector_unit_reason(detection_count)
            attempt = Attempt(
                "not-run", None, None, capabilities, no_response_health(reason=reason), reason
            )
        payload = page_testimonium_payload(
            chair=chair,
            page_ordinal=page_ordinal,
            ordinal=ordinal,
            provenance=provenance_for(context, resolved, attempted=False),
            attempt=attempt,
            presented={},
            observed=[],
            testimonium_id=page_artifact_id,
        )
        inputs: list[dict[str, str]] = [] if census is None else [census]
    else:
        text, observed = _detector_page_reading(served)
        outcome, reason, completed = _detector_page_outcome(served, text)
        arrived = any(attempt.raw_response_ref is not None for _r, _p, attempt in served)
        reading = outcome in WITNESS_READING_OUTCOMES
        attempt = Attempt(
            outcome,
            text if reading or arrived else None,
            None,
            capabilities,
            (
                content_health(text, completed=completed)
                if reading or arrived
                else no_response_health(reason=reason)
            ),
            reason,
            receipt_ref=receipt_ref,
        )
        presentations = [presented for _region, presented, _attempt in served]
        raw_refs = _named_once(
            [a.raw_response_ref for _r, _p, a in served if a.raw_response_ref is not None]
        )
        unit_call_refs = [a.serving_call_ref for _r, _p, a in served]
        payload = page_testimonium_payload(
            chair=chair,
            page_ordinal=page_ordinal,
            ordinal=ordinal,
            provenance=provenance_for(context, resolved, attempted=True, receipt_ref=receipt_ref),
            attempt=attempt,
            presented=presentations[0],
            observed=observed,
            testimonium_id=page_artifact_id,
            raw_response_refs=raw_refs,
            presentations=presentations,
            unit_captures=[a.native_capture for _r, _p, a in served],
            unit_call_refs=unit_call_refs,
        )
        inputs = _named_once(
            [context.input_ref(presented["image_path"]) for presented in presentations]
            + raw_refs
            + [reference for reference in unit_call_refs if reference is not None]
        )
        verify_page_call_sampling(context, payload, chair)
    validate_testimonium_presentation(
        context, {"outcome": attempt.outcome, "payload": payload, "inputs": inputs}
    )
    context.publish(
        kind="page-testimonium",
        subject_id=page_subject_id,
        outcome=attempt.outcome,
        attempt=page_attempt_id,
        inputs=inputs,
        payload=payload,
    )


def _serve_detector_page(
    context,
    *,
    client: ChairClient,
    chair: str,
    resolved: ChairIdentity,
    adapter,
    page_ordinal: int,
    ordinal: int,
    units: list[dict[str, Any]],
    page_ids: dict[int, str],
) -> None:
    """One DAI page: one request per record, then the page record.

    Every response is retained as it arrives; the page record is sealed only
    once all its records are read, so a pass interrupted inside a page asks
    that page's records again.
    """
    served: list[tuple[dict[str, Any], dict[str, Any], Attempt]] = []
    for region in units:
        source = presentation_for_region(region)
        what = f"the {resolved.witness_adapter} request for record {region['subject_id']}"
        try:
            built = live_witness.record_chair_request(
                context, adapter, source, profile=client.handle.profile
            )
        except RequestCapacityRefusal as error:
            presented = adapter.present(context, dict(source))
            attempt = capacity_refusal_attempt(
                error, receipt_ref=client.handle.receipt_reference, what=what, adapter=adapter
            )
        else:
            response = client.read(built.request)
            live = live_witness.live_attempt_from_response(
                context,
                adapter,
                resolved.witness_adapter,
                response,
                presentation=source,
                presented=built.presented,
                prompt=built.prompt,
                generation_declared=built.request.generation_declared,
                parser="text",
                generation_accounting=built.generation_accounting,
            )
            _refuse_unpublishable_response(response, what.replace("request", "response"))
            presented = built.presented
            attempt = attempt_from_live(live)
        witness_adapters.validate_adapter_presentation(resolved.witness_adapter, source, presented)
        served.append((region, presented, attempt))
    publish_detector_page_testimonium(
        context,
        chair=chair,
        resolved=resolved,
        page_ordinal=page_ordinal,
        ordinal=ordinal,
        served=served,
        receipt_ref=dict(client.handle.receipt_reference),
        page_ids=page_ids,
        detection_count=None,
    )


def detector_pages_to_read(
    context,
    *,
    chair: str,
    pages: list[tuple[int, str]],
    ordinal: int,
    sealed: dict[tuple[int, str], dict[str, Any]],
    detector: tuple[dict[int, list[dict[str, Any]]], dict[int, int]],
    page_ids: dict[int, str],
) -> tuple[int, list[int]]:
    """Settle every page a record reader needs no request for; return the rest to read.

    A page record sealed by an interrupted pass is kept, never read again, and a
    page with no record crop is sealed without a request
    (`publish_detector_page_testimonium` says how). Returns how many records
    stand for the chair and the pages it must still be shown.
    """
    units_by_page, detections_by_page = detector
    resolved = context.registry.resolve(chair)
    recorded = 0
    to_read = []
    for page_ordinal, _page_id in pages:
        if page_ordinal not in units_by_page:
            raise FatalAccounting(
                f"page {page_ordinal} has no census from DAI's record detector; chair {chair!r} "
                "cannot be shown the page as it was trained to read it. Run the Designator "
                "with its secondary proposer configured"
            )
        if (page_ordinal, chair) in sealed:
            recorded += 1
        elif not units_by_page[page_ordinal]:
            publish_detector_page_testimonium(
                context,
                chair=chair,
                resolved=resolved,
                page_ordinal=page_ordinal,
                ordinal=ordinal,
                served=[],
                receipt_ref=None,
                page_ids=page_ids,
                detection_count=detections_by_page[page_ordinal],
            )
            recorded += 1
        else:
            to_read.append(page_ordinal)
    return recorded, to_read


def _retained_call(context, reference: Any, field: str) -> dict[str, Any]:
    validate_retained_response_blob(context.tree, reference, field)
    try:
        call = json.loads(context.tree.read_bytes(reference["relative_path"]))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SchemaRefusal(f"a page Testimonium's {field} record is not JSON") from error
    if not isinstance(call, dict):
        raise SchemaRefusal(f"a page Testimonium's {field} record is not an object")
    return call


def _served_live(context, payload: dict[str, Any]) -> bool:
    """Whether the record's receipt is a live serving moment, not the fixture's declaration."""
    receipt_ref = payload["provenance"].get("receipt_ref")
    if receipt_ref is None:
        return False
    endpoint = context.tree.read_run_receipt(dict(receipt_ref)).get("endpoint")
    return not (isinstance(endpoint, str) and endpoint.startswith("fixture://"))


def verify_page_call_sampling(context, payload: dict[str, Any], chair: str) -> None:
    """Hold every call a page record retains to its chair's sealed sampling row.

    DAI names one call per record it was shown; a whole-page chair names the one
    request it was sent, and a live record that retains a response must name it.
    A Chandra native page sends no seed and samples at its returned attempt's
    ordinal.
    """
    for reference in payload.get("unit_call_refs", []):
        if reference is None:
            continue
        call = _retained_call(context, reference, "unit call")
        try:
            verify_retained_call_sampling(context, call, chair)
        except ContractError as error:
            raise SchemaRefusal(
                f"a page Testimonium's unit call record is not its sealed request: {error}"
            ) from error
    if "unit_call_refs" in payload:
        return
    reference = payload.get("serving_call_ref")
    if reference is None:
        retains_response = payload.get("native_capture") is not None or bool(
            payload.get("raw_response_refs")
        )
        if retains_response and _served_live(context, payload):
            raise SchemaRefusal(
                f"chair {chair!r}'s live page Testimonium retains a response and names no "
                "serving call, so the request that produced it cannot be held to its sealed "
                "sampling row"
            )
        return
    call = _retained_call(context, reference, "serving call")
    inference = payload.get("native_inference")
    try:
        if inference is not None:
            verify_retained_call_sampling(
                context,
                call,
                chair,
                attempt_ordinal=inference["returned_attempt_ordinal"],
                sends_seed=False,
            )
        else:
            verify_retained_call_sampling(context, call, chair)
    except ContractError as error:
        raise SchemaRefusal(
            f"a page Testimonium's serving call record is not its sealed request: {error}"
        ) from error


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
        format_capabilities=_declared_format_capabilities(adapter),
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
    model_output_ref = retain_chair_bytes(context, response.content.encode("utf-8"))
    return Attempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=_declared_format_capabilities(adapter),
        health=no_response_health(reason=reason),
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
        or not _is_positive_int(payload["page_ordinal"])
        or not _is_positive_int(payload["witness_attempt_ordinal"])
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
    expected_inputs = _named_once(
        [
            {
                "relative_path": context.tree.blob_path(ATTESTATORES, digest),
                "sha256": digest,
            }
            for digest in payload["image_sha256s"]
        ]
        + [body_ref, payload["receipt_ref"]]
    )
    if record.get("inputs") != _sorted_refs(expected_inputs):
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
    if isinstance(call_record, dict):
        refuse_retired_call_record(
            call_record.get("schema"),
            subject="a Chandra native serving call record",
            error_type=SchemaRefusal,
        )
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
    if "inputs" in record and record["inputs"] != _sorted_refs(_named_once(expected_inputs)):
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


def _chandra_trace_inputs(trace: dict[str, Any] | None) -> list[dict[str, str]]:
    if trace is None:
        return []
    checked = validate_chandra_trace(trace)
    return [
        reference
        for row in checked["attempts"]
        for reference in (row["intent_ref"], row["attempt_ref"])
    ]


def _publish_chandra_intent(
    context,
    *,
    subject_id: str,
    page_ordinal: int,
    chair: str,
    witness_attempt_ordinal: int,
    native_attempt_ordinal: int,
    dispatch: Any,
    receipt_ref: Mapping[str, str],
) -> tuple[dict[str, str], bool]:
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
    published = context.publish(
        kind="chandra-native-attempt-intent",
        subject_id=subject_id,
        outcome="recorded",
        attempt=native_attempt,
        inputs=_named_once(image_refs + [request_body_ref, dict(receipt_ref)]),
        payload=payload,
    )
    return (
        _chandra_native_artifact_ref(
            context, "chandra-native-attempt-intent", subject_id, native_attempt_ordinal
        ),
        published.reused,
    )


def _publish_chandra_terminal(
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
) -> tuple[dict[str, Any], dict[str, str]]:
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
    _validate_chandra_terminal(
        context,
        subject_id=subject_id,
        native_attempt_ordinal=native_attempt_ordinal,
        record={"payload": payload},
    )
    context.publish(
        kind="chandra-native-attempt",
        subject_id=subject_id,
        outcome="recorded",
        attempt=native_attempt,
        inputs=_named_once(refs),
        payload=payload,
    )
    return payload, _chandra_native_artifact_ref(
        context, "chandra-native-attempt", subject_id, native_attempt_ordinal
    )


def _sealed_chandra_records(context, subject_id: str, kind: str, what: str):
    """Yield each retained `kind` record for the subject with its checked native ordinal."""
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != kind or entry["subject_id"] != subject_id:
            continue
        record = context.tree.read_artifact(ATTESTATORES, kind, entry["artifact_id"])
        payload = record.get("payload")
        ordinal = payload.get("native_attempt_ordinal") if isinstance(payload, dict) else None
        if not _is_positive_int(ordinal) or ordinal > CHANDRA_MAX_ATTEMPTS:
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
            _refuse_unpublishable_response(
                response, f"the {resolved.witness_adapter} response for page {page_ordinal}"
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
                attempt_from_live(live) if live is not None else None,
            )
            if application_refusal is not None
            else attempt_from_live(live)
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


def _serve_chandra_native_page(
    context,
    *,
    client: ChairClient,
    chair: str,
    resolved: ChairIdentity,
    adapter: Any,
    page_ordinal: int,
    witness_attempt_ordinal: int,
    request: Any,
    framing: str | None,
    page_subject_id: str,
) -> Attempt:
    """Run or resume the pinned vendor loop and return only its final result."""

    subject_id = _chandra_native_subject(page_subject_id, chair, witness_attempt_ordinal)
    intent_records = _sealed_chandra_intents(context, subject_id)
    terminal_records = _sealed_chandra_attempts(context, subject_id)
    resumed = _resumed_chandra_native_attempt(context, subject_id, intent_records, terminal_records)
    if resumed is not None:
        return resumed

    next_ordinal = len(terminal_records) + 1
    if terminal_records and terminal_records[-1]["payload"].get("trigger") == "inference-error":
        # A crash during the backoff cannot show how much elapsed, so the full
        # delay is repeated.
        _chandra_backoff(next_ordinal - 1)
    while next_ordinal <= CHANDRA_MAX_ATTEMPTS:
        dispatch = client.prepare_chandra_native(request, attempt_ordinal=next_ordinal)
        intent_ref, reused_intent = _publish_chandra_intent(
            context,
            subject_id=subject_id,
            page_ordinal=page_ordinal,
            chair=chair,
            witness_attempt_ordinal=witness_attempt_ordinal,
            native_attempt_ordinal=next_ordinal,
            dispatch=dispatch,
            receipt_ref=client.handle.receipt_reference,
        )
        if reused_intent:
            # No terminal exists, so the request may have reached vLLM before a
            # crash; reissuing could duplicate it. An operator must decide.
            refuse_chandra_orphan_intent()

        result = _read_chandra_native_result(
            context, client, dispatch, intent_ref, page_ordinal, chair, resolved, adapter, framing
        )
        trigger, returned_condition = _chandra_conditions(
            result.raw, result.inference_error, next_ordinal
        )
        payload, _terminal_ref = _publish_chandra_terminal(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=next_ordinal,
            intent_ref=intent_ref,
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
        terminal_records.append({"payload": payload})
        if trigger is None:
            if result.application_refusal is not None:
                raise result.application_refusal
            return _with_chandra_trace(context, subject_id, terminal_records, result.attempt)
        if trigger == "inference-error":
            _chandra_backoff(next_ordinal)
        next_ordinal += 1

    raise FatalAccounting("the Chandra native retry loop ended without a returned attempt")


def _serve_page_unit(
    context,
    *,
    client: ChairClient,
    chair: str,
    resolved: ChairIdentity,
    adapter,
    page_ordinal: int,
    ordinal: int,
    page_ids: dict[int, str],
    framing: str | None = None,
) -> None:
    """One whole-page chair, one page: one request, then its sealed page record."""
    presentation = presentation_for_page(context, page_ordinal, page_ids=page_ids)
    try:
        request = live_witness.page_chair_request(
            context,
            adapter,
            resolved.witness_adapter,
            presentation,
            # The generation bound derives from this row and the request's capacity.
            profile=client.handle.profile,
            framing=framing,
        )
    except RequestCapacityRefusal as error:
        # Only this page fails. No model view, since there was no response.
        attempt = capacity_refusal_attempt(
            error,
            receipt_ref=client.handle.receipt_reference,
            what=f"the {resolved.witness_adapter} request for page {page_ordinal}",
            adapter=adapter,
        )
    else:
        if resolved.role == "attestator_1" and resolved.witness_adapter == "chandra.v1":
            attempt = _serve_chandra_native_page(
                context,
                client=client,
                chair=chair,
                resolved=resolved,
                adapter=adapter,
                page_ordinal=page_ordinal,
                witness_attempt_ordinal=ordinal,
                request=request,
                framing=framing,
                page_subject_id=page_subject(context, page_ordinal, page_ids=page_ids),
            )
        else:
            response = client.read(request)
            live = live_witness.captured_page_attempt(
                context,
                page_ordinal,
                chair,
                resolved.witness_adapter,
                adapter,
                response,
                framing=framing,
            )
            _refuse_unpublishable_response(
                response, f"the {resolved.witness_adapter} response for page {page_ordinal}"
            )
            attempt = attempt_from_live(live)
    publish_page_testimonium(
        context,
        chair=chair,
        resolved=resolved,
        page_ordinal=page_ordinal,
        attempt=attempt,
        ordinal=ordinal,
        page_ids=page_ids,
        live=True,
    )


def refuse_unread_fixture_declarations(context, live_chairs: list[str]) -> None:
    """Disclose ignored fixture responses when live chairs read its corpus."""
    if real_ingress(context):
        return
    counted = {
        table: sum(
            1
            for row in context.fixture.get(table, [])
            if isinstance(row, dict)
            and row.get("chair") in live_chairs
            and row.get("scenario") in (None, context.scenario)
        )
        for table in (*PAGE_RESPONSE_TABLES, "dai_record_response", "native_observation")
    }
    declared = {table: count for table, count in counted.items() if count}
    if declared:
        print(
            "Attestatores live pass: the sealed fixture declares witness rows this posture does "
            f"not read {dict(sorted(declared.items()))}; every outcome below came from a chair "
            "that served it",
            file=sys.stderr,
        )


def _sealed_page_testimonia(context, ordinal: int) -> dict[tuple[int, str], dict[str, Any]]:
    """Every page Testimonium already sealed at this ordinal, by page and chair."""
    sealed: dict[tuple[int, str], dict[str, Any]] = {}
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != PAGE_TESTIMONIUM_KIND:
            continue
        record = context.tree.read_artifact(
            ATTESTATORES, PAGE_TESTIMONIUM_KIND, entry["artifact_id"]
        )
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("attempt_ordinal") != ordinal:
            continue
        page_ordinal, chair = payload.get("page_ordinal"), payload.get("chair")
        if isinstance(page_ordinal, int) and isinstance(chair, str):
            sealed[(page_ordinal, chair)] = record
    return sealed


def fixture_pass(
    context,
    pages: list[tuple[int, str]],
    ordinal: int,
    planned: dict[tuple[int, str], Attempt],
    *,
    page_ids: dict[int, str],
) -> int:
    """Publish the preflight-checked fixture answers, then each record reader's pages."""
    for (page_ordinal, chair), attempt in planned.items():
        publish_page_testimonium(
            context,
            chair=chair,
            resolved=context.registry.resolve(chair),
            page_ordinal=page_ordinal,
            attempt=attempt,
            ordinal=ordinal,
            page_ids=page_ids,
            live=False,
        )
    recorded = len(planned)
    detector_chairs = [
        chair
        for chair in page_witness_roster(context)
        if reads_detector_records(context.registry.resolve(chair))
    ]
    if not detector_chairs:
        return recorded
    detector = detector_units_by_page(context)
    sealed = _sealed_page_testimonia(context, ordinal)
    for chair in detector_chairs:
        resolved = context.registry.resolve(chair)
        settled, to_read = detector_pages_to_read(
            context,
            chair=chair,
            pages=pages,
            ordinal=ordinal,
            sealed=sealed,
            detector=detector,
            page_ids=page_ids,
        )
        recorded += settled
        for page_ordinal in to_read:
            publish_detector_page_testimonium(
                context,
                chair=chair,
                resolved=resolved,
                page_ordinal=page_ordinal,
                ordinal=ordinal,
                served=fixture_detector_units(
                    context,
                    chair=chair,
                    resolved=resolved,
                    page_ordinal=page_ordinal,
                    units=detector[0][page_ordinal],
                ),
                receipt_ref=None,
                page_ids=page_ids,
                detection_count=None,
            )
            recorded += 1
    return recorded


def live_pass(
    context,
    pages: list[tuple[int, str]],
    ordinal: int,
    *,
    page_ids: dict[int, str],
    serving_factory,
    tier: str,
) -> int:
    """Serve one resident chair at a time and seal each page record as it is read.

    An interruption leaves every received response sealed; a page record
    already sealed at this ordinal is kept and never asked again.
    """
    roster = page_witness_roster(context)
    detector_chairs = [
        chair for chair in roster if reads_detector_records(context.registry.resolve(chair))
    ]
    detector = detector_units_by_page(context) if detector_chairs else ({}, {})
    sealed = _sealed_page_testimonia(context, ordinal)
    recorded = 0
    to_read: dict[str, list[int]] = {}
    for chair in roster:
        if chair in detector_chairs:
            settled, to_read[chair] = detector_pages_to_read(
                context,
                chair=chair,
                pages=pages,
                ordinal=ordinal,
                sealed=sealed,
                detector=detector,
                page_ids=page_ids,
            )
        else:
            to_read[chair] = [
                page_ordinal for page_ordinal, _ in pages if (page_ordinal, chair) not in sealed
            ]
            settled = len(pages) - len(to_read[chair])
        recorded += settled

    # One schedule per chair keeps each page unit with its resident chair.
    units: dict[tuple[str, str], int] = {}
    schedule: list[dict[str, str]] = []
    for chair in sorted(to_read):
        rows = []
        for page_ordinal in to_read[chair]:
            unit_id = page_ids[page_ordinal]
            units[(chair, unit_id)] = page_ordinal
            rows.append({"unit_id": unit_id, "page_ordinal": page_ordinal})
        schedule.extend(feeding.stage_major_schedule(context.tree.run_id, rows, [chair]))
    # `None` for an adapter with a single framing.
    framings = {
        chair: witness_adapters.framing_for(context.registry.config, chair) for chair in roster
    }

    def serve(client: ChairClient, row: dict[str, str]) -> None:
        nonlocal recorded
        chair = row["chair"]
        resolved = context.registry.resolve(chair)
        adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
        page_ordinal = units[(chair, row["unit_id"])]
        if chair in detector_chairs:
            _serve_detector_page(
                context,
                client=client,
                chair=chair,
                resolved=resolved,
                adapter=adapter,
                page_ordinal=page_ordinal,
                ordinal=ordinal,
                units=detector[0][page_ordinal],
                page_ids=page_ids,
            )
        else:
            _serve_page_unit(
                context,
                client=client,
                chair=chair,
                resolved=resolved,
                adapter=adapter,
                page_ordinal=page_ordinal,
                ordinal=ordinal,
                page_ids=page_ids,
                framing=framings[chair],
            )
        recorded += 1

    def load(chair: str) -> ChairClient:
        client = serving_factory(context, context.registry.resolve(chair), tier)
        client.__enter__()
        return client

    def unload(chair: str, client: ChairClient) -> None:
        del chair
        client.__exit__(None, None, None)

    if schedule:
        try:
            feeding.execute_stage_major_schedule(
                schedule,
                residency=feeding.SingleChairResidency(load, unload),
                serve=serve,
            )
        except ServingError as error:
            # Reported as a refusal; everything that arrived is already sealed.
            raise ContractError(f"a live witness reading was refused: {error}") from error
    return recorded


def _finish_pass(context, pages: list[tuple[int, str]], recorded: int) -> int:
    if recorded == 0:
        raise ContractError("no page witness produced an outcome for any sealed page")
    # The inventory precedes its tally; the boundary is sealed only afterward.
    context.finish()
    tally = attempt_tally(context, pages=pages)
    if tally["hold"]:
        print(f"Attestatores attempt tally UNKNOWN: {tally['reason']}", file=sys.stderr)
    context.seal_boundary()
    context.finish()
    return EXIT_HELD if tally["hold"] else EXIT_COMPLETE


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    parser = stage_parser(DESCRIPTION)
    parser.add_argument(
        "--attempt-ordinal",
        type=_positive_ordinal,
        default=1,
        help="append this ordinal for every page/chair, or repeat the current one byte-identically",
    )
    args = parser.parse_args()
    context = open_stage_context(args, ATTESTATORES, registry_factory=registry_factory)
    real = real_ingress(context)
    # A witness reading is a model decode, so its decoding policy must be sealed.
    decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    if serving_factory is None:
        serving_factory = production_serving_factory(decoding_policy, decoding_sha256)
    witness_adapters.validate_runnable_adapter_bindings(context.registry.config)
    # Resolved first: the serving posture decides which pass runs.
    modes = witness_serving_modes(
        context, bound_serving_recipes(context, args.serving_recipes_config), args.placement_tier
    )
    if real:
        require_every_witness_served(modes)
    live_chairs = sorted(chair for chair, mode in modes.items() if mode == "live")
    page_ids = exemplar_page_ids(context)
    pages = sealed_pages(context, page_ids)
    try:
        history = page_history(context)
    except FatalAccounting:
        raise
    except ContractError as error:
        print(f"Attestatores attempt tally UNKNOWN: {error}", file=sys.stderr)
        return EXIT_HELD
    # A stored inventory or a stage seal means a pass finished writing, so its tally must
    # reconcile even if every Testimonium is gone. Records alone do not trigger
    # it: a crash before the inventory was written leaves nothing to contradict,
    # and the pass resumes at its ordinal, reusing what is sealed.
    stored_inventory = context.tree.resolve(context.tree.manifest_path(ATTESTATORES)).exists()
    has_stage_seal = any(
        entry["kind"] == "stage-seal"
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
    )
    if stored_inventory or has_stage_seal:
        # No page denominator here: this pass is what fills it.
        prior_tally = attempt_tally(context)
        if prior_tally["hold"]:
            print(f"Attestatores attempt tally UNKNOWN: {prior_tally['reason']}", file=sys.stderr)
            return EXIT_HELD
    ordinal = args.attempt_ordinal
    try:
        planned = preflight(context, pages, ordinal, history, fixture=not live_chairs)
    except ContractError as error:
        # A preflight refusal precedes writes; accounting imbalance remains fatal.
        if isinstance(error, FatalAccounting):
            raise
        print(f"Attestatores refused this pass: {error}", file=sys.stderr)
        return EXIT_HELD
    if live_chairs:
        refuse_unread_fixture_declarations(context, live_chairs)
        recorded = live_pass(
            context,
            pages,
            ordinal,
            page_ids=page_ids,
            serving_factory=serving_factory,
            tier=args.placement_tier,
        )
    else:
        recorded = fixture_pass(context, pages, ordinal, planned, page_ids=page_ids)
    return _finish_pass(context, pages, recorded)


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
