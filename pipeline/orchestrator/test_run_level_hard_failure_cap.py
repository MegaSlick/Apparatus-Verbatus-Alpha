"""The run-level hard-failure cap, driven over the real orchestrator.

Two hard failures in a run is an early warning and the run keeps going; more
than two halts it at the next stage boundary, with whatever finished intact.
This is the RUN-level mechanism `common/hard_failure.py` builds.

The synthetic fixture produces no counted hard failure of its own, so every one
here is forged directly onto a real `happy` tree: an extra Perlector `failed`
record under a subject no page reading names, the same technique the tamper
tests in `test_orchestrator_acceptance.py` use to reach a state the fixture
cannot.
"""

import os
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.envelope import build_envelope
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ARMARIUM, DOOR, PERLECTOR, RECENSOR
from common.runtree.store import RunTree
from common.stage import _stage_seal_payload, latest_attempt
from conftest import file_bytes_snapshot as snapshot
from conftest import load_stage, programs_through
from conftest import run_orchestrator as orchestrate

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"
STAGES_THROUGH_PERLECTOR = programs_through("perlector")


def call_stage(
    run_root: Path, run_id: str, scenario: str, program: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def run_through_perlector(run_root: Path, run_id: str, scenario: str) -> None:
    for program in STAGES_THROUGH_PERLECTOR:
        result = call_stage(run_root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def forge_perlector_failure(tree: RunTree, fake_subject: str) -> None:
    """Write a second, unrelated PERLECTOR `failed` artifact directly to the tree.

    Not a real act — the proposal seal never names it — so no other stage ever
    reads it. It exists only to give the tally a second, distinct hard-failure
    subject without touching the two acts the fixture actually declares.
    """
    run = tree.read_run()
    envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(PERLECTOR, "perlectio", fake_subject),
        subject_id=fake_subject,
        stage=PERLECTOR,
        kind="perlectio",
        outcome="failed",
        config_digest=run["config_digest"],
        adapter_revision=run["adapter_recipes"][PERLECTOR],
        inputs=[],
        payload={"attempt_ordinal": 1, "reason": "forged for the hard-failure cap test"},
    )
    path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", envelope["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(envelope))
    tree.write_manifest(PERLECTOR)


def rebind_perlector_seal(tree: RunTree) -> None:
    """Model a coherent sealed predecessor after the cap evidence was retained."""
    seals = [
        tree.read_artifact(PERLECTOR, "stage-seal", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    seal = latest_attempt(seals, "perlector stage seal", operation="seal")
    payload = seal["payload"]
    # `_stage_seal_payload` derives the decode-environment name itself; it
    # takes no explicit fifth argument.
    seal["payload"] = _stage_seal_payload(
        tree,
        PERLECTOR,
        payload["attempt_ordinal"],
        seal["attempt_id"],
    )
    seal["self_hash"] = self_hash(seal)
    tree.resolve(tree.artifact_path(PERLECTOR, "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(seal)
    )
    tree.write_manifest(PERLECTOR)


def has_any_artifact(tree: RunTree, stage: str) -> bool:
    return bool(tree.build_manifest(stage)["artifacts"])


def test_more_than_two_hard_failures_halts_the_run_at_the_next_checkpoint(tmp_path):
    root = tmp_path / "runs"
    run_through_perlector(root, "r", "happy")

    tree = RunTree(root, "r")
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")
    forge_perlector_failure(tree, "fake-hard-failure-subject-3")

    result = orchestrate(root, "r", "happy")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "halted at the" in result.stdout
    # Durable failure evidence already on the tree is found before any stage is
    # re-entered, so the halt is the resume preflight's, not a later boundary's.
    # What happens when the third failure appears *during* a run is a different
    # path, driven below by `test_a_breach_first_seen_at_a_stage_boundary...`.
    assert "resume-preflight" in result.stdout

    # The Perlector section this test's own setup ran is still intact (nothing
    # was torn down), and nothing past the checkpoint that tripped was invoked.
    assert has_any_artifact(tree, PERLECTOR)
    assert not has_any_artifact(tree, RECENSOR)
    assert not has_any_artifact(tree, ARCHETYPUS)
    assert not has_any_artifact(tree, ARMARIUM)


def test_re_running_a_halted_orchestration_halts_again_the_same_way(tmp_path):
    """Idempotent: recomputed from disk, so a retry without a real fix repeats it."""
    root = tmp_path / "runs"
    run_through_perlector(root, "r", "happy")
    tree = RunTree(root, "r")
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")
    forge_perlector_failure(tree, "fake-hard-failure-subject-3")

    first = orchestrate(root, "r", "happy")
    door_manifest = tree.resolve(tree.manifest_path("door"))
    os.utime(door_manifest, ns=(1_000_000_000, 1_000_000_000))
    second = orchestrate(root, "r", "happy")
    assert first.returncode == second.returncode == 4
    assert door_manifest.stat().st_mtime_ns == 1_000_000_000
    assert not has_any_artifact(tree, RECENSOR)


def test_zero_hard_failures_never_mentions_the_cap(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "page-unbroken")
    assert result.returncode == 0, result.stderr
    assert "hard failure" not in result.stdout
    assert "halted" not in result.stdout


def test_a_direct_stage_refuses_a_halted_run_before_it_writes(tmp_path):
    """Direct entry shares the run cap and must refuse before writes."""
    root = tmp_path / "runs"
    run_through_perlector(root, "r", "happy")
    tree = RunTree(root, "r")
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")
    forge_perlector_failure(tree, "fake-hard-failure-subject-3")
    rebind_perlector_seal(tree)

    result = call_stage(root, "r", "happy", "pipeline/5_recensor/run.py")

    assert result.returncode == 4
    assert "RunHalted" in result.stderr
    assert "recensor refuses to start" in result.stderr
    assert not has_any_artifact(tree, RECENSOR)


def test_the_direct_door_also_refuses_a_halted_run_without_replaying_bytes(tmp_path):
    """Door bypasses ``open_context``, so it must apply the same gate explicitly."""
    root = tmp_path / "runs"
    run_through_perlector(root, "r", "happy")
    tree = RunTree(root, "r")
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")
    forge_perlector_failure(tree, "fake-hard-failure-subject-3")
    before = snapshot(root)

    result = call_stage(root, "r", "happy", "pipeline/1_exemplar/door.py")

    assert result.returncode == 4
    assert "door refuses to start" in result.stderr
    assert snapshot(root) == before


def test_an_unmeasurable_direct_entry_cap_refuses_instead_of_writing(tmp_path):
    """A damaged tally is a failed measurement, never an implicit zero count."""
    root = tmp_path / "runs"
    run_through_perlector(root, "r", "happy")
    tree = RunTree(root, "r")
    admission = next(
        entry for entry in tree.build_manifest(DOOR)["artifacts"] if entry["kind"] == "admission"
    )
    tree.resolve(admission["relative_path"]).write_bytes(b"not an artifact")

    result = call_stage(root, "r", "happy", "pipeline/5_recensor/run.py")

    assert result.returncode == 2
    assert "could not be read as an artifact" in result.stderr
    assert not has_any_artifact(tree, RECENSOR)


# --- The tally at a boundary reached mid-run -----------------------------------
#
# Every end-to-end test above forges its failures before the orchestrator starts,
# so all of them trip at `resume-preflight` — the branch before the sequence loop.
# The ruling's actual shape ("finishes that section but pauses") lives in the loop
# body. A forged Perlector failure stops the page-read Recensor on its own, and
# adding a scenario that produced one would move `config_digest` and every pinned
# run-tree digest with it. So the sequencing itself is driven directly instead.


def _breach(checkpoint_name: str) -> dict:
    return {
        "threshold": 2,
        "count": 3,
        "breached": True,
        "by_kind": {"perlector:failed": ["a1", "a2", "a3"]},
        "subjects": ["perlector:a1", "perlector:a2", "perlector:a3"],
        "checkpoint": checkpoint_name,
    }


def _argv(tmp_path: Path, *selection: str) -> list[str]:
    return [
        "run.py",
        "--fixture",
        FIXTURE,
        "--scenario",
        "happy",
        "--run-id",
        "r",
        "--run-root",
        str(tmp_path / "runs"),
        *selection,
    ]


def test_exactly_two_hard_failures_is_only_a_warning_and_the_run_continues(
    monkeypatch, tmp_path, capsys
):
    """Two is the named early warning: said at every boundary, and nothing stops."""
    orchestrator = load_stage("orchestrator")
    invoked: list[str] = []
    monkeypatch.setattr(orchestrator, "invoke", lambda program, _args: invoked.append(program))
    monkeypatch.setattr(
        orchestrator,
        "tally_hard_failures",
        lambda _tree, _policy: {
            "threshold": 2,
            "count": 2,
            "breached": False,
            "by_kind": {"perlector:failed": ["a1", "a2"]},
            "subjects": ["perlector:a1", "perlector:a2"],
        },
    )
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, "--from", "door", "--to", "recensor"))

    assert orchestrator.main() == orchestrator.EXIT_COMPLETE
    stages = ("door", "exemplar", "ink-map", "designator", "attestatores", "perlector", "recensor")
    assert invoked == [orchestrator.STAGE_PROGRAMS[name] for name in stages]
    printed = capsys.readouterr().out
    assert printed.count("2 hard failure(s) so far") == len(stages)
    assert "early warning" in printed
    assert "halted" not in printed


def test_a_breach_first_seen_at_a_stage_boundary_stops_the_rest_of_the_sequence(
    monkeypatch, tmp_path, capsys
):
    """Nothing after the boundary that tripped is invoked, and the halt is said.

    The cap's whole point is that it stops the run from spending more work, so
    "the stage after the breach was never invoked" is the assertion that matters,
    not merely the exit code.
    """
    orchestrator = load_stage("orchestrator")
    invoked: list[str] = []
    monkeypatch.setattr(orchestrator, "invoke", lambda program, _args: invoked.append(program))
    monkeypatch.setattr(
        orchestrator,
        "checkpoint",
        lambda _args, name, _policy: _breach(name) if name == "designator" else None,
    )
    monkeypatch.setattr(sys, "argv", _argv(tmp_path))

    assert orchestrator.main() == orchestrator.EXIT_RUN_HALTED
    assert invoked == [
        orchestrator.STAGE_PROGRAMS["door"],
        orchestrator.STAGE_PROGRAMS["exemplar"],
        orchestrator.STAGE_PROGRAMS["ink-map"],
        orchestrator.STAGE_PROGRAMS["designator"],
    ]
    printed = capsys.readouterr().out
    assert "halted at the designator checkpoint" in printed
    assert "perlector:failed" in printed


# --- The policy is sealed, and its point of use requires it ---------------------
#
# `config/hard_failure.toml` is the sealing family's fourth and last member. It is
# the one policy the orchestrator must read BEFORE the run exists -- the threshold
# has to be known to decide whether a resumed run may re-enter a stage at all --
# and then holds for the whole run. Until it was sealed, a rewrite between one
# orchestration and the next moved the cap that halts the run with nothing
# recording that it had moved.


def _shipped_hard_failure() -> str:
    return (ROOT / "config" / "hard_failure.toml").read_text(encoding="utf-8")


def test_the_run_authority_names_the_hard_failure_policy_it_was_sealed_under(
    orchestrated_run, tmp_path
):
    """Recorded by name, not merely folded into `config_digest`.

    A reader holding only the tree can say which hard-failure bytes governed the
    run, instead of testing a candidate file against one hash of everything.
    """
    from common.hard_failure import load_hard_failure_policy

    root = tmp_path / "runs"
    orchestrated_run(root, "sealed", "page-unbroken")

    run = RunTree(root, "sealed").read_run()
    assert (
        run["sealed_config_digests"]["hard-failure"]
        == load_hard_failure_policy(ROOT / "config" / "hard_failure.toml")["config_sha256"]
    )


def test_a_hard_failure_policy_swapped_between_orchestrations_is_refused_on_resume(tmp_path):
    """The point of use, and the first moment a run authority exists to hold it to.

    On a resume the orchestrator has both halves in hand -- the bytes it just read
    and the digest the run sealed -- so it proves them against each other before
    invoking anything. Without this the second orchestration would have re-entered
    every stage under a cap the run never sealed, and the run's own record would
    still name the old one.
    """
    root = tmp_path / "runs"
    policy = tmp_path / "hard_failure.toml"
    policy.write_text(_shipped_hard_failure(), encoding="utf-8")
    first = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            "page-unbroken",
            "--run-id",
            "swapped",
            "--run-root",
            str(root),
            "--hard-failure-config",
            str(policy),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr

    # The threshold is fixed at `HARD_FAILURE_THRESHOLD` and refuses any other value,
    # so the swap moves one [[kind]] to the end: a value change the resolved policy
    # sorts away, which only this file's seal can attribute.
    first_kind = '[[kind]]\nstage = "perlector"\noutcome = "failed"\n'
    shipped = _shipped_hard_failure()
    assert first_kind in shipped
    policy.write_text(shipped.replace(first_kind, "", 1) + "\n" + first_kind, encoding="utf-8")
    # The refusal must arrive before any stage is re-entered: a tree byte moving
    # under the resumed invocation would mean work was spent under the unsealed
    # cap before the proof fired.
    before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    second = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            "page-unbroken",
            "--run-id",
            "swapped",
            "--run-root",
            str(root),
            "--hard-failure-config",
            str(policy),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert second.returncode != 0, "a resumed run re-entered every stage under an unsealed cap"
    assert "hard-failure configuration changed between" in second.stderr, second.stderr
    after = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    assert after == before, "the refused resume wrote to the run tree before the proof fired"
