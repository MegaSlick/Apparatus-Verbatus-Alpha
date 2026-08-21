"""Fixtures shared by tests that cross pipeline stage directories."""

import shutil
import tomllib
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.stage import _stage_seal_payload, latest_attempt

ROOT = Path(__file__).resolve().parent


def _rebind_stage_seal(tree, stage: str, *, rewrite_manifest: bool = True) -> None:
    """Carry a deliberate semantic forgery through its producer's completion seal.

    A stage seal witnesses the producer's whole boundary, so any test that
    rewrites an upstream record now trips the seal first and proves the boundary
    refusal instead of the check it was written for. Rebinding models the other
    hypothesis — a producer that wrote the bad record *and* honestly witnessed
    it — which is the state the downstream check exists to catch and the only
    state in which it is reachable at all.

    Boundary-corruption tests deliberately do NOT rebind: leaving the seal alone
    is what proves the earlier refusal.

    `rewrite_manifest=False` is for the forgeries that leave a dangling input
    reference behind on purpose. `write_manifest` revalidates every input, so it
    would refuse here instead of at the consumer the test is aiming at — and the
    stored inventory is not what the seal is read out of anyway.
    """
    seals = [
        tree.read_artifact(stage, "stage-seal", entry["artifact_id"])
        for entry in tree.build_manifest(stage, verify_inputs=False)["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
    payload = seal["payload"]
    seal["payload"] = _stage_seal_payload(
        tree,
        stage,
        payload["attempt_ordinal"],
        seal["attempt_id"],
        payload["decode_environment_artifact_id"],
    )
    seal["self_hash"] = self_hash(seal)
    tree.resolve(tree.artifact_path(stage, "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(seal)
    )
    if rewrite_manifest:
        tree.write_manifest(stage)


@pytest.fixture
def rebind_stage_seal():
    """The shared spelling of the helper three stage directories each copied.

    Offered here so a new call site does not add a fifth copy. The copies in
    `pipeline/6_archetypus/reseal_chain.py` and the two stage test modules are
    left where they are on purpose: they predate this fixture, they are covered
    by the suite as written, and churning an audit candidate's tests for a
    maintainability-only gain is the wrong trade this late in a review chain.
    """
    return _rebind_stage_seal


@pytest.fixture
def absent_third_chair_config(tmp_path: Path) -> Path:
    """Copy the live model config and mark its third witness explicitly absent."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert tomllib.loads(live)["chairs"]["attestator_3"]["state"] == "configured"
    section_start = live.index("[chairs.attestator_3]\n")
    next_table = live.find("\n[", section_start + 1)
    section_end = len(live) - 1 if next_table == -1 else next_table
    absent = """[chairs.attestator_3]
state = "absent"
reason = "fixture test removes this witness without replacing it"
"""
    path = config_root / "models.toml"
    path.write_text(live[:section_start] + absent + live[section_end + 1 :], encoding="utf-8")
    rewritten = tomllib.loads(path.read_text(encoding="utf-8"))
    assert rewritten["chairs"]["attestator_3"]["state"] == "absent"
    assert set(rewritten["chairs"]) == set(tomllib.loads(live)["chairs"]), (
        "the splice changed which chairs the roster declares"
    )
    return path
