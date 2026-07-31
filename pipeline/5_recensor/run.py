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
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.outcomes import witness_coverage  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    PERLECTOR,
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

ADAPTER_REVISION = "fake-recensor-v0"


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


def seat_outcomes(context, act_id: str) -> dict[str, str]:
    """The current outcome per seat: the latest attempt, with its honest status.

    Derived, never stored as a pointer. A failed attempt 2 over a successful
    attempt 1 therefore reads as `failed`, with attempt 1 intact as history.
    """
    latest: dict[str, dict] = {}
    for record in artifacts_for(context, ATTESTATORES, "testimonium", act_id):
        seat = record["payload"]["seat"]
        ordinal = record["payload"]["attempt_ordinal"]
        if seat not in latest or ordinal > latest[seat]["payload"]["attempt_ordinal"]:
            latest[seat] = record
    return {seat: record["outcome"] for seat, record in latest.items()}


def recoveries_so_far(context, act_id: str) -> int:
    """Requests already made for this act, read from the artifacts themselves."""
    return len(artifacts_for(context, RECENSOR, "recovery-request", act_id))


def main() -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, RECENSOR, ADAPTER_REVISION)
    budget = recovery_budget()

    scenario = next(
        item for item in context.fixture["scenario"] if item["name"] == context.scenario
    )
    floor = context.fixture["witness_floor"]

    held = 0
    for act in expected_acts(context):
        act_id, act_key = act["act_id"], act["act_key"]

        readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
        if not readings:
            raise FatalAccounting(
                f"act {act_id} reached the Recensor with no reading at all. A unit "
                "in no terminal set is a fatal accounting imbalance (#10)"
            )

        outcomes = seat_outcomes(context, act_id)
        missing = set(context.witness_seats) - set(outcomes)
        if missing:
            raise FatalAccounting(
                f"act {act_id} has no outcome for configured seat(s) {sorted(missing)}. "
                "Every configured seat gets an explicit outcome for every act"
            )
        coverage = witness_coverage(outcomes, floor)

        used = recoveries_so_far(context, act_id)
        wants_recovery = act_key in scenario["recover_acts"] and used == 0
        ordinal = used + 1

        if wants_recovery and used < budget["allowed"]:
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

        if act_key in scenario["hold_acts"]:
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
            inputs=[
                context.input_ref(reference["image_path"])
                for reference in latest_attempt(readings, f"reading of {act_id}")["payload"][
                    "basis"
                ]["regions"]
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
