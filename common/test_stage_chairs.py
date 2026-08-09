"""The stage/run-receipt boundary for model-chair provenance."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from common.chairs import AbsentChair, ChairIdentity, ChairRegistry, build_receipt
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import ATTESTATORES, PERLECTOR
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


def test_run_config_bindings_refuses_a_witness_context_missing_a_configured_chair(tmp_path):
    """Audit finding: an incomplete `witness_context.toml` used to refuse only at
    the Perlector, after the Exemplar, Designator and the entire Attestatores leg
    had already run against every witness model on every act -- the expensive
    part of a live pod run, spent on what is usually a config typo.
    `run_config_bindings` already holds `models.witness_chairs` and already reads
    this file's bytes for the digest, so the coverage refusal belongs here, at
    run creation, before any of that work starts."""
    registry = ChairRegistry.from_toml(MODELS_CONFIG)
    incomplete = tmp_path / "witness_context.toml"
    incomplete.write_text('[attestator_1]\ntraining_domain = "only one witness declared"\n')

    with pytest.raises(ContractError, match="has no declared entry"):
        run_config_bindings(
            registry.config,
            {"fixture": "none"},
            "test",
            witness_context_config_path=incomplete,
        )


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
        serving_config_inputs=bindings["serving_config_inputs"],
    )
    identity = registry.resolve("attestator_1")
    assert isinstance(identity, ChairIdentity)
    return context, identity


def _served_provenance(
    context: StageContext,
    identity: ChairIdentity,
    *,
    reference: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build one otherwise-valid configured-chair projection."""
    if reference is None:
        reference = context.write_serving_receipt(identity, fixture_serving_details(identity))
    return {
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


def _declared_absence(context: StageContext) -> tuple[AbsentChair, dict[str, object]]:
    absent = context.registry.resolve("secondary_proposer")
    assert isinstance(absent, AbsentChair)
    return absent, {
        "chair": absent.role,
        "chair_state": "absent",
        "absence": absent.to_record(),
        "resolved_identity": None,
        "resolved_revision": None,
        "receipt_ref": None,
        "adapter_revision": context.adapter_revision,
    }


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


def test_serving_launch_audit_is_a_content_addressed_stage_blob(tmp_path):
    context, _ = _context(tmp_path)
    audit = {
        "schema": "serving-launch-audit.v1",
        "chair": "attestator_1",
        "started_at": "2026-08-09T12:00:00Z",
        "configuration_inputs": dict(context.serving_config_inputs),
    }

    first = context.write_serving_launch_audit(audit)
    second = context.write_serving_launch_audit(audit)

    assert first == second
    stored = context.tree.read_bytes(first["relative_path"])
    assert b'"configuration_inputs"' in stored
    assert context.serving_config_inputs["serving_recipes_sha256"].encode() in stored
    with pytest.raises(SchemaRefusal, match="non-empty"):
        context.write_serving_launch_audit({})
    with pytest.raises(SchemaRefusal, match="differ from the run-sealed"):
        context.write_serving_launch_audit(
            {
                **audit,
                "configuration_inputs": {
                    **context.serving_config_inputs,
                    "pod_placement_sha256": "0" * 64,
                },
            }
        )


def test_serving_evidence_manifest_durably_binds_receipt_and_launch_audit(tmp_path):
    context, identity = _context(tmp_path)
    receipt_reference = context.write_serving_receipt(identity, fixture_serving_details(identity))
    audit_reference = context.write_serving_launch_audit(
        {
            "schema": "serving-launch-audit.v1",
            "chair": identity.role,
            "started_at": "2026-08-09T12:00:00Z",
            "configuration_inputs": dict(context.serving_config_inputs),
        }
    )

    evidence_reference = context.write_serving_evidence_manifest(receipt_reference, audit_reference)
    evidence = context.tree.read_bytes(evidence_reference["relative_path"])
    assert receipt_reference["relative_path"].encode() in evidence
    assert audit_reference["relative_path"].encode() in evidence
    with pytest.raises(SchemaRefusal, match="malformed"):
        context.write_serving_evidence_manifest(
            {"relative_path": "/absolute", "sha256": "c" * 64}, audit_reference
        )


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


@pytest.mark.parametrize(
    ("field", "value"),
    [("endpoint", "fixture://leaked"), ("started_at", "2026-08-05T00:00:00Z")],
)
def test_serving_only_fields_have_their_own_named_provenance_refusal(tmp_path, field, value):
    """Endpoint and start time belong only in the digest-checked run receipt."""
    context, identity = _context(tmp_path)

    with pytest.raises(SchemaRefusal, match="leaks serving-only field"):
        validate_serving_provenance(
            context,
            {**_served_provenance(context, identity), field: value},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_a_provenance_record_cannot_substitute_the_sealed_adapter_recipe(tmp_path):
    context, identity = _context(tmp_path)

    with pytest.raises(SchemaRefusal, match="sealed adapter recipe"):
        validate_serving_provenance(
            context,
            {
                **_served_provenance(context, identity),
                "adapter_revision": adapter_recipe_for(context.run, PERLECTOR),
            },
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_provenance_must_be_an_object_with_a_named_known_chair_state(tmp_path):
    context, identity = _context(tmp_path)
    provenance = _served_provenance(context, identity)

    with pytest.raises(SchemaRefusal, match="is not an object"):
        validate_serving_provenance(
            context,
            [],
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    with pytest.raises(SchemaRefusal, match="has no chair name"):
        validate_serving_provenance(
            context,
            {**provenance, "chair": ""},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    with pytest.raises(SchemaRefusal, match="unknown chair state"):
        validate_serving_provenance(
            context,
            {**provenance, "chair_state": "invented"},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_an_absent_chair_can_make_only_its_declared_non_serving_claim(tmp_path):
    context, identity = _context(tmp_path)
    absent, provenance = _declared_absence(context)

    assert (
        validate_serving_provenance(
            context,
            provenance,
            producer_stage=ATTESTATORES,
            require_receipt=False,
        )
        is None
    )

    with pytest.raises(SchemaRefusal, match="differs from models config"):
        validate_serving_provenance(
            context,
            {**provenance, "absence": {**absent.to_record(), "reason": "invented"}},
            producer_stage=ATTESTATORES,
            require_receipt=False,
        )

    with pytest.raises(SchemaRefusal, match="carries a model identity or receipt"):
        validate_serving_provenance(
            context,
            {**provenance, "resolved_identity": identity.to_record()},
            producer_stage=ATTESTATORES,
            require_receipt=False,
        )

    with pytest.raises(SchemaRefusal, match="cannot have produced a reading"):
        validate_serving_provenance(
            context,
            provenance,
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    with pytest.raises(SchemaRefusal, match="calls configured chair"):
        validate_serving_provenance(
            context,
            {**provenance, "chair": identity.role},
            producer_stage=ATTESTATORES,
            require_receipt=False,
        )


def test_a_configured_chair_must_carry_its_exact_resolved_identity(tmp_path):
    context, identity = _context(tmp_path)
    provenance = _served_provenance(context, identity)

    with pytest.raises(SchemaRefusal, match="has no resolved identity"):
        validate_serving_provenance(
            context,
            {**provenance, "resolved_identity": None},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    with pytest.raises(SchemaRefusal, match="wrong schema"):
        validate_serving_provenance(
            context,
            {**provenance, "resolved_identity": {"role": identity.role}},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    with pytest.raises(SchemaRefusal, match="is malformed"):
        validate_serving_provenance(
            context,
            {
                **provenance,
                "resolved_identity": {**identity.to_record(), "role": "attestator_2"},
            },
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    with pytest.raises(SchemaRefusal, match="differs from the sealed models config"):
        validate_serving_provenance(
            context,
            {
                **provenance,
                "resolved_identity": {**identity.to_record(), "license_note": "invented"},
            },
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_receipt_claims_must_match_the_serving_state_and_resolved_identity(tmp_path, monkeypatch):
    context, identity = _context(tmp_path)
    provenance = _served_provenance(context, identity)

    with pytest.raises(SchemaRefusal, match="was not run but carries a serving receipt"):
        validate_serving_provenance(
            context,
            provenance,
            producer_stage=ATTESTATORES,
            require_receipt=False,
        )

    with pytest.raises(SchemaRefusal, match="has no serving receipt reference"):
        validate_serving_provenance(
            context,
            {**provenance, "receipt_ref": None},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    other = context.registry.resolve("attestator_2")
    assert isinstance(other, ChairIdentity)
    other_reference = context.write_serving_receipt(other, fixture_serving_details(other))
    with pytest.raises(SchemaRefusal, match="differs from the resolved identity"):
        validate_serving_provenance(
            context,
            _served_provenance(context, identity, reference=other_reference),
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )

    # Receipt construction refuses this lie upstream; exercise the consumer's
    # independent defence with the same fully shaped receipt projection.
    class TreeWithUnadaptedReceipt:
        def read_run_receipt(self, reference):
            return {
                "chair": identity.role,
                "source": identity.source,
                "resolved": identity.source_reference,
                "revision": identity.receipt_revision,
                "revision_kind": identity.receipt_revision_kind,
                "digest_manifest": identity.digest_manifest,
                "adapter_identity": other.to_record(),
            }

    monkeypatch.setattr(context, "tree", TreeWithUnadaptedReceipt())
    with pytest.raises(SchemaRefusal, match="unadapted chair"):
        validate_serving_provenance(
            context,
            _served_provenance(
                context,
                identity,
                reference={"relative_path": "receipts/forged.json", "sha256": "0" * 64},
            ),
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
    """Tyrel's 2026-07-30 ruling (courtroom_doctrine.md, formalized in the
    unbuilt spec_08_perlector.md — this is not in ARCHITECTURE.md, which names
    no regime/toggle anywhere): the named/blinded toggle is run-level and every
    Perlectio records its regime. The Perlector writes the field; until this
    check nothing read it back, so a Perlectio claiming a regime that does not
    exist travelled sealed and provenance-checked. Binding it to a real
    run-level toggle is Spec 08's work; refusing a value that cannot be true is
    provenance validation.

    Required of the Perlector and forbidden of everyone else, in both directions.
    An earlier version of this test proved only that a bad value is refused *when
    present*, using `producer_stage=ATTESTATORES` — a producer that does not own
    the field — so it left a Perlectio that recorded no regime at all validating
    cleanly, which is the half of the clause that matters.
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
        "adapter_revision": adapter_recipe_for(context.run, PERLECTOR),
    }
    for regime in ("named", "blinded"):
        assert (
            validate_serving_provenance(
                context,
                {**provenance, "witness_regime": regime},
                producer_stage=PERLECTOR,
                require_receipt=True,
            )
            == identity
        )

    for wrong in ("anonymised", None):
        carried = dict(provenance) if wrong is None else {**provenance, "witness_regime": wrong}
        with pytest.raises(SchemaRefusal, match="witness regime"):
            validate_serving_provenance(
                context, carried, producer_stage=PERLECTOR, require_receipt=True
            )

    with pytest.raises(SchemaRefusal, match="only the Perlector"):
        validate_serving_provenance(
            context,
            {**provenance, "adapter_revision": context.adapter_revision, "witness_regime": "named"},
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_a_consumed_adapter_receipt_must_retain_the_configured_base_identity(tmp_path):
    """A structurally valid base role at a different pin is still wrong provenance."""
    adapter = ChairIdentity(
        role="attestator_adapter",
        source="local-repository",
        repo=None,
        path="adapter",
        revision=None,
        digest_manifest="a" * 64,
        manifest="manifests/adapter.json",
        adapter_of="base",
        serving_recipe="fixture-adapter-v0",
        license_note="fixture only",
    )
    configured_base = ChairIdentity(
        role="base",
        source="local-repository",
        repo=None,
        path="base",
        revision=None,
        digest_manifest="b" * 64,
        manifest="manifests/base.json",
        adapter_of=None,
        serving_recipe="fixture-base-v0",
        license_note="fixture only",
    )
    stale_base = replace(configured_base, digest_manifest="c" * 64)

    class Registry:
        def resolve(self, role):
            return {adapter.role: adapter, configured_base.role: configured_base}[role]

    tree = RunTree.create(
        tmp_path,
        "adapter-receipt",
        source_manifest=[],
        config_digest="d" * 64,
        adapter_recipes={ATTESTATORES: adapter.serving_recipe},
        witness_chairs=[],
    )
    context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture={},
        scenario="test",
        stage=ATTESTATORES,
        adapter_revision=adapter.serving_recipe,
        args=object(),
        registry=Registry(),
    )
    details = replace(fixture_serving_details(adapter), adapter_identity=stale_base)
    reference, _ = tree.write_run_receipt(build_receipt(adapter, details))
    provenance = {
        "chair": adapter.role,
        "chair_state": "configured",
        "resolved_identity": adapter.to_record(),
        "resolved_revision": {
            "kind": adapter.receipt_revision_kind,
            "value": adapter.receipt_revision,
        },
        "receipt_ref": reference.to_record(),
        "adapter_revision": adapter.serving_recipe,
    }

    with pytest.raises(SchemaRefusal, match="configured base identity"):
        validate_serving_provenance(
            context,
            provenance,
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )
