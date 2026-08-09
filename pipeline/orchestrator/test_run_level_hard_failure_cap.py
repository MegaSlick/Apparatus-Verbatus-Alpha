"""The run-level hard-failure cap, driven over the real orchestrator.

Tyrel's ruling, 2026-08-05: two hard failures in a run is an early warning and
the run keeps going; more than two halts it at the next stage boundary, with
whatever finished intact. Distinct from `pipeline/5_recensor/test_recovery_
budget_exhaustion.py` and friends, which exercise the PER-ACT recovery budget —
this is the separate, RUN-level mechanism `common/hard_failure.py` builds.

The two-act synthetic fixture cannot organically produce three distinct hard
failures (there are only two acts, and a `truncated` Perlectio no longer counts
— see `config/hard_failure.toml`'s own comment: a dense page is not a damaged
one, the old pipeline's own Tyrel-ruled distinction), so every hard failure
here is forged directly onto a real tree — the same technique every tamper
test in `test_orchestrator_acceptance.py` already uses to reach a state the
happy path cannot. `truncated-reading` is still used as the driving scenario,
deliberately: it puts one genuine, real `truncated` Perlectio on the tree
alongside the forged failures, so these tests also stand as the end-to-end
proof that a real truncation does not, by itself, move the tally.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes
from common.contracts.envelope import build_envelope
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"


def invoke_stage(run_root: Path, run_id: str, scenario: str, program: str) -> None:
    result = subprocess.run(
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
    assert result.returncode == 0, f"{program}: {result.stderr}"


def orchestrate(run_root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


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


def has_any_artifact(tree: RunTree, stage: str) -> bool:
    return bool(tree.build_manifest(stage)["artifacts"])


def test_more_than_two_hard_failures_halts_the_run_at_the_next_checkpoint(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        invoke_stage(root, "r", "truncated-reading", program)

    tree = RunTree(root, "r")
    # a1's `truncated` Perlectio is real but does not count (see module
    # docstring); all three counted hard failures are forged.
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")
    forge_perlector_failure(tree, "fake-hard-failure-subject-3")

    result = orchestrate(root, "r", "truncated-reading")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "halted at the" in result.stdout
    assert "resume-preflight" in result.stdout

    # The section already in flight (perlector) finished -- its manifest exists --
    # but nothing past the checkpoint that tripped the cap was ever invoked.
    assert has_any_artifact(tree, PERLECTOR)
    assert not has_any_artifact(tree, RECENSOR)
    assert not has_any_artifact(tree, ARCHETYPUS)
    assert not has_any_artifact(tree, ARMARIUM)


def test_re_running_a_halted_orchestration_halts_again_the_same_way(tmp_path):
    """Idempotent: recomputed from disk, so a retry without a real fix repeats it."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        invoke_stage(root, "r", "truncated-reading", program)
    tree = RunTree(root, "r")
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")
    forge_perlector_failure(tree, "fake-hard-failure-subject-3")

    first = orchestrate(root, "r", "truncated-reading")
    door_manifest = tree.resolve(tree.manifest_path("door"))
    os.utime(door_manifest, ns=(1_000_000_000, 1_000_000_000))
    second = orchestrate(root, "r", "truncated-reading")
    assert first.returncode == second.returncode == 4
    assert door_manifest.stat().st_mtime_ns == 1_000_000_000
    assert not has_any_artifact(tree, RECENSOR)


def test_exactly_two_hard_failures_is_only_a_warning_and_the_run_continues(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        invoke_stage(root, "r", "truncated-reading", program)
    tree = RunTree(root, "r")
    # a1's real `truncated` Perlectio does not count; two forged failures is
    # exactly two, Tyrel's named "early warning" -- the run must not stop early.
    forge_perlector_failure(tree, "fake-hard-failure-subject-1")
    forge_perlector_failure(tree, "fake-hard-failure-subject-2")

    result = orchestrate(root, "r", "truncated-reading")
    assert result.returncode in (0, 3), result.stdout + result.stderr
    assert "early warning" in result.stdout
    assert "halted at the" not in result.stdout
    # The run reached the end of the sequence: Armarium produced its export.
    assert has_any_artifact(tree, ARMARIUM)


def test_zero_hard_failures_never_mentions_the_cap(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    assert "hard failure" not in result.stdout
    assert "halted" not in result.stdout


def test_a_real_truncated_reading_alone_never_mentions_the_cap(tmp_path):
    """A dense page is not a damaged one (the old pipeline's own Tyrel-ruled
    distinction, carried into `config/hard_failure.toml`'s comment). One
    genuine truncated Perlectio, with nothing forged, must not move the tally
    at all -- the run proceeds exactly as an ordinary held act would."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "truncated-reading")
    assert result.returncode in (0, 3), result.stdout + result.stderr
    assert "hard failure" not in result.stdout
    assert "halted" not in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
