"""The staged driver is one sequence, irrespective of how an operator enters it."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

from common.runtree.store import RunTree
from common.stage import EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"
# Keep this list independent of the implementation: importing the production
# sequence would make the byte-identity test accept the same missing member.
SEQUENCE = (
    "door",
    "exemplar",
    "designator",
    "attestatores",
    "perlector",
    "recensor",
    "recovery",
    "archetypus",
    "armarium",
)


def drive(root: Path, run_id: str, scenario: str, *selection: str) -> subprocess.CompletedProcess:
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
            str(root),
            *selection,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def snapshot(root: Path) -> dict[str, bytes]:
    """Require literal tree identity; semantic normalization would hide mode leaks."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_all_and_manual_stages_write_the_identical_happy_run_tree(tmp_path):
    automatic = tmp_path / "automatic"
    manual = tmp_path / "manual"

    assert drive(automatic, "r", "happy", "--all").returncode == 0
    for stage in SEQUENCE:
        result = drive(manual, "r", "happy", "--stage", stage)
        assert result.returncode == 0, result.stdout + result.stderr

    assert snapshot(manual) == snapshot(automatic)


def test_all_and_a_split_semi_range_write_the_identical_happy_run_tree(tmp_path):
    automatic = tmp_path / "automatic"
    split = tmp_path / "split"

    assert drive(automatic, "r", "happy", "--all").returncode == 0
    first = drive(split, "r", "happy", "--from", "door", "--to", "recensor")
    assert first.returncode == 0, first.stdout + first.stderr
    second = drive(split, "r", "happy", "--from", "recovery", "--to", "armarium")
    assert second.returncode == 0, second.stdout + second.stderr

    assert snapshot(split) == snapshot(automatic)


def test_recovery_is_a_manual_sequence_member_with_its_own_contiguous_seal_attempt(tmp_path):
    automatic = tmp_path / "automatic"
    manual = tmp_path / "manual"

    assert drive(automatic, "r", "review", "--all").returncode == EXIT_HELD
    for stage in SEQUENCE:
        result = drive(manual, "r", "review", "--stage", stage)
        expected = EXIT_HELD if stage in {"recensor", "armarium"} else 0
        assert result.returncode == expected, result.stdout + result.stderr

    assert snapshot(manual) == snapshot(automatic)
    tree = RunTree(manual, "r")
    seals = [
        tree.read_artifact("designator", "stage-seal", entry["artifact_id"])
        for entry in tree.build_manifest("designator")["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    # Even unassigned page geometry must produce an explicitly sequenced,
    # sealed recovery round.
    assert sorted(seal["payload"]["attempt_ordinal"] for seal in seals) == [1, 2, 3]


def test_from_refuses_an_unsealed_predecessor_by_name(tmp_path):
    root = tmp_path / "runs"
    assert drive(root, "r", "happy", "--stage", "door").returncode == 0

    result = drive(root, "r", "happy", "--from", "designator", "--to", "designator")

    assert result.returncode == 2
    assert "predecessor exemplar has no stage-seal" in result.stderr


def test_invalid_selection_combinations_refuse_before_creating_a_tree(tmp_path):
    cases = (
        ("--all", "--stage", "door"),
        ("--all", "--from", "door", "--to", "exemplar"),
        ("--all", "--to", "door"),
        ("--stage", "door", "--to", "door"),
        ("--from", "door"),
        ("--to", "door"),
        ("--from", "exemplar", "--to", "door"),
        ("--stage", "door", "--mode", "auto"),
        ("--mode", "manual"),
    )

    for index, selection in enumerate(cases):
        root = tmp_path / str(index)
        result = drive(root, "r", "happy", *selection)
        assert result.returncode == 2, (selection, result.stdout, result.stderr)
        assert not root.exists(), selection


def test_semi_mode_stops_at_a_named_hold(tmp_path):
    result = drive(
        tmp_path / "runs", "r", "review", "--from", "door", "--to", "recensor", "--mode", "semi"
    )

    assert result.returncode == EXIT_HELD
    assert "semi mode stopped at held recensor" in result.stdout


def test_a_held_armarium_reports_its_terminal_reasons_under_every_mode(tmp_path):
    """Armarium holds must retain the terminal report's named partial reasons."""
    automatic = tmp_path / "automatic"
    manual = tmp_path / "manual"
    semi = tmp_path / "semi"

    all_result = drive(automatic, "r", "review", "--all")
    assert all_result.returncode == EXIT_HELD
    assert "run r: partial" in all_result.stdout
    assert "act a2 is held-for-review" in all_result.stdout

    for stage in SEQUENCE[:-1]:
        drive(manual, "r", "review", "--stage", stage)
    manual_result = drive(manual, "r", "review", "--stage", "armarium")
    assert manual_result.returncode == EXIT_HELD
    assert "run r: partial" in manual_result.stdout
    assert "act a2 is held-for-review" in manual_result.stdout

    drive(semi, "r", "review", "--from", "door", "--to", "recensor")
    semi_result = drive(semi, "r", "review", "--from", "recovery", "--to", "armarium")
    assert semi_result.returncode == EXIT_HELD
    assert "run r: partial" in semi_result.stdout
    assert "act a2 is held-for-review" in semi_result.stdout


def test_a_damaged_armarium_decode_environment_stops_the_run_at_its_producer(tmp_path):
    """A deleted terminal ``decode-environment`` refuses, and nothing is exported.

    This test was written when the seal only *named* its decode-environment, so
    the producer could reuse the seal after this damage and only the orchestrator
    could diagnose it. work/staged-stage-seal then made the seal bind that
    record's bytes as ``decode_environment_sha256``, which means the producer now
    reads it while sealing and refuses first. That is the stronger of the two
    behaviours and the merge keeps it, so the refusal this asserts moved one stage
    upstream. The orchestrator's own half of the pair did not go unproven with it:
    ``common/test_stage_seal.py`` drives ``verify_final_seal`` against exactly this
    damage, on the layer that still owns the check.
    """
    root = tmp_path / "runs"
    assert drive(root, "r", "happy", "--all").returncode == 0
    record = next((root / "r" / "7_armarium" / "artifacts" / "decode-environment").iterdir())
    kept = record.read_bytes()
    record.unlink()

    refused = drive(root, "r", "happy", "--all")

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "armarium cannot seal its boundary" in refused.stderr
    assert "decode-environment" in refused.stderr and "is unreadable" in refused.stderr
    assert "run r: complete" not in refused.stdout
    record.write_bytes(kept)
    assert drive(root, "r", "happy", "--all").returncode == 0


def test_an_attestatores_prework_hold_leaves_a_boundary_the_next_stage_refuses(tmp_path):
    """A pre-write Attestatores hold leaves no boundary later stages may cross.

    Otherwise ``--from`` could advance past a hold that ``--all`` stops for.
    """
    root = tmp_path / "runs"
    assert drive(root, "r", "happy", "--from", "door", "--to", "attestatores").returncode == 0
    testimonium = next((root / "r" / "3_attestatores" / "artifacts" / "testimonium").iterdir())
    testimonium.unlink()

    held = drive(root, "r", "happy", "--stage", "attestatores")
    assert held.returncode == EXIT_HELD, held.stdout + held.stderr
    assert "attempt tally UNKNOWN" in held.stderr

    advanced = drive(root, "r", "happy", "--from", "perlector", "--to", "armarium")

    assert advanced.returncode == 2, advanced.stdout + advanced.stderr
    assert "perlector refuses attestatores stage-seal" in advanced.stderr
    assert not (root / "r" / "4_perlector").exists()


def test_every_mode_checkpoints_a_held_member_before_it_stops(monkeypatch, tmp_path):
    """A run that is both held and over the cap has two stop reasons; the cap wins.

    The two-act fixture cannot organically cross the cap mid-sequence, so this
    test must inject the otherwise unreachable boundary state.
    """
    spec = importlib.util.spec_from_file_location("orchestrator_run_modes", ORCHESTRATOR)
    orchestrator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orchestrator)
    args = argparse.Namespace(run_root=str(tmp_path), run_id="r")
    breach = {
        "threshold": 2,
        "count": 3,
        "breached": True,
        "by_kind": {},
        "checkpoint": "designator",
    }

    checkpointed: list[str] = []

    def hold(_program, _args, **_extra):
        return orchestrator.EXIT_HELD

    def record(_args, name, _policy):
        checkpointed.append(name)
        return None

    monkeypatch.setattr(orchestrator, "invoke", hold)
    cases = (
        ("semi", ("designator", "attestatores"), "designator"),
        ("manual", ("designator",), "designator"),
        ("auto", ("attestatores",), "attestatores"),
        ("semi", ("attestatores", "perlector"), "attestatores"),
        ("manual", ("attestatores",), "attestatores"),
    )
    for mode, names, stopped_at in cases:
        checkpointed.clear()
        monkeypatch.setattr(orchestrator, "checkpoint", record)
        assert orchestrator.run_sequence(args, names, mode, {}) == EXIT_HELD
        assert checkpointed == [stopped_at], f"{mode} skipped its held member's checkpoint"

        monkeypatch.setattr(orchestrator, "checkpoint", lambda _args, _name, _policy: breach)
        assert orchestrator.run_sequence(args, names, mode, {}) == orchestrator.EXIT_RUN_HALTED
