"""The orchestrator drives a page-read run from the Door through the Recensor's recovery member.

A page-read run asks for no recovery, so the recovery member finds no request
and lets the run on. The committed roster seals three page witnesses against a
witness floor of 3, and every unit of `happy` is accepted.
"""

from __future__ import annotations

import json

from conftest import run_orchestrator


def test_the_orchestrator_reads_pages_and_the_recensor_accepts_every_unit(tmp_path):
    root = tmp_path / "runs"
    result = run_orchestrator(root, "r", "happy", **{"from": "door", "to": "recovery"})
    assert result.returncode == 0, result.stderr
    artifacts = root / "r" / "5_recensor" / "artifacts"
    reviews = [json.loads(path.read_text()) for path in (artifacts / "review").glob("*.json")]
    assert sorted(review["payload"]["act_key"] for review in reviews) == ["p1:1", "p1:2", "p2:1"]
    assert {review["outcome"] for review in reviews} == {"accepted"}
    assert not (artifacts / "recovery-request").exists()
    assert {review["payload"]["coverage"]["configured"] for review in reviews} == {3}
    assert not any(review["payload"]["coverage"]["under_witnessed"] for review in reviews)
    assert (artifacts / "stage-seal").exists()
    receipt = json.loads(
        (root / "r" / "run-health" / "recensor-partition-receipt.json").read_text()
    )
    assert (receipt["schema"], receipt["recensor_status"]) == (
        "recensor-partition-receipt.v3",
        "complete",
    )
    assert not (root / "r" / "6_archetypus").exists()
