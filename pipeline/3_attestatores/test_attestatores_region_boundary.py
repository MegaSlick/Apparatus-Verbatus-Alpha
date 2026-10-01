"""Attestatores refuses an unverified Designator record crop before any page record names it."""

import copy

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR
from common.stage import open_context, stage_parser
from conftest import load_stage, run_through

attestatores = load_stage("3_attestatores")
RUN_ID = "attestatores-boundary"


@pytest.fixture
def designated(tmp_path):
    root = tmp_path / "runs"
    run_through(root, RUN_ID, "happy", "designator")
    args = stage_parser("region boundary test").parse_args(
        ["--run-root", str(root), "--run-id", RUN_ID, "--scenario", "happy"]
    )
    return open_context(args, ATTESTATORES)


def _first_region_entry(context):
    return next(
        entry
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "detector-region"
    )


def test_a_record_crop_that_does_not_rederive_from_its_page_is_refused(designated, monkeypatch):
    monkeypatch.setattr(attestatores, "crop_png", lambda *_args: b"not the sealed crop")
    with pytest.raises(SchemaRefusal, match="does not re-derive from its sealed page"):
        attestatores.detector_units_by_page(designated)


def test_a_record_crop_rewritten_under_its_record_is_refused_by_name(designated):
    context = designated
    entry = _first_region_entry(context)
    region = context.tree.read_artifact(DESIGNATOR, "detector-region", entry["artifact_id"])
    rewritten = copy.deepcopy(region)
    rewritten["payload"]["provenance"] = {**rewritten["payload"]["provenance"], "chair": "other"}
    rewritten["self_hash"] = self_hash({k: v for k, v in rewritten.items() if k != "self_hash"})
    context.tree.resolve(entry["relative_path"]).write_bytes(canonical_bytes(rewritten))

    with pytest.raises(SchemaRefusal, match="changed under a sealed reference"):
        attestatores.detector_units_by_page(context)
    assert not [
        entry
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
