"""The orchestrator: sequencing, resume, and recovery dispatch. It is not a stage.

Its home is decided here, once: `pipeline/orchestrator/`, a peer of the numbered
stage directories rather than one of them. It is stage-neutral, imports only
`common/`, and invokes stages **as programs** — real subprocesses, real argv, real
exit codes. That last part is meta-invariant #90's requirement made concrete: one
harness runs the real orchestration end to end offline, so a green Python suite can
never stand in for a pipeline that was never actually executed.

It establishes nothing and reads nothing. Its three jobs:

  Sequence.   Door, Exemplar, Designator, Attestatores, Perlector, Recensor,
              Archetypus, Armarium, in that order.
  Recover.    The Recensor appends a request; the orchestrator invokes the owning
              stage — the Designator — for a replacement region, then re-reads and
              re-reviews. The Recensor never cuts a crop, so recovery does not grow
              a second author for regions.
  Resume.     Nothing here tracks progress in a file of its own. Every stage
              republishes what it already published, and the run tree reuses
              identical bytes and refuses different ones. Resume is therefore a
              property of the artifacts rather than of a checkpoint that could
              disagree with them.

    python pipeline/orchestrator/run.py --fixture synthetic-two-page-v0 \\
      --scenario <happy|review> --run-id <id> --run-root <dir>
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.outcomes import check_algebra_is_total  # noqa: E402
from common.contracts.stages import DESIGNATOR, RECENSOR  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    latest_attempt,
    load_fixture,
    scenario_for,
)

ROOT = Path(__file__).resolve().parents[2]

# The pipeline in flow order. The door is a program of the Exemplar's directory
# because it owns no directory of its own.
SEQUENCE = (
    ("door", "pipeline/1_exemplar/door.py"),
    ("exemplar", "pipeline/1_exemplar/run.py"),
    ("designator", "pipeline/2_designator/run.py"),
    ("attestatores", "pipeline/3_attestatores/run.py"),
    ("perlector", "pipeline/4_perlector/run.py"),
    ("recensor", "pipeline/5_recensor/run.py"),
    ("archetypus", "pipeline/6_archetypus/run.py"),
    ("armarium", "pipeline/7_armarium/run.py"),
)

STAGE_PROGRAMS = dict(SEQUENCE)

# A ceiling on dispatch rounds, independent of the per-act budget the Recensor
# enforces. Two bounds rather than one because they fail differently: the budget
# stops an act being reconsidered forever, and this stops the orchestrator looping
# even if a stage misreports its state.
MAX_RECOVERY_ROUNDS = 3


def invoke(program: str, args: argparse.Namespace, **extra) -> int:
    """Run one stage as a program and return its exit code."""
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(args.run_root),
        "--run-id",
        args.run_id,
        "--scenario",
        args.scenario,
        "--fixture-root",
        str(args.fixture_root),
        "--models-config",
        str(args.models_config),
        "--pdf-render-config",
        str(args.pdf_render_config),
    ]
    if args.pdf_target_dpi is not None:
        command += ["--pdf-target-dpi", str(args.pdf_target_dpi)]
    for key, value in extra.items():
        command += [f"--{key.replace('_', '-')}", str(value)]

    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if completed.stdout.strip():
        print(completed.stdout.rstrip())
    if completed.returncode not in (EXIT_COMPLETE, EXIT_HELD):
        raise ContractError(f"{program} exited {completed.returncode}\n{completed.stderr.rstrip()}")
    return completed.returncode


def pending_recoveries(tree: RunTree) -> list[str]:
    """Acts whose latest Recensor word is a request for a replacement region."""
    by_subject: dict[str, list[dict]] = {}
    for entry in tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "review":
            continue
        record = tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        by_subject.setdefault(record["subject_id"], []).append(record)
    return sorted(
        subject
        for subject, records in by_subject.items()
        if latest_attempt(records, "Recensor review")["outcome"] == "recovery-requested"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", required=True)
    # The fixture declares which scenarios exist; `scenario_for` refuses an
    # undeclared name once the fixture is loaded, so there is no second list here
    # to drift from the declaration.
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--fixture-root", default="proof")
    parser.add_argument(
        "--models-config",
        default="config/models.toml",
        help="the sealed model-chair roster and recipes for this run",
    )
    parser.add_argument(
        "--pdf-render-config",
        default="config/pdf_render.toml",
        help="the default whole-page PDF rasterisation target for this run",
    )
    parser.add_argument(
        "--pdf-target-dpi",
        type=int,
        default=None,
        help="override the configured PDF target for this run only",
    )
    args = parser.parse_args()

    # Prove the algebra total before anything runs. A stage added later without a
    # class or a terminal decision should fail at the first run, not at the first
    # unusual page.
    check_algebra_is_total()

    fixture = load_fixture(args.fixture_root)
    if fixture["fixture_id"] != args.fixture:
        raise ContractError(
            f"asked for fixture {args.fixture!r} but {args.fixture_root} declares "
            f"{fixture['fixture_id']!r}"
        )
    scenario_for(fixture, args.scenario)

    for name, program in SEQUENCE:
        if name == "archetypus":
            drive_recovery(args)
        invoke(program, args)

    tree = RunTree(Path(args.run_root), args.run_id)
    export = tree.read_artifact(
        "armarium",
        "export",
        artifact_id("armarium", "export", "export", None),
    )
    aggregate = export["payload"]["aggregate"]
    print(f"run {args.run_id}: {aggregate['status']}")
    for reason in aggregate["reasons"]:
        print(f"  - {reason}")

    return EXIT_COMPLETE if aggregate["status"] == "complete" else EXIT_HELD


def drive_recovery(args) -> None:
    """Dispatch every outstanding recovery request, then re-read and re-review.

    The Recensor decides an act needs a wider crop; the Designator is the only
    stage that cuts one. Keeping that ownership is why recovery lives here and not
    inside the Recensor, where it would be one short step from a stage recropping
    its own evidence until it liked it.
    """
    tree = RunTree(Path(args.run_root), args.run_id)

    for round_number in range(MAX_RECOVERY_ROUNDS + 1):
        outstanding = pending_recoveries(tree)
        if not outstanding:
            return
        if round_number == MAX_RECOVERY_ROUNDS:
            raise ContractError(
                f"recovery is still outstanding for {outstanding} after "
                f"{MAX_RECOVERY_ROUNDS} rounds. The loop is bounded and stops"
            )
        for act_id in outstanding:
            invoke(STAGE_PROGRAMS[DESIGNATOR], args, operation="recover", act=act_id)
            invoke(STAGE_PROGRAMS["perlector"], args, act=act_id)
        invoke(STAGE_PROGRAMS[RECENSOR], args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
