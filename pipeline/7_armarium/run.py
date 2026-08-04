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
from common.contracts.canonical import digest_bytes  # noqa: E402
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
        return terminal, review, None

    established = artifacts_for(context, ARCHETYPUS, "archetypus", act_id)
    if not established:
        raise FatalAccounting(
            f"act {act_id} was accepted by the Recensor but has no Archetypus. It "
            "would leave the pipeline in no terminal set at all"
        )
    record = established[0]
    return terminal_category(ARCHETYPUS, record["outcome"]), review, record


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARMARIUM, registry_factory=registry_factory)
    # Verify the source ledger's final boundary before publishing even a reusable
    # manifest entry. A seal damaged after Designator must stop export at once.
    census = page_census(context)

    categories: dict[str, ArmariumCategory] = {}
    coverages: dict[str, dict] = {}
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

        entry = {
            "act_id": act["act_id"],
            "act_key": act["act_key"],
            "category": category.value,
            "under_witnessed": review["payload"]["coverage"]["under_witnessed"],
            "witness_coverage": review["payload"]["coverage"],
        }

        if established is not None:
            payload = established["payload"]
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
