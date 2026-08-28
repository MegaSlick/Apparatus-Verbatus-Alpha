"""The stage-completion seal: what it witnesses, and what it refuses.

The acceptance module proves the seal end to end through real stage programs.
These are the unit-level properties that do not need a run to be true, and that
the pins would only catch by accident: the census's own shape, the deletion of a
seal that leaves a contiguous prefix behind, and the decode-environment
comparison's separation of the machine from the stage's own role.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
)
from common.runtree.store import RunTree
from common.stage import (
    StageContext,
    _decode_environment,
    _stage_blob_inventory,
    _stage_records,
    adapter_recipe_for,
    refuse_halted_run,
    run_config_bindings,
    verify_final_seal,
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


def test_a_run_authority_missing_a_sealed_binding_is_refused_not_crashed_on(tmp_path):
    """`read_run` proves a self-hash, a schema, and a run id — not a field list.

    So an authority not written by `RunTree.create` reaches the seal whole and
    still missing one of the fields the seal witnesses. Subscripting it raised a
    bare KeyError, which `run_stage` catches neither as a ContractError nor as a
    RunHalted: the stage ended in a traceback naming a dict key rather than one
    of its four honest exit codes. The verifier reads both fields with `.get`
    and tolerates their absence, so this side has to name the gap itself.
    """
    tree, run, registry, bindings = _tree(tmp_path)
    authority = json.loads((tree.root / "run.json").read_text())
    del authority["register_digest"], authority["self_hash"]
    authority["self_hash"] = self_hash(authority)
    (tree.root / "run.json").write_bytes(canonical_bytes(authority))
    assert "register_digest" not in tree.read_run(), "read_run still accepts the authority"

    context = _context(tree, tree.read_run(), registry, bindings)
    context.publish(kind="testimonium", subject_id="a1", outcome="read", payload={})

    with pytest.raises(SchemaRefusal, match="carries no register_digest"):
        context.seal_boundary()


def test_a_stage_cannot_seal_an_artifact_whose_input_bytes_changed(tmp_path):
    """The inventory verifies its hash links, not only the artifact files."""
    tree, run, registry, bindings = _tree(tmp_path)
    _digest, source = tree.put_blob(DESIGNATOR, b"the bytes the witness consumed")
    context = _context(tree, run, registry, bindings)
    context.publish(
        kind="testimonium",
        subject_id="act-1",
        outcome="read",
        inputs=[context.input_ref(source.relative_path)],
        payload={"read": "ink"},
    )
    tree.resolve(source.relative_path).write_bytes(b"changed after the testimony")

    with pytest.raises(SchemaRefusal, match="bytes changed under"):
        context.seal_boundary()
    assert not _stage_records(tree, ATTESTATORES, "stage-seal")


def test_a_stage_refuses_a_blob_whose_content_does_not_match_its_name(tmp_path):
    """A computed blob digest must be compared with its content address."""
    tree, run, registry, bindings = _tree(tmp_path)
    _digest, blob = tree.put_blob(ATTESTATORES, b"original")
    tree.resolve(blob.relative_path).write_bytes(b"different")

    with pytest.raises(SchemaRefusal, match="not the digest in its name"):
        _context(tree, run, registry, bindings).seal_boundary()
    assert not _stage_records(tree, ATTESTATORES, "stage-seal")


def test_serving_evidence_cannot_be_stored_after_the_boundary_is_sealed(tmp_path):
    """The post-seal guard covers blobs, not only artifacts.

    `_write_serving_blob` goes through `tree.put_blob` into the stage's own blob
    directory — the one `_stage_blob_inventory` walks and whose digest the seal
    carries. A write afterwards makes the witnessed inventory false, and the
    symptom lands on the wrong stage: the next consumer refuses with "its named
    inventory no longer matches disk", reporting an honest producer as a tampered
    tree. The second half here shows that consequence directly.
    """
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings)
    context.publish(kind="testimonium", subject_id="a1", outcome="read", payload={})
    context.seal_boundary()

    with pytest.raises(SchemaRefusal, match="witnessed blob inventory false"):
        context._write_serving_blob({"chair": "attestator_1"}, "a serving launch audit")

    # What the guard prevents: the same bytes written straight to the store leave
    # the stored seal answering for an inventory that is no longer on disk.
    tree.put_blob(ATTESTATORES, b"evidence written after the boundary")
    with pytest.raises(SchemaRefusal, match="named inventory no longer matches disk"):
        verify_predecessor_seal(tree, PERLECTOR)


def test_an_interrupted_publish_leaves_a_blob_that_can_still_be_sealed(tmp_path):
    """SIGKILL between the publisher's link and its unlink must not end the run.

    `_atomic_create` hard-links `.<digest>.tmp-<unique>` onto the digest name and
    then unlinks the temporary. Killed in between, the published blob keeps a
    second link. Its bytes are complete and its digest matches its name, but the
    inventory refused it for the link count — so a run killed at the wrong
    microsecond could never seal again, and the message accused intact evidence.
    """
    tree, run, registry, bindings = _tree(tmp_path)
    digest, published = tree.put_blob(ATTESTATORES, b"page pixels")
    blob = tree.resolve(published.relative_path)
    os.link(blob, blob.parent / f".{digest}.tmp-interrupted")
    assert blob.stat().st_nlink == 2

    _context(tree, run, registry, bindings).seal_boundary()

    seal = _stage_records(tree, ATTESTATORES, "stage-seal")[0]
    assert seal["payload"]["blob_inventory"], "the interrupted publish still sealed"


def test_a_second_link_the_publisher_cannot_explain_is_still_refused(tmp_path):
    """The allowance is exactly the publisher's own leftover and nothing else."""
    tree, run, registry, bindings = _tree(tmp_path)
    _digest, published = tree.put_blob(ATTESTATORES, b"page pixels")
    blob = tree.resolve(published.relative_path)
    os.link(blob, tmp_path / "reachable-from-outside")

    with pytest.raises(SchemaRefusal, match="did not publish it under"):
        _context(tree, run, registry, bindings).seal_boundary()


# Two layers refuse a planted blob symlink, and which one speaks first is not a
# property either of them promises. The run tree's own fd-bound blob walk runs
# while the seal's manifest is built, so it now answers before the stage's
# inventory reaches the entry; both refusals are kept, and these tests assert that
# one of them fires rather than pinning the layer.
_SYMLINK_REFUSAL = r"no-follow|resolves outside the run tree"


def test_a_stage_refuses_case_variant_and_symlink_blob_entries(tmp_path):
    """The blob namespace is lowercase and never follows a planted link."""
    tree, run, registry, bindings = _tree(tmp_path)
    blob_root = tree.resolve(tree.blob_path(ATTESTATORES, "0" * 64)).parent
    blob_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside the run tree")
    link_name = digest_bytes(outside.read_bytes())
    (blob_root / link_name).symlink_to(outside)

    with pytest.raises(SchemaRefusal, match=_SYMLINK_REFUSAL):
        _context(tree, run, registry, bindings).seal_boundary()

    (blob_root / link_name).unlink()
    (blob_root / link_name.upper()).write_bytes(outside.read_bytes())
    with pytest.raises(SchemaRefusal, match="noncanonical content address"):
        _context(tree, run, registry, bindings).seal_boundary()


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


def test_the_final_reader_uses_the_same_named_seal_set_deletion_check(tmp_path):
    """Armarium has no next stage, but its orchestrator reader is not weaker."""
    tree, run, registry, bindings = _tree(tmp_path)
    first = _context(tree, run, registry, bindings, stage=ARMARIUM)
    first.seal_boundary()
    first.finish()
    first_paths = {
        entry["relative_path"]
        for entry in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
    }

    second = _context(tree, run, registry, bindings, stage=ARMARIUM)
    second.publish(kind="test-output", subject_id="changed", outcome="delivered", payload={})
    second.seal_boundary()
    second.finish()
    second_paths = {
        entry["relative_path"]
        for entry in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
    }
    for relative_path in second_paths - first_paths:
        tree.resolve(relative_path).unlink()

    # Disk now agrees with the first boundary again, while the stored manifest
    # still names the removed second seal. Only the witnessed named-set deletion
    # check can distinguish this from an honestly single-pass Armarium.
    with pytest.raises(SchemaRefusal, match="never re-derived"):
        verify_final_seal(tree)


def test_the_final_reader_refuses_an_armarium_seal_whose_decode_environment_is_gone(tmp_path):
    """The orchestrator's own half of the terminal decode-environment check.

    ``pipeline/orchestrator/test_run_modes.py`` used to prove this end to end by
    deleting the record and rerunning, but the seal now binds that record's bytes,
    so the Armarium producer refuses first and the orchestrator's reader is no
    longer reachable that way. It is still the last reader of a boundary with no
    stage successor, so it is driven directly here instead of going unproven.
    """
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings, stage=ARMARIUM)
    context.seal_boundary()
    context.finish()
    record = next(
        entry
        for entry in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if entry["kind"] == "decode-environment"
    )
    tree.resolve(record["relative_path"]).unlink()

    with pytest.raises(SchemaRefusal, match="decode-environment is missing or damaged") as refusal:
        verify_final_seal(tree)
    assert "the orchestrator refuses armarium stage-seal" in str(refusal.value)


def test_a_sigkill_blob_orphan_is_not_witnessed_as_published_evidence(tmp_path):
    """SIGKILL before the atomic link leaves the temporary bytes unpublished."""
    tree, run, registry, bindings = _tree(tmp_path)
    first = _context(tree, run, registry, bindings)
    first.seal_boundary()
    first.finish()
    original = _stage_records(tree, ATTESTATORES, "stage-seal")[0]

    killed_writer = """
import os
import signal
import sys
from pathlib import Path

import common.runtree.store as store
from common.runtree.store import RunTree

def die_before_publication(_source, _target):
    os.kill(os.getpid(), signal.SIGKILL)

store.os.link = die_before_publication
RunTree(Path(sys.argv[1]), "seal-unit").put_blob("attestatores", b"interrupted blob")
"""
    killed = subprocess.run(
        [sys.executable, "-c", killed_writer, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert killed.returncode == -signal.SIGKILL
    blobs_root = tree.resolve(tree.blob_path(ATTESTATORES, "0" * 64)).parent
    orphans = [path for path in blobs_root.iterdir() if path.name.startswith(".")]
    assert len(orphans) == 1
    assert ".tmp-" in orphans[0].name

    repeated = _context(tree, run, registry, bindings).seal_boundary()

    assert repeated.reused
    seals = _stage_records(tree, ATTESTATORES, "stage-seal")
    assert len(seals) == 1
    assert seals[0]["payload"]["blob_inventory"] == original["payload"]["blob_inventory"]

    # The exception is the publisher's exact private convention, not "dotfiles".
    # An unrelated name in the evidence directory is REFUSED rather than merely
    # witnessed: work/staged-stage-seal's inventory refuses every content address
    # that is not a canonical sha256, and the merge keeps that refusal with the
    # temporary-name hole punched in it and nothing else. Refusing is strictly
    # louder than recording, so the "can never disappear behind this exception"
    # guarantee this test was written for still holds -- the run stops instead.
    (blobs_root / ".unexpected-published-name").write_bytes(b"must stay visible")
    with pytest.raises(SchemaRefusal, match="noncanonical content address"):
        _context(tree, run, registry, bindings).seal_boundary()
    assert len(_stage_records(tree, ATTESTATORES, "stage-seal")) == 1


def test_a_blob_symlink_is_refused_even_when_it_looks_like_an_unpublished_temporary(tmp_path):
    """The temporary-name exception is not permission to follow or ignore a symlink."""
    tree, run, registry, bindings = _tree(tmp_path)
    blobs_root = tree.resolve(tree.blob_path(ATTESTATORES, "0" * 64)).parent
    blobs_root.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"bytes outside the evidence directory")
    (blobs_root / f".{('a' * 64)}.tmp-attacker").symlink_to(outside)

    with pytest.raises(SchemaRefusal, match=_SYMLINK_REFUSAL):
        _context(tree, run, registry, bindings).seal_boundary()


def test_a_case_variant_blob_digest_is_refused_portably(tmp_path):
    """A tree cannot acquire different names on default APFS and Linux."""
    tree, run, registry, bindings = _tree(tmp_path)
    blobs_root = tree.resolve(tree.blob_path(ATTESTATORES, "0" * 64)).parent
    blobs_root.mkdir(parents=True)
    (blobs_root / ("A" * 64)).write_bytes(b"not canonically named")

    with pytest.raises(SchemaRefusal, match="non-canonical case variant"):
        _context(tree, run, registry, bindings).seal_boundary()


def test_blob_inventory_hashes_large_evidence_in_bounded_chunks(tmp_path, monkeypatch):
    """A large retained blob cannot force a second full-size in-memory copy.

    The bound is the property under test, not one branch's spelling of the read
    loop: the surviving inventory hashes through ``hashlib.file_digest``, which
    fills a fixed buffer with ``readinto`` rather than calling ``read(1 << 20)``.
    Both routes are recorded here so the assertion stays about how much of the
    file can be resident at once.
    """
    tree, _run, _registry, _bindings = _tree(tmp_path)
    blobs_root = tree.resolve(tree.blob_path(ATTESTATORES, "0" * 64)).parent
    blobs_root.mkdir(parents=True)
    payload = b"x" * ((2 << 20) + 17)
    (blobs_root / digest_bytes(payload)).write_bytes(payload)
    real_fdopen = os.fdopen
    read_sizes = []

    class BoundedReader:
        def __init__(self, descriptor, mode="rb", *args, **kwargs):
            self._stream = real_fdopen(descriptor, mode, *args, **kwargs)

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self._stream.__exit__(*exc_info)

        def readinto(self, buffer):
            read_sizes.append(len(buffer))
            return self._stream.readinto(buffer)

        def read(self, size=-1):
            read_sizes.append(size)
            return self._stream.read(size)

        def __getattr__(self, name):
            return getattr(self._stream, name)

    import common.stage as stage_module

    monkeypatch.setattr(stage_module.os, "fdopen", BoundedReader)

    inventory = _stage_blob_inventory(tree, ATTESTATORES)

    assert inventory[0]["name"] == digest_bytes(payload)
    assert read_sizes and max(read_sizes) <= 1 << 20
    assert -1 not in read_sizes, "an unbounded read would copy the whole blob at once"


def test_final_seal_returns_only_the_export_from_its_verified_manifest_snapshot(
    tmp_path, monkeypatch
):
    """Replacement bytes after the manifest check cannot reach terminal reporting."""
    tree, run, registry, bindings = _tree(tmp_path)
    context = _context(tree, run, registry, bindings, stage=ARMARIUM)
    published = context.publish(
        kind="export",
        subject_id="export",
        outcome="delivered",
        payload={"aggregate": {"status": "complete", "reasons": []}},
    )
    context.seal_boundary()
    context.finish()
    export_path = tree.resolve(published.relative_path)
    replacement = tree.read_artifact(
        ARMARIUM, "export", published.relative_path.split("/")[-1][:-5]
    )
    replacement["payload"]["aggregate"]["status"] = "partial"
    replacement["self_hash"] = self_hash(replacement)

    original_build_manifest = tree.build_manifest
    swapped = False

    def snapshot_then_replace(stage, *, verify_inputs=True):
        nonlocal swapped
        manifest = original_build_manifest(stage, verify_inputs=verify_inputs)
        if stage == ARMARIUM and not swapped:
            swapped = True
            export_path.write_bytes(canonical_bytes(replacement))
        return manifest

    monkeypatch.setattr(tree, "build_manifest", snapshot_then_replace)

    with pytest.raises(SchemaRefusal, match="changed between its manifest snapshot and use"):
        verify_final_seal(tree)


def test_a_real_run_missing_the_named_hard_failure_digest_refuses_direct_entry(tmp_path):
    """Losing the cap's proof cannot turn a real run into an uncapped legacy fixture."""
    registry = ChairRegistry.from_toml(MODELS_CONFIG)
    bindings = run_config_bindings(registry.config, {"fixture": "none"}, "test")
    tree = RunTree.create(
        tmp_path,
        "real-missing-cap",
        source_manifest=[],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress={"mode": "real"},
    )

    with pytest.raises(ContractError, match="seals no hard-failure configuration digest"):
        refuse_halted_run(tree, PERLECTOR, ROOT / "config" / "hard_failure.toml")


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
