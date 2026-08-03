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
from common.seats.registry import ChairRegistry  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    StageContext,
    adapter_recipe_for,
    load_fixture,
    run_config_bindings,
    run_stage,
    scenario_for,
    stage_parser,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def declared_digests(fixture: dict, scenario: str) -> dict[int, str]:
    """The digest each page is declared to have, per ordinal, for this scenario.

    A `page_refusal` row substitutes a declared digest the checked-in bytes
    cannot match, so the refusal scenarios exercise the door's real inspection
    path — the same comparison, the same refusal artifact — rather than any
    scenario-aware branch that a real door would not have.
    """
    declared = {page["ordinal"]: page["sha256"] for page in fixture["page"]}
    for row in fixture.get("page_refusal", []):
        if row["scenario"] != scenario:
            continue
        if row["ordinal"] not in declared:
            raise ContractError(
                f"page_refusal names ordinal {row['ordinal']}, which no declared page has"
            )
        declared[row["ordinal"]] = row["declared_sha256"]
    return declared


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Create the run with an explicitly supplied seat implementation.

    The command-line default is the production registry. Tests supply an
    independent deterministic implementation through this seam; no command-line
    option chooses among implementations, seats, revisions, recipes, or caches.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    fixture = load_fixture(args.fixture_root)
    scenario_for(fixture, args.scenario)
    fixture_root = Path(args.fixture_root)
    declared = declared_digests(fixture, args.scenario)
    registry = registry_factory(args.models_config)
    bindings = run_config_bindings(registry.config, fixture, args.scenario)

    # The door creates the run: it is the first thing that knows what arrived, so
    # it is the only stage that can bind a run id to its inputs. The manifest
    # carries the *declared* digests — what this run believed about its sources —
    # so a refusal and the declaration it was refused against tell one story.
    tree = RunTree.create(
        Path(args.run_root),
        args.run_id,
        source_manifest=[
            {
                "relative_path": page["path"],
                "sha256": declared[page["ordinal"]],
                "ordinal": page["ordinal"],
            }
            for page in fixture["page"]
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_seats=bindings["witness_seats"],
    )
    run = tree.read_run()
    context = StageContext(
        tree=tree,
        run=run,
        fixture=fixture,
        scenario=args.scenario,
        stage=DOOR,
        adapter_revision=adapter_recipe_for(run, DOOR),
        args=args,
        registry=registry,
    )

    admitted = 0
    for page in fixture["page"]:
        source = fixture_root / page["path"]
        outcome, reason, digest, data = inspect(source, declared[page["ordinal"]])
        if outcome == "admitted":
            # Store the bytes that were actually verified. Reading the file a
            # second time here would open a window in which the file changed
            # between the check and the store, leaving an admission whose recorded
            # digest describes bytes the run does not hold. What is sealed must be
            # what was inspected.
            _, published = tree.put_blob(DOOR, data)
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
            # alone and named, and the readable pages proceed (harvest #2). The
            # ordinal travels with the refusal so the page stays reconcilable as
            # a unit all the way to the Armarium's census.
            context.publish(
                kind="admission",
                subject_id=f"source-{page['ordinal']}",
                outcome="refused",
                payload={
                    "declared_path": page["path"],
                    "ordinal": page["ordinal"],
                    "reason": reason,
                },
            )

    if admitted == 0:
        raise ContractError(
            "the door admitted nothing. An empty or wholly unreadable input set is "
            "a loud failure, never a green run with no output (harvest #3)"
        )

    context.finish(DOOR)
    return EXIT_COMPLETE


def inspect(source: Path, declared_sha256: str) -> tuple[str, str, str, bytes]:
    """Decide one file, and hand back the exact bytes the decision was made on.

    Returning the bytes rather than the caller re-reading them is what makes the
    decision and the stored evidence describe the same thing.
    """
    if not source.exists():
        return "refused", f"{source} does not exist", "", b""
    data = source.read_bytes()
    if not data:
        return "refused", f"{source} is empty", "", b""
    if not data.startswith(PNG_SIGNATURE):
        # By signature, never by extension: a text file renamed .png is exactly
        # the case the old upload endpoint let through to die deep in a run.
        return "refused", f"{source} does not carry a PNG signature", "", b""
    digest = digest_bytes(data)
    if digest != declared_sha256:
        return "refused", f"{source} has digest {digest}, not the declared one", digest, b""
    return "admitted", "", digest, data


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
