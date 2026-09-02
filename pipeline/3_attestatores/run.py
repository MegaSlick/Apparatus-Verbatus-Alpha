"""Attestatores: retain every witness attempt without changing its history.

Each Testimonium has two deliberately separate witness-facing fields. ``payload``
is the witness's native response, retained without shaping it into an imagined
common body schema. ``witness_reported`` is a witness's own confidence or status
claim, retained as a claim but never used to compute channel health. The latter is
computed here from the native response and the transport boundary.

Attempts are append-only. A re-read receives a new ordinal and artifact identity;
there is no current pointer to update. Consumers derive current from the newest
contiguous ordinal, so a later failed attempt remains visibly failed while the
earlier reading remains intact in history.

Two write paths, and both append. `--attempt-ordinal N` is the whole pass: every
configured chair, every expected act, at that one ordinal — the same command
twice writes the same bytes. `--operation reread --act <id> --chair <role>` moves
exactly one chair on one act, at the ordinal that chair's own history says comes
next; a reread happens because one witness failed on one act, and re-witnessing
the other chairs to reach it would re-read ink nobody doubted.

    python pipeline/3_attestatores/run.py --run-root <dir> --run-id <id>
    python pipeline/3_attestatores/run.py ... --operation reread --act <id> --chair <role>
"""

import json
import sys
from pathlib import Path
from typing import Any, Final, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import feeding  # noqa: E402
import live_witness  # noqa: E402
import witness_adapters  # noqa: E402

from common.alignment import align_to_anchor, load_alignment_limits, markup_text_view  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.serving import (  # noqa: E402
    RAW_RESPONSE_KINDS,
    RAW_RESPONSE_MODEL_OUTPUT,
    STOP_REASON_UNREPORTED,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, PERLECTOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.fixture_identity import page_identity  # noqa: E402
from common.imaging import dimensions  # noqa: E402
from common.native_witness import (  # noqa: E402
    PAGE_TESTIMONIUM_REQUIRED_FIELDS,
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
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    EXIT_COMPLETE,
    EXIT_HELD,
    WITNESS_READING_OUTCOMES,
    continuation_for,
    expected_acts,
    fixture_serving_details,
    latest_attempt,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    ServingRecipes,
    load_serving_recipes,
)
from operations.serving.errors import ServingError  # noqa: E402
from operations.serving.http import UrllibHttpTransport  # noqa: E402
from operations.serving.manager import ServingManager, StageContextReceiptPublisher  # noqa: E402
from operations.serving.process import SubprocessLauncher  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

# A witness may report one of these ordinal self-assessments. They are retained
# as testimony about its own response, never promoted into a model ranking or
# used to choose a witness. Six plain levels keep fixture and future adapters
# interoperable while refusing an unbounded integer scale or invented prose.
# `uncertain` and `unsure` are deliberately both admitted: real adapters emit
# both spellings, and collapsing them to one is R3's call when it meets those
# adapters, not this stage's.
WITNESS_CONFIDENCE_ORDINALS = frozenset({"certain", "high", "medium", "low", "uncertain", "unsure"})

DEFAULT_FORMAT_CAPABILITIES = {
    "can_express_uncertainty": False,
    "can_express_layout": False,
}


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


# The two write paths this program implements, checked against by name because
# `--operation` carries no argparse `choices` — the same parser serves every
# stage — so an unrecognized one otherwise falls through to the whole pass and a
# mistyped reread re-reads nothing while exiting 0.
OPERATIONS = frozenset({"initial", "reread"})

# A witness response is untrusted input: a several-thousand-deep nested value
# drives `_native_problem` past Python's recursion limit, and `RecursionError` is
# not a `ContractError`, so nothing between here and process exit catches it —
# one adversarial witness takes down the whole folder rather than its own attempt.
# Real transcription output nests a handful of levels deep, so this is headroom
# rather than a fit.
_MAX_NATIVE_DEPTH = 64


def proposed_regions(context, act_id: str) -> list[dict]:
    """Every original Designator region the chair was actually shown.

    A later recovery region is intentionally not substituted. A Testimonium binds
    to these exact pixel blobs, not to whichever crop happens to be current when a
    later consumer reads the run tree.
    """
    regions = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
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

    The page partition and unrouted-observation denominator is the page's whole
    sealed proposal set (Unit 10C's rule): a held act's proposal was still
    sealed on this page, and omitting it makes the retained snapshot contradict
    the Recensor's independent re-derivation of the same denominator.
    """
    regions = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
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

    The writer's caller has already verified this region's crop lineage, but
    `validate_testimonium_presentation` reads regions straight out of the
    Designator manifest to reconcile a record it is treating as untrusted. A
    sealed region missing its transform or blob identity left that seam as a
    raw KeyError -- from the check whose whole job is to name what is wrong
    with the evidence -- so the missing field is named here instead.
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

    Distinct from `page_join`'s `reading` (whether text was actually produced):
    a chair attempted and shown a crop that came back `failed` is still a chair
    that was shown an image, so `presented` must not collapse that case into the
    same empty block held acts, refused pages, and absent chairs record. Using
    `reading` here would misreport "shown pixels, bad response" as "never shown
    an image" (GOVERNANCE 2).
    """
    return any(
        attempts_by_pair[(act["act_id"], chair)].outcome in ATTEMPTED_WITNESS_OUTCOMES
        for act in page_acts
    )


def presentation_for_page(context, page_ordinal: int) -> dict[str, Any]:
    """Bind a page witness to the sealed whole-page pixels it was shown."""
    page_id = page_identity(context.fixture, page_ordinal)
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
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
        # A row missing a coordinate is refused where the fixture row can be
        # identified. Passing `{"w": None}` down instead reaches the geometry
        # contract as "non-integer page-pixel coordinates", which is true and
        # names neither the chair, the page, nor which row to go and fix.
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


def chandra_page_partition_entries(
    observed: list[dict[str, Any]],
    *,
    page_size: tuple[int, int],
    raw_response_ref: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return surviving Chandra boxes and response-linked page-edge findings.

    `page_size` is the sealed source page, never a presentation crop: a block
    may cross the crop edge while remaining valid page-space geometry.

    The page Testimonium, rather than the retired textual act bridge, owns the
    witness partition.  A finding without the raw response that supplied its
    block is a named but unauditable assertion, so this narrow join refuses it
    before either a page record or an attachment can be published.
    """
    survivors, overshoots = split_page_edge_overshoots(observed, page_size=page_size)
    if overshoots and (
        not isinstance(raw_response_ref, dict)
        or not isinstance(raw_response_ref.get("sha256"), str)
        or len(raw_response_ref["sha256"]) != 64
    ):
        raise SchemaRefusal(
            "a Chandra page-edge finding has no retained response reference. "
            "The rejected block cannot be traced to the response that produced it. "
            "Retain the raw Chandra response before deriving the page partition."
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

    ``read_artifact`` verifies the page's inputs, but reopening its blob afterward
    creates a check/use interval. Compare the one immutable byte object handed to
    Pillow/cropping with the page digest so a filesystem swap cannot cross it.
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
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
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

    Older fixture rows mean attempt one explicitly. Silently applying a declared
    first-attempt failure to every re-read would make the test seam a mutable
    outcome selector rather than a description of one attempt.
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


# A Designator page-fallback act used to be recognized here, by its derived
# identity, and given `genuinely-empty` for every configured chair without any
# response boundary being consulted (Sol-S1). Nothing in this stage asks what
# kind of act it is reading any more: a fallback crop is a proposed region like
# any other, its chairs are asked like any other, and what comes back decides
# the outcome. The identity check that guarded the branch went with the branch --
# an unforgeable selector for a branch that must not exist is still the branch.


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

    Two tables reach this boundary and both declare a *response*, never an
    outcome. `[[testimony]]` carries whatever the witness returned;
    `[[witness_empty]]` carries the one response whose whole body is the empty
    string, kept as its own table so a reviewer reading the fixture can see at a
    glance which chairs returned nothing. Either way the declared bytes come back
    through `prepared_response` and `resolve_attempt` derives the outcome from
    what was actually retained.

    The tables handled before this one -- `witness_failure`, `witness_not_run`,
    `witness_malformed` -- are its exact complement: each declares the *absence*
    of a usable response, which is the only thing that may name an outcome with
    nothing retained. That line is what this stage's `genuinely-empty` no longer
    crosses; there is no third shape, and in particular no act identity, that
    mints a completed reading without a response to it.

    Every `witness_empty` row is scenario-scoped, so it sits at the same
    precedence as a scenario-specific `[[testimony]]` row and overrides the
    scenario-agnostic base response for its own scenario exactly as one does --
    that is how a shipped blank scenario says "this chair returned nothing here"
    over the base table's declared text. Two declarations at *that* precedence
    are two answers to one question, and the elif chain would have resolved them
    silently in `witness_empty`'s favour. `declarations_for`'s cross-table check
    cannot see this pair, because `[[testimony]]` is looked up per request rather
    than collected into a declared-pair set.
    """
    response = testimony_for(context, act_key, chair, declarations["ordinal"])
    if (act_key, chair) not in declarations["empty"]:
        return response
    if response is not None and response.get("scenario") == context.scenario:
        raise SchemaRefusal(
            "fixture declares both an empty response and a scenario response for "
            f"{(act_key, chair)!r} at attempt ordinal {declarations['ordinal']}"
        )
    # An empty Chandra response can still carry native layout blocks.  Keep the
    # response declaration as its source: manufacturing a box from the shown
    # page would turn a presentation fallback into reported geometry and let a
    # completed blank witness count as having covered ink it never located.
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

    The generic artifact writer rejects floats and malformed Unicode later, but a
    witness response must become a retained ``failed`` attempt rather than make a
    whole folder crash or be quietly repaired with ``str()``, replacement Unicode,
    or a shared text schema. This is deliberately strict until Spec 04 defines a
    binary provider-body contract.
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


# Every health field of a chair with no native response except the reason, so the
# writer below and the reader in `validate_content_health` cannot disagree about
# what "no response" looks like.
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


# A `genuinely-empty` reading has no witness text at all -- there is nothing to
# locate in the anchor, so nothing was lost finding it either. Shared by the
# trivial-attachment branch below so a zero-length alignment always reads as
# exactly as empty as it is.
_ZERO_ALIGNMENT_LOSS: dict[str, int] = {
    "markup_characters": 0,
    "whitespace_characters": 0,
    "unicode_reencoded_characters": 0,
}


def content_health(native_payload: Any, *, completed: bool | None = None) -> dict[str, Any]:
    """Compute deterministic channel facts from native output alone.

    ``witness_reported`` intentionally is not an argument. A witness's assertion
    that it was confident, complete, or uncertain cannot become health merely by
    being present. The synthetic fixture is an explicit complete transport seam;
    real serving code must pass a trusted response-boundary fact or leave
    truncation unknown.
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

    A self-hashed artifact can still have been re-sealed with a damaged health
    field.  The tally must not count it as known simply because its envelope and
    JSON syntax remain valid.  Whether an unrecordable channel is an accounted
    failure or #23's UNKNOWN is not decided here — that is
    `require_accounted_unrecordable_channel`, which reads the outcome this
    function deliberately does not.
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
        # The narrowest record this stage writes: nothing of what the witness
        # returned could be kept, so there is nothing left to measure and every
        # remaining field is fixed. Leaving them free would let a resealed record
        # take this branch and then assert a character count, a truncation state
        # and a retained payload — self-reported facts wearing the name of the
        # computed ones spec 07 requires these to be.
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

    The final element is a reason that turns this one attempt into ``failed``.
    The raw object is left untouched for every recordable response, including an
    unexpected-but-parseable JSON shape.
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

    ``receipt_ref`` is the live boundary's half: a chair that really served this
    reading already published its receipt when the client started it
    (``ServingManager.start`` -> ``StageContextReceiptPublisher`` ->
    ``StageContext.write_serving_receipt``), so the live pass names *that*
    moment's receipt rather than writing a second, declared one. Absent it the
    fixture path is unchanged: it writes `fixture_serving_details`, which says
    `fixture://` out loud (GOVERNANCE 10).
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
# `page_witness` marks a page chair's act-scoped compatibility record and is
# validated here. `scope` and `page_ordinal` are deliberately NOT listed: they
# belong to the page-scoped kind, which this closed act-level payload never
# carries, and allowing them here let a resealed act record wear page clothing.
# `native_capture` and `serving_call_ref` are the live boundary's two additions
# (SPEC_A section 2.3). Both are written only by the live pass: a fixture attempt
# carries neither, so the fixture record's bytes are exactly what they were.
# `native_capture` was already admitted on a page record by the shared contract
# (`common/native_witness.PAGE_TESTIMONIUM_OPTIONAL_FIELDS`); this admits it on an
# act record too, where a live attempt's own retained model view belongs.
# `raw_response_kind` says what sort of bytes `raw_response_ref` names, because
# on a live record it is two different things: the adapter's own output, when a
# parser read it, and the whole transport body, when no adapter ever saw a
# reading. Both are retained evidence and neither is the other, so the record
# says which rather than leaving a reader to infer it from whether some other
# optional field happens to be present.
OPTIONAL_TESTIMONIUM_FIELDS = frozenset(
    {
        "adapter_metadata",
        "raw_response_ref",
        "raw_response_kind",
        "reason",
        "page_witness",
        "native_capture",
        "serving_call_ref",
    }
)

# A page Testimonium is a different, closed record from the act-scoped
# compatibility Testimonium above.  In particular, ``page_role`` says whether
# the page is the act's primary page, only carries continuations, or contains
# both.  Keeping that fact in the producer's contract prevents page two from
# being an anonymous duplicate of page one.
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
        # Only the derived view joins the payload; the response bytes stay in the
        # named blob the capture already points at.
        record["native_capture"] = native_capture
    if serving_call_ref is not None:
        record["serving_call_ref"] = serving_call_ref

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

    Order is the record's own account of how it was derived, so it is
    preserved; a repeat is not a second response and must not read as one.
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
    """Close the two fields only a live reading writes onto an act Testimonium.

    A retained model view (`native_capture`) is the adapter's own record of the
    bytes it parsed, so it must name the very blob this Testimonium names: two
    references disagreeing about which response was read is a record that cannot
    say what it read. The call record (`serving_call_ref`) is the request half of
    the same moment, and it is meaningless without a retained response beside it
    -- a chair that answered nothing has no reading to account for.

    `raw_response_kind` is the third: a live record's retained blob is the
    adapter's own output on every branch where a parser ran, and the whole
    transport body on the one branch where none could. Those are different
    evidence -- one is the model's answer, the other is an envelope around a
    body that was never a reading -- and a reader that guessed between them
    from the presence of some other field would be reading a record that never
    said. So a record that names a serving call and retains a response must
    name which kind it retained, and a retained model view must agree that it
    is the adapter's own output, because that is the only thing a capture can
    describe.
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
            raise SchemaRefusal(
                "a Testimonium names the serving call that produced it but retains no response; "
                "a request with no retained answer is not evidence of a reading"
            )
        if kind is None:
            raise SchemaRefusal(
                "a live Testimonium retains a response without saying which kind of bytes it "
                "is; the adapter's own output and the transport body are not interchangeable "
                "evidence, and a record that does not say cannot be read as either"
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

    ``field`` names which reference is being re-read. A live Testimonium carries
    two of them -- the response bytes and the `chair-call-record.v1` blob -- and
    a tally that re-hashed only the first would leave the request half of the
    serving moment as a reference nothing checks.
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
    validate_adapter_metadata(payload)
    validate_retained_response_pairing(payload)
    # The closed confidence-ordinal set is a writer-side rule
    # (`_confidence_problem`, applied in `prepared_response` and the Chandra
    # branch of `resolve_attempt`), and this is the one envelope validator both
    # of those writers and the tally/resume read-back share. Checking it again
    # here is what makes it a property of every sealed Testimonium rather than
    # of however many call sites remember to ask -- and, since Unit 2, this
    # validator is also the gate a crash resume trusts before carrying a
    # retained self-report forward into a freshly published record: unchecked
    # here, a malformed claim that ever reached disk would be revalidated as
    # fine and republished rather than refused.
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
        # Only derived text joins the payload; raw bytes remain in the named blob.
        record["native_capture"] = native_capture
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
    return validate_shared_page_testimonium_payload(payload, testimonium_id=testimonium_id)


AttemptHistory = dict[tuple[str, str], list[dict[str, Any]]]


class AttemptIndex(NamedTuple):
    """This stage's own prior output, indexed once per invocation."""

    stage_has_artifacts: bool
    by_pair: AttemptHistory
    attachments_by_act: dict[str, list[dict[str, Any]]]


def _attempt_history(context) -> AttemptIndex:
    """Index immutable Testimonia and derived attachments once for this invocation.

    The independent tally deliberately rebuilds and validates the inventory for
    accounting. This index serves only append/collision decisions, whose repeated
    per-pair manifest walks otherwise make a pass quadratic in the folder size —
    which is why the derived act-attachments travel in the same walk rather than
    in a second one per act.
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
        # No `payload["scope"] == "page"` skip here. Page-scoped Testimonia are a
        # kind of their own and the filter above already excludes them, so the
        # skip could only ever fire for an act-scoped record that *claimed* page
        # scope — and it would then drop that record out of the append/collision
        # history on the strength of one self-reported field, which is exactly
        # the disguise F-O5 closed in `attempt_tally`. The identical line was
        # left standing here. Found in fresh-context review (P2).
        chair = payload.get("chair") if isinstance(payload, dict) else None
        if isinstance(chair, str):
            by_pair.setdefault((entry["subject_id"], chair), []).append(record)
    return AttemptIndex(bool(manifest["artifacts"]), by_pair, attachments_by_act)


def require_appendable_ordinal(
    history: AttemptHistory, act_id: str, chair: str, ordinal: int
) -> None:
    """Allow only a rerun of an attempt that exists, or exactly the next one.

    Ordinals are the contiguous run 1..N — `latest_attempt` refuses a gap — so any
    ordinal at or below the current one names an attempt that is already on disk,
    and rewriting it is a resume: the RunTree refuses it outright if the bytes
    differ. Only `current + 1` adds anything.

    The bound is `<= current + 1` rather than `in {current, current + 1}` because a
    targeted reread moves one chair's ordinal without moving any other's. Insisting
    every pair be at the same ordinal would mean the orchestrator — which always
    asks for ordinal 1 — held the whole folder from the moment one chair was
    reread, over writes that would every one have been byte-identical no-ops.
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

    A targeted reread and a whole pass can reach the very same identity with a
    different honest outcome — `resolve_attempt`'s docstring says so plainly: an
    undeclared response is `failed` under a reread and `not-run` under a whole
    pass, because the two write paths mean different things by silence.
    `RunTree.publish_artifact` already refuses a colliding write, but only when
    it is reached, mid-pass, after every earlier pair in this invocation has
    already been published — a half-written attempt layer whose stored manifest
    was never rewritten to describe it. Checking every pair against what already
    exists, before any of them is written, keeps a doomed pass from writing a
    Testimonium rather than stranding the folder partway through. Native response
    bytes are deliberately retained before parsing and therefore stay as
    content-addressed custody even when this later comparison refuses.

    Only a pair whose target ordinal already holds a record can collide;
    `require_appendable_ordinal` already refuses any ordinal beyond that, so
    this only ever compares against a genuine resume attempt. Compared on the
    fields the two write paths can actually disagree on, not the full sealed
    envelope: provenance does not vary with the `reread` flag, only what
    `resolve_attempt` decided did.
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
        # The parsed text is not the native response. Two Chandra bodies may
        # produce the same text while carrying different layout blocks, and the
        # raw digest is what binds the geometry this attempt will publish. A
        # resume that compared only the text discovered that collision later at
        # the immutable writer, after earlier pairs had already been published.
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

    The bound in `require_appendable_ordinal` admits both, deliberately — a
    targeted reread moves one chair's ordinal without moving any other's, so the
    orchestrator's ordinal-1 pass has to stay a byte-identical resume over a
    folder one chair has been reread in. The rules that close an act's witness
    layer apply to the append and not to the repeat, so they need this told apart
    rather than assumed.
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

    The whole pass is a run-level instrument: every configured chair, every
    expected act, at one ordinal. It also re-derives each act's act-attachment at
    that same ordinal. A targeted reread moves exactly one chair and re-derives
    the attachment one ordinal above whatever it found, so after a reread the
    whole pass's next attachment ordinal is already taken — by a record describing
    a different state.

    `RunTree.publish_artifact` would refuse that write correctly but *late*: in
    the derived-record loop, after every Testimonium of the pass had been written
    and before `context.finish()` rewrote the inventory to describe them. The
    folder would then hold attempts its own manifest does not name, which the next
    pass can only report as UNKNOWN. Refused here instead, before anything is
    written.

    The condition is the model rather than the mechanism: **a targeted reread
    takes its act off the shared whole-pass ordinal**, and an act is off it
    exactly when its chairs no longer agree on one current ordinal. A pass that
    only *repeats* attempts already sealed is untouched (see `pass_would_append`),
    which is what keeps the orchestrator's ordinal-1 resume working over a folder
    one chair has been reread in, and a partly-lost attempt layer — where some
    pairs have no record at all — is a repair at the ordinal its surviving pairs
    already share, not a mixed act.

    One residual is deliberately left to the RunTree rather than checked here:
    reread *every* chair on one act up to the same ordinal and the act agrees
    again — and a whole pass at that ordinal is then a REPEAT, so
    `pass_would_append` skips both new rules entirely and the pass proceeds to
    the attachment derivation, whose next ordinal the rereads already took.
    Reaching that collision needs each chair's whole-pass attempt to be
    byte-identical to its reread attempt — otherwise `_refuse_write_collision`
    stops the pass first — so the pass that survives had nothing to add. The
    outcome is a loud fatal refusal with `RunTree.write_manifest` as the recorded
    one-step recovery, and a check for it would cost a second derivation of every
    attachment in preflight to close a case whose worst outcome is a noisy stop.
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
) -> tuple[
    dict[str, tuple[list[dict], str | None]],
    dict[tuple[str, str], "Attempt"],
    frozenset[tuple[str, str]],
]:
    """Refuse a damaged history, or a colliding write, before adding any new
    attempt to this invocation.

    The ordinal bound alone is not enough: it lets a whole pass through at an
    ordinal a targeted reread has already sealed a *different* record at for one
    chair, and the collision then surfaces reactively, mid-pass, at whichever
    pair the loop reaches it on — see `_refuse_write_collision`. The caller
    supplies one declaration set for both this preflight and `attempt_pass`,
    since the two must agree on what this ordinal means. The returned region map
    lets publication use the exact regions preflight already verified instead of
    walking and hashing Designator again.

    **One ordinal for the whole pass, on a resume exactly as on a first run.**
    Unit 2 asks what a crashed pass does about the pairs it already sealed, and
    the answer is not a per-pair ordinal map. Three facts forbid one. A
    page-scoped chair has no act-scoped attempt to repeat at all — `reread_pass`
    refuses to mint one by name — so moving such a pair to ordinal 2 while the
    page Testimonium and the act attachment stay at ordinal 1 publishes a
    `not-run` over a good `read` and drops that chair out of the act's coverage.
    `require_shared_whole_pass_ordinal` exists precisely because an act's
    attachment is derived once per pass and cannot describe two ordinals at
    once. And the Perlector's reading ordinal is a function of the act's crop
    history, which the Recensor, Archetypus and Armarium each re-derive, so a
    reading that appears without a recrop is refused downstream by all three.

    So a resumed whole pass repeats its ordinal, but it never asks a chair again
    for a pair already sealed at that ordinal. The retained Testimonium is
    validated and supplies the attempt facts used by the derived page and
    attachment records; only pairs the interrupted pass did not seal reach the
    chair. This is what makes the resume safe for a real non-deterministic chair:
    no second answer has to reproduce immutable bytes, and no later ordinal is
    invented for an act the pass never finished (GOVERNANCE 2, 4).

    **`resolve` is what keeps this preflight a no-write preflight when the chair
    is live.** In fixture mode it is `resolve_attempt`, which reads the sealed
    fixture and needs nothing outside this process. A live chair cannot be
    consulted here at all -- that would be N x M model calls with nothing on
    disk until the last one returned -- so the live pass supplies a resolver
    that returns `PENDING_LIVE_ATTEMPT` for every unsealed pair and fills each
    in as its own response arrives. The live caller also passes
    `resume_incomplete_pass=True` unconditionally: a pair already sealed at this
    ordinal is reused from its retained Testimonium and never asked again,
    because a live chair cannot reproduce immutable bytes (GOVERNANCE 4).
    """
    resolve = resolve_attempt if resolve is None else resolve
    # Native declarations must refuse before compatibility records are published.
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
        # The whole pass is the other write path that can add testimony to an act,
        # so it meets the same closed-layer rule the targeted reread does. Only an
        # append: a whole pass that rewrites attempts already sealed at this
        # ordinal is the orchestrator's ordinary resume, moves no chair's current
        # record, and is untouched by this.
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
                # Seeded only when non-empty: `validate_tallied_testimonium`
                # re-derives a missing entry and names the absent proposal crop,
                # and an empty list would suppress that named refusal.
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
                # Nothing to compare: the branch above reuses every pair already
                # sealed at this ordinal whenever a resolver of this kind is in
                # play, so a pending pair has no record here to collide with.
                # The live pass checks nothing further before publishing because
                # there is nothing there to check -- and if that ever stopped
                # being true, this is where it would have to be caught.
                if existing:
                    raise FatalAccounting(
                        f"the live preflight left {pair!r} unresolved while a Testimonium is "
                        f"already sealed at ordinal {ordinal}; a sealed pair is reused, never "
                        "asked again"
                    )
                continue
            _refuse_write_collision(index.by_pair, act, chair, ordinal, attempt)
    # Last, so a genuine witness-attempt disagreement is named for what it is
    # rather than reported as its consequence one derivation downstream: the
    # attachment collides *because* a reread already sealed an attempt this pass
    # would contradict, and where that contradiction exists `_refuse_write_collision`
    # says which chair and which two outcomes.
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

    The generic envelope proves a record is syntactically sealed; the attempt
    tally also has to prove its stage-specific channel remains interpretable
    before authorizing another immutable append. This deliberately validates no
    witness's *content* and makes no quality decision. `regions_by_act` retains
    that independent verification once per act while the tally checks each chair.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium tally record has no object payload")
    validate_testimonium_payload(payload)
    if "raw_response_ref" in payload:
        validate_retained_response_blob(context.tree, payload["raw_response_ref"])
    if "serving_call_ref" in payload:
        # The live half of the same rule. `inputs` is re-derived below from the
        # regions and the presentation alone, so a live record's two retained
        # blobs are deliberately not envelope inputs -- which is exactly why the
        # tally re-hashes them itself rather than leaving them as references
        # nothing ever reads back.
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
        if payload["regions"] != region_references(regions) or record[
            "inputs"
        ] != testimonium_inputs(context, regions, payload["presented"]):
            raise SchemaRefusal(
                "a Testimonium tally record does not bind exactly the proposal regions and inputs"
            )
        # Re-derive the explicit limit from sealed regions so it cannot understate
        # which bound crops the one presented image does not speak for.
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

    Two of spec 07's requirements look contradictory here and are not, because
    they are about different channels. Its isolation bullet — "one bad crop, one
    dead witness, one malformed response never kills the folder ... recorded as a
    failed attempt and refused, not repaired silently" — is about one witness's
    own output. Invariant #23's — "a damaged or unrecordable evidence channel
    makes the count UNKNOWN, and UNKNOWN holds the folder" — is about the attempt
    tally, the independent count of what was attempted.

    So an unrecordable response is accounted inside an honestly `failed` attempt
    that says why: countable, counted, visibly failed, its act under-witnessed and
    its run partial. Holding the whole folder instead would stop the Perlector
    reading ink nobody doubts because one witness of three failed. Only a record
    claiming to be a *reading* while saying nothing could retain what it read is
    incoherent, and that one is #23's UNKNOWN.
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

    The stored manifest is derived state, not the evidence: it is checked against
    a fresh tree walk and the immutable Testimonia. An unreadable, malformed,
    missing or divergent inventory makes the count UNKNOWN, and a caller must hold
    rather than turn that uncertainty into a favourable count.

    A witness whose own output could not be retained is a different fact and does
    not make the count unknown — see `require_accounted_unrecordable_channel`.

    `chairs` supplies the act/chair denominator and is optional independently of
    `acts`, because *whether every pair is accounted for* is a closing check and
    not a precondition. Demanding it before a pass deadlocks the one thing that
    could satisfy it: a pass interrupted before its manifest was written leaves a
    partial inventory, and the pass that would complete it was refused on the
    grounds that it was incomplete. Every record on disk is still validated
    either way; only the denominator moves.
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
        # RecursionError beside the others for the same reason `_read_json` in
        # common/runtree/store.py added it: json's scanner recurses per nesting
        # level, so a stored manifest an attacker or a damaged write replaced with
        # deeply nested JSON raises it here directly, on this stage's own read,
        # rather than through the shared reader. Uncaught, that is a traceback
        # where #23 promises UNKNOWN + hold.
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    if stored != rebuilt:
        return {
            "state": "UNKNOWN",
            "count": None,
            "hold": True,
            "reason": "the stored Attestatores manifest does not equal its rebuilt inventory",
        }

    # Page-scoped Testimonia are independently retained source evidence under
    # their own kind, so this kind filter keeps the act-level walk to act
    # attempts alone. There was also a `payload["scope"] == "page"` skip inside
    # the loop below, which this filter made unreachable -- and which, had
    # anything reached it, would have carried an act-scoped record past every
    # check in this function on the strength of one self-reported field. Found
    # in audit; F-O5.
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
        # `FatalAccounting` is a `ContractError`, and `latest_attempt` raises it in five
        # places directly inside this block — so without this the broadest handler here
        # turned invariant #10 into a hold. The two are not interchangeable: a hold says
        # *the count is unknown*, while an accounting imbalance says *the partition
        # itself is broken*, and the error class exists to keep those apart. Its own
        # docstring is the rule — "nothing may catch this and carry on". Two other sites
        # in this file already re-raise it the same way; this one was missed. Found by
        # CodeRabbit reviewing the rebased branch.
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

    One shape shared by every constructor — `dead_attempt`, `not_read_attempt`,
    and `resolve_attempt` — so the whole-pass write path and the targeted reread
    cannot drift on what a witness attempt is. It describes one chair and reads
    no other chair's record: nothing here compares, ranks, or chooses among
    witnesses, and there is no argument through which it could.
    """

    outcome: str
    native_payload: Any
    witness_reported: Any
    format_capabilities: dict[str, Any] | None
    health: dict[str, Any]
    reason: str | None
    raw_response_ref: dict[str, str] | None = None
    observation_payload: Any = None
    # Live-only, and appended so every existing constructor -- positional or
    # keyword -- keeps writing exactly the record it wrote before (the fixture
    # pass must stay byte-identical). `native_capture` is the adapter's own
    # retained model view; `serving_call_ref` names the `chair-call-record.v1`
    # blob the client wrote for the one request this attempt came from.
    native_capture: dict[str, Any] | None = None
    serving_call_ref: dict[str, str] | None = None
    receipt_ref: dict[str, str] | None = None
    # Which sort of bytes `raw_response_ref` names. `None` on the fixture path,
    # which retains one kind of declared bytes and has no branch that could
    # mean the other.
    raw_response_kind: str | None = None


class _PendingLiveAttempt:
    """The live pass has not asked this chair for this pair yet.

    Deliberately not an `Attempt` with a `pending` outcome: an outcome is a
    published fact, and a sentinel that can be published is a sentinel that
    eventually is. Nothing here can be mistaken for a witness result -- it has no
    outcome, no payload and no health -- so a code path that fails to replace it
    fails loudly at the first attribute it reaches.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "PENDING_LIVE_ATTEMPT"


PENDING_LIVE_ATTEMPT: Any = _PendingLiveAttempt()


def pending_live_attempt(context, act, chair, resolved, declarations) -> Any:
    """The live preflight's resolver: every unsealed pair is still unasked.

    Same signature as `resolve_attempt`, because it stands exactly where that
    function stands and the seam must not grow a second shape. It reads none of
    its arguments: in live mode the fixture's declared responses are not this
    run's evidence, and the chair is asked once, later, from the pass that can
    publish its answer immediately.
    """
    del context, act, chair, declarations
    if isinstance(resolved, AbsentChair):
        # An absent chair is unavailable before any attempt reaches it, in either
        # posture. Leaving it pending would put a chair the roster says is not
        # there into the live schedule, and the first thing the schedule does is
        # try to start it.
        return dead_attempt(resolved)
    return PENDING_LIVE_ATTEMPT


def _attempt_from_retained_testimonium(tree, record: dict[str, Any]) -> Attempt:
    """Crash resume must rehydrate retained Chandra bytes for page geometry.

    The act-scoped Testimonium is not republished, but its response still feeds
    the derived page record. Its blob therefore has to remain present and
    digest-identical before that Testimonium can stand in for a new chair response.
    """
    payload = record["payload"]
    raw_response_ref = payload.get("raw_response_ref")
    observation_payload = None
    # A live attempt whose response never parsed carries no observation payload:
    # `live_witness.captured_page_attempt` sets one only on the parsed branch,
    # because geometry derived from bytes no parser recognized would be geometry
    # nobody read. The resume must reconstruct the same fact, or the page record
    # it rebuilds derives a partition the interrupted pass never wrote and the
    # immutable writer refuses the republish. `serving_call_ref` is the live
    # marker; the fixture path retains its declared bytes on every branch and is
    # deliberately untouched here.
    served_by_a_chair = payload.get("serving_call_ref") is not None
    parsed_into_a_payload = (
        isinstance(payload.get("content_health"), dict)
        and payload["content_health"].get("recordable") is True
    )
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
        # Read and digest-checked either way -- the blob must still be there and
        # still be itself before this record can stand in for a chair response.
        # Only whether it is offered as *geometry* depends on the branch above.
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
        # Carried back so a resumed live pass can rebuild the page record its
        # interrupted predecessor derived from this same response, without ever
        # asking the chair a second time (`live_page_capture`).
        native_capture=payload.get("native_capture"),
        serving_call_ref=payload.get("serving_call_ref"),
        receipt_ref=provenance.get("receipt_ref") if isinstance(provenance, dict) else None,
        raw_response_kind=payload.get("raw_response_kind"),
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
    # The registry's churro retain wrapper pins the adapter name itself; the
    # relabel-proof seam accepts no adapter argument at all.
    capture = adapter.retain(
        context.tree,
        view={"prompt": adapter.prompt(), "generation": feeding.churro_generation()},
        raw_response=raw,
        transport_stop_reason=stop,
        parser="xml",
    )
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
                DEFAULT_FORMAT_CAPABILITIES,
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
                DEFAULT_FORMAT_CAPABILITIES,
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
    basis = (
        f"response cut off by the provider ({stop!r}); {parsed['reason']}"
        if cut_off
        else parsed["reason"]
    )
    return (
        Attempt(
            "failed",
            None,
            None,
            DEFAULT_FORMAT_CAPABILITIES,
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
            f"Churro response retained but not usable: {cut_note}{parsed['reason']}",
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

    An absent chair stays `dead` whatever kept the act from being read, because
    the two facts are independent — holding the act does not turn an unreachable
    witness into a merely unasked one.
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

    Read once per pass rather than once per chair, and bound to the ordinal, so a
    declared first-attempt failure cannot silently describe a later reread.

    `empty` is a declared empty *response* — the fixture's stand-in for a
    provider that returned an empty body — and `declared_response` sends it back
    through the same retention boundary as any declared text, so the outcome is
    derived from what was retained rather than named here. `not_run` is a
    configured chair deliberately never asked for this attempt.
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

    Every branch ends in exactly one member of the closed six-outcome vocabulary,
    because a chair that simply does not appear is the silent skip this stage
    exists to refuse.

    `reread` decides which member an undeclared response lands on, and the two
    write paths genuinely differ there. A whole pass asks the fixture what each
    chair returned at this ordinal, and silence means the chair was not asked:
    `not-run`. A targeted reread names one chair on one act, so the invocation
    *is* the attempt and silence is an attempt that produced no usable
    Testimonium — which spec 07 gives to `failed`, "as against ... `not-run`
    (configured, never attempted)".
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
            if "raw_response" in response and resolved.witness_adapter != "chandra.v1":
                raise SchemaRefusal(
                    f"fixture raw_response has no native byte route for adapter "
                    f"{resolved.witness_adapter!r}"
                )
            if "raw_response" in response and not isinstance(response["raw_response"], str):
                raise SchemaRefusal(
                    "fixture raw_response is not text encoding retained response bytes"
                )
            if resolved.witness_adapter == "chandra.v1" and isinstance(
                response.get("raw_response"), str
            ):
                # Geometry may derive only from explicitly declared response
                # bytes; synthesizing JSON from `payload` would create native
                # evidence no witness returned.
                raw_response = response["raw_response"].encode("utf-8")
                adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")
                retained = adapter.retain(
                    context.tree,
                    # No adapter argument: Chandra's retain wrapper pins its own
                    # registry identity, as Churro's and DAI's do.
                    view={"prompt": adapter.prompt()},
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
                # The health the response boundary produced is kept as it came.
                # Recomputing it here repeated the same call for every
                # recordable payload, and on the unrecordable branch -- where
                # `prepared_response` returns a `None` payload with the honest
                # unrecordable health -- it would have overwritten that with the
                # health of `None`, reporting a recordable null channel and
                # leaving the unrecordable-channel accounting with nothing to
                # fire on (GOVERNANCE 2).
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
                # Derived from the response this chair actually returned, never
                # asserted about it. `genuinely-empty` is the one completed
                # outcome whose whole content is an absence, so it is the one
                # most easily minted from something other than evidence -- and
                # it used to be: a Designator page-fallback act took this
                # outcome for every configured chair from its own derived
                # identity, before any response boundary was consulted at all
                # (Sol-S1). Nothing reaches here without a retained, recordable
                # response to this exact request; a missing one is the
                # `not-run`/`failed` above and holds the act.
                outcome = "genuinely-empty"
            else:
                outcome = "read"

    return Attempt(outcome, native_payload, witness_reported, capabilities, health, reason)


def declared_page_witness_chairs(context) -> set[str]:
    """Read page-witness scope from the sealed model configuration.

    Unit 10A moves the source of truth here: scope is `witness_scope` in the
    sealed roster, never a fixture declaration a run could contradict. This
    producer must not accept fixture scope or delegate roster validation to the
    Perlector; each side of the handoff checks the sealed authority itself.

    All write and reread paths use this accessor, because a downstream page-join
    check cannot repair an immutable act record that already carries the wrong
    scope.
    """
    roster = context.witness_chairs
    # `type(chair) is not str` rather than `isinstance`: set construction and
    # refusal formatting below both invoke subclass-defined behaviour, so the
    # exact built-in string is required first (work/boundary-named-refusals).
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

    ``live`` says the response came from a chair that actually served it. It
    changes exactly one derivation: a fixture `[[native_observation]]` row is a
    declared stimulus for the offline posture, and letting one stand in for the
    geometry a live response really carried would record a measurement nobody
    made (GOVERNANCE 10). Everything else here is identical in both postures,
    which is what keeps the fixture record byte-for-byte what it was.
    """
    # This shared accessor must run before building any artifact facts: a bad
    # roster cannot be allowed to seal an otherwise plausible record first.
    page_witness_chairs = declared_page_witness_chairs(context)
    attempted = attempt.outcome in ATTEMPTED_WITNESS_OUTCOMES
    # One presentation has one page-pixel space. Continuation crops remain bound
    # in `regions`/`inputs`, while `unpresented_regions` explicitly prevents the
    # derived geometry from looking complete across pages it cannot describe.
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
    # A declared observation is the offline posture's stimulus. A live response
    # carries its own geometry, and letting a fixture row stand in for it would
    # publish a measurement nobody made (GOVERNANCE 10).
    fixture_observed = (
        _fixture_native_observations(
            context, chair=chair, page_ordinal=presented["source_page_ordinal"]
        )
        if presented and not live
        else None
    )
    if not presented:
        observed: list[dict[str, Any]] = []
    elif fixture_observed is not None:
        observed = fixture_observed
    elif adapter is not None:
        observed = adapter.observe(
            presented,
            attempt.observation_payload
            if attempt.observation_payload is not None
            else attempt.native_payload,
        )
    else:
        observed = observed_from_presentation(presented)
    if (
        presented
        and isinstance(resolved, ChairIdentity)
        and resolved.witness_adapter == "chandra.v1"
    ):
        # The act view cannot retain partition findings, but it must exclude an
        # overshoot so one bad block does not prevent the page record retaining it.
        observed, _ = split_page_edge_overshoots(
            observed, page_size=_sealed_source_page(context, presented)[2]
        )
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
        # The interim act view of an immutable page witness carries the flag
        # from construction so the geometry contract validates it correctly.
        # Its attachment points at the retained page Testimonium; R4 replaces
        # this view with alignment, not with another witness kind.
        page_witness=chair in page_witness_chairs,
        reason=attempt.reason,
        raw_response_ref=attempt.raw_response_ref,
        raw_response_kind=attempt.raw_response_kind,
        adapter_metadata=declared_adapter_metadata(
            resolved, has_raw_response=attempt.raw_response_ref is not None
        ),
        native_capture=attempt.native_capture,
        serving_call_ref=attempt.serving_call_ref,
    )
    inputs = testimonium_inputs(context, regions, presented) if attempted else []
    # Adapter output is untrusted. Reconcile it while refusal can still leave
    # the immutable Testimonium identity unwritten; tally/consumer validation
    # repeats this check but cannot undo an invalid publication.
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

    `align_to_anchor`'s matching runs on the normalized (whitespace-collapsed)
    text, so a matched block's `start`/`end` are normalized-text offsets. Storing
    them as-is under `witness_span` -- which every later reader (this stage's own
    `span` field, and the Recensor's page-Testimonium content-coverage check)
    indexes into the RAW page text -- silently shifts by however much leading or
    internal whitespace the normalization collapsed. `offset_map[i]` is `None`
    only for a synthesized separator character with no raw counterpart, so the
    real span is the min/max raw index actually mapped inside the range.
    """
    raw_indices = [
        offset_map[index] for index in range(start, end) if offset_map[index] is not None
    ]
    if not raw_indices:
        return None
    return min(raw_indices), max(raw_indices) + 1


class PageJoin(NamedTuple):
    """R0's synthetic page reading for one chair: the text, what it amounts to,
    and every act attempt the join could not carry."""

    native_payload: str
    outcome: str
    unjoined_act_attempts: list[dict[str, Any]]
    # How many attempts the join DID carry. Carried rather than derived, because
    # `page_failure_reason` cannot tell "some acts joined empty" from "nothing
    # joined at all" out of the unjoined list alone, and the difference is the
    # difference between a page read as blank and a page not read.
    joined_act_attempts: int


def page_failure_reason(unjoined_act_attempts: list[dict[str, Any]], joined: int) -> str:
    """Why a page record failed, derived from the unjoined attempts' own outcomes.

    **Never from their count.** Two different things land in
    `unjoined_act_attempts`: an attempt that was not a reading at all, and a
    reading this chair genuinely delivered as a structured native object that the
    synthetic text join cannot concatenate (`page_join` above spells the second
    out in each row's own `reason`). Counting them together called both "unread",
    which is false of the second and sends an operator hunting a provider failure
    that never happened — the same defect as the "page witness had no recordable
    response" wording this stage already replaced, one case over.

    So the page-level reason reads the partition the rows already carry rather
    than re-deriving a worse one from a length comparison.

    **`joined` is needed and is not derivable from the list.** The first version of
    this function reported "the page join carried only empty readings" for a page
    where *nothing* joined — every attempt a failure, no reading of any kind — which
    is the same misdescription one case further along, introduced by the commit that
    fixed the previous one. An unjoined list of non-readings is identical in both
    cases; only the joined count separates a page read as blank from a page not read
    (CodeRabbit CLI, PR #63).
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
        # One case further along the same road the docstring above walks twice.
        # A page whose chair was never served at all reaches here with every row
        # `not-run` or `dead`, and "N attempts, none of them carrying a reading"
        # describes requests that were made and came back useless -- sending an
        # operator to look for responses that were never asked for. `not-run` at
        # page scope means no request reached the chair, and the reason has to
        # say that rather than borrow the attempted wording.
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

    Only a genuine reading contributes text. An attempt whose *outcome* is
    `failed` can still carry a parsed `native_payload` string (a bad
    `witness_reported`/`format_capabilities` fails the whole attempt without
    clearing the text `prepared_response` already parsed) -- filtering on
    `isinstance(..., str)` alone let that failed act's own text be silently
    folded into a page witness's "read" testimony, laundering a recorded failure
    into apparent coverage (D2/D3; GOVERNANCE 2). Found in audit; F-S1.

    The disclosure is the exact complement of that filter, computed from one
    partition rather than from a second predicate. They were two predicates, and
    they did not agree: the join also dropped an act whose reading is a
    structured native object (a dict or list rather than text), while the closed
    `unjoined_act_attempts` list named only non-reading OUTCOMES. In the shipped
    `structured-witness` scenario that made attestator_1's page-1 record report
    `read`, carry act a2's text alone, and disclose nothing -- act a1 gone behind
    a successful status, which is F-P3's own defect through F-S1's own door.
    Found in audit; F-O7.

    **A separator is not a reading.** The join used to be `"\n".join(readable)`
    over every joined payload including the empty ones, and the outcome was
    `read` whenever `readable` was non-empty -- so a page whose every act this
    chair genuinely read as empty produced `payload="\n"` and a `read` page
    Testimonium: characters no act delivered, under an outcome claiming a
    reading of them (CodeRabbit W44). Separators are therefore placed only
    *between* delivered characters, and the outcome is derived from the joined
    text rather than from the length of the list that produced it:

    - `failed`: no act reading joined and at least one underlying attempt reached
      the chair. The page has no reading, but the attempted serving receipt still
      travels with the failed record.
    - `not-run`: no act reading joined because none of the underlying requests was
      attempted. It carries neither a presentation nor a receipt.
    - `genuinely-empty`: acts joined and every one of them delivered an empty
      body. The chair read the page's acts and reported nothing on each, which
      is the same fact at page scope that the act-scoped outcome records, and
      `payload=""` is what `genuinely-empty` means everywhere in this stage.
    - `read`: the joined text carries at least one delivered character.

    A joined-but-empty act is not listed in `unjoined_act_attempts`: it was
    carried, faithfully, and its zero characters are in the text. What it read
    stays visible in its own act-scoped Testimonium and in the act attachment.
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
        # A completed absence is claimed only over a page this chair's join
        # fully carried: `genuinely-empty` says "read the page's acts and
        # reported nothing on each", and an act the join could not carry is an
        # act this record did not read — a proved absence over unread ground
        # would be the fabrication defect one scope up (invariant 6). `read`
        # beside unjoined rows stays honest because delivered characters plus
        # a disclosure claim less, not more. Nothing seals on a page record's
        # outcome today; the act-scoped Testimonia carry the read-empty facts
        # either way.
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
                # A non-reading attempt always carries its own reason. A reading
                # the join could not carry has none to borrow, and an omission
                # with no reason is the silent loss this list exists to refuse --
                # so the join states its own limit instead.
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

    The page-wide matcher runs once per (page, chair) and each act clips its own
    hull out of it, so two acts can end up claiming overlapping stretches of one
    chair's page reading.  Both claims are then unattributable: choosing between
    them by span size, act order, or overlap fraction would be a picker over one
    witness's text (GOVERNANCE 3, hard rule 8), and letting both stand would feed
    the same characters to two acts' dissent rows as though the witness had said
    them twice.

    So neither wins.  Both stay geometrically attached -- the chair really did
    report ink there -- while their text correspondence becomes explicitly
    unaligned with a named reason, and neither can count toward the witness
    floor.  A zero-width span (the trivial attach a genuinely-empty page reading
    gets) touches nothing and is deliberately not an overlap.

    Extracted from the attachment pass so it can be exercised directly: the
    combination needs one chair's page reading to match one act's anchor range in
    two separate places, which no fixture currently produces.
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
            entries[index]["alignment"] = {
                "status": "unaligned",
                "reason": "ambiguous-overlapping-act-alignment",
            }
            entries[index]["span"] = None
            entries[index]["comparable"] = False


def act_scoped_attachment_entry(
    context,
    act: dict[str, Any],
    chair: str,
    attempt: "Attempt",
    ordinal: int,
) -> dict[str, Any]:
    """One act-scoped chair's derived attachment view of one attempt.

    Shared by the whole pass and the targeted reread, because both derive the
    same view of the same per-(act, chair) attempt stream and a second spelling
    is how the two would come to disagree about what the derived record says —
    the drift F-O1 was, one layer down.

    An act-scoped chair reads the act crop directly, so there is no page reading
    to place it inside and `alignment` is deliberately absent. The span is this
    chair's own complete delivered reading, the interim measure that stands until
    R4's alignment computes a true covered span (F-S2: it is derived from the
    response, never fixture-declared).
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

    A native page capture owns the page Testimonium's outcome.  The legacy
    synthetic join instead gates this act on its own act attempt, because the
    joined page record can still read on the strength of a different act.
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

    Under a live posture there is no second place a page record could come
    from. Falling through to the legacy act-attempt join would publish a page
    record derived from act views while the chair really did answer once, for
    the page -- a record about a response nobody made (GOVERNANCE 10).
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

    One derivation, two readers. The page publisher needs it to know which page
    records to write; the live pass needs the same answer *before* it asks a
    page-scoped chair anything, because a page is that chair's unit of work.
    Deriving it twice would be two answers to "which pages does this act
    contribute?" that can drift, and the drift would show up as a page record
    published for a page nobody was asked about.
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

    R0 uses each successful chair's complete delivered act reading as an interim
    span so the custody chain is real before R4 owns text alignment. The fixture
    declares no spans. The act-scoped records for chairs 1 and 3 remain a temporary
    compatibility view for the current Perlector; each is explicitly linked below
    to the immutable page Testimonium that supplied it.
    """
    # Scope is authoritative only after the sealed roster and configured
    # occupants agree.
    page_chairs = declared_page_witness_chairs(context)
    anchor_chair = declared_chandra_anchor_chair(context)
    # Declared Churro responses are validated in the attempt preflight now,
    # before any compatibility record publishes.
    limits, limits_digest = load_alignment_limits(context.args.alignment_config)
    context.require_sealed_config("alignment", limits_digest)
    page_records: dict[tuple[int, str], dict[str, str]] = {}
    page_observations: dict[tuple[int, str], list[dict[str, Any]]] = {}
    page_texts: dict[tuple[int, str], str] = {}
    # Native captures own their page outcome; legacy joins derive it from act
    # attempts, so the two paths cannot share an attempt-object fallback.
    page_outcomes: dict[tuple[int, str], str] = {}
    # The anchor is a page fact, not a chair's report, and it is kept in its own
    # map for that reason: parked in `page_texts` under a reserved chair slot it
    # shared a key space with the configured roster, so a chair carrying that
    # name would have had its retained page reading silently overwritten by the
    # anchor markup and then been aligned against itself.
    anchor_texts: dict[int, str] = {}
    page_alignments: dict[tuple[int, str], dict[str, Any]] = {}
    anchor_ranges: dict[tuple[int, str], dict[str, int]] = {}
    contributing_pages_by_act, by_page = page_denominator(context, acts, regions_by_act)

    for page_ordinal, page_acts in sorted(by_page.items()):
        page_subject = page_identity(context.fixture, page_ordinal)
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
                # Legacy fixture rows retain their deliberately synthetic join.
                # A Churro row above never takes this path.
                join = page_join(
                    [(act, attempts_by_pair[(act["act_id"], chair)]) for act in page_acts]
                )
                page_attempt_result, native_capture = join, None
                native_payload, outcome = join.native_payload, join.outcome
                unjoined_act_attempts = join.unjoined_act_attempts
            else:
                # Not `page_attempt`: that name is rebound below to this page
                # record's attempt *identity* string, and one name meaning both
                # an Attempt and an attempt id is how a page record ends up
                # published under the wrong identity when this loop is edited.
                page_attempt_result, native_capture = captured
                native_payload, outcome = (
                    page_attempt_result.native_payload,
                    page_attempt_result.outcome,
                )
                unjoined_act_attempts = []
                page_outcomes[(page_ordinal, chair)] = page_attempt_result.outcome
            reading = outcome in WITNESS_READING_OUTCOMES
            # Did a response actually arrive for this page? A retained capture
            # says so, and so does a live capture whose wire body `ChairClient`
            # could not parse into a reading at all -- that body arrived, was
            # retained, and produced an unrecordable channel, which is a
            # different fact from a chair that answered nothing (GOVERNANCE 2).
            # In the fixture posture `page_captures` is None and this is exactly
            # the `native_capture is not None` it has always been.
            arrived = native_capture is not None or (
                page_captures is not None and captured is not None
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
            # Only a retained reading. A failed native capture has no text, and
            # a `None` parked here read back as "this page has no anchor" three
            # hundred lines below. The page-attempt gate below keeps that lookup
            # from being reached; this keeps the map's own type honest.
            if isinstance(native_payload, str):
                page_texts[(page_ordinal, chair)] = native_payload
            health = (
                page_attempt_result.health
                if captured is not None
                else content_health(native_payload, completed=reading)
            )
            presented = presentation_for_page(context, page_ordinal) if attempted_page else {}
            adapter = (
                witness_adapters.resolve_runnable_adapter(resolved.witness_adapter)
                if attempted_page and isinstance(resolved, ChairIdentity)
                else None
            )
            if adapter is not None:
                source_presentation = presented
                presented = adapter.present(context, source_presentation)
                witness_adapters.validate_adapter_presentation(
                    resolved.witness_adapter, source_presentation, presented
                )
            unpresented_regions = unpresented_region_ids(presented, page_proposal_regions)
            page_attempt = attempt_id(page_subject, f"read:{chair}", ordinal)
            roles = {
                "primary" if act["page_ordinal"] == page_ordinal else "continuation"
                for act in page_acts
            }
            page_role = roles.pop() if len(roles) == 1 else "mixed"
            page_response_refs: list[dict[str, str]] = []
            page_edge_overshoots: list[dict[str, Any]] = []
            # Two acts on one page legitimately share one chair's raw response
            # (the comment below dedupes `page_response_refs` for exactly this
            # reason), so re-deriving that response's overshoots once per act
            # would re-add the identical (response_sha256, ordinal) finding more
            # than once. `validate_partition_disagreement` refuses that as one
            # rejected block counted twice, aborting the whole page publish over
            # ordinary shared testimony rather than a malformed record.
            seen_page_edge_overshoots: set[tuple[str, int]] = set()
            # Declared fixture observations simulate native geometry; for a
            # Chandra chair they are additive marginal evidence rather than the
            # whole derived layer.
            fixture_observed = (
                _fixture_native_observations(context, chair=chair, page_ordinal=page_ordinal)
                if page_captures is None
                else None
            )
            if not presented:
                observed: list[dict[str, Any]] = []
            elif isinstance(resolved, ChairIdentity) and resolved.witness_adapter == "chandra.v1":
                # The fixture executes one retained Chandra response per
                # compatibility act, while the durable page Testimonium owns
                # their page partition. Re-derive that partition only from
                # responses whose primary page is this page; a continuation's
                # primary-page response must not become geometry on its far
                # page merely because the act belongs to both.
                adapter = witness_adapters.resolve_runnable_adapter("chandra.v1")
                observed = []
                captured_geometry = False
                needs_default_observation = False
                for act in page_acts:
                    if act["page_ordinal"] != page_ordinal:
                        continue
                    source_attempt = attempts_by_pair[(act["act_id"], chair)]
                    raw = source_attempt.observation_payload
                    if raw is None and source_attempt.outcome == "genuinely-empty":
                        needs_default_observation = True
                    if raw is None:
                        continue
                    captured_geometry = True
                    # Keep the reference to the bytes this page's geometry was
                    # quantized from, in the record that carries the geometry.
                    # Retained once per distinct blob and in the order the
                    # partition was built, so the record answers "derived from
                    # what?" without rejoining act-scoped compatibility records.
                    reference = source_attempt.raw_response_ref
                    if reference is not None and reference not in page_response_refs:
                        page_response_refs.append(reference)
                    source_observed, overshoots = chandra_page_partition_entries(
                        adapter.observe(presented, raw),
                        page_size=_sealed_source_page(context, presented)[2],
                        raw_response_ref=reference,
                    )
                    for overshoot in overshoots:
                        overshoot_key = (overshoot["response_sha256"], overshoot["ordinal"])
                        if overshoot_key not in seen_page_edge_overshoots:
                            seen_page_edge_overshoots.add(overshoot_key)
                            page_edge_overshoots.append(overshoot)
                    for item in source_observed:
                        observed.append({**item, "ordinal": len(observed)})
                if needs_default_observation or not captured_geometry:
                    observed.extend(
                        {**item, "ordinal": len(observed)}
                        for item in observed_from_presentation(presented)
                    )
                if fixture_observed is not None:
                    # Declared marginal geometry is additional evidence: native
                    # response blocks still attach testimony to acts, while the
                    # marginal box must remain available to the unclaimed route.
                    for item in fixture_observed:
                        observed.append({**item, "ordinal": len(observed)})
            elif fixture_observed is not None:
                observed = fixture_observed
            elif adapter is not None:
                observed = adapter.observe(presented, native_payload)
            else:
                # An absent chair cannot currently be attempted; keep its
                # no-adapter fallback explicit if that invariant changes.
                observed = observed_from_presentation(presented)
            # The page Testimonium is the durable home for a witness's own
            # partition.  Keep every proposal/observation pairing as geometry,
            # including the common unrouted-observation finding; this stage does
            # not choose an act for a marginal observation.
            page_proposals = page_proposal_regions
            page_artifact_id = artifact_id(
                ATTESTATORES, "page-testimonium", page_subject, page_attempt
            )
            # A never-presented page has no witness geometry to partition, and a
            # retained snapshot naming zero proposals on a page the Designator
            # sealed proposals for would be a false fact the Recensor refuses.
            # The optional field is honestly absent instead.
            disagreement = (
                partition_disagreement(
                    {
                        "artifact_id": page_artifact_id,
                        "payload": {"presented": presented, "observed": observed},
                    },
                    page_proposals,
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
                chair=chair,
                act_key=f"page-{page_ordinal}",
                ordinal=ordinal,
                regions=[],
                # A failed attempted page still records the serving moment;
                # every attempted witness outcome is receipt-backed. Under a live
                # capture that moment is the one the chair really served, named by
                # the receipt its own client re-read at start.
                provenance=provenance_for(
                    context,
                    resolved,
                    attempted=attempted_page,
                    receipt_ref=page_attempt_result.receipt_ref if captured is not None else None,
                ),
                format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
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
            # See the act-scoped writer: no adapter-derived evidence is made
            # immutable before it reconciles with its sealed page and inputs.
            validate_testimonium_presentation(context, {"payload": payload, "inputs": inputs})
            context.publish(
                kind="page-testimonium",
                subject_id=page_subject,
                outcome=outcome,
                attempt=page_attempt,
                # Every retained response this record derived from is an input,
                # not only the Churro capture: `RunTree.read_artifact` re-reads
                # `inputs` and nothing else, so a reference that lives only in
                # the payload is a blob no ordinary consumer re-hashes. The
                # order is the payload's own -- presented image, then the
                # partition's responses in partition order, then the capture.
                # Named once each: a page whose partition was derived from the
                # very bytes its capture describes -- a live Chandra page that
                # parsed -- reaches the same reference twice, and one response
                # listed twice is not two responses.
                inputs=_named_once(
                    inputs
                    + page_response_refs
                    + ([native_capture["raw_response_ref"]] if native_capture is not None else [])
                ),
                payload=payload,
            )
            page_records[(page_ordinal, chair)] = context.artifact_ref(
                ATTESTATORES,
                "page-testimonium",
                page_artifact_id,
            )
            page_observations[(page_ordinal, chair)] = observed
        # A declared anchor is the fixture posture's stand-in for the anchor
        # chair's own page text, and its per-act `lines` carry act geometry no
        # live run has measured. Aligning a live page reading against it would
        # place real witness text on declared spans -- a measurement nobody made
        # (GOVERNANCE 10) -- so a live pass reads no anchor at all and its page
        # witnesses come back `unaligned: missing-chandra-page-anchor`, which is
        # what this run honestly holds until R4 owns live alignment.
        anchors = (
            [
                row
                for row in context.fixture.get("chandra_anchor", [])
                if row.get("page_ordinal") == page_ordinal
            ]
            if page_captures is None
            else []
        )
        if len(anchors) > 1:
            # Skipping a malformed declaration is not the same fact as an absent
            # one: it would detach every page witness on the page from every act
            # on it, and record `missing-chandra-page-anchor` for an anchor that
            # is present on disk -- a default substituted for malformed evidence
            # (GOVERNANCE 2/10).
            raise SchemaRefusal(
                f"page {page_ordinal} declares {len(anchors)} Chandra anchors; a page has "
                "one anchor, and skipping a duplicated declaration would detach every "
                "page witness on it under a reason naming an absent anchor"
            )
        if anchors and not isinstance(anchors[0].get("html"), str):
            raise SchemaRefusal(
                f"the Chandra anchor for page {page_ordinal} carries no anchor markup "
                "text; a malformed anchor is not an absent one"
            )
        if anchors:
            anchor = anchors[0]
            anchor_texts[page_ordinal] = anchor["html"]
            normalized_anchor = markup_text_view(anchor["html"])["text"]
            # `lines` is declared in reading order (ARCHITECTURE: Chandra's own
            # `ocr_layout` reading flow). Searching each line from where the
            # previous one ended, rather than from the start of the page every
            # time, means a phrase repeated across two acts on the same page
            # (a formulaic register opening, most plainly) resolves to its own
            # occurrence in order instead of both lines collapsing onto the
            # first match `str.find` would return from position 0.
            search_from = 0
            for line in anchor.get("lines", []):
                # The same malformed-versus-absent rule as the anchor checks
                # above: a skipped row leaves the act reporting
                # act-anchor-line-not-located for a line that sits malformed on
                # disk, and leaves `search_from` behind the malformed line's
                # span so the next act's formulaic opening can resolve into it.
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
                # The haystack is the markup-stripped, whitespace-collapsed
                # view, so the needle must be the same view of the same
                # declaration -- searching raw declared text inside the
                # normalized anchor failed for any line carrying a tag, an
                # entity, or a double space, and it failed SILENTLY: nothing
                # recorded the miss, the act reported
                # act-anchor-line-not-located for a line sitting on disk, and
                # `search_from` stayed behind the unlocated line's span. An
                # unlocatable declared line is malformed evidence, not an
                # absent act line.
                needle = markup_text_view(source)["text"]
                start = normalized_anchor.find(needle, search_from) if needle else -1
                act = next((item for item in page_acts if item["act_key"] == line["act_key"]), None)
                if start < 0:
                    raise SchemaRefusal(
                        f"the Chandra anchor line for act {line['act_key']} on page "
                        f"{page_ordinal} does not occur in the page's own anchor text at or "
                        "after the previous line; an unlocatable declared line is malformed "
                        "evidence, not an absent act line"
                    )
                if start >= 0:
                    if act is not None:
                        if (page_ordinal, act["act_id"]) in anchor_ranges:
                            # The same malformed-vs-absent rule as every branch
                            # above: keeping the last line would drop the first
                            # line's span and geometry without a record, and the
                            # dropped half's characters would read as witness
                            # departure. The day an act genuinely owns several
                            # anchor lines, line_geometry carries all of them --
                            # it does not keep the last.
                            raise SchemaRefusal(
                                f"page {page_ordinal} declares more than one Chandra anchor "
                                f"line for act {line['act_key']}; keeping the last one would "
                                "drop the first line's span and geometry without a record"
                            )
                        bbox = {key: line.get(key) for key in ("x", "y", "w", "h")}
                        if (
                            any(
                                not isinstance(value, int) or isinstance(value, bool)
                                for value in bbox.values()
                            )
                            or bbox["x"] < 0
                            or bbox["y"] < 0
                            or bbox["w"] <= 0
                            or bbox["h"] <= 0
                        ):
                            # A null, non-integer, or negative coordinate is a
                            # default standing in for geometry nobody measured;
                            # published as this act's line_geometry it would be
                            # indistinguishable from a real rectangle -- or be a
                            # rectangle nothing can draw, refused two stages
                            # later as a type error at the consumer instead of
                            # here, at the declaration (GOVERNANCE 2/10).
                            raise SchemaRefusal(
                                f"the Chandra anchor line for act {line['act_key']} on page "
                                f"{page_ordinal} declares an unusable rectangle; only measured "
                                "non-negative integer geometry can be published as this act's "
                                "line geometry"
                            )
                        anchor_ranges[(page_ordinal, act["act_id"])] = {
                            "start": start,
                            "end": start + len(needle),
                            "bbox": bbox,
                        }
                    # A located line advances the cursor whether or not it maps
                    # to a proposed act on this page -- an anchor line for an
                    # unproposed act still occupies its span of the page, and
                    # leaving the cursor behind it would let the NEXT act's
                    # formulaic opening resolve into this line's text.
                    search_from = start + len(needle)

    attachment_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for act in acts:
        entries: list[dict[str, Any]] = []
        for chair in context.witness_chairs:
            act_attempt = attempts_by_pair[(act["act_id"], chair)]
            page_witness = chair in page_chairs and act["outcome"] == "proposed"
            alignment: dict[str, Any] | None = None
            if page_witness:
                act_anchor = anchor_ranges.get((act["page_ordinal"], act["act_id"]))
                # Whose reading this attachment is a view OF. The entry names one
                # page Testimonium, so the answer is that record's own attempt
                # wherever a native capture produced one -- and it is resolved per
                # contributing page, because a chair can be captured on the act's
                # primary page and not on its continuation. Under a native capture
                # the act-scoped rows are a separate compatibility channel, and
                # reading them here published an act attachment describing a
                # response the referenced page record never made: an alignment
                # computed over a page the chair failed to deliver, or a failed
                # page laundered into `attached: true`. The legacy join keeps the
                # act attempt because its page outcome is derived from exactly
                # those attempts.
                captured_outcome = page_outcomes.get((act["page_ordinal"], chair))
                page_outcome = (
                    captured_outcome if captured_outcome is not None else act_attempt.outcome
                )
                if page_outcome not in WITNESS_READING_OUTCOMES:
                    # There is no reading to place. Running the page alignment
                    # here would manufacture an `aligned` status for text this
                    # chair never delivered on this act, and the Perlector
                    # refuses exactly that shape (`attached: False` beside an
                    # aligned alignment) -- one failed attempt would stop the
                    # act for a reason that has nothing to do with the ink.
                    # The attempt's own outcome is the explicit unaligned
                    # reason instead.
                    alignment = {
                        "status": "unaligned",
                        # Native captures name the page attempt; legacy joins name
                        # this act's attempt because another act can make the joined
                        # page record read successfully.
                        "reason": non_reading_alignment_reason(
                            page_outcome,
                            native_page_capture=captured_outcome is not None,
                        ),
                    }
                elif page_outcome == "genuinely-empty":
                    # There is no witness text to place, which is a different fact
                    # from text that was placed and searched for in vain: bounded
                    # alignment can never succeed against an empty string (an empty
                    # `SequenceMatcher` sequence has no matching block of positive
                    # size), so running it here would turn an honest "nothing was
                    # here" into a permanent, unrecoverable "unaligned" -- silently
                    # dropping a genuine blank corroboration below the witness
                    # floor (GOVERNANCE 2/10). Attach trivially at a zero-length
                    # span instead, exactly as the act-scoped branch below already
                    # does for the same outcome.
                    alignment = {
                        "status": "aligned",
                        # A trivial attach with no located anchor line says so,
                        # and says WHICH absence: an ink-free or fallback page
                        # legitimately has no Chandra anchor at all
                        # (`no-page-anchor` -- blank confirmation stays open),
                        # while a page whose anchor exists but locates no line
                        # for this act is geometry that does not reconcile
                        # (`act-line-not-located` -- `blank_corroboration`
                        # refuses to seal a terminal blank on it). Without the
                        # distinction the Recensor and the export could not
                        # tell either from a computed alignment (GOVERNANCE
                        # 2/10).
                        "anchor_basis": (
                            "act-anchor"
                            if act_anchor is not None
                            else (
                                "no-page-anchor"
                                if anchor_texts.get(act["page_ordinal"]) is None
                                else "act-line-not-located"
                            )
                        ),
                        "anchor_chair": anchor_chair if act_anchor is not None else None,
                        "anchor_span": (
                            {"start": act_anchor["start"], "end": act_anchor["start"]}
                            if act_anchor is not None
                            else {"start": 0, "end": 0}
                        ),
                        "witness_span": {"start": 0, "end": 0},
                        "line_geometry": (
                            [
                                {
                                    "bbox": {
                                        key: act_anchor["bbox"][key] for key in ("x", "y", "w", "h")
                                    }
                                }
                            ]
                            if act_anchor is not None
                            else []
                        ),
                        "loss": {"witness": _ZERO_ALIGNMENT_LOSS, "anchor": _ZERO_ALIGNMENT_LOSS},
                        "offset_maps": {"witness": [], "anchor": []},
                    }
                else:
                    page_text = page_texts.get((act["page_ordinal"], chair))
                    anchor_text = anchor_texts.get(act["page_ordinal"])
                    if page_text is None or anchor_text is None:
                        result = {"status": "unaligned", "reason": "missing-chandra-page-anchor"}
                    elif act_anchor is None:
                        # The page anchor exists; this act's line was not located
                        # in it (or the fixture declared none for it). Saying
                        # "missing-chandra-page-anchor" here sent an operator
                        # looking for an anchor file that exists.
                        result = {"status": "unaligned", "reason": "act-anchor-line-not-located"}
                    else:
                        # One alignment per (page, chair), not per (act, chair):
                        # the inputs do not depend on the act, and the design
                        # doc's own measurement puts a scattered-difference
                        # `SequenceMatcher` near CUBIC in length. Recomputing it
                        # once per act turned a page of forty acts into forty
                        # identical full-page alignments per page witness.
                        result = page_alignments.get((act["page_ordinal"], chair))
                        if result is None:
                            result = align_to_anchor(page_text, anchor_text, limits)
                            page_alignments[(act["page_ordinal"], chair)] = result
                    if result["status"] == "aligned":
                        # CLIPPED to this act's anchor range, then carried back
                        # through each block's own witness/anchor offset, rather
                        # than hulling whole overlapping blocks (R4 audit,
                        # F-X2: the hull handed every act the chair's entire
                        # page reading, inverting the dissent instrument), THEN
                        # translated from the markup-stripped normalized space
                        # the matcher measured in back to RAW page-text indices
                        # through the alignment's own offset_map (R6 audit,
                        # F-G2: every consumer of witness_span -- the Perlector
                        # comparison views, the Recensor content coverage, the
                        # act-scoped `span` mirror -- indexes the RAW retained
                        # text). Wave composition per R6-Opus's recorded
                        # verdict: clip in normalized space first, translate at
                        # this one storage point, spans stay RAW everywhere.
                        clipped = []
                        for span in result["spans"]:
                            start = max(span["anchor"]["start"], act_anchor["start"])
                            end = min(span["anchor"]["end"], act_anchor["end"])
                            if start < end:
                                shift = span["witness"]["start"] - span["anchor"]["start"]
                                clipped.append((start + shift, end + shift))
                        if clipped:
                            # Still a hull ACROSS the clipped fragments: when the
                            # act's anchor range matches the witness in two
                            # separate places, the span also covers whatever the
                            # witness wrote between them, and the comparison view
                            # may carry a few of a neighbour's characters into
                            # the dissent row as a departure. That direction is
                            # deliberate -- it overstates disagreement and never
                            # hides it, which is what an instrument watching for
                            # a reader that learned to agree with witnesses
                            # needs. Do not "fix" this towards agreement.
                            normalized_start = min(start for start, _ in clipped)
                            normalized_end = max(end for _, end in clipped)
                            raw_span = _raw_span_from_normalized(
                                result["witness"]["offset_map"], normalized_start, normalized_end
                            )
                            if raw_span is None:
                                result = {
                                    "status": "unaligned",
                                    "reason": "no-raw-counterpart-for-aligned-span",
                                }
                            else:
                                witness_start, witness_end = raw_span
                                alignment = {
                                    "status": "aligned",
                                    "anchor_basis": "act-anchor",
                                    "anchor_chair": anchor_chair,
                                    "anchor_span": {
                                        key: act_anchor[key] for key in ("start", "end")
                                    },
                                    "witness_span": {"start": witness_start, "end": witness_end},
                                    "line_geometry": [
                                        {
                                            "bbox": {
                                                key: act_anchor["bbox"][key]
                                                for key in ("x", "y", "w", "h")
                                            }
                                        }
                                    ],
                                    "loss": {
                                        "witness": result["witness"]["loss"],
                                        "anchor": result["anchor"]["loss"],
                                    },
                                    "offset_maps": {
                                        "witness": result["witness"]["offset_map"],
                                        "anchor": result["anchor"]["offset_map"],
                                    },
                                }
                        else:
                            result = {"status": "unaligned", "reason": "no-overlap-with-act-anchor"}
                    if result["status"] == "unaligned":
                        alignment = {"status": "unaligned", "reason": result["reason"]}
            if page_witness:
                # Derive each row from the primary alignment without mutating it:
                # source-page ordinal does not determine primary-first act order,
                # so an earlier continuation must not erase the comparison view.
                for contributing_page in contributing_pages_by_act[act["act_id"]]:
                    is_primary_page = contributing_page == act["page_ordinal"]
                    page_alignment = (
                        alignment
                        if is_primary_page
                        else {
                            "status": "unaligned",
                            "reason": "continuation-page-no-act-anchor",
                        }
                    )
                    page_bounds = [
                        region["payload"]["transform"]["bounds"]
                        for region in regions_by_act[act["act_id"]][0]
                        if region["payload"]["transform"]["source_page_ordinal"]
                        == contributing_page
                    ]
                    # Alignment only supplies a span inside this witness's own
                    # text.  The attachment itself is the page geometry this
                    # chair reported against the sealed proposal; no anchor
                    # selects a witness/proposal correspondence.
                    contributing_outcome = page_outcomes.get(
                        (contributing_page, chair), act_attempt.outcome
                    )
                    page_attached = contributing_outcome in WITNESS_READING_OUTCOMES and any(
                        reported_geometry_overlaps(
                            page_observations[(contributing_page, chair)], bounds
                        )
                        for bounds in page_bounds
                    )
                    attachment_basis = "geometric-overlap" if page_attached else "unattached"
                    reference = page_records[(contributing_page, chair)]
                    entries.append(
                        {
                            "chair": chair,
                            "page_witness": True,
                            "page_ordinal": contributing_page,
                            "testimonium_ref": reference,
                            "attached": page_attached,
                            "comparable": page_attached
                            and page_alignment["status"] == "aligned"
                            and isinstance(page_texts.get((contributing_page, chair)), str),
                            "attachment_basis": attachment_basis,
                            # The ACT attempt's health, deliberately, even under a
                            # native page capture: both later readers require this
                            # field to equal the chair's current act-scoped
                            # Testimonium health, as the staleness check that
                            # catches a reread appended after this derived view
                            # was written (`pipeline/4_perlector/run.py`, reopened
                            # F-O1; `pipeline/5_recensor/run.py`). It is a currency
                            # check on the per-(act, chair) stream, not a claim
                            # about the page response -- which is what `attached`
                            # and `alignment` beside it describe.
                            "content_health": act_attempt.health,
                            "alignment": page_alignment,
                            "span": (
                                {
                                    "start": page_alignment["witness_span"]["start"],
                                    "end": page_alignment["witness_span"]["end"],
                                }
                                if page_attached and page_alignment["status"] == "aligned"
                                else None
                            ),
                        }
                    )
            else:
                entries.append(
                    act_scoped_attachment_entry(context, act, chair, act_attempt, ordinal)
                )
        attachment_rows.append((act, entries))

    refuse_ambiguous_act_alignments([entries for _act, entries in attachment_rows])

    for act, entries in attachment_rows:
        context.publish(
            kind="act-attachment",
            subject_id=act["act_id"],
            outcome="read",
            attempt=attempt_id(act["act_id"], "act-attachment", ordinal),
            # The attachment payload retains each page/act Testimonium reference.
            # It deliberately does not make the derived record's immutable
            # publication depend on a later testimonio history surviving: the
            # tally must diagnose that missing evidence itself, not have the
            # manifest rebuild fail before it reaches the denominator check.
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

    Returns how many records were written and whether any proposal crop was
    refused — the second is reported, never swallowed, because an act whose crop
    no chair could be shown is a different fact from an act every chair read. The
    region and attempt maps are the result of this invocation's no-write
    preflight. Publication therefore seals the exact attempt whose collision was
    checked, while a pair the interrupted invocation already sealed is counted
    but not published or sent to its chair a second time.
    """
    recorded = 0
    isolated_crop_failure = False
    for act in acts:
        regions, not_read = regions_by_act[act["act_id"]]
        if not_read is not None and act["outcome"] != "held":
            # A refused crop is isolated to its act. No witness is claimed to
            # have read pixels whose lineage failed; every chair instead receives
            # an explicit non-reading record and the other acts proceed.
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

    `open_context` already refuses a run whose configuration bytes moved, so
    this is the same authority read a second time rather than a new one: the
    point is that the rows this stage decides live-or-fixture from are the rows
    the run's `config_digest` covers, checked at the moment they are used
    (GOVERNANCE 6). Refuses in this stage's own vocabulary, because a serving
    catalogue that cannot be read is a configuration refusal, not a witness
    failure.
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

    The mode is the sealed serving-recipe row's own `kind`, read through
    `operations.serving.client.serving_mode_for` -- a three-name lookup with a
    named refusal on zero rows, an unresolved tier, or a catalogue that is half
    live for one chair. There is no new configuration key, and no fallback in
    either direction (hard rule 8).

    One run, one serving posture. A roster half live and half fixture would
    publish, in one attempt layer at one ordinal, records whose receipts say
    `fixture://` beside records from a rented card -- and every consumer that
    compares witnesses across an act would be comparing two different kinds of
    evidence without being told. An absent chair has no serving row to read and
    is `dead` in either posture, so it names no mode here.
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


def default_serving_factory(context, identity: ChairIdentity, tier: str) -> ChairClient:
    """Build the client a live pass reads one chair through.

    Every part of it belongs to the run: the registry that resolved the chair,
    the receipt publisher bound to this same `StageContext` (so the receipt a
    Testimonium names is one this run really wrote), the catalogue the run
    sealed, and the decoding posture its `config_digest` covers. Nothing here
    starts anything -- `ChairClient.__enter__` does, later, once.

    A stage test supplies its own factory instead (`main(serving_factory=...)`),
    which is the same in-process injection seam `registry_factory` already is
    and, for the same reason, is deliberately not a command-line flag: a `--fake`
    route to a fake answering under a configured chair's name is the one thing
    this framework exists to refuse.
    """
    policy, decoding_sha256 = load_decoding_policy(context.args.decoding_config)
    manager = ServingManager(
        registry=context.registry,
        recipes=bound_serving_recipes(context),
        config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=StageContextReceiptPublisher(context),
        log_root=context.tree.resolve(f"{ATTESTATORES}/serving-logs"),
        residency_lease=FileResidencyLease(context.tree.resolve("pod-gpu.lock")),
        producer="pipeline/3_attestatores/run.py",
    )
    return ChairClient(
        manager=manager,
        identity=identity,
        tier=tier,
        retain=lambda data: retained_blob_ref(context, data),
        decoding_config_sha256=decoding_sha256,
        record_temperature=policy["reading_of_record"]["temperature"],
        # Wired bare, the way the serving README describes.
        # `ServiceHandle.receipt_reference` is a read-only mapping proxy and
        # `RunTree.read_run_receipt` accepts its own reference type or a plain
        # `dict` and refuses anything else by name; `ChairClient.__enter__`
        # copies at that seam, so no stage-side conversion is left to do.
        read_receipt=context.tree.read_run_receipt,
    )


def retained_blob_ref(context, data: bytes) -> dict[str, str]:
    """Retain bytes in this stage's own content-addressed blob store."""
    digest, published = context.tree.put_blob(ATTESTATORES, data)
    return {"relative_path": published.relative_path, "sha256": digest}


def attempt_from_live(live: live_witness.LiveAttempt) -> Attempt:
    """Convert one `LiveAttempt` into the `Attempt` every write path shares.

    A rename, not a remap: `live_witness` derives exactly the facts
    `resolve_attempt` derives, from a retained response instead of a declared
    one, plus the three the live boundary adds.
    """
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

    The receipt is the one place that answers it: the fixture posture writes
    `fixture://offline-chair-runner` out loud (`common/stage.fixture_serving_details`),
    a live start writes the endpoint that actually answered. Read rather than
    inferred from which optional fields a payload happens to carry, because a
    page record of a live Chandra response carries neither a retained model
    view nor a serving-call reference and is still a live record.
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

    A live chair cannot reproduce immutable bytes, so a resumed live pass never
    re-asks for a page some sealed record already describes; it rebuilds the
    page facts from that record instead (GOVERNANCE 4). A record whose own
    receipt says the fixture posture served it is refused by name: rebuilding a
    live page record from it would attribute a declared response to a chair
    that served this run.
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
    return (
        Attempt(
            outcome=record["outcome"],
            native_payload=payload["payload"],
            witness_reported=None,
            format_capabilities=payload["format_capabilities"],
            health=payload["content_health"],
            reason=payload.get("reason"),
            raw_response_ref=capture["raw_response_ref"] if capture is not None else None,
            native_capture=capture,
            receipt_ref=provenance.get("receipt_ref") if isinstance(provenance, dict) else None,
            # Derived from the capture rather than read off the record: the
            # sealed record here may be a *page* Testimonium, whose own closed
            # schema has no place for this field, and a capture's retained
            # reference is by definition the adapter's own output bytes. There
            # is nothing to guess.
            raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT if capture is not None else None,
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

    Two sealed records can hold one: the page Testimonium itself, and -- when
    the interrupted pass got as far as the act layer but not the page layer --
    the act-scoped compatibility record of any act whose *primary* page is this
    one, which this boundary derives from the very same page response. A page
    that neither describes was never answered at this ordinal and is asked for
    normally; a continuation page is exactly that case, because no act record is
    ever derived from a continuation page's response.

    More than one act on the page can carry that compatibility record (the
    happy fixture's a1 and a2 are both primary on page 1), and every one of
    them is checked, not just the first found: they all claim to derive from
    the same page response, so a disagreement between them is a record
    problem this boundary must name rather than silently resolve by taking
    whichever act sorts first.
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
                    # `dead`/`not-run`: this pair was never shown pixels, so it
                    # neither stands in for a response nor disagrees with one --
                    # a not-run act sealed by live_attempt_pass's own first loop
                    # (a held crop, a refused proposal) is not a fixture-posture
                    # record wearing this act's name, it is simply not evidence
                    # of this page's response either way.
                    continue
                if attempt.serving_call_ref is None:
                    # An *attempted* outcome naming no serving call is the
                    # fixture posture's own shape: every live attempt names the
                    # call record of the request that produced it, whether or
                    # not its adapter's retained view could be published beside
                    # it.
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

    Three rulings meet here and are all executable code rather than intent.
    Sequential serving and "one chair runs its whole span, then the next"
    (2026-08-05, 2026-08-20) are `feeding.stage_major_schedule` and
    `SingleChairResidency`: one resident chair, a deterministic chair-outer
    order, and a named refusal if a schedule ever served one unit twice or
    returned to a chair already unloaded. Response-as-arrival is the publish
    inside the serve callback: an interrupted pass leaves sealed Testimonia and
    their retained bytes, never N x M model calls with nothing on disk.

    The unit of work is the chair's own scope. An act-scoped chair is asked once
    per act; a page-scoped chair is asked once per *page*, and its act-scoped
    compatibility records are derived from that one page response rather than
    from a second request per act -- which is what `witness_scope` has always
    meant and what the fixture path already does with a declared page response.
    """
    page_chairs = declared_page_witness_chairs(context)
    _contributing_pages, acts_by_page = page_denominator(context, acts, regions_by_act)
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

    # Everything no chair has to answer for: a pair already sealed at this
    # ordinal (counted, never re-asked, never rewritten) and a pair no chair was
    # shown pixels for at all. Published first, so the folder already accounts
    # for them if the first request refuses.
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

    # A resumed page capture answers for its page's response, but not for
    # every act view that response feeds: an interruption between two of a
    # page's own act publications leaves the later ones sealed nowhere, and a
    # resumed pass that only reused the page capture would never revisit them
    # -- `resumed_page_captures` records that the response happened, this
    # loop finishes publishing what it answers for. Only a pair still
    # `PENDING_LIVE_ATTEMPT` is published; the pairs the non-serving loop
    # above already published or sealed are untouched. Runs after that loop
    # so a pair it published is no longer `PENDING_LIVE_ATTEMPT` here and is
    # not published a second time.
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

    # One schedule per chair, concatenated: `stage_major_schedule` orders one
    # chair's own units, and a chair's unit is a page or an act depending on its
    # sealed scope, so there is no single act list that could describe them all.
    # Concatenating keeps every guarantee the executor checks -- contiguous
    # chair blocks, no chair returned to, no unit served twice, one parish.
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
                # The page's own sealed subject id, not a synthesized name: the
                # schedule is a record of what was served, and a page unit is
                # addressed by the page the Exemplar sealed.
                unit_id = page_identity(context.fixture, page_ordinal)
                units[(chair, unit_id)] = page_ordinal
                rows.append({"act_id": unit_id, "page_ordinal": page_ordinal})
        else:
            for act in acts:
                if attempts_by_pair[(act["act_id"], chair)] is not PENDING_LIVE_ATTEMPT:
                    continue
                units[(chair, act["act_id"])] = act
                rows.append({"act_id": act["act_id"], "page_ordinal": act["page_ordinal"]})
        schedule.extend(feeding.stage_major_schedule(context.tree.run_id, rows, [chair]))

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
            # A serving refusal is this stage's refusal to report, not a
            # traceback: the bytes of every response that did arrive are already
            # retained and every Testimonium published before it is sealed.
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


# The engine words a live reading may carry into a record: vLLM's own `stop`
# and `length`, the fixture transport's synonyms for the same two facts, and the
# explicit marker for an engine that reported no stop reason at all. Anything
# else is a word this system has never measured a meaning for.
_LIVE_ENGINE_STOP_WORDS: Final = _CHURRO_STOP_REASONS | {STOP_REASON_UNREPORTED}


def refuse_unpublishable_stop_word(transport_stop_reason: str, what: str) -> None:
    """Refuse a live response whose engine stop word cannot be recorded honestly.

    An engine word outside `_LIVE_ENGINE_STOP_WORDS` has no measured meaning
    here: recording it would put a word into a truncation channel nothing can
    read, and mapping it to either "complete" or "cut off" would be a
    measurement nobody made (GOVERNANCE 10, and the same rule
    `pipeline/4_perlector/truncation.py` applies by refusing an unknown engine
    string by name). The check runs on `transport_stop_reason` alone, so it
    also catches an unmeasured word on a response `ChairClient` could not parse
    into a reading at all: a wire body no adapter parsed still names its engine
    word verbatim inside the retained `chair-call-record.v1` blob, and a word
    this pipeline has never measured a meaning for is exactly as unpublishable
    there as on a parsed capture.

    It refuses before the response's own record is published, with its bytes
    already retained by the client (GOVERNANCE 2).

    A *reported-nothing* boundary used to be refused here as well, for Churro
    alone, because the shared page contract asked a two-valued question of a
    three-state fact and would have published `truncated: false` over a
    boundary nothing observed. `common/native_witness.py` now measures the
    third state, so that refusal is gone rather than merely relaxed: an
    unreported word publishes `truncated: null` with basis `not-recorded`, on
    the page record and the act record alike.
    """
    if transport_stop_reason not in _LIVE_ENGINE_STOP_WORDS:
        raise ContractError(
            f"{what} reports transport_stop_reason {transport_stop_reason!r}, which this "
            "pipeline has never measured a meaning for; recording it as complete or as cut "
            "off would assert a boundary nobody observed. The response bytes are retained "
            "and nothing was published for it"
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
    built = live_witness.act_chair_request(context, adapter, presentation)
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

    The act-scoped records are the same facts as the page record -- outcome,
    retained text, health, retained bytes -- because they are the same response.
    Only an act whose *primary* page is this one takes its view from here: a
    continuation's act view belongs to the act's own page, and the far page's
    reading reaches that act through the page record its attachment names.

    Shared by `_serve_page_unit` (a page response this pass just received) and
    `live_attempt_pass` (a page response a resumed pass recovered from a sealed
    record, per the interrupted-mid-page repair below). Only a pair still
    `PENDING_LIVE_ATTEMPT` is published: a pair the interrupted pass already
    sealed for this page is left exactly as it was.
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
) -> int:
    """One page-scoped chair, one page: one request, then every act view it feeds."""
    presentation = presentation_for_page(context, page_ordinal)
    request = live_witness.page_chair_request(
        context, adapter, resolved.witness_adapter, presentation
    )
    response = client.read(request)
    live = live_witness.captured_page_attempt(
        context, page_ordinal, chair, resolved.witness_adapter, adapter, response
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

    The one question the attempt model turns on. `pipeline/4_perlector/run.py::
    _next_attempt` derives the reading ordinal from the act's *crop* history —
    one reading of the proposal, plus one for each recovery crop cut since — and
    the Recensor, Archetypus and Armarium each enforce that same identity. So a
    Testimonium that arrives after such a reading has nowhere to go: the Perlector
    recomputes the same ordinal, builds a different payload from the new
    testimony, and the run tree refuses the write against the record it already
    sealed. There is no forward path, because the Perlectio that would have to
    change is itself immutable (GOVERNANCE 4).

    A witness pass may add coverage. A reading is still made only by a crop.
    A second look's coverage has nowhere to go except a recovery request, and a
    recovery request mints a region, and a region moves the reading ordinal.
    New testimony after a reading is refused; new INK after a reading is a
    recovery request. This distinguishes coverage recovery (GOVERNANCE 11) from
    re-rolling a witness until it says something preferable.

    **Closed by a reading that cites testimony, not by any Perlectio at all.** A
    held act and an absent Perlector chair both publish `not-run` records with no
    witness basis; their bytes do not depend on the testimony, so new testimony
    wedges nothing there and a whole second pass over a run holding one held act
    must not be refused on its account.

    One walk for the whole invocation, and read from the Perlector's own artifacts
    rather than from a flag, so a folder assembled or resumed in any order answers
    the same way.
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

    At entry, before anything is written. The alternative is what the audit found:
    the attempt is appended, and the wedge it makes is discovered three stages
    later as an immutability refusal on a reading identity nothing can move — with
    no forward path, because the Perlectio that would have to change is itself
    immutable (GOVERNANCE 4).

    A *rerun* of an attempt already sealed is untouched by this: it rewrites
    byte-identical bytes, changes no chair's current record, and is how the
    orchestrator resumes. Only an append is refused.
    """
    if act["act_id"] in closed:
        raise ContractError(
            f"act {act['act_id']} ({act['act_key']}) already carries a Perlectio, so its "
            f"witness layer is closed: {what} would append testimony no reading can be "
            "established from. A witness pass may add coverage, but a reading is made only "
            "by a crop: new ink must route through a Recensor recovery request, which mints "
            "a region and moves the reading ordinal. New testimony after a reading is "
            "refused; new INK after a reading is a recovery request. Re-asking a witness "
            "because it spoke again is the re-roll GOVERNANCE 11 refuses"
        )


def next_attempt_ordinal(history: AttemptHistory, act_id: str, chair: str) -> int:
    """The ordinal a reread of this one chair appends at.

    Derived from that chair's own history on disk, exactly as
    `pipeline/2_designator/run.py::_next_region_ordinal` derives the next crop
    ordinal — so append-only is a property of what already exists rather than of
    how many times this program has been invoked.
    """
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

    The whole-pass `--attempt-ordinal` is the wrong instrument for this. A reread
    happens because *one* chair failed on *one* act; re-witnessing every chair on
    every act to reach it re-reads ink nobody doubted, costs a provider call per
    chair per act, and moves every other chair's derived-current record for no
    reason. This path moves exactly the chair named, and every other chair's
    current record stays the attempt it already was.

    Everything else matches the whole pass: same declaration tables at the new
    ordinal, same regions the first attempt was shown (a reread is a second look
    at the original proposal, never a first look at ink a recovery uncovered),
    same single write path, and no pointer anywhere — "current" stays derived.
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
        # A page witness reports one reading of one page. Its act-level view is
        # *derived* — the page join, then the alignment of that join against the
        # page anchor — so there is no act-scoped request to put to it a second
        # time, and re-deriving one act's view from an attempt the page record
        # does not describe would leave the page Testimonium and the attachment
        # disagreeing about the same chair. No operation exists today to re-ask
        # a page witness about anything: building one would be new, page-scoped
        # Attestatores work, and it is deliberately not half-performed here.
        # (The recovery vocabulary's `page-level-reread` is a PERLECTOR
        # operation — a different concept whose name must not be borrowed for
        # this one; one word per concept.)
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

    # No `require_appendable_ordinal` here: `next_attempt_ordinal` returns the
    # current ordinal plus one, off the same history, so the bound cannot fire.
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

    Split from the publication deliberately: all three refusals here depend only
    on state that existed before the reread writes anything, and running them
    after `publish_attempt` is how a damaged tree could strand a sealed
    Testimonium its manifest does not yet name — the exact failure
    `require_shared_whole_pass_ordinal`'s docstring condemns on the whole-pass
    path. The reread preflights this first, publishes the Testimonium second,
    and publishes the attachment last, so a refusal leaves the folder untouched.

    The reread chair's own slot comes back as `None`: its re-derived entry
    references the NEW Testimonium by digest, so it can only be built after the
    publish (`republish_act_attachment` fills it). The other chairs' attempts
    did not move, so their entries are carried forward — but *checked* first,
    so a stale entry is refused rather than laundered into a newer record by a
    reread that has nothing to do with it.
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
            # The other half of staleness for an act-scoped carried entry: a
            # positive `attached` over a chair whose current outcome is not a
            # reading. A page witness's `attached` is alignment-derived and may
            # legitimately diverge; the Perlector's own guard holds that case.
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
    """Publish the attachment `prepared_act_attachment` already checked.

    The reread chair's `None` slot is filled here, after its Testimonium exists
    to be referenced by digest; every other entry was carried and checked in the
    preflight.
    """
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

    The fixture is this run's corpus: its pages, acts, continuations and
    proposals are what a live chair is shown. Its *declared responses* are the
    offline posture's stand-in for a model, and a live pass reads none of them —
    every outcome here comes from a response a chair really returned. Nothing is
    lost: those rows are still in the sealed fixture, and every record this pass
    writes names the receipt of the moment that produced it, so no reader can
    mistake one posture's record for the other's. What would be lost is an
    operator's ability to notice, which is what this line is for (GOVERNANCE 2).
    """
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
    # `chandra_anchor` keys on `page_ordinal`, not `chair` -- a page anchor is
    # not any one witness's row -- so the `chair in live_chairs` filter above
    # cannot be reused for it. `publish_page_testimonia_and_attachments` drops
    # every declared anchor unconditionally once it is passed a live
    # `page_captures` dict, so every anchor the scenario declares is counted
    # here, not only the ones a particular chair would have read.
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

    ``serving_factory(context, identity, tier) -> ChairClient`` is the live
    boundary's in-process injection seam, exactly as ``registry_factory`` is for
    chair resolution and for exactly the same reason: a command-line route to a
    fake serving a configured chair's name is the thing this framework refuses.
    It is consulted only when the sealed serving catalogue says this run's
    witness chairs are live.
    """
    parser = stage_parser(__doc__.splitlines()[0], accepts_chair=True)
    parser.add_argument(
        "--attempt-ordinal",
        type=_positive_ordinal,
        # No default ordinal: a reread derives its own from the named chair's
        # history, and a default would make "asked for ordinal 1" and "asked for
        # nothing" the same argv, so the reread could not say it was overridden.
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
    context = open_context(args, ATTESTATORES, registry_factory=registry_factory)
    # A witness reading is a model decode too.  The adapter currently exposes no
    # generation knobs in the fixture seam, but this check keeps a future real
    # adapter from treating the record posture as an unbound side setting.
    _decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    witness_adapters.validate_runnable_adapter_bindings(context.registry.config)
    # The serving posture of this run's witnesses, read from the sealed
    # serving-recipe rows and nothing else (SPEC_A section 2.1). Resolved before
    # any act is read, because it decides which pass structure runs.
    modes = witness_serving_modes(context, bound_serving_recipes(context), args.placement_tier)
    live_chairs = sorted(chair for chair, mode in modes.items() if mode == "live")
    acts = expected_acts(context)
    try:
        index = _attempt_history(context)
    except FatalAccounting:
        raise
    except ContractError as error:
        print(f"Attestatores attempt tally UNKNOWN: {error}", file=sys.stderr)
        return EXIT_HELD
    # A stored inventory is evidence that attempts existed, and it is evidence even
    # when none of them is left: gating this check on the *walk* finding something
    # meant that losing part of a folder's Testimonium layer held it — stored and
    # rebuilt no longer agree — while losing all of it did not, because the
    # first-run path was taken instead and `context.finish()` rewrote the inventory
    # that said otherwise. So the stored manifest's own existence is one trigger,
    # and a stage seal is the other: a sealed boundary is a completed pass, and its
    # tally must reconcile whether or not the inventory file survived.
    #
    # The walk alone is deliberately NOT a third trigger, and that is Unit 2's
    # change here. A pass interrupted inside `attempt_pass` leaves immutable
    # Testimonia, no inventory and no seal — and asking `attempt_tally` to
    # reconcile against an inventory that was never written held every crash
    # resume on a missing file, with a manual `RunTree.write_manifest` as the only
    # way out. There is no stored claim to contradict in that state, so the resume
    # below repeats the pass at its own ordinal: what is already sealed is reused
    # byte-for-byte, what the crash never reached is written, and nothing is
    # overwritten or concealed. A pass that sealed and then lost its inventory
    # still reconciles, because the seal is still in the rebuilt walk.
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
            # A live reread is a second request to a chair that already answered
            # this act, at a new ordinal. Nothing about it is wrong in principle
            # -- it is what GOVERNANCE 11 bounds recovery with -- but it needs
            # its own residency, its own per-response publication and its own
            # answer to what an act-scoped reread of a page witness means, and
            # none of that is built. Refused by name rather than half-performed
            # into a pass that starts a chair and cannot publish what it hears.
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
            # Read in both postures: `declarations_for` refuses a fixture that
            # contradicts itself at this ordinal, which is a fact about the
            # sealed inputs rather than about who answers. The live resolver
            # below then reads none of it.
            declarations = declarations_for(context, ordinal)
            regions_by_act, attempts_by_pair, sealed_pairs = preflight_appendable_ordinals(
                context,
                acts,
                ordinal,
                declarations,
                index,
                # A live pass always reuses a pair already sealed at this
                # ordinal: a live chair cannot reproduce immutable bytes, so
                # asking again could only produce a collision (GOVERNANCE 4).
                resume_incomplete_pass=bool(live_chairs) or not has_prior_boundary,
                resolve=pending_live_attempt if live_chairs else None,
            )
        except ContractError as error:
            # An ordinary preflight refusal holds this pass before it writes any
            # witness artifact. An accounting imbalance is a broken partition, not
            # a holdable request refusal; it must still reach the fatal boundary.
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

    # The tally is part of closing this pass and can still expose a fatal
    # accounting imbalance.  Give it the derived inventory it reconciles, then
    # publish the completion seal only after that refusal boundary has passed.
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
        # Every chair still has its explicit non-reading artifact, so retention
        # completed and later stages can make that partial state visible. This is
        # distinct from an UNKNOWN evidence tally, which is the only stage-3 hold.
        print("Attestatores recorded one or more refused proposal crops", file=sys.stderr)
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
