"""The recovery-budget-exhaustion hold branch, genuinely exercised.

The review fixture asks for an expanded fallback recrop.  Its request must spend
only the fallback-recrop allowance; a page-level-reread allowance is a distinct,
currently unimplemented operation and must not be silently consumed instead.
These real-stage tests drive both zero fallback capacity and a page-level-only
policy, so the guard is not a branch no configuration can falsify.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def test_a_genuinely_zero_budget_holds_the_act_and_names_it_spent(tmp_path):
    root = tmp_path / "runs"
    recovery_config = tmp_path / "zero-recovery.toml"
    recovery_config.write_text(
        "absolute_cap = 3\n[budget]\nfallback_recrop = 0\npage_level_reread = 0\n",
        encoding="utf-8",
    )
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "review",
                "--recovery-config",
                str(recovery_config),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/5_recensor/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "review",
            "--recovery-config",
            str(recovery_config),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, "r")
    reviews = [
        record
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
        for record in [tree.read_artifact(RECENSOR, "review", entry["artifact_id"])]
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "fallback-recrops" in reviews[0]["payload"]["reason"]
    assert "budget of 0" in reviews[0]["payload"]["reason"]


def test_a_page_level_allowance_never_becomes_a_fallback_recrop(tmp_path):
    root = tmp_path / "runs"
    recovery_config = tmp_path / "page-level-only.toml"
    recovery_config.write_text(
        "absolute_cap = 3\n[budget]\nfallback_recrop = 0\npage_level_reread = 1\n",
        encoding="utf-8",
    )
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "page-level-only",
                "--scenario",
                "review",
                "--recovery-config",
                str(recovery_config),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/5_recensor/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "page-level-only",
            "--scenario",
            "review",
            "--recovery-config",
            str(recovery_config),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, "page-level-only")
    assert [
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    ] == []
    review = next(
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
        and tree.read_artifact(RECENSOR, "review", entry["artifact_id"])["payload"]["act_key"]
        == "a1"
    )
    assert "page-level reread is not a substitute" in review["payload"]["reason"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
