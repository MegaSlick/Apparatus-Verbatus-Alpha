"""Recensor: establishes that the text is complete. It establishes no text.

It reconciles what the proposal seal expected against what happened and gives every
expected act exactly one outcome. Nothing it does touches a reading.

**Recovery is bounded and recorded.** The budget comes from `config/recovery.toml`,
every request is an artifact, and a spent budget holds the act for review. Recovery
recovers coverage, not quality, so nothing is re-rolled until it looks better
(principle 7).

**It does not select among witnesses.** Witness outcomes form a coverage record that
can mark an act under-witnessed and the run visibly partial. On a Perlector
`no-readable-text` finding, unanimous region-bound absence may corroborate a blank; it
never supplies characters.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import copy
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.act_visibility_geometry import (  # noqa: E402
    MAX_POLYGON_POINTS,
    classify_capture_visibility,
    expected_surface_cells,
)
from common.background import (  # noqa: E402
    BackgroundInferenceRefusal,
    load_background_config,
    resolve_background_policy,
    validate_ink_not_measurable_payload,
    validate_measured_ink_map_payload,
)
from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes, is_sha256  # noqa: E402
from common.contracts.errors import (  # noqa: E402
    ContractError,
    FatalAccounting,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import act_id as derive_act_id  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    ATTACHMENT_BASES,
    OutcomeClass,
    classify,
    page_attachment_basis,
    terminal_category,
    witness_coverage,
)
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
)
from common.corpus_register import refuse_capture_preference  # noqa: E402
from common.cross_capture_autopsia import validate_autopsia  # noqa: E402
from common.cross_capture_coverage import (  # noqa: E402
    build_cross_capture_coverage,
    capture_specific_recovery,
    same_chair_witness_floor,
    validate_cross_capture_coverage,
)
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.imaging import grayscale_rows  # noqa: E402
from common.native_witness import (  # noqa: E402
    reported_geometry_overlaps,
    unrouted_observations,
    validate_page_testimonium_payload,
    validate_partition_disagreement,
    validate_reportable_observations,
    verify_native_capture_blob,
)
from common.perlector_audit import (  # noqa: E402
    EXAMINATION_CAP_EXHAUSTED,
    EXAMINATION_INCOMPLETE,
    EXAMINATION_REPROOF_REJECTED,
    unresolved_state,
    validate_chain,
)
from common.perlector_failure import validate_failed_perlectio  # noqa: E402
from common.recensor_receipt import build_recensor_partition_receipt  # noqa: E402
from common.recovery import (  # noqa: E402
    FALLBACK_RECROP,
    RECOVERY_KINDS,
    reconcile_recovery_requests,
    recovery_kind_budget,
)
from common.residual_ink import (  # noqa: E402
    INK_NOT_MEASURABLE,
    INK_RUNS_SCHEMA,
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    load_coverage_audit_config,
    reconcile_edge_finding_with_runs,
    residual_ink,
    resolve_coverage_audit_policy,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    RESIDUAL_ENUMERATION_AGGREGATED,
    RESIDUAL_ENUMERATION_COMPLETE,
    RESIDUAL_ENUMERATION_WITHHELD,
    RESIDUAL_ENUMERATIONS,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    is_real_ingress,
    latest_attempt,
    latest_per_chair,
    open_context,  # noqa: F401  (re-export: this stage's tests open fixture trees through it)
    open_stage_context,
    page_residual_act_key,
    reading_basis_regions,
    recovery_region_count,
    require_current_witness_basis,
    run_stage,
    scenario_for,
    sealed_residual_presentation_policy,
    stage_manifest,
    stage_parser,
)
from common.testimony_content_coverage import (  # noqa: E402
    validate_testimony_content_coverage,
    validate_testimony_content_coverage_continuation,
)


def designator_hold(context, act_id: str) -> tuple[dict, str]:
    """The Designator's hold record for a seal-held act, and its path.

    Refused loudly when absent: a seal entry that says `held` with no record of
    why is a claim with no evidence, and absent evidence never reads cleaner
    than damaged evidence.
    """
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] == "hold" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "hold", entry["artifact_id"])
            return record, entry["relative_path"]
    raise FatalAccounting(
        f"the seal holds act {act_id} but the Designator published no hold record "
        "saying why; a hold with no evidence cannot be reviewed"
    )


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in stage_manifest(context, stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def _records_of_kind(context, stage: str, kind: str) -> Iterator[dict]:
    """Read every artifact of one kind from a stage's manifest, in manifest order."""
    for entry in stage_manifest(context, stage)["artifacts"]:
        if entry["kind"] == kind:
            yield context.tree.read_artifact(stage, kind, entry["artifact_id"])


_BOX_SIDES = ("x", "y", "w", "h")


def _plain_int(value) -> bool:
    """An int that is not a bool: JSON `true` would otherwise pass as 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _int_box(bounds) -> bool:
    """Exactly the keys x, y, w, h, each a plain int."""
    return (
        isinstance(bounds, dict)
        and set(bounds) == set(_BOX_SIDES)
        and all(_plain_int(bounds[side]) for side in _BOX_SIDES)
    )


def _page_rect(bounds) -> bool:
    """An `_int_box` with a non-negative origin and a positive size."""
    return (
        _int_box(bounds)
        and bounds["x"] >= 0
        and bounds["y"] >= 0
        and bounds["w"] > 0
        and bounds["h"] > 0
    )


def audit_state(
    context, reading: dict, act_id: str, *, expected_act_key: str | None = None
) -> dict | None:
    """Verify the two audit artifacts behind a Perlectio's audit claim.

    The Perlectio's self-hash proves only that someone sealed its references; this
    proves they name this act, the exact kinds, and the finding whose unresolved state
    routes review.

    `not-run` never reaches Pass C, so it has no chain and returns `None`; it is held on
    its own outcome, so a forged one buys nothing. An operational `failed` record is
    verified through the shared failure contract. `None` means no audit at all; an
    `unresolved` of `False` means audited with the round spent or nothing to spend it
    on, not that every flag resolved. `examination` says why, so review can tell an
    exhausted cap from an incomplete re-proof.
    """
    if reading["outcome"] == "not-run":
        return None
    # Only the closed operational-failure shape skips the chain; a `failed` outcome with
    # an ordinary reading payload is still audited.
    if reading["outcome"] == "failed" and "failure" in reading["payload"]:
        validate_failed_perlectio(context, reading, act_id, expected_act_key=expected_act_key)
        return None
    chain = validate_chain(context.tree, reading, act_id)
    return {
        "unresolved": chain["record"]["unresolved"],
        "examination": chain["record"]["examination"],
        "reproof_truncation": chain["finding"]["payload"]["reproof_truncation"],
    }


def chair_current_attempts(context, act_id: str) -> dict[str, dict]:
    """Each chair's current attempt facts, from one latest-attempt collapse.

    Derived, never stored: a failed attempt 2 over a successful attempt 1 reads as
    `failed`. `latest_per_chair` is the derivation the Perlector's `testimonia_of` also
    uses, and outcome, health and read evidence come from this one collapse so the
    staleness check and `blank_corroboration` never compare two ideas of "current". A
    reread appends to this per-(act, chair) stream for page witnesses too.
    """
    records = artifacts_for(context, ATTESTATORES, "testimonium", act_id)
    return {
        record["payload"]["chair"]: {
            # Rejects an attachment pairing a current outcome with a superseded payload.
            "artifact_id": record["artifact_id"],
            "outcome": record["outcome"],
            "content_health": record["payload"].get("content_health"),
            "read_evidence": _read_evidence(record["payload"]),
        }
        for record in latest_per_chair(records, f"testimonium for {act_id}")
    }


def _read_evidence(payload: dict) -> dict[str, bool]:
    """Whether an Attestatores attempt claims a chair looked: regions and a receipt.

    Not a quality signal. A completed absence is the one outcome that could be produced
    without anything being asked, so the claim is read rather than assumed.
    """
    regions = payload.get("regions")
    provenance = payload.get("provenance")
    receipt = provenance.get("receipt_ref") if isinstance(provenance, dict) else None
    return {
        "regions": isinstance(regions, list) and bool(regions),
        "receipt": receipt is not None,
    }


def chair_outcomes(current_attempts: dict[str, dict]) -> dict[str, str]:
    """The current outcome per chair, from the caller's collapse so evidence shares it."""
    return {chair: fact["outcome"] for chair, fact in current_attempts.items()}


def chair_read_evidence(current_attempts: dict[str, dict]) -> dict[str, dict[str, bool]]:
    """Each chair's read evidence, projected from the same passed collapse."""
    return {chair: fact["read_evidence"] for chair, fact in current_attempts.items()}


def _proposal_geometry_by_page(context, act_id: str) -> dict[int, dict]:
    """The sealed-proposal denominator a page witness could have seen.

    Recovery regions are deliberately absent.  They are minted only after the
    Attestatores has sealed, so including them here would let a later crop
    retroactively attach testimony to an act the witness never identified.
    """
    pages: dict[int, dict] = {}
    for region in artifacts_for(context, DESIGNATOR, "region", act_id):
        payload = _payload(region, f"Designator region of {act_id}")
        if payload.get("origin") != "proposal":
            continue
        facts = _expected_basis_facts(region, act_id)
        ordinal = facts["source_page_ordinal"]
        transform = facts["transform"]
        bounds = transform.get("bounds") if isinstance(transform, dict) else None
        if (
            not _plain_int(ordinal)
            or not isinstance(facts["source_page_id"], str)
            or not facts["source_page_id"]
            or not _page_rect(bounds)
        ):
            raise FatalAccounting(f"act {act_id} has malformed sealed-proposal page geometry")
        page = pages.setdefault(
            ordinal,
            {"source_page_id": facts["source_page_id"], "bounds": []},
        )
        if page["source_page_id"] != facts["source_page_id"]:
            raise FatalAccounting(
                f"act {act_id}'s sealed proposals give page {ordinal} more than one page identity"
            )
        page["bounds"].append(bounds)
    return pages


def _merge_page_attachment_fact(previous: dict, current: dict) -> dict:
    """An unattached continuation may not erase another page's attachment.

    Chosen by strength, not arrival order: `comparable` is a per-page fact, so a
    continuation that attached without comparable text must not outrank a primary page
    that has both. Ties keep `previous`.
    """
    return max(previous, current, key=lambda fact: (fact["attached"], fact["comparable"]))


SURVEY_ABSENT = "act-visibility-survey-absent"
REGISTRATION_ABSENT = "cross-capture-registration-absent"
# A view that rendered two pages cannot be surveyed on one grid: its rectangle exists on
# no page. Recorded as an absence, not measured.
SURVEY_SPANS_TWO_PAGES = "act-visibility-survey-spans-two-pages"
# An absent instrument is recorded but does not become a measured shortfall.
INSTRUMENT_ABSENT_CODES = frozenset({SURVEY_ABSENT, REGISTRATION_ABSENT, SURVEY_SPANS_TWO_PAGES})


def occlusion_records_by_page(context) -> dict[str, list[dict]]:
    """Read every sealed Designator occlusion record once per run, by page.

    A record with no string `page_id` refuses: a page never surveyed and a page whose
    survey was unreadable must not read alike. Polygon checks stay in the per-page
    survey, where they name the page.
    """
    records: dict[str, list[dict]] = {}
    for record in _records_of_kind(context, DESIGNATOR, "occlusion"):
        payload = _payload(record, f"Designator occlusion {record['artifact_id']}")
        page = payload.get("page_id")
        if not isinstance(page, str) or not page:
            raise FatalAccounting(
                f"Designator occlusion {record['artifact_id']} has no valid page_id "
                f"({page!r}); the Recensor refuses to file occlusion evidence under "
                "no page rather than let it read as a page never surveyed"
            )
        records.setdefault(page, []).append(
            {"artifact_id": record["artifact_id"], "payload": payload}
        )
    return records


def _page_occlusion_survey(occlusions: dict[str, list[dict]], page_id: str) -> dict:
    """Return the sealed occlusion evidence for one Exemplar page.

    Artifact absence is not evidence that a survey ran. A `below-ink` record does not
    obscure ink; every other relationship occludes. Validated locally because stages
    may not import one another's modules.

    No Designator stage seals `kind="occlusion"` yet, so every current run records
    `act-visibility-survey-absent`: a named absence, which routing does not treat as a
    shortfall.
    """
    polygons: list[list[dict[str, int]]] = []
    refs: list[str] = []
    surveyed = False
    for record in occlusions.get(page_id, []):
        payload = record["payload"]
        surveyed = True
        polygon = payload.get("polygon")
        if (
            not isinstance(polygon, list)
            or len(polygon) < 3
            or len(polygon) > MAX_POLYGON_POINTS
            or any(
                not isinstance(point, dict)
                or set(point) != {"x", "y"}
                or not isinstance(point.get("x"), int)
                or not isinstance(point.get("y"), int)
                or isinstance(point.get("x"), bool)
                or isinstance(point.get("y"), bool)
                or point["x"] < 0
                or point["y"] < 0
                for point in polygon
            )
            or len({(point["x"], point["y"]) for point in polygon}) < 3
        ):
            raise FatalAccounting(
                f"Designator occlusion {record['artifact_id']} names page {page_id!r} "
                "with a malformed polygon"
            )
        z_relationship = payload.get("z_relationship")
        if z_relationship not in {"unknown", "above-ink", "below-ink"}:
            raise FatalAccounting(
                f"Designator occlusion {record['artifact_id']} names page {page_id!r} with "
                f"unknown z_relationship {z_relationship!r}; the Recensor refuses to infer "
                "visibility from an occlusion relationship it cannot interpret"
            )
        if z_relationship == "below-ink":
            continue
        polygons.append([{"x": point["x"], "y": point["y"]} for point in polygon])
        # An occlusion claim must retain the exact artifact that supports it.
        refs.append(record["artifact_id"])
    return {"surveyed": surveyed, "polygons": polygons, "occlusion_refs": sorted(refs)}


def _view_visibility(
    surveyed: bool, page_count: int, bounds_list: list[dict], polygons: list
) -> tuple[str, list, list, list[str]]:
    """``(visibility_state, visible_cells, occluded_cells, finding_codes)`` for one view."""
    if not surveyed:
        return "unresolved", [], [], [SURVEY_ABSENT]
    # One bounding box over two pages would mix two coordinate spaces, so occlusion on
    # page two would land in page one's cells. Until each page is classified on its own
    # grid such a view is unmeasured; continuation acts make this the common case.
    if page_count > 1:
        return "unresolved", [], [], [SURVEY_SPANS_TWO_PAGES]
    x0 = min(bounds["x"] for bounds in bounds_list)
    y0 = min(bounds["y"] for bounds in bounds_list)
    x1 = max(bounds["x"] + bounds["w"] for bounds in bounds_list)
    y1 = max(bounds["y"] + bounds["h"] for bounds in bounds_list)
    survey = classify_capture_visibility(
        bounds={"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
        occlusion_polygons=polygons,
    )
    return survey["visibility_state"], survey["visible_cells"], survey["occluded_cells"], []


def act_cross_capture_coverage(
    context,
    act_id: str,
    latest_payload: dict,
    *,
    occlusions: dict[str, list[dict]] | None = None,
    proposal_geometry: dict[str, dict] | None = None,
) -> dict | None:
    """Survey the captures sealed in this act's current Perlectio; `None` if none registered.

    Missing page surveys, and capture-local grids with no sealed registration into one
    frame, remain unresolved. `occlusions` and `proposal_geometry` are run-level reads
    `main` passes down; they default to reading from `context`, and the Recensor writes
    no Designator artifacts, so they cannot go stale within a pass.
    """
    if occlusions is None:
        occlusions = occlusion_records_by_page(context)
    if proposal_geometry is None:
        proposal_geometry = {}
    dossier = latest_payload.get("dossier")
    if not isinstance(dossier, dict) or "cross_capture_autopsia" not in dossier:
        return None
    autopsia = validate_autopsia(dossier["cross_capture_autopsia"])
    logical_act_id = dossier.get("logical_act_id")
    if logical_act_id != autopsia["logical_act_id"]:
        raise FatalAccounting(
            f"act {act_id}'s current Perlectio names logical act {logical_act_id!r}, but its "
            f"sealed cross-capture autopsia names {autopsia['logical_act_id']!r}; the Recensor "
            "refuses to attribute one logical act's visibility evidence to another"
        )
    if not any(act_id in view["local_act_ids"] for view in autopsia["views"]):
        raise FatalAccounting(
            f"act {act_id}'s current Perlectio's cross-capture autopsia does not name it "
            "among any view's local acts"
        )
    components: dict[str, dict] = {}
    for view in autopsia["views"]:
        bounds_list: list[dict] = []
        for local_id in view["local_act_ids"]:
            if local_id not in proposal_geometry:
                proposal_geometry[local_id] = _proposal_geometry_by_page(context, local_id)
            for page in proposal_geometry[local_id].values():
                if page["source_page_id"] in view["page_ids"]:
                    bounds_list.extend(page["bounds"])
        if not bounds_list:
            raise FatalAccounting(
                f"logical act {logical_act_id!r} view {view['view_id']!r} names no sealed "
                "proposal geometry to survey"
            )
        polygons: list[list[dict[str, int]]] = []
        occlusion_refs: list[str] = []
        surveyed = True
        for page_id in view["page_ids"]:
            page_survey = _page_occlusion_survey(occlusions, page_id)
            surveyed = surveyed and page_survey["surveyed"]
            polygons.extend(page_survey["polygons"])
            occlusion_refs.extend(page_survey["occlusion_refs"])
        visibility_state, visible_cells, occluded_cells, finding_codes = _view_visibility(
            surveyed, len(view["page_ids"]), bounds_list, polygons
        )
        row = {
            "source_sha256": view["source_sha256"],
            "alignment_ref": view["alignment_ref"],
            "visibility_state": visibility_state,
            "visible_cells": visible_cells,
            "occluded_cells": occluded_cells,
            "occlusion_refs": sorted(set(occlusion_refs)),
            "finding_codes": finding_codes,
        }
        physical_page = view["physical_page_id"]
        expected = expected_surface_cells()
        component = components.setdefault(
            physical_page,
            {"expected_cells": expected, "captures": [], "required": []},
        )
        # A component denominator cannot silently inherit whichever member's
        # expected surface arrived first.
        if component["expected_cells"] != expected:
            raise FatalAccounting(
                f"logical act {logical_act_id!r} component {physical_page!r} is surveyed over "
                "two different expected surfaces; the component's extent is not a choice "
                "between its members"
            )
        component["captures"].append(row)
        component["required"].append(view["source_sha256"])
    for entry in components.values():
        if len(entry["captures"]) < 2:
            continue
        # Opaque alignment references do not map capture-local grids into one
        # coordinate frame, so their masks cannot support a union.
        for row in entry["captures"]:
            row["visibility_state"] = "unresolved"
            row["visible_cells"] = []
            row["occluded_cells"] = []
            row["finding_codes"] = sorted({*row["finding_codes"], REGISTRATION_ABSENT})
    payload_components = [
        {
            "physical_page_id": page,
            "expected_cells": entry["expected_cells"],
            "required_capture_sha256s": sorted(entry["required"]),
            "captures": sorted(entry["captures"], key=lambda row: row["source_sha256"]),
        }
        for page, entry in sorted(components.items())
    ]
    return build_cross_capture_coverage(
        logical_act_id=logical_act_id, components=payload_components
    )


def cross_capture_review_causes(coverage: dict | None) -> tuple[bool, bool | None]:
    """Return ``(occluded_everywhere, unresolved)`` for review routing.

    Component findings, rather than the aggregate state, preserve a measured
    occlusion beside a full continuation component. ``None`` means every
    unresolved component lacks its instrument entirely; any measured gap is a
    real shortfall and returns ``True``.
    """
    if coverage is None:
        return False, None
    occluded_everywhere = any(row["code"] == "occluded-everywhere" for row in coverage["findings"])
    unresolved_components = [
        row for row in coverage["components"] if row["union_state"] == "unresolved"
    ]
    if not unresolved_components:
        return occluded_everywhere, False
    instrument_absence_only = all(
        all(
            row["visibility_state"] == "unresolved"
            and row["finding_codes"]
            and set(row["finding_codes"]) <= INSTRUMENT_ABSENT_CODES
            for row in component["captures"]
        )
        for component in unresolved_components
    )
    if instrument_absence_only:
        return occluded_everywhere, None
    return occluded_everywhere, True


def act_attachment_facts(
    context, act_id: str, current_attempts: dict[str, dict]
) -> dict[str, dict]:
    """Re-derive the attachment record before counting the witness floor.

    The Recensor is the independent floor-accounting seam: a sealed attachment boolean
    is not evidence, so page testimony must still overlap this act's original proposal
    geometry.
    """
    outcomes = chair_outcomes(current_attempts)
    records = artifacts_for(context, ATTESTATORES, "act-attachment", act_id)
    if not records:
        raise FatalAccounting(f"act {act_id} has no derived act-attachment record")
    record = latest_attempt(records, f"act-attachment for {act_id}", operation="act-attachment")
    payload = record.get("payload")
    entries = payload.get("attachments") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise FatalAccounting(f"act {act_id} has malformed derived act-attachment payload")
    proposal_pages = _proposal_geometry_by_page(context, act_id)
    facts: dict[str, dict] = {}
    seen_pairs: set[tuple[str, object]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("chair"), str):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        chair = entry["chair"]
        if not isinstance(entry.get("attached"), bool) or not isinstance(
            entry.get("comparable"), bool
        ):
            raise FatalAccounting(
                f"act {act_id} attachment entry for chair {entry['chair']!r} has no boolean "
                "attached/comparable pair; the witness floor cannot be counted from an "
                "ambiguous attachment; rebuild the entry from the retained Testimonia"
            )
        attachment_basis = entry.get("attachment_basis")
        if attachment_basis not in ATTACHMENT_BASES:
            raise FatalAccounting(
                f"act {act_id} attachment entry for chair {entry['chair']!r} has no known attachment basis"
            )
        health = entry.get("content_health")
        # Malformed is not absent: only an absent record means "health not recorded".
        if health is not None and not isinstance(health, dict):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        truncated = health.get("truncated") if isinstance(health, dict) else None
        # A non-boolean read as act-scoped would skip the alignment check.
        page_witness = entry.get("page_witness")
        if not isinstance(page_witness, bool):
            raise FatalAccounting(
                f"act {act_id} attachment entry for chair {chair!r} carries no boolean "
                "page_witness flag; its scope decides which consistency check applies"
            )
        page_ordinal = entry.get("page_ordinal")
        if page_witness:
            if not _plain_int(page_ordinal):
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} has no integer page ordinal; its "
                    "attachment cannot be placed; restore the contributing page identity"
                )
        elif page_ordinal is not None:
            raise FatalAccounting(
                f"act {act_id} act-scoped witness {chair!r} carries page ordinal "
                f"{page_ordinal!r}; its scope is contradictory; restore null page_ordinal"
            )
        pair = (chair, page_ordinal)
        if pair in seen_pairs:
            raise FatalAccounting(
                f"act {act_id} repeats attachment pair {pair!r}; its page evidence is "
                "ambiguous; retain exactly one row per chair and contributing page"
            )
        seen_pairs.add(pair)
        if page_witness:
            _verify_page_witness_entry(
                context, act_id, entry, proposal_pages, outcomes, attachment_basis
            )
        else:
            _verify_act_scoped_entry(context, act_id, entry, current_attempts, attachment_basis)
        fact = {
            "attached": entry["attached"],
            "comparable": entry["comparable"],
            "attachment_basis": attachment_basis,
            "truncated": truncated,
            "health_unrecorded": truncated is None,
            "page_witness": page_witness,
            "content_health": health,
            # What an aligned page witness aligned against, for `blank_corroboration`;
            # None for act-scoped chairs and unaligned page witnesses.
            "anchor_basis": (
                entry["alignment"].get("anchor_basis")
                if page_witness and entry["attached"]
                else None
            ),
        }
        if chair in facts:
            if not (page_witness and facts[chair]["page_witness"]):
                raise FatalAccounting(
                    f"act {act_id} has two attachment entries for chair {chair!r} and at least "
                    "one is act-scoped; only page-witness rows may repeat, once per contributing "
                    "page; retain exactly one act-scoped entry per chair"
                )
            if facts[chair]["content_health"] != fact["content_health"]:
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} restates different content health "
                    "across its pages; one act attempt cannot have two health records; "
                    "restore the attempt's single recorded health"
                )
            # One row per contributing page, one act attempt: whole rows merge, never
            # OR-ed booleans, so no combination appears that no single page supplied.
            previous = facts[chair]
            merged = dict(_merge_page_attachment_fact(previous, fact))
            # Only rows of one attempt merge (the health check above holds that), so
            # filling a missing basis from a sibling page borrows nothing.
            # `act-line-not-located` is sticky, or `blank_corroboration` would treat a
            # failed alignment as checked geometry.
            bases_seen = (previous["anchor_basis"], fact["anchor_basis"])
            if "act-line-not-located" in bases_seen:
                merged["anchor_basis"] = "act-line-not-located"
            elif merged["anchor_basis"] is None:
                merged["anchor_basis"] = next(
                    (basis for basis in bases_seen if basis is not None), None
                )
            facts[chair] = merged
            continue
        facts[chair] = fact
    return facts


def _verify_page_witness_entry(
    context,
    act_id: str,
    entry: dict,
    proposal_pages: dict[int, dict],
    outcomes: dict[str, str],
    attachment_basis: str,
) -> None:
    """Derive a page witness's attachment from its own page Testimonium and alignment."""
    chair, page_ordinal = entry["chair"], entry["page_ordinal"]
    proposal_page = proposal_pages.get(page_ordinal)
    if proposal_page is None:
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} attaches outside the sealed proposal denominator"
        )
    reference = entry.get("testimonium_ref")
    if not isinstance(reference, dict):
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} has no page Testimonium reference"
        )
    try:
        page_testimonium = context.tree.read_artifact_reference(
            reference,
            stage=ATTESTATORES,
            kind="page-testimonium",
            subject_id=proposal_page["source_page_id"],
        )
        page_payload = validate_page_testimonium_payload(
            page_testimonium.get("payload"),
            testimonium_id=page_testimonium.get("artifact_id"),
            read_bytes=context.tree.read_bytes,
        )
    except ContractError as error:
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} has no valid page geometry: {error}"
        ) from error
    if page_payload.get("chair") != chair or page_payload.get("page_ordinal") != page_ordinal:
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} points to a different page Testimonium"
        )
    # A response named only in the payload is one no ordinary artifact read re-hashes.
    for reference in page_payload.get("raw_response_refs", []):
        if reference not in page_testimonium.get("inputs", []):
            raise FatalAccounting(
                f"act {act_id} page witness {chair!r} does not bind a retained raw "
                "response its own geometry was quantized from as a verified input"
            )
    native_capture = page_payload.get("native_capture")
    if native_capture is not None:
        _verify_native_capture(context, act_id, chair, page_testimonium, native_capture)
    # Native page and compatibility act outcomes are independent; legacy
    # page joins instead derive their outcome from the act attempts.
    attachment_outcome = (
        page_testimonium["outcome"] if native_capture is not None else outcomes.get(chair)
    )
    # The floor is counted from this derivation, never from the record's boolean.
    derived_basis = page_attachment_basis(
        reading=attachment_outcome in WITNESS_READING_OUTCOMES,
        geometry_overlaps=any(
            reported_geometry_overlaps(page_payload.get("observed", []), bounds)
            for bounds in proposal_page["bounds"]
        ),
        alignment=entry.get("alignment"),
    )
    if entry["attached"] != (derived_basis != "unattached"):
        raise FatalAccounting(
            f"act {act_id} page attachment for chair {chair!r} does not derive from "
            "that witness's reported geometry, or from an anchor line located in its "
            "page text, against the sealed proposal"
        )
    if entry["comparable"] and not entry["attached"]:
        raise FatalAccounting(
            f"act {act_id} has comparable text without an attached witness. "
            "The witness floor could count text that geometry did not place in the act. "
            "Rebuild the attachment facts from the retained witness geometry."
        )
    alignment = entry.get("alignment")
    alignment_status = alignment.get("status") if isinstance(alignment, dict) else None
    # An unhashable JSON value at an enum field is a named refusal,
    # never a set-membership TypeError.
    if not isinstance(alignment_status, str) or alignment_status not in {
        "aligned",
        "unaligned",
    }:
        raise FatalAccounting(f"act {act_id} page witness {chair!r} has no computed alignment fact")
    # The exact label: `anchor-line` says this chair counts only because another
    # chair's anchor located its text.
    if entry["attached"] and attachment_basis != derived_basis:
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} names attachment basis "
            f"{attachment_basis!r}, but its own retained evidence attached it by "
            f"{derived_basis!r}"
        )
    if not entry["attached"] and attachment_basis != "unattached":
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} names a basis for an unattached record"
        )
    _require_alignment_shape(act_id, chair, alignment)
    # `attached` proves some evidence placed this reading in the act, not that
    # there is retained text to compare; the floor also needs an aligned record
    # and a string page payload.
    if entry["comparable"] != (
        entry["attached"]
        and alignment["status"] == "aligned"
        and isinstance(page_payload.get("payload"), str)
    ):
        raise FatalAccounting(
            f"act {act_id} page attachment for chair {chair!r} claims a comparability "
            "its own retained page testimony does not support. The witness floor could "
            "count text the page record cannot supply for this act. Rebuild the attachment "
            "from the referenced page Testimonium and alignment."
        )


def _verify_native_capture(
    context, act_id: str, chair: str, page_testimonium: dict, native_capture: dict
) -> None:
    """A native capture must bind its raw response and come from the chair's own adapter."""
    if native_capture["raw_response_ref"] not in page_testimonium.get("inputs", []):
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} does not bind its retained raw "
            "response as a verified input"
        )
    # `resolve` may return an `AbsentChair`, which has no `witness_adapter`;
    # refuse by name rather than raise AttributeError.
    resolved = context.registry.resolve(chair)
    if not isinstance(resolved, ChairIdentity):
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} carries a native capture while the "
            "roster records that chair as absent; an absent chair has no adapter "
            "boundary to attribute it to; restore the chair or the retained record"
        )
    if native_capture["adapter"] != resolved.witness_adapter:
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} attributes its native capture to "
            "an adapter other than that chair's configured boundary"
        )
    try:
        verify_native_capture_blob(context.tree, native_capture)
    except ContractError as error:
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} has a native capture that does "
            f"not derive from its retained raw response: {error}"
        ) from error


_ALIGNED_KEYS = frozenset(
    {
        "status",
        "anchor_basis",
        "anchor_chair",
        "anchor_span",
        "witness_span",
        "anchor_line_match",
        "line_geometry",
        "loss",
        "offset_maps",
        "deadline_in_force",
    }
)
_ANCHOR_BASES = frozenset({"act-anchor", "no-page-anchor", "act-line-not-located"})


def _require_alignment_shape(act_id: str, chair: str, alignment: dict) -> None:
    """Refuse an alignment record outside its closed aligned or unaligned shape.

    An attached record missing its geometry or `anchor_basis` must not count, and an
    unaligned record needs a reason.
    """
    if alignment["status"] == "aligned":
        if (
            set(alignment) != _ALIGNED_KEYS
            or not isinstance(alignment["anchor_basis"], str)
            or alignment["anchor_basis"] not in _ANCHOR_BASES
            or (
                alignment["anchor_basis"] == "act-anchor"
                and not isinstance(alignment.get("anchor_chair"), str)
            )
            or (
                alignment["anchor_basis"] != "act-anchor"
                and alignment.get("anchor_chair") is not None
            )
            # The SIGALRM backstop fact, as the Perlector requires it.
            or not isinstance(alignment.get("deadline_in_force"), bool)
        ):
            raise FatalAccounting(
                f"act {act_id} page witness {chair!r} carries a malformed aligned "
                "alignment record; the witness floor may not be counted from "
                "geometry evidence that is missing or unrecognised"
            )
    elif set(alignment) != {"status", "reason"} or not (
        isinstance(alignment["reason"], str) and alignment["reason"].strip()
    ):
        raise FatalAccounting(
            f"act {act_id} page witness {chair!r} carries an unaligned record with "
            "no usable reason; an unexplained failure is a silent loss"
        )


def _verify_act_scoped_entry(
    context, act_id: str, entry: dict, current_attempts: dict[str, dict], attachment_basis: str
) -> None:
    """Act-scoped floor facts come from the current referenced Testimonium.

    Trusting both stored booleans would let a producer forge them false and silently
    remove a completed chair.
    """
    chair = entry["chair"]
    reference = entry.get("testimonium_ref")
    if not isinstance(reference, dict):
        raise FatalAccounting(
            f"act {act_id} act-scoped witness {chair!r} has no Testimonium reference. "
            "Its attachment and comparability cannot be checked against immutable evidence. "
            "Rebuild the attachment with a reference to the current Testimonium."
        )
    try:
        testimonium = context.tree.read_artifact_reference(
            reference,
            stage=ATTESTATORES,
            kind="testimonium",
            subject_id=act_id,
        )
    except ContractError as error:
        raise FatalAccounting(
            f"act {act_id} act-scoped witness {chair!r} names no readable "
            f"Testimonium: {error}. Its witness-floor contribution is unverifiable. "
            "Restore the referenced artifact and retry the Recensor."
        ) from error
    act_payload = testimonium.get("payload")
    if not isinstance(act_payload, dict) or act_payload.get("chair") != chair:
        raise FatalAccounting(
            f"act {act_id} act-scoped attachment for chair {chair!r} points to "
            "another chair's Testimonium. One witness's evidence would be attributed "
            "to another chair. Rebuild the attachment from the named chair's own record."
        )
    current = current_attempts.get(chair)
    if not isinstance(current, dict) or testimonium.get("artifact_id") != current.get(
        "artifact_id"
    ):
        raise FatalAccounting(
            f"act {act_id} act-scoped attachment for chair {chair!r} does not point "
            "to that chair's current Testimonium; its referenced witness basis has "
            "since superseded. The witness floor would be computed from stale evidence. "
            "Rebuild the attachment against the current immutable attempt."
        )
    derived_attached = testimonium.get("outcome") in WITNESS_READING_OUTCOMES
    if entry["attached"] != derived_attached:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment disagrees with the current "
            f"Testimonium outcome for chair {chair!r}; the witness floor may not be "
            "counted from a superseded attempt. The attachment is stale or malformed. "
            "Rebuild it from the current Testimonium before retrying."
        )
    if entry.get("page_ordinal") is not None or entry.get("alignment") is not None:
        raise FatalAccounting(
            f"act {act_id} act-scoped attachment for chair {chair!r} carries page "
            "alignment evidence. The record mixes witness scopes with different "
            "derivations. Rebuild it without page alignment fields."
        )
    expected_basis = "presented-region" if derived_attached else "unattached"
    if attachment_basis != expected_basis:
        raise FatalAccounting(
            f"act {act_id} act-scoped attachment for chair {chair!r} names "
            f"{attachment_basis!r} instead of its derived {expected_basis!r} basis. "
            "The stated cause contradicts the current Testimonium outcome. "
            "Rebuild the basis from that current outcome."
        )
    if entry["comparable"] != (derived_attached and isinstance(act_payload.get("payload"), str)):
        raise FatalAccounting(
            f"act {act_id} attachment for chair {chair!r} claims a comparability its "
            "own retained derived testimony does not support. The witness floor could "
            "count a structured or absent report as act text. Rebuild comparability "
            "from the current referenced Testimonium."
        )


def blank_corroboration(
    coverage: dict,
    outcomes: dict[str, str],
    attachments: dict[str, dict],
    read_evidence: dict[str, dict[str, bool]],
    *,
    witness_uncovered: bool = False,
) -> list[str] | None:
    """The corroborating chairs if every witness that read this act's ink agrees
    nothing was there, or `None` if the evidence does not support that.

    Unanimity about an absence, never a selection among presences: the Perlector
    already found `no-readable-text` in the ink, and this asks only whether the
    witnesses corroborate it. One chair that read text holds the act for a human and is
    never outvoted. A blank needs several independent completed reads, never a
    reader's second opinion.

    The floor is counted from chairs that completed a read, not from `under_witnessed`,
    which also counts an excluded chair; no chair may still be unresolved. A recovery
    region is witness-uncovered, so inherited testimony cannot corroborate absence
    there. A page witness whose anchor locates no line for this act
    (`act-line-not-located`) counts toward the floor but cannot corroborate a terminal
    blank; `no-page-anchor` can, or the blank-page path would be unreachable.

    A completed outcome missing its regions or receipt is a record this pipeline's
    writer cannot produce, so it is fatal rather than a quiet `None`.
    """
    completed = sorted(
        chair for chair, outcome in outcomes.items() if outcome in WITNESS_READING_OUTCOMES
    )
    # Named per chair and per missing fact: which half is absent points to the producer
    # branch.
    unproved = []
    for chair in completed:
        evidence = read_evidence.get(chair, {})
        missing = [
            label
            for fact, label in (("regions", "region inputs"), ("receipt", "serving receipt"))
            if not evidence.get(fact)
        ]
        if missing:
            unproved.append(f"{chair} has no {' and no '.join(missing)}")
    if unproved:
        raise FatalAccounting(
            "a completed witness outcome for this act records no request having been made: "
            f"{'; '.join(unproved)}. A blank may not be corroborated by a read that nothing "
            "records having happened"
        )
    # After the validation: a writer-impossible record must be fatal on every path,
    # including recovery regions and runs missing a chair, where these quiet returns
    # would otherwise skip it.
    if witness_uncovered or coverage["unresolved_chairs"]:
        return None
    if (
        len(completed) < coverage["floor"]
        or not completed
        or any(outcomes[chair] != "genuinely-empty" for chair in completed)
        or any(
            attachments.get(chair, {}).get("anchor_basis") == "act-line-not-located"
            # A page witness with no basis at all is geometry nobody checked.
            or (
                attachments.get(chair, {}).get("page_witness")
                and attachments.get(chair, {}).get("anchor_basis") is None
            )
            for chair in completed
        )
    ):
        return None
    return completed


def validate_chair_coverage(context, act_id: str, floor: int) -> dict[str, object]:
    """Return one act's coverage after refusing ambiguous witness history.

    Callable before anything is published, so an ambiguity found at a later act never
    leaves a partial set of reviews for a retry to mistake for history.
    """
    current_attempts = chair_current_attempts(context, act_id)
    outcomes = chair_outcomes(current_attempts)
    sealed = set(context.witness_chairs)
    missing = sealed - set(outcomes)
    if missing:
        raise FatalAccounting(
            f"act {act_id} has no outcome for configured chair(s) {sorted(missing)}. "
            "Every configured chair gets an explicit outcome for every act"
        )
    unsealed = set(outcomes) - sealed
    if unsealed:
        raise FatalAccounting(
            f"act {act_id} carries a testimonium from chair(s) {sorted(unsealed)}, "
            f"which this run was not sealed with. `run.json` names its witness "
            "chairs and nothing may add one after the seal"
        )
    attachments = act_attachment_facts(context, act_id, current_attempts)
    unaccounted = sorted(set(outcomes) ^ set(attachments))
    if unaccounted:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment and its current Testimonia disagree on "
            f"chair(s) {unaccounted}; an absent fact would silently read as unattached, and "
            "an extra one would attach a chair that never testified for this act"
        )
    # A reread appends an attempt without a new attachment, so a mismatch means a
    # superseded attempt. Page witnesses are exempt: their alignment is their own fact.
    superseded = sorted(
        chair
        for chair, outcome in outcomes.items()
        if not attachments[chair]["page_witness"]
        and attachments[chair]["attached"] != (outcome in WITNESS_READING_OUTCOMES)
    )
    if superseded:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment disagrees with the current Testimonium "
            f"outcome for chair(s) {superseded}; the witness floor may not be counted from "
            "a superseded attempt"
        )
    # Every chair, page witnesses included; the Perlector's `act_attachment_view` agrees.
    stale_health = sorted(
        chair
        for chair, fact in attachments.items()
        if fact["content_health"] != current_attempts[chair]["content_health"]
    )
    if stale_health:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment describes an attempt that is no longer "
            f"the current Testimonium for chair(s) {stale_health}; the witness floor may not "
            "be counted from a superseded attempt"
        )
    coverage = witness_coverage(outcomes, floor, attachments=attachments)
    # The cross-capture primitive must agree with the established floor while
    # every readable logical act has one component. Truncation is folded into
    # comparability because it is not a separate fact in that primitive.
    cross_capture_floor = same_chair_witness_floor(
        [
            {
                "chair": chair,
                "capture": act_id,
                "attached": fact["attached"],
                "comparable": fact["comparable"] and fact.get("truncated") is not True,
                "components": ["whole"],
            }
            for chair, fact in attachments.items()
        ],
        components={"whole"},
        floor=floor,
    )
    if cross_capture_floor["under_witnessed"] != coverage["under_witnessed"]:
        raise FatalAccounting(
            f"act {act_id}'s cross-capture witness-floor union disagrees with its "
            "single-component witness floor; the two must agree while every logical act "
            "is one capture"
        )
    return coverage


def preflight_witness_denominator(context, floor: int) -> None:
    """Refuse all witness ambiguity before this stage writes its first review."""
    for act in expected_acts(context):
        validate_chair_coverage(context, act["act_id"], floor)


def _payload(record: dict, what: str) -> dict:
    """One record payload, refusing an untyped recovery fact before using it."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise FatalAccounting(f"{what} has no object payload")
    return payload


def recovery_state(context, act_id: str, budget: dict) -> dict:
    """Reconcile this act's requested, cut, and reviewed recovery history.

    A request is not evidence of a recrop, nor a recrop of a reading, so the histories
    must agree before another review is written: one recovery-requested review per
    request and at most one recrop per request. The request history itself is
    reconciled by the shared `reconcile_recovery_requests`.
    """
    ordered_requests = reconcile_recovery_requests(
        artifacts_for(context, RECENSOR, "recovery-request", act_id), act_id, budget
    )
    requests_by_ordinal = dict(enumerate(ordered_requests, start=1))
    requests_by_kind: dict[str, list[dict]] = {kind: [] for kind in RECOVERY_KINDS}
    request_refs: dict[str, dict] = {}
    for request in ordered_requests:
        requests_by_kind[request["payload"]["recovery_kind"]].append(request)
        request_refs[request["artifact_id"]] = context.artifact_ref(
            RECENSOR, "recovery-request", request["artifact_id"]
        )

    reviews_by_request = {request_id: [] for request_id in request_refs}
    for review in artifacts_for(context, RECENSOR, "review", act_id):
        if review.get("outcome") != "recovery-requested":
            continue
        payload = _payload(review, f"recovery-requested review of {act_id}")
        review_ordinal = payload.get("attempt_ordinal")
        request_ordinal = payload.get("recovery_request_ordinal")
        request_ref = payload.get("recovery_request_ref")
        matching_request = next(
            (
                request_id
                for request_id, reference in request_refs.items()
                if request_ref == reference
            ),
            None,
        )
        if (
            matching_request is None
            or request_ref not in review.get("inputs", [])
            or not _plain_int(review_ordinal)
            or review.get("attempt_id") != attempt_id(act_id, "recense", review_ordinal)
            or not _plain_int(request_ordinal)
        ):
            raise FatalAccounting(
                f"recovery-requested review of {act_id} has no exact matching recovery request"
            )
        request = requests_by_ordinal.get(request_ordinal)
        if request is None or request["artifact_id"] != matching_request:
            raise FatalAccounting(
                f"recovery-requested review of {act_id} names recovery attempt "
                f"{request_ordinal}, which is not the position of the request it references"
            )
        request_payload = _payload(request, f"recovery request for {act_id}")
        if (
            payload.get("perlectio_ref") != request_payload.get("perlectio_ref")
            or payload.get("recovery_kind") != request_payload.get("recovery_kind")
            or payload.get("recovery_policy") != budget
        ):
            raise FatalAccounting(
                f"recovery-requested review of {act_id} does not name the Perlectio, recovery "
                "kind, and policy its request assessed"
            )
        reviews_by_request[matching_request].append(review)

    missing_reviews = [
        request_id for request_id, reviews in reviews_by_request.items() if len(reviews) != 1
    ]
    if missing_reviews:
        raise FatalAccounting(
            f"act {act_id} has recovery request(s) without exactly one matching "
            f"recovery-requested review {sorted(missing_reviews)}; a crash between the two "
            "publications is named rather than turned into a later ordinal"
        )

    regions = artifacts_for(context, DESIGNATOR, "region", act_id)
    recrops_by_request = {request_id: [] for request_id in request_refs}
    recovery_regions = []
    # Validate the origin vocabulary through the shared reader so every stage agrees
    # what a recovery crop is; the count itself is unused.
    recovery_region_count(act_id, regions)
    for region in regions:
        payload = _payload(region, f"Designator region of {act_id}")
        if payload.get("origin") != "recovery":
            continue
        inputs = region.get("inputs")
        if not isinstance(inputs, list):
            raise FatalAccounting(f"recovery region of {act_id} has no input list")
        matches = [
            request_id for request_id, reference in request_refs.items() if reference in inputs
        ]
        if len(matches) != 1:
            raise FatalAccounting(
                f"recovery region of {act_id} is not bound to exactly one recorded recovery request"
            )
        recovery_regions.append(region)
        recrops_by_request[matches[0]].append(region)

    repeated_recrops = [
        request_id for request_id, recrops in recrops_by_request.items() if len(recrops) > 1
    ]
    if repeated_recrops:
        raise FatalAccounting(
            f"act {act_id} has more than one recovery crop for request(s) "
            f"{sorted(repeated_recrops)}; one request may not silently create a second reread"
        )
    return {
        "requests": ordered_requests,
        "regions": regions,
        "recovery_regions": recovery_regions,
        "requests_by_kind": requests_by_kind,
        "outstanding_request_ids": [
            request_id for request_id, recrops in recrops_by_request.items() if not recrops
        ],
    }


def preflight_recovery_history(context, budget: dict) -> None:
    """Name every broken recovery pair before publishing another review."""
    for act in expected_acts(context):
        recovery_state(context, act["act_id"], budget)


def _basis_facts(region: dict, what: str) -> dict:
    """The read-side facts that identify one Designator crop without its witness flag."""
    if not isinstance(region, dict):
        raise FatalAccounting(f"{what} is not an object")
    fields = (
        "region_id",
        "image_path",
        "image_sha256",
        "source_page_ordinal",
        "source_page_id",
        "transform",
    )
    facts = {field: region.get(field) for field in fields}
    if not isinstance(facts["region_id"], str) or not facts["region_id"]:
        raise FatalAccounting(f"{what} has no region identity")
    if not isinstance(facts["image_path"], str) or not facts["image_path"]:
        raise FatalAccounting(f"{what} has no crop image path")
    return facts


def _expected_basis_facts(region: dict, act_id: str) -> dict:
    payload = _payload(region, f"Designator region of {act_id}")
    transform = payload.get("transform")
    return _basis_facts(
        {
            "region_id": payload.get("region_id"),
            "image_path": payload.get("image_path"),
            "image_sha256": payload.get("image_sha256"),
            "source_page_ordinal": transform.get("source_page_ordinal")
            if isinstance(transform, dict)
            else None,
            "source_page_id": transform.get("source_page_id")
            if isinstance(transform, dict)
            else None,
            "transform": transform,
        },
        f"Designator region of {act_id}",
    )


def recensor_continuation_link(regions: list[dict], act_id: str) -> dict:
    """The Recensor's own continuation fact, derived from evidence alone.

    This link is the authoritative continuation relation; the seal's `has_continuation`
    is only the Designator's proposal, checked against it. A continuation needs
    proposal regions on two distinct pages, not merely two regions.
    """
    facts = [
        _expected_basis_facts(region, act_id)
        for region in regions
        if _payload(region, f"Designator region of {act_id}").get("origin") == "proposal"
    ]
    page_ordinals = sorted({row["source_page_ordinal"] for row in facts})
    return {
        "is_continuation": len(page_ordinals) > 1,
        "page_ordinals": page_ordinals,
        "region_ids": sorted(row["region_id"] for row in facts),
    }


def reconcile_continuation(act: dict, continuation_link: dict, act_id: str) -> bool:
    """Reconcile the seal's proposed continuation against the Recensor's own link.

    Returns whether the reading covers only part of a claimed continuation. Raises when
    the seal denies a continuation its own evidence proves: a proposal may not override
    the Recensor's authoritative fact.
    """
    if act["has_continuation"] and not continuation_link["is_continuation"]:
        return True
    if not act["has_continuation"] and continuation_link["is_continuation"]:
        raise FatalAccounting(
            f"act {act_id}'s own proposal regions span pages "
            f"{continuation_link['page_ordinals']}, but the Designator's sealed "
            "proposal claims no continuation for it; the Recensor's own "
            "reconciliation is the authoritative continuation fact and may not "
            "silently agree with a seal that under-claims against the evidence"
        )
    return False


def regions_by_source_page(context) -> dict[int, list[dict]]:
    """Every currently-cut Designator region's page-pixel bounds, by source page.

    Proposal and recovery regions of every act count as coverage, because the
    residual-ink check asks about the page's pixels, not one act's denominator. A page
    with no region at all has no entry, so its residual ink is never measured here and
    no act's review reports it.
    """
    by_page: dict[int, list[dict]] = {}
    for record in _records_of_kind(context, DESIGNATOR, "region"):
        payload = record.get("payload")
        transform = payload.get("transform") if isinstance(payload, dict) else None
        if not isinstance(transform, dict):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has no object transform"
            )
        ordinal = transform.get("source_page_ordinal")
        bounds = transform.get("bounds")
        # All four numbers: `residual_ink` indexes each, and a bare dict would fail by
        # traceback.
        if (
            not _plain_int(ordinal)
            or not isinstance(bounds, dict)
            or not all(_plain_int(bounds.get(side)) for side in _BOX_SIDES)
        ):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has an invalid transform"
            )
        by_page.setdefault(ordinal, []).append(bounds)
    return by_page


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted source-manifest row for each ordinal.

    Every stage that reads sealed Exemplar pixels rebuilds the submitted denominator
    before trusting a page's claim; the Designator and Armarium carry their own copies.
    """
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise FatalAccounting("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise FatalAccounting("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        if not _plain_int(ordinal):
            raise FatalAccounting(
                "run.json carries a source-manifest row without an integer ordinal"
            )
        if ordinal in sources:
            raise FatalAccounting(f"run.json repeats source ordinal {ordinal}")
        sources[ordinal] = row
    return sources


def sealed_page_images(context) -> dict[int, dict]:
    """Every sealed Exemplar page's own artifact, by ordinal, checked against the manifest.

    The residual-ink check reads raw bytes off `payload["image_path"]`, a self-declared
    field the envelope never relates to the page's digest-checked inputs, so it is
    verified first, as every stage that reads page pixels does.
    """
    pages: dict[int, dict] = {}
    for record in _records_of_kind(context, EXEMPLAR, "page"):
        if record["outcome"] != "sealed":
            continue
        ordinal = record["payload"].get("ordinal")
        if not _plain_int(ordinal):
            raise FatalAccounting(
                f"Exemplar page {record.get('artifact_id')} carries no integer ordinal"
            )
        if ordinal in pages:
            raise FatalAccounting(
                f"the Exemplar carries more than one sealed page for ordinal {ordinal}; "
                "the Recensor has no rule for selecting one page image"
            )
        pages[ordinal] = record

    # Second pass, so the structural denominator above is settled before any
    # pixel is touched.
    sources = _source_rows(context.run)
    for ordinal, record in pages.items():
        source = sources.get(ordinal)
        if source is None:
            raise FatalAccounting(
                f"a sealed Exemplar page names ordinal {ordinal}, which run.json never submitted"
            )
        try:
            verify_sealed_page_pixels(context.tree, context.run, source, record)
        except ContractError as error:
            raise FatalAccounting(
                f"sealed Exemplar page {ordinal} failed pixel verification; the residual-ink "
                "check may not read bytes over an unverified image_path"
            ) from error
    return pages


def capture_digest_by_page(sealed_pages: dict[int, dict]) -> dict[int, str]:
    """The capture identity every cross-capture consumer means, by page ordinal.

    Captures are named by the sealed Exemplar page's `source_sha256`, already verified.
    The source-manifest row is not that identity: its declared digest is optional and,
    for a page rendered from a container, names the container. A digest that cannot be
    stated is this stage's own accounting failure, so it refuses here.
    """
    digests: dict[int, str] = {}
    for ordinal, page in sorted(sealed_pages.items()):
        digest = page.get("payload", {}).get("source_sha256")
        if not is_sha256(digest):
            raise FatalAccounting(
                f"sealed Exemplar page {ordinal} carries no lowercase capture digest; the "
                "Recensor cannot name the capture its cross-capture accounting is about"
            )
        digests[ordinal] = digest
    return digests


def capture_digest_for(capture_digests: dict[int, str], page_ordinal: int, act_id: str) -> str:
    """One page's capture digest, refused by name when this run cannot state it."""
    digest = capture_digests.get(page_ordinal)
    if digest is None:
        raise FatalAccounting(
            f"act {act_id}'s recovery request names source page {page_ordinal}, for which this "
            "run has no verified sealed capture digest; the capture-specific gate may not be "
            "asked about a capture the Recensor cannot name"
        )
    return digest


def page_coverage_findings(context, sealed_pages: dict[int, dict] | None = None) -> dict[int, dict]:
    """Residual-ink findings for every sealed page with a region cut on it, once per run.

    The input is the page image itself, never the proposal set, a witness or a reading.
    The paper value is the Designator's shared inference under the sealed background
    policy, never the page's own histogram mode, which on a photographed opening is the
    bezel and hides all residual ink. A page whose paper the inference refuses gets a
    finding carrying the refusal and no counts. `sealed_pages` lets `main` share one
    pixel-verification pass.
    """
    regions = regions_by_source_page(context)
    if not regions:
        return {}
    background_config = load_background_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", background_config["config_sha256"])
    # The Designator's own page-spanning bound, so this audit sets aside the component
    # that stage accounts for.
    coverage_config = load_coverage_audit_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", coverage_config["config_sha256"])
    pages = sealed_page_images(context) if sealed_pages is None else sealed_pages
    findings: dict[int, dict] = {}
    for ordinal, bounds in regions.items():
        page = pages.get(ordinal)
        if page is None:
            raise FatalAccounting(
                f"a Designator region names source page {ordinal}, which the Exemplar "
                "did not seal; a crop of unsealed pixels is invariant #10's imbalance"
            )
        # Digest the bytes actually measured, not an earlier read (principle 8).
        image_bytes = context.tree.read_bytes(page["payload"]["image_path"])
        if digest_bytes(image_bytes) != page["payload"]["source_sha256"]:
            raise FatalAccounting(
                f"the sealed Exemplar page {ordinal} the residual-ink check read does not "
                "match the pixel digest its own page record verified"
            )
        width, height, rows = grayscale_rows(image_bytes)
        try:
            findings[ordinal] = residual_ink(
                width,
                height,
                rows,
                bounds,
                background_policy=resolve_background_policy(background_config, width, height),
                coverage_policy=resolve_coverage_audit_policy(coverage_config, width, height),
            )
        except BackgroundInferenceRefusal as error:
            # Without a paper value zero ink would be a false clean page.
            findings[ordinal] = {
                "ink_measurable": False,
                "named_finding": INK_NOT_MEASURABLE,
                "background_refusal": str(error),
                "background_config_sha256": background_config["config_sha256"],
            }
    return findings


def _region_page_ordinals(act_regions: list[dict]) -> set[int]:
    """The source pages of every region with an object payload and transform."""
    return {
        region["payload"]["transform"]["source_page_ordinal"]
        for region in act_regions
        if isinstance(region.get("payload"), dict)
        and isinstance(region["payload"].get("transform"), dict)
    }


def page_coverage_for(act_regions: list[dict], findings: dict[int, dict]) -> dict[str, list[int]]:
    """The residual-ink fact every review records: which pages this act's regions were
    cut from, and which of those are flagged.

    Every page the act's regions touch, so a continuation's far side is examined too.
    `checked_pages` holds only pages `findings` has, so "checked and clear" never covers
    "never checked". One derivation for every review shape, including a held act, so a
    flagged page's only evidence is never dropped.
    """
    present = [
        ordinal for ordinal in sorted(_region_page_ordinals(act_regions)) if ordinal in findings
    ]
    # A refused background is a third state beside checked-and-clear and never-checked,
    # listed apart from both.
    unmeasurable = {
        ordinal for ordinal in present if findings[ordinal].get("ink_measurable") is False
    }
    checked = [ordinal for ordinal in present if ordinal not in unmeasurable]
    return {
        "checked_pages": checked,
        "flagged_pages": [ordinal for ordinal in checked if findings[ordinal].get("flagged")],
        "unmeasurable_pages": sorted(unmeasurable),
    }


def ink_map_by_page(context) -> dict[int, dict | None]:
    """Read the sealed page-space evidence without re-decoding page pixels.

    A page the Ink Map published as `ink-not-measurable` maps to `None`: it is
    in the census and it has no retained runs, because the shared background
    inference refused its paper value and no threshold was ever cut.
    """
    coverage_config = None
    maps: dict[int, dict | None] = {}
    for record in _records_of_kind(context, INK_MAP, "ink-map"):
        payload = _payload(record, "ink-map")
        ordinal = payload.get("page_ordinal")
        evidence = payload.get("edge_findings")
        if not _plain_int(ordinal):
            raise FatalAccounting(
                "ink-map has a record without an integer page ordinal. The Recensor cannot bind "
                "its ink evidence to a sealed page. Restore the sealed Ink Map inventory or "
                "restart the run before rerunning the Recensor."
            )
        if ordinal in maps:
            raise FatalAccounting(
                f"ink-map repeats page ordinal {ordinal}. The Recensor has no rule for choosing "
                "which retained page-space evidence confirms witness pointers. Restore the sealed "
                "Ink Map inventory or restart the run before rerunning the Recensor."
            )
        if record.get("outcome") == INK_NOT_MEASURABLE:
            # Validate before discarding runs, so a malformed refusal cannot become the
            # same `None` as an honest one.
            try:
                refusal = validate_ink_not_measurable_payload(payload)
                context.require_sealed_config(
                    "designator-grouping", refusal["background_config_sha256"]
                )
            except ContractError as error:
                raise FatalAccounting(
                    f"ink-map page {ordinal} has an invalid sealed ink-not-measurable "
                    "payload. Restore the sealed Ink Map artifact or restart the run before "
                    "rerunning the Recensor."
                ) from error
            maps[ordinal] = None
            continue
        if record.get("outcome") not in {"mapped", "unclaimed-edge-ink"}:
            raise FatalAccounting(
                f"ink-map page {ordinal} has an unknown measured outcome. The Recensor cannot "
                "bind its retained runs to a current Ink Map measurement. Restore the sealed "
                "Ink Map artifact or restart the run before rerunning the Recensor."
            )
        try:
            measured = validate_measured_ink_map_payload(
                payload, audit_contrast=MINIMUM_CONTRAST_BELOW_BACKGROUND
            )
            context.require_sealed_config(
                "designator-grouping", measured["background_config_sha256"]
            )
        except ContractError as error:
            raise FatalAccounting(
                f"ink-map page {ordinal} has an invalid sealed measured payload. Restore the "
                "sealed Ink Map artifact or restart the run before rerunning the Recensor."
            ) from error
        try:
            if not isinstance(evidence, dict):
                raise ContractError("the measured payload has no ink-run evidence object")
            if coverage_config is None:
                coverage_config = load_coverage_audit_config(
                    context.args.designator_grouping_config
                )
            if coverage_config["config_sha256"] != measured["background_config_sha256"]:
                raise ContractError(
                    "the background and coverage instruments did not read the same sealed bytes"
                )
            initial_measure = reconcile_edge_finding_with_runs(
                payload.get("edge"),
                evidence,
                coverage_policy=resolve_coverage_audit_policy(
                    coverage_config, evidence.get("width"), evidence.get("height")
                ),
            )
            measured_outcome = "unclaimed-edge-ink" if initial_measure["flagged"] else "mapped"
            if record.get("outcome") != measured_outcome:
                raise ContractError(
                    "the measured outcome disagrees with the retained initial edge measurement"
                )
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise FatalAccounting(
                f"ink-map page {ordinal} has an edge finding that does not reconcile with its "
                f"retained {INK_RUNS_SCHEMA} page-space evidence. The Recensor cannot confirm "
                "witness pointers from damaged evidence. Restore the sealed Ink Map artifact "
                "or restart the run before rerunning the Recensor."
            ) from error
        retained_evidence = dict(evidence)
        if hasattr(context, "artifact_ref"):
            retained_evidence["_ink_map_ref"] = context.artifact_ref(
                INK_MAP, "ink-map", record["artifact_id"]
            )
        maps[ordinal] = retained_evidence
    return maps


def _ink_outside_cuts_in_box(evidence: dict, box: dict, covered: list[dict]) -> int:
    """Ink of the Ink Map's retained runs inside ``box`` and outside every cut region.

    An observation may overlap a recovery crop cut after it was recorded; subtracting
    every current crop keeps covered ink from funding another recovery.
    """
    width, height, rows = evidence.get("width"), evidence.get("height"), evidence.get("rows")
    if (
        not _plain_int(width)
        or width <= 0
        or not _plain_int(height)
        or height <= 0
        or not isinstance(rows, list)
        or len(rows) != height
    ):
        raise FatalAccounting(
            "ink-map edge findings have invalid dimensions. Their retained runs cannot be "
            "measured against a witness pointer, so reading them as empty would suppress a "
            "possible coverage finding. Restore the sealed Ink Map artifact or restart the "
            "run before rerunning the Recensor."
        )
    x0 = max(0, box["x"])
    y0 = max(0, box["y"])
    # Clamp the far edge to the near edge too: a box above the page would otherwise
    # slice `rows[0:-2]`, almost the whole page.
    x1 = max(x0, min(width, box["x"] + box["w"]))
    y1 = max(y0, min(height, box["y"] + box["h"]))
    total = 0
    for offset, row in enumerate(rows[y0:y1]):
        if not isinstance(row, list):
            raise FatalAccounting(
                "ink-map edge findings contain a malformed row. Its ink count cannot be "
                "measured reliably, so it cannot authorize recovery. Restore the sealed Ink "
                "Map artifact or restart the run before rerunning the Recensor."
            )
        previous_end = 0
        ink_spans: list[tuple[int, int]] = []
        for run in row:
            if not isinstance(run, list) or len(run) != 2 or not all(_plain_int(v) for v in run):
                raise FatalAccounting(
                    "ink-map edge findings contain a malformed run. Its ink count cannot be "
                    "measured reliably, so it cannot authorize recovery. Restore the sealed "
                    "Ink Map artifact or restart the run before rerunning the Recensor."
                )
            start, length = run
            # Ordered and disjoint, as `ink_runs` writes them; overlapping runs would
            # count ink twice.
            if start < previous_end or length <= 0 or start + length > width:
                raise FatalAccounting(
                    "ink-map edge findings have unordered or out-of-bounds runs. Counting them "
                    "could invent ink and authorize unsupported recovery. Restore the sealed "
                    "Ink Map artifact or restart the run before rerunning the Recensor."
                )
            previous_end = start + length
            ink_spans.append((max(x0, start), min(x1, start + length)))
        # A union, so overlapping act crops never subtract their shared pixels twice.
        cuts = _union(
            (max(x0, bounds["x"]), min(x1, bounds["x"] + bounds["w"]))
            for bounds in covered
            if bounds["y"] <= y0 + offset < bounds["y"] + bounds["h"]
        )
        total += sum(_length_outside(start, end, cuts) for start, end in ink_spans)
    return total


def _union(intervals) -> list[tuple[int, int]]:
    """Merge half-open intervals into sorted, disjoint ones; empty ones drop out."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _length_outside(start: int, end: int, cuts: list[tuple[int, int]]) -> int:
    """How much of ``[start, end)`` the sorted, disjoint ``cuts`` leave uncovered."""
    total = 0
    cursor = start
    for cut_start, cut_end in cuts:
        if cut_end <= cursor:
            continue
        if cut_start >= end:
            break
        total += max(0, min(cut_start, end) - cursor)
        cursor = max(cursor, cut_end)
        if cursor >= end:
            break
    return total + max(0, end - cursor)


def unclaimed_ink_observations(
    maps: dict[int, dict],
    unclaimed_observations: list,
    page_ordinal: int,
    cut_regions: dict[int, list[dict]],
    *,
    minimum_ink_pixels: int,
) -> list[dict]:
    """Which of this page's retained unclaimed observations point at real ink.

    A witness box is only a pointer: recovery needs independently measured ink outside
    every current cut, at least the sealed `minimum_ink_pixels`. Both are required
    arguments, so covered ink is never re-cut and no unsealed floor funds recovery. A
    retained observation with no map row or malformed bounds is fatal: missing evidence
    is not a measurement of zero.
    """
    evidence = maps.get(page_ordinal)
    if evidence is None and page_ordinal in maps:
        # The Ink Map refused this page, so no pointer can be confirmed. The page is
        # carried in `unmeasurable_pages`, so review can tell "nobody could say" from
        # "no ink".
        return []
    if evidence is None:
        if unclaimed_observations:
            raise FatalAccounting(
                f"page {page_ordinal} has retained unclaimed witness observations but no "
                "ink-map page-space evidence. The Recensor cannot determine whether those "
                "pointers cover real ink, so treating the missing map as zero ink would lose "
                "a coverage finding silently. Restore the page's sealed Ink Map artifact or "
                "restart the run before rerunning the Recensor."
            )
        return []
    covered = cut_regions.get(page_ordinal, [])
    requests = []
    for observation in unclaimed_observations:
        bounds = observation.get("bounds") if isinstance(observation, dict) else None
        if not _int_box(bounds):
            raise FatalAccounting(
                f"page {page_ordinal} has a retained unclaimed witness observation with no "
                "{x, y, w, h} bounds. Skipping it would read a malformed pointer as one that "
                "never pointed at ink, the same silent loss this gate exists to refuse. Restore "
                "the page's sealed Testimonium evidence or restart the run before rerunning the "
                "Recensor."
            )
        if bounds["w"] <= 0 or bounds["h"] <= 0:
            raise FatalAccounting(
                f"page {page_ordinal} has a retained unclaimed witness observation with a "
                "non-positive rectangle"
            )
        canonical_bounds = {
            "x": max(0, bounds["x"]),
            "y": max(0, bounds["y"]),
            "w": max(0, min(evidence["width"], bounds["x"] + bounds["w"]) - max(0, bounds["x"])),
            "h": max(0, min(evidence["height"], bounds["y"] + bounds["h"]) - max(0, bounds["y"])),
        }
        if canonical_bounds["w"] == 0 or canonical_bounds["h"] == 0:
            continue
        ink_pixels = _ink_outside_cuts_in_box(evidence, canonical_bounds, covered)
        if ink_pixels >= minimum_ink_pixels:
            request = {
                "page_ordinal": page_ordinal,
                "outside_ink_pixels": ink_pixels,
                # Reached only after measured ink confirmed the pointer; gives the
                # Designator exact sealed-image geometry to cut.
                "bounds": canonical_bounds,
            }
            for name in (
                "testimonium_ref",
                "testimonium_id",
                "observation_ordinal",
            ):
                if name in observation:
                    request[name] = observation[name]
            if "_ink_map_ref" in evidence:
                request["ink_map_ref"] = evidence["_ink_map_ref"]
            requests.append(request)
    return requests


# Request origin is recorded data because the page-wide bound must not depend
# on parsing mutable human-readable reason text.
COVERAGE_OBSERVATION_ORIGIN = "coverage-observation"
DECLARED_CROP_ORIGIN = "declared-incomplete-crop"
RECOVERY_ORIGINS = (COVERAGE_OBSERVATION_ORIGIN, DECLARED_CROP_ORIGIN)


def observation_funded_pages(context, acts: list[dict]) -> set[int]:
    """Pages whose one observation-funded recovery has already been spent.

    An observation is page-scoped but funds an act-scoped request, so the grant is
    counted from the retained tree, not per act or per pass. The first eligible act in
    sealed proposal order spends it; spending it claims nothing about coverage.
    """
    page_of = {act["act_id"]: act["page_ordinal"] for act in acts}
    funded: dict[int, str] = {}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "recovery-request":
            continue
        request = context.tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        payload = _payload(request, f"recovery request {entry['artifact_id']}")
        origin = payload.get("origin")
        if origin not in RECOVERY_ORIGINS:
            raise FatalAccounting(
                f"recovery request {entry['artifact_id']} names no recorded origin. The "
                "one-observation-one-request bound cannot be counted from the tree, so another "
                "request could be granted silently. Restore a valid request artifact or restart "
                "the run before rerunning the Recensor."
            )
        ordinal = page_of.get(request.get("subject_id"))
        if ordinal is None:
            raise FatalAccounting(
                f"recovery request {entry['artifact_id']} names an act outside the "
                "proposal seal's expected set. Its page-scoped recovery grant cannot be "
                "accounted to this run. Remove no evidence; inspect the inconsistent run tree "
                "and restart the run from its sealed inputs."
            )
        if origin == COVERAGE_OBSERVATION_ORIGIN:
            previous = funded.get(ordinal)
            if previous is not None:
                raise FatalAccounting(
                    f"page {ordinal} carries more than one observation-funded recovery "
                    f"request ({previous!r}, {entry['artifact_id']!r}). The page-wide one-grant "
                    "bound is already broken, and collapsing both would hide the extra spend. "
                    "Inspect both retained requests and restart the run before recovery continues."
                )
            funded[ordinal] = entry["artifact_id"]
    return set(funded)


def recovery_request_origin(*, declared: bool, outside_ink_requests: list) -> str:
    """Name the route that actually caused one fallback-recrop request.

    A declaration takes precedence over a coinciding ink-confirmed observation, so the
    page's one observation grant is not spent on a request the declaration caused.
    """
    if declared:
        return DECLARED_CROP_ORIGIN
    if outside_ink_requests:
        return COVERAGE_OBSERVATION_ORIGIN
    raise FatalAccounting(
        "a recovery request has neither a declared nor an ink-confirmed origin. Publishing it "
        "would spend bounded recovery without coverage evidence. Fix the request caller before "
        "rerunning the Recensor."
    )


def unresolved_observation_hold(
    outside_ink_requests: list,
    page_ordinal: int,
    funded_pages: set[int],
) -> tuple[str, str] | None:
    """Keep a still-confirmed pointer visible when no request can be published.

    A supported measured recrop exists on either ingress, so this only reports
    the recorded grant or budget reason that actually prevented publication.
    """
    if not outside_ink_requests:
        return None
    grant_state = (
        "the page's one observation-funded recovery request is already recorded"
        if page_ordinal in funded_pages
        else "the bounded recovery policy cannot admit another request"
    )
    return (
        "held-for-review",
        "Unit 9 still confirms ink in a witness-reported pointer outside every "
        f"current cut, but {grant_state}; the unresolved coverage evidence is "
        "held visibly rather than dropped behind an accepted review",
    )


def _require_reconciled_pixels(ordinal: int, pixel_counts: dict) -> tuple[int, int, int]:
    """Pixel-count typing and `claimed + residual == total`, shared by every page shape."""
    if any(not _plain_int(count) or count < 0 for count in pixel_counts.values()):
        raise FatalAccounting(
            f"Designator conservation page {ordinal} has malformed measured pixel "
            "counts; total, claimed, and residual must be non-negative integers"
        )
    total = pixel_counts["total_ink_pixel_count"]
    claimed = pixel_counts["claimed_pixel_count"]
    residual = pixel_counts["residual_pixel_count"]
    if claimed + residual != total:
        raise FatalAccounting(
            f"Designator conservation page {ordinal} pixel accounting does not "
            "reconcile: claimed_pixel_count + residual_pixel_count does not equal "
            "total_ink_pixel_count"
        )
    return total, claimed, residual


def geometry_coverage_inputs(context) -> dict[int, dict]:
    """Consume, and independently reconcile, the Designator's conservation denominator.

    Every sealed page with a non-held act needs a record, and its residual components
    must equal the held residual acts in the proposal seal. An all-held page never
    reached sealing, so its absence stays absence.

    A page that withheld its enumeration (more components than the sealed policy lets
    one page list) is its own shape: no `residual_components`, exactly one page-residual
    act and no per-component ones. Every condition is recomputed from the record and
    the seal.
    """
    acts = expected_acts(context)
    residual_keys = {act["act_key"] for act in acts if act["act_key"].startswith("residual:")}
    page_residual_keys = [
        act["act_key"] for act in acts if act["act_key"].startswith("page-residual:")
    ]
    findings: dict[int, dict] = {}
    for record in _records_of_kind(context, DESIGNATOR, "conservation"):
        payload = _payload(record, f"Designator conservation {record['artifact_id']}")
        ordinal = payload.get("page_ordinal")
        measurable = payload.get("ink_measurable")
        components = payload.get("residual_components")
        enumeration = payload.get("residual_enumeration")
        pixel_count_fields = (
            "total_ink_pixel_count",
            "claimed_pixel_count",
            "residual_pixel_count",
        )
        pixel_counts = {field: payload.get(field) for field in pixel_count_fields}
        if not _plain_int(ordinal) or not isinstance(measurable, bool) or ordinal in findings:
            raise FatalAccounting("Designator conservation has malformed or duplicate page facts")
        if enumeration not in RESIDUAL_ENUMERATIONS:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} records its residual enumeration as "
                f"{enumeration!r}, which is outside the closed set {RESIDUAL_ENUMERATIONS}; "
                "this stage cannot tell a page with no unclaimed ink from one whose unclaimed "
                "ink was counted and not listed without being told which it is"
            )
        if enumeration == RESIDUAL_ENUMERATION_WITHHELD:
            findings[ordinal] = _withheld_page_conservation(
                ordinal, payload, measurable, pixel_counts, residual_keys, page_residual_keys
            )
            continue
        if enumeration == RESIDUAL_ENUMERATION_AGGREGATED:
            findings[ordinal] = _aggregate_page_conservation(
                context,
                ordinal,
                payload,
                measurable,
                pixel_counts,
                residual_keys,
                page_residual_keys,
                acts,
                record["subject_id"],
            )
            continue
        page_residual_act_count = page_residual_keys.count(page_residual_act_key(ordinal))
        if page_residual_act_count > 0:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} enumerated its residual components "
                "and is also held as one page-residual review item; a page is accounted for by "
                "one held act per residual or by the single item that replaced them, never by "
                "both"
            )
        if not isinstance(components, list):
            raise FatalAccounting("Designator conservation has malformed or duplicate page facts")
        declared_count = payload.get("residual_component_count")
        if not _plain_int(declared_count) or declared_count != len(components):
            raise FatalAccounting(
                f"Designator conservation page {ordinal} names residual_component_count "
                f"{declared_count!r} but lists {len(components)} residual components; the count "
                "a review is shown is the count the list beside it supports"
            )
        if not measurable and components:
            raise FatalAccounting(
                f"unmeasured Designator conservation page {ordinal} must carry no residual "
                "components"
            )
        for index, component in enumerate(components):
            bounds = component.get("bounds") if isinstance(component, dict) else None
            pixel_count = component.get("pixel_count") if isinstance(component, dict) else None
            if not _int_box(bounds) or not _plain_int(pixel_count) or pixel_count < 0:
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} residual component {index} "
                    "is malformed"
                )
        if measurable:
            _, _, residual = _require_reconciled_pixels(ordinal, pixel_counts)
            if sum(component["pixel_count"] for component in components) != residual:
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} residual component pixel sum "
                    "does not equal residual_pixel_count"
                )
        elif any(count is not None for count in pixel_counts.values()):
            raise FatalAccounting(
                f"unmeasured Designator conservation page {ordinal} must carry None for "
                "total_ink_pixel_count, claimed_pixel_count, and residual_pixel_count"
            )
        expected = {f"residual:{ordinal}:{index}" for index in range(len(components))}
        actual = {key for key in residual_keys if key.startswith(f"residual:{ordinal}:")}
        if measurable and actual != expected:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} has residual components whose "
                "invariant-8 held-act partition diverges from the components it measured"
            )
        if not measurable and actual:
            raise FatalAccounting(
                f"unmeasured Designator conservation page {ordinal} minted residual acts"
            )
        findings[ordinal] = {
            "ink_measurable": measurable,
            "residual_component_count": len(components),
            "residual_act_count": len(actual),
            "residual_enumeration": RESIDUAL_ENUMERATION_COMPLETE,
            "max_residual_components": None,
            # Zero, and checked rather than assumed: the refusal above is what
            # proves an enumerated page carries no page-residual item.
            "page_residual_act_count": page_residual_act_count,
            "reason": None,
        }
    required_ordinals = {act["page_ordinal"] for act in acts if act["outcome"] != "held"}
    missing = sorted(required_ordinals - findings.keys())
    if missing:
        pages = ", ".join(str(ordinal) for ordinal in missing)
        if len(missing) == 1:
            raise FatalAccounting(
                f"Designator conservation page {pages} carries a non-held expected act "
                "but has no conservation record"
            )
        raise FatalAccounting(
            f"Designator conservation pages {pages} carry non-held expected acts "
            "but have no conservation records"
        )
    return findings


def _aggregate_page_conservation(
    context,
    ordinal: int,
    payload: dict,
    measurable: bool,
    pixel_counts: dict,
    residual_keys: set,
    page_residual_keys: list,
    acts: list[dict],
    page_id: str,
) -> dict:
    """Reconcile both retained partitions and their exact held-act identities."""
    if not measurable:
        raise FatalAccounting(
            f"unmeasured Designator conservation page {ordinal} cannot aggregate components"
        )
    promoted = payload.get("residual_components")
    aggregate = payload.get("aggregated_residual_components")
    if not isinstance(promoted, list) or not isinstance(aggregate, list) or not aggregate:
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} has no complete retained partition"
        )
    width, height = payload.get("page_width"), payload.get("page_height")
    if any(not _plain_int(value) or value <= 0 for value in (width, height)):
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} has no positive page geometry"
        )
    policy = sealed_residual_presentation_policy(context)
    if any(
        not _plain_int(payload.get(name)) or payload.get(name) != value
        for name, value in policy.items()
    ):
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} does not carry the exact sealed "
            "presentation thresholds"
        )
    declared_counts = (
        payload.get("residual_promoted_component_count"),
        payload.get("residual_aggregated_component_count"),
        payload.get("residual_component_count"),
    )
    if any(not _plain_int(value) or value < 0 for value in declared_counts) or declared_counts != (
        len(promoted),
        len(aggregate),
        len(promoted) + len(aggregate),
    ):
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} does not reconcile its combined "
            "component counts"
        )
    identities = set()
    for label, components in (("promoted", promoted), ("aggregate", aggregate)):
        for index, component in enumerate(components):
            bounds = component.get("bounds") if isinstance(component, dict) else None
            pixels = component.get("pixel_count") if isinstance(component, dict) else None
            if (
                not _page_rect(bounds)
                or bounds["x"] + bounds["w"] > width
                or bounds["y"] + bounds["h"] > height
                or not _plain_int(pixels)
                or pixels < 0
                or pixels > bounds["w"] * bounds["h"]
            ):
                raise FatalAccounting(
                    f"aggregate Designator conservation page {ordinal} {label} component "
                    f"{index} is malformed"
                )
            identity = tuple(bounds[name] for name in _BOX_SIDES)
            if identity in identities:
                raise FatalAccounting(
                    f"aggregate Designator conservation page {ordinal} repeats component "
                    f"identity {identity} across its partition"
                )
            identities.add(identity)
            significant = (
                pixels >= policy["residual_aggregate_max_pixel_count"]
                or bounds["w"] * bounds["h"] >= policy["residual_aggregate_max_area_px"]
            )
            if (label == "promoted") != significant:
                raise FatalAccounting(
                    f"aggregate Designator conservation page {ordinal} puts {label} component "
                    f"{index} on the wrong side of the sealed presentation threshold"
                )
    _, _, residual = _require_reconciled_pixels(ordinal, pixel_counts)
    if sum(component["pixel_count"] for component in [*promoted, *aggregate]) != residual:
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} combined component pixels do "
            "not equal residual_pixel_count"
        )
    actual = {key for key in residual_keys if key.startswith(f"residual:{ordinal}:")}
    expected = {f"residual:{ordinal}:{index}" for index in range(len(promoted))}
    if actual != expected:
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} promoted held-act identities "
            "diverge from the retained promoted partition"
        )
    actual_promoted_identities = {
        (act["act_key"], act["act_id"])
        for act in acts
        if act["act_key"].startswith(f"residual:{ordinal}:")
    }
    expected_promoted_identities = {
        (
            f"residual:{ordinal}:{index}",
            derive_act_id(page_id, "residual", component["bounds"]),
        )
        for index, component in enumerate(promoted)
    }
    if actual_promoted_identities != expected_promoted_identities:
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} promoted act identities do "
            "not derive from the retained component geometry"
        )
    held_as_one = page_residual_keys.count(page_residual_act_key(ordinal))
    if held_as_one != 1:
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} is accounted for by "
            f"{held_as_one} page-residual acts rather than exactly one"
        )
    page_rows = [act for act in acts if act["act_key"] == page_residual_act_key(ordinal)]
    expected_page_id = derive_act_id(
        page_id, "page-residual", {"x": 0, "y": 0, "w": width, "h": height}
    )
    if len(page_rows) != 1 or page_rows[0]["act_id"] != expected_page_id:
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} page-hold identity does not "
            "derive from its exact page geometry"
        )
    return {
        "ink_measurable": True,
        "residual_component_count": len(promoted) + len(aggregate),
        "residual_act_count": len(actual),
        "residual_enumeration": RESIDUAL_ENUMERATION_AGGREGATED,
        "max_residual_components": None,
        "page_residual_act_count": held_as_one,
        "reason": (
            f"{len(promoted)} significant residual components remain individual held acts; "
            f"{len(aggregate)} below-threshold components remain retained on one page hold"
        ),
    }


def _withheld_page_conservation(
    ordinal: int,
    payload: dict,
    measurable: bool,
    pixel_counts: dict,
    residual_keys: set,
    page_residual_keys: list,
) -> dict:
    """One page held as a single review item in place of its residual components.

    Reconciled against the seal, like an enumerated page. Without the list the
    per-component pixel sum cannot be recomputed, so the checks are the ink accounting
    and the partition: exactly one page-residual act and no per-component ones. The
    finding keeps the key set of every other shape, so consumers read one schema; each
    value is true of this page.
    """
    if not measurable:
        raise FatalAccounting(
            f"unmeasured Designator conservation page {ordinal} withheld its residual "
            "enumeration; a page with no threshold to separate ink from paper enumerated "
            "nothing because nothing was measured, not because a bound stopped it"
        )
    if "residual_components" in payload:
        raise FatalAccounting(
            f"withheld Designator conservation page {ordinal} still carries a "
            "residual_components key; the key is omitted when the enumeration is withheld, so "
            "that no consumer reads a present list as the complete one"
        )
    count = payload.get("residual_component_count")
    bound = payload.get("max_residual_components")
    if any(not _plain_int(value) or value < 0 for value in (count, bound)):
        raise FatalAccounting(
            f"withheld Designator conservation page {ordinal} names no integer residual "
            "component count and no integer bound it was judged against"
        )
    if count <= bound:
        raise FatalAccounting(
            f"withheld Designator conservation page {ordinal} counted {count} residual "
            f"components against a bound of {bound}, which it does not exceed; a page within "
            "the bound owes one held act per residual, not a withheld enumeration"
        )
    _require_reconciled_pixels(ordinal, pixel_counts)
    minted = sorted(key for key in residual_keys if key.startswith(f"residual:{ordinal}:"))
    if minted:
        raise FatalAccounting(
            f"withheld Designator conservation page {ordinal} withheld its residual "
            f"enumeration and still minted {len(minted)} per-component residual acts; the "
            "unlisted ink is accounted for by the single item that replaced those acts, never "
            "by both at once"
        )
    held_as_one = page_residual_keys.count(page_residual_act_key(ordinal))
    if held_as_one != 1:
        raise FatalAccounting(
            f"withheld Designator conservation page {ordinal} is accounted for by "
            f"{held_as_one} page-residual acts in the proposal seal rather than exactly one; "
            "unlisted ink is accounted for by the single review item that replaced it, or it "
            "is lost silently"
        )
    return {
        "ink_measurable": measurable,
        "residual_component_count": count,
        "residual_act_count": 0,
        "residual_enumeration": RESIDUAL_ENUMERATION_WITHHELD,
        "max_residual_components": bound,
        "page_residual_act_count": held_as_one,
        "reason": (
            f"this page's reconciliation counted {count} residual components against the "
            f"sealed bound of {bound}, so the Designator held the page as one page-residual "
            "review item and did not list them; no per-component held act exists for this "
            "page by design, and the per-component pixel sum is the one reconciliation this "
            "stage cannot recompute against a list that was deliberately not carried"
        ),
    }


def current_act_attachments(context) -> dict[str, dict]:
    """The current act-attachment record per act, from one manifest pass.

    Uses the shared `latest_attempt`: manifest order is a hash, so the last listed
    record is not the current one.
    """
    records: dict[str, list[dict]] = {}
    for entry in stage_manifest(context, ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        records.setdefault(entry["subject_id"], []).append(
            context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        )
    return {
        act_id: latest_attempt(group, f"act-attachment for {act_id}", operation="act-attachment")
        for act_id, group in records.items()
    }


def current_page_testimonia(context) -> dict[tuple[int, str], dict]:
    """The current page Testimonium per page and chair, from retained history.

    Testimony is append-only, so the Attestatores manifest legitimately carries
    superseded page records after a later pass.  Group by the semantic subject
    and use the shared attempt derivation: manifest order is a hash, not a
    currency signal, and duplicate or gapped ordinals are accounting failures.
    """
    records: dict[tuple[int, str], list[dict]] = {}
    for record in _records_of_kind(context, ATTESTATORES, "page-testimonium"):
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        ordinal, chair = payload.get("page_ordinal"), payload.get("chair")
        # A boolean ordinal hashes as its integer counterpart, so accepting one
        # here would merge page `true` into page 1 before currency is derived.
        if not _plain_int(ordinal) or not isinstance(chair, str):
            raise FatalAccounting("page Testimonium has no textual page identity")
        records.setdefault((ordinal, chair), []).append(record)
    return {
        (ordinal, chair): latest_attempt(
            group,
            f"page Testimonium for page {ordinal}, chair {chair}",
            operation=f"read:{chair}",
        )
        for (ordinal, chair), group in records.items()
    }


def _covered_intervals(
    spans: list[tuple[int, int, str]], text_length: int
) -> list[tuple[int, int]]:
    """Validate and merge coverage without allocating one slot per character."""
    for start, end, _ in spans:
        if start < 0 or end < start or end > text_length:
            raise FatalAccounting("act attachment span lies outside its page Testimonium")
    return _union((start, end) for start, end, _ in spans)


def uncovered_non_whitespace_ranges(text: str, covered_intervals: list[tuple[int, int]]) -> dict:
    """Losslessly compact uncovered non-whitespace offsets into half-open ranges.

    Page testimony crosses an untrusted boundary and can be much larger than a
    normal reading.  Coverage therefore stays proportional to the number of
    retained attachment spans, not to the response length; the text itself is
    scanned once without materializing a page-sized boolean bitmap.
    """
    ranges = []
    count = 0
    interval_index = 0
    for index, char in enumerate(text):
        while (
            interval_index < len(covered_intervals)
            and covered_intervals[interval_index][1] <= index
        ):
            interval_index += 1
        covered = (
            interval_index < len(covered_intervals)
            and covered_intervals[interval_index][0] <= index
        )
        if covered or char.isspace():
            continue
        count += 1
        if ranges and ranges[-1]["end"] == index:
            ranges[-1]["end"] = index + 1
        else:
            ranges.append({"start": index, "end": index + 1})
    return {"ranges": ranges, "count": count}


def reconcile_page_roles(
    context,
    attachments: dict[str, dict],
    page_testimonia: dict[tuple[int, str], dict],
) -> None:
    """Re-derive every page Testimonium's `page_role` from the whole page.

    Only the whole-page view can distinguish `mixed` from a uniform primary or
    continuation page. Attachments are the denominator because the Perlector has
    already reconciled their page set to the regions it read; walking Designator
    regions again would duplicate that grouping rule.
    """
    primary_page = {act["act_id"]: act["page_ordinal"] for act in expected_acts(context)}
    contributors: dict[int, set[str]] = {}
    expected_page_records: set[tuple[int, str]] = set()
    for act_id, attachment in attachments.items():
        rows = _payload(attachment, f"attachment for {act_id}").get("attachments")
        if not isinstance(rows, list):
            raise FatalAccounting(
                f"attachment for {act_id} has no row list; its page contributors cannot be "
                "reconciled; restore the closed attachment payload"
            )
        for row in rows:
            if not isinstance(row, dict) or not row.get("page_witness"):
                continue
            ordinal = row.get("page_ordinal")
            if not _plain_int(ordinal):
                raise FatalAccounting(
                    f"act {act_id} page-witness attachment carries no integer page ordinal; "
                    "its page Testimonium cannot be addressed; restore the contributing page"
                )
            if act_id not in primary_page:
                raise FatalAccounting(
                    f"act {act_id} carries a page-witness attachment but the proposal seal "
                    "names no such act; the page evidence has no sealed owner; restore the "
                    "proposal partition or remove the foreign attachment"
                )
            chair = row.get("chair")
            if not isinstance(chair, str) or not chair:
                raise FatalAccounting(
                    f"act {act_id} page-witness attachment has no chair identity; its page "
                    "Testimonium cannot be reconciled; restore the configured chair name"
                )
            expected_page_records.add((ordinal, chair))
            contributors.setdefault(ordinal, set()).add(
                "primary" if primary_page[act_id] == ordinal else "continuation"
            )
    actual_page_records = set(page_testimonia)
    missing = sorted(expected_page_records - actual_page_records)
    orphaned = sorted(actual_page_records - expected_page_records)
    if missing or orphaned:
        raise FatalAccounting(
            "page-witness attachments and current page Testimonia disagree: "
            f"missing page/chair record(s) {missing}, orphaned record(s) {orphaned}; page "
            "evidence cannot be reconciled; restore the retained Attestatores records"
        )
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        roles = contributors[ordinal]
        # Do not consume this shared set; every chair on the page uses it.
        expected = next(iter(roles)) if len(roles) == 1 else "mixed"
        if payload.get("page_role") != expected:
            raise FatalAccounting(
                f"page {ordinal}'s Testimonium for chair {chair!r} claims page_role "
                f"{payload.get('page_role')!r}; the acts attached to that page make it "
                f"{expected!r}; rebuild the page record from those retained attachments"
            )


#: The alignment reason forced onto every attachment row on a page that is not its
#: act's primary page. The act anchor comes from the primary page, so no row there can
#: be `aligned`.
CONTINUATION_NO_ACT_ANCHOR = "continuation-page-no-act-anchor"


def continuation_unmeasured_reason(
    ordinal: int,
    observations: list[tuple[str, int, list]],
    *,
    beside_a_measured_verdict: bool = False,
) -> str:
    """Say, in one sentence, why a declared-unanchored act's uncovered text has no verdict.

    The observation is kept, but it is not a shortfall: by declaration no span of such an
    act can enter the union (principle 8). With no aligned spans on the page this is the
    page's `reason`; beside a measured verdict it rides as `unmeasured_reason`, scoped to
    those acts.
    """
    observed = "; ".join(
        f"chair {chair!r} saw {count} uncovered non-whitespace character(s) beside "
        f"{', '.join(acts)} declared unanchored"
        for chair, count, acts in sorted(observations)
    )
    if beside_a_measured_verdict:
        subject = (
            f"page {ordinal}'s testimony content coverage is unmeasured for the acts the "
            f"Perlector declares {CONTINUATION_NO_ACT_ANCHOR}, so no witness span can be "
            "attached to them and what is uncovered beside them is not a measured shortfall"
        )
    else:
        subject = (
            f"page {ordinal}'s testimony content coverage is unmeasured: the Perlector declares "
            f"every act attachment on this continuation page {CONTINUATION_NO_ACT_ANCHOR}, so no "
            "witness span can be attached there and what is uncovered is not a measured shortfall"
        )
    return (
        f"{subject} "
        f"({observed}); continuation-page alignment in the Perlector is what would make this "
        "measurement real"
    )


def _proposal_pages_of_acts(context) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """Each page's expected acts and sealed proposal regions, from one seal read."""
    acts_by_page: dict[int, list[dict]] = {}
    proposal_regions_by_page: dict[int, list[dict]] = {}
    for act in expected_acts(context):
        found_page = False
        for region in artifacts_for(context, DESIGNATOR, "region", act["act_id"]):
            payload = _payload(region, f"Designator region of {act['act_id']}")
            transform = payload.get("transform")
            if payload.get("origin") != "proposal" or not isinstance(transform, dict):
                continue
            ordinal = transform.get("source_page_ordinal")
            if not _plain_int(ordinal):
                raise FatalAccounting(
                    f"Designator region of {act['act_id']} has no integer page ordinal; its "
                    "page testimony cannot be grouped; restore the region's sealed transform"
                )
            page_acts = acts_by_page.setdefault(ordinal, [])
            if act not in page_acts:
                page_acts.append(act)
            bounds = transform.get("bounds")
            # A degenerate rectangle would overlap nothing and manufacture an
            # unrouted-observation finding that drives recovery.
            if not _page_rect(bounds):
                raise FatalAccounting(
                    f"Designator proposal region of {act['act_id']} has no page-pixel bounds"
                )
            proposal_regions_by_page.setdefault(ordinal, []).append(region)
            found_page = True
        # Unit callers may provide the proposal seal without materialized
        # regions; its primary page remains a known one-page denominator.
        if not found_page:
            acts_by_page.setdefault(act["page_ordinal"], []).append(act)
    return acts_by_page, proposal_regions_by_page


def _page_rows_by_chair(
    context,
    acts_by_page: dict[int, list[dict]],
    attachments: dict[str, dict],
    page_testimonia: dict[tuple[int, str], dict],
) -> dict[tuple[int, str], list[tuple[str, dict]]]:
    """Every page-witness attachment row, by page and chair, bound to its current record."""
    rows_by_page_chair: dict[tuple[int, str], list[tuple[str, dict]]] = {}
    for ordinal, acts in acts_by_page.items():
        for act in acts:
            attachment = attachments.get(act["act_id"])
            if attachment is None:
                raise FatalAccounting(f"act {act['act_id']} has no attachment for content coverage")
            rows = _payload(attachment, f"attachment for {act['act_id']}").get("attachments")
            if not isinstance(rows, list):
                raise FatalAccounting(f"attachment for {act['act_id']} has no rows")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not row.get("page_witness")
                    # Missing must not default to the page currently being
                    # scanned, which would count one malformed row on every page.
                    or row["page_ordinal"] != ordinal
                ):
                    continue
                chair = row.get("chair")
                page_testimonium = page_testimonia.get((ordinal, chair))
                if page_testimonium is None or row.get("testimonium_ref") != context.artifact_ref(
                    ATTESTATORES, "page-testimonium", page_testimonium["artifact_id"]
                ):
                    raise FatalAccounting(
                        f"act {act['act_id']} page witness {chair!r} on page {ordinal} "
                        "references no current page Testimonium; retained evidence is missing "
                        "or stale; restore the referenced Attestatores record"
                    )
                rows_by_page_chair.setdefault((ordinal, chair), []).append((act["act_id"], row))
    return rows_by_page_chair


def _aligned_spans(rows: list[tuple[str, dict]]) -> tuple[list[tuple[int, int, str]], list[str]]:
    """The aligned witness spans of these rows, and the acts declared unanchored."""
    spans = []
    declared_unanchored: list[str] = []
    for act_id, row in rows:
        alignment = row.get("alignment")
        if (
            row.get("attached")
            and isinstance(alignment, dict)
            and alignment.get("status") == "aligned"
        ):
            span = alignment.get("witness_span")
            if not isinstance(span, dict) or not all(
                _plain_int(span.get(k)) for k in ("start", "end")
            ):
                raise FatalAccounting("attached page witness has malformed alignment span")
            spans.append((span["start"], span["end"], act_id))
        elif (
            isinstance(alignment, dict)
            and alignment.get("status") == "unaligned"
            and alignment.get("reason") == CONTINUATION_NO_ACT_ANCHOR
        ):
            # Not conditional on `attached`: unattached continuation rows would
            # otherwise read as a measured shortfall.
            declared_unanchored.append(act_id)
    return spans, declared_unanchored


def testimony_content_findings(context) -> dict[int, dict]:
    """Compare each page witness's text to its own aligned act attachments.

    Testimony to testimony; no Perlectio text participates. A non-whitespace page
    character outside the union of the aligned spans is a visible coverage shortfall,
    never a verdict about which witness is right.
    """
    attachments = current_act_attachments(context)
    page_testimonia = current_page_testimonia(context)
    acts_by_page, proposal_regions_by_page = _proposal_pages_of_acts(context)
    rows_by_page_chair = _page_rows_by_chair(context, acts_by_page, attachments, page_testimonia)
    # After the rows, so a missing record names the act that lost it; this catches the
    # converse orphan (a page record no act owns) before any finding is built.
    reconcile_page_roles(context, attachments, page_testimonia)
    findings: dict[int, dict] = {}
    unanchored_by_page: dict[int, list[tuple[str, int, list[str]]]] = {}
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        try:
            validate_reportable_observations(payload.get("observed", []))
        except ContractError as error:
            raise FatalAccounting(
                f"page Testimonium {record['artifact_id']} for page {ordinal}, chair {chair!r} "
                f"has malformed observed geometry: {error}"
            ) from error
        presented = payload.get("presented")
        disagreement = payload.get("partition_disagreement")
        if disagreement is not None:
            try:
                validate_partition_disagreement(
                    disagreement,
                    observed=payload.get("observed"),
                    source_page_id=(
                        presented.get("source_page_id") if isinstance(presented, dict) else None
                    ),
                    testimonium_id=record["artifact_id"],
                    proposal_boxes=[
                        region["payload"]["transform"]["bounds"]
                        for region in proposal_regions_by_page.get(ordinal, [])
                    ],
                )
            except ContractError as error:
                raise FatalAccounting(
                    f"page Testimonium {record['artifact_id']} has false partition facts: {error}"
                ) from error
        observed = payload.get("observed")
        if isinstance(presented, dict) and presented and isinstance(observed, list):
            # The retained partition is audit evidence and may not suppress a finding.
            unclaimed = unrouted_observations([record], proposal_regions_by_page.get(ordinal, []))
        else:
            unclaimed = []
        if disagreement is not None or unclaimed:
            finding = findings.setdefault(
                ordinal,
                {"by_chair": {}, "shortfall": False},
            )
            testimonium_ref = context.artifact_ref(
                ATTESTATORES, "page-testimonium", record["artifact_id"]
            )
            for observation in unclaimed:
                observation["testimonium_ref"] = testimonium_ref
                observation["observation_ordinal"] = observation.pop("ordinal")
            finding.setdefault("unclaimed_observations", []).extend(copy.deepcopy(unclaimed))
            # Not a text shortfall: that would hold every act on the page over an
            # observation assigned to none of them.
        if "payload" not in payload:
            if record.get("outcome") in WITNESS_READING_OUTCOMES:
                raise FatalAccounting(
                    "reading page Testimonium has no retained derived payload for content "
                    f"coverage: {record['artifact_id']} for page {ordinal}, chair {chair!r}; "
                    "restore the retained Attestatores record"
                )
            # Nothing read here; the absence stays visible through the witness floor.
            continue
        text = payload.get("payload")
        if not isinstance(text, str):
            # Structured testimony: no comparable text, so it cannot meet the floor either.
            continue
        spans, declared_unanchored = _aligned_spans(rows_by_page_chair.get((ordinal, chair), []))
        covered_intervals = _covered_intervals(spans, len(text))
        uncovered = uncovered_non_whitespace_ranges(text, covered_intervals)
        finding = findings.setdefault(ordinal, {"by_chair": {}, "shortfall": False})
        finding["by_chair"][chair] = {
            "attached_spans": [
                {"start": start, "end": end, "act_id": act_id}
                for start, end, act_id in sorted(spans)
            ],
            "uncovered_non_whitespace": uncovered,
        }
        if declared_unanchored and uncovered["count"]:
            unanchored_by_page.setdefault(ordinal, []).append(
                (chair, uncovered["count"], sorted(declared_unanchored))
            )
        if covered_intervals or not declared_unanchored:
            # Only an empty covered union is unmeasured; a neighbour's unanchored
            # declaration may not hide this chair's real measurement.
            finding["shortfall"] = finding["shortfall"] or bool(uncovered["count"])
    for finding in findings.values():
        if finding["by_chair"]:
            continue
        # No chair reported text: `False` would publish a measurement nobody took.
        finding["shortfall"] = None
        finding.setdefault("reason", NO_PAGE_CONTENT_COVERAGE["reason"])
    for ordinal, observations in unanchored_by_page.items():
        finding = findings[ordinal]
        if finding["shortfall"]:
            # A measured shortfall outranks the unmeasured verdict, which rides beside it.
            finding.setdefault(
                "unmeasured_reason",
                continuation_unmeasured_reason(
                    ordinal, observations, beside_a_measured_verdict=True
                ),
            )
            continue
        # Measured against a union the Perlector declared empty: not a shortfall.
        finding["shortfall"] = None
        finding.setdefault("reason", continuation_unmeasured_reason(ordinal, observations))
    return findings


def recovery_request_reason(*, declared_crop: bool, unclaimed_observation: bool) -> str:
    """Every triggered coverage cause must remain explicit in the request."""
    causes = []
    if declared_crop:
        causes.append("the crop may be incomplete")
    if unclaimed_observation:
        causes.append("a page witness reported ink outside every sealed proposal")
    if not causes:
        raise FatalAccounting("a fallback-recrop request has no recorded coverage cause")
    return f"{' and '.join(causes)}; an expanded recrop is requested"


NO_PAGE_CONSERVATION = {
    "ink_measurable": None,
    "residual_component_count": None,
    "residual_act_count": None,
    "residual_enumeration": None,
    "max_residual_components": None,
    "page_residual_act_count": None,
    "reason": (
        "the Designator published no conservation record for this page, so nothing on it "
        "was measured; its acts are held for the reason the page itself carries"
    ),
}


NO_PAGE_CONTENT_COVERAGE = {
    "by_chair": None,
    "shortfall": None,
    # A structured page report is retained testimony but supplies no comparable
    # text; describing that as no report would make the measurement record false.
    "reason": (
        "no page witness supplied comparable page text for this page, so testimony content "
        "coverage was not measured; its acts are already held or floored by their own causes"
    ),
}


def geometry_coverage_for(findings: dict[int, dict], ordinal: int) -> dict:
    """Return one review's private copy of a page's geometry-coverage fact.

    The Designator publishes a record for every page it sealed, so a missing record
    means the page never reached it (a door refusal). Defaulting to
    `ink_measurable: False` would restate a measurement nobody took (principle 8), so
    absence is recorded as absence.
    """
    return copy.deepcopy(findings.get(ordinal, NO_PAGE_CONSERVATION))


def testimony_content_for_page(findings: dict[int, dict], ordinal: int) -> dict:
    """Return one review's private copy of a page-level content finding.

    Each act gets its own object, so preparing one act cannot mutate a sibling's
    unpublished evidence. A page absent from `findings` records absence, not a clean
    page.
    """
    return copy.deepcopy(findings.get(ordinal, NO_PAGE_CONTENT_COVERAGE))


def testimony_content_for_continuation_pages(
    findings: dict[int, dict], act_regions: list[dict], primary_ordinal: int
) -> list[dict]:
    """Restate the content finding of every page this act spans but is not primary on.

    Without this, a continuation page's finding reached no review. These rows are not
    route inputs: their verdict is `None` where the Perlector declared the page
    unanchorable, and routing on `None` would route an absence; they make it visible
    (principle 2). Present and empty for a one-page act, so "no continuation" differs
    from "never derived".
    """
    ordinals = sorted(_region_page_ordinals(act_regions) - {primary_ordinal})
    return [
        {"page_ordinal": ordinal, **testimony_content_for_page(findings, ordinal)}
        for ordinal in ordinals
    ]


def _describe_termination(record: dict | None) -> str:
    """The sealed truncation verdict and its signals, in the words a review carries."""
    if not isinstance(record, dict) or not isinstance(record.get("signals"), dict):
        return "the truncation instrument did not classify the re-proof call as complete"
    signals = record["signals"]
    declared = signals.get("stop_reason_declared")
    flagged = [
        name
        for name in ("unclosed_structure", "length_suspicious", "ends_abruptly")
        if signals.get(name) is True
    ]
    return (
        f"the truncation instrument classified the re-proof call {record.get('classification')!r} "
        f"(engine stop word {declared!r}; computed signals raised: "
        f"{', '.join(flagged) if flagged else 'none'})"
    )


def review_route_from_findings(
    *,
    cross_capture_occluded_everywhere: bool = False,
    cross_capture_unresolved: bool | None = False,
    testimony_shortfall: bool | None,
    audit_unresolved: bool | None,
    under_witnessed: bool,
    unreconciled: bool = False,
    audit_examination: str | None = None,
    audit_reproof_truncation: dict | None = None,
    assessment_malformed: bool = False,
    assessment_problem: str | None = None,
) -> tuple[str, str] | None:
    """Compose every independent review cause in stable priority order.

    All active causes are retained under one ``held-for-review`` outcome.
    ``None`` means the corresponding measurement does not exist and therefore
    routes like ``False``; absence is not a measured shortfall.
    """
    # A shape guard that cannot refuse anything yet; `publish_review` and the recovery
    # payload are the screens that bite.
    refuse_capture_preference(
        {
            "cross_capture_occluded_everywhere": cross_capture_occluded_everywhere,
            "cross_capture_unresolved": cross_capture_unresolved,
            "testimony_shortfall": testimony_shortfall,
            "audit_unresolved": audit_unresolved,
            "audit_examination": audit_examination,
            "audit_reproof_truncation": audit_reproof_truncation,
            "assessment_malformed": assessment_malformed,
            "assessment_problem": assessment_problem,
            "under_witnessed": under_witnessed,
            "unreconciled": unreconciled,
        },
        what="a Recensor review route",
    )
    # The examination is the fact and `audit_unresolved` derives from it, so both must
    # agree and the route follows the examination.
    if audit_examination is not None:
        derived = unresolved_state(audit_examination)
        if audit_unresolved is not None and audit_unresolved != derived:
            raise ContractError(
                f"a Recensor review route was handed audit_unresolved={audit_unresolved!r} "
                f"beside examination {audit_examination!r}, which derives {derived!r}"
            )
        audit_unresolved = derived
    reasons = []
    if cross_capture_occluded_everywhere:
        reasons.append(
            "every registered capture's remaining required surface was explicitly measured "
            "and found occluded; recropping cannot reveal ink no capture can see, so the act "
            "is held rather than recovery spent chasing a view that does not exist"
        )
    if cross_capture_unresolved:
        reasons.append(
            "the logical act's cross-capture visible-surface union does not yet reach its "
            "complete required surface, and what remains is neither fully seen nor exactly "
            "occluded everywhere"
        )
    if testimony_shortfall:
        reasons.append(
            "a page Testimonium contains non-whitespace text outside the ordered union "
            "of that witness's aligned act attachments; testimony coverage is incomplete "
            "at the whole-page level, so the uncovered text may belong to another act on "
            "the same page and the hold is page-scoped by design"
        )
    if audit_unresolved:
        if audit_examination == EXAMINATION_INCOMPLETE:
            # Names the instrument's verdict, not an engine word that may not exist.
            reasons.append(
                "the Perlector's audit re-proof of this act did not complete: "
                f"{_describe_termination(audit_reproof_truncation)}, so the flag(s) it was "
                "sent to settle stand unassessed; the establishing reading and the incomplete "
                "re-proof are both retained, and the act is held rather than delivered on a "
                "re-examination that never finished"
            )
        elif audit_examination == EXAMINATION_REPROOF_REJECTED:
            reasons.append(
                "the Perlector's audit re-proof for this act completed but rewrote text "
                "outside every location its own flag identified; the rewrite is refused rather "
                "than published, the establishing reading is retained, and the act is held "
                "rather than delivered on a re-examination that overran its own scope"
            )
        elif audit_examination in (None, EXAMINATION_CAP_EXHAUSTED):
            # `None` means the caller did not name the examination; it gets the generic
            # cap-exhausted reason.
            reasons.append(
                "the Perlector exhausted its sealed audit re-proof cap with unresolved span(s); "
                "they remain explicit uncertainty rather than a silent retry"
            )
        else:
            raise ContractError(
                f"a Recensor review route found audit_unresolved with examination "
                f"{audit_examination!r}, which is none of the states this composer knows how "
                "to hold for"
            )
    if assessment_malformed:
        # Held, never re-rolled. The problem is quoted: it alone says what went wrong.
        reason = (
            "the reader's doubt report over this act could not be anchored to its text and is "
            "retained as a malformed assessment; the act is held rather than delivered with "
            "its doubts unread"
        )
        if isinstance(assessment_problem, str) and assessment_problem:
            reason = f"{reason} ({assessment_problem})"
        reasons.append(reason)
    if under_witnessed:
        reasons.append(
            "the configured act-level witness floor is not met; a witness failure is not coverage"
        )
    if unreconciled:
        reasons.append("the act did not reconcile and needs a human")
    if not reasons:
        return None
    return "held-for-review", "; ".join(reasons)


def current_review(context, act_id: str) -> dict | None:
    """This act's current Recensor review, or `None` before its first.

    `stage_manifest` rebuilds fresh from the tree on every call, so this also
    sees a review this same pass already published for the act -- not only
    ones from an earlier invocation.
    """
    reviews = artifacts_for(context, RECENSOR, "review", act_id)
    if not reviews:
        return None
    return latest_attempt(reviews, f"Recensor review of {act_id}", operation="recense")


def publish_review(
    context,
    *,
    subject_id: str,
    outcome: str,
    prior: dict | None,
    inputs: list[dict],
    payload: dict,
) -> dict:
    """Write a review only after rejecting witness-selection vocabulary.

    A review's content can change between passes without the act recovering, because
    page-wide facts come from every act on its page. So the prior review's ordinal is
    tried first (unchanged content reuses byte for byte), and a fresh ordinal is minted
    only when the store proves the content differs. The ordinal is stamped here, never
    trusted from the caller.

    The whole payload is screened, not only the route inputs: a review is a second
    durable record, and a future direct payload field would otherwise go unchecked.
    """
    refuse_capture_preference(payload, what="a Recensor review")
    measurement_field = "testimony_content_coverage"
    try:
        validate_testimony_content_coverage(payload[measurement_field])
        measurement_field = "testimony_content_coverage_continuation"
        validate_testimony_content_coverage_continuation(payload[measurement_field])
        measurement_field = "cross_capture_coverage"
        coverage = payload[measurement_field]
        if coverage is not None:
            if not isinstance(coverage, dict):
                raise SchemaRefusal("cross-capture coverage is neither an object nor null")
            validate_cross_capture_coverage(coverage)
    except (KeyError, SchemaRefusal, TypeError) as error:
        raise FatalAccounting(
            f"the Recensor review of {subject_id!r} has malformed measurement evidence "
            f"in {measurement_field}: {error}"
        ) from error

    def publish_at(ordinal: int) -> dict:
        return context.publish(
            kind="review",
            subject_id=subject_id,
            outcome=outcome,
            attempt=attempt_id(subject_id, "recense", ordinal),
            inputs=inputs,
            payload={**payload, "attempt_ordinal": ordinal},
        )

    if prior is None:
        return publish_at(1)
    try:
        return publish_at(prior["payload"]["attempt_ordinal"])
    except IncompatibleReuse:
        return publish_at(prior["payload"]["attempt_ordinal"] + 1)


def _reconcile_reading_regions(reading: dict, regions: list[dict], act_id: str) -> list[dict]:
    """Require a completed Perlectio to name exactly every region currently cut."""
    basis = reading_basis_regions(reading, f"reading of {act_id}")
    expected_by_id = {
        facts["region_id"]: facts
        for facts in (_expected_basis_facts(region, act_id) for region in regions)
    }
    actual_by_id = {
        facts["region_id"]: facts
        for facts in (_basis_facts(region, f"reading of {act_id}") for region in basis)
    }
    if len(expected_by_id) != len(regions) or len(actual_by_id) != len(basis):
        raise FatalAccounting(
            f"act {act_id} repeats a crop identity in its cut or read basis; duplicate evidence "
            "cannot count as recovered coverage"
        )
    if actual_by_id != expected_by_id:
        raise FatalAccounting(
            f"act {act_id}'s latest Perlectio does not name exactly the Designator regions "
            "currently cut for it; a recovery crop may not disappear before it is reread"
        )
    return basis


def _refuse_an_unhandled_designator_terminal(act: dict) -> None:
    """Name a Designator outcome that ends an act but has no handling here yet.

    `excluded` and `failed` are terminal like `held` and would otherwise be misreported
    as an act with no reading. Their review record is undecided and the Designator emits
    neither today, so this refuses by name until that handling lands here.
    """
    category = terminal_category(DESIGNATOR, act["outcome"])
    if category is None:
        return
    raise FatalAccounting(
        f"act {act['act_id']} carries the terminal Designator outcome {act['outcome']!r} "
        f"({category.value}), which ends the act before any reading — but the Recensor has "
        "no review record for it yet, and nothing may pass through this stage unaccounted "
        "for. Only 'held' is handled today"
    )


def preflight_review_evidence(context, budget: dict) -> None:
    """Validate every readable act before publishing a review for any one of them."""
    for act in expected_acts(context):
        act_id = act["act_id"]
        if act["outcome"] == "held":
            designator_hold(context, act_id)
            continue
        _refuse_an_unhandled_designator_terminal(act)
        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
        if not readings:
            raise FatalAccounting(
                f"act {act_id} reached the Recensor with no reading at all. A unit "
                "in no terminal set is a fatal accounting imbalance (#10)"
            )
        state = recovery_state(context, act_id, budget)
        expected_readings = len(state["recovery_regions"]) + 1
        if len(readings) != expected_readings:
            raise FatalAccounting(
                f"act {act_id} carries {len(readings)} Perlectio attempt(s) for "
                f"{len(state['recovery_regions'])} recovery crop(s); every reread must answer "
                "one recorded recrop and no reading may appear unrequested"
            )
        latest = latest_attempt(readings, f"reading of {act_id}", operation="perlegere")
        # The Archetypus and the export check this too, since any may be reached first.
        require_current_witness_basis(
            act_id,
            latest,
            artifacts_for(context, ATTESTATORES, "testimonium", act_id),
            f"the current reading of {act_id}",
        )
        context.artifact_ref(PERLECTOR, "perlectio", latest["artifact_id"])
        audit_state(context, latest, act_id, expected_act_key=act["act_key"])
        if classify(PERLECTOR, latest["outcome"]) is OutcomeClass.COMPLETED:
            for region in _reconcile_reading_regions(latest, state["regions"], act_id):
                context.input_ref(region["image_path"])


def write_partition_receipt(context, budget: dict) -> None:
    """Rebuild the scoped Recensor partition receipt from disk, never from manifests.

    Manifests are a cache, checked against disk first; the denominator is rederived
    through `expected_acts`, and every record is read afresh. The receipt speaks only
    for the proposal-act and witness denominators at review time, not for the run's
    final export: a page with no region cut on it lies outside every denominator here
    (principle 2).
    """
    for stage in (DESIGNATOR, ATTESTATORES, PERLECTOR, RECENSOR):
        if not context.tree.manifest_agrees_with_disk(stage):
            raise FatalAccounting(
                f"the stored {stage} manifest disagrees with its on-disk artifacts; the "
                "Recensor partition receipt refuses a cache as its denominator"
            )
    acts = expected_acts(context)
    expected_by_id = {act["act_id"]: act for act in acts}
    reviews_by_act: dict[str, list[dict]] = {act_id: [] for act_id in expected_by_id}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] not in {"review", "recovery-request"}:
            continue
        record = context.tree.read_artifact(RECENSOR, entry["kind"], entry["artifact_id"])
        if record["subject_id"] not in expected_by_id:
            raise FatalAccounting(
                f"Recensor {entry['kind']} {record['artifact_id']} names act "
                f"{record['subject_id']!r} outside the proposal-act denominator"
            )
        if entry["kind"] == "review":
            reviews_by_act[record["subject_id"]].append(record)

    proposal_seal_ref = context.artifact_ref(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    items = []
    for act_id in sorted(expected_by_id):
        act = expected_by_id[act_id]
        review = latest_attempt(
            reviews_by_act[act_id], f"Recensor review of {act_id}", operation="recense"
        )
        payload = _payload(review, f"Recensor review of {act_id}")
        coverage = validate_chair_coverage(context, act_id, context.witness_floor)
        if payload.get("act_key") != act["act_key"] or payload.get("coverage") != coverage:
            raise FatalAccounting(
                f"Recensor review of {act_id} does not retain the act key and witness coverage "
                "recomputed from disk"
            )
        recovery_state(context, act_id, budget)
        items.append(
            {
                "act_id": act_id,
                "act_key": act["act_key"],
                "designator_outcome": act["outcome"],
                "review_ref": context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
                "review_outcome": review["outcome"],
                "partition_class": classify(RECENSOR, review["outcome"]).value,
                "coverage": coverage,
            }
        )
    receipt = build_recensor_partition_receipt(
        run_id=context.tree.run_id,
        config_digest=context.run["config_digest"],
        proposal_seal_ref=proposal_seal_ref,
        items=items,
    )
    context.tree.write_recensor_partition_receipt(receipt)


def real_ingress(context) -> bool:
    """Whether this run authority names the real route, by the shared reader."""
    return is_real_ingress(context.run)


def declared_scenario(context) -> dict | None:
    """The declared scenario on the fixture route; `None` on a real submission."""
    return None if real_ingress(context) else scenario_for(context.fixture, context.scenario)


def declared_unreconciled(scenario: dict | None, act_key: str) -> bool:
    """Whether a declared scenario holds this act as unreconciled.

    The only feeder of the `unreconciled` route cause. On a real submission it is
    `False` because nothing fed it, not because the act measured as reconciled; the
    cross-act anomaly check is Pass C's, arriving as `audit_unresolved`.
    """
    if scenario is None:
        return False
    return act_key in scenario["hold_acts"]


def declared_recovery(scenario: dict | None, act_key: str) -> bool:
    """Whether a declared scenario asks a recrop for this act.

    `False` with no scenario, because nothing fed it -- not because the act
    was measured as needing none. On a real submission the only producer of
    a recovery request is ink measured outside the live crop union, which
    reaches `recovery_request_origin` as COVERAGE_OBSERVATION_ORIGIN.
    """
    if scenario is None:
        return False
    return act_key in scenario["recover_acts"]


def _publish_designator_hold_review(
    context,
    act: dict,
    *,
    budget: dict,
    coverage: dict,
    geometry_coverage: dict,
    content_coverage: dict,
    content_findings: dict[int, dict],
    page_findings: dict[int, dict],
) -> None:
    """An explicit review for a Designator-held act, so its terminal category derives.

    A hold may still have a cut near-side region (only a continuation's page failed to
    seal), so the page facts come from what was cut rather than being reported empty.
    """
    act_id = act["act_id"]
    hold, hold_path = designator_hold(context, act_id)
    hold_regions = artifacts_for(context, DESIGNATOR, "region", act_id)
    publish_review(
        context,
        subject_id=act_id,
        outcome="held-for-review",
        prior=current_review(context, act_id),
        inputs=[context.input_ref(hold_path)]
        + [context.input_ref(region["payload"]["image_path"]) for region in hold_regions],
        payload={
            "act_key": act["act_key"],
            "reason": f"the Designator held this act: {hold['payload']['reason']}",
            "coverage": coverage,
            "geometry_coverage": geometry_coverage,
            "testimony_content_coverage": content_coverage,
            "testimony_content_coverage_continuation": (
                testimony_content_for_continuation_pages(
                    content_findings, hold_regions, act["page_ordinal"]
                )
            ),
            "continuation": recensor_continuation_link(hold_regions, act_id),
            "page_coverage": page_coverage_for(hold_regions, page_findings),
            "recoveries_used": 0,
            "budget_allowed": budget["allowed"],
            "absolute_cap": budget["absolute_cap"],
            # No Perlectio, so no audit: distinct from audited-and-resolved (False).
            "audit_unresolved": None,
            "audit_examination": None,
            "uncertainty_assessment": None,
            "cross_capture_coverage": None,
        },
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_stage_context(args, RECENSOR, registry_factory=registry_factory)
    # The policy parsed when the run's binding was checked, never re-read: a rewrite in
    # between would publish an allowance the run never sealed.
    budget = context.recovery_policy
    context.require_sealed_config("recovery", budget["config_sha256"])
    scenario = declared_scenario(context)
    floor = context.witness_floor

    # Before any publication, so a malformed later act never leaves an earlier act's
    # review behind.
    preflight_witness_denominator(context, floor)
    preflight_recovery_history(context, budget)
    preflight_review_evidence(context, budget)

    cut_regions = regions_by_source_page(context)
    # Pixels no crop came from are never read.
    sealed_pages = sealed_page_images(context) if cut_regions else {}
    capture_digests = capture_digest_by_page(sealed_pages)
    page_findings = page_coverage_findings(context, sealed_pages)
    geometry_inputs = geometry_coverage_inputs(context)
    content_findings = testimony_content_findings(context)
    ink_maps = ink_map_by_page(context)
    coverage_config = load_coverage_audit_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", coverage_config["config_sha256"])
    minimum_ink_pixels = coverage_config["coverage_audit"]["minimum_ink_pixels"]
    occlusions = occlusion_records_by_page(context)
    proposal_geometry: dict[str, dict] = {}
    # Counted from the tree: an in-memory counter would reset after the requested recrop.
    funded_pages = observation_funded_pages(context, expected_acts(context))

    held = 0
    for act in expected_acts(context):
        act_id, act_key = act["act_id"], act["act_key"]

        coverage = validate_chair_coverage(context, act_id, floor)
        content_coverage = testimony_content_for_page(content_findings, act["page_ordinal"])
        geometry_coverage = geometry_coverage_for(geometry_inputs, act["page_ordinal"])

        if act["outcome"] == "held":
            _publish_designator_hold_review(
                context,
                act,
                budget=budget,
                coverage=coverage,
                geometry_coverage=geometry_coverage,
                content_coverage=content_coverage,
                content_findings=content_findings,
                page_findings=page_findings,
            )
            held += 1
            continue

        state = recovery_state(context, act_id, budget)
        if state["outstanding_request_ids"]:
            # The matching review already records this hold; a retry must not accept
            # the act before the Designator cuts the requested crop.
            held += 1
            continue

        # Non-empty: `preflight_review_evidence` refused an act with no reading.
        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)

        # Every review names the exact Perlectio it assessed, as input and payload, so
        # the Archetypus can prove it establishes that reading.
        latest = latest_attempt(readings, f"reading of {act_id}", operation="perlegere")
        latest_payload = _payload(latest, f"reading of {act_id}")
        audit_facts = audit_state(context, latest, act_id, expected_act_key=act["act_key"]) or {}
        audit_unresolved = audit_facts.get("unresolved")
        audit_examination = audit_facts.get("examination")
        # `None` means the Perlectio carries no assessment object.
        assessment = latest_payload.get("uncertainty_assessment")
        assessment_record = (
            {"state": assessment.get("state"), "problem": assessment.get("problem")}
            if isinstance(assessment, dict)
            else None
        )
        # The survey must come from the exact Perlectio this review assesses.
        cross_coverage = act_cross_capture_coverage(
            context,
            act_id,
            latest_payload,
            occlusions=occlusions,
            proposal_geometry=proposal_geometry,
        )
        (
            cross_capture_occluded_everywhere,
            cross_capture_unresolved,
        ) = cross_capture_review_causes(cross_coverage)
        findings_route = review_route_from_findings(
            cross_capture_occluded_everywhere=cross_capture_occluded_everywhere,
            cross_capture_unresolved=cross_capture_unresolved,
            testimony_shortfall=content_coverage["shortfall"],
            audit_unresolved=audit_unresolved,
            under_witnessed=coverage["under_witnessed"],
            unreconciled=declared_unreconciled(scenario, act_key),
            audit_examination=audit_examination,
            audit_reproof_truncation=audit_facts.get("reproof_truncation"),
            assessment_malformed=(assessment_record or {}).get("state") == "malformed",
            assessment_problem=(assessment_record or {}).get("problem"),
        )
        reading_class = classify(PERLECTOR, latest["outcome"])
        reading_ref = context.artifact_ref(PERLECTOR, "perlectio", latest["artifact_id"])
        basis_regions = (
            reading_basis_regions(latest, f"reading of {act_id}")
            if reading_class is OutcomeClass.COMPLETED
            else []
        )

        continuation_link = recensor_continuation_link(state["regions"], act_id)
        continuation_shortfall = reconcile_continuation(act, continuation_link, act_id)

        # Against the page image itself, never a stage's claim. A flagged page holds
        # every act on it, because nobody knows which act the uncovered ink belongs to.
        page_coverage = page_coverage_for(state["regions"], page_findings)
        flagged_pages = page_coverage["flagged_pages"]
        # Recorded, never routed: see `testimony_content_for_continuation_pages`.
        continuation_content_coverage = testimony_content_for_continuation_pages(
            content_findings, state["regions"], act["page_ordinal"]
        )

        used_total = len(state["requests"])
        used_fallback = len(state["requests_by_kind"][FALLBACK_RECROP])
        allowed_fallback = recovery_kind_budget(budget, FALLBACK_RECROP)
        # A witness box is only a pointer: recovery needs measured ink outside the live
        # crop union, or a misreported box could spend budget or hold an act on no ink.
        outside_ink_requests = unclaimed_ink_observations(
            ink_maps,
            content_coverage.get("unclaimed_observations", []),
            act["page_ordinal"],
            cut_regions,
            minimum_ink_pixels=minimum_ink_pixels,
        )
        wants_recovery = (
            declared_recovery(scenario, act_key)
            or (bool(outside_ink_requests) and act["page_ordinal"] not in funded_pages)
        ) and used_total == 0
        observation_hold = unresolved_observation_hold(
            outside_ink_requests, act["page_ordinal"], funded_pages
        )
        # The request's own position among this act's requests, not the review's
        # ordinal, which follows content.
        request_ordinal = used_total + 1

        # Enforced at the request boundary: the kind allowance, the pooled total and the
        # absolute cap must each permit it, because a policy file can be edited.
        if (
            not continuation_shortfall
            # Cross-capture geometry neither funds nor vetoes recovery; only a measured
            # ink observation and bounded grants do.
            and wants_recovery
            and used_fallback < allowed_fallback
            and used_total < budget["allowed"]
            and used_total < budget["absolute_cap"]
        ):
            # The Recensor asks; the Designator cuts. Only `fallback-recrop` is
            # requested: `page-level-reread` stays a budgeted kind, but nothing
            # downstream can honour it yet, and a request that can only be refused turns
            # a hold into a failure.
            request_origin = recovery_request_origin(
                declared=declared_recovery(scenario, act_key),
                outside_ink_requests=outside_ink_requests,
            )
            if request_origin == COVERAGE_OBSERVATION_ORIGIN:
                observation = outside_ink_requests[0]
                required = (
                    "testimonium_ref",
                    "testimonium_id",
                    "observation_ordinal",
                    "ink_map_ref",
                )
                if any(name not in observation for name in required) or not isinstance(
                    observation.get("ink_map_ref"), dict
                ):
                    raise FatalAccounting(
                        "an ink-confirmed recovery observation has no retained Ink Map and "
                        "Testimonium references; refusing before publishing a request the "
                        "Designator cannot independently verify"
                    )
            if request_origin == COVERAGE_OBSERVATION_ORIGIN:
                # A second spelling of the gate that cannot fire on the production path:
                # every conjunct is proved above. It catches an edit that loosens the
                # live gate alone, not one that loosens both.
                dossier = latest_payload.get("dossier")
                gate = capture_specific_recovery(
                    logical_act_id=(
                        dossier["logical_act_id"]
                        if isinstance(dossier, dict) and "logical_act_id" in dossier
                        else act_id
                    ),
                    # The sealed page's verified pixel digest, which the partition and
                    # autopsia use; the source-manifest row is not a capture identity.
                    source_sha256=capture_digest_for(capture_digests, act["page_ordinal"], act_id),
                    page_ordinal=act["page_ordinal"],
                    # Reachable only from a measured ink observation.
                    ink_confirmed=True,
                    page_observation_grant_available=act["page_ordinal"] not in funded_pages,
                    act_budget_available=(
                        used_fallback < allowed_fallback
                        and used_total < budget["allowed"]
                        and used_total < budget["absolute_cap"]
                    ),
                )
                if not gate["admitted"]:
                    raise FatalAccounting(
                        f"act {act_id}'s ink-confirmed recovery request is being published, "
                        "but Unit 19C's own capture-specific gate says it should not be "
                        f"admitted: {gate['reason']}"
                    )
            # Screened inline, not in a helper: `test_quality_firewall.py` finds this
            # write by its literal `kind="recovery-request"` and reads the conditions
            # around it.
            recovery_payload = {
                "act_key": act_key,
                "attempt_ordinal": request_ordinal,
                "recovery_kind": FALLBACK_RECROP,
                # As data, so the page-wide bound counts a fact rather than parsing prose.
                "origin": request_origin,
                "reason": recovery_request_reason(
                    declared_crop=declared_recovery(scenario, act_key),
                    unclaimed_observation=len(outside_ink_requests) > 0,
                ),
                "budget_allowed": budget["allowed"],
                "budget_used": used_total,
                "kind_budget_allowed": allowed_fallback,
                "kind_budget_used": used_fallback,
                "coverage": coverage,
                "geometry_coverage": geometry_coverage,
                "testimony_content_coverage": content_coverage,
                "testimony_content_coverage_continuation": continuation_content_coverage,
                "perlectio_ref": reading_ref,
                "recovery_policy": budget,
                # Declared fixture recovery keeps its fixture geometry; a measured
                # observation supplies its confirmed page-space rectangle.
                **(
                    {
                        "recovery_bounds": outside_ink_requests[0]["bounds"],
                        "coverage_observation": {
                            "testimonium_ref": outside_ink_requests[0]["testimonium_ref"],
                            "testimonium_id": outside_ink_requests[0]["testimonium_id"],
                            "observation_ordinal": outside_ink_requests[0]["observation_ordinal"],
                            "bounds": outside_ink_requests[0]["bounds"],
                        },
                        "ink_map_ref": outside_ink_requests[0]["ink_map_ref"],
                        "outside_ink_pixels": outside_ink_requests[0]["outside_ink_pixels"],
                        "minimum_ink_pixels": minimum_ink_pixels,
                    }
                    if request_origin == COVERAGE_OBSERVATION_ORIGIN
                    else {}
                ),
            }
            refuse_capture_preference(recovery_payload, what="a Recensor recovery request")
            request = context.publish(
                kind="recovery-request",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recover", request_ordinal),
                inputs=[reading_ref]
                + (
                    [
                        outside_ink_requests[0]["testimonium_ref"],
                        outside_ink_requests[0]["ink_map_ref"],
                    ]
                    if request_origin == COVERAGE_OBSERVATION_ORIGIN
                    else []
                ),
                payload=recovery_payload,
            )
            # Spent where the request is published, so an act refused by budget or
            # continuation does not consume the page's grant.
            if request_origin == COVERAGE_OBSERVATION_ORIGIN:
                funded_pages.add(act["page_ordinal"])
            request_ref = context.input_ref(request.relative_path)
            publish_review(
                context,
                subject_id=act_id,
                outcome="recovery-requested",
                prior=current_review(context, act_id),
                inputs=[reading_ref, request_ref],
                payload={
                    "act_key": act_key,
                    # The request this review answers, distinct from the review's own
                    # content-derived ordinal; `recovery_state` cross-checks it.
                    "recovery_request_ordinal": request_ordinal,
                    "recovery_kind": FALLBACK_RECROP,
                    "coverage": coverage,
                    "geometry_coverage": geometry_coverage,
                    "testimony_content_coverage": content_coverage,
                    "testimony_content_coverage_continuation": continuation_content_coverage,
                    "continuation": continuation_link,
                    "page_coverage": page_coverage,
                    "perlectio_ref": reading_ref,
                    "recovery_request_ref": request_ref,
                    "recovery_policy": budget,
                    # This act was audited; omitting the field would read back as None,
                    # "no audit exists".
                    "audit_unresolved": audit_unresolved,
                    "audit_examination": audit_examination,
                    "uncertainty_assessment": assessment_record,
                    "cross_capture_coverage": cross_coverage,
                },
            )
            held += 1
            continue

        # The Archetypus copies the latest reading's text, so text nobody successfully
        # read is held visibly (principle 2).
        blank_evidence = None
        if reading_class is not OutcomeClass.COMPLETED:
            # `no-readable-text` is the Perlector's own positive finding of absence, so
            # it alone may seal blank if the witnesses corroborate it; other
            # non-completed outcomes fall through to the hold. One collapse feeds both
            # maps, so the gate and the outcomes agree on the current attempt.
            current_attempts = chair_current_attempts(context, act_id)
            current_outcomes = chair_outcomes(current_attempts)
            corroborating_chairs = (
                blank_corroboration(
                    coverage,
                    current_outcomes,
                    act_attachment_facts(context, act_id, current_attempts),
                    chair_read_evidence(current_attempts),
                    witness_uncovered=bool(state["recovery_regions"]),
                )
                if (
                    latest["outcome"] == "no-readable-text"
                    and not continuation_shortfall
                    and not flagged_pages
                    # Every hold cause the ordinary chain would apply: `confirmed-blank`
                    # is terminal, so a cause that appears only in that chain would be
                    # silently overridden (principle 2).
                    and findings_route is None
                    # Confirmed ink in a witness pointer outside every cut, with no
                    # request published (grant spent or budget exhausted), is also a
                    # shortfall `confirmed-blank` must not override.
                    and observation_hold is None
                )
                else None
            )
            if corroborating_chairs is not None:
                outcome, reason = (
                    "confirmed-blank",
                    "the Perlector's own reading found no-readable-text, and every witness "
                    f"that actually read this act ({', '.join(corroborating_chairs)}) "
                    "independently reports the same absence; sealed blank with that evidence"
                    + (
                        "; page ink could not be measured or reconciled for this act's "
                        "recorded page evidence"
                        if page_coverage["unmeasurable_pages"]
                        or geometry_coverage.get("ink_measurable") is False
                        else ""
                    ),
                )
                # The blank's evidence as data, not only prose. The page field is named
                # for exactly what was measured: no residual ink outside coverage, never
                # inside the act's own crop (principle 8).
                blank_evidence = {
                    "perlector_outcome": latest["outcome"],
                    "corroborating_chairs": corroborating_chairs,
                    "pages_without_residual_ink_outside_coverage": page_coverage["checked_pages"],
                }
            else:
                route_reason = (
                    f"; its corroboration is blocked because {findings_route[1]}"
                    if findings_route is not None
                    else ""
                )
                outcome, reason = (
                    "held-for-review",
                    f"the latest reading is {latest['outcome']!r} ({reading_class.value}); "
                    "accepting would establish text that nobody successfully read"
                    f"{route_reason}",
                )
        elif not isinstance(latest_payload.get("text"), str) or not latest_payload["text"].strip():
            outcome, reason = (
                "held-for-review",
                "the latest reading establishes no readable text; silence is not blank proof and "
                "is held until the Recensor can seal one",
            )
        elif continuation_shortfall:
            outcome, reason = (
                "held-for-review",
                "the seal claims a continuation but the Recensor's own reconciliation "
                f"finds proposal regions on only {len(continuation_link['page_ordinals'])} "
                "distinct page(s); accepting would deliver part of an act as the act",
            )
        elif flagged_pages:
            outcome, reason = (
                "held-for-review",
                f"page(s) {flagged_pages} carry ink outside every region currently cut on "
                "them (a residual-ink check against the page image itself, never the "
                "proposal set — goal 2: a missed act is worse than a poorly read one); "
                "accepting this act would leave that ink unaccounted for",
            )
        elif findings_route is not None:
            outcome, reason = findings_route
        elif observation_hold is not None:
            outcome, reason = observation_hold
        elif wants_recovery:
            outcome, reason = (
                "held-for-review",
                f"fallback-recrops use {used_fallback} of their budget of {allowed_fallback}; "
                "a page-level reread is not a substitute and remains unimplemented, so the act "
                "is held rather than re-rolled because recovery recovers coverage and never "
                "quality",
            )
        else:
            outcome = "accepted"
            if (
                page_coverage["unmeasurable_pages"]
                or geometry_coverage.get("ink_measurable") is False
            ):
                reason = (
                    "the reading is accepted; page ink could not be measured or reconciled for "
                    "this act's recorded page evidence"
                )
            else:
                reason = "coverage and geometry reconcile"

        # From the outcome's class, so a review shape added later also reaches the exit code.
        if classify(RECENSOR, outcome) is not OutcomeClass.COMPLETED:
            held += 1

        publish_review(
            context,
            subject_id=act_id,
            outcome=outcome,
            prior=current_review(context, act_id),
            # `latest`, not `readings[0]`: manifest order is a hash.
            inputs=[reading_ref]
            + [context.input_ref(reference["image_path"]) for reference in basis_regions],
            payload={
                "act_key": act_key,
                "reason": reason,
                "coverage": coverage,
                "geometry_coverage": geometry_coverage,
                "testimony_content_coverage": content_coverage,
                "testimony_content_coverage_continuation": continuation_content_coverage,
                "continuation": continuation_link,
                "recoveries_used": used_total,
                "budget_allowed": budget["allowed"],
                "absolute_cap": budget["absolute_cap"],
                "perlectio_ref": reading_ref,
                # Recorded for every act: "checked and clear" is not "never checked".
                "page_coverage": page_coverage,
                "audit_unresolved": audit_unresolved,
                "audit_examination": audit_examination,
                # Tells an empty uncertainty layer from an absent channel.
                "uncertainty_assessment": assessment_record,
                "cross_capture_coverage": cross_coverage,
                **({"blank_evidence": blank_evidence} if blank_evidence is not None else {}),
            },
        )

    # The receipt can refuse, so it is written before the seal; its denominator needs a
    # current stored manifest, written first.
    context.finish()
    write_partition_receipt(context, budget)
    context.seal_boundary()
    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
