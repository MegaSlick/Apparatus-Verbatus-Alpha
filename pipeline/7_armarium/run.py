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
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    ArmariumCategory,
    run_aggregate,
    terminal_category,
)
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ARMARIUM,
    DESIGNATOR,
    RECENSOR,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    latest_attempt,
    open_context,
    run_stage,
    stage_parser,
)

ADAPTER_REVISION = "fake-armarium-v0"


def expected_acts(context) -> list[dict]:
    seal = context.tree.read_artifact(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    return seal["payload"]["expected_acts"]


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


def main() -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARMARIUM, ADAPTER_REVISION)

    categories: dict[str, ArmariumCategory] = {}
    coverages: dict[str, dict] = {}
    delivered: list[dict] = []
    review_items: list[dict] = []

    for act in expected_acts(context):
        act_id = act["act_key"]
        category, review, established = categorize(context, act["act_id"])
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

    aggregate = run_aggregate(categories, coverages)
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
            "witness_seats": context.witness_seats,
            "witness_floor": context.fixture["witness_floor"],
        },
    )

    context.finish()
    return EXIT_COMPLETE if aggregate["status"] == "complete" else EXIT_HELD


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
