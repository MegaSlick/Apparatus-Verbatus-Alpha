"""The door: what may enter at all, decided before a run tree means anything.

The door owns no directory. It writes its admissions and refusals into the
Exemplar's, so the record of what arrived and the record of what was sealed sit
together — a refusal filed somewhere nothing downstream reads is a refusal that
has been lost, which GOVERNANCE 2 does not allow.

Two invariants from the harvest shape this even at skeleton scale. **#1: only
images enter, verified by decoding, not by extension** — here that is the PNG
signature check, because the skeleton has no decoder and must not pretend to. The
real door (spec 03) decodes. **#3: a refused file is never silently omitted** —
every refusal is an artifact with a reason, and an input set that admitted nothing
is a loud failure rather than a green run with no output.

Invoked as a program:

    python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.stages import DOOR  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    StageContext,
    fixture_config_digest,
    load_fixture,
    run_stage,
    stage_parser,
)

ADAPTER_REVISION = "fake-door-v0"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def main() -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    fixture = load_fixture(args.fixture_root)
    fixture_root = Path(args.fixture_root)

    # The door creates the run: it is the first thing that knows what arrived, so
    # it is the only stage that can bind a run id to its inputs.
    tree = RunTree.create(
        Path(args.run_root),
        args.run_id,
        source_manifest=[
            {"relative_path": page["path"], "sha256": page["sha256"], "ordinal": page["ordinal"]}
            for page in fixture["page"]
        ],
        config_digest=fixture_config_digest(fixture, args.scenario),
        adapter_recipes=dict(fixture["adapter_recipes"]),
        witness_seats=list(fixture["witness_seats"]),
    )
    context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture=fixture,
        scenario=args.scenario,
        stage=DOOR,
        adapter_revision=ADAPTER_REVISION,
        args=args,
    )

    admitted = 0
    for page in fixture["page"]:
        source = fixture_root / page["path"]
        outcome, reason, digest = inspect(source, page["sha256"])
        if outcome == "admitted":
            _, published = tree.put_blob(DOOR, source.read_bytes())
            context.publish(
                kind="admission",
                subject_id=f"source-{page['ordinal']}",
                outcome="admitted",
                # The admission names the bytes it admitted. Without this the
                # first handoff would be the one boundary carrying no verifiable
                # reference, and the boundary test would have to skip it — a
                # skip-list being precisely how a gap goes unnoticed (#87).
                inputs=[context.input_ref(published.relative_path)],
                payload={
                    "declared_path": page["path"],
                    "ordinal": page["ordinal"],
                    "sha256": digest,
                    "stored_at": published.relative_path,
                },
            )
            admitted += 1
        else:
            # Per-file refusal, never per-folder: one unreadable file is refused
            # alone and named, and the readable pages proceed (harvest #2).
            context.publish(
                kind="admission",
                subject_id=f"source-{page['ordinal']}",
                outcome="refused",
                payload={"declared_path": page["path"], "reason": reason},
            )

    if admitted == 0:
        raise ContractError(
            "the door admitted nothing. An empty or wholly unreadable input set is "
            "a loud failure, never a green run with no output (harvest #3)"
        )

    context.finish(DOOR)
    return EXIT_COMPLETE


def inspect(source: Path, declared_sha256: str) -> tuple[str, str, str]:
    """Decide one file, and say why when the answer is no."""
    if not source.exists():
        return "refused", f"{source} does not exist", ""
    data = source.read_bytes()
    if not data:
        return "refused", f"{source} is empty", ""
    if not data.startswith(PNG_SIGNATURE):
        # By signature, never by extension: a text file renamed .png is exactly
        # the case the old upload endpoint let through to die deep in a run.
        return "refused", f"{source} does not carry a PNG signature", ""
    digest = digest_bytes(data)
    if digest != declared_sha256:
        return "refused", f"{source} has digest {digest}, not the declared one", digest
    return "admitted", "", digest


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
