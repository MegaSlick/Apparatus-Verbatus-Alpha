"""Designator publishes the page evidence the page reading reads; it establishes no text.

On every sealed page it publishes Surya's line and block census and the record
detector's records and their crops, after proving the Exemplar's boundary.
Nothing here decides an act: the Perlector reads whole pages.

    python pipeline/2_designator/run.py --run-root <dir> --run-id <id>
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# `2_designator` is not an importable package name, so sibling modules import
# by plain name; tests load this file via `importlib`, which does not add it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import geometry_layer  # noqa: E402
import surya_detection  # noqa: E402
from no_text import refuse_text_fields as _refuse_text_fields  # noqa: E402

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import REAL_INGRESS, parse_ingress_record  # noqa: E402
from common.contracts.canonical import half_even_bp, is_plain_int  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import region_id  # noqa: E402
from common.contracts.stages import DESIGNATOR, EXEMPLAR  # noqa: E402
from common.exemplar_boundary import (  # noqa: E402
    cut_exemplar_crop,
    sealed_page_bytes,
    verify_exemplar_corpus_seal,
    verify_sealed_page_pixels,
)
from common.imaging import dimensions  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    SECONDARY_PROPOSER_CHAIR,
    StageContext,
    _stage_records,
    fixture_serving_details,
    open_stage_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)
from operations.serving.assembly import bound_serving_recipes  # noqa: E402
from operations.serving.client import serving_mode_for  # noqa: E402
from operations.serving.detector import (  # noqa: E402
    RecordDetector,
    check_record_detector_runnable,
    fixture_record_detector,
    load_ultralytics_record_detector,
)
from operations.serving.errors import ServingError  # noqa: E402

DESCRIPTION = (
    "Designator: publishes each sealed page's Surya census and record-detector records. "
    "It establishes no text."
)


def _configured_chair_record(context, resolved: ChairIdentity) -> dict:
    """The provenance block a resolved chair contributes to every artifact."""
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": context.write_serving_receipt(resolved, fixture_serving_details(resolved)),
        "adapter_revision": context.adapter_revision,
    }


def _absent_chair_record(context, resolved: AbsentChair) -> dict:
    return {
        "chair": resolved.role,
        "chair_state": "absent",
        "absence": resolved.to_record(),
        "resolved_identity": None,
        "resolved_revision": None,
        "receipt_ref": None,
        "adapter_revision": context.adapter_revision,
    }


def _publish_secondary_provenance(context, secondary: dict) -> dict:
    context.publish(
        kind="secondary-provenance",
        subject_id="secondary-provenance",
        outcome="proposed",
        inputs=[],
        payload=secondary,
    )
    return secondary


def secondary_provenance(context, *, real: bool) -> tuple[dict, RecordDetector | None]:
    """Resolve and record the record detector's chair, and open its detector.

    Absence is not a refusal: the detector's records are evidence, never an act.
    The role is still resolved every run so `common/stage.py::unaddressed_chairs`
    stays accurate about it.
    """
    resolved = _resolved_secondary(context)
    if isinstance(resolved, AbsentChair):
        return _absent_chair_record(context, resolved), None
    return _open_record_detector(
        context,
        resolved,
        fixture_allowed=not real,
        published=_published_secondary_provenance(context),
    )


def _resolved_secondary(context) -> ChairIdentity | AbsentChair:
    resolved = context.registry.resolve(SECONDARY_PROPOSER_CHAIR)
    if not isinstance(resolved, ChairIdentity | AbsentChair):
        raise ContractError(
            "secondary proposer resolution returned neither an identity nor an absence"
        )
    return resolved


def _record_detector_mode(context, identity: ChairIdentity, *, fixture_allowed: bool) -> str:
    """`fixture` or `in-process`, by the sealed catalogue row alone.

    The record detector is never an engine this stage starts, so a `vllm` row
    is refused; a fixture row answers only a synthetic run, which declares the
    fixture's boxes.
    """
    try:
        mode = serving_mode_for(
            bound_serving_recipes(context, context.args.serving_recipes_config),
            identity,
            context.args.placement_tier,
        )
    except ServingError as error:
        raise ContractError(
            f"the serving posture of the record detector could not be resolved: {error}"
        ) from error
    if mode == "in-process" or (mode == "fixture" and fixture_allowed):
        return mode
    raise ContractError(
        f"the record detector chair {identity.role!r} resolves to a {mode!r} row; it runs "
        "in-process in this stage"
        + ("" if fixture_allowed else ", and a fixture row answers only a synthetic run")
    )


def _open_record_detector(
    context,
    identity: ChairIdentity,
    *,
    fixture_allowed: bool,
    published: dict | None = None,
) -> tuple[dict, RecordDetector]:
    """Load the detector and the provenance its records carry.

    A fixture row answers from the fixture's `[[detector_record]]` rows under a
    declared receipt. An in-process row loads the verified weights and writes a
    receipt for this load, unless a resumed pass already published one: then
    that provenance is reused and the deterministic detector re-derives the same
    records, and any difference refuses at publication.
    """
    mode = _record_detector_mode(context, identity, fixture_allowed=fixture_allowed)
    if mode == "fixture":
        # A scenario that declares its own records replaces the unscoped ones.
        declared = context.fixture.get("detector_record", [])
        rows = [row for row in declared if row.get("scenario") == context.scenario] or [
            row for row in declared if row.get("scenario") is None
        ]
        detector = fixture_record_detector(rows, identity, fixture_serving_details(identity))
        return _configured_chair_record(context, identity), detector
    profile = bound_serving_recipes(context, context.args.serving_recipes_config).for_identity(
        identity, context.args.placement_tier
    )
    detector = load_ultralytics_record_detector(
        identity, profile, context.registry.ensure(identity).root
    )
    if published is not None:
        provenance = published
    else:
        provenance = {
            "chair": identity.role,
            "chair_state": "configured",
            "resolved_identity": identity.to_record(),
            "resolved_revision": {
                "kind": identity.receipt_revision_kind,
                "value": identity.receipt_revision,
            },
            "receipt_ref": context.write_serving_receipt(identity, detector.serving_details),
            "adapter_revision": context.adapter_revision,
        }
    validate_serving_provenance(
        context, provenance, producer_stage=DESIGNATOR, require_receipt=True
    )
    return provenance, detector


def _read_checked_page_bytes(context, page_record: dict) -> bytes:
    """Re-read a sealed page's pixels and re-verify their digest before use.

    The upfront boundary check runs once; re-checking at each use catches pixels
    changed on disk mid-run before they enter sealed Designator evidence.
    """
    return sealed_page_bytes(context.tree, page_record, refusal=ContractError)


def page_records(context) -> dict[int, dict]:
    """Every page outcome the Exemplar recorded — sealed and refused — by ordinal.

    Read from the Exemplar, not the fixture, so a refused page is never seen as
    ink; refused records are the evidence a hold rests on.
    """
    manifest = context.tree.build_manifest(EXEMPLAR)
    source_rows = _source_rows(context.run)
    records = {}
    entries_by_ordinal = {}
    for entry in manifest["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        ordinal = record["payload"].get("ordinal")
        if not is_plain_int(ordinal):
            raise ContractError("an Exemplar page carries no integer ordinal")
        if ordinal in records:
            raise ContractError(f"the Exemplar carries more than one outcome for ordinal {ordinal}")
        records[ordinal] = {
            "record": record,
            "relative_path": entry["relative_path"],
        }
        entries_by_ordinal[ordinal] = entry
    _verify_exemplar_boundary(context, manifest, source_rows, records, entries_by_ordinal)
    # Manifests are ordered by identity path; process in submission order.
    return {ordinal: records[ordinal] for ordinal in sorted(records)}


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted denominator, retaining each filename for a useful failure."""
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise ContractError("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        path = row.get("relative_path")
        if not is_plain_int(ordinal):
            raise ContractError("run.json carries a source-manifest row without an integer ordinal")
        if ordinal in sources:
            raise ContractError(f"run.json repeats source ordinal {ordinal}")
        if not isinstance(path, str) or not path:
            raise ContractError(f"run.json source ordinal {ordinal} carries no filename")
        sources[ordinal] = row
    return sources


def _verify_exemplar_boundary(context, manifest, sources, records, entries_by_ordinal) -> None:
    """Reconcile the immutable Exemplar census before the Designator reads pixels."""
    verify_exemplar_corpus_seal(
        context.tree,
        context.run,
        manifest,
        sources,
        {ordinal: item["record"] for ordinal, item in records.items()},
        entries_by_ordinal,
    )
    for ordinal, source in sources.items():
        record = records[ordinal]["record"]
        if record["outcome"] == "sealed":
            verify_sealed_page_pixels(context.tree, context.run, source, record)


def sealed_pages(records: dict[int, dict]) -> dict[int, dict]:
    """The sealed subset, by ordinal, each value the page artifact itself."""
    return {
        ordinal: entry["record"]
        for ordinal, entry in records.items()
        if entry["record"]["outcome"] == "sealed"
    }


def validate_bounds(bounds, width: int, height: int, what: str) -> None:
    """Refuse a rectangle that does not belong to its declared pixel space."""
    if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "w", "h"}:
        raise ContractError(f"{what} is not a closed x/y/w/h rectangle")
    if not all(is_plain_int(bounds[field]) for field in ("x", "y", "w", "h")):
        raise ContractError(f"{what} has a non-integer coordinate")
    x, y, w, h = (bounds[field] for field in ("x", "y", "w", "h"))
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise ContractError(f"{what} {bounds} falls outside its {width}x{height} pixel space")


def _stored_crop(context, page_bytes: bytes, page_ordinal: int, page_record: dict, bounds: dict):
    """Cut and store one crop, returned as the payload fields that describe it."""
    return cut_exemplar_crop(
        context.retain, page_bytes, page_ordinal, page_record["subject_id"], bounds
    )


def _published_secondary_provenance(context) -> dict | None:
    """The secondary provenance a resumed pass already sealed, if any."""
    records = _stage_records(context.tree, DESIGNATOR, "secondary-provenance")
    return records[0]["payload"] if records else None


def _check_record_detector_runnable(context, *, real: bool) -> None:
    """Refuse a configured record detector this run could not load: its row, and for
    an in-process row its pinned package versions and its weights digest, without
    loading the model."""
    identity = _resolved_secondary(context)
    if not isinstance(identity, ChairIdentity):
        return
    if _record_detector_mode(context, identity, fixture_allowed=not real) != "in-process":
        return
    profile = bound_serving_recipes(context, context.args.serving_recipes_config).for_identity(
        identity, context.args.placement_tier
    )
    try:
        check_record_detector_runnable(profile, lambda: context.registry.ensure(identity).root)
    except ServingError as error:
        raise ContractError(f"the record detector is not ready: {error}") from error


# --- DAI's own record detector ------------------------------------------------

DETECTOR_OUTPUT_SCHEMA = "record-detector-output.v1"
# How a detection's float corners become the integer page geometry
# `geometry_layer.yolo_obb` takes: each corner is floored to the pixel it falls
# in and clamped to the page. The oriented box is kept as that polygon, and the
# crop is its axis-aligned hull (`aabb-enclose`): whether DAI's own pipeline
# rectifies a rotated record before reading it is not stated anywhere, so it is
# not done. The float corners stay in the raw output blob.
DETECTOR_QUANTIZATION = "obb-corner-floor-clamp.v1"
# Scores are recorded in basis points, rounded half to even.
DETECTOR_SCORE_QUANTIZATION = "score-round-half-even-bp.v1"
DETECTOR_RECORD_KIND = "detector-record"
DETECTOR_PAGE_KIND = "detector-page"
# A detector box is page evidence, not an act or act coverage, so its crop has
# its own kind.
DETECTOR_REGION_KIND = "detector-region"


def detector_record_subject(page_id: str, detector_ordinal: int) -> str:
    return f"{page_id}-detector-{detector_ordinal}"


def _quantized_corners(corners: list, width: int, height: int) -> list[dict]:
    return [
        {
            "x": min(width - 1, max(0, math.floor(x))),
            "y": min(height - 1, max(0, math.floor(y))),
        }
        for x, y in corners
    ]


def _cut_detector_region(
    context, subject: str, page_record: dict, page_bytes: bytes, bounds: dict, provenance: dict
):
    """Cut one record's crop by the stage's one crop path; it is never an act region."""
    ordinal = page_record["payload"]["ordinal"]
    width, height = dimensions(page_bytes)
    validate_bounds(bounds, width, height, "detector record bounds")
    crop = _stored_crop(context, page_bytes, ordinal, page_record, bounds)
    return context.publish(
        kind=DETECTOR_REGION_KIND,
        subject_id=subject,
        outcome="proposed",
        inputs=[context.input_ref(page_record["payload"]["image_path"])],
        payload={
            "region_id": region_id(subject, crop["transform"]),
            "record_key": subject,
            "origin": "detector",
            **crop,
            "raw_bounds": bounds,
            "padding": None,
            "provenance": provenance,
        },
    )


def _publish_detector_records(
    context,
    pages: dict[int, dict],
    secondary: dict,
    detector: RecordDetector,
    geometry_policy: dict,
) -> None:
    """Every sealed page's detector records: page evidence that decides nothing.

    One `detector-page` per page says how many records were found, so a page
    with none reads differently from a page never asked. Each record keeps its
    oriented box, score and class: records hold nothing and enter no act. They
    are the units DAI reads.
    """
    for ordinal, page_record in pages.items():
        page_id = page_record["subject_id"]
        page_bytes = _read_checked_page_bytes(context, page_record)
        width, height = dimensions(page_bytes)
        detections = detector.detect(page_bytes, page_ordinal=ordinal)
        raw_ref = context.retain(
            json.dumps(
                {
                    "schema": DETECTOR_OUTPUT_SCHEMA,
                    "page_ordinal": ordinal,
                    "page_id": page_id,
                    "run": dict(detector.run_facts),
                    "detections": detections,
                },
                sort_keys=True,
            ).encode("utf-8"),
            "a record detector output",
        )
        quantized = [_quantized_corners(item["corners"], width, height) for item in detections]
        cuttable = [
            index
            for index, points in enumerate(quantized)
            if len({(point["x"], point["y"]) for point in points}) >= 3
        ]
        proposals = geometry_layer.yolo_obb(
            page_id=page_id,
            page_ordinal=ordinal,
            page_w=width,
            page_h=height,
            policy=geometry_policy,
            receipt_ref=secondary["receipt_ref"],
            response_ref=raw_ref,
            detections=[
                {
                    "ordinal": index,
                    "obb": quantized[index],
                    "score_bp": half_even_bp(detections[index]["score"]),
                }
                for index in cuttable
            ],
        )
        by_ordinal = {
            index: proposal for proposal in proposals for index in proposal["observed_ordinals"]
        }
        subjects = []
        for index, detection in enumerate(detections):
            subject = detector_record_subject(page_id, index)
            proposal = by_ordinal.get(index)
            region = (
                _cut_detector_region(
                    context, subject, page_record, page_bytes, proposal["aabb"], secondary
                )
                if proposal is not None
                else None
            )
            bounds = proposal["aabb"] if proposal is not None else None
            payload = {
                "page_ordinal": ordinal,
                "detector_ordinal": index,
                "raw_output_ref": raw_ref,
                "quantization": DETECTOR_QUANTIZATION,
                "score_quantization": DETECTOR_SCORE_QUANTIZATION,
                "score_bp": half_even_bp(detection["score"]),
                "class_id": detection["class_id"],
                "class_name": detection["class_name"],
                "raw_proposal": proposal,
                "bounds": bounds,
                # A box whose corners collapse to fewer than three pixels encloses no
                # crop; it is kept, uncut, rather than dropped.
                "cut": proposal is not None,
                "authoritative": False,
                "authority_effect": "none",
                "region_ref": (
                    context.input_ref(region.relative_path) if region is not None else None
                ),
                "provenance": secondary,
            }
            _refuse_text_fields(payload, kind=DETECTOR_RECORD_KIND)
            inputs = [context.input_ref(page_record["payload"]["image_path"]), raw_ref]
            if region is not None:
                inputs.append(context.input_ref(region.relative_path))
            context.publish(
                kind=DETECTOR_RECORD_KIND,
                subject_id=subject,
                outcome="proposed",
                inputs=inputs,
                payload=payload,
            )
            subjects.append(subject)
        page_payload = {
            "page_ordinal": ordinal,
            "detection_count": len(detections),
            "record_subjects": subjects,
            "raw_output_ref": raw_ref,
            "provenance": secondary,
        }
        _refuse_text_fields(page_payload, kind=DETECTOR_PAGE_KIND)
        context.publish(
            kind=DETECTOR_PAGE_KIND,
            subject_id=page_id,
            outcome="proposed",
            inputs=[context.input_ref(page_record["payload"]["image_path"]), raw_ref],
            payload=page_payload,
        )


def _open(args, registry_factory) -> tuple[StageContext, bool]:
    """Open the run on either ingress route, and say whether it is a real submission.

    The route comes from the same `context.run` the context was built on; it
    forbids fixture rows on real input.
    """
    context = open_stage_context(args, DESIGNATOR, registry_factory=registry_factory)
    return context, parse_ingress_record(context.run.get("ingress")) == REAL_INGRESS


def publish_page_evidence(
    context,
    *,
    real: bool,
    surya_runner: surya_detection.SuryaRunner = surya_detection.SURYA_SUBPROCESS,
) -> None:
    """Publish every sealed page's Surya census and record-detector records.

    Each chair's sealed serving row says how it answers; a fixture row answers
    only a synthetic run. Every chair this run cannot run is refused before
    anything is published.
    """
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to publish evidence for")
    geometry_policy = geometry_layer.load_geometry_policy(context.args.designator_geometry_config)
    context.require_sealed_config("designator-geometry", geometry_policy["config_sha256"])
    _check_record_detector_runnable(context, real=real)
    surya_detection.check_surya_runnable(context, surya_runner, real=real)

    surya_detection.publish_surya_detections(
        context,
        pages,
        real=real,
        runner=surya_runner,
        refused_pages=frozenset(records) - frozenset(pages),
    )
    secondary, detector = secondary_provenance(context, real=real)
    secondary = _publish_secondary_provenance(context, secondary)
    if detector is not None:
        _publish_detector_records(context, pages, secondary, detector, geometry_policy)


def main(registry_factory=ChairRegistry.from_toml, surya_runner=None) -> int:
    """Run the stage. `surya_runner` is the seam for a Surya subprocess row:
    production runs Surya's runner process, tests an in-process stand-in."""
    args = stage_parser(DESCRIPTION).parse_args()
    context, real = _open(args, registry_factory)
    publish_page_evidence(
        context,
        real=real,
        surya_runner=surya_detection.SURYA_SUBPROCESS if surya_runner is None else surya_runner,
    )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
