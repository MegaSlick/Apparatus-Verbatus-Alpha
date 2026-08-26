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
    classify_capture_visibility,
    expected_surface_cells,
)
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
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
from common.corpus_register import refuse_preference  # noqa: E402
from common.cross_capture_autopsia import validate_autopsia  # noqa: E402
from common.cross_capture_coverage import (  # noqa: E402
    build_cross_capture_coverage,
    capture_specific_recovery,
    same_chair_witness_floor,
)
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.native_witness import (  # noqa: E402
    reported_geometry_overlaps,
    validate_page_testimonium_payload,
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
    WITNESS_READING_OUTCOMES,
    expected_acts,
    latest_attempt,
    latest_per_chair,
    open_context,
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
    (`pipeline/4_perlector/run.py`: a Designator-held act, and an explicitly
    absent Perlector chair). It never reaches Pass C, so there is no chain to
    verify and no unresolved span to route on — the act is held on its own
    outcome further down. Demanding a chain here turned the absent-chair hold
    this stage is built to report into a traceback about missing final text,
    which is exactly the trap the `basis_regions` guard below is named for.
    Every attempted outcome (`read`, `truncated`, `no-readable-text`, `failed`)
    publishes the pair and is verified; a forged `not-run` buys nothing, because
    that class is held rather than accepted.

    `None`, not `False`: this act has no audit at all — the same fact a
    Designator-held act's review records. `False` means audited and resolved,
    and claiming it here would tell R8's canonical export that a reading
    nobody examined came back clean. Routing is unchanged (`elif
    audit_unresolved:` treats both as falsy); only the record is honest.
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
            # The exact current record identity travels with the projections.
            # `act_attachment_facts` must prove that an act-scoped attachment
            # references this record, rather than combine the current outcome
            # with a stale Testimonium's payload and call the result a
            # re-derivation.
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


def _enclosing_bounds(bounds_list: list[dict]) -> dict[str, int]:
    x0 = min(b["x"] for b in bounds_list)
    y0 = min(b["y"] for b in bounds_list)
    x1 = max(b["x"] + b["w"] for b in bounds_list)
    y1 = max(b["y"] + b["h"] for b in bounds_list)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


SURVEY_ABSENT = "act-visibility-survey-absent"
REGISTRATION_ABSENT = "cross-capture-registration-absent"
# The two causes that say the *instrument* is missing rather than that a
# measurement came up short. Consult §4.1 gives both the same act-level state
# (`capture-visibility-unresolved`, never an occlusion claim); they are held
# apart from a measured shortfall only where the review route is decided, and
# `cross_capture_review_causes` is the one place that reads this set.
INSTRUMENT_ABSENT_CODES = frozenset({SURVEY_ABSENT, REGISTRATION_ABSENT})


def _page_occlusion_survey(context, page_id: str) -> dict:
    """What this run actually knows about occlusion on one Exemplar page.

    ``surveyed`` is the fact Unit 19C cannot do without and Sonnet's round-2
    wiring inferred: consult §4.1 is explicit that "absence of an occlusion
    artifact is not proof of visibility until the producer seals a complete
    survey", and §11.3 that until that producer lands "every such state is
    `unresolved`; it is never inferred visible from absence". A sealed
    occlusion record naming the page is the only evidence in this repository
    that an occlusion pass ran over it at all -- the Designator's production
    run publishes none (`pipeline/2_designator/run.py` never reaches
    `geometry_layer`'s occlusion path), so today every page is unsurveyed and
    every act's visibility is honestly unresolved rather than falsely full.
    A page carrying only a `below-ink` record is surveyed and unoccluded: the
    survey ran and positively placed its one occluder behind the ink.

    Read independently of ``geometry_layer.validate_occlusion`` -- a stage may
    not import another stage's own module -- so only the fields this survey
    actually needs are trusted here: real page lineage, a real polygon, and
    the one z_relationship that positively proves an occlusion does NOT sit
    in front of the ink. Everything else is treated as occluding, matching
    how conservatively the existing (unconnected) page-wide resolver rule
    already treats any occlusion at all.
    """
    polygons: list[list[dict[str, int]]] = []
    refs: list[str] = []
    surveyed = False
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "occlusion":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "occlusion", entry["artifact_id"])
        payload = _payload(record, f"Designator occlusion {record['artifact_id']}")
        if payload.get("page_id") != page_id:
            continue
        surveyed = True
        polygon = payload.get("polygon")
        if (
            not isinstance(polygon, list)
            or len(polygon) < 3
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
        # The evidence an `occluded` classification rests on, carried into the
        # published row: consult §4.1 requires the finding to record the
        # occlusion refs, and GOALS 5 wants every result to return to the ink
        # it came from. An empty list here used to be published beside a real
        # occlusion claim, which is a claim with its evidence detached.
        refs.append(record["artifact_id"])
    return {"surveyed": surveyed, "polygons": polygons, "occlusion_refs": sorted(refs)}


def act_cross_capture_coverage(context, act_id: str, latest_payload: dict) -> dict | None:
    """This act's real Unit 19C visibility survey, or ``None`` with no basis.

    Reads the current Perlectio's own sealed ``cross_capture_autopsia`` --
    the complete registered-capture presentation the Perlector actually read
    from (consult §3.1) -- rather than rebuilding a second partition here.
    ``None`` exactly when the current reading was never actually shown real
    capture pixels (a Designator hold, an absent chair, or an over-capacity
    cluster): there is then no registered presentation to survey, and this
    act's own page geometry alone cannot stand in for the logical act's
    complete required-capture set.

    Every view's own local acts' sealed proposal geometry is read directly
    from the Designator's manifest (``_proposal_geometry_by_page``, already
    generic over any local act_id) -- including a sibling member's, when the
    logical act's autopsia names one -- so a genuinely clustered act surveys
    real geometry from every one of its registered captures, not only the
    one local act driving this loop's current iteration.

    **Two evidence preconditions decide what a row may claim**, because the
    consult makes both of them preconditions of the union rather than
    niceties (§4.1, §11.3, GOVERNANCE 10 "a metric that cannot be measured is
    a failure, not a pass"):

    * a capture is measured only where its pages carry a sealed occlusion
      survey (``_page_occlusion_survey``). Otherwise the row is `unresolved`
      with ``act-visibility-survey-absent`` -- never `visible` inferred from
      an absent artifact;
    * a component of more than one capture may only union its cells through a
      sealed geometric alignment. The cells this build can produce are
      normalized to each capture's own footprint
      (``common/act_visibility_geometry.py``), and no registration exists in
      this repository to map them into one frame, so every row of a
      multi-capture component is `unresolved` with
      ``cross-capture-registration-absent``. This is the case the union was
      built for and it is exactly the case the evidence cannot yet support:
      two captures each showing half of one act would otherwise both report
      their own 16 cells visible and union to `full`.

    Neither precondition can be met by any production run on this base, so
    every real survey today is honestly unresolved. The union arithmetic
    itself is unchanged and remains exercised over explicit cell surfaces by
    ``pipeline/5_recensor/test_cross_capture_coverage.py``; what this function
    refuses to do is manufacture those cells from evidence that is not there.
    """
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
            for page in _proposal_geometry_by_page(context, local_id).values():
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
            page_survey = _page_occlusion_survey(context, page_id)
            surveyed = surveyed and page_survey["surveyed"]
            polygons.extend(page_survey["polygons"])
            occlusion_refs.extend(page_survey["occlusion_refs"])
        if surveyed:
            survey = classify_capture_visibility(
                bounds=_enclosing_bounds(bounds_list), occlusion_polygons=polygons
            )
            row = {
                "source_sha256": view["source_sha256"],
                "alignment_ref": view["alignment_ref"],
                "visibility_state": survey["visibility_state"],
                "visible_cells": survey["visible_cells"],
                "occluded_cells": survey["occluded_cells"],
                "occlusion_refs": sorted(set(occlusion_refs)),
                "finding_codes": [],
            }
        else:
            row = {
                "source_sha256": view["source_sha256"],
                "alignment_ref": view["alignment_ref"],
                "visibility_state": "unresolved",
                "visible_cells": [],
                "occluded_cells": [],
                "occlusion_refs": sorted(set(occlusion_refs)),
                "finding_codes": [SURVEY_ABSENT],
            }
        physical_page = view["physical_page_id"]
        expected = expected_surface_cells()
        component = components.setdefault(
            physical_page,
            {"expected_cells": expected, "captures": [], "required": []},
        )
        # Checked, not `setdefault`-and-forget (consult §7.14): a second view
        # of one physical page whose expected surface disagreed with the
        # first's would otherwise have the first view's surface silently
        # stand in for it, and the extent of a component is exactly the fact
        # a coverage denominator may not take from whichever member arrived
        # first (§4.1, "no member's bounding box is selected as the extent").
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
        # Consult §4.1: "Union visible masks only after mapping each mask
        # through sealed geometric alignment." Nothing in this repository
        # registers one capture's pixels onto another's -- every
        # `alignment_ref` a production partition carries is an opaque string,
        # and the cells above are normalized to each capture's own footprint
        # -- so these rows are not addressed in one frame and may not be
        # unioned. Recording that as an unresolved measurement keeps the act
        # visible as unproven (GOVERNANCE 2) instead of publishing a
        # `full` union the evidence does not support.
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
    """The two review causes this act's visibility survey actually supports.

    Returns ``(occluded_everywhere, unresolved)`` exactly as
    ``review_route_from_findings`` reads them.

    ``occluded_everywhere`` is read from the published findings rather than
    from ``act_state``, because ``act_state`` is `unresolved` for a
    continuation whose one component was measured occluded in every capture
    while another component was fully seen. The finding is in the record
    either way, but routing off the act-level state alone would leave the act
    held under the vaguer reason and never say the exact thing that was
    measured (GOVERNANCE 2: a finding may not disappear behind a status).

    ``unresolved`` is ``None`` -- the "no measurement exists" value this
    stage's route already uses for ``testimony_shortfall`` and
    ``audit_unresolved``, and which routes like ``False`` -- when every
    unresolved component is unresolved only because the instrument is absent:
    no sealed occlusion survey, no cross-capture registration. An absent
    measurement is not itself a measured shortfall. It is recorded in the
    review payload and named in the finding, so nothing is lost; what it may
    not do is convert a producer this pipeline has not built yet into a
    universal hold on every act of every run, which would report the state of
    our tooling as a finding about the ink. A component where anything *was*
    measured and the surface still does not reconcile is a real shortfall and
    holds.
    """
    if coverage is None:
        return False, None
    occluded_everywhere = any(row["code"] == "occluded-everywhere" for row in coverage["findings"])
    unresolved_components = [
        row for row in coverage["components"] if row["union_state"] == "unresolved"
    ]
    if not unresolved_components:
        return occluded_everywhere, False
    if all(_component_measured_nothing(row) for row in unresolved_components):
        return occluded_everywhere, None
    return occluded_everywhere, True


def _component_measured_nothing(component: dict) -> bool:
    """True when no capture of this component was measured at all.

    Deliberately stricter than "some row names an absent instrument": a
    component where one capture was surveyed and left a gap has a measured
    shortfall, and the fact that a second capture could not be measured is
    the reason the gap stands rather than an excuse for not holding it.
    """
    return all(
        row["visibility_state"] == "unresolved"
        and row["finding_codes"]
        and set(row["finding_codes"]) <= INSTRUMENT_ABSENT_CODES
        for row in component["captures"]
    )


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
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("chair"), str):
            raise FatalAccounting(f"act {act_id} has malformed derived act-attachment entry")
        chair = entry["chair"]
        if not isinstance(entry.get("attached"), bool) or not isinstance(
            entry.get("comparable"), bool
        ):
            raise FatalAccounting(f"act {act_id} has ambiguous derived act-attachment facts")
        attachment_basis = entry.get("attachment_basis")
        if attachment_basis not in {
            "presented-region",
            "anchor-line",
            "geometric-overlap",
            "unattached",
        }:
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
        if page_witness:
            page_ordinal = entry.get("page_ordinal")
            if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool):
                raise FatalAccounting(
                    f"act {act_id} page witness {chair!r} has no integer page ordinal"
                )
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
                page_payload = validate_page_testimonium_payload(page_testimonium.get("payload"))
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
            geometrically_attached = outcomes.get(chair) in WITNESS_READING_OUTCOMES and any(
                reported_geometry_overlaps(page_payload, bounds)
                for bounds in proposal_page["bounds"]
            )
            if entry["attached"] != geometrically_attached:
                raise FatalAccounting(
                    f"act {act_id} page attachment for chair {chair!r} does not derive from "
                    "that witness's reported geometry against the sealed proposal"
                )
            if entry["comparable"] and not entry["attached"]:
                raise FatalAccounting(
                    f"act {act_id} has comparable text without an attached witness"
                )
            alignment = entry.get("alignment")
            if not isinstance(alignment, dict) or alignment.get("status") not in {
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
            # The floor seam's own derivation of the comparability safety net,
            # independent of the Perlector's (which asks the same question of
            # the same evidence at its read seam). `attached` is geometry and
            # says nothing about text; a chair counts only where this page
            # record retains text AND this act's alignment placed a span in it.
            # Believing the sealed boolean would leave the retirement guarded by
            # a field no reader recomputes.
            if entry["comparable"] != (
                entry["attached"]
                and alignment["status"] == "aligned"
                and isinstance(page_payload.get("payload"), str)
            ):
                raise FatalAccounting(
                    f"act {act_id} page attachment for chair {chair!r} claims a comparability "
                    "its own retained page testimony does not support"
                )
        else:
            # The act-scoped half. The floor may not be counted from a boolean
            # this stage never checked against the Testimonium it names, so the
            # exact current record is read and its outcome plus retained derived
            # payload answer both predicates.  Reading `attached` back from this
            # attachment row would make `comparable = attached and text` one
            # assertion in two costumes: a producer could forge both booleans
            # false and quietly remove a completed chair from the floor.
            reference = entry.get("testimonium_ref")
            if not isinstance(reference, dict):
                raise FatalAccounting(
                    f"act {act_id} act-scoped witness {chair!r} has no Testimonium reference"
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
                    f"Testimonium: {error}"
                ) from error
            act_payload = testimonium.get("payload")
            if not isinstance(act_payload, dict) or act_payload.get("chair") != chair:
                raise FatalAccounting(
                    f"act {act_id} act-scoped attachment for chair {chair!r} points to "
                    "another chair's Testimonium"
                )
            current = current_attempts.get(chair)
            if not isinstance(current, dict) or testimonium.get("artifact_id") != current.get(
                "artifact_id"
            ):
                raise FatalAccounting(
                    f"act {act_id} act-scoped attachment for chair {chair!r} does not point "
                    "to that chair's current Testimonium; its referenced witness basis has "
                    "since superseded"
                )
            derived_attached = testimonium.get("outcome") in WITNESS_READING_OUTCOMES
            if entry["attached"] != derived_attached:
                raise FatalAccounting(
                    f"act {act_id}'s derived act-attachment disagrees with the current "
                    f"Testimonium outcome for chair {chair!r}; the witness floor may not be "
                    "counted from a superseded attempt"
                )
            if entry.get("page_ordinal") is not None or entry.get("alignment") is not None:
                raise FatalAccounting(
                    f"act {act_id} act-scoped attachment for chair {chair!r} carries page "
                    "alignment evidence"
                )
            expected_basis = "presented-region" if derived_attached else "unattached"
            if attachment_basis != expected_basis:
                raise FatalAccounting(
                    f"act {act_id} act-scoped attachment for chair {chair!r} names "
                    f"{attachment_basis!r} instead of its derived {expected_basis!r} basis"
                )
            if entry["comparable"] != (
                derived_attached and isinstance(act_payload.get("payload"), str)
            ):
                raise FatalAccounting(
                    f"act {act_id} attachment for chair {chair!r} claims a comparability its "
                    "own retained derived testimony does not support"
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
                raise FatalAccounting(f"act {act_id} has ambiguous derived act-attachment facts")
            # A page witness has one attachment for every contributing page.
            # Its act-level floor remains the one act attempt, so a continuation
            # whose page has no anchor cannot erase the primary page's valid
            # attachment; all page references remain separately checked by the
            # content denominator below.
            # One contribution has to carry both predicates.  OR-ing them
            # independently would let geometry on one continuation and text on
            # another create a countable witness neither page actually supplied.
            facts[chair]["comparable"] = facts[chair]["comparable"] or (
                fact["attached"] and fact["comparable"]
            )
            facts[chair]["attached"] = facts[chair]["attached"] or fact["attached"]
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
    # Unit 19C: the same floor, re-derived through the cross-capture union
    # primitive rather than trusted merely because `witness_coverage` is
    # well-tested. Every act today is one component ("whole"): a genuinely
    # clustered logical act would union real per-capture rows here instead
    # (`pipeline/4_perlector/test_cross_capture_cluster_path.py`'s two-capture
    # precedent), but no production caller can reach that yet (19B's own
    # `logical_reading.py` refuses to read a clustered partition at all). A
    # chair truncated on its current attempt is comparable text
    # `witness_coverage` still excludes from its floor (`truncated is not
    # True`); `same_chair_witness_floor` has no truncation concept of its own,
    # so that exclusion is folded into the row's `comparable` fact here rather
    # than left for the cross-check to disagree on a fact both functions
    # actually agree about.
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


def page_coverage_findings(context) -> dict[int, dict]:
    """Residual-ink findings for every sealed page with at least one region cut
    on it (ARCHITECTURE's candidate list: "a residual-ink check whose input is
    the page image itself, never the proposal set"), computed once per run and
    reused by every act that reaches one of these pages.

    Deterministic core, cheapest instrument first: pure geometry over the
    page's own pixels, entirely independent of any witness or reading. A page
    with zero regions cut on it at all is not checked here — see
    `regions_by_source_page`.
    """
    regions = regions_by_source_page(context)
    if not regions:
        return {}
    pages = sealed_page_images(context)
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
    """Read Unit 9's sealed page-space evidence once; this stage never re-decodes
    a page to make a second ink measurement (consult §4.5: "the box is a
    pointer; the ink is the evidence").
    """
    maps: dict[int, dict] = {}
    for entry in context.tree.build_manifest(INK_MAP)["artifacts"]:
        if entry["kind"] != "ink-map":
            continue
        record = context.tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
        payload = _payload(record, "ink-map")
        ordinal = payload.get("page_ordinal")
        evidence = payload.get("edge_findings")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(evidence, dict)
            or evidence.get("schema") != "ink-runs.v1"
            or ordinal in maps
        ):
            raise FatalAccounting("ink-map has no unique readable page-space edge findings")
        maps[ordinal] = evidence
    return maps


def _merged_row_coverage(covered: list[dict], y: int, x0: int, x1: int) -> list[tuple[int, int]]:
    """The cut regions crossing row ``y``, clipped to ``[x0, x1)`` and merged.

    Merged rather than summed per rectangle: two acts cut on the same page may
    overlap, and subtracting each one's span separately would remove the shared
    pixels twice and understate the ink that really is outside every cut.
    """
    spans = sorted(
        (max(x0, bounds["x"]), min(x1, bounds["x"] + bounds["w"]))
        for bounds in covered
        if bounds["y"] <= y < bounds["y"] + bounds["h"]
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _ink_outside_cuts_in_box(evidence: dict, box: dict, covered: list[dict]) -> int:
    """Ink of Unit 9's retained runs inside ``box`` and outside every cut region.

    Consult §4.5 condition (2) measures "THE INK MAP shows >= MINIMUM_INK_PIXELS
    in **the outside part**", and §4.3 fixes what "outside" means: the mask is
    "every region currently cut (proposal AND recovery)". Unit 10C's own
    `unclaimed_observations` denominator is the *proposal* set alone
    (`common/native_witness.py::unrouted_observations`, deliberately, so a
    later crop cannot retroactively claim an earlier observation), so a pointer
    can sit inside a recovery crop already cut for a neighbouring act on the
    same page and still be retained as unclaimed. Counting the whole box would
    let ink the Designator has already cut confirm a *fresh* expanded recrop of
    it -- work with nothing left to recover, spent out of the one bounded pool
    a genuinely missed region draws on. Subtracting the live mask here is what
    makes condition (2) measure the part §4.5 names.
    """
    width, height, rows = evidence.get("width"), evidence.get("height"), evidence.get("rows")
    if (
        not isinstance(width, int)
        or not isinstance(height, int)
        or not isinstance(rows, list)
        or len(rows) != height
    ):
        raise FatalAccounting("ink-map edge findings are malformed")
    x0, x1 = max(0, box["x"]), min(width, box["x"] + box["w"])
    y0, y1 = max(0, box["y"]), min(height, box["y"] + box["h"])
    total = 0
    for offset, row in enumerate(rows[y0:y1]):
        if not isinstance(row, list):
            raise FatalAccounting("ink-map edge findings contain a malformed row")
        previous_end = 0
        spans: list[tuple[int, int]] = []
        for run in row:
            if (
                not isinstance(run, list)
                or len(run) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in run)
            ):
                raise FatalAccounting("ink-map edge findings contain a malformed run")
            start, length = run
            # Ordered and disjoint, as `ink_runs` writes them and as
            # `edge_ink_from_runs` already requires of the same evidence. Two
            # runs that overlap would be counted twice here and could confirm
            # a pointer over ink that is not there.
            if start < previous_end or length <= 0 or start + length > width:
                raise FatalAccounting("ink-map edge findings have unordered or out-of-bounds runs")
            previous_end = start + length
            spans.append((max(x0, start), min(x1, start + length)))
        cuts = _merged_row_coverage(covered, y0 + offset, x0, x1)
        for start, end in spans:
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

    Consult §4.5's exact trigger: a native/derived box that reaches outside
    every region cut on its page (Unit 10C's own retained finding, narrowed to
    the live mask below, is condition 1) is a *pointer* only; recovery may be
    requested only where Unit 9's independently-measured ink map shows at least
    ``MINIMUM_INK_PIXELS`` in that outside part (condition 2). Agreement, IoU,
    delta magnitude, chair weight and two-chair disagreement never enter this
    function -- only the box's location, the page's cut regions, and the ink
    map's own pixel counts do.

    ``maps`` and ``cut_regions`` are each read once per run
    (``ink_map_by_page``, ``regions_by_source_page``) and passed in, the same
    way ``page_coverage_findings`` is -- not re-read per act. ``cut_regions``
    has no default: omitting the mask silently restores the pre-fix measure,
    which counts ink the Designator has already cut as evidence for cutting it
    again. A gate whose safe behaviour depends on a caller remembering an
    optional argument is not a gate.
    """
    evidence = maps.get(page_ordinal)
    if evidence is None:
        return []
    covered = cut_regions.get(page_ordinal, [])
    requests = []
    for observation in unclaimed_observations:
        bounds = observation.get("bounds") if isinstance(observation, dict) else None
        if not isinstance(bounds, dict):
            continue
        ink_pixels = _ink_outside_cuts_in_box(evidence, bounds, covered)
        if ink_pixels >= MINIMUM_INK_PIXELS:
            requests.append({"page_ordinal": page_ordinal, "outside_ink_pixels": ink_pixels})
    return requests


# The two reasons this stage has for spending a bounded fallback recrop, written
# into the request as data rather than left to be re-read out of its prose. The
# page-wide bound below counts requests by origin, and a bound that has to match
# a sentence is a bound one rewording removes.
COVERAGE_OBSERVATION_ORIGIN = "coverage-observation"
DECLARED_CROP_ORIGIN = "declared-incomplete-crop"
RECOVERY_ORIGINS = (COVERAGE_OBSERVATION_ORIGIN, DECLARED_CROP_ORIGIN)


def observation_funded_pages(context, acts: list[dict]) -> set[int]:
    """Pages whose one observation-funded recovery has already been spent.

    Consult base question 11 -- "one-observation-two-requests accounting:
    inherited from 10C -- confirm or change here" -- resolved as a change.

    An unclaimed observation is page-scoped by construction and deliberately so
    (``common/native_witness.py::unrouted_observations``: the denominator is
    every sealed proposal on the presented page, because scoping it to one act
    would produce eleven false findings per box on a page of twelve acts). The
    request it funds, however, is act-scoped: it draws on ONE act's single,
    unrecoverable chance to widen its crop (`pipeline/2_designator/run.py`: "a
    spent recovery budget is not recoverable"). Ungoverned, each act on the
    page evaluated that same page-scoped pointer independently and spent its
    own pool on it -- N bounded pools for one observation, which is not what
    GOVERNANCE 11 means by bounded, and which is the shape that produced the
    second `review` recovery round earlier passes of this unit argued over.

    One observation therefore funds at most one recovery request on its page,
    counted from the tree rather than from a per-run variable so the bound
    survives the next Recensor pass over the same run. The act that spends it
    is the first eligible act in the proposal seal's own order: the choice is
    made by the Designator's sealed act order and by budget state alone, and
    contains no quantity any witness reported -- no agreement, IoU, delta,
    chair weight or chair identity (consult §4.5's forbidden triggers).

    What the bound does NOT do is decide that the ink is accounted for. The
    observation stays retained on every act's review payload, and the pointer's
    own geometry never reaches the Designator -- the recovery rectangle is the
    act's declared one -- so the request is a bounded attempt at coverage, not
    a claim to have covered that ink. `HANDOFF.md` names the deeper fix (a
    request carrying the pointer's bounds, so the ink chooses the act) as
    Designator recovery geometry, outside this unit.
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
                f"recovery request {entry['artifact_id']} names no recorded origin; the "
                "one-observation-one-request bound cannot be counted from the tree"
            )
        ordinal = page_of.get(request.get("subject_id"))
        if ordinal is None:
            raise FatalAccounting(
                f"recovery request {entry['artifact_id']} names an act outside the "
                "proposal seal's expected set"
            )
        if origin == COVERAGE_OBSERVATION_ORIGIN:
            previous = funded.get(ordinal)
            if previous is not None:
                raise FatalAccounting(
                    f"page {ordinal} carries more than one observation-funded recovery "
                    f"request ({previous!r}, {entry['artifact_id']!r}); the page-wide "
                    "one-grant bound refuses the second request rather than collapsing "
                    "both into one counted page"
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
    raise FatalAccounting("a recovery request has neither a declared nor an ink-confirmed origin")


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
    """
    acts = expected_acts(context)
    residual_keys = {act["act_key"] for act in acts if act["act_key"].startswith("residual:")}
    findings: dict[int, dict] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "conservation":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "conservation", entry["artifact_id"])
        payload = _payload(record, f"Designator conservation {record['artifact_id']}")
        ordinal = payload.get("page_ordinal")
        measurable = payload.get("ink_measurable")
        components = payload.get("residual_components")
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
            or not isinstance(components, list)
            or ordinal in findings
        ):
            raise FatalAccounting("Designator conservation has malformed or duplicate page facts")
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


def uncovered_non_whitespace_ranges(text: str, covered: list[bool]) -> dict:
    """Losslessly compact uncovered non-whitespace offsets into half-open ranges."""
    ranges = []
    count = 0
    for index, char in enumerate(text):
        if covered[index] or char.isspace():
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

    The Perlector already refuses a role that one act's own sealed
    `page_ordinal` contradicts, but it reads one act at a time and so can only
    catch the two labels a single act disproves
    (`pipeline/4_perlector/run.py::act_attachment_view`, which says as much).
    `mixed` contradicts no single act: it claims the page holds a primary region
    AND a continuation, and only a stage holding every act on the page can tell
    whether that is true. So a resealed continuation-only page could wear
    `mixed` and pass every check downstream of the producer — the same hole as
    the `primary` forgery, one label along.

    The denominator here is the producer's own published attachment set, not a
    second walk of the Designator: an act contributes to page P exactly when its
    current attachment carries a page-witness row for P. Deriving it from the
    regions again would be a second spelling of the producer's grouping, free to
    drift from it — and the Perlector already binds each act's attachment pages
    to the regions it actually read, so the attachments cannot quietly claim a
    page the ink does not support.
    """
    primary_page = {act["act_id"]: act["page_ordinal"] for act in expected_acts(context)}
    contributors: dict[int, set[str]] = {}
    for act_id, attachment in attachments.items():
        rows = _payload(attachment, f"attachment for {act_id}").get("attachments")
        if not isinstance(rows, list):
            raise FatalAccounting(f"attachment for {act_id} has no rows")
        for row in rows:
            if not isinstance(row, dict) or not row.get("page_witness"):
                continue
            ordinal = row.get("page_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise FatalAccounting(
                    f"act {act_id} page-witness attachment carries no integer page ordinal"
                )
            if act_id not in primary_page:
                raise FatalAccounting(
                    f"act {act_id} carries a page-witness attachment but the proposal seal "
                    "names no such act"
                )
            contributors.setdefault(ordinal, set()).add(
                "primary" if primary_page[act_id] == ordinal else "continuation"
            )
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        roles = contributors.get(ordinal)
        if not roles:
            # No act's attachment reaches this page, so there is no denominator
            # to derive a role from and nothing here to refuse. That is not a
            # silent pass: the content-coverage loop below reads the same page
            # record and finds every character of it uncovered, which is a
            # visible shortfall routed to review.
            continue
        # `next(iter(...))`, never `set.pop()`: `roles` is the shared set held in
        # `contributors`, and popping it would empty the page's own denominator
        # for the second chair that reads it.
        expected = next(iter(roles)) if len(roles) == 1 else "mixed"
        if payload.get("page_role") != expected:
            raise FatalAccounting(
                f"page {ordinal}'s Testimonium for chair {chair!r} claims page_role "
                f"{payload.get('page_role')!r}; the acts attached to that page make it "
                f"{expected!r}"
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
    reconcile_page_roles(context, attachments, page_testimonia)
    # Read and validated once, not once per page witness: `expected_acts` re-reads
    # the proposal seal and re-verifies its self-hash on every call, and this stage
    # never writes to the Designator's seal while it runs.
    acts_by_page: dict[int, list[dict]] = {}
    for act in expected_acts(context):
        found_page = False
        for region in artifacts_for(context, DESIGNATOR, "region", act["act_id"]):
            payload = _payload(region, f"Designator region of {act['act_id']}")
            transform = payload.get("transform")
            if payload.get("origin") != "proposal" or not isinstance(transform, dict):
                continue
            ordinal = transform.get("source_page_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                raise FatalAccounting(f"Designator region of {act['act_id']} has no page ordinal")
            page_acts = acts_by_page.setdefault(ordinal, [])
            if act not in page_acts:
                page_acts.append(act)
            found_page = True
        # Unit-level consumers can supply only the proposal denominator, before
        # a Designator-region fixture exists. That is still one known primary
        # page, not an empty denominator; real continuation evidence always
        # takes the branch above.
        if not found_page:
            acts_by_page.setdefault(act["page_ordinal"], []).append(act)
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
                    or not row.get("attached")
                    # Indexed, not defaulted: `reconcile_page_roles` above has
                    # already proved every page-witness row carries an integer
                    # page ordinal. A default of `ordinal` read "absent means
                    # this page", so a row that lost its ordinal would have
                    # counted on EVERY page it was compared against.
                    or row["page_ordinal"] != ordinal
                ):
                    continue
                chair = row.get("chair")
                page_testimonium = page_testimonia.get((ordinal, chair))
                if page_testimonium is None or row.get("testimonium_ref") != context.artifact_ref(
                    ATTESTATORES, "page-testimonium", page_testimonium["artifact_id"]
                ):
                    raise FatalAccounting(
                        f"act {act['act_id']} attached page witness {chair!r} references no "
                        "current page Testimonium"
                    )
    findings: dict[int, dict] = {}
    for (ordinal, chair), record in page_testimonia.items():
        payload = _payload(record, f"page Testimonium {record['artifact_id']}")
        disagreement = payload.get("partition_disagreement")
        if disagreement is not None:
            unclaimed = (
                disagreement.get("unclaimed_observations")
                if isinstance(disagreement, dict)
                else None
            )
            if not isinstance(unclaimed, list):
                raise FatalAccounting(
                    "page Testimonium has no retained unclaimed-observation partition facts"
                )
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
                    "reading page Testimonium has no retained derived payload for content coverage"
                )
            # A page witness that read nothing across every act on this page --
            # every configured act was `dead`, `not-run`, or otherwise non-reading
            # for this chair -- carries no `reported` text: `testimonium_payload`'s
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
        for act in acts_by_page.get(ordinal, []):
            attachment = attachments.get(act["act_id"])
            if attachment is None:
                raise FatalAccounting(f"act {act['act_id']} has no attachment for content coverage")
            rows = _payload(attachment, f"attachment for {act['act_id']}").get("attachments")
            if not isinstance(rows, list):
                raise FatalAccounting(f"attachment for {act['act_id']} has no rows")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("chair") != chair
                    or not row.get("page_witness")
                    # Indexed, not defaulted: `reconcile_page_roles` above has
                    # already proved every page-witness row carries an integer
                    # page ordinal. A default of `ordinal` read "absent means
                    # this page", so a row that lost its ordinal would have
                    # counted on EVERY page it was compared against.
                    or row["page_ordinal"] != ordinal
                ):
                    continue
                if row.get("testimonium_ref") != context.artifact_ref(
                    ATTESTATORES, "page-testimonium", record["artifact_id"]
                ):
                    raise FatalAccounting("act attachment points to a different page Testimonium")
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
                    spans.append((span["start"], span["end"], act["act_id"]))
        covered = [False] * len(text)
        for start, end, _ in spans:
            if start < 0 or end < start or end > len(text):
                raise FatalAccounting("act attachment span lies outside its page Testimonium")
            for index in range(start, end):
                covered[index] = True
        uncovered = uncovered_non_whitespace_ranges(text, covered)
        finding = findings.setdefault(
            ordinal,
            {"by_chair": {}, "shortfall": False},
        )
        finding["by_chair"][chair] = {
            "attached_spans": [
                {"start": start, "end": end, "act_id": act_id}
                for start, end, act_id in sorted(spans)
            ],
            "uncovered_non_whitespace": uncovered,
        }
        finding["shortfall"] = finding["shortfall"] or bool(uncovered["count"])
    return findings


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
    # "supplied comparable page text", not "reported text": since Unit 14A a
    # page witness can report and be retained while its derived payload is
    # structured, which is testimony this instrument cannot measure but is not
    # an absence of testimony. Saying the chair reported nothing would put a
    # false statement in the record of a page that was in fact witnessed
    # (GOVERNANCE 2 and 10); the chair stays visible on its page Testimonium and
    # incomparable in its act's own witness floor.
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
    """Compose independent review findings without last-writer-wins routing.

    Unit 19C's cross-capture visibility finding comes first, ahead of GOALS 1's
    own page coverage cause, because it is act-surface evidence the reading
    itself could never see -- followed by R5b's reading-audit finding, then the
    witness floor. Every active reason is retained in that stable order; they
    all map to the same `held-for-review` outcome. `None` cross-capture
    visibility joins the two fields below in meaning "no measurement exists"
    and routing like `False`: no occlusion survey and no cross-capture
    registration were sealed, so nothing about this act's surface was
    measured, and an absent instrument is not a measured shortfall
    (`cross_capture_review_causes`, which is the only caller allowed to decide
    that). `None` testimony coverage
    means no page witness supplied comparable page text and routes like `False`:
    the act's own held or witness-floor cause already routes it, while an absent
    measurement is not itself a measured shortfall. `audit_unresolved` is
    wired to the Recensor's verified `audit_state` since the wave restacked R5b
    below this branch; `None` means no audit exists and routes like `False` by
    design, because absence of an audit is not an unresolved audit. `unreconciled`
    folds the scenario hold into the composer (R6 audit F-O5): it was the one
    preempted cause with no independent field, so an act simultaneously
    under-witnessed and scenario-held recorded only the floor cause.
    """
    # Review routing is the stage's closed decision payload.  Screen it before
    # any reason is assembled so a future consensus/vote field cannot become a
    # witness selector under review vocabulary.
    refuse_preference(
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
    refuse_preference(payload, what="a Recensor review")
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

    page_findings = page_coverage_findings(context)
    geometry_inputs = geometry_coverage_inputs(context)
    content_findings = testimony_content_findings(context)
    ink_maps = ink_map_by_page(context)
    # §4.3's mask, read once: every region currently cut on each page, proposal
    # and recovery together. `page_coverage_findings` above measures the page's
    # own residual ink against exactly this set; the §4.5 pointer gate measures
    # one witness box against it.
    cut_regions = regions_by_source_page(context)
    # Counted from the tree, once, before any act is reviewed: one unclaimed
    # observation funds at most one recovery request on its page, and the bound
    # has to survive the Recensor pass that follows a recrop (consult base
    # question 11, resolved in `observation_funded_pages`).
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
        # Unit 19C: the act-surface visibility survey, read from this act's
        # own current Perlectio (`None` exactly when nothing was ever shown
        # real capture pixels -- see `act_cross_capture_coverage`). Every act
        # today resolves to exactly one required capture (19B's
        # `logical_reading.py` refuses to read a genuinely clustered
        # partition at all) and no Designator run seals an occlusion survey,
        # so every real survey today is `unresolved` for a named,
        # instrument-absent reason and routes like `False`
        # (`cross_capture_review_causes`).
        cross_coverage = act_cross_capture_coverage(context, act_id, latest_payload)
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
        # A witness's own unclaimed geometry is a pointer, never the evidence
        # (consult §4.5): it may ask for bounded recovery only where Unit 9's
        # independently-measured ink map confirms real ink under that pointer.
        # Without this check, an Attestator's mis-reported box alone could
        # spend a real recovery budget or hold an act on zero actual ink --
        # exactly the witness-preference GOVERNANCE 3 forbids.
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
        declared_recovery_requested = act_key in scenario["recover_acts"]
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
            # Consult §4.2 rule 3 and this slice's own charge: an ink-confirmed
            # recovery is gated by the §4.5 conjuncts and by nothing else, and
            # union geometry is not one of them. Round 2 of this slice added
            # `and not cross_capture_occluded_everywhere` here, which reads
            # the right rule backwards: §7.20 forbids occlusion *funding* a
            # reroll -- it never does, `wants_recovery` is a declared recrop
            # or Unit 9's own measured ink -- while that conjunct let union
            # geometry *veto* a recovery Unit 14B had already funded from ink
            # it measured in the page's actual pixels. The occlusion survey is
            # taken over the act's own sealed proposal footprint; unclaimed
            # ink lies outside every current cut, so the survey has no
            # evidence about the surface the recrop would go and get. The act
            # still holds on `occluded-everywhere` through `findings_route`
            # below, and the ink still gets recovered.
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
                declared=declared_recovery_requested,
                outside_ink_requests=outside_ink_requests,
            )
            if request_origin == COVERAGE_OBSERVATION_ORIGIN:
                # Unit 19C's capture-specific gate, called for real rather
                # than left with zero production callers. Every boolean below
                # is the exact fact that already put this request on the
                # ink-confirmed route above, so admission cannot disagree
                # while this ad hoc gate and that function's own three
                # conjuncts stay in step; a future edit to either that drifts
                # from the other fails loudly here instead of silently
                # spending or refusing a recovery on a stale rule.
                dossier = latest_payload.get("dossier")
                gate = capture_specific_recovery(
                    logical_act_id=(
                        dossier["logical_act_id"]
                        if isinstance(dossier, dict) and "logical_act_id" in dossier
                        else act_id
                    ),
                    source_sha256=_source_rows(context.run)[act["page_ordinal"]]["sha256"],
                    page_ordinal=act["page_ordinal"],
                    # True, not re-tested: `recovery_request_origin` only ever
                    # returns `COVERAGE_OBSERVATION_ORIGIN` when
                    # `outside_ink_requests` is already non-empty, so a second
                    # copy of that live boolean expression would only defeat
                    # `test_a_bypass_of_ink_confirmation_is_caught_by_this_files_own_guard`'s
                    # single-occurrence pin on it, not add a real check.
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
            request = context.publish(
                kind="recovery-request",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recover", ordinal),
                inputs=[reading_ref],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "recovery_kind": FALLBACK_RECROP,
                    # The origin as data beside the sentence that states it, so
                    # the page-wide bound counts a recorded fact rather than
                    # re-reading prose (`observation_funded_pages`).
                    "origin": request_origin,
                    "reason": (
                        "the crop may be incomplete; an expanded recrop is requested"
                        if request_origin == DECLARED_CROP_ORIGIN
                        else "a page witness reported ink outside every sealed proposal; an "
                        "expanded recrop is requested"
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
                },
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
                # an audit that resolved cleanly must be tellable from one
                # never checked. R8's canonical export reads uncertainty spans
                # whose review-side "why" lives exactly here.
                "audit_unresolved": audit_unresolved,
                "cross_capture_coverage": cross_coverage,
                # Present only on a `confirmed-blank`, because it is the evidence
                # that outcome rests on and nothing else has any. Every other
                # review carries the fields above and no more.
                **({"blank_evidence": blank_evidence} if blank_evidence is not None else {}),
            },
        )

    context.seal_boundary()
    context.finish()
    # After `finish()`, so the manifest the receipt checks against disk is the
    # one this pass just wrote rather than the previous pass's.
    write_partition_receipt(context, budget)
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
