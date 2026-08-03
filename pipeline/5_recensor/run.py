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
coverage record and used for exactly two things: marking an act under-witnessed,
and forcing the run's aggregate visibly partial. They never decide an act's
outcome, and no count of agreeing seats can change a reading.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contracts.errors import ContractError, FatalAccounting  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.outcomes import OutcomeClass, classify, witness_coverage  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    PERLECTOR,
    RECENSOR,
)
from common.seats.registry import ChairRegistry  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    expected_acts,
    latest_attempt,
    open_context,
    run_stage,
    scenario_for,
    stage_parser,
)


def recovery_budget(root: str = "config") -> dict:
    path = Path(root) / "recovery.toml"
    if not path.exists():
        raise ContractError(
            f"no {path}: the recovery loop refuses to run without a tracked, finite "
            "budget. An unbounded loop is how a system reconsiders itself forever"
        )
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    allowed = config["budget"]["fallback_recrop"] + config["budget"]["page_level_reread"]
    cap = config["absolute_cap"]
    if allowed > cap:
        raise ContractError(
            f"the configured recovery budget of {allowed} exceeds the absolute cap "
            f"of {cap}. The cap is a ruling, not a default"
        )
    return {"allowed": allowed, "absolute_cap": cap}


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


def chair_outcomes(context, act_id: str) -> dict[str, str]:
    """The current outcome per seat: the latest attempt, with its honest status.

    Derived, never stored as a pointer. A failed attempt 2 over a successful
    attempt 1 therefore reads as `failed`, with attempt 1 intact as history.
    """
    latest: dict[str, dict] = {}
    for record in artifacts_for(context, ATTESTATORES, "testimonium", act_id):
        chair = record["payload"]["seat"]
        ordinal = record["payload"]["attempt_ordinal"]
        if chair not in latest or ordinal > latest[chair]["payload"]["attempt_ordinal"]:
            latest[chair] = record
    return {chair: record["outcome"] for chair, record in latest.items()}


def recoveries_so_far(context, act_id: str) -> int:
    """Requests already made for this act, read from the artifacts themselves."""
    return len(artifacts_for(context, RECENSOR, "recovery-request", act_id))


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied seat/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, RECENSOR, registry_factory=registry_factory)
    budget = recovery_budget()

    scenario = scenario_for(context.fixture, context.scenario)
    floor = context.witness_floor

    held = 0
    for act in expected_acts(context):
        act_id, act_key = act["act_id"], act["act_key"]

        outcomes = chair_outcomes(context, act_id)
        missing = set(context.witness_seats) - set(outcomes)
        if missing:
            raise FatalAccounting(
                f"act {act_id} has no outcome for configured seat(s) {sorted(missing)}. "
                "Every configured seat gets an explicit outcome for every act"
            )
        coverage = witness_coverage(outcomes, floor)

        if act["outcome"] == "held":
            # The Designator could not mark this act out. There is no reading to
            # review and no recovery to request — recovery recovers coverage on
            # sealed ink, and this act's missing ink was never sealed. The act
            # still gets this stage's explicit outcome, so its terminal category
            # derives from a review like every other act's.
            hold, hold_path = designator_hold(context, act_id)
            context.publish(
                kind="review",
                subject_id=act_id,
                outcome="held-for-review",
                attempt=attempt_id(act_id, "recense", 1),
                inputs=[context.input_ref(hold_path)],
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": 1,
                    "reason": f"the Designator held this act: {hold['payload']['reason']}",
                    "coverage": coverage,
                    "recoveries_used": 0,
                    "budget_allowed": budget["allowed"],
                    "absolute_cap": budget["absolute_cap"],
                },
            )
            held += 1
            continue

        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
        if not readings:
            raise FatalAccounting(
                f"act {act_id} reached the Recensor with no reading at all. A unit "
                "in no terminal set is a fatal accounting imbalance (#10)"
            )

        # The seal's continuation claim against the regions the tree actually
        # holds. A claim with only one proposal region — drift, tampering, or a
        # future bug reintroducing the declared-not-cut gap — may not be
        # accepted: the reading it would bless covers one side of a page break
        # while the record says it covers the act.
        proposal_count = len(
            [
                record
                for record in artifacts_for(context, DESIGNATOR, "region", act_id)
                if record["payload"]["origin"] == "proposal"
            ]
        )
        continuation_shortfall = act["has_continuation"] and proposal_count < 2

        used = recoveries_so_far(context, act_id)
        wants_recovery = act_key in scenario["recover_acts"] and used == 0
        ordinal = used + 1

        if not continuation_shortfall and wants_recovery and used < budget["allowed"]:
            # The Recensor asks; the Designator cuts. Recording the request as an
            # artifact is what keeps the loop countable from the tree alone.
            context.publish(
                kind="recovery-request",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recover", ordinal),
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "reason": "the crop may be incomplete; an expanded recrop is requested",
                    "budget_allowed": budget["allowed"],
                    "budget_used": used,
                    "coverage": coverage,
                },
            )
            context.publish(
                kind="review",
                subject_id=act_id,
                outcome="recovery-requested",
                attempt=attempt_id(act_id, "recense", ordinal),
                payload={
                    "act_key": act_key,
                    "attempt_ordinal": ordinal,
                    "coverage": coverage,
                },
            )
            continue

        # Whether the reading *succeeded*, not merely whether one exists. The
        # check above asks only that `readings` is non-empty, and the Archetypus
        # copies `payload["text"]` out of whatever the latest reading is — so a
        # `truncated` or `failed` Perlectio carrying stale text was established
        # as the one text, and a `not-run` record crashed on the missing field.
        # GOALS 2 is accuracy against the ink; text nobody successfully read is
        # not a reading, and GOVERNANCE 2 says it may not vanish behind a
        # successful status either. So it is held, visibly, with the outcome named.
        latest = latest_attempt(readings, f"reading of {act_id}")
        reading_class = classify(PERLECTOR, latest["outcome"])

        if reading_class is not OutcomeClass.COMPLETED:
            outcome, reason = (
                "held-for-review",
                f"the latest reading is {latest['outcome']!r} ({reading_class.value}); "
                "accepting would establish text that nobody successfully read",
            )
            held += 1
        elif continuation_shortfall:
            outcome, reason = (
                "held-for-review",
                f"the seal claims a continuation but only {proposal_count} proposal "
                "region(s) exist; accepting would deliver part of an act as the act",
            )
            held += 1
        elif act_key in scenario["hold_acts"]:
            outcome, reason = "held-for-review", "the act did not reconcile and needs a human"
            held += 1
        elif wants_recovery:
            outcome, reason = (
                "held-for-review",
                f"recovery budget of {budget['allowed']} is spent; holding rather than "
                "re-rolling, because recovery recovers coverage and never quality",
            )
            held += 1
        else:
            outcome, reason = "accepted", "coverage and geometry reconcile"

        context.publish(
            kind="review",
            subject_id=act_id,
            outcome=outcome,
            attempt=attempt_id(act_id, "recense", ordinal),
            # The reading this outcome is actually about, not whichever artifact
            # id happened to sort first. `readings[0]` is manifest order, which is
            # a hash: after a recovery it could cite the superseded attempt's crop
            # as the basis for accepting the new one. Deterministic, and wrong.
            # `latest`, computed once above, rather than a second `latest_attempt`
            # over the same list. A completed reading must cite the regions it
            # read, and is indexed strictly so a missing basis stays a loud
            # failure. A held one need not: a `not-run` Perlectio carries no
            # `basis` key at all, and dereferencing it would turn an honest hold
            # into a traceback — the raw missing-field crash a reviewer filed.
            inputs=[
                context.input_ref(reference["image_path"])
                for reference in (
                    latest["payload"]["basis"]["regions"]
                    if reading_class is OutcomeClass.COMPLETED
                    else latest["payload"].get("basis", {}).get("regions", [])
                )
            ],
            payload={
                "act_key": act_key,
                "attempt_ordinal": ordinal,
                "reason": reason,
                "coverage": coverage,
                "recoveries_used": used,
                "budget_allowed": budget["allowed"],
                "absolute_cap": budget["absolute_cap"],
            },
        )

    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
