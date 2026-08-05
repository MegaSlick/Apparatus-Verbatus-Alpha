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
    reading_basis_regions,
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
    return latest_attempt(
        artifacts_for(context, RECENSOR, "review", act_id),
        f"review of {act_id}",
        operation="recense",
    )


def reviewed_reading(context, review: dict, act_id: str) -> tuple[dict, dict[str, str]]:
    """Resolve the exact Perlectio the Recensor reviewed, never a newer one.

    `latest_attempt` establishes which review is current.  That review then
    carries the evidence of the reading it assessed.  Looking up the current
    Perlectio independently would silently establish a recovery attempt nobody
    reviewed, which is a reconciliation failure rather than a useful fallback.
    """
    payload = review.get("payload")
    if not isinstance(payload, dict):
        raise FatalAccounting(f"review of {act_id} has no payload")
    reference = payload.get("perlectio_ref")
    if not isinstance(reference, dict) or reference not in review.get("inputs", []):
        raise FatalAccounting(
            f"accepted review of {act_id} does not retain its digest-checked Perlectio reference"
        )
    reading = context.tree.read_artifact_reference(
        reference,
        stage=PERLECTOR,
        kind="perlectio",
        subject_id=act_id,
    )
    current = latest_attempt(
        artifacts_for(context, PERLECTOR, "perlectio", act_id),
        f"reading of {act_id}",
        operation="perlegere",
    )
    if current["artifact_id"] != reading["artifact_id"]:
        raise FatalAccounting(
            f"act {act_id} has a newer Perlectio that the accepted Recensor review did not "
            "assess; no unreconciled reading may become established"
        )
    recovery_regions = 0
    for region in artifacts_for(context, DESIGNATOR, "region", act_id):
        payload = region.get("payload")
        if not isinstance(payload, dict):
            raise FatalAccounting(f"Designator region of {act_id} has no object payload")
        if payload.get("origin") == "recovery":
            recovery_regions += 1
    readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
    if len(readings) != recovery_regions + 1:
        raise FatalAccounting(
            f"act {act_id} has {recovery_regions} recovery crop(s) but {len(readings)} "
            "Perlectio attempt(s); a recovery crop must be reread before any text is established"
        )
    return reading, reference


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
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

        reading, reading_ref = reviewed_reading(context, review, act_id)
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
        regions = reading_basis_regions(reading, f"accepted reading of {act_id}")
        if not isinstance(payload.get("text"), str) or not payload["text"].strip():
            raise FatalAccounting(
                f"accepted reading of {act_id} establishes no readable text; silence is held "
                "until a Recensor blank proof exists"
            )
        validate_serving_provenance(
            context,
            payload.get("provenance"),
            producer_stage=PERLECTOR,
            require_receipt=True,
        )
        provenance = payload.get("provenance")
        record = {
            "act_id": act_id,
            "act_key": act["act_key"],
            "page_id": act["page_id"],
            # The one text. Written once, never rewritten, never accompanied by
            # an alternative.
            "text": payload["text"],
            "status": "established",
            "regions": regions,
            "provenance": provenance,
            # Dissent travels by a digest-checked Perlectio reference rather than
            # by value: the Perlectio holds it, and copying it here would create a
            # second copy to drift. The terminal export can retain this reference
            # after the run volume has been disposed of.
            "dissent_ref": reading_ref,
            "perlectio_ref": reading_ref,
            "recensor_ref": context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
        }
        record["self_hash"] = self_hash(record)

        context.publish(
            kind="archetypus",
            subject_id=act_id,
            outcome="established",
            inputs=[record["recensor_ref"], reading_ref]
            + [context.input_ref(region["image_path"]) for region in regions],
            payload=record,
        )

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
