"""The orchestrator: sequencing, resume, and recovery dispatch. It is not a stage.

Its home is decided here, once: `pipeline/orchestrator/`, a peer of the numbered
stage directories rather than one of them. It is stage-neutral, imports only
`common/`, and invokes stages **as programs** — real subprocesses, real argv, real
exit codes. That last part is meta-invariant #90's requirement made concrete: one
harness runs the real orchestration end to end offline, so a green Python suite can
never stand in for a pipeline that was never actually executed.

It establishes nothing and reads nothing except the outcome bookkeeping it needs to
sequence and to checkpoint. Its four jobs:

  Sequence.   Door, Exemplar, Designator, Attestatores, Perlector, Recensor,
              Archetypus, Armarium, in that order.
  Recover.    The Recensor appends a request; the orchestrator invokes the owning
              stage — the Designator — for a replacement region, then re-reads and
              re-reviews. The Recensor never cuts a crop, so recovery does not grow
              a second author for regions.
  Checkpoint. After every stage invocation and every recovery round, the run-level
              hard-failure cap (`common/hard_failure.py`, Tyrel's ruling of
              2026-08-05) is recomputed from the artifacts actually on disk. Two
              hard failures is an early warning and the run keeps going; more than
              two halts it at the stage boundary just finished — never mid-stage —
              with whatever completed intact. This is distinct from the Recensor's
              own per-act recovery budget (`common/recovery.py`): that bounds how
              often ONE ACT may ask for rework, this bounds how many accounted hard
              failures ONE RUN may carry before it needs Tyrel rather than another
              automatic stage invocation.
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
from common.hard_failure import (  # noqa: E402
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.recovery import (  # noqa: E402
    DEFAULT_RECOVERY_CONFIG_PATH,
    FALLBACK_RECROP,
    load_recovery_policy,
)
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    current_recovery_request,
    latest_attempt,
    load_fixture,
    scenario_for,
)

ROOT = Path(__file__).resolve().parents[2]

# The orchestrator's own exit code, never a stage's. `common/stage.py`'s
# EXIT_COMPLETE/EXIT_FATAL/EXIT_HELD are the contract a stage PROGRAM returns to
# the orchestrator that invokes it; no stage ever halts a run for the run-level
# hard-failure cap; only the orchestrator can, since only it decides whether to
# invoke another stage at all. A distinct code keeps "the run-level cap tripped"
# distinguishable from "a stage crashed" (2) and from the ordinary "some acts are
# held for review" (3) that a working pipeline produces every day.
EXIT_RUN_HALTED = 4

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
        "--recovery-config",
        str(args.recovery_config),
        "--hard-failure-config",
        str(args.hard_failure_config),
    ]
    if args.pdf_target_dpi is not None:
        command += ["--pdf-target-dpi", str(args.pdf_target_dpi)]
    for key, value in extra.items():
        command += [f"--{key.replace('_', '-')}", str(value)]

    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if completed.stdout.strip():
        print(completed.stdout.rstrip())
    # A completed-but-partial Door publishes its private refusal report on stderr.
    # Forward it on the normal orchestration path so the human who ran the pipeline
    # is told that the retained record exists.  Unexpected exits remain reported by
    # the ContractError below, without printing their diagnostics twice.
    if completed.returncode in (EXIT_COMPLETE, EXIT_HELD) and completed.stderr.strip():
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode not in (EXIT_COMPLETE, EXIT_HELD):
        raise ContractError(f"{program} exited {completed.returncode}\n{completed.stderr.rstrip()}")
    return completed.returncode


def pending_recoveries(tree: RunTree, recovery_policy: dict) -> list[tuple[str, str, str]]:
    """Checked `(act_id, request_id, recovery_kind)` triples the latest review asks for.

    The kind travels alongside the request id because dispatch depends on it:
    a Designator recrop and a Perlector page-level/continuation-aware reread
    are two distinct operations (ARCHITECTURE, spec 09), and which one a
    request means is not this function's business to decide, only to report.
    """
    by_subject: dict[str, list[dict]] = {}
    for entry in tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "review":
            continue
        record = tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        by_subject.setdefault(record["subject_id"], []).append(record)
    outstanding: list[tuple[str, str, str]] = []
    for subject, records in by_subject.items():
        review = latest_attempt(records, f"Recensor review of {subject}", operation="recense")
        if review["outcome"] != "recovery-requested":
            continue
        request = current_recovery_request(tree, subject, recovery_policy)
        payload = request.get("payload")
        recovery_kind = payload.get("recovery_kind") if isinstance(payload, dict) else None
        if not isinstance(recovery_kind, str) or not recovery_kind:
            raise ContractError(
                f"recovery request {request['artifact_id']} names no recovery kind; the owning "
                "stage cannot be derived from a request that does not say what it asks for"
            )
        outstanding.append((subject, request["artifact_id"], recovery_kind))
    return sorted(outstanding)


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
    parser.add_argument(
        "--recovery-config",
        default=str(DEFAULT_RECOVERY_CONFIG_PATH),
        help="the bounded recovery policy sealed into this run",
    )
    parser.add_argument(
        "--hard-failure-config",
        default=str(DEFAULT_HARD_FAILURE_CONFIG_PATH),
        help="the run-level hard-failure cap this orchestrator checkpoints against",
    )
    args = parser.parse_args()

    # Prove the algebra total before anything runs. A stage added later without a
    # class or a terminal decision should fail at the first run, not at the first
    # unusual page.
    check_algebra_is_total()
    hard_failure_policy = load_hard_failure_policy(args.hard_failure_config)

    fixture = load_fixture(args.fixture_root)
    if fixture["fixture_id"] != args.fixture:
        raise ContractError(
            f"asked for fixture {args.fixture!r} but {args.fixture_root} declares "
            f"{fixture['fixture_id']!r}"
        )
    scenario_for(fixture, args.scenario)

    halted = None
    # A resumed run may already be over the cap. Recompute before re-entering
    # any stage, not only after the first idempotent replay: "stop" bounds work,
    # and replaying a model stage before rediscovering durable failure evidence
    # would spend work after the run was already known to need Tyrel.
    tree = RunTree(Path(args.run_root), args.run_id)
    if tree.resolve("run.json").exists():
        halted = checkpoint(args, "resume-preflight", hard_failure_policy)
    for name, program in SEQUENCE:
        if halted is not None:
            break
        if name == "archetypus":
            halted = drive_recovery(args, hard_failure_policy)
            if halted is not None:
                break
        invoke(program, args)
        halted = checkpoint(args, name, hard_failure_policy)
        if halted is not None:
            break

    if halted is not None:
        report_halt(args, halted)
        return EXIT_RUN_HALTED

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


def checkpoint(args, checkpoint_name: str, hard_failure_policy: dict) -> dict | None:
    """Recompute the run-level hard-failure tally from disk; the tally if breached.

    The boundary's own name travels back inside the tally, so the halt below can
    say which section finished rather than only that one did.

    Recomputed fresh every time from the artifacts a stage actually published,
    never from a counter this process keeps in memory — a count that only lived
    in this process's memory would read zero exactly when a run is resumed after
    a crash, which is backwards for a mechanism whose job is noticing that
    something is going wrong. Two hard failures is Tyrel's named "early warning"
    and does not stop anything; more than two does, at this exact boundary.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    tally = tally_hard_failures(tree, hard_failure_policy)
    if tally["count"] == tally["threshold"] and tally["count"] > 0:
        print(
            f"run {args.run_id}: {tally['count']} hard failure(s) so far — Tyrel's ruling "
            f"treats this as an early warning; one more halts the run at the next checkpoint"
        )
    return dict(tally, checkpoint=checkpoint_name) if tally["breached"] else None


def report_halt(args, tally: dict) -> None:
    """The one place this halt is said out loud. GOVERNANCE 2: not lost silently."""
    print(
        f"run {args.run_id}: halted at the {tally['checkpoint']} checkpoint — {tally['count']} "
        f"hard failure(s) exceed the run-level cap of {tally['threshold']} (Tyrel's ruling: "
        f"more than {tally['threshold']} needs fixing, not another automatic stage). The "
        "section already in flight finished; nothing further was invoked"
    )
    for kind, subjects in tally["by_kind"].items():
        if subjects:
            print(f"  - {kind}: {subjects}")


def drive_recovery(args, hard_failure_policy: dict) -> dict | None:
    """Dispatch every outstanding recovery request, then re-read and re-review.

    The Recensor decides an act needs a wider crop; the Designator is the only
    stage that cuts one. Keeping that ownership is why recovery lives here and not
    inside the Recensor, where it would be one short step from a stage recropping
    its own evidence until it liked it.

    Returns the hard-failure tally if the run-level cap trips partway through.
    A recovery round is one completed Designator section followed by one
    completed Perlector section followed by one Recensor pass, and the cap is
    checked at each of those three boundaries — never between two acts of the
    same batch. That is Tyrel's own shape for the cap ("if errors happened in
    chandra stage it finishes that section but pauses"): a section already in
    flight finishes, and a second act whose recrop was already approved is not
    left without its owning stage's answer.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    recovery_policy = load_recovery_policy(args.recovery_config)
    maximum_rounds = recovery_policy["absolute_cap"]

    for round_number in range(maximum_rounds + 1):
        outstanding = pending_recoveries(tree, recovery_policy)
        if not outstanding:
            return None
        if round_number == maximum_rounds:
            raise ContractError(
                f"recovery is still outstanding for {outstanding} after "
                f"{maximum_rounds} rounds. The run-bound policy stops the loop"
            )
        for act_id, _request_id, recovery_kind in outstanding:
            # Only the recrop operation has a real implementation today. Refuse
            # loudly rather than silently dispatching any other kind as though
            # it were one — that silent conflation is what naming the kind
            # exists to stop, not a gap to paper over with a substitute crop.
            # Checked for the whole batch before any of it is dispatched, so an
            # unanswerable request does not leave half a round behind it.
            if recovery_kind != FALLBACK_RECROP:
                raise ContractError(
                    f"act {act_id}'s outstanding recovery request names recovery_kind "
                    f"{recovery_kind!r}, which this orchestrator has no dispatch for; only "
                    f"{FALLBACK_RECROP!r} (a Designator recrop) is implemented today, and "
                    "the page-level reread belongs to the Perlector, which has not built it"
                )
        for act_id, request_id, _recovery_kind in outstanding:
            invoke(
                STAGE_PROGRAMS[DESIGNATOR],
                args,
                operation="recover",
                act=act_id,
                recovery_request=request_id,
            )
        tally = checkpoint(args, DESIGNATOR, hard_failure_policy)
        if tally is not None:
            return tally
        for act_id, _request_id, _recovery_kind in outstanding:
            invoke(STAGE_PROGRAMS["perlector"], args, act=act_id)
        tally = checkpoint(args, "perlector", hard_failure_policy)
        if tally is not None:
            return tally
        invoke(STAGE_PROGRAMS[RECENSOR], args)
        tally = checkpoint(args, RECENSOR, hard_failure_policy)
        if tally is not None:
            return tally
    return None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
