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

from common.contracts.errors import FatalAccounting  # noqa: E402
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
from common.seats.registry import SeatRegistry  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    expected_acts,
    latest_attempt,
    open_context,
    run_stage,
    stage_parser,
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
    census: dict[int, dict] = {}
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        ordinal = record["payload"].get("ordinal")
        if not isinstance(ordinal, int):
            raise FatalAccounting(
                f"page outcome {record['artifact_id']} carries no ordinal and cannot "
                "be reconciled against the sources that arrived"
            )
        if ordinal in census:
            raise FatalAccounting(f"page {ordinal} carries two Exemplar outcomes")
        census[ordinal] = {
            "outcome": record["outcome"],
            "reason": record["payload"].get("reason", ""),
        }

    # Counted, then compared as a set. `RunTree.create` refuses a manifest that
    # repeats an ordinal, so this should be unreachable — but the census is the
    # last boundary in the pipeline and it reads a `run.json` written earlier,
    # possibly by an older writer. A set comparison alone cannot see the
    # difference between two pages sharing an ordinal and one page, which is
    # exactly the arithmetic that lets a lost page reconcile.
    declared_ordinals = [page["ordinal"] for page in context.run["source_manifest"]]
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
    return census


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


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


def main(registry_factory=SeatRegistry.from_toml) -> int:
    """Run under the explicitly supplied seat/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARMARIUM, registry_factory=registry_factory)

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
                    "source_regions": payload["regions"],
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

    census = page_census(context)
    aggregate = run_aggregate(categories, coverages, census)
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
            "witness_seats": context.witness_seats,
            "witness_floor": context.witness_floor,
        },
    )

    context.finish()
    return EXIT_COMPLETE if aggregate["status"] == "complete" else EXIT_HELD


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
