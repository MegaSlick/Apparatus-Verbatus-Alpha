"""Attestatores refuses an unverified crop before a chair is asked to read it."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.contracts.stages import DESIGNATOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()


class _Context:
    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()


@pytest.fixture
def real_region(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "attestatores-boundary",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "attestatores-boundary")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    return _Context(tree), tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])


def test_attestatores_verifies_crop_lineage_before_a_witness_reads_it(real_region, monkeypatch):
    context, region = real_region
    monkeypatch.setattr(attestatores, "validate_serving_provenance", lambda *args, **kwargs: None)

    def refuse(*args, **kwargs):
        raise ContractError("crop-lineage marker")

    monkeypatch.setattr(attestatores, "verify_exemplar_crop_lineage", refuse)
    with pytest.raises(ContractError, match="crop-lineage marker"):
        attestatores.proposed_regions(context, region["subject_id"])
