"""Attestatores: every configured seat reports, and every absence is a record.

The biggest change from the old stage is the write path. Attempts are append-only:
nothing overwrites attempt 1 to record attempt 2, and "current" is *derived* — the
latest attempt with its honest status — so a failed re-read shows as `failed` with
the earlier success intact and visible as history, never hidden behind it. That is
Tyrel's retention ruling of 2026-07-30 and GOVERNANCE 4.

Every configured seat gets an explicit outcome for every act, drawn from the closed
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

from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    expected_acts,
    open_context,
    run_stage,
    stage_parser,
)

ADAPTER_REVISION = "fake-attestatores-v0"


def proposed_regions(context, act_id: str) -> list[dict]:
    """Every region the Designator originally marked out for this act.

    Plural, because an act that runs over a page break has two of them, and a
    witness shown only the near side would have read half an act while the record
    said it had read the act.

    A Testimonium binds to the exact region it read, and a later recrop never
    silently inherits it (spec 07). Reading whichever region was *current* would
    do exactly that: after a recovery, the same seat-and-attempt identity would
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
            regions.append(context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"]))
    proposed = [record for record in regions if record["payload"]["origin"] == "proposal"]
    if not proposed:
        raise ContractError(f"act {act_id} has no proposed region for a witness to read")
    return sorted(proposed, key=lambda record: record["payload"]["attempt_ordinal"])


def declared_failures(context) -> set[tuple[str, str]]:
    return {
        (row["act_key"], row["seat"])
        for row in context.fixture.get("witness_failure", [])
        if row["scenario"] == context.scenario
    }


def testimony_for(context, act_key: str, seat: str) -> str | None:
    for row in context.fixture["testimony"]:
        if row["act_key"] == act_key and row["seat"] == seat:
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


def main() -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ATTESTATORES, ADAPTER_REVISION)
    failures = declared_failures(context)

    recorded = 0
    for act in expected_acts(context):
        if act["outcome"] == "held":
            # The Designator held this act: its proposal is incomplete, and a
            # witness shown only what exists would have read part of an act
            # while the record said it read the act. Every configured seat
            # still gets an explicit outcome — `not-run`, an unresolved unit —
            # because a seat that simply never appears is a silent skip, and a
            # silent skip is the shape of the original defect.
            for seat in context.witness_seats:
                context.publish(
                    kind="testimonium",
                    subject_id=act["act_id"],
                    outcome="not-run",
                    attempt=attempt_id(act["act_id"], f"read:{seat}", 1),
                    payload={
                        "seat": seat,
                        "act_key": act["act_key"],
                        "attempt_ordinal": 1,
                        "regions": [],
                        "provenance": {
                            "seat": seat,
                            "resolved_identity": f"{seat}@{ADAPTER_REVISION}",
                            "adapter_revision": ADAPTER_REVISION,
                        },
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

        for seat in context.witness_seats:
            reported = testimony_for(context, act["act_key"], seat)
            failed = (act["act_key"], seat) in failures

            if failed:
                outcome, payload = "failed", {"reason": "the seat returned no usable report"}
            elif reported is None:
                # Configured, nothing declared for it: not-run, which is an
                # unresolved unit and forces the run visibly partial. It is not
                # an empty reading and must never be counted as one.
                outcome, payload = "not-run", {"reason": "no attempt was made for this seat"}
            else:
                outcome, payload = "read", {"reported": reported}

            context.publish(
                kind="testimonium",
                subject_id=act["act_id"],
                outcome=outcome,
                attempt=attempt_id(act["act_id"], f"read:{seat}", 1),
                inputs=[context.input_ref(record["payload"]["image_path"]) for record in regions],
                payload={
                    "seat": seat,
                    "act_key": act["act_key"],
                    "attempt_ordinal": 1,
                    "regions": region_references,
                    "provenance": {
                        "seat": seat,
                        "resolved_identity": f"{seat}@{ADAPTER_REVISION}",
                        "adapter_revision": ADAPTER_REVISION,
                    },
                    # What this witness's output format can even express. A seat
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
        raise ContractError("no seat produced an outcome for any act")

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
