"""Recensor: establishes that the text is complete. It establishes no text.

Every unit a page-read run counts gets exactly one review (`page_review.py`):
its witness floor, the residual ink on its page and, for a page said to hold no
act, whether that is confirmed. Every page break a reading flags becomes a
`continuation-link`. The stage asks for no recovery and never touches a
reading; a unit that cannot be accepted is held for review with every reason
named.

**It does not select among witnesses.** Witness outcomes form a coverage record
that can mark a unit under-witnessed and the run visibly partial.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import page_review  # noqa: E402

from common.background import (  # noqa: E402
    BackgroundInferenceRefusal,
    load_background_config,
    resolve_background_policy,
)
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import is_plain_int  # noqa: E402
from common.contracts.errors import (  # noqa: E402
    ContractError,
    FatalAccounting,
    IncompatibleReuse,
)
from common.contracts.identities import act_id as derive_act_id  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import DESIGNATOR, EXEMPLAR, RECENSOR  # noqa: E402
from common.corpus_register import refuse_capture_preference  # noqa: E402
from common.exemplar_boundary import sealed_page_bytes, verify_sealed_page_pixels  # noqa: E402
from common.imaging import grayscale_rows  # noqa: E402
from common.perlector_audit import (  # noqa: E402
    EXAMINATION_CAP_EXHAUSTED,
    EXAMINATION_INCOMPLETE,
    unresolved_state,
    validate_chain,
)
from common.perlector_failure import validate_failed_perlectio  # noqa: E402
from common.residual_ink import (  # noqa: E402
    INK_NOT_MEASURABLE,
    load_coverage_audit_config,
    residual_ink,
    resolve_coverage_audit_policy,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    READING_UNIT_PAGE,
    RESIDUAL_ENUMERATION_AGGREGATED,
    RESIDUAL_ENUMERATION_COMPLETE,
    RESIDUAL_ENUMERATIONS,
    RETIRED_RESIDUAL_ENUMERATION,
    expected_acts,
    latest_attempt,
    open_stage_context,
    page_residual_act_key,
    reading_denominator,
    run_stage,
    sealed_decoding_policy,
    sealed_residual_presentation_policy,
    stage_manifest,
    stage_parser,
)

DESCRIPTION = "Recensor: establishes that the text is complete. It establishes no text."


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
    # An ordinary failed reading is still audited; only operational failure skips the chain.
    if reading["outcome"] == "failed" and "failure" in reading["payload"]:
        validate_failed_perlectio(context, reading, act_id, expected_act_key=expected_act_key)
        return None
    chain = validate_chain(
        context.tree, reading, act_id, decoding_policy=sealed_decoding_policy(context)[0]
    )
    return {
        "unresolved": chain["record"]["unresolved"],
        "examination": chain["record"]["examination"],
        "reproof_truncation": chain["finding"]["payload"]["reproof_truncation"],
    }


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
        if not is_plain_int(ordinal):
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
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                f"Exemplar page {record.get('artifact_id')} carries no integer ordinal"
            )
        if ordinal in pages:
            raise FatalAccounting(
                f"the Exemplar carries more than one sealed page for ordinal {ordinal}; "
                "the Recensor has no rule for selecting one page image"
            )
        pages[ordinal] = record

    # Settle the structural denominator before reading pixels.
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


def page_coverage_findings(context, *, regions: dict[int, list[dict]]) -> dict[int, dict]:
    """Residual-ink findings for every page in `regions`, once per run.

    `regions` maps a sealed page ordinal to the bounds counted as covering its
    ink: every reading region cut on it, and every sealed page, one with no
    region included. The input is the page image itself, never the proposal
    set, a witness or a reading.
    The paper value is the Designator's shared inference under the sealed background
    policy, never the page's own histogram mode, which on a photographed opening is the
    bezel and hides all residual ink. A page whose paper the inference refuses gets a
    finding carrying the refusal and no counts.
    """
    if not regions:
        return {}
    background_config = load_background_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", background_config["config_sha256"])
    # Set aside the page-spanning component already accounted for by Designator.
    coverage_config = load_coverage_audit_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", coverage_config["config_sha256"])
    pages = sealed_page_images(context)
    findings: dict[int, dict] = {}
    for ordinal, bounds in regions.items():
        page = pages.get(ordinal)
        if page is None:
            raise FatalAccounting(
                f"a region names source page {ordinal}, which the Exemplar did not seal; "
                "a crop of unsealed pixels is invariant #10's imbalance"
            )
        # Digest the bytes actually measured, not an earlier read.
        image_bytes = sealed_page_bytes(
            context.tree, page, what="the residual-ink check", refusal=FatalAccounting
        )
        width, height, rows = grayscale_rows(image_bytes)
        try:
            findings[ordinal] = {
                **residual_ink(
                    width,
                    height,
                    rows,
                    bounds,
                    background_policy=resolve_background_policy(background_config, width, height),
                    coverage_policy=resolve_coverage_audit_policy(coverage_config, width, height),
                ),
                "background_config_sha256": background_config["config_sha256"],
            }
        except BackgroundInferenceRefusal as error:
            # Without a paper value zero ink would be a false clean page.
            findings[ordinal] = {
                "ink_measurable": False,
                "named_finding": INK_NOT_MEASURABLE,
                "background_refusal": str(error),
                "background_config_sha256": background_config["config_sha256"],
            }
    return findings


_BOX_SIDES = ("x", "y", "w", "h")


def _int_box(bounds) -> bool:
    """Exactly the keys x, y, w, h, each a plain int."""
    return (
        isinstance(bounds, dict)
        and set(bounds) == set(_BOX_SIDES)
        and all(is_plain_int(bounds[side]) for side in _BOX_SIDES)
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


SURVEY_ABSENT = "act-visibility-survey-absent"


REGISTRATION_ABSENT = "cross-capture-registration-absent"


# A view spanning pages has no single grid; record absence, not measurement.
SURVEY_SPANS_TWO_PAGES = "act-visibility-survey-spans-two-pages"


# An absent instrument is recorded but does not become a measured shortfall.
INSTRUMENT_ABSENT_CODES = frozenset({SURVEY_ABSENT, REGISTRATION_ABSENT, SURVEY_SPANS_TWO_PAGES})


def _payload(record: dict, what: str) -> dict:
    """One record payload, refused unless it is an object."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise FatalAccounting(f"{what} has no object payload")
    return payload


def _require_reconciled_pixels(ordinal: int, pixel_counts: dict) -> tuple[int, int, int]:
    """Pixel-count typing and `claimed + residual == total`, shared by every page shape."""
    if any(not is_plain_int(count) or count < 0 for count in pixel_counts.values()):
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

    An aggregate page retains promoted and below-threshold components. Its promoted
    acts and single page hold are checked against that retained partition.
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
        if not is_plain_int(ordinal) or not isinstance(measurable, bool) or ordinal in findings:
            raise FatalAccounting("Designator conservation has malformed or duplicate page facts")
        if enumeration == RETIRED_RESIDUAL_ENUMERATION:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} was sealed under {enumeration}, "
                "which this build no longer reads; re-run"
            )
        if enumeration not in RESIDUAL_ENUMERATIONS:
            raise FatalAccounting(
                f"Designator conservation page {ordinal} records its residual enumeration as "
                f"{enumeration!r}, which is outside the closed set {RESIDUAL_ENUMERATIONS}; "
                "this stage cannot tell a page with no unclaimed ink from one whose unclaimed "
                "ink was counted and not listed without being told which it is"
            )
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
        if not is_plain_int(declared_count) or declared_count != len(components):
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
            if not _int_box(bounds) or not is_plain_int(pixel_count) or pixel_count < 0:
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


def _validate_aggregate_components(
    ordinal: int,
    promoted: list,
    aggregate: list,
    width: int,
    height: int,
    policy: dict,
) -> None:
    identities = set()
    for label, components in (("promoted", promoted), ("aggregate", aggregate)):
        for index, component in enumerate(components):
            bounds = component.get("bounds") if isinstance(component, dict) else None
            pixels = component.get("pixel_count") if isinstance(component, dict) else None
            if (
                not _page_rect(bounds)
                or bounds["x"] + bounds["w"] > width
                or bounds["y"] + bounds["h"] > height
                or not is_plain_int(pixels)
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
    if any(not is_plain_int(value) or value <= 0 for value in (width, height)):
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} has no positive page geometry"
        )
    policy = sealed_residual_presentation_policy(context)
    if any(
        not is_plain_int(payload.get(name)) or payload.get(name) != value
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
    if any(
        not is_plain_int(value) or value < 0 for value in declared_counts
    ) or declared_counts != (
        len(promoted),
        len(aggregate),
        len(promoted) + len(aggregate),
    ):
        raise FatalAccounting(
            f"aggregate Designator conservation page {ordinal} does not reconcile its combined "
            "component counts"
        )
    _validate_aggregate_components(ordinal, promoted, aggregate, width, height, policy)
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
        "page_residual_act_count": held_as_one,
        "reason": (
            f"{len(promoted)} significant residual components remain individual held acts; "
            f"{len(aggregate)} below-threshold components remain retained on one page hold"
        ),
    }


NO_PAGE_CONSERVATION = {
    "ink_measurable": None,
    "residual_component_count": None,
    "residual_act_count": None,
    "residual_enumeration": None,
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
    # A shape guard over the route inputs; `publish_review` screens the payload.
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
    check,
) -> dict:
    """Write a review only after rejecting witness-selection vocabulary.

    A review's content can change between passes without the act recovering, because
    page-wide facts come from every act on its page. So the prior review's ordinal is
    tried first (unchanged content reuses byte for byte), and a fresh ordinal is minted
    only when the store proves the content differs. The ordinal is stamped here, never
    trusted from the caller.

    The whole payload is screened, not only the route inputs: a review is a second
    durable record, and a future direct payload field would otherwise go unchecked.
    `check` validates the payload's own closed shape.
    """
    refuse_capture_preference(payload, what="a Recensor review")
    check(subject_id, payload)

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


def review_a_page_read_run(context, denominator: dict) -> int:
    """The page path (`page_review.py`): every counted unit reviewed, then the v3 receipt."""
    held = page_review.review_pages(
        context,
        denominator,
        page_coverage_findings=page_coverage_findings,
        publish_review=publish_review,
        current_review=current_review,
    )
    # The receipt needs the current manifest and may refuse before the seal.
    context.finish()
    page_review.write_reading_receipt(context, page_coverage_findings=page_coverage_findings)
    context.seal_boundary()
    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(DESCRIPTION).parse_args()
    context = open_stage_context(args, RECENSOR, registry_factory=registry_factory)
    denominator = reading_denominator(context)
    if denominator["reading_unit"] != READING_UNIT_PAGE:
        raise FatalAccounting(
            f"this run reads by {denominator['reading_unit']!r}; the Recensor reviews only "
            "page-read runs"
        )
    return review_a_page_read_run(context, denominator)


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
