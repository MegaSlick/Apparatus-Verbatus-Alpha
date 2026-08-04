"""The stage/run-receipt boundary for model-chair provenance."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from common.chairs import ChairIdentity, ChairRegistry
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree
from common.stage import (
    StageContext,
    adapter_recipe_for,
    fixture_serving_details,
    run_config_bindings,
    validate_serving_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS_CONFIG = ROOT / "config" / "models.toml"


def _context(tmp_path) -> tuple[StageContext, ChairIdentity]:
    registry = ChairRegistry.from_toml(MODELS_CONFIG)
    bindings = run_config_bindings(registry.config, {"fixture": "none"}, "test")
    tree = RunTree.create(
        tmp_path,
        "seat-boundary",
        source_manifest=[],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
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
    assert isinstance(identity, ChairIdentity)
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
    assert receipt["chair"] == identity.role
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
        "chair": identity.role,
        "chair_state": "configured",
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


def test_a_field_nothing_validates_is_refused_rather_than_carried(tmp_path):
    """Invariant #42 refuses wrong-schema provenance, not a list of known-bad names.

    The two serving-only leaks are named and refused above them, which catches the
    mistake we have already made. This catches the one we have not: a field a later
    stage invents travels inside a sealed reading with nothing checking it, and the
    reading still verifies. An allowlist is the only shape that closes that.
    """
    context, identity = _context(tmp_path)
    reference = context.write_serving_receipt(identity, fixture_serving_details(identity))
    provenance = {
        "chair": identity.role,
        "chair_state": "configured",
        "resolved_identity": identity.to_record(),
        "resolved_revision": {
            "kind": identity.receipt_revision_kind,
            "value": identity.receipt_revision,
        },
        "receipt_ref": reference,
        "adapter_revision": context.adapter_revision,
    }
    with pytest.raises(SchemaRefusal, match="unknown field"):
        validate_serving_provenance(
            context,
            {**provenance, "confidence": 0.9},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    # An allowlist says which fields may exist, never which may exist together.
    # `absence` is legal provenance on an absent chair, so the closed schema admits
    # it — and a configured chair carrying one is two contradictory claims about the
    # same chair, sealed into a reading that still verified. Found by the Terra
    # review seat, which reproduced it with an absence for a chair that never existed.
    with pytest.raises(SchemaRefusal, match="carries an absence record"):
        validate_serving_provenance(
            context,
            {
                **provenance,
                "absence": {"role": "attestator_99", "state": "absent", "reason": "fabricated"},
            },
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_a_witness_regime_that_cannot_be_true_is_refused(tmp_path):
    """ARCHITECTURE: the named/blinded toggle is run-level and every Perlectio
    records its regime. The Perlector writes the field; until this check nothing
    read it back, so a Perlectio claiming a regime that does not exist travelled
    sealed and provenance-checked. Binding it to a real run-level toggle is Spec
    08's work; refusing a value that cannot be true is provenance validation.
    """
    context, identity = _context(tmp_path)
    reference = context.write_serving_receipt(identity, fixture_serving_details(identity))
    provenance = {
        "chair": identity.role,
        "chair_state": "configured",
        "resolved_identity": identity.to_record(),
        "resolved_revision": {
            "kind": identity.receipt_revision_kind,
            "value": identity.receipt_revision,
        },
        "receipt_ref": reference,
        "adapter_revision": context.adapter_revision,
    }
    for regime in ("named", "blinded"):
        assert (
            validate_serving_provenance(
                context,
                {**provenance, "witness_regime": regime},
                producer_stage=ATTESTATORES,
                require_receipt=True,
            )
            == identity
        )

    with pytest.raises(SchemaRefusal, match="witness regime"):
        validate_serving_provenance(
            context,
            {**provenance, "witness_regime": "anonymised"},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )
