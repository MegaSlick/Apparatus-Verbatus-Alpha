"""The stage-completion seal: what it witnesses, and what it refuses.

The acceptance module proves the seal end to end through real stage programs.
These are the unit-level properties that do not need a run to be true, and that
the pins would only catch by accident: the census's own shape, the deletion of a
seal that leaves a contiguous prefix behind, and the decode-environment
comparison's separation of the machine from the stage's own role.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
)
from common.runtree.store import RunTree
from common.stage import (
    StageContext,
    _decode_environment,
    _stage_records,
    adapter_recipe_for,
    run_config_bindings,
    verify_predecessor_seal,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS_CONFIG = ROOT / "config" / "models.toml"


def _tree(tmp_path: Path) -> tuple[RunTree, dict, ChairRegistry, dict]:
    registry = ChairRegistry.from_toml(MODELS_CONFIG)
    bindings = run_config_bindings(registry.config, {"fixture": "none"}, "test")
    tree = RunTree.create(
        tmp_path,
        "seal-unit",
        source_manifest=[],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
    )
    return tree, tree.read_run(), registry, bindings


def _context(
    tree: RunTree,
    run: dict,
    registry: ChairRegistry,
    bindings: dict,
    *,
    stage: str = ATTESTATORES,
) -> StageContext:
    """A fresh context per pass: `sealed` is a fact about one invocation."""
    return StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario="test",
        stage=stage,
        adapter_revision=adapter_recipe_for(run, stage),
        args=object(),
        registry=registry,
        serving_config_inputs=bindings["serving_config_inputs"],
    )


def _seal_ids(tree: RunTree) -> dict[int, str]:
    return {
        record["payload"]["attempt_ordinal"]: record["artifact_id"]
        for record in _stage_records(tree, ATTESTATORES, "stage-seal")
    }


def _two_sealed_passes(tmp_path: Path) -> tuple[RunTree, dict, ChairRegistry, dict]:
    """Seal once, publish an artifact, seal again — the recovery-re-entry shape."""
    tree, run, registry, bindings = _tree(tmp_path)
    first = _context(tree, run, registry, bindings)
    first.seal_boundary()
    first.finish()

    second = _context(tree, run, registry, bindings)
    second.publish(kind="testimonium", subject_id="act-1", outcome="read", payload={"read": "ink"})
    second.seal_boundary()
    second.finish()

    assert sorted(_seal_ids(tree)) == [1, 2], "the second pass did not witness a second boundary"
    return tree, run, registry, bindings


def test_the_census_counts_this_stage_by_kind_and_outcome_and_excludes_the_boundary(tmp_path):
    """The census is the stage's own arithmetic, not a second run denominator."""
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.publish(kind="testimonium", subject_id="a1", outcome="read", payload={})
    context.publish(kind="testimonium", subject_id="a2", outcome="read", payload={})
    context.publish(kind="testimonium", subject_id="a3", outcome="failed", payload={})
    context.seal_boundary()

    seal = _stage_records(tree, ATTESTATORES, "stage-seal")[0]

    # Sorted by (kind, outcome), counted, and carrying neither of the two kinds
    # the seal itself writes — the fixpoint exclusion, asserted where it is
    # cheap to read rather than only inside a tree digest.
    assert seal["payload"]["census"] == [
        {"kind": "testimonium", "outcome": "failed", "count": 1},
        {"kind": "testimonium", "outcome": "read", "count": 2},
    ]


def test_a_deleted_latest_seal_is_refused_although_its_ordinals_stay_contiguous(tmp_path):
    """Deleting seal N of N leaves 1..N-1, which no contiguity check can see.

    The earlier seal would otherwise answer for a boundary it never witnessed,
    and the next pass would mint the vacated ordinal a second time over a
    different inventory.
    """
    tree, run, registry, bindings = _two_sealed_passes(tmp_path)
    tree.resolve(tree.artifact_path(ATTESTATORES, "stage-seal", _seal_ids(tree)[2])).unlink()

    with pytest.raises(SchemaRefusal, match="never re-derived"):
        _context(tree, run, registry, bindings).seal_boundary()


def test_the_consumer_refuses_a_deleted_seal_the_producer_is_never_asked_about(tmp_path):
    """The next stage is the side reached without invoking the producer again."""
    tree, run, registry, bindings = _two_sealed_passes(tmp_path)
    seals = _seal_ids(tree)
    tree.resolve(tree.artifact_path(ATTESTATORES, "stage-seal", seals[2])).unlink()
    # Revert what the second boundary witnessed, so the surviving seal agrees
    # with disk and every other check in `verify_predecessor_seal` passes.
    testimonium = next(
        entry
        for entry in tree.build_manifest(ATTESTATORES, verify_inputs=False)["artifacts"]
        if entry["kind"] == "testimonium"
    )
    tree.resolve(testimonium["relative_path"]).unlink()

    with pytest.raises(SchemaRefusal, match="never re-derived"):
        verify_predecessor_seal(tree, PERLECTOR)


def test_a_seal_the_stored_inventory_never_named_is_not_a_deletion(tmp_path):
    """A pass that sealed and then died before its manifest write is resumable."""
    tree, run, registry, bindings = _two_sealed_passes(tmp_path)
    stored_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    stored["artifacts"] = [
        entry
        for entry in stored["artifacts"]
        if entry["artifact_id"] != _seal_ids(tree)[2]
        and entry["kind"] not in {"testimonium", "decode-environment"}
    ]
    stored_path.write_text(json.dumps(stored), encoding="utf-8")

    # Nothing named is missing; the extra evidence on disk is the interrupted
    # pass's own, and re-sealing it reuses the statement it already wrote.
    result = _context(tree, run, registry, bindings).seal_boundary()
    assert result.reused


def test_manifest_entry_reordering_does_not_change_the_named_seal_set(tmp_path):
    tree, run, registry, bindings = _two_sealed_passes(tmp_path)
    stored_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    stored["artifacts"].reverse()
    stored_path.write_text(json.dumps(stored), encoding="utf-8")

    assert _context(tree, run, registry, bindings).seal_boundary().reused


def test_a_sibling_stage_named_by_the_stored_manifest_is_a_refusal(tmp_path):
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.seal_boundary()
    context.finish()
    stored_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    stored["stage"] = EXEMPLAR
    stored_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(SchemaRefusal, match="sibling inventory"):
        _context(tree, run, registry, bindings).seal_boundary()


def test_an_ordinal_gap_is_refused_even_without_a_stored_manifest_trigger(tmp_path):
    tree, _, _, _ = _two_sealed_passes(tmp_path)
    first = _seal_ids(tree)[1]
    tree.resolve(tree.artifact_path(ATTESTATORES, "stage-seal", first)).unlink()
    tree.resolve(tree.manifest_path(ATTESTATORES)).unlink()

    with pytest.raises(FatalAccounting, match="not the contiguous run"):
        verify_predecessor_seal(tree, PERLECTOR)


def test_a_sibling_stages_seal_cannot_be_presented_as_the_predecessors(tmp_path):
    tree, run, registry, bindings = _tree(tmp_path)
    sibling_context = _context(tree, run, registry, bindings, stage=EXEMPLAR)
    sibling_context.seal_boundary()
    sibling = _stage_records(tree, EXEMPLAR, "stage-seal")[0]
    sibling_path = tree.resolve(
        tree.artifact_path(ATTESTATORES, "stage-seal", sibling["artifact_id"])
    )
    sibling_path.parent.mkdir(parents=True, exist_ok=True)
    sibling_path.write_bytes(
        tree.resolve(
            tree.artifact_path(EXEMPLAR, "stage-seal", sibling["artifact_id"])
        ).read_bytes()
    )

    with pytest.raises(SchemaRefusal, match="does not occupy its derived path"):
        verify_predecessor_seal(tree, PERLECTOR)


def test_door_deletion_trigger_survives_the_exemplar_manifest_write(tmp_path):
    """The shared artifact directory must not make the two manifests one file."""
    tree, run, registry, bindings = _tree(tmp_path)
    first = _context(tree, run, registry, bindings, stage=DOOR)
    first.seal_boundary()
    first.finish()
    second = _context(tree, run, registry, bindings, stage=DOOR)
    admission = second.publish(
        kind="admission", subject_id="page-1", outcome="admitted", payload={}
    )
    second.seal_boundary()
    second.finish()
    door_seals = {
        record["payload"]["attempt_ordinal"]: record
        for record in _stage_records(tree, DOOR, "stage-seal")
    }

    exemplar = _context(tree, run, registry, bindings, stage=EXEMPLAR)
    exemplar.seal_boundary()
    exemplar.finish()
    assert tree.manifest_path(DOOR) != tree.manifest_path(EXEMPLAR)

    tree.resolve(tree.artifact_path(DOOR, "stage-seal", door_seals[2]["artifact_id"])).unlink()
    tree.resolve(admission.relative_path).unlink()

    with pytest.raises(SchemaRefusal, match="never re-derived"):
        _context(tree, run, registry, bindings, stage=DOOR).seal_boundary()
    with pytest.raises(SchemaRefusal, match="never re-derived"):
        verify_predecessor_seal(tree, EXEMPLAR)


def test_stage_role_fields_are_reported_by_name_without_refusal(tmp_path, capsys):
    """The consult requires every field compared; Unit 17 owns fatality."""
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.seal_boundary()
    context.finish()
    assert _decode_environment(ATTESTATORES)["produced_pixels"] is False
    assert _decode_environment(PERLECTOR)["produced_pixels"] is True
    capsys.readouterr()

    verify_predecessor_seal(tree, PERLECTOR)

    reported = capsys.readouterr().err
    assert "decode environment differs by name from attestatores" in reported
    assert "decode_paths_used" in reported
    assert "produced_pixels" in reported


def test_a_decoder_version_that_moved_between_stages_is_reported_by_name(
    tmp_path, capsys, monkeypatch
):
    """Still an observation, never a refusal: Unit 17 owns the fatal policy."""
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.seal_boundary()
    context.finish()

    import common.stage as stage_module

    moved = _decode_environment(PERLECTOR)
    moved["decoders"] = [
        dict(row, version="0.0.0-moved") if row["name"] == "pillow" else row
        for row in moved["decoders"]
    ]
    monkeypatch.setattr(stage_module, "_decode_environment", lambda _: moved)
    capsys.readouterr()

    verify_predecessor_seal(tree, PERLECTOR)

    reported = capsys.readouterr().err
    assert "decode environment differs by name from attestatores" in reported
    assert "pillow" in reported


def test_decode_environment_bytes_cannot_change_under_an_existing_seal(tmp_path):
    """The seal binds the environment record, not only its deterministic name."""
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.seal_boundary()
    context.finish()
    seal = _stage_records(tree, ATTESTATORES, "stage-seal")[0]
    environment_id = seal["payload"]["decode_environment_artifact_id"]
    environment = tree.read_artifact(ATTESTATORES, "decode-environment", environment_id)
    environment["payload"]["platform"] += "-changed-after-seal"
    environment["self_hash"] = self_hash(environment)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "decode-environment", environment_id))
    path.write_bytes(canonical_bytes(environment))

    with pytest.raises(SchemaRefusal, match="decode-environment digest differs"):
        verify_predecessor_seal(tree, PERLECTOR)


def test_a_malformed_decode_environment_is_a_named_refusal_not_a_difference(tmp_path):
    """Report-only applies to valid differences, not to a forged record shape."""
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.seal_boundary()
    context.finish()
    seal = _stage_records(tree, ATTESTATORES, "stage-seal")[0]
    environment_id = seal["payload"]["decode_environment_artifact_id"]
    environment = tree.read_artifact(ATTESTATORES, "decode-environment", environment_id)
    environment["payload"]["produced_pixels"] = "not-a-boolean"
    environment["self_hash"] = self_hash(environment)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "decode-environment", environment_id))
    path.write_bytes(canonical_bytes(environment))

    with pytest.raises(SchemaRefusal, match="malformed produced_pixels"):
        verify_predecessor_seal(tree, PERLECTOR)


def test_the_orchestrator_consumes_armariums_seal_not_archetypus_again(tmp_path):
    tree, run, registry, bindings = _tree(tmp_path)
    archetypus = _context(tree, run, registry, bindings, stage=ARCHETYPUS)
    archetypus.seal_boundary()
    archetypus.finish()
    armarium = _context(tree, run, registry, bindings, stage=ARMARIUM)
    armarium.seal_boundary()
    armarium.finish()
    final_seal = _stage_records(tree, ARMARIUM, "stage-seal")[0]
    tree.resolve(tree.artifact_path(ARMARIUM, "stage-seal", final_seal["artifact_id"])).unlink()

    with pytest.raises(SchemaRefusal, match="orchestrator refuses: predecessor armarium"):
        verify_predecessor_seal(tree, "orchestrator")
