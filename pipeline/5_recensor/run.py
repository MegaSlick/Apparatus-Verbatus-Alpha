"""Recensor: establishes that the text is complete. It establishes no text.

It reconciles what the proposal seal expected against what actually happened, and
gives every expected act exactly one outcome. Three of those outcomes end the act
here; two send it onward. Nothing it does touches a reading.

**Recovery is bounded and recorded.** The budget comes from `config/recovery.toml`,
whose absolute cap is Tyrel's "PURE ABSOLUTE, STOP AT 3". When the budget is spent
the act is held for review — it is never re-rolled until it looks better, because
recovery recovers coverage and not quality (GOVERNANCE 11). Every request is an
artifact, so nothing can disappear inside a loop.

**It does not select among witnesses.** Witness outcomes are aggregated into a
coverage record that marks an act under-witnessed and forces the run's aggregate
visibly partial. On an explicit Perlector `no-readable-text` finding, unanimous
region-bound absence may additionally corroborate a blank; it never supplies
characters, and no count of chairs can change a reading.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.act_visibility_geometry import (  # noqa: E402
    MAX_POLYGON_POINTS,
    classify_capture_visibility,
    expected_surface_cells,
)
from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes, is_sha256  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    ATTACHMENT_BASES,
    OutcomeClass,
    classify,
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
)
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.native_witness import (  # noqa: E402
    reported_geometry_overlaps,
    unrouted_observations,
    validate_page_testimonium_payload,
    validate_partition_disagreement,
    validate_reportable_observations,
    verify_native_capture_blob,
)
from common.perlector_audit import validate_chain  # noqa: E402
from common.recensor_receipt import build_recensor_partition_receipt  # noqa: E402
from common.recovery import (  # noqa: E402
    FALLBACK_RECROP,
    RECOVERY_KINDS,
    reconcile_recovery_requests,
    recovery_kind_budget,
)
from common.residual_ink import MINIMUM_INK_PIXELS, page_residual_ink  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    RESIDUAL_ENUMERATION_WITHHELD,
    RESIDUAL_ENUMERATIONS,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    latest_attempt,
    latest_per_chair,
    open_context,
    page_residual_act_key,
    reading_basis_regions,
    recovery_region_count,
    require_current_witness_basis,
    run_stage,
    scenario_for,
    stage_parser,
)


def designator_hold(context, act_id: str) -> tuple[dict, str]:
    """The Designator's hold record for a seal-held act, and its path.

    Refused loudly when absent: a seal entry that says `held` with no record of
    why is a claim with no evidence, and absent evidence never reads cleaner
    than damaged evidence.
    """
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "hold" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "hold", entry["artifact_id"])
            return record, entry["relative_path"]
    raise FatalAccounting(
        f"the seal holds act {act_id} but the Designator published no hold record "
        "saying why; a hold with no evidence cannot be reviewed"
    )


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def audit_state(context, reading: dict, act_id: str) -> bool | None:
    """Verify the two R5b artifacts behind a Perlectio's audit claim.

    The Perlectio's self-hash only proves that somebody sealed its references;
    this consumer proves they name the matching act, exact kinds, and the exact
    finding bytes whose unresolved state governs review routing.

    `not-run` is the one Perlector outcome published without a reading attempt
    (`pipeline/4_perlector/run.py`: a Designator-held act, an explicitly absent
    Perlector chair, or an atomic presentation over the sealed image ceiling).
    It never reaches Pass C, so there is no chain to
    verify and no unresolved span to route on — the act is held on its own
    outcome further down. Demanding a chain here turned the absent-chair hold
    this stage is built to report into a traceback about missing final text,
    which is exactly the trap the `basis_regions` guard below is named for.
    Every attempted outcome (`read`, `truncated`, `no-readable-text`, `failed`)
    publishes the pair and is verified; a forged `not-run` buys nothing, because
    that class is held rather than accepted.

    `None`, not `False`: this act has no audit at all — the same fact a
    Designator-held act's review records. `False` means audited, with its
    sealed re-proof round spent — or nothing to spend it on — not that every
    flag resolved: one reader call answers every flag at once, and
    `change_record` carries at most one span, so `False` is not a per-flag
    resolution claim. Routing is unchanged (`elif audit_unresolved:` treats
    both as falsy); only the record is honest.
    """
    if reading["outcome"] == "not-run":
        return None
    chain = validate_chain(context.tree, reading, act_id)
    return chain["record"]["unresolved"]


def chair_current_attempts(context, act_id: str) -> dict[str, dict]:
    """Each chair's current attempt facts, from ONE latest-attempt collapse.

    Derived, never stored as a pointer. A failed attempt 2 over a successful
    attempt 1 therefore reads as `failed`, with attempt 1 intact as history.
    `latest_per_chair` is the one shared derivation of "current" per chair,
    also used by `pipeline/4_perlector/run.py::testimonia_of` over the same
    upstream artifacts, so the consumers cannot drift on what "current" means.
    `outcome` and `content_health` come out of the same collapse for the same
    reason: two functions that each re-derived "current" independently could
    drift apart, and the staleness check below would then compare two
    different ideas of the current attempt.

    A page witness's act-attachment `content_health` is recorded from this
    exact per-(act, chair) attempt stream (`pipeline/3_attestatores/run.py`'s
    `attempts_by_pair`), not from its page-level Testimonium -- a targeted
    reread appends to this stream whether or not the chair is page-scoped, so
    this is a staleness signal for every chair alike (REOPENED F-O1).

    `read_evidence` joins them for the same reason again: it is a fact about
    the same current attempt, and `blank_corroboration` may not read it from a
    second, independently derived idea of which attempt that is.
    """
    records = artifacts_for(context, ATTESTATORES, "testimonium", act_id)
    return {
        record["payload"]["chair"]: {
            # The identity is required to reject an attachment that combines a
            # current outcome with a superseded Testimonium payload.
            "artifact_id": record["artifact_id"],
            "outcome": record["outcome"],
            "content_health": record["payload"].get("content_health"),
            "read_evidence": _read_evidence(record["payload"]),
        }
        for record in latest_per_chair(records, f"testimonium for {act_id}")
    }


def _read_evidence(payload: dict) -> dict[str, bool]:
    """The two facts an Attestatores attempt leaves behind when a chair looked.

    `pipeline/3_attestatores/HANDOFF.md`: `read` and `genuinely-empty` "mean a
    chair actually read the exact regions and carry a serving receipt", and its
    one write path sets both together for every attempted outcome. So these are
    not a quality signal about the reading -- they are whether the record even
    claims a request was made, and they are read here rather than assumed
    because a completed *absence* is the outcome whose whole content is that
    nothing was there, and therefore the one that can be produced without
    anything having been asked (Sol-S1).
    """
    regions = payload.get("regions")
    provenance = payload.get("provenance")
    receipt = provenance.get("receipt_ref") if isinstance(provenance, dict) else None
    return {
        "regions": isinstance(regions, list) and bool(regions),
        "receipt": receipt is not None,
    }


def chair_outcomes(current_attempts: dict[str, dict]) -> dict[str, str]:
    """The current outcome per chair, projected from ONE passed collapse.

    Takes the `chair_current_attempts` result rather than re-deriving it, so a
    caller that needs outcomes and read evidence together provably reads both
    from the same collapse instead of two identical walks happening to agree.
    """
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
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(facts["source_page_id"], str)
            or not facts["source_page_id"]
            or not isinstance(bounds, dict)
            or set(bounds) != {"x", "y", "w", "h"}
            or any(
                not isinstance(bounds[key], int) or isinstance(bounds[key], bool)
                for key in ("x", "y", "w", "h")
            )
            or bounds["x"] < 0
            or bounds["y"] < 0
            or bounds["w"] <= 0
            or bounds["h"] <= 0
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

    Chosen by strength, not by arrival order. `attached` alone decided this
    before, and `comparable` is a per-page fact -- the producer derives it from
    that page's own alignment status and that page's own retained text
    (`pipeline/3_attestatores/run.py`), so one chair's two rows for one act
    genuinely differ in it. Rows arrive in page order, so a continuation page
    that attached without comparable text sorted ahead of the primary page that
    had both and won on `attached` being equal. The chair was then recorded
    incomparable, dropped out of the witness floor, and the act read
    under-witnessed -- an act held for a human on evidence that existed.

    Ties keep `previous`, so equal-strength rows behave exactly as before.
    """
    return max(previous, current, key=lambda fact: (fact["attached"], fact["comparable"]))


SURVEY_ABSENT = "act-visibility-survey-absent"
REGISTRATION_ABSENT = "cross-capture-registration-absent"
# A view whose capture rendered two pages cannot be surveyed on one grid: the
# instrument divides a single rectangle into cells, and a rectangle spanning two
# page coordinate spaces exists on no page. Recorded like the other absences
# rather than measured, because a verdict over that rectangle would describe a
# surface no camera saw.
SURVEY_SPANS_TWO_PAGES = "act-visibility-survey-spans-two-pages"
# An absent instrument is recorded but does not become a measured shortfall.
INSTRUMENT_ABSENT_CODES = frozenset({SURVEY_ABSENT, REGISTRATION_ABSENT, SURVEY_SPANS_TWO_PAGES})


def occlusion_records_by_page(context) -> dict[str, list[dict]]:
    """Read every sealed Designator occlusion record once per run, by page.

    The same shape as this stage's other page-level inputs
    (`page_coverage_findings`, `geometry_coverage_inputs`, `ink_map_by_page`):
    computed once in `main` and passed down. `_page_occlusion_survey` used to
    rebuild the whole Designator manifest and re-read every occlusion artifact
    for every page of every view of every act, which is the same evidence read
    O(acts x views x pages) times.

    Only the reading moves. Each record's payload is still opened here exactly
    as it was on the first survey, and the polygon and `z_relationship` checks
    stay in the survey below, where they fire for the page actually being
    surveyed and name it. A record with no string `page_id` cannot be filed
    under any page, so it cannot be silently absorbed as "no survey for this
    page" either -- a page that is never surveyed and a page whose survey was
    unreadable must not read alike, so this refuses instead of dropping.
    """
    records: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "occlusion":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "occlusion", entry["artifact_id"])
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

    Artifact absence is not evidence that a survey ran. A ``below-ink`` record
    proves its polygon does not obscure ink; every other accepted relationship
    remains occluding. Validation is local because stages may not import one
    another's implementation modules.

    No Designator stage publishes ``kind="occlusion"`` today
    (`pipeline/2_designator/geometry_layer.py::occlusion_envelope` can build the
    geometry; nothing seals it), so on every current run this returns
    ``surveyed: False`` and the caller records `act-visibility-survey-absent`.
    That is a named absence, not a measurement, and it is deliberately not a
    shortfall: `review_route_from_findings` routes an absent instrument like
    `False` because absence is not a measured gap. See both HANDOFFs.
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


def act_cross_capture_coverage(
    context,
    act_id: str,
    latest_payload: dict,
    *,
    occlusions: dict[str, list[dict]] | None = None,
    proposal_geometry: dict[str, dict] | None = None,
) -> dict | None:
    """Survey the captures sealed in this act's current Perlectio.

    ``None`` means no registered capture presentation exists. Each view uses
    the proposal geometry for every local act named by its sealed autopsia.
    Missing page surveys remain unresolved, and capture-local grids remain
    unresolved when multiple captures lack a sealed registration into one
    coordinate frame.

    ``occlusions`` and ``proposal_geometry`` are the run-level reads `main`
    performs once and passes down: the sealed occlusion records by page, and a
    cache of local-act proposal geometry filled as this survey asks for it.
    A logical act names the same local acts from every one of its views, so the
    same Designator regions were otherwise re-read once per view per act.
    Both default to deriving from ``context`` so a caller with one act and one
    tree — every test of this function — needs no bookkeeping; nothing in the
    Recensor writes Designator artifacts, so the cached reads cannot go stale
    inside a pass.
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
        # One capture may render two pages -- `logical_reading.act_autopsia`
        # groups every touched page by capture, so an act running over a page
        # break becomes one view with two page identifiers. The bounding box
        # below is taken over every page's proposal geometry at once, and the
        # occlusion polygons above are pooled the same way, so for such a view
        # the rectangle handed to the instrument mixes two coordinate spaces:
        # a sticker over the top of page two would be reported as covering
        # cells whose coordinates belong to page one. The verdict is not
        # decorative -- `cross_capture_review_causes` reads it and
        # `review_route_from_findings` turns occluded-everywhere into a stated
        # reason -- so an act could be held on a false measurement, or called
        # fully visible while real occlusion landed in the wrong cells.
        # Continuation acts are ordinary in these registers, so this is the
        # common case and not an edge. Until the instrument can classify each
        # page on its own grid and combine the results, such a view is recorded
        # as unmeasured rather than measured wrongly (GOVERNANCE 2 and 10).
        if surveyed and len(view["page_ids"]) > 1:
            visibility_state = "unresolved"
            visible_cells = []
            occluded_cells = []
            finding_codes = [SURVEY_SPANS_TWO_PAGES]
        elif surveyed:
            x0 = min(bounds["x"] for bounds in bounds_list)
            y0 = min(bounds["y"] for bounds in bounds_list)
            x1 = max(bounds["x"] + bounds["w"] for bounds in bounds_list)
            y1 = max(bounds["y"] + bounds["h"] for bounds in bounds_list)
            survey = classify_capture_visibility(
                bounds={"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                occlusion_polygons=polygons,
            )
            visibility_state = survey["visibility_state"]
            visible_cells = survey["visible_cells"]
            occluded_cells = survey["occluded_cells"]
            finding_codes = []
        else:
            visibility_state = "unresolved"
            visible_cells = []
            occluded_cells = []
            finding_codes = [SURVEY_ABSENT]
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
    """Re-derive R0's attachment record before counting the witness floor.

    The Perlector checks the same writer fact at its read seam.  The Recensor is
    the independent floor-accounting seam, so a sealed attachment boolean is
    not evidence merely because its basis label is spelled correctly: page
    testimony must still overlap this act's original proposal geometry, in
    either direction of claimed/derived drift.
    """
    outcomes = chair_outcomes(current_attempts)
    records = artifacts_for(context, ATTESTATORES, "act-attachment", act_id)
    if not records:
        raise FatalAccounting(f"act {act_id} has no derived act-attachment record")
    # The one shared derivation of "current", exactly as the Perlector's
    # act_attachment_view selects it. The local sort this replaces defaulted a
    # missing ordinal to 0 and took the last record blind, so a duplicate or
    # gapped ordinal chain picked an arbitrary attachment where the strict
    # helper refuses — the same two-predicates-drifting shape as F-O1/F-O3
    # (CodeRabbit chain-end review, critical; host disposition: fixed).
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
        # A malformed health record and an absent one are different facts: only
        # the absent one is honestly "health not recorded", and only the
        # malformed one tells the operator to look at the artifact.
        if health is not None and not isinstance(health, dict):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        truncated = health.get("truncated") if isinstance(health, dict) else None
        # The same malformed-versus-absent rule `attached` and `content_health`
        # get above: any non-boolean flag read as "act-scoped" would skip the
        # alignment-consistency check and halt later on the outcome check with
        # a message blaming the Testimonium, when the real fault is this field.
        page_witness = entry.get("page_witness")
        if not isinstance(page_witness, bool):
            raise FatalAccounting(
                f"act {act_id} attachment entry for chair {chair!r} carries no boolean "
                "page_witness flag; its scope decides which consistency check applies"
            )
        page_ordinal = entry.get("page_ordinal")
        if page_witness:
            if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool):
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
            # `page_ordinal` was type-checked above, before it became half of
            # the duplicate-pair key; re-reading and re-checking it here said
            # the same thing twice with a thinner message.
            proposal_page = proposal_pages.get(page_ordinal)
            if proposal_page is None:
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} attaches outside the sealed "
                    "proposal denominator"
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
            if (
                page_payload.get("chair") != chair
                or page_payload.get("page_ordinal") != page_ordinal
            ):
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} points to a different page Testimonium"
                )
            # The same rule as the native capture below, for the responses a
            # page partition was quantized from: a retained response named only
            # in the payload is one an ordinary artifact read never re-hashes.
            for reference in page_payload.get("raw_response_refs", []):
                if reference not in page_testimonium.get("inputs", []):
                    raise FatalAccounting(
                        f"act {act_id} page witness {chair!r} does not bind a retained raw "
                        "response its own geometry was quantized from as a verified input"
                    )
            native_capture = page_payload.get("native_capture")
            if native_capture is not None:
                if native_capture["raw_response_ref"] not in page_testimonium.get("inputs", []):
                    raise FatalAccounting(
                        f"act {act_id} page witness {chair!r} does not bind its retained raw "
                        "response as a verified input"
                    )
                # `resolve` returns an identity *or* an absence, and `AbsentChair`
                # carries no `witness_adapter`. Read straight through, a page
                # record naming a chair the roster marks absent stopped this
                # stage with an AttributeError -- a traceback where its contract
                # owes a named refusal, and one that says nothing about which
                # act or chair was inconsistent.
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
            # Native page and compatibility act outcomes are independent; legacy
            # page joins instead derive their outcome from the act attempts.
            attachment_outcome = (
                page_testimonium["outcome"] if native_capture is not None else outcomes.get(chair)
            )
            geometrically_attached = attachment_outcome in WITNESS_READING_OUTCOMES and any(
                reported_geometry_overlaps(page_payload.get("observed", []), bounds)
                for bounds in proposal_page["bounds"]
            )
            if entry["attached"] != geometrically_attached:
                raise FatalAccounting(
                    f"act {act_id} page attachment for chair {chair!r} does not derive from "
                    "that witness's reported geometry against the sealed proposal"
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
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} has no computed alignment fact"
                )
            if entry["attached"] and attachment_basis != "geometric-overlap":
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} is attached without geometric evidence"
                )
            if not entry["attached"] and attachment_basis != "unattached":
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} names a basis for an unattached record"
                )
            # The documented closed shapes (pipeline/3_attestatores/HANDOFF.md),
            # enforced where the floor is counted, not only at the Perlector: an
            # attached record missing its geometry -- or its anchor_basis, which
            # the blank gate below reads -- must not count as valid coverage,
            # and a reason-free unaligned record leaves an operator with no
            # statement of why comparison failed.
            if alignment["status"] == "aligned":
                if (
                    set(alignment)
                    != {
                        "status",
                        "anchor_basis",
                        "anchor_chair",
                        "anchor_span",
                        "witness_span",
                        "line_geometry",
                        "loss",
                        "offset_maps",
                    }
                    or not isinstance(alignment["anchor_basis"], str)
                    or alignment["anchor_basis"]
                    not in {
                        "act-anchor",
                        "no-page-anchor",
                        "act-line-not-located",
                    }
                    or (
                        alignment["anchor_basis"] == "act-anchor"
                        and not isinstance(alignment.get("anchor_chair"), str)
                    )
                    or (
                        alignment["anchor_basis"] != "act-anchor"
                        and alignment.get("anchor_chair") is not None
                    )
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
            # `attached` proves geometry, not text. The floor also requires an
            # aligned slice from the referenced page record.
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
        else:
            # Act-scoped floor facts come from the current referenced
            # Testimonium; trusting both stored booleans would allow a producer
            # to forge them false and silently remove a completed chair.
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
            if entry["comparable"] != (
                derived_attached and isinstance(act_payload.get("payload"), str)
            ):
                raise FatalAccounting(
                    f"act {act_id} attachment for chair {chair!r} claims a comparability its "
                    "own retained derived testimony does not support. The witness floor could "
                    "count a structured or absent report as act text. Rebuild comparability "
                    "from the current referenced Testimonium."
                )
        fact = {
            "attached": entry["attached"],
            "comparable": entry["comparable"],
            "attachment_basis": attachment_basis,
            "truncated": truncated,
            "health_unrecorded": truncated is None,
            "page_witness": page_witness,
            "content_health": health,
            # None for act-scoped chairs and unaligned page witnesses; the
            # producer's disclosure of what an aligned page witness aligned
            # against ("act-anchor" | "no-page-anchor" | "act-line-not-located").
            # `blank_corroboration`
            # is the consumer that must see it.
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
            # A page witness has one attachment for every contributing page.
            # Its act-level floor remains the one act attempt, so a continuation
            # whose page has no anchor cannot erase the primary page's valid
            # attachment; all page references remain separately checked by the
            # content denominator below. Merging whole rows keeps one page's
            # contribution carrying both predicates together: the surviving row
            # is the strongest single page's, never a pair of booleans OR-ed
            # across pages, which could manufacture a combination no one page
            # supplied.
            previous = facts[chair]
            merged = dict(_merge_page_attachment_fact(previous, fact))
            # Only rows of this same act attempt are merged -- the health
            # equality just above is what holds that -- so filling one page's
            # missing basis from a sibling page never borrows another attempt's
            # evidence.
            #
            # `act-line-not-located` is sticky across aligned page rows: a
            # terminal blank cannot discard the page whose geometry failed.
            # Without the stickiness `blank_corroboration` would read the merged
            # row as geometry-checked and let a failed alignment corroborate a
            # blank it never located.
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

    ARCHITECTURE and spec 09 both name blank confirmation as a candidate
    completeness check ("a zero-output unit is diagnosed, then either sealed
    confirmed-blank with evidence or held unresolved-with-evidence"). A blank
    verdict may not rest on fewer than several genuinely INDEPENDENT completed
    reads, and never on a reader's own second opinion — the old pipeline paid to
    learn that (window pass, 2026-08-05).

    This is **unanimity about an absence, never a selection among presences**:
    the Perlector's own direct examination of the ink (autopsia, not testimony)
    already produced `no-readable-text`, and this asks only whether the witnesses
    corroborate or contradict that finding. A single chair that actually read
    text is exactly the disagreement GOALS 1 says must never be silently
    resolved — it holds the act for a human, and never outvotes the dissenter.

    A recovery region is witness-uncovered by contract: the inherited
    testimonia remain bound to the original proposal regions. They therefore
    cannot corroborate absence in an expanded region they never saw.

    Requires the configured witness floor to have been met by chairs that
    actually completed a read (not merely configured), and no chair still
    unresolved — a floor met only by `failed`/`dead` chairs, or a run that
    has not yet heard from every configured chair, corroborates nothing.

    The floor is checked against `completed` below, never against
    `coverage["under_witnessed"]`: that flag is `ATTESTATORES`'s own
    COMPLETED class, which also counts an approval-bound `excluded` chair
    (`common/contracts/outcomes.py`) — a chair Tyrel excluded from witnessing
    at all, not one that read the ink and found nothing. `under_witnessed`
    can therefore be `False` while the actual reading evidence is one chair
    short of the floor; trusting it here would let an excluded chair stand
    in for a witness that never looked.

    `attachments` is `act_attachment_facts`'s per-chair record. A page witness
    whose trivial attach discloses `anchor_basis: "act-line-not-located"`
    still counts toward the floor (the chair did complete, and that is
    disclosed rather than hidden), but it may not corroborate a TERMINAL
    blank: the page's Chandra anchor exists yet locates no line for this act,
    so the geometry does not reconcile, and confirmed-blank is a proved
    absence -- the act holds for a human instead (GOVERNANCE 2/9; GOALS 1:
    the unproved direction costs a review, never an act). `no-page-anchor` is
    the different fact of a page with no anchor at all -- an ink-free or
    fallback page has nothing for Chandra to anchor, and refusing blank there
    would make the intended blank-page path unreachable.

    `read_evidence` is `chair_current_attempts`'s per-chair record of the two
    facts an actual request leaves behind: the regions the chair was shown, and
    the serving receipt for the attempt. The sentence this function's caller
    publishes is a claim about what happened -- "every witness that actually read
    this act ... independently reports the same absence" -- and until Sol-S1 that
    claim was made without anyone checking it. The Sol-S1 repair itself is
    upstream (the minting branch is deleted); the fabricated records carried
    regions AND receipts, so this gate is defence in depth against a resealed
    or foreign artifact, not a second catch for that finding. A completed-class
    outcome missing either fact is a record this pipeline's own writer cannot
    produce, so it is `FatalAccounting` rather than a quiet `None`: a hold
    would say the evidence was weak, and what is actually true is that the
    evidence is not this stage's to interpret. A presence check -- the strong
    per-byte counterpart runs at the Perlector over the same artifacts.
    """
    completed = sorted(
        chair for chair, outcome in outcomes.items() if outcome in WITNESS_READING_OUTCOMES
    )
    # Named per chair AND per missing fact: "this record shows no request was
    # made" sends an operator to the producer, and which half is absent says
    # which producer branch to look at. One message for two faults would not.
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
    # **Below the validation, deliberately.** These two are ordinary "this act
    # cannot be confirmed blank" facts and they return a quiet `None`; the check
    # above is a claim that the run tree holds a record this pipeline's own writer
    # could not have produced. Ordering the short-circuit first made that alarm
    # conditional on the act being otherwise eligible, so exactly the trees most
    # likely to be malformed — a recovery region, a run still missing a chair —
    # were the ones where a forged completed record travelled unexamined. A
    # writer-impossible record is fatal on every path or it is not fatal at all.
    if witness_uncovered or coverage["unresolved_chairs"]:
        return None
    if (
        len(completed) < coverage["floor"]
        or not completed
        or any(outcomes[chair] != "genuinely-empty" for chair in completed)
        or any(
            attachments.get(chair, {}).get("anchor_basis") == "act-line-not-located"
            # Defence in depth beside `act_attachment_facts`' own refusal: a
            # page witness whose fact somehow carries no basis at all is
            # geometry nobody checked, and a terminal blank may not rest on it.
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

    Deliberately callable before the Recensor publishes anything: an ambiguity
    discovered while reviewing the second act would otherwise leave a review for
    the first one already on disk. That fragment is not a completed stage, but it
    is an easy thing for a later retry to mistake for history, so the whole
    witness denominator is validated before any of it is published.
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
    # R4's attachment is an independent computed fact for a PAGE witness.  It
    # must not be forced back into the act attempt outcome there: a page
    # witness can have read its page while the bounded text-to-anchor
    # calculation honestly remains unaligned, and `act_attachment_facts`
    # already checks that fact for internal consistency against its own
    # computed alignment.
    unaccounted = sorted(set(outcomes) ^ set(attachments))
    if unaccounted:
        raise FatalAccounting(
            f"act {act_id}'s derived act-attachment and its current Testimonia disagree on "
            f"chair(s) {unaccounted}; an absent fact would silently read as unattached, and "
            "an extra one would attach a chair that never testified for this act"
        )
    # An ACT-SCOPED chair carries no independent computed fact: its `attached`
    # is a restatement of that chair's own current Testimonium outcome, not a
    # second measurement of anything. `reread_pass` (`pipeline/3_attestatores/
    # run.py`) appends a new act-scoped attempt without writing a new
    # attachment record, so the derived attachment is a THIRD consumer of the
    # same artifacts as `chair_outcomes`/`testimonia_of` and can drift from
    # both exactly as it did before R4 (F-O1): a targeted reread would
    # otherwise count the witness floor from an attempt the reread already
    # superseded. Restored on R4's audit (REOPENED F-O1) after removing it
    # here left this hole open for every act-scoped chair; page witnesses are
    # exempted because their own alignment-consistency check above is the
    # genuinely independent fact this check would otherwise wrongly demand
    # agreement from.
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
    # NOT scoped to act-scoped chairs, unlike the outcome check just above: a
    # page witness's attachment `content_health` is recorded from this exact
    # per-(act, chair) attempt stream, not from page-level text, so it is a
    # valid staleness signal for every chair (`chair_current_attempts`'s
    # docstring; mirrors `pipeline/4_perlector/run.py::act_attachment_view`'s
    # identical, symmetric check -- REOPENED F-O1).
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

    A recovery request is not evidence that its recrop happened, and a recovery
    crop is not evidence that the Perlector read it.  The three append-only
    histories must therefore agree before another Recensor review can be written:
    exactly one recovery-requested review per request, at most one recrop per
    request, and a later Perlectio for every recrop.  No branch here establishes
    text or selects among readings; disagreement is fatal accounting.

    The request history itself — ordinals, kinds, and the counters each request
    recorded — is reconciled by `common/recovery.py::reconcile_recovery_requests`,
    the one implementation the Designator and orchestrator boundary also uses.
    What this function adds is the binding between that history and the reviews,
    recrops and rereads that answered it.
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
        ordinal = payload.get("attempt_ordinal")
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
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or review.get("attempt_id") != attempt_id(act_id, "recense", ordinal)
        ):
            raise FatalAccounting(
                f"recovery-requested review of {act_id} has no exact matching recovery request"
            )
        request = requests_by_ordinal.get(ordinal)
        if request is None or request["artifact_id"] != matching_request:
            raise FatalAccounting(
                f"recovery-requested review of {act_id} disagrees with its request ordinal"
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
    # Validate the whole origin vocabulary through the one shared reader, so this
    # stage and the two downstream ones cannot disagree about what counts as a
    # recovery crop. The count is discarded here; the per-region binding below is
    # what this function additionally needs and the shared reader does not do.
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

    ARCHITECTURE and spec 09 agree the Recensor's link is the authoritative
    continuation relation; the Designator's proposal seal carries
    `has_continuation` as its own PROPOSAL, not a settled fact this stage may
    inherit unexamined ("the Designator proposes continuations"). This derives
    the answer directly from the original proposal regions actually cut — never
    from the seal's flag — so the seal's claim can be checked against it rather
    than trusted in its place. A genuine continuation cuts two proposal regions
    on two distinct source pages; counting bare regions without checking the
    pages would call two regions on the SAME page a continuation, which they
    are not.
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

    Returns whether the act's reading covers only part of a claimed
    continuation — never established as a whole act while that is true. Raises
    when the seal instead denies a continuation its own evidence already
    proves: silently agreeing with a seal that under-claims against the
    evidence would let the Designator's proposal override the Recensor's own
    authoritative continuation fact, which is exactly what stage ownership of
    this relation (ARCHITECTURE, spec 09) exists to prevent.
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

    Proposal and recovery together, from every act that touches a page — the
    residual-ink check (`residual_ink.py`) asks about the PAGE's own pixels,
    never any one act's denominator, so a region cut for a different act on the
    same page still counts as coverage here. A page nobody cut a region on at
    all has no entry: there is no evidence to read a region's absence against
    yet. That gap is named, not papered over, in `HANDOFF.md`.
    """
    by_page: dict[int, list[dict]] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        payload = record.get("payload")
        transform = payload.get("transform") if isinstance(payload, dict) else None
        if not isinstance(transform, dict):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has no object transform"
            )
        ordinal = transform.get("source_page_ordinal")
        bounds = transform.get("bounds")
        # Every one of the four numbers, not merely that `bounds` is an object:
        # `residual_ink` indexes all four, so a rectangle that is a dict and
        # nothing more reaches the pixel arithmetic and leaves by traceback
        # instead of by the named refusal this check exists to give.
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(bounds, dict)
            or any(
                not isinstance(bounds.get(side), int) or isinstance(bounds.get(side), bool)
                for side in ("x", "y", "w", "h")
            )
        ):
            raise FatalAccounting(
                f"Designator region {record.get('artifact_id')} has an invalid transform"
            )
        by_page.setdefault(ordinal, []).append(bounds)
    return by_page


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted source-manifest row for each ordinal, by ordinal.

    The same reconciliation `pipeline/2_designator/run.py::_source_rows` and
    `pipeline/7_armarium/run.py::page_census` each carry locally rather than
    share — every stage that touches sealed Exemplar pixels rebuilds its own
    view of the submitted denominator before trusting a page's own claim
    about them.
    """
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise FatalAccounting("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise FatalAccounting("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                "run.json carries a source-manifest row without an integer ordinal"
            )
        if ordinal in sources:
            raise FatalAccounting(f"run.json repeats source ordinal {ordinal}")
        sources[ordinal] = row
    return sources


def sealed_page_images(context) -> dict[int, dict]:
    """Every sealed Exemplar page's own artifact, by ordinal.

    Verified against the run's own submitted source manifest before this
    stage trusts its pixels — the residual-ink check reads raw bytes off
    `payload["image_path"]`, a self-declared field `validate_envelope` never
    relates to a page's digest-checked `inputs`. Every other stage that reads
    sealed page pixels (`pipeline/2_designator/run.py`,
    `pipeline/7_armarium/run.py`) calls this exact check first; reading raw
    bytes off an unverified path here would be the one place in the pipeline
    that skips it.
    """
    pages: dict[int, dict] = {}
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        if record["outcome"] != "sealed":
            continue
        ordinal = record["payload"].get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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

    The physical-act partition, the cross-capture autopsia and this stage's own
    visibility survey all name a capture by the *sealed Exemplar page's*
    `source_sha256` (`pipeline/4_perlector/logical_reading.py::
    _source_sha256_of_page`), which `verify_sealed_page_pixels` has already
    proved is a lowercase SHA-256 over bytes that verify.

    `run.json`'s source-manifest row is a different fact and is not that
    identity. It records what the *submission* declared: optional at real
    ingress (`pipeline/1_exemplar/door.py`'s `SourceEntry.declared_sha256` is
    `str | None`, and `RunTree.create` validates only ordinals), and for a page
    rendered out of a container it names the container, so several ordinals
    share one value. Asking the capture-specific gate about that row therefore
    either named the wrong capture or handed it `None`, and `None` came back as
    "lacks logical-act/capture identity" — a refusal naming the wrong thing.

    Refuses here rather than at the gate: a digest that cannot be stated is an
    accounting failure of this stage's own evidence, not of the caller's
    request.
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
    """Residual-ink findings for every sealed page with at least one region cut
    on it (ARCHITECTURE's candidate list: "a residual-ink check whose input is
    the page image itself, never the proposal set"), computed once per run and
    reused by every act that reaches one of these pages.

    Deterministic core, cheapest instrument first: pure geometry over the
    page's own pixels, entirely independent of any witness or reading. A page
    with zero regions cut on it at all is not checked here — see
    `regions_by_source_page`.

    ``sealed_pages`` is the same verified page map `main` already needs for the
    capture digests; passing it keeps one pixel-verification pass per run
    rather than one per consumer. Omitting it derives the map here exactly as
    before, so every direct caller and test is unchanged.
    """
    regions = regions_by_source_page(context)
    if not regions:
        return {}
    pages = sealed_page_images(context) if sealed_pages is None else sealed_pages
    findings: dict[int, dict] = {}
    for ordinal, bounds in regions.items():
        page = pages.get(ordinal)
        if page is None:
            raise FatalAccounting(
                f"a Designator region names source page {ordinal}, which the Exemplar "
                "did not seal; a crop of unsealed pixels is invariant #10's imbalance"
            )
        # The bytes measured, digested — not the separate earlier read
        # `sealed_page_images` verified. Verifying one read and measuring
        # another records a finding derived from pixels nobody checked, which is
        # a metric that was not measured passing as one (GOVERNANCE 10).
        image_bytes = context.tree.read_bytes(page["payload"]["image_path"])
        if digest_bytes(image_bytes) != page["payload"]["source_sha256"]:
            raise FatalAccounting(
                f"the sealed Exemplar page {ordinal} the residual-ink check read does not "
                "match the pixel digest its own page record verified"
            )
        findings[ordinal] = page_residual_ink(image_bytes, bounds)
    return findings


def page_coverage_for(act_regions: list[dict], findings: dict[int, dict]) -> dict[str, list[int]]:
    """The residual-ink fact every review records: which source pages this act's
    own current regions were cut from, and which of those are flagged.

    Every page the act's proposal or recovery regions touch, not only its primary
    `page_ordinal` — a continuation's far-side page is examined exactly as its
    near side is, and a successful recovery crop that reaches previously-missed
    ink clears the page's finding on the very next pass.

    `checked_pages` is narrowed to pages `findings` actually has an entry for,
    not merely every ordinal an act's regions name: in `main()`'s real call the
    two sets always coincide (`page_coverage_findings` derives its keys from
    the same region set this function reads), but the field's own purpose --
    "a consumer cannot tell 'checked and clear' from 'never checked'" -- only
    holds if a page absent from `findings` is never reported as checked.

    One derivation for all four review shapes, including a Designator-held act
    whose own near-side region really was cut: a shape that records this fact
    empty rather than deriving it drops a flagged page's only evidence whenever
    that act is the only one touching the page.
    """
    ordinals = sorted(
        {
            region["payload"]["transform"]["source_page_ordinal"]
            for region in act_regions
            if isinstance(region.get("payload"), dict)
            and isinstance(region["payload"].get("transform"), dict)
        }
    )
    checked = [ordinal for ordinal in ordinals if ordinal in findings]
    return {
        "checked_pages": checked,
        "flagged_pages": [ordinal for ordinal in checked if findings[ordinal].get("flagged")],
    }


def ink_map_by_page(context) -> dict[int, dict]:
    """Read the sealed page-space evidence without re-decoding page pixels."""
    maps: dict[int, dict] = {}
    for entry in context.tree.build_manifest(INK_MAP)["artifacts"]:
        if entry["kind"] != "ink-map":
            continue
        record = context.tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
        payload = _payload(record, "ink-map")
        ordinal = payload.get("page_ordinal")
        evidence = payload.get("edge_findings")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                "ink-map has a record without an integer page ordinal. The Recensor cannot bind "
                "its ink evidence to a sealed page. Restore the sealed Ink Map inventory or "
                "restart the run before rerunning the Recensor."
            )
        if not isinstance(evidence, dict) or evidence.get("schema") != "ink-runs.v1":
            raise FatalAccounting(
                f"ink-map page {ordinal} has no readable ink-runs.v1 page-space evidence. The "
                "Recensor cannot confirm witness pointers from a bare page outcome. Restore the "
                "sealed Ink Map artifact or restart the run before rerunning the Recensor."
            )
        if ordinal in maps:
            raise FatalAccounting(
                f"ink-map repeats page ordinal {ordinal}. The Recensor has no rule for choosing "
                "which retained page-space evidence confirms witness pointers. Restore the sealed "
                "Ink Map inventory or restart the run before rerunning the Recensor."
            )
        maps[ordinal] = evidence
    return maps


def _ink_outside_cuts_in_box(evidence: dict, box: dict, covered: list[dict]) -> int:
    """Ink of Unit 9's retained runs inside ``box`` and outside every cut region.

    Witness observations remain classified against the proposal set that
    existed when they were recorded, so an observation may overlap a recovery
    crop cut later. Subtracting every current proposal and recovery crop keeps
    already-covered ink from funding another bounded recovery.
    """
    width, height, rows = evidence.get("width"), evidence.get("height"), evidence.get("rows")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
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
    # Clamp the far edge back to the near edge as well as to the page. A box
    # wholly above the page otherwise produces (for example) ``rows[0:-2]``;
    # Python interprets that as almost the whole page, manufacturing ink inside
    # geometry that intersects no page pixel at all.
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
            if (
                not isinstance(run, list)
                or len(run) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in run)
            ):
                raise FatalAccounting(
                    "ink-map edge findings contain a malformed run. Its ink count cannot be "
                    "measured reliably, so it cannot authorize recovery. Restore the sealed "
                    "Ink Map artifact or restart the run before rerunning the Recensor."
                )
            start, length = run
            # Ordered and disjoint, as `ink_runs` writes them and as
            # `edge_ink_from_runs` already requires of the same evidence. Two
            # runs that overlap would be counted twice here and could confirm
            # a pointer over ink that is not there.
            if start < previous_end or length <= 0 or start + length > width:
                raise FatalAccounting(
                    "ink-map edge findings have unordered or out-of-bounds runs. Counting them "
                    "could invent ink and authorize unsupported recovery. Restore the sealed "
                    "Ink Map artifact or restart the run before rerunning the Recensor."
                )
            previous_end = start + length
            ink_spans.append((max(x0, start), min(x1, start + length)))
        cut_spans = sorted(
            (max(x0, bounds["x"]), min(x1, bounds["x"] + bounds["w"]))
            for bounds in covered
            if bounds["y"] <= y0 + offset < bounds["y"] + bounds["h"]
        )
        cuts: list[tuple[int, int]] = []
        for cut_start, cut_end in cut_spans:
            if cut_start >= cut_end:
                continue
            # Coverage is a union: overlapping act crops must not subtract
            # their shared pixels twice and understate the unclaimed ink.
            if cuts and cut_start <= cuts[-1][1]:
                cuts[-1] = (cuts[-1][0], max(cut_end, cuts[-1][1]))
            else:
                cuts.append((cut_start, cut_end))
        for start, end in ink_spans:
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
            total += max(0, end - cursor)
    return total


def unclaimed_ink_observations(
    maps: dict[int, dict],
    unclaimed_observations: list,
    page_ordinal: int,
    cut_regions: dict[int, list[dict]],
) -> list[dict]:
    """Which of this page's retained unclaimed observations point at real ink.

    A witness box is only a pointer. Recovery requires independently measured
    ink outside every current cut; agreement, overlap scores, chair identity,
    and other witness-derived quantities cannot authorize it. ``cut_regions``
    is required because an omitted mask would count already-covered ink as a
    reason to cut it again.

    A retained observation with no map row is a fatal accounting gap, not an
    empty result: absence of the independent evidence cannot honestly be read
    as a measurement of zero ink. With no observations there is no pointer to
    confirm, so an absent row remains inert here; the Armarium later reconciles
    the complete page denominator independently.

    A retained observation whose own ``bounds`` is missing or not the closed
    ``{x, y, w, h}`` shape is the same kind of gap, not a pointer that happens
    to point at nothing: silently skipping it would let a malformed witness
    record disappear behind an empty result instead of the fatal refusal every
    other malformed-evidence path in this module raises.
    """
    evidence = maps.get(page_ordinal)
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
        # Each of x, y, w and h is required, and required to be a real integer.
        # `isinstance(bounds, dict)` alone let a partial rectangle through to
        # `_ink_outside_cuts_in_box`, which indexes `box["x"]` and ended the
        # stage with a bare KeyError -- an unnamed crash in place of the named
        # refusal this gate exists to give, for the one malformed shape that
        # actually reaches the arithmetic.
        if not isinstance(bounds, dict) or any(
            key not in bounds or not isinstance(bounds[key], int) or isinstance(bounds[key], bool)
            for key in ("x", "y", "w", "h")
        ):
            raise FatalAccounting(
                f"page {page_ordinal} has a retained unclaimed witness observation with no "
                "{x, y, w, h} bounds. Skipping it would read a malformed pointer as one that "
                "never pointed at ink, the same silent loss this gate exists to refuse. Restore "
                "the page's sealed Testimonium evidence or restart the run before rerunning the "
                "Recensor."
            )
        ink_pixels = _ink_outside_cuts_in_box(evidence, bounds, covered)
        if ink_pixels >= MINIMUM_INK_PIXELS:
            requests.append({"page_ordinal": page_ordinal, "outside_ink_pixels": ink_pixels})
    return requests


# Request origin is recorded data because the page-wide bound must not depend
# on parsing mutable human-readable reason text.
COVERAGE_OBSERVATION_ORIGIN = "coverage-observation"
DECLARED_CROP_ORIGIN = "declared-incomplete-crop"
RECOVERY_ORIGINS = (COVERAGE_OBSERVATION_ORIGIN, DECLARED_CROP_ORIGIN)


def observation_funded_pages(context, acts: list[dict]) -> set[int]:
    """Pages whose one observation-funded recovery has already been spent.

    An unclaimed observation is page-scoped, while the recovery request it can
    fund is act-scoped. Counting from the retained tree prevents each act on a
    page, or each later Recensor pass, from spending a separate grant for the
    same page evidence. The first eligible act follows the sealed proposal
    order and budget state; no witness-derived ranking enters that choice.

    Spending the grant does not claim the ink is covered. The observation stays
    retained, and the Designator expands the act's declared rectangle rather
    than treating the witness box as authoritative geometry.
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

    A scenario declaration and an ink-confirmed observation can coincide.  The
    declaration is the independent structural route and takes precedence: the
    page's one observation-funded grant must not be consumed merely because
    witness geometry happened to be present beside a request the declaration
    already caused.  With neither cause, publication is an accounting defect.
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
    outside_ink_requests: list, page_ordinal: int, funded_pages: set[int]
) -> tuple[str, str] | None:
    """Keep a still-confirmed pointer visible when no request can be published."""
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


def geometry_coverage_inputs(context) -> dict[int, dict]:
    """Consume, and independently reconcile, R2's conservation denominator.

    A conservation record is not a conclusion this stage may copy into its own
    review.  Every sealed page represented by a non-held act must have one, and
    its residual components must have become exactly the held residual acts in
    the proposal seal.  Reading both sides here makes a broken R2 invariant-8
    partition or a missing sealed-page denominator a refusal, rather than a
    reassuring Recensor record.  An all-held page is the distinct door-refusal
    shape: it never reached sealing, so absence remains absence for its reviews.

    **A page that withheld its enumeration is a third shape, and it fails for
    its own name.**  The Designator holds a page as one `page-residual` review
    item when its reconciliation counted more unclaimed components than the
    sealed grouping policy allows one page to enumerate, and then omits
    `residual_components` rather than emptying it -- an empty list is the claim
    "no unclaimed ink", which is the opposite of what happened.  Without a
    branch of its own such a record arrived at the malformed-facts refusal and
    was reported as a broken artifact, which is a true statement about the
    shape and a false one about the run: nothing was malformed, a page was
    held under a policy, and the operator was told the wrong thing.  Every
    condition below is recomputed from the record and the seal, never read off
    the Designator's word for it: the count must exceed the bound it names, the
    page must carry exactly one page-residual act, and it must carry none of
    the per-component ones -- minting both would account for the same unlisted
    ink twice.
    """
    acts = expected_acts(context)
    residual_keys = {act["act_key"] for act in acts if act["act_key"].startswith("residual:")}
    page_residual_keys = [
        act["act_key"] for act in acts if act["act_key"].startswith("page-residual:")
    ]
    findings: dict[int, dict] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "conservation":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "conservation", entry["artifact_id"])
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
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(measurable, bool)
            or ordinal in findings
        ):
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
        if page_residual_keys.count(page_residual_act_key(ordinal)) > 0:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} enumerated its residual components "
                "and is also held as one page-residual review item; a page is accounted for by "
                "one held act per residual or by the single item that replaced them, never by "
                "both"
            )
        if not isinstance(components, list):
            raise FatalAccounting("Designator conservation has malformed or duplicate page facts")
        declared_count = payload.get("residual_component_count")
        if (
            not isinstance(declared_count, int)
            or isinstance(declared_count, bool)
            or declared_count != len(components)
        ):
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
            if (
                not isinstance(component, dict)
                or not isinstance(bounds, dict)
                or set(bounds) != {"x", "y", "w", "h"}
                or any(
                    not isinstance(bounds[side], int) or isinstance(bounds[side], bool)
                    for side in ("x", "y", "w", "h")
                )
                or not isinstance(pixel_count, int)
                or isinstance(pixel_count, bool)
                or pixel_count < 0
            ):
                raise FatalAccounting(
                    f"Designator conservation page {ordinal} residual component {index} "
                    "is malformed"
                )
        if measurable:
            if any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in pixel_counts.values()
            ):
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


def _withheld_page_conservation(
    ordinal: int,
    payload: dict,
    measurable: bool,
    pixel_counts: dict,
    residual_keys: set,
    page_residual_keys: list,
) -> dict:
    """One page held as a single review item in place of its residual components.

    This stage is the second consumer of that decision and reconciles it the
    same way it reconciles an enumerated page: against the seal, never against
    the producer's assurance.  What it cannot do is add up the components, so
    the two checks that survive are the ones that still can be made -- the ink
    accounting itself (`claimed + residual == total`, still exact and still
    published) and the partition (exactly one page-residual act for this page,
    and none of the per-component ones).  The per-component pixel sum is the
    one check genuinely lost here, and it is lost because the list it summed is
    the thing deliberately not carried; saying so is better than quietly
    dropping it.

    The finding this returns is deliberately not the shape an enumerated page
    returns.  `residual_act_count` is 0 and true -- no `residual:` act was
    minted -- and on its own it would read to a reviewer as a page whose
    unclaimed components vanished, so the count, the bound, the enumeration and
    the one page-residual act are all named beside it.  A record that restated a
    withheld page in an enumerated page's shape would be the same class of
    untruth as `NO_PAGE_CONSERVATION` defaulting to `ink_measurable: False`.
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
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (count, bound)
    ):
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
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in pixel_counts.values()
    ):
        raise FatalAccounting(
            f"Designator conservation page {ordinal} has malformed measured pixel counts; "
            "total, claimed, and residual must be non-negative integers"
        )
    if (
        pixel_counts["claimed_pixel_count"] + pixel_counts["residual_pixel_count"]
        != pixel_counts["total_ink_pixel_count"]
    ):
        raise FatalAccounting(
            f"Designator conservation page {ordinal} pixel accounting does not reconcile: "
            "claimed_pixel_count + residual_pixel_count does not equal total_ink_pixel_count"
        )
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

    `latest_attempt` is the one shared derivation of "current", exactly as
    `act_attachment_facts` above and `pipeline/4_perlector/run.py`'s
    `act_attachment_view` derive it. Keeping the last record the manifest
    happens to list is the defect both of those already record: manifest order
    is a hash, so a second Attestatores pass would hand this check whichever
    attachment sorted last rather than the current one, and a third consumer
    deriving "current" its own way is the F-O1/F-O3 drift shape itself.
    """
    records: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
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
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        ordinal, chair = payload.get("page_ordinal"), payload.get("chair")
        # A boolean ordinal hashes as its integer counterpart, so accepting one
        # here would merge page `true` into page 1 before currency is derived.
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not isinstance(chair, str):
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
    intervals = []
    for start, end, _ in spans:
        if start < 0 or end < start or end > text_length:
            raise FatalAccounting("act attachment span lies outside its page Testimonium")
        if start != end:
            intervals.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


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
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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


def testimony_content_findings(context) -> dict[int, dict]:
    """Compare each page witness's text to its own aligned act attachments.

    This is deliberately testimony-to-testimony: no Perlectio text participates
    until R5b's Pass-C output exists.  The retained alignment spans are the loss
    map; a non-whitespace page character outside their ordered union is a visible
    coverage shortfall, never a verdict about which witness is right.
    """
    attachments = current_act_attachments(context)
    page_testimonia = current_page_testimonia(context)
    # Read and validated once, not once per page witness: `expected_acts` re-reads
    # the proposal seal and re-verifies its self-hash on every call, and this stage
    # never writes to the Designator's seal while it runs.
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
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise FatalAccounting(
                    f"Designator region of {act['act_id']} has no integer page ordinal; its "
                    "page testimony cannot be grouped; restore the region's sealed transform"
                )
            page_acts = acts_by_page.setdefault(ordinal, [])
            if act not in page_acts:
                page_acts.append(act)
            bounds = transform.get("bounds")
            # The same rectangle `_proposal_geometry_by_page` requires of these
            # same sealed proposals: four integer sides, on the page, with
            # positive area. Two distinct failures follow from accepting less.
            # A rectangle that is a dict and nothing more reaches
            # `unrouted_observations`, which indexes all four sides by name, and
            # leaves the stage that decides recovery as a bare `KeyError` naming
            # neither page nor act. Worse, a *degenerate* rectangle -- zero or
            # negative width, or an off-page origin -- indexes cleanly and
            # overlaps nothing, so `_overlaps` reports that no proposal accounts
            # for ink a proposal does in fact cover, and the witness's
            # observation is published as an unrouted-observation finding. That
            # is manufactured coverage evidence driving bounded recovery
            # (GOVERNANCE 10, 11), which is why the range checks belong here and
            # not only in the sibling reader.
            if (
                not isinstance(bounds, dict)
                or set(bounds) != {"x", "y", "w", "h"}
                or any(
                    not isinstance(bounds[side], int) or isinstance(bounds[side], bool)
                    for side in ("x", "y", "w", "h")
                )
                or bounds["x"] < 0
                or bounds["y"] < 0
                or bounds["w"] <= 0
                or bounds["h"] <= 0
            ):
                raise FatalAccounting(
                    f"Designator proposal region of {act['act_id']} has no page-pixel bounds"
                )
            proposal_regions_by_page.setdefault(ordinal, []).append(region)
            found_page = True
        # Unit callers may provide the proposal seal without materialized
        # regions; its primary page remains a known one-page denominator.
        if not found_page:
            acts_by_page.setdefault(act["page_ordinal"], []).append(act)
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
    # Reference every page row first, so a missing record names the act whose
    # evidence was lost. The whole-page role reconciliation then catches the
    # converse orphan (a page record no act owns) before any finding is built.
    reconcile_page_roles(context, attachments, page_testimonia)
    findings: dict[int, dict] = {}
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        # `current_page_testimonia` types only the page ordinal and the chair,
        # because those two become dict keys. The observed rows below are still
        # untrusted evidence read from disk, and `unrouted_observations` indexes
        # each one by name: a row that is not a closed observation would leave
        # this stage as a raw KeyError rather than a named refusal, from the one
        # stage that decides whether coverage recovery runs.
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
            # Coverage is computed from what the witness saw and the current
            # sealed proposal denominator. The optional retained partition is
            # audit evidence, not an input whose omission or older denominator
            # may suppress a present finding.
            unclaimed = unrouted_observations([record], proposal_regions_by_page.get(ordinal, []))
        else:
            unclaimed = []
        if disagreement is not None or unclaimed:
            finding = findings.setdefault(
                ordinal,
                {"by_chair": {}, "shortfall": False},
            )
            finding.setdefault("unclaimed_observations", []).extend(copy.deepcopy(unclaimed))
            # An observation outside every proposal is a retained coverage
            # finding, not evidence that the page's *reported text* fell
            # outside an attached span.  It independently asks the Recensor
            # for bounded recovery below; turning it into this text shortfall
            # would keep every act on the page held after that route has run,
            # even though the finding never assigned the observation to one of
            # them.  That would turn unknown ownership into a silent negative
            # verdict about otherwise reconciled acts.
        if "payload" not in payload:
            if record.get("outcome") in WITNESS_READING_OUTCOMES:
                raise FatalAccounting(
                    "reading page Testimonium has no retained derived payload for content "
                    f"coverage: {record['artifact_id']} for page {ordinal}, chair {chair!r}; "
                    "restore the retained Attestatores record"
                )
            # A page witness that read nothing across every act on this page --
            # every configured act was `dead`, `not-run`, or otherwise non-reading
            # for this chair -- carries no retained `payload` text: `testimonium_payload`'s
            # reading-only bridge (pipeline/3_attestatores/run.py) never sets it for
            # a non-reading outcome. The outcome check above distinguishes that
            # legitimate absence from a malformed producer that claims it read the
            # page but lost the text. There is no witness content to diff against
            # attachments in the former case; the chair's absence stays visible
            # through its act-scoped Testimonia and the act's witness-coverage floor.
            continue
        text = payload.get("payload")
        if not isinstance(text, str):
            # Structured derived testimony is retained but has no comparable
            # page text.  Its act attachment is explicitly `comparable: false`,
            # so it cannot satisfy the witness floor; treating that declared
            # limit as a malformed page would erase the very evidence the
            # retirement is meant to preserve.
            continue
        spans = []
        for act_id, row in rows_by_page_chair.get((ordinal, chair), []):
            alignment = row.get("alignment")
            if (
                row.get("attached")
                and isinstance(alignment, dict)
                and alignment.get("status") == "aligned"
            ):
                span = alignment.get("witness_span")
                if not isinstance(span, dict) or not all(
                    isinstance(span.get(k), int) and not isinstance(span.get(k), bool)
                    for k in ("start", "end")
                ):
                    raise FatalAccounting("attached page witness has malformed alignment span")
                spans.append((span["start"], span["end"], act_id))
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
        finding["shortfall"] = finding["shortfall"] or bool(uncovered["count"])
    for finding in findings.values():
        if finding["by_chair"]:
            continue
        # This finding exists only because unclaimed geometry created it: no
        # chair on this page reported text, so the loop above never diffed one
        # and `by_chair` stayed empty. Leaving the seeded `shortfall: False`
        # would publish a clean text measurement nobody took, indistinguishable
        # from a page whose witnesses were read and covered everything
        # (GOVERNANCE 10) -- the very restatement `NO_PAGE_CONTENT_COVERAGE`
        # exists to avoid for pages that reach no measurement at all. The
        # unclaimed observations stay: they are geometry, and they still route
        # bounded recovery.
        finding["shortfall"] = None
        finding.setdefault("reason", NO_PAGE_CONTENT_COVERAGE["reason"])
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

    A page with no conservation record at all is **not** a page the Designator
    measured and found unmeasurable. It publishes one record per page it sealed,
    unmeasurable pages included, precisely "because a page with no conservation
    record at all is the silent gap this artifact exists to close"
    (`pipeline/2_designator/run.py::_publish_conservation_and_secondary`), so an
    absent record means the page never reached that stage — a door refusal, whose
    acts are already held for the page loss itself. Defaulting to
    `ink_measurable: False` here would restate a measurement nobody took, in a
    record byte-identical to a real unmeasurable page's (GOVERNANCE 10). The
    absence is recorded as absence instead, and every act gets its own object for
    the reason `testimony_content_for_page` does.
    """
    return copy.deepcopy(findings.get(ordinal, NO_PAGE_CONSERVATION))


def testimony_content_for_page(findings: dict[int, dict], ordinal: int) -> dict:
    """Return one review's private copy of a page-level content finding.

    The measurement is intentionally computed once per page, but review payloads
    are act-scoped consumers. Giving each consumer its own nested object prevents
    an in-process mutation made while preparing one act from changing a sibling
    act's still-to-be-published evidence. A page absent from `findings` had no
    page witness report text to measure; its None-valued fallback records that
    absence rather than restating it as a measured, clean page.
    """
    return copy.deepcopy(findings.get(ordinal, NO_PAGE_CONTENT_COVERAGE))


def review_route_from_findings(
    *,
    cross_capture_occluded_everywhere: bool = False,
    cross_capture_unresolved: bool | None = False,
    testimony_shortfall: bool | None,
    audit_unresolved: bool | None,
    under_witnessed: bool,
    unreconciled: bool = False,
) -> tuple[str, str] | None:
    """Compose every independent review cause in stable priority order.

    All active causes are retained under one ``held-for-review`` outcome.
    ``None`` means the corresponding measurement does not exist and therefore
    routes like ``False``; absence is not a measured shortfall.
    """
    # A shape guard, not a live filter: all four route inputs are booleans or
    # None today, so the walk finds nothing to inspect and this call cannot
    # currently refuse anything. Said plainly rather than left reading as a
    # screen that catches something (GOVERNANCE 10). It is kept because the
    # cost is four scalars and the day one of these becomes a nested fact is
    # the day the routing decision could carry vocabulary. The screens that do
    # bite are `publish_review` and the recovery payload, which see the nested
    # coverage objects.
    refuse_capture_preference(
        {
            "cross_capture_occluded_everywhere": cross_capture_occluded_everywhere,
            "cross_capture_unresolved": cross_capture_unresolved,
            "testimony_shortfall": testimony_shortfall,
            "audit_unresolved": audit_unresolved,
            "under_witnessed": under_witnessed,
            "unreconciled": unreconciled,
        },
        what="a Recensor review route",
    )
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
        reasons.append(
            "the Perlector exhausted its sealed audit re-proof cap with unresolved span(s); "
            "they remain explicit uncertainty rather than a silent retry"
        )
    if under_witnessed:
        reasons.append(
            "the configured act-level witness floor is not met; a witness failure is not coverage"
        )
    if unreconciled:
        reasons.append("the act did not reconcile and needs a human")
    if not reasons:
        return None
    return "held-for-review", "; ".join(reasons)


def publish_review(
    context,
    *,
    subject_id: str,
    outcome: str,
    attempt: str,
    inputs: list[dict],
    payload: dict,
) -> dict:
    """Write a review only after rejecting witness-selection vocabulary.

    Review records are a second durable consumer beside Perlectio: screening
    only the route inputs would leave a future direct payload field unchecked.
    """
    refuse_capture_preference(payload, what="a Recensor review")
    return context.publish(
        kind="review",
        subject_id=subject_id,
        outcome=outcome,
        attempt=attempt,
        inputs=inputs,
        payload=payload,
    )


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

    `held` is not the only terminal Designator outcome. `excluded` and `failed` are
    terminal in the same table (`common/contracts/outcomes.py`), and both stages of
    this file tested only for `held` — so either would fall through to the reading
    path and be reported as "reached the Recensor with no reading at all". That
    message is false: the act was never going to have a reading, and the imbalance
    was invented by the check rather than found by it.

    What review record such an act should get is genuinely undecided — `excluded` is
    approval-bound and `failed` is a refusal, and the Designator emits neither today.
    So this says exactly that, rather than inventing the policy or letting a wrong
    message stand. Whoever teaches the Designator to emit one lands the handling here
    and this refusal stops firing.
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
        # The earliest stage that can say so: a reading whose witness basis a
        # later Testimonium has superseded is not reconciled, and the Recensor is
        # what decides whether a reading may be accepted at all. Checked here as
        # well as at the Archetypus and the export because one derivation with
        # three consumers is what kept `recovery_region_count` from drifting, and
        # each of the three can be reached first by hand.
        require_current_witness_basis(
            act_id,
            latest,
            artifacts_for(context, ATTESTATORES, "testimonium", act_id),
            f"the current reading of {act_id}",
        )
        context.artifact_ref(PERLECTOR, "perlectio", latest["artifact_id"])
        audit_state(context, latest, act_id)
        if classify(PERLECTOR, latest["outcome"]) is OutcomeClass.COMPLETED:
            for region in _reconcile_reading_regions(latest, state["regions"], act_id):
                context.input_ref(region["image_path"])


def write_partition_receipt(context, budget: dict) -> None:
    """Rebuild the scoped Recensor partition receipt from disk, never manifests.

    Spec 09: "a self-hashed run receipt that **recomputes every denominator from
    the artifacts on disk** rather than trusting stage manifests." The mutable
    stage manifests stay a cache, so their agreement with disk is checked before
    either may stand beside a receipt; the denominator itself is rederived
    through `expected_acts`, and every review and coverage record is read afresh
    from the immutable artifacts.

    Its status is deliberately scoped, and the scope is part of the record: it
    speaks for the proposal-act and configured-witness denominators at the moment
    the Recensor reviewed them. It does not claim to be the run's final export
    verdict — the page-level residual-ink and continuation facts are recorded in
    the review payloads this receipt cites, and a page nobody cut a region on is
    outside every denominator here. Claiming otherwise would be exactly the
    "complete" GOVERNANCE 2 refuses.
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


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, RECENSOR, registry_factory=registry_factory)
    # The run's own sealed policy, parsed once when the run's binding was checked,
    # never reopened here. `config/recovery.toml` used to be read a second time at
    # this line: a rewrite landing between `open_context` and it published reviews
    # and recovery-requests carrying an allowance the run never sealed — measured
    # as both acts held for review with `budget_allowed: 0` under a run whose
    # digest bound the stock allowance, and unrecoverable afterwards because the
    # correct rerun computes different bytes under the same immutable review
    # identity and stops with IncompatibleReuse (audit S3). The recheck below
    # proves the carried policy is the sealed one, so a reintroduced second read
    # refuses instead of publishing.
    budget = context.recovery_policy
    context.require_sealed_config("recovery", budget["config_sha256"])

    scenario = scenario_for(context.fixture, context.scenario)
    floor = context.witness_floor

    # This pass must precede publication.  `latest_attempt` refuses duplicate
    # semantic ordinals rather than selecting an arbitrary hash-sorted record;
    # doing that only as each act is published can leave an earlier act's review
    # behind when a later act is malformed.
    preflight_witness_denominator(context, floor)
    preflight_recovery_history(context, budget)
    preflight_review_evidence(context, budget)

    # Both residual measurement and the witness-pointer gate use the union of
    # every proposal and recovery crop currently cut on the page.
    cut_regions = regions_by_source_page(context)
    # One verification pass over the sealed pages, on exactly the condition
    # `page_coverage_findings` already applied: with no region cut anywhere, it
    # returned before touching a page, and this stage still does not verify
    # pixels no crop was taken from.
    sealed_pages = sealed_page_images(context) if cut_regions else {}
    capture_digests = capture_digest_by_page(sealed_pages)
    page_findings = page_coverage_findings(context, sealed_pages)
    geometry_inputs = geometry_coverage_inputs(context)
    content_findings = testimony_content_findings(context)
    ink_maps = ink_map_by_page(context)
    # The remaining page-level inputs read once per run for the same reason:
    # the sealed occlusion records by page, and a cache of the local-act
    # proposal geometry every cross-capture view asks for.
    occlusions = occlusion_records_by_page(context)
    proposal_geometry: dict[str, dict] = {}
    # Tree-backed accounting keeps the page-wide grant spent across Recensor
    # passes; an in-memory counter would reset after the requested recrop.
    funded_pages = observation_funded_pages(context, expected_acts(context))

    held = 0
    for act in expected_acts(context):
        act_id, act_key = act["act_id"], act["act_key"]

        coverage = validate_chair_coverage(context, act_id, floor)
        content_coverage = testimony_content_for_page(content_findings, act["page_ordinal"])
        geometry_coverage = geometry_coverage_for(geometry_inputs, act["page_ordinal"])

        if act["outcome"] == "held":
            # The Designator could not mark this act out. There is no reading to
            # review and no recovery to request — recovery recovers coverage on
            # sealed ink, and this act's missing ink was never sealed. The act
            # still gets this stage's explicit outcome, so its terminal category
            # derives from a review like every other act's.
            hold, hold_path = designator_hold(context, act_id)
            # A Designator hold has two distinct shapes (`pipeline/2_designator/
            # run.py::initial_pass`): the act's own page never sealed, and no
            # region of it is cut at all; or the act's own page sealed and its
            # near-side region WAS cut, but a declared continuation's page
            # never sealed. `hold_regions` reads what was actually cut rather
            # than assuming the first shape for both — a real near-side region
            # has real continuation and page-coverage facts to report, and
            # hardcoding them empty would silently drop a flagged page's
            # evidence for the one act that touches it.
            hold_regions = artifacts_for(context, DESIGNATOR, "region", act_id)
            publish_review(
                context,
                subject_id=act_id,
                outcome="held-for-review",
                attempt=attempt_id(act_id, "recense", 1),
                inputs=[context.input_ref(hold_path)]
                + [context.input_ref(region["payload"]["image_path"]) for region in hold_regions],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": 1,
                    "reason": f"the Designator held this act: {hold['payload']['reason']}",
                    "coverage": coverage,
                    "geometry_coverage": geometry_coverage,
                    "testimony_content_coverage": content_coverage,
                    "continuation": recensor_continuation_link(hold_regions, act_id),
                    "page_coverage": page_coverage_for(hold_regions, page_findings),
                    "recoveries_used": 0,
                    "budget_allowed": budget["allowed"],
                    "absolute_cap": budget["absolute_cap"],
                    # None, not absent, and not False: a Designator-held act
                    # has no Perlectio and therefore no audit to report. The
                    # field stays universal so a consumer can tell "no audit
                    # exists" (here) from "audited, resolved" (False) and
                    # "audited, unresolved" (True).
                    "audit_unresolved": None,
                    # None for the same reason: a held act was never shown
                    # real capture pixels, so there is no cross-capture
                    # visibility survey to report, universally present like
                    # every field above.
                    "cross_capture_coverage": None,
                },
            )
            held += 1
            continue

        state = recovery_state(context, act_id, budget)
        if state["outstanding_request_ids"]:
            # The matching review is already the durable record of this hold. A
            # direct Recensor retry must not turn it into a later acceptance while
            # the Designator has not yet cut the requested recovery crop.
            held += 1
            continue

        # `preflight_review_evidence`, above, already refused a non-held act with
        # no reading at all, over this same list and the same Designator seal
        # this process never writes to.
        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)

        # Every review is about one specific Perlectio, not merely the current
        # object a later stage happens to find.  The reference is both an input
        # digest and a payload fact so Archetypus can prove it establishes the
        # exact reading Recensor assessed.
        latest = latest_attempt(readings, f"reading of {act_id}", operation="perlegere")
        latest_payload = _payload(latest, f"reading of {act_id}")
        audit_unresolved = audit_state(context, latest, act_id)
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
        # WAVE WIRING (was the pre-wave seam `False`): R5b's Pass-C producer
        # now sits below this branch, so the composer receives the verified
        # audit state the seat-era candidate could not have. Computed here,
        # after audit_state, because a held act has no reading and no audit
        # chain to consult — it takes its own branch above and never reaches
        # the routing that consumes this.
        findings_route = review_route_from_findings(
            cross_capture_occluded_everywhere=cross_capture_occluded_everywhere,
            cross_capture_unresolved=cross_capture_unresolved,
            testimony_shortfall=content_coverage["shortfall"],
            audit_unresolved=audit_unresolved,
            under_witnessed=coverage["under_witnessed"],
            unreconciled=act_key in scenario["hold_acts"],
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

        # The residual-ink check, against the page(s) this act's own current
        # regions were cut from — never against what any stage claimed to
        # find. A flagged page holds every act that touches it: nobody yet
        # knows which act, if any, the uncovered ink belongs to, so a human
        # needs the whole page, not a guess at which one act is "responsible".
        page_coverage = page_coverage_for(state["regions"], page_findings)
        flagged_pages = page_coverage["flagged_pages"]

        used_total = len(state["requests"])
        used_fallback = len(state["requests_by_kind"][FALLBACK_RECROP])
        allowed_fallback = recovery_kind_budget(budget, FALLBACK_RECROP)
        # A witness's geometry is only a pointer; independently measured ink
        # outside the live crop union is required to authorize recovery.
        # Without this check, an Attestator's mis-reported box alone could
        # spend a real recovery budget or hold an act on zero actual ink.
        outside_ink_requests = unclaimed_ink_observations(
            ink_maps,
            content_coverage.get("unclaimed_observations", []),
            act["page_ordinal"],
            cut_regions,
        )
        wants_recovery = (
            act_key in scenario["recover_acts"]
            or (bool(outside_ink_requests) and act["page_ordinal"] not in funded_pages)
        ) and used_total == 0
        observation_hold = unresolved_observation_hold(
            outside_ink_requests, act["page_ordinal"], funded_pages
        )
        ordinal = used_total + 1

        # The cap is enforced at the request boundary rather than by convention
        # (spec 09's third test): the kind's own allowance, the pooled total, and
        # Tyrel's absolute cap all have to permit this request before it is made.
        # `allowed` can only ever be the smaller of the three today, but a policy
        # is a file somebody edits and a bound nobody checks is not a bound.
        if (
            not continuation_shortfall
            # Cross-capture geometry neither funds nor vetoes a recovery: only
            # the Unit 14B ink observation and bounded grants do. The survey
            # covers the current proposal, not the unclaimed ink outside it.
            and wants_recovery
            and used_fallback < allowed_fallback
            and used_total < budget["allowed"]
            and used_total < budget["absolute_cap"]
        ):
            # The Recensor asks; the Designator cuts. Recording the request as an
            # artifact is what keeps the loop countable from the tree alone. Only
            # `fallback-recrop` is requested here: it is the one recovery
            # operation this pipeline can actually dispatch today (a Designator
            # recrop). `page-level-reread` stays a real, distinct, budgeted kind
            # in the policy and the payload schema below, ready for the day a
            # Perlector continuation-aware reread exists to answer it — but this
            # stage does not request an operation nothing downstream can honor,
            # because a request the orchestrator can only refuse turns a graceful
            # hold into a hard failure for no gain.
            request_origin = recovery_request_origin(
                declared=act_key in scenario["recover_acts"],
                outside_ink_requests=outside_ink_requests,
            )
            if request_origin == COVERAGE_OBSERVATION_ORIGIN:
                # A shape guard, and it cannot fire on the production path
                # today -- said plainly here rather than left to be discovered,
                # the way the route screen above already states its own reach.
                # Every conjunct below is proved before this point: the three
                # budget comparisons are the enclosing condition's own, this
                # origin is reachable only from a non-empty measured ink
                # observation, and that same branch is what proves the page's
                # grant unspent. So `admitted` is true whenever control arrives.
                # Its worth is structural: it is a second spelling of the rule,
                # and it fires if a later edit makes the live gate looser than
                # the contract. It does not, on its own, hold the two in
                # agreement -- an edit that loosens both together would pass.
                dossier = latest_payload.get("dossier")
                gate = capture_specific_recovery(
                    logical_act_id=(
                        dossier["logical_act_id"]
                        if isinstance(dossier, dict) and "logical_act_id" in dossier
                        else act_id
                    ),
                    # The sealed page's own verified pixel digest, which is the
                    # capture identity the partition and the autopsia use.
                    # `run.json`'s source-manifest row is the *submission's*
                    # declaration: optional at real ingress and shared by every
                    # page rendered from one container, so reading `["sha256"]`
                    # off it could hand the gate `None` (refused as "lacks
                    # logical-act/capture identity", which names the wrong
                    # fault) or a digest belonging to a different capture.
                    source_sha256=capture_digest_for(capture_digests, act["page_ordinal"], act_id),
                    page_ordinal=act["page_ordinal"],
                    # This origin is reachable only from a non-empty measured
                    # ink observation; duplicating that expression would let
                    # the gate and its structural guard drift independently.
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
            # Screened inline rather than behind a `publish_recovery_request`
            # helper. This stage's fourth durable record carries the same nested
            # coverage objects `publish_review` screens, and unscreened it was
            # the one shape where a preference field reached disk. But the
            # quality firewall in `test_quality_firewall.py` finds this write by
            # its literal `kind="recovery-request"` and reads the conditionals
            # around it; moving the call into a helper would put the request
            # outside every gate as far as that scan could see, and a firewall
            # taught to follow wrappers is a firewall that can be walked around.
            recovery_payload = {
                "act_key": act_key,
                "attempt_ordinal": ordinal,
                "recovery_kind": FALLBACK_RECROP,
                # The origin as data beside the sentence that states it, so
                # the page-wide bound counts a recorded fact rather than
                # re-reading prose (`observation_funded_pages`). The origin
                # is single -- declaration takes precedence for funding --
                # while the reason keeps every triggered cause visible when
                # the two coincide.
                "origin": request_origin,
                "reason": recovery_request_reason(
                    declared_crop=act_key in scenario["recover_acts"],
                    unclaimed_observation=len(outside_ink_requests) > 0,
                ),
                "budget_allowed": budget["allowed"],
                "budget_used": used_total,
                "kind_budget_allowed": allowed_fallback,
                "kind_budget_used": used_fallback,
                "coverage": coverage,
                "geometry_coverage": geometry_coverage,
                "testimony_content_coverage": content_coverage,
                "perlectio_ref": reading_ref,
                "recovery_policy": budget,
            }
            refuse_capture_preference(recovery_payload, what="a Recensor recovery request")
            request = context.publish(
                kind="recovery-request",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recover", ordinal),
                inputs=[reading_ref],
                payload=recovery_payload,
            )
            # Spent where the request is actually published, not where
            # `wants_recovery` was computed: an act that wanted recovery and was
            # refused it by budget or continuation must not consume the page's
            # one grant on the way past. The condition is the same one that
            # recorded the origin above, so the page marked funded is exactly
            # the page whose request says it was.
            if request_origin == COVERAGE_OBSERVATION_ORIGIN:
                funded_pages.add(act["page_ordinal"])
            request_ref = context.input_ref(request.relative_path)
            publish_review(
                context,
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recense", ordinal),
                inputs=[reading_ref, request_ref],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "recovery_kind": FALLBACK_RECROP,
                    "coverage": coverage,
                    # R6 audit F-O7: this was the only review shape carrying
                    # neither field, so its consumers could not tell "checked
                    # and clear" from "never checked".
                    "geometry_coverage": geometry_coverage,
                    "testimony_content_coverage": content_coverage,
                    "continuation": continuation_link,
                    "page_coverage": page_coverage,
                    "perlectio_ref": reading_ref,
                    "recovery_request_ref": request_ref,
                    "recovery_policy": budget,
                    # This act WAS audited (computed above for every act with a
                    # Perlectio); omitting the field here would read back as
                    # None -- "no audit exists" -- which is false, and R8's
                    # canonical export is the consumer that would believe it.
                    "audit_unresolved": audit_unresolved,
                    "cross_capture_coverage": cross_coverage,
                },
            )
            held += 1
            continue

        # Whether the reading *succeeded*, not merely whether one exists. The
        # Archetypus copies `payload["text"]` out of whatever the latest reading
        # is, so a `truncated` or `failed` Perlectio carrying stale text would be
        # established as the one text and a `not-run` one would crash on the
        # missing field. GOALS 2 is accuracy against the ink; text nobody
        # successfully read is not a reading, and GOVERNANCE 2 says it may not
        # vanish behind a successful status either. Held, visibly, outcome named.
        blank_evidence = None
        if reading_class is not OutcomeClass.COMPLETED:
            # `no-readable-text` is the one non-completed Perlector outcome that
            # can end here rather than in the ordinary hold below: it is the
            # Perlector's own direct finding (autopsia against the ink, not
            # testimony), and `blank_corroboration` asks only whether the
            # witnesses corroborate or contradict it — never a selection among
            # them. Every other non-completed outcome (`failed`, `truncated`,
            # `not-run`) falls straight through to the ordinary hold: none of
            # them is a positive claim of absence, so there is no absence here
            # to confirm.
            # One `chair_current_attempts` collapse feeds both maps, so "the
            # gate and the outcomes cannot disagree about which attempt is
            # current" is structural rather than two identical walks agreeing.
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
                    # Every hold cause the ordinary chain below would apply,
                    # asked once. `confirmed-blank` is COMPLETED-class and
                    # terminal, so a cause that only appears in that chain is a
                    # cause this seal silently overrides: an act whose page
                    # carries witness text outside every aligned attachment, or
                    # whose Perlector exhausted its audit re-proof cap, would be
                    # sealed complete over a shortfall this stage had already
                    # measured (GOVERNANCE 2; invariant 6). The three page-level
                    # conditions above are named separately because they are
                    # already refused before the route is consulted; this
                    # subsumes the scenario hold `act_key not in
                    # scenario["hold_acts"]` used to state, through the
                    # composer's own `unreconciled` cause.
                    and findings_route is None
                    # The fourth page-level cause, and the newest. Unit 9 still
                    # confirming ink in a witness pointer outside every cut is
                    # exactly the shortfall the three conditions above exist to
                    # keep out of a terminal seal: with the page's one grant
                    # already spent, or the budget exhausted, no request is
                    # published, so this cause appears only in the chain below
                    # and `confirmed-blank` would silently override it. An act
                    # sealed COMPLETED-class over measured, unclaimed ink is the
                    # missed act GOALS 1 puts above every other failure.
                    and observation_hold is None
                )
                else None
            )
            if corroborating_chairs is not None:
                outcome, reason = (
                    "confirmed-blank",
                    "the Perlector's own reading found no-readable-text, and every witness "
                    f"that actually read this act ({', '.join(corroborating_chairs)}) "
                    "independently reports the same absence; sealed blank with that evidence",
                )
                # Spec 09 seals a blank "with evidence", and a sentence is not
                # evidence a consumer can read. The review queue, the Armarium
                # and anyone re-deriving this outcome get the same facts as
                # data: what the reading itself found, which chairs corroborated
                # it, and which pages this check found no ink outside coverage
                # on. That is narrower than "clear": the check never looks
                # inside the act's own crop, so the field is named for exactly
                # what it measured and no more (GOVERNANCE 10). The `reason`
                # above stays, for a human reading one record.
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
                "proposal set — GOALS 1: a missed act is worse than a poorly read one); "
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
            outcome, reason = "accepted", "coverage and geometry reconcile"

        # Derived from the outcome's own class rather than counted by hand in each
        # branch above, so a review shape added later cannot land in the tree
        # without also landing in this stage's exit code.
        if classify(RECENSOR, outcome) is not OutcomeClass.COMPLETED:
            held += 1

        publish_review(
            context,
            subject_id=act_id,
            outcome=outcome,
            attempt=attempt_id(act_id, "recense", ordinal),
            # `latest`, never `readings[0]`: manifest order is a hash, so after a
            # recovery the first record can be the superseded attempt, and citing
            # its crop as the basis for accepting the new reading is deterministic
            # and wrong. `basis_regions` is empty unless the reading completed —
            # a `not-run` Perlectio carries no `basis` key at all, and indexing it
            # would turn an honest hold into a traceback.
            inputs=[reading_ref]
            + [context.input_ref(reference["image_path"]) for reference in basis_regions],
            payload={
                "act_key": act_key,
                "attempt_ordinal": ordinal,
                "reason": reason,
                "coverage": coverage,
                "geometry_coverage": geometry_coverage,
                "testimony_content_coverage": content_coverage,
                "continuation": continuation_link,
                "recoveries_used": used_total,
                "budget_allowed": budget["allowed"],
                "absolute_cap": budget["absolute_cap"],
                "perlectio_ref": reading_ref,
                # Recorded the same way for every act rather than only when it
                # flags something — the same reasoning `continuation` above is
                # recorded under: a consumer that only ever sees the field
                # populated cannot tell "checked and clear" from "never checked".
                "page_coverage": page_coverage,
                # The Pass-C verdict, recorded as data for every act for the
                # same reason: an act held for an exhausted audit cap must be
                # separable from every other hold without matching prose, and
                # an audit whose sealed round was spent must be tellable from
                # one never checked. R8's canonical export reads uncertainty
                # spans whose review-side "why" lives exactly here.
                "audit_unresolved": audit_unresolved,
                "cross_capture_coverage": cross_coverage,
                # Present only on a `confirmed-blank`, because it is the evidence
                # that outcome rests on and nothing else has any. Every other
                # review carries the fields above and no more.
                **({"blank_evidence": blank_evidence} if blank_evidence is not None else {}),
            },
        )

    # The partition receipt is a refusal-capable part of closing the Recensor
    # pass.  Its denominator requires a current stored manifest, so write that
    # derived cache first; only a pass whose receipt succeeds may publish the
    # completion seal, after which the final manifest includes that seal.
    context.finish()
    write_partition_receipt(context, budget)
    context.seal_boundary()
    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
