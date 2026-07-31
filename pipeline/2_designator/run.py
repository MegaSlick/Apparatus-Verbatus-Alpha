"""Designator: marks out the acts and cuts the crops. It establishes no text.

Two things it owns that nothing else may touch. **Crops** — the Recensor may
*request* a replacement region, but only this stage cuts one, so a crop always has
one author. And the **proposal seal**: an immutable record of every act this run
expects, emitted once, which becomes the downstream expected-act authority. Without
it, a later stage could only ask "did I account for the acts I happen to have seen"
rather than "did I account for the acts that were found", and an act lost between
stages would leave no hole to notice.

Regions are append-only per act, and each carries an `origin` saying what kind of
region it is: a **proposal** region is part of what was originally marked out — the
first crop, and a continuation on the next page, both — while a **recovery** region
is a recrop cut later at the Recensor's request. The distinction is load-bearing:
witnesses read the proposal regions, so ink that only a recovery uncovered was
never shown to a witness, and the Perlectio records that rather than papering over
it. A bare sequence number cannot express this, and reading one as an attempt count
made the witnesses skip the far side of a page break.

Act identity is bound to the *original proposal* and so is unchanged by any recrop;
the region identity is bound to the transform and so must change. ARCHITECTURE's
first invariant therefore falls out of the derivation rather than being maintained
by hand.

    python pipeline/2_designator/run.py --run-root <dir> --run-id <id>
    python pipeline/2_designator/run.py ... --operation recover --act <act_id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import attempt_id, region_id  # noqa: E402
from common.contracts.stages import DESIGNATOR, EXEMPLAR  # noqa: E402
from common.imaging import crop_png  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    act_bounds,
    act_identity,
    continuation_for,
    open_context,
    page_identity,
    run_stage,
    stage_parser,
)

ADAPTER_REVISION = "fake-designator-v0"


def sealed_pages(context) -> dict[int, dict]:
    """The pages the Exemplar actually sealed, by ordinal.

    Read from the Exemplar's artifacts rather than from the fixture, so a page the
    door refused is a page this stage genuinely does not see.
    """
    pages = {}
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page" or entry["outcome"] != "sealed":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        pages[record["payload"]["ordinal"]] = record
    return pages


def cut_region(context, act, page_record, bounds, ordinal, page_ordinal, origin):
    """Cut one region of one act and publish it.

    `origin` separates two things that a bare sequence number runs together. A
    **proposal** region is part of what the Designator originally marked out —
    including a continuation on the next page, which is a second region of the
    same act rather than a later attempt at it. A **recovery** region is a recrop
    cut later at the Recensor's request. Witnesses read the proposal regions;
    ink a recovery uncovers was never shown to them. Numbering alone cannot say
    which is which, and reading it as an attempt count made this stage skip the
    far side of a page break.
    """
    act_id = act_identity(context.fixture, act)
    image_path = page_record["payload"]["image_path"]
    page_bytes = context.tree.read_bytes(image_path)

    transform = {
        "operation": "crop",
        "source_page_ordinal": page_ordinal,
        "source_page_id": page_record["subject_id"],
        "bounds": bounds,
    }
    crop_bytes = crop_png(page_bytes, bounds)
    digest, stored = context.tree.put_blob(DESIGNATOR, crop_bytes)

    context.publish(
        kind="region",
        subject_id=act_id,
        outcome="proposed",
        attempt=attempt_id(act_id, "crop", ordinal),
        inputs=[context.input_ref(image_path)],
        payload={
            "region_id": region_id(act_id, transform),
            "act_key": act["key"],
            "attempt_ordinal": ordinal,
            "origin": origin,
            "transform": transform,
            "image_path": stored.relative_path,
            "image_sha256": digest,
        },
    )
    return act_id


def initial_pass(context) -> int:
    pages = sealed_pages(context)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    expected = []
    for act in context.fixture["act"]:
        page_ordinal = act["page_ordinal"]
        if page_ordinal not in pages:
            continue
        act_id = cut_region(
            context, act, pages[page_ordinal], act_bounds(act), 1, page_ordinal, "proposal"
        )

        # An act that runs over the page break gets a second region of the SAME
        # act. A continuation that became its own act would quietly turn one
        # entry into two and break identity where it is hardest to see.
        continuation = continuation_for(context.fixture, act["key"])
        if continuation and continuation["page_ordinal"] in pages:
            cut_region(
                context,
                act,
                pages[continuation["page_ordinal"]],
                {key: continuation[key] for key in ("x", "y", "w", "h")},
                2,
                continuation["page_ordinal"],
                "proposal",
            )

        expected.append(
            {
                "act_id": act_id,
                "act_key": act["key"],
                "page_id": page_identity(context.fixture, page_ordinal),
                "page_ordinal": page_ordinal,
                "has_continuation": bool(continuation),
            }
        )

    if not expected:
        raise ContractError("no act was marked out on any sealed page")

    # The seal, emitted once and never rewritten: this is what downstream stages
    # reconcile against, so "every expected act has exactly one outcome" is a
    # question with an answer.
    context.publish(
        kind="proposal-seal",
        subject_id="proposal-seal",
        outcome="proposed",
        payload={"expected_acts": expected, "count": len(expected)},
    )
    return len(expected)


def recovery_pass(context, act_id: str) -> int:
    """Cut one replacement region for one act, at the Recensor's request.

    The Recensor asked; the Designator cuts. Keeping the ownership straight is
    what stops the recovery loop from growing a second author for crops.
    """
    seal = context.tree.read_artifact(DESIGNATOR, "proposal-seal", _seal_artifact_id(context))
    match = [item for item in seal["payload"]["expected_acts"] if item["act_id"] == act_id]
    if not match:
        raise ContractError(f"recovery asked for {act_id}, which the proposal seal does not name")

    act = next(item for item in context.fixture["act"] if item["key"] == match[0]["act_key"])
    recovery = [row for row in context.fixture.get("recovery", []) if row["act_key"] == act["key"]]
    if not recovery:
        raise ContractError(f"the fixture declares no recovery region for act {act['key']}")

    pages = sealed_pages(context)
    bounds = {key: recovery[0][key] for key in ("x", "y", "w", "h")}
    ordinal = _next_region_ordinal(context, act_id)
    cut_region(
        context, act, pages[act["page_ordinal"]], bounds, ordinal, act["page_ordinal"], "recovery"
    )
    return 1


def _seal_artifact_id(context) -> str:
    from common.contracts.identities import artifact_id

    return artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)


def _next_region_ordinal(context, act_id: str) -> int:
    ordinals = [record["payload"]["attempt_ordinal"] for record in _regions_of(context, act_id)]
    return max(ordinals, default=0) + 1


def _regions_of(context, act_id: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            records.append(context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"]))
    return records


def main() -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, DESIGNATOR, ADAPTER_REVISION)

    if args.operation == "recover":
        if not args.act:
            raise ContractError("a recovery operation must name the act it is recovering")
        recovery_pass(context, args.act)
    else:
        initial_pass(context)

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
