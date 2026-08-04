"""The recovery-budget-exhaustion hold branch, genuinely exercised.

`pipeline/5_recensor/run.py::main`'s `wants_recovery = act_key in
scenario["recover_acts"] and used == 0` means an act can be granted at most one
recovery-request in its whole lifetime, so with the shipped
`config/recovery.toml` (allowed = fallback_recrop(1) + page_level_reread(1) = 2)
the "recovery budget ... is spent" held-for-review branch can only ever fire on
an act's very FIRST Recensor pass, when `used` is genuinely 0 and the configured
budget is genuinely 0 too. No shipped scenario configures a zero budget, so
nothing in the suite ever drove this branch — the exact "unfalsifiable check"
this round exists to find and close. This does not change recovery.toml's
shipped default or the review scenario's one-recovery-then-accepted story; it
only proves the branch that names a spent budget actually behaves as its own
message claims when a budget genuinely is spent.
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
    assert "budget" in reviews[0]["payload"]["reason"]
    assert "spent" in reviews[0]["payload"]["reason"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
