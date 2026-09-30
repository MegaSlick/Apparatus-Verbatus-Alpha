"""Surya's lines and blocks on every sealed page: evidence that decides nothing.

Surya runs as an independent, deterministic text-line and layout detector, a
check that no ink goes unseen. For each sealed page this stage publishes one
`surya-page` census and one `surya-line` or `surya-block` per detection, in
Surya's own order, each with its box in sealed-page pixels by a declared
quantization. Nothing here carries text, cuts a crop, holds an act or enters
one: the records are for later stages to account against.

The chair is resolved every run. Absent, nothing is published and the sealed
roster records why. Configured, its serving row says how it answers, and each
row answers one pass only, so a run's receipts are never mixed: a fixture row
from the synthetic fixture's declared rows, on the fixture pass (a live pass
reads no fixture and writes no fixture receipt); a subprocess row by running
Surya in its own pinned environment, on the live pass.
"""

from __future__ import annotations

import dataclasses
import math
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import geometry_layer
from no_text import refuse_text_fields

from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.errors import ContractError
from common.contracts.stages import DESIGNATOR
from common.exemplar_boundary import sealed_page_bytes
from common.imaging import dimensions
from common.stage import (
    DESIGNATOR_SURYA_CHAIR,
    _stage_records,
    fixture_serving_details,
    validate_serving_provenance,
)
from operations.serving.assembly import bound_serving_recipes
from operations.serving.client import serving_mode_for
from operations.serving.errors import ServingError
from operations.serving.surya_detector import (
    SuryaRun,
    SuryaSubprocess,
    fixture_surya_run,
)

# How a subprocess row is answered: `check(profile)` asks Surya's environment
# for its versions, and a call `(profile, bundle_root, page_bytes, sizes,
# identity, *, manifest_rows) -> SuryaRun` runs it. Production starts Surya's
# runner process; tests inject an in-process stand-in
# (`operations.serving.fakes.InProcessSurya`).
SuryaRunner = SuryaSubprocess
SURYA_SUBPROCESS = SuryaSubprocess()

SURYA_PROVENANCE_KIND = "surya-provenance"
SURYA_PAGE_KIND = "surya-page"
SURYA_LINE_KIND = "surya-line"
SURYA_BLOCK_KIND = "surya-block"
PAGE_SCHEMA = "surya-page.v1"
LINE_SCHEMA = "surya-line.v1"
BLOCK_SCHEMA = "surya-block.v1"
# How Surya's float corners become sealed-page geometry: each corner is floored
# to the pixel it falls in and clamped to the page, and the box is the half-open
# hull of those pixels (`geometry_layer.enclosing_aabb`). Surya's own corners
# stay, as floats, in the retained page document.
QUANTIZATION = "surya-corner-floor-clamp.v1"
# A confidence in basis points, rounded half to even from its shortest decimal form.
CONFIDENCE_QUANTIZATION = "confidence-round-half-even-bp.v1"


def line_subject(page_id: str, n: int) -> str:
    return f"{page_id}-surya-line-{n}"


def block_subject(page_id: str, n: int) -> str:
    return f"{page_id}-surya-block-{n}"


def quantized_corners(polygon: list, width: int, height: int) -> list[dict[str, int]]:
    return [
        {
            "x": min(width - 1, max(0, math.floor(x))),
            "y": min(height - 1, max(0, math.floor(y))),
        }
        for x, y in polygon
    ]


def quantized_bounds(corners: list[dict[str, int]], width: int, height: int) -> dict[str, int]:
    return dict(geometry_layer.enclosing_aabb(corners, width, height))


def confidence_bp(value: float | None) -> int | None:
    if value is None:
        return None
    return int((Decimal(repr(value)) * 10_000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def resolved_surya(context) -> ChairIdentity | None:
    """The configured Surya chair, or None when the roster records it absent."""
    resolved = context.registry.resolve(DESIGNATOR_SURYA_CHAIR)
    if isinstance(resolved, AbsentChair):
        return None
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("Surya chair resolution returned neither an identity nor an absence")
    return resolved


def surya_mode(context, identity: ChairIdentity, *, live: bool) -> str:
    """`fixture` or `subprocess`, by the sealed catalogue row alone.

    Surya is never a served engine, so a `vllm` row is refused. A fixture row
    answers only the fixture pass and a subprocess row only the live pass, so
    a fixture run never carries a real receipt beside its declared ones.
    """
    try:
        mode = serving_mode_for(
            bound_serving_recipes(context, context.args.serving_recipes_config),
            identity,
            context.args.placement_tier,
        )
    except ServingError as error:
        raise ContractError(
            f"the serving posture of the Surya chair could not be resolved: {error}"
        ) from error
    if mode == ("subprocess" if live else "fixture"):
        return mode
    if mode == "subprocess":
        raise ContractError(
            f"the Surya chair {identity.role!r} resolves to a subprocess row on the fixture "
            "pass; a subprocess row answers only the live pass, and a fixture run's receipts "
            "are all declared"
        )
    raise ContractError(
        f"the Surya chair {identity.role!r} resolves to a {mode!r} row; Surya runs as a "
        "subprocess in its own environment"
        + (", and a fixture row answers only the fixture pass" if live else "")
    )


def _profile(context, identity: ChairIdentity):
    return bound_serving_recipes(context, context.args.serving_recipes_config).for_identity(
        identity, context.args.placement_tier
    )


def check_surya_runnable(context, runner: SuryaRunner = SURYA_SUBPROCESS) -> None:
    """Refuse a Surya chair the live pass cannot run, before any paid work starts:
    its row, the versions its environment reports, and its verified weights."""
    identity = resolved_surya(context)
    if identity is None:
        return
    surya_mode(context, identity, live=True)
    try:
        runner.check(_profile(context, identity))
    except ServingError as error:
        raise ContractError(f"Surya's environment is not ready: {error}") from error
    context.registry.ensure(identity)


def _fixture_rows(context, refused_pages: frozenset[int]) -> dict[str, list[dict]]:
    """This scenario's declared rows, less those for a page the Exemplar refused:
    that page's loss is already recorded by name at the door, and Surya never
    sees it. A row for any other page that is not sealed is refused downstream."""
    return {
        family: [
            row
            for row in context.fixture.get(family, [])
            if row.get("scenario") in (None, context.scenario)
            and row["page_ordinal"] not in refused_pages
        ]
        for family in ("surya_line", "surya_block")
    }


def _run_surya(
    context,
    identity: ChairIdentity,
    mode: str,
    pages: dict[int, dict],
    runner: SuryaRunner,
    refused_pages: frozenset[int],
) -> tuple[SuryaRun, dict[int, bytes]]:
    if not pages:
        raise ContractError("there is no sealed page for Surya to run on")
    page_bytes = {
        ordinal: sealed_page_bytes(context.tree, record, refusal=ContractError)
        for ordinal, record in sorted(pages.items())
    }
    sizes = {ordinal: dimensions(data) for ordinal, data in page_bytes.items()}
    try:
        if mode == "fixture":
            rows = _fixture_rows(context, refused_pages)
            # A detector has no token context and sizes each page itself, as
            # the live receipt says too.
            details = dataclasses.replace(
                fixture_serving_details(identity), context_cap=0, pixel_cap=0
            )
            run = fixture_surya_run(
                rows["surya_line"], rows["surya_block"], sizes, identity, details
            )
            return run, page_bytes
        snapshot = context.registry.ensure(identity)
        manifest_rows = [row.to_record() for row in context.registry.manifest(identity).rows]
        run = runner(
            _profile(context, identity),
            snapshot.root,
            page_bytes,
            sizes,
            identity,
            manifest_rows=manifest_rows,
        )
    except ServingError as error:
        raise ContractError(f"Surya did not run: {error}") from error
    return run, page_bytes


def _provenance(context, identity: ChairIdentity, run: SuryaRun) -> dict:
    """The chair's provenance, published once; a resumed pass reuses what it sealed.

    A receipt names its serving moment, so a second run would write a different
    one; the records it re-derives must then match the ones already sealed. A
    run on another engine or CPU instruction set is refused outright: its
    records would be re-derived by something the sealed receipt does not name.
    """
    published = _stage_records(context.tree, DESIGNATOR, SURYA_PROVENANCE_KIND)
    if published:
        provenance = published[0]["payload"]
        sealed = context.tree.read_run_receipt(provenance["receipt_ref"])["engine_version"]
        if sealed != run.serving_details.engine_version:
            raise ContractError(
                f"the sealed Surya receipt names {sealed!r}, and this run is "
                f"{run.serving_details.engine_version!r}; a resumed run re-derives Surya's "
                "records only on the engine and CPU instruction set that sealed them"
            )
    else:
        provenance = {
            "chair": identity.role,
            "chair_state": "configured",
            "resolved_identity": identity.to_record(),
            "resolved_revision": {
                "kind": identity.receipt_revision_kind,
                "value": identity.receipt_revision,
            },
            "receipt_ref": context.write_serving_receipt(identity, run.serving_details),
            "adapter_revision": context.adapter_revision,
        }
    validate_serving_provenance(
        context, provenance, producer_stage=DESIGNATOR, require_receipt=True
    )
    context.publish(
        kind=SURYA_PROVENANCE_KIND,
        subject_id=SURYA_PROVENANCE_KIND,
        outcome="proposed",
        inputs=[],
        payload=provenance,
    )
    return provenance


def _detection_payload(
    schema: str,
    page_id: str,
    ordinal: int,
    n: int,
    item: dict,
    size: tuple[int, int],
    raw_ref: dict,
    provenance: dict,
) -> dict[str, Any]:
    corners = quantized_corners(item["polygon"], *size)
    return {
        "schema": schema,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "n": n,
        "polygon_px": corners,
        "bounds": quantized_bounds(corners, *size),
        "quantization": QUANTIZATION,
        "confidence_bp": confidence_bp(item["confidence"]),
        "confidence_quantization": CONFIDENCE_QUANTIZATION,
        "raw_output_ref": raw_ref,
        "authoritative": False,
        "provenance": provenance,
    }


def _publish(context, kind: str, subject: str, inputs: list, payload: dict) -> None:
    refuse_text_fields(payload, kind=kind)
    context.publish(
        kind=kind, subject_id=subject, outcome="proposed", inputs=inputs, payload=payload
    )


def publish_surya_detections(
    context,
    pages: dict[int, dict],
    *,
    live: bool,
    runner: SuryaRunner = SURYA_SUBPROCESS,
    refused_pages: frozenset[int] = frozenset(),
) -> None:
    """Run Surya over every sealed page and publish what it found, page by page.

    A page with no detection still gets its `surya-page`, with zero counts, so
    a page Surya found empty reads differently from a page never asked. A
    re-run republishes identical bytes, or refuses where anything differs.
    `refused_pages` are the ordinals the Exemplar refused at the door, whose
    declared fixture rows are left out; a row for any other unsealed page is
    refused by name.
    """
    identity = resolved_surya(context)
    if identity is None:
        return
    mode = surya_mode(context, identity, live=live)
    run, page_bytes = _run_surya(context, identity, mode, pages, runner, refused_pages)
    provenance = _provenance(context, identity, run)
    for ordinal, page_record in sorted(pages.items()):
        page_id = page_record["subject_id"]
        page = run.pages[ordinal]
        size = dimensions(page_bytes[ordinal])
        raw_ref = context.retain(page.raw, "a Surya page document")
        inputs = [context.input_ref(page_record["payload"]["image_path"]), raw_ref]
        lines = page.document["text_detection"]["bboxes"]
        blocks = page.document["layout"]["bboxes"]
        line_subjects = [line_subject(page_id, n) for n in range(1, len(lines) + 1)]
        block_subjects = [block_subject(page_id, n) for n in range(1, len(blocks) + 1)]
        for n, line in enumerate(lines, start=1):
            payload = _detection_payload(
                LINE_SCHEMA, page_id, ordinal, n, line, size, raw_ref, provenance
            )
            _publish(context, SURYA_LINE_KIND, line_subjects[n - 1], inputs, payload)
        for n, block in enumerate(blocks, start=1):
            payload = _detection_payload(
                BLOCK_SCHEMA, page_id, ordinal, n, block, size, raw_ref, provenance
            )
            payload["label"] = block["label"]
            payload["raw_label"] = block["raw_label"]
            payload["reading_order_position"] = block["position"]
            payload["reading_order"] = page.document["reading_order"]
            _publish(context, SURYA_BLOCK_KIND, block_subjects[n - 1], inputs, payload)
        _publish(
            context,
            SURYA_PAGE_KIND,
            page_id,
            inputs,
            {
                "schema": PAGE_SCHEMA,
                "page_id": page_id,
                "page_ordinal": ordinal,
                "page_width_px": size[0],
                "page_height_px": size[1],
                "line_count": len(lines),
                "block_count": len(blocks),
                "line_subjects": line_subjects,
                "block_subjects": block_subjects,
                "reading_order": page.document["reading_order"],
                "reading_order_reason": page.document["reading_order_reason"],
                "raw_output_ref": raw_ref,
                "run": dict(run.run_facts),
                "quantization": QUANTIZATION,
                "confidence_quantization": CONFIDENCE_QUANTIZATION,
                "authoritative": False,
                "provenance": provenance,
            },
        )
