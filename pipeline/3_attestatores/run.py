"""Attestatores: every configured chair reports, and every absence is a record.

The biggest change from the old stage is the write path. Attempts are append-only:
nothing overwrites attempt 1 to record attempt 2, and "current" is *derived* — the
latest attempt with its honest status — so a failed re-read shows as `failed` with
the earlier success intact and visible as history, never hidden behind it. That is
Tyrel's retention ruling of 2026-07-30 and GOVERNANCE 4.

Every configured chair gets an explicit outcome for every act, drawn from the closed
six-member vocabulary the contracts define. `failed` is the member Sol's finding
B-2 added: an attempt was made and produced no usable Testimonium, as against
`dead` (unavailable, never attempted) and `not-run` (configured, never tried). The
old stage collapsed every one of these into a single indistinguishable empty file.

The payload is the witness's own words, verbatim. Coercing testimony into a shared
body schema is where detail dies silently, so `reported` is stored as it arrived
and `content_health` is computed here rather than self-reported — a witness is not
asked to grade its own output.

    python pipeline/3_attestatores/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    expected_acts,
    fixture_serving_details,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)


def proposed_regions(context, act_id: str) -> list[dict]:
    """Every region the Designator originally marked out for this act.

    Plural, because an act that runs over a page break has two of them, and a
    witness shown only the near side would have read half an act while the record
    said it had read the act.

    A Testimonium binds to the exact region it read, and a later recrop never
    silently inherits it (spec 07). Reading whichever region was *current* would
    do exactly that: after a recovery, the same chair-and-attempt identity would
    describe different pixels, and the run tree rightly refuses to let one
    identity mean two things.

    So witnesses read what they were shown, and the ink a recovery uncovers is
    **witness-uncovered** — a fact the Perlector records rather than papers over.
    Spec 01 says the skeleton need not rerun witnesses during recovery; this is
    what that means in the record.
    """
    regions = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record["payload"]["provenance"],
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
            regions.append(record)
    proposed = [record for record in regions if record["payload"]["origin"] == "proposal"]
    if not proposed:
        raise ContractError(f"act {act_id} has no proposed region for a witness to read")
    return sorted(proposed, key=lambda record: record["payload"]["attempt_ordinal"])


def declared_failures(context) -> set[tuple[str, str]]:
    return {
        (row["act_key"], row["chair"])
        for row in context.fixture.get("witness_failure", [])
        if row["scenario"] == context.scenario
    }


def testimony_for(context, act_key: str, chair: str) -> str | None:
    for row in context.fixture["testimony"]:
        if row["act_key"] == act_key and row["chair"] == chair:
            return row["reported"]
    return None


def content_health(reported: str | None) -> dict:
    """Computed here, deterministically. Never self-reported by the witness."""
    if reported is None:
        return {"empty": True, "truncated": False, "characters": 0}
    return {
        "empty": reported.strip() == "",
        # A report ending mid-token is the shape of a truncation. The skeleton's
        # check is crude on purpose; what matters is that the channel exists and
        # is computed rather than trusted.
        "truncated": reported.endswith("-"),
        "characters": len(reported),
    }


def provenance_for(context, resolved: ChairIdentity | AbsentChair, *, attempted: bool) -> dict:
    """The exact configured identity for one witness outcome.

    An absent chair has no model identity and no serving moment. A configured chair
    gets a receipt only when it actually attempted a reading; `not-run` records
    retain the resolved pin but do not invent a serving event that never happened.
    """
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            "adapter_revision": context.adapter_revision,
        }
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("witness resolution returned neither an identity nor an absence")
    receipt_ref = (
        context.write_serving_receipt(resolved, fixture_serving_details(resolved))
        if attempted
        else None
    )
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": receipt_ref,
        "adapter_revision": context.adapter_revision,
    }


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run through the explicitly supplied chair implementation.

    Production passes the default registry. The test-only injection is a
    dependency seam, not a runtime choice among models or chairs.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ATTESTATORES, registry_factory=registry_factory)
    failures = declared_failures(context)

    recorded = 0
    for act in expected_acts(context):
        if act["outcome"] == "held":
            # The Designator held this act: its proposal is incomplete, and a
            # witness shown only what exists would have read part of an act
            # while the record said it read the act. Every configured chair
            # still gets an explicit outcome — `not-run`, an unresolved unit —
            # because a chair that simply never appears is a silent skip, and a
            # silent skip is the shape of the original defect.
            for chair in context.witness_chairs:
                resolved = context.registry.resolve(chair)
                context.publish(
                    kind="testimonium",
                    subject_id=act["act_id"],
                    outcome="not-run",
                    attempt=attempt_id(act["act_id"], f"read:{chair}", 1),
                    payload={
                        "chair": chair,
                        "act_key": act["act_key"],
                        "attempt_ordinal": 1,
                        "regions": [],
                        "provenance": provenance_for(context, resolved, attempted=False),
                        "format_capabilities": {
                            "can_express_uncertainty": False,
                            "can_express_layout": False,
                        },
                        "content_health": content_health(None),
                        "reason": (
                            "the Designator held this act; its incomplete proposal "
                            "was not shown to any witness"
                        ),
                    },
                )
                recorded += 1
            continue

        regions = proposed_regions(context, act["act_id"])
        region_references = [
            {
                "region_id": record["payload"]["region_id"],
                "image_path": record["payload"]["image_path"],
                "image_sha256": record["payload"]["image_sha256"],
            }
            for record in regions
        ]

        for chair in context.witness_chairs:
            resolved = context.registry.resolve(chair)
            reported = testimony_for(context, act["act_key"], chair)
            failed = (act["act_key"], chair) in failures

            if isinstance(resolved, AbsentChair):
                # An explicitly absent witness remains in the run roster and
                # therefore receives a visible outcome. Fixture testimony never
                # turns an absent chair into a different configured model.
                outcome = "not-run"
                payload = {"reason": f"chair is explicitly absent: {resolved.reason}"}
            elif failed:
                outcome, payload = "failed", {"reason": "the chair returned no usable report"}
            elif reported is None:
                # Configured, nothing declared for it: not-run, which is an
                # unresolved unit and forces the run visibly partial. It is not
                # an empty reading and must never be counted as one.
                outcome, payload = "not-run", {"reason": "no attempt was made for this chair"}
            else:
                outcome, payload = "read", {"reported": reported}

            # Derived from the outcome, never set beside it: the Perlector reads
            # the same set to decide whether a testimonium must carry a receipt,
            # and a producer and a consumer disagreeing about which outcomes mean
            # "a chair actually served" is a refusal nobody could act on.
            attempted = outcome in ATTEMPTED_WITNESS_OUTCOMES

            context.publish(
                kind="testimonium",
                subject_id=act["act_id"],
                outcome=outcome,
                attempt=attempt_id(act["act_id"], f"read:{chair}", 1),
                inputs=(
                    [context.input_ref(record["payload"]["image_path"]) for record in regions]
                    if attempted
                    else []
                ),
                payload={
                    "chair": chair,
                    "act_key": act["act_key"],
                    "attempt_ordinal": 1,
                    "regions": region_references if attempted else [],
                    "provenance": provenance_for(context, resolved, attempted=attempted),
                    # What this witness's output format can even express. A chair
                    # that cannot say "unsure" must not be read as confident.
                    "format_capabilities": {
                        "can_express_uncertainty": False,
                        "can_express_layout": False,
                    },
                    "content_health": content_health(payload.get("reported")),
                    **payload,
                },
            )
            recorded += 1

    if recorded == 0:
        raise ContractError("no chair produced an outcome for any act")

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
