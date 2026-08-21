"""Perlector: reads the ink, with the testimonia as fallible clues.

The fake here proves wiring and nothing else — its text comes from the fixture, so
it demonstrates exactly zero about reading. What it *does* prove is the shape of
the record, and the shape is where GOVERNANCE 3 either holds or quietly fails:

  It verifies the region evidence.        The stage reads the bytes, checks their
                                          digest against the sealed reference, and
                                          decodes them to confirm the image is the
                                          size the transform claims. The fixture
                                          reader observes pixels only to prove a
                                          page-fallback act empty; ordinary text
                                          remains declared fixture text.
  It records its basis.                   The region it read, and every testimonium
                                          it saw, by reference.
  It never counts witnesses.              No branch anywhere in this file reads how
                                          many chairs agreed. The dissent record is
                                          computed *after* the reading is fixed,
                                          and cannot reach back into it.

Dissent is structural, not evaluative: it records where the reading departed from
each witness, which makes parroting measurable without new instrumentation. It is
not a quality signal — most lines in a register are easy and every witness agrees,
and zero dissent there is the correct output.

    python pipeline/4_perlector/run.py --run-root <dir> --run-id <id>
"""

import copy
import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import annotations  # noqa: E402
import audit  # noqa: E402
import dossier as dossier_module  # noqa: E402
import nuda  # noqa: E402
import prompts  # noqa: E402
import protocol  # noqa: E402
import regime  # noqa: E402
import truncation  # noqa: E402
from dissent import departures, dissent_against, validate_dissent  # noqa: E402
from reader import FixtureReader, validate_audit_delivery  # noqa: E402

from common.alignment import markup_text_view  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_of  # noqa: E402
from common.contracts.envelope import validate_input_refs  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id, perlector_attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    PERLECTOR_CHAIR,
    WITNESS_CONTEXT_REGIMES,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    fixture_serving_details,
    latest_attempt,
    latest_per_chair,
    open_context,
    reading_basis_regions,
    recovery_region_count,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)


def regions_of(context, act_id: str) -> list[dict]:
    """Every Designator region for this act, its own provenance verified first.

    `pipeline/3_attestatores/run.py::proposed_regions` already validates the
    identical artifact kind before showing a region to a witness; reading it here
    unvalidated would let a tampered Designator provenance reach a real reading
    while the equivalent tamper on a Testimonium is refused. A region always
    carries a receipt-backed provenance -- `structure_provenance` refuses before
    any region is cut if the Designator chair is absent or unverifiable -- so
    every region validated here requires one.
    """
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
            records.append(record)
    return sorted(records, key=_region_ordinal)


def _region_ordinal(record: dict) -> int:
    """The sort key, refused by name rather than escaping as a raw `KeyError`.

    Ordering happens before `verify_region` validates the region, so this is
    the one place a resealed record whose payload lost `attempt_ordinal` is
    read — and a stage boundary that answers untrusted input with a traceback
    is the one thing the Designator's own tests assert never appears in stderr.
    """
    ordinal = record.get("payload", {}).get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal("a Designator region carries no integer attempt ordinal to order by")
    return ordinal


def act_regions(context, act_id: str) -> tuple[list[dict], list[dict]]:
    """Every region for this act, and the original-proposal subset of it.

    One spelling, because the preflight and the reading loop both need the pair
    and both refuse the same way. A second copy of the filter is how the two
    come to disagree about what counts as a proposal region, which is the fact
    the witness-coverage record is built on.
    """
    regions = regions_of(context, act_id)
    proposals = [region for region in regions if region["payload"].get("origin") == "proposal"]
    if not proposals:
        raise ContractError(f"act {act_id} reached the Perlector with no original proposal region")
    return regions, proposals


def _region_reference(region: dict) -> dict[str, str]:
    """The exact public region facts a Testimonium may claim it saw."""
    payload = region["payload"]
    return {
        "region_id": payload["region_id"],
        "image_path": payload["image_path"],
        "image_sha256": payload["image_sha256"],
    }


def _testimonium_inputs(context, regions: list[dict]) -> list[dict[str, str]]:
    """The crop blobs that a witness attempt must directly bind."""
    inputs = {}
    for region in regions:
        reference = context.input_ref(region["payload"]["image_path"])
        inputs[reference["relative_path"]] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


def validate_testimonium_regions(context, record: dict, proposal_regions: list[dict]) -> None:
    """Bind a witness's claimed regions to the actual original proposal.

    A Testimonium's direct blob references prove its pixels have not changed, but
    not by themselves that the `regions` payload names the proposal it was shown.
    Without this comparison, a resealed record could add a recovery crop and make
    Perlector mark newly uncovered ink as witnessed.  Recovery must stay visible
    as recovery, not become retrospective witness coverage.
    """
    payload = record["payload"]
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    expected_regions = (
        [_region_reference(region) for region in proposal_regions] if attempted else []
    )
    expected_inputs = _testimonium_inputs(context, proposal_regions) if attempted else []
    if payload.get("regions") != expected_regions or record.get("inputs") != expected_inputs:
        raise SchemaRefusal(
            "a Testimonium does not bind exactly the original proposal regions and pixel "
            "inputs it claims its witness saw"
        )


def testimonia_of(context, act_id: str, proposal_regions: list[dict]) -> list[dict]:
    """Every chair's current testimonium for this act — the latest attempt only.

    Attempts are append-only (GOVERNANCE 4): a failed re-read is recorded beside
    the earlier success, never over it. Every record is still read and its
    provenance still validated, but only each chair's latest attempt is returned
    as evidence — the same collapsing `pipeline/5_recensor/run.py::chair_outcomes`
    already does for the identical artifacts, so dissent, witness-coverage, and the
    Perlectio's own recorded basis cannot see a superseded attempt as though it
    were still live.
    """
    records = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=ATTESTATORES,
                require_receipt=record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES,
            )
            validate_testimonium_regions(context, record, proposal_regions)
            records.append(record)
    current = latest_per_chair(records, f"testimonium for {act_id}")
    chairs = {record["payload"]["chair"] for record in current}
    configured = set(context.witness_chairs)
    missing = configured - chairs
    if missing:
        raise FatalAccounting(
            f"act {act_id} has no current Testimonium for configured chair(s) {sorted(missing)}; "
            "the Perlector may not seal a reading over a shortened witness denominator"
        )
    unsealed = chairs - configured
    if unsealed:
        raise FatalAccounting(
            f"act {act_id} carries Testimonium from chair(s) {sorted(unsealed)}, which this "
            "run was not sealed with"
        )
    return current


def act_attachment_view(
    context, act: dict[str, Any], testimonia: list[dict], bases: list[dict]
) -> dict[str, Any]:
    """Validate the R0 attachment that makes a page witness act-addressable.

    R4 owns alignment; until then this is the chair's complete delivered act
    reading as an interim span, retained beside the page Testimonium and surfaced
    in the dossier rather than silently treating page completion as an act-level
    read.

    `testimonia` is this act's *current* attempt per chair, already collapsed by
    `testimonia_of`. The attachment is a derived view of one attempt, so it is
    checked against that collapse rather than trusted on its own: see the
    per-chair reconciliation below.
    """
    act_id = act["act_id"]
    current = {record["payload"]["chair"]: record for record in testimonia}
    entries = [
        entry
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment" and entry["subject_id"] == act_id
    ]
    if not entries:
        raise FatalAccounting(f"act {act_id} has no act-attachment record")
    records = [
        context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for entry in entries
    ]
    record = latest_attempt(records, f"act-attachment for {act_id}", operation="act-attachment")
    payload = record.get("payload")
    attachments = payload.get("attachments") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"act_key", "attempt_ordinal", "attachments"}
        or payload.get("act_key") != act["act_key"]
        or not isinstance(attachments, list)
    ):
        raise SchemaRefusal("an act-attachment record has no attachment list")
    configured = set(context.witness_chairs)
    page_ids = {basis["source_page_ordinal"]: basis["source_page_id"] for basis in bases}
    declared_chairs = context.fixture.get("page_witness_chairs", [])
    if not isinstance(declared_chairs, list) or any(
        not isinstance(item, str) for item in declared_chairs
    ):
        raise SchemaRefusal(
            "the fixture's page_witness_chairs declaration is not a list of chair names"
        )
    page_chairs = set(declared_chairs)
    attachment_chairs = [
        attachment.get("chair") if isinstance(attachment, dict) else None
        for attachment in attachments
    ]
    if any(chair not in configured for chair in attachment_chairs):
        raise FatalAccounting(
            f"act {act_id} attachment chairs do not equal this run's configured witnesses"
        )
    expected_pairs = {
        (chair, ordinal if chair in page_chairs else None)
        for chair in configured
        for ordinal in (page_ids if chair in page_chairs else (None,))
    }
    pairs = [
        (attachment.get("chair"), attachment.get("page_ordinal"))
        if isinstance(attachment, dict)
        else (None, None)
        for attachment in attachments
    ]
    if len(pairs) != len(set(pairs)) or set(pairs) != expected_pairs:
        raise FatalAccounting(
            f"act {act_id} attachments do not cover every contributing page/witness pair"
        )
    page_witness_count = 0
    comparison_views: dict[str, str] = {}
    for attachment in attachments:
        if (
            not isinstance(attachment, dict)
            or set(attachment)
            != {
                "chair",
                "page_witness",
                "page_ordinal",
                "testimonium_ref",
                "attached",
                "content_health",
                "alignment",
                "span",
            }
            or not isinstance(attachment.get("chair"), str)
            or not isinstance(attachment.get("page_witness"), bool)
            or not isinstance(attachment.get("attached"), bool)
            or not isinstance(attachment.get("content_health"), dict)
        ):
            raise SchemaRefusal("an act-attachment record has a malformed attachment")
        span = attachment["span"]
        characters = attachment["content_health"].get("characters")
        if attachment["attached"] and not attachment["page_witness"]:
            expected_end = (
                characters
                if isinstance(characters, int) and not isinstance(characters, bool)
                else 0
            )
            if span != {"start": 0, "end": expected_end}:
                raise SchemaRefusal(
                    "an attached act view does not span its complete delivered reading"
                )
        elif not attachment["attached"] and span is not None:
            raise SchemaRefusal("an unattached act view claims an alignment span")
        chair = attachment["chair"]
        attachment_page = attachment["page_ordinal"]
        # The attachment describes one attempt, and the reread path
        # (`pipeline/3_attestatores/run.py::reread_pass`) appends a new act-scoped
        # attempt without rewriting it — D8 leaves page-witness reread addressing
        # to R3. Unchecked, the record then presents a superseded attempt's
        # outcome and delivered-character count as this act's live attachment,
        # which is exactly what `testimonia_of`'s latest-attempt collapse exists
        # to stop ("cannot see a superseded attempt as though it were still
        # live"), and what R0's own `granularity_basis` claims is impossible:
        # `attached` IS the current act outcome before R4 alignment. Refuse the
        # divergence rather than count a stale span or a stale reading as
        # current. R4 replaces this reconciliation with real alignment. Found in
        # audit; F-O1.
        chair_testimonium = current.get(chair)
        if chair_testimonium is None:
            raise FatalAccounting(
                f"act {act_id} attachment names chair {chair!r}, which has no current Testimonium"
            )
        if not attachment["page_witness"] and attachment["attached"] != (
            chair_testimonium["outcome"] in WITNESS_READING_OUTCOMES
        ):
            raise SchemaRefusal(
                f"act {act_id} attachment for chair {chair!r} disagrees with that chair's "
                "current Testimonium outcome"
            )
        # NOT exempted for a page witness, unlike the `attached`/outcome check just
        # above: `content_health` is recorded from this act's own per-chair attempt
        # (`attempts_by_pair` in `pipeline/3_attestatores/run.py`) for every chair,
        # page witness or not -- a targeted reread (`reread_pass`) appends a new
        # attempt to that exact same per-(act, chair) `testimonium` stream whether
        # or not the chair is page-scoped, so a page witness's attachment can go
        # stale after a reread exactly as an act-scoped one can (REOPENED F-O1):
        # `attached` legitimately differs from the chair's outcome for a page
        # witness (alignment can honestly fail against live text), but the health
        # of the attempt the attachment actually describes must always still be
        # this chair's current one.
        if attachment["content_health"] != chair_testimonium["payload"].get("content_health"):
            raise SchemaRefusal(
                f"act {act_id} attachment for chair {chair!r} describes an attempt that is no "
                "longer this chair's current Testimonium"
            )
        # The producer (`pipeline/3_attestatores/run.py::declared_page_witness_chairs`)
        # refuses a declaration that is not a unique list of strings; this reader
        # holds the same key to the same shape, or a string-valued declaration
        # would degrade into per-character membership and blame the attachment
        # for the fixture's own malformation.
        expected_page_witness = chair in page_chairs
        if attachment["page_witness"] != expected_page_witness:
            raise SchemaRefusal(
                f"act {act_id} attachment changes page-witness scope for chair {chair!r}"
            )
        # The act-scoped Testimonium carries the same scope claim a second time, as
        # its optional `page_witness` flag, and `pipeline/4_perlector/dissent.py`
        # trusts that flag directly: a record wearing it emits `compared: "unknown"`
        # instead of a real comparison. The attachment's copy is checked above and
        # this one was checked nowhere, so a resealed Testimonium for an ordinary
        # act-scoped chair could silence that chair's dissent row — the structural
        # parroting instrument switched off behind a well-formed and plausible
        # reason, which is the one failure mode ARCHITECTURE's dissent section
        # exists to make measurable. Two spellings of one fact, so both are
        # reconciled against the run's own declaration. Found in fresh-context
        # review (P2).
        if chair_testimonium["payload"].get("page_witness", False) is not expected_page_witness:
            raise SchemaRefusal(
                f"act {act_id} Testimonium for chair {chair!r} claims a page-witness scope this "
                "run did not declare"
            )
        reference = attachment.get("testimonium_ref")
        if attachment["page_witness"]:
            if attachment_page not in page_ids:
                raise SchemaRefusal(
                    f"act {act_id} page attachment names a page outside its regions"
                )
            testimonium = context.tree.read_artifact_reference(
                reference,
                stage=ATTESTATORES,
                kind="page-testimonium",
                subject_id=page_ids[attachment_page],
            )
            page_payload = testimonium.get("payload")
            unjoined = (
                page_payload.get("unjoined_act_attempts")
                if isinstance(page_payload, dict)
                else None
            )
            if (
                not isinstance(page_payload, dict)
                or page_payload.get("chair") != chair
                or page_payload.get("scope") != "page"
                or page_payload.get("page_ordinal") != attachment_page
                or not isinstance(unjoined, list)
                or any(
                    not isinstance(row, dict)
                    or set(row) != {"act_id", "act_key", "outcome", "reason"}
                    or not isinstance(row["act_id"], str)
                    or not isinstance(row["act_key"], str)
                    or not isinstance(row["outcome"], str)
                    or not isinstance(row["reason"], str)
                    or not row["reason"].strip()
                    for row in unjoined
                )
            ):
                raise SchemaRefusal(f"act {act_id} attachment points to the wrong page Testimonium")
            # `page_role` is the producer's own claim about whether this page is
            # the act's primary page or one it only reaches by continuation
            # (`pipeline/3_attestatores/run.py::publish_page_testimonia_and_attachments`).
            # Nothing above reconciles that claim against the one fact this
            # reader already holds independently -- `attachment_page` versus the
            # act's own sealed `page_ordinal` -- so a resealed page Testimonium
            # could wear either label with nothing to catch the flip. This is a
            # partial check, not a full re-derivation of "mixed" (which needs
            # every other act on the page, not available here): it only refuses
            # the two combinations this act's own facts already contradict.
            role = page_payload.get("page_role")
            is_act_primary_page = attachment_page == act["page_ordinal"]
            if role not in {"primary", "continuation", "mixed"} or (
                (is_act_primary_page and role == "continuation")
                or (not is_act_primary_page and role == "primary")
            ):
                raise SchemaRefusal(
                    f"act {act_id} page Testimonium for chair {chair!r} carries a page_role "
                    f"{role!r} its own primary-page fact contradicts"
                )
            current_unjoined = [row for row in unjoined if row["act_id"] == act_id]
            if len(current_unjoined) > 1:
                raise SchemaRefusal(
                    f"act {act_id} appears more than once in a page Testimonium's "
                    "unjoined-attempt record"
                )
            # An act the page join omitted is disclosed with the attempt outcome
            # that explains it, and that outcome must be the one the attachment
            # records. Not `bool(rows) != attached`, which was this check before:
            # it read every omission as a failure, so the moment the join could
            # omit a genuine reading -- a structured native object it cannot
            # concatenate -- the honest disclosure of that omission became a
            # refusal, and staying silent stayed legal. Absence of a row still
            # means the act joined, so an unattached act must always be named.
            # Found in audit; F-O7.
            row = current_unjoined[0] if current_unjoined else None
            disclosed = row["outcome"] in WITNESS_READING_OUTCOMES if row is not None else True
            # Joining a response into the retained page body only proves that
            # the bytes arrived. It does not prove a bounded text alignment can
            # attach them to this act (notably a genuine empty response has no
            # span). An omitted response can never be attached; a joined one may
            # still be explicitly unaligned.
            if not disclosed and attachment["attached"]:
                raise SchemaRefusal(
                    f"act {act_id} attachment disagrees with its page Testimonium's "
                    "unjoined-attempt record"
                )
            alignment = attachment["alignment"]
            if attachment["attached"]:
                if (
                    not isinstance(alignment, dict)
                    or set(alignment)
                    != {
                        "status",
                        "anchor_basis",
                        "anchor_span",
                        "witness_span",
                        "line_geometry",
                        "loss",
                        "offset_maps",
                    }
                    or alignment.get("status") != "aligned"
                    or span != alignment.get("witness_span")
                ):
                    raise SchemaRefusal("an attached page witness has no computed alignment")
                page_text = page_payload.get("reported")
                witness_span = alignment["witness_span"]
                if not isinstance(page_text, str):
                    raise SchemaRefusal("an attached page witness has no textual comparison view")
                # `witness_span` indexes the RAW page reading. It is stored that
                # way at the one storage point (`pipeline/3_attestatores/run.py`
                # clips in the normalized space the matcher measured in, then
                # translates through the alignment's own `offset_map`), so the
                # raw text is the space this slice belongs in and every consumer
                # of the field shares it. F-X3's requirement is met by
                # `act_comparison_view` stripping the SLICE: the premise
                # `dissent.is_comparable` rests on -- that `comparison_reported`
                # is a markup-stripped view and therefore safe to diff -- must
                # hold whichever space the offsets came from, and a raw slice
                # handed on unstripped would carry whatever markup it cut
                # through. Found in audit; F-X3, recomposed by R6's F-G2.
                comparison_views[chair] = act_comparison_view(page_text, witness_span)
            elif (
                not isinstance(alignment, dict)
                or set(alignment) != {"status", "reason"}
                or alignment.get("status") != "unaligned"
                or not (isinstance(alignment["reason"], str) and alignment["reason"].strip())
            ):
                # The producer emits exactly {status, reason}; a reason-free
                # mapping would validate while leaving the operator no
                # statement of why comparison failed.
                raise SchemaRefusal("an unattached page witness has no explicit unaligned result")
            page_witness_count += 1
        else:
            if attachment_page is not None:
                raise SchemaRefusal("an act-scoped witness carries a page ordinal")
            if attachment["alignment"] is not None:
                raise SchemaRefusal("an act-scoped witness carries page alignment evidence")
            testimonium = context.tree.read_artifact_reference(
                reference,
                stage=ATTESTATORES,
                kind="testimonium",
                subject_id=act_id,
            )
            if testimonium.get("payload", {}).get("chair") != chair:
                raise SchemaRefusal(
                    f"act {act_id} attachment points to another chair's Testimonium"
                )
    return {
        "reference": context.artifact_ref(ATTESTATORES, "act-attachment", record["artifact_id"]),
        # A blinded dossier may show that page evidence exists, but not the
        # chair names embedded in its retained attachment artifact.
        "page_witness_count": page_witness_count,
        # Stated exactly, because the count above was chosen to disclose an
        # aggregate and this does not: `comparison_views` is keyed per chair,
        # relabeled through `witness_label` in `dossier.build_dossier`, and
        # present only for page witnesses. A blinded reader therefore learns
        # WHICH pseudonyms are page-scoped -- scope, never identity, but with
        # a roster of three where one chair is act-scoped that narrows a
        # witness from one-in-three to one-in-two and names the act-scoped
        # chair outright. U3 requires the act-anchored view and the view has
        # to be attributable to a label for dissent to use it, so this is the
        # cost of the instrument rather than an oversight; it is recorded here
        # so R5a/R5b, which own the dossier's reference-based act views, can
        # weigh it deliberately. R4 audit, F-X5.
        "comparison_views": comparison_views,
    }


def act_comparison_view(page_text: str, witness_span: dict[str, int]) -> str:
    """One act's markup-stripped slice of a page reading, from a RAW span.

    Since the wave composed R4's per-act clip with R6's raw translation,
    `witness_span` indexes the RAW page-Testimonium text at the one storage
    point (`pipeline/3_attestatores/run.py`) — every consumer of the field
    shares that space. The slice is therefore taken from the raw bytes, and
    the markup stripping F-X3 requires (a comparison view safe to diff — a
    raw slice would carry whatever markup it cut through) is applied to the
    slice itself, not to the whole page before slicing.
    """
    # Both bounds, not only the end: this reads an artifact back from disk, so
    # it is the last gate before untrusted numbers become a comparison view. A
    # slice is the one place a malformed offset does not announce itself:
    # `text[-3:2]` is a perfectly good Python expression and a silently wrong
    # comparison view, which dissent would then read as departure from a
    # witness that said no such thing -- or as corroborating a blank it never
    # reported. The Recensor's own consumer of this field checks the same
    # three conditions.
    if not isinstance(witness_span, dict) or set(witness_span) != {"start", "end"}:
        raise SchemaRefusal("an attached page witness carries no two-bound comparison span")
    start, end = witness_span["start"], witness_span["end"]
    if any(not isinstance(bound, int) or isinstance(bound, bool) for bound in (start, end)):
        raise SchemaRefusal("an attached page witness claims a non-integer comparison span")
    if start < 0 or end < start or end > len(page_text):
        raise SchemaRefusal("an attached page witness claims a span past its own comparison view")
    return markup_text_view(page_text[start:end])["text"]


def dissent_testimonia(testimonia: list[dict], attachment_view: dict[str, Any]) -> list[dict]:
    """Give dissent an act-anchored page slice without changing retained testimony."""
    views = attachment_view["comparison_views"]
    result = []
    for record in testimonia:
        copied = {**record, "payload": dict(record["payload"])}
        chair = copied["payload"]["chair"]
        if copied["payload"].get("page_witness") and chair in views:
            copied["payload"]["comparison_reported"] = views[chair]
        result.append(copied)
    return result


def verify_region(context, region: dict) -> dict:
    """Prove the region handed over is the region the reference describes.

    Three checks, because each catches a different lie: the digest catches bytes
    that changed under a sealed reference, decoding catches a reference pointing at
    something that is not an image, and the dimensions catch a crop that does not
    match the transform it claims to be.

    The underlying cause is carried into the refusal text, not only onto the
    exception chain. `run_stage` prints the refusal it catches, so a cause left
    behind on `__cause__` reached nobody: every one of those distinct faults —
    a missing blob, a relabelled act, a transform outside the page — arrived at
    the operator as the same nine words, and the one thing they needed to know,
    which of them it was, had been thrown away one frame down (GOVERNANCE 2).
    The boundary's own messages name ordinals and run-tree-relative paths, never
    a submitted filename, so nothing this adds to stderr crosses the logging rule.
    """
    try:
        return verify_exemplar_crop_lineage(context.tree, context.run, region)
    except ContractError as error:
        raise SchemaRefusal(
            f"a Designator region does not trace to its Exemplar page: {error}"
        ) from error


# Moved into dossier.py so the dossier derives witness coverage from the same
# testimonia it carries; re-exported here for its callers and tests.
witnessed_region_ids = dossier_module.witnessed_region_ids


def declared_reading_failure(context, act_key: str) -> str | None:
    """The non-completed outcome this scenario declares for an act, if any.

    The fixture is the authority on what a scenario does, exactly as it is for a
    witness failure. A reading that did not succeed still carries whatever text
    it managed, which is the shape that matters: it is what let a `truncated`
    reading be established as the one text.
    """
    for row in context.fixture.get("reading_failure", []):
        if row["scenario"] == context.scenario and row["act_key"] == act_key:
            return row["outcome"]
    return None


def perlector_chair(context) -> ChairIdentity | AbsentChair:
    """The Perlector chair, resolved by name. Never another chair, never a base."""
    resolved = context.registry.resolve(PERLECTOR_CHAIR)
    if not isinstance(resolved, (ChairIdentity, AbsentChair)):
        raise ContractError("Perlector resolution returned neither an identity nor an absence")
    return resolved


def preflight_testimonia_denominator(context, acts: list[dict]) -> None:
    """Validate every requested act's witness denominator before any Perlectio writes.

    A Perlectio is immutable, so one published over a short denominator cannot
    be corrected: restoring the missing witness changes the bytes under the same
    reading identity, and the run can no longer resume normally. Checking the
    whole requested set first is what stops a malformed act discovered late from
    leaving an unfixable reading behind it.
    """
    for act in acts:
        if act["outcome"] == "held":
            continue
        _, proposal_regions = act_regions(context, act["act_id"])
        testimonia_of(context, act["act_id"], proposal_regions)


def provenance_for(context, resolved: ChairIdentity | AbsentChair, *, attempted: bool) -> dict:
    """Project one Perlector outcome's immutable provenance.

    A record for a reading that never happened — a held act, or an absent chair —
    names what would have read and stops there. Manufacturing a receipt for it
    would be a serving moment nobody observed.

    A reading that *did* happen re-verifies the configured snapshot first, at the
    moment it is produced rather than once at run creation: GOVERNANCE 6 is about
    identity when the reading was made, and a receipt captured at serve time and
    copied forward is the weaker claim spec 02 names and refuses.
    """
    regime = {
        # Tyrel's 2026-07-30 ruling: witness identity travels under a run-level
        # toggle, and every Perlectio records the regime it ran under, because a
        # reading's provenance includes what its reader was shown.
        "witness_regime": context.witness_context,
        "adapter_revision": context.adapter_revision,
    }
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            **regime,
        }
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": (
            context.write_serving_receipt(resolved, fixture_serving_details(resolved))
            if attempted
            else None
        ),
        **regime,
    }


def _page_renders_for(context, bases: list[dict]) -> list[dict]:
    """One downscaled page render per distinct page an act's regions touch.

    A continuation act spans two pages; nuda and the primed pass see both,
    because sight is never what nuda withholds.
    """
    by_page: dict[str, dict] = {}
    for basis in bases:
        page_id = basis["source_page_id"]
        if page_id not in by_page:
            by_page[page_id] = dossier_module.build_page_render(
                context,
                source_page_id=page_id,
                source_page_ordinal=basis["source_page_ordinal"],
            )
    return list(by_page.values())


def _whole_act_gap(testimonia: list[dict], references: dict[str, dict]) -> list[dict]:
    """The one gap an unreadable act carries: zero-width, evidence attached,
    never a character inside `text` (the establishment firewall,
    `annotations.py`).

    Each variant travels with the digest-checked reference to the Testimonium
    that reported it, so a displayed "⟨illegible — witnesses agree: …⟩" leads
    back to the sealed record rather than to a chair name somebody would then
    have to go looking for.
    """
    evidence = [
        {
            "chair": record["payload"]["chair"],
            "testimonium_id": record["artifact_id"],
            "reference": references[record["artifact_id"]],
            "variant": record["payload"]["reported"],
        }
        for record in testimonia
        # Presence and type, never truthiness: a genuinely-empty witness
        # reported "" and that report is the strongest corroboration a
        # whole-act gap can carry.
        if record["outcome"] in WITNESS_READING_OUTCOMES
        and isinstance(record["payload"].get("reported"), str)
    ]
    return [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": evidence}]


def _region_pixels(bases: list[dict]) -> int:
    return sum(
        basis["transform"]["bounds"]["w"] * basis["transform"]["bounds"]["h"] for basis in bases
    )


def _reading_image_inputs(context, bases: list[dict], page_renders: list[dict]) -> list[dict]:
    """Every image blob the reading actually saw, including page context.

    The dossier records these references as payload facts; the envelope input
    list independently binds them as direct evidence.  Omitting page context
    there would let a Perlectio claim it saw a render that its own provenance
    never retained as an input.
    """
    inputs = {
        reference["relative_path"]: reference
        for reference in (context.input_ref(basis["image_path"]) for basis in bases)
    }
    for render in page_renders:
        source = render.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("relative_path"), str):
            raise SchemaRefusal("a Perlector page render carries no sealed source-page reference")
        for reference in (
            context.input_ref(source["relative_path"]),
            context.input_ref(render["image_path"]),
        ):
            inputs[reference["relative_path"]] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


# Every field an established-reading Perlectio payload carries. Closed, and
# checked before publication rather than described in the handoff and hoped
# for: spec 08's schema test asks that a Perlectio "missing identity, missing
# dissent, missing regime record, or with annotation spans outside text bounds"
# be refused, and three of those four are fields that would simply be absent
# rather than wrong. An absent field is the failure mode a per-field type check
# never sees.
_PERLECTIO_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "basis",
        "dossier",
        "prompt",
        "dissent",
        "truncation",
        "uncertain_spans",
        "gaps",
        "provenance",
        "lectio_kind",
        "self_revision",
        "protocol",
        "audit",
    }
)

# The same, for the instrument record. It carries no `basis` -- a nuda reading
# has no witness basis to record, which is the whole point of it -- and it does
# carry the sampling design it was drawn under.
_LECTIO_NUDA_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "dossier",
        "prompt",
        "sampling",
        "dissent",
        "truncation",
        "uncertain_spans",
        "gaps",
        "provenance",
    }
)

_LECTIO_PRIOR_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "dossier",
        "prompt",
        "dissent",
        "truncation",
        "uncertain_spans",
        "gaps",
        "provenance",
        "protocol",
    }
)

_PRIMED_WITHOUT_PRIOR_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "basis",
        "dossier",
        "prompt",
        "sampling",
        "dissent",
        "truncation",
        "uncertain_spans",
        "gaps",
        "provenance",
        "lectio_kind",
        "protocol",
        "membership",
    }
)

# The two shapes a Perlectio takes when nothing was read: a held act (the
# proposal never completed) and an explicitly absent chair. Both predate the
# closed-schema guard the other two record kinds already carry -- D-6: the
# record that says *why nothing was read* deserves the same guard as the one
# that says what was, so a future edit that quietly drops `provenance` from
# either shape is refused rather than published.
_NOT_RUN_HELD_FIELDS: Final = frozenset({"act_key", "attempt_ordinal", "reason", "provenance"})
_NOT_RUN_ABSENT_FIELDS: Final = frozenset(
    {"act_key", "attempt_ordinal", "reason", "basis", "dissent", "provenance"}
)


def validate_not_run_payload(payload: dict, *, fields: frozenset) -> None:
    """Refuse a not-run Perlectio missing part of the record it claims.

    Deliberately just the closed field set, not full record validation: a
    not-run payload has no text, no dossier and no completed reading to check
    against each other -- there is nothing else about it to validate.
    """
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector not-run payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )


def validate_reading_payload(
    payload: dict,
    *,
    outcome: str,
    fields: frozenset,
    run_id: str | None = None,
    config_digest: str | None = None,
    protocol_config: dict[str, str] | None = None,
    protocol_sha256: str | None = None,
) -> None:
    """Refuse a reading payload that is missing part of the record it claims.

    Producer-local and deliberately so: `validate_serving_provenance` already
    refuses a wrong-schema provenance wherever a Perlectio is *consumed*, and
    this is the matching check at the moment one is written, so a defect
    surfaces where it was introduced rather than one stage later.
    """
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector reading payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )
    if outcome == "read" and (not isinstance(payload["text"], str) or not payload["text"].strip()):
        raise SchemaRefusal("a completed reading cannot establish an empty text")
    # The caller's field set decides which record shape this is: a Perlectio
    # payload smuggling `basis: None` must refuse as a missing witness basis,
    # never slip down the unprimed branch with every witness gone from the
    # record. Two record kinds are unprimed since R5a -- Lectio nuda and the
    # universal Pass-A `lectio-prior` -- so the branch is named for the
    # condition rather than for one of its two occupants, and so are its
    # refusals: a refusal that says "Lectio nuda" over a lectio-prior record
    # sends the next reader to the wrong artifact.
    is_unprimed = "basis" not in fields
    basis = payload.get("basis")
    if is_unprimed:
        if payload["dissent"] != []:
            raise SchemaRefusal(
                "an unprimed reading (Lectio nuda or lectio-prior) cannot dissent from "
                "testimony it was not shown"
            )
    elif not isinstance(basis, dict) or not isinstance(basis.get("testimonia"), list):
        raise SchemaRefusal("a Perlectio carries no Testimonium basis for its dissent record")
    else:
        validate_dissent(
            payload["dissent"], text=payload["text"], basis_testimonia=basis["testimonia"]
        )
    provenance = payload["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("witness_regime") not in WITNESS_CONTEXT_REGIMES
    ):
        raise SchemaRefusal(
            "a Perlector reading records no witness regime; a reading's provenance includes "
            "what its reader was shown"
        )
    if provenance.get("chair_state") == "configured" and not provenance.get("resolved_identity"):
        raise SchemaRefusal(
            "a Perlector reading by a configured chair records no resolved identity"
        )
    reading_dossier = payload["dossier"]
    dossier_fields = {
        "act_id",
        "act_key",
        "witness_regime",
        "regions",
        "page_renders",
        "testimonia",
        "dossier_digest",
    }
    if not isinstance(reading_dossier, dict) or set(reading_dossier) not in (
        dossier_fields,
        dossier_fields | {"act_attachment"},
        dossier_fields | {"prior_draft", "prior_draft_view"},
        dossier_fields | {"act_attachment", "prior_draft", "prior_draft_view"},
    ):
        raise SchemaRefusal("a Perlector reading carries no closed dossier record")
    if reading_dossier["act_key"] != payload["act_key"]:
        raise SchemaRefusal("a Perlector reading disagrees with its dossier's act key")
    if reading_dossier["witness_regime"] != provenance["witness_regime"]:
        raise SchemaRefusal("a Perlector reading disagrees with its dossier's witness regime")
    dossier_body = {key: value for key, value in reading_dossier.items() if key != "dossier_digest"}
    if reading_dossier["dossier_digest"] != digest_of(dossier_body):
        raise SchemaRefusal("a Perlector dossier digest does not match the dossier it seals")
    dossier_module.assert_no_order_bearing_field(dossier_body)
    lectio_kind = payload.get("lectio_kind")
    prior_draft = reading_dossier.get("prior_draft")
    if lectio_kind == "primed-with-prior":
        if (
            not isinstance(prior_draft, dict)
            or set(prior_draft) != {"reference", "text"}
            or not isinstance(prior_draft["text"], str)
            or reading_dossier.get("prior_draft_view") not in {"fed", "withheld"}
        ):
            raise SchemaRefusal(
                "a Perlectio claims primed-with-prior but carries no closed prior-draft "
                "reference and view"
            )
        validate_input_refs([prior_draft["reference"]])
    elif lectio_kind == "primed-without-prior":
        # Key presence, not value: the dossier field-set check above admits the
        # {prior_draft, prior_draft_view} key combination, so a None prior_draft
        # beside a view key would slip a value-only test.
        if "prior_draft" in reading_dossier or "prior_draft_view" in reading_dossier:
            raise SchemaRefusal(
                "a Perlectio claims primed-without-prior but carries prior-draft data"
            )
    elif lectio_kind is not None:
        # `None` is the two kinds whose field sets exclude the key entirely
        # (lectio-nuda and lectio-prior). Any other value matched neither
        # branch above, so its prior-draft evidence would publish uninspected
        # and the defect would surface one stage later at the Archetypus —
        # the opposite of what this validator promises.
        raise SchemaRefusal(
            f"a Perlector reading names unknown lectio kind {lectio_kind!r}; a kind this "
            "validator cannot name would publish its prior-draft evidence unchecked"
        )
    if "act_attachment" in reading_dossier:
        attachment = reading_dossier["act_attachment"]
        if (
            not isinstance(attachment, dict)
            or set(attachment) != {"reference", "page_witness_count", "comparison_views"}
            or not isinstance(attachment["reference"], dict)
            or not isinstance(attachment["page_witness_count"], int)
            or isinstance(attachment["page_witness_count"], bool)
            or attachment["page_witness_count"] < 0
            or not isinstance(attachment["comparison_views"], dict)
        ):
            raise SchemaRefusal("a Perlector dossier has malformed act-attachment evidence")
        if is_unprimed:
            raise SchemaRefusal(
                "an unprimed reading's dossier cannot carry witness-derived act attachment metadata"
            )
    dossier_testimonia = reading_dossier["testimonia"]
    if not isinstance(dossier_testimonia, list):
        raise SchemaRefusal("a Perlector dossier has no Testimonium list")
    if is_unprimed:
        if dossier_testimonia:
            raise SchemaRefusal("an unprimed reading's dossier cannot carry Testimonia")
    elif len(dossier_testimonia) != len(basis["testimonia"]):
        raise SchemaRefusal(
            "a Perlector dossier does not account for exactly its Testimonium basis"
        )
    else:
        # Label-for-label, not merely count-for-count: a sealed reading must not
        # show one witness set in the prompt while its basis, dissent and export
        # record another. Named labels are the chairs themselves; blinded labels
        # are re-derived from the run's own identity when the caller supplies it
        # (the production publish path always does — the bare form exists for
        # schema tests that assert other refusals).
        basis_chairs = {row["chair"] for row in basis["testimonia"] if isinstance(row, dict)}
        dossier_labels = {
            row["witness_label"] for row in dossier_testimonia if isinstance(row, dict)
        }
        regime_name = reading_dossier["witness_regime"]
        if regime_name == regime.NAMED:
            expected_labels = basis_chairs
        elif run_id is not None and config_digest is not None:
            expected_labels = {
                regime.pseudonym_for(chair, run_id=run_id, config_digest=config_digest)
                for chair in basis_chairs
            }
        else:
            expected_labels = None
        if expected_labels is not None and dossier_labels != expected_labels:
            raise SchemaRefusal(
                "a Perlector dossier's witness labels do not match its Testimonium basis"
            )
    prompt_record = payload["prompt"]
    identity_record = provenance.get("resolved_identity")
    if not isinstance(identity_record, dict):
        raise SchemaRefusal("a Perlector prompt has no resolved chair identity")
    try:
        identity = ChairIdentity(**identity_record)
    except TypeError as error:
        raise SchemaRefusal("a Perlector prompt carries a malformed chair identity") from error
    protocol_record = payload.get("protocol")
    if protocol_record is not None and (
        not isinstance(protocol_record, dict)
        or set(protocol_record) != {"selection_rule", "page_shared_prefix_policy", "draft_fed"}
        or not isinstance(protocol_record["draft_fed"], bool)
    ):
        raise SchemaRefusal("a prior-draft protocol record is not its closed schema")
    # Two statements of one fact: the run-level `draft_fed` the record declares
    # and the per-act view its dossier names. Both derive from the same flag in
    # `main()`, so this cannot fire on the production path -- and that is the
    # point. `self_revision` is only interpretable against a known feeding
    # state, so a record that claimed `draft_fed` true while its dossier
    # withheld the draft would make the Pass-A->B change rate (a standing metric
    # under design v2.1) mean nothing, with nothing in the record saying so.
    prior_draft_view = reading_dossier.get("prior_draft_view")
    if protocol_record is not None and prior_draft_view is not None:
        declared_view = "fed" if protocol_record["draft_fed"] else "withheld"
        if prior_draft_view != declared_view:
            raise SchemaRefusal(
                f"a Perlector reading shows its prior draft {prior_draft_view!r} while the same "
                f"record's protocol declares draft_fed {protocol_record['draft_fed']!r}"
            )
    if protocol_config is None and protocol_record is not None:
        raise SchemaRefusal(
            "a Perlector reading carries a prior-draft protocol record but this validation "
            "call was not given the sealed protocol bytes it reproduces from -- a cwd-relative "
            "reload is not a sealed-config recheck"
        )
    # The record's own two policy names, bound to the sealed bytes rather than
    # merely present. `prompt.page_shared_prefix_policy` is already reproduced
    # from `protocol_config` by the prompt check below, so a payload could
    # declare one rule in its `protocol` block while its prompt was built under
    # another, and nothing said so. Unfireable on the production path for the
    # same reason the draft_fed cross-check above is, and recorded here for the
    # same reason: what these two names mean is what the run sealed.
    if protocol_record is not None:
        declared = (protocol_record["selection_rule"], protocol_record["page_shared_prefix_policy"])
        sealed = (protocol_config["selection_rule"], protocol_config["page_shared_prefix_policy"])
        if declared != sealed:
            raise SchemaRefusal(
                f"a Perlector reading declares protocol {declared!r} while the bytes this run "
                f"sealed declare {sealed!r}"
            )
    if protocol_config is None:
        protocol_config = {
            "page_shared_prefix_policy": protocol.PAGE_SHARED_PREFIX_POLICY,
            "pass_b_fragment": "",
        }
    if protocol_sha256 is None:
        protocol_sha256 = "unsealed-test"
    if prompt_record != prompts.prompt_evidence(
        identity, reading_dossier, protocol_config, protocol_sha256
    ):
        raise SchemaRefusal(
            "a Perlector prompt record does not reproduce from its resolved chair and dossier"
        )
    if (
        not isinstance(payload["truncation"], dict)
        or payload["truncation"].get("classification") not in truncation.CLASSIFICATIONS
    ):
        raise SchemaRefusal(
            "a Perlector reading carries no truncation classification; truncation is detected "
            "by an instrument, never assumed"
        )
    if outcome == "read" and payload["truncation"]["classification"] != truncation.COMPLETE:
        raise SchemaRefusal(
            "a truncated or unknown attempt cannot carry the completed outcome 'read'"
        )
    if outcome == "truncated" and payload["truncation"]["classification"] == truncation.COMPLETE:
        raise SchemaRefusal(
            "a Perlectio with outcome 'truncated' cannot carry a 'complete' truncation "
            "classification; outcome == 'truncated' means 'not established complete', and "
            "the truncation field is where that is confirmed or held unknown, never "
            "contradicted"
        )
    if "audit" not in fields:
        annotations.validate_annotations(payload, outcome=outcome)
        return
    # The re-proof locations are offsets in the frozen semi-final, which may
    # be longer than the corrected final after a deletion. The full shared
    # chain check binds them to the audit draft before publication; this
    # payload-only shape check therefore does not guess a bound from final text.
    audit.validate_perlectio_audit(payload.get("audit"), text_length=None)
    annotations.validate_annotations(payload, outcome=outcome)


def _resolve_outcome(*, declared_failure: str | None, truncation_record: dict, text: str) -> str:
    """One place the outcome is decided, so the precedence is stated once:
    a scenario's declared engine behaviour outranks the computed detector
    (it stands in for a real engine's own report), the detector outranks a
    default `read`, and an empty reading is never silently `read`."""
    if declared_failure is not None:
        return declared_failure
    if truncation.holds_as_failure(truncation_record["classification"]):
        return "truncated"
    if not text.strip():
        # The same emptiness rubric the publish-time schema uses: a reader
        # returning "\n" for one act is an unreadable act, not a reason to
        # abort the stage and lose the parish's other readings.
        return "no-readable-text"
    return "read"


def _reconciled_truncation(*, declared_failure: str | None, truncation_record: dict) -> dict:
    """Keep the published truncation record from contradicting a declared failure.

    A declared failure stands in for a real engine's own report that a reading
    did not complete, and nothing about *why* need show in the text's shape --
    so the three computed signals can land on `complete` under an outcome that
    means "not established complete" (HANDOFF.md, verbatim). The signals stay
    exactly as measured; only the classification is raised to `unknown`, because
    the instrument did not itself confirm a cutoff. Something outside it did.
    """
    if (
        declared_failure == "truncated"
        and truncation_record["classification"] == truncation.COMPLETE
    ):
        return {**truncation_record, "classification": truncation.UNKNOWN}
    return truncation_record


def _audited_truncation(
    *,
    pass_b: dict,
    declared_failure: str | None,
    text: str,
    region_pixels: int,
    stop_reason: str | None,
) -> dict:
    """The truncation instrument, re-measured over an audit-changed reading.

    Three of the four declared signals are computed over the reading text and
    the fourth is the engine's own word on why it stopped, so a Pass-C re-proof
    that changes the text invalidates the whole record: the published Perlectio
    would otherwise state what the *pre-audit* text looked like, and `outcome`
    is derived from that record. The re-proof's own stop reason was dropped
    entirely, so a re-proof generation cut off mid-emission could replace
    established text while the record still read `complete` — the reading
    delivered as an output would be the truncated one (ARCHITECTURE: "it reads
    through to the end; truncation is a failure, not an output").

    **Pass C may only ever make this worse.** The re-proof is span-scoped and
    H8-bounded to the flagged location, so it cannot restore ink a cut-off Pass
    B never read. A clean re-proof over a truncated semi-final therefore keeps
    the earlier classification: the recomputed signals describe the published
    text, but the verdict never improves.
    """
    audited = _reconciled_truncation(
        declared_failure=declared_failure,
        truncation_record=truncation.classify(
            text, region_pixels=region_pixels, stop_reason=stop_reason
        ),
    )
    if (
        pass_b["classification"] != truncation.COMPLETE
        and audited["classification"] == truncation.COMPLETE
    ):
        return {**audited, "classification": pass_b["classification"]}
    return audited


def _audit_semi_final(
    *,
    act_id: str,
    page_id: str,
    order: int,
    text: str,
    regions: list[dict[str, Any]],
    dossier: dict[str, Any],
) -> dict[str, Any]:
    """Derive one Pass-C row from either a pending or sealed Perlectio."""
    if not regions:
        raise FatalAccounting(f"Perlectio for {act_id} has no region for its audit geometry")
    first = regions[0]
    bounds = first.get("transform", {}).get("bounds")
    if first.get("source_page_id") != page_id or not isinstance(bounds, dict):
        raise FatalAccounting(
            f"Perlectio for {act_id} does not bind its starting page and crop geometry"
        )
    # Proved before it becomes a sort key: `bounds.get` handed a crop record
    # that lost either number a (None, None) geometry_order, and comparing
    # None with an integer ends the whole page's flag pass in an unnamed
    # TypeError -- the same failure the `_region_ordinal` refusal exists to
    # prevent.
    if any(
        not isinstance(bounds.get(side), int) or isinstance(bounds.get(side), bool)
        for side in ("x", "y")
    ):
        raise FatalAccounting(
            f"Perlectio for {act_id} has no integer crop origin to order its page audit by"
        )
    testimonia = dossier.get("testimonia")
    if not isinstance(testimonia, list):
        raise FatalAccounting(f"Perlectio for {act_id} has no sealed audit testimonia")
    reports: list[str] = []
    for record in testimonia:
        if not isinstance(record, dict):
            raise FatalAccounting(f"Perlectio for {act_id} carries a non-object audit testimonium")
        reported = record.get("reported")
        # Absent is the honest shape for a chair that failed or never ran; a
        # present-but-untextual report is a witness this pass cannot compare
        # and may not quietly omit from the flag denominator -- the audit
        # draft would state a flag set computed from fewer witnesses than the
        # act has, with nothing saying which was left out. `dissent_against`
        # already refuses the same shape; this keeps the two rules aligned so
        # a reordering cannot open the gap.
        if reported is None:
            continue
        if not isinstance(reported, str):
            raise FatalAccounting(
                f"Perlectio for {act_id} carries a witness report that is not text; a witness "
                "this pass cannot compare may not be dropped from its flags"
            )
        reports.append(reported)
    return {
        "act_id": act_id,
        "page_id": page_id,
        "order": order,
        # The act's own crop position, independent of the sequence it was
        # declared and processed in -- reusing `order` here would make declared
        # and geometric identical by construction, so the "order" flag class
        # could never fire (audit finding H1).
        "geometry_order": (bounds.get("y"), bounds.get("x")),
        "text": text,
        "testimonia": reports,
        # Pass C accounts only within the delivered crop. Page partition and
        # residual-ink predicates belong to the Recensor.
        "within_crop": True,
    }


def audit_page_ids(bases: list[dict[str, Any]]) -> list[str]:
    """The complete, stable page denominator for one act's page audit."""
    by_ordinal = {basis["source_page_ordinal"]: basis["source_page_id"] for basis in bases}
    page_ids = [by_ordinal[ordinal] for ordinal in sorted(by_ordinal)]
    if not page_ids:
        raise FatalAccounting("Perlectio has no source pages for its audit")
    return page_ids


def audit_semi_finals_for_pages(
    *, act_id: str, order: int, text: str, bases: list[dict[str, Any]], dossier: dict[str, Any]
) -> list[dict[str, Any]]:
    """Place the same act in every page comparison its pixels contribute to."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for basis in bases:
        grouped.setdefault(basis["source_page_id"], []).append(basis)
    return [
        _audit_semi_final(
            act_id=act_id, page_id=page_id, order=order, text=text, regions=regions, dossier=dossier
        )
        for page_id, regions in sorted(grouped.items())
    ]


def _sealed_sibling_semi_finals(
    context,
    current: list[dict[str, Any]],
    *,
    expected: list[dict[str, Any]],
    protocol_config: dict[str, Any] | None = None,
    protocol_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Read same-page sibling Perlectiones as immutable recovery context.

    `RunTree.build_manifest` and `read_artifact` both validate every envelope's
    self-hash, derived path, run/config binding, and input bytes. The sibling
    text below therefore comes from the sealed Perlectio in the tree, never a
    reconstruction from fixture or source text. Only rows are returned; the
    publication loop remains over `pending`, so a sibling cannot be republished.
    """
    current_ids = {row["act_id"] for row in current}
    page_ids = {row["page_id"] for row in current}
    order_by_id = {act["act_id"]: order for order, act in enumerate(expected)}
    # Candidate selection itself must use the complete page set. The proposal
    # seal carries only an act's primary page, but every completed Perlectio
    # already carries the full region basis it read. Selecting on the scalar
    # first meant an act whose primary page was elsewhere never had that basis
    # opened, even when its continuation shared the recovered page. Omitting an
    # intermediate row is not conservative: adjacency checks can both miss the
    # omitted inversion and invent a new comparison between its former
    # neighbours. Read the existing sealed map; no new artifact contract is
    # needed.
    sibling_ids = {act["act_id"] for act in expected if act["act_id"] not in current_ids}
    records_by_subject: dict[str, list[dict[str, Any]]] = {act_id: [] for act_id in sibling_ids}
    for entry in context.tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "perlectio" or entry["subject_id"] not in sibling_ids:
            continue
        record = context.tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        records_by_subject[entry["subject_id"]].append(record)

    siblings = []
    for act_id in sorted(sibling_ids, key=order_by_id.__getitem__):
        records = records_by_subject[act_id]
        if not records:
            # Never a skip: by the time a recovery pass runs, every expected
            # act carries a Perlectio -- a held one carries `not-run`, handled
            # below. Zero artifacts means a reading that existed is no longer
            # here, and a page flag pass computed over a short row set would
            # seal a different flag set than the page's evidence supports. A
            # missing middle row can remove its own comparison or create a new
            # adjacency between its former neighbours.
            raise FatalAccounting(
                f"act {act_id} shares this page with the recovered act but has no Perlectio "
                "at all; the page audit may not be computed over a row that is missing"
            )
        reading = latest_attempt(
            records, f"sealed sibling Perlectio for {act_id}", operation="perlegere"
        )
        payload = reading.get("payload")
        # A held/not-run sibling was absent from the original frozen Pass-B
        # collection too, so it contributes no row to a later recovery audit.
        # ONLY that outcome skips: any other outcome whose payload lost its
        # text is malformed evidence, and dropping it would quietly shrink the
        # page's cross-act flag comparisons exactly like the zero-record case
        # above.
        if reading["outcome"] == "not-run":
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise FatalAccounting(
                f"sealed sibling Perlectio for {act_id} has outcome {reading['outcome']!r} "
                "but no text to audit; a malformed sibling may not be dropped from the "
                "page's flag comparisons"
            )
        validate_reading_payload(
            payload,
            outcome=reading["outcome"],
            fields=_PERLECTIO_FIELDS,
            run_id=context.tree.run_id,
            config_digest=context.config_digest,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )
        chain = audit.validate_chain(context.tree, reading, act_id)
        draft_payload = chain["draft"]["payload"]
        finding_payload = chain["finding"]["payload"]
        expected_page = expected[order_by_id[act_id]]["page_id"]
        if draft_payload["page_id"] != expected_page or finding_payload["page_id"] != expected_page:
            raise FatalAccounting(
                f"sealed sibling Perlectio for {act_id} does not reconcile with its audit chain"
            )
        bases = reading_basis_regions(reading, f"sealed sibling Perlectio for {act_id}")
        sibling_page_ids = {basis["source_page_id"] for basis in bases}
        if not page_ids.intersection(sibling_page_ids):
            continue
        # Page-multiplied exactly like the recovered act's own rows above. A
        # sibling that runs across the page break belongs in BOTH of its pages'
        # cross-act comparisons, and the first (whole-run) pass placed it in
        # both; building it here on its primary page alone made a recovery
        # round's page comparisons a row short of the pass that preceded it,
        # the same unsupported-short-denominator hazard this function already
        # refuses for a missing record.
        siblings.extend(
            audit_semi_finals_for_pages(
                act_id=act_id,
                order=order_by_id[act_id],
                text=payload["text"],
                bases=bases,
                dossier=payload["dossier"],
            )
        )
    return siblings


def _page_flags(
    context,
    semi_finals: list[dict[str, Any]],
    *,
    expected: list[dict[str, Any]],
    recovery_act_id: str | None,
    protocol_config: dict[str, Any] | None = None,
    protocol_sha256: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    frozen = list(semi_finals)
    if recovery_act_id is not None:
        frozen.extend(
            _sealed_sibling_semi_finals(
                context,
                frozen,
                expected=expected,
                protocol_config=protocol_config,
                protocol_sha256=protocol_sha256,
            )
        )
    return audit.flags_once_per_page(frozen)


def _publish_lectio_nuda(
    context,
    *,
    act: dict,
    act_id: str,
    ordinal: int,
    chair: ChairIdentity,
    bases: list[dict],
    page_renders: list[dict],
    reader: FixtureReader,
    region_pixels: int,
    witness_context_table: dict,
    protocol_config: dict[str, str],
    protocol_sha256: str,
) -> None:
    """Publish the unprimed instrument reading.

    Carries no testimonia at all (spec 08). Written under its own artifact
    `kind` (`lectio-nuda`, never `perlectio`) and its own attempt operation
    (`lectio-nuda`, never `perlegere`) -- structurally outside every consumer
    that queries `kind == "perlectio"`, and outside the identity space
    `latest_attempt`'s `attempt_id` derivation binds to `perlegere` readings,
    so nothing can ever conflate a nuda attempt with an establishing one.
    """
    nuda_dossier, delivered_pixels = dossier_module.build_reader_dossier(
        context,
        act_id=act_id,
        act_key=act["act_key"],
        regions=bases,
        testimonia=[],
        regime=context.witness_context,
        page_renders=page_renders,
        witness_context=witness_context_table,
    )
    prompt = prompts.prompt_evidence(chair, nuda_dossier, protocol_config, protocol_sha256)
    result = reader.read(nuda_dossier, pass_kind="lectio-nuda", delivered_pixels=delivered_pixels)
    truncation_record = truncation.classify(
        result["text"], region_pixels=region_pixels, stop_reason=result["stop_reason"]
    )
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    # One emptiness rubric everywhere: a whitespace-only reading resolved to
    # `no-readable-text` publishes the empty text that outcome's schema
    # requires — the whitespace was never established ink.
    nuda_text = "" if outcome == "no-readable-text" else result["text"]
    payload = {
        "act_key": act["act_key"],
        "attempt_ordinal": ordinal,
        "text": nuda_text,
        "dossier": nuda_dossier,
        "prompt": prompt,
        "sampling": nuda.sampling_design(
            nuda_per_mille=context.nuda_per_mille,
            approval_ref=context.nuda_approval_ref,
        ),
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap([], {}) if outcome == "no-readable-text" else [],
        "provenance": provenance_for(context, chair, attempted=True),
    }
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=_LECTIO_NUDA_FIELDS,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    context.publish(
        kind=nuda.LECTIO_NUDA_KIND,
        subject_id=act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(act_id, "lectio-nuda", ordinal),
        inputs=_reading_image_inputs(context, bases, page_renders),
        payload=payload,
    )


def _publish_lectio_prior(
    context,
    *,
    act,
    act_id,
    ordinal,
    chair,
    bases,
    page_renders,
    reader,
    region_pixels,
    witness_context_table,
    protocol_config,
    protocol_sha256,
) -> dict:
    """Pass A is universal and un-fed; it is a retained draft, never a Perlectio."""
    prior_dossier, delivered_pixels = dossier_module.build_reader_dossier(
        context,
        act_id=act_id,
        act_key=act["act_key"],
        regions=bases,
        testimonia=[],
        regime=context.witness_context,
        page_renders=page_renders,
        witness_context=witness_context_table,
    )
    prompt = prompts.prompt_evidence(chair, prior_dossier, protocol_config, protocol_sha256)
    result = reader.read(prior_dossier, pass_kind="lectio-prior", delivered_pixels=delivered_pixels)
    truncation_record = truncation.classify(
        result["text"], region_pixels=region_pixels, stop_reason=result["stop_reason"]
    )
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    text = "" if outcome == "no-readable-text" else result["text"]
    payload = {
        "act_key": act["act_key"],
        "attempt_ordinal": ordinal,
        "text": text,
        "dossier": prior_dossier,
        "prompt": prompt,
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap([], {}) if outcome == "no-readable-text" else [],
        "provenance": provenance_for(context, chair, attempted=True),
        "protocol": {
            "selection_rule": protocol_config["selection_rule"],
            "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
            "draft_fed": context.draft_fed,
        },
    }
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=_LECTIO_PRIOR_FIELDS,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    context.publish(
        kind="lectio-prior",
        subject_id=act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(act_id, "lectio-prior", ordinal),
        inputs=_reading_image_inputs(context, bases, page_renders),
        payload=payload,
    )
    prior_artifact_id = artifact_id(
        PERLECTOR, "lectio-prior", act_id, perlector_attempt_id(act_id, "lectio-prior", ordinal)
    )
    return {
        "reference": context.artifact_ref(PERLECTOR, "lectio-prior", prior_artifact_id),
        "text": text,
    }


def _publish_primed_without_prior(
    context,
    *,
    act,
    act_id,
    ordinal,
    chair,
    bases,
    page_renders,
    reader,
    region_pixels,
    testimonia,
    attachment_view,
    witness_context_table,
    protocol_config,
    protocol_sha256,
) -> None:
    """The sampled control sees witnesses but never the Pass-A draft."""
    control_dossier, delivered_pixels = dossier_module.build_reader_dossier(
        context,
        act_id=act_id,
        act_key=act["act_key"],
        regions=bases,
        testimonia=testimonia,
        regime=context.witness_context,
        page_renders=page_renders,
        witness_context=witness_context_table,
        act_attachment=attachment_view,
    )
    prompt = prompts.prompt_evidence(chair, control_dossier, protocol_config, protocol_sha256)
    result = reader.read(
        control_dossier, pass_kind="primed-without-prior", delivered_pixels=delivered_pixels
    )
    truncation_record = truncation.classify(
        result["text"], region_pixels=region_pixels, stop_reason=result["stop_reason"]
    )
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    text = "" if outcome == "no-readable-text" else result["text"]
    testimonium_references = {
        record["artifact_id"]: context.artifact_ref(
            ATTESTATORES, "testimonium", record["artifact_id"]
        )
        for record in testimonia
    }
    # `context.run` is the run authority `open_context` already read and
    # verified from disk, and nothing writes `run.json` after the Door creates
    # it. Re-reading it here would re-verify the same bytes once per sampled
    # act, and once more per act at the sampling decision below.
    membership = context.run["corpus_frame_membership"]
    payload = {
        "act_key": act["act_key"],
        "attempt_ordinal": ordinal,
        "text": text,
        "basis": {
            "regions": bases,
            "testimonia": [
                {
                    "chair": record["payload"]["chair"],
                    "artifact_id": record["artifact_id"],
                    "outcome": record["outcome"],
                    "reference": testimonium_references[record["artifact_id"]],
                }
                for record in testimonia
            ],
        },
        "dossier": control_dossier,
        "prompt": prompt,
        "sampling": {
            "perlector_instrument_per_mille": context.perlector_instrument_per_mille,
            "selection_rule": protocol_config["selection_rule"],
            "approval_ref": context.perlector_instrument_approval_ref,
        },
        "membership": {**membership, "act_id": act_id, "protocol_sha256": protocol_sha256},
        "dissent": dissent_against(text, dissent_testimonia(testimonia, attachment_view)),
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap(testimonia, testimonium_references)
        if outcome == "no-readable-text"
        else [],
        "provenance": provenance_for(context, chair, attempted=True),
        "lectio_kind": "primed-without-prior",
        "protocol": {
            "selection_rule": protocol_config["selection_rule"],
            "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
            "draft_fed": context.draft_fed,
        },
    }
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=_PRIMED_WITHOUT_PRIOR_FIELDS,
        run_id=context.tree.run_id,
        config_digest=context.config_digest,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    context.publish(
        kind="primed-without-prior",
        subject_id=act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(act_id, "primed-without-prior", ordinal),
        inputs=_reading_image_inputs(context, bases, page_renders)
        + list(testimonium_references.values())
        + [attachment_view["reference"]],
        payload=payload,
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run through the explicitly supplied chair implementation.

    Production passes the default registry. The test-only injection is a
    dependency seam, not a runtime choice among models or chairs.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, PERLECTOR, registry_factory=registry_factory)
    reader = FixtureReader(context.fixture, context.scenario)
    witness_context_table = dossier_module.load_witness_context(
        Path(context.witness_context_config_path)
    )
    protocol_config, protocol_sha256 = protocol.load(context.perlector_protocol_config_path)
    context.require_sealed_config("perlector-protocol", protocol_sha256)
    audit_policy, audit_sha256 = audit.load(context.perlector_audit_config_path)
    context.require_sealed_config("perlector-audit", audit_sha256)

    # A recovery re-reads only the acts that were recovered. Re-reading the rest
    # would add an attempt nobody requested to an act nothing happened to, and an
    # attempt tally that counts work no stage asked for stops meaning anything.
    expected = expected_acts(context)
    declared_order = {act["act_id"]: order for order, act in enumerate(expected)}
    wanted = [act for act in expected if args.act in (None, act["act_id"])]
    if args.act and not wanted:
        raise ContractError(f"asked to read {args.act}, which the proposal seal does not name")
    preflight_testimonia_denominator(context, wanted)

    read = 0
    acknowledged = 0
    pending: list[dict[str, Any]] = []
    chair = perlector_chair(context)
    for act in wanted:
        act_id = act["act_id"]
        if act["outcome"] == "held":
            # A held act's proposal is incomplete — its page or its continuation
            # never sealed. Reading whatever regions exist would produce a
            # reading of part of an act, and it reads through to the end:
            # truncation is a failure, not an output. The act is acknowledged
            # with an explicit unresolved outcome rather than skipped, because
            # a unit this stage never mentions is invariant #10's imbalance.
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": 1,
                "reason": (
                    "the Designator held this act; an incomplete proposal is "
                    "not read, because a reading of part of an act would be a "
                    "truncation delivered as an output"
                ),
                "provenance": provenance_for(context, chair, attempted=False),
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_HELD_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=perlector_attempt_id(act_id, "perlegere", 1),
                payload=payload,
            )
            acknowledged += 1
            continue

        # Read once, before the ordinal is derived from it: the same region set
        # answers both which attempt this is and which crops are read, and
        # `_next_attempt` refuses an unplaceable origin here rather than leaving
        # the next stage to discover it over an immutable Perlectio. Every act
        # reaching this line already had its regions walked and validated by
        # `preflight_testimonia_denominator`, absent chair or not.
        regions, proposal_regions = act_regions(context, act_id)
        ordinal = _next_attempt(context, act_id, regions)
        if isinstance(chair, AbsentChair):
            # No chair to read with. Every act still gets an explicit record
            # naming the absence: a stage that simply produced nothing would
            # leave the Recensor to infer a gap it cannot see.
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "reason": f"the Perlector chair is explicitly absent: {chair.reason}",
                "basis": {"regions": [], "testimonia": []},
                "dissent": [],
                "provenance": provenance_for(context, chair, attempted=False),
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_ABSENT_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=perlector_attempt_id(act_id, "perlegere", ordinal),
                payload=payload,
            )
            acknowledged += 1
            continue

        # Every region of the act is verified and read, including a continuation
        # on the next page: an act that ran over the page break and was read only
        # up to the fold would be truncated, which is a failure and not an output.
        bases = [verify_region(context, region) for region in regions]
        testimonia = testimonia_of(context, act_id, proposal_regions)
        attachment_view = act_attachment_view(context, act, testimonia, bases)

        # Which regions any witness actually saw. Ink uncovered by a recovery
        # recrop was never shown to a witness, and saying so is the difference
        # between a gap in the record and a gap nobody can see. It changes nothing
        # about the reading — the Perlector reads the ink either way.
        witnessed = witnessed_region_ids(testimonia)
        for basis in bases:
            basis["witness_covered"] = basis["region_id"] in witnessed

        region_pixels = _region_pixels(bases)
        page_renders = _page_renders_for(context, bases)

        # The unprimed instrument, sampled by the run's own predeclared design
        # (`nuda_per_mille`, fixed before the run). Ahead of the establishing
        # pass in this loop, but the two are independent artifacts; nothing
        # about the nuda reading feeds the primed one or vice versa.
        if nuda.is_nuda_sampled(
            act_id, run_id=context.tree.run_id, nuda_per_mille=context.nuda_per_mille
        ):
            _publish_lectio_nuda(
                context,
                act=act,
                act_id=act_id,
                ordinal=ordinal,
                chair=chair,
                bases=bases,
                page_renders=page_renders,
                reader=reader,
                region_pixels=region_pixels,
                witness_context_table=witness_context_table,
                protocol_config=protocol_config,
                protocol_sha256=protocol_sha256,
            )

        prior = _publish_lectio_prior(
            context,
            act=act,
            act_id=act_id,
            ordinal=ordinal,
            chair=chair,
            bases=bases,
            page_renders=page_renders,
            reader=reader,
            region_pixels=region_pixels,
            witness_context_table=witness_context_table,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )

        frame_membership = context.run["corpus_frame_membership"]
        if protocol.is_control_sampled(
            act_id,
            frame_digest=frame_membership["frame_digest"],
            page_digest=frame_membership["page_digest"],
            seed=frame_membership["seed"],
            per_mille=context.perlector_instrument_per_mille,
        ):
            _publish_primed_without_prior(
                context,
                act=act,
                act_id=act_id,
                ordinal=ordinal,
                chair=chair,
                bases=bases,
                page_renders=page_renders,
                reader=reader,
                region_pixels=region_pixels,
                testimonia=testimonia,
                attachment_view=attachment_view,
                witness_context_table=witness_context_table,
                protocol_config=protocol_config,
                protocol_sha256=protocol_sha256,
            )

        # The establishing read: every testimonium in the dossier, verbatim.
        primed_dossier, delivered_pixels = dossier_module.build_reader_dossier(
            context,
            act_id=act_id,
            act_key=act["act_key"],
            regions=bases,
            testimonia=testimonia,
            regime=context.witness_context,
            page_renders=page_renders,
            witness_context=witness_context_table,
            act_attachment=attachment_view,
            prior_draft=prior,
            prior_draft_view="fed" if context.draft_fed else "withheld",
        )
        # Built before the reader is called, from the dossier the reader is
        # about to be shown, so what is recorded is the prompt this reading was
        # produced through rather than one reconstructed afterwards.
        prompt = prompts.prompt_evidence(chair, primed_dossier, protocol_config, protocol_sha256)
        result = reader.read(
            primed_dossier, pass_kind="perlectio", delivered_pixels=delivered_pixels
        )

        # The scenario's declared engine behaviour stands in for a real
        # engine's own report and, when present, decides `reading` and
        # `outcome` together: a declared `no-readable-text` means nothing was
        # read, not that the fixture's normal act text happens to still apply.
        declared_failure = declared_reading_failure(context, act["act_key"])
        reading = "" if declared_failure == "no-readable-text" else result["text"]
        truncation_record = _reconciled_truncation(
            declared_failure=declared_failure,
            truncation_record=truncation.classify(
                reading, region_pixels=region_pixels, stop_reason=result["stop_reason"]
            ),
        )
        outcome = _resolve_outcome(
            declared_failure=declared_failure, truncation_record=truncation_record, text=reading
        )
        if outcome == "no-readable-text":
            # See the nuda publish path: whitespace resolved as unreadable is
            # published as the empty text its schema requires.
            reading = ""
        testimonium_references = {
            record["artifact_id"]: context.artifact_ref(
                ATTESTATORES, "testimonium", record["artifact_id"]
            )
            for record in testimonia
        }
        gaps = (
            _whole_act_gap(testimonia, testimonium_references)
            if outcome == "no-readable-text"
            else []
        )

        provenance = provenance_for(context, chair, attempted=True)
        payload = {
            "act_key": act["act_key"],
            "attempt_ordinal": ordinal,
            "text": reading,
            "basis": {
                "regions": bases,
                "testimonia": [
                    {
                        "chair": record["payload"]["chair"],
                        "artifact_id": record["artifact_id"],
                        "outcome": record["outcome"],
                        "reference": testimonium_references[record["artifact_id"]],
                    }
                    for record in testimonia
                ],
            },
            "dossier": primed_dossier,
            "prompt": prompt,
            "dissent": dissent_against(reading, dissent_testimonia(testimonia, attachment_view)),
            "truncation": truncation_record,
            "uncertain_spans": [],
            "gaps": gaps,
            "provenance": provenance,
            "lectio_kind": "primed-with-prior",
            "self_revision": departures(reading, prior["text"]),
            "protocol": {
                "selection_rule": protocol_config["selection_rule"],
                "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
                "draft_fed": context.draft_fed,
            },
        }
        pending.append(
            {
                "act": act,
                "act_id": act_id,
                "order": declared_order[act_id],
                "bases": bases,
                "payload": payload,
                "outcome": outcome,
                # Deliberately NOT the decoded pixels: holding every act's
                # delivered images until the audit loop reaches it would grow
                # peak memory with the number of acts on the run -- a parish
                # of several hundred acts is several hundred sets of
                # page-sized buffers held at once, and the failure mode is an
                # OOM kill after the drafts are on disk and before any
                # Perlectio is published. The rare re-proof rebuilds its
                # pixels from the same sealed artifacts instead.
                "region_pixels": region_pixels,
                "declared_failure": declared_failure,
                "testimonia": testimonia,
                "attachment_view": attachment_view,
                "prior": prior,
                "inputs": _reading_image_inputs(context, bases, page_renders)
                + list(testimonium_references.values())
                + [attachment_view["reference"], prior["reference"]],
            }
        )
        read += 1

    # The page flag pass receives these immutable Pass-B semi-finals together,
    # before any re-proof result exists.  Its output is therefore one
    # deterministic cross-act computation per page, with no cascade.
    semi_finals = []
    for row in pending:
        payload = row["payload"]
        bases = row["bases"]
        semi_finals.extend(
            audit_semi_finals_for_pages(
                act_id=row["act_id"],
                order=row["order"],
                text=payload["text"],
                bases=bases,
                dossier=payload["dossier"],
            )
        )
    page_flags = _page_flags(
        context,
        semi_finals,
        expected=expected,
        recovery_act_id=args.act,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    policy_record = audit.policy_record(audit_policy, audit_sha256)
    # One page's renders at a time: flags are common (the fixture flags every
    # act), so the re-proof rebuild below runs for most acts on a real run.
    # Page renders are the expensive half and are per PAGE, not per act;
    # `pending` walks acts in declared order, so a single-slot cache keyed by
    # the act's page set removes the repeated render work while keeping peak
    # memory bounded to one page -- the same bound the no-hoarding comment
    # below defends.
    render_cache: dict[str, Any] = {}
    for row in pending:
        payload = row["payload"]
        act_id = row["act_id"]
        flags = page_flags[act_id]
        page_ids = audit_page_ids(row["bases"])
        page_id = page_ids[0]
        draft_payload = {
            "act_key": row["act"]["act_key"],
            "attempt_ordinal": payload["attempt_ordinal"],
            "semi_final_text": payload["text"],
            "page_id": page_id,
            "page_ids": page_ids,
            "round_cap": audit_policy["round_cap"],
            "policy": policy_record,
            "flags": flags,
        }
        audit.validate_draft(draft_payload)
        draft = context.publish(
            kind="audit-draft",
            subject_id=act_id,
            outcome="read",
            attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
            inputs=row["inputs"],
            payload=draft_payload,
        )
        draft_ref = context.input_ref(draft.relative_path)
        final_text = payload["text"]
        unresolved = bool(flags) and audit_policy["round_cap"] == 0
        # The plan this act's frozen flags imply, computed once by the function
        # `validate_chain` re-derives it with and `audit.audit_request` builds
        # the reader's copy from. What is sealed below, what the reader is
        # handed, and what every later consumer recomputes are therefore the
        # same computation over the same frozen flags rather than three
        # spellings that have to be kept agreeing.
        reproofs = audit.reproof_plan(flags, text_length=len(final_text))
        request_digest: str | None = None
        changes: list[dict[str, Any]] = []
        uncertainty: list[dict[str, Any]] = []
        # The same predicate `validate_chain` re-derives from the frozen draft:
        # one spelling of "a re-proof request exists for this act".
        if audit.reproof_delivery_due(flags, audit_policy["round_cap"]):
            # Exactly one reader invocation for this act and audit round.  The
            # list of neutral locations is retained on the Perlectio below;
            # no flag result can reopen this page's frozen calculation.
            #
            # The pixels are rebuilt here, for this act alone, from the same
            # sealed artifacts the Pass-B dossier was built from -- the
            # rebuilt dossier is discarded, and the double-run byte-identity
            # acceptance tests hold the determinism this relies on.
            page_key = tuple(basis["source_page_id"] for basis in row["bases"])
            if render_cache.get("key") != page_key:
                render_cache = {
                    "key": page_key,
                    "renders": _page_renders_for(context, row["bases"]),
                }
            _, reproof_pixels = dossier_module.build_reader_dossier(
                context,
                act_id=act_id,
                act_key=row["act"]["act_key"],
                regions=row["bases"],
                testimonia=row["testimonia"],
                regime=context.witness_context,
                page_renders=render_cache["renders"],
                witness_context=witness_context_table,
                act_attachment=row["attachment_view"],
                prior_draft=row["prior"],
                prior_draft_view="fed" if context.draft_fed else "withheld",
            )
            # Ordering constraint: `payload["text"]` and `payload["dossier"]`
            # are read here, BEFORE the re-proof result overwrites the final
            # text below — the request must carry the frozen semi-final its
            # locations index into, and the dossier must stay the sealed one.
            #
            # The instrument, delivered rather than only sealed. The draft is
            # already published, so its reference names bytes that exist and
            # carries their digest: the request the reader receives is bound to
            # the exact frozen semi-final its locations index into. `pass_kind`
            # stays routing -- everything the reader needs to know about this
            # pass is in the request (`reader.py`'s module docstring).
            #
            # The dossier goes through untouched. It used to travel as
            # `{**dossier, "semi_final_text": ...}`, which handed the reader an
            # object whose own `dossier_digest` no longer covered its contents.
            audit_request = audit.audit_request(
                act_key=row["act"]["act_key"],
                attempt_ordinal=payload["attempt_ordinal"],
                draft_ref=draft_ref,
                semi_final_text=payload["text"],
                flags=flags,
            )
            request_digest = audit.audit_digest(audit_request)
            # The producer enforces the delivery contract itself, so the
            # obligation binds whichever reader sits in the chair rather than
            # resting on each implementation remembering to call the seam's
            # validator (FixtureReader also calls it; twice is harmless).
            validate_audit_delivery(
                payload["dossier"], pass_kind="audit-reproof", audit_request=audit_request
            )
            reproof = reader.read(
                payload["dossier"],
                # The literal, not `audit.REPROOF_PASS_KIND`: every producer
                # call site spells its pass so `test_reader.py`'s pin can read
                # them out of this file and hold `reader.PASS_KINDS` to exactly
                # the set run.py calls. That pin is also what makes the literal
                # safe here -- a misspelling fails it rather than falling
                # through to the establishing branch.
                pass_kind="audit-reproof",
                delivered_pixels=reproof_pixels,
                # A copy, so a reader that mutated its input could not leave
                # the sealed digest describing an object that no longer exists
                # -- the exact shape of lie this seam was rebuilt to end.
                audit_request=copy.deepcopy(audit_request),
            )
            final_text = reproof["text"]
            pre_audit_text = payload["text"]
            if final_text != payload["text"]:
                payload["text"] = final_text
                payload["dissent"] = dissent_against(
                    final_text, dissent_testimonia(row["testimonia"], row["attachment_view"])
                )
                # self_revision was computed against the pre-audit Pass-B
                # reading (audit finding H6); an audit-changed text is the one
                # actually published, so the recorded self-revision must
                # describe *its* departure from Pass A, not a reading that
                # never left the Perlector.
                payload["self_revision"] = departures(final_text, row["prior"]["text"])
                # The truncation instrument is the same case as `self_revision`
                # and was the last field still describing a reading nobody
                # published: three of its four signals are computed over the
                # reading text, and `outcome` is derived from the record, so an
                # audit-changed text left both stating what the *pre-audit* text
                # looked like -- and the re-proof's own engine word on why it
                # stopped was dropped entirely, so a re-proof cut off mid-emission
                # could replace established text while the record still read
                # `complete` (ARCHITECTURE: "truncation is a failure, not an
                # output"; GOVERNANCE 10).
                #
                # Pass C may only ever make this worse -- see
                # `_audited_truncation`.
                payload["truncation"] = _audited_truncation(
                    pass_b=payload["truncation"],
                    declared_failure=row["declared_failure"],
                    text=final_text,
                    region_pixels=row["region_pixels"],
                    stop_reason=reproof["stop_reason"],
                )
                row["outcome"] = _resolve_outcome(
                    declared_failure=row["declared_failure"],
                    truncation_record=payload["truncation"],
                    text=final_text,
                )
                if row["outcome"] == "no-readable-text":
                    # One emptiness rubric everywhere: the Pass-B path empties
                    # `text` for this outcome (whitespace was never established
                    # ink) and attaches the whole-act gap so the absence
                    # travels with the witness evidence that corroborates it.
                    # A re-proof that turned the reading unreadable published
                    # neither -- an act saying "nothing readable here" while
                    # carrying whitespace as its text and no evidence of the
                    # absence.
                    final_text = ""
                    payload["text"] = ""
                    payload["gaps"] = _whole_act_gap(
                        row["testimonia"],
                        {
                            record["artifact_id"]: context.artifact_ref(
                                ATTESTATORES, "testimonium", record["artifact_id"]
                            )
                            for record in row["testimonia"]
                        },
                    )
                    payload["dissent"] = dissent_against(
                        "", dissent_testimonia(row["testimonia"], row["attachment_view"])
                    )
                    payload["self_revision"] = departures("", row["prior"]["text"])
                elif payload["gaps"] and all(
                    gap.get("position") == "whole-act" for gap in payload["gaps"]
                ):
                    # The symmetric direction: a Pass-B no-readable-text act
                    # carried the whole-act gap, and a re-proof that restored
                    # readable text would otherwise publish established text
                    # BESIDE a gap claiming the whole act is empty --
                    # validate_annotations refuses exactly that, so the valid
                    # re-proof could never publish. Only the whole-act shape
                    # clears; a legitimate narrower gap is not this case.
                    payload["gaps"] = []
            # After the projection, not before it: `validate_chain` recomputes
            # the change record from the draft's semi-final against the
            # PUBLISHED text, so the record must describe the projected text.
            changes = audit.change_record(pre_audit_text, final_text, flags)
        if unresolved:
            for flag in flags:
                start, end = flag["location"]["start"], flag["location"]["end"]
                if start < end:
                    uncertainty.append(
                        {"start": start, "end": end, "reason": "audit-round-cap-exhausted"}
                    )
        finding_payload = {
            "act_key": row["act"]["act_key"],
            "attempt_ordinal": payload["attempt_ordinal"],
            "page_id": page_id,
            "page_ids": page_ids,
            "round_cap": audit_policy["round_cap"],
            "policy": policy_record,
            "flags": flags,
            "change_record": changes,
            "uncertain_spans": uncertainty,
            "unresolved": unresolved,
        }
        audit.validate_finding(
            finding_payload,
            text=final_text,
            flag_text=draft_payload["semi_final_text"],
        )
        finding = context.publish(
            kind="audit-finding",
            subject_id=act_id,
            outcome="read",
            attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
            inputs=[draft_ref],
            payload=finding_payload,
        )
        finding_ref = context.input_ref(finding.relative_path)
        # R5b's uncertainty is carried on the existing Perlectio layer until
        # R8 reconciles the canonical export schema.  An unresolved flag never
        # silently remains a clean `read`: it becomes an explicit span and the
        # Recensor consumes the companion `unresolved` fact below.
        payload["uncertain_spans"] = [
            {"start": span["start"], "end": span["end"], "alternatives": [], "confidence": "low"}
            for span in uncertainty
        ]
        payload["audit"] = {
            "draft_ref": draft_ref,
            "finding_ref": finding_ref,
            "finding_digest": audit.audit_digest(finding_payload),
            "unresolved": unresolved,
            "reproofs": reproofs,
            # Which request the reader was actually handed, or `None` where
            # none was: an act with no flag has nothing to re-prove, and an act
            # whose sealed cap is spent records its flags as exhausted-cap
            # uncertainty instead of running a round it may not run. Both are
            # honest absences, and a record that could not tell them from a
            # delivered re-proof is the ambiguity `reproofs` alone used to
            # carry.
            "request_digest": request_digest,
        }
        reading_inputs = row["inputs"] + [draft_ref, finding_ref]
        # The producer and every later consumer use the same cross-record
        # validation. Run it before the Perlectio is published so a drifted
        # draft/finding relationship never becomes an unreadable artifact.
        audit.validate_chain(
            context.tree,
            {"payload": payload, "inputs": reading_inputs},
            act_id,
        )
        validate_reading_payload(
            payload,
            outcome=row["outcome"],
            fields=_PERLECTIO_FIELDS,
            run_id=context.tree.run_id,
            config_digest=context.config_digest,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )
        context.publish(
            kind="perlectio",
            subject_id=act_id,
            outcome=row["outcome"],
            attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
            inputs=reading_inputs,
            payload=payload,
        )

    if read == 0 and acknowledged == 0:
        raise ContractError("the Perlector read no act and acknowledged no held act")

    context.finish()
    return EXIT_COMPLETE


def _next_attempt(context, act_id: str, regions: list[dict]) -> int:
    """Which reading attempt this is, derived from the act rather than from history.

    Counting existing Perlectiones and adding one would make the answer depend on
    how many times the stage had been *invoked*, so a rerun of an unchanged run
    would append a reading nobody asked for and the Archetypus would then point at
    it. The reading attempt is instead a function of the act's own state: one
    reading of the proposal, and one more for each recovery region cut since. A
    rerun that changed nothing therefore recomputes the same ordinal, produces the
    same bytes, and is reused rather than rewritten.

    **The one attempt model, and the one reader for it.** Witness testimony does
    not appear in this derivation, and that is the model rather than an omission:
    a Testimonium is a clue that primes a reading, never the ink the reading is
    established from, so a second look by a witness does not make a second reading
    attempt exist (GOVERNANCE 3, 11). `pipeline/3_attestatores/run.py::reread_pass`
    therefore closes its own window at the reading rather than moving this number.

    Counted through `recovery_region_count`, the same shared reader the Recensor,
    Archetypus and Armarium ask, because this copy asked only whether an origin
    equalled `"recovery"` and silently counted every other value — including an
    unknown or malformed one — as zero. A resealed Designator tree carrying origin
    `"mystery"` was therefore read and published here at attempt 1 and became fatal
    only at the next stage, by which time this Perlectio was already immutable and
    the retry had nowhere to go. Refused before any model call or publication now
    (Sol-S5).
    """
    return recovery_region_count(act_id, regions) + 1


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
