"""Armarium: where the output is written, and where the totals must reconcile.

The pipeline ends here, so this is the last place a missing act could hide. Every
act the proposal seal expected gets exactly one manifest category, and the
categories are the closed five the contracts define. An act in none of them is a
fatal accounting imbalance and stops the run — never a warning, never a shrug.

**What leaves carries the Perlector's established reading and nothing else.**
Witness testimony informed that reading and its digest-checked references and
provenance leave alongside the result; witness words never become the delivered
text. There is no branch in this file that could put a witness's words into a
delivered text, and that is the point rather than an accident of the fixture.

**Partial cannot look complete.** A held act has no Archetypus, so it has no text
to export; it appears in the review output, and the run's aggregate says `partial`
with every reason named. "Complete" is refused unless everything reconciles —
which includes the witness roster the run was authorized with, not only the acts.

    python pipeline/7_armarium/run.py --run-root <dir> --run-id <id>
"""

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Second, so it lands ahead of the repository root: `spec_from_file_location` does not
# put a loaded file's own directory on `sys.path`, and
# `pipeline/orchestrator/test_terminal_guards.py` loads this file exactly that way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armarium_export import (  # noqa: E402
    ARMARIUM_ARCHIVE_NAME,
    ArmariumProjection,
    build_armarium_bundle,
    edge_hold_pages_from_rows,
)

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.annotations import validate_annotations  # noqa: E402
from common.contracts.canonical import digest_bytes, digest_of, verify_self_hash  # noqa: E402
from common.contracts.envelope import validate_input_refs  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import is_well_formed  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    TEXT_STATUSES,
    ArmariumCategory,
    derive_record_text_status,
    require_approval,
    run_aggregate,
    terminal_category,
)
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
)
from common.contracts.uncertainty import from_perlectio  # noqa: E402
from common.contracts.uncertainty import validate as validate_uncertainty
from common.cross_capture_coverage import validate_cross_capture_coverage  # noqa: E402
from common.exemplar_boundary import (  # noqa: E402
    verify_exemplar_corpus_seal,
    verify_exemplar_crop_lineage,
    verify_sealed_page_pixels,
)
from common.physical_act_partition import validate_physical_act_partition  # noqa: E402
from common.residual_ink import edge_ink_from_runs  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    EXIT_HELD,
    expected_acts,
    latest_attempt,
    open_stage_context,
    reading_basis_regions,
    recovery_region_count,
    require_current_witness_basis,
    run_stage,
    stage_parser,
    submission_identity,
    unaddressed_chairs,
    validate_serving_provenance,
)

_SOURCE_CITATION_FIELDS = frozenset(
    {
        "declared_path",
        "declared_sha256",
        "declared_bytes",
        "ledger_sha256",
        "container_page_index",
    }
)
# Respelled rather than imported from `pipeline/6_archetypus/run.py`: stages talk
# only through `common/` (`pipeline/test_stage_import_boundaries.py`), and the
# export edge is meant to re-prove the producer's shape independently rather than
# inherit it. `test_cross_capture_cluster_path.py` drives a real Archetypus record
# through this projection, so the two spellings cannot drift unnoticed.
_LOGICAL_RECORD_FIELDS = frozenset(
    {
        "logical_act_id",
        "physical_page_components",
        "member_local_acts",
        "text",
        "text_hash",
        "status",
        "text_status",
        "regions",
        "provenance",
        "annotations",
        "uncertainty",
        "evidence_ref",
        "cross_capture_dissent_ref",
        "perlectio_ref",
        "recensor_ref",
        "self_hash",
    }
)
_LOGICAL_COMPONENT_FIELDS = frozenset({"physical_page_id", "required_capture_sha256s"})
_LOGICAL_MEMBER_FIELDS = frozenset(
    {"act_id", "act_key", "page_id", "page_ordinal", "source_sha256", "proposal_refs"}
)


def logical_act_projection_entry(
    record: dict, *, category: str, source_regions: list[dict], witnesses: list[dict]
) -> dict:
    """Project one clustered Archetypus record without selecting a member act.

    The mature bundle codecs call their stable identity columns ``act_id`` and
    ``act_key``.  For a logical record both are derived solely from
    ``logical_act_id``; neither may contain a capture-local member key.  This
    compatibility spelling is at the export edge only, while the immutable
    Archetypus record and its index retain the explicit logical field.
    """
    if not isinstance(record, dict) or set(record) != _LOGICAL_RECORD_FIELDS:
        raise SchemaRefusal(
            "logical Armarium projection received an Archetypus outside its closed schema; "
            "the projection is refused because added or missing fields can bypass conservation"
        )
    # A matching field set proves nothing about the bytes inside it, so this
    # boundary re-derives the record's own integrity the way the image-local one
    # does (`_archetypus_rows`'s `validate_record`, and
    # `verify_established_record`'s self-hash and text_hash checks). Without the
    # checks that follow, a forged, truncated, or memberless record projects as
    # an established logical act.
    if not verify_self_hash(record):
        raise SchemaRefusal(
            "logical Armarium projection record fails its self_hash; the projection is "
            "refused because member, text, and evidence bytes must remain bound at export"
        )
    logical_id = record["logical_act_id"]
    if not is_well_formed(logical_id) or not logical_id.startswith("pac_"):
        raise SchemaRefusal(
            "logical Armarium projection logical_act_id is not a physical-act identity; the "
            "projection is refused because a normalization trick or local id cannot key a "
            "clustered export"
        )
    if (
        not isinstance(record["physical_page_components"], list)
        or not record["physical_page_components"]
    ):
        raise SchemaRefusal("logical Armarium projection has no retained physical page components")
    if not isinstance(record["member_local_acts"], list) or not record["member_local_acts"]:
        raise SchemaRefusal("logical Armarium projection has no retained local members")
    components = record["physical_page_components"]
    component_pages = []
    component_sources: set[str] = set()
    for component in components:
        if not isinstance(component, dict) or set(component) != _LOGICAL_COMPONENT_FIELDS:
            raise SchemaRefusal(
                "logical Armarium projection has a component outside its closed schema; the "
                "projection is refused because its capture denominator cannot be verified"
            )
        page = component["physical_page_id"]
        captures = component["required_capture_sha256s"]
        if (
            not is_well_formed(page)
            or not page.startswith("ppg_")
            or not isinstance(captures, list)
            or not captures
            # Element types before `set(...)`: an unhashable member would raise
            # TypeError out of the dedupe itself; the projection must refuse.
            or any(
                not isinstance(source, str)
                or len(source) != 64
                or any(character not in "0123456789abcdef" for character in source)
                for source in captures
            )
            or captures != sorted(set(captures))
        ):
            raise SchemaRefusal(
                "logical Armarium projection has a malformed component identity or capture "
                "set; the projection is refused because required evidence is not canonical"
            )
        component_pages.append(page)
        component_sources.update(captures)
    if component_pages != sorted(set(component_pages)):
        raise SchemaRefusal(
            "logical Armarium projection repeats or misorders a physical-page component; the "
            "projection is refused because one component cannot count twice"
        )
    members = record["member_local_acts"]
    member_ids = []
    member_keys = []
    for member in members:
        if not isinstance(member, dict) or set(member) != _LOGICAL_MEMBER_FIELDS:
            raise SchemaRefusal(
                "logical Armarium projection has a member outside its closed lineage schema; "
                "the projection is refused because every local proposal must remain named"
            )
        act_id_value = member["act_id"]
        act_key_value = member["act_key"]
        page_id_value = member["page_id"]
        ordinal = member["page_ordinal"]
        proposal_refs = member["proposal_refs"]
        source_value = member["source_sha256"]
        if (
            not is_well_formed(act_id_value)
            or not act_id_value.startswith("act_")
            # Element type before the `set(...)` over `source_sha256` below, as
            # with the component captures: an unhashable digest would raise
            # TypeError out of the dedupe and end the whole export run instead
            # of refusing this one record by name.
            or not isinstance(source_value, str)
            or len(source_value) != 64
            or any(character not in "0123456789abcdef" for character in source_value)
            or not is_well_formed(page_id_value)
            or not page_id_value.startswith("pg_")
            or not isinstance(act_key_value, str)
            or not act_key_value
            or not act_key_value.isprintable()
            or unicodedata.normalize("NFC", act_key_value) != act_key_value
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
            or not isinstance(proposal_refs, list)
            or not proposal_refs
            # Element types before `set(...)`, as with the component captures.
            or not all(isinstance(reference, str) and reference for reference in proposal_refs)
            or proposal_refs != sorted(set(proposal_refs))
        ):
            raise SchemaRefusal(
                "logical Armarium projection has malformed member identity, key, ordinal, "
                "source capture digest, or proposal evidence; the projection is refused "
                "because member lineage is not canonical"
            )
        member_ids.append(act_id_value)
        member_keys.append(act_key_value)
    if member_ids != sorted(set(member_ids)) or len(member_keys) != len(set(member_keys)):
        raise SchemaRefusal(
            "logical Armarium projection repeats a member id/key or misorders its members; "
            "the projection is refused because each proposal row must be conserved once"
        )
    member_sources = {member["source_sha256"] for member in members}
    if not member_sources <= component_sources:
        raise SchemaRefusal(
            "logical Armarium projection has a member outside its required capture set; the "
            "projection is refused because local ink cannot escape the clustered denominator"
        )
    text = record["text"]
    if not isinstance(text, str) or record["text_hash"] != digest_of(text):
        raise SchemaRefusal("logical Armarium projection text is not its one hashed string")
    if record["status"] != "established":
        raise SchemaRefusal("logical Armarium projection status is not the established literal")
    if not isinstance(record["regions"], list) or not record["regions"]:
        raise SchemaRefusal(
            "logical Armarium projection record has no source regions; the projection is "
            "refused because established text must stay anchored to ink"
        )
    if not isinstance(source_regions, list) or any(
        not isinstance(region, dict) for region in source_regions
    ):
        raise SchemaRefusal(
            "logical Armarium projection has malformed enriched source regions; the projection "
            "is refused because its export citations cannot be matched to established crops"
        )
    retained_regions = [
        {key: value for key, value in region.items() if key not in _SOURCE_CITATION_FIELDS}
        for region in source_regions
    ]
    if retained_regions != record["regions"]:
        raise SchemaRefusal(
            "logical Armarium projection source regions do not equal the established region "
            "basis; the projection is refused because no crop may vanish or be substituted "
            "while source-file citations are attached"
        )
    if not isinstance(record["provenance"], dict) or not record["provenance"]:
        raise SchemaRefusal(
            "logical Armarium projection record has no model provenance; the projection is "
            "refused because the reader identity and revision must travel with text"
        )
    validate_uncertainty(record["uncertainty"], text)
    if validate_annotations(
        record["annotations"], text, None, "logical Armarium annotation"
    ) != record["annotations"] or record["text_status"] != derive_record_text_status(
        text, record["annotations"], record["uncertainty"]
    ):
        raise SchemaRefusal(
            "logical Armarium projection damage layers disagree with the one text; the "
            "projection is refused because it would describe a different reading"
        )
    # The dissent is a sibling evidence record, never a text source.  Its
    # validation here is a one-way consumer check: nothing is passed back into
    # the Archetypus constructor.
    parent_refs = [
        record["cross_capture_dissent_ref"],
        record["perlectio_ref"],
        record["recensor_ref"],
    ]
    try:
        validate_input_refs(parent_refs)
    except SchemaRefusal as error:
        raise SchemaRefusal(
            "logical Armarium projection has a malformed or repeated parent reference; the "
            "projection is refused because dissent, reading, and review must remain distinct "
            "digest-bound evidence"
        ) from error
    if len({reference["relative_path"] for reference in parent_refs}) != 3:
        raise SchemaRefusal(
            "logical Armarium projection reuses one parent artifact path; the projection is "
            "refused because dissent, reading, and review are distinct evidence records"
        )
    if category != ArmariumCategory.DELIVERED.value:
        raise SchemaRefusal("logical Armarium projection only projects an established record")
    # Consult §5.2: "every member local act and every capture/page attribution
    # retained under that one logical entry."  Without it a clustered bundle
    # exports one act row whose member captures appear nowhere in it, and the
    # second capture's local act is simply gone -- GOVERNANCE 2's silent loss at
    # the last boundary.  Every member is carried, in canonical set order; none
    # is promoted to the row's identity, which stays derived from
    # `logical_act_id` alone.  §7.15's duplicate export -- a member act beside
    # its own logical act -- is refused against exactly this list by
    # `armarium_export._validate_projection`, so a member dropped here would
    # also drop out of that check.  The loop above has already proved every
    # member's id, key and ordinal, so this projects checked rows rather than
    # validating them again.
    membership = {
        "member_local_act_ids": sorted(member["act_id"] for member in members),
        "member_act_keys": sorted(member["act_key"] for member in members),
        "member_source_page_ordinals": sorted({member["page_ordinal"] for member in members}),
        "physical_page_components": record["physical_page_components"],
    }
    return {
        "act_id": logical_id,
        "act_key": f"logical:{logical_id}",
        "logical_act_id": logical_id,
        "logical_membership": membership,
        "category": category,
        "canonical_clean_text": record["text"],
        "text_status": record["text_status"],
        "transcription_annotations": record["annotations"],
        "provenance": record["provenance"],
        "source_regions": source_regions,
        "reason": None,
        # Reading, review, and dissent, as the held path cites all three: a
        # reader following evidence_refs from a delivered logical act must
        # reach the review that accepted it, not only the sibling dissent.
        "evidence_refs": sorted(
            (
                record["recensor_ref"],
                record["perlectio_ref"],
                record["cross_capture_dissent_ref"],
            ),
            key=lambda reference: reference["relative_path"],
        ),
        "witnesses": witnesses,
        "perlectio_ref": record["perlectio_ref"],
        "recensor_ref": record["recensor_ref"],
        "dissent_ref": record["cross_capture_dissent_ref"],
        "uncertainty": record["uncertainty"],
    }


def logical_cross_capture_review_entry(
    *,
    partition: dict,
    logical_act: dict,
    review: dict,
    review_ref: dict[str, str],
    witness_coverage: dict,
    witnesses: list[dict],
) -> dict:
    """Project a clustered Recensor hold as one text-free review item."""
    checked_partition = validate_physical_act_partition(partition)
    if checked_partition["findings"]:
        raise SchemaRefusal(
            "logical Armarium review projection received an unresolved physical-act "
            "partition; the review row is refused because no act census may be claimed over "
            "an unresolved equivalence relation"
        )
    if not isinstance(logical_act, dict):
        raise SchemaRefusal(
            "logical Armarium review projection has no partition row; the review row is "
            "refused because its logical subject and members are unknown"
        )
    logical_id = logical_act.get("logical_act_id")
    rows = [row for row in checked_partition["logical_acts"] if row["logical_act_id"] == logical_id]
    if len(rows) != 1 or rows != [logical_act]:
        raise SchemaRefusal(
            "logical Armarium review projection row is not the row its partition publishes; "
            "the review row is refused because member provenance cannot be supplied by a "
            "foreign or retracted partition row"
        )
    try:
        validate_input_refs([review_ref])
    except SchemaRefusal as error:
        raise SchemaRefusal(
            "logical Armarium review projection has no digest-bound Recensor reference; the "
            "review row is refused because its terminal decision cannot be cited"
        ) from error
    if not isinstance(review, dict) or digest_of(review) != review_ref["sha256"]:
        raise SchemaRefusal(
            "logical Armarium review projection bytes do not match the Recensor reference; "
            "the review row is refused because one decision cannot travel beside another's "
            "digest"
        )
    payload = review.get("payload")
    if review.get("outcome") != "held-for-review" or not isinstance(payload, dict):
        raise SchemaRefusal(
            "logical Armarium review projection did not receive a held-for-review payload; "
            "the review row is refused because delivered and held acts have different "
            "terminal paths"
        )
    reason = payload.get("reason")
    coverage = payload.get("cross_capture_coverage")
    if (
        not isinstance(reason, str)
        or not reason
        or not isinstance(coverage, dict)
        or coverage.get("logical_act_id") != logical_id
        or not isinstance(coverage.get("findings"), list)
        or not coverage["findings"]
    ):
        raise SchemaRefusal(
            "logical Armarium review projection has no named cross-capture finding and "
            "reason for this act; the review row is refused because a hold without its cause "
            "would lose the finding at export"
        )
    finding_facts: list[tuple[str, str]] = []
    for finding in coverage["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"code", "physical_page_id"}
            or not isinstance(finding["code"], str)
            or not finding["code"]
            or not isinstance(finding["physical_page_id"], str)
            or not finding["physical_page_id"]
        ):
            raise SchemaRefusal(
                "logical Armarium review projection has a malformed cross-capture finding; "
                "the review row is refused because every finding must name both its code "
                "and physical-page component"
            )
        finding_facts.append((finding["code"], finding["physical_page_id"]))
    if len(finding_facts) != len(set(finding_facts)):
        raise SchemaRefusal(
            "logical Armarium review projection repeats a cross-capture finding for one "
            "physical-page component; the review row is refused because duplicated evidence "
            "cannot inflate the terminal finding census"
        )
    finding_labels = [f"{code}:{page}" for code, page in sorted(finding_facts)]
    try:
        checked_coverage = validate_cross_capture_coverage(coverage)
    except (SchemaRefusal, TypeError) as error:
        raise SchemaRefusal(
            "logical Armarium review projection has malformed cross-capture coverage; the "
            "review row is refused because an unvalidated visibility record cannot account "
            "for the act's capture denominator"
        ) from error
    expected_components = {
        component["physical_page_id"]: component["required_capture_sha256s"]
        for component in logical_act["physical_page_components"]
    }
    measured_components = {
        component["physical_page_id"]: component["required_capture_sha256s"]
        for component in checked_coverage["components"]
    }
    if measured_components != expected_components:
        raise SchemaRefusal(
            "logical Armarium review projection coverage does not measure the partition's "
            "physical-page components and captures; the review row is refused because a "
            "finding about other evidence cannot hold this act"
        )
    # `under_witnessed` is required, not defaulted: a None here would let a
    # reader confuse "the witness floor was met" with "nobody measured the
    # witness floor" (GOVERNANCE 2). The image-local path indexes the key
    # directly for the same reason.
    if (
        not isinstance(witness_coverage, dict)
        or not isinstance(witness_coverage.get("under_witnessed"), bool)
        or not isinstance(witnesses, list)
    ):
        raise SchemaRefusal(
            "logical Armarium review projection has malformed witness accounting; the review "
            "row is refused because cross-capture coverage cannot replace the chair denominator"
        )
    members = logical_act["member_local_acts"]
    # Canonical set order, which `armarium_export._validate_logical_act_conservation`
    # requires of every membership list it is handed. A validated partition row is
    # already sorted-unique by act id, so sorting here changes nothing and keeps the
    # requirement visible where the list is built.
    membership = {
        "member_local_act_ids": sorted(member["act_id"] for member in members),
        "member_act_keys": sorted(member["act_key"] for member in members),
        "member_source_page_ordinals": sorted({member["page_ordinal"] for member in members}),
        "physical_page_components": logical_act["physical_page_components"],
    }
    perlectio_ref = payload.get("perlectio_ref")
    dissent_ref = payload.get("cross_capture_dissent_ref")
    if perlectio_ref is None or dissent_ref is None:
        raise SchemaRefusal(
            "logical Armarium review projection is missing its Perlectio or cross-capture "
            "dissent reference; the review row is refused because a visibility hold must "
            "retain both the reading and its sibling evidence"
        )
    # Two passes on purpose: each reference alone first, so a malformed one is
    # named as malformed, then the three together, so a repeated path is named as
    # a repeat. Collapsing them reports the wrong fault for one of the two.
    evidence_refs = [review_ref]
    for reference in (perlectio_ref, dissent_ref):
        try:
            validate_input_refs([reference])
        except SchemaRefusal as error:
            raise SchemaRefusal(
                "logical Armarium review projection has a malformed reading or dissent "
                "reference; the review row is refused because its evidence chain cannot "
                "be followed"
            ) from error
        evidence_refs.append(reference)
    try:
        validate_input_refs(evidence_refs)
    except SchemaRefusal as error:
        raise SchemaRefusal(
            "logical Armarium review projection repeats or contradicts an evidence path; the "
            "review row is refused because one artifact cannot stand for two parents"
        ) from error
    return {
        "act_id": logical_id,
        "act_key": f"logical:{logical_id}",
        "logical_act_id": logical_id,
        "logical_membership": membership,
        "category": ArmariumCategory.HELD_FOR_REVIEW.value,
        "canonical_clean_text": None,
        "text_status": None,
        "transcription_annotations": None,
        "provenance": None,
        "source_regions": [],
        "reason": f"{reason}; cross-capture finding(s): {', '.join(finding_labels)}",
        "evidence_refs": sorted(evidence_refs, key=lambda reference: reference["relative_path"]),
        "witnesses": witnesses,
        "perlectio_ref": perlectio_ref,
        "recensor_ref": review_ref,
        "dissent_ref": dissent_ref,
        "uncertainty": None,
        "under_witnessed": witness_coverage["under_witnessed"],
        "witness_coverage": witness_coverage,
        "cross_capture_coverage": coverage,
    }


def page_census(context) -> dict[int, dict]:
    """Every page's Exemplar outcome, by ordinal, reconciled against the sources.

    This is the page-level conservation check the pipeline was missing: the
    proposal seal only ever names acts that were marked out, so a page the door
    refused left no hole in the act-level accounting at all. The census closes
    that — every source the run declared must have exactly one page outcome, and
    a page with none, or with two, is invariant #10's imbalance at the last
    boundary.
    """
    declared_rows = context.run["source_manifest"]
    sources: dict[int, dict] = {}
    for source in declared_rows:
        ordinal = source.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting("the run declares a source without an integer ordinal")
        if ordinal in sources:
            raise FatalAccounting(f"the run declares source ordinal {ordinal} more than once")
        sources[ordinal] = source

    exemplar_manifest = context.tree.build_manifest(EXEMPLAR)
    census: dict[int, dict] = {}
    records: dict[int, dict] = {}
    entries_by_ordinal: dict[int, dict] = {}
    for entry in exemplar_manifest["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        ordinal = record["payload"].get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                f"page outcome {record['artifact_id']} carries no ordinal and cannot "
                "be reconciled against the sources that arrived"
            )
        if ordinal in census:
            raise FatalAccounting(f"page {ordinal} carries two Exemplar outcomes")
        source = sources.get(ordinal)
        if source is None:
            raise FatalAccounting(
                f"the Exemplar produced page ordinal {ordinal}, which run.json never submitted"
            )
        payload = record["payload"]
        if payload.get("declared_path") != source.get("relative_path") or payload.get(
            "declared_sha256"
        ) != source.get("sha256"):
            raise FatalAccounting(
                f"the Exemplar page for ordinal {ordinal} no longer matches its submitted "
                "filename and digest"
            )
        item = {
            "outcome": record["outcome"],
            "reason": payload.get("reason", ""),
            "declared_path": source["relative_path"],
            "declared_sha256": source["sha256"],
            "page_id": record["subject_id"] if record["outcome"] == "sealed" else None,
        }
        if record["outcome"] == "sealed":
            image_path, image_sha256 = payload.get("image_path"), payload.get("source_sha256")
            if not isinstance(image_path, str) or not isinstance(image_sha256, str):
                raise FatalAccounting(
                    f"the sealed Exemplar page for ordinal {ordinal} has no pixel reference"
                )
            item["image_path"] = image_path
            item["image_sha256"] = image_sha256
        if "bytes" in source:
            item["declared_bytes"] = source["bytes"]
        if "ledger_sha256" in source:
            item["ledger_sha256"] = source["ledger_sha256"]
        if source.get("container_page_index") is not None:
            if payload.get("container_page_index") != source["container_page_index"]:
                raise FatalAccounting(
                    f"the Exemplar page for ordinal {ordinal} no longer matches its submitted "
                    "container page index"
                )
            item["container_page_index"] = source["container_page_index"]
        census[ordinal] = item
        records[ordinal] = record
        entries_by_ordinal[ordinal] = entry
        if record["outcome"] == "sealed":
            try:
                verify_sealed_page_pixels(context.tree, context.run, source, record)
            except ContractError as error:
                raise FatalAccounting(
                    "the final Exemplar pixel boundary is not immutable; no export may be "
                    "written over altered source bytes"
                ) from error

    # Counted before it is compared as a set: a set comparison cannot tell two pages
    # sharing an ordinal from one page, and that is precisely the arithmetic by which
    # a lost page reconciles. `RunTree.create` refuses a repeated ordinal, but this is
    # the last boundary in the pipeline and it reads a `run.json` written earlier.
    declared_ordinals = [page["ordinal"] for page in declared_rows]
    declared = set(declared_ordinals)
    if len(declared) != len(declared_ordinals):
        raise FatalAccounting(
            f"the run declared {len(declared_ordinals)} source pages under only "
            f"{len(declared)} distinct ordinals; the run's own record cannot say "
            "how many pages arrived, so nothing downstream can balance against it"
        )
    if set(census) != declared:
        raise FatalAccounting(
            f"the run declared source pages {sorted(declared)} but the Exemplar "
            f"accounted for {sorted(census)}; a page in neither the sealed nor the "
            "refused set is a fatal accounting imbalance, never a warning"
        )
    try:
        verify_exemplar_corpus_seal(
            context.tree,
            context.run,
            exemplar_manifest,
            sources,
            records,
            entries_by_ordinal,
        )
    except ContractError as error:
        raise FatalAccounting(str(error)) from error
    return census


def ink_map_page_rows(
    context, census: dict[int, dict], claimed_bounds: dict[int, list[dict]]
) -> tuple[dict, ...]:
    """Re-measure the Ink Map against the Designator's actual cuts, and say so.

    The pre-proposal map cannot know whether edge ink belongs to an act. Its
    lossless page-space runs let this final boundary apply the Designator's
    recorded crop geometry to the *same measurement*, so a claimed edge mark
    releases and a genuinely unclaimed one remains visible for review.

    One row per sealed page, carrying what was found and what was re-measured
    rather than only the resulting hold. The export's clean-machine verifier
    recomputes the held set from these numbers with the ink map's own gate; a
    bare list of held ordinals would be a claim it could only check against
    itself. A page the map never flagged is re-measured by nobody and records
    `remeasured: None`, because writing zeros would record a measurement that
    never occurred. Every initial outcome is first reconciled with the retained
    runs against the original empty crop set, independently of the later
    re-measurement against Designator cuts.
    """
    found: dict[int, dict] = {}
    for entry in context.tree.build_manifest(INK_MAP)["artifacts"]:
        if entry["kind"] != "ink-map":
            continue
        record = context.tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
        payload = record.get("payload", {})
        ordinal = payload.get("page_ordinal") if isinstance(payload, dict) else None
        # Duplicate page records are refused because manifest order cannot
        # decide which retained finding controls the hold.
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                "ink-map has a record without an integer page ordinal. The Armarium cannot bind "
                "its finding to a sealed page. Restore the sealed Ink Map inventory or restart "
                "the run before exporting."
            )
        if ordinal in found:
            raise FatalAccounting(
                f"ink-map repeats page ordinal {ordinal}. The Armarium cannot choose which page "
                "record decides the edge hold. Restore the sealed Ink Map inventory or restart "
                "the run before exporting."
            )
        if record["outcome"] not in {"mapped", "unclaimed-edge-ink"}:
            raise FatalAccounting(
                "ink-map has an unknown page finding outcome. The Armarium cannot determine "
                "whether the page remains held. Rebuild the Ink Map under this version before "
                "exporting."
            )
        evidence = payload.get("edge_findings")
        if not isinstance(evidence, dict):
            raise FatalAccounting(
                "ink-map has no reusable page-space edge evidence. The Armarium cannot verify "
                "or release the page finding from a bare outcome. Restore the sealed Ink Map "
                "artifact or restart the run before exporting."
            )
        try:
            initial_measure = edge_ink_from_runs(evidence, [])
        except (KeyError, TypeError, ValueError) as error:
            raise FatalAccounting(
                f"ink-map page {ordinal} has unreadable retained page-space edge evidence. "
                "The Armarium cannot verify the page finding that decides whether edge ink "
                "must remain held. Restore the sealed Ink Map artifact or restart the run "
                "before exporting."
            ) from error
        measured_outcome = "unclaimed-edge-ink" if initial_measure["flagged"] else "mapped"
        if record["outcome"] != measured_outcome:
            raise FatalAccounting(
                f"ink-map page {ordinal} records outcome {record['outcome']!r}, but its retained "
                f"page-space evidence measures {measured_outcome!r}. The Armarium cannot choose "
                "between a page finding and the evidence meant to prove it. Repair or restart "
                "the Ink Map stage before exporting."
            )
        found[ordinal] = {"outcome": record["outcome"], "evidence": evidence}
    sealed = {ordinal for ordinal, page in census.items() if page.get("outcome") == "sealed"}
    if set(found) != sealed:
        raise FatalAccounting(
            "ink-map page denominator does not match the Armarium page census. At least one "
            "sealed page lacks a finding or an unsealed page gained one, so page coverage cannot "
            "reconcile. Restore the sealed stage inventories or restart the run before exporting."
        )
    rows = []
    for ordinal in sorted(found):
        finding = found[ordinal]
        remeasured = None
        if finding["outcome"] == "unclaimed-edge-ink":
            try:
                measure = edge_ink_from_runs(finding["evidence"], claimed_bounds.get(ordinal, []))
            except (KeyError, TypeError, ValueError) as error:
                raise FatalAccounting(
                    "ink-map page-space edge evidence cannot be re-measured. The Armarium cannot "
                    "decide whether later crops released the page hold. Restore the sealed Ink "
                    "Map artifact or restart the run before exporting."
                ) from error
            remeasured = {
                "total_ink_pixels": measure["total_ink_pixels"],
                "outside_ink_pixels": measure["outside_ink_pixels"],
                "edge_band_pixels": measure["edge_band_pixels"],
            }
        rows.append(
            {
                "ordinal": ordinal,
                "initial_outcome": finding["outcome"],
                "remeasured": remeasured,
            }
        )
    return tuple(rows)


def _cached_manifest(context, stage: str, manifest_cache: dict[str, dict]) -> dict:
    """``context.tree.build_manifest(stage)``, read and revalidated once per run.

    Reusing a manifest is only safe because Armarium never writes into
    DESIGNATOR/RECENSOR/PERLECTOR/ARCHETYPUS during its own run, so their artifact
    sets are static for the whole invocation. It is worth doing because
    ``build_manifest`` walks a stage's whole artifact directory and revalidates every
    envelope, binding and input each time it is called: on the two-act synthetic
    fixture one run did that fifteen times, against a stated scale of tens of
    thousands of acts.
    """
    if stage not in manifest_cache:
        manifest_cache[stage] = context.tree.build_manifest(stage)
    return manifest_cache[stage]


def pages_marked_out(context, manifest_cache: dict[str, dict]) -> dict[str, list[int]]:
    """Every page ordinal the Designator actually cut a region on, per act.

    The proposal seal names one primary `page_ordinal` per act, and that is not the
    same question. An act running over a page break is cut on both sides, so its
    far-side page *was* examined; a continuation page attributed to nobody would be
    reported as silent when it is the best-covered page in the run. The regions are
    the record of what was marked out, so they are what the page-coverage question
    is answered from.

    An act with no region at all — held because its own page never sealed — maps to
    an empty list rather than being absent. That is a fact about the act, not a gap
    in the attribution: the page it would have come from is refused in the census
    and named there.
    """
    marked: dict[str, list[int]] = {act["act_id"]: [] for act in expected_acts(context)}
    for entry in _cached_manifest(context, DESIGNATOR, manifest_cache)["artifacts"]:
        if entry["kind"] != "region":
            continue
        region = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        act_id = region["subject_id"]
        if act_id not in marked:
            raise FatalAccounting(
                f"the Designator cut a region for act {act_id}, which its own proposal seal "
                "does not expect; a crop of an act nobody declared is invariant #10's imbalance"
            )
        try:
            verified = verify_exemplar_crop_lineage(context.tree, context.run, region)
        except ContractError as error:
            raise FatalAccounting(
                f"the Designator region {entry['artifact_id']} cannot be verified as a crop of "
                "the Exemplar page it claims to mark out"
            ) from error
        ordinal = verified["source_page_ordinal"]
        if ordinal not in marked[act_id]:
            marked[act_id].append(ordinal)
    return {act_id: sorted(ordinals) for act_id, ordinals in marked.items()}


def claimed_bounds_by_page(context, manifest_cache: dict[str, dict]) -> dict[int, list[dict]]:
    """Verified capture rectangles are the only geometry that can release a finding.

    This walks the Designator regions a second time and verifies each crop
    again, after `pages_marked_out` has already verified the same ones. The
    duplicate decode is deliberate, not an oversight: the rectangles that
    *release* an edge hold are verified by the function that uses them, so a
    release can never rest on a verification performed somewhere else for
    another purpose. `test_unit14b_edge_release.py` pins that independence
    directly. Sharing one cached verification between the two walks would keep
    those tests green while removing the property in production, which is the
    shape of regression this stage exists to refuse.
    """
    claimed: dict[int, list[dict]] = {}
    for entry in _cached_manifest(context, DESIGNATOR, manifest_cache)["artifacts"]:
        if entry["kind"] != "region":
            continue
        region = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        try:
            verified = verify_exemplar_crop_lineage(context.tree, context.run, region)
        except ContractError as error:
            raise FatalAccounting(
                f"the Designator region {entry['artifact_id']} cannot be verified as a crop of "
                "the Exemplar page it claims to mark out. Its bounds therefore cannot release "
                "any page ink. Restore the sealed Designator artifact or restart the run before "
                "exporting."
            ) from error
        transform = region.get("payload", {}).get("transform")
        bounds = transform.get("bounds") if isinstance(transform, dict) else None
        if not isinstance(bounds, dict):
            raise FatalAccounting(
                "a verified Designator region has no crop bounds. It cannot claim any page ink, "
                "so using it to release an edge hold would be unsupported. Restore the sealed "
                "Designator artifact or restart the run before exporting."
            )
        claimed.setdefault(verified["source_page_ordinal"], []).append(bounds)
    return claimed


def artifacts_for(
    context, stage: str, kind: str, subject: str, manifest_cache: dict[str, dict]
) -> list[dict]:
    records = []
    for entry in _cached_manifest(context, stage, manifest_cache)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def export_witnesses(context, reading: dict, act_id: str) -> list[dict]:
    """Project the exact evidence-backed witnesses without exporting their words."""
    payload = reading.get("payload")
    basis = payload.get("basis") if isinstance(payload, dict) else None
    testimonia = basis.get("testimonia") if isinstance(basis, dict) else None
    if not isinstance(testimonia, list) or not testimonia:
        raise FatalAccounting("an established Perlectio has no witness basis to retain at export")

    dossier = payload.get("dossier") if isinstance(payload, dict) else None
    attachment = dossier.get("act_attachment") if isinstance(dossier, dict) else None
    # Required, like the witness basis above: an established reading that reaches
    # export without its act-attachment view is one whose page-witness custody was
    # never rechecked here. Guarding the check with `if attachment is not None`
    # left the export boundary opt-out. Found in audit; F-O2.
    if not isinstance(attachment, dict):
        raise FatalAccounting("an established Perlectio has no act-attachment evidence to retain")
    reference = attachment.get("reference")
    if not isinstance(reference, dict) or reference not in reading.get("inputs", []):
        raise FatalAccounting("an established Perlectio has no direct act-attachment evidence")
    context.tree.read_artifact_reference(
        reference,
        stage=ATTESTATORES,
        kind="act-attachment",
        subject_id=act_id,
    )

    witnesses: list[dict] = []
    seen_chairs: set[str] = set()
    for item in testimonia:
        if not isinstance(item, dict):
            raise FatalAccounting("an established Perlectio has a non-object witness basis entry")
        chair, outcome, reference = item.get("chair"), item.get("outcome"), item.get("reference")
        if not isinstance(chair, str) or not chair or not isinstance(outcome, str):
            raise FatalAccounting("an established Perlectio has an untyped witness basis entry")
        if chair in seen_chairs:
            raise FatalAccounting("an established Perlectio names one witness more than once")
        record = context.tree.read_artifact_reference(
            reference,
            stage=ATTESTATORES,
            kind="testimonium",
            subject_id=act_id,
        )
        testimony = record.get("payload")
        if (
            record.get("outcome") != outcome
            or not isinstance(testimony, dict)
            or testimony.get("chair") != chair
            or item.get("artifact_id") != record.get("artifact_id")
        ):
            raise FatalAccounting(
                "an established Perlectio's witness basis does not match its sealed Testimonium"
            )
        validate_serving_provenance(
            context,
            testimony.get("provenance"),
            producer_stage=ATTESTATORES,
            require_receipt=outcome in ATTEMPTED_WITNESS_OUTCOMES,
        )
        seen_chairs.add(chair)
        witnesses.append(
            {
                "chair": chair,
                "outcome": outcome,
                "testimonium_ref": reference,
                "provenance": testimony["provenance"],
            }
        )
    return sorted(witnesses, key=lambda witness: witness["chair"])


def export_source_regions(tree, regions: list[dict], census: dict[int, dict]) -> list[dict]:
    """Attach every delivered crop to the original filename-ledger page it used.

    A region's image digest proves the crop bytes, but an export needs the other
    half of the citation link too: which original source file/frame those bytes
    came from.  The Perlector retains the Designator transform's Exemplar page
    locator; reconcile it against the final census rather than trusting a bare
    ordinal in a downstream record.
    """
    linked: list[dict] = []
    for region in regions:
        if not isinstance(region, dict):
            raise FatalAccounting("an established reading has a non-object source region")
        ordinal = region.get("source_page_ordinal")
        page_id = region.get("source_page_id")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(page_id, str)
        ):
            raise FatalAccounting("an established source region has no Exemplar page locator")
        source = census.get(ordinal)
        if source is None or source.get("outcome") != "sealed":
            raise FatalAccounting(
                "an established source region names a page absent from the final sealed census"
            )
        if page_id != source.get("page_id"):
            raise FatalAccounting(
                "an established source region's Exemplar page id disagrees with the final census"
            )
        transform = region.get("transform")
        if (
            not isinstance(transform, dict)
            or transform.get("operation") != "crop"
            or transform.get("source_page_ordinal") != ordinal
            or transform.get("source_page_id") != page_id
            or not isinstance(transform.get("bounds"), dict)
        ):
            raise FatalAccounting(
                "an established source region does not retain its complete crop transform"
            )
        image_path, image_sha256 = region.get("image_path"), region.get("image_sha256")
        if not isinstance(image_path, str) or not isinstance(image_sha256, str):
            raise FatalAccounting("an established source region names no sealed crop")
        try:
            crop = tree.read_bytes(image_path)
        except OSError as error:
            raise FatalAccounting("an established source region's crop is missing") from error
        if digest_bytes(crop) != image_sha256:
            raise FatalAccounting("an established source region's crop bytes changed before export")
        entry = dict(region)
        for field in (
            "declared_path",
            "declared_sha256",
            "declared_bytes",
            "ledger_sha256",
            "container_page_index",
        ):
            if field in source:
                entry[field] = source[field]
        linked.append(entry)
    return linked


def categorize(
    context, act_id: str, manifest_cache: dict[str, dict]
) -> tuple[ArmariumCategory, dict, dict | None]:
    """One category per act, derived from the stages rather than decided here.

    The transition table is the authority: the Recensor's outcome either
    terminates the act or hands it to the Archetypus, whose outcome terminates it.
    This function routes; it does not judge.
    """
    reviews = artifacts_for(context, RECENSOR, "review", act_id, manifest_cache)
    if not reviews:
        raise FatalAccounting(f"act {act_id} reached the Armarium with no Recensor outcome")
    review = latest_attempt(reviews, f"review of {act_id}", operation="recense")

    terminal = terminal_category(RECENSOR, review["outcome"])
    if terminal is not None:
        # A held/refused/confirmed-blank/excluded act is terminal here, at the
        # Recensor, and the Archetypus's own guard already refuses to establish
        # one (pipeline/6_archetypus/run.py: "a stage may not resurrect a held
        # act"). An Archetypus record existing anyway is therefore an artifact
        # nothing published through that guard -- exactly the class of imbalance
        # invariant #10 exists to catch, checked here rather than trusted absent,
        # the same way an accepted act with no Archetypus is checked below.
        orphaned = artifacts_for(context, ARCHETYPUS, "archetypus", act_id, manifest_cache)
        if orphaned:
            raise FatalAccounting(
                f"act {act_id} is {review['outcome']!r} at the Recensor but carries an "
                "Archetypus record anyway; a non-accepted act may not also be established"
            )
        return terminal, review, None

    if review["outcome"] == "recovery-requested":
        raise FatalAccounting(
            f"act {act_id} has an outstanding recovery request; its recrop must be reread "
            "before an Archetypus can exist"
        )

    established = artifacts_for(context, ARCHETYPUS, "archetypus", act_id, manifest_cache)
    if not established:
        raise FatalAccounting(
            f"act {act_id} was accepted by the Recensor but has no Archetypus. It "
            "would leave the pipeline in no terminal set at all"
        )
    if len(established) != 1:
        raise FatalAccounting(
            f"act {act_id} was accepted by the Recensor but carries {len(established)} "
            "Archetypus records. There is no rule for choosing one established text"
        )
    record = established[0]
    return terminal_category(ARCHETYPUS, record["outcome"]), review, record


def verify_established_record(
    context, act: dict, review: dict, established: dict, manifest_cache: dict[str, dict]
) -> dict:
    """Reconcile an exportable Archetypus against its exact reviewed Perlectio.

    The Archetypus is the first record to call one reading established.  It must
    therefore carry a tamper-evident payload and digest-checked parents for both
    the Recensor decision and the Perlectio it decided about.  Reconstructing
    these links from whatever is latest would be a hidden selection of evidence.
    """
    payload = established.get("payload")
    if not isinstance(payload, dict) or not verify_self_hash(payload):
        raise FatalAccounting("an Archetypus payload fails its own self-hash before export")
    expected_scalars = {
        "act_id": act["act_id"],
        "act_key": act["act_key"],
        "page_id": act["page_id"],
        "status": "established",
    }
    if any(payload.get(field) != value for field, value in expected_scalars.items()):
        raise FatalAccounting("an Archetypus does not describe the act the export is categorizing")

    review_ref = payload.get("recensor_ref")
    reading_ref = payload.get("perlectio_ref")
    if not isinstance(review_ref, dict) or not isinstance(reading_ref, dict):
        raise FatalAccounting(
            "an Archetypus lacks digest-checked Recensor and Perlectio parent references"
        )
    expected_review_ref = context.artifact_ref(RECENSOR, "review", review["artifact_id"])
    if review_ref != expected_review_ref or review_ref not in established.get("inputs", []):
        raise FatalAccounting(
            "an Archetypus does not input the exact current Recensor review that accepted it"
        )
    checked_review = context.tree.read_artifact_reference(
        review_ref,
        stage=RECENSOR,
        kind="review",
        subject_id=act["act_id"],
    )
    if (
        checked_review["artifact_id"] != review["artifact_id"]
        or checked_review["outcome"] != "accepted"
    ):
        raise FatalAccounting("an Archetypus is not bound to an accepted Recensor review")

    review_payload = checked_review.get("payload")
    if not isinstance(review_payload, dict) or review_payload.get("perlectio_ref") != reading_ref:
        raise FatalAccounting(
            "an Archetypus names a Perlectio different from the one its Recensor review assessed"
        )
    if reading_ref not in established.get("inputs", []) or reading_ref not in checked_review.get(
        "inputs", []
    ):
        raise FatalAccounting("the Perlectio parent is not a direct sealed input at every handoff")
    reading = context.tree.read_artifact_reference(
        reading_ref,
        stage=PERLECTOR,
        kind="perlectio",
        subject_id=act["act_id"],
    )
    current = latest_attempt(
        artifacts_for(context, PERLECTOR, "perlectio", act["act_id"], manifest_cache),
        f"reading of {act['act_id']}",
        operation="perlegere",
    )
    if current["artifact_id"] != reading["artifact_id"]:
        raise FatalAccounting(
            f"act {act['act_id']} has a newer Perlectio than the one its Archetypus and "
            "Recensor review bind; export may not hide unreconciled evidence"
        )
    recovery_regions = recovery_region_count(
        act["act_id"], artifacts_for(context, DESIGNATOR, "region", act["act_id"], manifest_cache)
    )
    readings = artifacts_for(context, PERLECTOR, "perlectio", act["act_id"], manifest_cache)
    if len(readings) != recovery_regions + 1:
        raise FatalAccounting(
            f"act {act['act_id']} has {recovery_regions} recovery crop(s) but {len(readings)} "
            "Perlectio attempt(s); export may not complete over an unre-read recrop"
        )
    # The witness side of the same question, and the one route into this stage
    # that did not exist. Everything this stage says about an act is derived from
    # the latest Recensor review and from the reading's own basis references;
    # neither route passes back through `latest_per_chair`, so a Testimonium
    # appended after the reading was established was structurally invisible at
    # the point where the export decides to say `complete` -- and the sealed
    # export went on saying it (audit Opus-F2, 2d). GOVERNANCE 2 is
    # unconditional: `complete` is refused unless everything reconciles.
    require_current_witness_basis(
        act["act_id"],
        reading,
        artifacts_for(context, ATTESTATORES, "testimonium", act["act_id"], manifest_cache),
        f"the established Perlectio of {act['act_id']}",
    )
    reading_payload = reading.get("payload")
    if not isinstance(reading_payload, dict):
        raise FatalAccounting("an established Perlectio has no payload")
    reading_regions = reading_basis_regions(reading, f"established Perlectio of {act['act_id']}")
    # The older annotation layer, reconciled through the one shared validator
    # rather than by raw equality. The Archetypus NORMALIZES what it seals — an
    # `illegible` note may legally arrive without `witness_evidence` and the
    # sealed form always carries it — so comparing the sealed (normalized) copy
    # against the reading's raw one would refuse a perfectly correct record the
    # day the first real annotation is produced. Both sides go through
    # `validate_annotations` (witnesses=None: the roster lives in the reading's
    # basis, and attribution was already checked where it was sealed), and the
    # validated forms must be identical.
    try:
        sealed_annotations = validate_annotations(
            payload.get("annotations"), payload.get("text", ""), None, "Archetypus annotation"
        )
        reading_annotations = validate_annotations(
            reading_payload.get("annotations", []),
            payload.get("text", ""),
            None,
            "accepted Perlectio annotation",
        )
    except SchemaRefusal as error:
        raise FatalAccounting(
            "an Archetypus damage layer cannot be reconciled with its accepted Perlectio"
        ) from error
    if (
        payload.get("text") != reading_payload.get("text")
        or payload.get("regions") != reading_regions
        or payload.get("provenance") != reading_payload.get("provenance")
        or payload.get("dissent_ref") != reading_ref
        or sealed_annotations != reading_annotations
    ):
        raise FatalAccounting(
            "an Archetypus does not exactly preserve the Perlectio its review accepted"
        )
    try:
        expected_uncertainty = from_perlectio(reading_payload)
    except SchemaRefusal as error:
        raise FatalAccounting("an accepted Perlectio is malformed") from error
    try:
        if payload.get("uncertainty") != expected_uncertainty:
            raise FatalAccounting(
                "an Archetypus uncertainty layer differs from its accepted Perlectio"
            )
        validate_uncertainty(payload["uncertainty"], payload["text"])
    except SchemaRefusal as error:
        raise FatalAccounting("an Archetypus uncertainty layer is malformed") from error

    # Recomputed from the two damage layers just proven equal to the reading's own,
    # never read out of the record and believed. `text_status` is the field that
    # says whether the one established reading is whole, and export had no opinion
    # about it at all: a record could claim `established` over a text its own gap
    # list says was partly unread, and every other check here would pass. This is
    # also where that word enters the export -- the projection, the products, and
    # the run aggregate all take it from this checked value.
    try:
        expected_text_status = derive_record_text_status(
            payload.get("text"), payload.get("annotations"), payload.get("uncertainty")
        )
    except SchemaRefusal as error:
        raise FatalAccounting(
            "an Archetypus damage layer cannot be read for the status of its own text"
        ) from error
    if payload.get("text_status") != expected_text_status:
        raise FatalAccounting(
            f"an Archetypus claims text_status {payload.get('text_status')!r} over a reading "
            f"whose own gaps and annotations say {expected_text_status!r}; a damaged act may "
            "not leave the pipeline described as a whole one"
        )

    expected_inputs = [review_ref, reading_ref] + [
        context.input_ref(region["image_path"]) for region in reading_regions
    ]
    if sorted(established.get("inputs", []), key=lambda item: item["relative_path"]) != sorted(
        expected_inputs, key=lambda item: item["relative_path"]
    ):
        raise FatalAccounting("an Archetypus input set does not reconcile to its parent evidence")
    return payload


def missing_export_provenance(payload: object) -> str | None:
    """Name a reading that cannot travel as an exportable, cited result.

    This is deliberately narrower than the lineage checks in
    ``verify_established_record``.  A damaged envelope or a disagreement with a
    parent remains fatal accounting; an otherwise sealed established reading
    that simply lacks identity or region provenance is a refused unit with a
    visible review record, not a dropped one.
    """
    if not isinstance(payload, dict) or not verify_self_hash(payload):
        # This does not mean provenance is complete. A damaged envelope is fatal
        # accounting, and `verify_established_record` immediately raises on this same
        # self-hash. Returning None delegates to that check; callers must preserve the
        # order rather than treating this helper alone as an exportability decision.
        return None
    if not isinstance(payload.get("provenance"), dict):
        return "the established reading has no model identity provenance"
    regions = payload.get("regions")
    if not isinstance(regions, list) or not regions:
        return "the established reading has no source-region provenance"
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            return f"source region {index} is not an object"
        required = (
            "region_id",
            "image_path",
            "image_sha256",
            "source_page_ordinal",
            "source_page_id",
        )
        missing = [field for field in required if region.get(field) is None]
        if missing:
            return f"source region {index} lacks {', '.join(missing)} provenance"
    return None


def export_evidence_refs(context, review: dict, established: dict | None) -> list[dict[str, str]]:
    """The small, digest-checked record a review item keeps after text is refused."""
    references = [context.artifact_ref(RECENSOR, "review", review["artifact_id"])]
    if established is not None:
        references.append(
            context.artifact_ref(ARCHETYPUS, "archetypus", established["artifact_id"])
        )
    return sorted(references, key=lambda reference: reference["relative_path"])


def exclusion_approval_ref(act: dict, category: ArmariumCategory) -> str | None:
    """Carry an exclusion's recorded approval, or refuse it before export.

    **This refuses every exclusion today, approved or not.** ``act`` is one row of
    ``expected_acts()``, whose closed schema in ``common/stage.py`` has no
    ``approval_ref`` field for ``act.get("approval_ref")`` to find. That is a safe
    failure mode -- an export stops rather than admitting an unapproved exclusion --
    but not a working one, and the boundary is here so that a bare
    ``excluded-with-approval`` can never become an apparently complete export with
    its required citation silently missing.

    Making a real citation reach here needs a Designator-contract decision -- a
    widened ``expected_acts()`` row, or its own published artifact read the way an
    Archetypus record already is -- which is out of this stage's scope to invent.
    """
    if category is not ArmariumCategory.EXCLUDED_WITH_APPROVAL:
        return None
    approval_ref = act.get("approval_ref")
    require_approval(ARMARIUM, category.value, approval_ref)
    return approval_ref


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_stage_context(args, ARMARIUM, registry_factory=registry_factory)
    # The manifest's run identity: a fixture id on a fixture run, a real
    # submission's filename-ledger self-hash on a real one -- never both, never
    # neither. `submission_identity` itself decides which route this run took;
    # `context.fixture` is asked only once that has already ruled out real
    # ingress, so it is never touched on the route where it would refuse.
    submission_id = submission_identity(context.run)
    fixture_id = context.fixture["fixture_id"] if submission_id is None else None
    run_identity: dict[str, str] = (
        {"submission_id": submission_id}
        if submission_id is not None
        else {"fixture_id": fixture_id}
    )
    formats = context.armarium_formats
    if formats is None:
        raise FatalAccounting("Armarium has no format projection bound to the run configuration")
    # Verify the source ledger's final boundary before publishing even a reusable
    # manifest entry. A seal damaged after Designator must stop export at once.
    census = page_census(context)

    categories: dict[str, ArmariumCategory] = {}
    coverages: dict[str, dict] = {}
    # One entry per *delivered* act: what its Archetypus record says about its own
    # text. A held act has no record for a status to describe, so it has no entry
    # here, and `run_aggregate` refuses one.
    act_text_status: dict[str, str] = {}
    # The page each act was marked out on, straight from the proposal seal, so the
    # aggregate can tell a sealed page that produced nothing from one that produced
    # acts. Without it a silent page reconciles behind its busy neighbours.
    act_pages: dict[str, list[int]] = {}
    manifest_cache: dict[str, dict] = {}
    marked_out_pages = pages_marked_out(context, manifest_cache)
    delivered: list[dict] = []
    review_items: list[dict] = []
    projected_acts: list[dict] = []
    expected = expected_acts(context)

    for act in expected:
        act_key = act["act_key"]
        category, review, established = categorize(context, act["act_id"], manifest_cache)

        # The seal's own word is binding: an act the Designator held terminates
        # as held, and an export that categorized it any other way would have
        # quietly outvoted the record of why it could not be marked out.
        sealed_terminal = terminal_category(DESIGNATOR, act["outcome"])
        if sealed_terminal is not None and category is not sealed_terminal:
            raise FatalAccounting(
                f"act {act['act_id']} is {act['outcome']!r} at the proposal seal "
                f"(terminal category {sealed_terminal.value}) but the export "
                f"derived {category.value}; the seal and the export may not "
                "disagree about a terminal act"
            )

        entry = {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "category": category.value,
            "under_witnessed": review["payload"]["coverage"]["under_witnessed"],
            "witness_coverage": review["payload"]["coverage"],
            "evidence_refs": export_evidence_refs(context, review, established),
        }
        approval_ref = exclusion_approval_ref(act, category)
        if approval_ref is not None:
            entry["approval_ref"] = approval_ref

        if established is not None and category is ArmariumCategory.DELIVERED:
            refusal = missing_export_provenance(established.get("payload"))
            if refusal is None:
                payload = verify_established_record(
                    context, act, review, established, manifest_cache
                )
                try:
                    validate_serving_provenance(
                        context,
                        payload.get("provenance"),
                        producer_stage=PERLECTOR,
                        require_receipt=True,
                    )
                except SchemaRefusal as error:
                    refusal = f"the established reading's provenance was refused: {error}"
                else:
                    provenance = payload["provenance"]
                    source_regions = export_source_regions(context.tree, payload["regions"], census)
                    reading = context.tree.read_artifact_reference(
                        payload["perlectio_ref"],
                        stage=PERLECTOR,
                        kind="perlectio",
                        subject_id=act["act_id"],
                    )
                    witnesses = export_witnesses(context, reading, act["act_id"])
                    entry.update(
                        {
                            # The Archetypus's own field. Nothing else may reach here.
                            "text": payload["text"],
                            # The two things the record says *about* that text, which
                            # travel with it or the export describes a damaged act as
                            # a whole one: the status verified above, and the older
                            # annotation layer carried whole rather than replaced.
                            "text_status": payload["text_status"],
                            "transcription_annotations": payload["annotations"],
                            "provenance": provenance,
                            "source_regions": source_regions,
                            "perlectio_ref": payload["perlectio_ref"],
                            "recensor_ref": payload["recensor_ref"],
                            "witnesses": witnesses,
                            "dissent_ref": payload["dissent_ref"],
                            "uncertainty": payload["uncertainty"],
                        }
                    )
                    delivered.append(entry)
            if refusal is not None:
                category = ArmariumCategory.REFUSED_WITH_REASON
                entry["category"] = category.value
                entry["reason"] = refusal
                review_items.append(entry)
        elif established is not None:
            refused_payload = established.get("payload")
            entry["reason"] = (
                refused_payload.get("reason", "")
                if isinstance(refused_payload, dict)
                else review["payload"].get("reason", "")
            )
            review_items.append(entry)
        else:
            entry["reason"] = review["payload"].get("reason", "")
            review_items.append(entry)

        categories[act_key] = category
        coverages[act_key] = review["payload"]["coverage"]
        act_pages[act_key] = marked_out_pages[act["act_id"]]
        if category is ArmariumCategory.DELIVERED:
            canonical_clean_text = entry.get("text")
            if not isinstance(canonical_clean_text, str):
                raise FatalAccounting(
                    "a delivered Armarium entry has no literal Archetypus text; an export "
                    "may not substitute or fall back to another reading"
                )
            # Checked the same way and for the same reason as the literal beside it:
            # a delivered act reaching the aggregate with no status would be counted
            # as merely unmeasured, and this stage has just verified one.
            text_status = entry.get("text_status")
            if not isinstance(text_status, str) or text_status not in TEXT_STATUSES:
                raise FatalAccounting(
                    "a delivered Armarium entry has no established-text status; an act the "
                    "pipeline knows is damaged may not be aggregated as an unmeasured one"
                )
            act_text_status[act_key] = text_status
        else:
            canonical_clean_text = None
        projected_acts.append(
            {
                "act_id": entry["act_id"],
                "act_key": entry["act_key"],
                "category": category.value,
                # This is intentionally the same literal object value just read
                # from the Archetypus; no writer receives another text source.
                "canonical_clean_text": canonical_clean_text,
                # Beside the literal, never instead of it, and `None` for an act
                # with no established reading exactly as the literal is: an
                # absent status is "this act has no record", not "this act was
                # whole".
                "text_status": entry.get("text_status"),
                "transcription_annotations": entry.get("transcription_annotations"),
                "provenance": entry.get("provenance"),
                "source_regions": entry.get("source_regions", []),
                "reason": entry.get("reason"),
                "evidence_refs": entry["evidence_refs"],
                "witnesses": entry.get("witnesses", []),
                "perlectio_ref": entry.get("perlectio_ref"),
                "recensor_ref": entry.get("recensor_ref"),
                "dissent_ref": entry.get("dissent_ref"),
                "approval_ref": entry.get("approval_ref"),
                "uncertainty": entry.get("uncertainty"),
            }
        )

        context.publish(
            kind="manifest-entry",
            subject_id=act["act_id"],
            outcome=category.value,
            payload=entry,
            approval_ref=entry.get("approval_ref"),
        )

    unaddressed = list(unaddressed_chairs(context.registry.config))
    # The aggregate and terminal ledger must derive from the same edge holds;
    # computing the aggregate first could report complete beside a held page.
    ink_map_pages = ink_map_page_rows(
        context, census, claimed_bounds_by_page(context, manifest_cache)
    )
    aggregate = run_aggregate(
        categories,
        coverages,
        census,
        unaddressed_chairs=unaddressed,
        act_pages=act_pages,
        act_text_status=act_text_status,
        edge_hold_pages=edge_hold_pages_from_rows(ink_map_pages),
    )
    expected_count = len(expected)
    if len(categories) != expected_count:
        raise FatalAccounting(
            f"the seal expected {expected_count} acts and the export categorized "
            f"{len(categories)}. Conservation failed at the last boundary"
        )

    pages = [{"ordinal": ordinal, **census[ordinal]} for ordinal in sorted(census)]
    bundle = build_armarium_bundle(
        ArmariumProjection(
            fixture_id=fixture_id,
            submission_id=submission_id,
            scenario=context.scenario,
            config_digest=context.config_digest,
            aggregate=aggregate,
            acts=tuple(projected_acts),
            pages=tuple(pages),
            source_manifest=tuple(context.run["source_manifest"]),
            expected_acts=expected_count,
            witness_chairs=tuple(context.witness_chairs),
            witness_floor=context.witness_floor,
            aggregate_basis={
                "coverage_records": coverages,
                "unaddressed_chairs": unaddressed,
                "act_pages": act_pages,
                # Non-text, and the reason the exported `partial` over a damaged act
                # is recomputable on a clean machine rather than merely asserted.
                "act_text_status": act_text_status,
            },
            ink_map_pages=ink_map_pages,
        ),
        formats,
        context.tree.read_bytes,
    )
    bundle_digest, bundle_result = context.tree.put_blob(ARMARIUM, bundle.data)
    bundle_ref = context.input_ref(bundle_result.relative_path)

    # The terminal ledger's status, not the run aggregate's, is what this stage
    # reports. The ledger accounts three unit types -- source, sealed page, act --
    # and folds the aggregate's own reasons into its own, so it is never *less*
    # partial than the aggregate and is partial in one case the aggregate is not:
    # a sealed page whose acts all reached a completed category but disagree about
    # which one is `held-for-review` in the ledger (`_page_ledger_category` errs
    # toward "a human must look") while every act reconciles for `run_aggregate`.
    # Reporting the aggregate there would have exited 0 and published an `export`
    # outcome of `delivered` over a bundle whose own `claims.status` said `partial`
    # and named the held page -- GOVERNANCE 2's "a partial result is visibly
    # partial" broken by the run's own two measurements disagreeing. No run this
    # repository can produce reaches that case today, because the two categories it
    # needs come from Designator `excluded` and Recensor `confirmed-blank` outcomes
    # that no stage emits; it is reachable and proven at the projection boundary,
    # which is where spec 11's five-category accounting is proven at all.
    export_status = bundle.manifest["claims"]["status"]

    context.publish(
        kind="export",
        subject_id="export",
        outcome=(
            ArmariumCategory.DELIVERED.value
            if export_status == "complete"
            else ArmariumCategory.HELD_FOR_REVIEW.value
        ),
        payload={
            **run_identity,
            "scenario": context.scenario,
            "aggregate": aggregate,
            "expected_acts": expected_count,
            "delivered": sorted(delivered, key=lambda item: item["act_key"]),
            # Every non-delivered act, not only held/refused review items: a
            # confirmed-blank or excluded-with-approval act (COMPLETED-class) lands
            # here too. The bundle's own review-items.jsonl filters correctly to
            # held/refused (armarium_export.py::_review_records); this internal
            # accounting field is named for what it actually holds.
            "non_delivered": sorted(review_items, key=lambda item: item["act_key"]),
            # The page-level record beside the act-level one: every source the
            # run declared, with the Exemplar's outcome for it. A page that was
            # refused is named here and in the aggregate's reasons, never only
            # implied by an act count that came up short.
            "pages": pages,
            "witness_chairs": context.witness_chairs,
            "witness_floor": context.witness_floor,
            "bundle": {
                "filename": ARMARIUM_ARCHIVE_NAME,
                "format": "zip",
                "reference": bundle_ref,
                "sha256": bundle_digest,
                "manifest_member": "EXPORT_MANIFEST.json",
                "manifest_self_hash": bundle.manifest["self_hash"],
                "claims_status": export_status,
            },
        },
        inputs=[bundle_ref],
    )

    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE if export_status == "complete" else EXIT_HELD


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
