"""Page witnesses as every consumer reads them: the sealed roster's page-scoped
chairs, and each page's current, fully validated `page-testimonium` per chair.

The Attestatores, the Perlector and the Recensor each read the page witnesses
from their own run authority through these helpers, so the three never derive a
different roster or accept a page Testimonium another would refuse. Nothing here
trusts an upstream stage's check: every record is validated where it is read.

This module is the name stages import these from, the roster reader
(`declared_page_witness_chairs`) and its check (`require_page_roster`)
included, which live beside the page feed in `common.page_path`.
`common.stage` alone reads them from `common.page_path`, since this module
reads `common.stage`.
"""

from __future__ import annotations

from typing import Any

from common.chairs.models import AbsentChair, ChairIdentity
from common.chandra_native_retry import validate_trace as validate_chandra_trace
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR
from common.exemplar_boundary import read_sealed_page, verify_exemplar_crop_lineage
from common.imaging import dimensions
from common.native_witness import (
    record_presentations,
    unpresented_region_ids,
    validate_capture_text_view,
    validate_native_capture,
    validate_native_witness_geometry,
    validate_page_testimonium_payload,
    validate_presented_page_binding,
    verify_native_capture_blob,
)
from common.page_path import (
    PAGE_TESTIMONIUM_KIND,
    distinct_refs,
    empty_detector_page,
    refs_by_path,
)
from common.page_path import declared_page_witness_chairs as declared_page_witness_chairs
from common.page_path import require_page_roster as require_page_roster
from common.page_witness_units import UNIT_KINDS
from common.stage import (
    ATTEMPTED_WITNESS_OUTCOMES,
    latest_per_chair,
    recovery_region_count,
    stage_manifest,
    validate_serving_provenance,
)
from common.witness_regime import NAMED, witness_label

# --- inputs -------------------------------------------------------------------------


def input_order(reference: dict[str, str]) -> tuple[str, str]:
    return reference["relative_path"], reference["sha256"]


# --- the Designator's proposals a page Testimonium is measured against -------------


def verify_region(context, region: dict) -> dict:
    """Prove the region handed over is the region the reference describes.

    The digest catches changed bytes, decoding catches a non-image, and the dimensions
    catch a crop that does not match its transform. The cause goes into the refusal
    text because `run_stage` prints only the refusal; these messages name
    ordinals and run-relative paths, never a submitted filename.
    """
    try:
        return verify_exemplar_crop_lineage(context.tree, context.run, region)
    except ContractError as error:
        raise SchemaRefusal(
            f"a Designator region does not trace to its Exemplar page: {error}"
        ) from error


def sealed_proposal_regions(context) -> list[dict]:
    """Every verified proposal in the run-wide routing denominator."""
    regions = []
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        # Keep the origin-specific refusal ahead of the general lineage refusal;
        # callers rely on the shared recovery-denominator vocabulary.
        recovery_region_count(record.get("subject_id", "unidentified act"), [record])
        validate_serving_provenance(
            context,
            record.get("payload", {}).get("provenance"),
            producer_stage=DESIGNATOR,
            require_receipt=True,
        )
        verify_region(context, record)
        if record["payload"]["origin"] == "proposal":
            regions.append(record)
    return regions


# --- one page Testimonium -------------------------------------------------------------


def validate_presented_page(context, payload: dict, presentations: list[dict]) -> None:
    """Bind a witness's presentations and observed geometry to their one sealed Exemplar page.

    The page is read and sized once; every presentation must name it.
    """
    if not presentations:
        raise SchemaRefusal("an attempted Testimonium names no presentation")
    page_id = presentations[0].get("source_page_id")
    if any(shown.get("source_page_id") != page_id for shown in presentations):
        raise SchemaRefusal("a Testimonium's presentations name more than one sealed page")
    page, page_bytes = read_sealed_page(context.tree, page_id)
    page_size = dimensions(page_bytes)
    validate_native_witness_geometry(payload, page_size=page_size)
    for presented in presentations:
        validate_presented_page_binding(
            presented,
            page_ordinal=page["payload"]["ordinal"],
            page_image_path=page["payload"]["image_path"],
            page_sha256=page["payload"]["source_sha256"],
            page_size=page_size,
            page_bytes=page_bytes,
        )


def reads_detector_records(context, chair: Any) -> bool:
    """Whether the sealed configuration's chair reads its page one detector record at a time."""
    identity = context.registry.config.chairs.get(chair) if isinstance(chair, str) else None
    return (
        isinstance(identity, ChairIdentity)
        and identity.witness_scope == "page"
        and UNIT_KINDS.get(identity.witness_adapter) == "detector-record"
    )


def is_detector_blank_testimony(context, record: dict[str, Any]) -> bool:
    """Whether a page Testimonium is a record reader's testimony that its page holds nothing.

    A chair that reads its page one detector record at a time is shown nothing
    on a page its detector found no record on. When the detector stopped below
    its cap, it looked and saw nothing: the chair's page record is
    `genuinely-empty` with no presentation, and binds the detector's census.
    """
    payload = record.get("payload")
    return (
        record.get("outcome") == "genuinely-empty"
        and isinstance(payload, dict)
        and payload.get("presented") == {}
        and reads_detector_records(context, payload.get("chair"))
    )


def chair_was_served(context, record: dict[str, Any]) -> bool:
    """Whether a page Testimonium's outcome says its chair was served, so it names a receipt.

    Blank testimony is `genuinely-empty` without a serving moment: the
    detector's census, not the chair, found the page empty.
    """
    return record.get("outcome") in ATTEMPTED_WITNESS_OUTCOMES and not (
        is_detector_blank_testimony(context, record)
    )


def validate_page_testimonium_record(
    context,
    record: dict[str, Any],
    proposal_regions: list[dict[str, Any]],
) -> None:
    """Reconcile a page Testimonium's outcome, page, presentation, and inputs."""
    payload = record.get("payload")
    validate_page_testimonium_payload(
        payload,
        testimonium_id=record.get("artifact_id"),
        read_bytes=context.tree.read_bytes,
    )
    blank_testimony = is_detector_blank_testimony(context, record)
    attempted = chair_was_served(context, record)
    presented = payload["presented"]
    if payload["regions"] != []:
        raise SchemaRefusal(
            "a page Testimonium carries act-region references. Its page evidence would acquire "
            "an act identity the page record does not own. Keep act associations in the "
            "digest-bound attachments"
        )
    if blank_testimony:
        census = empty_detector_page(
            context, stage_manifest(context, DESIGNATOR)["artifacts"], record["subject_id"]
        )
        if census is None or payload.get("payload") != "":
            raise SchemaRefusal(
                "a record reader's page Testimonium says the page holds nothing for it, but its "
                "text is not empty or its record detector's census for the page is not one of no "
                "record below a stated cap. Only such a census is blank testimony; any other page "
                "is read record by record or not at all"
            )
        if record.get("inputs") != [census]:
            raise SchemaRefusal(
                "a record reader's blank page Testimonium does not bind exactly its record "
                "detector's census for the page, the one input its testimony rests on"
            )
    if not attempted:
        # As in the act-scoped check: before the image-evidence refusal, which a
        # stripped record that kept its response would pass.
        if payload.get("native_capture") is not None or payload.get("raw_response_refs"):
            raise SchemaRefusal(
                "a non-attempted page Testimonium retains a provider response. The record would "
                "say the chair was not served while naming the bytes it answered with, outside "
                "its own input set. Record the attempted outcome that produced the response, or "
                "remove the retained capture"
            )
        if (
            presented != {}
            or payload["observed"] != []
            or (record.get("inputs") != [] and not blank_testimony)
        ):
            raise SchemaRefusal(
                "a non-attempted page Testimonium carries image evidence. The record would say "
                "a chair saw pixels when its outcome says it was not served. Remove the image "
                "evidence or record the attempted outcome that actually occurred"
            )
    else:
        if presented == {}:
            raise SchemaRefusal(
                "an attempted page Testimonium has no image presentation. Its outcome cannot be "
                "traced to pixels the chair received. Retain the exact presentation before "
                "publishing the attempted record"
            )
        presentations = record_presentations(payload)
        if any(
            shown["source_page_id"] != record["subject_id"]
            or shown["source_page_ordinal"] != payload["page_ordinal"]
            for shown in presentations
        ):
            raise SchemaRefusal(
                "wrong page Testimonium: its presentation names a different page than its "
                "record. Its observations would be attributed to the wrong sealed ink. Restore "
                "the page identity and ordinal of the presentation actually served"
            )
        validate_presented_page(context, payload, presentations)
        expected_inputs = [
            {"relative_path": shown["image_path"], "sha256": shown["image_sha256"]}
            for shown in presentations
        ]
        # Each retained response is bound beside the presented pixels, so an ordinary
        # artifact read re-hashes it instead of trusting a nested reference.
        retained = list(payload.get("raw_response_refs", []))
        capture = payload.get("native_capture")
        if capture is not None:
            retained.append(capture["raw_response_ref"])
        native_inference = payload.get("native_inference")
        if native_inference is not None:
            for row in validate_chandra_trace(native_inference)["attempts"]:
                retained.extend((row["intent_ref"], row["attempt_ref"]))
        retained.extend(
            reference for reference in payload.get("unit_call_refs", []) if reference is not None
        )
        # De-duplicated as the producer does: one response can reach the same blob
        # through both `raw_response_refs` and `native_capture`, and
        # `validate_input_refs` refuses a repeated path, so a doubled expectation could
        # never be met.
        expected_inputs = refs_by_path(distinct_refs(expected_inputs + retained))
        if record.get("inputs") != expected_inputs:
            raise SchemaRefusal(
                "a page Testimonium does not bind exactly its presented image"
                + (" and every retained raw response" if retained else "")
                + ". The consumer cannot prove which immutable pixels produced the page "
                "report. Restore the digest-bound inputs and remove unrelated ones"
            )
    page_proposals = [
        region
        for region in proposal_regions
        if region["payload"]["transform"]["source_page_id"] == record["subject_id"]
    ]
    if payload["unpresented_regions"] != unpresented_region_ids(
        record_presentations(payload), page_proposals
    ):
        raise SchemaRefusal(
            "a page Testimonium does not name exactly the proposal regions outside its "
            "presentation. Its derived layer would look more complete than the pixels shown. "
            "Re-derive unpresented_regions from the sealed page proposals"
        )
    validate_serving_provenance(
        context,
        payload["provenance"],
        producer_stage=ATTESTATORES,
        require_receipt=attempted,
    )


def verify_page_native_capture(
    context, subject: str, chair: str, testimonium: dict, native_capture: dict
) -> None:
    """The retained native capture is bound, attributed to the chair's adapter and intact.

    A capture read under a text view this build does not read is refused as
    that, before its bytes are read.
    """
    if native_capture["raw_response_ref"] not in testimonium.get("inputs", []):
        raise SchemaRefusal(
            f"{subject} page Testimonium for chair {chair!r} does not bind its "
            "retained raw response as a verified input"
        )
    resolved = context.registry.resolve(chair)
    if isinstance(resolved, AbsentChair):
        raise SchemaRefusal(
            f"{subject} page Testimonium for chair {chair!r} carries a native capture while "
            "the roster records that chair as absent; an absent chair has no adapter "
            "boundary to attribute it to"
        )
    if native_capture["adapter"] != resolved.witness_adapter:
        raise SchemaRefusal(
            f"{subject} page Testimonium for chair {chair!r} attributes its "
            "native capture to an adapter other than that chair's configured boundary"
        )
    validate_capture_text_view(validate_native_capture(native_capture))
    verify_native_capture_blob(context.tree, native_capture)


# --- every page ------------------------------------------------------------------------


def current_page_testimonia(context, proposal_regions: list[dict]) -> dict[str, list[dict]]:
    """Every page's current page Testimonium per chair, by page id, each fully validated.

    Every record is validated, its native capture included, but only each
    chair's latest attempt is current. A page no witness testified to is absent.
    """
    by_page: dict[str, list[dict[str, Any]]] = {}
    for entry in stage_manifest(context, ATTESTATORES)["artifacts"]:
        if entry["kind"] != PAGE_TESTIMONIUM_KIND:
            continue
        record = context.tree.read_artifact(
            ATTESTATORES, PAGE_TESTIMONIUM_KIND, entry["artifact_id"]
        )
        validate_page_testimonium_record(context, record, proposal_regions)
        capture = record["payload"].get("native_capture")
        if capture is not None:
            verify_page_native_capture(
                context,
                f"page {record['subject_id']}",
                record["payload"]["chair"],
                record,
                capture,
            )
        by_page.setdefault(record["subject_id"], []).append(record)
    return {
        page_id: latest_per_chair(records, f"page Testimonium for page {page_id}")
        for page_id, records in by_page.items()
    }


# --- the witnesses a page reading was shown ------------------------------------------


def shown_page_witnesses(
    context, reading: dict[str, Any], current: list[dict[str, Any]], what: str
) -> list[dict[str, Any]]:
    """The page witnesses a page reading was shown, each its chair's current page Testimonium.

    `current` is the reading's page's entry in `current_page_testimonia`: each
    chair's latest page Testimonium, every one validated. The feed
    the reading inputs lists every witness it showed; each row must name one of
    the current ones, under the label this run's regime gives that chair (a
    blinded feed names no chair, so the Testimonium is found by its reference).
    A feed that showed no witness made the reading a Lectio nuda, which is never
    established. The reading's dissent compares against exactly the letters the
    feed showed, once each. Returns `{letter, witness_label, chair, testimonium,
    testimonium_ref}` per shown witness, in letter order.
    """
    payload = reading.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    page_id = payload.get("page_id")
    feed_ref = payload.get("feed_ref")
    if feed_ref not in reading.get("inputs", []):
        raise FatalAccounting(f"{what} does not input the page feed it was read from")
    feed = context.tree.read_artifact_reference(
        feed_ref, stage=PERLECTOR, kind="page-feed", subject_id=page_id
    )["payload"]
    regime = context.witness_context
    if feed.get("witness_regime") != regime:
        raise FatalAccounting(
            f"{what} was read from a feed under witness regime {feed.get('witness_regime')!r}, "
            f"not this run's {regime!r}"
        )
    rows = feed.get("witnesses")
    if not isinstance(rows, list) or not rows:
        raise FatalAccounting(
            f"{what} was shown no page witness: a reading shown no witness is a Lectio nuda, "
            "an instrument record, never an establishing read"
        )
    by_reference = {}
    for record in current:
        reference = context.artifact_ref(ATTESTATORES, PAGE_TESTIMONIUM_KIND, record["artifact_id"])
        by_reference[input_order(reference)] = (reference, record)
    shown: list[dict[str, Any]] = []
    for row in rows:
        reference = row.get("testimonium_ref") if isinstance(row, dict) else None
        found = (
            by_reference.get((reference.get("relative_path"), reference.get("sha256")))
            if isinstance(reference, dict)
            else None
        )
        if found is None or found[0] != reference:
            raise FatalAccounting(
                f"{what} was shown a witness from a Testimonium that is not its chair's current "
                "page Testimonium; nothing is established over superseded testimony"
            )
        record = found[1]
        chair = record["payload"].get("chair")
        if not isinstance(chair, str) or not chair:
            raise FatalAccounting(f"{what} was shown a page Testimonium that names no chair")
        label = witness_label(
            chair, regime=regime, run_id=context.tree.run_id, config_digest=context.config_digest
        )
        if row.get("witness_label") != label or row.get("chair") != (
            chair if regime == NAMED else None
        ):
            raise FatalAccounting(
                f"{what} was shown witness {row.get('witness_label')!r}, which is not the label "
                "this run's regime gives the chair whose Testimonium it names"
            )
        shown.append(
            {
                "letter": row.get("letter"),
                "witness_label": label,
                "chair": chair,
                "testimonium": record,
                "testimonium_ref": reference,
            }
        )
    letters = [witness["letter"] for witness in shown]
    labels = {witness["letter"]: witness["witness_label"] for witness in shown}
    if len(set(letters)) != len(letters) or len({w["chair"] for w in shown}) != len(shown):
        raise FatalAccounting(f"{what} was shown one witness letter or chair twice")
    dissent = payload.get("dissent")
    if (
        not isinstance(dissent, list)
        or not all(isinstance(row, dict) for row in dissent)
        or not all(isinstance(row.get("letter"), str) for row in dissent)
        or sorted(row.get("letter") for row in dissent) != sorted(letters)
        or any(labels[row["letter"]] != row.get("witness_label") for row in dissent)
    ):
        raise FatalAccounting(
            f"{what} does not record its dissent against exactly the witnesses its feed showed, "
            "once each"
        )
    return sorted(shown, key=lambda witness: witness["letter"])
