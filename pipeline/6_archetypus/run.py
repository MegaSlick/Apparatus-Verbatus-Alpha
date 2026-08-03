"""Archetypus: exactly one established reading per act, written once.

The authoritative pipeline output — a machine reading, not truth. Three properties
make that claim honest rather than decorative.

**One text.** A single text field per act, written once. There is no second field
holding an alternative, no per-format variant, and no place for a witness's words
to sit beside the reading as an option. GOVERNANCE 5: one established text,
projected identically into every format.

**Write-once.** An Archetypus already on disk is never rewritten. The run tree
refuses different bytes under the same identity, so this is enforced a layer down
rather than promised here; what this stage adds is that it never tries.

**Only for acts the Recensor accepted.** A held act reaches no Archetypus at all.
That is the load-bearing half of "partial cannot look complete" — the absence is
the evidence, and an export that showed a held act as delivered would have to
invent a record that does not exist.

Human correction lives *above* this record, never inside it: the output is a
machine reading, and a corrected text is a different kind of thing.

    python pipeline/6_archetypus/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import self_hash  # noqa: E402
from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.outcomes import OutcomeClass, classify, terminal_category  # noqa: E402
from common.contracts.stages import ARCHETYPUS, DESIGNATOR, PERLECTOR, RECENSOR  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    expected_acts,
    latest_attempt,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def final_review(context, act_id: str) -> dict:
    """The Recensor's last word on this act."""
    return latest_attempt(artifacts_for(context, RECENSOR, "review", act_id), f"review of {act_id}")


def latest_reading(context, act_id: str) -> dict:
    return latest_attempt(
        artifacts_for(context, PERLECTOR, "perlectio", act_id), f"reading of {act_id}"
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied seat/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARCHETYPUS, registry_factory=registry_factory)

    for act in expected_acts(context):
        act_id = act["act_id"]
        review = final_review(context, act_id)

        # An act the seal already holds is terminal at the Designator. If the
        # Recensor nonetheless accepted it, establishing a text here would
        # resurrect a held act into a delivered one — refused before a single
        # character is written, because the Archetypus is the last stage before
        # the text exists.
        if terminal_category(DESIGNATOR, act["outcome"]) is not None and (
            review["outcome"] == "accepted"
        ):
            raise FatalAccounting(
                f"act {act_id} is {act['outcome']!r} at the proposal seal, but the "
                "Recensor accepted it; a stage may not resurrect a held act into "
                "an established reading"
            )

        if review["outcome"] != "accepted":
            # Deliberately nothing. A held act has no Archetypus, and that
            # absence is what the Armarium reconciles against.
            continue

        reading = latest_reading(context, act_id)
        # The Recensor now holds an act whose latest reading did not succeed, so
        # reaching here with a failed one means that check was bypassed or a
        # future edit removed it. This is the last stage before the text exists
        # and the only place a reading becomes "the one text", which is exactly
        # where a guard should be loudest rather than trusting the stage before.
        reading_class = classify(PERLECTOR, reading["outcome"])
        if reading_class is not OutcomeClass.COMPLETED:
            raise FatalAccounting(
                f"act {act_id} would be established from a {reading['outcome']!r} "
                f"reading ({reading_class.value}); the established text may only come "
                "from a reading that succeeded, and a failed one is held, never written"
            )
        payload = reading["payload"]
        validate_serving_provenance(
            context,
            payload["provenance"],
            producer_stage=PERLECTOR,
            require_receipt=True,
        )
        record = {
            "act_id": act_id,
            "act_key": act["act_key"],
            "page_id": act["page_id"],
            # The one text. Written once, never rewritten, never accompanied by
            # an alternative.
            "text": payload["text"],
            "status": "established",
            "regions": payload["basis"]["regions"],
            "provenance": payload["provenance"],
            # Dissent travels by reference rather than by value: the Perlectio
            # holds it, and copying it here would create a second copy to drift.
            "dissent_ref": reading["artifact_id"],
            "recensor_ref": review["artifact_id"],
        }
        record["self_hash"] = self_hash(record)

        context.publish(
            kind="archetypus",
            subject_id=act_id,
            outcome="established",
            inputs=[
                context.input_ref(region["image_path"]) for region in payload["basis"]["regions"]
            ],
            payload=record,
        )

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
