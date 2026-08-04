"""The Perlector accepts only crops bound to their actual sealed Exemplar page."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import region_id
from common.contracts.stages import DESIGNATOR, EXEMPLAR
from common.imaging import dimensions
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


def _load_attestatores():
    path = ROOT / "pipeline/3_attestatores/run.py"
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
            "region-boundary",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "region-boundary")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    return _Context(tree), tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])


def test_a_region_bound_to_its_actual_exemplar_input_verifies(real_region):
    context, region = real_region
    verified = perlector.verify_region(context, region)

    assert verified["source_page_ordinal"] == region["payload"]["transform"]["source_page_ordinal"]
    assert verified["source_page_id"] == region["payload"]["transform"]["source_page_id"]
    assert verified["transform"] == region["payload"]["transform"]


def test_attestatores_verifies_crop_lineage_before_a_witness_reads_it(real_region, monkeypatch):
    context, region = real_region
    monkeypatch.setattr(attestatores, "validate_serving_provenance", lambda *args, **kwargs: None)

    def refuse(*args, **kwargs):
        raise ContractError("crop-lineage marker")

    monkeypatch.setattr(attestatores, "verify_exemplar_crop_lineage", refuse)
    with pytest.raises(ContractError, match="crop-lineage marker"):
        attestatores.proposed_regions(context, region["subject_id"])


def test_a_crop_from_page_one_cannot_claim_another_valid_page(real_region):
    context, region = real_region
    other = next(
        page
        for page in (
            context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
            for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
        )
        if page["payload"]["ordinal"] != region["payload"]["transform"]["source_page_ordinal"]
    )
    mismatched = copy.deepcopy(region)
    mismatched["payload"]["transform"]["source_page_ordinal"] = other["payload"]["ordinal"]
    mismatched["payload"]["transform"]["source_page_id"] = other["subject_id"]
    mismatched["payload"]["region_id"] = region_id(
        mismatched["subject_id"], mismatched["payload"]["transform"]
    )

    with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
        perlector.verify_region(context, mismatched)


def test_malformed_exemplar_locators_all_refuse(real_region):
    context, region = real_region
    changes = [
        {"source_page_ordinal": None},
        {"source_page_ordinal": "1"},
        {"source_page_ordinal": True},
        {"source_page_ordinal": -1},
        {"source_page_id": None},
        {"source_page_id": ""},
        {"source_page_id": 7},
    ]
    for change in changes:
        malformed = copy.deepcopy(region)
        malformed["payload"]["transform"].update(change)
        with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
            perlector.verify_region(context, malformed)


def test_a_crop_transform_must_fit_inside_its_sealed_exemplar_page(real_region):
    context, region = real_region
    page = context.tree.read_artifact(
        EXEMPLAR,
        "page",
        next(
            entry["artifact_id"]
            for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
            and entry["subject_id"] == region["payload"]["transform"]["source_page_id"]
        ),
    )
    page_width, page_height = dimensions(context.tree.read_bytes(page["payload"]["image_path"]))
    original = region["payload"]["transform"]["bounds"]
    bad_bounds = [
        {**original, "x": -1},
        {**original, "y": -1},
        {**original, "x": page_width - original["w"] + 1},
        {**original, "y": page_height - original["h"] + 1},
    ]
    for bounds in bad_bounds:
        malformed = copy.deepcopy(region)
        malformed["payload"]["transform"]["bounds"] = bounds
        malformed["payload"]["region_id"] = region_id(
            malformed["subject_id"], malformed["payload"]["transform"]
        )
        with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
            perlector.verify_region(context, malformed)
