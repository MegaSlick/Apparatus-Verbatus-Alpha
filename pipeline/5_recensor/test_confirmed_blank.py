"""Blank confirmation: the Recensor's other terminal outcome for a non-completed
reading, alongside `held-for-review`.

ARCHITECTURE and spec 09 both name this: "a zero-output unit is diagnosed, then
either sealed confirmed-blank with evidence or held unresolved-with-evidence.
Never quietly completed." Before this build, `confirmed-blank` was a real member
of the outcome algebra (`common/contracts/outcomes.py`) that nothing ever
produced -- every `no-readable-text` Perlectio was held for review forever, with
no path to ever close the act.

The window pass (2026-08-05, `/out/report.md`) found the old pipeline's own
hard-won rule for this: a blank verdict may never rest on fewer than several
genuinely independent completed reads, and never on a reader's own second
opinion. This is unanimity about an absence, never a selection among presences
(GOVERNANCE 3) -- the Perlector's own direct reading of the ink already found
nothing; the witnesses only corroborate or contradict that finding, and a single
dissenting witness holds the act for a human rather than being outvoted.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_module(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECENSOR_RUN = _load_module("pipeline/5_recensor/run.py", "recensor_run_confirmed_blank")


def _invoke(root: Path, run_id: str, scenario: str, program: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _run_through_recensor(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    result = None
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _invoke(root, run_id, scenario, program)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    return result


def _review_of(tree: RunTree, act_key: str) -> dict:
    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    matches = [review for review in reviews if review["payload"]["act_key"] == act_key]
    assert len(matches) == 1
    return matches[0]


def test_unanimous_absence_seals_confirmed_blank(tmp_path):
    """The Perlector's own `no-readable-text`, corroborated by every one of the
    three configured witnesses independently reporting `genuinely-empty`."""
    root = tmp_path / "runs"
    result = _run_through_recensor(root, "r", "confirmed-blank")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "r")
    review = _review_of(tree, "a1")
    assert review["outcome"] == "confirmed-blank"
    assert review["payload"]["coverage"]["by_outcome"] == {"genuinely-empty": 3}
    assert "attestator_1" in review["payload"]["reason"]
    assert "attestator_2" in review["payload"]["reason"]
    assert "attestator_3" in review["payload"]["reason"]

    # Spec 09 seals a blank "with evidence", and a sentence is not evidence a
    # consumer can read. The same facts are recorded as data beside the prose.
    assert review["payload"]["blank_evidence"] == {
        "perlector_outcome": "no-readable-text",
        "corroborating_chairs": ["attestator_1", "attestator_2", "attestator_3"],
        "residual_ink_clear_pages": [1],
    }

    # The act this scenario does not touch reads and accepts exactly as ever --
    # blank confirmation is additive, not a change to the ordinary path.
    other = _review_of(tree, "a2")
    assert other["outcome"] == "accepted"


def test_a_dissenting_witness_holds_instead_of_confirming_blank(tmp_path):
    """Same Perlector finding (`no-readable-text`), but only two of three chairs
    agree -- the third reads real text. GOALS 1: a single dissent is never
    silently resolved, so the act is held for a human, never outvoted."""
    root = tmp_path / "runs"
    result = _run_through_recensor(root, "r", "blank-with-dissent")
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, "r")
    review = _review_of(tree, "a1")
    assert review["outcome"] == "held-for-review"
    assert "no-readable-text" in review["payload"]["reason"]
    assert review["payload"]["coverage"]["by_outcome"] == {"read": 1, "genuinely-empty": 2}
    # No evidence field at all, rather than an empty one: nothing was sealed, so
    # there is nothing this act was sealed on.
    assert "blank_evidence" not in review["payload"]


def test_confirmed_blank_is_a_completed_class_terminal_outcome(tmp_path):
    """`confirmed-blank` does not hold the run -- unlike `held-for-review`, it is
    COMPLETED-class (`common/contracts/outcomes.py`) and the run reaches EXIT_COMPLETE
    when it is the only unusual act."""
    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "confirmed-blank",
            "--run-id",
            "r",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "run r: complete" in result.stdout


# --- blank_corroboration: the pure gate, exercised directly -----------------


def test_under_witnessed_coverage_never_corroborates_blank():
    coverage = {"under_witnessed": True, "unresolved_chairs": 0}
    outcomes = {"attestator_1": "genuinely-empty"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes) is None


def test_an_unresolved_chair_never_corroborates_blank():
    coverage = {"under_witnessed": False, "unresolved_chairs": 1}
    outcomes = {"attestator_1": "genuinely-empty", "attestator_2": "not-run"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes) is None


def test_zero_completed_chairs_never_corroborates_blank():
    """`under_witnessed` guards the ordinary floor, but a floor of zero must not
    let an empty completed set stand in for positive evidence of absence."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0}
    outcomes = {"attestator_1": "failed", "attestator_2": "dead"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes) is None


def test_one_dissenting_read_refuses_corroboration():
    coverage = {"under_witnessed": False, "unresolved_chairs": 0}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "read",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes) is None


def test_unanimous_genuinely_empty_corroborates_blank():
    coverage = {"under_witnessed": False, "unresolved_chairs": 0}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes) == [
        "attestator_1",
        "attestator_2",
        "attestator_3",
    ]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
