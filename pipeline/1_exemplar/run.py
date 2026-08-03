"""Exemplar: the sealed source. Nothing downstream may alter it.

Reads what the door admitted and seals each admitted source as a page: the bytes
into the run tree's blob store, and a `page` artifact binding the page identity to
the source digest and the ordinal. From here on, every region in the run traces
back to one of these — ARCHITECTURE's second invariant — because a region's
identity is derived from an act's, and an act's from a page's.

The Exemplar reads the door's artifacts rather than the fixture's file list. That
is the handoff being real: if the door refused a page, this stage sees a refusal
and seals nothing for it, instead of quietly going back to the source and sealing
it anyway.

    python pipeline/1_exemplar/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.stages import DOOR, EXEMPLAR  # noqa: E402
from common.seats.registry import ChairRegistry  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    open_context,
    page_identity,
    run_stage,
    stage_parser,
)


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied seat/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, EXEMPLAR, registry_factory=registry_factory)
    tree = context.tree

    admissions = [
        entry for entry in tree.build_manifest(DOOR)["artifacts"] if entry["kind"] == "admission"
    ]
    if not admissions:
        raise ContractError(
            "no admissions to seal: the Exemplar was run before the door, or the "
            "door's artifacts are missing. Sealing nothing quietly would leave a "
            "run that looks finished and read no page at all"
        )

    sealed = 0
    for entry in sorted(admissions, key=lambda item: item["subject_id"]):
        admission = tree.read_artifact(DOOR, "admission", entry["artifact_id"])
        ordinal = admission["payload"].get("ordinal")

        if admission["outcome"] == "refused":
            # The refusal is carried forward as this stage's own outcome so the
            # page is accounted for here too. A unit that simply stopped being
            # mentioned would be invariant #10's imbalance — which is why the
            # ordinal is required, not defaulted: a refusal with no ordinal is a
            # page the Armarium's census could never reconcile.
            if not isinstance(ordinal, int):
                raise ContractError(
                    f"refused admission {admission['subject_id']} carries no ordinal; "
                    "an unaccountable refusal is a silent loss wearing a record"
                )
            context.publish(
                kind="page",
                subject_id=admission["subject_id"],
                outcome="refused",
                inputs=[context.input_ref(entry["relative_path"])],
                payload={"ordinal": ordinal, "reason": admission["payload"]["reason"]},
            )
            continue

        stored_at = admission["payload"]["stored_at"]
        page_id = page_identity(context.fixture, ordinal)
        context.publish(
            kind="page",
            subject_id=page_id,
            outcome="sealed",
            inputs=[
                context.input_ref(entry["relative_path"]),
                context.input_ref(stored_at),
            ],
            payload={
                "ordinal": ordinal,
                "source_sha256": admission["payload"]["sha256"],
                "image_path": stored_at,
            },
        )
        sealed += 1

    if sealed == 0:
        raise ContractError("every admitted source failed to seal")

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
