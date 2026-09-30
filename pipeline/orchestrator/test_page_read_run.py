"""The orchestrator drives a page-read run from the Door through the Recensor's recovery member.

A page-read run asks for no recovery, so the recovery member finds no request
and lets the run on. The page-read roster seals three page witnesses against a
witness floor of 3, and every unit of `happy` is accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import page_roster_options, run_orchestrator

ROOT = Path(__file__).resolve().parents[2]


def _configs(directory: Path) -> dict[str, Path]:
    protocol = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    assert 'reading_unit = "act"' in protocol
    directory.mkdir(parents=True)
    protocol_path = directory / "perlector_protocol.toml"
    protocol_path.write_text(protocol.replace('reading_unit = "act"', 'reading_unit = "page"'))
    return {
        "perlector_protocol_config": protocol_path,
        **page_roster_options(directory / "models"),
    }


def test_the_orchestrator_reads_pages_and_the_recensor_accepts_every_unit(tmp_path):
    root = tmp_path / "runs"
    result = run_orchestrator(
        root, "r", "happy", **_configs(tmp_path / "config"), **{"from": "door", "to": "recovery"}
    )
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
