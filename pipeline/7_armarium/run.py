"""Armarium: where the output is written, and where the totals must reconcile.

The pipeline ends here, so this is the last place a missing act could hide. Every
act the proposal seal expected gets exactly one manifest category, and the
categories are the closed five the contracts define. An act in none of them is a
fatal accounting imbalance and stops the run — never a warning, never a shrug.

**What leaves carries the Perlector's established reading and nothing else.**
Witness testimony informed that reading and is retained inside the run as evidence;
it never appears as output. There is no branch in this file that could put a
witness's words into a delivered text, and that is the point rather than an
accident of the fixture.

**Partial cannot look complete.** A held act has no Archetypus, so it has no text
to export; it appears in the review output, and the run's aggregate says `partial`
with every reason named. "Complete" is refused unless everything reconciles —
which includes the witness roster the run was authorized with, not only the acts.

    python pipeline/7_armarium/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes, verify_self_hash  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    ArmariumCategory,
    run_aggregate,
    terminal_category,
)
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ARMARIUM,
    DESIGNATOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
)
from common.exemplar_boundary import (  # noqa: E402
    verify_exemplar_corpus_seal,
    verify_sealed_page_pixels,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    expected_acts,
    latest_attempt,
    open_context,
    reading_basis_regions,
    run_stage,
    stage_parser,
    unaddressed_chairs,
    validate_serving_provenance,
)


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

    # Counted, then compared as a set. `RunTree.create` refuses a manifest that
    # repeats an ordinal, so this should be unreachable — but the census is the
    # last boundary in the pipeline and it reads a `run.json` written earlier,
    # possibly by an older writer. A set comparison alone cannot see the
    # difference between two pages sharing an ordinal and one page, which is
    # exactly the arithmetic that lets a lost page reconcile.
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


def pages_marked_out(context) -> dict[str, list[int]]:
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
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        region = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        act_id = region["subject_id"]
        if act_id not in marked:
            raise FatalAccounting(
                f"the Designator cut a region for act {act_id}, which its own proposal seal "
                "does not expect; a crop of an act nobody declared is invariant #10's imbalance"
            )
        ordinal = region["payload"].get("transform", {}).get("source_page_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                f"the Designator region {entry['artifact_id']} names no integer source page "
                "ordinal, so the page it examined cannot be reconciled against the census"
            )
        if ordinal not in marked[act_id]:
            marked[act_id].append(ordinal)
    return {act_id: sorted(ordinals) for act_id, ordinals in marked.items()}


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


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


def categorize(context, act_id: str) -> tuple[ArmariumCategory, dict, dict | None]:
    """One category per act, derived from the stages rather than decided here.

    The transition table is the authority: the Recensor's outcome either
    terminates the act or hands it to the Archetypus, whose outcome terminates it.
    This function routes; it does not judge.
    """
    reviews = artifacts_for(context, RECENSOR, "review", act_id)
    if not reviews:
        raise FatalAccounting(f"act {act_id} reached the Armarium with no Recensor outcome")
    review = latest_attempt(reviews, f"review of {act_id}")

    terminal = terminal_category(RECENSOR, review["outcome"])
    if terminal is not None:
        # A held/refused/confirmed-blank/excluded act is terminal here, at the
        # Recensor, and the Archetypus's own guard already refuses to establish
        # one (pipeline/6_archetypus/run.py: "a stage may not resurrect a held
        # act"). An Archetypus record existing anyway is therefore an artifact
        # nothing published through that guard -- exactly the class of imbalance
        # invariant #10 exists to catch, checked here rather than trusted absent,
        # the same way an accepted act with no Archetypus is checked below.
        orphaned = artifacts_for(context, ARCHETYPUS, "archetypus", act_id)
        if orphaned:
            raise FatalAccounting(
                f"act {act_id} is {review['outcome']!r} at the Recensor but carries an "
                "Archetypus record anyway; a non-accepted act may not also be established"
            )
        return terminal, review, None

    established = artifacts_for(context, ARCHETYPUS, "archetypus", act_id)
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


def verify_established_record(context, act: dict, review: dict, established: dict) -> dict:
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
    reading_payload = reading.get("payload")
    if not isinstance(reading_payload, dict):
        raise FatalAccounting("an established Perlectio has no payload")
    reading_regions = reading_basis_regions(reading, f"established Perlectio of {act['act_id']}")
    if (
        payload.get("text") != reading_payload.get("text")
        or payload.get("regions") != reading_regions
        or payload.get("provenance") != reading_payload.get("provenance")
        or payload.get("dissent_ref") != reading["artifact_id"]
    ):
        raise FatalAccounting(
            "an Archetypus does not exactly preserve the Perlectio its review accepted"
        )

    expected_inputs = [review_ref, reading_ref] + [
        context.input_ref(region["image_path"]) for region in reading_regions
    ]
    if sorted(established.get("inputs", []), key=lambda item: item["relative_path"]) != sorted(
        expected_inputs, key=lambda item: item["relative_path"]
    ):
        raise FatalAccounting("an Archetypus input set does not reconcile to its parent evidence")
    return payload


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARMARIUM, registry_factory=registry_factory)
    # Verify the source ledger's final boundary before publishing even a reusable
    # manifest entry. A seal damaged after Designator must stop export at once.
    census = page_census(context)

    categories: dict[str, ArmariumCategory] = {}
    coverages: dict[str, dict] = {}
    # The page each act was marked out on, straight from the proposal seal, so the
    # aggregate can tell a sealed page that produced nothing from one that produced
    # acts. Without it a silent page reconciles behind its busy neighbours.
    act_pages: dict[str, list[int]] = {}
    marked_out_pages = pages_marked_out(context)
    delivered: list[dict] = []
    review_items: list[dict] = []

    for act in expected_acts(context):
        act_id = act["act_key"]
        category, review, established = categorize(context, act["act_id"])

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

        categories[act_id] = category
        coverages[act_id] = review["payload"]["coverage"]
        act_pages[act_id] = marked_out_pages[act["act_id"]]

        entry = {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "category": category.value,
            "under_witnessed": review["payload"]["coverage"]["under_witnessed"],
            "witness_coverage": review["payload"]["coverage"],
        }

        if established is not None:
            payload = verify_established_record(context, act, review, established)
            validate_serving_provenance(
                context,
                payload["provenance"],
                producer_stage=PERLECTOR,
                require_receipt=True,
            )
            entry.update(
                {
                    # The established reading, and nothing else. No witness text
                    # reaches this field by any path.
                    "text": payload["text"],
                    "provenance": payload["provenance"],
                    # The link back to the exact ink: every region, with the
                    # transform that produced it and the digest of its bytes.
                    "source_regions": export_source_regions(
                        context.tree, payload["regions"], census
                    ),
                    "dissent_ref": payload["dissent_ref"],
                }
            )
            delivered.append(entry)
        else:
            entry["reason"] = review["payload"].get("reason", "")
            review_items.append(entry)

        context.publish(
            kind="manifest-entry",
            subject_id=act["act_id"],
            outcome=category.value,
            payload=entry,
        )

    aggregate = run_aggregate(
        categories,
        coverages,
        census,
        unaddressed_chairs=unaddressed_chairs(context.registry.config),
        act_pages=act_pages,
    )
    expected_count = len(expected_acts(context))
    if len(categories) != expected_count:
        raise FatalAccounting(
            f"the seal expected {expected_count} acts and the export categorized "
            f"{len(categories)}. Conservation failed at the last boundary"
        )

    context.publish(
        kind="export",
        subject_id="export",
        outcome=(
            ArmariumCategory.DELIVERED.value
            if aggregate["status"] == "complete"
            else ArmariumCategory.HELD_FOR_REVIEW.value
        ),
        payload={
            "fixture_id": context.fixture["fixture_id"],
            "scenario": context.scenario,
            "aggregate": aggregate,
            "expected_acts": expected_count,
            "delivered": sorted(delivered, key=lambda item: item["act_key"]),
            "review": sorted(review_items, key=lambda item: item["act_key"]),
            # The page-level record beside the act-level one: every source the
            # run declared, with the Exemplar's outcome for it. A page that was
            # refused is named here and in the aggregate's reasons, never only
            # implied by an act count that came up short.
            "pages": [{"ordinal": ordinal, **census[ordinal]} for ordinal in sorted(census)],
            "witness_chairs": context.witness_chairs,
            "witness_floor": context.witness_floor,
        },
    )

    context.finish()
    return EXIT_COMPLETE if aggregate["status"] == "complete" else EXIT_HELD


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
