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
              hard-failure cap (`common/hard_failure.py`, which owns the tally and
              the reasoning behind it) is recomputed. Two hard failures is an early
              warning and the run keeps going; more than two halts it at the stage
              boundary just finished — never mid-stage — with whatever completed
              intact.
  Resume.     Nothing here tracks progress in a file of its own. Every stage
              republishes what it already published, and the run tree reuses
              identical bytes and refuses different ones. Resume is therefore a
              property of the artifacts rather than of a checkpoint that could
              disagree with them.

    python pipeline/orchestrator/run.py --fixture synthetic-two-page-v0 \\
      --scenario <happy|review> --run-id <id> --run-root <dir>
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.alignment import DEFAULT_ALIGNMENT_CONFIG_PATH  # noqa: E402
from common.armarium_formats import DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.outcomes import ArmariumCategory, check_algebra_is_total  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR  # noqa: E402
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
    DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH,
    DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH,
    DEFAULT_PDF_RENDER_CONFIG_PATH,
    DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    EXIT_COMPLETE,
    EXIT_HELD,
    EXIT_RUN_HALTED,
    WITNESS_CONTEXT_REGIMES,
    current_recovery_request,
    latest_attempt,
    load_fixture,
    require_sealed_config,
    run_sealed_config_digests,
    scenario_for,
)

ROOT = Path(__file__).resolve().parents[2]

# The pipeline in flow order. The door is a program of the Exemplar's directory
# because it owns no directory of its own.
SEQUENCE = (
    ("door", "pipeline/1_exemplar/door.py"),
    ("exemplar", "pipeline/1_exemplar/run.py"),
    ("designator", "pipeline/2_designator/run.py"),
    (ATTESTATORES, "pipeline/3_attestatores/run.py"),
    ("perlector", "pipeline/4_perlector/run.py"),
    ("recensor", "pipeline/5_recensor/run.py"),
    ("archetypus", "pipeline/6_archetypus/run.py"),
    ("armarium", "pipeline/7_armarium/run.py"),
)

STAGE_PROGRAMS = dict(SEQUENCE)

# Importing the gate here would cross the orchestrator's common-only boundary;
# a test reconciles this duplicate path with the gate's constant.
DEFAULT_DATA_GATE_POLICY_PATH = ROOT / "config" / "data_handling_policy.json"
_TRANSFER_CREDENTIAL_ENV = frozenset({"RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"})


def require_coherent_ingress_options(args: argparse.Namespace) -> None:
    if args.submission_folder is not None:
        return
    if args.submission_manifest is not None:
        raise ContractError(
            "a submission filename ledger is meaningful only with a real submission folder; "
            "the walking skeleton's declared synthetic pages are not gated input "
            "(--submission-manifest was supplied without --submission-folder)"
        )
    if args.data_gate_policy is not None:
        raise ContractError(
            "--data-gate-policy is meaningful only with --submission-folder; the synthetic "
            "fixture route does not evaluate the real-input storage policy"
        )


def resolve_caller_paths(args: argparse.Namespace) -> argparse.Namespace:
    """Bind paths to the caller's cwd without hiding symlinks from the Door."""
    args.run_root = Path(args.run_root).absolute()
    for attribute in ("submission_folder", "submission_manifest"):
        value = getattr(args, attribute)
        if value is not None:
            setattr(args, attribute, Path(value).absolute())
    # A real run's absent policy means the repository default; fixture runs must
    # not forward a real-only control that the Door would ignore.
    if args.data_gate_policy is None and args.submission_folder is not None:
        args.data_gate_policy = DEFAULT_DATA_GATE_POLICY_PATH
    elif args.data_gate_policy is not None:
        args.data_gate_policy = Path(args.data_gate_policy).absolute()
    return args


def stage_environment() -> dict[str, str]:
    """Keep stage runtime settings, but drop credentials for the upload-only verb."""
    environment = dict(os.environ)
    for name in _TRANSFER_CREDENTIAL_ENV:
        environment.pop(name, None)
    return environment


def invoke(program: str, args: argparse.Namespace, **extra) -> int:
    """Run one stage as a program and return its exit code."""
    require_coherent_ingress_options(args)
    # Direct invocation entry points must not reinterpret caller paths under the
    # child's repository-root cwd.
    for attribute, flag in (
        ("run_root", "--run-root"),
        ("submission_folder", "--submission-folder"),
        ("submission_manifest", "--submission-manifest"),
        ("data_gate_policy", "--data-gate-policy"),
    ):
        value = getattr(args, attribute, None)
        if value is not None and not Path(value).is_absolute():
            raise ContractError(
                f"{flag} is still the caller-relative path {str(value)!r}. Stages run from "
                f"{ROOT} while the caller may be anywhere, so this must be resolved at the "
                "orchestration boundary (`resolve_caller_paths`) before any child sees it"
            )
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
        "--designator-padding-config",
        str(args.designator_padding_config),
        "--designator-geometry-config",
        str(args.designator_geometry_config),
        "--alignment-config",
        str(args.alignment_config),
        "--formats-config",
        str(args.formats_config),
        "--recovery-config",
        str(args.recovery_config),
        "--hard-failure-config",
        str(args.hard_failure_config),
    ]
    # Later stages may read only the run tree the Door sealed, never source paths.
    if program == STAGE_PROGRAMS["door"]:
        if args.submission_folder is not None:
            command += ["--submission-folder", str(args.submission_folder)]
        if args.submission_manifest is not None:
            command += ["--submission-manifest", str(args.submission_manifest)]
        if args.data_gate_policy is not None:
            command += ["--data-gate-policy", str(args.data_gate_policy)]
    if args.pdf_target_dpi is not None:
        command += ["--pdf-target-dpi", str(args.pdf_target_dpi)]
    command += [
        "--witness-context",
        args.witness_context,
        "--witness-context-config",
        str(args.witness_context_config),
        "--nuda-per-mille",
        str(args.nuda_per_mille),
        "--nuda-approval-ref",
        str(args.nuda_approval_ref),
        "--perlector-instrument-per-mille",
        str(args.perlector_instrument_per_mille),
        "--perlector-instrument-approval-ref",
        str(args.perlector_instrument_approval_ref),
        "--perlector-protocol-config",
        str(args.perlector_protocol_config),
        "--perlector-audit-config",
        str(args.perlector_audit_config),
    ]
    command.append("--draft-fed" if args.draft_fed else "--no-draft-fed")
    for key, value in extra.items():
        command += [f"--{key.replace('_', '-')}", str(value)]

    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=stage_environment(),
    )
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
        # Indexed without a check: `current_recovery_request` refuses a request
        # whose `recovery_kind` is outside `RECOVERY_KINDS` before returning one.
        request = current_recovery_request(tree, subject, recovery_policy)
        recovery_kind = request["payload"]["recovery_kind"]
        outstanding.append((subject, request["artifact_id"], recovery_kind))
    return sorted(outstanding)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--submission-folder")
    parser.add_argument("--submission-manifest")
    # A relative default would bind beside the caller, not inside the repository.
    # `resolve_caller_paths` fills the repository default only for real ingress.
    parser.add_argument("--data-gate-policy", default=None)
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
        "--perlector-instrument-per-mille",
        type=int,
        default=0,
        help="per-mille rate at which the protocol's selection rule samples acts into "
        "the primed-without-prior control arm (Lectio nuda has its own "
        "--nuda-per-mille); raising it above 0 is Tyrel's, with "
        "--perlector-instrument-approval-ref (config/README.md, R5a toggle register)",
    )
    parser.add_argument(
        "--perlector-instrument-approval-ref",
        default="",
        help="Tyrel's recorded approval reference for a nonzero instrument rate",
    )
    parser.add_argument(
        "--perlector-protocol-config",
        default=str(DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH),
        help="the sealed Perlector prior-draft protocol; its exact bytes enter every "
        "run's config digest",
    )
    parser.add_argument(
        "--draft-fed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="feed the Pass-A draft to Pass B (fed) or withhold it (--no-draft-fed); "
        "changing the default is Tyrel's through B5a (config/README.md, R5a toggle "
        "register)",
    )
    parser.add_argument(
        "--perlector-audit-config", default=str(DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH)
    )
    parser.add_argument(
        "--pdf-render-config",
        default=str(DEFAULT_PDF_RENDER_CONFIG_PATH),
        help="the default whole-page PDF rasterisation target for this run",
    )
    parser.add_argument(
        "--designator-padding-config",
        default=str(DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH),
        help="the capture padding applied to every act crop, sealed into this run",
    )
    parser.add_argument(
        "--designator-geometry-config",
        default=str(DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH),
        help="the sealed Surya/YOLO geometry and crop-policy declaration for this run",
    )
    parser.add_argument(
        "--alignment-config",
        default=str(DEFAULT_ALIGNMENT_CONFIG_PATH),
        help="the sealed limits for page-witness alignment",
    )
    parser.add_argument(
        "--formats-config",
        default=str(DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH),
        help="the sealed Armarium product projections for this run",
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
    parser.add_argument(
        "--witness-context",
        default="named",
        choices=WITNESS_CONTEXT_REGIMES,
        help="the run-level named/blinded toggle the Perlector's dossier is built under",
    )
    parser.add_argument(
        "--witness-context-config",
        default=str(DEFAULT_WITNESS_CONTEXT_CONFIG_PATH),
        help="the Perlector-owned factual witness-context declaration this run seals",
    )
    parser.add_argument(
        "--nuda-per-mille",
        type=int,
        default=0,
        help="the sealed Lectio nuda sampling rate, in thousandths (0 disables it)",
    )
    parser.add_argument(
        "--nuda-approval-ref",
        default="",
        help="Tyrel's reference for the predeclared Lectio nuda sampling design",
    )
    args = parser.parse_args()

    require_coherent_ingress_options(args)
    resolve_caller_paths(args)

    # Prove the algebra total before anything runs. A stage added later without a
    # class or a terminal decision should fail at the first run, not at the first
    # unusual page.
    check_algebra_is_total()

    # Real run authority seals neither fixture identity nor fixture scenario.
    if args.submission_folder is None:
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
    # Read once, here, and held for the whole run: every `checkpoint` below takes
    # this exact policy object rather than reopening the file, so the cap that
    # halts the run cannot move under it mid-orchestration (S3's shape, applied to
    # the sealing family's fourth member).
    #
    # The proof is a step behind the read for a reason peculiar to this policy.
    # The threshold has to be known before the resume preflight can decide whether
    # a resumed run may re-enter a stage at all, and on a FIRST run there is no
    # run authority to prove it against until the Door creates one -- so the point
    # of use is the first moment such an authority exists. On a resume that is
    # right here, before anything is invoked; on a first run the Door seals these
    # digests from the same bytes and every stage after it rechecks its own.
    hard_failure_policy = load_hard_failure_policy(args.hard_failure_config)
    if tree.resolve("run.json").exists():
        require_sealed_config(
            run_sealed_config_digests(tree.read_run()),
            "hard-failure",
            hard_failure_policy["config_sha256"],
        )
        halted = checkpoint(args, "resume-preflight", hard_failure_policy)
    for name, program in SEQUENCE:
        if halted is not None:
            break
        if name == "archetypus":
            halted = drive_recovery(args, hard_failure_policy)
            if halted is not None:
                break
        result = invoke(program, args)
        if name == "door" and result in (EXIT_COMPLETE, EXIT_HELD):
            # On a FIRST run there was no authority to prove the policy against
            # before this moment; the Door has just created one from its own
            # read of the same path. Proving the two reads against each other
            # here closes the first-run window in which the file could change
            # between the orchestrator's read above and the Door's — the exact
            # straddle the sealing family exists to catch. On a resume this is
            # a harmless re-proof of the check already made at preflight. A
            # completed-but-partial Door (EXIT_HELD) has created the authority
            # just as surely as a complete one, so both named exits prove it;
            # `invoke` returns no other code.
            require_sealed_config(
                run_sealed_config_digests(tree.read_run()),
                "hard-failure",
                hard_failure_policy["config_sha256"],
            )
        # A held Attestatores exit is not an ordinary partial act result. Its own
        # forwarded stderr names whether the attempt tally was UNKNOWN or a whole
        # pass was refused during preflight; either cause stops orchestration.
        if name == ATTESTATORES and result == EXIT_HELD:
            print(f"run {args.run_id}: held; its reason is on stderr above")
            return EXIT_HELD
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
    status, lines = terminal_report(export)
    print(f"run {args.run_id}: {status}")
    for line in lines:
        print(f"  - {line}")

    return EXIT_COMPLETE if status == "complete" else EXIT_HELD


def terminal_report(export: dict) -> tuple[str, list[str]]:
    """The run's verdict, taken from the Armarium's own terminal outcome.

    Deriving it again from `payload["aggregate"]` was a second, weaker derivation of
    a question the last stage had already answered: the Armarium reports its terminal
    ledger's status, which subsumes the aggregate's and is partial in one case the
    aggregate is not (7_armarium/HANDOFF.md). A run whose bundle said `partial` on its
    own face would have printed `complete` and exited 0 here.

    The reasons stay the aggregate's, because they are the ones an operator acts on
    and every reachable run's two statuses agree. When they do not, the ledger's own
    unresolved units are on the bundle's face and this says where to read them rather
    than reporting a partial run with nothing named.
    """
    aggregate = export["payload"]["aggregate"]
    complete = export["outcome"] == ArmariumCategory.DELIVERED.value
    reasons = list(aggregate["reasons"])
    if not complete and not reasons:
        reasons.append(
            "the export bundle's terminal ledger is partial while the run aggregate "
            "reconciled; its unresolved units are named in EXPORT_MANIFEST.json's "
            "claims.partial_reasons"
        )
    return ("complete" if complete else "partial"), reasons


def checkpoint(args, checkpoint_name: str, hard_failure_policy: dict) -> dict | None:
    """Recompute the run-level hard-failure tally from disk; the tally if breached.

    The boundary's own name travels back inside the tally, so the halt below can
    say which section finished rather than only that one did. Two hard failures
    is Tyrel's named "early warning" and stops nothing; more than two halts the
    run at this exact boundary.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    tally = tally_hard_failures(tree, hard_failure_policy)
    if tally["instrument_count"]:
        print(
            f"run {args.run_id}: {tally['instrument_count']} Perlector instrument failure(s) "
            "retained separately; they do not consume Tyrel's production hard-failure cap"
        )
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
    # The orchestrator is not a stage and holds no `StageContext`, so it proves the
    # policy it dispatches under against the digests the run authority recorded for
    # itself. Without this, the dispatcher bounded the whole recovery loop — the
    # round ceiling and every request it checked — on whatever `config/recovery.toml`
    # said at this moment, which need not be what the run sealed (audit S3 names
    # this the third point of use). Checked before the first round, so a swapped
    # policy stops the loop rather than being discovered by the stage it dispatched.
    require_sealed_config(
        run_sealed_config_digests(tree.read_run()), "recovery", recovery_policy["config_sha256"]
    )
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
