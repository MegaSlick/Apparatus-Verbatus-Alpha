"""The two new fixture scenarios that exercise the truncation detector and the
no-readable-text path end to end, over the real orchestrator.
"""

import subprocess
import sys
from pathlib import Path

from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import ARCHETYPUS, PERLECTOR, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def orchestrate(run_root: Path, run_id: str, scenario: str):
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _perlectio_for(tree, act_key):
    return next(
        record
        for record in (
            tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
            for entry in tree.build_manifest(PERLECTOR)["artifacts"]
            if entry["kind"] == "perlectio"
        )
        if record["payload"]["act_key"] == act_key
    )


def test_engine_declared_length_produces_truncated_with_no_reading_failure_declared(tmp_path):
    """Unlike `truncated-reading`, this scenario declares no `reading_failure`
    row at all -- the outcome must come purely from the truncation detector
    reading the fixture's declared engine stop-reason."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "engine-truncated-reading")
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    reading = _perlectio_for(tree, "a1")
    assert reading["outcome"] == "truncated"
    assert reading["payload"]["truncation"]["classification"] == "truncated"
    assert reading["payload"]["truncation"]["signals"]["stop_reason_declared"] == "length"
    assert classify(PERLECTOR, reading["outcome"]) is OutcomeClass.FAILED

    # And the Recensor holds it rather than establishing stale text over a
    # truncated reading -- the exact hazard this scenario exists to drive.
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    assert review["outcome"] == "held-for-review"
    established = [
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    ]
    assert "a1" not in {
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])["payload"]["act_key"]
        for entry in established
    }


def test_no_readable_text_forces_empty_reading_and_a_whole_act_gap(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "no-readable-text-reading")
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    reading = _perlectio_for(tree, "a1")
    assert reading["outcome"] == "no-readable-text"
    assert reading["payload"]["text"] == ""
    assert classify(PERLECTOR, reading["outcome"]) is OutcomeClass.UNRESOLVED
    gaps = reading["payload"]["gaps"]
    assert len(gaps) == 1
    assert gaps[0]["position"] == "whole-act"
    assert gaps[0]["start"] == gaps[0]["end"] == 0
    # The witnesses' actual reports still travel as linked evidence, even
    # though nothing they said entered `text`.
    assert gaps[0]["witness_evidence"], (
        "the witnesses' variants must be attached to the gap as evidence, not "
        "silently dropped merely because the Perlector could not read the ink"
    )

    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    assert review["outcome"] == "held-for-review"


def test_the_second_act_is_unaffected_by_the_first_acts_declared_failure(tmp_path):
    """Both new scenarios declare a failure for a1 only; a2 must still read
    and establish normally, proving the declaration is scoped per-act."""
    for scenario in ("engine-truncated-reading", "no-readable-text-reading"):
        root = tmp_path / scenario
        result = orchestrate(root, "r", scenario)
        assert result.returncode == 3, result.stderr
        tree = RunTree(root, "r")
        reading = _perlectio_for(tree, "a2")
        assert reading["outcome"] == "read"
        assert reading["payload"]["text"] != ""
        established = {
            tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])["payload"]["act_key"]
            for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
            if entry["kind"] == "archetypus"
        }
        assert "a2" in established
