"""The stage/run-receipt boundary for model-seat provenance."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree
from common.seats import SeatIdentity, SeatRegistry
from common.stage import (
    StageContext,
    adapter_recipe_for,
    fixture_serving_details,
    run_config_bindings,
    validate_serving_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS_CONFIG = ROOT / "config" / "models.toml"


def _context(tmp_path) -> tuple[StageContext, SeatIdentity]:
    registry = SeatRegistry.from_toml(MODELS_CONFIG)
    bindings = run_config_bindings(registry.config, {"fixture": "none"}, "test")
    tree = RunTree.create(
        tmp_path,
        "seat-boundary",
        source_manifest=[],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_seats=bindings["witness_seats"],
    )
    run = tree.read_run()
    context = StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario="test",
        stage=ATTESTATORES,
        adapter_revision=adapter_recipe_for(run, ATTESTATORES),
        args=object(),
        registry=registry,
    )
    identity = registry.resolve("attestator_1")
    assert isinstance(identity, SeatIdentity)
    return context, identity


def test_serving_receipts_are_refused_as_stage_artifacts_and_accepted_as_run_receipts(tmp_path):
    context, identity = _context(tmp_path)

    with pytest.raises(SchemaRefusal) as refused:
        context.publish(
            kind="serving-receipt",
            subject_id="not-an-artifact",
            outcome="read",
            payload={},
        )
    assert "run receipts" in str(refused.value)

    reference = context.write_serving_receipt(identity, fixture_serving_details(identity))
    receipt = context.tree.read_run_receipt(reference)
    assert receipt["seat"] == identity.role
    assert receipt["revision"] == identity.receipt_revision


def test_receipt_reuse_is_by_full_serving_moment_not_only_model_identity(tmp_path):
    context, identity = _context(tmp_path)
    original = fixture_serving_details(identity)
    changed = replace(
        original,
        endpoint="fixture://offline-seat-runner-restarted",
        started_at="2026-08-03T00:01:00Z",
    )

    first = context.write_serving_receipt(identity, original)
    second = context.write_serving_receipt(identity, changed)

    assert first != second
    assert context.tree.read_run_receipt(first)["endpoint"] == original.endpoint
    assert context.tree.read_run_receipt(second)["endpoint"] == changed.endpoint


def test_a_consumer_refuses_tampered_model_provenance_and_receipt_reference(tmp_path):
    context, identity = _context(tmp_path)
    reference = context.write_serving_receipt(identity, fixture_serving_details(identity))
    provenance = {
        "seat": identity.role,
        "seat_state": "configured",
        "resolved_identity": identity.to_record(),
        "resolved_revision": {
            "kind": identity.receipt_revision_kind,
            "value": identity.receipt_revision,
        },
        "receipt_ref": reference,
        "adapter_revision": context.adapter_revision,
    }
    assert (
        validate_serving_provenance(
            context,
            provenance,
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )
        == identity
    )

    altered_revision = {
        **provenance,
        "resolved_revision": {"kind": "digest-manifest", "value": "0" * 64},
    }
    with pytest.raises(SchemaRefusal, match="resolved revision"):
        validate_serving_provenance(
            context,
            altered_revision,
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    altered_reference = {
        **provenance,
        "receipt_ref": {**reference, "sha256": "0" * 64},
    }
    with pytest.raises(SchemaRefusal, match="content-addressed path"):
        validate_serving_provenance(
            context,
            altered_reference,
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )
