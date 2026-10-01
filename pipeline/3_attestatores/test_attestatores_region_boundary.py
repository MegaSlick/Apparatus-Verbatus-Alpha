"""Attestatores refuses an unverified Designator proposal before any page record names it."""

import copy
from pathlib import Path

import pytest

from common import page_testimonia
from common.chairs import ChairRegistry
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import DESIGNATOR
from common.runtree.store import RunTree
from conftest import load_stage, run_through

ROOT = Path(__file__).resolve().parents[2]


attestatores = load_stage("3_attestatores")


class _Context:
    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(ROOT / "config/models.toml")


@pytest.fixture
def real_region(tmp_path):
    run_through(tmp_path / "runs", "attestatores-boundary", "happy", "designator")
    tree = RunTree(tmp_path / "runs", "attestatores-boundary")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    return _Context(tree), tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])


def test_attestatores_verifies_crop_lineage_before_a_page_record_names_it(real_region, monkeypatch):
    context, _region = real_region
    monkeypatch.setattr(
        page_testimonia, "validate_serving_provenance", lambda *args, **kwargs: None
    )

    def refuse(*args, **kwargs):
        raise ContractError("crop-lineage marker")

    monkeypatch.setattr(page_testimonia, "verify_exemplar_crop_lineage", refuse)
    with pytest.raises(SchemaRefusal, match="crop-lineage marker"):
        attestatores.sealed_proposal_regions(context)


def test_attestatores_names_a_designator_region_with_missing_provenance(real_region, monkeypatch):
    context, region = real_region
    missing = copy.deepcopy(region)
    del missing["payload"]["provenance"]
    missing["self_hash"] = self_hash(missing)
    entry = next(
        entry
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["artifact_id"] == region["artifact_id"]
    )
    context.tree.resolve(entry["relative_path"]).write_bytes(canonical_bytes(missing))
    monkeypatch.setattr(context.tree, "build_manifest", lambda stage: {"artifacts": [entry]})

    with pytest.raises(SchemaRefusal, match="model provenance is not an object"):
        attestatores.sealed_proposal_regions(context)
