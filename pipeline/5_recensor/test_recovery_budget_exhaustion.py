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

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
MODELS_CONFIG = ROOT / "config" / "models.toml"


def _load_recensor():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("recensor_budget_exhaustion_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_genuinely_zero_budget_holds_the_act_and_names_it_spent(tmp_path, monkeypatch):
    root = tmp_path / "runs"
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
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    recensor = _load_recensor()
    monkeypatch.setattr(
        recensor, "recovery_budget", lambda root="config": {"allowed": 0, "absolute_cap": 3}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(recensor.__file__),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "review",
        ],
    )
    exit_code = recensor.main(registry_factory=ChairRegistry.from_toml)
    assert exit_code == 3  # EXIT_HELD

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
