"""Attestatores: retain every witness attempt without changing its history.

``payload`` is the witness's native response, unshaped. ``witness_reported`` is
the witness's own confidence or status claim, never used for channel health,
which is computed here from the response and the transport boundary.

Attempts are append-only: a re-read gets a new ordinal, and consumers take the
newest contiguous ordinal as current. `--attempt-ordinal N` runs every chair on
every expected act at that ordinal, byte-identically resumable.
`--operation reread --act <id> --chair <role>` retries one chair's one act at
its next ordinal without re-reading ink nobody doubted.

    python pipeline/3_attestatores/run.py --run-root <dir> --run-id <id>
    python pipeline/3_attestatores/run.py ... --operation reread --act <id> --chair <role>
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Final, Mapping, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chandra  # noqa: E402
import feeding  # noqa: E402
import live_witness  # noqa: E402
import witness_adapters  # noqa: E402

from common.alignment import align_to_anchor, load_alignment_limits, markup_text_view  # noqa: E402
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
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.outcomes import page_attachment_basis  # noqa: E402
from common.contracts.serving import (  # noqa: E402
    CHANDRA_NATIVE_CALL_RECORD_FIELDS,
    CHANDRA_NATIVE_CALL_RECORD_SCHEMA,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHANDRA_NATIVE_TRANSPORT_FAILURE_RECORD_SCHEMA,
    RAW_RESPONSE_KINDS,
    RAW_RESPONSE_MODEL_OUTPUT,
    RAW_RESPONSE_TRANSPORT_BODY,
    STOP_REASON_UNREPORTED,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, PERLECTOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.imaging import dimensions  # noqa: E402
from common.native_witness import (  # noqa: E402
    PAGE_TESTIMONIUM_REQUIRED_FIELDS,
    REPORTED_BOUNDS_SOURCES,
    native_parse_refusal,
    partition_disagreement,
    reported_geometry_overlaps,
    split_page_edge_overshoots,
    unpresented_region_ids,
    validate_native_capture,
    validate_native_witness_geometry,
    validate_presented_page_binding,
    validate_unpresented_regions,
)
from common.native_witness import (
    validate_page_testimonium_payload as validate_shared_page_testimonium_payload,
)
from common.request_capacity import RequestCapacityRefusal  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    EXIT_COMPLETE,
    EXIT_HELD,
    WITNESS_READING_OUTCOMES,
    continuation_for,
    exemplar_page_ids,
    expected_acts,
    fixture_serving_details,
    is_real_ingress,
    latest_attempt,
    open_stage_context,
    run_stage,
    stage_manifest,
    stage_parser,
    validate_serving_provenance,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    ServingRecipes,
    load_serving_recipes,
)
from operations.serving.errors import (  # noqa: E402
    ChairResponseRefusal,
    ChairTransportFailure,
    ServingError,
)
from operations.serving.http import UrllibHttpTransport  # noqa: E402
from operations.serving.manager import (  # noqa: E402
    MECHANICS_QUALIFICATION_PURPOSE,
    ServingManager,
    StageContextReceiptPublisher,
)
from operations.serving.process import SubprocessLauncher  # noqa: E402
from operations.serving.residency import (  # noqa: E402
    POD_RESIDENCY_LOCK_PATH,
    FileResidencyLease,
)

# Self-assessments a witness may report. They are retained as testimony, never used
# to rank or choose a witness. `uncertain` and `unsure` are both admitted because
# real adapters emit both spellings.
WITNESS_CONFIDENCE_ORDINALS = frozenset({"certain", "high", "medium", "low", "uncertain", "unsure"})

DEFAULT_FORMAT_CAPABILITIES = {
    "can_express_uncertainty": False,
    "can_express_layout": False,
}


def _declared_format_capabilities(adapter: Any) -> dict[str, Any]:
    """What this adapter's own grammar can carry, validated, for a pre-send refusal.

    A request refused before sending still names a real adapter, and its declared
    expressiveness is recorded either way.
    """
    return witness_adapters.declared_format_capabilities(adapter)


def real_ingress(context) -> bool:
    """Whether this context opened a real submission, read off its run authority.

    Read from `run.json`, never `context.scenario`: the ingress record is inside the
    run's self-hash, so argv or a fixture scenario cannot switch the route.
    """
    return is_real_ingress(context.run)


def page_subject(context, page_ordinal: int, *, page_ids: dict[int, str] | None = None) -> str:
    """The Exemplar page one submitted ordinal names, on either ingress route.

    `page_ids` is an optional prebuilt `exemplar_page_ids(context)`; a caller visiting
    many pages passes it to avoid one inventory walk per lookup.
    """
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


# Checked by name because the shared parser gives `--operation` no `choices`; a
# mistyped reread would otherwise run the whole pass and exit 0.
OPERATIONS = frozenset({"initial", "reread"})

# A witness response is untrusted: deep nesting would raise an uncaught
# `RecursionError` in `_native_problem` and kill the whole run, not one attempt.
# Real output nests a few levels, so this is headroom.
_MAX_NATIVE_DEPTH = 64


def proposed_regions(context, act_id: str) -> list[dict]:
    """Every original Designator region the chair was actually shown.

    Later recovery regions are not substituted: a Testimonium binds to the exact
    pixels shown.
    """
    regions = []
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
            verify_exemplar_crop_lineage(context.tree, context.run, record)
            regions.append(record)
    proposed = [record for record in regions if record["payload"]["origin"] == "proposal"]
    if not proposed:
        raise ContractError(f"act {act_id} has no proposed region for a witness to read")
    return sorted(proposed, key=_region_ordinal)


def sealed_page_proposal_regions(context, page_ordinal: int) -> list[dict]:
    """Every sealed Designator proposal on one page, independent of act state.

    A held act's proposal is included: the Recensor re-derives the same denominator
    from the whole sealed set and must agree with it.
    """
    regions = []
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        payload = record.get("payload", {})
        if (
            payload.get("origin") == "proposal"
            and payload.get("transform", {}).get("source_page_ordinal") == page_ordinal
        ):
            regions.append(record)
    return sorted(regions, key=_region_ordinal)


def _region_ordinal(record: dict) -> int:
    ordinal = record.get("payload", {}).get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal("a Designator region carries no integer attempt ordinal to order by")
    return ordinal


def region_references(regions: list[dict]) -> list[dict[str, str]]:
    """The public identity facts of the exact crops a chair saw."""
    return [
        {
            "region_id": record["payload"]["region_id"],
            "image_path": record["payload"]["image_path"],
            "image_sha256": record["payload"]["image_sha256"],
        }
        for record in regions
    ]


def region_inputs(context, regions: list[dict], presented: dict[str, Any]) -> list[dict[str, str]]:
    """Bind every proposal crop and the exact presentation, each distinct blob once."""
    inputs = {}
    for record in regions:
        reference = context.input_ref(record["payload"]["image_path"])
        inputs[reference["relative_path"]] = reference
    if presented:
        reference = context.input_ref(presented["image_path"])
        inputs[reference["relative_path"]] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


def testimonium_inputs(
    context, regions: list[dict], presented: dict[str, Any]
) -> list[dict[str, str]]:
    """Bind proposal crops and the exact adapter-owned image a witness saw."""
    inputs = {
        (reference["relative_path"], reference["sha256"]): reference
        for reference in region_inputs(context, regions, {})
    }
    if presented:
        reference = context.input_ref(presented["image_path"])
        inputs[(reference["relative_path"], reference["sha256"])] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


REGION_PRESENTATION_FIELDS: Final = ("region_id", "image_path", "image_sha256")
REGION_TRANSFORM_FIELDS: Final = ("source_page_id", "source_page_ordinal")


def presentation_for_region(region: dict[str, Any]) -> dict[str, Any]:
    """Derive one region presentation from a sealed proposal record.

    The validator also calls this on untrusted records, so missing fields are
    named rather than raised as a bare KeyError.
    """
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


def page_witness_attempted(
    page_acts: list[dict[str, Any]],
    chair: str,
    attempts_by_pair: dict[tuple[str, str], "Attempt"],
) -> bool:
    """Whether this page-scoped chair was shown pixels for at least one of its acts.

    Not `page_join`'s `reading`: a failed response was still shown an image, and
    must not be recorded as never shown (principle 2).
    """
    return any(
        attempts_by_pair[(act["act_id"], chair)].outcome in ATTEMPTED_WITNESS_OUTCOMES
        for act in page_acts
    )


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
    page_bytes = _verified_page_bytes(context, page)
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


#: Adapters whose fixture rows may declare `raw_response` bytes; `resolve_attempt`
#: refuses them for any other adapter, since fixture bytes may not be attributed to
#: a model that never produced them.
FIXTURE_NATIVE_RESPONSE_ADAPTERS: Final = frozenset({"chandra.v1"})


def _derives_partition_from_response(resolved: Any, page_captures: Any) -> bool:
    """Whether this chair's page partition is re-derived from its own response bytes.

    True for a geometry-reporting (`takes_page_size`) adapter on the live path, and
    in fixture runs only for adapters allowed fixture response bytes; the others
    keep the declared-observation route.
    """
    if not isinstance(resolved, ChairIdentity):
        return False
    # Unguarded on purpose: an adapter with no runnable binding must be refused,
    # not routed down the no-geometry branch.
    adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
    if not adapter.takes_page_size:
        return False
    return page_captures is not None or (
        resolved.witness_adapter in FIXTURE_NATIVE_RESPONSE_ADAPTERS
    )


def _partition_geometry(observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reported boxes a page partition may be derived from, or a named refusal.

    All reported: returned unchanged. None reported: empty; the page publisher
    substitutes the presentation echo. A mix is refused, not filtered: half a
    record would look like a complete partition (principle 2). An echo is excluded
    because it restates the image shown, not what the witness reported.
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
    """Return a page witness's surviving boxes and response-linked page-edge findings.

    `page_size` is the sealed source page, never a presentation crop: a block may
    cross the crop edge and still be valid page geometry. A finding without its raw
    response reference cannot be audited, so it is refused before publication.
    """
    survivors, overshoots = split_page_edge_overshoots(observed, page_size=page_size)
    if overshoots and (
        not isinstance(raw_response_ref, dict)
        or not isinstance(raw_response_ref.get("sha256"), str)
        or len(raw_response_ref["sha256"]) != 64
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
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    page_bytes = _verified_page_bytes(context, page)
    return page, page_bytes, dimensions(page_bytes)


def _verified_page_bytes(context, page: dict[str, Any]) -> bytes:
    """Read once and bind the exact page bytes that image operations will use.

    Digesting the same bytes object that is decoded closes the check/use gap a
    second read of the blob would open.
    """
    payload = page.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a sealed Exemplar page has no object payload")
    image_path = payload.get("image_path")
    expected_digest = payload.get("source_sha256")
    if not isinstance(image_path, str) or not image_path:
        raise SchemaRefusal("a sealed Exemplar page has no image path")
    try:
        page_bytes = context.tree.read_bytes(image_path)
    except OSError as error:
        raise SchemaRefusal(f"sealed Exemplar page bytes could not be read: {error}") from error
    actual_digest = digest_bytes(page_bytes)
    if actual_digest != expected_digest:
        raise SchemaRefusal(
            "sealed Exemplar page bytes changed between artifact verification and image use: "
            f"digest {actual_digest}, not {expected_digest}"
        )
    return page_bytes


def validate_testimonium_presentation(context, record: dict[str, Any]) -> None:
    """Re-derive the presentation's sealed page, blob binding, and region wall."""
    payload = record["payload"]
    presented = payload["presented"]
    validate_native_witness_geometry(payload)
    if presented == {}:
        if record.get("inputs") != []:
            raise SchemaRefusal("an unpresented Testimonium carries image inputs")
        return
    page, page_bytes, page_size = _sealed_source_page(context, presented)
    validate_native_witness_geometry(payload, page_size=page_size)
    validate_presented_page_binding(
        presented,
        page_ordinal=page["payload"]["ordinal"],
        page_image_path=page["payload"]["image_path"],
        page_sha256=page["payload"]["source_sha256"],
        page_size=page_size,
        page_bytes=page_bytes,
    )
    if not any(
        item == {"relative_path": presented["image_path"], "sha256": presented["image_sha256"]}
        for item in record.get("inputs", [])
    ):
        raise SchemaRefusal("a Testimonium presented image is not digest-bound in record.inputs")
    if presented["kind"] == "region":
        matches = []
        for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
            if entry["kind"] != "region":
                continue
            region = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            if region.get("payload", {}).get("region_id") == presented["region_ref"]["region_id"]:
                matches.append(region)
        if len(matches) != 1:
            raise SchemaRefusal("a region presentation names no unique sealed Designator region")
        region = matches[0]
        if region["payload"].get("origin") != "proposal":
            raise SchemaRefusal(
                "a recovery region cannot be presented as a witness basis; origin is not proposal"
            )
        if presentation_for_region(region) != presented:
            raise SchemaRefusal(
                "a region presentation disagrees with its sealed proposal geometry or blob"
            )


def _declared_for_ordinal(row: dict[str, Any], ordinal: int) -> bool:
    """Whether a fixture declaration belongs to this immutable attempt.

    A row without an ordinal means attempt one only, so a declared failure does not
    repeat on every re-read.
    """
    declared = row.get("attempt_ordinal", 1)
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 1:
        raise SchemaRefusal("a fixture witness declaration has no positive attempt ordinal")
    return declared == ordinal


def _declared_pairs(context, ordinal: int, fixture_key: str) -> set[tuple[str, str]]:
    """The (act, chair) pairs one fixture table declares for this exact attempt."""
    pairs = set()
    for row_number, row in enumerate(context.fixture.get(fixture_key, []), start=1):
        scenario = row.get("scenario")
        if not isinstance(scenario, str) or not scenario:
            raise SchemaRefusal(
                f"fixture [[{fixture_key}]] row {row_number} has no scenario: {row!r}"
            )
        if scenario == context.scenario and _declared_for_ordinal(row, ordinal):
            pair = (row["act_key"], row["chair"])
            if pair in pairs:
                raise SchemaRefusal(
                    f"fixture [[{fixture_key}]] declares {pair!r} twice for attempt ordinal "
                    f"{ordinal}; a repeated declaration is a copy-paste error or two answers "
                    "to one question, and neither may collapse silently into one"
                )
            pairs.add(pair)
    return pairs


def declared_malformed(context, ordinal: int) -> dict[tuple[str, str], str]:
    """Fixture stand-in for a provider response the recording channel could not keep."""
    rows: dict[tuple[str, str], str] = {}
    fixture_key = "witness_malformed"
    for row_number, row in enumerate(context.fixture.get(fixture_key, []), start=1):
        scenario = row.get("scenario")
        if not isinstance(scenario, str) or not scenario:
            raise SchemaRefusal(
                f"fixture [[{fixture_key}]] row {row_number} has no scenario: {row!r}"
            )
        if scenario != context.scenario or not _declared_for_ordinal(row, ordinal):
            continue
        key = (row["act_key"], row["chair"])
        if key in rows:
            raise SchemaRefusal(f"fixture declares malformed witness output twice for {key!r}")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal("a malformed witness declaration has no reason")
        rows[key] = reason
    return rows


def testimony_for(context, act_key: str, chair: str, ordinal: int) -> dict[str, Any] | None:
    """Return the fixture's response for this exact attempt.

    A scenario-specific declaration overrides a scenario-agnostic declaration.
    """
    base_matches = []
    scenario_matches = []
    for row in context.fixture["testimony"]:
        if row["act_key"] != act_key or row["chair"] != chair:
            continue
        if not _declared_for_ordinal(row, ordinal):
            continue
        declared_scenario = row.get("scenario")
        if declared_scenario is None:
            base_matches.append(row)
        elif declared_scenario == context.scenario:
            scenario_matches.append(row)
    matches = scenario_matches or base_matches
    if len(matches) > 1:
        raise SchemaRefusal(f"fixture declares more than one response for {(act_key, chair)!r}")
    return matches[0] if matches else None


def declared_response(
    context, act_key: str, chair: str, declarations: dict[str, Any]
) -> dict[str, Any] | None:
    """The one response the fixture declares this chair returned for this request.

    `[[testimony]]` and `[[witness_empty]]` declare responses, never outcomes; the
    outcome is derived from what is retained. A `witness_empty` row overrides the
    base response for its scenario, and one that collides with a scenario
    `[[testimony]]` row is refused here, since `declarations_for` cannot see it.
    """
    response = testimony_for(context, act_key, chair, declarations["ordinal"])
    if (act_key, chair) not in declarations["empty"]:
        return response
    if response is not None and response.get("scenario") == context.scenario:
        raise SchemaRefusal(
            "fixture declares both an empty response and a scenario response for "
            f"{(act_key, chair)!r} at attempt ordinal {declarations['ordinal']}"
        )
    # An empty Chandra response can still carry layout blocks, so keep the declared
    # raw response; a box invented from the shown page would claim ink never located.
    matching_empty_rows = [
        row
        for row in context.fixture.get("witness_empty", [])
        if row.get("scenario") == context.scenario
        and row.get("act_key") == act_key
        and row.get("chair") == chair
        and _declared_for_ordinal(row, declarations["ordinal"])
    ]
    if len(matching_empty_rows) > 1:
        raise SchemaRefusal(
            f"fixture declares more than one empty response for {(act_key, chair)!r}"
        )
    empty_response = matching_empty_rows[0] if matching_empty_rows else {}
    if "raw_response" in empty_response and not isinstance(empty_response["raw_response"], str):
        raise SchemaRefusal("fixture raw_response is not text encoding retained response bytes")
    return {
        "payload": "",
        **(
            {"raw_response": empty_response["raw_response"]}
            if isinstance(empty_response.get("raw_response"), str)
            else {}
        ),
    }


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


# Alignment loss for a `genuinely-empty` reading: no text, so nothing was lost.
_ZERO_ALIGNMENT_LOSS: dict[str, int] = {
    "markup_characters": 0,
    "whitespace_characters": 0,
    "unicode_reencoded_characters": 0,
}


def content_health(native_payload: Any, *, completed: bool | None = None) -> dict[str, Any]:
    """Compute deterministic channel facts from native output alone.

    ``witness_reported`` is deliberately not an input: a self-report never becomes
    health. ``completed`` must come from a trusted response boundary, or be None.
    """
    if (problem := _native_problem(native_payload)) is not None:
        return {
            "native_type": _native_type(native_payload),
            "encoding": "invalid-or-unrecordable",
            "recordable": False,
            "empty": None,
            "blank": None,
            "truncated": None,
            "characters": None,
            "truncation_basis": problem,
        }

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
    resolved: ChairIdentity | AbsentChair,
    *,
    attempted: bool,
    receipt_ref: dict[str, str] | None = None,
) -> dict:
    """The exact configured identity and actual serving moment for one outcome.

    A live chair already published its receipt when serving started, so the live
    pass passes ``receipt_ref``. Without it, an attempted fixture chair gets a
    receipt that says `fixture://` (principle 8).
    """
    if receipt_ref is not None and not attempted:
        raise ContractError(
            "a witness attempt that was never made carries a serving receipt reference; "
            "a receipt names a serving moment, and there was none"
        )
    if isinstance(resolved, AbsentChair):
        if receipt_ref is not None:
            raise ContractError(
                f"chair {resolved.role!r} is absent and cannot carry a serving receipt"
            )
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            "adapter_revision": context.adapter_revision,
        }
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("witness resolution returned neither an identity nor an absence")
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


# `reason` is outcome-dependent; `payload` is the sole derived report layer.
TESTIMONIUM_FIELDS = frozenset(
    {
        "chair",
        "act_key",
        "attempt_ordinal",
        "regions",
        "provenance",
        "format_capabilities",
        "payload",
        "witness_reported",
        "content_health",
        "presented",
        "observed",
        "unpresented_regions",
    }
)
# `scope` and `page_ordinal` belong only to the page-scoped kind, so an act record
# cannot pose as one. `native_capture` and `serving_call_ref` are written only on
# the live path. `raw_response_kind` says whether `raw_response_ref` names the
# adapter's output or the whole transport body.
OPTIONAL_TESTIMONIUM_FIELDS = frozenset(
    {
        "adapter_metadata",
        "raw_response_ref",
        "raw_response_kind",
        "reason",
        "page_witness",
        "native_capture",
        "serving_call_ref",
        "native_inference",
    }
)

# A page Testimonium is a separate closed record; its ``page_role`` (primary,
# continuation, or both) keeps page two from duplicating page one anonymously.
PAGE_TESTIMONIUM_FIELDS = PAGE_TESTIMONIUM_REQUIRED_FIELDS


def testimonium_payload(
    *,
    chair: str,
    act_key: str,
    ordinal: int,
    regions: list[dict[str, str]],
    provenance: dict[str, Any],
    format_capabilities: dict[str, Any] | None,
    native_payload: Any,
    witness_reported: Any,
    health: dict[str, Any],
    presented: dict[str, Any] | None = None,
    observed: list[dict[str, Any]] | None = None,
    unpresented_regions: list[str] | None = None,
    outcome: str,
    reason: str | None = None,
    page_witness: bool = False,
    raw_response_ref: dict[str, str] | None = None,
    raw_response_kind: str | None = None,
    adapter_metadata: dict[str, Any] | None = None,
    native_capture: dict[str, Any] | None = None,
    serving_call_ref: dict[str, str] | None = None,
    native_inference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stage schema without letting a compatibility field define it."""
    record: dict[str, Any] = {
        "chair": chair,
        "act_key": act_key,
        "attempt_ordinal": ordinal,
        "regions": regions,
        "provenance": provenance,
        "format_capabilities": format_capabilities,
        "payload": native_payload,
        "witness_reported": witness_reported,
        "content_health": health,
        "presented": {} if presented is None else presented,
        "observed": [] if observed is None else observed,
        "unpresented_regions": [] if unpresented_regions is None else unpresented_regions,
    }
    if reason is not None:
        record["reason"] = reason
    if page_witness:
        # Set before validation: the geometry contract must know this act view
        # restates a page witness's page-space geometry (see validate_observed).
        record["page_witness"] = True
    if raw_response_ref is not None:
        record["raw_response_ref"] = raw_response_ref
    if raw_response_kind is not None:
        record["raw_response_kind"] = raw_response_kind
    if adapter_metadata is not None:
        record["adapter_metadata"] = adapter_metadata
    if native_capture is not None:
        # The response bytes stay in the blob the capture names.
        record["native_capture"] = native_capture
    if serving_call_ref is not None:
        record["serving_call_ref"] = serving_call_ref
    if native_inference is not None:
        record["native_inference"] = native_inference

    return validate_testimonium_payload(record)


def declared_adapter_metadata(
    resolved: ChairIdentity | AbsentChair, *, has_raw_response: bool
) -> dict[str, str] | None:
    """Declare only this occupant's conversion rule and only beside raw bytes."""
    if not has_raw_response or not isinstance(resolved, ChairIdentity):
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
        or not isinstance(reference["sha256"], str)
        or len(reference["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in reference["sha256"])
        or reference["relative_path"] != prefix + reference["sha256"]
    ):
        raise SchemaRefusal(f"a Testimonium {field} is not an Attestatores blob reference")
    return reference


def validate_raw_response_ref(reference: Any) -> dict[str, str]:
    """Close one retained-response reference to this stage's own blob store."""
    return validate_stage_blob_ref(reference, "raw_response_ref")


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
    provenance = payload.get("provenance")
    identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
    adapter_name = identity.get("witness_adapter") if isinstance(identity, dict) else None
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
    has_references = (
        bool(payload.get("raw_response_refs"))
        if "raw_response_refs" in payload
        else "raw_response_ref" in payload
    )
    if "adapter_metadata" in payload and not has_references:
        raise SchemaRefusal("a Testimonium declares adapter metadata without a retained response")
    provenance = payload.get("provenance")
    identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
    adapter_name = identity.get("witness_adapter") if isinstance(identity, dict) else None
    if isinstance(adapter_name, str):
        quantization = witness_adapters.resolve_runnable_adapter(adapter_name).quantization
        if has_references and quantization is not None and "adapter_metadata" not in payload:
            raise SchemaRefusal(
                "a Testimonium retained a quantized adapter response without naming its rule"
            )


def validate_live_serving_fields(payload: dict[str, Any]) -> None:
    """Close the fields only a live reading writes onto an act Testimonium.

    `native_capture` must name the same blob as the record. `serving_call_ref`
    needs a retained response beside it, except for a Chandra error trace. A live
    record that retains a response must say whether it is model output or a
    transport body, and a capture can only describe model output.
    """
    kind = payload.get("raw_response_kind")
    if kind is not None:
        if kind not in RAW_RESPONSE_KINDS:
            raise SchemaRefusal(
                f"a Testimonium names raw response kind {kind!r}, which is not one of "
                f"{sorted(RAW_RESPONSE_KINDS)}"
            )
        if "raw_response_ref" not in payload:
            raise SchemaRefusal(
                "a Testimonium says what kind of response bytes it holds while retaining none"
            )
    if "serving_call_ref" in payload:
        validate_stage_blob_ref(payload["serving_call_ref"], "serving_call_ref")
        if "raw_response_ref" not in payload:
            trace = payload.get("native_inference")
            if not (
                isinstance(trace, dict)
                and validate_chandra_trace(trace)["attempts"][-1]["error"] is True
                and payload.get("payload") is None
            ):
                raise SchemaRefusal(
                    "a Testimonium names the serving call that produced it but retains no "
                    "response outside a Chandra native inference error trace"
                )
        if kind is None:
            if "raw_response_ref" in payload:
                raise SchemaRefusal(
                    "a live Testimonium retains a response without saying which kind of bytes "
                    "it is; model output and a transport body are not interchangeable"
                )
    if "native_capture" not in payload:
        return
    capture = validate_native_capture(payload["native_capture"])
    if payload.get("raw_response_ref") != capture["raw_response_ref"]:
        raise SchemaRefusal(
            "a Testimonium's retained model view names a different response blob than the "
            "record itself; one attempt reads one response"
        )
    if kind is not None and kind != RAW_RESPONSE_MODEL_OUTPUT:
        raise SchemaRefusal(
            f"a Testimonium carries an adapter's retained model view over bytes it calls "
            f"{kind!r}; a capture describes the model's own output and nothing else"
        )


def validate_retained_response_blob(
    tree: Any, reference: Any, field: str = "raw_response_ref"
) -> None:
    """Re-read one retained blob so a missing or changed one cannot pass a tally.

    ``field`` names the reference: a live record carries both the response and the
    call-record blob, and both are re-hashed.
    """
    checked = validate_stage_blob_ref(reference, field)
    try:
        data = tree.read_bytes(checked["relative_path"])
    except OSError as error:
        raise SchemaRefusal(
            f"retained witness {field} {checked['relative_path']} could not be read: {error}"
        ) from error
    if digest_bytes(data) != checked["sha256"]:
        raise SchemaRefusal(
            f"retained witness {field} {checked['relative_path']} differs from its digest"
        )


def validate_testimonium_payload(payload: Any) -> dict[str, Any]:
    """Close the act Testimonium at both its writer and its tally read-back."""
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium is not its closed payload schema")
    if missing := sorted(TESTIMONIUM_FIELDS - set(payload)):
        raise SchemaRefusal(f"a Testimonium carries no required field(s) {missing}")
    allowed = TESTIMONIUM_FIELDS | OPTIONAL_TESTIMONIUM_FIELDS
    if unexpected := sorted(set(payload) - allowed):
        raise SchemaRefusal(
            f"a Testimonium carries unknown field(s) {unexpected}; this stage writes a closed "
            "payload, and a field nothing validates is a field nothing downstream can trust"
        )
    validate_unpresented_regions(payload)
    if "raw_response_ref" in payload:
        validate_raw_response_ref(payload["raw_response_ref"])
    validate_live_serving_fields(payload)
    if "native_inference" in payload:
        validate_chandra_trace(payload["native_inference"])
        _require_chandra_native_testimonium_scope(payload, page_record=False)
    validate_adapter_metadata(payload)
    validate_retained_response_pairing(payload)
    # Checked again here because this validator is shared by the write path
    # (`prepared_response`) and by the tally and crash-resume read-back, which
    # must not republish a bad claim.
    if problem := _confidence_problem(payload.get("witness_reported")):
        raise SchemaRefusal(problem)
    return validate_native_witness_geometry(payload)


def page_testimonium_payload(
    *,
    page_ordinal: int,
    page_role: str,
    unjoined_act_attempts: list[dict[str, Any]],
    partition_disagreement: dict[str, Any] | None,
    testimonium_id: str,
    raw_response_refs: list[dict[str, str]] | None = None,
    adapter_metadata: dict[str, str] | None = None,
    native_capture: dict[str, Any] | None = None,
    native_inference: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Page-scoped Testimonia admit only the producer's closed field set."""
    writer_fields = {
        "chair",
        "act_key",
        "ordinal",
        "regions",
        "provenance",
        "format_capabilities",
        "native_payload",
        "witness_reported",
        "health",
        "presented",
        "observed",
        "unpresented_regions",
        "outcome",
        "reason",
    }
    if unknown := sorted(set(kwargs) - writer_fields):
        raise SchemaRefusal(
            f"a page Testimonium writer received unknown field(s) {unknown}; its closed "
            "payload cannot account for them; remove the fields before publication"
        )
    record = {
        **testimonium_payload(**kwargs),
        "scope": "page",
        "page_ordinal": page_ordinal,
        "page_role": page_role,
        "unjoined_act_attempts": unjoined_act_attempts,
    }
    if partition_disagreement is not None:
        record["partition_disagreement"] = partition_disagreement
    if raw_response_refs:
        record["raw_response_refs"] = raw_response_refs
    if adapter_metadata is not None:
        record["adapter_metadata"] = adapter_metadata
    if native_capture is not None:
        # Raw bytes remain in the named blob.
        record["native_capture"] = native_capture
    if native_inference is not None:
        record["native_inference"] = native_inference
    validate_page_testimonium_payload(record, testimonium_id=testimonium_id)
    # The tally read-back excludes page Testimonia, so their health closes here.
    validate_content_health(record["payload"], record["content_health"])
    if record["content_health"]["recordable"] is False:
        require_accounted_unrecordable_channel(
            {"outcome": kwargs["outcome"]}, {"reason": record.get("reason")}
        )
    return record


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
            _require_chandra_native_testimonium_scope(payload, page_record=True)
    return validate_shared_page_testimonium_payload(payload, testimonium_id=testimonium_id)


def _require_chandra_native_testimonium_scope(
    payload: dict[str, Any], *, page_record: bool
) -> None:
    """Keep the native retry capability on Chandra's one admitted chair/scope."""

    provenance = payload.get("provenance")
    identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
    if (
        payload.get("chair") != "attestator_1"
        or not isinstance(identity, dict)
        or identity.get("role") != "attestator_1"
        or identity.get("witness_adapter") != "chandra.v1"
        or identity.get("witness_scope") != "page"
        or (payload.get("scope") == "page") != page_record
        or (not page_record and payload.get("page_witness") is not True)
    ):
        raise SchemaRefusal(
            "native_inference belongs only to page-scoped attestator_1 with adapter chandra.v1"
        )


AttemptHistory = dict[tuple[str, str], list[dict[str, Any]]]


class AttemptIndex(NamedTuple):
    """This stage's own prior output, indexed once per invocation."""

    stage_has_artifacts: bool
    by_pair: AttemptHistory
    attachments_by_act: dict[str, list[dict[str, Any]]]


def _attempt_history(context) -> AttemptIndex:
    """Index immutable Testimonia and derived attachments once for this invocation.

    Serves only append/collision decisions, which would otherwise walk the manifest
    per pair and go quadratic; the tally validates independently.
    """
    manifest = context.tree.build_manifest(ATTESTATORES)
    by_pair: AttemptHistory = {}
    attachments_by_act: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest["artifacts"]:
        if entry["kind"] == "act-attachment":
            attachments_by_act.setdefault(entry["subject_id"], []).append(
                context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
            )
            continue
        if entry["kind"] != "testimonium":
            continue
        record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        payload = record.get("payload")
        # No skip on `payload["scope"]`: page records are already a separate kind,
        # and a self-reported scope must not drop an act record from the history.
        chair = payload.get("chair") if isinstance(payload, dict) else None
        if isinstance(chair, str):
            by_pair.setdefault((entry["subject_id"], chair), []).append(record)
    return AttemptIndex(bool(manifest["artifacts"]), by_pair, attachments_by_act)


def require_appendable_ordinal(
    history: AttemptHistory, act_id: str, chair: str, ordinal: int
) -> None:
    """Allow only a rerun of an attempt that exists, or exactly the next one.

    Any ordinal up to the current one is a resume (ordinals are contiguous;
    `latest_attempt` refuses a gap), which the RunTree refuses if the bytes differ. Lower ordinals are admitted because a reread moves one chair
    ahead, and the orchestrator's ordinal-1 pass must remain a no-op resume.
    """
    records = history.get((act_id, chair), [])
    if not records:
        if ordinal != 1:
            raise SchemaRefusal(
                f"Testimonium for {(act_id, chair)!r} has no attempt 1; cannot append ordinal "
                f"{ordinal} across a missing history"
            )
        return
    current = latest_attempt(
        records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
    )
    current_ordinal = current["payload"]["attempt_ordinal"]
    if ordinal > current_ordinal + 1:
        raise SchemaRefusal(
            f"Testimonium for {(act_id, chair)!r} is current at ordinal {current_ordinal}; "
            f"ordinal {ordinal} is neither a rerun of an attempt it holds nor its next "
            "append-only attempt"
        )


def _refuse_write_collision(
    history: AttemptHistory,
    act: dict[str, Any],
    chair: str,
    ordinal: int,
    attempt: "Attempt",
) -> None:
    """Refuse before any Testimonium write if this pass would seal different
    bytes than an attempt already recorded at this exact identity.

    A reread and a whole pass can reach one identity with different outcomes (an
    undeclared response is `failed` under one and `not-run` under the other). The
    RunTree would refuse only mid-pass, after earlier pairs were published, so
    every pair is checked first. Compared on the fields the two write paths can
    disagree on; provenance does not vary with `reread`. Raw response blobs
    retained before this refusal stay in custody.
    """
    existing = [
        record
        for record in history.get((act["act_id"], chair), [])
        if record["payload"]["attempt_ordinal"] == ordinal
    ]
    if not existing:
        return
    (record,) = existing
    payload = record["payload"]
    if (
        record["outcome"] != attempt.outcome
        or payload.get("payload") != attempt.native_payload
        or payload.get("witness_reported") != attempt.witness_reported
        or payload.get("format_capabilities") != attempt.format_capabilities
        or payload.get("content_health") != attempt.health
        or payload.get("reason") != attempt.reason
        # Two Chandra bodies can parse to the same text with different layout
        # blocks; the raw digest binds the geometry.
        or payload.get("raw_response_ref") != attempt.raw_response_ref
    ):
        raise SchemaRefusal(
            f"a whole pass at ordinal {ordinal} would record a different attempt for "
            f"{(act['act_key'], chair)!r} than the one already sealed there: sealed outcome "
            f"{record['outcome']!r}, this pass would write {attempt.outcome!r}. No Testimonium "
            "was written for this pass; any raw response custody retained before this refusal "
            "remains visible in the blob inventory"
        )


def pass_would_append(history: AttemptHistory, act_id: str, chairs, ordinal: int) -> bool:
    """Would a whole pass at this ordinal add an attempt to this act, or repeat one?

    The witness-layer closing rules apply to an append, not a repeat.
    """
    for chair in chairs:
        records = history.get((act_id, chair), [])
        if not records:
            return True
        current = latest_attempt(
            records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
        )
        if ordinal > current["payload"]["attempt_ordinal"]:
            return True
    return False


def require_shared_whole_pass_ordinal(
    index: "AttemptIndex", act: dict[str, Any], chairs, ordinal: int
) -> None:
    """Refuse an appending whole pass on an act a targeted reread has moved.

    A reread already took the act's next attachment ordinal, and the RunTree would
    refuse only after the pass's Testimonia were written, leaving attempts the
    manifest does not name. An act is off the shared ordinal when its chairs
    disagree on their current ordinal; pairs with no record are ignored.

    Not caught here: rereading every chair to the same ordinal makes the pass a
    repeat, and the attachment write then fails loudly in the RunTree
    (`RunTree.write_manifest` recovers). Catching it would cost a second
    derivation of every attachment.
    """
    current: dict[str, int] = {}
    for chair in chairs:
        records = index.by_pair.get((act["act_id"], chair), [])
        if not records:
            continue
        current[chair] = latest_attempt(
            records, f"Testimonium for {(act['act_id'], chair)!r}", operation=f"read:{chair}"
        )["payload"]["attempt_ordinal"]
    if len(set(current.values())) <= 1:
        return
    raise SchemaRefusal(
        f"act {act['act_id']} ({act['act_key']}) carries chairs at different current "
        f"ordinals {dict(sorted(current.items()))}: that act was reread, which takes it off "
        f"the shared whole-pass ordinal. A whole pass at ordinal {ordinal} would re-derive "
        "its act-attachment over the one the reread already sealed. Nothing was written "
        "for this pass"
    )


def preflight_appendable_ordinals(
    context,
    acts: list[dict[str, Any]],
    ordinal: int,
    declarations: dict[str, Any],
    index: "AttemptIndex",
    *,
    resume_incomplete_pass: bool,
    resolve=None,
    fixture_declared: bool = True,
) -> tuple[
    dict[str, tuple[list[dict], str | None]],
    dict[tuple[str, str], "Attempt"],
    frozenset[tuple[str, str]],
]:
    """Refuse a damaged history, or a colliding write, before adding any new
    attempt to this invocation.

    The returned region map lets publication reuse the regions verified here.

    A resumed pass keeps one ordinal for every pair: an attachment describes one
    ordinal, and downstream stages refuse a reading ordinal that moves without a
    recrop (principles 2 and 4). With `resume_incomplete_pass`, a pair already
    sealed at this ordinal is reused rather than re-resolved, since a live chair
    cannot reproduce immutable bytes; a fixture pass over a completed boundary
    re-resolves and compares.

    `resolve` defaults to the fixture resolver; the live pass passes one that
    returns `PENDING_LIVE_ATTEMPT` so no model is called before anything is written.
    `fixture_declared` comes from `real_ingress` in `main`: a real submission has
    no fixture to validate.
    """
    resolve = resolve_attempt if resolve is None else resolve
    # Native declarations must refuse before compatibility records are published.
    if fixture_declared:
        validate_declared_churro_page_responses(context, declared_page_witness_chairs(context))
    regions_by_act: dict[str, tuple[list[dict], str | None]] = {}
    attempts_by_pair: dict[tuple[str, str], Attempt] = {}
    sealed_pairs: set[tuple[str, str]] = set()
    appending = [
        act
        for act in acts
        if pass_would_append(index.by_pair, act["act_id"], context.witness_chairs, ordinal)
    ]
    closed = witness_bound_reading_acts(context) if appending else frozenset()
    for act in appending:
        # An appending whole pass meets the same closed-layer rule as a reread.
        require_open_witness_layer(closed, act, f"a whole pass at ordinal {ordinal}")
    for act in acts:
        regions: list[dict] = []
        if act["outcome"] == "held":
            not_read: str | None = (
                "the Designator held this act; its incomplete proposal was not shown "
                "to any configured witness"
            )
        else:
            try:
                regions = proposed_regions(context, act["act_id"])
                not_read = None
            except ContractError as error:
                if isinstance(error, FatalAccounting):
                    raise
                not_read = f"the proposed region was refused before this chair ran: {error}"
        regions_by_act[act["act_id"]] = (regions, not_read)
        for chair in context.witness_chairs:
            require_appendable_ordinal(index.by_pair, act["act_id"], chair, ordinal)
            resolved = context.registry.resolve(chair)
            pair = (act["act_id"], chair)
            existing = [
                record
                for record in index.by_pair.get(pair, [])
                if record["payload"]["attempt_ordinal"] == ordinal
            ]
            if existing and resume_incomplete_pass:
                if len(existing) != 1:
                    raise FatalAccounting(
                        f"Testimonium for {pair!r} has {len(existing)} records at ordinal "
                        f"{ordinal}; a resume cannot choose one"
                    )
                record = existing[0]
                # Omitted when empty so the validator re-derives it and names the
                # missing proposal crop.
                validate_tallied_testimonium(
                    context, record, act, {act["act_id"]: regions} if regions else {}
                )
                attempt = _attempt_from_retained_testimonium(context.tree, record)
                sealed_pairs.add(pair)
            else:
                attempt = (
                    not_read_attempt(resolved, not_read)
                    if not_read is not None
                    else resolve(
                        context,
                        act,
                        chair,
                        resolved,
                        declarations,
                    )
                )
            attempts_by_pair[pair] = attempt
            if attempt is PENDING_LIVE_ATTEMPT:
                # A pending pair must have no sealed record: sealed pairs are
                # reused above, never asked again.
                if existing:
                    raise FatalAccounting(
                        f"the live preflight left {pair!r} unresolved while a Testimonium is "
                        f"already sealed at ordinal {ordinal}; a sealed pair is reused, never "
                        "asked again"
                    )
                continue
            _refuse_write_collision(index.by_pair, act, chair, ordinal, attempt)
    # Last, so `_refuse_write_collision` names the underlying chair conflict first.
    for act in appending:
        require_shared_whole_pass_ordinal(index, act, context.witness_chairs, ordinal)
    return regions_by_act, attempts_by_pair, frozenset(sealed_pairs)


def validate_tallied_testimonium(
    context,
    record: dict[str, Any],
    act: dict[str, Any],
    regions_by_act: dict[str, list[dict]],
) -> None:
    """Refuse a resealed Testimonium that this stage could not have produced.

    Checks structure only, never witness content. `regions_by_act` caches the
    verified regions per act across chairs.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium tally record has no object payload")
    validate_testimonium_payload(payload)
    if "raw_response_ref" in payload:
        validate_retained_response_blob(context.tree, payload["raw_response_ref"])
    if "serving_call_ref" in payload:
        # Live blobs are not envelope inputs, so the tally re-hashes them itself.
        validate_retained_response_blob(
            context.tree, payload["serving_call_ref"], "serving_call_ref"
        )
    validate_testimonium_presentation(context, record)
    chair = payload["chair"]
    if not isinstance(chair, str) or chair not in context.witness_chairs:
        raise SchemaRefusal("a Testimonium tally record names no configured chair")
    if payload["act_key"] != act["act_key"]:
        raise SchemaRefusal("a Testimonium tally record disagrees with its act key")
    ordinal = payload["attempt_ordinal"]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise SchemaRefusal("a Testimonium tally record has no positive attempt ordinal")
    if payload["format_capabilities"] is None:
        if record["outcome"] != "failed":
            raise SchemaRefusal("a non-failed Testimonium carries no format_capabilities record")
    else:
        format_capabilities_for({"format_capabilities": payload["format_capabilities"]})
    if problem := _native_problem(payload["witness_reported"], "witness_reported"):
        raise SchemaRefusal(problem)
    validate_content_health(payload["payload"], payload["content_health"])
    if record["outcome"] in {"failed", "dead", "not-run"}:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal(
                f"a {record['outcome']} Testimonium records no reason for its non-reading outcome"
            )
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    validate_serving_provenance(
        context,
        payload["provenance"],
        producer_stage=ATTESTATORES,
        require_receipt=attempted,
    )
    if attempted:
        if act["outcome"] != "proposed":
            raise SchemaRefusal("a Testimonium attempted a Designator-held act")
        regions = regions_by_act.get(act["act_id"])
        if regions is None:
            regions = proposed_regions(context, act["act_id"])
            regions_by_act[act["act_id"]] = regions
        identity = context.registry.config.chairs[chair]
        if isinstance(identity, ChairIdentity):
            witness_adapters.validate_adapter_presentation(
                identity.witness_adapter,
                presentation_for_region(regions[0]),
                payload["presented"],
            )
        expected_inputs = _named_once(
            testimonium_inputs(context, regions, payload["presented"])
            + _chandra_trace_inputs(payload.get("native_inference"))
        )
        if payload["regions"] != region_references(regions) or record["inputs"] != sorted(
            expected_inputs,
            key=lambda ref: (ref.get("relative_path", ""), ref.get("sha256", "")),
        ):
            raise SchemaRefusal(
                "a Testimonium tally record does not bind exactly the proposal regions and inputs"
            )
        # Re-derived so a record cannot understate which bound crops go unpresented.
        if payload["unpresented_regions"] != unpresented_region_ids(payload["presented"], regions):
            raise SchemaRefusal(
                "a Testimonium tally record does not name exactly the bound regions its "
                "presentation does not speak for"
            )
    elif payload["regions"] != []:
        raise SchemaRefusal("a non-attempted Testimonium tally record carries regions")
    elif payload["presented"] != {} or payload["observed"] != [] or record["inputs"] != []:
        raise SchemaRefusal("a non-attempted Testimonium carries image evidence or observations")
    if record["outcome"] == "dead" and payload["provenance"].get("chair_state") != "absent":
        raise SchemaRefusal("a dead Testimonium tally record does not retain an absent chair")
    if record["outcome"] == "not-run" and payload["provenance"].get("chair_state") != "configured":
        raise SchemaRefusal("a not-run Testimonium tally record does not retain a configured chair")


def require_accounted_unrecordable_channel(record: dict[str, Any], payload: dict[str, Any]) -> None:
    """Tell a witness whose output could not be kept from an evidence channel nobody can read.

    An unrecordable response is accounted as a `failed` attempt with a reason, so
    one bad witness does not hold the folder. Only a record claiming a reading it
    could not retain is refused, which makes the tally UNKNOWN.
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


def attempt_tally(
    tree,
    *,
    context=None,
    acts: list[dict[str, Any]] | None = None,
    chairs: list[str] | None = None,
) -> dict[str, Any]:
    """Rebuild and check the stage's attempt inventory.

    The stored manifest is checked against a fresh walk and the Testimonia; any
    damage or divergence makes the count UNKNOWN, and the caller must hold.

    `chairs` is optional because full pair coverage is a closing check: demanding
    it before a pass would block the pass that completes an interrupted one.
    """
    if chairs is not None and acts is None:
        raise SchemaRefusal("an attempt tally denominator names chairs but no expected acts")
    try:
        stored_path = tree.resolve(tree.manifest_path(ATTESTATORES))
        stored = json.loads(stored_path.read_bytes().decode("utf-8"))
        rebuilt = tree.build_manifest(ATTESTATORES)
    except FatalAccounting:
        raise
    except (ContractError, OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        # json recurses per nesting level, so a deeply nested manifest raises
        # RecursionError here; it must become UNKNOWN and hold, not a traceback.
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    if stored != rebuilt:
        return {
            "state": "UNKNOWN",
            "count": None,
            "hold": True,
            "reason": "the stored Attestatores manifest does not equal its rebuilt inventory",
        }

    # Filtered by kind, never by the self-reported `scope`, so an act record cannot
    # claim page scope and skip these checks.
    testimonia = [entry for entry in rebuilt["artifacts"] if entry["kind"] == "testimonium"]
    by_act = {act["act_id"]: act for act in acts or ()}
    try:
        by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
        regions_by_act: dict[str, list[dict]] = {}
        for entry in testimonia:
            record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise SchemaRefusal("a Testimonium carries no object payload")
            validate_testimonium_payload(payload)
            chair = payload.get("chair")
            if not isinstance(chair, str) or not chair:
                raise SchemaRefusal("a Testimonium carries no named chair")
            by_pair.setdefault((record["subject_id"], chair), []).append(record)
            health = record.get("payload", {}).get("content_health")
            validate_content_health(payload["payload"], health)
            if health["recordable"] is False:
                require_accounted_unrecordable_channel(record, payload)
            if context is not None:
                if acts is None:
                    raise SchemaRefusal(
                        "a contextual attempt tally has no expected-act denominator"
                    )
                act = by_act.get(record["subject_id"])
                if act is None:
                    raise SchemaRefusal("a Testimonium tally record names no expected act")
                validate_tallied_testimonium(context, record, act, regions_by_act)
        if chairs is not None:
            expected_pairs = {(act["act_id"], chair) for act in acts for chair in chairs}
            if set(by_pair) != expected_pairs:
                raise SchemaRefusal(
                    "the rebuilt Testimonium inventory does not account for every expected "
                    "act/chair pair"
                )
        for (act_id, chair), records in by_pair.items():
            latest_attempt(
                records,
                f"Testimonium tally for {(act_id, chair)!r}",
                operation=f"read:{chair}",
            )
    except ContractError as error:
        # `FatalAccounting` is a `ContractError` but means the partition is broken,
        # not that the count is unknown; it must never become a hold.
        if isinstance(error, FatalAccounting):
            raise
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    except OSError as error:
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    return {
        "state": "KNOWN",
        "count": sum(len(records) for records in by_pair.values()),
        "hold": False,
        "reason": None,
    }


def _positive_ordinal(value: str) -> int:
    try:
        ordinal = int(value)
    except ValueError as error:
        raise ValueError("attempt ordinal must be an integer") from error
    if ordinal < 1:
        raise ValueError("attempt ordinal must be positive")
    return ordinal


class Attempt(NamedTuple):
    """One chair's resolved outcome for one act on one attempt.

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


class _PendingLiveAttempt:
    """The live pass has not asked this chair for this pair yet.

    Not an `Attempt`, so an unreplaced sentinel fails loudly instead of being
    published.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "PENDING_LIVE_ATTEMPT"


PENDING_LIVE_ATTEMPT: Any = _PendingLiveAttempt()


def pending_live_attempt(context, act, chair, resolved, declarations) -> Any:
    """The live preflight's resolver: every unsealed pair is still unasked.

    Has `resolve_attempt`'s signature; the chair is asked later by the pass that
    publishes its answer.
    """
    del context, act, chair, declarations
    if isinstance(resolved, AbsentChair):
        # Pending would put an absent chair into the live schedule to be started.
        return dead_attempt(resolved)
    return PENDING_LIVE_ATTEMPT


def _attempt_from_retained_testimonium(tree, record: dict[str, Any]) -> Attempt:
    """Crash resume must rehydrate retained Chandra bytes for page geometry.

    The response still feeds the derived page record, so its blob must be present
    and digest-identical.
    """
    payload = record["payload"]
    raw_response_ref = payload.get("raw_response_ref")
    observation_payload = None
    # A live attempt carries geometry bytes only where its response parsed; the
    # resume must match, or the rebuilt page record differs from the sealed one.
    served_by_a_chair = payload.get("serving_call_ref") is not None
    # Gated on outcome, not `recordable`, which is also true for a parsed but
    # cut-off `failed` response that carried no geometry (principle 4).
    parsed_into_a_payload = _retains_chandra_observation_payload(record)
    if raw_response_ref is not None:
        validate_raw_response_ref(raw_response_ref)
        try:
            observation_payload = tree.read_bytes(raw_response_ref["relative_path"])
        except OSError as error:
            raise SchemaRefusal(
                "a resumed Testimonium's retained raw response could not be read: "
                f"{raw_response_ref['relative_path']}: {error}"
            ) from error
        observed = digest_bytes(observation_payload)
        if observed != raw_response_ref["sha256"]:
            raise SchemaRefusal(
                "a resumed Testimonium's retained raw response digest differs from its "
                f"reference: expected {raw_response_ref['sha256']}, read {observed}"
            )
        # Always digest-checked; only its use as geometry depends on the branch.
        if served_by_a_chair and not parsed_into_a_payload:
            observation_payload = None
    provenance = payload.get("provenance")
    return Attempt(
        outcome=record["outcome"],
        native_payload=payload["payload"],
        witness_reported=payload["witness_reported"],
        format_capabilities=payload["format_capabilities"],
        health=payload["content_health"],
        reason=payload.get("reason"),
        raw_response_ref=raw_response_ref,
        observation_payload=observation_payload,
        # Lets a resumed live pass rebuild the page record without re-asking.
        native_capture=payload.get("native_capture"),
        serving_call_ref=payload.get("serving_call_ref"),
        receipt_ref=provenance.get("receipt_ref") if isinstance(provenance, dict) else None,
        raw_response_kind=payload.get("raw_response_kind"),
        native_inference=payload.get("native_inference"),
    )


def _retains_chandra_observation_payload(record: Mapping[str, Any]) -> bool:
    """Whether an immutable page response originally carried parsed geometry bytes."""

    if record.get("outcome") in WITNESS_READING_OUTCOMES:
        return True
    payload = record.get("payload")
    trace = payload.get("native_inference") if isinstance(payload, dict) else None
    capture = payload.get("native_capture") if isinstance(payload, dict) else None
    return (
        isinstance(trace, dict)
        and validate_chandra_trace(trace)["exhausted_condition"] == "repeat-token"
        and isinstance(capture, dict)
        and validate_native_capture(capture)["parse"]["state"] == "parsed"
    )


def churro_page_capture(context, page_ordinal: int, chair: str) -> dict[str, Any] | None:
    """Return the page-keyed row; scenario scope overrides only the unscoped default."""
    base: list[dict[str, Any]] = []
    scoped: list[dict[str, Any]] = []
    for row in context.fixture.get("churro_page_response", []):
        if row.get("page_ordinal") != page_ordinal or row.get("chair") != chair:
            continue
        declared_scenario = row.get("scenario")
        if declared_scenario is None:
            base.append(row)
        elif declared_scenario == context.scenario:
            scoped.append(row)
    matches = scoped or base
    if len(matches) > 1:
        raise SchemaRefusal(
            f"fixture declares more than one Churro page response for {(page_ordinal, chair)!r}"
        )
    return matches[0] if matches else None


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
    """Refuse current-scenario rows no declared page-scoped chair can consume.

    Absent occupants remain valid roster facts, and rows for other scenarios are
    outside this pass.
    """
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
    context, page_ordinal: int, chair: str, adapter_name: str
) -> tuple[Attempt, dict[str, Any]] | None:
    """Capture before parsing one response; never repair or retry it."""
    row = churro_page_capture(context, page_ordinal, chair)
    if row is None:
        return None
    raw, stop = churro_page_response_bytes(row)
    if adapter_name != "churro.v1":
        raise SchemaRefusal(
            f"a Churro page response for chair {chair!r} reached adapter {adapter_name!r}; "
            "fixture bytes may not be attributed to a different model boundary"
        )
    adapter = witness_adapters.resolve_runnable_adapter(adapter_name)
    # The retain wrapper pins the adapter name; it takes no adapter argument.
    capture = adapter.retain(
        context.tree,
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
            {
                "native_type": "unrecordable",
                "encoding": "invalid-or-unrecordable",
                "recordable": False,
                "empty": None,
                "blank": None,
                "truncated": None,
                "characters": None,
                "truncation_basis": basis,
            },
            f"Churro response retained but not usable: {cut_note}{parse_refusal}",
        ),
        capture,
    )


def dead_attempt(resolved: AbsentChair) -> Attempt:
    """A chair the roster declares absent: unavailable before any attempt reached it."""
    return Attempt(
        outcome="dead",
        native_payload=None,
        witness_reported=None,
        format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
        health=no_response_health(reason="not-attempted"),
        reason=f"chair is explicitly absent: {resolved.reason}",
    )


def not_read_attempt(resolved: ChairIdentity | AbsentChair, reason: str) -> Attempt:
    """One chair on an act no witness was shown: unavailable, or not asked.

    An absent chair stays `dead`: holding the act does not make it merely unasked.
    """
    if isinstance(resolved, AbsentChair):
        return dead_attempt(resolved)
    return Attempt(
        outcome="not-run",
        native_payload=None,
        witness_reported=None,
        format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
        health=no_response_health(reason="not-attempted"),
        reason=reason,
    )


def declarations_for(context, ordinal: int) -> dict[str, Any]:
    """Every fixture declaration that applies to this exact attempt ordinal.

    Bound to the ordinal so a first-attempt failure cannot describe a reread.
    `empty` is a declared empty response, not an outcome.
    """
    declarations = {
        "ordinal": ordinal,
        "failures": _declared_pairs(context, ordinal, "witness_failure"),
        "empty": _declared_pairs(context, ordinal, "witness_empty"),
        "not_run": _declared_pairs(context, ordinal, "witness_not_run"),
        "malformed": declared_malformed(context, ordinal),
    }
    outcome_sets = {
        name: set(value) if isinstance(value, dict) else value
        for name, value in declarations.items()
        if name != "ordinal"
    }
    names = sorted(outcome_sets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if overlap := sorted(outcome_sets[left] & outcome_sets[right]):
                raise SchemaRefusal(
                    f"fixture declares conflicting witness outcomes {left!r} and {right!r} "
                    f"for {overlap!r} at attempt ordinal {ordinal}"
                )
    return declarations


def real_declarations(ordinal: int) -> dict[str, Any]:
    """The declaration set of a real submission: empty, in `declarations_for`'s shape.

    A real run has no fixture; its failures are what the transport returned.
    """
    return {
        "ordinal": ordinal,
        "failures": set(),
        "empty": set(),
        "not_run": set(),
        "malformed": {},
    }


def resolve_attempt(
    context,
    act: dict[str, Any],
    chair: str,
    resolved: ChairIdentity | AbsentChair,
    declarations: dict[str, Any],
    *,
    reread: bool = False,
) -> Attempt:
    """What one configured chair's attempt at one act came to.

    Every branch ends in one closed outcome. An undeclared response is `not-run`
    in a whole pass but `failed` under `reread`, where the invocation itself is
    the attempt.
    """
    if isinstance(resolved, AbsentChair):
        return dead_attempt(resolved)

    key = (act["act_key"], chair)
    native_payload: Any = None
    witness_reported: Any = None
    capabilities = DEFAULT_FORMAT_CAPABILITIES
    health = no_response_health(reason="not-attempted")
    reason: str | None = None

    if key in declarations["not_run"]:
        outcome = "not-run"
        reason = "fixture declares that this configured chair was never attempted"
    elif key in declarations["failures"]:
        outcome = "failed"
        health = no_response_health(reason="attempted-but-no-usable-response")
        reason = "the chair returned no usable response"
    elif key in declarations["malformed"]:
        outcome = "failed"
        health = {
            "native_type": "unrecordable",
            "encoding": "invalid-or-unrecordable",
            "recordable": False,
            "empty": None,
            "blank": None,
            "truncated": None,
            "characters": None,
            "truncation_basis": declarations["malformed"][key],
        }
        reason = (
            f"the provider response was refused without repair: {declarations['malformed'][key]}"
        )
    else:
        response = declared_response(context, act["act_key"], chair, declarations)
        if response is None and reread:
            outcome = "failed"
            health = no_response_health(reason="attempted-but-no-usable-response")
            reason = "the reread reached this chair and it returned no response"
        elif response is None:
            outcome = "not-run"
            reason = "no attempt was made for this configured chair"
        else:
            if (
                "raw_response" in response
                and resolved.witness_adapter not in FIXTURE_NATIVE_RESPONSE_ADAPTERS
            ):
                raise SchemaRefusal(
                    f"fixture raw_response has no native byte route for adapter "
                    f"{resolved.witness_adapter!r}"
                )
            if "raw_response" in response and not isinstance(response["raw_response"], str):
                raise SchemaRefusal(
                    "fixture raw_response is not text encoding retained response bytes"
                )
            if resolved.witness_adapter in FIXTURE_NATIVE_RESPONSE_ADAPTERS and isinstance(
                response.get("raw_response"), str
            ):
                # Geometry derives only from declared response bytes, never from
                # JSON synthesized from `payload`.
                raw_response = response["raw_response"].encode("utf-8")
                # This branch is Chandra's recipe; any other adapter's bytes would
                # be filed under Chandra's model boundary (principle 6).
                if resolved.witness_adapter != "chandra.v1":
                    raise SchemaRefusal(
                        f"fixture raw_response for adapter {resolved.witness_adapter!r} would be "
                        "retained through Chandra's recipe -- its own retained view, prompt and "
                        "parser -- and filed under Chandra's model boundary; write that "
                        "adapter's own fixture retain branch before adding it to "
                        "FIXTURE_NATIVE_RESPONSE_ADAPTERS"
                    )
                adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")
                retained = adapter.retain(
                    context.tree,
                    # The fixture's frozen prompt, not `adapter.prompt()`: this view
                    # is sealed into pinned fixture bytes, and the served prompt must
                    # be free to change without moving them.
                    view={"prompt": dict(chandra.FIXTURE_PROMPT)},
                    raw_response=raw_response,
                    transport_stop_reason="fixture-complete",
                    parser="json",
                )
                parsed = retained["parse"]
                native_payload = (
                    parsed["text"]
                    if parsed["state"] == "parsed"
                    else {"parse_outcome": parsed["outcome"]}
                )
                if parsed["state"] == "parsed" and response.get("payload") != native_payload:
                    raise SchemaRefusal(
                        "fixture Chandra raw response text differs from its declared payload"
                    )
                (
                    native_payload,
                    witness_reported,
                    capabilities,
                    health,
                    recording_problem,
                ) = prepared_response({**response, "payload": native_payload})
                # Health is kept as `prepared_response` computed it; recomputing it
                # from a `None` payload would erase an unrecordable channel
                # (principle 2).
                if parsed["state"] != "parsed":
                    outcome = "failed"
                    reason = f"the Chandra response shape was not recognized: {parsed['outcome']}"
                    if recording_problem is not None:
                        reason = f"{reason}; {recording_problem}"
                elif recording_problem is not None:
                    outcome = "failed"
                    reason = (
                        f"the provider response was refused without repair: {recording_problem}"
                    )
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
                    retained["raw_response_ref"],
                    raw_response,
                )
            (
                native_payload,
                witness_reported,
                capabilities,
                health,
                recording_problem,
            ) = prepared_response(response)
            if recording_problem is not None:
                outcome = "failed"
                reason = f"the provider response was refused without repair: {recording_problem}"
            elif isinstance(native_payload, str) and native_payload == "":
                # Only a retained, recordable empty response reaches here;
                # `genuinely-empty` is never asserted without one.
                outcome = "genuinely-empty"
            else:
                outcome = "read"

    return Attempt(outcome, native_payload, witness_reported, capabilities, health, reason)


def declared_page_witness_chairs(context) -> set[str]:
    """Read page-witness scope from the sealed model configuration.

    Scope comes from the sealed roster's `witness_scope`, never a fixture. Every
    write path uses this, since an immutable record with the wrong scope cannot be
    repaired downstream.
    """
    roster = context.witness_chairs
    # Exact `str`, not `isinstance`: a subclass could override hashing or
    # formatting used below.
    if (
        not isinstance(roster, list)
        or any(type(chair) is not str for chair in roster)
        or len(roster) != len(set(roster))
    ):
        raise SchemaRefusal(
            "the sealed witness roster is not a unique list of chair names. Page-witness scope "
            "cannot be derived from this run authority. Start a new run from the sealed models "
            "configuration; do not edit the existing run"
        )
    configured = context.registry.config.chairs
    unknown = set(roster) - set(configured)
    if unknown:
        raise SchemaRefusal(
            "the sealed witness roster names chair(s) absent from the current models "
            "configuration: "
            f"{sorted(unknown)} not in {sorted(configured)}. The run authority and current models "
            "configuration do not describe the same witness set. Reopen the run with its original "
            "models configuration or start a new run; do not edit sealed evidence"
        )
    return {
        chair
        for chair in roster
        if isinstance(configured[chair], ChairIdentity)
        and configured[chair].witness_scope == "page"
    }


def _line_geometry(act_anchor: dict[str, Any]) -> list[dict[str, dict[str, int]]]:
    """A fresh copy of the anchor's line rectangles, in the closed `line_geometry` shape."""
    return [
        {"bbox": {key: line["bbox"][key] for key in ("x", "y", "w", "h")}}
        for line in act_anchor["line_geometry"]
    ]


def derived_chandra_anchor(
    *,
    page_text: str,
    observed: list[dict[str, Any]],
    page_ordinal: int,
    page_acts: list[dict[str, Any]],
    regions_by_act: dict[str, tuple[list[dict], str | None]],
) -> dict[str, dict[str, Any]]:
    """Each act's anchor range on one page, from the anchor chair's own served response.

    The live counterpart of the fixture's `[[chandra_anchor]]` rows. An act's anchor
    lines are the reported blocks overlapping its sealed regions, so acts are
    matched by geometry, never by choosing a witness (principle 1). The range is
    the hull of those blocks' spans in normalized text; a non-adjacent neighbour
    inside the hull overstates disagreement rather than hiding it.

    An act with no overlapping text gets no range. Only acts whose primary page is
    this one are anchored. A block shared by two acts gives both the same range,
    which `refuse_ambiguous_act_alignments` then refuses.
    """
    offset_map = markup_text_view(page_text)["offset_map"]
    normalized_by_raw: dict[int, list[int]] = {}
    for normalized_index, raw_index in enumerate(offset_map):
        if raw_index is not None:
            normalized_by_raw.setdefault(raw_index, []).append(normalized_index)
    anchors: dict[str, dict[str, Any]] = {}
    for act in page_acts:
        if act["page_ordinal"] != page_ordinal:
            continue
        act_bounds = [
            region["payload"]["transform"]["bounds"]
            for region in regions_by_act[act["act_id"]][0]
            if region["payload"]["transform"]["source_page_ordinal"] == page_ordinal
        ]
        starts: list[int] = []
        ends: list[int] = []
        line_geometry: list[dict[str, Any]] = []
        for block in observed:
            span = block.get("span")
            if span is None or not any(
                reported_geometry_overlaps([block], bounds) for bounds in act_bounds
            ):
                continue
            line_geometry.append({"bbox": dict(block["bounds"])})
            normalized = [
                index
                for raw_index in range(span["start"], span["end"])
                for index in normalized_by_raw.get(raw_index, [])
            ]
            if normalized:
                starts.append(min(normalized))
                ends.append(max(normalized) + 1)
        if starts:
            anchors[act["act_id"]] = {
                "start": min(starts),
                "end": max(ends),
                "line_geometry": line_geometry,
            }
    return anchors


def declared_chandra_anchor_chair(context) -> str:
    """The sole configured Chandra chair named as the alignment anchor."""
    chairs = [
        chair
        for chair in context.witness_chairs
        if isinstance(context.registry.config.chairs.get(chair), ChairIdentity)
        and context.registry.config.chairs[chair].witness_adapter == "chandra.v1"
    ]
    if len(chairs) != 1:
        raise SchemaRefusal(
            "anchor-line alignment requires exactly one configured Chandra chair; "
            "the Designator has no text and may not be used as an anchor. "
            "The alignment's textual anchor identity is therefore unresolved. "
            "Configure exactly one Chandra witness chair before running Attestatores."
        )
    return chairs[0]


def publish_attempt(
    context,
    *,
    act: dict[str, Any],
    chair: str,
    resolved: ChairIdentity | AbsentChair,
    ordinal: int,
    regions: list[dict],
    attempt: Attempt,
    live: bool = False,
) -> None:
    """Seal one immutable Testimonium. The only write path for an attempt.

    ``live`` only stops fixture `[[native_observation]]` rows standing in for the
    geometry a live response carried (principle 8).
    """
    # First, so a bad roster refuses before any record is built.
    page_witness_chairs = declared_page_witness_chairs(context)
    attempted = attempt.outcome in ATTEMPTED_WITNESS_OUTCOMES
    # One presentation covers one page; continuation crops stay bound in
    # `regions` and are named in `unpresented_regions`.
    presented = presentation_for_region(regions[0]) if attempted else {}
    adapter = (
        witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
        if attempted and isinstance(resolved, ChairIdentity)
        else None
    )
    if adapter is not None:
        source_presentation = presented
        presented = adapter.present(context, source_presentation)
        witness_adapters.validate_adapter_presentation(
            resolved.witness_adapter, source_presentation, presented
        )
    unpresented_regions = unpresented_region_ids(presented, regions)
    fixture_observed = (
        _fixture_native_observations(
            context, chair=chair, page_ordinal=presented["source_page_ordinal"]
        )
        if presented and not live
        else None
    )
    # Read from the adapter's registry entry, never by adapter name.
    takes_page_size = adapter is not None and adapter.takes_page_size
    # Read once: `_sealed_source_page` re-reads and re-digests the whole page.
    sealed_page_size = (
        _sealed_source_page(context, presented)[2] if presented and takes_page_size else None
    )
    if not presented:
        observed: list[dict[str, Any]] = []
    elif fixture_observed is not None:
        observed = fixture_observed
    elif takes_page_size:
        # Normalized boxes convert against the sealed page's size, not the crop's.
        observed = adapter.observe(
            presented,
            attempt.observation_payload
            if attempt.observation_payload is not None
            else attempt.native_payload,
            page_size=sealed_page_size,
        )
    elif adapter is not None:
        observed = adapter.observe(
            presented,
            attempt.observation_payload
            if attempt.observation_payload is not None
            else attempt.native_payload,
        )
    else:
        observed = observed_from_presentation(presented)
    # Same helper as the page partition, so both treat a mixed response alike.
    # A presentation echo lies inside its page by construction and needs no split.
    if presented and takes_page_size and (reported := _partition_geometry(observed)):
        # Overshoots are dropped here; the page record retains them as findings.
        observed, _ = split_page_edge_overshoots(reported, page_size=sealed_page_size)
    payload = testimonium_payload(
        chair=chair,
        act_key=act["act_key"],
        ordinal=ordinal,
        regions=region_references(regions) if attempted else [],
        provenance=provenance_for(
            context, resolved, attempted=attempted, receipt_ref=attempt.receipt_ref
        ),
        format_capabilities=attempt.format_capabilities,
        native_payload=attempt.native_payload,
        witness_reported=attempt.witness_reported,
        health=attempt.health,
        presented=presented,
        observed=observed,
        unpresented_regions=unpresented_regions,
        outcome=attempt.outcome,
        # Set at construction so the geometry contract validates this act view
        # as page-space geometry.
        page_witness=chair in page_witness_chairs,
        reason=attempt.reason,
        raw_response_ref=attempt.raw_response_ref,
        raw_response_kind=attempt.raw_response_kind,
        adapter_metadata=declared_adapter_metadata(
            resolved, has_raw_response=attempt.raw_response_ref is not None
        ),
        native_capture=attempt.native_capture,
        serving_call_ref=attempt.serving_call_ref,
        native_inference=attempt.native_inference,
    )
    inputs = testimonium_inputs(context, regions, presented) if attempted else []
    inputs = _named_once(inputs + _chandra_trace_inputs(attempt.native_inference))
    # Adapter output is untrusted; check before the immutable write.
    validate_testimonium_presentation(context, {"payload": payload, "inputs": inputs})
    context.publish(
        kind="testimonium",
        subject_id=act["act_id"],
        outcome=attempt.outcome,
        attempt=attempt_id(act["act_id"], f"read:{chair}", ordinal),
        inputs=inputs,
        payload=payload,
    )


def _raw_span_from_normalized(
    offset_map: list[int | None], start: int, end: int
) -> tuple[int, int] | None:
    """Translate a `[start, end)` span over `markup_text_view`'s normalized text
    back into the raw text's own character indices.

    Readers index `witness_span` into the raw text, so a normalized offset would
    shift by the collapsed whitespace. `None` entries are synthesized separators.
    """
    raw_indices = [
        offset_map[index] for index in range(start, end) if offset_map[index] is not None
    ]
    if not raw_indices:
        return None
    return min(raw_indices), max(raw_indices) + 1


class PageJoin(NamedTuple):
    """One chair's synthetic page reading: the text, what it amounts to,
    and every act attempt the join could not carry."""

    native_payload: str
    outcome: str
    unjoined_act_attempts: list[dict[str, Any]]
    # Needed to tell a page read as blank from a page not read at all.
    joined_act_attempts: int


def page_failure_reason(unjoined_act_attempts: list[dict[str, Any]], joined: int) -> str:
    """Why a page record failed, derived from the unjoined attempts' own outcomes.

    Unjoined rows are either non-readings or structured readings the text join
    cannot concatenate, and the reason must not call the latter unread. `joined`
    separates a page read as blank from one not read at all.
    """

    unread = [
        row for row in unjoined_act_attempts if row["outcome"] not in WITNESS_READING_OUTCOMES
    ]
    unjoinable = len(unjoined_act_attempts) - len(unread)
    if not unjoined_act_attempts:
        return "the page join carried no textual reading"
    if not unread:
        return (
            "every act this chair reported was a structured native reading the page join "
            "could not concatenate; the page was read and no part of it is claimed unread"
        )
    if unjoinable:
        return (
            f"the page join could not carry {len(unjoined_act_attempts)} act attempts: "
            f"{len(unread)} were not readings, and {unjoinable} were structured native "
            "readings the join cannot concatenate; a completed absence is not claimed "
            "while either kind is outstanding"
        )
    if not joined:
        # A chair never asked must not be described as asked and unread.
        if not any(row["outcome"] in ATTEMPTED_WITNESS_OUTCOMES for row in unread):
            return (
                f"this chair was never shown any of the {len(unread)} act(s) on this page: "
                "no request reached it, so the page is unattempted rather than attempted "
                "and unread"
            )
        return (
            f"no act attempt on this page was a reading at all: {len(unread)} attempts, "
            "none of them carrying a reading this join could take; the page is unread "
            "rather than read and empty"
        )
    return (
        "the page join carried only empty readings and could not carry every act attempt; "
        "a completed absence is not claimed over a page partly unread"
    )


def page_join(pairs: list[tuple[dict[str, Any], Attempt]]) -> PageJoin:
    """Concatenate one chair's delivered act readings into its page reading.

    Only a reading outcome with text joins: a `failed` attempt can still hold
    parsed text, which must not become coverage (principle 2). Everything else,
    including structured readings, is listed in `unjoined_act_attempts` from the
    same partition. Separators go only between delivered characters, and the
    outcome follows the joined text:

    - `failed`: nothing joined, and at least one attempt reached the chair.
    - `not-run`: nothing joined, and no attempt reached the chair.
    - `genuinely-empty`: every joined act delivered an empty body.
    - `read`: at least one delivered character.
    """
    joined: list[tuple[dict[str, Any], Attempt]] = []
    unjoined: list[tuple[dict[str, Any], Attempt]] = []
    for act, attempt in pairs:
        target = (
            joined
            if attempt.outcome in WITNESS_READING_OUTCOMES
            and isinstance(attempt.native_payload, str)
            else unjoined
        )
        target.append((act, attempt))
    native_payload = "\n".join(
        attempt.native_payload for _, attempt in joined if attempt.native_payload
    )
    if not joined:
        outcome = (
            "failed"
            if any(attempt.outcome in ATTEMPTED_WITNESS_OUTCOMES for _, attempt in unjoined)
            else "not-run"
        )
    elif native_payload == "":
        # An absence is claimed only over a fully joined page; unjoined acts were
        # not read here.
        outcome = "genuinely-empty" if not unjoined else "failed"
    else:
        outcome = "read"
    return PageJoin(
        native_payload=native_payload,
        outcome=outcome,
        joined_act_attempts=len(joined),
        unjoined_act_attempts=[
            {
                "act_id": act["act_id"],
                "act_key": act["act_key"],
                "outcome": attempt.outcome,
                # A reading the join could not carry has no reason of its own.
                "reason": attempt.reason
                if attempt.outcome not in WITNESS_READING_OUTCOMES
                else (
                    "this chair delivered a structured native reading for the act; R0's "
                    "synthetic page join concatenates delivered text only"
                ),
            }
            for act, attempt in unjoined
        ],
    )


def refuse_ambiguous_act_alignments(rows_by_act: list[list[dict[str, Any]]]) -> None:
    """Unalign, in place, every pair of act spans one chair cannot tell apart.

    Two acts claiming overlapping text from one chair's page reading are both
    unaligned: picking one would choose over a witness's text (principle 1). A
    zero-width span is not an overlap. A `geometric-overlap` attachment survives;
    an `anchor-line` one loses its only evidence and becomes unattached, matching
    `page_attachment_basis`. Separate so tests can reach a case no fixture makes.
    """
    by_page_chair: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for entries in rows_by_act:
        for entry in entries:
            alignment = entry["alignment"]
            if (
                entry["page_witness"]
                and entry["page_ordinal"] is not None
                and entry["attached"]
                and isinstance(alignment, dict)
                and alignment.get("status") == "aligned"
            ):
                by_page_chair.setdefault((entry["page_ordinal"], entry["chair"]), []).append(entry)
    for entries in by_page_chair.values():
        ambiguous: set[int] = set()
        for index, left in enumerate(entries):
            left_span = left["alignment"]["witness_span"]
            for other_index, right in enumerate(entries[index + 1 :], start=index + 1):
                right_span = right["alignment"]["witness_span"]
                if min(left_span["end"], right_span["end"]) > max(
                    left_span["start"], right_span["start"]
                ):
                    ambiguous.update({index, other_index})
        for index in ambiguous:
            entry = entries[index]
            entry["alignment"] = {
                "status": "unaligned",
                "reason": "ambiguous-overlapping-act-alignment",
            }
            entry["span"] = None
            entry["comparable"] = False
            if entry["attachment_basis"] == "anchor-line":
                entry["attached"] = False
                entry["attachment_basis"] = "unattached"


def act_scoped_attachment_entry(
    context,
    act: dict[str, Any],
    chair: str,
    attempt: "Attempt",
    ordinal: int,
) -> dict[str, Any]:
    """One act-scoped chair's derived attachment view of one attempt.

    Shared by the whole pass and the reread so the two cannot drift. There is no
    page reading to align into, so the span is the whole delivered reading.
    """
    attached = act["outcome"] == "proposed" and attempt.outcome in WITNESS_READING_OUTCOMES
    act_attempt = attempt_id(act["act_id"], f"read:{chair}", ordinal)
    return {
        "chair": chair,
        "page_witness": False,
        "page_ordinal": None,
        "testimonium_ref": context.artifact_ref(
            ATTESTATORES,
            "testimonium",
            artifact_id(ATTESTATORES, "testimonium", act["act_id"], act_attempt),
        ),
        "attached": attached,
        "comparable": attached and isinstance(attempt.native_payload, str),
        "attachment_basis": "presented-region" if attached else "unattached",
        "content_health": attempt.health,
        "alignment": None,
        "span": (
            {
                "start": 0,
                "end": len(attempt.native_payload)
                if isinstance(attempt.native_payload, str)
                else 0,
            }
            if attached
            else None
        ),
    }


def non_reading_alignment_reason(outcome: str, *, native_page_capture: bool) -> str:
    """Name the record whose non-reading outcome prevents page alignment.

    With a native page capture the page record's outcome gates; with the synthetic
    join it is the act attempt's, since the page can read on another act.
    """
    if outcome in WITNESS_READING_OUTCOMES:
        raise FatalAccounting(
            f"a reading outcome {outcome!r} cannot explain a non-reading page alignment. "
            "The unaligned reason would contradict the outcome it names. "
            "Derive this reason only from the non-reading record that blocked alignment."
        )
    subject = "page-testimonium" if native_page_capture else "act-attempt"
    return f"non-reading-{subject}-{outcome}"


def require_live_page_capture(
    page_captures: dict[tuple[int, str], tuple["Attempt", dict[str, Any]]],
    page_ordinal: int,
    chair: str,
) -> tuple["Attempt", dict[str, Any]]:
    """The live response this page record is derived from, or a named refusal.

    Falling back to the act-view join would describe a response nobody made
    (principle 8).
    """
    captured = page_captures.get((page_ordinal, chair))
    if captured is None:
        raise FatalAccounting(
            f"the live pass holds no response for page {page_ordinal} and chair {chair!r}, "
            "which its own page denominator names; a page record cannot be derived from "
            "testimony that was never requested"
        )
    return captured


def page_denominator(
    context,
    acts: list[dict[str, Any]],
    regions_by_act: dict[str, tuple[list[dict], str | None]],
) -> tuple[dict[str, list[int]], dict[int, list[dict[str, Any]]]]:
    """Which pages every proposed act stands on, and which acts stand on each page.

    Shared by the page publisher and the live pass so the pages asked about and
    the pages published cannot drift.
    """
    contributing_pages_by_act: dict[str, list[int]] = {}
    by_page: dict[int, list[dict[str, Any]]] = {}
    for act in acts:
        if act["outcome"] == "proposed":
            regions, refusal = regions_by_act[act["act_id"]]
            if regions:
                # The proposal's scalar page identifies the primary; the region
                # transforms supply the complete page denominator.
                contributing_pages = sorted(
                    {region["payload"]["transform"]["source_page_ordinal"] for region in regions}
                )
            else:
                # With no verified regions, the sealed proposal and continuation
                # declaration are the only available page denominator. The
                # non-reading testimony must still account for every such page.
                if refusal is None:
                    raise FatalAccounting(
                        f"act {act['act_id']} has neither verified proposal regions nor a "
                        "recorded crop refusal; its page denominator is unknowable; restore "
                        "the Designator region or refusal evidence"
                    )
                contributing_pages = [act["page_ordinal"]]
                if act["has_continuation"]:
                    if real_ingress(context):
                        # The far-page region was refused, and a real run has no
                        # declaration to name the far page instead.
                        raise FatalAccounting(
                            f"act {act['act_id']}'s proposal seal claims a continuation and "
                            "real ingress carries no continuation declaration; its far-page "
                            "evidence cannot be addressed. The Designator must publish the "
                            "continuation region that names the far page"
                        )
                    continuation = continuation_for(context.fixture, act["act_key"])
                    if continuation is None:
                        raise FatalAccounting(
                            f"act {act['act_id']} claims a continuation but the sealed fixture "
                            "names none; its far-page evidence cannot be addressed; correct the "
                            "proposal seal or fixture continuation declaration"
                        )
                    contributing_pages.append(continuation["page_ordinal"])
                contributing_pages.sort()
            contributing_pages_by_act[act["act_id"]] = contributing_pages
            for source_ordinal in contributing_pages:
                page_acts = by_page.setdefault(source_ordinal, [])
                if act not in page_acts:
                    page_acts.append(act)
    return contributing_pages_by_act, by_page


def _renumbered_onto(observed: list[dict[str, Any]], items) -> None:
    for item in items:
        observed.append({**item, "ordinal": len(observed)})


def _response_partition(
    context,
    *,
    resolved: ChairIdentity,
    presented: dict[str, Any],
    page_ordinal: int,
    page_acts: list[dict[str, Any]],
    chair: str,
    attempts_by_pair: dict[tuple[str, str], Attempt],
    page_attempt_result: Any,
    live: bool,
    fixture_observed: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """The page geometry a chair's own responses report, the responses it names, and
    its page-edge findings.

    Live, the source is the single page response; in a fixture, one declared
    response per act whose primary page this is — a continuation's primary-page
    response must not become geometry on its far page.
    """
    adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
    page_size = _sealed_source_page(context, presented)[2]
    needs_default_observation = False
    sources: list[tuple[bytes, dict[str, str] | None, bool]] = []
    if live:
        raw = page_attempt_result.observation_payload
        if raw is not None:
            # `False`: the capture already names these bytes, and
            # listing them again would state one reading twice.
            sources.append((raw, page_attempt_result.raw_response_ref, False))
    else:
        for act in page_acts:
            if act["page_ordinal"] != page_ordinal:
                continue
            source_attempt = attempts_by_pair[(act["act_id"], chair)]
            raw = source_attempt.observation_payload
            if raw is None and source_attempt.outcome == "genuinely-empty":
                needs_default_observation = True
            if raw is not None:
                sources.append((raw, source_attempt.raw_response_ref, True))
    observed: list[dict[str, Any]] = []
    response_refs: list[dict[str, str]] = []
    edge_overshoots: list[dict[str, Any]] = []
    # Acts on one page can share a raw response; a repeated finding would
    # be refused as one block counted twice.
    seen_overshoots: set[tuple[str, int]] = set()
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
        for overshoot in overshoots:
            overshoot_key = (overshoot["response_sha256"], overshoot["ordinal"])
            if overshoot_key not in seen_overshoots:
                seen_overshoots.add(overshoot_key)
                edge_overshoots.append(overshoot)
        _renumbered_onto(observed, source_observed)
    if live and sources and not observed:
        # No reported geometry: the presentation echo stands in,
        # excluded from routing and coverage.
        needs_default_observation = True
    if needs_default_observation or not sources:
        _renumbered_onto(observed, observed_from_presentation(presented))
    if fixture_observed is not None:
        # Kept alongside native blocks for the unclaimed route.
        _renumbered_onto(observed, fixture_observed)
    return observed, response_refs, edge_overshoots


def _measured_line_bbox(line: dict[str, Any], page_ordinal: int) -> dict[str, int]:
    bbox = {key: line.get(key) for key in ("x", "y", "w", "h")}
    if (
        any(not isinstance(value, int) or isinstance(value, bool) for value in bbox.values())
        or bbox["x"] < 0
        or bbox["y"] < 0
        or bbox["w"] <= 0
        or bbox["h"] <= 0
    ):
        raise SchemaRefusal(
            f"the Chandra anchor line for act {line['act_key']} on page "
            f"{page_ordinal} declares an unusable rectangle; only measured "
            "non-negative integer geometry can be published as this act's "
            "line geometry"
        )
    return bbox


def _declared_anchor(
    context, page_ordinal: int, page_acts: list[dict[str, Any]]
) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """A fixture page's declared Chandra anchor markup and each proposed act's located line.

    A malformed declaration is refused, never treated as absent: skipping it would
    detach page witnesses under a reason naming an absent anchor (principles 2, 8).
    """
    anchors = [
        row
        for row in context.fixture.get("chandra_anchor", [])
        if row.get("page_ordinal") == page_ordinal
    ]
    if len(anchors) > 1:
        raise SchemaRefusal(
            f"page {page_ordinal} declares {len(anchors)} Chandra anchors; a page has "
            "one anchor, and skipping a duplicated declaration would detach every "
            "page witness on it under a reason naming an absent anchor"
        )
    if not anchors:
        return None, {}
    anchor = anchors[0]
    if not isinstance(anchor.get("html"), str):
        raise SchemaRefusal(
            f"the Chandra anchor for page {page_ordinal} carries no anchor markup "
            "text; a malformed anchor is not an absent one"
        )
    normalized_anchor = markup_text_view(anchor["html"])["text"]
    ranges: dict[str, dict[str, Any]] = {}
    # `lines` are in reading order; searching from the previous match lets
    # a repeated formulaic opening resolve to its own occurrence.
    search_from = 0
    for line in anchor.get("lines", []):
        if not isinstance(line, dict) or not isinstance(line.get("act_key"), str):
            raise SchemaRefusal(
                f"a Chandra anchor line for page {page_ordinal} names no act key; "
                "skipping it would detach an act under a reason naming an absent line"
            )
        source = line.get("text")
        if not isinstance(source, str):
            raise SchemaRefusal(
                f"the Chandra anchor line for act {line['act_key']} on page "
                f"{page_ordinal} carries no text; a malformed line is not an absent one"
            )
        needle = markup_text_view(source)["text"]
        start = normalized_anchor.find(needle, search_from) if needle else -1
        if start < 0:
            raise SchemaRefusal(
                f"the Chandra anchor line for act {line['act_key']} on page "
                f"{page_ordinal} does not occur in the page's own anchor text at or "
                "after the previous line; an unlocatable declared line is malformed "
                "evidence, not an absent act line"
            )
        act = next((item for item in page_acts if item["act_key"] == line["act_key"]), None)
        if act is not None:
            if act["act_id"] in ranges:
                raise SchemaRefusal(
                    f"page {page_ordinal} declares more than one Chandra anchor "
                    f"line for act {line['act_key']}; keeping the last one would "
                    "drop the first line's span and geometry without a record"
                )
            ranges[act["act_id"]] = {
                "start": start,
                "end": start + len(needle),
                "line_geometry": [{"bbox": _measured_line_bbox(line, page_ordinal)}],
            }
        # Advance even for an unproposed act: its line still occupies the page.
        search_from = start + len(needle)
    return anchor["html"], ranges


def _blank_reading_alignment(
    act_anchor: dict[str, Any] | None, *, page_anchored: bool, anchor_chair: str | None
) -> dict[str, Any]:
    """A genuinely empty reading attaches trivially at a zero-length span.

    Alignment can never match an empty string, so the blank would otherwise stay
    permanently unaligned (principles 2, 8).
    """
    located = act_anchor is not None
    start = act_anchor["start"] if located else 0
    return {
        "status": "aligned",
        # Which absence: no page anchor at all (blank confirmation stays open) or
        # an anchor that locates no line for this act (a terminal blank is refused).
        "anchor_basis": (
            "act-anchor"
            if located
            else ("act-line-not-located" if page_anchored else "no-page-anchor")
        ),
        "anchor_chair": anchor_chair if located else None,
        "anchor_span": {"start": start, "end": start},
        "witness_span": {"start": 0, "end": 0},
        # Recorded anyway, so "no match" never looks like "not measured" (principle 2).
        "anchor_line_match": {
            "anchor_characters": act_anchor["end"] - start if located else 0,
            "matched_characters": 0,
            "longest_matched_run": 0,
        },
        "line_geometry": _line_geometry(act_anchor) if located else [],
        "loss": {"witness": _ZERO_ALIGNMENT_LOSS, "anchor": _ZERO_ALIGNMENT_LOSS},
        "offset_maps": {"witness": [], "anchor": []},
        # Alignment never ran, so no deadline applied.
        "deadline_in_force": False,
    }


def _act_span_alignment(
    result: dict[str, Any], act_anchor: dict[str, Any], anchor_chair: str | None
) -> dict[str, Any]:
    """Clip a page alignment to one act's anchor range and store it in raw indices.

    Clipping happens in normalized space, since whole blocks would hand every act
    the whole page; the span is translated once, at storage, because every
    consumer indexes the raw text.
    """
    clipped = []
    # Measured here, where the fragments exist; the record keeps only a hull. The
    # longest run stops scattered coincidental characters counting as located.
    # Blocks are disjoint, so the sum does not double-count.
    matched_characters = 0
    longest_matched_run = 0
    for span in result["spans"]:
        start = max(span["anchor"]["start"], act_anchor["start"])
        end = min(span["anchor"]["end"], act_anchor["end"])
        if start < end:
            shift = span["witness"]["start"] - span["anchor"]["start"]
            clipped.append((start + shift, end + shift))
            matched_characters += end - start
            longest_matched_run = max(longest_matched_run, end - start)
    if not clipped:
        return {"status": "unaligned", "reason": "no-overlap-with-act-anchor"}
    # A hull across fragments may include a neighbour's characters. Deliberate: it
    # overstates disagreement and never hides it. Do not "fix" towards agreement.
    raw_span = _raw_span_from_normalized(
        result["witness"]["offset_map"],
        min(start for start, _ in clipped),
        max(end for _, end in clipped),
    )
    if raw_span is None:
        return {"status": "unaligned", "reason": "no-raw-counterpart-for-aligned-span"}
    witness_start, witness_end = raw_span
    return {
        "status": "aligned",
        "anchor_basis": "act-anchor",
        "anchor_chair": anchor_chair,
        "anchor_span": {key: act_anchor[key] for key in ("start", "end")},
        "witness_span": {"start": witness_start, "end": witness_end},
        "anchor_line_match": {
            "anchor_characters": act_anchor["end"] - act_anchor["start"],
            "matched_characters": matched_characters,
            "longest_matched_run": longest_matched_run,
        },
        "line_geometry": _line_geometry(act_anchor),
        "loss": {"witness": result["witness"]["loss"], "anchor": result["anchor"]["loss"]},
        "offset_maps": {
            "witness": result["witness"]["offset_map"],
            "anchor": result["anchor"]["offset_map"],
        },
        "deadline_in_force": result["deadline_in_force"],
    }


def _page_witness_alignment(
    *,
    page_outcome: str,
    native_page_capture: bool,
    act_anchor: dict[str, Any] | None,
    page_text: str | None,
    anchor_text: str | None,
    anchor_chair: str | None,
    page_alignments: dict[tuple[int, str], dict[str, Any]],
    page_key: tuple[int, str],
    limits: Any,
) -> dict[str, Any]:
    """Where a page witness's reading places one act, measured against the page anchor."""
    if page_outcome not in WITNESS_READING_OUTCOMES:
        # No reading to place; aligning anyway would claim text the chair never delivered.
        return {
            "status": "unaligned",
            "reason": non_reading_alignment_reason(
                page_outcome, native_page_capture=native_page_capture
            ),
        }
    if page_outcome == "genuinely-empty":
        return _blank_reading_alignment(
            act_anchor, page_anchored=anchor_text is not None, anchor_chair=anchor_chair
        )
    if page_text is None or anchor_text is None:
        return {"status": "unaligned", "reason": "missing-chandra-page-anchor"}
    if act_anchor is None:
        return {"status": "unaligned", "reason": "act-anchor-line-not-located"}
    # Cached per (page, chair): the inputs do not depend on the act, and
    # `SequenceMatcher` can be near cubic.
    result = page_alignments.get(page_key)
    if result is None:
        result = align_to_anchor(page_text, anchor_text, limits)
        page_alignments[page_key] = result
    if result["status"] == "aligned":
        return _act_span_alignment(result, act_anchor, anchor_chair)
    # No `deadline_in_force`: `reason` already names a fired deadline.
    return {"status": "unaligned", "reason": result["reason"]}


def _page_witness_entries(
    *,
    act: dict[str, Any],
    chair: str,
    act_attempt: Attempt,
    alignment: dict[str, Any],
    contributing_pages: Any,
    act_regions: list[dict[str, Any]],
    page_outcomes: dict[tuple[int, str], str],
    page_observations: dict[tuple[int, str], list[dict[str, Any]]],
    page_records: dict[tuple[int, str], dict[str, str]],
    page_texts: dict[tuple[int, str], str],
) -> list[dict[str, Any]]:
    """One attachment entry per page the act spans; only its primary page carries the alignment.

    An entry is attached by reported ink over the proposal or, where there is none
    (Churro reports no geometry), by a located anchor line. The anchor places
    text; it never judges a reading (principle 1).
    """
    entries = []
    for contributing_page in contributing_pages:
        page_alignment = (
            alignment
            if contributing_page == act["page_ordinal"]
            else {"status": "unaligned", "reason": "continuation-page-no-act-anchor"}
        )
        page_bounds = [
            region["payload"]["transform"]["bounds"]
            for region in act_regions
            if region["payload"]["transform"]["source_page_ordinal"] == contributing_page
        ]
        contributing_outcome = page_outcomes.get((contributing_page, chair), act_attempt.outcome)
        attachment_basis = page_attachment_basis(
            reading=contributing_outcome in WITNESS_READING_OUTCOMES,
            geometry_overlaps=any(
                reported_geometry_overlaps(page_observations[(contributing_page, chair)], bounds)
                for bounds in page_bounds
            ),
            alignment=page_alignment,
        )
        page_attached = attachment_basis != "unattached"
        spans_text = page_attached and page_alignment["status"] == "aligned"
        entries.append(
            {
                "chair": chair,
                "page_witness": True,
                "page_ordinal": contributing_page,
                "testimonium_ref": page_records[(contributing_page, chair)],
                "attached": page_attached,
                "comparable": spans_text
                and isinstance(page_texts.get((contributing_page, chair)), str),
                "attachment_basis": attachment_basis,
                # The act attempt's health, even under a page capture: the Perlector
                # and Recensor compare it with the current act Testimonium to detect
                # a later reread.
                "content_health": act_attempt.health,
                "alignment": page_alignment,
                "span": (
                    {
                        "start": page_alignment["witness_span"]["start"],
                        "end": page_alignment["witness_span"]["end"],
                    }
                    if spans_text
                    else None
                ),
            }
        )
    return entries


def publish_page_testimonia_and_attachments(
    context,
    *,
    acts: list[dict[str, Any]],
    ordinal: int,
    regions_by_act: dict[str, tuple[list[dict], str | None]],
    attempts_by_pair: dict[tuple[str, str], Attempt],
    page_captures: dict[tuple[int, str], tuple[Attempt, dict[str, Any]]] | None = None,
) -> None:
    """Retain page testimony and derive one attachment record for every act.

    Page witnesses' act-scoped records are a compatibility view for the Perlector;
    each attachment links to the page Testimonium that supplied it.
    """
    page_chairs = declared_page_witness_chairs(context)
    anchor_chair = declared_chandra_anchor_chair(context)
    limits, limits_digest = load_alignment_limits(context.args.alignment_config)
    context.require_sealed_config("alignment", limits_digest)
    page_records: dict[tuple[int, str], dict[str, str]] = {}
    page_observations: dict[tuple[int, str], list[dict[str, Any]]] = {}
    page_texts: dict[tuple[int, str], str] = {}
    # Native captures only; a legacy join's outcome comes from its act attempts.
    page_outcomes: dict[tuple[int, str], str] = {}
    # Separate from `page_texts` so no chair name can collide with the anchor.
    anchor_texts: dict[int, str] = {}
    page_alignments: dict[tuple[int, str], dict[str, Any]] = {}
    anchor_ranges: dict[tuple[int, str], dict[str, int]] = {}
    contributing_pages_by_act, by_page = page_denominator(context, acts, regions_by_act)
    # Built once; each lookup would otherwise walk the Exemplar inventory.
    page_ids = exemplar_page_ids(context)

    for page_ordinal, page_acts in sorted(by_page.items()):
        page_subject_id = page_subject(context, page_ordinal, page_ids=page_ids)
        page_proposal_regions = sealed_page_proposal_regions(context, page_ordinal)
        for chair in sorted(page_chairs):
            resolved = context.registry.resolve(chair)
            if not isinstance(resolved, ChairIdentity):
                raise FatalAccounting(
                    f"page witness chair {chair!r} did not resolve to a configured identity"
                )
            if page_captures is None:
                captured = captured_churro_page_attempt(
                    context, page_ordinal, chair, resolved.witness_adapter
                )
            else:
                captured = require_live_page_capture(page_captures, page_ordinal, chair)
            if captured is None:
                # Legacy fixture rows keep the synthetic join.
                join = page_join(
                    [(act, attempts_by_pair[(act["act_id"], chair)]) for act in page_acts]
                )
                page_attempt_result, native_capture = join, None
                native_payload, outcome = join.native_payload, join.outcome
                unjoined_act_attempts = join.unjoined_act_attempts
            else:
                page_attempt_result, native_capture = captured
                native_payload, outcome = (
                    page_attempt_result.native_payload,
                    page_attempt_result.outcome,
                )
                unjoined_act_attempts = []
                page_outcomes[(page_ordinal, chair)] = page_attempt_result.outcome
            reading = outcome in WITNESS_READING_OUTCOMES
            # Whether a response arrived, judged by retained bytes: an unparsable
            # live body arrived (principle 2), but a request refused before
            # sending is also filed as a capture and nothing arrived for it.
            arrived = native_capture is not None or (
                page_captures is not None
                and captured is not None
                and page_attempt_result.raw_response_ref is not None
            )
            attempted_page = captured is not None or page_witness_attempted(
                page_acts, chair, attempts_by_pair
            )
            failure_reason = (
                page_attempt_result.reason
                if captured is not None
                else page_failure_reason(
                    unjoined_act_attempts, page_attempt_result.joined_act_attempts
                )
            )
            # Text only; a `None` here would read later as "no anchor".
            if isinstance(native_payload, str):
                page_texts[(page_ordinal, chair)] = native_payload
            health = (
                page_attempt_result.health
                if captured is not None
                else content_health(native_payload, completed=reading)
            )
            presented = (
                presentation_for_page(context, page_ordinal, page_ids=page_ids)
                if attempted_page
                else {}
            )
            adapter = (
                witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
                if attempted_page
                else None
            )
            if adapter is not None:
                source_presentation = presented
                presented = adapter.present(context, source_presentation)
                witness_adapters.validate_adapter_presentation(
                    resolved.witness_adapter, source_presentation, presented
                )
            unpresented_regions = unpresented_region_ids(presented, page_proposal_regions)
            page_attempt = attempt_id(page_subject_id, f"read:{chair}", ordinal)
            roles = {
                "primary" if act["page_ordinal"] == page_ordinal else "continuation"
                for act in page_acts
            }
            page_role = roles.pop() if len(roles) == 1 else "mixed"
            page_response_refs: list[dict[str, str]] = []
            page_edge_overshoots: list[dict[str, Any]] = []
            # For a Chandra chair, declared observations add to derived geometry.
            fixture_observed = (
                _fixture_native_observations(context, chair=chair, page_ordinal=page_ordinal)
                if page_captures is None
                else None
            )
            if not presented:
                observed: list[dict[str, Any]] = []
            elif _derives_partition_from_response(resolved, page_captures):
                observed, page_response_refs, page_edge_overshoots = _response_partition(
                    context,
                    resolved=resolved,
                    presented=presented,
                    page_ordinal=page_ordinal,
                    page_acts=page_acts,
                    chair=chair,
                    attempts_by_pair=attempts_by_pair,
                    page_attempt_result=page_attempt_result,
                    live=page_captures is not None,
                    fixture_observed=fixture_observed,
                )
            elif fixture_observed is not None:
                observed = fixture_observed
            elif adapter is not None:
                observed = adapter.observe(presented, native_payload)
            else:
                # Unreachable while absent chairs are never attempted.
                observed = observed_from_presentation(presented)
            page_artifact_id = artifact_id(
                ATTESTATORES, "page-testimonium", page_subject_id, page_attempt
            )
            # Every proposal/observation pairing is kept; this stage does not assign
            # a marginal observation to an act. Absent, not empty, for a
            # never-presented page: zero proposals would be false and the Recensor
            # refuses it.
            disagreement = (
                partition_disagreement(
                    {
                        "artifact_id": page_artifact_id,
                        "payload": {"presented": presented, "observed": observed},
                    },
                    page_proposal_regions,
                    page_edge_overshoots=page_edge_overshoots,
                )
                if presented
                else None
            )
            payload = page_testimonium_payload(
                page_ordinal=page_ordinal,
                page_role=page_role,
                unjoined_act_attempts=unjoined_act_attempts,
                partition_disagreement=disagreement,
                testimonium_id=page_artifact_id,
                raw_response_refs=page_response_refs,
                adapter_metadata=declared_adapter_metadata(
                    resolved, has_raw_response=bool(page_response_refs)
                ),
                native_capture=native_capture,
                native_inference=page_attempt_result.native_inference
                if captured is not None
                else None,
                chair=chair,
                act_key=f"page-{page_ordinal}",
                ordinal=ordinal,
                regions=[],
                # Every attempted outcome, failed included, is receipt-backed.
                provenance=provenance_for(
                    context,
                    resolved,
                    attempted=attempted_page,
                    receipt_ref=page_attempt_result.receipt_ref if captured is not None else None,
                ),
                # A synthetic join spans several attempts and has no single value,
                # so it records the default.
                format_capabilities=(
                    page_attempt_result.format_capabilities
                    if captured is not None
                    else DEFAULT_FORMAT_CAPABILITIES
                ),
                # A cut-off empty capture retains text without claiming absence.
                native_payload=native_payload if reading or arrived else None,
                witness_reported=None,
                # Native failure health means a response arrived; legacy
                # non-reading health means no response channel arrived.
                health=(
                    health if reading or arrived else no_response_health(reason=failure_reason)
                ),
                presented=presented,
                observed=observed,
                unpresented_regions=unpresented_regions,
                outcome=outcome,
                reason=None if reading else failure_reason,
            )
            inputs = [context.input_ref(presented["image_path"])] if presented else []
            # Checked before the immutable write, as in `publish_attempt`.
            validate_testimonium_presentation(context, {"payload": payload, "inputs": inputs})
            context.publish(
                kind="page-testimonium",
                subject_id=page_subject_id,
                outcome=outcome,
                attempt=page_attempt,
                # Every retained response is an input, because `read_artifact`
                # re-hashes only `inputs`. A live Chandra page reaches one blob
                # twice, so each is named once.
                inputs=_named_once(
                    inputs
                    + page_response_refs
                    + ([native_capture["raw_response_ref"]] if native_capture is not None else [])
                    + _chandra_trace_inputs(
                        page_attempt_result.native_inference if captured is not None else None
                    )
                ),
                payload=payload,
            )
            page_records[(page_ordinal, chair)] = context.artifact_ref(
                ATTESTATORES,
                "page-testimonium",
                page_artifact_id,
            )
            page_observations[(page_ordinal, chair)] = observed
        if page_captures is not None:
            # Live anchors come from the anchor chair's own served response,
            # never fixture rows (principle 8).
            anchor_page_text = page_texts.get((page_ordinal, anchor_chair))
            if page_outcomes.get((page_ordinal, anchor_chair)) == "read" and isinstance(
                anchor_page_text, str
            ):
                anchor_texts[page_ordinal] = anchor_page_text
                for act_id, act_anchor in derived_chandra_anchor(
                    page_text=anchor_page_text,
                    observed=page_observations[(page_ordinal, anchor_chair)],
                    page_ordinal=page_ordinal,
                    page_acts=page_acts,
                    regions_by_act=regions_by_act,
                ).items():
                    anchor_ranges[(page_ordinal, act_id)] = act_anchor
        else:
            anchor_html, declared_ranges = _declared_anchor(context, page_ordinal, page_acts)
            if anchor_html is not None:
                anchor_texts[page_ordinal] = anchor_html
            for act_id, act_anchor in declared_ranges.items():
                anchor_ranges[(page_ordinal, act_id)] = act_anchor

    attachment_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for act in acts:
        entries: list[dict[str, Any]] = []
        for chair in context.witness_chairs:
            act_attempt = attempts_by_pair[(act["act_id"], chair)]
            if chair not in page_chairs or act["outcome"] != "proposed":
                entries.append(
                    act_scoped_attachment_entry(context, act, chair, act_attempt, ordinal)
                )
                continue
            page_key = (act["page_ordinal"], chair)
            # The outcome of the page record this entry names: the native capture's
            # where there is one, else the act attempt the legacy join came from.
            captured_outcome = page_outcomes.get(page_key)
            alignment = _page_witness_alignment(
                page_outcome=(
                    captured_outcome if captured_outcome is not None else act_attempt.outcome
                ),
                native_page_capture=captured_outcome is not None,
                act_anchor=anchor_ranges.get((act["page_ordinal"], act["act_id"])),
                page_text=page_texts.get(page_key),
                anchor_text=anchor_texts.get(act["page_ordinal"]),
                anchor_chair=anchor_chair,
                page_alignments=page_alignments,
                page_key=page_key,
                limits=limits,
            )
            entries.extend(
                _page_witness_entries(
                    act=act,
                    chair=chair,
                    act_attempt=act_attempt,
                    alignment=alignment,
                    contributing_pages=contributing_pages_by_act[act["act_id"]],
                    act_regions=regions_by_act[act["act_id"]][0],
                    page_outcomes=page_outcomes,
                    page_observations=page_observations,
                    page_records=page_records,
                    page_texts=page_texts,
                )
            )
        attachment_rows.append((act, entries))

    refuse_ambiguous_act_alignments([entries for _act, entries in attachment_rows])

    for act, entries in attachment_rows:
        context.publish(
            kind="act-attachment",
            subject_id=act["act_id"],
            outcome="read",
            attempt=attempt_id(act["act_id"], "act-attachment", ordinal),
            # References live in the payload, not `inputs`, so missing evidence is
            # diagnosed by the tally rather than failing the manifest rebuild.
            inputs=[],
            payload={
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "attachments": entries,
            },
        )


def attempt_pass(
    context,
    acts: list[dict[str, Any]],
    ordinal: int,
    regions_by_act: dict[str, tuple[list[dict], str | None]],
    attempts_by_pair: dict[tuple[str, str], Attempt],
    sealed_pairs: frozenset[tuple[str, str]],
) -> tuple[int, bool]:
    """Every configured chair's attempt at every expected act, at one ordinal.

    Returns the records counted and whether any proposal crop was refused.
    Publishes exactly the attempts preflight checked; already sealed pairs are
    counted, not republished.
    """
    recorded = 0
    isolated_crop_failure = False
    for act in acts:
        regions, not_read = regions_by_act[act["act_id"]]
        if not_read is not None and act["outcome"] != "held":
            # Isolated to this act: every chair gets a non-reading record.
            isolated_crop_failure = True

        for chair in context.witness_chairs:
            if (act["act_id"], chair) in sealed_pairs:
                recorded += 1
                continue
            resolved = context.registry.resolve(chair)
            publish_attempt(
                context,
                act=act,
                chair=chair,
                resolved=resolved,
                ordinal=ordinal,
                regions=regions,
                attempt=attempts_by_pair[(act["act_id"], chair)],
            )
            recorded += 1
    return recorded, isolated_crop_failure


def bound_serving_recipes(context) -> ServingRecipes:
    """The serving catalogue this run sealed, re-read and re-checked by digest.

    Re-checked at the moment of use so the rows deciding live or fixture are the
    sealed ones (principle 6). An unreadable catalogue is a configuration refusal,
    not a witness failure.
    """
    if context.serving_config_inputs is None:  # pragma: no cover - open_context always sets it
        raise ContractError(
            "this run authority seals no serving configuration inputs; the serving posture "
            "of its chairs cannot be read"
        )
    try:
        recipes = load_serving_recipes(context.args.serving_recipes_config)
        placement_bytes = Path(DEFAULT_POD_PLACEMENT_CONFIG_PATH).read_bytes()
        ServingConfigInputs.from_record(dict(context.serving_config_inputs)).require_loaded(
            recipes_sha256=recipes.source_sha256,
            placement_sha256=digest_bytes(placement_bytes),
        )
    except OSError as error:
        raise ContractError(
            f"the sealed serving configuration could not be read: {error}"
        ) from error
    except ServingError as error:
        raise ContractError(f"the sealed serving configuration was refused: {error}") from error
    return recipes


def witness_serving_modes(context, recipes: ServingRecipes, tier: str | None) -> dict[str, str]:
    """`fixture` or `live` for every configured witness chair, and never a mix.

    A mixed roster would put fixture and live evidence side by side in one
    attempt layer, unmarked. Absent chairs have no mode.
    """
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
    """On a real submission every configured witness chair serves, or nothing runs.

    A real run has no fixture, so a fixture-posture chair could never answer, and
    a run with no served chair would read no ink.
    """
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


def default_serving_factory(context, identity: ChairIdentity, tier: str) -> ChairClient:
    """Build the client a live pass reads one chair through.

    Everything is bound to this run, so a Testimonium's receipt is one this run
    wrote. Nothing starts until `ChairClient.__enter__`. Tests inject their own
    factory in-process; it is deliberately not a CLI flag, so no fake can answer
    under a configured chair's name.
    """
    policy, decoding_sha256 = load_decoding_policy(context.args.decoding_config)
    manager = ServingManager(
        registry=context.registry,
        recipes=bound_serving_recipes(context),
        config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=StageContextReceiptPublisher(context),
        # Logs go where `inventory_scope()` expects them, or `fetch-run` refuses
        # the served tree. The lease is per pod, not per run tree, and a network
        # mount may not honour an advisory lock, so it uses the container path.
        log_root=context.tree.resolve(context.tree.serving_log_path(ATTESTATORES)),
        residency_lease=FileResidencyLease(POD_RESIDENCY_LOCK_PATH),
        producer="pipeline/3_attestatores/run.py",
        _launch_purpose=(
            MECHANICS_QUALIFICATION_PURPOSE
            if getattr(context.args, "mechanics_qualification", False)
            else None
        ),
    )
    return ChairClient(
        manager=manager,
        identity=identity,
        tier=tier,
        retain=lambda data: retained_blob_ref(context, data),
        decoding_config_sha256=decoding_sha256,
        record_temperature=policy["reading_of_record"]["temperature"],
        # Passed bare: `ChairClient.__enter__` copies the read-only receipt
        # reference into a dict.
        read_receipt=context.tree.read_run_receipt,
        chandra_native_policy=policy.get("chandra_native_inference"),
    )


def retained_blob_ref(context, data: bytes) -> dict[str, str]:
    """Retain bytes in this stage's own content-addressed blob store."""
    digest, published = context.tree.put_blob(ATTESTATORES, data)
    return {"relative_path": published.relative_path, "sha256": digest}


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


def _sealed_page_testimonia(context, ordinal: int) -> dict[tuple[int, str], dict[str, Any]]:
    """Every page Testimonium already sealed at this ordinal, by page and chair."""
    sealed: dict[tuple[int, str], dict[str, Any]] = {}
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("attempt_ordinal") != ordinal:
            continue
        page_ordinal, chair = payload.get("page_ordinal"), payload.get("chair")
        if isinstance(page_ordinal, int) and isinstance(chair, str):
            sealed[(page_ordinal, chair)] = record
    return sealed


def served_live(context, provenance: Any) -> bool:
    """Did a chair really serve the record this provenance belongs to?

    Read from the receipt endpoint, not inferred from optional fields: a live
    Chandra page record may carry neither a capture nor a call reference.
    """
    reference = provenance.get("receipt_ref") if isinstance(provenance, dict) else None
    if not isinstance(reference, dict):
        return False
    receipt = context.tree.read_run_receipt(dict(reference))
    return not str(receipt.get("endpoint", "")).startswith("fixture://")


def _page_capture_from_record(
    context, record: dict[str, Any], what: str
) -> tuple[Attempt, dict[str, Any] | None]:
    """Rebuild one page capture from a record the interrupted pass already sealed.

    A live chair cannot reproduce immutable bytes, so the page is rebuilt, not
    re-asked (principle 4). A fixture-served record is refused.
    """
    payload = record["payload"]
    provenance = payload.get("provenance")
    if not served_live(context, provenance):
        raise SchemaRefusal(
            f"{what} names no live serving receipt, so it was not written by a live pass; a "
            "live pass cannot resume over a fixture-posture record, and re-asking the chair "
            "would replace immutable evidence with different bytes"
        )
    capture = payload.get("native_capture")
    if capture is not None:
        capture = validate_native_capture(capture)
    observation_payload = None
    if (
        capture is not None
        # Read from the registry, as in `_derives_partition_from_response`.
        and witness_adapters.resolve_runnable_adapter(capture["adapter"]).takes_page_size
        and capture["parse"]["state"] == "parsed"
        and _retains_chandra_observation_payload(record)
    ):
        # Parse state alone is too wide: a parsed but cut-off `failed` body
        # carried no geometry bytes originally (principle 4). The bytes are
        # re-read and digest-checked because the page geometry is re-derived
        # from them on republish.
        reference = validate_raw_response_ref(capture["raw_response_ref"])
        try:
            observation_payload = context.tree.read_bytes(reference["relative_path"])
        except OSError as error:
            raise SchemaRefusal(
                f"{what} names a retained raw response that could not be read: "
                f"{reference['relative_path']}: {error}"
            ) from error
        if digest_bytes(observation_payload) != reference["sha256"]:
            raise SchemaRefusal(
                f"{what} names a retained raw response whose digest differs from its reference"
            )
    return (
        Attempt(
            outcome=record["outcome"],
            native_payload=payload["payload"],
            witness_reported=None,
            format_capabilities=payload["format_capabilities"],
            health=payload["content_health"],
            reason=payload.get("reason"),
            raw_response_ref=capture["raw_response_ref"] if capture is not None else None,
            observation_payload=observation_payload,
            native_capture=capture,
            receipt_ref=provenance.get("receipt_ref") if isinstance(provenance, dict) else None,
            # A page record has no such field; a capture always names model output.
            raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT if capture is not None else None,
            native_inference=payload.get("native_inference"),
        ),
        capture,
    )


def resumed_page_captures(
    context,
    *,
    acts_by_page: dict[int, list[dict[str, Any]]],
    page_chairs: list[str],
    ordinal: int,
    attempts_by_pair: dict[tuple[str, str], Attempt],
    sealed_pairs: frozenset[tuple[str, str]],
) -> dict[tuple[int, str], tuple[Attempt, dict[str, Any]]]:
    """Every page response a resumed live pass must not ask for a second time.

    Found in the sealed page Testimonium, or else in the act records of acts
    whose primary page this is, which derive from the same response. Every such
    act record is compared, and a disagreement is refused rather than resolved.
    """
    sealed_records = _sealed_page_testimonia(context, ordinal)
    captures: dict[tuple[int, str], tuple[Attempt, dict[str, Any]]] = {}
    for page_ordinal, page_acts in sorted(acts_by_page.items()):
        for chair in page_chairs:
            record = sealed_records.get((page_ordinal, chair))
            if record is not None:
                captures[(page_ordinal, chair)] = _page_capture_from_record(
                    context,
                    record,
                    f"the page Testimonium sealed for page {page_ordinal}, chair {chair!r}",
                )
                continue
            candidates: list[tuple[str, Attempt]] = []
            for act in page_acts:
                pair = (act["act_id"], chair)
                if act["page_ordinal"] != page_ordinal or pair not in sealed_pairs:
                    continue
                attempt = attempts_by_pair[pair]
                if attempt.outcome not in ATTEMPTED_WITNESS_OUTCOMES:
                    # Never shown pixels, so no evidence about the page response.
                    continue
                if (
                    attempt.serving_call_ref is None
                    and attempt.health.get("recordable") is None
                    and served_live(context, {"receipt_ref": attempt.receipt_ref})
                ):
                    # A live request refused before sending: no call, no response.
                    # The receipt tells it from a fixture no-payload row. Reused,
                    # since re-asking would refuse the same way.
                    candidates.append((act["act_id"], attempt))
                    continue
                if attempt.serving_call_ref is None:
                    # Every live attempt names its call record; this one is fixture.
                    raise SchemaRefusal(
                        f"the Testimonium sealed for act {act['act_id']} and chair {chair!r} at "
                        f"ordinal {ordinal} names no serving call, so it was not written by a "
                        "live pass; a live pass cannot resume over a fixture-posture record"
                    )
                candidates.append((act["act_id"], attempt))
            if not candidates:
                continue
            first_act_id, first_attempt = candidates[0]
            for act_id, attempt in candidates[1:]:
                if (
                    attempt.raw_response_ref != first_attempt.raw_response_ref
                    or attempt.native_capture != first_attempt.native_capture
                    or attempt.native_inference != first_attempt.native_inference
                    or attempt.outcome != first_attempt.outcome
                ):
                    raise SchemaRefusal(
                        f"the Testimonia sealed for page {page_ordinal}, chair {chair!r} "
                        f"disagree between act {first_act_id!r} and act {act_id!r} about which "
                        "response produced them; a resumed page capture cannot be rebuilt from "
                        "records that do not agree about their own evidence"
                    )
            captures[(page_ordinal, chair)] = (first_attempt, first_attempt.native_capture)
    return captures


def live_attempt_pass(
    context,
    acts: list[dict[str, Any]],
    ordinal: int,
    regions_by_act: dict[str, tuple[list[dict], str | None]],
    attempts_by_pair: dict[tuple[str, str], Attempt],
    sealed_pairs: frozenset[tuple[str, str]],
    *,
    serving_factory,
    tier: str,
) -> tuple[int, bool, dict[tuple[int, str], tuple[Attempt, dict[str, Any]]]]:
    """The same pass, asked of chairs that really serve: chair-outer, one request
    at a time, and every response published before the next one is requested.

    One resident chair at a time, in a deterministic chair-outer order
    (`feeding.stage_major_schedule`). Publishing on arrival means an interrupted
    pass leaves its sealed responses on disk. A page-scoped chair is asked once
    per page, and its act records derive from that response.
    """
    page_chairs = declared_page_witness_chairs(context)
    _contributing_pages, acts_by_page = page_denominator(context, acts, regions_by_act)
    # Built once for this whole pass; see `publish_page_testimonia_and_attachments`.
    page_ids = exemplar_page_ids(context)
    live_page_chairs = sorted(
        chair
        for chair in context.witness_chairs
        if chair in page_chairs and isinstance(context.registry.resolve(chair), ChairIdentity)
    )
    page_captures = resumed_page_captures(
        context,
        acts_by_page=acts_by_page,
        page_chairs=live_page_chairs,
        ordinal=ordinal,
        attempts_by_pair=attempts_by_pair,
        sealed_pairs=sealed_pairs,
    )
    recorded = 0
    isolated_crop_failure = False

    # Pairs needing no request are published first, so the folder accounts for
    # them if the first request refuses. Sealed pairs are only counted.
    for act in acts:
        regions, not_read = regions_by_act[act["act_id"]]
        if not_read is not None and act["outcome"] != "held":
            isolated_crop_failure = True
        for chair in context.witness_chairs:
            pair = (act["act_id"], chair)
            if pair in sealed_pairs:
                recorded += 1
                continue
            attempt = attempts_by_pair[pair]
            if attempt is PENDING_LIVE_ATTEMPT:
                continue
            publish_attempt(
                context,
                act=act,
                chair=chair,
                resolved=context.registry.resolve(chair),
                ordinal=ordinal,
                regions=regions,
                attempt=attempt,
                live=True,
            )
            recorded += 1

    # A resume may have stopped between a page's act views; publish any still
    # pending from the resumed capture. After the loop above, so nothing is
    # published twice.
    for (page_ordinal, chair), (attempt, _capture) in page_captures.items():
        recorded += publish_page_act_views(
            context,
            chair=chair,
            resolved=context.registry.resolve(chair),
            attempt=attempt,
            page_ordinal=page_ordinal,
            page_acts=acts_by_page[page_ordinal],
            ordinal=ordinal,
            regions_by_act=regions_by_act,
            attempts_by_pair=attempts_by_pair,
        )

    # One schedule per chair, concatenated, since a unit is a page or an act by
    # scope; the executor's ordering guarantees still hold.
    units: dict[tuple[str, str], Any] = {}
    schedule: list[dict[str, str]] = []
    for chair in sorted(set(context.witness_chairs)):
        resolved = context.registry.resolve(chair)
        if not isinstance(resolved, ChairIdentity):
            continue
        rows: list[dict[str, Any]] = []
        if chair in page_chairs:
            for page_ordinal in sorted(acts_by_page):
                if (page_ordinal, chair) in page_captures:
                    continue
                # Addressed by the sealed Exemplar page id.
                unit_id = page_subject(context, page_ordinal, page_ids=page_ids)
                units[(chair, unit_id)] = page_ordinal
                rows.append({"act_id": unit_id, "page_ordinal": page_ordinal})
        else:
            for act in acts:
                if attempts_by_pair[(act["act_id"], chair)] is not PENDING_LIVE_ATTEMPT:
                    continue
                units[(chair, act["act_id"])] = act
                rows.append({"act_id": act["act_id"], "page_ordinal": act["page_ordinal"]})
        schedule.extend(feeding.stage_major_schedule(context.tree.run_id, rows, [chair]))

    # `None` for an adapter with a single framing.
    framings = {
        chair: witness_adapters.framing_for(context.registry.config, chair)
        for chair in sorted(page_chairs)
    }

    def serve(client: ChairClient, row: dict[str, str]) -> None:
        nonlocal recorded
        chair = row["chair"]
        resolved = context.registry.resolve(chair)
        adapter = witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
        unit = units[(chair, row["act_id"])]
        if chair in page_chairs:
            recorded += _serve_page_unit(
                context,
                client=client,
                chair=chair,
                resolved=resolved,
                adapter=adapter,
                page_ordinal=unit,
                page_acts=acts_by_page[unit],
                ordinal=ordinal,
                regions_by_act=regions_by_act,
                attempts_by_pair=attempts_by_pair,
                page_captures=page_captures,
                page_ids=page_ids,
                framing=framings[chair],
            )
        else:
            recorded += _serve_act_unit(
                context,
                client=client,
                chair=chair,
                resolved=resolved,
                adapter=adapter,
                act=unit,
                ordinal=ordinal,
                regions=regions_by_act[unit["act_id"]][0],
                attempts_by_pair=attempts_by_pair,
            )

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

    unresolved = sorted(
        pair for pair, value in attempts_by_pair.items() if value is PENDING_LIVE_ATTEMPT
    )
    if unresolved:
        raise FatalAccounting(
            f"the live pass finished with {len(unresolved)} unresolved witness attempt(s) "
            f"{unresolved[:3]}; every configured chair answers for every expected act, or the "
            "record says why"
        )
    return recorded, isolated_crop_failure, page_captures


# vLLM's `stop` and `length`, the fixture transport's synonyms for them, and the
# no-stop-reason marker. Any other word has no measured meaning.
_LIVE_ENGINE_STOP_WORDS: Final = _CHURRO_STOP_REASONS | {STOP_REASON_UNREPORTED}


def refuse_unpublishable_stop_word(transport_stop_reason: str, what: str) -> None:
    """Refuse a live response whose engine stop word cannot be recorded honestly.

    Mapping an unknown word to complete or cut off would be a measurement nobody
    made (principle 8). Checked on the stop word alone, so unparsed responses are
    covered too. The bytes are already retained (principle 2).
    """
    if transport_stop_reason not in _LIVE_ENGINE_STOP_WORDS:
        raise ContractError(
            f"{what} reports transport_stop_reason {transport_stop_reason!r}, which this "
            "pipeline has never measured a meaning for; recording it as complete or as cut "
            "off would assert a boundary nobody observed. The response bytes are retained "
            "and nothing was published for it"
        )


def capacity_refusal_attempt(
    error: RequestCapacityRefusal,
    *,
    receipt_ref: Mapping[str, str],
    what: str,
    adapter: Any = None,
) -> Attempt:
    """One chair's outcome for a request its sealed row could not hold.

    The refusal concerns this request only, so it becomes a `failed` attempt and
    the pass continues; one oversized page must not cost every other page. Health
    is the no-response shape, since nothing arrived. `receipt_ref` is the chair's
    real start, which marks the record as live for a resume. `adapter` keeps the
    chair's declared capabilities on the record; the default is for a caller with
    no adapter in hand.
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


def _serve_act_unit(
    context,
    *,
    client: ChairClient,
    chair: str,
    resolved: ChairIdentity,
    adapter,
    act: dict[str, Any],
    ordinal: int,
    regions: list[dict],
    attempts_by_pair: dict[tuple[str, str], Attempt],
) -> int:
    """One act-scoped chair, one act: ask, derive, publish, before the next act."""
    presentation = presentation_for_region(regions[0])
    try:
        built = live_witness.act_chair_request(
            context,
            adapter,
            presentation,
            # Checked against the row this chair runs under, like any request.
            profile=client.handle.profile,
        )
    except RequestCapacityRefusal as error:
        # Only this act's crop failed; the next may fit.
        attempt = capacity_refusal_attempt(
            error,
            receipt_ref=client.handle.receipt_reference,
            what=f"the {resolved.witness_adapter} request for act {act['act_id']}",
            adapter=adapter,
        )
        attempts_by_pair[(act["act_id"], chair)] = attempt
        publish_attempt(
            context,
            act=act,
            chair=chair,
            resolved=resolved,
            ordinal=ordinal,
            regions=regions,
            attempt=attempt,
            live=True,
        )
        return 1
    response = client.read(built.request)
    live = live_witness.live_attempt_from_response(
        context,
        adapter,
        resolved.witness_adapter,
        response,
        presentation=presentation,
        presented=built.presented,
        prompt=built.prompt,
        generation_declared=built.request.generation_declared,
        parser="text",
        generation_accounting=built.generation_accounting,
    )
    transport_stop_reason = (
        response.finish_reason if response.finish_reason is not None else STOP_REASON_UNREPORTED
    )
    refuse_unpublishable_stop_word(
        transport_stop_reason,
        f"the {resolved.witness_adapter} response for act {act['act_id']}",
    )
    attempt = attempt_from_live(live)
    attempts_by_pair[(act["act_id"], chair)] = attempt
    publish_attempt(
        context,
        act=act,
        chair=chair,
        resolved=resolved,
        ordinal=ordinal,
        regions=regions,
        attempt=attempt,
        live=True,
    )
    return 1


def publish_page_act_views(
    context,
    *,
    chair: str,
    resolved: ChairIdentity,
    attempt: Attempt,
    page_ordinal: int,
    page_acts: list[dict[str, Any]],
    ordinal: int,
    regions_by_act: dict[str, tuple[list[dict], str | None]],
    attempts_by_pair: dict[tuple[str, str], Attempt],
) -> int:
    """Publish every still-pending act view one page chair's response feeds.

    Only acts whose primary page is this one; a continuation reaches its act
    through the page record. Pairs already sealed are left untouched.
    """
    recorded = 0
    for act in page_acts:
        pair = (act["act_id"], chair)
        if (
            act["page_ordinal"] != page_ordinal
            or attempts_by_pair[pair] is not PENDING_LIVE_ATTEMPT
        ):
            continue
        attempts_by_pair[pair] = attempt
        publish_attempt(
            context,
            act=act,
            chair=chair,
            resolved=resolved,
            ordinal=ordinal,
            regions=regions_by_act[act["act_id"]][0],
            attempt=attempt,
            live=True,
        )
        recorded += 1
    return recorded


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


def _attempt_evidence_record(attempt: Attempt) -> dict[str, Any]:
    """Canonical subset needed to recover the vendor-returned result."""

    return {
        "outcome": attempt.outcome,
        "native_payload": attempt.native_payload,
        "witness_reported": attempt.witness_reported,
        "format_capabilities": attempt.format_capabilities,
        "health": attempt.health,
        "reason": attempt.reason,
        "raw_response_ref": attempt.raw_response_ref,
        "native_capture": attempt.native_capture,
        "serving_call_ref": attempt.serving_call_ref,
        "receipt_ref": attempt.receipt_ref,
        "raw_response_kind": attempt.raw_response_kind,
    }


def _attempt_from_evidence_record(context, value: Any) -> Attempt:
    required = {
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
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SchemaRefusal("a Chandra native terminal artifact has no closed result record")
    observation_payload = None
    capture = value["native_capture"]
    if capture is not None:
        validate_native_capture(capture)
        reference = validate_raw_response_ref(capture["raw_response_ref"])
        observation_payload = context.tree.read_bytes(reference["relative_path"])
        if digest_bytes(observation_payload) != reference["sha256"]:
            raise SchemaRefusal(
                "a Chandra native terminal artifact's model output differs from its digest"
            )
    return Attempt(
        outcome=value["outcome"],
        native_payload=value["native_payload"],
        witness_reported=value["witness_reported"],
        format_capabilities=value["format_capabilities"],
        health=value["health"],
        reason=value["reason"],
        raw_response_ref=value["raw_response_ref"],
        observation_payload=observation_payload,
        native_capture=capture,
        serving_call_ref=value["serving_call_ref"],
        receipt_ref=value["receipt_ref"],
        raw_response_kind=value["raw_response_kind"],
    )


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
    model_output_ref = retained_blob_ref(context, response.content.encode("utf-8"))
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
        or not isinstance(payload["page_ordinal"], int)
        or isinstance(payload["page_ordinal"], bool)
        or payload["page_ordinal"] < 1
        or not isinstance(payload["witness_attempt_ordinal"], int)
        or isinstance(payload["witness_attempt_ordinal"], bool)
        or payload["witness_attempt_ordinal"] < 1
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
    body = context.tree.read_bytes(body_ref["relative_path"])
    if digest_bytes(body) != body_ref["sha256"] or body_ref["sha256"] != payload["request_sha256"]:
        raise SchemaRefusal("a Chandra native attempt intent's retained request body moved")
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
    if record.get("inputs") != sorted(
        expected_inputs,
        key=lambda ref: (ref.get("relative_path", ""), ref.get("sha256", "")),
    ):
        raise SchemaRefusal("a Chandra native attempt intent does not bind its exact request")
    return payload


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
    expected_temperature = json.dumps(float(parameters["temperature"]))
    expected_top_p = json.dumps(float(parameters["top_p"]))
    if (
        call_record.get("native_attempt_intent_ref") != payload["intent_ref"]
        or call_record.get("request_sha256") != payload["request_sha256"]
        or call_record.get("chair") != "attestator_1"
        or call_record.get("receipt_ref") != intent["receipt_ref"]
        or call_record.get("image_sha256s") != intent["image_sha256s"]
        or resolved.receipt_ref != intent["receipt_ref"]
        or call_record.get("generation_declared") != {"max_new_tokens": CHANDRA_MAX_OUTPUT_TOKENS}
        or not isinstance(sent, dict)
        or set(sent) - {"chat_template_kwargs", "max_tokens", "temperature", "top_p"}
        or sent.get("chat_template_kwargs") != {"enable_thinking": False}
        or sent.get("max_tokens", CHANDRA_MAX_OUTPUT_TOKENS) != CHANDRA_MAX_OUTPUT_TOKENS
        or "seed" in sent
        or sent.get("temperature") != {"schema": "wire-decimal.v1", "decimal": expected_temperature}
        or sent.get("top_p") != {"schema": "wire-decimal.v1", "decimal": expected_top_p}
    ):
        raise SchemaRefusal("a Chandra native serving call record moved its pinned request")

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
        model_output = context.tree.read_bytes(model_output_ref["relative_path"])
        if digest_bytes(model_output) != model_output_ref["sha256"]:
            raise SchemaRefusal("a Chandra native terminal's final model output moved")
        try:
            raw = model_output.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SchemaRefusal("a Chandra native terminal's model output is not UTF-8") from error
    expected_trigger = chandra_retry_trigger(
        raw,
        inference_error=inference_error,
        attempt_ordinal=native_attempt_ordinal,
    )
    expected_returned = (
        chandra_exhausted_condition(raw, inference_error=inference_error)
        if native_attempt_ordinal == CHANDRA_MAX_ATTEMPTS
        else expected_trigger
    )
    if trigger != expected_trigger or returned != expected_returned:
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
    if "inputs" in record and record["inputs"] != sorted(
        _named_once(expected_inputs),
        key=lambda ref: (ref.get("relative_path", ""), ref.get("sha256", "")),
    ):
        raise SchemaRefusal("a Chandra native terminal artifact does not bind all call evidence")
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
    request_body_ref = retained_blob_ref(context, dispatch.body)
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


def _sealed_chandra_intents(context, subject_id: str) -> list[dict[str, Any]]:
    """Return the validated intent chain, including a possible unmatched tail."""

    rows: list[dict[str, Any]] = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "chandra-native-attempt-intent" or entry["subject_id"] != subject_id:
            continue
        record = context.tree.read_artifact(
            ATTESTATORES, "chandra-native-attempt-intent", entry["artifact_id"]
        )
        payload = record.get("payload")
        ordinal = payload.get("native_attempt_ordinal") if isinstance(payload, dict) else None
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 1 <= ordinal <= CHANDRA_MAX_ATTEMPTS
        ):
            raise SchemaRefusal("a retained Chandra native intent has no valid ordinal")
        expected_artifact_id = artifact_id(
            ATTESTATORES,
            "chandra-native-attempt-intent",
            subject_id,
            attempt_id(subject_id, "chandra-native-intent", ordinal),
        )
        if entry["artifact_id"] != expected_artifact_id:
            raise SchemaRefusal("a retained Chandra native intent has a moved identity")
        intent_ref = context.artifact_ref(
            ATTESTATORES, "chandra-native-attempt-intent", entry["artifact_id"]
        )
        _validate_chandra_intent(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=ordinal,
            intent_ref=intent_ref,
        )
        rows.append(record)
    return sorted(rows, key=lambda record: record["payload"]["native_attempt_ordinal"])


def _sealed_chandra_attempts(context, subject_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "chandra-native-attempt" or entry["subject_id"] != subject_id:
            continue
        record = context.tree.read_artifact(
            ATTESTATORES, "chandra-native-attempt", entry["artifact_id"]
        )
        payload = record.get("payload")
        ordinal = payload.get("native_attempt_ordinal") if isinstance(payload, dict) else None
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 1 <= ordinal <= CHANDRA_MAX_ATTEMPTS
        ):
            raise SchemaRefusal("a retained Chandra native terminal has no valid ordinal")
        _validate_chandra_terminal(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=ordinal,
            record=record,
        )
        rows.append(record)
    return sorted(rows, key=lambda record: record["payload"]["native_attempt_ordinal"])


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


def _with_chandra_trace(attempt: Attempt, trace: dict[str, Any]) -> Attempt:
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
        refuse_chandra_orphan_intent(True)

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
            trace = _chandra_retry_trace(context, subject_id, terminal_records)
            return _with_chandra_trace(returned_attempt, trace)

    next_ordinal = len(terminal_records) + 1
    if terminal_records and terminal_records[-1]["payload"].get("trigger") == "inference-error":
        # A crash during the backoff cannot show how much elapsed, so the full
        # delay is repeated.
        resumed_delay = chandra_error_backoff_seconds(next_ordinal - 1)
        if resumed_delay is None:
            raise FatalAccounting("the final Chandra attempt requested an impossible retry")
        time.sleep(resumed_delay)
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
            refuse_chandra_orphan_intent(True)

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
                transport_stop_reason = (
                    response.finish_reason
                    if response.finish_reason is not None
                    else STOP_REASON_UNREPORTED
                )
                refuse_unpublishable_stop_word(
                    transport_stop_reason,
                    f"the {resolved.witness_adapter} response for page {page_ordinal}",
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

        trigger = chandra_retry_trigger(
            raw, inference_error=inference_error, attempt_ordinal=next_ordinal
        )
        returned_condition = (
            chandra_exhausted_condition(raw, inference_error=inference_error)
            if next_ordinal == CHANDRA_MAX_ATTEMPTS
            else trigger
        )
        payload, _terminal_ref = _publish_chandra_terminal(
            context,
            subject_id=subject_id,
            native_attempt_ordinal=next_ordinal,
            intent_ref=intent_ref,
            request_sha256=dispatch.request_sha256,
            attempt=attempt,
            trigger=trigger,
            returned_condition=returned_condition,
            error=inference_error,
            error_code=error_code,
            error_detail=error_detail,
            transport_response_ref=(
                dict(transport_response_ref) if transport_response_ref is not None else None
            ),
        )
        terminal_records.append({"payload": payload})
        if trigger is None:
            if application_refusal is not None:
                raise application_refusal
            trace = _chandra_retry_trace(context, subject_id, terminal_records)
            return _with_chandra_trace(attempt, trace)
        if trigger == "inference-error":
            delay = chandra_error_backoff_seconds(next_ordinal)
            if delay is None:
                raise FatalAccounting("the final Chandra attempt requested an impossible retry")
            time.sleep(delay)
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
    page_acts: list[dict[str, Any]],
    ordinal: int,
    regions_by_act: dict[str, tuple[list[dict], str | None]],
    attempts_by_pair: dict[tuple[str, str], Attempt],
    page_captures: dict[tuple[int, str], tuple[Attempt, dict[str, Any]]],
    page_ids: dict[int, str] | None = None,
    framing: str | None = None,
) -> int:
    """One page-scoped chair, one page: one request, then every act view it feeds."""
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
        page_captures[(page_ordinal, chair)] = (attempt, None)
        return publish_page_act_views(
            context,
            chair=chair,
            resolved=resolved,
            attempt=attempt,
            page_ordinal=page_ordinal,
            page_acts=page_acts,
            ordinal=ordinal,
            regions_by_act=regions_by_act,
            attempts_by_pair=attempts_by_pair,
        )
    if (
        client.carries_chandra_native_recipe
        and resolved.role == "attestator_1"
        and resolved.witness_adapter == "chandra.v1"
        and resolved.witness_scope == "page"
    ):
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
        transport_stop_reason = (
            response.finish_reason if response.finish_reason is not None else STOP_REASON_UNREPORTED
        )
        refuse_unpublishable_stop_word(
            transport_stop_reason,
            f"the {resolved.witness_adapter} response for page {page_ordinal}",
        )
        attempt = attempt_from_live(live)
    page_captures[(page_ordinal, chair)] = (attempt, attempt.native_capture)
    return publish_page_act_views(
        context,
        chair=chair,
        resolved=resolved,
        attempt=attempt,
        page_ordinal=page_ordinal,
        page_acts=page_acts,
        ordinal=ordinal,
        regions_by_act=regions_by_act,
        attempts_by_pair=attempts_by_pair,
    )


def witness_bound_reading_acts(context) -> frozenset[str]:
    """Every act whose reading was already established from this act's testimony.

    The reading ordinal follows the crop history, so testimony added after a
    reading would collide with the immutable Perlectio (principle 4); new ink must
    come through a recovery crop, never a re-rolled witness (principle 7). Only a
    Perlectio citing testimony closes an act; a `not-run` one does not depend on it.
    """
    closed = set()
    for entry in context.tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "perlectio" or entry["subject_id"] in closed:
            continue
        record = context.tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        basis = record.get("payload", {}).get("basis")
        if isinstance(basis, dict) and basis.get("testimonia"):
            closed.add(entry["subject_id"])
    return frozenset(closed)


def require_open_witness_layer(closed: frozenset[str], act: dict[str, Any], what: str) -> None:
    """Refuse a new witness attempt on an act the Perlector has already read.

    Checked before any write; otherwise the collision surfaces stages later with
    no way forward (principle 4). Callers apply it to appends only, not resumes.
    """
    if act["act_id"] in closed:
        raise ContractError(
            f"act {act['act_id']} ({act['act_key']}) already carries a Perlectio, so its "
            f"witness layer is closed: {what} would append testimony no reading can be "
            "established from. A witness pass may add coverage, but a reading is made only "
            "by a crop: new ink must route through a Recensor recovery request, which mints "
            "a region and moves the reading ordinal. New testimony after a reading is "
            "refused; new INK after a reading is a recovery request. Re-asking a witness "
            "because it spoke again is the re-roll principle 7 refuses"
        )


def next_attempt_ordinal(history: AttemptHistory, act_id: str, chair: str) -> int:
    """The ordinal a reread of this one chair appends at, from its history on disk."""
    records = history.get((act_id, chair), [])
    if not records:
        raise ContractError(
            f"a reread named chair {chair!r} on act {act_id!r}, which has no prior attempt for "
            "that chair to follow — a reread is a second attempt, and there is no first"
        )
    current = latest_attempt(
        records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
    )
    return current["payload"]["attempt_ordinal"] + 1


def reread_pass(
    context,
    acts: list[dict[str, Any]],
    act_id: str,
    chair: str,
    index: "AttemptIndex",
) -> int:
    """Append one new attempt for one named chair on one named act.

    Every other chair's current record stays as it was. The reread is shown the
    original proposal regions, never a recovery crop.
    """
    act = next((row for row in acts if row["act_id"] == act_id), None)
    if act is None:
        raise ContractError(
            f"a reread named act {act_id!r}, which the Designator proposal seal does not"
        )
    if chair not in context.witness_chairs:
        raise ContractError(f"a reread named chair {chair!r}, which this run is not sealed with")
    if act["outcome"] == "held":
        raise ContractError(f"act {act_id} is held; no witness was shown a reading there to reread")
    resolved = context.registry.resolve(chair)
    if isinstance(resolved, AbsentChair):
        raise ContractError(
            f"chair {chair!r} is explicitly absent: {resolved.reason}; there is no witness "
            "to reread"
        )
    if chair in declared_page_witness_chairs(context):
        # A page witness's act view is derived from its page reading; rereading
        # one act would contradict the page record. (`page-level-reread` is a
        # Perlector operation, unrelated.)
        raise ContractError(
            f"chair {chair!r} is page-scoped in this run: it reports one reading per "
            "page and its act-level view is derived from that page reading, so there is no "
            f"act-scoped attempt for act {act_id} to repeat. No operation exists to re-ask "
            "a page witness; building one would be new page-scoped Attestatores work, and "
            "an act-scoped reread of a derived view is not it"
        )
    require_open_witness_layer(
        witness_bound_reading_acts(context), act, f"a reread of chair {chair!r}"
    )

    # `next_attempt_ordinal` is always current + 1, so no appendable check is needed.
    ordinal = next_attempt_ordinal(index.by_pair, act_id, chair)
    attempt = resolve_attempt(
        context,
        act,
        chair,
        resolved,
        declarations_for(context, ordinal),
        reread=True,
    )
    next_ordinal, entries = prepared_act_attachment(context, index, act, chair)
    publish_attempt(
        context,
        act=act,
        chair=chair,
        resolved=resolved,
        ordinal=ordinal,
        regions=proposed_regions(context, act_id),
        attempt=attempt,
    )
    republish_act_attachment(context, act, chair, attempt, ordinal, next_ordinal, entries)
    return 1


def prepared_act_attachment(
    context,
    index: "AttemptIndex",
    act: dict[str, Any],
    chair: str,
) -> tuple[int, list[dict[str, Any] | None]]:
    """Every refusal for the reread's re-derived attachment, WITHOUT writing.

    Run before the Testimonium is published, so a refusal leaves the folder
    untouched. The reread chair's slot is `None` until its new Testimonium exists;
    other chairs' entries are carried forward after a staleness check.
    """
    records = index.attachments_by_act.get(act["act_id"], [])
    if not records:
        raise ContractError(
            f"act {act['act_id']} has no act-attachment for the reread to re-derive; a "
            "targeted reread follows a whole pass and never stands in for one"
        )
    current = latest_attempt(
        records, f"act-attachment for {act['act_id']}", operation="act-attachment"
    )
    attachments = current.get("payload", {}).get("attachments")
    if not isinstance(attachments, list) or {
        item.get("chair") if isinstance(item, dict) else None for item in attachments
    } != set(context.witness_chairs):
        raise SchemaRefusal(
            f"act {act['act_id']}'s current act-attachment does not describe this run's "
            "configured witnesses; a reread may not re-derive it"
        )
    entries: list[dict[str, Any] | None] = []
    for item in attachments:
        if item["chair"] == chair:
            entries.append(None)
            continue
        other = latest_attempt(
            index.by_pair.get((act["act_id"], item["chair"]), []),
            f"Testimonium for {(act['act_id'], item['chair'])!r}",
            operation=f"read:{item['chair']}",
        )
        if item.get("content_health") != other["payload"].get("content_health"):
            raise SchemaRefusal(
                f"act {act['act_id']}'s current act-attachment already describes an attempt "
                f"that is no longer chair {item['chair']!r}'s current Testimonium; a reread "
                "of another chair does not make that record current again"
            )
        if (
            not item.get("page_witness")
            and item.get("attached")
            and other["outcome"] not in WITNESS_READING_OUTCOMES
        ):
            # Act-scoped only: a page witness's `attached` comes from alignment.
            raise SchemaRefusal(
                f"act {act['act_id']}'s current act-attachment claims chair "
                f"{item['chair']!r} attached while its current outcome is "
                f"{other['outcome']!r}; a reread of another chair does not make that "
                "claim current again"
            )
        entries.append(item)
    return current["payload"]["attempt_ordinal"] + 1, entries


def republish_act_attachment(
    context,
    act: dict[str, Any],
    chair: str,
    attempt: "Attempt",
    ordinal: int,
    next_ordinal: int,
    entries: list[dict[str, Any] | None],
) -> None:
    """Publish the attachment `prepared_act_attachment` already checked, filling
    the reread chair's slot."""
    filled = [
        act_scoped_attachment_entry(context, act, chair, attempt, ordinal) if item is None else item
        for item in entries
    ]
    context.publish(
        kind="act-attachment",
        subject_id=act["act_id"],
        outcome="read",
        attempt=attempt_id(act["act_id"], "act-attachment", next_ordinal),
        inputs=[],
        payload={
            "act_key": act["act_key"],
            "attempt_ordinal": next_ordinal,
            "attachments": filled,
        },
    )


def refuse_unread_fixture_declarations(context, live_chairs: list[str]) -> None:
    """Say, once and out loud, which fixture declarations a live pass does not read.

    A live pass uses the fixture's corpus but none of its declared responses; the
    operator is told so (principle 2). A real submission has no fixture.
    """
    if real_ingress(context):
        return
    families = ("testimony", "witness_failure", "witness_empty", "witness_not_run")
    counted = {
        family: sum(
            1
            for row in context.fixture.get(family, [])
            if isinstance(row, dict)
            and row.get("chair") in live_chairs
            and row.get("scenario") in (None, context.scenario)
        )
        for family in ("churro_page_response", "native_observation", *families)
    }
    # Anchors have no chair, and a live pass ignores all of them.
    counted["chandra_anchor"] = sum(
        1
        for row in context.fixture.get("chandra_anchor", [])
        if isinstance(row, dict) and row.get("scenario") in (None, context.scenario)
    )
    declared = {family: count for family, count in counted.items() if count}
    if declared:
        print(
            "Attestatores live pass: the sealed fixture declares witness rows this posture does "
            f"not read {dict(sorted(declared.items()))}; every outcome below came from a chair "
            "that served it",
            file=sys.stderr,
        )


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Run every configured chair through one attempt, or reread one named chair.

    ``serving_factory`` is an in-process test seam like ``registry_factory``, used
    only when the sealed catalogue says the witnesses are live.
    """
    parser = stage_parser(__doc__.splitlines()[0], accepts_chair=True)
    parser.add_argument(
        "--attempt-ordinal",
        type=_positive_ordinal,
        # No default, so a reread can tell an explicit ordinal from none.
        default=None,
        help="append this ordinal for every act/chair, or repeat the current one byte-identically",
    )
    args = parser.parse_args()
    if args.operation not in OPERATIONS:
        raise ContractError(
            f"the Attestatores has no {args.operation!r} operation; it implements "
            f"{sorted(OPERATIONS)}. A mistyped reread would otherwise run a whole pass, "
            "ignore the act and chair it was given, and report success"
        )
    # Opens the fixture or the real context, as the run authority says.
    context = open_stage_context(args, ATTESTATORES, registry_factory=registry_factory)
    real = real_ingress(context)
    # A witness reading is a model decode, so its decoding policy must be sealed.
    _decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    witness_adapters.validate_runnable_adapter_bindings(context.registry.config)
    # Resolved first: the serving posture decides which pass runs.
    modes = witness_serving_modes(context, bound_serving_recipes(context), args.placement_tier)
    if real:
        require_every_witness_served(modes)
    live_chairs = sorted(chair for chair, mode in modes.items() if mode == "live")
    acts = expected_acts(context)
    try:
        index = _attempt_history(context)
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
    has_prior_boundary = stored_inventory or has_stage_seal
    if has_prior_boundary:
        # No chair denominator here: this pass is what fills it. See `attempt_tally`.
        prior_tally = attempt_tally(context.tree, context=context, acts=acts)
        if prior_tally["hold"]:
            print(f"Attestatores attempt tally UNKNOWN: {prior_tally['reason']}", file=sys.stderr)
            return EXIT_HELD

    isolated_crop_failure = False
    if args.operation == "reread":
        if live_chairs:
            # A live reread needs its own residency and publication, not yet built.
            raise ContractError(
                "this run's witness chairs serve live, and no live reread is built: a reread "
                "asks one chair for one act again, and the live boundary here publishes a whole "
                "pass chair-outer. Run the whole pass at the next ordinal, or reread under the "
                "fixture catalogue"
            )
        if not args.act or not args.chair:
            raise ContractError(
                "a reread names the one act and the one chair it rereads; without both it "
                "would be a whole second pass wearing a narrower name"
            )
        if args.attempt_ordinal is not None:
            raise ContractError(
                "a reread appends at the ordinal the named chair's own history says comes "
                f"next; --attempt-ordinal {args.attempt_ordinal} names a different attempt "
                "and honouring neither of the two silently is not an option"
            )
        recorded = reread_pass(context, acts, args.act, args.chair, index)
    else:
        if args.act or args.chair:
            raise ContractError(
                "--act and --chair name a targeted reread; a whole pass reads every "
                "configured chair on every expected act and cannot narrow to them"
            )
        ordinal = 1 if args.attempt_ordinal is None else args.attempt_ordinal
        try:
            # Read even for a live fixture run, to refuse a self-contradicting
            # fixture; the live resolver then ignores it.
            declarations = (
                real_declarations(ordinal) if real else declarations_for(context, ordinal)
            )
            regions_by_act, attempts_by_pair, sealed_pairs = preflight_appendable_ordinals(
                context,
                acts,
                ordinal,
                declarations,
                index,
                # A live chair cannot reproduce immutable bytes (principle 4).
                resume_incomplete_pass=bool(live_chairs) or not has_prior_boundary,
                resolve=pending_live_attempt if live_chairs else None,
                fixture_declared=not real,
            )
        except ContractError as error:
            # Held before any write; an accounting imbalance stays fatal.
            if isinstance(error, FatalAccounting):
                raise
            print(f"Attestatores refused this pass: {error}", file=sys.stderr)
            return EXIT_HELD
        page_captures = None
        if live_chairs:
            refuse_unread_fixture_declarations(context, live_chairs)
            recorded, isolated_crop_failure, page_captures = live_attempt_pass(
                context,
                acts,
                ordinal,
                regions_by_act,
                attempts_by_pair,
                sealed_pairs,
                serving_factory=default_serving_factory
                if serving_factory is None
                else serving_factory,
                tier=args.placement_tier,
            )
        else:
            recorded, isolated_crop_failure = attempt_pass(
                context,
                acts,
                ordinal,
                regions_by_act,
                attempts_by_pair,
                sealed_pairs,
            )
        publish_page_testimonia_and_attachments(
            context,
            acts=acts,
            ordinal=ordinal,
            regions_by_act=regions_by_act,
            attempts_by_pair=attempts_by_pair,
            page_captures=page_captures,
        )

    if recorded == 0:
        raise ContractError("no chair produced an outcome for any act")

    # Write the inventory for the tally, and seal only after it passes.
    context.finish()
    tally = attempt_tally(context.tree, context=context, acts=acts, chairs=context.witness_chairs)
    if tally["hold"]:
        print(f"Attestatores attempt tally UNKNOWN: {tally['reason']}", file=sys.stderr)
        context.seal_boundary()
        context.finish()
        return EXIT_HELD
    context.seal_boundary()
    context.finish()
    if isolated_crop_failure:
        # Not a hold: every chair has a non-reading record, and later stages show it.
        print("Attestatores recorded one or more refused proposal crops", file=sys.stderr)
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
